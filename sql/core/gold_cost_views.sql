-- Gold Cost Add-On Views & Attempt-Pricing Table Function (`ADR-048`, `ADR-049`).
-- Strict open-only boundary: reads ONLY `silver_open`, `silver_ref`, and `agent_eval`.
-- Never touches `silver_restricted`, `silver_internal`, Shape-3 BigLake, or Bronze tables.

CREATE OR REPLACE TABLE FUNCTION `agent_eval.gold_call_costs`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH mc AS (
    SELECT *
    FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.model_calls`(from_time, to_time)
  ),
  req_attempts AS (
    SELECT
      r.client_request_id, r.span_id, r.attempt_index, r.request_time, r.response_model, r.request_model,
      r.serving_tier, r.tier_source, r.pool_id, r.runtime_pool_id, r.http_status_code, r.traffic_type,
      JSON_VALUE(r.labels, '$.request_type_header') AS request_type_header,
      GREATEST(0, COALESCE(r.input_tokens, 0) - COALESCE(r.cache_read_tokens, 0) - COALESCE(r.cache_write_5m_tokens, 0) - COALESCE(r.cache_write_1h_tokens, 0)) AS att_uncached_tokens,
      COALESCE(r.cache_read_tokens, 0) AS att_cache_read_tokens,
      COALESCE(r.cache_write_5m_tokens, 0) AS att_cw5m_tokens,
      COALESCE(r.cache_write_1h_tokens, 0) AS att_cw1h_tokens,
      COALESCE(r.output_tokens, 0) AS att_output_tokens,
      COALESCE(r.reasoning_tokens, 0) AS att_thinking_tokens,
      GREATEST(COALESCE(r.input_tokens, 0), COALESCE(r.cache_read_tokens, 0) + COALESCE(r.cache_write_5m_tokens, 0) + COALESCE(r.cache_write_1h_tokens, 0)) AS att_prompt_context_tokens
    FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.requests`(from_time, to_time) AS r
  ),
  req_collapsed AS (
    SELECT
      client_request_id, span_id,
      COUNT(*) AS upstream_attempt_count, GREATEST(0, COUNT(*) - 1) AS gateway_failover_attempts,
      SUM(att_uncached_tokens) AS req_uncached_tokens, SUM(att_cache_read_tokens) AS req_cache_read_tokens,
      SUM(att_cw5m_tokens) AS req_cw5m_tokens, SUM(att_cw1h_tokens) AS req_cw1h_tokens,
      SUM(att_output_tokens) AS req_output_tokens, SUM(att_thinking_tokens) AS req_thinking_tokens,
      MAX(att_prompt_context_tokens) AS max_att_prompt_context_tokens,
      ARRAY_AGG(serving_tier IGNORE NULLS ORDER BY attempt_index DESC LIMIT 1)[SAFE_OFFSET(0)] AS req_serving_tier,
      ARRAY_AGG(tier_source IGNORE NULLS ORDER BY attempt_index DESC LIMIT 1)[SAFE_OFFSET(0)] AS req_tier_source,
      ARRAY_AGG(pool_id IGNORE NULLS ORDER BY attempt_index DESC LIMIT 1)[SAFE_OFFSET(0)] AS req_pool_id,
      ARRAY_AGG(runtime_pool_id IGNORE NULLS ORDER BY attempt_index DESC LIMIT 1)[SAFE_OFFSET(0)] AS req_runtime_pool_id,
      ARRAY_AGG(request_type_header IGNORE NULLS ORDER BY attempt_index DESC LIMIT 1)[SAFE_OFFSET(0)] AS req_type_header,
      MAX(IF(http_status_code = 429, 429, http_status_code)) AS req_http_status_code
    FROM req_attempts
    GROUP BY client_request_id, span_id
  ),
  joined_calls AS (
    SELECT
      mc.trace_id, mc.span_id, mc.parent_span_id, mc.outcome_id, mc.trace_owner_app_id, mc.operation_name,
      mc.start_time, mc.end_time, mc.latency_ms, mc.agent_id, mc.agent_name, mc.skill_name, mc.skill_version, mc.skill_sha256,
      COALESCE(mc.response_model, mc.request_model, 'none') AS model_name,
      IF(
        STARTS_WITH(REGEXP_REPLACE(REGEXP_REPLACE(TRIM(COALESCE(mc.response_model, mc.request_model, 'none')), r'@.*$', ''), r'^.*/', ''), 'text-embedding-'),
        REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(TRIM(COALESCE(mc.response_model, mc.request_model, 'none')), r'@.*$', ''), r'^.*/', ''), r'-(?:ga|rc|qcd|preview)$', ''),
        REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(TRIM(COALESCE(mc.response_model, mc.request_model, 'none')), r'@.*$', ''), r'^.*/', ''), r'-(?:\d{3}|ga|rc|qcd|preview)$', '')
      ) AS norm_model_name,
      mc.provider AS span_provider, mc.runtime_lane, mc.state_mode,
      COALESCE(rc.req_serving_tier, mc.serving_tier, 'STANDARD') AS serving_traffic_type,
      COALESCE(rc.req_tier_source, 'span') AS tier_source,
      rc.req_pool_id AS pool_id, rc.req_runtime_pool_id AS runtime_pool_id, rc.req_type_header AS request_type_header,
      COALESCE(mc.traffic_type, 'production_inference') AS finops_traffic_type,
      mc.traffic_type_attested, mc.thought_signature_present, mc.caller_retry_count,
      COALESCE(rc.gateway_failover_attempts, 0) AS gateway_failover_attempts,
      mc.tool_definitions_sha256, COALESCE(mc.http_status_code, rc.req_http_status_code) AS http_status_code,
      mc.error_type, mc.content_missing, mc.model_confidence, mc.cascade_tier, mc.escalation_reason, mc.labels,
      COALESCE(rc.req_uncached_tokens, mc.input_tokens, 0) AS uncached_input_tokens,
      COALESCE(rc.req_cache_read_tokens, mc.cache_read_tokens, 0) AS cached_input_tokens,
      COALESCE(rc.req_cw5m_tokens, mc.cache_write_tokens, 0) AS cache_write_5m_tokens,
      COALESCE(rc.req_cw1h_tokens, 0) AS cache_write_1h_tokens,
      COALESCE(rc.req_output_tokens, mc.output_tokens, 0) AS output_tokens,
      COALESCE(rc.req_thinking_tokens, mc.reasoning_tokens, 0) AS thinking_tokens
    FROM mc
    LEFT JOIN req_collapsed AS rc
      ON mc.client_request_id IS NOT NULL AND mc.client_request_id = rc.client_request_id AND mc.span_id = rc.span_id
  ),
  catalog_matched AS (
    SELECT
      jc.*,
      (
        jc.uncached_input_tokens
        + jc.cached_input_tokens
        + jc.cache_write_5m_tokens
        + jc.cache_write_1h_tokens
      ) AS prompt_context_tokens,
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
      m.flex_paygo_multiplier AS m_flex_paygo_multiplier,
      m.min_cache_tokens AS m_min_cache_tokens,
      ROW_NUMBER() OVER (
        PARTITION BY jc.trace_id, jc.span_id
        ORDER BY m.effective_start DESC, m.effective_end ASC
      ) AS cat_rn
    FROM joined_calls AS jc
    LEFT JOIN `agent_eval.model_pricing_catalog` AS m
      ON jc.norm_model_name = m.model_name
      AND jc.start_time >= m.effective_start
      AND jc.start_time < m.effective_end
  ),
  resolved AS (
    SELECT
      c.*,
      d.model_name AS d_model_name,
      COALESCE(c.m_provider, d.provider, c.span_provider, 'google') AS provider,
      COALESCE(c.m_lifecycle_track, d.lifecycle_track, 'long_term_ga') AS lifecycle_track,
      COALESCE(c.m_min_cache_tokens, d.min_cache_tokens, 0) AS min_cache_tokens,
      CASE
        WHEN c.m_model_name IS NOT NULL THEN c.m_long_context_threshold_tokens
        ELSE d.long_context_threshold_tokens
      END AS eff_cliff_threshold,
      (c.m_model_name IS NOT NULL OR d.model_name IS NOT NULL) AS has_sku_match,
      d.input_usd_per_1m AS d_input_usd_per_1m,
      d.cached_input_usd_per_1m AS d_cached_input_usd_per_1m,
      d.cache_write_5m_usd_per_1m AS d_cache_write_5m_usd_per_1m,
      d.cache_write_1h_usd_per_1m AS d_cache_write_1h_usd_per_1m,
      d.output_usd_per_1m AS d_output_usd_per_1m,
      d.long_context_input_usd_per_1m AS d_long_context_input_usd_per_1m,
      d.long_context_cached_usd_per_1m AS d_long_context_cached_usd_per_1m,
      d.long_context_output_usd_per_1m AS d_long_context_output_usd_per_1m,
      d.burndown_uncached_weight AS d_burndown_uncached_weight,
      d.burndown_cached_weight AS d_burndown_cached_weight,
      d.burndown_cache_write_5m_weight AS d_burndown_cache_write_5m_weight,
      d.burndown_cache_write_1h_weight AS d_burndown_cache_write_1h_weight,
      d.burndown_output_weight AS d_burndown_output_weight,
      d.burndown_long_uncached_weight AS d_burndown_long_uncached_weight,
      d.burndown_long_cached_weight AS d_burndown_long_cached_weight,
      d.burndown_long_output_weight AS d_burndown_long_output_weight,
      d.priority_paygo_multiplier AS d_priority_paygo_multiplier,
      d.flex_paygo_multiplier AS d_flex_paygo_multiplier
    FROM catalog_matched AS c
    LEFT JOIN `agent_eval.v_default_model_pricing` AS d
      ON c.norm_model_name = d.model_name
    WHERE c.cat_rn = 1
  )
  SELECT
    r.trace_id, r.span_id, r.parent_span_id, r.outcome_id, r.trace_owner_app_id, r.operation_name,
    r.start_time, r.end_time, r.latency_ms, r.agent_id, r.agent_name, r.skill_name, r.skill_version, r.skill_sha256,
    r.model_name, r.provider, r.lifecycle_track, r.runtime_lane, r.state_mode, r.serving_traffic_type,
    r.tier_source, r.pool_id, r.runtime_pool_id, r.request_type_header, r.finops_traffic_type,
    r.traffic_type_attested, r.thought_signature_present, r.caller_retry_count, r.gateway_failover_attempts,
    r.tool_definitions_sha256, r.http_status_code, r.error_type, r.content_missing, r.model_confidence,
    r.cascade_tier, r.escalation_reason, r.prompt_context_tokens AS input_tokens,
    r.cached_input_tokens, r.cache_write_5m_tokens, r.cache_write_1h_tokens, r.uncached_input_tokens,
    r.output_tokens, r.thinking_tokens, r.min_cache_tokens,
    (r.min_cache_tokens <= 0 OR r.prompt_context_tokens >= r.min_cache_tokens) AS meets_min_cache_floor,
    (r.eff_cliff_threshold IS NOT NULL AND r.prompt_context_tokens > r.eff_cliff_threshold) AS is_long_context_cliff,
    CASE
      WHEN r.eff_cliff_threshold IS NOT NULL AND r.prompt_context_tokens > r.eff_cliff_threshold THEN
        (COALESCE(r.m_burndown_long_uncached_weight, r.d_burndown_long_uncached_weight, 2.0) * r.uncached_input_tokens)
        + (COALESCE(r.m_burndown_long_cached_weight, r.d_burndown_long_cached_weight, 0.2) * r.cached_input_tokens)
        + ((COALESCE(r.m_burndown_long_uncached_weight, r.d_burndown_long_uncached_weight, 2.0) / COALESCE(NULLIF(COALESCE(r.m_burndown_uncached_weight, r.d_burndown_uncached_weight, 1.0), 0.0), 1.0)) * COALESCE(r.m_burndown_cache_write_5m_weight, r.d_burndown_cache_write_5m_weight, 1.0) * r.cache_write_5m_tokens)
        + ((COALESCE(r.m_burndown_long_uncached_weight, r.d_burndown_long_uncached_weight, 2.0) / COALESCE(NULLIF(COALESCE(r.m_burndown_uncached_weight, r.d_burndown_uncached_weight, 1.0), 0.0), 1.0)) * COALESCE(r.m_burndown_cache_write_1h_weight, r.d_burndown_cache_write_1h_weight, 1.0) * r.cache_write_1h_tokens)
        + (COALESCE(r.m_burndown_long_output_weight, r.d_burndown_long_output_weight, 7.5) * (r.output_tokens + r.thinking_tokens))
      ELSE
        (COALESCE(r.m_burndown_uncached_weight, r.d_burndown_uncached_weight, 1.0) * r.uncached_input_tokens)
        + (COALESCE(r.m_burndown_cached_weight, r.d_burndown_cached_weight, 0.1) * r.cached_input_tokens)
        + (COALESCE(r.m_burndown_cache_write_5m_weight, r.d_burndown_cache_write_5m_weight, 1.0) * r.cache_write_5m_tokens)
        + (COALESCE(r.m_burndown_cache_write_1h_weight, r.d_burndown_cache_write_1h_weight, 1.0) * r.cache_write_1h_tokens)
        + (COALESCE(r.m_burndown_output_weight, r.d_burndown_output_weight, 4.0) * (r.output_tokens + r.thinking_tokens))
    END AS weighted_burndown_tokens,
    CASE
      WHEN r.prompt_context_tokens = 0 AND r.output_tokens = 0 AND r.thinking_tokens = 0 THEN 0.0
      WHEN NOT r.has_sku_match THEN CAST(NULL AS FLOAT64)
      ELSE
        (
          CASE UPPER(COALESCE(r.serving_traffic_type, 'STANDARD'))
            WHEN 'PRIORITY' THEN COALESCE(r.m_priority_paygo_multiplier, r.d_priority_paygo_multiplier, 1.8)
            WHEN 'FLEX' THEN COALESCE(r.m_flex_paygo_multiplier, r.d_flex_paygo_multiplier, 0.5)
            ELSE 1.0
          END
        ) * (
          CASE
            WHEN r.eff_cliff_threshold IS NOT NULL AND r.prompt_context_tokens > r.eff_cliff_threshold THEN
              (r.uncached_input_tokens / 1e6) * COALESCE(r.m_long_context_input_usd_per_1m, r.d_long_context_input_usd_per_1m)
              + (r.cached_input_tokens / 1e6) * COALESCE(r.m_long_context_cached_usd_per_1m, r.d_long_context_cached_usd_per_1m)
              + (r.cache_write_5m_tokens / 1e6) * (COALESCE(r.m_long_context_input_usd_per_1m, r.d_long_context_input_usd_per_1m) * COALESCE(r.m_burndown_cache_write_5m_weight, r.d_burndown_cache_write_5m_weight, 1.0))
              + (r.cache_write_1h_tokens / 1e6) * (COALESCE(r.m_long_context_input_usd_per_1m, r.d_long_context_input_usd_per_1m) * COALESCE(r.m_burndown_cache_write_1h_weight, r.d_burndown_cache_write_1h_weight, 1.0))
              + ((r.output_tokens + r.thinking_tokens) / 1e6) * COALESCE(r.m_long_context_output_usd_per_1m, r.d_long_context_output_usd_per_1m)
            ELSE
              (r.uncached_input_tokens / 1e6) * COALESCE(r.m_input_usd_per_1m, r.d_input_usd_per_1m)
              + (r.cached_input_tokens / 1e6) * COALESCE(r.m_cached_input_usd_per_1m, r.d_cached_input_usd_per_1m)
              + (r.cache_write_5m_tokens / 1e6) * COALESCE(r.m_cache_write_5m_usd_per_1m, r.d_cache_write_5m_usd_per_1m, r.m_input_usd_per_1m, r.d_input_usd_per_1m)
              + (r.cache_write_1h_tokens / 1e6) * COALESCE(r.m_cache_write_1h_usd_per_1m, r.d_cache_write_1h_usd_per_1m, r.m_input_usd_per_1m, r.d_input_usd_per_1m)
              + ((r.output_tokens + r.thinking_tokens) / 1e6) * COALESCE(r.m_output_usd_per_1m, r.d_output_usd_per_1m)
          END
        )
    END AS span_cost_usd,
    r.labels
  FROM resolved AS r
);

