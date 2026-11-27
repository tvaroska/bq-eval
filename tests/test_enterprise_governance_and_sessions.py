"""Shipped Gold SQL view tests for governance (`RL-1`..`RL-5`), open-only AST safety, 5m SLO/429 breaches, and NFR gates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlglot import exp

from bq_eval.core.contract.otel import FinOpsTrafficType, GenAISpanKind, ServingTrafficType
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)
from bq_eval.runner.engine.bronze_bridge import BRONZE_LOG_EXTRA_COLS
from tests.conftest import (
    SQL_DIR,
    eval_traces,
    make_span,
    open_runner_for_spans,
    parse_bigquery_sql,
    seed_sme_confusion,
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
        "turn_prompt",
        "turn_context",
        "turn_output",
    }
)


_SAFE_OPEN_TVF_SOURCES: frozenset[str] = frozenset(
    {
        "traces_open",
        "outcomes_open",
        "outcome_links",
        "tool_calls_open",
        "events_open",
        "model_calls",
        "requests",
        "gold_call_costs",
    }
)


def _extract_from_table_name(tbl: exp.Table) -> str:
    if tbl.name:
        return tbl.name.lower().rsplit(".", 1)[-1]
    if isinstance(tbl.this, exp.Anonymous) and tbl.this.name:
        return tbl.this.name.lower().rsplit(".", 1)[-1]
    return ""


def _check_view_zero_egress(
    view_name: str,
    body: exp.Expression,
    declared_views: frozenset[str],
) -> list[str]:
    violations: list[str] = []
    safe_star_sources = (
        declared_views
        | _SAFE_OPEN_TVF_SOURCES
        | {cte.alias.lower() for cte in body.find_all(exp.CTE) if cte.alias}
    )
    if isinstance(body, exp.Select) and any(isinstance(e, exp.Star) for e in body.expressions):
        violations.append(f"{view_name}: top-level SELECT * is forbidden in zero-egress views")
    for sub_select in body.find_all(exp.Select):
        if not any(isinstance(e, exp.Star) for e in sub_select.expressions):
            continue
        from_clause = sub_select.args.get("from_")
        from_tables = (
            [n for t in from_clause.find_all(exp.Table) if (n := _extract_from_table_name(t))]
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
    stmts = parse_bigquery_sql(sql_text, label)
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
    """`v_inference_request_reconciliation`, `v_app_risk_governance` & `v_lob_model_lifecycle_export` enforce gates."""
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
            http_status_code=429,
            input_tokens=5_000,
            cached_input_tokens=0,
            output_tokens=500,
            thinking_tokens=0,
            base_time=base,
        ),
    ]

    with open_runner_for_spans(otel_spans) as runner:
        runner.load_rows(
            "obs_logs._AllLogs",
            [
                {
                    "insert_id": "gw-drift-1",
                    "timestamp": base.isoformat(),
                    "receive_timestamp": base.isoformat(),
                    "log_id": "inference_request",
                    "log_name": "projects/test-proj/logs/inference_request",
                    "trace": "tr-rec-2",
                    "span_id": "sp-rec-2",
                    "labels": {"request_type_header": "shared"},
                    "json_payload": {
                        "client_request_id": "req-drift-2",
                        "trace_id": "tr-rec-2",
                        "span_id": "sp-rec-2",
                        "attempt_index": 1,
                        "caller_app_id": "gov-rl4-app",
                        "request_model": "gemini-3.8-flash",
                        "response_model": "gemini-3.8-flash",
                        "serving_tier": "STANDARD",
                        "server_traffic_type_present": True,
                        "http_status_code": 429,
                        "input_tokens": 9_000,
                        "output_tokens": 1_000,
                        "traffic_type": "production_inference",
                    },
                    "otel": {"resource": {"attributes": {"service.name": "agent-service"}}},
                }
            ],
            extra_columns=BRONZE_LOG_EXTRA_COLS,
            append=True,
        )
        runner.load_rows(
            "silver_ref.app_registry",
            [
                {
                    "app_id": "gov-rl1-app",
                    "apphub_application_id": "gov-rl1-app",
                    "allowed_landing_places": ["test-proj.obs_traces._AllSpans"],
                    "owner": "rl1-owner",
                    "business_unit": "core_banking",
                    "risk_tier": "RL-1",
                    "effective_from": "2025-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
                {
                    "app_id": "gov-rl4-app",
                    "apphub_application_id": "gov-rl4-app",
                    "allowed_landing_places": ["test-proj.obs_traces._AllSpans"],
                    "owner": "rl4-owner",
                    "business_unit": "internal_ops",
                    "risk_tier": "RL-4",
                    "effective_from": "2025-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
            ],
            append=True,
        )
        runner.load_rows(
            "agent_eval.workload_thresholds",
            [
                {
                    "apphub_application_id": "gov-rl1-app",
                    "cost_center": "CC-RL1",
                    "sla_latency_ms": 3000.0,
                    "autonomy_tier": "SUPERVISED_AUTONOMOUS",
                    "lifecycle_track_policy": "long_term_ga_only",
                    "funded_gsus": 4.0,
                    "spillover_tier": "priority",
                    "pt_commitment_months": 12,
                    "updated_at": "2026-11-01T00:00:00+00:00",
                },
                {
                    "apphub_application_id": "gov-rl4-app",
                    "cost_center": "CC-RL4",
                    "sla_latency_ms": 5000.0,
                    "autonomy_tier": "HUMAN_IN_LOOP",
                    "lifecycle_track_policy": "short_term_ga_allowed",
                    "funded_gsus": 0.0,
                    "spillover_tier": "standard",
                    "pt_commitment_months": 0,
                    "updated_at": "2026-11-01T00:00:00+00:00",
                },
            ],
        )
        runner.load_rows(
            "silver_ref.allowed_models",
            [
                {
                    "app_id": "*",
                    "model": "gemini-4-carbon",
                    "is_allowed": True,
                    "invokable_status": "active",
                    "lifecycle_tier": "short_term_ga",
                    "eol_date": "2027-06-30",
                    "migration_target_model": "gemini-4-argon",
                    "zdr_allowed": True,
                    "effective_from": "2025-01-01T00:00:00+00:00",
                    "effective_to": None,
                }
            ],
            append=True,
        )
        sql_rec = {
            str(r["apphub_application_id"]): dict(r)
            for r in runner.query(
                "SELECT * FROM `agent_eval.v_inference_request_reconciliation`"
            ).rows
        }
        sql_gov = {
            str(r["apphub_application_id"]): dict(r)
            for r in runner.query("SELECT * FROM `agent_eval.v_app_risk_governance`").rows
        }
        sql_lob = {
            str(r["apphub_application_id"]): dict(r)
            for r in runner.query("SELECT * FROM `agent_eval.v_lob_model_lifecycle_export`").rows
        }

    assert sql_rec["gov-rl1-app"]["reconciliation_status"] == "RECONCILED"
    assert int(str(sql_rec["gov-rl1-app"]["pt_eligible_involuntary_spillover_count"])) == 1
    assert sql_rec["gov-rl4-app"]["reconciliation_status"] == "DRIFT_ALERT"
    assert int(str(sql_rec["gov-rl4-app"]["intentional_shared_bypass_count"])) == 1
    assert float(str(sql_gov["gov-rl1-app"]["required_ac2_gate"])) == pytest.approx(0.85)
    assert bool(sql_gov["gov-rl1-app"]["pt_commitment_allowed"]) is False
    assert float(str(sql_gov["gov-rl4-app"]["required_ac2_gate"])) == pytest.approx(0.75)
    assert bool(sql_gov["gov-rl4-app"]["pt_commitment_allowed"]) is True
    assert sql_lob["gov-rl1-app"]["migration_target_model"] == "gemini-4-argon"


def test_open_only_gold_cost_and_capacity_zero_raw_payload_ast_proof() -> None:
    """Open-only Gold cost & capacity views project zero raw prompts or unsafe `SELECT *`, with positive controls."""
    combined_sql = (
        (SQL_DIR / "core" / "gold_cost_views.sql").read_text(encoding="utf-8")
        + "\n"
        + (SQL_DIR / "finops" / "capacity_views.sql").read_text(encoding="utf-8")
    )
    violations = _find_zero_egress_ast_violations(combined_sql, "open_gold_views")
    assert not violations, f"Open-only Gold view zero-egress violation: {violations}"

    star_leak = _find_zero_egress_ast_violations(
        "CREATE VIEW `agent_eval.v_bad_star` AS SELECT * FROM `agent_eval.external_tbl`;",
        "star_leak",
    )
    assert any("SELECT *" in v for v in star_leak)

    col_leak = _find_zero_egress_ast_violations(
        "CREATE VIEW `agent_eval.v_bad_col` AS SELECT ';' AS sep, user_prompt FROM `agent_eval.t`;",
        "col_leak",
    )
    assert any("user_prompt" in v for v in col_leak)


def test_sessions_5m_breaches_burst_metrics_and_optimize_lane_sql_views() -> None:
    """Session lineage, 5m SLO/429 breaches with `burst_metrics_1m`, and `optimize` spend lane execute cleanly."""
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

    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "silver_open.burst_metrics_1m",
            [
                {
                    "minute_utc": "2026-11-22T16:01:00+00:00",
                    "trace_owner_app_id": "live-app",
                    "model": "gemini-3.8-flash",
                    "peak_burst_ratio": 3.25,
                    "gateway_failover_count": 4,
                    "caller_retry_count": 2,
                }
            ],
            append=True,
        )
        bq_sess = runner.query("SELECT * FROM `agent_eval.v_interaction_session_lineage`").rows[0]
        bq_breach = runner.query("SELECT * FROM `agent_eval.v_live_slo_and_429_breaches`").rows[0]
        bq_opt = runner.query(
            "SELECT optimize_usd, unclassified_usd FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'live-app'"
        ).rows[0]

    py_sum = eval_traces(spans)["live-app"]
    assert int(str(bq_sess["turn_count"])) == 2
    assert int(str(bq_sess["first_failure_turn_index"])) == 1
    assert bool(bq_sess["session_rule0_pass"]) is False
    assert bq_breach["breach_reason"] == "HTTP_429_STORM"
    assert bool(bq_breach["is_breach"]) is True
    assert float(str(bq_breach["peak_burst_ratio"])) == pytest.approx(3.25)
    assert int(str(bq_breach["gateway_failover_count"])) == 4
    assert float(str(bq_opt["optimize_usd"])) == pytest.approx(py_sum.optimize_usd, abs=1e-12)
    assert float(str(bq_opt["optimize_usd"])) > 0.0
    assert float(str(bq_opt["unclassified_usd"])) == 0.0


def test_clause_scoped_nfr_gate_sql_view() -> None:
    """`v_step3_policy_nfr_gate` separates amended-clause flips from unamended negative flips."""
    with open_runner_for_spans() as runner:
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "e-b1",
                    "eval_run_id": "run-base",
                    "trace_id": "tr-c1",
                    "apphub_application_id": "pol-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "FULL_100PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 100,
                    "judge_output_tokens": 20,
                    "judge_thinking_tokens": 0,
                    "policy_sha256": "sha-v1",
                    "amended_clause_ids": None,
                    "target_clause_id": "CL-4.2",
                    "evaluated_at": "2026-11-22T10:00:00+00:00",
                },
                {
                    "eval_id": "e-c1",
                    "eval_run_id": "run-cand",
                    "trace_id": "tr-c1",
                    "apphub_application_id": "pol-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "FULL_100PCT",
                    "rule0_pass": True,
                    "ordinal_score": 2,
                    "binary_pass": False,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 100,
                    "judge_output_tokens": 20,
                    "judge_thinking_tokens": 0,
                    "policy_sha256": "sha-v2",
                    "amended_clause_ids": "CL-4.2,CL-9.1",
                    "target_clause_id": "CL-4.2",
                    "evaluated_at": "2026-11-22T11:00:00+00:00",
                },
                {
                    "eval_id": "e-b2",
                    "eval_run_id": "run-base",
                    "trace_id": "tr-c2",
                    "apphub_application_id": "pol-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "FULL_100PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 100,
                    "judge_output_tokens": 20,
                    "judge_thinking_tokens": 0,
                    "policy_sha256": "sha-v1",
                    "amended_clause_ids": None,
                    "target_clause_id": "CL-1.0",
                    "evaluated_at": "2026-11-22T10:00:00+00:00",
                },
                {
                    "eval_id": "e-c2",
                    "eval_run_id": "run-cand",
                    "trace_id": "tr-c2",
                    "apphub_application_id": "pol-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "FULL_100PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 100,
                    "judge_output_tokens": 20,
                    "judge_thinking_tokens": 0,
                    "policy_sha256": "sha-v2",
                    "amended_clause_ids": "CL-4.2,CL-9.1",
                    "target_clause_id": "CL-1.0",
                    "evaluated_at": "2026-11-22T11:00:00+00:00",
                },
            ],
            append=True,
        )
        nfr_sql_row = runner.query("SELECT * FROM `agent_eval.v_step3_policy_nfr_gate`").rows[0]

    assert int(str(nfr_sql_row["amended_clause_flips"])) == 1
    assert int(str(nfr_sql_row["unamended_negative_flips"])) == 0
    assert bool(nfr_sql_row["passes_nfr_gate"]) is True


def test_breaches_exclude_tool_spans_and_reconciliation_normalizes_model_suffixes() -> None:
    """`v_live_slo_and_429_breaches` excludes tool/root spans; reconciliation & LOB strip version/`@region` suffixes."""
    base = datetime(2026, 11, 22, 17, 5, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-norm-1",
            "sp-norm-root",
            app_id="norm-app",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            model_name="none",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=8000.0,
            http_status_code=200,
            base_time=base,
        ),
        make_span(
            "tr-norm-1",
            "sp-norm-llm",
            parent_span_id="sp-norm-root",
            span_name="chat",
            app_id="norm-app",
            span_kind=GenAISpanKind.CHAT,
            model_name="publishers/google/models/gemini-3.8-flash-001@us-central1",
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            latency_ms=420.0,
            http_status_code=429,
            offset_ms=50,
            base_time=base,
        ),
        *[
            make_span(
                "tr-norm-1",
                f"sp-norm-tool-{i}",
                parent_span_id="sp-norm-root",
                span_name="execute_tool",
                app_id="norm-app",
                span_kind=GenAISpanKind.EXECUTE_TOOL,
                model_name="none",
                tool_name="lookup",
                tool_call_status="SUCCESS",
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                thinking_tokens=0,
                latency_ms=15.0,
                http_status_code=200,
                offset_ms=100 + i,
                base_time=base,
            )
            for i in range(10)
        ],
        make_span(
            "tr-norm-tooltok",
            "sp-norm-tooltok-chat",
            app_id="norm-tool-app",
            span_kind=GenAISpanKind.CHAT,
            model_name="gemini-3.8-flash",
            input_tokens=2_000,
            cached_input_tokens=0,
            output_tokens=200,
            thinking_tokens=0,
            latency_ms=300.0,
            http_status_code=429,
            base_time=base,
        ),
        make_span(
            "tr-norm-tooltok",
            "sp-norm-tooltok-exec",
            parent_span_id="sp-norm-tooltok-chat",
            span_name="execute_tool",
            app_id="norm-tool-app",
            span_kind=GenAISpanKind.EXECUTE_TOOL,
            model_name="gemini-3.8-flash",
            tool_name="nested_llm_tool",
            tool_call_status="SUCCESS",
            input_tokens=1_000,
            cached_input_tokens=0,
            output_tokens=100,
            thinking_tokens=0,
            latency_ms=9000.0,
            http_status_code=200,
            offset_ms=10,
            base_time=base,
        ),
    ]

    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "obs_logs._AllLogs",
            [
                {
                    "insert_id": "gw-norm-1",
                    "timestamp": base.isoformat(),
                    "receive_timestamp": base.isoformat(),
                    "log_id": "inference_request",
                    "log_name": "projects/test-proj/logs/inference_request",
                    "trace": "tr-norm-1",
                    "span_id": "sp-norm-llm",
                    "labels": {},
                    "json_payload": {
                        "client_request_id": "req-norm-1",
                        "trace_id": "tr-norm-1",
                        "span_id": "sp-norm-llm",
                        "attempt_index": 1,
                        "caller_app_id": "norm-app",
                        "request_model": "gemini-3.8-flash@us-central1",
                        "response_model": "gemini-3.8-flash",
                        "serving_tier": "STANDARD",
                        "server_traffic_type_present": True,
                        "http_status_code": 429,
                        "input_tokens": 10_000,
                        "output_tokens": 1_000,
                        "traffic_type": "production_inference",
                    },
                    "otel": {"resource": {"attributes": {"service.name": "agent-service"}}},
                }
            ],
            extra_columns=BRONZE_LOG_EXTRA_COLS,
            append=True,
        )
        runner.load_rows(
            "silver_ref.allowed_models",
            [
                {
                    "app_id": "*",
                    "model": "gemini-3.8-flash",
                    "is_allowed": True,
                    "invokable_status": "active",
                    "lifecycle_tier": "long_term_ga",
                    "eol_date": "2028-01-01",
                    "migration_target_model": "gemini-4-hydrogen",
                    "zdr_allowed": True,
                    "effective_from": "2025-01-01T00:00:00+00:00",
                    "effective_to": None,
                }
            ],
            append=True,
        )
        breach_rows = runner.query(
            "SELECT span_count, http_429_count, http_429_rate_pct, p95_latency_ms, breach_reason "
            "FROM `agent_eval.v_live_slo_and_429_breaches` "
            "WHERE apphub_application_id = 'norm-app'"
        ).rows
        tooltok_breach = runner.query(
            "SELECT span_count, http_429_rate_pct, p95_latency_ms "
            "FROM `agent_eval.v_live_slo_and_429_breaches` "
            "WHERE apphub_application_id = 'norm-tool-app'"
        ).rows[0]
        rec_rows = runner.query(
            "SELECT model_name, otel_span_count, server_request_count, reconciliation_status "
            "FROM `agent_eval.v_inference_request_reconciliation` "
            "WHERE apphub_application_id = 'norm-app'"
        ).rows
        lob_row = runner.query(
            "SELECT migration_target_model, total_cost_usd "
            "FROM `agent_eval.v_lob_model_lifecycle_export` "
            "WHERE apphub_application_id = 'norm-app'"
        ).rows[0]

    assert len(breach_rows) == 1
    assert int(str(breach_rows[0]["span_count"])) == 1
    assert int(str(breach_rows[0]["http_429_count"])) == 1
    assert float(str(breach_rows[0]["http_429_rate_pct"])) == pytest.approx(100.0)
    assert float(str(breach_rows[0]["p95_latency_ms"])) == pytest.approx(420.0)
    assert breach_rows[0]["breach_reason"] == "HTTP_429_STORM"
    assert int(str(tooltok_breach["span_count"])) == 1
    assert float(str(tooltok_breach["http_429_rate_pct"])) == pytest.approx(100.0)
    assert float(str(tooltok_breach["p95_latency_ms"])) == pytest.approx(300.0)

    assert len(rec_rows) == 1
    assert rec_rows[0]["model_name"] == "gemini-3.8-flash"
    assert int(str(rec_rows[0]["otel_span_count"])) == 1
    assert int(str(rec_rows[0]["server_request_count"])) == 1
    assert rec_rows[0]["reconciliation_status"] == "RECONCILED"
    assert lob_row["migration_target_model"] == "gemini-4-hydrogen"
    assert float(str(lob_row["total_cost_usd"])) > 0.0


def test_eval_results_is_synthetic_excluded_from_calibration_and_surfaced_in_runs() -> None:
    """`eval_results.is_synthetic = TRUE` is audited in `v_step3_eval_runs` and excluded from calibration."""
    base = datetime(2026, 11, 22, 18, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            f"tr-syn-{i:02d}",
            f"sp-syn-{i:02d}",
            app_id="syn-app",
            outcome_id=f"out-syn-{i:02d}",
            outcome_owner="syn-app",
            offset_s=i,
            base_time=base,
        )
        for i in range(12)
    ]

    with open_runner_for_spans(
        spans,
        current_date_utc="2026-11-22",
        current_timestamp_utc="2026-11-22T18:30:00+00:00",
    ) as runner:
        syn_rows: list[dict[str, object]] = [
            {
                "eval_id": f"ev-syn-{i:02d}",
                "eval_run_id": "run-syn-1",
                "trace_id": f"tr-syn-{i:02d}",
                "outcome_id": f"out-syn-{i:02d}",
                "apphub_application_id": "syn-app",
                "rubric_id": "groundedness_4pt_v1",
                "judge_model": "gemini-3.8-flash",
                "sampling_stratum": "CANARY_5PCT",
                "rule0_pass": True,
                "ordinal_score": 4,
                "binary_pass": True,
                "judge_status": "SCORED",
                "judge_input_tokens": 500,
                "judge_output_tokens": 50,
                "judge_thinking_tokens": 0,
                "is_synthetic": True,
                "evaluated_at": "2026-11-22T18:10:00+00:00",
            }
            for i in range(12)
        ]
        syn_rows.append(
            {
                "eval_id": "ev-syn-flip",
                "eval_run_id": "run-syn-2",
                "trace_id": "tr-syn-00",
                "outcome_id": "out-syn-00",
                "apphub_application_id": "syn-app",
                "rubric_id": "groundedness_4pt_v1",
                "judge_model": "gemini-3.8-flash",
                "sampling_stratum": "CANARY_5PCT",
                "rule0_pass": True,
                "ordinal_score": 1,
                "binary_pass": False,
                "judge_status": "SCORED",
                "judge_input_tokens": 500,
                "judge_output_tokens": 50,
                "judge_thinking_tokens": 0,
                "is_synthetic": True,
                "evaluated_at": "2026-11-22T18:20:00+00:00",
            }
        )
        runner.load_rows("agent_eval.eval_results", syn_rows, append=False)
        seed_sme_confusion(runner, "syn-app", ConfusionMatrix(tp=10, fn=0, tn=10, fp=0))
        run_row = runner.query(
            "SELECT evaluated_traces, synthetic_traces FROM `agent_eval.v_step3_eval_runs` "
            "WHERE eval_run_id = 'run-syn-1'"
        ).rows[0]
        sum_row = runner.query(
            "SELECT judged_samples, calibration_status, calibrated_pass_rate "
            "FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'syn-app'"
        ).rows[0]
        cpvo_row = runner.query(
            "SELECT measured_judge_cost_usd "
            "FROM `agent_eval.v_enterprise_cost_per_verified_outcome` "
            "WHERE apphub_application_id = 'syn-app'"
        ).rows[0]
        nfr_rows = runner.query(
            "SELECT * FROM `agent_eval.v_step3_policy_nfr_gate` "
            "WHERE apphub_application_id = 'syn-app'"
        ).rows
        quota_row = runner.query(
            "SELECT tokens_used_today FROM `agent_eval.v_step2_daily_quota_status`"
        ).rows[0]
        app_quota_rows = runner.query(
            "SELECT * FROM `agent_eval.v_step2_app_daily_quota_status` "
            "WHERE apphub_application_id = 'syn-app'"
        ).rows
        gov_row = runner.query(
            "SELECT gwet_ac2, passes_ac2_gate FROM `agent_eval.v_app_risk_governance` "
            "WHERE apphub_application_id = 'syn-app'"
        ).rows[0]

    assert int(str(run_row["evaluated_traces"])) == 12
    assert int(str(run_row["synthetic_traces"])) == 12
    assert int(str(sum_row["judged_samples"])) == 0
    assert sum_row["calibration_status"] == "UNJUDGED"
    assert sum_row["calibrated_pass_rate"] is None
    assert float(str(cpvo_row["measured_judge_cost_usd"])) == pytest.approx(0.0, abs=1e-12)
    assert nfr_rows == ()
    assert int(str(quota_row["tokens_used_today"])) == 0
    assert app_quota_rows == ()
    assert gov_row["gwet_ac2"] is None
    assert gov_row["passes_ac2_gate"] is None

    gen_sql = build_ai_generate_table_judge_sql(
        project_id="demo-proj", canary_sample_pct=5, canary_salt="salt-a"
    )
    eval_agent_persist_sql = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        canary_sample_pct=10,
        run_id="run-filter-1",
        persist_run_id="run-ea-1",
        canary_salt="salt-b",
    )
    eval_agent_read_sql = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        canary_sample_pct=10,
        canary_salt="salt-c",
    )
    parse_bigquery_sql(gen_sql, "gen_sql_salted")
    parse_bigquery_sql(eval_agent_persist_sql, "eval_agent_persist_sql_salted")
    parse_bigquery_sql(eval_agent_read_sql, "eval_agent_read_sql_salted")
    assert "FALSE AS is_synthetic" in gen_sql
    assert "FARM_FINGERPRINT(CONCAT('salt-a:', t.trace_id))" in gen_sql
    assert "FALSE AS is_synthetic" in eval_agent_persist_sql
    assert "FARM_FINGERPRINT(CONCAT('salt-b:', cand.trace_id))" in eval_agent_persist_sql
    assert "cand.trace_id IN (" in eval_agent_persist_sql
    assert "FARM_FINGERPRINT(CONCAT('salt-c:', trace_id))" in eval_agent_read_sql
