-- Silver Events & Outcome Links (`events_base`, `silver_restricted.events`, `silver_open.events_open`, `silver_open.outcome_links`).
-- Unions the 3 isolated `workflow_events` copies, closing spans, deduplicated span-event `guardrail.intervention`
-- (multi-event offset safe), and `_AllLogs` `gen_ai.evaluation.result` with gateway/root tenant & midnight-boundary safety.

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.events_base`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  -- Extend span lookup back by READ_MARGIN_MINUTES so midnight-straddling turns keep closing spans, owner, BU, and tenant.
  WITH base_spans AS (
    SELECT *
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.spans`(
      TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE),
      to_time
    )
  ),
  trace_owner AS (
    SELECT
      tm.trace_id,
      CASE WHEN tm.has_gw_caller THEN gw_r.app_id ELSE tm.root_app_id END AS trace_owner_app_id,
      tm.root_user_id AS trace_user_id,
      COALESCE(tm.gw_tenant_id, tm.root_tenant_id, tm.earliest_tenant_id) AS trace_tenant_id,
      ROW_NUMBER() OVER (PARTITION BY tm.trace_id ORDER BY gw_r.effective_from DESC) AS reg_rn
    FROM (
      SELECT
        s.trace_id,
        MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.app_id END) AS root_app_id,
        MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.user_id END) AS root_user_id,
        MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.tenant_id END) AS root_tenant_id,
        MAX(CASE WHEN s.source_class = 'platform' AND s.caller_app_id IS NOT NULL THEN 1 ELSE 0 END) = 1 AS has_gw_caller,
        ARRAY_AGG(CASE WHEN s.source_class = 'platform' THEN s.caller_app_id END IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_caller_app_id,
        ARRAY_AGG(CASE WHEN s.source_class = 'platform' AND s.caller_app_id IS NOT NULL THEN s.start_time END IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_start_time,
        ARRAY_AGG(CASE WHEN s.source_class = 'platform' THEN s.tenant_id END IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_tenant_id,
        ARRAY_AGG(s.tenant_id IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS earliest_tenant_id
      FROM (
        SELECT
          trace_id, span_id, start_time, source_class, app_id, caller_app_id, user_id, tenant_id,
          ROW_NUMBER() OVER (
            PARTITION BY trace_id
            ORDER BY CASE WHEN source_class IS NOT NULL THEN 0 ELSE 1 END, start_time ASC, span_id ASC
          ) AS root_rn
        FROM base_spans
      ) AS s
      GROUP BY s.trace_id
    ) AS tm
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS gw_r
      ON tm.gw_caller_app_id IS NOT NULL
      AND gw_r.app_id = tm.gw_caller_app_id
      AND gw_r.effective_from <= tm.gw_start_time
      AND (gw_r.effective_to IS NULL OR tm.gw_start_time < gw_r.effective_to)
  ),
  raw_events AS (
    -- 1. System-of-record workflow_events copy (authoritative).
    SELECT
      e.event_id, e.event_time, e.event_type, e.source_system,
      'system_of_record' AS source_class, TRUE AS is_authoritative,
      e.trace_id, e.span_id, e.response_id, e.outcome_id,
      JSON_VALUE(e.payload_json, '$.linked_outcome_id') AS linked_outcome_id,
      JSON_VALUE(e.payload_json, '$.outcome_status') AS outcome_status,
      JSON_VALUE(e.payload_json, '$.state_transition_to') AS state_transition_to,
      e.conversation_id,
      JSON_VALUE(e.payload_json, '$.outcome_owner') AS claimed_owner,
      e.action_id, e.nudge_id, e.control_id, e.control_layer, e.control_action,
      e.action_args, e.approved_expires_at,
      JSON_VALUE(e.payload_json, '$.feedback_reason') AS feedback_reason,
      e.user_id, e.tenant_id, e.actor_id, e.payload_json,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', COALESCE(JSON_VALUE(e.payload_json, '$.mcp_method_name'), JSON_VALUE(e.payload_json, '$."mcp.method.name"')),
        'a2a_task_id', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_id'), JSON_VALUE(e.payload_json, '$."a2a.task.id"')),
        'a2a_task_state', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_state'), JSON_VALUE(e.payload_json, '$."a2a.task.state"'))
      )) AS extra_restricted_labels,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', COALESCE(JSON_VALUE(e.payload_json, '$.mcp_method_name'), JSON_VALUE(e.payload_json, '$."mcp.method.name"')),
        'a2a_task_id', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_id'), JSON_VALUE(e.payload_json, '$."a2a.task.id"')),
        'a2a_task_state', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_state'), JSON_VALUE(e.payload_json, '$."a2a.task.state"'))
      )) AS extra_open_labels
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_sor` AS e
    WHERE e.event_time >= from_time AND e.event_time < to_time

    UNION ALL

    -- 2. Platform-controls workflow_events copy.
    SELECT
      e.event_id, e.event_time, e.event_type, e.source_system,
      'platform' AS source_class, FALSE AS is_authoritative,
      e.trace_id, e.span_id, e.response_id, e.outcome_id,
      JSON_VALUE(e.payload_json, '$.linked_outcome_id') AS linked_outcome_id,
      JSON_VALUE(e.payload_json, '$.outcome_status') AS outcome_status,
      JSON_VALUE(e.payload_json, '$.state_transition_to') AS state_transition_to,
      e.conversation_id,
      JSON_VALUE(e.payload_json, '$.outcome_owner') AS claimed_owner,
      e.action_id, e.nudge_id, e.control_id, e.control_layer, e.control_action,
      e.action_args, e.approved_expires_at,
      JSON_VALUE(e.payload_json, '$.feedback_reason') AS feedback_reason,
      e.user_id, e.tenant_id, e.actor_id, e.payload_json,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', COALESCE(JSON_VALUE(e.payload_json, '$.mcp_method_name'), JSON_VALUE(e.payload_json, '$."mcp.method.name"')),
        'a2a_task_id', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_id'), JSON_VALUE(e.payload_json, '$."a2a.task.id"')),
        'a2a_task_state', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_state'), JSON_VALUE(e.payload_json, '$."a2a.task.state"'))
      )) AS extra_restricted_labels,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', COALESCE(JSON_VALUE(e.payload_json, '$.mcp_method_name'), JSON_VALUE(e.payload_json, '$."mcp.method.name"')),
        'a2a_task_id', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_id'), JSON_VALUE(e.payload_json, '$."a2a.task.id"')),
        'a2a_task_state', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_state'), JSON_VALUE(e.payload_json, '$."a2a.task.state"'))
      )) AS extra_open_labels
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_platform` AS e
    WHERE e.event_time >= from_time AND e.event_time < to_time

    UNION ALL

    -- 3. Application/IDE workflow_events copy.
    SELECT
      e.event_id, e.event_time, e.event_type, e.source_system,
      'application' AS source_class, FALSE AS is_authoritative,
      e.trace_id, e.span_id, e.response_id, e.outcome_id,
      JSON_VALUE(e.payload_json, '$.linked_outcome_id') AS linked_outcome_id,
      JSON_VALUE(e.payload_json, '$.outcome_status') AS outcome_status,
      JSON_VALUE(e.payload_json, '$.state_transition_to') AS state_transition_to,
      e.conversation_id,
      JSON_VALUE(e.payload_json, '$.outcome_owner') AS claimed_owner,
      e.action_id, e.nudge_id, e.control_id, e.control_layer, e.control_action,
      e.action_args, e.approved_expires_at,
      JSON_VALUE(e.payload_json, '$.feedback_reason') AS feedback_reason,
      e.user_id, e.tenant_id, e.actor_id, e.payload_json,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', COALESCE(JSON_VALUE(e.payload_json, '$.mcp_method_name'), JSON_VALUE(e.payload_json, '$."mcp.method.name"')),
        'a2a_task_id', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_id'), JSON_VALUE(e.payload_json, '$."a2a.task.id"')),
        'a2a_task_state', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_state'), JSON_VALUE(e.payload_json, '$."a2a.task.state"'))
      )) AS extra_restricted_labels,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', COALESCE(JSON_VALUE(e.payload_json, '$.mcp_method_name'), JSON_VALUE(e.payload_json, '$."mcp.method.name"')),
        'a2a_task_id', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_id'), JSON_VALUE(e.payload_json, '$."a2a.task.id"')),
        'a2a_task_state', COALESCE(JSON_VALUE(e.payload_json, '$.a2a_task_state'), JSON_VALUE(e.payload_json, '$."a2a.task.state"'))
      )) AS extra_open_labels
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_app` AS e
    WHERE e.event_time >= from_time AND e.event_time < to_time

    UNION ALL

    -- 4. Closing spans (`outcome.closed`).
    SELECT
      CONCAT('span:', s.trace_id, ':', s.span_id) AS event_id,
      s.start_time AS event_time,
      'outcome.closed' AS event_type,
      JSON_VALUE(s.labels, '$.service_name') AS source_system,
      COALESCE(s.source_class, 'application') AS source_class,
      FALSE AS is_authoritative,
      s.trace_id, s.span_id, s.response_id, s.outcome_id,
      CAST(NULL AS STRING) AS linked_outcome_id,
      s.outcome_status,
      CAST(NULL AS STRING) AS state_transition_to,
      s.conversation_id,
      s.outcome_owner AS claimed_owner,
      s.action_id, s.nudge_id,
      CAST(NULL AS STRING) AS control_id,
      CAST(NULL AS STRING) AS control_layer,
      CAST(NULL AS STRING) AS control_action,
      CAST(NULL AS STRING) AS action_args,
      CAST(NULL AS TIMESTAMP) AS approved_expires_at,
      CAST(NULL AS STRING) AS feedback_reason,
      s.user_id, s.tenant_id,
      CAST(NULL AS STRING) AS actor_id,
      CAST(NULL AS JSON) AS payload_json,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', JSON_VALUE(s.labels, '$.mcp_method_name'),
        'a2a_task_id', JSON_VALUE(s.labels, '$.a2a_task_id'),
        'a2a_task_state', JSON_VALUE(s.labels, '$.a2a_task_state')
      )) AS extra_restricted_labels,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', JSON_VALUE(s.labels, '$.mcp_method_name'),
        'a2a_task_id', JSON_VALUE(s.labels, '$.a2a_task_id'),
        'a2a_task_state', JSON_VALUE(s.labels, '$.a2a_task_state')
      )) AS extra_open_labels
    FROM base_spans AS s
    WHERE s.span_name = 'outcome.closed'
      AND s.start_time >= from_time AND s.start_time < to_time

    UNION ALL

    -- 5. Deduplicated span-event `guardrail.intervention` with multi-event offset keying.
    SELECT
      CASE
        WHEN g.gi_rn = 1 THEN CONCAT('span:', g.trace_id, ':', g.span_id)
        ELSE CONCAT('span:', g.trace_id, ':', g.span_id, ':', CAST(g.gi_rn AS STRING))
      END AS event_id,
      SAFE_CAST(g.ev_time AS TIMESTAMP) AS event_time,
      'guardrail.intervention' AS event_type,
      g.source_system, g.source_class, FALSE AS is_authoritative,
      g.trace_id, g.span_id, g.response_id, g.outcome_id,
      CAST(NULL AS STRING) AS linked_outcome_id,
      CAST(NULL AS STRING) AS outcome_status,
      CAST(NULL AS STRING) AS state_transition_to,
      g.conversation_id, g.outcome_owner AS claimed_owner,
      g.action_id, g.nudge_id,
      JSON_VALUE(g.ev_attributes, '$.control_id') AS control_id,
      JSON_VALUE(g.ev_attributes, '$.control_layer') AS control_layer,
      JSON_VALUE(g.ev_attributes, '$.control_action') AS control_action,
      CAST(NULL AS STRING) AS action_args,
      CAST(NULL AS TIMESTAMP) AS approved_expires_at,
      CAST(NULL AS STRING) AS feedback_reason,
      g.user_id, g.tenant_id,
      CAST(NULL AS STRING) AS actor_id,
      CASE WHEN g.ev_attributes IS NOT NULL THEN SAFE.PARSE_JSON(TO_JSON_STRING(g.ev_attributes)) ELSE CAST(NULL AS JSON) END AS payload_json,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', g.mcp_method_name,
        'a2a_task_id', g.a2a_task_id,
        'a2a_task_state', g.a2a_task_state
      )) AS extra_restricted_labels,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', g.mcp_method_name,
        'a2a_task_id', g.a2a_task_id,
        'a2a_task_state', g.a2a_task_state
      )) AS extra_open_labels
    FROM (
      SELECT
        d.*,
        ROW_NUMBER() OVER (PARTITION BY d.source_class, d.trace_id, d.span_id ORDER BY d.ev_idx ASC) AS gi_rn
      FROM (
        SELECT
          'application' AS source_class, s.trace_id, s.span_id,
          JSON_VALUE(s.resource.attributes, '$."service.name"') AS source_system,
          JSON_VALUE(s.attributes, '$."gen_ai.response.id"') AS response_id,
          JSON_VALUE(s.attributes, '$."outcome.id"') AS outcome_id,
          JSON_VALUE(s.attributes, '$."outcome.owner"') AS outcome_owner,
          JSON_VALUE(s.attributes, '$."gen_ai.conversation.id"') AS conversation_id,
          JSON_VALUE(s.attributes, '$."bq_eval.action_id"') AS action_id,
          JSON_VALUE(s.attributes, '$."bq_eval.nudge_id"') AS nudge_id,
          JSON_VALUE(s.attributes, '$."user.id"') AS user_id,
          JSON_VALUE(s.attributes, '$."bq_eval.tenant_id"') AS tenant_id,
          JSON_VALUE(s.attributes, '$."mcp.method.name"') AS mcp_method_name,
          JSON_VALUE(s.attributes, '$."a2a.task.id"') AS a2a_task_id,
          JSON_VALUE(s.attributes, '$."a2a.task.state"') AS a2a_task_state,
          ev.time AS ev_time,
          ev.attributes AS ev_attributes,
          ev_idx
        FROM (
          SELECT
            trace_id, span_id, receive_time, end_time, attributes, resource, events,
            ROW_NUMBER() OVER (PARTITION BY trace_id, span_id ORDER BY receive_time DESC, end_time DESC) AS span_rn
          FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans`
          WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
            AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        ) AS s
        CROSS JOIN UNNEST(s.events) AS ev WITH OFFSET AS ev_idx
        WHERE s.span_rn = 1 AND ev.name = 'guardrail.intervention'

        UNION ALL

        SELECT
          'platform' AS source_class, s.trace_id, s.span_id,
          JSON_VALUE(s.resource.attributes, '$."service.name"') AS source_system,
          JSON_VALUE(s.attributes, '$."gen_ai.response.id"') AS response_id,
          JSON_VALUE(s.attributes, '$."outcome.id"') AS outcome_id,
          JSON_VALUE(s.attributes, '$."outcome.owner"') AS outcome_owner,
          JSON_VALUE(s.attributes, '$."gen_ai.conversation.id"') AS conversation_id,
          JSON_VALUE(s.attributes, '$."bq_eval.action_id"') AS action_id,
          JSON_VALUE(s.attributes, '$."bq_eval.nudge_id"') AS nudge_id,
          JSON_VALUE(s.attributes, '$."user.id"') AS user_id,
          JSON_VALUE(s.attributes, '$."bq_eval.tenant_id"') AS tenant_id,
          JSON_VALUE(s.attributes, '$."mcp.method.name"') AS mcp_method_name,
          JSON_VALUE(s.attributes, '$."a2a.task.id"') AS a2a_task_id,
          JSON_VALUE(s.attributes, '$."a2a.task.state"') AS a2a_task_state,
          ev.time AS ev_time,
          ev.attributes AS ev_attributes,
          ev_idx
        FROM (
          SELECT
            trace_id, span_id, receive_time, end_time, attributes, resource, events,
            ROW_NUMBER() OVER (PARTITION BY trace_id, span_id ORDER BY receive_time DESC, end_time DESC) AS span_rn
          FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}`
          WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
            AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        ) AS s
        CROSS JOIN UNNEST(s.events) AS ev WITH OFFSET AS ev_idx
        WHERE s.span_rn = 1 AND ev.name = 'guardrail.intervention'
      ) AS d
    ) AS g
    WHERE SAFE_CAST(g.ev_time AS TIMESTAMP) >= from_time
      AND SAFE_CAST(g.ev_time AS TIMESTAMP) < to_time

    UNION ALL

    -- 6. OTel `gen_ai.evaluation.result` log rows (`_AllLogs`).
    SELECT
      l.insert_id AS event_id,
      l.timestamp AS event_time,
      CASE
        WHEN JSON_VALUE(l.labels, '$."gen_ai.evaluation.score.label"') = 'thumbs_up' THEN 'feedback.thumbs_up'
        WHEN JSON_VALUE(l.labels, '$."gen_ai.evaluation.score.label"') = 'thumbs_down' THEN 'feedback.thumbs_down'
        ELSE COALESCE(JSON_VALUE(l.labels, '$."gen_ai.evaluation.name"'), 'gen_ai.evaluation.result')
      END AS event_type,
      JSON_VALUE(l.otel, '$.resource.attributes."service.name"') AS source_system,
      'application' AS source_class,
      FALSE AS is_authoritative,
      NULLIF(REGEXP_REPLACE(COALESCE(l.trace, ''), r'^projects/[^/]+/traces/', ''), '') AS trace_id,
      NULLIF(l.span_id, '') AS span_id,
      JSON_VALUE(l.labels, '$."gen_ai.response.id"') AS response_id,
      JSON_VALUE(l.labels, '$."outcome.id"') AS outcome_id,
      CAST(NULL AS STRING) AS linked_outcome_id,
      CAST(NULL AS STRING) AS outcome_status,
      CAST(NULL AS STRING) AS state_transition_to,
      JSON_VALUE(l.labels, '$."gen_ai.conversation.id"') AS conversation_id,
      CAST(NULL AS STRING) AS claimed_owner,
      CAST(NULL AS STRING) AS action_id,
      JSON_VALUE(l.labels, '$."bq_eval.nudge_id"') AS nudge_id,
      CAST(NULL AS STRING) AS control_id,
      CAST(NULL AS STRING) AS control_layer,
      CAST(NULL AS STRING) AS control_action,
      CAST(NULL AS STRING) AS action_args,
      CAST(NULL AS TIMESTAMP) AS approved_expires_at,
      JSON_VALUE(l.labels, '$."bq_eval.feedback_reason"') AS feedback_reason,
      JSON_VALUE(l.labels, '$."user.id"') AS user_id,
      JSON_VALUE(l.labels, '$."bq_eval.tenant_id"') AS tenant_id,
      CAST(NULL AS STRING) AS actor_id,
      l.labels AS payload_json,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', JSON_VALUE(l.labels, '$."mcp.method.name"'),
        'a2a_task_id', JSON_VALUE(l.labels, '$."a2a.task.id"'),
        'a2a_task_state', JSON_VALUE(l.labels, '$."a2a.task.state"'),
        'evaluation_name', JSON_VALUE(l.labels, '$."gen_ai.evaluation.name"'),
        'evaluation_score_value', SAFE_CAST(JSON_VALUE(l.labels, '$."gen_ai.evaluation.score.value"') AS FLOAT64),
        'evaluation_score_label', JSON_VALUE(l.labels, '$."gen_ai.evaluation.score.label"'),
        'evaluation_explanation', JSON_VALUE(l.labels, '$."gen_ai.evaluation.explanation"')
      )) AS extra_restricted_labels,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'mcp_method_name', JSON_VALUE(l.labels, '$."mcp.method.name"'),
        'a2a_task_id', JSON_VALUE(l.labels, '$."a2a.task.id"'),
        'a2a_task_state', JSON_VALUE(l.labels, '$."a2a.task.state"'),
        'evaluation_name', JSON_VALUE(l.labels, '$."gen_ai.evaluation.name"'),
        'evaluation_score_value', SAFE_CAST(JSON_VALUE(l.labels, '$."gen_ai.evaluation.score.value"') AS FLOAT64),
        'evaluation_score_label', JSON_VALUE(l.labels, '$."gen_ai.evaluation.score.label"')
      )) AS extra_open_labels
    FROM `${PROJECT_ID}.${LOGS_DATASET_ID}._AllLogs` AS l
    WHERE l.log_id = 'gen_ai.evaluation.result'
      AND l.timestamp >= from_time AND l.timestamp < to_time
  ),
  signed AS (
    SELECT
      r.*,
      TO_JSON_STRING(JSON_OBJECT(
        'action_args', r.action_args, 'action_id', r.action_id, 'actor_id', r.actor_id,
        'approved_expires_at', FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S', r.approved_expires_at),
        'control_action', r.control_action, 'control_id', r.control_id, 'control_layer', r.control_layer,
        'conversation_id', r.conversation_id, 'event_type', r.event_type, 'nudge_id', r.nudge_id,
        'outcome_id', r.outcome_id,
        'payload_json', CASE WHEN r.payload_json IS NOT NULL THEN TO_JSON_STRING(r.payload_json) END,
        'response_id', r.response_id, 'source_system', r.source_system, 'span_id', r.span_id,
        'tenant_id', r.tenant_id, 'trace_id', r.trace_id, 'user_id', r.user_id
      )) AS row_content_sig
    FROM raw_events AS r
  ),
  ranked AS (
    SELECT
      s.*,
      ROW_NUMBER() OVER (PARTITION BY s.source_class, s.event_id ORDER BY s.event_time ASC, s.row_content_sig ASC) AS ev_rn,
      FIRST_VALUE(s.row_content_sig) OVER (PARTITION BY s.source_class, s.event_id ORDER BY s.event_time ASC, s.row_content_sig ASC) AS winning_sig
    FROM signed AS s
  ),
  with_counts AS (
    SELECT
      r.*,
      SUM(CASE WHEN r.ev_rn > 1 AND r.row_content_sig = r.winning_sig THEN 1 ELSE 0 END) OVER (PARTITION BY r.source_class, r.event_id) AS duplicate_count,
      SUM(CASE WHEN r.ev_rn > 1 AND r.row_content_sig != r.winning_sig THEN 1 ELSE 0 END) OVER (PARTITION BY r.source_class, r.event_id) AS conflict_count
    FROM ranked AS r
  ),
  with_bu AS (
    SELECT
      w.*,
      r.app_id AS verified_app_id,
      r.business_unit,
      -- Trace-level gateway/root tenant takes precedence over untrusted child spans (blocks UC9 cross-tenant spoofing).
      COALESCE(to_map.trace_tenant_id, w.tenant_id) AS resolved_tenant_id,
      COALESCE(w.user_id, to_map.trace_user_id) AS resolved_user_id,
      ROW_NUMBER() OVER (PARTITION BY w.source_class, w.event_id ORDER BY r.effective_from DESC) AS bu_rn
    FROM with_counts AS w
    LEFT JOIN trace_owner AS to_map ON to_map.trace_id = w.trace_id AND to_map.reg_rn = 1
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
      ON r.app_id = COALESCE(to_map.trace_owner_app_id, w.claimed_owner)
      AND r.effective_from <= w.event_time
      AND (r.effective_to IS NULL OR w.event_time < r.effective_to)
    WHERE w.ev_rn = 1
  )
  SELECT
    event_id, event_time, event_type, source_system, source_class, is_authoritative,
    trace_id, span_id, response_id, outcome_id, linked_outcome_id, outcome_status, state_transition_to,
    conversation_id, verified_app_id AS trace_owner_app_id, action_id, nudge_id,
    control_id, control_layer, control_action, action_args,
    CASE WHEN event_type = 'outcome.approved' AND action_args IS NOT NULL THEN `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_sha256`(action_args) ELSE CAST(NULL AS STRING) END AS approved_args_sha256,
    CASE WHEN event_type = 'action.committed' AND action_args IS NOT NULL THEN `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_sha256`(action_args) ELSE CAST(NULL AS STRING) END AS committed_args_sha256,
    approved_expires_at, feedback_reason, duplicate_count, conflict_count,
    resolved_user_id AS user_id,
    resolved_tenant_id AS tenant_id,
    actor_id, payload_json, business_unit,
    CASE WHEN extra_restricted_labels IS NULL OR TO_JSON_STRING(extra_restricted_labels) = '{}' THEN CAST(NULL AS JSON) ELSE extra_restricted_labels END AS restricted_labels,
    CASE WHEN extra_open_labels IS NULL OR TO_JSON_STRING(extra_open_labels) = '{}' THEN CAST(NULL AS JSON) ELSE extra_open_labels END AS open_labels
  FROM with_bu
  WHERE bu_rn = 1
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.events`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  SELECT
    e.event_id, e.event_time, e.event_type, e.source_system, e.source_class, e.is_authoritative,
    e.trace_id, e.span_id, e.response_id, e.outcome_id, e.linked_outcome_id, e.outcome_status, e.state_transition_to,
    e.conversation_id, e.trace_owner_app_id, e.action_id, e.nudge_id, e.control_id, e.control_layer, e.control_action,
    e.action_args, e.approved_args_sha256, e.committed_args_sha256, e.approved_expires_at, e.feedback_reason,
    e.duplicate_count, e.conflict_count, e.user_id, e.tenant_id, e.actor_id, e.payload_json,
    e.restricted_labels AS labels
  FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.events_base`(from_time, to_time) AS e
  WHERE EXISTS (
    SELECT 1
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.unit_entitlements` AS ue
    WHERE LOWER(ue.principal_email) = LOWER(SESSION_USER())
      AND ue.business_unit = COALESCE(e.business_unit, '__unowned__')
      AND (ue.tenant_id = '*' OR ue.tenant_id = e.tenant_id)
  )
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_OPEN_DATASET}.events_open`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  SELECT
    event_id, event_time, event_type, source_system, source_class, is_authoritative,
    trace_id, span_id, response_id, outcome_id, linked_outcome_id, outcome_status, state_transition_to,
    conversation_id, trace_owner_app_id, action_id, nudge_id, control_id, control_layer, control_action,
    approved_expires_at, feedback_reason, duplicate_count, conflict_count,
    open_labels AS labels
  FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.events_base`(from_time, to_time)
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcome_links`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  -- Direct read of the three `workflow_events_*` copies (zero `_AllSpans` or `_AllLogs` scans).
  WITH raw_wf AS (
    SELECT 'system_of_record' AS source_class, event_id, event_time, event_type, source_system,
      trace_id, span_id, response_id, outcome_id, conversation_id, action_id, nudge_id,
      control_id, control_layer, control_action, action_args, approved_expires_at,
      user_id, tenant_id, actor_id, payload_json
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_sor`
    WHERE event_time >= from_time AND event_time < to_time
    UNION ALL
    SELECT 'platform' AS source_class, event_id, event_time, event_type, source_system,
      trace_id, span_id, response_id, outcome_id, conversation_id, action_id, nudge_id,
      control_id, control_layer, control_action, action_args, approved_expires_at,
      user_id, tenant_id, actor_id, payload_json
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_platform`
    WHERE event_time >= from_time AND event_time < to_time
    UNION ALL
    SELECT 'application' AS source_class, event_id, event_time, event_type, source_system,
      trace_id, span_id, response_id, outcome_id, conversation_id, action_id, nudge_id,
      control_id, control_layer, control_action, action_args, approved_expires_at,
      user_id, tenant_id, actor_id, payload_json
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_app`
    WHERE event_time >= from_time AND event_time < to_time
  ),
  ranked AS (
    SELECT
      r.event_type,
      r.outcome_id,
      JSON_VALUE(r.payload_json, '$.linked_outcome_id') AS linked_outcome_id,
      ROW_NUMBER() OVER (
        PARTITION BY r.source_class, r.event_id
        ORDER BY r.event_time ASC,
          TO_JSON_STRING(JSON_OBJECT(
            'action_args', r.action_args, 'action_id', r.action_id, 'actor_id', r.actor_id,
            'approved_expires_at', FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S', r.approved_expires_at),
            'control_action', r.control_action, 'control_id', r.control_id, 'control_layer', r.control_layer,
            'conversation_id', r.conversation_id, 'event_type', r.event_type, 'nudge_id', r.nudge_id,
            'outcome_id', r.outcome_id,
            'payload_json', CASE WHEN r.payload_json IS NOT NULL THEN TO_JSON_STRING(r.payload_json) END,
            'response_id', r.response_id, 'source_system', r.source_system, 'span_id', r.span_id,
            'tenant_id', r.tenant_id, 'trace_id', r.trace_id, 'user_id', r.user_id
          )) ASC
      ) AS ev_rn
    FROM raw_wf AS r
  )
  SELECT DISTINCT
    outcome_id AS primary_outcome_id,
    linked_outcome_id
  FROM ranked
  WHERE ev_rn = 1
    AND event_type = 'outcome.linked'
    AND outcome_id IS NOT NULL
    AND linked_outcome_id IS NOT NULL
);
