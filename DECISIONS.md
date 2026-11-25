# Architectural Decisions (`DECISIONS.md`)

## ADR-001: Repository Separation & Cross-Repo Boundaries (`bq-eval` vs. `agent-evals` vs. `capacity`)
- **Context**: Combining a runnable BigQuery evaluation, monitoring, and cost toolkit with Financial Services Model Risk Management (`SR 11-7`, `SR 26-2`) regulatory playbooks, academic literature surveys, and GSU/GPU capacity actuation in one repository blurred build and compliance boundaries.
- **Decision**: Split responsibilities across three repositories that never reach into each other's internal views:
  1. **`bq-eval` (`tvaroska/bq-eval`)**: Owns the BigQuery Bronze and Silver core ([`spec/silver.md`](spec/silver.md)) plus optional Gold add-ons for evaluation, monitoring, and cost ([`spec/layers.md`](spec/layers.md)).
  2. **`agent-evals`**: Owns normative cross-repo evaluation standards (`EVAL-P01`–`EVAL-P11`), regulated banking Model Risk Management playbooks, audit governance gates, academic compendiums, and cross-workspace compliance auditing (`eval-audit`).
  3. **`capacity`**: Owns GSU/GPU sizing optimization, sub-minute burst physics, gateway routing policies, and `FOCUS 1.2` billing actuation over exported burndown payloads (never naming `bq-eval` views directly).
- **Rejected Option**: Monolithic single-repo bundling MRM regulatory playbooks, gateway actuation, and BigQuery SQL together.

## ADR-002: Progressive Zero-Table Step 1 → Step 3 View Ladder *(Superseded by `ADR-010`)*
- **Summary**: Earlier design priced spans inside zero-table Step-1 views with Step 2 and Step 3 layered on top. Replaced by the price-free Silver core with optional Gold add-ons.

## ADR-003: Cross-Cloud Collector Forwarding & `'unregistered_app'` String Defaults *(Superseded by `ADR-010`)*
- **Summary**: Earlier design forwarded multi-cloud spans into GCP through a collector and substituted `'unregistered_app'` string defaults. Under `ADR-010`, spans stay in their cloud and missing values stay `NULL`.

## ADR-004: Fail-Closed Rogan-Gladen Calibration, Rule-Check Pre-Screening & Gwet's `AC2` (Gold Evaluation)
- **Context**: Scoring deterministic rule failures with an LLM wastes judge tokens, pooling `100%`-sampled failure tails with `5–10%`-sampled healthy canaries biases raw judge pass rates by `10x–20x`, and defaulting `judged_samples == 0` to `1.0` fabricates `100%` pass rates.
- **Decision**:
  1. **Rule-Check Pre-Screening**: Routine semantic judging skips traces that failed Silver behaviour checks, sampling `5–10%` of passing traces plus `100%` of slow/throttled passing tails; optional diagnostic sweeps may inspect failing traces separately.
  2. **Conditional Prevalence Calibration & 100% Judge Billing**: Estimate $\hat{\theta}_{\text{RG}} = \hat{P}(\text{Semantic Pass} \mid \text{Silver Pass})$ strictly over rule-passing judged traces weighted by population sampling stratum (`StratPPI`), while billing `100%` of judge runs in Gold cost.
  3. **Fail-Closed `NULL` Guards**: Report `NULL` (`UNJUDGED` or `UNCALIBRATED`) whenever `judged_samples == 0`, any populated stratum has zero judged rows, `judged_samples < 10`, reference counts `(tp + fn) < 5` or `(tn + fp) < 5`, or Youden's $J$ or Wilson-adjusted $J' \le 10^{-6}$.
  4. **Boundary-Safe Intervals & Agreement**: Report **Lang & Reiczigel (2014) Eq. (6)/(7)/(16)/(17)** 95% confidence intervals (with center shift $\Delta P$ and Kish effective $n$) alongside prevalence-robust Quadratic **Gwet's `AC2`**.
- **Rejected Option**: Judging 100% of traces unconditionally, pooling rule-rejected traces into $\hat{\theta}_{\text{RG}}$, or defaulting unjudged/uncalibrated workloads to `1.0`.

