-- Gold FinOps Capacity, 3-Pool FOCUS 1.2, Session Lineage, Nudge Feedback, SLO Breaches & LOB Lifecycle Views (`ADR-001`, `ADR-048`, `ADR-049`).
-- Strict open-only boundary: reads ONLY `silver_open`, `silver_ref`, and `agent_eval`.

CREATE OR REPLACE VIEW `agent_eval.v_capacity_hourly_burndown_export` AS
SELECT
  TIMESTAMP_TRUNC(w.start_time, HOUR) AS usage_hour,
  FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', TIMESTAMP_TRUNC(w.start_time, HOUR)) AS timestamp,
  w.apphub_application_id,
  w.apphub_application_id AS tenant,
  w.model_name,
  w.model_name AS model,
  w.finops_traffic_type,
  w.serving_traffic_type,
  COUNT(DISTINCT w.trace_id) AS distinct_traces,
  COUNT(*) AS span_count,
  COUNTIF(w.status_code = 'OK') AS ok_span_count,
  COUNTIF(w.status_code = 'ERROR') AS error_span_count,
  COUNTIF(w.http_status_code = 429) AS http_429_count,
  COUNTIF(UPPER(COALESCE(w.serving_traffic_type, 'STANDARD')) IN ('PRIORITY', 'STANDARD', 'FLEX')) AS spillover_span_count,
  SUM(w.input_tokens) AS input_tokens,
  SUM(w.cached_input_tokens) AS cached_input_tokens,
  SUM(w.cached_input_tokens) AS cached_tokens,
  SUM(w.cache_write_5m_tokens) AS cache_write_5m_tokens,
  SUM(w.cache_write_1h_tokens) AS cache_write_1h_tokens,
  SUM(w.uncached_input_tokens) AS uncached_input_tokens,
  SUM(w.output_tokens) AS output_tokens,
  SUM(w.thinking_tokens) AS thinking_tokens,
  SUM(w.thinking_tokens) AS thoughts_tokens,
  COUNTIF(w.is_long_context_cliff) AS long_context_cliff_spans,
  SUM(w.weighted_burndown_tokens) AS weighted_burndown_tokens,
  SUM(w.weighted_burndown_tokens) AS precomputed_weighted_burndown_tokens,
  SUM(COALESCE(w.span_cost_usd, 0.0)) AS total_cost_usd
FROM `agent_eval.v_trace_waterfall` AS w
WHERE w.model_name IS NOT NULL
  AND w.model_name != 'none'
GROUP BY
  usage_hour,
  timestamp,
  w.apphub_application_id,
  w.model_name,
  w.finops_traffic_type,
  w.serving_traffic_type;

