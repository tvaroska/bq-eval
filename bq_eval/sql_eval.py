"""Tier 1-3 BigQuery SQL Evaluation Builder & Local SQLite Execution Twin."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from bq_eval.calibration import ConfusionMatrix
from bq_eval.otel import OTelGenAISpanRecord
from bq_eval.rubrics import CANONICAL_RUBRICS, build_autorater_system_prompt
from bq_eval.sqlite_views import SQLITE_TABLES_DDL, SQLITE_VIEWS_DDL

_MODEL_VERSION_SUFFIX_RE = re.compile(r"-(?:\d{3}|ga|rc|qcd|preview)$")


class SQLTraceEvalSummary(BaseModel):
    """Per-application Tier 1-3 evaluation and 4-bucket FinOps summary (EVAL-P01, EVAL-P03, EVAL-P04, EVAL-P08, EVAL-P11)."""

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
    unclassified_usd: float = 0.0
    total_spend_usd: float
    cost_per_verified_outcome_usd: float | None
    outcome_total_count: int = 0
    outcome_verified_count: int = 0
    outcome_cost_per_verified_usd: float | None = None


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
    """Build production BigQuery `AI.GENERATE_TABLE` SQL query (EVAL-P02, EVAL-P08).

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
        if rubric_id == "rfp_quote_accuracy_4pt_v1":
            raise ValueError(
                "Unknown rubric_id: 'rfp_quote_accuracy_4pt_v1' "
                "(renamed to 'rfp_quote_completeness_4pt_v1')."
            )
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
    "ai_evaluate_agent_managed": (0.15, 0.015, 0.60, 0.30, 0.030, 2.25),
}


def normalize_model_name(model_name: str) -> str:
    """Strip release/version suffixes (`-001`, `-ga`, `-rc`, `-qcd`, `-preview`) before pricing lookup."""
    return _MODEL_VERSION_SUFFIX_RE.sub("", model_name.strip())


def compute_default_span_cost_usd(span: OTelGenAISpanRecord) -> float:
    """Compute span USD cost from `DEFAULT_SKU_RATES`, raising on unpriced models with token usage."""
    if (
        not span.model_name
        or span.model_name == "none"
        or (span.input_tokens == 0 and span.output_tokens == 0 and span.thinking_tokens == 0)
    ):
        return 0.0
    normalized = normalize_model_name(span.model_name)
    rates = DEFAULT_SKU_RATES.get(normalized)
    if rates is None:
        raise ValueError(
            f"Unknown model_name {span.model_name!r} (normalized {normalized!r}): "
            f"expected one of {sorted(DEFAULT_SKU_RATES)}."
        )
    uncached = span.uncached_input_tokens
    out_and_think = span.output_tokens + span.thinking_tokens
    if span.is_long_context_cliff:
        return (
            (uncached / 1e6) * rates[3]
            + (span.cached_input_tokens / 1e6) * rates[4]
            + (out_and_think / 1e6) * rates[5]
        )
    return (
        (uncached / 1e6) * rates[0]
        + (span.cached_input_tokens / 1e6) * rates[1]
        + (out_and_think / 1e6) * rates[2]
    )


def _create_sqlite_tables(cur: sqlite3.Cursor) -> None:
    cur.executescript(SQLITE_TABLES_DDL)


def _create_sqlite_views(cur: sqlite3.Cursor) -> None:
    cur.executescript(SQLITE_VIEWS_DDL)