## ADR-005: Portable, Customer-Agnostic Repository Hygiene
- **Context**: Public repositories must be safe to clone, build in isolated Cloud Build containers, and share externally without leaking customer identifiers, internal shortlinks, or workstation paths.
- **Decision**: Enforce automated AST and text hygiene gates (`tests/test_repo_hygiene.py`, `tests/test_isolation_guard.py`) blocking internal codenames, internal shortlinks, hardcoded workstation paths, local file URI schemes, `.github/workflows/`, escaping relative Markdown links, and forbidden upward-layer imports.
- **Rejected Option**: Manual review checklists without automated CI AST and regex gates.

## ADR-006: Outcome-Adjusted Cost, `>200K` Cliffs & Zero-Fabrication Model Selection (Gold Cost)
- **Context**: Raw token totals hide whether an agent finished the business job, miss `>200K` long-context pricing cliffs (`2.0x` input / `7.5x` output on 1P Gemini), ignore `1/N` prefix-cache dilution, and cannot tell if a cheaper model breaks passing cases.
- **Decision**: In the Gold cost add-on, separate spend into 6 traffic buckets (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`, `optimize`, `unclassified`), price each model call before aggregating to traces and outcomes, divide total spend (including failed/escalated attempts and judge overhead) by verified outcomes, enforce `0.0%` negative flips on regulated/safety gates (`UC5`), and evaluate cascaded model/threshold switches (`UC4`) on combined `(primary + escalation + eval)` cost above the declared calibrated accuracy floor.
- **Rejected Option**: Trace-averaged token dashboards or accuracy-unconstrained model downgrades.

## ADR-007: Dedicated Capacity & `FOCUS 1.2` Chargeback (Gold Cost)
- **Context**: `8x5` business-hours workloads active `40h/week` are penalized `3x` under flat `24x7` Provisioned Throughput allocations, while overnight reserved capacity sits idle.
- **Decision**: Attribute dedicated-capacity commitments in Gold cost using `FOCUS 1.2` multi-pool rules (splitting active production use, off-peak eval/optimize backfill, platform idle capacity, and involuntary vs. burst PayGo overflow against each application's funded share) and schedule batch evaluations into idle off-peak capacity valleys.
- **Rejected Option**: Uniform `24x7` flat reservation splits across tenants.

## ADR-008: Dual-Path `BQAA` `agent_events` + `_AllLogs` Switchboard *(Superseded by `ADR-011`)*
- **Summary**: Earlier design kept parallel adapters over `BigQueryAgentAnalyticsPlugin` (`agent_events`) and `_AllLogs` payloads. Replaced by the three shapes in [`spec/shapes.md`](spec/shapes.md).

## ADR-009: Hand-Written Offline SQLite Runtime Twin *(Superseded by `ADR-010`)*
- **Summary**: Earlier design kept a hand-written SQLite mirror behind transpiled parity tests. Reversed by `ADR-010` so defects are not fixed twice.

## ADR-010: Bronze and Silver Core in BigQuery, Gold Add-Ons on Top *(Amended by `ADR-012`, `ADR-013`, `ADR-014`, `ADR-017`)*
- **Context**: Dual trace view chains fed different add-ons, priced every span, filled string defaults for missing values, pre-aggregated cross-cloud spans before joining, and duplicated Silver logic in a hand-written SQLite mirror (`ADR-009`).
- **Decision**: Core is Bronze (telemetry in place) plus Silver SQL in BigQuery next to the data, using telemetry and shared reference tables with zero prices, zero judge verdicts, `NULL` for missing values (counted, never defaulted), one column per check, and one-way trust on self-reported failures. Gold add-ons (evaluation, monitoring, cost) read only published Silver views ([`spec/`](spec/layers.md), [`design/`](design/bq-eval.md)). BigQuery SQL is the single Silver implementation; offline runs translate that SQL (`ADR-009` superseded for Silver).
- **Rejected Options**: Keeping either legacy priced/defaulted view chain; centralizing raw cross-cloud spans in the hub; maintaining a separate hand-written SQLite copy.
- **Consequences**: The SQL translator backs offline demo/tests (BigQuery is authoritative); split cross-cloud traces stay local fragments; judging runs in the cloud holding trace text.

## ADR-011: At Most Three Telemetry Shapes, One Adapter View per Shape, Content by Reference
- **Context**: Frameworks emit telemetry under varied attribute names and placements; supporting every combination requires tens of readers, while large prompts and files exceed span attribute limits.
- **Decision**: Silver accepts at most three shapes ([`spec/shapes.md`](spec/shapes.md)): ADK's legacy step attributes, OpenTelemetry GenAI step attributes, and OpenTelemetry GenAI with content by reference in storage. One BigQuery adapter view per shape maps to common `spans` columns; `file_data` storage URIs stay in place and project to `trace_payloads`; high-volume/agent-batch Shape 3 setups require a non-dropping uploader. Any new shape replaces one of the three.
- **Rejected Options**: Per-framework readers; pre-storage collector rewriting (loses raw data); single inline shape only.
- **Consequences**: ADK's legacy shape retires when ADK drops it; content by reference adds an external-table join and second retention policy; unsupported shapes convert before landing.

## ADR-012: View-Only Silver Contract with Optional Acceleration Mid-Layer (No Mandatory Date Pruning)
- **Context**: Day-partitioned Silver tables (`ADR-010`) orphaned multi-day outcomes and added DML lag, while mandatory narrow date bounds on plain views severed multi-day cases (UC2 `>24h`, UC5 HITL) and BigQuery Materialized Views cannot read Linked Datasets (`_AllSpans`), External Tables, self-joins, or `CURRENT_TIMESTAMP()`.
- **Decision**: Silver publishes **SQL views** with **no mandatory Gold date-range pruning**, zero default telemetry copies, and dynamic `is_settled` checks. High-volume deployments may insert an **optional native staging/acceleration mid-layer** (or Gold rollup) beneath Silver without changing published view contracts.
- **Rejected Options**: Mandatory narrow date pruning on Silver views; Day-1 scheduled Silver tables everywhere; assuming native Materialized Views work over `_AllSpans` and external tables.
- **Consequences**: Multi-day outcomes and sessions stay whole by default, spoke and SQLite runs remain pure `SELECT` views, and long-term audit retention lives in Bronze or Gold outputs.

## ADR-013: Typed Silver Views (`model_calls`, `tool_calls`, `events`) and `tools` Table *(Amended by `ADR-019`, `ADR-020`, `ADR-023`)*
- **Summary**: Established typed Silver views (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`) and independent `tools` capability flags, removing raw `spans` access for add-ons; `requests` moved to the capacity extension (`ADR-023`) and wait sums moved to Gold (`ADR-020`).

