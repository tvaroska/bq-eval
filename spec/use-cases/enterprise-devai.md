---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 8: Enterprise DevAI & Ephemeral Coding Containers

> Coding agents (across multiple SDKs including `Claude Agent SDK` and IDE/CLI harnesses) operating inside ephemeral developer containers to edit repositories, run builds/tests, and clear CI/SAST gates at `300x/day` release velocity — requiring a `< 5s` Local/CI Eval Runner Port, zero-ceremony container Collector adapters, a clean split between workspace edits and regulated system-of-record writes, SDLC trajectory metrics, and container CPU/memory attribution.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Author code changes, refactorings, unit/integration tests, and security fixes inside ephemeral developer containers and CI pipelines, culminating in merged PRs or accepted IDE edits. |
| **Topology & SDKs** | Multi-turn coding trajectories across diverse agent harnesses (ADK, `Claude Agent SDK`, IDE/CLI agents) invoking file-patch tools, language servers, compilers, test runners, and SAST scanners inside shared multi-turn ephemeral containers. |
| **Execution & latency speeds** | Two distinct loops (`RL-4`/`RL-5`): (1) **Fast local / pre-merge CI loop (`< 5s`)** executing deterministic Silver SQL + `Rule 0` gates locally via the **Local/CI Eval Runner Port** (`CI_ONLY`) without polling BigQuery or blocking on SME floors; (2) **Asynchronous fleet analytics** aggregating merged/abandoned PR efficiency, revert rates, and container spend in BigQuery. |
| **Registry velocity** | High-velocity (`300x/day`) feature-branch tool/skill iteration synced automatically via the fast lane of the **Two-Speed CI/CD Dual-Registry Ingestion Port** (additive `RL-4`/`RL-5` and `modifies_workspace` tools never block on manual SCD-2 tickets). |
| **Workspace vs. SoR boundary** | Local file edits mark `tools.modifies_workspace = true` (distinct from `tools.writes_state = true`) so coding edits never trigger regulated SoR HITL SoD failures. |

## Telemetry & Data Expectations

- **Zero-ceremony container Collector adapter**: A sidecar/host OTel Collector Adapter Port auto-normalizes `Claude Agent SDK` and IDE/CLI spans into the General OTel GenAI shape and injects `user.id`, `gen_ai.response.id`, `gen_ai.response.time_to_first_chunk`, `workspace_session_id` (linking across ephemeral container restarts), `outcome.*`, and RFC 8785 `args_sha256` with zero agent-code boilerplate.
- **SDLC trajectory & container compute signals**: Emit `bq_eval.devai.diff_stats` (files/lines changed), `bq_eval.devai.test_summary` (pass/fail/flaky counts), `bq_eval.devai.sast_findings` (severity counts & fix loops), `tool_definitions_sha256` / `selected_tool_names` (MCP tool-retriever debugging), and container-level `bq_eval.sandbox.cpu_sec` + `bq_eval.sandbox.mem_gb_sec` (not wall-clock duration fallback that double-counts idle think time).
- **Standard Git/CI webhook ingestion**: A standard **Git/CI-to-`workflow_events` Adapter** writes authoritative `ci.verified`, `test.failed`, `sast.blocked`, `code.accepted`, `code.reverted`, and `pr.abandoned` (catching silent local branch discard — the primary DevAI failure mode) into `workflow_events`.

## Required Checks, Alerts & Views

1. **Sub-5-second Local/CI paired regression gate (`CI_ONLY`)**
   - Execute Silver SQL + deterministic Gold `Rule 0` + build/test/SAST checks locally in `< 5s` over captured OTLP traces on every PR or agent harness change, tagged `CI_ONLY` so it is never conflated with SME-calibrated production metrics.
2. **Authoritative SDLC quality & silent-abandonment tracking**
   - Verify DevAI outcomes strictly on authoritative `ci.verified`, `code.accepted`, and clean SAST gates (never self-reported agent pass); track inline accept-vs-revert ratios, SAST fix-loop counts, flaky vs. agent-induced test failures, and `pr.abandoned` discard rates.
3. **Edit-test oscillation & dynamic MCP tool-churn budgets**
   - Detect runaway edit-compile-test loops via repeated identical tool calls and monitor `tool_definitions_sha256` churn driving prefix-cache invalidation.
4. **Full DevAI `CPVO` per merged PR**
   - Combine LLM token spend (including abandoned local branches and retries) with container `cpu_sec` + `mem_gb_sec` and divide by authoritatively merged/accepted PRs.
