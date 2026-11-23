"""Comprehensive tests for pure-SQLite statistical parity and CRITICAL.md invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import (
    BQAAAgentEvent,
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
    compute_weighted_burndown_tokens,
)
from bq_eval.core.contract.pricing import ModelPricingSKU
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.contract.rubrics import OrdinalScoreOutput
from bq_eval.core.engine.bqaa import (
    bqaa_events_to_otel_spans,
    bqaa_events_to_trace_payloads,
    deduplicate_bqaa_events,
)
from bq_eval.core.engine.calibration import (
    build_calibration_result,
    compute_gwet_ac2,
    rogan_gladen_calibrate,
    rogan_gladen_wilson_ci,
    stratified_rogan_gladen_calibrate,
)
from bq_eval.core.engine.cost import compute_span_cost_usd
from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
    evaluate_traces_in_sqlite,
)
from bq_eval.demo.golden_seeds import build_golden_seed_templates
from bq_eval.demo.loadgen import evaluate_time_shifted_corpus, generate_time_shifted_corpus
from bq_eval.finops.contract.results import ModelOption
from bq_eval.finops.engine.spend import (
    allocate_focus_two_pool_chargeback,
    compute_pareto_frontier,
    diagnose_prefix_cache_dilution,
)
from tests.conftest import make_span

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_pure_sqlite_rogan_gladen_and_gwet_ac2_exact_parity() -> None:
    """Pure SQLite views must match Python Rogan-Gladen + 95% CI + Gwet's AC2 within 1e-12."""
    spans = [make_span(f"trace-cal-{i:03d}", f"span-cal-{i:03d}", offset_s=i) for i in range(25)]
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

    spans = [make_span(f"tr-unc-{i}", f"sp-unc-{i}", offset_s=i) for i in range(5)]
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
    spans, _, _ = generate_time_shifted_corpus(
        reference_time=now,
        total_replicas=2,
        last_hour_replicas=1,
    )
    assert len(spans) == 33 * 2
    t16_spans = [s for s in spans if s.trace_id.startswith("seed-audit-004-r001")]
    summary = evaluate_traces_in_sqlite(spans=t16_spans)["unregistered_app"]
    assert summary.rule0_pass_rate == 0.0
    assert summary.tool_success_rate == 1.0

    report = evaluate_time_shifted_corpus(spans=spans, reference_time=now)
    audit_row = next(r for r in report.step1_rows if r.apphub_application_id == "unregistered_app")
    assert audit_row.step1_pass_rate == pytest.approx(0.75)


