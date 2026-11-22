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
  outcome_id STRING OPTIONS (description = 'Business outcome identifier (EVAL-P11 outcome.id).'),
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
  evaluated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC judge execution timestamp.')
)
PARTITION BY DATE(evaluated_at)
CLUSTER BY apphub_application_id, rubric_id, trace_id;

-- -----------------------------------------------------------------------------
-- 2A. Idempotent Step-3 Insert Query — Engine A (`AI.GENERATE_TABLE`)
--     Skips already-scored (eval_run_id, trace_id, rubric_id) triples while
--     preserving `outcome_id` for EVAL-P11 outcome-level CPVO joins.
--     * Routine runs read `v_step2_trace_rubric_prompts` (`step1_pass = TRUE`)
--       to avoid spending judge tokens on $0 deterministic rejects.
--     * For diagnostic Rule-0 failure inspection, swap to
--       `v_step2_all_trace_rubric_prompts`; `v_agent_eval_and_finops_summary`
--       always estimates theta_RG strictly over `WHERE rule0_pass = TRUE`
--       while billing 100% of judge calls (`ADR-004`).
-- -----------------------------------------------------------------------------
-- INSERT INTO `agent_eval.eval_results` (
--   eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id, judge_model,
--   sampling_stratum, rule0_pass, reasoning_trace, ordinal_score, binary_pass,
--   judge_input_tokens, judge_output_tokens, judge_thinking_tokens, evaluated_at
-- )
-- SELECT
--   GENERATE_UUID() AS eval_id,
--   'run_2026_11_06_canary' AS eval_run_id,
--   trace_id,
--   outcome_id,
--   apphub_application_id,
--   rubric_id,
--   judge_model,
--   'SAMPLE_10PCT' AS sampling_stratum,
--   step1_pass AS rule0_pass,
--   reasoning_trace,
--   ordinal_score,
--   (ordinal_score >= passing_score) AS binary_pass,
--   DIV(LENGTH(prompt), 4) AS judge_input_tokens,
--   DIV(LENGTH(COALESCE(reasoning_trace, '')), 4) + 12 AS judge_output_tokens,
--   0 AS judge_thinking_tokens,
--   CURRENT_TIMESTAMP() AS evaluated_at
-- FROM AI.GENERATE_TABLE(
--   MODEL `agent_eval.gemini_3_8_flash_judge`,
--   (
--     SELECT cand.*
--     FROM `agent_eval.v_step2_trace_rubric_prompts` AS cand
--     LEFT JOIN `agent_eval.eval_results` AS existing
--       ON cand.trace_id = existing.trace_id
--       AND cand.rubric_id = existing.rubric_id
--       AND existing.eval_run_id = 'run_2026_11_06_canary'
--     WHERE cand.rubric_id = 'groundedness_4pt_v1'
--       AND MOD(ABS(FARM_FINGERPRINT(cand.trace_id)), 100) < 10
--       AND existing.eval_id IS NULL
--   ),
--   STRUCT(
--     'reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL' AS output_schema,
--     '{"generation_config": {"temperature": 0.0, "max_output_tokens": 2048, "thinking_config": {"thinking_level": "LOW"}}}' AS model_params
--   )
-- );

