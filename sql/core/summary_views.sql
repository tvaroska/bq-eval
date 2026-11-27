-- Gold Step-1 Cost per Outcome, Enterprise CPVO, and Fail-Closed Stratified Summary (`ADR-033`, `ADR-048`, `ADR-049`).

CREATE OR REPLACE VIEW `agent_eval.v_step1_cost_per_outcome` AS
SELECT
  apphub_application_id, business_unit, cost_center,
  COUNT(*) AS total_traces,
  COUNTIF(rule0_pass) AS step1_passing_traces,
  AVG(IF(rule0_pass, 1.0, 0.0)) AS step1_pass_rate,
  AVG(IF(tool_error_count = 0, 1.0, 0.0)) AS tool_success_rate,
  APPROX_QUANTILES(trace_latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
  COALESCE(AVG(cache_hit_ratio), 0.0) AS avg_cache_hit_ratio,
  SUM(unpriced_spans) AS unpriced_spans,
  IF(SUM(unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(trace_span_cost_usd)) AS total_cost_usd,
  IF(SUM(unpriced_spans) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(trace_span_cost_usd), COUNT(*))) AS cost_per_trace_usd,
  IF(SUM(unpriced_spans) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(trace_span_cost_usd), NULLIF(COUNTIF(rule0_pass), 0))) AS step1_cost_per_outcome_usd
FROM `agent_eval.v_trace_sql_evaluation`
GROUP BY apphub_application_id, business_unit, cost_center;

