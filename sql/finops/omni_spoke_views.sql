-- =============================================================================
-- BQ Eval & FinOps (bq-eval) — BigQuery Omni Cloud-Agnostic Zero-Egress Spokes
--   * Zero raw prompt/context/completion egress: Hive-partitioned AWS S3 and Azure
--     ADLS BigLake tables project strictly metadata, token counts, Rule 0 status,
--     and pushed-down FARM_FINGERPRINT sampling indicators in-region.
--   * Normalizes `principal://` and `spiffe://` workload identities while preserving
--     strict `'unregistered_app'` fallback when App Hub metadata is absent.
--   * Reconciles remote spoke rollups (`agent_eval.remote_trace_slices`) with local
--     GCP enterprise outcomes into unified multi-cloud CPVO.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. AWS S3 & Azure ADLS Hive-Partitioned BigLake External Tables (In-Cloud Residency)
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS `agent_eval.aws_omni_otel_spans` (
  trace_id STRING,
  span_id STRING,
  parent_span_id STRING,
  span_name STRING,
  start_time TIMESTAMP,
  end_time TIMESTAMP,
  latency_ms FLOAT64,
  status_code STRING,
  span_kind STRING,
  service_name STRING,
  gcp_apphub_application_id STRING,
  workload_identity STRING,
  model_name STRING,
  input_tokens INT64,
  cached_input_tokens INT64,
  cache_write_5m_tokens INT64,
  cache_write_1h_tokens INT64,
  output_tokens INT64,
  thinking_tokens INT64,
  rule0_pass BOOL,
  finops_traffic_type STRING,
  serving_traffic_type STRING,
  http_status_code INT64,
  outcome_id STRING,
  outcome_verified BOOL,
  cloud_region STRING,
  span_cost_usd FLOAT64
)
WITH CONNECTION `${AWS_OMNI_CONNECTION_ID}`
OPTIONS (
  format = 'PARQUET',
  uris = ['s3://${AWS_S3_BUCKET}/otel-spans/*']
);

CREATE EXTERNAL TABLE IF NOT EXISTS `agent_eval.azure_omni_otel_spans` (
  trace_id STRING,
  span_id STRING,
  parent_span_id STRING,
  span_name STRING,
  start_time TIMESTAMP,
  end_time TIMESTAMP,
  latency_ms FLOAT64,
  status_code STRING,
  span_kind STRING,
  service_name STRING,
  gcp_apphub_application_id STRING,
  workload_identity STRING,
  model_name STRING,
  input_tokens INT64,
  cached_input_tokens INT64,
  cache_write_5m_tokens INT64,
  cache_write_1h_tokens INT64,
  output_tokens INT64,
  thinking_tokens INT64,
  rule0_pass BOOL,
  finops_traffic_type STRING,
  serving_traffic_type STRING,
  http_status_code INT64,
  outcome_id STRING,
  outcome_verified BOOL,
  cloud_region STRING,
  span_cost_usd FLOAT64
)
WITH CONNECTION `${AZURE_OMNI_CONNECTION_ID}`
OPTIONS (
  format = 'PARQUET',
  uris = ['azure://${AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/${AZURE_CONTAINER}/otel-spans/*']
);

