"""Tier 1-3 BigQuery SQL Evaluation Builder & Local SQLite Execution Twin."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from bq_eval.calibration import ConfusionMatrix, rogan_gladen_calibrate
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


def build_ai_generate_table_judge_sql(
    project_id: str,
    dataset_id: str = "agent_eval",
    rubric_id: str = "groundedness_4pt_v1",
    canary_sample_pct: int = 5,
) -> str:
    """Build production BigQuery `AI.GENERATE_TABLE` SQL query (EVAL-P08).

    Enforces:
    1. Stratified sampling: 100% of deterministic failures (`NOT rule0_pass`, tool errors,
       or SLA breaches) plus `canary_sample_pct`% (`FARM_FINGERPRINT`) of healthy traces.
    2. Nested SQL `REPLACE` XML escaping inside `<untrusted_text>` blocks so prompt payloads
       cannot break out of XML delimiters.
    3. CoT-first output struct: `reasoning_trace STRING, ordinal_score INT64, binary_pass BOOL`.
    """
    if rubric_id not in CANONICAL_RUBRICS:
        raise ValueError(f"Unknown rubric_id: {rubric_id}")
    if not (0 <= canary_sample_pct <= 100):
        raise ValueError("canary_sample_pct must be between 0 and 100.")

    rubric = CANONICAL_RUBRICS[rubric_id]
    system_prompt = build_autorater_system_prompt(rubric).replace("'", "\\'")
    escaped_prompt = escape_sql_xml_expr("p.user_prompt")
    escaped_context = escape_sql_xml_expr("p.retrieved_context")
    escaped_response = escape_sql_xml_expr("p.final_response")

    return f"""-- Tier 2 Stratified AI.GENERATE_TABLE Judge Execution ({rubric.rubric_id})
WITH candidate_traces AS (
  SELECT
    t.trace_id,
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
  FROM `{project_id}.{dataset_id}.v_trace_sql_evaluation` AS t
  INNER JOIN `{project_id}.{dataset_id}.trace_payloads` AS p
    ON t.trace_id = p.trace_id
  WHERE
    NOT t.rule0_pass
    OR t.tool_error_count > 0
    OR t.trace_latency_ms > t.sla_latency_ms
    OR MOD(ABS(FARM_FINGERPRINT(t.trace_id)), 100) < {canary_sample_pct}
)
SELECT
  GENERATE_UUID() AS eval_id,
  trace_id,
  apphub_application_id,
  '{rubric.rubric_id}' AS rubric_id,
  '{rubric.judge_model}' AS judge_model,
  sampling_stratum,
  rule0_pass,
  reasoning_trace,
  ordinal_score,
  (ordinal_score >= {rubric.passing_score}) AS binary_pass,
  CURRENT_TIMESTAMP() AS evaluated_at
