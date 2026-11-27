-- Silver Three Isolated `workflow_events` Input Copies (`ADR-022`, `ADR-034`, `ADR-041`, `ADR-047`, `ADR-048`).
-- Exact 20 fixed columns per copy; landing table determines `source_class` and `is_authoritative`.

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_sor` (
  event_id STRING NOT NULL,
  event_time TIMESTAMP NOT NULL,
  event_type STRING NOT NULL,
  source_system STRING,
  trace_id STRING,
  span_id STRING,
  response_id STRING,
  outcome_id STRING,
  conversation_id STRING,
  action_id STRING,
  nudge_id STRING,
  control_id STRING,
  control_layer STRING,
  control_action STRING,
  action_args STRING,
  approved_expires_at TIMESTAMP,
  user_id STRING,
  tenant_id STRING,
  actor_id STRING,
  payload_json JSON
)
PARTITION BY DATE(event_time)
CLUSTER BY event_type, outcome_id, trace_id, action_id;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_platform` (
  event_id STRING NOT NULL,
  event_time TIMESTAMP NOT NULL,
  event_type STRING NOT NULL,
  source_system STRING,
  trace_id STRING,
  span_id STRING,
  response_id STRING,
  outcome_id STRING,
  conversation_id STRING,
  action_id STRING,
  nudge_id STRING,
  control_id STRING,
  control_layer STRING,
  control_action STRING,
  action_args STRING,
  approved_expires_at TIMESTAMP,
  user_id STRING,
  tenant_id STRING,
  actor_id STRING,
  payload_json JSON
)
PARTITION BY DATE(event_time)
CLUSTER BY event_type, outcome_id, trace_id, action_id;

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.workflow_events_app` (
  event_id STRING NOT NULL,
  event_time TIMESTAMP NOT NULL,
  event_type STRING NOT NULL,
  source_system STRING,
  trace_id STRING,
  span_id STRING,
  response_id STRING,
  outcome_id STRING,
  conversation_id STRING,
  action_id STRING,
  nudge_id STRING,
  control_id STRING,
  control_layer STRING,
  control_action STRING,
  action_args STRING,
  approved_expires_at TIMESTAMP,
  user_id STRING,
  tenant_id STRING,
  actor_id STRING,
  payload_json JSON
)
PARTITION BY DATE(event_time)
CLUSTER BY event_type, outcome_id, trace_id, action_id;
