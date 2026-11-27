-- Silver Capacity & Governance Extension Tables (`M19-0`, `ADR-028`, `ADR-032`, `ADR-048`, `ADR-049`).
-- SCD-2 `capacity_pools`, `capacity_allocations`, and `allowed_models` in `silver_ref`;
-- gateway-written 1-minute rollup `burst_metrics_1m` in `silver_open` (zero `*_open` twins).

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_pools` (
  order_id STRING NOT NULL,
  pool_id STRING NOT NULL,
  runtime_pool_id STRING NOT NULL,
  cloud_provider STRING NOT NULL,
  scope_key STRING NOT NULL,
  model STRING NOT NULL,
  region STRING NOT NULL,
  committed_units FLOAT64 NOT NULL,
  unit_rate FLOAT64 NOT NULL,
  renewal_type STRING NOT NULL,
  expiry_date DATE,
  min_cache_tokens INT64,
  supports_priority_paygo BOOL NOT NULL,
  emits_traffic_type BOOL NOT NULL,
  effective_from TIMESTAMP NOT NULL,
  effective_to TIMESTAMP
)
CLUSTER BY runtime_pool_id, model, region;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_REF_DATASET}.capacity_allocations` (
  allocation_id STRING NOT NULL,
  pool_id STRING NOT NULL,
  order_id STRING,
  runtime_pool_id STRING NOT NULL,
  app_id STRING,
  business_unit STRING,
  cost_center STRING,
  funded_units FLOAT64 NOT NULL,
  effective_from TIMESTAMP NOT NULL,
  effective_to TIMESTAMP
)
CLUSTER BY runtime_pool_id, app_id;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_REF_DATASET}.allowed_models` (
  provider STRING NOT NULL,
  model STRING NOT NULL,
  region STRING NOT NULL,
  environment STRING NOT NULL,
  business_unit STRING NOT NULL,
  risk_tier STRING NOT NULL,
  app_id STRING NOT NULL,
  is_allowed BOOL NOT NULL,
  invokable_status STRING NOT NULL,
  zdr_allowed BOOL NOT NULL,
  lifecycle_tier STRING NOT NULL,
  eol_date DATE,
  deprecation_notice_days INT64,
  migration_target_model STRING,
  min_cache_tokens INT64,
  effective_from TIMESTAMP NOT NULL,
  effective_to TIMESTAMP
)
CLUSTER BY provider, model, risk_tier;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_OPEN_DATASET}.burst_metrics_1m` (
  minute_utc TIMESTAMP NOT NULL,
  runtime_pool_id STRING NOT NULL,
  trace_owner_app_id STRING,
  risk_tier STRING,
  window_sec INT64 NOT NULL,
  weighted_burndown_units FLOAT64 NOT NULL,
  committed_capacity_units FLOAT64 NOT NULL,
  peak_burst_ratio FLOAT64 NOT NULL,
  pt_requests INT64 NOT NULL,
  priority_spillover_requests INT64 NOT NULL,
  standard_spillover_requests INT64 NOT NULL,
  involuntary_spillover_requests INT64 NOT NULL,
  http_429_count INT64 NOT NULL,
  gateway_failover_count INT64 NOT NULL,
  caller_retry_count INT64 NOT NULL
)
PARTITION BY DATE(minute_utc)
CLUSTER BY runtime_pool_id, trace_owner_app_id;
