# Agent Instructions (`bq-eval`)

## 1. Purpose & Scope
`bq-eval` (**BQ Eval & FinOps**) is the standalone **BigQuery-Native Enterprise Agent Evaluation, Observability, and Cost Management Showcase (`v0.3.0`)**. It positions **BigQuery as a pure execution engine** over **Dual-Path Agent Telemetry** — **Light Capture (`BQAA` `agent_events`)** for 3-line ADK/LangGraph developer onboarding and **Full Capture (OpenTelemetry `_AllSpans`)** for multi-service/multi-cloud enterprise fleets — using a progressive **`Step 1 -> Step 2 -> Step 2.5 (Agentic Skill) -> Step 3 -> Open Extensions`** ladder centered on **USD Cost per Verified Outcome**.

For regulated banking Model Risk Management (`SR 11-7`, `SR 26-2 / OCC 2026-13`) playbooks, academic literature compendiums, and cross-workspace compliance auditing, see sibling repository `agent-evals`.

Before editing this repository, read:
1. [`CRITICAL.md`](CRITICAL.md) — Non-negotiable engineering, calibration, FinOps, and hygiene invariants.
2. [`DECISIONS.md`](DECISIONS.md) — Architectural Decision Records (`ADR-001` through `ADR-009`).
3. [`docs/roadmap.md`](docs/roadmap.md) — Showcase milestones and roadmap.

---

## 2. Architecture & Documentation Map

