"""Transpiled quota guard, deterministic traffic priority, and counterparty SQL view tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bq_eval.core.contract.otel import OTelGenAISpanRecord
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


def _assert_quota_views_and_procedure_sql_guards() -> None:
    """Guard `FAILED_BILLED`, exact cap boundary, and transpiled procedure candidate-selection SQL."""
    quota_path = SQL_DIR / "core" / "step3_daily_quota_guard.sql"
    quota_text = quota_path.read_text(encoding="utf-8")
    assert "IF COALESCE(v_quota_exceeded, TRUE) THEN" in quota_text
    assert "UNNEST(v_app_batches)" in quota_text
    assert "MERGE INTO `agent_eval.eval_daily_quota_state`" in quota_text
    assert "UPDATE `agent_eval.eval_daily_quota_state`" in quota_text
    assert "COALESCE(status, '') = ''" in quota_text
    assert "ordinal_score BETWEEN 1 AND 4" in quota_text
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
    start_marker = "CREATE OR REPLACE TEMP TABLE _quota_selected_candidates AS\n"
    end_marker = ";\n\n    SET v_selected_count"
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
