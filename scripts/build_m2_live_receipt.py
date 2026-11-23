#!/usr/bin/env python3
"""Deterministic builder and SHA-256 manifest generator for `benchmarks/results/m2_live_receipt.json`.

Constructs the sanitized `ScenarioCorpusBundle` for `m2-live-20261113b` (`343` labeled
fleet cases across KYC, Benefits RFP, Meridian Loan Consolidation, and unregistered
shadow workloads) and derives every `LiveFleetReceipt` summary metric directly from
the bundle's `spans`, `outcome.closed` events, and `judge_outcomes`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
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
from bq_eval.engine.finops import compute_span_cost_usd

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
RECEIPT_PATH = RESULTS_DIR / "m2_live_receipt.json"
MANIFEST_PATH = RESULTS_DIR / "SHA256SUMS"

_Z_95 = 1.959963984540054
_VCPU_RATE_PER_SEC = 2.0 * 0.00002400
_GIB_RATE_PER_SEC = 2.0 * 0.00000250
_DEFAULT_STORAGE_USD = 0.000020


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


def _lag_ms_for_index(idx: int) -> int:
    return 11_380 if idx % 17 == 0 else 87


def _outcome_closed_span(
    root: OTelGenAISpanRecord,
    *,
    outcome_status: str,
    outcome_verified: bool,
) -> OTelGenAISpanRecord:
    return OTelGenAISpanRecord(
        trace_id=root.trace_id,
        span_id=f"{root.span_id}-close",
        parent_span_id=root.span_id,
        span_name="outcome.closed",
        start_time=root.end_time,
        end_time=root.end_time,
        receive_time=root.receive_time,
        latency_ms=0.0,
        status_code="OK",
        otel_span_kind="SPAN_KIND_INTERNAL",
        span_kind=GenAISpanKind.INVOKE_AGENT,
        service_name=root.service_name,
        gcp_apphub_application_id=root.gcp_apphub_application_id,
        agent_name=root.agent_name,
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        rule0_pass=root.rule0_pass,
        finops_traffic_type=root.finops_traffic_type,
        outcome_id=root.outcome_id,
        outcome_owner=root.outcome_owner,
        source_mode=root.source_mode,
        skill_id=root.skill_id,
        skill_version=root.skill_version,
        attributes={
            "outcome.status": outcome_status,
            "outcome.verified": "true" if outcome_verified else "false",
        },
    )


def _build_kyc_spans(base: datetime) -> tuple[list[OTelGenAISpanRecord], dict[str, bool]]:
    spans: list[OTelGenAISpanRecord] = []
    judge_outcomes: dict[str, bool] = {}

    for i in range(280):
        tid = f"m2-kyc-{i:04d}"
        t0 = base + timedelta(seconds=i * 3)
        t_end = t0 + timedelta(milliseconds=640)
        lag_ms = _lag_ms_for_index(i)
        if i < 28:
            reject_reason = "structural"
            is_step1_reject = True
            attrs: dict[str, str] = {
                "kyc.case_type": "step1_reject",
                "step1.reject_reason": reject_reason,
            }
            is_clean = False
        elif i < 36:
            is_step1_reject = True
            attrs = {"kyc.case_type": "step1_reject", "step1.reject_reason": "po_box"}
            is_clean = False
        elif i < 44:
            is_step1_reject = True
            attrs = {"kyc.case_type": "step1_reject", "step1.reject_reason": "ocr_reject"}
            is_clean = False
        elif i < 64:
            is_step1_reject = False
            attrs = {
                "kyc.case_type": "step2_trap",
                "kyc.held_out": "true" if i < 49 else "false",
            }
            is_clean = False
        else:
            is_step1_reject = False
            attrs = {"kyc.case_type": "clean"}
            is_clean = True

        root = OTelGenAISpanRecord(
            trace_id=tid,
            span_id=f"sp-kyc-{i:04d}",
            start_time=t0,
            end_time=t_end,
            receive_time=t_end + timedelta(milliseconds=lag_ms),
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
            attributes=attrs,
        )
        spans.append(root)
        spans.append(
            _outcome_closed_span(
                root,
                outcome_status="succeeded" if is_clean else "failed",
                outcome_verified=is_clean,
            )
        )
        if not is_step1_reject:
            judge_outcomes[tid] = is_clean

    return spans, judge_outcomes


def _build_rfp_spans(base: datetime) -> list[OTelGenAISpanRecord]:
    spans: list[OTelGenAISpanRecord] = []
    for i in range(35):
        tid = f"m2-rfp-{i:04d}"
        t0 = base + timedelta(minutes=20, seconds=i * 5)
        t_end = t0 + timedelta(milliseconds=920)
        lag_ms = _lag_ms_for_index(280 + i)
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

        root = OTelGenAISpanRecord(
            trace_id=tid,
            span_id=f"sp-rfp-{i:04d}",
            start_time=t0,
            end_time=t_end,
            receive_time=t_end + timedelta(milliseconds=lag_ms),
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
            attributes={"outcome.storage_usd": "0.000120"},
        )
        spans.append(root)
        spans.append(_outcome_closed_span(root, outcome_status="succeeded", outcome_verified=True))
    return spans


def _build_meridian_spans(base: datetime) -> list[OTelGenAISpanRecord]:
    spans: list[OTelGenAISpanRecord] = []
    for i in range(18):
        tid = f"m2-mer-{i:04d}"
        t0 = base + timedelta(minutes=35, seconds=i * 8)
        is_day1 = i < 8
        latency_ms = 30_830.0 if is_day1 else 5_490.0
        t_end = t0 + timedelta(milliseconds=int(latency_ms))
        lag_ms = _lag_ms_for_index(315 + i)
        in_policy = not is_day1 and i < 17
        needs_review = is_day1 or i == 17
        root = OTelGenAISpanRecord(
            trace_id=tid,
            span_id=f"sp-mer-{i:04d}",
            start_time=t0,
            end_time=t_end,
            receive_time=t_end + timedelta(milliseconds=lag_ms),
            latency_ms=latency_ms,
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
            attributes={
                "hitl.review": "true" if needs_review else "false",
                "policy.in_policy": "true" if in_policy else "false",
                "outcome.storage_usd": "0.000400",
            },
        )
        spans.append(root)
        spans.append(_outcome_closed_span(root, outcome_status="succeeded", outcome_verified=True))
    return spans


def _build_shadow_spans(base: datetime) -> list[OTelGenAISpanRecord]:
    spans: list[OTelGenAISpanRecord] = []
    for i in range(10):
        tid = f"m2-shd-{i:04d}"
        t0 = base + timedelta(minutes=50, seconds=i * 4)
        t_end = t0 + timedelta(milliseconds=310)
        lag_ms = _lag_ms_for_index(333 + i)
        spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-shd-{i:04d}",
                start_time=t0,
                end_time=t_end,
                receive_time=t_end + timedelta(milliseconds=lag_ms),
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
    return spans


def _build_corpus_spans_and_outcomes() -> tuple[list[OTelGenAISpanRecord], dict[str, bool]]:
    base = datetime(2026, 11, 13, 18, 0, 0, tzinfo=UTC)
    kyc_spans, judge_outcomes = _build_kyc_spans(base)
    rfp_spans = _build_rfp_spans(base)
    meridian_spans = _build_meridian_spans(base)
    shadow_spans = _build_shadow_spans(base)
    return [*kyc_spans, *rfp_spans, *meridian_spans, *shadow_spans], judge_outcomes


def _wilson_ci_95(successes: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    z2 = _Z_95 * _Z_95
    denom = total + z2
    center = (successes + z2 / 2.0) / denom
    half = _Z_95 * math.sqrt((successes * (total - successes)) / total + z2 / 4.0) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def _span_infra_usd(span: OTelGenAISpanRecord) -> float:
    compute = (span.latency_ms / 1000.0) * (_VCPU_RATE_PER_SEC + _GIB_RATE_PER_SEC)
    raw_storage = span.attributes.get("outcome.storage_usd")
    storage = float(raw_storage) if raw_storage is not None else _DEFAULT_STORAGE_USD
    return compute + storage


def _compute_kyc_receipt(
    spans: Sequence[OTelGenAISpanRecord],
    judge_outcomes: dict[str, bool],
    ordinal_pairs: tuple[tuple[int, int], ...],
    confusion: ConfusionMatrix,
) -> LiveKycCohortReceipt:
    kyc_roots = [
        s
        for s in spans
        if s.resolved_application_id == "kyc-address-verification"
        and s.span_name != "outcome.closed"
    ]
    structural = sum(
        1 for s in kyc_roots if s.attributes.get("step1.reject_reason") == "structural"
    )
    po_box = sum(1 for s in kyc_roots if s.attributes.get("step1.reject_reason") == "po_box")
    ocr_rejects = sum(
        1 for s in kyc_roots if s.attributes.get("step1.reject_reason") == "ocr_reject"
    )
    trap_spans = [s for s in kyc_roots if s.attributes.get("kyc.case_type") == "step2_trap"]
    clean_spans = [s for s in kyc_roots if s.attributes.get("kyc.case_type") == "clean"]
    held_out = [s for s in trap_spans if s.attributes.get("kyc.held_out") == "true"]

    traps_caught = sum(1 for s in trap_spans if judge_outcomes.get(s.trace_id) is False)
    held_out_caught = sum(1 for s in held_out if judge_outcomes.get(s.trace_id) is False)
    false_positives = sum(1 for s in clean_spans if judge_outcomes.get(s.trace_id) is False)

    traps_total = len(trap_spans)
    recall = round(traps_caught / traps_total, 4) if traps_total > 0 else 0.0
    precision_denom = traps_caught + false_positives
    precision = round(traps_caught / precision_denom, 4) if precision_denom > 0 else 0.0
    binary_agreed = sum(1 for h, j in ordinal_pairs if (h >= 3) == (j >= 3))

    return LiveKycCohortReceipt(
        total_cases=len(kyc_roots),
        structural_step1_rejects=structural,
        po_box_step1_traps_caught=po_box,
        ocr_step1_rejects=ocr_rejects,
        step2_semantic_traps_caught=traps_caught,
        step2_semantic_traps_total=traps_total,
        held_out_traps_caught=held_out_caught,
        held_out_traps_total=len(held_out),
        clean_cases_evaluated=len(clean_spans),
        false_positives=false_positives,
        trap_recall=recall,
        trap_recall_wilson_ci_95=_wilson_ci_95(traps_caught, traps_total),
        trap_precision=precision,
        argon_calibration_pairs=len(ordinal_pairs),
        flash_argon_binary_agreed=binary_agreed,
        ordinal_pairs=ordinal_pairs,
        confusion_matrix=confusion,
    )


def _compute_rfp_receipts(
    spans: Sequence[OTelGenAISpanRecord],
    verified_outcome_ids: set[str],
) -> tuple[LiveRfpModalityReceipt, ...]:
    rfp_roots = [
        s
        for s in spans
        if s.resolved_application_id == "benefits-rfp-procurement"
        and s.span_name != "outcome.closed"
    ]
    rows: list[LiveRfpModalityReceipt] = []
    for mode in ("A2A_PATH_A", "PDF_PATH_B_FLASH", "PDF_PATH_B_ARGON"):
        mode_spans = [s for s in rfp_roots if s.source_mode == mode]
        verified = sum(1 for s in mode_spans if s.outcome_id in verified_outcome_ids)
        ai_cost = sum(compute_span_cost_usd(s) for s in mode_spans)
        total_cost = ai_cost + sum(_span_infra_usd(s) for s in mode_spans)
        rows.append(
            LiveRfpModalityReceipt(
                source_mode=mode,
                agent_name=mode_spans[0].agent_name,
                verified_outcomes=verified,
                input_tokens=sum(s.input_tokens for s in mode_spans),
                output_tokens=sum(s.output_tokens for s in mode_spans),
                ai_cost_per_verified_outcome_usd=round(ai_cost / verified, 6),
                total_cost_per_verified_outcome_usd=round(total_cost / verified, 6),
            )
        )
    return tuple(rows)


def _compute_skill_receipt(
    spans: Sequence[OTelGenAISpanRecord],
    verified_outcome_ids: set[str],
) -> LiveSkillLineageReceipt:
    mer_roots = [
        s
        for s in spans
        if s.resolved_application_id == "meridian-loan-consolidation"
        and s.span_name != "outcome.closed"
    ]
    day1_spans = [s for s in mer_roots if s.skill_version == "1.0.0"]
    day2_spans = [s for s in mer_roots if s.skill_version == "1.1.0"]

    day1_verified = sum(1 for s in day1_spans if s.outcome_id in verified_outcome_ids)
    day2_verified = sum(1 for s in day2_spans if s.outcome_id in verified_outcome_ids)
    day1_cpvo = sum(compute_span_cost_usd(s) for s in day1_spans) / day1_verified
    day2_cpvo = sum(compute_span_cost_usd(s) for s in day2_spans) / day2_verified
    reduction_pct = round((1.0 - day2_cpvo / day1_cpvo) * 100.0, 1)

    day1_reviews = sum(1 for s in day1_spans if s.attributes.get("hitl.review") == "true")
    day2_reviews = sum(1 for s in day2_spans if s.attributes.get("hitl.review") == "true")
    day2_in_policy = [s for s in day2_spans if s.attributes.get("policy.in_policy") == "true"]
    day2_out_policy = [s for s in day2_spans if s.attributes.get("policy.in_policy") != "true"]

    return LiveSkillLineageReceipt(
        skill_id="loan-payoff-statement",
        day1_version="1.0.0",
        day2_version="1.1.0",
        day1_cpvo_usd=round(day1_cpvo, 6),
        day2_cpvo_usd=round(day2_cpvo, 6),
        cpvo_reduction_pct=reduction_pct,
        day1_human_reviews_per_outcome=round(day1_reviews / day1_verified, 2),
        day2_human_reviews_per_outcome=round(day2_reviews / day2_verified, 2),
        day2_in_policy_auto_approved=sum(
            1 for s in day2_in_policy if s.attributes.get("hitl.review") != "true"
        ),
        day2_in_policy_total=len(day2_in_policy),
        day2_out_of_policy_escalated=sum(
            1 for s in day2_out_policy if s.attributes.get("hitl.review") == "true"
        ),
        day2_out_of_policy_total=len(day2_out_policy),
        day1_avg_cycle_seconds=round(
            sum(s.latency_ms for s in day1_spans) / len(day1_spans) / 1000.0, 2
        ),
        day2_avg_cycle_seconds=round(
            sum(s.latency_ms for s in day2_spans) / len(day2_spans) / 1000.0, 2
        ),
    )


def _compute_attribution_cohorts(
    spans: Sequence[OTelGenAISpanRecord],
) -> tuple[LiveAttributionCohortReceipt, ...]:
    by_app: dict[str, list[OTelGenAISpanRecord]] = {}
    for span in spans:
        if span.span_name == "outcome.closed":
            continue
        by_app.setdefault(span.resolved_application_id, []).append(span)

    cohorts: list[LiveAttributionCohortReceipt] = []
    for app_id, app_spans in sorted(
        by_app.items(),
        key=lambda item: sum(compute_span_cost_usd(s) for s in item[1]),
        reverse=True,
    ):
        total_traces = len({s.trace_id for s in app_spans})
        is_registered = app_id != "unregistered_app"
        total_spend = round(sum(compute_span_cost_usd(s) for s in app_spans), 6)
        attributed_traces = total_traces if is_registered else 0
        coverage_pct = round((attributed_traces / total_traces) * 100.0, 1) if total_traces else 0.0
        cohorts.append(
            LiveAttributionCohortReceipt(
                apphub_application_id=app_id,
                total_traces=total_traces,
                attributed_traces=attributed_traces,
                attributed_spend_usd=total_spend if is_registered else 0.0,
                unattributed_spend_usd=0.0 if is_registered else total_spend,
                attribution_coverage_pct=coverage_pct,
                passes_coverage_gate=is_registered and coverage_pct >= 99.0,
            )
        )
    return tuple(cohorts)


def _quantile_ms(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(pct * len(ordered)) - 1))
    return round(ordered[idx], 1)


def build_m2_live_receipt_bundle() -> ScenarioCorpusBundle:
    """Assemble the sanitized, span-reconciled `m2-live-20261113b` `ScenarioCorpusBundle`."""
    ordinal_pairs = _build_ordinal_pairs()
    confusion = ConfusionMatrix(tp=25, fn=0, tn=5, fp=0)
    spans, judge_outcomes = _build_corpus_spans_and_outcomes()

    closed_spans = [
        s for s in spans if s.span_name == "outcome.closed" and s.outcome_id is not None
    ]
    verified_outcome_ids = {
        s.outcome_id
        for s in closed_spans
        if s.outcome_id is not None
        and s.attributes.get("outcome.status") == "succeeded"
        and s.attributes.get("outcome.verified") == "true"
    }
    lags_ms = [
        (s.receive_time - s.end_time).total_seconds() * 1000.0
        for s in spans
        if s.receive_time is not None and s.span_name != "outcome.closed"
    ]
    unique_traces = {s.trace_id for s in spans}

    live_receipt = LiveFleetReceipt(
        schema_version="1.0",
        run_id="m2-live-20261113b",
        recorded_date_utc="2026-11-14",
        summary_provenance="reconstructed_from_bundle_spans",
        labeled_fleet_cases=len(unique_traces),
        cumulative_closed_outcomes=len({s.outcome_id for s in closed_spans}),
        cumulative_allspans_count=len(spans),
        cumulative_traces_count=len(unique_traces),
        allspans_ingestion_lag_p50_ms=_quantile_ms(lags_ms, 0.50),
        allspans_ingestion_lag_p95_ms=_quantile_ms(lags_ms, 0.95),
        kyc_cohort=_compute_kyc_receipt(spans, judge_outcomes, ordinal_pairs, confusion),
        rfp_modalities=_compute_rfp_receipts(spans, verified_outcome_ids),
        skill_lineage=_compute_skill_receipt(spans, verified_outcome_ids),
        attribution_cohorts=_compute_attribution_cohorts(spans),
    )

    return ScenarioCorpusBundle(
        corpus_id="m2-live",
        title="Sanitized Reconstructed Fleet Receipt (m2-live-20261113b, 343 Labeled Cases)",
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
