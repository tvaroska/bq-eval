"""Silver M18-11 `outcomes` and `outcomes_open` verification tests (`ADR-045`, `ADR-047`)."""

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


def test_outcomes_rows() -> None:
    """`outcomes` and `outcomes_open` enforce lookback, close-winner authority, confidences, and open omission."""
    with OfflineSqlRunner(
        RunnerConfig(
            current_date_utc="2026-11-26",
            current_timestamp_utc="2026-11-26T18:00:00Z",
            session_user=ALL_ADMIN_USER,
        )
    ) as runner:
        runner.load_silver_manifest(MANIFEST_PATH, schema_dir=SCHEMA_DIR)
        runner.load_rows(
            "silver_internal.unit_entitlements",
            [
                {"principal_email": ALL_ADMIN_USER, "business_unit": bu, "tenant_id": "*"}
                for bu in ("operations", "retail", "platform", "wealth", "__unowned__")
            ],
            append=True,
        )
        # Add: (a) a multi-trace outcome with 2 model confidences (`0.61` min vs `0.92` final)
        # and `rejected` terminal status; (b) an event-only `denied` outcome (`trace_count = 0`).
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "eeee0000000000000000000000000001",
                    "span_id": "e100000000000001",
                    "parent_span_id": None,
                    "name": "generate_content gemini-3.8-flash",
                    "kind": "1",
                    "start_time": "2026-11-24 10:00:00 UTC",
                    "end_time": "2026-11-24 10:00:01 UTC",
                    "receive_time": "2026-11-24 10:00:02 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.usage.input_tokens": 100,
                            "gen_ai.usage.output_tokens": 20,
                            "bq_eval.model_confidence": 0.61,
                            "bq_eval.case_id": "case-loan-77",
                            "bq_eval.eval_run_id": "run-01",
                            "bq_eval.document.page_count": 12,
                            "outcome.id": "out-loan-multi",
                            "outcome.owner": "wealth-advisor-uc9",
                            "outcome.subject": "loan-ticket-77",
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
                    "trace_id": "eeee0000000000000000000000000002",
                    "span_id": "e200000000000001",
                    "parent_span_id": None,
                    "name": "generate_content gemini-3.8-flash",
                    "kind": "1",
                    "start_time": "2026-11-26 10:00:00 UTC",
                    "end_time": "2026-11-26 10:00:01 UTC",
                    "receive_time": "2026-11-26 10:00:02 UTC",
                    "status": {"code": "2", "message": "err"},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": json.dumps(
                        {
                            "gen_ai.operation.name": "generate_content",
                            "gen_ai.usage.input_tokens": 80,
                            "gen_ai.usage.output_tokens": 15,
                            "bq_eval.model_confidence": 0.92,
                            "bq_eval.app_failed": True,
                            "outcome.id": "out-loan-multi",
                            "outcome.owner": "wealth-advisor-uc9",
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
            "silver_internal.workflow_events_sor",
            [
                {
                    "event_id": "ev-sor-loan-reject",
                    "event_time": "2026-11-26 11:00:00 UTC",
                    "event_type": "outcome.closed",
                    "source_system": "loan-sor",
                    "trace_id": "eeee0000000000000000000000000002",
                    "outcome_id": "out-loan-multi",
                    "payload_json": json.dumps({"outcome_status": "rejected"}),
                },
                {
                    "event_id": "ev-sor-denied-only",
                    "event_time": "2026-11-26 12:00:00 UTC",
                    "event_type": "outcome.closed",
                    "source_system": "policy-sor",
                    "outcome_id": "out-event-only-denied",
                    "payload_json": json.dumps(
                        {
                            "outcome_status": "denied",
                            "outcome_owner": "payments-agent",
                            "outcome_subject": "denied-case-12",
                        }
                    ),
                },
            ],
            append=True,
        )

        for sql_file in (
            "jcs.sql",
            "spans.sql",
            "tool_calls.sql",
            "events.sql",
            "traces.sql",
            "outcomes.sql",
        ):
            runner.register_silver_file(SILVER_SQL_DIR / sql_file)

        # Query ONLY Day 2 (`2026-11-26`) so multi-day outcomes prove `extends_outside_window = TRUE`!
        res_restricted = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_restricted.outcomes`(
              TIMESTAMP '2026-11-26 00:00:00 UTC',
              TIMESTAMP '2026-11-27 00:00:00 UTC'
            )
            ORDER BY outcome_id
            """
        )
        res_open = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_open.outcomes_open`(
              TIMESTAMP '2026-11-26 00:00:00 UTC',
              TIMESTAMP '2026-11-27 00:00:00 UTC'
            )
            ORDER BY outcome_id
            """
        )

        assert res_restricted.columns == _schema_columns("outcomes")
        assert res_open.columns == _schema_columns("outcomes_open")

        by_id: dict[str, dict[str, Any]] = {str(r["outcome_id"]): r for r in res_restricted.rows}
        open_by_id: dict[str, dict[str, Any]] = {str(r["outcome_id"]): r for r in res_open.rows}

        # `out-pay-001`: opened & trace on 2026-11-25, SoR closed on 2026-11-26 -> active on Day 2,
        # `extends_outside_window = TRUE`, SoR close (`succeeded` at 09:00 on 26th) beats earlier
        # application close (`failed` at 10:05 on 25th) AND platform closing span (`ADR-047`)!
        pay = by_id["out-pay-001"]
        assert pay["is_closed"] == 1
        assert pay["outcome_status"] == "succeeded"
        assert pay["close_time"] == "2026-11-26T09:00:00+00:00"
        assert pay["open_time"] == "2026-11-25T10:04:50+00:00"
        assert pay["outcome_subject"] == "wire-case-9001"
        assert pay["business_unit"] == "retail"
        assert pay["trace_count"] == 1
        assert json.loads(pay["trace_ids"]) == ["bbbb0000000000000000000000000001"]
        assert pay["extends_outside_window"] == 1
        assert "outcome_subject" not in open_by_id["out-pay-001"]

        # `out-loan-multi`: spans across 2026-11-24 and 2026-11-26, `rejected`, min=0.61, final=0.92.
        loan = by_id["out-loan-multi"]
        assert loan["is_closed"] == 1
        assert loan["outcome_status"] == "rejected"
        assert loan["outcome_subject"] == "loan-ticket-77"
        assert loan["case_id"] == "case-loan-77"
        assert loan["eval_run_id"] == "run-01"
        assert loan["page_count"] == 12
        assert loan["min_model_confidence"] == 0.61
        assert loan["final_model_confidence"] == 0.92
        assert loan["trace_count"] == 2
        assert json.loads(loan["trace_ids"]) == [
            "eeee0000000000000000000000000001",
            "eeee0000000000000000000000000002",
        ]
        assert loan["root_error_trace_count"] == 1
        assert loan["failed_trace_count"] == 1
        assert loan["business_unit"] == "wealth"
        assert loan["extends_outside_window"] == 1

        # `out-event-only-denied`: zero traces (`trace_count = 0`, `trace_ids = NULL`), `denied`.
        denied = by_id["out-event-only-denied"]
        assert denied["is_closed"] == 1
        assert denied["outcome_status"] == "denied"
        assert denied["trace_count"] == 0
        assert denied["trace_ids"] is None
        assert denied["last_span_arrival_time"] is None
        assert denied["business_unit"] == "retail"
        assert denied["extends_outside_window"] == 0
        assert denied["outcome_subject"] == "denied-case-12"
        assert "outcome_subject" not in open_by_id["out-event-only-denied"]
