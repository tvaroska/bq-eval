---
type: atom
related:
  - "layers.md"
  - "silver.md"
  - "shapes.md"
  - "clouds.md"
tags: [bq-eval]
---

# Open Questions

> What is undecided or unverified. Settled decisions live in [silver.md](silver.md), [gold.md](gold.md) and the design notes; this note tracks only what is still open, with the current leaning where one exists.

## Column lists

The objects and their rules are fixed in [silver.md](silver.md). The columns of each object are not. They come with the schema files.

- Whether `traces` also counts spans without a user id, an outcome id or a conversation id. The leaning is yes.
- How `final_model_confidence`, `escalated` and `page_count` roll up from traces to an outcome. The leaning for confidence is the last trace that reports one.
- Whether the open twins carry salted hashes of user and tenant ids in the core, or only in the [clouds extension](clouds.md). The leaning is in the core, so cost per user can be read without raw ids.

## Tests on the trace store

Five Cloud Trace (`_AllSpans` linked BigQuery dataset) behaviors were tested live:

- **Access — verified for `_AllSpans`; BigLake required for Shape 3.** Both an authorized view and an authorized dataset (`targetTypes: ["VIEWS"]`) patch onto a linked `_AllSpans` dataset and allow a service account with zero read access on `_AllSpans` or Cloud Trace to query rows while direct queries to `_AllSpans` return `403`. Over Cloud Storage external tables (Shape 3), a plain external table **fails** (`HTTP 400 accessDenied`, requiring `storage.objects.list` on the caller), whereas a **BigLake table (`WITH CONNECTION`)** succeeds (`HTTP 200`) for a zero-privilege service account. **Still untested:** authorized views across VPC-SC / CMEK perimeters.
- **Pruning & single-pass — verified with two traps.** Timestamp predicates on `start_time`, `receive_time`, and `end_time` prune both dry-run/executed bytes scanned and stage-0 records read (`18d` -> `3d` cut scanned bytes to `4.8%`, `24h` to `1.8%`, and `1h` to streaming buffer). Filtering `start_time` *inside* a horizon view cut scanned bytes **`37x`** compared to filtering `MIN(start_time)` *outside* a `GROUP BY trace_id` view (confirming zero pushdown past `GROUP BY`). **Trap:** expressing `spans` as a `UNION ALL` of two shape views over `_AllSpans` caused BigQuery to scan `_AllSpans` twice (`2x` rows/bytes), so shape routing over `_AllSpans` must run in one single `SELECT` pass.
- **Arrival — verified.** Spans became queryable in `_AllSpans` in `13.8s–21.6s` after OTLP `HTTP 200` (`receive_time - end_time` was `50–113 ms`), well inside the 5-minute missing-identity check.
- **Size — rejects the span at `65,533` bytes (no truncation).** Exact measured per-attribute string cap is **`65,532` bytes** (`65,536 - 4` bytes protobuf framing); `65,533+` bytes returns `HTTP 400`, and inside a mixed OTLP batch returns `HTTP 200` with `partialSuccess.rejectedSpans = "1"` — silently dropping only the oversized span while sibling spans land. Two `40 KiB` attributes (`80 KiB` total) on one span succeed (`HTTP 200`), confirming the cap is per attribute, not per span. Span `events` attributes enforce the exact same 64 KiB hard-reject cap. Because dropped spans never reach `_AllSpans`, `read_whole = false` cannot detect them; they surface only as missing-parent or rootless traces.
- **Trace state — verified.** W3C `traceState` (including `ot=th:...`) is preserved verbatim in `_AllSpans.trace_state`.

## Settle time and horizon lengths

How long a trace must be quiet before an add-on counts it as settled, and how long each read horizon is. Both are settings, not design, and need a measurement of how late spans arrive and what a scan costs. Whether a stored layer is ever needed on top of horizons is decided by that measurement; it is not part of the contract.

## Hash equality

Tested live in BigQuery: `TO_JSON_STRING(PARSE_JSON(...))` normalizes object key ordering (including nested objects), whitespace, trailing decimal zeros (`10.50` == `10.5`), and Unicode escapes (`\u00e9` == `é`), **but does not normalize integer vs. float representations**: `100` vs. `100.0`, `100` vs. `1e2`, `0` vs. `0.0`, and `-0.0` vs. `0` all hash differently (`equal = false`). Raw `TO_JSON_STRING(PARSE_JSON(...))` is therefore insufficient for `args_sha256` / `approved_args_sha256` equality whenever one side serializes `100` and the other serializes `100.0`; canonical RFC 8785 (JCS) normalization is required.

## What each shape can fill

The three [shapes](shapes.md) do not carry the same facts, and no table yet says which columns are empty by construction for which shape. That includes what counts as throttled in each shape. The leaning is to build the table from recorded traces, not from reading packages.

## First-token time & live ADK 2.14.0 findings

Tested live with Google ADK 2.14.0 (both Format 1 legacy and Format 2 `SPAN_ONLY`, with a tool call, exported to Cloud Trace `_AllSpans`):

