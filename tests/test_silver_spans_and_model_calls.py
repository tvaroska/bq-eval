"""Silver M18-4 & M18-5 tests: `spans`, `model_calls`, and SCD-2 reference lookups."""

from __future__ import annotations

import json
from pathlib import Path

from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.loader import OfflineSqlRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
SILVER_SQL_DIR = REPO_ROOT / "sql" / "silver"
SCHEMA_DIR = SILVER_SQL_DIR / "schema"
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "silver" / "manifest.json"


def _schema_columns(name: str) -> tuple[str, ...]:
    cols = json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return tuple(str(c["name"]) for c in cols)


def _new_silver_runner() -> OfflineSqlRunner:
    runner = OfflineSqlRunner(
        RunnerConfig(
            current_date_utc="2026-11-26",
            current_timestamp_utc="2026-11-26 12:00:00 UTC",
            session_user="gold-eval-retail@showcase-project.iam.gserviceaccount.com",
        )
    )
    runner.load_silver_manifest(MANIFEST_PATH, schema_dir=SCHEMA_DIR)
    runner.register_silver_file(SILVER_SQL_DIR / "spans.sql")
    runner.register_silver_file(SILVER_SQL_DIR / "model_calls.sql")
    return runner


def _assert_shape_and_folding_spans(by_key: dict[tuple[str, str], dict[str, object]]) -> None:
    outer_llm = by_key[("64080a2b2c98df9f78fa47303c4a2333", "899e55fdac8f9afc")]
    inner_gc = by_key[("64080a2b2c98df9f78fa47303c4a2333", "4dc6d4987898ce84")]
    assert outer_llm["is_model_call"] == 0
    assert inner_gc["is_model_call"] == 1 and inner_gc["shape"] == "adk_legacy"
    assert inner_gc["content_missing"] == 0 and inner_gc["input_tokens"] == 82

    adk_v2 = by_key[("616b8e81296c5f90d74eee04610a695d", "ab5e59b40913b4c0")]
    assert adk_v2["shape"] == "otel_inline" and adk_v2["is_model_call"] == 1
    assert adk_v2["content_missing"] == 0

    s3_emb = by_key[("aaaa0000000000000000000000000001", "a100000000000004")]
    s3_tool = by_key[("aaaa0000000000000000000000000001", "a100000000000003")]
    assert s3_emb["span_kind"] == "embedding" and s3_emb["is_model_call"] == 1
    assert s3_emb["content_missing"] == 0 and s3_tool["content_missing"] == 0

    pos_tool = by_key[("bbbb0000000000000000000000000001", "b100000000000004")]
    assert pos_tool["span_kind"] == "tool_call" and pos_tool["is_model_call"] == 1
    assert pos_tool["source_class"] == "platform" and pos_tool["content_missing"] == 0


