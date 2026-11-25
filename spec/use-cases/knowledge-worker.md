---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 3: General Knowledge Worker Agent

> One managed corporate productivity assistant serving tens of thousands of employees across drafting, research, Q&A, large inline document uploads, async deep-research modes, and dynamically selected enterprise tools — focused on end-to-end and first-token latency, per-user cost and cache/context efficiency, DLP/guardrail observability, live A/B model migration gating, and user feedback. *(Enterprise software engineering and coding-agent SDLC workflows are covered separately in [Use Case 8](enterprise-devai.md).)*

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | General-purpose corporate assistant for ad-hoc knowledge work: drafting, summarizing, deep research, enterprise Q&A, analysis over large inline user-uploaded documents (`up to 100 MB`), and dynamically selected workspace/enterprise tools. |
| **Deployment scope** | **One flagship managed corporate assistant** serving tens of thousands of employees (`RL-3` / `RL-4`) behind a central LLM gateway. |
| **Workflow & tasks** | Unconstrained and user-driven: interactive multi-turn conversations and async deep-research jobs with wide variation in prompt length, inline attachment size, dynamic per-turn tool-catalog selection, context growth, prefix-cache reuse, and tool-call loops; no standardized transaction outcome. |
| **Identity & custody** | Gateway-attested `user.id` required to attribute spend and track data leaks per user (optional org/cost-center rollup via Gold directory join); supports in-cloud payload inspection (`ephemeral_sample`). Zero-retention traffic is kept in the text-free objects and is not scored. |
| **Cloud, models & serving** | Managed multi-model fleet routed through a central gateway across regional cache-pinned pools and global pools. |

## Telemetry & Data Expectations

- **Per-user, session & live migration correlation**: Capture attested `user.id`, `gen_ai.conversation.id`, `x-client-request-id`, `upstream_endpoint_location`, `thought_signature_present`, and live A/B migration keys (`bq_eval.shadow_of_trace_id`, `bq_eval.experiment_id`, `bq_eval.variant_id`) so interactive model migrations pair cleanly without pre-authored `case_id`s.
- **Large inline uploads & dynamic tool selection**: Emit `bq_eval.document.bytes`, `bq_eval.document.page_count`, and `bq_eval.attachment_sha256` from the Gateway Inline-Artifact Port (never buffering `100 MB` attachments in SDK telemetry hooks), plus lightweight open-safe tool-catalog signals (`tool_definitions_sha256`, `available_tool_count`, `selected_tool_names`).
- **Latency, cache & token signals**: Capture end-to-end latency, streaming TTFT (`gen_ai.response.time_to_first_chunk`), model identity, input/output/cached tokens (`5m`/`1h` cache-write splits), and tool-call loop counts.
- **Explicit feedback & guardrails**: Capture `feedback.thumbs_up` / `feedback.thumbs_down` with `bq_eval.feedback_reason`, plus structured `guardrail.intervention` events (`control_id`, `layer`, `action`) from inline DLP/safety filters.

## Required Checks, Alerts & Views

1. **Latency monitoring (interactive E2E, TTFT & async research)**
   - Monitor end-to-end latency and streaming TTFT across interactive turns separately from long-running async deep-research tasks.
2. **Cost per user, inline-upload cost & prefix-cache efficiency**
   - Report spend **per user** (total, average, heavy-tail distribution, optional org-unit rollup), surface context-growth and regional-vs-global prefix-cache cliff drops (`thought_signature_present`, `tool_definitions_sha256` churn), and track spend by inline attachment size band.
3. **Secrets, PII & guardrail intervention tracking**
   - Detect exposure of secrets and PII across turn deltas where stored, and audit structured inline DLP/safety `guardrail.intervention` verdicts (`control_id`, `layer`, `action`) — always separating safety refusals from runtime crashes (`app_failed`).
4. **User feedback & live interactive A/B model migration gating**
   - Track thumbs up/down rates (accounting for click-rate sampling bias, structured `feedback_reason`, and `thumbs_down` tail oversampling), conversation turn length, and **live paired A/B or shadow model migration evaluation** (`shadow_of_trace_id`, `experiment_id`, `variant_id`) across employee fleets.
5. **Baseline execution health & dynamic tool loops**
   - Track failed requests, tool errors, tool-retriever misses (via `available_tool_count` vs. `selected_tool_names`), and runaway multi-step tool loops.
