"""Hourly capacity burndown rows from either evaluation backend."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.engine.ports import BigQueryEvalBackend, EvalBackend
from bq_eval.finops.contract.results import HourlyBurndownExportRow
from bq_eval.finops.engine.sqlite_capacity import evaluate_hourly_burndown_in_sqlite


def _req_int(val: object) -> int:
    if val is None or val == "":
        return 0
    if isinstance(val, int):
        return val
    return int(float(str(val)))


def _req_float(val: object) -> float:
    if val is None or val == "":
        return 0.0
    return float(str(val))


def _mapping_to_hourly_row(row: Mapping[str, object]) -> HourlyBurndownExportRow:
    return HourlyBurndownExportRow(
        usage_hour=str(row["usage_hour"]),
        timestamp=str(row["timestamp"]),
        apphub_application_id=str(row["apphub_application_id"]),
        tenant=str(row["tenant"]),
        model_name=str(row["model_name"]),
        model=str(row["model"]),
        finops_traffic_type=str(row["finops_traffic_type"]),
        distinct_traces=_req_int(row["distinct_traces"]),
        span_count=_req_int(row["span_count"]),
        input_tokens=_req_int(row["input_tokens"]),
        cached_input_tokens=_req_int(row["cached_input_tokens"]),
        cached_tokens=_req_int(row["cached_tokens"]),
        uncached_input_tokens=_req_int(row["uncached_input_tokens"]),
        output_tokens=_req_int(row["output_tokens"]),
        thinking_tokens=_req_int(row["thinking_tokens"]),
        thoughts_tokens=_req_int(row["thoughts_tokens"]),
        long_context_cliff_spans=_req_int(row["long_context_cliff_spans"]),
        weighted_burndown_tokens=_req_float(row["weighted_burndown_tokens"]),
        precomputed_weighted_burndown_tokens=_req_float(
            row["precomputed_weighted_burndown_tokens"]
        ),
        total_cost_usd=_req_float(row["total_cost_usd"]),
    )


def hourly_burndown_rows(
    backend: EvalBackend,
    spans: Sequence[OTelGenAISpanRecord],
) -> list[HourlyBurndownExportRow]:
    """Return `v_capacity_hourly_burndown_export` rows from BigQuery, or compute them locally."""
    if isinstance(backend, BigQueryEvalBackend):
        sql = (
            f"SELECT * FROM `{backend.project_id}.{backend.dataset_id}.v_capacity_hourly_burndown_export` "  # noqa: S608
            "ORDER BY timestamp, tenant, model, finops_traffic_type"
        )
        return [_mapping_to_hourly_row(r) for r in backend.run_query(sql)]
    return evaluate_hourly_burndown_in_sqlite(spans)
