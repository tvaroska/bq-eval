"""Day-2 BigQuery daily quota view and procedure splitter tests."""

from __future__ import annotations

from scripts.bq_dry_run import split_sql_statements
from tests.sql_helpers import (
    SQL_DIR,
    _extract_view_sql_text,
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
)


def test_sql_app_daily_quota_view_and_procedure_splitter() -> None:
    """`v_step2_app_daily_quota_status` enforces per-app sub-caps and procedure splits atomically."""
    quota_path = SQL_DIR / "core" / "step3_daily_quota_guard.sql"
    quota_text = quota_path.read_text(encoding="utf-8")
    assert "DATE(evaluated_at) >= DATE_SUB(CURRENT_DATE('UTC'), INTERVAL 7 DAY)" in quota_text
    stmts = split_sql_statements(quota_text)
    proc_stmts = [s for s in stmts if s.upper().startswith("CREATE OR REPLACE PROCEDURE")]
    assert len(proc_stmts) == 1
    assert "EXCEPTION WHEN ERROR THEN" in proc_stmts[0]
    assert proc_stmts[0].rstrip().endswith("END")

    conn = _new_sqlite_bq_conn(current_date_utc="2026-11-21")
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
        """
    )
    conn.executemany(
        "INSERT INTO eval_daily_quota_config VALUES (?,?,?,?,?,?,?)",
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
                "noisy_override",
                "noisy-app",
                300_000,
                500_000,
                2048,
                1024,
                "2026-11-21T00:00:00+00:00",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO eval_results VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "e1",
            "r1",
            "t1",
            "noisy-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            250_000,
            50_000,
            0,
            "2026-11-21T08:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO eval_daily_quota_reservations VALUES (?,?,?,?,?,?,?,?)",
        (
            "res-q",
            "2026-11-21",
            "quiet-app",
            "groundedness_4pt_v1",
            100_000,
            "RESERVED",
            "2026-11-21T08:00:00+00:00",
            "2026-11-21T08:00:00+00:00",
        ),
    )
    app_view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(quota_path, "agent_eval.v_step2_app_daily_quota_status")
    )
    conn.execute(f"CREATE VIEW v_step2_app_daily_quota_status AS {app_view_sql}")
    by_app = {
        str(r["apphub_application_id"]): dict(r)
        for r in conn.execute("SELECT * FROM v_step2_app_daily_quota_status").fetchall()
    }
    assert int(by_app["noisy-app"]["app_tokens_remaining_today"]) == 0
    assert bool(by_app["noisy-app"]["app_quota_available"]) is False
    assert int(by_app["quiet-app"]["app_daily_token_cap"]) == 500_000
    assert int(by_app["quiet-app"]["app_tokens_remaining_today"]) == 400_000
    assert bool(by_app["quiet-app"]["app_quota_available"]) is True
    conn.close()
