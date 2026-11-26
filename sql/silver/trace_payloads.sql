-- Silver Restricted `trace_payloads` Table Function (`ADR-020`, `ADR-036`, `ADR-044`, `ADR-045`, `ADR-048`).
-- One row per content-bearing turn across ADK Format 1 inline, OTel GenAI inline, and Shape-3 BigLake
-- with static Hive `dt`/`hr` pruning, split single-pass input/output BigLake joins, and structured part checks.

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.trace_payloads`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH base_spans AS (
    SELECT *
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.spans`(from_time, to_time)
  ),
  trace_rollup AS (
    SELECT
      s.trace_id,
      MIN(s.start_time) AS trace_start_time,
      MAX(CASE WHEN s.span_kind IN ('agent', 'model_call', 'embedding', 'tool_call', 'retrieval', 'memory') OR s.is_model_call IS TRUE THEN 1 ELSE 0 END) = 1 AS is_turn,
      MAX(CASE WHEN s.source_class = 'platform' AND s.caller_app_id IS NOT NULL THEN 1 ELSE 0 END) = 1 AS has_gw_caller,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.app_id END) AS root_app_id,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.tenant_id END) AS root_tenant_id,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.previous_interaction_id END) AS root_prev,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.page_count END) AS root_pages,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.document_bytes END) AS root_doc_b,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.attachment_sha256 END) AS root_att_sha,
      SAFE.PARSE_JSON(MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL AND s.labels IS NOT NULL THEN TO_JSON_STRING(s.labels) END)) AS root_labels,
      ARRAY_AGG(CASE WHEN s.source_class = 'platform' THEN s.tenant_id END IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS g_tenant_id,
      ARRAY_AGG(CASE WHEN s.source_class = 'platform' THEN s.page_count END IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS g_pages,
      ARRAY_AGG(CASE WHEN s.source_class = 'platform' THEN s.document_bytes END IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS g_doc_b,
      ARRAY_AGG(CASE WHEN s.source_class = 'platform' THEN s.attachment_sha256 END IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS g_att_sha,
      ARRAY_AGG(s.tenant_id IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_tenant_id,
      ARRAY_AGG(CASE WHEN s.is_model_call IS TRUE THEN s.previous_interaction_id END IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_call_prev,
      ARRAY_AGG(s.previous_interaction_id IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_prev,
      MAX(s.page_count) AS max_pages,
      MAX(s.document_bytes) AS max_doc_b,
      ARRAY_AGG(s.attachment_sha256 IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_att_sha,
      ARRAY_AGG(CASE WHEN s.span_kind = 'agent' THEN s.agent_name END IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS root_agent_name,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.workflow_name') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_wf_n,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.agent_version') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_ag_v,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.agent_description') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_ag_d,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.prompt_name') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_pr_n,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.prompt_version') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_pr_v,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.mcp_method_name') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_mcp,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.a2a_task_id') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_a2a_id,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.a2a_task_state') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_a2a_st,
      ARRAY_AGG(JSON_VALUE(s.labels, '$.adk_invocation_id') IGNORE NULLS ORDER BY s.start_time, s.span_id LIMIT 1)[SAFE_OFFSET(0)] AS e_adk_id
    FROM (
      SELECT sp.*, ROW_NUMBER() OVER (PARTITION BY sp.trace_id ORDER BY CASE WHEN sp.source_class IS NOT NULL THEN 0 ELSE 1 END, sp.start_time ASC, sp.span_id ASC) AS root_rn
      FROM base_spans AS sp
    ) AS s
    GROUP BY s.trace_id
  ),
  gateway_caller AS (
    SELECT trace_id, resolved_caller_app_id
    FROM (
      SELECT
        s.trace_id, r.app_id AS resolved_caller_app_id,
        ROW_NUMBER() OVER (PARTITION BY s.trace_id ORDER BY s.start_time ASC, s.span_id ASC, r.effective_from DESC) AS rn
      FROM base_spans AS s
      JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
        ON r.app_id = s.caller_app_id AND r.effective_from <= s.start_time AND (r.effective_to IS NULL OR s.start_time < r.effective_to)
      WHERE s.source_class = 'platform' AND s.caller_app_id IS NOT NULL
    )
    WHERE rn = 1
  ),
  allowed_traces AS (
    SELECT
      t.trace_id, t.trace_owner_app_id, t.business_unit, t.tenant_id, t.previous_interaction_id, t.is_turn,
      t.page_count, t.document_bytes, t.attachment_sha256, t.root_agent_name,
      CASE WHEN TO_JSON_STRING(t.rl) = '{}' THEN CAST(NULL AS JSON) ELSE t.rl END AS restricted_labels
    FROM (
      SELECT
        tr.trace_id,
        CASE WHEN tr.has_gw_caller THEN gc.resolved_caller_app_id ELSE tr.root_app_id END AS trace_owner_app_id,
        r.business_unit,
        COALESCE(tr.g_tenant_id, tr.root_tenant_id, tr.e_tenant_id) AS tenant_id,
        COALESCE(tr.root_prev, tr.e_call_prev, tr.e_prev) AS previous_interaction_id,
        tr.is_turn,
        COALESCE(tr.g_pages, tr.root_pages, tr.max_pages) AS page_count,
        COALESCE(tr.g_doc_b, tr.root_doc_b, tr.max_doc_b) AS document_bytes,
        COALESCE(tr.g_att_sha, tr.root_att_sha, tr.e_att_sha) AS attachment_sha256,
        tr.root_agent_name,
        JSON_STRIP_NULLS(JSON_OBJECT(
          'apphub_service_id', JSON_VALUE(tr.root_labels, '$.apphub_service_id'),
          'apphub_workload_id', JSON_VALUE(tr.root_labels, '$.apphub_workload_id'),
          'service_name', JSON_VALUE(tr.root_labels, '$.service_name'),
          'telemetry_environment', JSON_VALUE(tr.root_labels, '$.telemetry_environment'),
          'cloud_provider', JSON_VALUE(tr.root_labels, '$.cloud_provider'),
          'cloud_region', JSON_VALUE(tr.root_labels, '$.cloud_region'),
          'cloud_account_id', JSON_VALUE(tr.root_labels, '$.cloud_account_id'),
          'agent_version', COALESCE(JSON_VALUE(tr.root_labels, '$.agent_version'), tr.e_ag_v),
          'agent_description', COALESCE(JSON_VALUE(tr.root_labels, '$.agent_description'), tr.e_ag_d),
          'workflow_name', COALESCE(JSON_VALUE(tr.root_labels, '$.workflow_name'), tr.e_wf_n),
          'prompt_name', COALESCE(JSON_VALUE(tr.root_labels, '$.prompt_name'), tr.e_pr_n),
          'prompt_version', COALESCE(JSON_VALUE(tr.root_labels, '$.prompt_version'), tr.e_pr_v),
          'mcp_method_name', COALESCE(JSON_VALUE(tr.root_labels, '$.mcp_method_name'), tr.e_mcp),
          'a2a_task_id', COALESCE(JSON_VALUE(tr.root_labels, '$.a2a_task_id'), tr.e_a2a_id),
          'a2a_task_state', COALESCE(JSON_VALUE(tr.root_labels, '$.a2a_task_state'), tr.e_a2a_st),
          'adk_invocation_id', COALESCE(JSON_VALUE(tr.root_labels, '$.adk_invocation_id'), tr.e_adk_id),
          'telemetry_sdk_name', JSON_VALUE(tr.root_labels, '$.telemetry_sdk_name'),
          'telemetry_sdk_version', JSON_VALUE(tr.root_labels, '$.telemetry_sdk_version')
        )) AS rl,
        ROW_NUMBER() OVER (PARTITION BY tr.trace_id ORDER BY r.effective_from DESC) AS rn
      FROM trace_rollup AS tr
      LEFT JOIN gateway_caller AS gc ON gc.trace_id = tr.trace_id
      LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
        ON r.app_id = CASE WHEN tr.has_gw_caller THEN gc.resolved_caller_app_id ELSE tr.root_app_id END
        AND r.effective_from <= tr.trace_start_time AND (r.effective_to IS NULL OR tr.trace_start_time < r.effective_to)
    ) AS t
    WHERE t.rn = 1 AND t.is_turn IS TRUE
      AND EXISTS (
        SELECT 1
        FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.unit_entitlements` AS ue
        WHERE LOWER(ue.principal_email) = LOWER(SESSION_USER())
          AND ue.business_unit = COALESCE(t.business_unit, '__unowned__')
          AND (ue.tenant_id = '*' OR ue.tenant_id = t.tenant_id)
      )
  ),
  raw_spans AS (
    SELECT trace_id, span_id, attributes,
      ROW_NUMBER() OVER (PARTITION BY trace_id, span_id ORDER BY src_prio ASC, receive_time DESC, end_time DESC) AS rn
    FROM (
      SELECT 1 AS src_prio, trace_id, span_id, receive_time, end_time, attributes FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans`
      WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      UNION ALL
      SELECT 0 AS src_prio, trace_id, span_id, receive_time, end_time, attributes FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}`
      WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE) AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    )
  ),
  shape3_window AS (
    SELECT ref_uri, payload
    FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.shape3_payloads`
    WHERE dt >= DATE(TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE))
      AND dt <= DATE(TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE))
      AND (dt > DATE(TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)) OR hr >= FORMAT_TIMESTAMP('%H', TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)))
      AND (dt < DATE(TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)) OR hr <= FORMAT_TIMESTAMP('%H', TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)))
  ),
  -- Inline call extraction WITHOUT Shape-3 joins (keeps Shape-3 scans isolated per side).
  raw_calls AS (
    SELECT
      mc.*,
      ROW_NUMBER() OVER (
        PARTITION BY mc.trace_id
        ORDER BY CASE WHEN mc.root_agent_name IS NULL OR COALESCE(mc.agent_name, '') = mc.root_agent_name THEN 0 ELSE 1 END, mc.start_time ASC, mc.span_id ASC
      ) AS call_rn
    FROM (
      SELECT
        s.trace_id, s.span_id, s.start_time, s.end_time, s.nudge_id,
        alt.root_agent_name,
        COALESCE(
          s.agent_name,
          CASE WHEN p1.span_kind = 'agent' THEN p1.agent_name END,
          CASE WHEN p2.span_kind = 'agent' THEN p2.agent_name END,
          CASE WHEN p3.span_kind = 'agent' THEN p3.agent_name END,
          CASE WHEN p4.span_kind = 'agent' THEN p4.agent_name END,
          p1.agent_name, p2.agent_name, p3.agent_name, p4.agent_name
        ) AS agent_name,
        COALESCE(JSON_VALUE(rs.attributes, '$."gen_ai.input.messages_ref"'), CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN JSON_VALUE(prs.attributes, '$."gen_ai.input.messages_ref"') END) AS input_ref,
        COALESCE(JSON_VALUE(rs.attributes, '$."gen_ai.output.messages_ref"'), CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN JSON_VALUE(prs.attributes, '$."gen_ai.output.messages_ref"') END) AS output_ref,
        COALESCE(JSON_VALUE(rs.attributes, '$."gen_ai.system_instructions_ref"'), CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN JSON_VALUE(prs.attributes, '$."gen_ai.system_instructions_ref"') END) AS system_instructions_ref,
        COALESCE(JSON_VALUE(rs.attributes, '$."gen_ai.tool.definitions_ref"'), CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN JSON_VALUE(prs.attributes, '$."gen_ai.tool.definitions_ref"') END) AS tool_definitions_ref,
        COALESCE(
          SAFE.PARSE_JSON(JSON_VALUE(rs.attributes, '$."gen_ai.input.messages"')),
          SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gen_ai.input.messages"')), 'null')),
          SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(
            COALESCE(
              SAFE.PARSE_JSON(NULLIF(JSON_VALUE(rs.attributes, '$."gcp.vertex.agent.llm_request"'), '{}')),
              SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gcp.vertex.agent.llm_request"')), 'null')),
              CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN
                COALESCE(SAFE.PARSE_JSON(NULLIF(JSON_VALUE(prs.attributes, '$."gcp.vertex.agent.llm_request"'), '{}')), SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(prs.attributes, '$."gcp.vertex.agent.llm_request"')), 'null')))
              END
            ), '$.contents')), 'null'))
        ) AS inline_input_messages,
        COALESCE(
          SAFE.PARSE_JSON(JSON_VALUE(rs.attributes, '$."gen_ai.output.messages"')),
          SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gen_ai.output.messages"')), 'null')),
          CASE
            WHEN NULLIF(TO_JSON_STRING(JSON_QUERY(
              COALESCE(
                SAFE.PARSE_JSON(NULLIF(JSON_VALUE(rs.attributes, '$."gcp.vertex.agent.llm_response"'), '{}')),
                SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gcp.vertex.agent.llm_response"')), 'null')),
                CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN
                  COALESCE(SAFE.PARSE_JSON(NULLIF(JSON_VALUE(prs.attributes, '$."gcp.vertex.agent.llm_response"'), '{}')), SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(prs.attributes, '$."gcp.vertex.agent.llm_response"')), 'null')))
                END
              ), '$.content')), 'null') IS NOT NULL
            THEN SAFE.PARSE_JSON(CONCAT('[', TO_JSON_STRING(JSON_QUERY(
              COALESCE(
                SAFE.PARSE_JSON(NULLIF(JSON_VALUE(rs.attributes, '$."gcp.vertex.agent.llm_response"'), '{}')),
                SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gcp.vertex.agent.llm_response"')), 'null')),
                CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN
                  COALESCE(SAFE.PARSE_JSON(NULLIF(JSON_VALUE(prs.attributes, '$."gcp.vertex.agent.llm_response"'), '{}')), SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(prs.attributes, '$."gcp.vertex.agent.llm_response"')), 'null')))
                END
              ), '$.content')), ']'))
          END
        ) AS inline_output_messages,
        COALESCE(
          SAFE.PARSE_JSON(JSON_VALUE(rs.attributes, '$."gen_ai.system_instructions"')),
          SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gen_ai.system_instructions"')), 'null')),
          SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(
            COALESCE(
              SAFE.PARSE_JSON(NULLIF(JSON_VALUE(rs.attributes, '$."gcp.vertex.agent.llm_request"'), '{}')),
              SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gcp.vertex.agent.llm_request"')), 'null')),
              CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN
                COALESCE(SAFE.PARSE_JSON(NULLIF(JSON_VALUE(prs.attributes, '$."gcp.vertex.agent.llm_request"'), '{}')), SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(prs.attributes, '$."gcp.vertex.agent.llm_request"')), 'null')))
              END
            ), '$.config.system_instruction')), 'null'))
        ) AS inline_system_instructions,
        COALESCE(
          SAFE.PARSE_JSON(JSON_VALUE(rs.attributes, '$."gen_ai.tool.definitions"')),
          SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gen_ai.tool.definitions"')), 'null')),
          SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(
            COALESCE(
              SAFE.PARSE_JSON(NULLIF(JSON_VALUE(rs.attributes, '$."gcp.vertex.agent.llm_request"'), '{}')),
              SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(rs.attributes, '$."gcp.vertex.agent.llm_request"')), 'null')),
              CASE WHEN s.operation_name = 'generate_content' AND p1.span_name = 'call_llm' THEN
                COALESCE(SAFE.PARSE_JSON(NULLIF(JSON_VALUE(prs.attributes, '$."gcp.vertex.agent.llm_request"'), '{}')), SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(prs.attributes, '$."gcp.vertex.agent.llm_request"')), 'null')))
              END
            ), '$.config.tools')), 'null'))
        ) AS inline_tool_definitions
      FROM base_spans AS s
      JOIN allowed_traces AS alt ON alt.trace_id = s.trace_id
      LEFT JOIN base_spans AS p1 ON p1.trace_id = s.trace_id AND p1.span_id = s.parent_span_id
      LEFT JOIN base_spans AS p2 ON p2.trace_id = s.trace_id AND p2.span_id = p1.parent_span_id
      LEFT JOIN base_spans AS p3 ON p3.trace_id = s.trace_id AND p3.span_id = p2.parent_span_id
      LEFT JOIN base_spans AS p4 ON p4.trace_id = s.trace_id AND p4.span_id = p3.parent_span_id
      LEFT JOIN raw_spans AS rs ON rs.trace_id = s.trace_id AND rs.span_id = s.span_id AND rs.rn = 1
      LEFT JOIN raw_spans AS prs ON prs.trace_id = s.trace_id AND prs.span_id = p1.span_id AND prs.rn = 1
      WHERE s.is_model_call IS TRUE AND s.span_kind != 'embedding'
    ) AS mc
  ),
  -- Single Shape-3 BigLake pass for inputs (first-call metadata, prior_history, turn_prompt, media_uris).
  input_agg AS (
    SELECT
      trace_id,
      SAFE.PARSE_JSON(MAX(CASE WHEN call_rn = 1 AND system_instructions IS NOT NULL THEN TO_JSON_STRING(system_instructions) END)) AS system_instructions,
      MAX(CASE WHEN call_rn = 1 THEN system_instructions_ref END) AS system_instructions_ref,
      SAFE.PARSE_JSON(MAX(CASE WHEN call_rn = 1 AND tool_definitions IS NOT NULL THEN TO_JSON_STRING(tool_definitions) END)) AS tool_definitions,
      MAX(CASE WHEN call_rn = 1 THEN tool_definitions_ref END) AS tool_definitions_ref,
      MAX(CASE WHEN call_rn = 1 AND input_ref_missing THEN 1 ELSE 0 END) = 1 AS first_input_ref_missing,
      MAX(CASE WHEN input_ref_missing THEN 1 ELSE 0 END) = 1 AS any_input_ref_missing,
      CASE
        WHEN MAX(CASE WHEN call_rn = 1 THEN msg_count END) > 1
          AND COUNTIF(call_rn = 1 AND msg_idx < msg_count - 1 AND part_rn = 1 AND msg IS NOT NULL) > 0 THEN
          TO_JSON(ARRAY_AGG(CASE WHEN call_rn = 1 AND msg_idx < msg_count - 1 AND part_rn = 1 THEN msg END IGNORE NULLS ORDER BY msg_idx ASC))
        ELSE CAST(NULL AS JSON)
      END AS raw_prior_history,
      SAFE.PARSE_JSON(MAX(CASE WHEN call_rn = 1 AND msg_idx = msg_count - 1 AND part_rn = 1 AND msg IS NOT NULL THEN TO_JSON_STRING(msg) END)) AS turn_prompt,
      CASE
        WHEN COUNTIF(media_uri IS NOT NULL) > 0 THEN
          TO_JSON(ARRAY_AGG(
            CASE WHEN media_uri IS NOT NULL THEN
              JSON_STRIP_NULLS(JSON_OBJECT('uri', media_uri, 'document_bytes', document_bytes, 'page_count', page_count, 'attachment_sha256', attachment_sha256))
            END
            IGNORE NULLS ORDER BY start_time ASC, span_id ASC
          ))
        ELSE CAST(NULL AS JSON)
      END AS media_uris
    FROM (
      SELECT
        ip.trace_id, ip.span_id, ip.start_time, ip.call_rn, ip.system_instructions, ip.system_instructions_ref,
        ip.tool_definitions, ip.tool_definitions_ref, ip.input_ref_missing, ip.document_bytes, ip.page_count, ip.attachment_sha256,
        msg, msg_idx,
        MAX(CASE WHEN msg IS NOT NULL THEN msg_idx + 1 ELSE 0 END) OVER (PARTITION BY ip.trace_id, ip.span_id) AS msg_count,
        COALESCE(JSON_VALUE(part, '$.uri'), JSON_VALUE(part, '$.file_data.file_uri')) AS media_uri,
        ROW_NUMBER() OVER (PARTITION BY ip.trace_id, ip.span_id, msg_idx ORDER BY part_idx ASC) AS part_rn
      FROM (
        SELECT
          rc.trace_id, rc.span_id, rc.start_time, rc.call_rn,
          CASE WHEN rc.system_instructions_ref IS NOT NULL THEN CAST(NULL AS JSON) ELSE rc.inline_system_instructions END AS system_instructions,
          rc.system_instructions_ref,
          CASE WHEN rc.tool_definitions_ref IS NOT NULL THEN CAST(NULL AS JSON) ELSE rc.inline_tool_definitions END AS tool_definitions,
          rc.tool_definitions_ref,
          COALESCE(rc.inline_input_messages, s_in.payload) AS input_messages,
          (rc.input_ref IS NOT NULL AND rc.inline_input_messages IS NULL AND s_in.payload IS NULL) AS input_ref_missing,
          alt.document_bytes, alt.page_count, alt.attachment_sha256
        FROM raw_calls AS rc
        JOIN allowed_traces AS alt ON alt.trace_id = rc.trace_id
        LEFT JOIN shape3_window AS s_in ON s_in.ref_uri = rc.input_ref
      ) AS ip
      LEFT JOIN UNNEST(JSON_QUERY_ARRAY(ip.input_messages)) AS msg WITH OFFSET AS msg_idx
      LEFT JOIN UNNEST(JSON_QUERY_ARRAY(msg, '$.parts')) AS part WITH OFFSET AS part_idx
    )
    GROUP BY trace_id
  ),
  -- Single Shape-3 BigLake pass for outputs (`step_outputs`, `any_output_ref_missing`, structured `model_turn_output`).
  output_agg AS (
    SELECT
      trace_id,
      MAX(CASE WHEN output_ref_missing THEN 1 ELSE 0 END) = 1 AS any_output_ref_missing,
      CASE
        WHEN COUNTIF(out_msg_rn = 1 AND output_messages IS NOT NULL) > 0 THEN
          TO_JSON(ARRAY_AGG(
            CASE WHEN out_msg_rn = 1 AND output_messages IS NOT NULL THEN
              JSON_STRIP_NULLS(JSON_OBJECT('span_id', span_id, 'nudge_id', nudge_id, 'output', output_messages))
            END
            IGNORE NULLS ORDER BY start_time ASC, span_id ASC
          ))
        ELSE CAST(NULL AS JSON)
      END AS step_outputs,
      ARRAY_AGG(
        CASE WHEN is_plain_assistant_output THEN out_msg END
        IGNORE NULLS ORDER BY root_prio ASC, end_time DESC, span_id DESC, out_idx DESC LIMIT 1
      )[SAFE_OFFSET(0)] AS model_turn_output
    FROM (
      SELECT
        op.trace_id, op.span_id, op.start_time, op.end_time, op.nudge_id,
        op.output_messages, op.output_ref_missing,
        CASE WHEN op.root_agent_name IS NULL OR COALESCE(op.agent_name, '') = op.root_agent_name THEN 0 ELSE 1 END AS root_prio,
        out_msg, out_idx,
        ROW_NUMBER() OVER (PARTITION BY op.trace_id, op.span_id ORDER BY out_idx ASC) AS out_msg_rn,
        (
          out_msg IS NOT NULL
          AND COALESCE(JSON_VALUE(out_msg, '$.role'), 'assistant') IN ('assistant', 'model')
          AND has_tool_call_part = 0
        ) AS is_plain_assistant_output
      FROM (
        SELECT
          op.trace_id, op.span_id, op.start_time, op.end_time, op.nudge_id, op.agent_name, op.root_agent_name, op.output_ref_missing,
          out_idx,
          SAFE.PARSE_JSON(MAX(CASE WHEN op.output_messages IS NOT NULL THEN TO_JSON_STRING(op.output_messages) END)) AS output_messages,
          SAFE.PARSE_JSON(MAX(CASE WHEN out_msg IS NOT NULL THEN TO_JSON_STRING(out_msg) END)) AS out_msg,
          MAX(CASE WHEN
            JSON_VALUE(opart, '$.type') IN ('tool_call', 'function_call')
            OR NULLIF(TO_JSON_STRING(JSON_QUERY(opart, '$.function_call')), 'null') IS NOT NULL
            OR NULLIF(TO_JSON_STRING(JSON_QUERY(opart, '$.functionCall')), 'null') IS NOT NULL
          THEN 1 ELSE 0 END) AS has_tool_call_part
        FROM (
          SELECT
            rc.trace_id, rc.span_id, rc.start_time, rc.end_time, rc.nudge_id, rc.agent_name, rc.root_agent_name,
            COALESCE(rc.inline_output_messages, s_out.payload) AS output_messages,
            (rc.output_ref IS NOT NULL AND rc.inline_output_messages IS NULL AND s_out.payload IS NULL) AS output_ref_missing
          FROM raw_calls AS rc
          LEFT JOIN shape3_window AS s_out ON s_out.ref_uri = rc.output_ref
        ) AS op
        LEFT JOIN UNNEST(JSON_QUERY_ARRAY(op.output_messages)) AS out_msg WITH OFFSET AS out_idx
        LEFT JOIN UNNEST(JSON_QUERY_ARRAY(out_msg, '$.parts')) AS opart
        GROUP BY op.trace_id, op.span_id, op.start_time, op.end_time, op.nudge_id, op.agent_name, op.root_agent_name, op.output_ref_missing, out_idx
      ) AS op
    )
    GROUP BY trace_id
  ),
  tool_rollup AS (
    SELECT
      tc.trace_id,
      CASE
        WHEN COUNTIF(tc.results IS NOT NULL) > 0 THEN
          TO_JSON(ARRAY_AGG(
            CASE WHEN tc.results IS NOT NULL THEN
              JSON_OBJECT(
                'span_id', tc.span_id, 'tool_name', tc.tool_name, 'operation_name', tc.operation_name,
                'result', COALESCE(SAFE.PARSE_JSON(tc.results), TO_JSON(tc.results)),
                'writes_state', tc.writes_state, 'modifies_workspace', tc.modifies_workspace,
                'executes_code', tc.executes_code, 'sends_external_reply', tc.sends_external_reply,
                'returns_pii', tc.returns_pii, 'creates_ticket', tc.creates_ticket
              )
            END
            IGNORE NULLS ORDER BY tc.start_time ASC, tc.span_id ASC
          ))
        ELSE CAST(NULL AS JSON)
      END AS turn_context,
      COUNTIF(tc.content_missing IS TRUE) AS missing_tool_count,
      MAX(CASE WHEN tc.sends_external_reply IS TRUE THEN 1 ELSE 0 END) = 1 AS has_external_reply,
      MAX(CASE WHEN tc.ext_rn = 1 AND tc.sends_external_reply IS TRUE THEN tc.arguments END) AS reply_raw_args
    FROM (
      SELECT t.*,
        ROW_NUMBER() OVER (PARTITION BY t.trace_id ORDER BY CASE WHEN t.sends_external_reply IS TRUE THEN 0 ELSE 1 END, t.end_time DESC, t.span_id DESC) AS ext_rn
      FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.tool_calls`(from_time, to_time) AS t
    ) AS tc
    GROUP BY tc.trace_id
  ),
  assembled AS (
    SELECT
      alt.trace_id, alt.trace_owner_app_id, alt.previous_interaction_id,
      CASE WHEN alt.previous_interaction_id IS NULL THEN ia.raw_prior_history ELSE CAST(NULL AS JSON) END AS prior_history,
      ia.turn_prompt, tr.turn_context,
      -- Never fall back to unsent draft model text when a `sends_external_reply` tool call exists.
      CASE
        WHEN COALESCE(tr.has_external_reply, FALSE) THEN
          CASE WHEN tr.reply_raw_args IS NOT NULL THEN COALESCE(SAFE.PARSE_JSON(tr.reply_raw_args), TO_JSON(tr.reply_raw_args)) END
        ELSE oa.model_turn_output
      END AS turn_output,
      oa.step_outputs, ia.media_uris,
      ia.system_instructions, ia.system_instructions_ref, ia.tool_definitions, ia.tool_definitions_ref,
      CASE
        WHEN alt.previous_interaction_id IS NOT NULL THEN CAST(NULL AS BOOL)
        WHEN COALESCE(ia.first_input_ref_missing, FALSE) THEN FALSE
        WHEN ia.raw_prior_history IS NOT NULL THEN TRUE
        ELSE CAST(NULL AS BOOL)
      END AS prior_history_read_whole,
      CASE WHEN COALESCE(ia.first_input_ref_missing, FALSE) THEN FALSE WHEN ia.turn_prompt IS NOT NULL THEN TRUE ELSE CAST(NULL AS BOOL) END AS turn_prompt_read_whole,
      CASE
        WHEN COALESCE(tr.missing_tool_count, 0) > 0 AND (COALESCE(ia.any_input_ref_missing, FALSE) OR COALESCE(oa.any_output_ref_missing, FALSE)) THEN FALSE
        WHEN tr.turn_context IS NOT NULL THEN TRUE
        ELSE CAST(NULL AS BOOL)
      END AS turn_context_read_whole,
      CASE
        WHEN COALESCE(tr.has_external_reply, FALSE) AND tr.reply_raw_args IS NULL AND (COALESCE(ia.any_input_ref_missing, FALSE) OR COALESCE(oa.any_output_ref_missing, FALSE)) THEN FALSE
        WHEN tr.reply_raw_args IS NOT NULL THEN TRUE
        WHEN COALESCE(tr.has_external_reply, FALSE) THEN CAST(NULL AS BOOL)
        WHEN COALESCE(oa.any_output_ref_missing, FALSE) THEN FALSE
        WHEN oa.model_turn_output IS NOT NULL THEN TRUE
        ELSE CAST(NULL AS BOOL)
      END AS turn_output_read_whole,
      CASE WHEN COALESCE(oa.any_output_ref_missing, FALSE) THEN FALSE WHEN oa.step_outputs IS NOT NULL THEN TRUE ELSE CAST(NULL AS BOOL) END AS step_outputs_read_whole,
      alt.restricted_labels AS labels,
      COALESCE(ia.any_input_ref_missing, FALSE) AS any_input_ref_missing,
      COALESCE(oa.any_output_ref_missing, FALSE) AS any_output_ref_missing
    FROM allowed_traces AS alt
    LEFT JOIN input_agg AS ia ON ia.trace_id = alt.trace_id
    LEFT JOIN output_agg AS oa ON oa.trace_id = alt.trace_id
    LEFT JOIN tool_rollup AS tr ON tr.trace_id = alt.trace_id
  )
  SELECT
    trace_id, trace_owner_app_id, previous_interaction_id, prior_history, turn_prompt, turn_context,
    turn_output, step_outputs, media_uris, system_instructions, system_instructions_ref,
    tool_definitions, tool_definitions_ref, prior_history_read_whole, turn_prompt_read_whole,
    turn_context_read_whole, turn_output_read_whole, step_outputs_read_whole, labels
  FROM assembled
  WHERE prior_history IS NOT NULL OR turn_prompt IS NOT NULL OR turn_context IS NOT NULL
    OR turn_output IS NOT NULL OR step_outputs IS NOT NULL OR media_uris IS NOT NULL
    OR system_instructions IS NOT NULL OR system_instructions_ref IS NOT NULL
    OR tool_definitions IS NOT NULL OR tool_definitions_ref IS NOT NULL
    OR any_input_ref_missing OR any_output_ref_missing
);
