"""Comprehensive tests for pure-SQLite statistical parity and CRITICAL.md invariants."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from bq_eval.bqaa import (
    BQAAAgentEvent,
    bqaa_events_to_otel_spans,
    bqaa_events_to_trace_payloads,
    deduplicate_bqaa_events,
)
from bq_eval.calibration import (
    ConfusionMatrix,
    build_calibration_result,
    compute_gwet_ac2,
    rogan_gladen_calibrate,
)
from bq_eval.cli import main as cli_main
from bq_eval.finops import (
    ModelPricingSKU,
    allocate_focus_two_pool_chargeback,
    compute_span_cost_usd,
    diagnose_prefix_cache_dilution,
)
from bq_eval.loadgen import (
    build_golden_seed_templates,
    evaluate_time_shifted_corpus,
    generate_time_shifted_corpus,
)
from bq_eval.otel import (
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
    compute_weighted_burndown_tokens,
)
from bq_eval.rubrics import OrdinalScoreOutput
from bq_eval.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
    evaluate_traces_in_sqlite,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_span(  # noqa: PLR0913
    trace_id: str,
    span_id: str,
    *,
    app_id: str = "critical-app",
    span_kind: GenAISpanKind = GenAISpanKind.INVOKE_AGENT,
    status_code: str = "OK",
    tool_name: str | None = None,
    tool_call_status: str | None = None,
    rule0_pass: bool = True,
    input_tokens: int = 10_000,
    cached_input_tokens: int = 2_000,
    output_tokens: int = 500,
    thinking_tokens: int = 100,
    offset_s: int = 0,
) -> OTelGenAISpanRecord:
    base = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_s)
    return OTelGenAISpanRecord(
        trace_id=trace_id,
        span_id=span_id,
        start_time=base,
        end_time=base + timedelta(milliseconds=400),
        latency_ms=400.0,
        status_code=status_code,
        span_kind=span_kind,
        gcp_apphub_application_id=app_id,
        model_name="gemini-3.8-flash",
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        tool_name=tool_name,
        tool_call_status=tool_call_status,
        rule0_pass=rule0_pass,
        finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
    )


def test_pure_sqlite_rogan_gladen_and_gwet_ac2_exact_parity() -> None:
    """Pure SQLite views must match Python Rogan-Gladen + 95% CI + Gwet's AC2 within 1e-12."""
    spans = [_make_span(f"trace-cal-{i:03d}", f"span-cal-{i:03d}", offset_s=i) for i in range(25)]
    judge_outcomes = {f"trace-cal-{i:03d}": (i < 22) for i in range(25)}
    sme_pairs: list[tuple[int, int]] = [
        (4, 4),
        (4, 4),
        (4, 3),
        (3, 4),
        (3, 3),
        (3, 3),
        (4, 4),
        (3, 3),
        (4, 4),
        (3, 2),
        (2, 2),
        (2, 1),
        (1, 1),
        (1, 2),
        (2, 3),
        (4, 4),
        (3, 3),
        (4, 4),
        (1, 1),
        (2, 2),
    ]
    human_scores = [h for h, _ in sme_pairs]
    judge_scores = [j for _, j in sme_pairs]

    summaries = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes=judge_outcomes,
        sme_ordinal_pairs=sme_pairs,
    )
    summary = summaries["critical-app"]

    py_cal = build_calibration_result(
        human_ordinal_scores=human_scores,
        judge_ordinal_scores=judge_scores,
        observed_pass_rate=22 / 25,
        sample_size=25,
    )

    assert summary.calibration_status == "CALIBRATED"
    assert summary.calibrated_pass_rate == pytest.approx(
        py_cal.rogan_gladen.calibrated_pass_rate, abs=1e-12
    )
    assert summary.calibrated_ci_95_lower == pytest.approx(
        py_cal.rogan_gladen.ci_95_lower, abs=1e-12
    )
    assert summary.calibrated_ci_95_upper == pytest.approx(
        py_cal.rogan_gladen.ci_95_upper, abs=1e-12
    )
    assert summary.gwet_ac2 == pytest.approx(py_cal.gwet_ac2, abs=1e-12)
    assert summary.passes_irr_gate is py_cal.passes_irr_gate
    assert summary.gwet_ac2 == pytest.approx(
        compute_gwet_ac2(human_scores, judge_scores), abs=1e-12
    )


