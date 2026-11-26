# BQ Eval

Agents fail quietly: the request succeeds while the answer is wrong, slow, or expensive. Copying traces into a separate evaluation tool duplicates storage, loses warehouse joins, and splits token spend from business outcomes.

BQ Eval evaluates agent telemetry in BigQuery where it already lands, organized into three layers plus clean **Ports, Adapters and Contracts**:

1. **Bronze (telemetry in place):** Trace spans, BigLake Hive-partitioned Parquet payloads in storage, and three VPC-SC-isolated `workflow_events` tables (systems of record, platform controls, applications) stay in the cloud and perimeter where they landed. Non-conforming SDKs, coding containers, and custom gateways normalize upstream via the Bronze / OTel Collector Adapter Port.
2. **Silver (the BigQuery core):** One logical SQL contract maps up to three accepted [telemetry shapes](specs/shapes.md), `workflow_events`, and SCD-2 reference tables (`app_registry`, `tools`, `skill_registry` synced via a Two-Speed CI/CD Dual-Registry Port) into published objects (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`, `outcome_links` over single-pass internal `spans`). Silver runs as `light` table functions of an explicit time window with no stored data (fixed-window views are thin wrappers; the high-volume `enterprise` profile is design only), enforces `silver_open` vs. `silver_restricted` IAM/CMEK isolation with a per-unit caller filter, defaults multi-cloud hubs to `US`/`EU` multi-region with strict per-domain location alignment, keeps text and raw ids in-perimeter, and carries zero prices, verdicts, or pass flags.
3. **Gold (optional detective `>= 5m` add-ons) & Adjacent Ports:** Separate add-ons for **evaluation** (deterministic SQL gates, reverse `action.committed` checks, bounded stateful session reconstruction, pinned independent 4-point judges, guardrail FP/FN audits, stratum-specific Rogan-Gladen + design-based joint CIs, `AC2` + binary cut-point agreement, PII-free GCS Object-Lock WORM snapshots + RTBF crypto-shredding, Optimizer Gold Export Port), **monitoring** (SLAs, loops, dynamic tool churn, `thought_signature`, `remote_fragment_pending`), and **cost** (per-attempt `1:N` `requests`, 3-pool `FOCUS 1.2` + Cloud Billing reconciliation, `INFORMATION_SCHEMA.JOBS`-reconciled warehouse compute, `CPVO`, `capacity` export). Live synchronous blocking belongs to the **Preventive Request-Path Enforcement Port**; `< 5s` PR and optimizer sweep gates run through the **Local/CI Eval Runner Port** (`CI_ONLY`).

## Documentation

- **Requirements (`specs/`):** [Layers](specs/layers.md) · [Silver contract](specs/silver.md) · [Gold add-ons](specs/gold.md) · [Feature matrix](specs/features.md) · [Telemetry shapes](specs/shapes.md) · [Attributes & events](specs/attributes.md) · [Target use cases (`UC1`–`UC11`)](specs/use-cases.md) · [Clouds & regions extension](specs/clouds.md) · [Capacity & governance extension](specs/capacity.md) · [Open questions](specs/open-questions.md)
- **Architecture (`design/`):** [Overview](design/bq-eval.md) · [Layer dependencies](design/layers.md) · [Silver production](design/silver.md) · [Gold pipelines](design/gold.md) · [Capacity & governance](design/capacity.md) · [Multi-cloud & multi-region](design/clouds.md)
- **Governance & Plan:** [Critical rules](CRITICAL.md) · [Decisions (`ADR-001`–`ADR-048`)](DECISIONS.md) · [Roadmap](docs/roadmap.md)

## Quick Start

> **Note:** The single-domain GCP Silver core (`sql/silver/`, `bq_eval/runner/`, `bq_eval/upload/`, `bq-eval bootstrap --mode silver`) is implemented alongside the previous-generation `v0.4.0` Gold/demo SQL views while Milestone `M19` ([roadmap](docs/roadmap.md)) migrates Gold add-ons onto Silver.

```bash
bash scripts/gate.sh
uv run bq-eval bootstrap --mode silver --project-id demo-proj
uv run python -m bq_eval.cli demo --html-out /tmp/bq_eval_scorecard.html
uv run python -m bq_eval.cli serve --port 8080
```
