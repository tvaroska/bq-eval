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
- **Decision**: Project `embeddings` onto `model_calls`, retrieval/memory + warehouse compute onto `tool_calls`, `system_instructions`/`tool_definitions` onto `trace_payloads`, optimizer lineage onto `traces`, and `outcome.linked` onto `events`; add Optimizer Gold Export Port, dual-SME adjudication, calibration expiry, PT eval isolation, and slot backpressure.

## ADR-031: Multi-Region Hub, Location Alignment, Multi-Project Security, Bounded `MERGE` & WORM/RTBF *(Amended by `ADR-035`, `ADR-044`, `ADR-046`)*
- **Decision**: (1) Default Hub to `US`/`EU` multi-region (or single region for regional Lakehouse + CCI, `ADR-046`) with per-domain location + KMS CMEK alignment (`specs/clouds.md`). (2) Keep `outcome.subject` restricted-only; split `enterprise` compaction SAs; enforce per-BU/tenant access (`ADR-044`, `ADR-047`); require target `DATE(start_time)` pruning on `MERGE`, bounded lookbacks, and `max_bytes_billed` + `INFORMATION_SCHEMA.JOBS` slot breakers. (3) Export regulated Gold snapshots to PII-free **GCS Bucket Retention-Lock + CMEK** archives with per-subject RTBF DEKs.

## ADR-032: Preventive Port, Two-Speed Registry, `< 5s` Runner & Gateway/FinOps Hardening *(Amended by `ADR-034`, `ADR-039`, `ADR-045`, `ADR-046`)*
- **Decision**: (1) Gateways enforce live writes, DLP, `allowed_models`, `1s/10s` `429` shedding, daytime PT `optimize`, and `100 MB` streaming emitting `guardrail.intervention`; promote offline SQL translator to **`< 5s` Local/CI Eval Runner Port** (`CI_ONLY`). (2) Add SCD-2 `skill_registry` (`protected_block_sha256`), split `modifies_workspace` from `writes_state`, sync registries via Two-Speed CI/CD + dark-agent sync, require OBO + `authz_mode`, and check reverse `action.committed -> tool_calls`. (3) Mandate `x-client-request-id` + `(trace_id, span_id)` `1:N` joins, `trace_owner_app_id` precedence, `state_mode`, honest 3P `inferred_ambiguous`, 3-pool `FOCUS 1.2`, `burst_metrics_1m`, and `allowed_models` ([`specs/capacity.md`](specs/capacity.md)).

## ADR-033: `SR 11-7` Stratified Calibration, Stateful Memory, Guardrail FP/FN & 11-Workload Split *(Amended by `ADR-049`)*
- **Decision**: (1) Stratum-specific $Se_h/Sp_h$ (and per-cloud $Se/Sp$) Rogan-Gladen + joint CIs + $[0,1]$ clamping; fail-closed `AC2` + binary cut-point agreement (`ADR-049`); `RL-1 >= 100` floors + SME tie-break; deterministic independent judges (`temperature = 0`, `seed`, snapshot ID, prompt hash, `zdr_allowed`); expanded `CALIBRATED` expiry; `min_model_confidence` default on `outcomes`. (2) Add `memory_*` lineage, `state.transition`, bounded session reconstruction (`MISSING_SESSION_CONTEXT`), PII-masked judge prompts, guardrail FP/FN, MNPI `barrier_domain` + empty-RAG alerts, `shadow_of_trace_id`, `nudge_id`, `outcome_links`, `db.query.job_id`/`JOBS` reconciliation, delayed ground truth, and sibling non-LLM table (`UC1`–`UC11`).

## ADR-034: Silver Keeps What It Saw; "Verified" Comes From Provenance
- **Decision**: Silver publishes the root span's `user_id` + `user_id_source`; Gold picks the requester when platform spans differ and gates unverified IDs by tier. Writer class (`system_of_record | platform | application`) is set by landing place, deriving `identity_attested`, `args_hash_attested`, and `traffic_type_attested` (never self-reported). Regulated deployments isolate gateway/tool-proxy spans from app writes; control verdicts require those spans or a platform-only `workflow_events` copy (cloud runtime spans excluded).

