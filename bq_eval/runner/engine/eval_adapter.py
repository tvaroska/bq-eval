"""Offline Silver + Gold SQL evaluation adapter (`OfflineEvalBackend` & `< 5s` `CI_ONLY` runner)."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, timedelta

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.contract.results import (
    BqQueryCostRow,
    ConfusionMatrix,
    SQLTraceEvalSummary,
)
from bq_eval.core.contract.rubrics import CANONICAL_RUBRICS
from bq_eval.core.engine.ports import _mapping_to_summary
from bq_eval.core.engine.sql_eval import build_ai_generate_table_judge_sql
from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.bronze_bridge import (
    GOLD_BOOT_ORDER as GOLD_BOOT_ORDER,
)
from bq_eval.runner.engine.bronze_bridge import (
    SILVER_BOOT_ORDER as SILVER_BOOT_ORDER,
)
from bq_eval.runner.engine.bronze_bridge import (
    open_silver_gold_runner as open_silver_gold_runner,
)
from bq_eval.runner.engine.bronze_bridge import (
    populate_runner_from_spans as populate_runner_from_spans,
)
from bq_eval.runner.engine.bronze_bridge import (
    spans_to_bronze_bundle as spans_to_bronze_bundle,
)


def _resolve_per_app_rubric_cm(
    sme_confusion: ConfusionMatrix
    | Mapping[str, ConfusionMatrix]
    | Mapping[tuple[str, str], ConfusionMatrix]
    | None,
    app_id: str,
    rubric_id: str,
) -> ConfusionMatrix | None:
    if sme_confusion is None:
        return None
    if isinstance(sme_confusion, ConfusionMatrix):
        return sme_confusion
    for key, val in sme_confusion.items():
        if key == (app_id, rubric_id):
            return val
    for key, val in sme_confusion.items():
        if key == app_id:
            return val
    return None


def _resolve_per_app_rubric_pairs(
    sme_ordinal_pairs: Sequence[tuple[int, int]]
    | Mapping[str, Sequence[tuple[int, int]]]
    | Mapping[tuple[str, str], Sequence[tuple[int, int]]]
    | None,
    app_id: str,
    rubric_id: str,
) -> Sequence[tuple[int, int]] | None:
    if sme_ordinal_pairs is None:
        return None
    if isinstance(sme_ordinal_pairs, Mapping):
        for key, val in sme_ordinal_pairs.items():
            if key == (app_id, rubric_id):
                return val
        for key, val in sme_ordinal_pairs.items():
            if key == app_id:
                return val
        return None
    return sme_ordinal_pairs


def _make_sme_row(  # noqa: PLR0913
    prefix: str,
    seq: int,
    app_id: str,
    rub_id: str,
    labeled_at_iso: str,
    *,
    h_ord: int | None,
    h_pass: bool | None,
    j_ord: int | None,
    j_pass: bool | None,
) -> dict[str, object]:
    return {
        "label_id": f"{prefix}-{seq}",
        "trace_id": f"sme-tr-{seq}",
        "apphub_application_id": app_id,
        "rubric_id": rub_id,
        "rater_id": "offline_sme",
        "rule0_pass": True,
        "human_ordinal_score": h_ord,
        "human_pass": h_pass,
        "judge_ordinal_score": j_ord,
        "judge_pass": j_pass,
        "labeled_at": labeled_at_iso,
    }


def _sme_rows_for_app_rubric(  # noqa: PLR0913
    app_id: str,
    rub_id: str,
    passing_score: int,
    cm: ConfusionMatrix | None,
    pairs: Sequence[tuple[int, int]] | None,
    labeled_at_iso: str,
    seq: int,
) -> tuple[list[dict[str, object]], int]:
    rows: list[dict[str, object]] = []
    if cm is not None and pairs is not None:
        for h_pass, j_pass, count in (
            (True, True, cm.tp),
            (True, False, cm.fn),
            (False, False, cm.tn),
            (False, True, cm.fp),
        ):
            for _ in range(count):
                seq += 1
                rows.append(
                    _make_sme_row(
                        "sme-cm",
                        seq,
                        app_id,
                        rub_id,
                        labeled_at_iso,
                        h_ord=None,
                        h_pass=h_pass,
                        j_ord=None,
                        j_pass=j_pass,
                    )
                )
        for h_ord, j_ord in pairs:
            seq += 1
            rows.append(
                _make_sme_row(
                    "sme-ord",
                    seq,
                    app_id,
                    rub_id,
                    labeled_at_iso,
                    h_ord=int(h_ord),
                    h_pass=None,
                    j_ord=int(j_ord),
                    j_pass=None,
                )
            )
    elif cm is not None:
        for h_ord, h_pass, j_ord, j_pass, count in (
            (4, True, 4, True, cm.tp),
            (4, True, 2, False, cm.fn),
            (1, False, 1, False, cm.tn),
            (1, False, 4, True, cm.fp),
        ):
            for _ in range(count):
                seq += 1
                rows.append(
                    _make_sme_row(
                        "sme-full",
                        seq,
                        app_id,
                        rub_id,
                        labeled_at_iso,
                        h_ord=h_ord,
                        h_pass=h_pass,
                        j_ord=j_ord,
                        j_pass=j_pass,
                    )
                )
    elif pairs is not None:
        for h_ord, j_ord in pairs:
            seq += 1
            rows.append(
                _make_sme_row(
                    "sme-pair",
                    seq,
                    app_id,
                    rub_id,
                    labeled_at_iso,
                    h_ord=int(h_ord),
                    h_pass=int(h_ord) >= passing_score,
                    j_ord=int(j_ord),
                    j_pass=int(j_ord) >= passing_score,
                )
            )
    return rows, seq


def _build_sme_label_rows(
    app_ids: Sequence[str],
    rubric_ids: Sequence[str],
    sme_confusion: ConfusionMatrix
    | Mapping[str, ConfusionMatrix]
    | Mapping[tuple[str, str], ConfusionMatrix]
    | None,
    sme_ordinal_pairs: Sequence[tuple[int, int]]
    | Mapping[str, Sequence[tuple[int, int]]]
    | Mapping[tuple[str, str], Sequence[tuple[int, int]]]
    | None,
    labeled_at_iso: str,
) -> list[dict[str, object]]:
    sme_rows: list[dict[str, object]] = []
    seq = 0
    for app_id in app_ids:
        for rub_id in rubric_ids:
            passing_score = (
                CANONICAL_RUBRICS[rub_id].passing_score if rub_id in CANONICAL_RUBRICS else 3
            )
            batch, seq = _sme_rows_for_app_rubric(
                app_id,
                rub_id,
                passing_score,
                _resolve_per_app_rubric_cm(sme_confusion, app_id, rub_id),
                _resolve_per_app_rubric_pairs(sme_ordinal_pairs, app_id, rub_id),
                labeled_at_iso,
                seq,
            )
            sme_rows.extend(batch)
    return sme_rows


def _fixed_judge_pricing_row(judge_cost_per_call_usd: float) -> dict[str, object]:
    out_rate = float(judge_cost_per_call_usd) * 1_000_000.0
    return {
        "model_name": "offline_judge_fixed_rate",
        "provider": "google",
        "lifecycle_track": "long_term_ga",
        "effective_start": "1970-01-01T00:00:00+00:00",
        "effective_end": "2099-12-31T23:59:59+00:00",
        "input_usd_per_1m": 1.0,
        "cached_input_usd_per_1m": 0.0,
        "cache_write_5m_usd_per_1m": 0.0,
        "cache_write_1h_usd_per_1m": 0.0,
        "output_usd_per_1m": out_rate,
        "long_context_threshold_tokens": None,
        "long_context_input_usd_per_1m": 1.0,
        "long_context_cached_usd_per_1m": 0.0,
        "long_context_output_usd_per_1m": out_rate,
        "burndown_uncached_weight": 1.0,
        "burndown_cached_weight": 0.1,
        "burndown_cache_write_5m_weight": 1.0,
        "burndown_cache_write_1h_weight": 1.0,
        "burndown_output_weight": 4.0,
        "burndown_long_uncached_weight": 1.0,
        "burndown_long_cached_weight": 0.1,
        "burndown_long_output_weight": 4.0,
        "priority_paygo_multiplier": 1.8,
        "flex_paygo_multiplier": 0.5,
        "min_cache_tokens": 0,
    }


def _collect_rubric_outcomes(
    rubric_id: str,
    judge_outcomes: Mapping[str, bool] | None,
    rubric_outcomes_by_id: Mapping[str, Mapping[str, bool]] | None,
) -> dict[str, Mapping[str, bool]]:
    rubric_map: dict[str, Mapping[str, bool]] = {}
    if judge_outcomes:
        rubric_map[rubric_id] = judge_outcomes
    if rubric_outcomes_by_id:
        rubric_map.update({k: v for k, v in rubric_outcomes_by_id.items() if v})
    return rubric_map


def _build_offline_eval_rows(
    rubric_map: Mapping[str, Mapping[str, bool]],
    trace_eval_rows: Mapping[str, Mapping[str, object]],
    sampling_strata: Mapping[str, str] | None,
    evaluated_at_iso: str,
) -> list[dict[str, object]]:
    eval_rows: list[dict[str, object]] = []
    seq = 0
    for rub_id, outcomes in rubric_map.items():
        for tid, passed in outcomes.items():
            t_meta = trace_eval_rows.get(tid)
            app_id = str(t_meta["apphub_application_id"]) if t_meta else "unregistered_app"
            r0_ok = bool(t_meta["rule0_pass"]) if t_meta else False
            out_id = t_meta["outcome_id"] if t_meta else None
            stratum = (
                sampling_strata.get(tid, "CANARY_5PCT")
                if sampling_strata is not None
                else "CANARY_5PCT"
            )
            seq += 1
            eval_rows.append(
                {
                    "eval_id": f"offline-ev-{seq}",
                    "eval_run_id": "offline-run-1",
                    "trace_id": tid,
                    "outcome_id": out_id,
                    "apphub_application_id": app_id,
                    "rubric_id": rub_id,
                    "rubric_version": "1.0",
                    "judge_model": "offline_judge_fixed_rate",
                    "sampling_stratum": stratum,
                    "rule0_pass": r0_ok,
                    "reasoning_trace": "offline verdict",
                    "ordinal_score": 4 if passed else 1,
                    "binary_pass": bool(passed),
                    "judge_status": "SCORED",
                    "judge_input_tokens": 0,
                    "judge_output_tokens": 1,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": evaluated_at_iso,
                }
            )
    return eval_rows


class OfflineEvalBackend:
    """In-memory Silver + Gold BigQuery SQL evaluation adapter (`bq_eval.runner`)."""

    def _run_eval_session(  # noqa: PLR0913
        self,
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
        include_burndown: bool = False,
    ) -> tuple[dict[tuple[str, str], SQLTraceEvalSummary], Sequence[Mapping[str, object]]]:
        if not spans:
            return {}, ()
        max_end = max(s.end_time.astimezone(UTC) for s in spans)
        now_dt = max_end + timedelta(minutes=10)
        cfg = RunnerConfig(
            current_date_utc=now_dt.strftime("%Y-%m-%d"),
            current_timestamp_utc=now_dt.isoformat(),
            default_dataset="agent_eval",
        )
        with open_silver_gold_runner(cfg) as runner:
            populate_runner_from_spans(
                runner,
                spans,
                sla_latency_ms=sla_latency_ms,
                bq_query_jobs=bq_query_jobs,
            )
            rubric_map = _collect_rubric_outcomes(rubric_id, judge_outcomes, rubric_outcomes_by_id)
            if rubric_map:
                runner.load_rows(
                    "agent_eval.model_pricing_catalog",
                    [_fixed_judge_pricing_row(judge_cost_per_call_usd)],
                    append=True,
                )
            trace_eval_rows = {
                str(r["trace_id"]): r
                for r in runner.query(
                    "SELECT trace_id, apphub_application_id, outcome_id, rule0_pass "
                    "FROM `agent_eval.v_trace_sql_evaluation`"
                ).rows
            }
            app_ids = sorted({str(r["apphub_application_id"]) for r in trace_eval_rows.values()})
            evaluated_at_iso = (max_end + timedelta(minutes=1)).isoformat()
            eval_rows = _build_offline_eval_rows(
                rubric_map, trace_eval_rows, sampling_strata, evaluated_at_iso
            )
            if eval_rows:
                runner.load_rows("agent_eval.eval_results", eval_rows, append=True)

            sme_rows = _build_sme_label_rows(
                app_ids,
                sorted(set(rubric_map) | {rubric_id}),
                sme_confusion,
                sme_ordinal_pairs,
                evaluated_at_iso,
            )
            if sme_rows:
                runner.load_rows("agent_eval.sme_labels", sme_rows, append=True)

            raw_rows = runner.query(
                "SELECT * FROM `agent_eval.v_agent_eval_and_finops_summary` "
                "ORDER BY apphub_application_id, rubric_id"
            ).rows
            hourly_rows: Sequence[Mapping[str, object]] = (
                runner.query(
                    "SELECT * FROM `agent_eval.v_capacity_hourly_burndown_export` "
                    "ORDER BY timestamp, tenant, model, finops_traffic_type, serving_traffic_type"
                ).rows
                if include_burndown
                else ()
            )
            out: dict[tuple[str, str], SQLTraceEvalSummary] = {}
            for r in raw_rows:
                summary = _mapping_to_summary(r)
                if summary.calibration_status == "UNJUDGED" and rubric_id != summary.rubric_id:
                    summary = summary.model_copy(update={"rubric_id": rubric_id})
                out[(summary.apphub_application_id, summary.rubric_id)] = summary
            return out, hourly_rows

    def evaluate_all_rubrics(  # noqa: PLR0913
        self,
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
        """Evaluate Silver + Gold SQL across one or more rubrics and return `(app_id, rubric_id)` summaries."""
        summaries, _ = self._run_eval_session(
            spans,
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
        return summaries

    def evaluate_traces_and_burndown(  # noqa: PLR0913
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
        bq_query_jobs: Sequence[BqQueryCostRow] = (),
    ) -> tuple[dict[str, SQLTraceEvalSummary], Sequence[Mapping[str, object]]]:
        """Evaluate application summaries and hourly burndown in one Silver + Gold runner session."""
        all_res, hourly_rows = self._run_eval_session(
            spans,
            judge_outcomes=judge_outcomes,
            sme_confusion=sme_confusion,
            rubric_id=rubric_id,
            judge_cost_per_call_usd=judge_cost_per_call_usd,
            sme_ordinal_pairs=sme_ordinal_pairs,
            sampling_strata=sampling_strata,
            sla_latency_ms=sla_latency_ms,
            bq_query_jobs=bq_query_jobs,
            include_burndown=True,
        )
        selected: dict[str, SQLTraceEvalSummary] = {}
        for (app_id, rub), summary in all_res.items():
            if rub == rubric_id or app_id not in selected:
                selected[app_id] = summary
        return selected, hourly_rows

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
        bq_query_jobs: Sequence[BqQueryCostRow] = (),
    ) -> dict[str, SQLTraceEvalSummary]:
        """Evaluate traces through Silver + Gold SQL and return per-application summaries."""
        all_res = self.evaluate_all_rubrics(
            spans,
            judge_outcomes=judge_outcomes,
            sme_confusion=sme_confusion,
            rubric_id=rubric_id,
            judge_cost_per_call_usd=judge_cost_per_call_usd,
            sme_ordinal_pairs=sme_ordinal_pairs,
            sampling_strata=sampling_strata,
            sla_latency_ms=sla_latency_ms,
            bq_query_jobs=bq_query_jobs,
        )
        selected: dict[str, SQLTraceEvalSummary] = {}
        for (app_id, rub), summary in all_res.items():
            if rub == rubric_id or app_id not in selected:
                selected[app_id] = summary
        return selected

    def evaluate_hourly_burndown_dicts(
        self,
        spans: Sequence[OTelGenAISpanRecord],
    ) -> Sequence[Mapping[str, object]]:
        """Evaluate Silver + Gold `v_capacity_hourly_burndown_export` rows locally."""
        if not spans:
            return ()
        max_end = max(s.end_time.astimezone(UTC) for s in spans)
        now_dt = max_end + timedelta(minutes=10)
        cfg = RunnerConfig(
            current_date_utc=now_dt.strftime("%Y-%m-%d"),
            current_timestamp_utc=now_dt.isoformat(),
            default_dataset="agent_eval",
        )
        with open_silver_gold_runner(cfg) as runner:
            populate_runner_from_spans(runner, spans)
            return runner.query(
                "SELECT * FROM `agent_eval.v_capacity_hourly_burndown_export` "
                "ORDER BY timestamp, tenant, model, finops_traffic_type, serving_traffic_type"
            ).rows

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
        """Build the stratified Tier-2 judge SQL statement over Silver `trace_payloads`."""
        return build_ai_generate_table_judge_sql(
            project_id=project_id,
            dataset_id=dataset_id,
            rubric_id=rubric_id,
            canary_sample_pct=canary_sample_pct,
            run_id=run_id,
            judge_model=judge_model,
            include_rule0_failures=include_rule0_failures,
        )


def evaluate_step1_traces(
    spans: Sequence[OTelGenAISpanRecord],
    *,
    sla_latency_ms: float = 5000.0,
) -> dict[str, dict[str, object]]:
    """Run Silver + Gold `v_trace_sql_evaluation` locally and return `{trace_id: row_dict}`."""
    if not spans:
        return {}
    max_end = max(s.end_time.astimezone(UTC) for s in spans)
    now_dt = max_end + timedelta(minutes=10)
    cfg = RunnerConfig(
        current_date_utc=now_dt.strftime("%Y-%m-%d"),
        current_timestamp_utc=now_dt.isoformat(),
        default_dataset="agent_eval",
    )
    with open_silver_gold_runner(cfg) as runner:
        populate_runner_from_spans(runner, spans, sla_latency_ms=sla_latency_ms)
        rows = runner.query("SELECT * FROM `agent_eval.v_trace_sql_evaluation`").rows
        return {str(r["trace_id"]): dict(r) for r in rows}


def run_ci_only_eval(
    spans: Sequence[OTelGenAISpanRecord],
    *,
    max_seconds: float = 5.0,
) -> dict[str, object]:
    """Execute `< 5s` `CI_ONLY` deterministic Rule-0 + Optimizer Gold export offline (`UC6`, `UC8`)."""
    t0 = time.perf_counter()
    if not spans:
        return {
            "calibration_mode": "CI_ONLY",
            "total_traces": 0,
            "candidates": [],
            "elapsed_sec": 0.0,
        }
    max_end = max(s.end_time.astimezone(UTC) for s in spans)
    now_dt = max_end + timedelta(minutes=10)
    cfg = RunnerConfig(
        current_date_utc=now_dt.strftime("%Y-%m-%d"),
        current_timestamp_utc=now_dt.isoformat(),
        default_dataset="agent_eval",
    )
    with open_silver_gold_runner(cfg) as runner:
        populate_runner_from_spans(runner, spans)
        export_rows = [
            {
                **dict(r),
                "mounted_skills": (
                    json.loads(str(r["mounted_skills"]))
                    if isinstance(r.get("mounted_skills"), str)
                    else r.get("mounted_skills")
                ),
            }
            for r in runner.query("SELECT * FROM `agent_eval.v_optimizer_gold_export`").rows
        ]
    elapsed = time.perf_counter() - t0
    if elapsed >= max_seconds:
        raise TimeoutError(f"CI_ONLY runner exceeded {max_seconds:.1f}s ceiling ({elapsed:.3f}s)")
    return {
        "calibration_mode": "CI_ONLY",
        "total_traces": len({s.trace_id for s in spans}),
        "candidates": export_rows,
        "elapsed_sec": round(elapsed, 4),
    }
