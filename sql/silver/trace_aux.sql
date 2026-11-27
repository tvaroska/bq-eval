-- Silver Internal Trace Auxiliary Rollup (`silver_internal.trace_aux_agg`, `ADR-042`, `ADR-048`).
-- Single base scan combining deduplicated `guardrail.intervention` / `outcome.escalated` counts
-- and per-agent `extra_user_turns` (2-hop `is_model_call` + ADK `call_llm` parent `input_msgs` lift).

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.trace_aux_agg`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH wf_events AS (
    SELECT
      trace_id,
      COUNTIF(event_type = 'guardrail.intervention') AS wf_gi_c,
      MAX(IF(event_type = 'outcome.escalated', 1, 0)) = 1 AS wf_ev_esc,
      STRING_AGG(IF(event_type = 'guardrail.intervention', CONCAT(sc, '|', event_id), NULL), ',') AS wf_gi_keys
    FROM (
      SELECT
        sc, event_id, event_type, trace_id,
        ROW_NUMBER() OVER (PARTITION BY sc, event_id ORDER BY event_time ASC) AS rn
      FROM (
        SELECT 'system_of_record' AS sc, event_id, event_time, event_type, trace_id
        FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_sor`
        UNION ALL
        SELECT 'platform' AS sc, event_id, event_time, event_type, trace_id
        FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_platform`
        UNION ALL
        SELECT 'application' AS sc, event_id, event_time, event_type, trace_id
        FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_app`
      )
      WHERE event_time >= from_time
        AND event_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        AND event_type IN ('guardrail.intervention', 'outcome.escalated')
        AND trace_id IS NOT NULL
    )
    WHERE rn = 1
    GROUP BY trace_id
  ),
  raw_union AS (
    SELECT
      'application' AS raw_source,
      CASE WHEN s.instrumentation_scope.name IS NULL AND s.kind IS NULL THEN CAST(NULL AS STRING) ELSE 'application' END AS source_class,
      s.trace_id, s.span_id, s.parent_span_id, COALESCE(s.name, '') AS span_name,
      s.start_time, s.end_time, s.receive_time, s.attributes,
      CASE WHEN ARRAY_LENGTH(s.events) > 0 THEN TO_JSON(s.events) ELSE CAST(NULL AS JSON) END AS events_json
    FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans` AS s
    WHERE s.start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      AND s.start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    UNION ALL
    SELECT
      'platform' AS raw_source,
      CASE WHEN s.instrumentation_scope.name IS NULL AND s.kind IS NULL THEN CAST(NULL AS STRING) ELSE 'platform' END AS source_class,
      s.trace_id, s.span_id, s.parent_span_id, COALESCE(s.name, '') AS span_name,
      s.start_time, s.end_time, s.receive_time, s.attributes,
      CASE WHEN ARRAY_LENGTH(s.events) > 0 THEN TO_JSON(s.events) ELSE CAST(NULL AS JSON) END AS events_json
    FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}` AS s
    WHERE s.start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      AND s.start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
  ),
  raw_parsed AS (
    SELECT
      r.raw_source, r.source_class, r.trace_id, r.span_id, r.parent_span_id, r.span_name, r.start_time, r.events_json,
      JSON_VALUE(r.attributes, '$."gen_ai.operation.name"') AS operation_name,
      CASE
        WHEN JSON_VALUE(r.attributes, '$."gen_ai.operation.name"') IN ('chat', 'generate_content', 'call_llm') THEN 'model_call'
        WHEN JSON_VALUE(r.attributes, '$."gen_ai.operation.name"') = 'embeddings' THEN 'embedding'
        WHEN JSON_VALUE(r.attributes, '$."gen_ai.operation.name"') = 'invoke_agent' THEN 'agent'
        ELSE 'other'
      END AS span_kind,
      JSON_VALUE(r.attributes, '$."gen_ai.agent.name"') AS agent_name,
      (
        COALESCE(SAFE_CAST(JSON_VALUE(r.attributes, '$."gen_ai.usage.input_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(r.attributes, '$."gen_ai.usage.output_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(r.attributes, '$."gen_ai.usage.cache_read.input_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(r.attributes, '$."gen_ai.usage.cache_write.input_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(r.attributes, '$."gen_ai.usage.reasoning.output_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(r.attributes, '$."gen_ai.usage.thoughts_tokens"') AS INT64), 0) > 0
      ) AS has_tok,
      COALESCE(
        SAFE.PARSE_JSON(JSON_VALUE(r.attributes, '$."gen_ai.input.messages"')),
        SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(r.attributes, '$."gen_ai.input.messages"')), 'null')),
        SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(
          COALESCE(
            SAFE.PARSE_JSON(NULLIF(JSON_VALUE(r.attributes, '$."gcp.vertex.agent.llm_request"'), '{}')),
            SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(r.attributes, '$."gcp.vertex.agent.llm_request"')), 'null'))
          ), '$.contents')), 'null'))
      ) AS input_msgs,
      ROW_NUMBER() OVER (PARTITION BY r.raw_source, r.trace_id, r.span_id ORDER BY r.receive_time DESC, r.end_time DESC) AS place_rn,
      ROW_NUMBER() OVER (
        PARTITION BY r.trace_id, r.span_id
        ORDER BY CASE WHEN r.source_class = 'platform' THEN 0 WHEN r.source_class = 'application' THEN 1 ELSE 2 END,
          r.receive_time DESC, r.end_time DESC, r.span_id ASC
      ) AS cross_rn
    FROM raw_union AS r
  ),
  raw_anc1 AS (
    SELECT
      rp.*,
      MIN(IF(rp.cross_rn = 1, rp.start_time, NULL)) OVER (PARTITION BY rp.trace_id) AS trace_first_start_time,
      STRING_AGG(IF(rp.cross_rn = 1 AND rp.has_tok AND rp.parent_span_id IS NOT NULL AND rp.parent_span_id != '', rp.parent_span_id, NULL), '|') OVER (PARTITION BY rp.trace_id) AS tok_p1,
      STRING_AGG(IF(rp.cross_rn = 1 AND rp.operation_name = 'generate_content' AND rp.parent_span_id IS NOT NULL AND rp.parent_span_id != '', rp.parent_span_id, NULL), '|') OVER (PARTITION BY rp.trace_id) AS gc_pids
    FROM raw_parsed AS rp
    WHERE rp.place_rn = 1 OR rp.cross_rn = 1
  ),
  raw_anc AS (
    SELECT
      ra.*,
      CASE
        WHEN ra.cross_rn != 1 THEN FALSE
        WHEN ra.span_id IS NOT NULL AND ra.span_id != '' AND (
          STRPOS(CONCAT('|', COALESCE(ra.tok_p1, ''), '|', COALESCE(ra.tok_p2, ''), '|', COALESCE(ra.tok_p3, ''), '|', COALESCE(ra.tok_p4, ''), '|', COALESCE(ra.tok_p5, ''), '|', COALESCE(ra.tok_p6, ''), '|'), CONCAT('|', ra.span_id, '|')) > 0
          OR (ra.span_name = 'call_llm' AND STRPOS(CONCAT('|', COALESCE(ra.gc_pids, ''), '|'), CONCAT('|', ra.span_id, '|')) > 0)
        ) THEN FALSE
        WHEN ra.span_kind IN ('model_call', 'embedding') OR ra.has_tok THEN TRUE
        ELSE FALSE
      END AS is_model_call
    FROM (
      SELECT ra5.*, STRING_AGG(IF(ra5.cross_rn = 1 AND ra5.span_id IS NOT NULL AND ra5.span_id != '' AND STRPOS(CONCAT('|', COALESCE(ra5.tok_p5, ''), '|'), CONCAT('|', ra5.span_id, '|')) > 0 AND ra5.parent_span_id IS NOT NULL AND ra5.parent_span_id != '', ra5.parent_span_id, NULL), '|') OVER (PARTITION BY ra5.trace_id) AS tok_p6
      FROM (
        SELECT ra4.*, STRING_AGG(IF(ra4.cross_rn = 1 AND ra4.span_id IS NOT NULL AND ra4.span_id != '' AND STRPOS(CONCAT('|', COALESCE(ra4.tok_p4, ''), '|'), CONCAT('|', ra4.span_id, '|')) > 0 AND ra4.parent_span_id IS NOT NULL AND ra4.parent_span_id != '', ra4.parent_span_id, NULL), '|') OVER (PARTITION BY ra4.trace_id) AS tok_p5
        FROM (
          SELECT ra3.*, STRING_AGG(IF(ra3.cross_rn = 1 AND ra3.span_id IS NOT NULL AND ra3.span_id != '' AND STRPOS(CONCAT('|', COALESCE(ra3.tok_p3, ''), '|'), CONCAT('|', ra3.span_id, '|')) > 0 AND ra3.parent_span_id IS NOT NULL AND ra3.parent_span_id != '', ra3.parent_span_id, NULL), '|') OVER (PARTITION BY ra3.trace_id) AS tok_p4
          FROM (
            SELECT ra2.*, STRING_AGG(IF(ra2.cross_rn = 1 AND ra2.span_id IS NOT NULL AND ra2.span_id != '' AND STRPOS(CONCAT('|', COALESCE(ra2.tok_p2, ''), '|'), CONCAT('|', ra2.span_id, '|')) > 0 AND ra2.parent_span_id IS NOT NULL AND ra2.parent_span_id != '', ra2.parent_span_id, NULL), '|') OVER (PARTITION BY ra2.trace_id) AS tok_p3
            FROM (
              SELECT ra1.*, STRING_AGG(IF(ra1.cross_rn = 1 AND ra1.span_id IS NOT NULL AND ra1.span_id != '' AND STRPOS(CONCAT('|', COALESCE(ra1.tok_p1, ''), '|'), CONCAT('|', ra1.span_id, '|')) > 0 AND ra1.parent_span_id IS NOT NULL AND ra1.parent_span_id != '', ra1.parent_span_id, NULL), '|') OVER (PARTITION BY ra1.trace_id) AS tok_p2
              FROM raw_anc1 AS ra1
              WHERE ra1.trace_first_start_time >= from_time AND ra1.trace_first_start_time < to_time
            ) AS ra2
          ) AS ra3
        ) AS ra4
      ) AS ra5
    ) AS ra
  ),
  by_trace AS (
    SELECT
      trace_id,
      ARRAY_AGG(IF(cross_rn = 1 AND span_kind = 'agent', agent_name, NULL) IGNORE NULLS ORDER BY start_time ASC, span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS root_agent_name,
      ARRAY_AGG(IF(place_rn = 1 AND events_json IS NOT NULL, STRUCT(raw_source, span_id, events_json), NULL) IGNORE NULLS) AS gi_spans_arr,
      ARRAY_AGG(IF(cross_rn = 1 AND (agent_name IS NOT NULL OR span_name = 'call_llm'), STRUCT(span_id, span_name, agent_name, input_msgs), NULL) IGNORE NULLS) AS parents_arr,
      ARRAY_AGG(IF(cross_rn = 1 AND is_model_call IS TRUE, STRUCT(span_id, parent_span_id, start_time, operation_name, agent_name, input_msgs), NULL) IGNORE NULLS) AS model_calls_arr
    FROM raw_anc
    GROUP BY trace_id
  ),
  with_gi AS (
    SELECT
      g.trace_id,
      ANY_VALUE(g.root_agent_name) AS root_agent_name,
      ANY_VALUE(g.parents_arr) AS parents_arr,
      ANY_VALUE(g.model_calls_arr) AS model_calls_arr,
      COALESCE(MAX(g.wf_gi_c), 0) + COUNTIF(
        g.is_gi
        AND g.ev_ts >= from_time
        AND g.ev_ts < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        AND STRPOS(
          CONCAT(',', COALESCE(g.wf_gi_keys, ''), ','),
          CONCAT(
            ',',
            g.raw_source,
            '|',
            CASE
              WHEN g.gi_rn = 1 THEN CONCAT('span:', g.trace_id, ':', g.span_id)
              ELSE CONCAT('span:', g.trace_id, ':', g.span_id, ':', CAST(g.gi_rn AS STRING))
            END,
            ','
          )
        ) = 0
      ) AS gi_c,
      COALESCE(MAX(IF(g.wf_ev_esc, 1, 0)), 0) = 1 AS ev_esc
    FROM (
      SELECT
        bt.trace_id,
        bt.root_agent_name,
        bt.parents_arr,
        bt.model_calls_arr,
        wf.wf_gi_c,
        wf.wf_gi_keys,
        wf.wf_ev_esc,
        gs.raw_source,
        gs.span_id,
        (JSON_VALUE(ev, '$.name') = 'guardrail.intervention') AS is_gi,
        SAFE_CAST(JSON_VALUE(ev, '$.time') AS TIMESTAMP) AS ev_ts,
        ROW_NUMBER() OVER (
          PARTITION BY bt.trace_id, gs.raw_source, gs.span_id, IF(JSON_VALUE(ev, '$.name') = 'guardrail.intervention', 0, 1)
          ORDER BY ev_idx ASC
        ) AS gi_rn
      FROM by_trace AS bt
      LEFT JOIN wf_events AS wf ON wf.trace_id = bt.trace_id
      LEFT JOIN UNNEST(bt.gi_spans_arr) AS gs
      LEFT JOIN UNNEST(JSON_QUERY_ARRAY(gs.events_json)) AS ev WITH OFFSET AS ev_idx
    ) AS g
    GROUP BY g.trace_id
  ),
  msg_level AS (
    SELECT
      wg.trace_id,
      MAX(wg.gi_c) AS gi_c,
      MAX(IF(wg.ev_esc, 1, 0)) = 1 AS ev_esc,
      COALESCE(s.agent_name, p.agent_name, wg.root_agent_name, '') AS ag_key,
      s.span_id,
      s.start_time,
      msg_idx,
      COUNTIF(part IS NOT NULL) > 0 AS has_part,
      (
        COUNTIF(part IS NOT NULL) > 0
        AND MAX(CASE WHEN JSON_VALUE(msg, '$.role') = 'user' THEN 1 ELSE 0 END) = 1
        AND COUNTIF(
          JSON_VALUE(part, '$.type') = 'tool_call_response'
          OR NULLIF(TO_JSON_STRING(JSON_QUERY(part, '$.function_response')), 'null') IS NOT NULL
          OR NULLIF(TO_JSON_STRING(JSON_QUERY(part, '$.functionResponse')), 'null') IS NOT NULL
        ) = 0
      ) AS is_real_user_msg
    FROM with_gi AS wg
    LEFT JOIN UNNEST(wg.model_calls_arr) AS s
    LEFT JOIN UNNEST(wg.parents_arr) AS p ON p.span_id = s.parent_span_id
    LEFT JOIN UNNEST(
      JSON_QUERY_ARRAY(
        COALESCE(
          s.input_msgs,
          CASE WHEN s.operation_name = 'generate_content' AND p.span_name = 'call_llm' THEN p.input_msgs END
        )
      )
    ) AS msg WITH OFFSET AS msg_idx
    LEFT JOIN UNNEST(JSON_QUERY_ARRAY(msg, '$.parts')) AS part
    GROUP BY wg.trace_id, ag_key, s.span_id, s.start_time, msg_idx
  ),
  span_level AS (
    SELECT
      trace_id,
      MAX(gi_c) AS gi_c,
      MAX(IF(ev_esc, 1, 0)) = 1 AS ev_esc,
      ag_key,
      span_id,
      start_time,
      MAX(IF(has_part, 1, 0)) = 1 AS span_has_parts,
      COUNTIF(is_real_user_msg) AS upc
    FROM msg_level
    GROUP BY trace_id, ag_key, span_id, start_time
  ),
  agent_level AS (
    SELECT
      trace_id,
      MAX(gi_c) AS gi_c,
      MAX(IF(ev_esc, 1, 0)) = 1 AS ev_esc,
      ag_key,
      GREATEST(
        0,
        COALESCE(
          ARRAY_AGG(IF(span_has_parts, upc, NULL) IGNORE NULLS ORDER BY start_time DESC, span_id DESC LIMIT 1)[SAFE_OFFSET(0)]
          - ARRAY_AGG(IF(span_has_parts, upc, NULL) IGNORE NULLS ORDER BY start_time ASC, span_id ASC LIMIT 1)[SAFE_OFFSET(0)],
          0
        )
      ) AS agent_extra
    FROM span_level
    GROUP BY trace_id, ag_key
  )
  SELECT
    trace_id,
    MAX(gi_c) AS gi_c,
    MAX(IF(ev_esc, 1, 0)) = 1 AS ev_esc,
    MAX(agent_extra) AS extra_user_turns
  FROM agent_level
  GROUP BY trace_id
);
