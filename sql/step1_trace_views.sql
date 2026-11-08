-- =============================================================================
-- BQ Eval — Step 1: Static SQL Views & Queries Over Existing Traces (M1 EVAL-P11)
--   * 0 New Tables Required (runs directly over native _AllSpans via otel_spans)
--   * No Agentic Skill Required (100% static, copy-paste SQL views & queries)
--   * Outcome attribution (EVAL-P11): outcome_id & outcome_owner via W3C baggage
--   * Delivers Cost per Verified Outcome, Attribution Coverage, and Span Coverage
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS `agent_eval`
OPTIONS (
  description = 'BQ Eval: Step 1-3 progressive trace evaluation, rubric catalog, and cost management.'
);

-- -----------------------------------------------------------------------------
-- View 1A: Span-Level Waterfall with Portable App/BU ID & Built-In Default Pricing
--   * Works with zero extra tables (no app_registry or model_pricing_catalog required)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step1_trace_waterfall` AS
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
  COALESCE(
    JSON_VALUE(s.attributes, '$."gcp.apphub.business_unit"'),
    JSON_VALUE(s.attributes, '$."business_unit"'),
    'unassigned_bu'
  ) AS business_unit,
  COALESCE(
    JSON_VALUE(s.attributes, '$."gcp.apphub.cost_center"'),
    JSON_VALUE(s.attributes, '$."cost_center"'),
    'UNASSIGNED'
  ) AS cost_center,
  s.service_name,
  s.agent_name,
  s.otel_span_kind,
  s.span_kind,
  s.model_name,
  s.tool_name,
  s.tool_call_status,
  s.status_code,
  COALESCE(s.rule0_pass, s.status_code != 'ERROR') AS rule0_pass,
  COALESCE(s.finops_traffic_type, 'production_inference') AS finops_traffic_type,
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
  -- Span-level weighted burndown tokens (1.0x/0.1x/4.0x <=200K; 2.0x/0.2x/7.5x >200K)
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
  -- Built-in default Gemini pricing (USD) so Step 1 requires 0 extra pricing tables
  CASE
    WHEN s.model_name IS NULL OR s.model_name = 'none' OR (s.input_tokens = 0 AND s.output_tokens = 0 AND s.thinking_tokens = 0) THEN
      0.0
    WHEN s.model_name = 'gemini-4-argon' AND s.input_tokens > 200000 THEN
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * 2.50
      + (s.cached_input_tokens / 1e6) * 0.250
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * 18.75
    WHEN s.model_name = 'gemini-4-argon' THEN
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * 1.25
      + (s.cached_input_tokens / 1e6) * 0.125
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * 5.00
    WHEN s.model_name = 'gemini-3.5-flash-lite' AND s.input_tokens > 200000 THEN
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * 0.15
      + (s.cached_input_tokens / 1e6) * 0.015
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * 1.125
    WHEN s.model_name = 'gemini-3.5-flash-lite' THEN
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * 0.075
      + (s.cached_input_tokens / 1e6) * 0.0075
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * 0.30
    WHEN s.input_tokens > 200000 THEN
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * 0.30
      + (s.cached_input_tokens / 1e6) * 0.030
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * 2.25
    ELSE
      (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * 0.15
      + (s.cached_input_tokens / 1e6) * 0.015
      + ((s.output_tokens + s.thinking_tokens) / 1e6) * 0.60
  END AS span_cost_usd,
  s.attributes
FROM `agent_eval.otel_spans` AS s;

-- -----------------------------------------------------------------------------
-- View 1B: Trace-Level Deterministic SQL Evaluation ($0 LLM Judge Cost)
--   * Resolves apphub_application_id from outcome_owner / root span (parent_span_id IS NULL)
--     with fallback to gcp_apphub_application_id and 'unregistered_app'
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step1_trace_eval` AS
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
  ANY_VALUE(w.business_unit) AS business_unit,
  ANY_VALUE(w.cost_center) AS cost_center,
  MIN(w.start_time) AS trace_start_time,
  MAX(w.end_time) AS trace_end_time,
  MAX(w.receive_time) AS trace_receive_time,
  TIMESTAMP_DIFF(MAX(w.end_time), MIN(w.start_time), MILLISECOND) AS trace_latency_ms,
  MAX(TIMESTAMP_DIFF(w.receive_time, w.end_time, MILLISECOND)) AS ingestion_lag_ms,
  COUNT(*) AS span_count,
  COUNTIF(w.span_kind = 'execute_tool') AS tool_call_count,
  COUNTIF(
    w.span_kind = 'execute_tool'
    AND (w.status_code = 'ERROR' OR COALESCE(w.tool_call_status, 'SUCCESS') != 'SUCCESS')
  ) AS tool_error_count,
  -- Step-1 deterministic pass: zero span/tool errors, rule0_pass = TRUE, and <= 8 tool calls
  (
    LOGICAL_AND(w.rule0_pass)
    AND COUNTIF(w.status_code = 'ERROR') = 0
    AND COUNTIF(
      w.span_kind = 'execute_tool'
      AND COALESCE(w.tool_call_status, 'SUCCESS') != 'SUCCESS'
    ) = 0
    AND COUNTIF(w.span_kind = 'execute_tool') <= 8
  ) AS step1_pass,
  LOGICAL_OR(w.is_long_context_cliff) AS hit_long_context_cliff,
  SUM(w.input_tokens) AS total_input_tokens,
  SUM(w.cached_input_tokens) AS total_cached_input_tokens,
  SUM(w.uncached_input_tokens) AS total_uncached_input_tokens,
  SUM(w.output_tokens) AS total_output_tokens,
  SUM(w.thinking_tokens) AS total_thinking_tokens,
  SAFE_DIVIDE(SUM(w.cached_input_tokens), NULLIF(SUM(w.input_tokens), 0)) AS cache_hit_ratio,
  SUM(w.weighted_burndown_tokens) AS weighted_burndown_tokens,
  SUM(w.span_cost_usd) AS trace_cost_usd
