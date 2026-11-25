---
type: atom
related:
  - "layers.md"
  - "shapes.md"
  - "attributes.md"
  - "clouds.md"
  - "capacity.md"
  - "open-questions.md"
  - "../design/silver.md"
  - "../design/clouds.md"
tags: [bq-eval, silver]
---

# Silver

> The objects the core publishes and the rules they follow. Every add-on is written against this note and nothing else.

## Published objects

Every Silver object is published through **one logical SQL contract** over Bronze sources and the reference tables. The default (`light`) profile runs as pure SQL views with zero scheduled jobs; high-volume (`enterprise`) deployments may materialize incremental partitioned tables in an isolated slot reservation behind the exact same published view names (`ADR-026`). `spans` is the internal single-pass base; Gold add-ons read the six published views built on top of it (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`).

| Object | One row per | Holds |
|---|---|---|
| `spans` *(internal base)* | span | The normalized span, the telemetry shape it came from, and the application the span itself belongs to. |
| `model_calls` | model call (no text) | `span_id`, `parent_span_id`, `request_id`, `outcome_id`, `trace_owner_app_id`, call start/end times, agent, skill, model, provider, serving tier, tokens, latency, time to first chunk, finish reason, status, and confidence/cascade facts. |
| `tool_calls` | tool call | `span_id`, `parent_span_id`, `outcome_id`, `trace_owner_app_id`, call start/end times, agent, skill, tool name, status, duration, idempotency key, `action_id`, `args_sha256`, arguments and results, sandbox facts, and the flags from the tool list. |
| `events` | lifecycle, guardrail or workflow event | Outcome transitions, guardrail interventions (`guardrail.intervention`), committed writes, human handling, approvals with their approved arguments (`expires_at`), and operational human signals (thumbs, routed reviews), each marked with `source_system` and `is_authoritative`. Blind SME calibration labels live in Gold, never in Silver `events`. |
| `trace_payloads` | trace (= 1 turn) with content | `prior_history` (Turns `1..k-1`), plus this turn's `turn_prompt`, `turn_context`, `turn_output`, `step_outputs`, and `media_uris`, and for each text part whether it could be read whole. |
| `traces` | trace | Owning application, `has_root_span`, 3-principal identity (`trigger_principal`, `agent_principal`, requester `user_id` / `tenant_id` with attestation flag), conversation, start/end times, what started the turn, `case_id`, `eval_run_id`, `page_count`, `final_model_confidence`, `escalated`, counts (including `mutating_tool_call_count`, `unregistered_tool_count`, `guardrail_intervention_count`), token totals, facts, what was missing, completeness, and the time the last span arrived. |
| `outcomes` | business outcome (open or closed) | `is_closed`, open time, closing facts when closed, `case_id`, `eval_run_id`, `page_count`, `final_model_confidence`, `escalated`, its traces, how many of them ended in a root error or reported failure, which trace closed it, and the time the last span arrived. |

Three views hold text or raw ids next to other columns. Each has a twin without them: `traces_open`, `tool_calls_open`, `events_open` (see [Access](#access)).

## Reference tables

| Table | One row per | Holds |
|---|---|---|
| Application list (`app_registry`) | application (optionally versioned by `effective_from` / `effective_to`) | Owner, business unit, environment, `risk_tier`, `payload_profile` (`zdr_metadata \| ephemeral_sample \| regulated_worm`), and `sampling_mode` (`full \| sampled`). |
| Tool list (`tools`) | application and tool (optionally versioned by `effective_from` / `effective_to`) | Factual capability flags only: `writes_state`, `executes_code`, `sends_external_reply`, `returns_pii`, `creates_ticket` (approval thresholds and rules live in Gold). |

Reference tables support optional SCD-2 validity windows (`effective_from`, `effective_to`; `NULL` end means active) joined against span/trace time so editing an owner, risk tier, or tool flag never rewrites historical Silver reads. Simple single-row deployments leave validity timestamps open and Gold additionally snapshots the reference values it used with each stored result ([gold.md](gold.md)). Downgrading a tool capability flag (such as `writes_state = true -> false`) is a governed change, never an automated application write.

Keeping factual capabilities in `tools` ensures an agent never self-certifies whether a tool mutates state, runs code, or sends external replies. Capacity pools, capacity allocations and the model allow-list belong to the [capacity and governance extension](capacity.md). A user directory is not a Silver table; the cost add-on joins whatever directory the deployment has.

## Rules

### Telemetry comes in at most three shapes

Silver reads three shapes of telemetry and no others. One of them carries content by reference: the step holds an address and the content sits in storage. The shapes, and the setup each framework needs to produce one, are in [shapes.md](shapes.md). Standard OpenTelemetry names, `EVAL-P11` `outcome.*` keys, and the minimal `bq_eval.*` extension attributes are specified in [attributes.md](attributes.md).

### Reference data enriches and flags; it never drops

Every trace gets its owner, unit and environment from `app_registry`, and every tool call gets its `writes_state`, `executes_code`, `sends_external_reply`, `returns_pii`, and `creates_ticket` flags from `tools`. A lookup that finds nothing is a fact of its own: the application is not registered, the tool is not registered. Every row stays. An add-on decides what a fact means.

### Counts and facts, no pass

Silver does not decide whether a trace passed. It publishes what happened, and each add-on defines its own pass ([gold.md](gold.md)).

| `traces` publishes | Kind |
|---|---|
| Spans that ended in error | count |
| Tool calls that failed | count |
| All tool calls, and repeated tool calls (same tool and `args_sha256`) | count |
| Calls that were throttled | count |
| The root span ended in error | fact |
| The application reported a failure of its own (`bq_eval.app_failed = true`) | fact |
| The application is not registered; a tool is not registered | fact |

Silver compares nothing with a limit. Latency targets, loop budgets and approval thresholds are settings of the add-on that uses them.

### Missing values stay empty

When telemetry lacks a value, Silver leaves it empty and `traces` counts what was missing: spans without a model name, without token counts, without a traffic type, without an application. Silver never fills a default that could pass for a real value. Labels for the unknown are applied by whoever reports.

### What an application says about itself

An application's own claims are kept in their own columns. A reported failure is published as a fact. A reported pass is not read, and neither is a reported "outcome verified". Evaluation counts a reported failure against the turn and gives a reported pass no weight.

### Row rules

- **3-principal identity.** Every trace records `trigger_principal` (`human | event | automated`), `agent_principal`, and requester `user_id` (with `identity_attested` when verified by a gateway or identity proxy). When no gateway/proxy attests identity, `user_id` falls back to the root span's `user.id`. Spans carrying a disagreeing `user.id` are counted. A trace without a root span has `user_id = NULL` unless attested on a gateway/proxy span, and is explicitly flagged (`has_root_span = false`).
- **Agent of a call.** A model or tool call belongs to the agent named on the call itself. When the call names none, it belongs to the nearest enclosing agent span.
- **Span deduplication & tool pairing.** A server tool span is folded into a client tool span only when it is that span's child (never paired by name and arguments). In legacy ADK traces where `call_llm` wraps a child `generate_content` span with duplicate token usage, the pair is folded into one `model_calls` row so tokens are never double-counted.
- **Turn prompt.** `turn_prompt` is the last input before the first model output of the turn, whatever its role. `traces` records what started the turn: a user message or a tool result (a resume).
- **Reply text.** `turn_output` is the arguments of the last tool call that sends an external reply. When the turn has none, it is the last model output under the root agent that asked for no tool.
- **Final confidence.** `final_model_confidence` is the confidence reported by the last model call of the trace that reports one.

### Outcomes

- `outcomes` publishes one row per seen `outcome.id` across `events` (`outcome.opened`), `traces`, and closing spans (`outcome.closed`), whether open or closed (`is_closed`).
- A trace belongs to at most one outcome. An outcome can have many traces.
- A trace that carries two outcome IDs is flagged, and so is an outcome that contains such a trace.
- `outcomes` publishes facts: `is_closed`, open time, closing status/time/owner when closed (empty while open), its traces, how many of them ended in a root error, how many reported failure, how many have no root span, and which trace closed it.
- Waiting times are not summed in Silver. `events` carries every transition with its time, and the add-on that needs a wait computes it.
- Silver does not say whether an outcome is verified. That word is defined by the evaluation add-on.

### Ownership

The application of the root span owns the trace. Each span keeps its own application, and `traces` says how many applications took part.

### Text

One `trace_id` represents one back-and-forth turn (multi-turn sessions link turns via `gen_ai.conversation.id`; multi-trace cases link via `outcome.id`). Because Turn `k`'s model input repeats Turns `1..k-1`, `trace_payloads` publishes one row per trace that has text or referenced media, separating `prior_history` from **this turn's delta**:

- `turn_prompt`: the input that started this turn (row rule above).
- `turn_context`: retrieval documents and every tool result of the turn in order, each tagged with its tool name and tool flags. An add-on filters by flag; a result is never left out because its tool writes.
- `turn_output`: the reply of this turn (row rule above).
- `step_outputs`: per-step model outputs within the turn, for primary-vs-fallback cascades and for scanning each turn once.
- `media_uris`: source file URIs left in storage in place.

A missing part stays `NULL`, never an empty string. Each text part also says whether it could be read whole: text that does not parse, or an address with no file behind it, is marked and counted. Silver does not classify a reply as an answer, a clarification or a rejection.

In Shape 3 (content by reference), storage sits behind a **BigLake table (`WITH CONNECTION`)** with Hive date/hour path partitioning (`dt=YYYY-MM-DD/hr=HH/`) and static horizon predicates — never a plain external table and never an unpruned dynamic `_FILE_NAME` join. When `execute_tool` spans omit inline payloads and are not running in zero-payload ZDR mode, `trace_payloads` and restricted `tool_calls` may reconstruct full text from uploaded payloads; however, **open twins (`tool_calls_open`, `traces_open`) never scan restricted payload buckets**. Tool execution errors on aborted turns must be emitted on the tool span itself so they are not lost if no subsequent model call occurs.

### Hashes

`args_sha256` and `approved_args_sha256` use **RFC 8785 (JSON Canonicalization Scheme / JCS)** so semantically identical JSON (including `100` vs. `100.0` vs. `1e2` and `-0.0` vs. `0`) always hashes identically — raw BigQuery `TO_JSON_STRING(PARSE_JSON(...))` is insufficient because it preserves `INT64` vs. `FLOAT64` differences.

- **Two-plane custody & open-twin isolation:** Trusted tool proxies or SDK execution adapters emit execution-time `bq_eval.args_sha256` (RFC 8785 JCS over the **executed** arguments, not LLM intent) directly on tool spans. That keeps `tool_calls_open` and `traces_open` 100% decoupled from restricted Shape-3 payload buckets and preserves `UC1`/`UC5` loop and approval checks under zero-payload ZDR (`payload_profile = 'zdr_metadata'`).
- **Fallback SQL hashing:** When inline tool arguments are present on a span without an execution-time hash, or for `approved_args` on `workflow_events`, Silver computes the RFC 8785 JCS SHA-256 hash using the same canonicalizer (via a deterministic BigQuery UDF) so both sides match identically.

### Completeness

- `traces` counts spans whose parent is absent, records `has_root_span`, and exposes `tool_call_count`, `mutating_tool_call_count`, and `unregistered_tool_count` even when `has_root_span = false`.
- `traces` carries the sampling probability of the trace from W3C `trace_state` (preserved verbatim by Cloud Trace `_AllSpans`), falling back to `bq_eval.sampling_probability`. **Missing stays `NULL` — Silver never defaults an empty sampling probability to `1.0`.**
- Silver scales nothing. Gold scales counts and cost by the probability and fails closed on "never" checks unless the application declares `sampling_mode = 'full'` in `app_registry` and has zero unexplained missing-sampling spans.
- Gold **never silently drops** a rootless trace (`has_root_span = false`) that contains tool calls or state-mutating writes; those traces route to an explicit incomplete/orphaned safety-alert queue (catching crashed writes, oversized spans dropped via `partialSuccess`, and split-cloud fragments).

### Read horizons

`traces`, `trace_payloads` and `outcomes` group spans, and BigQuery does not push outer filters past `GROUP BY`. In `light` (views-only) mode, Silver is deployed once per read horizon with a lookback predicate applied inside `spans` (and as a static Hive partition predicate on Shape-3 BigLake tables) before grouping — for example two hours, three days, and full Bronze retention. In `enterprise` mode, incremental partitioned/clustered Silver tables replace duplicate horizon datasets so any reader prunes directly on `DATE(start_time)`.

- Every horizon (and incremental profile) publishes the same views with the same columns.
- A normal turn cut by the edge of a horizon has `has_root_span = false` and is excluded from routine baseline metrics, **but** any rootless trace containing a mutating tool call still triggers the incomplete-write safety check.
- An add-on reads the shortest horizon (or partition range) that covers its question and keeps its own results from day to day.
- Multi-day outcomes and conversations are whole in the full horizon or incremental tables.

### Freshness and retention

In `light` mode Silver is views-only, so reads see Bronze directly (`14–22s` arrival lag on Cloud Trace `_AllSpans`). In `enterprise` mode incremental compaction runs every `5–15` minutes in an isolated slot reservation, decoupling `silver_open` metadata retention (e.g., 1–7 years) from Bronze trace-store defaults (30 days) without retaining raw prompts.

Checks on Silver run on a schedule of minutes, not seconds. A guard that must act in under a minute belongs to the request path (gateway or collector/app) and is not a BigQuery polling query ([gold.md](gold.md#2-monitoring-add-on)).

### Clouds

Text stays in the cloud where it landed. The core contract describes one cloud. What a second cloud adds, and how spoke open twins stage into a hub, is in the [clouds extension](clouds.md).

### Access

Restricted and open views live in **two physically separate datasets** (`silver_restricted` and `silver_open`, per horizon in `light` mode) with separate IAM and CMEK keys — never co-located in one dataset with per-view grants.

- `silver_restricted` holds `trace_payloads`, `traces`, `tool_calls`, and `events` (text, arguments/results, raw `user_id`/`tenant_id`).
- `silver_open` holds `traces_open`, `tool_calls_open`, `events_open`, `model_calls`, and `outcomes` (zero text, zero raw ids, zero joins to restricted Shape-3 payload buckets).
- In `light` mode, both datasets are **authorized datasets** (`targetTypes: ["VIEWS"]`) on Bronze `_AllSpans`/`_AllLogs`, and `silver_restricted` reads Shape-3 storage strictly via a **BigLake Connection** (never plain external tables). In `enterprise` mode, the incremental service account populates `silver_open` and `silver_restricted`, so downstream readers need zero Bronze IAM.

## Who may read what

| Reader | May read |
|---|---|
| Any Gold add-on | `traces`, `outcomes`, `model_calls`, `tool_calls`, `events`, `trace_payloads`, the open twins, and the reference tables (`app_registry`, `tools`) |
| Anything outside this repository | Nothing by view name (sibling tools consume versioned Gold export contracts) |

## One implementation

The SQL that ships for BigQuery is the only implementation of Silver. Offline runs use that same SQL through a translator. When the two disagree, BigQuery is right.

## Proof

Each object has a schema file: column, type, meaning, and whether it may be empty. A small fixed input set comes with the rows Silver must produce from it. No Silver column depends on the time of reading, so the expected rows are exact. That check runs offline on every gate run and on BigQuery in the build and before each release.
