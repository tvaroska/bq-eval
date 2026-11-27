"""Gold-on-Silver enterprise views parity on spend, CPVO, production strata (`A-12`), rubrics, and guards."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from bq_eval.core.contract.otel import (
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
)
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.calibration import (
    rogan_gladen_calibrate,
    stratum_specific_rogan_gladen_calibrate,
)
from tests.conftest import (
    eval_all_rubrics,
    eval_traces,
    make_span,
    open_runner_for_spans,
    seed_sme_confusion,
)
from tests.test_sql_boundary_guards import (
    _assert_dedup_cpvo_rule0_and_nfr_sql_guards,
    _assert_unregistered_and_cliff_and_tool_loop_sql_guards,
)
from tests.test_sql_quota_and_counterparty_views import (
    _assert_quota_views_and_procedure_sql_guards,
)


def test_shipped_enterprise_views_value_parity_dedup_and_latest_verdict() -> None:
    """Gold `v_agent_eval_and_finops_summary` matches Python Rogan-Gladen, dedups, and uses latest verdict."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    py_spans: list[OTelGenAISpanRecord] = []
    py_outcomes: dict[str, bool] = {}

    for i in range(22):
        tid = f"trace-f3-{i:03d}"
        r0 = i < 20
        t0 = base + timedelta(seconds=i)
        t1 = t0 + timedelta(milliseconds=500)
        py_spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-f3-{i:03d}",
                start_time=t0,
                end_time=t1,
                latency_ms=500.0,
                status_code="OK" if r0 else "ERROR",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                gcp_apphub_application_id="cal-app",
                model_name="gemini-3.8-flash",
                input_tokens=10_000,
                cached_input_tokens=2_000,
                output_tokens=500,
                thinking_tokens=100,
                rule0_pass=r0,
                outcome_id=f"out-f3-{i:03d}",
                outcome_owner="cal-app",
                source_mode="PDF",
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            )
        )
        if i == 0:
            py_spans.append(
                make_span(
                    tid,
                    "sp-close-000",
                    parent_span_id=f"sp-f3-{i:03d}",
                    span_name="outcome.closed",
                    app_id="cal-app",
                    outcome_id="out-f3-000",
                    outcome_owner="cal-app",
                    model_name="none",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    thinking_tokens=0,
                    latency_ms=0.0,
                    offset_ms=500,
                    attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
                    base_time=base,
                )
            )
        if i < 20:
            py_outcomes[tid] = i < 18

    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    with open_runner_for_spans(py_spans) as runner:
        eval_rows: list[dict[str, object]] = []
        for i in range(20):
            tid = f"trace-f3-{i:03d}"
            passed = i < 18
            eval_rows.append(
                {
                    "eval_id": f"ev-{i}",
                    "eval_run_id": "run-1",
                    "trace_id": tid,
                    "outcome_id": f"out-f3-{i:03d}",
                    "apphub_application_id": "cal-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "reasoning_trace": "rationale",
                    "ordinal_score": 4 if passed else 2,
                    "binary_pass": passed,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 1_000,
                    "judge_output_tokens": 100,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T12:10:00+00:00",
                }
            )
            if i == 0:
                eval_rows.extend(
                    (
                        {
                            "eval_id": "ev-0-dup-older",
                            "eval_run_id": "run-1",
                            "trace_id": tid,
                            "outcome_id": "out-f3-000",
                            "apphub_application_id": "cal-app",
                            "rubric_id": "groundedness_4pt_v1",
                            "judge_model": "gemini-3.8-flash",
                            "sampling_stratum": "CANARY_5PCT",
                            "rule0_pass": True,
                            "reasoning_trace": "older dup",
                            "ordinal_score": 4,
                            "binary_pass": True,
                            "judge_status": "SCORED",
                            "judge_input_tokens": 1_000,
                            "judge_output_tokens": 100,
                            "judge_thinking_tokens": 0,
                            "evaluated_at": "2026-11-21T12:05:00+00:00",
                        },
                        {
                            "eval_id": "ev-0-old-fail",
                            "eval_run_id": "run-0-old",
                            "trace_id": tid,
                            "outcome_id": "out-f3-000",
                            "apphub_application_id": "cal-app",
                            "rubric_id": "groundedness_4pt_v1",
                            "judge_model": "ai_evaluate_agent_managed",
                            "sampling_stratum": "CANARY_5PCT",
                            "rule0_pass": True,
                            "reasoning_trace": "old fail",
                            "ordinal_score": 1,
                            "binary_pass": False,
                            "judge_status": "SCORED",
                            "judge_input_tokens": 1_000,
                            "judge_output_tokens": 100,
                            "judge_thinking_tokens": 0,
                            "evaluated_at": "2026-11-20T12:00:00+00:00",
                        },
                    )
                )
        runner.load_rows("agent_eval.eval_results", eval_rows, append=False)
        seed_sme_confusion(runner, "cal-app", cm)

        bq_row = runner.query(
            "SELECT * FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'cal-app'"
        ).rows[0]
        cpvo_row = runner.query(
            "SELECT verified_outcomes, measured_judge_cost_usd "
            "FROM `agent_eval.v_enterprise_cost_per_verified_outcome` "
            "WHERE apphub_application_id = 'cal-app'"
        ).rows[0]

    py_rg = rogan_gladen_calibrate(observed_pass_rate=0.90, sample_size=20, confusion=cm)
    backend_summary = eval_traces(
        spans=py_spans,
        judge_outcomes=py_outcomes,
        sme_confusion=cm,
    )["cal-app"]

    assert bq_row["calibration_status"] == "CALIBRATED"
    assert int(str(bq_row["judged_samples"])) == 20
    assert float(str(bq_row["calibrated_pass_rate"])) == pytest.approx(
        py_rg.calibrated_pass_rate, abs=1e-12
    )
    assert float(str(bq_row["calibrated_pass_rate"])) == pytest.approx(
        backend_summary.calibrated_pass_rate, abs=1e-12
    )
    assert float(str(bq_row["calibrated_ci_95_lower"])) == pytest.approx(
        py_rg.ci_95_lower, abs=1e-12
    )
    assert float(str(bq_row["calibrated_ci_95_upper"])) == pytest.approx(
        py_rg.ci_95_upper, abs=1e-12
    )
    assert int(str(cpvo_row["verified_outcomes"])) == 1
    expected_deduped_judge = 21 * ((1_000 / 1e6) * 0.15 + (100 / 1e6) * 0.60)
    assert float(str(cpvo_row["measured_judge_cost_usd"])) == pytest.approx(
        expected_deduped_judge, abs=1e-12
    )


