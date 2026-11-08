-- =============================================================================
-- BigQuery Agent Evaluation, Observability & FinOps Reference Views
-- Standard: EVAL-P04, EVAL-P05, EVAL-P06, EVAL-P07, EVAL-P08 (v2026.11.2)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- View 1: Span-Level Waterfall with Portable App ID & Long-Context Burndown
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_trace_waterfall` AS
SELECT
  s.trace_id,
  s.span_id,
  s.parent_span_id,
  COALESCE(
    s.gcp_apphub_application_id,
    JSON_VALUE(s.attributes, '$."gcp.apphub.application.id"'),
    JSON_VALUE(s.attributes, '$."apphub.application.id"'),
    'unregistered_app'
  ) AS apphub_application_id,
  s.service_name,
  s.agent_name,
  s.span_kind,
  s.model_name,
  s.tool_name,
  s.tool_call_status,
  s.status_code,
  s.rule0_pass,
  s.finops_traffic_type,
  s.start_time,
  s.end_time,
  s.latency_ms,
  s.input_tokens,
  s.cached_input_tokens,
  GREATEST(0, s.input_tokens - s.cached_input_tokens) AS uncached_input_tokens,
  s.output_tokens,
  s.thinking_tokens,
  s.input_tokens > 200000 AS is_long_context_cliff,
  -- Span-level weighted GSU burndown (1.0x/0.1x/4.0x <=200K; 2.0x/0.2x/7.5x >200K)
  CASE
    WHEN s.input_tokens > 200000 THEN
      (2.0 * GREATEST(0, s.input_tokens - s.cached_input_tokens))
      + (0.2 * s.cached_input_tokens)
      + (7.5 * (s.output_tokens + s.thinking_tokens))
    ELSE
      (1.0 * GREATEST(0, s.input_tokens - s.cached_input_tokens))
      + (0.1 * s.cached_input_tokens)
      + (4.0 * (s.output_tokens + s.thinking_tokens))
  END AS weighted_burndown_tokens,
  -- Span-level USD cost joined against effective-dated model_pricing_catalog
  CASE
    WHEN s.input_tokens > COALESCE(m.long_context_threshold_tokens, 200000) THEN
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * COALESCE(m.long_context_input_usd_per_1m, 0.30)
      + (s.cached_input_tokens / 1e6) * COALESCE(m.long_context_cached_usd_per_1m, 0.03)
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * COALESCE(m.long_context_output_usd_per_1m, 2.25)
    ELSE
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * COALESCE(m.input_usd_per_1m, 0.15)
      + (s.cached_input_tokens / 1e6) * COALESCE(m.cached_input_usd_per_1m, 0.015)
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * COALESCE(m.output_usd_per_1m, 0.60)
  END AS span_cost_usd
FROM `agent_eval.otel_spans` AS s
LEFT JOIN `agent_eval.model_pricing_catalog` AS m
  ON s.model_name = m.model_name
  AND s.start_time >= m.effective_start
  AND s.start_time < m.effective_end;

-- -----------------------------------------------------------------------------
-- View 2: Tier 1 Trace-Level Deterministic SQL Evaluation (100% Traces, $0 LLM)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_trace_sql_evaluation` AS
WITH trace_agg AS (
  SELECT
    w.trace_id,
    ANY_VALUE(w.apphub_application_id) AS apphub_application_id,
    MIN(w.start_time) AS trace_start_time,
    MAX(w.end_time) AS trace_end_time,
    TIMESTAMP_DIFF(MAX(w.end_time), MIN(w.start_time), MILLISECOND) AS trace_latency_ms,
    COUNT(*) AS span_count,
    COUNTIF(w.span_kind = 'execute_tool') AS tool_call_count,
    COUNTIF(w.span_kind = 'execute_tool' AND (w.status_code = 'ERROR' OR w.tool_call_status != 'SUCCESS')) AS tool_error_count,
    LOGICAL_AND(w.rule0_pass) AND COUNTIF(w.status_code = 'ERROR') = 0 AS rule0_pass,
    LOGICAL_OR(w.finops_traffic_type = 'conflict_escalation') AS escalated_to_high_reasoning,
    LOGICAL_OR(w.is_long_context_cliff) AS hit_long_context_cliff,
    ANY_VALUE(w.finops_traffic_type) AS primary_traffic_type,
    SUM(w.input_tokens) AS total_input_tokens,
    SUM(w.cached_input_tokens) AS total_cached_input_tokens,
    SUM(w.uncached_input_tokens) AS total_uncached_input_tokens,
    SUM(w.output_tokens) AS total_output_tokens,
    SUM(w.thinking_tokens) AS total_thinking_tokens,
    SUM(w.weighted_burndown_tokens) AS weighted_burndown_tokens,
    SUM(CASE WHEN w.finops_traffic_type = 'production_inference' THEN w.span_cost_usd ELSE 0.0 END) AS production_inference_usd,
    SUM(CASE WHEN w.finops_traffic_type = 'conflict_escalation' THEN w.span_cost_usd ELSE 0.0 END) AS conflict_escalation_usd,
    SUM(CASE WHEN w.finops_traffic_type = 'agent_gym_eval' THEN w.span_cost_usd ELSE 0.0 END) AS agent_gym_eval_usd,
    SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END) AS span_autorater_judge_usd,
    SUM(w.span_cost_usd) AS trace_span_cost_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  GROUP BY w.trace_id
)
SELECT
  t.*,
  COALESCE(r.business_unit, 'unassigned_bu') AS business_unit,
  COALESCE(r.sla_latency_ms, 5000.0) AS sla_latency_ms,
  COALESCE(r.cost_center, 'UNASSIGNED') AS cost_center,
  SAFE_DIVIDE(t.total_cached_input_tokens, NULLIF(t.total_input_tokens, 0)) AS cache_hit_ratio,
  SAFE_DIVIDE(t.total_thinking_tokens, NULLIF(t.total_output_tokens + t.total_thinking_tokens, 0)) AS thinking_token_ratio
