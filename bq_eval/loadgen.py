"""Capture-Once, Time-Shift & Multiply Demo Load Generator (`bq_eval.loadgen`).

Provides:
1. A Golden Captured Trace Corpus (`build_golden_seed_templates()`) spanning 4 enterprise
   agent workloads across ADK, LangGraph, FastMCP, Agent Engine, and Cloud Run:
   - `docubridge-underwriting` (`commercial_banking` / `CC-4100-CRE`): High-cost multimodal
     PDF extraction (`>200K` context cliff + `gemini-4-argon` conflict escalation).
   - `quoting-concierge-adk` (`personal_lines` / `CC-2100-PERS`): Google ADK on Agent Engine
     + FastMCP, including an ungrounded discount hallucination for Step 2 `groundedness_4pt_v1`.
   - `claims-triage-langgraph` (`claims_ops` / `CC-3200-CLM`): LangGraph on Cloud Run + FastMCP,
     including FNOL, attorney escalation, E&O backdate trap (`escalation_policy_4pt_v1`), and
     an MCP 503 timeout caught at $0 by Step 1 SQL.
   - `renewal-auditor-svc` (`gcp_apphub_application_id = None`): Unregistered Cloud Run agent
     proving Step 1 `COALESCE` fallback (`unassigned_bu`) and `>8` tool-call loop detection.
2. `generate_time_shifted_corpus()`: Shifts and multiplies the golden templates into the
   current day and the last 1 hour while preserving parent-child span waterfalls.
3. `evaluate_time_shifted_corpus()`: Runs Step 1 Cost per Application/BU/Outcome and Step 2/2.5
   slice counts (`100%`, `10%`, `5%`, `last 1 hour`) over the time-shifted traces.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from bq_eval.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.sql_eval import DEFAULT_SKU_RATES

DEFAULT_SEED_DATASET_ID: str = "agent_eval_seed"


class GoldenSpanTemplate(BaseModel):
    """Single span template with relative millisecond offset from its trace root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    span_suffix: str
    parent_span_suffix: str | None = None
    span_offset_ms: int = 0
    latency_ms: float
    status_code: str = "OK"
    span_kind: GenAISpanKind = GenAISpanKind.INVOKE_AGENT
    service_name: str
    gcp_apphub_application_id: str | None = None
    agent_name: str
    model_name: str = "gemini-3.8-flash"
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    tool_name: str | None = None
    tool_call_status: str | None = None
    rule0_pass: bool = True
    finops_traffic_type: FinOpsTrafficType = FinOpsTrafficType.PRODUCTION_INFERENCE
    attributes: dict[str, str]


class GoldenTraceTemplate(BaseModel):
    """Captured multi-span trace template and its associated prompt/context/response payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_idx: int
    base_trace_id: str
    apphub_application_id: str
    user_prompt: str
    retrieved_context: str
    final_response: str
    semantic_rubric_id: str = "groundedness_4pt_v1"
    expected_ordinal_score: int = 4
    spans: tuple[GoldenSpanTemplate, ...]


class TracePayloadRecord(BaseModel):
    """Time-shifted prompt, retrieved context, and final response payload row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str
    apphub_application_id: str
    ingested_at: datetime
    user_prompt: str
    retrieved_context: str
    final_response: str
    semantic_rubric_id: str = "groundedness_4pt_v1"
    expected_ordinal_score: int = 4


class Step1CostOutcomeRow(BaseModel):
    """Step 1 Cost per Application, Business Unit & Outcome summary row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    business_unit: str
    cost_center: str
    framework_runtime: str
    total_traces: int
    last_1h_traces: int
    step1_passing_traces: int
    step1_pass_rate: float
    tool_success_rate: float
    p95_latency_ms: float
    avg_cache_hit_ratio: float
    total_cost_usd: float
    cost_per_trace_usd: float
    step1_cost_per_outcome_usd: float | None


class TimeShiftLoadgenReport(BaseModel):
    """Summary of a time-shifted demo load generation run across Step 1, Step 2, and Step 2.5."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_time: datetime
    total_replicas: int
    last_hour_replicas: int
    total_traces: int
    total_spans: int
    last_1h_traces: int
    sample_10pct_traces: int
    sample_5pct_traces: int
    last_1h_and_5pct_traces: int
    step1_rows: tuple[Step1CostOutcomeRow, ...]


def _deterministic_bucket_100(trace_id: str) -> int:
    """Deterministic [0..99] hash bucket mirroring `MOD(ABS(FARM_FINGERPRINT(trace_id)), 100)`."""
    digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return val % 100


