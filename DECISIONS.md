# Architectural Decisions (`DECISIONS.md`)

## ADR-001: Repository Separation & Cross-Repo Boundaries (`bq-eval` vs. `agent-evals` vs. `capacity`)
- **Decision**: Keep three repositories decoupled behind versioned **Ports, Adapters and Contracts**: (1) `bq-eval` owns Bronze/Silver SQL + Gold evaluation, monitoring, and cost add-ons; (2) `agent-evals` owns normative `EVAL-P01`–`EVAL-P11` standards, MRM playbooks, and `eval-audit`; (3) `capacity` owns Newsvendor sizing, sub-minute burst physics, gateway routing/shedding policies, and billing actuation over exported `bq-eval` payloads (never naming `bq-eval` views directly).

## ADR-002: Progressive Zero-Table Step 1 -> Step 3 View Ladder *(Superseded by `ADR-010`)*
- **Summary**: Earlier design priced spans inside Step-1 views. Replaced by price-free Silver core + optional Gold add-ons.

## ADR-003: Cross-Cloud Collector Forwarding & `'unregistered_app'` Defaults *(Superseded by `ADR-010`)*
- **Summary**: Replaced by in-cloud telemetry residency and `NULL` for missing fields.

## ADR-004: Fail-Closed Rogan-Gladen Calibration, Rule Pre-Screening & Gwet's `AC2` *(Amended by `ADR-030`, `ADR-033`)*
- **Decision**: Run deterministic SQL gates before LLM judging; sample `5–10%` passing baseline + `100%` passing slow/throttled tails; report conditional ($\hat{\theta}_{\text{RG}}$) and unconditional end-to-end pass rates strictly over rule-passing judged traces (billing `100%` of judge runs); fail closed to `NULL` on empty strata, missed floors, or $J / J' \le 10^{-6}$; pair Quadratic Gwet's `AC2` with 95% CIs (`ADR-033` upgrades to stratum-specific $Se_h/Sp_h$, design-based joint CIs, and binary cut-point agreement).

## ADR-005: Portable, Customer-Agnostic Repository Hygiene
- **Decision**: Enforce automated AST and text hygiene gates (`tests/test_repo_hygiene.py`, `tests/test_isolation_guard.py`) blocking internal codenames, shortlinks, bug IDs, workstation paths, `file://` URIs, `.github/workflows/`, escaping relative links, and forbidden upward-layer imports.

## ADR-006: Outcome-Adjusted Cost, `>200K` Cliffs & Zero-Fabrication Model Selection *(Amended by `ADR-032`)*
- **Decision**: Separate spend into 6 buckets (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`, `optimize`, `unclassified`), price each call before aggregating (`>200K` cliffs keyed on `input + cache_read`), divide total spend (incl. failures, escalations, and `100%` judge spend) by verified outcomes, enforce `0.0%` negative flips on regulated/safety gates (`UC5`), and evaluate cascades (`UC4`) on combined `(primary + escalation + eval)` cost above the calibrated accuracy floor.

## ADR-007: Dedicated Capacity & `FOCUS 1.2` Chargeback *(Amended by `ADR-028`, `ADR-032`)*
- **Decision**: Attribute dedicated-capacity commitments via `FOCUS 1.2` multi-pool rules (active-hours funded share, off-peak backfill, unallocated idle pool, Priority vs. Standard PayGo spillover, reconciled to Cloud Billing) and route batch evals into off-peak valleys.

## ADR-008: Dual-Path `BQAA` `agent_events` + `_AllLogs` Switchboard *(Superseded by `ADR-011`)*
- **Summary**: Replaced by the three shapes in [`spec/shapes.md`](spec/shapes.md).

## ADR-009: Hand-Written Offline SQLite Runtime Twin *(Superseded by `ADR-010`)*
- **Summary**: Replaced by translating the single BigQuery SQL implementation offline (`ADR-032` promotes it to the Local/CI Eval Runner Port).

## ADR-010: Bronze and Silver Core in BigQuery, Gold Add-Ons on Top *(Amended by `ADR-012`–`ADR-046`)*
- **Decision**: Core is Bronze (telemetry in place) + Silver SQL in BigQuery (zero prices, zero verdicts, `NULL` for missing, one-way trust on self-reported failures). Gold add-ons read only published Silver views. BigQuery SQL is the single Silver implementation.

## ADR-011: At Most Three Telemetry Shapes & Content by Reference *(Amended by `ADR-026`, `ADR-028`, `ADR-032`, `ADR-036`)*
- **Decision**: Silver reads at most three shapes in BigQuery SQL ([`spec/shapes.md`](spec/shapes.md)): legacy ADK, OTel GenAI inline, and OTel GenAI by reference (`file_data` URIs stay in place). Non-conforming frameworks, coding SDKs, and custom gateways normalize via the Collector Port.

## ADR-012: Table-Function Silver Default (`light`) with Optional Span Base *(Amended by `ADR-035`)*
- **Summary**: Default (`light`) publishes explicit-window table functions over Bronze; `enterprise` (design only) tests an append-only text-free span base against incremental tables.

## ADR-013: Typed Silver Views and `tools` Table *(Amended by `ADR-019`, `ADR-020`, `ADR-023`, `ADR-032`)*
- **Summary**: Established typed Silver views (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`) and factual `tools` flags; `requests` moved to capacity (`ADR-023`).

