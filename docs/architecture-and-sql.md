# BigQuery Dual-Path Progressive Architecture: Step 1 -> Step 2 -> Step 2.5 (Skill) -> Step 3

## 0. Dual-Path Capture -> Single Execution Engine (`ADR-008`)

**BQ Eval & FinOps (`bq-eval`)** decouples how agent telemetry enters BigQuery from how quality, risk, and unit economics are computed:
- **Light Capture (`BQAA` — [`sql/core/bqaa_adapter_views.sql`](../sql/core/bqaa_adapter_views.sql))**: Developers stream events from Python ADK (`BigQueryAgentAnalyticsPlugin`) or LangGraph (`BigQueryCallbackHandler`) into a single auto-created `agent_events` table. Zero-copy views (`v_bqaa_otel_spans` and `v_bqaa_trace_payloads`) deduplicate Storage Write API retries by `event_id` and map token counts and inline payloads onto the `otel_spans` / `trace_payloads` contract while preserving strict `'unregistered_app'` fallback (`ADR-003`).
- **Full Capture (`OTel` — [`sql/core/ddl.sql`](../sql/core/ddl.sql))**: Multi-service and multi-cloud fleets emit standard OTLP to `telemetry.googleapis.com` (`obs_traces._AllSpans` + `obs_logs._AllLogs` + BigLake `completions`) with server-stamped `apphub.application.id` and W3C `baggage` (`outcome.id`).
- **Hybrid Mode**: Combines both lanes via `UNION ALL` (`NOT EXISTS` trace deduplication in [`sql/core/bqaa_adapter_views.sql`](../sql/core/bqaa_adapter_views.sql)) so server-stamped `_AllSpans` rows take precedence while `BQAA`-only developer workloads fill in seamlessly.

---

## 1. Progressive Adoption Architecture (Source = Always Traces or `BQAA` Events)

To eliminate day-1 setup friction while supporting full enterprise governance when needed, `bq-eval` is organized into **Steps 1, 2, 2.5, and 3**, with enterprise extensions kept open:

| Step | SQL & Skill Artifacts | Tables Needed | Skill Needed? | What It Delivers |
| :--- | :--- | :--- | :--- | :--- |
| **Step 1** | [`sql/core/step1_trace_views.sql`](../sql/core/step1_trace_views.sql) | **`0` new tables** (uses existing `otel_spans`) | **No** (100% static SQL views & queries) | `v_step1_trace_waterfall`, `v_step1_trace_eval`, and `v_step1_cost_per_outcome`: waterfalls, latency P95, tool/loop errors, `$0` SQL checks, and **Day-1 Cost per Application, Business Unit & Outcome**. |
| **Step 2** | [`sql/core/step2_rubrics.csv`](../sql/core/step2_rubrics.csv) & [`sql/core/step2_stateless_eval.sql`](../sql/core/step2_stateless_eval.sql) | **`1` table: `rubrics`** (GCS CSV external table or native table) | **No** (Static SQL for `100%` and `10%` of traces) | Seeds 8 canonical 4-point rubrics (`groundedness_4pt_v1`, `tool_trajectory_4pt_v1`, `completeness_4pt_v1`, `escalation_policy_4pt_v1`, `kyc_policy_compliance_4pt_v1`, `rfp_quote_completeness_4pt_v1`, `contradiction_grounding_4pt_v1`, `pii_perturbation_invariance_4pt_v1`), creates `v_step2_all_trace_rubric_prompts` and `v_step2_trace_rubric_prompts`, and runs stateless `AI.GENERATE_TABLE` evals on **100% of traces** or a **10% deterministic sample** without saving results. |
| **Step 2.5** | [`skills/bq-eval/SKILL.md`](../skills/bq-eval/SKILL.md) & [`skills/bq-eval/references/sql_recipes.md`](../skills/bq-eval/references/sql_recipes.md) | Same `1` rubric table | **Yes** (Agentic Skill) | Translates plain-English requirements into new 4-point rubrics, custom trace slice queries (`5%` sample, `only the last 1 hour of traces`, error/SLA outliers, per-app filters), and custom domain Step-1+ SQL checks. |
| **Step 3** | [`sql/core/step3_saved_results.sql`](../sql/core/step3_saved_results.sql) & [`sql/core/step3_daily_quota_guard.sql`](../sql/core/step3_daily_quota_guard.sql) | **`2` tables: `rubrics` + `eval_results`** *(plus optional `eval_daily_quota_config`)* | Optional | Persists evaluation verdicts (`eval_run_id`, `outcome_id`) across Engine A and Engine B to track longitudinal trends (`v_step3_eval_runs`), run paired model/prompt comparisons (`v_step3_paired_run_comparison` for `0%` negative flips), and cap scheduled judge token spend (`v_step2_daily_quota_status`). |
| **Open Extensions** | [`sql/core/ddl.sql`](../sql/core/ddl.sql) & [`sql/core/views.sql`](../sql/core/views.sql) | Optional (`sme_labels`, `app_registry`, `model_pricing_catalog`) | Optional | Plugs in `~100` blind human spot-checks (`sme_labels`) for SQL Quadratic Gwet's `AC2 >= 0.80` + Rogan-Gladen 95% CIs (`v_agent_eval_and_finops_summary`), effective-dated multi-agent outcome CPVO (`v_enterprise_cost_per_verified_outcome` with per-app `app_registry.storage_usd_per_outcome`), App Hub registries, and `FOCUS 1.2` Provisioned Throughput sizing. |

