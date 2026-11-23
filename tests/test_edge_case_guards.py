"""Regression guards for pricing/input edge cases, CLI errors, receipt verification, and BQAA codes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import BQAAAgentEvent, OTelGenAISpanRecord
from bq_eval.core.engine.bqaa import bqaa_events_to_otel_spans
from bq_eval.core.engine.cost import compute_span_burndown_tokens, compute_span_cost_usd
from bq_eval.core.engine.sql_eval import evaluate_traces_in_sqlite
from bq_eval.demo.bundles import verify_receipt_sha256
from bq_eval.finops.engine.spend import allocate_focus_hourly_chargeback
from tests.conftest import make_span
from tests.sql_helpers import (
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
)


def test_1p_long_context_cache_write_cliff_pricing_parity() -> None:
    """1P `>200K` cache-write tokens price at cliff tier and 3P stays flat across Python, SQLite, and SQL."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    span_1p = OTelGenAISpanRecord(
        trace_id="tr-cw-1p",
        span_id="sp-cw-1p",
        start_time=base,
        end_time=base + timedelta(seconds=1),
        latency_ms=1000.0,
        gcp_apphub_application_id="cw-1p-app",
        model_name="gemini-3.8-flash",
        input_tokens=250_000,
        cached_input_tokens=50_000,
        cache_write_5m_tokens=40_000,
        cache_write_1h_tokens=10_000,
        output_tokens=1_000,
        thinking_tokens=0,
    )
    span_3p = span_1p.model_copy(
        update={
            "trace_id": "tr-cw-3p",
            "span_id": "sp-cw-3p",
            "gcp_apphub_application_id": "cw-3p-app",
            "model_name": "claude-sonnet-4-6",
        }
    )

    # 1P cliff: 150K*2.0 + 50K*0.2 + 40K*2.0 + 10K*2.0 + 1K*7.5 = 417,500
    # 1P cliff USD: (150K*0.30 + 50K*0.03 + 40K*0.30 + 10K*0.30 + 1K*2.25) / 1e6 = 0.06375
    assert compute_span_burndown_tokens(span_1p) == pytest.approx(417_500.0, abs=1e-12)
    assert compute_span_cost_usd(span_1p) == pytest.approx(0.06375, abs=1e-12)

    # 3P flat: 150K*1.0 + 50K*0.1 + 40K*1.25 + 10K*2.0 + 1K*5.0 = 230,000
    # 3P flat USD: (150K*3.00 + 50K*0.30 + 40K*3.75 + 10K*6.00 + 1K*15.00) / 1e6 = 0.690
    assert compute_span_burndown_tokens(span_3p) == pytest.approx(230_000.0, abs=1e-12)
    assert compute_span_cost_usd(span_3p) == pytest.approx(0.690, abs=1e-12)

    twin = evaluate_traces_in_sqlite(spans=[span_1p, span_3p])
    assert twin["cw-1p-app"].total_weighted_burndown_tokens == pytest.approx(417_500.0, abs=1e-12)
    assert twin["cw-1p-app"].production_inference_usd == pytest.approx(0.06375, abs=1e-12)
    assert twin["cw-3p-app"].total_weighted_burndown_tokens == pytest.approx(230_000.0, abs=1e-12)
    assert twin["cw-3p-app"].production_inference_usd == pytest.approx(0.690, abs=1e-12)

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    insert_enterprise_sqlite_span(conn, span_1p)
    insert_enterprise_sqlite_span(conn, span_3p)

    for view_name in ("v_step1_trace_waterfall", "v_trace_waterfall"):
        rows = {
            str(r["trace_id"]): r
            for r in conn.execute(
                f"SELECT trace_id, weighted_burndown_tokens, span_cost_usd FROM {view_name}"  # noqa: S608
            ).fetchall()
        }
        assert float(rows["tr-cw-1p"]["weighted_burndown_tokens"]) == pytest.approx(
            417_500.0, abs=1e-12
        )
        assert float(rows["tr-cw-1p"]["span_cost_usd"]) == pytest.approx(0.06375, abs=1e-12)
        assert float(rows["tr-cw-3p"]["weighted_burndown_tokens"]) == pytest.approx(
            230_000.0, abs=1e-12
        )
        assert float(rows["tr-cw-3p"]["span_cost_usd"]) == pytest.approx(0.690, abs=1e-12)
    conn.close()


