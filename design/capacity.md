---
type: atom
up: "layers.md"
tags: [bq-eval, silver, capacity, extension]
---

# Extension: capacity and governance

> How custom-proxy `requests`, 3-pool `FOCUS 1.2` chargeback, and scoped model governance are built alongside [Silver](silver.md) without bloating the core or breaking `ADR-001` repository separation. Contract: [specs/capacity.md](../specs/capacity.md).

## Custom Proxy Bronze Contract, `requests` view & `1:N` failover reconciliation

`requests` is a SQL view (or bounded incremental partitioned table in `enterprise` mode) over central LLM gateway or model-server request logs (`_AllLogs` `inference_request` on GCP, S3 Parquet/Iceberg proxy logs on AWS), emitting one row per upstream attempt keyed by **`(x-client-request-id, span_id, attempt_index)`**:

- **Mandatory correlation & caller ownership:** Because frameworks omit `gen_ai.response.id` and failed `429` legs have none, the gateway propagates `x-client-request-id` + `traceparent` (`trace_id`, `span_id`) across every attempt and stamps caller/desk `trace_owner_app_id` (taking precedence over the proxy's own root service name).
- **Option A / Option B neutrality (`state_mode`):** Carries `state_mode` (`stateless_chat | stateful_cmek_platform | stateful_cmek_gateway | zdr_stateless`) so thin stateless proxies (Option A) and dual-lane stateful `/interactions` proxies (Option B) emit identical Silver/Gold and `FOCUS 1.2` chargeback records.
- **Price per attempt, then collapse:** Gold prices **each `requests` attempt individually** (using its actual `serving_tier`, `upstream_endpoint_location`, `>200K` cliffs keyed on `input_tokens + cache_read_tokens`, weighted burndown units `1x/0.1x/4x` vs. `2x/0.2x/7.5x`, and `5m`/`1h` cache splits) and collapses to one summary per `(x-client-request-id, span_id)` before joining `model_calls`. `gateway_failover_attempts` stays strictly separated from illegal SDK `caller_retry_count` storms (`attempts > 1`).
- **Honest 3P `inferred_ambiguous` tiering:** When `emits_traffic_type = false` on a third-party server where shared pools can spill downstream without signaling it, `requests` emits `tier_source = 'inferred_ambiguous'` (never falsely claiming deterministic `'inferred'`), leaving aggregate 3P PT-vs-PayGo split to Cloud Billing reconciliation.

## Scoped SCD-2 governance & 3-pool `FOCUS 1.2` reconciliation

`capacity_pools`, `capacity_allocations`, and `allowed_models` enforce mandatory SCD-2 `(effective_from, effective_to)` windows joined at `call.start_time`:

- **`capacity_pools` & merged FCFS `order_id` attribution:** Maps individual commercial `order_id`s (`renewal_type = FIXED_TERM | AUTO_RENEW`, `expiry_date`, `committed_units`, `unit_rate`, `min_cache_tokens`, `supports_priority_paygo`, `emits_traffic_type`) to their shared FCFS `runtime_pool_id`. Gold attributes merged-pool usage **pro-rata by `committed_units`** (or explicit priority policy) so expiring fixed-term renewal exposure stays isolated from auto-renew orders.
- **3-pool spillover & Cloud Billing invoice reconciliation:** Splits spend explicitly across (1) **Base PT** (active-hours funded share, off-peak backfill credit, unallocated idle pool), (2) **`1.8x` Priority PayGo spillover**, and (3) **Standard/Flex PayGo**, and reconciles modeled Gold totals against Cloud Billing `FOCUS 1.2` invoice/net-realized exports (capturing unmapped `workload_id = NULL` subscription rows and reporting reconciliation delta %).
- **Extended `allowed_models` & sanitized LOB lifecycle export:** Scoped by `(provider, model, region, environment, business_unit, risk_tier, app_id)` with `is_allowed`, `invokable_status` (`active | drain | circuit_broken`), `zdr_allowed`, `lifecycle_tier`, `eol_date`, `deprecation_notice_days`, `migration_target_model`, and `min_cache_tokens`. Publishes a sanitized LOB lifecycle export (zero raw project/order IDs).

## Sub-minute `burst_metrics_1m` & bidirectional `bq-eval` <-> `capacity` contract

Sub-minute weighted burndown (`1s`/`10s` for 1P; `30s`/`60s` for 3P), involuntary vs. burst spillover counters, and `~2 ms` live `429` shedding run inside the **Preventive Request-Path Enforcement Port** (gateway/collector), writing the fixed `burst_metrics_1m` BigQuery rollup schema (`minute_utc`, `runtime_pool_id`, `trace_owner_app_id`, `risk_tier`, `window_sec`, `weighted_burndown_units`, `committed_capacity_units`, `peak_burst_ratio`, `pt/priority/standard/involuntary_spillover_requests`, `http_429_count`, `gateway_failover_count`, `caller_retry_count`).

Per `ADR-001`, `bq-eval` never actuates live shedding: it exports hourly `FOCUS 1.2` chargeback + `burst_metrics_1m` + `RL-1`..`RL-5` risk tiers to `capacity`, which computes Newsvendor PT sizing and gateway admission policies.
