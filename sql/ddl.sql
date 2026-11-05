-- =============================================================================
-- BigQuery Agent Evaluation, Observability & FinOps Showcase (bq-eval)
-- Complete 7-Table Physical DDL + Remote Judge Model Provisioning
-- Standard: EVAL-P04, EVAL-P05, EVAL-P06, EVAL-P07, EVAL-P08 (v2026.11.2)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS `agent_eval`
OPTIONS (
  description = 'Enterprise Agent Evaluation, OTel Trace Analytics, Rogan-Gladen Calibration, and 4-Bucket FinOps Cost Management dataset.'
);

-- -----------------------------------------------------------------------------
-- 1. Application Registry & FinOps Budget Targets (agent_eval.app_registry)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.app_registry` (
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Canonical application identifier (gcp.apphub.application.id or fallback agent ID).'),
  display_name STRING NOT NULL OPTIONS (description = 'Human-readable application name.'),
  business_unit STRING NOT NULL OPTIONS (description = 'Owning business unit or line of business.'),
  cost_center STRING NOT NULL OPTIONS (description = 'FinOps chargeback cost center code.'),
  environment STRING NOT NULL OPTIONS (description = 'Deployment environment (prod, staging, eval).'),
  sla_latency_ms FLOAT64 NOT NULL OPTIONS (description = 'Target end-to-end trace SLA latency in milliseconds.'),
  monthly_budget_usd FLOAT64 NOT NULL OPTIONS (description = 'Monthly LLM & evaluation budget cap in USD.'),
  owner_email STRING NOT NULL OPTIONS (description = 'Technical owner contact email.'),
  updated_at TIMESTAMP NOT NULL OPTIONS (description = 'Last registry metadata update timestamp.')
)
CLUSTER BY apphub_application_id, cost_center
OPTIONS (
  description = 'Application registry mapping App Hub / agent identifiers to SLA thresholds and FinOps cost centers.'
);

-- -----------------------------------------------------------------------------
-- 2. OpenTelemetry GenAI Spans Sink (agent_eval.otel_spans)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.otel_spans` (
  trace_id STRING NOT NULL OPTIONS (description = 'W3C 32-hex trace identifier.'),
  span_id STRING NOT NULL OPTIONS (description = 'W3C 16-hex span identifier.'),
  parent_span_id STRING OPTIONS (description = 'Parent span ID; NULL for root invoke_agent span.'),
  start_time TIMESTAMP NOT NULL OPTIONS (description = 'UTC span start timestamp (daily partition key).'),
  end_time TIMESTAMP NOT NULL OPTIONS (description = 'UTC span completion timestamp.'),
  latency_ms FLOAT64 NOT NULL OPTIONS (description = 'Span execution duration in milliseconds.'),
  status_code STRING NOT NULL OPTIONS (description = 'OTel status code: OK, ERROR, UNSET.'),
  span_kind STRING NOT NULL OPTIONS (description = 'GenAI operation: invoke_agent, execute_tool, chat, embeddings, retrieval.'),
  service_name STRING NOT NULL OPTIONS (description = 'OTel service.name resource attribute.'),
  gcp_apphub_application_id STRING OPTIONS (description = 'Explicit gcp.apphub.application.id attribute when present.'),
  gcp_apphub_service_id STRING OPTIONS (description = 'Explicit gcp.apphub.service.id attribute.'),
  gcp_apphub_workload_id STRING OPTIONS (description = 'Explicit gcp.apphub.workload.id attribute.'),
  agent_name STRING NOT NULL OPTIONS (description = 'Logical agent or sub-agent name (gen_ai.agent.name).'),
  model_name STRING NOT NULL OPTIONS (description = 'Public model name (gemini-3.8-flash, gemini-4-argon, gemini-3.5-flash-lite).'),
  input_tokens INT64 NOT NULL OPTIONS (description = 'Total input tokens including cached_input_tokens.'),
  cached_input_tokens INT64 NOT NULL OPTIONS (description = 'Cached prefix input tokens (0.1x <=200K, 0.2x >200K).'),
  output_tokens INT64 NOT NULL OPTIONS (description = 'Generated output tokens (4.0x <=200K, 7.5x >200K).'),
  thinking_tokens INT64 NOT NULL OPTIONS (description = 'Reasoning tokens (4.0x <=200K, 7.5x >200K).'),
  tool_name STRING OPTIONS (description = 'Tool identifier when span_kind = execute_tool.'),
  tool_call_status STRING OPTIONS (description = 'Tool outcome: SUCCESS, VALIDATION_ERROR, TIMEOUT, RATE_LIMITED.'),
  rule0_pass BOOL NOT NULL OPTIONS (description = 'Deterministic Rule 0 verification pass flag.'),
  finops_traffic_type STRING NOT NULL OPTIONS (description = 'FinOps bucket: production_inference, conflict_escalation, agent_gym_eval, autorater_judge.'),
  attributes JSON OPTIONS (description = 'Full OpenTelemetry span attributes JSON payload.')
)
PARTITION BY DATE(start_time)
CLUSTER BY gcp_apphub_application_id, trace_id, span_kind
OPTIONS (
  description = 'Partitioned OpenTelemetry gen_ai.* span sink for Tier 1 SQL evaluation and FinOps burndown.'
);

