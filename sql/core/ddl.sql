-- =============================================================================
-- BQ Eval & FinOps (bq-eval) — Physical Tables & Schemas DDL
--   * Full Capture OTel compatibility views: see `sql/core/otel_compat_views.sql`
--   * Default pricing view & catalog seed: see `sql/core/pricing_seed.sql`
--   * Light Capture (`BQAA`) & Hybrid Dual-Path: see `sql/core/bqaa_adapter_views.sql`
--   * Remote judge models live in `sql/core/step2_stateless_eval.sql`;
--     `agent_eval.eval_results` lives in `sql/core/step3_saved_results.sql`.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS `agent_eval`
OPTIONS (
  description = 'Enterprise Agent Evaluation, Native OTel _AllSpans/_AllLogs Compatibility Views, Rogan-Gladen Calibration, and Cost per Verified Outcome dataset.'
);

CREATE SCHEMA IF NOT EXISTS `agent_eval_seed`
OPTIONS (
  description = 'Isolated synthetic seed and time-shift demo loadgen dataset (never mixed with live agent_eval telemetry).'
);

-- -----------------------------------------------------------------------------
-- 1. Application Registry & FinOps Budget Targets (agent_eval.app_registry)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.app_registry` (
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Canonical application identifier (apphub.application.id or fallback agent ID).'),
  display_name STRING NOT NULL OPTIONS (description = 'Human-readable application name.'),
  business_unit STRING NOT NULL OPTIONS (description = 'Owning business unit or line of business.'),
  cost_center STRING NOT NULL OPTIONS (description = 'FinOps chargeback cost center code.'),
  environment STRING NOT NULL OPTIONS (description = 'Deployment environment (prod, staging, eval).'),
  sla_latency_ms FLOAT64 NOT NULL OPTIONS (description = 'Target end-to-end trace SLA latency in milliseconds.'),
  monthly_budget_usd FLOAT64 NOT NULL OPTIONS (description = 'Monthly LLM & evaluation budget cap in USD.'),
  is_external_counterparty BOOL OPTIONS (description = 'TRUE when the registered application represents an external counterparty excluded from internal CPVO.'),
  storage_usd_per_outcome FLOAT64 OPTIONS (description = 'Optional per-outcome storage/WORM retention proxy override in USD (defaults to 0.000020 when NULL).'),
  risk_tier STRING OPTIONS (description = '5-level criticality tier (RL-1, RL-2, RL-3a, RL-3b, RL-4, RL-5) driving tiered AC2 IRR gates (0.85 / 0.80 / 0.75).'),
  autonomy_tier STRING OPTIONS (description = 'Operational autonomy tier (ASSISTIVE, HUMAN_IN_LOOP, HUMAN_ON_LOOP, BOUNDED_AUTONOMOUS, FULLY_AUTONOMOUS).'),
  lifecycle_track_policy STRING OPTIONS (description = 'Allowed model lifecycle track for PT commitments (long_term_ga_only, short_term_ga_allowed, preview_allowed).'),
  funded_gsus FLOAT64 OPTIONS (description = 'Contracted Provisioned Throughput GSU share owned by this application for Pool 3 Involuntary vs. TenantBurst attribution.'),
  spillover_tier STRING OPTIONS (description = 'Configured PayGo spillover billing tier (priority, standard, flex).'),
  pt_commitment_months INT64 OPTIONS (description = 'Provisioned Throughput commitment term in months (0 for PayGo-only; >= 3 blocked on short_term_ga/preview models).'),
  owner_email STRING NOT NULL OPTIONS (description = 'Technical owner contact email.'),
  updated_at TIMESTAMP NOT NULL OPTIONS (description = 'Last registry metadata update timestamp.')
)
CLUSTER BY apphub_application_id, cost_center
OPTIONS (
  description = 'Application registry mapping App Hub / agent identifiers to SLA thresholds, RL-1..RL-5 governance, and FinOps cost centers.'
);

ALTER TABLE `agent_eval.app_registry`
  ADD COLUMN IF NOT EXISTS is_external_counterparty BOOL OPTIONS (description = 'TRUE when the registered application represents an external counterparty excluded from internal CPVO.'),
  ADD COLUMN IF NOT EXISTS storage_usd_per_outcome FLOAT64 OPTIONS (description = 'Optional per-outcome storage/WORM retention proxy override in USD (defaults to 0.000020 when NULL).'),
  ADD COLUMN IF NOT EXISTS risk_tier STRING OPTIONS (description = '5-level criticality tier (RL-1, RL-2, RL-3a, RL-3b, RL-4, RL-5).'),
  ADD COLUMN IF NOT EXISTS autonomy_tier STRING OPTIONS (description = 'Operational autonomy tier.'),
  ADD COLUMN IF NOT EXISTS lifecycle_track_policy STRING OPTIONS (description = 'Allowed model lifecycle track for PT commitments.'),
  ADD COLUMN IF NOT EXISTS funded_gsus FLOAT64 OPTIONS (description = 'Contracted GSU entitlement owned by this application.'),
  ADD COLUMN IF NOT EXISTS spillover_tier STRING OPTIONS (description = 'Configured PayGo spillover tier (priority, standard, flex).'),
  ADD COLUMN IF NOT EXISTS pt_commitment_months INT64 OPTIONS (description = 'Provisioned Throughput commitment term in months.');

-- -----------------------------------------------------------------------------
-- 1B. BigLake External Table for Uploaded OTel GenAI Completion Payloads
--     Dual-schema: supports nested `payload` JSON and top-level text columns
-- -----------------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS `agent_eval.completions` (
  payload JSON OPTIONS (description = 'Uploaded OTel GenAI prompt/context/completion JSON payload.'),
  user_prompt STRING OPTIONS (description = 'Optional top-level user prompt text.'),
  retrieved_context STRING OPTIONS (description = 'Optional top-level retrieved context.'),
  final_response STRING OPTIONS (description = 'Optional top-level final response.')
)
WITH CONNECTION `${CONNECTION_ID}`
OPTIONS (
  format = 'JSON',
  uris = ['gs://${PAYLOAD_BUCKET}/${PREFIX}*.jsonl']
);

-- -----------------------------------------------------------------------------
-- 2. Golden Evaluation Dataset Catalog (agent_eval.golden_cases)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.golden_cases` (
  case_id STRING NOT NULL OPTIONS (description = 'Immutable golden test case identifier.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Target application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Target rubric identifier (e.g., groundedness_4pt_v1).'),
  risk_tier STRING NOT NULL OPTIONS (description = 'Scenario criticality tier: HIGH, MEDIUM, LOW.'),
  user_prompt STRING NOT NULL OPTIONS (description = 'Canonical test input prompt.'),
  retrieved_context STRING NOT NULL OPTIONS (description = 'Canonical grounding context.'),
  reference_answer STRING NOT NULL OPTIONS (description = 'SME-verified reference response.'),
  expected_rule0_pass BOOL NOT NULL OPTIONS (description = 'Expected deterministic Rule 0 outcome.'),
  expected_semantic_pass BOOL NOT NULL OPTIONS (description = 'Expected semantic rubric binary pass outcome.'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = 'Golden case creation timestamp.')
)
CLUSTER BY apphub_application_id, rubric_id, risk_tier
OPTIONS (
  description = 'Version-controlled golden evaluation dataset for pre-deployment Agent Gym and judge regression gates.'
);

