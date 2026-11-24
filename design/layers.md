---
type: reference
up: "bq-eval.md"
down:
  - "silver.md"
  - "clouds.md"
tags: [bq-eval, layers]
---

# Layers

> How Bronze, Silver and Gold fit together, what runs where, and which way the dependencies point. Requirements: [spec/layers.md](../spec/layers.md).

```
Agents ──▶ Bronze ───────────▶ Silver ─────────────────▶ Gold
           telemetry           spans                     evaluation
           in place            requests                  monitoring
                               trace_payloads            cost
                               traces
                               outcomes
           reference tables ──▶
```

## Bronze

Bronze is not built; it is what already exists. Spans from a trace store, logs, agent event tables, span files in another cloud's object storage, and gateway request records. BQ Eval reads them where they are.

## Silver

Silver is SQL inside BigQuery. One adapter per source brings spans into a common shape. From there, per-trace and per-outcome tables are built by a scheduled SQL job. See [silver](silver.md).

Silver also reads three pieces of shared reference data: the application list, the dedicated capacity that was bought, and the allowed models. It reads no prices.

## Gold

Each add-on is separate and optional.

| Add-on | Reads | Adds |
|---|---|---|
| Evaluation | `traces`, `trace_payloads`, `outcomes` | Judge results, calibration, the meaning of "verified" |
| Monitoring | `traces`, `requests` | Latency, loops, throttling over time |
| Cost | `traces`, `outcomes`, `requests`, and `spans` with a date range | Prices, cost per trace and per outcome, capacity use |

## Dependency rule

Dependencies point one way: Gold reads Silver, Silver reads Bronze and the reference tables. An add-on does not read Bronze, does not read another add-on's internals, and does not reach into the views Silver is built from. The cost add-on's access to `spans` is the single exception.

## Where Python is allowed

Nowhere in the core. Above it, an application uses Python only where SQL cannot do the job, and a Python procedure that runs inside BigQuery is preferred to a process outside it. Deployment tooling is outside this rule.

## More than one cloud

Each cloud has its own Silver, built by the same SQL next to the data. See [clouds](clouds.md).