## ADR-014: Non-Blocking Tool-Call Counts in Silver; Loop Rules in Gold *(Amended by `ADR-020`)*
- **Summary**: Kept tool-call and repeat counts non-blocking in Silver with loop budgets in Gold; `ADR-020` later removed `overall_pass` from Silver completely.

## ADR-015: Standard-First Telemetry Attributes (OTel + `EVAL-P11` + Minimal `bq_eval.*`)
- **Context**: Bespoke attribute names for concepts already standardized in OpenTelemetry fragment instrumentation.
- **Decision**: Read OTel (`v1.45+`), OTel GenAI (`v0.1+`), MCP, A2A, and cloud attributes verbatim (`user.id`, `gen_ai.conversation.id`, `gen_ai.response.time_to_first_chunk`, `gen_ai.evaluation.result`, `deployment.environment.name`), keep `EVAL-P11` `outcome.*`, and confine non-standard keys to `bq_eval.*` ([`spec/attributes.md`](spec/attributes.md)). Agent-batch runs emit normal OTel spans with `bq_eval.serving_tier = FLEX`.

## ADR-016: Turn-Scoped `trace_payloads` Delta Contract *(Amended by `ADR-020`, `ADR-027`)*
- **Summary**: Defined one `trace_payloads` row per content-bearing turn splitting `prior_history` from `turn_prompt`, `turn_context`, `turn_output`, `step_outputs`, and `media_uris`. `ADR-020` moved `output_kind` to Gold; `ADR-027` replaced Shape-3 JSONL hashing on `*_open` views with execution-time RFC 8785 JCS `args_sha256` and BigLake Hive-partitioned storage.

