"""Dual-Path (`Light Capture BQAA` + `Full Capture OTel`) verification tests."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import BQAAAgentEvent
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.bqaa import (
    bqaa_events_to_otel_spans,
    bqaa_events_to_trace_payloads,
    deduplicate_bqaa_events,
)
from bq_eval.core.engine.cost import compute_span_burndown_tokens, is_span_long_context_cliff
from bq_eval.core.engine.sql_eval import build_ai_evaluate_agent_sql, evaluate_traces_in_sqlite
from tests.conftest import REQUIRED_OTEL_SPANS_VIEW_COLUMNS

REPO_ROOT = Path(__file__).resolve().parent.parent


def _projected_columns_of_view(sql_text: str, view_name: str) -> list[str]:
    stmts = [s for s in sqlglot.parse(sql_text, read="bigquery") if s is not None]
    for stmt in stmts:
        if isinstance(stmt, exp.Create) and view_name in stmt.this.sql("bigquery"):
            select_node = stmt.expression
            assert isinstance(select_node, exp.Select)
            return [expr.alias_or_name for expr in select_node.expressions]
    raise AssertionError(f"View {view_name!r} not found")


def test_bqaa_adapter_sql_contract_and_strict_fallback() -> None:
    """Verify `sql/core/bqaa_adapter_views.sql` 39/39 ordered column parity, live Mode A/C views, dedup, and strict fallback."""
    adapter_sql = (REPO_ROOT / "sql" / "core" / "bqaa_adapter_views.sql").read_text(
        encoding="utf-8"
    )
    compat_sql = (REPO_ROOT / "sql" / "core" / "otel_compat_views.sql").read_text(encoding="utf-8")
    schema_cols = tuple(
        col["name"]
        for col in json.loads(
            (REPO_ROOT / "sql" / "core" / "otel_spans_schema.json").read_text(encoding="utf-8")
        )
    )
    assert len(REQUIRED_OTEL_SPANS_VIEW_COLUMNS) == 39
    assert REQUIRED_OTEL_SPANS_VIEW_COLUMNS == schema_cols

    for view_name in (
        "agent_eval.v_bqaa_otel_spans",
        "agent_eval.v_bqaa_trace_payloads",
        "agent_eval.v_bqaa_eval_turn_filtered",
        "agent_eval.v_mode_a_otel_spans",
        "agent_eval.v_mode_a_trace_payloads",
        "agent_eval.v_mode_c_hybrid_otel_spans",
        "agent_eval.v_mode_c_hybrid_trace_payloads",
    ):
        assert f"CREATE OR REPLACE VIEW `{view_name}`" in adapter_sql

    assert "PARTITION BY event_id ORDER BY timestamp DESC" in adapter_sql

    compat_allspans_cols = _projected_columns_of_view(compat_sql, "agent_eval.v_otel_allspans_only")
    bqaa_cols = _projected_columns_of_view(adapter_sql, "agent_eval.v_bqaa_otel_spans")
    expected_cols = list(REQUIRED_OTEL_SPANS_VIEW_COLUMNS)
    assert compat_allspans_cols == expected_cols
    assert bqaa_cols == expected_cols
    assert "SELECT * FROM `agent_eval.v_otel_allspans_only`" in compat_sql
    assert "SELECT * FROM `agent_eval.v_otel_allspans_only`" in adapter_sql
    assert "FROM `agent_eval.v_bqaa_otel_spans`" in adapter_sql

    spans_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`agent_eval\.v_bqaa_otel_spans`\s+AS(.*?);",
        adapter_sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert spans_match is not None
    spans_body = spans_match.group(1)

    apphub_expr_match = re.search(
        r"COALESCE\(((?:(?!COALESCE\().)*?)\)\s+AS\s+gcp_apphub_application_id",
        spans_body,
        re.DOTALL,
    )
    assert apphub_expr_match is not None
    apphub_expr = apphub_expr_match.group(1)
    assert "custom_metadata" in apphub_expr
    assert "adk.app_name" not in apphub_expr
    assert "root_agent_name" not in apphub_expr

    owner_expr_match = re.search(
        r"COALESCE\(((?:(?!COALESCE\().)*?)\)\s+AS\s+outcome_owner",
        spans_body,
        re.DOTALL,
    )
    assert owner_expr_match is not None
    owner_expr = owner_expr_match.group(1)
    assert "adk.app_name" not in owner_expr
    assert not re.search(r"\bagent\b", owner_expr)


def test_bqaa_mode_a_and_mode_c_transpile_to_sqlite_with_null_shadow_outcome_and_tool_error() -> (
    None
):
    """Transpile shipped Mode A and Mode C span & payload views directly in SQLite."""
    from tests.sql_helpers import (
        _extract_view_sql_text,
        _new_sqlite_bq_conn,
        _transpile_bq_view_to_sqlite,
    )

    adapter_path = REPO_ROOT / "sql" / "core" / "bqaa_adapter_views.sql"
    compat_path = REPO_ROOT / "sql" / "core" / "otel_compat_views.sql"
    bqaa_view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(adapter_path, "agent_eval.v_bqaa_otel_spans")
    )
    bqaa_payloads_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(adapter_path, "agent_eval.v_bqaa_trace_payloads")
    )
    allspans_view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(compat_path, "agent_eval.v_otel_allspans_only")
        .replace("`obs_traces._AllSpans`", "`_AllSpans`")
        .replace("status.code", "status_code_raw")
        .replace("resource.attributes", "resource_attributes")
    )
    logs_payloads_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(compat_path, "agent_eval.v_otel_logs_payloads_only")
        .replace("`obs_logs._AllLogs`", "`_AllLogs`")
        .replace("l.resource.labels", "l.resource_labels")
    )
    mode_a_spans_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(adapter_path, "agent_eval.v_mode_a_otel_spans")
    )
    mode_a_payloads_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(adapter_path, "agent_eval.v_mode_a_trace_payloads")
    )
    mode_c_spans_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(adapter_path, "agent_eval.v_mode_c_hybrid_otel_spans")
    )
    mode_c_payloads_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(adapter_path, "agent_eval.v_mode_c_hybrid_trace_payloads")
    )

    conn = _new_sqlite_bq_conn()
    conn.execute(
        """
        CREATE TABLE agent_events (
            event_id TEXT,
            timestamp TEXT,
            event_type TEXT,
            agent TEXT,
            session_id TEXT,
            invocation_id TEXT,
            trace_id TEXT,
            span_id TEXT,
            parent_span_id TEXT,
            content TEXT,
            attributes TEXT,
            latency_ms TEXT,
            status TEXT,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE _AllSpans (
            trace_id TEXT,
            span_id TEXT,
            parent_span_id TEXT,
            name TEXT,
            start_time TEXT,
            end_time TEXT,
            receive_time TEXT,
            duration_nano INTEGER,
            status_code_raw TEXT,
            kind TEXT,
            resource_attributes TEXT,
            attributes TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE _AllLogs (
            trace TEXT,
            timestamp TEXT,
            resource_labels TEXT,
            json_payload TEXT
        )
        """
    )
    conn.execute(
        "CREATE TABLE completions (_FILE_NAME TEXT, payload TEXT, user_prompt TEXT, retrieved_context TEXT, final_response TEXT)"
    )
    conn.executemany(
        "INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "ev-shared-user",
                "2026-11-21T12:00:00+00:00",
                "USER_MESSAGE_RECEIVED",
                "shared_agent",
                "s1",
                "inv-1",
                "0123456789abcdef0123456789abcdef",
                None,
                None,
                json.dumps({"text": "BQAA prompt"}),
                json.dumps({"custom_metadata": {"apphub.application.id": "bqaa-app"}}),
                None,
                "OK",
                None,
            ),
            (
                "ev-shared",
                "2026-11-21T12:00:01+00:00",
                "LLM_RESPONSE",
                "shared_agent",
                "s1",
                "inv-1",
                "0123456789abcdef0123456789abcdef",
                "sp-bqaa-shared",
                None,
                "{}",
                json.dumps(
                    {
                        "model": "gemini-3.8-flash",
                        "custom_metadata": {
                            "apphub.application.id": "bqaa-app",
                            "outcome.id": "out-shared",
                        },
                    }
                ),
                '{"total_ms": 100}',
                "OK",
                None,
            ),
            (
                "ev-shadow-user",
                "2026-11-21T12:00:01+00:00",
                "USER_MESSAGE_RECEIVED",
                "shadow_agent",
                "s2",
                "inv-shadow",
                "tr-bqaa-only",
                None,
                None,
                json.dumps({"text": "Shadow prompt"}),
                "{}",
                None,
                "OK",
                None,
            ),
            (
                "ev-shadow-err",
                "2026-11-21T12:00:02+00:00",
                "TOOL_ERROR",
                "shadow_agent",
                "s2",
                "inv-shadow",
                "tr-bqaa-only",
                "sp-bqaa-err",
                None,
                json.dumps({"name": "lookup_policy", "code": 503}),
                json.dumps({"tool.call.status": "SUCCESS"}),
                '{"total_ms": 250}',
                "ERROR",
                "upstream 503 timeout",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO _AllSpans VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "0123456789abcdef0123456789abcdef",
            "sp-otel-shared",
            None,
            "invoke_agent",
            "2026-11-21T12:00:00+00:00",
            "2026-11-21T12:00:01+00:00",
            "2026-11-21T12:00:01+00:00",
            1_000_000_000,
            "OK",
            "SPAN_KIND_SERVER",
            json.dumps({"service.name": "srv-otel", "apphub.application.id": "otel-stamped-app"}),
            json.dumps(
                {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.response.model": "gemini-3.8-flash",
                    "outcome.id": "out-shared",
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO _AllLogs VALUES (?,?,?,?)",
        (
            "projects/p/traces/0123456789abcdef0123456789abcdef",
            "2026-11-21T12:00:01+00:00",
            json.dumps({"apphub.application.id": "otel-stamped-app"}),
            json.dumps(
                {
                    "gen_ai.prompt": "OTel prompt wins",
                    "gen_ai.retrieved_context": "otel ctx",
                    "gen_ai.completion": "otel resp",
                }
            ),
        ),
    )

    conn.execute(f"CREATE VIEW v_bqaa_otel_spans AS {bqaa_view_sql}")
    conn.execute(f"CREATE VIEW v_bqaa_trace_payloads AS {bqaa_payloads_sql}")
    conn.execute(f"CREATE VIEW v_otel_allspans_only AS {allspans_view_sql}")
    conn.execute(f"CREATE VIEW v_otel_logs_payloads_only AS {logs_payloads_sql}")
    conn.execute(f"CREATE VIEW v_mode_a_otel_spans AS {mode_a_spans_sql}")
    conn.execute(f"CREATE VIEW v_mode_a_trace_payloads AS {mode_a_payloads_sql}")
    conn.execute(f"CREATE VIEW v_mode_c_hybrid_otel_spans AS {mode_c_spans_sql}")
    conn.execute(f"CREATE VIEW v_mode_c_hybrid_trace_payloads AS {mode_c_payloads_sql}")

    mode_a_spans = {
        str(r["trace_id"]): r for r in conn.execute("SELECT * FROM v_mode_a_otel_spans").fetchall()
    }
    mode_a_payloads = {
        str(r["trace_id"]): r
        for r in conn.execute("SELECT * FROM v_mode_a_trace_payloads").fetchall()
    }
    mode_c_spans = {
        str(r["trace_id"]): r
        for r in conn.execute("SELECT * FROM v_mode_c_hybrid_otel_spans").fetchall()
    }
    mode_c_payloads = {
        str(r["trace_id"]): r
        for r in conn.execute("SELECT * FROM v_mode_c_hybrid_trace_payloads").fetchall()
    }
    conn.close()

    shared_id = "0123456789abcdef0123456789abcdef"
    assert set(mode_a_spans) == {shared_id, "tr-bqaa-only"}
    assert mode_a_spans["tr-bqaa-only"]["outcome_id"] is None
    assert mode_a_spans["tr-bqaa-only"]["tool_call_status"] == "TIMEOUT"
    assert mode_a_spans["tr-bqaa-only"]["gcp_apphub_application_id"] is None
    assert mode_a_payloads[shared_id]["user_prompt"] == "BQAA prompt"
    assert mode_a_payloads["tr-bqaa-only"]["apphub_application_id"] == "unregistered_app"

    assert set(mode_c_spans) == {shared_id, "tr-bqaa-only"}
    assert mode_c_spans[shared_id]["span_id"] == "sp-otel-shared"
    assert mode_c_spans[shared_id]["gcp_apphub_application_id"] == "otel-stamped-app"
    assert mode_c_spans["tr-bqaa-only"]["outcome_id"] is None
    assert mode_c_spans["tr-bqaa-only"]["tool_call_status"] == "TIMEOUT"

    assert set(mode_c_payloads) == {shared_id, "tr-bqaa-only"}
    assert mode_c_payloads[shared_id]["user_prompt"] == "OTel prompt wins"
    assert mode_c_payloads[shared_id]["apphub_application_id"] == "otel-stamped-app"
    assert mode_c_payloads["tr-bqaa-only"]["user_prompt"] == "Shadow prompt"
    assert mode_c_payloads["tr-bqaa-only"]["apphub_application_id"] == "unregistered_app"


def test_bqaa_python_twin_dedup_strict_fallback_and_sqlite_eval() -> None:
    """Verify `BQAA` retry dedup, `>200K` cliff, OTel span context, and SQLite evaluation."""
    t0 = datetime(2026, 11, 11, 14, 0, 0, tzinfo=UTC)

    events = [
        BQAAAgentEvent(
            event_id="ev-user-01",
            timestamp=t0,
            event_type="USER_MESSAGE_RECEIVED",
            agent="underwriter_adk",
            session_id="sess-101",
            invocation_id="inv-101",
            trace_id="0123456789abcdef0123456789abcdef",
            content={"text": "Verify payoff letter #7712"},
            attributes={
                "custom_metadata": {
                    "apphub.application.id": "meridian-loan-consolidation",
                    "outcome.id": "out_deal_101",
                }
            },
        ),
        BQAAAgentEvent(
            event_id="ev-tool-01",
            timestamp=t0 + timedelta(milliseconds=180),
            event_type="TOOL_COMPLETED",
            agent="underwriter_adk",
            session_id="sess-101",
            invocation_id="inv-101",
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="bqaa-uuid-tool",
            latency_ms={"total_ms": 120},
            content={"name": "fetch_payoff_ledger", "response": "Payoff $1,089,790.00"},
            attributes={
                "otel": {
                    "trace_id": "0123456789abcdef0123456789abcdef",
                    "span_id": "fedcba9876543210",
                },
                "custom_metadata": {
                    "apphub.application.id": "meridian-loan-consolidation",
                    "outcome.id": "out_deal_101",
                },
            },
        ),
        BQAAAgentEvent(
            event_id="ev-llm-01",
            timestamp=t0 + timedelta(milliseconds=500),
            event_type="LLM_RESPONSE",
            agent="underwriter_adk",
            session_id="sess-101",
            invocation_id="inv-101",
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="span-llm-01",
            latency_ms={"total_ms": 300},
            attributes={
                "model_version": "gemini-4-argon",
                "usage_metadata": {
                    "prompt_token_count": 1_000,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 10,
                    "thoughts_token_count": 0,
                },
                "custom_metadata": {
                    "apphub.application.id": "meridian-loan-consolidation",
                    "outcome.id": "out_deal_101",
                },
            },
        ),
        BQAAAgentEvent(
            event_id="ev-llm-01",
            timestamp=t0 + timedelta(milliseconds=900),
            event_type="LLM_RESPONSE",
            agent="underwriter_adk",
            session_id="sess-101",
            invocation_id="inv-101",
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="span-llm-01",
            latency_ms={"total_ms": 680},
            attributes={
                "model_version": "gemini-4-argon",
                "usage_metadata": {
                    "prompt_token_count": 250_000,
                    "cached_content_token_count": 200_000,
                    "candidates_token_count": 1_000,
                    "thoughts_token_count": 1_000,
                },
                "custom_metadata": {
                    "apphub.application.id": "meridian-loan-consolidation",
                    "finops.traffic_type": "conflict_escalation",
                    "outcome.id": "out_deal_101",
                },
            },
        ),
        BQAAAgentEvent(
            event_id="ev-resp-01",
            timestamp=t0 + timedelta(milliseconds=950),
            event_type="AGENT_RESPONSE",
            agent="underwriter_adk",
            session_id="sess-101",
            invocation_id="inv-101",
            trace_id="0123456789abcdef0123456789abcdef",
            content={"text": "Verified payoff $1,089,790.00."},
            attributes={
                "custom_metadata": {
                    "apphub.application.id": "meridian-loan-consolidation",
                    "outcome.id": "out_deal_101",
                }
            },
        ),
        BQAAAgentEvent(
            event_id="ev-close-01",
            timestamp=t0 + timedelta(seconds=1),
            event_type="OUTCOME_CLOSED",
            agent="underwriter_adk",
            session_id="sess-101",
            invocation_id="inv-101",
            trace_id="0123456789abcdef0123456789abcdef",
            span_id="span-close-01",
            attributes={
                "custom_metadata": {
                    "apphub.application.id": "meridian-loan-consolidation",
                    "outcome.id": "out_deal_101",
                    "outcome.status": "succeeded",
                }
            },
        ),
        BQAAAgentEvent(
            event_id="ev-shadow-01",
            timestamp=t0 + timedelta(seconds=2),
            event_type="LLM_RESPONSE",
            agent="rogue_shadow_agent",
            session_id="sess-shadow",
            invocation_id="inv-shadow-99",
            latency_ms={"total_ms": 210},
            attributes={
                "adk": {"app_name": "self_reported_shadow_app"},
                "root_agent_name": "rogue_root",
                "model": "gemini-3.8-flash",
                "usage_metadata": {
                    "prompt_token_count": 4_000,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 200,
                },
            },
        ),
    ]

    deduped = deduplicate_bqaa_events(events)
    assert len(deduped) == 6

    spans = bqaa_events_to_otel_spans(events)
    assert len(spans) == 4

    tool_span = next(s for s in spans if s.tool_name == "fetch_payoff_ledger")
    assert tool_span.span_id == "fedcba9876543210"

    llm_span = next(s for s in spans if s.span_id == "span-llm-01")
    assert is_span_long_context_cliff(llm_span) is True
    assert compute_span_burndown_tokens(llm_span) == pytest.approx(155_000.0)

    close_span = next(s for s in spans if s.span_name == "outcome.closed")
    assert close_span.outcome_id == "out_deal_101"
    assert close_span.outcome_owner == "meridian-loan-consolidation"

    shadow_span = next(s for s in spans if s.agent_name == "rogue_shadow_agent")
    assert shadow_span.service_name == "self_reported_shadow_app"
    assert shadow_span.gcp_apphub_application_id is None
    assert shadow_span.outcome_id is None
    assert shadow_span.outcome_owner is None
    assert shadow_span.resolved_application_id == "unregistered_app"

    payloads = bqaa_events_to_trace_payloads(events)
    by_trace = {p.trace_id: p for p in payloads}
    deal_payload = by_trace["0123456789abcdef0123456789abcdef"]
    assert deal_payload.apphub_application_id == "meridian-loan-consolidation"
    assert deal_payload.user_prompt == "Verify payoff letter #7712"
    assert deal_payload.retrieved_context == "Payoff $1,089,790.00"
    assert deal_payload.final_response == "Verified payoff $1,089,790.00."
    assert by_trace["inv-shadow-99"].apphub_application_id == "unregistered_app"

    summaries = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes={"0123456789abcdef0123456789abcdef": True},
        sme_confusion=ConfusionMatrix(tp=87, fn=2, tn=7, fp=4),
    )
    assert set(summaries) == {"meridian-loan-consolidation", "unregistered_app"}
    # Single judged trace fails closed under the minimum-n >= 10 calibration guard.
    assert summaries["meridian-loan-consolidation"].judged_samples == 1
    assert summaries["meridian-loan-consolidation"].calibration_status == "UNCALIBRATED"
    assert summaries["meridian-loan-consolidation"].calibrated_pass_rate is None
    assert summaries["unregistered_app"].calibration_status == "UNJUDGED"
    assert summaries["unregistered_app"].calibrated_pass_rate is None


def test_ai_evaluate_agent_sql_builder_and_cli_engine_b_default_canary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Engine B (`AI.EVALUATE_AGENT`) SQL builder and CLI default canary (`10%`)."""
    sql = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        dataset_id="agent_eval",
        canary_sample_pct=10,
    )
    assert "AI.EVALUATE_AGENT(" in sql
    assert "`demo-proj.agent_eval.v_bqaa_eval_turn_filtered`" in sql
    assert "MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 10" in sql
    assert "'tool_use_quality_v1'" in sql

    with pytest.raises(ValueError, match=r"Unknown AI\.EVALUATE_AGENT"):
        build_ai_evaluate_agent_sql(
            project_id="demo-proj",
            managed_metrics=("invalid_metric_v99",),
        )

    rc_default = cli_main(
        [
            "judge-sql",
            "--engine",
            "evaluate-agent",
            "--project-id",
            "demo-proj",
        ]
    )
    assert rc_default == 0
    default_out = capsys.readouterr().out
    assert "AI.EVALUATE_AGENT(" in default_out
    assert "MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 10" in default_out

    rc_override = cli_main(
        [
            "judge-sql",
            "--engine",
            "evaluate-agent",
            "--project-id",
            "demo-proj",
            "--canary-pct",
            "5",
        ]
    )
    assert rc_override == 0
    captured = capsys.readouterr().out
    assert "MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 5" in captured


def test_public_sql_and_terminology_hygiene() -> None:
    """Ensure SQL files contain no scratchpad project IDs."""
    for sql_file in sorted((REPO_ROOT / "sql").rglob("*.sql")):
        content = sql_file.read_text(encoding="utf-8")
        assert "btv-scratch" not in content, (
            f"{sql_file.name} leaks scratchpad project ID into public SQL"
        )
