-- Silver Core Datasets & SCD-2 Reference Tables (`ADR-032`, `ADR-040`, `ADR-044`, `ADR-047`, `ADR-048`).
-- Parameterized for `bq-eval bootstrap --mode silver` and offline SQLite execution.

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}`
OPTIONS (
  location = '${LOCATION}',
  description = 'BQ Eval Silver internal base spans, Python JCS UDF, unit_entitlements, and workflow_events tables (ADR-048).'
);

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.${SILVER_REF_DATASET}`
OPTIONS (
  location = '${LOCATION}',
  description = 'BQ Eval Silver SCD-2 reference tables (app_registry, tools, skill_registry) readable by Silver and Gold.'
);

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.${SILVER_OPEN_DATASET}`
OPTIONS (
  location = '${LOCATION}',
  description = 'BQ Eval Silver open published TVFs and wrapper views (zero text, raw IDs, hashes, or Shape-3 scans).'
);

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}`
OPTIONS (
  location = '${LOCATION}',
  description = 'BQ Eval Silver restricted published TVFs, Shape-3 BigLake payloads, and wrapper views (caller-filtered via unit_entitlements).'
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` (
  app_id STRING NOT NULL,
  agent_id STRING,
  apphub_application_id STRING,
  service_name STRING,
  environment STRING,
  allowed_landing_places JSON,
  owner STRING,
  business_unit STRING,
  risk_tier STRING,
  payload_profile STRING,
  sampling_mode STRING,
  barrier_domain STRING,
  runtime_lane STRING,
  mrm_model_id STRING,
  validation_expiry_date DATE,
  effective_from TIMESTAMP NOT NULL,
  effective_to TIMESTAMP
)
CLUSTER BY app_id, service_name;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_REF_DATASET}.tools` (
  app_id STRING NOT NULL,
  tool_name STRING NOT NULL,
  writes_state BOOL NOT NULL,
  modifies_workspace BOOL NOT NULL,
  executes_code BOOL NOT NULL,
  sends_external_reply BOOL NOT NULL,
  returns_pii BOOL NOT NULL,
  creates_ticket BOOL NOT NULL,
  effective_from TIMESTAMP NOT NULL,
  effective_to TIMESTAMP
)
CLUSTER BY app_id, tool_name;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_REF_DATASET}.skill_registry` (
  app_id STRING NOT NULL,
  skill_name STRING NOT NULL,
  skill_version STRING NOT NULL,
  skill_sha256 STRING NOT NULL,
  protected_block_sha256 STRING,
  effective_from TIMESTAMP NOT NULL,
  effective_to TIMESTAMP
)
CLUSTER BY app_id, skill_name;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.unit_entitlements` (
  principal_email STRING NOT NULL,
  business_unit STRING NOT NULL,
  tenant_id STRING NOT NULL
)
CLUSTER BY principal_email, business_unit;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.platform_spans` (
  trace_id STRING,
  span_id STRING,
  parent_span_id STRING,
  trace_state STRING,
  name STRING,
  kind INT64,
  start_time TIMESTAMP,
  end_time TIMESTAMP,
  duration_nano INT64,
  receive_time TIMESTAMP,
  attributes JSON,
  status STRUCT<code INT64, message STRING>,
  events ARRAY<STRUCT<time TIMESTAMP, name STRING, attributes JSON>>,
  resource STRUCT<attributes JSON>,
  instrumentation_scope STRUCT<name STRING, version STRING>
)
PARTITION BY DATE(start_time)
CLUSTER BY trace_id, span_id;
