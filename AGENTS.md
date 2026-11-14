# Agent Instructions (`bq-eval`)

## 1. Purpose & Scope
`bq-eval` (**BQ Eval & FinOps**) is the standalone **BigQuery-Native Enterprise Agent Evaluation, Observability, and Cost Management Showcase (`v0.2.2`)**. It positions **BigQuery as a pure execution engine** over **Dual-Path Agent Telemetry** — **Light Capture (`BQAA` `agent_events`)** for 3-line ADK/LangGraph developer onboarding and **Full Capture (OpenTelemetry `_AllSpans`)** for multi-service/multi-cloud enterprise fleets — using a progressive **`Step 1 -> Step 2 -> Step 2.5 (Agentic Skill) -> Step 3 -> Open Extensions`** ladder centered on **USD Cost per Verified Outcome**.

For regulated banking Model Risk Management (`SR 11-7`, `SR 26-2 / OCC 2026-13`) playbooks, academic literature compendiums, and cross-workspace compliance auditing, see sibling repository `agent-evals`.

Before editing this repository, read:
1. [`CRITICAL.md`](CRITICAL.md) — Non-negotiable engineering, calibration, FinOps, and hygiene invariants.
2. [`DECISIONS.md`](DECISIONS.md) — Architectural Decision Records (`ADR-001` through `ADR-008`).
3. [`docs/roadmap.md`](docs/roadmap.md) — Showcase milestones and roadmap.

---

## 2. Architecture & Documentation Map

