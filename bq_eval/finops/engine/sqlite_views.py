"""SQLite copy of the hourly capacity burndown export view."""

FINOPS_SQLITE_VIEWS_DDL: str = """
CREATE VIEW v_capacity_hourly_burndown_export AS
SELECT
    usage_hour_iso AS usage_hour,
    usage_hour_iso AS timestamp,
    apphub_application_id,
    apphub_application_id AS tenant,
    model_name,
    model_name AS model,
    finops_traffic_type,
    serving_traffic_type,
    COUNT(DISTINCT trace_id) AS distinct_traces,
    COUNT(*) AS span_count,
    SUM(CASE WHEN status_code = 'OK' THEN 1 ELSE 0 END) AS ok_span_count,
    SUM(CASE WHEN status_code = 'ERROR' THEN 1 ELSE 0 END) AS error_span_count,
    SUM(CASE WHEN http_status_code = 429 THEN 1 ELSE 0 END) AS http_429_count,
    SUM(CASE WHEN UPPER(serving_traffic_type) IN ('PRIORITY', 'STANDARD', 'FLEX') THEN 1 ELSE 0 END) AS spillover_span_count,
    SUM(input_tokens) AS input_tokens,
    SUM(cached_input_tokens) AS cached_input_tokens,
    SUM(cached_input_tokens) AS cached_tokens,
    SUM(cache_write_5m_tokens) AS cache_write_5m_tokens,
    SUM(cache_write_1h_tokens) AS cache_write_1h_tokens,
    SUM(uncached_input_tokens) AS uncached_input_tokens,
    SUM(output_tokens) AS output_tokens,
    SUM(thinking_tokens) AS thinking_tokens,
    SUM(thinking_tokens) AS thoughts_tokens,
    SUM(is_long_context_cliff) AS long_context_cliff_spans,
    SUM(weighted_burndown_tokens) AS weighted_burndown_tokens,
    SUM(weighted_burndown_tokens) AS precomputed_weighted_burndown_tokens,
    SUM(span_cost_usd) AS total_cost_usd
FROM otel_spans
GROUP BY usage_hour_iso, apphub_application_id, model_name, finops_traffic_type, serving_traffic_type
ORDER BY usage_hour_iso, apphub_application_id, model_name, finops_traffic_type, serving_traffic_type;
"""
