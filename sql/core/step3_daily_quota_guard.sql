-- Gold Step 3 Day-2 Judge Token Quota Guard, Run Audit Log & Executable Procedure (`S-18`, `S-19`, `S-23`, `S-26`).

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

CREATE TABLE IF NOT EXISTS `agent_eval.eval_daily_quota_state` (
  quota_date_utc DATE NOT NULL OPTIONS (description = 'UTC calendar date of the serialized quota lock row.'),
  updated_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC timestamp of the last serialized reservation lock.')
)
OPTIONS (
  description = 'Per-day mutable lock row acquiring a BigQuery transaction write lock to serialize concurrent quota reservations (S-26).'
);

CREATE TABLE IF NOT EXISTS `agent_eval.eval_quota_run_log` (
  run_log_id STRING NOT NULL OPTIONS (description = 'Unique procedure execution log ID.'),
  eval_run_id STRING NOT NULL OPTIONS (description = 'Evaluation run ID passed to or derived by the procedure.'),
  rubric_id STRING NOT NULL OPTIONS (description = 'Target rubric identifier.'),
  quota_date_utc DATE NOT NULL OPTIONS (description = 'Pinned single UTC quota date used across the run.'),
  selected_count INT64 NOT NULL OPTIONS (description = 'Number of candidate traces selected before reservation.'),
  reserved_tokens INT64 NOT NULL OPTIONS (description = 'Total batch tokens requested or reserved.'),
  exit_reason STRING NOT NULL OPTIONS (description = 'Deterministic exit reason: NO_CANDIDATES, QUOTA_EXCEEDED, RESERVATION_CONFLICT, COMMITTED, or FAILED_BILLED.'),
  logged_at TIMESTAMP NOT NULL OPTIONS (description = 'UTC audit log timestamp.')
)
PARTITION BY quota_date_utc
CLUSTER BY rubric_id, exit_reason
OPTIONS (
  description = 'Auditable exit-reason log for scheduled quota-guarded judge runs (S-23).'
);

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
    AND NOT COALESCE(is_synthetic, FALSE)
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
    AND NOT COALESCE(is_synthetic, FALSE)
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
  DECLARE v_now_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE v_quota_date_utc DATE DEFAULT DATE(v_now_utc);
  DECLARE v_lookback_hours INT64 DEFAULT LEAST(168, GREATEST(1, COALESCE(p_lookback_hours, 24)));
  DECLARE v_rubric_model STRING DEFAULT NULL;
  DECLARE v_remote_model_fqn STRING DEFAULT NULL;
  DECLARE v_eval_run_id STRING DEFAULT COALESCE(
    NULLIF(TRIM(p_eval_run_id), ''),
    CONCAT('sched_', FORMAT_DATE('%Y%m%d', v_quota_date_utc), '_', COALESCE(p_rubric_id, 'unknown'))
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

  SET v_rubric_model = (
    SELECT judge_model
    FROM `agent_eval.rubrics`
    WHERE rubric_id = p_rubric_id
    LIMIT 1
  );
  IF v_rubric_model IS NULL THEN
    RAISE USING MESSAGE = CONCAT('Unknown p_rubric_id in agent_eval.rubrics: ', COALESCE(p_rubric_id, 'NULL'));
  END IF;
  SET v_remote_model_fqn = IF(
    v_rubric_model = 'gemini-4-argon',
    '`agent_eval.gemini_4_argon_judge`',
    '`agent_eval.gemini_3_8_flash_judge`'
  );

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
      WHERE DATE(evaluated_at) >= DATE_SUB(v_quota_date_utc, INTERVAL 7 DAY)
        AND rubric_id = p_rubric_id
        AND NOT COALESCE(is_synthetic, FALSE)
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
        AND cand.trace_start_time >= TIMESTAMP_SUB(v_now_utc, INTERVAL v_lookback_hours HOUR)
        AND MOD(ABS(FARM_FINGERPRINT(CONCAT(v_eval_run_id, ':', cand.trace_id))), 100) < LEAST(100, GREATEST(0, COALESCE(p_canary_sample_pct, 10)))
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
      INSERT INTO `agent_eval.eval_quota_run_log` (
        run_log_id, eval_run_id, rubric_id, quota_date_utc, selected_count, reserved_tokens, exit_reason, logged_at
      )
      VALUES (GENERATE_UUID(), v_eval_run_id, p_rubric_id, v_quota_date_utc, 0, 0, 'NO_CANDIDATES', CURRENT_TIMESTAMP());
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
        MERGE INTO `agent_eval.eval_daily_quota_state` AS target
        USING (
          SELECT
            v_quota_date_utc AS quota_date_utc,
            CURRENT_TIMESTAMP() AS updated_at
        ) AS src
        ON target.quota_date_utc = src.quota_date_utc
        WHEN MATCHED THEN
          UPDATE SET updated_at = src.updated_at
        WHEN NOT MATCHED THEN
          INSERT (quota_date_utc, updated_at)
          VALUES (src.quota_date_utc, src.updated_at);

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
              AND NOT COALESCE(is_synthetic, FALSE)
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
          SET updated_at = CURRENT_TIMESTAMP()
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
    INSERT INTO `agent_eval.eval_quota_run_log` (
      run_log_id, eval_run_id, rubric_id, quota_date_utc, selected_count, reserved_tokens, exit_reason, logged_at
    )
    VALUES (
      GENERATE_UUID(),
      v_eval_run_id,
      p_rubric_id,
      v_quota_date_utc,
      v_selected_count,
      v_batch_tokens,
      IF(COALESCE(v_quota_exceeded, FALSE), 'QUOTA_EXCEEDED', 'RESERVATION_CONFLICT'),
      CURRENT_TIMESTAMP()
    );
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
        rubric_version,
        judge_model,
        sampling_stratum,
        rule0_pass,
        reasoning_trace,
        ordinal_score,
        binary_pass,
        judge_status,
        judge_input_tokens,
        judge_output_tokens,
        judge_thinking_tokens,
        is_synthetic,
        evaluated_at
      )
      SELECT
        GENERATE_UUID() AS eval_id,
        @eval_run_id AS eval_run_id,
        trace_id,
        outcome_id,
        apphub_application_id,
        rubric_id,
        rubric_version,
        @judge_model AS judge_model,
        @sample_stratum AS sampling_stratum,
        step1_pass AS rule0_pass,
        IF(
          COALESCE(status, '') = '' AND ordinal_score BETWEEN 1 AND 4,
          reasoning_trace,
          CONCAT('REJECTED: ', COALESCE(NULLIF(status, ''), 'invalid_ordinal_score'))
        ) AS reasoning_trace,
        IF(COALESCE(status, '') = '' AND ordinal_score BETWEEN 1 AND 4, ordinal_score, CAST(NULL AS INT64)) AS ordinal_score,
        IF(COALESCE(status, '') = '' AND ordinal_score BETWEEN 1 AND 4, (ordinal_score >= passing_score), CAST(NULL AS BOOL)) AS binary_pass,
        IF(COALESCE(status, '') = '' AND ordinal_score BETWEEN 1 AND 4, 'SCORED', 'REJECTED') AS judge_status,
        COALESCE(
          SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.promptTokenCount') AS INT64),
          DIV(LENGTH(prompt), 4)
        ) AS judge_input_tokens,
        COALESCE(
          SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.candidatesTokenCount') AS INT64),
          IF(COALESCE(status, '') = '' AND ordinal_score BETWEEN 1 AND 4, DIV(LENGTH(COALESCE(reasoning_trace, '')), 4) + 12, 0)
        ) AS judge_output_tokens,
        COALESCE(
          SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.thoughtsTokenCount') AS INT64),
          @thinking_headroom
        ) AS judge_thinking_tokens,
        FALSE AS is_synthetic,
        CURRENT_TIMESTAMP() AS evaluated_at
      FROM AI.GENERATE_TABLE(
        MODEL """,
      v_remote_model_fqn,
      """,
        (SELECT * EXCEPT (est_call_tokens) FROM _quota_selected_candidates),
        STRUCT(
          'reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL' AS output_schema,
          '{"generation_config": {"temperature": 0.0, "seed": 42, "max_output_tokens": """,
      CAST(v_max_out_tokens AS STRING),
      """, "thinking_config": {"thinking_level": \"""",
      v_thinking_level,
      """\"}}}' AS model_params
        )
      )
      """
    )
    USING
      v_eval_run_id AS eval_run_id,
      v_rubric_model AS judge_model,
      v_sample_stratum AS sample_stratum,
      v_thinking_headroom AS thinking_headroom;

    UPDATE `agent_eval.eval_daily_quota_reservations`
    SET status = 'COMMITTED', updated_at = CURRENT_TIMESTAMP()
    WHERE reservation_id = v_reservation_id
      AND quota_date_utc = v_quota_date_utc;

    INSERT INTO `agent_eval.eval_quota_run_log` (
      run_log_id, eval_run_id, rubric_id, quota_date_utc, selected_count, reserved_tokens, exit_reason, logged_at
    )
    VALUES (
      GENERATE_UUID(), v_eval_run_id, p_rubric_id, v_quota_date_utc, v_selected_count, v_batch_tokens, 'COMMITTED', CURRENT_TIMESTAMP()
    );
  EXCEPTION WHEN ERROR THEN
    UPDATE `agent_eval.eval_daily_quota_reservations`
    SET status = 'FAILED_BILLED', updated_at = CURRENT_TIMESTAMP()
    WHERE reservation_id = v_reservation_id
      AND quota_date_utc = v_quota_date_utc;

    INSERT INTO `agent_eval.eval_quota_run_log` (
      run_log_id, eval_run_id, rubric_id, quota_date_utc, selected_count, reserved_tokens, exit_reason, logged_at
    )
    VALUES (
      GENERATE_UUID(), v_eval_run_id, p_rubric_id, v_quota_date_utc, v_selected_count, v_batch_tokens, 'FAILED_BILLED', CURRENT_TIMESTAMP()
    );
    RAISE;
  END;
END;
