"""Gold quota guard (`S-18`, `S-19`, `S-23`, `S-26`), traffic priority, and counterparty SQL tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bq_eval.core.contract.otel import OTelGenAISpanRecord, TracePayloadRecord
from bq_eval.core.engine.sql_eval import build_ai_evaluate_agent_sql
from tests.conftest import (
    SQL_DIR,
    eval_all_rubrics,
    eval_traces,
    make_span,
    open_runner_for_spans,
    parse_bigquery_sql,
)


def _extract_quota_exceeded_query(
    quota_sql: str,
    *,
    quota_date_utc: str,
    app_batches: list[tuple[str, int]],
    batch_tokens: int,
) -> str:
    start_marker = "SET v_quota_exceeded = (\n"
    end_marker = "\n        );\n\n        IF COALESCE(v_quota_exceeded, TRUE) THEN"
    subquery = quota_sql.split(start_marker, maxsplit=1)[1].split(end_marker, maxsplit=1)[0]
    union_rows = " UNION ALL ".join(
        f"SELECT '{app_id}' AS apphub_application_id, {tokens} AS batch_tokens"
        for app_id, tokens in app_batches
    )
    return (
        subquery.replace(
            "WITH global_cfg AS (",
            f"WITH req_batches AS ({union_rows}), global_cfg AS (",
        )
        .replace("UNNEST(v_app_batches)", "req_batches")
        .replace("v_quota_date_utc", f"DATE '{quota_date_utc}'")
        .replace("v_batch_tokens", str(batch_tokens))
    )


def _extract_selected_candidates_query(  # noqa: PLR0913
    quota_sql: str,
    *,
    rubric_id: str = "groundedness_4pt_v1",
    quota_date_utc: str = "2026-11-21",
    now_utc: str = "2026-11-21T12:00:00Z",
    headroom_tokens: int = 1000,
    lookback_hours: int = 24,
    canary_pct: int = 100,
) -> str:
    start_marker = "CREATE OR REPLACE TEMP TABLE _quota_selected_candidates AS\n"
    end_marker = ";\n\n    SET v_selected_count"
    inner = quota_sql.split(start_marker, maxsplit=1)[1].split(end_marker, maxsplit=1)[0]
    return (
        inner.replace("p_rubric_id", f"'{rubric_id}'")
        .replace("v_quota_date_utc", f"DATE '{quota_date_utc}'")
        .replace("v_now_utc", f"TIMESTAMP '{now_utc}'")
        .replace("v_lookback_hours", str(lookback_hours))
        .replace("v_call_headroom_tokens", str(headroom_tokens))
        .replace("COALESCE(p_canary_sample_pct, 10)", str(canary_pct))
        .replace(
            "SELECT * EXCEPT (app_cumulative_tokens, global_cumulative_tokens, "
            "app_remaining_today, global_remaining_today)",
            "SELECT trace_id, apphub_application_id, est_call_tokens",
        )
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
        persist_run_id="run_2026_11_21_eval_agent",
    )
    parse_bigquery_sql(engine_b_persist_sql, "build_ai_evaluate_agent_sql(persist)")
    assert "v_eval_agent_turns" in engine_b_persist_sql
    assert "v_bqaa_" not in engine_b_persist_sql
    assert "DIV(CHAR_LENGTH(TO_JSON_STRING(eval_out.trace)), 4)" in engine_b_persist_sql
    assert "DIV(CHAR_LENGTH(COALESCE(metric_row.explanation, '')), 4)" in engine_b_persist_sql
    assert "AND metric_row.metric_name = existing.rubric_id" in engine_b_persist_sql
    assert "WHERE metric_row.score IS NOT NULL" in engine_b_persist_sql
    assert "(ordinal_score >= 3) AS binary_pass" in engine_b_persist_sql

    with open_runner_for_spans(
        current_date_utc="2026-11-21",
        current_timestamp_utc="2026-11-21T12:00:00+00:00",
    ) as runner:
        runner.load_rows(
            "agent_eval.eval_daily_quota_config",
            [
                {
                    "quota_name": "default_step2_daily_cap",
                    "apphub_application_id": "*",
                    "daily_judge_token_cap": 2_000_000,
                    "per_app_default_token_cap": 500_000,
                    "max_output_tokens": 2048,
                    "thinking_budget_tokens": 1024,
                    "updated_at": "2026-11-21T00:00:00+00:00",
                },
                {
                    "quota_name": "secondary_profile",
                    "apphub_application_id": "app-a",
                    "daily_judge_token_cap": 1_500_000,
                    "per_app_default_token_cap": 500_000,
                    "max_output_tokens": 2048,
                    "thinking_budget_tokens": 1024,
                    "updated_at": "2026-11-21T00:00:00+00:00",
                },
            ],
            append=False,
        )
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "e1",
                    "eval_run_id": "run-a",
                    "trace_id": "t1",
                    "apphub_application_id": "app-a",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 700_000,
                    "judge_output_tokens": 100_000,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T08:00:00+00:00",
                },
                {
                    "eval_id": "e1-retry",
                    "eval_run_id": "run-a",
                    "trace_id": "t1",
                    "apphub_application_id": "app-a",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 200_000,
                    "judge_output_tokens": 0,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T08:05:00+00:00",
                },
                {
                    "eval_id": "e2",
                    "eval_run_id": "run-b",
                    "trace_id": "t2",
                    "apphub_application_id": "app-b",
                    "rubric_id": "hallucination_v1",
                    "judge_model": "ai_evaluate_agent_managed",
                    "sampling_stratum": "SAMPLE_10PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 450_000,
                    "judge_output_tokens": 50_000,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T09:00:00+00:00",
                },
            ],
            append=False,
        )
        runner.load_rows(
            "agent_eval.eval_daily_quota_reservations",
            [
                {
                    "reservation_id": "res-1",
                    "quota_date_utc": "2026-11-21",
                    "apphub_application_id": "app-b",
                    "rubric_id": "hallucination_v1",
                    "reserved_tokens": 100_000,
                    "status": "RESERVED",
                    "created_at": "2026-11-21T09:05:00+00:00",
                    "updated_at": "2026-11-21T09:05:00+00:00",
                }
            ],
            append=False,
        )
        row = runner.query("SELECT * FROM `agent_eval.v_step2_daily_quota_status`").rows[0]
        assert int(str(row["daily_judge_token_cap"])) == 2_000_000
        assert int(str(row["tokens_used_today"])) == 1_500_000
        assert int(str(row["tokens_reserved_today"])) == 100_000
        assert int(str(row["tokens_remaining_today"])) == 400_000
        assert bool(row["quota_available"]) is True

        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "e3",
                    "eval_run_id": "run-b",
                    "trace_id": "t2",
                    "apphub_application_id": "app-b",
                    "rubric_id": "safety_v1",
                    "judge_model": "ai_evaluate_agent_managed",
                    "sampling_stratum": "SAMPLE_10PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 450_000,
                    "judge_output_tokens": 50_000,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T10:00:00+00:00",
                }
            ],
            append=True,
        )
        tripped = runner.query("SELECT * FROM `agent_eval.v_step2_daily_quota_status`").rows[0]
        assert int(str(tripped["tokens_used_today"])) == 2_000_000
        assert int(str(tripped["tokens_remaining_today"])) == 0
        assert bool(tripped["quota_available"]) is False


def test_quota_reservation_check_executes() -> None:
    """`SET v_quota_exceeded` executes cleanly on under-cap/over-cap batches and excludes `ERROR` spans (`S-18`)."""
    quota_sql = (SQL_DIR / "core" / "step3_daily_quota_guard.sql").read_text(encoding="utf-8")
    for sql_file in sorted(SQL_DIR.rglob("*.sql")):
        assert "SELEC " not in sql_file.read_text(encoding="utf-8")

    base = datetime(2026, 11, 21, 11, 30, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-s18-ok",
            "sp-s18-ok",
            app_id="app-a",
            status_code="OK",
            rule0_pass=True,
            base_time=base,
        ),
        make_span(
            "tr-s18-err",
            "sp-s18-err",
            app_id="app-a",
            status_code="ERROR",
            rule0_pass=False,
            offset_s=5,
            base_time=base,
        ),
    ]
    payloads = [
        TracePayloadRecord(
            trace_id="tr-s18-ok",
            apphub_application_id="app-a",
            ingested_at=base,
            user_prompt="Verify healthy prompt",
            retrieved_context="ctx",
            final_response="ok response",
        ),
        TracePayloadRecord(
            trace_id="tr-s18-err",
            apphub_application_id="app-a",
            ingested_at=base + timedelta(seconds=5),
            user_prompt="Verify failing prompt",
            retrieved_context="ctx",
            final_response="err response",
        ),
    ]
    with open_runner_for_spans(
        spans,
        payloads=payloads,
        current_date_utc="2026-11-21",
        current_timestamp_utc="2026-11-21T12:00:00+00:00",
    ) as runner:
        all_prompts = {
            str(r["trace_id"]): bool(r["step1_pass"])
            for r in runner.query(
                "SELECT trace_id, step1_pass FROM `agent_eval.v_step2_all_trace_rubric_prompts` "
                "WHERE rubric_id = 'groundedness_4pt_v1'"
            ).rows
        }
        routine_prompts = {
            str(r["trace_id"])
            for r in runner.query(
                "SELECT trace_id FROM `agent_eval.v_step2_trace_rubric_prompts` "
                "WHERE rubric_id = 'groundedness_4pt_v1'"
            ).rows
        }
        assert all_prompts == {"tr-s18-ok": True, "tr-s18-err": False}
        assert routine_prompts == {"tr-s18-ok"}

        runner.load_rows(
            "agent_eval.eval_daily_quota_config",
            [
                {
                    "quota_name": "default_step2_daily_cap",
                    "apphub_application_id": "*",
                    "daily_judge_token_cap": 2_000_000,
                    "per_app_default_token_cap": 500_000,
                    "max_output_tokens": 2048,
                    "thinking_budget_tokens": 1024,
                    "updated_at": "2026-11-21T00:00:00+00:00",
                }
            ],
            append=False,
        )
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "ev-s18-1",
                    "eval_run_id": "run-s18",
                    "trace_id": "tr-prev",
                    "apphub_application_id": "app-a",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 400_000,
                    "judge_output_tokens": 0,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T09:00:00+00:00",
                }
            ],
            append=False,
        )
        under_q = _extract_quota_exceeded_query(
            quota_sql,
            quota_date_utc="2026-11-21",
            app_batches=[("app-a", 100_000)],
            batch_tokens=100_000,
        )
        over_app_q = _extract_quota_exceeded_query(
            quota_sql,
            quota_date_utc="2026-11-21",
            app_batches=[("app-a", 100_001)],
            batch_tokens=100_001,
        )
        assert bool(next(iter(runner.query(under_q).rows[0].values()))) is False
        assert bool(next(iter(runner.query(over_app_q).rows[0].values()))) is True

        runner.load_rows(
            "agent_eval.eval_daily_quota_reservations",
            [
                {
                    "reservation_id": "res-s18-global",
                    "quota_date_utc": "2026-11-21",
                    "apphub_application_id": "app-other",
                    "rubric_id": "groundedness_4pt_v1",
                    "reserved_tokens": 1_550_000,
                    "status": "RESERVED",
                    "created_at": "2026-11-21T09:10:00+00:00",
                    "updated_at": "2026-11-21T09:10:00+00:00",
                }
            ],
            append=False,
        )
        over_global_q = _extract_quota_exceeded_query(
            quota_sql,
            quota_date_utc="2026-11-21",
            app_batches=[("app-a", 60_000)],
            batch_tokens=60_000,
        )
        assert bool(next(iter(runner.query(over_global_q).rows[0].values()))) is True


def test_rejected_judge_rows_stay_in_burn_and_dedup() -> None:
    """`judge_status = 'REJECTED'` rows burn quota, block re-selection, and stay out of pass rates (`S-19`)."""
    quota_sql = (SQL_DIR / "core" / "step3_daily_quota_guard.sql").read_text(encoding="utf-8")
    base = datetime(2026, 11, 21, 11, 0, 0, tzinfo=UTC)
    spans = [
        make_span("tr-s19-scored", "sp-s19-1", app_id="app-s19", base_time=base),
        make_span("tr-s19-rejected", "sp-s19-2", app_id="app-s19", offset_s=5, base_time=base),
        make_span("tr-s19-fresh", "sp-s19-3", app_id="app-s19", offset_s=10, base_time=base),
    ]
    payloads = [
        TracePayloadRecord(
            trace_id=sp.trace_id,
            apphub_application_id="app-s19",
            ingested_at=sp.start_time,
            user_prompt=f"prompt for {sp.trace_id}",
            retrieved_context="ctx",
            final_response="resp",
        )
        for sp in spans
    ]
    with open_runner_for_spans(
        spans,
        payloads=payloads,
        current_date_utc="2026-11-21",
        current_timestamp_utc="2026-11-21T12:00:00+00:00",
    ) as runner:
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "ev-s19-ok",
                    "eval_run_id": "run-s19",
                    "trace_id": "tr-s19-scored",
                    "apphub_application_id": "app-s19",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_100PCT",
                    "rule0_pass": True,
                    "reasoning_trace": "valid verdict",
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 1_000,
                    "judge_output_tokens": 100,
                    "judge_thinking_tokens": 50,
                    "evaluated_at": "2026-11-21T11:20:00+00:00",
                },
                {
                    "eval_id": "ev-s19-rej",
                    "eval_run_id": "run-s19",
                    "trace_id": "tr-s19-rejected",
                    "apphub_application_id": "app-s19",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_100PCT",
                    "rule0_pass": True,
                    "reasoning_trace": "REJECTED: RESOURCE_EXHAUSTED",
                    "ordinal_score": None,
                    "binary_pass": None,
                    "judge_status": "REJECTED",
                    "judge_input_tokens": 2_000,
                    "judge_output_tokens": 0,
                    "judge_thinking_tokens": 1_024,
                    "evaluated_at": "2026-11-21T11:21:00+00:00",
                },
            ],
            append=False,
        )
        quota_row = runner.query("SELECT * FROM `agent_eval.v_step2_daily_quota_status`").rows[0]
        assert int(str(quota_row["tokens_used_today"])) == 1_150 + 3_024

        run_row = runner.query("SELECT * FROM `agent_eval.v_step3_eval_runs`").rows[0]
        assert int(str(run_row["evaluated_traces"])) == 1
        assert int(str(run_row["rejected_traces"])) == 1
        assert float(str(run_row["rubric_pass_rate"])) == pytest.approx(1.0)

        sum_row = runner.query(
            "SELECT judged_samples, raw_judge_pass_rate "
            "FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'app-s19'"
        ).rows[0]
        assert int(str(sum_row["judged_samples"])) == 1
        assert float(str(sum_row["raw_judge_pass_rate"])) == pytest.approx(1.0)

        cand_q = _extract_selected_candidates_query(quota_sql)
        selected_ids = [str(r["trace_id"]) for r in runner.query(cand_q).rows]
        assert selected_ids == ["tr-s19-fresh"]


def test_quota_procedure_exit_reasons_and_date_pin() -> None:
    """Procedure raises on unknown rubric, logs 5 exit reasons, pins one UTC date, and drops `reserved_tokens` (`S-23`, `S-26`)."""
    quota_sql = (SQL_DIR / "core" / "step3_daily_quota_guard.sql").read_text(encoding="utf-8")
    proc_body = quota_sql.split("CREATE OR REPLACE PROCEDURE", maxsplit=1)[1]

    assert "RAISE USING MESSAGE = CONCAT('Unknown p_rubric_id in agent_eval.rubrics: '" in proc_body
    for reason in (
        "'NO_CANDIDATES'",
        "'QUOTA_EXCEEDED'",
        "'RESERVATION_CONFLICT'",
        "'COMMITTED'",
        "'FAILED_BILLED'",
    ):
        assert reason in proc_body
    assert "DECLARE v_now_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP();" in proc_body
    assert "DECLARE v_quota_date_utc DATE DEFAULT DATE(v_now_utc);" in proc_body
    assert "CURRENT_DATE" not in proc_body
    assert "LEAST(168, GREATEST(1, COALESCE(p_lookback_hours, 24)))" in proc_body

    with open_runner_for_spans() as runner:
        state_cols = runner.query("SELECT * FROM `agent_eval.eval_daily_quota_state`").columns
        assert state_cols == ("quota_date_utc", "updated_at")
        assert "reserved_tokens" not in state_cols
        log_cols = runner.query("SELECT * FROM `agent_eval.eval_quota_run_log`").columns
        assert "exit_reason" in log_cols and "quota_date_utc" in log_cols

        clamp_rows = runner.query(
            "SELECT "
            "LEAST(168, GREATEST(1, COALESCE(-5, 24))) AS neg_val, "
            "LEAST(168, GREATEST(1, COALESCE(0, 24))) AS zero_val, "
            "LEAST(168, GREATEST(1, COALESCE(CAST(NULL AS INT64), 24))) AS null_val, "
            "LEAST(168, GREATEST(1, COALESCE(999, 24))) AS huge_val"
        ).rows[0]
        assert int(str(clamp_rows["neg_val"])) == 1
        assert int(str(clamp_rows["zero_val"])) == 1
        assert int(str(clamp_rows["null_val"])) == 24
        assert int(str(clamp_rows["huge_val"])) == 168


def test_deterministic_traffic_type_unjudged_rubric_and_multi_rubric_filter() -> None:
    """Explicit `finops_traffic_type` priority, `'groundedness_4pt_v1'` unjudged label, and multi-rubric parity."""
    gates_text = (SQL_DIR / "core" / "gold_eval_gates.sql").read_text(encoding="utf-8")
    assert "ANY_VALUE(w.finops_traffic_type)" not in gates_text
    assert "MAX(w.finops_traffic_type) AS primary_traffic_type" not in gates_text
    assert "WHEN 'production_inference' THEN 1" in gates_text

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
    all_rubs = eval_all_rubrics(
        spans=py_spans,
        judge_outcomes={"tr-multi": False},
        rubric_outcomes_by_id={"groundedness_4pt_v1": {"tr-multi": True}},
        rubric_id="escalation_policy_4pt_v1",
    )
    unjudged = eval_traces(spans=py_spans[:1])["unjudged-parity-app"]

    assert unjudged.rubric_id == "groundedness_4pt_v1"
    assert unjudged.calibration_status == "UNJUDGED"
    assert all_rubs[
        ("multi-rubric-app", "escalation_policy_4pt_v1")
    ].raw_judge_pass_rate == pytest.approx(0.0)
    assert all_rubs[
        ("multi-rubric-app", "groundedness_4pt_v1")
    ].raw_judge_pass_rate == pytest.approx(1.0)


def test_counterparty_classification_uses_explicit_flag_not_app_id_substring() -> None:
    """An internal app named `external-facing-chat` stays in CPVO; explicit flags route to `v_counterparty_spend`."""
    for sql_file in sorted((SQL_DIR / "core").glob("*.sql")):
        assert "LIKE '%external%'" not in sql_file.read_text(encoding="utf-8")

    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    for idx, (app_id, out_id, attrs) in enumerate(
        (
            ("external-facing-chat", "out-chat-1", {}),
            ("partner-carrier-hub", "out-ext-1", {}),
            ("attr-marked-partner", "out-ext-2", {"apphub.external": "true"}),
        )
    ):
        spans.extend(
            (
                make_span(
                    f"tr-{out_id}",
                    f"sp-{out_id}",
                    app_id=app_id,
                    outcome_id=out_id,
                    outcome_owner=app_id,
                    source_mode="A2A",
                    input_tokens=10_000,
                    cached_input_tokens=0,
                    output_tokens=500,
                    thinking_tokens=0,
                    offset_s=idx * 5,
                    attributes=attrs,
                    base_time=base,
                ),
                make_span(
                    f"tr-{out_id}",
                    f"sp-close-{out_id}",
                    parent_span_id=f"sp-{out_id}",
                    span_name="outcome.closed",
                    app_id=app_id,
                    outcome_id=out_id,
                    outcome_owner=app_id,
                    model_name="none",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    thinking_tokens=0,
                    latency_ms=0.0,
                    offset_s=idx * 5 + 1,
                    attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
                    base_time=base,
                ),
            )
        )

    with open_runner_for_spans(spans) as runner:
        runner.conn.execute(
            "UPDATE agent_eval__workload_thresholds "
            "SET is_external_counterparty = 1 WHERE apphub_application_id = 'partner-carrier-hub'"
        )
        cpvo_apps = {
            str(r["apphub_application_id"])
            for r in runner.query(
                "SELECT apphub_application_id FROM `agent_eval.v_enterprise_cost_per_verified_outcome`"
            ).rows
        }
        counterparty_apps = {
            str(r["apphub_application_id"])
            for r in runner.query(
                "SELECT apphub_application_id FROM `agent_eval.v_counterparty_spend`"
            ).rows
        }

    assert "external-facing-chat" in cpvo_apps
    assert "external-facing-chat" not in counterparty_apps
    assert counterparty_apps == {"partner-carrier-hub", "attr-marked-partner"}


def _assert_quota_views_and_procedure_sql_guards() -> None:
    """Guard `FAILED_BILLED`, exact cap boundary, and procedure candidate-selection SQL."""
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

    base = datetime(2026, 11, 21, 11, 10, 0, tzinfo=UTC)
    cand_specs = [
        ("tr-p-a1", "o1", "app-a", 0),
        ("tr-p-a2", "o2", "app-a", 60),
        ("tr-p-a3", "o3", "app-a", 120),
        ("tr-p-b1", "o4", "app-b", 180),
        ("tr-p-b2", "o5", "app-b", 240),
    ]
    spans = [
        make_span(tid, f"sp-{tid}", app_id=app_id, outcome_id=oid, offset_s=off, base_time=base)
        for tid, oid, app_id, off in cand_specs
    ]
    payloads = [
        TracePayloadRecord(
            trace_id=tid,
            apphub_application_id=app_id,
            ingested_at=base + timedelta(seconds=off),
            user_prompt="x" * 6600,
            retrieved_context="",
            final_response="ok",
        )
        for tid, _, app_id, off in cand_specs
    ]
    with open_runner_for_spans(
        spans,
        payloads=payloads,
        current_date_utc="2026-11-21",
        current_timestamp_utc="2026-11-21T12:00:00+00:00",
    ) as runner:
        runner.load_rows(
            "agent_eval.eval_daily_quota_config",
            [
                {
                    "quota_name": "default_step2_daily_cap",
                    "apphub_application_id": "*",
                    "daily_judge_token_cap": 10_000,
                    "per_app_default_token_cap": 6_000,
                    "max_output_tokens": 1_000,
                    "thinking_budget_tokens": 0,
                    "updated_at": "2026-11-21T00:00:00+00:00",
                }
            ],
            append=False,
        )
        runner.load_rows(
            "agent_eval.eval_daily_quota_reservations",
            [
                {
                    "reservation_id": "r-fb",
                    "quota_date_utc": "2026-11-21",
                    "apphub_application_id": "app-q",
                    "rubric_id": "groundedness_4pt_v1",
                    "reserved_tokens": 4_000,
                    "status": "FAILED_BILLED",
                    "created_at": "2026-11-21T08:00:00+00:00",
                    "updated_at": "2026-11-21T08:01:00+00:00",
                },
                {
                    "reservation_id": "r-rel",
                    "quota_date_utc": "2026-11-21",
                    "apphub_application_id": "app-q",
                    "rubric_id": "groundedness_4pt_v1",
                    "reserved_tokens": 99_999,
                    "status": "RELEASED",
                    "created_at": "2026-11-21T08:00:00+00:00",
                    "updated_at": "2026-11-21T08:01:00+00:00",
                },
                {
                    "reservation_id": "r-com",
                    "quota_date_utc": "2026-11-21",
                    "apphub_application_id": "app-q",
                    "rubric_id": "groundedness_4pt_v1",
                    "reserved_tokens": 99_999,
                    "status": "COMMITTED",
                    "created_at": "2026-11-21T08:00:00+00:00",
                    "updated_at": "2026-11-21T08:01:00+00:00",
                },
            ],
            append=False,
        )
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "e-q1",
                    "eval_run_id": "r1",
                    "trace_id": "t-q1",
                    "apphub_application_id": "app-q",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 2_000,
                    "judge_output_tokens": 0,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T09:00:00+00:00",
                },
                {
                    "eval_id": "e-q2",
                    "eval_run_id": "r1",
                    "trace_id": "t-q2",
                    "apphub_application_id": "app-other",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 3_999,
                    "judge_output_tokens": 0,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T09:00:00+00:00",
                },
            ],
            append=False,
        )
        g_below = runner.query("SELECT * FROM `agent_eval.v_step2_daily_quota_status`").rows[0]
        a_rows = {
            str(r["apphub_application_id"]): dict(r)
            for r in runner.query("SELECT * FROM `agent_eval.v_step2_app_daily_quota_status`").rows
        }
        assert int(str(g_below["tokens_reserved_today"])) == 4_000
        assert int(str(g_below["tokens_remaining_today"])) == 1
        assert int(str(g_below["quota_available"])) == 1
        assert int(str(a_rows["app-q"]["app_tokens_reserved_today"])) == 4_000
        assert int(str(a_rows["app-q"]["app_tokens_remaining_today"])) == 0
        assert int(str(a_rows["app-q"]["app_quota_available"])) == 0

        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "e-q3",
                    "eval_run_id": "r1",
                    "trace_id": "t-q3",
                    "apphub_application_id": "app-other",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 1,
                    "judge_output_tokens": 0,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T09:05:00+00:00",
                }
            ],
            append=True,
        )
        g_exact = runner.query(
            "SELECT tokens_remaining_today, quota_available FROM `agent_eval.v_step2_daily_quota_status`"
        ).rows[0]
        assert int(str(g_exact["tokens_remaining_today"])) == 0
        assert int(str(g_exact["quota_available"])) == 0

        runner.load_rows("agent_eval.eval_results", (), append=False)
        runner.load_rows("agent_eval.eval_daily_quota_reservations", (), append=False)
        selected = runner.query(_extract_selected_candidates_query(quota_text)).rows
        assert [str(r["trace_id"]) for r in selected] == ["tr-p-a1", "tr-p-a2", "tr-p-b1"]
