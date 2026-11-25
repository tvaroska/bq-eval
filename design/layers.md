---
type: reference
up: "bq-eval.md"
down:
  - "silver.md"
  - "gold.md"
  - "clouds.md"
  - "capacity.md"
tags: [bq-eval, layers]
---

# Layers

> How Bronze, Silver, and Gold fit together, what runs where, and which way the dependencies point. Requirements: [spec/layers.md](../spec/layers.md).

```
Preventive Request-Path Port (Gateways / Proxies: <1m blocking, inline DLP, HITL, 1s/10s shedding)
        │  attested receipts & spans
        ▼
Agents & Systems ──▶ Bronze ───────────▶ Silver (BigQuery SQL) ────▶ Gold (Detective >=5m)
                     telemetry           [spans — internal base]     evaluation ──▶ GCS WORM / Optimizer Port
                     input tables (x3)   model_calls, tool_calls     monitoring
                                         events, trace_payloads      cost ────────▶ export contract (capacity)
                                         traces, outcomes,
                                         outcome_links
                     SCD-2 refs ────────▶ (app_registry, tools, skill_registry)
                     ▲
                     └── Two-Speed CI/CD Dual-Registry Ingestion Port (GitOps)

Local/CI Eval Runner Port (<5s CI_ONLY): translates the exact Silver SQL + deterministic Gold Rule 0 locally
```

## Bronze

Bronze is not built; it is what already exists. Spans in a trace store, referenced content in BigLake storage, and three VPC-SC-isolated copies of `workflow_events` (systems of record, platform controls, applications). Gateway and tool-proxy spans land in a trace store applications cannot write to, and that landing place is what makes their facts verified (`ADR-034`). Non-conforming frameworks, coding SDKs, and custom gateways normalize upstream through the **Upstream Bronze / OTel Collector Adapter Port** before landing.

## Silver

Silver is defined by **one logical BigQuery SQL contract**. A single-pass span router classifies up to three accepted telemetry shapes into internal `spans`, and the published views (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`, `outcome_links`) sit over Bronze and three SCD-2 reference tables (`app_registry`, `tools`, `skill_registry`) maintained through the **Two-Speed CI/CD Dual-Registry Ingestion Port**. Restricted objects and open twins live in **two physically separate datasets** (`silver_restricted`, where in the default profile each function keeps only the caller's business units, read from the entitlement list `unit_entitlements`, and row policies cover stored tables, `ADR-044`; and `silver_open`, supporting multi-project landing zones and split compaction SAs). The default (`light`) profile deploys each object as a table function of an explicit time window, with fixed-window wrapper views; the high-volume (`enterprise`) profile is design only, with a stored text-free span base under the same functions as the candidate (`ADR-035`). See [silver](silver.md).

Silver reads no prices and sets no pass.

## Gold & Adjacent Ports

- **Gold add-ons (detective `>= 5m`):** Each add-on (Evaluation, Monitoring, Cost) is separate and optional, reads only published Silver views, defines its own pass, enforces concrete slot/scan circuit breakers, and keeps daily stored results (with PII-free **GCS Bucket Retention-Lock + CMEK** WORM exports + RTBF crypto-shredding for regulated tiers). See [gold](gold.md).
- **Preventive Request-Path Enforcement Port:** Synchronous blocking (`< 1m` identity guards, inline RFC 8785/SoD/TTL, DLP/safety blocking, `allowed_models` rejection, `1s/10s` `429` shedding, `100 MB` streaming, daytime-PT blocking of `optimize`) executes in gateways and tool proxies — never BigQuery SQL.
- **Local/CI Eval Runner Port (`< 5s` `CI_ONLY`):** Executes the exact Silver SQL translator + deterministic Gold `Rule 0` locally over OTLP fixtures for DevAI PR merge gates and offline optimizer sweeps.

| Add-on | Reads | Adds |
|---|---|---|
| Evaluation | `traces`, `outcomes`, `outcome_links`, `model_calls`, `tool_calls`, `events`, `trace_payloads`, SCD-2 refs | Completed turns, deterministic policy/HITL/MNPI/fiduciary checks, session reconstruction, deterministic judges, stratified `SR 11-7` calibration, GCS WORM snapshots, Optimizer Port |
| Monitoring | `traces_open`, `outcomes_open`, `model_calls`, `tool_calls_open`, `events_open`, SCD-2 refs | Latency, TTFT, loops, dynamic tool churn, `thought_signature` & session rollups, throttling, SLAs, settled/skew checks |
| Cost | `traces_open`, `outcomes_open`, `outcome_links`, `model_calls`, `tool_calls_open`, SCD-2 refs | Per-attempt `1:N` pricing, 3-pool `FOCUS 1.2` + invoice reconciliation, `INFORMATION_SCHEMA.JOBS` warehouse compute, `CPVO`, `capacity` export |

## Dependency rule

Dependencies point one way: Gold reads published Silver views, open twins, and reference tables; Silver reads Bronze and reference tables. No add-on reads Bronze, raw `spans`, or another add-on's internals. Cross-repo tools consume versioned **Ports, Adapters and Contracts** only (`ADR-001`).

## Where Python and UDFs are allowed

Silver runs inside BigQuery SQL with zero external Python services; only a deterministic, payload-byte-guarded BigQuery UDF is allowed for fallback RFC 8785 (JCS) canonical hashing. Gold uses Python only where SQL cannot express the calculation (preferring in-BigQuery Python stored procedures). Deployment tooling, the Local/CI Eval Runner CLI, and the reference async BigLake Parquet upload hook run outside BigQuery.

## Extensions

The core is one location-aligned processing domain (defaulting to a `US` or `EU` multi-region Hub). Two modular extensions add to it without changing the core:

- **More than one cloud or region:** non-GCP clouds (S3/Iceberg Omni) and sovereign single-region GCP spokes run the same Silver contract and In-Cloud Judge Port locally, staging text-free open rows into the Hub via the scheduled Hub Transfer Port. See [clouds](clouds.md).
- **Capacity and governance:** custom-proxy `1:N` `requests` (`x-client-request-id`), `burst_metrics_1m`, SCD-2 `capacity_pools`/`capacity_allocations` (3-pool `FOCUS 1.2` + invoice reconciliation + pro-rata `order_id` splits), and extended `allowed_models`. See [capacity](capacity.md).
