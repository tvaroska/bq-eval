-- Gold Add-On Stored State Tables (`ADR-048`, `ADR-049`).
-- SCD-2 registries (`app_registry`, `tools`, `skill_registry`, `capacity_pools`,
-- `capacity_allocations`, `allowed_models`) live in `silver_ref`.
-- Gold owns only evaluation/pricing/calibration/WORM/threshold state in `agent_eval`.

CREATE SCHEMA IF NOT EXISTS `agent_eval`
OPTIONS (
  description = 'BQ Eval Gold Evaluation, Monitoring, and Cost add-ons over Silver views (ADR-048, ADR-049).'
);

CREATE TABLE IF NOT EXISTS `agent_eval.workload_thresholds` (
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Registered application ID joining to silver_ref.app_registry.'),
  display_name STRING OPTIONS (description = 'Optional human-readable workload title.'),
  cost_center STRING OPTIONS (description = 'FinOps chargeback cost center override when capacity_allocations is absent.'),
  sla_latency_ms FLOAT64 NOT NULL OPTIONS (description = 'Gold SLA turn latency threshold in milliseconds.'),
  max_tool_calls INT64 OPTIONS (description = 'Gold per-turn tool-loop cap (defaults to 8 when NULL).'),
  max_repeated_tool_calls INT64 OPTIONS (description = 'Gold per-turn identical (tool_name, args_sha256) repeat cap (defaults to 0 when NULL).'),
  monthly_budget_usd FLOAT64 OPTIONS (description = 'Monthly AI & evaluation budget cap in USD.'),
  is_external_counterparty BOOL OPTIONS (description = 'TRUE when the workload represents an external counterparty excluded from internal CPVO.'),
  storage_usd_per_outcome FLOAT64 OPTIONS (description = 'Optional per-outcome WORM/storage cost override in USD (defaults to 0.000020 when NULL).'),
  autonomy_tier STRING OPTIONS (description = 'Operational autonomy tier (ASSISTIVE, HUMAN_IN_LOOP, HUMAN_ON_LOOP, BOUNDED_AUTONOMOUS, FULLY_AUTONOMOUS).'),
  lifecycle_track_policy STRING OPTIONS (description = 'Allowed model lifecycle policy for PT commitments (long_term_ga_only, short_term_ga_allowed, preview_allowed).'),
  funded_gsus FLOAT64 OPTIONS (description = 'Fallback contracted GSU entitlement when silver_ref.capacity_allocations has no active row.'),
  spillover_tier STRING OPTIONS (description = 'Configured PayGo spillover tier (priority, standard, flex).'),
  pt_commitment_months INT64 OPTIONS (description = 'Provisioned Throughput commitment term in months.'),
  owner_email STRING OPTIONS (description = 'Technical owner contact email.'),
  updated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC last threshold update timestamp.')
)
CLUSTER BY apphub_application_id
OPTIONS (
  description = 'Gold-owned SLA, loop-cap, budget, and PT lifecycle governance thresholds keyed by application.'
);

ALTER TABLE `agent_eval.workload_thresholds`
  ADD COLUMN IF NOT EXISTS max_repeated_tool_calls INT64 OPTIONS (description = 'Gold per-turn identical (tool_name, args_sha256) repeat cap (defaults to 0 when NULL).');

CREATE TABLE IF NOT EXISTS `agent_eval.golden_cases` (
  case_id STRING NOT NULL OPTIONS (description = 'Immutable golden test case identifier.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Target application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Target rubric identifier (e.g., groundedness_4pt_v1).'),
  risk_tier STRING NOT NULL OPTIONS (description = 'Scenario criticality tier: HIGH, MEDIUM, LOW.'),
  eval_split STRING OPTIONS (description = 'Optimizer split membership: train or holdout (enforced disjoint on case_id).'),
  user_prompt STRING NOT NULL OPTIONS (description = 'Canonical test input prompt.'),
  retrieved_context STRING NOT NULL OPTIONS (description = 'Canonical grounding context.'),
  reference_answer STRING NOT NULL OPTIONS (description = 'SME-verified reference response.'),
  expected_rule0_pass BOOL NOT NULL OPTIONS (description = 'Expected deterministic gate outcome.'),
  expected_semantic_pass BOOL NOT NULL OPTIONS (description = 'Expected semantic rubric binary pass outcome.'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = 'Golden case creation timestamp.')
)
CLUSTER BY apphub_application_id, rubric_id, risk_tier
OPTIONS (
  description = 'Version-controlled golden evaluation dataset for pre-deployment and Optimizer train/holdout gates.'
);