FROM `agent_eval.v_step1_trace_waterfall` AS w
GROUP BY w.trace_id;

-- -----------------------------------------------------------------------------
-- View 1C: Day-1 Cost per Application, Business Unit & Outcome (Compatibility View)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step1_cost_per_outcome` AS
SELECT
  apphub_application_id,
  business_unit,
  cost_center,
  COUNT(*) AS total_traces,
  COUNTIF(step1_pass) AS step1_passing_traces,
  AVG(IF(step1_pass, 1.0, 0.0)) AS step1_pass_rate,
  AVG(IF(tool_error_count = 0, 1.0, 0.0)) AS tool_success_rate,
  APPROX_QUANTILES(trace_latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
  AVG(cache_hit_ratio) AS avg_cache_hit_ratio,
  SUM(trace_cost_usd) AS total_cost_usd,
  SAFE_DIVIDE(SUM(trace_cost_usd), COUNT(*)) AS cost_per_trace_usd,
  SAFE_DIVIDE(SUM(trace_cost_usd), NULLIF(COUNTIF(step1_pass), 0)) AS step1_cost_per_outcome_usd
FROM `agent_eval.v_step1_trace_eval`
GROUP BY apphub_application_id, business_unit, cost_center;

-- -----------------------------------------------------------------------------
-- View 1D: Business Outcomes Closed (`v_outcomes` — EVAL-P11 Rule 3)
--   * Reads zero-duration spans where span_name = 'outcome.closed'
--   * Applies per-workflow verified definition (Decision 19)
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
-- View 1E: Estimated Cloud Run Compute & Storage Infra Cost per Outcome (`v_infra_cost_estimate`)
--   * Decision 15: Server-span duration * list price * configured vCPU/GiB + per-workflow storage
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_infra_cost_estimate` AS
WITH server_span_compute AS (
  SELECT
    w.outcome_id,
    COALESCE(w.outcome_owner, w.apphub_application_id) AS apphub_application_id,
    SUM(w.latency_ms / 1000.0) AS total_server_seconds,
    -- Cloud Run Tier 1 list price proxy: 2 vCPU * $0.00002400/vCPU-s + 2 GiB * $0.00000250/GiB-s = $0.000053/s
    SUM(
      (w.latency_ms / 1000.0)
      * (
        (COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."service.vcpu"') AS FLOAT64), 2.0) * 0.00002400)
        + (COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."service.gib"') AS FLOAT64), 2.0) * 0.00000250)
      )
    ) AS estimated_compute_usd
  FROM `agent_eval.v_step1_trace_waterfall` AS w
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
    WHEN c.apphub_application_id = 'meridian-loan-consolidation' THEN 0.000400 -- ~4 MB * 7yr Archive WORM vault
    WHEN c.apphub_application_id = 'benefits-rfp-procurement' THEN 0.000120 -- ~2 MB * 3yr Standard/Archive
    WHEN c.apphub_application_id = 'kyc-address-verification' THEN 0.000040 -- ~0.5 MB * 5yr Archive KYC retention
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
-- View 1F: Cost per Verified Outcome Across Multi-Agent Traces (`v_cost_per_verified_outcome`)
--   * EVAL-P11 Rule 4 + Decision 15 + Decision 19:
--   * Groups model spend by outcome_id (excluding external counterparty apps),
--     keeps failed/abandoned/escalated outcomes in numerator (no survivor bias),
--     and slices by apphub_application_id, source_mode, and agent_name.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_cost_per_verified_outcome` AS
WITH outcome_spans AS (
  SELECT
    w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(w.source_mode, o.source_mode, 'UNSPECIFIED') AS source_mode,
    w.agent_name,
    COALESCE(o.outcome_status, 'open') AS outcome_status,
    COALESCE(o.is_verified, FALSE) AS is_verified,
    COUNT(DISTINCT w.trace_id) AS trace_count,
    COUNT(*) AS span_count,
    SUM(w.input_tokens) AS input_tokens,
    SUM(w.cached_input_tokens) AS cached_input_tokens,
    SUM(w.output_tokens) AS output_tokens,
    SUM(w.thinking_tokens) AS thinking_tokens,
    SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END) AS measured_model_cost_usd,
    SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END) AS measured_judge_cost_usd
  FROM `agent_eval.v_step1_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o
    ON w.outcome_id = o.outcome_id
  WHERE w.outcome_id IS NOT NULL
    AND COALESCE(w.apphub_application_id, '') NOT LIKE '%external%'
    AND COALESCE(JSON_VALUE(w.attributes, '$."apphub.external"'), 'false') != 'true'
  GROUP BY
    w.outcome_id,
    apphub_application_id,
    source_mode,
    w.agent_name,
    outcome_status,
    is_verified
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
-- View 1G: Outcome Attribution Coverage Gate (`v_attribution_coverage` — EVAL-P11 Rule 4)
--   * Reports attributed vs. unattributed (outcome_id IS NULL) model spend
--   * Gate: attribution_coverage_pct >= 99.0 for registered applications
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
FROM `agent_eval.v_step1_trace_waterfall` AS w
GROUP BY w.apphub_application_id;

-- -----------------------------------------------------------------------------
-- View 1H: Day-1 vs. Day-2 Skill Lineage ROI (`v_skill_lineage_roi` — Decision 6 & 20)
--   * Compares Day-1 (v1.0.0) vs. Day-2 (v1.1.0 after Agent Gym) cost and human reviews
--     per verified outcome grouped by outcome_id / outcome_parent_id.
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
  FROM `agent_eval.v_step1_trace_waterfall` AS w
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
-- View 1I: Non-Null OTel Span Attribute Coverage (`v_otel_spans_coverage` — M1 Step 5)
--   * Reports per service_name the share (0..100%) of model-call spans (model_name != 'none')
--     with non-default span_kind, agent_name, model_name, token columns, finops_traffic_type,
--     and outcome_id.
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
  AND (s.input_tokens > 0 OR s.output_tokens > 0 OR s.thinking_tokens > 0)
GROUP BY s.service_name;

-- =============================================================================
-- Ready-to-Run Static Step-1 Queries (Copy & Run in BigQuery Console)
-- =============================================================================

-- Query 1A: Cost per Verified Outcome by App, Source Mode & Agent
-- SELECT * FROM `agent_eval.v_cost_per_verified_outcome` ORDER BY total_cost_with_infra_usd DESC;

-- Query 1B: Attribution Coverage Gate (>= 99% of spend attributed to outcome_id)
-- SELECT * FROM `agent_eval.v_attribution_coverage` ORDER BY total_spend_usd DESC;

-- Query 1C: Failing Trace Triage (Tool Errors, Loops, or Rule 0 Violations)
-- SELECT
--   trace_id, outcome_id, apphub_application_id, business_unit, trace_start_time,
--   trace_latency_ms, ingestion_lag_ms, tool_call_count, tool_error_count, trace_cost_usd
-- FROM `agent_eval.v_step1_trace_eval`
-- WHERE NOT step1_pass
-- ORDER BY trace_start_time DESC
-- LIMIT 100;
