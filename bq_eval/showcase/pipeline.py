"""Showcase scenario loaders, SQLite twin replay, Markdown scorecard, and JSON formatter."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from bq_eval.contract.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.contract.results import (
    ConfusionMatrix,
    LiveFleetReceipt,
    ModelOption,
    ParetoEvalResult,
    ScenarioCorpusBundle,
    ShowcaseRunArtifacts,
    SQLTraceEvalSummary,
)
from bq_eval.engine.calibration import compute_gwet_ac2
from bq_eval.engine.finops import (
    allocate_focus_two_pool_chargeback,
    compute_eval_finops_roi,
    compute_pareto_frontier,
    diagnose_prefix_cache_dilution,
    plan_off_peak_eval_schedule,
)
from bq_eval.engine.sql_eval import evaluate_traces_in_sqlite
from bq_eval.showcase.loadgen import generate_time_shifted_corpus
from bq_eval.showcase.view import (
    format_calibrated_pass,
    format_cpvo_usd,
    format_irr_gate_verdict,
    format_raw_judge_pass,
)

CorpusChoice = Literal["m2-live", "golden-4app", "retail-2app"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_M2_RECEIPT_PATH = _REPO_ROOT / "benchmarks" / "results" / "m2_live_receipt.json"
DEFAULT_M2_SHA256_PATH = _REPO_ROOT / "benchmarks" / "results" / "SHA256SUMS"


def build_showcase_dataset() -> tuple[
    list[OTelGenAISpanRecord],
    dict[str, bool],
    ConfusionMatrix,
]:
    """Generate the deterministic 50-trace 2-app reference twin (`retail-2app`)."""
    base_time = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    judge_outcomes: dict[str, bool] = {}

    for i in range(40):
        trace_id = f"trace-order-{i:04d}"
        is_error_tail = i in (38, 39)
        is_escalated = i in (35, 36, 37)
        traffic_type = (
            FinOpsTrafficType.CONFLICT_ESCALATION
            if is_escalated
            else FinOpsTrafficType.PRODUCTION_INFERENCE
        )
        model_name = "gemini-4-argon" if is_escalated else "gemini-3.8-flash"
        input_tokens = 240_000 if i == 37 else 12_000
        cached_tokens = 2_400 if i == 37 else 3_000

        spans.append(
            OTelGenAISpanRecord(
                trace_id=trace_id,
                span_id=f"span-root-{i:04d}",
                parent_span_id=None,
                start_time=base_time + timedelta(seconds=i * 15),
                end_time=base_time + timedelta(seconds=i * 15, milliseconds=850 + (i * 20)),
                latency_ms=850.0 + (i * 20),
                status_code="OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                service_name="order-support-service",
                gcp_apphub_application_id="order-support-agent",
                agent_name="order-concierge",
                model_name=model_name,
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                output_tokens=650,
                thinking_tokens=350 if is_escalated else 150,
                rule0_pass=not is_error_tail,
                finops_traffic_type=traffic_type,
            )
        )
        spans.append(
            OTelGenAISpanRecord(
                trace_id=trace_id,
                span_id=f"span-tool-{i:04d}",
                parent_span_id=f"span-root-{i:04d}",
                start_time=base_time + timedelta(seconds=i * 15, milliseconds=100),
                end_time=base_time + timedelta(seconds=i * 15, milliseconds=320),
                latency_ms=220.0,
                status_code="ERROR" if is_error_tail else "OK",
                span_kind=GenAISpanKind.EXECUTE_TOOL,
                service_name="order-support-service",
                gcp_apphub_application_id="order-support-agent",
                agent_name="order-concierge",
                model_name=model_name,
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                thinking_tokens=0,
                tool_name="lookup_order_ledger",
                tool_call_status="VALIDATION_ERROR" if is_error_tail else "SUCCESS",
                rule0_pass=not is_error_tail,
                finops_traffic_type=traffic_type,
            )
        )
        if i < 20:
            judge_outcomes[trace_id] = i not in (18, 19)
        elif is_error_tail:
            judge_outcomes[trace_id] = False

    for j in range(10):
        trace_id = f"trace-catalog-{j:04d}"
        spans.append(
            OTelGenAISpanRecord(
                trace_id=trace_id,
                span_id=f"span-cat-{j:04d}",
                parent_span_id=None,
                start_time=base_time + timedelta(minutes=15, seconds=j * 10),
                end_time=base_time + timedelta(minutes=15, seconds=j * 10, milliseconds=420),
                latency_ms=420.0,
                status_code="OK",
                span_kind=GenAISpanKind.CHAT,
                service_name="agent-service",
                gcp_apphub_application_id=None,
                agent_name="catalog-enricher",
                model_name="gemini-3.5-flash-lite",
                input_tokens=8_000,
                cached_input_tokens=6_400,
                output_tokens=300,
                thinking_tokens=0,
                rule0_pass=True,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
                attributes={"gcp.apphub.application.id": "catalog-enrichment-agent"},
            )
        )

    confusion = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    return spans, judge_outcomes, confusion


def build_retail_2app_bundle() -> ScenarioCorpusBundle:
    """Wrap `build_showcase_dataset()` into a portable `ScenarioCorpusBundle`."""
    spans, judge_outcomes, confusion = build_showcase_dataset()
    rater_a = [4, 4, 3, 4, 3, 4, 1, 2, 4, 3, 4, 4, 3, 2, 4, 4, 3, 4, 1, 4]
    rater_b = [4, 4, 3, 4, 3, 3, 1, 2, 4, 3, 4, 4, 3, 2, 4, 4, 3, 4, 1, 4]
    sme_pairs = tuple(zip(rater_a, rater_b, strict=True))
    return ScenarioCorpusBundle(
        corpus_id="retail-2app",
        title="50-Trace Offline Reference Twin (order-support-agent + catalog-enrichment-agent)",
        spans=tuple(spans),
        judge_outcomes=judge_outcomes,
        sme_confusion_by_app={"order-support-agent": confusion},
        sme_ordinal_pairs_by_app={"order-support-agent": sme_pairs},
        distinct_prompt_variants=4,
        dilution_target_app_id="order-support-agent",
        reserved_pt_capacity_tokens=100_000_000.0,
        reserved_pt_fixed_cost_usd=120.0,
        app_cost_centers={
            "order-support-agent": "CC-RETAIL-OPS",
            "catalog-enrichment-agent": "CC-MERCH-CATALOG",
        },
    )


def build_golden_4app_bundle() -> ScenarioCorpusBundle:
    """Build a 4-application `ScenarioCorpusBundle` over the 16 golden trace templates."""
    ref_time = datetime(2026, 11, 14, 15, 0, 0, tzinfo=UTC)
    spans, payloads, _ = generate_time_shifted_corpus(
        reference_time=ref_time,
        total_replicas=5,
        last_hour_replicas=2,
    )
    judge_outcomes = {
        p.trace_id: (p.expected_ordinal_score >= 3)
        for p in payloads
        if p.apphub_application_id in {"docubridge-underwriting", "quoting-concierge-adk"}
    }
    sme_pairs = (
        *((4, 4) for _ in range(10)),
        *((3, 3) for _ in range(4)),
        *((4, 3) for _ in range(2)),
        *((2, 2) for _ in range(2)),
        *((1, 1) for _ in range(2)),
    )
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    return ScenarioCorpusBundle(
        corpus_id="golden-4app",
        title="4-Application Golden Seed Corpus (80 Time-Shifted Traces)",
        spans=tuple(spans),
        judge_outcomes=judge_outcomes,
        sme_confusion_by_app={
            "docubridge-underwriting": cm,
            "quoting-concierge-adk": cm,
        },
        sme_ordinal_pairs_by_app={
            "docubridge-underwriting": sme_pairs,
            "quoting-concierge-adk": sme_pairs,
        },
        distinct_prompt_variants=4,
        dilution_target_app_id="quoting-concierge-adk",
        reserved_pt_capacity_tokens=100_000_000.0,
        reserved_pt_fixed_cost_usd=120.0,
        app_cost_centers={
            "docubridge-underwriting": "CC-4100-CRE",
            "quoting-concierge-adk": "CC-2100-PERS",
            "claims-triage-langgraph": "CC-3200-CLM",
            "unregistered_app": "UNASSIGNED",
        },
    )


def verify_receipt_sha256(
    receipt_path: Path = DEFAULT_M2_RECEIPT_PATH,
    manifest_path: Path = DEFAULT_M2_SHA256_PATH,
) -> str:
    """Verify `receipt_path` SHA-256 digest against `SHA256SUMS` and return the hex digest."""
    if not receipt_path.is_file():
        raise ValueError(f"Scenario receipt file not found: {receipt_path}")
    actual_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    if manifest_path.is_file():
        expected_map: dict[str, str] = {}
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                expected_map[parts[1].lstrip("*")] = parts[0]
        expected_sha = expected_map.get(receipt_path.name)
        if expected_sha is not None and actual_sha != expected_sha:
            raise ValueError(
                f"SHA-256 mismatch for {receipt_path.name}: expected {expected_sha}, got {actual_sha}"
            )
    return actual_sha


def load_scenario_bundle(path: Path) -> ScenarioCorpusBundle:
    """Load and validate a `ScenarioCorpusBundle` JSON file from disk."""
    if not path.is_file():
        raise ValueError(f"Scenario file does not exist: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ScenarioCorpusBundle.model_validate(raw)


def load_m2_live_bundle() -> ScenarioCorpusBundle:
    """Verify SHA-256 and load the committed `m2-live-20261113b` multi-agent receipt bundle."""
    verify_receipt_sha256(DEFAULT_M2_RECEIPT_PATH, DEFAULT_M2_SHA256_PATH)
    return load_scenario_bundle(DEFAULT_M2_RECEIPT_PATH)


def _default_pareto_candidates() -> ParetoEvalResult:
    return compute_pareto_frontier(
        candidates=[
            ModelOption(
                config_id="flash-3.8-cached-prefix",
                model_name="gemini-3.8-flash",
                rule0_pass_rate=0.992,
                fabrication_rate=0.0,
                calibrated_pass_rate=0.915,
                cost_per_verified_outcome_usd=0.0019,
                p95_latency_ms=980.0,
            ),
            ModelOption(
                config_id="argon-4-high-reasoning",
                model_name="gemini-4-argon",
                rule0_pass_rate=0.998,
                fabrication_rate=0.0,
                calibrated_pass_rate=0.968,
                cost_per_verified_outcome_usd=0.0084,
                p95_latency_ms=2150.0,
            ),
            ModelOption(
                config_id="flash-3.8-uncached-fragmented",
                model_name="gemini-3.8-flash",
                rule0_pass_rate=0.985,
                fabrication_rate=0.0,
                calibrated_pass_rate=0.890,
                cost_per_verified_outcome_usd=0.0034,
                p95_latency_ms=1280.0,
            ),
            ModelOption(
                config_id="unverified-draft-prompt",
                model_name="gemini-3.5-flash-lite",
                rule0_pass_rate=0.940,
                fabrication_rate=0.025,
                calibrated_pass_rate=0.820,
                cost_per_verified_outcome_usd=0.0009,
                p95_latency_ms=540.0,
            ),
        ]
    )


def _evaluate_bundle(
    bundle: ScenarioCorpusBundle,
    *,
    offline_twin_summaries: dict[str, SQLTraceEvalSummary] | None = None,
) -> ShowcaseRunArtifacts:
    sme_pairs_arg: dict[str, list[tuple[int, int]]] | None = (
        {app: list(pairs) for app, pairs in bundle.sme_ordinal_pairs_by_app.items()}
        if bundle.sme_ordinal_pairs_by_app
        else None
    )
    sme_conf_arg: dict[str, ConfusionMatrix] | None = (
        dict(bundle.sme_confusion_by_app) if bundle.sme_confusion_by_app else None
    )
    summaries = evaluate_traces_in_sqlite(
        spans=bundle.spans,
        judge_outcomes=bundle.judge_outcomes,
        sme_confusion=sme_conf_arg,
        sme_ordinal_pairs=sme_pairs_arg,
        sampling_strata=bundle.sampling_strata or None,
        rubric_id=bundle.rubric_id,
    )

    ac2: float | None = None
    if bundle.sme_ordinal_pairs_by_app:
        first_pairs = next(iter(bundle.sme_ordinal_pairs_by_app.values()))
        ac2 = compute_gwet_ac2(
            [h for h, _ in first_pairs],
            [j for _, j in first_pairs],
        )

    eval_roi = compute_eval_finops_roi(
        total_production_traces=100_000,
        error_or_sla_tail_rate=0.03,
        canary_sample_rate=0.05,
        judge_cost_per_trace_usd=0.0025,
    )

    target_app = bundle.dilution_target_app_id or next(iter(summaries))
    target_spans = [s for s in bundle.spans if s.resolved_application_id == target_app]
    cache_dilution = diagnose_prefix_cache_dilution(
        spans=target_spans,
        distinct_prompt_variants=bundle.distinct_prompt_variants,
        target_cache_hit_ratio=0.75,
    )

    if bundle.corpus_id == "retail-2app":
        burndown_map = {
            "order-support-agent": 65_000_000.0,
            "catalog-enrichment-agent": 15_000_000.0,
        }
    else:
        burndown_map = {
            app_id: summary.total_weighted_burndown_tokens for app_id, summary in summaries.items()
        }
    cost_centers = {
        app_id: bundle.app_cost_centers.get(app_id, "UNASSIGNED") for app_id in burndown_map
    }
    focus_rows = allocate_focus_two_pool_chargeback(
        app_burndown_tokens=burndown_map,
        app_cost_centers=cost_centers,
        reserved_pt_capacity_tokens=bundle.reserved_pt_capacity_tokens,
        reserved_pt_fixed_cost_usd=bundle.reserved_pt_fixed_cost_usd,
    )

    off_peak_plan = plan_off_peak_eval_schedule(
        hourly_production_burndown=[
            2_000_000.0,
            1_500_000.0,
            1_200_000.0,
            1_500_000.0,
            4_500_000.0,
            5_000_000.0,
            4_800_000.0,
            3_200_000.0,
        ],
        hourly_reserved_pt_capacity=5_000_000.0,
        total_batch_eval_tokens=12_000_000.0,
    )

    return ShowcaseRunArtifacts(
        corpus_id=bundle.corpus_id,
        summaries=summaries,
        gwet_ac2=ac2,
        eval_roi=eval_roi,
        cache_dilution=cache_dilution,
        pareto=_default_pareto_candidates(),
        focus_chargeback=tuple(focus_rows),
        off_peak_plan=off_peak_plan,
        live_receipt=bundle.live_receipt,
        offline_twin_summaries=offline_twin_summaries,
    )


def run_showcase_pipeline(
    corpus: CorpusChoice = "m2-live",
    *,
    scenario_file: Path | None = None,
) -> ShowcaseRunArtifacts:
    """Execute the Tier 1-3 SQLite evaluation and FinOps pipeline over the selected corpus."""
    if scenario_file is not None:
        custom_bundle = load_scenario_bundle(scenario_file)
        return _evaluate_bundle(custom_bundle)

    if corpus == "retail-2app":
        return _evaluate_bundle(build_retail_2app_bundle())
    if corpus == "golden-4app":
        return _evaluate_bundle(build_golden_4app_bundle())
    if corpus == "m2-live":
        twin_artifacts = _evaluate_bundle(build_retail_2app_bundle())
        live_bundle = load_m2_live_bundle()
        return _evaluate_bundle(
            live_bundle,
            offline_twin_summaries=twin_artifacts.summaries,
        )
    raise ValueError(
        f"Unsupported corpus '{corpus}'. Expected m2-live, golden-4app, or retail-2app."
    )


def format_demo_json(artifacts: ShowcaseRunArtifacts) -> str:
    """Serialize `ShowcaseRunArtifacts` + deterministic `README.md` KPI table JSON (`M7-2`)."""
    twin = artifacts.offline_twin_summaries or artifacts.summaries
    kpi_table: dict[str, Any] = {}

    if "order-support-agent" in twin:
        order = twin["order-support-agent"]
        cat = twin.get("catalog-enrichment-agent")
        kpi_table["offline_twin_50_traces"] = {
            "order_support_agent": {
                "total_traces": order.total_traces,
                "sql_rule0_pass_rate": round(order.rule0_pass_rate, 6),
                "raw_judge_pass_rate": (
                    round(order.raw_judge_pass_rate, 6)
                    if order.raw_judge_pass_rate is not None
                    else None
                ),
                "calibrated_pass_rate": (
                    round(order.calibrated_pass_rate, 6)
                    if order.calibrated_pass_rate is not None
                    else None
                ),
                "calibrated_ci_95": (
                    [
                        round(order.calibrated_ci_95_lower, 6),
                        round(order.calibrated_ci_95_upper, 6),
                    ]
                    if order.calibrated_ci_95_lower is not None
                    and order.calibrated_ci_95_upper is not None
                    else None
                ),
                "cost_per_verified_outcome_usd": (
                    round(order.cost_per_verified_outcome_usd, 6)
                    if order.cost_per_verified_outcome_usd is not None
                    else None
                ),
            },
            "unjudged_app_fail_closed": (
                {
                    "apphub_application_id": cat.apphub_application_id,
                    "total_traces": cat.total_traces,
                    "calibration_status": cat.calibration_status,
                    "calibrated_pass_rate": cat.calibrated_pass_rate,
                    "cost_per_verified_outcome_usd": cat.cost_per_verified_outcome_usd,
                }
                if cat is not None
                else None
            ),
            "judge_spend_savings_pct": round(artifacts.eval_roi.savings_pct, 2),
            "baseline_100pct_judge_usd": round(artifacts.eval_roi.baseline_100pct_judge_usd, 2),
            "stratified_3tier_judge_usd": round(artifacts.eval_roi.stratified_3tier_judge_usd, 2),
        }

    if artifacts.live_receipt is not None:
        lr = artifacts.live_receipt
        kyc_sum = artifacts.summaries.get("kyc-address-verification")
        kpi_table["summary_provenance"] = lr.summary_provenance
        kpi_table["live_kyc_280_cases"] = {
            "total_cases": lr.kyc_cohort.total_cases,
            "structural_step1_rejects": lr.kyc_cohort.structural_step1_rejects,
            "po_box_step1_traps_caught": lr.kyc_cohort.po_box_step1_traps_caught,
            "ocr_step1_rejects": lr.kyc_cohort.ocr_step1_rejects,
            "judged_rows": kyc_sum.judged_samples if kyc_sum is not None else None,
            "step2_semantic_traps_caught": lr.kyc_cohort.step2_semantic_traps_caught,
            "step2_semantic_traps_total": lr.kyc_cohort.step2_semantic_traps_total,
            "trap_recall": lr.kyc_cohort.trap_recall,
            "trap_recall_wilson_ci_95": list(lr.kyc_cohort.trap_recall_wilson_ci_95),
            "false_positives": lr.kyc_cohort.false_positives,
            "clean_cases_evaluated": lr.kyc_cohort.clean_cases_evaluated,
            "argon_binary_agreed": lr.kyc_cohort.flash_argon_binary_agreed,
            "argon_calibration_pairs": lr.kyc_cohort.argon_calibration_pairs,
            "gwet_ac2": round(artifacts.gwet_ac2, 4) if artifacts.gwet_ac2 is not None else None,
            "calibrated_pass_rate": (
                round(kyc_sum.calibrated_pass_rate, 4)
                if kyc_sum is not None and kyc_sum.calibrated_pass_rate is not None
                else None
            ),
            "calibrated_ci_95": (
                [
                    round(kyc_sum.calibrated_ci_95_lower, 4),
                    round(kyc_sum.calibrated_ci_95_upper, 4),
                ]
                if kyc_sum is not None
                and kyc_sum.calibrated_ci_95_lower is not None
                and kyc_sum.calibrated_ci_95_upper is not None
                else None
            ),
        }
        kpi_table["live_rfp_cliff_35_cases"] = {
            m.source_mode: {
                "agent_name": m.agent_name,
                "verified_outcomes": m.verified_outcomes,
                "input_tokens": m.input_tokens,
                "ai_cost_per_verified_outcome_usd": m.ai_cost_per_verified_outcome_usd,
                "total_cost_per_verified_outcome_usd": m.total_cost_per_verified_outcome_usd,
            }
            for m in lr.rfp_modalities
        }
        shadow_cohort = next(
            (c for c in lr.attribution_cohorts if c.apphub_application_id == "unregistered_app"),
            None,
        )
        kpi_table["live_skill_18_cases_and_shadow_10"] = {
            "skill_id": lr.skill_lineage.skill_id,
            "day1_version": lr.skill_lineage.day1_version,
            "day2_version": lr.skill_lineage.day2_version,
            "day1_cpvo_usd": lr.skill_lineage.day1_cpvo_usd,
            "day2_cpvo_usd": lr.skill_lineage.day2_cpvo_usd,
            "cpvo_reduction_pct": lr.skill_lineage.cpvo_reduction_pct,
            "day1_human_reviews_per_outcome": lr.skill_lineage.day1_human_reviews_per_outcome,
            "day2_human_reviews_per_outcome": lr.skill_lineage.day2_human_reviews_per_outcome,
            "day2_in_policy_auto_approved": lr.skill_lineage.day2_in_policy_auto_approved,
            "day2_in_policy_total": lr.skill_lineage.day2_in_policy_total,
            "day2_out_of_policy_escalated": lr.skill_lineage.day2_out_of_policy_escalated,
            "day2_out_of_policy_total": lr.skill_lineage.day2_out_of_policy_total,
            "unregistered_shadow_attribution_coverage_pct": (
                shadow_cohort.attribution_coverage_pct if shadow_cohort is not None else None
            ),
        }

    payload = {
        "corpus_id": artifacts.corpus_id,
        "kpi_table": kpi_table,
        "artifacts": artifacts.model_dump(mode="json"),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _render_live_receipt_markdown(receipt: LiveFleetReceipt) -> str:
    kyc = receipt.kyc_cohort
    step1_total = (
        kyc.structural_step1_rejects + kyc.po_box_step1_traps_caught + kyc.ocr_step1_rejects
    )
    rfp_parts = "; ".join(
        f"`{m.source_mode}` (`{m.verified_outcomes}` outcomes, AI `${m.ai_cost_per_verified_outcome_usd:.5f}`/outcome, total `${m.total_cost_per_verified_outcome_usd:.5f}`/outcome)"
        for m in receipt.rfp_modalities
    )
    sl = receipt.skill_lineage
    attr_parts = "; ".join(
        f"`{c.apphub_application_id}` (`{c.attributed_traces}/{c.total_traces}` attributed, `${c.attributed_spend_usd + c.unattributed_spend_usd:.5f}`, `{c.attribution_coverage_pct:.1f}%`)"
        for c in receipt.attribution_cohorts
    )
    return (
        f"\n## 5. Reconstructed Fleet Cohort Receipts (`{receipt.run_id}`)\n\n"
        f"- **Provenance & Scope:** `{receipt.summary_provenance}` (`{receipt.labeled_fleet_cases}` labeled cases, "
        f"`{receipt.cumulative_closed_outcomes}` closed outcomes, `{receipt.cumulative_allspans_count}` spans; "
        f"lag p50 `{receipt.allspans_ingestion_lag_p50_ms:.0f} ms`, p95 `{receipt.allspans_ingestion_lag_p95_ms:,.0f} ms`)\n"
        f"- **KYC Funnel (`{kyc.total_cases}` cases):** `{kyc.structural_step1_rejects}` structural + "
        f"`{kyc.po_box_step1_traps_caught}` PO Box + `{kyc.ocr_step1_rejects}` OCR = `{step1_total}` `$0` Step-1 rejects; "
        f"`{kyc.step2_semantic_traps_caught}/{kyc.step2_semantic_traps_total}` Step-2 traps caught "
        f"(`{kyc.trap_recall * 100:.1f}%`, `{kyc.false_positives}/{kyc.clean_cases_evaluated}` false positives, "
        f"`{kyc.flash_argon_binary_agreed}/{kyc.argon_calibration_pairs}` Flash-vs-Argon)\n"
        f"- **Benefits RFP Modalities:** {rfp_parts}\n"
        f"- **Skill Lineage (`{sl.skill_id}`):** `{sl.day1_version}` (`${sl.day1_cpvo_usd:.5f}`/outcome) -> "
        f"`{sl.day2_version}` (`${sl.day2_cpvo_usd:.6f}`/outcome, `{sl.cpvo_reduction_pct:.1f}%` CPVO reduction; "
        f"human reviews `{sl.day1_human_reviews_per_outcome:.2f}` -> `{sl.day2_human_reviews_per_outcome:.2f}`/outcome)\n"
        f"- **Attribution Cohorts:** {attr_parts}\n"
    )


def render_markdown_report(artifacts: ShowcaseRunArtifacts) -> str:
    """Format a concise Markdown executive scorecard dynamically over `artifacts.summaries` (`F7`)."""
    summary_lines = "\n".join(
        f"| `{s.apphub_application_id}` | {s.total_traces} | {s.rule0_pass_rate * 100:.1f}% | "
        f"`{s.calibration_status}` | {format_raw_judge_pass(s)} | {format_calibrated_pass(s)} | "
        f"{s.total_weighted_burndown_tokens:,.0f} | ${s.total_spend_usd:.4f} | "
        f"{format_cpvo_usd(s.cost_per_verified_outcome_usd)} |"
        for s in artifacts.summaries.values()
    )
    primary_cal = next(
        (
            s
            for s in artifacts.summaries.values()
            if s.calibration_status == "CALIBRATED" and s.calibrated_pass_rate is not None
        ),
        None,
    )
    irr_val, irr_caption, _ = format_irr_gate_verdict(
        artifacts.gwet_ac2,
        primary_cal.passes_irr_gate if primary_cal is not None else None,
    )

    roi = artifacts.eval_roi
    dil = artifacts.cache_dilution
    rec = artifacts.pareto.recommended_default
    rec_line = (
        f"`{rec.config_id}` (`{rec.model_name}`, CPVO `${rec.cost_per_verified_outcome_usd:.4f}`)"
        if rec is not None
        else "`none` (`NULL` — no candidate passed zero-fabrication and Rule-0 gates)"
    )

    focus_lines = "\n".join(
        f"| `{row.apphub_application_id}` | `{row.cost_center}` | "
        f"{row.weighted_burndown_tokens:,.0f} | {row.utilization_share * 100:.1f}% | "
        f"${row.utilized_pt_cost_usd:.2f} | ${row.unutilized_idle_capacity_tax_usd:.2f} | "
        f"${row.payg_overflow_cost_usd:.2f} | ${row.total_allocated_cost_usd:.2f} |"
        for row in artifacts.focus_chargeback
    )

    live_section = (
        _render_live_receipt_markdown(artifacts.live_receipt)
        if artifacts.live_receipt is not None
        else ""
    )

    return f"""# BQ Eval & FinOps Executive Scorecard (`bq-eval`, Corpus `{artifacts.corpus_id}`)

