-- =============================================================================
-- BQ Eval — Step 3 Daily Judge Token Quota Guard & Scheduled Production Query
--   * Caps daily Step-2 AI.GENERATE_TABLE spend via a single config row
--   * Exposes `v_step2_daily_quota_status` checked before scheduled judging
--   * Scheduled query runs idempotently across already-scored (trace_id, rubric_id)
--     so production traces are never re-judged across daily ticks
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Daily Judge Token Cap Configuration (`agent_eval.eval_daily_quota_config`)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.eval_daily_quota_config` (
  quota_name STRING NOT NULL OPTIONS (description = 'Unique daily quota profile name.'),
  daily_judge_token_cap INT64 NOT NULL OPTIONS (description = 'Maximum total judge tokens (input + output + thinking) permitted per UTC day.'),
  updated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC timestamp when the daily quota cap was last updated.')
)
OPTIONS (
  description = 'Daily token budget guardrails for scheduled Step-2 AI.GENERATE_TABLE judge runs.'
);

MERGE INTO `agent_eval.eval_daily_quota_config` AS target
USING (
  SELECT
    'default_step2_daily_cap' AS quota_name,
    2000000 AS daily_judge_token_cap,
    CURRENT_TIMESTAMP() AS updated_at
) AS src
ON target.quota_name = src.quota_name
WHEN NOT MATCHED THEN
  INSERT (quota_name, daily_judge_token_cap, updated_at)
  VALUES (src.quota_name, src.daily_judge_token_cap, src.updated_at);

-- -----------------------------------------------------------------------------
-- 2. Real-Time Daily Quota Status View (`agent_eval.v_step2_daily_quota_status`)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step2_daily_quota_status` AS
SELECT
  CURRENT_DATE('UTC') AS quota_date_utc,
  MAX(q.daily_judge_token_cap) AS daily_judge_token_cap,
  COALESCE(
    SUM(e.judge_input_tokens + e.judge_output_tokens + e.judge_thinking_tokens),
    0
  ) AS tokens_used_today,
  GREATEST(
    0,
    MAX(q.daily_judge_token_cap) - COALESCE(
      SUM(e.judge_input_tokens + e.judge_output_tokens + e.judge_thinking_tokens),
      0
    )
  ) AS tokens_remaining_today,
  COALESCE(
    SUM(e.judge_input_tokens + e.judge_output_tokens + e.judge_thinking_tokens),
    0
  ) < MAX(q.daily_judge_token_cap) AS quota_available
FROM `agent_eval.eval_daily_quota_config` AS q
LEFT JOIN `agent_eval.eval_results` AS e
  ON DATE(e.evaluated_at) = CURRENT_DATE('UTC');

-- -----------------------------------------------------------------------------
-- 3. Scheduled Query Template: Daily Quota-Guarded 10% Stratified Judge
--    * Provision non-interactively via BigQuery Data Transfer REST API
--      (manualSchedule / disableAutoScheduling=true at rest; enable for live)
--    * Intentionally deduplicates on (trace_id, rubric_id) so the same
--      production trace is scored at most once by scheduled daily ticks
-- -----------------------------------------------------------------------------
-- INSERT INTO `agent_eval.eval_results` (
--   eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id,
--   judge_model, sampling_stratum, rule0_pass, reasoning_trace, ordinal_score,
--   binary_pass, judge_input_tokens, judge_output_tokens, judge_thinking_tokens, evaluated_at
-- )
-- SELECT
--   GENERATE_UUID() AS eval_id,
--   CONCAT('sched_10pct_', FORMAT_DATE('%Y%m%d', CURRENT_DATE('UTC'))) AS eval_run_id,
--   trace_id,
--   outcome_id,
--   apphub_application_id,
--   rubric_id,
--   'gemini-3.8-flash' AS judge_model,
--   'SAMPLE_10PCT' AS sampling_stratum,
--   step1_pass AS rule0_pass,
--   reasoning_trace,
--   ordinal_score,
--   (ordinal_score >= passing_score) AS binary_pass,
--   DIV(LENGTH(prompt), 4) AS judge_input_tokens,
--   DIV(LENGTH(reasoning_trace), 4) + 12 AS judge_output_tokens,
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
--     WHERE cand.rubric_id = 'groundedness_4pt_v1'
--       AND cand.trace_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
--       AND MOD(ABS(FARM_FINGERPRINT(cand.trace_id)), 100) < 10
--       AND existing.eval_id IS NULL
--       AND (SELECT quota_available FROM `agent_eval.v_step2_daily_quota_status`)
--   ),
--   STRUCT(
--     'reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL' AS output_schema,
--     '{"generation_config": {"temperature": 0.0, "max_output_tokens": 2048, "thinking_config": {"thinking_level": "LOW"}}}' AS model_params
--   )
-- );
