-- =============================================================================
-- BQ Eval — Optional DocuBridge Reference Demo Extensions
--   * Extracted from core `sql/step1_trace_views.sql` and `sql/views.sql` so the
--     reusable core stays 100% domain-agnostic with zero hardcoded application IDs.
--   * Distinct view names guarantee zero collisions across `sql/*.sql`.
--   * Deploy after `sql/ddl.sql`, `sql/step1_trace_views.sql`, and `sql/views.sql`.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Demo View 1: DocuBridge WORM Storage & Compute Infra Cost (`v_demo_infra_cost_estimate`)
--   * Strictly 1 row per outcome_id (GROUP BY w.outcome_id)
--   * Applies DocuBridge WORM retention proxies for Meridian, Benefits RFP, and KYC
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_demo_infra_cost_estimate` AS
WITH server_span_compute AS (
  SELECT
    w.outcome_id,
    ANY_VALUE(COALESCE(w.outcome_owner, w.apphub_application_id)) AS apphub_application_id,
    SUM(w.latency_ms / 1000.0) AS total_server_seconds,
    SUM(
      (w.latency_ms / 1000.0)
      * (
        (COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."service.vcpu"') AS FLOAT64), 2.0) * 0.00002400)
        + (COALESCE(SAFE_CAST(JSON_VALUE(w.attributes, '$."service.gib"') AS FLOAT64), 2.0) * 0.00000250)
      )
    ) AS estimated_compute_usd,
    MAX(SAFE_CAST(JSON_VALUE(w.attributes, '$."outcome.storage_usd"') AS FLOAT64)) AS explicit_storage_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  WHERE w.outcome_id IS NOT NULL
    AND w.otel_span_kind IN ('2', 'SPAN_KIND_SERVER', 'SPAN_KIND_INTERNAL')
    AND COALESCE(w.apphub_application_id, '') NOT LIKE '%external%'
  GROUP BY w.outcome_id
)
SELECT
  c.outcome_id,
  c.apphub_application_id,
  c.total_server_seconds,
  c.estimated_compute_usd,
  COALESCE(
    c.explicit_storage_usd,
    CASE
      WHEN c.apphub_application_id = 'meridian-loan-consolidation' THEN 0.000400
      WHEN c.apphub_application_id = 'benefits-rfp-procurement' THEN 0.000120
      WHEN c.apphub_application_id = 'kyc-address-verification' THEN 0.000040
      ELSE 0.000020
    END
  ) AS estimated_storage_usd,
  c.estimated_compute_usd + COALESCE(
    c.explicit_storage_usd,
    CASE
      WHEN c.apphub_application_id = 'meridian-loan-consolidation' THEN 0.000400
      WHEN c.apphub_application_id = 'benefits-rfp-procurement' THEN 0.000120
      WHEN c.apphub_application_id = 'kyc-address-verification' THEN 0.000040
      ELSE 0.000020
    END
  ) AS estimated_infra_cost_usd
FROM server_span_compute AS c;

-- -----------------------------------------------------------------------------
-- Demo View 2: DocuBridge Cost per Verified Outcome (`v_demo_cost_per_verified_outcome`)
--   * Uses `v_demo_infra_cost_estimate` WORM proxies + Step-2 `eval_results`
--   * Proportionally allocates 1.0x outcome infra & table-only judge spend across slices
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_demo_cost_per_verified_outcome` AS
WITH step2_judge_by_outcome AS (
  SELECT
    COALESCE(e.outcome_id, t.outcome_id) AS outcome_id,
    LOGICAL_AND(e.binary_pass) AS step2_all_pass,
    SUM(
      (
        (e.judge_input_tokens / 1e6) * COALESCE(
          m.input_usd_per_1m,
          CASE
            WHEN e.judge_model = 'gemini-4-argon' THEN 1.25
            WHEN e.judge_model = 'gemini-3.5-flash-lite' THEN 0.075
            ELSE 0.15
          END
        )
      )
      + (
        ((e.judge_output_tokens + e.judge_thinking_tokens) / 1e6) * COALESCE(
          m.output_usd_per_1m,
          CASE
            WHEN e.judge_model = 'gemini-4-argon' THEN 5.00
            WHEN e.judge_model = 'gemini-3.5-flash-lite' THEN 0.30
            ELSE 0.60
          END
        )
      )
    ) AS eval_table_judge_usd
  FROM `agent_eval.eval_results` AS e
  LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t
    ON e.trace_id = t.trace_id
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m
    ON e.judge_model = m.model_name
    AND e.evaluated_at >= m.effective_start
    AND e.evaluated_at < m.effective_end
  WHERE COALESCE(e.outcome_id, t.outcome_id) IS NOT NULL
  GROUP BY 1
),
outcome_spans AS (
  SELECT
    w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id, 'unregistered_app') AS apphub_application_id,
    COALESCE(w.source_mode, o.source_mode, 'UNSPECIFIED') AS source_mode,
    w.agent_name,
    COALESCE(o.outcome_status, 'open') AS outcome_status,
    CASE
      WHEN COALESCE(o.outcome_status, 'open') != 'succeeded' THEN FALSE
      ELSE COALESCE(j.step2_all_pass, o.is_verified, FALSE)
    END AS is_verified,
    COUNT(DISTINCT w.trace_id) AS trace_count,
    COUNT(*) AS span_count,
    SUM(w.input_tokens) AS input_tokens,
    SUM(w.cached_input_tokens) AS cached_input_tokens,
    SUM(w.output_tokens) AS output_tokens,
    SUM(w.thinking_tokens) AS thinking_tokens,
    SUM(CASE WHEN w.finops_traffic_type != 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END) AS measured_model_cost_usd,
    SUM(CASE WHEN w.finops_traffic_type = 'autorater_judge' THEN w.span_cost_usd ELSE 0.0 END) AS span_judge_cost_usd,
    COALESCE(MAX(j.eval_table_judge_usd), 0.0) AS outcome_table_judge_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o
    ON w.outcome_id = o.outcome_id
  LEFT JOIN step2_judge_by_outcome AS j
    ON w.outcome_id = j.outcome_id
  WHERE w.outcome_id IS NOT NULL
    AND COALESCE(w.apphub_application_id, '') NOT LIKE '%external%'
    AND COALESCE(JSON_VALUE(w.attributes, '$."apphub.external"'), 'false') != 'true'
  GROUP BY 1, 2, 3, 4, 5, 6
),
weighted_outcome_spans AS (
  SELECT
    os.*,
    SUM(os.span_count) OVER (PARTITION BY os.outcome_id) AS outcome_total_spans,
    SUM(os.span_judge_cost_usd) OVER (PARTITION BY os.outcome_id) AS outcome_span_judge_usd
  FROM outcome_spans AS os
),
allocated_outcome_spans AS (
  SELECT
    os.*,
    os.span_judge_cost_usd
      + GREATEST(0.0, os.outcome_table_judge_usd - os.outcome_span_judge_usd)
      * COALESCE(SAFE_DIVIDE(os.span_count, os.outcome_total_spans), 0.0) AS measured_judge_cost_usd,
    COALESCE(inf.estimated_infra_cost_usd, 0.0)
      * COALESCE(SAFE_DIVIDE(os.span_count, os.outcome_total_spans), 0.0) AS allocated_infra_cost_usd
  FROM weighted_outcome_spans AS os
  LEFT JOIN `agent_eval.v_demo_infra_cost_estimate` AS inf
    ON os.outcome_id = inf.outcome_id
)
SELECT
  os.apphub_application_id,
  os.source_mode,
  os.agent_name,
  COUNT(DISTINCT os.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)) AS verified_outcomes,
  COUNT(DISTINCT IF(NOT os.is_verified, os.outcome_id, NULL)) AS unverified_or_failed_outcomes,
  SUM(os.trace_count) AS total_traces,
  SUM(os.input_tokens) AS total_input_tokens,
  SUM(os.output_tokens) AS total_output_tokens,
  SUM(os.thinking_tokens) AS total_thinking_tokens,
  SUM(os.measured_model_cost_usd) AS measured_model_cost_usd,
  SUM(os.measured_judge_cost_usd) AS measured_judge_cost_usd,
  SUM(os.allocated_infra_cost_usd) AS estimated_infra_cost_usd,
  SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd) AS total_ai_cost_usd,
  SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd + os.allocated_infra_cost_usd) AS total_cost_with_infra_usd,
  SAFE_DIVIDE(
    SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd),
    NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0)
  ) AS cost_per_verified_outcome_ai_usd,
  SAFE_DIVIDE(
    SUM(os.measured_model_cost_usd + os.measured_judge_cost_usd + os.allocated_infra_cost_usd),
    NULLIF(COUNT(DISTINCT IF(os.is_verified, os.outcome_id, NULL)), 0)
  ) AS cost_per_verified_outcome_usd
