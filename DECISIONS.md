# Architectural Decisions (`DECISIONS.md`)

## ADR-001: Repository Separation & Cross-Repo Boundaries (`bq-eval` vs. `agent-evals` vs. `capacity`)
- **Context**: Combining BigQuery evaluation/FinOps SQL, regulated MRM playbooks, and live GSU/GPU capacity actuation in one repository blurred build, compliance, and latency boundaries.
- **Decision**: Keep three repositories decoupled behind versioned **Ports, Adapters and Contracts**: (1) `bq-eval` owns Bronze/Silver SQL + Gold evaluation, monitoring, and cost add-ons; (2) `agent-evals` owns normative `EVAL-P01`–`EVAL-P11` standards, MRM playbooks, and `eval-audit`; (3) `capacity` owns Newsvendor sizing, sub-minute burst physics, gateway routing/shedding policies, and billing actuation over exported `bq-eval` payloads (never naming `bq-eval` views directly).
- **Rejected Option**: Monolithic single-repo bundling MRM playbooks, live `429` shedding actuation, and BigQuery SQL together.

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

## ADR-010: Bronze and Silver Core in BigQuery, Gold Add-Ons on Top *(Amended by `ADR-012`–`ADR-043`)*
- **Decision**: Core is Bronze (telemetry in place) + Silver SQL in BigQuery (zero prices, zero verdicts, `NULL` for missing, one-way trust on self-reported failures). Gold add-ons read only published Silver views. BigQuery SQL is the single Silver implementation.

## ADR-011: At Most Three Telemetry Shapes & Content by Reference *(Amended by `ADR-026`, `ADR-028`, `ADR-032`, `ADR-036`)*
- **Decision**: Silver reads at most three shapes in BigQuery SQL ([`spec/shapes.md`](spec/shapes.md)): legacy ADK, OTel GenAI inline, and OTel GenAI by reference (`file_data` URIs stay in place). Non-conforming frameworks, coding SDKs, and custom gateways normalize via the Collector Port.

## ADR-012: View-Only Silver Default with Optional Acceleration Mid-Layer *(Amended by `ADR-026`, `ADR-031`, `ADR-035`)*
- **Decision**: Silver defaults to pure SQL views (`light`); high-volume deployments back the exact same view names with bounded incremental partitioned tables (`enterprise`).

