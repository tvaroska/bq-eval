"""Pure-SQLite statistical calibration parity, boundary-safe CIs, and Step-1 outcome guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from bq_eval.core.contract.otel import (
    GenAISpanKind,
    OTelGenAISpanRecord,
    resolve_application_id,
)
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.calibration import (
    build_calibration_result,
    compute_gwet_ac2,
    rogan_gladen_calibrate,
    rogan_gladen_wilson_ci,
    stratified_rogan_gladen_calibrate,
)
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
)
from tests.conftest import make_span


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


def _run_transpiled_summary_for_app(
    spans: list[OTelGenAISpanRecord],
    eval_rows: list[tuple[str, str, bool]],
    cm: ConfusionMatrix,
    *,
    app_id: str,
    rubric_id: str = "groundedness_4pt_v1",
) -> dict[str, str | int | float | None]:
    from tests.sql_helpers import (
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        _create_enterprise_sqlite_fixture,
        _install_enterprise_views,
        _new_sqlite_bq_conn,
        insert_enterprise_sqlite_span,
    )

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    for sp in spans:
        insert_enterprise_sqlite_span(conn, sp)
    ts = datetime(2026, 11, 21, 13, 0, 0, tzinfo=UTC)
    conn.executemany(
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        [
            (
                f"ev-{idx}",
                "run-test",
                tid,
                None,
                app_id,
                rubric_id,
                "gemini-3.8-flash",
                stratum,
                1,
                "rationale",
                4 if passed else 2,
                1 if passed else 0,
                1000,
                100,
                0,
                (ts + timedelta(seconds=idx)).isoformat(),
            )
            for idx, (tid, stratum, passed) in enumerate(eval_rows)
        ],
    )
    sme_rows = (
        [(app_id, rubric_id, 1, 4, 1, 4, 1)] * cm.tp
        + [(app_id, rubric_id, 1, 4, 1, 2, 0)] * cm.fn
        + [(app_id, rubric_id, 1, 1, 0, 1, 0)] * cm.tn
        + [(app_id, rubric_id, 1, 1, 0, 4, 1)] * cm.fp
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)
    row = conn.execute(
        "SELECT * FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = ?",
        (app_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def test_unjudged_stratum_fails_closed() -> None:
    """Any unjudged or mismatched sampling stratum fails closed to NULL / UNCALIBRATED (A-1)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)

    # 20 healthy traces (unjudged) + 12 slow traces (all judged failing) -> MUST NOT report 0% CALIBRATED
    spans = [
        make_span(
            f"tr-healthy-{i:02d}",
            f"sp-healthy-{i:02d}",
            app_id="unjudged-stratum-app",
            latency_ms=800.0,
            offset_s=i,
            base_time=base,
        )
        for i in range(20)
    ] + [
        make_span(
            f"tr-slow-{i:02d}",
            f"sp-slow-{i:02d}",
            app_id="unjudged-stratum-app",
            latency_ms=8000.0,
            offset_s=30 + i,
            base_time=base,
        )
        for i in range(12)
    ]
    judge_outcomes = {f"tr-slow-{i:02d}": False for i in range(12)}
    sampling_strata = {f"tr-slow-{i:02d}": "ERROR_OR_SLA_TAIL" for i in range(12)}

    sqlite_res = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes=judge_outcomes,
        sme_confusion=cm,
        sampling_strata=sampling_strata,
        sla_latency_ms=5000.0,
    )["unjudged-stratum-app"]
    bq_res = _run_transpiled_summary_for_app(
        spans,
        [(f"tr-slow-{i:02d}", "ERROR_OR_SLA_TAIL", False) for i in range(12)],
        cm,
        app_id="unjudged-stratum-app",
    )

    assert sqlite_res.judged_samples == 12
    assert sqlite_res.raw_judge_pass_rate is None
    assert sqlite_res.calibration_status == "UNCALIBRATED"
    assert sqlite_res.calibrated_pass_rate is None
    assert sqlite_res.calibrated_ci_95_lower is None
    assert sqlite_res.calibrated_ci_95_upper is None
    assert sqlite_res.cost_per_verified_outcome_usd is None

    assert bq_res["judged_samples"] == 12
    assert bq_res["raw_judge_pass_rate"] is None
    assert bq_res["calibration_status"] == "UNCALIBRATED"
    assert bq_res["calibrated_pass_rate"] is None
    assert bq_res["calibrated_ci_95_lower"] is None
    assert bq_res["calibrated_ci_95_upper"] is None
    assert bq_res["cost_per_verified_outcome_usd"] is None

    # Mismatched stored stratum (population is all HEALTHY_POPULATION, eval_results says ERROR_OR_SLA_TAIL)
    healthy_only = [
        make_span(
            f"tr-mismatch-{i:02d}",
            f"sp-mismatch-{i:02d}",
            app_id="mismatch-app",
            latency_ms=500.0,
            offset_s=i,
            base_time=base,
        )
        for i in range(12)
    ]
    mismatch_sqlite = evaluate_traces_in_sqlite(
        spans=healthy_only,
        judge_outcomes={f"tr-mismatch-{i:02d}": True for i in range(12)},
        sme_confusion=cm,
        sampling_strata={f"tr-mismatch-{i:02d}": "ERROR_OR_SLA_TAIL" for i in range(12)},
    )["mismatch-app"]
    mismatch_bq = _run_transpiled_summary_for_app(
        healthy_only,
        [(f"tr-mismatch-{i:02d}", "ERROR_OR_SLA_TAIL", True) for i in range(12)],
        cm,
        app_id="mismatch-app",
    )
    assert mismatch_sqlite.calibration_status == "UNCALIBRATED"
    assert mismatch_sqlite.raw_judge_pass_rate is None
    assert mismatch_bq["calibration_status"] == "UNCALIBRATED"
    assert mismatch_bq["raw_judge_pass_rate"] is None

    with pytest.raises(ValueError, match="Unjudged population stratum"):
        stratified_rogan_gladen_calibrate([(20, 0, 0), (12, 12, 0)], cm)
    with pytest.raises(ValueError, match="between 1 and 100"):
        build_ai_generate_table_judge_sql(project_id="demo-proj", canary_sample_pct=0)
    with pytest.raises(ValueError, match="between 1 and 100"):
        build_ai_evaluate_agent_sql(project_id="demo-proj", canary_sample_pct=0)


