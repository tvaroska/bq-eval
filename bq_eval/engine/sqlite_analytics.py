"""Extended SQLite twin queries for hourly burndown (`BQ-G1`), `/interactions` lineage, and 5m breaches."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC

from bq_eval.contract.governance import BqQueryCostRow
from bq_eval.contract.otel import OTelGenAISpanRecord
from bq_eval.contract.results import HourlyBurndownExportRow
from bq_eval.engine.finops import compute_span_burndown_tokens, compute_span_cost_usd
from bq_eval.engine.sqlite_views import SQLITE_TABLES_DDL, SQLITE_VIEWS_DDL


@dataclass(frozen=True)
class InteractionSessionLineageRow:
    """SQLite twin row mirroring `agent_eval.v_interaction_session_lineage` (`M11-4`)."""

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
    """SQLite twin row mirroring `agent_eval.v_live_slo_and_429_breaches` (`M11-4`)."""

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


def open_sqlite_twin() -> sqlite3.Connection:
    """Create an in-memory SQLite connection with BigQuery math UDFs registered."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("SQRT", 1, math.sqrt)
    conn.create_function("POW", 2, math.pow)
    conn.create_function("CEIL", 1, math.ceil)
    return conn


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


def _parse_optional_float(raw_value: object, default: float | None = None) -> float | None:
    if raw_value is None or isinstance(raw_value, bool):
        return default
    try:
        parsed = float(str(raw_value))
    except ValueError:
        return default
    return parsed if math.isfinite(parsed) else default


def insert_sqlite_spans(
    cur: sqlite3.Cursor,
    spans: Sequence[OTelGenAISpanRecord],
    *,
    external_apps: frozenset[str] = frozenset(),
) -> None:
    """Insert `OTelGenAISpanRecord` rows into the SQLite twin `otel_spans` table."""
    for s in spans:
        cost_usd = compute_span_cost_usd(s)
        burndown = compute_span_burndown_tokens(s)
        raw_status = s.attributes.get("outcome.status")
        outcome_status = str(raw_status) if isinstance(raw_status, str) and raw_status else None
        outcome_verified = _parse_outcome_verified_flag(s.attributes.get("outcome.verified"))
        is_ext = (
            1
            if (
                s.resolved_application_id in external_apps
                or (s.outcome_owner is not None and s.outcome_owner in external_apps)
                or str(s.attributes.get("apphub.external", "")).strip().lower() == "true"
            )
            else 0
        )
        service_vcpu = _parse_optional_float(s.attributes.get("service.vcpu"), 2.0) or 2.0
        service_gib = _parse_optional_float(s.attributes.get("service.gib"), 2.0) or 2.0
        outcome_storage_usd = _parse_optional_float(s.attributes.get("outcome.storage_usd"), None)
        start_utc = (
            s.start_time.replace(tzinfo=UTC)
            if s.start_time.tzinfo is None
            else s.start_time.astimezone(UTC)
        )
        hour_iso = start_utc.replace(minute=0, second=0, microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        cur.execute(
            """
            INSERT INTO otel_spans VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                s.otel_span_kind,
                str(s.span_kind),
                s.model_name,
                s.tool_call_status,
                s.status_code,
                1 if s.rule0_pass else 0,
                str(s.finops_traffic_type),
                str(s.serving_traffic_type),
                s.request_type_header,
                s.http_status_code,
                s.cache_write_5m_tokens,
                s.cache_write_1h_tokens,
                s.session_id,
                s.turn_index,
                is_ext,
                service_vcpu,
                service_gib,
                outcome_storage_usd,
                outcome_status,
                outcome_verified,
                hour_iso,
                s.start_time.timestamp() * 1000.0,
                s.end_time.timestamp() * 1000.0,
                s.latency_ms,
                s.input_tokens,
                s.cached_input_tokens,
                s.uncached_input_tokens,
                s.output_tokens,
                s.thinking_tokens,
                1 if s.is_long_context_cliff else 0,
                burndown,
                cost_usd,
            ),
        )


def insert_sqlite_bq_query_jobs(
    cur: sqlite3.Cursor,
    query_jobs: Sequence[BqQueryCostRow] | None,
) -> None:
    """Seed `bq_query_jobs` rows into the SQLite twin (`M10-3`)."""
    if not query_jobs:
        return
    cur.executemany(
        "INSERT INTO bq_query_jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row.job_id,
                row.apphub_application_id,
                row.outcome_id,
                row.total_bytes_billed,
                row.total_slot_ms,
                row.pricing_mode,
                row.query_cost_usd,
            )
            for row in query_jobs
        ],
    )


def evaluate_hourly_burndown_in_sqlite(
    spans: Sequence[OTelGenAISpanRecord],
) -> list[HourlyBurndownExportRow]:
    """Execute SQLite twin of `v_capacity_hourly_burndown_export` (`M7-3` / `M11-2` / `BQ-G1`)."""
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
    """Execute SQLite twin of `v_interaction_session_lineage` (`M11-4`)."""
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
    """Execute SQLite twin of `v_live_slo_and_429_breaches` (`M11-4`)."""
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