## 1. Tier 1-3 Application Quality & Cost per Verified Outcome (`CPVO`)

| Application ID | Traces | Rule 0 Pass | Calibration Status | Raw Judge | Rogan-Gladen Calibrated | Weighted Burndown | Total Spend | CPVO |
| :--- | ---: | ---: | :--- | ---: | :--- | ---: | ---: | ---: |
{summary_lines}

- **Inter-Rater Reliability (`Gwet's AC2`):** `{irr_val}` ({irr_caption})

## 2. 3-Tier Evaluation Funnel FinOps ROI (`100,000` Production Traces)

- **Baseline (`100%` LLM-as-a-Judge):** `${roi.baseline_100pct_judge_usd:,.2f}` (`{roi.total_production_traces:,}` judge calls)
- **3-Tier Stratified Funnel (`5%` Canary + `100%` Error/SLA-Tail):** `${roi.stratified_3tier_judge_usd:,.2f}` (`{roi.tier2_total_judged_traces:,}` judge calls)
- **Net Evaluation Cost Savings:** **`${roi.net_savings_usd:,.2f}` (`{roi.savings_pct:.1f}%` reduction)**

## 3. Prefix-Cache `1/N` Dilution & Zero-Fabrication Pareto Frontier

- **Observed Prefix-Cache Hit Ratio (`{dil.apphub_application_id}`):** `{dil.observed_cache_hit_ratio * 100:.1f}%` across `{dil.distinct_prompt_variants}` prompt variants (`1/N` ceiling `{dil.theoretical_dilution_ceiling * 100:.1f}%`)
- **Action:** {dil.recommendation}
- **Recommended Default Routing (`fabrication_rate = 0.0%` Gate):** {rec_line}

## 4. FOCUS 1.2 Two-Pool Chargeback & Off-Peak PT Water-Filling

| Allocation Owner | Cost Center | Weighted Burndown | Share | Utilized PT | Idle Capacity Tax | PAYG Overflow | Total Allocated |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
{focus_lines}

- **Off-Peak PT Batch-Eval Coverage:** `{artifacts.off_peak_plan.zero_marginal_cost_coverage_pct:.1f}%` of `{artifacts.off_peak_plan.total_batch_eval_tokens_requested:,.0f}` batch eval tokens scheduled into zero-marginal-cost PT valleys (saving `${artifacts.off_peak_plan.avoided_payg_cost_usd:.2f}`).
{live_section}"""