---

## 2. Step-by-Step SQL Walkthrough (`sql/step1_*.sql` → `sql/step3_*.sql`)

### 2.1 Step 1: Static SQL Views Over Existing Traces (`sql/core/step1_trace_views.sql`)
- **`agent_eval.v_step1_trace_waterfall`**: Normalizes `apphub_application_id`, `business_unit`, and `cost_center` via `COALESCE` (so workloads missing App Hub registration fall back directly to `'unregistered_app'` and `'unassigned_bu'`), and calculates span-level burndown and USD spend using built-in default Gemini rates (`<=200K` and `>200K` cliffs).
- **`agent_eval.v_step1_trace_eval`**: Rolls up each `trace_id` to compute `trace_latency_ms`, `tool_call_count`, `tool_error_count`, `step1_pass` (`$0` deterministic check), and `trace_cost_usd`.
- **`agent_eval.v_step1_cost_per_outcome`**: Aggregates by `(apphub_application_id, business_unit, cost_center)` to report `step1_pass_rate`, `p95_latency_ms`, `total_cost_usd`, and **`step1_cost_per_outcome_usd`**.

### 2.2 Step 2: `1` Rubric Table + Stateless Dual-Engine Eval (`sql/core/step2_stateless_eval.sql`)
- **`agent_eval.rubrics`**: Can be mounted directly from [`sql/core/step2_rubrics.csv`](../sql/core/step2_rubrics.csv) in Cloud Storage (`CREATE OR REPLACE EXTERNAL TABLE`) or created as a native BigQuery table seeded with the 8 canonical 4-point rubrics (`gemini-3.8-flash` default workhorse, `passing_score = 3`), including `EVAL-P10` contradiction-aware grounding and PII perturbation invariance rubrics.
- **`agent_eval.v_step2_all_trace_rubric_prompts` & `agent_eval.v_step2_trace_rubric_prompts`**: `v_step2_trace_rubric_prompts` filters `WHERE step1_pass = TRUE` so routine Step-2/Step-3 semantic scoring skips `$0` deterministic rejects, while `v_step2_all_trace_rubric_prompts` preserves failing traces for optional diagnostic error-tail inspection (with $\hat{\theta}_{\text{RG}}$ strictly restricted to `rule0_pass = TRUE` per `ADR-004`). Both escape `&`, `<`, and `>` inside `<untrusted_text>` blocks.
- **Engine A (`AI.GENERATE_TABLE` — All Regions incl. `US`/`EU`)**: Runs 4-point ordinal rubrics statelessly on `100%` or `10%` of traces with `NOT NULL` schema fields (`reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL`) and returns `reasoning_trace`, `ordinal_score`, `verified_pass`, and `trace_cost_usd`.
- **Engine B (`AI.EVALUATE_AGENT` — Private Preview, Non-Public Single-Region `BQAA` `AgentData` TVF)**: Scores Step-1 passing turns (`v_bqaa_eval_turn_filtered`) against Google's 7 managed trajectory metrics (`tool_use_quality_v1`, `final_response_quality_v1`, `hallucination_v1`, `safety_v1`, etc.) with zero `CREATE MODEL` or `CREATE CONNECTION` setup (unannounced private preview TVF not yet in public Cloud BigQuery docs).

