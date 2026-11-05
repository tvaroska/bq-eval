# Critical Invariants (`bq-eval`)

1. **Rule 0 — The Deterministic Money Firewall (`$0` LLM Cost Across 100% of Traffic)**:
   - Never invoke an LLM judge to check deterministic invariants: span status errors, HTTP `429` throttles, retry counts (`attempt_number`), latency SLAs (`latency_sla_ms`), producer-verifier span pairing, or per-trace token/USD/tool-call budget caps.
   - Always execute `Rule 0` in BigQuery SQL (`v_trace_sql_evaluation`) or Python (`evaluate_traces_rule0_in_memory`) across 100% of traces at `$0` LLM cost *before* invoking `AI.GENERATE_TABLE` on the `5–10%` canary + error/SLA tail.

2. **Fail-Closed Calibration & Prevalence Debiasing (`No Unjudged 100% Defaults`)**:
   - Never report raw uncalibrated LLM judge pass rates ($\hat{p}_{\text{judge}}$) as ground truth, and **never** default unjudged workloads (`judged_samples == 0`) or degenerate calibration slices (`youden_j <= 1e-6`) to `calibrated_pass_rate = 1.0`.
   - Unjudged or uncalibrated applications must evaluate fail-closed to `None` / SQL `NULL` (`calibration_status = "UNJUDGED"` or `"UNCALIBRATED"`).
   - Estimate conditional semantic pass rate $\hat{\theta}_{\text{RG}} = \hat{P}(\text{Semantic Pass} \mid \text{Rule 0 Pass})$ strictly over `rule0_pass = TRUE` judged traces using the **Rogan-Gladen / PPI** estimator (`rogan_gladen_debiased_pass_rate()`) with delta-method 95% confidence intervals, paired with **Krippendorff's $\alpha \ge 0.80$** or prevalence-robust **Gwet's AC2 $\ge 0.80$**.

3. **Prompt-Injection Escaping & Verdict Field Ordering**:
   - Escape `&`, `<`, and `>` in all untrusted user prompts, retrieved RAG chunks, and candidate outputs (`sanitize_untrusted_text()` in Python and nested `REPLACE()` in `build_ai_generate_table_judge_sql()`) before wrapping in `<untrusted_context>` / `<untrusted_candidate>` / `<untrusted_text>`.
   - Enforce `reasoning_trace` **before** `score` and `passed` (`PointwiseVerdict`). Prohibit 5-point Likert scales; allow only Binary (`2`), 3-Point (`3`), or 4-Point (`4`) scales.

4. **Outcome-Adjusted Unit Economics (`USD Cost per Verified Outcome`)**:
   - Attribute spend across four explicit `FinOpsTrafficType` buckets (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`) joined against `model_pricing_catalog`.
   - Model span-level `<= 200K` (`1.0x` input, `0.1x` cached, `4.0x` output+thoughts) and `> 200K` (`2.0x` input, `0.2x` cached, `7.5x` output+thoughts) Provisioned Throughput burndown cliffs before trace aggregation.
   - Gate model right-sizing (`gemini-4-argon` $\to$ `gemini-3.8-flash` / `gemini-3.5-flash-lite`) on paired item-level **Negative Flip Rate (`NFR`) $= 0.0\%$**.

5. **Zero Internal, Customer, or Machine-Path Leaks (`ADR-005`)**:
   - Keep this repository 100% customer-agnostic, externally portable, and free of internal codenames, internal shortlinks, bug tracker IDs, sibling `../` `sys.path` traversal, or hardcoded workstation paths (`tests/test_repo_hygiene.py`).
   - Default to public model names (`gemini-3.8-flash`, `gemini-4-argon`, `gemini-3.5-flash-lite`) and **"Gemini platform"** terminology.
