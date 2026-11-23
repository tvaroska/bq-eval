-- =============================================================================
-- BQ Eval & FinOps (bq-eval) — Dual-Path Adapter Views for BigQuery Agent Analytics (BQAA)
--   * Light Capture (BQAA): Maps `agent_events` (BigQueryAgentAnalyticsPlugin /
--     BigQueryCallbackHandler / Conversational Analytics) onto `agent_eval.otel_spans`
--     and `agent_eval.trace_payloads` with zero data copies and 0 external tables.
--   * Preserves ADR-003 / EVAL-P08 strict `'unregistered_app'` fallback: workloads
--     without explicit `custom_metadata."apphub.application.id"` (or server-stamped
--     `_AllSpans` join) resolve directly to `'unregistered_app'`, never collapsing
--     into self-reported `adk.app_name` or `agent` strings.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS `agent_eval`
OPTIONS (
  description = 'BQ Eval & FinOps: Light Capture (BQAA) and Hybrid Dual-Path adapter dataset.'
);

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
  -- ADR-003 / EVAL-P08 strict App Hub resolution (NULL -> 'unregistered_app' downstream)
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
    WHEN event_type = 'TOOL_COMPLETED' THEN 'SUCCESS'
    WHEN event_type = 'TOOL_ERROR' THEN COALESCE(
      NULLIF(
        NULLIF(
          UPPER(
            COALESCE(
              JSON_VALUE(attributes, '$."tool.call.status"'),
              JSON_VALUE(attributes, '$."error.type"'),
              JSON_VALUE(attributes, '$.error_type'),
              JSON_VALUE(content, '$.error_type')
            )
          ),
          'OK'
        ),
        'SUCCESS'
      ),
      CASE
        WHEN SAFE_CAST(JSON_VALUE(content, '$.code') AS INT64) IN (408, 503, 504)
          OR REGEXP_CONTAINS(LOWER(COALESCE(error_message, '')), r'timeout|timed\s*out|503|504')
          THEN 'TIMEOUT'
        WHEN SAFE_CAST(JSON_VALUE(content, '$.code') AS INT64) = 429
          OR REGEXP_CONTAINS(LOWER(COALESCE(error_message, '')), r'rate[_\s-]?limit|429')
          THEN 'RATE_LIMITED'
        WHEN SAFE_CAST(JSON_VALUE(content, '$.code') AS INT64) IN (400, 422)
          OR REGEXP_CONTAINS(LOWER(COALESCE(error_message, '')), r'validat|schema|invalid')
          THEN 'VALIDATION_ERROR'
        ELSE 'TOOL_ERROR'
      END
    )
    ELSE NULL
  END AS tool_call_status,
  NOT (UPPER(COALESCE(status, 'OK')) = 'ERROR' OR event_type LIKE '%_ERROR') AS rule0_pass,
  COALESCE(
    JSON_VALUE(attributes, '$.custom_metadata."finops.traffic_type"'),
    'production_inference'
  ) AS finops_traffic_type,
  -- Preserve NULL when outcome.id is absent so unattributed shadow workloads surface in v_attribution_coverage
  JSON_VALUE(attributes, '$.custom_metadata."outcome.id"') AS outcome_id,
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
  CASE UPPER(COALESCE(
    JSON_VALUE(attributes, '$.usage_metadata.traffic_type'),
    JSON_VALUE(attributes, '$.custom_metadata."serving.traffic_type"'),
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
    JSON_VALUE(attributes, '$.custom_metadata."x-vertex-ai-llm-request-type"'),
    JSON_VALUE(attributes, '$.custom_metadata.request_type_header')
  ) AS request_type_header,
  COALESCE(
    SAFE_CAST(JSON_VALUE(content, '$.code') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$."http.response.status_code"') AS INT64),
    CASE
      WHEN UPPER(COALESCE(status, 'OK')) = 'ERROR' OR event_type LIKE '%_ERROR' THEN 500
      ELSE 200
    END
  ) AS http_status_code,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_write_5m_tokens') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.ephemeral_5m_input_tokens') AS INT64),
    0
  ) AS cache_write_5m_tokens,
  COALESCE(
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_write_1h_tokens') AS INT64),
    SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.ephemeral_1h_input_tokens') AS INT64),
    0
  ) AS cache_write_1h_tokens,
  COALESCE(
    JSON_VALUE(attributes, '$.custom_metadata."interaction.session_id"'),
    session_id
  ) AS session_id,
  SAFE_CAST(
    COALESCE(
      JSON_VALUE(attributes, '$.custom_metadata."interaction.turn_index"'),
      JSON_VALUE(attributes, '$.turn_index')
    ) AS INT64
  ) AS turn_index,
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
  COALESCE(
    ARRAY_AGG(
      COALESCE(
        JSON_VALUE(attributes, '$.custom_metadata."gcp.apphub.application.id"'),
        JSON_VALUE(attributes, '$.custom_metadata."apphub.application.id"'),
        JSON_VALUE(attributes, '$.custom_metadata.apphub_application_id')
      )
      IGNORE NULLS ORDER BY timestamp ASC, event_id ASC LIMIT 1
    )[SAFE_OFFSET(0)],
    'unregistered_app'
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
--   * Matches ADR-004 routine Step-2 semantics (`step1_pass = TRUE` and
--     `NOT has_tool_loop`) over BQAA's `{table}_eval_turn` (`AgentData` JSON)
--     to skip $0 deterministic rejects before invoking `AI.EVALUATE_AGENT`.
--   * Any optional diagnostic tail runs billed as `autorater_judge_usd` are still
--     excluded from calibrated theta_RG via `WHERE rule0_pass = TRUE`.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_bqaa_eval_turn_filtered` AS
WITH default_skus AS (
  SELECT 'gemini-4-argon' AS model_name, 1.25 AS input_usd_per_1m, 0.125 AS cached_input_usd_per_1m, 5.00 AS output_usd_per_1m, 200000 AS long_context_threshold_tokens, 2.50 AS long_context_input_usd_per_1m, 0.250 AS long_context_cached_usd_per_1m, 18.75 AS long_context_output_usd_per_1m
  UNION ALL
  SELECT 'gemini-3.5-flash-lite', 0.075, 0.0075, 0.30, 200000, 0.15, 0.015, 1.125
  UNION ALL
  SELECT 'gemini-3.8-flash', 0.15, 0.015, 0.60, 200000, 0.30, 0.030, 2.25
  UNION ALL
  SELECT 'ai_evaluate_agent_managed', 0.15, 0.015, 0.60, 200000, 0.30, 0.030, 2.25
),
bqaa_span_costs AS (
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
      WHEN s.input_tokens > COALESCE(p.long_context_threshold_tokens, 200000) THEN
        (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * p.long_context_input_usd_per_1m
        + (s.cached_input_tokens / 1e6) * p.long_context_cached_usd_per_1m
        + ((s.output_tokens + s.thinking_tokens) / 1e6) * p.long_context_output_usd_per_1m
      ELSE
        (GREATEST(0, s.input_tokens - s.cached_input_tokens) / 1e6) * p.input_usd_per_1m
        + (s.cached_input_tokens / 1e6) * p.cached_input_usd_per_1m
        + ((s.output_tokens + s.thinking_tokens) / 1e6) * p.output_usd_per_1m
    END AS span_cost_usd
  FROM `agent_eval.v_bqaa_otel_spans` AS s
  LEFT JOIN default_skus AS p
    ON REGEXP_REPLACE(s.model_name, r'-(?:\d{3}|ga|rc|qcd|preview)$', '') = p.model_name
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

-- -----------------------------------------------------------------------------
-- 4. Live Mode A (Light Capture) & Mode C (Hybrid Dual-Path) Canonical Views
--    `bq-eval bootstrap --mode light|full|hybrid` binds `otel_spans` and
--    `trace_payloads` to the selected mode with zero manual SQL edits.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_mode_a_otel_spans` AS
SELECT * FROM `agent_eval.v_bqaa_otel_spans`;

CREATE OR REPLACE VIEW `agent_eval.v_mode_a_trace_payloads` AS
SELECT * FROM `agent_eval.v_bqaa_trace_payloads`;

CREATE OR REPLACE VIEW `agent_eval.v_mode_c_hybrid_otel_spans` AS
SELECT * FROM `agent_eval.v_otel_allspans_only`
UNION ALL
SELECT b.*
FROM `agent_eval.v_bqaa_otel_spans` AS b
WHERE NOT EXISTS (
  SELECT 1 FROM `agent_eval.v_otel_allspans_only` AS f WHERE f.trace_id = b.trace_id
);

CREATE OR REPLACE VIEW `agent_eval.v_mode_c_hybrid_trace_payloads` AS
SELECT * FROM `agent_eval.v_otel_logs_payloads_only`
UNION ALL
SELECT b.*
FROM `agent_eval.v_bqaa_trace_payloads` AS b
WHERE NOT EXISTS (
  SELECT 1 FROM `agent_eval.v_otel_logs_payloads_only` AS f WHERE f.trace_id = b.trace_id
);