def _seed_sqlite_sme_tables(  # noqa: PLR0913
    cur: sqlite3.Cursor,
    *,
    app_ids: Sequence[str],
    rubric_id: str,
    sme_confusion: ConfusionMatrix | Mapping[str, ConfusionMatrix] | None,
    sme_ordinal_pairs: Sequence[tuple[int, int]] | Mapping[str, Sequence[tuple[int, int]]] | None,
    passing_score: int,
) -> None:
    if isinstance(sme_ordinal_pairs, Mapping):
        for app_id, pairs in sme_ordinal_pairs.items():
            cur.executemany(
                "INSERT INTO sme_ordinal_labels (apphub_application_id, rubric_id, human_ordinal_score, judge_ordinal_score) "
                "VALUES (?, ?, ?, ?)",
                [(app_id, rubric_id, int(h), int(j)) for h, j in pairs],
            )
    elif sme_ordinal_pairs:
        for app_id in app_ids:
            cur.executemany(
                "INSERT INTO sme_ordinal_labels (apphub_application_id, rubric_id, human_ordinal_score, judge_ordinal_score) "
                "VALUES (?, ?, ?, ?)",
                [(app_id, rubric_id, int(h), int(j)) for h, j in sme_ordinal_pairs],
            )

    if isinstance(sme_confusion, Mapping):
        for app_id, cm in sme_confusion.items():
            cur.execute(
                "INSERT INTO sme_confusion_counts (apphub_application_id, rubric_id, tp, fn, tn, fp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (app_id, rubric_id, cm.tp, cm.fn, cm.tn, cm.fp),
            )
    elif sme_confusion is not None:
        for app_id in app_ids:
            cur.execute(
                "INSERT INTO sme_confusion_counts (apphub_application_id, rubric_id, tp, fn, tn, fp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    rubric_id,
                    sme_confusion.tp,
                    sme_confusion.fn,
                    sme_confusion.tn,
                    sme_confusion.fp,
                ),
            )
    elif sme_ordinal_pairs:
        cur.execute(
            """
            INSERT INTO sme_confusion_counts (apphub_application_id, rubric_id, tp, fn, tn, fp)
            SELECT
                apphub_application_id,
                rubric_id,
                SUM(CASE WHEN human_ordinal_score >= ? AND judge_ordinal_score >= ? THEN 1 ELSE 0 END),
                SUM(CASE WHEN human_ordinal_score >= ? AND judge_ordinal_score < ? THEN 1 ELSE 0 END),
                SUM(CASE WHEN human_ordinal_score < ? AND judge_ordinal_score < ? THEN 1 ELSE 0 END),
                SUM(CASE WHEN human_ordinal_score < ? AND judge_ordinal_score >= ? THEN 1 ELSE 0 END)
            FROM sme_ordinal_labels
            WHERE human_ordinal_score BETWEEN 1 AND 4 AND judge_ordinal_score BETWEEN 1 AND 4
            GROUP BY apphub_application_id, rubric_id
            """,
            (passing_score,) * 8,
        )


def _parse_outcome_verified_flag(raw_value: object) -> int | None:
    if isinstance(raw_value, bool):
        return 1 if raw_value else 0
    if isinstance(raw_value, str):
        lowered = raw_value.strip().lower()
        if lowered == "true":
            return 1
        if lowered == "false":
            return 0
    return None