def test_uncalibrated_youden_j_guard_in_python_and_sqlite() -> None:
    """When Youden's J <= 1e-6, Python raises ValueError and SQLite fails closed to UNCALIBRATED."""
    uncalibrated_cm = ConfusionMatrix(tp=5, fn=5, tn=5, fp=5)
    with pytest.raises(ValueError, match="uncalibrated"):
        rogan_gladen_calibrate(observed_pass_rate=0.80, sample_size=20, confusion=uncalibrated_cm)

    spans = [_make_span(f"tr-unc-{i}", f"sp-unc-{i}", offset_s=i) for i in range(5)]
    outcomes = {f"tr-unc-{i}": True for i in range(5)}
    summary = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes=outcomes,
        sme_confusion=uncalibrated_cm,
    )["critical-app"]
    assert summary.calibration_status == "UNCALIBRATED"
    assert summary.calibrated_pass_rate is None
    assert summary.calibrated_ci_95_lower is None
    assert summary.calibrated_ci_95_upper is None
    assert summary.cost_per_verified_outcome_usd is None


def test_sqlite_rule0_tool_loop_and_template16_real_9_tool_spans() -> None:
    """Template 16 emits 9 OK execute_tool spans and fails Step 1 strictly via tool_call_count <= 8."""
    templates = build_golden_seed_templates()
    t16 = next(t for t in templates if t.base_trace_id == "seed-audit-004")
    tool_spans = [s for s in t16.spans if s.span_kind == GenAISpanKind.EXECUTE_TOOL]
    assert len(tool_spans) == 9
    assert all(s.rule0_pass and s.status_code == "OK" for s in t16.spans)
    assert all(s.tool_call_status == "SUCCESS" for s in tool_spans)

    now = datetime(2026, 11, 14, 15, 0, 0, tzinfo=UTC)
    spans, payloads, _ = generate_time_shifted_corpus(
        reference_time=now,
        total_replicas=2,
        last_hour_replicas=1,
    )
    assert len(spans) == 33 * 2
    t16_spans = [s for s in spans if s.trace_id.startswith("seed-audit-004-r001")]
    summary = evaluate_traces_in_sqlite(spans=t16_spans)["unregistered_app"]
    assert summary.rule0_pass_rate == 0.0
    assert summary.tool_success_rate == 1.0

    report = evaluate_time_shifted_corpus(
        spans=spans, payloads=payloads, reference_time=now, total_replicas=2, last_hour_replicas=1
    )
    audit_row = next(r for r in report.step1_rows if r.apphub_application_id == "unregistered_app")
    assert audit_row.step1_pass_rate == pytest.approx(0.75)


def test_ordinal_score_output_contract() -> None:
    """CoT-first OrdinalScoreOutput validates fields, min_length, and forbids extra fields."""
    valid = OrdinalScoreOutput(
        reasoning_trace="Every figure matches [DOC-01] with zero extrapolation.",
        ordinal_score=4,
        binary_pass=True,
    )
    assert next(iter(OrdinalScoreOutput.model_fields.keys())) == "reasoning_trace"
    assert valid.ordinal_score == 4

    with pytest.raises(ValidationError):
        OrdinalScoreOutput(reasoning_trace="too short", ordinal_score=4, binary_pass=True)
    with pytest.raises(ValidationError):
        OrdinalScoreOutput(
            reasoning_trace="Long enough rationale for validation.",
            ordinal_score=5,  # type: ignore[arg-type]
            binary_pass=True,
        )


