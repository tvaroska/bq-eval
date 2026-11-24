-- =============================================================================
-- BQ Eval & FinOps (bq-eval) — Full Capture OTel Compatibility Views
--   * Native OTel `_AllSpans` + `_AllLogs` + BigLake `completions` views
--   * Shared by Full Capture (`otel_spans` / `trace_payloads`) and Hybrid Mode C
--     (`sql/core/bqaa_adapter_views.sql`).
-- =============================================================================

-- 1. Native Telemetry Compatibility Views (agent_eval.v_otel_allspans_only -> obs_traces._AllSpans)
CREATE OR REPLACE VIEW `agent_eval.v_otel_allspans_only` AS
SELECT
  trace_id,
  span_id,
  parent_span_id,
  name AS span_name,
  start_time,
  end_time,
  receive_time,
  SAFE_DIVIDE(CAST(duration_nano AS FLOAT64), 1e6) AS latency_ms,
  CASE
    WHEN STARTS_WITH(name, 'a2a.server.events.event_queue') THEN 'OK'
    WHEN CAST(status.code AS STRING) IN ('1', 'OK', 'STATUS_CODE_OK') THEN 'OK'
    WHEN CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR') THEN 'ERROR'
    ELSE 'UNSET'
  END AS status_code,
  COALESCE(CAST(kind AS STRING), 'SPAN_KIND_INTERNAL') AS otel_span_kind,
  COALESCE(
    JSON_VALUE(attributes, '$."gen_ai.operation.name"'),
    'internal'
  ) AS span_kind,
  COALESCE(
    JSON_VALUE(resource.attributes, '$."service.name"'),
    JSON_VALUE(attributes, '$."service.name"'),
    'unknown_service'
  ) AS service_name,
  COALESCE(
    JSON_VALUE(resource.attributes, '$."apphub.application.id"'),
    JSON_VALUE(resource.attributes, '$."gcp.apphub.application.id"'),
    JSON_VALUE(attributes, '$."apphub.application.id"'),
    JSON_VALUE(attributes, '$."gcp.apphub.application.id"')
  ) AS gcp_apphub_application_id,
  COALESCE(
    JSON_VALUE(resource.attributes, '$."apphub.service.id"'),
    JSON_VALUE(resource.attributes, '$."gcp.apphub.service.id"'),
    JSON_VALUE(attributes, '$."apphub.service.id"'),
    JSON_VALUE(attributes, '$."gcp.apphub.service.id"')
  ) AS gcp_apphub_service_id,
  COALESCE(
    JSON_VALUE(resource.attributes, '$."apphub.workload.id"'),
    JSON_VALUE(resource.attributes, '$."gcp.apphub.workload.id"'),
    JSON_VALUE(attributes, '$."apphub.workload.id"'),
    JSON_VALUE(attributes, '$."gcp.apphub.workload.id"')
  ) AS gcp_apphub_workload_id,
  COALESCE(
    JSON_VALUE(attributes, '$."gen_ai.agent.name"'),
    JSON_VALUE(resource.attributes, '$."service.name"'),
    'unknown_agent'
  ) AS agent_name,
  COALESCE(
    JSON_VALUE(attributes, '$."gen_ai.response.model"'),
    JSON_VALUE(attributes, '$."gen_ai.request.model"'),
    'none'
  ) AS model_name,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.input_tokens"') AS INT64), 0) AS input_tokens,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.cache_read.input_tokens"') AS INT64), 0) AS cached_input_tokens,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.output_tokens"') AS INT64), 0) AS output_tokens,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.reasoning.output_tokens"') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.thoughts_tokens"') AS INT64),
    0
  ) AS thinking_tokens,
  JSON_VALUE(attributes, '$."gen_ai.tool.name"') AS tool_name,
  CASE
    WHEN JSON_VALUE(attributes, '$."gen_ai.operation.name"') = 'execute_tool' THEN
      COALESCE(
        JSON_VALUE(attributes, '$."tool.call.status"'),
        CASE
          WHEN JSON_VALUE(attributes, '$."error.type"') IN ('VALIDATION_ERROR', 'TIMEOUT', 'RATE_LIMITED')
            THEN JSON_VALUE(attributes, '$."error.type"')
          WHEN CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR')
            THEN 'VALIDATION_ERROR'
          ELSE 'SUCCESS'
        END
      )
    ELSE NULL
  END AS tool_call_status,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$."rule0.pass"') AS BOOL),
    IF(
      STARTS_WITH(name, 'a2a.server.events.event_queue'),
      TRUE,
      IF(CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR'), FALSE, TRUE)
    )
  ) AS rule0_pass,
  COALESCE(
    JSON_VALUE(attributes, '$."finops.traffic_type"'),
    'production_inference'
  ) AS finops_traffic_type,
  JSON_VALUE(attributes, '$."outcome.id"') AS outcome_id,
  COALESCE(
    JSON_VALUE(attributes, '$."outcome.owner"'),
    JSON_VALUE(resource.attributes, '$."apphub.application.id"'),
    JSON_VALUE(resource.attributes, '$."gcp.apphub.application.id"')
  ) AS outcome_owner,
  JSON_VALUE(attributes, '$."source_mode"') AS source_mode,
  JSON_VALUE(attributes, '$."skill_id"') AS skill_id,
  JSON_VALUE(attributes, '$."skill_version"') AS skill_version,
  JSON_VALUE(attributes, '$."entity_id"') AS entity_id,
  CASE UPPER(COALESCE(
    JSON_VALUE(attributes, '$."gen_ai.response.traffic_type"'),
    JSON_VALUE(attributes, '$."gen_ai.vertex.traffic_type"'),
    JSON_VALUE(attributes, '$."serving.traffic_type"'),
    'STANDARD'
  ))
    WHEN 'PROVISIONED_THROUGHPUT' THEN 'PROVISIONED_THROUGHPUT'
    WHEN 'DEDICATED' THEN 'PROVISIONED_THROUGHPUT'
    WHEN 'PRIORITY' THEN 'PRIORITY'
    WHEN 'ON_DEMAND_PRIORITY' THEN 'PRIORITY'
    WHEN 'FLEX' THEN 'FLEX'
    WHEN 'ON_DEMAND_FLEX' THEN 'FLEX'
    ELSE 'STANDARD'
  END AS serving_traffic_type,
  COALESCE(
    JSON_VALUE(attributes, '$."x-vertex-ai-llm-request-type"'),
    JSON_VALUE(attributes, '$."request_type_header"')
  ) AS request_type_header,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$."http.response.status_code"') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$."http.status_code"') AS INT64),
    CASE
      WHEN JSON_VALUE(attributes, '$."error.type"') = 'RATE_LIMITED' THEN 429
      WHEN CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR') THEN 500
      ELSE 200
    END
  ) AS http_status_code,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.cache_creation.ephemeral_5m_input_tokens"') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$."cache_write_5m_tokens"') AS INT64),
    0
  ) AS cache_write_5m_tokens,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.cache_creation.ephemeral_1h_input_tokens"') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$."cache_write_1h_tokens"') AS INT64),
    0
  ) AS cache_write_1h_tokens,
  COALESCE(
    JSON_VALUE(attributes, '$."gen_ai.conversation.id"'),
    JSON_VALUE(attributes, '$."interaction.session_id"'),
    JSON_VALUE(attributes, '$."session_id"')
  ) AS session_id,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.interaction.turn_index"') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$."turn_index"') AS INT64)
  ) AS turn_index,
  attributes