## ADR-035: Explicit Read Windows; High-Volume Profile Is Design Only
- **Decision**: Every published object is a `(from_time, to_time)` TVF with no stored data (views are thin wrappers). Traces belong to their first span's window, read with margin, and settle after `end + margin + settle_time`; Gold keeps settled windows only (telemetry repeatable; registry needs validity windows; `events`/`outcomes` reflect read time). `enterprise` (candidate text-free typed span store under identical TVFs; evaluated vs merged tables) is design-only.
- **Measured**: literal or parameter windows prune through a function; functions are authorized on Bronze one by one.

## ADR-036: Content Is Required; Calls Without It Are Kept, Not Scored *(Amended by `ADR-042`)*
- **Decision**: Shape routing stays keyed on content (inline or by reference). Contentless model/tool calls remain in text-free objects with content-missing facts (counted in cost/monitoring, unscored by eval; zero-payload profile dropped). Span kinds come from operation names; non-GenAI spans stay `'other'` (kept, counted, unflagged).
- **Rejected Options**: routing by name family; counting every unmatched span as unknown shape; excluding calls without content.

## ADR-037: Root, Turn and Clock
- **Decision**: Trace root = earliest application-emitted span (excluding cloud runtime spans), which must carry `user.id`. A turn has `>= 1` agent, model-call, or tool span (others kept, not turns). SLA clock starts at earliest stored span (recording runtime vs app writer). Silver publishes incompleteness reasons (missing parent, `> read_margin`, last arrival time); Gold alerts only on settled missing-parent gaps.

## ADR-038: Open Twins Carry No Hash Material *(Amended by `ADR-045`)*
- **Decision**: Argument hashes, approvals, commits, and file digests publish in `silver_restricted` only (hash-guessing and upload-digest leaks). `tool_calls` and `tool_calls_open` both expose per-trace `repeat_number` (same tool + args; `tool_calls_open` also omits `idempotency_key`, `corpus_digest`, `memory_id`, `memory_sha256`, `memory_source_provenance`, `devai_*` summaries); cross-outcome repeats run in Gold over restricted views.
- **Rejected Options**: keyed hashes; a key hashed from the trace or outcome id, reversible by guessing.

## ADR-039: Bounded Columns, Labels and Column Corrections *(Amended by `ADR-045`)*
- **Decision**: Formula/join/filter keys get typed Silver columns; slice-only attributes (~30 prompt, agent-version, MCP, A2A, cloud-residency, eval keys) ride in one `JSON` `labels` column registered in `sql/silver/schema/labels_registry.json` (`*_open` keeps open-safe keys only). `model_calls` splits client request id vs provider response id and requested vs responding model; retry count is `caller_retry_count` ([`specs/silver.md`](specs/silver.md), [`specs/capacity.md`](specs/capacity.md)); cache-write TTL split deferred; `traces.user_turn_count > 1` marks multi-turn traces.

## ADR-040: Application Identity Through the Registry, Bound to the Landing Place
- **Decision**: `app_registry` maps `apphub.application.id`, else `service.name` + `COALESCE(deployment.environment.name, deployment.environment)`, leaving misses unregistered (zero invented defaults), and binds landing places (off-place spans flagged unbound). Unbound/unregistered apps default to the strictest Gold tier; identity stays self-declared within one landing place.

## ADR-041: One Hashed Object; Declared Hash Source *(Amended by `ADR-045`)*
- **Decision**: Hashed object = `tool_name` + executed args, never raw LLM text. Approval and commit `workflow_events` rows both carry `action_args` (formerly `approved_args`) hashed by Silver; writers never send hashes. Tool hashes record source (`proxy | silver | application | none`, `args_hash_attested = true` on `proxy` only); shared test cases required across implementations. Within one `workflow_events` copy, first arrival per `event_id` counts; differing repeats flag conflicts.
- **Rejected Options**: ready-made hashes from systems of record; a reduced per-tool object; new columns.

