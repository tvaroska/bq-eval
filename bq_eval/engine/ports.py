"""Evaluation Backend Ports & Adapters (`EvalBackend` `Protocol`, SQLite local + BigQuery cloud)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, runtime_checkable

from bq_eval.contract.otel import OTelGenAISpanRecord
from bq_eval.contract.results import (
    ConfusionMatrix,
    HourlyBurndownExportRow,
    SQLTraceEvalSummary,
)
from bq_eval.engine.sql_eval import (
    _validate_sql_identifier,
    build_ai_generate_table_judge_sql,
    evaluate_hourly_burndown_in_sqlite,
    evaluate_traces_in_sqlite,
)

QueryRunner = Callable[[str], Sequence[Mapping[str, object]]]


@runtime_checkable
class EvalBackend(Protocol):
    """Minimal evaluation execution port implemented by local SQLite and cloud BigQuery adapters."""

    def evaluate_traces(  # noqa: PLR0913
        self,
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
    ) -> dict[str, SQLTraceEvalSummary]:
        """Evaluate traces and return per-application quality and FinOps summaries."""
        ...

    def evaluate_hourly_burndown(
        self,
        spans: Sequence[OTelGenAISpanRecord],
    ) -> list[HourlyBurndownExportRow]:
        """Return hourly capacity burndown rows (`v_capacity_hourly_burndown_export`)."""
        ...

    def build_judge_sql(  # noqa: PLR0913
        self,
        *,
        project_id: str,
        dataset_id: str = "agent_eval",
        rubric_id: str = "groundedness_4pt_v1",
        canary_sample_pct: int = 5,
        run_id: str | None = None,
        judge_model: str | None = None,
        include_rule0_failures: bool = False,
    ) -> str:
        """Build the stratified Tier-2 judge SQL statement for this backend."""
        ...


class SQLiteEvalBackend:
    """Zero-credential local SQLite adapter implementing `EvalBackend`."""

    def evaluate_traces(  # noqa: PLR0913
        self,
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
    ) -> dict[str, SQLTraceEvalSummary]:
        return evaluate_traces_in_sqlite(
            spans=spans,
            judge_outcomes=judge_outcomes,
            sme_confusion=sme_confusion,
            rubric_id=rubric_id,
            judge_cost_per_call_usd=judge_cost_per_call_usd,
            sme_ordinal_pairs=sme_ordinal_pairs,
            sampling_strata=sampling_strata,
            sla_latency_ms=sla_latency_ms,
        )

    def evaluate_hourly_burndown(
        self,
        spans: Sequence[OTelGenAISpanRecord],
    ) -> list[HourlyBurndownExportRow]:
        return evaluate_hourly_burndown_in_sqlite(spans)

    def build_judge_sql(  # noqa: PLR0913
        self,
        *,
        project_id: str,
        dataset_id: str = "agent_eval",
        rubric_id: str = "groundedness_4pt_v1",
        canary_sample_pct: int = 5,
        run_id: str | None = None,
        judge_model: str | None = None,
        include_rule0_failures: bool = False,
    ) -> str:
        return build_ai_generate_table_judge_sql(
            project_id=project_id,
            dataset_id=dataset_id,
            rubric_id=rubric_id,
            canary_sample_pct=canary_sample_pct,
            run_id=run_id,
            judge_model=judge_model,
            include_rule0_failures=include_rule0_failures,
        )


def _optional_float(val: object) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    return float(str(val))


def _optional_bool(val: object) -> bool | None:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return bool(val)
    lowered = str(val).strip().lower()
    if lowered in {"true", "1"}:
        return True
    if lowered in {"false", "0"}:
        return False
    return None


def _req_int(val: object, default: int = 0) -> int:
    if val is None or val == "":
        return default
    if isinstance(val, int):
        return val
    return int(float(str(val)))


def _req_float(val: object, default: float = 0.0) -> float:
    parsed = _optional_float(val)
    return default if parsed is None else parsed


def _mapping_to_summary(row: Mapping[str, object]) -> SQLTraceEvalSummary:
    status_raw = str(row["calibration_status"])
    if status_raw not in {"CALIBRATED", "UNCALIBRATED", "UNJUDGED"}:
        raise ValueError(f"Invalid calibration_status {status_raw!r}")
    return SQLTraceEvalSummary.model_validate(
        {
            "apphub_application_id": str(row["apphub_application_id"]),
            "rubric_id": str(row["rubric_id"]),
            "total_traces": _req_int(row["total_traces"]),
            "production_traces": _req_int(row["production_traces"]),
            "rule0_pass_rate": _req_float(row["rule0_pass_rate"]),
            "tool_success_rate": _req_float(row["tool_success_rate"]),
            "p95_latency_ms": _req_float(row["p95_latency_ms"]),
            "avg_cache_hit_ratio": _req_float(row.get("avg_cache_hit_ratio"), 0.0),
            "total_weighted_burndown_tokens": _req_float(row["total_weighted_burndown_tokens"]),
            "judged_samples": _req_int(row["judged_samples"]),
            "calibration_status": status_raw,
            "raw_judge_pass_rate": _optional_float(row.get("raw_judge_pass_rate")),
            "calibrated_pass_rate": _optional_float(row.get("calibrated_pass_rate")),
            "calibrated_ci_95_lower": _optional_float(row.get("calibrated_ci_95_lower")),
            "calibrated_ci_95_upper": _optional_float(row.get("calibrated_ci_95_upper")),
            "gwet_ac2": _optional_float(row.get("gwet_ac2")),
            "passes_irr_gate": _optional_bool(row.get("passes_irr_gate")),
            "production_inference_usd": _req_float(row["production_inference_usd"]),
            "conflict_escalation_usd": _req_float(row["conflict_escalation_usd"]),
            "agent_gym_eval_usd": _req_float(row["agent_gym_eval_usd"]),
            "autorater_judge_usd": _req_float(row["autorater_judge_usd"]),
            "unclassified_usd": _req_float(row.get("unclassified_usd"), 0.0),
            "total_spend_usd": _req_float(row["total_spend_usd"]),
            "cost_per_verified_outcome_usd": _optional_float(
                row.get("cost_per_verified_outcome_usd")
            ),
            "outcome_total_count": _req_int(row.get("outcome_total_count"), 0),
            "outcome_verified_count": _req_int(row.get("outcome_verified_count"), 0),
            "outcome_cost_per_verified_ai_usd": _optional_float(
                row.get("outcome_cost_per_verified_ai_usd")
            ),
            "outcome_cost_per_verified_usd": _optional_float(
                row.get("outcome_cost_per_verified_usd")
            ),
        }
    )


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


class BigQueryEvalBackend:
    """Cloud BigQuery adapter implementing `EvalBackend` via injectable runner or `bq` CLI."""

    def __init__(
        self,
        *,
        project_id: str,
        dataset_id: str = "agent_eval",
        query_runner: QueryRunner | None = None,
    ) -> None:
        self.project_id = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
        self.dataset_id = _validate_sql_identifier(dataset_id, "dataset_id")
        self._query_runner = query_runner or self._run_bq_cli_query

    def _run_bq_cli_query(self, sql: str) -> Sequence[Mapping[str, object]]:
        bq_bin = shutil.which("bq")
        if bq_bin is None:
            raise OSError("`bq` CLI not found on PATH; install Google Cloud SDK or pass a runner.")
        proc = subprocess.run(  # noqa: S603
            [
                bq_bin,
                "query",
                f"--project_id={self.project_id}",
                "--use_legacy_sql=false",
                "--format=json",
            ],
            input=sql,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise OSError(f"BigQuery query failed: {(proc.stderr or proc.stdout).strip()}")
        raw = proc.stdout.strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("Expected JSON list output from `bq query --format=json`.")
        return parsed

    def evaluate_traces(  # noqa: PLR0913
        self,
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
    ) -> dict[str, SQLTraceEvalSummary]:
        # Cloud views read live BigQuery tables; local kwargs stay unused by design in warehouse mode.
        _ = (
            spans,
            judge_outcomes,
            sme_confusion,
            judge_cost_per_call_usd,
            sme_ordinal_pairs,
            sampling_strata,
            sla_latency_ms,
        )
        valid_rubric = _validate_sql_identifier(rubric_id, "rubric_id")
        sql = (
            f"SELECT * FROM `{self.project_id}.{self.dataset_id}.v_agent_eval_and_finops_summary` "  # noqa: S608
            f"WHERE rubric_id = '{valid_rubric}' OR calibration_status = 'UNJUDGED' "
            "ORDER BY apphub_application_id"
        )
        rows = self._query_runner(sql)
        selected: dict[str, SQLTraceEvalSummary] = {}
        for raw_row in rows:
            summary = _mapping_to_summary(raw_row)
            if summary.rubric_id == valid_rubric:
                selected[summary.apphub_application_id] = summary
            elif (
                summary.apphub_application_id not in selected
                and summary.calibration_status == "UNJUDGED"
            ):
                selected[summary.apphub_application_id] = summary.model_copy(
                    update={"rubric_id": valid_rubric}
                )
        return selected

    def evaluate_hourly_burndown(
        self,
        spans: Sequence[OTelGenAISpanRecord],
    ) -> list[HourlyBurndownExportRow]:
        _ = spans
        sql = (
            f"SELECT * FROM `{self.project_id}.{self.dataset_id}.v_capacity_hourly_burndown_export` "  # noqa: S608
            "ORDER BY timestamp, tenant, model, finops_traffic_type"
        )
        return [_mapping_to_hourly_row(r) for r in self._query_runner(sql)]

    def build_judge_sql(  # noqa: PLR0913
        self,
        *,
        project_id: str | None = None,
        dataset_id: str | None = None,
        rubric_id: str = "groundedness_4pt_v1",
        canary_sample_pct: int = 5,
        run_id: str | None = None,
        judge_model: str | None = None,
        include_rule0_failures: bool = False,
    ) -> str:
        return build_ai_generate_table_judge_sql(
            project_id=project_id or self.project_id,
            dataset_id=dataset_id or self.dataset_id,
            rubric_id=rubric_id,
            canary_sample_pct=canary_sample_pct,
            run_id=run_id,
            judge_model=judge_model,
            include_rule0_failures=include_rule0_failures,
        )