CREATE OR REPLACE TABLE FUNCTION `agent_eval.gold_trace_waterfall`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
WITH outs AS (
  SELECT outcome_id, outcome_owner
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcomes_open`(from_time, to_time)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY outcome_id ORDER BY close_time DESC) = 1
),
raw_spans AS (
  SELECT
    1 AS prio, c.trace_id, c.span_id, c.parent_span_id,
    COALESCE(c.operation_name, 'chat') AS span_name,
    c.trace_owner_app_id AS raw_app_id,
    JSON_VALUE(c.labels, '$.service_name') AS call_service_name,
    COALESCE(c.agent_name, 'primary-agent') AS agent_name,
    COALESCE(JSON_VALUE(c.labels, '$.otel_span_kind'), IF(c.parent_span_id IS NULL OR c.operation_name = 'invoke_agent', 'SERVER', 'CLIENT')) AS otel_span_kind,
    CASE WHEN c.operation_name = 'embeddings' THEN 'embeddings' WHEN c.operation_name IN ('invoke_agent', 'execute_tool', 'retrieval') THEN c.operation_name ELSE 'chat' END AS span_kind,
    c.model_name, c.provider, c.lifecycle_track,
    CAST(NULL AS STRING) AS tool_name, CAST(NULL AS STRING) AS tool_call_status,
    CASE WHEN c.error_type IS NOT NULL OR COALESCE(c.http_status_code, 200) >= 400 THEN 'ERROR' ELSE 'OK' END AS status_code,
    (c.error_type IS NULL AND COALESCE(c.http_status_code, 200) < 400) AS call_ok,
    c.finops_traffic_type AS raw_finops_traffic_type, c.serving_traffic_type, c.request_type_header, c.http_status_code,
    c.outcome_id AS raw_outcome_id, JSON_VALUE(c.labels, '$.source_mode') AS raw_source_mode,
    c.skill_name AS raw_skill_id, c.skill_version, c.start_time, c.end_time,
    CAST(c.latency_ms AS FLOAT64) AS latency_ms,
    c.input_tokens, c.cached_input_tokens, c.cache_write_5m_tokens, c.cache_write_1h_tokens, c.uncached_input_tokens,
    c.output_tokens, c.thinking_tokens, c.min_cache_tokens, c.meets_min_cache_floor, c.is_long_context_cliff,
    c.weighted_burndown_tokens, c.span_cost_usd, 0.0 AS sandbox_cpu_sec, 0.0 AS sandbox_mem_gb_sec,
    c.caller_retry_count, c.gateway_failover_attempts, c.pool_id, c.runtime_pool_id, c.tier_source,
    c.thought_signature_present, c.tool_definitions_sha256,
    CAST(NULL AS STRING) AS t_bu, CAST(NULL AS INT64) AS t_app_failed, CAST(NULL AS STRING) AS t_traffic_type,
    CAST(NULL AS STRING) AS t_outcome_id, CAST(NULL AS STRING) AS t_service_name, CAST(NULL AS STRING) AS t_workflow_name,
    CAST(NULL AS STRING) AS t_skill_name, CAST(NULL AS STRING) AS t_session_id, CAST(NULL AS TIMESTAMP) AS t_receive_time
  FROM `agent_eval.gold_call_costs`(from_time, to_time) AS c
  UNION ALL
  SELECT
    2 AS prio, tc.trace_id, tc.span_id, tc.parent_span_id,
    tc.operation_name AS span_name, tc.trace_owner_app_id AS raw_app_id,
    JSON_VALUE(tc.labels, '$.service_name') AS call_service_name,
    COALESCE(tc.agent_name, 'primary-agent') AS agent_name,
    IF(tc.parent_span_id IS NULL, 'INTERNAL', 'CLIENT') AS otel_span_kind,
    IF(tc.operation_name = 'retrieval', 'retrieval', 'execute_tool') AS span_kind,
    'none' AS model_name, 'google' AS provider, 'long_term_ga' AS lifecycle_track,
    tc.tool_name,
    IF(tc.span_status = 'ERROR', COALESCE(JSON_VALUE(tc.labels, '$.tool_error_type'), 'ERROR'), 'SUCCESS') AS tool_call_status,
    IF(tc.span_status = 'ERROR', 'ERROR', 'OK') AS status_code,
    (tc.span_status != 'ERROR') AS call_ok,
    CAST(NULL AS STRING) AS raw_finops_traffic_type, 'STANDARD' AS serving_traffic_type, CAST(NULL AS STRING) AS request_type_header,
    SAFE_CAST(JSON_VALUE(tc.labels, '$.tool_http_status_code') AS INT64) AS http_status_code,
    tc.outcome_id AS raw_outcome_id, CAST(NULL AS STRING) AS raw_source_mode,
    tc.skill_name AS raw_skill_id, tc.skill_version, tc.start_time, tc.end_time,
    CAST(tc.duration_ms AS FLOAT64) AS latency_ms,
    0 AS input_tokens, 0 AS cached_input_tokens, 0 AS cache_write_5m_tokens, 0 AS cache_write_1h_tokens, 0 AS uncached_input_tokens,
    0 AS output_tokens, 0 AS thinking_tokens, 0 AS min_cache_tokens, TRUE AS meets_min_cache_floor, FALSE AS is_long_context_cliff,
    0.0 AS weighted_burndown_tokens, 0.0 AS span_cost_usd,
    COALESCE(tc.sandbox_cpu_sec, 0.0) AS sandbox_cpu_sec, COALESCE(tc.sandbox_mem_gb_sec, 0.0) AS sandbox_mem_gb_sec,
    0 AS caller_retry_count, 0 AS gateway_failover_attempts, CAST(NULL AS STRING) AS pool_id, CAST(NULL AS STRING) AS runtime_pool_id,
    'span' AS tier_source, CAST(NULL AS BOOL) AS thought_signature_present, CAST(NULL AS STRING) AS tool_definitions_sha256,
    CAST(NULL AS STRING) AS t_bu, CAST(NULL AS INT64) AS t_app_failed, CAST(NULL AS STRING) AS t_traffic_type,
    CAST(NULL AS STRING) AS t_outcome_id, CAST(NULL AS STRING) AS t_service_name, CAST(NULL AS STRING) AS t_workflow_name,
    CAST(NULL AS STRING) AS t_skill_name, CAST(NULL AS STRING) AS t_session_id, CAST(NULL AS TIMESTAMP) AS t_receive_time
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.tool_calls_open`(from_time, to_time) AS tc
  UNION ALL
  SELECT
    3 AS prio, t.trace_id, COALESCE(t.root_span_id, CONCAT('root:', t.trace_id)) AS span_id, CAST(NULL AS STRING) AS parent_span_id,
    'invoke_agent' AS span_name, t.trace_owner_app_id AS raw_app_id,
    JSON_VALUE(t.labels, '$.service_name') AS call_service_name, 'primary-agent' AS agent_name,
    'SERVER' AS otel_span_kind, 'invoke_agent' AS span_kind, 'none' AS model_name, 'google' AS provider, 'long_term_ga' AS lifecycle_track,
    CAST(NULL AS STRING) AS tool_name, CAST(NULL AS STRING) AS tool_call_status,
    IF(t.root_status = 'ERROR' OR t.error_count > 0, 'ERROR', 'OK') AS status_code,
    (COALESCE(t.root_status, 'OK') != 'ERROR' AND t.error_count = 0) AS call_ok,
    COALESCE(t.traffic_type, 'production_inference') AS raw_finops_traffic_type, 'STANDARD' AS serving_traffic_type, CAST(NULL AS STRING) AS request_type_header,
    IF(t.throttle_count > 0, 429, CAST(NULL AS INT64)) AS http_status_code,
    t.outcome_id AS raw_outcome_id, JSON_VALUE(t.labels, '$.workflow_name') AS raw_source_mode,
    t.skill_name AS raw_skill_id, CAST(NULL AS STRING) AS skill_version, t.trace_start_time AS start_time, t.trace_end_time AS end_time,
    CAST(GREATEST(0, TIMESTAMP_DIFF(t.trace_end_time, t.trace_start_time, MILLISECOND)) AS FLOAT64) AS latency_ms,
    0 AS input_tokens, 0 AS cached_input_tokens, 0 AS cache_write_5m_tokens, 0 AS cache_write_1h_tokens, 0 AS uncached_input_tokens,
    0 AS output_tokens, 0 AS thinking_tokens, 0 AS min_cache_tokens, TRUE AS meets_min_cache_floor, FALSE AS is_long_context_cliff,
    0.0 AS weighted_burndown_tokens, 0.0 AS span_cost_usd, 0.0 AS sandbox_cpu_sec, 0.0 AS sandbox_mem_gb_sec,
    0 AS caller_retry_count, 0 AS gateway_failover_attempts, CAST(NULL AS STRING) AS pool_id, CAST(NULL AS STRING) AS runtime_pool_id,
    'span' AS tier_source, CAST(NULL AS BOOL) AS thought_signature_present, t.tool_definitions_sha256,
    t.business_unit AS t_bu, IF(t.app_failed, 1, 0) AS t_app_failed, t.traffic_type AS t_traffic_type,
    t.outcome_id AS t_outcome_id, JSON_VALUE(t.labels, '$.service_name') AS t_service_name,
    JSON_VALUE(t.labels, '$.workflow_name') AS t_workflow_name, t.skill_name AS t_skill_name,
    t.conversation_id AS t_session_id, t.last_span_arrival_time AS t_receive_time
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.traces_open`(from_time, to_time) AS t
),
deduped_spans AS (
  SELECT
    r.prio, r.trace_id, r.span_id, r.parent_span_id, r.span_name,
    COALESCE(r.raw_app_id, 'unregistered_app') AS apphub_application_id,
    r.call_service_name, r.agent_name, r.otel_span_kind, r.span_kind, r.model_name, r.provider, r.lifecycle_track,
    r.tool_name, r.tool_call_status, r.status_code, r.call_ok, r.raw_finops_traffic_type, r.serving_traffic_type,
    r.request_type_header, r.http_status_code, r.raw_outcome_id, r.raw_source_mode, r.raw_skill_id, r.skill_version,
    r.start_time, r.end_time, r.latency_ms,
    r.input_tokens, r.cached_input_tokens, r.cache_write_5m_tokens, r.cache_write_1h_tokens, r.uncached_input_tokens,
    r.output_tokens, r.thinking_tokens, r.min_cache_tokens, r.meets_min_cache_floor, r.is_long_context_cliff,
    r.weighted_burndown_tokens, r.span_cost_usd, r.sandbox_cpu_sec, r.sandbox_mem_gb_sec,
    r.caller_retry_count, r.gateway_failover_attempts, r.pool_id, r.runtime_pool_id, r.tier_source,
    r.thought_signature_present, r.tool_definitions_sha256,
    MAX(r.t_bu) OVER (PARTITION BY r.trace_id) AS tr_bu,
    MAX(r.t_app_failed) OVER (PARTITION BY r.trace_id) AS tr_app_failed,
    MAX(r.t_traffic_type) OVER (PARTITION BY r.trace_id) AS tr_traffic_type,
    MAX(r.t_outcome_id) OVER (PARTITION BY r.trace_id) AS tr_outcome_id,
    MAX(r.t_service_name) OVER (PARTITION BY r.trace_id) AS tr_service_name,
    MAX(r.t_workflow_name) OVER (PARTITION BY r.trace_id) AS tr_workflow_name,
    MAX(r.t_skill_name) OVER (PARTITION BY r.trace_id) AS tr_skill_name,
    MAX(r.t_session_id) OVER (PARTITION BY r.trace_id) AS tr_session_id,
    MAX(r.t_receive_time) OVER (PARTITION BY r.trace_id) AS tr_receive_time,
    ROW_NUMBER() OVER (PARTITION BY r.trace_id, r.span_id ORDER BY r.prio ASC) AS span_rn
  FROM raw_spans AS r
),
active_alloc_cc AS (
  SELECT app_id, cost_center
  FROM `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_allocations`
  WHERE effective_to IS NULL AND app_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY effective_from DESC, allocation_id ASC) = 1
)
SELECT
  u.trace_id, u.span_id, u.parent_span_id, u.span_name, u.apphub_application_id,
  COALESCE(u.tr_bu, 'unassigned_bu') AS business_unit,
  COALESCE(ca.cost_center, wt.cost_center, 'UNASSIGNED') AS cost_center,
  COALESCE(u.call_service_name, u.tr_service_name, 'agent-service') AS service_name,
  u.agent_name, u.otel_span_kind, u.span_kind, u.model_name, u.provider, u.lifecycle_track,
  u.tool_name, u.tool_call_status, u.status_code,
  (u.call_ok AND COALESCE(u.tr_app_failed, 0) = 0) AS rule0_pass,
  COALESCE(u.raw_finops_traffic_type, u.tr_traffic_type, 'production_inference') AS finops_traffic_type,
  u.serving_traffic_type, u.request_type_header, u.http_status_code,
  COALESCE(u.raw_outcome_id, u.tr_outcome_id) AS outcome_id,
  COALESCE(outs.outcome_owner, NULLIF(u.apphub_application_id, 'unregistered_app')) AS outcome_owner,
  CASE WHEN u.prio = 1 THEN COALESCE(u.raw_source_mode, u.tr_workflow_name) ELSE u.tr_workflow_name END AS source_mode,
  COALESCE(u.raw_skill_id, u.tr_skill_name) AS skill_id, u.skill_version,
  u.tr_session_id AS session_id, CAST(NULL AS INT64) AS turn_index,
  u.start_time, u.end_time, u.tr_receive_time AS receive_time, u.latency_ms,
  TIMESTAMP_DIFF(u.tr_receive_time, u.end_time, MILLISECOND) AS ingestion_lag_ms,
  u.input_tokens, u.cached_input_tokens, u.cache_write_5m_tokens, u.cache_write_1h_tokens, u.uncached_input_tokens,
  u.output_tokens, u.thinking_tokens, u.min_cache_tokens, u.meets_min_cache_floor, u.is_long_context_cliff,
  u.weighted_burndown_tokens, u.span_cost_usd, u.sandbox_cpu_sec, u.sandbox_mem_gb_sec,
  u.caller_retry_count, u.gateway_failover_attempts, u.pool_id, u.runtime_pool_id, u.tier_source,
  u.thought_signature_present, u.tool_definitions_sha256
FROM deduped_spans AS u
LEFT JOIN outs ON COALESCE(u.raw_outcome_id, u.tr_outcome_id) = outs.outcome_id
LEFT JOIN `agent_eval.workload_thresholds` AS wt ON u.apphub_application_id = wt.apphub_application_id
LEFT JOIN active_alloc_cc AS ca ON u.apphub_application_id = ca.app_id
WHERE u.span_rn = 1
);

CREATE OR REPLACE VIEW `agent_eval.v_trace_waterfall` AS
SELECT
  trace_id, span_id, parent_span_id, span_name, apphub_application_id,
  business_unit, cost_center, service_name, agent_name, otel_span_kind,
  span_kind, model_name, provider, lifecycle_track, tool_name,
  tool_call_status, status_code, rule0_pass, finops_traffic_type,
  serving_traffic_type, request_type_header, http_status_code,
  outcome_id, outcome_owner, source_mode, skill_id, skill_version,
  session_id, turn_index, start_time, end_time, receive_time,
  latency_ms, ingestion_lag_ms, input_tokens, cached_input_tokens,
  cache_write_5m_tokens, cache_write_1h_tokens, uncached_input_tokens,
  output_tokens, thinking_tokens, min_cache_tokens, meets_min_cache_floor,
  is_long_context_cliff, weighted_burndown_tokens, span_cost_usd,
  sandbox_cpu_sec, sandbox_mem_gb_sec, caller_retry_count,
  gateway_failover_attempts, pool_id, runtime_pool_id, tier_source,
  thought_signature_present, tool_definitions_sha256
FROM `agent_eval.gold_trace_waterfall`(
  TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
  CURRENT_TIMESTAMP()
);

CREATE OR REPLACE VIEW `agent_eval.v_bq_query_cost_attribution` AS
WITH reconciled_jobs AS (
  SELECT
    apphub_application_id,
    job_id,
    total_bytes_billed,
    total_slot_ms,
    COALESCE(
      NULLIF(query_cost_usd, 0.0),
      CASE
        WHEN pricing_mode = 'capacity_slot' THEN (total_slot_ms / 3600000.0) * 0.04
        ELSE (total_bytes_billed / 1099511627776.0) * 6.25
      END
    ) AS job_cost_usd,
    FALSE AS is_unlinked_tool_fallback
  FROM `agent_eval.bq_query_jobs`
),
unlinked_tool_queries AS (
  SELECT
    COALESCE(tc.trace_owner_app_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(tc.job_id, CONCAT('unlinked:', tc.trace_id, ':', tc.span_id)) AS job_id,
    COALESCE(tc.bytes_billed, tc.bytes_scanned, 0) AS total_bytes_billed,
    COALESCE(tc.slot_ms, 0) AS total_slot_ms,
    CASE
      WHEN COALESCE(tc.bytes_billed, tc.bytes_scanned, 0) > 0
        THEN (COALESCE(tc.bytes_billed, tc.bytes_scanned, 0) / 1099511627776.0) * 6.25
      ELSE (COALESCE(tc.slot_ms, 0) / 3600000.0) * 0.04
    END AS job_cost_usd,
    TRUE AS is_unlinked_tool_fallback
  FROM `${PROJECT_ID}.${SILVER_OPEN_DATASET}.tool_calls_open`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${GOLD_LOOKBACK_HOURS} HOUR),
    CURRENT_TIMESTAMP()
  ) AS tc
  LEFT JOIN `agent_eval.bq_query_jobs` AS j
    ON tc.job_id IS NOT NULL AND tc.job_id = j.job_id
  WHERE j.job_id IS NULL
    AND (COALESCE(tc.bytes_billed, tc.bytes_scanned, 0) > 0 OR COALESCE(tc.slot_ms, 0) > 0)
),
combined AS (
  SELECT * FROM reconciled_jobs
  UNION ALL
  SELECT * FROM unlinked_tool_queries
)
SELECT
  apphub_application_id,
  COUNT(*) AS query_job_count,
  COUNTIF(is_unlinked_tool_fallback) AS unlinked_tool_query_count,
  SUM(total_bytes_billed) AS total_bytes_billed,
  SUM(total_slot_ms) AS total_slot_ms,
  SUM(job_cost_usd) AS bq_query_cost_usd
FROM combined
GROUP BY apphub_application_id;

CREATE OR REPLACE VIEW `agent_eval.v_infra_cost_estimate` AS
WITH normalized_waterfall AS (
  SELECT
    w.outcome_id,
    w.trace_id,
    w.span_id,
    w.parent_span_id,
    COALESCE(w.service_name, 'agent-service') AS service_name,
    COALESCE(w.outcome_owner, w.apphub_application_id) AS apphub_application_id,
    w.start_time,
    w.latency_ms,
    COALESCE(w.sandbox_cpu_sec, 0.0) AS sandbox_cpu_sec,
    COALESCE(w.sandbox_mem_gb_sec, 0.0) AS sandbox_mem_gb_sec,
    CASE UPPER(TRIM(COALESCE(w.otel_span_kind, 'INTERNAL')))
      WHEN '1' THEN 'INTERNAL'
      WHEN 'INTERNAL' THEN 'INTERNAL'
      WHEN 'SPAN_KIND_INTERNAL' THEN 'INTERNAL'
      WHEN '2' THEN 'SERVER'
      WHEN 'SERVER' THEN 'SERVER'
      WHEN 'SPAN_KIND_SERVER' THEN 'SERVER'
      ELSE 'OTHER'
    END AS norm_otel_kind,
    wt.storage_usd_per_outcome,
    COALESCE(wt.is_external_counterparty, FALSE) AS is_external_counterparty
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.workload_thresholds` AS wt
    ON COALESCE(w.outcome_owner, w.apphub_application_id) = wt.apphub_application_id
  WHERE w.outcome_id IS NOT NULL
),
ranked_entry_spans AS (
  SELECT
    nw.*,
    ROW_NUMBER() OVER (
      PARTITION BY nw.trace_id, nw.service_name
      ORDER BY
        CASE WHEN nw.norm_otel_kind = 'SERVER' THEN 0 WHEN nw.norm_otel_kind = 'INTERNAL' THEN 1 ELSE 2 END,
        CASE WHEN nw.parent_span_id IS NULL THEN 0 ELSE 1 END,
        nw.start_time ASC, nw.span_id ASC
    ) AS svc_entry_rn
  FROM normalized_waterfall AS nw
  WHERE NOT nw.is_external_counterparty
),
outcome_infra AS (
  SELECT
    w.outcome_id,
    ANY_VALUE(CASE WHEN w.norm_otel_kind IN ('SERVER', 'INTERNAL') AND w.svc_entry_rn = 1 THEN w.apphub_application_id END) AS apphub_application_id,
    SUM(CASE WHEN w.norm_otel_kind IN ('SERVER', 'INTERNAL') AND w.svc_entry_rn = 1 THEN w.latency_ms / 1000.0 ELSE 0.0 END) AS total_server_seconds,
    SUM(CASE WHEN w.norm_otel_kind IN ('SERVER', 'INTERNAL') AND w.svc_entry_rn = 1 THEN (w.latency_ms / 1000.0) * ((2.0 * 0.00002400) + (2.0 * 0.00000250)) ELSE 0.0 END)
      + SUM((GREATEST(0.0, w.sandbox_cpu_sec) * 0.00002400) + (GREATEST(0.0, w.sandbox_mem_gb_sec) * 0.00000250)) AS estimated_compute_usd,
    MAX(CASE WHEN w.norm_otel_kind IN ('SERVER', 'INTERNAL') AND w.svc_entry_rn = 1 AND w.storage_usd_per_outcome >= 0.0 THEN w.storage_usd_per_outcome END) AS explicit_storage_usd,
    COALESCE(MAX(CASE WHEN w.norm_otel_kind IN ('SERVER', 'INTERNAL') AND w.svc_entry_rn = 1 AND w.storage_usd_per_outcome >= 0.0 THEN w.storage_usd_per_outcome END), 0.000020) AS estimated_storage_usd
  FROM ranked_entry_spans AS w
  GROUP BY w.outcome_id
  HAVING COUNTIF(w.norm_otel_kind IN ('SERVER', 'INTERNAL') AND w.svc_entry_rn = 1) > 0
)
SELECT
  outcome_id,
  apphub_application_id,
  total_server_seconds,
  estimated_compute_usd,
  explicit_storage_usd,
  estimated_storage_usd,
  estimated_compute_usd + estimated_storage_usd AS estimated_infra_cost_usd
