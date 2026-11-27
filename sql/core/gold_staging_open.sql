-- Gold Split-SA Open Staging (`ADR-048`, `ADR-049`): Zero-`JSON` Scheduled Open KPI Tables.
-- Strict open-only boundary: reads ONLY `silver_open`, `silver_ref`, and `agent_eval` (zero restricted datasets).
-- All staged tables use scalar columns only (zero `JSON`) to unlock BigQuery Query Cache, BI Engine, and `JOB_CREATION_OPTIONAL`.

CREATE TABLE IF NOT EXISTS `agent_eval.gold_staged_open_waterfall` (
  trace_id STRING NOT NULL,
  span_id STRING NOT NULL,
  parent_span_id STRING,
  span_name STRING,
  apphub_application_id STRING NOT NULL,
  business_unit STRING,
  cost_center STRING,
  service_name STRING,
  agent_name STRING,
  otel_span_kind STRING,
  span_kind STRING,
  model_name STRING,
  provider STRING,
  lifecycle_track STRING,
  tool_name STRING,
  tool_call_status STRING,
  status_code STRING,
  rule0_pass BOOL,
  finops_traffic_type STRING,
  serving_traffic_type STRING,
  request_type_header STRING,
  http_status_code INT64,
  outcome_id STRING,
  outcome_owner STRING,
  source_mode STRING,
  skill_id STRING,
  skill_version STRING,
  session_id STRING,
  start_time TIMESTAMP NOT NULL,
  end_time TIMESTAMP,
  receive_time TIMESTAMP,
  latency_ms FLOAT64,
  ingestion_lag_ms INT64,
  input_tokens INT64,
  cached_input_tokens INT64,
  cache_write_5m_tokens INT64,
  cache_write_1h_tokens INT64,
  uncached_input_tokens INT64,
  output_tokens INT64,
  thinking_tokens INT64,
  min_cache_tokens INT64,
  meets_min_cache_floor BOOL,
  is_long_context_cliff BOOL,
  weighted_burndown_tokens FLOAT64,
  span_cost_usd FLOAT64,
  sandbox_cpu_sec FLOAT64,
  sandbox_mem_gb_sec FLOAT64,
  caller_retry_count INT64,
  gateway_failover_attempts INT64,
  pool_id STRING,
  runtime_pool_id STRING,
  tier_source STRING,
  thought_signature_present BOOL,
  tool_definitions_sha256 STRING,
  staged_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(start_time)
CLUSTER BY apphub_application_id, outcome_id, trace_id, span_kind
OPTIONS (
  description = 'Zero-JSON scheduled Open-SA trace waterfall staging for BI Engine and Query Cache.'
);

CREATE TABLE IF NOT EXISTS `agent_eval.gold_staged_open_kpi` (
  usage_hour TIMESTAMP NOT NULL,
  timestamp STRING NOT NULL,
  apphub_application_id STRING NOT NULL,
  tenant STRING NOT NULL,
  model_name STRING NOT NULL,
  model STRING NOT NULL,
  finops_traffic_type STRING,
  serving_traffic_type STRING,
  distinct_traces INT64,
  span_count INT64,
  ok_span_count INT64,
  error_span_count INT64,
  http_429_count INT64,
  spillover_span_count INT64,
  input_tokens INT64,
  cached_input_tokens INT64,
  cached_tokens INT64,
  cache_write_5m_tokens INT64,
  cache_write_1h_tokens INT64,
  uncached_input_tokens INT64,
  output_tokens INT64,
  thinking_tokens INT64,
  thoughts_tokens INT64,
  long_context_cliff_spans INT64,
  weighted_burndown_tokens FLOAT64,
  precomputed_weighted_burndown_tokens FLOAT64,
  total_cost_usd FLOAT64,
  staged_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(usage_hour)
CLUSTER BY apphub_application_id, model_name, finops_traffic_type, serving_traffic_type
OPTIONS (
  description = 'Zero-JSON scheduled Open-SA hourly burndown KPI staging (Query Cache & BI Engine eligible).'
);

CREATE OR REPLACE PROCEDURE `agent_eval.refresh_gold_open_staging`(
  IN p_from_time TIMESTAMP,
  IN p_to_time TIMESTAMP
)
BEGIN
  DECLARE v_now TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_hour_start TIMESTAMP DEFAULT TIMESTAMP_TRUNC(p_from_time, HOUR);
  DECLARE v_window_end TIMESTAMP DEFAULT TIMESTAMP_ADD(p_to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE);
  DECLARE v_hour_end TIMESTAMP DEFAULT TIMESTAMP_ADD(TIMESTAMP_TRUNC(v_window_end, HOUR), INTERVAL 1 HOUR);

  -- Evaluate parameterized open waterfall once into a session TEMP TABLE before populating both staged tables.
  CREATE TEMP TABLE _tmp_open_waterfall AS
  SELECT
    trace_id, span_id, parent_span_id, span_name, apphub_application_id, business_unit, cost_center,
    service_name, agent_name, otel_span_kind, span_kind, model_name, provider, lifecycle_track,
    tool_name, tool_call_status, status_code, rule0_pass, finops_traffic_type, serving_traffic_type,
    request_type_header, http_status_code, outcome_id, outcome_owner, source_mode, skill_id, skill_version,
    session_id, start_time, end_time, receive_time, latency_ms, ingestion_lag_ms,
    input_tokens, cached_input_tokens, cache_write_5m_tokens, cache_write_1h_tokens, uncached_input_tokens,
    output_tokens, thinking_tokens, min_cache_tokens, meets_min_cache_floor, is_long_context_cliff,
    weighted_burndown_tokens, span_cost_usd, sandbox_cpu_sec, sandbox_mem_gb_sec,
    caller_retry_count, gateway_failover_attempts, pool_id, runtime_pool_id, tier_source,
    thought_signature_present, tool_definitions_sha256
  FROM `agent_eval.gold_trace_waterfall`(p_from_time, p_to_time);

  -- Scope deletion to traces emitted by this window across the read margin so straddling child spans never duplicate or drop.
  DELETE FROM `agent_eval.gold_staged_open_waterfall`
  WHERE start_time >= p_from_time
    AND start_time < v_window_end
    AND trace_id IN (SELECT DISTINCT trace_id FROM _tmp_open_waterfall);

  INSERT INTO `agent_eval.gold_staged_open_waterfall`
  SELECT *, v_now AS staged_at
  FROM _tmp_open_waterfall;

  DELETE FROM `agent_eval.gold_staged_open_kpi`
  WHERE usage_hour >= v_hour_start AND usage_hour < v_hour_end;

  INSERT INTO `agent_eval.gold_staged_open_kpi`
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
    SUM(COALESCE(w.span_cost_usd, 0.0)) AS total_cost_usd,
    v_now AS staged_at
  FROM `agent_eval.gold_staged_open_waterfall` AS w
  WHERE w.start_time >= v_hour_start AND w.start_time < v_hour_end
    AND w.model_name IS NOT NULL
    AND w.model_name != 'none'
  GROUP BY
    usage_hour,
    timestamp,
    w.apphub_application_id,
    w.model_name,
    w.finops_traffic_type,
    w.serving_traffic_type;
END;

CREATE OR REPLACE VIEW `agent_eval.v_staged_capacity_hourly_burndown_export` AS
SELECT
  usage_hour, timestamp, apphub_application_id, tenant, model_name, model,
  finops_traffic_type, serving_traffic_type, distinct_traces, span_count,
  ok_span_count, error_span_count, http_429_count, spillover_span_count,
  input_tokens, cached_input_tokens, cached_tokens, cache_write_5m_tokens,
  cache_write_1h_tokens, uncached_input_tokens, output_tokens, thinking_tokens,
  thoughts_tokens, long_context_cliff_spans, weighted_burndown_tokens,
  precomputed_weighted_burndown_tokens, total_cost_usd
FROM `agent_eval.gold_staged_open_kpi`;
