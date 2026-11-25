---
type: atom
related:
  - "layers.md"
  - "silver.md"
  - "features.md"
  - "capacity.md"
  - "use-cases.md"
  - "../design/gold.md"
tags: [bq-eval, gold]
---

# Gold Add-Ons

> What the optional Evaluation, Monitoring, and Cost add-ons deliver on top of [Silver](silver.md), and the rules they obey.

## Shared rules

1. **Silver views and reference tables only:** Gold add-ons never read raw `spans`, shape adapters, or Bronze sources directly.
2. **Independent add-ons:** Each add-on (Evaluation, Monitoring, Cost) can be deployed on its own and never reaches into another add-on's internal views; only published Gold outputs (such as verified-outcome status for Cost) are shared.
3. **Each add-on defines its own pass:** Silver publishes counts and facts. What counts as completed, slow, looping or verified is decided here, per add-on and per workload.
4. **In-cloud text & PII boundary:** Any check or judge that inspects `trace_payloads`, raw tool arguments/results, event payload JSON, or raw `user_id` / `tenant_id` reads the restricted Silver views inside the cloud holding that data; everything else reads the open twins.
5. **SQL first:** Deterministic checks, aggregations, pricing, and calibration math run in BigQuery SQL (Python stored procedures inside BigQuery are used only where SQL cannot express the calculation).
6. **Results are kept:** An add-on reads the shortest Silver horizon that covers its question and stores its results day by day, together with the reference values and population counts it used, so a past result can be reproduced after reference data changes or Bronze expires.
7. **Sampled applications fail closed:** Counts and cost are divided by the sampling probability Silver carries (`NULL` never defaults to `1.0`). Checks that must never fire require `app_registry.sampling_mode = 'full'` and zero unexplained missing-sampling rows.
8. **Rootless write safety:** Routine baseline metrics skip turns with `has_root_span = false`, but any rootless trace containing tool calls or `writes_state = true` mutations routes to an explicit orphaned-activity safety alert — never silently dropped.

## 1. Evaluation add-on

- **Completed turns, guardrail interventions, and verified outcomes:**
  - A turn is **completed** when its root span ended without error and the application did not report failure. Safety/policy blocks (`guardrail.intervention` in `events` or span attribute) are classified separately from `bq_eval.app_failed` crashes so over-refusals and prompt-injection blocks are tracked explicitly rather than silently skipped or miscounted as crashes.
  - An outcome is **verified** only when every one of its traces completed, the deterministic gates pass, the traces are settled, `outcome.status = 'succeeded'`, and the semantic or human verification the workload requires is met. `outcome.status` also supports `rejected` and `denied` for pre-ticket rejections (`UC2`) and denied regulated actions (`UC5`).
  - A workload that declares retries as expected (batch extraction, model cascades) needs only the closing trace to complete.
  - **Regulated WORM Gold Audit Snapshots (`UC5`):** Rather than retaining raw prompts in Bronze for years (which conflicts with ZDR/data-minimization), regulated workloads persist immutable Gold audit snapshots containing `snapshot_id`, `snapshot_time`, `trace_id`/`outcome_id`, pinned `silver_sql_version`, `rubric_version`, `judge_model_version`, frozen SCD-2 `reference_table_snapshot_json`, `policy_verdicts_json`, `args_sha256`/`approved_args_sha256`, and optional `sor_signature_ref` under WORM/retention lock.
- **Deterministic SQL policy, safety & schema gates (run on 100% of rows before any LLM judge):**
  - Self-only and cross-tenant isolation (`traces.user_id` / `tenant_id` matched against the argument the workload names in `tool_calls.arguments`, failing closed when absent; tools enforced via signed OBO tokens record `authz_mode = 'token_enforced'` and validate attested identity claims).
  - Pre-ticket rejection (`outcome.rejected` without `outcome.ticket_created` or a call to a `creates_ticket` tool, plus a reply sent through a `sends_external_reply` tool).
  - Objective runbook escalation (verifying `tool_calls` lookup results against `outcome.escalated`).
  - Sandbox isolation & verification (`tools.executes_code = true` checked against the call status and `bq_eval.sandbox.status` when reported, blocking failed or unexecuted code from verification).
  - Per-turn PII and secrets leak scanning across turn deltas (`turn_prompt`, `tool_calls` I/O, `step_outputs`, `turn_output`) with `1x` per-turn attribution; text that could not be read whole is reported, never passed.
  - Output schema conformance, required fields, mandatory disclaimers, and citation URL grounding against `turn_context`.
  - **Fail-closed HITL write gates (`UC5`):** Any call to an **unregistered tool** fails closed on regulated workloads (never treated as read-only). For `tools.writes_state = true`, Gold matches authoritative `outcome.approved` rows on `action_id`, `approved_args_sha256 = args_sha256` (RFC 8785 JCS), `event_time < tool_calls.start_time`, **approval TTL** (`tool_calls.start_time <= approved_expires_at` when set), **Maker-Checker Separation of Duties** (`events.actor_id != traces.user_id`), and **single-use binding** (one approval `event_id` authorizes at most one execution). Duplicate-write checks cross-verify against authoritative `action.committed` rows.
  - **Model allow-list enforcement (with [capacity extension](capacity.md)):** Flags any `model_calls` row violating scoped `allowed_models` (`environment`, `business_unit`, `zdr_allowed`, `eol_date`, `effective_from`/`effective_to`).
  - Reply kind (answer, clarification, rejection, guardrail_refusal, parked), derived from `events` and reply-sending tool calls where a rule needs it.
