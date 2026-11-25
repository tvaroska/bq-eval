---
type: reference
related:
  - "layers.md"
  - "silver.md"
  - "gold.md"
  - "clouds.md"
  - "capacity.md"
  - "use-cases.md"
tags: [bq-eval, features, use-cases]
---

# Architecture Feature Matrix

> Complete capability list across Bronze/Silver Core, the three Gold add-ons and the two extensions, how each capability is addressed in the architecture, and which [target use cases](use-cases.md) require it (`UC1` Corporate Self-Service, `UC2` Customer Support, `UC3` Knowledge Worker, `UC4` Document Extraction, `UC5` Regulated Action Workflow).

## 1. Bronze & Silver Core ([silver.md](silver.md))

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F01 | Single-pass multi-shape span normalization & Collector Port | Up to 3 in-BQ SQL shapes routed in one single `SELECT` pass over `_AllSpans` (folding ADK Format 1 `call_llm` + child `generate_content` to prevent `2x` token counts) plus an upstream OTel Collector / Bronze Adapter Port for non-conforming frameworks; missing fields stay `NULL` and are counted. | UC1, UC2, UC3, UC4, UC5 |
| F02 | Turn-scoped delta payloads, BigLake Shape 3 & in-place media URIs | `trace_payloads` (1 row per turn/`trace_id`) splits `prior_history` from `turn_prompt`, `turn_context`, `turn_output`, `step_outputs`, and `media_uris`; Shape 3 runs over Hive-partitioned BigLake storage with risk-tiered async uploads (zero-payload ZDR supported). | UC1, UC2, UC3, UC4, UC5 |
| F03 | Call-level model/tool views & open-twin-safe RFC 8785 hashing | `model_calls` and `tool_calls` carry agent/skill lineage, `idempotency_key`, `action_id`, and RFC 8785 (JCS) `args_sha256` emitted at tool/proxy execution so `*_open` views never scan restricted payload buckets; server tool spans fold only into parent client spans. | UC1, UC2, UC3, UC4, UC5 |
| F04 | Lifecycle, guardrail, committed-write & feedback `events` | Partitioned `workflow_events` deployed twice (SoR copy authoritative, app copy not), closing spans, `guardrail.intervention` (distinct from runtime crashes), and OTel `gen_ai.evaluation.result` (`_AllLogs` `labels` JSON); approvals carry `approved_args` + `approved_expires_at` hashed with RFC 8785 JCS. | UC2, UC3, UC4, UC5 |
| F05 | Open & multi-day `outcomes` | `outcomes` emits 1 row per seen `outcome.id` (`is_closed`) with terminal statuses including `rejected` and `denied`, root-error/failure/rootless counts, closing trace, and `case_id`, `eval_run_id`, `page_count`, `final_model_confidence`, and `escalated`. | UC2, UC4, UC5 |
| F06 | Fail-closed completeness & 3-principal identity | `traces` publishes counts and facts with 3-principal identity (`trigger_principal`, `agent_principal`, attested `user_id`), `has_root_span`, `mutating_tool_call_count`, `unregistered_tool_count`, `guardrail_intervention_count`, and `sampling_probability` (`NULL` never defaults to `1.0`). | UC1, UC2, UC3, UC4, UC5 |
| F07 | SCD-2 factual reference tables | Owner-maintained `app_registry` (owner, unit, env, `risk_tier`, `payload_profile`, `sampling_mode`) and `tools` (`writes_state`, `executes_code`, `sends_external_reply`, `returns_pii`, `creates_ticket`) with optional `effective_from`/`effective_to` windows; missing lookups stay flagged facts. | UC1, UC2, UC3, UC4, UC5 |
| F08 | Split-dataset access (`silver_open` vs. `silver_restricted`) | Text and raw `user_id`/`tenant_id` live in `silver_restricted` (Authorized Dataset + BigLake Connection); `silver_open` holds open twins with separate IAM/CMEK and zero restricted-bucket scans. | UC1, UC2, UC3, UC5 |
| F09 | Single BigQuery SQL contract (`light` views + `enterprise` incremental) | One BigQuery SQL codebase deployed as horizon views (`light`) or incremental partitioned tables in isolated slots (`enterprise`), verified against schema files and recorded-trace fixtures offline and on live BigQuery. | UC1, UC2, UC3, UC4, UC5 |

