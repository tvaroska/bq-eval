"""Golden captured trace templates 1-8 (`docubridge-underwriting` & `quoting-concierge-adk`)."""

from __future__ import annotations

from bq_eval.contract.otel import FinOpsTrafficType, GenAISpanKind
from bq_eval.contract.results import GoldenSpanTemplate, GoldenTraceTemplate


def build_banking_and_quoting_templates() -> tuple[GoldenTraceTemplate, ...]:
    """Return golden trace templates 1-8 for commercial underwriting and personal lines quoting."""
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
    )
