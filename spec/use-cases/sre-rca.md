---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 7: Agentic SRE Root-Cause Analysis

> Incident-investigation agents querying multi-petabyte enterprise logging lakes and metric warehouses to diagnose Sev-1 outages and draft post-mortems — requiring authoritative `INFORMATION_SCHEMA.JOBS` warehouse-compute reconciliation, M:N incident correlation (`outcome_links`), bounded `enterprise` `MERGE` compaction, concrete slot circuit breakers during `5–10x` outage spikes, and delayed `T+7d..30d` post-mortem verification. *(Enterprise coding agents are covered separately in [Use Case 8](enterprise-devai.md).)*

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Triage live production incidents over multi-petabyte centralized log/trace warehouses (e.g., `15 PiB/mo` ingest, `3+ PiB` stored), correlate alerts, identify root causes, and draft incident post-mortems. |
| **Tools & downstream compute** | Warehouse SQL/log-query tools, observability/metric APIs, and incident-management APIs where downstream BigQuery `bytes_billed` and `slot_ms` can dwarf LLM token cost. |
| **Correlation & lifecycle** | **M:N incident correlation**: one production incident triggers multiple agent RCA runs, and one diagnostic trace spans multiple related alerts (linked via `outcome.linked` -> `outcome_links`), with authoritative `postmortem.verified` ground truth arriving `7–30 days` after the outage. |
| **Cloud & serving** | Multi-project storage vs. query warehouse estate (`US`/`EU` multi-region Hub default); `5–10x` outage traffic spikes (`RL-2`/`RL-3`) with gateway shedding (`Priority` vs. `FLEX`). |
| **Operational constraint** | BQ Eval compaction and judging must **never** starve live incident-response warehouse slots or rewrite multi-TiB partitions during a Sev-1 outage. |

## Telemetry & Data Expectations

- **Authoritative warehouse compute signals**: Emit `db.query.job_id`, `db.query.bytes_scanned`, `db.query.bytes_billed`, and `db.query.slot_ms` on warehouse query `tool_calls` so Gold reconciles actual billed scan/slot cost against `INFORMATION_SCHEMA.JOBS` (catching cancelled, cached, or sub-query slot burn rather than trusting self-reported tool spans alone).
- **M:N correlation & delayed post-mortem signals**: Emit primary `outcome.id` on spans, secondary incident links via `outcome.linked` in `events` (projected through `outcome_links`), and delayed authoritative `postmortem.verified` events in `workflow_events` (joined to persisted Gold RCA snapshots past `light` retention and calibration TTLs).
- **Loop & retry telemetry**: Emit execution-time RFC 8785 `bq_eval.args_sha256` on tool spans so identical runaway warehouse queries are caught without reading raw log payloads: inside one trace by the repeat number in `tool_calls_open`, across an investigation by Gold from the restricted views.

## Required Checks, Alerts & Views

1. **Full `CPVO` with authoritative `INFORMATION_SCHEMA.JOBS` & M:N `outcome_links` allocation**
   - Reconcile tool-span `db.query.job_id` against `INFORMATION_SCHEMA.JOBS` (`bytes_billed`, `slot_ms`), combine with LLM token spend, and allocate M:N linked incident costs across `outcome_links` (`1 / COUNT(linked_outcomes)` default or explicit weight).
2. **Bounded `enterprise` `MERGE` & concrete outage slot circuit breakers**
   - Require static target `DATE(start_time)` partition pruning on every `enterprise` `MERGE`, bounded `outcomes` lookback windows, per-query `max_bytes_billed` caps, and pre-flight `INFORMATION_SCHEMA.JOBS` reservation-utilization breakers shedding non-critical judging and capping tail-sampling fan-out during `5–10x` outage spikes.
3. **Query-retry oscillation & runaway-scan budgets**
   - Enforce per-investigation caps on repeated identical warehouse queries (`repeated_tool_call_count`) and cumulative `bytes_billed` per incident.
4. **Delayed `T+7d..30d` post-mortem truth reconciliation**
   - Join persisted Gold RCA snapshots to late-arriving authoritative `postmortem.verified` events to score root-cause accuracy and false-alarm rates weeks after the incident closes.
