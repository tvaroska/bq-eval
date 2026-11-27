"""M20-0 query-plan scan budgets, Gold OOM/dedup/Shape-3 guards & Split-SA staging tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
import sqlglot
from sqlglot import exp

from bq_eval.deploy.engine.bootstrap import validate_bootstrap_offline
from bq_eval.deploy.engine.silver_deploy import (
    SILVER_SQL_ORDER,
    build_gold_view_authorizations,
    build_silver_routine_authorizations,
)
from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.bronze_bridge import open_silver_gold_runner
from bq_eval.runner.engine.silver_udfs import render_silver_sql
from tests.conftest import REPO_ROOT, SQL_DIR, make_span, open_runner_for_spans

SILVER_DIR = SQL_DIR / "silver"
CORE_DIR = SQL_DIR / "core"


def _extract_short_name(raw: str) -> tuple[str, str]:
    """Return `(dataset, object_name)` lowercased from backticked or dotted SQL identifiers."""
    parts = [p.strip("`").lower() for p in raw.strip().split(".") if p.strip("`")]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", parts[-1] if parts else ""


def _load_silver_tvf_asts() -> dict[str, exp.Expr]:
    tvfs: dict[str, exp.Expr] = {}
    for rel in SILVER_SQL_ORDER:
        if rel == "silver/shape3.sql":
            continue
        rendered = render_silver_sql((SQL_DIR / rel).read_text(encoding="utf-8"))
        for stmt in sqlglot.parse(rendered, read="bigquery"):
            if not isinstance(stmt, exp.Create) or stmt.expression is None:
                continue
            if not isinstance(stmt.this, exp.UserDefinedFunction):
                continue
            if stmt.args.get("returns") is not None:
                continue
            if not isinstance(stmt.this.this, exp.Table):
                continue
            _, tvf_name = _extract_short_name(stmt.this.this.sql("bigquery"))
            if tvf_name:
                tvfs[tvf_name] = stmt.expression
    return tvfs


def _iter_child_exprs(node: exp.Expr, *, skip_with: bool = False) -> list[exp.Expr]:
    out: list[exp.Expr] = []
    for key, val in node.args.items():
        if skip_with and key == "with_":
            continue
        items = val if isinstance(val, list) else [val]
        out.extend(item for item in items if isinstance(item, exp.Expr))
    return out


def _count_obs_spans_scans(
    node: exp.Expr,
    tvf_bodies: Mapping[str, exp.Expr],
    cte_costs: Mapping[str, int] | None = None,
) -> int:
    """Recursively count `obs_traces._AllSpans` scans using BigQuery's CTE-inlining semantics."""
    scope = dict(cte_costs or {})
    if isinstance(node, exp.Subquery) and node.this is not None:
        return _count_obs_spans_scans(node.this, tvf_bodies, scope)

    with_clause = node.args.get("with_")
    if isinstance(with_clause, exp.With):
        for cte in with_clause.expressions:
            alias = cte.alias.lower()
            if alias and cte.this is not None:
                scope[alias] = _count_obs_spans_scans(cte.this, tvf_bodies, scope)

    if isinstance(node, exp.Union):
        arms = [arm for arm in (node.this, node.expression) if arm is not None]
        return sum(_count_obs_spans_scans(arm, tvf_bodies, scope) for arm in arms)

    return sum(
        _walk_non_with_expr(child, tvf_bodies, scope)
        for child in _iter_child_exprs(node, skip_with=True)
    )


def _count_table_node_scans(
    node: exp.Table,
    tvf_bodies: Mapping[str, exp.Expr],
    scope: Mapping[str, int],
) -> int:
    if isinstance(node.this, exp.Anonymous):
        _, fn_name = _extract_short_name(node.this.name)
        return (
            _count_obs_spans_scans(tvf_bodies[fn_name], tvf_bodies) if fn_name in tvf_bodies else 0
        )
    db_l = node.db.lower().strip("`")
    raw_name = node.name
    if not db_l and "." in raw_name:
        db_l, name_l = _extract_short_name(raw_name)
    else:
        name_l = raw_name.lower().strip("`")
    if not db_l and name_l in scope:
        return scope[name_l]
    return 1 if (db_l == "obs_traces" and name_l == "_allspans") else 0


