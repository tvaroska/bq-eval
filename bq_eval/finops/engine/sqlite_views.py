"""SQLite copies of the capacity burndown, session lineage and 5-minute breach views."""

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

CREATE VIEW v_interaction_session_lineage AS
WITH session_spans AS (
    SELECT
        apphub_application_id,
        session_id,
        COALESCE(
            turn_index,
            ROW_NUMBER() OVER (PARTITION BY apphub_application_id, session_id ORDER BY start_epoch_ms, span_id) - 1
        ) AS resolved_turn_index,
        trace_id,
        start_epoch_ms,
        end_epoch_ms,
        input_tokens,
        output_tokens,
        thinking_tokens,
        span_cost_usd,
        CASE WHEN rule0_pass = 1 AND status_code != 'ERROR' THEN 1 ELSE 0 END AS turn_ok
    FROM otel_spans
    WHERE session_id IS NOT NULL AND session_id != ''
)
SELECT
    apphub_application_id,
    session_id,
    COUNT(DISTINCT resolved_turn_index) AS turn_count,
    MAX(resolved_turn_index) AS max_turn_index,
    COUNT(DISTINCT trace_id) AS trace_count,
    MAX(end_epoch_ms) - MIN(start_epoch_ms) AS session_duration_ms,
    SUM(input_tokens) AS cumulative_input_tokens,
    SUM(output_tokens + thinking_tokens) AS cumulative_output_tokens,
    SUM(span_cost_usd) AS cumulative_cost_usd,
    MIN(turn_ok) AS session_rule0_pass,
    MIN(CASE WHEN turn_ok = 0 THEN resolved_turn_index ELSE NULL END) AS first_failure_turn_index
FROM session_spans
GROUP BY apphub_application_id, session_id
ORDER BY apphub_application_id, session_id;

CREATE VIEW v_live_slo_and_429_breaches AS
WITH bucketed_spans AS (
    SELECT
        CAST(CAST(start_epoch_ms / 300000.0 AS INTEGER) * 300000 AS INTEGER) AS window_5m_epoch_ms,
        apphub_application_id,
        latency_ms,
        http_status_code,
        rule0_pass,
        status_code
    FROM otel_spans
),
ranked AS (
    SELECT
        window_5m_epoch_ms,
        apphub_application_id,
        latency_ms,
        ROW_NUMBER() OVER (PARTITION BY window_5m_epoch_ms, apphub_application_id ORDER BY latency_ms) AS rn,
        COUNT(*) OVER (PARTITION BY window_5m_epoch_ms, apphub_application_id) AS cnt
    FROM bucketed_spans
),
p95_win AS (
    SELECT window_5m_epoch_ms, apphub_application_id, latency_ms AS p95_latency_ms
    FROM ranked
    WHERE rn = MIN(cnt, MAX(1, CAST(CEIL(0.95 * cnt) AS INTEGER)))
),
agg AS (
    SELECT
        window_5m_epoch_ms,
        apphub_application_id,
        COUNT(*) AS span_count,
        SUM(CASE WHEN http_status_code = 429 THEN 1 ELSE 0 END) AS http_429_count,
        SUM(CASE WHEN rule0_pass = 0 OR status_code = 'ERROR' THEN 1 ELSE 0 END) AS rule0_fail_count
    FROM bucketed_spans
    GROUP BY window_5m_epoch_ms, apphub_application_id
)
SELECT
    a.window_5m_epoch_ms,
    a.apphub_application_id,
    a.span_count,
    a.http_429_count,
    100.0 * CAST(a.http_429_count AS REAL) / a.span_count AS http_429_rate_pct,
    100.0 * CAST(a.rule0_fail_count AS REAL) / a.span_count AS rule0_fail_rate_pct,
    p.p95_latency_ms,
    cfg.sla_latency_ms,
    CASE
        WHEN p.p95_latency_ms > cfg.sla_latency_ms
            OR (100.0 * CAST(a.http_429_count AS REAL) / a.span_count) >= 1.0
            OR (100.0 * CAST(a.rule0_fail_count AS REAL) / a.span_count) >= 5.0
        THEN 1 ELSE 0
    END AS is_breach,
    CASE
        WHEN (100.0 * CAST(a.http_429_count AS REAL) / a.span_count) >= 1.0 THEN 'HTTP_429_STORM'
        WHEN p.p95_latency_ms > cfg.sla_latency_ms THEN 'SLA_LATENCY'
        WHEN (100.0 * CAST(a.rule0_fail_count AS REAL) / a.span_count) >= 5.0 THEN 'RULE0_SPIKE'
        ELSE 'OK'
    END AS breach_reason
FROM agg AS a
INNER JOIN p95_win AS p
    ON a.window_5m_epoch_ms = p.window_5m_epoch_ms
    AND a.apphub_application_id = p.apphub_application_id
CROSS JOIN eval_config AS cfg
ORDER BY a.window_5m_epoch_ms, a.apphub_application_id;
"""
