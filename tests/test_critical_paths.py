"""Gold-on-Silver statistical calibration parity, boundary-safe CIs, ADR-049 agreement gates & outcome guards."""

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
    evaluate_calibration_agreement_gate,
    rogan_gladen_calibrate,
    rogan_gladen_wilson_ci,
    stratified_rogan_gladen_calibrate,
)
from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)
from bq_eval.demo.golden_seeds import build_golden_seed_templates
from bq_eval.demo.loadgen import evaluate_time_shifted_corpus, generate_time_shifted_corpus
from bq_eval.finops.contract.results import ModelOption
from bq_eval.finops.engine.spend import (
    allocate_focus_two_pool_chargeback,
    compute_pareto_frontier,
)
from tests.conftest import eval_traces, make_span, open_runner_for_spans, seed_sme_confusion


def test_silver_gold_rogan_gladen_and_gwet_ac2_exact_parity() -> None:
    """Silver + Gold SQL views match Python Rogan-Gladen + 95% CI + Gwet's AC2 within 1e-12."""
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

    summary = eval_traces(
        spans=spans,
        judge_outcomes=judge_outcomes,
        sme_ordinal_pairs=sme_pairs,
    )["critical-app"]

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


def test_uncalibrated_youden_j_guard_in_python_and_sql() -> None:
    """When Youden's J <= 1e-6, Python raises ValueError and Gold SQL fails closed to UNCALIBRATED."""
    uncalibrated_cm = ConfusionMatrix(tp=5, fn=5, tn=5, fp=5)
    with pytest.raises(ValueError, match="uncalibrated"):
        rogan_gladen_calibrate(observed_pass_rate=0.80, sample_size=20, confusion=uncalibrated_cm)

    spans = [make_span(f"tr-unc-{i}", f"sp-unc-{i}", offset_s=i) for i in range(12)]
    outcomes = {f"tr-unc-{i}": True for i in range(12)}
    summary = eval_traces(
        spans=spans,
        judge_outcomes=outcomes,
        sme_confusion=uncalibrated_cm,
    )["critical-app"]
    assert summary.calibration_status == "UNCALIBRATED"
    assert summary.calibrated_pass_rate is None
    assert summary.calibrated_ci_95_lower is None
    assert summary.calibrated_ci_95_upper is None
    assert summary.cost_per_verified_outcome_usd is None


def test_rule0_tool_loop_and_template16_real_9_tool_spans() -> None:
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
    summary = eval_traces(spans=t16_spans)["unregistered_app"]
    assert summary.rule0_pass_rate == 0.0
    assert summary.tool_success_rate == 1.0

    report = evaluate_time_shifted_corpus(spans=spans, reference_time=now)
    audit_row = next(r for r in report.step1_rows if r.apphub_application_id == "unregistered_app")
    assert audit_row.step1_pass_rate == pytest.approx(0.75)


def test_numeric_gates_fail_closed_on_nan_negative_and_out_of_range() -> None:
    """Numeric gates reject NaN/Inf/negative inputs, out-of-category AC2 scores, and propagate StratPPI variance."""
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

    with pytest.raises(ValueError, match="outside allowed integer categories"):
        compute_gwet_ac2([5, 5, 0, 7], [5, 5, 0, 7])

    with pytest.raises(ValueError, match="finite and non-negative"):
        allocate_focus_two_pool_chargeback(
            app_burndown_tokens={"app-a": 100.0, "app-b": -20.0},
            app_cost_centers={"app-a": "CC-A", "app-b": "CC-B"},
            reserved_pt_capacity_tokens=100.0,
            reserved_pt_fixed_cost_usd=100.0,
        )

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


