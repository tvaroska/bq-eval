"""3P/PayGo, 3-Pool FOCUS & `capacity` handoff, unified Omni CPVO, and exact clause/turn gates."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import (
    FinOpsTrafficType,
    OTelGenAISpanRecord,
    ServingTrafficType,
    normalize_serving_traffic_type,
)
from bq_eval.core.contract.pricing import DEFAULT_MODEL_SKUS, normalize_model_name
from bq_eval.core.engine.cost import compute_span_burndown_tokens, compute_span_cost_usd
from bq_eval.finops.contract.chargeback import CapacityDeadlineJobPayload
from bq_eval.finops.engine.chargeback_three_pool import (
    allocate_focus_three_pool_hourly_chargeback,
    build_capacity_residual_schedule_request,
)
from bq_eval.finops.engine.sqlite_capacity import evaluate_hourly_burndown_in_sqlite
from tests.conftest import make_span
from tests.sql_helpers import (
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
)


def _install_extra_views(conn: sqlite3.Connection, file_name: str, views: tuple[str, ...]) -> None:
    path = SQL_DIR / file_name
    for full_name in views:
        short = full_name.split(".", 1)[1]
        sql = _transpile_bq_view_to_sqlite(_extract_view_sql_text(path, full_name))
        conn.execute(f"CREATE VIEW {short} AS {sql}")


def test_3p_claude_cache_write_paygo_multipliers_sql_sqlite_python_parity() -> None:
    """3P Claude cache-write (`1.25x` 5m / `2.0x` 1h), `5.0x` output & PayGo (`1.8x` / `0.5x`) match `< 1e-12`."""
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


def test_three_pool_focus_chargeback_and_capacity_bq_g1_wire_contract() -> None:
    """3-Pool FOCUS (`prod|eval|optimize`, `< 1e-12` drift, Involuntary vs TenantBurst) & `capacity` payload."""
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
    _install_extra_views(
        conn, "finops/capacity_views.sql", ("agent_eval.v_focus_three_pool_hourly_chargeback",)
    )
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


def test_three_pool_multi_lane() -> None:
    """Three-pool SQL & Python classify spillover at the app level across lanes and include idle funded apps (S-2, T-5)."""
    t0 = datetime(2026, 11, 22, 14, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    # Hour 0 (t0):
    #   app-funded (funded=5): prod 300K + eval 100K = 400K total (demand_share=400/2000=0.20 <= 5/20=0.25 -> Involuntary on BOTH lanes)
    #   app-burst  (funded=5): prod 1600K (demand_share=1600/2000=0.80 > 0.25 -> TenantBurst)
    #   app-idle   (funded=10): 0 traffic in t0 (must STILL stay in pool_total_funded_gsus=20.0!)
    # Hour 1 (t1):
    #   app-funded (funded=5): prod 300K + eval 300K = 600K total (demand_share=600/2000=0.30 > 0.25 -> TenantBurst on BOTH lanes,
    #     even though prod alone is 300/2000=0.15 <= 0.25!)
    #   app-burst  (funded=5): prod 1400K (demand_share=1400/2000=0.70 > 0.25 -> TenantBurst)
    spans = [
        # Hour 0
        make_span(
            "tr-ml-h0-fp",
            "sp-ml-h0-fp",
            app_id="app-funded",
            model_name="gemini-3.8-flash",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            serving_traffic_type=ServingTrafficType.STANDARD,
            input_tokens=150_000,
            cached_input_tokens=0,
            output_tokens=37_500,  # burndown = 150K + 4*37.5K = 300K
            thinking_tokens=0,
            base_time=t0,
        ),
        make_span(
            "tr-ml-h0-fe",
            "sp-ml-h0-fe",
            app_id="app-funded",
            model_name="gemini-3.8-flash",
            finops_traffic_type=FinOpsTrafficType.AGENT_GYM_EVAL,
            serving_traffic_type=ServingTrafficType.STANDARD,
            input_tokens=60_000,
            cached_input_tokens=0,
            output_tokens=10_000,  # burndown = 60K + 4*10K = 100K
            thinking_tokens=0,
            base_time=t0,
        ),
        make_span(
            "tr-ml-h0-bp",
            "sp-ml-h0-bp",
            app_id="app-burst",
            model_name="gemini-3.8-flash",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            serving_traffic_type=ServingTrafficType.STANDARD,
            input_tokens=160_000,
            cached_input_tokens=0,
            output_tokens=360_000,  # burndown = 160K + 4*360K = 1,600K
            thinking_tokens=0,
            base_time=t0,
        ),
        # Hour 1
        make_span(
            "tr-ml-h1-fp",
            "sp-ml-h1-fp",
            app_id="app-funded",
            model_name="gemini-3.8-flash",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            serving_traffic_type=ServingTrafficType.STANDARD,
            input_tokens=150_000,
            cached_input_tokens=0,
            output_tokens=37_500,  # 300K
            thinking_tokens=0,
            base_time=t1,
        ),
        make_span(
            "tr-ml-h1-fe",
            "sp-ml-h1-fe",
            app_id="app-funded",
            model_name="gemini-3.8-flash",
            finops_traffic_type=FinOpsTrafficType.AGENT_GYM_EVAL,
            serving_traffic_type=ServingTrafficType.STANDARD,
            input_tokens=150_000,
            cached_input_tokens=0,
            output_tokens=37_500,  # 300K -> app-funded combined = 600K (30% > 25%)
            thinking_tokens=0,
            base_time=t1,
        ),
        make_span(
            "tr-ml-h1-bp",
            "sp-ml-h1-bp",
            app_id="app-burst",
            model_name="gemini-3.8-flash",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            serving_traffic_type=ServingTrafficType.STANDARD,
            input_tokens=160_000,
            cached_input_tokens=0,
            output_tokens=310_000,  # 1,400K
            thinking_tokens=0,
            base_time=t1,
        ),
    ]

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    _install_extra_views(
        conn, "finops/capacity_views.sql", ("agent_eval.v_focus_three_pool_hourly_chargeback",)
    )
    conn.executemany(
        "INSERT INTO app_registry (apphub_application_id, cost_center, funded_gsus) VALUES (?,?,?)",
        [
            ("app-funded", "CC-F", 5.0),
            ("app-burst", "CC-B", 5.0),
            ("app-idle", "CC-I", 10.0),
        ],
    )
    for sp in spans:
        insert_enterprise_sqlite_span(conn, sp)
    sql_rows = {
        (str(r["usage_hour"]), str(r["apphub_application_id"]), str(r["traffic_lane"])): dict(r)
        for r in conn.execute("SELECT * FROM v_focus_three_pool_hourly_chargeback").fetchall()
    }
    conn.close()

    h0_key = "2026-11-22T14:00:00+00:00"
    h1_key = "2026-11-22T15:00:00+00:00"
    # Pool total funded GSUs = 5 + 5 + 10 = 20 -> funded_gsu_share = 0.25 (not 5/15=0.333 or 5/10=0.5)
    for lane in ("prod", "eval"):
        r0 = sql_rows[(h0_key, "app-funded", lane)]
        assert float(r0["funded_gsu_share"]) == pytest.approx(0.25, abs=1e-12)
        assert float(r0["demand_share"]) == pytest.approx(0.20, abs=1e-12)
        assert float(r0["pool3_involuntary_spillover_usd"]) > 0.0
        assert float(r0["pool3_tenant_burst_spillover_usd"]) == pytest.approx(0.0, abs=1e-12)

        r1 = sql_rows[(h1_key, "app-funded", lane)]
        assert float(r1["funded_gsu_share"]) == pytest.approx(0.25, abs=1e-12)
        assert float(r1["demand_share"]) == pytest.approx(0.30, abs=1e-12)
        assert float(r1["pool3_involuntary_spillover_usd"]) == pytest.approx(0.0, abs=1e-12)
        assert float(r1["pool3_tenant_burst_spillover_usd"]) > 0.0

    assert float(sql_rows[(h0_key, "app-burst", "prod")]["pool3_tenant_burst_spillover_usd"]) > 0.0
    assert float(
        sql_rows[(h0_key, "app-burst", "prod")]["pool3_involuntary_spillover_usd"]
    ) == pytest.approx(0.0, abs=1e-12)

    # Python allocator: exact app-level entitlement parity (including idle app, exact boundary,
    # zero-funded app, all-zero funding, and wf_overflow > 0).
    # Capacity = 1,000,000 tokens/hr; funded = {app-funded: 5, app-burst: 5, app-idle: 10} -> entitlement = 250,000 each.
    py_multi = allocate_focus_three_pool_hourly_chargeback(
        hourly_demand_by_app_lane={
            # Hour 0: app-funded total = 150K + 100K = 250K (exact boundary -> Involuntary on BOTH prod and eval)
            # Hour 1: app-funded total = 150K + 150K = 300K (> 250K -> TenantBurst on BOTH prod and eval)
            ("app-funded", "prod"): [150_000.0, 150_000.0],
            ("app-funded", "eval"): [100_000.0, 150_000.0],
            ("app-burst", "prod"): [1_200_000.0, 1_200_000.0],
            ("app-unfunded", "prod"): [100_000.0, 0.0],
        },
        app_cost_centers={"app-funded": "CC-F", "app-burst": "CC-B", "app-unfunded": "CC-U"},
        funded_gsus_by_app={
            "app-funded": 5.0,
            "app-burst": 5.0,
            "app-idle": 10.0,
            "app-unfunded": 0.0,
        },
        hourly_reserved_pt_capacity_tokens=1_000_000.0,
        reserved_pt_fixed_cost_usd=200.0,
        paygo_spillover_usd_per_1m_by_app=1.0,
    )
    assert py_multi.reconciliation_drift_usd < 1e-12
    by_k = {(r.apphub_application_id, r.traffic_lane): r for r in py_multi.rows}
    # Hour 0 app-funded eval overflows (wf_overflow > 0) and is within entitlement -> involuntary > 0!
    assert by_k[("app-funded", "prod")].pool3_involuntary_spillover_usd > 0.0
    assert by_k[("app-funded", "prod")].pool3_tenant_burst_spillover_usd > 0.0
    assert by_k[("app-funded", "eval")].pool3_involuntary_spillover_usd > 0.0
    assert by_k[("app-funded", "eval")].pool3_tenant_burst_spillover_usd > 0.0
    # Unfunded app always bills to tenant burst, never involuntary.
    assert by_k[("app-unfunded", "prod")].pool3_involuntary_spillover_usd == pytest.approx(0.0)
    assert by_k[("app-unfunded", "prod")].pool3_tenant_burst_spillover_usd > 0.0

    # Zero total funded GSUs -> all overflow is tenant burst (no division by zero).
    py_zero_funded = allocate_focus_three_pool_hourly_chargeback(
        hourly_demand_by_app_lane={("app-a", "prod"): [1_500_000.0]},
        app_cost_centers={"app-a": "CC-A"},
        funded_gsus_by_app={"app-a": 0.0},
        hourly_reserved_pt_capacity_tokens=1_000_000.0,
        reserved_pt_fixed_cost_usd=100.0,
        paygo_spillover_usd_per_1m_by_app=1.0,
    )
    assert py_zero_funded.rows[0].pool3_involuntary_spillover_usd == pytest.approx(0.0)
    assert py_zero_funded.rows[0].pool3_tenant_burst_spillover_usd == pytest.approx(0.5, abs=1e-12)


def test_unified_cpvo_once() -> None:
    """Unified multi-cloud CPVO counts multi-agent GCP and multi-model remote outcomes once (S-4)."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    spoke_cols = (
        "trace_id TEXT, span_id TEXT, parent_span_id TEXT, span_name TEXT, "
        "start_time TEXT, end_time TEXT, latency_ms REAL, status_code TEXT, span_kind TEXT, "
        "service_name TEXT, gcp_apphub_application_id TEXT, workload_identity TEXT, "
        "model_name TEXT, input_tokens INTEGER, cached_input_tokens INTEGER, "
        "cache_write_5m_tokens INTEGER, cache_write_1h_tokens INTEGER, output_tokens INTEGER, "
        "thinking_tokens INTEGER, rule0_pass INTEGER, finops_traffic_type TEXT, "
        "serving_traffic_type TEXT, http_status_code INTEGER, outcome_id TEXT, "
        "outcome_verified INTEGER, cloud_region TEXT, span_cost_usd REAL"
    )
    conn.execute(f"CREATE TABLE aws_omni_otel_spans ({spoke_cols})")
    conn.execute(f"CREATE TABLE azure_omni_otel_spans ({spoke_cols})")
    conn.execute(
        "CREATE TABLE remote_trace_slices ("
        "cloud_provider TEXT, cloud_region TEXT, usage_hour TEXT, apphub_application_id TEXT, "
        "workload_principal TEXT, model_name TEXT, finops_traffic_type TEXT, "
        "total_traces INTEGER, rule0_passing_traces INTEGER, verified_outcomes INTEGER, "
        "total_input_tokens INTEGER, total_output_tokens INTEGER, "
        "weighted_burndown_tokens REAL, total_spend_usd REAL)"
    )
    _install_extra_views(
        conn,
        "finops/omni_spoke_views.sql",
        (
            "agent_eval.v_omni_aws_spoke_spans",
            "agent_eval.v_omni_azure_spoke_spans",
            "agent_eval.v_omni_remote_trace_slice_rollup",
            "agent_eval.v_unified_multicloud_cpvo",
        ),
    )

    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    # 1 GCP outcome spanning 2 agents (agent-a + agent-b) in 1 trace -> MUST count as 1 outcome, 1 trace!
    for idx, agent in enumerate(("agent-a", "agent-b")):
        insert_enterprise_sqlite_span(
            conn,
            make_span(
                "trace-s4-gcp",
                f"sp-s4-gcp-{idx}",
                parent_span_id=None if idx == 0 else "sp-s4-gcp-0",
                app_id="uni-app",
                agent_name=agent,
                outcome_id="out-s4-gcp",
                outcome_owner="uni-app",
                input_tokens=100_000,
                cached_input_tokens=0,
                output_tokens=10_000,
                thinking_tokens=0,
                latency_ms=100.0,
                offset_s=idx,
                base_time=base,
            ),
        )
    insert_enterprise_sqlite_span(
        conn,
        make_span(
            "trace-s4-gcp",
            "sp-s4-close",
            parent_span_id="sp-s4-gcp-0",
            span_name="outcome.closed",
            app_id="uni-app",
            agent_name="agent-b",
            outcome_id="out-s4-gcp",
            outcome_owner="uni-app",
            model_name="none",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=0.0,
            offset_s=2,
            attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
            base_time=base,
        ),
    )
    # 1 AWS remote outcome spanning 2 models (claude + gemini) in 1 trace -> MUST count as 1 outcome, 1 trace!
    for idx, (model, cost) in enumerate([("claude-sonnet-4-6", 0.04), ("gemini-3.8-flash", 0.01)]):
        conn.execute(
            "INSERT INTO aws_omni_otel_spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "tr-s4-aws",
                f"sp-s4-aws-{idx}",
                None,
                "chat",
                f"2026-11-22T12:00:0{idx}+00:00",
                f"2026-11-22T12:00:0{idx + 1}+00:00",
                500.0,
                "OK",
                "chat",
                "svc-aws",
                "uni-app",
                "arn:aws:iam::123:role/agent",
                model,
                10_000,
                0,
                0,
                0,
                500,
                0,
                1,
                "production_inference",
                "STANDARD",
                200,
                "out-s4-aws",
                1,
                "aws-us-east-1",
                cost,
            ),
        )
    conn.execute("INSERT INTO remote_trace_slices SELECT * FROM v_omni_remote_trace_slice_rollup")
    row = conn.execute(
        "SELECT active_clouds, total_traces, verified_outcomes, total_spend_usd, "
        "unified_cost_per_verified_outcome_usd FROM v_unified_multicloud_cpvo "
        "WHERE apphub_application_id = 'uni-app'"
    ).fetchone()
    conn.close()

    assert int(row["active_clouds"]) == 2
    assert int(row["total_traces"]) == 2
    assert int(row["verified_outcomes"]) == 2
    assert float(row["unified_cost_per_verified_outcome_usd"]) == pytest.approx(
        float(row["total_spend_usd"]) / 2.0, abs=1e-12
    )


