"""Transpiled enterprise views match the SQLite copy and Python on spend, quota and verdict rules."""

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
    build_ai_evaluate_agent_sql,
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
    _parse_bigquery_statements,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
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


def test_daily_quota_guard_no_config_fanout_and_engine_b_quota_trip() -> None:
    """`v_step2_daily_quota_status` ignores extra config rows, retains billed retries, and trips on Engine B."""
    quota_path = SQL_DIR / "core" / "step3_daily_quota_guard.sql"
    quota_sql = quota_path.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE PROCEDURE `agent_eval.run_daily_quota_guarded_judge`" in quota_sql
    assert "app_cumulative_tokens <= app_remaining_today" in quota_sql
    assert "global_cumulative_tokens <= global_remaining_today" in quota_sql
    assert "BEGIN TRANSACTION;" in quota_sql
    assert "FAILED_BILLED" in quota_sql

    engine_b_persist_sql = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        canary_sample_pct=10,
        persist_run_id="run_2026_11_21_bqaa_managed",
    )
    _parse_bigquery_statements(engine_b_persist_sql, "build_ai_evaluate_agent_sql(persist)")
    assert "DIV(CHAR_LENGTH(TO_JSON_STRING(eval_out.trace)), 4)" in engine_b_persist_sql
    assert "DIV(CHAR_LENGTH(COALESCE(metric_row.explanation, '')), 4)" in engine_b_persist_sql
    assert "AND metric_row.metric_name = existing.rubric_id" in engine_b_persist_sql
    assert "WHERE metric_row.score IS NOT NULL" in engine_b_persist_sql
    assert "(ordinal_score >= 3) AS binary_pass" in engine_b_persist_sql

    conn = _new_sqlite_bq_conn(current_date_utc="2026-11-21")
    conn.executescript(
        """
        CREATE TABLE eval_daily_quota_config (
            quota_name TEXT,
            apphub_application_id TEXT,
            daily_judge_token_cap INTEGER,
            per_app_default_token_cap INTEGER,
            max_output_tokens INTEGER,
            thinking_budget_tokens INTEGER,
            updated_at TEXT
        );
        CREATE TABLE eval_daily_quota_reservations (
            reservation_id TEXT,
            quota_date_utc TEXT,
            apphub_application_id TEXT,
            rubric_id TEXT,
            reserved_tokens INTEGER,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE eval_results (
            eval_id TEXT,
            eval_run_id TEXT,
            trace_id TEXT,
            apphub_application_id TEXT,
            rubric_id TEXT,
            judge_model TEXT,
            judge_input_tokens INTEGER,
            judge_output_tokens INTEGER,
            judge_thinking_tokens INTEGER,
            evaluated_at TEXT
        );
        """
    )
    # Two config rows must not fan out tokens_used_today via Cartesian join.
    conn.executemany(
        "INSERT INTO eval_daily_quota_config VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "default_step2_daily_cap",
                "*",
                2_000_000,
                500_000,
                2048,
                1024,
                "2026-11-21T00:00:00+00:00",
            ),
            (
                "secondary_profile",
                "app-a",
                1_500_000,
                500_000,
                2048,
                1024,
                "2026-11-21T00:00:00+00:00",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO eval_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "e1",
                "run-a",
                "t1",
                "app-a",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                700_000,
                100_000,
                0,
                "2026-11-21T08:00:00+00:00",
            ),
            # Non-deduped burn accounting: billed retries still count against daily quota.
            (
                "e1-retry",
                "run-a",
                "t1",
                "app-a",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                200_000,
                0,
                0,
                "2026-11-21T08:05:00+00:00",
            ),
            # Engine B managed row uses 500k tokens -> total 1.5M < 2.0M
            (
                "e2",
                "run-b",
                "t2",
                "app-b",
                "hallucination_v1",
                "ai_evaluate_agent_managed",
                450_000,
                50_000,
                0,
                "2026-11-21T09:00:00+00:00",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO eval_daily_quota_reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "res-1",
            "2026-11-21",
            "app-b",
            "hallucination_v1",
            100_000,
            "RESERVED",
            "2026-11-21T09:05:00+00:00",
            "2026-11-21T09:05:00+00:00",
        ),
    )
    view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(quota_path, "agent_eval.v_step2_daily_quota_status")
    )
    conn.execute(f"CREATE VIEW v_step2_daily_quota_status AS {view_sql}")
    row = conn.execute("SELECT * FROM v_step2_daily_quota_status").fetchone()
    assert int(row["daily_judge_token_cap"]) == 2_000_000
    assert int(row["tokens_used_today"]) == 1_500_000
    assert int(row["tokens_reserved_today"]) == 100_000
    assert int(row["tokens_remaining_today"]) == 400_000
    assert bool(row["quota_available"]) is True

    # Add another 500k Engine B metric row -> 2.0M used + 100k reserved trips the 2M cap
    conn.execute(
        "INSERT INTO eval_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "e3",
            "run-b",
            "t2",
            "app-b",
            "safety_v1",
            "ai_evaluate_agent_managed",
            450_000,
            50_000,
            0,
            "2026-11-21T10:00:00+00:00",
        ),
    )
    tripped = conn.execute("SELECT * FROM v_step2_daily_quota_status").fetchone()
    assert int(tripped["tokens_used_today"]) == 2_000_000
    assert int(tripped["tokens_remaining_today"]) == 0
    assert bool(tripped["quota_available"]) is False
    conn.close()