def test_unjudged_stratum_fails_closed() -> None:
    """Any unjudged population stratum fails closed to NULL / UNCALIBRATED (`A-1`, `A-15`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)

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
    res = eval_traces(
        spans=spans,
        judge_outcomes={f"tr-slow-{i:02d}": False for i in range(12)},
        sme_confusion=cm,
        sampling_strata={f"tr-slow-{i:02d}": "ERROR_OR_SLA_TAIL" for i in range(12)},
        sla_latency_ms=5000.0,
    )["unjudged-stratum-app"]

    assert res.judged_samples == 12
    assert res.raw_judge_pass_rate is None
    assert res.calibration_status == "UNCALIBRATED"
    assert res.calibrated_pass_rate is None
    assert res.cost_per_verified_outcome_usd is None

    with pytest.raises(ValueError, match="Unjudged population stratum"):
        stratified_rogan_gladen_calibrate([(20, 0, 0), (12, 12, 0)], cm)
    with pytest.raises(ValueError, match="between 1 and 100"):
        build_ai_generate_table_judge_sql(project_id="demo-proj", canary_sample_pct=0)
    with pytest.raises(ValueError, match="between 1 and 100"):
        build_ai_evaluate_agent_sql(project_id="demo-proj", canary_sample_pct=0)


def test_judged_exceeding_population_fails_closed() -> None:
    """`A-15`: Orphan `eval_results` trace IDs or `judged_n > pop_n` fail closed to UNCALIBRATED / NULL."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    with pytest.raises(ValueError, match="cannot exceed population_count"):
        stratified_rogan_gladen_calibrate([(10, 11, 10)], cm)

    spans = [
        make_span(
            f"tr-pop-{i:02d}", f"sp-pop-{i:02d}", app_id="orphan-app", offset_s=i, base_time=base
        )
        for i in range(10)
    ]
    with open_runner_for_spans(spans) as runner:
        eval_rows = [
            {
                "eval_id": f"ev-pop-{i:02d}",
                "eval_run_id": "run-orphan",
                "trace_id": f"tr-pop-{i:02d}",
                "apphub_application_id": "orphan-app",
                "rubric_id": "groundedness_4pt_v1",
                "judge_model": "gemini-3.8-flash",
                "sampling_stratum": "CANARY_5PCT",
                "rule0_pass": True,
                "ordinal_score": 4,
                "binary_pass": True,
                "judge_status": "SCORED",
                "judge_input_tokens": 500,
                "judge_output_tokens": 50,
                "judge_thinking_tokens": 0,
                "evaluated_at": "2026-11-21T12:05:00+00:00",
            }
            for i in range(10)
        ]
        eval_rows.append(
            {
                **eval_rows[0],
                "eval_id": "ev-orphan-extra",
                "trace_id": "tr-nonexistent-999",
            }
        )
        runner.load_rows("agent_eval.eval_results", eval_rows, append=True)
        seed_sme_confusion(runner, "orphan-app", cm)
        row = runner.query(
            "SELECT calibration_status, raw_judge_pass_rate, calibrated_pass_rate "
            "FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'orphan-app'"
        ).rows[0]
    assert row["calibration_status"] == "UNCALIBRATED"
    assert row["raw_judge_pass_rate"] is None
    assert row["calibrated_pass_rate"] is None


