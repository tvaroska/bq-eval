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

Every Silver object is published through **one logical SQL contract** over Bronze sources and the reference tables. The default (`light`) deployment publishes each object as a table function of an explicit time window, with no stored data and no scheduled job ([Read windows](#read-windows), `ADR-035`); in these notes "view" means such an object, and fixed-window views are thin wrappers. The high-volume (`enterprise`) profile is design only. `spans` is the internal single-pass base; Gold add-ons read the six published views (plus the `outcome_links` bridge view for M:N correlation) built on top of it (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`, `outcome_links`).

| Object | One row per | Holds |
|---|---|---|
| `spans` *(internal base)* | span | The normalized span, the telemetry shape it came from, and the application the span itself belongs to. |
| `model_calls` | model or embedding call (no text) | `span_id`, `parent_span_id`, `client_request_id` (`x-client-request-id`), `response_id` (`gen_ai.response.id`), `outcome_id`, `trace_owner_app_id`, `operation_name` (`chat \| generate_content \| embeddings`, or the name on any other span that reports tokens), call start/end times, agent, skill (`skill_name`, `skill_version`, `skill_sha256`), `request_model`, `response_model`, provider, `upstream_endpoint_location`, `runtime_lane`, `state_mode`, serving tier, `traffic_type`, `traffic_type_attested`, `thought_signature_present`, `caller_retry_count`, `tool_definitions_sha256`, `available_tool_count`, `selected_tool_names`, tokens, latency, time to first chunk, finish reason, status, `content_missing`, and confidence/cascade facts. |
| `tool_calls` | tool, retrieval, or memory call | `span_id`, `parent_span_id`, `outcome_id`, `trace_owner_app_id`, `operation_name` (`execute_tool \| retrieval \| memory_*`), call start/end times, agent, skill, tool name, status, duration, idempotency key, `action_id`, `args_sha256` (binding `tool_name` + executed args), `args_hash_source`, `args_hash_attested`, `authz_mode` (`parameter_matched \| token_enforced`), retrieval/memory lineage (`data_source_id`, `corpus_digest`, `acl_dropped_count`, `memory_op`, `memory_id`, `memory_sha256`, `memory_source_provenance`), downstream warehouse compute (`db.query.job_id`, `bytes_scanned`, `bytes_billed`, `slot_ms`), SDLC/container compute (`devai.diff_stats`, `devai.test_summary`, `devai.sast_findings`, `sandbox.cpu_sec`, `sandbox.mem_gb_sec`), arguments and results (restricted only), `content_missing`, sandbox facts, and flags from `tools`. |
| `events` | lifecycle, guardrail, SDLC, or workflow event | Outcome transitions (incl. `outcome.linked` for M:N correlation), structured `guardrail.intervention` (`control_id`, `layer`, `action`), `state.transition` (`Guide \| Answer`), `model.approved` (MRM release sign-off), committed writes (`action.committed` with `committed_args_sha256`), SDLC events (`code.accepted`, `code.reverted`, `test.failed`, `sast.blocked`, `pr.abandoned`), human handling, approvals with approved arguments (`approved_expires_at`), and operational human signals (thumbs with `feedback_reason`, `nudge_id`, routed reviews), each marked with `source_system`, `source_class` (`system_of_record \| platform \| application`, set by where the row landed) and `is_authoritative`. Blind SME calibration labels live strictly in Gold. |
| `trace_payloads` | trace (= 1 turn) with content | `prior_history` (Turns `1..k-1`), plus this turn's `turn_prompt`, `turn_context`, `turn_output`, `step_outputs` (preserving per-nudge `nudge_id` where emitted), `media_uris` (with `document_bytes`, `page_count`, `attachment_sha256`), optional `system_instructions` / `system_instructions_ref` and `tool_definitions` / `tool_definitions_ref`, and for each text part whether it could be read whole. |
| `traces` | trace | Owning application (`trace_owner_app_id`), `app_identity_bound`, `root_span_id`, `is_turn`, `has_root_span`, `trace_start_time` with `trace_start_writer` and the other completeness facts ([Completeness](#completeness)), 3-principal identity (`trigger_principal`, `agent_principal`, requester `user_id` as seen with `user_id_source`, `tenant_id`, optional `act_chain`, `identity_attested` derived from provenance, OBO claims `auth_method`, `issuer`, `aud`, `delegation_chain_hash`), conversation, `workspace_session_id`, `previous_interaction_id`, `state_mode`, `runtime_lane`, `shadow_of_trace_id`, `experiment_id`, `variant_id`, start/end times, turn starter, `case_id`, `eval_run_id`, `eval_split`, `skill_sha256`, `protected_block_sha256`, `mounted_skills`, `tool_definitions_sha256`, `page_count`, `document_bytes`, `final_model_confidence`, `escalated`, counts (incl. `mutating_tool_call_count`, `workspace_edit_count`, `unregistered_tool_count`, `guardrail_intervention_count`, `content_missing_call_count`, `user_turn_count`, `duplicate_span_count`), token totals, facts, missing-field counts, completeness, and last span arrival time. |
| `outcomes` | business outcome (open or closed) | `is_closed`, open time, closing facts when closed, `outcome.subject` (restricted `outcomes` only; omitted from `outcomes_open`), `case_id`, `eval_run_id`, `page_count`, `min_model_confidence`, `final_model_confidence`, `escalated`, its traces, root-error/failure/rootless counts, closing trace, and last span arrival time. |
| `outcome_links` | M:N `(primary_outcome_id, linked_outcome_id)` pair | Published bridge view projected from `events` (`outcome.linked`) enabling M:N incident/case correlation and Gold `CPVO` allocation (`UC7`) without overloading primary span `outcome.id`. |

Four views hold text, raw ids, or business keys next to other columns. Each has an open twin without them: `traces_open`, `tool_calls_open`, `events_open`, and `outcomes_open` (see [Access](#access)). The open twins carry no hashes of arguments and no file digests, only a repeat number per trace ([Hashes](#hashes-rfc-8785-jcs)). Non-LLM time-series or raw GPU inference workloads use a separate sibling metric table rather than polluting LLM token semantics in `model_calls`.

## Reference tables & Two-Speed CI/CD Registry Port

| Table | One row per | Holds |
|---|---|---|
| Application list (`app_registry`) | application (and optional `agent_id`), versioned by `effective_from` / `effective_to` | Telemetry identities that map to it (an App Hub id, or a service name plus environment), the landing places its spans must arrive in, owner, business unit, environment, `risk_tier` (`RL-1`..`RL-5`), `payload_profile` (`ephemeral_sample \| regulated_worm`), `sampling_mode` (`full \| sampled`), `barrier_domain` (MNPI / information-barrier clearance domain), `runtime_lane`, and optional MRM governance (`mrm_model_id`, `validation_expiry_date`). |
| Tool list (`tools`) | application and tool (versioned by `effective_from` / `effective_to`) | Factual capability flags only: `writes_state` (system-of-record mutation), `modifies_workspace` (local dev/sandbox file edit — never triggers SoR HITL SoD checks), `executes_code`, `sends_external_reply`, `returns_pii`, `creates_ticket` (approval thresholds and rules live in Gold). |
| Skill list (`skill_registry`) | application, skill, and version (versioned by `effective_from` / `effective_to`) | Authoritative pinned `skill_sha256` and immutable `protected_block_sha256` compliance-block digest (so automated optimizers and applications never self-certify compliance hashes). |

- **Mandatory SCD-2 for regulated workloads:** SCD-2 validity windows (`effective_from`, `effective_to`; `NULL` end means active) joined at span/trace time are **mandatory** for `RL-1`–`RL-3` and `regulated_worm` workloads so edits never rewrite historical audits (non-regulated `RL-4`/`RL-5` apps may leave windows open). Gold additionally snapshots reference values with every stored result.
- **Two-Speed CI/CD Dual-Registry Ingestion Port (`ADR-032`):** To support high-velocity (`300x/day`) GitOps delivery without sacrificing `SR 11-7` control, Silver defines a GitOps **CI/CD Dual-Registry Ingestion Port** syncing manifest artifacts (`agent-card.json`, `toolspec.json`, `SkillRevision`) into `app_registry`, `tools`, and `skill_registry`:
  - *Fast lane (`RL-3`–`RL-5` additive changes):* Automated GitOps sync registers new low-risk applications, read-only/workspace tools, and candidate skills.
  - *Governed lane (`RL-1`/`RL-2` & capability downgrades):* Any `RL-1`/`RL-2` promotion, `skill_registry.protected_block_sha256` change, or tool-capability downgrade (`writes_state`, `returns_pii`, or `sends_external_reply`: `true -> false`) requires dual-signed `CODEOWNERS` / MRM approval — never an automated application write.
- **Federated Registry Sync (dark-agent detection):** Reconciles `app_registry` against cloud agent registries (`AWS`, `GCP`) so Gold flags both uninstrumented registered agents ("dark agents" emitting zero telemetry) and unregistered emitting workloads.

Capacity pools, capacity allocations, and the model allow-list belong to the [capacity and governance extension](capacity.md). A user directory is not a Silver table; Gold cost joins whatever directory the deployment provides.

## Rules

### Telemetry comes in at most three shapes

Silver reads three shapes of telemetry in BigQuery SQL and no others. Every shape carries content, on the step or by reference ([shapes.md](shapes.md#content-is-required)). The shape says how content is read; the kind of a span (model call, embedding, tool call, retrieval, memory, agent, other) is read from its operation name, and a span that reports tokens above zero is a model call whatever that name is (`ADR-042`). Spans with no GenAI operation and no tokens are kept and counted as ordinary spans, never flagged. A model or tool call without content is kept: it stays in the text-free objects with a content-missing fact (`content_missing`), has no text row, is not scored by evaluation, and is counted by cost and monitoring (`ADR-036`). Non-conforming frameworks, coding SDKs, and custom gateways normalize upstream through the Bronze / OTel Collector Adapter Port ([shapes.md](shapes.md)). Standard OpenTelemetry names, `EVAL-P11` `outcome.*` keys, and minimal `bq_eval.*` attributes are specified in [attributes.md](attributes.md).

### Reference data enriches and flags; it never drops

Every trace gets its owner, unit, environment, `risk_tier`, and `barrier_domain` from `app_registry`, every tool call gets its flags from `tools`, and every skill gets its pinned digests from `skill_registry`. A missing lookup is published as a fact (`unregistered_app`, `unregistered_tool_count`, `unregistered_skill`). Every row stays; each Gold add-on decides what a fact means.

### Counts and facts, no pass

Silver does not decide whether a trace passed. It publishes counts and facts only, and each add-on defines its own pass ([gold.md](gold.md)). Missing values stay `NULL` and are counted — never defaulted. An application's self-reported failure (`bq_eval.app_failed = true`) is published as a fact; a self-reported pass or "outcome verified" is never read.

### Columns and labels

A value gets its own Silver column only if Silver computes with it, or it defines a row's identity, a join or access. Everything an add-on only slices by travels in one `labels` column whose allowed keys are registered (`ADR-039`). This rule governs: any earlier list of core columns is provisional until the column files settle which of today's columns move to `labels` ([open-questions.md](open-questions.md)).

### Provenance and verified facts

A record's writer class is set by where it landed, never by what it says about itself (`ADR-034`).

| Class | Lands in | Examples |
|---|---|---|
| `system_of_record` | the copy of the input table only systems of record may write | approvals, committed writes, releases |
| `platform` | a trace store, or a copy of the input table, only the deployment's own controls may write | gateway, identity proxy, tool proxy, safety filter |
| `application` | everything an application can write | agent spans, application events, feedback |

`identity_attested`, `args_hash_attested` and `traffic_type_attested` are true only for facts read from platform-class records, and `is_authoritative` only for system-of-record rows. An attribute that claims to be verified is ignored. A deployment that needs verified facts gives gateway and tool-proxy spans a landing place applications cannot write to. The trace store has no column that records who wrote a span (measured), so the landing place is the only source of the class ([open-questions.md](open-questions.md)). The cloud runtime's own spans (its load balancer and front end) are not platform-class.

An application's identity is bound the same way (`ADR-040`): `app_registry` lists the landing places an application's spans must arrive in, and an identity claimed from anywhere else is published as not bound (`traces.app_identity_bound = false`). Where a rule depends on the tier, Gold treats unbound and unregistered applications as the strictest tier. Inside one landing place identity stays self-declared.

### Row rules

- **3-principal identity, structured OBO claims & provenance.** Every trace records `trigger_principal` (`human | event | automated`), `agent_principal`, requester `user_id` / `tenant_id` (plus optional `act_chain`), and structured OBO attestation (`identity_attested`, `auth_method`, `issuer`, `aud` per RFC 8787, `delegation_chain_hash`). **Silver publishes the `user_id` it saw**, the root span's `user.id` (`ADR-025`), with `user_id_source` (`platform | application`) naming the class of span that carried it. `identity_attested` is true only when a platform-class span carried it, a fact derived from provenance ([Provenance](#provenance-and-verified-facts)) — never read from an attribute. Whether an unverified id counts is decided in Gold per risk tier (`ADR-034`). Because the root is the first application span, an application has to stamp `user.id` on its entry span or `user_id` stays empty. Disagreeing `user.id` spans are counted. Where a platform-class span elsewhere in the trace carries a different id, Silver does not choose: which id is the requester is decided in Gold (`ADR-034`), and what Silver has to publish for that choice is settled with Gold's rule ([open-questions.md](open-questions.md)).
- **Ownership (`trace_owner_app_id` precedence over central proxies).** Where a shared multi-tenant LLM gateway or proxy sits at the trace root, caller/desk entitlement attribution (the caller's application as recorded on the platform-class gateway span) takes precedence over the proxy's own service name so multi-tenant gateway traffic never collapses under one proxy app ID. Otherwise the application of the root span owns the trace; participating app count is recorded. A span's application is found through the telemetry identities in `app_registry` (an App Hub id, or a service name plus environment); no match stays unregistered (`ADR-040`).
- **Agent of a call.** A model or tool call belongs to the agent named on the call itself, else the nearest enclosing agent span.
- **Span deduplication & tool pairing.** `spans` holds one row per `(trace_id, span_id)`, the latest arrival, and the rest are counted as duplicates (`ADR-042`). A server tool span folds into a client tool span only when it is that span's child (or linked via `mcp.session.id` through a proxy-mediated W3C parent chain — never by name and arguments). In legacy ADK traces where `call_llm` wraps a child `generate_content` span with duplicate token usage, the pair folds into one `model_calls` row. The general rule: where a span that reports tokens encloses another one, only the innermost is a model call, and the enclosing span's tokens are a rollup that is never summed (`ADR-042`).
- **Turn prompt & stateful delta turns.** `turn_prompt` is the last input before the first model output of the turn, whatever its role. For stateful APIs carrying `previous_interaction_id`, `prior_history` stays `NULL` in Silver (without miscounting missing history), while Gold performs bounded cross-turn session-context reconstruction where required ([gold.md](gold.md#1-evaluation-add-on)).
- **Reply text & multi-output steps.** `turn_output` is the arguments of the last tool call with `sends_external_reply = true`, else the last model output under the root agent that asked for no tool. Multi-part structured outputs and per-nudge outputs remain inspectable in `step_outputs` keyed by `nudge_id` where emitted.
- **Model-call ids, models and retries.** `model_calls` keeps the client request id and the provider response id in two columns, and the requested and the responding model in two columns; nothing is merged. `caller_retry_count` is, for one model call, the number of directly preceding model calls in the same trace, by the same agent for the same requested model, that ended with HTTP `429`. A split of cache-write tokens by lifetime is not published until an attribute carries it (`ADR-039`).
- **Confidence rollups.** `model_calls` retains per-call `model_confidence`. `traces` exposes `final_model_confidence` (last call reporting confidence), and `outcomes` publishes **both `min_model_confidence` (default for multi-trace/map-reduce safety gating) and `final_model_confidence`**.

### Outcomes & M:N correlation

- `outcomes` publishes one row per seen `outcome.id` across `events`, `traces`, and closing spans (`outcome.closed`), whether open or closed (`is_closed`).
- Each trace carries at most one primary `outcome.id` on spans (multiple primary `outcome.id` values on one trace are flagged). Workloads requiring M:N correlation (e.g., one SRE investigation spanning multiple incidents in `UC7`) link secondary outcomes via `outcome.linked` rows in `events`, projected through the published `outcome_links` bridge view.
- Waiting times are not summed in Silver, and Silver never declares an outcome verified.

### Text & Shape 3

A turn is a trace with at least one agent, model-call or tool span (`traces.is_turn`); other traces, such as page loads, are kept and are not turns (`ADR-037`). One turn is one `trace_id` (`gen_ai.conversation.id` links multi-turn sessions; `outcome.id` links multi-trace cases); `traces.user_turn_count` above 1 marks a trace that holds more than one user turn. `trace_payloads` splits `prior_history` from this turn's delta (`turn_prompt`, `turn_context`, `turn_output`, `step_outputs`, `media_uris`), plus `read_whole` flags per part.

In Shape 3 (content by reference), storage sits behind a **BigLake table (`WITH CONNECTION`)** over Hive date/hour partitions (`dt=YYYY-MM-DD/hr=HH/`, micro-batched Parquet/Iceberg preferred at scale) with static window predicates — never a plain external table and never an unpruned dynamic `_FILE_NAME` join. **Open twins (`*_open`) never scan restricted Shape-3 payload buckets.**

### Hashes (RFC 8785 JCS)

`args_sha256`, `approved_args_sha256`, and `committed_args_sha256` use **RFC 8785 (JCS)** over `{"tool_name": <tool_name>, "args": <arguments>}`. The hashed object is the tool name plus the arguments exactly as the tool received them when it ran; the approval service stores that same object, and the model's raw text is not used (`ADR-041`). The tool-side hash names its source in `args_hash_source` (`proxy | silver | none`): the execution-time `bq_eval.args_sha256` of a platform-class tool-proxy span, which sets `args_hash_attested = true`; or Silver's own hash of the arguments on the tool span; or none. It is never computed from the model's message, and a write without a tool-side hash cannot pass Gold's approval check. Every implementation must pass one shared set of test cases that is part of the contract. The open twins never carry these hashes or file digests: `tool_calls_open` publishes `repeat_number`, a number per trace that is equal for calls with the same tool name and arguments, and nothing built from hashes; the hashes stay in `silver_restricted`, and repeats across an outcome are counted in Gold from the restricted views (`ADR-038`). Silver's own hashing uses a deterministic BigQuery UDF guarded by a payload-byte ceiling so malformed/oversized inline JSON never exhausts query slots.

### Completeness

- The root of a trace is the first span, by start time, that an application emitted; a gateway or proxy at the entry counts as an application here, the cloud runtime's own spans (its load balancer and front end) do not (`ADR-037`). `traces.root_span_id` names it, and `has_root_span` is false only when no application span is stored. While a trace is still arriving its entry span may not be stored yet, so the root is final once the trace has settled. `trace_start_time` is the start of the earliest stored span, with `trace_start_writer` (`runtime | application`) saying who wrote it.
- Silver publishes separately why a trace is incomplete: `missing_parent_count` (a parent that should be in the store is not, for example a span rejected for size); `entry_parent_outside` (the root span has a parent that is not stored: the caller or the runtime's front end); `exceeds_read_margin` (the trace is longer than the margin read around the window); and the last span arrival time, from which Gold tells a trace that is still running. A server or consumer span whose parent is not stored is normal at the entry of a trace and at every service hop, so it is not counted as a missing parent; any other orphan is. A lost span whose only children are server spans in another service therefore stays invisible to these facts; the reverse commit check covers writes (measured, [open-questions.md](open-questions.md)).
- `traces` records `tool_call_count`, `mutating_tool_call_count`, `workspace_edit_count`, `unregistered_tool_count`, `content_missing_call_count`, `user_turn_count` and `duplicate_span_count` whatever the root facts say.
- `traces` reads `sampling_probability` from W3C `trace_state` else `bq_eval.sampling_probability`; **missing stays `NULL`, never `1.0`**; Gold does not scale applications the registry declares `full` (`ADR-043`).
- Gold never silently drops traces without a root that contain tool calls or `writes_state` mutations; its safety alert uses `missing_parent_count` only, once the trace has settled. Gold also reconciles authoritative `action.committed` rows back against `tool_calls` to catch `65,533+ B` OTLP dropped spans.

### Read windows

BigQuery does not push outer filters past `GROUP BY` (`37x` scan penalty measured live).

- **Explicit window (`light`, `ADR-035`):** every published object is a table function of a from-time and a to-time, with no stored data. The window filter sits inside `spans`, with matching static Hive `dt` predicates on BigLake Shape 3.
- **Whole turns only:** a trace belongs to the window in which its first span starts. Silver reads a margin before and after the window so no member trace is cut; a trace longer than the margin is flagged (`exceeds_read_margin`).
- **What is repeatable (`ADR-035`):** a window has settled when its end, the read margin and the settle time have passed. From then on the same call returns the same telemetry columns. Registry columns are repeatable only where validity windows exist; elsewhere they are as known at read time, and Gold stores the values it used. `events` and `outcomes` are as known at read time. Fixed-window views (for example the last 2 hours) are thin wrappers over the functions.
- **Untested:** that a window passed as function arguments limits scanned bytes as a filter inside a view does, and that the functions can be authorized on Bronze like the views. How `outcomes`, which can span days, relate to a window is open ([open-questions.md](open-questions.md)).
- **High-volume profile (`enterprise`, design only, `ADR-035`):** not decided. The candidate stores only a typed, text-free copy of spans, appended as they arrive, and keeps the published objects as functions on top, so both profiles run the same SQL and text is never copied. The earlier candidate, one incrementally merged table per published object, stays on record until a side-by-side test chooses: it runs every `5–15` minutes in an isolated slot reservation over `(receive_time_watermark - settle_buffer .. now)` into `DATE(start_time)`-partitioned tables clustered by `(COALESCE(trace_owner_app_id, '__unowned__'), trace_id)`. **Mandatory partition-pruning contract (`ADR-031`):**
  1. Every `MERGE` statement **must include a static target partition-prune predicate** (`target.partition_date >= DATE_SUB(CURRENT_DATE(), INTERVAL merge_lookback_days DAY)`) in the `ON` clause so late-arriving or midnight-straddling spans never rewrite multi-TiB historical partitions.
  2. `outcomes` and Gold session rollups enforce explicit bounded lookback windows (configurable `outcome_lookback_days` / `session_inactivity_hours`) rather than scanning full history.
  3. All compaction jobs enforce `max_bytes_billed` and reservation slot circuit-breakers ([gold.md](gold.md#shared-rules)).

### Access (multi-project split datasets, split SAs & RAPs)

Restricted and open objects live in **two physically separate datasets** (`silver_restricted` and `silver_open`, supporting multi-project Bronze/Silver/Gold landing-zone separation) with independent IAM and location-matched CMEK keys:

- `silver_restricted` holds `trace_payloads`, `traces`, `tool_calls`, `events`, and `outcomes` (including `outcome.subject`, text, arguments/results, raw `user_id`/`tenant_id`), protected by **per-business-unit / per-tenant BigQuery Row Access Policies (RAPs)** so multi-LOB payloads are never co-mingled across readers.
- `silver_open` holds `traces_open`, `tool_calls_open`, `events_open`, `model_calls`, `outcomes_open` (zero `outcome.subject`), and `outcome_links` — zero text, zero raw ids, zero business keys, zero argument hashes or file digests, zero joins to restricted Shape-3 payload buckets.
- In `light` mode, both datasets are **authorized datasets** (`targetTypes: ["VIEWS"]`) on Bronze `_AllSpans`/`_AllLogs`, and `silver_restricted` reads Shape 3 strictly via a per-BU-scoped **BigLake Connection** with VPC-SC egress/CMEK verification.
- In the design-only `enterprise` profile, compaction would run under **two separate least-privilege service accounts** (a restricted-tier SA writing `silver_restricted` and a text-free open-tier SA writing `silver_open` from text-free projections) so one compromised job never bridges both tiers.

## Who may read what

| Reader | May read |
|---|---|
| Gold Evaluation add-on (in-cloud checks & judges) | `silver_restricted` (subject to BU/tenant RAPs), `silver_open`, and SCD-2 reference tables (`app_registry`, `tools`, `skill_registry`) |
| Gold Monitoring & Cost add-ons (default least privilege) | `silver_open` (`traces_open`, `tool_calls_open`, `events_open`, `model_calls`, `outcomes_open`, `outcome_links`) and reference tables; `silver_restricted` only where an explicit workload rule requires raw `user_id`/`tenant_id`, or counts repeated calls across an outcome, inside the data's home cloud |
| Anything outside this repository | Nothing by view name (sibling tools consume versioned Gold export contracts) |

## One implementation & Local/CI Runner Port

The SQL that ships for BigQuery is the single implementation of Silver. Offline runs — both repo verification gates and the first-class **`< 5s` Local/CI Eval Runner Port** (`CI_ONLY`, [layers.md](layers.md)) — execute that exact SQL through a deterministic translator over local OTLP trace fixtures. When the translator and BigQuery disagree, BigQuery is right.

## Proof

Each object has a schema file (column, type, meaning, nullability) and fixed recorded-trace inputs with exact expected rows. No telemetry column depends on the time of reading once its window has settled; registry columns, `events` and `outcomes` are as known at read time ([Read windows](#read-windows)). Verification runs offline on every gate and on live BigQuery in build/release pipelines.
