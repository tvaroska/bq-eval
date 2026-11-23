-- Capacity, chargeback, governance and live-breach views (FinOps add-on).
-- Deploy after sql/core/: these views read core tables and views.

-- -----------------------------------------------------------------------------
-- View 6: Hourly Weighted Burndown Export for Provisioned Throughput & `capacity` handoff
-- -----------------------------------------------------------------------------
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
  SUM(w.span_cost_usd) AS total_cost_usd
FROM `agent_eval.v_trace_waterfall` AS w
GROUP BY
  usage_hour,
  timestamp,
  w.apphub_application_id,
  w.model_name,
  w.finops_traffic_type,
  w.serving_traffic_type;

-- -----------------------------------------------------------------------------
-- View 7: Client OTel vs. Server-Side `inference_request` Reconciliation
--   * Detects telemetry-vs-billing drift, 429 drops, and involuntary PT spillover
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_inference_request_reconciliation` AS
WITH otel_hourly AS (
  SELECT
    TIMESTAMP_TRUNC(start_time, HOUR) AS usage_hour,
    apphub_application_id,
    model_name,
    COUNT(*) AS otel_span_count,
    COUNTIF(http_status_code = 429) AS otel_429_count,
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
    TIMESTAMP_TRUNC(request_timestamp, HOUR) AS usage_hour,
    apphub_application_id,
    model_name,
    COUNT(*) AS server_request_count,
    COUNTIF(http_status_code = 429) AS server_429_count,
    SUM(input_tokens + output_tokens + thinking_tokens) AS server_total_tokens,
    COUNTIF(
      UPPER(COALESCE(serving_traffic_type, 'STANDARD')) != 'PROVISIONED_THROUGHPUT'
      AND LOWER(COALESCE(request_type_header, '')) != 'shared'
    ) AS pt_eligible_involuntary_spillover_count,
    COUNTIF(LOWER(COALESCE(request_type_header, '')) = 'shared') AS intentional_shared_bypass_count
  FROM `agent_eval.inference_request_logs`
  GROUP BY usage_hour, apphub_application_id, model_name
)
SELECT
  COALESCE(o.usage_hour, s.usage_hour) AS usage_hour,
  COALESCE(o.apphub_application_id, s.apphub_application_id) AS apphub_application_id,
  COALESCE(o.model_name, s.model_name) AS model_name,
  COALESCE(o.otel_span_count, 0) AS otel_span_count,
  COALESCE(s.server_request_count, 0) AS server_request_count,
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

-- -----------------------------------------------------------------------------
-- View 8: `RL-1`-`RL-5` Application Governance & PT Lifecycle Gate
--   * Blocks long-term PT commitments on short_term_ga / preview / legacy_sunset models
--   * Enforces tiered AC2 IRR gates (0.85 RL-1/2, 0.80 RL-3a/3b, 0.75 RL-4/5)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_app_risk_governance` AS
WITH app_models AS (
  SELECT
    apphub_application_id,
    model_name,
    ANY_VALUE(lifecycle_track) AS model_lifecycle_track,
    COUNT(*) AS model_span_count
  FROM `agent_eval.v_trace_waterfall`
  WHERE model_name IS NOT NULL AND model_name != 'none'
  GROUP BY apphub_application_id, model_name
)
SELECT
  r.apphub_application_id,
  COALESCE(r.risk_tier, 'RL-3b') AS risk_tier,
  COALESCE(r.autonomy_tier, 'HUMAN_IN_LOOP') AS autonomy_tier,
  COALESCE(r.lifecycle_track_policy, 'long_term_ga_only') AS lifecycle_track_policy,
  COALESCE(r.funded_gsus, 0.0) AS funded_gsus,
  COALESCE(r.pt_commitment_months, 0) AS pt_commitment_months,
  COALESCE(r.spillover_tier, 'standard') AS spillover_tier,
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
    NULL,
    s.gwet_ac2 >= CASE COALESCE(r.risk_tier, 'RL-3b')
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
    (COALESCE(r.pt_commitment_months, 0) >= 3 OR (COALESCE(r.funded_gsus, 0.0) > 0.0 AND COALESCE(r.lifecycle_track_policy, 'long_term_ga_only') = 'long_term_ga_only'))
    AND COALESCE(am.model_lifecycle_track, 'long_term_ga') IN ('short_term_ga', 'preview', 'legacy_sunset')
  ) AS pt_commitment_allowed
FROM `agent_eval.app_registry` AS r
LEFT JOIN app_models AS am
  ON r.apphub_application_id = am.apphub_application_id
LEFT JOIN `agent_eval.v_agent_eval_and_finops_summary` AS s
  ON r.apphub_application_id = s.apphub_application_id;

