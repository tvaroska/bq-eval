-- Gold Step 3: Saved Evaluation Results, Paired A/B & Case-ID Comparisons, and Clause-Scoped Policy NFR Gate (`ADR-048`, `ADR-049`).

CREATE TABLE IF NOT EXISTS `agent_eval.eval_results` (
  eval_id STRING NOT NULL OPTIONS (description = 'Unique evaluation record ID.'),
  eval_run_id STRING OPTIONS (description = 'Logical run identifier grouping traces evaluated in one batch or experiment.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Evaluated W3C trace identifier.'),
  outcome_id STRING OPTIONS (description = 'Business outcome identifier (outcome.id).'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Resolved application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Rubric identifier from agent_eval.rubrics.'),
  rubric_version STRING OPTIONS (description = 'Pinned semantic rubric version.'),
  judge_model STRING NOT NULL OPTIONS (description = 'Autorater model identifier (gemini-3.8-flash or gemini-4-argon).'),
  judge_model_snapshot_id STRING OPTIONS (description = 'Pinned immutable judge model snapshot identifier.'),
  judge_prompt_sha256 STRING OPTIONS (description = 'Pinned SHA-256 digest of the rendered judge system prompt.'),
  seed INT64 OPTIONS (description = 'Deterministic judge sampling seed.'),
  sampling_stratum STRING NOT NULL OPTIONS (description = 'Sampling reason: FULL_100PCT, SAMPLE_10PCT, CANARY_5PCT, LAST_1H, or ERROR_OR_SLA_TAIL.'),
  rule0_pass BOOL NOT NULL OPTIONS (description = 'Trace-level deterministic Gold gate status at evaluation time.'),
  reasoning_trace STRING OPTIONS (description = 'Step-by-step rationale emitted before ordinal_score (or rejection status on REJECTED rows).'),
  ordinal_score INT64 OPTIONS (description = '4-point ordinal rubric score (1..4; NULL when judge_status = REJECTED).'),
  binary_pass BOOL OPTIONS (description = 'Deterministic threshold outcome (ordinal_score >= passing_score; NULL when judge_status = REJECTED).'),
  judge_status STRING OPTIONS (description = 'Judge outcome status: SCORED or REJECTED (S-19).'),
  judge_input_tokens INT64 NOT NULL OPTIONS (description = 'Prompt tokens consumed by the judge call.'),
  judge_output_tokens INT64 NOT NULL OPTIONS (description = 'Completion tokens consumed by the judge call.'),
  judge_thinking_tokens INT64 NOT NULL OPTIONS (description = 'Reasoning tokens consumed by the judge call.'),
  case_id STRING OPTIONS (description = 'Optional golden test case_id for paired offline candidate comparisons.'),
  shadow_of_trace_id STRING OPTIONS (description = 'Optional baseline trace_id for live shadow A/B comparisons.'),
  experiment_id STRING OPTIONS (description = 'Optional live migration experiment identifier.'),
  variant_id STRING OPTIONS (description = 'Optional candidate arm or variant identifier.'),
  eval_split STRING OPTIONS (description = 'Optional train or holdout split label.'),
  policy_sha256 STRING OPTIONS (description = 'SHA-256 hash of active policy text for zero-day amendment drift tracking.'),
  amended_clause_ids STRING OPTIONS (description = 'Comma-separated clause IDs modified under the active policy revision.'),
  target_clause_id STRING OPTIONS (description = 'Optional clause identifier evaluated by this trace/rubric pair for clause-scoped NFR gating.'),
  evaluated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC judge execution timestamp.')
)
PARTITION BY DATE(evaluated_at)
CLUSTER BY apphub_application_id, rubric_id, trace_id
OPTIONS (
  description = 'Gold evaluation results with snapshot/prompt/seed provenance, REJECTED retry retention (S-19), and paired A/B keys.'
);

ALTER TABLE `agent_eval.eval_results`
  ADD COLUMN IF NOT EXISTS outcome_id STRING,
  ADD COLUMN IF NOT EXISTS rubric_version STRING,
  ADD COLUMN IF NOT EXISTS judge_model_snapshot_id STRING,
  ADD COLUMN IF NOT EXISTS judge_prompt_sha256 STRING,
  ADD COLUMN IF NOT EXISTS seed INT64,
  ADD COLUMN IF NOT EXISTS judge_status STRING,
  ADD COLUMN IF NOT EXISTS case_id STRING,
  ADD COLUMN IF NOT EXISTS shadow_of_trace_id STRING,
  ADD COLUMN IF NOT EXISTS experiment_id STRING,
  ADD COLUMN IF NOT EXISTS variant_id STRING,
  ADD COLUMN IF NOT EXISTS eval_split STRING,
  ADD COLUMN IF NOT EXISTS policy_sha256 STRING,
  ADD COLUMN IF NOT EXISTS amended_clause_ids STRING,
  ADD COLUMN IF NOT EXISTS target_clause_id STRING;

CREATE OR REPLACE VIEW `agent_eval.v_step3_eval_runs` AS
WITH normalized_results AS (
  SELECT
    COALESCE(e.eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', e.evaluated_at)) AS eval_run_id,
    e.trace_id,
    e.apphub_application_id,
    e.rubric_id,
    e.judge_model,
    e.sampling_stratum,
    e.rule0_pass,
    e.binary_pass,
    COALESCE(e.judge_status, IF(e.binary_pass IS NULL, 'REJECTED', 'SCORED')) AS judge_status,
    e.evaluated_at
  FROM `agent_eval.eval_results` AS e
),
deduped_results AS (
  SELECT *
  FROM normalized_results
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY eval_run_id, trace_id, rubric_id
    ORDER BY IF(judge_status = 'SCORED', 0, 1) ASC, evaluated_at DESC
  ) = 1
)
SELECT
  e.eval_run_id,
  e.apphub_application_id,
  e.rubric_id,
  e.judge_model,
  e.sampling_stratum,
  COUNTIF(e.judge_status = 'SCORED' AND e.binary_pass IS NOT NULL) AS evaluated_traces,
  COUNTIF(e.judge_status != 'SCORED' OR e.binary_pass IS NULL) AS rejected_traces,
  AVG(CASE WHEN e.judge_status = 'SCORED' AND e.binary_pass IS NOT NULL THEN IF(e.rule0_pass, 1.0, 0.0) END) AS step1_pass_rate,
  AVG(CASE WHEN e.judge_status = 'SCORED' AND e.binary_pass IS NOT NULL THEN IF(e.binary_pass, 1.0, 0.0) END) AS rubric_pass_rate,
  AVG(CASE WHEN e.judge_status = 'SCORED' AND e.binary_pass IS NOT NULL THEN IF(e.rule0_pass AND e.binary_pass, 1.0, 0.0) END) AS verified_outcome_rate,
  MIN(e.evaluated_at) AS started_at,
  MAX(e.evaluated_at) AS completed_at
FROM deduped_results AS e
GROUP BY
  e.eval_run_id,
  e.apphub_application_id,
  e.rubric_id,
  e.judge_model,
  e.sampling_stratum;

CREATE OR REPLACE VIEW `agent_eval.v_step3_paired_run_comparison` AS
WITH normalized_results AS (
  SELECT
    COALESCE(e.eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', e.evaluated_at)) AS eval_run_id,
    e.apphub_application_id,
    e.rubric_id,
    e.trace_id,
    COALESCE(e.shadow_of_trace_id, e.case_id, e.trace_id) AS pair_key,
    e.experiment_id,
    e.variant_id,
    e.eval_split,
    e.ordinal_score,
    e.binary_pass,
    e.reasoning_trace,
    e.policy_sha256,
    e.amended_clause_ids,
    e.target_clause_id,
    e.evaluated_at
  FROM `agent_eval.eval_results` AS e
  WHERE COALESCE(e.judge_status, 'SCORED') = 'SCORED'
    AND e.binary_pass IS NOT NULL
),
deduped_results AS (
  SELECT *
  FROM normalized_results
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY eval_run_id, pair_key, rubric_id
    ORDER BY evaluated_at DESC
  ) = 1
),
run_starts AS (
  SELECT
    eval_run_id,
    MIN(evaluated_at) AS run_started_at
  FROM deduped_results
  GROUP BY eval_run_id
)
SELECT
  base.apphub_application_id,
  base.rubric_id,
  base.eval_run_id AS baseline_run_id,
  cand.eval_run_id AS candidate_run_id,
  base.trace_id,
  cand.trace_id AS candidate_trace_id,
  base.pair_key,
  COALESCE(cand.experiment_id, base.experiment_id) AS experiment_id,
  cand.variant_id AS candidate_variant_id,
  COALESCE(cand.eval_split, base.eval_split) AS eval_split,
  COALESCE(cand.target_clause_id, base.target_clause_id) AS target_clause_id,
  base.policy_sha256 AS baseline_policy_sha256,
  cand.policy_sha256 AS candidate_policy_sha256,
  (base.policy_sha256 IS DISTINCT FROM cand.policy_sha256) AS policy_changed,
  cand.amended_clause_ids,
  (
    (base.policy_sha256 IS DISTINCT FROM cand.policy_sha256)
    AND COALESCE(cand.target_clause_id, base.target_clause_id) IS NOT NULL
    AND STRPOS(
      CONCAT(',', REPLACE(COALESCE(cand.amended_clause_ids, ''), ' ', ''), ','),
      CONCAT(',', COALESCE(cand.target_clause_id, base.target_clause_id), ',')
    ) > 0
  ) AS is_clause_amended,
  base.ordinal_score AS baseline_score,
  cand.ordinal_score AS candidate_score,
  base.binary_pass AS baseline_pass,
  cand.binary_pass AS candidate_pass,
  (base.binary_pass AND NOT cand.binary_pass) AS is_negative_flip,
  (
    base.binary_pass
    AND NOT cand.binary_pass
    AND NOT (
      (base.policy_sha256 IS DISTINCT FROM cand.policy_sha256)
      AND COALESCE(cand.target_clause_id, base.target_clause_id) IS NOT NULL
      AND STRPOS(
        CONCAT(',', REPLACE(COALESCE(cand.amended_clause_ids, ''), ' ', ''), ','),
        CONCAT(',', COALESCE(cand.target_clause_id, base.target_clause_id), ',')
      ) > 0
    )
  ) AS is_unamended_negative_flip,
  (NOT base.binary_pass AND cand.binary_pass) AS is_positive_flip,
  cand.reasoning_trace AS candidate_reasoning
FROM deduped_results AS base
INNER JOIN run_starts AS base_start
  ON base.eval_run_id = base_start.eval_run_id
INNER JOIN deduped_results AS cand
  ON base.pair_key = cand.pair_key
  AND base.rubric_id = cand.rubric_id
  AND base.eval_run_id != cand.eval_run_id
INNER JOIN run_starts AS cand_start
  ON cand.eval_run_id = cand_start.eval_run_id
WHERE base_start.run_started_at < cand_start.run_started_at
  OR (
    base_start.run_started_at = cand_start.run_started_at
    AND base.eval_run_id < cand.eval_run_id
  );

CREATE OR REPLACE VIEW `agent_eval.v_step3_policy_nfr_gate` AS
SELECT
  apphub_application_id,
  rubric_id,
  baseline_run_id,
  candidate_run_id,
  COUNT(*) AS paired_traces,
  COUNTIF(baseline_pass) AS baseline_passing_traces,
  COUNTIF(is_negative_flip) AS total_negative_flips,
  COUNTIF(is_unamended_negative_flip) AS unamended_negative_flips,
  COUNTIF(is_negative_flip AND is_clause_amended) AS amended_clause_flips,
  COUNTIF(is_positive_flip) AS positive_flips,
  COALESCE(
    SAFE_DIVIDE(
      COUNTIF(is_unamended_negative_flip),
      NULLIF(COUNTIF(baseline_pass AND NOT is_clause_amended), 0)
    ),
    0.0
  ) AS unamended_nfr,
  COUNTIF(is_unamended_negative_flip) = 0 AS passes_nfr_gate
FROM `agent_eval.v_step3_paired_run_comparison`
GROUP BY
  apphub_application_id,
  rubric_id,
  baseline_run_id,
  candidate_run_id;
