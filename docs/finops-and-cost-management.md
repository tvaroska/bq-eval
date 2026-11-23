# FinOps, Serving Physics & Cost Management Guide

## 1. Serving Physics: Weighted Burndown & Long-Context Cliffs (`EVAL-P08`)

Raw token counts (`input_tokens + output_tokens`) fail to reflect accelerator compute consumption or API billing under modern LLM serving physics:
- **Prefix Caching (`0.1x` / `0.2x`):** Cached prefix tokens reuse pre-computed KV-cache blocks and consume `90%` less compute than uncached prompt tokens.
- **Autoregressive Decoding & Reasoning (`4.0x` / `7.5x`):** Output tokens and internal `<thought>` (`thinking_tokens`) are memory-bandwidth bound during decoding and billed at the output multiplier.
- **`>200K` Context Cliff:** Spans whose total prompt context (`input_tokens`, inclusive of `cached_input_tokens`) exceeds `200,000` tokens trigger higher attention memory footprint and long-context pricing tiers.

[`sql/core/views.sql`](../sql/core/views.sql), [`bq_eval/core/contract/otel.py`](../bq_eval/core/contract/otel.py) (`compute_weighted_burndown_tokens()`), and [`bq_eval/core/engine/cost.py`](../bq_eval/core/engine/cost.py) (`compute_span_cost_usd()`) compute weighted burndown and cliff-aware USD cost at the **individual span level** before aggregating to the trace:

$$
W_{\text{span}} = \begin{cases}
1.0 \cdot T_{\text{uncached}} + 0.1 \cdot T_{\text{cached}} + 4.0 \cdot (T_{\text{out}} + T_{\text{think}}) & \text{if } T_{\text{in}} \le 200{,}000 \\[6pt]
2.0 \cdot T_{\text{uncached}} + 0.2 \cdot T_{\text{cached}} + 7.5 \cdot (T_{\text{out}} + T_{\text{think}}) & \text{if } T_{\text{in}} > 200{,}000
\end{cases}
$$

where $T_{\text{uncached}} = \max(0, T_{\text{in}} - T_{\text{cached}})$.

---

## 2. 6-Bucket USD FinOps Attribution (`EVAL-P08`)

Every OTel span is tagged with `finops.traffic_type` (`FinOpsTrafficType`) and joined against `agent_eval.model_pricing_catalog` so platform teams separate production spend from evaluation, optimization, and untagged overhead:

| Traffic Bucket (`finops.traffic_type`) | Source Spans | Optimization Lever |
| :--- | :--- | :--- |
| **`production_inference`** | Standard user-facing agent turns (`gemini-3.8-flash` / `gemini-3.5-flash-lite`) | Prompt prefix caching (`0.1x`), model tier routing, context window pruning (`<=200K`). |
| **`conflict_escalation`** | High-reasoning fallback turns (`gemini-4-argon`) triggered when deterministic `Rule 0` or tool verification fails | Improve tool schemas and `Rule 0` pre-checks to reduce escalation rate. |
| **`agent_gym_eval`** | Synthetic pre-production regression suites and stress tests | Schedule batch runs into off-peak Provisioned Throughput (PT) valleys. |
| **`autorater_judge`** | Tier 2 `AI.GENERATE_TABLE` LLM-as-a-Judge scoring (`gemini-3.8-flash` workhorse / `gemini-4-argon` calibration) | Stratified 3-Tier funnel (`5%`–`10%` canary + `100%` error/SLA-tail) saves `90–95%` vs. 100% LLM judging. |
| **`optimize`** | Automated prompt/rubric optimization and offline tuning sweeps | Schedule into off-peak idle PT capacity valleys alongside batch evaluation workloads. |
| **`unclassified`** | Untagged spans falling outside declared traffic buckets | Enforce `finops.traffic_type` tagging so unattributed workloads surface separately. |

---

## 3. Cost per Verified Outcome (`CPVO`, `EVAL-P04` & `EVAL-P11`) & Eval FinOps ROI

Traditional dashboards report **Cost per Trace**, which rewards cheap models even when they hallucinate or violate schemas. `bq-eval` establishes **Cost per Verified Outcome (`CPVO`)** as the primary North Star metric across two complementary BigQuery views:

1. **Trace-Level Rogan-Gladen Calibrated CPVO (`v_agent_eval_and_finops_summary` in [`sql/core/summary_views.sql`](../sql/core/summary_views.sql), `EVAL-P04`):**

$$
\text{CPVO}_{\text{calibrated}} = \frac{\text{Total Spend USD (6 Buckets + BQ Query Cost)}}{\text{Production Traces} \times \hat{P}(\text{Rule 0 Pass}) \times \hat{\theta}_{\text{RG}}(\text{Semantic Pass} \mid \text{Rule 0 Pass})}
$$