-- -----------------------------------------------------------------------------
-- 3. Stratified SME Calibration Audit Labels (agent_eval.sme_labels)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.sme_labels` (
  label_id STRING NOT NULL OPTIONS (description = 'Unique SME calibration label identifier.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Audited trace identifier.'),
  outcome_id STRING OPTIONS (description = 'Business outcome identifier (outcome.id).'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Calibrated rubric identifier.'),
  rater_id STRING NOT NULL OPTIONS (description = 'Anonymized SME rater identifier.'),
  rule0_pass BOOL NOT NULL OPTIONS (description = 'Whether the trace passed Tier 1 deterministic Rule 0 checks.'),
  human_ordinal_score INT64 NOT NULL OPTIONS (description = 'SME 4-point ordinal rating (1..4).'),
  human_pass BOOL NOT NULL OPTIONS (description = 'SME binary pass ground truth.'),
  judge_ordinal_score INT64 OPTIONS (description = 'Autorater 4-point ordinal rating on the same trace.'),
  judge_pass BOOL OPTIONS (description = 'Autorater binary pass prediction on the same trace.'),
  labeled_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC SME annotation timestamp.')
)
PARTITION BY DATE(labeled_at)
CLUSTER BY apphub_application_id, rubric_id
OPTIONS (
  description = 'Human SME audit labels for computing empirical TPR, TNR, Youden J, and Gwet AC2.'
);

ALTER TABLE `agent_eval.sme_labels`
  ADD COLUMN IF NOT EXISTS outcome_id STRING OPTIONS (description = 'Business outcome identifier (outcome.id).');

-- -----------------------------------------------------------------------------
-- 4. Effective-Dated 1P & 3P Model Pricing & Burndown Catalog (agent_eval.model_pricing_catalog)
--    Default view and idempotent 8-SKU seed live in `sql/core/pricing_seed.sql`.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.model_pricing_catalog` (
  model_name STRING NOT NULL OPTIONS (description = 'Public model name (gemini-3.8-flash, gemini-4-argon, claude-sonnet-4-6, etc.).'),
  provider STRING OPTIONS (description = 'Model provider family: google, anthropic, or self_hosted.'),
  lifecycle_track STRING OPTIONS (description = 'GA lifecycle track: long_term_ga, short_term_ga, preview, or legacy_sunset.'),
  effective_start TIMESTAMP NOT NULL OPTIONS (description = 'Inclusive pricing effective start timestamp.'),
  effective_end TIMESTAMP NOT NULL OPTIONS (description = 'Exclusive pricing effective end timestamp.'),
  input_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Uncached input token price per 1M tokens (<=200K context).'),
  cached_input_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Cached prefix token price per 1M tokens (<=200K context).'),
  cache_write_5m_usd_per_1m FLOAT64 OPTIONS (description = '5m ephemeral cache-write price per 1M tokens (1.25x input on 3P models).'),
  cache_write_1h_usd_per_1m FLOAT64 OPTIONS (description = '1h ephemeral cache-write price per 1M tokens (2.0x input on 3P models).'),
  output_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Output and thinking token price per 1M tokens (<=200K context).'),
  long_context_threshold_tokens INT64 OPTIONS (description = 'Context cliff threshold in input tokens (200000 for 1P Gemini; NULL for flat 3P).'),
  long_context_input_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Uncached input price per 1M tokens above threshold (>200K).'),
  long_context_cached_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Cached input price per 1M tokens above threshold (>200K).'),
  long_context_output_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Output/thinking price per 1M tokens above threshold (>200K).'),
  burndown_uncached_weight FLOAT64 NOT NULL OPTIONS (description = 'Standard uncached input burndown multiplier (1.0).'),
  burndown_cached_weight FLOAT64 NOT NULL OPTIONS (description = 'Standard cached input burndown multiplier (0.1).'),
  burndown_cache_write_5m_weight FLOAT64 OPTIONS (description = '5m cache-write burndown multiplier (1.0 1P, 1.25 3P).'),
  burndown_cache_write_1h_weight FLOAT64 OPTIONS (description = '1h cache-write burndown multiplier (1.0 1P, 2.0 3P).'),
  burndown_output_weight FLOAT64 NOT NULL OPTIONS (description = 'Standard output/thinking burndown multiplier (4.0 1P, 5.0 3P).'),
  burndown_long_uncached_weight FLOAT64 NOT NULL OPTIONS (description = 'Long-context (>200K) uncached input multiplier (2.0 1P, 1.0 3P).'),
  burndown_long_cached_weight FLOAT64 NOT NULL OPTIONS (description = 'Long-context (>200K) cached input multiplier (0.2 1P, 0.1 3P).'),
  burndown_long_output_weight FLOAT64 NOT NULL OPTIONS (description = 'Long-context (>200K) output/thinking multiplier (7.5 1P, 5.0 3P).'),
  priority_paygo_multiplier FLOAT64 OPTIONS (description = 'Priority PayGo billing multiplier (1.8x).'),
  flex_paygo_multiplier FLOAT64 OPTIONS (description = 'Flex/Batch PayGo billing multiplier (0.5x).')
)
CLUSTER BY model_name
OPTIONS (
  description = 'Effective-dated 1P/3P SKU pricing, PayGo multipliers, and GSU serving-physics burndown weights.'
);

