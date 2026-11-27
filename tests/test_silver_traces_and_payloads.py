"""Silver M18-8 (`traces`, `traces_open`) and M18-9 (`trace_payloads`) verification tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.loader import OfflineSqlRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
SILVER_SQL_DIR = REPO_ROOT / "sql" / "silver"
SCHEMA_DIR = SILVER_SQL_DIR / "schema"
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "silver" / "manifest.json"
ALL_ADMIN_USER = "admin-all@example.invalid"


def _schema_columns(name: str) -> tuple[str, ...]:
    raw = json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return tuple(str(col["name"]) for col in raw)


def _setup_runner(session_user: str = ALL_ADMIN_USER) -> OfflineSqlRunner:
    runner = OfflineSqlRunner(
        RunnerConfig(
            current_date_utc="2026-11-26",
            current_timestamp_utc="2026-11-26T18:00:00Z",
            session_user=session_user,
        )
    )
    runner.load_silver_manifest(MANIFEST_PATH, schema_dir=SCHEMA_DIR)
    if session_user == ALL_ADMIN_USER:
        runner.load_rows(
            "silver_internal.unit_entitlements",
            [
                {"principal_email": ALL_ADMIN_USER, "business_unit": bu, "tenant_id": "*"}
                for bu in ("operations", "retail", "platform", "wealth", "__unowned__")
            ],
            append=True,
        )
    for sql_file in (
        "jcs.sql",
        "spans.sql",
        "model_calls.sql",
        "tool_calls.sql",
        "events.sql",
        "trace_aux.sql",
        "traces.sql",
        "trace_payloads.sql",
    ):
        runner.register_silver_file(SILVER_SQL_DIR / sql_file)
    return runner


def _by_prefix(by_id: dict[str, dict[str, Any]], prefix: str) -> dict[str, Any]:
    matches = [row for tid, row in by_id.items() if tid.startswith(prefix)]
    assert len(matches) == 1, f"Expected 1 match for prefix {prefix!r}, got {len(matches)}"
    return matches[0]


def _assert_recorded_trace_rows(by_id: dict[str, dict[str, Any]]) -> None:
    rt_only = _by_prefix(by_id, "b2800d7f")
    assert rt_only["has_root_span"] == 0 and rt_only["root_span_id"] is None
    assert rt_only["is_turn"] == 0 and rt_only["trace_start_writer"] == "runtime"
    assert rt_only["unregistered_app"] == 1 and rt_only["app_identity_bound"] is None
    assert rt_only["entry_parent_outside"] is None and rt_only["sampling_probability"] is None
    assert rt_only["model_call_count"] == 0 and rt_only["user_turn_count"] == 0
    assert rt_only["duplicate_span_count"] == 1
    assert rt_only["input_tokens"] is None and rt_only["output_tokens"] is None

    adk1 = _by_prefix(by_id, "64080a2b")
    assert adk1["trace_owner_app_id"] == "bq-eval-adk-v1" and adk1["app_identity_bound"] == 1
    assert adk1["has_root_span"] == 1 and adk1["is_turn"] == 1
    assert adk1["trace_start_writer"] == "application" and adk1["span_count"] == 7
    assert adk1["model_call_count"] == 2 and adk1["tool_call_count"] == 1
    assert adk1["user_turn_count"] == 1 and adk1["input_tokens"] > 0
    assert adk1["sampling_probability"] is None

    kyc = _by_prefix(by_id, "003466f0")
    assert kyc["trace_start_writer"] == "application" and kyc["has_root_span"] == 1
    assert kyc["trace_owner_app_id"] == "kyc-address-verification"
    assert kyc["duplicate_span_count"] == 3 and kyc["content_missing_call_count"] >= 2
    assert kyc["user_turn_count"] == 1


def _assert_platform_and_edge_trace_rows(
    by_id: dict[str, dict[str, Any]],
    open_by_id: dict[str, dict[str, Any]],
) -> None:
    pay = by_id["bbbb0000000000000000000000000001"]
    assert pay["trace_owner_app_id"] == "payments-agent" and pay["app_identity_bound"] == 1
    assert pay["business_unit"] == "retail" and pay["user_id"] == "user-pay-42"
    assert pay["platform_user_id"] == "user-pay-42" and pay["user_id_source"] == "platform"
    assert pay["identity_attested"] == 1 and pay["tool_call_count"] == 2
    assert pay["mutating_tool_call_count"] == 1 and pay["model_call_count"] == 1
    assert pay["guardrail_intervention_count"] == 1

    edge = by_id["cccc0000000000000000000000000001"]
    assert edge["trace_start_writer"] == "runtime" and edge["has_root_span"] == 1
    assert edge["root_span_id"] == "c100000000000001" and edge["sampling_probability"] == 0.5
    assert edge["user_id"] == "app-user-1" and edge["platform_user_id"] == "verified-platform-user"
    assert edge["user_id_source"] == "application" and edge["identity_attested"] == 0
    assert edge["user_id_conflict_count"] == 2 and edge["tenant_id"] == "tenant-gw-01"
    assert edge["traffic_type"] == "production_inference"
    assert edge["skill_name"] == "unregistered_custom_skill" and edge["unregistered_skill"] == 1
    assert edge["multiple_outcome_ids"] == 1 and edge["entry_parent_outside"] == 1
    assert edge["missing_parent_count"] == 2 and edge["exceeds_read_margin"] == 1
    assert edge["final_model_confidence"] == 0.94 and edge["escalated"] == 1
    assert edge["app_failed"] == 1 and edge["app_failure_reason"] == "downstream_timeout"
    assert edge["user_turn_count"] == 2

    edge_open = open_by_id["cccc0000000000000000000000000001"]
    assert edge_open["trace_owner_app_id"] == "wealth-advisor-uc9"


def test_traces_rows() -> None:
    """`traces` and `traces_open` enforce root rule, completeness, precedence, skills, and counts."""
    with _setup_runner() as runner:
        # Inject synthetic edge-case traces covering W3C trace_state, platform vs root user_id
        # conflict (ADR-034 / S-17), unregistered skill, multi-outcome, missing non-server parent,
        # exceeds_read_margin, and multi-user-turn trace.
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c100000000000000",
                    "parent_span_id": None,
                    "name": "cloud_run.ingress",
                    "kind": None,
                    "start_time": "2026-11-25 10:59:59 UTC",
                    "end_time": "2026-11-25 12:15:00 UTC",
                    "receive_time": "2026-11-25 12:15:02 UTC",
                    "status": {"code": "0", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": None, "version": None},
                    "events": [],
                    "attributes": "{}",
                    "resource": {"attributes": "{}"},
                },
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c100000000000001",
                    "parent_span_id": "external-caller-01",
                    "name": "invoke_agent advisor",
                    "kind": "1",
                    "start_time": "2026-11-25 11:00:00 UTC",
                    "end_time": "2026-11-25 12:15:00 UTC",
                    "receive_time": "2026-11-25 12:15:02 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "vendor=1,ot=th:8",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "invoke_agent",
                            "gen_ai.agent.name": "advisor",
                            "gen_ai.skill.name": "unregistered_custom_skill",
                            "user.id": "app-user-1",
                            "outcome.id": "out-multi-1",
                            "bq_eval.app_failed": True,
                            "bq_eval.app_failure_reason": "downstream_timeout",
                        }
                    ),
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "wealth-advisor-uc9",
                                "service.name": "wealth-advisor-svc",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                },
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c100000000000002",
                    "parent_span_id": "missing-middle-span",
                    "name": "generate_content gemini-3.8-flash",
                    "kind": "1",
                    "start_time": "2026-11-25 11:00:01 UTC",
                    "end_time": "2026-11-25 11:00:02 UTC",
                    "receive_time": "2026-11-25 11:00:03 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.request.model": "gemini-3.8-flash",
                            "gen_ai.usage.input_tokens": 50,
                            "gen_ai.usage.output_tokens": 20,
                            "bq_eval.model_confidence": 0.88,
                            "bq_eval.cascade_tier": "escalation",
                            "user.id": "app-user-conflict",
                            "outcome.id": "out-multi-2",
                            "gen_ai.input.messages": json.dumps(
                                [{"role": "user", "parts": [{"type": "text", "content": "Q1"}]}]
                            ),
                        }
                    ),
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "wealth-advisor-uc9",
                                "service.name": "wealth-advisor-svc",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                },
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c100000000000003",
                    "parent_span_id": "c100000000000001",
                    "name": "generate_content gemini-3.8-flash",
                    "kind": "1",
                    "start_time": "2026-11-25 11:00:05 UTC",
                    "end_time": "2026-11-25 11:00:06 UTC",
                    "receive_time": "2026-11-25 11:00:07 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.request.model": "gemini-3.8-flash",
                            "gen_ai.usage.input_tokens": 70,
                            "gen_ai.usage.output_tokens": 30,
                            "bq_eval.model_confidence": 0.94,
                            "gen_ai.input.messages": json.dumps(
                                [
                                    {"role": "user", "parts": [{"type": "text", "content": "Q1"}]},
                                    {
                                        "role": "assistant",
                                        "parts": [{"type": "text", "content": "A1"}],
                                    },
                                    {"role": "user", "parts": [{"type": "text", "content": "Q2"}]},
                                ]
                            ),
                        }
                    ),
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "wealth-advisor-uc9",
                                "service.name": "wealth-advisor-svc",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                },
            ],
            append=True,
        )
        runner.load_rows(
            "platform_traces._AllSpans",
            [
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c100000000000004",
                    "parent_span_id": "c100000000000001",
                    "name": "platform.auth_check",
                    "kind": "2",
                    "start_time": "2026-11-25 11:00:00.500000 UTC",
                    "end_time": "2026-11-25 11:00:00.600000 UTC",
                    "receive_time": "2026-11-25 11:00:01 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.gateway_proxy", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "user.id": "verified-platform-user",
                            "bq_eval.tenant_id": "tenant-gw-01",
                            "bq_eval.traffic_type": "production_inference",
                        }
                    ),
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "service.name": "central-ai-gateway",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                }
            ],
            append=True,
        )

        res_restricted = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_restricted.traces`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY trace_id
            """
        )
        res_open = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_open.traces_open`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY trace_id
            """
        )

        assert res_restricted.columns == _schema_columns("traces")
        assert res_open.columns == _schema_columns("traces_open")
        assert len(res_restricted.rows) == 11
        assert len(res_open.rows) == 11

        by_id = {str(row["trace_id"]): row for row in res_restricted.rows}
        open_by_id = {str(row["trace_id"]): row for row in res_open.rows}

        _assert_recorded_trace_rows(by_id)
        _assert_platform_and_edge_trace_rows(by_id, open_by_id)

        # Open twin drops restricted columns cleanly while matching open counts/facts.
        edge_open = open_by_id["cccc0000000000000000000000000001"]
        for restricted_col in (
            "user_id",
            "platform_user_id",
            "tenant_id",
            "act_chain",
            "attachment_sha256",
            "app_failure_reason",
        ):
            assert restricted_col not in edge_open
        assert edge_open["skill_name"] == "unregistered_custom_skill"
        assert edge_open["sampling_probability"] == 0.5
        assert edge_open["user_turn_count"] == 2

        # Unregistered gateway caller_app_id must stay unregistered even if the gateway proxy itself is registered.
        runner.load_rows(
            "silver_ref.app_registry",
            [
                {
                    "app_id": "central-ai-gateway",
                    "apphub_application_id": "central-ai-gateway",
                    "service_name": "central-ai-gateway",
                    "environment": "prod",
                    "allowed_landing_places": ["showcase-project.platform_traces._AllSpans"],
                    "owner": "team-platform",
                    "business_unit": "platform",
                    "risk_tier": "RL-4",
                    "payload_profile": "ephemeral_sample",
                    "sampling_mode": "full",
                    "effective_from": "2026-11-01 00:00:00 UTC",
                    "effective_to": None,
                }
            ],
            append=True,
        )
        runner.load_rows(
            "platform_traces._AllSpans",
            [
                {
                    "trace_id": "99990000000000000000000000000001",
                    "span_id": "9900000000000001",
                    "parent_span_id": None,
                    "name": "gateway.dispatch",
                    "kind": "2",
                    "start_time": "2026-11-25 11:20:00 UTC",
                    "end_time": "2026-11-25 11:20:01 UTC",
                    "receive_time": "2026-11-25 11:20:02 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.gateway_proxy", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps({"bq_eval.caller_app_id": "shadow-unregistered-bot"}),
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "central-ai-gateway",
                                "service.name": "central-ai-gateway",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                }
            ],
            append=True,
        )
        dark_res = runner.query(
            """
            SELECT trace_owner_app_id, unregistered_app, business_unit, app_identity_bound
            FROM `showcase-project.silver_open.traces_open`(
              TIMESTAMP '2026-11-25 11:00:00 UTC',
              TIMESTAMP '2026-11-25 12:00:00 UTC'
            )
            WHERE trace_id = '99990000000000000000000000000001'
            """
        )
        assert dark_res.rows == (
            {
                "trace_owner_app_id": None,
                "unregistered_app": 1,
                "business_unit": None,
                "app_identity_bound": None,
            },
        )


def test_trace_payloads_rows() -> None:
    """`trace_payloads` extracts ADK v1, OTel inline, Shape-3, prior_history, and sends_external_reply."""
    with _setup_runner() as runner:
        # Add a multi-turn inline trace with prior_history, media_uris, nudge_id, and
        # sends_external_reply tool call (`send_customer_email` is in synthetic tools or register it).
        runner.load_rows(
            "silver_ref.tools",
            [
                {
                    "app_id": "wealth-advisor-uc9",
                    "tool_name": "send_client_reply",
                    "effective_from": "2026-11-01 00:00:00 UTC",
                    "effective_to": None,
                    "writes_state": False,
                    "modifies_workspace": False,
                    "executes_code": False,
                    "sends_external_reply": True,
                    "returns_pii": False,
                    "creates_ticket": False,
                }
            ],
            append=True,
        )
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "dddd0000000000000000000000000001",
                    "span_id": "d100000000000001",
                    "parent_span_id": None,
                    "name": "invoke_agent advisor",
                    "kind": "1",
                    "start_time": "2026-11-25 11:10:00 UTC",
                    "end_time": "2026-11-25 11:10:05 UTC",
                    "receive_time": "2026-11-25 11:10:06 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "invoke_agent",
                            "gen_ai.agent.name": "advisor",
                            "bq_eval.document.page_count": 4,
                            "bq_eval.document.bytes": 12000,
                            "bq_eval.attachment_sha256": "aa" * 32,
                        }
                    ),
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "wealth-advisor-uc9",
                                "service.name": "wealth-advisor-svc",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                },
                {
                    "trace_id": "dddd0000000000000000000000000001",
                    "span_id": "d100000000000002",
                    "parent_span_id": "d100000000000001",
                    "name": "generate_content gemini-3.8-flash",
                    "kind": "1",
                    "start_time": "2026-11-25 11:10:01 UTC",
                    "end_time": "2026-11-25 11:10:02 UTC",
                    "receive_time": "2026-11-25 11:10:03 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.request.model": "gemini-3.8-flash",
                            "gen_ai.usage.input_tokens": 120,
                            "gen_ai.usage.output_tokens": 30,
                            "bq_eval.nudge_id": "nudge-reply-1",
                            "gen_ai.input.messages": json.dumps(
                                [
                                    {
                                        "role": "user",
                                        "parts": [{"type": "text", "content": "Turn 1 hello"}],
                                    },
                                    {
                                        "role": "assistant",
                                        "parts": [{"type": "text", "content": "Turn 1 hi"}],
                                    },
                                    {
                                        "role": "user",
                                        "parts": [
                                            {"type": "text", "content": "Email my summary"},
                                            {
                                                "type": "file",
                                                "uri": "gs://showcase-project-docs/statement.pdf",
                                            },
                                        ],
                                    },
                                ]
                            ),
                            "gen_ai.output.messages": json.dumps(
                                [
                                    {
                                        "role": "assistant",
                                        "parts": [{"type": "text", "content": "Draft model text"}],
                                    }
                                ]
                            ),
                        }
                    ),
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "wealth-advisor-uc9",
                                "service.name": "wealth-advisor-svc",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                },
                {
                    "trace_id": "dddd0000000000000000000000000001",
                    "span_id": "d100000000000003",
                    "parent_span_id": "d100000000000001",
                    "name": "execute_tool send_client_reply",
                    "kind": "1",
                    "start_time": "2026-11-25 11:10:03 UTC",
                    "end_time": "2026-11-25 11:10:04 UTC",
                    "receive_time": "2026-11-25 11:10:05 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": "send_client_reply",
                            "gen_ai.tool.call.arguments": json.dumps(
                                {"body": "Sent portfolio summary."}
                            ),
                            "gen_ai.tool.call.result": json.dumps({"delivered": True}),
                        }
                    ),
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "wealth-advisor-uc9",
                                "service.name": "wealth-advisor-svc",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                },
            ],
            append=True,
        )

        res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_restricted.trace_payloads`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY trace_id
            """
        )
        assert res.columns == _schema_columns("trace_payloads")
        by_id = {str(row["trace_id"]): row for row in res.rows}

        # Content-missing and non-turn traces must NOT appear in `trace_payloads` (`ADR-036`).
        for absent_prefix in ("f64f78b6", "003466f0", "09587491", "b2800d7f"):
            assert not any(tid.startswith(absent_prefix) for tid in by_id)

        # Recorded ADK Format 1 success trace (`64080a2b`).
        adk1 = _by_prefix(by_id, "64080a2b")
        assert adk1["prior_history"] is None
        assert adk1["prior_history_read_whole"] is None
        assert "POL-42" in json.dumps(json.loads(adk1["turn_prompt"]))
        assert adk1["turn_prompt_read_whole"] == 1
        ctx1 = json.loads(adk1["turn_context"])
        assert len(ctx1) == 1 and ctx1[0]["tool_name"] == "lookup_policy"
        assert ctx1[0]["result"]["limit_usd"] == 500
        assert adk1["turn_context_read_whole"] == 1
        out1 = json.loads(adk1["turn_output"])
        assert "500 USD" in json.dumps(out1)
        assert adk1["turn_output_read_whole"] == 1
        steps1 = json.loads(adk1["step_outputs"])
        assert len(steps1) == 2
        assert adk1["step_outputs_read_whole"] == 1
        assert adk1["system_instructions"] is not None
        assert adk1["tool_definitions"] is not None

        # Recorded ADK Format 2 `SPAN_ONLY` success trace (`616b8e81`).
        adk2 = _by_prefix(by_id, "616b8e81")
        assert adk2["prior_history"] is None
        assert "POL-42" in json.dumps(json.loads(adk2["turn_prompt"]))
        assert adk2["turn_prompt_read_whole"] == 1
        ctx2 = json.loads(adk2["turn_context"])
        assert len(ctx2) == 1 and ctx2[0]["tool_name"] == "lookup_policy"
        assert "500 USD" in json.dumps(json.loads(adk2["turn_output"]))
        assert adk2["turn_output_read_whole"] == 1
        assert len(json.loads(adk2["step_outputs"])) == 2

        # Recorded ADK Format 2 404 error trace (`081f625e`): input present, output absent.
        adk2_err = _by_prefix(by_id, "081f625e")
        assert adk2_err["turn_prompt_read_whole"] == 1
        assert adk2_err["turn_output"] is None
        assert adk2_err["turn_output_read_whole"] is None

        # Multi-turn inline trace: prior_history split, media_uris, nudge_id, sends_external_reply.
        multi: dict[str, Any] = by_id["dddd0000000000000000000000000001"]
        assert len(json.loads(multi["prior_history"])) == 2
        assert multi["prior_history_read_whole"] == 1
        assert "Email my summary" in json.dumps(json.loads(multi["turn_prompt"]))
        assert json.loads(multi["turn_output"]) == {"body": "Sent portfolio summary."}
        assert multi["turn_output_read_whole"] == 1
        steps_m = json.loads(multi["step_outputs"])
        assert steps_m[0]["nudge_id"] == "nudge-reply-1"
        media_m = json.loads(multi["media_uris"])
        assert media_m[0]["uri"] == "gs://showcase-project-docs/statement.pdf"
        assert media_m[0]["page_count"] == 4
        assert media_m[0]["document_bytes"] == 12000
