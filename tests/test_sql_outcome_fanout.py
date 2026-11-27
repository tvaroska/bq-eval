"""Gold-on-Silver multi-agent outcome fanout, multi-outcome trace gating (`S-17`), and entry-span infra (`S-20 / A-17`)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from bq_eval.core.contract.otel import FinOpsTrafficType, GenAISpanKind
from bq_eval.core.engine.cost import compute_span_cost_usd
from tests.conftest import SQL_DIR, make_span, open_runner_for_spans


def test_multi_key_outcome_fanout_exact_1x_infra_and_judge_cost() -> None:
    """Multi-agent, multi-source_mode, multi-app & multi-skill slices sum to exact 1.0x outcome cost."""
    cost_sql = (SQL_DIR / "core" / "gold_cost_views.sql").read_text(encoding="utf-8")
    summary_sql = (SQL_DIR / "core" / "summary_views.sql").read_text(encoding="utf-8")

    assert re.search(
        r"GROUP\s+BY\s+w\.outcome_id",
        cost_sql,
        flags=re.IGNORECASE,
    ), "v_infra_cost_estimate must group strictly by w.outcome_id (1:1 outcome key)"
    assert summary_sql.count("OVER (PARTITION BY") >= 4

    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    s1 = make_span(
        "t1",
        "s1",
        app_id="app-sub-a",
        service_name="svc-a",
        agent_name="agent-intake",
        otel_span_kind="SPAN_KIND_SERVER",
        model_name="gemini-3.8-flash",
        finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
        outcome_id="out-multi-001",
        outcome_owner="parent-app",
        source_mode="A2A_PATH_A",
        skill_id="skill-a",
        skill_version="1.0.0",
        latency_ms=1000.0,
        input_tokens=10_000,
        cached_input_tokens=0,
        output_tokens=1_000,
        thinking_tokens=0,
        base_time=base,
    )
    s1_child = make_span(
        "t1",
        "s1-child",
        parent_span_id="s1",
        span_name="execute_tool",
        app_id="app-sub-a",
        service_name="svc-a",
        agent_name="agent-intake",
        otel_span_kind="SPAN_KIND_INTERNAL",
        span_kind=GenAISpanKind.EXECUTE_TOOL,
        model_name="none",
        tool_name="lookup",
        tool_call_status="SUCCESS",
        outcome_id="out-multi-001",
        outcome_owner="parent-app",
        source_mode="A2A_PATH_A",
        skill_id="skill-a",
        skill_version="1.0.0",
        latency_ms=900.0,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        offset_ms=50,
        base_time=base,
    )
    s2 = make_span(
        "t2",
        "s2",
        app_id="app-sub-b",
        service_name="svc-b",
        agent_name="agent-extract",
        otel_span_kind="SPAN_KIND_SERVER",
        model_name="gemini-3.8-flash",
        finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
        outcome_id="out-multi-001",
        outcome_owner="parent-app",
        source_mode="PDF_PATH_B_FLASH",
        skill_id="skill-a",
        skill_version="1.0.0",
        latency_ms=1000.0,
        input_tokens=20_000,
        cached_input_tokens=0,
        output_tokens=2_000,
        thinking_tokens=0,
        offset_s=2,
        base_time=base,
    )
    s3 = make_span(
        "t3",
        "s3",
        app_id="app-sub-a",
        service_name="svc-a",
        agent_name="agent-adjudicate",
        otel_span_kind="SPAN_KIND_SERVER",
        model_name="gemini-4-argon",
        finops_traffic_type=FinOpsTrafficType.CONFLICT_ESCALATION,
        outcome_id="out-multi-001",
        outcome_owner="parent-app",
        source_mode="PDF_PATH_B_FLASH",
        skill_id="skill-b",
        skill_version="1.1.0",
        latency_ms=1000.0,
        input_tokens=30_000,
        cached_input_tokens=0,
        output_tokens=3_000,
        thinking_tokens=0,
        offset_s=4,
        attributes={"hitl.review": "true"},
        base_time=base,
    )
    s4 = make_span(
        "t4",
        "s4",
        app_id="app-sub-b",
        service_name="svc-b",
        agent_name="agent-adjudicate",
        otel_span_kind="SPAN_KIND_SERVER",
        model_name="gemini-3.8-flash",
        finops_traffic_type=FinOpsTrafficType.AUTORATER_JUDGE,
        outcome_id="out-multi-001",
        outcome_owner="parent-app",
        source_mode="PDF_PATH_B_FLASH",
        skill_id="skill-b",
        skill_version="1.1.0",
        latency_ms=1000.0,
        input_tokens=5_000,
        cached_input_tokens=0,
        output_tokens=500,
        thinking_tokens=0,
        offset_s=6,
        base_time=base,
    )
    s_close = make_span(
        "t4",
        "s4-close",
        parent_span_id="s4",
        span_name="outcome.closed",
        app_id="parent-app",
        outcome_id="out-multi-001",
        outcome_owner="parent-app",
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        latency_ms=0.0,
        offset_s=7,
        attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
        base_time=base,
    )

    expected_model_cost = sum(compute_span_cost_usd(sp) for sp in (s1, s2, s3))
    expected_judge_cost = compute_span_cost_usd(s4)

    with open_runner_for_spans([s1, s1_child, s2, s3, s4, s_close]) as runner:
        infra_rows = runner.query(
            "SELECT outcome_id, total_server_seconds, estimated_infra_cost_usd "
            "FROM `agent_eval.v_infra_cost_estimate`"
        ).rows
        assert len(infra_rows) == 1
        assert float(str(infra_rows[0]["total_server_seconds"])) == pytest.approx(4.0, abs=1e-12)
        expected_infra_usd = float(str(infra_rows[0]["estimated_infra_cost_usd"]))
        assert expected_infra_usd == pytest.approx(4 * 0.000053 + 0.000020, abs=1e-12)

        cpvo_sum = runner.query(
            "SELECT SUM(estimated_infra_cost_usd) AS infra, "
            "SUM(measured_model_cost_usd) AS model_usd, "
            "SUM(measured_judge_cost_usd) AS judge_usd "
            "FROM `agent_eval.v_cost_per_verified_outcome`"
        ).rows[0]
        assert float(str(cpvo_sum["infra"])) == pytest.approx(expected_infra_usd, abs=1e-12)
        assert float(str(cpvo_sum["model_usd"])) == pytest.approx(expected_model_cost, abs=1e-12)
        assert float(str(cpvo_sum["judge_usd"])) == pytest.approx(expected_judge_cost, abs=1e-12)

        skill_sum = runner.query(
            "SELECT SUM(estimated_infra_cost_usd) AS infra, "
            "SUM(measured_model_cost_usd) AS model_usd "
            "FROM `agent_eval.v_skill_lineage_roi`"
        ).rows[0]
        assert float(str(skill_sum["infra"])) == pytest.approx(expected_infra_usd, abs=1e-12)
        assert float(str(skill_sum["model_usd"])) == pytest.approx(
            expected_model_cost + expected_judge_cost, abs=1e-12
        )

        runner.load_rows(
            "silver_internal.workflow_events_sor",
            [
                {
                    "event_id": "ev-link-a",
                    "event_time": "2026-11-21T12:00:08+00:00",
                    "event_type": "outcome.linked",
                    "source_system": "sor",
                    "trace_id": "t1",
                    "span_id": None,
                    "response_id": None,
                    "outcome_id": "out-multi-001",
                    "conversation_id": None,
                    "action_id": None,
                    "nudge_id": None,
                    "control_id": None,
                    "control_layer": None,
                    "control_action": None,
                    "action_args": None,
                    "approved_expires_at": None,
                    "user_id": None,
                    "tenant_id": None,
                    "actor_id": "sor",
                    "payload_json": {"linked_outcome_id": "out-child-a"},
                },
                {
                    "event_id": "ev-link-b",
                    "event_time": "2026-11-21T12:00:08+00:00",
                    "event_type": "outcome.linked",
                    "source_system": "sor",
                    "trace_id": "t1",
                    "span_id": None,
                    "response_id": None,
                    "outcome_id": "out-multi-001",
                    "conversation_id": None,
                    "action_id": None,
                    "nudge_id": None,
                    "control_id": None,
                    "control_layer": None,
                    "control_action": None,
                    "action_args": None,
                    "approved_expires_at": None,
                    "user_id": None,
                    "tenant_id": None,
                    "actor_id": "sor",
                    "payload_json": {"linked_outcome_id": "out-child-b"},
                },
            ],
            append=False,
        )
        linked_sum = runner.query(
            "SELECT SUM(measured_model_cost_usd) AS model_usd, "
            "SUM(measured_judge_cost_usd) AS judge_usd, "
            "SUM(estimated_infra_cost_usd) AS infra_usd "
            "FROM `agent_eval.v_enterprise_cost_per_verified_outcome`"
        ).rows[0]
        assert float(str(linked_sum["model_usd"])) == pytest.approx(expected_model_cost, abs=1e-12)
        assert float(str(linked_sum["judge_usd"])) == pytest.approx(expected_judge_cost, abs=1e-12)
        assert float(str(linked_sum["infra_usd"])) == pytest.approx(expected_infra_usd, abs=1e-12)


def test_multi_outcome_trace_gates_every_outcome() -> None:
    """A Rule-0-failing trace touching multiple outcomes blocks `is_verified` on every linked `(outcome_id, trace_id)` (`S-17`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-clean-only",
            "sp-clean-1",
            app_id="multi-out-app",
            outcome_id="out-clean",
            outcome_owner="multi-out-app",
            status_code="OK",
            rule0_pass=True,
            base_time=base,
        ),
        make_span(
            "tr-sec-healthy",
            "sp-sec-1",
            app_id="multi-out-app",
            outcome_id="out-secondary",
            outcome_owner="multi-out-app",
            status_code="OK",
            rule0_pass=True,
            offset_s=1,
            base_time=base,
        ),
        make_span(
            "tr-shared-fail",
            "sp-shared-fail",
            app_id="multi-out-app",
            outcome_id="out-primary",
            outcome_owner="multi-out-app",
            status_code="ERROR",
            rule0_pass=False,
            offset_s=2,
            base_time=base,
        ),
    ]
    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "silver_internal.workflow_events_sor",
            [
                {
                    "event_id": "ev-close-clean",
                    "event_time": "2026-11-21T12:00:10+00:00",
                    "event_type": "outcome.closed",
                    "source_system": "sor",
                    "trace_id": "tr-clean-only",
                    "span_id": None,
                    "response_id": None,
                    "outcome_id": "out-clean",
                    "conversation_id": None,
                    "action_id": None,
                    "nudge_id": None,
                    "control_id": None,
                    "control_layer": None,
                    "control_action": None,
                    "action_args": None,
                    "approved_expires_at": None,
                    "user_id": None,
                    "tenant_id": None,
                    "actor_id": "sor",
                    "payload_json": {
                        "outcome_status": "succeeded",
                        "outcome_owner": "multi-out-app",
                    },
                },
                {
                    "event_id": "ev-close-prim",
                    "event_time": "2026-11-21T12:00:10+00:00",
                    "event_type": "outcome.closed",
                    "source_system": "sor",
                    "trace_id": "tr-shared-fail",
                    "span_id": None,
                    "response_id": None,
                    "outcome_id": "out-primary",
                    "conversation_id": None,
                    "action_id": None,
                    "nudge_id": None,
                    "control_id": None,
                    "control_layer": None,
                    "control_action": None,
                    "action_args": None,
                    "approved_expires_at": None,
                    "user_id": None,
                    "tenant_id": None,
                    "actor_id": "sor",
                    "payload_json": {
                        "outcome_status": "succeeded",
                        "outcome_owner": "multi-out-app",
                    },
                },
                {
                    "event_id": "ev-close-sec",
                    "event_time": "2026-11-21T12:00:10+00:00",
                    "event_type": "outcome.closed",
                    "source_system": "sor",
                    "trace_id": "tr-shared-fail",
                    "span_id": None,
                    "response_id": None,
                    "outcome_id": "out-secondary",
                    "conversation_id": None,
                    "action_id": None,
                    "nudge_id": None,
                    "control_id": None,
                    "control_layer": None,
                    "control_action": None,
                    "action_args": None,
                    "approved_expires_at": None,
                    "user_id": None,
                    "tenant_id": None,
                    "actor_id": "sor",
                    "payload_json": {
                        "outcome_status": "succeeded",
                        "outcome_owner": "multi-out-app",
                    },
                },
            ],
            append=False,
        )
        outcomes = {
            str(r["outcome_id"]): bool(r["is_verified"])
            for r in runner.query(
                "SELECT outcome_id, is_verified FROM `agent_eval.v_outcomes`"
            ).rows
        }

    assert outcomes == {
        "out-clean": True,
        "out-primary": False,
        "out-secondary": False,
    }


