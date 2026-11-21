"""SQLite schema and view definitions mirroring `sql/views.sql` and `sql/step1_trace_views.sql`."""

from __future__ import annotations

SQLITE_TABLES_DDL: str = """
CREATE TABLE otel_spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    span_name TEXT NOT NULL,
    apphub_application_id TEXT NOT NULL,
    outcome_id TEXT,
    outcome_owner TEXT,
    source_mode TEXT,
    agent_name TEXT NOT NULL,
    service_name TEXT NOT NULL,
    span_kind TEXT NOT NULL,
    model_name TEXT NOT NULL,
    tool_call_status TEXT,
    status_code TEXT NOT NULL,
    rule0_pass INTEGER NOT NULL,
    finops_traffic_type TEXT NOT NULL,
    outcome_status TEXT,
    outcome_verified INTEGER,
    start_epoch_ms REAL NOT NULL,
    end_epoch_ms REAL NOT NULL,
    latency_ms REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    uncached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    thinking_tokens INTEGER NOT NULL,
    weighted_burndown_tokens REAL NOT NULL,
    span_cost_usd REAL NOT NULL
);
CREATE TABLE eval_results (
    trace_id TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    sampling_stratum TEXT NOT NULL,
    binary_pass INTEGER NOT NULL,
    judge_cost_usd REAL NOT NULL,
    PRIMARY KEY (trace_id, rubric_id)
);
CREATE TABLE sme_confusion_counts (
    apphub_application_id TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    tp INTEGER NOT NULL,
    fn INTEGER NOT NULL,
    tn INTEGER NOT NULL,
    fp INTEGER NOT NULL,
    PRIMARY KEY (apphub_application_id, rubric_id)
);
CREATE TABLE sme_ordinal_labels (
    apphub_application_id TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    human_ordinal_score INTEGER NOT NULL,
    judge_ordinal_score INTEGER NOT NULL
);
CREATE TABLE eval_config (
    rubric_id TEXT NOT NULL,
    irr_threshold REAL NOT NULL,
    sla_latency_ms REAL NOT NULL
);
"""

