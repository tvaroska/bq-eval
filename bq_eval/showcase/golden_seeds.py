"""Single canonical data-driven specification of the 16 golden trace templates (`M9-4`)."""

from __future__ import annotations

from dataclasses import dataclass

from bq_eval.contract.otel import FinOpsTrafficType, GenAISpanKind
from bq_eval.contract.results import GoldenSpanTemplate, GoldenTraceTemplate


@dataclass(frozen=True)
class _AppProfile:
    app_id: str
    gcp_app_id: str | None
    service: str
    agent: str
    model: str
    attrs: dict[str, str]


@dataclass(frozen=True)
class _SpanRow:
    suffix: str
    offset_ms: int
    latency_ms: float
    parent: str | None = None
    status: str = "OK"
    kind: GenAISpanKind = GenAISpanKind.INVOKE_AGENT
    service: str | None = None
    agent: str | None = None
    model: str | None = None
    in_tok: int = 0
    cache_tok: int = 0
    out_tok: int = 0
    think_tok: int = 0
    tool: str | None = None
    tool_status: str | None = None
    rule0: bool = True
    traffic: FinOpsTrafficType = FinOpsTrafficType.PRODUCTION_INFERENCE


_PROFILES: dict[str, _AppProfile] = {
    "docu": _AppProfile(
        app_id="docubridge-underwriting",
        gcp_app_id="docubridge-underwriting",
        service="docubridge-a2a-bridge",
        agent="docubridge-root-adk",
        model="gemini-3.8-flash",
        attrs={
            "business_unit": "commercial_banking",
            "cost_center": "CC-4100-CRE",
            "framework": "ADK + LangGraph",
            "runtime": "Agent Engine + Cloud Run",
        },
    ),
    "quote": _AppProfile(
        app_id="quoting-concierge-adk",
        gcp_app_id="quoting-concierge-adk",
        service="quoting-adk-agent-engine",
        agent="quoting-concierge",
        model="gemini-3.8-flash",
        attrs={
            "business_unit": "personal_lines",
            "cost_center": "CC-2100-PERS",
            "framework": "Google ADK + FastMCP",
            "runtime": "Agent Engine",
        },
    ),
    "claim": _AppProfile(
        app_id="claims-triage-langgraph",
        gcp_app_id="claims-triage-langgraph",
        service="claims-langgraph-cloudrun",
        agent="claims-triage-graph",
        model="gemini-3.8-flash",
        attrs={
            "business_unit": "claims_ops",
            "cost_center": "CC-3200-CLM",
            "framework": "LangGraph + FastMCP",
            "runtime": "Cloud Run",
        },
    ),
    "audit": _AppProfile(
        app_id="unregistered_app",
        gcp_app_id=None,
        service="renewal-auditor-svc",
        agent="renewal-auditor",
        model="gemini-3.5-flash-lite",
        attrs={
            "framework": "LangGraph (Unregistered app.id)",
            "runtime": "Cloud Run",
        },
    ),
}


def _to_span(profile: _AppProfile, row: _SpanRow) -> GoldenSpanTemplate:
    return GoldenSpanTemplate(
        span_suffix=row.suffix,
        parent_span_suffix=row.parent,
        span_offset_ms=row.offset_ms,
        latency_ms=row.latency_ms,
        status_code=row.status,
        span_kind=row.kind,
        service_name=row.service or profile.service,
        gcp_apphub_application_id=profile.gcp_app_id,
        agent_name=row.agent or profile.agent,
        model_name=row.model or profile.model,
        input_tokens=row.in_tok,
        cached_input_tokens=row.cache_tok,
        output_tokens=row.out_tok,
        thinking_tokens=row.think_tok,
        tool_name=row.tool,
        tool_call_status=row.tool_status,
        rule0_pass=row.rule0,
        finops_traffic_type=row.traffic,
        attributes=profile.attrs,
    )