### 2.3 Step 2.5: Agentic Skill (`skills/bq-eval/SKILL.md`)
- Uses [`skills/bq-eval/references/sql_recipes.md`](../skills/bq-eval/references/sql_recipes.md) to author new 4-point ordinal rubrics (`MERGE INTO agent_eval.rubrics`), compose custom slice queries (`MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 5` for `5%`, `trace_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)` for the last 1 hour), and create custom domain SQL views (`v_step1_custom_domain_eval`).

### 2.4 Step 3: Saved Eval Outputs, Paired Comparisons & Quota Guard (`sql/core/step3_saved_results.sql`, `sql/core/step3_daily_quota_guard.sql`)
- **`agent_eval.eval_results`**: Partitioned by `DATE(evaluated_at)` and clustered by `(apphub_application_id, rubric_id, trace_id)`, with `outcome_id` preserved for `EVAL-P11` joins and idempotent `LEFT JOIN` persistence adapters for both Engine A (`AI.GENERATE_TABLE`) and Engine B (`AI.EVALUATE_AGENT`).
- **`agent_eval.v_step3_eval_runs` & `agent_eval.v_step3_paired_run_comparison`**: Deduplicate append-only `eval_results` on `(eval_run_id, trace_id, rubric_id)` via `QUALIFY ROW_NUMBER() ... = 1`, summarize runs by `eval_run_id`, and compare baseline -> candidate pairs temporally by `MIN(evaluated_at)` (with deterministic `base.eval_run_id < cand.eval_run_id` tie-break) on matching `trace_id`s to flag negative flips (`is_negative_flip`) without reciprocal duplicates.
- **`agent_eval.eval_daily_quota_config`, `agent_eval.eval_daily_quota_reservations` & `agent_eval.run_daily_quota_guarded_judge(...)`**: Enforces global and per-application daily UTC judge token sub-caps across Engine A and Engine B (`ai_evaluate_agent_managed`) using non-deduped burn accounting, pre-flight `max_output_tokens` + `thinking_level` headroom, and atomic reservation transactions (`v_step2_daily_quota_status`, `v_step2_app_daily_quota_status`).

---

## 3. Open Enterprise Extensions (`sql/core/ddl.sql` & `sql/core/views.sql`)

When teams expand beyond Step 3 into multi-BU chargeback and calibrated Model Risk Management (`MRM`), [`sql/core/ddl.sql`](../sql/core/ddl.sql) and [`sql/core/views.sql`](../sql/core/views.sql) provision the full compatibility-view + physical-table schema:

| Object | Kind / Partitioning & Clustering | Purpose |
| :--- | :--- | :--- |
| `agent_eval.app_registry` | Table · Clustered by `apphub_application_id, cost_center` | Maps `apphub.application.id` to business unit, cost center, SLA (`sla_latency_ms`), and monthly token budget. |
| `agent_eval.otel_spans` | View over `obs_traces._AllSpans` *(or `v_bqaa_otel_spans`)* | 32-column compatibility projection of OTel `gen_ai.*` spans, token counts (`input_tokens`, `cached_input_tokens`, `output_tokens`, `thinking_tokens`), `finops_traffic_type`, and `EVAL-P11` `outcome_id` / `outcome_owner`. |
| `agent_eval.trace_payloads` | View over `obs_logs._AllLogs` `LEFT JOIN completions` *(or `v_bqaa_trace_payloads`)* | 1-row-per-`trace_id` sanitized prompt, retrieved context, and final response text for `AI.GENERATE_TABLE` judge execution. |
| `agent_eval.golden_cases` | Table · Clustered by `apphub_application_id, rubric_id, risk_tier` | Version-controlled golden evaluation scenarios and expected invariants (`Rule 0` contracts, risk tiers). |
| `agent_eval.eval_results` | Table · `DATE(evaluated_at)` · Clustered by `apphub_application_id, rubric_id, trace_id` | Output sink for Engine A & Engine B judge runs (`outcome_id`, `ordinal_score`, `binary_pass`, `judge_*_tokens`). |
| `agent_eval.sme_labels` | Table · `DATE(labeled_at)` · Clustered by `apphub_application_id, rubric_id` | Stratified blind human SME audit labels (`human_ordinal_score`, `judge_ordinal_score`, `human_pass`, `judge_pass`, `rule0_pass`) used to compute empirical $\widehat{\text{TPR}}$, $\widehat{\text{TNR}}$, Youden's $J$, and Quadratic Gwet's $AC_2$ directly in SQL. |
| `agent_eval.model_pricing_catalog` | Table · Clustered by `model_name` | Effective-dated SKU pricing and serving-physics burndown multipliers (`<=200K` standard and `>200K` long-context cliffs). |
| `agent_eval.gemini_3_8_flash_judge` / `gemini_4_argon_judge` | Remote Cloud AI Models | Remote connection models (`gemini-3.8-flash` workhorse + `gemini-4-argon` calibration/escalation, `T = 0.0`) invoked by `AI.GENERATE_TABLE`. |

