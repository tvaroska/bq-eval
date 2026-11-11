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
    WHEN event_type IN ('AGENT_COMPLETED', 'AGENT_ERROR') THEN 'invoke_agent'
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
  attributes
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
--   * Filters BQAA's `{table}_eval_turn` (`AgentData` JSON `trace` column) through
--     Step-1 deterministic SQL gates (`step1_pass = TRUE` and `NOT has_tool_loop`)
--     before invoking `AI.EVALUATE_AGENT`.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_bqaa_eval_turn_filtered` AS
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
INNER JOIN `agent_eval.v_step1_trace_eval` AS s1
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
--   * Server-stamped `_AllSpans` rows take precedence when an agent emits both;
--     `BQAA`-only developer/off-GCP traces fill in via `NOT EXISTS` on `trace_id`.
-- CREATE OR REPLACE VIEW `agent_eval.v_otel_allspans_only` AS
-- SELECT * FROM `agent_eval.otel_spans`;
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
