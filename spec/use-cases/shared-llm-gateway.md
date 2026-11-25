---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 11: Shared Multi-Tenant LLM Gateway & Bulk Embeddings

> A central, zero-bypass multi-tenant LLM gateway mediating `100%` of model inference across hundreds of decentralized business applications (`RL-1`..`RL-5`) over 1P and 3P providers, combined with high-volume bulk `embeddings` indexing jobs — requiring custom-proxy `1:N` correlation (`x-client-request-id`), caller `trace_owner_app_id` precedence, Option A/B neutral `state_mode`, `100 MB` inline artifact streaming + inline DLP verdicts, 3-pool `FOCUS 1.2` spillover & Cloud Billing reconciliation, mixed `FIXED_TERM`/`AUTO_RENEW` `order_id` attribution inside merged FCFS PT pools, honest 3P `inferred_ambiguous` tiering, and `burst_metrics_1m` exports to `capacity`.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Govern, attribute, and right-size a centralized multi-tenant LLM gateway serving `200+` business applications across 1P and 3P models (both stateless `/v1/chat/completions` and stateful `/interactions` lanes) alongside `100M+`-call bulk `embeddings` vector-indexing bursts. |
| **Topology & Option A/B neutrality** | `100%` of AWS, on-prem, and GCP inference traverses the central gateway for RBAC, OBO identity, DLP, ZDR (`store = false`), and `~2 ms` quota admission. Supports both **Option A** (`state_mode = 'stateful_cmek_platform'`, thin stateless proxy + platform session/MCP state) and **Option B** (`state_mode = 'stateful_cmek_gateway'`, dual-lane stateful proxy) identically via neutral **Ports, Adapters and Contracts**. |
| **Capacity & noisy-neighbor physics** | Dozens of commercial PT orders (`FIXED_TERM` expiring cohorts + `AUTO_RENEW` monthly orders) merge into shared FCFS `runtime_pool_id` buckets governed by sub-minute weighted burndown (`1s`/`10s` for 1P; `30s`/`60s` for 3P; `1x/0.1x/4x <= 200K` vs. `2x/0.2x/7.5x > 200K` keyed on `input + cache_read`), overflowing from PT (`RL-1`/`RL-2`) -> `1.8x` Priority PayGo -> Sheddable Standard/Flex PayGo (`RL-4`/`RL-5`). |
| **3P asymmetry & bulk embeddings** | Third-party providers may omit `TrafficType` response headers (`emits_traffic_type = false`) and Priority PayGo (`supports_priority_paygo = false`), while bulk `embeddings` backfills can triple monthly call volume and saturate shared pools if unattributed. |
| **Separation of duties (`ADR-001`)** | Gateway owns `~2 ms` live `429` shedding and `100 MB` artifact streaming; `bq-eval` owns observational chargeback, reconciliation, and `burst_metrics_1m` exports; sibling `capacity` owns Newsvendor sizing and shedding policy compilation. |

## Telemetry & Data Expectations

- **Custom Proxy Bronze Contract**: Gateway emits metadata-only `requests` logs (`_AllLogs` `inference_request` on GCP, S3 Parquet/Iceberg on AWS) carrying mandatory **`x-client-request-id` + `(trace_id, span_id)`**, `traceparent`, caller/desk **`trace_owner_app_id`** (overriding proxy root service name), `attempt_index`, `upstream_endpoint_location`, `state_mode`, `thought_signature_present`, `traffic_type_attested`, billed tokens (`5m`/`1h` cache splits), weighted burndown units, `serving_tier`, `tier_source` (`server | inferred | inferred_ambiguous`), and write-IAM-attested OBO claims.
- **Inline artifact & safety port**: Gateway streams `100 MB` attachments upstream via pre-signed private URLs (emitting `document_bytes`, `page_count`, `attachment_sha256`) and emits structured inline DLP/safety verdicts (`guardrail.intervention` with `control_id`, `layer`, `action`). Zero-retention traffic is kept in the text-free objects and is not scored.
- **Sub-minute burst rollups & pool tables**: Gateway/collector emits `burst_metrics_1m` rollups into BigQuery alongside SCD-2 `capacity_pools` (`order_id -> runtime_pool_id`, `renewal_type`, `expiry_date`, `unit_rate`, `min_cache_tokens`, `emits_traffic_type`), `capacity_allocations`, and `allowed_models`.

## Required Checks, Alerts & Views

1. **Per-attempt `1:N` failover pricing & retry-storm separation**
   - Join `requests` to `model_calls` on `x-client-request-id` + `(trace_id, span_id)` (never breaking on missing `gen_ai.response.id` or failed `429` legs), price every attempt individually first (`>200K` on `input + cache_read`), and separate healthy `gateway_failover_attempts` from illegal caller SDK `caller_retry_count` (`attempts > 1`) retry amplification.
2. **3-pool `FOCUS 1.2` spillover, pro-rata `order_id` & Cloud Billing invoice reconciliation**
   - Compute active-hours funded PT share, off-peak backfill credit, unallocated idle PT, `1.8x` Priority PayGo spillover, and Standard/Flex PayGo; attribute merged FCFS `runtime_pool_id` consumption pro-rata across `FIXED_TERM` expiring orders vs. `AUTO_RENEW` orders; flag 3P calls lacking `TrafficType` honestly as `tier_source = 'inferred_ambiguous'`; reconcile modeled spend against Cloud Billing invoice/net-realized exports (`workload_id = NULL` rows included).
3. **Prefix-cache physics & dynamic tool-churn monitoring**
   - Track `thought_signature_present`, `upstream_endpoint_location` (regional vs. global cache collapse), `min_cache_tokens` floor breaches, and `tool_definitions_sha256` churn per desk.
4. **Bulk `embeddings` pool saturation & daytime-PT eval isolation**
   - Attribute `embeddings` calls (`operation_name = 'embeddings'`) to their `capacity_pools` alongside LLM inference, flag `agent_gym_eval`/`optimize` hitting daytime `RL-1`/`RL-2` PT pools, and export hourly + `burst_metrics_1m` + `RL-1`..`RL-5` payloads to `capacity`.
