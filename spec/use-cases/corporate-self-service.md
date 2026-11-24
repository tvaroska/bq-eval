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
| **Access scope** | **Strictly self-service only** — no manager, HR-admin, or delegate exceptions to inspect another employee's records. |
| **Tools (2 MCP)** | 1. **Internal procedures MCP**: searches internal procedures and returns document references/links.<br>2. **Personal info MCP**: accesses the requester's personal data (vacation allowance, etc.) keyed by `user_id`, with reference links to the source HR system. |
| **Models & flow** | 1. **Model 1 (`gemini-3.5-flash-lite`)**: defines tool calls; loops in this model when initial search results are insufficient and more lookups are needed.<br>2. **Model 2 (`gemini-3.8-flash`)**: formulates the final answer; called once only after Model 1 finishes. |
| **Cloud & serving** | GCP only; PayGo models only (no dedicated capacity). |
| **Traffic shape** | Almost no traffic outside working hours; reasonable baseline during working hours; **massive peak on the last and first day of the month**. |

## Telemetry & Data Expectations

- **OTel GenAI structure**: Standard GenAI + MCP call traces (including `execute_tool` / MCP calls and both model steps).
- **Mandatory `user.id`**: `user.id` is mandated on every trace.
- **Inspectable signals**: Tool-call parameters (specifically `user_id` passed to any tool call), retrieved MCP outputs/links, and the final answer text.

## Required Checks, Alerts & Views

1. **Sub-1m missing-`user.id` warning (circuit breaker)**
   - Trigger a warning in under 1 minute if incoming traces are missing `user.id`.
2. **Cross-user data access evaluation (self-service only)**
   - Detect whenever `user_id` in any tool call differs from the requester's `user.id`, with zero delegation exceptions (and avoid passing silently when identity or tool arguments are absent).
3. **Result accuracy & groundedness (rubric-based)**
   - Evaluate with a rubric whether the final answer accurately reflects the retrieved MCP procedure and personal-info results, without fabricating numbers/rules when search or lookup returns nothing.
4. **Required citations / source links**
   - Verify that every factual or personal piece of information in the final answer includes a citation/link (such as a link to the HR system for vacation balance or the source procedure document), grounded in the retrieved tool outputs rather than invented.
5. **Final-answer disclaimer evaluation**
   - Verify there is always some disclaimer in the final answer.
6. **Operational & cost visibility**
   - Track tool-call loop behavior in Model 1, peak latency and throttling on month-end / month-start spikes, and PayGo cost across both models and traffic periods.