SQLITE_VIEWS_DDL: str = """
CREATE VIEW v_trace_sql_evaluation AS
SELECT
    trace_id,
    COALESCE(
        MAX(CASE WHEN parent_span_id IS NULL THEN NULLIF(outcome_owner, '') END),
        MAX(NULLIF(outcome_owner, '')),
        MAX(CASE WHEN parent_span_id IS NULL THEN NULLIF(apphub_application_id, 'unregistered_app') END),
        MAX(NULLIF(apphub_application_id, 'unregistered_app')),
        'unregistered_app'
    ) AS apphub_application_id,
    MAX(outcome_id) AS outcome_id,
    MAX(end_epoch_ms) - MIN(start_epoch_ms) AS trace_latency_ms,
    SUM(CASE WHEN span_kind = 'execute_tool' THEN 1 ELSE 0 END) AS tool_call_count,
    SUM(CASE WHEN span_kind = 'execute_tool'
        AND (status_code = 'ERROR' OR COALESCE(tool_call_status, 'SUCCESS') != 'SUCCESS')
        THEN 1 ELSE 0 END) AS tool_error_count,
    CASE WHEN MIN(rule0_pass) = 1
        AND SUM(CASE WHEN status_code = 'ERROR' THEN 1 ELSE 0 END) = 0
        AND SUM(CASE WHEN span_kind = 'execute_tool'
            AND (status_code = 'ERROR' OR COALESCE(tool_call_status, 'SUCCESS') != 'SUCCESS')
            THEN 1 ELSE 0 END) = 0
        AND SUM(CASE WHEN span_kind = 'execute_tool' THEN 1 ELSE 0 END) <= 8
        THEN 1 ELSE 0 END AS rule0_pass,
    MAX(finops_traffic_type) AS primary_traffic_type,
    SUM(input_tokens) AS total_input_tokens,
    SUM(cached_input_tokens) AS total_cached_input_tokens,
    CASE WHEN SUM(input_tokens) > 0
        THEN CAST(SUM(cached_input_tokens) AS REAL) / SUM(input_tokens)
        ELSE 0.0 END AS cache_hit_ratio,
    SUM(weighted_burndown_tokens) AS weighted_burndown_tokens,
    SUM(CASE WHEN finops_traffic_type = 'production_inference' THEN span_cost_usd ELSE 0.0 END) AS production_inference_usd,
    SUM(CASE WHEN finops_traffic_type = 'conflict_escalation' THEN span_cost_usd ELSE 0.0 END) AS conflict_escalation_usd,
    SUM(CASE WHEN finops_traffic_type = 'agent_gym_eval' THEN span_cost_usd ELSE 0.0 END) AS agent_gym_eval_usd,
    SUM(CASE WHEN finops_traffic_type = 'autorater_judge' THEN span_cost_usd ELSE 0.0 END) AS span_autorater_judge_usd,
    SUM(CASE WHEN finops_traffic_type NOT IN ('production_inference', 'conflict_escalation', 'agent_gym_eval', 'autorater_judge')
        THEN span_cost_usd ELSE 0.0 END) AS unclassified_usd
FROM otel_spans
GROUP BY trace_id;

CREATE VIEW v_outcomes AS
SELECT
    outcome_id,
    COALESCE(MAX(NULLIF(outcome_owner, '')), MAX(NULLIF(apphub_application_id, 'unregistered_app')), 'unregistered_app') AS outcome_owner,
    COALESCE(MAX(outcome_status), 'succeeded') AS outcome_status,
    MIN(
        CASE
            WHEN COALESCE(outcome_status, 'succeeded') != 'succeeded' THEN 0
            ELSE COALESCE(outcome_verified, rule0_pass)
        END
    ) AS is_verified
FROM otel_spans
WHERE span_name = 'outcome.closed' AND outcome_id IS NOT NULL
GROUP BY outcome_id;

CREATE VIEW v_cost_per_verified_outcome AS
WITH outcome_agg AS (
    SELECT
        s.outcome_id,
        COALESCE(o.outcome_owner, MAX(NULLIF(s.outcome_owner, '')), MAX(NULLIF(s.apphub_application_id, 'unregistered_app')), 'unregistered_app') AS apphub_application_id,
        COALESCE(o.is_verified, 0) AS is_verified,
        SUM(s.span_cost_usd) AS outcome_span_cost_usd
    FROM otel_spans AS s
    LEFT JOIN v_outcomes AS o ON s.outcome_id = o.outcome_id
    WHERE s.outcome_id IS NOT NULL
    GROUP BY s.outcome_id
),
outcome_judge AS (
    SELECT t.outcome_id, SUM(e.judge_cost_usd) AS outcome_judge_usd
    FROM eval_results AS e
    INNER JOIN v_trace_sql_evaluation AS t ON e.trace_id = t.trace_id
    WHERE t.outcome_id IS NOT NULL
    GROUP BY t.outcome_id
)
SELECT
    oa.apphub_application_id,
    COUNT(*) AS total_outcomes,
    SUM(CASE WHEN oa.is_verified = 1 THEN 1 ELSE 0 END) AS verified_outcomes,
    SUM(oa.outcome_span_cost_usd + COALESCE(oj.outcome_judge_usd, 0.0)) AS total_outcome_cost_usd,
    CASE
        WHEN SUM(CASE WHEN oa.is_verified = 1 THEN 1 ELSE 0 END) > 0
        THEN SUM(oa.outcome_span_cost_usd + COALESCE(oj.outcome_judge_usd, 0.0))
             / SUM(CASE WHEN oa.is_verified = 1 THEN 1 ELSE 0 END)
        ELSE NULL
    END AS cost_per_verified_outcome_usd
FROM outcome_agg AS oa
LEFT JOIN outcome_judge AS oj ON oa.outcome_id = oj.outcome_id
GROUP BY oa.apphub_application_id;

CREATE VIEW v_agent_eval_and_finops_summary AS
WITH app_traces AS (
    SELECT
        apphub_application_id,
        COUNT(*) AS total_traces,
        SUM(CASE WHEN primary_traffic_type IN ('production_inference', 'conflict_escalation')
            THEN 1 ELSE 0 END) AS production_traces,
        AVG(CAST(rule0_pass AS REAL)) AS rule0_pass_rate,
        AVG(CASE WHEN tool_error_count = 0 THEN 1.0 ELSE 0.0 END) AS tool_success_rate,
        AVG(cache_hit_ratio) AS avg_cache_hit_ratio,
        SUM(weighted_burndown_tokens) AS total_weighted_burndown_tokens,
        SUM(production_inference_usd) AS production_inference_usd,
        SUM(conflict_escalation_usd) AS conflict_escalation_usd,
        SUM(agent_gym_eval_usd) AS agent_gym_eval_usd,
        SUM(span_autorater_judge_usd) AS span_autorater_judge_usd,
        SUM(unclassified_usd) AS unclassified_usd
    FROM v_trace_sql_evaluation
    GROUP BY apphub_application_id
),
ranked_latencies AS (
    SELECT
        apphub_application_id,
        trace_latency_ms,
        ROW_NUMBER() OVER (PARTITION BY apphub_application_id ORDER BY trace_latency_ms) AS rn,
        COUNT(*) OVER (PARTITION BY apphub_application_id) AS cnt
    FROM v_trace_sql_evaluation
),
p95_latencies AS (
    SELECT
        apphub_application_id,
        trace_latency_ms AS p95_latency_ms
    FROM ranked_latencies
    WHERE rn = MIN(cnt, MAX(1, CAST(CEIL(0.95 * cnt) AS INTEGER)))
),
stratum_pop AS (
    SELECT
        t.apphub_application_id,
        CASE WHEN t.tool_error_count > 0 OR t.trace_latency_ms > cfg.sla_latency_ms
            THEN 'ERROR_OR_SLA_TAIL' ELSE 'HEALTHY_POPULATION' END AS stratum_bucket,
        COUNT(*) AS stratum_pop_count
    FROM v_trace_sql_evaluation AS t
    CROSS JOIN eval_config AS cfg
    WHERE t.rule0_pass = 1
    GROUP BY t.apphub_application_id, stratum_bucket
),
stratum_judge AS (
    SELECT
        t.apphub_application_id,
        e.rubric_id,
        CASE WHEN e.sampling_stratum = 'ERROR_OR_SLA_TAIL'
            THEN 'ERROR_OR_SLA_TAIL' ELSE 'HEALTHY_POPULATION' END AS stratum_bucket,
        COUNT(*) AS stratum_judged_count,
        AVG(CAST(e.binary_pass AS REAL)) AS stratum_pass_rate
    FROM eval_results AS e
    INNER JOIN v_trace_sql_evaluation AS t ON e.trace_id = t.trace_id
    WHERE t.rule0_pass = 1
    GROUP BY t.apphub_application_id, e.rubric_id, stratum_bucket
),
stratum_weighted AS (
    SELECT
        sj.apphub_application_id,
        sj.rubric_id,
        sj.stratum_judged_count,
        sj.stratum_pass_rate,
        CAST(COALESCE(sp.stratum_pop_count, sj.stratum_judged_count) AS REAL)
        / SUM(CAST(COALESCE(sp.stratum_pop_count, sj.stratum_judged_count) AS REAL))
          OVER (PARTITION BY sj.apphub_application_id, sj.rubric_id) AS stratum_weight
    FROM stratum_judge AS sj
    LEFT JOIN stratum_pop AS sp
        ON sj.apphub_application_id = sp.apphub_application_id
        AND sj.stratum_bucket = sp.stratum_bucket
),
stratum_agg AS (
    SELECT
        apphub_application_id,
        rubric_id,
        SUM(stratum_judged_count) AS judged_samples,
        SUM(stratum_weight * stratum_pass_rate) AS raw_judge_pass_rate,
        SUM(
            POW(stratum_weight, 2.0)
            * (stratum_pass_rate * (1.0 - stratum_pass_rate)) / stratum_judged_count
        ) AS var_p_obs
    FROM stratum_weighted
    GROUP BY apphub_application_id, rubric_id
),
judge_spend AS (
    SELECT
        t.apphub_application_id,
        e.rubric_id,
        SUM(e.judge_cost_usd) AS table_judge_usd
    FROM eval_results AS e
    INNER JOIN v_trace_sql_evaluation AS t ON e.trace_id = t.trace_id
    GROUP BY t.apphub_application_id, e.rubric_id
),
judge_stats AS (
    SELECT
        js.apphub_application_id,
        js.rubric_id,
        COALESCE(sa.judged_samples, 0) AS judged_samples,
        sa.raw_judge_pass_rate,
        sa.var_p_obs,
        js.table_judge_usd
    FROM judge_spend AS js
    LEFT JOIN stratum_agg AS sa
        ON js.apphub_application_id = sa.apphub_application_id
        AND js.rubric_id = sa.rubric_id
),
sme_rates AS (
    SELECT
        apphub_application_id,
        rubric_id,
        CAST(tp + fn AS REAL) AS n_pos,
        CAST(tn + fp AS REAL) AS n_neg,
        CASE WHEN (tp + fn) > 0 THEN CAST(tp AS REAL) / (tp + fn) ELSE NULL END AS tpr,
        CASE WHEN (tn + fp) > 0 THEN CAST(tn AS REAL) / (tn + fp) ELSE NULL END AS tnr
    FROM sme_confusion_counts
),
gwet_marginals AS (
    SELECT
        apphub_application_id,
        rubric_id,
        COUNT(*) AS pair_count,
        AVG(1.0 - POW(CAST(human_ordinal_score - judge_ordinal_score AS REAL) / 3.0, 2.0)) AS p_a,
        (AVG(CASE WHEN human_ordinal_score = 1 THEN 1.0 ELSE 0.0 END) + AVG(CASE WHEN judge_ordinal_score = 1 THEN 1.0 ELSE 0.0 END)) / 2.0 AS pi_1,
        (AVG(CASE WHEN human_ordinal_score = 2 THEN 1.0 ELSE 0.0 END) + AVG(CASE WHEN judge_ordinal_score = 2 THEN 1.0 ELSE 0.0 END)) / 2.0 AS pi_2,
        (AVG(CASE WHEN human_ordinal_score = 3 THEN 1.0 ELSE 0.0 END) + AVG(CASE WHEN judge_ordinal_score = 3 THEN 1.0 ELSE 0.0 END)) / 2.0 AS pi_3,
        (AVG(CASE WHEN human_ordinal_score = 4 THEN 1.0 ELSE 0.0 END) + AVG(CASE WHEN judge_ordinal_score = 4 THEN 1.0 ELSE 0.0 END)) / 2.0 AS pi_4
    FROM sme_ordinal_labels
    WHERE human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4
    GROUP BY apphub_application_id, rubric_id
),
gwet_irr AS (
    SELECT
        apphub_application_id,
        rubric_id,
        CASE
            WHEN pair_count = 0 THEN NULL
            WHEN ABS(1.0 - ((26.0 / 27.0) * (pi_1 * (1.0 - pi_1) + pi_2 * (1.0 - pi_2) + pi_3 * (1.0 - pi_3) + pi_4 * (1.0 - pi_4)))) < 1e-12
                THEN 1.0
            ELSE (
                p_a - ((26.0 / 27.0) * (pi_1 * (1.0 - pi_1) + pi_2 * (1.0 - pi_2) + pi_3 * (1.0 - pi_3) + pi_4 * (1.0 - pi_4)))
            ) / (
                1.0 - ((26.0 / 27.0) * (pi_1 * (1.0 - pi_1) + pi_2 * (1.0 - pi_2) + pi_3 * (1.0 - pi_3) + pi_4 * (1.0 - pi_4)))
            )
        END AS gwet_ac2
    FROM gwet_marginals
),
rg_base AS (
    SELECT
        a.*,
        p.p95_latency_ms,
        COALESCE(j.judged_samples, 0) AS judged_samples,
        j.raw_judge_pass_rate,
        j.var_p_obs,
        MAX(a.span_autorater_judge_usd, COALESCE(j.table_judge_usd, 0.0)) AS autorater_judge_usd,
        s.n_pos,
        s.n_neg,
        s.tpr,
        s.tnr,
        (s.tpr + s.tnr - 1.0) AS youden_j,
        g.gwet_ac2,
        cfg.irr_threshold,
        COALESCE(oc.total_outcomes, 0) AS outcome_total_count,
        COALESCE(oc.verified_outcomes, 0) AS outcome_verified_count,
        oc.cost_per_verified_outcome_usd AS outcome_cost_per_verified_usd,
        CASE
            WHEN COALESCE(j.judged_samples, 0) = 0 THEN 'UNJUDGED'
            WHEN s.tpr IS NOT NULL AND s.tnr IS NOT NULL AND (s.tpr + s.tnr - 1.0) > 1e-6 THEN 'CALIBRATED'
            ELSE 'UNCALIBRATED'
        END AS calibration_status,
        CASE
            WHEN COALESCE(j.judged_samples, 0) > 0
                AND s.tpr IS NOT NULL
                AND s.tnr IS NOT NULL
                AND (s.tpr + s.tnr - 1.0) > 1e-6
            THEN MIN(1.0, MAX(0.0, (j.raw_judge_pass_rate + s.tnr - 1.0) / (s.tpr + s.tnr - 1.0)))
            ELSE NULL
        END AS calibrated_pass_rate
    FROM app_traces AS a
    CROSS JOIN eval_config AS cfg
    INNER JOIN p95_latencies AS p
        ON a.apphub_application_id = p.apphub_application_id
    LEFT JOIN judge_stats AS j
        ON a.apphub_application_id = j.apphub_application_id
        AND j.rubric_id = cfg.rubric_id
    LEFT JOIN sme_rates AS s
        ON a.apphub_application_id = s.apphub_application_id
        AND s.rubric_id = cfg.rubric_id
    LEFT JOIN gwet_irr AS g
        ON a.apphub_application_id = g.apphub_application_id
        AND g.rubric_id = cfg.rubric_id
    LEFT JOIN v_cost_per_verified_outcome AS oc
        ON a.apphub_application_id = oc.apphub_application_id
),
rg_ci AS (
    SELECT
        *,
        CASE
            WHEN calibrated_pass_rate IS NOT NULL THEN
                SQRT(
                    COALESCE(var_p_obs, 0.0)
                    + POW(calibrated_pass_rate, 2.0) * (tpr * (1.0 - tpr) / n_pos)
                    + POW(1.0 - calibrated_pass_rate, 2.0) * (tnr * (1.0 - tnr) / n_neg)
                ) / youden_j
            ELSE NULL
        END AS se_theta,
        (production_inference_usd + conflict_escalation_usd + agent_gym_eval_usd + autorater_judge_usd + unclassified_usd) AS total_spend_usd
    FROM rg_base
)
SELECT
    apphub_application_id,
    total_traces,
    production_traces,
    rule0_pass_rate,
    tool_success_rate,
    p95_latency_ms,
    avg_cache_hit_ratio,
    total_weighted_burndown_tokens,
    judged_samples,
    calibration_status,
    raw_judge_pass_rate,
    calibrated_pass_rate,
    CASE WHEN se_theta IS NOT NULL
        THEN MIN(1.0, MAX(0.0, calibrated_pass_rate - 1.959963984540054 * se_theta))
        ELSE NULL END AS calibrated_ci_95_lower,
    CASE WHEN se_theta IS NOT NULL
        THEN MIN(1.0, MAX(0.0, calibrated_pass_rate + 1.959963984540054 * se_theta))
        ELSE NULL END AS calibrated_ci_95_upper,
    gwet_ac2,
    CASE WHEN gwet_ac2 IS NOT NULL
        THEN CASE WHEN gwet_ac2 >= irr_threshold THEN 1 ELSE 0 END
        ELSE NULL END AS passes_irr_gate,
    production_inference_usd,
    conflict_escalation_usd,
    agent_gym_eval_usd,
    autorater_judge_usd,
    unclassified_usd,
    total_spend_usd,
    CASE
        WHEN calibrated_pass_rate IS NOT NULL
            AND calibrated_pass_rate > 0.0
            AND rule0_pass_rate > 0.0
            AND production_traces > 0
        THEN total_spend_usd / (production_traces * rule0_pass_rate * calibrated_pass_rate)
        ELSE NULL
    END AS cost_per_verified_outcome_usd,
    outcome_total_count,
    outcome_verified_count,
    outcome_cost_per_verified_usd
FROM rg_ci
ORDER BY apphub_application_id;
"""
