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

> What is added when an organisation buys dedicated capacity, runs a gateway, or keeps a model allow-list. The core contract in [silver.md](silver.md) stays modular and does not depend on this note; see [design/capacity.md](../design/capacity.md) for the implementation topology.

## Objects

| Object | One row per | Holds |
|---|---|---|
| `requests` (view / incremental table) | upstream model attempt, as the gateway or model server saw it | `request_id` / `span_id` join key, `attempt_index` (`1..N` across failover/retries), `trace_id` when sent, `billing_project_id`, `upstream_endpoint_location`, billed tokens (including optional `cache_write_5m` vs. `cache_write_1h` split), serving tier (`PROVISIONED_THROUGHPUT \| PRIORITY \| STANDARD \| FLEX`), HTTP status code, `pool_id`, `ttft_ms`, `thought_signature_present`, and attested OBO identity where the gateway verifies tokens. |
| Capacity pools (`capacity_pools`) | `pool_id` (or `order_id -> runtime_pool_id`) and validity window (`effective_from`, `effective_to`) | Cloud, scope key (GCP project / Shared-PT pool, Azure PTU deployment, or AWS PT ARN), `runtime_pool_id` (modeling merged FCFS runtime buckets across split orders), model, region, committed units, and expiry date. |
| Capacity allocations (`capacity_allocations`) | `pool_id`, validity window, and consumer (`app_id` or business unit) | Funded share of that capacity pool and workload risk tier (`RL-1`..`RL-5`). |
| Allowed models (`allowed_models`) | `(provider, model, environment, business_unit)` and validity window (`effective_from`, `effective_to`) | Scoped model governance (`is_allowed`, `zdr_allowed`, `lifecycle_tier`: `short_term_ga \| long_term_ga \| preview`, `eol_date`). |

## Rules

- **`1:N` failover reconciliation (never raw `1:1`):** One agent `model_calls` span may trigger `N` upstream `requests` attempts (for example, `429` on Provisioned Throughput failing over to Priority PayGo). Gold aggregates `requests` by `(request_id / span_id)` (preserving total billed tokens, `attempt_count`, `throttled_attempt_count`, and final fulfillment tier) before joining `model_calls`, so multi-attempt spillover never fans out rows or hides `429` saturation. Where both exist, the gateway/server side is the authority for billed tokens, serving tier, and dedicated-pool attribution.
- **3P missing-`TrafficType` fallback:** When a third-party model server does not return serving-tier metadata on responses, `requests` derives fulfillment tier deterministically from the gateway's admission policy/headers and pool mapping, flagging inferred vs. server-attested tier.
- **Scoped SCD-2 governance + Gold enforcement:** `allowed_models`, `capacity_pools`, and `capacity_allocations` carry `effective_from` / `effective_to` windows so historical chargeback and compliance audits remain reproducible. Silver flags unapproved or expiring models on `model_calls`; Gold enforces `allowed_models` as an explicit deterministic governance gate ([gold.md](gold.md#1-evaluation-add-on)).
- **Sub-minute burst metrics vs. BigQuery rollups:** Sub-minute weighted burndown (`1s`/`10s` for 1P pools, `30s`/`60s` for 3P pools) and `<1m` admission guards run at the gateway/collector metrics Port (exporting `1m` rollups to BigQuery) rather than polling raw spans every second.
- Every model call resolves its `pool_id` from `requests`, from the `bq_eval.pool_id` attribute (`STRING`), or from `(cloud.account.id, upstream_endpoint_location, model)`.
- The cost add-on uses these objects for `FOCUS 1.2` pool chargeback and the per-pool portion of the export to the `capacity` repository ([gold.md](gold.md#3-cost-add-on--capacity-export-contract)).

Without this extension, cost is computed from the token counts the application reports, and throttling is seen only where the application's telemetry reports it.