FROM trace_agg AS t
LEFT JOIN `agent_eval.app_registry` AS r
  ON t.apphub_application_id = r.apphub_application_id;

-- -----------------------------------------------------------------------------
-- View 2B: Step 1 Cost per Application, Business Unit & Outcome (0 New Tables)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step1_cost_per_outcome` AS
SELECT
  apphub_application_id,
  business_unit,
  cost_center,
  COUNT(*) AS total_traces,
  COUNTIF(rule0_pass) AS step1_passing_traces,
  AVG(IF(rule0_pass, 1.0, 0.0)) AS step1_pass_rate,
  AVG(IF(tool_error_count = 0, 1.0, 0.0)) AS tool_success_rate,
  APPROX_QUANTILES(trace_latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
  SUM(trace_span_cost_usd) AS total_cost_usd,
  SAFE_DIVIDE(SUM(trace_span_cost_usd), COUNT(*)) AS cost_per_trace_usd,
  SAFE_DIVIDE(SUM(trace_span_cost_usd), NULLIF(COUNTIF(rule0_pass), 0)) AS step1_cost_per_outcome_usd
FROM `agent_eval.v_trace_sql_evaluation`
GROUP BY apphub_application_id, business_unit, cost_center;

-- -----------------------------------------------------------------------------
-- View 3: Unified Tier 1-3 Calibrated Quality & 4-Bucket FinOps Summary
--   * Conditional Rule 0 conditioning: p_hat_obs estimated strictly on rule0_pass = TRUE
--   * Fail-closed NULL guard when judged_samples = 0 or Youden J <= 1e-6
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_agent_eval_and_finops_summary` AS
WITH trace_metrics AS (
  SELECT
    apphub_application_id,
    COUNT(*) AS total_traces,
    COUNTIF(primary_traffic_type IN ('production_inference', 'conflict_escalation')) AS production_traces,
    AVG(IF(rule0_pass, 1.0, 0.0)) AS rule0_pass_rate,
    AVG(IF(tool_error_count = 0, 1.0, 0.0)) AS tool_success_rate,
    APPROX_QUANTILES(trace_latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
    AVG(cache_hit_ratio) AS avg_cache_hit_ratio,
    SUM(weighted_burndown_tokens) AS total_weighted_burndown_tokens,
    SUM(production_inference_usd) AS production_inference_usd,
    SUM(conflict_escalation_usd) AS conflict_escalation_usd,
    SUM(agent_gym_eval_usd) AS agent_gym_eval_usd,
    SUM(span_autorater_judge_usd) AS span_autorater_judge_usd
  FROM `agent_eval.v_trace_sql_evaluation`
  GROUP BY apphub_application_id
),
judge_metrics AS (
  SELECT
    e.apphub_application_id,
    e.rubric_id,
    COUNTIF(e.rule0_pass) AS judged_samples,
    COUNT(*) AS total_judge_invocations,
    AVG(CASE WHEN e.rule0_pass THEN IF(e.binary_pass, 1.0, 0.0) END) AS raw_judge_pass_rate,
    SUM(
      ((e.judge_input_tokens / 1e6) * COALESCE(m.input_usd_per_1m, 1.25))
      + (((e.judge_output_tokens + e.judge_thinking_tokens) / 1e6) * COALESCE(m.output_usd_per_1m, 5.00))
    ) AS eval_table_judge_usd
  FROM `agent_eval.eval_results` AS e
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m
    ON e.judge_model = m.model_name
    AND e.evaluated_at >= m.effective_start
    AND e.evaluated_at < m.effective_end
  GROUP BY e.apphub_application_id, e.rubric_id
),
sme_calibration AS (
  SELECT
    apphub_application_id,
    rubric_id,
    COUNTIF(human_pass AND judge_pass) AS tp,
    COUNTIF(human_pass AND NOT judge_pass) AS fn,
    COUNTIF(NOT human_pass AND NOT judge_pass) AS tn,
    COUNTIF(NOT human_pass AND judge_pass) AS fp,
    SAFE_DIVIDE(
      COUNTIF(human_pass AND judge_pass),
      NULLIF(COUNTIF(human_pass), 0)
    ) AS tpr,
    SAFE_DIVIDE(
      COUNTIF(NOT human_pass AND NOT judge_pass),
      NULLIF(COUNTIF(NOT human_pass), 0)
    ) AS tnr
  FROM `agent_eval.sme_labels`
  WHERE rule0_pass = TRUE
    AND human_pass IS NOT NULL
    AND judge_pass IS NOT NULL
  GROUP BY apphub_application_id, rubric_id
),
calibrated AS (
  SELECT
    tm.apphub_application_id,
    COALESCE(jm.rubric_id, 'groundedness_4pt_v1') AS rubric_id,
    tm.total_traces,
    tm.production_traces,
    tm.rule0_pass_rate,
    tm.tool_success_rate,
    tm.p95_latency_ms,
    tm.avg_cache_hit_ratio,
    tm.total_weighted_burndown_tokens,
    COALESCE(jm.judged_samples, 0) AS judged_samples,
    jm.raw_judge_pass_rate,
    sc.tpr,
    sc.tnr,
    (sc.tpr + sc.tnr - 1.0) AS youden_j,
    CASE
      WHEN COALESCE(jm.judged_samples, 0) = 0 OR jm.raw_judge_pass_rate IS NULL THEN 'UNJUDGED'
      WHEN sc.tpr IS NULL OR sc.tnr IS NULL OR (sc.tpr + sc.tnr - 1.0) <= 1e-6 THEN 'UNCALIBRATED'
      ELSE 'CALIBRATED'
    END AS calibration_status,
    -- Rogan-Gladen point estimate: theta_RG = clip((p_obs + TNR - 1) / (TPR + TNR - 1), 0, 1)
    CASE
      WHEN COALESCE(jm.judged_samples, 0) = 0 OR jm.raw_judge_pass_rate IS NULL THEN NULL
      WHEN sc.tpr IS NULL OR sc.tnr IS NULL OR (sc.tpr + sc.tnr - 1.0) <= 1e-6 THEN NULL
      ELSE LEAST(
        1.0,
        GREATEST(
          0.0,
          SAFE_DIVIDE(jm.raw_judge_pass_rate + sc.tnr - 1.0, sc.tpr + sc.tnr - 1.0)
        )
      )
    END AS theta_rg,
    -- Variance components for 95% CI propagation
    SAFE_DIVIDE(
      jm.raw_judge_pass_rate * (1.0 - jm.raw_judge_pass_rate),
      NULLIF(jm.judged_samples, 0)
    ) AS var_p_obs,
    SAFE_DIVIDE(
      sc.tpr * (1.0 - sc.tpr),
      NULLIF(sc.tp + sc.fn, 0)
    ) AS var_tpr,
    SAFE_DIVIDE(
      sc.tnr * (1.0 - sc.tnr),
      NULLIF(sc.tn + sc.fp, 0)
    ) AS var_tnr,
    tm.production_inference_usd,
    tm.conflict_escalation_usd,
    tm.agent_gym_eval_usd,
    GREATEST(tm.span_autorater_judge_usd, COALESCE(jm.eval_table_judge_usd, 0.0)) AS autorater_judge_usd
  FROM trace_metrics AS tm
  LEFT JOIN judge_metrics AS jm
    ON tm.apphub_application_id = jm.apphub_application_id
  LEFT JOIN sme_calibration AS sc
    ON tm.apphub_application_id = sc.apphub_application_id
    AND jm.rubric_id = sc.rubric_id
)
SELECT
  c.apphub_application_id,
  c.rubric_id,
  c.total_traces,
  c.production_traces,
  c.rule0_pass_rate,
  c.tool_success_rate,
  c.p95_latency_ms,
  c.avg_cache_hit_ratio,
  c.total_weighted_burndown_tokens,
  c.judged_samples,
  c.calibration_status,
  c.raw_judge_pass_rate,
  c.theta_rg AS calibrated_pass_rate,
  CASE
    WHEN c.theta_rg IS NULL THEN NULL
    ELSE LEAST(
      1.0,
      GREATEST(
        0.0,
        c.theta_rg - 1.959963984540054 * SAFE_DIVIDE(
          SQRT(
            COALESCE(c.var_p_obs, 0.0)
            + POW(c.theta_rg, 2) * COALESCE(c.var_tpr, 0.0)
            + POW(1.0 - c.theta_rg, 2) * COALESCE(c.var_tnr, 0.0)
          ),
          NULLIF(c.youden_j, 0.0)
        )
      )
    )
  END AS calibrated_ci_95_lower,
  CASE
    WHEN c.theta_rg IS NULL THEN NULL
    ELSE LEAST(
      1.0,
      GREATEST(
        0.0,
        c.theta_rg + 1.959963984540054 * SAFE_DIVIDE(
          SQRT(
            COALESCE(c.var_p_obs, 0.0)
            + POW(c.theta_rg, 2) * COALESCE(c.var_tpr, 0.0)
            + POW(1.0 - c.theta_rg, 2) * COALESCE(c.var_tnr, 0.0)
          ),
          NULLIF(c.youden_j, 0.0)
        )
      )
    )
  END AS calibrated_ci_95_upper,
  c.production_inference_usd,
  c.conflict_escalation_usd,
  c.agent_gym_eval_usd,
  c.autorater_judge_usd,
  (c.production_inference_usd + c.conflict_escalation_usd + c.agent_gym_eval_usd + c.autorater_judge_usd) AS total_spend_usd,
  CASE
    WHEN c.theta_rg IS NULL OR c.theta_rg <= 0.0 OR c.rule0_pass_rate <= 0.0 OR c.production_traces = 0 THEN NULL
    ELSE SAFE_DIVIDE(
      c.production_inference_usd + c.conflict_escalation_usd + c.agent_gym_eval_usd + c.autorater_judge_usd,
      c.production_traces * c.rule0_pass_rate * c.theta_rg
    )
  END AS cost_per_verified_outcome_usd
FROM calibrated AS c;

-- -----------------------------------------------------------------------------
-- View 4: Hourly Weighted Burndown Export for Provisioned Throughput & FOCUS 1.2
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_capacity_hourly_burndown_export` AS
SELECT
  TIMESTAMP_TRUNC(w.start_time, HOUR) AS usage_hour,
  w.apphub_application_id,
  w.model_name,
  w.finops_traffic_type,
  COUNT(DISTINCT w.trace_id) AS distinct_traces,
  COUNT(*) AS span_count,
  SUM(w.input_tokens) AS input_tokens,
  SUM(w.cached_input_tokens) AS cached_input_tokens,
  SUM(w.uncached_input_tokens) AS uncached_input_tokens,
  SUM(w.output_tokens) AS output_tokens,
  SUM(w.thinking_tokens) AS thinking_tokens,
  COUNTIF(w.is_long_context_cliff) AS long_context_cliff_spans,
  SUM(w.weighted_burndown_tokens) AS weighted_burndown_tokens,
  SUM(w.span_cost_usd) AS total_cost_usd
FROM `agent_eval.v_trace_waterfall` AS w
GROUP BY
  usage_hour,
  w.apphub_application_id,
  w.model_name,
  w.finops_traffic_type;
