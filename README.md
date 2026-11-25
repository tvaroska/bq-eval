# BQ Eval

Agents fail quietly: the request succeeds while the answer is wrong, slow, or expensive. Copying traces into a separate evaluation tool duplicates storage, loses warehouse joins, and splits token spend from business outcomes.

BQ Eval evaluates agent telemetry in BigQuery where it already lands, organized into three layers:

1. **Bronze (telemetry in place):** Trace spans, referenced payloads in object storage, and the input table for records that are not spans (approvals, tickets, committed writes) stay in the cloud where they landed. Nothing is copied to make it readable.
2. **Silver (the BigQuery core):** SQL inside BigQuery maps up to three accepted [telemetry shapes](spec/shapes.md), the non-span input table, and SCD-2 reference tables (`app_registry`, `tools`) into six published views (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes` over single-pass internal `spans`). Silver runs in two deployment profiles — `light` (zero-copy views with zero scheduled jobs) and optional `enterprise` (incremental partitioned tables of text-free facts with late-arrival upsert) — enforces dataset-level IAM isolation (`silver_open` vs. `silver_restricted`) with BigLake Connections for referenced payloads, counts missing fields instead of inventing defaults, keeps text and raw ids inside their home cloud, and carries zero prices, judge scores, or pass flags.
3. **Gold (optional add-ons):** Separate add-ons for **evaluation** (sampled 4-point ordinal rubrics, stratum-weighted Rogan-Gladen bias correction with Lang & Reiczigel 95% intervals, Gwet's `AC2`, paired non-regression gates, immutable WORM audit snapshots), **monitoring** (latency, loops, throttling, guardrail interventions, rootless write safety), and **cost** (span-cliff pricing, reserved-capacity attribution, cost per verified outcome) read only the published Silver contract and define their own pass.

## Documentation

- **Requirements (`spec/`):** [Layers](spec/layers.md) · [Silver contract](spec/silver.md) · [Gold add-ons](spec/gold.md) · [Feature matrix](spec/features.md) · [Telemetry shapes](spec/shapes.md) · [Attributes & events](spec/attributes.md) · [Target use cases](spec/use-cases.md) · [Clouds extension](spec/clouds.md) · [Capacity extension](spec/capacity.md) · [Open questions](spec/open-questions.md)
- **Architecture (`design/`):** [Overview](design/bq-eval.md) · [Layer dependencies](design/layers.md) · [Silver production](design/silver.md) · [Gold pipelines](design/gold.md) · [Capacity & governance](design/capacity.md) · [Multi-cloud residency](design/clouds.md)
- **Governance & Plan:** [Critical rules](CRITICAL.md) · [Decisions (`ADR-001`–`ADR-029`)](DECISIONS.md) · [Roadmap](docs/roadmap.md)

## Quick Start

> **Note:** `sql/` and `bq_eval/` currently run the `v0.4.0` previous-generation SQL views and local SQLite demo engine while Milestones `M18`–`M19` ([roadmap](docs/roadmap.md)) implement the Silver core and migrate the Gold add-ons onto a single BigQuery SQL source.

```bash
bash scripts/gate.sh
uv run python -m bq_eval.cli demo --html-out /tmp/bq_eval_scorecard.html
uv run python -m bq_eval.cli serve --port 8080
```
