-- Gold Deterministic Evaluation Gates, WORM Audit Candidates, `AI.EVALUATE_AGENT` Turns & Optimizer Export (`ADR-048`, `ADR-049`).
-- Reads Silver `traces`, `tool_calls`, `events`, `trace_payloads`, SCD-2 `silver_ref`, and published Gold `v_trace_waterfall`.

CREATE OR REPLACE VIEW `agent_eval.v_orphan_committed_actions` AS
WITH committed AS (
  SELECT event_id, event_time, trace_id, outcome_id, trace_owner_app_id, action_id, committed_args_sha256, user_id, actor_id
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.events`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  WHERE event_type = 'action.committed' AND is_authoritative IS TRUE
),
tools AS (
  SELECT trace_id, span_id, action_id, args_sha256, args_hash_source, writes_state
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.tool_calls`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
)
SELECT
  c.event_id, c.event_time, c.trace_id, c.outcome_id,
  COALESCE(c.trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
  c.action_id, c.committed_args_sha256,
  t.span_id AS matched_tool_span_id, t.args_sha256 AS matched_tool_args_sha256,
  CASE
    WHEN t.span_id IS NULL THEN 'MISSING_TOOL_SPAN'
    WHEN c.committed_args_sha256 IS NOT NULL AND COALESCE(t.args_sha256, '') != c.committed_args_sha256 THEN 'COMMITTED_ARGS_MISMATCH'
    ELSE 'OK'
  END AS orphan_status
FROM committed AS c
LEFT JOIN tools AS t ON c.action_id IS NOT NULL AND c.action_id = t.action_id AND (c.trace_id IS NULL OR c.trace_id = t.trace_id)
WHERE t.span_id IS NULL OR (c.committed_args_sha256 IS NOT NULL AND COALESCE(t.args_sha256, '') != c.committed_args_sha256);

CREATE OR REPLACE VIEW `agent_eval.v_trace_sql_evaluation` AS
WITH silver_traces AS (
  SELECT *
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.traces`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
    CURRENT_TIMESTAMP()
  )
),
silver_tools AS (
  SELECT *
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.tool_calls`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
    CURRENT_TIMESTAMP()
  )
),
silver_events AS (
  SELECT *
  FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.events`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
    CURRENT_TIMESTAMP()
  )
),
approvals AS (
  SELECT
    action_id,
    event_id,
    event_time,
    approved_args_sha256,
    approved_expires_at,
    actor_id,
    user_id,
    COUNT(*) OVER (PARTITION BY event_id) AS event_id_reuse_count
  FROM silver_events
  WHERE event_type = 'outcome.approved'
    AND is_authoritative IS TRUE
    AND action_id IS NOT NULL
),
tool_gate_rollup AS (
  SELECT
    tc.trace_id,
    COUNTIF(tc.span_status = 'ERROR') AS tool_error_count,
    COUNTIF(COALESCE(tc.executes_code, FALSE) AND (tc.span_status != 'OK' OR COALESCE(tc.sandbox_status, 'ok') != 'ok')) AS sandbox_violation_count,
    COUNTIF(COALESCE(tc.acl_dropped_count, 0) > 0 AND (tc.results IS NULL OR TRIM(tc.results) IN ('', '[]', '{}', 'null'))) AS silent_empty_rag_count,
    COUNTIF(
      COALESCE(tc.writes_state, FALSE)
      AND NOT (
        tc.action_id IS NOT NULL
        AND tc.args_sha256 IS NOT NULL
        AND tc.args_hash_source != 'none'
        AND ap.event_id IS NOT NULL
        AND ap.event_id_reuse_count = 1
        AND ap.approved_args_sha256 = tc.args_sha256
        AND ap.event_time < tc.start_time
        AND (ap.approved_expires_at IS NULL OR tc.start_time <= ap.approved_expires_at)
        AND ap.actor_id IS NOT NULL
        AND ap.actor_id != COALESCE(tc.user_id, '')
      )
    ) AS unauthorized_write_count,
    MAX(tc.args_sha256) AS sample_args_sha256,
    MAX(ap.approved_args_sha256) AS sample_approved_args_sha256
  FROM silver_tools AS tc
  LEFT JOIN approvals AS ap
    ON tc.action_id IS NOT NULL
    AND tc.action_id = ap.action_id
  GROUP BY tc.trace_id
),
guardrail_rollup AS (
  SELECT
    trace_id,
    COUNTIF(event_type = 'guardrail.intervention' AND source_class = 'platform' AND control_action = 'block') AS platform_guardrail_blocks,
    COUNTIF(event_type = 'guardrail.intervention') AS total_guardrail_events
  FROM silver_events
  WHERE trace_id IS NOT NULL
  GROUP BY trace_id
),
orphan_commits_by_trace AS (
  SELECT
    trace_id,
    COUNT(*) AS orphan_commit_count
  FROM `agent_eval.v_orphan_committed_actions`
  WHERE trace_id IS NOT NULL
  GROUP BY trace_id
),
waterfall_with_allow AS (
  SELECT
    w.*,
    am_any.has_rules,
    am.is_allowed,
    am.invokable_status,
    am.eol_date,
    ROW_NUMBER() OVER (
      PARTITION BY w.trace_id, w.span_id
      ORDER BY
        IF(am.app_id = w.apphub_application_id, 0, 1) ASC,
        IF(COALESCE(am.is_allowed AND am.invokable_status = 'active' AND (am.eol_date IS NULL OR am.eol_date >= CURRENT_DATE('UTC')), FALSE), 0, 1) ASC,
        am.effective_from DESC
    ) AS am_rn
  FROM `agent_eval.v_trace_waterfall` AS w
  CROSS JOIN (
    SELECT COUNT(*) > 0 AS has_rules
    FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.allowed_models`
    WHERE effective_to IS NULL
  ) AS am_any
  LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.allowed_models` AS am
    ON w.model_name IS NOT NULL AND w.model_name != 'none'
    AND am.effective_to IS NULL
    AND IF(
      STARTS_WITH(REGEXP_REPLACE(w.model_name, r'^.*/', ''), 'text-embedding-'),
      REGEXP_REPLACE(w.model_name, r'^.*/', ''),
      REGEXP_REPLACE(REGEXP_REPLACE(w.model_name, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '')
    ) = am.model
    AND (am.app_id = '*' OR am.app_id = w.apphub_application_id)
),
waterfall_rollup AS (
  SELECT
    w.trace_id,
    MAX(w.outcome_owner) AS outcome_owner,
    MAX(w.source_mode) AS source_mode,
    MAX(w.skill_version) AS skill_version,
    COUNT(*) AS wf_span_count,
    COUNTIF(w.http_status_code = 429) AS http_429_count,
    COUNTIF(
      w.model_name IS NOT NULL AND w.model_name != 'none'
      AND w.has_rules
      AND NOT COALESCE(w.is_allowed AND w.invokable_status = 'active' AND (w.eol_date IS NULL OR w.eol_date >= CURRENT_DATE('UTC')), FALSE)
    ) AS disallowed_model_call_count,
    COUNTIF(UPPER(COALESCE(w.serving_traffic_type, 'STANDARD')) IN ('PRIORITY', 'STANDARD', 'FLEX') AND w.model_name != 'none') AS spillover_span_count,
    LOGICAL_OR(w.is_long_context_cliff) AS hit_long_context_cliff,
    LOGICAL_OR(w.finops_traffic_type = 'conflict_escalation') AS wf_escalated_traffic,
    LOGICAL_AND(w.meets_min_cache_floor) AS meets_min_cache_floor,
    MAX(w.min_cache_tokens) AS min_cache_tokens,
    CASE MIN(
      CASE w.finops_traffic_type
        WHEN 'production_inference' THEN 1
        WHEN 'conflict_escalation' THEN 2
        WHEN 'agent_gym_eval' THEN 3
        WHEN 'autorater_judge' THEN 4
        WHEN 'optimize' THEN 5
        ELSE 6
      END
    )
      WHEN 1 THEN 'production_inference'
      WHEN 2 THEN 'conflict_escalation'
      WHEN 3 THEN 'agent_gym_eval'
      WHEN 4 THEN 'autorater_judge'
      WHEN 5 THEN 'optimize'
      ELSE COALESCE(MAX(w.finops_traffic_type), 'unclassified')
    END AS primary_traffic_type,
    SUM(w.input_tokens) AS total_input_tokens, SUM(w.cached_input_tokens) AS total_cached_input_tokens, SUM(w.uncached_input_tokens) AS total_uncached_input_tokens,
    SUM(w.output_tokens) AS total_output_tokens, SUM(w.thinking_tokens) AS total_thinking_tokens, SUM(w.weighted_burndown_tokens) AS weighted_burndown_tokens,
    IF(COUNTIF(w.finops_traffic_type = 'production_inference' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'production_inference' THEN w.span_cost_usd ELSE 0.0 END)) AS production_inference_usd,
    IF(COUNTIF(w.finops_traffic_type = 'conflict_escalation' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'conflict_escalation' THEN w.span_cost_usd ELSE 0.0 END)) AS conflict_escalation_usd,
    IF(COUNTIF(w.finops_traffic_type = 'agent_gym_eval' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'agent_gym_eval' THEN w.span_cost_usd ELSE 0.0 END)) AS agent_gym_eval_usd,
    IF(COUNTIF(w.finops_traffic_type = 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)) AS span_autorater_judge_usd,
    IF(COUNTIF(w.finops_traffic_type = 'optimize' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'optimize' THEN w.span_cost_usd ELSE 0.0 END)) AS optimize_usd,
    IF(COUNTIF(w.finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge', 'optimize') AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge', 'optimize') THEN w.span_cost_usd ELSE 0.0 END)) AS unclassified_usd,
    COUNTIF(w.span_cost_usd IS NULL) AS unpriced_spans,
    IF(COUNTIF(w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(w.span_cost_usd)) AS trace_span_cost_usd
  FROM waterfall_with_allow AS w
  WHERE w.am_rn = 1
  GROUP BY w.trace_id
),
active_apps AS (
  SELECT *
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry`
  WHERE effective_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY effective_from DESC) = 1
),
active_skills AS (
  SELECT *
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.skill_registry`
  WHERE effective_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id, skill_name ORDER BY effective_from DESC) = 1
),
active_alloc AS (
  SELECT
    app_id,
    MAX(cost_center) AS cost_center,
    SUM(funded_units) AS funded_gsus
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_allocations`
  WHERE effective_to IS NULL AND app_id IS NOT NULL
  GROUP BY app_id
)
SELECT
  t.trace_id,
  COALESCE(t.trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
  t.app_identity_bound,
  t.unregistered_app,
  CASE
    WHEN t.unregistered_app OR COALESCE(t.app_identity_bound, FALSE) IS FALSE THEN 'RL-1'
    ELSE COALESCE(t.risk_tier, ar.risk_tier, 'RL-3b')
  END AS effective_risk_tier,
  COALESCE(t.risk_tier, ar.risk_tier, 'RL-3b') AS risk_tier,
  t.outcome_id,
  t.multiple_outcome_ids,
  COALESCE(wf.outcome_owner, t.trace_owner_app_id) AS outcome_owner,
  wf.source_mode,
  t.skill_name AS skill_id,
  wf.skill_version,
  t.skill_sha256,
  t.protected_block_sha256,
  t.mounted_skills,
  t.tool_definitions_sha256,
  t.conversation_id,
  t.previous_interaction_id,
  t.case_id,
  t.eval_run_id,
  t.eval_split,
  t.shadow_of_trace_id,
  t.experiment_id,
  t.variant_id,
  COALESCE(t.platform_user_id, t.user_id) AS resolved_requester_id,
  (t.platform_user_id IS NOT NULL OR COALESCE(t.identity_attested, FALSE)) AS has_attested_requester,
  (
    (t.platform_user_id IS NOT NULL AND t.user_id IS NOT NULL AND t.platform_user_id != t.user_id)
    OR t.user_id_conflict_count > 0
  ) AS identity_spoof_detected,
  COALESCE(t.business_unit, ar.business_unit, 'unassigned_bu') AS business_unit,
  COALESCE(wt.sla_latency_ms, 5000.0) AS sla_latency_ms,
  COALESCE(ca.cost_center, wt.cost_center, 'UNASSIGNED') AS cost_center,
  COALESCE(ca.funded_gsus, wt.funded_gsus, 0.0) AS funded_gsus,
  t.trace_start_time,
  t.trace_end_time,
  t.last_span_arrival_time AS trace_receive_time,
  CAST(GREATEST(0, TIMESTAMP_DIFF(t.trace_end_time, t.trace_start_time, MILLISECOND)) AS FLOAT64) AS trace_latency_ms,
  TIMESTAMP_DIFF(t.last_span_arrival_time, t.trace_end_time, MILLISECOND) AS ingestion_lag_ms,
  t.span_count,
  t.model_call_count,
  t.tool_call_count,
  COALESCE(t.repeated_tool_call_count, 0) AS repeated_tool_call_count,
  COALESCE(tgr.tool_error_count, 0) AS tool_error_count,
  COALESCE(wf.http_429_count, t.throttle_count, 0) AS http_429_count,
  COALESCE(wf.spillover_span_count, 0) AS spillover_span_count,
  t.content_missing_call_count,
  COALESCE(gr.platform_guardrail_blocks, 0) AS platform_guardrail_blocks,
  COALESCE(tgr.silent_empty_rag_count, 0) AS silent_empty_rag_count,
  COALESCE(tgr.unauthorized_write_count, 0) AS unauthorized_write_count,
  COALESCE(oct.orphan_commit_count, 0) AS orphan_commit_count,
  tgr.sample_args_sha256,
  tgr.sample_approved_args_sha256,
  JSON_VALUE(t.labels, '$.agent_version') AS agent_version,
  (
    NOT (
      (t.platform_user_id IS NOT NULL AND t.user_id IS NOT NULL AND t.platform_user_id != t.user_id)
      OR t.user_id_conflict_count > 0
    )
    AND NOT (
      (
        CASE
          WHEN t.unregistered_app OR COALESCE(t.app_identity_bound, FALSE) IS FALSE THEN 'RL-1'
          ELSE COALESCE(t.risk_tier, ar.risk_tier, 'RL-3b')
        END
      ) IN ('RL-1', 'RL-2', 'RL-3', 'RL-3a', 'RL-3b')
      AND (t.user_id IS NOT NULL OR COALESCE(ar.payload_profile, '') = 'regulated_worm' OR t.mutating_tool_call_count > 0)
      AND t.platform_user_id IS NULL
      AND NOT COALESCE(t.identity_attested, FALSE)
    )
  ) AS identity_gate_pass,
  (
    NOT t.unregistered_skill
    AND (sk.protected_block_sha256 IS NULL OR COALESCE(t.protected_block_sha256, '') = sk.protected_block_sha256)
    AND (sk.skill_sha256 IS NULL OR t.skill_sha256 IS NULL OR t.skill_sha256 = sk.skill_sha256)
  ) AS skill_gate_pass,
  (
    t.is_turn
    AND t.has_root_span
    AND COALESCE(t.root_status, 'OK') != 'ERROR'
    AND NOT t.app_failed
    AND t.error_count = 0
    AND COALESCE(tgr.tool_error_count, 0) = 0
    AND t.tool_call_count <= COALESCE(wt.max_tool_calls, 8)
    AND COALESCE(t.repeated_tool_call_count, 0) <= COALESCE(wt.max_repeated_tool_calls, 0)
    AND COALESCE(tgr.silent_empty_rag_count, 0) = 0
    AND COALESCE(tgr.sandbox_violation_count, 0) = 0
    AND COALESCE(tgr.unauthorized_write_count, 0) = 0
    AND COALESCE(oct.orphan_commit_count, 0) = 0
    AND COALESCE(wf.disallowed_model_call_count, 0) = 0
    AND NOT t.unregistered_skill
    AND (sk.protected_block_sha256 IS NULL OR COALESCE(t.protected_block_sha256, '') = sk.protected_block_sha256)
    AND NOT (
      (t.platform_user_id IS NOT NULL AND t.user_id IS NOT NULL AND t.platform_user_id != t.user_id)
      OR t.user_id_conflict_count > 0
    )
    AND NOT (
      (
        CASE
          WHEN t.unregistered_app OR COALESCE(t.app_identity_bound, FALSE) IS FALSE THEN 'RL-1'
          ELSE COALESCE(t.risk_tier, ar.risk_tier, 'RL-3b')
        END
      ) IN ('RL-1', 'RL-2', 'RL-3', 'RL-3a', 'RL-3b')
      AND (t.user_id IS NOT NULL OR COALESCE(ar.payload_profile, '') = 'regulated_worm' OR t.mutating_tool_call_count > 0)
      AND t.platform_user_id IS NULL
      AND NOT COALESCE(t.identity_attested, FALSE)
    )
  ) AS rule0_pass,
  (t.escalated OR COALESCE(wf.wf_escalated_traffic, FALSE)) AS escalated_to_high_reasoning,
  (COALESCE(tgr.tool_error_count, 0) > 0 OR COALESCE(wf.http_429_count, t.throttle_count, 0) > 0 OR (t.escalated OR COALESCE(wf.wf_escalated_traffic, FALSE)) OR COALESCE(t.repeated_tool_call_count, 0) > 0 OR COALESCE(tgr.silent_empty_rag_count, 0) > 0) AS is_loop_or_error_tail,
  (NOT (COALESCE(tgr.tool_error_count, 0) > 0 OR COALESCE(wf.http_429_count, t.throttle_count, 0) > 0 OR (t.escalated OR COALESCE(wf.wf_escalated_traffic, FALSE)) OR COALESCE(t.repeated_tool_call_count, 0) > 0 OR COALESCE(tgr.silent_empty_rag_count, 0) > 0) AND CAST(GREATEST(0, TIMESTAMP_DIFF(t.trace_end_time, t.trace_start_time, MILLISECOND)) AS FLOAT64) > COALESCE(wt.sla_latency_ms, 5000.0)) AS is_healthy_slow_tail,
  CASE
    WHEN COALESCE(tgr.tool_error_count, 0) > 0 THEN 'TOOL_ERROR'
    WHEN COALESCE(wf.http_429_count, t.throttle_count, 0) > 0 THEN 'HTTP_429'
    WHEN COALESCE(tgr.silent_empty_rag_count, 0) > 0 THEN 'SILENT_EMPTY_RAG'
    WHEN COALESCE(t.repeated_tool_call_count, 0) > 0 THEN 'REPEATED_TOOL_LOOP'
    WHEN (t.escalated OR COALESCE(wf.wf_escalated_traffic, FALSE)) THEN 'ESCALATED'
    WHEN CAST(GREATEST(0, TIMESTAMP_DIFF(t.trace_end_time, t.trace_start_time, MILLISECOND)) AS FLOAT64) > COALESCE(wt.sla_latency_ms, 5000.0) THEN 'HEALTHY_SLOW'
    ELSE 'NONE'
  END AS tail_root_cause,
  CASE
    WHEN (COALESCE(tgr.tool_error_count, 0) > 0 OR COALESCE(wf.http_429_count, t.throttle_count, 0) > 0 OR (t.escalated OR COALESCE(wf.wf_escalated_traffic, FALSE)) OR COALESCE(t.repeated_tool_call_count, 0) > 0 OR COALESCE(tgr.silent_empty_rag_count, 0) > 0) THEN 'LOOP_OR_ERROR_TAIL'
    WHEN CAST(GREATEST(0, TIMESTAMP_DIFF(t.trace_end_time, t.trace_start_time, MILLISECOND)) AS FLOAT64) > COALESCE(wt.sla_latency_ms, 5000.0) THEN 'HEALTHY_SLOW'
    ELSE 'HEALTHY'
  END AS tail_class,
  COALESCE(wf.hit_long_context_cliff, FALSE) AS hit_long_context_cliff,
  COALESCE(wf.meets_min_cache_floor, TRUE) AS meets_min_cache_floor,
  COALESCE(wf.min_cache_tokens, 0) AS min_cache_tokens,
  COALESCE(wf.primary_traffic_type, t.traffic_type, 'production_inference') AS primary_traffic_type,
  COALESCE(wf.total_input_tokens, 0) AS total_input_tokens,
  COALESCE(wf.total_cached_input_tokens, 0) AS total_cached_input_tokens,
  COALESCE(wf.total_uncached_input_tokens, 0) AS total_uncached_input_tokens,
  COALESCE(wf.total_output_tokens, 0) AS total_output_tokens,
  COALESCE(wf.total_thinking_tokens, 0) AS total_thinking_tokens,
  COALESCE(wf.weighted_burndown_tokens, 0.0) AS weighted_burndown_tokens,
  COALESCE(wf.production_inference_usd, IF(COALESCE(wf.unpriced_spans, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS production_inference_usd,
  COALESCE(wf.conflict_escalation_usd, IF(COALESCE(wf.unpriced_spans, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS conflict_escalation_usd,
  COALESCE(wf.agent_gym_eval_usd, IF(COALESCE(wf.unpriced_spans, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS agent_gym_eval_usd,
  COALESCE(wf.span_autorater_judge_usd, IF(COALESCE(wf.unpriced_spans, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS span_autorater_judge_usd,
  COALESCE(wf.optimize_usd, IF(COALESCE(wf.unpriced_spans, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS optimize_usd,
  COALESCE(wf.unclassified_usd, IF(COALESCE(wf.unpriced_spans, 0) > 0, CAST(NULL AS FLOAT64), 0.0)) AS unclassified_usd,
  COALESCE(wf.unpriced_spans, 0) AS unpriced_spans,
  wf.trace_span_cost_usd,
  COALESCE(ar.sampling_mode, 'full') AS sampling_mode,
  t.sampling_probability,
  CASE
    WHEN COALESCE(ar.sampling_mode, 'full') = 'full' THEN 1.0
    WHEN t.sampling_probability IS NOT NULL AND t.sampling_probability > 0.0 THEN SAFE_DIVIDE(1.0, t.sampling_probability)
    ELSE CAST(NULL AS FLOAT64)
  END AS scaled_trace_weight,
  CASE
    WHEN COALESCE(wf.unpriced_spans, 0) > 0 THEN CAST(NULL AS FLOAT64)
    WHEN COALESCE(ar.sampling_mode, 'full') = 'full' THEN wf.trace_span_cost_usd
    WHEN t.sampling_probability IS NOT NULL AND t.sampling_probability > 0.0 THEN SAFE_DIVIDE(wf.trace_span_cost_usd, t.sampling_probability)
    ELSE CAST(NULL AS FLOAT64)
  END AS scaled_trace_span_cost_usd,
  SAFE_DIVIDE(COALESCE(wf.total_cached_input_tokens, 0), NULLIF(COALESCE(wf.total_input_tokens, 0), 0)) AS cache_hit_ratio,
  SAFE_DIVIDE(COALESCE(wf.total_thinking_tokens, 0), NULLIF(COALESCE(wf.total_output_tokens, 0) + COALESCE(wf.total_thinking_tokens, 0), 0)) AS thinking_token_ratio
FROM silver_traces AS t
LEFT JOIN waterfall_rollup AS wf
  ON t.trace_id = wf.trace_id
LEFT JOIN tool_gate_rollup AS tgr
  ON t.trace_id = tgr.trace_id
LEFT JOIN guardrail_rollup AS gr
  ON t.trace_id = gr.trace_id
LEFT JOIN orphan_commits_by_trace AS oct
  ON t.trace_id = oct.trace_id
LEFT JOIN active_apps AS ar
  ON t.trace_owner_app_id = ar.app_id
LEFT JOIN active_skills AS sk
  ON t.trace_owner_app_id = sk.app_id AND t.skill_name = sk.skill_name
LEFT JOIN `agent_eval.workload_thresholds` AS wt
  ON COALESCE(t.trace_owner_app_id, 'unregistered_app') = wt.apphub_application_id
LEFT JOIN active_alloc AS ca
  ON COALESCE(t.trace_owner_app_id, 'unregistered_app') = ca.app_id;

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
  JSON_OBJECT('rule0_pass', t.rule0_pass, 'identity_gate_pass', t.identity_gate_pass, 'skill_gate_pass', t.skill_gate_pass, 'unauthorized_write_count', t.unauthorized_write_count, 'orphan_commit_count', t.orphan_commit_count) AS policy_verdicts_json,
  t.sample_args_sha256 AS args_sha256, t.sample_approved_args_sha256 AS approved_args_sha256,
  TO_HEX(SHA256(CONCAT(t.trace_id, '|', COALESCE(t.outcome_id, ''), '|', COALESCE(t.skill_sha256, ''), '|', COALESCE(t.protected_block_sha256, ''), '|', CAST(t.rule0_pass AS STRING), '|', COALESCE(t.sample_args_sha256, ''), '|', COALESCE(t.sample_approved_args_sha256, '')))) AS snapshot_sha256
FROM `agent_eval.v_trace_sql_evaluation` AS t;

CREATE OR REPLACE VIEW `agent_eval.v_optimizer_gold_export` AS
WITH nudge_rollup AS (
  SELECT
    trace_id,
    ARRAY_AGG(nudge_id IGNORE NULLS ORDER BY event_time ASC LIMIT 1)[SAFE_OFFSET(0)] AS nudge_id
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.events_open`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
    CURRENT_TIMESTAMP()
  )
  WHERE trace_id IS NOT NULL AND nudge_id IS NOT NULL
  GROUP BY trace_id
),
tagged_eval AS (
  SELECT
    COALESCE(t.eval_run_id, 'default_run') AS eval_run_id,
    COALESCE(t.variant_id, t.skill_sha256, t.apphub_application_id) AS candidate_id,
    CASE
      WHEN t.primary_traffic_type = 'optimize' THEN 'OFFLINE_SWEEP'
      WHEN t.shadow_of_trace_id IS NOT NULL OR t.experiment_id IS NOT NULL THEN 'LIVE_SHADOW_AB'
      ELSE 'CI_ONLY'
    END AS optimizer_mode,
    t.apphub_application_id,
    t.trace_id,
    t.skill_sha256,
    t.protected_block_sha256,
    t.skill_gate_pass,
    TO_JSON_STRING(t.mounted_skills) AS mounted_skills_json,
    COALESCE(t.eval_split, 'unspecified') AS eval_split,
    t.rule0_pass,
    t.min_cache_tokens,
    t.meets_min_cache_floor,
    MIN(CASE WHEN t.case_id IS NOT NULL AND t.eval_split IN ('train', 'holdout') THEN t.eval_split END)
      OVER (PARTITION BY COALESCE(t.eval_run_id, 'default_run'), t.case_id) AS min_case_split,
    MAX(CASE WHEN t.case_id IS NOT NULL AND t.eval_split IN ('train', 'holdout') THEN t.eval_split END)
      OVER (PARTITION BY COALESCE(t.eval_run_id, 'default_run'), t.case_id) AS max_case_split
  FROM `agent_eval.v_trace_sql_evaluation` AS t
),
candidate_rollup AS (
  SELECT
    te.eval_run_id,
    te.candidate_id,
    te.optimizer_mode,
    te.apphub_application_id,
    MAX(te.skill_sha256) AS skill_sha256,
    MAX(te.protected_block_sha256) AS protected_block_sha256,
    LOGICAL_AND(te.skill_gate_pass) AS protected_block_verified,
    ANY_VALUE(te.mounted_skills_json) AS mounted_skills_json,
    te.eval_split,
    MAX(nr.nudge_id) AS nudge_id,
    AVG(IF(te.rule0_pass, 1.0, 0.0)) AS ci_only_pareto_score,
    MAX(te.min_cache_tokens) AS min_cache_tokens,
    LOGICAL_AND(te.meets_min_cache_floor) AS passes_prefix_cache_floor,
    COUNTIF(te.min_case_split IS NOT NULL AND te.min_case_split != te.max_case_split) AS overlap_rows
  FROM tagged_eval AS te
  LEFT JOIN nudge_rollup AS nr
    ON te.trace_id = nr.trace_id
  GROUP BY 1, 2, 3, 4, 9
)
SELECT
  cr.eval_run_id,
  cr.candidate_id,
  cr.optimizer_mode,
  cr.apphub_application_id,
  cr.skill_sha256,
  cr.protected_block_sha256,
  cr.protected_block_verified,
  SAFE.PARSE_JSON(cr.mounted_skills_json) AS mounted_skills,
  cr.eval_split,
  SUM(cr.overlap_rows) OVER (PARTITION BY cr.eval_run_id) = 0 AS split_disjoint_verified,
  cr.nudge_id,
  cr.ci_only_pareto_score,
  CAST(NULL AS FLOAT64) AS calibrated_holdout_pass_rate,
  cr.min_cache_tokens,
  cr.passes_prefix_cache_floor
FROM candidate_rollup AS cr;

CREATE OR REPLACE VIEW `agent_eval.v_outcomes` AS
WITH raw_outcomes AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcomes_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY outcome_id ORDER BY is_closed DESC, close_time DESC) = 1
),
recent_events AS (
  SELECT outcome_id, trace_id, event_type, labels
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.events_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP())
  WHERE outcome_id IS NOT NULL
),
distinct_outcome_traces AS (
  SELECT DISTINCT outcome_id, trace_id FROM (
    SELECT outcome_id, trace_id FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.traces_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) WHERE outcome_id IS NOT NULL
    UNION DISTINCT
    SELECT outcome_id, trace_id FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.model_calls`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) WHERE outcome_id IS NOT NULL
    UNION DISTINCT
    SELECT outcome_id, trace_id FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.tool_calls_open`(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR), CURRENT_TIMESTAMP()) WHERE outcome_id IS NOT NULL
    UNION DISTINCT
    SELECT outcome_id, trace_id FROM recent_events WHERE trace_id IS NOT NULL
  )
),
outcome_trace_gates AS (
  SELECT dot.outcome_id, COUNT(*) AS member_trace_count, LOGICAL_AND(COALESCE(t.rule0_pass, FALSE)) AS all_traces_rule0_pass,
    MAX(t.source_mode) AS source_mode, MAX(t.skill_id) AS skill_id, MAX(t.skill_version) AS skill_version, MAX(t.apphub_application_id) AS fallback_owner
  FROM distinct_outcome_traces AS dot
  LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t ON dot.trace_id = t.trace_id
  GROUP BY dot.outcome_id
),
explicit_close_flags AS (
  SELECT outcome_id,
    LOGICAL_AND(CASE WHEN JSON_VALUE(labels, '$.evaluation_score_label') IS NULL THEN TRUE WHEN LOWER(JSON_VALUE(labels, '$.evaluation_score_label')) IN ('true', 'verified', 'pass') THEN TRUE ELSE FALSE END) AS explicit_verified_ok
  FROM recent_events
  WHERE event_type IN ('outcome.closed', 'gen_ai.evaluation.result')
  GROUP BY outcome_id
)
SELECT
  o.outcome_id,
  COALESCE(o.outcome_status, IF(o.is_closed, 'unknown', 'open')) AS outcome_status,
  o.outcome_parent_id,
  COALESCE(o.outcome_owner, otg.fallback_owner, 'unregistered_app') AS outcome_owner,
  o.close_time AS closed_at,
  o.closing_trace_id,
  otg.source_mode,
  otg.skill_id,
  otg.skill_version,
  (o.is_closed AND COALESCE(o.outcome_status, 'unknown') = 'succeeded' AND COALESCE(otg.member_trace_count, 0) > 0 AND COALESCE(otg.all_traces_rule0_pass, FALSE) AND o.root_error_trace_count = 0 AND o.failed_trace_count = 0 AND o.rootless_trace_count = 0 AND COALESCE(ecf.explicit_verified_ok, TRUE)) AS is_verified
FROM raw_outcomes AS o
LEFT JOIN outcome_trace_gates AS otg ON o.outcome_id = otg.outcome_id
LEFT JOIN explicit_close_flags AS ecf ON o.outcome_id = ecf.outcome_id;

CREATE OR REPLACE VIEW `agent_eval.v_cost_per_verified_outcome` AS
WITH outcome_spans AS (
  SELECT
    w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(w.source_mode, o.source_mode, 'UNSPECIFIED') AS source_mode,
    w.agent_name,
    COALESCE(o.outcome_status, 'open') AS outcome_status,
    COALESCE(o.is_verified, FALSE) AS is_verified,
    COUNT(DISTINCT w.trace_id) AS trace_count, COUNT(*) AS span_count,
    SUM(w.input_tokens) AS input_tokens, SUM(w.cached_input_tokens) AS cached_input_tokens, SUM(w.output_tokens) AS output_tokens, SUM(w.thinking_tokens) AS thinking_tokens,
    COUNTIF(w.span_cost_usd IS NULL) AS unpriced_spans,
    IF(COUNTIF(w.finops_traffic_type != 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)) AS measured_model_cost_usd,
    IF(COUNTIF(w.finops_traffic_type = 'autorater_judge' AND w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)) AS measured_judge_cost_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o ON w.outcome_id = o.outcome_id
  LEFT JOIN `agent_eval.workload_thresholds` AS wt ON COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') = wt.apphub_application_id
  WHERE w.outcome_id IS NOT NULL AND NOT COALESCE(wt.is_external_counterparty, FALSE)
  GROUP BY 1, 2, 3, 4, 5, 6
),
ws AS (SELECT os.*, SUM(os.span_count) OVER (PARTITION BY os.outcome_id) AS tot_spans FROM outcome_spans AS os)
SELECT
  os.apphub_application_id, os.source_mode, os.agent_name,
  COUNT(DISTINCT os.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)) AS verified_outcomes,
  COUNT(DISTINCT IF(NOT os.is_verified, os.outcome_id, NULL)) AS unverified_or_failed_outcomes,
  SUM(os.trace_count) AS total_traces, SUM(os.input_tokens) AS total_input_tokens, SUM(os.output_tokens) AS total_output_tokens, SUM(os.thinking_tokens) AS total_thinking_tokens,
  SUM(os.unpriced_spans) AS unpriced_spans,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.measured_model_cost_usd)) AS measured_model_cost_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.measured_judge_cost_usd)) AS measured_judge_cost_usd,
  SUM(COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.span_count, os.tot_spans), 0.0)) AS estimated_infra_cost_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd)) AS total_ai_cost_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd + COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.span_count, os.tot_spans), 0.0))) AS total_cost_with_infra_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd), NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0))) AS cost_per_verified_outcome_ai_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SAFE_DIVIDE(SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd + COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.span_count, os.tot_spans), 0.0)), NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0))) AS cost_per_verified_outcome_usd
FROM ws AS os
LEFT JOIN `agent_eval.v_infra_cost_estimate` AS inf ON os.outcome_id = inf.outcome_id
GROUP BY os.apphub_application_id, os.source_mode, os.agent_name;

CREATE OR REPLACE VIEW `agent_eval.v_skill_lineage_roi` AS
WITH deal_outcomes AS (
  SELECT
    COALESCE(o.outcome_parent_id, w.outcome_id) AS deal_outcome_id, w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id) AS apphub_application_id,
    COALESCE(w.skill_id, o.skill_id, 'default-skill') AS skill_id,
    COALESCE(w.skill_version, o.skill_version, '1.0.0') AS skill_version,
    COALESCE(o.is_verified, FALSE) AS is_verified,
    COUNT(*) AS span_count,
    LOGICAL_OR(w.finops_traffic_type = 'conflict_escalation' OR w.model_name = 'gemini-4-argon') AS escalated_to_argon,
    COUNTIF(w.finops_traffic_type = 'conflict_escalation') AS human_review_spans,
    SUM(CASE WHEN w.finops_traffic_type != 'agent_gym_eval' THEN COALESCE(w.span_cost_usd, 0.0) ELSE 0.0 END) AS deal_model_cost_usd,
    SUM(CASE WHEN w.finops_traffic_type = 'agent_gym_eval' THEN COALESCE(w.span_cost_usd, 0.0) ELSE 0.0 END) AS agent_gym_compile_cost_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o ON w.outcome_id = o.outcome_id
  WHERE w.outcome_id IS NOT NULL
  GROUP BY 1, 2, 3, 4, 5, 6
),
wd AS (SELECT d.*, SUM(d.span_count) OVER (PARTITION BY d.outcome_id) AS tot_spans FROM deal_outcomes AS d)
SELECT
  d.apphub_application_id, d.skill_id, d.skill_version,
  IF(d.skill_version = '1.0.0', 'DAY_1_BASELINE', 'DAY_2_COMPILED_SKILL') AS skill_stage,
  COUNT(DISTINCT d.deal_outcome_id) AS total_deals,
  COUNT(DISTINCT d.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)) AS verified_outcomes,
  COUNTIF(d.escalated_to_argon) AS argon_escalated_outcomes,
  SUM(IF(d.human_review_spans > 0, 1, 0)) AS human_reviews,
  SAFE_DIVIDE(SUM(IF(d.human_review_spans > 0, 1, 0)), NULLIF(COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)), 0)) AS human_reviews_per_verified_outcome,
  SUM(d.deal_model_cost_usd) AS measured_model_cost_usd,
  SUM(COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(d.span_count, d.tot_spans), 0.0)) AS estimated_infra_cost_usd,
  SUM(d.agent_gym_compile_cost_usd) AS one_time_skill_compile_cost_usd,
  SAFE_DIVIDE(SUM(d.deal_model_cost_usd + COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(d.span_count, d.tot_spans), 0.0)), NULLIF(COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)), 0)) AS cost_per_verified_outcome_usd
FROM wd AS d
LEFT JOIN `agent_eval.v_infra_cost_estimate` AS inf ON d.outcome_id = inf.outcome_id
GROUP BY d.apphub_application_id, d.skill_id, d.skill_version;
