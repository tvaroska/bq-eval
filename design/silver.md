---
type: atom
up: "layers.md"
tags: [bq-eval, silver]
---

# Silver

> How the Silver views are produced. The contract they meet is [spec/silver.md](../spec/silver.md).

## From telemetry shapes to `spans`

The trace store holds telemetry in up to three [shapes](../spec/shapes.md). Over a shared `_AllSpans` table, `spans` routes shapes in **one single `SELECT` pass** (`CASE WHEN` marker checks rather than a `UNION ALL` over `_AllSpans`, which empirically scans the trace store `2x`). Shape 1 (legacy ADK) is identified by `gcp.vertex.agent.llm_request` / `gcp.vertex.agent.tool_call_args` / `call_llm` (not `gcp.vertex.agent.invocation_id`, which ADK 2.14 still emits in Format 2); Shape 2/3 are identified by `gen_ai.input.messages` / `gen_ai.input.messages_ref` / `gen_ai.tool.call.arguments`. Frameworks outside the three shapes normalize upstream through the Bronze / OTel Collector Adapter Port before landing ([spec/shapes.md](../spec/shapes.md)).

- An adapter maps; it does not repair. A value the source lacks stays empty.
- The single-pass router folds standard name variants (`gen_ai.operation.name IN ('chat', 'generate_content')`, `COALESCE(gen_ai.response.model, gen_ai.request.model)`, optional `5m`/`1h` cache-write splits) per [spec/attributes.md](../spec/attributes.md).
- Gold add-ons read the typed views below rather than `spans` directly.

### Content by reference (BigLake + Hive partitioning)

For Shape 3, storage sits behind a **BigLake external table (`WITH CONNECTION`)** over Hive date/hour partitions (`dt=YYYY-MM-DD/hr=HH/...`), preferring micro-batched Parquet/Iceberg at scale. Plain GCS external tables fail authorized-view IAM (`storage.objects.list`), and dynamic `JOIN`s on `_FILE_NAME` scan `100%` of files — so horizon views apply a **static Hive partition predicate** (`WHERE dt >= CURRENT_DATE() - N`) matching the span horizon before joining by reference address. Missing files are counted and mark `read_whole = false`.

