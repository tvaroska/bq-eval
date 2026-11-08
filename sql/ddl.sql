-- =============================================================================
-- BigQuery Agent Evaluation, Observability & FinOps Showcase (bq-eval)
-- M1 Native Telemetry Compatibility Views (_AllSpans / _AllLogs) + Physical DDL
-- Standard: EVAL-P04, EVAL-P05, EVAL-P06, EVAL-P07, EVAL-P08, EVAL-P11 (v2026.11.3)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS `agent_eval`
OPTIONS (
  description = 'Enterprise Agent Evaluation, Native OTel _AllSpans/_AllLogs Compatibility Views, Rogan-Gladen Calibration, and Cost per Verified Outcome dataset.'
);

CREATE SCHEMA IF NOT EXISTS `agent_eval_seed`
OPTIONS (
  description = 'Isolated synthetic seed and time-shift demo loadgen dataset (never mixed with live agent_eval telemetry).'
);

-- -----------------------------------------------------------------------------
-- 1. Application Registry & FinOps Budget Targets (agent_eval.app_registry)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.app_registry` (
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Canonical application identifier (apphub.application.id or fallback agent ID).'),
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
-- 1B. BigLake External Table for Uploaded OTel GenAI Completion Payloads
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS `agent_eval.completions` (
  payload JSON OPTIONS (description = 'Uploaded OTel GenAI prompt/context/completion JSON payload.')
)
WITH CONNECTION `us.vertex_ai_connection`
OPTIONS (
  format = 'JSON',
  uris = ['gs://btv-scratch-01-genai-payloads/*.jsonl']
);

-- -----------------------------------------------------------------------------
-- 2. Native Telemetry Compatibility View (agent_eval.otel_spans -> obs_traces._AllSpans)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.otel_spans` AS
SELECT
  trace_id,
  span_id,
  parent_span_id,
  name AS span_name,
  start_time,
  end_time,
  receive_time,
  SAFE_DIVIDE(CAST(duration_nano AS FLOAT64), 1e6) AS latency_ms,
  CASE
    WHEN CAST(status.code AS STRING) IN ('1', 'OK', 'STATUS_CODE_OK') THEN 'OK'
    WHEN CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR') THEN 'ERROR'
    ELSE 'UNSET'
  END AS status_code,
  COALESCE(CAST(kind AS STRING), 'SPAN_KIND_INTERNAL') AS otel_span_kind,
  COALESCE(
    JSON_VALUE(attributes, '$."gen_ai.operation.name"'),
    'internal'
  ) AS span_kind,
  COALESCE(
    JSON_VALUE(resource.attributes, '$."service.name"'),
    JSON_VALUE(attributes, '$."service.name"'),
    'unknown_service'
  ) AS service_name,
  COALESCE(
    JSON_VALUE(resource.attributes, '$."apphub.application.id"'),
    JSON_VALUE(resource.attributes, '$."gcp.apphub.application.id"'),
    JSON_VALUE(attributes, '$."apphub.application.id"'),
    JSON_VALUE(attributes, '$."gcp.apphub.application.id"')
  ) AS gcp_apphub_application_id,
  COALESCE(
    JSON_VALUE(resource.attributes, '$."apphub.service.id"'),
    JSON_VALUE(resource.attributes, '$."gcp.apphub.service.id"'),
    JSON_VALUE(attributes, '$."apphub.service.id"'),
    JSON_VALUE(attributes, '$."gcp.apphub.service.id"')
  ) AS gcp_apphub_service_id,
  COALESCE(
    JSON_VALUE(resource.attributes, '$."apphub.workload.id"'),
    JSON_VALUE(resource.attributes, '$."gcp.apphub.workload.id"'),
    JSON_VALUE(attributes, '$."apphub.workload.id"'),
    JSON_VALUE(attributes, '$."gcp.apphub.workload.id"')
  ) AS gcp_apphub_workload_id,
  COALESCE(
    JSON_VALUE(attributes, '$."gen_ai.agent.name"'),
    JSON_VALUE(resource.attributes, '$."service.name"'),
    'unknown_agent'
  ) AS agent_name,
  COALESCE(
    JSON_VALUE(attributes, '$."gen_ai.response.model"'),
    JSON_VALUE(attributes, '$."gen_ai.request.model"'),
    'none'
  ) AS model_name,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.input_tokens"') AS INT64), 0) AS input_tokens,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.cache_read.input_tokens"') AS INT64), 0) AS cached_input_tokens,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.output_tokens"') AS INT64), 0) AS output_tokens,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.reasoning.output_tokens"') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.thoughts_tokens"') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$."docubridge.thinking_tokens"') AS INT64),
    0
  ) AS thinking_tokens,
  JSON_VALUE(attributes, '$."gen_ai.tool.name"') AS tool_name,
  CASE
    WHEN JSON_VALUE(attributes, '$."gen_ai.operation.name"') = 'execute_tool' THEN
      COALESCE(
        JSON_VALUE(attributes, '$."tool.call.status"'),
        CASE
          WHEN JSON_VALUE(attributes, '$."error.type"') IN ('VALIDATION_ERROR', 'TIMEOUT', 'RATE_LIMITED')
            THEN JSON_VALUE(attributes, '$."error.type"')
          WHEN CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR')
            THEN 'VALIDATION_ERROR'
          ELSE 'SUCCESS'
        END
      )
    ELSE NULL
  END AS tool_call_status,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$."rule0.pass"') AS BOOL),
    SAFE_CAST(JSON_VALUE(attributes, '$."docubridge.rule0_pass"') AS BOOL),
    IF(CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR'), FALSE, TRUE)
  ) AS rule0_pass,
  COALESCE(
    JSON_VALUE(attributes, '$."finops.traffic_type"'),
    JSON_VALUE(attributes, '$."docubridge.finops.traffic_type"'),
    'production_inference'
  ) AS finops_traffic_type,
  COALESCE(
    JSON_VALUE(attributes, '$."outcome.id"'),
    JSON_VALUE(attributes, '$."docubridge.outcome_id"')
  ) AS outcome_id,
  COALESCE(
    JSON_VALUE(attributes, '$."outcome.owner"'),
    JSON_VALUE(resource.attributes, '$."apphub.application.id"'),
    JSON_VALUE(resource.attributes, '$."gcp.apphub.application.id"')
  ) AS outcome_owner,
  COALESCE(
    JSON_VALUE(attributes, '$."source_mode"'),
    JSON_VALUE(attributes, '$."docubridge.source_mode"')
  ) AS source_mode,
  COALESCE(
    JSON_VALUE(attributes, '$."skill_id"'),
    JSON_VALUE(attributes, '$."docubridge.skill_id"')
  ) AS skill_id,
  COALESCE(
    JSON_VALUE(attributes, '$."skill_version"'),
    JSON_VALUE(attributes, '$."docubridge.skill_version"')
  ) AS skill_version,
  COALESCE(
    JSON_VALUE(attributes, '$."entity_id"'),
    JSON_VALUE(attributes, '$."docubridge.entity_id"')
  ) AS entity_id,
  attributes