## 2. Gold — Evaluation Add-On ([gold.md](gold.md#1-evaluation-add-on))

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F10 | Self-only & cross-tenant access enforcement | Deterministic in-cloud SQL comparing `traces.user_id` / `tenant_id` to the target IDs in the argument the workload names in `tool_calls.arguments`, failing closed when identity or arguments are `NULL`; reported only for applications that send every trace. | UC1, UC2 |
| F11 | Pre-ticket relevance gate & automated rejection check | Deterministic SQL verifying `outcome.rejected` without `outcome.ticket_created` or a call to a `creates_ticket` tool, plus a reply sent through a `sends_external_reply` tool, paired with sampled rubric evaluation for false rejections. | UC2 |
| F12 | Objective runbook-missing human escalation check | SQL comparing runbook `tool_calls` results (empty vs. matched) against `outcome.escalated` in `events`. | UC2 |
| F13 | Code sandbox safety & execution verification | SQL gate over `tool_calls` (`tools.executes_code = true`, call status, `bq_eval.sandbox.status` when reported) catching timeouts, policy blocks, and unexecuted/failed code before verification. | UC2 |
| F14 | Turn-delta PII & secrets leak detection | Deterministic regex/DLP SQL scan across this turn's `turn_prompt`, `tool_calls` I/O, `step_outputs`, and `turn_output` (`1x` per turn without repeating `prior_history`), attributed per `user_id`; text not read whole is reported, never passed. | UC3 |
| F15 | Schema conformance, citation grounding & disclaimers | Deterministic JSON-schema/required-field validation (`UC4`), disclaimer check (`UC1`), and URL/source matching of `turn_output` claims against `turn_context` (`UC1`, `UC2`). | UC1, UC2, UC4 |
| F16 | Fail-closed HITL write gate, SoD & WORM audit snapshots | Fails closed on unregistered tools; matches `tools.writes_state = true` to authoritative `outcome.approved` on `action_id`, RFC 8785 `approved_args_sha256 = args_sha256`, TTL (`approved_expires_at`), Maker-Checker SoD (`actor_id != user_id`), and single-use `event_id`; persists immutable WORM Gold Audit Snapshots. | UC5 |
| F17 | Idempotency & duplicate-mutation prevention | SQL check across `tool_calls` (`writes_state = true`, `idempotency_key`, `args_sha256`) and authoritative `action.committed` rows across retries and multi-trace resumes. | UC5 |
| F18 | Sampled 4-point ordinal LLM judging (in-cloud) | XML-escaped `<untrusted_text>` prompts over `trace_payloads` of completed turns that pass the deterministic gates (plus in-place `media_uris` for PDFs), oversampling failure/low-confidence/escalated tails in the data's home cloud (`>= 3` passes, reasoning first; supports follow-up quality rubrics). | UC1, UC2, UC4, UC5 |
| F19 | Fail-closed `StratPPI` calibration & blind SME audit | Stratum-weighted Rogan-Gladen + Lang & Reiczigel 95% CIs + Quadratic Gwet's `AC2` (`>= 0.80`) strictly over Gold blind SME labels and stored population counts (including spot-checks of auto-accepted high-confidence `UC4` docs and persisted `UC5` audit snapshots), returning `NULL` when unjudged/uncalibrated. | UC1, UC2, UC4, UC5 |
| F20 | Paired non-regression & cascade model/threshold eval | Pairs candidate vs. baseline runs on `bq_eval.case_id` across `traces` and `outcomes` (by prompt, agent, model, `skill_version`), enforcing `0.0%` negative flips on compliance gates (`UC5`) and testing total `(primary + escalation + eval)` cost vs. accuracy floor (`UC4`). | UC4, UC5 |

