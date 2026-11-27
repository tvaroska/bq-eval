-- Gold Step-1 Cost per Outcome, Enterprise CPVO, and Fail-Closed Stratified Summary (`ADR-033`, `ADR-048`, `ADR-049`).

CREATE OR REPLACE VIEW `agent_eval.v_step1_cost_per_outcome` AS
SELECT
  apphub_application_id, business_unit, cost_center,
  COUNT(*) AS total_traces, COUNTIF(rule0_pass) AS step1_passing_traces,
  AVG(IF(rule0_pass, 1.0, 0.0)) AS step1_pass_rate, AVG(IF(tool_error_count = 0, 1.0, 0.0)) AS tool_success_rate,
  APPROX_QUANTILES(trace_latency_ms, 100)[OFFSET(95)] AS p95_latency_ms, COALESCE(AVG(cache_hit_ratio), 0.0) AS avg_cache_hit_ratio,
  SUM(unpriced_spans) AS unpriced_spans,
  IF(SUM(unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(trace_span_cost_usd)) AS total_cost_usd,
  IF(SUM(unpriced_spans) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(trace_span_cost_usd), COUNT(*))) AS cost_per_trace_usd,
  IF(SUM(unpriced_spans) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(trace_span_cost_usd), NULLIF(COUNTIF(rule0_pass), 0))) AS step1_cost_per_outcome_usd
FROM `agent_eval.v_trace_sql_evaluation`
GROUP BY apphub_application_id, business_unit, cost_center;

