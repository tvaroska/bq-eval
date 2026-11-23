-- BigQuery Agent Evaluation, Observability & FinOps Enterprise Extension Views
--   * Zero duplicate view names with `sql/step1_trace_views.sql` (deploy Step 1 first)
--   * Joins `model_pricing_catalog`, `v_default_model_pricing`, `app_registry`,
--     `eval_results`, `sme_labels`, `inference_request_logs`, and `bq_query_jobs`
--   * Domain-agnostic core: per-app storage overrides live in `app_registry.storage_usd_per_outcome`

-- View 1: Span-Level Waterfall with Effective-Dated 1P/3P Pricing, Burndown Weights & PayGo Multipliers
CREATE OR REPLACE VIEW `agent_eval.v_trace_waterfall` AS
WITH priced_spans AS (
  SELECT
    s.trace_id,
    s.span_id,
    s.parent_span_id,
    s.span_name,
    s.gcp_apphub_application_id,
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
    s.serving_traffic_type,
    s.request_type_header,
    s.http_status_code,
    s.outcome_id,
    s.outcome_owner,
    s.source_mode,
    s.skill_id,
    s.skill_version,
    s.entity_id,
    s.session_id,
    s.turn_index,
    s.start_time,
    s.end_time,
    s.receive_time,
    s.latency_ms,
    s.input_tokens,
    s.cached_input_tokens,
    s.cache_write_5m_tokens,
    s.cache_write_1h_tokens,
    s.output_tokens,
    s.thinking_tokens,
    s.attributes,
    m.model_name AS m_model_name,
    m.provider AS m_provider,
    m.lifecycle_track AS m_lifecycle_track,
    m.input_usd_per_1m AS m_input_usd_per_1m,
    m.cached_input_usd_per_1m AS m_cached_input_usd_per_1m,
    m.cache_write_5m_usd_per_1m AS m_cache_write_5m_usd_per_1m,
    m.cache_write_1h_usd_per_1m AS m_cache_write_1h_usd_per_1m,
    m.output_usd_per_1m AS m_output_usd_per_1m,
    m.long_context_threshold_tokens AS m_long_context_threshold_tokens,
    m.long_context_input_usd_per_1m AS m_long_context_input_usd_per_1m,
    m.long_context_cached_usd_per_1m AS m_long_context_cached_usd_per_1m,
    m.long_context_output_usd_per_1m AS m_long_context_output_usd_per_1m,
    m.burndown_uncached_weight AS m_burndown_uncached_weight,
    m.burndown_cached_weight AS m_burndown_cached_weight,
    m.burndown_cache_write_5m_weight AS m_burndown_cache_write_5m_weight,
    m.burndown_cache_write_1h_weight AS m_burndown_cache_write_1h_weight,
    m.burndown_output_weight AS m_burndown_output_weight,
    m.burndown_long_uncached_weight AS m_burndown_long_uncached_weight,
    m.burndown_long_cached_weight AS m_burndown_long_cached_weight,
    m.burndown_long_output_weight AS m_burndown_long_output_weight,
    m.priority_paygo_multiplier AS m_priority_paygo_multiplier,
    m.flex_paygo_multiplier AS m_flex_paygo_multiplier
  FROM `agent_eval.otel_spans` AS s
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m
    ON REGEXP_REPLACE(REGEXP_REPLACE(s.model_name, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') = m.model_name
    AND s.start_time >= m.effective_start
    AND s.start_time < m.effective_end
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY s.trace_id, s.span_id
    ORDER BY m.effective_start DESC, m.effective_end ASC
  ) = 1
)
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
  COALESCE(s.m_provider, d.provider, 'google') AS provider,
  COALESCE(s.m_lifecycle_track, d.lifecycle_track, 'long_term_ga') AS lifecycle_track,
  s.tool_name,
  s.tool_call_status,
  s.status_code,
  s.rule0_pass,
  s.finops_traffic_type,
  COALESCE(s.serving_traffic_type, 'STANDARD') AS serving_traffic_type,
  s.request_type_header,
  s.http_status_code,
  s.outcome_id,
  s.outcome_owner,
  s.source_mode,
  s.skill_id,
  s.skill_version,
  s.entity_id,
  s.session_id,
  s.turn_index,
  s.start_time,
  s.end_time,
  s.receive_time,
  s.latency_ms,
  TIMESTAMP_DIFF(s.receive_time, s.end_time, MILLISECOND) AS ingestion_lag_ms,
  s.input_tokens,
  s.cached_input_tokens,
  COALESCE(s.cache_write_5m_tokens, 0) AS cache_write_5m_tokens,
  COALESCE(s.cache_write_1h_tokens, 0) AS cache_write_1h_tokens,
  GREATEST(
    0,
    s.input_tokens
      - s.cached_input_tokens
      - COALESCE(s.cache_write_5m_tokens, 0)
      - COALESCE(s.cache_write_1h_tokens, 0)
  ) AS uncached_input_tokens,
  s.output_tokens,
  s.thinking_tokens,
  CASE
    WHEN s.m_model_name IS NOT NULL THEN (s.m_long_context_threshold_tokens IS NOT NULL AND s.input_tokens > s.m_long_context_threshold_tokens)
    ELSE (d.long_context_threshold_tokens IS NOT NULL AND s.input_tokens > d.long_context_threshold_tokens)
  END AS is_long_context_cliff,
  CASE
    WHEN (
      CASE
        WHEN s.m_model_name IS NOT NULL THEN (s.m_long_context_threshold_tokens IS NOT NULL AND s.input_tokens > s.m_long_context_threshold_tokens)
        ELSE (d.long_context_threshold_tokens IS NOT NULL AND s.input_tokens > d.long_context_threshold_tokens)
      END
    ) THEN
      (COALESCE(s.m_burndown_long_uncached_weight, d.burndown_long_uncached_weight, 2.0) * GREATEST(0, s.input_tokens - s.cached_input_tokens - COALESCE(s.cache_write_5m_tokens, 0) - COALESCE(s.cache_write_1h_tokens, 0)))
      + (COALESCE(s.m_burndown_long_cached_weight, d.burndown_long_cached_weight, 0.2) * s.cached_input_tokens)
      + (COALESCE(s.m_burndown_cache_write_5m_weight, d.burndown_cache_write_5m_weight, 1.0) * COALESCE(s.cache_write_5m_tokens, 0))
      + (COALESCE(s.m_burndown_cache_write_1h_weight, d.burndown_cache_write_1h_weight, 1.0) * COALESCE(s.cache_write_1h_tokens, 0))
      + (COALESCE(s.m_burndown_long_output_weight, d.burndown_long_output_weight, 7.5) * (s.output_tokens + s.thinking_tokens))
    ELSE
      (COALESCE(s.m_burndown_uncached_weight, d.burndown_uncached_weight, 1.0) * GREATEST(0, s.input_tokens - s.cached_input_tokens - COALESCE(s.cache_write_5m_tokens, 0) - COALESCE(s.cache_write_1h_tokens, 0)))
      + (COALESCE(s.m_burndown_cached_weight, d.burndown_cached_weight, 0.1) * s.cached_input_tokens)
      + (COALESCE(s.m_burndown_cache_write_5m_weight, d.burndown_cache_write_5m_weight, 1.0) * COALESCE(s.cache_write_5m_tokens, 0))
      + (COALESCE(s.m_burndown_cache_write_1h_weight, d.burndown_cache_write_1h_weight, 1.0) * COALESCE(s.cache_write_1h_tokens, 0))
      + (COALESCE(s.m_burndown_output_weight, d.burndown_output_weight, 4.0) * (s.output_tokens + s.thinking_tokens))
  END AS weighted_burndown_tokens,
  CASE
    WHEN s.input_tokens = 0
      AND s.output_tokens = 0
      AND s.thinking_tokens = 0
      AND COALESCE(s.cache_write_5m_tokens, 0) = 0
      AND COALESCE(s.cache_write_1h_tokens, 0) = 0 THEN
      0.0
    ELSE
      (
        CASE UPPER(COALESCE(s.serving_traffic_type, 'STANDARD'))
          WHEN 'PRIORITY' THEN COALESCE(s.m_priority_paygo_multiplier, d.priority_paygo_multiplier, 1.8)
          WHEN 'FLEX' THEN COALESCE(s.m_flex_paygo_multiplier, d.flex_paygo_multiplier, 0.5)
          ELSE 1.0
        END
      ) * (
        CASE
          WHEN (
            CASE
              WHEN s.m_model_name IS NOT NULL THEN (s.m_long_context_threshold_tokens IS NOT NULL AND s.input_tokens > s.m_long_context_threshold_tokens)
              ELSE (d.long_context_threshold_tokens IS NOT NULL AND s.input_tokens > d.long_context_threshold_tokens)
            END
          ) THEN
            (GREATEST(0, s.input_tokens - s.cached_input_tokens - COALESCE(s.cache_write_5m_tokens, 0) - COALESCE(s.cache_write_1h_tokens, 0)) / 1e6) * COALESCE(s.m_long_context_input_usd_per_1m, d.long_context_input_usd_per_1m)
            + (s.cached_input_tokens / 1e6) * COALESCE(s.m_long_context_cached_usd_per_1m, d.long_context_cached_usd_per_1m)
            + (COALESCE(s.cache_write_5m_tokens, 0) / 1e6) * COALESCE(s.m_cache_write_5m_usd_per_1m, d.cache_write_5m_usd_per_1m, s.m_long_context_input_usd_per_1m, d.long_context_input_usd_per_1m)
            + (COALESCE(s.cache_write_1h_tokens, 0) / 1e6) * COALESCE(s.m_cache_write_1h_usd_per_1m, d.cache_write_1h_usd_per_1m, s.m_long_context_input_usd_per_1m, d.long_context_input_usd_per_1m)
            + ((s.output_tokens + s.thinking_tokens) / 1e6) * COALESCE(s.m_long_context_output_usd_per_1m, d.long_context_output_usd_per_1m)
          ELSE
            (GREATEST(0, s.input_tokens - s.cached_input_tokens - COALESCE(s.cache_write_5m_tokens, 0) - COALESCE(s.cache_write_1h_tokens, 0)) / 1e6) * COALESCE(s.m_input_usd_per_1m, d.input_usd_per_1m)
            + (s.cached_input_tokens / 1e6) * COALESCE(s.m_cached_input_usd_per_1m, d.cached_input_usd_per_1m)
            + (COALESCE(s.cache_write_5m_tokens, 0) / 1e6) * COALESCE(s.m_cache_write_5m_usd_per_1m, d.cache_write_5m_usd_per_1m, s.m_input_usd_per_1m, d.input_usd_per_1m)
            + (COALESCE(s.cache_write_1h_tokens, 0) / 1e6) * COALESCE(s.m_cache_write_1h_usd_per_1m, d.cache_write_1h_usd_per_1m, s.m_input_usd_per_1m, d.input_usd_per_1m)
            + ((s.output_tokens + s.thinking_tokens) / 1e6) * COALESCE(s.m_output_usd_per_1m, d.output_usd_per_1m)
        END
      )
  END AS span_cost_usd,
  s.attributes
FROM priced_spans AS s
LEFT JOIN `agent_eval.v_default_model_pricing` AS d
  ON REGEXP_REPLACE(REGEXP_REPLACE(s.model_name, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') = d.model_name;

-- -----------------------------------------------------------------------------
-- View 2: Tier 1 Trace-Level Deterministic SQL Evaluation (100% Traces, $0 LLM)
--   * Mirrors Step-1 Rule 0 gate (zero span/tool errors and <= 8 tool calls)
--   * Isolates `'optimize'` traffic lane alongside prod and eval buckets
--   * Enriches with app_registry SLA, RL-1..RL-5 risk tier, and cost center metadata
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
    COUNTIF(
      w.span_kind = 'execute_tool'
      AND (w.status_code = 'ERROR' OR COALESCE(w.tool_call_status, 'SUCCESS') != 'SUCCESS')
    ) AS tool_error_count,
    COUNTIF(w.http_status_code = 429) AS http_429_count,
    COUNTIF(UPPER(COALESCE(w.serving_traffic_type, 'STANDARD')) IN ('PRIORITY', 'STANDARD', 'FLEX')) AS spillover_span_count,
    (
      LOGICAL_AND(w.rule0_pass)
      AND COUNTIF(w.status_code = 'ERROR') = 0
      AND COUNTIF(
        w.span_kind = 'execute_tool'
        AND COALESCE(w.tool_call_status, 'SUCCESS') != 'SUCCESS'
      ) = 0
      AND COUNTIF(w.span_kind = 'execute_tool') <= 8
    ) AS rule0_pass,
    LOGICAL_OR(w.finops_traffic_type = 'conflict_escalation') AS escalated_to_high_reasoning,
    LOGICAL_OR(w.is_long_context_cliff) AS hit_long_context_cliff,
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
    SUM(w.input_tokens) AS total_input_tokens,
    SUM(w.cached_input_tokens) AS total_cached_input_tokens,
    SUM(w.uncached_input_tokens) AS total_uncached_input_tokens,
    SUM(w.output_tokens) AS total_output_tokens,
    SUM(w.thinking_tokens) AS total_thinking_tokens,
    SUM(w.weighted_burndown_tokens) AS weighted_burndown_tokens,
    IF(
      COUNTIF(w.finops_traffic_type = 'production_inference' AND w.span_cost_usd IS NULL) > 0,
      CAST(NULL AS FLOAT64),
      SUM(CASE WHEN w.finops_traffic_type = 'production_inference' THEN w.span_cost_usd ELSE 0.0 END)
    ) AS production_inference_usd,
    IF(
      COUNTIF(w.finops_traffic_type = 'conflict_escalation' AND w.span_cost_usd IS NULL) > 0,
      CAST(NULL AS FLOAT64),
      SUM(CASE WHEN w.finops_traffic_type = 'conflict_escalation' THEN w.span_cost_usd ELSE 0.0 END)
    ) AS conflict_escalation_usd,
    IF(
      COUNTIF(w.finops_traffic_type = 'agent_gym_eval' AND w.span_cost_usd IS NULL) > 0,
      CAST(NULL AS FLOAT64),
      SUM(CASE WHEN w.finops_traffic_type = 'agent_gym_eval' THEN w.span_cost_usd ELSE 0.0 END)
    ) AS agent_gym_eval_usd,
    IF(
      COUNTIF(w.finops_traffic_type = 'autorater_judge' AND w.span_cost_usd IS NULL) > 0,
      CAST(NULL AS FLOAT64),
      SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)
    ) AS span_autorater_judge_usd,
    IF(
      COUNTIF(w.finops_traffic_type = 'optimize' AND w.span_cost_usd IS NULL) > 0,
      CAST(NULL AS FLOAT64),
      SUM(CASE WHEN w.finops_traffic_type = 'optimize' THEN w.span_cost_usd ELSE 0.0 END)
    ) AS optimize_usd,
    IF(
      COUNTIF(
        w.finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge', 'optimize')
        AND w.span_cost_usd IS NULL
      ) > 0,
      CAST(NULL AS FLOAT64),
      SUM(
        CASE
          WHEN w.finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge', 'optimize')
            THEN w.span_cost_usd
          ELSE 0.0
        END
      )
    ) AS unclassified_usd,
    COUNTIF(w.span_cost_usd IS NULL) AS unpriced_spans,
    IF(COUNTIF(w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(w.span_cost_usd)) AS trace_span_cost_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  GROUP BY w.trace_id
)
SELECT
  t.*,
  COALESCE(r.business_unit, 'unassigned_bu') AS business_unit,
  COALESCE(r.sla_latency_ms, 5000.0) AS sla_latency_ms,
  COALESCE(r.cost_center, 'UNASSIGNED') AS cost_center,
  COALESCE(r.risk_tier, 'RL-3b') AS risk_tier,
  COALESCE(r.funded_gsus, 0.0) AS funded_gsus,
  SAFE_DIVIDE(t.total_cached_input_tokens, NULLIF(t.total_input_tokens, 0)) AS cache_hit_ratio,
  SAFE_DIVIDE(t.total_thinking_tokens, NULLIF(t.total_output_tokens + t.total_thinking_tokens, 0)) AS thinking_token_ratio
FROM trace_agg AS t
LEFT JOIN `agent_eval.app_registry` AS r
  ON t.apphub_application_id = r.apphub_application_id;

-- -----------------------------------------------------------------------------
-- View 2B: Attributed BigQuery INFORMATION_SCHEMA.JOBS Query Cost
--   * Rolls up on-demand bytes-billed ($6.25/TiB) and slot-ms query costs per app
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_bq_query_cost_attribution` AS
SELECT
  apphub_application_id,
  COUNT(*) AS query_job_count,
  SUM(total_bytes_billed) AS total_bytes_billed,
  SUM(total_slot_ms) AS total_slot_ms,
  SUM(
    COALESCE(
      NULLIF(query_cost_usd, 0.0),
      CASE
        WHEN pricing_mode = 'capacity_slot' THEN (total_slot_ms / 3600000.0) * 0.04
        ELSE (total_bytes_billed / 1099511627776.0) * 6.25
      END
    )
  ) AS bq_query_cost_usd
FROM `agent_eval.bq_query_jobs`
GROUP BY apphub_application_id;

-- -----------------------------------------------------------------------------
-- View 3: Enterprise Cost per Verified Outcome (`v_enterprise_cost_per_verified_outcome`)
--   * Extends Step-1 `v_cost_per_verified_outcome` with effective-dated pricing,
--     Step-2 `eval_results` judge verdicts & spend, and `INFORMATION_SCHEMA.JOBS`
--     BigQuery query cost folded cleanly into `CPVO`.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_enterprise_cost_per_verified_outcome` AS
WITH deduped_eval_runs AS (
  SELECT *
  FROM `agent_eval.eval_results`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY COALESCE(eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', evaluated_at)), trace_id, rubric_id
    ORDER BY evaluated_at DESC
  ) = 1
),
latest_eval_verdicts AS (
  SELECT
    COALESCE(e.outcome_id, t.outcome_id) AS outcome_id,
    e.binary_pass
  FROM deduped_eval_runs AS e
  LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t
    ON e.trace_id = t.trace_id
  WHERE COALESCE(e.outcome_id, t.outcome_id) IS NOT NULL
    AND e.binary_pass IS NOT NULL
    AND (e.ordinal_score IS NULL OR e.ordinal_score BETWEEN 1 AND 4)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.trace_id, e.rubric_id
    ORDER BY e.evaluated_at DESC
  ) = 1
),
step2_verdict_by_outcome AS (
  SELECT
    outcome_id,
    LOGICAL_AND(binary_pass) AS step2_all_pass
  FROM latest_eval_verdicts
  GROUP BY outcome_id
),
priced_outcome_evals AS (
  SELECT
    e.eval_id,
    COALESCE(e.outcome_id, t.outcome_id) AS outcome_id,
    e.judge_input_tokens,
    e.judge_output_tokens,
    e.judge_thinking_tokens,
    COALESCE(m.input_usd_per_1m, d.input_usd_per_1m) AS input_usd_per_1m,
    COALESCE(m.output_usd_per_1m, d.output_usd_per_1m) AS output_usd_per_1m
  FROM deduped_eval_runs AS e
  LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t
    ON e.trace_id = t.trace_id
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m
    ON REGEXP_REPLACE(REGEXP_REPLACE(e.judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') = m.model_name
    AND e.evaluated_at >= m.effective_start
    AND e.evaluated_at < m.effective_end
  LEFT JOIN `agent_eval.v_default_model_pricing` AS d
    ON REGEXP_REPLACE(REGEXP_REPLACE(e.judge_model, r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '') = d.model_name
  WHERE COALESCE(e.outcome_id, t.outcome_id) IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.eval_id
    ORDER BY m.effective_start DESC, m.effective_end ASC
  ) = 1
),
step2_spend_by_outcome AS (
  SELECT
    outcome_id,
    SUM(
      ((judge_input_tokens / 1e6) * input_usd_per_1m)
      + (((judge_output_tokens + judge_thinking_tokens) / 1e6) * output_usd_per_1m)
    ) AS eval_table_judge_usd
  FROM priced_outcome_evals
  GROUP BY outcome_id
),
step2_judge_by_outcome AS (
  SELECT
    s.outcome_id,
    v.step2_all_pass,
    s.eval_table_judge_usd
  FROM step2_spend_by_outcome AS s
  LEFT JOIN step2_verdict_by_outcome AS v
    ON s.outcome_id = v.outcome_id
),
outcome_spans AS (
  SELECT
    w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(w.source_mode, o.source_mode, 'UNSPECIFIED') AS source_mode,
    w.agent_name,
    COALESCE(o.outcome_status, 'open') AS outcome_status,
    CASE
      WHEN COALESCE(o.outcome_status, 'open') != 'succeeded' THEN FALSE
      ELSE COALESCE(o.is_verified, FALSE) AND COALESCE(j.step2_all_pass, TRUE)
    END AS is_verified,
    COUNT(DISTINCT w.trace_id) AS trace_count,
    COUNT(*) AS span_count,
    SUM(w.input_tokens) AS input_tokens,
    SUM(w.cached_input_tokens) AS cached_input_tokens,
    SUM(w.output_tokens) AS output_tokens,
    SUM(w.thinking_tokens) AS thinking_tokens,
    COUNTIF(w.span_cost_usd IS NULL) AS unpriced_spans,
    IF(
      COUNTIF(w.finops_traffic_type != 'autorater_judge' AND w.span_cost_usd IS NULL) > 0,
      CAST(NULL AS FLOAT64),
      SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)
    ) AS measured_model_cost_usd,
    IF(
      COUNTIF(w.finops_traffic_type = 'autorater_judge' AND w.span_cost_usd IS NULL) > 0,
      CAST(NULL AS FLOAT64),
      SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END)
    ) AS span_judge_cost_usd,
    COALESCE(MAX(j.eval_table_judge_usd), 0.0) AS outcome_table_judge_usd,
    MAX(r.storage_usd_per_outcome) AS app_storage_usd_per_outcome
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o
    ON w.outcome_id = o.outcome_id
  LEFT JOIN step2_judge_by_outcome AS j
    ON w.outcome_id = j.outcome_id
  LEFT JOIN `agent_eval.app_registry` AS r
    ON COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') = r.apphub_application_id
  WHERE w.outcome_id IS NOT NULL
    AND NOT COALESCE(r.is_external_counterparty, FALSE)
    AND LOWER(COALESCE(JSON_VALUE(w.attributes, '$."apphub.external"'), 'false')) != 'true'
  GROUP BY 1, 2, 3, 4, 5, 6
),
weighted_outcome_spans AS (
  SELECT
    os.*,
    SUM(os.span_count) OVER (PARTITION BY os.outcome_id) AS outcome_total_spans,
    SUM(os.span_count) OVER (PARTITION BY os.apphub_application_id) AS app_total_spans,
    SUM(COALESCE(os.span_judge_cost_usd, 0.0)) OVER (PARTITION BY os.outcome_id) AS outcome_span_judge_usd
  FROM outcome_spans AS os
),
allocated_outcome_spans AS (
  SELECT
    os.*,
    os.span_judge_cost_usd
      + GREATEST(0.0, os.outcome_table_judge_usd - os.outcome_span_judge_usd)
      * COALESCE(SAFE_DIVIDE(os.span_count, os.outcome_total_spans), 0.0) AS measured_judge_cost_usd,
    (
      COALESCE(inf.estimated_compute_usd, 0.0)
      + COALESCE(inf.explicit_storage_usd, os.app_storage_usd_per_outcome, inf.estimated_storage_usd, 0.0)
    ) * COALESCE(SAFE_DIVIDE(os.span_count, os.outcome_total_spans), 0.0) AS allocated_infra_cost_usd,
    COALESCE(qc.bq_query_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(os.span_count, os.app_total_spans), 0.0) AS allocated_bq_query_cost_usd
  FROM weighted_outcome_spans AS os
  LEFT JOIN `agent_eval.v_infra_cost_estimate` AS inf
    ON os.outcome_id = inf.outcome_id
  LEFT JOIN `agent_eval.v_bq_query_cost_attribution` AS qc
    ON os.apphub_application_id = qc.apphub_application_id
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
  SUM(os.unpriced_spans) AS unpriced_spans,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.measured_model_cost_usd)) AS measured_model_cost_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.measured_judge_cost_usd)) AS measured_judge_cost_usd,
  SUM(os.allocated_infra_cost_usd) AS estimated_infra_cost_usd,
  SUM(os.allocated_bq_query_cost_usd) AS bq_query_cost_usd,
  IF(SUM(os.unpriced_spans) > 0, CAST(NULL AS FLOAT64), SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd)) AS total_ai_cost_usd,
  IF(
    SUM(os.unpriced_spans) > 0,
    CAST(NULL AS FLOAT64),
    SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd + os.allocated_infra_cost_usd + os.allocated_bq_query_cost_usd)
  ) AS total_cost_with_infra_usd,
  IF(
    SUM(os.unpriced_spans) > 0,
    CAST(NULL AS FLOAT64),
    SAFE_DIVIDE(
      SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd),
      NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0)
    )
  ) AS cost_per_verified_outcome_ai_usd,
  IF(
    SUM(os.unpriced_spans) > 0,
    CAST(NULL AS FLOAT64),
    SAFE_DIVIDE(
      SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd + os.allocated_infra_cost_usd + os.allocated_bq_query_cost_usd),
      NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0)
    )
  ) AS cost_per_verified_outcome_usd
