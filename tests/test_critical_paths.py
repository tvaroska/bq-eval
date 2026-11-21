"""Comprehensive tests for pure-SQLite statistical parity and CRITICAL.md invariants."""

from __future__ import annotations

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
    rogan_gladen_wilson_ci,
    stratified_rogan_gladen_calibrate,
)
from bq_eval.cli import main as cli_main
from bq_eval.finops import (
    ModelOption,
    ModelPricingSKU,
    allocate_focus_two_pool_chargeback,
    compute_pareto_frontier,
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
    assert spans[2].finops_traffic_type == FinOpsTrafficType.UNCLASSIFIED
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


def test_step3_paired_run_comparison_temporal_order_and_dedup() -> None:
    """Transpiled `v_step3_paired_run_comparison` orders `run_9` -> `run_10` temporally (`F2`, `F3`, `M6-3`)."""
    from tests.test_sql_contracts import (
        _extract_view_sql_text,
        _new_sqlite_bq_conn,
        _transpile_bq_view_to_sqlite,
    )

    step3_path = REPO_ROOT / "sql" / "step3_saved_results.sql"
    view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(step3_path, "agent_eval.v_step3_paired_run_comparison")
    )
    conn = _new_sqlite_bq_conn()
    conn.execute(
        "CREATE TABLE eval_results (apphub_application_id TEXT, rubric_id TEXT, eval_run_id TEXT, "
        "trace_id TEXT, ordinal_score INT, binary_pass INT, reasoning_trace TEXT, evaluated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO eval_results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # run_9 ran earlier and passed; duplicate older row in run_9 must be deduplicated
            (
                "app-1",
                "groundedness_4pt_v1",
                "run_9",
                "tr-1",
                2,
                0,
                "stale",
                "2026-11-10T09:00:00+00:00",
            ),
            (
                "app-1",
                "groundedness_4pt_v1",
                "run_9",
                "tr-1",
                4,
                1,
                "good",
                "2026-11-10T10:00:00+00:00",
            ),
            # run_10 ran later and failed (lexically 'run_10' < 'run_9', temporally run_9 < run_10)
            (
                "app-1",
                "groundedness_4pt_v1",
                "run_10",
                "tr-1",
                2,
                0,
                "regressed",
                "2026-11-11T10:00:00+00:00",
            ),
        ],
    )
    conn.execute(f"CREATE VIEW v_step3_paired_run_comparison AS {view_sql}")
    rows = conn.execute(
        "SELECT baseline_run_id, candidate_run_id, is_negative_flip, is_positive_flip "
        "FROM v_step3_paired_run_comparison"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("run_9", "run_10", 1, 0)


def _build_stratppi_20_to_1_fixture() -> tuple[
    list[OTelGenAISpanRecord],
    dict[str, bool],
    dict[str, str],
    list[tuple[object, ...]],
    list[tuple[object, ...]],
]:
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    judge_outcomes: dict[str, bool] = {}
    sampling_strata: dict[str, str] = {}
    sql_span_rows: list[tuple[object, ...]] = []
    sql_eval_rows: list[tuple[object, ...]] = []

    for i in range(1000):
        is_tail = i < 50
        tid = f"tr-strat-{i:04d}"
        latency_ms = 6_000.0 if is_tail else 500.0
        t0 = base + timedelta(seconds=i)
        t1 = t0 + timedelta(milliseconds=latency_ms)
        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-strat-{i:04d}",
                start_time=t0,
                end_time=t1,
                latency_ms=latency_ms,
                status_code="OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                gcp_apphub_application_id="strat-app",
                model_name="gemini-3.8-flash",
                input_tokens=1_000,
                cached_input_tokens=0,
                output_tokens=100,
                rule0_pass=True,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            )
        )
        sql_span_rows.append(
            (
                tid,
                f"sp-strat-{i:04d}",
                None,
                "invoke_agent",
                "strat-app",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "invoke_agent",
                "gemini-3.8-flash",
                None,
                None,
                "OK",
                1,
                "production_inference",
                None,
                "strat-app",
                None,
                None,
                None,
                None,
                t0.isoformat(),
                t1.isoformat(),
                t1.isoformat(),
                latency_ms,
                1_000,
                0,
                100,
                0,
                "{}",
            )
        )
        if is_tail or i < 100:
            stratum = "ERROR_OR_SLA_TAIL" if is_tail else "CANARY_5PCT"
            passed = (i < 10) if is_tail else ((i - 50) < 48)
            judge_outcomes[tid] = passed
            sampling_strata[tid] = stratum
            sql_eval_rows.append(
                (
                    f"ev-{i}",
                    "run-strat",
                    tid,
                    None,
                    "strat-app",
                    "groundedness_4pt_v1",
                    "gemini-3.8-flash",
                    stratum,
                    1,
                    "rationale",
                    4 if passed else 2,
                    1 if passed else 0,
                    1000,
                    100,
                    0,
                    t1.isoformat(),
                )
            )
    return spans, judge_outcomes, sampling_strata, sql_span_rows, sql_eval_rows


