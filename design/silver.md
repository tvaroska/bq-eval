---
type: atom
up: "layers.md"
tags: [bq-eval, silver]
---

# Silver

> How the five Silver objects are produced. The contract they meet is [spec/silver.md](../spec/silver.md).

## From sources to `spans`

Each source has one adapter: a view that maps the source's own columns to the common span columns and tags each row with its source. `spans` is the union of the installed adapters.

- An adapter maps; it does not repair. A value the source lacks stays empty.
- When the same trace arrives from two sources in one cloud, the trace store wins the whole trace. Agent events are used only for traces the trace store does not have. Spans from two sources are never mixed inside one trace.
- Which adapters are installed is chosen when Silver is deployed, because a view cannot refer to a source that does not exist.

### Shapes inside the trace store

The trace store holds telemetry in up to three [shapes](../spec/shapes.md). Each shape has its own adapter view, and every adapter maps to the same span columns. That column list is the contract; the adapters are the only place that knows a shape.

- A step's shape is decided from a marker attribute, in one pass over the table.
- The adapters fold the few names that differ inside one shape, such as the token counts, into the common columns.
- A shape is absorbed here, inside BigQuery. Telemetry is not rewritten before it is stored: the original stays readable, a wrong mapping can be corrected for past data, and what could not be read can be counted.

### Content by reference

For the shape that carries addresses, an external table lies over the storage path. An adapter joins a step to its content by the address, which is the file name. An address with no file behind it is counted as missing.

## `requests`

A view over the gateway's or the model server's request records, one row per model call. It is kept apart from `spans` on purpose: the two describe the same call from different sides, and the server's side is the authority for billed tokens and for whether dedicated capacity served the call.

## `trace_payloads`

A view that gathers the prompt, the retrieved context and the final answer per trace from wherever the source keeps text. It produces a row only when there is text.

## `traces`

A stored table, partitioned by day, filled by a scheduled SQL job.

1. Group the spans of each trace.
2. Take the owner from the root span and count the applications that took part.
3. Count spans, tool calls, errors and throttled calls; sum tokens.
4. Count what was missing.
5. Join the application list and the model capacity table to set the reference flags and the latency target.
6. Compute each check as its own column, then the overall pass from the behaviour checks and the application's own reported failures.
7. Record when the last span arrived and whether the trace is settled.

The job rebuilds a sliding window of recent days and replaces those day partitions. Running it twice gives the same result.

## `outcomes`

A stored table built by the same job. An outcome's rows come from its closing span and from the `traces` rows that carry its ID. A trace with two outcome IDs is flagged in `traces`; an outcome that contains one is marked so that it cannot count as "all traces pass".

## Reference tables

The application list and the model capacity table are ordinary tables that the owner of the deployment maintains. Silver only reads them. A trace whose application is not in the list still gets a row; the flag says so.

## One implementation

There is one body of SQL. Offline runs, including the demo and the fast tests, execute it through a SQL translator. The translator rewrites a few functions, so an offline result is a quick check, not the final word.

## Proof

- One schema file per object, next to the SQL.
- A small fixed input set, with the Silver rows it must produce.
- The check runs through the translator on every gate run.
- The same check runs on BigQuery, in a scratch dataset, in the build and before each release.