FROM `obs_traces._AllSpans`;

CREATE OR REPLACE VIEW `agent_eval.otel_spans` AS
SELECT * FROM `agent_eval.v_otel_allspans_only`;

-- 2. Sanitized Trace Text Payloads Views (1 row per trace_id)
CREATE OR REPLACE VIEW `agent_eval.v_otel_logs_payloads_only` AS
SELECT
  COALESCE(
    REGEXP_EXTRACT(l.trace, r'traces/([0-9a-fA-F]+)$'),
    l.trace,
    JSON_VALUE(l.json_payload, '$."trace_id"')
  ) AS trace_id,
  ANY_VALUE(
    COALESCE(
      JSON_VALUE(l.resource.labels, '$."apphub.application.id"'),
      JSON_VALUE(l.json_payload, '$."apphub.application.id"'),
      JSON_VALUE(l.json_payload, '$."gcp.apphub.application.id"'),
      'unregistered_app'
    )
  ) AS apphub_application_id,
  MAX(l.timestamp) AS ingested_at,
  COALESCE(
    STRING_AGG(
      COALESCE(
        c.user_prompt,
        JSON_VALUE(c.payload, '$."user_prompt"'),
        JSON_VALUE(l.json_payload, '$."gen_ai.prompt"'),
        JSON_VALUE(l.json_payload, '$."user_prompt"')
      ),
      '\n' ORDER BY l.timestamp
    ),
    ''
  ) AS user_prompt,
  COALESCE(
    STRING_AGG(
      COALESCE(
        c.retrieved_context,
        JSON_VALUE(c.payload, '$."retrieved_context"'),
        JSON_VALUE(l.json_payload, '$."gen_ai.retrieved_context"'),
        JSON_VALUE(l.json_payload, '$."retrieved_context"')
      ),
      '\n' ORDER BY l.timestamp
    ),
    ''
  ) AS retrieved_context,
  COALESCE(
    ARRAY_AGG(
      COALESCE(
        c.final_response,
        JSON_VALUE(c.payload, '$."final_response"'),
        JSON_VALUE(l.json_payload, '$."gen_ai.completion"'),
        JSON_VALUE(l.json_payload, '$."final_response"')
      ) IGNORE NULLS ORDER BY l.timestamp DESC LIMIT 1
    )[SAFE_OFFSET(0)],
    ''
  ) AS final_response
FROM `obs_logs._AllLogs` AS l
LEFT JOIN `agent_eval.completions` AS c
  ON c._FILE_NAME = COALESCE(
    JSON_VALUE(l.json_payload, '$."gen_ai.completion.uri"'),
    JSON_VALUE(l.json_payload, '$."gen_ai.input.messages_uri"'),
    JSON_VALUE(l.json_payload, '$."upload_uri"')
  )
WHERE COALESCE(l.trace, JSON_VALUE(l.json_payload, '$."trace_id"')) IS NOT NULL
GROUP BY 1;

CREATE OR REPLACE VIEW `agent_eval.trace_payloads` AS
SELECT * FROM `agent_eval.v_otel_logs_payloads_only`;
