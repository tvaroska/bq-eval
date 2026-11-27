"""Workbench, Empirical Receipt, `< 5s` `CI_ONLY` Optimizer Export & Cross-Repo Wire Contracts."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq_eval import __version__
from bq_eval.cli.app import main as cli_main
from bq_eval.cli.server import create_workbench_server
from bq_eval.core.contract.otel import GenAISpanKind
from bq_eval.core.contract.rubrics import CANONICAL_RUBRICS, build_autorater_system_prompt
from bq_eval.demo.contract.results import ShowcaseRunArtifacts
from bq_eval.demo.loadgen import (
    evaluate_time_shifted_corpus,
    export_time_shifted_jsonl,
    generate_time_shifted_corpus,
)
from bq_eval.demo.pipeline import (
    DEFAULT_M2_RECEIPT_PATH,
    DEFAULT_M2_SHA256_PATH,
    build_retail_2app_bundle,
    render_markdown_report,
    run_showcase_pipeline,
    verify_receipt_sha256,
)
from bq_eval.demo.render.renderer import render_html_scorecard, render_page_view_html
from bq_eval.demo.render.view import build_showcase_page_view
from bq_eval.finops.contract.results import FocusChargebackRow
from bq_eval.finops.engine.spend import (
    allocate_focus_hourly_chargeback,
    diagnose_prefix_cache_dilution,
    plan_off_peak_eval_schedule,
)
from bq_eval.runner.engine.eval_adapter import run_ci_only_eval
from tests.conftest import eval_hourly_burndown, make_span, open_runner_for_spans

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_stitch_design_contract_and_gm3_light_tokens() -> None:
    """Rendered HTML scorecard enforces GM3 Light tokens, Pareto table, and reduced-motion."""
    artifacts = run_showcase_pipeline(corpus="m2-live")
    html_doc = render_html_scorecard(artifacts, live_api=True)
    assert "--surface: #f8f9fa;" in html_doc
    assert "tabular-nums" in html_doc
    assert "@media (prefers-reduced-motion: reduce)" in html_doc
    for panel_id in ("tab-quality", "tab-receipts", "tab-pareto", "tab-finops"):
        assert f'id="{panel_id}"' in html_doc
    assert "flash-3.8-cached-prefix" in html_doc
    assert "unverified-draft-prompt" in html_doc
    assert "DISQUALIFIED" in html_doc
    assert artifacts.finops_model_source == "illustrative_reference"
    assert "Illustrative reference model options" in html_doc
    assert run_showcase_pipeline(corpus="golden-4app").finops_model_source == "derived_from_corpus"


def test_html_renderer_escapes_hostile_xss_payloads() -> None:
    """Single-point `_esc()` neutralizes `<script>` and attribute breakouts in all dynamic strings."""
    base = run_showcase_pipeline(corpus="retail-2app")
    xss_app = '<script>alert("xss")</script>" onload="evil()'
    order_sum = base.summaries["order-support-agent"].model_copy(
        update={"apphub_application_id": xss_app}
    )
    dil = base.cache_dilution.model_copy(
        update={
            "apphub_application_id": xss_app,
            "recommendation": "Fix <img src=x onerror=alert(1)> & consolidate.",
        }
    )
    fb_row = FocusChargebackRow(
        apphub_application_id=xss_app,
        cost_center='CC-"><svg onload=alert(2)>',
        weighted_burndown_tokens=1_000.0,
        utilization_share=1.0,
        utilized_pt_cost_usd=10.0,
        unutilized_idle_capacity_tax_usd=0.0,
        payg_overflow_cost_usd=0.0,
        total_allocated_cost_usd=10.0,
    )
    hostile_artifacts = ShowcaseRunArtifacts(
        corpus_id='evil"><script>alert(3)</script>',
        summaries={xss_app: order_sum},
        gwet_ac2=base.gwet_ac2,
        eval_roi=base.eval_roi,
        cache_dilution=dil,
        pareto=base.pareto,
        focus_chargeback=(fb_row,),
        off_peak_plan=base.off_peak_plan,
    )
    html_doc = render_html_scorecard(hostile_artifacts, live_api=True)
    assert '<script>alert("xss")</script>' not in html_doc
    assert "<script>alert(3)</script>" not in html_doc
    assert "<img src=x onerror=alert(1)>" not in html_doc
    assert "<svg onload=alert(2)>" not in html_doc
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in html_doc


def test_workbench_http_server_loopback_endpoints_via_proxy_bypass() -> None:
    """`bq-eval serve` HTTP server returns 200 on `/`, `/healthz`, and `/api/v1/demo` over loopback."""
    server = create_workbench_server(host="127.0.0.1", port=0, corpus="m2-live")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        raw_host, port = server.server_address[:2]
        host = (
            raw_host.decode("ascii") if isinstance(raw_host, (bytes, bytearray)) else str(raw_host)
        )
        base_url = f"http://{host}:{port}"

        with opener.open(f"{base_url}/healthz", timeout=5) as resp:
            assert resp.status == 200
            health = json.loads(resp.read().decode("utf-8"))
            assert health["status"] == "ok"

        with opener.open(f"{base_url}/?corpus=golden-4app", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "docubridge-underwriting" in body
            assert 'data-live-api="true"' in body

        with opener.open(f"{base_url}/api/v1/demo?corpus=m2-live", timeout=5) as resp:
            assert resp.status == 200
            api_payload = json.loads(resp.read().decode("utf-8"))
            assert api_payload["corpus_id"] == "m2-live"
            assert "live_kyc_280_cases" in api_payload["kpi_table"]

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            opener.open(f"{base_url}/?corpus=nonexistent", timeout=5)
        assert exc_info.value.code == 400

        with pytest.raises(urllib.error.HTTPError) as api_exc:
            opener.open(f"{base_url}/api/v1/demo?corpus=nonexistent", timeout=5)
        assert api_exc.value.code == 400
        api_err = json.loads(api_exc.value.read().decode("utf-8"))
        assert "error" in api_err
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_m2_live_receipt_sha256_manifest_and_custom_scenario_replay(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`m2_live_receipt.json` verifies against `SHA256SUMS`, detects tampering, and replays `--scenario-file`."""
    digest = verify_receipt_sha256(DEFAULT_M2_RECEIPT_PATH, DEFAULT_M2_SHA256_PATH)
    assert len(digest) == 64

    tampered_receipt = tmp_path / "m2_live_receipt.json"
    tampered_manifest = tmp_path / "SHA256SUMS"
    tampered_receipt.write_text('{"corrupted": true}\n', encoding="utf-8")
    tampered_manifest.write_text(f"{'0' * 64}  m2_live_receipt.json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_receipt_sha256(tampered_receipt, tampered_manifest)

    custom_path = tmp_path / "custom_bundle.json"
    custom_bundle = build_retail_2app_bundle().model_copy(update={"corpus_id": "custom-scenario"})
    custom_path.write_text(
        json.dumps(custom_bundle.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    rc = cli_main(["demo", "--scenario-file", str(custom_path), "--format", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["corpus_id"] == "custom-scenario"
    assert "order-support-agent" in out["artifacts"]["summaries"]

    m2_artifacts = run_showcase_pipeline(corpus="m2-live")
    receipt = m2_artifacts.live_receipt
    assert receipt is not None
    assert receipt.summary_provenance == "reconstructed_from_bundle_spans"
    assert receipt.labeled_fleet_cases == 343
    assert receipt.cumulative_closed_outcomes == 333
    assert receipt.cumulative_allspans_count == 676
    assert receipt.kyc_cohort.structural_step1_rejects == 28
    assert receipt.kyc_cohort.po_box_step1_traps_caught == 8
    assert receipt.kyc_cohort.ocr_step1_rejects == 8

    workbench_html = render_page_view_html(build_showcase_page_view(m2_artifacts, live_api=False))
    assert "28 structural + 8 PO Box + 8 OCR" in workbench_html
    assert "44 of 280 cases rejected" in workbench_html
    assert "28/28" not in workbench_html
    assert "8/8" not in workbench_html
    assert sum(m.verified_outcomes for m in receipt.rfp_modalities) == 35
    assert sum(c.total_traces for c in receipt.attribution_cohorts) == 343

    kyc_sum = m2_artifacts.summaries["kyc-address-verification"]
    assert kyc_sum.judged_samples == 236
    assert kyc_sum.outcome_total_count == 280
    assert kyc_sum.outcome_verified_count == 216
    assert kyc_sum.outcome_cost_per_verified_ai_usd == pytest.approx(
        kyc_sum.cost_per_verified_outcome_usd, abs=1e-12
    )
    assert kyc_sum.outcome_cost_per_verified_usd is not None
    assert kyc_sum.outcome_cost_per_verified_ai_usd is not None
    assert kyc_sum.outcome_cost_per_verified_usd > kyc_sum.outcome_cost_per_verified_ai_usd

    md_report = render_markdown_report(m2_artifacts)
    assert "## 5. Reconstructed Fleet Cohort Receipts" in md_report
    assert "reconstructed_from_bundle_spans" in md_report
    assert "illustrative reference" in md_report.lower()


def test_capacity_wire_contract_hourly_burndown_jsonl_and_hourly_focus_chargeback(
    tmp_path: Path,
) -> None:
    """Verify `v_capacity_hourly_burndown_export`, `otel_spans.jsonl`, and `allocate_focus_hourly_chargeback`."""
    t_h0 = datetime(2026, 11, 21, 10, 15, 0, tzinfo=UTC)
    t_h1 = datetime(2026, 11, 21, 11, 30, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-cap-1",
            "sp-cap-1",
            app_id="tenant-alpha",
            input_tokens=100_000,
            cached_input_tokens=20_000,
            output_tokens=5_000,
            thinking_tokens=1_000,
            base_time=t_h0,
        ),
        make_span(
            "tr-cap-2",
            "sp-cap-2",
            app_id="tenant-beta",
            input_tokens=20_000,
            cached_input_tokens=0,
            output_tokens=2_000,
            thinking_tokens=0,
            base_time=t_h1,
        ),
    ]
    export_rows = eval_hourly_burndown(spans)
    assert len(export_rows) == 2

    with open_runner_for_spans(spans) as runner:
        bq_rows = runner.query(
            "SELECT * FROM `agent_eval.v_capacity_hourly_burndown_export` "
            "ORDER BY timestamp ASC, tenant ASC"
        ).rows

    assert len(bq_rows) == 2
    for bq_r, exp_r in zip(bq_rows, export_rows, strict=True):
        parsed_ts = datetime.fromisoformat(str(bq_r["timestamp"]).replace("Z", "+00:00"))
        assert parsed_ts.minute == 0
        assert bq_r["tenant"] == exp_r.tenant == exp_r.apphub_application_id
        assert bq_r["model"] == exp_r.model == exp_r.model_name
        assert int(str(bq_r["cached_tokens"])) == exp_r.cached_tokens == exp_r.cached_input_tokens
        assert int(str(bq_r["thoughts_tokens"])) == exp_r.thoughts_tokens == exp_r.thinking_tokens
        assert float(str(bq_r["precomputed_weighted_burndown_tokens"])) == pytest.approx(
            exp_r.precomputed_weighted_burndown_tokens, abs=1e-12
        )

    seed_spans, seed_payloads, _ = generate_time_shifted_corpus(
        reference_time=t_h0,
        total_replicas=1,
        last_hour_replicas=1,
    )
    spans_jsonl, _ = export_time_shifted_jsonl(seed_spans, seed_payloads, tmp_path)
    parsed_jsonl = [
        json.loads(line) for line in spans_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert parsed_jsonl
    for rec in parsed_jsonl:
        datetime.fromisoformat(rec["timestamp"])
        assert isinstance(rec["http_status_code"], int)
        assert rec["http_status_code"] in {200, 429, 500, 504}
        assert rec["otel_status_code"] in {"OK", "ERROR", "UNSET"}
        assert isinstance(rec["precomputed_weighted_burndown_tokens"], float)

    hourly_chargeback = allocate_focus_hourly_chargeback(
        hourly_burndown_by_app={
            "tenant-alpha": [80_000.0, 20_000.0],
            "tenant-beta": [40_000.0, 20_000.0],
        },
        app_cost_centers={"tenant-alpha": "CC-ALPHA", "tenant-beta": "CC-BETA"},
        hourly_reserved_pt_capacity_tokens=100_000.0,
        reserved_pt_fixed_cost_usd=200.0,
        payg_overflow_usd_per_1m_weighted=1.0,
    )
    by_tenant = {r.apphub_application_id: r for r in hourly_chargeback}
    expected_alpha_pt = (80_000.0 / 120_000.0) * 100.0 + 20.0
    expected_beta_pt = (40_000.0 / 120_000.0) * 100.0 + 20.0
    assert by_tenant["tenant-alpha"].utilized_pt_cost_usd == pytest.approx(expected_alpha_pt)
    assert by_tenant["tenant-beta"].utilized_pt_cost_usd == pytest.approx(expected_beta_pt)
    assert by_tenant["platform-shared-reservation"].unutilized_idle_capacity_tax_usd == (
        pytest.approx(60.0)
    )
    expected_overflow = (20_000.0 / 1e6) * 1.0
    assert sum(r.payg_overflow_cost_usd for r in hourly_chargeback) == pytest.approx(
        expected_overflow
    )
    assert sum(r.total_allocated_cost_usd for r in hourly_chargeback) == pytest.approx(
        200.0 + expected_overflow
    )


def test_docubridge_canonical_select_views_and_ci_only_optimizer_export() -> None:
    """Execute DocuBridge's 4 canonical `SELECT` queries and `< 5s` `CI_ONLY` Optimizer Gold export."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-db-1",
            "sp-db-1",
            app_id="meridian-loan-consolidation",
            service_name="bridge-meridian",
            agent_name="meridian_underwriter",
            otel_span_kind="SPAN_KIND_SERVER",
            outcome_id="out-db-1",
            outcome_owner="meridian-loan-consolidation",
            source_mode="PDF_PATH_B_FLASH",
            skill_id="loan-payoff-statement",
            skill_version="1.1.0",
            latency_ms=2000.0,
            input_tokens=5_000,
            cached_input_tokens=2_000,
            output_tokens=400,
            thinking_tokens=0,
            attributes={
                "bq_eval.case_id": "case-001",
                "bq_eval.eval_run_id": "opt-run-1",
                "bq_eval.eval_split": "train",
                "bq_eval.skill.sha256": "a" * 64,
                "bq_eval.skill.protected_block_sha256": "b" * 64,
            },
            base_time=base,
        ),
        make_span(
            "tr-db-1",
            "sp-db-close",
            parent_span_id="sp-db-1",
            span_name="outcome.closed",
            app_id="meridian-loan-consolidation",
            service_name="bridge-meridian",
            agent_name="meridian_underwriter",
            outcome_id="out-db-1",
            outcome_owner="meridian-loan-consolidation",
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
    ]

    with open_runner_for_spans(spans) as runner:
        docubridge_queries = (
            (
                "SELECT apphub_application_id, source_mode, agent_name, total_outcomes, "
                "verified_outcomes, measured_model_cost_usd, measured_judge_cost_usd, "
                "estimated_infra_cost_usd, cost_per_verified_outcome_ai_usd, "
                "cost_per_verified_outcome_usd FROM `agent_eval.v_cost_per_verified_outcome`"
            ),
            (
                "SELECT apphub_application_id, total_traces, attributed_traces, attributed_spend_usd, "
                "unattributed_spend_usd, attribution_coverage_pct, passes_coverage_gate "
                "FROM `agent_eval.v_attribution_coverage`"
            ),
            (
                "SELECT apphub_application_id, skill_id, skill_version, skill_stage, total_outcomes, "
                "verified_outcomes, human_reviews_per_verified_outcome, cost_per_verified_outcome_usd "
                "FROM `agent_eval.v_skill_lineage_roi`"
            ),
            (
                "SELECT apphub_application_id, rubric_id, total_traces, rule0_pass_rate, "
                "calibration_status, calibrated_pass_rate, total_spend_usd, "
                "cost_per_verified_outcome_usd FROM `agent_eval.v_agent_eval_and_finops_summary`"
            ),
        )
        for query in docubridge_queries:
            rows = runner.query(query).rows
            assert len(rows) == 1

    ci_res = run_ci_only_eval(spans[:1], max_seconds=5.0)
    assert ci_res["calibration_mode"] == "CI_ONLY"
    assert float(str(ci_res["elapsed_sec"])) < 5.0
    cands = ci_res["candidates"]
    assert isinstance(cands, list) and len(cands) == 1
    cand = cands[0]
    assert cand["optimizer_mode"] == "CI_ONLY"
    assert bool(cand["split_disjoint_verified"]) is True
    assert bool(cand["passes_prefix_cache_floor"]) is True
    assert cand["calibrated_holdout_pass_rate"] is None


def test_rubric_temperature_mixed_app_rejection_and_root_span_order() -> None:
    """`RubricSpec.temperature` feeds prompt, mixed-app dilution fails, and child-first spans find root."""
    base_rubric = CANONICAL_RUBRICS["groundedness_4pt_v1"]
    custom_temp_rubric = base_rubric.model_copy(update={"temperature": 0.25})
    prompt = build_autorater_system_prompt(custom_temp_rubric)
    assert "T=0.25" in prompt
    assert "T=0.0" not in prompt

    mixed_spans = [
        make_span("trace-mix-1", "span-mix-1", app_id="app-one"),
        make_span("trace-mix-2", "span-mix-2", app_id="app-two"),
    ]
    with pytest.raises(ValueError, match="single-application"):
        diagnose_prefix_cache_dilution(mixed_spans, distinct_prompt_variants=2)

    anchor = datetime(2026, 11, 21, 15, 0, 0, tzinfo=UTC)
    child_first_spans = [
        make_span(
            "tr-order-test",
            "sp-child-tool",
            parent_span_id="sp-true-root",
            app_id=None,
            span_kind=GenAISpanKind.EXECUTE_TOOL,
            offset_ms=50,
            attributes={"business_unit": "wrong_child_bu"},
            base_time=anchor,
        ),
        make_span(
            "tr-order-test",
            "sp-true-root",
            parent_span_id=None,
            app_id="root-app-owner",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            offset_ms=0,
            attributes={"business_unit": "true_root_bu", "cost_center": "CC-ROOT"},
            base_time=anchor,
        ),
    ]
    report = evaluate_time_shifted_corpus(spans=child_first_spans, reference_time=anchor)
    assert len(report.step1_rows) == 1
    assert report.step1_rows[0].apphub_application_id == "root-app-owner"
    assert report.step1_rows[0].business_unit == "true_root_bu"


def test_true_valley_water_filling_cli_version_and_clean_error_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Valley water-filling equalizes deepest slots, CLI `--version` & clean errors work, zero dead symbols."""
    plan = plan_off_peak_eval_schedule(
        hourly_production_burndown=[100.0, 200.0, 450.0],
        hourly_reserved_pt_capacity=500.0,
        total_batch_eval_tokens=300.0,
    )
    assert plan.hourly_scheduled_eval_tokens == pytest.approx((200.0, 100.0, 0.0))

    with pytest.raises(SystemExit) as ver_exit:
        cli_main(["--version"])
    assert ver_exit.value.code == 0
    assert f"bq-eval {__version__}" in capsys.readouterr().out

    rc_err = cli_main(["judge-sql", "--project-id", "bad; DROP TABLE x"])
    captured = capsys.readouterr()
    assert rc_err == 2
    assert "bq-eval: error:" in captured.err
    assert "Traceback" not in captured.err

    banned_tokens = ("del service_name", "del payloads", "reserved_pt_Fixed")
    for py_file in sorted((REPO_ROOT / "bq_eval").rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        for tok in banned_tokens:
            assert tok not in text, f"{py_file} still contains {tok!r}"
