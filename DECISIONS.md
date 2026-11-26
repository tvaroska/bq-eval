# Architectural Decisions (`DECISIONS.md`)

## ADR-001: Repository Separation & Cross-Repo Boundaries (`bq-eval` vs. `agent-evals` vs. `capacity`)
- **Decision**: Keep three repositories decoupled behind versioned **Ports, Adapters and Contracts**: (1) `bq-eval` owns Bronze/Silver SQL + Gold evaluation, monitoring, and cost add-ons; (2) `agent-evals` owns normative `EVAL-P01`–`EVAL-P11` standards, MRM playbooks, and `eval-audit`; (3) `capacity` owns Newsvendor sizing, sub-minute burst physics, gateway routing/shedding policies, and billing actuation over exported `bq-eval` payloads (never naming `bq-eval` views directly).

## ADR-002: Progressive Zero-Table Step 1 -> Step 3 View Ladder *(Superseded by `ADR-010`)*
- **Summary**: Replaced by price-free Silver core + Gold add-ons.

## ADR-003: Cross-Cloud Collector Forwarding & `'unregistered_app'` Defaults *(Superseded by `ADR-010`)*
- **Summary**: Replaced by in-cloud residency and `NULL` for missing fields.

## ADR-004: Fail-Closed Rogan-Gladen Calibration, Rule Pre-Screening & Gwet's `AC2` *(Amended by `ADR-030`, `ADR-033`)*
- **Decision**: Run deterministic SQL gates before LLM judging; sample `5–10%` passing baseline + `100%` passing slow/throttled tails; report conditional ($\hat{\theta}_{\text{RG}}$) and unconditional pass rates over rule-passing judged traces (billing `100%` of judge runs); fail closed to `NULL` on empty strata, missed floors, or $J / J' \le 10^{-6}$; pair Quadratic `AC2` with 95% CIs (`ADR-033` adds stratum-specific $Se_h/Sp_h$, joint CIs, and binary cut-point agreement).

## ADR-005: Portable, Customer-Agnostic Repository Hygiene
- **Decision**: Enforce AST and text hygiene gates (`tests/test_repo_hygiene.py`, `tests/test_isolation_guard.py`) blocking internal codenames, shortlinks, bug IDs, workstation paths, `file://` URIs, `.github/workflows/`, escaping links, and forbidden upward imports.

## ADR-006: Outcome-Adjusted Cost, `>200K` Cliffs & Zero-Fabrication Model Selection *(Amended by `ADR-032`)*
- **Decision**: Separate spend into 6 buckets (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`, `optimize`, `unclassified`), price each call first (`>200K` cliffs on `input + cache_read`), divide total spend (incl. failures, escalations, `100%` judge spend) by verified outcomes, enforce `0.0%` negative flips (`UC5`), and evaluate cascades (`UC4`) on combined cost above the calibrated floor.

## ADR-007: Dedicated Capacity & `FOCUS 1.2` Chargeback *(Amended by `ADR-028`, `ADR-032`)*
- **Decision**: Attribute dedicated capacity via `FOCUS 1.2` multi-pool rules (active-hours funded share, off-peak backfill, unallocated idle, Priority/Standard PayGo spillover, reconciled to Cloud Billing) and route batch evals to off-peak valleys.

## ADR-008: Dual-Path `BQAA` `agent_events` + `_AllLogs` Switchboard *(Superseded by `ADR-011`)*
- **Summary**: Replaced by the three shapes in [`specs/shapes.md`](specs/shapes.md).

## ADR-009: Hand-Written Offline SQLite Runtime Twin *(Superseded by `ADR-010`)*
- **Summary**: Replaced by translating the single BigQuery SQL implementation offline (`ADR-032` promotes it to the Local/CI Eval Runner Port).

## ADR-010: Bronze and Silver Core in BigQuery, Gold Add-Ons on Top *(Amended by `ADR-012`–`ADR-047`)*
- **Decision**: Core is Bronze (telemetry in place) + Silver SQL in BigQuery (zero prices, zero verdicts, `NULL` for missing, one-way trust on self-reported failures). Gold add-ons read only published Silver views. BigQuery SQL is the single Silver implementation.

## ADR-011: At Most Three Telemetry Shapes & Content by Reference *(Amended by `ADR-026`, `ADR-028`, `ADR-032`, `ADR-036`)*
- **Decision**: Silver reads at most three shapes in BigQuery SQL ([`specs/shapes.md`](specs/shapes.md)): legacy ADK, OTel GenAI inline, and OTel GenAI by reference (`file_data` URIs stay in place). Non-conforming frameworks, coding SDKs, and custom gateways normalize via the Collector Port.

## ADR-012: Table-Function Silver Default (`light`) with Optional Span Base *(Amended by `ADR-035`)*
- **Summary**: Default (`light`) publishes explicit-window table functions over Bronze; `enterprise` (design only) tests an append-only text-free span base against incremental tables.

## ADR-013: Typed Silver Views and `tools` Table *(Amended by `ADR-019`, `ADR-020`, `ADR-023`, `ADR-032`)*
- **Summary**: Established typed Silver views (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`) and factual `tools` flags; `requests` moved to capacity (`ADR-023`).

