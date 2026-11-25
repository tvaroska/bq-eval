---
type: reference
related:
  - "layers.md"
  - "silver.md"
  - "gold.md"
  - "features.md"
  - "open-questions.md"
  - "use-cases/corporate-self-service.md"
  - "use-cases/customer-support.md"
  - "use-cases/knowledge-worker.md"
  - "use-cases/document-extraction.md"
  - "use-cases/regulated-action-workflow.md"
  - "use-cases/front-office-advisory-optimizer.md"
  - "use-cases/sre-rca.md"
  - "use-cases/enterprise-devai.md"
  - "use-cases/stateful-fiduciary-advisory.md"
  - "use-cases/multi-cloud-regulated-fleet.md"
  - "use-cases/shared-llm-gateway.md"
tags: [bq-eval, use-cases]
---

# Use Cases

> Concrete agent and platform workloads that BQ Eval must support. Each note is status-free and solution-free: it captures only topology, serving and traffic profile, telemetry inputs, and what must be evaluated, monitored, or cost-attributed. See [features.md](features.md) for how each requirement is addressed across Silver, Gold, and the extensions.

| # | Use Case | Topology & Models | Serving & Traffic | Key Checks & Alerts |
|---|---|---|---|---|
| 1 | [Corporate self-service agent](use-cases/corporate-self-service.md) | 2 MCP tools (procedures search, personal info); Model 1 tool loop (`gemini-3.5-flash-lite`) -> Model 2 answer (`gemini-3.8-flash`) | GCP PayGo (`RL-3`); near-zero off-hours, steady daytime baseline, massive month-boundary peak | Preventive `< 1m` missing-`user.id` + `attempts=1` guard + 5m BQ audit; `token_enforced` OBO self-only check; `x-client-request-id` & `thought_signature`; TTFT; rubric accuracy; citations; disclaimer |
| 2 | [Customer support agent](use-cases/customer-support.md) | Multi-agent orchestrator + domain specialists + isolated code sandbox + warm human handoff | Multi-turn email/chat; `<= 1m` ack SLA, `<= 24h` closure SLA; open models/capacity (SLO recommendations) | `DKIM`/`DMARC` sender auth; pre-ticket `rejected` + auto-reply; missing-runbook escalation; inline DLP before external reply; detective sandbox audit; warm handoff packets; strict `CPVO` & SLO recommendations |
| 3 | [General knowledge worker agent](use-cases/knowledge-worker.md) | Single managed corporate assistant across drafting, research, Q&A, large inline uploads & dynamic tools (coding split to `UC8`) | Interactive multi-turn sessions (`RL-3`/`RL-4`); variable context/cache cliffs | End-to-end & TTFT latency; `100 MB` inline artifact metadata; dynamic tool-catalog churn; per-user cost & cache cliffs; structured `guardrail.intervention`; live A/B migration (`shadow_of_trace_id`) |
| 4 | [High-volume document extraction](use-cases/document-extraction.md) | Pure document extraction; 2-tier cascade (primary -> stronger LLM on failure or low confidence) | Agent batch / gateway-ingested PDFs (few to 250 pages), `FLEX` or off-peak PT; total cost is primary driver | `>200K` `(input + cache_read)` cliff pricing; stratified eval + ECE + confidence-gated review; worst-chunk `MIN(model_confidence)`; cheaper-model total-cost & `FLEX`-vs-idle-PT arbitrage; `outcome.parent_id` chaining |
| 5 | [Regulated action-taking workflow](use-cases/regulated-action-workflow.md) | Decision & write-capable agent mutating systems of record behind HITL approval gates | Asynchronous pause/resume (`RL-1`); private SoR `workflow_events` perimeter; multi-region Hub + GCS WORM | Preventive request-path HITL + detective SoD/notional quorums/TTL/single-use; reverse `action.committed -> tool_calls` check; `model.approved` MRM gate; stratified dual-SME `RL-1` (`>= 100`) calibration; PII-free GCS Object-Lock WORM + RTBF; `0.0%` regression gate |
| 6 | [Front-office advisory & continual optimization](use-cases/front-office-advisory-optimizer.md) | Hybrid cloud/on-prem advisory agent with multi-nudge structured outputs + offline skill optimizer (`400–800` rollouts) | Business-hours interactive PT (`RL-2`) + off-peak `optimize` sweeps across `train`/`holdout` splits | Authoritative `skill_registry` `protected_block_sha256` pin; `< 5s` Local/CI Runner Port for sweeps; `train`/`holdout` disjointness & `mounted_skills` isolation; per-`nudge_id` feedback; `min_cache_tokens` floor guard; delayed `T+1` conversion joins; PT isolation |
| 7 | [Agentic SRE root-cause analysis](use-cases/sre-rca.md) | Incident-investigation agents querying multi-PiB log/data warehouses & drafting post-mortems | M:N incident correlation (`outcome_links`); `5–10x` outage spikes on shared enterprise warehouse | Full `CPVO` reconciled via `db.query.job_id` / `INFORMATION_SCHEMA.JOBS` (`bytes_billed`/`slot_ms`); query-retry oscillation caps; bounded `enterprise` `MERGE` + concrete `max_bytes_billed` slot breakers; `T+7..30d` delayed `postmortem.verified` reconciliation |
| 8 | [Enterprise DevAI & ephemeral coding containers](use-cases/enterprise-devai.md) | Coding agents (multi-SDK incl. `Claude Agent SDK`) editing workspaces, running builds/tests/SAST inside ephemeral containers | High-velocity (`300x/day`) local dev & CI PR loops (`RL-4`/`RL-5`) | `< 5s` Local/CI Eval Runner Port (`CI_ONLY`); `tools.modifies_workspace` vs. `writes_state` split; `devai.diff_stats`/`test_summary`/`sast_findings` & container `cpu_sec`/`mem_gb_sec`; `code.accepted`/`reverted`/`pr.abandoned` events; `workspace_session_id` continuity |
| 9 | [Stateful fiduciary & wealth advisory](use-cases/stateful-fiduciary-advisory.md) | Long-lived cross-session coaching (`previous_interaction_id`, persistent memory, ACL-filtered vector retrieval, `Guide`/`Answer` flow) | Multi-week stateful sessions (`RL-1`) under strict client-PII isolation, RTBF & compliance controls | Bounded Gold session-context reconstruction (`MISSING_SESSION_CONTEXT`); memory-mutation lineage; `state.transition`; guardrail FP/FN stratified judging (`control_id`/`layer`/`action`); silent empty-RAG & numeric/prohibited-entity gates; per-tenant RAPs; PII-free GCS WORM + RTBF; `< 2%` cohort parity |
| 10 | [Multi-cloud regulated fleet governance](use-cases/multi-cloud-regulated-fleet.md) | Enterprise fleet spanning AWS, GCP, and on-premises across `RL-1`..`RL-5`, multiple `runtime_lane`s, and surveillance/AML/credit workloads | Multi-region `US`/`EU` Hub default + sovereign single-region/non-GCP spokes (Hub Transfer Port, In-Cloud Judge Port) | Two-Speed CI/CD Dual-Registry Sync + dark-agent reconciliation; MNPI `barrier_domain`; extended scoped `allowed_models` & EOL LOB exports; Pre-GA lane guards; per-cloud $Se/Sp$ + `RL-1` (`>= 100`) calibration; sibling non-LLM inference metric table |
| 11 | [Shared multi-tenant LLM gateway & bulk embeddings](use-cases/shared-llm-gateway.md) | 100%-gateway-mediated inference across 1P + 3P models (`200+` apps) + high-volume bulk `embeddings` indexing | Merged FCFS PT orders (`FIXED_TERM` + `AUTO_RENEW`) -> `1.8x` Priority PayGo -> Standard shedding (`RL-1`..`RL-5`) | Mandatory `x-client-request-id` `1:N` joins & caller `trace_owner_app_id` precedence; Option A/B `state_mode` neutrality; `100 MB` streaming + inline DLP; 3-pool `FOCUS 1.2` spillover + Cloud Billing invoice reconciliation; `order_id` pro-rata split; 3P `inferred_ambiguous`; `burst_metrics_1m` -> `capacity`; embeddings pool attribution |