| File / Module | Responsibility |
| :--- | :--- |
| [`README.md`](README.md) | Dual-path pitch, progressive `Step 1 -> Step 2 -> Step 2.5 -> Step 3` onboarding guide, 3 personas, measured live receipts, and runnable `bq-eval demo` quickstart. |
| [`docs/bqaa-and-dual-path-architecture.md`](docs/bqaa-and-dual-path-architecture.md) | **Dual-Path Capture -> Single Engine Reference**: **Light Capture (`BQAA` `agent_events`)** vs. **Full Capture (`OTel` `_AllSpans`)**, App Hub linkage, `Agent Runtime` / `Cloud Run` / `Non-GCP` matrix, and `AI.EVALUATE_AGENT` integration. |
| [`docs/architecture-and-sql.md`](docs/architecture-and-sql.md) | Dual-path ingestion modes, Step 1–3 SQL walkthrough, portable App ID resolution (`EVAL-P08`), XML injection defense (`EVAL-P02`), Quadratic Gwet's `AC2` (`EVAL-P03`), conditional Rogan-Gladen SQL (`EVAL-P04`), and `outcome.id` attribution (`EVAL-P11`). |
| [`docs/finops-and-cost-management.md`](docs/finops-and-cost-management.md) | **USD Cost per Verified Outcome (`CPVO`)**, 4-bucket `FinOpsTrafficType` attribution, Eval FinOps ROI (`90–95%` savings), `>200K` burndown cliffs, `NFR = 0.0%` Pareto right-sizing, and `FOCUS 1.2` Two-Pool Chargeback + Off-Peak PT Batch-Eval Water-Filling. |
| [`docs/roadmap.md`](docs/roadmap.md) | Phased showcase delivery roadmap (`M1`–`M9`, `v0.1.0` through `v0.3.0`). |
| [`sql/bqaa_adapter_views.sql`](sql/bqaa_adapter_views.sql) | **Light Capture (`BQAA`) & Hybrid Adapter Views**: Zero-copy `v_bqaa_otel_spans`, `v_bqaa_trace_payloads`, `v_bqaa_eval_turn_filtered` (`event_id` dedup + strict `ADR-003` `'unregistered_app'` fallback), and Light / Full / Hybrid switchboard. |
| [`sql/demo_time_shift_loadgen.sql`](sql/demo_time_shift_loadgen.sql) | **Demo Time-Shift Load Generator (`agent_eval_seed`)**: Seeds 16 synthetic Golden Trace Templates (`33` spans/replica = `1,650` spans at 50 replicas) into isolated dataset `agent_eval_seed` so `agent_eval` holds exclusively real traces. |
| [`sql/step1_trace_views.sql`](sql/step1_trace_views.sql) | **Step 1 (`0` New Tables, No Skill)**: Domain-agnostic static SQL views (`v_step1_trace_waterfall`, `v_step1_trace_eval`, `v_step1_cost_per_outcome`, `v_outcomes`, `v_infra_cost_estimate`, `v_cost_per_verified_outcome`, `v_attribution_coverage`, `v_skill_lineage_roi`, `v_otel_spans_coverage`) with proportional `1.0x` infra allocation. |
| [`sql/step2_rubrics.csv`](sql/step2_rubrics.csv) | **Step 2 (`1` Rubric Table/CSV)**: 6 canonical 4-point ordinal rubrics (`groundedness_4pt_v1`, `tool_trajectory_4pt_v1`, `completeness_4pt_v1`, `escalation_policy_4pt_v1`, `kyc_policy_compliance_4pt_v1`, `rfp_quote_accuracy_4pt_v1`). |
| [`sql/step2_stateless_eval.sql`](sql/step2_stateless_eval.sql) | **Step 2 (Stateless Dual-Engine Eval, No Skill)**: `agent_eval.rubrics` setup (`6` rubrics) + **Engine A (`AI.GENERATE_TABLE`)** and **Engine B (`AI.EVALUATE_AGENT`)** queries on 100% and 10% trace slices. |
| [`skills/bq-eval/SKILL.md`](skills/bq-eval/SKILL.md) | **Step 2.5 (Agentic Skill)**: Defines new 4-point rubrics from plain English and generates custom slice queries (`5%`, `last 1 hour`, Engine A/B) via [`skills/bq-eval/references/sql_recipes.md`](skills/bq-eval/references/sql_recipes.md). |
| [`sql/step3_saved_results.sql`](sql/step3_saved_results.sql) | **Step 3 (Saved Eval Results)**: Persists Engine A (`AI.GENERATE_TABLE`) and Engine B (`AI.EVALUATE_AGENT`) verdicts into `agent_eval.eval_results` (`eval_run_id`) with deduplicated `v_step3_eval_runs` and `v_step3_paired_run_comparison` (`0%` negative-flip gates). |
| [`sql/step3_daily_quota_guard.sql`](sql/step3_daily_quota_guard.sql) | **Step 3 Daily Quota Guard**: `eval_daily_quota_config`, `v_step2_daily_quota_status`, and the quota-guarded scheduled Step-2 judge query template. |
| [`sql/demo_docubridge_extensions.sql`](sql/demo_docubridge_extensions.sql) | **Optional Reference Demo Extensions**: DocuBridge-specific WORM storage proxies (`v_demo_infra_cost_estimate`, `v_demo_cost_per_verified_outcome`, `v_demo_skill_lineage_roi`) and KYC trap-recall view (`v_m2_kyc_trap_eval`), kept cleanly separate from core SQL. |
| [`sql/ddl.sql`](sql/ddl.sql) | BigQuery DDL: Full Capture compatibility views `agent_eval.otel_spans` (over `obs_traces._AllSpans`, `32` columns) and `agent_eval.trace_payloads` (over `obs_logs._AllLogs` `LEFT JOIN agent_eval.completions`), physical tables, and remote judge models. |
| [`sql/views.sql`](sql/views.sql) | Enterprise BigQuery views (`v_trace_waterfall`, `v_trace_sql_evaluation`, `v_enterprise_cost_per_verified_outcome`, `v_counterparty_spend`, `v_agent_eval_and_finops_summary` with SQL Quadratic Gwet's `AC2` & Rogan-Gladen 95% CIs, and `v_capacity_hourly_burndown_export`). |
| [`examples/otel-collector-wif.yaml`](examples/otel-collector-wif.yaml) | Keyless Workload Identity Federation (`sts.googleapis.com`) OpenTelemetry Collector config for AWS / Azure / on-prem workloads. |
| [`scripts/bq_dry_run.py`](scripts/bq_dry_run.py) | Live BigQuery `bq query --dry_run` validator across `sql/*.sql` and Python-generated SQL builders. |
| [`bq_eval/otel.py`](bq_eval/otel.py) | `OTelGenAISpanRecord`, `GenAISpanKind`, `FinOpsTrafficType`, `<=200K` and `>200K` `compute_weighted_burndown_tokens()`, `compute_span_cost_usd()`, and `resolve_application_id()`. |
| [`bq_eval/bqaa.py`](bq_eval/bqaa.py) | `BQAAAgentEvent`, `deduplicate_bqaa_events()`, `bqaa_events_to_otel_spans()`, and `bqaa_events_to_trace_payloads()` (Light Capture Python twin). |
| [`bq_eval/rubrics.py`](bq_eval/rubrics.py) | 4-point `RubricSpec`, `OrdinalScoreOutput` (`reasoning_trace` first), `wrap_untrusted_text()`, `build_autorater_system_prompt()`, and 6 `CANONICAL_RUBRICS`. |
| [`bq_eval/calibration.py`](bq_eval/calibration.py) | Pure-Python `ConfusionMatrix`, `compute_confusion_matrix()`, `rogan_gladen_calibrate()` (95% CIs + fail-closed `None` guard), `compute_gwet_ac2()`, and `build_calibration_result()`. |
| [`bq_eval/sql_eval.py`](bq_eval/sql_eval.py) | Pure-SQLite view twin `evaluate_traces_in_sqlite()` (Rogan-Gladen, 95% CI, Quadratic Gwet's `AC2`), `escape_sql_xml_expr()`, `build_ai_generate_table_judge_sql()`, and `build_ai_evaluate_agent_sql()`. |
| [`bq_eval/finops.py`](bq_eval/finops.py) | `compute_eval_finops_roi()`, cliff-aware `diagnose_prefix_cache_dilution()`, `compute_pareto_frontier()` (`NFR = 0.0%`), `allocate_focus_two_pool_chargeback()` (`reserved_pt_fixed_cost_usd`), and `plan_off_peak_eval_schedule()`. |
| [`bq_eval/loadgen.py`](bq_eval/loadgen.py) | `generate_time_shifted_corpus()`, `evaluate_time_shifted_corpus()`, and `export_time_shifted_jsonl()` backed by [`bq_eval/loadgen_seeds.py`](bq_eval/loadgen_seeds.py). |
| [`bq_eval/showcase.py`](bq_eval/showcase.py) | `run_showcase_pipeline()`, `render_markdown_report()`, and `write_html_scorecard()` (with `FOCUS 1.2` table and embedded SVG favicon). |
| [`bq_eval/cli.py`](bq_eval/cli.py) | `bq-eval` CLI (`demo [--html-out PATH]`, `loadgen`, and `judge-sql [--engine generate-table|evaluate-agent]`). |
| [`tests/`](tests/test_sql_contracts.py) | `54` tests across SQL AST/SQLite execution contracts (`test_sql_contracts.py`), critical path edge cases (`test_critical_paths.py`), end-to-end parity, M1 `_AllSpans`, M5 `BQAA`, isolation, and hygiene (`standards_debt.json = 0`). |

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