## ADR-014: Non-Blocking Tool-Call Counts in Silver; Loop Rules in Gold *(Amended by `ADR-020`)*
- **Summary**: Kept tool-call/repeat counts in Silver and thresholds/pass rules in Gold.

## ADR-015: Standard-First Telemetry Attributes (OTel + `EVAL-P11` + Minimal `bq_eval.*`) *(Amended by `ADR-039`, `ADR-045`)*
- **Decision**: Read OTel (`v1.45+`), OTel GenAI (`v0.1+`), MCP, A2A, and cloud attributes verbatim, preserve `EVAL-P11` `outcome.*`, and confine custom keys strictly to `bq_eval.*` ([`specs/attributes.md`](specs/attributes.md)).

## ADR-016: Turn-Scoped `trace_payloads` Delta Contract *(Amended by `ADR-020`, `ADR-027`, `ADR-045`)*
- **Summary**: One `trace_payloads` row per content-bearing turn splitting `prior_history` from turn deltas (`turn_prompt`, `turn_context`, `turn_output`, `step_outputs`, `media_uris`).

## ADR-017: In-Cloud Identity Residency & Zero Hot Stores *(Amended by `ADR-021`, `ADR-028`, `ADR-029`, `ADR-031`)*
- **Summary**: Raw identities stay in-cloud; no hot external databases; `>= 5m` BigQuery floor; out-of-band KMS-HMAC hashes for cross-cloud joins.

## ADR-018: Call-Level Join/Lineage Keys & Operational-vs-Calibration Boundary *(Amended by `ADR-028`, `ADR-032`)*
- **Summary**: Added call join keys; separated operational `events` feedback from Gold SME labels.

## ADR-019: Factual Tool Capabilities, Payload-Bound HITL Approvals & Scoped `capacity_pools` *(Amended by `ADR-028`, `ADR-029`, `ADR-032`)*
- **Summary**: Kept factual flags in `tools`, bound `outcome.approved` via `action_id` + hash, split capacity tables, added `capacity` exports.

## ADR-020: Silver Publishes Counts and Facts Only; Each Add-On Defines Its Own Pass
- **Decision**: Silver holds zero pass/threshold logic (`output_kind`, turn TTFT, wait sums, `is_settled`, slow flags live in Gold). Evaluation default: completed turn = root OK and not `app_failed`; verified outcome = every trace completed (or closing trace when retries expected).

## ADR-021: Read Windows, 5-Minute Floor and SLA Clocks from Trace Start *(Amended by `ADR-035`, `ADR-037`)*
- **Summary**: Explicit-window reads (`start_time` inside `spans`), Gold snapshots, `>= 5m` floor, SLA clock from trace start.