-- -----------------------------------------------------------------------------
-- 2. Zero-Egress Spoke Normalization Views (AWS S3 & Azure ADLS)
--    * Pushed-down `FARM_FINGERPRINT(trace_id)` 5% canary selection in-region
--    * Identity normalization for `principal://` and `spiffe://` URIs
--    * Zero raw prompt/context/completion/payload columns projected
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_omni_aws_spoke_spans` AS
SELECT
  'aws' AS cloud_provider,
  COALESCE(NULLIF(TRIM(cloud_region), ''), 'aws-us-east-1') AS cloud_region,
  trace_id,
  span_id,
  start_time,
  latency_ms,
  status_code,
  COALESCE(NULLIF(TRIM(gcp_apphub_application_id), ''), 'unregistered_app') AS apphub_application_id,
  CASE
    WHEN STARTS_WITH(COALESCE(workload_identity, ''), 'spiffe://')
      OR STARTS_WITH(COALESCE(workload_identity, ''), 'principal://')
      THEN TRIM(workload_identity)
    WHEN workload_identity IS NOT NULL AND TRIM(workload_identity) != ''
      THEN CONCAT('principal://iam.googleapis.com/projects/external-aws/subject/', TRIM(workload_identity))
    ELSE 'principal://unattributed/unknown'
  END AS workload_principal,
  model_name,
  COALESCE(finops_traffic_type, 'production_inference') AS finops_traffic_type,
  COALESCE(serving_traffic_type, 'STANDARD') AS serving_traffic_type,
  COALESCE(http_status_code, IF(status_code = 'ERROR', 500, 200)) AS http_status_code,
  COALESCE(rule0_pass, status_code != 'ERROR') AS rule0_pass,
  outcome_id,
  COALESCE(outcome_verified, FALSE) AS outcome_verified,
  input_tokens,
  cached_input_tokens,
  COALESCE(cache_write_5m_tokens, 0) AS cache_write_5m_tokens,
  COALESCE(cache_write_1h_tokens, 0) AS cache_write_1h_tokens,
  output_tokens,
  thinking_tokens,
  (
    1.0 * GREATEST(0, input_tokens - cached_input_tokens - COALESCE(cache_write_5m_tokens, 0) - COALESCE(cache_write_1h_tokens, 0))
    + 0.1 * cached_input_tokens
    + 1.25 * COALESCE(cache_write_5m_tokens, 0)
    + 2.0 * COALESCE(cache_write_1h_tokens, 0)
    + 4.0 * (output_tokens + thinking_tokens)
  ) AS weighted_burndown_tokens,
  COALESCE(span_cost_usd, 0.0) AS span_cost_usd,
  (MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 5) AS in_canary_5pct
FROM `agent_eval.aws_omni_otel_spans`;

CREATE OR REPLACE VIEW `agent_eval.v_omni_azure_spoke_spans` AS
SELECT
  'azure' AS cloud_provider,
  COALESCE(NULLIF(TRIM(cloud_region), ''), 'azure-eastus2') AS cloud_region,
  trace_id,
  span_id,
  start_time,
  latency_ms,
  status_code,
  COALESCE(NULLIF(TRIM(gcp_apphub_application_id), ''), 'unregistered_app') AS apphub_application_id,
  CASE
    WHEN STARTS_WITH(COALESCE(workload_identity, ''), 'spiffe://')
      OR STARTS_WITH(COALESCE(workload_identity, ''), 'principal://')
      THEN TRIM(workload_identity)
    WHEN workload_identity IS NOT NULL AND TRIM(workload_identity) != ''
      THEN CONCAT('principal://iam.googleapis.com/projects/external-azure/subject/', TRIM(workload_identity))
    ELSE 'principal://unattributed/unknown'
  END AS workload_principal,
  model_name,
  COALESCE(finops_traffic_type, 'production_inference') AS finops_traffic_type,
  COALESCE(serving_traffic_type, 'STANDARD') AS serving_traffic_type,
  COALESCE(http_status_code, IF(status_code = 'ERROR', 500, 200)) AS http_status_code,
  COALESCE(rule0_pass, status_code != 'ERROR') AS rule0_pass,
  outcome_id,
  COALESCE(outcome_verified, FALSE) AS outcome_verified,
  input_tokens,
  cached_input_tokens,
  COALESCE(cache_write_5m_tokens, 0) AS cache_write_5m_tokens,
  COALESCE(cache_write_1h_tokens, 0) AS cache_write_1h_tokens,
  output_tokens,
  thinking_tokens,
  (
    1.0 * GREATEST(0, input_tokens - cached_input_tokens - COALESCE(cache_write_5m_tokens, 0) - COALESCE(cache_write_1h_tokens, 0))
    + 0.1 * cached_input_tokens
    + 1.25 * COALESCE(cache_write_5m_tokens, 0)
    + 2.0 * COALESCE(cache_write_1h_tokens, 0)
    + 4.0 * (output_tokens + thinking_tokens)
  ) AS weighted_burndown_tokens,
  COALESCE(span_cost_usd, 0.0) AS span_cost_usd,
  (MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 5) AS in_canary_5pct
FROM `agent_eval.azure_omni_otel_spans`;

-- -----------------------------------------------------------------------------
-- 3. Remote Partial Rollup View (`v_omni_remote_trace_slice_rollup`)
--    * Matches `agent_eval.remote_trace_slices` schema for cross-cloud hub ingestion
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_omni_remote_trace_slice_rollup` AS
WITH combined_spokes AS (
  SELECT * FROM `agent_eval.v_omni_aws_spoke_spans`
  UNION ALL
  SELECT * FROM `agent_eval.v_omni_azure_spoke_spans`
),
attributed_spokes AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY cloud_provider, apphub_application_id, trace_id
      ORDER BY start_time, span_id
    ) AS trace_span_rank,
    CASE
      WHEN outcome_verified AND outcome_id IS NOT NULL THEN
        ROW_NUMBER() OVER (
          PARTITION BY cloud_provider, apphub_application_id, outcome_id
          ORDER BY start_time, span_id
        )
      ELSE NULL
    END AS verified_outcome_span_rank
  FROM combined_spokes
)
SELECT
  cloud_provider,
  cloud_region,
  TIMESTAMP_TRUNC(start_time, HOUR) AS usage_hour,
  apphub_application_id,
  workload_principal,
  model_name,
  finops_traffic_type,
  COUNTIF(trace_span_rank = 1) AS total_traces,
  COUNTIF(trace_span_rank = 1 AND rule0_pass) AS rule0_passing_traces,
  COUNTIF(verified_outcome_span_rank = 1) AS verified_outcomes,
  SUM(input_tokens) AS total_input_tokens,
  SUM(output_tokens) AS total_output_tokens,
  SUM(weighted_burndown_tokens) AS weighted_burndown_tokens,
  SUM(span_cost_usd) AS total_spend_usd