FROM allocated_outcome_spans AS os
GROUP BY
  os.apphub_application_id,
  os.source_mode,
  os.agent_name;

-- -----------------------------------------------------------------------------
-- Demo View 3: DocuBridge Day-1 vs. Day-2 Skill Lineage ROI (`v_demo_skill_lineage_roi`)
--   * Includes DocuBridge Overwatch HITL spans and WORM storage proxy
--   * Proportionally allocates 1.0x outcome infra cost across multi-skill outcomes
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_demo_skill_lineage_roi` AS
WITH deal_outcomes AS (
  SELECT
    COALESCE(o.outcome_parent_id, w.outcome_id) AS deal_outcome_id,
    w.outcome_id,
    COALESCE(o.outcome_owner, w.outcome_owner, w.apphub_application_id) AS apphub_application_id,
    COALESCE(w.skill_id, o.skill_id, 'default-skill') AS skill_id,
    COALESCE(w.skill_version, o.skill_version, '1.0.0') AS skill_version,
    COALESCE(o.is_verified, FALSE) AS is_verified,
    COUNT(*) AS span_count,
    LOGICAL_OR(w.finops_traffic_type = 'conflict_escalation' OR w.model_name = 'gemini-4-argon') AS escalated_to_argon,
    COUNTIF(
      COALESCE(JSON_VALUE(w.attributes, '$."hitl.review"'), '') = 'true'
      OR w.span_name LIKE '%overwatch%'
      OR w.finops_traffic_type = 'conflict_escalation'
    ) AS human_review_spans,
    SUM(CASE WHEN w.finops_traffic_type != 'agent_gym_eval' THEN w.span_cost_usd ELSE 0.0 END) AS deal_model_cost_usd,
    SUM(CASE WHEN w.finops_traffic_type = 'agent_gym_eval' THEN w.span_cost_usd ELSE 0.0 END) AS agent_gym_compile_cost_usd
  FROM `agent_eval.v_trace_waterfall` AS w
  LEFT JOIN `agent_eval.v_outcomes` AS o
    ON w.outcome_id = o.outcome_id
  WHERE w.outcome_id IS NOT NULL
  GROUP BY 1, 2, 3, 4, 5, 6
),
weighted_deal_outcomes AS (
  SELECT
    d.*,
    SUM(d.span_count) OVER (PARTITION BY d.outcome_id) AS outcome_total_spans
  FROM deal_outcomes AS d
)
SELECT
  d.apphub_application_id,
  d.skill_id,
  d.skill_version,
  CASE
    WHEN d.skill_version = '1.0.0' THEN 'DAY_1_BASELINE'
    ELSE 'DAY_2_COMPILED_SKILL'
  END AS skill_stage,
  COUNT(DISTINCT d.deal_outcome_id) AS total_deals,
  COUNT(DISTINCT d.outcome_id) AS total_outcomes,
  COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)) AS verified_outcomes,
  COUNTIF(d.escalated_to_argon) AS argon_escalated_outcomes,
  SUM(IF(d.human_review_spans > 0, 1, 0)) AS human_reviews,
  SAFE_DIVIDE(
    SUM(IF(d.human_review_spans > 0, 1, 0)),
    NULLIF(COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)), 0)
  ) AS human_reviews_per_verified_outcome,
  SUM(d.deal_model_cost_usd) AS measured_model_cost_usd,
  SUM(COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(d.span_count, d.outcome_total_spans), 0.0)) AS estimated_infra_cost_usd,
  SUM(d.agent_gym_compile_cost_usd) AS one_time_skill_compile_cost_usd,
  SAFE_DIVIDE(
    SUM(
      d.deal_model_cost_usd
      + COALESCE(inf.estimated_infra_cost_usd, 0.0) * COALESCE(SAFE_DIVIDE(d.span_count, d.outcome_total_spans), 0.0)
    ),
    NULLIF(COUNT(DISTINCT IF(d.is_verified, d.outcome_id, NULL)), 0)
  ) AS cost_per_verified_outcome_usd
FROM weighted_deal_outcomes AS d
LEFT JOIN `agent_eval.v_demo_infra_cost_estimate` AS inf
  ON d.outcome_id = inf.outcome_id
GROUP BY
  d.apphub_application_id,
  d.skill_id,
  d.skill_version;

-- -----------------------------------------------------------------------------
-- Demo View 4: M2 KYC Step-2 Semantic Trap Catch Rate, 95% Wilson CI & Argon Agreement
--   * Joins fleet_labels -> fleet_case_outcomes -> eval_results on outcome_id
--   * Separates held_out = TRUE (`third_party_holder_mismatch`) from rubric-listed traps
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW `agent_eval.v_m2_kyc_trap_eval` AS
WITH flash_verdicts AS (
  SELECT
    COALESCE(e.outcome_id, t.outcome_id) AS outcome_id,
    ARRAY_AGG(e.binary_pass ORDER BY e.evaluated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS flash_pass,
    ARRAY_AGG(e.ordinal_score ORDER BY e.evaluated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS flash_score,
    SUM(
      ((e.judge_input_tokens / 1e6) * COALESCE(m.input_usd_per_1m, 0.15))
      + (((e.judge_output_tokens + e.judge_thinking_tokens) / 1e6) * COALESCE(m.output_usd_per_1m, 0.60))
    ) AS flash_judge_cost_usd
  FROM `agent_eval.eval_results` AS e
  LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t
    ON e.trace_id = t.trace_id
  LEFT JOIN `agent_eval.model_pricing_catalog` AS m
    ON e.judge_model = m.model_name
    AND e.evaluated_at >= m.effective_start
    AND e.evaluated_at < m.effective_end
  WHERE e.rubric_id = 'kyc_policy_compliance_4pt_v1'
    AND e.judge_model = 'gemini-3.8-flash'
    AND COALESCE(e.outcome_id, t.outcome_id) IS NOT NULL
  GROUP BY 1
),
argon_verdicts AS (
  SELECT
    COALESCE(e.outcome_id, t.outcome_id) AS outcome_id,
    ARRAY_AGG(e.binary_pass ORDER BY e.evaluated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS argon_pass,
    ARRAY_AGG(e.ordinal_score ORDER BY e.evaluated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS argon_score
  FROM `agent_eval.eval_results` AS e
  LEFT JOIN `agent_eval.v_trace_sql_evaluation` AS t
    ON e.trace_id = t.trace_id
  WHERE e.rubric_id = 'kyc_policy_compliance_4pt_v1'
    AND e.judge_model = 'gemini-4-argon'
    AND COALESCE(e.outcome_id, t.outcome_id) IS NOT NULL
  GROUP BY 1
),
joined_cases AS (
  SELECT
    l.run_id,
    l.case_id,
    l.case_type,
    COALESCE(l.trap_class, 'clean_street_proof') AS trap_class,
    l.held_out,
    (l.case_type = 'step2_trap') AS is_trap,
    o.outcome_id,
    o.observed_status,
    fv.flash_pass,
    fv.flash_score,
    COALESCE(fv.flash_judge_cost_usd, 0.0) AS flash_judge_cost_usd,
    av.argon_pass,
    av.argon_score
  FROM `agent_eval.fleet_labels` AS l
  INNER JOIN `agent_eval.fleet_case_outcomes` AS o
    ON l.run_id = o.run_id
    AND l.case_id = o.case_id
  LEFT JOIN flash_verdicts AS fv
    ON o.outcome_id = fv.outcome_id
  LEFT JOIN argon_verdicts AS av
    ON o.outcome_id = av.outcome_id
  WHERE l.app = 'kyc-address-verification'
    AND l.case_type IN ('clean', 'step2_trap')
),
unioned_slices AS (
  SELECT run_id, 'ALL_STEP1_ACCEPTS' AS cohort_slice, * EXCEPT(run_id) FROM joined_cases
  UNION ALL
  SELECT run_id, 'KNOWN_TRAPS_AND_CLEAN' AS cohort_slice, * EXCEPT(run_id) FROM joined_cases WHERE NOT held_out
  UNION ALL
  SELECT run_id, 'HELD_OUT_TRAP_ONLY' AS cohort_slice, * EXCEPT(run_id) FROM joined_cases WHERE held_out
  UNION ALL
  SELECT run_id, CONCAT('CLASS:', trap_class) AS cohort_slice, * EXCEPT(run_id) FROM joined_cases
),
counts AS (
  SELECT
    run_id,
    cohort_slice,
    COUNT(*) AS total_cases,
    COUNTIF(flash_pass IS NOT NULL) AS judged_cases,
    COUNTIF(is_trap) AS actual_traps,
    COUNTIF(NOT is_trap) AS actual_clean,
    COUNTIF(is_trap AND observed_status = 'failed') AS step1_caught_traps,
    COUNTIF(is_trap AND flash_pass = FALSE) AS tp_traps_caught,
    COUNTIF(is_trap AND (observed_status = 'failed' OR flash_pass = FALSE)) AS end_to_end_traps_caught,
    COUNTIF(is_trap AND flash_pass = TRUE) AS fn_traps_missed,
    COUNTIF(NOT is_trap AND flash_pass = TRUE) AS tn_clean_approved,
    COUNTIF(NOT is_trap AND flash_pass = FALSE) AS fp_clean_flagged,
    COUNTIF(argon_pass IS NOT NULL AND flash_pass IS NOT NULL) AS argon_calibrated_rows,
    COUNTIF(argon_pass IS NOT NULL AND flash_pass = argon_pass) AS flash_argon_agreed_rows,
    SUM(flash_judge_cost_usd) AS full_100pct_judge_cost_usd
  FROM unioned_slices
  GROUP BY run_id, cohort_slice
)
SELECT
  c.*,
  SAFE_DIVIDE(c.tp_traps_caught, NULLIF(c.tp_traps_caught + c.fn_traps_missed, 0)) AS trap_recall,
  SAFE_DIVIDE(
    (c.tp_traps_caught + 1.920729)
    - 1.959964 * SQRT(
      SAFE_DIVIDE(c.tp_traps_caught * c.fn_traps_missed, NULLIF(c.tp_traps_caught + c.fn_traps_missed, 0))
      + 0.960365
    ),
    NULLIF((c.tp_traps_caught + c.fn_traps_missed) + 3.841459, 0)
  ) AS recall_ci_95_lower,
  LEAST(
    1.0,
    SAFE_DIVIDE(
      (c.tp_traps_caught + 1.920729)
      + 1.959964 * SQRT(
        SAFE_DIVIDE(c.tp_traps_caught * c.fn_traps_missed, NULLIF(c.tp_traps_caught + c.fn_traps_missed, 0))
        + 0.960365
      ),
      NULLIF((c.tp_traps_caught + c.fn_traps_missed) + 3.841459, 0)
    )
  ) AS recall_ci_95_upper,
  SAFE_DIVIDE(c.tp_traps_caught, NULLIF(c.tp_traps_caught + c.fp_clean_flagged, 0)) AS trap_precision,
  SAFE_DIVIDE(
    (c.tp_traps_caught + 1.920729)
    - 1.959964 * SQRT(
      SAFE_DIVIDE(c.tp_traps_caught * c.fp_clean_flagged, NULLIF(c.tp_traps_caught + c.fp_clean_flagged, 0))
      + 0.960365
    ),
    NULLIF((c.tp_traps_caught + c.fp_clean_flagged) + 3.841459, 0)
  ) AS precision_ci_95_lower,
  LEAST(
    1.0,
    SAFE_DIVIDE(
      (c.tp_traps_caught + 1.920729)
      + 1.959964 * SQRT(
        SAFE_DIVIDE(c.tp_traps_caught * c.fp_clean_flagged, NULLIF(c.tp_traps_caught + c.fp_clean_flagged, 0))
        + 0.960365
      ),
      NULLIF((c.tp_traps_caught + c.fp_clean_flagged) + 3.841459, 0)
    )
  ) AS precision_ci_95_upper,
  SAFE_DIVIDE(c.fp_clean_flagged, NULLIF(c.tn_clean_approved + c.fp_clean_flagged, 0)) AS false_positive_rate,
  100.0 * SAFE_DIVIDE(c.flash_argon_agreed_rows, NULLIF(c.argon_calibrated_rows, 0)) AS flash_argon_agreement_pct,
  CAST(CEIL(0.10 * c.judged_cases) AS INT64) AS projected_10pct_sample_cases,
  0.10 * c.full_100pct_judge_cost_usd AS projected_10pct_judge_cost_usd
FROM counts AS c;
