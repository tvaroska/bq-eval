---
type: atom
up: "layers.md"
tags: [bq-eval, silver, clouds, extension]
---

# Extension: more than one cloud

> Data stays in the cloud where it landed, and Silver is computed next to it. This note says what that means for the objects and for what may cross between clouds. Contract: [spec/clouds.md](../spec/clouds.md). The core in [silver](silver.md) is one cloud and does not depend on this note.

## One Silver per cloud and the Hub Transfer Port

Every cloud holding telemetry runs the same logical Silver contract over its local Bronze store (views in `light` mode; incremental partitioned tables in `enterprise` mode). SCD-2 reference tables replicate into each cloud.

Because BigQuery cannot live-`JOIN` non-GCP BigQuery Omni views (such as `aws-us-east-1`) directly with GCP regional datasets (`US`/`us-east4`) in a single query, a scheduled **Hub Transfer Port** stages text-free open rows and judge verdicts from each spoke into a hub staging dataset in the hub's region.

## What may cross (via Hub Transfer Port)

| Data | May cross to the hub |
|---|---|
| `model_calls` rows (no text) | yes |
| `outcomes` rows | yes |
| `traces_open` rows (no text; KMS-HMAC `user_id_hash` / `tenant_id_hash`) | yes |
| `tool_calls_open` rows (status, duration, flags, execution-time `args_sha256`, `idempotency_key`, `user_id_hash` — zero text, zero payload bucket scans) | yes |
| `events_open` rows (event type, timestamps, `source_system`, `is_authoritative`, `approved_args_sha256`, `user_id_hash` — zero free-text payloads) | yes |
| `requests` rows, with the capacity extension | yes |
| Text-free Gold judge verdict rows | yes |
| `spans` (internal base) | no |
| `trace_payloads`, `traces`, `tool_calls`, `events` (text, arguments, results, payloads, raw `user_id` / `tenant_id`) | no |

`user_id_hash` and `tenant_id_hash` are computed via an out-of-band **KMS HMAC** Port (never a static salt in SQL view DDL) so cross-cloud user rollups stay consistent without allowing directory reversal by view readers.

## A trace in two clouds (hub correlation)

A trace can start in one cloud (or on-premises) and call a model or tool through a gateway in another cloud. Each cloud sees only its local spans:

- The cloud holding the root span publishes `has_root_span = true` from its local spans.
- The remote cloud publishes its spans flagged as a cross-cloud fragment (`has_root_span = false`).
- **Hub text-free correlation:** After the Hub Transfer Port stages open rows from each cloud into the hub region, hub views correlate split-trace fragments on `(trace_id, parent_span_id)` without moving text — so remote `429`s, errors, `requests`, and tool-write alerts are reconciled with their owning trace instead of being silently dropped.

## In-Cloud Judge Port

Text never leaves its home cloud. Judges and leak scans run inside an **In-Cloud Judge Port** backed by that cloud's native model endpoint (never cross-cloud `AI.GENERATE_TABLE` routing non-GCP text to GCP), exporting only text-free verdict rows to the Hub Transfer Port. See [open questions](../spec/open-questions.md#clouds-extension).
