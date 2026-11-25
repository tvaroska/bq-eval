---
type: atom
related:
  - "layers.md"
  - "silver.md"
  - "gold.md"
  - "attributes.md"
  - "../design/capacity.md"
tags: [bq-eval, silver, capacity, extension]
---

# Extension: capacity and governance

> What is added when an organization runs a central multi-tenant LLM gateway, buys dedicated capacity, or enforces scoped model governance. The core contract in [silver.md](silver.md) stays modular and does not depend on this note; see [design/capacity.md](../design/capacity.md) for the implementation topology.

## Objects

| Object | One row per | Holds |
|---|---|---|
| `requests` (view / incremental table) | upstream model attempt, as the custom gateway or model server saw it | Mandatory join key `x-client-request-id` + `(trace_id, span_id)` (plus optional `gen_ai.response.id`), `attempt_index` (`1..N` across gateway failover), caller/desk `trace_owner_app_id`, `billing_project_id`, `upstream_endpoint_location`, `state_mode`, billed tokens (incl. `cache_write_5m` vs. `cache_write_1h`), weighted burndown units, serving tier (`PROVISIONED_THROUGHPUT \| PRIORITY \| STANDARD \| FLEX`), `tier_source` (`server \| inferred \| inferred_ambiguous`), HTTP status code, `pool_id`, `runtime_pool_id`, `ttft_ms`, `thought_signature_present`, `traffic_type_attested`, and write-IAM-attested OBO claims. |
| `burst_metrics_1m` (rollup table) | `(minute_utc, runtime_pool_id, trace_owner_app_id)` | Explicit BigQuery schema: `minute_utc`, `runtime_pool_id`, `trace_owner_app_id`, `risk_tier`, `window_sec` (`1 \| 10 \| 30 \| 60`), `weighted_burndown_units`, `committed_capacity_units`, `peak_burst_ratio`, `pt_requests`, `priority_spillover_requests`, `standard_spillover_requests`, `involuntary_spillover_requests`, `http_429_count`, `gateway_failover_count`, `caller_retry_count`. |
| Capacity pools (`capacity_pools`) | `order_id` (`order_id -> runtime_pool_id`) and validity window (`effective_from`, `effective_to`) | Cloud, scope key (GCP project / Shared-PT pool, Azure PTU deployment, or AWS PT ARN), `runtime_pool_id` (merged FCFS runtime bucket across split commercial orders), model, region, committed units, `unit_rate`, `renewal_type` (`AUTO_RENEW \| FIXED_TERM`), `expiry_date`, `min_cache_tokens`, and provider capability flags (`supports_priority_paygo`, `emits_traffic_type`). |
| Capacity allocations (`capacity_allocations`) | `pool_id` / `order_id`, validity window, and consumer (`app_id` or business unit) | Funded share of that capacity pool (`app_registry.risk_tier` is the single authority for `RL-1`..`RL-5` 429 risk tolerance). |
| Allowed models (`allowed_models`) | `(provider, model, region, environment, business_unit, risk_tier, app_id)` and validity window (`effective_from`, `effective_to`) | Scoped model governance (`is_allowed`, `invokable_status`: `active \| drain \| circuit_broken`, `zdr_allowed`, `lifecycle_tier`: `short_term_ga \| long_term_ga \| preview`, `eol_date`, `deprecation_notice_days`, `migration_target_model`, `min_cache_tokens`). |

## Rules