## 3. Gold — Monitoring Add-On ([gold.md](gold.md#2-monitoring-add-on))

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F21 | Missing-user check & `1m` / `24h` SLA tracking | Checks turns with a root span for a missing requester on a 5-minute schedule (an under-1-minute guard is the application's duty in the request path); measures acknowledgement and net closure times on open/closed `outcomes` from trace start, with customer wait computed from `events`. | UC1, UC2 |
| F22 | End-to-end latency, first-token time & throttling | Monitors turn duration, per-call latency, and `429` throttling across `traces` and `model_calls`; builds turn-level first-token time from per-call values where the application stamps `gen_ai.response.time_to_first_chunk`; latency targets are monitoring settings. | UC1, UC2, UC3 |
| F23 | Workload tool-loop & retry-oscillation budgets | Applies per-app call-count caps, `repeated_tool_call_count` thresholds, and sequential edit/test/sandbox retry rules over `traces` and ordered call views. | UC1, UC2, UC3 |
| F24 | User feedback (thumbs) & session turn-length tracking | Aggregates `feedback.thumbs_up` / `feedback.thumbs_down` (`events`) and counts user turns (`COUNT(trace_id)` ordered by `start_time` per `gen_ai.conversation.id`). | UC3 |

## 4. Gold — Cost Add-On & `capacity` Export ([gold.md](gold.md#3-cost-add-on--capacity-export-contract))

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F25 | Per-call cliff, `FLEX` & sandbox pricing (6 buckets) | Prices each `model_calls` row from application-reported tokens (`>200K` Gemini cliffs, `FLEX` & 3P weights) plus sandbox compute (`bq_eval.sandbox.cpu_sec` when reported, call duration otherwise) into 6 spend buckets (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`, `optimize`, `unclassified`), scaled by the sampling probability. | UC1, UC2, UC3, UC4, UC5 |
| F26 | True `CPVO`, cascade, page-band & specialist rollups | Divides total spend (including failed, rejected, and escalated attempts + `100%` judge spend via `trace_owner_app_id` / `outcome_id`) by Gold-verified outcomes, sliced by specialist (`UC2`), PDF `page_count` & escalation reason (`UC4`), and candidate model (`UC4`). | UC2, UC4, UC5 |
| F27 | Per-user spend, context/cache efficiency & org rollups | Reports per-user spend distribution, steep session context growth, and `1/N` prefix-cache efficiency over `traces`/`model_calls`, rolling up to org unit and cost center by joining a directory the deployment provides. | UC3 |
| F28 | `capacity` export & scoped `FOCUS 1.2` pool chargeback | Exports versioned hourly burndown/latency payloads to the `capacity` repo for SLO-keeping model/routing/capacity sizing; with the capacity extension, attributes `capacity_pools` commitments across `capacity_allocations` (active use, off-peak backfill, idle pool, PayGo overflow). | UC2, UC5 |

## 5. Cross-Cutting Gold Rules & Extensions

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F29 | Completed turns & verified outcomes | Evaluation's default: a turn is completed when its root span ended without error and the application reported no failure; an outcome is verified only when every trace completed, or only the closing trace where the workload declares retries as expected. | UC1, UC2, UC3, UC4, UC5 |
| F30 | Kept results & sampled scaling | Each add-on stores daily results with the reference values and population counts it used, and scales counts and cost by the sampling probability; "never" checks are reported only for applications that send every trace. | UC1, UC2, UC3, UC4, UC5 |
| F31 | More than one cloud ([clouds.md](clouds.md)) | Same logical Silver SQL per cloud; scheduled Hub Transfer Port stages text-free open twins (with KMS-HMAC id hashes) and correlates split-trace fragments; In-Cloud Judge Port runs judges natively where text lives. | none today |
| F32 | Capacity and governance ([capacity.md](capacity.md)) | `1:N` upstream gateway `requests`, SCD-2 `capacity_pools` (`order_id -> runtime_pool_id`), `capacity_allocations`, scoped `allowed_models` with Gold enforcement, and sub-minute metrics rollups. | UC2, UC5 when capacity is bought |

## 6. Covered in Part by Decision

| Requirement | What BQ Eval delivers | What is left to the application |
|---|---|---|
| `UC1` warning under 1 minute for a missing `user.id` | A check on a 5-minute schedule and the report | A guard in the request path |
| `UC2` 1-minute acknowledgement SLA | Time from trace start | Starting the trace when the mail arrives |
| `UC3` first-token time | Per-call and turn-level values where spans carry them | Stamping the standard attribute (ADK does not) |
| `UC2`, `UC5` capacity advice and chargeback | Export of observed load | Installing the capacity extension |
| "Never" checks on sampled applications | Nothing | Sending every trace |
