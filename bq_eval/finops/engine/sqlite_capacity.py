"""SQLite query for hourly capacity burndown export."""

from __future__ import annotations

from collections.abc import Sequence

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.engine.sqlite_twin import insert_sqlite_spans, open_sqlite_twin
from bq_eval.core.engine.sqlite_views import SQLITE_TABLES_DDL, SQLITE_VIEWS_DDL
from bq_eval.finops.contract.results import HourlyBurndownExportRow
from bq_eval.finops.engine.sqlite_views import FINOPS_SQLITE_VIEWS_DDL


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