def test_clause_gate_exact() -> None:
    """Clause gate matches exact IDs (no `_` wildcard) and session turns rank by trace, not span (S-11)."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    _install_extra_views(
        conn,
        "core/step3_saved_results.sql",
        ("agent_eval.v_step3_paired_run_comparison", "agent_eval.v_step3_policy_nfr_gate"),
    )
    _install_extra_views(
        conn, "finops/capacity_views.sql", ("agent_eval.v_interaction_session_lineage",)
    )
    # Target c_1 MUST NOT match amended cX1 (LIKE '_' wildcard bug)
    conn.executemany(
        "INSERT INTO eval_results ("
        "eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id, "
        "judge_model, sampling_stratum, rule0_pass, reasoning_trace, ordinal_score, binary_pass, "
        "judge_input_tokens, judge_output_tokens, judge_thinking_tokens, "
        "policy_sha256, amended_clause_ids, target_clause_id, evaluated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "e-s11-b",
                "run-1",
                "tr-s11-1",
                None,
                "s11-app",
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
                "sha-1",
                None,
                "c_1",
                "2026-11-22T10:00:00+00:00",
            ),
            (
                "e-s11-c",
                "run-2",
                "tr-s11-1",
                None,
                "s11-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "FULL_100PCT",
                1,
                "regressed",
                1,
                0,
                100,
                20,
                0,
                "sha-2",
                "cX1,c_10",
                "c_1",
                "2026-11-22T11:00:00+00:00",
            ),
        ],
    )
    nfr = conn.execute(
        "SELECT unamended_negative_flips, passes_nfr_gate FROM v_step3_policy_nfr_gate "
        "WHERE apphub_application_id = 's11-app'"
    ).fetchone()

    # 2 traces x 3 spans each with turn_index=None -> MUST count turn_count = 2 (not 6)
    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    session_spans: list[OTelGenAISpanRecord] = []
    for t_idx in range(2):
        for s_idx in range(3):
            sp = make_span(
                f"trace-turn-{t_idx}",
                f"sp-turn-{t_idx}-{s_idx}",
                parent_span_id=None if s_idx == 0 else f"sp-turn-{t_idx}-0",
                app_id="s11-app",
                session_id="sess-multi-span",
                turn_index=None,
                offset_s=t_idx * 10 + s_idx,
                base_time=base,
            )
            session_spans.append(sp)
            insert_enterprise_sqlite_span(conn, sp)

    bq_sess = conn.execute(
        "SELECT turn_count, max_turn_index, trace_count FROM v_interaction_session_lineage "
        "WHERE session_id = 'sess-multi-span'"
    ).fetchone()
    conn.close()

    assert int(nfr["unamended_negative_flips"]) == 1
    assert int(nfr["passes_nfr_gate"]) == 0
    assert int(bq_sess["turn_count"]) == 2
    assert int(bq_sess["max_turn_index"]) == 1
    assert int(bq_sess["trace_count"]) == 2


def test_residual_schedule_preserves_zero_traffic_hours_and_capacity_export_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero-traffic hours stay in the hourly grid as `0.0` and `bq-eval capacity-export` runs end-to-end."""
    t0 = datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-gap-h0",
            "sp-gap-h0",
            app_id="gap-app",
            model_name="gemini-3.8-flash",
            input_tokens=60_000,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            base_time=t0,
        ),
        make_span(
            "tr-gap-h3",
            "sp-gap-h3",
            app_id="gap-app",
            model_name="gemini-3.8-flash",
            input_tokens=120_000,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            base_time=t0 + timedelta(hours=3),
        ),
    ]
    hourly_rows = evaluate_hourly_burndown_in_sqlite(spans)
    req = build_capacity_residual_schedule_request(
        hourly_rows,
        model="gemini-3.8-flash",
        provisioned_gsus=4,
    )
    # 10:00Z, 11:00Z (gap), 12:00Z (gap), 13:00Z -> 4 contiguous hours with 0.0 in the valley.
    assert req.revenue_tpm_by_hour == (1000.0, 0.0, 0.0, 2000.0)

    rc = cli_main(["capacity-export", "--corpus", "m2-live", "--provisioned-gsus", "4"])
    assert rc == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["corpus_id"] == "m2-live"
    assert exported["three_pool_chargeback"]["reconciliation_drift_usd"] < 1e-12
    assert len(exported["residual_schedule_request"]["revenue_tpm_by_hour"]) >= 1
