"""Dynamic `--scenario-file` FinOps (`A-14`, `A-19`) and parameterized WORM storage tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine import cost as finops_cost
from bq_eval.demo.contract.results import ScenarioCorpusBundle
from bq_eval.demo.golden_seeds import build_golden_seed_templates
from bq_eval.demo.pipeline import run_showcase_pipeline
from bq_eval.finops.engine import spend as finops_spend
from bq_eval.finops.engine.chargeback_three_pool import contiguous_utc_hours
from tests.conftest import REPO_ROOT, SQL_DIR, make_span, open_runner_for_spans


def _build_custom_scenario_bundle(
    *,
    corpus_id: str,
    n_flash_traces: int,
    n_error_traces: int,
    include_hallucinating_lite: bool,
    pt_capacity_tokens: float,
) -> ScenarioCorpusBundle:
    base = datetime(2026, 11, 22, 8, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    judge_outcomes: dict[str, bool] = {}
    cm_perf = ConfusionMatrix(tp=45, fn=0, tn=5, fp=0)

    for i in range(n_flash_traces):
        tid = f"{corpus_id}-flash-{i:03d}"
        hour_offset = 0 if i < n_flash_traces // 4 else (2 if i < (3 * n_flash_traces) // 4 else 3)
        start = base + timedelta(hours=hour_offset, seconds=i * 5)
        is_err = i < n_error_traces
        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-{tid}",
                start_time=start,
                end_time=start + timedelta(milliseconds=600 + i * 5),
                latency_ms=600.0 + i * 5,
                status_code="ERROR" if is_err else "OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                service_name="svc-flash",
                gcp_apphub_application_id="app-clean",
                agent_name="clean-agent",
                model_name="gemini-3.8-flash",
                input_tokens=20_000,
                cached_input_tokens=10_000,
                output_tokens=500,
                thinking_tokens=100,
                rule0_pass=not is_err,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            )
        )
        if not is_err:
            judge_outcomes[tid] = True

    if include_hallucinating_lite:
        for j in range(12):
            tid = f"{corpus_id}-lite-{j:03d}"
            start = base + timedelta(hours=2, seconds=j * 10)
            spans.append(
                OTelGenAISpanRecord(
                    trace_id=tid,
                    span_id=f"sp-{tid}",
                    start_time=start,
                    end_time=start + timedelta(milliseconds=250),
                    latency_ms=250.0,
                    status_code="OK",
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="svc-lite",
                    gcp_apphub_application_id="app-hallucinator",
                    agent_name="lite-agent",
                    model_name="gemini-3.5-flash-lite",
                    input_tokens=8_000,
                    cached_input_tokens=4_000,
                    output_tokens=200,
                    thinking_tokens=0,
                    rule0_pass=True,
                    finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
                )
            )
            judge_outcomes[tid] = j > 0

    return ScenarioCorpusBundle(
        corpus_id=corpus_id,
        title=f"Dynamic Scenario {corpus_id}",
        spans=tuple(spans),
        judge_outcomes=judge_outcomes,
        sme_confusion_by_app={"app-clean": cm_perf, "app-hallucinator": cm_perf},
        distinct_prompt_variants=2,
        dilution_target_app_id="app-clean",
        reserved_pt_capacity_tokens=pt_capacity_tokens,
        reserved_pt_fixed_cost_usd=80.0,
        app_cost_centers={"app-clean": "CC-CLEAN", "app-hallucinator": "CC-LITE"},
    )


def test_dynamic_scenario_file_computes_roi_pareto_pt_valley_and_hourly_chargeback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Changing `--scenario-file` input traces dynamically updates ROI, Pareto, PT-valley, and hourly chargeback."""
    b_low = _build_custom_scenario_bundle(
        corpus_id="scen-low",
        n_flash_traces=20,
        n_error_traces=0,
        include_hallucinating_lite=True,
        pt_capacity_tokens=400_000.0,
    )
    b_high = _build_custom_scenario_bundle(
        corpus_id="scen-high",
        n_flash_traces=40,
        n_error_traces=8,
        include_hallucinating_lite=False,
        pt_capacity_tokens=200_000.0,
    )
    p_low = tmp_path / "scen_low.json"
    p_high = tmp_path / "scen_high.json"
    p_low.write_text(json.dumps(b_low.model_dump(mode="json")), encoding="utf-8")
    p_high.write_text(json.dumps(b_high.model_dump(mode="json")), encoding="utf-8")

    assert cli_main(["demo", "--scenario-file", str(p_low), "--format", "json"]) == 0
    out_low = json.loads(capsys.readouterr().out)["artifacts"]
    assert cli_main(["demo", "--scenario-file", str(p_high), "--format", "json"]) == 0
    out_high = json.loads(capsys.readouterr().out)["artifacts"]

    assert out_low["eval_roi"]["total_production_traces"] == 32
    assert out_high["eval_roi"]["total_production_traces"] == 40
    assert out_low["eval_roi"]["tier2_error_tail_traces"] == 0
    assert out_high["eval_roi"]["tier2_error_tail_traces"] == 8
    assert out_low["eval_roi"]["savings_pct"] > out_high["eval_roi"]["savings_pct"]

    disq_ids = {c["config_id"] for c in out_low["pareto"]["disqualified_options"]}
    assert "app-hallucinator::gemini-3.5-flash-lite" in disq_ids
    assert out_low["pareto"]["recommended_default"]["config_id"] == "app-clean::gemini-3.8-flash"
    assert out_high["pareto"]["recommended_default"] is None

    low_overflow = sum(r["payg_overflow_cost_usd"] for r in out_low["focus_chargeback"])
    high_overflow = sum(r["payg_overflow_cost_usd"] for r in out_high["focus_chargeback"])
    assert high_overflow > low_overflow
    # Hours 08:00Z, 09:00Z (idle), 10:00Z, 11:00Z -> 4 contiguous hours
    assert len(out_low["off_peak_plan"]["hourly_scheduled_eval_tokens"]) == 4