---

## 4. Portable Application & Agent Identity Resolution (`EVAL-P08`)

Workloads registered in App Hub (on GKE, Cloud Run, Agent Runtime, or via WIF collector resource stamping — see [`examples/otel-collector-wif.yaml`](../examples/otel-collector-wif.yaml)) carry `gcp_apphub_application_id` or `apphub.application.id`, whereas unregistered workloads have `NULL` App Hub attributes and must be flagged rather than masked by self-reported service names. Both `sql/core/step1_trace_views.sql`, `sql/core/views.sql`, `sql/core/bqaa_adapter_views.sql`, and `bq_eval.core.contract.otel.resolve_application_id()` normalize application identity via the same deterministic precedence chain (`ADR-003`):

```sql
COALESCE(
  gcp_apphub_application_id,
  JSON_VALUE(attributes, '$."gcp.apphub.application.id"'),
  JSON_VALUE(attributes, '$."apphub.application.id"'),
  'unregistered_app'
) AS apphub_application_id
```

---

## 5. Prompt-Injection Defense in `AI.GENERATE_TABLE` (`EVAL-P02`, `EVAL-P08`)

Untrusted user prompts (`p.user_prompt`), retrieved chunks (`p.retrieved_context`), and model responses (`p.final_response`) could contain literal `</untrusted_text>` tags designed to break out of the judge's XML boundary and spoof `<score>4</score>`.

`v_step2_all_trace_rubric_prompts`, `v_step2_trace_rubric_prompts`, and `bq_eval.core.engine.sql_eval.build_ai_generate_table_judge_sql()` enforce two hard safeguards:
1. **SQL-Level XML Entity Escaping (`EVAL-P02`):** Wraps `p.user_prompt`, `p.retrieved_context`, and `p.final_response` in nested `REPLACE(REPLACE(REPLACE(COALESCE(col, ''), '&', '&amp;'), '<', '&lt;'), '>', '&gt;')` before interpolating into `<untrusted_text>` blocks.
2. **Two-Mode Stratified Deterministic Sampling (`FARM_FINGERPRINT`, `EVAL-P08`):** Routine Step-2 semantic scoring filters `step1_pass = TRUE` / `rule0_pass = TRUE` (`v_step2_trace_rubric_prompts`, `v_bqaa_eval_turn_filtered`, or `build_ai_generate_table_judge_sql(..., include_rule0_failures=False)`), routing `5%`–`10%` healthy canaries plus `100%` tool-error/SLA-tail passes (`t.tool_error_count > 0 OR t.trace_latency_ms > t.sla_latency_ms`) to the judge for `90–95%` cost reduction. Optional diagnostic root-cause sweeps (`v_step2_all_trace_rubric_prompts` or `include_rule0_failures=True`) include `NOT t.rule0_pass` failures, while `v_agent_eval_and_finops_summary` always restricts $\hat{\theta}_{\text{RG}}$ to `WHERE rule0_pass = TRUE` and bills `100%` of judge runs (`ADR-004`).

---

## 6. Conditional Rogan-Gladen Calibration, SQL Gwet's `AC2` & Fail-Closed Guard (`EVAL-P03`, `EVAL-P04`, `EVAL-P05`)

Because semantic evaluation is conditioned on traces passing Step 1 / `Rule 0` deterministic checks ($P(\text{Verified Pass}) = P(\text{Rule 0 Pass}) \times P(\text{Semantic Pass} \mid \text{Rule 0 Pass})$):