FROM AI.GENERATE_TABLE(
  MODEL `{project_id}.{dataset_id}.gemini_4_argon_judge`,
  TABLE candidate_traces,
  STRUCT(
    'reasoning_trace STRING, ordinal_score INT64, binary_pass BOOL' AS output_schema,
    0.0 AS temperature
  )
);
"""


DEFAULT_SKU_RATES: dict[str, tuple[float, float, float, float, float, float]] = {
    # model_name: (in_1m, cached_1m, out_1m, long_in_1m, long_cached_1m, long_out_1m)
    "gemini-3.5-flash-lite": (0.075, 0.0075, 0.30, 0.15, 0.015, 1.125),
    "gemini-3.8-flash": (0.15, 0.015, 0.60, 0.30, 0.030, 2.25),
    "gemini-4-argon": (1.25, 0.125, 5.00, 2.50, 0.250, 18.75),
}


def evaluate_traces_in_sqlite(
    spans: Sequence[OTelGenAISpanRecord],
    judge_outcomes: dict[str, bool] | None = None,
    sme_confusion: ConfusionMatrix | None = None,
    rubric_id: str = "groundedness_4pt_v1",
    judge_cost_per_call_usd: float = 0.0025,
) -> dict[str, SQLTraceEvalSummary]:
    """Execute deterministic SQLite twin of `sql/views.sql` with exact BigQuery parity.

    Mirrors:
    - Portable `COALESCE` application ID resolution (`EVAL-P07`)
    - Span-level `<=200K` and `>200K` burndown cliff (`EVAL-P04`)
    - Conditional `Rule 0` conditioning on `judge_outcomes` (`EVAL-P05`)
    - Fail-closed `None` (`NULL`) guard when `judged_samples == 0` or `Youden J <= 1e-6` (`D3`)
    """
    if not spans:
        return {}

    judge_map = judge_outcomes or {}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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

    cur.execute(
        """
        CREATE VIEW v_trace_sql_evaluation AS
        SELECT
            trace_id,
            apphub_application_id,
            MAX(end_epoch_ms) - MIN(start_epoch_ms) AS trace_latency_ms,
            SUM(CASE WHEN span_kind = 'execute_tool' AND (status_code = 'ERROR' OR tool_call_status != 'SUCCESS') THEN 1 ELSE 0 END) AS tool_error_count,
            CASE WHEN MIN(rule0_pass) = 1 AND SUM(CASE WHEN status_code = 'ERROR' THEN 1 ELSE 0 END) = 0 THEN 1 ELSE 0 END AS rule0_pass,
            MAX(finops_traffic_type) AS primary_traffic_type,
            SUM(input_tokens) AS total_input_tokens,
            SUM(cached_input_tokens) AS total_cached_input_tokens,
            CASE WHEN SUM(input_tokens) > 0 THEN CAST(SUM(cached_input_tokens) AS REAL) / SUM(input_tokens) ELSE 0.0 END AS cache_hit_ratio,
            SUM(weighted_burndown_tokens) AS weighted_burndown_tokens,
            SUM(CASE WHEN finops_traffic_type = 'production_inference' THEN span_cost_usd ELSE 0.0 END) AS production_inference_usd,
            SUM(CASE WHEN finops_traffic_type = 'conflict_escalation' THEN span_cost_usd ELSE 0.0 END) AS conflict_escalation_usd,
            SUM(CASE WHEN finops_traffic_type = 'agent_gym_eval' THEN span_cost_usd ELSE 0.0 END) AS agent_gym_eval_usd,
            SUM(CASE WHEN finops_traffic_type = 'autorater_judge' THEN span_cost_usd ELSE 0.0 END) AS span_autorater_judge_usd
        FROM otel_spans
        GROUP BY trace_id, apphub_application_id
        """
    )

    rows = cur.execute("SELECT * FROM v_trace_sql_evaluation").fetchall()
    by_app: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_app.setdefault(row["apphub_application_id"], []).append(row)

    summaries: dict[str, SQLTraceEvalSummary] = {}
    for app_id, app_rows in sorted(by_app.items()):
        total_traces = len(app_rows)
        prod_traces = sum(
            1
            for r in app_rows
            if r["primary_traffic_type"] in ("production_inference", "conflict_escalation")
        )
        rule0_pass_rate = sum(r["rule0_pass"] for r in app_rows) / total_traces
        tool_success_rate = (
            sum(1 for r in app_rows if r["tool_error_count"] == 0) / total_traces
        )
        latencies = sorted(float(r["trace_latency_ms"]) for r in app_rows)
        p95_idx = min(len(latencies) - 1, max(0, math.ceil(0.95 * len(latencies)) - 1))
        p95_latency = latencies[p95_idx]
        avg_cache_hit = sum(float(r["cache_hit_ratio"]) for r in app_rows) / total_traces
        total_burndown = sum(float(r["weighted_burndown_tokens"]) for r in app_rows)

        prod_usd = sum(float(r["production_inference_usd"]) for r in app_rows)
        esc_usd = sum(float(r["conflict_escalation_usd"]) for r in app_rows)
        gym_usd = sum(float(r["agent_gym_eval_usd"]) for r in app_rows)
        span_judge_usd = sum(float(r["span_autorater_judge_usd"]) for r in app_rows)

        # Conditional Rule 0 conditioning: only Rule-0-passing judged traces enter p_hat_obs,
        # while all judge calls on this app contribute to autorater_judge_usd.
        all_judged_for_app = [r for r in app_rows if r["trace_id"] in judge_map]
        rule0_judged_outcomes = [
            judge_map[r["trace_id"]]
            for r in all_judged_for_app
            if r["rule0_pass"] == 1
        ]
        judged_samples = len(rule0_judged_outcomes)
        table_judge_usd = len(all_judged_for_app) * judge_cost_per_call_usd
        autorater_usd = max(span_judge_usd, table_judge_usd)
        total_spend = prod_usd + esc_usd + gym_usd + autorater_usd

        raw_pass_rate: float | None = None
        calibrated_pass: float | None = None
        ci_lower: float | None = None
        ci_upper: float | None = None
        cal_status: Literal["CALIBRATED", "UNCALIBRATED", "UNJUDGED"] = "UNJUDGED"

        if judged_samples > 0:
            raw_pass_rate = sum(1 for passed in rule0_judged_outcomes if passed) / judged_samples
            if sme_confusion is not None:
                try:
                    rg = rogan_gladen_calibrate(
                        observed_pass_rate=raw_pass_rate,
                        sample_size=judged_samples,
                        confusion=sme_confusion,
                    )
                    calibrated_pass = rg.calibrated_pass_rate
                    ci_lower = rg.ci_95_lower
                    ci_upper = rg.ci_95_upper
                    cal_status = "CALIBRATED"
                except ValueError:
                    cal_status = "UNCALIBRATED"
            else:
                cal_status = "UNCALIBRATED"

        cpvo: float | None = None
        if (
            calibrated_pass is not None
            and calibrated_pass > 0.0
            and rule0_pass_rate > 0.0
            and prod_traces > 0
        ):
            verified_outcomes = prod_traces * rule0_pass_rate * calibrated_pass
            cpvo = total_spend / verified_outcomes

        summaries[app_id] = SQLTraceEvalSummary(
            apphub_application_id=app_id,
            rubric_id=rubric_id,
            total_traces=total_traces,
            production_traces=prod_traces,
            rule0_pass_rate=rule0_pass_rate,
            tool_success_rate=tool_success_rate,
            p95_latency_ms=p95_latency,
            avg_cache_hit_ratio=avg_cache_hit,
            total_weighted_burndown_tokens=total_burndown,
            judged_samples=judged_samples,
            calibration_status=cal_status,
            raw_judge_pass_rate=raw_pass_rate,
            calibrated_pass_rate=calibrated_pass,
            calibrated_ci_95_lower=ci_lower,
            calibrated_ci_95_upper=ci_upper,
            production_inference_usd=prod_usd,
            conflict_escalation_usd=esc_usd,
            agent_gym_eval_usd=gym_usd,
            autorater_judge_usd=autorater_usd,
            total_spend_usd=total_spend,
            cost_per_verified_outcome_usd=cpvo,
        )

    conn.close()
    return summaries
