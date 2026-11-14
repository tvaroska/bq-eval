"""M5 Dual-Path (`Light Capture BQAA` + `Full Capture OTel`) verification tests.

Verifies:
1. `sql/bqaa_adapter_views.sql` defines `v_bqaa_otel_spans`, `v_bqaa_trace_payloads`, and
   `v_bqaa_eval_turn_filtered`, projects all 25 required `otel_spans` contract columns,
   deduplicates Storage Write API retries by `event_id`, and preserves `ADR-003` strict
   `'unregistered_app'` fallback (never collapsing `gcp_apphub_application_id` or
   `outcome_owner` into self-reported `adk.app_name` or `agent`).
2. `bq_eval/bqaa.py` deduplicates retry events, maps `usage_metadata` (`<=200K` and `>200K`
   context cliffs), honors `attributes.otel.{trace_id, span_id}`, maps `OUTCOME_CLOSED` to
   `outcome.closed`, and feeds `evaluate_traces_in_sqlite()` with strict `'unregistered_app'`
   resolution for unregistered shadow agents.
3. Engine B (`AI.EVALUATE_AGENT`) SQL builder and `bq-eval judge-sql --engine evaluate-agent`
   pre-filter through `v_bqaa_eval_turn_filtered` and `FARM_FINGERPRINT` sampling.
4. Public portability & terminology hygiene: zero hardcoded scratchpad bucket IDs in `sql/`
   and zero `"Path A"` / `"Path B"` capture-lane naming collisions in docs.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq_eval.bqaa import (
    BQAAAgentEvent,
    bqaa_events_to_otel_spans,
    bqaa_events_to_trace_payloads,
    deduplicate_bqaa_events,
)
from bq_eval.calibration import ConfusionMatrix
from bq_eval.cli import main as cli_main
from bq_eval.sql_eval import (
    build_ai_evaluate_agent_sql,
    evaluate_traces_in_sqlite,
)
from tests.test_m1_allspans_compat_views import REQUIRED_OTEL_SPANS_VIEW_COLUMNS

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_bqaa_adapter_sql_contract_and_strict_adr003_fallback() -> None:
    """Verify `sql/bqaa_adapter_views.sql` column parity, dedup, and strict ADR-003 fallback."""
    adapter_sql = (REPO_ROOT / "sql" / "bqaa_adapter_views.sql").read_text(encoding="utf-8")

    for view_name in (
        "agent_eval.v_bqaa_otel_spans",
        "agent_eval.v_bqaa_trace_payloads",
        "agent_eval.v_bqaa_eval_turn_filtered",
    ):
        assert f"CREATE OR REPLACE VIEW `{view_name}`" in adapter_sql

    assert "PARTITION BY event_id ORDER BY timestamp DESC" in adapter_sql

    spans_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`agent_eval\.v_bqaa_otel_spans`\s+AS(.*?);",
        adapter_sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert spans_match is not None
    spans_body = spans_match.group(1)

    for col in REQUIRED_OTEL_SPANS_VIEW_COLUMNS:
        assert re.search(rf"\b{re.escape(col)}\b", spans_body), (
            f"v_bqaa_otel_spans missing required column {col!r}"
        )

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


def test_bqaa_python_twin_dedup_strict_fallback_and_sqlite_eval() -> None:
    """Verify `BQAA` retry dedup, `>200K` cliff, OTel span context, and SQLite evaluation."""
    t0 = datetime(2026, 11, 11, 14, 0, 0, tzinfo=UTC)

    events = [
        # Registered app turn: user prompt -> tool -> stale LLM_RESPONSE -> retried LLM_RESPONSE (>200K) -> response -> outcome.closed
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
        # Duplicate older retry row for ev-llm-01 (should be superseded by newer timestamp)
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
        # Canonical newer Storage Write API retry row for ev-llm-01 (>200K cliff)
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
        # Unregistered shadow ADK agent: sets adk.app_name and agent, but NO custom_metadata apphub ID
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
    assert len(spans) == 4  # tool + deduped llm + outcome.closed + shadow llm

    tool_span = next(s for s in spans if s.tool_name == "fetch_payoff_ledger")
    assert tool_span.span_id == "fedcba9876543210"  # preferred attributes.otel.span_id

    llm_span = next(s for s in spans if s.span_id == "span-llm-01")
    assert llm_span.is_long_context_cliff is True
    assert llm_span.weighted_burndown_tokens == pytest.approx(155_000.0)

    close_span = next(s for s in spans if s.span_name == "outcome.closed")
    assert close_span.outcome_id == "out_deal_101"
    assert close_span.outcome_owner == "meridian-loan-consolidation"

    shadow_span = next(s for s in spans if s.agent_name == "rogue_shadow_agent")
    assert shadow_span.service_name == "self_reported_shadow_app"
    assert shadow_span.gcp_apphub_application_id is None
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
    assert summaries["meridian-loan-consolidation"].calibration_status == "CALIBRATED"
    assert summaries["unregistered_app"].calibration_status == "UNJUDGED"
    assert summaries["unregistered_app"].calibrated_pass_rate is None


def test_ai_evaluate_agent_sql_builder_and_cli(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify Engine B (`AI.EVALUATE_AGENT`) SQL builder and CLI flag."""
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

    rc = cli_main(
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
    assert rc == 0
    captured = capsys.readouterr().out
    assert "AI.EVALUATE_AGENT(" in captured
    assert "MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 5" in captured


def test_public_sql_and_terminology_hygiene() -> None:
    """Ensure SQL files contain no scratchpad project IDs and docs avoid Path A/B collisions."""
    for sql_file in sorted((REPO_ROOT / "sql").glob("*.sql")):
        content = sql_file.read_text(encoding="utf-8")
        assert "btv-scratch" not in content, (
            f"{sql_file.name} leaks scratchpad project ID into public SQL"
        )

    dual_doc = (REPO_ROOT / "docs" / "bqaa-and-dual-path-architecture.md").read_text(
        encoding="utf-8"
    )
    assert "Light Capture" in dual_doc
    assert "Full Capture" in dual_doc
    assert not re.search(r"\bPath A\b|\bPath B\b", dual_doc), (
        "Use 'Light Capture (BQAA)' / 'Full Capture (OTel)' instead of 'Path A' / 'Path B'"
    )
