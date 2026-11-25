---
type: atom
related:
  - "layers.md"
  - "silver.md"
  - "capacity.md"
  - "open-questions.md"
  - "../design/clouds.md"
tags: [bq-eval, silver, clouds, extension]
---

# Extension: more than one cloud or region

> What is added when telemetry lands across multiple clouds, multiple BigQuery regions, or on-premises environments. The core contract in [silver.md](silver.md) describes one location-aligned processing domain and does not depend on this note.

## Hub Topology & Location-Alignment Invariant (`ADR-031`, `ADR-046`)

- **Multi-region (`US` or `EU`) default Hub, with regional Hub option for zero-copy federation:** Because `bq-eval` is an asynchronous (`>= 5m`) audit, evaluation, and FinOps warehouse plane — never on the hot inference path — defaulting the Hub to **`US` or `EU` multi-region** enables zero-copy `light` views over multi-region `_AllSpans`/`_AllLogs`, direct `FOCUS 1.2` Cloud Billing joins (with automatic prior-month backfill), multi-region HA, and broad `AI.GENERATE_TABLE` judge availability. Deployments whose primary goal is zero-copy cross-cloud Apache Iceberg REST catalog federation over regional **Cross-Cloud Interconnect** may instead pin the entire Hub domain to that single region (such as `us-east4` or `europe-west3`).
- **Strict Location-Alignment Invariant (never mix locations in one query domain):** Within any single processing domain (Hub or Spoke), Bronze tables, Shape-3 BigLake buckets, BigLake / Cross-Cloud Connections, federated Lakehouse catalogs, `silver_open`, `silver_restricted`, `workflow_events`, and **Cloud KMS CMEK key rings** must share the **exact same BigQuery location** (all `US`, all `EU`, or all one single region like `us-east4` — never `US` mixed with `us-east4`). Neither regional KMS keys nor regional **Lakehouse Runtime Catalogs** (`Project.Catalog.Namespace.Table`) can join directly with a `US` or `EU` multi-region dataset.
- **Why standard BigQuery Omni cannot compute Silver (`ADR-041`, `ADR-046`):** Standard BigQuery Omni connections execute in remote cloud clusters that lack User-Defined Functions (breaking `ADR-041`'s mandatory RFC 8785 JCS SQL hashing on `workflow_events.action_args` and inline `tool_calls`), cannot live-`JOIN` GCP reference tables, and cannot invoke `AI.GENERATE_TABLE`. `bq-eval` therefore replaces legacy Omni spoke views with the three residency- and location-routed adapter modes below.

## Tiered Multi-Cloud & Multi-Region Adapter Modes (`ADR-046`)

1. **Mode 1 — Direct Cross-Cloud Bronze (`RL-4`–`RL-5` permissive residency; GCP block reads permitted):**
   - Remote OTel spans and Shape-3 payloads land as micro-batched **Parquet** or **Apache Iceberg V2+** (Iceberg V1 and Iceberg views unsupported) in AWS S3 or Azure ADLS with static Hive/partition predicates.
   - A **BigQuery Cross-Cloud Connection** or **Borderless Lakehouse Federated Catalog** (`Project.Catalog.Namespace.Table` 4-part identifier) in the Hub's exact GCP location reads remote blocks onto standard GCP slots, executing `bq-eval`'s single BigQuery Silver SQL (`ADR-010`, including table functions `ADR-035` and RFC 8785 JCS UDFs `ADR-041`) plus `AI.GENERATE_TABLE` and `AI.EVALUATE_AGENT` (Preview) with zero spoke SQL compute and zero transfer ETL.
   - Because federated catalogs and Cross-Cloud Connections do not support BigQuery Row Access Policies or column data policies, per-business-unit access separation on restricted reads MUST run inside Silver table functions via `unit_entitlements` (`ADR-044`).
2. **Mode 2 — Zero-Copy Federated `*_open` Spokes (`RL-1`–`RL-3` strict residency on a regionally aligned Hub + Cross-Cloud Interconnect):**
   - Raw text, tool arguments, `outcome.subject`, and raw IDs stay strictly inside AWS/Azure (`ADR-017`). An in-cloud spoke engine (Apache Spark / Trino / Snowflake passing `M18-1` row fixtures and `ADR-041` shared JCS hash vectors) compacts leak-free `*_open` **Apache Iceberg V2** tables (`model_calls`, `traces_open`, `tool_calls_open`, `events_open`, `outcomes_open`, `outcome_links`, `requests`, and text-free **In-Cloud Judge Port** verdicts) in S3/ADLS.
   - A regionally aligned GCP Hub (`us-east4`, `europe-west3`) federates **only** those text-free `*_open` Iceberg V2 tables via **Borderless Lakehouse** over **Cross-Cloud Interconnect** (AWS-supported; Azure private transport requires live verification before production use).
   - Hub Gold reads each settled window **once** ([gold.md](gold.md#shared-rules)) into native CMEK-protected GCP Gold tables, bounding uncached remote egress even when `constraints/gcp.restrictNonCmekServices` disables non-CMEK **Intelligent Caching** (`256 KiB` local SSD cache, off by default).
3. **Mode 3 — Staged `Hub Transfer Port` (`US`/`EU` multi-region Hubs bridging regional CCI spokes, Azure private perimeters without CCI, and sovereign single-region GCP spokes such as `europe-west2` or `asia-*`):**
   - A scheduled **Hub Transfer Port** copies text-free `*_open` rows and text-free judge verdicts over private interconnect / Storage Transfer Service -> Hub GCS -> BigQuery `LOAD DATA` into native CMEK-protected Hub tables (`(spoke_id, transfer_watermark_utc, batch_id)` idempotent `MERGE`), preserving full `US`/`EU` multi-region joins, native BigQuery Row Access Policies, clustering, and accurate pre-flight `dry_run` byte bounds.

## Shared Multi-Cloud Ports, Adapters and Contracts

- **Skew watermarks (`remote_fragment_pending = true`) across both staged and federated modes:** Staged batches stamp `transfer_watermark_utc`, and federated Lakehouse catalogs synchronize metadata at `--refresh-interval >= 300s` (skipping overrun cycles) while GCP `_AllSpans` arrives in `~14–22s`. Hub Gold compares spoke watermark / catalog snapshot freshness against trace `start_time` and flags cross-cloud split traces awaiting remote arrival as **`remote_fragment_pending = true`** (never misclassifying in-flight fragments as orphaned).
- **Dry-run `0`-byte blindspot & post-execution cost breakers (Modes 1 & 2):** Because `bq query --dry_run` on federated Cross-Cloud Connections and Lakehouse Runtime Catalog tables may report a **`0`-byte** lower bound, pre-flight dry-run estimators cannot price federated scans; Modes 1 and 2 enforce static partition windows (`ADR-035`), `max_bytes_billed` ceilings, and post-execution `INFORMATION_SCHEMA.JOBS_BY_PROJECT` (`total_bytes_billed` plus `object_storage_stats` `object_storage_bytes_read` vs. `cache_bytes_read`) circuit breakers.
- **4-part `Project.Catalog.Namespace.Table` SQL & offline runner parity:** Deployment templating and the `< 5s` Local/CI SQLite translator (`ADR-032`) normalize both 3-part BigQuery names and 4-part `P.C.N.T` Lakehouse table identifiers. Federated catalogs are read-only from BigQuery; `eval_results`, `sme_labels`, and `workflow_events` always live in native GCP datasets.
- **Cross-cloud KMS-HMAC & RTBF key custody:** `traces_open`, `tool_calls_open`, and `events_open` carry `user_id_hash` and `tenant_id_hash` computed via an out-of-band **KMS HMAC Port** using shared key material imported across KMS boundaries (`AWS KMS` <-> `GCP Cloud KMS`) scoped per business unit with `hmac_key_version`, keeping raw IDs and per-subject RTBF crypto-shredding DEKs in-perimeter.
- **Raw text Residency, Split-Trace Correlation, In-Cloud Judge Port & On-Premises Relay:**
  - *Residency (`RL-1`–`RL-3`):* `spans`, `trace_payloads`, `traces`, `tool_calls`, `events`, and restricted `outcomes` (`outcome.subject`) never leave their home perimeter.
  - *Split-trace correlation:* Spokes publish `has_root_span = true` on the owning spoke (`false` on remote fragments), correlated in the Hub on `(trace_id, parent_span_id)` (or `trace_id` across runtime hops).
  - *In-Cloud Judge Port (`RL-1`–`RL-3` non-GCP spokes):* Runs deterministic LLM judges and PII scans inside an **in-VPC compute runtime** calling native AWS Bedrock / Azure OpenAI (`temperature = 0`, `seed`, immutable snapshot ID, `allowed_models.zdr_allowed`), exporting only text-free verdicts (`judge_cloud`, `judge_model_snapshot_id`) calibrated with per-cloud $Se/Sp$ in Gold.
  - *On-Premises Telemetry Port (`On-Prem -> Transit Cloud -> Hub`):* Forwards OTel spans through an mTLS, egress-filtered OTel Collector relay over private interconnect stripping forbidden payloads per `payload_profile` before landing in the nearest approved cloud Bronze store.

Remaining live empirical benchmarks are tracked in [open-questions.md](open-questions.md#clouds-extension).
