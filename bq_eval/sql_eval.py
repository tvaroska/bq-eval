"""Tier 1-3 BigQuery SQL Evaluation Builder & Local SQLite Execution Twin."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from bq_eval.calibration import ConfusionMatrix
from bq_eval.otel import OTelGenAISpanRecord
from bq_eval.rubrics import CANONICAL_RUBRICS, build_autorater_system_prompt


class SQLTraceEvalSummary(BaseModel):
    """Per-application Tier 1-3 evaluation and 4-bucket FinOps summary (EVAL-P04..P08)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    rubric_id: str
    total_traces: int
    production_traces: int
    rule0_pass_rate: float
    tool_success_rate: float
    p95_latency_ms: float
    avg_cache_hit_ratio: float
    total_weighted_burndown_tokens: float
    judged_samples: int
    calibration_status: Literal["CALIBRATED", "UNCALIBRATED", "UNJUDGED"]
    raw_judge_pass_rate: float | None
    calibrated_pass_rate: float | None
    calibrated_ci_95_lower: float | None
    calibrated_ci_95_upper: float | None
    gwet_ac2: float | None = None
    passes_irr_gate: bool | None = None
    production_inference_usd: float
    conflict_escalation_usd: float
    agent_gym_eval_usd: float
    autorater_judge_usd: float
    total_spend_usd: float
    cost_per_verified_outcome_usd: float | None


def escape_sql_xml_expr(column_expr: str) -> str:
    """Return BigQuery SQL expression that XML-escapes `&`, `<`, and `>` in untrusted columns."""
    return (
        f"REPLACE(REPLACE(REPLACE(COALESCE({column_expr}, ''), "
        "'&', '&amp;'), '<', '&lt;'), '>', '&gt;')"
    )


def _validate_sql_identifier(value: str, field_name: str, *, allow_hyphen: bool = False) -> str:
    cleaned = value.strip()
    pattern = r"[a-zA-Z0-9_-]+" if allow_hyphen else r"[a-zA-Z0-9_]+"
    if not cleaned or not re.fullmatch(pattern, cleaned):
        raise ValueError(f"Invalid {field_name} {value!r}: must match ^{pattern}$")
    return cleaned


def _validate_run_id(run_id: str | None) -> str | None:
    if run_id is None:
        return None
    cleaned = run_id.strip()
    if not cleaned:
        return None
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", cleaned):
        raise ValueError(f"Invalid run_id {run_id!r}: must match ^[a-zA-Z0-9_.-]+$")
    return cleaned