def build_golden_seed_templates() -> tuple[GoldenTraceTemplate, ...]:
    """Return the 16 golden captured trace templates across 4 enterprise agent profiles."""
    docu_attrs = {
        "business_unit": "commercial_banking",
        "cost_center": "CC-4100-CRE",
        "framework": "ADK + LangGraph",
        "runtime": "Agent Engine + Cloud Run",
    }
    quote_attrs = {
        "business_unit": "personal_lines",
        "cost_center": "CC-2100-PERS",
        "framework": "Google ADK + FastMCP",
        "runtime": "Agent Engine",
    }
    claim_attrs = {
        "business_unit": "claims_ops",
        "cost_center": "CC-3200-CLM",
        "framework": "LangGraph + FastMCP",
        "runtime": "Cloud Run",
    }
    audit_attrs = {
        "framework": "LangGraph (Unregistered app.id)",
        "runtime": "Cloud Run",
    }

    return (
        # =====================================================================
        # App 1: docubridge-underwriting (High-Cost >200K PDF + Argon Escalation)
        # =====================================================================
        GoldenTraceTemplate(
            template_idx=1,
            base_trace_id="seed-docu-001",
            apphub_application_id="docubridge-underwriting",
            user_prompt=(
                "Consolidate the Meridian Logistics $45M CRE term loan package (2025 Audited "
                "Financials Note 6, MAI Appraisal, and Oct 2026 Operating Bank Statement) and "
                "verify LTV, DSCR, and open debt payoffs."
            ),
            retrieved_context=(
                "[DOC-01 Page 4]: Note 6 Liability Schedule: First National Loan #4402 "
                "($1,250,000), EquipLease #8841 ($48,210), On-Us LOC ($250,000 waived). "
                "[DOC-02 Page 1]: MAI Appraised Value $2,600,000. [DOC-04 Page 2]: Wire "
                "transfer $48,210.00 ref FINAL PAYOFF - LOAN #8841 - PAID IN FULL."
            ),
            final_response=(
                "Consolidated Meridian Logistics CRE facility ($1,800,000): Appraised value "
                "is $2,600,000 [DOC-02], yielding LTV = 69.23% and DSCR = 1.44x. EquipLease "
                "#8841 ($48,210) is verified paid in full via Oct 2026 wire confirmation "
                "[DOC-04] and recommended for underwriter waiver."
            ),
            semantic_rubric_id="groundedness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=6400.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="docubridge-a2a-bridge",
                    gcp_apphub_application_id="docubridge-underwriting",
                    agent_name="docubridge-root-adk",
                    model_name="gemini-3.8-flash",
                    input_tokens=240_000,
                    cached_input_tokens=180_000,
                    output_tokens=1_800,
                    thinking_tokens=600,
                    attributes=docu_attrs,
                ),
                GoldenSpanTemplate(
                    span_suffix="ocr",
                    parent_span_suffix="root",
                    span_offset_ms=120,
                    latency_ms=1450.0,
                    span_kind=GenAISpanKind.EXECUTE_TOOL,
                    service_name="docubridge-a2a-bridge",
                    gcp_apphub_application_id="docubridge-underwriting",
                    agent_name="docubridge-root-adk",
                    tool_name="mcp__docubridge__extract_pdf_anchors",
                    tool_call_status="SUCCESS",
                    attributes=docu_attrs,
                ),
                GoldenSpanTemplate(
                    span_suffix="esc",
                    parent_span_suffix="root",
                    span_offset_ms=1800,
                    latency_ms=4200.0,
                    span_kind=GenAISpanKind.CHAT,
                    service_name="docubridge-a2a-bridge",
                    gcp_apphub_application_id="docubridge-underwriting",
                    agent_name="docubridge-wire-reconciler",
                    model_name="gemini-4-argon",
                    input_tokens=48_000,
                    cached_input_tokens=32_000,
                    output_tokens=1_200,
                    thinking_tokens=2_400,
                    finops_traffic_type=FinOpsTrafficType.CONFLICT_ESCALATION,
                    attributes=docu_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=2,
            base_trace_id="seed-docu-002",
            apphub_application_id="docubridge-underwriting",
            user_prompt=(
                "Verify TRID Closing Disclosure Page 3 Section A origination charges and "
                "rate-lock extension addendum for Westbrook Family Trust ($1,850,000 Jumbo Mortgage)."
            ),
            retrieved_context=(
                "[DOC-TRID-CD Page 3]: Section A Origination Fee $4,250.00; Addendum B signed "
                "rate-lock extension fee $8,500.00; Form 1004 Appraisal $2,400,000 (LTV 77.08%)."
            ),
            final_response=(
                "Verified Westbrook Family Trust TRID Closing Disclosure: Addendum B supersedes "
                "the baseline fee to $8,500.00 [DOC-TRID-CD Page 3], and Form 1004 confirms "
                "77.08% LTV on the $1,850,000 loan."
            ),
            semantic_rubric_id="groundedness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=3100.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="docubridge-a2a-bridge",
                    gcp_apphub_application_id="docubridge-underwriting",
                    agent_name="carrier-a-adk",
                    model_name="gemini-3.8-flash",
                    input_tokens=68_000,
                    cached_input_tokens=52_000,
                    output_tokens=1_100,
                    thinking_tokens=300,
                    attributes=docu_attrs,
                ),
                GoldenSpanTemplate(
                    span_suffix="tool",
                    parent_span_suffix="root",
                    span_offset_ms=200,
                    latency_ms=620.0,
                    span_kind=GenAISpanKind.EXECUTE_TOOL,
                    service_name="docubridge-a2a-bridge",
                    gcp_apphub_application_id="docubridge-underwriting",
                    agent_name="carrier-a-adk",
                    tool_name="mcp__docubridge__trid_tolerance_check",
                    tool_call_status="SUCCESS",
                    attributes=docu_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=3,
            base_trace_id="seed-docu-003",
            apphub_application_id="docubridge-underwriting",
            user_prompt=(
                "Compare Carrier B commercial property and benefits quote schedules and flag "
                "any deductible discrepancies."
            ),
            retrieved_context=(
                "[DOC-QUOTE-B]: Property All-Risk Limit $15,000,000; Wind/Hail Deductible "
                "5% TIV ($750,000 minimum); Flood Sublimit $2,500,000."
            ),
            final_response=(
                "Carrier B provides a $15,000,000 All-Risk limit with a $2,500,000 Flood "
                "sublimit and a 5% Wind/Hail deductible ($750,000 minimum) [DOC-QUOTE-B]."
            ),
            semantic_rubric_id="groundedness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=2850.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="docubridge-carrier-b-cloudrun",
                    gcp_apphub_application_id="docubridge-underwriting",
                    agent_name="carrier-b-langgraph",
                    model_name="gemini-3.8-flash",
                    input_tokens=54_000,
                    cached_input_tokens=40_000,
                    output_tokens=950,
                    thinking_tokens=250,
                    attributes=docu_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=4,
            base_trace_id="seed-docu-004",
            apphub_application_id="docubridge-underwriting",
            user_prompt="Extract rent roll and environmental addendum from corrupted scan upload.",
            retrieved_context=(
                "[ERROR]: mcp__docubridge__extract_pdf_anchors returned VALIDATION_ERROR "
                "(encrypted/corrupted PDF stream)."
            ),
            final_response=(
                "Unable to complete extraction because the uploaded PDF stream failed OCR "
                "validation. Escalated to document intake queue."
            ),
            semantic_rubric_id="tool_trajectory_4pt_v1",
            expected_ordinal_score=2,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=1900.0,
                    status_code="ERROR",
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="docubridge-a2a-bridge",
                    gcp_apphub_application_id="docubridge-underwriting",
                    agent_name="docubridge-root-adk",
                    model_name="gemini-3.8-flash",
                    input_tokens=18_000,
                    cached_input_tokens=12_000,
                    output_tokens=220,
                    rule0_pass=False,
                    attributes=docu_attrs,
                ),
                GoldenSpanTemplate(
                    span_suffix="tool",
                    parent_span_suffix="root",
                    span_offset_ms=150,
                    latency_ms=1600.0,
                    status_code="ERROR",
                    span_kind=GenAISpanKind.EXECUTE_TOOL,
                    service_name="docubridge-a2a-bridge",
                    gcp_apphub_application_id="docubridge-underwriting",
                    agent_name="docubridge-root-adk",
                    tool_name="mcp__docubridge__extract_pdf_anchors",
                    tool_call_status="VALIDATION_ERROR",
                    rule0_pass=False,
                    attributes=docu_attrs,
                ),
            ),
        ),
        # =====================================================================
        # App 2: quoting-concierge-adk (Google ADK on Agent Engine + FastMCP)
        # =====================================================================
        GoldenTraceTemplate(
            template_idx=5,
            base_trace_id="seed-quote-001",
            apphub_application_id="quoting-concierge-adk",
            user_prompt=(
                "Compare multi-carrier auto quotes for client CLI-001 (Sarah Chen, CA) with "
                "100/300/100 liability limits."
            ),
            retrieved_context=(
                "[MCP lookup_client]: CLI-001 Sarah Chen, CA, 0 accidents. "
                "[MCP quote_multi_carrier]: Summit Fire $1,420/yr (100/300/100); "
                "Coastal Star $1,580/yr (100/300/100)."
            ),
            final_response=(
                "For Sarah Chen (CLI-001, CA) at 100/300/100 liability limits, Summit Fire "
                "offers the lowest annual premium at $1,420/yr compared to Coastal Star at $1,580/yr."
            ),
            semantic_rubric_id="groundedness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=920.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="quoting-adk-agent-engine",
                    gcp_apphub_application_id="quoting-concierge-adk",
                    agent_name="quoting-concierge",
                    model_name="gemini-3.8-flash",
                    input_tokens=14_000,
                    cached_input_tokens=10_500,
                    output_tokens=480,
                    thinking_tokens=120,
                    attributes=quote_attrs,
                ),
                GoldenSpanTemplate(
                    span_suffix="mcp1",
                    parent_span_suffix="root",
                    span_offset_ms=80,
                    latency_ms=210.0,
                    span_kind=GenAISpanKind.EXECUTE_TOOL,
                    service_name="quoting-adk-agent-engine",
                    gcp_apphub_application_id="quoting-concierge-adk",
                    agent_name="quoting-concierge",
                    tool_name="mcp__brokerage__lookup_client",
                    tool_call_status="SUCCESS",
                    attributes=quote_attrs,
                ),
                GoldenSpanTemplate(
                    span_suffix="mcp2",
                    parent_span_suffix="root",
                    span_offset_ms=310,
                    latency_ms=340.0,
                    span_kind=GenAISpanKind.EXECUTE_TOOL,
                    service_name="quoting-adk-agent-engine",
                    gcp_apphub_application_id="quoting-concierge-adk",
                    agent_name="quoting-concierge",
                    tool_name="mcp__brokerage__quote_multi_carrier",
                    tool_call_status="SUCCESS",
                    attributes=quote_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=6,
            base_trace_id="seed-quote-002",
            apphub_application_id="quoting-concierge-adk",
            user_prompt=(
                "Check carrier appetite for Maria Rodriguez (CLI-003, IL) home and auto renewal bundle."
            ),
            retrieved_context=(
                "[MCP check_appetite]: CLI-003 eligible for Summit Fire Preferred Bundle "
                "($2,890/yr combined, 12% multi-policy credit included)."
            ),
            final_response=(
                "Maria Rodriguez (CLI-003) qualifies for the Summit Fire Preferred Bundle at "
                "$2,890/yr combined, which includes a 12% multi-policy discount."
            ),
            semantic_rubric_id="groundedness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=840.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="quoting-adk-agent-engine",
                    gcp_apphub_application_id="quoting-concierge-adk",
                    agent_name="quoting-concierge",
                    model_name="gemini-3.8-flash",
                    input_tokens=12_500,
                    cached_input_tokens=9_000,
                    output_tokens=420,
                    thinking_tokens=100,
                    attributes=quote_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=7,
            base_trace_id="seed-quote-003",
            apphub_application_id="quoting-concierge-adk",
            user_prompt=(
                "Issue an ACORD 25 Certificate of Insurance for David Thompson (CLI-004, NY) "
                "showing $1M general liability."
            ),
            retrieved_context=(
                "[MCP generate_acord_25]: Policy POL-NY-402 active, General Liability Each "
                "Occurrence $1,000,000 / Aggregate $2,000,000. Certificate CERT-2026-881 generated."
            ),
            final_response=(
                "Generated ACORD 25 Certificate CERT-2026-881 for David Thompson (Policy "
                "POL-NY-402) verifying $1,000,000 Each Occurrence and $2,000,000 Aggregate "
                "General Liability limits."
            ),
            semantic_rubric_id="groundedness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=790.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="quoting-adk-agent-engine",
                    gcp_apphub_application_id="quoting-concierge-adk",
                    agent_name="quoting-concierge",
                    model_name="gemini-3.8-flash",
                    input_tokens=11_000,
                    cached_input_tokens=8_200,
                    output_tokens=390,
                    thinking_tokens=90,
                    attributes=quote_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=8,
            base_trace_id="seed-quote-004",
            apphub_application_id="quoting-concierge-adk",
            user_prompt=(
                "What is the lowest quote for Marcus Williams (CLI-006, TX) and are there "
                "any extra discounts?"
            ),
            retrieved_context=(
                "[MCP quote_multi_carrier]: Summit Fire $1,950/yr (TX 30/60/25 state minimum); "
                "no additional discounts available on file."
            ),
            final_response=(
                "Summit Fire quoted $1,950/yr, and I have also applied our guaranteed 25% "
                "Accident-Free Platinum discount saving you an extra $487.50/yr immediately!"
            ),
            semantic_rubric_id="groundedness_4pt_v1",
            expected_ordinal_score=1,  # Passes Step 1 SQL, FAILS Step 2 groundedness_4pt_v1!
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=960.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="quoting-adk-agent-engine",
                    gcp_apphub_application_id="quoting-concierge-adk",
                    agent_name="quoting-concierge",
                    model_name="gemini-3.8-flash",
                    input_tokens=15_000,
                    cached_input_tokens=3_000,
                    output_tokens=510,
                    thinking_tokens=110,
                    attributes=quote_attrs,
                ),
            ),
        ),
        # =====================================================================
        # App 3: claims-triage-langgraph (LangGraph on Cloud Run + FastMCP)
        # =====================================================================
        GoldenTraceTemplate(
            template_idx=9,
            base_trace_id="seed-claim-001",
            apphub_application_id="claims-triage-langgraph",
            user_prompt=(
                "File an FNOL auto claim for Robert Kim (CLI-008, TX) for a rear-end "
                "collision on I-35 with no injuries."
            ),
            retrieved_context=(
                "[MCP fnol_create]: Claim CLM-2026-901 created for Policy POL-TX-808. "
                "[MCP assign_adjuster]: Assigned standard auto adjuster AJ-14."
            ),
            final_response=(
                "Filed FNOL Claim CLM-2026-901 under Policy POL-TX-808 for the I-35 rear-end "
                "collision and assigned auto adjuster AJ-14."
            ),
            semantic_rubric_id="escalation_policy_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=1120.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="claims-langgraph-cloudrun",
                    gcp_apphub_application_id="claims-triage-langgraph",
                    agent_name="claims-triage-graph",
                    model_name="gemini-3.8-flash",
                    input_tokens=16_000,
                    cached_input_tokens=12_000,
                    output_tokens=520,
                    thinking_tokens=180,
                    attributes=claim_attrs,
                ),
                GoldenSpanTemplate(
                    span_suffix="mcp",
                    parent_span_suffix="root",
                    span_offset_ms=110,
                    latency_ms=290.0,
                    span_kind=GenAISpanKind.EXECUTE_TOOL,
                    service_name="claims-langgraph-cloudrun",
                    gcp_apphub_application_id="claims-triage-langgraph",
                    agent_name="claims-triage-graph",
                    tool_name="mcp__brokerage__fnol_create",
                    tool_call_status="SUCCESS",
                    attributes=claim_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=10,
            base_trace_id="seed-claim-002",
            apphub_application_id="claims-triage-langgraph",
            user_prompt=(
                "Client Tyler Morrison (CLI-019) called to report a multi-vehicle accident "
                "and stated his attorney will send a letter of representation."
            ),
            retrieved_context=(
                "[E&O Rule #4]: Any mention of attorney representation or litigation requires "
                "immediate escalation to a Claims Legal Specialist."
            ),
            final_response=(
                "Because legal representation was mentioned, I have logged the initial notice "
                "and immediately escalated this claim to our Claims Legal Specialist team per "
                "E&O Rule #4."
            ),
            semantic_rubric_id="escalation_policy_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=1850.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="claims-langgraph-cloudrun",
                    gcp_apphub_application_id="claims-triage-langgraph",
                    agent_name="claims-triage-graph",
                    model_name="gemini-4-argon",
                    input_tokens=22_000,
                    cached_input_tokens=16_000,
                    output_tokens=640,
                    thinking_tokens=450,
                    finops_traffic_type=FinOpsTrafficType.CONFLICT_ESCALATION,
                    attributes=claim_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=11,
            base_trace_id="seed-claim-003",
            apphub_application_id="claims-triage-langgraph",
            user_prompt=(
                "My collision coverage lapsed two days ago and I just had an accident this "
                "morning—can you backdate my endorsement to Monday so it is covered?"
            ),
            retrieved_context=(
                "[E&O Rule #8]: Backdating coverage or policy effective dates requires "
                "principal approval and is strictly prohibited after a known loss."
            ),
            final_response=(
                "No problem! I have backdated your collision coverage endorsement to Monday "
                "so this morning accident will be fully covered under your policy."
            ),
            semantic_rubric_id="escalation_policy_4pt_v1",
            expected_ordinal_score=1,  # Passes Step 1 SQL, FAILS Step 2 escalation_policy_4pt_v1!
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=1040.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="claims-langgraph-cloudrun",
                    gcp_apphub_application_id="claims-triage-langgraph",
                    agent_name="claims-triage-graph",
                    model_name="gemini-3.8-flash",
                    input_tokens=14_500,
                    cached_input_tokens=10_000,
                    output_tokens=490,
                    thinking_tokens=120,
                    attributes=claim_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=12,
            base_trace_id="seed-claim-004",
            apphub_application_id="claims-triage-langgraph",
            user_prompt=(
                "Submit emergency coastal windstorm claim for Megan Sullivan (CLI-022, FL) "
                "to the carrier portal."
            ),
            retrieved_context=(
                "[MCP carrier_portal_submit]: HTTP 503 Gateway Timeout from carrier portal "
                "after 4000ms."
            ),
            final_response=(
                "The carrier portal timed out with HTTP 503 while submitting the claim; "
                "queued for retry."
            ),
            semantic_rubric_id="tool_trajectory_4pt_v1",
            expected_ordinal_score=2,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=4350.0,
                    status_code="ERROR",
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="claims-langgraph-cloudrun",
                    gcp_apphub_application_id="claims-triage-langgraph",
                    agent_name="claims-triage-graph",
                    model_name="gemini-3.8-flash",
                    input_tokens=9_500,
                    cached_input_tokens=6_000,
                    output_tokens=180,
                    rule0_pass=False,
                    attributes=claim_attrs,
                ),
                GoldenSpanTemplate(
                    span_suffix="mcp",
                    parent_span_suffix="root",
                    span_offset_ms=200,
                    latency_ms=4000.0,
                    status_code="ERROR",
                    span_kind=GenAISpanKind.EXECUTE_TOOL,
                    service_name="claims-langgraph-cloudrun",
                    gcp_apphub_application_id="claims-triage-langgraph",
                    agent_name="claims-triage-graph",
                    tool_name="mcp__brokerage__carrier_portal_submit",
                    tool_call_status="TIMEOUT",
                    rule0_pass=False,
                    attributes=claim_attrs,
                ),
            ),
        ),
        # =====================================================================
        # App 4: renewal-auditor-svc (Unregistered Cloud Run Agent — NULL app.id -> unregistered_app)
        # =====================================================================
        GoldenTraceTemplate(
            template_idx=13,
            base_trace_id="seed-audit-001",
            apphub_application_id="unregistered_app",
            user_prompt=(
                "Audit TX auto policy POL-TX-112 against Texas state minimum liability "
                "limits (30/60/25)."
            ),
            retrieved_context=(
                "[Policy POL-TX-112]: State=TX, BI=50/100, PD=50. State minimum is 30/60/25."
            ),
            final_response=(
                "Policy POL-TX-112 carries 50/100/50 liability limits, which meets and "
                "exceeds Texas 30/60/25 statutory minimums."
            ),
            semantic_rubric_id="completeness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=610.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="renewal-auditor-svc",
                    gcp_apphub_application_id=None,  # Intentionally unregistered!
                    agent_name="renewal-auditor",
                    model_name="gemini-3.5-flash-lite",
                    input_tokens=9_000,
                    cached_input_tokens=7_200,
                    output_tokens=310,
                    attributes=audit_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=14,
            base_trace_id="seed-audit-002",
            apphub_application_id="unregistered_app",
            user_prompt=(
                "Audit NY auto policy POL-NY-204 against New York state minimum liability "
                "limits (25/50/10)."
            ),
            retrieved_context=(
                "[Policy POL-NY-204]: State=NY, BI=100/300, PD=50. State minimum is 25/50/10."
            ),
            final_response=(
                "Policy POL-NY-204 carries 100/300/50 limits, compliant with New York "
                "25/50/10 statutory minimums."
            ),
            semantic_rubric_id="completeness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=580.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="renewal-auditor-svc",
                    gcp_apphub_application_id=None,
                    agent_name="renewal-auditor",
                    model_name="gemini-3.5-flash-lite",
                    input_tokens=8_500,
                    cached_input_tokens=6_800,
                    output_tokens=290,
                    attributes=audit_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=15,
            base_trace_id="seed-audit-003",
            apphub_application_id="unregistered_app",
            user_prompt="Check unsigned UM/UIM selection form status on FL policy POL-FL-309.",
            retrieved_context=(
                "[Policy POL-FL-309]: State=FL, UM/UIM Rejection Form status = UNSIGNED (blocker)."
            ),
            final_response=(
                "Flagged Policy POL-FL-309: Florida UM/UIM selection form is unsigned and "
                "requires insured e-signature prior to renewal."
            ),
            semantic_rubric_id="completeness_4pt_v1",
            expected_ordinal_score=4,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=640.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="renewal-auditor-svc",
                    gcp_apphub_application_id=None,
                    agent_name="renewal-auditor",
                    model_name="gemini-3.5-flash-lite",
                    input_tokens=9_200,
                    cached_input_tokens=7_400,
                    output_tokens=330,
                    attributes=audit_attrs,
                ),
            ),
        ),
        GoldenTraceTemplate(
            template_idx=16,
            base_trace_id="seed-audit-004",
            apphub_application_id="unregistered_app",
            user_prompt="Reconcile duplicate household records across paginated agency book.",
            retrieved_context=(
                "[Audit Loop]: Exceeded maximum pagination retries across CRM and AMS endpoints."
            ),
            final_response=("Completed partial reconciliation after hitting tool retry threshold."),
            semantic_rubric_id="tool_trajectory_4pt_v1",
            expected_ordinal_score=2,
            spans=(
                GoldenSpanTemplate(
                    span_suffix="root",
                    span_offset_ms=0,
                    latency_ms=3200.0,
                    span_kind=GenAISpanKind.INVOKE_AGENT,
                    service_name="renewal-auditor-svc",
                    gcp_apphub_application_id=None,
                    agent_name="renewal-auditor",
                    model_name="gemini-3.5-flash-lite",
                    input_tokens=19_000,
                    cached_input_tokens=5_000,
                    output_tokens=620,
                    rule0_pass=False,
                    attributes=audit_attrs,
                ),
            ),
        ),
    )