def test_deterministic_traffic_type_unjudged_rubric_and_multi_rubric_filter() -> None:
    """Explicit `finops_traffic_type` priority, `'groundedness_4pt_v1'` unjudged label, and multi-rubric parity."""
    views_text = (SQL_DIR / "core" / "views.sql").read_text(encoding="utf-8")
    assert "ANY_VALUE(w.finops_traffic_type)" not in views_text
    assert "MAX(w.finops_traffic_type) AS primary_traffic_type" not in views_text
    assert "WHEN 'production_inference' THEN 1" in views_text

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    conn.executemany(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        [
            (
                "tr-unjudged",
                "sp-u1",
                None,
                "invoke_agent",
                "unjudged-parity-app",
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
                "unjudged-parity-app",
                None,
                None,
                None,
                None,
                t0,
                t1,
                t1,
                500.0,
                4_000,
                1_000,
                200,
                0,
                "{}",
            ),
            (
                "tr-multi",
                "sp-m1",
                None,
                "invoke_agent",
                "multi-rubric-app",
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
                "multi-rubric-app",
                None,
                None,
                None,
                None,
                t0,
                t1,
                t1,
                500.0,
                4_000,
                1_000,
                200,
                0,
                "{}",
            ),
        ],
    )
    conn.executemany(
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        [
            (
                "ev-m-g",
                "run-1",
                "tr-multi",
                None,
                "multi-rubric-app",
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
                t1,
            ),
            (
                "ev-m-e",
                "run-1",
                "tr-multi",
                None,
                "multi-rubric-app",
                "escalation_policy_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "failed escalation",
                1,
                0,
                500,
                50,
                0,
                t1,
            ),
        ],
    )

    bq_unjudged = conn.execute(
        "SELECT rubric_id, calibration_status FROM v_agent_eval_and_finops_summary "
        "WHERE apphub_application_id = 'unjudged-parity-app'"
    ).fetchone()
    bq_multi_esc = conn.execute(
        "SELECT raw_judge_pass_rate FROM v_agent_eval_and_finops_summary "
        "WHERE apphub_application_id = 'multi-rubric-app' AND rubric_id = 'escalation_policy_4pt_v1'"
    ).fetchone()
    conn.close()

    base_dt = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    py_spans = [
        OTelGenAISpanRecord(
            trace_id="tr-unjudged",
            span_id="sp-u1",
            start_time=base_dt,
            end_time=base_dt + timedelta(seconds=1),
            latency_ms=500.0,
            gcp_apphub_application_id="unjudged-parity-app",
            input_tokens=4_000,
            cached_input_tokens=1_000,
            output_tokens=200,
        ),
        OTelGenAISpanRecord(
            trace_id="tr-multi",
            span_id="sp-m1",
            start_time=base_dt,
            end_time=base_dt + timedelta(seconds=1),
            latency_ms=500.0,
            gcp_apphub_application_id="multi-rubric-app",
            input_tokens=4_000,
            cached_input_tokens=1_000,
            output_tokens=200,
        ),
    ]
    twin_esc = evaluate_traces_in_sqlite(
        spans=py_spans,
        judge_outcomes={"tr-multi": False},
        extra_rubric_outcomes={"groundedness_4pt_v1": {"tr-multi": True}},
        rubric_id="escalation_policy_4pt_v1",
    )
    twin_unjudged = evaluate_traces_in_sqlite(spans=py_spans[:1])["unjudged-parity-app"]

    assert bq_unjudged["rubric_id"] == twin_unjudged.rubric_id == "groundedness_4pt_v1"
    assert bq_unjudged["calibration_status"] == twin_unjudged.calibration_status == "UNJUDGED"
    assert float(bq_multi_esc["raw_judge_pass_rate"]) == pytest.approx(0.0)
    assert twin_esc["multi-rubric-app"].raw_judge_pass_rate == pytest.approx(0.0)