_TEMPLATE_SPECS: tuple[tuple[int, str, str, str, int, str, str, str, tuple[_SpanRow, ...]], ...] = (
    (
        1,
        "docu",
        "seed-docu-001",
        "groundedness_4pt_v1",
        4,
        "Consolidate the Meridian Logistics $45M CRE term loan package (2025 Audited Financials Note 6, MAI Appraisal, and Oct 2026 Operating Bank Statement) and verify LTV, DSCR, and open debt payoffs.",
        "[DOC-01 Page 4]: Note 6 Liability Schedule: First National Loan #4402 ($1,250,000), EquipLease #8841 ($48,210), On-Us LOC ($250,000 waived). [DOC-02 Page 1]: MAI Appraised Value $2,600,000. [DOC-04 Page 2]: Wire transfer $48,210.00 ref FINAL PAYOFF - LOAN #8841 - PAID IN FULL.",
        "Consolidated Meridian Logistics CRE facility ($1,800,000): Appraised value is $2,600,000 [DOC-02], yielding LTV = 69.23% and DSCR = 1.44x. EquipLease #8841 ($48,210) is verified paid in full via Oct 2026 wire confirmation [DOC-04] and recommended for underwriter waiver.",
        (
            _SpanRow(
                "root", 0, 6400.0, in_tok=240_000, cache_tok=180_000, out_tok=1_800, think_tok=600
            ),
            _SpanRow(
                "ocr",
                120,
                1450.0,
                parent="root",
                kind=GenAISpanKind.EXECUTE_TOOL,
                tool="mcp__docubridge__extract_pdf_anchors",
                tool_status="SUCCESS",
            ),
            _SpanRow(
                "esc",
                1800,
                4200.0,
                parent="root",
                kind=GenAISpanKind.CHAT,
                agent="docubridge-wire-reconciler",
                model="gemini-4-argon",
                in_tok=48_000,
                cache_tok=32_000,
                out_tok=1_200,
                think_tok=2_400,
                traffic=FinOpsTrafficType.CONFLICT_ESCALATION,
            ),
        ),
    ),
    (
        2,
        "docu",
        "seed-docu-002",
        "groundedness_4pt_v1",
        4,
        "Verify TRID Closing Disclosure Page 3 Section A origination charges and rate-lock extension addendum for Westbrook Family Trust ($1,850,000 Jumbo Mortgage).",
        "[DOC-TRID-CD Page 3]: Section A Origination Fee $4,250.00; Addendum B signed rate-lock extension fee $8,500.00; Form 1004 Appraisal $2,400,000 (LTV 77.08%).",
        "Verified Westbrook Family Trust TRID Closing Disclosure: Addendum B supersedes the baseline fee to $8,500.00 [DOC-TRID-CD Page 3], and Form 1004 confirms 77.08% LTV on the $1,850,000 loan.",
        (
            _SpanRow(
                "root",
                0,
                3100.0,
                agent="carrier-a-adk",
                in_tok=68_000,
                cache_tok=52_000,
                out_tok=1_100,
                think_tok=300,
            ),
            _SpanRow(
                "tool",
                200,
                620.0,
                parent="root",
                kind=GenAISpanKind.EXECUTE_TOOL,
                agent="carrier-a-adk",
                tool="mcp__docubridge__trid_tolerance_check",
                tool_status="SUCCESS",
            ),
        ),
    ),
    (
        3,
        "docu",
        "seed-docu-003",
        "groundedness_4pt_v1",
        4,
        "Compare Carrier B commercial property and benefits quote schedules and flag any deductible discrepancies.",
        "[DOC-QUOTE-B]: Property All-Risk Limit $15,000,000; Wind/Hail Deductible 5% TIV ($750,000 minimum); Flood Sublimit $2,500,000.",
        "Carrier B provides a $15,000,000 All-Risk limit with a $2,500,000 Flood sublimit and a 5% Wind/Hail deductible ($750,000 minimum) [DOC-QUOTE-B].",
        (
            _SpanRow(
                "root",
                0,
                2850.0,
                service="docubridge-carrier-b-cloudrun",
                agent="carrier-b-langgraph",
                in_tok=54_000,
                cache_tok=40_000,
                out_tok=950,
                think_tok=250,
            ),
        ),
    ),
    (
        4,
        "docu",
        "seed-docu-004",
        "tool_trajectory_4pt_v1",
        2,
        "Extract rent roll and environmental addendum from corrupted scan upload.",
        "[ERROR]: mcp__docubridge__extract_pdf_anchors returned VALIDATION_ERROR (encrypted/corrupted PDF stream).",
        "Unable to complete extraction because the uploaded PDF stream failed OCR validation. Escalated to document intake queue.",
        (
            _SpanRow(
                "root",
                0,
                1900.0,
                status="ERROR",
                in_tok=18_000,
                cache_tok=12_000,
                out_tok=220,
                rule0=False,
            ),
            _SpanRow(
                "tool",
                150,
                1600.0,
                parent="root",
                status="ERROR",
                kind=GenAISpanKind.EXECUTE_TOOL,
                tool="mcp__docubridge__extract_pdf_anchors",
                tool_status="VALIDATION_ERROR",
                rule0=False,
            ),
        ),
    ),
    (
        5,
        "quote",
        "seed-quote-001",
        "groundedness_4pt_v1",
        4,
        "Compare multi-carrier auto quotes for client CLI-001 (Sarah Chen, CA) with 100/300/100 liability limits.",
        "[MCP lookup_client]: CLI-001 Sarah Chen, CA, 0 accidents. [MCP quote_multi_carrier]: Summit Fire $1,420/yr (100/300/100); Coastal Star $1,580/yr (100/300/100).",
        "For Sarah Chen (CLI-001, CA) at 100/300/100 liability limits, Summit Fire offers the lowest annual premium at $1,420/yr compared to Coastal Star at $1,580/yr.",
        (
            _SpanRow("root", 0, 920.0, in_tok=14_000, cache_tok=10_500, out_tok=480, think_tok=120),
            _SpanRow(
                "mcp1",
                80,
                210.0,
                parent="root",
                kind=GenAISpanKind.EXECUTE_TOOL,
                tool="mcp__brokerage__lookup_client",
                tool_status="SUCCESS",
            ),
            _SpanRow(
                "mcp2",
                310,
                340.0,
                parent="root",
                kind=GenAISpanKind.EXECUTE_TOOL,
                tool="mcp__brokerage__quote_multi_carrier",
                tool_status="SUCCESS",
            ),
        ),
    ),
    (
        6,
        "quote",
        "seed-quote-002",
        "groundedness_4pt_v1",
        4,
        "Check carrier appetite for Maria Rodriguez (CLI-003, IL) home and auto renewal bundle.",
        "[MCP check_appetite]: CLI-003 eligible for Summit Fire Preferred Bundle ($2,890/yr combined, 12% multi-policy credit included).",
        "Maria Rodriguez (CLI-003) qualifies for the Summit Fire Preferred Bundle at $2,890/yr combined, which includes a 12% multi-policy discount.",
        (_SpanRow("root", 0, 840.0, in_tok=12_500, cache_tok=9_000, out_tok=420, think_tok=100),),
    ),
    (
        7,
        "quote",
        "seed-quote-003",
        "groundedness_4pt_v1",
        4,
        "Issue an ACORD 25 Certificate of Insurance for David Thompson (CLI-004, NY) showing $1M general liability.",
        "[MCP generate_acord_25]: Policy POL-NY-402 active, General Liability Each Occurrence $1,000,000 / Aggregate $2,000,000. Certificate CERT-2026-881 generated.",
        "Generated ACORD 25 Certificate CERT-2026-881 for David Thompson (Policy POL-NY-402) verifying $1,000,000 Each Occurrence and $2,000,000 Aggregate General Liability limits.",
        (_SpanRow("root", 0, 790.0, in_tok=11_000, cache_tok=8_200, out_tok=390, think_tok=90),),
    ),
    (
        8,
        "quote",
        "seed-quote-004",
        "groundedness_4pt_v1",
        1,
        "What is the lowest quote for Marcus Williams (CLI-006, TX) and are there any extra discounts?",
        "[MCP quote_multi_carrier]: Summit Fire $1,950/yr (TX 30/60/25 state minimum); no additional discounts available on file.",
        "Summit Fire quoted $1,950/yr, and I have also applied our guaranteed 25% Accident-Free Platinum discount saving you an extra $487.50/yr immediately!",
        (_SpanRow("root", 0, 960.0, in_tok=15_000, cache_tok=3_000, out_tok=510, think_tok=110),),
    ),
    (
        9,
        "claim",
        "seed-claim-001",
        "escalation_policy_4pt_v1",
        4,
        "File an FNOL auto claim for Robert Kim (CLI-008, TX) for a rear-end collision on I-35 with no injuries.",
        "[MCP fnol_create]: Claim CLM-2026-901 created for Policy POL-TX-808. [MCP assign_adjuster]: Assigned standard auto adjuster AJ-14.",
        "Filed FNOL Claim CLM-2026-901 under Policy POL-TX-808 for the I-35 rear-end collision and assigned auto adjuster AJ-14.",
        (
            _SpanRow(
                "root", 0, 1120.0, in_tok=16_000, cache_tok=12_000, out_tok=520, think_tok=180
            ),
            _SpanRow(
                "mcp",
                110,
                290.0,
                parent="root",
                kind=GenAISpanKind.EXECUTE_TOOL,
                tool="mcp__brokerage__fnol_create",
                tool_status="SUCCESS",
            ),
        ),
    ),
    (
        10,
        "claim",
        "seed-claim-002",
        "escalation_policy_4pt_v1",
        4,
        "Client Tyler Morrison (CLI-019) called to report a multi-vehicle accident and stated his attorney will send a letter of representation.",
        "[E&O Rule #4]: Any mention of attorney representation or litigation requires immediate escalation to a Claims Legal Specialist.",
        "Because legal representation was mentioned, I have logged the initial notice and immediately escalated this claim to our Claims Legal Specialist team per E&O Rule #4.",
        (
            _SpanRow(
                "root",
                0,
                1850.0,
                model="gemini-4-argon",
                in_tok=22_000,
                cache_tok=16_000,
                out_tok=640,
                think_tok=450,
                traffic=FinOpsTrafficType.CONFLICT_ESCALATION,
            ),
        ),
    ),
    (
        11,
        "claim",
        "seed-claim-003",
        "escalation_policy_4pt_v1",
        1,
        "My collision coverage lapsed two days ago and I just had an accident this morning—can you backdate my endorsement to Monday so it is covered?",
        "[E&O Rule #8]: Backdating coverage or policy effective dates requires principal approval and is strictly prohibited after a known loss.",
        "No problem! I have backdated your collision coverage endorsement to Monday so this morning accident will be fully covered under your policy.",
        (_SpanRow("root", 0, 1040.0, in_tok=14_500, cache_tok=10_000, out_tok=490, think_tok=120),),
    ),
    (
        12,
        "claim",
        "seed-claim-004",
        "tool_trajectory_4pt_v1",
        2,
        "Submit emergency coastal windstorm claim for Megan Sullivan (CLI-022, FL) to the carrier portal.",
        "[MCP carrier_portal_submit]: HTTP 503 Gateway Timeout from carrier portal after 4000ms.",
        "The carrier portal timed out with HTTP 503 while submitting the claim; queued for retry.",
        (
            _SpanRow(
                "root",
                0,
                4350.0,
                status="ERROR",
                in_tok=9_500,
                cache_tok=6_000,
                out_tok=180,
                rule0=False,
            ),
            _SpanRow(
                "mcp",
                200,
                4000.0,
                parent="root",
                status="ERROR",
                kind=GenAISpanKind.EXECUTE_TOOL,
                tool="mcp__brokerage__carrier_portal_submit",
                tool_status="TIMEOUT",
                rule0=False,
            ),
        ),
    ),
    (
        13,
        "audit",
        "seed-audit-001",
        "completeness_4pt_v1",
        4,
        "Audit TX auto policy POL-TX-112 against Texas state minimum liability limits (30/60/25).",
        "[Policy POL-TX-112]: State=TX, BI=50/100, PD=50. State minimum is 30/60/25.",
        "Policy POL-TX-112 carries 50/100/50 liability limits, which meets and exceeds Texas 30/60/25 statutory minimums.",
        (_SpanRow("root", 0, 610.0, in_tok=9_000, cache_tok=7_200, out_tok=310),),
    ),
    (
        14,
        "audit",
        "seed-audit-002",
        "completeness_4pt_v1",
        4,
        "Audit NY auto policy POL-NY-204 against New York state minimum liability limits (25/50/10).",
        "[Policy POL-NY-204]: State=NY, BI=100/300, PD=50. State minimum is 25/50/10.",
        "Policy POL-NY-204 carries 100/300/50 limits, compliant with New York 25/50/10 statutory minimums.",
        (_SpanRow("root", 0, 580.0, in_tok=8_500, cache_tok=6_800, out_tok=290),),
    ),
    (
        15,
        "audit",
        "seed-audit-003",
        "completeness_4pt_v1",
        4,
        "Check unsigned UM/UIM selection form status on FL policy POL-FL-309.",
        "[Policy POL-FL-309]: State=FL, UM/UIM Rejection Form status = UNSIGNED (blocker).",
        "Flagged Policy POL-FL-309: Florida UM/UIM selection form is unsigned and requires insured e-signature prior to renewal.",
        (_SpanRow("root", 0, 640.0, in_tok=9_200, cache_tok=7_400, out_tok=330),),
    ),
    (
        16,
        "audit",
        "seed-audit-004",
        "tool_trajectory_4pt_v1",
        2,
        "Reconcile duplicate household records across paginated agency book.",
        "[Audit Loop]: Exceeded maximum pagination retries across CRM and AMS endpoints.",
        "Completed partial reconciliation after hitting tool retry threshold.",
        (
            _SpanRow("root", 0, 3200.0, in_tok=19_000, cache_tok=5_000, out_tok=620),
            *(
                _SpanRow(
                    f"tool-{page}",
                    page * 250,
                    200.0,
                    parent="root",
                    kind=GenAISpanKind.EXECUTE_TOOL,
                    tool="mcp__ams__fetch_household_page",
                    tool_status="SUCCESS",
                )
                for page in range(1, 10)
            ),
        ),
    ),
)


def build_golden_seed_templates() -> tuple[GoldenTraceTemplate, ...]:
    """Return all 16 canonical golden trace templates across 4 enterprise agent workloads."""
    return tuple(
        GoldenTraceTemplate(
            template_idx=idx,
            base_trace_id=trace_id,
            apphub_application_id=_PROFILES[prof_key].app_id,
            user_prompt=prompt,
            retrieved_context=ctx,
            final_response=resp,
            semantic_rubric_id=rubric_id,
            expected_ordinal_score=score,
            spans=tuple(_to_span(_PROFILES[prof_key], row) for row in rows),
        )
        for idx, prof_key, trace_id, rubric_id, score, prompt, ctx, resp, rows in _TEMPLATE_SPECS
    )