## ADR-042: Tokens Mean a Model Call; Duplicate Spans Keep the Latest *(Amended by `ADR-045`)*
- **Decision**: Positive-token spans are model calls (`is_model_call = true` beside operation `span_kind`); nested spans mark only the innermost, while ADK Format 1 (`call_llm` -> `generate_content`) lifts `llm_request`, `llm_response`, and `session_id` fallback onto the inner row. `spans` keeps latest arrival per `(trace_id, span_id)` per landing place and counts duplicates.

## ADR-043: Declared "Full" Is Not Scaled
- **Decision**: Gold skips count/cost scaling when registry sets `sampling_mode = full`; missing `sampling_probability` leaves scaled numbers NULL beside observed totals (Silver unchanged).

## ADR-044: Business Units Are Separated Inside the Restricted Functions *(Amended by `ADR-045`)*
- **Decision**: Restricted TVFs filter to caller BUs via `unit_entitlements` (mapping `NULL` BU to `'__unowned__'` for admin/audit entitlements so unowned rows are never silently dropped); row policies remain for stored tables.
- **Measured**: no row policy on the linked trace table, a view or a table function.

## ADR-045: Committed Silver Schemas, JSON Typing, Payload/Event/Outcome Rules & Fixes
- **Decision**: Ship Silver JSON schemas + `labels_registry.json` (`sql/silver/schema/`): (1) internal `spans(from_time, to_time)` stays text-free; (2) structured columns use `JSON` (never `ARRAY`), child counts default to `0`, `traces` token sums are `NULL` when `model_call_count = 0`, subject-less booleans are `NULL`, `embeddings` set `content_missing = false`, and trace precedence is gateway -> root -> earliest span; (3) `trace_payloads` builds `turn_context` with `tools` flags and 5 `*_read_whole` flags; (4) `events` lifts `linked_outcome_id`, `outcome_status`, `feedback_reason`, `state_transition_to` (`ci.verified` authoritative in SoR only), and `outcomes` sets `extends_outside_window`.

## ADR-046: Tiered Multi-Cloud Routing (Cross-Cloud Connections, Borderless Lakehouse & Staged Hub Transfer)
- **Decision**: Retire BigQuery Omni spoke SQL and route spokes via three modes ([`specs/clouds.md`](specs/clouds.md)) leaving single-domain Silver Core unchanged: (1) **Direct Cross-Cloud Bronze (`RL-4`–`RL-5`)**: S3/ADLS Parquet/Iceberg V2+ via Hub Cross-Cloud Connections or Borderless Lakehouse (`P.C.N.T`) with `ADR-044` filtering; (2) **Zero-Copy Federated `*_open` Spokes (`RL-1`–`RL-3` regional CCI Hub)**: in-cloud Silver + **In-Cloud Judge Port** (`ADR-041` JCS) and federated `*_open` Iceberg V2 materialized per settled window (`restrictNonCmekServices`); (3) **Staged `Hub Transfer Port`**: `STS -> GCS -> LOAD DATA` for `US`/`EU` Hubs, Azure gaps, and sovereign spokes.

## ADR-047: `M18-0` Span Edge Cases, Event Ties, Hash-Free Single-Cloud Open Twins, Outcome Close Order & Entitlements
- **Decision**: (1) Positive-token tool spans keep `span_kind = 'tool_call'` (`tool_calls`) + `is_model_call = true` (`model_calls`); (2) non-convention zero-token ops stay `span_kind = 'other'` (`is_model_call = false`), unflagged; (3) same-timestamp `(source_class, event_id, event_time)` ties in `workflow_events` break by row-content lex order (`conflict_count > 0`); (4) single-cloud `silver_open` omits `user_id_hash`/`tenant_id_hash` (`ADR-038`); (5) multi-day `outcomes` pick close by `system_of_record > platform > application` then earliest `event_time` (`event_id` asc), deduplicated in Gold by `outcome_id`; (6) `unit_entitlements` holds `(principal_email, business_unit, tenant_id)` (`'*'` wildcard / `UC9` tenant) plus `'__unowned__'` admin SA.

