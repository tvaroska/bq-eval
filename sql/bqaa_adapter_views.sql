-- =============================================================================
-- BQ Eval & FinOps (bq-eval) — Dual-Path Adapter Views for BigQuery Agent Analytics (BQAA)
--   * Light Capture (BQAA): Maps `agent_events` (BigQueryAgentAnalyticsPlugin /
--     BigQueryCallbackHandler / Conversational Analytics) onto `agent_eval.otel_spans`
--     and `agent_eval.trace_payloads` with zero data copies and 0 external tables.
--   * Preserves ADR-003 / EVAL-P07 strict `'unregistered_app'` fallback: workloads
--     without explicit `custom_metadata."apphub.application.id"` (or server-stamped
--     `_AllSpans` join) resolve directly to `'unregistered_app'`, never collapsing
--     into self-reported `adk.app_name` or `agent` strings.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Zero-Copy Span Waterfall Adapter (`agent_events` -> `v_bqaa_otel_spans`)
--   * Deduplicates BigQuery Storage Write API retry rows by `event_id`
--   * Adopts `attributes.otel.{trace_id, span_id}` when `log_otel_span_context=True`
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_bqaa_otel_spans` AS
WITH deduped_events AS (
  SELECT *
  FROM `agent_eval.agent_events`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp DESC) = 1
)
SELECT
  COALESCE(
    JSON_VALUE(attributes, '$.otel.trace_id'),
    trace_id,
    invocation_id
  ) AS trace_id,
  COALESCE(
    JSON_VALUE(attributes, '$.otel.span_id'),
    span_id,
    event_id
  ) AS span_id,
  parent_span_id,
  CASE
    WHEN event_type = 'OUTCOME_CLOSED'
      OR JSON_VALUE(attributes, '$.custom_metadata."outcome.closed"') = 'true'
      THEN 'outcome.closed'
    ELSE event_type
  END AS span_name,
  TIMESTAMP_SUB(
    timestamp,
    INTERVAL COALESCE(SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 0) MILLISECOND
  ) AS start_time,
  timestamp AS end_time,
  timestamp AS receive_time,
  COALESCE(SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64), 0.0) AS latency_ms,
  CASE
    WHEN UPPER(COALESCE(status, 'OK')) = 'ERROR' OR event_type LIKE '%_ERROR' THEN 'ERROR'
    ELSE 'OK'
  END AS status_code,
  'SPAN_KIND_INTERNAL' AS otel_span_kind,
  CASE
    WHEN event_type IN ('LLM_RESPONSE', 'LLM_ERROR') THEN 'chat'
    WHEN event_type IN ('TOOL_COMPLETED', 'TOOL_ERROR') THEN 'execute_tool'
    WHEN event_type IN ('AGENT_COMPLETED', 'AGENT_ERROR', 'OUTCOME_CLOSED')
      OR JSON_VALUE(attributes, '$.custom_metadata."outcome.closed"') = 'true' THEN 'invoke_agent'
    ELSE 'internal'
  END AS span_kind,
  COALESCE(
    JSON_VALUE(attributes, '$.adk.app_name'),
    JSON_VALUE(attributes, '$.root_agent_name'),
    agent,
    'unknown_service'
  ) AS service_name,
  -- ADR-003 / EVAL-P07 strict App Hub resolution (NULL -> 'unregistered_app' downstream)
  COALESCE(
    JSON_VALUE(attributes, '$.custom_metadata."gcp.apphub.application.id"'),
    JSON_VALUE(attributes, '$.custom_metadata."apphub.application.id"'),
    JSON_VALUE(attributes, '$.custom_metadata.apphub_application_id')
  ) AS gcp_apphub_application_id,
  JSON_VALUE(attributes, '$.custom_metadata."apphub.service.id"') AS gcp_apphub_service_id,
  JSON_VALUE(attributes, '$.custom_metadata."apphub.workload.id"') AS gcp_apphub_workload_id,
  COALESCE(agent, 'unknown_agent') AS agent_name,
  COALESCE(
    JSON_VALUE(attributes, '$.model_version'),
    JSON_VALUE(attributes, '$.model'),
    'none'
  ) AS model_name,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64), 0) AS input_tokens,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cached_content_token_count') AS INT64), 0) AS cached_input_tokens,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64), 0) AS output_tokens,
  COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64), 0) AS thinking_tokens,
  COALESCE(
    JSON_VALUE(attributes, '$.tool_name'),
    JSON_VALUE(content, '$.tool_name'),
    JSON_VALUE(content, '$.name')
  ) AS tool_name,
  CASE
    WHEN event_type = 'TOOL_ERROR' THEN 'VALIDATION_ERROR'
    WHEN event_type = 'TOOL_COMPLETED' THEN 'SUCCESS'
    ELSE NULL
  END AS tool_call_status,
  NOT (UPPER(COALESCE(status, 'OK')) = 'ERROR' OR event_type LIKE '%_ERROR') AS rule0_pass,
  COALESCE(
    JSON_VALUE(attributes, '$.custom_metadata."finops.traffic_type"'),
    'production_inference'
  ) AS finops_traffic_type,
  COALESCE(
    JSON_VALUE(attributes, '$.custom_metadata."outcome.id"'),
    trace_id,
    invocation_id
  ) AS outcome_id,
  -- Preserve strict ADR-003 fallback: never fall back outcome_owner to adk.app_name or agent
  COALESCE(
    JSON_VALUE(attributes, '$.custom_metadata."outcome.owner"'),
    JSON_VALUE(attributes, '$.custom_metadata."gcp.apphub.application.id"'),
    JSON_VALUE(attributes, '$.custom_metadata."apphub.application.id"'),
    JSON_VALUE(attributes, '$.custom_metadata.apphub_application_id')
  ) AS outcome_owner,
  JSON_VALUE(attributes, '$.custom_metadata.source_mode') AS source_mode,
  JSON_VALUE(attributes, '$.custom_metadata.skill_id') AS skill_id,
  JSON_VALUE(attributes, '$.custom_metadata.skill_version') AS skill_version,
  COALESCE(
    JSON_VALUE(attributes, '$.custom_metadata.entity_id'),
    session_id
  ) AS entity_id,
  JSON_SET(
    COALESCE(attributes, JSON '{}'),
    '$."business_unit"', COALESCE(JSON_VALUE(attributes, '$."business_unit"'), JSON_VALUE(attributes, '$.custom_metadata."business_unit"')),
    '$."cost_center"', COALESCE(JSON_VALUE(attributes, '$."cost_center"'), JSON_VALUE(attributes, '$.custom_metadata."cost_center"')),
    '$."outcome.status"', COALESCE(JSON_VALUE(attributes, '$."outcome.status"'), JSON_VALUE(attributes, '$.custom_metadata."outcome.status"')),
    '$."outcome.verified"', COALESCE(JSON_VALUE(attributes, '$."outcome.verified"'), JSON_VALUE(attributes, '$.custom_metadata."outcome.verified"')),
    '$."outcome.subject"', COALESCE(JSON_VALUE(attributes, '$."outcome.subject"'), JSON_VALUE(attributes, '$.custom_metadata."outcome.subject"')),
    '$."outcome.parent_id"', COALESCE(JSON_VALUE(attributes, '$."outcome.parent_id"'), JSON_VALUE(attributes, '$.custom_metadata."outcome.parent_id"')),
    '$."hitl.review"', COALESCE(JSON_VALUE(attributes, '$."hitl.review"'), JSON_VALUE(attributes, '$.custom_metadata."hitl.review"')),
    '$."apphub.external"', COALESCE(JSON_VALUE(attributes, '$."apphub.external"'), JSON_VALUE(attributes, '$.custom_metadata."apphub.external"'))
  ) AS attributes
FROM deduped_events
WHERE event_type IN (
  'LLM_RESPONSE', 'LLM_ERROR',
  'TOOL_COMPLETED', 'TOOL_ERROR',
  'AGENT_COMPLETED', 'AGENT_ERROR',
  'OUTCOME_CLOSED'
)
OR JSON_VALUE(attributes, '$.custom_metadata."outcome.closed"') = 'true';

-- -----------------------------------------------------------------------------
-- 2. Zero-Copy Trace Payloads Adapter (`agent_events` -> `v_bqaa_trace_payloads`)
--   * Reconstructs 1 row per `trace_id` directly from inline `agent_events.content`
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_bqaa_trace_payloads` AS
WITH deduped_events AS (
  SELECT *
  FROM `agent_eval.agent_events`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp DESC) = 1
)
SELECT
  COALESCE(
    JSON_VALUE(attributes, '$.otel.trace_id'),
    trace_id,
    invocation_id
  ) AS trace_id,
  ANY_VALUE(
    COALESCE(
      JSON_VALUE(attributes, '$.custom_metadata."gcp.apphub.application.id"'),
      JSON_VALUE(attributes, '$.custom_metadata."apphub.application.id"'),
      JSON_VALUE(attributes, '$.custom_metadata.apphub_application_id'),
      'unregistered_app'
    )
  ) AS apphub_application_id,
  MAX(timestamp) AS ingested_at,
  COALESCE(
    STRING_AGG(
      IF(
        event_type = 'USER_MESSAGE_RECEIVED',
        COALESCE(JSON_VALUE(content, '$.text'), TO_JSON_STRING(content)),
        NULL
      ),
      '\n' ORDER BY timestamp
    ),
    ''
  ) AS user_prompt,
  COALESCE(
    STRING_AGG(
      IF(
        event_type = 'TOOL_COMPLETED',
        COALESCE(JSON_VALUE(content, '$.response'), TO_JSON_STRING(content)),
        NULL
      ),
      '\n' ORDER BY timestamp
    ),
    ''
  ) AS retrieved_context,
  COALESCE(
    ARRAY_AGG(
      IF(
        event_type = 'AGENT_RESPONSE',
        COALESCE(JSON_VALUE(content, '$.text'), TO_JSON_STRING(content)),
        NULL
      )
      IGNORE NULLS ORDER BY timestamp DESC LIMIT 1
    )[SAFE_OFFSET(0)],
    ''
  ) AS final_response