## ADR-022: Input Table for Non-Span Records, Authority by Table & RFC 8785 Hashes *(Amended by `ADR-034`, `ADR-041`)*
- **Summary**: Isolated `workflow_events` copies (authority by table); `action_args` hashed in Silver via RFC 8785 JCS.

## ADR-023: One-Domain Core; Clouds, Capacity and Governance as Modular Extensions *(Amended by `ADR-028`, `ADR-030`, `ADR-031`)*
- **Summary**: Modular core isolating multi-cloud ([`specs/clouds.md`](specs/clouds.md)) and capacity ([`specs/capacity.md`](specs/capacity.md)).

## ADR-024: Long Text by Reference, Completeness Facts, Reference Data & Access *(Amended by `ADR-026`–`ADR-031`, `ADR-045`)*
- **Summary**: Content by reference, per-part `*_read_whole`, tool-flagged `turn_context`, fail-closed `sampling_probability`.

## ADR-025: Six Row Rules *(Amended by `ADR-026`, `ADR-029`, `ADR-032`–`ADR-034`, `ADR-037`, `ADR-045`)*
- **Summary**: Reply text, confidence rollup (`min_model_confidence`), agent attribution, child/`mcp.session.id` fold, turn-prompt delta, root `user.id`.

## ADR-026: Split Open/Restricted Datasets & Single-Pass `spans` *(Amended by `ADR-031`, `ADR-035`, `ADR-038`)*
- **Summary**: Separate `silver_open`/`silver_restricted`; single-pass `SELECT` over `_AllSpans` folding ADK Format 1 `call_llm`+`generate_content`.

## ADR-027: BigLake Hive-Partitioned Shape 3 & Execution-Time RFC 8785 Hashing *(Amended by `ADR-036`, `ADR-038`, `ADR-041`, `ADR-045`)*
- **Summary**: BigLake (`WITH CONNECTION`) + Hive date/hour partitions with static predicates; async uploads; execution-time JCS `bq_eval.args_sha256`.

## ADR-028: `1:N` Gateway `requests`, SCD-2 Governance, Hub Transfer Port & Upstream Collector Port *(Amended by `ADR-031`, `ADR-032`, `ADR-046`)*
- **Decision**: Price each `1:N` `requests` attempt before aggregating to `model_calls` (separating `gateway_failover_attempts` from `caller_retry_count`); add `burst_metrics_1m` and SCD-2 validity windows; connect non-GCP open rows via tiered federation/transfer (`ADR-046`) with KMS-HMAC hashes and In-Cloud Judge Port; normalize non-conforming SDKs via the Collector Port.

## ADR-029: Fail-Closed Completeness, Attested 3-Principal OBO, Guardrail Separation & WORM Audit Snapshots *(Amended by `ADR-031`, `ADR-032`, `ADR-034`, `ADR-037`, `ADR-043`)*
- **Decision**: Keep `sampling_probability = NULL` fail-closed; alert on rootless mutating traces; fail closed on unregistered tools in regulated workflows; require write-IAM provenance for `identity_attested`/`args_hash_attested`; bind `tool_name` + executed args in RFC 8785 hashes; enforce SoD/TTL/single-use and `action.committed` verification; add `guardrail.intervention`, `rejected | denied`, and WORM audit snapshots.

## ADR-030: Typed Non-LLM Spans, Optimizer Gold Port, Dual-SME MRM Rigor & Enterprise Use Cases
- **Decision**: Project `embeddings` onto `model_calls`, retrieval/memory provenance + warehouse compute onto `tool_calls`, `system_instructions`/`tool_definitions` onto `trace_payloads`, optimizer lineage onto `traces`, and `outcome.linked` onto `events`; add Optimizer Gold Export Port, dual-SME adjudication, calibration expiry, PT eval isolation, and slot backpressure.

