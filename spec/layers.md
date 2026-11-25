---
type: reference
related:
  - "silver.md"
  - "gold.md"
  - "features.md"
  - "clouds.md"
  - "capacity.md"
  - "open-questions.md"
  - "../design/layers.md"
tags: [bq-eval, layers]
---

# Layers: Bronze, Silver, Gold

> What each layer must deliver and where the line between them runs. Silver is specified in [silver.md](silver.md), Gold in [gold.md](gold.md), and the end-to-end capability matrix in [features.md](features.md).

| Layer | What it is | Must deliver |
|---|---|---|
| **Bronze** | Telemetry where it already lands | Traces, referenced content in storage, and the input table for records that are not spans stay in place. Nothing is copied to make them readable. |
| **Silver** | The core | One set of SQL views with the same columns for every accepted telemetry shape, derived from telemetry and shared SCD-2 reference data. Counts and facts only. |
| **Gold** | Add-ons | Evaluation, monitoring, and cost. Each add-on reads the published Silver views and nothing below them, and defines its own pass. |

## Rules

1. **The core is Bronze plus Silver.** Everything else is an add-on or an extension that can be left out.
2. **Silver is defined as one logical BigQuery SQL contract.** The default (`light`) deployment publishes each object as a table function of an explicit time window over Bronze ([silver.md](silver.md#read-windows)), with no stored data and zero scheduled jobs. A high-volume or long-retention (`enterprise`) profile is design only: the candidate stores a typed, text-free copy of spans and runs the same functions on top (`ADR-012`, `ADR-035`). Silver needs no Python outside an optional BigQuery UDF for RFC 8785 (JCS) hashing when hashes are not emitted at execution time.
3. **Detective warehouse audit vs. Preventive Request-Path Enforcement Port:** BQ Eval runs asynchronously on a schedule of minutes (`>= 5m`) and never sits on the live request path. Synchronous blocking controls — inline RFC 8785 approval/SoD/TTL enforcement, DLP/safety blocking, `allowed_models` admission, `< 1m` missing-identity guards, `1s/10s` `429` shedding, and daytime-PT rejection of `optimize`/`eval` traffic — live in a **Preventive Request-Path Enforcement Port** owned by gateways and tool proxies, which emit receipts into a landing place applications cannot write to, audited post-hoc by BQ Eval (`ADR-032`, `ADR-034`).
4. **Local/CI Eval Runner Port (`CI_ONLY`):** Sub-5-second pre-merge PR gates and offline optimizer sweeps do not poll BigQuery. BQ Eval exposes its offline SQL translator as a first-class **Local/CI Eval Runner Port** running Silver SQL + deterministic Gold `Rule 0` and composite checks locally over captured OTLP traces (`CI_ONLY`, strictly separated from SME-calibrated production scores; `ADR-032`).
5. **Applications on top use Python only where SQL cannot do the job.** Python procedures that run inside BigQuery are acceptable.
6. **Silver holds counts and facts, not judgements.** It carries no prices, no judge results, no pass, and no threshold. All of those belong to Gold.
7. **Gold reads the published Silver views only.** No add-on reads raw `spans` or anything below the published Silver views; see [silver.md](silver.md#who-may-read-what).
8. **Other repositories do not name this repository's views.** Cross-repo integrations use versioned **Ports, Adapters and Contracts** (`capacity` burndown export, Optimizer Gold Export Port, CI/CD Dual-Registry Ingestion Port).
9. **The core contract is one processing domain (location-aligned).** Multi-region (`US` or `EU`) is the default hub topology; more than one cloud or sovereign single-region spoke ([clouds.md](clouds.md)) and capacity and governance ([capacity.md](capacity.md)) are modular extensions with their own specifications and design notes. Text stays in the cloud and perimeter where it landed in every case.

## What Silver may use

Silver is built from two things:

- **Telemetry & workflow records:** trace spans in one of the three accepted [shapes](shapes.md) (including content that a step refers to in storage), the input table for records that are not spans (approvals, tickets, mail, reviews, committed writes, MRM releases, SDLC/CI events), and OTel feedback/guardrail events. Every shape carries content, and a record's writer class (system of record, platform control, application) is set by where it landed (`ADR-034`, `ADR-036`). Non-span records reach Gold through the `events` view.
- **Shared reference data:** SCD-2 `app_registry` (telemetry identities, owner, business unit, environment, `risk_tier`, `payload_profile`, `sampling_mode`, `barrier_domain`, optional MRM governance), `tools` (factual `writes_state`, `modifies_workspace`, `executes_code`, `sends_external_reply`, `returns_pii`, `creates_ticket` flags), and `skill_registry` (authoritative `skill_sha256` and `protected_block_sha256` compliance pins), maintained through a Two-Speed **CI/CD Dual-Registry Ingestion Port**.

The price list is not part of Silver. Neither are latency targets, approval thresholds, or any other limit a result is compared with.