def test_counterparty_classification_uses_explicit_flag_not_app_id_substring() -> None:
    """An internal app named `external-facing-chat` stays in CPVO; explicit flags route to `v_counterparty_spend`."""
    views_text = (SQL_DIR / "core" / "views.sql").read_text(encoding="utf-8")
    assert "LIKE '%external%'" not in views_text

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    conn.executemany(
        "INSERT INTO app_registry (apphub_application_id, business_unit, sla_latency_ms, cost_center, is_external_counterparty) VALUES (?, ?, ?, ?, ?)",
        [
            ("external-facing-chat", "retail_banking", 3000.0, "CC-CHAT", 0),
            ("partner-carrier-hub", "external_b2b", 5000.0, "CC-EXT", 1),
        ],
    )
    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    for app_id, out_id, attrs in (
        ("external-facing-chat", "out-chat-1", "{}"),
        ("partner-carrier-hub", "out-ext-1", "{}"),
        ("attr-marked-partner", "out-ext-2", '{"apphub.external": "true"}'),
    ):
        conn.executemany(
            ENTERPRISE_OTEL_SPANS_BASE_INSERT,
            [
                (
                    f"tr-{out_id}",
                    f"sp-{out_id}",
                    None,
                    "invoke_agent",
                    app_id,
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
                    out_id,
                    app_id,
                    "A2A",
                    None,
                    None,
                    None,
                    t0,
                    t1,
                    t1,
                    500.0,
                    10_000,
                    0,
                    500,
                    0,
                    attrs,
                ),
                (
                    f"tr-{out_id}",
                    f"sp-close-{out_id}",
                    f"sp-{out_id}",
                    "outcome.closed",
                    app_id,
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
                    out_id,
                    app_id,
                    "A2A",
                    None,
                    None,
                    None,
                    t1,
                    t1,
                    t1,
                    0.0,
                    0,
                    0,
                    0,
                    0,
                    '{"outcome.status": "succeeded", "outcome.verified": "true"}',
                ),
            ],
        )

    cpvo_apps = {
        str(r["apphub_application_id"])
        for r in conn.execute(
            "SELECT apphub_application_id FROM v_enterprise_cost_per_verified_outcome"
        ).fetchall()
    }
    counterparty_apps = {
        str(r["apphub_application_id"])
        for r in conn.execute("SELECT apphub_application_id FROM v_counterparty_spend").fetchall()
    }
    conn.close()

    assert "external-facing-chat" in cpvo_apps
    assert "external-facing-chat" not in counterparty_apps
    assert counterparty_apps == {"partner-carrier-hub", "attr-marked-partner"}


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


