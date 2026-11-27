"""Documentation, `specs/` & `design/` tree, and receipt-provenance contract tests."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

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
_CHECKBOX_RE = re.compile(r"(?m)^\s*-\s*\[[ xX!]\]")


def _parse_front_matter(md_path: Path) -> tuple[dict[str, str | list[str]], str]:
    text = md_path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{md_path.relative_to(REPO_ROOT)} missing YAML front-matter"
    _, fm_raw, body = text.split("---\n", 2)
    meta: dict[str, str | list[str]] = {}
    active_list_key: str | None = None
    for line in fm_raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and active_list_key is not None:
            val = line[4:].strip().strip('"').strip("'")
            existing = meta[active_list_key]
            assert isinstance(existing, list)
            existing.append(val)
            continue
        active_list_key = None
        key, _, rest = line.partition(":")
        key = key.strip()
        cleaned = rest.strip()
        if not cleaned:
            meta[key] = []
            active_list_key = key
        elif cleaned.startswith("[") and cleaned.endswith("]"):
            meta[key] = [
                item.strip().strip('"').strip("'")
                for item in cleaned[1:-1].split(",")
                if item.strip()
            ]
        else:
            meta[key] = cleaned.strip('"').strip("'")
    return meta, body


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


def _assert_design_tree(design_files: list[Path]) -> None:
    design_meta: dict[Path, dict[str, str | list[str]]] = {}
    for md_path in design_files:
        meta, body = _parse_front_matter(md_path)
        assert meta.get("type") in {"root", "reference", "atom"}
        assert isinstance(meta.get("tags"), list) and meta["tags"]
        assert not _CHECKBOX_RE.search(body), (
            f"{md_path.relative_to(REPO_ROOT)} must stay status-free"
        )
        design_meta[md_path.resolve()] = meta

    roots = [p for p, m in design_meta.items() if m["type"] == "root"]
    assert roots == [(REPO_ROOT / "design" / "bq-eval.md").resolve()]

    for md_path, meta in design_meta.items():
        if meta["type"] != "root":
            up_rel = str(meta.get("up", ""))
            assert up_rel
            parent = (md_path.parent / up_rel).resolve()
            assert parent in design_meta, f"{md_path.name} `up: {up_rel}` missing from design/ tree"
            parent_down = design_meta[parent].get("down", [])
            assert isinstance(parent_down, list)
            resolved_children = {(parent.parent / child).resolve() for child in parent_down}
            assert md_path in resolved_children, f"{parent.name} `down` omits child {md_path.name}"
        down_list = meta.get("down", [])
        assert isinstance(down_list, list)
        for child_rel in down_list:
            child = (md_path.parent / child_rel).resolve()
            assert child in design_meta, (
                f"{md_path.name} `down: {child_rel}` missing from design/ tree"
            )
            assert (child.parent / str(design_meta[child].get("up", ""))).resolve() == md_path


def test_spec_and_design_front_matter_and_tree_contract() -> None:
    """`specs/` and `design/` carry valid bidirectional front-matter, stay status-free, and omit retired docs."""
    for retired in (
        "docs/architecture-and-sql.md",
        "docs/bqaa-and-dual-path-architecture.md",
        "docs/finops-and-cost-management.md",
        "docs/getting-started.md",
        "docs/results.md",
        "docs/assets/architecture_card.svg",
        ".stitch/DESIGN.md",
        "skills/bq-eval/SKILL.md",
        "examples/otel-collector-wif.yaml",
    ):
        assert not (REPO_ROOT / retired).exists(), f"Retired doc file still present: {retired}"

    design_files = sorted((REPO_ROOT / "design").rglob("*.md"))
    spec_files = sorted((REPO_ROOT / "specs").rglob("*.md"))
    assert design_files and spec_files
    _assert_design_tree(design_files)

    for md_path in spec_files:
        meta, body = _parse_front_matter(md_path)
        assert meta.get("type") in {"reference", "atom"}
        assert isinstance(meta.get("tags"), list) and meta["tags"]
        assert not _CHECKBOX_RE.search(body), (
            f"{md_path.relative_to(REPO_ROOT)} must stay status-free"
        )
        rel_links = meta.get("related", [])
        assert isinstance(rel_links, list) and rel_links
        for rel_target in rel_links:
            assert (md_path.parent / rel_target).resolve().is_file(), (
                f"{md_path.relative_to(REPO_ROOT)} related target missing: {rel_target}"
            )

    for doc_path in (
        *spec_files,
        *design_files,
        REPO_ROOT / "README.md",
        REPO_ROOT / "CRITICAL.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "docs" / "roadmap.md",
    ):
        text = doc_path.read_text(encoding="utf-8")
        for stale_term in ("BigQueryAgentAnalyticsPlugin", "EVENT_ONLY", "sqlite_views"):
            assert stale_term not in text, (
                f"{doc_path.relative_to(REPO_ROOT)} still contains old-generation term {stale_term!r}"
            )


def test_receipts_recompute_from_spans_and_dry_run_manifest() -> None:
    """`bq-eval demo --format json` & bundle spans reproduce committed fleet and dry-run receipts."""
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
    assert manifest["checked"] > 0
    assert manifest["skipped"] > 0
    assert "silver/spans.sql" in manifest["per_file"]
    assert "core/gold_cost_views.sql" in manifest["per_file"]
    assert "core/views.sql" not in manifest["per_file"]


def test_rubric_count_and_names_match_docs_csv_and_code() -> None:
    """Canonical rubric count matches Python, CSV, and `AGENTS.md`."""
    expected_count = len(CANONICAL_RUBRICS)
    assert expected_count == 8

    with (REPO_ROOT / "sql" / "core" / "step2_rubrics.csv").open(
        "r", encoding="utf-8", newline=""
    ) as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == expected_count

    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"`{expected_count}` canonical 4-point ordinal rubrics" in agents_md
    assert f"`{expected_count}` `CANONICAL_RUBRICS`" in agents_md
    assert "rfp_quote_accuracy_4pt_v1" not in agents_md


def test_license_present_and_apache_2() -> None:
    """Repo ships Apache-2.0 `LICENSE`."""
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file()
    assert "Apache License" in license_path.read_text(encoding="utf-8")


def _collect_pytest_count() -> int:
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
    return int(m.group(1))


def test_documented_test_count_matches_pytest_collection() -> None:
    """Test count cited in `AGENTS.md` matches actual collected pytest items."""
    collected = _collect_pytest_count()
    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"`{collected}` tests" in agents_md, (
        f"AGENTS.md test count out of sync with collected={collected}"
    )


def test_roadmap_baseline_and_m16_receipt_contract() -> None:
    """`docs/roadmap.md` baseline counters and `m16_live_cloud_proof.json` invariants stay pinned."""
    collected = _collect_pytest_count()
    roadmap_md = (REPO_ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    assert f"`{collected}/{collected}` pytest" in roadmap_md, (
        f"docs/roadmap.md pytest baseline out of sync with collected={collected}"
    )
    schema_files = [
        p
        for p in (REPO_ROOT / "sql" / "silver" / "schema").glob("*.json")
        if p.name != "labels_registry.json"
    ]
    assert f"`{len(schema_files)}` JSON schemas" in roadmap_md

    res_dir = REPO_ROOT / "benchmarks" / "results"
    proof = json.loads((res_dir / "m16_live_cloud_proof.json").read_text(encoding="utf-8"))
    anon_sha = hashlib.sha256(
        (res_dir / "m16_anonymized_traces_100.jsonl").read_bytes()
    ).hexdigest()
    assert proof["apphub_fleet_export"]["traces_sha256"] == anon_sha
    assert proof["unconfounded_context_cliff"]["short_arm_count"] == 30
    assert proof["unconfounded_context_cliff"]["long_arm_count"] == 30
    assert proof["unconfounded_context_cliff"]["cost_multiplier"] > 200.0
    assert proof["s5_concurrent_quota_lock"]["zero_overspend"] is True
    assert proof["s15_views_and_job_costs"]["deployed_view_count"] >= 37
