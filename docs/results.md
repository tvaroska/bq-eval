# Results and Denominators

Full figures behind the README results table. Every number below is recomputed from the fleet bundle by [`test_doc_contract.py`](../tests/test_doc_contract.py), so this page fails CI if it drifts from `bq-eval demo --format json`.

## Verified Receipts

Static [rule views](../sql/core/step1_trace_views.sql) check every trace at **`$0` LLM judge cost** (BigQuery scan bytes apply) over `agent_events` or `_AllSpans` (`39`-col [adapter parity](../tests/test_bqaa_dual_path.py)). Scheduled queries persist sampled scores under a `2M`-token/day [quota guard](../sql/core/step3_daily_quota_guard.sql). [Dry-run validation](../benchmarks/results/bq_dry_run_receipt.json) parses **`73`** SQL statements via `sqlglot` (`17` DDL/proc skipped; opt-in GCP `--dry_run` in [`bq_dry_run.py`](../scripts/bq_dry_run.py)). SQLite mirrors BigQuery math (`< 1e-12` parity, **`157/157` tests**). The [fleet receipt](../benchmarks/results/m2_live_receipt.json) (`343` reconstructed cases, `676` spans, `333` `outcome.closed`, `0` human labels; `gemini-4-argon` reference judge; `AC2` = Quadratic Gwet's) replays below:

| Scope | Measured Result | Proof |
| :--- | :--- | :--- |
| **Twin (`50`)** | `order-support-agent` (`40`, `catalog-enrichment-agent` `10/10` unjudged `NULL`): `38/40` (`95%`) rules, `18/20` canary + `0/2` tail (`81.8%` pooled vs. `87.4%` calibrated `[64.7%, 100%]`), `$0.0232`/outcome (`$0.7160` agent + `$0.0550` judge) | [SQL tests](../tests/test_sql_contracts.py) |
| **KYC (`280`)** | `28` structural + `8` PO Box + `8` OCR (`44` `$0`-LLM rejects); `20/20` traps (`100%` `[83.9%, 100%]`, `0/216` FP); across `236` Rule-0 passing judged rows, `30/30` Flash/Argon match (`AC2 = 0.8605`, calibrated `91.53%` `[87.97%, 95.08%]`, `$0.0054`/outcome) | [Fleet receipt](../benchmarks/results/m2_live_receipt.json) |
| **RFP (`35`)** | `100%` pass (`35/35`): `A2A_PATH_A` (`8`) `$0.0000` AI vs. Flash (`17`) `$0.00035` AI vs. `>200K` Argon (`10`) `$0.7226` AI (mixes tier & cliff) | [E2E tests](../tests/test_bq_eval_end_to_end.py) |
| **Skill (`18`) & shadow (`10`)** | `100%` pass (`18/18`): `1.0.0` (`8`) `$0.08350` AI/outcome to `1.1.0` (`10`) `$0.000588` AI/outcome (`99.3%` drop, excl. wages; `9/9` auto, `1/1` escalated); `10` shadow hit `unregistered_app` (`0%`, `NULL`) | [Contract tests](../tests/test_workbench_and_contracts.py) |

[Cost model](../bq_eval/finops/engine/spend.py): on `100k` traces (`3%` tail + `5%` canary), rule screening cuts judge spend `92.2%` (`$250` to `$19.62`) while catching `100%` of rule/telemetry-detectable tail failures missed by uniform `5%` sampling.