ALTER TABLE `agent_eval.model_pricing_catalog`
  ADD COLUMN IF NOT EXISTS provider STRING,
  ADD COLUMN IF NOT EXISTS lifecycle_track STRING,
  ADD COLUMN IF NOT EXISTS cache_write_5m_usd_per_1m FLOAT64,
  ADD COLUMN IF NOT EXISTS cache_write_1h_usd_per_1m FLOAT64,
  ADD COLUMN IF NOT EXISTS burndown_cache_write_5m_weight FLOAT64,
  ADD COLUMN IF NOT EXISTS burndown_cache_write_1h_weight FLOAT64,
  ADD COLUMN IF NOT EXISTS priority_paygo_multiplier FLOAT64,
  ADD COLUMN IF NOT EXISTS flex_paygo_multiplier FLOAT64;

-- -----------------------------------------------------------------------------
-- 4B. Server-Side Inference Request Logs (for v_inference_request_reconciliation)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.inference_request_logs` (
  request_id STRING NOT NULL OPTIONS (description = 'Unique server-side inference request ID.'),
  trace_id STRING OPTIONS (description = 'Optional W3C trace_id linking server log to OTel span.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Attributed application identifier.'),
  model_name STRING NOT NULL OPTIONS (description = 'Resolved serving model name.'),
  serving_traffic_type STRING NOT NULL OPTIONS (description = 'Server-reported serving tier: PROVISIONED_THROUGHPUT, PRIORITY, STANDARD, or FLEX.'),
  request_type_header STRING OPTIONS (description = 'Inbound X-Vertex-AI-LLM-Request-Type header value (dedicated, shared, or NULL).'),
  http_status_code INT64 NOT NULL OPTIONS (description = 'HTTP response status code (e.g., 200, 429, 503).'),
  input_tokens INT64 NOT NULL OPTIONS (description = 'Server-billed input tokens.'),
  cached_input_tokens INT64 NOT NULL OPTIONS (description = 'Server-billed cache-read input tokens.'),
  cache_write_5m_tokens INT64 NOT NULL OPTIONS (description = 'Server-billed 5m cache-write tokens.'),
  cache_write_1h_tokens INT64 NOT NULL OPTIONS (description = 'Server-billed 1h cache-write tokens.'),
  output_tokens INT64 NOT NULL OPTIONS (description = 'Server-billed output tokens.'),
  thinking_tokens INT64 NOT NULL OPTIONS (description = 'Server-billed thinking tokens.'),
  request_timestamp TIMESTAMP NOT NULL OPTIONS (description = 'UTC server request timestamp.')
)
PARTITION BY DATE(request_timestamp)
CLUSTER BY apphub_application_id, model_name, serving_traffic_type
OPTIONS (
  description = 'Server-side inference_request telemetry for OTel-vs-billing reconciliation and involuntary spillover detection.'
);

-- -----------------------------------------------------------------------------
-- 4C. BigQuery INFORMATION_SCHEMA.JOBS Query Cost Ledger
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.bq_query_jobs` (
  job_id STRING NOT NULL OPTIONS (description = 'BigQuery job_id from INFORMATION_SCHEMA.JOBS_BY_PROJECT.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Attributed application identifier from job labels.'),
  outcome_id STRING OPTIONS (description = 'Optional business outcome identifier from job labels.'),
  total_bytes_billed INT64 NOT NULL OPTIONS (description = 'Bytes billed for on-demand query pricing ($6.25/TiB).'),
  total_slot_ms INT64 NOT NULL OPTIONS (description = 'Slot milliseconds consumed for capacity-slot pricing.'),
  pricing_mode STRING NOT NULL OPTIONS (description = 'on_demand or capacity_slot.'),
  query_cost_usd FLOAT64 NOT NULL OPTIONS (description = 'Attributed BigQuery execution cost in USD.'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC BigQuery job creation timestamp.')
)
PARTITION BY DATE(created_at)
CLUSTER BY apphub_application_id, pricing_mode
OPTIONS (
  description = 'Attributed BigQuery query scan/slot costs folded into enterprise CPVO.'
);

-- -----------------------------------------------------------------------------
-- 4D. Multi-Cloud BigQuery Omni Remote Trace Slice Rollups
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.remote_trace_slices` (
  cloud_provider STRING NOT NULL OPTIONS (description = 'Origin cloud spoke: aws, azure, or on_prem.'),
  cloud_region STRING NOT NULL OPTIONS (description = 'Origin cloud region.'),
  usage_hour TIMESTAMP NOT NULL OPTIONS (description = 'UTC hourly rollup bucket.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Normalized App Hub application ID or unregistered_app.'),
  workload_principal STRING NOT NULL OPTIONS (description = 'Normalized principal:// or spiffe:// workload identity.'),
  model_name STRING NOT NULL OPTIONS (description = 'Model identifier.'),
  finops_traffic_type STRING NOT NULL OPTIONS (description = 'FinOps traffic lane: production_inference, conflict_escalation, agent_gym_eval, autorater_judge, optimize.'),
  total_traces INT64 NOT NULL OPTIONS (description = 'Remote trace count rolled up in-region with zero raw prompt egress.'),
  rule0_passing_traces INT64 NOT NULL OPTIONS (description = 'Remote traces passing Rule 0 checks.'),
  verified_outcomes INT64 NOT NULL OPTIONS (description = 'Remote verified outcomes counted in-region.'),
  total_input_tokens INT64 NOT NULL OPTIONS (description = 'Aggregated remote input tokens.'),
  total_output_tokens INT64 NOT NULL OPTIONS (description = 'Aggregated remote output tokens.'),
  weighted_burndown_tokens FLOAT64 NOT NULL OPTIONS (description = 'Aggregated remote weighted burndown tokens.'),
  total_spend_usd FLOAT64 NOT NULL OPTIONS (description = 'Aggregated remote model + judge spend in USD.')
)
PARTITION BY DATE(usage_hour)
CLUSTER BY cloud_provider, apphub_application_id, model_name
OPTIONS (
  description = 'Zero-egress partial telemetry rollups pushed from AWS/Azure BigQuery Omni spokes.'
);

-- -----------------------------------------------------------------------------
-- 5. Multi-Agent Fleet Ground-Truth Labels, Outcomes & Run Manifests
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.fleet_labels` (
  run_id STRING NOT NULL OPTIONS (description = 'Fleet evaluation run identifier.'),
  case_id STRING NOT NULL OPTIONS (description = 'Deterministic test case identifier within run_id.'),
  app STRING NOT NULL OPTIONS (description = 'Expected App Hub application identifier (or unregistered_app).'),
  case_type STRING NOT NULL OPTIONS (description = 'Cohort slice: clean, step1_reject, step2_trap, argon_cohort, day1, day2_in_policy, day2_out_of_policy, shadow.'),
  trap_class STRING OPTIONS (description = 'Specific defect or trap class (e.g., po_box, stale_over_90d, cross_bill_street_mismatch, third_party_holder_mismatch).'),
  held_out BOOL NOT NULL OPTIONS (description = 'TRUE when trap_class is omitted from judge rubric instructions to measure unseen defect generalization.'),
  expected_status STRING NOT NULL OPTIONS (description = 'Expected business outcome status (succeeded, failed, escalated, denied).'),
  doc_uris ARRAY<STRING> OPTIONS (description = 'Cloud Storage gs:// or a2a:// document URIs submitted for the case.'),
  doc_sha256 ARRAY<STRING> OPTIONS (description = 'SHA-256 digests of the generated in-memory documents.'),
  request_payload_json STRING OPTIONS (description = 'Serialized request payload dispatched by the live evaluation fleet runner.'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC corpus generation timestamp.')
)
CLUSTER BY run_id, app, case_type
OPTIONS (
  description = 'Seeded ground-truth label catalog for live multi-agent fleet evaluation runs.'
);

CREATE TABLE IF NOT EXISTS `agent_eval.fleet_case_outcomes` (
  run_id STRING NOT NULL OPTIONS (description = 'Fleet evaluation run identifier.'),
  case_id STRING NOT NULL OPTIONS (description = 'Test case identifier joining 1:1 to agent_eval.fleet_labels.'),
  app STRING NOT NULL OPTIONS (description = 'Target App Hub application identifier.'),
  case_type STRING NOT NULL OPTIONS (description = 'Cohort slice from fleet_labels.'),
  outcome_id STRING OPTIONS (description = 'outcome.id returned via x-outcome-id header (NULL for unregistered shadow workload).'),
  trace_id STRING NOT NULL OPTIONS (description = 'Root W3C trace_id returned via x-trace-id header.'),
  observed_status STRING NOT NULL OPTIONS (description = 'Observed outcome status returned by the live service.'),
  http_status INT64 NOT NULL OPTIONS (description = 'HTTP response status code.'),
  recorded_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC completion timestamp recorded by the fleet runner.')
)
CLUSTER BY run_id, app, outcome_id
OPTIONS (
  description = '1:1 live execution mapping between fleet_labels cases and outcome_id values.'
);

CREATE TABLE IF NOT EXISTS `agent_eval.fleet_runs` (
  run_id STRING NOT NULL OPTIONS (description = 'Fleet evaluation run identifier.'),
  total_cases INT64 NOT NULL OPTIONS (description = 'Total cases executed in the run.'),
  kyc_cases INT64 NOT NULL OPTIONS (description = 'KYC cases executed.'),
  rfp_cases INT64 NOT NULL OPTIONS (description = 'Benefits RFP cases executed.'),
  meridian_cases INT64 NOT NULL OPTIONS (description = 'Meridian consolidation deals executed.'),
  shadow_cases INT64 NOT NULL OPTIONS (description = 'Unregistered shadow attempts executed.'),
  attributed_outcomes INT64 NOT NULL OPTIONS (description = 'Cases returning a non-null outcome_id.'),
  promoted_skill_revision_id STRING OPTIONS (description = 'SkillRevision ID promoted between Day-1 and Day-2 deals.'),
  concurrency INT64 NOT NULL OPTIONS (description = 'Max bounded HTTP concurrency used by the runner.'),
  elapsed_seconds FLOAT64 NOT NULL OPTIONS (description = 'Total wall-clock fleet execution time in seconds.'),
  started_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC fleet start timestamp.'),
  completed_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC fleet completion timestamp.')
)
CLUSTER BY run_id
OPTIONS (
  description = 'Run-level execution manifest for live multi-agent fleet evaluation runs.'
);

-- -----------------------------------------------------------------------------
-- 5B. Isolated Synthetic Seed Tables in agent_eval_seed
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval_seed.seed_otel_spans` (
  template_idx INT64 NOT NULL,
  trace_id STRING NOT NULL,
  span_id STRING NOT NULL,
  parent_span_id STRING,
  span_offset_ms INT64 NOT NULL,
  latency_ms FLOAT64 NOT NULL,
  status_code STRING NOT NULL,
  span_kind STRING NOT NULL,
  service_name STRING NOT NULL,
  gcp_apphub_application_id STRING,
  gcp_apphub_service_id STRING,
  gcp_apphub_workload_id STRING,
  agent_name STRING NOT NULL,
  model_name STRING NOT NULL,
  input_tokens INT64 NOT NULL,
  cached_input_tokens INT64 NOT NULL,
  output_tokens INT64 NOT NULL,
  thinking_tokens INT64 NOT NULL,
  tool_name STRING,
  tool_call_status STRING,
  rule0_pass BOOL NOT NULL,
  finops_traffic_type STRING NOT NULL,
  attributes JSON
);

CREATE TABLE IF NOT EXISTS `agent_eval_seed.seed_trace_payloads` (
  template_idx INT64 NOT NULL,
  trace_id STRING NOT NULL,
  apphub_application_id STRING NOT NULL,
  user_prompt STRING NOT NULL,
  retrieved_context STRING NOT NULL,
  final_response STRING NOT NULL
);
