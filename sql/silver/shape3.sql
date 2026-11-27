-- Silver Shape-3 BigLake External Table DDL (`ADR-027`, `ADR-045`, `ADR-048`).
-- Typed Hive-partitioned BigLake table `(dt DATE, hr STRING, ref_uri STRING, payload JSON)`.
-- Supports `JSON`, `PARQUET` (recommended with `metadata_cache_mode = 'MANUAL'`, `max_staleness = INTERVAL 30 MINUTE`),
-- and `ICEBERG` via deploy-time format and cache options.

CREATE EXTERNAL TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.shape3_payloads`
(
  ref_uri STRING,
  payload JSON
)
WITH PARTITION COLUMNS (
  dt DATE,
  hr STRING
)
WITH CONNECTION `${PROJECT_ID}.${CONNECTION_ID}`
OPTIONS (
  format = '${SHAPE3_FORMAT}',
  uris = ['gs://${PAYLOAD_BUCKET}/${SHAPE3_PREFIX}*'],
  hive_partition_uri_prefix = 'gs://${PAYLOAD_BUCKET}/${SHAPE3_PREFIX}',
  require_hive_partition_filter = true${SHAPE3_CACHE_OPTIONS}
);
