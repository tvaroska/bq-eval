"""Tier 1-3 BigQuery SQL Evaluation Builder & Local SQLite Execution Twin."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.contract.pricing import (
    JUDGE_MODEL_ENDPOINTS,
    MANAGED_EVALUATE_AGENT_METRICS,
    normalize_model_name,
)
from bq_eval.core.contract.results import BqQueryCostRow, ConfusionMatrix, SQLTraceEvalSummary
from bq_eval.core.contract.rubrics import CANONICAL_RUBRICS, build_autorater_system_prompt
from bq_eval.core.engine.sqlite_twin import (
    insert_sqlite_bq_query_jobs,
    insert_sqlite_spans,
    open_sqlite_twin,
)
from bq_eval.core.engine.sqlite_views import SQLITE_TABLES_DDL, SQLITE_VIEWS_DDL

__all__ = [
    "JUDGE_MODEL_ENDPOINTS",
    "MANAGED_EVALUATE_AGENT_METRICS",
    "build_ai_evaluate_agent_sql",
    "build_ai_generate_table_judge_sql",
    "escape_sql_xml_expr",
    "evaluate_all_rubrics_in_sqlite",
    "evaluate_step1_traces_in_sqlite",
    "evaluate_traces_in_sqlite",
    "normalize_model_name",
]


_COLUMN_EXPR_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$")


def escape_sql_xml_expr(column_expr: str) -> str:
    """Return BigQuery SQL expression that XML-escapes `&`, `<`, and `>` in untrusted columns."""
    cleaned = column_expr.strip()
    if not cleaned or not _COLUMN_EXPR_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid column_expr {column_expr!r}: must be a simple or dotted SQL identifier."
        )
    return (
        f"REPLACE(REPLACE(REPLACE(COALESCE({cleaned}, ''), "
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


def _validate_policy_sha256(policy_sha256: str | None) -> str | None:
    if policy_sha256 is None:
        return None
    cleaned = policy_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        raise ValueError(
            f"Invalid policy_sha256 {policy_sha256!r}: must be 64 lowercase hex chars."
        )
    return cleaned


def _validate_amended_clause_ids(clause_ids: Sequence[str] | None) -> str | None:
    if not clause_ids:
        return None
    validated: list[str] = []
    for cid in clause_ids:
        cleaned = cid.strip()
        if not cleaned or not re.fullmatch(r"[a-zA-Z0-9_.:-]+", cleaned):
            raise ValueError(f"Invalid amended clause ID {cid!r}.")
        validated.append(cleaned)
    return ",".join(validated)


def build_ai_generate_table_judge_sql(  # noqa: PLR0913
    project_id: str,
    dataset_id: str = "agent_eval",
    rubric_id: str = "groundedness_4pt_v1",
    canary_sample_pct: int = 5,
    run_id: str | None = None,
    judge_model: str | None = None,
    include_rule0_failures: bool = True,
    policy_sha256: str | None = None,
    amended_clause_ids: Sequence[str] | None = None,
) -> str:
    """Build production BigQuery `AI.GENERATE_TABLE` SQL query."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_dataset = _validate_sql_identifier(dataset_id, "dataset_id")
    valid_policy_sha256 = _validate_policy_sha256(policy_sha256)
    valid_clauses_csv = _validate_amended_clause_ids(amended_clause_ids)
    if rubric_id not in CANONICAL_RUBRICS:
        if rubric_id == "rfp_quote_accuracy_4pt_v1":
            raise ValueError(
                "Unknown rubric_id: 'rfp_quote_accuracy_4pt_v1' "
                "(renamed to 'rfp_quote_completeness_4pt_v1')."
            )
        raise ValueError(f"Unknown rubric_id: {rubric_id}")
    if (
        isinstance(canary_sample_pct, bool)
        or not isinstance(canary_sample_pct, int)
        or not (0 <= canary_sample_pct <= 100)
    ):
        raise ValueError("canary_sample_pct must be an integer between 0 and 100 (not bool).")
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
    policy_sha_expr = f"'{valid_policy_sha256}'" if valid_policy_sha256 else "CAST(NULL AS STRING)"
    amended_expr = f"'{valid_clauses_csv}'" if valid_clauses_csv else "CAST(NULL AS STRING)"
    if include_rule0_failures:
        stratum_condition = (
            "NOT t.rule0_pass OR t.tool_error_count > 0 OR t.trace_latency_ms > t.sla_latency_ms"
        )
        where_clause = (
            "  WHERE (\n"
            "    NOT t.rule0_pass\n"
            "    OR t.tool_error_count > 0\n"
            "    OR t.trace_latency_ms > t.sla_latency_ms\n"
            f"    OR MOD(ABS(FARM_FINGERPRINT(t.trace_id)), 100) < {canary_sample_pct}\n"
            f"  ){run_filter}"
        )
    else:
        stratum_condition = "t.tool_error_count > 0 OR t.trace_latency_ms > t.sla_latency_ms"
        where_clause = (
            "  WHERE t.rule0_pass\n"
            "    AND (\n"
            "      t.tool_error_count > 0\n"
            "      OR t.trace_latency_ms > t.sla_latency_ms\n"
            f"      OR MOD(ABS(FARM_FINGERPRINT(t.trace_id)), 100) < {canary_sample_pct}\n"
            f"    ){run_filter}"
        )

    return f"""-- Tier 2 Stratified AI.GENERATE_TABLE Judge Execution ({rubric.rubric_id})
WITH candidate_traces AS (
  SELECT
    t.trace_id,
    t.outcome_id,
    t.apphub_application_id,
    t.rule0_pass,
    CASE
      WHEN {stratum_condition}
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
{where_clause}
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
  {policy_sha_expr} AS policy_sha256,
  {amended_expr} AS amended_clause_ids,
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
    persist_run_id: str | None = None,
) -> str:
    """Build Step-1 pre-filtered BigQuery `AI.EVALUATE_AGENT` SQL query (Engine B)."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_dataset = _validate_sql_identifier(dataset_id, "dataset_id")
    valid_view = _validate_sql_identifier(eval_view, "eval_view")
    if not managed_metrics:
        raise ValueError("At least one managed metric is required.")
    unknown = [m for m in managed_metrics if m not in MANAGED_EVALUATE_AGENT_METRICS]
    if unknown:
        raise ValueError(f"Unknown AI.EVALUATE_AGENT managed metric(s): {unknown}")
    if (
        isinstance(canary_sample_pct, bool)
        or not isinstance(canary_sample_pct, int)
        or not (0 <= canary_sample_pct <= 100)
    ):
        raise ValueError("canary_sample_pct must be an integer between 0 and 100 (not bool).")
    valid_run_id = _validate_run_id(run_id)
    valid_persist_run_id = _validate_run_id(persist_run_id)

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

    if valid_persist_run_id is not None:
        cand_where = f"WHERE {' AND '.join(predicates)} AND " if predicates else "WHERE "
        return f"""-- Tier 3 Engine B Persistence: Idempotent AI.EVALUATE_AGENT -> eval_results ({canary_sample_pct}% Sample)
INSERT INTO `{valid_project}.{valid_dataset}.eval_results` (
  eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id, judge_model,
  sampling_stratum, rule0_pass, reasoning_trace, ordinal_score, binary_pass,
  judge_input_tokens, judge_output_tokens, judge_thinking_tokens, evaluated_at
)
WITH evaluated AS (
  SELECT
    eval_out.trace_id,
    eval_out.outcome_id,
    eval_out.apphub_application_id,
    metric_row.metric_name AS rubric_id,
    eval_out.step1_pass AS rule0_pass,
    COALESCE(metric_row.explanation, 'Managed AI.EVALUATE_AGENT verdict') AS reasoning_trace,
    CAST(ROUND(LEAST(4.0, GREATEST(1.0, metric_row.score * 3.0 + 1.0))) AS INT64) AS ordinal_score,
    DIV(CHAR_LENGTH(TO_JSON_STRING(eval_out.trace)), 4) AS judge_input_tokens,
    DIV(CHAR_LENGTH(COALESCE(metric_row.explanation, '')), 4) AS judge_output_tokens
  FROM AI.EVALUATE_AGENT(
    TABLE (
      SELECT cand.*
      FROM `{valid_project}.{valid_dataset}.{valid_view}` AS cand
      LEFT JOIN (
        SELECT trace_id, COUNT(DISTINCT rubric_id) AS scored_metrics
        FROM `{valid_project}.{valid_dataset}.eval_results`
        WHERE eval_run_id = '{valid_persist_run_id}'
          AND rubric_id IN ({metrics_sql})
        GROUP BY trace_id
      ) AS completed
        ON cand.trace_id = completed.trace_id
      {cand_where}COALESCE(completed.scored_metrics, 0) < {len(managed_metrics)}
    ),
    trace_col => 'trace',
    managed_metrics => [
      {metrics_sql}
    ]
  ) AS eval_out,
  UNNEST(eval_out.evaluation_results) AS metric_row
  LEFT JOIN `{valid_project}.{valid_dataset}.eval_results` AS existing
    ON eval_out.trace_id = existing.trace_id
    AND metric_row.metric_name = existing.rubric_id
    AND existing.eval_run_id = '{valid_persist_run_id}'
  WHERE metric_row.score IS NOT NULL
    AND existing.eval_id IS NULL
)
SELECT
  GENERATE_UUID() AS eval_id,
  '{valid_persist_run_id}' AS eval_run_id,
  trace_id,
  outcome_id,
  apphub_application_id,
  rubric_id,
  'ai_evaluate_agent_managed' AS judge_model,
  'SAMPLE_{canary_sample_pct}PCT' AS sampling_stratum,
  rule0_pass,
  reasoning_trace,
  ordinal_score,
  (ordinal_score >= 3) AS binary_pass,
  judge_input_tokens,
  judge_output_tokens,
  0 AS judge_thinking_tokens,
  CURRENT_TIMESTAMP() AS evaluated_at
FROM evaluated;
"""  # noqa: S608

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


