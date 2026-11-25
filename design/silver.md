---
type: atom
up: "layers.md"
tags: [bq-eval, silver]
---

# Silver

> How the Silver views and bounded incremental tables are produced. The contract they meet is [spec/silver.md](../spec/silver.md).

## From telemetry shapes to `spans`

The trace store holds telemetry in up to three [shapes](../spec/shapes.md). Over a shared `_AllSpans` table, `spans` routes shapes in **one single `SELECT` pass** (`CASE WHEN` marker checks rather than `UNION ALL`, which scans `_AllSpans` `2x`). Shape 1 (legacy ADK) keys on `gcp.vertex.agent.llm_request` / `tool_call_args` / `call_llm` (never `invocation_id`); Shape 2/3 key on `gen_ai.input.messages` / `gen_ai.input.messages_ref` / `gen_ai.tool.call.arguments`. The content markers decide the shape, meaning how content is read; the kind of a span (model call, embedding, tool call, retrieval, memory, agent, other) is read from `gen_ai.operation.name`, so embedding calls and agent spans are placed although they carry no messages. A span that reports tokens above zero is a model call whatever its operation name, which also places the legacy ADK model-call span, which carried no operation name in the recorded traces; where token spans nest, only the innermost stays a model call (`ADR-042`). Spans with no GenAI operation and no tokens stay in `spans` as ordinary spans and are counted, not flagged; a model or tool call with no content keeps its row in the text-free objects with a content-missing fact and gets no text row (`ADR-036`). Gateway and tool-proxy spans are read from their own landing place, one pass per place, and carry `source_class = 'platform'` (`ADR-034`). The cloud runtime's own spans, recognised in the recorded traces by having no instrumentation scope, are kept without a class and skipped when the root is chosen (`ADR-037`). Custom gateways, coding SDKs (`Claude Agent SDK`), and non-conforming frameworks normalize upstream through the **Bronze / OTel Collector Adapter Port** before landing.

- An adapter maps; it never repairs or invents defaults.
- `spans` keeps one row per `(trace_id, span_id)`, the one that arrived last, and counts the others (`ADR-042`).
- Gold add-ons read the published views below, never `spans` directly.

### Content by reference (BigLake Parquet + Hive partitioning) & Gateway Inline-Artifact Port

For Shape 3, storage sits behind a per-BU-scoped **BigLake external table (`WITH CONNECTION`)** over Hive date/hour partitions (`dt=YYYY-MM-DD/hr=HH/...`), using **micro-batched Parquet/Iceberg** as the canonical production format (`jsonl` dev/low-volume only) in the same BigQuery location and CMEK perimeter as the trace. The window functions apply a **static Hive partition predicate** derived from the from-time and to-time, matching the span window, before joining by reference address. Missing files mark `read_whole = false`.

Interactive large attachments (`100 MB`) arriving directly at a central gateway never buffer inside SDK telemetry hooks; the **Gateway Inline-Artifact & Guardrail Port** streams files upstream via pre-signed private URLs (`media_uris`, `document_bytes`, `page_count`, `attachment_sha256`) and emits structured `guardrail.intervention` verdicts. Traffic whose content is stored nowhere is kept in the text-free objects and is not scored (`ADR-036`).

## Read windows (`light`) & the design-only high-volume profile (`enterprise`)

BigQuery does not push outer `WHERE` filters past `GROUP BY trace_id` (`37x` scan penalty measured live):

- **`light` (table functions, no stored data, `ADR-035`):** each published object is a table function of a from-time and a to-time. `spans` filters `start_time` to the window plus a margin on both sides, with static Hive `dt` predicates on Shape 3; a trace is a member when its first span starts inside the window, and a trace longer than the margin is flagged. A window has settled when its end, the margin and the settle time have passed; Gold keeps results from settled windows only. Fixed-window views are thin wrappers. To confirm on the trace store: bytes scanned through a function, and authorizing functions on Bronze.
- **`enterprise` (design only, `ADR-035`):** not decided. Candidate: a scheduled append writes a typed, text-free copy of new spans, selected by arrival time, into one partitioned table, and the same functions run on top of it; nothing is merged or rewritten and text is never copied. Earlier candidate, kept until a side-by-side test chooses: a scheduled BigQuery SQL `MERGE` per published object runs every `5–15` minutes in an isolated slot reservation over `(receive_time_watermark - settle_buffer .. now)` into `DATE(start_time)`-partitioned tables clustered on `(COALESCE(trace_owner_app_id, '__unowned__'), trace_id)`:
  1. Every `MERGE ON` clause mandates a **static target partition-prune predicate** (`target.partition_date >= DATE_SUB(CURRENT_DATE(), INTERVAL merge_lookback_days DAY)`) so late or midnight-straddling spans never rewrite multi-TiB historical partitions.
  2. `outcomes` and session rollups apply explicit bounded lookback windows (`outcome_lookback_days`, `session_inactivity_hours`).
  3. Compaction runs under `max_bytes_billed` ceilings and pre-flight `INFORMATION_SCHEMA.JOBS` slot breakers.

