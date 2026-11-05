# Roadmap (`bq-eval`)

## Delivered Milestones (`v0.1.0`)

- [x] **M1 — Complete BigQuery Single-Store DDL & Fail-Closed SQL/Python Parity (`ARCH-S1` & `ARCH-S2`)**:
  - Added [`sql/ddl.sql`](../sql/ddl.sql) defining all 7 physical BigQuery tables (`app_registry`, `otel_spans`, `trace_payloads`, `golden_cases`, `eval_results`, `sme_labels`, `model_pricing_catalog`) plus remote judge model `gemini_4_argon_judge`.
  - Hardened [`bq_eval/sql_eval.py`](../bq_eval/sql_eval.py) and [`sql/views.sql`](../sql/views.sql) with XML prompt-injection escaping (`REPLACE()` on `&`, `<`, `>`), off-GCP `COALESCE` application ID resolution, fail-closed `None`/`NULL` pass rates when `judged_samples == 0`, conditional `rule0_pass = TRUE` Rogan-Gladen estimation with delta-method 95% confidence intervals, and SQL Gwet's `AC1`/`AC2`.
- [x] **M2 — Outcome-Adjusted AI FinOps Engine & Capacity Actuation Bridge (`ARCH-S3` & `ARCH-S4`)**:
  - Implemented [`bq_eval/finops.py`](../bq_eval/finops.py) with multi-cloud `DEFAULT_MODEL_PRICING_CATALOG`, 4-bucket `FinOpsTrafficType` USD attribution (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`), **USD Cost per Verified Outcome**, and `compute_eval_roi()` (`90–95%` Eval FinOps savings).
  - Added span-level `<=200K` (`1.0x/0.1x/4.0x`) and `>200K` (`2.0x/0.2x/7.5x`) long-context burndown cliffs (`compute_weighted_burndown_tokens`), `diagnose_prefix_cache_health()` (`1/N` round-robin dilution), `compute_pareto_frontier()` (`NFR = 0.0%` model right-sizing + `APO` token compression), `plan_offpeak_eval_waterfill()`, and `compute_focus_two_pool_chargeback()` (`FOCUS 1.2` Active Consumed vs. Unallocated Idle).
- [x] **M3 — 60-Second Runnable Showcase CLI, 3-BU Seed Corpus & Executive Scorecard (`ARCH-S5` & `ARCH-S6`)**:
  - Shipped [`bq_eval/showcase.py`](../bq_eval/showcase.py) and [`bq_eval/cli.py`](../bq_eval/cli.py) (`bq-eval demo | evaluate | calibrate | finops | sql`) over a 3-Business-Unit enterprise dataset with `< 1e-15` SQLite vs. Python calibration parity and self-contained HTML + Markdown Executive Scorecards.

## Next Horizons (`v0.2.0`)

- [ ] **M4 — Live BigQuery Continuous Query & Looker Studio / Cloud Run Scorecard Viewer**:
  - Add optional Terraform module for one-command provisioning of the 7 BigQuery tables, Cloud Storage `trace_payloads` Object Table connection, and scheduled `AI.GENERATE_TABLE` off-peak water-filling query.