FROM `obs_traces._AllSpans`;

-- -----------------------------------------------------------------------------
-- 3. Sanitized Trace Text Payloads View (agent_eval.trace_payloads -> obs_logs._AllLogs + completions)
--   * Aggregated to 1 row per trace_id
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.trace_payloads` AS
SELECT
  COALESCE(
    REGEXP_EXTRACT(l.trace, r'traces/([0-9a-fA-F]+)$'),
    l.trace,
    JSON_VALUE(l.json_payload, '$."trace_id"')
  ) AS trace_id,
  ANY_VALUE(
    COALESCE(
      JSON_VALUE(l.resource.labels, '$."apphub.application.id"'),
      JSON_VALUE(l.json_payload, '$."apphub.application.id"'),
      JSON_VALUE(l.json_payload, '$."gcp.apphub.application.id"'),
      'unregistered_app'
    )
  ) AS apphub_application_id,
  MAX(l.timestamp) AS ingested_at,
  COALESCE(
    STRING_AGG(
      COALESCE(
        JSON_VALUE(c.payload, '$."user_prompt"'),
        JSON_VALUE(l.json_payload, '$."gen_ai.prompt"'),
        JSON_VALUE(l.json_payload, '$."user_prompt"')
      ),
      '\n' ORDER BY l.timestamp
    ),
    ''
  ) AS user_prompt,
  COALESCE(
    STRING_AGG(
      COALESCE(
        JSON_VALUE(c.payload, '$."retrieved_context"'),
        JSON_VALUE(l.json_payload, '$."gen_ai.retrieved_context"'),
        JSON_VALUE(l.json_payload, '$."retrieved_context"')
      ),
      '\n' ORDER BY l.timestamp
    ),
    ''
  ) AS retrieved_context,
  COALESCE(
    ARRAY_AGG(
      COALESCE(
        JSON_VALUE(c.payload, '$."final_response"'),
        JSON_VALUE(l.json_payload, '$."gen_ai.completion"'),
        JSON_VALUE(l.json_payload, '$."final_response"')
      ) IGNORE NULLS ORDER BY l.timestamp DESC LIMIT 1
    )[SAFE_OFFSET(0)],
    ''
  ) AS final_response
FROM `obs_logs._AllLogs` AS l
LEFT JOIN `agent_eval.completions` AS c
  ON c._FILE_NAME = COALESCE(
    JSON_VALUE(l.json_payload, '$."gen_ai.completion.uri"'),
    JSON_VALUE(l.json_payload, '$."gen_ai.input.messages_uri"'),
    JSON_VALUE(l.json_payload, '$."upload_uri"')
  )
WHERE COALESCE(l.trace, JSON_VALUE(l.json_payload, '$."trace_id"')) IS NOT NULL
GROUP BY trace_id;

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
  eval_run_id STRING OPTIONS (description = 'Logical evaluation batch or experiment run identifier.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Evaluated W3C trace identifier.'),
  outcome_id STRING OPTIONS (description = 'Business outcome identifier (EVAL-P11 outcome.id).'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Resolved application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Rubric identifier (e.g., groundedness_4pt_v1).'),
  judge_model STRING NOT NULL OPTIONS (description = 'Autorater model identifier (gemini-3.8-flash or gemini-4-argon).'),
  sampling_stratum STRING NOT NULL OPTIONS (description = 'Sampling reason: FULL_100PCT, SAMPLE_10PCT, ERROR_OR_SLA_TAIL, or CANARY_5PCT.'),
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
  description = 'Tier 2 AI.GENERATE_TABLE evaluation results with judge token telemetry and outcome_id attribution.'
);

ALTER TABLE `agent_eval.eval_results`
  ADD COLUMN IF NOT EXISTS outcome_id STRING OPTIONS (description = 'Business outcome identifier (EVAL-P11 outcome.id).');

-- -----------------------------------------------------------------------------
-- 6. Stratified SME Calibration Audit Labels (agent_eval.sme_labels)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.sme_labels` (
  label_id STRING NOT NULL OPTIONS (description = 'Unique SME calibration label identifier.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Audited trace identifier.'),
  outcome_id STRING OPTIONS (description = 'Business outcome identifier (EVAL-P11 outcome.id).'),
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

ALTER TABLE `agent_eval.sme_labels`
  ADD COLUMN IF NOT EXISTS outcome_id STRING OPTIONS (description = 'Business outcome identifier (EVAL-P11 outcome.id).');

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
-- 8. Isolated Synthetic Seed Tables in agent_eval_seed
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval_seed.seed_otel_spans` (
  template_idx INT64 NOT NULL,
  trace_id STRING NOT NULL,
  span_id STRING NOT NULL,
  parent_span_id STRING,
  span_offset_ms INT64 NOT NULL,
  latency_ms FLOAT64 NOT NULL,
  status_code STRING NOT NULL,
  span_kind STRING NOT NULL,
  service_name STRING NOT NULL,
  gcp_apphub_application_id STRING,
  gcp_apphub_service_id STRING,
  gcp_apphub_workload_id STRING,
  agent_name STRING NOT NULL,
  model_name STRING NOT NULL,
  input_tokens INT64 NOT NULL,
  cached_input_tokens INT64 NOT NULL,
  output_tokens INT64 NOT NULL,
  thinking_tokens INT64 NOT NULL,
  tool_name STRING,
  tool_call_status STRING,
  rule0_pass BOOL NOT NULL,
  finops_traffic_type STRING NOT NULL,
  attributes JSON
);

CREATE TABLE IF NOT EXISTS `agent_eval_seed.seed_trace_payloads` (
  template_idx INT64 NOT NULL,
  trace_id STRING NOT NULL,
  apphub_application_id STRING NOT NULL,
  user_prompt STRING NOT NULL,
  retrieved_context STRING NOT NULL,
  final_response STRING NOT NULL
);

-- -----------------------------------------------------------------------------
-- 9. Remote Cloud AI Autorater Models
-- -----------------------------------------------------------------------------
CREATE MODEL IF NOT EXISTS `agent_eval.gemini_3_8_flash_judge`
REMOTE WITH CONNECTION `us.vertex_ai_connection`
OPTIONS (
  endpoint = 'gemini-3.8-flash'
);

CREATE MODEL IF NOT EXISTS `agent_eval.gemini_4_argon_judge`
REMOTE WITH CONNECTION `us.vertex_ai_connection`
OPTIONS (
  endpoint = 'gemini-4-argon'
);