def test_dynamic_pareto_skips_unjudged_apps(tmp_path: Path) -> None:
    """`A-14`: Dynamic Pareto never fabricates a `0.90` pass rate for unjudged/uncalibrated apps."""
    base = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            f"tr-cal-{i:02d}",
            f"sp-cal-{i:02d}",
            app_id="calibrated-app",
            model_name="gemini-3.8-flash",
            offset_s=i,
            base_time=base,
        )
        for i in range(12)
    ] + [
        make_span(
            f"tr-unjudged-{j:02d}",
            f"sp-unjudged-{j:02d}",
            app_id="unjudged-cheap-app",
            model_name="gemini-3.5-flash-lite",
            input_tokens=1_000,
            cached_input_tokens=500,
            output_tokens=100,
            thinking_tokens=0,
            offset_s=30 + j,
            base_time=base,
        )
        for j in range(12)
    ]
    bundle = ScenarioCorpusBundle(
        corpus_id="pareto-a14-probe",
        title="Pareto A-14 Probe",
        spans=tuple(spans),
        judge_outcomes={f"tr-cal-{i:02d}": True for i in range(12)},
        sme_confusion_by_app={"calibrated-app": ConfusionMatrix(tp=45, fn=0, tn=5, fp=0)},
    )
    scen_path = tmp_path / "a14.json"
    scen_path.write_text(json.dumps(bundle.model_dump(mode="json")), encoding="utf-8")

    artifacts = run_showcase_pipeline(scenario_file=scen_path)
    assert artifacts.summaries["unjudged-cheap-app"].calibration_status == "UNJUDGED"
    assert artifacts.summaries["unjudged-cheap-app"].calibrated_pass_rate is None
    all_pareto_ids = {
        *(c.config_id for c in artifacts.pareto.eligible_options),
        *(c.config_id for c in artifacts.pareto.pareto_frontier),
        *(c.config_id for c in artifacts.pareto.disqualified_options),
    }
    assert all_pareto_ids == {"calibrated-app::gemini-3.8-flash"}
    assert artifacts.pareto.recommended_default is not None
    assert artifacts.pareto.recommended_default.config_id == "calibrated-app::gemini-3.8-flash"


