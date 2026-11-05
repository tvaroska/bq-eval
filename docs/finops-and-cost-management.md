# FinOps, Serving Physics & Cost Management Guide

## 1. Serving Physics: Weighted Burndown & Long-Context Cliffs (`EVAL-P04`)

Raw token counts (`input_tokens + output_tokens`) fail to reflect accelerator compute consumption or API billing under modern LLM serving physics:
- **Prefix Caching (`0.1x` / `0.2x`):** Cached prefix tokens reuse pre-computed KV-cache blocks and consume `90%` less compute than uncached prompt tokens.
- **Autoregressive Decoding & Reasoning (`4.0x` / `7.5x`):** Output tokens and internal `<thought>` (`thinking_tokens`) are memory-bandwidth bound during decoding and billed at the output multiplier.
- **`>200K` Context Cliff:** Spans whose total prompt context (`input_tokens + cached_input_tokens`) exceeds `200,000` tokens trigger higher attention memory footprint and long-context pricing tiers.

Both [`sql/views.sql`](../sql/views.sql) and [`bq_eval/otel.py`](../bq_eval/otel.py) compute weighted burndown at the **individual span level** before aggregating to the trace:

$$
W_{\text{span}} = \begin{cases}
1.0 \cdot T_{\text{uncached}} + 0.1 \cdot T_{\text{cached}} + 4.0 \cdot (T_{\text{out}} + T_{\text{think}}) & \text{if } T_{\text{in}} \le 200{,}000 \\[6pt]
2.0 \cdot T_{\text{uncached}} + 0.2 \cdot T_{\text{cached}} + 7.5 \cdot (T_{\text{out}} + T_{\text{think}}) & \text{if } T_{\text{in}} > 200{,}000
\end{cases}
$$

where $T_{\text{uncached}} = \max(0, T_{\text{in}} - T_{\text{cached}})$.

---

## 2. 4-Bucket USD FinOps Attribution (`EVAL-P07`)

Every OTel span is tagged with `finops.traffic_type` (`FinOpsTrafficType`) and joined against `agent_eval.model_pricing_catalog` so platform teams separate production spend from evaluation and escalation overhead:

| Traffic Bucket (`finops.traffic_type`) | Source Spans | Optimization Lever |
| :--- | :--- | :--- |
| **`production_inference`** | Standard user-facing agent turns (`gemini-3.8-flash` / `gemini-3.5-flash-lite`) | Prompt prefix caching (`0.1x`), model tier routing, context window pruning (`<=200K`). |
| **`conflict_escalation`** | High-reasoning fallback turns (`gemini-4-argon`) triggered when deterministic `Rule 0` or tool verification fails | Improve tool schemas and `Rule 0` pre-checks to reduce escalation rate. |
| **`agent_gym_eval`** | Synthetic pre-production regression suites and stress tests | Schedule batch runs into off-peak Provisioned Throughput (PT) valleys. |
| **`autorater_judge`** | Tier 2 `AI.GENERATE_TABLE` LLM-as-a-Judge scoring (`gemini-4-argon`) | Stratified 3-Tier funnel (`5%` canary + `100%` error/SLA-tail) saves `90–95%` vs. 100% LLM judging. |

---

## 3. Cost per Verified Outcome (`CPVO`) & Eval FinOps ROI

Traditional dashboards report **Cost per Trace**, which rewards cheap models even when they hallucinate or violate schemas. `bq-eval` establishes **Cost per Verified Outcome (`CPVO`)** as the primary North Star metric:

$$
\text{CPVO} = \frac{\text{Total Spend USD (4 Buckets)}}{\text{Production Traces} \times \hat{P}(\text{Rule 0 Pass}) \times \hat{\theta}_{\text{RG}}(\text{Semantic Pass} \mid \text{Rule 0 Pass})}
$$

Alongside `CPVO`, `compute_eval_finops_roi()` quantifies the **Eval FinOps Savings** of the 3-Tier funnel:
- **Baseline (`100%` LLM Judge):** Every production trace is evaluated by `gemini-4-argon`.
- **3-Tier Funnel (`bq-eval`):** `100%` of traces run Tier 1 SQL checks at `$0` LLM cost; Tier 2 `AI.GENERATE_TABLE` runs only on `5%` deterministic canary + `100%` error/SLA-tail traces.

---

## 4. Prefix-Cache `1/N` Dilution Diagnostics

When an orchestration graph routes requests across $N$ distinct system-prompt templates or injects dynamic timestamps/UUIDs at the top of the system prompt, the effective prefix-cache hit rate degrades as $\approx 1/N$.

`diagnose_prefix_cache_dilution()` inspects span-level `cached_input_tokens / input_tokens`, detects `1/N` prompt-template fragmentation, and calculates the exact monthly USD and weighted-token savings recoverable by canonicalizing static prefix blocks above dynamic turn context.

---

## 5. Non-Fabrication (`NFR = 0.0%`) Cost-vs-Quality Pareto Frontier

Model routing should never trade away factual integrity for lower token cost. `compute_pareto_frontier()` filters candidate configurations through a hard **Zero-Fabrication Gate (`nfr == 0.0` and `rule0_pass_rate >= min_rule0_pass_rate`)** before computing the non-dominated Pareto frontier over `(cost_per_verified_outcome_usd, calibrated_pass_rate, p95_latency_ms)`.

---

## 6. FOCUS 1.2 Two-Pool Chargeback & Off-Peak PT Batch-Eval Water-Filling

For organizations running reserved Provisioned Throughput (`PT` / `GSU` commitments) alongside Pay-As-You-Go (`PAYG`) overflow, `bq_eval.finops` exports hourly weighted burndown directly to the `capacity` planner:
1. **FOCUS 1.2 Two-Pool Attribution (`allocate_focus_two_pool_chargeback`):** Splits reserved PT commitment cost into **Utilized PT Cost** (allocated proportional to each app's active weighted burndown) and **Unutilized Idle Capacity Tax** (attributed to the central platform/reservation owner so tenant apps are never penalized for off-peak idle hours).
2. **Off-Peak PT Water-Filling (`plan_off_peak_eval_schedule`):** Identifies hourly valleys between reserved PT capacity and live production demand, scheduling `agent_gym_eval` and batch `autorater_judge` workloads into zero-marginal-cost idle PT slots.