- **Sampled 4-point ordinal LLM judging:**
  - Skips turns that are not completed or fail deterministic Gold SQL gates; samples `5–10%` of passing baseline turns plus `100%` of slow, throttled, escalated, or low-confidence strata.
  - Escapes `&`, `<`, and `>` in untrusted trace text inside `<untrusted_text>`, passes in-place `media_uris` for multimodal/PDF tasks, and emits reasoning before a 4-point ordinal score (`>= 3` passes).
- **Fail-closed calibration & blind SME grounding:**
  - Computes stratum-weighted Rogan-Gladen prevalence ($\hat{\theta}_{\text{RG}}$) with Lang & Reiczigel (2014) 95% confidence intervals and Quadratic Gwet's `AC2` (`>= 0.80` default gate) strictly against Gold-stored **blind human SME labels** (never operational `events` reviews), including audit spot-checks on auto-accepted high-confidence strata.
  - Reports `NULL` (`UNJUDGED` or `UNCALIBRATED`) whenever any populated stratum is unjudged, `judged_samples < 10`, positive or negative reference counts `< 5`, or Youden's $J$ or Wilson-adjusted $J' \le 10^{-6}$.
- **Paired non-regression & cascade evaluation:**
  - Pairs candidate vs. baseline runs on `bq_eval.case_id` across both single `traces` and multi-trace `outcomes` (tracking prompt, agent, model, and `skill_version`).
  - Enforces `0.0%` negative flips on regulated/safety gates, while allowing cascaded extraction workloads to evaluate whether candidate models and confidence thresholds lower combined `(primary + escalation + eval)` cost above a declared calibrated accuracy floor (`0.0%` safety flips).

## 2. Monitoring add-on

- **SLA & missing-identity monitoring:** An SLA clock starts at trace start; starting the trace when the work arrives is the application's duty. Computes time to acknowledgement or rejection and net resolution time (elapsed minus customer wait, computed from `events`) across open and closed `outcomes`. Checks turns that have a root span for a missing requester on a schedule of 5 minutes or longer. A guard that must act in under a minute runs in the request path (a collector rule or the application itself) and is not part of BQ Eval.
- **Latency, first-token time & throttling:** Tracks end-to-end turn latency, per-call latency, and `429` throttling from `traces` and `model_calls`, and builds turn-level first-token time from the per-call values where applications stamp them. Latency targets are monitoring settings per application.
- **Settled traces:** A trace counts as settled when its last span arrived longer ago than the settle setting.
- **Workload loop & sandbox retry budgets:** Evaluates per-application tool-call caps, repeated-call counts, and sequential retry/sandbox oscillations over `traces` and ordered call views.
- **User satisfaction & session length:** Tracks `feedback.thumbs_up` / `feedback.thumbs_down` rates (`events`) and conversation length in user turns (`COUNT(trace_id)` per `gen_ai.conversation.id`).

## 3. Cost add-on & `capacity` export contract

- **Per-call pricing across six spend buckets:** Prices each `model_calls` row from the token counts the application reported, against the Gold SKU catalog (`>200K` 1P Gemini cliffs, `FLEX` and 3P tier weights), adds sandbox compute from `tool_calls` (`bq_eval.sandbox.cpu_sec` when reported, call duration otherwise), and classifies spend into `production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`, `optimize`, and `unclassified`.
- **Outcome, cascade, specialist & user attribution:** Computes Cost per Verified Outcome (`CPVO`) keeping failed, rejected, and escalated attempts plus `100%` of judge runs in the numerator; breaks spend down by specialist agent, PDF page band, failure-vs-confidence escalation, and per user (including prefix-cache dilution and session context-growth drivers). Org-unit roll-ups join a directory the deployment provides.
- **`capacity` export:** Publishes a versioned hourly burndown, token-distribution, and latency/throttling export contract consumed by the sibling `capacity` repository for SLO-keeping model, routing, and capacity recommendations.
- **With the [capacity extension](capacity.md):** Reconciles each `model_calls` row **`1:N`** against upstream gateway/server `requests` (aggregating failover/retry attempts on `request_id`/`span_id` before joining so cost and `StratPPI` never double-count) and attributes `capacity_pools` commitments across `capacity_allocations` (active production use, off-peak eval/batch backfill, idle pool, and involuntary vs. burst PayGo overflow) under `FOCUS 1.2`.