def test_model_none_with_tokens_fails_closed_and_overflow_default_aligned() -> None:
    """Token-bearing `none`/empty model spans raise `ValueError`; hourly overflow defaults to `$0.15/1M`."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    zero_span = make_span(
        "tr-none-0",
        "sp-none-0",
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        base_time=base,
    )
    assert compute_span_cost_usd(zero_span) == 0.0

    with pytest.raises(ValueError, match="Cannot price non-zero tokens"):
        compute_span_cost_usd(zero_span.model_copy(update={"input_tokens": 1_000}))

    with pytest.raises(ValueError, match="Cannot price non-zero tokens"):
        compute_span_cost_usd(
            zero_span.model_copy(update={"input_tokens": 500, "cache_write_5m_tokens": 500})
        )

    with pytest.raises(ValueError, match="Cannot price non-zero tokens"):
        compute_span_cost_usd(zero_span.model_copy(update={"model_name": "", "output_tokens": 10}))

    rows = allocate_focus_hourly_chargeback(
        hourly_burndown_by_app={"app-a": [2_000_000.0]},
        app_cost_centers={"app-a": "CC-A"},
        hourly_reserved_pt_capacity_tokens=1_000_000.0,
        reserved_pt_fixed_cost_usd=10.0,
    )
    by_app = {r.apphub_application_id: r for r in rows}
    assert by_app["app-a"].payg_overflow_cost_usd == pytest.approx(0.15, abs=1e-12)


def test_unjudged_rubric_id_preserved_and_negative_infra_attributes_guarded() -> None:
    """Unjudged SQLite rows honor `rubric_id`, and negative infra attributes fall back cleanly."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    root = make_span(
        "tr-neg-infra",
        "sp-neg-root",
        app_id="neg-infra-app",
        outcome_id="out-neg-1",
        outcome_owner="neg-infra-app",
        input_tokens=10_000,
        cached_input_tokens=0,
        output_tokens=1_000,
        thinking_tokens=0,
        latency_ms=1000.0,
        attributes={
            "service.vcpu": "-4.0",
            "service.gib": "0",
            "outcome.storage_usd": "-0.50",
        },
        base_time=base,
    )
    close = make_span(
        "tr-neg-infra",
        "sp-neg-close",
        parent_span_id="sp-neg-root",
        span_name="outcome.closed",
        app_id="neg-infra-app",
        outcome_id="out-neg-1",
        outcome_owner="neg-infra-app",
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        latency_ms=0.0,
        offset_s=1,
        attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
        base_time=base,
    )

    twin = evaluate_traces_in_sqlite(
        spans=[root, close],
        rubric_id="completeness_4pt_v1",
    )["neg-infra-app"]
    assert twin.calibration_status == "UNJUDGED"
    assert twin.rubric_id == "completeness_4pt_v1"

    # Expected fallback: 1.0s * (2.0 * 0.000024 + 2.0 * 0.0000025) + 0.000020 = 0.000073
    expected_infra = 0.000073
    expected_ai = compute_span_cost_usd(root)
    assert twin.outcome_cost_per_verified_ai_usd == pytest.approx(expected_ai, abs=1e-12)
    assert twin.outcome_cost_per_verified_usd == pytest.approx(
        expected_ai + expected_infra, abs=1e-12
    )

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    insert_enterprise_sqlite_span(conn, root)
    insert_enterprise_sqlite_span(conn, close)
    inf_row = conn.execute(
        "SELECT estimated_compute_usd, estimated_storage_usd, estimated_infra_cost_usd "
        "FROM v_infra_cost_estimate WHERE outcome_id = 'out-neg-1'"
    ).fetchone()
    conn.close()

    assert float(inf_row["estimated_compute_usd"]) == pytest.approx(0.000053, abs=1e-12)
    assert float(inf_row["estimated_storage_usd"]) == pytest.approx(0.000020, abs=1e-12)
    assert float(inf_row["estimated_infra_cost_usd"]) == pytest.approx(expected_infra, abs=1e-12)


