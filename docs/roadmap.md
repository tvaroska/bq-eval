# Roadmap (`bq-eval`)

## Delivered Milestones (`v0.1.0`)

- [x] **M1 — Complete BigQuery Single-Store DDL & Fail-Closed SQL/Python Parity (`ARCH-S1` & `ARCH-S2`)**:
  - Added [`sql/ddl.sql`](../sql/ddl.sql) defining all 7 physical BigQuery tables (`app_registry`, `otel_spans`, `trace_payloads`, `golden_cases`, `eval_results`, `sme_labels`, `model_pricing_catalog`) plus remote judge model `gemini_4_argon_judge`.
  - Hardened [`bq_eval/sql_eval.py`](../bq_eval/sql_eval.py) and [`sql/views.sql`](../sql/views.sql) with XML prompt-injection escaping (`escape_sql_xml_expr()` / `REPLACE()` on `&`, `<`, `>`), off-GCP `COALESCE` application ID resolution (`resolve_application_id()`), fail-closed `None`/`NULL` pass rates when `judged_samples == 0`, conditional `rule0_pass = TRUE` Rogan-Gladen estimation (`rogan_gladen_calibrate()`) with delta-method 95% confidence intervals, and prevalence-robust Gwet's `AC2` (`compute_gwet_ac2()`).
- [x] **M2 — Outcome-Adjusted AI FinOps Engine & Capacity Actuation Bridge (`ARCH-S3` & `ARCH-S4`)**:
  - Implemented [`bq_eval/finops.py`](../bq_eval/finops.py) with 4-bucket `FinOpsTrafficType` USD attribution (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`), **USD Cost per Verified Outcome (`CPVO`)**, and `compute_eval_finops_roi()` (`90–95%` Eval FinOps savings).
  - Added span-level `<=200K` (`1.0x/0.1x/4.0x`) and `>200K` (`2.0x/0.2x/7.5x`) long-context burndown cliffs (`compute_weighted_burndown_tokens()`), `diagnose_prefix_cache_dilution()` (`1/N` round-robin dilution), `compute_pareto_frontier()` (`NFR = 0.0%` model right-sizing), `plan_off_peak_eval_schedule()`, and `allocate_focus_two_pool_chargeback()` (`FOCUS 1.2` Utilized PT vs. Unutilized Idle).
- [x] **M3 — 60-Second Runnable Showcase CLI, Offline SQLite Twin & Executive Scorecard (`ARCH-S5` & `ARCH-S6`)**:
  - Shipped [`bq_eval/showcase.py`](../bq_eval/showcase.py) and [`bq_eval/cli.py`](../bq_eval/cli.py) (`bq-eval demo [--html-out PATH]` and `bq-eval judge-sql`) with `< 1e-15` SQLite vs. Python calibration parity (`evaluate_traces_in_sqlite()`) and self-contained HTML + Markdown Executive Scorecards.
- [x] **M4 — Progressive Step 1 -> Step 2 -> Step 2.5 (Agentic Skill) -> Step 3 SQL Pack & Time-Shift Demo Load Generator**:
  - Added [`sql/step1_trace_views.sql`](../sql/step1_trace_views.sql) (`0` extra tables, static SQL views & Day-1 Cost per Application, Business Unit & Outcome), [`sql/step2_rubrics.csv`](../sql/step2_rubrics.csv) + [`sql/step2_stateless_eval.sql`](../sql/step2_stateless_eval.sql) (`1` rubric table/CSV + static stateless `100%` and `10%` `AI.GENERATE_TABLE` queries), [`skills/bq-eval/SKILL.md`](../skills/bq-eval/SKILL.md) + [`skills/bq-eval/references/sql_recipes.md`](../skills/bq-eval/references/sql_recipes.md) (Step 2.5 Agentic Skill for plain-English rubric creation and `5%` / `last 1 hour` slice queries), and [`sql/step3_saved_results.sql`](../sql/step3_saved_results.sql) (`eval_results`, `v_step3_eval_runs`, `v_step3_paired_run_comparison`).
  - Added [`sql/demo_time_shift_loadgen.sql`](../sql/demo_time_shift_loadgen.sql) and [`bq_eval/loadgen.py`](../bq_eval/loadgen.py) (`python3 -m bq_eval.cli loadgen`) to capture once and time-shift/multiply a 4-application Golden Trace Corpus (`docubridge-underwriting`, `quoting-concierge-adk`, `claims-triage-langgraph`, and unregistered `renewal-auditor-svc`) into today and the last 1 hour in `<2 seconds`.

## Next Horizons (`v0.2.0`)

- [ ] **M5 — Live BigQuery Continuous Query & Looker Studio / Cloud Run Scorecard Viewer**:
  - Add optional Terraform module for one-command provisioning of Step 1–3 views/tables, Cloud Storage `trace_payloads` Object Table connection, and scheduled `AI.GENERATE_TABLE` off-peak water-filling query.