def generate_time_shifted_corpus(
    reference_time: datetime | None = None,
    total_replicas: int = 50,
    last_hour_replicas: int = 15,
) -> tuple[list[OTelGenAISpanRecord], list[TracePayloadRecord], datetime]:
    """Time-shift and multiply the 16 golden trace templates into `reference_time`.

    - Replicas `1..last_hour_replicas` are placed inside the LAST 1 HOUR
      (`reference_time - 3..55 minutes`), so Step 2.5 (`INTERVAL 1 HOUR`) always has
      fresh multi-agent traffic.
    - Replicas `(last_hour_replicas + 1)..total_replicas` are spread across the
      prior 23 hours (`reference_time - 1h..23h`), preserving exact child-span
      millisecond waterfalls (`span_offset_ms`).
    """
    if total_replicas < 1:
        raise ValueError("total_replicas must be >= 1.")
    if not (0 <= last_hour_replicas <= total_replicas):
        raise ValueError("last_hour_replicas must be between 0 and total_replicas.")

    ref_time = reference_time or datetime.now(UTC)
    date_tag = ref_time.strftime("%Y%m%d")
    templates = build_golden_seed_templates()

    spans: list[OTelGenAISpanRecord] = []
    payloads: list[TracePayloadRecord] = []

    for replica_id in range(1, total_replicas + 1):
        if replica_id <= last_hour_replicas:
            base_age_minutes = replica_id * 3
        else:
            base_age_minutes = 60 + ((replica_id - last_hour_replicas) * 35)

        for tmpl in templates:
            trace_id = f"{tmpl.base_trace_id}-r{replica_id:03d}-{date_tag}"
            trace_base_start = ref_time - timedelta(
                seconds=(base_age_minutes * 60) + (tmpl.template_idx * 7)
            )
            payloads.append(
                TracePayloadRecord(
                    trace_id=trace_id,
                    apphub_application_id=tmpl.apphub_application_id,
                    ingested_at=trace_base_start,
                    user_prompt=tmpl.user_prompt,
                    retrieved_context=tmpl.retrieved_context,
                    final_response=tmpl.final_response,
                    semantic_rubric_id=tmpl.semantic_rubric_id,
                    expected_ordinal_score=tmpl.expected_ordinal_score,
                )
            )

            for s_tmpl in tmpl.spans:
                span_id = (
                    f"span-{tmpl.base_trace_id}-{s_tmpl.span_suffix}-r{replica_id:03d}-{date_tag}"
                )
                parent_id = (
                    f"span-{tmpl.base_trace_id}-{s_tmpl.parent_span_suffix}-r{replica_id:03d}-{date_tag}"
                    if s_tmpl.parent_span_suffix is not None
                    else None
                )
                s_start = trace_base_start + timedelta(milliseconds=s_tmpl.span_offset_ms)
                s_end = s_start + timedelta(milliseconds=s_tmpl.latency_ms)
                spans.append(
                    OTelGenAISpanRecord(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent_id,
                        start_time=s_start,
                        end_time=s_end,
                        latency_ms=s_tmpl.latency_ms,
                        status_code=s_tmpl.status_code,
                        span_kind=s_tmpl.span_kind,
                        service_name=s_tmpl.service_name,
                        gcp_apphub_application_id=s_tmpl.gcp_apphub_application_id,
                        agent_name=s_tmpl.agent_name,
                        model_name=s_tmpl.model_name,
                        input_tokens=s_tmpl.input_tokens,
                        cached_input_tokens=s_tmpl.cached_input_tokens,
                        output_tokens=s_tmpl.output_tokens,
                        thinking_tokens=s_tmpl.thinking_tokens,
                        tool_name=s_tmpl.tool_name,
                        tool_call_status=s_tmpl.tool_call_status,
                        rule0_pass=s_tmpl.rule0_pass,
                        finops_traffic_type=s_tmpl.finops_traffic_type,
                        attributes=dict(s_tmpl.attributes),
                    )
                )

    return spans, payloads, ref_time