1. **Stratified Conditional Prevalence (`StratPPI`, `EVAL-P04`):** `v_agent_eval_and_finops_summary` deduplicates `eval_results`, groups `rule0_pass = TRUE` judged verdicts by stratum (`ERROR_OR_SLA_TAIL` vs. `HEALTHY_POPULATION`), and weights each stratum by its population share $w_s = N_s / \sum N_k$ from `v_trace_sql_evaluation` so `100%` tail sampling + `5%` canary sampling never introduces `20:1` selection bias, while billing `100%` of `autorater_judge_usd` across all judge calls.
2. **Blind SME Confusion-Matrix & Quadratic Gwet's `AC2` (`EVAL-P03`, `EVAL-P05`):** `sme_calibration` restricts rows to `WHERE rule0_pass = TRUE AND human_pass IS NOT NULL AND judge_pass IS NOT NULL` so unjudged audit rows never corrupt $\widehat{\text{TPR}}$, $\widehat{\text{TNR}}$, or SQL-native Quadratic Gwet's `AC2` (`gwet_ac2` and `passes_irr_gate = (gwet_ac2 >= 0.80)`, matching Python `compute_gwet_ac2()` to `< 1e-12`).
3. **Fail-Closed `NULL` Guard & Boundary-Safe CIs (`EVAL-P04`):** When `judged_samples = 0` or Youden's $J = \widehat{\text{TPR}} + \widehat{\text{TNR}} - 1 \le 10^{-6}$, `calibrated_pass_rate`, `calibrated_ci_95_lower`, `calibrated_ci_95_upper`, and `cost_per_verified_outcome_usd` return `NULL` (with `calibration_status = 'UNJUDGED'` or `'UNCALIBRATED'`), never fabricating a `100%` pass rate. Python also provides Lang & Reiczigel (2014) Wilson plug-in CIs (`rogan_gladen_wilson_ci()`) to prevent zero-width intervals when $\hat{p}$, $\widehat{\text{TPR}}$, or $\widehat{\text{TNR}}$ reach `0.0` or `1.0`.
4. **Two Complementary CPVO Views (`EVAL-P04` vs. `EVAL-P11`):**
   - `v_agent_eval_and_finops_summary` reports **trace-level `StratPPI` Rogan-Gladen calibrated CPVO** per `(apphub_application_id, rubric_id)` with 95% delta-method CIs and SQL Gwet's `AC2`.
   - `v_cost_per_verified_outcome` (Step 1) and `v_enterprise_cost_per_verified_outcome` (effective-dated pricing + latest-verdict Step-2 judge cost) report **multi-agent business-outcome CPVO** grouped by `outcome.id` with proportional `1.0x` infra and judge allocation across multi-agent slices.

---

## 6B. Multi-Agent Outcome Attribution Wire Contract (`EVAL-P11`)

A single business outcome (KYC approval, RFP award, loan consolidation) spans multiple HTTP requests, traces, A2A agent hops, and HITL pauses. `bq-eval` consumes the self-contained `EVAL-P11` wire contract directly from OpenTelemetry `_AllSpans` or `BQAA` `custom_metadata`:

| Wire Surface | Key / Span Name | Contract & Propagation Rule |
| :--- | :--- | :--- |
| **HTTP Response Header** | `x-outcome-id` | Minted once at the edge service (`outcome.id`, opaque random ID); inbound `outcome.id` from untrusted callers is ignored. |
| **W3C `baggage` Header** | `outcome.id`, `outcome.owner` | Propagated via `tracecontext,baggage` across internal A2A/RPC hops and copied onto every span attribute; persisted across HITL/queue pauses and stripped on egress to external counterparties (`apphub.external = "true"`). |
| **Zero-Duration Close Span** | `span_name = "outcome.closed"` | Emitted with `start_time == end_time` carrying `outcome.id`, `outcome.owner`, `outcome.status` (`succeeded \| failed \| abandoned \| escalated`), optional `outcome.verified`, optional `outcome.subject` (entity ID, recorded once on close, never in baggage), and optional `outcome.parent_id`. |
| **BigQuery Attribution Views** | `v_outcomes`, `v_cost_per_verified_outcome`, `v_attribution_coverage` | Joins all internal spans by `outcome.id`, keeps failed/abandoned/escalated outcomes in the CPVO numerator (no survivor bias), isolates external counterparty spans in `v_counterparty_spend`, and gates registered-app coverage at `>= 99.0%`. |

---

## 7. BigQuery Deployment Commands & Repository Map

### 7.0 Cold Project Prerequisites & Regional Compatibility