def _assert_unregistered_and_cliff_and_tool_loop_sql_guards() -> None:
    """Guard `'unregistered_app'` fallback, exact 200K boundary, 8-vs-9 tool limit, and BQAA retry dedup."""
    import json

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    adapter_path = SQL_DIR / "core" / "bqaa_adapter_views.sql"
    conn.execute(
        """
        CREATE TABLE agent_events (
            event_id TEXT, timestamp TEXT, event_type TEXT, agent TEXT,
            session_id TEXT, invocation_id TEXT, trace_id TEXT, span_id TEXT,
            parent_span_id TEXT, content TEXT, attributes TEXT, latency_ms TEXT,
            status TEXT, error_message TEXT
        )
        """
    )
    conn.execute(
        "CREATE TABLE agent_events_eval_turn (trace_id TEXT, turn_id TEXT, has_tool_loop INTEGER)"
    )
    for view_name in (
        "agent_eval.v_bqaa_otel_spans",
        "agent_eval.v_bqaa_trace_payloads",
        "agent_eval.v_bqaa_eval_turn_filtered",
    ):
        short = view_name.split(".", maxsplit=1)[1]
        conn.execute(
            f"CREATE VIEW {short} AS "
            f"{_transpile_bq_view_to_sqlite(_extract_view_sql_text(adapter_path, view_name))}"
        )

    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    # 1. Unregistered span with service_name set, app_id/outcome_owner NULL -> 'unregistered_app'
    # 2. Exact 200,000 input tokens -> NOT cliff ($0.036); 200,001 -> cliff
    # 3. 8 OK execute_tool spans -> PASS; 9 OK execute_tool spans -> FAIL
    span_rows: list[tuple[object, ...]] = [
        (
            "tr-unreg-1",
            "sp-unreg-1",
            None,
            "chat",
            None,
            "self-reported-svc",
            "agent",
            "SPAN_KIND_SERVER",
            "chat",
            "gemini-3.8-flash",
            None,
            None,
            "OK",
            1,
            "production_inference",
            None,
            None,
            None,
            None,
            None,
            None,
            t0,
            t1,
            t1,
            100.0,
            200_000,
            0,
            10_000,
            0,
            "{}",
        ),
        (
            "tr-cliff-200001",
            "sp-cliff-200001",
            None,
            "chat",
            "cliff-app",
            "svc",
            "agent",
            "SPAN_KIND_SERVER",
            "chat",
            "gemini-3.8-flash",
            None,
            None,
            "OK",
            1,
            "production_inference",
            None,
            "cliff-app",
            None,
            None,
            None,
            None,
            t0,
            t1,
            t1,
            100.0,
            200_001,
            0,
            10_000,
            0,
            "{}",
        ),
    ]
    for count_tag, n_tools in (("tr-tools-8", 8), ("tr-tools-9", 9)):
        for i in range(n_tools):
            span_rows.append(
                (
                    count_tag,
                    f"sp-{count_tag}-{i}",
                    None if i == 0 else f"sp-{count_tag}-0",
                    "execute_tool",
                    "tool-app",
                    "svc",
                    "agent",
                    "SPAN_KIND_INTERNAL",
                    "execute_tool",
                    "none",
                    "lookup",
                    "SUCCESS",
                    "OK",
                    1,
                    "production_inference",
                    None,
                    "tool-app",
                    None,
                    None,
                    None,
                    None,
                    t0,
                    t1,
                    t1,
                    10.0,
                    0,
                    0,
                    0,
                    0,
                    "{}",
                )
            )
    conn.executemany(ENTERPRISE_OTEL_SPANS_BASE_INSERT, span_rows)

    # BQAA retry dedup (same event_id, older 1K tokens vs newer 5K tokens) + unregistered fallback
    conn.executemany(
        "INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "ev-retry-1",
                "2026-11-21T12:00:01+00:00",
                "LLM_RESPONSE",
                "rogue_agent",
                "s1",
                "inv1",
                "tr-bqaa-dedup",
                "sp-d1",
                None,
                "{}",
                json.dumps(
                    {
                        "adk": {"app_name": "self_reported_adk"},
                        "model": "gemini-3.8-flash",
                        "usage_metadata": {
                            "prompt_token_count": 1_000,
                            "candidates_token_count": 50,
                        },
                    }
                ),
                '{"total_ms": 100}',
                "OK",
                None,
            ),
            (
                "ev-retry-1",
                "2026-11-21T12:00:05+00:00",
                "LLM_RESPONSE",
                "rogue_agent",
                "s1",
                "inv1",
                "tr-bqaa-dedup",
                "sp-d1",
                None,
                "{}",
                json.dumps(
                    {
                        "adk": {"app_name": "self_reported_adk"},
                        "model": "gemini-3.8-flash",
                        "usage_metadata": {
                            "prompt_token_count": 5_000,
                            "candidates_token_count": 50,
                        },
                    }
                ),
                '{"total_ms": 120}',
                "OK",
                None,
            ),
        ],
    )
    conn.execute("INSERT INTO agent_events_eval_turn VALUES ('tr-bqaa-dedup', 't1', 0)")

    # Check empty-catalog 200K boundary + unregistered fallback
    wf_200k = conn.execute(
        "SELECT apphub_application_id, is_long_context_cliff, span_cost_usd "
        "FROM v_trace_waterfall WHERE trace_id = 'tr-unreg-1'"
    ).fetchone()
    s1_wf_200k = conn.execute(
        "SELECT apphub_application_id, is_long_context_cliff, span_cost_usd "
        "FROM v_step1_trace_waterfall WHERE trace_id = 'tr-unreg-1'"
    ).fetchone()
    wf_200001 = conn.execute(
        "SELECT is_long_context_cliff FROM v_trace_waterfall WHERE trace_id = 'tr-cliff-200001'"
    ).fetchone()
    eval_unreg = conn.execute(
        "SELECT apphub_application_id FROM v_trace_sql_evaluation WHERE trace_id = 'tr-unreg-1'"
    ).fetchone()
    assert wf_200k["apphub_application_id"] == "unregistered_app"
    assert s1_wf_200k["apphub_application_id"] == "unregistered_app"
    assert eval_unreg["apphub_application_id"] == "unregistered_app"
    assert int(wf_200k["is_long_context_cliff"]) == 0
    assert int(s1_wf_200k["is_long_context_cliff"]) == 0
    assert float(wf_200k["span_cost_usd"]) == pytest.approx(0.036, abs=1e-12)
    assert float(s1_wf_200k["span_cost_usd"]) == pytest.approx(0.036, abs=1e-12)
    assert int(wf_200001["is_long_context_cliff"]) == 1

    # Also test populated catalog at exact 200K boundary (kills m.long_context_threshold_tokens >= mutant)
    conn.execute(
        """
        INSERT INTO model_pricing_catalog (
            model_name, provider, lifecycle_track, effective_start, effective_end,
            input_usd_per_1m, cached_input_usd_per_1m, cache_write_5m_usd_per_1m,
            cache_write_1h_usd_per_1m, output_usd_per_1m, long_context_threshold_tokens,
            long_context_input_usd_per_1m, long_context_cached_usd_per_1m,
            long_context_output_usd_per_1m, burndown_uncached_weight,
            burndown_cached_weight, burndown_cache_write_5m_weight,
            burndown_cache_write_1h_weight, burndown_output_weight,
            burndown_long_uncached_weight, burndown_long_cached_weight,
            burndown_long_output_weight, priority_paygo_multiplier, flex_paygo_multiplier
        ) VALUES (
            'gemini-3.8-flash', 'google', 'long_term_ga',
            '2026-01-01T00:00:00+00:00', '2099-12-31T23:59:59+00:00',
            0.15, 0.015, 0.15, 0.15, 0.60, 200000, 0.30, 0.030, 2.25,
            1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5
        )
        """
    )
    wf_200k_cat = conn.execute(
        "SELECT is_long_context_cliff, span_cost_usd FROM v_trace_waterfall WHERE trace_id = 'tr-unreg-1'"
    ).fetchone()
    assert int(wf_200k_cat["is_long_context_cliff"]) == 0
    assert float(wf_200k_cat["span_cost_usd"]) == pytest.approx(0.036, abs=1e-12)

    s1_t8 = conn.execute(
        "SELECT step1_pass FROM v_step1_trace_eval WHERE trace_id = 'tr-tools-8'"
    ).fetchone()
    s1_t9 = conn.execute(
        "SELECT step1_pass FROM v_step1_trace_eval WHERE trace_id = 'tr-tools-9'"
    ).fetchone()
    ent_t8 = conn.execute(
        "SELECT rule0_pass FROM v_trace_sql_evaluation WHERE trace_id = 'tr-tools-8'"
    ).fetchone()
    ent_t9 = conn.execute(
        "SELECT rule0_pass FROM v_trace_sql_evaluation WHERE trace_id = 'tr-tools-9'"
    ).fetchone()
    assert int(s1_t8["step1_pass"]) == 1 and int(ent_t8["rule0_pass"]) == 1
    assert int(s1_t9["step1_pass"]) == 0 and int(ent_t9["rule0_pass"]) == 0

    bqaa_spans = conn.execute("SELECT input_tokens FROM v_bqaa_otel_spans").fetchall()
    bqaa_pl = conn.execute("SELECT apphub_application_id FROM v_bqaa_trace_payloads").fetchone()
    bqaa_turn = conn.execute(
        "SELECT apphub_application_id FROM v_bqaa_eval_turn_filtered"
    ).fetchone()
    conn.close()
    assert len(bqaa_spans) == 1
    assert int(bqaa_spans[0]["input_tokens"]) == 5_000
    assert bqaa_pl["apphub_application_id"] == "unregistered_app"
    assert bqaa_turn["apphub_application_id"] == "unregistered_app"


