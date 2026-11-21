# BQ Eval & FinOps

Token dashboards show what an agent spent, not whether it finished the job. Judging every trace with an LLM rivals production spend, uncalibrated judges skew pass rates, and prompts past 200K tokens hit a 2x context cliff. I built `bq-eval` to filter 100% of lakehouse traces in `$0` SQL first, judge a 5% to 10% slice, and report calibrated Cost per Verified Outcome.

![BQ Eval SQL-First Agent Evaluation and Cost per Verified Outcome](docs/assets/architecture_card.svg)

## Verified Receipts

Local math passes `67/67` tests (`0.0` mock ratio, `< 1e-12` SQLite-vs-Python parity). Live receipts come from a generated-document Google Cloud fleet (`343` labeled cases, `443` closed outcomes, `28,752` spans, `_AllSpans` lag `p50 = 87 ms`, `p95 = 11.4 s`).

| Scope | Measured Result | Proof |
| :--- | :--- | :--- |
| **Offline twin (`50` traces)** | `order-support-agent`: `38/40` (`95%`) SQL pass, `18/20` (`90%`) raw judge, `87.4%` calibrated `[64.7%, 100%]`, `$0.0232`/outcome; unjudged app (`10/10`) fails closed (`NULL`); `5%` canary + error tail cuts `100k`-trace judge spend `92.2%` (`$250` to `$19.62`) | [SQL tests](tests/test_sql_contracts.py) |
| **Live KYC (`280` cases)** | `28/28` structural + `8/8` PO Box traps caught in `$0` SQL; Flash caught `20/20` semantic traps (`100%` recall, Wilson `[83.9%, 100%]`, `0/216` FP); `30/30` binary pass match vs. Argon (4-point ordinal `AC2 = 0.8605`, calibrated `86.84%` `[82.8%, 90.9%]`) | [Step-2 SQL](sql/step2_stateless_eval.sql) |
| **Live RFP cliff (`35` cases)** | A2A `$0.0000` AI vs. Flash PDF `$0.00024` AI vs. misconfigured `>200K` Argon PDF `$0.7225` AI for the same award, surfaced by `$0` SQL (combines model tier and `>200K` cliff) | [Step-1 SQL](sql/step1_trace_views.sql) |
| **Live skill (`18` cases; `10` scored) & shadow (`10`)** | Skill `1.0.0` to `1.1.0`: `$0.02427` to `$0.000915`/outcome (`96.2%` drop, excl. reviewer wages); reviews `1.00` to `0.20`/outcome (`9/9` in-policy auto, `1/1` escalated). Unregistered service isolated to `unregistered_app` (`0%` coverage) | [Views](sql/views.sql), [BQAA tests](tests/test_m5_bqaa_dual_path.py) |

## Why Not Langfuse, LangSmith, Datadog, or Raw BQAA?

If you want prompt-iteration UIs, annotation queues, or single-team tracing outside BigQuery, start with Langfuse or LangSmith. Use `bq-eval` when traces already land in BigQuery and platform or finance teams need SQL-native evaluation, calibration, and chargeback without copying telemetry.

| Incumbent | What It Does Well | Gap `bq-eval` Fills |
| :--- | :--- | :--- |
| **Langfuse / LangSmith** | Prompt workflows, annotation queues, dev evals (Langfuse is MIT self-hostable on ClickHouse; LangSmith fits LangGraph) | Copies traces out of BigQuery and meters per unit/seat; no SQL Rogan-Gladen debiasing, `>200K` cliffs, or `FOCUS 1.2` chargeback |
| **Datadog LLM / Billing exports** | Unified APM alerts, latency, and raw token spend on existing infra | Bills per LLM span and stops at cost per trace; misses verified outcomes and fail-closed unjudged checks |
| **Raw `BQAA` + `AI.EVALUATE_AGENT`** | 3-line Python capture (ADK/LangGraph) and zero-setup managed turn scoring | Skips App Hub stamping, reports uncalibrated scores, and omits `$0` SQL pre-filtering and outcome cost; `bq-eval` layers directly on top with zero ETL |

## Where This Breaks

1. **Model-vs-model calibration today, not blind human SMEs:** Live `AC2 = 0.8605` compares `gemini-3.8-flash` to `gemini-4-argon` (`30/30` cases). Regulated calibration still requires `~100` blind human labels in `sme_labels`.
2. **Small live slices and reviewer wages:** `StratPPI` population stratum weighting debiases `ERROR_OR_SLA_TAIL` vs. canary sampling in SQL and SQLite, but the live skill comparison (`N = 10`) remains small and excludes human reviewer wages.
3. **BigQuery-only SQL and regional limits:** Core queries use `AI.GENERATE_TABLE` and `FARM_FINGERPRINT`. `AI.EVALUATE_AGENT` requires single regions (and its Step-3 token accounting uses `CHAR_LENGTH / 4` estimates).
4. **No one-click IaC deploy yet:** Offline mode uses a SQLite twin; live BigQuery views assume existing `agent_events` or `_AllSpans` datasets and manual `bq query` runs.

## Quick Start

Run the zero-credential SQLite twin and full test suite locally:

```bash
uv sync --extra dev
uv run python -m bq_eval.cli demo
uv run pytest -p no:cacheprovider -v
```

### Deep Dives

- [BigQuery Step 1-3 SQL setup & calibration math](docs/architecture-and-sql.md)
- [Light Capture (`BQAA`) vs. Full Capture (`OTel`) & App Hub](docs/bqaa-and-dual-path-architecture.md)
- [Serving physics, `>200K` cliffs, Pareto & `FOCUS 1.2`](docs/finops-and-cost-management.md)
- [Plain-English rubric & slice skill](skills/bq-eval/SKILL.md) · [Invariants](CRITICAL.md) · [ADRs](DECISIONS.md) · [Roadmap](docs/roadmap.md)
