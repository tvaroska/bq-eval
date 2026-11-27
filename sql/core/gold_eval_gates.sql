-- Gold Deterministic Evaluation Gates, WORM Audit Candidates, `AI.EVALUATE_AGENT` Turns & Optimizer Export (`ADR-048`, `ADR-049`).

CREATE OR REPLACE VIEW `agent_eval.v_orphan_committed_actions` AS
WITH cm AS (
  SELECT event_id, event_time, trace_id, outcome_id, trace_owner_app_id, action_id, committed_args_sha256
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.events`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  WHERE event_type = 'action.committed' AND is_authoritative IS TRUE
),
tc AS (
  SELECT trace_id, span_id, action_id, args_sha256
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.tool_calls`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
)
SELECT
  c.event_id, c.event_time, c.trace_id, c.outcome_id, COALESCE(c.trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
  c.action_id, c.committed_args_sha256, t.span_id AS matched_tool_span_id, t.args_sha256 AS matched_tool_args_sha256,
  CASE WHEN t.span_id IS NULL THEN 'MISSING_TOOL_SPAN' WHEN c.committed_args_sha256 IS NOT NULL AND COALESCE(t.args_sha256, '') != c.committed_args_sha256 THEN 'COMMITTED_ARGS_MISMATCH' ELSE 'OK' END AS orphan_status
FROM cm AS c
LEFT JOIN tc AS t ON c.action_id IS NOT NULL AND c.action_id = t.action_id AND (c.trace_id IS NULL OR c.trace_id = t.trace_id)
WHERE t.span_id IS NULL OR (c.committed_args_sha256 IS NOT NULL AND COALESCE(t.args_sha256, '') != c.committed_args_sha256);

CREATE OR REPLACE VIEW `agent_eval.v_trace_sql_evaluation` AS
WITH st AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.traces`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
),
stc AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.tool_calls`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
),
sev AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.events`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
),
ae AS (
  SELECT action_id, event_id, event_type, event_time, trace_id, approved_args_sha256, committed_args_sha256, approved_expires_at, actor_id,
    COUNT(*) OVER (PARTITION BY event_type, event_id) AS evc
  FROM sev WHERE is_authoritative IS TRUE AND action_id IS NOT NULL AND event_type IN ('outcome.approved', 'action.committed')
),
tgr AS (
  SELECT trace_id,
    COUNTIF(has_tc AND trn = 1 AND span_status = 'ERROR') AS te_c,
    COUNTIF(has_tc AND trn = 1 AND thttp = 429) AS t4_c,
    MAX(IF(has_tc, skill_version, NULL)) AS tsv,
    COUNTIF(has_tc AND trn = 1 AND COALESCE(executes_code, FALSE) AND (span_status != 'OK' OR COALESCE(sandbox_status, 'ok') != 'ok')) AS sb_c,
    COUNTIF(has_tc AND trn = 1 AND COALESCE(acl_dropped_count, 0) > 0 AND (results IS NULL OR TRIM(results) IN ('', '[]', '{}', 'null'))) AS ser_c,
    COUNTIF(has_tc AND trn = 1 AND COALESCE(writes_state, FALSE) AND NOT aok) AS uw_c,
    COUNTIF(iscm AND crn = 1 AND NOT cok) AS oc_c,
    MAX(IF(has_tc, args_sha256, NULL)) AS sa_s,
    MAX(IF(has_tc AND isap, approved_args_sha256, NULL)) AS saa_s
  FROM (
    SELECT j.*,
      ROW_NUMBER() OVER (PARTITION BY j.ttid, j.span_id ORDER BY IF(j.aok, 0, 1), j.event_id) AS trn,
      ROW_NUMBER() OVER (PARTITION BY j.event_id ORDER BY IF(j.cok, 0, 1), j.span_id) AS crn
    FROM (
      SELECT COALESCE(tc.trace_id, ae.trace_id) AS trace_id, tc.trace_id AS ttid, tc.span_id, ae.event_id,
        tc.span_id IS NOT NULL AS has_tc, tc.span_status, SAFE_CAST(JSON_VALUE(tc.labels, '$.tool_http_status_code') AS INT64) AS thttp,
        tc.skill_version, tc.executes_code, tc.sandbox_status, tc.acl_dropped_count, tc.results, tc.writes_state, tc.args_sha256,
        (ae.event_type = 'outcome.approved') AS isap, (ae.event_type = 'action.committed' AND ae.trace_id IS NOT NULL) AS iscm,
        ae.approved_args_sha256,
        (tc.action_id IS NOT NULL AND tc.args_sha256 IS NOT NULL AND tc.args_hash_source != 'none' AND ae.event_type = 'outcome.approved' AND ae.evc = 1 AND ae.approved_args_sha256 = tc.args_sha256 AND ae.event_time < tc.start_time AND (ae.approved_expires_at IS NULL OR tc.start_time <= ae.approved_expires_at) AND ae.actor_id IS NOT NULL AND ae.actor_id != COALESCE(tc.user_id, '')) AS aok,
        (tc.span_id IS NOT NULL AND (ae.trace_id IS NULL OR ae.trace_id = tc.trace_id) AND (ae.committed_args_sha256 IS NULL OR COALESCE(tc.args_sha256, '') = ae.committed_args_sha256)) AS cok
      FROM stc AS tc
      FULL OUTER JOIN ae ON tc.action_id IS NOT NULL AND tc.action_id = ae.action_id AND (ae.event_type = 'outcome.approved' OR ae.trace_id IS NULL OR ae.trace_id = tc.trace_id)
      WHERE COALESCE(tc.trace_id, ae.trace_id) IS NOT NULL
    ) AS j
  ) GROUP BY trace_id
),
gr AS (
  SELECT trace_id, COUNTIF(event_type = 'guardrail.intervention' AND source_class = 'platform' AND control_action = 'block') AS pgb
  FROM sev WHERE trace_id IS NOT NULL GROUP BY trace_id
),
outs AS (
  SELECT outcome_id, outcome_owner
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcomes_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY outcome_id ORDER BY close_time DESC) = 1
),
wwa AS (
  SELECT w.*, ama.hr, am.is_allowed, am.invokable_status, am.eol_date,
    ROW_NUMBER() OVER (
      PARTITION BY w.trace_id, w.span_id
      ORDER BY IF(am.app_id = COALESCE(w.trace_owner_app_id, 'unregistered_app'), 0, 1) ASC,
        IF(COALESCE(am.is_allowed AND am.invokable_status = 'active' AND (am.eol_date IS NULL OR am.eol_date >= CURRENT_DATE('UTC')), FALSE), 0, 1) ASC,
        am.effective_from DESC
    ) AS arn
  FROM `agent_eval.gold_call_costs`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) AS w
  CROSS JOIN (SELECT COUNT(*) > 0 AS hr FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.allowed_models` WHERE effective_to IS NULL) AS ama
  LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.allowed_models` AS am
    ON w.model_name IS NOT NULL AND w.model_name != 'none' AND am.effective_to IS NULL
    AND IF(STARTS_WITH(REGEXP_REPLACE(w.model_name, r'^.*/', ''), 'text-embedding-'), REGEXP_REPLACE(w.model_name, r'^.*/', ''), REGEXP_REPLACE(REGEXP_REPLACE(w.model_name, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '')) = am.model
    AND (am.app_id = '*' OR am.app_id = COALESCE(w.trace_owner_app_id, 'unregistered_app'))
),
wfr AS (
  SELECT w.trace_id,
    MAX(JSON_VALUE(w.labels, '$.source_mode')) AS sm, MAX(w.skill_version) AS sv,
    COUNT(*) AS wsc, COUNTIF(w.http_status_code = 429) AS h429,
    COUNTIF(w.model_name IS NOT NULL AND w.model_name != 'none' AND w.hr AND NOT COALESCE(w.is_allowed AND w.invokable_status = 'active' AND (w.eol_date IS NULL OR w.eol_date >= CURRENT_DATE('UTC')), FALSE)) AS dmc,
    COUNTIF(UPPER(COALESCE(w.serving_traffic_type, 'STANDARD')) IN ('PRIORITY', 'STANDARD', 'FLEX') AND w.model_name != 'none') AS ssc,
    LOGICAL_OR(w.is_long_context_cliff) AS cliff, LOGICAL_OR(w.finops_traffic_type = 'conflict_escalation') AS esct,
    LOGICAL_AND(w.meets_min_cache_floor) AS mcf, MAX(w.min_cache_tokens) AS mct,
    MIN(CASE w.finops_traffic_type WHEN 'production_inference' THEN 1 WHEN 'conflict_escalation' THEN 2 WHEN 'agent_gym_eval' THEN 3 WHEN 'autorater_judge' THEN 4 WHEN 'optimize' THEN 5 ELSE 6 END) AS mtp,
    MAX(w.finops_traffic_type) AS mft,
    SUM(w.input_tokens) AS in_t, SUM(w.cached_input_tokens) AS ca_t, SUM(w.uncached_input_tokens) AS uc_t,
    SUM(w.output_tokens) AS ou_t, SUM(w.thinking_tokens) AS th_t, SUM(w.weighted_burndown_tokens) AS wb_t,
    IF(COUNTIF(w.finops_traffic_type = 'production_inference' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'production_inference' THEN w.span_cost_usd ELSE 0.0 END)) AS pi_u,
    IF(COUNTIF(w.finops_traffic_type = 'conflict_escalation' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'conflict_escalation' THEN w.span_cost_usd ELSE 0.0 END)) AS ce_u,
    IF(COUNTIF(w.finops_traffic_type = 'agent_gym_eval' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'agent_gym_eval' THEN w.span_cost_usd ELSE 0.0 END)) AS ag_u,
    IF(COUNTIF(w.finops_traffic_type = 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)) AS aj_u,
    IF(COUNTIF(w.finops_traffic_type = 'optimize' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'optimize' THEN w.span_cost_usd ELSE 0.0 END)) AS op_u,
    IF(COUNTIF(w.finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge', 'optimize') AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge', 'optimize') THEN w.span_cost_usd ELSE 0.0 END)) AS un_u,
    COUNTIF(w.span_cost_usd IS NULL) AS unp,
    IF(COUNTIF(w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(w.span_cost_usd)) AS tsc_u
  FROM wwa AS w WHERE w.arn = 1 GROUP BY w.trace_id
),
aa AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` WHERE effective_to IS NULL QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY effective_from DESC) = 1
),
ask AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.skill_registry` WHERE effective_to IS NULL QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id, skill_name ORDER BY effective_from DESC) = 1
),
aal AS (
  SELECT app_id, MAX(cost_center) AS cost_center, SUM(funded_units) AS funded_gsus
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_allocations` WHERE effective_to IS NULL AND app_id IS NOT NULL GROUP BY app_id
),
be AS (
  SELECT
    t.trace_id, COALESCE(t.trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
    t.app_identity_bound, t.unregistered_app,
    IF(t.unregistered_app OR COALESCE(t.app_identity_bound, FALSE) IS FALSE, 'RL-1', COALESCE(t.risk_tier, ar.risk_tier, 'RL-3b')) AS eff_rt,
    COALESCE(t.risk_tier, ar.risk_tier, 'RL-3b') AS risk_tier,
    t.outcome_id, t.multiple_outcome_ids, COALESCE(outs.outcome_owner, t.trace_owner_app_id) AS outcome_owner,
    COALESCE(wf.sm, JSON_VALUE(t.labels, '$.workflow_name')) AS source_mode,
    t.skill_name AS skill_id, COALESCE(wf.sv, tgr.tsv) AS skill_version,
    t.skill_sha256, t.protected_block_sha256, t.mounted_skills, t.tool_definitions_sha256,
    t.conversation_id, t.previous_interaction_id, t.case_id, t.eval_run_id, t.eval_split, t.shadow_of_trace_id, t.experiment_id, t.variant_id,
    COALESCE(t.platform_user_id, t.user_id) AS resolved_requester_id,
    (t.platform_user_id IS NOT NULL OR COALESCE(t.identity_attested, FALSE)) AS has_attested_requester,
    ((t.platform_user_id IS NOT NULL AND t.user_id IS NOT NULL AND t.platform_user_id != t.user_id) OR t.user_id_conflict_count > 0) AS identity_spoof_detected,
    COALESCE(t.business_unit, ar.business_unit, 'unassigned_bu') AS business_unit,
    COALESCE(wt.sla_latency_ms, 5000.0) AS sla_latency_ms,
    COALESCE(ca.cost_center, wt.cost_center, 'UNASSIGNED') AS cost_center,
    COALESCE(ca.funded_gsus, wt.funded_gsus, 0.0) AS funded_gsus,
    t.trace_start_time, t.trace_end_time, t.last_span_arrival_time AS trace_receive_time,
    CAST(GREATEST(0, TIMESTAMP_DIFF(t.trace_end_time, t.trace_start_time, MILLISECOND)) AS FLOAT64) AS trace_latency_ms,
    TIMESTAMP_DIFF(t.last_span_arrival_time, t.trace_end_time, MILLISECOND) AS ingestion_lag_ms,
    t.span_count, t.model_call_count, t.tool_call_count,
    COALESCE(t.repeated_tool_call_count, 0) AS repeated_tool_call_count,
    COALESCE(tgr.te_c, 0) AS tool_error_count,
    (COALESCE(wf.h429, 0) + COALESCE(tgr.t4_c, 0) + IF(COALESCE(t.throttle_count, 0) > 0 AND COALESCE(wf.h429, 0) + COALESCE(tgr.t4_c, 0) = 0, 1, 0)) AS http_429_count,
    COALESCE(wf.ssc, 0) AS spillover_span_count, t.content_missing_call_count,
    COALESCE(gr.pgb, 0) AS platform_guardrail_blocks,
    COALESCE(tgr.ser_c, 0) AS silent_empty_rag_count, COALESCE(tgr.uw_c, 0) AS unauthorized_write_count, COALESCE(tgr.oc_c, 0) AS orphan_commit_count,
    tgr.sa_s AS sample_args_sha256, tgr.saa_s AS sample_approved_args_sha256,
    JSON_VALUE(t.labels, '$.agent_version') AS agent_version,
    (NOT ((t.platform_user_id IS NOT NULL AND t.user_id IS NOT NULL AND t.platform_user_id != t.user_id) OR t.user_id_conflict_count > 0)
      AND NOT (IF(t.unregistered_app OR COALESCE(t.app_identity_bound, FALSE) IS FALSE, 'RL-1', COALESCE(t.risk_tier, ar.risk_tier, 'RL-3b')) IN ('RL-1', 'RL-2', 'RL-3', 'RL-3a', 'RL-3b')
        AND (t.user_id IS NOT NULL OR COALESCE(ar.payload_profile, '') = 'regulated_worm' OR t.mutating_tool_call_count > 0)
        AND t.platform_user_id IS NULL AND NOT COALESCE(t.identity_attested, FALSE))) AS id_ok,
    (NOT t.unregistered_skill AND (sk.protected_block_sha256 IS NULL OR COALESCE(t.protected_block_sha256, '') = sk.protected_block_sha256) AND (sk.skill_sha256 IS NULL OR t.skill_sha256 IS NULL OR t.skill_sha256 = sk.skill_sha256)) AS sk_ok,
    (t.is_turn AND t.has_root_span AND COALESCE(t.root_status, 'OK') != 'ERROR' AND NOT t.app_failed AND t.error_count = 0
      AND COALESCE(tgr.te_c, 0) = 0 AND t.tool_call_count <= COALESCE(wt.max_tool_calls, 8)
      AND COALESCE(t.repeated_tool_call_count, 0) <= COALESCE(wt.max_repeated_tool_calls, 0)
      AND COALESCE(tgr.ser_c, 0) = 0 AND COALESCE(tgr.sb_c, 0) = 0 AND COALESCE(tgr.uw_c, 0) = 0 AND COALESCE(tgr.oc_c, 0) = 0
      AND COALESCE(wf.dmc, 0) = 0 AND NOT t.unregistered_skill
      AND (sk.protected_block_sha256 IS NULL OR COALESCE(t.protected_block_sha256, '') = sk.protected_block_sha256)) AS r0_ok,
    (t.escalated OR COALESCE(wf.esct, FALSE)) AS esc_high,
    (COALESCE(wf.h429, 0) + COALESCE(tgr.t4_c, 0) + COALESCE(t.throttle_count, 0)) > 0 AS any_429,
    COALESCE(wf.cliff, FALSE) AS hit_long_context_cliff, COALESCE(wf.mcf, TRUE) AS meets_min_cache_floor, COALESCE(wf.mct, 0) AS min_cache_tokens,
    CASE LEAST(COALESCE(wf.mtp, 99), IF(t.span_count > COALESCE(wf.wsc, 0) OR COALESCE(wf.wsc, 0) = 0, CASE COALESCE(t.traffic_type, 'production_inference') WHEN 'production_inference' THEN 1 WHEN 'conflict_escalation' THEN 2 WHEN 'agent_gym_eval' THEN 3 WHEN 'autorater_judge' THEN 4 WHEN 'optimize' THEN 5 ELSE 6 END, 99))
      WHEN 1 THEN 'production_inference' WHEN 2 THEN 'conflict_escalation' WHEN 3 THEN 'agent_gym_eval' WHEN 4 THEN 'autorater_judge' WHEN 5 THEN 'optimize'
      ELSE COALESCE(wf.mft, t.traffic_type, 'production_inference')
    END AS primary_traffic_type,
    COALESCE(wf.in_t, 0) AS total_input_tokens, COALESCE(wf.ca_t, 0) AS total_cached_input_tokens, COALESCE(wf.uc_t, 0) AS total_uncached_input_tokens,
    COALESCE(wf.ou_t, 0) AS total_output_tokens, COALESCE(wf.th_t, 0) AS total_thinking_tokens, COALESCE(wf.wb_t, 0.0) AS weighted_burndown_tokens,
    COALESCE(wf.pi_u, IF(COALESCE(wf.unp, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS production_inference_usd,
    COALESCE(wf.ce_u, IF(COALESCE(wf.unp, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS conflict_escalation_usd,
    COALESCE(wf.ag_u, IF(COALESCE(wf.unp, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS agent_gym_eval_usd,
    COALESCE(wf.aj_u, IF(COALESCE(wf.unp, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS span_autorater_judge_usd,
    COALESCE(wf.op_u, IF(COALESCE(wf.unp, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS optimize_usd,
    COALESCE(wf.un_u, IF(COALESCE(wf.unp, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS unclassified_usd,
    COALESCE(wf.unp, 0) AS unpriced_spans, wf.tsc_u AS trace_span_cost_usd,
    COALESCE(ar.sampling_mode, 'full') AS sampling_mode, t.sampling_probability,
    CASE WHEN COALESCE(ar.sampling_mode, 'full') = 'full' THEN 1.0 WHEN t.sampling_probability IS NOT NULL AND t.sampling_probability > 0.0 THEN SAFE_DIVIDE(1.0, t.sampling_probability) ELSE CAST(NULL AS FLOAT64) END AS scaled_trace_weight,
    CASE WHEN COALESCE(wf.unp, 0) > 0 THEN CAST(NULL AS FLOAT64) WHEN COALESCE(ar.sampling_mode, 'full') = 'full' THEN wf.tsc_u WHEN t.sampling_probability IS NOT NULL AND t.sampling_probability > 0.0 THEN SAFE_DIVIDE(wf.tsc_u, t.sampling_probability) ELSE CAST(NULL AS FLOAT64) END AS scaled_trace_span_cost_usd,
    SAFE_DIVIDE(COALESCE(wf.ca_t, 0), NULLIF(COALESCE(wf.in_t, 0), 0)) AS cache_hit_ratio,
    SAFE_DIVIDE(COALESCE(wf.th_t, 0), NULLIF(COALESCE(wf.ou_t, 0) + COALESCE(wf.th_t, 0), 0)) AS thinking_token_ratio
  FROM st AS t
  LEFT JOIN outs ON t.outcome_id = outs.outcome_id
  LEFT JOIN wfr AS wf ON t.trace_id = wf.trace_id
  LEFT JOIN tgr ON t.trace_id = tgr.trace_id
  LEFT JOIN gr ON t.trace_id = gr.trace_id
  LEFT JOIN aa AS ar ON t.trace_owner_app_id = ar.app_id
  LEFT JOIN ask AS sk ON t.trace_owner_app_id = sk.app_id AND t.skill_name = sk.skill_name
  LEFT JOIN `agent_eval.workload_thresholds` AS wt ON COALESCE(t.trace_owner_app_id, 'unregistered_app') = wt.apphub_application_id
  LEFT JOIN aal AS ca ON COALESCE(t.trace_owner_app_id, 'unregistered_app') = ca.app_id
)
SELECT
  b.trace_id, b.apphub_application_id, b.app_identity_bound, b.unregistered_app,
  b.eff_rt AS effective_risk_tier, b.risk_tier, b.outcome_id, b.multiple_outcome_ids, b.outcome_owner,
  b.source_mode, b.skill_id, b.skill_version, b.skill_sha256, b.protected_block_sha256, b.mounted_skills, b.tool_definitions_sha256,
  b.conversation_id, b.previous_interaction_id, b.case_id, b.eval_run_id, b.eval_split, b.shadow_of_trace_id, b.experiment_id, b.variant_id,
  b.resolved_requester_id, b.has_attested_requester, b.identity_spoof_detected, b.business_unit, b.sla_latency_ms, b.cost_center, b.funded_gsus,
  b.trace_start_time, b.trace_end_time, b.trace_receive_time, b.trace_latency_ms, b.ingestion_lag_ms,
  b.span_count, b.model_call_count, b.tool_call_count, b.repeated_tool_call_count, b.tool_error_count, b.http_429_count, b.spillover_span_count,
  b.content_missing_call_count, b.platform_guardrail_blocks, b.silent_empty_rag_count, b.unauthorized_write_count, b.orphan_commit_count,
  b.sample_args_sha256, b.sample_approved_args_sha256, b.agent_version,
  b.id_ok AS identity_gate_pass, b.sk_ok AS skill_gate_pass, (b.r0_ok AND b.id_ok) AS rule0_pass,
  b.esc_high AS escalated_to_high_reasoning,
  (b.tool_error_count > 0 OR b.any_429 OR b.esc_high OR b.repeated_tool_call_count > 0 OR b.silent_empty_rag_count > 0) AS is_loop_or_error_tail,
  (NOT (b.tool_error_count > 0 OR b.any_429 OR b.esc_high OR b.repeated_tool_call_count > 0 OR b.silent_empty_rag_count > 0) AND b.trace_latency_ms > b.sla_latency_ms) AS is_healthy_slow_tail,
  CASE WHEN b.tool_error_count > 0 THEN 'TOOL_ERROR' WHEN b.any_429 THEN 'HTTP_429' WHEN b.silent_empty_rag_count > 0 THEN 'SILENT_EMPTY_RAG' WHEN b.repeated_tool_call_count > 0 THEN 'REPEATED_TOOL_LOOP' WHEN b.esc_high THEN 'ESCALATED' WHEN b.trace_latency_ms > b.sla_latency_ms THEN 'HEALTHY_SLOW' ELSE 'NONE' END AS tail_root_cause,
  CASE WHEN (b.tool_error_count > 0 OR b.any_429 OR b.esc_high OR b.repeated_tool_call_count > 0 OR b.silent_empty_rag_count > 0) THEN 'LOOP_OR_ERROR_TAIL' WHEN b.trace_latency_ms > b.sla_latency_ms THEN 'HEALTHY_SLOW' ELSE 'HEALTHY' END AS tail_class,
  b.hit_long_context_cliff, b.meets_min_cache_floor, b.min_cache_tokens, b.primary_traffic_type,
  b.total_input_tokens, b.total_cached_input_tokens, b.total_uncached_input_tokens, b.total_output_tokens, b.total_thinking_tokens, b.weighted_burndown_tokens,
  b.production_inference_usd, b.conflict_escalation_usd, b.agent_gym_eval_usd, b.span_autorater_judge_usd, b.optimize_usd, b.unclassified_usd,
  b.unpriced_spans, b.trace_span_cost_usd, b.sampling_mode, b.sampling_probability, b.scaled_trace_weight, b.scaled_trace_span_cost_usd,
  b.cache_hit_ratio, b.thinking_token_ratio
FROM be AS b;

CREATE OR REPLACE VIEW `agent_eval.v_step1_trace_eval` AS
SELECT
  trace_id, apphub_application_id, outcome_id, outcome_owner, source_mode, skill_id, skill_version, business_unit, cost_center,
  trace_start_time, trace_end_time, trace_receive_time, trace_latency_ms, ingestion_lag_ms,
  span_count, tool_call_count, tool_error_count, content_missing_call_count, rule0_pass AS step1_pass, hit_long_context_cliff,
  total_input_tokens, total_cached_input_tokens, total_uncached_input_tokens, total_output_tokens, total_thinking_tokens,
  cache_hit_ratio, weighted_burndown_tokens, unpriced_spans, trace_span_cost_usd AS trace_cost_usd
FROM `agent_eval.v_trace_sql_evaluation`;

CREATE OR REPLACE VIEW `agent_eval.v_eval_agent_turns` AS
SELECT
  t.trace_id, t.outcome_id, t.apphub_application_id, t.business_unit, t.trace_start_time, t.trace_latency_ms,
  t.rule0_pass AS step1_pass, t.trace_span_cost_usd AS trace_cost_usd,
  JSON_OBJECT('trace_id', t.trace_id, 'turn_prompt', p.turn_prompt, 'turn_context', p.turn_context, 'turn_output', p.turn_output, 'step_outputs', p.step_outputs) AS trace
FROM `agent_eval.v_trace_sql_evaluation` AS t
INNER JOIN `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.trace_payloads`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) AS p
  ON t.trace_id = p.trace_id
WHERE t.content_missing_call_count = 0 AND COALESCE(p.turn_prompt_read_whole, TRUE) AND COALESCE(p.turn_context_read_whole, TRUE) AND COALESCE(p.turn_output_read_whole, TRUE) AND COALESCE(p.step_outputs_read_whole, TRUE);

CREATE OR REPLACE VIEW `agent_eval.v_gold_worm_snapshot_candidates` AS
SELECT
  CONCAT('worm:', t.trace_id) AS snapshot_id, CURRENT_TIMESTAMP() AS snapshot_time,
  t.trace_id, t.outcome_id, 'v0.7.0' AS silver_sql_version, t.agent_version, t.skill_sha256, t.protected_block_sha256,
  JSON_OBJECT('apphub_application_id', t.apphub_application_id, 'effective_risk_tier', t.effective_risk_tier, 'business_unit', t.business_unit, 'sampling_mode', t.sampling_mode) AS reference_table_snapshot_json,
  JSON_OBJECT('rule0_pass', IF(t.rule0_pass, TRUE, FALSE), 'identity_gate_pass', IF(t.identity_gate_pass, TRUE, FALSE), 'skill_gate_pass', IF(t.skill_gate_pass, TRUE, FALSE), 'unauthorized_write_count', t.unauthorized_write_count, 'orphan_commit_count', t.orphan_commit_count) AS policy_verdicts_json,
  t.sample_args_sha256 AS args_sha256, t.sample_approved_args_sha256 AS approved_args_sha256,
  TO_HEX(SHA256(CONCAT(t.trace_id, '|', COALESCE(t.outcome_id, ''), '|', COALESCE(t.skill_sha256, ''), '|', COALESCE(t.protected_block_sha256, ''), '|', IF(t.rule0_pass, 'true', 'false'), '|', COALESCE(t.sample_args_sha256, ''), '|', COALESCE(t.sample_approved_args_sha256, '')))) AS snapshot_sha256
FROM `agent_eval.v_trace_sql_evaluation` AS t;

CREATE OR REPLACE VIEW `agent_eval.v_optimizer_gold_export` AS
WITH nr AS (
  SELECT trace_id, ARRAY_AGG(nudge_id IGNORE NULLS ORDER BY event_time ASC LIMIT 1)[SAFE_OFFSET(0)] AS nudge_id
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.events_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  WHERE trace_id IS NOT NULL AND nudge_id IS NOT NULL GROUP BY trace_id
),
te AS (
  SELECT
    COALESCE(t.eval_run_id, 'default_run') AS eval_run_id,
    COALESCE(t.variant_id, t.skill_sha256, t.apphub_application_id) AS candidate_id,
    CASE WHEN t.primary_traffic_type = 'optimize' THEN 'OFFLINE_SWEEP' WHEN t.shadow_of_trace_id IS NOT NULL OR t.experiment_id IS NOT NULL THEN 'LIVE_SHADOW_AB' ELSE 'CI_ONLY' END AS optimizer_mode,
    t.apphub_application_id, t.trace_id, t.skill_sha256, t.protected_block_sha256, t.skill_gate_pass,
    NULLIF(TO_JSON_STRING(t.mounted_skills), 'null') AS ms_json, COALESCE(t.eval_split, 'unspecified') AS eval_split,
    t.rule0_pass, t.min_cache_tokens, t.meets_min_cache_floor,
    MIN(CASE WHEN t.case_id IS NOT NULL AND t.eval_split IN ('train', 'holdout') THEN t.eval_split END) OVER (PARTITION BY COALESCE(t.eval_run_id, 'default_run'), t.case_id) AS min_sp,
    MAX(CASE WHEN t.case_id IS NOT NULL AND t.eval_split IN ('train', 'holdout') THEN t.eval_split END) OVER (PARTITION BY COALESCE(t.eval_run_id, 'default_run'), t.case_id) AS max_sp
  FROM `agent_eval.v_trace_sql_evaluation` AS t
),
cr AS (
  SELECT te.eval_run_id, te.candidate_id, te.optimizer_mode, te.apphub_application_id,
    MAX(te.skill_sha256) AS skill_sha256, MAX(te.protected_block_sha256) AS protected_block_sha256,
    LOGICAL_AND(te.skill_gate_pass) AS protected_block_verified, ANY_VALUE(te.ms_json) AS ms_json,
    te.eval_split, MAX(nr.nudge_id) AS nudge_id, AVG(IF(te.rule0_pass, 1.0, 0.0)) AS ci_only_pareto_score,
    MAX(te.min_cache_tokens) AS min_cache_tokens, LOGICAL_AND(te.meets_min_cache_floor) AS passes_prefix_cache_floor,
    COUNTIF(te.min_sp IS NOT NULL AND te.min_sp != te.max_sp) AS ov_rows
  FROM te LEFT JOIN nr ON te.trace_id = nr.trace_id GROUP BY 1, 2, 3, 4, 9
)
SELECT
  cr.eval_run_id, cr.candidate_id, cr.optimizer_mode, cr.apphub_application_id, cr.skill_sha256, cr.protected_block_sha256,
  cr.protected_block_verified, SAFE.PARSE_JSON(cr.ms_json) AS mounted_skills, cr.eval_split,
  SUM(cr.ov_rows) OVER (PARTITION BY cr.eval_run_id) = 0 AS split_disjoint_verified,
  cr.nudge_id, cr.ci_only_pareto_score, CAST(NULL AS FLOAT64) AS calibrated_holdout_pass_rate, cr.min_cache_tokens, cr.passes_prefix_cache_floor
FROM cr;

CREATE OR REPLACE VIEW `agent_eval.v_outcomes` AS
WITH ro AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcomes_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY outcome_id ORDER BY is_closed DESC, close_time DESC) = 1
),
re AS (
  SELECT outcome_id, trace_id, event_type, labels
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.events_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  WHERE outcome_id IS NOT NULL
),
dot AS (
  SELECT DISTINCT outcome_id, trace_id FROM (
    SELECT outcome_id, trace_id FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.traces_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) WHERE outcome_id IS NOT NULL
    UNION DISTINCT
    SELECT outcome_id, trace_id FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.model_calls`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) WHERE outcome_id IS NOT NULL
    UNION DISTINCT
    SELECT outcome_id, trace_id FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.tool_calls_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) WHERE outcome_id IS NOT NULL
    UNION DISTINCT
    SELECT outcome_id, trace_id FROM re WHERE trace_id IS NOT NULL
  )
),
otg AS (
  SELECT dot.outcome_id, COUNT(*) AS mtc, LOGICAL_AND(COALESCE(t.rule0_pass, FALSE)) AS ar0,
    MAX(t.source_mode) AS source_mode, MAX(t.skill_id) AS skill_id, MAX(t.skill_version) AS skill_version, MAX(t.apphub_application_id) AS fb_owner
  FROM dot LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t ON dot.trace_id = t.trace_id GROUP BY dot.outcome_id
),
ecf AS (
  SELECT outcome_id,
    LOGICAL_AND(CASE WHEN JSON_VALUE(labels, '$.evaluation_score_label') IS NULL THEN TRUE WHEN LOWER(JSON_VALUE(labels, '$.evaluation_score_label')) IN ('true', 'verified', 'pass') THEN TRUE ELSE FALSE END) AS ev_ok
  FROM re WHERE event_type IN ('outcome.closed', 'gen_ai.evaluation.result') GROUP BY outcome_id
)
SELECT
  o.outcome_id, COALESCE(o.outcome_status, IF(o.is_closed, 'unknown', 'open')) AS outcome_status,
  o.outcome_parent_id, COALESCE(o.outcome_owner, otg.fb_owner, 'unregistered_app') AS outcome_owner,
  o.close_time AS closed_at, o.closing_trace_id, otg.source_mode, otg.skill_id, otg.skill_version,
  (o.is_closed AND COALESCE(o.outcome_status, 'unknown') = 'succeeded' AND COALESCE(otg.mtc, 0) > 0 AND COALESCE(otg.ar0, FALSE) AND o.root_error_trace_count = 0 AND o.failed_trace_count = 0 AND o.rootless_trace_count = 0 AND COALESCE(ecf.ev_ok, TRUE)) AS is_verified
FROM ro AS o LEFT JOIN otg ON o.outcome_id = otg.outcome_id LEFT JOIN ecf ON o.outcome_id = ecf.outcome_id;

CREATE OR REPLACE VIEW `agent_eval.v_cost_per_verified_outcome` AS
WITH os0 AS (
  SELECT
    w.outcome_id, COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(w.source_mode, o.source_mode, 'UNSPECIFIED') AS source_mode, w.agent_name,
    COALESCE(o.outcome_status, 'open') AS outcome_status, COALESCE(o.is_verified, FALSE) AS is_verified,
    COUNT(DISTINCT w.trace_id) AS tc, COUNT(*) AS sc,
    SUM(w.input_tokens) AS in_t, SUM(w.cached_input_tokens) AS ca_t, SUM(w.output_tokens) AS ou_t, SUM(w.thinking_tokens) AS th_t,
    COUNTIF(w.span_cost_usd IS NULL) AS unp,
    IF(COUNTIF(w.finops_traffic_type != 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)) AS m_u,
    IF(COUNTIF(w.finops_traffic_type = 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)) AS j_u
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o ON w.outcome_id = o.outcome_id
  LEFT JOIN `agent_eval.workload_thresholds` AS wt ON COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') = wt.apphub_application_id
  WHERE w.outcome_id IS NOT NULL AND NOT COALESCE(wt.is_external_counterparty, FALSE)
  GROUP BY 1, 2, 3, 4, 5, 6
),
ws AS (SELECT os.*, SUM(os.sc) OVER (PARTITION BY os.outcome_id) AS tot_sc FROM os0 AS os)
SELECT
  os.apphub_application_id, os.source_mode, os.agent_name,
  COUNT(DISTINCT os.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)) AS verified_outcomes,
  COUNT(DISTINCT IF(NOT os.is_verified, os.outcome_id, NULL)) AS unverified_or_failed_outcomes,
  SUM(os.tc) AS total_traces, SUM(os.in_t) AS total_input_tokens, SUM(os.ou_t) AS total_output_tokens, SUM(os.th_t) AS total_thinking_tokens,
  SUM(os.unp) AS unpriced_spans,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SUM(os.m_u)) AS measured_model_cost_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SUM(os.j_u)) AS measured_judge_cost_usd,
  SUM(COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.sc, os.tot_sc), 0.0)) AS estimated_infra_cost_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SUM(os.m_u + os.j_u)) AS total_ai_cost_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SUM(os.m_u + os.j_u + COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.sc, os.tot_sc), 0.0))) AS total_cost_with_infra_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(os.m_u + os.j_u), NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0))) AS cost_per_verified_outcome_ai_usd,
  IF(SUM(os.unp) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(os.m_u + os.j_u + COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.sc, os.tot_sc), 0.0)), NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0))) AS cost_per_verified_outcome_usd
FROM ws AS os LEFT JOIN `agent_eval.v_infra_cost_estimate` AS inf ON os.outcome_id = inf.outcome_id
GROUP BY os.apphub_application_id, os.source_mode, os.agent_name;

CREATE OR REPLACE VIEW `agent_eval.v_skill_lineage_roi` AS
WITH do0 AS (
  SELECT
    COALESCE(o.outcome_parent_id, w.outcome_id) AS doid, w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id) AS apphub_application_id,
    COALESCE(w.skill_id, o.skill_id, 'default-skill') AS skill_id,
    COALESCE(w.skill_version, o.skill_version, '1.0.0') AS skill_version,
    COALESCE(o.is_verified, FALSE) AS is_verified, COUNT(*) AS sc,
    LOGICAL_OR(w.finops_traffic_type = 'conflict_escalation' OR w.model_name = 'gemini-4-argon') AS esc,
    COUNTIF(w.finops_traffic_type = 'conflict_escalation') AS hrs,
    SUM(CASE WHEN w.finops_traffic_type != 'agent_gym_eval' THEN COALESCE(w.span_cost_usd, 0.0) ELSE 0.0 END) AS dm_u,
    SUM(CASE WHEN w.finops_traffic_type = 'agent_gym_eval' THEN COALESCE(w.span_cost_usd, 0.0) ELSE 0.0 END) AS ag_u
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o ON w.outcome_id = o.outcome_id
  WHERE w.outcome_id IS NOT NULL GROUP BY 1, 2, 3, 4, 5, 6
),
wd AS (SELECT d.*, SUM(d.sc) OVER (PARTITION BY d.outcome_id) AS tot_sc FROM do0 AS d)
SELECT
  d.apphub_application_id, d.skill_id, d.skill_version,
  IF(d.skill_version = '1.0.0', 'DAY_1_BASELINE', 'DAY_2_COMPILED_SKILL') AS skill_stage,
  COUNT(DISTINCT d.doid) AS total_deals, COUNT(DISTINCT d.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)) AS verified_outcomes,
  COUNTIF(d.esc) AS argon_escalated_outcomes, SUM(IF(d.hrs > 0, 1, 0)) AS human_reviews,
  SAFE_DIVIDE(SUM(IF(d.hrs > 0, 1, 0)), NULLIF(COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)), 0)) AS human_reviews_per_verified_outcome,
  SUM(d.dm_u) AS measured_model_cost_usd,
  SUM(COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(d.sc, d.tot_sc), 0.0)) AS estimated_infra_cost_usd,
  SUM(d.ag_u) AS one_time_skill_compile_cost_usd,
  SAFE_DIVIDE(SUM(d.dm_u + COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(d.sc, d.tot_sc), 0.0)), NULLIF(COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)), 0)) AS cost_per_verified_outcome_usd
FROM wd AS d LEFT JOIN `agent_eval.v_infra_cost_estimate` AS inf ON d.outcome_id = inf.outcome_id
GROUP BY d.apphub_application_id, d.skill_id, d.skill_version;