## ADR-031: Multi-Region Hub, Location Alignment, Multi-Project Security, Bounded `MERGE` & WORM/RTBF *(Amended by `ADR-035`, `ADR-044`, `ADR-046`)*
- **Decision**: (1) Default Hub to `US`/`EU` multi-region (or single region for regional Lakehouse + CCI, `ADR-046`) with strict per-domain location + KMS CMEK alignment (`specs/clouds.md`). (2) Keep `outcome.subject` restricted-only; split `enterprise` compaction SAs; enforce per-BU/tenant access (`ADR-044`, `ADR-047`); mandate static target `DATE(start_time)` pruning on `MERGE`, bounded lookbacks, and `max_bytes_billed` + `INFORMATION_SCHEMA.JOBS` slot breakers. (3) Export regulated Gold snapshots to PII-free **GCS Bucket Retention-Lock + CMEK** archives with per-subject RTBF DEKs.

## ADR-032: Preventive Port, Two-Speed Registry, `< 5s` Runner & Gateway/FinOps Hardening *(Amended by `ADR-034`, `ADR-039`, `ADR-045`, `ADR-046`)*
- **Decision**: (1) Gateways enforce live writes, DLP, `allowed_models`, `1s/10s` `429` shedding, daytime PT `optimize`, and `100 MB` streaming emitting `guardrail.intervention`; promote offline SQL translator to **`< 5s` Local/CI Eval Runner Port** (`CI_ONLY`). (2) Add SCD-2 `skill_registry` (`protected_block_sha256`), split `modifies_workspace` from `writes_state`, sync registries via Two-Speed CI/CD (`RL-1`/`RL-2` dual-signed) + dark-agent sync, require structured OBO + `authz_mode`, and check reverse `action.committed -> tool_calls`. (3) Mandate `x-client-request-id` + `(trace_id, span_id)` `1:N` joins, `trace_owner_app_id` precedence, `state_mode`, core `model_calls` columns, honest 3P `inferred_ambiguous`, 3-pool `FOCUS 1.2`, `burst_metrics_1m`, and extended `allowed_models` ([`specs/capacity.md`](specs/capacity.md)).

## ADR-033: `SR 11-7` Stratified Calibration, Stateful Memory, Guardrail FP/FN & 11-Workload Split
- **Decision**: (1) Stratum-specific $Se_h/Sp_h$ (and per-cloud $Se/Sp$) Rogan-Gladen + joint CIs + $[0,1]$ clamping; `AC2 >= 0.80` + binary cut-point agreement; `RL-1 >= 100` floors + SME tie-break; deterministic independent judges (`temperature = 0`, `seed`, snapshot ID, prompt hash, `zdr_allowed`); expanded `CALIBRATED` expiry; `min_model_confidence` default on `outcomes`. (2) Add `memory_*` lineage, `state.transition`, bounded session reconstruction (`MISSING_SESSION_CONTEXT`), PII-masked judge prompts, guardrail FP/FN, MNPI `barrier_domain` + empty-RAG alerts, `shadow_of_trace_id`, `nudge_id`, `outcome_links`, `db.query.job_id`/`JOBS` reconciliation, `T+1..30d` ground truth, and sibling non-LLM table (`UC1`–`UC11`).

## ADR-034: Silver Keeps What It Saw; "Verified" Comes From Provenance
- **Decision**: Silver publishes the user id it saw, the root span's, with `user_id_source`; Gold decides which id is the requester when a platform-class span carries a different one and, per tier, whether an unverified id counts. A record's writer class (`system_of_record | platform | application`) is set by where it landed, and `identity_attested`, `args_hash_attested` and `traffic_type_attested` are derived from that class, never read from an attribute. Regulated deployments give gateway and tool-proxy spans a landing place applications cannot write to. Control verdicts are platform-verified on such a span or in a third, platform-only copy of `workflow_events`. The cloud runtime's own spans are not platform-class.

