"""3P/PayGo, 3-Pool FOCUS 1.2, `capacity` v1.1 handoff, session lineage, nudges & clause gates on Silver+Gold."""

from __future__ import annotations

import json
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
from tests.conftest import eval_hourly_burndown, make_span, open_runner_for_spans


def test_3p_claude_cache_write_paygo_multipliers_sql_python_parity() -> None:
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

    expected_claude_std = 0.195 + 0.006 + 0.0375 + 0.030 + 0.075
    expected_claude_bd = 65_000.0 + 20_000.0 * 0.1 + 10_000.0 * 1.25 + 5_000.0 * 2.0 + 5_000.0 * 5.0
    assert compute_span_cost_usd(spans[0]) == pytest.approx(expected_claude_std, abs=1e-12)
    assert compute_span_burndown_tokens(spans[0]) == pytest.approx(expected_claude_bd, abs=1e-12)
    assert compute_span_cost_usd(spans[1]) == pytest.approx(expected_claude_std * 1.8, abs=1e-12)
    assert compute_span_cost_usd(spans[2]) == pytest.approx(
        ((100_000 / 1e6) * 0.15 + (10_000 / 1e6) * 0.60) * 0.5,
        abs=1e-12,
    )

    with open_runner_for_spans(spans) as runner:
        wf_rows = {
            str(r["trace_id"]): dict(r)
            for r in runner.query(
                "SELECT trace_id, span_cost_usd, weighted_burndown_tokens "
                "FROM `agent_eval.v_trace_waterfall` WHERE model_name != 'none'"
            ).rows
        }

    for sp in spans:
        assert float(str(wf_rows[sp.trace_id]["span_cost_usd"])) == pytest.approx(
            compute_span_cost_usd(sp), abs=1e-12
        )
        assert float(str(wf_rows[sp.trace_id]["weighted_burndown_tokens"])) == pytest.approx(
            compute_span_burndown_tokens(sp), abs=1e-12
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
    assert summary.eval_total_usd == pytest.approx(30.0, abs=1e-12)
    assert summary.optimize_total_usd == pytest.approx(20.0, abs=1e-12)
    assert summary.platform_idle_usd == pytest.approx(20.0, abs=1e-12)

    by_key = {(r.apphub_application_id, r.traffic_lane): r for r in summary.rows}
    assert by_key[("app-funded", "prod")].pool3_involuntary_spillover_usd > 0.0
    assert by_key[("app-burst", "prod")].pool3_tenant_burst_spillover_usd > 0.0

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
    hourly_rows = eval_hourly_burndown(spans)
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

    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "silver_ref.capacity_pools",
            [
                {
                    "pool_id": "pool-ft",
                    "runtime_pool_id": "rt-pool-1",
                    "order_id": "ord-1",
                    "renewal_type": "FIXED_TERM",
                    "committed_units": 6.0,
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
                {
                    "pool_id": "pool-ar",
                    "runtime_pool_id": "rt-pool-1",
                    "order_id": "ord-2",
                    "renewal_type": "AUTO_RENEW",
                    "committed_units": 4.0,
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
            ],
            append=True,
        )
        runner.load_rows(
            "silver_ref.capacity_allocations",
            [
                {
                    "allocation_id": "al-1",
                    "app_id": "app-funded",
                    "runtime_pool_id": "rt-pool-1",
                    "cost_center": "CC-FUNDED",
                    "funded_units": 5.0,
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
                {
                    "allocation_id": "al-2",
                    "app_id": "app-burst",
                    "runtime_pool_id": "rt-pool-1",
                    "cost_center": "CC-BURST",
                    "funded_units": 5.0,
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
            ],
            append=True,
        )
        cb_rows = runner.query(
            "SELECT * FROM `agent_eval.v_focus_three_pool_hourly_chargeback`"
        ).rows

    assert len(cb_rows) == 3
    by_app_lane = {
        (str(r["apphub_application_id"]), str(r["traffic_lane"])): dict(r) for r in cb_rows
    }
    assert float(str(by_app_lane[("app-funded", "prod")]["pool3_involuntary_spillover_usd"])) > 0.0
    assert float(
        str(by_app_lane[("app-funded", "prod")]["fixed_term_order_share"])
    ) == pytest.approx(0.6)
    assert float(str(by_app_lane[("app-burst", "prod")]["pool3_tenant_burst_spillover_usd"])) > 0.0
    assert ("app-funded", "optimize") in by_app_lane


def test_three_pool_multi_lane() -> None:
    """Three-pool SQL & Python classify spillover at the app level across lanes and include idle funded apps (`S-2`)."""
    t0 = datetime(2026, 11, 22, 14, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    spans = [
        make_span(
            "tr-ml-h0-fp",
            "sp-ml-h0-fp",
            app_id="app-funded",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            input_tokens=150_000,
            cached_input_tokens=0,
            output_tokens=37_500,
            thinking_tokens=0,
            base_time=t0,
        ),
        make_span(
            "tr-ml-h0-fe",
            "sp-ml-h0-fe",
            app_id="app-funded",
            finops_traffic_type=FinOpsTrafficType.AGENT_GYM_EVAL,
            input_tokens=60_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            base_time=t0,
        ),
        make_span(
            "tr-ml-h0-bp",
            "sp-ml-h0-bp",
            app_id="app-burst",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            input_tokens=160_000,
            cached_input_tokens=0,
            output_tokens=360_000,
            thinking_tokens=0,
            base_time=t0,
        ),
        make_span(
            "tr-ml-h1-fp",
            "sp-ml-h1-fp",
            app_id="app-funded",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            input_tokens=150_000,
            cached_input_tokens=0,
            output_tokens=37_500,
            thinking_tokens=0,
            base_time=t1,
        ),
        make_span(
            "tr-ml-h1-fe",
            "sp-ml-h1-fe",
            app_id="app-funded",
            finops_traffic_type=FinOpsTrafficType.AGENT_GYM_EVAL,
            input_tokens=150_000,
            cached_input_tokens=0,
            output_tokens=37_500,
            thinking_tokens=0,
            base_time=t1,
        ),
        make_span(
            "tr-ml-h1-bp",
            "sp-ml-h1-bp",
            app_id="app-burst",
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            input_tokens=160_000,
            cached_input_tokens=0,
            output_tokens=310_000,
            thinking_tokens=0,
            base_time=t1,
        ),
    ]

    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "silver_ref.app_registry",
            [
                {
                    "app_id": "app-idle",
                    "apphub_application_id": "app-idle",
                    "allowed_landing_places": ["test-proj.obs_traces._AllSpans"],
                    "owner": "idle",
                    "business_unit": "ops",
                    "risk_tier": "RL-4",
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                }
            ],
            append=True,
        )
        runner.load_rows(
            "silver_ref.capacity_allocations",
            [
                {
                    "allocation_id": "al-f",
                    "app_id": "app-funded",
                    "cost_center": "CC-F",
                    "funded_units": 5.0,
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
                {
                    "allocation_id": "al-b",
                    "app_id": "app-burst",
                    "cost_center": "CC-B",
                    "funded_units": 5.0,
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
                {
                    "allocation_id": "al-i",
                    "app_id": "app-idle",
                    "cost_center": "CC-I",
                    "funded_units": 10.0,
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                },
            ],
            append=True,
        )
        sql_rows = {
            (
                str(r["usage_hour"])[:13],
                str(r["apphub_application_id"]),
                str(r["traffic_lane"]),
            ): dict(r)
            for r in runner.query(
                "SELECT * FROM `agent_eval.v_focus_three_pool_hourly_chargeback`"
            ).rows
        }

    for lane in ("prod", "eval"):
        r0 = sql_rows[("2026-11-22T14", "app-funded", lane)]
        assert float(str(r0["funded_gsu_share"])) == pytest.approx(0.25, abs=1e-12)
        assert float(str(r0["demand_share"])) == pytest.approx(0.20, abs=1e-12)
        assert float(str(r0["pool3_involuntary_spillover_usd"])) > 0.0
        assert float(str(r0["pool3_tenant_burst_spillover_usd"])) == pytest.approx(0.0, abs=1e-12)

        r1 = sql_rows[("2026-11-22T15", "app-funded", lane)]
        assert float(str(r1["funded_gsu_share"])) == pytest.approx(0.25, abs=1e-12)
        assert float(str(r1["demand_share"])) == pytest.approx(0.30, abs=1e-12)
        assert float(str(r1["pool3_involuntary_spillover_usd"])) == pytest.approx(0.0, abs=1e-12)
        assert float(str(r1["pool3_tenant_burst_spillover_usd"])) > 0.0


def test_clause_gate_exact_and_session_lineage_and_nudge_feedback() -> None:
    """Clause gate matches exact IDs (`S-11`), session turns rank by trace, and nudge feedback aggregates."""
    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    session_spans: list[OTelGenAISpanRecord] = []
    for t_idx in range(2):
        for s_idx in range(3):
            session_spans.append(
                make_span(
                    f"trace-turn-{t_idx}",
                    f"sp-turn-{t_idx}-{s_idx}",
                    parent_span_id=None if s_idx == 0 else f"sp-turn-{t_idx}-0",
                    app_id="s11-app",
                    session_id="sess-multi-span",
                    turn_index=None,
                    offset_s=t_idx * 10 + s_idx,
                    attributes={
                        "bq_eval.thought_signature_present": "true" if t_idx == 0 else "false",
                        "bq_eval.tool_definitions_sha256": f"tools-v{t_idx}",
                    },
                    base_time=base,
                )
            )

    with open_runner_for_spans(session_spans) as runner:
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "e-s11-b",
                    "eval_run_id": "run-1",
                    "trace_id": "tr-s11-1",
                    "apphub_application_id": "s11-app",
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
                    "policy_sha256": "sha-1",
                    "amended_clause_ids": None,
                    "target_clause_id": "c_1",
                    "evaluated_at": "2026-11-22T10:00:00+00:00",
                },
                {
                    "eval_id": "e-s11-c",
                    "eval_run_id": "run-2",
                    "trace_id": "tr-s11-1",
                    "apphub_application_id": "s11-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "FULL_100PCT",
                    "rule0_pass": True,
                    "ordinal_score": 1,
                    "binary_pass": False,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 100,
                    "judge_output_tokens": 20,
                    "judge_thinking_tokens": 0,
                    "policy_sha256": "sha-2",
                    "amended_clause_ids": "cX1,c_10",
                    "target_clause_id": "c_1",
                    "evaluated_at": "2026-11-22T11:00:00+00:00",
                },
            ],
            append=True,
        )
        runner.load_rows(
            "silver_internal.workflow_events_app",
            [
                {
                    "event_id": "fb-1",
                    "event_time": "2026-11-22T12:00:05+00:00",
                    "event_type": "feedback.thumbs_up",
                    "source_system": "ui",
                    "trace_id": "trace-turn-0",
                    "nudge_id": "nudge-proactive-1",
                    "payload_json": {"feedback_reason": "helpful"},
                },
                {
                    "event_id": "fb-2",
                    "event_time": "2026-11-22T12:00:15+00:00",
                    "event_type": "outcome.converted",
                    "source_system": "ui",
                    "trace_id": "trace-turn-1",
                    "nudge_id": "nudge-proactive-1",
                    "payload_json": {},
                },
            ],
            append=True,
        )
        nfr = runner.query(
            "SELECT unamended_negative_flips, passes_nfr_gate "
            "FROM `agent_eval.v_step3_policy_nfr_gate` WHERE apphub_application_id = 's11-app'"
        ).rows[0]
        bq_sess = runner.query(
            "SELECT turn_count, max_turn_index, trace_count, missing_thought_signature_turns, "
            "tool_definitions_churn_detected FROM `agent_eval.v_interaction_session_lineage` "
            "WHERE session_id = 'sess-multi-span'"
        ).rows[0]
        nudge = runner.query(
            "SELECT thumbs_up_count, converted_count, positive_feedback_rate "
            "FROM `agent_eval.v_nudge_feedback_summary` WHERE nudge_id = 'nudge-proactive-1'"
        ).rows[0]

    assert int(str(nfr["unamended_negative_flips"])) == 1
    assert bool(nfr["passes_nfr_gate"]) is False
    assert int(str(bq_sess["turn_count"])) == 2
    assert int(str(bq_sess["max_turn_index"])) == 1
    assert int(str(bq_sess["trace_count"])) == 2
    assert int(str(bq_sess["missing_thought_signature_turns"])) == 1
    assert bool(bq_sess["tool_definitions_churn_detected"]) is True
    assert int(str(nudge["thumbs_up_count"])) == 1
    assert int(str(nudge["converted_count"])) == 1
    assert float(str(nudge["positive_feedback_rate"])) == pytest.approx(1.0)


def test_residual_schedule_preserves_zero_traffic_hours_and_capacity_export_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero-traffic hours stay in the hourly grid as `0.0` and `bq-eval capacity-export` emits schema `1.1`."""
    t0 = datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-gap-h0",
            "sp-gap-h0",
            app_id="gap-app",
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
            input_tokens=120_000,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            base_time=t0 + timedelta(hours=3),
        ),
    ]
    hourly_rows = eval_hourly_burndown(spans)
    req = build_capacity_residual_schedule_request(
        hourly_rows,
        model="gemini-3.8-flash",
        provisioned_gsus=4,
    )
    assert req.revenue_tpm_by_hour == (1000.0, 0.0, 0.0, 2000.0)

    rc = cli_main(["capacity-export", "--corpus", "m2-live", "--provisioned-gsus", "4"])
    assert rc == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["schema_version"] == "1.1"
    assert exported["corpus_id"] == "m2-live"
    assert exported["three_pool_chargeback"]["reconciliation_drift_usd"] < 1e-12
    assert isinstance(exported["burst_metrics_1m"], list)
    assert len(exported["app_risk_governance"]) >= 1
    assert len(exported["lob_model_lifecycle"]) >= 1
