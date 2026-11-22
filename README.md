# BQ Eval & FinOps: Stop Mistaking Token Spend for Working Agents

Agents don't fail loudly on monitoring dashboards: a looping tool or 200K-token PDF burns budget without finishing the job. Judging every trace with LLMs rivals production spend, and oversampling failing tails skews raw pass rates. I built BQ Eval & FinOps so Day-1 BigQuery views need zero new tables, mirror in SQLite, and return NULL when unjudged.

![BQ Eval and FinOps In-Warehouse Views](docs/assets/architecture_card.svg)

## Verified Receipts

Static [rule views](sql/step1_trace_views.sql) check all traces at **`$0` LLM judge cost** (BigQuery scan bytes still apply) over `agent_events` or `_AllSpans` (`32`-col [adapter parity](tests/test_m5_bqaa_dual_path.py)). Scheduled queries persist sampled scores and labeled cases under a `2M`-token/day [quota guard](sql/step3_daily_quota_guard.sql). [Dry-run validation](benchmarks/results/bq_dry_run_receipt.json) parses **`56`** SQL and judge statements via `sqlglot` (`12` DDL skipped; opt-in GCP `--dry_run` via [`bq_dry_run.py`](scripts/bq_dry_run.py)). Math matches BigQuery SQL in SQLite (`< 1e-12` parity, **`115/115` tests**). The [fleet receipt](benchmarks/results/m2_live_receipt.json) (`343` reconstructed cases, `676` spans incl. `333` `outcome.closed`, `0` human SME labels; `gemini-4-argon` reference judge, `AC2` = Quadratic Gwet's agreement) replays below:

| Scope | Measured Result | Proof |
| :--- | :--- | :--- |
| **Twin (`50`)** | `order-support-agent` (`40`, `catalog-enrichment-agent` `10/10` unjudged `NULL`): `38/40` (`95%`) rules, `18/20` canary + `0/2` tail (`81.8%` pooled vs. `87.4%` calibrated `[64.7%, 100%]`), `$0.0232`/outcome (`$0.7160` agent + `$0.0550` judge) | [SQL tests](tests/test_sql_contracts.py) |
| **KYC (`280`)** | `28/28` structural + `8/8` PO Box + `8` OCR (`44` `$0`-LLM rejects); `20/20` traps (`100%` `[83.9%, 100%]`, `0/216` FP); across `236` Rule-0 passing judged rows, `30/30` Flash/Argon match (`AC2 = 0.8605`, calibrated `91.53%` `[87.97%, 95.08%]`, `$0.0054`/outcome) | [Fleet receipt](benchmarks/results/m2_live_receipt.json) |
| **RFP (`35`)** | `100%` pass (`35/35`): `A2A_PATH_A` (`8`) `$0.0000` AI vs. Flash (`17`) `$0.00035` AI vs. `>200K` Argon (`10`) `$0.7226` AI (mixes tier & cliff) | [E2E tests](tests/test_bq_eval_end_to_end.py) |
| **Skill (`18`) & shadow (`10`)** | `100%` pass (`18/18`): `1.0.0` (`8`) `$0.08350` AI/outcome to `1.1.0` (`10`) `$0.000588` AI/outcome (`99.3%` drop, excl. wages; `9/9` auto, `1/1` escalated); `10` shadow hit `unregistered_app` (`0%`, `NULL`) | [Contract tests](tests/test_m7_workbench_and_contracts.py) |

[Cost model](bq_eval/engine/finops.py): on `100k` traces (`3%` tail + `5%` canary), rule screening cuts judge spend `92.2%` (`$250` to `$19.62`) while catching `100%` of rule/telemetry-detectable tail failures missed by uniform `5%` sampling.

## Why Not Langfuse, LangSmith, Datadog, or Managed BigQuery Eval?

For prompt UIs, annotation queues, or `< 50`-trace slices where `$0` LLM rules plus manual review beat wide intervals, use Langfuse, LangSmith, or plain SQL/`dbt`.

| Incumbent | What It Does Well | Gap `bq-eval` Fills |
| :--- | :--- | :--- |
| **Langfuse / LangSmith** | Prompt iteration, annotation UIs, dev tracing | Copies traces out of BigQuery; omits warehouse rules, debiased samples, and `>200K` cliffs |
| **Datadog LLM / Billing** | APM alerts, latency, raw token spend | Stops at cost/trace; misses verified outcomes, judge spend, and `NULL` on unjudged apps |
| **BQAA + `AI.EVALUATE_AGENT`** | 3-line capture, zero-setup turn scoring | Omits App Hub ownership, calibration, `$0` LLM rule filtering, and outcome cost |

## Where This Breaks

1. **Reconstructed synthetic docs & model labels:** `m2_live_receipt.json` has `343` reconstructed cases (`676` spans, `0` human SME labels); `AC2 = 0.8605` compares `gemini-3.8-flash` to `gemini-4-argon` (`[87.97%, 95.08%]` uses point judge rates). Regulated use needs `~100` blind human labels.
2. **Small slices & mixed cliffs:** Skill (`n=8` vs. `n=10`) excludes engineering/SME wages; RFP (`35`) mixes model tier with the `>200K` cliff.
3. **Scheduled writes, caps & SQLite dialects:** Sampled `AI.GENERATE_TABLE` verdicts persist via scheduled queries under a `2M`-token daily cap. `AI.EVALUATE_AGENT` supports `14` single regions only (`us.vertex_ai_connection` targets `US` multi-region) and estimates tokens via `CHAR_LENGTH / 4`. [`sqlite_views.py`](bq_eval/engine/sqlite_views.py) mirrors `APPROX_QUANTILES(...)[OFFSET(95)]` and `ARRAY_AGG(...)[SAFE_OFFSET(0)]` natively (`< 1e-12` parity).
4. **Manual cloud setup:** Local runs use SQLite; [`bq_dry_run.py`](scripts/bq_dry_run.py) needs `--project-id`, `bq` CLI, `gcloud auth`, and replaced `${CONNECTION_ID}` / `${PAYLOAD_BUCKET}` placeholders for live GCP `--dry_run`. Single-command bootstrap is on the [roadmap](docs/roadmap.md).

## Quick Start

Run the CLI, 4-tab GM3 workbench, load generator, judge SQL generator, or offline dry-run check:

```bash
uv sync --extra dev
uv run python -m bq_eval.cli demo --html-out /tmp/bq_eval_scorecard.html
uv run python -m bq_eval.cli serve --port 8080
uv run python -m bq_eval.cli loadgen --replicas 50 --last-hour-replicas 15
uv run python -m bq_eval.cli judge-sql --engine generate-table --project-id my-gcp-project --canary-pct 5
uv run python scripts/bq_dry_run.py --offline-manifest /tmp/bq_dry_run_manifest.json
uv run python scripts/bq_dry_run.py --project-id YOUR_GCP_PROJECT  # requires authenticated `bq` CLI
```

For 3-line Light Capture from Python ADK (`agent_events` via Storage Write API):

```python
bqaa_cfg = BigQueryLoggerConfig(custom_metadata={"apphub.application.id": "my-app"})
bqaa_plugin = BigQueryAgentAnalyticsPlugin(project_id="my-gcp-project", dataset_id="agent_eval", config=bqaa_cfg)
runner = Runner(agent=root_agent, app_name="my-app", session_service=session_svc, plugins=[bqaa_plugin])
```

Also try `--corpus retail-2app`, `--format json`, or `uv run pytest -p no:cacheprovider -v` (`115/115` tests).

### Deep Dives

- [SQL & calibration](docs/architecture-and-sql.md) · [`BQAA` & `_AllSpans`](docs/bqaa-and-dual-path-architecture.md) · [Cliffs & chargeback](docs/finops-and-cost-management.md) · [Rubric skill](skills/bq-eval/SKILL.md) · [Invariants](CRITICAL.md) · [ADRs](DECISIONS.md) · [Roadmap](docs/roadmap.md)