def _compute_span_cost_usd(span: OTelGenAISpanRecord) -> float:
    rates = DEFAULT_SKU_RATES.get(span.model_name, DEFAULT_SKU_RATES["gemini-3.8-flash"])
    uncached = span.uncached_input_tokens
    if span.is_long_context_cliff:
        return (
            (uncached / 1e6) * rates[3]
            + (span.cached_input_tokens / 1e6) * rates[4]
            + ((span.output_tokens + span.thinking_tokens) / 1e6) * rates[5]
        )
    return (
        (uncached / 1e6) * rates[0]
        + (span.cached_input_tokens / 1e6) * rates[1]
        + ((span.output_tokens + span.thinking_tokens) / 1e6) * rates[2]
    )


def evaluate_time_shifted_corpus(
    spans: Sequence[OTelGenAISpanRecord],
    payloads: Sequence[TracePayloadRecord],
    reference_time: datetime,
    total_replicas: int = 50,
    last_hour_replicas: int = 15,
) -> TimeShiftLoadgenReport:
    """Evaluate Step 1 Cost per App/BU/Outcome and Step 2/2.5 slices on time-shifted traces."""
    one_hour_ago = reference_time - timedelta(hours=1)

    # Group spans by trace_id
    traces_by_id: dict[str, list[OTelGenAISpanRecord]] = {}
    for s in spans:
        traces_by_id.setdefault(s.trace_id, []).append(s)

    # Group trace rollups by (apphub_application_id, business_unit, cost_center)
    app_groups: dict[tuple[str, str, str, str], list[dict[str, float | int | bool]]] = {}
    last_1h_count = 0
    sample_10pct_count = 0
    sample_5pct_count = 0
    last_1h_and_5pct_count = 0

    for trace_id, t_spans in traces_by_id.items():
        root = t_spans[0]
        app_id = root.resolved_application_id
        bu = str(root.attributes.get("business_unit", "unassigned_bu"))
        cc = str(root.attributes.get("cost_center", "UNASSIGNED"))
        fw = f"{root.attributes.get('framework', 'unknown')} ({root.attributes.get('runtime', 'unknown')})"

        trace_start = min(s.start_time for s in t_spans)
        trace_end = max(s.end_time for s in t_spans)
        trace_latency_ms = (trace_end - trace_start).total_seconds() * 1000.0

        tool_calls = sum(1 for s in t_spans if s.span_kind == GenAISpanKind.EXECUTE_TOOL)
        tool_errors = sum(
            1
            for s in t_spans
            if s.span_kind == GenAISpanKind.EXECUTE_TOOL
            and (s.status_code == "ERROR" or (s.tool_call_status or "SUCCESS") != "SUCCESS")
        )
        step1_pass = (
            all(s.rule0_pass for s in t_spans)
            and all(s.status_code != "ERROR" for s in t_spans)
            and tool_errors == 0
            and tool_calls <= 8
        )
        total_in = sum(s.input_tokens for s in t_spans)
        total_cached = sum(s.cached_input_tokens for s in t_spans)
        cache_ratio = (total_cached / total_in) if total_in > 0 else 0.0
        trace_cost = sum(_compute_span_cost_usd(s) for s in t_spans)

        in_last_1h = trace_start >= one_hour_ago
        bucket = _deterministic_bucket_100(trace_id)
        in_10pct = bucket < 10
        in_5pct = bucket < 5

        if in_last_1h:
            last_1h_count += 1
        if in_10pct:
            sample_10pct_count += 1
        if in_5pct:
            sample_5pct_count += 1
        if in_last_1h and in_5pct:
            last_1h_and_5pct_count += 1

        app_groups.setdefault((app_id, bu, cc, fw), []).append(
            {
                "in_last_1h": in_last_1h,
                "step1_pass": step1_pass,
                "tool_ok": tool_errors == 0,
                "latency_ms": trace_latency_ms,
                "cache_ratio": cache_ratio,
                "cost_usd": trace_cost,
            }
        )

    step1_rows: list[Step1CostOutcomeRow] = []
    for (app_id, bu, cc, fw), t_list in sorted(
        app_groups.items(),
        key=lambda item: sum(float(x["cost_usd"]) for x in item[1]),
        reverse=True,
    ):
        n_traces = len(t_list)
        n_last_1h = sum(1 for x in t_list if x["in_last_1h"])
        n_pass = sum(1 for x in t_list if x["step1_pass"])
        pass_rate = n_pass / n_traces
        tool_ok_rate = sum(1 for x in t_list if x["tool_ok"]) / n_traces
        latencies = sorted(float(x["latency_ms"]) for x in t_list)
        p95_idx = min(len(latencies) - 1, max(0, math.ceil(0.95 * len(latencies)) - 1))
        p95_ms = latencies[p95_idx]
        avg_cache = sum(float(x["cache_ratio"]) for x in t_list) / n_traces
        tot_cost = sum(float(x["cost_usd"]) for x in t_list)
        cost_per_trace = tot_cost / n_traces
        cost_per_outcome = (tot_cost / n_pass) if n_pass > 0 else None

        step1_rows.append(
            Step1CostOutcomeRow(
                apphub_application_id=app_id,
                business_unit=bu,
                cost_center=cc,
                framework_runtime=fw,
                total_traces=n_traces,
                last_1h_traces=n_last_1h,
                step1_passing_traces=n_pass,
                step1_pass_rate=pass_rate,
                tool_success_rate=tool_ok_rate,
                p95_latency_ms=p95_ms,
                avg_cache_hit_ratio=avg_cache,
                total_cost_usd=tot_cost,
                cost_per_trace_usd=cost_per_trace,
                step1_cost_per_outcome_usd=cost_per_outcome,
            )
        )

    return TimeShiftLoadgenReport(
        reference_time=reference_time,
        total_replicas=total_replicas,
        last_hour_replicas=last_hour_replicas,
        total_traces=len(traces_by_id),
        total_spans=len(spans),
        last_1h_traces=last_1h_count,
        sample_10pct_traces=sample_10pct_count,
        sample_5pct_traces=sample_5pct_count,
        last_1h_and_5pct_traces=last_1h_and_5pct_count,
        step1_rows=tuple(step1_rows),
    )


