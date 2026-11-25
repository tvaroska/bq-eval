---
type: atom
related:
  - "layers.md"
  - "silver.md"
  - "capacity.md"
  - "open-questions.md"
  - "../design/clouds.md"
tags: [bq-eval, silver, clouds, extension]
---

# Extension: more than one cloud

> What is added when telemetry lands in more than one cloud. The core contract in [silver.md](silver.md) describes one cloud and does not depend on this note.

## Rules

- Data stays in the cloud where it landed, and Silver is exposed there (views in `light` mode; incremental partitioned tables in `enterprise` mode) using the same logical SQL. SCD-2 reference tables replicate into each cloud.
- The hub is the cloud where cross-cloud monitoring, calibration aggregation, and cost add-ons run.
- **Hub Transfer Port (no live cross-region/cross-cloud view `JOIN`s):** Because BigQuery cannot directly `JOIN` BigQuery Omni views (e.g., `aws-us-east-1`) with GCP regional datasets (`US`/`us-east4`) in a single query, a scheduled **Hub Transfer Port** stages text-free open rows (`model_calls`, `outcomes`, `traces_open`, `tool_calls_open`, `events_open`, and `requests` with the [capacity extension](capacity.md)) plus text-free judge verdicts into the hub's region. None of those rows contain prompt, tool, event, or answer text.
- **Out-of-band KMS HMAC hashing:** In this extension `traces_open`, `tool_calls_open`, and `events_open` carry one-way `user_id_hash` and `tenant_id_hash` computed via an out-of-band **KMS HMAC** Port (never a static salt hardcoded inside SQL view DDL) so cross-cloud user rollups match consistently without allowing directory reversal by view readers.
- `spans`, `trace_payloads`, `traces`, `tool_calls`, and `events` stay strictly inside their home cloud and VPC-SC/CMEK perimeter. That covers text, tool arguments and results, event payloads, and raw `user.id` / `tenant_id`.
- **Split-trace correlation in the Hub:** When a trace starts in one cloud (or on-premises) and calls a model or tool in another cloud, each cloud publishes its local spans (`has_root_span = true` on the owning cloud; `has_root_span = false` flagged as a cross-cloud fragment on the remote cloud). The Hub Transfer Port correlates open-twin fragments on `(trace_id, parent_span_id)` without moving text so cross-cloud errors, `429`s, `requests`, and tool-write alerts are never silently orphaned.
- **In-Cloud Judge Port:** Text never leaves its home cloud. LLM judges and PII/leak scans run inside the cloud holding the text (using that cloud's native model endpoint — never cross-cloud `AI.GENERATE_TABLE` routing non-GCP text to GCP) and export only text-free verdict rows to the Hub.

What is still unverified about reading across clouds is listed in [open-questions.md](open-questions.md#clouds-extension).
