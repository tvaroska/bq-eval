-- =============================================================================
-- BQ Eval — Step 3: Saved Eval Outputs for Trends, Regression Gates & Cost
--   * Persists Step-2 / Step-2.5 evaluation outputs into `agent_eval.eval_results`
--   * Supports repeatable runs (`eval_run_id`), paired model/prompt comparisons
--     (`0%` negative-flip regression gate), and failure loss-bank mining
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Saved Evaluation Results Table (`agent_eval.eval_results`)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.eval_results` (
  eval_id STRING NOT NULL OPTIONS (description = 'Unique evaluation record ID.'),
  eval_run_id STRING OPTIONS (description = 'Logical run identifier grouping traces evaluated in one batch or experiment.'),
  trace_id STRING NOT NULL OPTIONS (description = 'Evaluated W3C trace identifier.'),
  outcome_id STRING OPTIONS (description = 'Business outcome identifier (outcome.id).'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Resolved application identifier.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Rubric identifier from agent_eval.rubrics.'),
  judge_model STRING NOT NULL OPTIONS (description = 'Autorater model identifier (gemini-3.8-flash or gemini-4-argon).'),
  sampling_stratum STRING NOT NULL OPTIONS (description = 'Sampling reason: FULL_100PCT, SAMPLE_10PCT, CANARY_5PCT, LAST_1H, or ERROR_OR_SLA_TAIL.'),
  rule0_pass BOOL NOT NULL OPTIONS (description = 'Trace-level Step-1 deterministic status at evaluation time.'),
  reasoning_trace STRING NOT NULL OPTIONS (description = 'Step-by-step CoT rationale emitted before ordinal_score.'),
  ordinal_score INT64 NOT NULL OPTIONS (description = '4-point ordinal rubric score (1, 2, 3, or 4).'),
  binary_pass BOOL NOT NULL OPTIONS (description = 'Deterministic threshold outcome (ordinal_score >= passing_score).'),
  judge_input_tokens INT64 NOT NULL OPTIONS (description = 'Prompt tokens consumed by the judge call.'),
  judge_output_tokens INT64 NOT NULL OPTIONS (description = 'Completion tokens consumed by the judge call.'),
  judge_thinking_tokens INT64 NOT NULL OPTIONS (description = 'Reasoning tokens consumed by the judge call.'),
  policy_sha256 STRING OPTIONS (description = 'SHA-256 hash of active policy text for zero-day amendment drift tracking.'),
  amended_clause_ids STRING OPTIONS (description = 'Comma-separated clause IDs modified under the active policy revision.'),
  target_clause_id STRING OPTIONS (description = 'Optional clause identifier evaluated by this trace/rubric pair for clause-scoped NFR gating.'),
  evaluated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC judge execution timestamp.')
)
PARTITION BY DATE(evaluated_at)
CLUSTER BY apphub_application_id, rubric_id, trace_id;

-- Idempotent Engine A persistence runs via `CALL agent_eval.run_daily_quota_guarded_judge(...)`
-- (`sql/step3_daily_quota_guard.sql`); Engine B (`AI.EVALUATE_AGENT`, private preview / non-public TVF) persistence SQL
-- is emitted via `bq_eval.engine.sql_eval.build_ai_evaluate_agent_sql(..., persist_run_id=...)`.

-- View 3A: Deduplicated Saved Run-Level Summary (`v_step3_eval_runs`)
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
    e.evaluated_at
  FROM `agent_eval.eval_results` AS e
),
deduped_results AS (
  SELECT *
  FROM normalized_results
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY eval_run_id, trace_id, rubric_id
    ORDER BY evaluated_at DESC
  ) = 1
)
SELECT
  e.eval_run_id,
  e.apphub_application_id,
  e.rubric_id,
  e.judge_model,
  e.sampling_stratum,
  COUNT(*) AS evaluated_traces,
  AVG(IF(e.rule0_pass, 1.0, 0.0)) AS step1_pass_rate,
  AVG(IF(e.binary_pass, 1.0, 0.0)) AS rubric_pass_rate,
  AVG(IF(e.rule0_pass AND e.binary_pass, 1.0, 0.0)) AS verified_outcome_rate,
  MIN(e.evaluated_at) AS started_at,
  MAX(e.evaluated_at) AS completed_at
FROM deduped_results AS e
GROUP BY
  e.eval_run_id,
  e.apphub_application_id,
  e.rubric_id,
  e.judge_model,
  e.sampling_stratum;

-- -----------------------------------------------------------------------------
-- 4. View 3B: Temporal Paired Run Comparison for 0% Negative-Flip Gates
--    * Deduplicates append-only eval_results on (eval_run_id, trace_id, rubric_id)
--    * Orders baseline -> candidate temporally by MIN(evaluated_at) (with
--      deterministic eval_run_id tie-break when timestamps match)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step3_paired_run_comparison` AS
WITH normalized_results AS (
  SELECT
    COALESCE(e.eval_run_id, FORMAT_TIMESTAMP('%Y-%m-%d', e.evaluated_at)) AS eval_run_id,
    e.apphub_application_id,
    e.rubric_id,
    e.trace_id,
    e.ordinal_score,
    e.binary_pass,
    e.reasoning_trace,
    e.policy_sha256,
    e.amended_clause_ids,
    e.target_clause_id,
    e.evaluated_at
  FROM `agent_eval.eval_results` AS e
),
deduped_results AS (
  SELECT *
  FROM normalized_results
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY eval_run_id, trace_id, rubric_id
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
  COALESCE(cand.target_clause_id, base.target_clause_id) AS target_clause_id,
  base.policy_sha256 AS baseline_policy_sha256,
  cand.policy_sha256 AS candidate_policy_sha256,
  (base.policy_sha256 IS DISTINCT FROM cand.policy_sha256) AS policy_changed,
  cand.amended_clause_ids,
  (
    (base.policy_sha256 IS DISTINCT FROM cand.policy_sha256)
    AND COALESCE(cand.target_clause_id, base.target_clause_id) IS NOT NULL
    AND CONCAT(',', COALESCE(cand.amended_clause_ids, ''), ',')
      LIKE CONCAT('%,', COALESCE(cand.target_clause_id, base.target_clause_id), ',%')
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
      AND CONCAT(',', COALESCE(cand.amended_clause_ids, ''), ',')
        LIKE CONCAT('%,', COALESCE(cand.target_clause_id, base.target_clause_id), ',%')
    )
  ) AS is_unamended_negative_flip,
  (NOT base.binary_pass AND cand.binary_pass) AS is_positive_flip,
  cand.reasoning_trace AS candidate_reasoning
FROM deduped_results AS base
INNER JOIN run_starts AS base_start
  ON base.eval_run_id = base_start.eval_run_id
INNER JOIN deduped_results AS cand
  ON base.trace_id = cand.trace_id
  AND base.rubric_id = cand.rubric_id
  AND base.eval_run_id != cand.eval_run_id
INNER JOIN run_starts AS cand_start
  ON cand.eval_run_id = cand_start.eval_run_id
WHERE base_start.run_started_at < cand_start.run_started_at
  OR (
    base_start.run_started_at = cand_start.run_started_at
    AND base.eval_run_id < cand.eval_run_id
  );

-- -----------------------------------------------------------------------------
-- 5. View 3C: Clause-Scoped Policy NFR == 0.0% Gate (`v_step3_policy_nfr_gate`)
--    * Enforces strict NFR == 0.0% across unamended policy clauses while isolating
--      flips on explicitly amended clauses under a new `policy_sha256`.
-- -----------------------------------------------------------------------------
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
