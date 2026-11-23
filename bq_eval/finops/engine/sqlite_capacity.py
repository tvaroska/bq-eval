"""SQLite queries for hourly capacity burndown, session lineage and 5-minute breaches."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.engine.sqlite_twin import insert_sqlite_spans, open_sqlite_twin
from bq_eval.core.engine.sqlite_views import SQLITE_TABLES_DDL, SQLITE_VIEWS_DDL
from bq_eval.finops.contract.results import HourlyBurndownExportRow
from bq_eval.finops.engine.sqlite_views import FINOPS_SQLITE_VIEWS_DDL


@dataclass(frozen=True)
class InteractionSessionLineageRow:
    """SQLite twin row mirroring `agent_eval.v_interaction_session_lineage`."""

    apphub_application_id: str
    session_id: str
    turn_count: int
    max_turn_index: int
    trace_count: int
    session_duration_ms: float
    cumulative_input_tokens: int
    cumulative_output_tokens: int
    cumulative_cost_usd: float
    session_rule0_pass: bool
    first_failure_turn_index: int | None


@dataclass(frozen=True)
class LiveSloBreachRow:
    """SQLite twin row mirroring `agent_eval.v_live_slo_and_429_breaches`."""

    window_5m_epoch_ms: int
    apphub_application_id: str
    span_count: int
    http_429_count: int
    http_429_rate_pct: float
    rule0_fail_rate_pct: float
    p95_latency_ms: float
    sla_latency_ms: float
    is_breach: bool
    breach_reason: str


def evaluate_hourly_burndown_in_sqlite(
    spans: Sequence[OTelGenAISpanRecord],
) -> list[HourlyBurndownExportRow]:
    """Execute SQLite twin of `v_capacity_hourly_burndown_export`."""
    if not spans:
        return []
    conn = open_sqlite_twin()
    cur = conn.cursor()
    cur.executescript(SQLITE_TABLES_DDL)
    cur.execute(
        "INSERT INTO eval_config (rubric_id, irr_threshold, sla_latency_ms) VALUES ('groundedness_4pt_v1', 0.80, 5000.0)"
    )
    insert_sqlite_spans(cur, spans)
    cur.executescript(SQLITE_VIEWS_DDL)
    cur.executescript(FINOPS_SQLITE_VIEWS_DDL)
    rows = [
        HourlyBurndownExportRow(
            usage_hour=str(r["usage_hour"]),
            timestamp=str(r["timestamp"]),
            apphub_application_id=str(r["apphub_application_id"]),
            tenant=str(r["tenant"]),
            model_name=str(r["model_name"]),
            model=str(r["model"]),
            finops_traffic_type=str(r["finops_traffic_type"]),
            serving_traffic_type=str(r["serving_traffic_type"]),
            distinct_traces=int(r["distinct_traces"]),
            span_count=int(r["span_count"]),
            ok_span_count=int(r["ok_span_count"]),
            error_span_count=int(r["error_span_count"]),
            http_429_count=int(r["http_429_count"]),
            spillover_span_count=int(r["spillover_span_count"]),
            input_tokens=int(r["input_tokens"]),
            cached_input_tokens=int(r["cached_input_tokens"]),
            cached_tokens=int(r["cached_tokens"]),
            cache_write_5m_tokens=int(r["cache_write_5m_tokens"]),
            cache_write_1h_tokens=int(r["cache_write_1h_tokens"]),
            uncached_input_tokens=int(r["uncached_input_tokens"]),
            output_tokens=int(r["output_tokens"]),
            thinking_tokens=int(r["thinking_tokens"]),
            thoughts_tokens=int(r["thoughts_tokens"]),
            long_context_cliff_spans=int(r["long_context_cliff_spans"]),
            weighted_burndown_tokens=float(r["weighted_burndown_tokens"]),
            precomputed_weighted_burndown_tokens=float(r["precomputed_weighted_burndown_tokens"]),
            total_cost_usd=float(r["total_cost_usd"]),
        )
        for r in cur.execute("SELECT * FROM v_capacity_hourly_burndown_export").fetchall()
    ]
    conn.close()
    return rows


def evaluate_interaction_lineage_in_sqlite(
    spans: Sequence[OTelGenAISpanRecord],
) -> list[InteractionSessionLineageRow]:
    """Execute SQLite twin of `v_interaction_session_lineage`."""
    if not spans:
        return []
    conn = open_sqlite_twin()
    cur = conn.cursor()
    cur.executescript(SQLITE_TABLES_DDL)
    cur.execute(
        "INSERT INTO eval_config (rubric_id, irr_threshold, sla_latency_ms) VALUES ('groundedness_4pt_v1', 0.80, 5000.0)"
    )
    insert_sqlite_spans(cur, spans)
    cur.executescript(SQLITE_VIEWS_DDL)
    cur.executescript(FINOPS_SQLITE_VIEWS_DDL)
    rows = [
        InteractionSessionLineageRow(
            apphub_application_id=str(r["apphub_application_id"]),
            session_id=str(r["session_id"]),
            turn_count=int(r["turn_count"]),
            max_turn_index=int(r["max_turn_index"]),
            trace_count=int(r["trace_count"]),
            session_duration_ms=float(r["session_duration_ms"]),
            cumulative_input_tokens=int(r["cumulative_input_tokens"]),
            cumulative_output_tokens=int(r["cumulative_output_tokens"]),
            cumulative_cost_usd=float(r["cumulative_cost_usd"]),
            session_rule0_pass=bool(r["session_rule0_pass"]),
            first_failure_turn_index=(
                int(r["first_failure_turn_index"])
                if r["first_failure_turn_index"] is not None
                else None
            ),
        )
        for r in cur.execute("SELECT * FROM v_interaction_session_lineage").fetchall()
    ]
    conn.close()
    return rows


def evaluate_5m_breaches_in_sqlite(
    spans: Sequence[OTelGenAISpanRecord],
    *,
    sla_latency_ms: float = 5000.0,
) -> list[LiveSloBreachRow]:
    """Execute SQLite twin of `v_live_slo_and_429_breaches`."""
    if not spans:
        return []
    conn = open_sqlite_twin()
    cur = conn.cursor()
    cur.executescript(SQLITE_TABLES_DDL)
    cur.execute(
        "INSERT INTO eval_config (rubric_id, irr_threshold, sla_latency_ms) VALUES ('groundedness_4pt_v1', 0.80, ?)",
        (float(sla_latency_ms),),
    )
    insert_sqlite_spans(cur, spans)
    cur.executescript(SQLITE_VIEWS_DDL)
    cur.executescript(FINOPS_SQLITE_VIEWS_DDL)
    rows = [
        LiveSloBreachRow(
            window_5m_epoch_ms=int(r["window_5m_epoch_ms"]),
            apphub_application_id=str(r["apphub_application_id"]),
            span_count=int(r["span_count"]),
            http_429_count=int(r["http_429_count"]),
            http_429_rate_pct=float(r["http_429_rate_pct"]),
            rule0_fail_rate_pct=float(r["rule0_fail_rate_pct"]),
            p95_latency_ms=float(r["p95_latency_ms"]),
            sla_latency_ms=float(r["sla_latency_ms"]),
            is_breach=bool(r["is_breach"]),
            breach_reason=str(r["breach_reason"]),
        )
        for r in cur.execute("SELECT * FROM v_live_slo_and_429_breaches").fetchall()
    ]
    conn.close()
    return rows