## ADR-014: Non-Blocking Tool-Call Counts in Silver; Loop Rules in Gold *(Amended by `ADR-020`)*
- **Summary**: Kept tool-call/repeat counts in Silver and thresholds/pass rules in Gold.

## ADR-015: Standard-First Telemetry Attributes (OTel + `EVAL-P11` + Minimal `bq_eval.*`) *(Amended by `ADR-039`, `ADR-045`)*
- **Decision**: Read OTel (`v1.45+`), OTel GenAI (`v0.1+`), MCP, A2A, and cloud attributes verbatim, preserve `EVAL-P11` `outcome.*`, and confine custom keys strictly to `bq_eval.*` ([`spec/attributes.md`](spec/attributes.md)).

## ADR-016: Turn-Scoped `trace_payloads` Delta Contract *(Amended by `ADR-020`, `ADR-027`, `ADR-045`)*
- **Summary**: One `trace_payloads` row per content-bearing turn splitting `prior_history` from turn deltas (`turn_prompt`, `turn_context`, `turn_output`, `step_outputs`, `media_uris`).

## ADR-017: In-Cloud Identity Residency & Zero Hot Stores *(Amended by `ADR-021`, `ADR-028`, `ADR-029`, `ADR-031`)*
- **Summary**: Raw identities stay in-cloud; no hot external databases; `>= 5m` BigQuery floor; out-of-band KMS-HMAC hashes for cross-cloud joins.

## ADR-018: Call-Level Join/Lineage Keys & Operational-vs-Calibration Boundary *(Amended by `ADR-028`, `ADR-032`)*
- **Summary**: Added call-level join/lineage keys and separated operational `events` feedback from Gold blind SME calibration labels.

## ADR-019: Factual Tool Capabilities, Payload-Bound HITL Approvals & Scoped `capacity_pools` *(Amended by `ADR-028`, `ADR-029`, `ADR-032`)*
- **Summary**: Kept factual capability flags in Silver `tools`, bound `outcome.approved` via `action_id` + `approved_args_sha256` (policies in Gold), split `allowed_models`/`capacity_pools`/`capacity_allocations`, and added versioned `capacity` exports.

## ADR-020: Silver Publishes Counts and Facts Only; Each Add-On Defines Its Own Pass
- **Decision**: Silver holds zero pass/threshold logic (`output_kind`, turn TTFT, wait sums, `is_settled`, slow flags live in Gold). Evaluation's default: completed turn = root OK and not `app_failed`; verified outcome = every trace completed (or closing trace when retries are expected).