FROM deduped_events
GROUP BY trace_id;

-- -----------------------------------------------------------------------------
-- 3. Step-1 Pre-Filtered Turn View for Engine B (`AI.EVALUATE_AGENT`)
--   * Self-contained over `v_bqaa_otel_spans` so fresh Light Capture projects
--     deploy `bqaa_adapter_views.sql` in one pass before `step1_trace_views.sql`.
--   * Filters BQAA's `{table}_eval_turn` (`AgentData` JSON `trace` column) through
--     Step-1 deterministic SQL gates (`step1_pass = TRUE` and `NOT has_tool_loop`)
--     before invoking `AI.EVALUATE_AGENT`.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_bqaa_eval_turn_filtered` AS
WITH bqaa_span_costs AS (
  SELECT
    s.trace_id,
    s.parent_span_id,
    s.outcome_id,
    s.outcome_owner,
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
    s.start_time,
    s.end_time,
    s.span_kind,
    s.status_code,
    s.tool_call_status,
    COALESCE(s.rule0_pass, s.status_code != 'ERROR') AS rule0_pass,
    CASE
      WHEN s.model_name IS NULL OR s.model_name = 'none' OR (s.input_tokens = 0 AND s.output_tokens = 0 AND s.thinking_tokens = 0) THEN 0.0
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
  FROM `agent_eval.v_bqaa_otel_spans` AS s
),
bqaa_step1_eval AS (
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
    ANY_VALUE(w.business_unit) AS business_unit,
    MIN(w.start_time) AS trace_start_time,
    TIMESTAMP_DIFF(MAX(w.end_time), MIN(w.start_time), MILLISECOND) AS trace_latency_ms,
    (
      LOGICAL_AND(w.rule0_pass)
      AND COUNTIF(w.status_code = 'ERROR') = 0
      AND COUNTIF(
        w.span_kind = 'execute_tool'
        AND COALESCE(w.tool_call_status, 'SUCCESS') != 'SUCCESS'
      ) = 0
      AND COUNTIF(w.span_kind = 'execute_tool') <= 8
    ) AS step1_pass,
    SUM(w.span_cost_usd) AS trace_cost_usd
  FROM bqaa_span_costs AS w
  GROUP BY w.trace_id
)
SELECT
  et.*,
  s1.apphub_application_id,
  s1.outcome_id,
  s1.business_unit,
  s1.trace_start_time,
  s1.trace_latency_ms,
  s1.step1_pass,
  s1.trace_cost_usd
FROM `agent_eval.agent_events_eval_turn` AS et
INNER JOIN bqaa_step1_eval AS s1
  ON et.trace_id = s1.trace_id
WHERE s1.step1_pass = TRUE
  AND NOT COALESCE(et.has_tool_loop, FALSE);

-- =============================================================================
-- 4. Ingestion Mode Switchboard (Uncomment Mode A or Mode C as Needed)
--    * Default in `sql/ddl.sql`: Full Capture (`obs_traces._AllSpans` + `_AllLogs`)
-- =============================================================================

-- Mode A: Light Capture Only (`BQAA` `agent_events`, 0 Observability Links Needed)
-- CREATE OR REPLACE VIEW `agent_eval.otel_spans` AS
-- SELECT * FROM `agent_eval.v_bqaa_otel_spans`;
--
-- CREATE OR REPLACE VIEW `agent_eval.trace_payloads` AS
-- SELECT * FROM `agent_eval.v_bqaa_trace_payloads`;

-- Mode C: Hybrid Dual-Path (`Full Capture _AllSpans` + `Light Capture BQAA` Fallback)
--   * Acyclic base views read `obs_traces._AllSpans` and `obs_logs._AllLogs` directly
--     so `agent_eval.otel_spans` and `agent_eval.trace_payloads` never self-reference.
--   * Server-stamped `_AllSpans` rows take precedence when an agent emits both;
--     `BQAA`-only developer/off-GCP traces fill in via `NOT EXISTS` on `trace_id`.
-- CREATE OR REPLACE VIEW `agent_eval.v_otel_allspans_only` AS
-- SELECT
--   trace_id,
--   span_id,
--   parent_span_id,
--   name AS span_name,
--   start_time,
--   end_time,
--   receive_time,
--   SAFE_DIVIDE(CAST(duration_nano AS FLOAT64), 1e6) AS latency_ms,
--   CASE
--     WHEN STARTS_WITH(name, 'a2a.server.events.event_queue') THEN 'OK'
--     WHEN CAST(status.code AS STRING) IN ('1', 'OK', 'STATUS_CODE_OK') THEN 'OK'
--     WHEN CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR') THEN 'ERROR'
--     ELSE 'UNSET'
--   END AS status_code,
--   COALESCE(CAST(kind AS STRING), 'SPAN_KIND_INTERNAL') AS otel_span_kind,
--   COALESCE(JSON_VALUE(attributes, '$."gen_ai.operation.name"'), 'internal') AS span_kind,
--   COALESCE(
--     JSON_VALUE(resource.attributes, '$."service.name"'),
--     JSON_VALUE(attributes, '$."service.name"'),
--     'unknown_service'
--   ) AS service_name,
--   COALESCE(
--     JSON_VALUE(resource.attributes, '$."apphub.application.id"'),
--     JSON_VALUE(resource.attributes, '$."gcp.apphub.application.id"'),
--     JSON_VALUE(attributes, '$."apphub.application.id"'),
--     JSON_VALUE(attributes, '$."gcp.apphub.application.id"')
--   ) AS gcp_apphub_application_id,
--   COALESCE(
--     JSON_VALUE(resource.attributes, '$."apphub.service.id"'),
--     JSON_VALUE(resource.attributes, '$."gcp.apphub.service.id"'),
--     JSON_VALUE(attributes, '$."apphub.service.id"'),
--     JSON_VALUE(attributes, '$."gcp.apphub.service.id"')
--   ) AS gcp_apphub_service_id,
--   COALESCE(
--     JSON_VALUE(resource.attributes, '$."apphub.workload.id"'),
--     JSON_VALUE(resource.attributes, '$."gcp.apphub.workload.id"'),
--     JSON_VALUE(attributes, '$."apphub.workload.id"'),
--     JSON_VALUE(attributes, '$."gcp.apphub.workload.id"')
--   ) AS gcp_apphub_workload_id,
--   COALESCE(
--     JSON_VALUE(attributes, '$."gen_ai.agent.name"'),
--     JSON_VALUE(resource.attributes, '$."service.name"'),
--     'unknown_agent'
--   ) AS agent_name,
--   COALESCE(
--     JSON_VALUE(attributes, '$."gen_ai.response.model"'),
--     JSON_VALUE(attributes, '$."gen_ai.request.model"'),
--     'none'
--   ) AS model_name,
--   COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.input_tokens"') AS INT64), 0) AS input_tokens,
--   COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.cache_read.input_tokens"') AS INT64), 0) AS cached_input_tokens,
--   COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.output_tokens"') AS INT64), 0) AS output_tokens,
--   COALESCE(
--     SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.reasoning.output_tokens"') AS INT64),
--     SAFE_CAST(JSON_VALUE(attributes, '$."gen_ai.usage.thoughts_tokens"') AS INT64),
--     0
--   ) AS thinking_tokens,
--   JSON_VALUE(attributes, '$."gen_ai.tool.name"') AS tool_name,
--   CASE
--     WHEN JSON_VALUE(attributes, '$."gen_ai.operation.name"') = 'execute_tool' THEN
--       COALESCE(
--         JSON_VALUE(attributes, '$."tool.call.status"'),
--         CASE
--           WHEN JSON_VALUE(attributes, '$."error.type"') IN ('VALIDATION_ERROR', 'TIMEOUT', 'RATE_LIMITED')
--             THEN JSON_VALUE(attributes, '$."error.type"')
--           WHEN CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR')
--             THEN 'VALIDATION_ERROR'
--           ELSE 'SUCCESS'
--         END
--       )
--     ELSE NULL
--   END AS tool_call_status,
--   COALESCE(
--     SAFE_CAST(JSON_VALUE(attributes, '$."rule0.pass"') AS BOOL),
--     IF(
--       STARTS_WITH(name, 'a2a.server.events.event_queue'),
--       TRUE,
--       IF(CAST(status.code AS STRING) IN ('2', 'ERROR', 'STATUS_CODE_ERROR'), FALSE, TRUE)
--     )
--   ) AS rule0_pass,
--   COALESCE(JSON_VALUE(attributes, '$."finops.traffic_type"'), 'production_inference') AS finops_traffic_type,
--   JSON_VALUE(attributes, '$."outcome.id"') AS outcome_id,
--   COALESCE(
--     JSON_VALUE(attributes, '$."outcome.owner"'),
--     JSON_VALUE(resource.attributes, '$."apphub.application.id"'),
--     JSON_VALUE(resource.attributes, '$."gcp.apphub.application.id"')
--   ) AS outcome_owner,
--   JSON_VALUE(attributes, '$."source_mode"') AS source_mode,
--   JSON_VALUE(attributes, '$."skill_id"') AS skill_id,
--   JSON_VALUE(attributes, '$."skill_version"') AS skill_version,
--   JSON_VALUE(attributes, '$."entity_id"') AS entity_id,
--   attributes
-- FROM `obs_traces._AllSpans`;
--
-- CREATE OR REPLACE VIEW `agent_eval.otel_spans` AS
-- SELECT * FROM `agent_eval.v_otel_allspans_only`
-- UNION ALL
-- SELECT b.*
-- FROM `agent_eval.v_bqaa_otel_spans` AS b
-- WHERE NOT EXISTS (
--   SELECT 1
--   FROM `agent_eval.v_otel_allspans_only` AS f
--   WHERE f.trace_id = b.trace_id
-- );
--
-- CREATE OR REPLACE VIEW `agent_eval.v_otel_logs_payloads_only` AS
-- SELECT
--   COALESCE(
--     REGEXP_EXTRACT(l.trace, r'traces/([0-9a-fA-F]+)$'),
--     l.trace,
--     JSON_VALUE(l.json_payload, '$."trace_id"')
--   ) AS trace_id,
--   ANY_VALUE(
--     COALESCE(
--       JSON_VALUE(l.resource.labels, '$."apphub.application.id"'),
--       JSON_VALUE(l.json_payload, '$."apphub.application.id"'),
--       JSON_VALUE(l.json_payload, '$."gcp.apphub.application.id"'),
--       'unregistered_app'
--     )
--   ) AS apphub_application_id,
--   MAX(l.timestamp) AS ingested_at,
--   COALESCE(
--     STRING_AGG(
--       COALESCE(
--         JSON_VALUE(c.payload, '$."user_prompt"'),
--         JSON_VALUE(l.json_payload, '$."gen_ai.prompt"'),
--         JSON_VALUE(l.json_payload, '$."user_prompt"')
--       ),
--       '\n' ORDER BY l.timestamp
--     ),
--     ''
--   ) AS user_prompt,
--   COALESCE(
--     STRING_AGG(
--       COALESCE(
--         JSON_VALUE(c.payload, '$."retrieved_context"'),
--         JSON_VALUE(l.json_payload, '$."gen_ai.retrieved_context"'),
--         JSON_VALUE(l.json_payload, '$."retrieved_context"')
--       ),
--       '\n' ORDER BY l.timestamp
--     ),
--     ''
--   ) AS retrieved_context,
--   COALESCE(
--     ARRAY_AGG(
--       COALESCE(
--         JSON_VALUE(c.payload, '$."final_response"'),
--         JSON_VALUE(l.json_payload, '$."gen_ai.completion"'),
--         JSON_VALUE(l.json_payload, '$."final_response"')
--       ) IGNORE NULLS ORDER BY l.timestamp DESC LIMIT 1
--     )[SAFE_OFFSET(0)],
--     ''
--   ) AS final_response
-- FROM `obs_logs._AllLogs` AS l
-- LEFT JOIN `agent_eval.completions` AS c
--   ON c._FILE_NAME = COALESCE(
--     JSON_VALUE(l.json_payload, '$."gen_ai.completion.uri"'),
--     JSON_VALUE(l.json_payload, '$."gen_ai.input.messages_uri"'),
--     JSON_VALUE(l.json_payload, '$."upload_uri"')
--   )
-- WHERE COALESCE(l.trace, JSON_VALUE(l.json_payload, '$."trace_id"')) IS NOT NULL
-- GROUP BY trace_id;
--
-- CREATE OR REPLACE VIEW `agent_eval.trace_payloads` AS
-- SELECT * FROM `agent_eval.v_otel_logs_payloads_only`
-- UNION ALL
-- SELECT b.*
-- FROM `agent_eval.v_bqaa_trace_payloads` AS b
-- WHERE NOT EXISTS (
--   SELECT 1
--   FROM `agent_eval.v_otel_logs_payloads_only` AS f
--   WHERE f.trace_id = b.trace_id
-- );
