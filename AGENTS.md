# Agent Instructions (`bq-eval`)
<!-- doc-tier: full -->

## 1. Purpose & Scope
`bq-eval` (**BQ Eval**) builds a **Bronze and Silver telemetry core in BigQuery** with optional **Gold add-ons** (evaluation, monitoring, and cost) on top ([`specs/layers.md`](specs/layers.md), [`design/bq-eval.md`](design/bq-eval.md)). Telemetry stays in the cloud and perimeter where it landed (multi-region `US`/`EU` Hub default with strict per-domain location/CMEK alignment); Silver normalizes up to three accepted [telemetry shapes](specs/shapes.md), three VPC-SC-isolated `workflow_events` tables (systems of record, platform controls, applications), and SCD-2 reference tables (`app_registry`, `tools`, `skill_registry` synced via a Two-Speed CI/CD Dual-Registry Port) into published views (`model_calls`, `tool_calls`, `events`, `trace_payloads`, `traces`, `outcomes`, `outcome_links` over single-pass internal `spans`, split across `silver_open` and `silver_restricted` with split compaction SAs and a per-unit caller filter, running as `light` table functions of an explicit time window, with the high-volume `enterprise` profile design only) that hold counts and facts only: zero prices, zero fabricated defaults, and no pass. Gold add-ons run detective `>= 5m` audits (with PII-free GCS Object-Lock WORM snapshots + RTBF crypto-shredding, concrete slot breakers, and versioned exports to `capacity`), while synchronous blocking lives in the Preventive Request-Path Enforcement Port and `< 5s` PR/sweep checks use the Local/CI Eval Runner Port (`CI_ONLY`). More than one cloud/region ([`specs/clouds.md`](specs/clouds.md)) and capacity and governance ([`specs/capacity.md`](specs/capacity.md)) are modular extensions.

> **Transition state (`docs/roadmap.md`):** Code under `sql/` and `bq_eval/` currently reflects the `v0.4.0` previous-generation views and hand-written SQLite copy. Milestone `M18` (`v0.6.0`) builds Silver in BigQuery; `M19` (`v0.7.0`) moves Gold add-ons onto Silver and deletes the legacy dual view chains and SQLite copy.

### Sibling Repository Boundaries
- **`bq-eval` (This Repo)**: Bronze-to-Silver SQL adapters and published Silver objects in BigQuery, plus Gold evaluation (4-point rubrics, stratum-specific Rogan-Gladen calibration, `AC2` + binary cut-point agreement, paired/live A-B regression gates), monitoring, and cost (`CPVO`, `>200K` cliffs, 3-pool `FOCUS 1.2` chargeback + Cloud Billing reconciliation) add-ons. Other repositories never name `bq-eval` views.
- **`agent-evals`**: Normative cross-repo evaluation standards (`EVAL-P01`–`EVAL-P11`), regulated banking Model Risk Management (`SR 11-7`, `SR 26-2 / OCC 2026-13`) playbooks, `RAIR` / `RSR` / `60-40` audit governance, academic compendiums, and cross-workspace compliance auditing (`eval-audit`).
- **`capacity`**: GSU/GPU capacity sizing optimization, sub-minute 3P burst & quota-cliff physics, gateway YAML/CEL routing/shedding, idle pool reallocation UIs, and `FOCUS 1.2` billing actuation over exported burndown payloads.

Before editing this repository, read:
1. [`CRITICAL.md`](CRITICAL.md) — Non-negotiable Silver/Gold, calibration, cost, boundary, and hygiene invariants.
2. [`DECISIONS.md`](DECISIONS.md) — Architectural Decision Records (`ADR-001` through `ADR-046`).
3. [`docs/roadmap.md`](docs/roadmap.md) — Live milestone status (`M16`–`M19`) and review backlog.

---

## 2. Architecture & Repository Map

