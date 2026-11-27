"""Day-2 BigQuery daily quota view and procedure splitter tests (`OfflineSqlRunner`)."""

from __future__ import annotations

from scripts.bq_dry_run import split_sql_statements
from tests.conftest import SQL_DIR, open_runner_for_spans


def test_sql_app_daily_quota_view_and_procedure_splitter() -> None:
    """`v_step2_app_daily_quota_status` enforces per-app sub-caps and procedure splits atomically."""
    quota_path = SQL_DIR / "core" / "step3_daily_quota_guard.sql"
    quota_text = quota_path.read_text(encoding="utf-8")
    assert "DATE(evaluated_at) >= DATE_SUB(v_quota_date_utc, INTERVAL 7 DAY)" in quota_text
    stmts = split_sql_statements(quota_text)
    proc_stmts = [s for s in stmts if s.upper().startswith("CREATE OR REPLACE PROCEDURE")]
    assert len(proc_stmts) == 1
    assert "EXCEPTION WHEN ERROR THEN" in proc_stmts[0]
    assert proc_stmts[0].rstrip().endswith("END")

    with open_runner_for_spans(current_date_utc="2026-11-21") as runner:
        runner.load_rows(
            "agent_eval.eval_daily_quota_config",
            [
                {
                    "quota_name": "noisy_override",
                    "apphub_application_id": "noisy-app",
                    "daily_judge_token_cap": 300_000,
                    "per_app_default_token_cap": 500_000,
                    "max_output_tokens": 2048,
                    "thinking_budget_tokens": 1024,
                    "updated_at": "2026-11-21T00:00:00+00:00",
                }
            ],
            append=True,
        )
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "e1",
                    "eval_run_id": "r1",
                    "trace_id": "t1",
                    "apphub_application_id": "noisy-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_10PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 250_000,
                    "judge_output_tokens": 50_000,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T08:00:00+00:00",
                }
            ],
            append=True,
        )
        runner.load_rows(
            "agent_eval.eval_daily_quota_reservations",
            [
                {
                    "reservation_id": "res-q",
                    "quota_date_utc": "2026-11-21",
                    "apphub_application_id": "quiet-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "reserved_tokens": 100_000,
                    "status": "RESERVED",
                    "created_at": "2026-11-21T08:00:00+00:00",
                    "updated_at": "2026-11-21T08:00:00+00:00",
                }
            ],
            append=True,
        )
        by_app = {
            str(r["apphub_application_id"]): dict(r)
            for r in runner.query("SELECT * FROM `agent_eval.v_step2_app_daily_quota_status`").rows
        }

    assert int(str(by_app["noisy-app"]["app_tokens_remaining_today"])) == 0
    assert bool(by_app["noisy-app"]["app_quota_available"]) is False
    assert int(str(by_app["quiet-app"]["app_daily_token_cap"])) == 500_000
    assert int(str(by_app["quiet-app"]["app_tokens_remaining_today"])) == 400_000
    assert bool(by_app["quiet-app"]["app_quota_available"]) is True