## ADR-035: Explicit Read Windows; High-Volume Profile Is Design Only
- **Decision**: Every published object is a table function of a from-time and a to-time, with no stored data; fixed-window views are thin wrappers. A trace belongs to the window in which its first span starts, read with a margin so no turn is cut. A window has settled when its end, the margin and the settle time have passed: Gold keeps results from settled windows only, and only telemetry columns are repeatable from then on (registry columns need validity windows; `events` and `outcomes` are as known at read time). The `enterprise` profile is design only: the candidate stores a typed, text-free copy of spans under the same functions; a side-by-side test against merged tables decides.
- **Measured**: literal or parameter windows prune through a function; functions are authorized on Bronze one by one.

## ADR-036: Content Is Required; Calls Without It Are Kept, Not Scored *(Amended by `ADR-042`)*
- **Decision**: Shape routing stays keyed on content, and every shape carries content on the step or by reference. A model or tool call without content stays in the text-free objects with a content-missing fact: no text row, not scored by evaluation, counted by cost and monitoring. Silver defines no zero-payload profile. The kind of a span (model call, embedding, tool call, retrieval, memory, agent, other) is read from its operation name. Spans with no GenAI operation are ordinary: kept, counted, not flagged.
- **Rejected Options**: routing by name family; counting every unmatched span as unknown shape; excluding calls without content.

## ADR-037: Root, Turn and Clock
- **Decision**: The root of a trace is the first span, by start time, that an application emitted; the cloud runtime's own spans do not count. So the user id must be stamped on that span. A turn is a trace with at least one agent, model-call or tool span; other traces are kept and are not turns. The SLA clock starts at the earliest stored span, with a fact saying whether the runtime or the application wrote it. Silver publishes why a trace is incomplete (a parent missing from the store, a trace longer than the read margin, the last arrival time); Gold's orphaned-activity alert uses the missing parent only, once the trace has settled.

## ADR-038: Open Twins Carry No Hash Material *(Amended by `ADR-045`)*
- **Decision**: Argument hashes, approval and commit hashes and file digests are published in `silver_restricted` only: a hash of a tool name plus a guessable id can be reversed, and a known file's digest reveals its upload. `tool_calls` and `tool_calls_open` both carry `repeat_number` per trace (equal for calls with the same tool and arguments; `tool_calls_open` also omits `idempotency_key`, `corpus_digest`, `memory_id`, `memory_sha256`, `memory_source_provenance`, and `devai_*` summaries so it stays a strict leak-free subset); repeats across an outcome are counted in Gold from the restricted views.
- **Rejected Options**: keyed hashes; a key hashed from the trace or outcome id, reversible by guessing.

## ADR-039: Bounded Columns, Labels and Column Corrections *(Amended by `ADR-045`)*
- **Decision**: A value gets its own typed Silver column when Silver or a Gold add-on formula/gate computes, joins, or filters on it; pure slice-only attributes (~30 prompt, agent-version, MCP, A2A, cloud-residency, and evaluation-score keys) travel in one `labels` `JSON` column registered in `sql/silver/schema/labels_registry.json` (open-safe keys only in `*_open`). `model_calls` keeps the client request id and the provider response id in two columns, and the requested and the responding model in two columns. The retry count has one name, `caller_retry_count`, defined identically in [`specs/silver.md`](specs/silver.md) and [`specs/capacity.md`](specs/capacity.md). The cache-write split by lifetime is not published until an attribute carries it. `traces.user_turn_count` above 1 marks a trace with more than one user turn.

## ADR-040: Application Identity Through the Registry, Bound to the Landing Place
- **Decision**: `app_registry` maps telemetry identities to applications: `apphub.application.id` first, else `service.name` plus `COALESCE(deployment.environment.name, deployment.environment)`. No match stays unregistered; nothing is invented. The registry also lists where each application's spans must land; an identity claimed from elsewhere is published as not bound. Where a rule depends on the tier, Gold treats unbound and unregistered applications as the strictest tier. Inside one landing place identity stays self-declared.