-- -----------------------------------------------------------------------------
-- View 9: 3-Pool Hourly FOCUS 1.2 & Spillover Attribution
--   * Isolates prod | eval | optimize lanes and separates Involuntary vs. TenantBurst
--     overflow using app_registry.funded_gsus entitlement share.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_focus_three_pool_hourly_chargeback` AS
WITH lane_hourly AS (
  SELECT
    e.usage_hour,
    e.apphub_application_id,
    COALESCE(r.cost_center, 'UNASSIGNED') AS cost_center,
    COALESCE(r.funded_gsus, 0.0) AS funded_gsus,
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
  LEFT JOIN `agent_eval.app_registry` AS r
    ON e.apphub_application_id = r.apphub_application_id
  GROUP BY 1, 2, 3, 4, 5
),
hourly_totals AS (
  SELECT
    lh.*,
    SUM(lh.total_weighted_burndown_tokens) OVER (PARTITION BY lh.usage_hour) AS hour_total_burndown_tokens,
    SUM(lh.funded_gsus) OVER (PARTITION BY lh.usage_hour) AS hour_total_funded_gsus
  FROM lane_hourly AS lh
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
  COALESCE(SAFE_DIVIDE(funded_gsus, NULLIF(hour_total_funded_gsus, 0.0)), 0.0) AS funded_gsu_share,
  COALESCE(SAFE_DIVIDE(total_weighted_burndown_tokens, NULLIF(hour_total_burndown_tokens, 0.0)), 0.0) AS demand_share,
  CASE
    WHEN paygo_spillover_usd <= 0.0 THEN 0.0
    WHEN funded_gsus <= 0.0
      OR SAFE_DIVIDE(total_weighted_burndown_tokens, NULLIF(hour_total_burndown_tokens, 0.0))
         > COALESCE(SAFE_DIVIDE(funded_gsus, NULLIF(hour_total_funded_gsus, 0.0)), 0.0)
      THEN paygo_spillover_usd
    ELSE 0.0
  END AS pool3_tenant_burst_spillover_usd,
  CASE
    WHEN paygo_spillover_usd <= 0.0 THEN 0.0
    WHEN funded_gsus > 0.0
      AND SAFE_DIVIDE(total_weighted_burndown_tokens, NULLIF(hour_total_burndown_tokens, 0.0))
         <= COALESCE(SAFE_DIVIDE(funded_gsus, NULLIF(hour_total_funded_gsus, 0.0)), 0.0)
      THEN paygo_spillover_usd
    ELSE 0.0
  END AS pool3_involuntary_spillover_usd,
  list_equivalent_cost_usd
FROM hourly_totals;

-- -----------------------------------------------------------------------------
-- View 10: Stateful `/interactions` Session & Turn Lineage
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_interaction_session_lineage` AS
WITH session_spans AS (
  SELECT
    apphub_application_id,
    session_id,
    COALESCE(
      turn_index,
      ROW_NUMBER() OVER (PARTITION BY apphub_application_id, session_id ORDER BY start_time, span_id) - 1
    ) AS resolved_turn_index,
    trace_id,
    start_time,
    end_time,
    input_tokens,
    output_tokens,
    thinking_tokens,
    span_cost_usd,
    (rule0_pass AND status_code != 'ERROR') AS turn_ok
  FROM `agent_eval.v_trace_waterfall`
  WHERE session_id IS NOT NULL AND session_id != ''
)
SELECT
  apphub_application_id,
  session_id,
  COUNT(DISTINCT resolved_turn_index) AS turn_count,
  MAX(resolved_turn_index) AS max_turn_index,
  COUNT(DISTINCT trace_id) AS trace_count,
  MIN(start_time) AS session_start_time,
  MAX(end_time) AS session_end_time,
  TIMESTAMP_DIFF(MAX(end_time), MIN(start_time), MILLISECOND) AS session_duration_ms,
  SUM(input_tokens) AS cumulative_input_tokens,
  SUM(output_tokens + thinking_tokens) AS cumulative_output_tokens,
  SUM(COALESCE(span_cost_usd, 0.0)) AS cumulative_cost_usd,
  LOGICAL_AND(turn_ok) AS session_rule0_pass,
  MIN(IF(NOT turn_ok, resolved_turn_index, NULL)) AS first_failure_turn_index
FROM session_spans
GROUP BY apphub_application_id, session_id;

-- -----------------------------------------------------------------------------
-- View 11: 5-Minute Streaming SLO Latency & HTTP 429 Breach Monitor
-- -----------------------------------------------------------------------------
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
)
SELECT
  b.window_5m_start,
  b.apphub_application_id,
  b.span_count,
  b.http_429_count,
  100.0 * SAFE_DIVIDE(b.http_429_count, NULLIF(b.span_count, 0)) AS http_429_rate_pct,
  100.0 * SAFE_DIVIDE(b.rule0_fail_count, NULLIF(b.span_count, 0)) AS rule0_fail_rate_pct,
  b.p95_latency_ms,
  COALESCE(r.sla_latency_ms, 5000.0) AS sla_latency_ms,
  (
    b.p95_latency_ms > COALESCE(r.sla_latency_ms, 5000.0)
    OR 100.0 * SAFE_DIVIDE(b.http_429_count, NULLIF(b.span_count, 0)) >= 1.0
    OR 100.0 * SAFE_DIVIDE(b.rule0_fail_count, NULLIF(b.span_count, 0)) >= 5.0
  ) AS is_breach,
  CASE
    WHEN 100.0 * SAFE_DIVIDE(b.http_429_count, NULLIF(b.span_count, 0)) >= 1.0 THEN 'HTTP_429_STORM'
    WHEN b.p95_latency_ms > COALESCE(r.sla_latency_ms, 5000.0) THEN 'SLA_LATENCY'
    WHEN 100.0 * SAFE_DIVIDE(b.rule0_fail_count, NULLIF(b.span_count, 0)) >= 5.0 THEN 'RULE0_SPIKE'
    ELSE 'OK'
  END AS breach_reason
FROM bucketed AS b
LEFT JOIN `agent_eval.app_registry` AS r
  ON b.apphub_application_id = r.apphub_application_id;
