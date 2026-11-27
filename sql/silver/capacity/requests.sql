-- Silver Capacity Extension `requests` Table Function (`silver_open`, `M19-0`, `ADR-028`, `ADR-032`, `ADR-048`, `ADR-049`).
-- One row per upstream gateway attempt from `_AllLogs` (`inference_request`), keyed by `(client_request_id, span_id, attempt_index)`,
-- resolving caller `trace_owner_app_id` via SCD-2 `app_registry` and enforcing honest 3P `inferred_ambiguous` tiering via `capacity_pools`.

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_OPEN_DATASET}.requests`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH raw_logs AS (
    SELECT
      l.insert_id,
      l.timestamp AS request_time,
      l.receive_timestamp,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.client_request_id'),
        JSON_VALUE(l.json_payload, '$."x-client-request-id"'),
        JSON_VALUE(l.labels, '$.client_request_id'),
        JSON_VALUE(l.labels, '$."x-client-request-id"')
      ) AS client_request_id,
      COALESCE(
        NULLIF(REGEXP_REPLACE(COALESCE(l.trace, ''), r'^projects/[^/]+/traces/', ''), ''),
        JSON_VALUE(l.json_payload, '$.trace_id'),
        JSON_VALUE(l.labels, '$.trace_id')
      ) AS trace_id,
      COALESCE(
        NULLIF(l.span_id, ''),
        JSON_VALUE(l.json_payload, '$.span_id'),
        JSON_VALUE(l.labels, '$.span_id')
      ) AS span_id,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.response_id'),
        JSON_VALUE(l.json_payload, '$."gen_ai.response.id"'),
        JSON_VALUE(l.labels, '$."gen_ai.response.id"')
      ) AS response_id,
      COALESCE(
        SAFE_CAST(JSON_VALUE(l.json_payload, '$.attempt_index') AS INT64),
        SAFE_CAST(JSON_VALUE(l.labels, '$.attempt_index') AS INT64),
        1
      ) AS attempt_index,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.caller_app_id'),
        JSON_VALUE(l.json_payload, '$.trace_owner_app_id'),
        JSON_VALUE(l.labels, '$."bq_eval.caller_app_id"')
      ) AS raw_caller_app_id,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.billing_project_id'),
        JSON_VALUE(l.labels, '$.billing_project_id')
      ) AS billing_project_id,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.provider'),
        JSON_VALUE(l.labels, '$."gen_ai.provider.name"')
      ) AS provider,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.request_model'),
        JSON_VALUE(l.json_payload, '$.model'),
        JSON_VALUE(l.labels, '$."gen_ai.request.model"')
      ) AS request_model,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.response_model'),
        JSON_VALUE(l.labels, '$."gen_ai.response.model"')
      ) AS response_model,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.upstream_endpoint_location'),
        JSON_VALUE(l.labels, '$."bq_eval.upstream_endpoint_location"')
      ) AS upstream_endpoint_location,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.state_mode'),
        JSON_VALUE(l.labels, '$."bq_eval.state_mode"')
      ) AS state_mode,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.pool_id'),
        JSON_VALUE(l.labels, '$.pool_id')
      ) AS pool_id,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.runtime_pool_id'),
        JSON_VALUE(l.labels, '$.runtime_pool_id')
      ) AS raw_runtime_pool_id,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.serving_tier'),
        JSON_VALUE(l.json_payload, '$.serving_traffic_type'),
        JSON_VALUE(l.labels, '$."bq_eval.serving_tier"')
      ) AS serving_tier,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.tier_source'),
        JSON_VALUE(l.labels, '$.tier_source')
      ) AS raw_tier_source,
      SAFE_CAST(
        COALESCE(
          JSON_VALUE(l.json_payload, '$.server_traffic_type_present'),
          JSON_VALUE(l.labels, '$.server_traffic_type_present')
        ) AS BOOL
      ) AS server_traffic_type_present,
      COALESCE(
        SAFE_CAST(JSON_VALUE(l.json_payload, '$.http_status_code') AS INT64),
        SAFE_CAST(JSON_VALUE(l.labels, '$."http.response.status_code"') AS INT64)
      ) AS http_status_code,
      SAFE_CAST(JSON_VALUE(l.json_payload, '$.input_tokens') AS INT64) AS input_tokens,
      SAFE_CAST(JSON_VALUE(l.json_payload, '$.output_tokens') AS INT64) AS output_tokens,
      SAFE_CAST(
        COALESCE(
          JSON_VALUE(l.json_payload, '$.cache_read_tokens'),
          JSON_VALUE(l.json_payload, '$.cached_input_tokens')
        ) AS INT64
      ) AS cache_read_tokens,
      SAFE_CAST(JSON_VALUE(l.json_payload, '$.cache_write_5m_tokens') AS INT64) AS cache_write_5m_tokens,
      SAFE_CAST(JSON_VALUE(l.json_payload, '$.cache_write_1h_tokens') AS INT64) AS cache_write_1h_tokens,
      SAFE_CAST(
        COALESCE(
          JSON_VALUE(l.json_payload, '$.reasoning_tokens'),
          JSON_VALUE(l.json_payload, '$.thinking_tokens')
        ) AS INT64
      ) AS reasoning_tokens,
      SAFE_CAST(JSON_VALUE(l.json_payload, '$.weighted_burndown_units') AS FLOAT64) AS weighted_burndown_units,
      SAFE_CAST(JSON_VALUE(l.json_payload, '$.ttft_ms') AS FLOAT64) AS ttft_ms,
      SAFE_CAST(JSON_VALUE(l.json_payload, '$.latency_ms') AS FLOAT64) AS latency_ms,
      SAFE_CAST(
        COALESCE(
          JSON_VALUE(l.json_payload, '$.thought_signature_present'),
          JSON_VALUE(l.labels, '$."bq_eval.thought_signature_present"')
        ) AS BOOL
      ) AS thought_signature_present,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.traffic_type'),
        JSON_VALUE(l.labels, '$."bq_eval.traffic_type"')
      ) AS traffic_type,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.trigger_principal'),
        JSON_VALUE(l.labels, '$."bq_eval.trigger_principal"')
      ) AS trigger_principal,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.agent_principal'),
        JSON_VALUE(l.labels, '$."bq_eval.agent_principal"')
      ) AS agent_principal,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.auth_method'),
        JSON_VALUE(l.labels, '$."bq_eval.auth_method"')
      ) AS auth_method,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.issuer'),
        JSON_VALUE(l.labels, '$."bq_eval.auth_issuer"')
      ) AS issuer,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.aud'),
        JSON_VALUE(l.labels, '$."bq_eval.auth_audience"')
      ) AS aud,
      COALESCE(
        JSON_VALUE(l.json_payload, '$.delegation_chain_hash'),
        JSON_VALUE(l.labels, '$."bq_eval.delegation_chain_hash"')
      ) AS delegation_chain_hash,
      JSON_STRIP_NULLS(
        JSON_OBJECT(
          'service_name', COALESCE(JSON_VALUE(l.otel, '$.resource.attributes."service.name"'), JSON_VALUE(l.json_payload, '$.gateway_service_name')),
          'telemetry_environment', COALESCE(JSON_VALUE(l.otel, '$.resource.attributes."deployment.environment.name"'), JSON_VALUE(l.otel, '$.resource.attributes."deployment.environment"')),
          'cloud_provider', JSON_VALUE(l.otel, '$.resource.attributes."cloud.provider"'),
          'cloud_region', JSON_VALUE(l.otel, '$.resource.attributes."cloud.region"'),
          'prompt_name', JSON_VALUE(l.labels, '$."gen_ai.prompt.name"'),
          'prompt_version', JSON_VALUE(l.labels, '$."gen_ai.prompt.version"'),
          'request_type_header', COALESCE(JSON_VALUE(l.labels, '$.request_type_header'), JSON_VALUE(l.json_payload, '$.request_type_header'))
        )
      ) AS open_labels
    FROM `${PROJECT_ID}.${LOGS_DATASET_ID}._AllLogs` AS l
    WHERE l.timestamp >= from_time
      AND l.timestamp < to_time
      AND (
        l.log_id = 'inference_request'
        OR REGEXP_CONTAINS(COALESCE(l.log_name, ''), r'/logs/inference_request$')
      )
  ),
  deduped AS (
    SELECT *
    FROM (
      SELECT
        r.*,
        ROW_NUMBER() OVER (
          PARTITION BY r.client_request_id, r.span_id, r.attempt_index
          ORDER BY r.receive_timestamp DESC, r.request_time DESC, COALESCE(r.insert_id, '') DESC
        ) AS attempt_rn
      FROM raw_logs AS r
      WHERE r.client_request_id IS NOT NULL
        AND r.span_id IS NOT NULL
    )
    WHERE attempt_rn = 1
  ),
  with_app AS (
    SELECT
      d.*,
      ar.app_id AS resolved_app_id,
      ROW_NUMBER() OVER (
        PARTITION BY d.client_request_id, d.span_id, d.attempt_index
        ORDER BY ar.effective_from DESC
      ) AS app_rn
    FROM deduped AS d
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS ar
      ON d.raw_caller_app_id IS NOT NULL
      AND (ar.app_id = d.raw_caller_app_id OR ar.apphub_application_id = d.raw_caller_app_id)
      AND ar.effective_from <= d.request_time
      AND (ar.effective_to IS NULL OR d.request_time < ar.effective_to)
  ),
  with_pool AS (
    SELECT
      a.*,
      cp.runtime_pool_id AS pool_runtime_id,
      cp.emits_traffic_type AS pool_emits_traffic_type,
      cp.supports_priority_paygo AS pool_supports_spillover,
      ROW_NUMBER() OVER (
        PARTITION BY a.client_request_id, a.span_id, a.attempt_index
        ORDER BY cp.effective_from DESC, cp.order_id ASC
      ) AS pool_rn
    FROM with_app AS a
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_pools` AS cp
      ON (
        (a.pool_id IS NOT NULL AND (cp.pool_id = a.pool_id OR cp.order_id = a.pool_id))
        OR (a.pool_id IS NULL AND a.raw_runtime_pool_id IS NOT NULL AND cp.runtime_pool_id = a.raw_runtime_pool_id)
      )
      AND cp.effective_from <= a.request_time
      AND (cp.effective_to IS NULL OR a.request_time < cp.effective_to)
    WHERE a.app_rn = 1
  )
  SELECT
    p.client_request_id,
    p.trace_id,
    p.span_id,
    p.response_id,
    p.attempt_index,
    p.request_time,
    p.resolved_app_id AS trace_owner_app_id,
    p.billing_project_id,
    p.provider,
    p.request_model,
    p.response_model,
    p.upstream_endpoint_location,
    p.state_mode,
    p.pool_id,
    COALESCE(p.raw_runtime_pool_id, p.pool_runtime_id) AS runtime_pool_id,
    p.serving_tier,
    CASE
      WHEN p.pool_emits_traffic_type IS FALSE AND COALESCE(p.server_traffic_type_present, FALSE) IS FALSE THEN
        CASE
          WHEN COALESCE(p.pool_supports_spillover, TRUE) IS TRUE THEN 'inferred_ambiguous'
          ELSE 'inferred'
        END
      WHEN p.raw_tier_source IN ('server', 'inferred', 'inferred_ambiguous') THEN p.raw_tier_source
      WHEN p.server_traffic_type_present IS TRUE THEN 'server'
      ELSE 'inferred_ambiguous'
    END AS tier_source,
    p.http_status_code,
    p.input_tokens,
    p.output_tokens,
    p.cache_read_tokens,
    p.cache_write_5m_tokens,
    p.cache_write_1h_tokens,
    p.reasoning_tokens,
    p.weighted_burndown_units,
    p.ttft_ms,
    p.latency_ms,
    p.thought_signature_present,
    p.traffic_type,
    CASE
      WHEN p.traffic_type IS NULL THEN CAST(NULL AS BOOL)
      ELSE TRUE
    END AS traffic_type_attested,
    p.trigger_principal,
    p.agent_principal,
    p.auth_method,
    p.issuer,
    p.aud,
    p.delegation_chain_hash,
    CASE
      WHEN TO_JSON_STRING(p.open_labels) = '{}' THEN CAST(NULL AS JSON)
      ELSE p.open_labels
    END AS labels
  FROM with_pool AS p
  WHERE p.pool_rn = 1
);