def test_agreement_gate_minimum_pairs_and_status() -> None:
    """`A-13` / `A-18`: `passes_irr_gate = FALSE` blocks `CALIBRATED`, enforces `RL-1 >= 100`, and flags `STALE_CALIBRATION`."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            f"tr-irr-{i:02d}", f"sp-irr-{i:02d}", app_id="irr-app", offset_s=i, base_time=base
        )
        for i in range(15)
    ]
    # Binary TPR=TNR=1.0 via confusion matrix, but only 6 ordinal pairs (< 10 minimum) -> fails closed!
    short_pairs_res = eval_traces(
        spans=spans,
        judge_outcomes={f"tr-irr-{i:02d}": True for i in range(15)},
        sme_confusion=ConfusionMatrix(tp=10, fn=0, tn=10, fp=0),
        sme_ordinal_pairs=[(4, 4), (4, 4), (4, 4), (1, 1), (1, 1), (1, 1)],
    )["irr-app"]
    assert short_pairs_res.passes_irr_gate is False
    assert short_pairs_res.calibration_status == "UNCALIBRATED"
    assert short_pairs_res.calibrated_pass_rate is None

    # Dual-SME disagreement (`sme_vs_sme_ac2 < 0.80`), dual-SME floor `< min_pairs`, & `STALE_CALIBRATION`
    gate_dual_fail = evaluate_calibration_agreement_gate(
        [4] * 10 + [1] * 10,
        [4] * 10 + [1] * 10,
        rater_a_ordinal_scores=[4, 1] * 10,
        rater_b_ordinal_scores=[1, 4] * 10,
    )
    assert gate_dual_fail.passes_irr_gate is False
    gate_dual_short = evaluate_calibration_agreement_gate(
        [4] * 10 + [1] * 10,
        [4] * 10 + [1] * 10,
        rater_a_ordinal_scores=[4] * 4 + [1] * 4,
        rater_b_ordinal_scores=[4] * 4 + [1] * 4,
    )
    assert gate_dual_short.passes_irr_gate is False

    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": f"ev-st-{i:02d}",
                    "eval_run_id": "run-st",
                    "trace_id": f"tr-irr-{i:02d}",
                    "apphub_application_id": "irr-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "judge_model_snapshot_id": "gemini-3.8-flash@2026-12",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 500,
                    "judge_output_tokens": 50,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T12:05:00+00:00",
                }
                for i in range(15)
            ],
            append=True,
        )
        runner.load_rows(
            "agent_eval.sme_labels",
            [
                {
                    "label_id": f"sme-st-{i}",
                    "trace_id": f"sme-tr-{i}",
                    "apphub_application_id": "irr-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "rater_id": "abbott_sme",
                    "rule0_pass": True,
                    "human_ordinal_score": 4 if i < 10 else 1,
                    "human_pass": i < 10,
                    "judge_ordinal_score": 4 if i < 10 else 1,
                    "judge_pass": i < 10,
                    "judge_model_snapshot_id": "gemini-3.8-flash@2026-10",
                    "labeled_at": "2026-11-21T12:00:00+00:00",
                }
                for i in range(20)
            ],
            append=True,
        )
        stale_row = runner.query(
            "SELECT calibration_status, calibrated_pass_rate "
            "FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'irr-app'"
        ).rows[0]
    assert stale_row["calibration_status"] == "STALE_CALIBRATION"
    assert stale_row["calibrated_pass_rate"] is None


def test_python_calibration_enforces_minimum_samples() -> None:
    """`A-16`: `rogan_gladen_calibrate` enforces `MIN_CALIBRATED_*` and `RL-1 >= 100` sample floors."""
    cm_valid = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    with pytest.raises(ValueError, match="sample_size"):
        rogan_gladen_calibrate(0.9, 9, cm_valid)
    with pytest.raises(ValueError, match="positive and 5 negative"):
        rogan_gladen_calibrate(0.9, 15, ConfusionMatrix(tp=4, fn=0, tn=10, fp=0))
    with pytest.raises(ValueError, match="positive and 5 negative"):
        rogan_gladen_calibrate(0.9, 15, ConfusionMatrix(tp=10, fn=0, tn=4, fp=0))
    with pytest.raises(ValueError, match=">= 100"):
        rogan_gladen_calibrate(0.9, 50, cm_valid, risk_tier="RL-1")
    with pytest.raises(ValueError, match=">= 100 paired labels"):
        rogan_gladen_calibrate(
            0.9, 120, ConfusionMatrix(tp=40, fn=0, tn=40, fp=0), risk_tier="RL-1"
        )


def test_boundary_interval_not_degenerate() -> None:
    """Lang & Reiczigel (2014) CI never collapses at 100%/0% boundaries and enforces min-n (`A-2`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    cm_std = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    spans_15 = [
        make_span(
            f"tr-b15-{i:02d}", f"sp-b15-{i:02d}", app_id="b15-app", offset_s=i, base_time=base
        )
        for i in range(15)
    ]
    sq_15 = eval_traces(
        spans=spans_15,
        judge_outcomes={f"tr-b15-{i:02d}": True for i in range(15)},
        sme_confusion=cm_std,
    )["b15-app"]
    py_15 = rogan_gladen_calibrate(1.0, 15, cm_std)
    assert sq_15.calibration_status == "CALIBRATED"
    assert sq_15.calibrated_ci_95_lower == pytest.approx(py_15.ci_95_lower, abs=1e-12)
    assert sq_15.calibrated_ci_95_lower is not None and sq_15.calibrated_ci_95_lower < 0.70

    cm_perf = ConfusionMatrix(tp=25, fn=0, tn=5, fp=0)
    spans_236 = [
        make_span(
            f"tr-b236-{i:03d}", f"sp-b236-{i:03d}", app_id="b236-app", offset_s=i, base_time=base
        )
        for i in range(236)
    ]
    sq_236 = eval_traces(
        spans=spans_236,
        judge_outcomes={f"tr-b236-{i:03d}": True for i in range(236)},
        sme_confusion=cm_perf,
    )["b236-app"]
    py_236 = rogan_gladen_calibrate(1.0, 236, cm_perf)
    assert sq_236.calibrated_ci_95_lower == pytest.approx(py_236.ci_95_lower, abs=1e-12)
    assert sq_236.calibrated_ci_95_upper == pytest.approx(1.0, abs=1e-12)

    spans_20 = [
        make_span(
            f"tr-z20-{i:02d}", f"sp-z20-{i:02d}", app_id="z20-app", offset_s=i, base_time=base
        )
        for i in range(20)
    ]
    sq_0 = eval_traces(
        spans=spans_20,
        judge_outcomes={f"tr-z20-{i:02d}": False for i in range(20)},
        sme_confusion=cm_perf,
    )["z20-app"]
    py_0 = rogan_gladen_calibrate(0.0, 20, cm_perf)
    assert sq_0.calibrated_ci_95_upper == pytest.approx(py_0.ci_95_upper, abs=1e-12)
    assert sq_0.calibrated_ci_95_upper is not None and 0.05 < sq_0.calibrated_ci_95_upper < 0.50

    spans_10 = [
        make_span(
            f"tr-s10-{i:02d}", f"sp-s10-{i:02d}", app_id="s10-app", offset_s=i, base_time=base
        )
        for i in range(10)
    ]
    assert (
        eval_traces(
            spans=spans_10[:9],
            judge_outcomes={f"tr-s10-{i:02d}": True for i in range(9)},
            sme_confusion=cm_std,
        )["s10-app"].calibration_status
        == "UNCALIBRATED"
    )
    assert (
        eval_traces(
            spans=spans_10,
            judge_outcomes={f"tr-s10-{i:02d}": True for i in range(10)},
            sme_confusion=ConfusionMatrix(tp=6, fn=0, tn=4, fp=0),
        )["s10-app"].calibration_status
        == "UNCALIBRATED"
    )


def test_negative_youden_j_stays_null() -> None:
    """Negative Youden's J fails closed to NULL in SQL/Python, plus app-id fallback (`T-5`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    cm_neg = ConfusionMatrix(tp=4, fn=6, tn=4, fp=6)
    spans = [
        make_span(
            f"tr-negj-{i:02d}", f"sp-negj-{i:02d}", app_id="negj-app", offset_s=i, base_time=base
        )
        for i in range(12)
    ]
    outcomes = {f"tr-negj-{i:02d}": (i < 9) for i in range(12)}
    ordinal_pairs = [(4, 4), (4, 3), (3, 3), (2, 2), (1, 1)] * 2

    sq_neg = eval_traces(
        spans=spans,
        judge_outcomes=outcomes,
        sme_confusion=cm_neg,
        sme_ordinal_pairs=ordinal_pairs,
    )["negj-app"]
    assert sq_neg.calibration_status == "UNCALIBRATED"
    assert sq_neg.calibrated_pass_rate is None
    assert sq_neg.cost_per_verified_outcome_usd is None
    assert sq_neg.passes_irr_gate is True

    cm_neg_imb = ConfusionMatrix(tp=0, fn=5, tn=19, fp=1)
    sq_neg_imb = eval_traces(spans=spans, judge_outcomes=outcomes, sme_confusion=cm_neg_imb)[
        "negj-app"
    ]
    assert sq_neg_imb.calibration_status == "UNCALIBRATED"
    assert sq_neg_imb.calibrated_pass_rate is None

    with pytest.raises(ValueError, match="uncalibrated after Wilson adjustment"):
        rogan_gladen_wilson_ci(0.75, 20, cm_neg)
    assert resolve_application_id(None, {"service.name": "self-reported-svc"}) == "unregistered_app"


def test_outcome_requires_step1() -> None:
    """Verified outcome requires trace Step-1 pass, explicit `succeeded`, fail-closed flag, and ANDed signals (`A-4`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []

    def _add_outcome(
        idx: int,
        outcome_id: str,
        *,
        root_status: str = "OK",
        root_r0: bool = True,
        close_attrs: dict[str, object],
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

    tid_err = _add_outcome(
        1,
        "out-err-step1",
        root_status="ERROR",
        root_r0=False,
        close_attrs={"outcome.status": "succeeded", "outcome.verified": "true"},
    )
    _add_outcome(2, "out-missing-status", close_attrs={"outcome.verified": "true"})
    tid_false_judged = _add_outcome(
        3,
        "out-false-judged",
        close_attrs={"outcome.status": "succeeded", "outcome.verified": "false"},
    )
    _add_outcome(
        4, "out-flag-zero", close_attrs={"outcome.status": "succeeded", "outcome.verified": "0"}
    )
    _add_outcome(
        5, "out-flag-no", close_attrs={"outcome.status": "succeeded", "outcome.verified": "no"}
    )
    tid_clean = _add_outcome(
        6,
        "out-clean-pass",
        close_attrs={"outcome.status": "succeeded", "outcome.verified": "true"},
    )

    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": ev_id,
                    "eval_run_id": "run-a4",
                    "trace_id": tid,
                    "outcome_id": oid,
                    "apphub_application_id": "a4-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 500,
                    "judge_output_tokens": 50,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": (base + timedelta(seconds=60)).isoformat(),
                }
                for ev_id, tid, oid in (
                    ("ev-a4-1", tid_false_judged, "out-false-judged"),
                    ("ev-a4-2", tid_err, "out-err-step1"),
                    ("ev-a4-3", tid_clean, "out-clean-pass"),
                )
            ],
            append=True,
        )
        bq_outcomes = {
            str(r["outcome_id"]): (str(r["outcome_status"]), bool(r["is_verified"]))
            for r in runner.query(
                "SELECT outcome_id, outcome_status, is_verified FROM `agent_eval.v_outcomes`"
            ).rows
        }
        bq_cpvo = runner.query(
            "SELECT total_outcomes, verified_outcomes "
            "FROM `agent_eval.v_enterprise_cost_per_verified_outcome` "
            "WHERE apphub_application_id = 'a4-app'"
        ).rows[0]

    twin = eval_traces(
        spans=spans,
        judge_outcomes={tid_false_judged: True, tid_err: True, tid_clean: True},
    )["a4-app"]

    assert bq_outcomes["out-err-step1"] == ("succeeded", False)
    assert bq_outcomes["out-missing-status"] == ("unknown", False)
    assert bq_outcomes["out-false-judged"] == ("succeeded", False)
    assert bq_outcomes["out-flag-zero"] == ("succeeded", False)
    assert bq_outcomes["out-flag-no"] == ("succeeded", False)
    assert bq_outcomes["out-clean-pass"] == ("succeeded", True)
    assert int(str(bq_cpvo["total_outcomes"])) == twin.outcome_total_count == 6
    assert int(str(bq_cpvo["verified_outcomes"])) == twin.outcome_verified_count == 1