def test_unclassified_traffic_type_included_in_total_spend_and_coverage() -> None:
    """An unrecognised `finops_traffic_type` lands in `unclassified_usd`, `total_spend_usd`, and `v_attribution_coverage`."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    span = make_span(
        "tr-unclass",
        "sp-1",
        app_id="app-u",
        outcome_id="out-u",
        outcome_owner="app-u",
        finops_traffic_type=FinOpsTrafficType.UNCLASSIFIED,
        input_tokens=100_000,
        cached_input_tokens=0,
        output_tokens=10_000,
        thinking_tokens=0,
        base_time=base,
    )
    with open_runner_for_spans([span]) as runner:
        raw_row = runner.conn.execute(
            "SELECT attributes FROM obs_traces___AllSpans WHERE trace_id = 'tr-unclass'"
        ).fetchone()
        attrs = json.loads(str(raw_row[0]))
        attrs["bq_eval.traffic_type"] = "not_a_real_type"
        runner.conn.execute(
            "UPDATE obs_traces___AllSpans SET attributes = ? WHERE trace_id = 'tr-unclass'",
            (json.dumps(attrs),),
        )

        expected_cost = (100_000 / 1e6) * 0.15 + (10_000 / 1e6) * 0.60
        trace_row = runner.query("SELECT * FROM `agent_eval.v_trace_sql_evaluation`").rows[0]
        summary_row = runner.query(
            "SELECT * FROM `agent_eval.v_agent_eval_and_finops_summary`"
        ).rows[0]
        cov_row = runner.query("SELECT * FROM `agent_eval.v_attribution_coverage`").rows[0]

    assert float(str(trace_row["unclassified_usd"])) == pytest.approx(expected_cost, abs=1e-12)
    assert float(str(trace_row["trace_span_cost_usd"])) == pytest.approx(expected_cost, abs=1e-12)
    assert float(str(summary_row["unclassified_usd"])) == pytest.approx(expected_cost, abs=1e-12)
    assert float(str(summary_row["total_spend_usd"])) == pytest.approx(
        float(str(trace_row["trace_span_cost_usd"])), abs=1e-12
    )
    assert int(str(cov_row["unclassified_traffic_spans"])) == 1
    assert float(str(cov_row["unclassified_traffic_spend_usd"])) == pytest.approx(
        expected_cost, abs=1e-12
    )


def test_cpvo_uses_production_rates() -> None:
    """Trace-based CPVO denominator uses production-only Rule-0 passes, unaffected by failing eval traces (`A-3`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    prod_spans = [
        make_span(
            f"tr-prod-{i:02d}",
            f"sp-prod-{i:02d}",
            app_id="mix-cpvo-app",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            status_code="OK",
            rule0_pass=True,
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=500,
            thinking_tokens=0,
            offset_s=i,
            base_time=base,
        )
        for i in range(10)
    ]
    eval_fail_spans = [
        make_span(
            f"tr-eval-{i:02d}",
            f"sp-eval-{i:02d}",
            app_id="mix-cpvo-app",
            finops_traffic_type=FinOpsTrafficType.AGENT_GYM_EVAL,
            status_code="ERROR",
            rule0_pass=False,
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=500,
            thinking_tokens=0,
            offset_s=20 + i,
            base_time=base,
        )
        for i in range(10)
    ]
    all_spans = [*prod_spans, *eval_fail_spans]
    outcomes = {f"tr-prod-{i:02d}": True for i in range(10)}

    summary = eval_traces(
        spans=all_spans,
        judge_outcomes=outcomes,
        sme_confusion=cm,
    )["mix-cpvo-app"]

    assert summary.rule0_pass_rate == pytest.approx(0.5, abs=1e-12)
    assert summary.calibrated_pass_rate == pytest.approx(1.0, abs=1e-12)
    assert summary.cost_per_verified_outcome_usd == pytest.approx(
        summary.total_spend_usd / 10.0, abs=1e-12
    )


def test_calibrated_rate_uses_production_strata() -> None:
    """`stratum_population` & `stratum_judge` exclude `agent_gym_eval` and apply stratum-specific `Se_h/Sp_h` (`A-12`, `A-13`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    prod_healthy = [
        make_span(
            f"tr-ph-{i:02d}",
            f"sp-ph-{i:02d}",
            app_id="prod-strata-app",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            latency_ms=400.0,
            offset_s=i,
            base_time=base,
        )
        for i in range(15)
    ]
    prod_tail = [
        make_span(
            f"tr-pt-{i:02d}",
            f"sp-pt-{i:02d}",
            app_id="prod-strata-app",
            finops_traffic_type=FinOpsTrafficType.CONFLICT_ESCALATION,
            latency_ms=8_000.0,
            offset_s=20 + i,
            base_time=base,
        )
        for i in range(5)
    ]
    non_prod_gym_tail = [
        make_span(
            f"tr-gym-{i:02d}",
            f"sp-gym-{i:02d}",
            app_id="prod-strata-app",
            finops_traffic_type=FinOpsTrafficType.AGENT_GYM_EVAL,
            latency_ms=9_500.0,
            offset_s=40 + i,
            base_time=base,
        )
        for i in range(40)
    ]
    all_spans = [*prod_healthy, *prod_tail, *non_prod_gym_tail]
    cm_healthy = ConfusionMatrix(tp=18, fn=2, tn=19, fp=1)
    cm_tail = ConfusionMatrix(tp=16, fn=4, tn=18, fp=2)

    with open_runner_for_spans(all_spans, sla_latency_ms=5_000.0) as runner:
        eval_rows: list[dict[str, object]] = []
        for i in range(15):
            eval_rows.append(
                {
                    "eval_id": f"ev-ph-{i:02d}",
                    "eval_run_id": "run-a12",
                    "trace_id": f"tr-ph-{i:02d}",
                    "apphub_application_id": "prod-strata-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4 if i < 12 else 1,
                    "binary_pass": i < 12,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 500,
                    "judge_output_tokens": 50,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T12:30:00+00:00",
                }
            )
        for i in range(5):
            eval_rows.append(
                {
                    "eval_id": f"ev-pt-{i:02d}",
                    "eval_run_id": "run-a12",
                    "trace_id": f"tr-pt-{i:02d}",
                    "apphub_application_id": "prod-strata-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "ERROR_OR_SLA_TAIL",
                    "rule0_pass": True,
                    "ordinal_score": 4 if i < 1 else 1,
                    "binary_pass": i < 1,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 500,
                    "judge_output_tokens": 50,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T12:30:00+00:00",
                }
            )
        for i in range(40):
            eval_rows.append(
                {
                    "eval_id": f"ev-gym-{i:02d}",
                    "eval_run_id": "run-a12",
                    "trace_id": f"tr-gym-{i:02d}",
                    "apphub_application_id": "prod-strata-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "ERROR_OR_SLA_TAIL",
                    "rule0_pass": True,
                    "ordinal_score": 1,
                    "binary_pass": False,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 500,
                    "judge_output_tokens": 50,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T12:30:00+00:00",
                }
            )
        runner.load_rows("agent_eval.eval_results", eval_rows, append=False)
        seed_sme_confusion(
            runner,
            "prod-strata-app",
            cm_healthy,
            sampling_stratum="HEALTHY_POPULATION",
        )
        seed_sme_confusion(
            runner,
            "prod-strata-app",
            cm_tail,
            sampling_stratum="ERROR_OR_SLA_TAIL",
        )

        row = runner.query(
            "SELECT * FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'prod-strata-app'"
        ).rows[0]

        runner.conn.execute(
            "DELETE FROM agent_eval__sme_labels WHERE sampling_stratum = 'ERROR_OR_SLA_TAIL'"
        )
        missing_tail_sme_row = runner.query(
            "SELECT calibration_status, calibrated_pass_rate "
            "FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'prod-strata-app'"
        ).rows[0]

    py_stratum_rg = stratum_specific_rogan_gladen_calibrate(
        [
            (15, 15, 12, cm_healthy),
            (5, 5, 1, cm_tail),
        ]
    )
    assert int(str(row["total_traces"])) == 60
    assert int(str(row["production_traces"])) == 20
    assert int(str(row["judged_samples"])) == 20
    assert float(str(row["raw_judge_pass_rate"])) == pytest.approx(0.65, abs=1e-12)
    assert row["calibration_status"] == "CALIBRATED"
    assert float(str(row["calibrated_pass_rate"])) == pytest.approx(
        py_stratum_rg.calibrated_pass_rate, abs=1e-12
    )
    assert missing_tail_sme_row["calibration_status"] == "UNCALIBRATED"
    assert missing_tail_sme_row["calibrated_pass_rate"] is None


def test_summary_all_rubrics() -> None:
    """Multi-rubric apps bill 100% of app judge spend on every row and produce 1 conservative governance row (`S-3`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            f"tr-mr-{i:02d}",
            f"sp-mr-{i:02d}",
            app_id="two-rubric-app",
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=500,
            thinking_tokens=0,
            offset_s=i,
            base_time=base,
        )
        for i in range(10)
    ]

    with open_runner_for_spans(spans) as runner:
        eval_rows: list[dict[str, object]] = []
        for i in range(10):
            eval_rows.extend(
                (
                    {
                        "eval_id": f"ev-a-{i:02d}",
                        "eval_run_id": "run-mr",
                        "trace_id": f"tr-mr-{i:02d}",
                        "apphub_application_id": "two-rubric-app",
                        "rubric_id": "groundedness_4pt_v1",
                        "judge_model": "gemini-3.8-flash",
                        "sampling_stratum": "CANARY_5PCT",
                        "rule0_pass": True,
                        "ordinal_score": 4,
                        "binary_pass": True,
                        "judge_status": "SCORED",
                        "judge_input_tokens": 1_000_000,
                        "judge_output_tokens": 100_000,
                        "judge_thinking_tokens": 0,
                        "evaluated_at": (base + timedelta(seconds=30 + i)).isoformat(),
                    },
                    {
                        "eval_id": f"ev-b-{i:02d}",
                        "eval_run_id": "run-mr",
                        "trace_id": f"tr-mr-{i:02d}",
                        "apphub_application_id": "two-rubric-app",
                        "rubric_id": "completeness_4pt_v1",
                        "judge_model": "gemini-3.8-flash",
                        "sampling_stratum": "CANARY_5PCT",
                        "rule0_pass": True,
                        "ordinal_score": 4,
                        "binary_pass": True,
                        "judge_status": "SCORED",
                        "judge_input_tokens": 2_000_000,
                        "judge_output_tokens": 200_000,
                        "judge_thinking_tokens": 0,
                        "evaluated_at": (base + timedelta(seconds=60 + i)).isoformat(),
                    },
                )
            )
        runner.load_rows("agent_eval.eval_results", eval_rows, append=False)
        seed_sme_confusion(
            runner,
            "two-rubric-app",
            ConfusionMatrix(tp=8, fn=0, tn=8, fp=0),
            rubric_id="groundedness_4pt_v1",
        )
        seed_sme_confusion(
            runner,
            "two-rubric-app",
            ConfusionMatrix(tp=4, fn=4, tn=4, fp=4),
            rubric_id="completeness_4pt_v1",
        )

        bq_rows = {
            str(r["rubric_id"]): dict(r)
            for r in runner.query(
                "SELECT * FROM `agent_eval.v_agent_eval_and_finops_summary` "
                "WHERE apphub_application_id = 'two-rubric-app'"
            ).rows
        }
        gov_rows = runner.query(
            "SELECT * FROM `agent_eval.v_app_risk_governance` "
            "WHERE apphub_application_id = 'two-rubric-app'"
        ).rows

    expected_bq_judge = 10 * (0.21 + 0.42)
    assert set(bq_rows) == {"groundedness_4pt_v1", "completeness_4pt_v1"}
    for rub_row in bq_rows.values():
        assert float(str(rub_row["autorater_judge_usd"])) == pytest.approx(
            expected_bq_judge, abs=1e-12
        )

    assert len(gov_rows) == 1
    assert float(str(gov_rows[0]["gwet_ac2"])) < 0.80
    assert int(str(gov_rows[0]["passes_ac2_gate"])) == 0

    sq_all = eval_all_rubrics(
        spans=spans,
        judge_outcomes={f"tr-mr-{i:02d}": True for i in range(10)},
        rubric_outcomes_by_id={"completeness_4pt_v1": {f"tr-mr-{i:02d}": True for i in range(10)}},
        judge_cost_per_call_usd=0.05,
    )
    assert sq_all[("two-rubric-app", "groundedness_4pt_v1")].autorater_judge_usd == pytest.approx(
        1.0, abs=1e-12
    )
    assert sq_all[("two-rubric-app", "completeness_4pt_v1")].autorater_judge_usd == pytest.approx(
        1.0, abs=1e-12
    )


def test_shipped_sql_fail_closed_and_boundary_branches() -> None:
    """Kill surviving SQL guard and boundary mutants across Gold cost, eval, quota, and FinOps views (`T-1 / S-8`)."""
    _assert_unregistered_and_cliff_and_tool_loop_sql_guards()
    _assert_dedup_cpvo_rule0_and_nfr_sql_guards()
    _assert_quota_views_and_procedure_sql_guards()
