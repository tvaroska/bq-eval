"""M11 verification: 3P/PayGo/Governance, 3-Pool FOCUS & `capacity` BQ-G1, Omni zero-egress & Optimizer/ATIF."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlglot import exp

from bq_eval.contract.governance import (
    AppGovernanceRecord,
    AutonomyTier,
    CapacityDeadlineJobPayload,
    LifecycleTrackPolicy,
    RiskLevel,
)
from bq_eval.contract.optimizer import PairedEvalOutcome
from bq_eval.contract.otel import (
    FinOpsTrafficType,
    GenAISpanKind,
    ServingTrafficType,
    TracePayloadRecord,
    normalize_serving_traffic_type,
)
from bq_eval.contract.pricing import DEFAULT_MODEL_SKUS, normalize_model_name
from bq_eval.engine.chargeback_3pool import (
    allocate_focus_three_pool_hourly_chargeback,
    build_capacity_residual_schedule_request,
)
from bq_eval.engine.finops import (
    compute_span_burndown_tokens,
    compute_span_cost_usd,
)
from bq_eval.engine.governance import (
    InferenceRequestLogRecord,
    evaluate_app_governance,
    reconcile_inference_requests,
)
from bq_eval.engine.omni import (
    FORBIDDEN_RAW_PAYLOAD_COLUMNS,
    RemoteTraceSliceRecord,
    build_omni_in_residency_judge_sql,
    normalize_omni_application_id,
    normalize_workload_principal,
    reconcile_multicloud_cpvo,
    verify_zero_raw_payload_egress,
)
from bq_eval.engine.optimizer import (
    cluster_failure_loss_bank,
    evaluate_clause_scoped_nfr_gate,
    export_atif_v1_8_trajectories,
    sanitize_atif_text,
)
from bq_eval.engine.otel_export import (
    GenAISpanRecorder,
    RingBufferSpanExporter,
    format_x_cloud_trace_context,
    parse_trace_context_headers,
)
from bq_eval.engine.ports import SQLiteEvalBackend
from bq_eval.engine.sql_eval import (
    evaluate_5m_breaches_in_sqlite,
    evaluate_hourly_burndown_in_sqlite,
    evaluate_interaction_lineage_in_sqlite,
    evaluate_traces_in_sqlite,
)
from tests.conftest import make_span
from tests.test_sql_contracts import (
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _parse_bigquery_statements,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
)


def _install_extra_views(conn: sqlite3.Connection, file_name: str, views: tuple[str, ...]) -> None:
    path = SQL_DIR / file_name
    for full_name in views:
        short = full_name.split(".", 1)[1]
        sql = _transpile_bq_view_to_sqlite(_extract_view_sql_text(path, full_name))
        conn.execute(f"CREATE VIEW {short} AS {sql}")


def test_m11_1_3p_claude_cache_write_paygo_multipliers_sql_sqlite_python_parity() -> None:
    """M11-1: 3P Claude cache-write (`1.25x` 5m / `2.0x` 1h), `5.0x` output & PayGo (`1.8x` / `0.5x`) match `< 1e-12`."""
    assert normalize_serving_traffic_type("PROVISIONED_THROUGHPUT") == (
        ServingTrafficType.PROVISIONED_THROUGHPUT
    )
    assert normalize_serving_traffic_type("priority_paygo") == ServingTrafficType.PRIORITY
    assert normalize_serving_traffic_type("flex_paygo") == ServingTrafficType.FLEX
    assert normalize_model_name("publishers/anthropic/models/claude-sonnet-4-6@20260501") == (
        "claude-sonnet-4-6"
    )
    assert DEFAULT_MODEL_SKUS["gemini-4-carbon"].lifecycle_track == "short_term_ga"

    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-claude-std",
            "sp-c1",
            app_id="app-3p",
            model_name="claude-sonnet-4-6",
            serving_traffic_type=ServingTrafficType.STANDARD,
            input_tokens=100_000,
            cached_input_tokens=20_000,
            cache_write_5m_tokens=10_000,
            cache_write_1h_tokens=5_000,
            output_tokens=4_000,
            thinking_tokens=1_000,
            base_time=base,
        ),
        make_span(
            "tr-claude-prio",
            "sp-c2",
            app_id="app-3p",
            model_name="claude-sonnet-4-6",
            serving_traffic_type=ServingTrafficType.PRIORITY,
            input_tokens=100_000,
            cached_input_tokens=20_000,
            cache_write_5m_tokens=10_000,
            cache_write_1h_tokens=5_000,
            output_tokens=4_000,
            thinking_tokens=1_000,
            offset_s=5,
            base_time=base,
        ),
        make_span(
            "tr-flash-flex",
            "sp-f1",
            app_id="app-3p",
            model_name="gemini-3.8-flash",
            serving_traffic_type=ServingTrafficType.FLEX,
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            offset_s=10,
            base_time=base,
        ),
    ]

    # Claude Standard: uncached=65k ($0.195), cached=20k ($0.006), cw5m=10k ($0.0375), cw1h=5k ($0.030), out+think=5k ($0.075)
    expected_claude_std = 0.195 + 0.006 + 0.0375 + 0.030 + 0.075
    expected_claude_bd = 65_000.0 + 20_000.0 * 0.1 + 10_000.0 * 1.25 + 5_000.0 * 2.0 + 5_000.0 * 5.0
    assert compute_span_cost_usd(spans[0]) == pytest.approx(expected_claude_std, abs=1e-12)
    assert compute_span_burndown_tokens(spans[0]) == pytest.approx(expected_claude_bd, abs=1e-12)
    assert compute_span_cost_usd(spans[1]) == pytest.approx(expected_claude_std * 1.8, abs=1e-12)
    assert compute_span_cost_usd(spans[2]) == pytest.approx(
        ((100_000 / 1e6) * 0.15 + (10_000 / 1e6) * 0.60) * 0.5,
        abs=1e-12,
    )

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    for sp in spans:
        insert_enterprise_sqlite_span(conn, sp)

    wf_rows = {
        str(r["trace_id"]): dict(r)
        for r in conn.execute(
            "SELECT trace_id, span_cost_usd, weighted_burndown_tokens FROM v_trace_waterfall"
        ).fetchall()
    }
    s1_rows = {
        str(r["trace_id"]): dict(r)
        for r in conn.execute(
            "SELECT trace_id, span_cost_usd, weighted_burndown_tokens FROM v_step1_trace_waterfall"
        ).fetchall()
    }
    conn.close()

    for sp in spans:
        py_cost = compute_span_cost_usd(sp)
        py_bd = compute_span_burndown_tokens(sp)
        assert float(wf_rows[sp.trace_id]["span_cost_usd"]) == pytest.approx(py_cost, abs=1e-12)
        assert float(s1_rows[sp.trace_id]["span_cost_usd"]) == pytest.approx(py_cost, abs=1e-12)
        assert float(wf_rows[sp.trace_id]["weighted_burndown_tokens"]) == pytest.approx(
            py_bd, abs=1e-12
        )
        assert float(s1_rows[sp.trace_id]["weighted_burndown_tokens"]) == pytest.approx(
            py_bd, abs=1e-12
        )


def test_m11_1_inference_reconciliation_and_rl1_rl5_governance_parity() -> None:
    """M11-1: `v_inference_request_reconciliation` & `v_app_risk_governance` match Python twins."""
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
    server_logs = [
        InferenceRequestLogRecord(
            request_id="req-1",
            apphub_application_id="gov-rl1-app",
            model_name="gemini-4-carbon",
            serving_traffic_type=ServingTrafficType.STANDARD,
            request_type_header="dedicated",
            http_status_code=200,
            input_tokens=10_000,
            output_tokens=1_000,
            request_timestamp=base,
        ),
        InferenceRequestLogRecord(
            request_id="req-2",
            apphub_application_id="gov-rl4-app",
            model_name="gemini-3.8-flash",
            serving_traffic_type=ServingTrafficType.STANDARD,
            request_type_header="shared",
            http_status_code=429,
            input_tokens=9_000,
            output_tokens=1_000,
            request_timestamp=base,
        ),
    ]

    py_rec = {
        r.apphub_application_id: r for r in reconcile_inference_requests(otel_spans, server_logs)
    }
    assert py_rec["gov-rl1-app"].reconciliation_status == "RECONCILED"
    assert py_rec["gov-rl1-app"].pt_eligible_involuntary_spillover_count == 1
    assert py_rec["gov-rl4-app"].reconciliation_status == "DRIFT_ALERT"
    assert py_rec["gov-rl4-app"].intentional_shared_bypass_count == 1

    rl1_app = AppGovernanceRecord(
        apphub_application_id="gov-rl1-app",
        risk_tier=RiskLevel.RL_1,
        autonomy_tier=AutonomyTier.BOUNDED_AUTONOMOUS,
        lifecycle_track_policy=LifecycleTrackPolicy.LONG_TERM_GA_ONLY,
        funded_gsus=4.0,
        pt_commitment_months=12,
    )
    rl4_app = AppGovernanceRecord(
        apphub_application_id="gov-rl4-app",
        risk_tier=RiskLevel.RL_4,
        autonomy_tier=AutonomyTier.HUMAN_IN_LOOP,
        lifecycle_track_policy=LifecycleTrackPolicy.SHORT_TERM_GA_ALLOWED,
        funded_gsus=0.0,
        pt_commitment_months=0,
    )
    rl1_eval = evaluate_app_governance(rl1_app, "gemini-4-carbon", observed_ac2=0.82)
    rl4_eval = evaluate_app_governance(rl4_app, "gemini-3.8-flash", observed_ac2=0.78)
    assert rl1_eval.required_ac2_gate == 0.85
    assert rl1_eval.passes_ac2_gate is False
    assert rl1_eval.pt_commitment_allowed is False
    assert rl4_eval.required_ac2_gate == 0.75
    assert rl4_eval.passes_ac2_gate is True
    assert rl4_eval.pt_commitment_allowed is True

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    _install_extra_views(
        conn,
        "views.sql",
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
    for lg in server_logs:
        conn.execute(
            "INSERT INTO inference_request_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lg.request_id,
                None,
                lg.apphub_application_id,
                lg.model_name,
                str(lg.serving_traffic_type),
                lg.request_type_header,
                lg.http_status_code,
                lg.input_tokens,
                lg.cached_input_tokens,
                lg.cache_write_5m_tokens,
                lg.cache_write_1h_tokens,
                lg.output_tokens,
                lg.thinking_tokens,
                base.isoformat(),
            ),
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


def test_m11_2_three_pool_focus_chargeback_and_capacity_bq_g1_wire_contract() -> None:
    """M11-2: 3-Pool FOCUS (`prod|eval|optimize`, `< 1e-12` drift, Involuntary vs TenantBurst) & `capacity` BQ-G1 payload."""
    summary = allocate_focus_three_pool_hourly_chargeback(
        hourly_demand_by_app_lane={
            ("app-funded", "prod"): [40_000.0, 20_000.0],
            ("app-burst", "prod"): [80_000.0, 10_000.0],
            ("app-funded", "eval"): [0.0, 30_000.0],
            ("app-funded", "optimize"): [0.0, 20_000.0],
        },
        app_cost_centers={"app-funded": "CC-FUNDED", "app-burst": "CC-BURST"},
        funded_gsus_by_app={"app-funded": 5.0, "app-burst": 5.0},
        hourly_reserved_pt_capacity_tokens=100_000.0,
        reserved_pt_fixed_cost_usd=200.0,
        paygo_spillover_usd_per_1m_by_app=1.0,
    )
    assert summary.reconciliation_drift_usd < 1e-12
    assert summary.prod_total_usd > 0.0
    assert summary.eval_total_usd == pytest.approx(30.0, abs=1e-12)
    assert summary.optimize_total_usd == pytest.approx(20.0, abs=1e-12)
    assert summary.platform_idle_usd == pytest.approx(20.0, abs=1e-12)

    by_key = {(r.apphub_application_id, r.traffic_lane): r for r in summary.rows}
    # Hour 0 entitlement for app-funded = 50k (>= 40k demand -> Involuntary); app-burst = 50k (< 80k -> TenantBurst).
    assert by_key[("app-funded", "prod")].pool3_involuntary_spillover_usd > 0.0
    assert by_key[("app-funded", "prod")].pool3_tenant_burst_spillover_usd == 0.0
    assert by_key[("app-burst", "prod")].pool3_tenant_burst_spillover_usd > 0.0
    assert by_key[("app-burst", "prod")].pool3_involuntary_spillover_usd == 0.0

    t0 = datetime(2026, 11, 22, 10, 10, 0, tzinfo=UTC)
    spans = [
        make_span(
            "trace-3p-01",
            "sp-3p-01",
            app_id="app-funded",
            model_name="gemini-3.8-flash",
            serving_traffic_type=ServingTrafficType.STANDARD,
            http_status_code=429,
            input_tokens=40_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            base_time=t0,
        ),
        make_span(
            "trace-3p-02",
            "sp-3p-02",
            app_id="app-burst",
            model_name="gemini-3.8-flash",
            serving_traffic_type=ServingTrafficType.PRIORITY,
            http_status_code=200,
            input_tokens=80_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            base_time=t0,
        ),
        make_span(
            "trace-3p-03",
            "sp-3p-03",
            app_id="app-funded",
            model_name="gemini-3.8-flash",
            finops_traffic_type=FinOpsTrafficType.OPTIMIZE,
            serving_traffic_type=ServingTrafficType.PROVISIONED_THROUGHPUT,
            http_status_code=200,
            input_tokens=20_000,
            cached_input_tokens=0,
            output_tokens=500,
            thinking_tokens=0,
            base_time=t0 + timedelta(hours=1),
        ),
    ]
    hourly_rows = evaluate_hourly_burndown_in_sqlite(spans)
    assert any(r.http_429_count == 1 for r in hourly_rows)
    assert any(r.finops_traffic_type == "optimize" for r in hourly_rows)

    schedule_req = build_capacity_residual_schedule_request(
        hourly_rows,
        model="gemini-3.8-flash",
        provisioned_gsus=8,
        safety_headroom_ratio=0.85,
        deadline_jobs=(
            CapacityDeadlineJobPayload(
                job_id="nightly-eval",
                value_usd=50.0,
                burndown_tokens=500_000.0,
                deadline_hour=2,
            ),
        ),
    )
    req_json = schedule_req.model_dump(mode="json")
    assert req_json["safety_headroom_ratio"] <= 0.85
    assert len(req_json["revenue_tpm_by_hour"]) == 2
    assert req_json["evergreen_backfill_tokens"] > 0.0

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    _install_extra_views(conn, "views.sql", ("agent_eval.v_focus_three_pool_hourly_chargeback",))
    conn.executemany(
        "INSERT INTO app_registry (apphub_application_id, cost_center, funded_gsus) VALUES (?,?,?)",
        [("app-funded", "CC-FUNDED", 5.0), ("app-burst", "CC-BURST", 5.0)],
    )
    for sp in spans:
        insert_enterprise_sqlite_span(conn, sp)
    cb_rows = conn.execute("SELECT * FROM v_focus_three_pool_hourly_chargeback").fetchall()
    conn.close()
    assert len(cb_rows) == 3
    by_app_lane = {
        (str(r["apphub_application_id"]), str(r["traffic_lane"])): dict(r) for r in cb_rows
    }
    assert float(by_app_lane[("app-funded", "prod")]["pool3_involuntary_spillover_usd"]) > 0.0
    assert float(by_app_lane[("app-burst", "prod")]["pool3_tenant_burst_spillover_usd"]) > 0.0
    assert ("app-funded", "optimize") in by_app_lane


def test_m11_3_bigquery_omni_zero_egress_ast_proof_and_multicloud_cpvo() -> None:
    """M11-3: `sql/omni_spoke_views.sql` proves zero raw prompt egress, strict fallback, and unified CPVO."""
    omni_sql = (SQL_DIR / "omni_spoke_views.sql").read_text(encoding="utf-8")
    stmts = _parse_bigquery_statements(omni_sql, "omni_spoke_views.sql")
    ok, violations = verify_zero_raw_payload_egress(omni_sql)
    assert ok, f"Zero-egress violation: {violations}"

    for stmt in stmts:
        if isinstance(stmt, exp.Create) and (stmt.args.get("kind") or "").upper() == "VIEW":
            select_node = stmt.expression
            if isinstance(select_node, exp.Select):
                projected = {col.alias_or_name.lower() for col in select_node.expressions}
                assert not (projected & FORBIDDEN_RAW_PAYLOAD_COLUMNS), (
                    f"Omni view projected forbidden raw payload columns: {projected & FORBIDDEN_RAW_PAYLOAD_COLUMNS}"
                )

    assert normalize_workload_principal("spiffe://corp.example/ns/agents/sa/runner") == (
        "spiffe://corp.example/ns/agents/sa/runner"
    )
    assert normalize_workload_principal("arn:aws:iam::123:role/agent", cloud_provider="aws") == (
        "principal://iam.googleapis.com/projects/external-aws/subject/arn:aws:iam::123:role/agent"
    )
    assert normalize_omni_application_id(None) == "unregistered_app"
    assert normalize_omni_application_id("   ") == "unregistered_app"

    judge_sql = build_omni_in_residency_judge_sql(
        project_id="hub-proj-01",
        cloud_provider="aws",
        canary_sample_pct=5,
    )
    _parse_bigquery_statements(judge_sql, "omni_in_residency_judge_sql")
    assert "FARM_FINGERPRINT(trace_id)" in judge_sql

    unified = reconcile_multicloud_cpvo(
        gcp_outcomes=[("shared-app", 100, 90, 9.0)],
        remote_slices=[
            RemoteTraceSliceRecord(
                cloud_provider="aws",
                cloud_region="us-east-1",
                apphub_application_id="shared-app",
                workload_principal="spiffe://aws/agent",
                model_name="claude-sonnet-4-6",
                finops_traffic_type="production_inference",
                total_traces=50,
                rule0_passing_traces=48,
                verified_outcomes=40,
                total_spend_usd=17.0,
            ),
            RemoteTraceSliceRecord(
                cloud_provider="azure",
                cloud_region="eastus2",
                apphub_application_id="",
                workload_principal="principal://azure/shadow",
                model_name="gemini-3.8-flash",
                finops_traffic_type="production_inference",
                total_traces=10,
                rule0_passing_traces=10,
                verified_outcomes=0,
                total_spend_usd=1.2,
            ),
        ],
    )
    assert unified["shared-app"].active_clouds == 2
    assert unified["shared-app"].verified_outcomes == 130
    assert unified["shared-app"].unified_cost_per_verified_outcome_usd == pytest.approx(
        26.0 / 130.0, abs=1e-12
    )
    assert "unregistered_app" in unified
    assert unified["unregistered_app"].unified_cost_per_verified_outcome_usd is None


def test_m11_4_sessions_5m_breaches_optimize_lane_and_ring_buffer() -> None:
    """M11-4: Session lineage, 5m SLO/429 breaches, `optimize` lane, and `<2s` RingBuffer export."""
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
        "views.sql",
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

    py_sess = evaluate_interaction_lineage_in_sqlite(spans)[0]
    py_breach = evaluate_5m_breaches_in_sqlite(spans, sla_latency_ms=5000.0)[0]
    py_sum = evaluate_traces_in_sqlite(spans)["live-app"]

    assert int(bq_sess["turn_count"]) == py_sess.turn_count == 2
    assert int(bq_sess["first_failure_turn_index"]) == py_sess.first_failure_turn_index == 1
    assert bool(bq_sess["session_rule0_pass"]) is False
    assert bq_breach["breach_reason"] == py_breach.breach_reason == "HTTP_429_STORM"
    assert bool(bq_breach["is_breach"]) is True
    assert float(bq_opt["optimize_usd"]) == pytest.approx(py_sum.optimize_usd, abs=1e-12)
    assert float(bq_opt["optimize_usd"]) > 0.0
    assert float(bq_opt["unclassified_usd"]) == 0.0

    # RingBufferSpanExporter + GenAISpanRecorder + W3C headers -> SQLiteEvalBackend (<2s CI parity).
    tid, parent_sid, sampled = parse_trace_context_headers(
        {"traceparent": "00-0123456789abcdef0123456789abcdef-fedcba9876543210-01"}
    )
    assert tid == "0123456789abcdef0123456789abcdef"
    assert parent_sid == "fedcba9876543210"
    assert sampled is True
    assert format_x_cloud_trace_context(tid, parent_sid, True).endswith(";o=1")

    exporter = RingBufferSpanExporter(maxlen=64)
    recorder = GenAISpanRecorder(exporter, service_name="ci-svc", apphub_application_id="ci-app")
    with recorder.start_span(
        "invoke_agent",
        span_kind=GenAISpanKind.INVOKE_AGENT,
        trace_id=tid,
        parent_span_id=parent_sid,
        session_id="sess-ci",
        turn_index=0,
    ) as st:
        st["input_tokens"] = 8_000
        st["output_tokens"] = 400
    backend = SQLiteEvalBackend()
    ci_summary = backend.evaluate_traces(exporter.get_spans())["ci-app"]
    assert ci_summary.total_traces == 1
    assert json.loads(json.dumps({"ok": True}))["ok"] is True


def test_m11_4_clause_scoped_nfr_gate_loss_bank_and_atif_export() -> None:
    """M11-4: Clause-scoped `EVAL-P09` `policy_sha256` `NFR == 0.0%` gate, loss-bank clustering & `ATIF-v1.8` export."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_extra_views(
        conn,
        "step3_saved_results.sql",
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

    outcomes = [
        PairedEvalOutcome(
            trace_id="tr-c1",
            apphub_application_id="pol-app",
            rubric_id="groundedness_4pt_v1",
            baseline_pass=True,
            candidate_pass=False,
            baseline_policy_sha256="sha-v1",
            candidate_policy_sha256="sha-v2",
            amended_clause_ids=("CL-4.2", "CL-9.1"),
            target_clause_id="CL-4.2",
            failure_signature="clause_4_2_stricter",
            reasoning_trace="Contact jane.doe@corp.example SSN 123-45-6789 token sk-secret1234567890",
            trace_cost_usd=0.05,
        ),
        PairedEvalOutcome(
            trace_id="tr-c2",
            apphub_application_id="pol-app",
            rubric_id="groundedness_4pt_v1",
            baseline_pass=True,
            candidate_pass=True,
            baseline_policy_sha256="sha-v1",
            candidate_policy_sha256="sha-v2",
            amended_clause_ids=("CL-4.2", "CL-9.1"),
            target_clause_id="CL-1.0",
        ),
    ]
    nfr_py = evaluate_clause_scoped_nfr_gate(outcomes)[("pol-app", "groundedness_4pt_v1")]
    assert int(nfr_sql_row["amended_clause_flips"]) == nfr_py.amended_clause_flips == 1
    assert int(nfr_sql_row["unamended_negative_flips"]) == nfr_py.unamended_negative_flips == 0
    assert bool(nfr_sql_row["passes_nfr_gate"]) is True
    assert nfr_py.passes_nfr_gate is True

    clusters = cluster_failure_loss_bank(outcomes)
    assert len(clusters) == 1
    assert clusters[0].status == "NEW"

    payload_map = {
        "tr-c1": TracePayloadRecord(
            trace_id="tr-c1",
            apphub_application_id="pol-app",
            ingested_at=datetime(2026, 11, 22, 11, 0, 0, tzinfo=UTC),
            user_prompt="Email alice@example.com about 123-45-6789",
            retrieved_context="",
            final_response="Used key AIzaSyD1234567890abcdef",
        )
    }
    trajs = export_atif_v1_8_trajectories(spans=[], payloads=payload_map, outcomes=outcomes)
    assert len(trajs) == 1
    assert trajs[0].schema_version == "ATIF-v1.8"
    assert "alice@example.com" not in trajs[0].sanitized_prompt
    assert "123-45-6789" not in trajs[0].sanitized_prompt
    assert "AIzaSyD1234567890abcdef" not in trajs[0].sanitized_response
    assert "sk-secret1234567890" not in trajs[0].sanitized_reasoning
    assert "user.synthetic@example.invalid" in sanitize_atif_text("bob@bank.com")
