"""Shipped SQL view tests for governance (`RL-1`-`RL-5`), Omni zero-egress, 5m SLO/429 breaches, sessions, and policy NFR gate."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from sqlglot import exp

from bq_eval.core.contract.otel import FinOpsTrafficType, ServingTrafficType
from bq_eval.core.engine.sql_eval import evaluate_traces_in_sqlite
from tests.conftest import make_span
from tests.sql_helpers import (
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _parse_bigquery_statements,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
)

FORBIDDEN_RAW_PAYLOAD_COLUMNS: frozenset[str] = frozenset(
    {
        "user_prompt",
        "retrieved_context",
        "final_response",
        "raw_prompt",
        "raw_response",
        "prompt_text",
        "completion_text",
        "payload",
        "json_payload",
    }
)


def _install_extra_views(conn: sqlite3.Connection, file_name: str, views: tuple[str, ...]) -> None:
    path = SQL_DIR / file_name
    for full_name in views:
        short = full_name.split(".", 1)[1]
        sql = _transpile_bq_view_to_sqlite(_extract_view_sql_text(path, full_name))
        conn.execute(f"CREATE VIEW {short} AS {sql}")


def _check_view_zero_egress(
    view_name: str,
    body: exp.Expression,
    declared_views: frozenset[str],
) -> list[str]:
    violations: list[str] = []
    safe_star_sources = declared_views | {
        cte.alias.lower() for cte in body.find_all(exp.CTE) if cte.alias
    }
    if isinstance(body, exp.Select) and any(isinstance(e, exp.Star) for e in body.expressions):
        violations.append(f"{view_name}: top-level SELECT * is forbidden in zero-egress views")
    for sub_select in body.find_all(exp.Select):
        if not any(isinstance(e, exp.Star) for e in sub_select.expressions):
            continue
        from_clause = sub_select.args.get("from_")
        from_tables = (
            [t.name.lower() for t in from_clause.find_all(exp.Table) if t.name]
            if from_clause is not None
            else []
        )
        if not from_tables or any(t not in safe_star_sources for t in from_tables):
            violations.append(
                f"{view_name}: SELECT * over external source {from_tables} is forbidden"
            )
    referenced = {col.name.lower() for col in body.find_all(exp.Column) if col.name}
    projected = (
        {e.alias_or_name.lower() for e in body.expressions}
        if isinstance(body, exp.Select)
        else set()
    )
    for bad in sorted((referenced | projected) & FORBIDDEN_RAW_PAYLOAD_COLUMNS):
        violations.append(f"{view_name}: forbidden raw payload reference {bad!r}")
    return violations


def _find_zero_egress_ast_violations(sql_text: str, label: str) -> list[str]:
    """Check every `CREATE [OR REPLACE] VIEW` via SQL AST for unsafe `SELECT *` or raw payload columns."""
    stmts = _parse_bigquery_statements(sql_text, label)
    view_stmts = [
        s
        for s in stmts
        if isinstance(s, exp.Create) and (s.args.get("kind") or "").upper() == "VIEW"
    ]
    declared_views = frozenset(s.this.name.lower() for s in view_stmts if s.this is not None)
    violations: list[str] = []
    for stmt in view_stmts:
        if stmt.expression is None:
            continue
        view_name = stmt.this.sql(dialect="bigquery") if stmt.this is not None else "<view>"
        violations.extend(_check_view_zero_egress(view_name, stmt.expression, declared_views))
    return violations


def test_inference_reconciliation_and_rl1_rl5_governance_sql_views() -> None:
    """`v_inference_request_reconciliation` & `v_app_risk_governance` enforce drift alerts and RL-1..RL-5 gates."""
    base = datetime(2026, 11, 22, 14, 15, 0, tzinfo=UTC)
    otel_spans = [
        make_span(
            "tr-rec-1",
            "sp-rec-1",
            app_id="gov-rl1-app",
            model_name="gemini-4-carbon",
            serving_traffic_type=ServingTrafficType.STANDARD,
            request_type_header="dedicated",
            http_status_code=200,
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            base_time=base,
        ),
        make_span(
            "tr-rec-2",
            "sp-rec-2",
            app_id="gov-rl4-app",
            model_name="gemini-3.8-flash",
            serving_traffic_type=ServingTrafficType.STANDARD,
            request_type_header="shared",
            http_status_code=429,
            input_tokens=5_000,
            cached_input_tokens=0,
            output_tokens=500,
            thinking_tokens=0,
            base_time=base,
        ),
    ]
    server_log_rows = [
        (
            "req-1",
            None,
            "gov-rl1-app",
            "gemini-4-carbon",
            "STANDARD",
            "dedicated",
            200,
            10_000,
            0,
            0,
            0,
            1_000,
            0,
            base.isoformat(),
        ),
        (
            "req-2",
            None,
            "gov-rl4-app",
            "gemini-3.8-flash",
            "STANDARD",
            "shared",
            429,
            9_000,
            0,
            0,
            0,
            1_000,
            0,
            base.isoformat(),
        ),
    ]

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    _install_extra_views(
        conn,
        "finops/capacity_views.sql",
        ("agent_eval.v_inference_request_reconciliation", "agent_eval.v_app_risk_governance"),
    )
    conn.executemany(
        "INSERT INTO app_registry ("
        "apphub_application_id, business_unit, sla_latency_ms, cost_center, "
        "risk_tier, autonomy_tier, lifecycle_track_policy, funded_gsus, spillover_tier, pt_commitment_months"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "gov-rl1-app",
                "core_banking",
                3000.0,
                "CC-RL1",
                "RL-1",
                "SUPERVISED_AUTONOMOUS",
                "long_term_ga_only",
                4.0,
                "priority",
                12,
            ),
            (
                "gov-rl4-app",
                "internal_ops",
                5000.0,
                "CC-RL4",
                "RL-4",
                "HUMAN_IN_LOOP",
                "short_term_allowed",
                0.0,
                "standard",
                0,
            ),
        ],
    )
    for sp in otel_spans:
        insert_enterprise_sqlite_span(conn, sp)
    conn.executemany(
        "INSERT INTO inference_request_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        server_log_rows,
    )

    sql_rec = {
        str(r["apphub_application_id"]): dict(r)
        for r in conn.execute("SELECT * FROM v_inference_request_reconciliation").fetchall()
    }
    sql_gov = {
        str(r["apphub_application_id"]): dict(r)
        for r in conn.execute("SELECT * FROM v_app_risk_governance").fetchall()
    }
    conn.close()

    assert sql_rec["gov-rl1-app"]["reconciliation_status"] == "RECONCILED"
    assert int(sql_rec["gov-rl1-app"]["pt_eligible_involuntary_spillover_count"]) == 1
    assert sql_rec["gov-rl4-app"]["reconciliation_status"] == "DRIFT_ALERT"
    assert int(sql_rec["gov-rl4-app"]["intentional_shared_bypass_count"]) == 1
    assert float(sql_gov["gov-rl1-app"]["required_ac2_gate"]) == pytest.approx(0.85)
    assert bool(sql_gov["gov-rl1-app"]["pt_commitment_allowed"]) is False
    assert float(sql_gov["gov-rl4-app"]["required_ac2_gate"]) == pytest.approx(0.75)
    assert bool(sql_gov["gov-rl4-app"]["pt_commitment_allowed"]) is True


def test_bigquery_omni_zero_egress_ast_proof() -> None:
    """`sql/finops/omni_spoke_views.sql` projects zero raw prompts or `SELECT *`, with positive controls."""
    omni_sql = (SQL_DIR / "finops" / "omni_spoke_views.sql").read_text(encoding="utf-8")
    violations = _find_zero_egress_ast_violations(omni_sql, "omni_spoke_views.sql")
    assert not violations, f"Zero-egress violation: {violations}"

    # Positive controls: CREATE VIEW without OR REPLACE, SELECT *, and literal ';' cannot slip past AST.
    star_leak = _find_zero_egress_ast_violations(
        "CREATE VIEW `agent_eval.v_bad_star` AS SELECT * FROM `agent_eval.aws_omni_otel_spans`;",
        "star_leak",
    )
    assert any("SELECT *" in v for v in star_leak)

    col_leak = _find_zero_egress_ast_violations(
        "CREATE VIEW `agent_eval.v_bad_col` AS SELECT ';' AS sep, user_prompt FROM `agent_eval.t`;",
        "col_leak",
    )
    assert any("user_prompt" in v for v in col_leak)


def test_sessions_5m_breaches_and_optimize_lane_sql_views() -> None:
    """Session lineage, 5m SLO/429 breaches, and `optimize` spend lane execute cleanly in SQL."""
    base = datetime(2026, 11, 22, 16, 1, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-sess-0",
            "sp-sess-0",
            app_id="live-app",
            session_id="sess-alpha",
            turn_index=0,
            latency_ms=800.0,
            http_status_code=200,
            base_time=base,
        ),
        make_span(
            "tr-sess-1",
            "sp-sess-1",
            app_id="live-app",
            session_id="sess-alpha",
            turn_index=1,
            status_code="ERROR",
            rule0_pass=False,
            latency_ms=6500.0,
            http_status_code=429,
            offset_s=30,
            base_time=base,
        ),
        make_span(
            "tr-opt-1",
            "sp-opt-1",
            app_id="live-app",
            finops_traffic_type=FinOpsTrafficType.OPTIMIZE,
            latency_ms=400.0,
            http_status_code=200,
            input_tokens=50_000,
            cached_input_tokens=0,
            output_tokens=2_000,
            thinking_tokens=0,
            offset_s=45,
            base_time=base,
        ),
    ]

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    _install_extra_views(
        conn,
        "finops/capacity_views.sql",
        ("agent_eval.v_interaction_session_lineage", "agent_eval.v_live_slo_and_429_breaches"),
    )
    for sp in spans:
        insert_enterprise_sqlite_span(conn, sp)

    bq_sess = conn.execute("SELECT * FROM v_interaction_session_lineage").fetchone()
    bq_breach = conn.execute("SELECT * FROM v_live_slo_and_429_breaches").fetchone()
    bq_opt = conn.execute(
        "SELECT optimize_usd, unclassified_usd FROM v_agent_eval_and_finops_summary "
        "WHERE apphub_application_id = 'live-app'"
    ).fetchone()
    conn.close()

    py_sum = evaluate_traces_in_sqlite(spans)["live-app"]

    assert int(bq_sess["turn_count"]) == 2
    assert int(bq_sess["first_failure_turn_index"]) == 1
    assert bool(bq_sess["session_rule0_pass"]) is False
    assert bq_breach["breach_reason"] == "HTTP_429_STORM"
    assert bool(bq_breach["is_breach"]) is True
    assert float(bq_opt["optimize_usd"]) == pytest.approx(py_sum.optimize_usd, abs=1e-12)
    assert float(bq_opt["optimize_usd"]) > 0.0
    assert float(bq_opt["unclassified_usd"]) == 0.0


def test_clause_scoped_nfr_gate_sql_view() -> None:
    """`v_step3_policy_nfr_gate` separates amended-clause flips from unamended negative flips."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_extra_views(
        conn,
        "core/step3_saved_results.sql",
        ("agent_eval.v_step3_paired_run_comparison", "agent_eval.v_step3_policy_nfr_gate"),
    )
    conn.executemany(
        "INSERT INTO eval_results ("
        "eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id, "
        "judge_model, sampling_stratum, rule0_pass, reasoning_trace, ordinal_score, binary_pass, "
        "judge_input_tokens, judge_output_tokens, judge_thinking_tokens, "
        "policy_sha256, amended_clause_ids, target_clause_id, evaluated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "e-b1",
                "run-base",
                "tr-c1",
                None,
                "pol-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "FULL_100PCT",
                1,
                "ok",
                4,
                1,
                100,
                20,
                0,
                "sha-v1",
                None,
                "CL-4.2",
                "2026-11-22T10:00:00+00:00",
            ),
            (
                "e-c1",
                "run-cand",
                "tr-c1",
                None,
                "pol-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "FULL_100PCT",
                1,
                "amended clause tightened",
                2,
                0,
                100,
                20,
                0,
                "sha-v2",
                "CL-4.2,CL-9.1",
                "CL-4.2",
                "2026-11-22T11:00:00+00:00",
            ),
            (
                "e-b2",
                "run-base",
                "tr-c2",
                None,
                "pol-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "FULL_100PCT",
                1,
                "ok",
                4,
                1,
                100,
                20,
                0,
                "sha-v1",
                None,
                "CL-1.0",
                "2026-11-22T10:00:00+00:00",
            ),
            (
                "e-c2",
                "run-cand",
                "tr-c2",
                None,
                "pol-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "FULL_100PCT",
                1,
                "passed unamended",
                4,
                1,
                100,
                20,
                0,
                "sha-v2",
                "CL-4.2,CL-9.1",
                "CL-1.0",
                "2026-11-22T11:00:00+00:00",
            ),
        ],
    )
    nfr_sql_row = conn.execute("SELECT * FROM v_step3_policy_nfr_gate").fetchone()
    conn.close()

    assert int(nfr_sql_row["amended_clause_flips"]) == 1
    assert int(nfr_sql_row["unamended_negative_flips"]) == 0
    assert bool(nfr_sql_row["passes_nfr_gate"]) is True