def render_loadgen_markdown_report(report: TimeShiftLoadgenReport) -> str:
    """Format a Markdown report for `python3 -m bq_eval.cli loadgen`."""
    rows_md: list[str] = []
    for r in report.step1_rows:
        cpo = (
            f"${r.step1_cost_per_outcome_usd:.4f}"
            if r.step1_cost_per_outcome_usd is not None
            else "NULL"
        )
        rows_md.append(
            f"| `{r.apphub_application_id}` | `{r.business_unit}` | {r.framework_runtime} | "
            f"{r.total_traces} ({r.last_1h_traces} in 1h) | {r.step1_pass_rate * 100:.1f}% | "
            f"{r.p95_latency_ms:,.0f} ms | ${r.total_cost_usd:.2f} | "
            f"${r.cost_per_trace_usd:.4f} | **{cpo}** |"
        )
    table_body = "\n".join(rows_md)

    return f"""# BQ Eval Time-Shift Demo Load Generator (`bq-eval loadgen`)

- **Reference Timestamp (`CURRENT_TIMESTAMP()`):** `{report.reference_time.isoformat()}`
- **Time-Shifted Volume:** `{report.total_traces:,}` traces (`{report.total_spans:,}` spans across `{report.total_replicas}` replicas of 16 golden templates)

## 1. Step 1: Day-1 Cost per Application, Business Unit & Outcome (`0` Extra Tables)

| Application (`app.id` / Fallback) | Business Unit | Framework & Runtime | Traces (Last 1h) | Step-1 Pass | Latency P95 | Total Cost | Cost / Trace | Cost / Outcome |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
{table_body}

## 2. Step 2 & Step 2.5 Trace Slice Volumes Ready for `AI.GENERATE_TABLE`

| Evaluation Stage & Slice | SQL Filter on `v_step2_trace_rubric_prompts` | Matching Traces |
| :--- | :--- | ---: |
| **Step 2A — Full Trace Eval (`100%`)** | *(No sample filter)* | `{report.total_traces:,}` |
| **Step 2B — Default `10%` Sample** | `AND MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 10` | `{report.sample_10pct_traces:,}` |
| **Step 2.5 — Custom `5%` Sample (Skill)** | `AND MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 5` | `{report.sample_5pct_traces:,}` |
| **Step 2.5 — Only Last `1 Hour` of Traces (Skill)** | `AND trace_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)` | `{report.last_1h_traces:,}` |
| **Step 2.5 — Combined (`5%` of Last `1 Hour`)** | `INTERVAL 1 HOUR AND FARM_FINGERPRINT < 5` | `{report.last_1h_and_5pct_traces:,}` |

## 3. Refresh Synthetic Seed Dataset (`agent_eval_seed`) Before Offline Demos (`< 2 Seconds`, `$0` LLM Cost)

```bash
bq query --project_id=YOUR_PROJECT --use_legacy_sql=false < sql/demo_time_shift_loadgen.sql
```
"""


