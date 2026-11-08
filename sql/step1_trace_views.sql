-- =============================================================================
-- BQ Eval — Step 1: Static SQL Views & Queries Over Existing Traces
--   * 0 New Tables Required (runs directly over existing OpenTelemetry spans)
--   * No Agentic Skill Required (100% static, copy-paste SQL views & queries)
--   * Optional app.id / business_unit: uses App Hub tags when present, falls back cleanly
--   * Delivers Day-1 Cost per Application, Business Unit & Outcome at $0 LLM judge cost
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
  s.span_kind,
  s.model_name,
  s.tool_name,
  s.tool_call_status,
  s.status_code,
  COALESCE(s.rule0_pass, s.status_code != 'ERROR') AS rule0_pass,
  COALESCE(s.finops_traffic_type, 'production_inference') AS finops_traffic_type,
  s.start_time,
  s.end_time,
  s.latency_ms,
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
  END AS span_cost_usd
FROM `agent_eval.otel_spans` AS s;

-- -----------------------------------------------------------------------------
-- View 1B: Trace-Level Deterministic SQL Evaluation ($0 LLM Judge Cost)
--   * Aggregates span health, tool errors, retry loops, latency, and trace spend
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step1_trace_eval` AS
SELECT
  w.trace_id,
  ANY_VALUE(w.apphub_application_id) AS apphub_application_id,
  ANY_VALUE(w.business_unit) AS business_unit,
  ANY_VALUE(w.cost_center) AS cost_center,
  MIN(w.start_time) AS trace_start_time,
  MAX(w.end_time) AS trace_end_time,
  TIMESTAMP_DIFF(MAX(w.end_time), MIN(w.start_time), MILLISECOND) AS trace_latency_ms,
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
-- View 1C: Day-1 Cost per Application, Business Unit & Outcome (0 New Tables)
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

-- =============================================================================
-- Ready-to-Run Static Step-1 Queries (Copy & Run in BigQuery Console)
-- =============================================================================

-- Query 1A: Cost per Application, Business Unit & Outcome Scorecard
-- SELECT * FROM `agent_eval.v_step1_cost_per_outcome` ORDER BY total_cost_usd DESC;

-- Query 1B: Failing Trace Triage (Tool Errors, Loops, or Rule 0 Violations)
-- SELECT
--   trace_id, apphub_application_id, business_unit, trace_start_time,
--   trace_latency_ms, tool_call_count, tool_error_count, trace_cost_usd
-- FROM `agent_eval.v_step1_trace_eval`
-- WHERE NOT step1_pass
-- ORDER BY trace_start_time DESC
-- LIMIT 100;
