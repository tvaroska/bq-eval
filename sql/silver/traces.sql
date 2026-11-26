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
    MAX(CASE WHEN span_kind IN ('agent', 'model_call', 'embedding', 'tool_call', 'retrieval', 'memory') OR is_m THEN 1 ELSE 0 END) = 1 AS is_turn,
    MAX(CASE WHEN is_p AND caller_app_id IS NOT NULL THEN 1 ELSE 0 END) = 1 AS has_gw_caller,
    COUNTIF(is_m) AS model_call_count, COUNTIF(is_m AND content_missing IS TRUE) AS model_content_missing_count,
    COALESCE(SUM(duplicate_count), 0) AS duplicate_span_count,
    COUNTIF(span_status = 'ERROR' OR error_type IS NOT NULL) AS error_count, COUNTIF(http_status_code = 429) AS throttle_count,
    COUNTIF(source_class IS NOT NULL AND user_id IS NULL) AS missing_user_id_span_count,
    COUNTIF(source_class IS NOT NULL AND outcome_id IS NULL) AS missing_outcome_id_span_count,
    COUNTIF(source_class IS NOT NULL AND conversation_id IS NULL) AS missing_conversation_id_span_count,
    COUNT(DISTINCT outcome_id) AS distinct_outcome_count,
    COUNTIF(tr_uid IS NOT NULL AND user_id IS NOT NULL AND user_id != tr_uid) AS user_id_conflict_count,
    MAX(CASE WHEN app_failed IS TRUE THEN 1 ELSE 0 END) = 1 AS app_failed,
    MAX(CASE WHEN is_m AND cascade_tier = 'escalation' THEN 1 ELSE 0 END) = 1 AS model_escalated,
    SUM(CASE WHEN is_m THEN input_tokens END) AS sum_in_tok, SUM(CASE WHEN is_m THEN output_tokens END) AS sum_out_tok,
    SUM(CASE WHEN is_m THEN cache_read_tokens END) AS sum_cr_tok, SUM(CASE WHEN is_m THEN cache_write_tokens END) AS sum_cw_tok,
    SUM(CASE WHEN is_m THEN reasoning_tokens END) AS sum_rs_tok,
    MIN(CASE WHEN is_m THEN model_confidence END) AS min_model_confidence, MAX(CASE WHEN is_c THEN model_confidence END) AS final_model_confidence,
    MAX(CASE WHEN is_c THEN end_time END) AS final_confidence_end_time, MAX(CASE WHEN is_c THEN sid END) AS final_confidence_span_id,
    MAX(page_count) AS max_pages, MAX(document_bytes) AS max_doc_b,
    MAX(CASE WHEN e_rn = 1 THEN source_class END) AS f_sc, MAX(CASE WHEN e_rn = 1 THEN trace_state END) AS f_ts, MAX(CASE WHEN e_rn = 1 THEN sampling_probability END) AS f_sp,
    MAX(CASE WHEN is_r THEN sid END) AS root_span_id, MAX(CASE WHEN is_r THEN span_status END) AS root_status,
    MAX(CASE WHEN is_r THEN source_class END) AS r_sc, MAX(CASE WHEN is_r THEN app_id END) AS r_app,
    MAX(CASE WHEN is_r THEN CASE WHEN app_identity_bound IS TRUE THEN 1 WHEN app_identity_bound IS FALSE THEN 0 END END) AS r_bnd,
    MAX(CASE WHEN is_r THEN trace_state END) AS r_ts, MAX(CASE WHEN is_r THEN sampling_probability END) AS r_sp,
    MAX(CASE WHEN is_r THEN trigger_principal END) AS r_tp, MAX(CASE WHEN is_r THEN agent_principal END) AS r_ap,
    SAFE.PARSE_JSON(MAX(CASE WHEN is_r AND act_chain IS NOT NULL THEN TO_JSON_STRING(act_chain) END)) AS r_act,
    MAX(CASE WHEN is_r THEN user_id END) AS r_uid, MAX(CASE WHEN is_r THEN tenant_id END) AS r_tid,
    MAX(CASE WHEN is_r THEN auth_method END) AS r_am, MAX(CASE WHEN is_r THEN auth_issuer END) AS r_ai,
    MAX(CASE WHEN is_r THEN auth_audience END) AS r_aa, MAX(CASE WHEN is_r THEN delegation_chain_hash END) AS r_dch,
    MAX(CASE WHEN is_r THEN conversation_id END) AS r_cid, MAX(CASE WHEN is_r THEN workspace_session_id END) AS r_ws,
    MAX(CASE WHEN is_r THEN previous_interaction_id END) AS r_prev, MAX(CASE WHEN is_r THEN state_mode END) AS r_sm,
    MAX(CASE WHEN is_r THEN runtime_lane END) AS r_rl, MAX(CASE WHEN is_r THEN shadow_of_trace_id END) AS r_sh,
    MAX(CASE WHEN is_r THEN experiment_id END) AS r_exp, MAX(CASE WHEN is_r THEN variant_id END) AS r_var,
    MAX(CASE WHEN is_r THEN case_id END) AS r_case, MAX(CASE WHEN is_r THEN eval_run_id END) AS r_erid,
    MAX(CASE WHEN is_r THEN eval_split END) AS r_espl, MAX(CASE WHEN is_r THEN skill_name END) AS r_skn,
    MAX(CASE WHEN is_r THEN skill_sha256 END) AS r_sks, MAX(CASE WHEN is_r THEN protected_block_sha256 END) AS r_pbs,
    SAFE.PARSE_JSON(MAX(CASE WHEN is_r AND mounted_skills IS NOT NULL THEN TO_JSON_STRING(mounted_skills) END)) AS r_msk,
    MAX(CASE WHEN is_r THEN tool_definitions_sha256 END) AS r_tds, MAX(CASE WHEN is_r THEN page_count END) AS r_pg,
    MAX(CASE WHEN is_r THEN document_bytes END) AS r_db, MAX(CASE WHEN is_r THEN attachment_sha256 END) AS r_as,
    MAX(CASE WHEN is_r THEN traffic_type END) AS r_tt,
    SAFE.PARSE_JSON(MAX(CASE WHEN is_r AND labels IS NOT NULL THEN TO_JSON_STRING(labels) END)) AS r_lbl,
    ARRAY_AGG(CASE WHEN is_p THEN user_id END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS platform_user_id,
    ARRAY_AGG(CASE WHEN is_p THEN tenant_id END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_tid,
    ARRAY_AGG(CASE WHEN is_p THEN trigger_principal END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_tp,
    ARRAY_AGG(CASE WHEN is_p THEN agent_principal END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_ap,
    SAFE.PARSE_JSON(ARRAY_AGG(CASE WHEN is_p AND act_chain IS NOT NULL THEN TO_JSON_STRING(act_chain) END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)]) AS g_act,
    ARRAY_AGG(CASE WHEN is_p THEN auth_method END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_am,
    ARRAY_AGG(CASE WHEN is_p THEN auth_issuer END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_ai,
    ARRAY_AGG(CASE WHEN is_p THEN auth_audience END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_aa,
    ARRAY_AGG(CASE WHEN is_p THEN delegation_chain_hash END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_dch,
    ARRAY_AGG(CASE WHEN is_p THEN conversation_id END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_cid,
    ARRAY_AGG(CASE WHEN is_p THEN state_mode END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_sm,
    ARRAY_AGG(CASE WHEN is_p THEN runtime_lane END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_rl,
    ARRAY_AGG(CASE WHEN is_p THEN skill_name END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_skn,
    ARRAY_AGG(CASE WHEN is_p THEN skill_sha256 END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_sks,
    ARRAY_AGG(CASE WHEN is_p THEN protected_block_sha256 END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_pbs,
    SAFE.PARSE_JSON(ARRAY_AGG(CASE WHEN is_p AND mounted_skills IS NOT NULL THEN TO_JSON_STRING(mounted_skills) END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)]) AS g_msk,
    ARRAY_AGG(CASE WHEN is_p THEN tool_definitions_sha256 END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_tds,
    ARRAY_AGG(CASE WHEN is_p THEN page_count END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_pg,
    ARRAY_AGG(CASE WHEN is_p THEN document_bytes END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_db,
    ARRAY_AGG(CASE WHEN is_p THEN attachment_sha256 END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_as,
    ARRAY_AGG(CASE WHEN is_p THEN traffic_type END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS g_tt,
    ARRAY_AGG(trigger_principal IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_tp,
    ARRAY_AGG(agent_principal IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ap,
    SAFE.PARSE_JSON(ARRAY_AGG(CASE WHEN act_chain IS NOT NULL THEN TO_JSON_STRING(act_chain) END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)]) AS e_act,
    SAFE.PARSE_JSON(ARRAY_AGG(CASE WHEN mounted_skills IS NOT NULL THEN TO_JSON_STRING(mounted_skills) END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)]) AS e_msk,
    ARRAY_AGG(tenant_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_tid, ARRAY_AGG(auth_method IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_am,
    ARRAY_AGG(auth_issuer IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ai, ARRAY_AGG(auth_audience IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_aa,
    ARRAY_AGG(delegation_chain_hash IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_dch, ARRAY_AGG(conversation_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_cid,
    ARRAY_AGG(outcome_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_oid, ARRAY_AGG(workspace_session_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ws,
    ARRAY_AGG(CASE WHEN is_m THEN previous_interaction_id END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_cp,
    ARRAY_AGG(previous_interaction_id IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_prev,
    ARRAY_AGG(state_mode IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_sm, ARRAY_AGG(runtime_lane IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_rl,
    ARRAY_AGG(skill_name IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_skn, ARRAY_AGG(skill_sha256 IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_sks,
    ARRAY_AGG(protected_block_sha256 IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_pbs,
    ARRAY_AGG(CASE WHEN is_m THEN tool_definitions_sha256 END IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_ctds,
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
    ARRAY_AGG(JSON_VALUE(labels, '$.adk_invocation_id') IGNORE NULLS ORDER BY st, sid LIMIT 1)[SAFE_OFFSET(0)] AS e_adk
  FROM (
    SELECT sp.*, sp.start_time AS st, sp.span_id AS sid, (sp.is_model_call IS TRUE) AS is_m, (sp.source_class = 'platform') AS is_p,
      (sp.source_class IS NOT NULL AND ROW_NUMBER() OVER (PARTITION BY sp.trace_id ORDER BY CASE WHEN sp.source_class IS NOT NULL THEN 0 ELSE 1 END, sp.start_time ASC, sp.span_id ASC) = 1) AS is_r,
      (sp.is_model_call IS TRUE AND sp.model_confidence IS NOT NULL AND ROW_NUMBER() OVER (PARTITION BY sp.trace_id ORDER BY CASE WHEN sp.is_model_call IS TRUE AND sp.model_confidence IS NOT NULL THEN 0 ELSE 1 END, sp.end_time DESC, sp.span_id DESC) = 1) AS is_c,
      ROW_NUMBER() OVER (PARTITION BY sp.trace_id ORDER BY sp.start_time ASC, sp.span_id ASC) AS e_rn,
      FIRST_VALUE(CASE WHEN sp.source_class IS NOT NULL THEN sp.user_id END) OVER (PARTITION BY sp.trace_id ORDER BY CASE WHEN sp.source_class IS NOT NULL THEN 0 ELSE 1 END, sp.start_time ASC, sp.span_id ASC) AS tr_uid
    FROM base_spans AS sp
  ) GROUP BY trace_id
),
gateway_caller AS (
  SELECT trace_id, resolved_caller_app_id, caller_identity_bound FROM (
    SELECT s.trace_id, r.app_id AS resolved_caller_app_id,
      COALESCE('${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}' IN UNNEST(JSON_VALUE_ARRAY(r.allowed_landing_places)), FALSE) AS caller_identity_bound,
      ROW_NUMBER() OVER (PARTITION BY s.trace_id ORDER BY s.start_time ASC, s.span_id ASC, r.effective_from DESC) AS rn
    FROM base_spans AS s
    JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
      ON r.app_id = s.caller_app_id AND r.effective_from <= s.start_time AND (r.effective_to IS NULL OR s.start_time < r.effective_to)
    WHERE s.source_class = 'platform' AND s.caller_app_id IS NOT NULL
  ) WHERE rn = 1
),
parent_agg AS (
  SELECT s.trace_id,
    COUNTIF(p.span_id IS NULL AND s.parent_span_id IS NOT NULL AND COALESCE(s.otel_kind, '') NOT IN ('SERVER', 'CONSUMER')) AS missing_parent_count,
    MAX(CASE WHEN s.r_rn = 1 AND s.source_class IS NOT NULL AND p.span_id IS NULL AND s.parent_span_id IS NOT NULL THEN 1 ELSE 0 END) = 1 AS root_parent_missing
  FROM (
    SELECT trace_id, span_id, parent_span_id, otel_kind, source_class,
      ROW_NUMBER() OVER (PARTITION BY trace_id ORDER BY CASE WHEN source_class IS NOT NULL THEN 0 ELSE 1 END, start_time ASC, span_id ASC) AS r_rn
    FROM base_spans
  ) AS s LEFT JOIN base_spans AS p ON p.trace_id = s.trace_id AND p.span_id = s.parent_span_id
  GROUP BY s.trace_id
),
tool_agg AS (
  SELECT trace_id, COUNT(*) AS tool_call_count, COUNTIF(writes_state IS TRUE) AS mutating_tool_call_count,
    COUNTIF(modifies_workspace IS TRUE) AS workspace_edit_count, COUNTIF(writes_state IS NULL) AS unregistered_tool_count,
    COUNTIF(pair_occ > 1) AS repeated_tool_call_count,
    COUNTIF(base_content_missing IS TRUE AND COALESCE(is_model_call, FALSE) IS FALSE) AS tool_only_content_missing_count
  FROM (
    SELECT trace_id, writes_state, modifies_workspace, base_content_missing, is_model_call,
      CASE WHEN args_sha256 IS NOT NULL THEN ROW_NUMBER() OVER (PARTITION BY trace_id, COALESCE(tool_name, ''), args_sha256 ORDER BY start_time ASC, span_id ASC) ELSE 0 END AS pair_occ
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.tool_calls_base`(from_time, to_time)
  ) GROUP BY trace_id
),
event_agg AS (
  SELECT trace_id, COUNTIF(event_type = 'guardrail.intervention') AS guardrail_intervention_count,
    MAX(CASE WHEN event_type = 'outcome.escalated' THEN 1 ELSE 0 END) = 1 AS event_escalated
  FROM (
    SELECT trace_id, event_type, ROW_NUMBER() OVER (PARTITION BY sc, event_id ORDER BY et ASC) AS rn FROM (
      SELECT 'system_of_record' AS sc, event_id, event_time AS et, event_type, trace_id FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_sor`
      WHERE event_time >= from_time AND event_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND event_type IN ('guardrail.intervention', 'outcome.escalated') AND trace_id IS NOT NULL
      UNION ALL
      SELECT 'platform' AS sc, event_id, event_time AS et, event_type, trace_id FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_platform`
      WHERE event_time >= from_time AND event_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND event_type IN ('guardrail.intervention', 'outcome.escalated') AND trace_id IS NOT NULL
      UNION ALL
      SELECT 'application' AS sc, event_id, event_time AS et, event_type, trace_id FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_app`
      WHERE event_time >= from_time AND event_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND event_type IN ('guardrail.intervention', 'outcome.escalated') AND trace_id IS NOT NULL
      UNION ALL
      SELECT sc, CONCAT('span:', trace_id, ':', span_id, ':', CAST(ev_idx AS STRING)) AS event_id, SAFE_CAST(ev_time AS TIMESTAMP) AS et, 'guardrail.intervention' AS event_type, trace_id
      FROM (
        SELECT d.sc, d.trace_id, d.span_id, ev.time AS ev_time, ev_idx FROM (
          SELECT sc, trace_id, span_id, events, ROW_NUMBER() OVER (PARTITION BY sc, trace_id, span_id ORDER BY receive_time DESC, end_time DESC) AS srn FROM (
            SELECT 'application' AS sc, trace_id, span_id, receive_time, end_time, events FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans`
            WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
            UNION ALL
            SELECT 'platform' AS sc, trace_id, span_id, receive_time, end_time, events FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}`
            WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
          )
        ) AS d CROSS JOIN UNNEST(d.events) AS ev WITH OFFSET AS ev_idx
        WHERE d.srn = 1 AND ev.name = 'guardrail.intervention'
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
        SELECT s.trace_id, COALESCE(s.agent_name, p.agent_name, ra.root_agent_name, '') AS ag_key, s.span_id, s.start_time, msg_idx,
          (MAX(CASE WHEN JSON_VALUE(msg, '$.role') = 'user' THEN 1 ELSE 0 END) = 1 AND COUNTIF(
            JSON_VALUE(part, '$.type') = 'tool_call_response'
            OR NULLIF(TO_JSON_STRING(JSON_QUERY(part, '$.function_response')), 'null') IS NOT NULL
            OR NULLIF(TO_JSON_STRING(JSON_QUERY(part, '$.functionResponse')), 'null') IS NOT NULL
          ) = 0) AS is_real_user_msg
        FROM base_spans AS s
        LEFT JOIN (
          SELECT trace_id, ARRAY_AGG(agent_name IGNORE NULLS ORDER BY start_time ASC, span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS root_agent_name
          FROM base_spans WHERE span_kind = 'agent' GROUP BY trace_id
        ) AS ra ON ra.trace_id = s.trace_id
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
      FIRST_VALUE(CASE WHEN sp.source_class IS NOT NULL THEN sp.app_id END) OVER (PARTITION BY sp.trace_id ORDER BY CASE WHEN sp.source_class IS NOT NULL THEN 0 ELSE 1 END, sp.start_time ASC, sp.span_id ASC) AS r_app
    FROM base_spans AS sp
  ) AS s
  LEFT JOIN gateway_caller AS gc ON gc.trace_id = s.trace_id
  LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.skill_registry` AS sr
    ON sr.app_id = CASE WHEN s.has_gw_caller THEN gc.resolved_caller_app_id ELSE s.r_app END
    AND sr.skill_name = s.skill_name AND (s.skill_version IS NULL OR sr.skill_version = s.skill_version)
    AND sr.effective_from <= s.start_time AND (sr.effective_to IS NULL OR s.start_time < sr.effective_to)
  WHERE s.skill_name IS NOT NULL GROUP BY s.trace_id
),
core AS (
  SELECT sa.*,
    CASE WHEN sa.has_gw_caller THEN gc.resolved_caller_app_id ELSE sa.r_app END AS trace_owner_app_id,
    CASE
      WHEN (CASE WHEN sa.has_gw_caller THEN gc.resolved_caller_app_id ELSE sa.r_app END) IS NULL THEN CAST(NULL AS BOOL)
      WHEN sa.has_gw_caller THEN (gc.caller_identity_bound OR (sa.r_app = gc.resolved_caller_app_id AND sa.r_bnd = 1))
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
    ROW_NUMBER() OVER (PARTITION BY sa.trace_id ORDER BY r.effective_from DESC) AS oreg_rn
  FROM span_rollup AS sa
  LEFT JOIN gateway_caller AS gc ON gc.trace_id = sa.trace_id
  LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
    ON r.app_id = CASE WHEN sa.has_gw_caller THEN gc.resolved_caller_app_id ELSE sa.r_app END
    AND r.effective_from <= sa.trace_start_time AND (r.effective_to IS NULL OR sa.trace_start_time < r.effective_to)
)
SELECT
  c.trace_id, c.trace_owner_app_id, c.app_identity_bound, (c.trace_owner_app_id IS NULL) AS unregistered_app,
  c.owner, c.business_unit, c.environment, c.risk_tier, c.barrier_domain,
  CASE WHEN c.span_app_count = 0 AND c.trace_owner_app_id IS NOT NULL THEN 1 ELSE c.span_app_count END AS participating_app_count,
  c.root_span_id, (c.root_span_id IS NOT NULL) AS has_root_span, c.is_turn, c.root_status, c.trace_start_time,
  CASE WHEN c.f_sc IS NULL THEN 'runtime' ELSE 'application' END AS trace_start_writer,
  c.trace_end_time, c.last_span_arrival_time, COALESCE(pa.missing_parent_count, 0) AS missing_parent_count,
  CASE WHEN c.root_span_id IS NULL THEN CAST(NULL AS BOOL) ELSE COALESCE(pa.root_parent_missing, FALSE) END AS entry_parent_outside,
  (TIMESTAMP_DIFF(c.trace_end_time, c.trace_start_time, MILLISECOND) > ${READ_MARGIN_MINUTES} * 60 * 1000) AS exceeds_read_margin,
  COALESCE(CASE WHEN c.th_hex IS NOT NULL THEN 1.0 - (CAST(SAFE_CAST(CONCAT('0x', RPAD(c.th_hex, 14, '0')) AS INT64) AS FLOAT64) / 72057594037927936.0) END, c.r_sp, c.f_sp, c.e_sp) AS sampling_probability,
  COALESCE(c.g_tp, c.r_tp, c.e_tp) AS trigger_principal, COALESCE(c.g_ap, c.r_ap, c.e_ap) AS agent_principal, COALESCE(c.g_act, c.r_act, c.e_act) AS act_chain,
  c.r_uid AS user_id, c.platform_user_id,
  CASE WHEN c.r_uid IS NULL THEN CAST(NULL AS STRING) ELSE c.r_sc END AS user_id_source,
  COALESCE(c.user_id_conflict_count, 0) AS user_id_conflict_count, COALESCE(c.g_tid, c.r_tid, c.e_tid) AS tenant_id,
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
  c.final_model_confidence, c.min_model_confidence, c.final_confidence_end_time, c.final_confidence_span_id,
  (c.model_escalated OR COALESCE(ea.event_escalated, FALSE)) AS escalated, c.app_failed, c.e_fr AS app_failure_reason,
  COALESCE(c.g_tt, c.r_tt, c.e_tt) AS traffic_type, c.span_count, c.model_call_count,
  COALESCE(ta.tool_call_count, 0) AS tool_call_count, COALESCE(ta.mutating_tool_call_count, 0) AS mutating_tool_call_count,
  COALESCE(ta.workspace_edit_count, 0) AS workspace_edit_count, COALESCE(ta.unregistered_tool_count, 0) AS unregistered_tool_count,
  COALESCE(ea.guardrail_intervention_count, 0) AS guardrail_intervention_count,
  c.model_content_missing_count + COALESCE(ta.tool_only_content_missing_count, 0) AS content_missing_call_count,
  CASE WHEN c.is_turn IS FALSE THEN 0 ELSE 1 + COALESCE(ute.extra_user_turns, 0) END AS user_turn_count,
  c.duplicate_span_count, COALESCE(ta.repeated_tool_call_count, 0) AS repeated_tool_call_count,
  c.error_count, c.throttle_count, c.missing_user_id_span_count, c.missing_outcome_id_span_count, c.missing_conversation_id_span_count,
  CASE WHEN c.model_call_count = 0 THEN CAST(NULL AS INT64) ELSE c.sum_in_tok END AS input_tokens,
  CASE WHEN c.model_call_count = 0 THEN CAST(NULL AS INT64) ELSE c.sum_out_tok END AS output_tokens,
  CASE WHEN c.model_call_count = 0 THEN CAST(NULL AS INT64) ELSE c.sum_cr_tok END AS cache_read_input_tokens,
  CASE WHEN c.model_call_count = 0 THEN CAST(NULL AS INT64) ELSE c.sum_cw_tok END AS cache_write_input_tokens,
  CASE WHEN c.model_call_count = 0 THEN CAST(NULL AS INT64) ELSE c.sum_rs_tok END AS reasoning_output_tokens,
  CASE WHEN TO_JSON_STRING(c.rl) = '{}' THEN CAST(NULL AS JSON) ELSE c.rl END AS restricted_labels,
  CASE WHEN TO_JSON_STRING(c.ol) = '{}' THEN CAST(NULL AS JSON) ELSE c.ol END AS open_labels
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
