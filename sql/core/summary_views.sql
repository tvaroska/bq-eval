-- =============================================================================
-- BQ Eval & FinOps (bq-eval) — Unified Calibrated Quality & FinOps Summary View
--   * Conditional Rule 0 conditioning + StratPPI + Quadratic Gwet's AC2
--   * Boundary-safe Lang & Reiczigel (2014) 95% CIs + RL-1..RL-5 tiered gates
--   * Deploy after `sql/core/views.sql`.
-- =============================================================================

CREATE OR REPLACE VIEW `agent_eval.v_agent_eval_and_finops_summary` AS
WITH trace_metrics AS (
  SELECT
    apphub_application_id,
    COUNT(*) AS total_traces,
    COUNTIF(primary_traffic_type IN ('production_inference', 'conflict_escalation')) AS production_traces,
    COUNTIF(primary_traffic_type IN ('production_inference', 'conflict_escalation') AND rule0_pass) AS production_rule0_passing_traces,
    AVG(IF(rule0_pass, 1.0, 0.0)) AS rule0_pass_rate,
    AVG(IF(tool_error_count = 0, 1.0, 0.0)) AS tool_success_rate,
    APPROX_QUANTILES(trace_latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
    COALESCE(AVG(cache_hit_ratio), 0.0) AS avg_cache_hit_ratio,
    SUM(weighted_burndown_tokens) AS total_weighted_burndown_tokens,
    SUM(unpriced_spans) AS unpriced_spans,
    IF(COUNTIF(production_inference_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(production_inference_usd)) AS production_inference_usd,
    IF(COUNTIF(conflict_escalation_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(conflict_escalation_usd)) AS conflict_escalation_usd,
    IF(COUNTIF(agent_gym_eval_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(agent_gym_eval_usd)) AS agent_gym_eval_usd,
    IF(COUNTIF(span_autorater_judge_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(span_autorater_judge_usd)) AS span_autorater_judge_usd,
    IF(COUNTIF(optimize_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(optimize_usd)) AS optimize_usd,
    IF(COUNTIF(unclassified_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(unclassified_usd)) AS unclassified_usd
  FROM `agent_eval.v_trace_sql_evaluation`
  GROUP BY apphub_application_id
),
stratum_population AS (
  SELECT
    apphub_application_id,
    CASE
      WHEN tool_error_count > 0 OR trace_latency_ms > sla_latency_ms THEN 'ERROR_OR_SLA_TAIL'
      ELSE 'HEALTHY_POPULATION'
    END AS stratum_bucket,
    COUNT(*) AS stratum_pop_count
  FROM `agent_eval.v_trace_sql_evaluation`
  WHERE rule0_pass = TRUE
  GROUP BY apphub_application_id, stratum_bucket
),
deduped_eval_results AS (
  SELECT *
  FROM `agent_eval.eval_results`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY COALESCE(eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', evaluated_at)), trace_id, rubric_id
    ORDER BY evaluated_at DESC
  ) = 1
),
priced_judge_evals AS (
  SELECT
    e.eval_id,
    e.apphub_application_id,
    e.rubric_id,
    e.judge_input_tokens,
    e.judge_output_tokens,
    e.judge_thinking_tokens,
    COALESCE(m.input_usd_per_1m, d.input_usd_per_1m) AS input_usd_per_1m,
    COALESCE(m.output_usd_per_1m, d.output_usd_per_1m) AS output_usd_per_1m
  FROM deduped_eval_results AS e
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m
    ON REGEXP_REPLACE(REGEXP_REPLACE(e.judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') = m.model_name
    AND e.evaluated_at >= m.effective_start
    AND e.evaluated_at < m.effective_end
  LEFT JOIN `agent_eval.v_default_model_pricing` AS d
    ON REGEXP_REPLACE(REGEXP_REPLACE(e.judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') = d.model_name
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.eval_id
    ORDER BY m.effective_start DESC, m.effective_end ASC
  ) = 1
),
judge_spend AS (
  SELECT
    apphub_application_id,
    rubric_id,
    COUNT(*) AS total_judge_invocations,
    SUM(
      ((judge_input_tokens / 1e6) * input_usd_per_1m)
      + (((judge_output_tokens + judge_thinking_tokens) / 1e6) * output_usd_per_1m)
    ) AS eval_table_judge_usd
  FROM priced_judge_evals
  GROUP BY apphub_application_id, rubric_id
),
app_judge_spend AS (
  SELECT
    apphub_application_id,
    SUM(eval_table_judge_usd) AS app_eval_table_judge_usd
  FROM judge_spend
  GROUP BY apphub_application_id
),
latest_rule0_evals AS (
  SELECT
    apphub_application_id,
    rubric_id,
    trace_id,
    CASE
      WHEN sampling_stratum = 'ERROR_OR_SLA_TAIL' THEN 'ERROR_OR_SLA_TAIL'
      ELSE 'HEALTHY_POPULATION'
    END AS stratum_bucket,
    binary_pass
  FROM deduped_eval_results
  WHERE rule0_pass = TRUE
    AND binary_pass IS NOT NULL
    AND (ordinal_score IS NULL OR ordinal_score BETWEEN 1 AND 4)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY apphub_application_id, trace_id, rubric_id
    ORDER BY evaluated_at DESC
  ) = 1
),
stratum_judge AS (
  SELECT
    apphub_application_id,
    rubric_id,
    stratum_bucket,
    COUNT(*) AS stratum_judged_count,
    AVG(IF(binary_pass, 1.0, 0.0)) AS stratum_pass_rate
  FROM latest_rule0_evals
  GROUP BY apphub_application_id, rubric_id, stratum_bucket
),
app_rubrics AS (
  SELECT
    apphub_application_id,
    rubric_id,
    SUM(stratum_judged_count) AS total_judged_samples
  FROM stratum_judge
  GROUP BY apphub_application_id, rubric_id
),
stratum_coverage AS (
  SELECT
    ar.apphub_application_id,
    ar.rubric_id,
    ar.total_judged_samples,
    sp.stratum_bucket,
    sp.stratum_pop_count,
    COALESCE(sj.stratum_judged_count, 0) AS stratum_judged_count,
    sj.stratum_pass_rate,
    SAFE_DIVIDE(
      CAST(sp.stratum_pop_count AS FLOAT64),
      NULLIF(
        SUM(CAST(sp.stratum_pop_count AS FLOAT64)) OVER (
          PARTITION BY ar.apphub_application_id, ar.rubric_id
        ),
        0.0
      )
    ) AS stratum_weight
  FROM app_rubrics AS ar
  LEFT JOIN stratum_population AS sp
    ON ar.apphub_application_id = sp.apphub_application_id
  LEFT JOIN stratum_judge AS sj
    ON ar.apphub_application_id = sj.apphub_application_id
    AND ar.rubric_id = sj.rubric_id
    AND sp.stratum_bucket = sj.stratum_bucket
),
unmatched_judge_strata AS (
  SELECT
    sj.apphub_application_id,
    sj.rubric_id,
    COUNT(*) AS unmatched_stratum_count
  FROM stratum_judge AS sj
  LEFT JOIN stratum_population AS sp
    ON sj.apphub_application_id = sp.apphub_application_id
    AND sj.stratum_bucket = sp.stratum_bucket
  WHERE sp.stratum_pop_count IS NULL
  GROUP BY sj.apphub_application_id, sj.rubric_id
),
stratum_agg AS (
  SELECT
    sc.apphub_application_id,
    sc.rubric_id,
    MAX(sc.total_judged_samples) AS judged_samples,
    (
      COUNTIF(sc.stratum_pop_count IS NULL OR sc.stratum_judged_count = 0) > 0
      OR COALESCE(MAX(ujs.unmatched_stratum_count), 0) > 0
    ) AS has_incomplete_strata,
    CASE
      WHEN COUNTIF(sc.stratum_pop_count IS NULL OR sc.stratum_judged_count = 0) > 0
        OR COALESCE(MAX(ujs.unmatched_stratum_count), 0) > 0
        THEN NULL
      ELSE SUM(sc.stratum_weight * sc.stratum_pass_rate)
    END AS raw_judge_pass_rate,
    CASE
      WHEN COUNTIF(sc.stratum_pop_count IS NULL OR sc.stratum_judged_count = 0) > 0
        OR COALESCE(MAX(ujs.unmatched_stratum_count), 0) > 0
        THEN NULL
      ELSE SUM(
        POW(sc.stratum_weight, 2)
        * SAFE_DIVIDE(
          sc.stratum_pass_rate * (1.0 - sc.stratum_pass_rate),
          NULLIF(sc.stratum_judged_count, 0)
        )
      )
    END AS var_p_obs,
    CASE
      WHEN COUNTIF(sc.stratum_pop_count IS NULL OR sc.stratum_judged_count = 0) > 0
        OR COALESCE(MAX(ujs.unmatched_stratum_count), 0) > 0
        THEN NULL
      ELSE SAFE_DIVIDE(
        1.0,
        NULLIF(
          SUM(SAFE_DIVIDE(POW(sc.stratum_weight, 2), NULLIF(sc.stratum_judged_count, 0))),
          0.0
        )
      )
    END AS kish_eff_n
  FROM stratum_coverage AS sc
  LEFT JOIN unmatched_judge_strata AS ujs
    ON sc.apphub_application_id = ujs.apphub_application_id
    AND sc.rubric_id = ujs.rubric_id
  GROUP BY sc.apphub_application_id, sc.rubric_id
),
judge_metrics AS (
  SELECT
    js.apphub_application_id,
    js.rubric_id,
    COALESCE(sa.judged_samples, 0) AS judged_samples,
    js.total_judge_invocations,
    COALESCE(sa.has_incomplete_strata, FALSE) AS has_incomplete_strata,
    sa.raw_judge_pass_rate,
    sa.var_p_obs,
    sa.kish_eff_n,
    js.eval_table_judge_usd
  FROM judge_spend AS js
  LEFT JOIN stratum_agg AS sa
    ON js.apphub_application_id = sa.apphub_application_id
    AND js.rubric_id = sa.rubric_id
),
sme_raw AS (
  SELECT
    apphub_application_id,
    rubric_id,
    COUNTIF(human_pass AND judge_pass) AS tp,
    COUNTIF(human_pass AND NOT judge_pass) AS fn,
    COUNTIF(NOT human_pass AND NOT judge_pass) AS tn,
    COUNTIF(NOT human_pass AND judge_pass) AS fp,
    SAFE_DIVIDE(COUNTIF(human_pass AND judge_pass), NULLIF(COUNTIF(human_pass), 0)) AS tpr,
    SAFE_DIVIDE(COUNTIF(NOT human_pass AND NOT judge_pass), NULLIF(COUNTIF(NOT human_pass), 0)) AS tnr,
    COUNTIF(human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4) AS ordinal_pairs,
    AVG(
      CASE
        WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4
          THEN 1.0 - POW(CAST(human_ordinal_score - judge_ordinal_score AS FLOAT64) / 3.0, 2)
      END
    ) AS gwet_p_a,
    (
      AVG(CASE WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4 THEN IF(human_ordinal_score = 1, 1.0, 0.0) END)
      + AVG(CASE WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4 THEN IF(judge_ordinal_score = 1, 1.0, 0.0) END)
    ) / 2.0 AS pi_1,
    (
      AVG(CASE WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4 THEN IF(human_ordinal_score = 2, 1.0, 0.0) END)
      + AVG(CASE WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4 THEN IF(judge_ordinal_score = 2, 1.0, 0.0) END)
    ) / 2.0 AS pi_2,
    (
      AVG(CASE WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4 THEN IF(human_ordinal_score = 3, 1.0, 0.0) END)
      + AVG(CASE WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4 THEN IF(judge_ordinal_score = 3, 1.0, 0.0) END)
    ) / 2.0 AS pi_3,
    (
      AVG(CASE WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4 THEN IF(human_ordinal_score = 4, 1.0, 0.0) END)
      + AVG(CASE WHEN human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4 THEN IF(judge_ordinal_score = 4, 1.0, 0.0) END)
    ) / 2.0 AS pi_4
  FROM `agent_eval.sme_labels`
  WHERE rule0_pass = TRUE
    AND human_pass IS NOT NULL
    AND judge_pass IS NOT NULL
  GROUP BY apphub_application_id, rubric_id
),
sme_calibration AS (
  SELECT
    sr.apphub_application_id,
    sr.rubric_id,
    sr.tp,
    sr.fn,
    sr.tn,
    sr.fp,
    sr.tpr,
    sr.tnr,
    CASE
      WHEN sr.ordinal_pairs = 0 THEN NULL
      WHEN ABS(
        1.0 - (26.0 / 27.0) * (
          sr.pi_1 * (1.0 - sr.pi_1)
          + sr.pi_2 * (1.0 - sr.pi_2)
          + sr.pi_3 * (1.0 - sr.pi_3)
          + sr.pi_4 * (1.0 - sr.pi_4)
        )
      ) < 1e-12 THEN 1.0
      ELSE SAFE_DIVIDE(
        sr.gwet_p_a - (26.0 / 27.0) * (
          sr.pi_1 * (1.0 - sr.pi_1)
          + sr.pi_2 * (1.0 - sr.pi_2)
          + sr.pi_3 * (1.0 - sr.pi_3)
          + sr.pi_4 * (1.0 - sr.pi_4)
        ),
        1.0 - (26.0 / 27.0) * (
          sr.pi_1 * (1.0 - sr.pi_1)
          + sr.pi_2 * (1.0 - sr.pi_2)
          + sr.pi_3 * (1.0 - sr.pi_3)
          + sr.pi_4 * (1.0 - sr.pi_4)
        )
      )
    END AS gwet_ac2
  FROM sme_raw AS sr
),
calibrated AS (
  SELECT
    tm.apphub_application_id,
    COALESCE(jm.rubric_id, 'groundedness_4pt_v1') AS rubric_id,
    COALESCE(r.risk_tier, 'RL-3b') AS risk_tier,
    CASE COALESCE(r.risk_tier, 'RL-3b')
      WHEN 'RL-1' THEN 0.85
      WHEN 'RL-2' THEN 0.85
      WHEN 'RL-3a' THEN 0.80
      WHEN 'RL-3b' THEN 0.80
      WHEN 'RL-4' THEN 0.75
      WHEN 'RL-5' THEN 0.75
      ELSE 0.80
    END AS required_ac2_gate,
    tm.total_traces,
    tm.production_traces,
    tm.production_rule0_passing_traces,
    tm.rule0_pass_rate,
    tm.tool_success_rate,
    tm.p95_latency_ms,
    tm.avg_cache_hit_ratio,
    tm.total_weighted_burndown_tokens,
    tm.unpriced_spans,
    COALESCE(jm.judged_samples, 0) AS judged_samples,
    jm.raw_judge_pass_rate,
    sc.tp,
    sc.fn,
    sc.tn,
    sc.fp,
    sc.tpr,
    sc.tnr,
    sc.gwet_ac2,
    (sc.tpr + sc.tnr - 1.0) AS youden_j,
    -- Minimum-n guard: >= 10 judged traces, >= 5 positive & >= 5 negative SME reference labels,
    -- complete stratum coverage, and positive Youden's J (both raw and Wilson-adjusted).
    CASE
      WHEN COALESCE(jm.judged_samples, 0) = 0 THEN 'UNJUDGED'
      WHEN COALESCE(jm.has_incomplete_strata, FALSE)
        OR jm.raw_judge_pass_rate IS NULL
        OR COALESCE(jm.judged_samples, 0) < 10
        OR sc.tpr IS NULL
        OR sc.tnr IS NULL
        OR (sc.tp + sc.fn) < 5
        OR (sc.tn + sc.fp) < 5
        OR (sc.tpr + sc.tnr - 1.0) <= 1e-6
        OR (
          SAFE_DIVIDE(CAST(sc.tp + 1 AS FLOAT64), CAST(sc.tp + sc.fn + 2 AS FLOAT64))
          + SAFE_DIVIDE(CAST(sc.tn + 1 AS FLOAT64), CAST(sc.tn + sc.fp + 2 AS FLOAT64))
          - 1.0
        ) <= 1e-6
        THEN 'UNCALIBRATED'
      ELSE 'CALIBRATED'
    END AS calibration_status,
    CASE
      WHEN COALESCE(jm.judged_samples, 0) < 10
        OR COALESCE(jm.has_incomplete_strata, FALSE)
        OR jm.raw_judge_pass_rate IS NULL
        OR sc.tpr IS NULL
        OR sc.tnr IS NULL
        OR (sc.tp + sc.fn) < 5
        OR (sc.tn + sc.fp) < 5
        OR (sc.tpr + sc.tnr - 1.0) <= 1e-6
        OR (
          SAFE_DIVIDE(CAST(sc.tp + 1 AS FLOAT64), CAST(sc.tp + sc.fn + 2 AS FLOAT64))
          + SAFE_DIVIDE(CAST(sc.tn + 1 AS FLOAT64), CAST(sc.tn + sc.fp + 2 AS FLOAT64))
          - 1.0
        ) <= 1e-6
        THEN NULL
      ELSE LEAST(
        1.0,
        GREATEST(
          0.0,
          SAFE_DIVIDE(jm.raw_judge_pass_rate + sc.tnr - 1.0, sc.tpr + sc.tnr - 1.0)
        )
      )
    END AS theta_rg,
    CASE
      WHEN jm.var_p_obs IS NOT NULL
        AND jm.var_p_obs > 0.0
        AND jm.raw_judge_pass_rate > 0.0
        AND jm.raw_judge_pass_rate < 1.0
        THEN GREATEST(
          1.0,
          SAFE_DIVIDE(jm.raw_judge_pass_rate * (1.0 - jm.raw_judge_pass_rate), jm.var_p_obs)
        )
      WHEN jm.kish_eff_n IS NOT NULL THEN GREATEST(1.0, jm.kish_eff_n)
      ELSE CAST(COALESCE(jm.judged_samples, 0) AS FLOAT64)
    END AS eff_n,
    CAST(sc.tp + sc.fn + 2 AS FLOAT64) AS n_se_adj,
    CAST(sc.tn + sc.fp + 2 AS FLOAT64) AS n_sp_adj,
    SAFE_DIVIDE(CAST(sc.tp + 1 AS FLOAT64), CAST(sc.tp + sc.fn + 2 AS FLOAT64)) AS se_adj,
    SAFE_DIVIDE(CAST(sc.tn + 1 AS FLOAT64), CAST(sc.tn + sc.fp + 2 AS FLOAT64)) AS sp_adj,
    tm.production_inference_usd,
    tm.conflict_escalation_usd,
    tm.agent_gym_eval_usd,
    IF(
      tm.span_autorater_judge_usd IS NULL,
      CAST(NULL AS FLOAT64),
      GREATEST(tm.span_autorater_judge_usd, COALESCE(ajs.app_eval_table_judge_usd, 0.0))
    ) AS autorater_judge_usd,
    tm.optimize_usd,
    COALESCE(qc.bq_query_cost_usd, 0.0) AS bq_query_cost_usd,
    tm.unclassified_usd
  FROM trace_metrics AS tm
  LEFT JOIN `agent_eval.app_registry` AS r
    ON tm.apphub_application_id = r.apphub_application_id
  LEFT JOIN `agent_eval.v_bq_query_cost_attribution` AS qc
    ON tm.apphub_application_id = qc.apphub_application_id
  LEFT JOIN app_judge_spend AS ajs
    ON tm.apphub_application_id = ajs.apphub_application_id
  LEFT JOIN judge_metrics AS jm
    ON tm.apphub_application_id = jm.apphub_application_id
  LEFT JOIN sme_calibration AS sc
    ON tm.apphub_application_id = sc.apphub_application_id
    AND jm.rubric_id = sc.rubric_id
),
-- Lang & Reiczigel (2014) Eq. (6), (7), (16), (17) boundary-safe 95% CI parameters
lr_ci_params AS (
  SELECT
    c.*,
    (c.eff_n + 3.841458820694124) AS n_p_adj,
    SAFE_DIVIDE(
      c.raw_judge_pass_rate * c.eff_n + 1.920729410347062,
      c.eff_n + 3.841458820694124
    ) AS p_adj,
    (c.se_adj + c.sp_adj - 1.0) AS j_adj
  FROM calibrated AS c
),
lr_ci AS (
  SELECT
    p.*,
    CASE
      WHEN p.theta_rg IS NULL THEN NULL
      ELSE LEAST(
        1.0,
        GREATEST(0.0, SAFE_DIVIDE(p.p_adj + p.sp_adj - 1.0, p.j_adj))
      )
    END AS theta_adj
  FROM lr_ci_params AS p
),
lr_ci_final AS (
  SELECT
    l.*,
    CASE
      WHEN l.theta_adj IS NULL THEN NULL
      ELSE 2.0 * 3.841458820694124 * (
        l.theta_adj * SAFE_DIVIDE(l.se_adj * (1.0 - l.se_adj), l.n_se_adj)
        - (1.0 - l.theta_adj) * SAFE_DIVIDE(l.sp_adj * (1.0 - l.sp_adj), l.n_sp_adj)
      )
    END AS dp_shift,
    CASE
      WHEN l.theta_adj IS NULL THEN NULL
      ELSE SAFE_DIVIDE(
        SQRT(
          SAFE_DIVIDE(l.p_adj * (1.0 - l.p_adj), l.n_p_adj)
          + POW(l.theta_adj, 2) * SAFE_DIVIDE(l.se_adj * (1.0 - l.se_adj), l.n_se_adj)
          + POW(1.0 - l.theta_adj, 2) * SAFE_DIVIDE(l.sp_adj * (1.0 - l.sp_adj), l.n_sp_adj)
        ),
        l.j_adj
      )
    END AS se_theta_adj
  FROM lr_ci AS l
)
SELECT
  c.apphub_application_id,
  c.rubric_id,
  c.risk_tier,
  c.required_ac2_gate,
  c.total_traces,
  c.production_traces,
  c.rule0_pass_rate,
  c.tool_success_rate,
  c.p95_latency_ms,
  c.avg_cache_hit_ratio,
  c.total_weighted_burndown_tokens,
  c.judged_samples,
  c.calibration_status,
  c.gwet_ac2,
  IF(c.gwet_ac2 IS NULL, NULL, c.gwet_ac2 >= c.required_ac2_gate) AS passes_irr_gate,
  c.raw_judge_pass_rate,
  c.theta_rg AS calibrated_pass_rate,
  CASE
    WHEN c.theta_adj IS NULL THEN NULL
    ELSE LEAST(
      1.0,
      GREATEST(0.0, c.theta_adj + c.dp_shift - 1.959963984540054 * c.se_theta_adj)
    )
  END AS calibrated_ci_95_lower,
  CASE
    WHEN c.theta_adj IS NULL THEN NULL
    ELSE LEAST(
      1.0,
      GREATEST(0.0, c.theta_adj + c.dp_shift + 1.959963984540054 * c.se_theta_adj)
    )
  END AS calibrated_ci_95_upper,
  c.unpriced_spans,
  c.production_inference_usd,
  c.conflict_escalation_usd,
  c.agent_gym_eval_usd,
  c.autorater_judge_usd,
  c.optimize_usd,
  c.bq_query_cost_usd,
  c.unclassified_usd,
  IF(
    c.unpriced_spans > 0,
    CAST(NULL AS FLOAT64),
    c.production_inference_usd + c.conflict_escalation_usd + c.agent_gym_eval_usd + c.autorater_judge_usd + c.optimize_usd + c.bq_query_cost_usd + c.unclassified_usd
  ) AS total_spend_usd,
  CASE
    WHEN c.unpriced_spans > 0
      OR c.theta_rg IS NULL
      OR c.theta_rg <= 0.0
      OR c.production_rule0_passing_traces = 0 THEN NULL
    ELSE SAFE_DIVIDE(
      c.production_inference_usd + c.conflict_escalation_usd + c.agent_gym_eval_usd + c.autorater_judge_usd + c.optimize_usd + c.bq_query_cost_usd + c.unclassified_usd,
      c.production_rule0_passing_traces * c.theta_rg
    )
  END AS cost_per_verified_outcome_usd
FROM lr_ci_final AS c;
