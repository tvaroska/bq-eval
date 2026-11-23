"""EvalBackend / WarehouseEvalBackend Ports and `bq-eval bqaa` CLI tests."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
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
from bq_eval.core.engine.cost import compute_span_burndown_tokens, is_span_long_context_cliff
from bq_eval.core.engine.ports import (
    BigQueryEvalBackend,
    EvalBackend,
    QueryRunner,
    SQLiteEvalBackend,
    WarehouseEvalBackend,
)
from bq_eval.demo.pipeline import run_showcase_pipeline
from bq_eval.finops.engine.capacity_export import hourly_burndown_rows
from bq_eval.finops.engine.sqlite_capacity import evaluate_hourly_burndown_in_sqlite
from tests.sql_helpers import (
    ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
    ENTERPRISE_OTEL_SPANS_BASE_INSERT,
    _create_enterprise_sqlite_fixture,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
)


def _sqlite_bq_query_runner(conn: sqlite3.Connection) -> QueryRunner:
    def _run(sql: str) -> Sequence[Mapping[str, object]]:
        rewritten = sql.replace("`demo-proj.agent_eval.", "").replace("`", "")
        rows = conn.execute(rewritten).fetchall()
        return [dict(r) for r in rows]

    return _run


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
    assert isinstance(cloud_backend, WarehouseEvalBackend)
    assert not isinstance(cloud_backend, EvalBackend)

    spans, outcomes, cm = _seed_port_parity_fixture(conn)
    judge_cost = (1_000 / 1e6) * 0.15 + (100 / 1e6) * 0.60
    local_res = local_backend.evaluate_traces(
        spans=spans,
        judge_outcomes=outcomes,
        sme_confusion={"port-app": cm},
        judge_cost_per_call_usd=judge_cost,
    )
    cloud_res = cloud_backend.read_summaries(rubric_id="groundedness_4pt_v1")

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


def test_bigquery_adapter_maps_all_spend_buckets() -> None:
    """`BigQueryEvalBackend` maps all spend buckets, rejects NULL rates, and `hourly_burndown_rows` maps all 27 fields (A-7, A-8)."""
    summary_row: dict[str, object] = {
        "apphub_application_id": "spend-app",
        "rubric_id": "groundedness_4pt_v1",
        "total_traces": 10,
        "production_traces": 8,
        "rule0_pass_rate": 0.9,
        "tool_success_rate": 1.0,
        "p95_latency_ms": 400.0,
        "avg_cache_hit_ratio": 0.2,
        "total_weighted_burndown_tokens": 50_000.0,
        "judged_samples": 0,
        "calibration_status": "UNJUDGED",
        "production_inference_usd": 4.0,
        "conflict_escalation_usd": 2.0,
        "agent_gym_eval_usd": 1.0,
        "autorater_judge_usd": 3.0,
        "optimize_usd": 5.0,
        "bq_query_cost_usd": 2.0,
        "unclassified_usd": 0.5,
        "total_spend_usd": 17.5,
    }
    hourly_row: dict[str, object] = {
        "usage_hour": "2026-11-22T12:00:00Z",
        "timestamp": "2026-11-22T12:00:00Z",
        "apphub_application_id": "spend-app",
        "tenant": "spend-app",
        "model_name": "claude-sonnet-4-6",
        "model": "claude-sonnet-4-6",
        "finops_traffic_type": "production_inference",
        "serving_traffic_type": "PRIORITY",
        "distinct_traces": 4,
        "span_count": 6,
        "ok_span_count": 5,
        "error_span_count": 1,
        "http_429_count": 2,
        "spillover_span_count": 3,
        "input_tokens": 10_000,
        "cached_input_tokens": 1_000,
        "cached_tokens": 1_000,
        "cache_write_5m_tokens": 2_000,
        "cache_write_1h_tokens": 500,
        "uncached_input_tokens": 6_500,
        "output_tokens": 400,
        "thinking_tokens": 100,
        "thoughts_tokens": 100,
        "long_context_cliff_spans": 0,
        "weighted_burndown_tokens": 12_500.0,
        "precomputed_weighted_burndown_tokens": 12_500.0,
        "total_cost_usd": 0.42,
    }

    backend = BigQueryEvalBackend(
        project_id="demo-proj",
        query_runner=lambda sql: [hourly_row] if "burndown" in sql else [summary_row],
    )
    summary = backend.read_summaries()["spend-app"]
    bucket_sum = (
        summary.production_inference_usd
        + summary.conflict_escalation_usd
        + summary.agent_gym_eval_usd
        + summary.autorater_judge_usd
        + summary.optimize_usd
        + summary.bq_query_cost_usd
        + summary.unclassified_usd
    )
    assert summary.optimize_usd == pytest.approx(5.0)
    assert summary.bq_query_cost_usd == pytest.approx(2.0)
    assert bucket_sum == pytest.approx(summary.total_spend_usd, abs=1e-12)

    # NULL rule0_pass_rate fails closed instead of silently becoming 0.0
    bad_row = {**summary_row, "rule0_pass_rate": None}
    bad_backend = BigQueryEvalBackend(project_id="demo-proj", query_runner=lambda _: [bad_row])
    with pytest.raises(ValueError, match="rule0_pass_rate"):
        bad_backend.read_summaries()

    # Custom protocol adapter (not BigQueryEvalBackend subclass) works via WarehouseEvalBackend
    class _CustomWarehouse:
        project_id = "demo-proj"
        dataset_id = "agent_eval"

        def run_query(self, sql: str) -> Sequence[Mapping[str, object]]:
            assert "v_capacity_hourly_burndown_export" in sql
            return [hourly_row]

        def read_summaries(self, *, rubric_id: str = "groundedness_4pt_v1") -> dict[str, object]:
            _ = rubric_id
            return {}

        def build_judge_sql(self, **kwargs: object) -> str:
            _ = kwargs
            return ""

    rows = hourly_burndown_rows(_CustomWarehouse())  # type: ignore[arg-type]
    assert len(rows) == 1
    assert rows[0].serving_traffic_type == "PRIORITY"
    assert rows[0].ok_span_count == 5
    assert rows[0].error_span_count == 1
    assert rows[0].http_429_count == 2
    assert rows[0].spillover_span_count == 3
    assert rows[0].cache_write_5m_tokens == 2_000
    assert rows[0].cache_write_1h_tokens == 500

    # A-8: 250K Claude span uses single SKU-aware burndown & cliff check (255,000, not 507,500)
    t0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    claude_span = OTelGenAISpanRecord(
        trace_id="trace-claude-250k",
        span_id="sp-claude-250k",
        start_time=t0,
        end_time=t0 + timedelta(seconds=1),
        latency_ms=1000.0,
        model_name="claude-sonnet-4-6",
        gcp_apphub_application_id="claude-app",
        input_tokens=250_000,
        output_tokens=1_000,
    )
    assert is_span_long_context_cliff(claude_span) is False
    assert compute_span_burndown_tokens(claude_span) == pytest.approx(255_000.0, abs=1e-12)
    sq_hourly = evaluate_hourly_burndown_in_sqlite([claude_span])
    assert sq_hourly[0].long_context_cliff_spans == 0
    assert sq_hourly[0].weighted_burndown_tokens == pytest.approx(255_000.0, abs=1e-12)
