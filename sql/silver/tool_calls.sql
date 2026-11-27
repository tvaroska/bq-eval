-- Silver Tool Calls (`tool_calls_base`, `silver_open.tool_calls_open`, `silver_restricted.tool_calls`).
-- Implements W3C-chained server-into-client folding lifting proxy/server attributes (`ADR-025`, `ADR-034`),
-- SCD-2 `tools` flags (`ADR-019`), four RFC 8785 hash sources & `repeat_number` (`ADR-038`, `ADR-041`),
-- Shape-3 Hive `dt`/`hr` static-pruned message fallback (`ADR-027`), and `ADR-044`/`ADR-047` caller filtering.

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.tool_calls_base`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH base_spans AS (
    SELECT *
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.spans`(from_time, to_time)
  ),
  raw_inline AS (
    SELECT
      trace_id,
      span_id,
      COALESCE(
        JSON_VALUE(attributes, '$."gen_ai.tool.call.arguments"'),
        NULLIF(TO_JSON_STRING(JSON_QUERY(attributes, '$."gen_ai.tool.call.arguments"')), 'null'),
        JSON_VALUE(attributes, '$."gcp.vertex.agent.tool_call_args"')
      ) AS inline_arguments,
      COALESCE(
        JSON_VALUE(attributes, '$."gen_ai.tool.call.result"'),
        NULLIF(TO_JSON_STRING(JSON_QUERY(attributes, '$."gen_ai.tool.call.result"')), 'null'),
        JSON_VALUE(attributes, '$."gcp.vertex.agent.tool_response"'),
        NULLIF(TO_JSON_STRING(JSON_QUERY(attributes, '$."gen_ai.retrieval.documents"')), 'null')
      ) AS inline_results,
      ROW_NUMBER() OVER (
        PARTITION BY trace_id, span_id
        ORDER BY src_prio ASC, receive_time DESC, end_time DESC
      ) AS rn
    FROM (
      SELECT 1 AS src_prio, trace_id, span_id, receive_time, end_time, attributes
      FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans`
      WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      UNION ALL
      SELECT 0 AS src_prio, trace_id, span_id, receive_time, end_time, attributes
      FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}`
      WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    )
  ),
  -- Per-attribute gateway -> root -> earliest precedence (avoids single-row platform shadowing).
  trace_meta AS (
    SELECT
      s.trace_id,
      MIN(s.start_time) AS trace_start_time,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.app_id END) AS root_app_id,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.user_id END) AS root_user_id,
      MAX(CASE WHEN s.root_rn = 1 AND s.source_class IS NOT NULL THEN s.tenant_id END) AS root_tenant_id,
      MAX(CASE WHEN s.source_class = 'platform' AND s.caller_app_id IS NOT NULL THEN 1 ELSE 0 END) = 1 AS has_gw_caller,
      ARRAY_AGG(CASE WHEN s.source_class = 'platform' THEN s.caller_app_id END IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_caller_app_id,
      ARRAY_AGG(CASE WHEN s.source_class = 'platform' AND s.caller_app_id IS NOT NULL THEN s.start_time END IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_start_time,
      ARRAY_AGG(CASE WHEN s.source_class = 'platform' THEN s.tenant_id END IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS gw_tenant_id,
      ARRAY_AGG(s.outcome_id IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS earliest_outcome_id,
      ARRAY_AGG(s.tenant_id IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS earliest_tenant_id
    FROM (
      SELECT
        trace_id, span_id, start_time, source_class, app_id, caller_app_id, user_id, tenant_id, outcome_id,
        ROW_NUMBER() OVER (
          PARTITION BY trace_id
          ORDER BY CASE WHEN source_class IS NOT NULL THEN 0 ELSE 1 END, start_time ASC, span_id ASC
        ) AS root_rn
      FROM base_spans
    ) AS s
    GROUP BY s.trace_id
  ),
  trace_owner AS (
    SELECT
      tm.trace_id,
      CASE WHEN tm.has_gw_caller THEN gw_r.app_id ELSE tm.root_app_id END AS trace_owner_app_id,
      tm.root_user_id AS trace_user_id,
      COALESCE(tm.gw_tenant_id, tm.root_tenant_id, tm.earliest_tenant_id) AS trace_tenant_id,
      tm.earliest_outcome_id AS trace_outcome_id,
      tm.trace_start_time,
      ROW_NUMBER() OVER (PARTITION BY tm.trace_id ORDER BY gw_r.effective_from DESC) AS gw_reg_rn
    FROM trace_meta AS tm
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS gw_r
      ON tm.gw_caller_app_id IS NOT NULL
      AND gw_r.app_id = tm.gw_caller_app_id
      AND gw_r.effective_from <= tm.gw_start_time
      AND (gw_r.effective_to IS NULL OR tm.gw_start_time < gw_r.effective_to)
  ),
  owner_bu AS (
    SELECT
      o.trace_id, o.trace_owner_app_id, o.trace_user_id, o.trace_tenant_id, o.trace_outcome_id,
      r.business_unit,
      ROW_NUMBER() OVER (PARTITION BY o.trace_id ORDER BY r.effective_from DESC) AS bu_rn
    FROM trace_owner AS o
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
      ON r.app_id = o.trace_owner_app_id
      AND r.effective_from <= o.trace_start_time
      AND (r.effective_to IS NULL OR o.trace_start_time < r.effective_to)
    WHERE o.gw_reg_rn = 1
  ),
  -- Enrich tool spans with inline payloads and 1-hop parent before server-into-client folding.
  tool_spans AS (
    SELECT
      s.*,
      p1.parent_span_id AS p2_id,
      p1.span_kind AS p1_span_kind,
      p1.agent_id AS p1_agent_id,
      p1.agent_name AS p1_agent_name,
      p1.skill_name AS p1_skill_name,
      p1.skill_version AS p1_skill_version,
      p1.skill_sha256 AS p1_skill_sha256,
      ri.inline_arguments,
      ri.inline_results
    FROM base_spans AS s
    LEFT JOIN raw_inline AS ri ON ri.trace_id = s.trace_id AND ri.span_id = s.span_id AND ri.rn = 1
    LEFT JOIN base_spans AS p1 ON p1.trace_id = s.trace_id AND p1.span_id = s.parent_span_id
    WHERE s.span_kind IN ('tool_call', 'retrieval', 'memory')
  ),
  -- Server-into-client fold strictly via W3C parent chain (never name/args), lifting proxy hashes & server metrics.
  folded_tools AS (
    SELECT
      s.*,
      CASE WHEN s.otel_kind = 'SERVER' AND peer.span_id IS NOT NULL THEN 1 ELSE 0 END AS is_folded_server,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' AND peer.span_status = 'ERROR' THEN 1 ELSE 0 END AS has_srv_error,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' AND peer.source_class = 'platform' THEN peer.emitted_args_sha256 END AS srv_proxy_sha,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.authz_mode END AS srv_authz_mode,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.job_id END AS srv_job_id,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.bytes_scanned END AS srv_bytes_scanned,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.bytes_billed END AS srv_bytes_billed,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.slot_ms END AS srv_slot_ms,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.acl_dropped_count END AS srv_acl_dropped_count,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.corpus_digest END AS srv_corpus_digest,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.sandbox_cpu_sec END AS srv_sandbox_cpu_sec,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.sandbox_mem_gb_sec END AS srv_sandbox_mem_gb_sec,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.sandbox_status END AS srv_sandbox_status,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.inline_arguments END AS srv_inline_arguments,
      CASE WHEN COALESCE(s.otel_kind, '') != 'SERVER' THEN peer.inline_results END AS srv_inline_results,
      ROW_NUMBER() OVER (
        PARTITION BY s.trace_id, s.span_id
        ORDER BY
          CASE WHEN peer.source_class = 'platform' THEN 0 ELSE 1 END ASC,
          CASE WHEN peer.span_status = 'ERROR' THEN 0 ELSE 1 END ASC,
          peer.span_id ASC
      ) AS peer_rn
    FROM tool_spans AS s
    LEFT JOIN tool_spans AS peer
      ON peer.trace_id = s.trace_id
      AND peer.span_id != s.span_id
      AND (
        (
          s.otel_kind = 'SERVER'
          AND COALESCE(peer.otel_kind, '') != 'SERVER'
          AND (
            s.parent_span_id = peer.span_id
            OR (s.mcp_session_id IS NOT NULL AND s.mcp_session_id = peer.mcp_session_id AND peer.span_id = s.p2_id)
          )
        )
        OR (
          COALESCE(s.otel_kind, '') != 'SERVER'
          AND peer.otel_kind = 'SERVER'
          AND (
            peer.parent_span_id = s.span_id
            OR (peer.mcp_session_id IS NOT NULL AND peer.mcp_session_id = s.mcp_session_id AND s.span_id = peer.p2_id)
          )
        )
      )
  ),
  kept_tools AS (
    SELECT
      s.*,
      COALESCE(s.agent_id, CASE WHEN s.p1_span_kind = 'agent' THEN s.p1_agent_id END, CASE WHEN p2.span_kind = 'agent' THEN p2.agent_id END, s.p1_agent_id, p2.agent_id) AS resolved_agent_id,
      COALESCE(s.agent_name, CASE WHEN s.p1_span_kind = 'agent' THEN s.p1_agent_name END, CASE WHEN p2.span_kind = 'agent' THEN p2.agent_name END, s.p1_agent_name, p2.agent_name) AS resolved_agent_name,
      COALESCE(s.skill_name, CASE WHEN s.p1_span_kind = 'agent' THEN s.p1_skill_name END, CASE WHEN p2.span_kind = 'agent' THEN p2.skill_name END, s.p1_skill_name, p2.skill_name) AS resolved_skill_name,
      COALESCE(s.skill_version, CASE WHEN s.p1_span_kind = 'agent' THEN s.p1_skill_version END, CASE WHEN p2.span_kind = 'agent' THEN p2.skill_version END, s.p1_skill_version, p2.skill_version) AS resolved_skill_version,
      COALESCE(s.skill_sha256, CASE WHEN s.p1_span_kind = 'agent' THEN s.p1_skill_sha256 END, CASE WHEN p2.span_kind = 'agent' THEN p2.skill_sha256 END, s.p1_skill_sha256, p2.skill_sha256) AS resolved_skill_sha256,
      ob.trace_owner_app_id,
      ob.trace_user_id,
      ob.trace_tenant_id,
      ob.trace_outcome_id,
      ob.business_unit,
      COALESCE(s.inline_arguments, s.srv_inline_arguments) AS eff_inline_arguments,
      COALESCE(s.inline_results, s.srv_inline_results) AS eff_inline_results,
      CASE WHEN s.span_status = 'ERROR' OR s.has_srv_error = 1 THEN 'ERROR' ELSE s.span_status END AS effective_span_status,
      `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_tool_args_sha256`(s.tool_name, COALESCE(s.inline_arguments, s.srv_inline_arguments)) AS silver_jcs_sha256
    FROM folded_tools AS s
    LEFT JOIN base_spans AS p2 ON p2.trace_id = s.trace_id AND p2.span_id = s.p2_id
    LEFT JOIN owner_bu AS ob ON ob.trace_id = s.trace_id AND ob.bu_rn = 1
    WHERE s.peer_rn = 1
      AND s.is_folded_server = 0
  ),
  hashed AS (
    SELECT
      k.*,
      CASE
        WHEN (k.source_class = 'platform' AND k.emitted_args_sha256 IS NOT NULL) OR k.srv_proxy_sha IS NOT NULL THEN 'proxy'
        WHEN k.silver_jcs_sha256 IS NOT NULL THEN 'silver'
        WHEN k.source_class = 'application' AND k.emitted_args_sha256 IS NOT NULL THEN 'application'
        ELSE 'none'
      END AS args_hash_source,
      CASE
        WHEN k.source_class = 'platform' AND k.emitted_args_sha256 IS NOT NULL THEN k.emitted_args_sha256
        WHEN k.srv_proxy_sha IS NOT NULL THEN k.srv_proxy_sha
        WHEN k.silver_jcs_sha256 IS NOT NULL THEN k.silver_jcs_sha256
        WHEN k.source_class = 'application' AND k.emitted_args_sha256 IS NOT NULL THEN k.emitted_args_sha256
        ELSE CAST(NULL AS STRING)
      END AS args_sha256
    FROM kept_tools AS k
  ),
  with_first_key AS (
    SELECT
      h.*,
      CASE
        WHEN h.args_sha256 IS NOT NULL THEN
          MIN(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S', h.start_time), '|', h.span_id)) OVER (
            PARTITION BY h.trace_id, COALESCE(h.tool_name, ''), h.args_sha256
          )
      END AS first_seen_key
    FROM hashed AS h
  ),
  with_repeat AS (
    SELECT
      f.*,
      CASE
        WHEN f.first_seen_key IS NOT NULL THEN
          DENSE_RANK() OVER (
            PARTITION BY f.trace_id, CASE WHEN f.first_seen_key IS NOT NULL THEN 1 ELSE 0 END
            ORDER BY f.first_seen_key ASC
          )
      END AS repeat_number
    FROM with_first_key AS f
  ),
  with_tool_ref AS (
    SELECT
      h.*,
      tr.writes_state, tr.modifies_workspace, tr.executes_code, tr.sends_external_reply, tr.returns_pii, tr.creates_ticket,
      ROW_NUMBER() OVER (PARTITION BY h.trace_id, h.span_id ORDER BY tr.effective_from DESC) AS tr_rn
    FROM with_repeat AS h
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.tools` AS tr
      ON tr.app_id = h.trace_owner_app_id
      AND tr.tool_name = h.tool_name
      AND tr.effective_from <= h.start_time
      AND (tr.effective_to IS NULL OR h.start_time < tr.effective_to)
  )
  SELECT
    h.trace_id, h.span_id, h.parent_span_id,
    COALESCE(h.outcome_id, h.trace_outcome_id) AS outcome_id,
    h.trace_owner_app_id, h.tool_call_id,
    COALESCE(h.operation_name, 'execute_tool') AS operation_name,
    h.start_time, h.end_time, h.duration_ms,
    h.resolved_agent_id AS agent_id, h.resolved_agent_name AS agent_name,
    h.resolved_skill_name AS skill_name, h.resolved_skill_version AS skill_version, h.resolved_skill_sha256 AS skill_sha256,
    h.tool_name, h.effective_span_status AS span_status, h.idempotency_key, h.action_id,
    h.args_sha256, h.args_hash_source,
    CASE WHEN h.args_hash_source = 'proxy' THEN TRUE WHEN h.args_hash_source IN ('silver', 'application') THEN FALSE ELSE CAST(NULL AS BOOL) END AS args_hash_attested,
    COALESCE(h.authz_mode, h.srv_authz_mode) AS authz_mode,
    h.data_source_id,
    COALESCE(h.corpus_digest, h.srv_corpus_digest) AS corpus_digest,
    COALESCE(h.acl_dropped_count, h.srv_acl_dropped_count) AS acl_dropped_count,
    h.memory_op, h.memory_id, h.memory_sha256, h.memory_source_provenance,
    COALESCE(h.job_id, h.srv_job_id) AS job_id,
    COALESCE(h.bytes_scanned, h.srv_bytes_scanned) AS bytes_scanned,
    COALESCE(h.bytes_billed, h.srv_bytes_billed) AS bytes_billed,
    COALESCE(h.slot_ms, h.srv_slot_ms) AS slot_ms,
    h.devai_diff_stats, h.devai_test_summary, h.devai_sast_findings,
    COALESCE(h.sandbox_cpu_sec, h.srv_sandbox_cpu_sec) AS sandbox_cpu_sec,
    COALESCE(h.sandbox_mem_gb_sec, h.srv_sandbox_mem_gb_sec) AS sandbox_mem_gb_sec,
    COALESCE(h.sandbox_status, h.srv_sandbox_status) AS sandbox_status,
    h.eff_inline_arguments AS inline_arguments,
    h.eff_inline_results AS inline_results,
    (h.base_content_missing AND h.eff_inline_arguments IS NULL AND h.eff_inline_results IS NULL) AS base_content_missing,
    h.is_model_call, h.repeat_number,
    h.writes_state, h.modifies_workspace, h.executes_code, h.sends_external_reply, h.returns_pii, h.creates_ticket,
    h.trace_user_id AS user_id, h.trace_tenant_id AS tenant_id, h.business_unit,
    JSON_STRIP_NULLS(JSON_OBJECT(
      'service_name', JSON_VALUE(h.labels, '$.service_name'),
      'agent_version', JSON_VALUE(h.labels, '$.agent_version'),
      'prompt_name', JSON_VALUE(h.labels, '$.prompt_name'),
      'prompt_version', JSON_VALUE(h.labels, '$.prompt_version'),
      'tool_type', JSON_VALUE(h.labels, '$.tool_type'),
      'tool_description', JSON_VALUE(h.labels, '$.tool_description'),
      'tool_http_status_code', h.http_status_code,
      'tool_error_type', h.error_type,
      'mcp_method_name', JSON_VALUE(h.labels, '$.mcp_method_name'),
      'mcp_protocol_version', JSON_VALUE(h.labels, '$.mcp_protocol_version'),
      'mcp_resource_uri', JSON_VALUE(h.labels, '$.mcp_resource_uri'),
      'a2a_task_id', JSON_VALUE(h.labels, '$.a2a_task_id'),
      'a2a_task_state', JSON_VALUE(h.labels, '$.a2a_task_state'),
      'adk_event_id', JSON_VALUE(h.labels, '$.adk_event_id')
    )) AS restricted_labels,
    JSON_STRIP_NULLS(JSON_OBJECT(
      'service_name', JSON_VALUE(h.labels, '$.service_name'),
      'agent_version', JSON_VALUE(h.labels, '$.agent_version'),
      'prompt_name', JSON_VALUE(h.labels, '$.prompt_name'),
      'prompt_version', JSON_VALUE(h.labels, '$.prompt_version'),
      'tool_type', JSON_VALUE(h.labels, '$.tool_type'),
      'tool_http_status_code', h.http_status_code,
      'tool_error_type', h.error_type,
      'mcp_method_name', JSON_VALUE(h.labels, '$.mcp_method_name'),
      'mcp_protocol_version', JSON_VALUE(h.labels, '$.mcp_protocol_version'),
      'a2a_task_id', JSON_VALUE(h.labels, '$.a2a_task_id'),
      'a2a_task_state', JSON_VALUE(h.labels, '$.a2a_task_state'),
      'adk_event_id', JSON_VALUE(h.labels, '$.adk_event_id')
    )) AS open_labels
  FROM (SELECT r.*, r.content_missing AS base_content_missing FROM with_tool_ref AS r) AS h
  WHERE h.tr_rn = 1
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_OPEN_DATASET}.tool_calls_open`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  SELECT
    trace_id, span_id, parent_span_id, outcome_id, trace_owner_app_id, tool_call_id, operation_name,
    start_time, end_time, duration_ms, agent_id, agent_name, skill_name, skill_version, skill_sha256,
    tool_name, span_status, action_id, args_hash_source, args_hash_attested, authz_mode, data_source_id,
    acl_dropped_count, memory_op, job_id, bytes_scanned, bytes_billed, slot_ms, sandbox_cpu_sec,
    sandbox_mem_gb_sec, sandbox_status, base_content_missing AS content_missing, repeat_number,
    writes_state, modifies_workspace, executes_code, sends_external_reply, returns_pii, creates_ticket,
    CASE WHEN TO_JSON_STRING(open_labels) = '{}' THEN CAST(NULL AS JSON) ELSE open_labels END AS labels
  FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.tool_calls_base`(from_time, to_time)
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.tool_calls`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH base_tools AS (
    SELECT * FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.tool_calls_base`(from_time, to_time)
  ),
  trace_refs AS (
    SELECT DISTINCT r.trace_id, ref_uri
    FROM (
      SELECT trace_id, JSON_VALUE(attributes, '$."gen_ai.input.messages_ref"') AS in_ref, JSON_VALUE(attributes, '$."gen_ai.output.messages_ref"') AS out_ref
      FROM `${PROJECT_ID}.${TRACES_DATASET_ID}._AllSpans`
      WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      UNION ALL
      SELECT trace_id, JSON_VALUE(attributes, '$."gen_ai.input.messages_ref"') AS in_ref, JSON_VALUE(attributes, '$."gen_ai.output.messages_ref"') AS out_ref
      FROM `${PROJECT_ID}.${PLATFORM_TRACES_DATASET_ID}.${PLATFORM_TRACES_TABLE_ID}`
      WHERE start_time >= TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
        AND start_time < TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    ) AS r
    CROSS JOIN UNNEST(JSON_VALUE_ARRAY(JSON_ARRAY(r.in_ref, r.out_ref))) AS ref_uri
    WHERE ref_uri IS NOT NULL AND ref_uri != ''
  ),
  shape3_parts AS (
    SELECT
      tr.trace_id,
      JSON_VALUE(part, '$.id') AS call_id,
      MAX(CASE WHEN JSON_VALUE(part, '$.type') = 'tool_call' THEN NULLIF(TO_JSON_STRING(JSON_QUERY(part, '$.arguments')), 'null') END) AS ref_arguments,
      MAX(CASE WHEN JSON_VALUE(part, '$.type') = 'tool_call_response' THEN NULLIF(TO_JSON_STRING(JSON_QUERY(part, '$.response')), 'null') END) AS ref_results
    FROM trace_refs AS tr
    JOIN `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.shape3_payloads` AS sp
      ON sp.ref_uri = tr.ref_uri
      AND sp.dt >= DATE(TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE))
      AND sp.dt <= DATE(TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE))
      AND (sp.dt > DATE(TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)) OR sp.hr >= FORMAT_TIMESTAMP('%H', TIMESTAMP_SUB(from_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)))
      AND (sp.dt < DATE(TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)) OR sp.hr <= FORMAT_TIMESTAMP('%H', TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)))
    CROSS JOIN UNNEST(JSON_QUERY_ARRAY(sp.payload)) AS msg
    CROSS JOIN UNNEST(JSON_QUERY_ARRAY(msg, '$.parts')) AS part
    WHERE JSON_VALUE(part, '$.id') IS NOT NULL
    GROUP BY tr.trace_id, call_id
  )
  SELECT
    b.trace_id, b.span_id, b.parent_span_id, b.outcome_id, b.trace_owner_app_id, b.tool_call_id, b.operation_name,
    b.start_time, b.end_time, b.duration_ms, b.agent_id, b.agent_name, b.skill_name, b.skill_version, b.skill_sha256,
    b.tool_name, b.span_status, b.idempotency_key, b.action_id, b.args_sha256, b.args_hash_source, b.args_hash_attested,
    b.authz_mode, b.data_source_id, b.corpus_digest, b.acl_dropped_count, b.memory_op, b.memory_id, b.memory_sha256,
    b.memory_source_provenance, b.job_id, b.bytes_scanned, b.bytes_billed, b.slot_ms, b.devai_diff_stats,
    b.devai_test_summary, b.devai_sast_findings, b.sandbox_cpu_sec, b.sandbox_mem_gb_sec, b.sandbox_status,
    COALESCE(b.inline_arguments, s3.ref_arguments) AS arguments,
    COALESCE(b.inline_results, s3.ref_results) AS results,
    (COALESCE(b.inline_arguments, s3.ref_arguments) IS NULL AND COALESCE(b.inline_results, s3.ref_results) IS NULL) AS content_missing,
    b.repeat_number, b.writes_state, b.modifies_workspace, b.executes_code, b.sends_external_reply, b.returns_pii, b.creates_ticket,
    b.user_id, b.tenant_id,
    CASE WHEN TO_JSON_STRING(b.restricted_labels) = '{}' THEN CAST(NULL AS JSON) ELSE b.restricted_labels END AS labels
  FROM base_tools AS b
  LEFT JOIN shape3_parts AS s3 ON s3.trace_id = b.trace_id AND s3.call_id = b.tool_call_id
  WHERE EXISTS (
    SELECT 1
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.unit_entitlements` AS ue
    WHERE LOWER(ue.principal_email) = LOWER(SESSION_USER())
      AND ue.business_unit = COALESCE(b.business_unit, '__unowned__')
      AND (ue.tenant_id = '*' OR ue.tenant_id = b.tenant_id)
  )
);