## ADR-048: Silver Dataset Layout, Deploy-Time Window Constants, Python JCS UDF & Bootstrap Mode *(Amended by `ADR-049`)*
- **Decision**: (1) Four Silver datasets — `silver_internal` (`spans`, `jcs`, `unit_entitlements`, 3 `workflow_events` copies), `silver_ref` (`app_registry`, `tools`, `skill_registry`), `silver_open`, `silver_restricted` — preserving 2-hop authorized-routine isolation; (2) `read_margin` (`60m`), `settle_time` (`5m`), `outcome_lookback_days` (`30d`) render into TVFs at deploy time (`37x` pruning); (3) trace-store JSONL fixtures + per-object SQL under `sql/silver/`; (4) `LANGUAGE python` (`python-3.11`) RFC 8785 JCS UDFs behind a `65,532`-byte guard matching `bq_eval/runner/`.

## ADR-049: Gold Requester Precedence, Fail-Closed IRR Gate, Capacity/Gold Datasets & Legacy Retirement (`M19`)
- **Decision**: (1) Gold resolves `COALESCE(platform_user_id, user_id)`, flags `platform_user_id != user_id` mismatches (fail-closed on regulated tiers), and requires attested/platform ID (`platform_user_id IS NOT NULL OR identity_attested IS TRUE`) on `RL-1`–`RL-3` (unbound/unregistered = `RL-1`, `ADR-040`). (2) `passes_irr_gate` fails closed (`CALIBRATION_STATUS = 'UNCALIBRATED'`, `calibrated_pass_rate = NULL`, `CPVO = NULL`) if `SME-vs-SME AC2` / `judge-vs-SME AC2` (`>= 0.85` `RL-1/2`, `>= 0.80` `RL-3*`, `>= 0.75` `RL-4/5`), binary cut-point agreement (`>= 0.85`), or paired label floors (`>= 10`, `>= 100` `RL-1`) miss. (3) SCD-2 `capacity_pools`, `capacity_allocations`, `allowed_models` -> `silver_ref`; open-safe `requests(from_time, to_time)` + `burst_metrics_1m` -> `silver_open`; Gold -> `agent_eval` via `bq-eval bootstrap --mode silver|gold` (retiring `light|full|hybrid`, `bqaa`, hand-written SQLite twins; keeping `m2-live` + `AI.EVALUATE_AGENT` Preview SQL).

## ADR-050: Tail Alignment, Causal Loop/Empty-RAG Rule-0 Gates & `step_outputs` Exposure
- **Decision**: (1) Align `build_ai_generate_table_judge_sql()` (`sql_eval.py`) and `v_agent_eval_and_finops_summary` (`summary_views.sql`) on identical tail criteria (`tool_error_count > 0`, `http_429_count > 0`, `escalated_to_high_reasoning`, `repeated_tool_call_count > 0`, `silent_empty_rag_count > 0`, plus `trace_latency_ms > sla_latency_ms` unless `include_healthy_slow_in_tail=False`) while exposing causal `tail_class` (`LOOP_OR_ERROR_TAIL | HEALTHY_SLOW | HEALTHY`) and `tail_root_cause` on `v_trace_sql_evaluation`. (2) Gate `rule0_pass` on `repeated_tool_call_count <= COALESCE(workload_thresholds.max_repeated_tool_calls, 0)` and `silent_empty_rag_count = 0`. (3) Expose XML-escaped `step_outputs` (`step_outputs_read_whole` checked) to `v_step2_session_reconstructed_payloads`, `v_step2_all_trace_rubric_prompts`, `v_eval_agent_turns`, and `build_ai_generate_table_judge_sql()`, switching `v_step2_5_example_5pct_slice` to `completeness_4pt_v1`.