def _walk_non_with_expr(
    node: exp.Expr,
    tvf_bodies: Mapping[str, exp.Expr],
    scope: Mapping[str, int],
) -> int:
    if isinstance(node, (exp.Select, exp.Union)):
        return _count_obs_spans_scans(node, tvf_bodies, scope)
    if isinstance(node, exp.Table):
        return _count_table_node_scans(node, tvf_bodies, scope)
    if isinstance(node, exp.Anonymous):
        _, fn_name = _extract_short_name(node.name)
        if fn_name in tvf_bodies:
            return _count_obs_spans_scans(tvf_bodies[fn_name], tvf_bodies)
    return sum(_walk_non_with_expr(child, tvf_bodies, scope) for child in _iter_child_exprs(node))


def test_silver_base_scan_budget_under_ten() -> None:
    """Every Silver TVF executes strictly `< 10` `_AllSpans` scans even under BigQuery CTE inlining."""
    tvf_bodies = _load_silver_tvf_asts()
    counts = {name: _count_obs_spans_scans(body, tvf_bodies) for name, body in tvf_bodies.items()}

    assert counts["spans"] == 1
    assert counts["model_calls"] == 1
    assert counts["tool_calls_base"] == 2
    assert counts["tool_calls_open"] == 2
    assert counts["tool_calls"] == 3
    assert counts["outcome_links"] == 0
    assert counts["events_base"] <= 3
    assert counts["events_open"] <= 3
    assert counts["events"] <= 3
    assert counts["trace_aux_agg"] == 1
    assert counts["traces_base"] == 4
    assert counts["traces_open"] == 4
    assert counts["traces"] == 4
    assert counts["trace_payloads"] <= 8
    assert counts["outcomes_base"] <= 4
    assert counts["outcomes_open"] <= 4
    assert counts["outcomes"] <= 4
    assert all(c < 10 for c in counts.values()), f"Silver TVF scan budget exceeded: {counts}"


