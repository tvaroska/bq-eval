"""Golden captured trace templates for the time-shift demo load generator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from bq_eval.otel import FinOpsTrafficType, GenAISpanKind


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
            expected_ordinal_score=1,
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
            expected_ordinal_score=1,
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
                    gcp_apphub_application_id=None,
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
            final_response="Completed partial reconciliation after hitting tool retry threshold.",
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
                    rule0_pass=True,
                    attributes=audit_attrs,
                ),
                *(
                    GoldenSpanTemplate(
                        span_suffix=f"tool-{page}",
                        parent_span_suffix="root",
                        span_offset_ms=page * 250,
                        latency_ms=200.0,
                        status_code="OK",
                        span_kind=GenAISpanKind.EXECUTE_TOOL,
                        service_name="renewal-auditor-svc",
                        gcp_apphub_application_id=None,
                        agent_name="renewal-auditor",
                        model_name="gemini-3.5-flash-lite",
                        tool_name="mcp__ams__fetch_household_page",
                        tool_call_status="SUCCESS",
                        rule0_pass=True,
                        attributes=audit_attrs,
                    )
                    for page in range(1, 10)
                ),
            ),
        ),
    )