-- -----------------------------------------------------------------------------
-- 3. Sanitized Trace Text Payloads (agent_eval.trace_payloads)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.trace_payloads` (
  trace_id STRING NOT NULL OPTIONS (description = 'W3C trace identifier joining to agent_eval.otel_spans.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Resolved application identifier.'),
  ingested_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC ingestion timestamp (daily partition key).'),
  user_prompt STRING NOT NULL OPTIONS (description = 'Sanitized user prompt text.'),
  retrieved_context STRING NOT NULL OPTIONS (description = 'Concatenated retrieved grounding context chunks.'),
  final_response STRING NOT NULL OPTIONS (description = 'Final agent response presented to the user.')
)
PARTITION BY DATE(ingested_at)
CLUSTER BY apphub_application_id, trace_id
OPTIONS (
  description = 'Sanitized prompt, retrieved context, and response payloads for Tier 2 AI.GENERATE_TABLE evaluation.'
);

-- -----------------------------------------------------------------------------
-- 4. Golden Evaluation Dataset Catalog (agent_eval.golden_cases)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.golden_cases` (
  case_id STRING NOT NULL OPTIONS (description = 'Immutable golden test case identifier.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Target application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Target rubric identifier (e.g., groundedness_4pt_v1).'),
  risk_tier STRING NOT NULL OPTIONS (description = 'Scenario criticality tier: HIGH, MEDIUM, LOW.'),
  user_prompt STRING NOT NULL OPTIONS (description = 'Canonical test input prompt.'),
  retrieved_context STRING NOT NULL OPTIONS (description = 'Canonical grounding context.'),
  reference_answer STRING NOT NULL OPTIONS (description = 'SME-verified reference response.'),
  expected_rule0_pass BOOL NOT NULL OPTIONS (description = 'Expected deterministic Rule 0 outcome.'),
  expected_semantic_pass BOOL NOT NULL OPTIONS (description = 'Expected semantic rubric binary pass outcome.'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = 'Golden case creation timestamp.')
)
CLUSTER BY apphub_application_id, rubric_id, risk_tier
OPTIONS (
  description = 'Version-controlled golden evaluation dataset for pre-deployment Agent Gym and judge regression gates.'
);