## ADR-017: In-Cloud Identity Residency & Zero Hot Stores *(Amended by `ADR-021`, `ADR-023`, `ADR-025`, `ADR-028`, `ADR-029`)*
- **Summary**: Kept raw `user.id` / `tenant_id` in-cloud and banned separate hot databases; `ADR-021` set the 5-minute BigQuery floor, `ADR-028` moved cross-cloud hashes to KMS HMAC + Hub Transfer Port, and `ADR-029` upgraded identity to attested 3-principal OBO.

## ADR-018: Call-Level Join/Lineage Keys & Operational-vs-Calibration Boundary *(Amended by `ADR-028`)*
- **Summary**: Added per-call `span_id`/`request_id`, agent/skill/outcome/case/page/confidence projections, and kept OTel `gen_ai.evaluation.result` operational feedback in Silver `events` while confining blind SME calibration labels strictly to Gold. `ADR-028` upgraded `requests` reconciliation from `1:1` to `1:N`.

## ADR-019: Factual Tool Capabilities, Payload-Bound HITL Approvals & Scoped `capacity_pools`
- **Context**: Threshold-based HITL rules ("credit `> $500` requires sign-off", `UC5.1`) are Gold policy rules rather than static Silver tool flags (`requires_approval`), while unbound `outcome.approved` events allow post-approval parameter swaps and self-reported `runbook_id` violates one-way trust (`UC2.2`). Meanwhile, Provisioned Throughput reservations are scoped to real cloud pools (GCP project / Shared-PT pool, Azure PTU deployment, AWS PT ARN + model + region + period) rather than one global `(model, region)` row, and `capacity` cannot read Silver views directly (`UC2.7`).
- **Decision**:
  1. **Factual `tools` & bound approvals**: Drop `requires_approval` from Silver `tools`, keeping strictly factual capabilities (`writes_state`, `executes_code`, `sends_external_reply`, `returns_pii`, `creates_ticket`). Bind authoritative `outcome.approved` rows in `events` via `action_id` and `approved_args_sha256` (`bq_eval.action_id` + `args_sha256` on `tool_calls`), evaluate approval-trigger policies in Gold, and verify runbook matches from `tool_calls` results rather than self-reported `bq_eval.runbook_id`.
  2. **Governance vs. `capacity_pools` & `capacity` export**: Split `model_capacity` and `app_registry` capacity scalars into `allowed_models` (`provider, model`), `capacity_pools` (`pool_id, period -> cloud, scope_key, model, region, committed_units`, matched via `bq_eval.pool_id` or `cloud.account.id`), and `capacity_allocations` (`pool_id, period, consumer_app_or_unit -> funded_share`), and publish a versioned Gold burndown/latency export contract for the `capacity` repository (`UC2.7`).
- **Rejected Options**: Static `tools.requires_approval` in Silver; timestamp-only approval matching; single global `(model, region)` capacity table or flat per-app share scalar.
- **Consequences**: Silver remains 100% policy-free facts, post-approval parameter tampering is detectable in SQL, multi-project/multi-cloud PT pools and BU shares attribute accurately, and `capacity` sizes SLOs without coupling to Silver views.

`ADR-020`–`ADR-025` amend `ADR-007` and `ADR-010`–`ADR-019`; the later decision holds.

## ADR-020: Silver Publishes Counts and Facts Only; Each Add-On Defines Its Own Pass
- **Context**: Failing a trace on any errored span or failed tool call kept recovered turns from being judged or verified, the mistake `ADR-014` removed for loops.
- **Decision**: Silver has no pass and no threshold. `traces` carries counts (errored spans, failed and repeated tool calls, throttled calls) and facts (root span ended in error, application reported failure). `output_kind`, trace-level first-token time, wait sums, `is_settled` (last arrival time instead) and the slow flag with its latency target move to Gold. Evaluation's default: a turn is completed when its root span ended without error and the application reported no failure; an outcome is verified only when every trace completed, or only the closing trace where the workload declares retries as expected.
- **Rejected Options**: A minimal pass in Silver; recovery detection in Silver.
- **Consequences**: No Silver row depends on reading time; add-ons may disagree on what passes.

