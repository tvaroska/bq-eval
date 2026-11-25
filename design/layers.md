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

> How Bronze, Silver and Gold fit together, what runs where, and which way the dependencies point. Requirements: [spec/layers.md](../spec/layers.md).

```
Agents & Systems ──▶ Bronze ───────────▶ Silver (views) ──────────▶ Gold
                     telemetry           [spans — internal base]    evaluation
                     input table         model_calls                monitoring
                                         tool_calls                 cost ──▶ export contract (capacity)
                                         events
                                         trace_payloads
                                         traces
                                         outcomes
                     reference tables ──▶ (app_registry, tools)
```

## Bronze

Bronze is not built; it is what already exists. Spans in a trace store, referenced content in storage, and the input table for records that are not spans (approvals, tickets, mail, reviews, committed writes). BQ Eval reads them where they are.

## Silver

Silver is defined by **one logical BigQuery SQL contract**. A single-pass span router classifies up to three accepted telemetry shapes into the internal `spans` base without double-scanning the trace store, and six Gold-facing objects (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`) sit on top of Bronze and two SCD-2-capable reference tables (`app_registry`, `tools`). Restricted views and open twins live in **two physically separate datasets** (`silver_restricted` and `silver_open`). The default (`light`) profile deploys pure SQL views per read horizon; high-volume (`enterprise`) deployments back the exact same view names with watermarked incremental partitioned tables in an isolated slot reservation (`ADR-026`). See [silver](silver.md).

Silver reads no prices and sets no pass.

## Gold

Each add-on is separate and optional, reads the published Silver views, defines its own pass, and keeps its own results from day to day. For SLO-keeping model, routing, and capacity recommendations, Gold exports a versioned burndown and latency payload consumed by the sibling `capacity` repository rather than exposing Silver views externally. See [gold](gold.md).

| Add-on | Reads | Adds |
|---|---|---|
| Evaluation | `traces`, `outcomes`, `model_calls`, `tool_calls`, `events`, `trace_payloads`, reference tables | Completed turns, deterministic policy & HITL threshold checks, judge results, blind SME calibration, the meaning of "verified" |
| Monitoring | `traces`, `outcomes`, `model_calls`, `tool_calls`, `events`, reference tables | Latency, first-token time, loops, throttling, SLAs, settled traces, checks on a schedule |
| Cost | `traces`, `outcomes`, `model_calls`, `tool_calls`, reference tables | Prices, cost per trace/user/outcome, `capacity` export payload |

## Dependency rule

Dependencies point one way: Gold reads the six published Silver views, their open twins and the reference tables; Silver reads Bronze and the reference tables. An add-on does not read Bronze, does not read raw `spans` directly, does not read another add-on's internals, and does not reach into the shape adapters Silver is built from.

## Where Python is allowed

Nowhere in the core. Above it, an application uses Python only where SQL cannot do the job, and a Python procedure that runs inside BigQuery is preferred to a process outside it. Deployment tooling is outside this rule, and so is the reference upload hook, which runs inside the application.

## Extensions

The core is one cloud, six views and two reference tables. Two extensions add to it without changing it.

- **More than one cloud:** each cloud runs the same Silver contract next to its data, staging text-free open rows into a hub via a scheduled Hub Transfer Port and running judges in-cloud. See [clouds](clouds.md).
- **Capacity and governance:** `1:N` gateway `requests`, SCD-2 capacity pools/allocations, and scoped `allowed_models` with Gold enforcement. See [capacity](capacity.md) and [spec/capacity.md](../spec/capacity.md).