## ADR-041: One Hashed Object; Declared Hash Source *(Amended by `ADR-045`)*
- **Decision**: The hashed object is the tool name plus the arguments exactly as the tool received them; the model's raw text is not used. Approval and commit rows both carry that object in one column, `action_args` (formerly `approved_args`), and Silver computes their hashes; writers of that table never send hashes. The tool-side hash names its source (`proxy | silver | application | none`): the tool proxy (`args_hash_attested = true`), Silver from inline tool-span arguments, an application `execute_tool` span's `bq_eval.args_sha256` when neither exists (`args_hash_attested = false`), or none. It is never computed from the model's message, and every implementation must pass one shared set of test cases. Within one copy of `workflow_events` the first row by event time with an `event_id` counts; a repeat with different content is counted as a conflict.
- **Rejected Options**: ready-made hashes from systems of record; a reduced per-tool object; new columns.

## ADR-042: Tokens Mean a Model Call; Duplicate Spans Keep the Latest *(Amended by `ADR-045`)*
- **Decision**: A span that reports tokens above zero is a model call, whatever its operation name (`is_model_call = true` alongside operation-based `span_kind`). Where such a span encloses another, only the innermost is a model call; in ADK Format 1 (`call_llm` wrapping `generate_content`), the folded inner row lifts `llm_request`, `llm_response`, and `session_id` fallback from `call_llm`. `spans` keeps one row per `(trace_id, span_id)` per landing place, the latest arrival, and counts the rest.

## ADR-043: Declared "Full" Is Not Scaled
- **Decision**: Gold does not scale counts or cost for an application the registry declares `sampling_mode = full`. Otherwise a missing sampling probability leaves the scaled number empty, with the observed number beside it. Silver is unchanged.

## ADR-044: Business Units Are Separated Inside the Restricted Functions *(Amended by `ADR-045`)*
- **Decision**: In the default profile each restricted function keeps only the caller's business units, read from `unit_entitlements` (mapping `NULL` business units to `'__unowned__'` for admin/audit entitlements so unregistered or unowned rows are never silently lost). Row policies stay for stored tables.
- **Measured**: no row policy on the linked trace table, a view or a table function.

## ADR-045: Committed Silver Schemas, JSON Typing, Payload/Event/Outcome Rules & Fixes
- **Decision**: Ship 12 Silver JSON schemas + `labels_registry.json` (`sql/silver/schema/`): (1) internal `spans(from_time, to_time)` stays text-free (restricted views join text by `trace_id, span_id`); (2) list/structured columns use `JSON` (never `ARRAY`) so `NULL != []`, child counts default to `0`, `traces` token sums are `NULL` when `model_call_count = 0`, subject-less provenance/bound booleans are `NULL`, `embeddings` set `content_missing = false`, and multi-span precedence is gateway -> root -> earliest span; (3) `trace_payloads` builds `turn_context` with `tools` flags and 5 `*_read_whole` booleans (following message refs on Hive BigLake, keeping instruction/def refs as addresses); (4) `events` lifts `linked_outcome_id`, `outcome_status`, `feedback_reason`, `state_transition_to` from `payload_json` (`ci.verified` authoritative in SoR copy only), and `outcomes` aggregates over `outcome_lookback_days` with `extends_outside_window`.