-- -----------------------------------------------------------------------------
-- 5. Tier 2 LLM-as-a-Judge Evaluation Results (agent_eval.eval_results)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.eval_results` (
  eval_id STRING NOT NULL OPTIONS (description = 'Unique evaluation execution record ID.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Evaluated W3C trace identifier.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Resolved application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Rubric identifier (e.g., groundedness_4pt_v1).'),
  judge_model STRING NOT NULL OPTIONS (description = 'Autorater model identifier (gemini-4-argon).'),
  sampling_stratum STRING NOT NULL OPTIONS (description = 'Sampling reason: ERROR_OR_SLA_TAIL or CANARY_5PCT.'),
  rule0_pass BOOL NOT NULL OPTIONS (description = 'Trace-level Rule 0 status at evaluation time.'),
  reasoning_trace STRING NOT NULL OPTIONS (description = 'Step-by-step CoT rationale emitted before ordinal_score.'),
  ordinal_score INT64 NOT NULL OPTIONS (description = '4-point ordinal rubric score (1, 2, 3, or 4).'),
  binary_pass BOOL NOT NULL OPTIONS (description = 'Deterministic threshold outcome (ordinal_score >= passing_score).'),
  judge_input_tokens INT64 NOT NULL OPTIONS (description = 'Prompt tokens consumed by the judge call.'),
  judge_output_tokens INT64 NOT NULL OPTIONS (description = 'Completion tokens consumed by the judge call.'),
  judge_thinking_tokens INT64 NOT NULL OPTIONS (description = 'Reasoning tokens consumed by the judge call.'),
  evaluated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC judge execution timestamp.')
)
PARTITION BY DATE(evaluated_at)
CLUSTER BY apphub_application_id, rubric_id, trace_id
OPTIONS (
  description = 'Tier 2 AI.GENERATE_TABLE evaluation results with judge token telemetry.'
);

-- -----------------------------------------------------------------------------
-- 6. Stratified SME Calibration Audit Labels (agent_eval.sme_labels)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.sme_labels` (
  label_id STRING NOT NULL OPTIONS (description = 'Unique SME calibration label identifier.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Audited trace identifier.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Calibrated rubric identifier.'),
  rater_id STRING NOT NULL OPTIONS (description = 'Anonymized SME rater identifier.'),
  rule0_pass BOOL NOT NULL OPTIONS (description = 'Whether the trace passed Tier 1 deterministic Rule 0 checks.'),
  human_ordinal_score INT64 NOT NULL OPTIONS (description = 'SME 4-point ordinal rating (1..4).'),
  human_pass BOOL NOT NULL OPTIONS (description = 'SME binary pass ground truth.'),
  judge_ordinal_score INT64 OPTIONS (description = 'Autorater 4-point ordinal rating on the same trace.'),
  judge_pass BOOL OPTIONS (description = 'Autorater binary pass prediction on the same trace.'),
  labeled_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC SME annotation timestamp.')
)
PARTITION BY DATE(labeled_at)
CLUSTER BY apphub_application_id, rubric_id
OPTIONS (
  description = 'Human SME audit labels for computing empirical TPR, TNR, Youden J, and Gwet AC2.'
);

-- -----------------------------------------------------------------------------
-- 7. Effective-Dated Model Pricing & Burndown Catalog (agent_eval.model_pricing_catalog)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.model_pricing_catalog` (
  model_name STRING NOT NULL OPTIONS (description = 'Public model name (gemini-3.8-flash, gemini-4-argon, gemini-3.5-flash-lite).'),
  effective_start TIMESTAMP NOT NULL OPTIONS (description = 'Inclusive pricing effective start timestamp.'),
  effective_end TIMESTAMP NOT NULL OPTIONS (description = 'Exclusive pricing effective end timestamp.'),
  input_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Uncached input token price per 1M tokens (<=200K context).'),
  cached_input_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Cached prefix token price per 1M tokens (<=200K context).'),
  output_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Output and thinking token price per 1M tokens (<=200K context).'),
  long_context_threshold_tokens INT64 NOT NULL OPTIONS (description = 'Context cliff threshold in input tokens (standard: 200000).'),
  long_context_input_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Uncached input price per 1M tokens above threshold (>200K).'),
  long_context_cached_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Cached input price per 1M tokens above threshold (>200K).'),
  long_context_output_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Output/thinking price per 1M tokens above threshold (>200K).'),
  burndown_uncached_weight FLOAT64 NOT NULL OPTIONS (description = 'Standard uncached input burndown multiplier (1.0).'),
  burndown_cached_weight FLOAT64 NOT NULL OPTIONS (description = 'Standard cached input burndown multiplier (0.1).'),
  burndown_output_weight FLOAT64 NOT NULL OPTIONS (description = 'Standard output/thinking burndown multiplier (4.0).'),
  burndown_long_uncached_weight FLOAT64 NOT NULL OPTIONS (description = 'Long-context (>200K) uncached input multiplier (2.0).'),
  burndown_long_cached_weight FLOAT64 NOT NULL OPTIONS (description = 'Long-context (>200K) cached input multiplier (0.2).'),
  burndown_long_output_weight FLOAT64 NOT NULL OPTIONS (description = 'Long-context (>200K) output/thinking multiplier (7.5).')
)
CLUSTER BY model_name
OPTIONS (
  description = 'Effective-dated SKU pricing and GSU serving-physics burndown weights.'
);

-- -----------------------------------------------------------------------------
-- 8. Remote Cloud AI Autorater Model (agent_eval.gemini_4_argon_judge)
-- -----------------------------------------------------------------------------
CREATE MODEL IF NOT EXISTS `agent_eval.gemini_4_argon_judge`
REMOTE WITH CONNECTION `us.vertex_ai_connection`
OPTIONS (
  endpoint = 'gemini-4-argon'
);
