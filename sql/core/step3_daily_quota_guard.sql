-- =============================================================================
-- BQ Eval — Step 3 Day-2 Judge Token Quota Guard & Executable Procedure
--   * Enforces global + per-application daily judge token sub-caps so a single
--     noisy application cannot starve the rest of the fleet.
--   * Accounts for pre-flight `max_output_tokens` + `thinking_level` headroom
--     before invoking `AI.GENERATE_TABLE` (blocking 2x output/thinking overruns).
--   * Uses non-deduped burn accounting over `eval_results` (retaining billed
--     retries) plus atomic reservation rows in `eval_daily_quota_reservations`
--     to prevent concurrent TOCTOU overspend and retry storms.
--   * Partition-prunes `eval_results` dedup joins and supports configurable
--     rubric selection via `CALL agent_eval.run_daily_quota_guarded_judge(...)`.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Global & Per-Application Daily Quota Config (`agent_eval.eval_daily_quota_config`)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.eval_daily_quota_config` (
  quota_name STRING NOT NULL OPTIONS (description = 'Unique daily quota profile name (e.g., default_step2_daily_cap or per-app override).'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Target application ID, or * for the global fleet cap.'),
  daily_judge_token_cap INT64 NOT NULL OPTIONS (description = 'Maximum total judge tokens (input + output + thinking) permitted per UTC day.'),
  per_app_default_token_cap INT64 NOT NULL OPTIONS (description = 'Default per-application daily token sub-cap preventing noisy-neighbor starvation.'),
  max_output_tokens INT64 NOT NULL OPTIONS (description = 'Maximum judge output token headroom reserved per trace call.'),
  thinking_budget_tokens INT64 NOT NULL OPTIONS (description = 'Pre-flight thinking token headroom reserved per trace call.'),
  updated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC timestamp when the daily quota cap was last updated.')
)
OPTIONS (
  description = 'Global and per-application daily token budget guardrails for scheduled Step-2 judge runs.'
);

MERGE INTO `agent_eval.eval_daily_quota_config` AS target
USING (
  SELECT
    'default_step2_daily_cap' AS quota_name,
    '*' AS apphub_application_id,
    2000000 AS daily_judge_token_cap,
    500000 AS per_app_default_token_cap,
    2048 AS max_output_tokens,
    1024 AS thinking_budget_tokens,
    CURRENT_TIMESTAMP() AS updated_at
) AS src
ON target.quota_name = src.quota_name
WHEN NOT MATCHED THEN
  INSERT (
    quota_name,
    apphub_application_id,
    daily_judge_token_cap,
    per_app_default_token_cap,
    max_output_tokens,
    thinking_budget_tokens,
    updated_at
  )
  VALUES (
    src.quota_name,
    src.apphub_application_id,
    src.daily_judge_token_cap,
    src.per_app_default_token_cap,
    src.max_output_tokens,
    src.thinking_budget_tokens,
    src.updated_at
  );

-- -----------------------------------------------------------------------------
-- 2. Atomic Quota Reservation Ledger (`agent_eval.eval_daily_quota_reservations`)
--    * Written inside a BigQuery transaction before calling AI.GENERATE_TABLE
--    * Status lifecycle: RESERVED -> COMMITTED (or FAILED_BILLED on error)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.eval_daily_quota_reservations` (
  reservation_id STRING NOT NULL OPTIONS (description = 'Unique atomic batch reservation ID.'),
  quota_date_utc DATE NOT NULL OPTIONS (description = 'UTC calendar date of the reservation.'),
  apphub_application_id STRING NOT NULL OPTIONS (description = 'Application owning the reserved token slice.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Rubric evaluated under this reservation.'),
  reserved_tokens INT64 NOT NULL OPTIONS (description = 'Pre-flight prompt + output + thinking tokens reserved before judge dispatch.'),
  status STRING NOT NULL OPTIONS (description = 'Reservation state: RESERVED, COMMITTED, FAILED_BILLED, or RELEASED.'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC reservation creation timestamp.'),
  updated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC reservation last update timestamp.')
)
PARTITION BY quota_date_utc
CLUSTER BY apphub_application_id, status
OPTIONS (
  description = 'Atomic pre-flight judge token reservation ledger preventing concurrent TOCTOU overspend and retry storms.'
);

-- -----------------------------------------------------------------------------
-- 2B. Per-Day Serialized Quota Counter (`agent_eval.eval_daily_quota_state`)
--     * BigQuery multi-statement transactions only detect concurrency conflicts
--       on mutating DML (UPDATE/MERGE/DELETE), not pure INSERT appends.
--     * Updating this single per-day row inside BEGIN TRANSACTION serializes
--       concurrent CALLs on the same UTC date via optimistic concurrency control.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval.eval_daily_quota_state` (
  quota_date_utc DATE NOT NULL OPTIONS (description = 'UTC calendar date of the serialized quota counter.'),
  reserved_tokens INT64 NOT NULL OPTIONS (description = 'Cumulative pre-flight tokens reserved on this UTC date.'),
  updated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC timestamp of the last serialized reservation.')
)
OPTIONS (
  description = 'Per-day mutable counter row acquiring a BigQuery transaction write lock to serialize concurrent quota reservations.'
);

-- -----------------------------------------------------------------------------
-- 3. Global Real-Time Daily Quota Status (`agent_eval.v_step2_daily_quota_status`)
--    * Non-deduped burn accounting: retains every billed Engine A & Engine B
--      retry row in `eval_results` plus active/failed-billed reservations.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step2_daily_quota_status` AS
WITH active_config AS (
  SELECT
    daily_judge_token_cap,
    per_app_default_token_cap,
    max_output_tokens,
    thinking_budget_tokens
  FROM `agent_eval.eval_daily_quota_config`
  WHERE quota_name = 'default_step2_daily_cap'
    AND apphub_application_id = '*'
  QUALIFY ROW_NUMBER() OVER (ORDER BY updated_at DESC) = 1
),
today_billed_usage AS (
  SELECT
    COALESCE(
      SUM(judge_input_tokens + judge_output_tokens + judge_thinking_tokens),
      0
    ) AS tokens_used_today
  FROM `agent_eval.eval_results`
  WHERE DATE(evaluated_at) = CURRENT_DATE('UTC')
),
today_reservations AS (
  SELECT
    COALESCE(SUM(reserved_tokens), 0) AS tokens_reserved_today
  FROM `agent_eval.eval_daily_quota_reservations`
  WHERE quota_date_utc = CURRENT_DATE('UTC')
    AND status IN ('RESERVED', 'FAILED_BILLED')
)
SELECT
  CURRENT_DATE('UTC') AS quota_date_utc,
  cfg.daily_judge_token_cap,
  cfg.per_app_default_token_cap,
  usage.tokens_used_today,
  res.tokens_reserved_today,
  usage.tokens_used_today + res.tokens_reserved_today AS tokens_burned_and_reserved_today,
  GREATEST(
    0,
    cfg.daily_judge_token_cap - (usage.tokens_used_today + res.tokens_reserved_today)
  ) AS tokens_remaining_today,
  (usage.tokens_used_today + res.tokens_reserved_today) < cfg.daily_judge_token_cap AS quota_available
FROM active_config AS cfg
CROSS JOIN today_billed_usage AS usage
CROSS JOIN today_reservations AS res;

-- -----------------------------------------------------------------------------
-- 4. Per-Application Daily Quota Sub-Cap Status (`agent_eval.v_step2_app_daily_quota_status`)
--    * Enforces per-app sub-caps so one noisy application cannot starve others.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_step2_app_daily_quota_status` AS
WITH global_cfg AS (
  SELECT
    per_app_default_token_cap
  FROM `agent_eval.eval_daily_quota_config`
  WHERE quota_name = 'default_step2_daily_cap'
    AND apphub_application_id = '*'
  QUALIFY ROW_NUMBER() OVER (ORDER BY updated_at DESC) = 1
),
app_caps AS (
  SELECT
    apphub_application_id,
    daily_judge_token_cap AS app_token_cap
  FROM `agent_eval.eval_daily_quota_config`
  WHERE apphub_application_id != '*'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY apphub_application_id ORDER BY updated_at DESC) = 1
),
app_billed_usage AS (
  SELECT
    apphub_application_id,
    COALESCE(
      SUM(judge_input_tokens + judge_output_tokens + judge_thinking_tokens),
      0
    ) AS app_tokens_used_today
  FROM `agent_eval.eval_results`
  WHERE DATE(evaluated_at) = CURRENT_DATE('UTC')
  GROUP BY apphub_application_id
),
app_reservations AS (
  SELECT
    apphub_application_id,
    COALESCE(SUM(reserved_tokens), 0) AS app_tokens_reserved_today
  FROM `agent_eval.eval_daily_quota_reservations`
  WHERE quota_date_utc = CURRENT_DATE('UTC')
    AND status IN ('RESERVED', 'FAILED_BILLED')
  GROUP BY apphub_application_id
),
known_apps AS (
  SELECT apphub_application_id FROM app_caps
  UNION DISTINCT
  SELECT apphub_application_id FROM app_billed_usage
  UNION DISTINCT
  SELECT apphub_application_id FROM app_reservations
)
SELECT
  CURRENT_DATE('UTC') AS quota_date_utc,
  k.apphub_application_id,
  COALESCE(ac.app_token_cap, gc.per_app_default_token_cap) AS app_daily_token_cap,
  COALESCE(u.app_tokens_used_today, 0) AS app_tokens_used_today,
  COALESCE(r.app_tokens_reserved_today, 0) AS app_tokens_reserved_today,
  GREATEST(
    0,
    COALESCE(ac.app_token_cap, gc.per_app_default_token_cap)
      - (COALESCE(u.app_tokens_used_today, 0) + COALESCE(r.app_tokens_reserved_today, 0))
  ) AS app_tokens_remaining_today,
  (COALESCE(u.app_tokens_used_today, 0) + COALESCE(r.app_tokens_reserved_today, 0))
    < COALESCE(ac.app_token_cap, gc.per_app_default_token_cap) AS app_quota_available
FROM known_apps AS k
CROSS JOIN global_cfg AS gc
LEFT JOIN app_caps AS ac
  ON k.apphub_application_id = ac.apphub_application_id
LEFT JOIN app_billed_usage AS u
  ON k.apphub_application_id = u.apphub_application_id
LEFT JOIN app_reservations AS r
  ON k.apphub_application_id = r.apphub_application_id;

-- -----------------------------------------------------------------------------
-- 5. Executable Day-2 Quota-Guarded Judge Procedure (`agent_eval.run_daily_quota_guarded_judge`)
--    * Atomically reserves pre-flight prompt + max_output_tokens + thinking headroom
--      per application inside a transaction before calling AI.GENERATE_TABLE.
--    * Partition-prunes `eval_results` dedup join to the last 7 UTC days.
--    * Marks failed batches `FAILED_BILLED` so retry storms still trip the cap.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE `agent_eval.run_daily_quota_guarded_judge`(
  IN p_rubric_id STRING,
  IN p_canary_sample_pct INT64,
  IN p_max_output_tokens INT64,
  IN p_thinking_level STRING,
  IN p_lookback_hours INT64,
  IN p_eval_run_id STRING
)
BEGIN
  DECLARE v_reservation_id STRING DEFAULT GENERATE_UUID();
  DECLARE v_quota_date_utc DATE DEFAULT CURRENT_DATE('UTC');
  DECLARE v_eval_run_id STRING DEFAULT COALESCE(
    NULLIF(TRIM(p_eval_run_id), ''),
    CONCAT('sched_', FORMAT_DATE('%Y%m%d', v_quota_date_utc), '_', p_rubric_id)
  );
  DECLARE v_thinking_level STRING DEFAULT (
    CASE UPPER(COALESCE(NULLIF(TRIM(p_thinking_level), ''), 'LOW'))
      WHEN 'HIGH' THEN 'HIGH'
      WHEN 'MEDIUM' THEN 'MEDIUM'
      ELSE 'LOW'
    END
  );
  DECLARE v_max_out_tokens INT64 DEFAULT GREATEST(1, LEAST(32768, COALESCE(p_max_output_tokens, 2048)));
  DECLARE v_sample_stratum STRING DEFAULT CONCAT(
    'CANARY_',
    CAST(LEAST(100, GREATEST(0, COALESCE(p_canary_sample_pct, 10))) AS STRING),
    'PCT'
  );
  DECLARE v_thinking_headroom INT64 DEFAULT (
    CASE v_thinking_level
      WHEN 'HIGH' THEN 8192
      WHEN 'MEDIUM' THEN 4096
      ELSE 1024
    END
  );
  DECLARE v_call_headroom_tokens INT64 DEFAULT v_max_out_tokens + v_thinking_headroom;
  DECLARE v_selected_count INT64 DEFAULT 0;
  DECLARE v_batch_tokens INT64 DEFAULT 0;
  DECLARE v_app_batches ARRAY<STRUCT<apphub_application_id STRING, batch_tokens INT64>>;
  DECLARE v_reserved BOOL DEFAULT FALSE;
  DECLARE v_quota_exceeded BOOL DEFAULT FALSE;
  DECLARE v_attempt INT64 DEFAULT 0;

  WHILE v_attempt < 3 AND NOT v_reserved DO
    SET v_attempt = v_attempt + 1;

    CREATE OR REPLACE TEMP TABLE _quota_selected_candidates AS
    WITH global_quota AS (
      SELECT
        tokens_remaining_today AS global_remaining_today,
        per_app_default_token_cap
      FROM `agent_eval.v_step2_daily_quota_status`
    ),
    recent_scored AS (
      SELECT DISTINCT trace_id, rubric_id
      FROM `agent_eval.eval_results`
      WHERE DATE(evaluated_at) >= DATE_SUB(CURRENT_DATE('UTC'), INTERVAL 7 DAY)
        AND rubric_id = p_rubric_id
    ),
    raw_candidates AS (
      SELECT
        cand.*,
        DIV(LENGTH(cand.prompt), 4) + v_call_headroom_tokens AS est_call_tokens,
        COALESCE(app_q.app_tokens_remaining_today, gq.per_app_default_token_cap) AS app_remaining_today,
        gq.global_remaining_today
      FROM `agent_eval.v_step2_trace_rubric_prompts` AS cand
      CROSS JOIN global_quota AS gq
      LEFT JOIN `agent_eval.v_step2_app_daily_quota_status` AS app_q
        ON cand.apphub_application_id = app_q.apphub_application_id
      LEFT JOIN recent_scored AS existing
        ON cand.trace_id = existing.trace_id
        AND cand.rubric_id = existing.rubric_id
      WHERE cand.rubric_id = p_rubric_id
        AND cand.trace_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL GREATEST(1, COALESCE(p_lookback_hours, 24)) HOUR)
        AND MOD(ABS(FARM_FINGERPRINT(cand.trace_id)), 100) < LEAST(100, GREATEST(0, COALESCE(p_canary_sample_pct, 10)))
        AND existing.trace_id IS NULL
    ),
    app_budgeted AS (
      SELECT
        *,
        SUM(est_call_tokens) OVER (
          PARTITION BY apphub_application_id
          ORDER BY trace_start_time, trace_id
        ) AS app_cumulative_tokens
      FROM raw_candidates
    ),
    global_budgeted AS (
      SELECT
        *,
        SUM(est_call_tokens) OVER (
          ORDER BY trace_start_time, apphub_application_id, trace_id
        ) AS global_cumulative_tokens
      FROM app_budgeted
      WHERE app_cumulative_tokens <= app_remaining_today
    )
    SELECT * EXCEPT (app_cumulative_tokens, global_cumulative_tokens, app_remaining_today, global_remaining_today)
    FROM global_budgeted
    WHERE global_cumulative_tokens <= global_remaining_today;

    SET v_selected_count = (SELECT COUNT(*) FROM _quota_selected_candidates);

    IF v_selected_count = 0 THEN
      RETURN;
    END IF;

    SET v_batch_tokens = (SELECT COALESCE(SUM(est_call_tokens), 0) FROM _quota_selected_candidates);
    SET v_app_batches = ARRAY(
      SELECT AS STRUCT
        apphub_application_id,
        SUM(est_call_tokens) AS batch_tokens
      FROM _quota_selected_candidates
      GROUP BY apphub_application_id
    );

    BEGIN
      BEGIN TRANSACTION;
        -- Mutate the single per-day counter row so concurrent CALLs conflict and serialize
        -- under BigQuery optimistic concurrency control (zero TEMP table refs in transaction).
        MERGE INTO `agent_eval.eval_daily_quota_state` AS target
        USING (
          SELECT
            v_quota_date_utc AS quota_date_utc,
            0 AS reserved_tokens,
            CURRENT_TIMESTAMP() AS updated_at
        ) AS src
        ON target.quota_date_utc = src.quota_date_utc
        WHEN MATCHED THEN
          UPDATE SET updated_at = src.updated_at
        WHEN NOT MATCHED THEN
          INSERT (quota_date_utc, reserved_tokens, updated_at)
          VALUES (src.quota_date_utc, src.reserved_tokens, src.updated_at);

        SET v_quota_exceeded = (
          WITH global_cfg AS (
            SELECT daily_judge_token_cap, per_app_default_token_cap
            FROM `agent_eval.eval_daily_quota_config`
            WHERE quota_name = 'default_step2_daily_cap'
              AND apphub_application_id = '*'
            QUALIFY ROW_NUMBER() OVER (ORDER BY updated_at DESC) = 1
          ),
          app_caps AS (
            SELECT apphub_application_id, daily_judge_token_cap AS app_token_cap
            FROM `agent_eval.eval_daily_quota_config`
            WHERE apphub_application_id != '*'
            QUALIFY ROW_NUMBER() OVER (PARTITION BY apphub_application_id ORDER BY updated_at DESC) = 1
          ),
          billed AS (
            SELECT
              apphub_application_id,
              COALESCE(SUM(judge_input_tokens + judge_output_tokens + judge_thinking_tokens), 0) AS used_tokens
            FROM `agent_eval.eval_results`
            WHERE DATE(evaluated_at) = v_quota_date_utc
            GROUP BY apphub_application_id
          ),
          reserved AS (
            SELECT
              apphub_application_id,
              COALESCE(SUM(reserved_tokens), 0) AS res_tokens
            FROM `agent_eval.eval_daily_quota_reservations`
            WHERE quota_date_utc = v_quota_date_utc
              AND status IN ('RESERVED', 'FAILED_BILLED')
            GROUP BY apphub_application_id
          ),
          global_burn AS (
            SELECT
              COALESCE((SELECT SUM(used_tokens) FROM billed), 0)
                + COALESCE((SELECT SUM(res_tokens) FROM reserved), 0) AS total_burned
          ),
          app_violations AS (
            SELECT
              COUNTIF(
                COALESCE(b.used_tokens, 0) + COALESCE(r.res_tokens, 0) + req.batch_tokens
                > COALESCE(ac.app_token_cap, gc.per_app_default_token_cap)
              ) AS violation_count
            FROM UNNEST(v_app_batches) AS req
            CROSS JOIN global_cfg AS gc
            LEFT JOIN app_caps AS ac
              ON req.apphub_application_id = ac.apphub_application_id
            LEFT JOIN billed AS b
              ON req.apphub_application_id = b.apphub_application_id
            LEFT JOIN reserved AS r
              ON req.apphub_application_id = r.apphub_application_id
          )
          SELECT
            (gb.total_burned + v_batch_tokens > gc.daily_judge_token_cap)
            OR (av.violation_count > 0)
          FROM global_cfg AS gc
          CROSS JOIN global_burn AS gb
          CROSS JOIN app_violations AS av
        );

        IF COALESCE(v_quota_exceeded, TRUE) THEN
          ROLLBACK TRANSACTION;
        ELSE
          UPDATE `agent_eval.eval_daily_quota_state`
          SET
            reserved_tokens = reserved_tokens + v_batch_tokens,
            updated_at = CURRENT_TIMESTAMP()
          WHERE quota_date_utc = v_quota_date_utc;

          INSERT INTO `agent_eval.eval_daily_quota_reservations` (
            reservation_id,
            quota_date_utc,
            apphub_application_id,
            rubric_id,
            reserved_tokens,
            status,
            created_at,
            updated_at
          )
          SELECT
            v_reservation_id,
            v_quota_date_utc,
            apphub_application_id,
            p_rubric_id,
            batch_tokens,
            'RESERVED',
            CURRENT_TIMESTAMP(),
            CURRENT_TIMESTAMP()
          FROM UNNEST(v_app_batches);

          COMMIT TRANSACTION;
          SET v_reserved = TRUE;
        END IF;
    EXCEPTION WHEN ERROR THEN
      BEGIN
        ROLLBACK TRANSACTION;
      EXCEPTION WHEN ERROR THEN
        SET v_reserved = FALSE;
      END;
      SET v_reserved = FALSE;
    END;
  END WHILE;

  IF NOT v_reserved THEN
    RETURN;
  END IF;

  BEGIN
    EXECUTE IMMEDIATE CONCAT(
      """
      INSERT INTO `agent_eval.eval_results` (
        eval_id,
        eval_run_id,
        trace_id,
        outcome_id,
        apphub_application_id,
        rubric_id,
        judge_model,
        sampling_stratum,
        rule0_pass,
        reasoning_trace,
        ordinal_score,
        binary_pass,
        judge_input_tokens,
        judge_output_tokens,
        judge_thinking_tokens,
        evaluated_at
      )
      SELECT
        GENERATE_UUID() AS eval_id,
        @eval_run_id AS eval_run_id,
        trace_id,
        outcome_id,
        apphub_application_id,
        rubric_id,
        'gemini-3.8-flash' AS judge_model,
        @sample_stratum AS sampling_stratum,
        step1_pass AS rule0_pass,
        reasoning_trace,
        ordinal_score,
        (ordinal_score >= passing_score) AS binary_pass,
        COALESCE(
          SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.promptTokenCount') AS INT64),
          DIV(LENGTH(prompt), 4)
        ) AS judge_input_tokens,
        COALESCE(
          SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.candidatesTokenCount') AS INT64),
          DIV(LENGTH(COALESCE(reasoning_trace, '')), 4) + 12
        ) AS judge_output_tokens,
        COALESCE(
          SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.thoughtsTokenCount') AS INT64),
          @thinking_headroom
        ) AS judge_thinking_tokens,
        CURRENT_TIMESTAMP() AS evaluated_at
      FROM AI.GENERATE_TABLE(
        MODEL `agent_eval.gemini_3_8_flash_judge`,
        (SELECT * EXCEPT (est_call_tokens) FROM _quota_selected_candidates),
        STRUCT(
          'reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL' AS output_schema,
          '{"generation_config": {"temperature": 0.0, "max_output_tokens": """,
      CAST(v_max_out_tokens AS STRING),
      """, "thinking_config": {"thinking_level": \"""",
      v_thinking_level,
      """\"}}}' AS model_params
        )
      )
      WHERE COALESCE(status, '') = ''
        AND ordinal_score BETWEEN 1 AND 4
      """
    )
    USING
      v_eval_run_id AS eval_run_id,
      v_sample_stratum AS sample_stratum,
      v_thinking_headroom AS thinking_headroom;

    UPDATE `agent_eval.eval_daily_quota_reservations`
    SET status = 'COMMITTED', updated_at = CURRENT_TIMESTAMP()
    WHERE reservation_id = v_reservation_id
      AND quota_date_utc = v_quota_date_utc;
  EXCEPTION WHEN ERROR THEN
    UPDATE `agent_eval.eval_daily_quota_reservations`
    SET status = 'FAILED_BILLED', updated_at = CURRENT_TIMESTAMP()
    WHERE reservation_id = v_reservation_id
      AND quota_date_utc = v_quota_date_utc;
    RAISE;
  END;
END;