## ADR-021: Read Windows, 5-Minute Floor and SLA Clocks from Trace Start *(Amended by `ADR-035`, `ADR-037`)*
- **Summary**: Explicit-window Silver reads (`start_time` filter inside `spans`), Gold daily snapshots, `>= 5m` missing-user audit floor, and SLA clocks from trace start.

## ADR-022: Input Table for Non-Span Records, Authority by Table & RFC 8785 Hashes *(Amended by `ADR-034`, `ADR-041`)*
- **Summary**: Non-span records enter via isolated copies of `workflow_events` (authority by landing copy); approvals and commits carry `action_args` hashed in Silver via RFC 8785 JCS.

## ADR-023: One-Domain Core; Clouds, Capacity and Governance as Modular Extensions *(Amended by `ADR-028`, `ADR-030`, `ADR-031`)*
- **Summary**: Kept single-domain core modular while isolating multi-cloud federation ([`spec/clouds.md`](spec/clouds.md)) and gateway/capacity governance ([`spec/capacity.md`](spec/capacity.md)).

## ADR-024: Long Text by Reference, Completeness Facts, Reference Data & Access *(Amended by `ADR-026`–`ADR-031`, `ADR-045`)*
- **Summary**: Content by reference for oversized spans, per-part `*_read_whole` flags, tool-flagged `turn_context`, and fail-closed `sampling_probability`.

## ADR-025: Six Row Rules *(Amended by `ADR-026`, `ADR-029`, `ADR-032`–`ADR-034`, `ADR-037`, `ADR-045`)*
- **Summary**: Canonical reply text, confidence rollup (`min_model_confidence` default on `outcomes`), agent attribution, child/`mcp.session.id` tool folding, turn-prompt delta rule, and root `user.id`.

## ADR-026: Split Open/Restricted Datasets & Single-Pass `spans` *(Amended by `ADR-031`, `ADR-035`, `ADR-038`)*
- **Summary**: Split `silver_open` and `silver_restricted` datasets; route shapes in one `SELECT` pass over `_AllSpans` (folding ADK Format 1 `call_llm`+`generate_content`).

## ADR-027: BigLake Hive-Partitioned Shape 3 & Execution-Time RFC 8785 Hashing *(Amended by `ADR-036`, `ADR-038`, `ADR-041`, `ADR-045`)*
- **Summary**: Require BigLake (`WITH CONNECTION`) + Hive date/hour partitions with static predicates; risk-tiered async uploads (`regulated_worm` fail-closed at proxy); execution-time RFC 8785 `bq_eval.args_sha256` on tool spans.

## ADR-028: `1:N` Gateway `requests`, SCD-2 Governance, Hub Transfer Port & Upstream Collector Port *(Amended by `ADR-031`, `ADR-032`, `ADR-046`)*
- **Decision**: Price each `1:N` `requests` attempt before aggregating to `model_calls` (separating `gateway_failover_attempts` from `caller_retry_count`); add `burst_metrics_1m` and SCD-2 validity windows; connect non-GCP open rows via tiered federation/transfer (`ADR-046`) with KMS-HMAC hashes and In-Cloud Judge Port; normalize non-conforming SDKs via the Collector Port.

## ADR-029: Fail-Closed Completeness, Attested 3-Principal OBO, Guardrail Separation & WORM Audit Snapshots *(Amended by `ADR-031`, `ADR-032`, `ADR-034`, `ADR-037`, `ADR-043`)*
- **Decision**: Keep `sampling_probability = NULL` fail-closed; alert on rootless mutating traces; fail closed on unregistered tools in regulated workflows; require write-IAM provenance for `identity_attested`/`args_hash_attested`; bind `tool_name` + executed args in RFC 8785 hashes; enforce SoD/TTL/single-use and `action.committed` verification; add `guardrail.intervention`, `rejected | denied`, and WORM audit snapshots.

## ADR-030: Typed Non-LLM Spans, Optimizer Gold Port, Dual-SME MRM Rigor & Enterprise Use Cases
- **Decision**: Project `embeddings` onto `model_calls`, retrieval/memory provenance + warehouse compute onto `tool_calls`, `system_instructions`/`tool_definitions` onto `trace_payloads`, optimizer lineage onto `traces`, and `outcome.linked` onto `events`; add Optimizer Gold Export Port, dual-SME adjudication, calibration expiry, PT eval isolation, and slot backpressure.

