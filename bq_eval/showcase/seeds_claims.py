"""Golden captured trace templates 9-16 (`claims-triage-langgraph` & `unregistered_app`)."""

from __future__ import annotations

from bq_eval.contract.otel import FinOpsTrafficType, GenAISpanKind
from bq_eval.contract.results import GoldenSpanTemplate, GoldenTraceTemplate


def build_claims_and_audit_templates() -> tuple[GoldenTraceTemplate, ...]:
    """Return golden trace templates 9-16 for claims triage and unregistered renewal auditing."""
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
