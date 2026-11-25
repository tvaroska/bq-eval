---
type: reference
related:
  - "silver.md"
  - "shapes.md"
  - "capacity.md"
  - "clouds.md"
  - "open-questions.md"
  - "../design/silver.md"
tags: [bq-eval, silver, telemetry, attributes]
---

# Telemetry Attribute & Event Contract

> Standard-first attribute and event names read by Silver adapters. Use OpenTelemetry and cloud semantic conventions wherever a standard exists, preserve `outcome.*` from `EVAL-P11`, and place only attributes outside those standards in the `bq_eval.*` namespace (`ADR-015`).

## Rules

1. **Standards first:** Never invent a `bq_eval.*` key when an OpenTelemetry (`v1.45+`), OpenTelemetry GenAI (`v0.1+`), MCP, A2A, or cloud semantic convention already defines the concept.
2. **Missing stays empty:** If an application omits an attribute, Silver leaves the column `NULL` and counts it where tracked. Silver never fabricates defaults.
3. **One-way trust on self-claims:** An application's claim that it failed (`bq_eval.app_failed = true`) is published as a fact and counted against the turn by evaluation. A claim that it passed or was verified is never read. A row in `events` is authoritative only when it sits in the input table that only systems of record may write.
4. **Restricted identity:** Raw `user.id` (and optional `bq_eval.tenant_id`) appears only in the restricted views (`traces`, `tool_calls`, `events`), so in-cloud SQL can enforce self-only and tenant-isolation rules. The open twins omit raw ids. Salted hashes belong to the [clouds extension](clouds.md).

## 1. Standard OpenTelemetry & Cloud Attributes

Silver adapters read these standard names verbatim:

| Attribute / Event | Standard | Projected To | Purpose |
| :--- | :--- | :--- | :--- |
| `apphub.application.id`, `apphub.service.id`, `apphub.workload.id` | GCP App Hub (resource) | `spans`, `traces` | Application ownership and `app_registry` join |
| `service.name`, `deployment.environment.name`, `cloud.provider`, `cloud.region`, `cloud.account.id` | OTel Resource (`v1.45`) | `spans`, `model_calls`, `traces` | Service identity, environment, and cloud/region residency (`cloud.account.id` also keys pools in the [capacity extension](capacity.md)) |
| `http.response.status_code`, `error.type` | OTel Core (`v1.45`) | `spans`, `model_calls`, `tool_calls`, `traces` | Throttling (`429`) and error classification |
| `user.id` | OTel General (`v1.45`) | `spans`, `tool_calls`, `events`, `traces` (restricted views only) | Requester identity: the root span's value is the requester of the trace, and spans that disagree are counted |
| `gen_ai.conversation.id` | OTel GenAI (`v0.1`) | `traces`, `events` | Multi-turn session correlation |
| `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons` | OTel GenAI (`v0.1`) | `model_calls`, `tool_calls`, `traces`, `events` (`response_id` join) | Call classification (`chat` and `generate_content` both map to `model_calls`; model resolves `COALESCE(gen_ai.response.model, gen_ai.request.model)` since ADK 2.14 emits `request.model` only), provider normalization, and async feedback correlation |
| `gen_ai.response.time_to_first_chunk` | OTel GenAI (`v0.1`) | `model_calls` | Per-call time to first chunk (injected by SDK/gateway adapter where the framework omits it); Gold builds turn-level TTFT from per-call values |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens`, `gen_ai.usage.reasoning.output_tokens` | OTel GenAI (`v0.1`) | `model_calls`, `traces` | Token accounting, cache efficiency, and Gold pricing (adapters preserve optional 5m vs. 1h ephemeral cache-write splits where emitted, e.g. `cache_creation.ephemeral_5m_input_tokens` / `_1h_`, and fold legacy `thoughts_tokens`) |
| `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.retrieval.documents` (plus `*_ref` storage addresses and `file_data` storage URIs) | OTel GenAI (`v0.1`) | `trace_payloads`, `tool_calls` (Shape 3 fallback) | Splits `prior_history` from `turn_prompt`, feeds `turn_context`, `turn_output`, `step_outputs` and `media_uris`, and reconstructs Shape 3 `tool_calls` args/results via `gen_ai.tool.call.id` |
| `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` | OTel GenAI (`v0.1`) | `tool_calls`, `trace_payloads` (`turn_context`, every result tagged with its tool flags) | Tool invocation facts, the `args_sha256` Silver computes, policy inspection, and grounding context |
| `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.agent.version`, `gen_ai.prompt.name`, `gen_ai.prompt.version`, `gen_ai.skill.name`, `gen_ai.data_source.id` | OTel GenAI (`v0.1`) | `model_calls`, `tool_calls`, `traces` | Multi-agent attribution (a call that names no agent takes the nearest enclosing agent span), prompt/agent/skill lineage, retrieval source |
| `gen_ai.evaluation.result` (`gen_ai.evaluation.{name,score.value,score.label,explanation}`) | OTel GenAI (`v0.1` event) | `events` | OTel event carrier for user feedback (e.g., `name = 'user_feedback'`, thumbs up/down) and operational reviews (matched via `trace_id`, `outcome_id`, or `gen_ai.response.id`); never authoritative; blind SME calibration labels stay in Gold |
| `mcp.method.name`, `mcp.session.id`, `a2a.task.id`, `a2a.task.state` | OTel MCP / A2A (`v0.1`) | `tool_calls`, `traces`, `events` | MCP client/server span pairing (a server span folds into its parent client span) and A2A task state |
| W3C trace state (OpenTelemetry `th` value) | W3C Trace Context / OTel probability sampling | `traces` | Sampling probability of the trace, when the trace store keeps the trace state |

## 2. Cross-Repo Outcome Attributes (`EVAL-P11`)

Propagated across agent hops via W3C `baggage` and stripped on external counterparty egress:

| Attribute / Span | Type / Values | Projected To | Purpose |
| :--- | :--- | :--- | :--- |
| `outcome.id` | `STRING` (opaque ID) | `spans`, `traces`, `outcomes`, `events` | End-to-end business outcome correlation across async traces |
| `outcome.owner` | `STRING` (App Hub app ID) | `spans`, `outcomes` | Owning application of the outcome |
| `outcome.status` | `succeeded \| failed \| abandoned \| escalated \| rejected \| denied` | `outcomes`, `events` | Terminal status reported on close (includes `rejected` for pre-ticket rejection and `denied` for HITL denial) |
| `outcome.subject` | `STRING` | `outcomes` | Optional business case / document / ticket key |
| `outcome.parent_id` | `STRING` | `outcomes` | Optional parent deal/case linking multi-stage outcomes |
| Span `outcome.closed` | Zero-duration span | `outcomes`, `events` | Marks terminal outcome closure in the trace store |

## 3. `bq_eval.*` Attributes (Outside OTel & `EVAL-P11`)

Only concepts with no OpenTelemetry or `EVAL-P11` standard live under `bq_eval.*`:

| Attribute | Type / Allowed Values | Span / View | Workload Scope | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `bq_eval.traffic_type` | `production_inference \| conflict_escalation \| agent_gym_eval \| autorater_judge \| optimize` | Span → `model_calls`, `traces` | All | Separates production (interactive and agent-batch), escalation, evaluation, and optimization traffic (adapters accept legacy `finops.traffic_type` alias; missing stays `NULL` and maps to `unclassified` in Gold) |
| `bq_eval.serving_tier` | `PROVISIONED_THROUGHPUT \| PRIORITY \| STANDARD \| FLEX` | Span → `model_calls` | All | Serving fulfillment tier including discounted Flex capacity for agent-batch runs (the `requests` view of the capacity extension is the authority when installed) |
| `bq_eval.action_id` | `STRING` | Tool span → `tool_calls` | HITL writes (UC5) | Binds a state-mutating tool invocation to an authoritative `outcome.approved` row and to `action.committed` rows |
| `bq_eval.args_sha256` | `STRING` (hex SHA-256) | Tool span → `tool_calls`, `tool_calls_open` | ZDR & HITL writes (UC1, UC5) | Execution-time RFC 8785 (JCS) SHA-256 of the executed tool arguments emitted by trusted tool/proxy middleware so `*_open` views never scan restricted payload buckets |
| `bq_eval.trigger_principal` | `human \| event \| automated` | Root/gateway span → `traces` | All | Distinguishes interactive human requests from event-triggered and automated evaluation/optimization runs |
| `bq_eval.agent_principal` | `STRING` | Root/gateway span → `traces` | Multi-agent / OBO | Workload or agent service identity acting on behalf of `user.id` |
| `bq_eval.identity_attested` | `BOOL` | Gateway/proxy span → `traces`, `tool_calls` | Regulated / OBO | `true` when `user.id` / OBO chain was verified by a trusted gateway or identity proxy (`authz_mode = 'token_enforced'` when tool auth validates signed tokens directly) |
| `bq_eval.app_failed` | `BOOL` | Span → `traces` | All | Self-reported runtime failure, published as a fact (`false` or `NULL` is not read); safety/policy blocks use `guardrail.intervention`, not `app_failed` |
| `bq_eval.app_failure_reason` | `STRING` | Span → `traces` | Optional | Diagnostic code or reason when `bq_eval.app_failed = true` |
| `bq_eval.tenant_id` | `STRING` | Span → `traces`, `tool_calls`, `events` (restricted views) | Multi-tenant (UC2) | Customer / tenant boundary identifier for cross-customer isolation checks |
| `bq_eval.case_id` | `STRING` | Root span → `traces`, `outcomes` | Regression / batch (UC4, UC5) | Golden or benchmark case identifier for candidate-vs-baseline pairing across distinct `trace_id`s and multi-trace outcomes |
| `bq_eval.eval_run_id` | `STRING` | Root span → `traces`, `outcomes` | Regression / eval (UC4, UC5) | Groups traces and outcomes belonging to one offline or pre-release evaluation run |
| `bq_eval.skill_version` | `STRING` (semver) | Span → `model_calls`, `tool_calls`, `traces` | Versioned skills (UC5) | Complements OTel `gen_ai.skill.name` with semantic version across multi-skill steps |
| `bq_eval.idempotency_key` | `STRING` | Tool span → `tool_calls` | State-mutating writes (UC5) | Detects duplicate mutations across retries or loops |
| `bq_eval.model_confidence` | `FLOAT64` (`0.0..1.0`) | Model span → `model_calls`, `traces`, `outcomes` | Cascaded extraction (UC4) | Per-call model confidence on `model_calls`; the last call of the trace that reports one gives `final_model_confidence` |
| `bq_eval.cascade_tier` | `primary \| escalation` | Model span → `model_calls`, `traces`, `outcomes` (`escalated`) | Cascaded models (UC4) | Distinguishes primary model attempt from stronger fallback model and marks escalated traces/outcomes |
| `bq_eval.escalation_reason` | `failure \| low_confidence \| missing_runbook \| policy_threshold` | Span / `events` → `traces`, `outcomes` | Escalating workflows (UC2, UC4, UC5) | Explains why a step escalated to a stronger model or human |
| `bq_eval.document.page_count` | `INT64` | Root / model span → `model_calls`, `traces`, `outcomes` | Document workloads (UC4) | Enables cost and escalation breakdown by PDF page length across single- and multi-trace document outcomes |
| `bq_eval.sandbox.status` | `ok \| timeout \| error \| policy_blocked` | Tool span → `tool_calls` | Code execution (UC2), optional | Sandbox runtime boundary status on tools marked `executes_code = true` in `tools` |
| `bq_eval.sandbox.cpu_sec` | `FLOAT64` | Tool span → `tool_calls` | Code execution (UC2), optional | Sandbox compute for cost attribution; the call duration is used when it is absent |
| `bq_eval.sampling_probability` | `FLOAT64` (`0.0 < p <= 1.0`) | Root span → `traces` | Sampled applications | Sampling probability of the trace, read only when the trace store does not keep the standard trace state |

`bq_eval.pool_id` belongs to the [capacity extension](capacity.md). Organizational or cost-center rollups per user (UC3) are a Gold join against a directory the deployment provides.

## 4. Canonical `events` View Taxonomy

Every row in Silver `events` carries `event_id`, `event_time`, `event_type`, `source_system`, `is_authoritative`, `trace_id`, `span_id`, `response_id` (`gen_ai.response.id`), `outcome_id`, `conversation_id`, `action_id`, `approved_args` and `approved_args_sha256` (approval rows), `user_id`, `tenant_id`, `actor_id`, and `payload_json`. `events_open` omits `approved_args`, `payload_json`, `user_id`, `tenant_id` and `actor_id`.

- **Outcome lifecycle (`outcome.*`):** `outcome.opened`, `outcome.acknowledged`, `outcome.rejected`, `outcome.ticket_created`, `outcome.waiting_customer`, `outcome.customer_replied`, `outcome.escalated`, `outcome.parked_for_approval`, `outcome.approved`, `outcome.denied`, `outcome.resumed`, `outcome.closed`.
- **Actions, guardrails and handling:** `action.committed` (the system of record committed a write; carries `action_id`, the idempotency key and the committed arguments), `guardrail.intervention` (policy/DLP/safety filter blocked or redacted a prompt, tool call, or reply — distinct from `bq_eval.app_failed` runtime crashes), `handling.started` and `handling.ended` (a person took a case over and handed it back).
- **Operational workflow human signals (blind SME calibration labels belong strictly to Gold):**
  - User feedback (`feedback.thumbs_up`, `feedback.thumbs_down`) — mapped from OTel `gen_ai.evaluation.result` log records (in Cloud Logging `_AllLogs`, OTLP `/v1/logs` places `eventName` in `log_id`, attributes in `labels` JSON, and bare-hex trace IDs in `trace`) or from input-table rows.
  - Operational human review (`review.routed`, `review.accepted`, `review.corrected`) — mapped from input-table rows or OTel `gen_ai.evaluation.result`; never mixed with blind stratified SME calibration labels in Gold.
- **Where rows come from:** the input table (section 5), closing spans (`outcome.closed`), and OTel `gen_ai.evaluation.result` events.
- **Authority rule:** `is_authoritative = true` only for rows from the copy of the input table that only systems of record may write. Rows written by applications, rows derived from spans, and feedback events read from telemetry are never authoritative.

## 5. The Input Table for Records That Are Not Spans

One append-only table, `workflow_events`, partitioned by `DATE(event_time)` and clustered by `(outcome_id, action_id)`, with fixed columns: `event_id`, `event_time`, `event_type`, `source_system`, `trace_id`, `span_id`, `response_id`, `outcome_id`, `conversation_id`, `action_id`, `approved_args`, `approved_expires_at`, `user_id`, `tenant_id`, `actor_id`, `payload_json`.

- It is deployed twice. One copy may be written only by systems of record (approval service, ticketing system, mail gateway, the system that commits writes). The other is for applications. Write access decides which copy a row is in, and the copy decides `is_authoritative`.
- A system that cannot write these columns is mapped by a view the deployment owns. The core ships no per-system adapter.
- An approval row carries the approved arguments in `approved_args` (and optional `approved_expires_at` TTL). Silver computes `approved_args_sha256` using RFC 8785 (JCS) canonicalization matching `tool_calls.args_sha256`. A writer never supplies a hash on `workflow_events`.