FROM attributed_spokes
GROUP BY
  cloud_provider,
  cloud_region,
  usage_hour,
  apphub_application_id,
  workload_principal,
  model_name,
  finops_traffic_type;

-- -----------------------------------------------------------------------------
-- 4. Unified Multi-Cloud CPVO View (`v_unified_multicloud_cpvo`)
--    * Reconciles local GCP outcomes + `agent_eval.remote_trace_slices` rollups
--    * Counts each GCP outcome and trace once per application (avoiding multi-agent fan-out)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_unified_multicloud_cpvo` AS
WITH gcp_spend AS (
  SELECT
    apphub_application_id,
    SUM(total_cost_with_infra_usd) AS total_spend_usd
  FROM `agent_eval.v_enterprise_cost_per_verified_outcome`
  GROUP BY apphub_application_id
),
gcp_latest_evals AS (
  SELECT
    COALESCE(e.outcome_id, t.outcome_id) AS outcome_id,
    e.binary_pass
  FROM `agent_eval.eval_results` AS e
  LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t
    ON e.trace_id = t.trace_id
  WHERE COALESCE(e.outcome_id, t.outcome_id) IS NOT NULL
    AND e.binary_pass IS NOT NULL
    AND (e.ordinal_score IS NULL OR e.ordinal_score BETWEEN 1 AND 4)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.trace_id, e.rubric_id
    ORDER BY e.evaluated_at DESC
  ) = 1
),
gcp_step2_by_outcome AS (
  SELECT
    outcome_id,
    LOGICAL_AND(binary_pass) AS step2_all_pass
  FROM gcp_latest_evals
  GROUP BY outcome_id
),
gcp_distinct_outcomes AS (
  SELECT
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COUNT(DISTINCT w.trace_id) AS total_traces,
    COUNT(
      DISTINCT IF(
        COALESCE(o.outcome_status, 'open') = 'succeeded'
          AND COALESCE(o.is_verified, FALSE)
          AND COALESCE(j.step2_all_pass, TRUE),
        w.outcome_id,
        NULL
      )
    ) AS verified_outcomes
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o
    ON w.outcome_id = o.outcome_id
  LEFT JOIN gcp_step2_by_outcome AS j
    ON w.outcome_id = j.outcome_id
  LEFT JOIN `agent_eval.app_registry` AS r
    ON COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') = r.apphub_application_id
  WHERE w.outcome_id IS NOT NULL
    AND NOT COALESCE(r.is_external_counterparty, FALSE)
    AND LOWER(COALESCE(JSON_VALUE(w.attributes, '$."apphub.external"'), 'false')) != 'true'
  GROUP BY 1
),
gcp_outcomes AS (
  SELECT
    'gcp' AS cloud_provider,
    c.apphub_application_id,
    c.total_traces,
    c.verified_outcomes,
    s.total_spend_usd
  FROM gcp_distinct_outcomes AS c
  LEFT JOIN gcp_spend AS s
    ON c.apphub_application_id = s.apphub_application_id
),
remote_outcomes AS (
  SELECT
    cloud_provider,
    apphub_application_id,
    SUM(total_traces) AS total_traces,
    SUM(verified_outcomes) AS verified_outcomes,
    SUM(total_spend_usd) AS total_spend_usd
  FROM `agent_eval.remote_trace_slices`
  GROUP BY cloud_provider, apphub_application_id
),
all_clouds AS (
  SELECT * FROM gcp_outcomes
  UNION ALL
  SELECT * FROM remote_outcomes
)
SELECT
  apphub_application_id,
  COUNT(DISTINCT cloud_provider) AS active_clouds,
  SUM(total_traces) AS total_traces,
  SUM(verified_outcomes) AS verified_outcomes,
  SUM(total_spend_usd) AS total_spend_usd,
  SAFE_DIVIDE(SUM(total_spend_usd), NULLIF(SUM(verified_outcomes), 0)) AS unified_cost_per_verified_outcome_usd
FROM all_clouds
GROUP BY apphub_application_id;
