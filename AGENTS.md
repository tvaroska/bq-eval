# Agent Instructions (`bq-eval`)

## 1. Purpose & Scope
`bq-eval` (**BQ Eval**) is the standalone **BigQuery-Native Enterprise Agent Evaluation, Observability, and Cost Management Showcase**. It positions **BigQuery as a pure execution engine** over standard OpenTelemetry `gen_ai.*` traces, using a progressive **`Step 1 -> Step 2 -> Step 2.5 (Agentic Skill) -> Step 3 -> Open Extensions`** onboarding ladder centered on **USD Cost per Verified Outcome**.

For regulated banking Model Risk Management (`SR 11-7`, `SR 26-2 / OCC 2026-13`) playbooks, academic literature compendiums, and cross-workspace compliance auditing, see sibling repository `agent-evals`.

Before editing this repository, read:
1. [`CRITICAL.md`](CRITICAL.md) — Non-negotiable engineering, calibration, FinOps, and hygiene invariants.
2. [`DECISIONS.md`](DECISIONS.md) — Architectural Decision Records (`ADR-001` through `ADR-007`).
3. [`docs/roadmap.md`](docs/roadmap.md) — Showcase milestones and roadmap.

---

## 2. Architecture & Documentation Map

| File / Module | Responsibility |
| :--- | :--- |
| [`README.md`](README.md) | 30-second pitch, progressive `Step 1 -> Step 2 -> Step 2.5 -> Step 3` onboarding guide, 3 personas, and runnable `bq-eval demo` quickstart. |
| [`docs/architecture-and-sql.md`](docs/architecture-and-sql.md) | Step 1–3 SQL walkthrough, portable App ID resolution (`EVAL-P07`), XML injection defense (`EVAL-P08`), conditional Rogan-Gladen SQL (`EVAL-P05`), and open 7-table enterprise DDL (`sql/ddl.sql`). |
| [`docs/finops-and-cost-management.md`](docs/finops-and-cost-management.md) | **USD Cost per Verified Outcome (`CPVO`)**, 4-bucket `FinOpsTrafficType` attribution, Eval FinOps ROI (`90–95%` savings), `>200K` burndown cliffs, `NFR = 0.0%` Pareto right-sizing, and `FOCUS 1.2` Two-Pool Chargeback + Off-Peak PT Batch-Eval Water-Filling. |
| [`docs/roadmap.md`](docs/roadmap.md) | Phased showcase delivery roadmap (`M1`–`M5`). |
| [`sql/demo_time_shift_loadgen.sql`](sql/demo_time_shift_loadgen.sql) | **Demo Time-Shift Load Generator (`agent_eval_seed`)**: Seeds 16 synthetic Golden Trace Templates into isolated dataset `agent_eval_seed` (`seed_otel_spans`, `seed_trace_payloads`, `otel_spans`, `trace_payloads`) so `agent_eval` holds exclusively real `_AllSpans` traces. |
| [`sql/step1_trace_views.sql`](sql/step1_trace_views.sql) | **Step 1 (`0` New Tables, No Skill)**: Static SQL views & queries over native `_AllSpans` traces (`v_step1_trace_waterfall`, `v_step1_trace_eval`, `v_outcomes`, `v_infra_cost_estimate`, `v_cost_per_verified_outcome`, `v_attribution_coverage`, `v_skill_lineage_roi`, `v_otel_spans_coverage`). |
| [`sql/step2_rubrics.csv`](sql/step2_rubrics.csv) | **Step 2 (`1` Rubric Table/CSV)**: 4 example 4-point ordinal rubrics (`groundedness_4pt_v1`, `tool_trajectory_4pt_v1`, `completeness_4pt_v1`, `escalation_policy_4pt_v1`). |
| [`sql/step2_stateless_eval.sql`](sql/step2_stateless_eval.sql) | **Step 2 (Stateless Eval, No Skill)**: `agent_eval.rubrics` setup (GCS CSV or native table) + static `AI.GENERATE_TABLE` queries on 100% of traces and a 10% sample without saving results. |
| [`skills/bq-eval/SKILL.md`](skills/bq-eval/SKILL.md) | **Step 2.5 (Agentic Skill)**: Defines new 4-point rubrics from plain English and generates custom slice queries (`5%`, `last 1 hour of traces`, app/error filters) via [`skills/bq-eval/references/sql_recipes.md`](skills/bq-eval/references/sql_recipes.md). |
| [`sql/step3_saved_results.sql`](sql/step3_saved_results.sql) | **Step 3 (Saved Eval Results)**: Persists verdicts into `agent_eval.eval_results` (`eval_run_id`) with `v_step3_eval_runs` and `v_step3_paired_run_comparison` (`0%` negative-flip gates). |
| [`sql/ddl.sql`](sql/ddl.sql) | BigQuery DDL: M1 compatibility views `agent_eval.otel_spans` (over `obs_traces._AllSpans`) and `agent_eval.trace_payloads` (over `obs_logs._AllLogs` `LEFT JOIN agent_eval.completions`), plus physical tables (`app_registry`, `golden_cases`, `eval_results`, `sme_labels`, `model_pricing_catalog`) and remote judge models. |
| [`sql/views.sql`](sql/views.sql) | BigQuery views: `v_trace_waterfall`, `v_trace_sql_evaluation`, `v_step1_cost_per_outcome`, `v_outcomes`, `v_infra_cost_estimate`, `v_cost_per_verified_outcome`, `v_attribution_coverage`, `v_skill_lineage_roi`, `v_otel_spans_coverage`, `v_agent_eval_and_finops_summary`, and `v_capacity_hourly_burndown_export`. |
| [`bq_eval/otel.py`](bq_eval/otel.py) | `OTelGenAISpanRecord`, `GenAISpanKind`, `FinOpsTrafficType`, `<=200K` and `>200K` `compute_weighted_burndown_tokens()`, and `resolve_application_id()`. |
| [`bq_eval/rubrics.py`](bq_eval/rubrics.py) | 4-point `RubricSpec`, `OrdinalScoreOutput` (`reasoning_trace` first), `wrap_untrusted_text()`, `build_autorater_system_prompt()`, and `CANONICAL_RUBRICS`. |
| [`bq_eval/calibration.py`](bq_eval/calibration.py) | Pure-Python `ConfusionMatrix`, `compute_confusion_matrix()`, `rogan_gladen_calibrate()` (95% CIs + fail-closed `None` guard), and `compute_gwet_ac2()`. |
| [`bq_eval/sql_eval.py`](bq_eval/sql_eval.py) | Fail-closed SQLite twin `evaluate_traces_in_sqlite()`, `escape_sql_xml_expr()`, and XML-escaped `build_ai_generate_table_judge_sql()`. |
| [`bq_eval/finops.py`](bq_eval/finops.py) | `compute_eval_finops_roi()`, `diagnose_prefix_cache_dilution()`, `compute_pareto_frontier()` (`NFR = 0.0%`), `allocate_focus_two_pool_chargeback()`, and `plan_off_peak_eval_schedule()`. |
| [`bq_eval/loadgen.py`](bq_eval/loadgen.py) | `build_golden_seed_templates()`, `generate_time_shifted_corpus()`, `evaluate_time_shifted_corpus()`, and `export_time_shifted_jsonl()`. |
| [`bq_eval/showcase.py`](bq_eval/showcase.py) | `run_showcase_pipeline()`, `render_markdown_report()`, and `write_html_scorecard()` (with embedded SVG favicon). |
| [`bq_eval/cli.py`](bq_eval/cli.py) | `bq-eval` CLI (`demo [--html-out PATH]`, `loadgen [--replicas ... --last-hour-replicas ...]`, and `judge-sql`). |

---

## 3. Canonical Verification Commands

Run linting, unit tests, and the runnable showcase demo before committing:
```bash
ruff check --no-cache bq_eval tests examples
pytest -p no:cacheprovider -v
python3 -m bq_eval.cli demo
```