CREATE OR REPLACE VIEW `agent_eval.v_inference_request_reconciliation` AS
WITH otel_hourly AS (
  SELECT
    TIMESTAMP_TRUNC(start_time, HOUR) AS usage_hour,
    apphub_application_id,
    model_name,
    COUNT(*) AS otel_span_count,
    COUNTIF(http_status_code = 429) AS otel_429_count,
    SUM(caller_retry_count) AS caller_retry_count,
    SUM(gateway_failover_attempts) AS gateway_failover_attempts,
    SUM(input_tokens + output_tokens + thinking_tokens) AS otel_total_tokens,
    COUNTIF(
      UPPER(COALESCE(serving_traffic_type, 'STANDARD')) != 'PROVISIONED_THROUGHPUT'
      AND LOWER(COALESCE(request_type_header, '')) != 'shared'
    ) AS otel_pt_eligible_spillover_count,
    COUNTIF(LOWER(COALESCE(request_type_header, '')) = 'shared') AS otel_shared_bypass_count
  FROM `agent_eval.v_trace_waterfall`
  WHERE model_name IS NOT NULL AND model_name != 'none'
  GROUP BY usage_hour, apphub_application_id, model_name
),
server_hourly AS (
  SELECT
    TIMESTAMP_TRUNC(request_time, HOUR) AS usage_hour,
    COALESCE(trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(response_model, request_model, 'none') AS model_name,
    COUNT(*) AS server_request_count,
    COUNTIF(attempt_index > 1) AS server_failover_attempts,
    COUNTIF(http_status_code = 429) AS server_429_count,
    COUNTIF(tier_source = 'inferred_ambiguous') AS inferred_ambiguous_count,
    SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + COALESCE(reasoning_tokens, 0)) AS server_total_tokens,
    COUNTIF(
      UPPER(COALESCE(serving_tier, 'STANDARD')) != 'PROVISIONED_THROUGHPUT'
      AND LOWER(COALESCE(JSON_VALUE(labels, '$.request_type_header'), '')) != 'shared'
    ) AS pt_eligible_involuntary_spillover_count,
    COUNTIF(LOWER(COALESCE(JSON_VALUE(labels, '$.request_type_header'), '')) = 'shared') AS intentional_shared_bypass_count
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.requests`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
    CURRENT_TIMESTAMP()
  )
  GROUP BY usage_hour, apphub_application_id, model_name
)
SELECT
  COALESCE(o.usage_hour, s.usage_hour) AS usage_hour,
  COALESCE(o.apphub_application_id, s.apphub_application_id) AS apphub_application_id,
  COALESCE(o.model_name, s.model_name) AS model_name,
  COALESCE(o.otel_span_count, 0) AS otel_span_count,
  COALESCE(s.server_request_count, 0) AS server_request_count,
  COALESCE(o.caller_retry_count, 0) AS caller_retry_count,
  COALESCE(s.server_failover_attempts, o.gateway_failover_attempts, 0) AS gateway_failover_attempts,
  COALESCE(s.inferred_ambiguous_count, 0) AS inferred_ambiguous_count,
  COALESCE(o.otel_429_count, 0) AS otel_429_count,
  COALESCE(s.server_429_count, 0) AS server_429_count,
  COALESCE(o.otel_total_tokens, 0) AS otel_total_tokens,
  COALESCE(s.server_total_tokens, 0) AS server_total_tokens,
  100.0 * COALESCE(
    SAFE_DIVIDE(
      CAST(COALESCE(o.otel_total_tokens, 0) - COALESCE(s.server_total_tokens, 0) AS FLOAT64),
      NULLIF(CAST(COALESCE(s.server_total_tokens, o.otel_total_tokens) AS FLOAT64), 0.0)
    ),
    0.0
  ) AS token_drift_pct,
  COALESCE(s.pt_eligible_involuntary_spillover_count, o.otel_pt_eligible_spillover_count, 0) AS pt_eligible_involuntary_spillover_count,
  COALESCE(s.intentional_shared_bypass_count, o.otel_shared_bypass_count, 0) AS intentional_shared_bypass_count,
  CASE
    WHEN ABS(
      100.0 * COALESCE(
        SAFE_DIVIDE(
          CAST(COALESCE(o.otel_total_tokens, 0) - COALESCE(s.server_total_tokens, 0) AS FLOAT64),
          NULLIF(CAST(COALESCE(s.server_total_tokens, o.otel_total_tokens) AS FLOAT64), 0.0)
        ),
        0.0
      )
    ) <= 2.0 THEN 'RECONCILED'
    ELSE 'DRIFT_ALERT'
  END AS reconciliation_status
FROM otel_hourly AS o
FULL OUTER JOIN server_hourly AS s
  ON o.usage_hour = s.usage_hour
  AND o.apphub_application_id = s.apphub_application_id
  AND o.model_name = s.model_name;

CREATE OR REPLACE VIEW `agent_eval.v_app_risk_governance` AS
WITH active_apps AS (
  SELECT app_id, risk_tier
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry`
  WHERE effective_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY effective_from DESC) = 1
),
active_alloc AS (
  SELECT
    app_id,
    SUM(funded_units) AS alloc_funded_gsus
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_allocations`
  WHERE effective_to IS NULL AND app_id IS NOT NULL
  GROUP BY app_id
),
app_models AS (
  SELECT
    COALESCE(c.trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
    c.model_name,
    ANY_VALUE(c.lifecycle_track) AS model_lifecycle_track,
    COUNT(*) AS model_span_count
  FROM `agent_eval.gold_call_costs`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
    CURRENT_TIMESTAMP()
  ) AS c
  WHERE c.model_name IS NOT NULL AND c.model_name != 'none'
  GROUP BY 1, 2
),
trace_eff_tier AS (
  SELECT
    COALESCE(trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
    COUNT(*) AS trace_cnt,
    MIN(CASE WHEN unregistered_app OR COALESCE(app_identity_bound, FALSE) IS FALSE THEN 'RL-1' ELSE COALESCE(risk_tier, 'RL-3b') END) AS effective_risk_tier
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.traces_open`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
    CURRENT_TIMESTAMP()
  )
  GROUP BY 1
),
judged_rubrics AS (
  SELECT DISTINCT apphub_application_id, rubric_id
  FROM `agent_eval.eval_results`
  WHERE COALESCE(judge_status, 'SCORED') = 'SCORED' AND binary_pass IS NOT NULL
),
sme_ord_gov AS (
  SELECT
    s.apphub_application_id,
    s.rubric_id,
    COUNT(*) AS ord_pairs,
    AVG(IF((s.human_ordinal_score >= COALESCE(rb.passing_score, 3)) = (s.judge_ordinal_score >= COALESCE(rb.passing_score, 3)), 1.0, 0.0)) AS binary_cutpoint_agreement,
    AVG(1.0 - POW(CAST(s.human_ordinal_score - s.judge_ordinal_score AS FLOAT64) / 3.0, 2)) AS gwet_p_a,
    (26.0 / 27.0) * (
      ((AVG(IF(s.human_ordinal_score = 1, 1.0, 0.0)) + AVG(IF(s.judge_ordinal_score = 1, 1.0, 0.0))) / 2.0) * (1.0 - ((AVG(IF(s.human_ordinal_score = 1, 1.0, 0.0)) + AVG(IF(s.judge_ordinal_score = 1, 1.0, 0.0))) / 2.0))
      + ((AVG(IF(s.human_ordinal_score = 2, 1.0, 0.0)) + AVG(IF(s.judge_ordinal_score = 2, 1.0, 0.0))) / 2.0) * (1.0 - ((AVG(IF(s.human_ordinal_score = 2, 1.0, 0.0)) + AVG(IF(s.judge_ordinal_score = 2, 1.0, 0.0))) / 2.0))
      + ((AVG(IF(s.human_ordinal_score = 3, 1.0, 0.0)) + AVG(IF(s.judge_ordinal_score = 3, 1.0, 0.0))) / 2.0) * (1.0 - ((AVG(IF(s.human_ordinal_score = 3, 1.0, 0.0)) + AVG(IF(s.judge_ordinal_score = 3, 1.0, 0.0))) / 2.0))
      + ((AVG(IF(s.human_ordinal_score = 4, 1.0, 0.0)) + AVG(IF(s.judge_ordinal_score = 4, 1.0, 0.0))) / 2.0) * (1.0 - ((AVG(IF(s.human_ordinal_score = 4, 1.0, 0.0)) + AVG(IF(s.judge_ordinal_score = 4, 1.0, 0.0))) / 2.0))
    ) AS gwet_pe
  FROM `agent_eval.sme_labels` AS s
  LEFT JOIN `agent_eval.rubrics` AS rb ON s.rubric_id = rb.rubric_id
  WHERE s.rule0_pass = TRUE AND s.human_ordinal_score BETWEEN 1 AND 4 AND s.judge_ordinal_score BETWEEN 1 AND 4
  GROUP BY 1, 2
),
sme_dual_gov AS (
  SELECT
    apphub_application_id,
    rubric_id,
    COUNT(*) AS dual_pairs,
    AVG(1.0 - POW(CAST(rater_a_score - rater_b_score AS FLOAT64) / 3.0, 2)) AS sme_p_a,
    (26.0 / 27.0) * (
      ((AVG(IF(rater_a_score = 1, 1.0, 0.0)) + AVG(IF(rater_b_score = 1, 1.0, 0.0))) / 2.0) * (1.0 - ((AVG(IF(rater_a_score = 1, 1.0, 0.0)) + AVG(IF(rater_b_score = 1, 1.0, 0.0))) / 2.0))
      + ((AVG(IF(rater_a_score = 2, 1.0, 0.0)) + AVG(IF(rater_b_score = 2, 1.0, 0.0))) / 2.0) * (1.0 - ((AVG(IF(rater_a_score = 2, 1.0, 0.0)) + AVG(IF(rater_b_score = 2, 1.0, 0.0))) / 2.0))
      + ((AVG(IF(rater_a_score = 3, 1.0, 0.0)) + AVG(IF(rater_b_score = 3, 1.0, 0.0))) / 2.0) * (1.0 - ((AVG(IF(rater_a_score = 3, 1.0, 0.0)) + AVG(IF(rater_b_score = 3, 1.0, 0.0))) / 2.0))
      + ((AVG(IF(rater_a_score = 4, 1.0, 0.0)) + AVG(IF(rater_b_score = 4, 1.0, 0.0))) / 2.0) * (1.0 - ((AVG(IF(rater_a_score = 4, 1.0, 0.0)) + AVG(IF(rater_b_score = 4, 1.0, 0.0))) / 2.0))
    ) AS sme_pe
  FROM `agent_eval.sme_labels`
  WHERE rule0_pass = TRUE AND rater_a_score BETWEEN 1 AND 4 AND rater_b_score BETWEEN 1 AND 4
  GROUP BY 1, 2
),
rubric_irr_gov AS (
  SELECT
    tet.apphub_application_id,
    CASE
      WHEN jr.rubric_id IS NULL OR COALESCE(so.ord_pairs, 0) = 0 THEN CAST(NULL AS FLOAT64)
      WHEN ABS(1.0 - so.gwet_pe) < 1e-12 THEN 1.0
      ELSE SAFE_DIVIDE(so.gwet_p_a - so.gwet_pe, 1.0 - so.gwet_pe)
    END AS gwet_ac2,
    CASE
      WHEN jr.rubric_id IS NULL OR COALESCE(so.ord_pairs, 0) = 0 THEN CAST(NULL AS BOOL)
      ELSE
        so.ord_pairs >= IF(COALESCE(tet.effective_risk_tier, aa.risk_tier, 'RL-1') = 'RL-1', 100, 10)
        AND (CASE WHEN ABS(1.0 - so.gwet_pe) < 1e-12 THEN 1.0 ELSE SAFE_DIVIDE(so.gwet_p_a - so.gwet_pe, 1.0 - so.gwet_pe) END)
          >= CASE COALESCE(tet.effective_risk_tier, aa.risk_tier, 'RL-1') WHEN 'RL-1' THEN 0.85 WHEN 'RL-2' THEN 0.85 WHEN 'RL-4' THEN 0.75 WHEN 'RL-5' THEN 0.75 ELSE 0.80 END
        AND COALESCE(so.binary_cutpoint_agreement, 0.0) >= 0.85
        AND (
          COALESCE(sd.dual_pairs, 0) = 0
          OR (
            sd.dual_pairs >= IF(COALESCE(tet.effective_risk_tier, aa.risk_tier, 'RL-1') = 'RL-1', 100, 10)
            AND COALESCE(CASE WHEN ABS(1.0 - sd.sme_pe) < 1e-12 THEN 1.0 ELSE SAFE_DIVIDE(sd.sme_p_a - sd.sme_pe, 1.0 - sd.sme_pe) END, 0.0)
              >= CASE COALESCE(tet.effective_risk_tier, aa.risk_tier, 'RL-1') WHEN 'RL-1' THEN 0.85 WHEN 'RL-2' THEN 0.85 WHEN 'RL-4' THEN 0.75 WHEN 'RL-5' THEN 0.75 ELSE 0.80 END
          )
        )
    END AS passes_irr_gate
  FROM trace_eff_tier AS tet
  LEFT JOIN active_apps AS aa ON tet.apphub_application_id = aa.app_id
  LEFT JOIN judged_rubrics AS jr ON tet.apphub_application_id = jr.apphub_application_id
  LEFT JOIN sme_ord_gov AS so ON tet.apphub_application_id = so.apphub_application_id AND jr.rubric_id = so.rubric_id
  LEFT JOIN sme_dual_gov AS sd ON tet.apphub_application_id = sd.apphub_application_id AND jr.rubric_id = sd.rubric_id
),
app_irr AS (
  SELECT
    apphub_application_id,
    CASE
      WHEN COUNTIF(gwet_ac2 IS NULL) > 0 THEN CAST(NULL AS FLOAT64)
      ELSE MIN(gwet_ac2)
    END AS gwet_ac2,
    CASE
      WHEN COUNTIF(passes_irr_gate IS NULL) > 0 THEN CAST(NULL AS BOOL)
      ELSE LOGICAL_AND(passes_irr_gate)
    END AS passes_irr_gate
  FROM rubric_irr_gov
  GROUP BY apphub_application_id
)
SELECT
  r.app_id AS apphub_application_id,
  COALESCE(r.risk_tier, 'RL-3b') AS risk_tier,
  COALESCE(wt.autonomy_tier, 'HUMAN_IN_LOOP') AS autonomy_tier,
  COALESCE(wt.lifecycle_track_policy, 'long_term_ga_only') AS lifecycle_track_policy,
  COALESCE(ca.alloc_funded_gsus, wt.funded_gsus, 0.0) AS funded_gsus,
  COALESCE(wt.pt_commitment_months, 0) AS pt_commitment_months,
  COALESCE(wt.spillover_tier, 'standard') AS spillover_tier,
  am.model_name,
  COALESCE(am.model_lifecycle_track, 'long_term_ga') AS model_lifecycle_track,
  CASE COALESCE(r.risk_tier, 'RL-3b')
    WHEN 'RL-1' THEN 0.85
    WHEN 'RL-2' THEN 0.85
    WHEN 'RL-3a' THEN 0.80
    WHEN 'RL-3b' THEN 0.80
    WHEN 'RL-4' THEN 0.75
    WHEN 'RL-5' THEN 0.75
    ELSE 0.80
  END AS required_ac2_gate,
  s.gwet_ac2,
  IF(
    s.gwet_ac2 IS NULL,
    CAST(NULL AS BOOL),
    COALESCE(s.passes_irr_gate, FALSE)
    AND s.gwet_ac2 >= CASE COALESCE(r.risk_tier, 'RL-3b')
      WHEN 'RL-1' THEN 0.85
      WHEN 'RL-2' THEN 0.85
      WHEN 'RL-3a' THEN 0.80
      WHEN 'RL-3b' THEN 0.80
      WHEN 'RL-4' THEN 0.75
      WHEN 'RL-5' THEN 0.75
      ELSE 0.80
    END
  ) AS passes_ac2_gate,
  NOT (
    (
      COALESCE(wt.pt_commitment_months, 0) >= 3
      OR (COALESCE(ca.alloc_funded_gsus, wt.funded_gsus, 0.0) > 0.0 AND COALESCE(wt.lifecycle_track_policy, 'long_term_ga_only') = 'long_term_ga_only')
    )
    AND COALESCE(am.model_lifecycle_track, 'long_term_ga') IN ('short_term_ga', 'preview', 'legacy_sunset')
  ) AS pt_commitment_allowed