CREATE OR REPLACE VIEW `agent_eval.v_enterprise_cost_per_verified_outcome` AS
WITH der AS (
  SELECT * FROM `agent_eval.eval_results`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY COALESCE(eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', evaluated_at)), trace_id, rubric_id ORDER BY IF(COALESCE(judge_status, 'SCORED') = 'SCORED', 0, 1) ASC, evaluated_at DESC) = 1
),
ewo AS (
  SELECT
    e.eval_id, e.trace_id, e.rubric_id, COALESCE(e.outcome_id, t.outcome_id) AS outcome_id,
    e.judge_status, e.binary_pass, e.ordinal_score, e.evaluated_at,
    e.judge_input_tokens, e.judge_output_tokens, e.judge_thinking_tokens,
    COALESCE(m.input_usd_per_1m, d.input_usd_per_1m) AS in_r, COALESCE(m.output_usd_per_1m, d.output_usd_per_1m) AS out_r,
    ROW_NUMBER() OVER (PARTITION BY e.trace_id, e.rubric_id, IF(COALESCE(e.judge_status, 'SCORED') = 'SCORED' AND e.binary_pass IS NOT NULL AND (e.ordinal_score IS NULL OR e.ordinal_score BETWEEN 1 AND 4), 0, 1) ORDER BY e.evaluated_at DESC) AS lrn
  FROM (SELECT *, REGEXP_REPLACE(REGEXP_REPLACE(judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') AS jm_norm FROM der) AS e
  LEFT JOIN `${PROJECT_ID}.${SILVER_OPEN_DATASET}.traces_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) AS t ON e.trace_id = t.trace_id
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m ON e.jm_norm = m.model_name AND e.evaluated_at >= m.effective_start AND e.evaluated_at < m.effective_end
  LEFT JOIN `agent_eval.v_default_model_pricing` AS d ON e.jm_norm = d.model_name
  WHERE COALESCE(e.outcome_id, t.outcome_id) IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.eval_id ORDER BY m.effective_start DESC, m.effective_end ASC) = 1
),
s2j AS (
  SELECT outcome_id,
    LOGICAL_AND(CASE WHEN COALESCE(judge_status, 'SCORED') = 'SCORED' AND binary_pass IS NOT NULL AND (ordinal_score IS NULL OR ordinal_score BETWEEN 1 AND 4) AND lrn = 1 THEN binary_pass END) AS s2_ok,
    SUM(((judge_input_tokens / 1e6) * in_r) + (((judge_output_tokens + judge_thinking_tokens) / 1e6) * out_r)) AS ej_usd
  FROM ewo GROUP BY outcome_id
),
ols AS (
  SELECT primary_outcome_id, linked_outcome_id, SAFE_DIVIDE(1.0, COUNT(*) OVER (PARTITION BY primary_outcome_id)) AS lsh
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcome_links`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
),
wft AS (
  SELECT w0.*,
    ROW_NUMBER() OVER (PARTITION BY w0.trace_id, COALESCE(w0.service_name, 'agent-service') ORDER BY CASE WHEN w0.okind = 'SERVER' THEN 0 WHEN w0.okind = 'INTERNAL' THEN 1 ELSE 2 END, IF(w0.parent_span_id IS NULL, 0, 1), w0.start_time ASC, w0.span_id ASC) AS srn,
    ROW_NUMBER() OVER (PARTITION BY w0.outcome_id ORDER BY IF(NOT w0.is_cp AND w0.okind IN ('SERVER', 'INTERNAL'), 0, 1), w0.start_time ASC, w0.span_id ASC) AS orn,
    MAX(CASE WHEN NOT w0.is_cp AND w0.okind IN ('SERVER', 'INTERNAL') AND w0.p_st >= 0.0 THEN w0.p_st END) OVER (PARTITION BY w0.outcome_id) AS p_est
  FROM (
    SELECT w.*, wt_p.storage_usd_per_outcome AS p_st, COALESCE(wt_p.is_external_counterparty, FALSE) AS is_cp,
      CASE UPPER(TRIM(COALESCE(w.otel_span_kind, 'INTERNAL')))
        WHEN '1' THEN 'INTERNAL' WHEN 'INTERNAL' THEN 'INTERNAL' WHEN 'SPAN_KIND_INTERNAL' THEN 'INTERNAL'
        WHEN '2' THEN 'SERVER' WHEN 'SERVER' THEN 'SERVER' WHEN 'SPAN_KIND_SERVER' THEN 'SERVER' ELSE 'OTHER'
      END AS okind
    FROM `agent_eval.v_trace_waterfall` AS w
    LEFT JOIN `agent_eval.workload_thresholds` AS wt_p ON COALESCE(w.outcome_owner, w.apphub_application_id) = wt_p.apphub_application_id
    WHERE w.outcome_id IS NOT NULL
  ) AS w0
),
ew AS (
  SELECT COALESCE(ols.linked_outcome_id, w.outcome_id) AS eff_oid, COALESCE(ols.lsh, 1.0) AS csh, w.*,
    IF(NOT w.is_cp, (IF(w.okind IN ('SERVER', 'INTERNAL') AND w.srn = 1, (w.latency_ms / 1000.0) * ((2.0 * 0.00002400) + (2.0 * 0.00000250)), 0.0) + (GREATEST(0.0, COALESCE(w.sandbox_cpu_sec, 0.0)) * 0.00002400) + (GREATEST(0.0, COALESCE(w.sandbox_mem_gb_sec, 0.0)) * 0.00000250)) * COALESCE(ols.lsh, 1.0), 0.0) AS c_u,
    IF(NOT w.is_cp AND w.okind IN ('SERVER', 'INTERNAL') AND w.orn = 1, COALESCE(w.p_est, 0.000020) * COALESCE(ols.lsh, 1.0), 0.0) AS s_u,
    IF(NOT w.is_cp AND w.okind IN ('SERVER', 'INTERNAL') AND w.orn = 1, COALESCE(ols.lsh, 1.0), 0.0) AS s_sh,
    (NOT w.is_cp AND w.okind IN ('SERVER', 'INTERNAL') AND w.orn = 1 AND w.p_est IS NOT NULL) AS has_es
  FROM wft AS w LEFT JOIN ols ON w.outcome_id = ols.primary_outcome_id
),
os0 AS (
  SELECT
    w.eff_oid AS outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(w.source_mode, o.source_mode, 'UNSPECIFIED') AS source_mode,
    w.agent_name,
    COALESCE(o.outcome_status, 'open') AS outcome_status,
    IF(COALESCE(o.outcome_status, 'open') != 'succeeded', FALSE, COALESCE(o.is_verified, FALSE) AND COALESCE(j.s2_ok, TRUE)) AS is_verified,
    COUNT(DISTINCT w.trace_id) AS trace_count, COUNT(*) AS span_count,
    SUM(CAST(ROUND(w.input_tokens * w.csh) AS INT64)) AS input_tokens,
    SUM(CAST(ROUND(w.cached_input_tokens * w.csh) AS INT64)) AS cached_input_tokens,
    SUM(CAST(ROUND(w.output_tokens * w.csh) AS INT64)) AS output_tokens,
    SUM(CAST(ROUND(w.thinking_tokens * w.csh) AS INT64)) AS thinking_tokens,
    COUNTIF(w.span_cost_usd IS NULL) AS unpriced_spans,
    IF(COUNTIF(w.finops_traffic_type != 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN w.span_cost_usd * w.csh ELSE 0.0 END)) AS m_usd,
    IF(COUNTIF(w.finops_traffic_type = 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd * w.csh ELSE 0.0 END)) AS sj_usd,
    COALESCE(MAX(j.ej_usd), 0.0) AS oj_usd,
    MAX(wt.storage_usd_per_outcome) AS app_st,
    SUM(w.c_u) AS sc_u, SUM(w.s_u) AS ss_u, SUM(w.s_sh) AS ss_sh, LOGICAL_OR(w.has_es) AS sh_es
  FROM ew AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o ON w.eff_oid = o.outcome_id
  LEFT JOIN s2j AS j ON w.eff_oid = j.outcome_id
  LEFT JOIN `agent_eval.workload_thresholds` AS wt ON COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') = wt.apphub_application_id
  WHERE NOT COALESCE(wt.is_external_counterparty, FALSE)
  GROUP BY 1, 2, 3, 4, 5, 6
),
wos AS (
  SELECT os.*,
    SUM(os.span_count) OVER (PARTITION BY os.outcome_id) AS out_sp,
    SUM(os.span_count) OVER (PARTITION BY os.apphub_application_id) AS app_sp,
    SUM(COALESCE(os.sj_usd, 0.0)) OVER (PARTITION BY os.outcome_id) AS out_sj,
    SUM(os.sc_u) OVER (PARTITION BY os.outcome_id) AS out_cu,
    SUM(os.ss_u) OVER (PARTITION BY os.outcome_id) AS out_su,
    SUM(os.ss_sh) OVER (PARTITION BY os.outcome_id) AS out_ssh,
    MAX(IF(os.sh_es, 1, 0)) OVER (PARTITION BY os.outcome_id) = 1 AS out_es
  FROM os0 AS os
),
aos AS (
  SELECT os.*,
    os.sj_usd + GREATEST(0.0, os.oj_usd - os.out_sj) * COALESCE(SAFE_DIVIDE(os.span_count, os.out_sp), 0.0) AS j_usd,
    (os.out_cu + IF(os.out_es, os.out_su, COALESCE(os.app_st * NULLIF(os.out_ssh, 0.0), NULLIF(os.out_su, 0.0), 0.0))) * COALESCE(SAFE_DIVIDE(os.span_count, os.out_sp), 0.0) AS inf_usd,
    COALESCE(qc.bq_query_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.span_count, os.app_sp), 0.0) AS bq_usd
  FROM wos AS os
  LEFT JOIN `agent_eval.v_bq_query_cost_attribution` AS qc ON os.apphub_application_id = qc.apphub_application_id
)
SELECT
  os.apphub_application_id, os.source_mode, os.agent_name,
  COUNT(DISTINCT os.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)) AS verified_outcomes,
  COUNT(DISTINCT IF(NOT os.is_verified, os.outcome_id, NULL)) AS unverified_or_failed_outcomes,
  SUM(os.trace_count) AS total_traces, SUM(os.input_tokens) AS total_input_tokens, SUM(os.output_tokens) AS total_output_tokens, SUM(os.thinking_tokens) AS total_thinking_tokens,
  SUM(os.unpriced_spans) AS unpriced_spans,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.m_usd)) AS measured_model_cost_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.j_usd)) AS measured_judge_cost_usd,
  SUM(os.inf_usd) AS estimated_infra_cost_usd,
  SUM(os.bq_usd) AS bq_query_cost_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.m_usd + os.j_usd)) AS total_ai_cost_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.m_usd + os.j_usd + os.inf_usd + os.bq_usd)) AS total_cost_with_infra_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(os.m_usd + os.j_usd), NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0))) AS cost_per_verified_outcome_ai_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(os.m_usd + os.j_usd + os.inf_usd + os.bq_usd), NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0))) AS cost_per_verified_outcome_usd