FROM outcome_infra;

CREATE OR REPLACE VIEW `agent_eval.v_attribution_coverage` AS
SELECT
  w.apphub_application_id,
  COUNT(DISTINCT w.trace_id) AS total_traces,
  COUNT(DISTINCT IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.trace_id, NULL)) AS attributed_traces,
  COUNT(DISTINCT IF(w.outcome_id IS NULL OR w.outcome_id = '', w.trace_id, NULL)) AS unattributed_traces,
  COUNTIF(w.span_cost_usd IS NULL) AS unpriced_spans,
  SUM(IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', COALESCE(w.span_cost_usd, 0.0), 0.0)) AS attributed_spend_usd,
  SUM(IF(w.outcome_id IS NULL OR w.outcome_id = '', COALESCE(w.span_cost_usd, 0.0), 0.0)) AS unattributed_spend_usd,
  COUNTIF(
    w.finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge', 'optimize')
  ) AS unclassified_traffic_spans,
  SUM(
    IF(
      w.finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge', 'optimize'),
      COALESCE(w.span_cost_usd, 0.0),
      0.0
    )
  ) AS unclassified_traffic_spend_usd,
  IF(COUNTIF(w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(w.span_cost_usd)) AS total_spend_usd,
  CASE
    WHEN COUNTIF(w.span_cost_usd IS NULL) > 0 THEN
      100.0 * COALESCE(
        SAFE_DIVIDE(
          COUNT(DISTINCT IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.trace_id, NULL)),
          NULLIF(COUNT(DISTINCT w.trace_id), 0)
        ),
        0.0
      )
    ELSE
      100.0 * COALESCE(
        SAFE_DIVIDE(
          SUM(IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.span_cost_usd, 0.0)),
          NULLIF(SUM(w.span_cost_usd), 0.0)
        ),
        SAFE_DIVIDE(
          COUNT(DISTINCT IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.trace_id, NULL)),
          NULLIF(COUNT(DISTINCT w.trace_id), 0)
        ),
        0.0
      )
  END AS attribution_coverage_pct,
  (
    w.apphub_application_id != 'unregistered_app'
    AND COUNTIF(w.span_cost_usd IS NULL) = 0
    AND 100.0 * COALESCE(
      SAFE_DIVIDE(
        SUM(IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.span_cost_usd, 0.0)),
        NULLIF(SUM(w.span_cost_usd), 0.0)
      ),
      SAFE_DIVIDE(
        COUNT(DISTINCT IF(w.outcome_id IS NOT NULL AND w.outcome_id != '', w.trace_id, NULL)),
        NULLIF(COUNT(DISTINCT w.trace_id), 0)
      ),
      0.0
    ) >= 99.0
  ) AS passes_coverage_gate
FROM `agent_eval.v_trace_waterfall` AS w
GROUP BY w.apphub_application_id;

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
  IF(COUNTIF(w.span_cost_usd IS NULL) > 0, CAST(NULL AS FLOAT64), SUM(w.span_cost_usd)) AS external_model_cost_usd
FROM `agent_eval.v_trace_waterfall` AS w
LEFT JOIN `agent_eval.workload_thresholds` AS wt
  ON w.apphub_application_id = wt.apphub_application_id
WHERE COALESCE(wt.is_external_counterparty, FALSE)
GROUP BY
  w.apphub_application_id,
  w.service_name,
  w.agent_name,
  source_mode;
