---
type: reference
related:
  - "silver.md"
  - "open-questions.md"
  - "../design/layers.md"
tags: [bq-eval, layers]
---

# Layers: Bronze, Silver, Gold

> What each layer must deliver and where the line between them runs. Silver is specified in [silver.md](silver.md); this note fixes the frame around it.

| Layer | What it is | Must deliver |
|---|---|---|
| **Bronze** | Telemetry where it already lands | Traces, logs and gateway request records stay in place. Nothing is copied to make them readable. |
| **Silver** | The core | One set of objects with the same shape for every source and every cloud, derived from telemetry and shared reference data. |
| **Gold** | Add-ons | Evaluation, monitoring and cost. Each add-on reads Silver and nothing below it. |

## Rules

1. **The core is Bronze plus Silver.** Everything else is an add-on that can be left out.
2. **The core lives in BigQuery.** Silver is SQL that runs inside BigQuery. No Python is needed to produce it.
3. **Applications on top use Python only where SQL cannot do the job.** Python procedures that run inside BigQuery are acceptable.
4. **Silver holds facts, not judgements.** It carries no prices and no judge results. Both belong to Gold.
5. **Gold reads the published Silver objects only.** There is one exception, for cost; see [silver.md](silver.md#who-may-read-what).
6. **Other repositories do not name this repository's views.**

## What Silver may use

Silver is built from two things:

- **Telemetry:** spans, logs, agent events and gateway request records, in one of the three accepted [shapes](shapes.md), including content that a step refers to in storage.
- **Shared reference data:** the application list, the dedicated capacity that was bought, and the models that are allowed.

The price list is not part of Silver.
