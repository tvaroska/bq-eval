---
type: reference
related:
  - "layers.md"
  - "silver.md"
  - "open-questions.md"
  - "use-cases/corporate-self-service.md"
  - "use-cases/customer-support.md"
  - "use-cases/knowledge-worker.md"
  - "use-cases/document-extraction.md"
  - "use-cases/regulated-action-workflow.md"
tags: [bq-eval, use-cases]
---

# Use Cases

> Concrete agent workloads that BQ Eval must support. Each note is status-free and solution-free: it captures only the agent topology, serving and traffic profile, telemetry inputs, and what must be evaluated, monitored, or cost-attributed.

| # | Use Case | Topology & Models | Serving & Traffic | Key Checks & Alerts |
|---|---|---|---|---|
| 1 | [Corporate self-service agent](use-cases/corporate-self-service.md) | 2 MCP tools (procedures search, personal info); Model 1 tool-call loop (`gemini-3.5-flash-lite`) -> Model 2 answer (`gemini-3.8-flash`) | GCP only, PayGo only; near-zero off-hours, steady working-hours baseline, massive peak on last and first day of month | `< 1m` warning if `user.id` missing (circuit breaker); strictly self-only tool access (`tool user_id == requester user.id`); rubric-based accuracy; source citation/link per claim (e.g., HR system); disclaimer in final answer |
| 2 | [Customer support agent](use-cases/customer-support.md) | Multi-agent: 1 orchestrator + multiple specialist agents and tools, including code generation and sandboxed execution | Multi-turn email channel with agent/customer follow-ups; `<= 1m` ack/reply SLA, `<= 24h` closure SLA; models/capacity open (needs SLO-keeping recommendations) | Pre-ticket irrelevant-question rejection + auto-reply with reason; missing-runbook human escalation; answer accuracy with citations & relevancy; sandbox safety/limits; strict cost visibility & SLO recommendations |
| 3 | [General knowledge worker agent](use-cases/knowledge-worker.md) | Single managed corporate assistant across open-ended knowledge, coding, and tool-assisted tasks | Interactive multi-turn sessions with variable context growth and caching; models and serving tier flexible | End-to-end & time-to-first-token latency; cost per user (plus context-growth & cache efficiency, optional org rollup); secrets & PII leak detection; thumbs up/down feedback; conversation length (user turns); tool loops |
| 4 | [High-volume document extraction](use-cases/document-extraction.md) | Pure document management & extraction; 2-tier model cascade (primary LLM -> stronger LLM on failure or low model confidence) | **Batch**, massive volumes of **PDFs (few to 250 pages)**; cost is primary driver | Stratified LLM eval + confidence-gated user review; evaluate whether cheaper models achieve **lower total cost** (primary + escalation + eval); cost & escalation breakdown by document size |
| 5 | [Regulated action-taking workflow](use-cases/regulated-action-workflow.md) | Decision & write-capable agent executing state changes in systems of record with HITL approval gates | Asynchronous / pause-and-resume cases across approvals; models, cloud, and serving open | Policy & HITL approval-gate compliance (no unauthorized writes); idempotent / no duplicate mutations; blind human SME calibration for auditability; pre-release regression gate; full outcome cost (incl. rejects & escalations) |
