# BQ Eval

Agents fail quietly: the request succeeds while the answer is wrong, slow, or expensive. Copying traces into a separate evaluation tool duplicates storage, loses warehouse joins, and splits token spend from business outcomes.

BQ Eval evaluates agent telemetry in BigQuery where it already lands, organized into three layers plus clean **Ports, Adapters and Contracts**:

1. **Bronze (telemetry in place):** Trace spans, BigLake Hive-partitioned Parquet payloads in storage, and three VPC-SC-isolated `workflow_events` tables (systems of record, platform controls, applications) stay in the cloud and perimeter where they landed. Non-conforming SDKs, coding containers, and custom gateways normalize upstream via the Bronze / OTel Collector Adapter Port.
2. **Silver (the BigQuery core):** One logical SQL contract maps up to three accepted [telemetry shapes](specs/shapes.md), `workflow_events`, gateway `requests` (`_AllLogs`), and SCD-2 reference tables (`app_registry`, `tools`, `skill_registry`, `capacity_pools`, `capacity_allocations`, `allowed_models` synced via a Two-Speed CI/CD Dual-Registry Port) into published objects (`model_calls`, `requests`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`, `outcome_links` over single-pass internal `spans`). Silver runs as `light` single-pass table functions (`< 10` base scans/view) with inline V8 `LANGUAGE js` RFC 8785 JCS canonicalization + SQL `SHA256` (fixed-window views are thin wrappers; the high-volume `enterprise` profile is design only), isolates four datasets (`silver_internal`, `silver_ref`, `silver_open`, `silver_restricted`) via 2-hop routine authorization, CMEK, and a per-unit caller filter, defaults multi-cloud hubs to `US`/`EU` multi-region with strict per-domain location alignment, keeps text and raw ids in-perimeter, and carries zero prices, verdicts, or pass flags.
3. **Gold (optional detective `>= 5m` add-ons) & Adjacent Ports:** Separate add-ons for **evaluation** (deterministic SQL gates, reverse `action.committed` checks, bounded stateful session reconstruction, pinned independent 4-point judges via `AI.GENERATE_TABLE` or Preview `AI.EVALUATE_AGENT`, guardrail FP/FN audits, stratum-specific Rogan-Gladen + design-based joint CIs, `AC2` + binary cut-point agreement, PII-free GCS Object-Lock WORM snapshots + RTBF crypto-shredding, Optimizer Gold Export Port), **monitoring** (SLAs, loops, dynamic tool churn, `thought_signature`, `remote_fragment_pending`), and **cost** (per-attempt `1:N` `requests`, 3-pool `FOCUS 1.2` + Cloud Billing reconciliation, `INFORMATION_SCHEMA.JOBS`-reconciled warehouse compute, `CPVO`, `capacity` `v1.1` export), plus Split-SA `5–15m` staged Gold tables behind 2-hop Authorized Views (`agent_eval_internal`) for sub-second dashboards. Live synchronous blocking belongs to the **Preventive Request-Path Enforcement Port**; `< 5s` PR and optimizer sweep gates run through the **Local/CI Eval Runner Port** (`CI_ONLY`).

## Documentation

- **Requirements (`specs/`):** [Layers](specs/layers.md) · [Silver contract](specs/silver.md) · [Gold add-ons](specs/gold.md) · [Feature matrix](specs/features.md) · [Telemetry shapes](specs/shapes.md) · [Attributes & events](specs/attributes.md) · [Target use cases (`UC1`–`UC11`)](specs/use-cases.md) · [Clouds & regions extension](specs/clouds.md) · [Capacity & governance extension](specs/capacity.md) · [Open questions](specs/open-questions.md)
- **Architecture (`design/`):** [Overview](design/bq-eval.md) · [Layer dependencies](design/layers.md) · [Silver production](design/silver.md) · [Gold pipelines](design/gold.md) · [Capacity & governance](design/capacity.md) · [Multi-cloud & multi-region](design/clouds.md)
- **Governance & Plan:** [Critical rules](CRITICAL.md) · [Decisions (`ADR-001`–`ADR-050`)](DECISIONS.md) · [Roadmap](docs/roadmap.md)

## Release Status & Provenance (`v0.7.0-rc1`)

> **Review Cut (`v0.7.0-rc1`):** Ships single-domain GCP Silver core + capacity extension (`sql/silver/`, `22` JSON schemas), Silver-backed Gold Evaluation/Monitoring/Cost + Split-SA staging (`sql/core/`, `sql/finops/`, `bq-eval bootstrap --mode silver|gold`), inline V8 `LANGUAGE js` JCS + `< 10`-scan rollups, `< 5s` offline BigQuery-SQL runner (`bq_eval/runner/`), and Shape-3 upload hook (`bq_eval/upload/`). Legacy `v0.4.0` views and hand-written SQLite copies are retired.

- **Proven live on BigQuery (`benchmarks/results/m20_silver_gold_live_receipt.json`):** Batched `--mode gold --apply` & `--dry-run`, uncached Silver/Gold queries over recorded `_AllSpans` + `_AllLogs` + recorded Shape-3 OTLP trace, V8 JS JCS parity (`node scripts/verify_jcs_js_parity.mjs`), Split-SA staging, and throwaway restricted-SA 2-hop Authorized View isolation on `agent_eval_internal`.
- **Synthetic or unverified boundaries (disclosed upfront):** `15` of `17` Silver manifest fixtures (`requests`, `burst_metrics_1m`, `workflow_events_*`, `platform_spans`, registries; leaving `outcomes*` views synthetic-only) remain `SYNTHETIC_UNTIL_RECORDED`; **`0` blind dual-human SME sheets collected yet** (`passes_irr_gate` stays fail-closed until `M19-3` labels real semantic edge cases for final `v0.7.0`); org-level VPC-SC, live Cloud KMS CMEK, multi-cloud `ADR-046` (`M20-1`), and `enterprise` compaction (`M20-2`) remain unprobed/unbuilt ([open questions](specs/open-questions.md)).

## Quick Start

```bash
bash scripts/gate.sh
node scripts/verify_jcs_js_parity.mjs
uv run bq-eval bootstrap --mode gold --project-id demo-proj
uv run python -m bq_eval.runner.engine.loader --manifest tests/fixtures/silver/manifest.json --max-seconds 5.0
uv run python -m bq_eval.cli demo --html-out /tmp/bq_eval_scorecard.html
uv run python -m bq_eval.cli serve --port 8080
```
