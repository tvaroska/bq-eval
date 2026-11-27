"""Silver M18-12 caller filtering, wrapper views, and open-vs-restricted isolation tests."""

from __future__ import annotations

import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.loader import OfflineSqlRunner
from bq_eval.runner.engine.silver_udfs import render_silver_sql

REPO_ROOT = Path(__file__).resolve().parent.parent
SILVER_SQL_DIR = REPO_ROOT / "sql" / "silver"
SCHEMA_DIR = SILVER_SQL_DIR / "schema"
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "silver" / "manifest.json"

ALL_SILVER_SQL_FILES: tuple[str, ...] = (
    "shape3.sql",
    "jcs.sql",
    "spans.sql",
    "model_calls.sql",
    "tool_calls.sql",
    "events.sql",
    "trace_aux.sql",
    "traces.sql",
    "trace_payloads.sql",
    "outcomes.sql",
    "wrapper_views.sql",
)


def _setup_caller_runner(session_user: str) -> OfflineSqlRunner:
    runner = OfflineSqlRunner(
        RunnerConfig(
            current_date_utc="2026-11-26",
            current_timestamp_utc="2026-11-26T12:00:00Z",
            session_user=session_user,
        )
    )
    runner.load_silver_manifest(MANIFEST_PATH, schema_dir=SCHEMA_DIR)
    for sql_file in ALL_SILVER_SQL_FILES:
        runner.register_silver_file(SILVER_SQL_DIR / sql_file)
    return runner