## `model_calls`

Projects text-free LLM and `embeddings` call facts over `spans` (`chat`, `generate_content`, `embeddings`, legacy `call_llm` deduplicated against child `generate_content`, and any other span that reports tokens above zero, innermost only), including `span_id`, `parent_span_id`, `client_request_id` (`x-client-request-id`), `response_id` (`gen_ai.response.id`), `outcome_id`, `trace_owner_app_id`, `agent_id`/`agent_name`, `skill_name`/`skill_version`/`skill_sha256`, `request_model`, `response_model`, `provider`, **`upstream_endpoint_location`**, **`thought_signature_present`**, **`caller_retry_count`** (directly preceding model calls in the same trace, by the same agent for the same requested model, that ended with HTTP `429`), **`state_mode`**, **`runtime_lane`**, **`traffic_type`**, **`traffic_type_attested`**, **`tool_definitions_sha256`**, **`available_tool_count`**, **`selected_tool_names`**, token counts, latency, TTFT, finish reason, HTTP status, `content_missing`, and confidence/cascade facts. Non-LLM time-series/GPU workloads report to a sibling metric table rather than `model_calls`.

## `tool_calls`

Projects `execute_tool`, `retrieval`, and `memory_*` spans enriched with SCD-2 `tools` flags (`writes_state`, `modifies_workspace`, `executes_code`, `sends_external_reply`, `returns_pii`, `creates_ticket`), carrying `authz_mode` (`parameter_matched | token_enforced`), execution-time RFC 8785 `args_sha256` + `args_hash_attested`, `content_missing`, retrieval & memory lineage (`data_source_id`, `corpus_digest`, `acl_dropped_count`, `memory_op`, `memory_id`, `memory_sha256`, `memory_source_provenance`), warehouse compute (`db.query.job_id`, `bytes_scanned`, `bytes_billed`, `slot_ms`), and DevAI/container signals (`devai.diff_stats`, `devai.test_summary`, `devai.sast_findings`, `sandbox.cpu_sec`, `sandbox.mem_gb_sec`). Server tool spans fold into client parents (or via proxy `mcp.session.id`). `tool_calls_open` never scans restricted Shape-3 buckets and carries no `args_sha256`: it publishes `repeat_number`, a dense rank of the argument hash inside the trace in order of first use, so equal calls share a number and nothing built from the hash leaves the restricted dataset (`ADR-038`). `args_hash_source` says where the hash came from: the tool proxy's span, Silver's own hash of the arguments on the tool span, or none, in which case the repeat number is empty; the model's message is never used (`ADR-041`).

## `events` & `outcome_links`

`UNION ALL` of the three VPC-SC-isolated copies of `workflow_events` (the system-of-record copy sets `is_authoritative = true`; the platform copy, written only by gateways, safety filters, identity proxies and tool proxies, sets `source_class = 'platform'`; the application/IDE copy sets neither, incl. standard Git/CI webhook events), closing spans (`outcome.closed`), structured `guardrail.intervention` (`control_id`, `control_layer`, `control_action`), `state.transition` (`Guide | Answer`), `model.approved`, SDLC events (`code.accepted`, `code.reverted`, `test.failed`, `sast.blocked`, `pr.abandoned`), and OTel `gen_ai.evaluation.result` (`_AllLogs` `labels` JSON, carrying optional `nudge_id`). Approval and committed-write rows carry the same `action_args` object (the tool name plus the arguments as the tool received them), from which Silver computes both hashes via a payload-byte-guarded BigQuery UDF (`ADR-041`). A `guardrail.intervention` counts as platform-verified when it sits on a platform-class span or in the platform copy; one from an application's own rule stays unverified. Rows that repeat an `event_id` within one copy keep the first by `event_time`, with a deterministic tie-break; a repeat with different content is counted as a conflict, an identical one as a duplicate. `events_open` retains `nudge_id`, `control_id`/`layer`/`action`, and `feedback_reason` while omitting `action_args`, the approval and commit hashes, `payload_json`, and raw IDs. `outcome_links` projects `(primary_outcome_id, linked_outcome_id)` pairs from `outcome.linked` rows for M:N Gold `CPVO`.

## `trace_payloads`

