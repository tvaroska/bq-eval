"""Local SQLite copy of the BigQuery tables: open it and load spans and query jobs."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from datetime import UTC

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.contract.results import BqQueryCostRow
from bq_eval.core.engine.cost import compute_span_burndown_tokens, compute_span_cost_usd


def open_sqlite_twin() -> sqlite3.Connection:
    """Create an in-memory SQLite connection with BigQuery math UDFs registered."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("SQRT", 1, math.sqrt)
    conn.create_function("POW", 2, math.pow)
    conn.create_function("CEIL", 1, math.ceil)
    return conn


def _parse_outcome_verified_flag(raw_value: object) -> int | None:
    """Parse `outcome.verified` fail-closed: absent/blank -> None, `'true'` -> 1, anything else -> 0."""
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        return 1 if raw_value else 0
    if isinstance(raw_value, str):
        lowered = raw_value.strip().lower()
        if not lowered:
            return None
        return 1 if lowered == "true" else 0
    return 0


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
    """Seed `bq_query_jobs` rows into the SQLite twin."""
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