- **First-token time:** Confirmed absent on spans in both Format 1 and Format 2 (`gen_ai.response.time_to_first_chunk` is `NULL` on every span). Stamping it requires SDK/gateway/collector middleware.
- **Format 1 double-counting:** Every model call in ADK 2.14.0 Format 1 emits nested `call_llm` + child `generate_content` spans with identical `input_tokens` and `output_tokens` (`82/21`, `118/18`). Shape 1 SQL must fold/deduplicate that parent-child pair to prevent `2x` token and cost inflation.
- **Shape marker collision:** ADK 2.14.0 Format 2 still emits `gcp.vertex.agent.invocation_id` and `gcp.vertex.agent.event_id` on `generate_content` spans. Shape routing must key on `gcp.vertex.agent.llm_request` / `tool_call_args` (Shape 1) vs. `gen_ai.input.messages` / `gen_ai.tool.call.arguments` (Shape 2), never `invocation_id`.
- **Operation name & model/identity gaps:** ADK 2.14.0 emits `gen_ai.operation.name = 'generate_content'` (not `'chat'`), omits `gen_ai.response.model` (only `gen_ai.request.model` is populated), omits `gen_ai.response.id`, and emits no `user.id` on root spans even when `user_id` is passed to `Runner.run_async()`.
- **Cloud Logging `_AllLogs` (also verified live):** Authorized Dataset (`targetTypes: ["VIEWS"]`) and timestamp pruning (`1.01 MB` -> `21.1 KB` 24h -> `680 B` 1h) both pass on linked `_AllLogs`. OTLP `/v1/logs` `gen_ai.evaluation.result` lands with `log_id = 'gen_ai.evaluation.result'`, body in `text_payload`, resource/scope in `otel`, all OTel attributes in `labels` JSON (not `json_payload`), and bare-hex `trace` IDs.

## Sessions

A session is not a Silver object. `traces` carries `gen_ai.conversation.id` and `start_time` (which Gold uses to order turns and count user-turn conversation length without a custom `turn_index`). Whether multi-turn conversations ever need a dedicated Silver view is open.

## Copies of text

Decision needed. Whether Silver ever stores a copy of text or encoded files. The leaning is never: content stays where it landed and is read only for the traces someone judges.

## A step that fits no shape

Decision needed. What Silver does with a step it cannot read as one of the three [shapes](shapes.md). The leaning is to keep it, flag it and count it.

## Size of content on a step

Tested live (see [Tests on the trace store](#tests-on-the-trace-store)): the exact per-attribute string cap is **`65,532` bytes** (`HTTP 200`), and **`65,533+` bytes** hard-rejects the span (even inside an `HTTP 200` `partialSuccess` batch) rather than truncating. The cap applies per attribute (`2 x 40 KiB` on one span succeeds), and span `events` attributes share the exact same 64 KiB cap. Any workload that can exceed `65,532` bytes in one attribute must use content by reference.

## Proof of each setup

Google ADK 2.14.0 Format 1 (legacy) and Format 2 (`SPAN_ONLY`) have now been recorded live against Cloud Trace `_AllSpans` (see [First-token time & live ADK 2.14.0 findings](#first-token-time--live-adk-2140-findings)). The remaining framework versions and settings in [shapes.md](shapes.md) — and the Shape 3 reference hook — still come from reading each package, not from running it. A setup counts as supported only when a recorded trace from it is among the fixed inputs.

## Limits of content by reference

- Standalone `execute_tool` spans in traces with zero model calls have no uploaded model-message JSONL to reconstruct arguments/results from (normal agent turns reconstruct `tool_calls` via `gen_ai.tool.call.id` under `ADR-016`), and aborted turns without a subsequent model call drop tool outputs/errors.
- The exact file names of the upload contract are fixed when the reference hook is written and a recorded trace from it is among the fixed inputs.
- **Measured live (`200` tiny JSONL files, `374 KB` total):** literal `WHERE _FILE_NAME = '...'` pruned to `1` file (`1.87 KB`, `100–136 ms` slot), **but dynamic `JOIN` on `e._FILE_NAME = s.messages_ref` scanned `100%` of all `200` files** (`374 KB`, `15.9s–24.9s` slot = `~80–125 ms` slot overhead per tiny file) on both plain external and BigLake tables — and even on Hive-partitioned BigLake tables when joining on `e.dt = s.span_dt`. Only a **static Hive partition predicate** (`WHERE dt = DATE '...'`) pruned GCS reads (`20/200` files, `37.5 KB`, `2.2s` slot). Flat per-call JSONL joined dynamically by `_FILE_NAME` therefore does not prune; Shape 3 requires BigLake Connections (for IAM), Hive date/hour partitioning with static horizon predicates, and/or micro-batched Parquet/Iceberg storage.

## Outcome status

Decided for BQ Eval in `ADR-029` ([attributes.md](attributes.md), [gold.md](gold.md)): `outcome.status` adds `rejected | denied` alongside `guardrail.intervention` so policy blocks do not inflate error rates. Open: synchronizing the new values into the cross-repo standard (`EVAL-P11`).

## Retention for regulated workloads

Decided in `ADR-029` ([gold.md](gold.md)): regulated workloads write immutable WORM Gold Audit Snapshots (point-in-time scorecard, hashes, calibration intervals, and reference-table snapshots) rather than forcing multi-year raw Bronze prompt retention. Open: exact retention-lock DDL and archive bucket lifecycle templates.

## Clouds extension

Decided in `ADR-028` ([clouds.md](clouds.md), [design/clouds.md](../design/clouds.md)): the hub ingests text-free `model_calls`, `outcomes`, and open twins via a scheduled Hub Transfer Port (avoiding synchronous cross-cloud queries on live dashboards), executes judges via an In-Cloud Judge Port next to raw text in each non-GCP cloud, and correlates split traces across clouds in the hub on `(trace_id, parent_span_id)`. Open: throughput benchmarking of the Hub Transfer Port at high call volume, multi-cloud reference fixtures, and whether Gold add-ons should flag split traces whose remote cloud transfer has not arrived yet as incomplete.
