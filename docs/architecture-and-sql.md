# BigQuery Progressive Architecture: Step 1 -> Step 2 -> Step 2.5 (Skill) -> Step 3

## 1. Progressive Adoption Architecture (Source = Always Traces)

To eliminate day-1 setup friction while supporting full enterprise governance when needed, `bq-eval` is organized into **Steps 1, 2, 2.5, and 3**, with enterprise extensions kept open:

| Step | SQL & Skill Artifacts | Tables Needed | Skill Needed? | What It Delivers |
| :--- | :--- | :--- | :--- | :--- |
| **Step 1** | [`sql/step1_trace_views.sql`](../sql/step1_trace_views.sql) | **`0` new tables** (uses existing `otel_spans`) | **No** (100% static SQL views & queries) | `v_step1_trace_waterfall`, `v_step1_trace_eval`, and `v_step1_cost_per_outcome`: waterfalls, latency P95, tool/loop errors, `$0` SQL checks, and **Day-1 Cost per Application, Business Unit & Outcome**. |
| **Step 2** | [`sql/step2_rubrics.csv`](../sql/step2_rubrics.csv) & [`sql/step2_stateless_eval.sql`](../sql/step2_stateless_eval.sql) | **`1` table: `rubrics`** (GCS CSV external table or native table) | **No** (Static SQL for `100%` and `10%` of traces) | Seeds 4 example 4-point rubrics (`groundedness_4pt_v1`, `tool_trajectory_4pt_v1`, `completeness_4pt_v1`, `escalation_policy_4pt_v1`), creates `v_step2_trace_rubric_prompts`, and runs stateless `AI.GENERATE_TABLE` evals on **100% of traces** or a **10% deterministic sample** without saving results. |
| **Step 2.5** | [`skills/bq-eval/SKILL.md`](../skills/bq-eval/SKILL.md) & [`skills/bq-eval/references/sql_recipes.md`](../skills/bq-eval/references/sql_recipes.md) | Same `1` rubric table | **Yes** (Agentic Skill) | Translates plain-English requirements into new 4-point rubrics, custom trace slice queries (`5%` sample, `only the last 1 hour of traces`, error/SLA outliers, per-app filters), and custom domain Step-1+ SQL checks. |
| **Step 3** | [`sql/step3_saved_results.sql`](../sql/step3_saved_results.sql) | **`2` tables: `rubrics` + `eval_results`** | Optional | Persists evaluation verdicts (`eval_run_id`) to track longitudinal trends (`v_step3_eval_runs`) and run paired model/prompt comparisons (`v_step3_paired_run_comparison` for `0%` negative flips). |
| **Open Extensions** | [`sql/ddl.sql`](../sql/ddl.sql) & [`sql/views.sql`](../sql/views.sql) | Optional (`sme_labels`, `app_registry`, `model_pricing_catalog`) | Optional | Plugs in `~100` blind human spot-checks (`sme_labels`) for Rogan-Gladen 95% CIs, App Hub registries, and `FOCUS 1.2` Provisioned Throughput sizing. |

---

## 2. Step-by-Step SQL Walkthrough (`sql/step1_*.sql` → `sql/step3_*.sql`)

### 2.1 Step 1: Static SQL Views Over Existing Traces (`sql/step1_trace_views.sql`)
- **`agent_eval.v_step1_trace_waterfall`**: Normalizes `apphub_application_id`, `business_unit`, and `cost_center` via `COALESCE` (so workloads without App Hub tags still group cleanly by `gcp.agent.id` or `service_name`), and calculates span-level burndown and USD spend using built-in default Gemini rates (`<=200K` and `>200K` cliffs).
- **`agent_eval.v_step1_trace_eval`**: Rolls up each `trace_id` to compute `trace_latency_ms`, `tool_call_count`, `tool_error_count`, `step1_pass` (`$0` deterministic check), and `trace_cost_usd`.
- **`agent_eval.v_step1_cost_per_outcome`**: Aggregates by `(apphub_application_id, business_unit, cost_center)` to report `step1_pass_rate`, `p95_latency_ms`, `total_cost_usd`, and **`step1_cost_per_outcome_usd`**.