def test_boundary_interval_not_degenerate() -> None:
    """Lang & Reiczigel (2014) CI never collapses at 100%/0% boundaries and enforces min-n (A-2)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)

    # 15/15 passing with 87/2/7/4 SME matrix (matches golden-4app underwriting boundary)
    cm_std = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    spans_15 = [
        make_span(
            f"tr-b15-{i:02d}", f"sp-b15-{i:02d}", app_id="b15-app", offset_s=i, base_time=base
        )
        for i in range(15)
    ]
    sq_15 = evaluate_traces_in_sqlite(
        spans=spans_15,
        judge_outcomes={f"tr-b15-{i:02d}": True for i in range(15)},
        sme_confusion=cm_std,
    )["b15-app"]
    bq_15 = _run_transpiled_summary_for_app(
        spans_15,
        [(f"tr-b15-{i:02d}", "CANARY_5PCT", True) for i in range(15)],
        cm_std,
        app_id="b15-app",
    )
    py_15 = rogan_gladen_calibrate(1.0, 15, cm_std)
    assert sq_15.calibration_status == "CALIBRATED"
    assert sq_15.calibrated_ci_95_lower is not None
    assert bq_15["calibrated_ci_95_lower"] is not None
    assert sq_15.calibrated_ci_95_lower == pytest.approx(py_15.ci_95_lower, abs=1e-12)
    assert float(bq_15["calibrated_ci_95_lower"]) == pytest.approx(py_15.ci_95_lower, abs=1e-12)
    assert sq_15.calibrated_ci_95_lower < 0.70  # Lang & Reiczigel ~0.6204, never delta 0.9498

    # 236/236 passing with 30/30 perfect reference agreement (tp=25, fn=0, tn=5, fp=0)
    cm_perf = ConfusionMatrix(tp=25, fn=0, tn=5, fp=0)
    spans_236 = [
        make_span(
            f"tr-b236-{i:03d}", f"sp-b236-{i:03d}", app_id="b236-app", offset_s=i, base_time=base
        )
        for i in range(236)
    ]
    sq_236 = evaluate_traces_in_sqlite(
        spans=spans_236,
        judge_outcomes={f"tr-b236-{i:03d}": True for i in range(236)},
        sme_confusion=cm_perf,
    )["b236-app"]
    bq_236 = _run_transpiled_summary_for_app(
        spans_236,
        [(f"tr-b236-{i:03d}", "CANARY_5PCT", True) for i in range(236)],
        cm_perf,
        app_id="b236-app",
    )
    py_236 = rogan_gladen_calibrate(1.0, 236, cm_perf)
    assert sq_236.calibrated_ci_95_lower is not None
    assert bq_236["calibrated_ci_95_lower"] is not None
    assert sq_236.calibrated_ci_95_lower == pytest.approx(py_236.ci_95_lower, abs=1e-12)
    assert float(bq_236["calibrated_ci_95_lower"]) == pytest.approx(py_236.ci_95_lower, abs=1e-12)
    assert 0.80 < sq_236.calibrated_ci_95_lower < 0.98
    assert sq_236.calibrated_ci_95_upper == pytest.approx(1.0, abs=1e-12)

    # 0/20 passing with perfect reference -> non-degenerate upper bound > 0
    spans_20 = [
        make_span(
            f"tr-z20-{i:02d}", f"sp-z20-{i:02d}", app_id="z20-app", offset_s=i, base_time=base
        )
        for i in range(20)
    ]
    sq_0 = evaluate_traces_in_sqlite(
        spans=spans_20,
        judge_outcomes={f"tr-z20-{i:02d}": False for i in range(20)},
        sme_confusion=cm_perf,
    )["z20-app"]
    bq_0 = _run_transpiled_summary_for_app(
        spans_20,
        [(f"tr-z20-{i:02d}", "CANARY_5PCT", False) for i in range(20)],
        cm_perf,
        app_id="z20-app",
    )
    py_0 = rogan_gladen_calibrate(0.0, 20, cm_perf)
    assert sq_0.calibrated_ci_95_upper is not None
    assert bq_0["calibrated_ci_95_upper"] is not None
    assert sq_0.calibrated_ci_95_upper == pytest.approx(py_0.ci_95_upper, abs=1e-12)
    assert float(bq_0["calibrated_ci_95_upper"]) == pytest.approx(py_0.ci_95_upper, abs=1e-12)
    assert 0.05 < sq_0.calibrated_ci_95_upper < 0.50

    # Minimum-n guard (3 judged + 2 ref, 9 judged + 100 ref, and 10 judged + 4 neg ref)
    spans_3 = [
        make_span(f"trace-s3-{i}", f"span-s3-{i}", app_id="small-app", offset_s=i, base_time=base)
        for i in range(3)
    ]
    cm_tiny = ConfusionMatrix(tp=1, fn=0, tn=1, fp=0)
    sq_small = evaluate_traces_in_sqlite(
        spans=spans_3,
        judge_outcomes={f"trace-s3-{i}": True for i in range(3)},
        sme_confusion=cm_tiny,
    )["small-app"]
    bq_small = _run_transpiled_summary_for_app(
        spans_3,
        [(f"trace-s3-{i}", "CANARY_5PCT", True) for i in range(3)],
        cm_tiny,
        app_id="small-app",
    )
    assert sq_small.calibration_status == "UNCALIBRATED"
    assert sq_small.calibrated_pass_rate is None
    assert bq_small["calibration_status"] == "UNCALIBRATED"
    assert bq_small["calibrated_pass_rate"] is None

    spans_10 = [
        make_span(
            f"tr-s10-{i:02d}", f"sp-s10-{i:02d}", app_id="s10-app", offset_s=i, base_time=base
        )
        for i in range(10)
    ]
    sq_9 = evaluate_traces_in_sqlite(
        spans=spans_10[:9],
        judge_outcomes={f"tr-s10-{i:02d}": True for i in range(9)},
        sme_confusion=cm_std,
    )["s10-app"]
    assert sq_9.calibration_status == "UNCALIBRATED"
    sq_low_neg = evaluate_traces_in_sqlite(
        spans=spans_10,
        judge_outcomes={f"tr-s10-{i:02d}": True for i in range(10)},
        sme_confusion=ConfusionMatrix(tp=5, fn=0, tn=4, fp=0),
    )["s10-app"]
    assert sq_low_neg.calibration_status == "UNCALIBRATED"


def test_negative_youden_j_stays_null() -> None:
    """Negative Youden's J fails closed to NULL in SQLite/SQL, plus AC2 boundary & app-id guards (T-5)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    cm_neg = ConfusionMatrix(tp=4, fn=6, tn=4, fp=6)  # TPR=0.4, TNR=0.4, J=-0.2
    spans = [
        make_span(
            f"tr-negj-{i:02d}", f"sp-negj-{i:02d}", app_id="negj-app", offset_s=i, base_time=base
        )
        for i in range(12)
    ]
    outcomes = {f"tr-negj-{i:02d}": (i < 9) for i in range(12)}
    ordinal_pairs = [(4, 4), (4, 3), (3, 3), (2, 2), (1, 1)]
    exact_ac2 = compute_gwet_ac2(
        [h for h, _ in ordinal_pairs],
        [j for _, j in ordinal_pairs],
    )

    sq_neg = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes=outcomes,
        sme_confusion=cm_neg,
        sme_ordinal_pairs=ordinal_pairs,
        irr_threshold=exact_ac2,
    )["negj-app"]
    bq_neg = _run_transpiled_summary_for_app(
        spans,
        [(f"tr-negj-{i:02d}", "CANARY_5PCT", i < 9) for i in range(12)],
        cm_neg,
        app_id="negj-app",
    )

    assert sq_neg.calibration_status == "UNCALIBRATED"
    assert sq_neg.calibrated_pass_rate is None
    assert sq_neg.calibrated_ci_95_lower is None
    assert sq_neg.calibrated_ci_95_upper is None
    assert sq_neg.cost_per_verified_outcome_usd is None
    assert sq_neg.passes_irr_gate is True

    sq_below_gate = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes=outcomes,
        sme_confusion=cm_neg,
        sme_ordinal_pairs=ordinal_pairs,
        irr_threshold=exact_ac2 + 0.01,
    )["negj-app"]
    assert sq_below_gate.passes_irr_gate is False

    assert bq_neg["calibration_status"] == "UNCALIBRATED"
    assert bq_neg["calibrated_pass_rate"] is None
    assert bq_neg["calibrated_ci_95_lower"] is None
    assert bq_neg["calibrated_ci_95_upper"] is None

    # Imbalanced negative J (-0.05) with positive Wilson-adjusted J (+0.052): kills raw-J guard removal
    cm_neg_imb = ConfusionMatrix(tp=0, fn=5, tn=19, fp=1)
    sq_neg_imb = evaluate_traces_in_sqlite(
        spans=spans, judge_outcomes=outcomes, sme_confusion=cm_neg_imb
    )["negj-app"]
    bq_neg_imb = _run_transpiled_summary_for_app(
        spans,
        [(f"tr-negj-{i:02d}", "CANARY_5PCT", i < 9) for i in range(12)],
        cm_neg_imb,
        app_id="negj-app",
    )
    assert sq_neg_imb.calibration_status == "UNCALIBRATED"
    assert sq_neg_imb.calibrated_pass_rate is None
    assert bq_neg_imb["calibration_status"] == "UNCALIBRATED"
    assert bq_neg_imb["calibrated_pass_rate"] is None

    with pytest.raises(ValueError, match="uncalibrated after Wilson adjustment"):
        rogan_gladen_wilson_ci(0.75, 20, cm_neg)

    assert resolve_application_id(None, {"service.name": "self-reported-svc"}) == "unregistered_app"