def test_ordinal_score_output_contract() -> None:
    """CoT-first OrdinalScoreOutput validates fields, min_length, and binary_pass consistency."""
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
    with pytest.raises(ValidationError, match="binary_pass"):
        OrdinalScoreOutput(
            reasoning_trace="Contradictory pass flag on failing score.",
            ordinal_score=1,
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


def test_bqaa_edge_cases_tool_error_classification_and_outcome_closed() -> None:
    """Cover context-aware TOOL_ERROR status, invalid traffic fallback, and OUTCOME_CLOSED."""
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
            error_message="internal plugin crash",
            content={"tool_name": "carrier_api", "code": 500},
            attributes={"custom_metadata": {"gcp.apphub.application.id": "edge-app"}},
        ),
        BQAAAgentEvent(
            event_id="evt-edge-02b",
            timestamp=ts + timedelta(milliseconds=120),
            event_type="TOOL_ERROR",
            agent="edge-agent",
            session_id="sess-1",
            invocation_id="inv-edge-01",
            trace_id="trace-edge-01",
            span_id="span-edge-02b",
            error_message="deadline exceeded timeout",
            content={"tool_name": "carrier_api"},
            attributes={"custom_metadata": {"gcp.apphub.application.id": "edge-app"}},
        ),
        BQAAAgentEvent(
            event_id="evt-edge-02c",
            timestamp=ts + timedelta(milliseconds=140),
            event_type="TOOL_ERROR",
            agent="edge-agent",
            session_id="sess-1",
            invocation_id="inv-edge-01",
            trace_id="trace-edge-01",
            span_id="span-edge-02c",
            error_message="HTTP 429 quota resource_exhausted",
            content={"tool_name": "carrier_api"},
            attributes={"custom_metadata": {"gcp.apphub.application.id": "edge-app"}},
        ),
        BQAAAgentEvent(
            event_id="evt-edge-02d",
            timestamp=ts + timedelta(milliseconds=160),
            event_type="TOOL_ERROR",
            agent="edge-agent",
            session_id="sess-1",
            invocation_id="inv-edge-01",
            trace_id="trace-edge-01",
            span_id="span-edge-02d",
            error_message="JSON schema validation error",
            content={"tool_name": "carrier_api"},
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

    assert len(spans) == 6
    assert spans[0].status_code == "ERROR"
    assert spans[0].tool_call_status == "TOOL_ERROR"
    assert spans[0].rule0_pass is False
    assert spans[1].tool_call_status == "TIMEOUT"
    assert spans[2].tool_call_status == "RATE_LIMITED"
    assert spans[3].tool_call_status == "VALIDATION_ERROR"
    assert spans[4].span_kind == GenAISpanKind.INVOKE_AGENT
    assert spans[4].outcome_id == "oc-edge-1"
    assert spans[5].finops_traffic_type == FinOpsTrafficType.UNCLASSIFIED
    assert len(payloads) == 1
    assert "Check policy" in payloads[0].user_prompt
    assert "42" in payloads[0].final_response


def test_weighted_burndown_200k_cliff_boundary() -> None:
    """Exact 200,000 input tokens stays standard; 200,001 crosses into >200K cliff."""
    bd_200k = compute_weighted_burndown_tokens(
        input_tokens=200_000, cached_input_tokens=0, output_tokens=0, thinking_tokens=0
    )
    bd_200k_plus1 = compute_weighted_burndown_tokens(
        input_tokens=200_001, cached_input_tokens=0, output_tokens=0, thinking_tokens=0
    )
    assert bd_200k == pytest.approx(200_000.0)
    assert bd_200k_plus1 == pytest.approx(400_002.0)


def test_compute_span_cost_usd_with_explicit_sku_cliff() -> None:
    """`compute_span_cost_usd` switches from standard to long-context SKU rates at 200,001 tokens."""
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
    std_span = make_span(
        "trace-std-001",
        "span-std-001",
        input_tokens=200_000,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
    )
    cliff_span = make_span(
        "trace-clif-001",
        "span-clif-001",
        input_tokens=200_001,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
    )
    assert compute_span_cost_usd(std_span, sku) == pytest.approx(0.03)
    assert compute_span_cost_usd(cliff_span, sku) == pytest.approx((200_001 / 1e6) * 0.30)


def test_prefix_cache_dilution_long_context_recoverable_window_usd() -> None:
    """`>200K` prefix-cache dilution computes 1.8x burndown delta and `recoverable_window_usd`."""
    long_span = make_span(
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
    assert dil.recoverable_window_usd == pytest.approx((550_000 / 1e6) * (0.30 - 0.03))


def test_focus_chargeback_payg_overflow_and_removed_legacy_kwarg() -> None:
    """`allocate_focus_two_pool_chargeback` allocates PAYG overflow and rejects removed `reserved_pt_Fixed`."""
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

    with pytest.raises(TypeError):
        allocate_focus_two_pool_chargeback(  # type: ignore[call-arg]
            app_burndown_tokens={"app-a": 50_000_000.0},
            app_cost_centers={"app-a": "CC-A"},
            reserved_pt_capacity_tokens=100_000_000.0,
            reserved_pt_Fixed_cost_usd=100.0,
        )


def test_step3_paired_run_comparison_temporal_order_and_dedup() -> None:
    """Transpiled `v_step3_paired_run_comparison` orders `run_9` -> `run_10` temporally."""
    from tests.sql_helpers import (
        _extract_view_sql_text,
        _new_sqlite_bq_conn,
        _transpile_bq_view_to_sqlite,
    )

    step3_path = REPO_ROOT / "sql" / "core" / "step3_saved_results.sql"
    view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(step3_path, "agent_eval.v_step3_paired_run_comparison")
    )
    conn = _new_sqlite_bq_conn()
    conn.execute(
        "CREATE TABLE eval_results (apphub_application_id TEXT, rubric_id TEXT, eval_run_id TEXT, "
        "trace_id TEXT, ordinal_score INT, binary_pass INT, reasoning_trace TEXT, "
        "policy_sha256 TEXT, amended_clause_ids TEXT, target_clause_id TEXT DEFAULT NULL, evaluated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO eval_results ("
        "apphub_application_id, rubric_id, eval_run_id, trace_id, ordinal_score, "
        "binary_pass, reasoning_trace, policy_sha256, amended_clause_ids, evaluated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "app-1",
                "groundedness_4pt_v1",
                "run_9",
                "tr-1",
                2,
                0,
                "stale",
                "a" * 64,
                None,
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
                "a" * 64,
                None,
                "2026-11-10T10:00:00+00:00",
            ),
            (
                "app-1",
                "groundedness_4pt_v1",
                "run_10",
                "tr-1",
                2,
                0,
                "regressed",
                "b" * 64,
                "c_02",
                "2026-11-11T10:00:00+00:00",
            ),
        ],
    )
    conn.execute(f"CREATE VIEW v_step3_paired_run_comparison AS {view_sql}")
    rows = conn.execute(
        "SELECT baseline_run_id, candidate_run_id, is_negative_flip, is_positive_flip, "
        "policy_changed, amended_clause_ids "
        "FROM v_step3_paired_run_comparison"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("run_9", "run_10", 1, 0, 1, "c_02")


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
            make_span(
                tid,
                f"sp-strat-{i:04d}",
                app_id="strat-app",
                latency_ms=latency_ms,
                input_tokens=1_000,
                cached_input_tokens=0,
                output_tokens=100,
                thinking_tokens=0,
                offset_s=i,
                base_time=base,
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
    """`20:1` tail-skewed fixture proves `< 1e-12` SQL/SQLite/Python parity and unbiased `calibrated_pass_rate`."""
    from tests.sql_helpers import (
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
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
    conn.executemany(ENTERPRISE_OTEL_SPANS_BASE_INSERT, sql_span_rows)
    conn.executemany(ENTERPRISE_EVAL_RESULTS_BASE_INSERT, sql_eval_rows)
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
    """Cross-app W3C trace stays 1 trace under root `outcome_owner`, SME keys isolate per app, and outcome CPVO computes."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "trace-cross-001",
            "span-root-001",
            app_id="app-alpha",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            latency_ms=600.0,
            base_time=base,
        ),
        make_span(
            "trace-cross-001",
            "span-child-001",
            parent_span_id="span-root-001",
            app_id="app-downstream",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            latency_ms=300.0,
            offset_ms=100,
            base_time=base,
        ),
        make_span(
            "trace-cross-001",
            "span-close-001",
            parent_span_id="span-root-001",
            span_name="outcome.closed",
            app_id="app-alpha",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            model_name="none",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=0.0,
            offset_ms=600,
            attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
            base_time=base,
        ),
        make_span("trace-beta-001", "span-beta-001", app_id="app-beta", offset_s=5, base_time=base),
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
    expected_infra_cost = (0.6 + 0.3) * (2.0 * 0.00002400 + 2.0 * 0.00000250) + 0.000020
    assert alpha.outcome_cost_per_verified_ai_usd == pytest.approx(
        expected_span_cost + 0.0025, abs=1e-12
    )
    assert alpha.outcome_cost_per_verified_usd == pytest.approx(
        expected_span_cost + 0.0025 + expected_infra_cost, abs=1e-12
    )
    assert beta.calibration_status == "UNCALIBRATED"
    assert beta.gwet_ac2 is None
    assert beta.calibrated_pass_rate is None


def test_numeric_gates_fail_closed_on_nan_negative_and_out_of_range() -> None:
    """Numeric gates reject NaN/Inf/negative inputs and out-of-category AC2 scores."""
    with pytest.raises(ValidationError):
        ModelOption(
            config_id="nan-opt",
            model_name="gemini-3.8-flash",
            rule0_pass_rate=float("nan"),
            fabrication_rate=float("nan"),
            calibrated_pass_rate=0.90,
            cost_per_verified_outcome_usd=0.01,
            p95_latency_ms=500.0,
        )
    with pytest.raises(ValidationError):
        ModelOption(
            config_id="neg-fab",
            model_name="gemini-3.8-flash",
            rule0_pass_rate=0.99,
            fabrication_rate=-0.01,
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
    """Removed legacy rubric name raises an explicit migration hint."""
    with pytest.raises(ValueError, match="rfp_quote_completeness_4pt_v1"):
        build_ai_generate_table_judge_sql(
            project_id="demo-proj",
            rubric_id="rfp_quote_accuracy_4pt_v1",
        )


def test_stratified_wilson_plugin_propagates_stratified_variance() -> None:
    """20:1 tail-sampling StratPPI variance must propagate into `wilson_plugin` CIs."""
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    strata_unequal = [(950, 50, 48), (50, 50, 10)]
    res_wilson = stratified_rogan_gladen_calibrate(strata_unequal, cm, ci_method="wilson_plugin")
    naive_wilson = rogan_gladen_wilson_ci(
        observed_pass_rate=res_wilson.raw_pass_rate,
        sample_size=100,
        confusion=cm,
    )
    assert (res_wilson.ci_95_lower, res_wilson.ci_95_upper) != pytest.approx(naive_wilson, abs=1e-6)
    with pytest.raises(ValueError, match="finite and non-negative"):
        rogan_gladen_wilson_ci(0.8, 100, cm, stratified_var_p_obs=-0.01)


def test_policy_sha256_and_amended_clause_ids_in_judge_sql_and_ddl() -> None:
    """Policy digest and amended clause IDs wire through DDL, Step-3, and judge SQL."""
    digest = "a" * 64
    sql = build_ai_generate_table_judge_sql(
        project_id="demo-proj",
        policy_sha256=digest,
        amended_clause_ids=("c_03", "c_07"),
    )
    assert f"'{digest}' AS policy_sha256" in sql
    assert "'c_03,c_07' AS amended_clause_ids" in sql
    with pytest.raises(ValueError, match="policy_sha256"):
        build_ai_generate_table_judge_sql(project_id="demo-proj", policy_sha256="not-a-sha")

    ddl = (REPO_ROOT / "sql" / "core" / "ddl.sql").read_text(encoding="utf-8")
    step3 = (REPO_ROOT / "sql" / "core" / "step3_saved_results.sql").read_text(encoding="utf-8")
    assert "policy_sha256 STRING" in ddl and "amended_clause_ids STRING" in ddl
    assert "policy_sha256 STRING" in step3 and "policy_changed" in step3


def test_public_doc_hygiene_marks_evaluate_agent_preview() -> None:
    """All public docs citing `AI.EVALUATE_AGENT` explicitly note preview/non-public status."""
    files = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "architecture-and-sql.md",
        REPO_ROOT / "docs" / "bqaa-and-dual-path-architecture.md",
        REPO_ROOT / "sql" / "core" / "step2_stateless_eval.sql",
        REPO_ROOT / "sql" / "core" / "step3_saved_results.sql",
    )
    for path in files:
        lower = path.read_text(encoding="utf-8").lower()
        assert "ai.evaluate_agent" in lower
        assert "preview" in lower or "non-public" in lower