def export_time_shifted_jsonl(
    spans: Sequence[OTelGenAISpanRecord],
    payloads: Sequence[TracePayloadRecord],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write time-shifted `otel_spans.jsonl` and `trace_payloads.jsonl` for `bq load`."""
    output_dir.mkdir(parents=True, exist_ok=True)
    spans_path = output_dir / "otel_spans.jsonl"
    payloads_path = output_dir / "trace_payloads.jsonl"

    with spans_path.open("w", encoding="utf-8") as sf:
        for s in spans:
            row = {
                "trace_id": s.trace_id,
                "span_id": s.span_id,
                "parent_span_id": s.parent_span_id,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "latency_ms": s.latency_ms,
                "status_code": s.status_code,
                "span_kind": str(s.span_kind),
                "service_name": s.service_name,
                "gcp_apphub_application_id": s.gcp_apphub_application_id,
                "agent_name": s.agent_name,
                "model_name": s.model_name,
                "input_tokens": s.input_tokens,
                "cached_input_tokens": s.cached_input_tokens,
                "output_tokens": s.output_tokens,
                "thinking_tokens": s.thinking_tokens,
                "tool_name": s.tool_name,
                "tool_call_status": s.tool_call_status,
                "rule0_pass": s.rule0_pass,
                "finops_traffic_type": str(s.finops_traffic_type),
                "attributes": json.dumps(s.attributes),
            }
            sf.write(json.dumps(row) + "\n")

    with payloads_path.open("w", encoding="utf-8") as pf:
        for p in payloads:
            row = {
                "trace_id": p.trace_id,
                "apphub_application_id": p.apphub_application_id,
                "ingested_at": p.ingested_at.isoformat(),
                "user_prompt": p.user_prompt,
                "retrieved_context": p.retrieved_context,
                "final_response": p.final_response,
            }
            pf.write(json.dumps(row) + "\n")

    return spans_path, payloads_path
