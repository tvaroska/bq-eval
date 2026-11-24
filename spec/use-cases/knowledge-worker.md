---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 3: General Knowledge Worker Agent

> One managed corporate productivity assistant supporting arbitrary employee tasks — from drafting and research to coding and multi-step tool use — focused on end-to-end and first-token latency, per-user cost and cache/context efficiency, secrets and PII leak detection, thumbs up/down feedback, and conversation turn length.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | General-purpose corporate assistant for ad-hoc knowledge work (drafting, summarizing, research, Q&A, analysis, coding, and tool-assisted tasks). |
| **Deployment scope** | **One managed corporate assistant** serving internal employees across shared workspace, knowledge, and technical tools. |
| **Workflow & tasks** | Unconstrained and user-driven: multi-turn interactive conversations with wide variation in prompt length, context growth, prefix-cache reuse, and tool-call loops; no standardized transaction outcome. |
| **Identity** | User-scoped (`user.id` required to attribute spend and track data leaks per user; organizational/cost-center rollup is nice to have, not required). |
| **Cloud, models & serving** | Flexible / open within the managed corporate deployment. |

## Telemetry & Data Expectations

- **Per-user & conversation correlation**: Capture `user.id`, conversation/session ID, and turn index so metrics roll up per user and per conversation (number of user turns).
- **Latency, cache & token signals**: Capture both **end-to-end response latency** and **time to first token (TTFT)** for streaming answers, model identity, input/output/cached token counts (to track context growth and prefix-cache reuse), and tool-call loop counts.
- **Explicit quality signal**: Capture **thumbs up / thumbs down** user feedback linked to the turn/trace.
- **Inspectable content**: Preserve prompts, tool inputs/outputs, and responses in-cloud so leak checks can scan for secrets and PII.

## Required Checks, Alerts & Views

1. **Latency monitoring (end-to-end & first token)**
   - Monitor overall end-to-end turn latency and time-to-first-token (TTFT) across the assistant without requiring separate task-type splits.
2. **Cost per user & token efficiency (with optional org rollup)**
   - Report spend **per user** (total, average, and distribution across heavy vs. light users), surface cost drivers such as steep context growth and poor prefix-cache reuse in long sessions, and optionally roll up by department or cost center.
3. **Secrets & PII leak detection**
   - Detect exposure of **secrets** (credentials, API keys, tokens, pasted code secrets) and **PII** across user prompts, tool inputs/outputs, and final responses.
4. **User feedback & conversation length**
   - Track **thumbs up / thumbs down** rates and **conversation length measured in number of user turns** across sessions.
5. **Baseline execution health & tool loops**
   - Track failed requests, tool errors, and runaway multi-step tool loops across coding and open-ended tasks.
