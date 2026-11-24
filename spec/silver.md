---
type: atom
related:
  - "layers.md"
  - "shapes.md"
  - "open-questions.md"
  - "../design/silver.md"
  - "../design/clouds.md"
tags: [bq-eval, silver]
---

# Silver

> The objects the core publishes and the rules they follow. Every add-on is written against this note and nothing else.

## Published objects

| Object | One row per | Kept as | Holds |
|---|---|---|---|
| `spans` | span | view | The normalized span, the source it came from, and the application the span itself belongs to. |
| `requests` | model call, as the server saw it | view | Billed tokens, serving tier, status code, time, and the trace ID when the caller sent one. |
| `trace_payloads` | trace that has text | view | Prompt, retrieved context and final answer. |
| `traces` | trace | stored table, by day | Owning application, times, counts, token totals, one column per check, the overall pass, what was missing, and whether the trace is settled. |
| `outcomes` | business outcome | stored table, by day | What the closing span reported, the traces that belong to the outcome, and whether all of them pass and are settled. |

## Reference tables

| Table | One row per | Holds |
|---|---|---|
| Application list | application | Owner, unit, environment, latency target, and the application's share of dedicated capacity. |
| Model capacity | model, region and period | Dedicated units bought (may be zero) and whether the model is allowed. |

A model can be allowed without dedicated capacity. There is one model capacity table for the whole organisation.

## Rules

### Telemetry comes in at most three shapes

Silver reads three shapes of telemetry and no others. One of them carries content by reference: the step holds an address and the content sits in storage. The shapes, and the setup each framework needs to produce one, are in [shapes.md](shapes.md).

### Reference data enriches and flags; it never drops

Every trace gets its owner, unit and environment from the application list. Three yes/no flags come from the reference tables: the application is not registered, a model is not on the allowed list, the call was served on dedicated capacity. Every row stays. An add-on decides what a flag means.

### Checks

Each check is its own column. The overall pass is built from the behaviour checks only.

| Check | Changes the overall pass |
|---|---|
| A span ended in error | yes |
| A tool call failed | yes |
| Tool loop: more than 8 tool calls in the trace | yes |
| The application reported a failure of its own | yes |
| A call was throttled | no |
| The trace was slower than the application's latency target | no |
| The application is not registered | no |
| A model is not on the allowed list | no |

The tool-loop limit is the same for every application. The latency target comes from the application list; when an application has none, the slow flag is empty, not false.

### Missing values stay empty

When telemetry lacks a value, Silver leaves it empty and `traces` counts what was missing: spans without a model name, without token counts, without a traffic type, without an application. Silver never fills a default that could pass for a real value. Labels for the unknown are applied by whoever reports.

### What an application says about itself

An application's own claims are kept in their own columns and trusted in one direction only. A reported failure fails the trace. A reported pass counts for nothing, and neither does a reported "outcome verified". The overall pass comes from what was observed.

### Outcomes

- A trace belongs to at most one outcome. An outcome can have many traces.
- A trace that carries two outcome IDs is flagged, and an outcome that contains such a trace cannot count as "all traces pass".
- `outcomes` publishes facts: the status, time and owner the closing span reported, the traces, and whether all of them pass and are settled.
- Silver does not say whether an outcome is verified. That word is defined by an add-on, which can add an evaluation result.

### Ownership

The application of the root span owns the trace. Each span keeps its own application, and `traces` says how many applications took part.

### Text

`trace_payloads` has a row only when text exists. A missing part is empty, never an empty string. `traces` says whether a trace has text, so "nothing to judge" can be told apart from "empty answer".

### Freshness

`traces` and `outcomes` are rebuilt over a sliding window of recent days. Each row says when its last span arrived and whether it is settled, meaning no new span has arrived for a set time. An add-on that needs final numbers reads settled rows only.

### Clouds

Data stays in the cloud where it landed, and Silver is computed there. Every cloud has the same objects, produced by the same SQL. The reference tables are copied into each cloud.

- Per-trace and per-outcome rows may be read from the hub. They contain no prompt or answer text.
- Spans and text do not leave their cloud, with one exception: for cost, the model, serving tier and token counts of model-call spans may be read from the hub.
- A trace whose spans sit in two clouds belongs to the cloud that holds the root span. That cloud publishes the trace from the spans it can see. The spans in the other cloud form a separate, flagged fragment and are never merged in.

## Who may read what

| Reader | May read |
|---|---|
| Any add-on | `traces`, `outcomes`, `requests`, `trace_payloads`, the reference tables |
| The cost add-on | Also `spans`, always with a date range |
| Anything outside this repository | Nothing by view name |

## One implementation

The SQL that ships for BigQuery is the only implementation of Silver. Offline runs use that same SQL through a translator. When the two disagree, BigQuery is right.

## Proof

Each object has a schema file: column, type, meaning, and whether it may be empty. A small fixed input set comes with the rows Silver must produce from it. That check runs offline on every gate run and on BigQuery in the build and before each release.
