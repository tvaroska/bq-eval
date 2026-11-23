# Agent Instructions (`bq-eval`)

## 1. Purpose & Scope
`bq-eval` (**BQ Eval & FinOps**) is the standalone **BigQuery-Native Enterprise Agent Evaluation, Observability, and Cost Management Showcase (`v0.3.0`)**. It positions **BigQuery as a pure execution engine** over **Dual-Path Agent Telemetry** — **Light Capture (`BQAA` `agent_events`)** for 3-line ADK/LangGraph developer onboarding and **Full Capture (OpenTelemetry `_AllSpans`)** for multi-service/multi-cloud enterprise fleets — using a progressive **`Step 1 -> Step 2 -> Step 2.5 (Agentic Skill) -> Step 3 -> Open Extensions`** ladder centered on **USD Cost per Verified Outcome**.

### Sibling Repository Boundaries
- **`bq-eval` (This Repo)**: Telemetry capture adapters (`BQAA` `agent_events`, OTel `_AllSpans`, `EVAL-P11` `outcome.id`), progressive Step 1–3 BigQuery SQL evaluation (`AI.GENERATE_TABLE` & `AI.EVALUATE_AGENT`), statistical calibration (`StratPPI`, Rogan-Gladen, Gwet's `AC2`), multi-provider token/infra cost & `CPVO`, hourly PT burndown export, and `.stitch/DESIGN.md` GM3 Light showcase (`demo` / `serve`).
- **`agent-evals`**: Normative cross-repo evaluation standards (`EVAL-P01`–`EVAL-P11`), regulated banking Model Risk Management (`SR 11-7`, `SR 26-2 / OCC 2026-13`) playbooks, `RAIR` / `RSR` / `60-40` audit governance, academic literature compendiums, and cross-workspace compliance auditing (`eval-audit`).
- **`capacity`**: GSU/GPU capacity sizing optimization, sub-minute 3P burst & quota-cliff physics, gateway YAML/CEL routing, idle pool reallocation UIs, and live `FOCUS 1.2` billing actuation consuming `bq-eval`'s hourly burndown exports.

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
| [`docs/roadmap.md`](docs/roadmap.md) | Phased showcase delivery roadmap (`M8`, `M9`, `M10-1`/`M10-2` + `M10-3` plumbing, and `M11` implemented locally pending release; `M10-3` live dual-human SME + GCP proof pending; `v0.1.0`–`v0.3.0` in archive). |
| [`benchmarks/results/m2_live_receipt.json`](benchmarks/results/m2_live_receipt.json) | Reconstructed sanitized `343`-case `m2-live-20261113b` fleet bundle (`676` spans incl. `333` `outcome.closed` spans, `summary_provenance: reconstructed_from_bundle_spans`, `0` human SME labels) + [`benchmarks/results/SHA256SUMS`](benchmarks/results/SHA256SUMS) manifest replayed by default in `bq-eval demo` and `bq-eval serve`. |
| [`sql/bqaa_adapter_views.sql`](sql/bqaa_adapter_views.sql) | **Light Capture (`BQAA`) & Hybrid Adapter Views**: `39`-col zero-copy `v_bqaa_otel_spans` (context-aware `TOOL_ERROR`, `NULL` shadow `outcome_id`, serving traffic & session fields), `v_bqaa_trace_payloads`, `v_bqaa_eval_turn_filtered`, plus live Mode A (`v_mode_a_*`) and Hybrid Mode C (`v_mode_c_hybrid_*`) views. |
| [`sql/demo_time_shift_loadgen.sql`](sql/demo_time_shift_loadgen.sql) | **Demo Time-Shift Load Generator (`agent_eval_seed`)**: Seeds 16 synthetic Golden Trace Templates (`33` spans/replica = `1,650` spans at 50 replicas) into isolated dataset `agent_eval_seed`. |
| [`sql/step1_trace_views.sql`](sql/step1_trace_views.sql) | **Step 1 (`0` New Tables, No Skill)**: Domain-agnostic static SQL views (`v_default_model_pricing` across 8 1P/3P SKUs, `v_step1_trace_waterfall`, `v_step1_trace_eval`, `v_step1_cost_per_outcome`, `v_outcomes`, `v_infra_cost_estimate`, `v_cost_per_verified_outcome`, `v_attribution_coverage`, `v_skill_lineage_roi`, `v_otel_spans_coverage`). |
| [`sql/step2_rubrics.csv`](sql/step2_rubrics.csv) | **Step 2 (`1` Rubric Table/CSV)**: 8 canonical 4-point ordinal rubrics (`groundedness_4pt_v1`, `tool_trajectory_4pt_v1`, `completeness_4pt_v1`, `escalation_policy_4pt_v1`, `kyc_policy_compliance_4pt_v1`, `rfp_quote_completeness_4pt_v1`, `contradiction_grounding_4pt_v1`, `pii_perturbation_invariance_4pt_v1`). |
| [`sql/step2_stateless_eval.sql`](sql/step2_stateless_eval.sql) | **Step 2 (Stateless Dual-Engine Eval, No Skill)**: `agent_eval.rubrics` setup (`8` rubrics), parameterized `${CONNECTION_ID}` remote models, and XML-escaped prompt and slice views (stateless Engine A/B SQL emitted via `bq-eval judge-sql`). |
| [`skills/bq-eval/SKILL.md`](skills/bq-eval/SKILL.md) | **Step 2.5 (Agentic Skill)**: Defines new 4-point rubrics from plain English and generates custom slice queries (`5%`, `last 1 hour`, Engine A/B) via [`skills/bq-eval/references/sql_recipes.md`](skills/bq-eval/references/sql_recipes.md) + fail-closed [`skills/bq-eval/EVAL.yaml`](skills/bq-eval/EVAL.yaml). |
| [`sql/step3_saved_results.sql`](sql/step3_saved_results.sql) | **Step 3 (Saved Eval Results)**: `agent_eval.eval_results` table (`policy_sha256`, `amended_clause_ids`, `target_clause_id`) with deduplicated `v_step3_eval_runs`, temporally ordered `v_step3_paired_run_comparison`, and clause-scoped `v_step3_policy_nfr_gate` (`EVAL-P09` `NFR == 0.0%`). |
| [`sql/step3_daily_quota_guard.sql`](sql/step3_daily_quota_guard.sql) | **Step 3 Daily Quota Guard**: Per-app `eval_daily_quota_config`, atomic `eval_daily_quota_reservations`, non-deduped `v_step2_daily_quota_status` / `v_step2_app_daily_quota_status`, and executable procedure `agent_eval.run_daily_quota_guarded_judge(...)`. |
| [`sql/ddl.sql`](sql/ddl.sql) | Parameterized BigQuery DDL (`${CONNECTION_ID}`, `gs://${PAYLOAD_BUCKET}/${PREFIX}*.jsonl`, dual-schema `completions`): `39`-col Full Capture views, `app_registry` (`RL-1`–`RL-5`, `funded_gsus`, `storage_usd_per_outcome`), 8-SKU 1P/3P `model_pricing_catalog`, `inference_request_logs`, `bq_query_jobs`, `remote_trace_slices`, and remote judge models. |
| [`sql/views.sql`](sql/views.sql) | Enterprise BigQuery views: `v_bq_query_cost_attribution`, `v_trace_waterfall`, `v_trace_sql_evaluation`, `v_enterprise_cost_per_verified_outcome`, `v_counterparty_spend`, `v_agent_eval_and_finops_summary`, `v_capacity_hourly_burndown_export`, `v_inference_request_reconciliation`, `v_app_risk_governance`, `v_focus_three_pool_hourly_chargeback`, `v_interaction_session_lineage`, and `v_live_slo_and_429_breaches`. |
| [`sql/omni_spoke_views.sql`](sql/omni_spoke_views.sql) | **BigQuery Omni Multi-Cloud Spokes**: AWS S3 & Azure ADLS BigLake tables with pushed-down `FARM_FINGERPRINT`, zero raw prompt egress, `principal://`/`spiffe://` normalization, and `v_unified_multicloud_cpvo`. |
| [`terraform/`](terraform/main.tf) & [`Dockerfile`](Dockerfile) | Standalone (`create_* = true`) / co-located (`create_* = false`, `power_policy = hibernate`) single-project Terraform + credential-free `terraform/tests/*.tftest.hcl`, non-root `UID 10001` container (`HEALTHCHECK /healthz`), and Trivy `os,library` gate in [`cloudbuild.yaml`](cloudbuild.yaml). |
| [`scripts/bq_dry_run.py`](scripts/bq_dry_run.py) | `sqlglot`-tokenized BigQuery `bq query --dry_run` validator across `sql/*.sql` (including procedure blocks & Omni spokes) and Python-generated SQL builders (`73` checked / `17` skipped). |
| [`bq_eval/contract/`](bq_eval/contract/__init__.py) | Frozen, `allow_inf_nan=False` Pydantic v2 contracts across [`otel.py`](bq_eval/contract/otel.py) (`ServingTrafficType`, `'optimize'`, 39-col span), [`pricing.py`](bq_eval/contract/pricing.py) (8 1P/3P `DEFAULT_MODEL_SKUS`), [`rubrics.py`](bq_eval/contract/rubrics.py) (8 `CANONICAL_RUBRICS`), [`results.py`](bq_eval/contract/results.py), [`governance.py`](bq_eval/contract/governance.py), and [`optimizer.py`](bq_eval/contract/optimizer.py) (`ATIF-v1.8`). |
| [`bq_eval/engine/`](bq_eval/engine/__init__.py) | Pure-stdlib engine: [`bootstrap.py`](bq_eval/engine/bootstrap.py), [`calibration.py`](bq_eval/engine/calibration.py), [`finops.py`](bq_eval/engine/finops.py) (1P/3P cache-write + Priority/Flex PayGo), [`chargeback_3pool.py`](bq_eval/engine/chargeback_3pool.py) (3-Pool FOCUS & `capacity` `BQ-G1`), [`governance.py`](bq_eval/engine/governance.py), [`empirical_proof.py`](bq_eval/engine/empirical_proof.py), [`omni.py`](bq_eval/engine/omni.py), [`otel_export.py`](bq_eval/engine/otel_export.py) (`RingBufferSpanExporter`), [`optimizer.py`](bq_eval/engine/optimizer.py), [`bqaa.py`](bq_eval/engine/bqaa.py), [`quota.py`](bq_eval/engine/quota.py), [`ports.py`](bq_eval/engine/ports.py), [`sql_eval.py`](bq_eval/engine/sql_eval.py), [`sqlite_analytics.py`](bq_eval/engine/sqlite_analytics.py), and [`sqlite_views.py`](bq_eval/engine/sqlite_views.py). |
| [`bq_eval/showcase/`](bq_eval/showcase/__init__.py) | GM3 Light workbench and corpus replay across [`pipeline.py`](bq_eval/showcase/pipeline.py) (dynamic `--scenario-file` FinOps), [`golden_seeds.py`](bq_eval/showcase/golden_seeds.py), [`loadgen.py`](bq_eval/showcase/loadgen.py), and pure presentation subpackage [`render/`](bq_eval/showcase/render/__init__.py) ([`view.py`](bq_eval/showcase/render/view.py), [`renderer.py`](bq_eval/showcase/render/renderer.py) with single-pass escaping and strict CSP, 0 engine imports). |
| [`bq_eval/cli/`](bq_eval/cli/__init__.py) | Driving CLI ([`app.py`](bq_eval/cli/app.py), [`bootstrap_cmd.py`](bq_eval/cli/bootstrap_cmd.py): `--version`, `demo`, `serve`, `loadgen`, `judge-sql`, `bqaa`, `bootstrap --mode light\|full\|hybrid`) and loopback HTTP server ([`server.py`](bq_eval/cli/server.py)). |
| [`tests/`](tests/test_sql_contracts.py) | `146` tests across [`conftest.py`](tests/conftest.py), SQL AST/SQLite parity (`test_sql_contracts.py`), critical paths, end-to-end pins & `F7`, `M7` workbench/wire contracts, skill & dry-run contract, `M9` suites (`test_m9_*.py`), `M10` bootstrap/Terraform/query-cost (`test_m10_bootstrap_and_infra.py`), `M11` 3P/3-pool/Omni/ATIF (`test_m11_enterprise_north_star.py`), doc contract, folder AST isolation (`test_isolation_guard.py`), and hygiene (`standards_debt.json = 0`). |

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