### 2.2 Step 2: `1` Rubric Table + Stateless `100%` & `10%` Eval (`sql/step2_stateless_eval.sql`)
- **`agent_eval.rubrics`**: Can be mounted directly from [`sql/step2_rubrics.csv`](../sql/step2_rubrics.csv) in Cloud Storage (`CREATE OR REPLACE EXTERNAL TABLE`) or created as a native BigQuery table seeded via `MERGE`.
- **`agent_eval.v_step2_trace_rubric_prompts`**: Joins `v_step1_trace_eval` with `trace_payloads` and `rubrics`, escaping `&`, `<`, and `>` inside `<untrusted_text>` blocks.
- **Static Query 2A (`100%` Full Traces)** & **Static Query 2B (`10%` Sample)**: Run `AI.GENERATE_TABLE` statelessly and return `reasoning_trace`, `ordinal_score`, `verified_pass`, and `trace_cost_usd` directly in the query result.

### 2.3 Step 2.5: Agentic Skill (`skills/bq-eval/SKILL.md`)
- Uses [`skills/bq-eval/references/sql_recipes.md`](../skills/bq-eval/references/sql_recipes.md) to author new 4-point ordinal rubrics (`MERGE INTO agent_eval.rubrics`), compose custom slice queries (`MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 5` for `5%`, `trace_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)` for the last 1 hour), and create custom domain SQL views (`v_step1_custom_domain_eval`).

### 2.4 Step 3: Saved Eval Outputs & Paired Comparisons (`sql/step3_saved_results.sql`)
- **`agent_eval.eval_results`**: Partitioned by `DATE(evaluated_at)` and clustered by `(apphub_application_id, rubric_id, trace_id)`.
- **`agent_eval.v_step3_eval_runs` & `agent_eval.v_step3_paired_run_comparison`**: Summarize historical runs by `eval_run_id` and compare baseline vs. candidate runs on matching `trace_id`s to flag any negative flips (`is_negative_flip`).

---

## 3. Open Enterprise Extensions (`sql/ddl.sql` & `sql/views.sql`)

When teams expand beyond Step 3 into multi-BU chargeback and calibrated Model Risk Management (`MRM`), [`sql/ddl.sql`](../sql/ddl.sql) and [`sql/views.sql`](../sql/views.sql) provision the full 7-table schema:

| Table / Model | Partitioning & Clustering | Purpose |
| :--- | :--- | :--- |
| `agent_eval.app_registry` | Clustered by `apphub_application_id, cost_center` | Maps `gcp.apphub.application.id` (or off-GCP agent IDs via `COALESCE`) to business unit, cost center, SLA (`sla_latency_ms`), and monthly token budget. |
| `agent_eval.otel_spans` | `DATE(start_time)` · Clustered by `apphub_application_id, trace_id, span_kind` | Streaming OTel `gen_ai.*` span sink (`invoke_agent`, `execute_tool`, `chat`, `retrieval`) with token counts (`input_tokens`, `cached_input_tokens`, `output_tokens`, `thinking_tokens`) and `finops_traffic_type`. |
| `agent_eval.trace_payloads` | `DATE(ingested_at)` · Clustered by `apphub_application_id, trace_id` | Sanitized prompt, retrieved context, and final response text for `AI.GENERATE_TABLE` judge execution. |
| `agent_eval.golden_cases` | Clustered by `apphub_application_id, rubric_id, risk_tier` | Version-controlled golden evaluation scenarios and expected invariants (`Rule 0` contracts, risk tiers). |
| `agent_eval.eval_results` | `DATE(evaluated_at)` · Clustered by `apphub_application_id, rubric_id, trace_id` | Output sink for `AI.GENERATE_TABLE` judge runs (`ordinal_score`, `binary_pass`, `judge_input_tokens`, `judge_output_tokens`, `judge_thinking_tokens`). |
| `agent_eval.sme_labels` | `DATE(labeled_at)` · Clustered by `apphub_application_id, rubric_id` | Stratified human SME audit labels (`human_pass`, `judge_pass`, `rule0_pass`) used to compute empirical $\widehat{\text{TPR}}$, $\widehat{\text{TNR}}$, Youden's $J$, and Gwet's $AC_2$. |
| `agent_eval.model_pricing_catalog` | Clustered by `model_name` | Effective-dated SKU pricing and serving-physics burndown multipliers (`<=200K` standard and `>200K` long-context cliffs). |
| `agent_eval.gemini_4_argon_judge` | Remote Cloud AI Model | Remote connection model (`gemini-4-argon`, `T = 0.0`) invoked by `AI.GENERATE_TABLE`. |