- **Mandatory `1:N` correlation & caller ownership (`ADR-032`):** Joining `requests` to `model_calls` on `gen_ai.response.id` alone breaks because frameworks omit it and failed `429` legs have no response ID. Gateways **must propagate `x-client-request-id` + `(trace_id, span_id)`** (and `traceparent`) across every attempt, and stamp caller/desk `trace_owner_app_id` with precedence over the gateway's own root service name.
- **`1:N` failover pricing order (price per attempt, then aggregate):** One agent `model_calls` span may trigger `N` upstream `requests` attempts (`429` on PT failing over to Priority PayGo). Gold **prices every `requests` attempt individually first** (using that attempt's actual `serving_tier`, `upstream_endpoint_location`, 1P/3P `>200K` cliffs keyed on `input_tokens + cache_read_tokens`, and `5m`/`1h` cache splits) and then collapses `(billed_cost_usd, billed_tokens, weighted_burndown_units, gateway_failover_attempts, throttled_attempt_count)` per `(x-client-request-id, span_id)` before joining `model_calls`. Where both exist, the gateway/server side is the authority for billed tokens, serving tier, and pool attribution.
- **Gateway failover vs. caller retry storms:** `gateway_failover_attempts` counts multiple upstream `requests` under one caller `span_id` (healthy single-endpoint spillover); `caller_retry_count` counts directly preceding `model_calls` in the same trace, by the same agent for the same `request_model` (ordered by `start_time, span_id`), that ended with HTTP `429` (`ADR-039`, `ADR-045`, catching illegal caller SDK `attempts > 1` retry amplification).
- **Honest 3P missing-`TrafficType` attribution (`inferred_ambiguous`):** When a third-party model server omits serving-tier response metadata (`emits_traffic_type = false`) and a shared pool can spill downstream without signaling it, `requests` **must never claim false determinism** — it emits `tier_source = 'inferred_ambiguous'` (reserving `'inferred'` strictly for dedicated no-spillover routes and `'server'` for server-attested `TrafficType`), and Gold reconciles aggregate 3P PT vs. PayGo spend against Cloud Billing exports.
- **Explicit 3-pool `FOCUS 1.2` spillover, pro-rata `order_id` & invoice reconciliation (`ADR-032`):**
  1. *Weighted burndown units:* Compute per-attempt weighted units using provider multipliers (e.g., `1x` input, `0.1x` cache read, `4x` output/reasoning at `<= 200K` total context `input + cache_read`; `2x` / `0.2x` / `7.5x` above `200K`).
  2. *3-pool cost split:* Separate (a) **Base PT pool** (`committed_units * unit_rate` split into active-hours funded share, off-peak backfill credit, and unallocated idle pool), (b) **Priority PayGo spillover** (`priority_multiplier * base_rate` on `PRIORITY` attempts), and (c) **Standard / Sheddable PayGo** (`STANDARD` / `FLEX` attempts).
  3. *Merged FCFS `order_id` attribution:* When multiple commercial orders (`FIXED_TERM` expiring cohorts and `AUTO_RENEW` monthly orders) share one merged FCFS `runtime_pool_id`, attribute pool consumption **pro-rata by `committed_units`** across active `order_id`s (or by explicit `order_id` priority policy) so expiring fixed-term renewal exposure is tracked separately.
  4. *Billing reconciliation:* Reconcile modeled Gold spend against Cloud Billing `FOCUS 1.2` invoice/net-realized exports (including unmapped `workload_id = NULL` subscription rows), publishing reconciliation delta % rather than silently trusting SKU catalog estimates.
- **Bidirectional `bq-eval` <-> `capacity` Ports, Adapters and Contracts (`ADR-001` preserved):** `bq-eval` remains strictly observational and never actuates live `429` shedding. `bq-eval` exports versioned hourly chargeback + `burst_metrics_1m` + `RL-1`..`RL-5` risk tiers to `capacity`; `capacity` computes Newsvendor sizing and shedding thresholds and publishes gateway admission configs to the Preventive Request-Path Enforcement Port.
- **Scoped SCD-2 governance & sanitized LOB lifecycle exports:** `allowed_models`, `capacity_pools`, and `capacity_allocations` carry mandatory SCD-2 `(effective_from, effective_to)` windows. Gold enforces `allowed_models` (including blocking `short_term_ga` models on `RL-1`/`RL-2` production apps and enforcing `zdr_allowed = true` on Gold judges) and publishes a **sanitized LOB lifecycle export** (`model`, `lifecycle_tier`, `eol_date`, `deprecation_notice_days`, `migration_target_model` — zero raw project/order IDs).
- **Eval/optimize PT isolation & embeddings pools:** Gold flags `agent_gym_eval` or `optimize` traffic hitting daytime `RL-1`/`RL-2` PT pools and attributes `embeddings` pool load alongside LLM inference.

Without this extension, cost uses application-reported tokens and throttling reflects only application spans.