2. **Multi-Agent Outcome-Level CPVO (`v_cost_per_verified_outcome` in [`sql/core/step1_trace_views.sql`](../sql/core/step1_trace_views.sql) & `v_enterprise_cost_per_verified_outcome` in [`sql/core/views.sql`](../sql/core/views.sql), `EVAL-P11`):** Groups spans across traces, agents, and human pauses by `outcome.id`, proportionally allocating `1.0x` outcome-level compute/storage infra (`v_infra_cost_estimate`) and table-only Step-2 judge spend across multi-agent slices without fanout duplication, while keeping failed/abandoned/escalated outcomes in the numerator (no survivor bias).

Alongside `CPVO`, `compute_eval_finops_roi()` quantifies the **Eval FinOps Savings** of the 3-Tier funnel:
- **Baseline (`100%` LLM Judge):** Every production trace is evaluated by an LLM judge.
- **3-Tier Funnel (`bq-eval`):** `100%` of traces run Tier 1 deterministic `Rule 0` checks at `$0` LLM cost; routine Tier 2 `AI.GENERATE_TABLE` semantic scoring runs on `step1_pass = TRUE` `5%`–`10%` deterministic canary + tool-error/SLA-tail traces, while optional diagnostic mode (`v_step2_all_trace_rubric_prompts` / `include_rule0_failures=True`) inspects `Rule 0` failures separately (excluded from $\hat{\theta}_{\text{RG}}$ via `WHERE rule0_pass = TRUE`).

---

## 4. Prefix-Cache `1/N` Dilution Diagnostics

When an orchestration graph routes requests across $N$ distinct system-prompt templates or injects dynamic timestamps/UUIDs at the top of the system prompt, the effective prefix-cache hit rate degrades as $\approx 1/N$.

`diagnose_prefix_cache_dilution()` inspects span-level `cached_input_tokens / input_tokens`, accounts for the `>200K` long-context multiplier (`1.8x` burndown and `2x` default USD savings above `200,000` tokens), detects `1/N` prompt-template fragmentation, and calculates the exact monthly USD and weighted-token savings recoverable by canonicalizing static prefix blocks above dynamic turn context.

---

## 5. Non-Fabrication (`NFR = 0.0%`) Cost-vs-Quality Pareto Frontier (`EVAL-P09`)

Model routing should never trade away factual integrity for lower token cost. `compute_pareto_frontier()` filters candidate configurations through a hard **Zero-Fabrication Gate (`nfr == 0.0` and `rule0_pass_rate >= min_rule0_pass_rate`)** before computing the non-dominated Pareto frontier over `(cost_per_verified_outcome_usd, calibrated_pass_rate, p95_latency_ms)`.

---

## 6. FOCUS 1.2 Two-Pool Chargeback & Off-Peak PT Batch-Eval Water-Filling

**`bq-eval` is the missing Silver/Gold attribution layer between raw AI telemetry and Cloud Billing FOCUS 1.2.** Cloud Billing exports stop at SKU and reservation rows with blank application labels and no `Prod` vs. `Eval` split. For organizations running reserved Provisioned Throughput (`PT` / `GSU` commitments) alongside Pay-As-You-Go (`PAYG`) overflow, [`bq_eval/finops/engine/spend.py`](../bq_eval/finops/engine/spend.py) and `v_capacity_hourly_burndown_export` in [`sql/finops/capacity_views.sql`](../sql/finops/capacity_views.sql) export hourly weighted burndown directly to the `capacity` planner:
1. **FOCUS 1.2 Two-Pool & Three-Pool Attribution (`allocate_focus_two_pool_chargeback`, `allocate_focus_three_pool_hourly_chargeback`, `v_focus_three_pool_hourly_chargeback`):** Splits `reserved_pt_fixed_cost_usd` into **Utilized PT Cost** (Pool 1 production + Pool 2 off-peak `eval`/`optimize` water-fill) and **Unutilized Idle Capacity Tax** (billed to the central platform owner). Pool 3 PayGo overflow is classified at the **application level** per hour: `pool_total_funded_gsus` sums `funded_gsus` once per registered app in `app_registry` (including idle funded apps), and each app's combined demand across all lanes (`prod + eval + optimize`) is compared against its funded share (`demand_share <= funded_gsu_share` in SQL; `app_total_tokens <= entitlement` in Python) to separate **Involuntary Spillover** from **Tenant Burst Spillover**.
2. **Off-Peak PT Water-Filling (`plan_off_peak_eval_schedule`):** Identifies hourly valleys between reserved PT capacity and live production demand, scheduling `agent_gym_eval` and batch `autorater_judge` workloads into zero-marginal-cost idle PT slots.
