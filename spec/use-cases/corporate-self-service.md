---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 1: Corporate Self-Service Agent

> Internal corporate assistant answering procedure questions and personal HR queries (such as vacation allowance), using a looping tool-planner model followed by a final-answer model on GCP PayGo under sharp month-boundary peaks.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Employee self-service assistant for internal corporate procedures and personal HR information (for example, vacation allowance). |
| **Access scope & risk tier** | **Strictly self-service only** (`RL-3`) — no manager, HR-admin, or delegate exceptions to inspect another employee's records. |
| **Tools (2 MCP)** | 1. **Internal procedures MCP**: searches internal procedures and returns document references/links.<br>2. **Personal info MCP**: accesses the requester's personal data (vacation allowance, etc.), preferably authorized via signed on-behalf-of (OBO) tokens (`authz_mode = 'token_enforced'`) rather than trusting LLM-supplied `user_id` parameters. |
| **Models & flow** | 1. **Model 1 (`gemini-3.5-flash-lite`)**: defines tool calls; loops in this model when initial search results are insufficient and more lookups are needed.<br>2. **Model 2 (`gemini-3.8-flash`)**: formulates the final answer; called once only after Model 1 finishes. |
| **Cloud & serving** | GCP only; PayGo models only (`attempts = 1` caller retry discipline to prevent month-boundary retry storms through any shared proxy). |
| **Traffic shape** | Almost no traffic outside working hours; reasonable baseline during working hours; **massive peak on the last and first day of the month**. |

## Telemetry & Data Expectations

- **OTel GenAI & proxy correlation**: Standard GenAI + MCP call traces (including `execute_tool` / MCP calls and both model steps), carrying `x-client-request-id` and `thought_signature_present` across the `gemini-3.5-flash-lite -> gemini-3.8-flash` handoff.
- **Attested identity**: `user.id` is mandated on every trace and verified via gateway/identity-proxy attestation (`bq_eval.identity_attested = true`, structured OBO claims `auth_method`/`issuer`/`aud`, and `authz_mode = 'token_enforced'`; unattested fallback fails closed on `RL-3`).
- **Inspectable signals**: Tool-call parameters (or attested OBO token subject claims), per-call time to first token (`gen_ai.response.time_to_first_chunk`), retrieved MCP outputs/links, and the final answer text.

## Required Checks, Alerts & Views

1. **Preventive `< 1m` missing-`user.id` guard & 5-minute audit**
   - Block or alert on missing `user.id` in under 1 minute inside the Preventive Request-Path Enforcement Port (gateway/application guard); run BQ Eval's 5-minute scheduled audit over Silver to verify zero unattributed traces slipped through.
2. **Cross-user data access evaluation (self-service only)**
   - Fail closed on unattested `RL-3` calls (`identity_attested != true`), verify `token_enforced` OBO subject binding (or parameter match where legacy parameter-keyed tools remain), with zero delegation exceptions.
3. **Result accuracy & groundedness (rubric-based)**
   - Evaluate with a calibrated rubric whether the final answer accurately reflects the retrieved MCP procedure and personal-info results, without fabricating numbers/rules when search or lookup returns nothing.
4. **Required citations / source links**
   - Verify that every factual or personal piece of information in the final answer includes a citation/link grounded in retrieved tool outputs rather than invented.
5. **Final-answer disclaimer evaluation**
   - Verify there is always a mandatory disclaimer in the final answer.
6. **Operational, TTFT, `thought_signature` & spike retry visibility**
   - Track Model 1 tool-call loops, end-to-end and user-visible TTFT, `thought_signature_present` preservation across the 2-model handoff, `caller_retry_count` storm detection (`attempts = 1`) during month-boundary spikes, and PayGo cost across both models.