def _assert_dedup_cpvo_rule0_and_nfr_sql_guards() -> None:
    """Guard eval dedup DESC order, unverified CPVO denominator, Rule-0 filters, NFR gate fail, and lane default."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    step3_path = SQL_DIR / "core" / "step3_saved_results.sql"
    cap_path = SQL_DIR / "finops" / "capacity_views.sql"
    for path, vname in (
        (step3_path, "agent_eval.v_step3_paired_run_comparison"),
        (step3_path, "agent_eval.v_step3_policy_nfr_gate"),
        (cap_path, "agent_eval.v_focus_three_pool_hourly_chargeback"),
    ):
        short = vname.split(".", maxsplit=1)[1]
        conn.execute(
            f"CREATE VIEW {short} AS "
            f"{_transpile_bq_view_to_sqlite(_extract_view_sql_text(path, vname))}"
        )

    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    # 10 Rule-0 passing traces + 2 Rule-0 failing traces; 2 outcomes (1 verified, 1 unverified)
    for i in range(12):
        r0 = i < 10
        oid = f"out-g-{i:02d}" if i < 2 else None
        insert_enterprise_sqlite_span(
            conn,
            make_span(
                f"trace-g-{i:02d}",
                f"span-g-{i:02d}",
                app_id="guard-app",
                outcome_id=oid,
                outcome_owner="guard-app" if oid else None,
                status_code="OK" if r0 else "ERROR",
                rule0_pass=r0,
                input_tokens=100_000,
                cached_input_tokens=0,
                output_tokens=10_000,
                thinking_tokens=0,
                latency_ms=500.0,
                offset_s=i * 2,
                base_time=base,
            ),
        )
        if oid is not None:
            insert_enterprise_sqlite_span(
                conn,
                make_span(
                    f"trace-g-{i:02d}",
                    f"span-g-close-{i:02d}",
                    parent_span_id=f"span-g-{i:02d}",
                    span_name="outcome.closed",
                    app_id="guard-app",
                    outcome_id=oid,
                    outcome_owner="guard-app",
                    model_name="none",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    thinking_tokens=0,
                    latency_ms=0.0,
                    offset_s=i * 2 + 1,
                    attributes={
                        "outcome.status": "succeeded" if i == 0 else "escalated",
                        "outcome.verified": "true" if i == 0 else "false",
                    },
                    base_time=base,
                ),
            )

    # Unknown traffic type -> lane defaults to 'prod' (kills ELSE 'eval' mutant)
    conn.execute(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        (
            "tr-unk-lane",
            "sp-unk-lane",
            None,
            "chat",
            "lane-app",
            "svc",
            "agent",
            "SPAN_KIND_SERVER",
            "chat",
            "gemini-3.8-flash",
            None,
            None,
            "OK",
            1,
            "custom_unknown_lane",
            None,
            "lane-app",
            None,
            None,
            None,
            None,
            base.isoformat(),
            (base + timedelta(seconds=1)).isoformat(),
            (base + timedelta(seconds=1)).isoformat(),
            100.0,
            10_000,
            0,
            500,
            0,
            "{}",
        ),
    )

    # eval_results:
    # - tr-g-00 has older FAIL (100K tokens) and newer PASS (1K tokens) in SAME run-1 -> DESC must win!
    # - tr-g-10, tr-g-11 have rule0_pass=0, binary_pass=0 -> MUST be excluded from judged_samples/raw rate!
    # - run-2 flips tr-g-01 from PASS -> FAIL on unamended clause -> passes_nfr_gate MUST be 0!
    eval_rows: list[tuple[object, ...]] = [
        (
            "ev-g-00-old",
            "run-1",
            "trace-g-00",
            "out-g-00",
            "guard-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            "CANARY_5PCT",
            1,
            "old fail",
            1,
            0,
            100_000,
            10_000,
            0,
            "2026-11-21T12:01:00+00:00",
            "sha-v1",
            None,
            "CL-1",
        ),
        (
            "ev-g-00-new",
            "run-1",
            "trace-g-00",
            "out-g-00",
            "guard-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            "CANARY_5PCT",
            1,
            "new pass",
            4,
            1,
            1_000,
            100,
            0,
            "2026-11-21T12:05:00+00:00",
            "sha-v1",
            None,
            "CL-1",
        ),
    ]
    for i in range(1, 10):
        eval_rows.append(
            (
                f"ev-g-{i:02d}",
                "run-1",
                f"trace-g-{i:02d}",
                f"out-g-{i:02d}" if i < 2 else None,
                "guard-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "pass",
                4,
                1,
                1_000,
                100,
                0,
                "2026-11-21T12:05:00+00:00",
                "sha-v1",
                None,
                "CL-1",
            )
        )
    for i in (10, 11):
        eval_rows.append(
            (
                f"ev-g-r0f-{i}",
                "run-1",
                f"trace-g-{i:02d}",
                None,
                "guard-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "ERROR_OR_SLA_TAIL",
                0,
                "r0 fail",
                1,
                0,
                1_000,
                100,
                0,
                "2026-11-21T12:05:00+00:00",
                "sha-v1",
                None,
                "CL-1",
            )
        )
    # Candidate run-2 flips trace-g-01 from PASS -> FAIL on unamended clause CL-1
    eval_rows.append(
        (
            "ev-g-01-flip",
            "run-2",
            "trace-g-01",
            "out-g-01",
            "guard-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            "CANARY_5PCT",
            1,
            "regressed",
            1,
            0,
            1_000,
            100,
            0,
            "2026-11-21T13:00:00+00:00",
            "sha-v2",
            "CL-9",
            "CL-1",
        )
    )
    conn.executemany(
        "INSERT INTO eval_results ("
        "eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id, "
        "judge_model, sampling_stratum, rule0_pass, reasoning_trace, ordinal_score, "
        "binary_pass, judge_input_tokens, judge_output_tokens, judge_thinking_tokens, "
        "evaluated_at, policy_sha256, amended_clause_ids, target_clause_id"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        eval_rows,
    )
    # SME labels: valid rule0_pass=1 (perfect 10/10) + corrupted rule0_pass=0 (must be ignored!)
    sme_rows = (
        [("guard-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 10
        + [("guard-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)] * 10
        + [("guard-app", "groundedness_4pt_v1", 0, 4, 1, 1, 0)] * 25
        + [("guard-app", "groundedness_4pt_v1", 0, 1, 0, 4, 1)] * 25
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)

    cpvo = conn.execute(
        "SELECT total_outcomes, verified_outcomes, total_ai_cost_usd, cost_per_verified_outcome_ai_usd "
        "FROM v_enterprise_cost_per_verified_outcome WHERE apphub_application_id = 'guard-app'"
    ).fetchone()
    summ = conn.execute(
        "SELECT judged_samples, raw_judge_pass_rate, calibrated_pass_rate, gwet_ac2 "
        "FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'guard-app'"
    ).fetchone()
    nfr = conn.execute(
        "SELECT unamended_negative_flips, passes_nfr_gate FROM v_step3_policy_nfr_gate "
        "WHERE apphub_application_id = 'guard-app'"
    ).fetchone()
    lane_row = conn.execute(
        "SELECT traffic_lane FROM v_focus_three_pool_hourly_chargeback "
        "WHERE apphub_application_id = 'lane-app'"
    ).fetchone()
    conn.close()

    # Only out-g-00 is verified (tr-g-00 latest verdict=PASS); out-g-01 is unverified -> denom = 1
    assert int(cpvo["total_outcomes"]) == 2
    assert int(cpvo["verified_outcomes"]) == 1
    assert float(cpvo["cost_per_verified_outcome_ai_usd"]) == pytest.approx(
        float(cpvo["total_ai_cost_usd"]), abs=1e-12
    )
    # 10 rule0-passing judged (tr-g-00 latest=PASS, tr-g-01 latest in run-2=FAIL -> 9/10=0.90)
    assert int(summ["judged_samples"]) == 10
    assert float(summ["raw_judge_pass_rate"]) == pytest.approx(0.90, abs=1e-12)
    # sme_raw ignored rule0_pass=0 corruption -> TPR=TNR=1.0 -> calibrated == raw == 0.90 and AC2 == 1.0
    assert float(summ["calibrated_pass_rate"]) == pytest.approx(0.90, abs=1e-12)
    assert float(summ["gwet_ac2"]) == pytest.approx(1.0, abs=1e-12)
    assert int(nfr["unamended_negative_flips"]) == 1
    assert int(nfr["passes_nfr_gate"]) == 0
    assert lane_row["traffic_lane"] == "prod"


def _assert_quota_views_and_procedure_sql_guards() -> None:
    """Guard `FAILED_BILLED`, exact cap boundary, and transpiled procedure candidate-selection SQL."""
    quota_path = SQL_DIR / "core" / "step3_daily_quota_guard.sql"
    quota_text = quota_path.read_text(encoding="utf-8")
    assert ") > COALESCE(" in quota_text
    assert "EXCEPTION WHEN ERROR THEN" in quota_text
    assert "SET status = 'FAILED_BILLED'" in quota_text

    conn = _new_sqlite_bq_conn(
        current_date_utc="2026-11-21",
        current_timestamp_utc="2026-11-21T12:00:00+00:00",
    )
    conn.executescript(
        """
        CREATE TABLE eval_daily_quota_config (
            quota_name TEXT, apphub_application_id TEXT, daily_judge_token_cap INTEGER,
            per_app_default_token_cap INTEGER, max_output_tokens INTEGER,
            thinking_budget_tokens INTEGER, updated_at TEXT
        );
        CREATE TABLE eval_daily_quota_reservations (
            reservation_id TEXT, quota_date_utc TEXT, apphub_application_id TEXT,
            rubric_id TEXT, reserved_tokens INTEGER, status TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE eval_results (
            eval_id TEXT, eval_run_id TEXT, trace_id TEXT, apphub_application_id TEXT,
            rubric_id TEXT, judge_model TEXT, judge_input_tokens INTEGER,
            judge_output_tokens INTEGER, judge_thinking_tokens INTEGER, evaluated_at TEXT
        );
        CREATE TABLE v_step2_trace_rubric_prompts (
            trace_id TEXT, outcome_id TEXT, apphub_application_id TEXT,
            rubric_id TEXT, passing_score INTEGER, step1_pass INTEGER,
            trace_start_time TEXT, prompt TEXT
        );
        """
    )
    for vname in (
        "agent_eval.v_step2_daily_quota_status",
        "agent_eval.v_step2_app_daily_quota_status",
    ):
        short = vname.split(".", maxsplit=1)[1]
        conn.execute(
            f"CREATE VIEW {short} AS "
            f"{_transpile_bq_view_to_sqlite(_extract_view_sql_text(quota_path, vname))}"
        )

    conn.execute(
        "INSERT INTO eval_daily_quota_config VALUES ('default_step2_daily_cap','*',10000,6000,1000,0,'2026-11-21T00:00:00+00:00')"
    )
    # FAILED_BILLED must count (4000); RELEASED/COMMITTED must NOT count (99999)
    conn.executemany(
        "INSERT INTO eval_daily_quota_reservations VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "r-fb",
                "2026-11-21",
                "app-q",
                "groundedness_4pt_v1",
                4000,
                "FAILED_BILLED",
                "2026-11-21T08:00:00+00:00",
                "2026-11-21T08:01:00+00:00",
            ),
            (
                "r-rel",
                "2026-11-21",
                "app-q",
                "groundedness_4pt_v1",
                99999,
                "RELEASED",
                "2026-11-21T08:00:00+00:00",
                "2026-11-21T08:01:00+00:00",
            ),
            (
                "r-com",
                "2026-11-21",
                "app-q",
                "groundedness_4pt_v1",
                99999,
                "COMMITTED",
                "2026-11-21T08:00:00+00:00",
                "2026-11-21T08:01:00+00:00",
            ),
        ],
    )
    # 5999 used + 4000 FAILED_BILLED = 9999 (< 10000 cap -> available=1); app-q used=2000 + 4000 = 6000 (== 6000 cap -> app_quota_available=0!)
    conn.executemany(
        "INSERT INTO eval_results VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "e-q1",
                "r1",
                "t-q1",
                "app-q",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                2000,
                0,
                0,
                "2026-11-21T09:00:00+00:00",
            ),
            (
                "e-q2",
                "r1",
                "t-q2",
                "app-other",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                3999,
                0,
                0,
                "2026-11-21T09:00:00+00:00",
            ),
        ],
    )

    g_below = conn.execute("SELECT * FROM v_step2_daily_quota_status").fetchone()
    a_rows = {
        str(r["apphub_application_id"]): dict(r)
        for r in conn.execute("SELECT * FROM v_step2_app_daily_quota_status").fetchall()
    }
    assert int(g_below["tokens_reserved_today"]) == 4000
    assert int(g_below["tokens_remaining_today"]) == 1
    assert int(g_below["quota_available"]) == 1
    assert int(a_rows["app-q"]["app_tokens_reserved_today"]) == 4000
    assert int(a_rows["app-q"]["app_tokens_remaining_today"]) == 0
    assert int(a_rows["app-q"]["app_quota_available"]) == 0

    # Add 1 token -> exact 10,000 == cap -> global quota_available MUST be 0 (kills < to <= mutant)
    conn.execute(
        "INSERT INTO eval_results VALUES ('e-q3','r1','t-q3','app-other','groundedness_4pt_v1','gemini-3.8-flash',1,0,0,'2026-11-21T09:05:00+00:00')"
    )
    g_exact = conn.execute(
        "SELECT tokens_remaining_today, quota_available FROM v_step2_daily_quota_status"
    ).fetchone()
    assert int(g_exact["tokens_remaining_today"]) == 0
    assert int(g_exact["quota_available"]) == 0

    # Transpile and execute the inner `_quota_selected_candidates` SELECT from the procedure!
    conn.execute("DELETE FROM eval_results")
    conn.execute("DELETE FROM eval_daily_quota_reservations")
    # Global cap 10,000, per-app cap 6,000; each prompt is 8000 chars -> 2000 input + 1000 headroom = 3000/call
    # app-a has 3 candidates (9000 > 6000 app cap -> picks 2); app-b has 2 candidates (global allows 1 more -> 3 total)
    conn.executemany(
        "INSERT INTO v_step2_trace_rubric_prompts VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "tr-p-a1",
                "o1",
                "app-a",
                "groundedness_4pt_v1",
                3,
                1,
                "2026-11-21T11:10:00+00:00",
                "x" * 8000,
            ),
            (
                "tr-p-a2",
                "o2",
                "app-a",
                "groundedness_4pt_v1",
                3,
                1,
                "2026-11-21T11:11:00+00:00",
                "x" * 8000,
            ),
            (
                "tr-p-a3",
                "o3",
                "app-a",
                "groundedness_4pt_v1",
                3,
                1,
                "2026-11-21T11:12:00+00:00",
                "x" * 8000,
            ),
            (
                "tr-p-b1",
                "o4",
                "app-b",
                "groundedness_4pt_v1",
                3,
                1,
                "2026-11-21T11:13:00+00:00",
                "x" * 8000,
            ),
            (
                "tr-p-b2",
                "o5",
                "app-b",
                "groundedness_4pt_v1",
                3,
                1,
                "2026-11-21T11:14:00+00:00",
                "x" * 8000,
            ),
        ],
    )
    start_marker = "CREATE TEMP TABLE _quota_selected_candidates AS\n"
    end_marker = ";\n\n  SET v_selected_count"
    inner_sql = quota_text.split(start_marker, maxsplit=1)[1].split(end_marker, maxsplit=1)[0]
    inner_sql = (
        inner_sql.replace("p_rubric_id", "'groundedness_4pt_v1'")
        .replace("v_call_headroom_tokens", "1000")
        .replace("COALESCE(p_lookback_hours, 24)", "24")
        .replace("COALESCE(p_canary_sample_pct, 10)", "100")
        .replace(
            "SELECT * EXCEPT (app_cumulative_tokens, global_cumulative_tokens, app_remaining_today, global_remaining_today)",
            "SELECT trace_id, apphub_application_id, est_call_tokens",
        )
    )
    selected = conn.execute(_transpile_bq_view_to_sqlite(inner_sql)).fetchall()
    conn.close()
    assert [str(r["trace_id"]) for r in selected] == ["tr-p-a1", "tr-p-a2", "tr-p-b1"]


def test_shipped_sql_fail_closed_and_boundary_branches() -> None:
    """Kill surviving SQL guard and boundary mutants across core, adapter, quota, and FinOps views (T-1 / S-8)."""
    _assert_unregistered_and_cliff_and_tool_loop_sql_guards()
    _assert_dedup_cpvo_rule0_and_nfr_sql_guards()
    _assert_quota_views_and_procedure_sql_guards()
