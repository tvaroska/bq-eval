"""Evaluation Backend Ports & Adapters (local `EvalBackend` and warehouse `WarehouseEvalBackend`)."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, runtime_checkable

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.contract.results import ConfusionMatrix, SQLTraceEvalSummary
from bq_eval.core.engine.sql_eval import (
    _validate_sql_identifier,
    build_ai_generate_table_judge_sql,
    evaluate_traces_in_sqlite,
)

QueryRunner = Callable[[str], Sequence[Mapping[str, object]]]


@runtime_checkable
class EvalBackend(Protocol):
    """Local in-memory span evaluation port implemented by `SQLiteEvalBackend`."""

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
        """Evaluate traces locally and return per-application quality and FinOps summaries."""
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


@runtime_checkable
class WarehouseEvalBackend(Protocol):
    """Warehouse summary & read-only query port implemented by `BigQueryEvalBackend`."""

    project_id: str
    dataset_id: str

    def run_query(self, sql: str) -> Sequence[Mapping[str, object]]:
        """Execute a read-only SQL query and return row mappings."""
        ...

    def read_summaries(
        self,
        *,
        rubric_id: str = "groundedness_4pt_v1",
    ) -> dict[str, SQLTraceEvalSummary]:
        """Read per-application evaluation & FinOps summaries from warehouse views."""
        ...

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
        """Build the stratified Tier-2 judge SQL statement targeting this warehouse."""
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
    if isinstance(val, bool):
        raise ValueError(f"Expected numeric float value, got bool {val!r}.")
    parsed = float(val) if isinstance(val, (int, float)) else float(str(val))
    if not math.isfinite(parsed):
        raise ValueError(f"Expected finite float value, got {val!r}.")
    return parsed


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


def parse_required_int(val: object, default: int = 0) -> int:
    """Parse an integer column with an explicit fallback when optional columns are omitted."""
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        raise ValueError(f"Expected integer value, got bool {val!r}.")
    if isinstance(val, int):
        return val
    return int(float(str(val)))


def parse_required_float(val: object, default: float = 0.0) -> float:
    """Parse a finite float column with an explicit fallback for optional columns."""
    parsed = _optional_float(val)
    return default if parsed is None else parsed


def _strict_float(row: Mapping[str, object], key: str) -> float:
    parsed = _optional_float(row.get(key))
    if parsed is None:
        raise ValueError(f"Required numeric column {key!r} is NULL or missing.")
    return parsed


def _mapping_to_summary(row: Mapping[str, object]) -> SQLTraceEvalSummary:
    status_raw = str(row["calibration_status"])
    if status_raw not in {"CALIBRATED", "UNCALIBRATED", "UNJUDGED"}:
        raise ValueError(f"Invalid calibration_status {status_raw!r}")
    return SQLTraceEvalSummary.model_validate(
        {
            "apphub_application_id": str(row["apphub_application_id"]),
            "rubric_id": str(row["rubric_id"]),
            "total_traces": parse_required_int(row["total_traces"]),
            "production_traces": parse_required_int(row["production_traces"]),
            "rule0_pass_rate": _strict_float(row, "rule0_pass_rate"),
            "tool_success_rate": _strict_float(row, "tool_success_rate"),
            "p95_latency_ms": _strict_float(row, "p95_latency_ms"),
            "avg_cache_hit_ratio": parse_required_float(row.get("avg_cache_hit_ratio"), 0.0),
            "total_weighted_burndown_tokens": _strict_float(row, "total_weighted_burndown_tokens"),
            "judged_samples": parse_required_int(row["judged_samples"]),
            "calibration_status": status_raw,
            "raw_judge_pass_rate": _optional_float(row.get("raw_judge_pass_rate")),
            "calibrated_pass_rate": _optional_float(row.get("calibrated_pass_rate")),
            "calibrated_ci_95_lower": _optional_float(row.get("calibrated_ci_95_lower")),
            "calibrated_ci_95_upper": _optional_float(row.get("calibrated_ci_95_upper")),
            "gwet_ac2": _optional_float(row.get("gwet_ac2")),
            "passes_irr_gate": _optional_bool(row.get("passes_irr_gate")),
            "production_inference_usd": _strict_float(row, "production_inference_usd"),
            "conflict_escalation_usd": _strict_float(row, "conflict_escalation_usd"),
            "agent_gym_eval_usd": _strict_float(row, "agent_gym_eval_usd"),
            "autorater_judge_usd": _strict_float(row, "autorater_judge_usd"),
            "optimize_usd": parse_required_float(row.get("optimize_usd"), 0.0),
            "bq_query_cost_usd": parse_required_float(row.get("bq_query_cost_usd"), 0.0),
            "unclassified_usd": parse_required_float(row.get("unclassified_usd"), 0.0),
            "total_spend_usd": _strict_float(row, "total_spend_usd"),
            "cost_per_verified_outcome_usd": _optional_float(
                row.get("cost_per_verified_outcome_usd")
            ),
            "outcome_total_count": parse_required_int(row.get("outcome_total_count"), 0),
            "outcome_verified_count": parse_required_int(row.get("outcome_verified_count"), 0),
            "outcome_cost_per_verified_ai_usd": _optional_float(
                row.get("outcome_cost_per_verified_ai_usd")
            ),
            "outcome_cost_per_verified_usd": _optional_float(
                row.get("outcome_cost_per_verified_usd")
            ),
        }
    )


class BigQueryEvalBackend:
    """Cloud BigQuery adapter implementing `WarehouseEvalBackend` via an injectable `QueryRunner`."""

    def __init__(
        self,
        *,
        project_id: str,
        dataset_id: str = "agent_eval",
        query_runner: QueryRunner | None = None,
    ) -> None:
        self.project_id = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
        self.dataset_id = _validate_sql_identifier(dataset_id, "dataset_id")
        self._query_runner = query_runner

    def run_query(self, sql: str) -> Sequence[Mapping[str, object]]:
        """Run a read-only query through the configured `QueryRunner`."""
        if self._query_runner is None:
            raise ValueError(
                "BigQueryEvalBackend requires an explicit query_runner callable "
                "(for CLI execution, pass `bq_cli_query_runner(project_id)` from `bq_eval.deploy.engine.bq_client`)."
            )
        return self._query_runner(sql)

    def read_summaries(
        self,
        *,
        rubric_id: str = "groundedness_4pt_v1",
    ) -> dict[str, SQLTraceEvalSummary]:
        """Read per-application evaluation & FinOps summaries from BigQuery views."""
        valid_rubric = _validate_sql_identifier(rubric_id, "rubric_id")
        sql = (
            f"SELECT * FROM `{self.project_id}.{self.dataset_id}.v_agent_eval_and_finops_summary` "  # noqa: S608
            f"WHERE rubric_id = '{valid_rubric}' OR calibration_status = 'UNJUDGED' "
            "ORDER BY apphub_application_id"
        )
        rows = self.run_query(sql)
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
