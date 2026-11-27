-- Canonical 1P, 3P & Embeddings Model Pricing Defaults & Seed (`agent_eval`).
-- Single default SKU view (`v_default_model_pricing`) and idempotent `MERGE` into `model_pricing_catalog`.

CREATE OR REPLACE VIEW `agent_eval.v_default_model_pricing` AS
SELECT
  'gemini-4-argon' AS model_name, 'google' AS provider, 'long_term_ga' AS lifecycle_track,
  1.25 AS input_usd_per_1m, 0.125 AS cached_input_usd_per_1m, 1.25 AS cache_write_5m_usd_per_1m, 1.25 AS cache_write_1h_usd_per_1m, 5.00 AS output_usd_per_1m,
  200000 AS long_context_threshold_tokens, 2.50 AS long_context_input_usd_per_1m, 0.250 AS long_context_cached_usd_per_1m, 18.75 AS long_context_output_usd_per_1m,
  1.0 AS burndown_uncached_weight, 0.1 AS burndown_cached_weight, 1.0 AS burndown_cache_write_5m_weight, 1.0 AS burndown_cache_write_1h_weight, 4.0 AS burndown_output_weight,
  2.0 AS burndown_long_uncached_weight, 0.2 AS burndown_long_cached_weight, 7.5 AS burndown_long_output_weight,
  1.8 AS priority_paygo_multiplier, 0.5 AS flex_paygo_multiplier, 4096 AS min_cache_tokens
UNION ALL
SELECT 'gemini-4-carbon', 'google', 'short_term_ga', 1.25, 0.125, 1.25, 1.25, 5.00, 200000, 2.50, 0.250, 18.75, 1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5, 4096
UNION ALL
SELECT 'gemini-3.5-flash-lite', 'google', 'long_term_ga', 0.075, 0.0075, 0.075, 0.075, 0.30, 200000, 0.15, 0.015, 1.125, 1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5, 1024
UNION ALL
SELECT 'gemini-3.8-flash', 'google', 'long_term_ga', 0.15, 0.015, 0.15, 0.15, 0.60, 200000, 0.30, 0.030, 2.25, 1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5, 1024
UNION ALL
SELECT 'text-embedding-005', 'google', 'long_term_ga', 0.025, 0.0, 0.0, 0.0, 0.0, CAST(NULL AS INT64), 0.025, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.8, 0.5, 0
UNION ALL
SELECT 'ai_evaluate_agent_managed', 'google', 'long_term_ga', 0.15, 0.015, 0.15, 0.15, 0.60, 200000, 0.30, 0.030, 2.25, 1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5, 0
UNION ALL
SELECT 'claude-sonnet-4-6', 'anthropic', 'long_term_ga', 3.00, 0.30, 3.75, 6.00, 15.00, CAST(NULL AS INT64), 3.00, 0.30, 15.00, 1.0, 0.1, 1.25, 2.0, 5.0, 1.0, 0.1, 5.0, 1.8, 0.5, 1024
UNION ALL
SELECT 'claude-opus-4-6', 'anthropic', 'long_term_ga', 15.00, 1.50, 18.75, 30.00, 75.00, CAST(NULL AS INT64), 15.00, 1.50, 75.00, 1.0, 0.1, 1.25, 2.0, 5.0, 1.0, 0.1, 5.0, 1.8, 0.5, 2048
UNION ALL
SELECT 'claude-sonnet-5-5', 'anthropic', 'long_term_ga', 3.00, 0.30, 3.75, 6.00, 15.00, CAST(NULL AS INT64), 3.00, 0.30, 15.00, 1.0, 0.1, 1.25, 2.0, 5.0, 1.0, 0.1, 5.0, 1.8, 0.5, 1024;

