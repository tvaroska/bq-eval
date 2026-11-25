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

> What is undecided or still requires live-environment verification. Settled decisions live in [silver.md](silver.md), [gold.md](gold.md), [clouds.md](clouds.md), [capacity.md](capacity.md), and the design notes; this note tracks only what remains open or empirically unverified.

## Column lists & exact schema files

The objects, semantics, and nullability rules are fixed in [silver.md](silver.md) and [capacity.md](capacity.md). Exact column DDL files ship alongside the Silver SQL implementation.

- Whether `traces` also counts spans without a user id, an outcome id, or a conversation id. Leaning: yes.
- **Decided (`ADR-034`):** when the root span and a platform-class span elsewhere in the trace carry different user ids, Silver does not choose; which id is the requester is Gold's rule. Open with that rule: the rule itself (leaning: the platform-class id, because it is the verified one), and whether Silver then publishes that id beside the root span's (leaning: yes, Gold cannot read it otherwise).
- Which of today's columns stay columns and which become registered keys of the `labels` column (`ADR-039`). Settled with the column files.
- Where the linked outcome id of an `outcome.linked` row is read from; `workflow_events` has no column for it. Leaning: a key in `payload_json`.
- How `outcomes`, which can span days, relate to a read window (`ADR-035`). Leaning: an outcome is returned when one of its traces or events falls in the window, with a fact for parts that lie outside.
- **Decided (`ADR-033`):** Multi-trace/map-reduce outcomes default to worst-chunk `min_model_confidence` alongside `final_model_confidence`.
- Whether per-BU KMS-HMAC `user_id_hash`/`tenant_id_hash` (`hmac_key_version`, `ADR-028`/`ADR-031`) should also populate `silver_open` in single-cloud deployments. Leaning: yes, so per-user cost reads stay in `silver_open` without raw ids while preserving RTBF crypto-shredding separation.

## Tests on the trace store (live probes & remaining perimeter checks)

Five Cloud Trace (`_AllSpans` linked BigQuery dataset) behaviors were tested live:

- **Access — verified on standard projects; VPC-SC + multi-region CMEK still untested:** Both an authorized view and an authorized dataset (`targetTypes: ["VIEWS"]`) patch onto linked `_AllSpans` and `_AllLogs` datasets and allow a zero-privilege service account to query rows (`HTTP 200`, direct=`403`). Over Shape-3 storage, plain GCS external tables fail (`400 accessDenied`), whereas **BigLake tables (`WITH CONNECTION`)** succeed (`HTTP 200`). **Still untested (P0 live probe):** cross-project Authorized Datasets (`_AllSpans`, `_AllLogs`) and per-BU `bqcx-*` BigLake Connections across **locked-down VPC-SC perimeters + `US`/`EU` multi-region CMEK** and multi-project landing zones (plus Row Access Policies on `silver_restricted`).
- **Pruning & single-pass — verified with two traps:** Inside-horizon `start_time` filters cut scanned bytes **`37x`** vs. outer filters past `GROUP BY`. Expressing `spans` as `UNION ALL` over `_AllSpans` scanned `_AllSpans` **`2x`**, so shape routing runs in one single `SELECT` pass. **Still untested (P0 live benchmark):** the design-only high-volume profile, a stored text-free span base side by side with bounded `enterprise` `MERGE` (with static target `DATE(start_time)` partition pruning and `max_bytes_billed` / `INFORMATION_SCHEMA.JOBS` slot circuit breakers) at multi-PiB scale.
- **Arrival — verified:** Spans landed in `_AllSpans` in `13.8s–21.6s` after OTLP `HTTP 200` (`receive_time - end_time` `50–113 ms`).
- **Size — hard-rejects at `65,533` bytes (no truncation):** Exact measured per-attribute string cap is **`65,532` bytes** (`HTTP 200`); `65,533+` bytes hard-rejects the span (including inside mixed `HTTP 200` `partialSuccess` batches and span `events`), requiring Shape 3 for large payloads and reverse `action.committed -> tool_calls` checks for regulated writes.
- **Trace state — verified:** W3C `traceState` (`ot=th:...`) is preserved verbatim in `_AllSpans.trace_state`.
- **Read windows, untested (`ADR-035`):** that a window passed as function arguments limits scanned bytes as a filter inside a view does, and that table functions can be authorized on linked `_AllSpans`/`_AllLogs` like the views.
- **Writer provenance, measured on recorded traces (`ADR-034`):** `_AllSpans` has no column that records who wrote a span, so the writer class has to come from the landing place. The server-set `apphub` column was empty on all `38,859` spans, while `34,803` carried a self-declared `apphub.application.id` resource attribute. The environment attribute appears under its older name `deployment.environment` on `23,783` spans and under `deployment.environment.name` on none, so the second identity path has to read both (`ADR-040`). **Still untested:** a gateway's spans in a separate landing place joining an application's spans by trace id.
- **Root facts, measured (`ADR-037`):** of `8,716` Cloud Run traces only `49` had a parentless span; `8,665` of the other `8,667` had only server-kind top spans, whose parent is the runtime's front end, and `2` had other orphans. A "no parent" definition of root would mark `99.4%` of these traces rootless. The same picture appears at every service hop: of `25,969` application server spans none has its parent in another service, and `11,169` have a parent that is not stored, because the cloud runtime's own span sits between caller and callee. An orphan of server kind therefore shows no loss, and a lost span whose only children are server spans in another service stays invisible. Of the `1,580` parentless spans, `1,555` were the cloud runtime's own request spans (no instrumentation scope, no span kind, a `/component` label of `HTTP load balancer` or `AppServer`) and `25` carried no SDK resource attributes (probe spans sent by hand; `24` checked by name). No deployed application emitted a parentless span. **Decided (`ADR-037`):** the root is the first span, by start time, that an application emitted; the cloud runtime's own spans do not count. Measured on `8,856` traces with application spans: that span never had an application span above it and was never tied; `6,144` traces had one top application span and `2,712` had two to eight, so "top-most" alone does not pick one. While a trace is still arriving, its entry span may not be in the store yet. Turns, measured: `499` of `9,494` traces hold an agent, model-call or tool span (`447` a model call); where the runtime's request span is stored and starts first, it precedes the first application span by `17 ms` at the median and up to `53 s`.
- **Read margin, measured (`ADR-035`):** trace length was `244 ms` at p50, `18.8 s` at p95, `54.1 s` at p99 and `406 s` at most; a start-time filter cut `6` of `170` traces at the start of a busy 30-minute window.
- **Span kinds and tokens, measured (`ADR-036`, `ADR-042`):** `35,708` spans (`92%`) carried no GenAI operation. `1,045` spans carried a token attribute and `924` reported a count above zero; the `121` zeros include all `99` tool spans that carried the attribute. The operations Silver lists as model calls (`chat`, `generate_content`, `embeddings`, legacy `call_llm`) covered `702` of the `924` spans but only `35%` of their input tokens; the rest sat under application-specific operation names. Only `8` spans carried content in an accepted shape.
- **Token spans inside token spans, measured (`ADR-042`):** `22` spans that report tokens enclose `50` others: `2` legacy ADK wrappers equal to their inner call, and `20` application spans of which `7` equal the sum of their inner calls and `13` report less. None reports more than its inner spans. Counting both adds `66,360` input tokens (`1.1%`) here and doubles them in the legacy ADK format. **Decided (`ADR-042`):** the innermost span that reports tokens is the model call, and an enclosing span is not counted again.
- **Duplicates, measured (`ADR-042`):** `791` `(trace_id, span_id)` keys appear twice, with the same attributes and different arrival times: `753` are runtime request spans and `38` are application spans from one load run. `dropped_attributes_count` and `dropped_events_count` were zero on every span.
- **Sampling, measured (`ADR-043`):** no application span carried a sampling probability, in trace state or as an attribute.
- **Scan size, measured (`ADR-035`):** a query that reads the trace table through five subqueries processed `6.1 MB` against `4.8 MB` for one, so bytes do not grow with the number of references, while slot time grew `5.5x`. A filter on arrival time processed the same bytes as a filter on start time, and a window passed as query parameters the same bytes as literals. A table function itself is still untested.

## Settle time, read margin & session inactivity cutoffs

Exact default values for the read margin around a window, `settle_buffer`, `merge_lookback_days`, `outcome_lookback_days`, and Gold's bounded `session_inactivity_hours` cutoff are deployment settings to be tuned from live span arrival skew and scan-cost measurements.

## Hash equality

Tested live in BigQuery: `TO_JSON_STRING(PARSE_JSON(...))` fails int-vs-float equality (`100` vs. `100.0` / `1e2`, `0` vs. `0.0` / `-0.0`), so both execution-time `bq_eval.args_sha256` and Silver SQL fallback hashing use deterministic **RFC 8785 (JCS)** normalization (with a payload-byte guard before SQL UDF invocation).

## What each shape can fill & proof of each setup

