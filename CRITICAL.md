# Critical Invariants (`bq-eval`)

1. **Rule 0 — The Deterministic Money Firewall (`$0` LLM Cost Across 100% of Traffic)**:
   - Never invoke an LLM judge to check deterministic invariants: span status errors, tool execution errors (`tool_call_status != 'SUCCESS'`), retry loops (`tool_call_count > 8`), latency SLAs (`sla_latency_ms`), or per-trace token/USD budget caps.
   - Always execute Step 1 / `Rule 0` in BigQuery SQL (`v_step1_trace_eval` / `v_trace_sql_evaluation`) or SQLite (`evaluate_traces_in_sqlite()`) across 100% of traces at `$0` LLM cost *before* invoking `AI.GENERATE_TABLE`.

2. **Fail-Closed Calibration & Prevalence Debiasing (`No Unjudged 100% Defaults`)**:
   - Never report raw uncalibrated LLM judge pass rates ($\hat{p}_{\text{judge}}$) as ground truth when SME calibration labels exist, and **never** default unjudged workloads (`judged_samples == 0`) or degenerate calibration slices (`youden_j <= 1e-6`) to `calibrated_pass_rate = 1.0`.
   - Unjudged or uncalibrated applications must evaluate fail-closed to `None` / SQL `NULL` (`calibration_status = "UNJUDGED"` or `"UNCALIBRATED"`).
   - Estimate conditional semantic pass rate $\hat{\theta}_{\text{RG}} = \hat{P}(\text{Semantic Pass} \mid \text{Rule 0 Pass})$ strictly over `rule0_pass = TRUE` judged traces using the **Rogan-Gladen / PPI** estimator (`rogan_gladen_calibrate()`) with delta-method 95% confidence intervals, paired with prevalence-robust **Gwet's AC2 $\ge 0.80$** (`compute_gwet_ac2()`).

3. **Prompt-Injection Escaping & Verdict Field Ordering**:
   - Escape `&`, `<`, and `>` in all untrusted user prompts, retrieved RAG chunks, and candidate outputs (`wrap_untrusted_text()` in Python and `escape_sql_xml_expr()` / nested `REPLACE()` in `build_ai_generate_table_judge_sql()` and `v_step2_trace_rubric_prompts`) before wrapping in `<untrusted_text>`.
   - Enforce `reasoning_trace` **before** `ordinal_score` (`OrdinalScoreOutput`). Prohibit 5-point Likert scales; use explicit 4-point ordinal scales (`1..4`, no neutral midpoint, default passing threshold `>= 3`).

4. **Outcome-Adjusted Unit Economics (`USD Cost per Verified Outcome`)**:
   - Attribute spend across four explicit `FinOpsTrafficType` buckets (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`).
   - Model span-level `<= 200K` (`1.0x` input, `0.1x` cached, `4.0x` output+thoughts) and `> 200K` (`2.0x` input, `0.2x` cached, `7.5x` output+thoughts) Provisioned Throughput burndown cliffs (`compute_weighted_burndown_tokens()`) before trace aggregation.
   - Gate model right-sizing (`gemini-4-argon` $\to$ `gemini-3.8-flash` / `gemini-3.5-flash-lite`) on paired item-level **Non-Fabrication / Negative Flip Rate (`NFR`) $= 0.0\%$** (`compute_pareto_frontier()`).

5. **Zero Internal, Customer, or Machine-Path Leaks (`ADR-005`)**:
   - Keep this repository 100% customer-agnostic, externally portable, and free of internal codenames, internal shortlinks, bug tracker IDs, sibling `../` `sys.path` traversal, or hardcoded workstation paths (`tests/test_repo_hygiene.py`).
   - Default to public model names (`gemini-3.8-flash`, `gemini-4-argon`, `gemini-3.5-flash-lite`) and **"Gemini platform"** terminology.