def test_outcome_requires_step1() -> None:
    """Verified outcome requires trace Step-1 pass, explicit `succeeded`, fail-closed flag, and ANDed signals (A-4)."""
    from tests.sql_helpers import (
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        _create_enterprise_sqlite_fixture,
        _install_enterprise_views,
        _new_sqlite_bq_conn,
        insert_enterprise_sqlite_span,
    )

    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []

    def _add_outcome(
        idx: int,
        outcome_id: str,
        *,
        root_status: str = "OK",
        root_r0: bool = True,
        close_attrs: dict[str, str],
    ) -> str:
        tid = f"trace-a4-{idx:02d}"
        spans.append(
            make_span(
                tid,
                f"sp-a4-root-{idx:02d}",
                app_id="a4-app",
                outcome_id=outcome_id,
                outcome_owner="a4-app",
                status_code=root_status,
                rule0_pass=root_r0,
                offset_s=idx * 5,
                base_time=base,
            )
        )
        spans.append(
            make_span(
                tid,
                f"sp-a4-close-{idx:02d}",
                parent_span_id=f"sp-a4-root-{idx:02d}",
                span_name="outcome.closed",
                app_id="a4-app",
                outcome_id=outcome_id,
                outcome_owner="a4-app",
                model_name="none",
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                thinking_tokens=0,
                latency_ms=0.0,
                offset_s=idx * 5 + 1,
                attributes=close_attrs,
                base_time=base,
            )
        )
        return tid

    # ERROR span in trace + succeeded/true close -> MUST NOT be verified
    tid_err = _add_outcome(
        1,
        "out-err-step1",
        root_status="ERROR",
        root_r0=False,
        close_attrs={"outcome.status": "succeeded", "outcome.verified": "true"},
    )
    # Missing outcome.status -> defaults to 'unknown', MUST NOT be verified
    _add_outcome(2, "out-missing-status", close_attrs={"outcome.verified": "true"})
    # outcome.verified='false' + passing Step-2 judge -> judge pass MUST NOT override!
    tid_false_judged = _add_outcome(
        3,
        "out-false-judged",
        close_attrs={"outcome.status": "succeeded", "outcome.verified": "false"},
    )
    # outcome.verified='0' -> fails closed (not treated as absent)
    _add_outcome(
        4, "out-flag-zero", close_attrs={"outcome.status": "succeeded", "outcome.verified": "0"}
    )
    # outcome.verified='no' -> fails closed (not treated as absent)
    _add_outcome(
        5, "out-flag-no", close_attrs={"outcome.status": "succeeded", "outcome.verified": "no"}
    )
    # Genuine clean trace + succeeded + verified='true' + judge pass -> verified!
    tid_clean = _add_outcome(
        6,
        "out-clean-pass",
        close_attrs={"outcome.status": "succeeded", "outcome.verified": "true"},
    )

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    for sp in spans:
        insert_enterprise_sqlite_span(conn, sp)
    for ev_id, tid, oid in (
        ("ev-a4-1", tid_false_judged, "out-false-judged"),
        ("ev-a4-2", tid_err, "out-err-step1"),
        ("ev-a4-3", tid_clean, "out-clean-pass"),
    ):
        conn.execute(
            ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
            (
                ev_id,
                "run-a4",
                tid,
                oid,
                "a4-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "ok",
                4,
                1,
                500,
                50,
                0,
                (base + timedelta(seconds=60)).isoformat(),
            ),
        )

    bq_outcomes = {
        str(r["outcome_id"]): (str(r["outcome_status"]), int(r["is_verified"]))
        for r in conn.execute(
            "SELECT outcome_id, outcome_status, is_verified FROM v_outcomes"
        ).fetchall()
    }
    bq_cpvo = conn.execute(
        "SELECT total_outcomes, verified_outcomes FROM v_enterprise_cost_per_verified_outcome "
        "WHERE apphub_application_id = 'a4-app'"
    ).fetchone()
    conn.close()

    twin = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes={tid_false_judged: True, tid_err: True, tid_clean: True},
    )["a4-app"]

    assert bq_outcomes["out-err-step1"] == ("succeeded", 0)
    assert bq_outcomes["out-missing-status"] == ("unknown", 0)
    assert bq_outcomes["out-false-judged"] == ("succeeded", 0)
    assert bq_outcomes["out-flag-zero"] == ("succeeded", 0)
    assert bq_outcomes["out-flag-no"] == ("succeeded", 0)
    assert bq_outcomes["out-clean-pass"] == ("succeeded", 1)

    assert int(bq_cpvo["total_outcomes"]) == twin.outcome_total_count == 6
    assert int(bq_cpvo["verified_outcomes"]) == twin.outcome_verified_count == 1
