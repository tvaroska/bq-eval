-- Silver Published `model_calls` Table Function (`silver_open`, `ADR-025`, `ADR-039`, `ADR-042`, `ADR-045`, `ADR-047`, `ADR-048`).
-- Reads internal `spans` and `silver_ref.app_registry` only (zero text, zero restricted fields, 2-hop routine auth).

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_OPEN_DATASET}.model_calls`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH base_spans AS (
    SELECT *
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.spans`(from_time, to_time)
  ),
  root_spans AS (
    SELECT
      trace_id,
      span_id AS root_span_id,
      start_time AS root_start_time,
      app_id AS root_app_id,
      runtime_lane AS root_runtime_lane,
      state_mode AS root_state_mode,
      traffic_type AS root_traffic_type,
      source_class AS root_source_class
    FROM (
      SELECT
        s.*,
        ROW_NUMBER() OVER (
          PARTITION BY s.trace_id
          ORDER BY s.start_time ASC, s.span_id ASC
        ) AS rn
      FROM base_spans AS s
      WHERE s.source_class IS NOT NULL
    )
    WHERE rn = 1
  ),
  gateway_owner AS (
    SELECT
      g.trace_id,
      g.has_gw_caller,
      r.app_id AS resolved_caller_app_id,
      g.gw_runtime_lane,
      g.gw_state_mode,
      g.gw_traffic_type,
      ROW_NUMBER() OVER (PARTITION BY g.trace_id ORDER BY r.effective_from DESC) AS reg_rn
    FROM (
      SELECT
        s.trace_id,
        MAX(CASE WHEN s.caller_app_id IS NOT NULL THEN 1 ELSE 0 END) = 1 AS has_gw_caller,
        ARRAY_AGG(s.caller_app_id IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_caller_app_id,
        ARRAY_AGG(CASE WHEN s.caller_app_id IS NOT NULL THEN s.start_time END IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_caller_time,
        ARRAY_AGG(s.runtime_lane IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_runtime_lane,
        ARRAY_AGG(s.state_mode IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_state_mode,
        ARRAY_AGG(s.traffic_type IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_traffic_type
      FROM base_spans AS s
      WHERE s.source_class = 'platform'
      GROUP BY s.trace_id
    ) AS g
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
      ON g.gw_caller_app_id IS NOT NULL
      AND r.app_id = g.gw_caller_app_id
      AND r.effective_from <= g.gw_caller_time
      AND (r.effective_to IS NULL OR g.gw_caller_time < r.effective_to)
  ),
  trace_outcomes AS (
    SELECT
      trace_id,
      outcome_id AS trace_outcome_id
    FROM (
      SELECT
        trace_id,
        outcome_id,
        ROW_NUMBER() OVER (
          PARTITION BY trace_id
          ORDER BY start_time ASC, span_id ASC
        ) AS rn
      FROM base_spans
      WHERE outcome_id IS NOT NULL
    )
    WHERE rn = 1
  ),
  enriched_calls AS (
    SELECT
      s.trace_id,
      s.span_id,
      CASE
        WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm'
          THEN p1.parent_span_id
        ELSE s.parent_span_id
      END AS parent_span_id,
      s.client_request_id,
      s.response_id,
      COALESCE(s.outcome_id, to_out.trace_outcome_id) AS outcome_id,
      CASE
        WHEN COALESCE(gw.has_gw_caller, FALSE) THEN gw.resolved_caller_app_id
        ELSE rt.root_app_id
      END AS trace_owner_app_id,
      s.operation_name,
      s.start_time,
      s.end_time,
      COALESCE(
        s.agent_id,
        CASE WHEN p1.span_kind = 'agent' THEN p1.agent_id END,
        CASE WHEN p2.span_kind = 'agent' THEN p2.agent_id END,
        CASE WHEN p3.span_kind = 'agent' THEN p3.agent_id END,
        CASE WHEN p4.span_kind = 'agent' THEN p4.agent_id END,
        CASE WHEN p5.span_kind = 'agent' THEN p5.agent_id END,
        CASE WHEN p6.span_kind = 'agent' THEN p6.agent_id END,
        p1.agent_id, p2.agent_id, p3.agent_id, p4.agent_id, p5.agent_id, p6.agent_id
      ) AS agent_id,
      COALESCE(
        s.agent_name,
        CASE WHEN p1.span_kind = 'agent' THEN p1.agent_name END,
        CASE WHEN p2.span_kind = 'agent' THEN p2.agent_name END,
        CASE WHEN p3.span_kind = 'agent' THEN p3.agent_name END,
        CASE WHEN p4.span_kind = 'agent' THEN p4.agent_name END,
        CASE WHEN p5.span_kind = 'agent' THEN p5.agent_name END,
        CASE WHEN p6.span_kind = 'agent' THEN p6.agent_name END,
        p1.agent_name, p2.agent_name, p3.agent_name, p4.agent_name, p5.agent_name, p6.agent_name
      ) AS agent_name,
      COALESCE(
        s.skill_name,
        CASE WHEN p1.span_kind = 'agent' THEN p1.skill_name END,
        CASE WHEN p2.span_kind = 'agent' THEN p2.skill_name END,
        CASE WHEN p3.span_kind = 'agent' THEN p3.skill_name END,
        p1.skill_name, p2.skill_name, p3.skill_name
      ) AS skill_name,
      COALESCE(
        s.skill_version,
        CASE WHEN p1.span_kind = 'agent' THEN p1.skill_version END,
        CASE WHEN p2.span_kind = 'agent' THEN p2.skill_version END,
        CASE WHEN p3.span_kind = 'agent' THEN p3.skill_version END,
        p1.skill_version, p2.skill_version, p3.skill_version
      ) AS skill_version,
      COALESCE(
        s.skill_sha256,
        CASE WHEN p1.span_kind = 'agent' THEN p1.skill_sha256 END,
        CASE WHEN p2.span_kind = 'agent' THEN p2.skill_sha256 END,
        CASE WHEN p3.span_kind = 'agent' THEN p3.skill_sha256 END,
        p1.skill_sha256, p2.skill_sha256, p3.skill_sha256
      ) AS skill_sha256,
      s.request_model,
      s.response_model,
      s.provider,
      s.upstream_endpoint_location,
      COALESCE(gw.gw_runtime_lane, rt.root_runtime_lane, s.runtime_lane) AS runtime_lane,
      COALESCE(gw.gw_state_mode, rt.root_state_mode, s.state_mode) AS state_mode,
      s.serving_tier,
      COALESCE(gw.gw_traffic_type, rt.root_traffic_type, s.traffic_type) AS traffic_type,
      CASE
        WHEN gw.gw_traffic_type IS NOT NULL THEN TRUE
        WHEN rt.root_traffic_type IS NOT NULL THEN (rt.root_source_class = 'platform')
        WHEN s.traffic_type IS NOT NULL THEN (s.source_class = 'platform')
        ELSE CAST(NULL AS BOOL)
      END AS traffic_type_attested,
      s.thought_signature_present,
      s.tool_definitions_sha256,
      s.available_tool_count,
      s.selected_tool_names,
      s.input_tokens,
      s.output_tokens,
      s.cache_read_tokens,
      s.cache_write_tokens,
      s.reasoning_tokens,
      s.duration_ms AS latency_ms,
      s.time_to_first_chunk,
      s.finish_reason,
      s.http_status_code,
      s.error_type,
      s.content_missing,
      s.model_confidence,
      s.cascade_tier,
      s.escalation_reason,
      JSON_STRIP_NULLS(
        JSON_OBJECT(
          'apphub_service_id', JSON_VALUE(s.labels, '$.apphub_service_id'),
          'apphub_workload_id', JSON_VALUE(s.labels, '$.apphub_workload_id'),
          'service_name', JSON_VALUE(s.labels, '$.service_name'),
          'telemetry_environment', JSON_VALUE(s.labels, '$.telemetry_environment'),
          'cloud_provider', JSON_VALUE(s.labels, '$.cloud_provider'),
          'cloud_region', JSON_VALUE(s.labels, '$.cloud_region'),
          'agent_version', JSON_VALUE(s.labels, '$.agent_version'),
          'prompt_name', JSON_VALUE(s.labels, '$.prompt_name'),
          'prompt_version', JSON_VALUE(s.labels, '$.prompt_version'),
          'finish_reasons_all', JSON_QUERY(s.labels, '$.finish_reasons_all'),
          'adk_event_id', JSON_VALUE(s.labels, '$.adk_event_id')
        )
      ) AS open_labels
    FROM base_spans AS s
    LEFT JOIN root_spans AS rt ON rt.trace_id = s.trace_id
    LEFT JOIN gateway_owner AS gw ON gw.trace_id = s.trace_id AND gw.reg_rn = 1
    LEFT JOIN trace_outcomes AS to_out ON to_out.trace_id = s.trace_id
    LEFT JOIN base_spans AS p1 ON p1.trace_id = s.trace_id AND p1.span_id = s.parent_span_id
    LEFT JOIN base_spans AS p2 ON p2.trace_id = s.trace_id AND p2.span_id = p1.parent_span_id
    LEFT JOIN base_spans AS p3 ON p3.trace_id = s.trace_id AND p3.span_id = p2.parent_span_id
    LEFT JOIN base_spans AS p4 ON p4.trace_id = s.trace_id AND p4.span_id = p3.parent_span_id
    LEFT JOIN base_spans AS p5 ON p5.trace_id = s.trace_id AND p5.span_id = p4.parent_span_id
    LEFT JOIN base_spans AS p6 ON p6.trace_id = s.trace_id AND p6.span_id = p5.parent_span_id
    WHERE s.is_model_call IS TRUE
  ),
  sequenced AS (
    SELECT
      c.*,
      ROW_NUMBER() OVER (
        PARTITION BY c.trace_id, COALESCE(c.agent_name, ''), COALESCE(c.request_model, '')
        ORDER BY c.start_time ASC, c.span_id ASC
      ) AS retry_seq
    FROM enriched_calls AS c
  ),
  with_retries AS (
    SELECT
      s.*,
      s.retry_seq - 1 - COALESCE(
        MAX(CASE WHEN COALESCE(s.http_status_code, 0) != 429 THEN s.retry_seq ELSE 0 END) OVER (
          PARTITION BY s.trace_id, COALESCE(s.agent_name, ''), COALESCE(s.request_model, '')
          ORDER BY s.start_time ASC, s.span_id ASC
          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ),
        0
      ) AS caller_retry_count
    FROM sequenced AS s
  )
  SELECT
    trace_id,
    span_id,
    parent_span_id,
    client_request_id,
    response_id,
    outcome_id,
    trace_owner_app_id,
    operation_name,
    start_time,
    end_time,
    agent_id,
    agent_name,
    skill_name,
    skill_version,
    skill_sha256,
    request_model,
    response_model,
    provider,
    upstream_endpoint_location,
    runtime_lane,
    state_mode,
    serving_tier,
    traffic_type,
    traffic_type_attested,
    thought_signature_present,
    caller_retry_count,
    tool_definitions_sha256,
    available_tool_count,
    selected_tool_names,
    input_tokens,
    output_tokens,
    cache_read_tokens,
    cache_write_tokens,
    reasoning_tokens,
    latency_ms,
    time_to_first_chunk,
    finish_reason,
    http_status_code,
    error_type,
    content_missing,
    model_confidence,
    cascade_tier,
    escalation_reason,
    CASE
      WHEN TO_JSON_STRING(open_labels) = '{}' THEN CAST(NULL AS JSON)
      ELSE open_labels
    END AS labels
  FROM with_retries
);