FROM allocated_outcome_spans AS os
GROUP BY
  os.apphub_application_id,
  os.source_mode,
  os.agent_name;

-- -----------------------------------------------------------------------------
-- View 4: External Counterparty Telemetry & Spend (`v_counterparty_spend`)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_counterparty_spend` AS
SELECT
  w.apphub_application_id,
  w.service_name,
  w.agent_name,
  COALESCE(w.source_mode, 'UNSPECIFIED') AS source_mode,
  COUNT(DISTINCT w.trace_id) AS distinct_traces,
  COUNT(*) AS span_count,
  SUM(w.input_tokens) AS total_input_tokens,
  SUM(w.output_tokens) AS total_output_tokens,
  SUM(w.thinking_tokens) AS total_thinking_tokens,
  SUM(w.span_cost_usd) AS external_model_cost_usd
FROM `agent_eval.v_trace_waterfall` AS w
LEFT JOIN `agent_eval.app_registry` AS r
  ON w.apphub_application_id = r.apphub_application_id
WHERE COALESCE(r.is_external_counterparty, FALSE)
   OR LOWER(COALESCE(JSON_VALUE(w.attributes, '$."apphub.external"'), 'false')) = 'true'
GROUP BY
  w.apphub_application_id,
  w.service_name,
  w.agent_name,
  source_mode;

-- -----------------------------------------------------------------------------
-- View 5: Unified Tier 1-3 Calibrated Quality & 5-Bucket FinOps Summary
--   * Conditional Rule 0 conditioning + StratPPI + Quadratic Gwet's AC2 + Rogan-Gladen
--   * Enforces RL-1..RL-5 tiered AC2 IRR gates (0.85 / 0.80 / 0.75) from app_registry
--   * Isolates `'optimize'` and `bq_query_cost_usd` inside total_spend_usd
-- -----------------------------------------------------------------------------
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
