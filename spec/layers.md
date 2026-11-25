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
| **Silver** | The core | One set of SQL views with the same columns for every accepted telemetry shape, derived from telemetry and shared reference data. Counts and facts only. |
| **Gold** | Add-ons | Evaluation, monitoring and cost. Each add-on reads the published Silver views and nothing below them, and defines its own pass. |

## Rules

1. **The core is Bronze plus Silver.** Everything else is an add-on or an extension that can be left out.
2. **Silver is defined as one logical BigQuery SQL contract.** The default (`light`) deployment is views-only over Bronze with read horizons ([silver.md](silver.md#read-horizons)) and zero scheduled jobs. High-volume or long-retention (`enterprise`) deployments may back the exact same published Silver view names with watermarked incremental partitioned tables in an isolated slot reservation (`ADR-012`, `ADR-026`). Silver needs no Python outside an optional BigQuery UDF for RFC 8785 (JCS) hashing when hashes are not emitted at execution time.
3. **Applications on top use Python only where SQL cannot do the job.** Python procedures that run inside BigQuery are acceptable.
4. **Silver holds counts and facts, not judgements.** It carries no prices, no judge results, no pass and no threshold. All of those belong to Gold.
5. **Gold reads the published Silver views only.** No add-on reads raw `spans` or anything below the published Silver views; see [silver.md](silver.md#who-may-read-what).
6. **Other repositories do not name this repository's views.**
7. **The core contract is one cloud.** More than one cloud ([clouds.md](clouds.md)) and capacity and governance ([capacity.md](capacity.md)) are modular extensions with their own specifications and design notes. Text stays in the cloud where it landed in every case.

## What Silver may use

Silver is built from two things:

- **Telemetry & workflow records:** trace spans in one of the three accepted [shapes](shapes.md) (including content that a step refers to in storage), the input table for records that are not spans (approvals, tickets, mail, reviews, committed writes), and OTel feedback events. The last two reach Gold through the `events` view.
- **Shared reference data:** `app_registry` (owner, business unit, environment) and `tools` (factual `writes_state`, `executes_code`, `sends_external_reply`, `returns_pii`, `creates_ticket` flags).

The price list is not part of Silver. Neither are latency targets, approval thresholds or any other limit a result is compared with.