-- -----------------------------------------------------------------------------
-- 2B. Idempotent Step-3 Persistence Adapter — Engine B (`AI.EVALUATE_AGENT`)
--     Persists managed BQAA `AI.EVALUATE_AGENT` verdicts into `eval_results` so
--     Engine B runs join seamlessly into Step-3 trends, paired diffs, quota
--     accounting, and CPVO.
--     * Per-metric `UNNEST(eval_out.evaluation_results)` fan-out attributes 1x
--       trace-prompt input tokens per managed metric call and output tokens from
--       `metric_row.explanation`.
--     * Skips rows where `metric_row.score IS NULL` (fail-closed) and derives
--       `binary_pass` strictly from `ordinal_score >= 3`.
--     * Routine Engine B scoring reads `v_bqaa_eval_turn_filtered` (`step1_pass
--       = TRUE` and `NOT has_tool_loop`), matching `ADR-004` $0 pre-filtering.
--     * Deduplicates on `(eval_run_id, trace_id, metric_row.metric_name)` so
--       partial-metric backfills persist only missing metrics.
-- -----------------------------------------------------------------------------
-- INSERT INTO `agent_eval.eval_results` (
--   eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id, judge_model,
--   sampling_stratum, rule0_pass, reasoning_trace, ordinal_score, binary_pass,
--   judge_input_tokens, judge_output_tokens, judge_thinking_tokens, evaluated_at
-- )
-- WITH evaluated AS (
--   SELECT
--     eval_out.trace_id,
--     eval_out.outcome_id,
--     eval_out.apphub_application_id,
--     metric_row.metric_name AS rubric_id,
--     eval_out.step1_pass AS rule0_pass,
--     COALESCE(metric_row.explanation, 'Managed AI.EVALUATE_AGENT verdict') AS reasoning_trace,
--     CAST(ROUND(LEAST(4.0, GREATEST(1.0, metric_row.score * 3.0 + 1.0))) AS INT64) AS ordinal_score,
--     DIV(CHAR_LENGTH(TO_JSON_STRING(eval_out.trace)), 4) AS judge_input_tokens,
--     DIV(CHAR_LENGTH(COALESCE(metric_row.explanation, '')), 4) AS judge_output_tokens
--   FROM AI.EVALUATE_AGENT(
--     TABLE (
--       SELECT cand.*
--       FROM `agent_eval.v_bqaa_eval_turn_filtered` AS cand
--       LEFT JOIN (
--         SELECT trace_id, COUNT(DISTINCT rubric_id) AS scored_metrics
--         FROM `agent_eval.eval_results`
--         WHERE eval_run_id = 'run_2026_11_06_bqaa_managed'
--           AND rubric_id IN (
--             'tool_use_quality_v1',
--             'final_response_quality_v1',
--             'hallucination_v1',
--             'safety_v1'
--           )
--         GROUP BY trace_id
--       ) AS completed
--         ON cand.trace_id = completed.trace_id
--       WHERE MOD(ABS(FARM_FINGERPRINT(cand.trace_id)), 100) < 10
--         AND COALESCE(completed.scored_metrics, 0) < 4
--     ),
--     trace_col => 'trace',
--     managed_metrics => [
--       'tool_use_quality_v1',
--       'final_response_quality_v1',
--       'hallucination_v1',
--       'safety_v1'
--     ]
--   ) AS eval_out,
--   UNNEST(eval_out.evaluation_results) AS metric_row
--   LEFT JOIN `agent_eval.eval_results` AS existing
--     ON eval_out.trace_id = existing.trace_id
--     AND metric_row.metric_name = existing.rubric_id
--     AND existing.eval_run_id = 'run_2026_11_06_bqaa_managed'
--   WHERE metric_row.score IS NOT NULL
--     AND existing.eval_id IS NULL
-- )
-- SELECT
--   GENERATE_UUID() AS eval_id,
--   'run_2026_11_06_bqaa_managed' AS eval_run_id,
--   trace_id,
--   outcome_id,
--   apphub_application_id,
--   rubric_id,
--   'ai_evaluate_agent_managed' AS judge_model,
--   'SAMPLE_10PCT' AS sampling_stratum,
--   rule0_pass,
--   reasoning_trace,
--   ordinal_score,
--   (ordinal_score >= 3) AS binary_pass,
--   judge_input_tokens,
--   judge_output_tokens,
--   0 AS judge_thinking_tokens,
--   CURRENT_TIMESTAMP() AS evaluated_at
-- FROM evaluated;

-- -----------------------------------------------------------------------------
-- 3. View 3A: Deduplicated Saved Run-Level Summary (`v_step3_eval_runs`)
-- -----------------------------------------------------------------------------
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
  base.ordinal_score AS baseline_score,
  cand.ordinal_score AS candidate_score,
  base.binary_pass AS baseline_pass,
  cand.binary_pass AS candidate_pass,
  (base.binary_pass AND NOT cand.binary_pass) AS is_negative_flip,
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