def test_caller_filter_rows() -> None:
    """Every restricted TVF filters by `unit_entitlements` (BU, UC9 tenant, `__unowned__`, deny-default)."""
    # Unauthorized caller gets 0 rows on ALL 5 restricted TVFs, while open views still work.
    restricted_queries: tuple[tuple[str, str], ...] = (
        (
            "tool_calls",
            "SELECT * FROM `showcase-project.silver_restricted.tool_calls`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')",
        ),
        (
            "events",
            "SELECT * FROM `showcase-project.silver_restricted.events`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')",
        ),
        (
            "traces",
            "SELECT * FROM `showcase-project.silver_restricted.traces`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')",
        ),
        (
            "trace_payloads",
            "SELECT * FROM `showcase-project.silver_restricted.trace_payloads`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')",
        ),
        (
            "outcomes",
            "SELECT * FROM `showcase-project.silver_restricted.outcomes`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')",
        ),
    )
    with _setup_caller_runner("nobody-unentitled@example.invalid") as runner:
        for fn, query_sql in restricted_queries:
            res = runner.query(query_sql)
            assert len(res.rows) == 0, f"Unentitled caller saw rows in silver_restricted.{fn}"
        open_traces = runner.query(
            "SELECT * FROM `showcase-project.silver_open.traces_open`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert len(open_traces.rows) == 10
        # Fixed-window 24h wrapper views execute cleanly (anchored at 2026-11-26T12:00:00Z).
        w_events = runner.query("SELECT * FROM `showcase-project.silver_open.v_events_open_24h`")
        assert len(w_events.rows) >= 1
        w_restr = runner.query("SELECT * FROM `showcase-project.silver_restricted.v_traces_24h`")
        assert len(w_restr.rows) == 0

    # Retail Gold SA sees ONLY retail rows across all 5 restricted TVFs.
    with _setup_caller_runner(
        "gold-eval-retail@showcase-project.iam.gserviceaccount.com"
    ) as runner:
        tr = runner.query(
            "SELECT trace_id, business_unit FROM `showcase-project.silver_restricted.traces`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert tr.rows and all(r["business_unit"] == "retail" for r in tr.rows)
        tids = {str(r["trace_id"]) for r in tr.rows}
        assert "bbbb0000000000000000000000000001" in tids
        assert "aaaa0000000000000000000000000001" not in tids
        out = runner.query(
            "SELECT outcome_id, business_unit FROM `showcase-project.silver_restricted.outcomes`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert out.rows and all(r["business_unit"] == "retail" for r in out.rows)
        tc = runner.query(
            "SELECT trace_owner_app_id FROM `showcase-project.silver_restricted.tool_calls`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert tc.rows and all(
            r["trace_owner_app_id"] in {"payments-agent", "meridian-loan-consolidation"}
            for r in tc.rows
        )
        ev = runner.query(
            "SELECT event_id FROM `showcase-project.silver_restricted.events`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert len(ev.rows) >= 5

    # UC9 tenant-scoped analyst sees ONLY wealth + tenant-acme-01 (blocks other wealth tenants).
    with _setup_caller_runner("analyst-tenant-a@example.invalid") as runner:
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "ffff0000000000000000000000000009",
                    "span_id": "f900000000000001",
                    "parent_span_id": None,
                    "name": "invoke_agent wealth_advisor",
                    "kind": "1",
                    "start_time": "2026-11-25 10:30:00 UTC",
                    "end_time": "2026-11-25 10:30:01 UTC",
                    "receive_time": "2026-11-25 10:30:02 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": '{"gen_ai.operation.name":"invoke_agent","bq_eval.tenant_id":"tenant-other-99"}',
                    "resource": {
                        "attributes": '{"apphub.application.id":"wealth-advisor-uc9","service.name":"wealth-advisor-svc","deployment.environment.name":"prod"}'
                    },
                }
            ],
            append=True,
        )
        tr = runner.query(
            "SELECT trace_id, tenant_id FROM `showcase-project.silver_restricted.traces`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert len(tr.rows) == 2
        assert all(r["tenant_id"] == "tenant-acme-01" for r in tr.rows)
        assert not any(r["trace_id"] == "ffff0000000000000000000000000009" for r in tr.rows)
        tp = runner.query(
            "SELECT trace_id FROM `showcase-project.silver_restricted.trace_payloads`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert {str(r["trace_id"]) for r in tp.rows} == {
            "aaaa0000000000000000000000000001",
            "aaaa0000000000000000000000000002",
        }
        # Untrusted child span spoofing `tenant-acme-01` inside a `tenant-other-99` trace cannot leak events.
        runner.load_rows(
            "obs_traces._AllSpans",
            [
                {
                    "trace_id": "ffff0000000000000000000000000009",
                    "span_id": "f900000000000002",
                    "parent_span_id": "f900000000000001",
                    "name": "outcome.closed",
                    "kind": "1",
                    "start_time": "2026-11-25 10:30:01 UTC",
                    "end_time": "2026-11-25 10:30:01 UTC",
                    "receive_time": "2026-11-25 10:30:02 UTC",
                    "status": {"code": "1", "message": ""},
                    "trace_state": "",
                    "instrumentation_scope": {"name": "bq_eval.test", "version": "1.0"},
                    "events": [],
                    "attributes": '{"outcome.id":"out-other-99","outcome.status":"succeeded","bq_eval.tenant_id":"tenant-acme-01"}',
                    "resource": {
                        "attributes": '{"apphub.application.id":"wealth-advisor-uc9","service.name":"wealth-advisor-svc","deployment.environment.name":"prod"}'
                    },
                }
            ],
            append=True,
        )
        ev_uc9 = runner.query(
            "SELECT trace_id, event_id, tenant_id FROM `showcase-project.silver_restricted.events`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert not any(r["trace_id"] == "ffff0000000000000000000000000009" for r in ev_uc9.rows)

    # `__unowned__` admin/audit SA sees ONLY traces/events with NULL business_unit (`ADR-044`/`ADR-047`).
    with _setup_caller_runner(
        "gold-eval-unowned@showcase-project.iam.gserviceaccount.com"
    ) as runner:
        tr = runner.query(
            "SELECT trace_id, business_unit FROM `showcase-project.silver_restricted.traces`(TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert len(tr.rows) >= 1
        assert all(r["business_unit"] is None for r in tr.rows)


def test_open_sql_reads_no_restricted_source() -> None:
    """Static AST + text check: zero `silver_open` TVFs or views ever reference `silver_restricted` or `shape3_payloads`."""
    open_objects_checked: list[str] = []
    forbidden_re = re.compile(r"\bsilver_restricted\b|\bshape3_payloads\b", re.IGNORECASE)

    for sql_file in sorted(SILVER_SQL_DIR.rglob("*.sql")):
        rendered = render_silver_sql(sql_file.read_text(encoding="utf-8"))
        for stmt in sqlglot.parse(rendered, read="bigquery"):
            if not isinstance(stmt, exp.Create):
                continue
            target_sql = stmt.this.sql("bigquery") if stmt.this else ""
            if "silver_open." not in target_sql:
                continue
            open_objects_checked.append(target_sql)
            body_sql = stmt.expression.sql("bigquery") if stmt.expression else ""
            assert not forbidden_re.search(body_sql), (
                f"Open object {target_sql} in {sql_file.name} references restricted source!"
            )

    # Also verify `silver_internal` base TVFs called by `silver_open` never touch `silver_restricted` or `shape3_payloads`.
    internal_bases = (
        "spans",
        "tool_calls_base",
        "events_base",
        "trace_aux_agg",
        "traces_base",
        "outcomes_base",
    )
    checked_bases: set[str] = set()
    for sql_file in sorted(SILVER_SQL_DIR.rglob("*.sql")):
        rendered = render_silver_sql(sql_file.read_text(encoding="utf-8"))
        for stmt in sqlglot.parse(rendered, read="bigquery"):
            if not isinstance(stmt, exp.Create) or stmt.this is None:
                continue
            target_sql = stmt.this.sql("bigquery")
            for base_name in internal_bases:
                if f"silver_internal.{base_name}" in target_sql:
                    checked_bases.add(base_name)
                    body_sql = stmt.expression.sql("bigquery") if stmt.expression else ""
                    assert not forbidden_re.search(body_sql), (
                        f"Internal base {target_sql} referenced by silver_open touches restricted source!"
                    )

    assert len(open_objects_checked) == 15  # 7 silver_open TVFs + 1 rollup table + 7 wrapper views
    assert checked_bases == set(internal_bases)
