-- Silver Traces (`traces_base`, `silver_restricted.traces`, `silver_open.traces_open`; `ADR-034`..`ADR-048`).

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.traces_base`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
WITH base_spans AS (
  SELECT * FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.spans`(from_time, to_time)
),
span_rollup AS (
  SELECT
    trace_id, MIN(st) AS trace_start_time, MAX(end_time) AS trace_end_time, MAX(receive_time) AS last_span_arrival_time,
    COUNT(*) AS span_count, COUNT(DISTINCT app_id) AS span_app_count,
    MAX(IF(span_kind IN ('agent', 'model_call', 'embedding', 'tool_call', 'retrieval', 'memory') OR is_m, 1, 0)) = 1 AS is_turn,
    MAX(IF(is_p AND caller_app_id IS NOT NULL, 1, 0)) = 1 AS has_gw_caller,
    COUNTIF(is_m) AS model_call_count, COUNTIF(is_m AND content_missing IS TRUE) AS m_cm,
    COALESCE(SUM(duplicate_count), 0) AS dup_c,
    COUNTIF(span_status = 'ERROR' OR error_type IS NOT NULL) AS error_count, COUNTIF(http_status_code = 429) AS throttle_count,
    COUNTIF(source_class IS NOT NULL AND user_id IS NULL) AS m_uid,
    COUNTIF(source_class IS NOT NULL AND outcome_id IS NULL) AS m_oid,
    COUNTIF(source_class IS NOT NULL AND conversation_id IS NULL) AS m_cid,
    COUNT(DISTINCT outcome_id) AS distinct_outcome_count,
    COUNTIF(tr_uid IS NOT NULL AND user_id IS NOT NULL AND user_id != tr_uid) AS uid_conf,
    MAX(IF(app_failed IS TRUE, 1, 0)) = 1 AS app_failed,
    MAX(IF(is_m AND cascade_tier = 'escalation', 1, 0)) = 1 AS m_esc,
    SUM(IF(is_m, input_tokens, NULL)) AS sum_in_tok, SUM(IF(is_m, output_tokens, NULL)) AS sum_out_tok,
    SUM(IF(is_m, cache_read_tokens, NULL)) AS sum_cr_tok, SUM(IF(is_m, cache_write_tokens, NULL)) AS sum_cw_tok,
    SUM(IF(is_m, reasoning_tokens, NULL)) AS sum_rs_tok,
    MIN(IF(is_m, model_confidence, NULL)) AS min_mc, MAX(IF(is_c, model_confidence, NULL)) AS fin_mc,
    MAX(IF(is_c, end_time, NULL)) AS fin_mc_et, MAX(IF(is_c, sid, NULL)) AS fin_mc_sid,
    MAX(page_count) AS max_pages, MAX(document_bytes) AS max_doc_b,
    MAX(IF(e_rn = 1, source_class, NULL)) AS f_sc, MAX(IF(e_rn = 1, trace_state, NULL)) AS f_ts, MAX(IF(e_rn = 1, sampling_probability, NULL)) AS f_sp,
    MAX(IF(is_r, sid, NULL)) AS root_span_id, MAX(IF(is_r, span_status, NULL)) AS root_status,
    MAX(IF(is_r, source_class, NULL)) AS r_sc, MAX(IF(is_r, app_id, NULL)) AS r_app,
    MAX(CASE WHEN is_r THEN CASE WHEN app_identity_bound IS TRUE THEN 1 WHEN app_identity_bound IS FALSE THEN 0 END END) AS r_bnd,
    MAX(IF(is_r, trace_state, NULL)) AS r_ts, MAX(IF(is_r, sampling_probability, NULL)) AS r_sp,
    MAX(IF(is_r, trigger_principal, NULL)) AS r_tp, MAX(IF(is_r, agent_principal, NULL)) AS r_ap,
    SAFE.PARSE_JSON(MAX(IF(is_r AND act_chain IS NOT NULL, TO_JSON_STRING(act_chain), NULL))) AS r_act,
    MAX(IF(is_r, user_id, NULL)) AS r_uid, MAX(IF(is_r, tenant_id, NULL)) AS r_tid,
    MAX(IF(is_r, auth_method, NULL)) AS r_am, MAX(IF(is_r, auth_issuer, NULL)) AS r_ai,
    MAX(IF(is_r, auth_audience, NULL)) AS r_aa, MAX(IF(is_r, delegation_chain_hash, NULL)) AS r_dch,
    MAX(IF(is_r, conversation_id, NULL)) AS r_cid, MAX(IF(is_r, workspace_session_id, NULL)) AS r_ws,
    MAX(IF(is_r, previous_interaction_id, NULL)) AS r_prev, MAX(IF(is_r, state_mode, NULL)) AS r_sm,
    MAX(IF(is_r, runtime_lane, NULL)) AS r_rl, MAX(IF(is_r, shadow_of_trace_id, NULL)) AS r_sh,
    MAX(IF(is_r, experiment_id, NULL)) AS r_exp, MAX(IF(is_r, variant_id, NULL)) AS r_var,
    MAX(IF(is_r, case_id, NULL)) AS r_case, MAX(IF(is_r, eval_run_id, NULL)) AS r_erid,
    MAX(IF(is_r, eval_split, NULL)) AS r_espl, MAX(IF(is_r, skill_name, NULL)) AS r_skn,
    MAX(IF(is_r, skill_sha256, NULL)) AS r_sks, MAX(IF(is_r, protected_block_sha256, NULL)) AS r_pbs,
    SAFE.PARSE_JSON(MAX(IF(is_r AND mounted_skills IS NOT NULL, TO_JSON_STRING(mounted_skills), NULL))) AS r_msk,
    MAX(IF(is_r, tool_definitions_sha256, NULL)) AS r_tds, MAX(IF(is_r, page_count, NULL)) AS r_pg,
    MAX(IF(is_r, document_bytes, NULL)) AS r_db, MAX(IF(is_r, attachment_sha256, NULL)) AS r_as,
    MAX(IF(is_r, traffic_type, NULL)) AS r_tt,
    SAFE.PARSE_JSON(MAX(IF(is_r AND labels IS NOT NULL, TO_JSON_STRING(labels), NULL))) AS r_lbl,
    ARRAY_AGG(IF(is_p, user_id, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS platform_user_id,
    ARRAY_AGG(IF(is_p, tenant_id, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_tid,
    ARRAY_AGG(IF(is_p, trigger_principal, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_tp,
    ARRAY_AGG(IF(is_p, agent_principal, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_ap,
    SAFE.PARSE_JSON(ARRAY_AGG(IF(is_p AND act_chain IS NOT NULL, TO_JSON_STRING(act_chain), NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)]) AS g_act,
    ARRAY_AGG(IF(is_p, auth_method, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_am,
    ARRAY_AGG(IF(is_p, auth_issuer, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_ai,
    ARRAY_AGG(IF(is_p, auth_audience, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_aa,
    ARRAY_AGG(IF(is_p, delegation_chain_hash, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_dch,
    ARRAY_AGG(IF(is_p, conversation_id, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_cid,
    ARRAY_AGG(IF(is_p, state_mode, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_sm,
    ARRAY_AGG(IF(is_p, runtime_lane, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_rl,
    ARRAY_AGG(IF(is_p, skill_name, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_skn,
    ARRAY_AGG(IF(is_p, skill_sha256, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_sks,
    ARRAY_AGG(IF(is_p, protected_block_sha256, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_pbs,
    SAFE.PARSE_JSON(ARRAY_AGG(IF(is_p AND mounted_skills IS NOT NULL, TO_JSON_STRING(mounted_skills), NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)]) AS g_msk,
    ARRAY_AGG(IF(is_p, tool_definitions_sha256, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_tds,
    ARRAY_AGG(IF(is_p, page_count, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_pg,
    ARRAY_AGG(IF(is_p, document_bytes, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_db,
    ARRAY_AGG(IF(is_p, attachment_sha256, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_as,
    ARRAY_AGG(IF(is_p, traffic_type, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_tt,
    ARRAY_AGG(trigger_principal IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_tp,
    ARRAY_AGG(agent_principal IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ap,
    SAFE.PARSE_JSON(ARRAY_AGG(IF(act_chain IS NOT NULL, TO_JSON_STRING(act_chain), NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)]) AS e_act,
    SAFE.PARSE_JSON(ARRAY_AGG(IF(mounted_skills IS NOT NULL, TO_JSON_STRING(mounted_skills), NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)]) AS e_msk,
    ARRAY_AGG(tenant_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_tid, ARRAY_AGG(auth_method IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_am,
    ARRAY_AGG(auth_issuer IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ai, ARRAY_AGG(auth_audience IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_aa,
    ARRAY_AGG(delegation_chain_hash IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_dch, ARRAY_AGG(conversation_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_cid,
    ARRAY_AGG(outcome_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_oid, ARRAY_AGG(workspace_session_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ws,
    ARRAY_AGG(IF(is_m, previous_interaction_id, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_cp,
    ARRAY_AGG(previous_interaction_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_prev,
    ARRAY_AGG(state_mode IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_sm, ARRAY_AGG(runtime_lane IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_rl,
    ARRAY_AGG(skill_name IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_skn, ARRAY_AGG(skill_sha256 IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_sks,
    ARRAY_AGG(protected_block_sha256 IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_pbs,
    ARRAY_AGG(IF(is_m, tool_definitions_sha256, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ctds,
    ARRAY_AGG(tool_definitions_sha256 IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_tds,
    ARRAY_AGG(attachment_sha256 IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_as, ARRAY_AGG(app_failure_reason IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_fr,
    ARRAY_AGG(traffic_type IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_tt, ARRAY_AGG(trace_state IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ts,
    ARRAY_AGG(sampling_probability IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_sp,
    ARRAY_AGG(JSON_VALUE(labels, '$.workflow_name') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_wf,
    ARRAY_AGG(JSON_VALUE(labels, '$.agent_version') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_av,
    ARRAY_AGG(JSON_VALUE(labels, '$.agent_description') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ad,
    ARRAY_AGG(JSON_VALUE(labels, '$.prompt_name') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_pn,
    ARRAY_AGG(JSON_VALUE(labels, '$.prompt_version') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_pv,
    ARRAY_AGG(JSON_VALUE(labels, '$.mcp_method_name') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_mcp,
    ARRAY_AGG(JSON_VALUE(labels, '$.a2a_task_id') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_a2i,
    ARRAY_AGG(JSON_VALUE(labels, '$.a2a_task_state') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_a2s,
    ARRAY_AGG(JSON_VALUE(labels, '$.adk_invocation_id') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_adk,
    ARRAY_AGG(IF(is_p, caller_app_id, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_capp,
    ARRAY_AGG(IF(is_p AND caller_app_id IS NOT NULL, st, NULL) IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_cst
  FROM (
    SELECT sp.*, sp.start_time AS st, sp.span_id AS sid, (sp.is_model_call IS TRUE) AS is_m, (sp.source_class = 'platform') AS is_p,
      (sp.source_class IS NOT NULL AND ROW_NUMBER() OVER (PARTITION BY sp.trace_id ORDER BY IF(sp.source_class IS NOT NULL, 0, 1), sp.start_time ASC, sp.span_id ASC) = 1) AS is_r,
      (sp.is_model_call IS TRUE AND sp.model_confidence IS NOT NULL AND ROW_NUMBER() OVER (PARTITION BY sp.trace_id ORDER BY IF(sp.is_model_call IS TRUE AND sp.model_confidence IS NOT NULL, 0, 1), sp.end_time DESC, sp.span_id DESC) = 1) AS is_c,
      ROW_NUMBER() OVER (PARTITION BY sp.trace_id ORDER BY sp.start_time ASC, sp.span_id ASC) AS e_rn,
      FIRST_VALUE(IF(sp.source_class IS NOT NULL, sp.user_id, NULL)) OVER (PARTITION BY sp.trace_id ORDER BY IF(sp.source_class IS NOT NULL, 0, 1), sp.start_time ASC, sp.span_id ASC) AS tr_uid
    FROM base_spans AS sp
  ) GROUP BY trace_id
),
parent_agg AS (
  SELECT s.trace_id,
    COUNTIF(p.span_id IS NULL AND s.parent_span_id IS NOT NULL AND COALESCE(s.otel_kind, '') NOT IN ('SERVER', 'CONSUMER')) AS mp_c,
    MAX(IF(s.r_rn = 1 AND s.source_class IS NOT NULL AND p.span_id IS NULL AND s.parent_span_id IS NOT NULL, 1, 0)) = 1 AS rpm
  FROM (
    SELECT trace_id, span_id, parent_span_id, otel_kind, source_class,
      ROW_NUMBER() OVER (PARTITION BY trace_id ORDER BY IF(source_class IS NOT NULL, 0, 1), start_time ASC, span_id ASC) AS r_rn
    FROM base_spans
  ) AS s LEFT JOIN base_spans AS p ON p.trace_id = s.trace_id AND p.span_id = s.parent_span_id
  GROUP BY s.trace_id
),
tool_agg AS (
  SELECT trace_id, COUNT(*) AS tc_c, COUNTIF(writes_state IS TRUE) AS mt_c,
    COUNTIF(modifies_workspace IS TRUE) AS we_c, COUNTIF(writes_state IS NULL) AS ut_c,
    COUNTIF(pair_occ > 1) AS rt_c,
    COUNTIF(base_content_missing IS TRUE AND COALESCE(is_model_call, FALSE) IS FALSE) AS t_cm
  FROM (
    SELECT trace_id, writes_state, modifies_workspace, base_content_missing, is_model_call,
      IF(args_sha256 IS NOT NULL, ROW_NUMBER() OVER (PARTITION BY trace_id, COALESCE(tool_name, ''), args_sha256 ORDER BY start_time ASC, span_id ASC), 0) AS pair_occ
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.tool_calls_base`(from_time, to_time)
  ) GROUP BY trace_id
),
event_agg AS (
  SELECT trace_id, COUNTIF(event_type = 'guardrail.intervention') AS gi_c,
    MAX(IF(event_type = 'outcome.escalated', 1, 0)) = 1 AS ev_esc
  FROM (
    SELECT trace_id, event_type, ROW_NUMBER() OVER (PARTITION BY sc, event_id ORDER BY et ASC) AS rn FROM (
      SELECT sc, event_id, event_time AS et, event_type, trace_id FROM (
        SELECT 'system_of_record' AS sc, event_id, event_time, event_type, trace_id FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_sor`
        UNION ALL
        SELECT 'platform' AS sc, event_id, event_time, event_type, trace_id FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_platform`
        UNION ALL
        SELECT 'application' AS sc, event_id, event_time, event_type, trace_id FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_app`
      ) WHERE event_time >= from_time AND event_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND event_type IN ('guardrail.intervention', 'outcome.escalated') AND trace_id IS NOT NULL
      UNION ALL
      SELECT sc, CONCAT('span:', trace_id, ':', span_id, ':', CAST(ev_idx AS STRING)) AS event_id, SAFE_CAST(ev_time AS TIMESTAMP) AS et, 'guardrail.intervention' AS event_type, trace_id
      FROM (
        SELECT 'application' AS sc, s.trace_id, s.span_id, ev.time AS ev_time, ev_idx
        FROM (
          SELECT trace_id, span_id, events, ROW_NUMBER() OVER (PARTITION BY trace_id, span_id ORDER BY receive_time DESC, end_time DESC) AS srn
          FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans`
          WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        ) AS s CROSS JOIN UNNEST(s.events) AS ev WITH OFFSET AS ev_idx
        WHERE s.srn = 1 AND ev.name = 'guardrail.intervention'
        UNION ALL
        SELECT 'platform' AS sc, s.trace_id, s.span_id, ev.time AS ev_time, ev_idx
        FROM (
          SELECT trace_id, span_id, events, ROW_NUMBER() OVER (PARTITION BY trace_id, span_id ORDER BY receive_time DESC, end_time DESC) AS srn
          FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}`
          WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        ) AS s CROSS JOIN UNNEST(s.events) AS ev WITH OFFSET AS ev_idx
        WHERE s.srn = 1 AND ev.name = 'guardrail.intervention'
      ) WHERE SAFE_CAST(ev_time AS TIMESTAMP) >= from_time AND SAFE_CAST(ev_time AS TIMESTAMP) < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    )
  ) WHERE rn = 1 GROUP BY trace_id
),
raw_inline_inputs AS (
  SELECT trace_id, span_id,
    COALESCE(
      SAFE.PARSE_JSON(JSON_VALUE(attributes, '$."gen_ai.input.messages"')),
      SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(attributes, '$."gen_ai.input.messages"')), 'null')),
      SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(COALESCE(SAFE.PARSE_JSON(NULLIF(JSON_VALUE(attributes, '$."gcp.vertex.agent.llm_request"'), '{}')), SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(attributes, '$."gcp.vertex.agent.llm_request"')), 'null'))), '$.contents')), 'null'))
    ) AS input_msgs,
    ROW_NUMBER() OVER (PARTITION BY trace_id, span_id ORDER BY src_prio ASC, receive_time DESC, end_time DESC) AS rn
  FROM (
    SELECT 1 AS src_prio, trace_id, span_id, receive_time, end_time, attributes FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans`
    WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    UNION ALL
    SELECT 0 AS src_prio, trace_id, span_id, receive_time, end_time, attributes FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}`
    WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
  )
),
user_turn_extra AS (
  SELECT trace_id, MAX(agent_extra) AS extra_user_turns FROM (
    SELECT trace_id, ag_key,
      GREATEST(0, COALESCE(ARRAY_AGG(upc ORDER BY start_time DESC, span_id DESC LIMIT 1)[SAFE_OFFSET(0)] - ARRAY_AGG(upc ORDER BY start_time ASC, span_id ASC LIMIT 1)[SAFE_OFFSET(0)], 0)) AS agent_extra
    FROM (
      SELECT trace_id, ag_key, span_id, start_time, COUNTIF(is_real_user_msg) AS upc FROM (
        SELECT s.trace_id, COALESCE(s.agent_name, p.agent_name, s.root_agent_name, '') AS ag_key, s.span_id, s.start_time, msg_idx,
          (MAX(CASE WHEN JSON_VALUE(msg, '$.role') = 'user' THEN 1 ELSE 0 END) = 1 AND COUNTIF(
            JSON_VALUE(part, '$.type') = 'tool_call_response'
            OR NULLIF(TO_JSON_STRING(JSON_QUERY(part, '$.function_response')), 'null') IS NOT NULL
            OR NULLIF(TO_JSON_STRING(JSON_QUERY(part, '$.functionResponse')), 'null') IS NOT NULL
          ) = 0) AS is_real_user_msg
        FROM (
          SELECT sp.*,
            FIRST_VALUE(CASE WHEN sp.span_kind = 'agent' THEN sp.agent_name END) OVER (
              PARTITION BY sp.trace_id
              ORDER BY CASE WHEN sp.span_kind = 'agent' AND sp.agent_name IS NOT NULL THEN 0 ELSE 1 END, sp.start_time ASC, sp.span_id ASC
            ) AS root_agent_name
          FROM base_spans AS sp
        ) AS s
        LEFT JOIN base_spans AS p ON p.trace_id = s.trace_id AND p.span_id = s.parent_span_id
        LEFT JOIN raw_inline_inputs AS ri ON ri.trace_id = s.trace_id AND ri.span_id = s.span_id AND ri.rn = 1
        LEFT JOIN raw_inline_inputs AS pi ON pi.trace_id = s.trace_id AND pi.span_id = p.span_id AND pi.rn = 1
        CROSS JOIN UNNEST(JSON_QUERY_ARRAY(COALESCE(ri.input_msgs, CASE WHEN s.operation_name = 'generate_content' AND p.span_name = 'call_llm' THEN pi.input_msgs END))) AS msg WITH OFFSET AS msg_idx
        CROSS JOIN UNNEST(JSON_QUERY_ARRAY(msg, '$.parts')) AS part
        WHERE s.is_model_call IS TRUE
        GROUP BY s.trace_id, ag_key, s.span_id, s.start_time, msg_idx
      ) GROUP BY trace_id, ag_key, span_id, start_time
    ) GROUP BY trace_id, ag_key
  ) GROUP BY trace_id
),
skill_checks AS (
  SELECT s.trace_id, MAX(CASE WHEN sr.skill_name IS NULL THEN 1 ELSE 0 END) = 1 AS unregistered_skill
  FROM (
    SELECT sp.trace_id, sp.skill_name, sp.skill_version, sp.start_time,
      MAX(CASE WHEN sp.source_class = 'platform' AND sp.caller_app_id IS NOT NULL THEN 1 ELSE 0 END) OVER (PARTITION BY sp.trace_id) = 1 AS has_gw_caller,
      FIRST_VALUE(CASE WHEN sp.source_class = 'platform' AND sp.caller_app_id IS NOT NULL THEN sp.caller_app_id END) OVER (
        PARTITION BY sp.trace_id
        ORDER BY CASE WHEN sp.source_class = 'platform' AND sp.caller_app_id IS NOT NULL THEN 0 ELSE 1 END, sp.start_time ASC, sp.span_id ASC
      ) AS g_capp,
      FIRST_VALUE(CASE WHEN sp.source_class = 'platform' AND sp.caller_app_id IS NOT NULL THEN sp.start_time END) OVER (
        PARTITION BY sp.trace_id
        ORDER BY CASE WHEN sp.source_class = 'platform' AND sp.caller_app_id IS NOT NULL THEN 0 ELSE 1 END, sp.start_time ASC, sp.span_id ASC
      ) AS g_cst,
      FIRST_VALUE(CASE WHEN sp.source_class IS NOT NULL THEN sp.app_id END) OVER (PARTITION BY sp.trace_id ORDER BY CASE WHEN sp.source_class IS NOT NULL THEN 0 ELSE 1 END, sp.start_time ASC, sp.span_id ASC) AS r_app
    FROM base_spans AS sp
  ) AS s
  LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS gc_r
    ON s.has_gw_caller AND s.g_capp IS NOT NULL AND gc_r.app_id = s.g_capp
    AND gc_r.effective_from <= s.g_cst AND (gc_r.effective_to IS NULL OR s.g_cst < gc_r.effective_to)
  LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.skill_registry` AS sr
    ON sr.app_id = CASE WHEN s.has_gw_caller THEN gc_r.app_id ELSE s.r_app END
    AND sr.skill_name = s.skill_name AND (s.skill_version IS NULL OR sr.skill_version = s.skill_version)
    AND sr.effective_from <= s.start_time AND (sr.effective_to IS NULL OR s.start_time < sr.effective_to)
  WHERE s.skill_name IS NOT NULL GROUP BY s.trace_id
),
core AS (
  SELECT sa.*,
    CASE WHEN sa.has_gw_caller THEN gc_r.app_id ELSE sa.r_app END AS trace_owner_app_id,
    CASE
      WHEN (CASE WHEN sa.has_gw_caller THEN gc_r.app_id ELSE sa.r_app END) IS NULL THEN CAST(NULL AS BOOL)
      WHEN sa.has_gw_caller THEN (COALESCE('${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}' IN UNNEST(JSON_VALUE_ARRAY(gc_r.allowed_landing_places)), FALSE) OR (sa.r_app = gc_r.app_id AND sa.r_bnd = 1))
      WHEN sa.r_bnd = 1 THEN TRUE WHEN sa.r_bnd = 0 THEN FALSE ELSE CAST(NULL AS BOOL)
    END AS app_identity_bound,
    r.owner, r.business_unit, r.environment, r.risk_tier, r.barrier_domain,
    COALESCE(
      REGEXP_EXTRACT(sa.r_ts, r'(?:^|,|;|ot=)th:([0-9a-fA-F]{1,14})(?:$|[,;])'),
      REGEXP_EXTRACT(sa.f_ts, r'(?:^|,|;|ot=)th:([0-9a-fA-F]{1,14})(?:$|[,;])'),
      REGEXP_EXTRACT(sa.e_ts, r'(?:^|,|;|ot=)th:([0-9a-fA-F]{1,14})(?:$|[,;])')
    ) AS th_hex,
    JSON_STRIP_NULLS(JSON_OBJECT(
      'apphub_service_id', JSON_VALUE(sa.r_lbl, '$.apphub_service_id'), 'apphub_workload_id', JSON_VALUE(sa.r_lbl, '$.apphub_workload_id'),
      'service_name', JSON_VALUE(sa.r_lbl, '$.service_name'), 'telemetry_environment', JSON_VALUE(sa.r_lbl, '$.telemetry_environment'),
      'cloud_provider', JSON_VALUE(sa.r_lbl, '$.cloud_provider'), 'cloud_region', JSON_VALUE(sa.r_lbl, '$.cloud_region'),
      'cloud_account_id', JSON_VALUE(sa.r_lbl, '$.cloud_account_id'),
      'agent_version', COALESCE(JSON_VALUE(sa.r_lbl, '$.agent_version'), sa.e_av), 'agent_description', COALESCE(JSON_VALUE(sa.r_lbl, '$.agent_description'), sa.e_ad),
      'workflow_name', COALESCE(JSON_VALUE(sa.r_lbl, '$.workflow_name'), sa.e_wf), 'prompt_name', COALESCE(JSON_VALUE(sa.r_lbl, '$.prompt_name'), sa.e_pn),
      'prompt_version', COALESCE(JSON_VALUE(sa.r_lbl, '$.prompt_version'), sa.e_pv), 'mcp_method_name', COALESCE(JSON_VALUE(sa.r_lbl, '$.mcp_method_name'), sa.e_mcp),
      'a2a_task_id', COALESCE(JSON_VALUE(sa.r_lbl, '$.a2a_task_id'), sa.e_a2i), 'a2a_task_state', COALESCE(JSON_VALUE(sa.r_lbl, '$.a2a_task_state'), sa.e_a2s),
      'adk_invocation_id', COALESCE(JSON_VALUE(sa.r_lbl, '$.adk_invocation_id'), sa.e_adk),
      'telemetry_sdk_name', JSON_VALUE(sa.r_lbl, '$.telemetry_sdk_name'), 'telemetry_sdk_version', JSON_VALUE(sa.r_lbl, '$.telemetry_sdk_version')
    )) AS rl,
    JSON_STRIP_NULLS(JSON_OBJECT(
      'apphub_service_id', JSON_VALUE(sa.r_lbl, '$.apphub_service_id'), 'apphub_workload_id', JSON_VALUE(sa.r_lbl, '$.apphub_workload_id'),
      'service_name', JSON_VALUE(sa.r_lbl, '$.service_name'), 'telemetry_environment', JSON_VALUE(sa.r_lbl, '$.telemetry_environment'),
      'cloud_provider', JSON_VALUE(sa.r_lbl, '$.cloud_provider'), 'cloud_region', JSON_VALUE(sa.r_lbl, '$.cloud_region'),
      'agent_version', COALESCE(JSON_VALUE(sa.r_lbl, '$.agent_version'), sa.e_av), 'agent_description', COALESCE(JSON_VALUE(sa.r_lbl, '$.agent_description'), sa.e_ad),
      'workflow_name', COALESCE(JSON_VALUE(sa.r_lbl, '$.workflow_name'), sa.e_wf), 'prompt_name', COALESCE(JSON_VALUE(sa.r_lbl, '$.prompt_name'), sa.e_pn),
      'prompt_version', COALESCE(JSON_VALUE(sa.r_lbl, '$.prompt_version'), sa.e_pv), 'mcp_method_name', COALESCE(JSON_VALUE(sa.r_lbl, '$.mcp_method_name'), sa.e_mcp),
      'a2a_task_id', COALESCE(JSON_VALUE(sa.r_lbl, '$.a2a_task_id'), sa.e_a2i), 'a2a_task_state', COALESCE(JSON_VALUE(sa.r_lbl, '$.a2a_task_state'), sa.e_a2s),
      'adk_invocation_id', COALESCE(JSON_VALUE(sa.r_lbl, '$.adk_invocation_id'), sa.e_adk),
      'telemetry_sdk_name', JSON_VALUE(sa.r_lbl, '$.telemetry_sdk_name'), 'telemetry_sdk_version', JSON_VALUE(sa.r_lbl, '$.telemetry_sdk_version')
    )) AS ol,
    ROW_NUMBER() OVER (PARTITION BY sa.trace_id ORDER BY gc_r.effective_from DESC, r.effective_from DESC) AS oreg_rn
  FROM span_rollup AS sa
  LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS gc_r
    ON sa.has_gw_caller AND sa.g_capp IS NOT NULL AND gc_r.app_id = sa.g_capp
    AND gc_r.effective_from <= sa.g_cst AND (gc_r.effective_to IS NULL OR sa.g_cst < gc_r.effective_to)
  LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
    ON r.app_id = CASE WHEN sa.has_gw_caller THEN gc_r.app_id ELSE sa.r_app END
    AND r.effective_from <= sa.trace_start_time AND (r.effective_to IS NULL OR sa.trace_start_time < r.effective_to)
)
SELECT
  c.trace_id, c.trace_owner_app_id, c.app_identity_bound, (c.trace_owner_app_id IS NULL) AS unregistered_app,
  c.owner, c.business_unit, c.environment, c.risk_tier, c.barrier_domain,
  IF(c.span_app_count = 0 AND c.trace_owner_app_id IS NOT NULL, 1, c.span_app_count) AS participating_app_count,
  c.root_span_id, (c.root_span_id IS NOT NULL) AS has_root_span, c.is_turn, c.root_status, c.trace_start_time,
  IF(c.f_sc IS NULL, 'runtime', 'application') AS trace_start_writer,
  c.trace_end_time, c.last_span_arrival_time, COALESCE(pa.mp_c, 0) AS missing_parent_count,
  IF(c.root_span_id IS NULL, CAST(NULL AS BOOL), COALESCE(pa.rpm, FALSE)) AS entry_parent_outside,
  (TIMESTAMP_DIFF(c.trace_end_time, c.trace_start_time, MILLISECOND) > ${READ_MARGIN_MINUTES} * 60 * 1000) AS exceeds_read_margin,
  COALESCE(IF(c.th_hex IS NOT NULL, 1.0 - (CAST(SAFE_CAST(CONCAT('0x', RPAD(c.th_hex, 14, '0')) AS INT64) AS FLOAT64) / 72057594037927936.0), NULL), c.r_sp, c.f_sp, c.e_sp) AS sampling_probability,
  COALESCE(c.g_tp, c.r_tp, c.e_tp) AS trigger_principal, COALESCE(c.g_ap, c.r_ap, c.e_ap) AS agent_principal, COALESCE(c.g_act, c.r_act, c.e_act) AS act_chain,
  c.r_uid AS user_id, c.platform_user_id,
  IF(c.r_uid IS NULL, CAST(NULL AS STRING), c.r_sc) AS user_id_source,
  COALESCE(c.uid_conf, 0) AS user_id_conflict_count, COALESCE(c.g_tid, c.r_tid, c.e_tid) AS tenant_id,
  CASE WHEN c.r_uid IS NULL THEN CAST(NULL AS BOOL) WHEN c.r_sc = 'platform' THEN TRUE ELSE FALSE END AS identity_attested,
  COALESCE(c.g_am, c.r_am, c.e_am) AS auth_method, COALESCE(c.g_ai, c.r_ai, c.e_ai) AS issuer,
  COALESCE(c.g_aa, c.r_aa, c.e_aa) AS aud, COALESCE(c.g_dch, c.r_dch, c.e_dch) AS delegation_chain_hash,
  COALESCE(c.g_cid, c.r_cid, c.e_cid) AS conversation_id, c.e_oid AS outcome_id, (c.distinct_outcome_count > 1) AS multiple_outcome_ids,
  COALESCE(c.r_ws, c.e_ws) AS workspace_session_id, COALESCE(c.r_prev, c.e_cp, c.e_prev) AS previous_interaction_id,
  COALESCE(c.g_sm, c.r_sm, c.e_sm) AS state_mode, COALESCE(c.g_rl, c.r_rl, c.e_rl) AS runtime_lane,
  c.r_sh AS shadow_of_trace_id, c.r_exp AS experiment_id, c.r_var AS variant_id, c.r_case AS case_id, c.r_erid AS eval_run_id, c.r_espl AS eval_split,
  COALESCE(c.g_skn, c.r_skn, c.e_skn) AS skill_name, COALESCE(c.g_sks, c.r_sks, c.e_sks) AS skill_sha256,
  COALESCE(c.g_pbs, c.r_pbs, c.e_pbs) AS protected_block_sha256, COALESCE(c.g_msk, c.r_msk, c.e_msk) AS mounted_skills,
  COALESCE(sc.unregistered_skill, FALSE) AS unregistered_skill, COALESCE(c.g_tds, c.r_tds, c.e_ctds, c.e_tds) AS tool_definitions_sha256,
  COALESCE(c.g_pg, c.r_pg, c.max_pages) AS page_count, COALESCE(c.g_db, c.r_db, c.max_doc_b) AS document_bytes, COALESCE(c.g_as, c.r_as, c.e_as) AS attachment_sha256,
  c.fin_mc AS final_model_confidence, c.min_mc AS min_model_confidence, c.fin_mc_et AS final_confidence_end_time, c.fin_mc_sid AS final_confidence_span_id,
  (c.m_esc OR COALESCE(ea.ev_esc, FALSE)) AS escalated, c.app_failed, c.e_fr AS app_failure_reason,
  COALESCE(c.g_tt, c.r_tt, c.e_tt) AS traffic_type, c.span_count, c.model_call_count,
  COALESCE(ta.tc_c, 0) AS tool_call_count, COALESCE(ta.mt_c, 0) AS mutating_tool_call_count,
  COALESCE(ta.we_c, 0) AS workspace_edit_count, COALESCE(ta.ut_c, 0) AS unregistered_tool_count,
  COALESCE(ea.gi_c, 0) AS guardrail_intervention_count,
  c.m_cm + COALESCE(ta.t_cm, 0) AS content_missing_call_count,
  IF(c.is_turn IS FALSE, 0, 1 + COALESCE(ute.extra_user_turns, 0)) AS user_turn_count,
  c.dup_c AS duplicate_span_count, COALESCE(ta.rt_c, 0) AS repeated_tool_call_count,
  c.error_count, c.throttle_count, c.m_uid AS missing_user_id_span_count, c.m_oid AS missing_outcome_id_span_count, c.m_cid AS missing_conversation_id_span_count,
  IF(c.model_call_count = 0, CAST(NULL AS INT64), c.sum_in_tok) AS input_tokens,
  IF(c.model_call_count = 0, CAST(NULL AS INT64), c.sum_out_tok) AS output_tokens,
  IF(c.model_call_count = 0, CAST(NULL AS INT64), c.sum_cr_tok) AS cache_read_input_tokens,
  IF(c.model_call_count = 0, CAST(NULL AS INT64), c.sum_cw_tok) AS cache_write_input_tokens,
  IF(c.model_call_count = 0, CAST(NULL AS INT64), c.sum_rs_tok) AS reasoning_output_tokens,
  IF(TO_JSON_STRING(c.rl) = '{}', CAST(NULL AS JSON), c.rl) AS restricted_labels,
  IF(TO_JSON_STRING(c.ol) = '{}', CAST(NULL AS JSON), c.ol) AS open_labels
FROM core AS c
LEFT JOIN parent_agg AS pa ON pa.trace_id = c.trace_id
LEFT JOIN tool_agg AS ta ON ta.trace_id = c.trace_id
LEFT JOIN event_agg AS ea ON ea.trace_id = c.trace_id
LEFT JOIN skill_checks AS sc ON sc.trace_id = c.trace_id
LEFT JOIN user_turn_extra AS ute ON ute.trace_id = c.trace_id
WHERE c.oreg_rn = 1
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.traces`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
SELECT
  t.trace_id, t.trace_owner_app_id, t.app_identity_bound, t.unregistered_app, t.owner, t.business_unit,
  t.environment, t.risk_tier, t.barrier_domain, t.participating_app_count, t.root_span_id, t.has_root_span,
  t.is_turn, t.root_status, t.trace_start_time, t.trace_start_writer, t.trace_end_time, t.last_span_arrival_time,
  t.missing_parent_count, t.entry_parent_outside, t.exceeds_read_margin, t.sampling_probability,
  t.trigger_principal, t.agent_principal, t.act_chain, t.user_id, t.platform_user_id, t.user_id_source,
  t.user_id_conflict_count, t.tenant_id, t.identity_attested, t.auth_method, t.issuer, t.aud,
  t.delegation_chain_hash, t.conversation_id, t.outcome_id, t.multiple_outcome_ids, t.workspace_session_id,
  t.previous_interaction_id, t.state_mode, t.runtime_lane, t.shadow_of_trace_id, t.experiment_id, t.variant_id,
  t.case_id, t.eval_run_id, t.eval_split, t.skill_name, t.skill_sha256, t.protected_block_sha256,
  t.mounted_skills, t.unregistered_skill, t.tool_definitions_sha256, t.page_count, t.document_bytes,
  t.attachment_sha256, t.final_model_confidence, t.escalated, t.app_failed, t.app_failure_reason,
  t.traffic_type, t.span_count, t.model_call_count, t.tool_call_count, t.mutating_tool_call_count,
  t.workspace_edit_count, t.unregistered_tool_count, t.guardrail_intervention_count, t.content_missing_call_count,
  t.user_turn_count, t.duplicate_span_count, t.repeated_tool_call_count, t.error_count, t.throttle_count,
  t.missing_user_id_span_count, t.missing_outcome_id_span_count, t.missing_conversation_id_span_count,
  t.input_tokens, t.output_tokens, t.cache_read_input_tokens, t.cache_write_input_tokens,
  t.reasoning_output_tokens, t.restricted_labels AS labels
FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.traces_base`(from_time, to_time) AS t
WHERE EXISTS (
  SELECT 1 FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.unit_entitlements` AS ue
  WHERE LOWER(ue.principal_email) = LOWER(SESSION_USER())
    AND ue.business_unit = COALESCE(t.business_unit, '__unowned__')
    AND (ue.tenant_id = '*' OR ue.tenant_id = t.tenant_id)
)
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_OPEN_DATASET}.traces_open`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
SELECT
  trace_id, trace_owner_app_id, app_identity_bound, unregistered_app, owner, business_unit, environment,
  risk_tier, barrier_domain, participating_app_count, root_span_id, has_root_span, is_turn, root_status,
  trace_start_time, trace_start_writer, trace_end_time, last_span_arrival_time, missing_parent_count,
  entry_parent_outside, exceeds_read_margin, sampling_probability, trigger_principal, agent_principal,
  user_id_source, user_id_conflict_count, identity_attested, auth_method, issuer, aud, delegation_chain_hash,
  conversation_id, outcome_id, multiple_outcome_ids, workspace_session_id, previous_interaction_id, state_mode,
  runtime_lane, shadow_of_trace_id, experiment_id, variant_id, case_id, eval_run_id, eval_split, skill_name,
  skill_sha256, protected_block_sha256, mounted_skills, unregistered_skill, tool_definitions_sha256, page_count,
  document_bytes, final_model_confidence, escalated, app_failed, traffic_type, span_count, model_call_count,
  tool_call_count, mutating_tool_call_count, workspace_edit_count, unregistered_tool_count,
  guardrail_intervention_count, content_missing_call_count, user_turn_count, duplicate_span_count,
  repeated_tool_call_count, error_count, throttle_count, missing_user_id_span_count,
  missing_outcome_id_span_count, missing_conversation_id_span_count, input_tokens, output_tokens,
  cache_read_input_tokens, cache_write_input_tokens, reasoning_output_tokens, open_labels AS labels
FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.traces_base`(from_time, to_time)
);