## ADR-013: Typed Silver Views and `tools` Table *(Amended by `ADR-019`, `ADR-020`, `ADR-023`, `ADR-032`)*
- **Summary**: Established typed Silver views (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`) and independent factual `tools` flags; `requests` moved to the capacity extension (`ADR-023`).

## ADR-014: Non-Blocking Tool-Call Counts in Silver; Loop Rules in Gold *(Amended by `ADR-020`)*
- **Summary**: Kept tool-call/repeat counts in Silver and thresholds/pass rules in Gold.

## ADR-015: Standard-First Telemetry Attributes (OTel + `EVAL-P11` + Minimal `bq_eval.*`) *(Amended by `ADR-039`)*
- **Decision**: Read OTel (`v1.45+`), OTel GenAI (`v0.1+`), MCP, A2A, and cloud attributes verbatim, preserve `EVAL-P11` `outcome.*`, and confine non-standard keys strictly to `bq_eval.*` ([`spec/attributes.md`](spec/attributes.md)).

## ADR-016: Turn-Scoped `trace_payloads` Delta Contract *(Amended by `ADR-020`, `ADR-027`)*
- **Summary**: One `trace_payloads` row per content-bearing turn splitting `prior_history` from turn deltas (`turn_prompt`, `turn_context`, `turn_output`, `step_outputs`, `media_uris`).

## ADR-017: In-Cloud Identity Residency & Zero Hot Stores *(Amended by `ADR-021`, `ADR-028`, `ADR-029`, `ADR-031`)*
- **Summary**: Raw identities stay in-cloud; no hot external databases; `>= 5m` BigQuery floor; out-of-band KMS-HMAC hashes for cross-cloud joins.

## ADR-018: Call-Level Join/Lineage Keys & Operational-vs-Calibration Boundary *(Amended by `ADR-028`, `ADR-032`)*
- **Summary**: Added call-level join/lineage keys and separated operational `events` feedback from Gold blind SME calibration labels.

## ADR-019: Factual Tool Capabilities, Payload-Bound HITL Approvals & Scoped `capacity_pools` *(Amended by `ADR-028`, `ADR-029`, `ADR-032`)*
- **Summary**: Kept strictly factual capability flags in Silver `tools`, bound `outcome.approved` via `action_id` + `approved_args_sha256` (policies in Gold), split `allowed_models`/`capacity_pools`/`capacity_allocations`, and added versioned `capacity` exports.

## ADR-020: Silver Publishes Counts and Facts Only; Each Add-On Defines Its Own Pass
- **Decision**: Silver holds zero pass/threshold logic (`output_kind`, turn TTFT, wait sums, `is_settled`, slow flags live in Gold). Evaluation's default: completed turn = root OK and not `app_failed`; verified outcome = every trace completed (or closing trace when retries are expected).

## ADR-021: Read Horizons, a 5-Minute Floor and SLA Clocks from Trace Start *(Amended by `ADR-035`, `ADR-037`)*
- **Decision**: Deploy `light` Silver SQL per read horizon (`start_time` filter inside `spans`); Gold keeps daily results. Missing-user checks run on `>= 5m` schedules (under-1m guards belong to request-path gateways/apps); SLA clocks start at trace start.

## ADR-022: One Input Table for Non-Span Records, Authority by Table, Approvals Carry Arguments *(Amended by `ADR-034`, `ADR-041`)*
- **Decision**: Non-span records enter via `workflow_events`, deployed twice in isolated perimeters (systems of record = authoritative; applications = non-authoritative). Approvals carry `approved_args`; Silver computes hashes via RFC 8785 JCS.

## ADR-023: One-Domain Core; Clouds, Capacity and Governance as Modular Extensions *(Amended by `ADR-028`, `ADR-030`, `ADR-031`)*
- **Summary**: Kept single-domain core modular while isolating multi-cloud/multi-region federation ([`spec/clouds.md`](spec/clouds.md)) and gateway/capacity governance ([`spec/capacity.md`](spec/capacity.md)) in dedicated extensions.

## ADR-024: Long Text by Reference, Completeness Facts, Reference Data & Access *(Amended by `ADR-026`–`ADR-031`)*
- **Summary**: Content by reference for oversized spans, per-part `read_whole` flags, tool-flagged `turn_context`, and fail-closed `sampling_probability`.

## ADR-025: Six Row Rules *(Amended by `ADR-026`, `ADR-029`, `ADR-032`, `ADR-033`, `ADR-034`, `ADR-037`)*
- **Summary**: Canonical reply text, confidence rollup (`ADR-033` adds `min_model_confidence` default on `outcomes`), agent attribution, strict child/`mcp.session.id` tool folding, turn-prompt delta rule, and attested requester identity (`ADR-032` bans unattested fallback on `RL-1`–`RL-3`).

## ADR-026: Split Open/Restricted Datasets, Single-Pass `spans` & Dual `light`/`enterprise` Profiles *(Amended by `ADR-031`, `ADR-035`, `ADR-038`)*
- **Decision**: Split Silver into physically separate `silver_open` and `silver_restricted` Authorized Datasets; route shapes in one `SELECT` pass over `_AllSpans` (deduplicating ADK Format 1 `call_llm`+`generate_content`); keep `light` views-only default alongside optional `enterprise` incremental tables behind identical view names.

## ADR-027: BigLake Hive-Partitioned Shape 3, Risk-Tiered Async Uploads & Execution-Time RFC 8785 Hashing *(Amended by `ADR-036`, `ADR-038`, `ADR-041`)*
- **Decision**: Require BigLake (`WITH CONNECTION`) + Hive date/hour partitions with static horizon predicates (micro-batched Parquet/Iceberg canonical at scale); run risk-tiered async uploads (`regulated_worm` fail-closed at proxy); emit execution-time RFC 8785 `bq_eval.args_sha256` on tool spans so `*_open` views never scan restricted buckets.

## ADR-028: `1:N` Gateway `requests`, SCD-2 Governance, Hub Transfer Port & Upstream Collector Port *(Amended by `ADR-031`, `ADR-032`)*
- **Decision**: Price each `1:N` `requests` attempt before aggregating to `model_calls` (separating `gateway_failover_attempts` from `caller_retry_count`); add `burst_metrics_1m` and SCD-2 validity windows; stage non-GCP open rows via a scheduled Hub Transfer Port with KMS-HMAC hashes and In-Cloud Judge Port; normalize non-conforming SDKs via the Collector Port.

## ADR-029: Fail-Closed Completeness, Attested 3-Principal OBO, Guardrail Separation & WORM Audit Snapshots *(Amended by `ADR-031`, `ADR-032`, `ADR-034`, `ADR-037`, `ADR-043`)*
- **Decision**: Keep `sampling_probability = NULL` fail-closed; alert on rootless mutating traces; fail closed on unregistered tools in regulated workflows; require write-IAM provenance for `identity_attested`/`args_hash_attested`; bind `tool_name` + executed args in RFC 8785 hashes; enforce SoD/TTL/single-use and `action.committed` verification; add `guardrail.intervention`, `rejected | denied`, and WORM audit snapshots.

## ADR-030: Typed Non-LLM Spans, Optimizer Gold Port, Dual-SME MRM Rigor & Enterprise Use Cases
- **Decision**: Project `embeddings` onto `model_calls`, retrieval/memory provenance + warehouse compute onto `tool_calls`, `system_instructions`/`tool_definitions` onto `trace_payloads`, optimizer lineage onto `traces`, and `outcome.linked` onto `events`; add Optimizer Gold Export Port, dual-SME adjudication, calibration expiry, PT eval isolation, and slot backpressure.

## ADR-031: Multi-Region Hub Default, Location Alignment, Multi-Project Security, Bounded `MERGE` & GCS WORM/RTBF *(Amended by `ADR-035`)*
- **Context**: Mixing `US` multi-region and regional `us-east4` breaks BigQuery joins and KMS encryption; single-region hubs break zero-copy joins over multi-region `_AllSpans`/`_AllLogs`/Billing exports, lose billing backfill, and restrict Omni `CTAS` + `AI.GENERATE_TABLE` availability. Meanwhile, multi-project estates need split compaction SAs and per-BU Row Access Policies; `outcome.subject` in `silver_open` leaks business keys; BigQuery tables lack native SEC 17a-4 WORM locks and collide with GDPR/CCPA RTBF.
- **Decision**:
  1. **Multi-Region (`US`/`EU`) Hub Default + Location-Alignment Invariant**: Default the Hub to `US` or `EU` multi-region (regional inference pools stay regional since BigQuery is async `>= 5m`). Mandate strict location + KMS CMEK alignment within every processing domain (never mix `US` and `us-east4`); treat non-hub sovereign/single-region GCP perimeters as spokes via the Hub Transfer Port.
  2. **Multi-Cloud Port Mechanics**: Non-GCP Bronze parity, private Hub Transfer transport with idempotent batches and skew watermarks, cross-cloud KMS-HMAC, in-VPC judge compute and On-Prem relays, as specified in [`spec/clouds.md`](spec/clouds.md).
  3. **Perimeter & `MERGE` Hardening**: Move `outcome.subject` out of `outcomes_open` into restricted `outcomes`; split `enterprise` compaction into separate open-tier and restricted-tier SAs; add per-BU/tenant RAPs on `silver_restricted`; mandate static target `DATE(start_time)` partition pruning on every `enterprise` `MERGE`, bounded `outcomes`/session lookbacks, and concrete `max_bytes_billed` + `INFORMATION_SCHEMA.JOBS` slot circuit breakers.
  4. **PII-Free GCS Object-Lock WORM + RTBF**: Back regulated Gold Audit Snapshots with PII-free **GCS Bucket Retention-Lock + CMEK** archives (`snapshot_sha256`, `calibration_snapshot_id`, input digests) paired with per-subject crypto-shredding DEKs for RTBF.

## ADR-032: Preventive Request-Path Port, Two-Speed Registry Sync, `< 5s` Local/CI Runner & Gateway/FinOps Hardening *(Amended by `ADR-034`, `ADR-039`)*
- **Context**: `>= 5m` BigQuery SQL cannot enforce live blocking, `< 5s` PR gates, or 800-rollout sweeps; manual SCD-2 updates deadlock `300x/day` delivery while uninstrumented agents evade inventory; self-reported `protected_block_sha256` violates one-way trust; `65,533 B` OTLP drops can hide `execute_tool` spans; `gen_ai.response.id` joins break on `429` legs; 3P missing `TrafficType` claimed false determinism.
- **Decision**:
  1. **Preventive vs. Detective & `< 5s` Local/CI Runner**: Codify the **Preventive Request-Path Enforcement Port** (gateways/proxies block live writes, DLP, `allowed_models`, `1s/10s` `429` shedding, `optimize` daytime PT, and stream `100 MB` attachments via Gateway Inline-Artifact Port emitting structured `guardrail.intervention` `control_id`/`layer`/`action`). Promote the offline SQL translator to a first-class **`< 5s` Local/CI Eval Runner Port** (`CI_ONLY`) for DevAI PR gates and optimizer sweeps.
  2. **Two-Speed Registry, `skill_registry` & Identity**: Add SCD-2 `skill_registry` pinning authoritative `protected_block_sha256`; split `tools.modifies_workspace` from `writes_state`; add the Two-Speed **CI/CD Dual-Registry Ingestion Port** (auto-sync `RL-3`–`RL-5`, dual-signed `CODEOWNERS`/MRM gates for `RL-1`/`RL-2` and flag downgrades) + cloud-registry dark-agent reconciliation; ban unattested `user.id` fallback on `RL-1`–`RL-3` and replace bare `identity_attested` with structured OBO claims + `authz_mode`; add reverse `action.committed -> tool_calls` orphan-commit checks.
  3. **Custom Proxy `1:N` & 3-Pool FinOps (`ADR-001` Preserved)**: Mandate `x-client-request-id` + `(trace_id, span_id)` `1:N` joins, caller `trace_owner_app_id` precedence, Option A/B neutral `state_mode`, core `model_calls` `thought_signature_present`/`upstream_endpoint_location`/`skill_sha256`/`caller_retry_count`/`tool_definitions_sha256`, honest 3P `tier_source = 'inferred_ambiguous'`, 3-pool `FOCUS 1.2` chargeback with invoice reconciliation, `burst_metrics_1m` and extended `allowed_models`, as specified in [`spec/capacity.md`](spec/capacity.md). Keep live PT actuation in `capacity`.

## ADR-033: `SR 11-7` Stratified Calibration, Stateful Memory Reconstruction, Guardrail FP/FN & 11-Workload Use-Case Split
- **Context**: Pooled-strata Lang-Reiczigel CIs bias prevalence when $Se/Sp$ varies across oversampled tails; quadratic `AC2` masks `2`-vs-`3` pass/fail flips; unpinned judges fail reproducibility; `previous_interaction_id` left Turn-`k` judges blind to prior disclosures/memory; skipping blocked turns blinded guardrail FP monitoring; conflated `UC7`/`UC8` hid DevAI, fiduciary advisory, and shared-gateway requirements.
- **Decision**:
  1. **Statistical & Judge Rigor**: Upgrade Gold calibration to stratum-specific $Se_h/Sp_h$ (and per-cloud $Se/Sp$) Rogan-Gladen + design-based joint CIs + $[0,1]$ clamping; pair `AC2 >= 0.80` with binary pass/fail cut-point agreement; set `RL-1` (`>= 100`) sample floors + SME tie-break; pin deterministic independent judges (`temperature = 0`, `seed`, snapshot ID, prompt hash, `zdr_allowed`); expand `CALIBRATED` expiry to `system_instructions`, `tool_definitions_sha256`, `agent_version`, `corpus_digest`, and judge prompt hash; default multi-trace outcomes to `min_model_confidence`.
  2. **Stateful Memory, Guardrail FP/FN & Reconciled Compute**: Add `memory_op`/`memory_id`/`memory_sha256`/`memory_source_provenance`, `state.transition`, bounded Gold session-context reconstruction (fail-closed `MISSING_SESSION_CONTEXT`), PII-masked judge prompts, guardrail FP/FN stratified judging, MNPI `barrier_domain` + silent empty-RAG alerts, `shadow_of_trace_id` live A/B pairing, `nudge_id` attribution, `outcome_links` M:N bridge, `db.query.job_id`/`INFORMATION_SCHEMA.JOBS` warehouse reconciliation, delayed `T+1`/`T+7..30d` ground-truth joins, and a sibling non-LLM inference metric table.
  3. **11-Workload Use-Case Taxonomy**: Split `UC7` into SRE RCA (`UC7`) and Enterprise DevAI (`UC8`), split old `UC8` into Stateful Fiduciary Advisory (`UC9`) and Multi-Cloud Regulated Fleet (`UC10`), and add Shared Multi-Tenant LLM Gateway & Bulk Embeddings (`UC11`).

## ADR-034: Silver Keeps What It Saw; "Verified" Comes From Provenance
- **Decision**: Silver publishes the user id it saw, the root span's, with `user_id_source`; Gold decides which id is the requester when a platform-class span carries a different one and, per tier, whether an unverified id counts. A record's writer class (`system_of_record | platform | application`) is set by where it landed, and `identity_attested`, `args_hash_attested` and `traffic_type_attested` are derived from that class, never read from an attribute. Regulated deployments give gateway and tool-proxy spans a landing place applications cannot write to. Control verdicts are platform-verified on such a span or in a third, platform-only copy of `workflow_events`. The cloud runtime's own spans are not platform-class.

## ADR-035: Explicit Read Windows; High-Volume Profile Is Design Only
- **Decision**: Every published object is a table function of a from-time and a to-time, with no stored data; fixed-window views are thin wrappers. A trace belongs to the window in which its first span starts, read with a margin so no turn is cut. A window has settled when its end, the margin and the settle time have passed: Gold keeps results from settled windows only, and only telemetry columns are repeatable from then on (registry columns need validity windows; `events` and `outcomes` are as known at read time). The `enterprise` profile is design only: the candidate stores a typed, text-free copy of spans under the same functions; a side-by-side test against merged tables decides.
- **Untested**: bytes scanned through a function; authorizing functions on Bronze.

## ADR-036: Content Is Required; Calls Without It Are Kept, Not Scored *(Amended by `ADR-042`)*
- **Decision**: Shape routing stays keyed on content, and every shape carries content on the step or by reference. A model or tool call without content stays in the text-free objects with a content-missing fact: no text row, not scored by evaluation, counted by cost and monitoring. Silver defines no zero-payload profile. The kind of a span (model call, embedding, tool call, retrieval, memory, agent, other) is read from its operation name. Spans with no GenAI operation are ordinary: kept, counted, not flagged.
- **Rejected Options**: routing by name family; counting every unmatched span as unknown shape; excluding calls without content.

## ADR-037: Root, Turn and Clock
- **Decision**: The root of a trace is the first span, by start time, that an application emitted; the cloud runtime's own spans do not count. So the user id must be stamped on that span. A turn is a trace with at least one agent, model-call or tool span; other traces are kept and are not turns. The SLA clock starts at the earliest stored span, with a fact saying whether the runtime or the application wrote it. Silver publishes why a trace is incomplete (a parent missing from the store, a trace longer than the read margin, the last arrival time); Gold's orphaned-activity alert uses the missing parent only, once the trace has settled.
- **Measured**: a server-kind orphan appears at every service hop, so it shows no loss.

## ADR-038: Open Twins Carry No Hash Material
- **Decision**: Argument hashes, approval and commit hashes and file digests are published in `silver_restricted` only: a hash of a tool name plus a guessable id can be reversed, and a known file's digest reveals its upload. `tool_calls_open` carries a repeat number per trace, equal for calls with the same tool and arguments; repeats across an outcome are counted in Gold from the restricted views.
- **Rejected Options**: keyed hashes; a key hashed from the trace or outcome id, reversible by guessing.

## ADR-039: Bounded Columns, Labels and Column Corrections
- **Decision**: A value gets its own Silver column only if Silver computes with it, or it defines a row's identity, a join or access; everything an add-on only slices by travels in one `labels` column with registered keys. This rule governs: the core-column list in `ADR-032` is provisional until the column files exist. `model_calls` keeps the client request id and the provider response id in two columns, and the requested and the responding model in two columns. The retry count has one name, `caller_retry_count`, defined in [`spec/silver.md`](spec/silver.md). The cache-write split by lifetime is not published until an attribute carries it. `traces.user_turn_count` above 1 marks a trace with more than one user turn.
- **Open**: which existing columns move to `labels`.

## ADR-040: Application Identity Through the Registry, Bound to the Landing Place
- **Decision**: `app_registry` maps telemetry identities to applications: an App Hub id, or a service name plus environment. No match stays unregistered; nothing is invented. The registry also lists where each application's spans must land; an identity claimed from elsewhere is published as not bound. Where a rule depends on the tier, Gold treats unbound and unregistered applications as the strictest tier. Inside one landing place identity stays self-declared.

## ADR-041: One Hashed Object; Declared Hash Source
- **Decision**: The hashed object is the tool name plus the arguments exactly as the tool received them; the model's raw text is not used. Approval and commit rows both carry that object in one column, `action_args` (formerly `approved_args`), and Silver computes their hashes; writers of that table never send hashes. The tool-side hash names its source: the tool proxy, Silver from the tool span's own arguments, or none. It is never computed from the model's message, and every implementation must pass one shared set of test cases. Within one copy of `workflow_events` the first row by event time with an `event_id` counts; a repeat with different content is counted as a conflict.
- **Rejected Options**: ready-made hashes from systems of record; a reduced per-tool object; new columns.

## ADR-042: Tokens Mean a Model Call; Duplicate Spans Keep the Latest
- **Decision**: A span that reports tokens above zero is a model call, whatever its operation name. Where such a span encloses another, only the innermost is a model call; the enclosing span's tokens are a rollup that is never summed. `spans` keeps one row per `(trace_id, span_id)`, the latest arrival, and counts the rest.
- **Measured**: the listed model operations covered `35%` of input tokens; no enclosing token span reported more than its inner spans; `791` span keys arrived twice.
- **Open**: a tool span that reports tokens; an unknown operation name without tokens.

## ADR-043: Declared "Full" Is Not Scaled
- **Decision**: Gold does not scale counts or cost for an application the registry declares `sampling_mode = full`. Otherwise a missing sampling probability leaves the scaled number empty, with the observed number beside it. Silver is unchanged.
- **Measured**: no recorded application span carried a sampling probability.
