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
    evaluate_traces_in_sqlite,
)
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