| File / Module | Responsibility |
| :--- | :--- |
| [`README.md`](README.md) | Dual-path pitch, progressive `Step 1 -> Step 2 -> Step 2.5 -> Step 3` onboarding guide, measured live receipts, and runnable `bq-eval demo` quickstart. |
| [`.stitch/DESIGN.md`](.stitch/DESIGN.md) | Tier-2 GM3 Light visual contract (`--surface` tokens, `tabular-nums`, `prefers-reduced-motion`, 4-tab workbench layout, and `Do Not Generate` guards). |
| [`docs/bqaa-and-dual-path-architecture.md`](docs/bqaa-and-dual-path-architecture.md) | **Dual-Path Capture -> Single Engine Reference**: **Light Capture (`BQAA` `agent_events`)** vs. **Full Capture (`OTel` `_AllSpans`)**, App Hub linkage, `Agent Runtime` / `Cloud Run` / `Non-GCP` matrix, and `AI.EVALUATE_AGENT` integration. |
| [`docs/architecture-and-sql.md`](docs/architecture-and-sql.md) | Dual-path ingestion modes, Step 1–3 SQL walkthrough, portable App ID resolution (`EVAL-P08`), XML injection defense (`EVAL-P02`), Quadratic Gwet's `AC2` (`EVAL-P03`), conditional Rogan-Gladen SQL (`EVAL-P04`), and `outcome.id` attribution (`EVAL-P11`). |
| [`docs/finops-and-cost-management.md`](docs/finops-and-cost-management.md) | **USD Cost per Verified Outcome (`CPVO`)**, 4-bucket `FinOpsTrafficType` attribution, Eval FinOps ROI (`90–95%` savings), `>200K` burndown cliffs, Zero-Fabrication Pareto right-sizing, and `FOCUS 1.2` Two-Pool Chargeback + Off-Peak PT Valley Water-Filling. |
| [`docs/roadmap.md`](docs/roadmap.md) | Phased showcase delivery roadmap (`M8`, `v0.4.0`; `v0.1.0`–`v0.3.0` in archive). |
| [`benchmarks/results/m2_live_receipt.json`](benchmarks/results/m2_live_receipt.json) | Sanitized `343`-case `m2-live-20261113b` fleet receipt (`28,752` spans, `443` outcomes) + [`benchmarks/results/SHA256SUMS`](benchmarks/results/SHA256SUMS) manifest replayed by default in `bq-eval demo` and `bq-eval serve`. |
| [`sql/bqaa_adapter_views.sql`](sql/bqaa_adapter_views.sql) | **Light Capture (`BQAA`) & Hybrid Adapter Views**: Zero-copy `v_bqaa_otel_spans` (context-aware `TOOL_ERROR` status), `v_bqaa_trace_payloads` (chronological `apphub_application_id`), `v_bqaa_eval_turn_filtered`, and Light / Full / Hybrid switchboard. |
| [`sql/demo_time_shift_loadgen.sql`](sql/demo_time_shift_loadgen.sql) | **Demo Time-Shift Load Generator (`agent_eval_seed`)**: Seeds 16 synthetic Golden Trace Templates (`33` spans/replica = `1,650` spans at 50 replicas) into isolated dataset `agent_eval_seed`. |
| [`sql/step1_trace_views.sql`](sql/step1_trace_views.sql) | **Step 1 (`0` New Tables, No Skill)**: Domain-agnostic static SQL views (`v_step1_trace_waterfall`, `v_step1_trace_eval`, `v_step1_cost_per_outcome`, `v_outcomes`, `v_infra_cost_estimate`, `v_cost_per_verified_outcome`, `v_attribution_coverage`, `v_skill_lineage_roi`, `v_otel_spans_coverage`). |
| [`sql/step2_rubrics.csv`](sql/step2_rubrics.csv) | **Step 2 (`1` Rubric Table/CSV)**: 8 canonical 4-point ordinal rubrics (`groundedness_4pt_v1`, `tool_trajectory_4pt_v1`, `completeness_4pt_v1`, `escalation_policy_4pt_v1`, `kyc_policy_compliance_4pt_v1`, `rfp_quote_completeness_4pt_v1`, `contradiction_grounding_4pt_v1`, `pii_perturbation_invariance_4pt_v1`). |
| [`sql/step2_stateless_eval.sql`](sql/step2_stateless_eval.sql) | **Step 2 (Stateless Dual-Engine Eval, No Skill)**: `agent_eval.rubrics` setup (`8` rubrics) + **Engine A (`AI.GENERATE_TABLE`)** and **Engine B (`AI.EVALUATE_AGENT`)** queries on 100% and 10% trace slices. |
| [`skills/bq-eval/SKILL.md`](skills/bq-eval/SKILL.md) | **Step 2.5 (Agentic Skill)**: Defines new 4-point rubrics from plain English and generates custom slice queries (`5%`, `last 1 hour`, Engine A/B) via [`skills/bq-eval/references/sql_recipes.md`](skills/bq-eval/references/sql_recipes.md) + fail-closed [`skills/bq-eval/EVAL.yaml`](skills/bq-eval/EVAL.yaml). |
| [`sql/step3_saved_results.sql`](sql/step3_saved_results.sql) | **Step 3 (Saved Eval Results)**: Persists Engine A and Engine B verdicts into `agent_eval.eval_results` with deduplicated `v_step3_eval_runs` and temporally ordered `v_step3_paired_run_comparison`. |
| [`sql/step3_daily_quota_guard.sql`](sql/step3_daily_quota_guard.sql) | **Step 3 Daily Quota Guard**: `eval_daily_quota_config`, `v_step2_daily_quota_status`, and token-budgeted scheduled Step-2 judge query template. |
| [`sql/demo_docubridge_extensions.sql`](sql/demo_docubridge_extensions.sql) | **Optional Reference Demo Extensions**: DocuBridge WORM storage proxies (`v_demo_infra_cost_estimate`, `v_demo_cost_per_verified_outcome`, `v_demo_skill_lineage_roi`) and KYC trap-recall view (`v_m2_kyc_trap_eval`). |
| [`sql/ddl.sql`](sql/ddl.sql) | BigQuery DDL: Full Capture compatibility views `agent_eval.otel_spans` and `agent_eval.trace_payloads`, physical tables (incl. `app_registry.is_external_counterparty`), seeded `model_pricing_catalog`, and remote judge models. |
| [`sql/views.sql`](sql/views.sql) | Enterprise BigQuery views (`v_trace_waterfall`, `v_trace_sql_evaluation`, `v_enterprise_cost_per_verified_outcome`, `v_counterparty_spend`, `v_agent_eval_and_finops_summary`, and `capacity`-aligned `v_capacity_hourly_burndown_export`). |
| [`scripts/bq_dry_run.py`](scripts/bq_dry_run.py) | `sqlglot`-tokenized BigQuery `bq query --dry_run` validator across `sql/*.sql` and Python-generated SQL builders. |
| [`bq_eval/contract/`](bq_eval/contract/__init__.py) | Frozen, `allow_inf_nan=False` Pydantic v2 contracts across [`otel.py`](bq_eval/contract/otel.py), [`pricing.py`](bq_eval/contract/pricing.py) (single `DEFAULT_MODEL_SKUS`), [`rubrics.py`](bq_eval/contract/rubrics.py) (8 `CANONICAL_RUBRICS`), and [`results.py`](bq_eval/contract/results.py). |
| [`bq_eval/engine/`](bq_eval/engine/__init__.py) | Pure-stdlib deterministic engine across [`calibration.py`](bq_eval/engine/calibration.py) (`StratPPI`, Rogan-Gladen, Gwet's `AC2`), [`finops.py`](bq_eval/engine/finops.py) (single `compute_span_cost_usd()`, hourly & window `FOCUS 1.2`, valley water-filling), [`bqaa.py`](bq_eval/engine/bqaa.py), [`sql_eval.py`](bq_eval/engine/sql_eval.py), and [`sqlite_views.py`](bq_eval/engine/sqlite_views.py). |
| [`bq_eval/showcase/`](bq_eval/showcase/__init__.py) | GM3 Light workbench and corpus replay across [`pipeline.py`](bq_eval/showcase/pipeline.py), [`view.py`](bq_eval/showcase/view.py), [`renderer.py`](bq_eval/showcase/renderer.py) (`_esc()` XSS guard, 0 engine imports), [`loadgen.py`](bq_eval/showcase/loadgen.py), [`seeds_banking.py`](bq_eval/showcase/seeds_banking.py), and [`seeds_claims.py`](bq_eval/showcase/seeds_claims.py). |
| [`bq_eval/cli/`](bq_eval/cli/__init__.py) | Driving CLI ([`app.py`](bq_eval/cli/app.py): `--version`, `demo [--corpus|--scenario-file|--format json]`, `serve [--port 8080]`, `loadgen`, `judge-sql`) and loopback HTTP server ([`server.py`](bq_eval/cli/server.py)). |
| [`tests/`](tests/test_sql_contracts.py) | `111` tests across [`conftest.py`](tests/conftest.py), SQL AST/SQLite parity (`test_sql_contracts.py`), critical paths, end-to-end pins & `F7`, `M7` workbench/wire contracts (`test_m7_workbench_and_contracts.py`), doc contract, folder-level AST isolation (`test_isolation_guard.py`), and hygiene (`standards_debt.json = 0`). |

---

## 3. Canonical Verification Commands

Run AST checks, lock check, formatting, strict lint/typing, unit tests, and the runnable showcase demo before committing:
```bash
ast-grep scan --rule specs/ast.rules.yaml
uv lock --check
ruff format --check bq_eval tests examples scripts
ruff check --no-cache bq_eval tests examples scripts
mypy --strict bq_eval tests examples scripts
pytest -p no:cacheprovider -v
python3 -m bq_eval.cli demo
```
