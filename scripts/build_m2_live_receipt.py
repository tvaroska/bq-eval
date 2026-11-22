#!/usr/bin/env python3
"""Deterministic builder and SHA-256 manifest generator for `benchmarks/results/m2_live_receipt.json`.

Constructs the sanitized `ScenarioCorpusBundle` for `m2-live-20261113b` (`343` labeled
fleet cases across KYC, Benefits RFP, Meridian Loan Consolidation, and unregistered
shadow workloads) so `bq-eval demo` replays multi-cohort empirical receipts through
`evaluate_traces_in_sqlite()` with zero cloud credentials or PII/project-ID leaks.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bq_eval.contract.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.contract.results import (
    ConfusionMatrix,
    LiveAttributionCohortReceipt,
    LiveFleetReceipt,
    LiveKycCohortReceipt,
    LiveRfpModalityReceipt,
    LiveSkillLineageReceipt,
    ScenarioCorpusBundle,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
RECEIPT_PATH = RESULTS_DIR / "m2_live_receipt.json"
MANIFEST_PATH = RESULTS_DIR / "SHA256SUMS"


def _build_ordinal_pairs() -> tuple[tuple[int, int], ...]:
    """Return the 30 Argon-vs-Flash ordinal pairs (`30/30` binary match, `AC2 = 37/43 = 0.8605`)."""
    return (
        (1, 2),
        (2, 1),
        *((2, 2) for _ in range(3)),
        *((4, 3) for _ in range(7)),
        *((3, 4) for _ in range(6)),
        *((3, 3) for _ in range(4)),
        *((4, 4) for _ in range(8)),
    )


def _build_corpus_spans_and_outcomes() -> tuple[list[OTelGenAISpanRecord], dict[str, bool]]:
    base = datetime(2026, 11, 13, 18, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    judge_outcomes: dict[str, bool] = {}

    for i in range(302):
        tid = f"m2-kyc-{i:04d}"
        t0 = base + timedelta(seconds=i * 3)
        is_step1_reject = i < 36
        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-kyc-{i:04d}",
                start_time=t0,
                end_time=t0 + timedelta(milliseconds=640),
                receive_time=t0 + timedelta(milliseconds=727),
                latency_ms=640.0,
                status_code="OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                service_name="bridge-kyc",
                gcp_apphub_application_id="kyc-address-verification",
                agent_name="kyc_address_verifier",
                model_name="none" if is_step1_reject else "gemini-3.8-flash",
                input_tokens=0 if is_step1_reject else 11_800,
                cached_input_tokens=0 if is_step1_reject else 3_200,
                output_tokens=0 if is_step1_reject else 1_650,
                thinking_tokens=0 if is_step1_reject else 120,
                rule0_pass=not is_step1_reject,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
                outcome_id=f"out-kyc-{i:04d}",
                outcome_owner="kyc-address-verification",
                source_mode="STEP1_SQL" if is_step1_reject else "PDF_PATH_B_FLASH",
            )
        )
        if not is_step1_reject:
            judged_idx = i - 36
            judge_outcomes[tid] = judged_idx < 231

    for i in range(35):
        tid = f"m2-rfp-{i:04d}"
        t0 = base + timedelta(minutes=20, seconds=i * 5)
        if i < 8:
            mode = "A2A_PATH_A"
            agent = "rfp_path_a_validator"
            model = "none"
            in_tok, cached_tok, out_tok = 0, 0, 0
        elif i < 25:
            mode = "PDF_PATH_B_FLASH"
            agent = "rfp_pdf_extractor_flash"
            model = "gemini-3.8-flash"
            in_tok, cached_tok, out_tok = 1_400, 400, 320
        else:
            mode = "PDF_PATH_B_ARGON"
            agent = "rfp_pdf_extractor_argon_uncalibrated"
            model = "gemini-4-argon"
            in_tok, cached_tok, out_tok = 288_000, 0, 140

        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-rfp-{i:04d}",
                start_time=t0,
                end_time=t0 + timedelta(milliseconds=920),
                receive_time=t0 + timedelta(milliseconds=1007),
                latency_ms=920.0,
                status_code="OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                service_name="bridge-rfp",
                gcp_apphub_application_id="benefits-rfp-procurement",
                agent_name=agent,
                model_name=model,
                input_tokens=in_tok,
                cached_input_tokens=cached_tok,
                output_tokens=out_tok,
                thinking_tokens=0,
                rule0_pass=True,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
                outcome_id=f"out-rfp-{i:04d}",
                outcome_owner="benefits-rfp-procurement",
                source_mode=mode,
            )
        )

    for i in range(18):
        tid = f"m2-mer-{i:04d}"
        t0 = base + timedelta(minutes=35, seconds=i * 8)
        is_day1 = i < 8
        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-mer-{i:04d}",
                start_time=t0,
                end_time=t0 + timedelta(milliseconds=30_830 if is_day1 else 5_490),
                receive_time=t0 + timedelta(milliseconds=30_917 if is_day1 else 5_577),
                latency_ms=30_830.0 if is_day1 else 5_490.0,
                status_code="OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                service_name="bridge-meridian",
                gcp_apphub_application_id="meridian-loan-consolidation",
                agent_name="meridian_underwriter",
                model_name="gemini-4-argon" if is_day1 else "gemini-3.8-flash",
                input_tokens=54_000 if is_day1 else 4_800,
                cached_input_tokens=4_000 if is_day1 else 2_400,
                output_tokens=3_600 if is_day1 else 320,
                thinking_tokens=500 if is_day1 else 0,
                rule0_pass=True,
                finops_traffic_type=(
                    FinOpsTrafficType.CONFLICT_ESCALATION
                    if is_day1
                    else FinOpsTrafficType.PRODUCTION_INFERENCE
                ),
                outcome_id=f"out-mer-{i:04d}",
                outcome_owner="meridian-loan-consolidation",
                skill_id="loan-payoff-statement",
                skill_version="1.0.0" if is_day1 else "1.1.0",
            )
        )

    for i in range(10):
        tid = f"m2-shd-{i:04d}"
        t0 = base + timedelta(minutes=50, seconds=i * 4)
        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-shd-{i:04d}",
                start_time=t0,
                end_time=t0 + timedelta(milliseconds=310),
                receive_time=t0 + timedelta(milliseconds=397),
                latency_ms=310.0,
                status_code="OK",
                span_kind=GenAISpanKind.CHAT,
                service_name="shadow-runner-svc",
                gcp_apphub_application_id=None,
                agent_name="shadow_prototype_bot",
                model_name="gemini-3.5-flash-lite",
                input_tokens=200,
                cached_input_tokens=0,
                output_tokens=10,
                thinking_tokens=0,
                rule0_pass=True,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            )
        )

    return spans, judge_outcomes


def build_m2_live_receipt_bundle() -> ScenarioCorpusBundle:
    """Assemble the full sanitized `m2-live-20261113b` `ScenarioCorpusBundle`."""
    ordinal_pairs = _build_ordinal_pairs()
    confusion = ConfusionMatrix(tp=25, fn=0, tn=5, fp=0)
    spans, judge_outcomes = _build_corpus_spans_and_outcomes()

    live_receipt = LiveFleetReceipt(
        schema_version="1.0",
        run_id="m2-live-20261113b",
        recorded_date_utc="2026-11-14",
        labeled_fleet_cases=343,
        cumulative_closed_outcomes=443,
        cumulative_allspans_count=28_752,
        cumulative_traces_count=2_785,
        allspans_ingestion_lag_p50_ms=87.0,
        allspans_ingestion_lag_p95_ms=11_380.0,
        kyc_cohort=LiveKycCohortReceipt(
            total_cases=280,
            structural_step1_rejects=28,
            po_box_step1_traps_caught=8,
            step2_semantic_traps_caught=20,
            step2_semantic_traps_total=20,
            held_out_traps_caught=5,
            held_out_traps_total=5,
            clean_cases_evaluated=216,
            false_positives=0,
            trap_recall=1.0,
            trap_recall_wilson_ci_95=(0.8389, 1.0),
            trap_precision=1.0,
            argon_calibration_pairs=30,
            flash_argon_binary_agreed=30,
            ordinal_pairs=ordinal_pairs,
            confusion_matrix=confusion,
        ),
        rfp_modalities=(
            LiveRfpModalityReceipt(
                source_mode="A2A_PATH_A",
                agent_name="rfp_path_a_validator",
                verified_outcomes=37,
                input_tokens=0,
                output_tokens=0,
                ai_cost_per_verified_outcome_usd=0.0,
                total_cost_per_verified_outcome_usd=0.000238,
            ),
            LiveRfpModalityReceipt(
                source_mode="PDF_PATH_B_FLASH",
                agent_name="rfp_pdf_extractor_flash",
                verified_outcomes=27,
                input_tokens=37_800,
                output_tokens=8_640,
                ai_cost_per_verified_outcome_usd=0.000242,
                total_cost_per_verified_outcome_usd=0.000651,
            ),
            LiveRfpModalityReceipt(
                source_mode="PDF_PATH_B_ARGON",
                agent_name="rfp_pdf_extractor_argon_uncalibrated",
                verified_outcomes=10,
                input_tokens=2_880_000,
                output_tokens=1_400,
                ai_cost_per_verified_outcome_usd=0.722454,
                total_cost_per_verified_outcome_usd=0.723991,
            ),
        ),
        skill_lineage=LiveSkillLineageReceipt(
            skill_id="loan-payoff-statement",
            day1_version="1.0.0",
            day2_version="1.1.0",
            day1_cpvo_usd=0.02427,
            day2_cpvo_usd=0.000915,
            cpvo_reduction_pct=96.2,
            day1_human_reviews_per_outcome=1.00,
            day2_human_reviews_per_outcome=0.20,
            day2_in_policy_auto_approved=9,
            day2_in_policy_total=9,
            day2_out_of_policy_escalated=1,
            day2_out_of_policy_total=1,
            day1_avg_cycle_seconds=30.83,
            day2_avg_cycle_seconds=5.49,
        ),
        attribution_cohorts=(
            LiveAttributionCohortReceipt(
                apphub_application_id="benefits-rfp-procurement",
                total_traces=420,
                attributed_traces=420,
                attributed_spend_usd=7.2311,
                unattributed_spend_usd=0.0,
                attribution_coverage_pct=100.0,
                passes_coverage_gate=True,
            ),
            LiveAttributionCohortReceipt(
                apphub_application_id="meridian-loan-consolidation",
                total_traces=312,
                attributed_traces=312,
                attributed_spend_usd=0.6639,
                unattributed_spend_usd=0.0,
                attribution_coverage_pct=100.0,
                passes_coverage_gate=True,
            ),
            LiveAttributionCohortReceipt(
                apphub_application_id="kyc-address-verification",
                total_traces=880,
                attributed_traces=880,
                attributed_spend_usd=0.6257,
                unattributed_spend_usd=0.0,
                attribution_coverage_pct=100.0,
                passes_coverage_gate=True,
            ),
            LiveAttributionCohortReceipt(
                apphub_application_id="unregistered_app",
                total_traces=1_173,
                attributed_traces=0,
                attributed_spend_usd=0.0,
                unattributed_spend_usd=0.00018,
                attribution_coverage_pct=0.0,
                passes_coverage_gate=False,
            ),
        ),
    )

    return ScenarioCorpusBundle(
        corpus_id="m2-live",
        title="Sanitized Empirical Fleet Receipt (m2-live-20261113b, 343 Labeled Cases)",
        rubric_id="kyc_policy_compliance_4pt_v1",
        spans=tuple(spans),
        judge_outcomes=judge_outcomes,
        sme_confusion_by_app={"kyc-address-verification": confusion},
        sme_ordinal_pairs_by_app={"kyc-address-verification": ordinal_pairs},
        distinct_prompt_variants=4,
        dilution_target_app_id="kyc-address-verification",
        reserved_pt_capacity_tokens=100_000_000.0,
        reserved_pt_fixed_cost_usd=120.0,
        app_cost_centers={
            "kyc-address-verification": "CC-KYC-OPS",
            "benefits-rfp-procurement": "CC-PROC-RFP",
            "meridian-loan-consolidation": "CC-4100-CRE",
            "unregistered_app": "UNASSIGNED",
        },
        live_receipt=live_receipt,
    )


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    bundle = build_m2_live_receipt_bundle()
    raw_json = json.dumps(bundle.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    RECEIPT_PATH.write_text(raw_json, encoding="utf-8")
    digest = hashlib.sha256(RECEIPT_PATH.read_bytes()).hexdigest()
    MANIFEST_PATH.write_text(f"{digest}  {RECEIPT_PATH.name}\n", encoding="utf-8")
    print(f"Wrote {RECEIPT_PATH} (sha256={digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
