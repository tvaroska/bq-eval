"""Day-2 Quota Guard Procedure, EvalBackend Ports, and `bq-eval bqaa` CLI."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import (
    BQAAAgentEvent,
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
)
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.ports import (
    BigQueryEvalBackend,
    EvalBackend,
    QueryRunner,
    SQLiteEvalBackend,
)
from bq_eval.core.engine.quota import (
    DailyQuotaGuard,
    QuotaCandidateTrace,
    estimate_judge_call_tokens,
    thinking_level_headroom_tokens,
)
from bq_eval.demo.pipeline import run_showcase_pipeline
from bq_eval.finops.engine.capacity_export import hourly_burndown_rows
from scripts.bq_dry_run import split_sql_statements
from tests.test_sql_contracts import (
    ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
    ENTERPRISE_OTEL_SPANS_BASE_INSERT,
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
)


def _sqlite_bq_query_runner(conn: sqlite3.Connection) -> QueryRunner:
    def _run(sql: str) -> Sequence[Mapping[str, object]]:
        rewritten = sql.replace("`demo-proj.agent_eval.", "").replace("`", "")
        rows = conn.execute(rewritten).fetchall()
        return [dict(r) for r in rows]

    return _run


def test_quota_guard_headroom_per_app_subcaps_non_deduped_and_retry_storm() -> None:
    """Output/thinking headroom, per-app sub-caps, non-deduped burn, and FAILED_BILLED lock."""
    assert thinking_level_headroom_tokens("NONE") == 0
    assert thinking_level_headroom_tokens("HIGH") == 8192
    with pytest.raises(ValueError, match="Unsupported thinking_level"):
        thinking_level_headroom_tokens("ULTRA")

    # 1,000 prompt tokens + 2,048 max_output + 8,192 HIGH thinking = 11,240 per call.
    prompt_1k = "x" * 4000
    est = estimate_judge_call_tokens(prompt_1k, max_output_tokens=2048, thinking_level="HIGH")
    assert est == 11_240

    # Without headroom (prompt only), 20k budget would wrongly allow 20 calls; with headroom, only 1!
    guard = DailyQuotaGuard(
        daily_judge_token_cap=25_000,
        per_app_default_token_cap=12_000,
        per_app_caps={"noisy-app": 12_000, "quiet-app": 12_000},
        max_output_tokens=2048,
        thinking_level="HIGH",
    )
    candidates = [
        QuotaCandidateTrace(
            trace_id=f"tr-noisy-{i:02d}",
            apphub_application_id="noisy-app",
            rubric_id="hallucination_4pt_v1",
            prompt_text=prompt_1k,
            trace_start_iso=f"2026-11-21T08:0{i}:00+00:00",
        )
        for i in range(4)
    ] + [
        QuotaCandidateTrace(
            trace_id="tr-quiet-00",
            apphub_application_id="quiet-app",
            rubric_id="hallucination_4pt_v1",
            prompt_text=prompt_1k,
            trace_start_iso="2026-11-21T08:09:00+00:00",
        ),
        QuotaCandidateTrace(
            trace_id="tr-other-rubric",
            apphub_application_id="quiet-app",
            rubric_id="groundedness_4pt_v1",
            prompt_text=prompt_1k,
            trace_start_iso="2026-11-21T08:00:00+00:00",
        ),
    ]

    res = guard.reserve_batch(
        candidates,
        rubric_id="hallucination_4pt_v1",
        max_output_tokens=2048,
        thinking_level="HIGH",
    )
    assert res.reservation_id is not None
    # Per-app sub-cap stops noisy-app at 1 trace so quiet-app still gets its 1 trace!
    assert res.selected_trace_ids == ("tr-noisy-00", "tr-quiet-00")
    assert res.reserved_tokens_by_app == {"noisy-app": 11_240, "quiet-app": 11_240}
    assert res.total_reserved_tokens == 22_480
    assert res.global_remaining_after == 2_520

    # Mark failed batch FAILED_BILLED -> tokens stay consumed so retry storms cannot bypass cap.
    guard.finalize_reservation(res.reservation_id, status="FAILED_BILLED")
    retry_res = guard.reserve_batch(
        candidates,
        rubric_id="hallucination_4pt_v1",
        max_output_tokens=2048,
        thinking_level="HIGH",
    )
    assert retry_res.reservation_id is None
    assert retry_res.selected_trace_ids == ()

    # Non-deduped burn accounting: two billed retries for the same trace_id both subtract budget.
    guard2 = DailyQuotaGuard(
        daily_judge_token_cap=5_000,
        per_app_default_token_cap=5_000,
        max_output_tokens=500,
        thinking_level="NONE",
    )
    guard2.record_billed_eval(
        eval_id="ev-1",
        trace_id="tr-dup",
        apphub_application_id="app-a",
        rubric_id="groundedness_4pt_v1",
        judge_input_tokens=2_000,
        judge_output_tokens=500,
    )
    guard2.record_billed_eval(
        eval_id="ev-1-retry",
        trace_id="tr-dup",
        apphub_application_id="app-a",
        rubric_id="groundedness_4pt_v1",
        judge_input_tokens=2_000,
        judge_output_tokens=500,
    )
    after_retries = guard2.reserve_batch(
        [
            QuotaCandidateTrace(
                trace_id="tr-new",
                apphub_application_id="app-a",
                rubric_id="groundedness_4pt_v1",
                prompt_text="x" * 400,
                trace_start_iso="2026-11-21T09:00:00+00:00",
            )
        ],
        rubric_id="groundedness_4pt_v1",
        max_output_tokens=500,
        thinking_level="NONE",
    )
    assert after_retries.selected_trace_ids == ()
    assert after_retries.global_remaining_after == 0
    guard.close()
    guard2.close()


def test_quota_guard_concurrent_reservations_prevent_toctou_and_sql_app_view_parity() -> None:
    """Atomic reservation stops concurrent TOCTOU overspend; SQL app view & splitter match."""
    guard = DailyQuotaGuard(
        daily_judge_token_cap=10_000,
        per_app_default_token_cap=10_000,
        max_output_tokens=1000,
        thinking_level="NONE",
    )
    batch_a = [
        QuotaCandidateTrace(
            trace_id=f"tr-a-{i}",
            apphub_application_id="app-a",
            rubric_id="groundedness_4pt_v1",
            prompt_text="x" * 16_000,  # 4,000 prompt + 1,000 output = 5,000 tokens
            trace_start_iso=f"2026-11-21T08:0{i}:00+00:00",
        )
        for i in range(3)
    ]
    batch_b = [
        QuotaCandidateTrace(
            trace_id=f"tr-b-{i}",
            apphub_application_id="app-b",
            rubric_id="groundedness_4pt_v1",
            prompt_text="x" * 16_000,
            trace_start_iso=f"2026-11-21T08:1{i}:00+00:00",
        )
        for i in range(3)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            guard.reserve_batch,
            batch_a,
            rubric_id="groundedness_4pt_v1",
            max_output_tokens=1000,
            thinking_level="NONE",
        )
        fut_b = pool.submit(
            guard.reserve_batch,
            batch_b,
            rubric_id="groundedness_4pt_v1",
            max_output_tokens=1000,
            thinking_level="NONE",
        )
        res_a, res_b = fut_a.result(), fut_b.result()

    assert res_a.total_reserved_tokens + res_b.total_reserved_tokens <= 10_000
    assert len(res_a.selected_trace_ids) + len(res_b.selected_trace_ids) == 2
    guard.close()

    # Verify `v_step2_app_daily_quota_status` executes cleanly in transpiled SQLite and
    # `split_sql_statements` keeps `CREATE OR REPLACE PROCEDURE` as a single atomic statement.
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


def _seed_port_parity_fixture(
    conn: sqlite3.Connection,
) -> tuple[list[OTelGenAISpanRecord], dict[str, bool], ConfusionMatrix]:
    t0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    outcomes: dict[str, bool] = {}

    for i in range(10):
        tid = f"trace-port-{i:03d}"
        start = t0 + timedelta(minutes=i * 15)
        end = start + timedelta(milliseconds=900 + i * 10)
        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-port-{i:03d}",
                start_time=start,
                end_time=end,
                latency_ms=900.0 + i * 10,
                status_code="OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                service_name="svc-port",
                gcp_apphub_application_id="port-app",
                agent_name="port-agent",
                model_name="gemini-3.8-flash",
                input_tokens=10_000,
                cached_input_tokens=2_000,
                output_tokens=400,
                thinking_tokens=100,
                rule0_pass=True,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            )
        )
        passed = i < 9
        outcomes[tid] = passed
        conn.execute(
            ENTERPRISE_OTEL_SPANS_BASE_INSERT,
            (
                tid,
                f"sp-port-{i:03d}",
                None,
                "invoke_agent",
                "port-app",
                "svc-port",
                "port-agent",
                "SPAN_KIND_SERVER",
                "invoke_agent",
                "gemini-3.8-flash",
                None,
                None,
                "OK",
                1,
                "production_inference",
                None,
                "port-app",
                None,
                None,
                None,
                None,
                start.isoformat(),
                end.isoformat(),
                end.isoformat(),
                900.0 + i * 10,
                10_000,
                2_000,
                400,
                100,
                "{}",
            ),
        )
        conn.execute(
            ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
            (
                f"ev-port-{i:03d}",
                "run-port",
                tid,
                None,
                "port-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "rationale",
                4 if passed else 2,
                1 if passed else 0,
                1_000,
                100,
                0,
                (end + timedelta(seconds=10)).isoformat(),
            ),
        )

    unjudged_start = t0 + timedelta(hours=1)
    unjudged_end = unjudged_start + timedelta(milliseconds=400)
    spans.append(
        OTelGenAISpanRecord(
            trace_id="trace-unjudged-001",
            span_id="sp-unjudged-001",
            start_time=unjudged_start,
            end_time=unjudged_end,
            latency_ms=400.0,
            status_code="OK",
            span_kind=GenAISpanKind.CHAT,
            service_name="svc-cat",
            gcp_apphub_application_id="unjudged-app",
            agent_name="cat-agent",
            model_name="gemini-3.5-flash-lite",
            input_tokens=5_000,
            cached_input_tokens=2_500,
            output_tokens=200,
            thinking_tokens=0,
            rule0_pass=True,
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
        )
    )
    conn.execute(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        (
            "trace-unjudged-001",
            "sp-unjudged-001",
            None,
            "chat",
            "unjudged-app",
            "svc-cat",
            "cat-agent",
            "SPAN_KIND_SERVER",
            "chat",
            "gemini-3.5-flash-lite",
            None,
            None,
            "OK",
            1,
            "production_inference",
            None,
            "unjudged-app",
            None,
            None,
            None,
            None,
            unjudged_start.isoformat(),
            unjudged_end.isoformat(),
            unjudged_end.isoformat(),
            400.0,
            5_000,
            2_500,
            200,
            0,
            "{}",
        ),
    )

    sme_rows = (
        [("port-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 87
        + [("port-app", "groundedness_4pt_v1", 1, 4, 1, 2, 0)] * 2
        + [("port-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)] * 7
        + [("port-app", "groundedness_4pt_v1", 1, 1, 0, 4, 1)] * 4
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)
    return spans, outcomes, ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)


@pytest.mark.port
def test_eval_backend_port_local_sqlite_vs_cloud_bigquery_parity() -> None:
    """`SQLiteEvalBackend` and `BigQueryEvalBackend` match to <1e-12 across both methods."""
    local_backend = SQLiteEvalBackend()
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    cloud_backend = BigQueryEvalBackend(
        project_id="demo-proj",
        dataset_id="agent_eval",
        query_runner=_sqlite_bq_query_runner(conn),
    )
    assert isinstance(local_backend, EvalBackend)
    assert isinstance(cloud_backend, EvalBackend)

    spans, outcomes, cm = _seed_port_parity_fixture(conn)
    judge_cost = (1_000 / 1e6) * 0.15 + (100 / 1e6) * 0.60
    local_res = local_backend.evaluate_traces(
        spans=spans,
        judge_outcomes=outcomes,
        sme_confusion={"port-app": cm},
        judge_cost_per_call_usd=judge_cost,
    )
    cloud_res = cloud_backend.evaluate_traces(spans=spans)

    assert sorted(local_res) == sorted(cloud_res) == ["port-app", "unjudged-app"]
    for app_id in ("port-app", "unjudged-app"):
        loc, cld = local_res[app_id], cloud_res[app_id]
        assert loc.calibration_status == cld.calibration_status
        assert loc.total_traces == cld.total_traces
        assert loc.rule0_pass_rate == pytest.approx(cld.rule0_pass_rate, abs=1e-12)
        assert loc.avg_cache_hit_ratio == pytest.approx(cld.avg_cache_hit_ratio, abs=1e-12)
        assert loc.total_weighted_burndown_tokens == pytest.approx(
            cld.total_weighted_burndown_tokens, abs=1e-12
        )
        assert loc.total_spend_usd == pytest.approx(cld.total_spend_usd, abs=1e-12)
        if loc.calibrated_pass_rate is None:
            assert cld.calibrated_pass_rate is None
            assert cld.cost_per_verified_outcome_usd is None
        else:
            assert cld.calibrated_pass_rate == pytest.approx(loc.calibrated_pass_rate, abs=1e-12)
            assert cld.calibrated_ci_95_lower == pytest.approx(
                loc.calibrated_ci_95_lower, abs=1e-12
            )
            assert cld.calibrated_ci_95_upper == pytest.approx(
                loc.calibrated_ci_95_upper, abs=1e-12
            )
            assert cld.cost_per_verified_outcome_usd == pytest.approx(
                loc.cost_per_verified_outcome_usd, abs=1e-12
            )

    local_hourly = hourly_burndown_rows(local_backend, spans)
    cloud_hourly = hourly_burndown_rows(cloud_backend, spans)
    assert len(local_hourly) == len(cloud_hourly) == 4
    for loc_h, cld_h in zip(local_hourly, cloud_hourly, strict=True):
        assert loc_h.timestamp == cld_h.timestamp
        assert loc_h.tenant == cld_h.tenant
        assert loc_h.model == cld_h.model
        assert loc_h.weighted_burndown_tokens == pytest.approx(
            cld_h.weighted_burndown_tokens, abs=1e-12
        )
        assert loc_h.total_cost_usd == pytest.approx(cld_h.total_cost_usd, abs=1e-12)

    sql = cloud_backend.build_judge_sql(rubric_id="groundedness_4pt_v1", canary_sample_pct=5)
    assert "`demo-proj.agent_eval.v_trace_sql_evaluation`" in sql
    with pytest.raises(ValueError, match="Invalid project_id"):
        BigQueryEvalBackend(project_id="bad; DROP TABLE")
    conn.close()


def test_pipeline_accepts_injected_eval_backend_and_bqaa_cli_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pipeline accepts injected `EvalBackend` and `bq-eval bqaa` runs end-to-end."""
    artifacts = run_showcase_pipeline(corpus="retail-2app", backend=SQLiteEvalBackend())
    assert "order-support-agent" in artifacts.summaries

    t0 = datetime(2026, 11, 21, 14, 0, 0, tzinfo=UTC)
    events = [
        BQAAAgentEvent(
            event_id="evt-cli-1",
            timestamp=t0,
            event_type="INVOCATION_STARTING",
            agent="claims-agent",
            session_id="sess-cli-1",
            invocation_id="inv-cli-1",
            trace_id="trace-bqaa-cli-001",
            span_id="sp-bqaa-inv-1",
            content={"text": "Check claim status for policy P-99."},
            attributes={
                "gcp.apphub.application.id": "claims-triage-app",
                "outcome.id": "claim-P-99",
            },
        ),
        BQAAAgentEvent(
            event_id="evt-cli-2",
            timestamp=t0 + timedelta(milliseconds=100),
            event_type="LLM_REQUEST",
            agent="claims-agent",
            session_id="sess-cli-1",
            invocation_id="inv-cli-1",
            trace_id="trace-bqaa-cli-001",
            span_id="sp-bqaa-llm-1",
            parent_span_id="sp-bqaa-inv-1",
            attributes={
                "gcp.apphub.application.id": "claims-triage-app",
                "outcome.id": "claim-P-99",
            },
        ),
        # Duplicate retry row for same event_id (at-least-once streaming) must be deduplicated.
        BQAAAgentEvent(
            event_id="evt-cli-3",
            timestamp=t0 + timedelta(milliseconds=400),
            event_type="LLM_RESPONSE",
            agent="claims-agent",
            session_id="sess-cli-1",
            invocation_id="inv-cli-1",
            trace_id="trace-bqaa-cli-001",
            span_id="sp-bqaa-llm-1",
            parent_span_id="sp-bqaa-inv-1",
            content={"text": "Policy P-99 is approved."},
            latency_ms={"total_ms": 300},
            attributes={
                "gcp.apphub.application.id": "claims-triage-app",
                "outcome.id": "claim-P-99",
                "model": "gemini-3.8-flash",
                "usage_metadata": {"prompt_token_count": 1200, "candidates_token_count": 80},
            },
        ),
        BQAAAgentEvent(
            event_id="evt-cli-3",
            timestamp=t0 + timedelta(milliseconds=405),
            event_type="LLM_RESPONSE",
            agent="claims-agent",
            session_id="sess-cli-1",
            invocation_id="inv-cli-1",
            trace_id="trace-bqaa-cli-001",
            span_id="sp-bqaa-llm-1",
            parent_span_id="sp-bqaa-inv-1",
            content={"text": "Policy P-99 is approved."},
            latency_ms={"total_ms": 300},
            attributes={
                "gcp.apphub.application.id": "claims-triage-app",
                "outcome.id": "claim-P-99",
                "model": "gemini-3.8-flash",
                "usage_metadata": {"prompt_token_count": 1200, "candidates_token_count": 80},
            },
        ),
        # Shadow event without apphub or outcome.id stays unregistered_app + outcome_id=None.
        BQAAAgentEvent(
            event_id="evt-cli-4",
            timestamp=t0 + timedelta(seconds=5),
            event_type="LLM_RESPONSE",
            agent="shadow-agent",
            session_id="sess-shadow",
            invocation_id="inv-shadow",
            trace_id="trace-bqaa-shadow-001",
            span_id="sp-bqaa-shadow-1",
            content={"text": "Shadow answer."},
            latency_ms={"total_ms": 200},
            attributes={
                "model": "gemini-3.5-flash-lite",
                "usage_metadata": {"prompt_token_count": 600, "candidates_token_count": 40},
            },
        ),
    ]
    events_path = tmp_path / "bqaa_events.jsonl"
    events_path.write_text(
        "\n".join(e.model_dump_json() for e in events) + "\n",
        encoding="utf-8",
    )
    spans_out = tmp_path / "spans.jsonl"
    payloads_out = tmp_path / "payloads.jsonl"

    rc = cli_main(
        [
            "bqaa",
            "--events-file",
            str(events_path),
            "--format",
            "json",
            "--spans-out",
            str(spans_out),
            "--payloads-out",
            str(payloads_out),
        ]
    )
    assert rc == 0
    out_json = json.loads(capsys.readouterr().out)
    assert out_json["events_raw"] == 5
    assert out_json["events_deduped"] == 4
    assert sorted(out_json["summaries"]) == ["claims-triage-app", "unregistered_app"]
    assert spans_out.is_file() and payloads_out.is_file()

    exported_spans = [
        json.loads(line) for line in spans_out.read_text(encoding="utf-8").splitlines()
    ]
    shadow_spans = [s for s in exported_spans if s["trace_id"] == "trace-bqaa-shadow-001"]
    assert len(shadow_spans) == 1
    assert shadow_spans[0]["outcome_id"] is None

    rc_md = cli_main(["bqaa", "--events-file", str(events_path), "--format", "markdown"])
    assert rc_md == 0
    md_out = capsys.readouterr().out
    assert "claims-triage-app" in md_out
    assert "unregistered_app" in md_out

    rc_missing = cli_main(["bqaa", "--events-file", str(tmp_path / "missing.jsonl")])
    assert rc_missing == 2