## ADR-046: Tiered Multi-Cloud Routing (Cross-Cloud Connections, Borderless Lakehouse & Staged Hub Transfer)
- **Decision**: Retire BigQuery Omni spoke SQL (lacks UDFs for `ADR-041` JCS hashing, live `JOIN`s, `AI.GENERATE_TABLE`) and route spokes via three modes ([`specs/clouds.md`](specs/clouds.md)) leaving single-domain Silver Core unchanged: (1) **Direct Cross-Cloud Bronze (`RL-4`–`RL-5`)**: read S3/ADLS Parquet/Iceberg V2+ via Hub Cross-Cloud Connections or Borderless Lakehouse (`P.C.N.T`) on GCP slots with `ADR-044` function filtering; (2) **Zero-Copy Federated `*_open` Spokes (`RL-1`–`RL-3` regional CCI Hub)**: compute Silver + **In-Cloud Judge Port** in-cloud (passing `ADR-041` JCS vectors) and federate leak-free `*_open` Iceberg V2 over CCI, materializing settled Gold windows once/day (`restrictNonCmekServices`); (3) **Staged `Hub Transfer Port`**: `STS -> GCS -> LOAD DATA` for `US`/`EU` Hubs, Azure gaps, and sovereign GCP spokes. Keep `remote_fragment_pending` (`>= 300s`), `object_storage_stats` (`0`-byte dry runs), and 4-part `P.C.N.T` support.

## ADR-047: `M18-0` Span Edge Cases, Event Ties, Hash-Free Single-Cloud Open Twins, Outcome Close Order & Entitlements
- **Decision**: Close 8 `M18-0` rules: (1) positive-token tool spans keep `span_kind = 'tool_call'` in `tool_calls` AND set `is_model_call = true` for `model_calls`; (2) non-convention zero-token ops (e.g. `invoke_node`) stay `span_kind = 'other'` (`is_model_call = false`), counted, unflagged; (3) identical `(source_class, event_id, event_time)` ties in `workflow_events` break by canonical row-content lexical order (keeping 20 fixed columns) while incrementing `conflict_count`; (4) single-cloud `silver_open` omits `user_id_hash`/`tenant_id_hash` (`ADR-038`; KMS-HMAC stays in `clouds.md`); (5) multi-day `outcomes` pick close by authority (`system_of_record > platform > application`), then earliest `event_time` (`event_id` asc), with Gold deduplicating by `outcome_id`; (6) `unit_entitlements` is directory/CI-synced with `(principal_email, business_unit, tenant_id)` (`'*'` wildcard / `UC9` tenant), read by per-BU Gold SAs plus `'__unowned__'` admin SA.

## ADR-048: Silver Dataset Layout, Deploy-Time Window Constants, Python JCS UDF & Bootstrap Mode
- **Decision**: Settle the `M18-4`/`M18-6` physical layout: (1) four datasets — `silver_internal` (internal `spans`, `jcs` UDF, `unit_entitlements`, 3 `workflow_events` copies), `silver_ref` (SCD-2 `app_registry`, `tools`, `skill_registry`), `silver_open`, and `silver_restricted` — preserving `M18-2` 2-hop authorized-routine isolation so `silver_open` callers never read `silver_internal`; (2) `read_margin` (`60m`), `settle_time` (`5m`), and `outcome_lookback_days` (`30d`) render into TVF SQL at deploy time via placeholders (`${READ_MARGIN_MINUTES}`, `${SETTLE_MINUTES}`, `${OUTCOME_LOOKBACK_DAYS}`), keeping `37x` BigQuery pruning and `(from_time, to_time)` signatures; (3) trace-store JSONL fixtures, one SQL file per Silver object under `sql/silver/`, and `bq-eval bootstrap --mode silver` (leaving legacy `--mode light` intact until `M19-2`); (4) RFC 8785 JCS hashing (`sql/silver/jcs.sql`) uses a BigQuery `LANGUAGE python` (`runtime_version = 'python-3.11'`) UDF behind a `65,532`-byte guard, sharing the exact canonicalizer algorithm with `bq_eval/runner/`.
- **Rejected Options**: three datasets placing `spans` inside `silver_restricted`; table-driven window constants (defeats pruning); `LANGUAGE js` UDF; separate `silver-deploy` CLI verb.