def _escape_googlesql_string_literal(text: str) -> str:
    """Escape backslashes, single quotes, and newlines for a single-line GoogleSQL literal."""
    return (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


JUDGE_MODEL_ENDPOINTS: dict[str, str] = {
    "gemini-3.8-flash": "gemini_3_8_flash_judge",
    "gemini-4-argon": "gemini_4_argon_judge",
}


def build_ai_generate_table_judge_sql(  # noqa: PLR0913
    project_id: str,
    dataset_id: str = "agent_eval",
    rubric_id: str = "groundedness_4pt_v1",
    canary_sample_pct: int = 5,
    run_id: str | None = None,
    judge_model: str | None = None,
) -> str:
    """Build production BigQuery `AI.GENERATE_TABLE` SQL query (EVAL-P08).

    Enforces:
    1. Stratified sampling: 100% of deterministic failures (`NOT rule0_pass`, tool errors,
       or SLA breaches) plus `canary_sample_pct`% (`FARM_FINGERPRINT`) of healthy traces,
       optionally scoped to a specific fleet `run_id`.
    2. Nested SQL `REPLACE` XML escaping inside `<untrusted_text>` blocks so prompt payloads
       cannot break out of XML delimiters.
    3. CoT-first output struct: `reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL`
       with deterministic `binary_pass` thresholding and judge token estimates.
    """
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_dataset = _validate_sql_identifier(dataset_id, "dataset_id")
    if rubric_id not in CANONICAL_RUBRICS:
        raise ValueError(f"Unknown rubric_id: {rubric_id}")
    if not (0 <= canary_sample_pct <= 100):
        raise ValueError("canary_sample_pct must be between 0 and 100.")
    valid_run_id = _validate_run_id(run_id)

    rubric = CANONICAL_RUBRICS[rubric_id]
    selected_model = judge_model or rubric.judge_model
    if selected_model not in JUDGE_MODEL_ENDPOINTS:
        raise ValueError(
            f"Unsupported judge_model {selected_model!r}: expected one of {sorted(JUDGE_MODEL_ENDPOINTS)}"
        )
    remote_model = JUDGE_MODEL_ENDPOINTS[selected_model]
    effective_rubric = (
        rubric
        if selected_model == rubric.judge_model
        else rubric.model_copy(update={"judge_model": selected_model})
    )
    system_prompt = _escape_googlesql_string_literal(
        build_autorater_system_prompt(effective_rubric)
    )
    escaped_prompt = escape_sql_xml_expr("p.user_prompt")
    escaped_context = escape_sql_xml_expr("p.retrieved_context")
    escaped_response = escape_sql_xml_expr("p.final_response")

    run_filter = (
        f"\n    AND t.outcome_id IN (\n"  # noqa: S608
        f"      SELECT outcome_id\n"
        f"      FROM `{valid_project}.{valid_dataset}.fleet_case_outcomes`\n"
        f"      WHERE run_id = '{valid_run_id}' AND outcome_id IS NOT NULL\n"
        f"    )"
        if valid_run_id
        else ""
    )
    eval_run_col = f"  '{valid_run_id}' AS eval_run_id,\n" if valid_run_id else ""

    return f"""-- Tier 2 Stratified AI.GENERATE_TABLE Judge Execution ({rubric.rubric_id})
WITH candidate_traces AS (
  SELECT
    t.trace_id,
    t.outcome_id,
    t.apphub_application_id,
    t.rule0_pass,
    CASE
      WHEN NOT t.rule0_pass OR t.tool_error_count > 0 OR t.trace_latency_ms > t.sla_latency_ms
        THEN 'ERROR_OR_SLA_TAIL'
      ELSE 'CANARY_{canary_sample_pct}PCT'
    END AS sampling_stratum,
    CONCAT(
      '{system_prompt}\\n\\n',
      'User Prompt:\\n<untrusted_text>\\n', {escaped_prompt}, '\\n</untrusted_text>\\n\\n',
      'Retrieved Context:\\n<untrusted_text>\\n', {escaped_context}, '\\n</untrusted_text>\\n\\n',
      'Agent Final Response:\\n<untrusted_text>\\n', {escaped_response}, '\\n</untrusted_text>'
    ) AS prompt
  FROM `{valid_project}.{valid_dataset}.v_trace_sql_evaluation` AS t
  LEFT JOIN `{valid_project}.{valid_dataset}.trace_payloads` AS p
    ON t.trace_id = p.trace_id
  WHERE (
    NOT t.rule0_pass
    OR t.tool_error_count > 0
    OR t.trace_latency_ms > t.sla_latency_ms
    OR MOD(ABS(FARM_FINGERPRINT(t.trace_id)), 100) < {canary_sample_pct}
  ){run_filter}
)
SELECT
  GENERATE_UUID() AS eval_id,
{eval_run_col}  trace_id,
  outcome_id,
  apphub_application_id,
  '{rubric.rubric_id}' AS rubric_id,
  '{selected_model}' AS judge_model,
  sampling_stratum,
  rule0_pass,
  reasoning_trace,
  ordinal_score,
  (ordinal_score >= {rubric.passing_score}) AS binary_pass,
  DIV(LENGTH(prompt), 4) AS judge_input_tokens,
  DIV(LENGTH(COALESCE(reasoning_trace, '')), 4) + 12 AS judge_output_tokens,
  0 AS judge_thinking_tokens,
  CURRENT_TIMESTAMP() AS evaluated_at
FROM AI.GENERATE_TABLE(
  MODEL `{valid_project}.{valid_dataset}.{remote_model}`,
  TABLE candidate_traces,
  STRUCT(
    'reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL' AS output_schema,
    '{{"generation_config": {{"temperature": 0.0, "max_output_tokens": 2048, "thinking_config": {{"thinking_level": "LOW"}}}}}}' AS model_params
  )
);
"""  # noqa: S608


MANAGED_EVALUATE_AGENT_METRICS: tuple[str, ...] = (
    "tool_use_quality_v1",
    "final_response_quality_v1",
    "hallucination_v1",
    "safety_v1",
    "multi_turn_task_success_v1",
    "multi_turn_tool_use_quality_v1",
    "multi_turn_trajectory_quality_v1",
)


def build_ai_evaluate_agent_sql(  # noqa: PLR0913
    project_id: str,
    dataset_id: str = "agent_eval",
    eval_view: str = "v_bqaa_eval_turn_filtered",
    managed_metrics: Sequence[str] = (
        "tool_use_quality_v1",
        "final_response_quality_v1",
        "hallucination_v1",
        "safety_v1",
    ),
    canary_sample_pct: int = 10,
    run_id: str | None = None,
) -> str:
    """Build Step-1 pre-filtered BigQuery `AI.EVALUATE_AGENT` SQL query (Engine B, M5).

    Filters `BQAA` `AgentData` turns (`v_bqaa_eval_turn_filtered`) through Step-1 `$0` SQL
    gates (`step1_pass = TRUE` and `NOT has_tool_loop`) plus deterministic `FARM_FINGERPRINT`
    sampling before invoking `AI.EVALUATE_AGENT`.
    """
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_dataset = _validate_sql_identifier(dataset_id, "dataset_id")
    valid_view = _validate_sql_identifier(eval_view, "eval_view")
    if not managed_metrics:
        raise ValueError("At least one managed metric is required.")
    unknown = [m for m in managed_metrics if m not in MANAGED_EVALUATE_AGENT_METRICS]
    if unknown:
        raise ValueError(f"Unknown AI.EVALUATE_AGENT managed metric(s): {unknown}")
    if not (0 <= canary_sample_pct <= 100):
        raise ValueError("canary_sample_pct must be between 0 and 100.")
    valid_run_id = _validate_run_id(run_id)

    metrics_sql = ",\n      ".join(f"'{m}'" for m in managed_metrics)
    predicates: list[str] = []
    if canary_sample_pct < 100:
        predicates.append(f"MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < {canary_sample_pct}")
    if valid_run_id:
        predicates.append(
            f"trace_id IN ("  # noqa: S608
            f"SELECT trace_id FROM `{valid_project}.{valid_dataset}.fleet_case_outcomes` "
            f"WHERE run_id = '{valid_run_id}' AND trace_id IS NOT NULL)"
        )
    sample_clause = f"\n    WHERE {' AND '.join(predicates)}" if predicates else ""

    return f"""-- Tier 2 Engine B: Step-1 Pre-Filtered AI.EVALUATE_AGENT Execution ({canary_sample_pct}% Sample)
WITH candidate_turns AS (
  SELECT *
  FROM `{valid_project}.{valid_dataset}.{valid_view}`{sample_clause}
)
SELECT *
FROM AI.EVALUATE_AGENT(
  TABLE candidate_turns,
  trace_col => 'trace',
  managed_metrics => [
      {metrics_sql}
  ]
);
"""  # noqa: S608


DEFAULT_SKU_RATES: dict[str, tuple[float, float, float, float, float, float]] = {
    # model_name: (in_1m, cached_1m, out_1m, long_in_1m, long_cached_1m, long_out_1m)
    "gemini-3.5-flash-lite": (0.075, 0.0075, 0.30, 0.15, 0.015, 1.125),
    "gemini-3.8-flash": (0.15, 0.015, 0.60, 0.30, 0.030, 2.25),
    "gemini-4-argon": (1.25, 0.125, 5.00, 2.50, 0.250, 18.75),
}


def evaluate_traces_in_sqlite(  # noqa: PLR0913
    spans: Sequence[OTelGenAISpanRecord],
    judge_outcomes: dict[str, bool] | None = None,
    sme_confusion: ConfusionMatrix | None = None,
    rubric_id: str = "groundedness_4pt_v1",
    judge_cost_per_call_usd: float = 0.0025,
    sme_ordinal_pairs: Sequence[tuple[int, int]] | None = None,
    passing_score: int = 3,
    irr_threshold: float = 0.80,
) -> dict[str, SQLTraceEvalSummary]:
    """Execute pure-SQLite twin of `sql/views.sql` with exact BigQuery SQL math parity.

    Computes Rule 0 (`<= 8` tools, zero tool errors), Rogan-Gladen prevalence calibration,
    95% Delta-method CIs, Quadratic Gwet's AC2 IRR, and 4-bucket CPVO directly inside
    SQLite SQL views without calling Python statistical helpers.
    """
    if not spans:
        return {}

    judge_map = judge_outcomes or {}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("SQRT", 1, math.sqrt)
    conn.create_function("POW", 2, math.pow)
    conn.create_function("CEIL", 1, math.ceil)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE otel_spans (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            parent_span_id TEXT,
            apphub_application_id TEXT NOT NULL,
            service_name TEXT NOT NULL,
            span_kind TEXT NOT NULL,
            model_name TEXT NOT NULL,
            tool_call_status TEXT,
            status_code TEXT NOT NULL,
            rule0_pass INTEGER NOT NULL,
            finops_traffic_type TEXT NOT NULL,
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
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE eval_results (
            trace_id TEXT PRIMARY KEY,
            binary_pass INTEGER NOT NULL,
            judge_cost_usd REAL NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE sme_confusion_counts (
            tp INTEGER NOT NULL,
            fn INTEGER NOT NULL,
            tn INTEGER NOT NULL,
            fp INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE sme_ordinal_labels (
            human_ordinal_score INTEGER NOT NULL,
            judge_ordinal_score INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE eval_config (
            irr_threshold REAL NOT NULL
        )
        """
    )
    cur.execute("INSERT INTO eval_config (irr_threshold) VALUES (?)", (float(irr_threshold),))

    for s in spans:
        rates = DEFAULT_SKU_RATES.get(s.model_name, DEFAULT_SKU_RATES["gemini-3.8-flash"])
        uncached = s.uncached_input_tokens
        if s.is_long_context_cliff:
            cost_usd = (
                (uncached / 1e6) * rates[3]
                + (s.cached_input_tokens / 1e6) * rates[4]
                + ((s.output_tokens + s.thinking_tokens) / 1e6) * rates[5]
            )
        else:
            cost_usd = (
                (uncached / 1e6) * rates[0]
                + (s.cached_input_tokens / 1e6) * rates[1]
                + ((s.output_tokens + s.thinking_tokens) / 1e6) * rates[2]
            )
        cur.execute(
            """
            INSERT INTO otel_spans VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                s.trace_id,
                s.span_id,
                s.parent_span_id,
                s.resolved_application_id,
                s.service_name,
                str(s.span_kind),
                s.model_name,
                s.tool_call_status,
                s.status_code,
                1 if s.rule0_pass else 0,
                str(s.finops_traffic_type),
                s.start_time.timestamp() * 1000.0,
                s.end_time.timestamp() * 1000.0,
                s.latency_ms,
                s.input_tokens,
                s.cached_input_tokens,
                uncached,
                s.output_tokens,
                s.thinking_tokens,
                s.weighted_burndown_tokens,
                cost_usd,
            ),
        )

    cur.executemany(
        "INSERT INTO eval_results (trace_id, binary_pass, judge_cost_usd) VALUES (?, ?, ?)",
        [(tid, 1 if passed else 0, judge_cost_per_call_usd) for tid, passed in judge_map.items()],
    )

    if sme_ordinal_pairs:
        cur.executemany(
            "INSERT INTO sme_ordinal_labels (human_ordinal_score, judge_ordinal_score) VALUES (?, ?)",
            [(int(h), int(j)) for h, j in sme_ordinal_pairs],
        )

    if sme_confusion is not None:
        cur.execute(
            "INSERT INTO sme_confusion_counts (tp, fn, tn, fp) VALUES (?, ?, ?, ?)",
            (sme_confusion.tp, sme_confusion.fn, sme_confusion.tn, sme_confusion.fp),
        )
    elif sme_ordinal_pairs:
        cur.execute(
            """
            INSERT INTO sme_confusion_counts (tp, fn, tn, fp)
            SELECT
                SUM(CASE WHEN human_ordinal_score >= ? AND judge_ordinal_score >= ? THEN 1 ELSE 0 END),
                SUM(CASE WHEN human_ordinal_score >= ? AND judge_ordinal_score < ? THEN 1 ELSE 0 END),
                SUM(CASE WHEN human_ordinal_score < ? AND judge_ordinal_score < ? THEN 1 ELSE 0 END),
                SUM(CASE WHEN human_ordinal_score < ? AND judge_ordinal_score >= ? THEN 1 ELSE 0 END)
            FROM sme_ordinal_labels
            """,
            (passing_score,) * 8,
        )

    cur.execute(
        """
        CREATE VIEW v_trace_sql_evaluation AS
        SELECT
            trace_id,
            apphub_application_id,
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
            SUM(CASE WHEN finops_traffic_type = 'autorater_judge' THEN span_cost_usd ELSE 0.0 END) AS span_autorater_judge_usd
        FROM otel_spans
        GROUP BY trace_id, apphub_application_id
        """
    )

    cur.execute(
        """
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
                SUM(span_autorater_judge_usd) AS span_autorater_judge_usd
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
        judge_stats AS (
            SELECT
                t.apphub_application_id,
                SUM(CASE WHEN t.rule0_pass = 1 THEN 1 ELSE 0 END) AS judged_samples,
                CASE WHEN SUM(CASE WHEN t.rule0_pass = 1 THEN 1 ELSE 0 END) > 0
                    THEN AVG(CASE WHEN t.rule0_pass = 1 THEN CAST(e.binary_pass AS REAL) END)
                    ELSE NULL END AS raw_judge_pass_rate,
                SUM(e.judge_cost_usd) AS table_judge_usd
            FROM eval_results AS e
            INNER JOIN v_trace_sql_evaluation AS t
                ON e.trace_id = t.trace_id
            GROUP BY t.apphub_application_id
        ),
        sme_rates AS (
            SELECT
                CAST(tp + fn AS REAL) AS n_pos,
                CAST(tn + fp AS REAL) AS n_neg,
                CASE WHEN (tp + fn) > 0 THEN CAST(tp AS REAL) / (tp + fn) ELSE NULL END AS tpr,
                CASE WHEN (tn + fp) > 0 THEN CAST(tn AS REAL) / (tn + fp) ELSE NULL END AS tnr
            FROM sme_confusion_counts
            LIMIT 1
        ),
        gwet_marginals AS (
            SELECT
                COUNT(*) AS pair_count,
                AVG(1.0 - POW(CAST(human_ordinal_score - judge_ordinal_score AS REAL) / 3.0, 2.0)) AS p_a,
                (AVG(CASE WHEN human_ordinal_score = 1 THEN 1.0 ELSE 0.0 END) + AVG(CASE WHEN judge_ordinal_score = 1 THEN 1.0 ELSE 0.0 END)) / 2.0 AS pi_1,
                (AVG(CASE WHEN human_ordinal_score = 2 THEN 1.0 ELSE 0.0 END) + AVG(CASE WHEN judge_ordinal_score = 2 THEN 1.0 ELSE 0.0 END)) / 2.0 AS pi_2,
                (AVG(CASE WHEN human_ordinal_score = 3 THEN 1.0 ELSE 0.0 END) + AVG(CASE WHEN judge_ordinal_score = 3 THEN 1.0 ELSE 0.0 END)) / 2.0 AS pi_3,
                (AVG(CASE WHEN human_ordinal_score = 4 THEN 1.0 ELSE 0.0 END) + AVG(CASE WHEN judge_ordinal_score = 4 THEN 1.0 ELSE 0.0 END)) / 2.0 AS pi_4
            FROM sme_ordinal_labels
        ),
        gwet_irr AS (
            SELECT
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
                MAX(a.span_autorater_judge_usd, COALESCE(j.table_judge_usd, 0.0)) AS autorater_judge_usd,
                s.n_pos,
                s.n_neg,
                s.tpr,
                s.tnr,
                (s.tpr + s.tnr - 1.0) AS youden_j,
                g.gwet_ac2,
                cfg.irr_threshold,
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
            INNER JOIN p95_latencies AS p
                ON a.apphub_application_id = p.apphub_application_id
            LEFT JOIN judge_stats AS j
                ON a.apphub_application_id = j.apphub_application_id
            LEFT JOIN sme_rates AS s ON 1 = 1
            LEFT JOIN gwet_irr AS g ON 1 = 1
            LEFT JOIN eval_config AS cfg ON 1 = 1
        ),
        rg_ci AS (
            SELECT
                *,
                CASE
                    WHEN calibrated_pass_rate IS NOT NULL THEN
                        SQRT(
                            (raw_judge_pass_rate * (1.0 - raw_judge_pass_rate) / judged_samples)
                            + POW(calibrated_pass_rate, 2.0) * (tpr * (1.0 - tpr) / n_pos)
                            + POW(1.0 - calibrated_pass_rate, 2.0) * (tnr * (1.0 - tnr) / n_neg)
                        ) / youden_j
                    ELSE NULL
                END AS se_theta,
                (production_inference_usd + conflict_escalation_usd + agent_gym_eval_usd + autorater_judge_usd) AS total_spend_usd
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
            total_spend_usd,
            CASE
                WHEN calibrated_pass_rate IS NOT NULL
                    AND calibrated_pass_rate > 0.0
                    AND rule0_pass_rate > 0.0
                    AND production_traces > 0
                THEN total_spend_usd / (production_traces * rule0_pass_rate * calibrated_pass_rate)
                ELSE NULL
            END AS cost_per_verified_outcome_usd
        FROM rg_ci
        ORDER BY apphub_application_id
        """
    )

    summaries: dict[str, SQLTraceEvalSummary] = {}
    for row in cur.execute("SELECT * FROM v_agent_eval_and_finops_summary").fetchall():
        app_id = str(row["apphub_application_id"])
        passes_irr = bool(row["passes_irr_gate"]) if row["passes_irr_gate"] is not None else None
        summaries[app_id] = SQLTraceEvalSummary(
            apphub_application_id=app_id,
            rubric_id=rubric_id,
            total_traces=int(row["total_traces"]),
            production_traces=int(row["production_traces"]),
            rule0_pass_rate=float(row["rule0_pass_rate"]),
            tool_success_rate=float(row["tool_success_rate"]),
            p95_latency_ms=float(row["p95_latency_ms"]),
            avg_cache_hit_ratio=float(row["avg_cache_hit_ratio"]),
            total_weighted_burndown_tokens=float(row["total_weighted_burndown_tokens"]),
            judged_samples=int(row["judged_samples"]),
            calibration_status=row["calibration_status"],
            raw_judge_pass_rate=(
                float(row["raw_judge_pass_rate"])
                if row["raw_judge_pass_rate"] is not None
                else None
            ),
            calibrated_pass_rate=(
                float(row["calibrated_pass_rate"])
                if row["calibrated_pass_rate"] is not None
                else None
            ),
            calibrated_ci_95_lower=(
                float(row["calibrated_ci_95_lower"])
                if row["calibrated_ci_95_lower"] is not None
                else None
            ),
            calibrated_ci_95_upper=(
                float(row["calibrated_ci_95_upper"])
                if row["calibrated_ci_95_upper"] is not None
                else None
            ),
            gwet_ac2=float(row["gwet_ac2"]) if row["gwet_ac2"] is not None else None,
            passes_irr_gate=passes_irr,
            production_inference_usd=float(row["production_inference_usd"]),
            conflict_escalation_usd=float(row["conflict_escalation_usd"]),
            agent_gym_eval_usd=float(row["agent_gym_eval_usd"]),
            autorater_judge_usd=float(row["autorater_judge_usd"]),
            total_spend_usd=float(row["total_spend_usd"]),
            cost_per_verified_outcome_usd=(
                float(row["cost_per_verified_outcome_usd"])
                if row["cost_per_verified_outcome_usd"] is not None
                else None
            ),
        )

    conn.close()
    return summaries
