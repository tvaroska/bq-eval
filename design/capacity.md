---
type: atom
up: "layers.md"
tags: [bq-eval, silver, capacity, extension]
---

# Extension: capacity and governance

> How gateway `requests`, dedicated capacity pools, and scoped model governance are built alongside [Silver](silver.md) without bloating the single-cloud core. Contract: [spec/capacity.md](../spec/capacity.md).

## `requests` view and `1:N` failover reconciliation

`requests` is a SQL view (or incremental partitioned table in `enterprise` mode) over gateway or model-server request logs, emitting one row per upstream attempt keyed by `(request_id / span_id, attempt_index)`.

- **Why `1:N` matters:** When a gateway hits HTTP `429` on Provisioned Throughput and fails over to Priority PayGo (or retries across endpoints), one agent `model_calls` span triggers `N` upstream `requests`.
- **No fan-out join:** Gold collapses `requests` to one summary row per `(request_id / span_id)` before joining `model_calls` — preserving total billed tokens across attempts, `attempt_count`, `throttled_attempt_count`, `final_serving_tier`, `upstream_endpoint_location`, `billing_project_id`, `ttft_ms`, `thought_signature_present`, and attested OBO identity where present.
- **3P serving-tier fallback:** When a third-party provider omits `TrafficType` response metadata, `requests` derives fulfillment tier deterministically from gateway admission headers and pool bindings, setting `tier_source = 'inferred'` vs. `'server'`.
- **Cache-write pricing tiers:** `requests` and `model_calls` preserve optional `5m` vs. `1h` ephemeral cache-write token splits alongside standard `gen_ai.usage.cache_write.input_tokens` so Gold prices multi-tier provider caches accurately.

## Scoped SCD-2 governance tables

`capacity_pools`, `capacity_allocations`, and `allowed_models` carry `(effective_from, effective_to)` validity windows (`NULL` end = active) joined on `call.start_time`:

- **`capacity_pools`:** Maps split commercial reservation orders (`order_id`) to their shared FCFS `runtime_pool_id` alongside `(cloud, scope_key, model, region, committed_units, expiry_date)`.
- **`capacity_allocations`:** Maps `(pool_id, consumer_app_or_unit)` to `funded_share` and `risk_tier` (`RL-1`..`RL-5`) for `FOCUS 1.2` multi-pool attribution.
- **`allowed_models`:** Scoped by `(provider, model, environment, business_unit)` with `is_allowed`, `zdr_allowed`, `lifecycle_tier` (`short_term_ga | long_term_ga | preview`), and `eol_date`. Silver flags policy violations on `model_calls`; Gold enforces `allowed_models` as a deterministic governance gate.

## Sub-minute burst physics vs. BigQuery rollups

Sub-minute weighted burndown (`1s`/`10s` for 1P pools, `30s`/`60s` for 3P pools) and `<1m` admission guards live at the gateway or OTel Collector metrics Port, which emits `1m` rollups to BigQuery rather than polling raw spans every second. Gold cost reconciles `1m`/hourly rollups with `FOCUS 1.2` billing exports and publishes the versioned payload consumed by the sibling `capacity` repository.