---

## 4. Portable Application & Agent Identity Resolution (`EVAL-P07`)

Workloads running on GKE, Cloud Run, or Agent Engine emit `gcp.apphub.application.id`, whereas local development, multi-cloud, or OSS frameworks emit `gcp.agent.id` or `service.name`. Both `sql/step1_trace_views.sql`, `sql/views.sql`, and `bq_eval.otel.resolve_application_id()` normalize application identity via the same deterministic precedence chain:

```sql
COALESCE(
  gcp_apphub_application_id,
  JSON_VALUE(attributes, '$."gcp.apphub.application.id"'),
  JSON_VALUE(attributes, '$."gcp.agent.id"'),
  NULLIF(service_name, 'agent-service'),
  'unregistered_app'
) AS apphub_application_id
```

---

## 5. Prompt-Injection Defense in `AI.GENERATE_TABLE` (`EVAL-P08`)

Untrusted user prompts (`p.user_prompt`), retrieved chunks (`p.retrieved_context`), and model responses (`p.final_response`) could contain literal `</untrusted_text>` tags designed to break out of the judge's XML boundary and spoof `<score>4</score>`.

`v_step2_trace_rubric_prompts` and `bq_eval.sql_eval.build_ai_generate_table_judge_sql()` enforce two hard safeguards:
1. **SQL-Level XML Entity Escaping:** Wraps `p.user_prompt`, `p.retrieved_context`, and `p.final_response` in nested `REPLACE(REPLACE(REPLACE(COALESCE(col, ''), '&', '&amp;'), '<', '&lt;'), '>', '&gt;')` before interpolating into `<untrusted_text>` blocks.
2. **Deterministic Hash Sampling (`FARM_FINGERPRINT`):** Supports `10%` (`< 10`), `5%` (`< 5`), or `100%` error/SLA-tail sampling (`NOT t.rule0_pass OR t.tool_error_count > 0 OR t.trace_latency_ms > t.sla_latency_ms`), cutting judge token spend by `90–95%`.

---

## 6. Conditional Rogan-Gladen Calibration & Fail-Closed Guard (`EVAL-P05`)

Because semantic evaluation is conditioned on traces passing Step 1 / `Rule 0` deterministic checks ($P(\text{Verified Pass}) = P(\text{Rule 0 Pass}) \times P(\text{Semantic Pass} \mid \text{Rule 0 Pass})$):

1. **Conditional Prevalence ($\hat{p}_{\text{obs} \mid R_0}$):** `v_agent_eval_and_finops_summary` computes `raw_judge_pass_rate` strictly over judged traces where `rule0_pass = TRUE` (`AVG(CASE WHEN e.rule0_pass THEN CAST(e.binary_pass AS FLOAT64) END)`), while still billing `100%` of `autorater_judge_usd` across both canary and error/SLA-tail judge runs.
2. **Confusion-Matrix Filtering:** `sme_calibration` restricts rows to `WHERE rule0_pass = TRUE AND human_pass IS NOT NULL AND judge_pass IS NOT NULL` so unjudged audit rows never corrupt $\widehat{\text{TPR}}$ or $\widehat{\text{TNR}}$.
3. **Fail-Closed `NULL` Guard:** When `judged_samples = 0` or Youden's $J = \widehat{\text{TPR}} + \widehat{\text{TNR}} - 1 \le 10^{-6}$, `calibrated_pass_rate`, `calibrated_ci_95_lower`, `calibrated_ci_95_upper`, and `cost_per_verified_outcome_usd` return `NULL` (with `calibration_status = 'UNJUDGED'` or `'UNCALIBRATED'`), never fabricating a `100%` pass rate.
