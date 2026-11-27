-- Silver Internal Text-Free Spans Table Function (`ADR-026`, `ADR-035`–`ADR-045`, `ADR-047`, `ADR-048`).
-- Single-pass shape routing per landing place with inside-window start_time pruning,
-- latest-arrival deduplication, 6-hop innermost token-span detection, single-pass ADK Format 1 fold, and SCD-2 app lookup.

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.spans`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH raw_union AS (
    SELECT
      '${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans' AS landing_place,
      CASE
        WHEN s.instrumentation_scope.name IS NULL AND s.kind IS NULL THEN CAST(NULL AS STRING)
        ELSE 'application'
      END AS source_class,
      s.trace_id, s.span_id, s.parent_span_id,
      COALESCE(s.name, '') AS span_name,
      s.kind AS raw_kind, s.start_time, s.end_time, s.receive_time,
      s.status.code AS raw_status_code,
      NULLIF(s.trace_state, '') AS trace_state,
      s.attributes,
      s.resource.attributes AS resource_attributes
    FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans` AS s
    WHERE s.start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      AND s.start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)

    UNION ALL

    SELECT
      '${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}' AS landing_place,
      CASE
        WHEN s.instrumentation_scope.name IS NULL AND s.kind IS NULL THEN CAST(NULL AS STRING)
        ELSE 'platform'
      END AS source_class,
      s.trace_id, s.span_id, s.parent_span_id,
      COALESCE(s.name, '') AS span_name,
      s.kind AS raw_kind, s.start_time, s.end_time, s.receive_time,
      s.status.code AS raw_status_code,
      NULLIF(s.trace_state, '') AS trace_state,
      s.attributes,
      s.resource.attributes AS resource_attributes
    FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}` AS s
    WHERE s.start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      AND s.start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
  ),
  deduped_across_places AS (
    SELECT
      r.*,
      COUNT(*) OVER (PARTITION BY r.trace_id, r.span_id) - 1 AS duplicate_count,
      ROW_NUMBER() OVER (
        PARTITION BY r.trace_id, r.span_id
        ORDER BY CASE WHEN r.source_class = 'platform' THEN 0 WHEN r.source_class = 'application' THEN 1 ELSE 2 END,
          r.receive_time DESC, r.end_time DESC, r.span_id ASC
      ) AS cross_rn
    FROM raw_union AS r
  ),
  in_window AS (
    SELECT *
    FROM (
      SELECT d.*, MIN(d.start_time) OVER (PARTITION BY d.trace_id) AS trace_first_start_time
      FROM deduped_across_places AS d
      WHERE d.cross_rn = 1
    )
    WHERE trace_first_start_time >= from_time AND trace_first_start_time < to_time
  ),
  parsed_raw AS (
    SELECT
      w.landing_place, w.source_class, w.trace_id, w.span_id, w.parent_span_id, w.span_name,
      (
        COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.input_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.output_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.cache_read.input_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.cache_write.input_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.reasoning.output_tokens"') AS INT64), 0) > 0
        OR COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.thoughts_tokens"') AS INT64), 0) > 0
      ) AS has_tok,
      CASE w.raw_kind WHEN 1 THEN 'INTERNAL' WHEN 2 THEN 'SERVER' WHEN 3 THEN 'CLIENT' WHEN 4 THEN 'PRODUCER' WHEN 5 THEN 'CONSUMER' ELSE CAST(NULL AS STRING) END AS otel_kind,
      w.start_time, w.end_time, w.receive_time,
      GREATEST(0, TIMESTAMP_DIFF(w.end_time, w.start_time, MILLISECOND)) AS duration_ms,
      CASE w.raw_status_code WHEN 1 THEN 'OK' WHEN 2 THEN 'ERROR' ELSE 'UNSET' END AS span_status,
      w.duplicate_count, w.trace_state,
      CASE
        WHEN JSON_VALUE(w.attributes, '$."gen_ai.input.messages_ref"') IS NOT NULL
          OR JSON_VALUE(w.attributes, '$."gen_ai.output.messages_ref"') IS NOT NULL THEN 'otel_ref'
        WHEN NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.input.messages"')), 'null') IS NOT NULL
          OR NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.output.messages"')), 'null') IS NOT NULL
          OR NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.tool.call.arguments"')), 'null') IS NOT NULL
          OR NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.tool.call.result"')), 'null') IS NOT NULL THEN 'otel_inline'
        WHEN NULLIF(JSON_VALUE(w.attributes, '$."gcp.vertex.agent.llm_request"'), '{}') IS NOT NULL
          OR NULLIF(JSON_VALUE(w.attributes, '$."gcp.vertex.agent.llm_response"'), '{}') IS NOT NULL
          OR JSON_VALUE(w.attributes, '$."gcp.vertex.agent.tool_call_args"') IS NOT NULL
          OR JSON_VALUE(w.attributes, '$."gcp.vertex.agent.tool_response"') IS NOT NULL THEN 'adk_legacy'
        ELSE CAST(NULL AS STRING)
      END AS raw_shape,
      (JSON_VALUE(w.attributes, '$."gen_ai.input.messages_ref"') IS NOT NULL OR JSON_VALUE(w.attributes, '$."gen_ai.output.messages_ref"') IS NOT NULL) AS has_msg_ref,
      (
        NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.input.messages"')), 'null') IS NOT NULL
        OR NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.output.messages"')), 'null') IS NOT NULL
        OR NULLIF(JSON_VALUE(w.attributes, '$."gcp.vertex.agent.llm_request"'), '{}') IS NOT NULL
        OR NULLIF(JSON_VALUE(w.attributes, '$."gcp.vertex.agent.llm_response"'), '{}') IS NOT NULL
      ) AS has_model_inline_content,
      (
        NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.tool.call.arguments"')), 'null') IS NOT NULL
        OR NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.tool.call.result"')), 'null') IS NOT NULL
        OR JSON_VALUE(w.attributes, '$."gcp.vertex.agent.tool_call_args"') IS NOT NULL
        OR JSON_VALUE(w.attributes, '$."gcp.vertex.agent.tool_response"') IS NOT NULL
        OR NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.retrieval.documents"')), 'null') IS NOT NULL
      ) AS has_tool_inline_content,
      JSON_VALUE(w.attributes, '$."gen_ai.operation.name"') AS operation_name,
      CASE
        WHEN JSON_VALUE(w.attributes, '$."gen_ai.operation.name"') IN ('chat', 'generate_content', 'call_llm') THEN 'model_call'
        WHEN JSON_VALUE(w.attributes, '$."gen_ai.operation.name"') = 'embeddings' THEN 'embedding'
        WHEN JSON_VALUE(w.attributes, '$."gen_ai.operation.name"') = 'execute_tool' THEN 'tool_call'
        WHEN JSON_VALUE(w.attributes, '$."gen_ai.operation.name"') = 'retrieval' THEN 'retrieval'
        WHEN JSON_VALUE(w.attributes, '$."gen_ai.operation.name"') = 'memory'
          OR STARTS_WITH(COALESCE(JSON_VALUE(w.attributes, '$."gen_ai.operation.name"'), ''), 'memory_') THEN 'memory'
        WHEN JSON_VALUE(w.attributes, '$."gen_ai.operation.name"') = 'invoke_agent' THEN 'agent'
        WHEN JSON_VALUE(w.attributes, '$."gen_ai.operation.name"') = 'invoke_workflow' THEN 'workflow'
        ELSE 'other'
      END AS span_kind,
      JSON_VALUE(w.resource_attributes, '$."apphub.application.id"') AS apphub_app_id,
      JSON_VALUE(w.resource_attributes, '$."service.name"') AS res_service_name,
      COALESCE(JSON_VALUE(w.resource_attributes, '$."deployment.environment.name"'), JSON_VALUE(w.resource_attributes, '$."deployment.environment"')) AS res_environment,
      JSON_VALUE(w.attributes, '$."bq_eval.caller_app_id"') AS caller_app_id,
      JSON_VALUE(w.attributes, '$."user.id"') AS user_id,
      JSON_VALUE(w.attributes, '$."bq_eval.tenant_id"') AS tenant_id,
      JSON_VALUE(w.attributes, '$."bq_eval.trigger_principal"') AS trigger_principal,
      JSON_VALUE(w.attributes, '$."bq_eval.agent_principal"') AS agent_principal,
      COALESCE(
        SAFE.PARSE_JSON(JSON_VALUE(w.attributes, '$."bq_eval.act_chain"')),
        SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."bq_eval.act_chain"')), 'null'))
      ) AS act_chain,
      JSON_VALUE(w.attributes, '$."bq_eval.auth_method"') AS auth_method,
      JSON_VALUE(w.attributes, '$."bq_eval.auth_issuer"') AS auth_issuer,
      JSON_VALUE(w.attributes, '$."bq_eval.auth_audience"') AS auth_audience,
      JSON_VALUE(w.attributes, '$."bq_eval.delegation_chain_hash"') AS delegation_chain_hash,
      JSON_VALUE(w.attributes, '$."bq_eval.authz_mode"') AS authz_mode,
      COALESCE(JSON_VALUE(w.attributes, '$."gen_ai.conversation.id"'), JSON_VALUE(w.attributes, '$."gcp.vertex.agent.session_id"')) AS raw_conversation_id,
      JSON_VALUE(w.attributes, '$."outcome.id"') AS outcome_id,
      JSON_VALUE(w.attributes, '$."outcome.owner"') AS outcome_owner,
      JSON_VALUE(w.attributes, '$."outcome.status"') AS outcome_status,
      JSON_VALUE(w.attributes, '$."outcome.subject"') AS outcome_subject,
      JSON_VALUE(w.attributes, '$."outcome.parent_id"') AS outcome_parent_id,
      JSON_VALUE(w.attributes, '$."bq_eval.client_request_id"') AS client_request_id,
      JSON_VALUE(w.attributes, '$."gen_ai.response.id"') AS response_id,
      COALESCE(JSON_VALUE(w.attributes, '$."bq_eval.previous_interaction_id"'), JSON_VALUE(w.attributes, '$."gen_ai.request.previous_interaction_id"')) AS previous_interaction_id,
      JSON_VALUE(w.attributes, '$."bq_eval.workspace_session_id"') AS workspace_session_id,
      JSON_VALUE(w.attributes, '$."bq_eval.shadow_of_trace_id"') AS shadow_of_trace_id,
      JSON_VALUE(w.attributes, '$."bq_eval.experiment_id"') AS experiment_id,
      JSON_VALUE(w.attributes, '$."bq_eval.variant_id"') AS variant_id,
      JSON_VALUE(w.attributes, '$."bq_eval.case_id"') AS case_id,
      JSON_VALUE(w.attributes, '$."bq_eval.eval_run_id"') AS eval_run_id,
      JSON_VALUE(w.attributes, '$."bq_eval.eval_split"') AS eval_split,
      JSON_VALUE(w.attributes, '$."mcp.session.id"') AS mcp_session_id,
      JSON_VALUE(w.attributes, '$."gen_ai.agent.id"') AS agent_id,
      JSON_VALUE(w.attributes, '$."gen_ai.agent.name"') AS agent_name,
      JSON_VALUE(w.attributes, '$."gen_ai.skill.name"') AS skill_name,
      JSON_VALUE(w.attributes, '$."bq_eval.skill_version"') AS skill_version,
      JSON_VALUE(w.attributes, '$."bq_eval.skill_sha256"') AS skill_sha256,
      JSON_VALUE(w.attributes, '$."bq_eval.protected_block_sha256"') AS protected_block_sha256,
      COALESCE(
        SAFE.PARSE_JSON(JSON_VALUE(w.attributes, '$."bq_eval.mounted_skills"')),
        SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."bq_eval.mounted_skills"')), 'null'))
      ) AS mounted_skills,
      JSON_VALUE(w.attributes, '$."gen_ai.request.model"') AS request_model,
      JSON_VALUE(w.attributes, '$."gen_ai.response.model"') AS response_model,
      COALESCE(JSON_VALUE(w.attributes, '$."gen_ai.provider.name"'), JSON_VALUE(w.attributes, '$."gen_ai.system"')) AS provider,
      JSON_VALUE(w.attributes, '$."bq_eval.upstream_endpoint_location"') AS upstream_endpoint_location,
      JSON_VALUE(w.attributes, '$."bq_eval.runtime_lane"') AS runtime_lane,
      JSON_VALUE(w.attributes, '$."bq_eval.state_mode"') AS state_mode,
      JSON_VALUE(w.attributes, '$."bq_eval.serving_tier"') AS serving_tier,
      COALESCE(JSON_VALUE(w.attributes, '$."bq_eval.traffic_type"'), JSON_VALUE(w.attributes, '$."finops.traffic_type"')) AS traffic_type,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.thought_signature_present"') AS BOOL) AS thought_signature_present,
      JSON_VALUE(w.attributes, '$."bq_eval.tool_definitions_sha256"') AS tool_definitions_sha256,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.available_tool_count"') AS INT64) AS available_tool_count,
      COALESCE(
        SAFE.PARSE_JSON(JSON_VALUE(w.attributes, '$."bq_eval.selected_tool_names"')),
        SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."bq_eval.selected_tool_names"')), 'null'))
      ) AS selected_tool_names,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.input_tokens"') AS INT64) AS input_tokens,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.output_tokens"') AS INT64) AS output_tokens,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.cache_read.input_tokens"') AS INT64) AS cache_read_tokens,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.cache_write.input_tokens"') AS INT64) AS cache_write_tokens,
      COALESCE(
        SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.reasoning.output_tokens"') AS INT64),
        SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.usage.thoughts_tokens"') AS INT64)
      ) AS reasoning_tokens,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."gen_ai.response.time_to_first_chunk"') AS FLOAT64) AS time_to_first_chunk,
      COALESCE(JSON_VALUE(w.attributes, '$."gen_ai.response.finish_reasons"[0]'), JSON_VALUE(w.attributes, '$."gen_ai.response.finish_reasons"')) AS raw_finish_reason,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."http.response.status_code"') AS INT64) AS http_status_code,
      JSON_VALUE(w.attributes, '$."error.type"') AS error_type,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.model_confidence"') AS FLOAT64) AS model_confidence,
      JSON_VALUE(w.attributes, '$."bq_eval.cascade_tier"') AS cascade_tier,
      JSON_VALUE(w.attributes, '$."bq_eval.escalation_reason"') AS escalation_reason,
      JSON_VALUE(w.attributes, '$."gen_ai.tool.name"') AS tool_name,
      JSON_VALUE(w.attributes, '$."gen_ai.tool.call.id"') AS tool_call_id,
      JSON_VALUE(w.attributes, '$."bq_eval.action_id"') AS action_id,
      JSON_VALUE(w.attributes, '$."bq_eval.idempotency_key"') AS idempotency_key,
      JSON_VALUE(w.attributes, '$."bq_eval.args_sha256"') AS emitted_args_sha256,
      JSON_VALUE(w.attributes, '$."gen_ai.data_source.id"') AS data_source_id,
      JSON_VALUE(w.attributes, '$."bq_eval.corpus_digest"') AS corpus_digest,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.acl_dropped_count"') AS INT64) AS acl_dropped_count,
      JSON_VALUE(w.attributes, '$."bq_eval.memory_op"') AS memory_op,
      JSON_VALUE(w.attributes, '$."bq_eval.memory_id"') AS memory_id,
      JSON_VALUE(w.attributes, '$."bq_eval.memory_sha256"') AS memory_sha256,
      JSON_VALUE(w.attributes, '$."bq_eval.memory_source_provenance"') AS memory_source_provenance,
      JSON_VALUE(w.attributes, '$."db.query.job_id"') AS job_id,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."db.query.bytes_scanned"') AS INT64) AS bytes_scanned,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."db.query.bytes_billed"') AS INT64) AS bytes_billed,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."db.query.slot_ms"') AS INT64) AS slot_ms,
      COALESCE(
        SAFE.PARSE_JSON(JSON_VALUE(w.attributes, '$."bq_eval.devai.diff_stats"')),
        SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."bq_eval.devai.diff_stats"')), 'null'))
      ) AS devai_diff_stats,
      COALESCE(
        SAFE.PARSE_JSON(JSON_VALUE(w.attributes, '$."bq_eval.devai.test_summary"')),
        SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."bq_eval.devai.test_summary"')), 'null'))
      ) AS devai_test_summary,
      COALESCE(
        SAFE.PARSE_JSON(JSON_VALUE(w.attributes, '$."bq_eval.devai.sast_findings"')),
        SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."bq_eval.devai.sast_findings"')), 'null'))
      ) AS devai_sast_findings,
      JSON_VALUE(w.attributes, '$."bq_eval.sandbox.status"') AS sandbox_status,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.sandbox.cpu_sec"') AS FLOAT64) AS sandbox_cpu_sec,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.sandbox.mem_gb_sec"') AS FLOAT64) AS sandbox_mem_gb_sec,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.document.page_count"') AS INT64) AS page_count,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.document.bytes"') AS INT64) AS document_bytes,
      JSON_VALUE(w.attributes, '$."bq_eval.attachment_sha256"') AS attachment_sha256,
      JSON_VALUE(w.attributes, '$."bq_eval.nudge_id"') AS nudge_id,
      CASE WHEN SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.app_failed"') AS BOOL) IS TRUE THEN TRUE ELSE CAST(NULL AS BOOL) END AS app_failed,
      JSON_VALUE(w.attributes, '$."bq_eval.app_failure_reason"') AS app_failure_reason,
      SAFE_CAST(JSON_VALUE(w.attributes, '$."bq_eval.sampling_probability"') AS FLOAT64) AS sampling_probability,
      JSON_STRIP_NULLS(JSON_OBJECT(
        'apphub_service_id', JSON_VALUE(w.resource_attributes, '$."apphub.service.id"'),
        'apphub_workload_id', JSON_VALUE(w.resource_attributes, '$."apphub.workload.id"'),
        'service_name', JSON_VALUE(w.resource_attributes, '$."service.name"'),
        'telemetry_environment', COALESCE(JSON_VALUE(w.resource_attributes, '$."deployment.environment.name"'), JSON_VALUE(w.resource_attributes, '$."deployment.environment"')),
        'cloud_provider', JSON_VALUE(w.resource_attributes, '$."cloud.provider"'),
        'cloud_region', JSON_VALUE(w.resource_attributes, '$."cloud.region"'),
        'cloud_account_id', JSON_VALUE(w.resource_attributes, '$."cloud.account.id"'),
        'agent_version', JSON_VALUE(w.attributes, '$."gen_ai.agent.version"'),
        'agent_description', JSON_VALUE(w.attributes, '$."gen_ai.agent.description"'),
        'workflow_name', JSON_VALUE(w.attributes, '$."gen_ai.workflow.name"'),
        'prompt_name', JSON_VALUE(w.attributes, '$."gen_ai.prompt.name"'),
        'prompt_version', JSON_VALUE(w.attributes, '$."gen_ai.prompt.version"'),
        'finish_reasons_all', SAFE.PARSE_JSON(NULLIF(TO_JSON_STRING(JSON_QUERY(w.attributes, '$."gen_ai.response.finish_reasons"')), 'null')),
        'tool_type', JSON_VALUE(w.attributes, '$."gen_ai.tool.type"'),
        'tool_description', JSON_VALUE(w.attributes, '$."gen_ai.tool.description"'),
        'mcp_method_name', JSON_VALUE(w.attributes, '$."mcp.method.name"'),
        'mcp_protocol_version', JSON_VALUE(w.attributes, '$."mcp.protocol.version"'),
        'mcp_resource_uri', JSON_VALUE(w.attributes, '$."mcp.resource.uri"'),
        'a2a_task_id', JSON_VALUE(w.attributes, '$."a2a.task.id"'),
        'a2a_task_state', JSON_VALUE(w.attributes, '$."a2a.task.state"'),
        'adk_invocation_id', JSON_VALUE(w.attributes, '$."gcp.vertex.agent.invocation_id"'),
        'adk_event_id', JSON_VALUE(w.attributes, '$."gcp.vertex.agent.event_id"'),
        'telemetry_sdk_name', JSON_VALUE(w.resource_attributes, '$."telemetry.sdk.name"'),
        'telemetry_sdk_version', JSON_VALUE(w.resource_attributes, '$."telemetry.sdk.version"')
      )) AS stripped_labels
    FROM in_window AS w
  ),
  parsed_anc1 AS (
    SELECT
      pr.*,
      STRING_AGG(IF(pr.has_tok AND pr.parent_span_id IS NOT NULL AND pr.parent_span_id != '', pr.parent_span_id, NULL), '|') OVER (PARTITION BY pr.trace_id) AS tok_p1,
      STRING_AGG(IF(pr.operation_name = 'generate_content' AND pr.parent_span_id IS NOT NULL AND pr.parent_span_id != '', pr.parent_span_id, NULL), '|') OVER (PARTITION BY pr.trace_id) AS gc_pids
    FROM parsed_raw AS pr
  ),
  parsed AS (
    SELECT
      pa.*,
      CASE
        WHEN pa.span_id IS NOT NULL AND pa.span_id != '' AND (
          STRPOS(CONCAT('|', COALESCE(pa.tok_p1, ''), '|', COALESCE(pa.tok_p2, ''), '|', COALESCE(pa.tok_p3, ''), '|', COALESCE(pa.tok_p4, ''), '|', COALESCE(pa.tok_p5, ''), '|', COALESCE(pa.tok_p6, ''), '|'), CONCAT('|', pa.span_id, '|')) > 0
          OR (pa.span_name = 'call_llm' AND STRPOS(CONCAT('|', COALESCE(pa.gc_pids, ''), '|'), CONCAT('|', pa.span_id, '|')) > 0)
        ) THEN FALSE
        WHEN pa.span_kind IN ('model_call', 'embedding') OR pa.has_tok THEN TRUE
        ELSE FALSE
      END AS is_model_call,
      CASE WHEN TO_JSON_STRING(pa.stripped_labels) = '{}' THEN CAST(NULL AS JSON) ELSE pa.stripped_labels END AS labels
    FROM (
      SELECT pa5.*, STRING_AGG(IF(pa5.span_id IS NOT NULL AND pa5.span_id != '' AND STRPOS(CONCAT('|', COALESCE(pa5.tok_p5, ''), '|'), CONCAT('|', pa5.span_id, '|')) > 0 AND pa5.parent_span_id IS NOT NULL AND pa5.parent_span_id != '', pa5.parent_span_id, NULL), '|') OVER (PARTITION BY pa5.trace_id) AS tok_p6
      FROM (
        SELECT pa4.*, STRING_AGG(IF(pa4.span_id IS NOT NULL AND pa4.span_id != '' AND STRPOS(CONCAT('|', COALESCE(pa4.tok_p4, ''), '|'), CONCAT('|', pa4.span_id, '|')) > 0 AND pa4.parent_span_id IS NOT NULL AND pa4.parent_span_id != '', pa4.parent_span_id, NULL), '|') OVER (PARTITION BY pa4.trace_id) AS tok_p5
        FROM (
          SELECT pa3.*, STRING_AGG(IF(pa3.span_id IS NOT NULL AND pa3.span_id != '' AND STRPOS(CONCAT('|', COALESCE(pa3.tok_p3, ''), '|'), CONCAT('|', pa3.span_id, '|')) > 0 AND pa3.parent_span_id IS NOT NULL AND pa3.parent_span_id != '', pa3.parent_span_id, NULL), '|') OVER (PARTITION BY pa3.trace_id) AS tok_p4
          FROM (
            SELECT pa2.*, STRING_AGG(IF(pa2.span_id IS NOT NULL AND pa2.span_id != '' AND STRPOS(CONCAT('|', COALESCE(pa2.tok_p2, ''), '|'), CONCAT('|', pa2.span_id, '|')) > 0 AND pa2.parent_span_id IS NOT NULL AND pa2.parent_span_id != '', pa2.parent_span_id, NULL), '|') OVER (PARTITION BY pa2.trace_id) AS tok_p3
            FROM (
              SELECT pa1.*, STRING_AGG(IF(pa1.span_id IS NOT NULL AND pa1.span_id != '' AND STRPOS(CONCAT('|', COALESCE(pa1.tok_p1, ''), '|'), CONCAT('|', pa1.span_id, '|')) > 0 AND pa1.parent_span_id IS NOT NULL AND pa1.parent_span_id != '', pa1.parent_span_id, NULL), '|') OVER (PARTITION BY pa1.trace_id) AS tok_p2
              FROM parsed_anc1 AS pa1
            ) AS pa2
          ) AS pa3
        ) AS pa4
      ) AS pa5
    ) AS pa
  ),
  with_app AS (
    SELECT
      f.*,
      r.app_id,
      CASE
        WHEN r.app_id IS NULL THEN CAST(NULL AS BOOL)
        ELSE COALESCE(f.landing_place IN UNNEST(JSON_VALUE_ARRAY(r.allowed_landing_places)), FALSE)
      END AS app_identity_bound,
      ROW_NUMBER() OVER (
        PARTITION BY f.trace_id, f.span_id
        ORDER BY
          CASE WHEN f.apphub_app_id IS NOT NULL AND r.apphub_application_id = f.apphub_app_id THEN 0 ELSE 1 END,
          CASE WHEN f.agent_id IS NOT NULL AND r.agent_id = f.agent_id THEN 0 WHEN r.agent_id IS NULL THEN 1 ELSE 2 END,
          r.effective_from DESC, r.app_id ASC
      ) AS match_rn
    FROM parsed AS f
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
      ON f.source_class IS NOT NULL
      AND r.effective_from <= f.start_time AND (r.effective_to IS NULL OR f.start_time < r.effective_to)
      AND (
        (f.apphub_app_id IS NOT NULL AND r.apphub_application_id = f.apphub_app_id)
        OR (f.res_service_name IS NOT NULL AND r.service_name = f.res_service_name AND (r.environment IS NULL AND f.res_environment IS NULL OR r.environment = f.res_environment))
      )
  ),
  by_trace AS (
    SELECT
      f.trace_id,
      MAX(CASE WHEN f.has_msg_ref THEN 1 ELSE 0 END) = 1 AS trace_has_shape3_ref,
      ARRAY_AGG(
        IF(
          f.span_name = 'call_llm',
          STRUCT(f.span_id, f.raw_shape, f.raw_conversation_id, f.raw_finish_reason, f.has_model_inline_content),
          NULL
        ) IGNORE NULLS
      ) AS call_llm_arr,
      ARRAY_AGG(
        STRUCT(
          f.span_id, f.parent_span_id, f.span_name, f.otel_kind, f.start_time, f.end_time, f.receive_time,
          f.duration_ms, f.span_status, f.duplicate_count, f.trace_state, f.raw_shape, f.raw_conversation_id,
          f.raw_finish_reason, f.has_msg_ref, f.has_model_inline_content, f.has_tool_inline_content,
          f.span_kind, f.is_model_call, f.operation_name, f.source_class, f.app_id, f.app_identity_bound,
          f.caller_app_id, f.user_id, f.tenant_id, f.trigger_principal, f.agent_principal, f.act_chain,
          f.auth_method, f.auth_issuer, f.auth_audience, f.delegation_chain_hash, f.authz_mode,
          f.outcome_id, f.outcome_owner, f.outcome_status, f.outcome_subject, f.outcome_parent_id,
          f.client_request_id, f.response_id, f.previous_interaction_id, f.workspace_session_id,
          f.shadow_of_trace_id, f.experiment_id, f.variant_id, f.case_id, f.eval_run_id, f.eval_split,
          f.mcp_session_id, f.agent_id, f.agent_name, f.skill_name, f.skill_version, f.skill_sha256,
          f.protected_block_sha256, f.mounted_skills, f.request_model, f.response_model, f.provider,
          f.upstream_endpoint_location, f.runtime_lane, f.state_mode, f.serving_tier, f.traffic_type,
          f.thought_signature_present, f.tool_definitions_sha256, f.available_tool_count, f.selected_tool_names,
          f.input_tokens, f.output_tokens, f.cache_read_tokens, f.cache_write_tokens, f.reasoning_tokens,
          f.time_to_first_chunk, f.http_status_code, f.error_type, f.model_confidence, f.cascade_tier,
          f.escalation_reason, f.tool_name, f.tool_call_id, f.action_id, f.idempotency_key, f.emitted_args_sha256,
          f.data_source_id, f.corpus_digest, f.acl_dropped_count, f.memory_op, f.memory_id, f.memory_sha256,
          f.memory_source_provenance, f.job_id, f.bytes_scanned, f.bytes_billed, f.slot_ms, f.devai_diff_stats,
          f.devai_test_summary, f.devai_sast_findings, f.sandbox_status, f.sandbox_cpu_sec, f.sandbox_mem_gb_sec,
          f.page_count, f.document_bytes, f.attachment_sha256, f.nudge_id, f.app_failed, f.app_failure_reason,
          f.sampling_probability, f.labels
        )
      ) AS spans_arr
    FROM with_app AS f
    WHERE f.match_rn = 1
    GROUP BY f.trace_id
  )
  SELECT
    bt.trace_id, p.span_id, p.parent_span_id, p.span_name, p.otel_kind, p.start_time, p.end_time, p.receive_time,
    p.duration_ms, p.span_status, p.duplicate_count, p.trace_state,
    CASE WHEN p.operation_name = 'generate_content' AND ap.span_id IS NOT NULL THEN COALESCE(p.raw_shape, ap.raw_shape) ELSE p.raw_shape END AS shape,
    p.span_kind, p.is_model_call,
    CASE
      WHEN p.span_kind = 'embedding' THEN FALSE
      WHEN p.is_model_call THEN NOT (
        (CASE WHEN p.operation_name = 'generate_content' AND ap.span_id IS NOT NULL THEN (p.has_model_inline_content OR COALESCE(ap.has_model_inline_content, FALSE)) ELSE p.has_model_inline_content END)
        OR p.has_msg_ref OR p.has_tool_inline_content
      )
      WHEN p.span_kind IN ('tool_call', 'retrieval', 'memory') THEN NOT (p.has_tool_inline_content OR bt.trace_has_shape3_ref)
      ELSE FALSE
    END AS content_missing,
    p.operation_name, p.source_class, p.app_id, p.app_identity_bound, p.caller_app_id, p.user_id, p.tenant_id,
    p.trigger_principal, p.agent_principal, p.act_chain, p.auth_method, p.auth_issuer, p.auth_audience,
    p.delegation_chain_hash, p.authz_mode,
    CASE WHEN p.operation_name = 'generate_content' AND ap.span_id IS NOT NULL THEN COALESCE(p.raw_conversation_id, ap.raw_conversation_id) ELSE p.raw_conversation_id END AS conversation_id,
    p.outcome_id, p.outcome_owner, p.outcome_status, p.outcome_subject, p.outcome_parent_id, p.client_request_id,
    p.response_id, p.previous_interaction_id, p.workspace_session_id, p.shadow_of_trace_id, p.experiment_id,
    p.variant_id, p.case_id, p.eval_run_id, p.eval_split, p.mcp_session_id, p.agent_id, p.agent_name,
    p.skill_name, p.skill_version, p.skill_sha256, p.protected_block_sha256, p.mounted_skills, p.request_model,
    p.response_model, p.provider, p.upstream_endpoint_location, p.runtime_lane, p.state_mode, p.serving_tier,
    p.traffic_type, p.thought_signature_present, p.tool_definitions_sha256, p.available_tool_count,
    p.selected_tool_names, p.input_tokens, p.output_tokens, p.cache_read_tokens, p.cache_write_tokens,
    p.reasoning_tokens, p.time_to_first_chunk,
    CASE WHEN p.operation_name = 'generate_content' AND ap.span_id IS NOT NULL THEN COALESCE(p.raw_finish_reason, ap.raw_finish_reason) ELSE p.raw_finish_reason END AS finish_reason,
    p.http_status_code, p.error_type, p.model_confidence, p.cascade_tier, p.escalation_reason, p.tool_name,
    p.tool_call_id, p.action_id, p.idempotency_key, p.emitted_args_sha256, p.data_source_id, p.corpus_digest,
    p.acl_dropped_count, p.memory_op, p.memory_id, p.memory_sha256, p.memory_source_provenance, p.job_id,
    p.bytes_scanned, p.bytes_billed, p.slot_ms, p.devai_diff_stats, p.devai_test_summary, p.devai_sast_findings,
    p.sandbox_status, p.sandbox_cpu_sec, p.sandbox_mem_gb_sec, p.page_count, p.document_bytes, p.attachment_sha256,
    p.nudge_id, p.app_failed, p.app_failure_reason, p.sampling_probability, p.labels
  FROM by_trace AS bt
  CROSS JOIN UNNEST(bt.spans_arr) AS p
  LEFT JOIN UNNEST(bt.call_llm_arr) AS ap ON ap.span_id = p.parent_span_id
);