CREATE OR REPLACE VIEW `agent_eval.v_enterprise_cost_per_verified_outcome` AS
WITH da AS (
  SELECT e.*, REGEXP_REPLACE(REGEXP_REPLACE(e.judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') AS jmn,
    ROW_NUMBER() OVER (PARTITION BY e.trace_id, e.rubric_id, IF(COALESCE(e.judge_status, 'SCORED') = 'SCORED' AND e.binary_pass IS NOT NULL AND (e.ordinal_score IS NULL OR e.ordinal_score BETWEEN 1 AND 4), 0, 1) ORDER BY e.evaluated_at DESC) AS lrn
  FROM (
    SELECT * FROM `agent_eval.eval_results`
    QUALIFY ROW_NUMBER() OVER (PARTITION BY COALESCE(eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', evaluated_at)), trace_id, rubric_id ORDER BY IF(COALESCE(judge_status, 'SCORED') = 'SCORED', 0, 1) ASC, evaluated_at DESC) = 1
  ) AS e
),
dp AS (
  SELECT e.eval_id, e.trace_id, e.outcome_id,
    (COALESCE(e.judge_status, 'SCORED') = 'SCORED' AND e.binary_pass IS NOT NULL AND (e.ordinal_score IS NULL OR e.ordinal_score BETWEEN 1 AND 4) AND e.lrn = 1) AS s2,
    e.binary_pass, ((e.judge_input_tokens / 1e6) * COALESCE(m.input_usd_per_1m, d.input_usd_per_1m)) + (((e.judge_output_tokens + e.judge_thinking_tokens) / 1e6) * COALESCE(m.output_usd_per_1m, d.output_usd_per_1m)) AS jc
  FROM da AS e
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m ON e.jmn = m.model_name AND e.evaluated_at >= m.effective_start AND e.evaluated_at < m.effective_end
  LEFT JOIN `agent_eval.v_default_model_pricing` AS d ON e.jmn = d.model_name
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.eval_id ORDER BY m.effective_start DESC, m.effective_end ASC) = 1
),
jo AS (SELECT outcome_id, LOGICAL_AND(IF(s2, binary_pass, NULL)) AS s2_ok, SUM(jc) AS ej_u FROM dp WHERE outcome_id IS NOT NULL GROUP BY 1),
jt AS (SELECT trace_id, LOGICAL_AND(IF(s2, binary_pass, NULL)) AS s2_ok, SUM(jc) AS ej_u FROM dp WHERE outcome_id IS NULL GROUP BY 1),
ols AS (
  SELECT primary_outcome_id, linked_outcome_id, SAFE_DIVIDE(1.0, COUNT(*) OVER (PARTITION BY primary_outcome_id)) AS lsh
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcome_links`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
),
wft AS (
  SELECT w0.*,
    ROW_NUMBER() OVER (PARTITION BY w0.trace_id, COALESCE(w0.service_name, 'agent-service') ORDER BY CASE WHEN w0.ok = 'SERVER' THEN 0 WHEN w0.ok = 'INTERNAL' THEN 1 ELSE 2 END, IF(w0.parent_span_id IS NULL, 0, 1), w0.start_time ASC, w0.span_id ASC) AS srn,
    ROW_NUMBER() OVER (PARTITION BY w0.outcome_id ORDER BY IF(NOT w0.icp AND w0.ok IN ('SERVER', 'INTERNAL'), 0, 1), w0.start_time ASC, w0.span_id ASC) AS orn,
    ROW_NUMBER() OVER (PARTITION BY w0.outcome_id, w0.trace_id ORDER BY w0.start_time ASC, w0.span_id ASC) AS trn,
    (MIN(w0.outcome_id) OVER (PARTITION BY w0.trace_id) = MAX(w0.outcome_id) OVER (PARTITION BY w0.trace_id)) AS o1,
    MAX(CASE WHEN NOT w0.icp AND w0.ok IN ('SERVER', 'INTERNAL') AND w0.pst >= 0.0 THEN w0.pst END) OVER (PARTITION BY w0.outcome_id) AS pest
  FROM (
    SELECT w.*, wp.storage_usd_per_outcome AS pst, COALESCE(wp.is_external_counterparty, FALSE) AS icp,
      CASE WHEN UPPER(TRIM(COALESCE(w.otel_span_kind, 'INTERNAL'))) IN ('1', 'INTERNAL', 'SPAN_KIND_INTERNAL') THEN 'INTERNAL' WHEN UPPER(TRIM(COALESCE(w.otel_span_kind, 'INTERNAL'))) IN ('2', 'SERVER', 'SPAN_KIND_SERVER') THEN 'SERVER' ELSE 'OTHER' END AS ok
    FROM `agent_eval.v_trace_waterfall` AS w
    LEFT JOIN `agent_eval.workload_thresholds` AS wp ON COALESCE(w.outcome_owner, w.apphub_application_id) = wp.apphub_application_id
    WHERE w.outcome_id IS NOT NULL
  ) AS w0
),
ew AS (
  SELECT COALESCE(ols.linked_outcome_id, w.outcome_id) AS eoid, (ols.linked_outcome_id IS NULL) AS dl, COALESCE(ols.lsh, 1.0) AS csh, w.*,
    IF(NOT w.icp, (IF(w.ok IN ('SERVER', 'INTERNAL') AND w.srn = 1, (w.latency_ms / 1000.0) * 0.000053, 0.0) + (GREATEST(0.0, COALESCE(w.sandbox_cpu_sec, 0.0)) * 0.000024) + (GREATEST(0.0, COALESCE(w.sandbox_mem_gb_sec, 0.0)) * 0.0000025)) * COALESCE(ols.lsh, 1.0), 0.0) AS cu,
    IF(NOT w.icp AND w.ok IN ('SERVER', 'INTERNAL') AND w.orn = 1, COALESCE(w.pest, 0.000020) * COALESCE(ols.lsh, 1.0), 0.0) AS su,
    IF(NOT w.icp AND w.ok IN ('SERVER', 'INTERNAL') AND w.orn = 1, COALESCE(ols.lsh, 1.0), 0.0) AS ssh,
    (NOT w.icp AND w.ok IN ('SERVER', 'INTERNAL') AND w.orn = 1 AND w.pest IS NOT NULL) AS hes
  FROM wft AS w LEFT JOIN ols ON w.outcome_id = ols.primary_outcome_id
),
os0 AS (
  SELECT
    w.eoid AS outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(w.source_mode, o.source_mode, 'UNSPECIFIED') AS source_mode,
    w.agent_name, COALESCE(o.outcome_status, 'open') AS outcome_status,
    (LOGICAL_AND(COALESCE(o.outcome_status, 'open') = 'succeeded' AND COALESCE(o.is_verified, FALSE) AND COALESCE(jo.s2_ok, TRUE)) AND COALESCE(LOGICAL_AND(IF(w.dl AND w.o1, jt.s2_ok, NULL)), TRUE)) AS g_ok,
    COUNT(DISTINCT w.trace_id) AS tc, COUNT(*) AS sc,
    SUM(CAST(ROUND(w.input_tokens * w.csh) AS INT64)) AS in_t, SUM(CAST(ROUND(w.cached_input_tokens * w.csh) AS INT64)) AS ca_t,
    SUM(CAST(ROUND(w.output_tokens * w.csh) AS INT64)) AS ou_t, SUM(CAST(ROUND(w.thinking_tokens * w.csh) AS INT64)) AS th_t,
    COUNTIF(w.span_cost_usd IS NULL) AS unp,
    IF(COUNTIF(w.finops_traffic_type != 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN w.span_cost_usd * w.csh ELSE 0.0 END)) AS m_u,
    IF(COUNTIF(w.finops_traffic_type = 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd * w.csh ELSE 0.0 END)) AS sj_u,
    COALESCE(MAX(jo.ej_u), 0.0) AS ejo_u, SUM(IF(w.dl AND w.o1 AND w.trn = 1, COALESCE(jt.ej_u, 0.0), 0.0)) AS ejt_u,
    MAX(wt.storage_usd_per_outcome) AS ast, SUM(w.cu) AS scu, SUM(w.su) AS ssu, SUM(w.ssh) AS sssh, LOGICAL_OR(w.hes) AS shes
  FROM ew AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o ON w.eoid = o.outcome_id
  LEFT JOIN jo ON w.eoid = jo.outcome_id LEFT JOIN jt ON w.dl AND w.trace_id = jt.trace_id
  LEFT JOIN `agent_eval.workload_thresholds` AS wt ON COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') = wt.apphub_application_id
  WHERE NOT COALESCE(wt.is_external_counterparty, FALSE) GROUP BY 1, 2, 3, 4, 5
),
wos AS (
  SELECT os.*,
    (MIN(IF(os.g_ok, 1, 0)) OVER (PARTITION BY os.outcome_id) = 1) AS is_v,
    (os.ejo_u + SUM(os.ejt_u) OVER (PARTITION BY os.outcome_id)) AS oj_u,
    SUM(os.sc) OVER (PARTITION BY os.outcome_id) AS out_sp, SUM(os.sc) OVER (PARTITION BY os.apphub_application_id) AS app_sp,
    SUM(COALESCE(os.sj_u, 0.0)) OVER (PARTITION BY os.outcome_id) AS out_sj,
    SUM(os.scu) OVER (PARTITION BY os.outcome_id) AS out_cu, SUM(os.ssu) OVER (PARTITION BY os.outcome_id) AS out_su,
    SUM(os.sssh) OVER (PARTITION BY os.outcome_id) AS out_ssh, MAX(IF(os.shes, 1, 0)) OVER (PARTITION BY os.outcome_id) = 1 AS out_es
  FROM os0 AS os
),
aos AS (
  SELECT os.*,
    os.sj_u + GREATEST(0.0, os.oj_u - os.out_sj) * COALESCE(SAFE_DIVIDE(os.sc, os.out_sp), 0.0) AS j_u,
    (os.out_cu + IF(os.out_es, os.out_su, COALESCE(os.ast * NULLIF(os.out_ssh, 0.0), NULLIF(os.out_su, 0.0), 0.0))) * COALESCE(SAFE_DIVIDE(os.sc, os.out_sp), 0.0) AS inf_u,
    COALESCE(qc.bq_query_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.sc, os.app_sp), 0.0) AS bq_u
  FROM wos AS os LEFT JOIN `agent_eval.v_bq_query_cost_attribution` AS qc ON os.apphub_application_id = qc.apphub_application_id
)
SELECT
  os.apphub_application_id, os.source_mode, os.agent_name,
  COUNT(DISTINCT os.outcome_id) AS total_outcomes, COUNT(DISTINCT IF(os.is_v, os.outcome_id, NULL)) AS verified_outcomes,
  COUNT(DISTINCT IF(NOT os.is_v, os.outcome_id, NULL)) AS unverified_or_failed_outcomes,
  SUM(os.tc) AS total_traces, SUM(os.in_t) AS total_input_tokens, SUM(os.ou_t) AS total_output_tokens, SUM(os.th_t) AS total_thinking_tokens,
  SUM(os.unp) AS unpriced_spans,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SUM(os.m_u)) AS measured_model_cost_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SUM(os.j_u)) AS measured_judge_cost_usd,
  SUM(os.inf_u) AS estimated_infra_cost_usd, SUM(os.bq_u) AS bq_query_cost_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SUM(os.m_u + os.j_u)) AS total_ai_cost_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SUM(os.m_u + os.j_u + os.inf_u + os.bq_u)) AS total_cost_with_infra_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(os.m_u + os.j_u), NULLIF(COUNT(DISTINCT IF(os.is_v, os.outcome_id, NULL)), 0))) AS cost_per_verified_outcome_ai_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(os.m_u + os.j_u + os.inf_u + os.bq_u), NULLIF(COUNT(DISTINCT IF(os.is_v, os.outcome_id, NULL)), 0))) AS cost_per_verified_outcome_usd
FROM aos AS os GROUP BY os.apphub_application_id, os.source_mode, os.agent_name;

CREATE OR REPLACE VIEW `agent_eval.v_agent_eval_and_finops_summary` AS
WITH da AS (
  SELECT e.*, REGEXP_REPLACE(REGEXP_REPLACE(e.judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') AS jmn,
    (COALESCE(e.judge_status, 'SCORED') = 'SCORED' AND e.binary_pass IS NOT NULL) AS sc_ok,
    ROW_NUMBER() OVER (PARTITION BY e.trace_id, e.rubric_id, IF(COALESCE(e.judge_status, 'SCORED') = 'SCORED' AND e.binary_pass IS NOT NULL AND (e.ordinal_score IS NULL OR e.ordinal_score BETWEEN 1 AND 4), 0, 1) ORDER BY e.evaluated_at DESC) AS lrn
  FROM (
    SELECT * FROM `agent_eval.eval_results`
    QUALIFY ROW_NUMBER() OVER (PARTITION BY COALESCE(eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', evaluated_at)), trace_id, rubric_id ORDER BY IF(COALESCE(judge_status, 'SCORED') = 'SCORED' AND binary_pass IS NOT NULL, 0, 1) ASC, evaluated_at DESC) = 1
  ) AS e
),
dp AS (
  SELECT e.eval_id, e.apphub_application_id AS app_id, e.rubric_id, e.trace_id, e.outcome_id, e.sc_ok,
    (e.sc_ok AND (e.ordinal_score IS NULL OR e.ordinal_score BETWEEN 1 AND 4) AND e.lrn = 1) AS s2, e.binary_pass,
    ((e.judge_input_tokens / 1e6) * COALESCE(m.input_usd_per_1m, d.input_usd_per_1m)) + (((e.judge_output_tokens + e.judge_thinking_tokens) / 1e6) * COALESCE(m.output_usd_per_1m, d.output_usd_per_1m)) AS jc
  FROM da AS e
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m ON e.jmn = m.model_name AND e.evaluated_at >= m.effective_start AND e.evaluated_at < m.effective_end
  LEFT JOIN `agent_eval.v_default_model_pricing` AS d ON e.jmn = d.model_name
  QUALIFY ROW_NUMBER() OVER (PARTITION BY e.eval_id ORDER BY m.effective_start DESC, m.effective_end ASC) = 1
),
js AS (SELECT app_id, rubric_id, SUM(jc) AS ej_u FROM dp WHERE sc_ok GROUP BY 1, 2),
ajs AS (SELECT app_id, SUM(ej_u) AS aej_u FROM js GROUP BY 1),
ler AS (
  SELECT apphub_application_id AS app_id, rubric_id, trace_id, binary_pass, judge_model_snapshot_id, judge_prompt_sha256, rubric_version
  FROM da WHERE sc_ok AND rule0_pass = TRUE AND (ordinal_score IS NULL OR ordinal_score BETWEEN 1 AND 4)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY apphub_application_id, trace_id, rubric_id ORDER BY evaluated_at DESC) = 1
),
ert AS (SELECT app_id, rubric_id, COUNT(*) AS rc, MAX(judge_model_snapshot_id) AS jsn, MAX(judge_prompt_sha256) AS jps, MAX(rubric_version) AS jrv FROM ler GROUP BY 1, 2),
jo AS (SELECT outcome_id, LOGICAL_AND(IF(s2, binary_pass, NULL)) AS s2_ok, SUM(jc) AS ej_u FROM dp WHERE outcome_id IS NOT NULL GROUP BY 1),
jt AS (SELECT trace_id, LOGICAL_AND(IF(s2, binary_pass, NULL)) AS s2_ok, SUM(jc) AS ej_u FROM dp WHERE outcome_id IS NULL GROUP BY 1),
tm AS (
  SELECT
    t.apphub_application_id AS app_id, js.rubric_id AS jmr_id, COALESCE(js.rubric_id, 'groundedness_4pt_v1') AS rubric_id, MIN(t.effective_risk_tier) AS eff_rt,
    COUNT(*) AS tot_t, COUNTIF(t.isp) AS prod_t, COUNTIF(t.pr0) AS pr0_t,
    COUNTIF(t.pr0 AND t.htp AND (t.badp OR (t.tpp IS NOT NULL AND t.eh AND NOT (t.conversation_id IS NOT NULL AND t.cmts < t.trace_start_time) AND NOT (t.tpp != t.trace_id AND STRPOS(CONCAT('|', COALESCE(t.atp, ''), '|'), CONCAT('|', t.tpp, '|')) > 0)))) AS msess,
    COUNTIF(t.pr0 AND t.ist) AS tp_c, COUNTIF(t.pr0 AND NOT t.ist) AS hp_c,
    COUNTIF(t.pr0 AND ea.trace_id IS NOT NULL AND t.ist) AS tj_c, AVG(CASE WHEN t.pr0 AND ea.trace_id IS NOT NULL AND t.ist THEN IF(ea.binary_pass, 1.0, 0.0) END) AS t_pr,
    COUNTIF(t.pr0 AND ea.trace_id IS NOT NULL AND NOT t.ist) AS hj_c, AVG(CASE WHEN t.pr0 AND ea.trace_id IS NOT NULL AND NOT t.ist THEN IF(ea.binary_pass, 1.0, 0.0) END) AS h_pr,
    COUNT(ea.trace_id) AS m_cnt, AVG(IF(t.rule0_pass, 1.0, 0.0)) AS r0_pr, AVG(IF(t.tool_error_count = 0, 1.0, 0.0)) AS ts_r,
    APPROX_QUANTILES(t.trace_latency_ms, 100)[OFFSET(95)] AS p95_ms, COALESCE(AVG(t.cache_hit_ratio), 0.0) AS chr,
    SUM(t.weighted_burndown_tokens) AS wbt, SUM(t.unpriced_spans) AS unp,
    IF(COUNTIF(t.production_inference_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.production_inference_usd)) AS pi_u,
    IF(COUNTIF(t.conflict_escalation_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.conflict_escalation_usd)) AS ce_u,
    IF(COUNTIF(t.agent_gym_eval_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.agent_gym_eval_usd)) AS ag_u,
    IF(COUNTIF(t.span_autorater_judge_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.span_autorater_judge_usd)) AS saj_u,
    IF(COUNTIF(t.optimize_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.optimize_usd)) AS op_u,
    IF(COUNTIF(t.unclassified_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(t.unclassified_usd)) AS un_u
  FROM (
    SELECT b.*, MIN(IF(b.htp, b.trace_start_time, NULL)) OVER (PARTITION BY b.conversation_id) AS cmts, STRING_AGG(IF(b.htp, b.trace_id, NULL), '|') OVER () AS atp
    FROM (
      SELECT te.*,
        te.primary_traffic_type IN ('production_inference', 'conflict_escalation') AS isp,
        (te.primary_traffic_type IN ('production_inference', 'conflict_escalation') AND te.rule0_pass) AS pr0,
        (te.is_loop_or_error_tail OR te.is_healthy_slow_tail) AS ist, tp.trace_id IS NOT NULL AS htp, tp.previous_interaction_id AS tpp,
        COALESCE(TO_JSON_STRING(tp.prior_history), '') IN ('', 'null') AS eh,
        NOT (COALESCE(tp.turn_prompt_read_whole, TRUE) AND COALESCE(tp.turn_context_read_whole, TRUE) AND COALESCE(tp.turn_output_read_whole, TRUE) AND COALESCE(tp.step_outputs_read_whole, TRUE) AND (tp.previous_interaction_id IS NOT NULL OR COALESCE(tp.prior_history_read_whole, TRUE))) AS badp
      FROM `agent_eval.v_trace_sql_evaluation` AS te
      LEFT JOIN `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.trace_payloads`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) AS tp ON te.trace_id = tp.trace_id
    ) AS b
  ) AS t
  LEFT JOIN js ON t.apphub_application_id = js.app_id
  LEFT JOIN ler AS ea ON js.rubric_id IS NOT NULL AND t.trace_id = ea.trace_id AND js.app_id = ea.app_id AND js.rubric_id = ea.rubric_id
  GROUP BY 1, 2
),
ssc AS (
  SELECT app_id, rubric_id, sb, SAFE_DIVIDE(tp, NULLIF(hp, 0)) AS stpr, SAFE_DIVIDE(tn, NULLIF(hn, 0)) AS stnr,
    (hp >= 5 AND hn >= 5 AND COALESCE(SAFE_DIVIDE(tp, NULLIF(hp, 0)) + SAFE_DIVIDE(tn, NULLIF(hn, 0)) - 1.0, 0.0) > 1e-6 AND (SAFE_DIVIDE(CAST(tp + 1 AS FLOAT64), CAST(hp + 2 AS FLOAT64)) + SAFE_DIVIDE(CAST(tn + 1 AS FLOAT64), CAST(hn + 2 AS FLOAT64)) - 1.0) > 1e-6) AS ok
  FROM (
    SELECT apphub_application_id AS app_id, rubric_id, IF(sampling_stratum IN ('ERROR_OR_SLA_TAIL', 'LOOP_OR_ERROR_TAIL'), 'ERROR_OR_SLA_TAIL', 'HEALTHY_POPULATION') AS sb,
      COUNTIF(human_pass AND judge_pass) AS tp, COUNTIF(NOT human_pass AND NOT judge_pass) AS tn, COUNTIF(human_pass) AS hp, COUNTIF(NOT human_pass) AS hn
    FROM `agent_eval.sme_labels` WHERE rule0_pass = TRUE AND sampling_stratum IS NOT NULL AND human_pass IS NOT NULL AND judge_pass IS NOT NULL GROUP BY 1, 2, 3
  )
),
ssp AS (
  SELECT app_id, rubric_id, TRUE AS hss,
    MAX(IF(sb = 'HEALTHY_POPULATION', stpr, NULL)) AS htpr, MAX(IF(sb = 'HEALTHY_POPULATION', stnr, NULL)) AS htnr, LOGICAL_OR(sb = 'HEALTHY_POPULATION' AND ok) AS hok,
    MAX(IF(sb = 'ERROR_OR_SLA_TAIL', stpr, NULL)) AS ttpr, MAX(IF(sb = 'ERROR_OR_SLA_TAIL', stnr, NULL)) AS ttnr, LOGICAL_OR(sb = 'ERROR_OR_SLA_TAIL' AND ok) AS tok
  FROM ssc GROUP BY 1, 2
),
jmr AS (
  SELECT m.*, (m.tj_c + m.hj_c) AS jsmp,
    IF(m.jmr_id IS NULL, FALSE, (m.tj_c + m.hj_c) = 0 OR (m.hp_c = 0 AND m.tp_c = 0) OR (m.hp_c > 0 AND (m.hj_c = 0 OR m.hj_c > m.hp_c)) OR (m.tp_c > 0 AND (m.tj_c = 0 OR m.tj_c > m.tp_c)) OR (m.hp_c = 0 AND m.hj_c > 0) OR (m.tp_c = 0 AND m.tj_c > 0) OR COALESCE(ert.rc, 0) > m.m_cnt) AS incs,
    SAFE_DIVIDE(CAST(m.hp_c AS FLOAT64), NULLIF(CAST(m.hp_c + m.tp_c AS FLOAT64), 0.0)) AS wh, SAFE_DIVIDE(CAST(m.tp_c AS FLOAT64), NULLIF(CAST(m.hp_c + m.tp_c AS FLOAT64), 0.0)) AS wt,
    COALESCE(ssp.hss, FALSE) AS hss, ssp.htpr, ssp.htnr, COALESCE(ssp.hok, FALSE) AS hok, ssp.ttpr, ssp.ttnr, COALESCE(ssp.tok, FALSE) AS tok, ert.jsn, ert.jps, ert.jrv
  FROM tm AS m LEFT JOIN ert ON m.app_id = ert.app_id AND m.jmr_id = ert.rubric_id LEFT JOIN ssp ON m.app_id = ssp.app_id AND m.jmr_id = ssp.rubric_id
),
so AS (
  SELECT app_id, rubric_id, on_c, bca, pa, (26.0 / 27.0) * (p1 * (1.0 - p1) + p2 * (1.0 - p2) + p3 * (1.0 - p3) + p4 * (1.0 - p4)) AS pe
  FROM (
    SELECT s.apphub_application_id AS app_id, s.rubric_id, COUNT(*) AS on_c,
      AVG(IF((s.human_ordinal_score >= COALESCE(r.passing_score, 3)) = (s.judge_ordinal_score >= COALESCE(r.passing_score, 3)), 1.0, 0.0)) AS bca,
      AVG(1.0 - POW(CAST(s.human_ordinal_score - s.judge_ordinal_score AS FLOAT64) / 3.0, 2)) AS pa,
      AVG(IF(s.human_ordinal_score = 1, 0.5, 0.0) + IF(s.judge_ordinal_score = 1, 0.5, 0.0)) AS p1,
      AVG(IF(s.human_ordinal_score = 2, 0.5, 0.0) + IF(s.judge_ordinal_score = 2, 0.5, 0.0)) AS p2,
      AVG(IF(s.human_ordinal_score = 3, 0.5, 0.0) + IF(s.judge_ordinal_score = 3, 0.5, 0.0)) AS p3,
      AVG(IF(s.human_ordinal_score = 4, 0.5, 0.0) + IF(s.judge_ordinal_score = 4, 0.5, 0.0)) AS p4
    FROM `agent_eval.sme_labels` AS s LEFT JOIN `agent_eval.rubrics` AS r ON s.rubric_id = r.rubric_id
    WHERE s.rule0_pass = TRUE AND s.human_ordinal_score BETWEEN 1 AND 4 AND s.judge_ordinal_score BETWEEN 1 AND 4 GROUP BY 1, 2
  )
),
sd AS (
  SELECT app_id, rubric_id, dn_c, pa, (26.0 / 27.0) * (p1 * (1.0 - p1) + p2 * (1.0 - p2) + p3 * (1.0 - p3) + p4 * (1.0 - p4)) AS pe
  FROM (
    SELECT apphub_application_id AS app_id, rubric_id, COUNT(*) AS dn_c,
      AVG(1.0 - POW(CAST(rater_a_score - rater_b_score AS FLOAT64) / 3.0, 2)) AS pa,
      AVG(IF(rater_a_score = 1, 0.5, 0.0) + IF(rater_b_score = 1, 0.5, 0.0)) AS p1,
      AVG(IF(rater_a_score = 2, 0.5, 0.0) + IF(rater_b_score = 2, 0.5, 0.0)) AS p2,
      AVG(IF(rater_a_score = 3, 0.5, 0.0) + IF(rater_b_score = 3, 0.5, 0.0)) AS p3,
      AVG(IF(rater_a_score = 4, 0.5, 0.0) + IF(rater_b_score = 4, 0.5, 0.0)) AS p4
    FROM `agent_eval.sme_labels` WHERE rule0_pass = TRUE AND rater_a_score BETWEEN 1 AND 4 AND rater_b_score BETWEEN 1 AND 4 GROUP BY 1, 2
  )
),
scm AS (
  SELECT app_id, rubric_id, tp, fn, tn, fp, SAFE_DIVIDE(tp, NULLIF(tp + fn, 0)) AS tpr, SAFE_DIVIDE(tn, NULLIF(tn + fp, 0)) AS tnr, mexp, ssn, sps, srv
  FROM (
    SELECT apphub_application_id AS app_id, rubric_id,
      COUNTIF(hj AND human_pass AND judge_pass) AS tp, COUNTIF(hj AND human_pass AND NOT judge_pass) AS fn,
      COUNTIF(hj AND NOT human_pass AND NOT judge_pass) AS tn, COUNTIF(hj AND NOT human_pass AND judge_pass) AS fp,
      MIN(validation_expiry_date) AS mexp, MAX(judge_model_snapshot_id) AS ssn, MAX(judge_prompt_sha256) AS sps, MAX(rubric_version) AS srv
    FROM (SELECT *, (human_pass IS NOT NULL AND judge_pass IS NOT NULL) AS hj FROM `agent_eval.sme_labels` WHERE rule0_pass = TRUE) GROUP BY 1, 2
  )
),
scal AS (
  SELECT cm.*, COALESCE(o.on_c, 0) AS opairs, o.bca, COALESCE(d.dn_c, 0) AS dpairs,
    CASE WHEN COALESCE(o.on_c, 0) = 0 THEN NULL WHEN ABS(1.0 - o.pe) < 1e-12 THEN 1.0 ELSE SAFE_DIVIDE(o.pa - o.pe, 1.0 - o.pe) END AS gac2,
    CASE WHEN COALESCE(d.dn_c, 0) = 0 THEN NULL WHEN ABS(1.0 - d.pe) < 1e-12 THEN 1.0 ELSE SAFE_DIVIDE(d.pa - d.pe, 1.0 - d.pe) END AS sac2
  FROM scm AS cm LEFT JOIN so AS o ON cm.app_id = o.app_id AND cm.rubric_id = o.rubric_id LEFT JOIN sd AS d ON cm.app_id = d.app_id AND cm.rubric_id = d.rubric_id
),
aa AS (
  SELECT app_id, risk_tier, validation_expiry_date FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry`
  WHERE effective_to IS NULL QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY effective_from DESC) = 1
),
ro AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcomes_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY outcome_id ORDER BY is_closed DESC, close_time DESC) = 1
),
oe AS (
  SELECT outcome_id, LOGICAL_AND(CASE WHEN JSON_VALUE(labels, '$.evaluation_score_label') IS NULL THEN TRUE WHEN LOWER(JSON_VALUE(labels, '$.evaluation_score_label')) IN ('true', 'verified', 'pass') THEN TRUE ELSE FALSE END) AS eok
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.events_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  WHERE outcome_id IS NOT NULL AND event_type IN ('outcome.closed', 'gen_ai.evaluation.result') GROUP BY 1
),
ao AS (
  SELECT app_id, COUNT(DISTINCT outcome_id) AS otc, COUNT(DISTINCT IF(is_v, outcome_id, NULL)) AS ovc,
    IF(SUM(unp) > 0, CAST(NULL AS FLOAT64), SUM(ai_s)) AS ai_u, IF(SUM(unp) > 0, CAST(NULL AS FLOAT64), SUM(ai_s + inf_s)) AS ai_inf_u
  FROM (
    SELECT os.outcome_id, os.app_id, (MIN(IF(os.g_ok, 1, 0)) OVER (PARTITION BY os.outcome_id) = 1) AS is_v, os.unp,
      COALESCE(os.m_u, 0.0) + COALESCE(os.sj_u, 0.0) + GREATEST(0.0, (os.ejo_u + SUM(os.ejt_u) OVER (PARTITION BY os.outcome_id)) - SUM(COALESCE(os.sj_u, 0.0)) OVER (PARTITION BY os.outcome_id)) * COALESCE(SAFE_DIVIDE(os.sc, SUM(os.sc) OVER (PARTITION BY os.outcome_id)), 0.0) AS ai_s,
      (SUM(os.scu) OVER (PARTITION BY os.outcome_id) + IF(MAX(IF(os.shes, 1, 0)) OVER (PARTITION BY os.outcome_id) = 1, SUM(os.ssu) OVER (PARTITION BY os.outcome_id), COALESCE(os.ast * NULLIF(SUM(os.sssh) OVER (PARTITION BY os.outcome_id), 0.0), NULLIF(SUM(os.ssu) OVER (PARTITION BY os.outcome_id), 0.0), 0.0))) * COALESCE(SAFE_DIVIDE(os.sc, SUM(os.sc) OVER (PARTITION BY os.outcome_id)), 0.0) AS inf_s
    FROM (
      SELECT COALESCE(ols.linked_outcome_id, w.outcome_id) AS outcome_id,
        COALESCE(o.outcome_owner, IF(o.outcome_id IS NOT NULL AND ols.linked_outcome_id IS NULL, w.fbo, NULL), w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS app_id,
        (LOGICAL_AND(COALESCE(o.is_closed, FALSE) AND COALESCE(o.outcome_status, 'unknown') = 'succeeded' AND COALESCE(o.trace_count, 0) > 0 AND o.root_error_trace_count = 0 AND o.failed_trace_count = 0 AND o.rootless_trace_count = 0 AND COALESCE(oe.eok, TRUE) AND w.r0_ok AND COALESCE(jo.s2_ok, TRUE)) AND COALESCE(LOGICAL_AND(IF(ols.linked_outcome_id IS NULL AND w.o1, jt.s2_ok, NULL)), TRUE)) AS g_ok,
        COUNT(*) AS sc, COUNTIF(w.span_cost_usd IS NULL) AS unp,
        SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN COALESCE(w.span_cost_usd, 0.0) * COALESCE(ols.lsh, 1.0) ELSE 0.0 END) AS m_u,
        SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN COALESCE(w.span_cost_usd, 0.0) * COALESCE(ols.lsh, 1.0) ELSE 0.0 END) AS sj_u,
        COALESCE(MAX(jo.ej_u), 0.0) AS ejo_u, SUM(IF(ols.linked_outcome_id IS NULL AND w.o1 AND w.trn = 1, COALESCE(jt.ej_u, 0.0), 0.0)) AS ejt_u,
        MAX(wt.storage_usd_per_outcome) AS ast,
        SUM(IF(NOT w.icp, (IF(w.ok IN ('SERVER', 'INTERNAL') AND w.srn = 1, (w.latency_ms / 1000.0) * 0.000053, 0.0) + (GREATEST(0.0, COALESCE(w.sandbox_cpu_sec, 0.0)) * 0.000024) + (GREATEST(0.0, COALESCE(w.sandbox_mem_gb_sec, 0.0)) * 0.0000025)) * COALESCE(ols.lsh, 1.0), 0.0)) AS scu,
        SUM(IF(NOT w.icp AND w.ok IN ('SERVER', 'INTERNAL') AND w.orn = 1, COALESCE(w.pest, 0.000020) * COALESCE(ols.lsh, 1.0), 0.0)) AS ssu,
        SUM(IF(NOT w.icp AND w.ok IN ('SERVER', 'INTERNAL') AND w.orn = 1, COALESCE(ols.lsh, 1.0), 0.0)) AS sssh,
        LOGICAL_OR(NOT w.icp AND w.ok IN ('SERVER', 'INTERNAL') AND w.orn = 1 AND w.pest IS NOT NULL) AS shes
      FROM (
        SELECT w0.*,
          ROW_NUMBER() OVER (PARTITION BY w0.trace_id, COALESCE(w0.service_name, 'agent-service') ORDER BY CASE WHEN w0.ok = 'SERVER' THEN 0 WHEN w0.ok = 'INTERNAL' THEN 1 ELSE 2 END, IF(w0.parent_span_id IS NULL, 0, 1), w0.start_time ASC, w0.span_id ASC) AS srn,
          ROW_NUMBER() OVER (PARTITION BY w0.outcome_id ORDER BY IF(NOT w0.icp AND w0.ok IN ('SERVER', 'INTERNAL'), 0, 1), w0.start_time ASC, w0.span_id ASC) AS orn,
          ROW_NUMBER() OVER (PARTITION BY w0.outcome_id, w0.trace_id ORDER BY w0.start_time ASC, w0.span_id ASC) AS trn,
          MAX(w0.apphub_application_id) OVER (PARTITION BY w0.outcome_id) AS fbo,
          (MIN(w0.outcome_id) OVER (PARTITION BY w0.trace_id) = MAX(w0.outcome_id) OVER (PARTITION BY w0.trace_id)) AS o1,
          (MIN(IF(w0.rule0_pass, 1, 0)) OVER (PARTITION BY w0.trace_id) = 1 AND COUNTIF(w0.span_kind IN ('execute_tool', 'retrieval')) OVER (PARTITION BY w0.trace_id) <= COALESCE(w0.mtc, 8)) AS r0_ok,
          MAX(CASE WHEN NOT w0.icp AND w0.ok IN ('SERVER', 'INTERNAL') AND w0.pst >= 0.0 THEN w0.pst END) OVER (PARTITION BY w0.outcome_id) AS pest
        FROM (
          SELECT wf.*, wp.storage_usd_per_outcome AS pst, COALESCE(wp.is_external_counterparty, FALSE) AS icp, wtr.max_tool_calls AS mtc,
            CASE WHEN UPPER(TRIM(COALESCE(wf.otel_span_kind, 'INTERNAL'))) IN ('1', 'INTERNAL', 'SPAN_KIND_INTERNAL') THEN 'INTERNAL' WHEN UPPER(TRIM(COALESCE(wf.otel_span_kind, 'INTERNAL'))) IN ('2', 'SERVER', 'SPAN_KIND_SERVER') THEN 'SERVER' ELSE 'OTHER' END AS ok
          FROM `agent_eval.v_trace_waterfall` AS wf
          LEFT JOIN `agent_eval.workload_thresholds` AS wp ON COALESCE(wf.outcome_owner, wf.apphub_application_id) = wp.apphub_application_id
          LEFT JOIN `agent_eval.workload_thresholds` AS wtr ON wf.apphub_application_id = wtr.apphub_application_id
          WHERE wf.outcome_id IS NOT NULL
        ) AS w0
      ) AS w
      LEFT JOIN (SELECT primary_outcome_id, linked_outcome_id, SAFE_DIVIDE(1.0, COUNT(*) OVER (PARTITION BY primary_outcome_id)) AS lsh FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcome_links`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())) AS ols ON w.outcome_id = ols.primary_outcome_id
      LEFT JOIN ro AS o ON COALESCE(ols.linked_outcome_id, w.outcome_id) = o.outcome_id LEFT JOIN oe ON COALESCE(ols.linked_outcome_id, w.outcome_id) = oe.outcome_id
      LEFT JOIN jo ON COALESCE(ols.linked_outcome_id, w.outcome_id) = jo.outcome_id LEFT JOIN jt ON ols.linked_outcome_id IS NULL AND w.trace_id = jt.trace_id
      LEFT JOIN `agent_eval.workload_thresholds` AS wt ON COALESCE(o.outcome_owner, IF(o.outcome_id IS NOT NULL AND ols.linked_outcome_id IS NULL, w.fbo, NULL), w.outcome_owner, w.apphub_application_id, 'unregistered_app') = wt.apphub_application_id
      WHERE NOT COALESCE(wt.is_external_counterparty, FALSE) GROUP BY 1, 2
    ) AS os
  ) GROUP BY 1
),
cb AS (
  SELECT jm.*,
    COALESCE(jm.eff_rt, ar.risk_tier, 'RL-1') AS risk_tier, IF(COALESCE(jm.eff_rt, ar.risk_tier, 'RL-1') = 'RL-1', 100, 10) AS min_req,
    CASE COALESCE(jm.eff_rt, ar.risk_tier, 'RL-1') WHEN 'RL-1' THEN 0.85 WHEN 'RL-2' THEN 0.85 WHEN 'RL-4' THEN 0.75 WHEN 'RL-5' THEN 0.75 ELSE 0.80 END AS req_ac2,
    IF(jm.jmr_id IS NULL OR jm.incs OR NOT ((jm.hp_c = 0 OR jm.hok) AND (jm.tp_c = 0 OR jm.tok)), CAST(NULL AS FLOAT64),
      COALESCE(jm.wh * LEAST(1.0, GREATEST(0.0, SAFE_DIVIDE(jm.h_pr + jm.htnr - 1.0, jm.htpr + jm.htnr - 1.0))), 0.0)
      + COALESCE(jm.wt * LEAST(1.0, GREATEST(0.0, SAFE_DIVIDE(jm.t_pr + jm.ttnr - 1.0, jm.ttpr + jm.ttnr - 1.0))), 0.0)) AS st_th,
    IF(jm.jmr_id IS NULL OR jm.incs, CAST(NULL AS FLOAT64), COALESCE(jm.wh * jm.h_pr, 0.0) + COALESCE(jm.wt * jm.t_pr, 0.0)) AS rj_pr,
    IF(jm.jmr_id IS NULL OR jm.incs, CAST(NULL AS FLOAT64),
      COALESCE(POW(jm.wh, 2) * SAFE_DIVIDE(jm.h_pr * (1.0 - jm.h_pr), NULLIF(jm.hj_c, 0)), 0.0)
      + COALESCE(POW(jm.wt, 2) * SAFE_DIVIDE(jm.t_pr * (1.0 - jm.t_pr), NULLIF(jm.tj_c, 0)), 0.0)) AS v_po,
    IF(jm.jmr_id IS NULL OR jm.incs, CAST(NULL AS FLOAT64),
      SAFE_DIVIDE(1.0, NULLIF(COALESCE(SAFE_DIVIDE(POW(jm.wh, 2), NULLIF(jm.hj_c, 0)), 0.0) + COALESCE(SAFE_DIVIDE(POW(jm.wt, 2), NULLIF(jm.tj_c, 0)), 0.0), 0.0))) AS ke_n,
    sc.tp, sc.fn, sc.tn, sc.fp, sc.tpr, sc.tnr, COALESCE(sc.opairs, 0) AS opairs, COALESCE(sc.dpairs, 0) AS dpairs,
    sc.gac2, sc.sac2, sc.bca, (sc.tpr + sc.tnr - 1.0) AS yj,
    ((ar.validation_expiry_date IS NOT NULL AND ar.validation_expiry_date < CURRENT_DATE('UTC')) OR (sc.mexp IS NOT NULL AND sc.mexp < CURRENT_DATE('UTC')) OR (sc.ssn IS NOT NULL AND jm.jsn IS NOT NULL AND sc.ssn != jm.jsn) OR (sc.sps IS NOT NULL AND jm.jps IS NOT NULL AND sc.sps != jm.jps) OR (sc.srv IS NOT NULL AND jm.jrv IS NOT NULL AND sc.srv != jm.jrv)) AS is_st,
    CAST(sc.tp + sc.fn + 2 AS FLOAT64) AS n_se, CAST(sc.tn + sc.fp + 2 AS FLOAT64) AS n_sp,
    SAFE_DIVIDE(CAST(sc.tp + 1 AS FLOAT64), CAST(sc.tp + sc.fn + 2 AS FLOAT64)) AS se_a, SAFE_DIVIDE(CAST(sc.tn + 1 AS FLOAT64), CAST(sc.tn + sc.fp + 2 AS FLOAT64)) AS sp_a,
    IF(jm.saj_u IS NULL, CAST(NULL AS FLOAT64), GREATEST(jm.saj_u, COALESCE(ajs.aej_u, 0.0))) AS aj_u,
    COALESCE(qc.bq_query_cost_usd, 0.0) AS bq_u, COALESCE(ao.otc, 0) AS otc, COALESCE(ao.ovc, 0) AS ovc,
    SAFE_DIVIDE(ao.ai_u, NULLIF(ao.ovc, 0)) AS ocpvo_ai,
    IF(ao.ai_inf_u IS NULL, CAST(NULL AS FLOAT64), SAFE_DIVIDE(ao.ai_inf_u + COALESCE(qc.bq_query_cost_usd, 0.0), NULLIF(ao.ovc, 0))) AS ocpvo_u
  FROM jmr AS jm
  LEFT JOIN aa AS ar ON jm.app_id = ar.app_id LEFT JOIN `agent_eval.v_bq_query_cost_attribution` AS qc ON jm.app_id = qc.apphub_application_id
  LEFT JOIN ajs ON jm.app_id = ajs.app_id LEFT JOIN scal AS sc ON jm.app_id = sc.app_id AND jm.jmr_id = sc.rubric_id LEFT JOIN ao ON jm.app_id = ao.app_id
),
wig AS (
  SELECT cb.*,
    IF(cb.gac2 IS NULL, CAST(NULL AS BOOL), cb.opairs >= cb.min_req AND cb.gac2 >= cb.req_ac2 AND COALESCE(cb.bca, 0.0) >= 0.85 AND (cb.dpairs = 0 OR (cb.dpairs >= cb.min_req AND COALESCE(cb.sac2, 0.0) >= cb.req_ac2))) AS pirr,
    CASE WHEN cb.v_po IS NOT NULL AND cb.v_po > 0.0 AND cb.rj_pr > 0.0 AND cb.rj_pr < 1.0 THEN GREATEST(1.0, SAFE_DIVIDE(cb.rj_pr * (1.0 - cb.rj_pr), cb.v_po)) WHEN cb.ke_n IS NOT NULL THEN GREATEST(1.0, cb.ke_n) ELSE CAST(cb.jsmp AS FLOAT64) END AS eff_n
  FROM cb
),
st AS (
  SELECT g.*,
    CASE
      WHEN g.msess > 0 THEN 'MISSING_SESSION_CONTEXT' WHEN g.jsmp = 0 THEN 'UNJUDGED' WHEN g.is_st THEN 'STALE_CALIBRATION'
      WHEN g.incs OR (g.hss AND g.st_th IS NULL) OR g.rj_pr IS NULL OR g.jsmp < g.min_req OR COALESCE(g.pirr, FALSE) IS FALSE OR g.tpr IS NULL OR g.tnr IS NULL OR (g.tp + g.fn) < 5 OR (g.tn + g.fp) < 5 OR g.yj <= 1e-6 OR (g.se_a + g.sp_a - 1.0) <= 1e-6 THEN 'UNCALIBRATED'
      ELSE 'CALIBRATED'
    END AS cal_st
  FROM wig AS g
),
ci AS (
  SELECT s.*,
    CASE WHEN s.cal_st != 'CALIBRATED' THEN NULL WHEN s.st_th IS NOT NULL THEN s.st_th ELSE LEAST(1.0, GREATEST(0.0, SAFE_DIVIDE(s.rj_pr + s.tnr - 1.0, s.tpr + s.tnr - 1.0))) END AS th_rg,
    (s.eff_n + 3.841458820694124) AS n_p, SAFE_DIVIDE(s.rj_pr * s.eff_n + 1.920729410347062, s.eff_n + 3.841458820694124) AS p_a, (s.se_a + s.sp_a - 1.0) AS j_a,
    IF(s.cal_st != 'CALIBRATED', NULL, LEAST(1.0, GREATEST(0.0, SAFE_DIVIDE(SAFE_DIVIDE(s.rj_pr * s.eff_n + 1.920729410347062, s.eff_n + 3.841458820694124) + s.sp_a - 1.0, s.se_a + s.sp_a - 1.0)))) AS th_a,
    (s.pi_u + s.ce_u + s.ag_u + s.aj_u + s.op_u + s.bq_u + s.un_u) AS tot_sp
  FROM st AS s
),
cif AS (
  SELECT l.*,
    IF(l.th_a IS NULL, NULL, 2.0 * 3.841458820694124 * (l.th_a * SAFE_DIVIDE(l.se_a * (1.0 - l.se_a), l.n_se) - (1.0 - l.th_a) * SAFE_DIVIDE(l.sp_a * (1.0 - l.sp_a), l.n_sp))) AS dps,
    IF(l.th_a IS NULL, NULL, SAFE_DIVIDE(SQRT(SAFE_DIVIDE(l.p_a * (1.0 - l.p_a), l.n_p) + POW(l.th_a, 2) * SAFE_DIVIDE(l.se_a * (1.0 - l.se_a), l.n_se) + POW(1.0 - l.th_a, 2) * SAFE_DIVIDE(l.sp_a * (1.0 - l.sp_a), l.n_sp)), l.j_a)) AS se_t
  FROM ci AS l
)
SELECT
  c.app_id AS apphub_application_id, c.rubric_id, c.risk_tier, c.req_ac2 AS required_ac2_gate,
  c.tot_t AS total_traces, c.prod_t AS production_traces, c.r0_pr AS rule0_pass_rate, c.ts_r AS tool_success_rate,
  c.p95_ms AS p95_latency_ms, c.chr AS avg_cache_hit_ratio, c.wbt AS total_weighted_burndown_tokens,
  c.jsmp AS judged_samples, c.cal_st AS calibration_status, c.gac2 AS gwet_ac2, c.sac2 AS sme_vs_sme_ac2,
  c.bca AS binary_cutpoint_agreement, c.pirr AS passes_irr_gate, c.rj_pr AS raw_judge_pass_rate, c.th_rg AS calibrated_pass_rate,
  IF(c.th_a IS NULL, NULL, LEAST(1.0, GREATEST(0.0, c.th_a + c.dps - 1.959963984540054 * c.se_t))) AS calibrated_ci_95_lower,
  IF(c.th_a IS NULL, NULL, LEAST(1.0, GREATEST(0.0, c.th_a + c.dps + 1.959963984540054 * c.se_t))) AS calibrated_ci_95_upper,
  c.unp AS unpriced_spans, c.pi_u AS production_inference_usd, c.ce_u AS conflict_escalation_usd,
  c.ag_u AS agent_gym_eval_usd, c.aj_u AS autorater_judge_usd, c.op_u AS optimize_usd, c.bq_u AS bq_query_cost_usd, c.un_u AS unclassified_usd,
  IF(c.unp > 0, CAST(NULL AS FLOAT64), c.tot_sp) AS total_spend_usd,
  CASE WHEN c.unp > 0 OR c.th_rg IS NULL OR c.th_rg <= 0.0 OR c.pr0_t = 0 THEN NULL ELSE SAFE_DIVIDE(c.tot_sp, c.pr0_t * c.th_rg) END AS cost_per_verified_outcome_usd,
  c.otc AS outcome_total_count, c.ovc AS outcome_verified_count,
  c.ocpvo_ai AS outcome_cost_per_verified_ai_usd, c.ocpvo_u AS outcome_cost_per_verified_usd
FROM cif AS c;