FROM aos AS os
GROUP BY os.apphub_application_id, os.source_mode, os.agent_name;

CREATE OR REPLACE VIEW `agent_eval.v_agent_eval_and_finops_summary` AS
WITH der AS (
  SELECT *, REGEXP_REPLACE(REGEXP_REPLACE(judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') AS jm_norm
  FROM `agent_eval.eval_results`
  WHERE COALESCE(judge_status, 'SCORED') = 'SCORED' AND binary_pass IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY COALESCE(eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', evaluated_at)), trace_id, rubric_id ORDER BY evaluated_at DESC) = 1
),
pje AS (
  SELECT e.eval_id, e.apphub_application_id AS app_id, e.rubric_id,
    ((e.judge_input_tokens / 1e6) * COALESCE(m.input_usd_per_1m, d.input_usd_per_1m)) + (((e.judge_output_tokens + e.judge_thinking_tokens) / 1e6) * COALESCE(m.output_usd_per_1m, d.output_usd_per_1m)) AS j_c
  FROM der AS e
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m ON e.jm_norm = m.model_name AND e.evaluated_at >= m.effective_start AND e.evaluated_at < m.effective_end
  LEFT JOIN `agent_eval.v_default_model_pricing` AS d ON e.jm_norm = d.model_name
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.eval_id ORDER BY m.effective_start DESC, m.effective_end ASC) = 1
),
js AS (SELECT app_id, rubric_id, SUM(j_c) AS ej_usd FROM pje GROUP BY 1, 2),
ajs AS (SELECT app_id, SUM(ej_usd) AS app_ej_usd FROM js GROUP BY 1),
ler AS (
  SELECT apphub_application_id AS app_id, rubric_id, trace_id, binary_pass, judge_model_snapshot_id, judge_prompt_sha256, rubric_version
  FROM der WHERE rule0_pass = TRUE AND (ordinal_score IS NULL OR ordinal_score BETWEEN 1 AND 4)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY apphub_application_id, trace_id, rubric_id ORDER BY evaluated_at DESC) = 1
),
ert AS (
  SELECT app_id, rubric_id, COUNT(*) AS raw_cnt,
    MAX(judge_model_snapshot_id) AS j_snap, MAX(judge_prompt_sha256) AS j_psha, MAX(rubric_version) AS j_rver
  FROM ler GROUP BY 1, 2
),
sf AS (SELECT trace_id, LOGICAL_OR(missing_session_context) AS miss_ctx FROM `agent_eval.v_step2_session_reconstructed_payloads` GROUP BY 1),
tm_jm AS (
  SELECT
    t.apphub_application_id AS app_id, js.rubric_id AS jm_rubric_id, COALESCE(js.rubric_id, 'groundedness_4pt_v1') AS rubric_id,
    MIN(t.effective_risk_tier) AS eff_rt,
    COUNT(*) AS total_traces, COUNTIF(t.is_p) AS production_traces, COUNTIF(t.pr0) AS prod_r0_traces,
    COUNTIF(t.pr0 AND COALESCE(sf.miss_ctx, FALSE)) AS miss_sess,
    COUNTIF(t.pr0 AND t.is_tail) AS tp_c, COUNTIF(t.pr0 AND NOT t.is_tail) AS hp_c,
    COUNTIF(t.pr0 AND ea.trace_id IS NOT NULL AND t.is_tail) AS tj_c,
    AVG(CASE WHEN t.pr0 AND ea.trace_id IS NOT NULL AND t.is_tail THEN IF(ea.binary_pass, 1.0, 0.0) END) AS t_pr,
    COUNTIF(t.pr0 AND ea.trace_id IS NOT NULL AND NOT t.is_tail) AS hj_c,
    AVG(CASE WHEN t.pr0 AND ea.trace_id IS NOT NULL AND NOT t.is_tail THEN IF(ea.binary_pass, 1.0, 0.0) END) AS h_pr,
    COUNT(ea.trace_id) AS m_cnt,
    AVG(IF(t.rule0_pass, 1.0, 0.0)) AS rule0_pass_rate, AVG(IF(t.tool_error_count = 0, 1.0, 0.0)) AS tool_success_rate,
    APPROX_QUANTILES(t.trace_latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
    COALESCE(AVG(t.cache_hit_ratio), 0.0) AS avg_cache_hit_ratio,
    SUM(t.weighted_burndown_tokens) AS total_weighted_burndown_tokens, SUM(t.unpriced_spans) AS unpriced_spans,
    IF(COUNTIF(t.production_inference_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.production_inference_usd)) AS production_inference_usd,
    IF(COUNTIF(t.conflict_escalation_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.conflict_escalation_usd)) AS conflict_escalation_usd,
    IF(COUNTIF(t.agent_gym_eval_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.agent_gym_eval_usd)) AS agent_gym_eval_usd,
    IF(COUNTIF(t.span_autorater_judge_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.span_autorater_judge_usd)) AS span_autorater_judge_usd,
    IF(COUNTIF(t.optimize_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.optimize_usd)) AS optimize_usd,
    IF(COUNTIF(t.unclassified_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.unclassified_usd)) AS unclassified_usd
  FROM (
    SELECT *, primary_traffic_type IN ('production_inference', 'conflict_escalation') AS is_p,
      (primary_traffic_type IN ('production_inference', 'conflict_escalation') AND rule0_pass) AS pr0,
      (is_loop_or_error_tail OR is_healthy_slow_tail) AS is_tail
    FROM `agent_eval.v_trace_sql_evaluation`
  ) AS t
  LEFT JOIN sf ON t.trace_id = sf.trace_id
  LEFT JOIN js ON t.apphub_application_id = js.app_id
  LEFT JOIN ler AS ea ON js.rubric_id IS NOT NULL AND t.trace_id = ea.trace_id AND js.app_id = ea.app_id AND js.rubric_id = ea.rubric_id
  GROUP BY 1, 2
),
ssc AS (
  SELECT app_id, rubric_id, sb, SAFE_DIVIDE(tp, NULLIF(hp, 0)) AS s_tpr, SAFE_DIVIDE(tn, NULLIF(hn, 0)) AS s_tnr,
    (hp >= 5 AND hn >= 5 AND COALESCE(SAFE_DIVIDE(tp, NULLIF(hp, 0)) + SAFE_DIVIDE(tn, NULLIF(hn, 0)) - 1.0, 0.0) > 1e-6
      AND (SAFE_DIVIDE(CAST(tp + 1 AS FLOAT64), CAST(hp + 2 AS FLOAT64)) + SAFE_DIVIDE(CAST(tn + 1 AS FLOAT64), CAST(hn + 2 AS FLOAT64)) - 1.0) > 1e-6) AS ok
  FROM (
    SELECT apphub_application_id AS app_id, rubric_id, IF(sampling_stratum IN ('ERROR_OR_SLA_TAIL', 'LOOP_OR_ERROR_TAIL'), 'ERROR_OR_SLA_TAIL', 'HEALTHY_POPULATION') AS sb,
      COUNTIF(human_pass AND judge_pass) AS tp, COUNTIF(NOT human_pass AND NOT judge_pass) AS tn,
      COUNTIF(human_pass) AS hp, COUNTIF(NOT human_pass) AS hn
    FROM `agent_eval.sme_labels`
    WHERE rule0_pass = TRUE AND sampling_stratum IS NOT NULL AND human_pass IS NOT NULL AND judge_pass IS NOT NULL
    GROUP BY 1, 2, 3
  )
),
ssp AS (
  SELECT app_id, rubric_id, TRUE AS has_strat_sme,
    MAX(IF(sb = 'HEALTHY_POPULATION', s_tpr, NULL)) AS h_tpr, MAX(IF(sb = 'HEALTHY_POPULATION', s_tnr, NULL)) AS h_tnr, LOGICAL_OR(sb = 'HEALTHY_POPULATION' AND ok) AS h_ok,
    MAX(IF(sb = 'ERROR_OR_SLA_TAIL', s_tpr, NULL)) AS t_tpr, MAX(IF(sb = 'ERROR_OR_SLA_TAIL', s_tnr, NULL)) AS t_tnr, LOGICAL_OR(sb = 'ERROR_OR_SLA_TAIL' AND ok) AS t_ok
  FROM ssc GROUP BY 1, 2
),
jmr AS (
  SELECT m.*, (m.tj_c + m.hj_c) AS judged_samples,
    IF(m.jm_rubric_id IS NULL, FALSE,
      (m.tj_c + m.hj_c) = 0 OR (m.hp_c = 0 AND m.tp_c = 0)
      OR (m.hp_c > 0 AND (m.hj_c = 0 OR m.hj_c > m.hp_c)) OR (m.tp_c > 0 AND (m.tj_c = 0 OR m.tj_c > m.tp_c))
      OR (m.hp_c = 0 AND m.hj_c > 0) OR (m.tp_c = 0 AND m.tj_c > 0) OR COALESCE(ert.raw_cnt, 0) > m.m_cnt) AS inc_strata,
    SAFE_DIVIDE(CAST(m.hp_c AS FLOAT64), NULLIF(CAST(m.hp_c + m.tp_c AS FLOAT64), 0.0)) AS w_h,
    SAFE_DIVIDE(CAST(m.tp_c AS FLOAT64), NULLIF(CAST(m.hp_c + m.tp_c AS FLOAT64), 0.0)) AS w_t,
    COALESCE(ssp.has_strat_sme, FALSE) AS has_strat_sme, ssp.h_tpr, ssp.h_tnr, COALESCE(ssp.h_ok, FALSE) AS h_ok,
    ssp.t_tpr, ssp.t_tnr, COALESCE(ssp.t_ok, FALSE) AS t_ok, ert.j_snap, ert.j_psha, ert.j_rver
  FROM tm_jm AS m
  LEFT JOIN ert ON m.app_id = ert.app_id AND m.jm_rubric_id = ert.rubric_id
  LEFT JOIN ssp ON m.app_id = ssp.app_id AND m.jm_rubric_id = ssp.rubric_id
),
sme_ord AS (
  SELECT app_id, rubric_id, ord_n, bca, pa, (26.0 / 27.0) * (p1 * (1.0 - p1) + p2 * (1.0 - p2) + p3 * (1.0 - p3) + p4 * (1.0 - p4)) AS pe
  FROM (
    SELECT s.apphub_application_id AS app_id, s.rubric_id, COUNT(*) AS ord_n,
      AVG(IF((s.human_ordinal_score >= COALESCE(r.passing_score, 3)) = (s.judge_ordinal_score >= COALESCE(r.passing_score, 3)), 1.0, 0.0)) AS bca,
      AVG(1.0 - POW(CAST(s.human_ordinal_score - s.judge_ordinal_score AS FLOAT64) / 3.0, 2)) AS pa,
      AVG((IF(s.human_ordinal_score = 1, 1.0, 0.0) + IF(s.judge_ordinal_score = 1, 1.0, 0.0)) / 2.0) AS p1,
      AVG((IF(s.human_ordinal_score = 2, 1.0, 0.0) + IF(s.judge_ordinal_score = 2, 1.0, 0.0)) / 2.0) AS p2,
      AVG((IF(s.human_ordinal_score = 3, 1.0, 0.0) + IF(s.judge_ordinal_score = 3, 1.0, 0.0)) / 2.0) AS p3,
      AVG((IF(s.human_ordinal_score = 4, 1.0, 0.0) + IF(s.judge_ordinal_score = 4, 1.0, 0.0)) / 2.0) AS p4
    FROM `agent_eval.sme_labels` AS s LEFT JOIN `agent_eval.rubrics` AS r ON s.rubric_id = r.rubric_id
    WHERE s.rule0_pass = TRUE AND s.human_ordinal_score BETWEEN 1 AND 4 AND s.judge_ordinal_score BETWEEN 1 AND 4
    GROUP BY 1, 2
  )
),
sme_dual AS (
  SELECT app_id, rubric_id, dr_n, pa, (26.0 / 27.0) * (p1 * (1.0 - p1) + p2 * (1.0 - p2) + p3 * (1.0 - p3) + p4 * (1.0 - p4)) AS pe
  FROM (
    SELECT apphub_application_id AS app_id, rubric_id, COUNT(*) AS dr_n,
      AVG(1.0 - POW(CAST(rater_a_score - rater_b_score AS FLOAT64) / 3.0, 2)) AS pa,
      AVG((IF(rater_a_score = 1, 1.0, 0.0) + IF(rater_b_score = 1, 1.0, 0.0)) / 2.0) AS p1,
      AVG((IF(rater_a_score = 2, 1.0, 0.0) + IF(rater_b_score = 2, 1.0, 0.0)) / 2.0) AS p2,
      AVG((IF(rater_a_score = 3, 1.0, 0.0) + IF(rater_b_score = 3, 1.0, 0.0)) / 2.0) AS p3,
      AVG((IF(rater_a_score = 4, 1.0, 0.0) + IF(rater_b_score = 4, 1.0, 0.0)) / 2.0) AS p4
    FROM `agent_eval.sme_labels`
    WHERE rule0_pass = TRUE AND rater_a_score BETWEEN 1 AND 4 AND rater_b_score BETWEEN 1 AND 4
    GROUP BY 1, 2
  )
),
sme_cm AS (
  SELECT app_id, rubric_id, tp, fn, tn, fp, SAFE_DIVIDE(tp, NULLIF(tp + fn, 0)) AS tpr, SAFE_DIVIDE(tn, NULLIF(tn + fp, 0)) AS tnr,
    min_exp, sme_snap, sme_psha, sme_rver
  FROM (
    SELECT apphub_application_id AS app_id, rubric_id,
      COUNTIF(hp_jp AND human_pass AND judge_pass) AS tp, COUNTIF(hp_jp AND human_pass AND NOT judge_pass) AS fn,
      COUNTIF(hp_jp AND NOT human_pass AND NOT judge_pass) AS tn, COUNTIF(hp_jp AND NOT human_pass AND judge_pass) AS fp,
      MIN(validation_expiry_date) AS min_exp, MAX(judge_model_snapshot_id) AS sme_snap, MAX(judge_prompt_sha256) AS sme_psha, MAX(rubric_version) AS sme_rver
    FROM (SELECT *, (human_pass IS NOT NULL AND judge_pass IS NOT NULL) AS hp_jp FROM `agent_eval.sme_labels` WHERE rule0_pass = TRUE) GROUP BY 1, 2
  )
),
sme_cal AS (
  SELECT cm.*, COALESCE(o.ord_n, 0) AS ord_pairs, o.bca AS binary_cutpoint_agreement, COALESCE(d.dr_n, 0) AS dual_pairs,
    CASE WHEN COALESCE(o.ord_n, 0) = 0 THEN NULL WHEN ABS(1.0 - o.pe) < 1e-12 THEN 1.0 ELSE SAFE_DIVIDE(o.pa - o.pe, 1.0 - o.pe) END AS gwet_ac2,
    CASE WHEN COALESCE(d.dr_n, 0) = 0 THEN NULL WHEN ABS(1.0 - d.pe) < 1e-12 THEN 1.0 ELSE SAFE_DIVIDE(d.pa - d.pe, 1.0 - d.pe) END AS sme_vs_sme_ac2
  FROM sme_cm AS cm
  LEFT JOIN sme_ord AS o ON cm.app_id = o.app_id AND cm.rubric_id = o.rubric_id
  LEFT JOIN sme_dual AS d ON cm.app_id = d.app_id AND cm.rubric_id = d.rubric_id
),
active_apps AS (
  SELECT app_id, risk_tier, validation_expiry_date FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry`
  WHERE effective_to IS NULL QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY effective_from DESC) = 1
),
app_outcomes AS (
  SELECT app_id,
    COUNT(DISTINCT outcome_id) AS outcome_total_count,
    COUNT(DISTINCT IF(is_verified, outcome_id, NULL)) AS outcome_verified_count,
    IF(SUM(unp) > 0, CAST(NULL AS FLOAT64), SUM(ai_s)) AS ai_usd,
    IF(SUM(unp) > 0, CAST(NULL AS FLOAT64), SUM(ai_s + inf_s + bq_s)) AS tot_usd
  FROM (
    SELECT os.outcome_id, os.app_id, os.is_verified, os.unp,
      COALESCE(os.m_u, 0.0) + COALESCE(os.sj_u, 0.0) + GREATEST(0.0, os.oj_u - SUM(COALESCE(os.sj_u, 0.0)) OVER (PARTITION BY os.outcome_id)) * COALESCE(SAFE_DIVIDE(os.sp_c, SUM(os.sp_c) OVER (PARTITION BY os.outcome_id)), 0.0) AS ai_s,
      (SUM(os.sc_u) OVER (PARTITION BY os.outcome_id) + IF(MAX(IF(os.sh_es, 1, 0)) OVER (PARTITION BY os.outcome_id) = 1, SUM(os.ss_u) OVER (PARTITION BY os.outcome_id), COALESCE(os.ast_u * NULLIF(SUM(os.ss_sh) OVER (PARTITION BY os.outcome_id), 0.0), NULLIF(SUM(os.ss_u) OVER (PARTITION BY os.outcome_id), 0.0), 0.0))) * COALESCE(SAFE_DIVIDE(os.sp_c, SUM(os.sp_c) OVER (PARTITION BY os.outcome_id)), 0.0) AS inf_s,
      COALESCE(qc.bq_query_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.sp_c, SUM(os.sp_c) OVER (PARTITION BY os.app_id)), 0.0) AS bq_s
    FROM (
      SELECT
        COALESCE(ols.linked_outcome_id, w.outcome_id) AS outcome_id,
        COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS app_id,
        (COALESCE(o.outcome_status, 'open') = 'succeeded' AND COALESCE(o.is_verified, FALSE) AND COALESCE(j.s2_ok, TRUE)) AS is_verified,
        COUNT(*) AS sp_c, COUNTIF(w.span_cost_usd IS NULL) AS unp,
        SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN COALESCE(w.span_cost_usd, 0.0) * COALESCE(ols.lsh, 1.0) ELSE 0.0 END) AS m_u,
        SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN COALESCE(w.span_cost_usd, 0.0) * COALESCE(ols.lsh, 1.0) ELSE 0.0 END) AS sj_u,
        COALESCE(MAX(j.ej_usd), 0.0) AS oj_u, MAX(wt.storage_usd_per_outcome) AS ast_u,
        SUM(IF(NOT w.is_cp, (IF(w.okind IN ('SERVER', 'INTERNAL') AND w.srn = 1, (w.latency_ms / 1000.0) * ((2.0 * 0.00002400) + (2.0 * 0.00000250)), 0.0) + (GREATEST(0.0, COALESCE(w.sandbox_cpu_sec, 0.0)) * 0.00002400) + (GREATEST(0.0, COALESCE(w.sandbox_mem_gb_sec, 0.0)) * 0.00000250)) * COALESCE(ols.lsh, 1.0), 0.0)) AS sc_u,
        SUM(IF(NOT w.is_cp AND w.okind IN ('SERVER', 'INTERNAL') AND w.orn = 1, COALESCE(w.p_est, 0.000020) * COALESCE(ols.lsh, 1.0), 0.0)) AS ss_u,
        SUM(IF(NOT w.is_cp AND w.okind IN ('SERVER', 'INTERNAL') AND w.orn = 1, COALESCE(ols.lsh, 1.0), 0.0)) AS ss_sh,
        LOGICAL_OR(NOT w.is_cp AND w.okind IN ('SERVER', 'INTERNAL') AND w.orn = 1 AND w.p_est IS NOT NULL) AS sh_es
      FROM (
        SELECT w0.*,
          ROW_NUMBER() OVER (PARTITION BY w0.trace_id, COALESCE(w0.service_name, 'agent-service') ORDER BY CASE WHEN w0.okind = 'SERVER' THEN 0 WHEN w0.okind = 'INTERNAL' THEN 1 ELSE 2 END, IF(w0.parent_span_id IS NULL, 0, 1), w0.start_time ASC, w0.span_id ASC) AS srn,
          ROW_NUMBER() OVER (PARTITION BY w0.outcome_id ORDER BY IF(NOT w0.is_cp AND w0.okind IN ('SERVER', 'INTERNAL'), 0, 1), w0.start_time ASC, w0.span_id ASC) AS orn,
          MAX(CASE WHEN NOT w0.is_cp AND w0.okind IN ('SERVER', 'INTERNAL') AND w0.p_st >= 0.0 THEN w0.p_st END) OVER (PARTITION BY w0.outcome_id) AS p_est
        FROM (
          SELECT wf.*, wt_p.storage_usd_per_outcome AS p_st, COALESCE(wt_p.is_external_counterparty, FALSE) AS is_cp,
            CASE UPPER(TRIM(COALESCE(wf.otel_span_kind, 'INTERNAL')))
              WHEN '1' THEN 'INTERNAL' WHEN 'INTERNAL' THEN 'INTERNAL' WHEN 'SPAN_KIND_INTERNAL' THEN 'INTERNAL'
              WHEN '2' THEN 'SERVER' WHEN 'SERVER' THEN 'SERVER' WHEN 'SPAN_KIND_SERVER' THEN 'SERVER' ELSE 'OTHER'
            END AS okind
          FROM `agent_eval.v_trace_waterfall` AS wf
          LEFT JOIN `agent_eval.workload_thresholds` AS wt_p ON COALESCE(wf.outcome_owner, wf.apphub_application_id) = wt_p.apphub_application_id
          WHERE wf.outcome_id IS NOT NULL
        ) AS w0
      ) AS w
      LEFT JOIN (SELECT primary_outcome_id, linked_outcome_id, SAFE_DIVIDE(1.0, COUNT(*) OVER (PARTITION BY primary_outcome_id)) AS lsh FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcome_links`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())) AS ols ON w.outcome_id = ols.primary_outcome_id
      LEFT JOIN `agent_eval.v_outcomes` AS o ON COALESCE(ols.linked_outcome_id, w.outcome_id) = o.outcome_id
      LEFT JOIN (
        SELECT outcome_id,
          LOGICAL_AND(CASE WHEN COALESCE(judge_status, 'SCORED') = 'SCORED' AND binary_pass IS NOT NULL AND (ordinal_score IS NULL OR ordinal_score BETWEEN 1 AND 4) AND lrn = 1 THEN binary_pass END) AS s2_ok,
          SUM(((judge_input_tokens / 1e6) * in_r) + (((judge_output_tokens + judge_thinking_tokens) / 1e6) * out_r)) AS ej_usd
        FROM (
          SELECT e.eval_id, e.trace_id, e.rubric_id, COALESCE(e.outcome_id, tr_o.outcome_id) AS outcome_id,
            e.judge_status, e.binary_pass, e.ordinal_score, e.evaluated_at, e.judge_input_tokens, e.judge_output_tokens, e.judge_thinking_tokens,
            COALESCE(m.input_usd_per_1m, d.input_usd_per_1m) AS in_r, COALESCE(m.output_usd_per_1m, d.output_usd_per_1m) AS out_r,
            ROW_NUMBER() OVER (PARTITION BY e.trace_id, e.rubric_id, IF(COALESCE(e.judge_status, 'SCORED') = 'SCORED' AND e.binary_pass IS NOT NULL AND (e.ordinal_score IS NULL OR e.ordinal_score BETWEEN 1 AND 4), 0, 1) ORDER BY e.evaluated_at DESC) AS lrn
          FROM (SELECT *, REGEXP_REPLACE(REGEXP_REPLACE(judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') AS jm_norm FROM `agent_eval.eval_results` QUALIFY ROW_NUMBER() OVER (PARTITION BY COALESCE(eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', evaluated_at)), trace_id, rubric_id ORDER BY IF(COALESCE(judge_status, 'SCORED') = 'SCORED', 0, 1) ASC, evaluated_at DESC) = 1) AS e
          LEFT JOIN `${PROJECT_ID}.${SILVER_OPEN_DATASET}.traces_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) AS tr_o ON e.trace_id = tr_o.trace_id
          LEFT JOIN `agent_eval.model_pricing_catalog` AS m ON e.jm_norm = m.model_name AND e.evaluated_at >= m.effective_start AND e.evaluated_at < m.effective_end
          LEFT JOIN `agent_eval.v_default_model_pricing` AS d ON e.jm_norm = d.model_name
          WHERE COALESCE(e.outcome_id, tr_o.outcome_id) IS NOT NULL
          QUALIFY ROW_NUMBER() OVER (PARTITION BY e.eval_id ORDER BY m.effective_start DESC, m.effective_end ASC) = 1
        ) GROUP BY 1
      ) AS j ON COALESCE(ols.linked_outcome_id, w.outcome_id) = j.outcome_id
      LEFT JOIN `agent_eval.workload_thresholds` AS wt ON COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') = wt.apphub_application_id
      WHERE NOT COALESCE(wt.is_external_counterparty, FALSE)
      GROUP BY 1, 2, 3
    ) AS os
    LEFT JOIN `agent_eval.v_bq_query_cost_attribution` AS qc ON os.app_id = qc.apphub_application_id
  ) GROUP BY 1
),
cb AS (
  SELECT jm.*,
    COALESCE(jm.eff_rt, ar.risk_tier, 'RL-1') AS risk_tier,
    IF(COALESCE(jm.eff_rt, ar.risk_tier, 'RL-1') = 'RL-1', 100, 10) AS min_req,
    CASE COALESCE(jm.eff_rt, ar.risk_tier, 'RL-1') WHEN 'RL-1' THEN 0.85 WHEN 'RL-2' THEN 0.85 WHEN 'RL-4' THEN 0.75 WHEN 'RL-5' THEN 0.75 ELSE 0.80 END AS req_ac2,
    IF(jm.jm_rubric_id IS NULL OR jm.inc_strata OR NOT ((jm.hp_c = 0 OR jm.h_ok) AND (jm.tp_c = 0 OR jm.t_ok)), CAST(NULL AS FLOAT64),
      COALESCE(jm.w_h * LEAST(1.0, GREATEST(0.0, SAFE_DIVIDE(jm.h_pr + jm.h_tnr - 1.0, jm.h_tpr + jm.h_tnr - 1.0))), 0.0)
      + COALESCE(jm.w_t * LEAST(1.0, GREATEST(0.0, SAFE_DIVIDE(jm.t_pr + jm.t_tnr - 1.0, jm.t_tpr + jm.t_tnr - 1.0))), 0.0)) AS st_theta,
    IF(jm.jm_rubric_id IS NULL OR jm.inc_strata, CAST(NULL AS FLOAT64), COALESCE(jm.w_h * jm.h_pr, 0.0) + COALESCE(jm.w_t * jm.t_pr, 0.0)) AS raw_judge_pass_rate,
    IF(jm.jm_rubric_id IS NULL OR jm.inc_strata, CAST(NULL AS FLOAT64),
      COALESCE(POW(jm.w_h, 2) * SAFE_DIVIDE(jm.h_pr * (1.0 - jm.h_pr), NULLIF(jm.hj_c, 0)), 0.0)
      + COALESCE(POW(jm.w_t, 2) * SAFE_DIVIDE(jm.t_pr * (1.0 - jm.t_pr), NULLIF(jm.tj_c, 0)), 0.0)) AS var_p_obs,
    IF(jm.jm_rubric_id IS NULL OR jm.inc_strata, CAST(NULL AS FLOAT64),
      SAFE_DIVIDE(1.0, NULLIF(COALESCE(SAFE_DIVIDE(POW(jm.w_h, 2), NULLIF(jm.hj_c, 0)), 0.0) + COALESCE(SAFE_DIVIDE(POW(jm.w_t, 2), NULLIF(jm.tj_c, 0)), 0.0), 0.0))) AS kish_eff_n,
    sc.tp, sc.fn, sc.tn, sc.fp, sc.tpr, sc.tnr, COALESCE(sc.ord_pairs, 0) AS ord_pairs, COALESCE(sc.dual_pairs, 0) AS dual_pairs,
    sc.gwet_ac2, sc.sme_vs_sme_ac2, sc.binary_cutpoint_agreement, (sc.tpr + sc.tnr - 1.0) AS youden_j,
    ((ar.validation_expiry_date IS NOT NULL AND ar.validation_expiry_date < CURRENT_DATE('UTC')) OR (sc.min_exp IS NOT NULL AND sc.min_exp < CURRENT_DATE('UTC')) OR (sc.sme_snap IS NOT NULL AND jm.j_snap IS NOT NULL AND sc.sme_snap != jm.j_snap) OR (sc.sme_psha IS NOT NULL AND jm.j_psha IS NOT NULL AND sc.sme_psha != jm.j_psha) OR (sc.sme_rver IS NOT NULL AND jm.j_rver IS NOT NULL AND sc.sme_rver != jm.j_rver)) AS is_stale,
    CAST(sc.tp + sc.fn + 2 AS FLOAT64) AS n_se, CAST(sc.tn + sc.fp + 2 AS FLOAT64) AS n_sp,
    SAFE_DIVIDE(CAST(sc.tp + 1 AS FLOAT64), CAST(sc.tp + sc.fn + 2 AS FLOAT64)) AS se_a,
    SAFE_DIVIDE(CAST(sc.tn + 1 AS FLOAT64), CAST(sc.tn + sc.fp + 2 AS FLOAT64)) AS sp_a,
    IF(jm.span_autorater_judge_usd IS NULL, CAST(NULL AS FLOAT64), GREATEST(jm.span_autorater_judge_usd, COALESCE(ajs.app_ej_usd, 0.0))) AS autorater_judge_usd,
    COALESCE(qc.bq_query_cost_usd, 0.0) AS bq_query_cost_usd,
    COALESCE(ao.outcome_total_count, 0) AS outcome_total_count, COALESCE(ao.outcome_verified_count, 0) AS outcome_verified_count,
    SAFE_DIVIDE(ao.ai_usd, NULLIF(ao.outcome_verified_count, 0)) AS outcome_cost_per_verified_ai_usd,
    SAFE_DIVIDE(ao.tot_usd, NULLIF(ao.outcome_verified_count, 0)) AS outcome_cost_per_verified_usd
  FROM jmr AS jm
  LEFT JOIN active_apps AS ar ON jm.app_id = ar.app_id
  LEFT JOIN `agent_eval.v_bq_query_cost_attribution` AS qc ON jm.app_id = qc.apphub_application_id
  LEFT JOIN ajs ON jm.app_id = ajs.app_id
  LEFT JOIN sme_cal AS sc ON jm.app_id = sc.app_id AND jm.jm_rubric_id = sc.rubric_id
  LEFT JOIN app_outcomes AS ao ON jm.app_id = ao.app_id
),
wig AS (
  SELECT cb.*,
    IF(cb.gwet_ac2 IS NULL, CAST(NULL AS BOOL), cb.ord_pairs >= cb.min_req AND cb.gwet_ac2 >= cb.req_ac2 AND COALESCE(cb.binary_cutpoint_agreement, 0.0) >= 0.85 AND (cb.dual_pairs = 0 OR (cb.dual_pairs >= cb.min_req AND COALESCE(cb.sme_vs_sme_ac2, 0.0) >= cb.req_ac2))) AS passes_irr_gate,
    CASE WHEN cb.var_p_obs IS NOT NULL AND cb.var_p_obs > 0.0 AND cb.raw_judge_pass_rate > 0.0 AND cb.raw_judge_pass_rate < 1.0 THEN GREATEST(1.0, SAFE_DIVIDE(cb.raw_judge_pass_rate * (1.0 - cb.raw_judge_pass_rate), cb.var_p_obs)) WHEN cb.kish_eff_n IS NOT NULL THEN GREATEST(1.0, cb.kish_eff_n) ELSE CAST(cb.judged_samples AS FLOAT64) END AS eff_n
  FROM cb
),
st AS (
  SELECT g.*,
    CASE
      WHEN g.miss_sess > 0 THEN 'MISSING_SESSION_CONTEXT'
      WHEN g.judged_samples = 0 THEN 'UNJUDGED'
      WHEN g.is_stale THEN 'STALE_CALIBRATION'
      WHEN g.inc_strata OR (g.has_strat_sme AND g.st_theta IS NULL) OR g.raw_judge_pass_rate IS NULL OR g.judged_samples < g.min_req OR COALESCE(g.passes_irr_gate, FALSE) IS FALSE OR g.tpr IS NULL OR g.tnr IS NULL OR (g.tp + g.fn) < 5 OR (g.tn + g.fp) < 5 OR g.youden_j <= 1e-6 OR (g.se_a + g.sp_a - 1.0) <= 1e-6 THEN 'UNCALIBRATED'
      ELSE 'CALIBRATED'
    END AS calibration_status
  FROM wig AS g
),
ci AS (
  SELECT s.*,
    CASE WHEN s.calibration_status != 'CALIBRATED' THEN NULL WHEN s.st_theta IS NOT NULL THEN s.st_theta ELSE LEAST(1.0, GREATEST(0.0, SAFE_DIVIDE(s.raw_judge_pass_rate + s.tnr - 1.0, s.tpr + s.tnr - 1.0))) END AS theta_rg,
    (s.eff_n + 3.841458820694124) AS n_p,
    SAFE_DIVIDE(s.raw_judge_pass_rate * s.eff_n + 1.920729410347062, s.eff_n + 3.841458820694124) AS p_a,
    (s.se_a + s.sp_a - 1.0) AS j_a,
    IF(s.calibration_status != 'CALIBRATED', NULL, LEAST(1.0, GREATEST(0.0, SAFE_DIVIDE(SAFE_DIVIDE(s.raw_judge_pass_rate * s.eff_n + 1.920729410347062, s.eff_n + 3.841458820694124) + s.sp_a - 1.0, s.se_a + s.sp_a - 1.0)))) AS theta_a,
    (s.production_inference_usd + s.conflict_escalation_usd + s.agent_gym_eval_usd + s.autorater_judge_usd + s.optimize_usd + s.bq_query_cost_usd + s.unclassified_usd) AS tot_sp
  FROM st AS s
),
cif AS (
  SELECT l.*,
    IF(l.theta_a IS NULL, NULL, 2.0 * 3.841458820694124 * (l.theta_a * SAFE_DIVIDE(l.se_a * (1.0 - l.se_a), l.n_se) - (1.0 - l.theta_a) * SAFE_DIVIDE(l.sp_a * (1.0 - l.sp_a), l.n_sp))) AS dp_shift,
    IF(l.theta_a IS NULL, NULL, SAFE_DIVIDE(SQRT(SAFE_DIVIDE(l.p_a * (1.0 - l.p_a), l.n_p) + POW(l.theta_a, 2) * SAFE_DIVIDE(l.se_a * (1.0 - l.se_a), l.n_se) + POW(1.0 - l.theta_a, 2) * SAFE_DIVIDE(l.sp_a * (1.0 - l.sp_a), l.n_sp)), l.j_a)) AS se_t
  FROM ci AS l
)
SELECT
  c.app_id AS apphub_application_id, c.rubric_id, c.risk_tier, c.req_ac2 AS required_ac2_gate,
  c.total_traces, c.production_traces, c.rule0_pass_rate, c.tool_success_rate, c.p95_latency_ms, c.avg_cache_hit_ratio, c.total_weighted_burndown_tokens,
  c.judged_samples, c.calibration_status, c.gwet_ac2, c.sme_vs_sme_ac2, c.binary_cutpoint_agreement, c.passes_irr_gate, c.raw_judge_pass_rate,
  c.theta_rg AS calibrated_pass_rate,
  IF(c.theta_a IS NULL, NULL, LEAST(1.0, GREATEST(0.0, c.theta_a + c.dp_shift - 1.959963984540054 * c.se_t))) AS calibrated_ci_95_lower,
  IF(c.theta_a IS NULL, NULL, LEAST(1.0, GREATEST(0.0, c.theta_a + c.dp_shift + 1.959963984540054 * c.se_t))) AS calibrated_ci_95_upper,
  c.unpriced_spans, c.production_inference_usd, c.conflict_escalation_usd, c.agent_gym_eval_usd, c.autorater_judge_usd, c.optimize_usd, c.bq_query_cost_usd, c.unclassified_usd,
  IF(c.unpriced_spans > 0, CAST(NULL AS FLOAT64), c.tot_sp) AS total_spend_usd,
  CASE WHEN c.unpriced_spans > 0 OR c.theta_rg IS NULL OR c.theta_rg <= 0.0 OR c.prod_r0_traces = 0 THEN NULL ELSE SAFE_DIVIDE(c.tot_sp, c.prod_r0_traces * c.theta_rg) END AS cost_per_verified_outcome_usd,
  c.outcome_total_count, c.outcome_verified_count, c.outcome_cost_per_verified_ai_usd, c.outcome_cost_per_verified_usd
FROM cif AS c;