def test_engine_b_and_cli_managed_metrics_and_judge_model_override(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify --managed-metrics, --run-id, and --judge-model flags on `bq-eval judge-sql`."""
    sql_b = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        dataset_id="agent_eval",
        managed_metrics=("hallucination_v1", "safety_v1"),
        canary_sample_pct=15,
        run_id="run-m5-01",
    )
    assert "'hallucination_v1'" in sql_b
    assert "'safety_v1'" in sql_b
    assert "'tool_use_quality_v1'" not in sql_b
    assert "WHERE run_id = 'run-m5-01'" in sql_b

    rc_b = cli_main(
        [
            "judge-sql",
            "--engine",
            "evaluate-agent",
            "--project-id",
            "demo-proj",
            "--managed-metrics",
            "hallucination_v1,safety_v1",
            "--run-id",
            "run-m5-01",
        ]
    )
    assert rc_b == 0
    out_b = capsys.readouterr().out
    assert "'hallucination_v1'" in out_b
    assert "run-m5-01" in out_b

    rc_a = cli_main(
        [
            "judge-sql",
            "--project-id",
            "demo-proj",
            "--rubric-id",
            "groundedness_4pt_v1",
            "--judge-model",
            "gemini-4-argon",
        ]
    )
    assert rc_a == 0
    out_a = capsys.readouterr().out
    assert "gemini_4_argon_judge" in out_a
    assert "'gemini-4-argon' AS judge_model" in out_a

    with pytest.raises(ValueError, match="Unsupported judge_model"):
        build_ai_generate_table_judge_sql(project_id="demo-proj", judge_model="unknown-model")


def test_bqaa_edge_cases_tool_error_invalid_traffic_and_outcome_closed() -> None:
    """Cover TOOL_ERROR status, invalid traffic fallback, non-string JSON content, and OUTCOME_CLOSED."""
    ts = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
    events = [
        BQAAAgentEvent(
            event_id="evt-edge-01",
            timestamp=ts,
            event_type="USER_MESSAGE_RECEIVED",
            agent="edge-agent",
            session_id="sess-1",
            invocation_id="inv-edge-01",
            trace_id="trace-edge-01",
            span_id="span-edge-01",
            content={"text": "Check policy", "meta": [1, 2]},
            attributes={
                "custom_metadata": {
                    "gcp.apphub.application.id": "edge-app",
                    "finops.traffic_type": "not_a_real_type",
                },
            },
        ),
        BQAAAgentEvent(
            event_id="evt-edge-02",
            timestamp=ts + timedelta(milliseconds=100),
            event_type="TOOL_ERROR",
            agent="edge-agent",
            session_id="sess-1",
            invocation_id="inv-edge-01",
            trace_id="trace-edge-01",
            span_id="span-edge-02",
            status="OK",
            error_message="downstream 503",
            content={"tool_name": "carrier_api", "code": 503},
            attributes={"custom_metadata": {"gcp.apphub.application.id": "edge-app"}},
        ),
        BQAAAgentEvent(
            event_id="evt-edge-03",
            timestamp=ts + timedelta(milliseconds=200),
            event_type="OUTCOME_CLOSED",
            agent="edge-agent",
            session_id="sess-1",
            invocation_id="inv-edge-01",
            trace_id="trace-edge-01",
            span_id="span-edge-03",
            content={"outcome_status": "VERIFIED"},
            attributes={
                "custom_metadata": {
                    "gcp.apphub.application.id": "edge-app",
                    "outcome.id": "oc-edge-1",
                }
            },
        ),
        BQAAAgentEvent(
            event_id="evt-edge-04",
            timestamp=ts + timedelta(milliseconds=250),
            event_type="AGENT_RESPONSE",
            agent="edge-agent",
            session_id="sess-1",
            invocation_id="inv-edge-01",
            trace_id="trace-edge-01",
            span_id="span-edge-04",
            content={"answer": 42},
            attributes={"custom_metadata": {"gcp.apphub.application.id": "edge-app"}},
        ),
        BQAAAgentEvent(
            event_id="evt-edge-05",
            timestamp=ts + timedelta(milliseconds=300),
            event_type="AGENT_COMPLETED",
            agent="edge-agent",
            session_id="sess-1",
            invocation_id="inv-edge-01",
            trace_id="trace-edge-01",
            span_id="span-edge-05",
            content={"done": True},
            attributes={
                "custom_metadata": {
                    "gcp.apphub.application.id": "edge-app",
                    "finops.traffic_type": "not_a_real_type",
                }
            },
        ),
    ]
    deduped = deduplicate_bqaa_events(events)
    spans = bqaa_events_to_otel_spans(deduped)
    payloads = bqaa_events_to_trace_payloads(deduped)

    assert len(spans) == 3
    assert spans[0].status_code == "ERROR"
    assert spans[0].tool_call_status == "VALIDATION_ERROR"
    assert spans[0].rule0_pass is False
    assert spans[1].span_kind == GenAISpanKind.INVOKE_AGENT
    assert spans[1].outcome_id == "oc-edge-1"
    assert spans[2].finops_traffic_type == FinOpsTrafficType.PRODUCTION_INFERENCE
    assert len(payloads) == 1
    assert "Check policy" in payloads[0].user_prompt
    assert "42" in payloads[0].final_response


def test_finops_cliff_boundary_sku_cost_overflow_and_deprecation_alias() -> None:
    """Test 200,000 vs 200,001 cliff, compute_span_cost_usd, PAYG overflow, >200K dilution, deprecation."""
    # Exact 200,000 stays standard; 200,001 crosses into >200K long-context cliff.
    bd_200k = compute_weighted_burndown_tokens(
        input_tokens=200_000, cached_input_tokens=0, output_tokens=0, thinking_tokens=0
    )
    bd_200k_plus1 = compute_weighted_burndown_tokens(
        input_tokens=200_001, cached_input_tokens=0, output_tokens=0, thinking_tokens=0
    )
    assert bd_200k == pytest.approx(200_000.0)
    assert bd_200k_plus1 == pytest.approx(400_002.0)

    sku = ModelPricingSKU(
        model_name="gemini-3.8-flash",
        effective_start=datetime(2026, 1, 1, tzinfo=UTC),
        effective_end=datetime(2099, 12, 31, tzinfo=UTC),
        input_usd_per_1m=0.15,
        cached_input_usd_per_1m=0.015,
        output_usd_per_1m=0.60,
        long_context_input_usd_per_1m=0.30,
        long_context_cached_usd_per_1m=0.030,
        long_context_output_usd_per_1m=2.25,
    )
    std_span = _make_span(
        "trace-std-001",
        "span-std-001",
        input_tokens=200_000,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
    )
    cliff_span = _make_span(
        "trace-clif-001",
        "span-clif-001",
        input_tokens=200_001,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
    )
    assert compute_span_cost_usd(std_span, sku) == pytest.approx(0.03)
    assert compute_span_cost_usd(cliff_span, sku) == pytest.approx((200_001 / 1e6) * 0.30)

    # >200K dilution uses 1.8x burndown delta and 2x default long-context USD delta.
    long_span = _make_span(
        "trace-dil-001",
        "span-dil-001",
        input_tokens=1_000_000,
        cached_input_tokens=200_000,
        output_tokens=0,
        thinking_tokens=0,
    )
    dil = diagnose_prefix_cache_dilution(
        [long_span], distinct_prompt_variants=5, target_cache_hit_ratio=0.75
    )
    assert dil.recoverable_weighted_burndown_tokens == pytest.approx(550_000 * 1.8)
    assert dil.recoverable_monthly_usd == pytest.approx((550_000 / 1e6) * (0.30 - 0.03))

    # PAYG overflow when total demand exceeds reserved PT capacity.
    overflow_rows = allocate_focus_two_pool_chargeback(
        app_burndown_tokens={"app-a": 100_000_000.0, "app-b": 50_000_000.0},
        app_cost_centers={"app-a": "CC-A", "app-b": "CC-B"},
        reserved_pt_capacity_tokens=100_000_000.0,
        reserved_pt_fixed_cost_usd=100.0,
        payg_overflow_usd_per_1m_weighted=0.20,
    )
    assert len(overflow_rows) == 2
    assert sum(r.payg_overflow_cost_usd for r in overflow_rows) == pytest.approx(10.0)
    assert sum(r.total_allocated_cost_usd for r in overflow_rows) == pytest.approx(110.0)

    # Backward-compatible deprecated kwarg emits DeprecationWarning.
    with pytest.warns(DeprecationWarning, match="reserved_pt_fixed_cost_usd"):
        legacy_rows = allocate_focus_two_pool_chargeback(
            app_burndown_tokens={"app-a": 50_000_000.0},
            app_cost_centers={"app-a": "CC-A"},
            reserved_pt_capacity_tokens=100_000_000.0,
            reserved_pt_Fixed_cost_usd=100.0,
        )
    assert sum(r.total_allocated_cost_usd for r in legacy_rows) == pytest.approx(100.0)


def test_step3_paired_run_comparison_non_duplication() -> None:
    """v_step3_paired_run_comparison must emit each (baseline, candidate) pair once (strict <)."""
    step3_sql = (REPO_ROOT / "sql" / "step3_saved_results.sql").read_text(encoding="utf-8")
    assert "base.eval_run_id < cand.eval_run_id" in step3_sql
    assert "base.eval_run_id != cand.eval_run_id" not in step3_sql

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE eval_results (apphub_application_id TEXT, rubric_id TEXT, eval_run_id TEXT, trace_id TEXT, ordinal_score INT, binary_pass INT, reasoning_trace TEXT)"
    )
    conn.executemany(
        "INSERT INTO eval_results VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("app-1", "groundedness_4pt_v1", "run-a", "tr-1", 4, 1, "good"),
            ("app-1", "groundedness_4pt_v1", "run-b", "tr-1", 2, 0, "regressed"),
        ],
    )
    rows = conn.execute(
        """
        SELECT base.eval_run_id, cand.eval_run_id, (base.binary_pass AND NOT cand.binary_pass)
        FROM eval_results AS base
        INNER JOIN eval_results AS cand
            ON base.trace_id = cand.trace_id
            AND base.rubric_id = cand.rubric_id
            AND base.eval_run_id < cand.eval_run_id
        """
    ).fetchall()
    conn.close()
    assert rows == [("run-a", "run-b", 1)]