def test_stratppi_20_to_1_tail_sampling_sql_sqlite_python_parity() -> None:
    """`20:1` tail-skewed fixture proves `< 1e-12` SQL/SQLite/Python parity and unbiased `calibrated_pass_rate` (`M6-1`)."""
    from tests.test_sql_contracts import (
        _create_enterprise_sqlite_fixture,
        _install_enterprise_views,
        _new_sqlite_bq_conn,
    )

    spans, judge_outcomes, sampling_strata, sql_span_rows, sql_eval_rows = (
        _build_stratppi_20_to_1_fixture()
    )
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    conn.executemany(
        "INSERT INTO otel_spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        sql_span_rows,
    )
    conn.executemany(
        "INSERT INTO eval_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        sql_eval_rows,
    )
    sme_rows = (
        [("strat-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 87
        + [("strat-app", "groundedness_4pt_v1", 1, 4, 1, 2, 0)] * 2
        + [("strat-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)] * 7
        + [("strat-app", "groundedness_4pt_v1", 1, 1, 0, 4, 1)] * 4
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)

    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    py_strat = stratified_rogan_gladen_calibrate(
        strata=[(50, 50, 10), (950, 50, 48)],
        confusion=cm,
    )
    sqlite_summary = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes=judge_outcomes,
        sme_confusion=cm,
        sampling_strata=sampling_strata,
        sla_latency_ms=5000.0,
    )["strat-app"]
    bq_row = conn.execute(
        "SELECT * FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'strat-app'"
    ).fetchone()
    conn.close()

    assert py_strat.raw_pass_rate == pytest.approx(0.922, abs=1e-12)
    assert sqlite_summary.raw_judge_pass_rate == pytest.approx(py_strat.raw_pass_rate, abs=1e-12)
    assert float(bq_row["raw_judge_pass_rate"]) == pytest.approx(py_strat.raw_pass_rate, abs=1e-12)
    assert sqlite_summary.calibrated_pass_rate == pytest.approx(
        py_strat.calibrated_pass_rate, abs=1e-12
    )
    assert float(bq_row["calibrated_pass_rate"]) == pytest.approx(
        py_strat.calibrated_pass_rate, abs=1e-12
    )
    assert sqlite_summary.calibrated_ci_95_lower == pytest.approx(py_strat.ci_95_lower, abs=1e-12)
    assert float(bq_row["calibrated_ci_95_lower"]) == pytest.approx(py_strat.ci_95_lower, abs=1e-12)
    assert sqlite_summary.calibrated_ci_95_upper == pytest.approx(py_strat.ci_95_upper, abs=1e-12)
    assert float(bq_row["calibrated_ci_95_upper"]) == pytest.approx(py_strat.ci_95_upper, abs=1e-12)

    perfect_cm = ConfusionMatrix(tp=50, fn=0, tn=20, fp=0)
    delta_boundary = rogan_gladen_calibrate(1.0, 20, perfect_cm, ci_method="delta")
    assert delta_boundary.ci_95_lower == pytest.approx(1.0)
    wilson_lo, wilson_hi = rogan_gladen_wilson_ci(1.0, 20, perfect_cm)
    assert 0.0 < wilson_lo < wilson_hi <= 1.0
    wilson_res = rogan_gladen_calibrate(1.0, 20, perfect_cm, ci_method="wilson_plugin")
    assert wilson_res.ci_95_lower == pytest.approx(wilson_lo, abs=1e-12)
    assert wilson_res.ci_95_upper == pytest.approx(wilson_hi, abs=1e-12)


def test_sqlite_multi_app_w3c_trace_per_app_sme_and_outcome_cpvo() -> None:
    """Cross-app W3C trace stays 1 trace under root `outcome_owner`, SME keys isolate per app, and outcome CPVO computes (`M6-2`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        # Cross-app trace: root belongs to app-alpha, child belongs to app-downstream
        OTelGenAISpanRecord(
            trace_id="trace-cross-001",
            span_id="span-root-001",
            parent_span_id=None,
            start_time=base,
            end_time=base + timedelta(milliseconds=600),
            latency_ms=600.0,
            gcp_apphub_application_id="app-alpha",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            model_name="gemini-3.8-flash",
            input_tokens=100_000,
            output_tokens=10_000,
        ),
        OTelGenAISpanRecord(
            trace_id="trace-cross-001",
            span_id="span-child-001",
            parent_span_id="span-root-001",
            start_time=base + timedelta(milliseconds=100),
            end_time=base + timedelta(milliseconds=400),
            latency_ms=300.0,
            gcp_apphub_application_id="app-downstream",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            model_name="gemini-3.8-flash",
            input_tokens=100_000,
            output_tokens=10_000,
        ),
        OTelGenAISpanRecord(
            trace_id="trace-cross-001",
            span_id="span-close-001",
            parent_span_id="span-root-001",
            span_name="outcome.closed",
            start_time=base + timedelta(milliseconds=600),
            end_time=base + timedelta(milliseconds=600),
            latency_ms=0.0,
            gcp_apphub_application_id="app-alpha",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            model_name="none",
            attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
        ),
        # Second app with judged trace but no SME calibration entry
        _make_span("trace-beta-001", "span-beta-001", app_id="app-beta", offset_s=5),
    ]
    summaries = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes={"trace-cross-001": True, "trace-beta-001": True},
        sme_confusion={"app-alpha": ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)},
        sme_ordinal_pairs={"app-alpha": [(4, 4), (4, 3), (3, 3), (2, 2), (1, 1)]},
        judge_cost_per_call_usd=0.0025,
    )
    assert set(summaries) == {"app-alpha", "app-beta"}
    alpha = summaries["app-alpha"]
    beta = summaries["app-beta"]
    assert alpha.total_traces == 1
    assert alpha.calibration_status == "CALIBRATED"
    assert alpha.gwet_ac2 is not None
    assert alpha.passes_irr_gate is True
    assert alpha.outcome_total_count == 1
    assert alpha.outcome_verified_count == 1
    expected_span_cost = 2 * ((100_000 / 1e6) * 0.15 + (10_000 / 1e6) * 0.60)
    assert alpha.outcome_cost_per_verified_usd == pytest.approx(
        expected_span_cost + 0.0025, abs=1e-12
    )
    assert beta.calibration_status == "UNCALIBRATED"
    assert beta.gwet_ac2 is None
    assert beta.calibrated_pass_rate is None


def test_numeric_gates_fail_closed_on_nan_negative_and_out_of_range() -> None:
    """Numeric gates reject NaN/Inf/negative inputs and out-of-category AC2 scores (`F6`)."""
    with pytest.raises(ValidationError):
        ModelOption(
            config_id="nan-opt",
            model_name="gemini-3.8-flash",
            rule0_pass_rate=float("nan"),
            non_fabrication_rate_nfr=float("nan"),
            calibrated_pass_rate=0.90,
            cost_per_verified_outcome_usd=0.01,
            p95_latency_ms=500.0,
        )
    with pytest.raises(ValidationError):
        ModelOption(
            config_id="neg-nfr",
            model_name="gemini-3.8-flash",
            rule0_pass_rate=0.99,
            non_fabrication_rate_nfr=-0.01,
            calibrated_pass_rate=0.90,
            cost_per_verified_outcome_usd=0.01,
            p95_latency_ms=500.0,
        )
    with pytest.raises(ValueError, match="min_rule0_pass_rate"):
        compute_pareto_frontier([], min_rule0_pass_rate=float("nan"))

    with pytest.raises(ValueError, match="outside allowed categories"):
        compute_gwet_ac2([5, 5, 0, 7], [5, 5, 0, 7])

    with pytest.raises(ValueError, match="finite and non-negative"):
        allocate_focus_two_pool_chargeback(
            app_burndown_tokens={"app-a": 100.0, "app-b": -20.0},
            app_cost_centers={"app-a": "CC-A", "app-b": "CC-B"},
            reserved_pt_capacity_tokens=100.0,
            reserved_pt_fixed_cost_usd=100.0,
        )


def test_rfp_quote_accuracy_migration_hint() -> None:
    """Renamed rubric ID gives an explicit migration hint (`M6-5`)."""
    with pytest.raises(ValueError, match="rfp_quote_completeness_4pt_v1"):
        build_ai_generate_table_judge_sql(
            project_id="demo-proj",
            rubric_id="rfp_quote_accuracy_4pt_v1",
        )