def test_infra_cost_numeric_kind_and_nested_server() -> None:
    """`v_infra_cost_estimate` normalizes numeric `'1'`/`'2'`, counts 1 entry span per `(trace_id, service_name)`, and prices sandboxes (`S-20 / A-17`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-internal-root",
            "sp-int-root",
            app_id="infra-app",
            service_name="svc-worker",
            otel_span_kind="1",
            span_kind=GenAISpanKind.EXECUTE_TOOL,
            model_name="none",
            tool_name="code_exec",
            tool_call_status="SUCCESS",
            outcome_id="out-int-1",
            outcome_owner="infra-app",
            latency_ms=2000.0,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            attributes={
                "bq_eval.sandbox.cpu_sec": 10.0,
                "bq_eval.sandbox.mem_gb_sec": 4.0,
            },
            base_time=base,
        ),
        make_span(
            "tr-nested-srv",
            "sp-srv-root",
            app_id="infra-app",
            service_name="svc-gateway",
            otel_span_kind="2",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            outcome_id="out-nested-1",
            outcome_owner="infra-app",
            latency_ms=3000.0,
            offset_s=5,
            base_time=base,
        ),
        make_span(
            "tr-nested-srv",
            "sp-srv-nested-same",
            parent_span_id="sp-srv-root",
            app_id="infra-app",
            service_name="svc-gateway",
            otel_span_kind="2",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            outcome_id="out-nested-1",
            outcome_owner="infra-app",
            latency_ms=2500.0,
            offset_s=5,
            offset_ms=100,
            base_time=base,
        ),
        make_span(
            "tr-nested-srv",
            "sp-srv-downstream",
            parent_span_id="sp-srv-root",
            app_id="infra-app",
            service_name="svc-downstream",
            otel_span_kind="2",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            outcome_id="out-nested-1",
            outcome_owner="infra-app",
            latency_ms=1000.0,
            offset_s=5,
            offset_ms=200,
            base_time=base,
        ),
    ]
    with open_runner_for_spans(spans) as runner:
        rows = {
            str(r["outcome_id"]): dict(r)
            for r in runner.query("SELECT * FROM `agent_eval.v_infra_cost_estimate`").rows
        }

        raw_num = runner.query(
            "SELECT "
            "CASE UPPER(TRIM( k )) "
            "WHEN '1' THEN 'INTERNAL' WHEN '2' THEN 'SERVER' ELSE 'OTHER' END AS norm "
            "FROM (SELECT '1' AS k UNION ALL SELECT '2' AS k)"
        ).rows
        assert [str(r["norm"]) for r in raw_num] == ["INTERNAL", "SERVER"]

    expected_sandbox = (10.0 * 0.000024) + (4.0 * 0.0000025)
    assert float(str(rows["out-int-1"]["total_server_seconds"])) == pytest.approx(2.0, abs=1e-12)
    assert float(str(rows["out-int-1"]["estimated_compute_usd"])) == pytest.approx(
        2.0 * 0.000053 + expected_sandbox, abs=1e-12
    )

    assert float(str(rows["out-nested-1"]["total_server_seconds"])) == pytest.approx(4.0, abs=1e-12)
    assert float(str(rows["out-nested-1"]["estimated_infra_cost_usd"])) == pytest.approx(
        4.0 * 0.000053 + 0.000020, abs=1e-12
    )
    assert json.dumps(list(rows.keys())) == '["out-int-1", "out-nested-1"]'