## ADR-021: Read Horizons, a 5-Minute Floor and SLA Clocks from Trace Start
- **Context**: A reader's time filter cuts no scan on views that group spans, and BigQuery schedules run at most every 5 minutes.
- **Decision**: The same Silver SQL is deployed once per read horizon, with a lookback applied to spans before grouping (for example 2 hours, 3 days, everything); Gold keeps its own daily results. The missing-identity check runs on a 5-minute schedule; an under-1-minute guard is an application duty in the request path. SLA clocks start at trace start, and starting the trace when the work arrives is the application's duty.
- **Rejected Options**: Table functions; a stored layer by default; an always-on poller; a mail-system clock.
- **Consequences**: Still views only; a turn cut by a horizon edge has no root span and is skipped.

## ADR-022: One Input Table for Non-Span Records, Authority by Table, Approvals Carry Arguments
- **Context**: `events` had no input contract, no committed writes or human handling, and no definition of the bytes behind `approved_args_sha256`.
- **Decision**: Non-span records enter through one append-only table with fixed columns (`workflow_events`), deployed twice: one copy only systems of record may write (authoritative), one for applications. The only other reader is the OTel feedback event (`ADR-018`). Added event types: `action.committed`, `handling.started`, `handling.ended`. An approval row carries the approved arguments, and Silver computes both hashes with the same SQL.
- **Rejected Options**: Per-system adapters in the core; an authority column trusted as written; a canonical byte form implemented outside.
- **Consequences**: Systems of record need a write path into BigQuery or a mapping view the deployment owns.

