"""Dark-LOC removal, parameterized app_registry storage, and dynamic `--scenario-file` FinOps."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.core.engine import cost as finops_cost
from bq_eval.demo.contract.results import ScenarioCorpusBundle
from bq_eval.demo.golden_seeds import build_golden_seed_templates
from bq_eval.finops.engine import spend as finops_spend
from tests.sql_helpers import (
    ENTERPRISE_OTEL_SPANS_BASE_INSERT,
    REPO_ROOT,
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
)


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

    for i in range(n_flash_traces):
        tid = f"{corpus_id}-flash-{i:03d}"
        # Spread across 4 hours (deep valley in hours 0-1, peak in hours 2-3)
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
        for j in range(4):
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
            # Judge fails 1 trace -> non-zero fabrication_rate disqualifies cheap model!
            judge_outcomes[tid] = j > 0

    return ScenarioCorpusBundle(
        corpus_id=corpus_id,
        title=f"Dynamic Scenario {corpus_id}",
        spans=tuple(spans),
        judge_outcomes=judge_outcomes,
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

    # Eval ROI tracks actual input trace count and error-tail rate (not hardcoded 100,000 / 3%).
    assert out_low["eval_roi"]["total_production_traces"] == 24
    assert out_high["eval_roi"]["total_production_traces"] == 40
    assert out_low["eval_roi"]["tier2_error_tail_traces"] == 0
    assert out_high["eval_roi"]["tier2_error_tail_traces"] == 8
    assert out_low["eval_roi"]["savings_pct"] > out_high["eval_roi"]["savings_pct"]

    # Pareto frontier derives candidates from input traces and disqualifies the hallucinating config.
    disq_ids = {c["config_id"] for c in out_low["pareto"]["disqualified_options"]}
    assert "app-hallucinator::gemini-3.5-flash-lite" in disq_ids
    assert out_low["pareto"]["recommended_default"]["config_id"] == "app-clean::gemini-3.8-flash"
    # High-error bundle (20% Rule-0 failures) fails the >=98% Rule-0 gate -> fail-closed None!
    assert out_high["pareto"]["recommended_default"] is None

    # Hourly FOCUS 1.2 chargeback reflects hourly peaks/overflow (high demand > 200k PT triggers PAYG overflow).
    low_overflow = sum(r["payg_overflow_cost_usd"] for r in out_low["focus_chargeback"])
    high_overflow = sum(r["payg_overflow_cost_usd"] for r in out_high["focus_chargeback"])
    assert high_overflow > low_overflow

    # Off-peak PT-valley schedule derives batch tokens and hourly slots directly from input spans.
    assert (
        out_low["off_peak_plan"]["total_batch_eval_tokens_requested"]
        != out_high["off_peak_plan"]["total_batch_eval_tokens_requested"]
    )
    assert len(out_low["off_peak_plan"]["hourly_scheduled_eval_tokens"]) == 3


def test_app_registry_worm_storage_parameterization_and_dark_loc_removals() -> None:
    """`app_registry.storage_usd_per_outcome` parameterizes WORM storage; deleted dark artifacts stay gone."""
    assert not hasattr(finops_cost, "compute_default_span_cost_usd")
    assert not hasattr(finops_spend, "compute_default_span_cost_usd")
    assert not (SQL_DIR / "demo_docubridge_extensions.sql").exists()
    assert not (REPO_ROOT / "bq_eval" / "showcase" / "seeds_banking.py").exists()
    assert not (REPO_ROOT / "bq_eval" / "showcase" / "seeds_claims.py").exists()

    # Single-source seed parity: every template in golden_seeds.py appears in demo_time_shift_loadgen.sql.
    loadgen_sql = (SQL_DIR / "demo" / "demo_time_shift_loadgen.sql").read_text(encoding="utf-8")
    templates = build_golden_seed_templates()
    assert len(templates) == 16
    for tmpl in templates:
        assert f"'{tmpl.base_trace_id}'" in loadgen_sql

    # Verify app_registry.storage_usd_per_outcome overrides default $0.000020 while explicit span attr wins.
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    conn.execute(
        "INSERT INTO app_registry (apphub_application_id, business_unit, sla_latency_ms, cost_center, is_external_counterparty, storage_usd_per_outcome) "
        "VALUES ('worm-app', 'banking', 5000.0, 'CC-WORM', 0, 0.000400)"
    )
    t0 = "2026-11-22T10:00:00+00:00"
    t1 = "2026-11-22T10:00:01+00:00"
    for out_id, attrs in (
        ("out-reg", "{}"),
        ("out-span-override", '{"outcome.storage_usd": "0.000900"}'),
    ):
        conn.executemany(
            ENTERPRISE_OTEL_SPANS_BASE_INSERT,
            [
                (
                    f"tr-{out_id}",
                    f"sp-{out_id}",
                    None,
                    "invoke_agent",
                    "worm-app",
                    "svc",
                    "agent",
                    "SPAN_KIND_SERVER",
                    "invoke_agent",
                    "gemini-3.8-flash",
                    None,
                    None,
                    "OK",
                    1,
                    "production_inference",
                    out_id,
                    "worm-app",
                    out_id,
                    None,
                    None,
                    None,
                    t0,
                    t1,
                    t1,
                    1000.0,
                    10_000,
                    0,
                    100,
                    0,
                    attrs,
                ),
                (
                    f"tr-{out_id}",
                    f"sp-close-{out_id}",
                    f"sp-{out_id}",
                    "outcome.closed",
                    "worm-app",
                    "svc",
                    "agent",
                    "SPAN_KIND_INTERNAL",
                    "invoke_agent",
                    "none",
                    None,
                    None,
                    "OK",
                    1,
                    "production_inference",
                    out_id,
                    "worm-app",
                    out_id,
                    None,
                    None,
                    None,
                    t1,
                    t1,
                    t1,
                    0.0,
                    0,
                    0,
                    0,
                    0,
                    '{"outcome.status": "succeeded", "outcome.verified": "true"}',
                ),
            ],
        )

    rows = {
        str(r["source_mode"]): float(r["estimated_infra_cost_usd"])
        for r in conn.execute(
            "SELECT source_mode, estimated_infra_cost_usd FROM v_enterprise_cost_per_verified_outcome"
        ).fetchall()
    }
    conn.close()
    # 1s server compute = $0.000053 + WORM storage override ($0.000400 vs explicit $0.000900).
    assert rows["out-reg"] == pytest.approx(0.000053 + 0.000400, abs=1e-12)
    assert rows["out-span-override"] == pytest.approx(0.000053 + 0.000900, abs=1e-12)
