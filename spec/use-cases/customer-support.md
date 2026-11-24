---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 2: Customer Support Agent

> An asynchronous, multi-turn email customer support system where an orchestrator coordinates specialist agents and tools — including code generation and sandboxed execution — under strict 1-minute response, 24-hour closure SLAs, runbook-based human escalation, full cost visibility, and data-driven SLO recommendations.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Handle external customer support inquiries end to end over email, from triage and ticketing through investigation, multi-turn clarification, escalation, and final resolution. |
| **Channel & lifecycle** | **Multi-turn asynchronous email thread**: inbound customer email -> relevance triage -> either (a) automated rejection reply with reason (no ticket created) or (b) ticket creation + reference number -> orchestrator/specialist investigation, possible customer follow-up questions/replies in both directions, optional human escalation -> final email reply & ticket closure. |
| **Agent topology** | **Complex multi-agent system**: 1 orchestrator delegating across multiple domain specialist agents and tools. |
| **Tools & sandbox** | Multiple external/internal tools (runbook/knowledge base, ticketing system, email outbound, diagnostics), including the ability to **generate code and execute it inside an isolated sandbox**. |
| **SLAs** | 1. **Initial response SLA (`<= 1 minute`)**: acknowledge valid inquiries with a reference number (or reply to irrelevant emails with a rejection reason) within 1 minute.<br>2. **Resolution SLA (`<= 24 hours`)**: close the support case within 24 hours (including multi-turn exchanges and human escalations). |
| **Cost constraint** | **Strict cost visibility**: full cost attribution across orchestrator, every specialist, tool calls, sandbox compute, human escalations, and rejected/failed inquiries. |
| **Models, cloud & capacity** | Starting baseline is intentionally open; workload observations must support **recommendations for model choice, serving tier, and capacity sizing to keep the 1m / 24h SLOs** at lowest cost. |

## Telemetry & Data Expectations

- **End-to-end case correlation**: Link every asynchronous step in a multi-turn email thread (inbound emails, triage verdict, 1-minute outbound ack/rejection, ticketing calls, orchestrator/specialist spans, agent follow-up questions, customer replies, sandbox runs, human escalation events, and closure) under one case identifier.
- **Runbook & escalation signals**: Record which runbook(s) matched (or that none matched) and whether/why the case escalated to a human.
- **Code & sandbox signals**: Capture generated code, sandbox execution status, timeouts/errors, retry counts, resource/duration cost, and security/boundary outcomes.
- **Inspectable conversation & citations**: Preserve the full email thread, retrieved runbook/evidence sources, sandbox outputs, rejection reasons, and final customer answer.

## Required Checks, Alerts & Views

1. **Pre-ticket relevance gate & automated rejection**
   - Verify irrelevant emails are rejected **before** submitting to the ticketing system (zero spurious tickets) **and** receive an automated reply explaining why the question is not relevant, without falsely rejecting valid support requests.
2. **Runbook-missing human escalation evaluation**
   - Verify the agent escalates to a human whenever no matching runbook exists for the question (instead of fabricating an answer), and avoids unnecessary escalation when an applicable runbook exists.
3. **SLA compliance monitoring (1 minute & 24 hours)**
   - Track inbound-to-ack/rejection latency (`<= 1 minute`) and end-to-end case closure time (`<= 24 hours`), separating multi-turn customer wait time from agent/human processing time.
4. **Answer correctness with citations & relevancy**
   - Verify the final customer answer is factually correct, backed by verifiable citations/sources (runbooks, docs, or verified sandbox outputs), and directly relevant to the customer's question.
5. **Code generation & sandbox safety & limits**
   - Track sandbox execution failures, timeouts, and retry loops; enforce isolation/safety checks (no unauthorized network, secret, or cross-customer data access); ensure failed or unexecuted code is never reported as verified.
6. **Multi-turn follow-up quality (deferred evaluation scope)**
   - Capture multi-turn agent-initiated follow-up questions and customer replies now so follow-up/clarification quality evaluations can be added later without changing telemetry.
7. **Strict cost visibility & SLO recommendations**
   - Attribute full cost per case/closed ticket and per specialist/sandbox run (including rejected and escalated tickets), and surface **model, routing, and capacity recommendations needed to hold the 1m and 24h SLOs**.
