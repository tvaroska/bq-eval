"""Shared test fixtures, span factories, and Silver + Gold runner helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlglot
from sqlglot import exp

from bq_eval.core.contract.otel import (
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
    ServingTrafficType,
    TracePayloadRecord,
)
from bq_eval.core.contract.results import (
    BqQueryCostRow,
    ConfusionMatrix,
    SQLTraceEvalSummary,
)
from bq_eval.finops.contract.results import HourlyBurndownExportRow
from bq_eval.finops.engine.capacity_export import hourly_burndown_rows
from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.bronze_bridge import (
    open_silver_gold_runner,
    populate_runner_from_spans,
)
from bq_eval.runner.engine.eval_adapter import OfflineEvalBackend
from bq_eval.runner.engine.loader import OfflineSqlRunner
from scripts.bq_dry_run import qualify_project_datasets, split_sql_statements

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"


def make_span(  # noqa: PLR0913
    trace_id: str = "trace-001",
    span_id: str = "span-001",
    *,
    parent_span_id: str | None = None,
    span_name: str = "invoke_agent",
    app_id: str | None = "critical-app",
    service_name: str = "agent-service",
    agent_name: str = "default-agent",
    otel_span_kind: str = "SPAN_KIND_INTERNAL",
    span_kind: GenAISpanKind = GenAISpanKind.INVOKE_AGENT,
    status_code: str = "OK",
    model_name: str = "gemini-3.8-flash",
    tool_name: str | None = None,
    tool_call_status: str | None = None,
    rule0_pass: bool = True,
    finops_traffic_type: FinOpsTrafficType = FinOpsTrafficType.PRODUCTION_INFERENCE,
    input_tokens: int = 10_000,
    cached_input_tokens: int = 2_000,
    output_tokens: int = 500,
    thinking_tokens: int = 100,
    latency_ms: float = 400.0,
    offset_s: int = 0,
    offset_ms: int = 0,
    outcome_id: str | None = None,
    outcome_owner: str | None = None,
    source_mode: str | None = None,
    skill_id: str | None = None,
    skill_version: str | None = None,
    serving_traffic_type: ServingTrafficType = ServingTrafficType.STANDARD,
    request_type_header: str | None = None,
    http_status_code: int | None = None,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    session_id: str | None = None,
    turn_index: int | None = None,
    attributes: Mapping[str, object] | None = None,
    base_time: datetime | None = None,
) -> OTelGenAISpanRecord:
    """Canonical `OTelGenAISpanRecord` test factory shared across the test suite."""
    anchor = base_time or datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
    start = anchor + timedelta(seconds=offset_s, milliseconds=offset_ms)
    return OTelGenAISpanRecord(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        span_name=span_name,
        start_time=start,
        end_time=start + timedelta(milliseconds=latency_ms),
        latency_ms=latency_ms,
        status_code=status_code,
        otel_span_kind=otel_span_kind,
        span_kind=span_kind,
        service_name=service_name,
        gcp_apphub_application_id=app_id,
        agent_name=agent_name,
        model_name=model_name,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        tool_name=tool_name,
        tool_call_status=tool_call_status,
        rule0_pass=rule0_pass,
        finops_traffic_type=finops_traffic_type,
        outcome_id=outcome_id,
        outcome_owner=outcome_owner,
        source_mode=source_mode,
        skill_id=skill_id,
        skill_version=skill_version,
        serving_traffic_type=serving_traffic_type,
        request_type_header=request_type_header,
        http_status_code=http_status_code,
        cache_write_5m_tokens=cache_write_5m_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
        session_id=session_id,
        turn_index=turn_index,
        attributes=dict(attributes) if attributes is not None else {},
    )


def eval_traces(  # noqa: PLR0913
    spans: Sequence[OTelGenAISpanRecord],
    *,
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
    sampling_strata: Mapping[str, str] | None = None,
    sla_latency_ms: float = 5000.0,
    bq_query_jobs: Sequence[BqQueryCostRow] = (),
) -> dict[str, SQLTraceEvalSummary]:
    """Evaluate `spans` through `OfflineEvalBackend` over Silver + Gold SQL."""
    return OfflineEvalBackend().evaluate_traces(
        spans=spans,
        judge_outcomes=judge_outcomes,
        sme_confusion=sme_confusion,
        rubric_id=rubric_id,
        judge_cost_per_call_usd=judge_cost_per_call_usd,
        sme_ordinal_pairs=sme_ordinal_pairs,
        sampling_strata=sampling_strata,
        sla_latency_ms=sla_latency_ms,
        bq_query_jobs=bq_query_jobs,
    )


def eval_all_rubrics(  # noqa: PLR0913
    spans: Sequence[OTelGenAISpanRecord],
    *,
    judge_outcomes: Mapping[str, bool] | None = None,
    rubric_outcomes_by_id: Mapping[str, Mapping[str, bool]] | None = None,
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
    sampling_strata: Mapping[str, str] | None = None,
    sla_latency_ms: float = 5000.0,
    bq_query_jobs: Sequence[BqQueryCostRow] = (),
) -> dict[tuple[str, str], SQLTraceEvalSummary]:
    """Evaluate `spans` across multiple rubrics through `OfflineEvalBackend`."""
    return OfflineEvalBackend().evaluate_all_rubrics(
        spans=spans,
        judge_outcomes=judge_outcomes,
        rubric_outcomes_by_id=rubric_outcomes_by_id,
        sme_confusion=sme_confusion,
        rubric_id=rubric_id,
        judge_cost_per_call_usd=judge_cost_per_call_usd,
        sme_ordinal_pairs=sme_ordinal_pairs,
        sampling_strata=sampling_strata,
        sla_latency_ms=sla_latency_ms,
        bq_query_jobs=bq_query_jobs,
    )


def eval_hourly_burndown(spans: Sequence[OTelGenAISpanRecord]) -> list[HourlyBurndownExportRow]:
    """Evaluate `v_capacity_hourly_burndown_export` through `OfflineEvalBackend`."""
    return hourly_burndown_rows(OfflineEvalBackend(), spans)


def open_runner_for_spans(  # noqa: PLR0913
    spans: Sequence[OTelGenAISpanRecord] = (),
    *,
    payloads: Sequence[TracePayloadRecord] = (),
    sla_latency_ms: float = 5000.0,
    bq_query_jobs: Sequence[BqQueryCostRow] = (),
    current_date_utc: str | None = None,
    current_timestamp_utc: str | None = None,
) -> OfflineSqlRunner:
    """Open an `OfflineSqlRunner` pre-loaded with Silver + Gold schemas and optional `spans`."""
    if spans and (current_date_utc is None or current_timestamp_utc is None):
        now_dt = max(s.end_time.astimezone(UTC) for s in spans) + timedelta(minutes=10)
        date_str = current_date_utc or now_dt.strftime("%Y-%m-%d")
        ts_str = current_timestamp_utc or now_dt.isoformat()
    else:
        date_str = current_date_utc or "2026-11-22"
        ts_str = current_timestamp_utc or "2026-11-22T18:00:00+00:00"
    runner = open_silver_gold_runner(
        RunnerConfig(
            current_date_utc=date_str,
            current_timestamp_utc=ts_str,
            default_dataset="agent_eval",
        )
    )
    if spans:
        populate_runner_from_spans(
            runner,
            spans,
            payloads=payloads,
            sla_latency_ms=sla_latency_ms,
            bq_query_jobs=bq_query_jobs,
        )
    return runner


def seed_sme_confusion(  # noqa: PLR0913
    runner: OfflineSqlRunner,
    app_id: str,
    cm: ConfusionMatrix,
    *,
    rubric_id: str = "groundedness_4pt_v1",
    labeled_at: str = "2026-11-21T12:10:00+00:00",
    sampling_stratum: str | None = None,
) -> None:
    """Insert `ConfusionMatrix` rows into `agent_eval.sme_labels` inside `runner`."""
    rows: list[dict[str, object]] = []
    seq = 0
    for h_ord, h_pass, j_ord, j_pass, count in (
        (4, True, 4, True, cm.tp),
        (4, True, 2, False, cm.fn),
        (1, False, 1, False, cm.tn),
        (1, False, 4, True, cm.fp),
    ):
        for _ in range(count):
            seq += 1
            rows.append(
                {
                    "label_id": f"sme-{app_id}-{rubric_id}-{sampling_stratum or 'all'}-{seq}",
                    "trace_id": f"sme-tr-{app_id}-{seq}",
                    "apphub_application_id": app_id,
                    "rubric_id": rubric_id,
                    "rater_id": "sme_alice",
                    "sampling_stratum": sampling_stratum,
                    "rule0_pass": True,
                    "human_ordinal_score": h_ord,
                    "human_pass": h_pass,
                    "judge_ordinal_score": j_ord,
                    "judge_pass": j_pass,
                    "labeled_at": labeled_at,
                }
            )
    if rows:
        runner.load_rows("agent_eval.sme_labels", rows, append=True)


def normalize_ai_tvf_sql(sql: str) -> str:
    """Normalize `AI.GENERATE_TABLE` / `AI.EVALUATE_AGENT` `MODEL` & `TABLE` keywords for `sqlglot`."""
    out = re.sub(r"(AI\.GENERATE_TABLE\s*\(\s*)MODEL\s+", r"\1", sql, flags=re.IGNORECASE)
    out = re.sub(
        r"(AI\.(?:GENERATE_TABLE|EVALUATE_AGENT)\s*\([^;]*?,\s*)TABLE\s+",
        r"\1",
        out,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"(AI\.EVALUATE_AGENT\s*\(\s*)TABLE\s+", r"\1", out, flags=re.IGNORECASE)


def parse_bigquery_sql(sql_text: str, label: str = "sql") -> list[exp.Expr]:
    """Parse BigQuery SQL text into `sqlglot` AST nodes after substituting bootstrap placeholders."""
    qualified = normalize_ai_tvf_sql(qualify_project_datasets(sql_text, "showcase-project"))
    out: list[exp.Expr] = []
    for idx, stmt in enumerate(split_sql_statements(qualified), start=1):
        upper = stmt.upper()
        if upper.startswith(
            (
                "CREATE OR REPLACE PROCEDURE",
                "CREATE PROCEDURE",
                "CREATE MODEL",
                "CREATE EXTERNAL TABLE",
                "CREATE OR REPLACE EXTERNAL TABLE",
                "CREATE SCHEMA",
                "ALTER TABLE",
            )
        ):
            continue
        parsed = sqlglot.parse_one(stmt, read="bigquery")
        assert parsed is not None, f"{label}#{idx} returned None AST"
        assert not isinstance(parsed, exp.Command), (
            f"{label}#{idx} fell back to unparsed Command AST: {stmt[:120]!r}"
        )
        out.append(parsed)
    return out