def evaluate_traces_in_sqlite(  # noqa: PLR0913
    spans: Sequence[OTelGenAISpanRecord],
    judge_outcomes: dict[str, bool] | None = None,
    sme_confusion: ConfusionMatrix | Mapping[str, ConfusionMatrix] | None = None,
    rubric_id: str = "groundedness_4pt_v1",
    judge_cost_per_call_usd: float = 0.0025,
    sme_ordinal_pairs: Sequence[tuple[int, int]]
    | Mapping[str, Sequence[tuple[int, int]]]
    | None = None,
    passing_score: int = 3,
    irr_threshold: float = 0.80,
    *,
    sampling_strata: Mapping[str, str] | None = None,
    sla_latency_ms: float = 5000.0,
) -> dict[str, SQLTraceEvalSummary]:
    """Execute pure-SQLite twin of `sql/views.sql` with exact BigQuery SQL math parity.

    Computes Rule 0 (`<= 8` tools, zero tool errors), StratPPI weighted Rogan-Gladen prevalence
    calibration, 95% Delta-method CIs, Quadratic Gwet's AC2 IRR, 4-bucket + unclassified CPVO,
    and EVAL-P11 `v_cost_per_verified_outcome` directly inside SQLite SQL views.
    """
    if not spans:
        return {}

    judge_map = judge_outcomes or {}
    strata_map = sampling_strata or {}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("SQRT", 1, math.sqrt)
    conn.create_function("POW", 2, math.pow)
    conn.create_function("CEIL", 1, math.ceil)
    cur = conn.cursor()

    _create_sqlite_tables(cur)
    cur.execute(
        "INSERT INTO eval_config (rubric_id, irr_threshold, sla_latency_ms) VALUES (?, ?, ?)",
        (rubric_id, float(irr_threshold), float(sla_latency_ms)),
    )

    for s in spans:
        cost_usd = compute_default_span_cost_usd(s)
        raw_status = s.attributes.get("outcome.status")
        outcome_status = str(raw_status) if isinstance(raw_status, str) and raw_status else None
        outcome_verified = _parse_outcome_verified_flag(s.attributes.get("outcome.verified"))
        cur.execute(
            """
            INSERT INTO otel_spans VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                s.trace_id,
                s.span_id,
                s.parent_span_id,
                s.span_name,
                s.resolved_application_id,
                s.outcome_id,
                s.outcome_owner,
                s.source_mode,
                s.agent_name,
                s.service_name,
                str(s.span_kind),
                s.model_name,
                s.tool_call_status,
                s.status_code,
                1 if s.rule0_pass else 0,
                str(s.finops_traffic_type),
                outcome_status,
                outcome_verified,
                s.start_time.timestamp() * 1000.0,
                s.end_time.timestamp() * 1000.0,
                s.latency_ms,
                s.input_tokens,
                s.cached_input_tokens,
                s.uncached_input_tokens,
                s.output_tokens,
                s.thinking_tokens,
                s.weighted_burndown_tokens,
                cost_usd,
            ),
        )

    _create_sqlite_views(cur)

    trace_meta = {
        str(row["trace_id"]): (
            str(row["apphub_application_id"]),
            float(row["trace_latency_ms"]),
            int(row["tool_error_count"]),
        )
        for row in cur.execute(
            "SELECT trace_id, apphub_application_id, trace_latency_ms, tool_error_count FROM v_trace_sql_evaluation"
        ).fetchall()
    }
    app_ids = sorted({meta[0] for meta in trace_meta.values()})

    eval_rows: list[tuple[str, str, str, int, float]] = []
    for tid, passed in judge_map.items():
        if tid in strata_map:
            stratum = strata_map[tid]
        elif tid in trace_meta and (trace_meta[tid][2] > 0 or trace_meta[tid][1] > sla_latency_ms):
            stratum = "ERROR_OR_SLA_TAIL"
        else:
            stratum = "CANARY_5PCT"
        eval_rows.append((tid, rubric_id, stratum, 1 if passed else 0, judge_cost_per_call_usd))
    cur.executemany(
        "INSERT INTO eval_results (trace_id, rubric_id, sampling_stratum, binary_pass, judge_cost_usd) "
        "VALUES (?, ?, ?, ?, ?)",
        eval_rows,
    )

    _seed_sqlite_sme_tables(
        cur,
        app_ids=app_ids,
        rubric_id=rubric_id,
        sme_confusion=sme_confusion,
        sme_ordinal_pairs=sme_ordinal_pairs,
        passing_score=passing_score,
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
            unclassified_usd=float(row["unclassified_usd"]),
            total_spend_usd=float(row["total_spend_usd"]),
            cost_per_verified_outcome_usd=(
                float(row["cost_per_verified_outcome_usd"])
                if row["cost_per_verified_outcome_usd"] is not None
                else None
            ),
            outcome_total_count=int(row["outcome_total_count"]),
            outcome_verified_count=int(row["outcome_verified_count"]),
            outcome_cost_per_verified_usd=(
                float(row["outcome_cost_per_verified_usd"])
                if row["outcome_cost_per_verified_usd"] is not None
                else None
            ),
        )

    conn.close()
    return summaries
