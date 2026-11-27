"""Hourly capacity burndown rows from either offline or warehouse evaluation ports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.engine.ports import (
    EvalBackend,
    WarehouseEvalBackend,
    parse_required_float,
    parse_required_int,
)
from bq_eval.finops.contract.results import HourlyBurndownExportRow


def _mapping_to_hourly_row(row: Mapping[str, object]) -> HourlyBurndownExportRow:
    return HourlyBurndownExportRow(
        usage_hour=str(row["usage_hour"]),
        timestamp=str(row["timestamp"]),
        apphub_application_id=str(row["apphub_application_id"]),
        tenant=str(row["tenant"]),
        model_name=str(row["model_name"]),
        model=str(row["model"]),
        finops_traffic_type=str(row["finops_traffic_type"]),
        serving_traffic_type=str(row.get("serving_traffic_type") or "STANDARD"),
        distinct_traces=parse_required_int(row["distinct_traces"]),
        span_count=parse_required_int(row["span_count"]),
        ok_span_count=parse_required_int(row.get("ok_span_count"), 0),
        error_span_count=parse_required_int(row.get("error_span_count"), 0),
        http_429_count=parse_required_int(row.get("http_429_count"), 0),
        spillover_span_count=parse_required_int(row.get("spillover_span_count"), 0),
        input_tokens=parse_required_int(row["input_tokens"]),
        cached_input_tokens=parse_required_int(row["cached_input_tokens"]),
        cached_tokens=parse_required_int(row["cached_tokens"]),
        cache_write_5m_tokens=parse_required_int(row.get("cache_write_5m_tokens"), 0),
        cache_write_1h_tokens=parse_required_int(row.get("cache_write_1h_tokens"), 0),
        uncached_input_tokens=parse_required_int(row["uncached_input_tokens"]),
        output_tokens=parse_required_int(row["output_tokens"]),
        thinking_tokens=parse_required_int(row["thinking_tokens"]),
        thoughts_tokens=parse_required_int(row["thoughts_tokens"]),
        long_context_cliff_spans=parse_required_int(row["long_context_cliff_spans"]),
        weighted_burndown_tokens=parse_required_float(row["weighted_burndown_tokens"]),
        precomputed_weighted_burndown_tokens=parse_required_float(
            row["precomputed_weighted_burndown_tokens"]
        ),
        total_cost_usd=parse_required_float(row["total_cost_usd"]),
    )


def hourly_burndown_rows(
    backend: EvalBackend | WarehouseEvalBackend,
    spans: Sequence[OTelGenAISpanRecord] = (),
) -> list[HourlyBurndownExportRow]:
    """Return `v_capacity_hourly_burndown_export` rows via warehouse or offline evaluation port."""
    if isinstance(backend, WarehouseEvalBackend):
        sql = (
            f"SELECT * FROM `{backend.project_id}.{backend.dataset_id}.v_capacity_hourly_burndown_export` "  # noqa: S608
            "ORDER BY timestamp, tenant, model, finops_traffic_type, serving_traffic_type"
        )
        return [_mapping_to_hourly_row(r) for r in backend.run_query(sql)]
    return [_mapping_to_hourly_row(r) for r in backend.evaluate_hourly_burndown_dicts(spans)]
