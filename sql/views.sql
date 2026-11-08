-- =============================================================================
-- BigQuery Agent Evaluation, Observability & FinOps Reference Views
-- Standard: EVAL-P04, EVAL-P05, EVAL-P06, EVAL-P07, EVAL-P08, EVAL-P11 (v2026.11.3)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- View 1: Span-Level Waterfall with Portable App ID, Outcome Keys & Long-Context Burndown
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_trace_waterfall` AS
SELECT
  s.trace_id,
  s.span_id,
  s.parent_span_id,
  s.span_name,
  COALESCE(
    s.gcp_apphub_application_id,
    JSON_VALUE(s.attributes, '$."gcp.apphub.application.id"'),
    JSON_VALUE(s.attributes, '$."apphub.application.id"'),
    'unregistered_app'
  ) AS apphub_application_id,
  s.service_name,
  s.agent_name,
  s.otel_span_kind,
  s.span_kind,
  s.model_name,
  s.tool_name,
  s.tool_call_status,
  s.status_code,
  s.rule0_pass,
  s.finops_traffic_type,
  s.outcome_id,
  s.outcome_owner,
  s.source_mode,
  s.skill_id,
  s.skill_version,
  s.entity_id,
  s.start_time,
  s.end_time,
  s.receive_time,
  s.latency_ms,
  TIMESTAMP_DIFF(s.receive_time, s.end_time, MILLISECOND) AS ingestion_lag_ms,
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
    WHEN s.model_name IS NULL OR s.model_name = 'none' OR (s.input_tokens = 0 AND s.output_tokens = 0 AND s.thinking_tokens = 0) THEN
      0.0
    WHEN s.input_tokens > COALESCE(m.long_context_threshold_tokens, 200000) THEN
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * COALESCE(m.long_context_input_usd_per_1m, 0.30)
      + (s.cached_input_tokens / 1e6) * COALESCE(m.long_context_cached_usd_per_1m, 0.03)
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * COALESCE(m.long_context_output_usd_per_1m, 2.25)
    ELSE
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * COALESCE(m.input_usd_per_1m, 0.15)
      + (s.cached_input_tokens / 1e6) * COALESCE(m.cached_input_usd_per_1m, 0.015)
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * COALESCE(m.output_usd_per_1m, 0.60)
  END AS span_cost_usd,
  s.attributes
FROM `agent_eval.otel_spans` AS s
LEFT JOIN `agent_eval.model_pricing_catalog` AS m
  ON s.model_name = m.model_name
  AND s.start_time >= m.effective_start
  AND s.start_time < m.effective_end;

-- -----------------------------------------------------------------------------
-- View 2: Tier 1 Trace-Level Deterministic SQL Evaluation (100% Traces, $0 LLM)
--   * Resolves apphub_application_id from outcome_owner / root span (parent_span_id IS NULL)
--     with fallback to gcp_apphub_application_id and 'unregistered_app'
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_trace_sql_evaluation` AS
WITH trace_agg AS (
  SELECT
    w.trace_id,
    COALESCE(
      MAX(IF(w.parent_span_id IS NULL, NULLIF(w.outcome_owner, ''), NULL)),
      MAX(NULLIF(w.outcome_owner, '')),
      MAX(IF(w.parent_span_id IS NULL, NULLIF(w.apphub_application_id, 'unregistered_app'), NULL)),
      MAX(NULLIF(w.apphub_application_id, 'unregistered_app')),
      'unregistered_app'
    ) AS apphub_application_id,
    MAX(w.outcome_id) AS outcome_id,
    MAX(w.outcome_owner) AS outcome_owner,
    MAX(w.source_mode) AS source_mode,
    MAX(w.skill_id) AS skill_id,
    MAX(w.skill_version) AS skill_version,
    MAX(w.entity_id) AS entity_id,
    MIN(w.start_time) AS trace_start_time,
    MAX(w.end_time) AS trace_end_time,
    MAX(w.receive_time) AS trace_receive_time,
    TIMESTAMP_DIFF(MAX(w.end_time), MIN(w.start_time), MILLISECOND) AS trace_latency_ms,
    MAX(TIMESTAMP_DIFF(w.receive_time, w.end_time, MILLISECOND)) AS ingestion_lag_ms,
    COUNT(*) AS span_count,
    COUNTIF(w.span_kind = 'execute_tool') AS tool_call_count,
    COUNTIF(w.span_kind = 'execute_tool' AND (w.status_code = 'ERROR' OR COALESCE(w.tool_call_status, 'SUCCESS') != 'SUCCESS')) AS tool_error_count,
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
-- View 2B: Step 1 Cost per Application, Business Unit & Outcome (Compatibility View)
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
-- View 2C: Business Outcomes Closed (`v_outcomes` — EVAL-P11 Rule 3 & Decision 19)
--   * Reads zero-duration spans where span_name = 'outcome.closed'
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_outcomes` AS
SELECT
  COALESCE(s.outcome_id, JSON_VALUE(s.attributes, '$."outcome.id"')) AS outcome_id,
  ARRAY_AGG(
    COALESCE(JSON_VALUE(s.attributes, '$."outcome.status"'), 'succeeded')
    ORDER BY s.end_time DESC LIMIT 1
  )[SAFE_OFFSET(0)] AS outcome_status,
  ARRAY_AGG(
    COALESCE(
      JSON_VALUE(s.attributes, '$."outcome.subject"'),
      s.entity_id,
      JSON_VALUE(s.attributes, '$."entity_id"')
    )
    IGNORE NULLS ORDER BY s.end_time DESC LIMIT 1
  )[SAFE_OFFSET(0)] AS outcome_subject,
  ARRAY_AGG(
    JSON_VALUE(s.attributes, '$."outcome.parent_id"')
    IGNORE NULLS ORDER BY s.end_time DESC LIMIT 1
  )[SAFE_OFFSET(0)] AS outcome_parent_id,
  ARRAY_AGG(
    COALESCE(
      s.outcome_owner,
      JSON_VALUE(s.attributes, '$."outcome.owner"'),
      s.gcp_apphub_application_id,
      'unregistered_app'
    )
    ORDER BY s.end_time DESC LIMIT 1
  )[SAFE_OFFSET(0)] AS outcome_owner,
  MAX(s.end_time) AS closed_at,
  ARRAY_AGG(s.trace_id ORDER BY s.end_time DESC LIMIT 1)[SAFE_OFFSET(0)] AS closing_trace_id,
  ARRAY_AGG(s.source_mode IGNORE NULLS ORDER BY s.end_time DESC LIMIT 1)[SAFE_OFFSET(0)] AS source_mode,
  ARRAY_AGG(s.skill_id IGNORE NULLS ORDER BY s.end_time DESC LIMIT 1)[SAFE_OFFSET(0)] AS skill_id,
  ARRAY_AGG(s.skill_version IGNORE NULLS ORDER BY s.end_time DESC LIMIT 1)[SAFE_OFFSET(0)] AS skill_version,
  LOGICAL_AND(
    CASE
      WHEN COALESCE(JSON_VALUE(s.attributes, '$."outcome.status"'), 'succeeded') != 'succeeded' THEN FALSE
      WHEN COALESCE(s.outcome_owner, s.gcp_apphub_application_id, '') = 'kyc-address-verification' THEN
        COALESCE(
          SAFE_CAST(JSON_VALUE(s.attributes, '$."outcome.verified"') AS BOOL),
          SAFE_CAST(JSON_VALUE(s.attributes, '$."human_accepted"') AS BOOL),
          SAFE_CAST(JSON_VALUE(s.attributes, '$."step2_judge_pass"') AS BOOL),
          s.rule0_pass
        )
      WHEN COALESCE(s.outcome_owner, s.gcp_apphub_application_id, '') = 'meridian-loan-consolidation' THEN
        COALESCE(
          SAFE_CAST(JSON_VALUE(s.attributes, '$."outcome.verified"') AS BOOL),
          SAFE_CAST(JSON_VALUE(s.attributes, '$."ledger_locked"') AS BOOL),
          TRUE
        )
      WHEN COALESCE(s.outcome_owner, s.gcp_apphub_application_id, '') = 'benefits-rfp-procurement' THEN
        COALESCE(
          SAFE_CAST(JSON_VALUE(s.attributes, '$."outcome.verified"') AS BOOL),
          SAFE_CAST(JSON_VALUE(s.attributes, '$."schema_valid_quotes"') AS BOOL),
          TRUE
        )
      ELSE COALESCE(SAFE_CAST(JSON_VALUE(s.attributes, '$."outcome.verified"') AS BOOL), s.rule0_pass)
    END
  ) AS is_verified
FROM `agent_eval.otel_spans` AS s
WHERE s.span_name = 'outcome.closed'
  AND COALESCE(s.outcome_id, JSON_VALUE(s.attributes, '$."outcome.id"')) IS NOT NULL
GROUP BY outcome_id;

-- -----------------------------------------------------------------------------
-- View 2D: Estimated Cloud Run Compute & Storage Infra Cost (`v_infra_cost_estimate`)
--   * Decision 15: Server-span duration * list price * service vCPU/GiB + storage constant
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_infra_cost_estimate` AS
WITH server_span_compute AS (
  SELECT
    w.outcome_id,
    COALESCE(w.outcome_owner, w.apphub_application_id) AS apphub_application_id,
    SUM(w.latency_ms / 1000.0) AS total_server_seconds,
    SUM(
      (w.latency_ms / 1000.0)
      * (
        (COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."service.vcpu"') AS FLOAT64), 2.0) * 0.00002400)
        + (COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."service.gib"') AS FLOAT64), 2.0) * 0.00000250)
      )
    ) AS estimated_compute_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  WHERE w.outcome_id IS NOT NULL
    AND w.otel_span_kind IN ('2', 'SPAN_KIND_SERVER', 'SPAN_KIND_INTERNAL')
    AND COALESCE(w.apphub_application_id, '') NOT LIKE '%external%'
  GROUP BY w.outcome_id, apphub_application_id
)
SELECT
  c.outcome_id,
  c.apphub_application_id,
  c.total_server_seconds,
  c.estimated_compute_usd,
  CASE
    WHEN c.apphub_application_id = 'meridian-loan-consolidation' THEN 0.000400
    WHEN c.apphub_application_id = 'benefits-rfp-procurement' THEN 0.000120
    WHEN c.apphub_application_id = 'kyc-address-verification' THEN 0.000040
    ELSE 0.000020
  END AS estimated_storage_usd,
  c.estimated_compute_usd + CASE
    WHEN c.apphub_application_id = 'meridian-loan-consolidation' THEN 0.000400
    WHEN c.apphub_application_id = 'benefits-rfp-procurement' THEN 0.000120
    WHEN c.apphub_application_id = 'kyc-address-verification' THEN 0.000040
    ELSE 0.000020
  END AS estimated_infra_cost_usd
FROM server_span_compute AS c;

-- -----------------------------------------------------------------------------
-- View 2E: Cost per Verified Outcome Across Multi-Agent Traces (`v_cost_per_verified_outcome`)
--   * Groups model spend from gen_ai.usage.* (via model_pricing_catalog) by outcome_id
--     + Step-2 judge cost from eval_results joined on outcome_id / trace_id,
--     excluding external-tagged counterparty apps, keeping failed/abandoned/escalated
--     outcomes in the numerator (no survivor bias), and slicing by
--     apphub_application_id, source_mode, and agent_name.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_cost_per_verified_outcome` AS
WITH step2_judge_by_outcome AS (
  SELECT
    COALESCE(e.outcome_id, t.outcome_id) AS outcome_id,
    LOGICAL_AND(e.binary_pass) AS step2_all_pass,
    SUM(
      ((e.judge_input_tokens / 1e6) * COALESCE(m.input_usd_per_1m, 0.15))
      + (((e.judge_output_tokens + e.judge_thinking_tokens) / 1e6) * COALESCE(m.output_usd_per_1m, 0.60))
    ) AS eval_table_judge_usd
  FROM `agent_eval.eval_results` AS e
  LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t
    ON e.trace_id = t.trace_id
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m
    ON e.judge_model = m.model_name
    AND e.evaluated_at >= m.effective_start
    AND e.evaluated_at < m.effective_end
  WHERE COALESCE(e.outcome_id, t.outcome_id) IS NOT NULL
  GROUP BY outcome_id
),
outcome_spans AS (
  SELECT
    w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(w.source_mode, o.source_mode, 'UNSPECIFIED') AS source_mode,
    w.agent_name,
    COALESCE(o.outcome_status, 'open') AS outcome_status,
    CASE
      WHEN COALESCE(o.outcome_status, 'succeeded') != 'succeeded' THEN FALSE
      WHEN COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id) = 'kyc-address-verification' THEN
        COALESCE(j.step2_all_pass, o.is_verified, FALSE)
      ELSE COALESCE(o.is_verified, FALSE)
    END AS is_verified,
    COUNT(DISTINCT w.trace_id) AS trace_count,
    COUNT(*) AS span_count,
    SUM(w.input_tokens) AS input_tokens,
    SUM(w.cached_input_tokens) AS cached_input_tokens,
    SUM(w.output_tokens) AS output_tokens,
    SUM(w.thinking_tokens) AS thinking_tokens,
    SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END) AS measured_model_cost_usd,
    GREATEST(
      SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END),
      COALESCE(MAX(j.eval_table_judge_usd), 0.0)
    ) AS measured_judge_cost_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o
    ON w.outcome_id = o.outcome_id
  LEFT JOIN step2_judge_by_outcome AS j
    ON w.outcome_id = j.outcome_id
  WHERE w.outcome_id IS NOT NULL
    AND COALESCE(w.apphub_application_id, '') NOT LIKE '%external%'
    AND COALESCE(JSON_VALUE(w.attributes, '$."apphub.external"'), 'false') != 'true'
  GROUP BY
    w.outcome_id,
    apphub_application_id,
    source_mode,
    w.agent_name,
    outcome_status,
    o.outcome_owner,
    w.outcome_owner,
    w.apphub_application_id,
    j.step2_all_pass,
    o.is_verified
)
SELECT
  os.apphub_application_id,
  os.source_mode,
  os.agent_name,
  COUNT(DISTINCT os.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)) AS verified_outcomes,
  COUNT(DISTINCT IF(NOT os.is_verified, os.outcome_id, NULL)) AS unverified_or_failed_outcomes,
  SUM(os.trace_count) AS total_traces,
  SUM(os.input_tokens) AS total_input_tokens,
  SUM(os.output_tokens) AS total_output_tokens,
  SUM(os.thinking_tokens) AS total_thinking_tokens,
  SUM(os.measured_model_cost_usd) AS measured_model_cost_usd,
  SUM(os.measured_judge_cost_usd) AS measured_judge_cost_usd,
  SUM(COALESCE(inf.estimated_infra_cost_usd, 0.0)) AS estimated_infra_cost_usd,
  SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd) AS total_ai_cost_usd,
  SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd + COALESCE(inf.estimated_infra_cost_usd, 0.0)) AS total_cost_with_infra_usd,
  SAFE_DIVIDE(
    SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd),
    NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0)
  ) AS cost_per_verified_outcome_ai_usd,
  SAFE_DIVIDE(
    SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd + COALESCE(inf.estimated_infra_cost_usd, 0.0)),
    NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0)
  ) AS cost_per_verified_outcome_usd
FROM outcome_spans AS os
LEFT JOIN `agent_eval.v_infra_cost_estimate` AS inf
  ON os.outcome_id = inf.outcome_id
GROUP BY
  os.apphub_application_id,
  os.source_mode,
  os.agent_name;

-- -----------------------------------------------------------------------------
-- View 2F: Outcome Attribution Coverage Gate (`v_attribution_coverage` — EVAL-P11 Rule 4)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_attribution_coverage` AS
SELECT
  w.apphub_application_id,
  COUNT(DISTINCT w.trace_id) AS total_traces,
  COUNT(DISTINCT IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.trace_id, NULL)) AS attributed_traces,
  COUNT(DISTINCT IF(w.outcome_id IS NULL OR w.outcome_id = '', w.trace_id, NULL)) AS unattributed_traces,
  SUM(IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.span_cost_usd, 0.0)) AS attributed_spend_usd,
  SUM(IF(w.outcome_id IS NULL OR w.outcome_id = '', w.span_cost_usd, 0.0)) AS unattributed_spend_usd,
  SUM(w.span_cost_usd) AS total_spend_usd,
  100.0 * COALESCE(
    SAFE_DIVIDE(
      SUM(IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.span_cost_usd, 0.0)),
      NULLIF(SUM(w.span_cost_usd), 0.0)
    ),
    SAFE_DIVIDE(
      COUNTIF(w.outcome_id IS NOT NULL AND w.outcome_id != ''),
      NULLIF(COUNT(*), 0)
    ),
    0.0
  ) AS attribution_coverage_pct,
  100.0 * COALESCE(
    SAFE_DIVIDE(
      SUM(IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.span_cost_usd, 0.0)),
      NULLIF(SUM(w.span_cost_usd), 0.0)
    ),
    SAFE_DIVIDE(
      COUNTIF(w.outcome_id IS NOT NULL AND w.outcome_id != ''),
      NULLIF(COUNT(*), 0)
    ),
    0.0
  ) >= 99.0 AS passes_coverage_gate
