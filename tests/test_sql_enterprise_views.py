"""Transpiled enterprise views match the SQLite copy and Python on spend, CPVO, rubrics, and guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bq_eval.core.contract.otel import (
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
)
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.calibration import rogan_gladen_calibrate
from bq_eval.core.engine.sql_eval import (
    evaluate_all_rubrics_in_sqlite,
    evaluate_traces_in_sqlite,
)
from tests.conftest import make_span
from tests.sql_helpers import (
    ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
    ENTERPRISE_OTEL_SPANS_BASE_INSERT,
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
)
from tests.test_sql_boundary_guards import (
    _assert_dedup_cpvo_rule0_and_nfr_sql_guards,
    _assert_unregistered_and_cliff_and_tool_loop_sql_guards,
)
from tests.test_sql_quota_and_counterparty_views import (
    _assert_quota_views_and_procedure_sql_guards,
)


def test_shipped_enterprise_views_value_parity_dedup_and_latest_verdict() -> None:
    """Transpiled `sql/core/views.sql` matches Python Rogan-Gladen & SQLite twin, dedups, and uses latest verdict."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)

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
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            )
        )
        conn.execute(
            ENTERPRISE_OTEL_SPANS_BASE_INSERT,
            (
                tid,
                f"sp-f3-{i:03d}",
                None,
                "invoke_agent",
                "cal-app",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "invoke_agent",
                "gemini-3.8-flash",
                None,
                None,
                "OK" if r0 else "ERROR",
                1 if r0 else 0,
                "production_inference",
                f"out-f3-{i:03d}",
                "cal-app",
                "PDF",
                None,
                None,
                None,
                t0.isoformat(),
                t1.isoformat(),
                t1.isoformat(),
                500.0,
                10_000,
                2_000,
                500,
                100,
                "{}",
            ),
        )
        if i == 0:
            conn.execute(
                ENTERPRISE_OTEL_SPANS_BASE_INSERT,
                (
                    tid,
                    "sp-close-000",
                    f"sp-f3-{i:03d}",
                    "outcome.closed",
                    "cal-app",
                    "svc",
                    "agent",
                    "SPAN_KIND_INTERNAL",
                    "invoke_agent",
                    "none",
                    None,
                    None,
                    "OK",
                    1,
                    "production_inference",
                    "out-f3-000",
                    "cal-app",
                    "PDF",
                    None,
                    None,
                    None,
                    t1.isoformat(),
                    t1.isoformat(),
                    t1.isoformat(),
                    0.0,
                    0,
                    0,
                    0,
                    0,
                    '{"outcome.status": "succeeded", "outcome.verified": "true"}',
                ),
            )
        if i < 20:
            passed = i < 18
            py_outcomes[tid] = passed
            conn.execute(
                ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
                (
                    f"ev-{i}",
                    "run-1",
                    tid,
                    f"out-f3-{i:03d}",
                    "cal-app",
                    "groundedness_4pt_v1",
                    "gemini-3.8-flash",
                    "CANARY_5PCT",
                    1,
                    "rationale",
                    4 if passed else 2,
                    1 if passed else 0,
                    1000,
                    100,
                    0,
                    "2026-11-21T12:10:00+00:00",
                ),
            )
            # Duplicate append of the same (eval_run_id, trace_id, rubric_id) must be deduplicated.
            if i == 0:
                conn.execute(
                    ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
                    (
                        "ev-0-dup-older",
                        "run-1",
                        tid,
                        "out-f3-000",
                        "cal-app",
                        "groundedness_4pt_v1",
                        "gemini-3.8-flash",
                        "CANARY_5PCT",
                        1,
                        "older dup",
                        4,
                        1,
                        1000,
                        100,
                        0,
                        "2026-11-21T12:05:00+00:00",
                    ),
                )
                # Older failing run for out-f3-000 must be superseded by newer passing run-1.
                conn.execute(
                    ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
                    (
                        "ev-0-old-fail",
                        "run-0-old",
                        tid,
                        "out-f3-000",
                        "cal-app",
                        "groundedness_4pt_v1",
                        "ai_evaluate_agent_managed",
                        "CANARY_5PCT",
                        1,
                        "old fail",
                        1,
                        0,
                        1000,
                        100,
                        0,
                        "2026-11-20T12:00:00+00:00",
                    ),
                )

    # Seed SME [TP=87, FN=2, TN=7, FP=4]
    sme_rows = (
        [("cal-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 87
        + [("cal-app", "groundedness_4pt_v1", 1, 4, 1, 2, 0)] * 2
        + [("cal-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)] * 7
        + [("cal-app", "groundedness_4pt_v1", 1, 1, 0, 4, 1)] * 4
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)

    bq_row = conn.execute(
        "SELECT * FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'cal-app'"
    ).fetchone()
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    py_rg = rogan_gladen_calibrate(observed_pass_rate=0.90, sample_size=20, confusion=cm)
    twin_summary = evaluate_traces_in_sqlite(
        spans=py_spans,
        judge_outcomes=py_outcomes,
        sme_confusion=cm,
    )["cal-app"]

    assert bq_row["calibration_status"] == "CALIBRATED"
    assert int(bq_row["judged_samples"]) == 20
    assert float(bq_row["calibrated_pass_rate"]) == pytest.approx(
        py_rg.calibrated_pass_rate, abs=1e-12
    )
    assert float(bq_row["calibrated_pass_rate"]) == pytest.approx(
        twin_summary.calibrated_pass_rate, abs=1e-12
    )
    assert float(bq_row["calibrated_ci_95_lower"]) == pytest.approx(py_rg.ci_95_lower, abs=1e-12)
    assert float(bq_row["calibrated_ci_95_upper"]) == pytest.approx(py_rg.ci_95_upper, abs=1e-12)

    # Out-f3-000 had an older failing verdict in run-0-old and a newer passing verdict in run-1 -> verified!
    # Duplicate ev-0-dup-older in run-1 was deduplicated (21 rows total across 20 outcomes, not 22).
    cpvo_row = conn.execute(
        "SELECT verified_outcomes, measured_judge_cost_usd FROM v_enterprise_cost_per_verified_outcome WHERE apphub_application_id = 'cal-app'"
    ).fetchone()
    assert int(cpvo_row["verified_outcomes"]) == 1
    expected_deduped_judge = 21 * ((1000 / 1e6) * 0.15 + (100 / 1e6) * 0.60)
    assert float(cpvo_row["measured_judge_cost_usd"]) == pytest.approx(
        expected_deduped_judge, abs=1e-12
    )
    conn.close()


def test_unclassified_traffic_type_included_in_total_spend_and_coverage() -> None:
    """An unrecognised `finops_traffic_type` lands in `unclassified_usd`, `total_spend_usd`, and `v_attribution_coverage`."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    conn.execute(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        (
            "tr-unclass",
            "sp-1",
            None,
            "chat",
            "app-u",
            "svc",
            "agent",
            "SPAN_KIND_SERVER",
            "chat",
            "gemini-3.8-flash",
            None,
            None,
            "OK",
            1,
            "not_a_real_type",
            "out-u",
            "app-u",
            None,
            None,
            None,
            None,
            t0,
            t1,
            t1,
            1000.0,
            100_000,
            0,
            10_000,
            0,
            "{}",
        ),
    )
    expected_cost = (100_000 / 1e6) * 0.15 + (10_000 / 1e6) * 0.60
    trace_row = conn.execute("SELECT * FROM v_trace_sql_evaluation").fetchone()
    summary_row = conn.execute("SELECT * FROM v_agent_eval_and_finops_summary").fetchone()
    cov_row = conn.execute("SELECT * FROM v_attribution_coverage").fetchone()

    assert float(trace_row["unclassified_usd"]) == pytest.approx(expected_cost, abs=1e-12)
    assert float(trace_row["trace_span_cost_usd"]) == pytest.approx(expected_cost, abs=1e-12)
    assert float(summary_row["unclassified_usd"]) == pytest.approx(expected_cost, abs=1e-12)
    assert float(summary_row["total_spend_usd"]) == pytest.approx(
        float(trace_row["trace_span_cost_usd"]), abs=1e-12
    )
    assert int(cov_row["unclassified_traffic_spans"]) == 1
    assert float(cov_row["unclassified_traffic_spend_usd"]) == pytest.approx(
        expected_cost, abs=1e-12
    )
    conn.close()


def test_cpvo_uses_production_rates() -> None:
    """Trace-based CPVO denominator uses production-only Rule-0 passes, unaffected by failing eval traces (A-3)."""
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

    sq = evaluate_traces_in_sqlite(
        spans=all_spans,
        judge_outcomes=outcomes,
        sme_confusion=cm,
    )["mix-cpvo-app"]

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    for sp in all_spans:
        insert_enterprise_sqlite_span(conn, sp)
    conn.executemany(
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        [
            (
                f"ev-p-{i:02d}",
                "run-p",
                f"tr-prod-{i:02d}",
                None,
                "mix-cpvo-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "ok",
                4,
                1,
                1000,
                100,
                0,
                (base + timedelta(seconds=50 + i)).isoformat(),
            )
            for i in range(10)
        ],
    )
    sme_rows = (
        [("mix-cpvo-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 87
        + [("mix-cpvo-app", "groundedness_4pt_v1", 1, 4, 1, 2, 0)] * 2
        + [("mix-cpvo-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)] * 7
        + [("mix-cpvo-app", "groundedness_4pt_v1", 1, 1, 0, 4, 1)] * 4
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)
    bq = conn.execute(
        "SELECT * FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'mix-cpvo-app'"
    ).fetchone()
    conn.close()

    # All-traffic rule0_pass_rate is 0.5, but all 10 production traces passed Rule 0.
    assert sq.rule0_pass_rate == pytest.approx(0.5, abs=1e-12)
    assert sq.calibrated_pass_rate == pytest.approx(1.0, abs=1e-12)
    assert sq.cost_per_verified_outcome_usd == pytest.approx(sq.total_spend_usd / 10.0, abs=1e-12)
    assert float(bq["rule0_pass_rate"]) == pytest.approx(0.5, abs=1e-12)
    assert float(bq["calibrated_pass_rate"]) == pytest.approx(1.0, abs=1e-12)
    assert float(bq["cost_per_verified_outcome_usd"]) == pytest.approx(
        float(bq["total_spend_usd"]) / 10.0, abs=1e-12
    )


def test_summary_all_rubrics() -> None:
    """Multi-rubric apps bill 100% of app judge spend on every row and produce 1 conservative governance row (S-3)."""
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

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    gov_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(
            SQL_DIR / "finops" / "capacity_views.sql", "agent_eval.v_app_risk_governance"
        )
    )
    conn.execute(f"CREATE VIEW v_app_risk_governance AS {gov_sql}")
    conn.execute(
        "INSERT INTO app_registry (apphub_application_id, risk_tier) VALUES ('two-rubric-app', 'RL-3b')"
    )
    for sp in spans:
        insert_enterprise_sqlite_span(conn, sp)

    eval_rows = []
    for i in range(10):
        # Rubric A: 1M input + 100K output at flash ($0.15 + $0.06 = $0.21/call -> $2.10)
        eval_rows.append(
            (
                f"ev-a-{i:02d}",
                "run-mr",
                f"tr-mr-{i:02d}",
                None,
                "two-rubric-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "ok",
                4,
                1,
                1_000_000,
                100_000,
                0,
                (base + timedelta(seconds=30 + i)).isoformat(),
            )
        )
        # Rubric B: 2M input + 200K output at flash ($0.30 + $0.12 = $0.42/call -> $4.20)
        eval_rows.append(
            (
                f"ev-b-{i:02d}",
                "run-mr",
                f"tr-mr-{i:02d}",
                None,
                "two-rubric-app",
                "completeness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "ok",
                4,
                1,
                2_000_000,
                200_000,
                0,
                (base + timedelta(seconds=60 + i)).isoformat(),
            )
        )
    conn.executemany(ENTERPRISE_EVAL_RESULTS_BASE_INSERT, eval_rows)

    # Rubric A: high agreement (AC2 == 1.0, passes 0.80 gate); Rubric B: poor agreement (fails 0.80 gate)
    sme_a = [("two-rubric-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 8 + [
        ("two-rubric-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)
    ] * 8
    sme_b = [
        ("two-rubric-app", "completeness_4pt_v1", 1, 4, 1, 4, 1),
        ("two-rubric-app", "completeness_4pt_v1", 1, 4, 1, 1, 0),
        ("two-rubric-app", "completeness_4pt_v1", 1, 1, 0, 4, 1),
        ("two-rubric-app", "completeness_4pt_v1", 1, 1, 0, 1, 0),
    ] * 4
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_a + sme_b)

    bq_rows = {
        str(r["rubric_id"]): dict(r)
        for r in conn.execute(
            "SELECT * FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'two-rubric-app'"
        ).fetchall()
    }
    gov_rows = conn.execute(
        "SELECT * FROM v_app_risk_governance WHERE apphub_application_id = 'two-rubric-app'"
    ).fetchall()
    conn.close()

    # Combined app judge spend is $2.10 + $4.20 = $6.30 on BOTH rubric rows (no shortfall).
    expected_bq_judge = 10 * (0.21 + 0.42)
    assert set(bq_rows) == {"groundedness_4pt_v1", "completeness_4pt_v1"}
    for rub_row in bq_rows.values():
        assert float(rub_row["autorater_judge_usd"]) == pytest.approx(expected_bq_judge, abs=1e-12)

    # Governance view has strictly 1 row per (app, model) using worst-case MIN(gwet_ac2) -> gate fails!
    assert len(gov_rows) == 1
    assert float(gov_rows[0]["gwet_ac2"]) < 0.80
    assert int(gov_rows[0]["passes_ac2_gate"]) == 0

    # SQLite twin matches app-level combined judge spend across both rubrics.
    sq_all = evaluate_all_rubrics_in_sqlite(
        spans=spans,
        judge_outcomes={f"tr-mr-{i:02d}": True for i in range(10)},
        extra_rubric_outcomes={"completeness_4pt_v1": {f"tr-mr-{i:02d}": True for i in range(10)}},
        judge_cost_per_call_usd=0.05,
    )
    assert sq_all[("two-rubric-app", "groundedness_4pt_v1")].autorater_judge_usd == pytest.approx(
        1.0, abs=1e-12
    )
    assert sq_all[("two-rubric-app", "completeness_4pt_v1")].autorater_judge_usd == pytest.approx(
        1.0, abs=1e-12
    )


def test_shipped_sql_fail_closed_and_boundary_branches() -> None:
    """Kill surviving SQL guard and boundary mutants across core, adapter, quota, and FinOps views (T-1 / S-8)."""
    _assert_unregistered_and_cliff_and_tool_loop_sql_guards()
    _assert_dedup_cpvo_rule0_and_nfr_sql_guards()
    _assert_quota_views_and_procedure_sql_guards()