def _seed_sqlite_sme_tables(  # noqa: PLR0913
    cur: sqlite3.Cursor,
    *,
    app_ids: Sequence[str],
    rubric_id: str,
    sme_confusion: ConfusionMatrix
    | Mapping[str, ConfusionMatrix]
    | Mapping[tuple[str, str], ConfusionMatrix]
    | None,
    sme_ordinal_pairs: Sequence[tuple[int, int]]
    | Mapping[str, Sequence[tuple[int, int]]]
    | Mapping[tuple[str, str], Sequence[tuple[int, int]]]
    | None,
    passing_score: int,
) -> None:
    if isinstance(sme_ordinal_pairs, Mapping):
        for key, pairs in sme_ordinal_pairs.items():
            app_k, rub_k = key if isinstance(key, tuple) else (key, rubric_id)
            cur.executemany(
                "INSERT INTO sme_ordinal_labels (apphub_application_id, rubric_id, human_ordinal_score, judge_ordinal_score) "
                "VALUES (?, ?, ?, ?)",
                [(app_k, rub_k, int(h), int(j)) for h, j in pairs],
            )
    elif sme_ordinal_pairs:
        for app_id in app_ids:
            cur.executemany(
                "INSERT INTO sme_ordinal_labels (apphub_application_id, rubric_id, human_ordinal_score, judge_ordinal_score) "
                "VALUES (?, ?, ?, ?)",
                [(app_id, rubric_id, int(h), int(j)) for h, j in sme_ordinal_pairs],
            )

    if isinstance(sme_confusion, Mapping):
        for key, cm in sme_confusion.items():
            app_k, rub_k = key if isinstance(key, tuple) else (key, rubric_id)
            cur.execute(
                "INSERT INTO sme_confusion_counts (apphub_application_id, rubric_id, tp, fn, tn, fp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (app_k, rub_k, cm.tp, cm.fn, cm.tn, cm.fp),
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


def _row_to_summary(row: sqlite3.Row) -> SQLTraceEvalSummary:
    passes_irr = bool(row["passes_irr_gate"]) if row["passes_irr_gate"] is not None else None
    return SQLTraceEvalSummary(
        apphub_application_id=str(row["apphub_application_id"]),
        rubric_id=str(row["rubric_id"]),
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
            float(row["raw_judge_pass_rate"]) if row["raw_judge_pass_rate"] is not None else None
        ),
        calibrated_pass_rate=(
            float(row["calibrated_pass_rate"]) if row["calibrated_pass_rate"] is not None else None
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
        optimize_usd=float(row["optimize_usd"]),
        bq_query_cost_usd=float(row["bq_query_cost_usd"]),
        unclassified_usd=float(row["unclassified_usd"]),
        total_spend_usd=float(row["total_spend_usd"]),
        cost_per_verified_outcome_usd=(
            float(row["cost_per_verified_outcome_usd"])
            if row["cost_per_verified_outcome_usd"] is not None
            else None
        ),
        outcome_total_count=int(row["outcome_total_count"]),
        outcome_verified_count=int(row["outcome_verified_count"]),
        outcome_cost_per_verified_ai_usd=(
            float(row["outcome_cost_per_verified_ai_usd"])
            if row["outcome_cost_per_verified_ai_usd"] is not None
            else None
        ),
        outcome_cost_per_verified_usd=(
            float(row["outcome_cost_per_verified_usd"])
            if row["outcome_cost_per_verified_usd"] is not None
            else None
        ),
    )


def evaluate_all_rubrics_in_sqlite(  # noqa: PLR0913
    spans: Sequence[OTelGenAISpanRecord],
    judge_outcomes: Mapping[str, bool] | None = None,
    sme_confusion: ConfusionMatrix
    | Mapping[str, ConfusionMatrix]
    | Mapping[tuple[str, str], ConfusionMatrix]
    | None = None,
    rubric_id: str = "groundedness_4pt_v1",
    judge_cost_per_call_usd: float = 0.0025,
    sme_ordinal_pairs: Sequence[tuple[int, int]]
    | Mapping[str, Sequence[tuple[int, int]]]
    | Mapping[tuple[str, str], Sequence[tuple[int, int]]]
    | None = None,
    passing_score: int = 3,
    irr_threshold: float = 0.80,
    *,
    sampling_strata: Mapping[str, str] | None = None,
    sla_latency_ms: float = 5000.0,
    extra_rubric_outcomes: Mapping[str, Mapping[str, bool]] | None = None,
    external_apps: frozenset[str] = frozenset(),
    bq_query_jobs: Sequence[BqQueryCostRow] | None = None,
) -> dict[tuple[str, str], SQLTraceEvalSummary]:
    """Execute pure-SQLite twin returning every `(apphub_application_id, rubric_id)` row."""
    if not spans:
        return {}

    strata_map = sampling_strata or {}
    conn = open_sqlite_twin()
    cur = conn.cursor()
    cur.executescript(SQLITE_TABLES_DDL)
    cur.execute(
        "INSERT INTO eval_config (rubric_id, irr_threshold, sla_latency_ms) VALUES (?, ?, ?)",
        (rubric_id, float(irr_threshold), float(sla_latency_ms)),
    )
    insert_sqlite_spans(cur, spans, external_apps=external_apps)
    insert_sqlite_bq_query_jobs(cur, bq_query_jobs)
    cur.executescript(SQLITE_VIEWS_DDL)

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

    rubric_maps: dict[str, Mapping[str, bool]] = {}
    if extra_rubric_outcomes:
        rubric_maps.update(extra_rubric_outcomes)
    if judge_outcomes is not None:
        rubric_maps[rubric_id] = judge_outcomes

    eval_rows: list[tuple[str, str, str, int, float]] = []
    for rub_key, outcome_map in rubric_maps.items():
        for tid, passed in outcome_map.items():
            if tid in strata_map:
                stratum = strata_map[tid]
            elif tid in trace_meta and (
                trace_meta[tid][2] > 0 or trace_meta[tid][1] > sla_latency_ms
            ):
                stratum = "ERROR_OR_SLA_TAIL"
            else:
                stratum = "CANARY_5PCT"
            eval_rows.append((tid, rub_key, stratum, 1 if passed else 0, judge_cost_per_call_usd))
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

    summaries = {
        (str(row["apphub_application_id"]), str(row["rubric_id"])): _row_to_summary(row)
        for row in cur.execute("SELECT * FROM v_agent_eval_and_finops_summary").fetchall()
    }
    conn.close()
    return summaries


def evaluate_traces_in_sqlite(  # noqa: PLR0913
    spans: Sequence[OTelGenAISpanRecord],
    judge_outcomes: Mapping[str, bool] | None = None,
    sme_confusion: ConfusionMatrix
    | Mapping[str, ConfusionMatrix]
    | Mapping[tuple[str, str], ConfusionMatrix]
    | None = None,
    rubric_id: str = "groundedness_4pt_v1",
    judge_cost_per_call_usd: float = 0.0025,
    sme_ordinal_pairs: Sequence[tuple[int, int]]
    | Mapping[str, Sequence[tuple[int, int]]]
    | Mapping[tuple[str, str], Sequence[tuple[int, int]]]
    | None = None,
    passing_score: int = 3,
    irr_threshold: float = 0.80,
    *,
    sampling_strata: Mapping[str, str] | None = None,
    sla_latency_ms: float = 5000.0,
    extra_rubric_outcomes: Mapping[str, Mapping[str, bool]] | None = None,
    external_apps: frozenset[str] = frozenset(),
    bq_query_jobs: Sequence[BqQueryCostRow] | None = None,
) -> dict[str, SQLTraceEvalSummary]:
    """Execute pure-SQLite twin of `sql/core/views.sql`, returning 1 summary per app for `rubric_id`."""
    all_rows = evaluate_all_rubrics_in_sqlite(
        spans=spans,
        judge_outcomes=judge_outcomes,
        sme_confusion=sme_confusion,
        rubric_id=rubric_id,
        judge_cost_per_call_usd=judge_cost_per_call_usd,
        sme_ordinal_pairs=sme_ordinal_pairs,
        passing_score=passing_score,
        irr_threshold=irr_threshold,
        sampling_strata=sampling_strata,
        sla_latency_ms=sla_latency_ms,
        extra_rubric_outcomes=extra_rubric_outcomes,
        external_apps=external_apps,
        bq_query_jobs=bq_query_jobs,
    )
    selected: dict[str, SQLTraceEvalSummary] = {}
    for (app_id, row_rubric), summary in all_rows.items():
        if row_rubric == rubric_id or (
            app_id not in selected and summary.calibration_status == "UNJUDGED"
        ):
            selected[app_id] = summary
    return selected


def evaluate_step1_traces_in_sqlite(
    spans: Sequence[OTelGenAISpanRecord],
) -> dict[str, dict[str, object]]:
    """Return canonical Step-1 per-trace metrics from SQLite `v_trace_sql_evaluation`."""
    if not spans:
        return {}
    conn = open_sqlite_twin()
    cur = conn.cursor()
    cur.executescript(SQLITE_TABLES_DDL)
    cur.execute(
        "INSERT INTO eval_config (rubric_id, irr_threshold, sla_latency_ms) VALUES ('groundedness_4pt_v1', 0.80, 5000.0)"
    )
    insert_sqlite_spans(cur, spans)
    cur.executescript(SQLITE_VIEWS_DDL)
    rows: dict[str, dict[str, object]] = {
        str(r["trace_id"]): {
            "apphub_application_id": str(r["apphub_application_id"]),
            "trace_latency_ms": float(r["trace_latency_ms"]),
            "rule0_pass": bool(r["rule0_pass"]),
            "tool_error_count": int(r["tool_error_count"]),
            "cache_hit_ratio": (
                float(r["cache_hit_ratio"]) if r["cache_hit_ratio"] is not None else None
            ),
            "trace_span_cost_usd": float(r["trace_span_cost_usd"]),
        }
        for r in cur.execute(
            "SELECT trace_id, apphub_application_id, trace_latency_ms, rule0_pass, "
            "tool_error_count, cache_hit_ratio, trace_span_cost_usd FROM v_trace_sql_evaluation"
        ).fetchall()
    }
    conn.close()
    return rows