ALTER TABLE `agent_eval.golden_cases`
  ADD COLUMN IF NOT EXISTS eval_split STRING OPTIONS (description = 'Optimizer split membership: train or holdout.');

CREATE TABLE IF NOT EXISTS `agent_eval.sme_labels` (
  label_id STRING NOT NULL OPTIONS (description = 'Unique SME calibration label identifier.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Audited trace identifier.'),
  outcome_id STRING OPTIONS (description = 'Business outcome identifier (outcome.id).'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Calibrated rubric identifier.'),
  rubric_version STRING OPTIONS (description = 'Pinned rubric version at calibration time.'),
  rater_id STRING NOT NULL OPTIONS (description = 'Primary or consensus human SME rater identifier.'),
  rater_a_id STRING OPTIONS (description = 'First blind independent human SME rater identifier.'),
  rater_b_id STRING OPTIONS (description = 'Second blind independent human SME rater identifier.'),
  adjudicator_id STRING OPTIONS (description = 'Optional tie-break human SME adjudicator identifier.'),
  sampling_stratum STRING OPTIONS (description = 'Population stratum for stratum-specific Se_h / Sp_h calibration.'),
  rule0_pass BOOL NOT NULL OPTIONS (description = 'Whether the trace passed deterministic Gold execution gates.'),
  rater_a_score INT64 OPTIONS (description = 'First blind SME 4-point ordinal rating (1..4).'),
  rater_b_score INT64 OPTIONS (description = 'Second blind SME 4-point ordinal rating (1..4).'),
  human_ordinal_score INT64 OPTIONS (description = 'Adjudicated or consensus SME 4-point ordinal rating (1..4).'),
  human_pass BOOL OPTIONS (description = 'SME binary pass ground truth.'),
  judge_ordinal_score INT64 OPTIONS (description = 'Autorater 4-point ordinal rating on the same trace.'),
  judge_pass BOOL OPTIONS (description = 'Autorater binary pass prediction on the same trace.'),
  judge_model_snapshot_id STRING OPTIONS (description = 'Pinned judge model snapshot ID when calibrated.'),
  judge_prompt_sha256 STRING OPTIONS (description = 'Pinned judge system-prompt SHA-256 when calibrated.'),
  calibration_snapshot_id STRING OPTIONS (description = 'Pinned agent/skill/corpus/model snapshot digest for STALE_CALIBRATION invalidation.'),
  validation_expiry_date DATE OPTIONS (description = 'Optional MRM SR 11-7 calibration TTL expiry date.'),
  labeled_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC SME annotation timestamp.')
)
PARTITION BY DATE(labeled_at)
CLUSTER BY apphub_application_id, rubric_id
OPTIONS (
  description = 'Dual-blind human SME audit labels for stratum-specific TPR, TNR, Youden J, Gwet AC2, and cut-point agreement.'
);

ALTER TABLE `agent_eval.sme_labels`
  ADD COLUMN IF NOT EXISTS outcome_id STRING,
  ADD COLUMN IF NOT EXISTS rubric_version STRING,
  ADD COLUMN IF NOT EXISTS rater_a_id STRING,
  ADD COLUMN IF NOT EXISTS rater_b_id STRING,
  ADD COLUMN IF NOT EXISTS adjudicator_id STRING,
  ADD COLUMN IF NOT EXISTS sampling_stratum STRING,
  ADD COLUMN IF NOT EXISTS rater_a_score INT64,
  ADD COLUMN IF NOT EXISTS rater_b_score INT64,
  ADD COLUMN IF NOT EXISTS judge_model_snapshot_id STRING,
  ADD COLUMN IF NOT EXISTS judge_prompt_sha256 STRING,
  ADD COLUMN IF NOT EXISTS calibration_snapshot_id STRING,
  ADD COLUMN IF NOT EXISTS validation_expiry_date DATE;

CREATE TABLE IF NOT EXISTS `agent_eval.model_pricing_catalog` (
  model_name STRING NOT NULL OPTIONS (description = 'Public model name (gemini-3.8-flash, gemini-4-argon, text-embedding-005, claude-sonnet-4-6, etc.).'),
  provider STRING OPTIONS (description = 'Model provider family: google, anthropic, or self_hosted.'),
  lifecycle_track STRING OPTIONS (description = 'GA lifecycle track: long_term_ga, short_term_ga, preview, or legacy_sunset.'),
  effective_start TIMESTAMP NOT NULL OPTIONS (description = 'Inclusive pricing effective start timestamp.'),
  effective_end TIMESTAMP NOT NULL OPTIONS (description = 'Exclusive pricing effective end timestamp.'),
  input_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Uncached input token price per 1M tokens (<=200K total context).'),
  cached_input_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Cached prefix token price per 1M tokens (<=200K total context).'),
  cache_write_5m_usd_per_1m FLOAT64 OPTIONS (description = '5m ephemeral cache-write price per 1M tokens (1.25x input on 3P models).'),
  cache_write_1h_usd_per_1m FLOAT64 OPTIONS (description = '1h ephemeral cache-write price per 1M tokens (2.0x input on 3P models).'),
  output_usd_per_1m FLOAT64 NOT NULL OPTIONS (description = 'Output and thinking token price per 1M tokens (<=200K total context).'),
  long_context_threshold_tokens INT64 OPTIONS (description = 'Total context cliff threshold in tokens (200000 for 1P Gemini; NULL for flat SKUs).'),
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
  flex_paygo_multiplier FLOAT64 OPTIONS (description = 'Flex/Batch PayGo billing multiplier (0.5x).'),
  min_cache_tokens INT64 OPTIONS (description = 'Minimum prompt token floor required for prefix caching.')
)
CLUSTER BY model_name
OPTIONS (
  description = 'Effective-dated 1P/3P/embeddings SKU pricing, PayGo multipliers, cache floors, and GSU burndown weights.'
);

ALTER TABLE `agent_eval.model_pricing_catalog`
  ADD COLUMN IF NOT EXISTS provider STRING,
  ADD COLUMN IF NOT EXISTS lifecycle_track STRING,
  ADD COLUMN IF NOT EXISTS cache_write_5m_usd_per_1m FLOAT64,
  ADD COLUMN IF NOT EXISTS cache_write_1h_usd_per_1m FLOAT64,
  ADD COLUMN IF NOT EXISTS burndown_cache_write_5m_weight FLOAT64,
  ADD COLUMN IF NOT EXISTS burndown_cache_write_1h_weight FLOAT64,
  ADD COLUMN IF NOT EXISTS priority_paygo_multiplier FLOAT64,
  ADD COLUMN IF NOT EXISTS flex_paygo_multiplier FLOAT64,
  ADD COLUMN IF NOT EXISTS min_cache_tokens INT64;

CREATE TABLE IF NOT EXISTS `agent_eval.bq_query_jobs` (
  job_id STRING NOT NULL OPTIONS (description = 'BigQuery job_id from INFORMATION_SCHEMA.JOBS_BY_PROJECT.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Attributed application identifier from job labels or tool_calls.'),
  outcome_id STRING OPTIONS (description = 'Optional business outcome identifier from job labels or tool_calls.'),
  total_bytes_billed INT64 NOT NULL OPTIONS (description = 'Bytes billed for on-demand query pricing ($6.25/TiB).'),
  total_slot_ms INT64 NOT NULL OPTIONS (description = 'Slot milliseconds consumed for capacity-slot pricing.'),
  pricing_mode STRING NOT NULL OPTIONS (description = 'on_demand or capacity_slot.'),
  query_cost_usd FLOAT64 NOT NULL OPTIONS (description = 'Attributed BigQuery execution cost in USD.'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC BigQuery job creation timestamp.')
)
PARTITION BY DATE(created_at)
CLUSTER BY apphub_application_id, pricing_mode
OPTIONS (
  description = 'Reconciled INFORMATION_SCHEMA.JOBS query scan/slot costs folded into Gold CPVO.'
);

CREATE TABLE IF NOT EXISTS `agent_eval.gold_worm_snapshots` (
  snapshot_id STRING NOT NULL OPTIONS (description = 'Immutable Gold WORM audit snapshot ID.'),
  snapshot_time TIMESTAMP NOT NULL OPTIONS (description = 'UTC WORM snapshot creation timestamp.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Evaluated W3C trace identifier.'),
  outcome_id STRING OPTIONS (description = 'Associated business outcome identifier.'),
  silver_sql_version STRING NOT NULL OPTIONS (description = 'Silver SQL contract version used to produce the snapshot.'),
  rubric_version STRING OPTIONS (description = 'Rubric semantic version.'),
  judge_model_version STRING OPTIONS (description = 'Pinned judge model snapshot identifier.'),
  agent_version STRING OPTIONS (description = 'Application/agent version from Silver labels.'),
  skill_sha256 STRING OPTIONS (description = 'Evaluated skill SHA-256 digest.'),
  protected_block_sha256 STRING OPTIONS (description = 'Evaluated compliance protected-block SHA-256 digest.'),
  calibration_snapshot_id STRING OPTIONS (description = 'Active calibration snapshot digest.'),
  reference_table_snapshot_json JSON OPTIONS (description = 'Frozen SCD-2 reference metadata JSON (zero raw PII or prompt text).'),
  policy_verdicts_json JSON NOT NULL OPTIONS (description = 'Structured deterministic Gold policy gate verdicts JSON.'),
  args_sha256 STRING OPTIONS (description = 'RFC 8785 JCS SHA-256 digest of mutating tool arguments.'),
  approved_args_sha256 STRING OPTIONS (description = 'RFC 8785 JCS SHA-256 digest of HITL-approved arguments.'),
  sor_signature_ref STRING OPTIONS (description = 'Optional system-of-record cryptographic receipt reference.'),
  subject_dek_key_ref STRING OPTIONS (description = 'Optional Cloud KMS per-subject crypto-shredding DEK reference for RTBF.'),
  gcs_worm_uri STRING OPTIONS (description = 'Retention-locked CMEK GCS object URI holding the archived snapshot.'),
  snapshot_sha256 STRING NOT NULL OPTIONS (description = 'Row-level SHA-256 integrity digest over canonical snapshot fields.')
)
PARTITION BY DATE(snapshot_time)
CLUSTER BY trace_id, outcome_id
OPTIONS (
  description = 'PII-free Gold audit snapshot manifest paired with GCS Retention-Lock + CMEK WORM archive and per-subject RTBF DEKs (ADR-031).'
);

CREATE TABLE IF NOT EXISTS `agent_eval.fleet_labels` (
  run_id STRING NOT NULL OPTIONS (description = 'Fleet evaluation run identifier.'),
  case_id STRING NOT NULL OPTIONS (description = 'Deterministic test case identifier within run_id.'),
  app STRING NOT NULL OPTIONS (description = 'Expected App Hub application identifier (or unregistered_app).'),
  case_type STRING NOT NULL OPTIONS (description = 'Cohort slice: clean, step1_reject, step2_trap, argon_cohort, day1, day2_in_policy, day2_out_of_policy, shadow.'),
  trap_class STRING OPTIONS (description = 'Specific defect or trap class.'),
  held_out BOOL NOT NULL OPTIONS (description = 'TRUE when trap_class is omitted from judge rubric instructions.'),
  expected_status STRING NOT NULL OPTIONS (description = 'Expected business outcome status (succeeded, failed, escalated, rejected, denied).'),
  doc_uris ARRAY<STRING> OPTIONS (description = 'Cloud Storage gs:// or a2a:// document URIs submitted for the case.'),
  doc_sha256 ARRAY<STRING> OPTIONS (description = 'SHA-256 digests of the generated in-memory documents.'),
  request_payload_json STRING OPTIONS (description = 'Serialized request payload dispatched by the evaluation fleet runner.'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC corpus generation timestamp.')
)
CLUSTER BY run_id, app, case_type
OPTIONS (
  description = 'Seeded ground-truth label catalog for multi-agent fleet evaluation runs.'
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
  description = '1:1 execution mapping between fleet_labels cases and outcome_id values.'
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
  description = 'Run-level execution manifest for multi-agent fleet evaluation runs.'
);
