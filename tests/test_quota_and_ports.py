"""`EvalBackend` / `WarehouseEvalBackend` Ports, spend mapping, and `SQLTraceEvalSummary` status validators (`A-18`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from bq_eval.core.contract.otel import (
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
)
from bq_eval.core.contract.results import ConfusionMatrix, SQLTraceEvalSummary
from bq_eval.core.engine.cost import compute_span_burndown_tokens, is_span_long_context_cliff
from bq_eval.core.engine.ports import (
    BigQueryEvalBackend,
    EvalBackend,
    QueryRunner,
    WarehouseEvalBackend,
)
from bq_eval.demo.pipeline import run_showcase_pipeline
from bq_eval.finops.engine.capacity_export import hourly_burndown_rows
from bq_eval.runner.engine.eval_adapter import OfflineEvalBackend
from bq_eval.runner.engine.loader import OfflineSqlRunner
from tests.conftest import eval_hourly_burndown, open_runner_for_spans, seed_sme_confusion


def _runner_query_port(runner: OfflineSqlRunner) -> QueryRunner:
    def _run(sql: str) -> Sequence[Mapping[str, object]]:
        rewritten = sql.replace("`demo-proj.agent_eval.", "`agent_eval.")
        return runner.query(rewritten).rows

    return _run


@pytest.mark.port
def test_eval_backend_port_local_offline_vs_cloud_bigquery_parity() -> None:
    """`OfflineEvalBackend` and `BigQueryEvalBackend` match to `< 1e-12` across summaries and hourly exports."""
    local_backend = OfflineEvalBackend()
    assert isinstance(local_backend, EvalBackend)

    t0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    outcomes: dict[str, bool] = {}

    for i in range(10):
        tid = f"trace-port-{i:03d}"
        start = t0 + timedelta(minutes=i * 15)
        end = start + timedelta(milliseconds=900 + i * 10)
        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-port-{i:03d}",
                start_time=start,
                end_time=end,
                latency_ms=900.0 + i * 10,
                status_code="OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                service_name="svc-port",
                gcp_apphub_application_id="port-app",
                agent_name="port-agent",
                model_name="gemini-3.8-flash",
                input_tokens=10_000,
                cached_input_tokens=2_000,
                output_tokens=400,
                thinking_tokens=100,
                rule0_pass=True,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            )
        )
        outcomes[tid] = i < 9

    unjudged_start = t0 + timedelta(hours=1)
    spans.append(
        OTelGenAISpanRecord(
            trace_id="trace-unjudged-001",
            span_id="sp-unjudged-001",
            start_time=unjudged_start,
            end_time=unjudged_start + timedelta(milliseconds=400),
            latency_ms=400.0,
            status_code="OK",
            span_kind=GenAISpanKind.CHAT,
            service_name="svc-cat",
            gcp_apphub_application_id="unjudged-app",
            agent_name="cat-agent",
            model_name="gemini-3.5-flash-lite",
            input_tokens=5_000,
            cached_input_tokens=2_500,
            output_tokens=200,
            thinking_tokens=0,
            rule0_pass=True,
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
        )
    )
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    judge_cost = (1_000 / 1e6) * 0.15 + (100 / 1e6) * 0.60

    local_res = local_backend.evaluate_traces(
        spans=spans,
        judge_outcomes=outcomes,
        sme_confusion={"port-app": cm},
        judge_cost_per_call_usd=judge_cost,
    )

    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": f"ev-port-{i:03d}",
                    "eval_run_id": "run-port",
                    "trace_id": f"trace-port-{i:03d}",
                    "apphub_application_id": "port-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4 if (i < 9) else 2,
                    "binary_pass": i < 9,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 1_000,
                    "judge_output_tokens": 100,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T15:00:00+00:00",
                }
                for i in range(10)
            ],
            append=True,
        )
        seed_sme_confusion(runner, "port-app", cm)
        cloud_backend = BigQueryEvalBackend(
            project_id="demo-proj",
            dataset_id="agent_eval",
            query_runner=_runner_query_port(runner),
        )
        assert isinstance(cloud_backend, WarehouseEvalBackend)
        assert not isinstance(cloud_backend, EvalBackend)

        cloud_res = cloud_backend.read_summaries(rubric_id="groundedness_4pt_v1")
        local_hourly = hourly_burndown_rows(local_backend, spans)
        cloud_hourly = hourly_burndown_rows(cloud_backend, spans)

    assert sorted(local_res) == sorted(cloud_res) == ["port-app", "unjudged-app"]
    for app_id in ("port-app", "unjudged-app"):
        loc, cld = local_res[app_id], cloud_res[app_id]
        assert loc.calibration_status == cld.calibration_status
        assert loc.total_traces == cld.total_traces
        assert loc.rule0_pass_rate == pytest.approx(cld.rule0_pass_rate, abs=1e-12)
        assert loc.total_weighted_burndown_tokens == pytest.approx(
            cld.total_weighted_burndown_tokens, abs=1e-12
        )
        assert loc.total_spend_usd == pytest.approx(cld.total_spend_usd, abs=1e-12)
        if loc.calibrated_pass_rate is None:
            assert cld.calibrated_pass_rate is None
            assert cld.cost_per_verified_outcome_usd is None
        else:
            assert cld.calibrated_pass_rate == pytest.approx(loc.calibrated_pass_rate, abs=1e-12)
            assert cld.cost_per_verified_outcome_usd == pytest.approx(
                loc.cost_per_verified_outcome_usd, abs=1e-12
            )

    assert len(local_hourly) == len(cloud_hourly) == 4
    for loc_h, cld_h in zip(local_hourly, cloud_hourly, strict=True):
        assert loc_h.timestamp == cld_h.timestamp
        assert loc_h.tenant == cld_h.tenant
        assert loc_h.weighted_burndown_tokens == pytest.approx(
            cld_h.weighted_burndown_tokens, abs=1e-12
        )
        assert loc_h.total_cost_usd == pytest.approx(cld_h.total_cost_usd, abs=1e-12)

    sql = cloud_backend.build_judge_sql(rubric_id="groundedness_4pt_v1", canary_sample_pct=5)
    assert "`demo-proj.agent_eval.v_trace_sql_evaluation`" in sql
    with pytest.raises(ValueError, match="Invalid project_id"):
        BigQueryEvalBackend(project_id="bad; DROP TABLE")


def test_pipeline_accepts_injected_eval_backend() -> None:
    """`run_showcase_pipeline` accepts an injected `EvalBackend` implementation."""
    artifacts = run_showcase_pipeline(corpus="retail-2app", backend=OfflineEvalBackend())
    assert "order-support-agent" in artifacts.summaries


def test_bigquery_adapter_maps_all_spend_buckets() -> None:
    """`BigQueryEvalBackend` maps all spend buckets, rejects NULL rates, and maps all 27 burndown fields (`A-7`, `A-8`)."""
    summary_row: dict[str, object] = {
        "apphub_application_id": "spend-app",
        "rubric_id": "groundedness_4pt_v1",
        "total_traces": 10,
        "production_traces": 8,
        "rule0_pass_rate": 0.9,
        "tool_success_rate": 1.0,
        "p95_latency_ms": 400.0,
        "avg_cache_hit_ratio": 0.2,
        "total_weighted_burndown_tokens": 50_000.0,
        "judged_samples": 0,
        "calibration_status": "UNJUDGED",
        "production_inference_usd": 4.0,
        "conflict_escalation_usd": 2.0,
        "agent_gym_eval_usd": 1.0,
        "autorater_judge_usd": 3.0,
        "optimize_usd": 5.0,
        "bq_query_cost_usd": 2.0,
        "unclassified_usd": 0.5,
        "total_spend_usd": 17.5,
    }
    hourly_row: dict[str, object] = {
        "usage_hour": "2026-11-22T12:00:00Z",
        "timestamp": "2026-11-22T12:00:00Z",
        "apphub_application_id": "spend-app",
        "tenant": "spend-app",
        "model_name": "claude-sonnet-4-6",
        "model": "claude-sonnet-4-6",
        "finops_traffic_type": "production_inference",
        "serving_traffic_type": "PRIORITY",
        "distinct_traces": 4,
        "span_count": 6,
        "ok_span_count": 5,
        "error_span_count": 1,
        "http_429_count": 2,
        "spillover_span_count": 3,
        "input_tokens": 10_000,
        "cached_input_tokens": 1_000,
        "cached_tokens": 1_000,
        "cache_write_5m_tokens": 2_000,
        "cache_write_1h_tokens": 500,
        "uncached_input_tokens": 6_500,
        "output_tokens": 400,
        "thinking_tokens": 100,
        "thoughts_tokens": 100,
        "long_context_cliff_spans": 0,
        "weighted_burndown_tokens": 12_500.0,
        "precomputed_weighted_burndown_tokens": 12_500.0,
        "total_cost_usd": 0.42,
    }

    backend = BigQueryEvalBackend(
        project_id="demo-proj",
        query_runner=lambda sql: [hourly_row] if "burndown" in sql else [summary_row],
    )
    summary = backend.read_summaries()["spend-app"]
    bucket_sum = (
        summary.production_inference_usd
        + summary.conflict_escalation_usd
        + summary.agent_gym_eval_usd
        + summary.autorater_judge_usd
        + summary.optimize_usd
        + summary.bq_query_cost_usd
        + summary.unclassified_usd
    )
    assert summary.optimize_usd == pytest.approx(5.0)
    assert summary.bq_query_cost_usd == pytest.approx(2.0)
    assert bucket_sum == pytest.approx(summary.total_spend_usd, abs=1e-12)

    bad_row = {**summary_row, "rule0_pass_rate": None}
    bad_backend = BigQueryEvalBackend(project_id="demo-proj", query_runner=lambda _: [bad_row])
    with pytest.raises(ValueError, match="rule0_pass_rate"):
        bad_backend.read_summaries()

    class _CustomWarehouse:
        project_id = "demo-proj"
        dataset_id = "agent_eval"

        def run_query(self, sql: str) -> Sequence[Mapping[str, object]]:
            assert "v_capacity_hourly_burndown_export" in sql
            return [hourly_row]

        def read_summaries(self, *, rubric_id: str = "groundedness_4pt_v1") -> dict[str, object]:
            _ = rubric_id
            return {}

        def build_judge_sql(self, **kwargs: object) -> str:
            _ = kwargs
            return ""

    rows = hourly_burndown_rows(_CustomWarehouse())  # type: ignore[arg-type]
    assert len(rows) == 1
    assert rows[0].serving_traffic_type == "PRIORITY"
    assert rows[0].http_429_count == 2
    assert rows[0].cache_write_5m_tokens == 2_000
    assert rows[0].cache_write_1h_tokens == 500

    t0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    claude_span = OTelGenAISpanRecord(
        trace_id="trace-claude-250k",
        span_id="sp-claude-250k",
        start_time=t0,
        end_time=t0 + timedelta(seconds=1),
        latency_ms=1000.0,
        model_name="claude-sonnet-4-6",
        gcp_apphub_application_id="claude-app",
        input_tokens=250_000,
        output_tokens=1_000,
    )
    assert is_span_long_context_cliff(claude_span) is False
    assert compute_span_burndown_tokens(claude_span) == pytest.approx(255_000.0, abs=1e-12)
    sq_hourly = eval_hourly_burndown([claude_span])
    assert sq_hourly[0].long_context_cliff_spans == 0
    assert sq_hourly[0].weighted_burndown_tokens == pytest.approx(255_000.0, abs=1e-12)


def test_summary_contract_rejects_calibrated_without_rate() -> None:
    """`A-18`: `SQLTraceEvalSummary` enforces status/rate/`NULL` invariants across all 5 statuses."""
    base_kwargs: dict[str, object] = {
        "apphub_application_id": "val-app",
        "rubric_id": "groundedness_4pt_v1",
        "total_traces": 20,
        "production_traces": 20,
        "rule0_pass_rate": 1.0,
        "tool_success_rate": 1.0,
        "p95_latency_ms": 300.0,
        "avg_cache_hit_ratio": 0.5,
        "total_weighted_burndown_tokens": 10_000.0,
        "production_inference_usd": 1.0,
        "conflict_escalation_usd": 0.0,
        "agent_gym_eval_usd": 0.0,
        "autorater_judge_usd": 0.1,
        "total_spend_usd": 1.1,
    }

    with pytest.raises(ValidationError, match="non-null"):
        SQLTraceEvalSummary(
            **base_kwargs,  # type: ignore[arg-type]
            judged_samples=15,
            calibration_status="CALIBRATED",
            raw_judge_pass_rate=0.9,
            calibrated_pass_rate=None,
            calibrated_ci_95_lower=0.7,
            calibrated_ci_95_upper=1.0,
            passes_irr_gate=True,
            cost_per_verified_outcome_usd=0.05,
        )

    with pytest.raises(ValidationError, match="passes_irr_gate=False"):
        SQLTraceEvalSummary(
            **base_kwargs,  # type: ignore[arg-type]
            judged_samples=15,
            calibration_status="CALIBRATED",
            raw_judge_pass_rate=0.9,
            calibrated_pass_rate=0.88,
            calibrated_ci_95_lower=0.7,
            calibrated_ci_95_upper=0.98,
            passes_irr_gate=False,
            cost_per_verified_outcome_usd=0.05,
        )

    with pytest.raises(ValidationError, match="judged_samples >= 10"):
        SQLTraceEvalSummary(
            **base_kwargs,  # type: ignore[arg-type]
            judged_samples=9,
            calibration_status="CALIBRATED",
            raw_judge_pass_rate=0.9,
            calibrated_pass_rate=0.88,
            calibrated_ci_95_lower=0.7,
            calibrated_ci_95_upper=0.98,
            passes_irr_gate=True,
            cost_per_verified_outcome_usd=0.05,
        )

    for non_cal_status in ("UNCALIBRATED", "STALE_CALIBRATION", "MISSING_SESSION_CONTEXT"):
        with pytest.raises(ValidationError, match="to be None"):
            SQLTraceEvalSummary(
                **base_kwargs,  # type: ignore[arg-type]
                judged_samples=15,
                calibration_status=non_cal_status,
                raw_judge_pass_rate=0.9,
                calibrated_pass_rate=0.88,
                calibrated_ci_95_lower=None,
                calibrated_ci_95_upper=None,
                cost_per_verified_outcome_usd=None,
            )

    with pytest.raises(ValidationError, match="UNJUDGED"):
        SQLTraceEvalSummary(
            **base_kwargs,  # type: ignore[arg-type]
            judged_samples=5,
            calibration_status="UNJUDGED",
            raw_judge_pass_rate=None,
            calibrated_pass_rate=None,
            calibrated_ci_95_lower=None,
            calibrated_ci_95_upper=None,
            cost_per_verified_outcome_usd=None,
        )