def test_demo_chargeback_counts_idle_hours(tmp_path: Path) -> None:
    """`A-19`: Zero-traffic hours between active hours accrue idle PT tax and windows past `max_hours` raise."""
    base = datetime(2026, 11, 22, 8, 0, 0, tzinfo=UTC)
    # Hour 0 (08:00Z) and Hour 3 (11:00Z) each burn exactly 100K weighted tokens; Hours 1 & 2 are completely idle!
    spans = [
        make_span(
            "tr-h0",
            "sp-h0",
            app_id="valley-app",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            base_time=base,
        ),
        make_span(
            "tr-h3",
            "sp-h3",
            app_id="valley-app",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            base_time=base + timedelta(hours=3),
        ),
    ]
    # Total PT across the 4-hour grid = 400K (100K/hr), fixed cost = $100 ($25/hr).
    # Active hours 0 & 3 saturate 100K/hr ($50 utilized); idle hours 1 & 2 burn 0 ($50 idle tax)!
    bundle = ScenarioCorpusBundle(
        corpus_id="idle-hours-a19",
        title="Idle Hours A-19",
        spans=tuple(spans),
        reserved_pt_capacity_tokens=400_000.0,
        reserved_pt_fixed_cost_usd=100.0,
        app_cost_centers={"valley-app": "CC-VALLEY"},
    )
    scen_path = tmp_path / "a19.json"
    scen_path.write_text(json.dumps(bundle.model_dump(mode="json")), encoding="utf-8")

    artifacts = run_showcase_pipeline(scenario_file=scen_path)
    by_owner = {r.apphub_application_id: r for r in artifacts.focus_chargeback}
    assert by_owner["valley-app"].utilized_pt_cost_usd == pytest.approx(50.0, abs=1e-12)
    assert by_owner[
        "platform-shared-reservation"
    ].unutilized_idle_capacity_tax_usd == pytest.approx(50.0, abs=1e-12)
    assert len(artifacts.off_peak_plan.hourly_scheduled_eval_tokens) == 4

    with pytest.raises(ValueError, match="exceeds max_hours"):
        contiguous_utc_hours(["2026-11-01T00:00:00Z", "2026-11-10T00:00:00Z"], max_hours=168)


def test_workload_thresholds_worm_storage_parameterization_and_dark_loc_removals() -> None:
    """`workload_thresholds.storage_usd_per_outcome` parameterizes WORM storage; deleted dark artifacts stay gone."""
    assert not hasattr(finops_cost, "compute_default_span_cost_usd")
    assert not hasattr(finops_spend, "compute_default_span_cost_usd")
    assert not list(SQL_DIR.rglob("demo_docubridge_extensions.sql"))
    assert not list((REPO_ROOT / "bq_eval").rglob("seeds_banking.py"))
    assert not list((REPO_ROOT / "bq_eval").rglob("seeds_claims.py"))

    loadgen_sql = (SQL_DIR / "demo" / "demo_time_shift_loadgen.sql").read_text(encoding="utf-8")
    templates = build_golden_seed_templates()
    assert len(templates) == 16
    for tmpl in templates:
        assert f"'{tmpl.base_trace_id}'" in loadgen_sql

    base = datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-worm-1",
            "sp-worm-1",
            app_id="worm-app",
            otel_span_kind="SPAN_KIND_SERVER",
            outcome_id="out-worm-1",
            outcome_owner="worm-app",
            latency_ms=1000.0,
            base_time=base,
        ),
        make_span(
            "tr-worm-1",
            "sp-worm-close",
            parent_span_id="sp-worm-1",
            span_name="outcome.closed",
            app_id="worm-app",
            outcome_id="out-worm-1",
            outcome_owner="worm-app",
            model_name="none",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=0.0,
            offset_s=1,
            attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
            base_time=base,
        ),
    ]
    with open_runner_for_spans(spans) as runner:
        runner.register_sql(
            "UPDATE `agent_eval.workload_thresholds` SET storage_usd_per_outcome = 0.000400 "
            "WHERE apphub_application_id = 'worm-app'"
        )
        row = runner.query(
            "SELECT estimated_infra_cost_usd FROM `agent_eval.v_enterprise_cost_per_verified_outcome` "
            "WHERE apphub_application_id = 'worm-app'"
        ).rows[0]
    assert float(str(row["estimated_infra_cost_usd"])) == pytest.approx(
        0.000053 + 0.000400, abs=1e-12
    )