Writers follow the risk-tiered async upload contract in [spec/shapes.md](../spec/shapes.md#upload-contract) (bounded non-blocking buffer with drop counters for standard tiers; fail-closed enqueue before execution for Tier-1 regulated writes; disabled under zero-payload ZDR).

## Read horizons and incremental acceleration (`light` vs. `enterprise`)

BigQuery does not push outer `WHERE` filters past `GROUP BY trace_id` (`37x` scan penalty measured live). Silver therefore supports two physical profiles behind the exact same six published view names (`ADR-026`):

- **`light` profile (views-only):** Deploys the same SQL per read horizon (`2h`, `3d`, `all`), placing `WHERE start_time >= TIMESTAMP_SUB(...)` inside `spans` (and static `WHERE dt >= ...` on BigLake Shape 3) before any grouping. The unfiltered horizon backs offline/CI exact-row proofs.
- **`enterprise` profile (incremental tables):** A scheduled BigQuery SQL `MERGE` runs every `5–15` minutes in an isolated slot reservation over `(watermark - settle_buffer .. now)` into native partitioned (`DATE(start_time)`) and clustered (`trace_owner_app_id, trace_id`) tables, eliminating duplicate horizon datasets, cutting PiB-scale scan cost, and decoupling `silver_open` metadata retention from `_AllSpans`'s 30-day default.

## `model_calls`

Projects text-free call facts over `spans` where `gen_ai.operation.name IN ('chat', 'generate_content')` or legacy `call_llm` (`span_id`, `parent_span_id`, `request_id` / `gen_ai.response.id`, `outcome_id`, `trace_owner_app_id`, `start_time`/`end_time`, `agent_id`/`agent_name`, `skill_name`/`skill_version`, `COALESCE(response_model, request_model)`, provider, serving tier, token counts incl. optional `5m`/`1h` cache-write breakdown, latency, time to first chunk, finish reason, HTTP status, confidence/cascade facts).

- **Legacy ADK deduplication:** In ADK Format 1 where `call_llm` wraps a child `generate_content` span carrying duplicate token usage, Silver folds the child into its parent `call_llm` row so tokens are never double-counted.
- The agent of a call is the agent named on the span, else the nearest enclosing agent span.

## `tool_calls`

Projects `span_id`, `parent_span_id`, `outcome_id`, `trace_owner_app_id`, `start_time`/`end_time`, `agent_id`/`agent_name`, `skill_name`/`skill_version`, tool name, status, duration, idempotency key, `action_id`, `args_sha256`, and (in `silver_restricted` only) tool arguments/results, enriched with SCD-2 capability flags from `tools`.

- A server tool span whose parent is a client tool span folds into that parent (never paired by name and arguments).
- **Open-twin isolation & ZDR:** `tool_calls_open` reads execution-time `bq_eval.args_sha256` directly from tool spans (or computes RFC 8785 JCS SHA-256 from inline span attributes when present) and **never scans restricted Shape-3 payload buckets**. Restricted `tool_calls` may still reconstruct full argument/result text from BigLake Shape-3 payloads where available; aborted-turn tool errors remain intact on the tool span itself.

## `events`

`UNION ALL` of the two copies of partitioned/clustered `workflow_events`, closing spans (`outcome.closed`), guardrail interventions (`guardrail.intervention`), and OTel `gen_ai.evaluation.result` log records (in linked `_AllLogs`, reading `log_id = 'gen_ai.evaluation.result'`, OTel attributes from `labels` JSON, and normalized 32-hex `trace_id`), matched via `trace_id`, `outcome_id`, or `gen_ai.response.id`.

- `is_authoritative = true` exclusively for rows in the system-of-record copy of `workflow_events`.
- Approval rows carry `approved_args` (and optional `approved_expires_at`); Silver computes `approved_args_sha256` using RFC 8785 (JCS).
- Blind SME calibration labels belong strictly to Gold, never Silver `events`.

## Hashes (RFC 8785 JCS)

Because BigQuery `TO_JSON_STRING(PARSE_JSON(...))` hashes `100` vs. `100.0` / `1e2` and `0` vs. `0.0` / `-0.0` differently, both execution-time `bq_eval.args_sha256` and Silver's SQL hash computation (`approved_args_sha256` and inline fallback) normalize JSON using **RFC 8785 (JSON Canonicalization Scheme)** via a deterministic canonicalizer so integer/float formatting differences never cause false HITL approval mismatches.

## `trace_payloads`

One row per content-bearing trace (= 1 turn) in `silver_restricted`, splitting `prior_history` from this turn's delta (`turn_prompt`, `turn_context`, `turn_output`, `step_outputs`, `media_uris`) with per-part `read_whole` flags. Missing parts stay `NULL`.

## `traces`

Groups spans per `trace_id` with SCD-2 reference enrichment:

1. Record `has_root_span`, 3-principal identity (`trigger_principal`, `agent_principal`, attested `user_id`/`tenant_id` or root-span fallback, `identity_attested`), participating app count, and disagreeing `user.id` count.
2. Count spans, model calls, tool calls, `mutating_tool_call_count`, `unregistered_tool_count`, `guardrail_intervention_count`, repeated `(tool_name, args_sha256)` calls, errored spans, failed tool calls, and throttled calls; sum tokens.
3. Record facts (`root_error`, `bq_eval.app_failed`, turn starter) and completeness (`missing_parent_count`, `sampling_probability` from `_AllSpans.trace_state` else `bq_eval.sampling_probability`, staying **`NULL`** when absent — never defaulting to `1.0`).
4. Join SCD-2 `app_registry` and `tools` at trace/span time and record last span arrival time.

No step compares a value with a limit, and none uses the current time.

## `outcomes`

Built over the union of `outcome.id` keys seen in `events`, `traces`, and closing spans (`outcome.closed`), publishing open and closed outcomes (`is_closed`), closing status (`succeeded | failed | abandoned | escalated | rejected | denied`), trace error/failure/rootless counts, and closing trace.

## Access (split datasets)

Restricted views and open twins live in **two physically separate datasets** (`silver_restricted` and `silver_open`, per horizon in `light` mode) with independent IAM and CMEK keys:

- `silver_restricted`: `trace_payloads`, `traces`, `tool_calls`, `events`.
- `silver_open`: `traces_open`, `tool_calls_open`, `events_open`, `model_calls`, `outcomes` (zero text, zero raw ids, zero joins to Shape-3 buckets).
- In `light` mode, both datasets are **authorized datasets** (`targetTypes: ["VIEWS"]`) on linked `_AllSpans` and `_AllLogs`, and `silver_restricted` reads Shape 3 via a **BigLake Connection**. In `enterprise` mode, the incremental service account populates both datasets so downstream readers need zero Bronze IAM.

## Reference tables

`app_registry` (owner, business unit, environment, `risk_tier`, `payload_profile`, `sampling_mode`) and `tools` (`writes_state`, `executes_code`, `sends_external_reply`, `returns_pii`, `creates_ticket`) support optional SCD-2 validity windows (`effective_from`, `effective_to`) joined at span/trace time. Missing lookups stay `NULL` and are flagged as unregistered facts.

## Capacity and governance extension

Documented in [capacity.md](capacity.md) and [spec/capacity.md](../spec/capacity.md): `1:N` upstream gateway `requests`, SCD-2 `capacity_pools` / `capacity_allocations` / `allowed_models`, and sub-minute metrics rollups.

## Retention

In `light` mode, Silver retention equals Bronze retention (30-day default on `_AllSpans`). In `enterprise` mode, incremental `silver_open` metadata tables retain history independently while regulated `UC5` workloads persist WORM Gold audit snapshots ([gold.md](gold.md)) instead of multi-year raw prompts.

## One implementation

There is one body of SQL. Offline runs, including the demo and the fast tests, execute it through a SQL translator. The translator rewrites a few functions, so an offline result is a quick check, not the final word.

## Proof

- One schema file per object, next to the SQL.
- A small fixed input set, with the Silver rows it must produce. No column depends on the time of reading, so the rows are exact.
- The check runs through the translator on every gate run.
- The same check runs on BigQuery, in a scratch dataset, in the build and before each release.