def test_gold_oom_pricing_dedup_clustering_and_shape3_options() -> None:
    """Verify OOM fix, deduplicated pricing/events joins, workflow clustering, Shape-3 options & Gold TVFs."""
    summary_sql = (CORE_DIR / "summary_views.sql").read_text(encoding="utf-8")
    assert not re.search(r"STRING_AGG\([^)]*\)\s*OVER\s*\(\s*\)", summary_sql, re.IGNORECASE)

    cost_sql = (CORE_DIR / "gold_cost_views.sql").read_text(encoding="utf-8")
    gcc_body = cost_sql.split("CREATE OR REPLACE TABLE FUNCTION `agent_eval.gold_trace_waterfall`")[
        0
    ]
    assert gcc_body.count("v_default_model_pricing") == 1

    gates_sql = (CORE_DIR / "gold_eval_gates.sql").read_text(encoding="utf-8")
    tse_body = gates_sql.split(
        "CREATE OR REPLACE TABLE FUNCTION `agent_eval.gold_trace_sql_evaluation`"
    )[1].split("CREATE OR REPLACE VIEW `agent_eval.v_trace_sql_evaluation`")[0]
    assert tse_body.count("${SILVER_RESTRICTED_DATASET}.events`(") == 1
    v_outcomes_body = gates_sql.split("CREATE OR REPLACE VIEW `agent_eval.v_outcomes`")[1].split(
        "CREATE OR REPLACE VIEW `agent_eval.v_cost_per_verified_outcome`"
    )[0]
    assert v_outcomes_body.count("${SILVER_OPEN_DATASET}.events_open`(") == 1

    wf_sql = (SILVER_DIR / "workflow_events.sql").read_text(encoding="utf-8")
    assert wf_sql.count("CLUSTER BY event_type, outcome_id, trace_id, action_id;") == 3

    parq_report, parq_stmts = validate_bootstrap_offline(
        mode="silver",
        project_id="cold-proj-01",
        shape3_format="PARQUET",
    )
    assert parq_report.passed, parq_report.errors
    s3_parq = next(stmt for label, stmt in parq_stmts if label == "silver/shape3.sql#1")
    assert "format = 'PARQUET'" in s3_parq
    assert "metadata_cache_mode = 'MANUAL'" in s3_parq
    assert "max_staleness = INTERVAL 30 MINUTE" in s3_parq

    ice_report, ice_stmts = validate_bootstrap_offline(
        mode="silver",
        project_id="cold-proj-01",
        shape3_format="ICEBERG",
    )
    assert ice_report.passed, ice_report.errors
    s3_ice = next(stmt for label, stmt in ice_stmts if label == "silver/shape3.sql#1")
    assert "format = 'ICEBERG'" in s3_ice
    assert "hive_partition_uri_prefix" not in s3_ice

    # Parameterized Gold TVFs match thin CURRENT_TIMESTAMP() wrapper views in OfflineSqlRunner.
    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    spans = [make_span("tr-gold-tvf-1", "sp-gold-tvf-1", app_id="tvf-test-app", base_time=base)]
    with open_runner_for_spans(spans) as runner:
        wf_view = runner.query(
            "SELECT trace_id, span_id, span_cost_usd FROM `agent_eval.v_trace_waterfall`"
        )
        wf_tvf = runner.query(
            "SELECT trace_id, span_id, span_cost_usd FROM `agent_eval.gold_trace_waterfall`("
            "TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert wf_view.rows == wf_tvf.rows and len(wf_tvf.rows) >= 1

        ev_view = runner.query(
            "SELECT trace_id, apphub_application_id, rule0_pass FROM `agent_eval.v_trace_sql_evaluation`"
        )
        ev_tvf = runner.query(
            "SELECT trace_id, apphub_application_id, rule0_pass FROM `agent_eval.gold_trace_sql_evaluation`("
            "TIMESTAMP '2026-11-01 00:00:00 UTC', TIMESTAMP '2026-12-01 00:00:00 UTC')"
        )
        assert ev_view.rows == ev_tvf.rows and len(ev_tvf.rows) == 1


def test_split_sa_gold_staging_zero_json_and_uc9_tenant_isolation() -> None:
    """Open staged tables carry zero JSON columns; restricted 2-hop staging prevents UC9 cross-tenant leaks."""
    open_sql = (CORE_DIR / "gold_staging_open.sql").read_text(encoding="utf-8")
    for table_name in ("gold_staged_open_waterfall", "gold_staged_open_kpi"):
        m = re.search(
            rf"CREATE TABLE IF NOT EXISTS `agent_eval\.{table_name}`\s*\((.*?)\)\s*PARTITION BY",
            open_sql,
            re.DOTALL,
        )
        assert m is not None, f"Missing staged open table {table_name}"
        assert not re.search(r"\bJSON\b", m.group(1)), f"{table_name} must stay zero-JSON"

    routine_auth = build_silver_routine_authorizations(
        project_id="proj-x",
        gold_dataset_id="agent_eval",
    )
    # Zero Gold routines may ever be authorized on `silver_internal`.
    silver_int_entries = routine_auth.get("silver_internal", ())
    assert not any(ds == "agent_eval" for _, ds, _ in silver_int_entries)

    view_auth = build_gold_view_authorizations(project_id="proj-x")
    for target_ds in ("agent_eval_internal", "silver_internal"):
        vnames = {tbl for _, _, tbl in view_auth[target_ds]}
        assert vnames == {"v_staged_trace_eval", "v_staged_agent_eval_and_finops_summary"}

    # Execute 2-hop entitlement isolation on staged trace + outcome tables across two tenants in same BU.
    staged_traces = [
        {
            "trace_id": "tr-acme-1",
            "apphub_application_id": "wealth-advisor-uc9",
            "business_unit": "wealth",
            "tenant_id": "tenant-acme-01",
            "effective_risk_tier": "RL-2",
            "risk_tier": "RL-2",
            "outcome_id": "out-acme-1",
            "outcome_owner": "wealth-advisor-uc9",
            "conversation_id": "conv-a1",
            "previous_interaction_id": None,
            "trace_start_time": "2026-11-25T10:00:00+00:00",
            "trace_end_time": "2026-11-25T10:00:02+00:00",
            "trace_latency_ms": 2000.0,
            "span_count": 3,
            "model_call_count": 1,
            "tool_call_count": 1,
            "tool_error_count": 0,
            "http_429_count": 0,
            "content_missing_call_count": 0,
            "platform_guardrail_blocks": 0,
            "rule0_pass": True,
            "is_loop_or_error_tail": False,
            "is_healthy_slow_tail": False,
            "primary_traffic_type": "production_inference",
            "weighted_burndown_tokens": 100.0,
            "production_inference_usd": 0.10,
            "conflict_escalation_usd": 0.0,
            "agent_gym_eval_usd": 0.0,
            "span_autorater_judge_usd": 0.0,
            "optimize_usd": 0.0,
            "unclassified_usd": 0.0,
            "unpriced_spans": 0,
            "trace_span_cost_usd": 0.10,
            "cache_hit_ratio": 0.5,
            "has_trace_payload": True,
            "empty_prior_history": True,
            "payload_read_incomplete": False,
            "prior_turn_matched": True,
            "staged_at": "2026-11-25T10:05:00+00:00",
        },
        {
            "trace_id": "tr-other-99",
            "apphub_application_id": "wealth-advisor-uc9",
            "business_unit": "wealth",
            "tenant_id": "tenant-other-99",
            "effective_risk_tier": "RL-2",
            "risk_tier": "RL-2",
            "outcome_id": "out-other-99",
            "outcome_owner": "wealth-advisor-uc9",
            "conversation_id": "conv-o99",
            "previous_interaction_id": None,
            "trace_start_time": "2026-11-25T10:01:00+00:00",
            "trace_end_time": "2026-11-25T10:01:05+00:00",
            "trace_latency_ms": 5000.0,
            "span_count": 3,
            "model_call_count": 1,
            "tool_call_count": 1,
            "tool_error_count": 1,
            "http_429_count": 0,
            "content_missing_call_count": 0,
            "platform_guardrail_blocks": 0,
            "rule0_pass": False,
            "is_loop_or_error_tail": True,
            "is_healthy_slow_tail": False,
            "primary_traffic_type": "production_inference",
            "weighted_burndown_tokens": 900.0,
            "production_inference_usd": 9.90,
            "conflict_escalation_usd": 0.0,
            "agent_gym_eval_usd": 0.0,
            "span_autorater_judge_usd": 0.0,
            "optimize_usd": 0.0,
            "unclassified_usd": 0.0,
            "unpriced_spans": 0,
            "trace_span_cost_usd": 9.90,
            "cache_hit_ratio": 0.0,
            "has_trace_payload": True,
            "empty_prior_history": True,
            "payload_read_incomplete": False,
            "prior_turn_matched": True,
            "staged_at": "2026-11-25T10:05:00+00:00",
        },
    ]
    staged_outcomes = [
        {
            "outcome_id": "out-acme-1",
            "apphub_application_id": "wealth-advisor-uc9",
            "business_unit": "wealth",
            "tenant_id": "tenant-acme-01",
            "outcome_status": "succeeded",
            "is_verified": True,
            "activity_time": "2026-11-25T10:00:02+00:00",
            "unpriced_spans": 0,
            "ai_cost_usd": 0.10,
            "infra_cost_usd": 0.0,
            "staged_at": "2026-11-25T10:05:00+00:00",
        },
        {
            "outcome_id": "out-other-99",
            "apphub_application_id": "wealth-advisor-uc9",
            "business_unit": "wealth",
            "tenant_id": "tenant-other-99",
            "outcome_status": "succeeded",
            "is_verified": True,
            "activity_time": "2026-11-25T10:01:05+00:00",
            "unpriced_spans": 0,
            "ai_cost_usd": 9.90,
            "infra_cost_usd": 0.0,
            "staged_at": "2026-11-25T10:05:00+00:00",
        },
    ]

    with open_silver_gold_runner(
        RunnerConfig(
            default_dataset="agent_eval",
            session_user="analyst-tenant-a@example.invalid",
        )
    ) as runner:
        runner.load_rows(
            "silver_internal.unit_entitlements",
            [
                {
                    "principal_email": "analyst-tenant-a@example.invalid",
                    "business_unit": "wealth",
                    "tenant_id": "tenant-acme-01",
                }
            ],
            append=True,
        )
        runner.load_rows("agent_eval_internal.gold_staged_trace_eval", staged_traces, append=True)
        runner.load_rows("agent_eval_internal.gold_staged_outcomes", staged_outcomes, append=True)
        runner.load_rows(
            "agent_eval.gold_staged_open_kpi",
            [
                {
                    "usage_hour": "2026-11-25T10:00:00+00:00",
                    "timestamp": "2026-11-25T10:00:00Z",
                    "apphub_application_id": "wealth-advisor-uc9",
                    "tenant": "wealth-advisor-uc9",
                    "model_name": "gemini-3.8-flash",
                    "model": "gemini-3.8-flash",
                    "finops_traffic_type": "production_inference",
                    "serving_traffic_type": "STANDARD",
                    "distinct_traces": 2,
                    "span_count": 2,
                    "ok_span_count": 2,
                    "error_span_count": 0,
                    "http_429_count": 0,
                    "spillover_span_count": 2,
                    "input_tokens": 1000,
                    "cached_input_tokens": 0,
                    "cached_tokens": 0,
                    "cache_write_5m_tokens": 0,
                    "cache_write_1h_tokens": 0,
                    "uncached_input_tokens": 1000,
                    "output_tokens": 200,
                    "thinking_tokens": 0,
                    "thoughts_tokens": 0,
                    "long_context_cliff_spans": 0,
                    "weighted_burndown_tokens": 1800.0,
                    "precomputed_weighted_burndown_tokens": 1800.0,
                    "total_cost_usd": 10.0,
                    "staged_at": "2026-11-25T10:05:00+00:00",
                }
            ],
            append=True,
        )

        # Tenant-scoped analyst sees ONLY tenant-acme-01 traces, spend, and outcomes (0 UC9 leakage).
        tr_rows = runner.query("SELECT trace_id, tenant_id FROM `agent_eval.v_staged_trace_eval`")
        assert tr_rows.rows == ({"trace_id": "tr-acme-1", "tenant_id": "tenant-acme-01"},)

        summary = runner.query(
            "SELECT total_traces, production_inference_usd, outcome_verified_count, outcome_cost_per_verified_ai_usd "
            "FROM `agent_eval.v_staged_agent_eval_and_finops_summary`"
        ).rows
        assert len(summary) == 1
        assert summary[0]["total_traces"] == 1
        assert float(str(summary[0]["production_inference_usd"])) == pytest.approx(0.10, abs=1e-9)
        assert summary[0]["outcome_verified_count"] == 1
        assert float(str(summary[0]["outcome_cost_per_verified_ai_usd"])) == pytest.approx(
            0.10, abs=1e-9
        )

        # Open staged KPI remains readable regardless of restricted tenant scope.
        kpi = runner.query("SELECT * FROM `agent_eval.v_staged_capacity_hourly_burndown_export`")
        assert len(kpi.rows) == 1

    # Unentitled caller sees 0 rows in restricted staged views.
    with open_silver_gold_runner(
        RunnerConfig(
            default_dataset="agent_eval",
            session_user="nobody@example.invalid",
        )
    ) as denied_runner:
        denied_runner.load_rows(
            "agent_eval_internal.gold_staged_trace_eval", staged_traces, append=True
        )
        denied_runner.load_rows(
            "agent_eval_internal.gold_staged_outcomes", staged_outcomes, append=True
        )
        assert denied_runner.query("SELECT * FROM `agent_eval.v_staged_trace_eval`").rows == ()
        assert (
            denied_runner.query(
                "SELECT * FROM `agent_eval.v_staged_agent_eval_and_finops_summary`"
            ).rows
            == ()
        )


def test_repo_root_reference_keeps_summary_under_cap() -> None:
    """Guard `sql/core/summary_views.sql` stays compact after OOM fix."""
    assert (REPO_ROOT / "sql" / "core" / "summary_views.sql").stat().st_size <= 35_000