FROM active_apps AS r
LEFT JOIN `agent_eval.workload_thresholds` AS wt
  ON r.app_id = wt.apphub_application_id
LEFT JOIN active_alloc AS ca
  ON r.app_id = ca.app_id
LEFT JOIN app_models AS am
  ON r.app_id = am.apphub_application_id
LEFT JOIN app_irr AS s
  ON r.app_id = s.apphub_application_id;

CREATE OR REPLACE VIEW `agent_eval.v_focus_three_pool_hourly_chargeback` AS
WITH active_apps AS (
  SELECT app_id
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry`
  WHERE effective_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY effective_from DESC) = 1
),
active_alloc AS (
  SELECT
    app_id,
    MAX(cost_center) AS cost_center,
    MAX(runtime_pool_id) AS runtime_pool_id,
    SUM(funded_units) AS funded_gsus
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_allocations`
  WHERE effective_to IS NULL AND app_id IS NOT NULL
  GROUP BY app_id
),
app_funding AS (
  SELECT
    a.app_id AS apphub_application_id,
    COALESCE(ca.cost_center, wt.cost_center, 'UNASSIGNED') AS cost_center,
    ca.runtime_pool_id,
    COALESCE(ca.funded_gsus, wt.funded_gsus, 0.0) AS funded_gsus
  FROM active_apps AS a
  LEFT JOIN active_alloc AS ca
    ON a.app_id = ca.app_id
  LEFT JOIN `agent_eval.workload_thresholds` AS wt
    ON a.app_id = wt.apphub_application_id
),
pool_orders AS (
  SELECT
    runtime_pool_id,
    SAFE_DIVIDE(
      SUM(IF(renewal_type = 'FIXED_TERM', committed_units, 0.0)),
      NULLIF(SUM(committed_units), 0.0)
    ) AS fixed_term_order_share,
    SAFE_DIVIDE(
      SUM(IF(renewal_type = 'AUTO_RENEW', committed_units, 0.0)),
      NULLIF(SUM(committed_units), 0.0)
    ) AS auto_renew_order_share
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_pools`
  WHERE effective_to IS NULL
  GROUP BY runtime_pool_id
),
pool_funding AS (
  SELECT COALESCE(SUM(funded_gsus), 0.0) AS pool_total_funded_gsus
  FROM app_funding
),
lane_hourly AS (
  SELECT
    e.usage_hour,
    e.apphub_application_id,
    COALESCE(af.cost_center, 'UNASSIGNED') AS cost_center,
    af.runtime_pool_id,
    COALESCE(af.funded_gsus, 0.0) AS funded_gsus,
    CASE
      WHEN e.finops_traffic_type IN ('production_inference', 'conflict_escalation') THEN 'prod'
      WHEN e.finops_traffic_type IN ('agent_gym_eval', 'autorater_judge') THEN 'eval'
      WHEN e.finops_traffic_type = 'optimize' THEN 'optimize'
      ELSE 'prod'
    END AS traffic_lane,
    SUM(IF(UPPER(e.serving_traffic_type) = 'PROVISIONED_THROUGHPUT', e.weighted_burndown_tokens, 0.0)) AS pt_burndown_tokens,
    SUM(IF(UPPER(e.serving_traffic_type) != 'PROVISIONED_THROUGHPUT', e.weighted_burndown_tokens, 0.0)) AS paygo_burndown_tokens,
    SUM(IF(UPPER(e.serving_traffic_type) != 'PROVISIONED_THROUGHPUT', e.total_cost_usd, 0.0)) AS paygo_spillover_usd,
    SUM(e.weighted_burndown_tokens) AS total_weighted_burndown_tokens,
    SUM(e.total_cost_usd) AS list_equivalent_cost_usd
  FROM `agent_eval.v_capacity_hourly_burndown_export` AS e
  LEFT JOIN app_funding AS af
    ON e.apphub_application_id = af.apphub_application_id
  GROUP BY 1, 2, 3, 4, 5, 6
),
hourly_totals AS (
  SELECT
    lh.*,
    SUM(lh.total_weighted_burndown_tokens) OVER (PARTITION BY lh.usage_hour, lh.apphub_application_id) AS app_hour_burndown_tokens,
    SUM(lh.total_weighted_burndown_tokens) OVER (PARTITION BY lh.usage_hour) AS hour_total_burndown_tokens,
    pf.pool_total_funded_gsus,
    COALESCE(po.fixed_term_order_share, 0.0) AS fixed_term_order_share,
    COALESCE(po.auto_renew_order_share, 0.0) AS auto_renew_order_share
  FROM lane_hourly AS lh
  CROSS JOIN pool_funding AS pf
  LEFT JOIN pool_orders AS po
    ON lh.runtime_pool_id = po.runtime_pool_id
)
SELECT
  usage_hour,
  apphub_application_id,
  cost_center,
  traffic_lane,
  funded_gsus,
  pt_burndown_tokens,
  paygo_burndown_tokens,
  total_weighted_burndown_tokens,
  fixed_term_order_share,
  auto_renew_order_share,
  (
    traffic_lane IN ('eval', 'optimize')
    AND pt_burndown_tokens > 0.0
    AND CAST(FORMAT_TIMESTAMP('%H', usage_hour) AS INT64) BETWEEN 13 AND 23
  ) AS daytime_pt_eval_optimize_violation,
  COALESCE(SAFE_DIVIDE(funded_gsus, NULLIF(pool_total_funded_gsus, 0.0)), 0.0) AS funded_gsu_share,
  COALESCE(SAFE_DIVIDE(app_hour_burndown_tokens, NULLIF(hour_total_burndown_tokens, 0.0)), 0.0) AS demand_share,
  CASE
    WHEN paygo_spillover_usd <= 0.0 THEN 0.0
    WHEN funded_gsus <= 0.0
      OR COALESCE(SAFE_DIVIDE(app_hour_burndown_tokens, NULLIF(hour_total_burndown_tokens, 0.0)), 0.0)
         > COALESCE(SAFE_DIVIDE(funded_gsus, NULLIF(pool_total_funded_gsus, 0.0)), 0.0) + 1e-9
      THEN paygo_spillover_usd
    ELSE 0.0
  END AS pool3_tenant_burst_spillover_usd,
  CASE
    WHEN paygo_spillover_usd <= 0.0 THEN 0.0
    WHEN funded_gsus > 0.0
      AND COALESCE(SAFE_DIVIDE(app_hour_burndown_tokens, NULLIF(hour_total_burndown_tokens, 0.0)), 0.0)
         <= COALESCE(SAFE_DIVIDE(funded_gsus, NULLIF(pool_total_funded_gsus, 0.0)), 0.0) + 1e-9
      THEN paygo_spillover_usd
    ELSE 0.0
  END AS pool3_involuntary_spillover_usd,
  list_equivalent_cost_usd
FROM hourly_totals;

CREATE OR REPLACE VIEW `agent_eval.v_interaction_session_lineage` AS
WITH session_traces AS (
  SELECT
    apphub_application_id,
    session_id,
    trace_id,
    MIN(turn_index) AS explicit_turn_index,
    MIN(start_time) AS trace_start_time,
    MAX(end_time) AS trace_end_time,
    SUM(input_tokens) AS trace_input_tokens,
    SUM(cached_input_tokens) AS trace_cached_tokens,
    SUM(output_tokens + thinking_tokens) AS trace_output_tokens,
    SUM(COALESCE(span_cost_usd, 0.0)) AS trace_cost_usd,
    LOGICAL_OR(COALESCE(thought_signature_present, FALSE)) AS thought_signature_present,
    MAX(tool_definitions_sha256) AS tool_definitions_sha256,
    LOGICAL_AND(rule0_pass AND status_code != 'ERROR') AS turn_ok
  FROM `agent_eval.v_trace_waterfall`
  WHERE session_id IS NOT NULL AND session_id != ''
  GROUP BY apphub_application_id, session_id, trace_id
),
ranked_turns AS (
  SELECT
    apphub_application_id,
    session_id,
    trace_id,
    COALESCE(
      explicit_turn_index,
      ROW_NUMBER() OVER (PARTITION BY apphub_application_id, session_id ORDER BY trace_start_time, trace_id) - 1
    ) AS resolved_turn_index,
    trace_start_time,
    trace_end_time,
    trace_input_tokens,
    trace_cached_tokens,
    trace_output_tokens,
    trace_cost_usd,
    thought_signature_present,
    tool_definitions_sha256,
    turn_ok
  FROM session_traces
)
SELECT
  apphub_application_id,
  session_id,
  COUNT(DISTINCT resolved_turn_index) AS turn_count,
  MAX(resolved_turn_index) AS max_turn_index,
  COUNT(DISTINCT trace_id) AS trace_count,
  MIN(trace_start_time) AS session_start_time,
  MAX(trace_end_time) AS session_end_time,
  TIMESTAMP_DIFF(MAX(trace_end_time), MIN(trace_start_time), MILLISECOND) AS session_duration_ms,
  SUM(trace_input_tokens) AS cumulative_input_tokens,
  SUM(trace_output_tokens) AS cumulative_output_tokens,
  SUM(trace_cost_usd) AS cumulative_cost_usd,
  SAFE_DIVIDE(SUM(trace_cached_tokens), NULLIF(SUM(trace_input_tokens), 0)) AS session_cache_hit_ratio,
  LOGICAL_AND(thought_signature_present) AS all_turns_thought_signature_present,
  COUNTIF(NOT thought_signature_present) AS missing_thought_signature_turns,
  COUNT(DISTINCT tool_definitions_sha256) > 1 AS tool_definitions_churn_detected,
  LOGICAL_AND(turn_ok) AS session_rule0_pass,
  MIN(IF(NOT turn_ok, resolved_turn_index, NULL)) AS first_failure_turn_index
FROM ranked_turns
GROUP BY apphub_application_id, session_id;

CREATE OR REPLACE VIEW `agent_eval.v_nudge_feedback_summary` AS
SELECT
  COALESCE(trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
  nudge_id,
  COUNT(*) AS feedback_events,
  COUNTIF(event_type = 'feedback.thumbs_up') AS thumbs_up_count,
  COUNTIF(event_type = 'feedback.thumbs_down') AS thumbs_down_count,
  COUNTIF(event_type = 'outcome.converted') AS converted_count,
  SAFE_DIVIDE(
    COUNTIF(event_type = 'feedback.thumbs_up'),
    NULLIF(COUNTIF(event_type IN ('feedback.thumbs_up', 'feedback.thumbs_down')), 0)
  ) AS positive_feedback_rate,
  ARRAY_AGG(feedback_reason IGNORE NULLS ORDER BY event_time DESC LIMIT 1)[SAFE_OFFSET(0)] AS latest_feedback_reason
FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.events_open`(
  TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
  CURRENT_TIMESTAMP()
)
WHERE nudge_id IS NOT NULL
GROUP BY 1, 2;

CREATE OR REPLACE VIEW `agent_eval.v_live_slo_and_429_breaches` AS
WITH bucketed AS (
  SELECT
    TIMESTAMP_SECONDS(300 * DIV(UNIX_SECONDS(w.start_time), 300)) AS window_5m_start,
    w.apphub_application_id,
    COUNT(*) AS span_count,
    COUNTIF(w.http_status_code = 429) AS http_429_count,
    COUNTIF(NOT w.rule0_pass OR w.status_code = 'ERROR') AS rule0_fail_count,
    APPROX_QUANTILES(w.latency_ms, 100)[OFFSET(95)] AS p95_latency_ms
  FROM `agent_eval.v_trace_waterfall` AS w
  GROUP BY window_5m_start, w.apphub_application_id
),
burst_5m AS (
  SELECT
    TIMESTAMP_SECONDS(300 * DIV(UNIX_SECONDS(minute_utc), 300)) AS window_5m_start,
    COALESCE(trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
    MAX(peak_burst_ratio) AS peak_burst_ratio,
    SUM(gateway_failover_count) AS gateway_failover_count,
    SUM(caller_retry_count) AS burst_caller_retry_count
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.burst_metrics_1m`
  GROUP BY 1, 2
)
SELECT
  b.window_5m_start,
  b.apphub_application_id,
  b.span_count,
  b.http_429_count,
  100.0 * SAFE_DIVIDE(b.http_429_count, NULLIF(b.span_count, 0)) AS http_429_rate_pct,
  100.0 * SAFE_DIVIDE(b.rule0_fail_count, NULLIF(b.span_count, 0)) AS rule0_fail_rate_pct,
  b.p95_latency_ms,
  COALESCE(wt.sla_latency_ms, 5000.0) AS sla_latency_ms,
  COALESCE(bm.peak_burst_ratio, 0.0) AS peak_burst_ratio,
  COALESCE(bm.gateway_failover_count, 0) AS gateway_failover_count,
  COALESCE(bm.burst_caller_retry_count, 0) AS burst_caller_retry_count,
  (
    b.p95_latency_ms > COALESCE(wt.sla_latency_ms, 5000.0)
    OR 100.0 * SAFE_DIVIDE(b.http_429_count, NULLIF(b.span_count, 0)) >= 1.0
    OR 100.0 * SAFE_DIVIDE(b.rule0_fail_count, NULLIF(b.span_count, 0)) >= 5.0
  ) AS is_breach,
  CASE
    WHEN 100.0 * SAFE_DIVIDE(b.http_429_count, NULLIF(b.span_count, 0)) >= 1.0 THEN 'HTTP_429_STORM'
    WHEN b.p95_latency_ms > COALESCE(wt.sla_latency_ms, 5000.0) THEN 'SLA_LATENCY'
    WHEN 100.0 * SAFE_DIVIDE(b.rule0_fail_count, NULLIF(b.span_count, 0)) >= 5.0 THEN 'RULE0_SPIKE'
    ELSE 'OK'
  END AS breach_reason
FROM bucketed AS b
LEFT JOIN `agent_eval.workload_thresholds` AS wt
  ON b.apphub_application_id = wt.apphub_application_id
LEFT JOIN burst_5m AS bm
  ON b.window_5m_start = bm.window_5m_start
  AND b.apphub_application_id = bm.apphub_application_id;

CREATE OR REPLACE VIEW `agent_eval.v_lob_model_lifecycle_export` AS
WITH active_apps AS (
  SELECT app_id, business_unit, risk_tier
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry`
  WHERE effective_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY effective_from DESC) = 1
),
active_allowed AS (
  SELECT
    model,
    MAX(lifecycle_tier) AS lifecycle_tier,
    MIN(eol_date) AS eol_date,
    MAX(migration_target_model) AS migration_target_model,
    LOGICAL_AND(zdr_allowed) AS zdr_allowed
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.allowed_models`
  WHERE effective_to IS NULL
  GROUP BY model
)
SELECT
  COALESCE(ar.business_unit, w.business_unit, 'unassigned_bu') AS business_unit,
  w.apphub_application_id,
  COALESCE(ar.risk_tier, 'RL-3b') AS risk_tier,
  w.model_name,
  w.provider,
  COALESCE(am.lifecycle_tier, w.lifecycle_track, 'long_term_ga') AS lifecycle_track,
  am.eol_date,
  am.migration_target_model,
  COALESCE(am.zdr_allowed, TRUE) AS zdr_allowed,
  COUNT(*) AS call_count,
  SUM(w.weighted_burndown_tokens) AS weighted_burndown_tokens,
  SUM(COALESCE(w.span_cost_usd, 0.0)) AS total_cost_usd
FROM `agent_eval.v_trace_waterfall` AS w
LEFT JOIN active_apps AS ar
  ON w.apphub_application_id = ar.app_id
LEFT JOIN active_allowed AS am
  ON IF(
    STARTS_WITH(REGEXP_REPLACE(w.model_name, r'^.*/', ''), 'text-embedding-'),
    REGEXP_REPLACE(w.model_name, r'^.*/', ''),
    REGEXP_REPLACE(REGEXP_REPLACE(w.model_name, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '')
  ) = am.model
WHERE w.model_name IS NOT NULL AND w.model_name != 'none'
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9;