def test_verify_receipt_sha256_fails_closed_on_missing_manifest_or_entry(tmp_path: Path) -> None:
    """`verify_receipt_sha256` fails closed when `SHA256SUMS` is missing or omits the receipt."""
    receipt = tmp_path / "custom_receipt.json"
    receipt.write_text('{"ok": true}\n', encoding="utf-8")
    missing_manifest = tmp_path / "SHA256SUMS"

    with pytest.raises(ValueError, match="SHA-256 manifest file not found"):
        verify_receipt_sha256(receipt, missing_manifest)

    manifest_without_entry = tmp_path / "SHA256SUMS"
    manifest_without_entry.write_text(f"{'a' * 64}  other_file.json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"has no entry for custom_receipt\.json"):
        verify_receipt_sha256(receipt, manifest_without_entry)


def test_zero_span_scenario_file_exits_2_not_stop_iteration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A zero-span `--scenario-file` exits cleanly with status 2 instead of `StopIteration`."""
    empty_bundle = tmp_path / "empty_bundle.json"
    empty_bundle.write_text(
        json.dumps({"corpus_id": "empty-probe", "title": "Empty", "spans": []}),
        encoding="utf-8",
    )
    rc = cli_main(["demo", "--scenario-file", str(empty_bundle), "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "zero spans" in captured.err


def test_bqaa_string_typed_status_code_and_shared_pricing_view() -> None:
    """BQAA string `content.code` `'429'`/`'503'` matches SQL, and adapter uses `v_default_model_pricing`."""
    adapter_sql = (SQL_DIR / "core" / "bqaa_adapter_views.sql").read_text(encoding="utf-8")
    assert "default_skus AS (" not in adapter_sql
    assert "`agent_eval.v_default_model_pricing`" in adapter_sql

    t0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    events = [
        BQAAAgentEvent(
            event_id="ev-str-429",
            timestamp=t0,
            event_type="TOOL_ERROR",
            agent="quota_agent",
            invocation_id="inv-str-1",
            trace_id="tr-str-429",
            span_id="sp-str-429",
            content={"name": "fetch_quote", "code": "429"},
            attributes={"custom_metadata": {"apphub.application.id": "str-code-app"}},
            latency_ms={"total_ms": 120.0},
            status="ERROR",
            error_message="upstream overloaded",
        ),
        BQAAAgentEvent(
            event_id="ev-str-503",
            timestamp=t0 + timedelta(seconds=1),
            event_type="TOOL_ERROR",
            agent="quota_agent",
            invocation_id="inv-str-2",
            trace_id="tr-str-503",
            span_id="sp-str-503",
            content={"name": "fetch_quote", "code": "503"},
            attributes={"custom_metadata": {"apphub.application.id": "str-code-app"}},
            latency_ms={"total_ms": 200.0},
            status="ERROR",
            error_message="gateway error",
        ),
    ]
    py_spans = {s.trace_id: s for s in bqaa_events_to_otel_spans(events)}
    assert py_spans["tr-str-429"].tool_call_status == "RATE_LIMITED"
    assert py_spans["tr-str-429"].http_status_code == 429
    assert py_spans["tr-str-503"].tool_call_status == "TIMEOUT"
    assert py_spans["tr-str-503"].http_status_code == 503

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
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
    adapter_path = SQL_DIR / "core" / "bqaa_adapter_views.sql"
    for vname in ("agent_eval.v_bqaa_otel_spans", "agent_eval.v_bqaa_eval_turn_filtered"):
        short = vname.split(".", maxsplit=1)[1]
        conn.execute(
            f"CREATE VIEW {short} AS "
            f"{_transpile_bq_view_to_sqlite(_extract_view_sql_text(adapter_path, vname))}"
        )
    for ev in events:
        conn.execute(
            "INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ev.event_id,
                ev.timestamp.isoformat(),
                ev.event_type,
                ev.agent,
                ev.session_id,
                ev.invocation_id,
                ev.trace_id,
                ev.span_id,
                ev.parent_span_id,
                json.dumps(ev.content),
                json.dumps(ev.attributes),
                json.dumps(ev.latency_ms),
                ev.status,
                ev.error_message,
            ),
        )
    # Also seed a passing 3P Claude turn to prove v_bqaa_eval_turn_filtered prices via v_default_model_pricing
    conn.execute(
        "INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ev-claude-ok",
            (t0 + timedelta(seconds=2)).isoformat(),
            "LLM_RESPONSE",
            "claude_agent",
            "s-c",
            "inv-c",
            "tr-claude-ok",
            "sp-claude-ok",
            None,
            "{}",
            json.dumps(
                {
                    "model": "claude-sonnet-4-6",
                    "usage_metadata": {
                        "prompt_token_count": 100_000,
                        "candidates_token_count": 10_000,
                    },
                    "custom_metadata": {"apphub.application.id": "claude-app"},
                }
            ),
            '{"total_ms": 150}',
            "OK",
            None,
        ),
    )
    conn.execute("INSERT INTO agent_events_eval_turn VALUES ('tr-claude-ok', 'turn-1', 0)")

    sql_rows = {
        str(r["trace_id"]): r
        for r in conn.execute(
            "SELECT trace_id, tool_call_status, http_status_code FROM v_bqaa_otel_spans"
        ).fetchall()
    }
    turn_row = conn.execute(
        "SELECT trace_cost_usd FROM v_bqaa_eval_turn_filtered WHERE trace_id = 'tr-claude-ok'"
    ).fetchone()
    conn.close()

    assert sql_rows["tr-str-429"]["tool_call_status"] == "RATE_LIMITED"
    assert int(sql_rows["tr-str-429"]["http_status_code"]) == 429
    assert sql_rows["tr-str-503"]["tool_call_status"] == "TIMEOUT"
    assert int(sql_rows["tr-str-503"]["http_status_code"]) == 503
    # 100K * $3.00/1M + 10K * $15.00/1M = $0.30 + $0.15 = $0.45
    assert float(turn_row["trace_cost_usd"]) == pytest.approx(0.45, abs=1e-12)