1. **Substitute Placeholders Before Running SQL**: Set `${PROJECT_ID}`, `${DATASET_ID}` (default `agent_eval`), `${PAYLOAD_BUCKET}` (`YOUR_GENAI_PAYLOAD_BUCKET` in [`sql/core/ddl.sql`](../sql/core/ddl.sql)), and `${CONNECTION_ID}` (`us.vertex_ai_connection` in `sql/core/ddl.sql` / `sql/core/step2_stateless_eval.sql`).
2. **Region Alignment (`US`/`EU` vs. Single-Region)**: `AI.GENERATE_TABLE` (Engine A) supports `US`/`EU` multi-regions (`us.vertex_ai_connection`) as well as single regions, whereas `AI.EVALUATE_AGENT` (Engine B; Private Preview, non-public TVF unannounced in public Cloud BigQuery docs) currently requires one of the 14 listed single regions (e.g., `us-central1`). Provision the dataset and Vertex AI connection in matching locations before running DDL.
3. **Create Dataset First (`CREATE SCHEMA`)**: [`sql/core/bqaa_adapter_views.sql`](../sql/core/bqaa_adapter_views.sql) defines views inside `agent_eval` without issuing `CREATE SCHEMA`. On a cold project, create `${DATASET_ID}` first via `bq mk --dataset` (or run `CREATE SCHEMA IF NOT EXISTS agent_eval`).

### 7.1 Step 1 on BigQuery (`0` New Result Tables, No Skill)

Choose **one** compatibility ingestion path (`otel_spans` + `trace_payloads`) before deploying [`sql/core/step1_trace_views.sql`](../sql/core/step1_trace_views.sql):

```bash
# Step 0: create target dataset in matching location (e.g. US for Engine A, us-central1 for Engine B)
bq mk --dataset --location=US "${PROJECT_ID}:${DATASET_ID}"

# Option 1 — Full Capture (_AllSpans + _AllLogs + BigLake completions):
#   First replace YOUR_GENAI_PAYLOAD_BUCKET and us.vertex_ai_connection in sql/core/ddl.sql
bq query --project_id="${PROJECT_ID}" --use_legacy_sql=false < sql/core/ddl.sql

# Option 2 — Light Capture (BQAA agent_events) or Hybrid Mode C:
#   First uncomment Mode A (BQAA-only) or Mode C (Hybrid) in sql/core/bqaa_adapter_views.sql
#   so agent_eval.otel_spans and agent_eval.trace_payloads exist before Step 1
bq query --project_id="${PROJECT_ID}" --use_legacy_sql=false < sql/core/bqaa_adapter_views.sql

# Deploy static Step-1 views over agent_eval.otel_spans + agent_eval.trace_payloads
bq query --project_id="${PROJECT_ID}" --use_legacy_sql=false < sql/core/step1_trace_views.sql

# Optional isolated demo seed: populates 800 synthetic traces in `agent_eval_seed` (not `agent_eval`)
bq query --project_id="${PROJECT_ID}" --use_legacy_sql=false < sql/demo/demo_time_shift_loadgen.sql
```

### 7.2 Step 2 & Step 3 on BigQuery

> [!NOTE]
> Deploying [`sql/core/step2_stateless_eval.sql`](../sql/core/step2_stateless_eval.sql), [`sql/core/step3_saved_results.sql`](../sql/core/step3_saved_results.sql), and [`sql/core/step3_daily_quota_guard.sql`](../sql/core/step3_daily_quota_guard.sql) creates only schemas, views, and the stored procedure (`$0` LLM cost). Run on-demand judge SQL via `bq-eval judge-sql` or invoke `CALL agent_eval.run_daily_quota_guarded_judge(...)` after replacing `${CONNECTION_ID}`.

```bash
# Step 2: create remote models (update CONNECTION_ID), agent_eval.rubrics, and prompt views
bq query --project_id="${PROJECT_ID}" --use_legacy_sql=false < sql/core/step2_stateless_eval.sql

# Step 3: create eval_results, deduplicated run views, paired comparisons, and quota guard tables/views
bq query --project_id="${PROJECT_ID}" --use_legacy_sql=false < sql/core/step3_saved_results.sql
bq query --project_id="${PROJECT_ID}" --use_legacy_sql=false < sql/core/step3_daily_quota_guard.sql
```

### 7.3 Local CLI Helpers & Verification Suite

```bash
python3 -m bq_eval.cli demo --html-out /tmp/bq_eval_scorecard.html
python3 -m bq_eval.cli loadgen --replicas 50 --last-hour-replicas 15
python3 -m bq_eval.cli judge-sql --engine generate-table --project-id my-gcp-project --canary-pct 5
python3 -m bq_eval.cli judge-sql --engine evaluate-agent --project-id my-gcp-project --canary-pct 10
```

See [`AGENTS.md`](../AGENTS.md) for the full file-by-file repository map and canonical lint, type-check, AST, and pytest commands.