## ADR-023: One-Cloud Core of Six Views; Clouds, Capacity and Governance as Extensions
- **Context**: No target use case needs a second cloud, dedicated capacity or the model allow-list, yet they shaped every core object.
- **Decision**: The core is one cloud with six views (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`) and two reference tables (`app_registry`, `tools`). Hub reads, hashed ids and fragments move to `spec/clouds.md`; "text stays in its cloud" stays in the core. `requests`, `capacity_pools`, `capacity_allocations`, `allowed_models` and `bq_eval.pool_id` move to `spec/capacity.md`; the user directory becomes a Gold join. `bq_eval.runbook_id` is dropped. First-token time is an application duty (stamp the standard attribute on the model-call span).
- **Rejected Options**: Keeping all of it in the core; removing the extensions from the documents.
- **Consequences**: Cost uses application-reported tokens until the capacity extension is installed.

## ADR-024: Long Text by Reference, Completeness Facts, Current-State Reference Data, Per-View Access
- **Context**: Span text is size-limited, uploads can drop, sampling hides "never" violations, and nothing said who may read text.
- **Decision**: An application where one prompt, answer or tool result can exceed the limit for one attribute uses content by reference, under a written upload contract with one reference hook that does not drop. Each text part says whether it could be read whole. `turn_context` holds every tool result, tagged with its tool flags. Silver carries the sampling probability (trace state, else one attribute) and counts spans with a missing parent; Gold scales counts and cost, and "never" checks need every trace. Reference tables stay current-state, and Gold stores the values and population counts it used. One dataset per horizon: each view with sensitive columns has a twin without them, and access is granted per view.
- **Rejected Options**: Waiting for the size test; a library per framework; validity periods; two datasets; column tags (views cannot carry them).
- **Consequences**: Whether a reader can use a view without access to the raw trace store is an open test.

## ADR-025: Six Row Rules *(Amended by `ADR-026`, `ADR-029`)*
- **Context**: Rows depended on rules no document stated.
- **Decision**: (1) Reply text is the arguments of the last tool call that sends an external reply, else the last model output under the root agent that asked for no tool. (2) Final confidence is the one reported by the last model call that reports any. (3) A call belongs to the agent it names, else to the nearest enclosing agent span. (4) A server tool span folds into a client tool span only as its child, never by name and arguments. (5) The turn prompt is the last input before the first model output, whatever its role, and a fact records what started the turn. (6) Requester identity uses attested 3-principal OBO with root-span `user.id` fallback (`ADR-029`).

## ADR-026: Split Open/Restricted Datasets, Single-Pass `spans` & Dual `light`/`enterprise` Profiles
- **Context**: Live BigQuery probes proved Authorized Datasets work over linked `_AllSpans`/`_AllLogs` and inside-horizon predicates prune `37x`, but co-locating open/restricted views weakens IAM/CMEK isolation, `UNION ALL` shape views scan `_AllSpans` `2x`, ADK Format 1 emits duplicate token counts on `call_llm` + child `generate_content`, and PiB-scale or >30d deployments need incremental compaction.
- **Decision**: Split Silver into separate `silver_open` and `silver_restricted` Authorized Datasets; route shapes in one `SELECT` pass over `_AllSpans` (keying ADK Format 1 vs. 2 on `llm_request`/`tool_call_args`, deduplicating `call_llm`+`generate_content`, accepting `generate_content` alongside `chat`); keep `light` views-only as default while formalizing an `enterprise` incremental partitioned-table profile behind identical view names.
- **Consequences**: Zero Gold SQL changes between `light` and `enterprise`; eliminates `2x` scan and token bugs.

## ADR-027: BigLake Hive-Partitioned Shape 3, Risk-Tiered Async Uploads & Execution-Time RFC 8785 Hashing
- **Context**: Live BigQuery probes proved plain GCS external tables fail Authorized Views (`400 accessDenied`), dynamic `_FILE_NAME` joins scan `100%` of files (`~100 ms` slot/file), `TO_JSON_STRING(PARSE_JSON(...))` fails on `100` vs. `100.0`/`0.0`, blocking hooks risk live outages, and Shape-3 JSONL hashing forced `*_open` views to scan restricted buckets and broke under zero-payload ZDR.
- **Decision**: Require BigLake (`WITH CONNECTION`) + Hive date/hour partitions with static horizon predicates (micro-batched Parquet preferred); move to risk-tiered async uploads (bounded buffer + drop counters for standard tiers; fail-closed audit enqueue for Tier-1 regulated writes; zero-payload ZDR profile supported); emit execution-time RFC 8785 (JCS) `bq_eval.args_sha256` on tool spans so `*_open` views never scan restricted buckets and match `approved_args_sha256` cleanly.
- **Consequences**: Fixes IAM isolation, ZDR compatibility, `100` vs. `100.0` hash mismatches, and GCS small-file scans.

## ADR-028: `1:N` Gateway `requests`, SCD-2 Governance, Hub Transfer Port & Upstream Collector Port
- **Context**: Gateway failover (`429 -> Priority`) makes `model_calls:requests` `1:N`; mutable reference tables rewrite history; BigQuery cannot live-`JOIN` Omni with GCP datasets; cross-cloud `AI.GENERATE_TABLE` leaks text; SQL-DDL salts leak directories; non-OTel SDKs need a clear path without bloating BigQuery SQL.
- **Decision**: Keep `capacity` and `clouds` modular extensions (`design/capacity.md`, `design/clouds.md`); aggregate `1:N` `requests` per call before joining; add SCD-2 validity windows (`effective_from`/`effective_to`) on `app_registry`, `tools`, `capacity_pools`, `capacity_allocations`, and scoped `allowed_models` (with Gold gate); stage non-GCP open rows via a scheduled Hub Transfer Port with KMS-HMAC hashes and cross-cloud split-trace correlation plus an In-Cloud Judge Port; add an upstream OTel Collector / Bronze Adapter Port gated on recorded traces.

## ADR-029: Fail-Closed Completeness, Attested 3-Principal OBO, Guardrail Separation & WORM Audit Snapshots
- **Context**: Live probes proved Cloud Trace silently drops `>= 65,533 B` spans inside `HTTP 200` `partialSuccess` batches (creating rootless traces); defaulting empty `sampling_probability` to `1.0`, skipping rootless tool traces, and allowing unregistered tools fail open; missing `guardrail.intervention` conflates safety blocks with crashes; `UC2`/`UC5` lacked `rejected`/`denied` status and WORM snapshot schemas.
- **Decision**: Keep `sampling_probability = NULL` fail-closed (`app_registry.sampling_mode = 'full'` required for "never" checks); alert on rootless traces with tool calls/writes; fail closed on unregistered tools in regulated workflows; enforce Maker-Checker SoD (`actor_id != user_id`), `approved_expires_at` TTL, and single-use approvals; adopt 3-principal OBO identity; add `guardrail.intervention`, `outcome.status` `rejected | denied`, and WORM Gold Audit Snapshots.
