---
type: atom
up: "layers.md"
tags: [bq-eval, silver, clouds, extension]
---

# Extension: more than one cloud or region

> Multi-cloud and multi-region execution routes by residency tier and Hub location ([specs/clouds.md](../specs/clouds.md)). The core in [silver](silver.md) describes one location-aligned processing domain and does not depend on this note.

## Hub Location & Why BigQuery Omni Is Replaced (`ADR-031`, `ADR-046`)

- **Default `US`/`EU` Multi-Region Hub vs. Regional Lakehouse Hub:** The Hub defaults to **`US` or `EU` multi-region** for zero-copy `light` views over multi-region `_AllSpans`/`_AllLogs`, direct `FOCUS 1.2` Cloud Billing joins, multi-region HA, and broad `AI.GENERATE_TABLE` judge availability. Because **Lakehouse Runtime Catalogs** (`Project.Catalog.Namespace.Table`) and **Cross-Cloud Interconnect** Service Directory endpoints are **regional** resources, deployments prioritizing zero-copy federated Iceberg joins over private interconnect either align the Hub to a single region (`us-east4`, `europe-west3`) or bridge regional spokes into `US`/`EU` via Mode 3.
- **Strict Per-Domain Location & CMEK Alignment:** Within every processing domain, Bronze stores, Shape-3 buckets, BigLake / Cross-Cloud Connections, federated catalogs, `silver_open`, `silver_restricted`, `workflow_events`, and **Cloud KMS CMEK key rings** share one exact BigQuery location (never mixing `US` with `us-east4`).
- **Why Legacy BigQuery Omni Is Retired:** Standard BigQuery Omni connections cannot execute User-Defined Functions (breaking `ADR-041` RFC 8785 JCS SQL hashing), cannot live-`JOIN` Hub reference tables, lack `AI.GENERATE_TABLE`, and require separate Omni slot reservations.

## Three Execution Modes by Residency & Location (`ADR-046`)

1. **Mode 1 — Direct Cross-Cloud Bronze (`RL-4`–`RL-5` permissive residency):**
   - Compacted **Parquet** or **Apache Iceberg V2+** (no V1, no Iceberg views, no tiny unbatched files) in AWS S3 / Azure ADLS is queried directly on standard GCP slots through a Hub-located **BigQuery Cross-Cloud Connection** or **Borderless Lakehouse Federated Catalog** (`P.C.N.T`).
   - Runs `bq-eval`'s single BigQuery Silver SQL (`ADR-035` table functions, `ADR-041` JCS UDFs, SCD-2 joins) + `AI.GENERATE_TABLE` / `AI.EVALUATE_AGENT` (Preview) in GCP. Because federated/cross-cloud tables disallow BigQuery Row Access Policies and column data policies, `silver_restricted` functions filter rows via `unit_entitlements` (`ADR-044`).
2. **Mode 2 — Zero-Copy Federated `*_open` Spokes (`RL-1`–`RL-3` strict residency on regional CCI Hub):**
   - Raw text and raw IDs stay in AWS/Azure. Spoke Apache Spark / Trino / Snowflake jobs (validated against `M18-1` row fixtures and `ADR-041` JCS hash vectors) write leak-free `*_open` **Iceberg V2** tables in S3/ADLS alongside **In-Cloud Judge Port** text-free verdicts.
   - A regionally aligned GCP Hub federates *only* those `*_open` Iceberg V2 tables over **Cross-Cloud Interconnect** (AWS-supported; Azure private routing gated on live verification). Hub Gold materializes each settled window **once** into native CMEK GCP tables, avoiding recurring egress even when `constraints/gcp.restrictNonCmekServices` disables non-CMEK **Intelligent Caching** (`256 KiB` local SSD blocks, off by default).
3. **Mode 3 — Staged `Hub Transfer Port` (`US`/`EU` Hub bridges, Azure private gaps & GCP sovereign spokes):**
   - Copies text-free `*_open` rows over private interconnect / Storage Transfer Service -> Hub GCS -> BigQuery `LOAD DATA` into native CMEK `US`/`EU` tables (`(spoke_id, transfer_watermark_utc, batch_id)` idempotent `MERGE`), also serving sovereign single-region GCP spokes (`europe-west2`, `asia-*`).

## What May Cross vs. Stay In-Perimeter (`RL-1`–`RL-3`)

| Data | Mode 2 (Federated `*_open`) & Mode 3 (`Hub Transfer Port`) |
|---|---|
| `model_calls`, `outcomes_open`, `outcome_links` (no text, no `outcome.subject`) | yes |
| `traces_open`, `tool_calls_open`, `events_open` (no text; per-BU KMS-HMAC `user_id_hash` / `tenant_id_hash` + `hmac_key_version`) | yes |
| `requests` and `burst_metrics_1m`, with the capacity extension | yes |
| Text-free Gold judge verdict rows (`judge_cloud`, `judge_model_snapshot_id`) | yes |
| `spans`, `trace_payloads`, `traces`, `tool_calls`, `events`, restricted `outcomes` (`outcome.subject`, text, args, hashes, raw IDs) | **no** (Mode 1 permitted only for `RL-4`–`RL-5` when policy allows GCP block reads) |

## Operational Guards, 4-Part `P.C.N.T` SQL, KMS-HMAC & Non-GCP Judge Port

- **Skew watermarks (`remote_fragment_pending = true`):** Enforced in both staged (`transfer_watermark_utc`) and federated (`--refresh-interval >= 300s`) modes so Hub Gold never flags in-flight split traces as orphaned while remote catalog metadata lags GCP `_AllSpans`.
- **`0`-Byte Dry-Run & Egress Telemetry:** Federated/Lakehouse `dry_run` calls return a `0`-byte lower bound; cost guards rely on static window predicates (`ADR-035`), `max_bytes_billed`, and post-job `INFORMATION_SCHEMA.JOBS_BY_PROJECT` (`total_bytes_billed` + `object_storage_stats` `object_storage_bytes_read` / `cache_bytes_read`).
- **4-Part `P.C.N.T` SQL & Read-Only Catalogs:** `bq_eval/deploy/engine/bootstrap.py` and the offline SQLite AST translator rewrite 4-part `project.catalog.namespace.table` references alongside 3-part names; writable Gold/event tables stay in native GCP BigQuery datasets.
- **Cross-Cloud KMS-HMAC, In-Cloud Judge Port & On-Premises Relay:** Out-of-band per-BU KMS-HMAC (`hmac_key_version`) + in-perimeter RTBF DEKs; in-VPC Bedrock / Azure OpenAI container runner (`temperature = 0`, `seed`, snapshot ID, `zdr_allowed`, per-cloud $Se/Sp$) for `RL-1`–`RL-3`; mTLS payload-stripping OTel Collector relay for on-prem. See [open questions](../specs/open-questions.md#clouds-extension).
