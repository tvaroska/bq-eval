---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 5: Regulated Action-Taking Workflow

> A regulated enterprise workflow where agents make policy-governed decisions and execute state-changing writes into systems of record (such as approvals, payouts, credits, or access changes) under mandatory human-in-the-loop (HITL) thresholds, idempotency guards, blind human audit calibration, and pre-release regression gates.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Execute high-impact, policy-governed enterprise transactions that mutate systems of record (for example, claims/credit adjustments, financial/procurement approvals, or access provisioning). |
| **Tools & actions** | **State-mutating tools and downstream APIs**: read context, verify policy rules, create/update records, and park or route decisions for **human-in-the-loop (HITL)** sign-off. |
| **Lifecycle** | Multi-step workflow that may pause for human review and resume hours or days later before closing a single business outcome. |
| **Governance & risk** | Regulated environment requiring auditable decisions, blind human spot-check calibration of automated evaluations, and strict pre-release non-regression gates. |
| **Cloud, models & serving** | Open (pending specification of multi-cloud, business-unit capacity sharing, and model/serving constraints). |

## Telemetry & Data Expectations

- **Business outcome & pause correlation**: Link all agent steps, policy checks, HITL review/approval events, and final state-mutating writes across pauses/resumes to one business outcome (including rejected, abandoned, and escalated outcomes).
- **Write & authorization signals**: Capture write tool invocations, policy thresholds/decisions, idempotency keys, and HITL reviewer sign-off records.
- **Audit labels & release versions**: Track prompt/skill/model version identifiers alongside blind human SME review labels paired against automated evaluator verdicts.
- **Inspectable decision payloads**: Preserve inputs, evidence, policy citations, and action payloads in-cloud for compliance audits and paired regression testing.

## Required Checks, Alerts & Views

1. **Authorization & HITL approval-gate enforcement**
   - Verify the agent never executes restricted state-mutating writes without satisfying policy rules and required human approval (and properly parks/escalates out-of-policy or high-value cases).
2. **Idempotency & duplicate-mutation prevention**
   - Detect duplicate write tool calls or double-committed actions caused by retries, timeouts, or agent loops.
3. **Audit-grade judge calibration (blind human SME grounding)**
   - Measure automated evaluator agreement and bias against blind human SME labels with explicit uncertainty bounds; fail closed rather than reporting verified pass rates when uncalibrated or unreviewed.
4. **Pre-release non-regression gating**
   - Compare candidate prompt, skill, or model versions against approved baselines on golden/paired cases so updates cannot silently flip passing compliance or policy outcomes into failures.
5. **True cost per completed outcome**
   - Measure total cost per successfully verified business outcome, including failed, rejected, and human-escalated attempts in the numerator (no survivor bias).
