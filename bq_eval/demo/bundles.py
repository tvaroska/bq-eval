"""Scenario corpus builders and fail-closed SHA-256 receipt loaders."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bq_eval.core.contract.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.resources import resolve_packaged_data
from bq_eval.demo.contract.results import ScenarioCorpusBundle
from bq_eval.demo.loadgen import generate_time_shifted_corpus

DEFAULT_M2_RECEIPT_PATH = resolve_packaged_data("benchmarks/results/m2_live_receipt.json")
DEFAULT_M2_SHA256_PATH = resolve_packaged_data("benchmarks/results/SHA256SUMS")


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
    """Verify `receipt_path` SHA-256 digest against `SHA256SUMS` (fail-closed if manifest/entry missing)."""
    if not receipt_path.is_file():
        raise ValueError(f"Scenario receipt file not found: {receipt_path}")
    if not manifest_path.is_file():
        raise ValueError(f"SHA-256 manifest file not found: {manifest_path}")
    actual_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    expected_map: dict[str, str] = {}
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            expected_map[parts[1].lstrip("*")] = parts[0]
    expected_sha = expected_map.get(receipt_path.name)
    if expected_sha is None:
        raise ValueError(
            f"SHA-256 manifest {manifest_path.name} has no entry for {receipt_path.name}"
        )
    if actual_sha != expected_sha:
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