Google ADK `2.14.0` Format 1 (legacy) and Format 2 (`SPAN_ONLY`) have been recorded live against Cloud Trace `_AllSpans` (exposing Format 1 `call_llm` + `generate_content` `2x` token duplication, Format 2 `invocation_id` marker collision, and missing `gen_ai.response.model`, `gen_ai.response.id`, root `user.id`, and `gen_ai.response.time_to_first_chunk`), and `_AllLogs` OTLP `gen_ai.evaluation.result` ingestion has been verified live (`labels` JSON, bare-hex `trace`).

**Still unrecorded (must capture live fixtures before marking supported):**
- LangGraph / LangChain, OpenAI Agents SDK, Pydantic AI, Strands, Microsoft Agent Framework, and LiteLLM gateway.
- Custom central LLM gateway (`_AllLogs` `inference_request` and AWS S3 Iceberg proxy logs) with `x-client-request-id` + `1:N` `429` failover attempts.
- Coding-agent SDK (`Claude Agent SDK`) and ephemeral dev-container OTel Collector adapter.
- Shape 3 async BigLake Parquet reference hook and batch API recorded traces.
- Empirical per-shape nullability matrix built from recorded traces (not package docs).

## Limits of content by reference

- Standalone `execute_tool` spans in traces with zero model calls have no uploaded model-message payload; aborted turns must emit tool errors and execution-time `bq_eval.args_sha256` directly on `execute_tool` spans.
- **Measured live (`200` tiny JSONL files, `374 KB`):** dynamic `_FILE_NAME` and dynamic Hive `e.dt = s.span_dt` joins scanned `100%` of files (`~80–125 ms` slot/file), whereas static Hive literal predicates (`WHERE dt = DATE '...'`) pruned reads. Shape 3 therefore mandates BigLake Connections + static Hive date/hour predicates + micro-batched Parquet/Iceberg at scale.

## Settled architecture items (`ADR-029`–`ADR-033`)

- **Hub region & residency (`ADR-031`):** Multi-region (`US` or `EU`) is the default Hub topology with strict per-domain location/CMEK alignment (never mix `US` and `us-east4` in one query domain); non-hub sovereign/single regions act as spokes via the Hub Transfer Port.
- **Regulated WORM & RTBF (`ADR-031`):** Regulated snapshots export to PII-free **GCS Bucket Retention-Lock + CMEK** archives (not BigQuery tables alone) paired with per-subject crypto-shredding DEKs for RTBF. Open: shipping the exact Terraform/DDL archival template.
- **Fast local/CI gates (`ADR-032`):** Sub-5s DevAI PR gates and offline optimizer sweeps run through the **Local/CI Eval Runner Port** (`CI_ONLY`), never polling BigQuery.
- **Cross-cloud skew (`ADR-031`):** Hub views flag split traces whose remote spoke `transfer_watermark_utc` has not yet advanced as `remote_fragment_pending = true`.
- **Outcome status (`ADR-029`):** `rejected | denied` added alongside `guardrail.intervention`. Open: syncing new enum values into cross-repo `EVAL-P11`.

## A step that fits no shape

Decided (`ADR-036`, `ADR-042`): the shape is decided by content and the kind of a span by its operation name, and a span that reports tokens above zero is a model call whatever that name is. A span with no GenAI operation and no tokens is an ordinary span: kept, counted, not flagged. `spans` keeps one row per `(trace_id, span_id)`, the latest arrival, and counts the rest. A model or tool call without content is kept in the text-free objects with a content-missing fact and is not scored (`ADR-036`); in the recorded traces no deployed service sends content in an accepted shape, so all `918` of its token spans are of that kind. Still open: a span whose operation name is outside the conventions and that reports no tokens, such as the `631` recorded `invoke_node` spans. Leaning: keep it and count it. Also open: a tool span that reports tokens above zero, which the recorded traces do not contain. Leaning: it stays a tool call and its tokens also appear as a model call.

## Clouds extension (remaining live benchmarks)

Architecture is decided in `ADR-028` and `ADR-031` ([clouds.md](clouds.md), [design/clouds.md](../design/clouds.md)). **Still unbenchmarked live:** Hub Transfer Port throughput (Omni `CTAS` `< 60 GiB` vs. S3 -> Interconnect/STS -> Hub GCS -> `LOAD DATA`) at multi-PiB/high-QPS scale, AWS Bedrock VPC In-Cloud Judge Port latency/cost, cross-cloud KMS-HMAC key-import rotation drill, and multi-cloud recorded reference fixtures.
