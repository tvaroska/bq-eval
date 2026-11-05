# Agent Instructions (`bq-eval`)

## 1. Purpose & Scope
`bq-eval` (**BQ Eval**) is the standalone **BigQuery-Native Enterprise Agent Evaluation, Observability, and Cost Management Showcase**. It turns **Google Cloud App Hub + BigQuery** into a zero-extra-database control plane that links `100%` of standard OpenTelemetry `gen_ai.*` traces to Business Unit and risk-tier metadata to measure, govern, and optimize **USD Cost per Verified Outcome**.

For regulated banking Model Risk Management (`SR 11-7`, `SR 26-2 / OCC 2026-13`) playbooks, academic literature compendiums, and cross-workspace compliance auditing, see sibling repository `agent-evals`.

Before editing this repository, read:
1. [`CRITICAL.md`](CRITICAL.md) — Non-negotiable engineering, calibration, FinOps, and hygiene invariants.
2. [`DECISIONS.md`](DECISIONS.md) — Architectural Decision Records (`ADR-001` through `ADR-007`).
3. [`docs/roadmap.md`](docs/roadmap.md) — Showcase milestones and roadmap.

---

## 2. Architecture & Documentation Map

| File / Module | Responsibility |
| :--- | :--- |
| [`README.md`](README.md) | 30-second pitch, 3 personas, 6 quantified benefits (`B1`–`B6`), 4-stage adoption ramp, 3-layer App Hub + BigQuery architecture, and 60-second runnable `bq-eval demo`. |
| [`docs/architecture-and-sql.md`](docs/architecture-and-sql.md) | App Hub In-GCP vs. Off-GCP WIF linkage, 7-table BigQuery Single Store schema (`sql/ddl.sql`), 4 SQL views (`sql/views.sql`), and developer/platform SQL cookbook. |
| [`docs/finops-and-cost-management.md`](docs/finops-and-cost-management.md) | **USD Cost per Verified Outcome**, 4-bucket `FinOpsTrafficType` attribution, Eval FinOps ROI (`90–95%` savings), `>200K` burndown cliffs, `NFR = 0.0%` Pareto right-sizing, and `FOCUS 1.2` Two-Pool Chargeback + Off-Peak PT Batch-Eval Water-Filling. |
| [`docs/roadmap.md`](docs/roadmap.md) | Phased showcase delivery roadmap (`M1`–`M4`). |
| [`sql/ddl.sql`](sql/ddl.sql) | Complete BigQuery DDL for all 7 tables (`app_registry`, `otel_spans`, `trace_payloads`, `golden_cases`, `eval_results`, `sme_labels`, `model_pricing_catalog`) and remote judge model (`gemini_4_argon_judge`). |
| [`sql/views.sql`](sql/views.sql) | BigQuery views: `v_trace_waterfall`, `v_trace_sql_evaluation`, `v_agent_eval_and_finops_summary`, and `v_capacity_hourly_burndown_export`. |
| [`bq_eval/otel.py`](bq_eval/otel.py) | OpenTelemetry `gen_ai.*` attributes, `<=200K` and `>200K` weighted burndown math, W3C/XCTC headers, and in-memory `RingBufferSpanExporter`. |
| [`bq_eval/rubrics.py`](bq_eval/rubrics.py) | 7-rule `AtomicRubric` (`{2, 3, 4}` scales), `PointwiseVerdict`, XML sanitization, and pre-built rubrics. |
| [`bq_eval/calibration.py`](bq_eval/calibration.py) | Pure-Python `krippendorff_alpha`, `gwets_ac2`, `rogan_gladen_debiased_pass_rate` (95% CIs), `human_agent_swap`, `negative_flip_rate`, `compute_rair_rsr`, `debias_system1_logprobs`, and `evaluate_release_gate`. |
| [`bq_eval/sql_eval.py`](bq_eval/sql_eval.py) | Fail-closed in-memory & SQL evaluators (`evaluate_traces_rule0_in_memory`, `summarize_eval_and_finops_in_memory`, XML-escaped `build_ai_generate_table_judge_sql`, golden promotion, and Rogan-Gladen SQL). |
| [`bq_eval/finops.py`](bq_eval/finops.py) | Multi-cloud `DEFAULT_MODEL_PRICING_CATALOG`, 4-bucket USD attribution, `compute_eval_roi`, `diagnose_prefix_cache_health`, `compute_pareto_frontier`, `plan_offpeak_eval_waterfill`, and `compute_focus_two_pool_chargeback`. |
| [`bq_eval/showcase.py`](bq_eval/showcase.py) | 3-BU synthetic enterprise dataset + `<2s` local SQLite/Python verification + HTML & Markdown Executive Scorecard generator. |
| [`bq_eval/cli.py`](bq_eval/cli.py) | `bq-eval` CLI (`demo`, `evaluate`, `calibrate`, `finops`, `sql`). |

---

## 3. Canonical Verification Commands

Run linting, unit tests, and the runnable showcase demo before committing:
```bash
ruff check --no-cache bq_eval tests examples
pytest -p no:cacheprovider -v
python3 -m bq_eval.cli demo --format markdown
```
