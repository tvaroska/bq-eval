---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 2: Customer Support Agent

> An asynchronous, multi-turn email and chat customer support system where an orchestrator coordinates specialist agents and tools — including code generation, sandboxed execution, and warm human handoffs — under strict 1-minute response, 24-hour closure SLAs, runbook-based escalation, full cost visibility, and data-driven SLO recommendations.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Handle external customer support inquiries end to end over email and interactive chat, from sender authentication, triage, and ticketing through investigation, multi-turn clarification, warm human handoff, and final resolution. |
| **Channel & lifecycle** | **Multi-turn asynchronous email / interactive thread**: authenticated inbound customer message (`DKIM`/`DMARC` verified on email) -> relevance triage -> either (a) automated rejection reply with reason (no ticket created) or (b) ticket creation + reference number -> orchestrator/specialist investigation, possible follow-ups in both directions, optional warm human escalation with verified handoff packet -> final reply & ticket closure. |
| **Agent topology** | **Complex multi-agent system**: 1 orchestrator delegating across domain specialist agents and tools (`RL-2`/`RL-3`). |
| **Tools & sandbox** | Multiple external/internal tools (runbook/knowledge base, ticketing system, email/chat outbound behind inline DLP/safety checks, diagnostics), including **code generation executed inside an isolated sandbox** (separating ephemeral code-sandbox runs from full VM runtimes). |
| **SLAs** | 1. **Initial response SLA (`<= 1 minute`)**: acknowledge valid inquiries with a reference number (or reply to irrelevant emails with a rejection reason) within 1 minute.<br>2. **Resolution SLA (`<= 24 hours`)**: close the support case within 24 hours (including multi-turn exchanges and human escalations). |
| **Cost constraint** | **Strict cost visibility**: full cost attribution across orchestrator, every specialist, tool calls, sandbox compute, human escalations, and rejected/failed inquiries. |
| **Models, cloud & capacity** | Starting baseline is intentionally open; workload observations export to `capacity` to produce **model choice, serving tier, and capacity sizing recommendations needed to hold the 1m / 24h SLOs** at lowest cost. |

## Telemetry & Data Expectations

- **End-to-end case correlation & sender auth**: Link every asynchronous step in a multi-turn thread under one `outcome.id`, carrying inbound email sender/tenant authentication verdicts (`DKIM`/`DMARC` binding to `bq_eval.tenant_id`) and warm human-handoff packet references (`handling.started` / `handling.ended`).
- **Runbook & escalation signals**: Record which runbook(s) matched (or that none matched) and whether/why the case escalated to a human.
- **Preventive vs. detective safety signals**: Record inline Preventive Request-Path Enforcement Port verdicts (`guardrail.intervention` with `control_id`, `layer`, `action` before any `sends_external_reply` execution) alongside sandbox execution status (`ok | timeout | error | policy_blocked`), runtime isolation/network-egress telemetry, retry counts, `sandbox.cpu_sec`/`mem_gb_sec`, and prompt-injection canary verdicts.
- **Inspectable conversation & citations**: Preserve the thread, retrieved runbook/evidence sources, sandbox outputs, rejection reasons, handoff summary, and final customer answer.

## Required Checks, Alerts & Views

1. **Inbound sender authentication & inline outbound safety audit**
   - Audit inbound `DKIM`/`DMARC` sender-to-tenant binding and verify inline gateway/DLP safety checks executed before every `sends_external_reply` tool call (preventive blocking lives in the gateway/sandbox runtime; BQ Eval audits attested receipts).
2. **Pre-ticket relevance gate & automated rejection**
   - Verify irrelevant emails are rejected (`outcome.status = 'rejected'`) **before** submitting to the ticketing system (zero spurious tickets) **and** receive an automated reply explaining why, paired with sampled false-rejection judging.
3. **Runbook-missing human escalation & warm handoff packet fidelity**
   - Verify the agent escalates to a human whenever no matching runbook exists (avoiding unnecessary escalation when one exists), and verify warm human-handoff packets accurately reflect prior conversation/context without dropping key customer facts.
4. **SLA compliance monitoring (1 minute & 24 hours)**
   - Track inbound-to-ack/rejection latency (`<= 1 minute`) and net closure time (`<= 24 hours`) from trace start, separating multi-turn customer wait time from agent/human processing time.
5. **Answer correctness with citations & relevancy**
   - Verify the final customer answer is factually correct, backed by verifiable citations/sources, and directly relevant to the customer's question.
6. **Code generation & sandbox detective audit**
   - Track sandbox execution failures, timeouts, retry loops, and prompt-injection canaries; ensure failed or unexecuted code is never reported as verified.
7. **Strict cost visibility & SLO recommendations**
   - Attribute full `CPVO` per closed ticket and per specialist/sandbox run (including rejected and escalated tickets), exporting observed burndown/latency to `capacity` for `1m` / `24h` SLO recommendations.
