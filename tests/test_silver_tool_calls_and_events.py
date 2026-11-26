"""Silver M18-6 & M18-7 tests: JCS vectors, `tool_calls`/`tool_calls_open`, `events`/`events_open`, `outcome_links`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.loader import OfflineSqlRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
SILVER_SQL_DIR = REPO_ROOT / "sql" / "silver"
SCHEMA_DIR = SILVER_SQL_DIR / "schema"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "silver"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"
ADMIN_USER = "test-admin@example.invalid"


def _schema_columns(name: str) -> tuple[str, ...]:
    cols = json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return tuple(str(c["name"]) for c in cols)


def _new_silver_runner(*, session_user: str = ADMIN_USER) -> OfflineSqlRunner:
    runner = OfflineSqlRunner(
        RunnerConfig(
            current_date_utc="2026-11-26",
            current_timestamp_utc="2026-11-26 12:00:00 UTC",
            session_user=session_user,
        )
    )
    runner.load_silver_manifest(MANIFEST_PATH, schema_dir=SCHEMA_DIR)
    runner.load_rows(
        "silver_internal.unit_entitlements",
        [
            {"principal_email": ADMIN_USER, "business_unit": bu, "tenant_id": "*"}
            for bu in ("operations", "retail", "platform", "wealth", "__unowned__")
        ],
        append=True,
    )
    for fname in ("jcs.sql", "spans.sql", "tool_calls.sql", "events.sql"):
        runner.register_silver_file(SILVER_SQL_DIR / fname)
    return runner


def test_jcs_vectors() -> None:
    """Offline JCS UDFs pass every committed ADR-041 vector and enforce the 65,532-byte ceiling."""
    vec_doc: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "adr041_jcs_vectors.json").read_text(encoding="utf-8")
    )
    with _new_silver_runner() as runner:
        for vec in vec_doc["vectors"]:
            runner.load_rows("silver_internal.jcs_probe", [{"payload": vec["input_json"]}])
            res = runner.query(
                """
                SELECT
                  `showcase-project.silver_internal.jcs_canonicalize`(payload) AS canon,
                  `showcase-project.silver_internal.jcs_sha256`(payload) AS digest
                FROM `showcase-project.silver_internal.jcs_probe`
                """
            )
            assert res.rows == ({"canon": vec["canonical_json"], "digest": vec["sha256"]},), (
                f"Vector mismatch on {vec['id']}: {res.rows}"
            )

        guard = vec_doc["oversize_guard"]
        ok_payload = (
            str(guard["template_prefix"])
            + str(guard["fill_char"]) * int(guard["accepted_fill_len"])
            + str(guard["template_suffix"])
        )
        bad_payload = (
            str(guard["template_prefix"])
            + str(guard["fill_char"]) * int(guard["rejected_fill_len"])
            + str(guard["template_suffix"])
        )
        runner.load_rows(
            "silver_internal.jcs_probe",
            [{"kind": "ok", "payload": ok_payload}, {"kind": "too_big", "payload": bad_payload}],
        )
        g_res = runner.query(
            """
            SELECT kind, `showcase-project.silver_internal.jcs_sha256`(payload) AS digest
            FROM `showcase-project.silver_internal.jcs_probe`
            ORDER BY kind
            """
        )
        assert g_res.rows == (
            {"kind": "ok", "digest": guard["accepted_sha256"]},
            {"kind": "too_big", "digest": None},
        )

        # Deeply nested JSON and lone UTF-16 surrogates fail safe to NULL (never raise).
        runner.load_rows(
            "silver_internal.jcs_probe",
            [
                {"kind": "deep", "payload": ("[" * 120) + ("]" * 120)},
                {"kind": "surr_val", "payload": r'{"a":"\ud800"}'},
                {"kind": "surr_key", "payload": r'{"\ud800":1}'},
            ],
        )
        edge_res = runner.query(
            """
            SELECT kind, `showcase-project.silver_internal.jcs_sha256`(payload) AS digest
            FROM `showcase-project.silver_internal.jcs_probe`
            ORDER BY kind
            """
        )
        assert edge_res.rows == (
            {"kind": "deep", "digest": None},
            {"kind": "surr_key", "digest": None},
            {"kind": "surr_val", "digest": None},
        )


def test_tool_calls_rows() -> None:
    """`tool_calls` & `tool_calls_open` fold server spans, apply tools flags, compute 4 hash sources & repeat_number."""
    vec_doc: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "adr041_jcs_vectors.json").read_text(encoding="utf-8")
    )
    expected_wire_sha = next(
        v["sha256"] for v in vec_doc["vectors"] if v["id"] == "tool_call_canonical_base"
    )
    with _new_silver_runner() as runner:
        res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_restricted.tool_calls`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY trace_id, start_time, span_id
            """
        )
        open_res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_open.tool_calls_open`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY trace_id, start_time, span_id
            """
        )
        assert res.columns == _schema_columns("tool_calls")
        assert open_res.columns == _schema_columns("tool_calls_open")
        # 1 (095874) + 1 (616b8e81) + 1 (64080a2b) + 1 (aaaa..01 Shape 3) + 2 (bbbb..01: transfer_funds folded + classify_memo) = 6.
        assert len(res.rows) == 6
        assert len(open_res.rows) == 6

        by_sid = {str(r["span_id"]): r for r in res.rows}
        open_by_sid = {str(r["span_id"]): r for r in open_res.rows}

        # Server span b100000000000003 folded into client span b100000000000002.
        assert "b100000000000003" not in by_sid
        wire = by_sid["b100000000000002"]
        assert wire["tool_name"] == "transfer_funds"
        assert wire["args_hash_source"] == "proxy"
        assert wire["args_hash_attested"] == 1
        assert wire["args_sha256"] == expected_wire_sha
        assert wire["writes_state"] == 1
        assert wire["repeat_number"] == 1
        assert open_by_sid["b100000000000002"]["repeat_number"] == 1
        assert "args_sha256" not in open_by_sid["b100000000000002"]

        # Positive-token tool span classify_memo hashed by Silver from inline arguments (ADR-047).
        memo = by_sid["b100000000000004"]
        assert memo["args_hash_source"] == "silver"
        assert memo["args_hash_attested"] == 0
        assert memo["args_sha256"] is not None
        assert memo["writes_state"] == 0
        assert memo["repeat_number"] == 2

        # Shape 3 execute_tool falls back to BigLake stored messages via gen_ai.tool.call.id without hashing model text.
        s3_tool = by_sid["a100000000000003"]
        assert s3_tool["args_hash_source"] == "none"
        assert s3_tool["args_hash_attested"] is None
        assert s3_tool["repeat_number"] is None
        assert s3_tool["returns_pii"] == 1
        assert s3_tool["content_missing"] == 0
        assert json.loads(str(s3_tool["arguments"])) == {"account_id": "acct-w-01"}
        assert json.loads(str(s3_tool["results"])) == {"bond_pct": 40, "equity_pct": 60}

        # Unregistered tool leaves flags NULL and keeps the row (ADR-040).
        unreg = by_sid["ee48c2c24daadaaf"]
        assert unreg["tool_name"] == "path_a_validate_payoff_assertion"
        assert unreg["writes_state"] is None
        assert unreg["args_hash_source"] == "none"
        assert unreg["content_missing"] == 1

        # Verify application hash source & duplicate repeat_number=1 on identical repeated calls.
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "eeee0000000000000000000000000001",
                    "span_id": "e000000000000001",
                    "parent_span_id": None,
                    "name": "execute_tool lookup_policy",
                    "kind": 1,
                    "start_time": "2026-11-26 02:00:00 UTC",
                    "end_time": "2026-11-26 02:00:01 UTC",
                    "receive_time": "2026-11-26 02:00:02 UTC",
                    "status": {"code": 1, "message": ""},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {"attributes": json.dumps({"service.name": "bq-eval-adk-v2"})},
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": "lookup_policy",
                            "gen_ai.tool.call.arguments": '{"policy_id":"POL-1"}',
                        }
                    ),
                },
                {
                    "trace_id": "eeee0000000000000000000000000001",
                    "span_id": "e000000000000002",
                    "parent_span_id": None,
                    "name": "execute_tool lookup_policy",
                    "kind": 1,
                    "start_time": "2026-11-26 02:00:02 UTC",
                    "end_time": "2026-11-26 02:00:03 UTC",
                    "receive_time": "2026-11-26 02:00:04 UTC",
                    "status": {"code": 1, "message": ""},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {"attributes": json.dumps({"service.name": "bq-eval-adk-v2"})},
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": "lookup_policy",
                            "gen_ai.tool.call.arguments": '{"policy_id": "POL-1"}',
                        }
                    ),
                },
                {
                    "trace_id": "eeee0000000000000000000000000001",
                    "span_id": "e000000000000003",
                    "parent_span_id": None,
                    "name": "execute_tool lookup_policy",
                    "kind": 1,
                    "start_time": "2026-11-26 02:00:04 UTC",
                    "end_time": "2026-11-26 02:00:05 UTC",
                    "receive_time": "2026-11-26 02:00:06 UTC",
                    "status": {"code": 1, "message": ""},
                    "instrumentation_scope": {"name": "test.sdk"},
                    "resource": {"attributes": json.dumps({"service.name": "bq-eval-adk-v2"})},
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": "lookup_policy",
                            "bq_eval.args_sha256": "deadbeef" * 8,
                        }
                    ),
                },
            ],
            append=True,
        )
        rep = runner.query(
            """
            SELECT span_id, args_hash_source, args_hash_attested, repeat_number
            FROM `showcase-project.silver_open.tool_calls_open`(
              TIMESTAMP '2026-11-26 01:00:00 UTC',
              TIMESTAMP '2026-11-26 03:00:00 UTC'
            )
            WHERE trace_id = 'eeee0000000000000000000000000001'
            ORDER BY start_time, span_id
            """
        )
        assert rep.rows == (
            {
                "span_id": "e000000000000001",
                "args_hash_source": "silver",
                "args_hash_attested": 0,
                "repeat_number": 1,
            },
            {
                "span_id": "e000000000000002",
                "args_hash_source": "silver",
                "args_hash_attested": 0,
                "repeat_number": 1,
            },
            {
                "span_id": "e000000000000003",
                "args_hash_source": "application",
                "args_hash_attested": 0,
                "repeat_number": 2,
            },
        )


def test_events_rows() -> None:
    """`events` & `events_open` enforce writer class, dedup/conflict ties, JCS hashes, lifted keys, and log/span sources."""
    vec_doc: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "adr041_jcs_vectors.json").read_text(encoding="utf-8")
    )
    expected_wire_sha = next(
        v["sha256"] for v in vec_doc["vectors"] if v["id"] == "tool_call_canonical_base"
    )
    with _new_silver_runner() as runner:
        res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_restricted.events`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY source_class, event_id
            """
        )
        open_res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_open.events_open`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY source_class, event_id
            """
        )
        assert res.columns == _schema_columns("events")
        assert open_res.columns == _schema_columns("events_open")
        by_key = {(str(r["source_class"]), str(r["event_id"])): r for r in res.rows}
        open_by_key = {(str(r["source_class"]), str(r["event_id"])): r for r in open_res.rows}

        # SoR approval & commit compute identical RFC 8785 JCS hashes across 100.0 vs 1e2.
        appr = by_key[("system_of_record", "ev-sor-appr-01")]
        comm = by_key[("system_of_record", "ev-sor-commit-01")]
        assert appr["is_authoritative"] == 1
        assert appr["approved_args_sha256"] == expected_wire_sha
        assert appr["committed_args_sha256"] is None
        assert comm["committed_args_sha256"] == expected_wire_sha

        # Duplicate vs same-timestamp tie-break (ADR-047).
        dup = by_key[("system_of_record", "ev-sor-dup-01")]
        assert dup["duplicate_count"] == 1
        assert dup["conflict_count"] == 0
        tie = by_key[("system_of_record", "ev-sor-tie-01")]
        assert tie["duplicate_count"] == 0
        assert tie["conflict_count"] == 1
        assert json.loads(str(tie["payload_json"])) == {"note": "alpha"}

        # ci.verified authoritative ONLY in SoR copy, never in application copy.
        assert by_key[("system_of_record", "ev-sor-ci-01")]["is_authoritative"] == 1
        assert by_key[("application", "ev-app-ci-unauth-01")]["is_authoritative"] == 0

        # Lifted payload_json keys.
        assert (
            by_key[("system_of_record", "ev-sor-link-01")]["linked_outcome_id"] == "out-incident-77"
        )
        assert by_key[("application", "ev-app-state-01")]["state_transition_to"] == "Answer"
        assert by_key[("application", "ev-app-fb-01")]["feedback_reason"] == "incomplete_citation"

        # Span-event guardrail.intervention from platform gateway span.
        gw_guard = by_key[("platform", "span:bbbb0000000000000000000000000001:b100000000000001")]
        assert gw_guard["event_type"] == "guardrail.intervention"
        assert gw_guard["control_id"] == "dlp-acct-mask"
        assert gw_guard["control_layer"] == "gateway"
        assert gw_guard["control_action"] == "shadow"

        # Closing spans from recorded and platform traces.
        rec_close = by_key[
            ("application", "span:003466f09e724c5d5e34b182501c491c:a45ca1f52af6bba2")
        ]
        assert rec_close["event_type"] == "outcome.closed"
        assert rec_close["outcome_status"] == "succeeded"

        # Recorded _AllLogs gen_ai.evaluation.result omits restricted explanation in events_open.
        log_ev = by_key[("application", "sb6dtue6k030")]
        log_ev_open = open_by_key[("application", "sb6dtue6k030")]
        assert log_ev["event_type"] == "feedback.thumbs_up"
        assert json.loads(str(log_ev["labels"]))["evaluation_explanation"] == "probe test"
        assert "evaluation_explanation" not in json.loads(str(log_ev_open["labels"]))

        # Midnight-straddling closing span & multi-guardrail span events stay preserved without collision.
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "77770000000000000000000000000001",
                    "span_id": "7700000000000001",
                    "parent_span_id": None,
                    "name": "invoke_agent advisor",
                    "kind": "1",
                    "start_time": "2026-11-25 23:59:50 UTC",
                    "end_time": "2026-11-26 00:00:10 UTC",
                    "receive_time": "2026-11-26 00:00:12 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [
                        {
                            "name": "guardrail.intervention",
                            "time": "2026-11-26 00:00:02 UTC",
                            "attributes": json.dumps(
                                {
                                    "control_id": "dlp-in",
                                    "control_layer": "input",
                                    "control_action": "redact",
                                }
                            ),
                        },
                        {
                            "name": "guardrail.intervention",
                            "time": "2026-11-26 00:00:03 UTC",
                            "attributes": json.dumps(
                                {
                                    "control_id": "ma-out",
                                    "control_layer": "output",
                                    "control_action": "block",
                                }
                            ),
                        },
                    ],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "invoke_agent",
                            "bq_eval.tenant_id": "tenant-acme-01",
                            "outcome.id": "out-mid-01",
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
                    "trace_id": "77770000000000000000000000000001",
                    "span_id": "7700000000000002",
                    "parent_span_id": "7700000000000001",
                    "name": "outcome.closed",
                    "kind": "1",
                    "start_time": "2026-11-26 00:00:05 UTC",
                    "end_time": "2026-11-26 00:00:05 UTC",
                    "receive_time": "2026-11-26 00:00:06 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {"outcome.id": "out-mid-01", "outcome.status": "succeeded"}
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
        mid_res = runner.query(
            """
            SELECT event_id, event_type, trace_owner_app_id, tenant_id, control_id, conflict_count
            FROM `showcase-project.silver_restricted.events`(
              TIMESTAMP '2026-11-26 00:00:00 UTC',
              TIMESTAMP '2026-11-26 01:00:00 UTC'
            )
            WHERE trace_id = '77770000000000000000000000000001'
            ORDER BY event_time, event_id
            """
        )
        assert len(mid_res.rows) == 3
        assert {
            r["control_id"] for r in mid_res.rows if r["event_type"] == "guardrail.intervention"
        } == {
            "dlp-in",
            "ma-out",
        }
        assert all(r["trace_owner_app_id"] == "wealth-advisor-uc9" for r in mid_res.rows)
        assert all(r["tenant_id"] == "tenant-acme-01" for r in mid_res.rows)
        assert all(r["conflict_count"] == 0 for r in mid_res.rows)


def test_outcome_links_rows() -> None:
    """`outcome_links` projects distinct (primary_outcome_id, linked_outcome_id) pairs in silver_open."""
    with _new_silver_runner() as runner:
        res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_open.outcome_links`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY primary_outcome_id, linked_outcome_id
            """
        )
        assert res.columns == _schema_columns("outcome_links")
        assert res.rows == (
            {"primary_outcome_id": "out-pay-001", "linked_outcome_id": "out-incident-77"},
        )