FROM `agent_eval.v_trace_waterfall` AS w
GROUP BY w.apphub_application_id;

-- -----------------------------------------------------------------------------
-- View 2G: Day-1 vs. Day-2 Skill Lineage ROI (`v_skill_lineage_roi` — Decision 6 & 20)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_skill_lineage_roi` AS
WITH deal_outcomes AS (
  SELECT
    COALESCE(o.outcome_parent_id, w.outcome_id) AS deal_outcome_id,
    w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id) AS apphub_application_id,
    COALESCE(w.skill_id, o.skill_id, 'loan-payoff-statement') AS skill_id,
    COALESCE(w.skill_version, o.skill_version, '1.0.0') AS skill_version,
    COALESCE(o.is_verified, FALSE) AS is_verified,
    LOGICAL_OR(w.finops_traffic_type = 'conflict_escalation' OR w.model_name = 'gemini-4-argon') AS escalated_to_argon,
    COUNTIF(
      COALESCE(JSON_VALUE(w.attributes, '$."hitl.review"'), '') = 'true'
      OR w.span_name LIKE '%overwatch%'
      OR w.finops_traffic_type = 'conflict_escalation'
    ) AS human_review_spans,
    SUM(CASE WHEN w.finops_traffic_type != 'agent_gym_eval' THEN w.span_cost_usd ELSE 0.0 END) AS deal_model_cost_usd,
    SUM(CASE WHEN w.finops_traffic_type = 'agent_gym_eval' THEN w.span_cost_usd ELSE 0.0 END) AS agent_gym_compile_cost_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o
    ON w.outcome_id = o.outcome_id
  WHERE w.outcome_id IS NOT NULL
  GROUP BY
    deal_outcome_id,
    w.outcome_id,
    apphub_application_id,
    skill_id,
    skill_version,
    is_verified
)
SELECT
  d.apphub_application_id,
  d.skill_id,
  d.skill_version,
  CASE
    WHEN d.skill_version = '1.0.0' THEN 'DAY_1_BASELINE'
    ELSE 'DAY_2_COMPILED_SKILL'
  END AS skill_stage,
  COUNT(DISTINCT d.deal_outcome_id) AS total_deals,
  COUNT(DISTINCT d.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)) AS verified_outcomes,
  COUNTIF(d.escalated_to_argon) AS argon_escalated_outcomes,
  SUM(IF(d.human_review_spans > 0, 1, 0)) AS human_reviews,
  SAFE_DIVIDE(
    SUM(IF(d.human_review_spans > 0, 1, 0)),
    NULLIF(COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)), 0)
  ) AS human_reviews_per_verified_outcome,
  SUM(d.deal_model_cost_usd) AS measured_model_cost_usd,
  SUM(COALESCE(inf.estimated_infra_cost_usd, 0.0)) AS estimated_infra_cost_usd,
  SUM(d.agent_gym_compile_cost_usd) AS one_time_skill_compile_cost_usd,
  SAFE_DIVIDE(
    SUM(d.deal_model_cost_usd + COALESCE(inf.estimated_infra_cost_usd, 0.0)),
    NULLIF(COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)), 0)
  ) AS cost_per_verified_outcome_usd
FROM deal_outcomes AS d
LEFT JOIN `agent_eval.v_infra_cost_estimate` AS inf
  ON d.outcome_id = inf.outcome_id
GROUP BY
  d.apphub_application_id,
  d.skill_id,
  d.skill_version;

-- -----------------------------------------------------------------------------
-- View 2H: Non-Null OTel Span Attribute Coverage (`v_otel_spans_coverage` — M1 Step 5)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_otel_spans_coverage` AS
SELECT
  s.service_name,
  COUNT(*) AS model_call_spans,
  100.0 * AVG(IF(s.span_kind IS NOT NULL AND s.span_kind != 'internal', 1.0, 0.0)) AS span_kind_coverage_pct,
  100.0 * AVG(IF(s.agent_name IS NOT NULL AND s.agent_name != 'unknown_agent', 1.0, 0.0)) AS agent_name_coverage_pct,
  100.0 * AVG(IF(s.model_name IS NOT NULL AND s.model_name != 'none', 1.0, 0.0)) AS model_name_coverage_pct,
  100.0 * AVG(IF(s.input_tokens > 0 OR s.output_tokens > 0 OR s.thinking_tokens > 0, 1.0, 0.0)) AS tokens_coverage_pct,
  100.0 * AVG(
    IF(
      s.finops_traffic_type IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge'),
      1.0,
      0.0
    )
  ) AS finops_traffic_type_coverage_pct,
  100.0 * AVG(IF(s.outcome_id IS NOT NULL AND s.outcome_id != '', 1.0, 0.0)) AS outcome_id_coverage_pct
FROM `agent_eval.otel_spans` AS s
WHERE s.model_name IS NOT NULL
  AND s.model_name != 'none'
GROUP BY s.service_name;

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