## ADR-031: Multi-Region Hub Default, Location Alignment, Multi-Project Security, Bounded `MERGE` & GCS WORM/RTBF *(Amended by `ADR-035`, `ADR-044`, `ADR-046`)*
- **Decision**:
  1. **Hub Location & Alignment**: Default the Hub to `US`/`EU` multi-region (or a single region when pairing regional Lakehouse catalogs + CCI, `ADR-046`); mandate strict location + KMS CMEK alignment within every domain (never mix `US` and `us-east4`); route non-hub sovereign GCP perimeters and cross-cloud spokes per [`spec/clouds.md`](spec/clouds.md).
  2. **Perimeter & `MERGE` Hardening**: Keep `outcome.subject` in restricted `outcomes` only; split `enterprise` compaction SAs; enforce per-BU access (`ADR-044`); require static target `DATE(start_time)` pruning on `enterprise` `MERGE`, bounded lookbacks, and `max_bytes_billed` + `INFORMATION_SCHEMA.JOBS` slot breakers.
  3. **PII-Free GCS WORM + RTBF**: Back regulated Gold Audit Snapshots with PII-free **GCS Bucket Retention-Lock + CMEK** archives paired with per-subject crypto-shredding DEKs.

## ADR-032: Preventive Request-Path Port, Two-Speed Registry Sync, `< 5s` Local/CI Runner & Gateway/FinOps Hardening *(Amended by `ADR-034`, `ADR-039`, `ADR-045`, `ADR-046`)*
- **Decision**:
  1. **Preventive Port & `< 5s` Runner**: Gateways/proxies enforce live writes, DLP, `allowed_models`, `1s/10s` `429` shedding, `optimize` daytime PT, and `100 MB` attachment streaming emitting structured `guardrail.intervention` (`control_id`/`layer`/`action`). Promote the offline SQL translator to a **`< 5s` Local/CI Eval Runner Port** (`CI_ONLY`).
  2. **Two-Speed Registry, `skill_registry` & Identity**: Add SCD-2 `skill_registry` (`protected_block_sha256`), split `tools.modifies_workspace` from `writes_state`, sync registries via Two-Speed CI/CD (`RL-1`/`RL-2` dual-signed) + dark-agent reconciliation, require structured OBO claims + `authz_mode`, and check reverse `action.committed -> tool_calls`.
  3. **Gateway `1:N` & 3-Pool FinOps (`ADR-001` Preserved)**: Mandate `x-client-request-id` + `(trace_id, span_id)` `1:N` joins, `trace_owner_app_id` precedence, `state_mode`, core `model_calls` columns, honest 3P `inferred_ambiguous`, 3-pool `FOCUS 1.2` invoice-reconciled chargeback, `burst_metrics_1m`, and extended `allowed_models` ([`spec/capacity.md`](spec/capacity.md)).

## ADR-033: `SR 11-7` Stratified Calibration, Stateful Memory Reconstruction, Guardrail FP/FN & 11-Workload Use-Case Split
- **Decision**:
  1. **Calibration Rigor**: Stratum-specific $Se_h/Sp_h$ (and per-cloud $Se/Sp$) Rogan-Gladen + design-based joint CIs + $[0,1]$ clamping; `AC2 >= 0.80` + binary cut-point agreement; `RL-1 >= 100` floors + SME tie-break; deterministic independent judges (`temperature = 0`, `seed`, snapshot ID, prompt hash, `zdr_allowed`); expanded `CALIBRATED` expiry triggers; `min_model_confidence` default on `outcomes`.
  2. **Memory, Guardrail FP/FN & Compute**: Add `memory_*` lineage, `state.transition`, bounded session reconstruction (`MISSING_SESSION_CONTEXT`), PII-masked judge prompts, guardrail FP/FN judging, MNPI `barrier_domain` + empty-RAG alerts, `shadow_of_trace_id`, `nudge_id`, `outcome_links`, `db.query.job_id`/`JOBS` reconciliation, `T+1..30d` ground truth, and sibling non-LLM table across 11 use cases (`UC1`–`UC11`).

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
- **Decision**: A value gets its own typed Silver column when Silver or a Gold add-on formula/gate computes, joins, or filters on it; pure slice-only attributes (~30 prompt, agent-version, MCP, A2A, cloud-residency, and evaluation-score keys) travel in one `labels` `JSON` column registered in `sql/silver/schema/labels_registry.json` (open-safe keys only in `*_open`). `model_calls` keeps the client request id and the provider response id in two columns, and the requested and the responding model in two columns. The retry count has one name, `caller_retry_count`, defined identically in [`spec/silver.md`](spec/silver.md) and [`spec/capacity.md`](spec/capacity.md). The cache-write split by lifetime is not published until an attribute carries it. `traces.user_turn_count` above 1 marks a trace with more than one user turn.

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

