"""Documentation-to-code contract tests."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.contract.rubrics import CANONICAL_RUBRICS
from bq_eval.core.engine.cost import compute_span_cost_usd
from bq_eval.demo.contract.results import (
    LiveFleetReceipt,
    ScenarioCorpusBundle,
    ShowcaseRunArtifacts,
)
from bq_eval.demo.pipeline import format_demo_json, load_m2_live_bundle, run_showcase_pipeline
from scripts import bq_dry_run

REPO_ROOT = Path(__file__).resolve().parent.parent


def _assert_kyc_receipt_from_spans(
    bundle: ScenarioCorpusBundle,
    lr: LiveFleetReceipt,
    artifacts: ShowcaseRunArtifacts,
) -> None:
    """Recompute KYC Step-1/Step-2 counts and judged row count directly from spans."""
    kyc_roots = [
        s
        for s in bundle.spans
        if s.resolved_application_id == "kyc-address-verification"
        and s.span_name != "outcome.closed"
    ]
    assert lr.kyc_cohort.total_cases == len(kyc_roots) == 280
    assert lr.kyc_cohort.structural_step1_rejects == sum(
        1 for s in kyc_roots if s.attributes.get("step1.reject_reason") == "structural"
    )
    assert lr.kyc_cohort.po_box_step1_traps_caught == sum(
        1 for s in kyc_roots if s.attributes.get("step1.reject_reason") == "po_box"
    )
    assert lr.kyc_cohort.ocr_step1_rejects == sum(
        1 for s in kyc_roots if s.attributes.get("step1.reject_reason") == "ocr_reject"
    )
    trap_spans = [s for s in kyc_roots if s.attributes.get("kyc.case_type") == "step2_trap"]
    clean_spans = [s for s in kyc_roots if s.attributes.get("kyc.case_type") == "clean"]
    assert lr.kyc_cohort.step2_semantic_traps_total == len(trap_spans)
    assert lr.kyc_cohort.step2_semantic_traps_caught == sum(
        1 for s in trap_spans if bundle.judge_outcomes.get(s.trace_id) is False
    )
    assert lr.kyc_cohort.clean_cases_evaluated == len(clean_spans)
    assert lr.kyc_cohort.false_positives == sum(
        1 for s in clean_spans if bundle.judge_outcomes.get(s.trace_id) is False
    )
    rule0_judged = sum(1 for s in kyc_roots if s.rule0_pass and s.trace_id in bundle.judge_outcomes)
    assert rule0_judged == artifacts.summaries["kyc-address-verification"].judged_samples == 236


def _assert_live_receipt_recomputes_from_spans(
    bundle: ScenarioCorpusBundle,
    lr: LiveFleetReceipt,
    artifacts: ShowcaseRunArtifacts,
) -> None:
    """Recompute KYC, RFP, Skill, and attribution metrics directly from bundle spans."""
    assert lr.summary_provenance == "reconstructed_from_bundle_spans"
    assert lr.labeled_fleet_cases == len({s.trace_id for s in bundle.spans}) == 343
    assert lr.cumulative_allspans_count == len(bundle.spans) == 676

    closed_spans = [
        s for s in bundle.spans if s.span_name == "outcome.closed" and s.outcome_id is not None
    ]
    assert lr.cumulative_closed_outcomes == len(closed_spans) == 333
    verified_outcome_ids = {
        s.outcome_id
        for s in closed_spans
        if s.outcome_id is not None
        and s.attributes.get("outcome.status") == "succeeded"
        and s.attributes.get("outcome.verified") == "true"
    }

    _assert_kyc_receipt_from_spans(bundle, lr, artifacts)

    rfp_roots = [
        s
        for s in bundle.spans
        if s.resolved_application_id == "benefits-rfp-procurement"
        and s.span_name != "outcome.closed"
    ]
    for modality in lr.rfp_modalities:
        mode_spans = [s for s in rfp_roots if s.source_mode == modality.source_mode]
        verified = sum(1 for s in mode_spans if s.outcome_id in verified_outcome_ids)
        assert verified == modality.verified_outcomes
        assert sum(s.input_tokens for s in mode_spans) == modality.input_tokens
        assert sum(s.output_tokens for s in mode_spans) == modality.output_tokens
        ai_cpvo = round(sum(compute_span_cost_usd(s) for s in mode_spans) / verified, 6)
        assert ai_cpvo == modality.ai_cost_per_verified_outcome_usd

    mer_roots = [
        s
        for s in bundle.spans
        if s.resolved_application_id == "meridian-loan-consolidation"
        and s.span_name != "outcome.closed"
    ]
    day1_spans = [s for s in mer_roots if s.skill_version == lr.skill_lineage.day1_version]
    day2_spans = [s for s in mer_roots if s.skill_version == lr.skill_lineage.day2_version]
    day1_ver = sum(1 for s in day1_spans if s.outcome_id in verified_outcome_ids)
    day2_ver = sum(1 for s in day2_spans if s.outcome_id in verified_outcome_ids)
    day1_cpvo = sum(compute_span_cost_usd(s) for s in day1_spans) / day1_ver
    day2_cpvo = sum(compute_span_cost_usd(s) for s in day2_spans) / day2_ver
    assert round(day1_cpvo, 6) == lr.skill_lineage.day1_cpvo_usd
    assert round(day2_cpvo, 6) == lr.skill_lineage.day2_cpvo_usd
    assert round((1.0 - day2_cpvo / day1_cpvo) * 100.0, 1) == lr.skill_lineage.cpvo_reduction_pct
    assert lr.skill_lineage.day2_in_policy_auto_approved == sum(
        1
        for s in day2_spans
        if s.attributes.get("policy.in_policy") == "true"
        and s.attributes.get("hitl.review") != "true"
    )
    assert lr.skill_lineage.day2_out_of_policy_escalated == sum(
        1
        for s in day2_spans
        if s.attributes.get("policy.in_policy") != "true"
        and s.attributes.get("hitl.review") == "true"
    )

    by_app: dict[str, list[OTelGenAISpanRecord]] = {}
    for s in bundle.spans:
        if s.span_name != "outcome.closed":
            by_app.setdefault(s.resolved_application_id, []).append(s)
    cohort_by_app = {c.apphub_application_id: c for c in lr.attribution_cohorts}
    assert set(cohort_by_app) == set(by_app)
    for app_id, cohort in cohort_by_app.items():
        app_spans = by_app[app_id]
        total_traces = len({s.trace_id for s in app_spans})
        total_spend = round(sum(compute_span_cost_usd(s) for s in app_spans), 6)
        assert cohort.total_traces == total_traces
        if app_id == "unregistered_app":
            assert cohort.attributed_traces == 0
            assert cohort.unattributed_spend_usd == total_spend
            assert cohort.attribution_coverage_pct == 0.0
        else:
            assert cohort.attributed_traces == total_traces
            assert cohort.attributed_spend_usd == total_spend
            assert cohort.attribution_coverage_pct == 100.0


def _assert_results_twin_and_kyc(
    results_md: str,
    parsed: dict[str, Any],
    lr: LiveFleetReceipt,
) -> None:
    kpi = parsed["kpi_table"]
    summaries = parsed["artifacts"]["summaries"]
    twin_summaries = parsed["artifacts"]["offline_twin_summaries"]

    twin = kpi["offline_twin_50_traces"]
    twin_order = twin_summaries["order-support-agent"]
    twin_cat = twin_summaries["catalog-enrichment-agent"]
    twin_total = int(twin_order["total_traces"]) + int(twin_cat["total_traces"])
    order_total = int(twin_order["total_traces"])
    cat_total = int(twin_cat["total_traces"])
    r0_passes = round(twin_order["rule0_pass_rate"] * order_total)
    r0_pct = f"{twin_order['rule0_pass_rate'] * 100:.0f}%"
    judged_n = int(twin_order["judged_samples"])
    judged_pass = round(twin_order["raw_judge_pass_rate"] * judged_n)
    raw_pct = f"{twin_order['raw_judge_pass_rate'] * 100:.1f}%"
    cal_pct = f"{twin_order['calibrated_pass_rate'] * 100:.1f}%"
    ci_low = f"{twin_order['calibrated_ci_95_lower'] * 100:.1f}%"
    ci_high = f"{twin_order['calibrated_ci_95_upper'] * 100:.0f}%"
    cpvo = f"${twin_order['cost_per_verified_outcome_usd']:.4f}"
    agent_usd = f"${twin_order['total_spend_usd'] - twin_order['autorater_judge_usd']:.4f}"
    judge_usd = f"${twin_order['autorater_judge_usd']:.4f}"
    savings = f"{twin['judge_spend_savings_pct']:.1f}%"
    base_judge_usd = f"${twin['baseline_100pct_judge_usd']:.0f}"
    funnel_judge_usd = f"${twin['stratified_3tier_judge_usd']:.2f}"

    assert f"**Twin (`{twin_total}`)**" in results_md
    assert (
        f"`order-support-agent` (`{order_total}`, `catalog-enrichment-agent` `{cat_total}/{cat_total}` unjudged `NULL`)"
        in results_md
    )
    assert f"`{r0_passes}/{order_total}` (`{r0_pct}`) rules" in results_md
    assert f"`{judged_pass}/{judged_n}` Rule-0 passing judged" in results_md
    assert f"`{raw_pct}` raw vs. `{cal_pct}` calibrated `[{ci_low}, {ci_high}]`" in results_md
    assert f"`{cpvo}`/outcome (`{agent_usd}` agent + `{judge_usd}` judge)" in results_md
    assert f"`{savings}` (`{base_judge_usd}` to `{funnel_judge_usd}`)" in results_md

    kyc = kpi["live_kyc_280_cases"]
    kyc_sum = summaries["kyc-address-verification"]
    kyc_total_rejects = (
        int(kyc["structural_step1_rejects"])
        + int(kyc["po_box_step1_traps_caught"])
        + int(kyc["ocr_step1_rejects"])
    )
    kyc_cpvo = f"${kyc_sum['outcome_cost_per_verified_usd']:.4f}"
    kyc_ci_lo = f"{kyc_sum['calibrated_ci_95_lower'] * 100:.2f}%"
    kyc_ci_hi = f"{kyc_sum['calibrated_ci_95_upper'] * 100:.2f}%"
    assert f"**KYC (`{lr.kyc_cohort.total_cases}`)**" in results_md
    assert f"`{kyc['structural_step1_rejects']}` structural" in results_md
    assert f"`{kyc['po_box_step1_traps_caught']}` PO Box" in results_md
    assert (
        f"`{kyc['ocr_step1_rejects']}` OCR (`{kyc_total_rejects}` `$0`-LLM rejects)" in results_md
    )
    assert f"`{kyc['structural_step1_rejects']}/" not in results_md
    assert f"`{kyc['po_box_step1_traps_caught']}/" not in results_md
    assert (
        f"`{lr.kyc_cohort.step2_semantic_traps_caught}/{lr.kyc_cohort.step2_semantic_traps_total}` traps"
        in results_md
    )
    assert (
        f"`{lr.kyc_cohort.false_positives}/{lr.kyc_cohort.clean_cases_evaluated}` FP" in results_md
    )
    assert f"`{kyc['judged_rows']}` Rule-0 passing" in results_md
    assert (
        f"`{lr.kyc_cohort.flash_argon_binary_agreed}/{lr.kyc_cohort.argon_calibration_pairs}` Flash/Argon match"
        in results_md
    )
    assert f"`AC2 = {kyc['gwet_ac2']:.4f}`" in results_md
    assert (
        f"calibrated `{kyc['calibrated_pass_rate'] * 100:.2f}%` `[{kyc_ci_lo}, {kyc_ci_hi}]`"
        in results_md
    )
    assert f"`{kyc_cpvo}`/outcome" in results_md


def _skill_day_counts(bundle: ScenarioCorpusBundle, lr: LiveFleetReceipt) -> tuple[int, int]:
    mer_roots = [
        s
        for s in bundle.spans
        if s.resolved_application_id == "meridian-loan-consolidation"
        and s.span_name != "outcome.closed"
    ]
    day1_cases = sum(1 for s in mer_roots if s.skill_version == lr.skill_lineage.day1_version)
    day2_cases = sum(1 for s in mer_roots if s.skill_version == lr.skill_lineage.day2_version)
    return day1_cases, day2_cases


def _assert_results_match_kpis(
    results_md: str,
    parsed: dict[str, Any],
    bundle: ScenarioCorpusBundle,
    *,
    checked_stmts: int,
    skipped_stmts: int,
) -> None:
    """Pin every published figure in `docs/results.md` directly to demo JSON and bundle spans."""
    kpi = parsed["kpi_table"]
    lr = bundle.live_receipt
    assert lr is not None

    assert f"`{lr.labeled_fleet_cases}` reconstructed cases" in results_md
    assert f"`{lr.cumulative_allspans_count}` spans" in results_md
    assert f"`{lr.cumulative_closed_outcomes}` `outcome.closed`" in results_md
    assert f"**`{checked_stmts}`** SQL statements" in results_md
    assert f"`{skipped_stmts}` DDL/proc skipped" in results_md

    _assert_results_twin_and_kyc(results_md, parsed, lr)

    assert "`100%` pass" not in results_md
    rfp = kpi["live_rfp_cliff_35_cases"]
    rfp_total = sum(int(m.verified_outcomes) for m in lr.rfp_modalities)
    assert f"**RFP (`{rfp_total}`)**" in results_md
    assert f"`100%` rules (`{rfp_total}/{rfp_total}`, unjudged)" in results_md
    assert (
        f"`A2A_PATH_A` (`{rfp['A2A_PATH_A']['verified_outcomes']}`) `${rfp['A2A_PATH_A']['ai_cost_per_verified_outcome_usd']:.4f}` AI"
        in results_md
    )
    assert (
        f"Flash (`{rfp['PDF_PATH_B_FLASH']['verified_outcomes']}`) `${rfp['PDF_PATH_B_FLASH']['ai_cost_per_verified_outcome_usd']:.5f}` AI"
        in results_md
    )
    assert (
        f"Argon (`{rfp['PDF_PATH_B_ARGON']['verified_outcomes']}`) `${rfp['PDF_PATH_B_ARGON']['ai_cost_per_verified_outcome_usd']:.4f}` AI"
        in results_md
    )

    skill = kpi["live_skill_18_cases_and_shadow_10"]
    day1_cases, day2_cases = _skill_day_counts(bundle, lr)
    skill_total = day1_cases + day2_cases
    shadow_cohort = next(
        c for c in lr.attribution_cohorts if c.apphub_application_id == "unregistered_app"
    )
    shadow_n = int(shadow_cohort.total_traces)
    assert f"**Skill (`{skill_total}`) & shadow (`{shadow_n}`)**" in results_md
    assert f"`100%` rules (`{skill_total}/{skill_total}`, unjudged)" in results_md
    assert (
        f"`{lr.skill_lineage.day1_version}` (`{day1_cases}`) `${skill['day1_cpvo_usd']:.5f}` AI/outcome"
        in results_md
    )
    assert (
        f"`{lr.skill_lineage.day2_version}` (`{day2_cases}`) `${skill['day2_cpvo_usd']:.6f}` AI/outcome"
        in results_md
    )
    assert f"`{skill['cpvo_reduction_pct']:.1f}%` drop" in results_md
    assert (
        f"`{lr.skill_lineage.day2_in_policy_auto_approved}/{lr.skill_lineage.day2_in_policy_total}` auto"
        in results_md
    )
    assert (
        f"`{lr.skill_lineage.day2_out_of_policy_escalated}/{lr.skill_lineage.day2_out_of_policy_total}` escalated"
        in results_md
    )
    assert f"`{shadow_n}` shadow hit `unregistered_app`" in results_md


def _assert_readme_matches_kpis(
    readme: str,
    parsed: dict[str, Any],
    bundle: ScenarioCorpusBundle,
) -> None:
    """Pin every published figure in `README.md` directly to demo JSON and bundle spans (T-3)."""
    kpi = parsed["kpi_table"]
    summaries = parsed["artifacts"]["summaries"]
    twin_summaries = parsed["artifacts"]["offline_twin_summaries"]
    lr = bundle.live_receipt
    assert lr is not None

    twin = kpi["offline_twin_50_traces"]
    order = twin_summaries["order-support-agent"]
    twin_total = sum(int(s["total_traces"]) for s in twin_summaries.values())

    assert (
        f"{lr.labeled_fleet_cases} reconstructed cases ({lr.cumulative_allspans_count} spans)"
        in readme
    )
    assert (
        f"Judge spend down {twin['judge_spend_savings_pct']:.1f}% "
        f"(${twin['baseline_100pct_judge_usd']:.0f} to ${twin['stratified_3tier_judge_usd']:.2f})"
        in readme
    )

    kyc = kpi["live_kyc_280_cases"]
    kyc_sum = summaries["kyc-address-verification"]
    kyc_rejects = (
        int(kyc["structural_step1_rejects"])
        + int(kyc["po_box_step1_traps_caught"])
        + int(kyc["ocr_step1_rejects"])
    )
    assert f"Address checks, {lr.kyc_cohort.total_cases} cases" in readme
    assert (
        f"{kyc_rejects} rejects at no AI cost; "
        f"{lr.kyc_cohort.step2_semantic_traps_caught} of {lr.kyc_cohort.step2_semantic_traps_total} planted traps caught, "
        f"{lr.kyc_cohort.false_positives} false alarms in {lr.kyc_cohort.clean_cases_evaluated}"
    ) in readme
    kyc_rate_1d = f"{kyc['calibrated_pass_rate'] * 100:.1f}%"
    kyc_ci_lo_1d = f"{kyc_sum['calibrated_ci_95_lower'] * 100:.1f}%"
    kyc_ci_hi_0d = f"{kyc_sum['calibrated_ci_95_upper'] * 100:.0f}%"
    assert (
        f"{lr.kyc_cohort.flash_argon_binary_agreed} of {lr.kyc_cohort.argon_calibration_pairs} "
        f"pass/fail matches; corrected pass rate {kyc_rate_1d} ({kyc_ci_lo_1d} to {kyc_ci_hi_0d})"
    ) in readme

    assert f"Order support, {twin_total} traces" in readme
    assert (
        f"Raw {order['raw_judge_pass_rate'] * 100:.1f}%, "
        f"corrected {order['calibrated_pass_rate'] * 100:.1f}% "
        f"(wide interval, {order['calibrated_ci_95_lower'] * 100:.0f}% to {order['calibrated_ci_95_upper'] * 100:.0f}%)"
    ) in readme

    skill = kpi["live_skill_18_cases_and_shadow_10"]
    day1_cases, day2_cases = _skill_day_counts(bundle, lr)
    skill_total = day1_cases + day2_cases
    rfp_total = sum(int(m.verified_outcomes) for m in lr.rfp_modalities)
    assert f"Skill upgrade, {skill_total} cases" in readme
    assert (
        f"AI cost per outcome down {skill['cpvo_reduction_pct']:.1f}% "
        f"({day1_cases} vs. {day2_cases} cases)"
    ) in readme
    assert f"AC2 {kyc['gwet_ac2']:.2f}" in readme
    assert f"{day1_cases} and {day2_cases} cases" in readme
    assert f"({rfp_total} cases)" in readme


def test_results_doc_kpis_match_demo_json_and_offline_twin() -> None:
    """`bq-eval demo --format json` & bundle spans reproduce `README.md`, `docs/results.md`, and dry-run receipt."""
    results_md = (REPO_ROOT / "docs" / "results.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    getting_started = (REPO_ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    bundle = load_m2_live_bundle()
    assert bundle.live_receipt is not None
    artifacts = run_showcase_pipeline(corpus="m2-live")
    _assert_live_receipt_recomputes_from_spans(bundle, bundle.live_receipt, artifacts)

    parsed = json.loads(format_demo_json(artifacts))
    summaries = parsed["artifacts"]["summaries"]
    for registered_app in (
        "kyc-address-verification",
        "benefits-rfp-procurement",
        "meridian-loan-consolidation",
    ):
        assert summaries[registered_app]["outcome_verified_count"] > 0
        assert summaries[registered_app]["outcome_cost_per_verified_usd"] is not None
    assert summaries["unregistered_app"]["outcome_verified_count"] == 0
    assert summaries["unregistered_app"]["outcome_cost_per_verified_usd"] is None

    dry_run_receipt = json.loads(
        (REPO_ROOT / "benchmarks" / "results" / "bq_dry_run_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = bq_dry_run.collect_dry_run_manifest(project_id="showcase-project")
    assert dry_run_receipt == manifest
    assert manifest["checked"] == 75
    assert manifest["skipped"] == 17

    _assert_results_match_kpis(
        results_md,
        parsed,
        bundle,
        checked_stmts=int(manifest["checked"]),
        skipped_stmts=int(manifest["skipped"]),
    )
    _assert_readme_matches_kpis(readme, parsed, bundle)

    for token in (
        "uv sync --extra dev",
        "bq_eval.cli serve",
        "bq_eval.cli judge-sql",
        "bq_eval.cli loadgen",
        "bq_eval.cli bootstrap",
        "BigQueryAgentAnalyticsPlugin",
    ):
        assert token in getting_started
    for doc in (readme, results_md, getting_started):
        for stale in ("rfp_quote_accuracy_4pt_v1", "51/51", "0.0 mocks"):
            assert stale not in doc


def test_rubric_count_and_names_match_docs_csv_and_code() -> None:
    """Canonical rubric count matches Python, CSV, `AGENTS.md`, `DECISIONS.md`, and `docs/architecture-and-sql.md`."""
    expected_count = len(CANONICAL_RUBRICS)
    assert expected_count == 8

    with (REPO_ROOT / "sql" / "core" / "step2_rubrics.csv").open(
        "r", encoding="utf-8", newline=""
    ) as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == expected_count

    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    decisions_md = (REPO_ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    arch_md = (REPO_ROOT / "docs" / "architecture-and-sql.md").read_text(encoding="utf-8")

    assert f"{expected_count} canonical 4-point ordinal rubrics" in agents_md
    assert f"{expected_count} `CANONICAL_RUBRICS`" in agents_md
    assert "rfp_quote_accuracy_4pt_v1" not in agents_md
    assert "rfp_quote_completeness_4pt_v1" in agents_md
    assert f"{expected_count} canonical 4-point rubrics" in decisions_md
    assert f"{expected_count} canonical 4-point rubrics" in arch_md
    assert "## 6B. Multi-Agent Outcome Attribution Wire Contract (`EVAL-P11`)" in arch_md


def test_license_and_skill_eval_yaml_fail_closed_contract() -> None:
    """Repo ships Apache-2.0 `LICENSE` and fail-closed `skills/bq-eval/EVAL.yaml`."""
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file()
    assert "Apache License" in license_path.read_text(encoding="utf-8")

    eval_yaml_path = REPO_ROOT / "skills" / "bq-eval" / "EVAL.yaml"
    assert eval_yaml_path.is_file()
    eval_text = eval_yaml_path.read_text(encoding="utf-8")
    assert "fail_closed_on_zero_scored: true" in eval_text
    assert re.search(r"(?m)^min_scored_cases:\s*[1-9]\d*\s*$", eval_text)
    case_ids = re.findall(r"(?m)^\s+-\s+id:\s+(\S+)", eval_text)
    assert len(case_ids) >= 4
    assert "EVAL-P10" in eval_text


def test_documented_test_count_matches_pytest_collection() -> None:
    """Test count cited in `docs/results.md` and `AGENTS.md` matches actual collected pytest items."""
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    m = re.search(r"(\d+)\s+tests?\s+collected", res.stdout)
    assert m is not None, f"Could not parse pytest collection output:\n{res.stdout}"
    collected = int(m.group(1))

    results_md = (REPO_ROOT / "docs" / "results.md").read_text(encoding="utf-8")
    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"`{collected}/{collected}` tests" in results_md, (
        f"docs/results.md test count out of sync with collected={collected}"
    )
    assert f"`{collected}` tests" in agents_md, (
        f"AGENTS.md test count out of sync with collected={collected}"
    )