MERGE INTO `agent_eval.model_pricing_catalog` AS target
USING (
  SELECT
    'gemini-3.5-flash-lite' AS model_name, 'google' AS provider, 'long_term_ga' AS lifecycle_track,
    TIMESTAMP '2026-01-01 00:00:00 UTC' AS effective_start, TIMESTAMP '2099-12-31 23:59:59 UTC' AS effective_end,
    0.075 AS input_usd_per_1m, 0.0075 AS cached_input_usd_per_1m, 0.075 AS cache_write_5m_usd_per_1m, 0.075 AS cache_write_1h_usd_per_1m, 0.30 AS output_usd_per_1m,
    200000 AS long_context_threshold_tokens, 0.15 AS long_context_input_usd_per_1m, 0.015 AS long_context_cached_usd_per_1m, 1.125 AS long_context_output_usd_per_1m,
    1.0 AS burndown_uncached_weight, 0.1 AS burndown_cached_weight, 1.0 AS burndown_cache_write_5m_weight, 1.0 AS burndown_cache_write_1h_weight, 4.0 AS burndown_output_weight,
    2.0 AS burndown_long_uncached_weight, 0.2 AS burndown_long_cached_weight, 7.5 AS burndown_long_output_weight,
    1.8 AS priority_paygo_multiplier, 0.5 AS flex_paygo_multiplier, 1024 AS min_cache_tokens
  UNION ALL
  SELECT 'gemini-3.8-flash', 'google', 'long_term_ga', TIMESTAMP '2026-01-01 00:00:00 UTC', TIMESTAMP '2099-12-31 23:59:59 UTC', 0.15, 0.015, 0.15, 0.15, 0.60, 200000, 0.30, 0.030, 2.25, 1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5, 1024
  UNION ALL
  SELECT 'gemini-4-argon', 'google', 'long_term_ga', TIMESTAMP '2026-01-01 00:00:00 UTC', TIMESTAMP '2099-12-31 23:59:59 UTC', 1.25, 0.125, 1.25, 1.25, 5.00, 200000, 2.50, 0.250, 18.75, 1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5, 4096
  UNION ALL
  SELECT 'gemini-4-carbon', 'google', 'short_term_ga', TIMESTAMP '2026-01-01 00:00:00 UTC', TIMESTAMP '2099-12-31 23:59:59 UTC', 1.25, 0.125, 1.25, 1.25, 5.00, 200000, 2.50, 0.250, 18.75, 1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5, 4096
  UNION ALL
  SELECT 'text-embedding-005', 'google', 'long_term_ga', TIMESTAMP '2026-01-01 00:00:00 UTC', TIMESTAMP '2099-12-31 23:59:59 UTC', 0.025, 0.0, 0.0, 0.0, 0.0, CAST(NULL AS INT64), 0.025, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.8, 0.5, 0
  UNION ALL
  SELECT 'ai_evaluate_agent_managed', 'google', 'long_term_ga', TIMESTAMP '2026-01-01 00:00:00 UTC', TIMESTAMP '2099-12-31 23:59:59 UTC', 0.15, 0.015, 0.15, 0.15, 0.60, 200000, 0.30, 0.030, 2.25, 1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5, 0
  UNION ALL
  SELECT 'claude-sonnet-4-6', 'anthropic', 'long_term_ga', TIMESTAMP '2026-01-01 00:00:00 UTC', TIMESTAMP '2099-12-31 23:59:59 UTC', 3.00, 0.30, 3.75, 6.00, 15.00, CAST(NULL AS INT64), 3.00, 0.30, 15.00, 1.0, 0.1, 1.25, 2.0, 5.0, 1.0, 0.1, 5.0, 1.8, 0.5, 1024
  UNION ALL
  SELECT 'claude-opus-4-6', 'anthropic', 'long_term_ga', TIMESTAMP '2026-01-01 00:00:00 UTC', TIMESTAMP '2099-12-31 23:59:59 UTC', 15.00, 1.50, 18.75, 30.00, 75.00, CAST(NULL AS INT64), 15.00, 1.50, 75.00, 1.0, 0.1, 1.25, 2.0, 5.0, 1.0, 0.1, 5.0, 1.8, 0.5, 2048
  UNION ALL
  SELECT 'claude-sonnet-5-5', 'anthropic', 'long_term_ga', TIMESTAMP '2026-01-01 00:00:00 UTC', TIMESTAMP '2099-12-31 23:59:59 UTC', 3.00, 0.30, 3.75, 6.00, 15.00, CAST(NULL AS INT64), 3.00, 0.30, 15.00, 1.0, 0.1, 1.25, 2.0, 5.0, 1.0, 0.1, 5.0, 1.8, 0.5, 1024
) AS src
ON target.model_name = src.model_name
  AND target.effective_start = src.effective_start
WHEN MATCHED THEN
  UPDATE SET min_cache_tokens = src.min_cache_tokens
WHEN NOT MATCHED THEN
  INSERT (
    model_name, provider, lifecycle_track, effective_start, effective_end,
    input_usd_per_1m, cached_input_usd_per_1m, cache_write_5m_usd_per_1m, cache_write_1h_usd_per_1m, output_usd_per_1m,
    long_context_threshold_tokens, long_context_input_usd_per_1m, long_context_cached_usd_per_1m, long_context_output_usd_per_1m,
    burndown_uncached_weight, burndown_cached_weight, burndown_cache_write_5m_weight, burndown_cache_write_1h_weight, burndown_output_weight,
    burndown_long_uncached_weight, burndown_long_cached_weight, burndown_long_output_weight,
    priority_paygo_multiplier, flex_paygo_multiplier, min_cache_tokens
  )
  VALUES (
    src.model_name, src.provider, src.lifecycle_track, src.effective_start, src.effective_end,
    src.input_usd_per_1m, src.cached_input_usd_per_1m, src.cache_write_5m_usd_per_1m, src.cache_write_1h_usd_per_1m, src.output_usd_per_1m,
    src.long_context_threshold_tokens, src.long_context_input_usd_per_1m, src.long_context_cached_usd_per_1m, src.long_context_output_usd_per_1m,
    src.burndown_uncached_weight, src.burndown_cached_weight, src.burndown_cache_write_5m_weight, src.burndown_cache_write_1h_weight, src.burndown_output_weight,
    src.burndown_long_uncached_weight, src.burndown_long_cached_weight, src.burndown_long_output_weight,
    src.priority_paygo_multiplier, src.flex_paygo_multiplier, src.min_cache_tokens
  );
