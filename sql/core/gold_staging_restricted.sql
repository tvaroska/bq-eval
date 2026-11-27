-- Gold Split-SA Restricted Staging (`ADR-044`, `ADR-048`, `ADR-049`):
-- Hidden trace/outcome-grain tables in `${GOLD_INTERNAL_DATASET}` carrying `(business_unit, tenant_id)`
-- plus 4 text-free payload booleans (zero raw text, zero `JSON` columns), populated by the Restricted SA
-- from `silver_restricted` (zero `getData` grant on `silver_internal`), and read ONLY through 2-hop
-- Authorized Views in `agent_eval` filtering `(business_unit, tenant_id)` BEFORE summary aggregation.

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}`
OPTIONS (
  description = 'Hidden trace/outcome-grain Gold staging tables accessed exclusively via Authorized Views in agent_eval.'
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_trace_eval` (
  trace_id STRING NOT NULL,
  apphub_application_id STRING NOT NULL,
  business_unit STRING,
  tenant_id STRING,
  effective_risk_tier STRING,
  risk_tier STRING,
  outcome_id STRING,
  outcome_owner STRING,
  conversation_id STRING,
  previous_interaction_id STRING,
  trace_start_time TIMESTAMP NOT NULL,
  trace_end_time TIMESTAMP,
  trace_latency_ms FLOAT64,
  span_count INT64,
  model_call_count INT64,
  tool_call_count INT64,
  tool_error_count INT64,
  http_429_count INT64,
  content_missing_call_count INT64,
  platform_guardrail_blocks INT64,
  rule0_pass BOOL,
  is_loop_or_error_tail BOOL,
  is_healthy_slow_tail BOOL,
  primary_traffic_type STRING,
  weighted_burndown_tokens FLOAT64,
  production_inference_usd FLOAT64,
  conflict_escalation_usd FLOAT64,
  agent_gym_eval_usd FLOAT64,
  span_autorater_judge_usd FLOAT64,
  optimize_usd FLOAT64,
  unclassified_usd FLOAT64,
  unpriced_spans INT64,
  trace_span_cost_usd FLOAT64,
  cache_hit_ratio FLOAT64,
  has_trace_payload BOOL NOT NULL,
  empty_prior_history BOOL NOT NULL,
  payload_read_incomplete BOOL NOT NULL,
  prior_turn_matched BOOL NOT NULL,
  staged_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(trace_start_time)
CLUSTER BY business_unit, tenant_id, apphub_application_id, trace_id
OPTIONS (
  description = 'Restricted-SA trace-grain Gold evaluation staging carrying (business_unit, tenant_id) and 4 text-free payload booleans.'
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_outcomes` (
  outcome_id STRING NOT NULL,
  apphub_application_id STRING NOT NULL,
  business_unit STRING,
  tenant_id STRING,
  outcome_status STRING,
  is_verified BOOL NOT NULL,
  activity_time TIMESTAMP NOT NULL,
  unpriced_spans INT64,
  ai_cost_usd FLOAT64,
  infra_cost_usd FLOAT64,
  staged_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(activity_time)
CLUSTER BY business_unit, tenant_id, apphub_application_id, outcome_id
OPTIONS (
  description = 'Restricted-SA outcome-grain Gold staging carrying (business_unit, tenant_id) for tenant-safe CPVO.'
);

CREATE OR REPLACE PROCEDURE `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.refresh_gold_restricted_staging`(
  IN p_from_time TIMESTAMP,
  IN p_to_time TIMESTAMP
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();

  -- Read restricted Silver TVFs once into session TEMP TABLEs so downstream writes never re-scan Bronze.
  CREATE TEMP TABLE _tmp_te AS
  SELECT *
  FROM `agent_eval.gold_trace_sql_evaluation`(p_from_time, p_to_time);

  CREATE TEMP TABLE _tmp_tr_tenant AS
  SELECT trace_id, business_unit AS raw_business_unit, tenant_id
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.traces`(p_from_time, p_to_time);

  CREATE TEMP TABLE _tmp_tp AS
  SELECT
    trace_id,
    previous_interaction_id,
    COALESCE(TO_JSON_STRING(prior_history), '') IN ('', 'null') AS empty_prior_history,
    NOT (
      COALESCE(turn_prompt_read_whole, TRUE)
      AND COALESCE(turn_context_read_whole, TRUE)
      AND COALESCE(turn_output_read_whole, TRUE)
      AND COALESCE(step_outputs_read_whole, TRUE)
      AND (previous_interaction_id IS NOT NULL OR COALESCE(prior_history_read_whole, TRUE))
    ) AS payload_read_incomplete
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.trace_payloads`(p_from_time, p_to_time);

  CREATE TEMP TABLE _tmp_prior_tp AS
  SELECT te.trace_id, te.conversation_id, te.trace_start_time
  FROM _tmp_te AS te
  INNER JOIN _tmp_tp AS tp ON te.trace_id = tp.trace_id
  UNION DISTINCT
  SELECT trace_id, conversation_id, trace_start_time
  FROM `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_trace_eval`
  WHERE has_trace_payload IS TRUE
    AND trace_start_time >= TIMESTAMP_SUB(p_from_time, INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR)
    AND trace_start_time < p_to_time;

  DELETE FROM `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_trace_eval`
  WHERE trace_start_time >= TIMESTAMP_SUB(p_from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    AND trace_start_time < TIMESTAMP_ADD(p_to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    AND trace_id IN (SELECT DISTINCT trace_id FROM _tmp_te);

  INSERT INTO `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_trace_eval`
  SELECT
    te.trace_id,
    te.apphub_application_id,
    tt.raw_business_unit AS business_unit,
    tt.tenant_id,
    te.effective_risk_tier,
    te.risk_tier,
    te.outcome_id,
    te.outcome_owner,
    te.conversation_id,
    COALESCE(tp.previous_interaction_id, te.previous_interaction_id) AS previous_interaction_id,
    te.trace_start_time,
    te.trace_end_time,
    te.trace_latency_ms,
    te.span_count,
    te.model_call_count,
    te.tool_call_count,
    te.tool_error_count,
    te.http_429_count,
    te.content_missing_call_count,
    te.platform_guardrail_blocks,
    te.rule0_pass,
    te.is_loop_or_error_tail,
    te.is_healthy_slow_tail,
    te.primary_traffic_type,
    te.weighted_burndown_tokens,
    te.production_inference_usd,
    te.conflict_escalation_usd,
    te.agent_gym_eval_usd,
    te.span_autorater_judge_usd,
    te.optimize_usd,
    te.unclassified_usd,
    te.unpriced_spans,
    te.trace_span_cost_usd,
    te.cache_hit_ratio,
    (tp.trace_id IS NOT NULL) AS has_trace_payload,
    COALESCE(tp.empty_prior_history, TRUE) AS empty_prior_history,
    COALESCE(tp.payload_read_incomplete, FALSE) AS payload_read_incomplete,
    COALESCE(
      (ptp.trace_id IS NOT NULL)
      OR (
        te.conversation_id IS NOT NULL
        AND pconv.min_payload_ts < te.trace_start_time
      ),
      FALSE
    ) AS prior_turn_matched,
    v_now AS staged_at
  FROM _tmp_te AS te
  LEFT JOIN _tmp_tr_tenant AS tt ON te.trace_id = tt.trace_id
  LEFT JOIN _tmp_tp AS tp ON te.trace_id = tp.trace_id
  LEFT JOIN (SELECT DISTINCT trace_id FROM _tmp_prior_tp) AS ptp
    ON tp.previous_interaction_id IS NOT NULL
    AND tp.previous_interaction_id != te.trace_id
    AND ptp.trace_id = tp.previous_interaction_id
  LEFT JOIN (
    SELECT conversation_id, MIN(trace_start_time) AS min_payload_ts
    FROM _tmp_prior_tp
    WHERE conversation_id IS NOT NULL
    GROUP BY conversation_id
  ) AS pconv
    ON te.conversation_id = pconv.conversation_id;

  CREATE TEMP TABLE _tmp_outcomes AS
  SELECT
    o.outcome_id,
    COALESCE(o.outcome_owner, te_agg.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    o.business_unit,
    COALESCE(te_agg.tenant_id, ev_agg.tenant_id) AS tenant_id,
    COALESCE(o.outcome_status, IF(o.is_closed, 'unknown', 'open')) AS outcome_status,
    COALESCE(
      o.is_closed
      AND COALESCE(o.outcome_status, 'unknown') = 'succeeded'
      AND COALESCE(o.trace_count, 0) > 0
      AND o.root_error_trace_count = 0
      AND o.failed_trace_count = 0
      AND o.rootless_trace_count = 0
      AND COALESCE(te_agg.all_r0, FALSE),
      FALSE
    ) AS is_verified,
    COALESCE(o.close_time, o.open_time, te_agg.first_trace_time, p_from_time) AS activity_time,
    COALESCE(te_agg.unpriced_spans, 0) AS unpriced_spans,
    te_agg.ai_cost_usd,
    0.0 AS infra_cost_usd,
    v_now AS staged_at
  FROM (
    SELECT *
    FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.outcomes`(p_from_time, p_to_time)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY outcome_id ORDER BY is_closed DESC, close_time DESC) = 1
  ) AS o
  LEFT JOIN (
    SELECT
      outcome_id,
      MAX(apphub_application_id) AS apphub_application_id,
      MAX(tenant_id) AS tenant_id,
      MIN(trace_start_time) AS first_trace_time,
      LOGICAL_AND(COALESCE(rule0_pass, FALSE)) AS all_r0,
      SUM(COALESCE(unpriced_spans, 0)) AS unpriced_spans,
      IF(COUNTIF(trace_span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(trace_span_cost_usd)) AS ai_cost_usd
    FROM `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_trace_eval`
    WHERE outcome_id IS NOT NULL
      AND trace_start_time >= TIMESTAMP_SUB(p_from_time, INTERVAL ${OUTCOME_LOOKBACK_DAYS} DAY)
      AND trace_start_time < TIMESTAMP_ADD(p_to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    GROUP BY outcome_id
  ) AS te_agg
    ON o.outcome_id = te_agg.outcome_id
  LEFT JOIN (
    SELECT outcome_id, MAX(tenant_id) AS tenant_id
    FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.events`(p_from_time, p_to_time)
    WHERE outcome_id IS NOT NULL AND tenant_id IS NOT NULL
    GROUP BY outcome_id
  ) AS ev_agg
    ON o.outcome_id = ev_agg.outcome_id;

  DELETE FROM `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_outcomes`
  WHERE outcome_id IN (SELECT outcome_id FROM _tmp_outcomes);

  INSERT INTO `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_outcomes`
  SELECT * FROM _tmp_outcomes;
END;

-- 2-hop Authorized View filtering `(business_unit, tenant_id)` BEFORE exposing trace rows.
CREATE OR REPLACE VIEW `agent_eval.v_staged_trace_eval` AS
SELECT
  t.trace_id, t.apphub_application_id, t.business_unit, t.tenant_id,
  t.effective_risk_tier, t.risk_tier, t.outcome_id, t.outcome_owner,
  t.conversation_id, t.previous_interaction_id, t.trace_start_time, t.trace_end_time,
  t.trace_latency_ms, t.span_count, t.model_call_count, t.tool_call_count,
  t.tool_error_count, t.http_429_count, t.content_missing_call_count,
  t.platform_guardrail_blocks, t.rule0_pass, t.is_loop_or_error_tail,
  t.is_healthy_slow_tail, t.primary_traffic_type, t.weighted_burndown_tokens,
  t.production_inference_usd, t.conflict_escalation_usd, t.agent_gym_eval_usd,
  t.span_autorater_judge_usd, t.optimize_usd, t.unclassified_usd,
  t.unpriced_spans, t.trace_span_cost_usd, t.cache_hit_ratio,
  t.has_trace_payload, t.empty_prior_history, t.payload_read_incomplete,
  t.prior_turn_matched, t.staged_at
FROM `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_trace_eval` AS t
WHERE EXISTS (
  SELECT 1
  FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.unit_entitlements` AS ue
  WHERE LOWER(ue.principal_email) = LOWER(SESSION_USER())
    AND ue.business_unit = COALESCE(t.business_unit, '__unowned__')
    AND (ue.tenant_id = '*' OR ue.tenant_id = t.tenant_id)
);

-- 2-hop Authorized Summary View: filters `(business_unit, tenant_id)` FIRST, then aggregates.
CREATE OR REPLACE VIEW `agent_eval.v_staged_agent_eval_and_finops_summary` AS
WITH entitled_traces AS (
  SELECT *
  FROM `agent_eval.v_staged_trace_eval`
),
entitled_outcomes AS (
  SELECT o.*
  FROM `${PROJECT_ID}.${GOLD_INTERNAL_DATASET}.gold_staged_outcomes` AS o
  WHERE EXISTS (
    SELECT 1
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.unit_entitlements` AS ue
    WHERE LOWER(ue.principal_email) = LOWER(SESSION_USER())
      AND ue.business_unit = COALESCE(o.business_unit, '__unowned__')
      AND (ue.tenant_id = '*' OR ue.tenant_id = o.tenant_id)
  )
),
entitled_evals AS (
  SELECT
    e.apphub_application_id AS app_id,
    e.rubric_id,
    e.trace_id,
    e.binary_pass,
    ((e.judge_input_tokens / 1e6) * COALESCE(d.input_usd_per_1m, 0.0))
      + (((e.judge_output_tokens + e.judge_thinking_tokens) / 1e6) * COALESCE(d.output_usd_per_1m, 0.0)) AS jc
  FROM (
    SELECT *
    FROM `agent_eval.eval_results`
    WHERE NOT COALESCE(is_synthetic, FALSE)
      AND COALESCE(judge_status, 'SCORED') = 'SCORED'
      AND binary_pass IS NOT NULL
      AND (ordinal_score IS NULL OR ordinal_score BETWEEN 1 AND 4)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY apphub_application_id, trace_id, rubric_id ORDER BY evaluated_at DESC) = 1
  ) AS e
  INNER JOIN entitled_traces AS et ON e.trace_id = et.trace_id
  LEFT JOIN `agent_eval.v_default_model_pricing` AS d
    ON REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(TRIM(e.judge_model), r'@.*$', ''), r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') = d.model_name
),
app_rubrics AS (
  SELECT app_id, rubric_id, SUM(jc) AS rubric_judge_usd
  FROM entitled_evals
  GROUP BY 1, 2
),
app_outcomes AS (
  SELECT
    apphub_application_id AS app_id,
    COUNT(DISTINCT outcome_id) AS outcome_total_count,
    COUNT(DISTINCT IF(is_verified, outcome_id, NULL)) AS outcome_verified_count,
    IF(SUM(COALESCE(unpriced_spans, 0)) > 0, CAST(NULL AS FLOAT64), SUM(COALESCE(ai_cost_usd, 0.0))) AS outcome_ai_cost_usd
  FROM entitled_outcomes
  GROUP BY 1
)
SELECT
  t.apphub_application_id,
  COALESCE(ar.rubric_id, 'groundedness_4pt_v1') AS rubric_id,
  MIN(t.effective_risk_tier) AS risk_tier,
  COUNT(*) AS total_traces,
  COUNTIF(t.primary_traffic_type IN ('production_inference', 'conflict_escalation')) AS production_traces,
  AVG(IF(t.rule0_pass, 1.0, 0.0)) AS rule0_pass_rate,
  AVG(IF(t.tool_error_count = 0, 1.0, 0.0)) AS tool_success_rate,
  APPROX_QUANTILES(t.trace_latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
  COALESCE(AVG(t.cache_hit_ratio), 0.0) AS avg_cache_hit_ratio,
  SUM(t.weighted_burndown_tokens) AS total_weighted_burndown_tokens,
  COUNTIF(
    t.primary_traffic_type IN ('production_inference', 'conflict_escalation')
    AND t.rule0_pass
    AND t.has_trace_payload
    AND (t.payload_read_incomplete OR (t.previous_interaction_id IS NOT NULL AND t.empty_prior_history AND NOT t.prior_turn_matched))
  ) AS missing_session_context_count,
  COUNT(ee.trace_id) AS judged_samples,
  AVG(IF(ee.trace_id IS NOT NULL, IF(ee.binary_pass, 1.0, 0.0), NULL)) AS raw_judge_pass_rate,
  SUM(COALESCE(t.unpriced_spans, 0)) AS unpriced_spans,
  IF(COUNTIF(t.production_inference_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.production_inference_usd)) AS production_inference_usd,
  IF(COUNTIF(t.conflict_escalation_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.conflict_escalation_usd)) AS conflict_escalation_usd,
  IF(COUNTIF(t.agent_gym_eval_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.agent_gym_eval_usd)) AS agent_gym_eval_usd,
  IF(
    COUNTIF(t.span_autorater_judge_usd IS NULL) > 0,
    CAST(NULL AS FLOAT64),
    GREATEST(SUM(t.span_autorater_judge_usd), COALESCE(MAX(ar.rubric_judge_usd), 0.0))
  ) AS autorater_judge_usd,
  IF(COUNTIF(t.optimize_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.optimize_usd)) AS optimize_usd,
  IF(COUNTIF(t.unclassified_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.unclassified_usd)) AS unclassified_usd,
  IF(
    SUM(COALESCE(t.unpriced_spans, 0)) > 0,
    CAST(NULL AS FLOAT64),
    SUM(COALESCE(t.trace_span_cost_usd, 0.0))
      + GREATEST(0.0, COALESCE(MAX(ar.rubric_judge_usd), 0.0) - SUM(COALESCE(t.span_autorater_judge_usd, 0.0)))
  ) AS total_spend_usd,
  COALESCE(MAX(ao.outcome_total_count), 0) AS outcome_total_count,
  COALESCE(MAX(ao.outcome_verified_count), 0) AS outcome_verified_count,
  SAFE_DIVIDE(MAX(ao.outcome_ai_cost_usd), NULLIF(MAX(ao.outcome_verified_count), 0)) AS outcome_cost_per_verified_ai_usd
FROM entitled_traces AS t
LEFT JOIN app_rubrics AS ar ON t.apphub_application_id = ar.app_id
LEFT JOIN entitled_evals AS ee
  ON ar.rubric_id IS NOT NULL
  AND t.trace_id = ee.trace_id
  AND ar.app_id = ee.app_id
  AND ar.rubric_id = ee.rubric_id
LEFT JOIN app_outcomes AS ao ON t.apphub_application_id = ao.app_id
GROUP BY t.apphub_application_id, COALESCE(ar.rubric_id, 'groundedness_4pt_v1');