One row per content-bearing trace (= 1 turn) in `silver_restricted` splitting `prior_history` from this turn's delta (`turn_prompt`, `turn_context`, `turn_output`, `step_outputs` with `nudge_id`, `media_uris` with `document_bytes`/`page_count`/`attachment_sha256`), plus optional `system_instructions`/`tool_definitions` and per-part `read_whole` flags. Stateful `previous_interaction_id` delta turns leave `prior_history = NULL` in Silver for bounded Gold session reconstruction.

## `traces`

Groups spans per `trace_id` with SCD-2 enrichment (`app_registry`, `tools`, `skill_registry`); a span's application is found through the telemetry identities in `app_registry` (an App Hub id, or a service name plus environment; `ADR-040`):
1. Caller/desk `trace_owner_app_id` (taking precedence over shared proxy root service names), `app_identity_bound` (the span landed where `app_registry` says), `root_span_id` (the first span by start time that an application emitted), `is_turn` (at least one agent, model-call or tool span), `has_root_span`, 3-principal identity with the root span's `user_id` and `user_id_source` (`platform | application`), `identity_attested` derived from the class of the span that carried the id, OBO claims (`auth_method`, `issuer`, `aud`, `delegation_chain_hash`), `workspace_session_id`, `previous_interaction_id`, `state_mode`, `runtime_lane`, live A/B keys (`shadow_of_trace_id`, `experiment_id`, `variant_id`), optimizer lineage, and `tool_definitions_sha256`.
2. Counts (spans, model/tool calls, `mutating_tool_call_count`, `workspace_edit_count`, `unregistered_tool_count`, `guardrail_intervention_count`, `content_missing_call_count`, `user_turn_count`, `duplicate_span_count`, repeated `(tool_name, args_sha256)` calls, errors, throttles) and token totals.
3. Completeness: `trace_start_time` with `trace_start_writer`, `missing_parent_count` (orphans other than server and consumer spans), `entry_parent_outside`, `exceeds_read_margin` (`ADR-037`), `sampling_probability` staying `NULL` when absent, and last span arrival time.

## `outcomes`

Built over seen `outcome.id` keys across `events`, `traces`, and closing spans, publishing open and closed outcomes (`is_closed`), `min_model_confidence` (default safety rollup) and `final_model_confidence`, error/failure/rootless counts, and closing trace. **`outcome.subject` lives strictly in restricted `outcomes` (`silver_restricted`) and is omitted from `outcomes_open` (`silver_open`)** to prevent MNPI/business-key leakage.

## Access (multi-project split datasets, split SAs & RAPs)

Restricted and open views live in **two physically separate datasets** (`silver_restricted` and `silver_open`) supporting multi-project Bronze/Silver/Gold estates with independent IAM and location-matched CMEK keys:
- `silver_restricted`: `trace_payloads`, `traces`, `tool_calls`, `events`, `outcomes` — protected by **per-BU / per-tenant Row Access Policies (RAPs)**.
- `silver_open`: `traces_open`, `tool_calls_open`, `events_open`, `model_calls`, `outcomes_open`, `outcome_links` — zero text, zero raw ids, zero business keys, zero argument hashes or file digests, zero Shape-3 bucket scans.
- In `light` mode, both are Authorized Datasets (`targetTypes: ["VIEWS"]`) on Bronze `_AllSpans`/`_AllLogs`, with per-BU BigLake Connections for Shape 3. In the design-only `enterprise` profile, **two separate least-privilege SAs** would populate `silver_open` vs. `silver_restricted` so no single SA bridges both tiers.

## Reference tables & Two-Speed CI/CD Registry Port

`app_registry`, `tools`, and `skill_registry` enforce mandatory SCD-2 validity windows on `RL-1`–`RL-3` and `regulated_worm` workloads, fed by the **Two-Speed CI/CD Dual-Registry Ingestion Port** (automated GitOps sync for additive `RL-3`–`RL-5` manifests; dual-signed `CODEOWNERS`/MRM gates for `RL-1`/`RL-2` promotions, `protected_block_sha256` edits, and capability flag downgrades) plus cloud-registry dark-agent reconciliation.

## Columns and labels

Values an add-on only slices by are projected into one `labels` JSON column from a registered key list, so a new slicing need adds a key, not a column; the rule governs over earlier lists of core columns (`ADR-039`).

## One implementation & Local/CI Eval Runner Port

BigQuery SQL is the single Silver implementation. The offline translator serves both repo verification proofs and the first-class **`< 5s` Local/CI Eval Runner Port** (`CI_ONLY`) running Silver SQL + deterministic Gold `Rule 0` over local OTLP traces for PR gates (`UC8`) and optimizer sweeps (`UC6`). When the translator and BigQuery disagree, BigQuery is right.