| File / Module | Responsibility |
| :--- | :--- |
| [`specs/`](specs/layers.md) | **The what**, status-free: Bronze, Silver, and Gold layers ([`layers.md`](specs/layers.md)), Silver contract ([`silver.md`](specs/silver.md)), Gold add-ons ([`gold.md`](specs/gold.md)), capability & use-case matrix ([`features.md`](specs/features.md)), three accepted telemetry shapes & Collector/Gateway Ports ([`shapes.md`](specs/shapes.md)), standard-first attribute and event contract ([`attributes.md`](specs/attributes.md)), 11 target workload use cases ([`use-cases.md`](specs/use-cases.md) and [`use-cases/`](specs/use-cases/corporate-self-service.md)), the two extensions ([`clouds.md`](specs/clouds.md), [`capacity.md`](specs/capacity.md)), and empirical open items ([`open-questions.md`](specs/open-questions.md)). |
| [`design/`](design/bq-eval.md) | **The how**, status-free: overview ([`bq-eval.md`](design/bq-eval.md)), layers and dependency rule ([`layers.md`](design/layers.md)), Silver production ([`silver.md`](design/silver.md)), Gold add-on pipelines & `capacity` export ([`gold.md`](design/gold.md)), capacity & governance ([`capacity.md`](design/capacity.md)), and multi-cloud / multi-region execution ([`clouds.md`](design/clouds.md)). Status lives only in [`docs/roadmap.md`](docs/roadmap.md). |
| [`README.md`](README.md) | Three-layer overview, documentation index, and local verification commands. |
| [`docs/roadmap.md`](docs/roadmap.md) | Sole live status tracker: `v0.5.0` (`M16` live proof + `M17` blind dual-human SME calibration), `v0.6.0` (`M18` Silver core), and `v0.7.0` (`M19` Gold add-ons on Silver & review fixes), plus [`docs/archive/roadmap-history.md`](docs/archive/roadmap-history.md). |
| [`benchmarks/results/`](benchmarks/results/SHA256SUMS) | Committed previous-generation `m2_live_receipt.json`, `m16_live_cloud_proof.json`, `m16_anonymized_traces_100.jsonl`, `bq_dry_run_receipt.json`, and `SHA256SUMS` manifest. |
| [`sql/silver/schema/`](sql/silver/schema/labels_registry.json), [`sql/core/`](sql/core/ddl.sql), [`sql/finops/`](sql/finops/capacity_views.sql), [`sql/demo/`](sql/demo/demo_time_shift_loadgen.sql) | Committed Silver core BigQuery column schemas & registered `labels` key catalog (`ADR-045`), plus previous-generation (`v0.4.0`) BigQuery DDL, compatibility views (`otel_compat_views.sql`, `bqaa_adapter_views.sql`), `8` canonical 4-point ordinal rubrics (`step2_rubrics.csv`, `step2_stateless_eval.sql`), saved eval & quota-guard procedures (`step3_saved_results.sql`, `step3_daily_quota_guard.sql`), trace/summary views (`step1_trace_views.sql`, `views.sql`, `summary_views.sql`), FinOps/Omni views, and seed loadgen (slated for Silver/Gold replacement in `M18`–`M19`). |
| [`terraform/`](terraform/main.tf) & [`Dockerfile`](Dockerfile) | Standalone (`create_* = true`) / co-located (`create_* = false`, `power_policy = hibernate`) single-project Terraform + `terraform/tests/*.tftest.hcl`, non-root `UID 10001` container (`HEALTHCHECK /healthz`), and Trivy gate in [`cloudbuild.yaml`](cloudbuild.yaml). |
| [`scripts/`](scripts/gate.sh) | Canonical [`gate.sh`](scripts/gate.sh), shared-bootstrap BigQuery dry-run validator ([`bq_dry_run.py`](scripts/bq_dry_run.py)), empirical proof validator ([`empirical_proof.py`](scripts/empirical_proof.py)), live `M16` runner ([`run_m16_live_proof.py`](scripts/run_m16_live_proof.py), [`m16_vertex_otel_probe.py`](scripts/m16_vertex_otel_probe.py)), and blind dual-human SME kit ([`blind_sme_kit.py`](scripts/blind_sme_kit.py)). |
| [`bq_eval/core/`](bq_eval/core/__init__.py) | Rule checks, judge SQL ([`sql_eval.py`](bq_eval/core/engine/sql_eval.py)), calibration ([`calibration.py`](bq_eval/core/engine/calibration.py)), per-span cost ([`cost.py`](bq_eval/core/engine/cost.py)), legacy SQLite copy ([`sqlite_twin.py`](bq_eval/core/engine/sqlite_twin.py), [`sqlite_views.py`](bq_eval/core/engine/sqlite_views.py)), capture helpers, `EvalBackend` / `WarehouseEvalBackend` ports ([`ports.py`](bq_eval/core/engine/ports.py)), and contracts (`8` `DEFAULT_MODEL_SKUS` and `8` `CANONICAL_RUBRICS` in [`rubrics.py`](bq_eval/core/contract/rubrics.py)). Imports no sibling subpackage. |
| [`bq_eval/finops/`](bq_eval/finops/__init__.py) | Judge ROI, cache dilution, Pareto frontier, and two-pool/three-pool chargeback ([`spend.py`](bq_eval/finops/engine/spend.py), [`chargeback_three_pool.py`](bq_eval/finops/engine/chargeback_three_pool.py), [`capacity_export.py`](bq_eval/finops/engine/capacity_export.py), [`sqlite_capacity.py`](bq_eval/finops/engine/sqlite_capacity.py)). Built on `core`. |
| [`bq_eval/deploy/`](bq_eval/deploy/__init__.py) | Bootstrap SQL rendering, offline validation ([`bootstrap.py`](bq_eval/deploy/engine/bootstrap.py)), and `bq` CLI query runner ([`bq_client.py`](bq_eval/deploy/engine/bq_client.py)). Built on `core`. |
| [`bq_eval/demo/`](bq_eval/demo/__init__.py) | Corpus replay and HTML workbench ([`bundles.py`](bq_eval/demo/bundles.py), [`pipeline.py`](bq_eval/demo/pipeline.py), [`loadgen.py`](bq_eval/demo/loadgen.py), [`golden_seeds.py`](bq_eval/demo/golden_seeds.py), [`render/`](bq_eval/demo/render/__init__.py)). Built on `core` and `finops`. |
| [`bq_eval/cli/`](bq_eval/cli/__init__.py) | Driving CLI ([`app.py`](bq_eval/cli/app.py), [`bootstrap_cmd.py`](bq_eval/cli/bootstrap_cmd.py), [`capacity_cmd.py`](bq_eval/cli/capacity_cmd.py)) and loopback HTTP server ([`server.py`](bq_eval/cli/server.py)). |
| [`tests/`](tests/conftest.py) | `193` tests across Silver schema contracts ([`test_silver_contract.py`](tests/test_silver_contract.py)), SQL AST/SQLite parity, calibration, cost, quota, ports, workbench, bootstrap/wheel/Terraform, folder AST isolation ([`test_isolation_guard.py`](tests/test_isolation_guard.py)), repo/link hygiene ([`test_repo_hygiene.py`](tests/test_repo_hygiene.py)), doc contracts ([`test_doc_contract.py`](tests/test_doc_contract.py)), and `800`-line source + test hygiene (`standards_debt.json = 0`). |

### Recording decisions

- A design decision confirmed in conversation is written in the same turn: the requirement in `specs/`, the way it is met in `design/`, and an entry in `DECISIONS.md`.
- What is still undecided goes to `specs/open-questions.md`, with the current leaning.
- `docs/roadmap.md` changes only with explicit approval.
- Leave the changes uncommitted unless asked, and run the gate.

---

## 3. Canonical Verification Commands

Run the canonical gate script (also invoked directly by [`cloudbuild.yaml`](cloudbuild.yaml), which adds `terraform test`, `docker build`, and `trivy` scan steps):
```bash
bash scripts/gate.sh
```
This checks `uv.lock`, syncs `.venv` against the lockfile, runs `.venv/bin/ast-grep scan --rule specs/ast.rules.yaml`, `ruff format --check`, `ruff check`, `mypy --strict`, `pytest -p no:cacheprovider -v -rs`, and all 9 CLI / example smoke runs (`demo`, `loadgen`, `judge-sql`, `bootstrap --mode light|full|hybrid`, `capacity-export`, `examples/run_showcase_demo.py`).