## ADR-045: Committed Silver Schemas, JSON List Typing, Payload/Event/Outcome Rules & Contradiction Fixes
- **Decision**: Ship the 12 core Silver BigQuery schemas and `labels_registry.json` under `sql/silver/schema/`: (1) internal `spans(from_time, to_time)` stays text-free (restricted views join text by `trace_id, span_id`); (2) list and structured columns use `JSON` (never `ARRAY`) so `NULL` stays distinct from `[]`, child-set counts default to `0`, `traces` token sums are `NULL` when `model_call_count = 0`, subject-less provenance/bound booleans are `NULL`, `embeddings` set `content_missing = false`, and trace-level multi-span precedence is gateway -> root -> earliest span; (3) `trace_payloads` builds `turn_context` from ordered tool/retrieval/memory results with `tools` flags and 5 `*_read_whole` booleans (following message refs on Hive BigLake, keeping instruction/definition refs as addresses); (4) `events` lifts `linked_outcome_id`, `outcome_status`, `feedback_reason`, and `state_transition_to` from `workflow_events.payload_json` (keeping `ci.verified` authoritative only in the SoR copy), and `outcomes` returns rows active in window aggregated over `outcome_lookback_days` with `extends_outside_window`.

## ADR-046: Tiered Multi-Cloud Routing (Cross-Cloud Connections, Borderless Lakehouse & Staged Hub Transfer)
- **Decision**: Retire standard BigQuery Omni spoke SQL (which lacks UDFs required for `ADR-041` RFC 8785 JCS hashing, live `JOIN`s, and `AI.GENERATE_TABLE`) and route multi-cloud/multi-region spokes across three explicit modes in [`spec/clouds.md`](spec/clouds.md) while keeping single-domain Silver Core unchanged: (1) **Direct Cross-Cloud Bronze (`RL-4`–`RL-5` permissive)**: read compacted S3/ADLS Parquet or Iceberg V2+ via Hub-located **BigQuery Cross-Cloud Connections** or **Borderless Lakehouse Federated Catalogs** (`P.C.N.T`) on standard GCP slots with `ADR-044` function filtering (federated tables disallow RAPs); (2) **Zero-Copy Federated `*_open` Spokes (`RL-1`–`RL-3` regional CCI Hub)**: compute Silver + **In-Cloud Judge Port** in-cloud (Spark/Trino/Snowflake passing `ADR-041` JCS vectors) and federate only leak-free `*_open` Iceberg V2 tables over **Cross-Cloud Interconnect**, materializing settled Gold windows once/day so `restrictNonCmekServices` cache disablement stays bounded; (3) **Staged `Hub Transfer Port`**: retain `STS -> GCS -> LOAD DATA` for `US`/`EU` multi-region Hubs bridging regional CCI catalogs, Azure private gaps, and sovereign GCP regions. Preserve `remote_fragment_pending` (`>= 300s` catalog lag), `object_storage_stats` cost breakers (`0`-byte federated dry runs), and 4-part `P.C.N.T` SQL support in `bootstrap.py` and the offline translator.