def test_spans_rows() -> None:
    """`spans` matches schema columns, deduplicates keys, routes 3 shapes, folds ADK v1, and marks content_missing."""
    with _new_silver_runner() as runner:
        res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_internal.spans`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY trace_id, span_id
            """
        )
        assert res.columns == _schema_columns("spans")
        assert len(res.rows) == 101
        by_key: dict[tuple[str, str], dict[str, object]] = {
            (str(r["trace_id"]), str(r["span_id"])): dict(r) for r in res.rows
        }

        dup_rt = by_key[("b2800d7f7f81c40d7a5017ce4cf3136a", "78d8a6f049562eed")]
        assert dup_rt["duplicate_count"] == 1 and dup_rt["source_class"] is None
        assert dup_rt["otel_kind"] is None and dup_rt["is_model_call"] == 0
        assert dup_rt["app_id"] is None and dup_rt["app_identity_bound"] is None

        dup_app = by_key[("003466f09e724c5d5e34b182501c491c", "0673cb1c8aa0c4cf")]
        assert (
            dup_app["duplicate_count"] == 1
            and dup_app["receive_time"] == "2026-11-13T15:26:27+00:00"
        )
        assert (
            dup_app["app_id"] == "kyc-address-verification" and dup_app["app_identity_bound"] == 1
        )

        inv_node = by_key[("003466f09e724c5d5e34b182501c491c", "82ac29a337966f50")]
        assert inv_node["span_kind"] == "other" and inv_node["is_model_call"] == 0
        assert inv_node["content_missing"] == 0

        extract_span = by_key[("09587491028bd87b83e9ace4252cdf1a", "7ee509ccdf82a71d")]
        assert extract_span["span_kind"] == "other" and extract_span["is_model_call"] == 1
        assert extract_span["content_missing"] == 1 and extract_span["input_tokens"] == 584

        zero_tool = by_key[("09587491028bd87b83e9ace4252cdf1a", "ee48c2c24daadaaf")]
        assert zero_tool["span_kind"] == "tool_call" and zero_tool["is_model_call"] == 0
        assert zero_tool["content_missing"] == 1

        _assert_shape_and_folding_spans(by_key)


def test_model_calls_rows() -> None:
    """`model_calls` matches schema columns, resolves agent/owner, folds ADK parent, and counts consecutive 429 retries."""
    with _new_silver_runner() as runner:
        res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_open.model_calls`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY trace_id, start_time, span_id
            """
        )
        assert res.columns == _schema_columns("model_calls")
        # 2 (003466) + 1 (081f625e) + 2 (095874) + 2 (616b8e81) + 2 (64080a2b) + 1 (f64f78b6)
        # + 2 (aaaa..01: generate_content + embeddings) + 1 (aaaa..02) + 1 (bbbb..01 classify_memo) = 14.
        assert len(res.rows) == 14
        by_sid = {str(r["span_id"]): r for r in res.rows}

        # ADK Format 1 fold lifts parent_span_id to invoke_agent (5d828e82d65c56d5).
        adk_v1_call = by_sid["4dc6d4987898ce84"]
        assert adk_v1_call["parent_span_id"] == "5d828e82d65c56d5"
        assert adk_v1_call["trace_owner_app_id"] == "bq-eval-adk-v1"
        assert adk_v1_call["agent_name"] == "probe_assistant"
        assert adk_v1_call["content_missing"] == 0

        # Shape 3 call inherits agent_name from enclosing invoke_agent span.
        s3_call = by_sid["a100000000000002"]
        assert s3_call["agent_name"] == "wealth_advisor"
        assert s3_call["trace_owner_app_id"] == "wealth-advisor-uc9"
        assert s3_call["response_id"] == "resp-s3-01"
        assert s3_call["response_model"] == "gemini-3.8-flash"

        # Platform positive-token tool call attributes trace_owner_app_id via gateway caller_app_id & attested traffic_type.
        plat_tool_call = by_sid["b100000000000004"]
        assert plat_tool_call["operation_name"] == "execute_tool"
        assert plat_tool_call["trace_owner_app_id"] == "payments-agent"
        assert plat_tool_call["traffic_type"] == "production_inference"
        assert plat_tool_call["traffic_type_attested"] == 1
        assert plat_tool_call["input_tokens"] == 64

        # Verify consecutive HTTP 429 caller_retry_count reset and window-edge margin membership.
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c000000000000001",
                    "parent_span_id": None,
                    "name": "generate_content",
                    "kind": 1,
                    "start_time": "2026-11-26 00:05:00 UTC",
                    "end_time": "2026-11-26 00:05:01 UTC",
                    "receive_time": "2026-11-26 00:05:02 UTC",
                    "status": {"code": 2, "message": "429"},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {"attributes": json.dumps({"service.name": "bq-eval-adk-v2"})},
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.agent.name": "retry_agent",
                            "gen_ai.request.model": "gemini-3.8-flash",
                            "http.response.status_code": 429,
                            "gen_ai.usage.input_tokens": 10,
                        }
                    ),
                },
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c000000000000002",
                    "parent_span_id": None,
                    "name": "generate_content",
                    "kind": 1,
                    "start_time": "2026-11-26 00:05:02 UTC",
                    "end_time": "2026-11-26 00:05:03 UTC",
                    "receive_time": "2026-11-26 00:05:04 UTC",
                    "status": {"code": 2, "message": "429"},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {"attributes": json.dumps({"service.name": "bq-eval-adk-v2"})},
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.agent.name": "retry_agent",
                            "gen_ai.request.model": "gemini-3.8-flash",
                            "http.response.status_code": 429,
                            "gen_ai.usage.input_tokens": 10,
                        }
                    ),
                },
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c000000000000003",
                    "parent_span_id": None,
                    "name": "generate_content",
                    "kind": 1,
                    "start_time": "2026-11-26 00:05:04 UTC",
                    "end_time": "2026-11-26 00:05:05 UTC",
                    "receive_time": "2026-11-26 00:05:06 UTC",
                    "status": {"code": 1, "message": ""},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {"attributes": json.dumps({"service.name": "bq-eval-adk-v2"})},
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.agent.name": "retry_agent",
                            "gen_ai.request.model": "gemini-3.8-flash",
                            "http.response.status_code": 200,
                            "gen_ai.usage.input_tokens": 10,
                            "gen_ai.usage.output_tokens": 5,
                        }
                    ),
                },
                {
                    "trace_id": "cccc0000000000000000000000000001",
                    "span_id": "c000000000000004",
                    "parent_span_id": None,
                    "name": "generate_content",
                    "kind": 1,
                    "start_time": "2026-11-26 00:05:06 UTC",
                    "end_time": "2026-11-26 00:05:07 UTC",
                    "receive_time": "2026-11-26 00:05:08 UTC",
                    "status": {"code": 1, "message": ""},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {"attributes": json.dumps({"service.name": "bq-eval-adk-v2"})},
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.agent.name": "retry_agent",
                            "gen_ai.request.model": "gemini-3.8-flash",
                            "http.response.status_code": 200,
                            "gen_ai.usage.input_tokens": 10,
                            "gen_ai.usage.output_tokens": 5,
                        }
                    ),
                },
            ],
            append=True,
        )
        retry_res = runner.query(
            """
            SELECT span_id, caller_retry_count
            FROM `showcase-project.silver_open.model_calls`(
              TIMESTAMP '2026-11-26 00:00:00 UTC',
              TIMESTAMP '2026-11-26 01:00:00 UTC'
            )
            WHERE trace_id = 'cccc0000000000000000000000000001'
            ORDER BY start_time, span_id
            """
        )
        assert retry_res.rows == (
            {"span_id": "c000000000000001", "caller_retry_count": 0},
            {"span_id": "c000000000000002", "caller_retry_count": 1},
            {"span_id": "c000000000000003", "caller_retry_count": 2},
            {"span_id": "c000000000000004", "caller_retry_count": 0},
        )


def test_reference_as_of_and_unregistered_facts() -> None:
    """SCD-2 `app_registry` joins by validity window, detects landing-place mismatch, and never drops unregistered spans."""
    with _new_silver_runner() as runner:
        runner.load_rows(
            "silver_ref.app_registry",
            [
                {
                    "app_id": "scd-app-v1",
                    "apphub_application_id": "scd-app",
                    "service_name": "scd-svc",
                    "environment": "prod",
                    "allowed_landing_places": ["showcase-project.obs_traces._AllSpans"],
                    "business_unit": "ops",
                    "risk_tier": "RL-2",
                    "effective_from": "2026-11-01 00:00:00 UTC",
                    "effective_to": "2026-11-20 00:00:00 UTC",
                },
                {
                    "app_id": "scd-app-v2",
                    "apphub_application_id": "scd-app",
                    "service_name": "scd-svc",
                    "environment": "prod",
                    "allowed_landing_places": ["showcase-project.platform_traces._AllSpans"],
                    "business_unit": "ops",
                    "risk_tier": "RL-1",
                    "effective_from": "2026-11-20 00:00:00 UTC",
                    "effective_to": None,
                },
            ],
            append=True,
        )
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "dddd0000000000000000000000000001",
                    "span_id": "d000000000000001",
                    "parent_span_id": None,
                    "name": "invoke_agent",
                    "kind": 1,
                    "start_time": "2026-11-15 12:00:00 UTC",
                    "end_time": "2026-11-15 12:00:01 UTC",
                    "receive_time": "2026-11-15 12:00:02 UTC",
                    "status": {"code": 1, "message": ""},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "scd-app",
                                "service.name": "scd-svc",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                    "attributes": json.dumps({"gen_ai.operation.name": "invoke_agent"}),
                },
                {
                    "trace_id": "dddd0000000000000000000000000002",
                    "span_id": "d000000000000002",
                    "parent_span_id": None,
                    "name": "invoke_agent",
                    "kind": 1,
                    "start_time": "2026-11-22 12:00:00 UTC",
                    "end_time": "2026-11-22 12:00:01 UTC",
                    "receive_time": "2026-11-22 12:00:02 UTC",
                    "status": {"code": 1, "message": ""},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {
                        "attributes": json.dumps(
                            {
                                "apphub.application.id": "scd-app",
                                "service.name": "scd-svc",
                                "deployment.environment.name": "prod",
                            }
                        )
                    },
                    "attributes": json.dumps({"gen_ai.operation.name": "invoke_agent"}),
                },
                {
                    "trace_id": "dddd0000000000000000000000000003",
                    "span_id": "d000000000000003",
                    "parent_span_id": None,
                    "name": "invoke_agent",
                    "kind": 1,
                    "start_time": "2026-11-22 12:00:00 UTC",
                    "end_time": "2026-11-22 12:00:01 UTC",
                    "receive_time": "2026-11-22 12:00:02 UTC",
                    "status": {"code": 1, "message": ""},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {"attributes": json.dumps({"service.name": "ghost-unregistered"})},
                    "attributes": json.dumps({"gen_ai.operation.name": "invoke_agent"}),
                },
            ],
            append=True,
        )
        res = runner.query(
            """
            SELECT span_id, app_id, app_identity_bound
            FROM `showcase-project.silver_internal.spans`(
              TIMESTAMP '2026-11-14 00:00:00 UTC',
              TIMESTAMP '2026-11-24 00:00:00 UTC'
            )
            WHERE trace_id LIKE 'dddd%'
            ORDER BY span_id
            """
        )
        assert res.rows == (
            {"span_id": "d000000000000001", "app_id": "scd-app-v1", "app_identity_bound": 1},
            {"span_id": "d000000000000002", "app_id": "scd-app-v2", "app_identity_bound": 0},
            {"span_id": "d000000000000003", "app_id": None, "app_identity_bound": None},
        )
