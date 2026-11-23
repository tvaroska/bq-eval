# Critical Rules (`bq-eval`)

Break one of these and the numbers this repo reports stop being trustworthy.

1. **Rule checks before any judge call.**
   - Errors, failed tool calls, retry loops (more than 8 tool calls), slow traces and budget overruns are checked in SQL, on every trace, at no model cost.
   - Never send a trace to an LLM judge to find something SQL can find. Run Step 1 (`v_step1_trace_eval`, `v_trace_sql_evaluation`, or `evaluate_traces_in_sqlite()`) before `AI.GENERATE_TABLE`.

2. **No judge data means no score.**
   - An app with no judged traces, any unjudged population stratum, fewer than 10 judged traces, fewer than 5 positive or 5 negative reference labels, or a judge that cannot tell pass from fail (Youden's J or adjusted J' at or below `1e-6`), reports `NULL`, never a 100% pass rate. Status is `UNJUDGED` or `UNCALIBRATED`; `rogan_gladen_calibrate()` raises `ValueError`.
   - Judged samples oversample failures, so raw judge pass rates are biased. Report the corrected rate (Rogan-Gladen, weighted by population sampling stratum, with a Lang & Reiczigel (2014) boundary-safe 95% interval), computed only over traces that passed Step 1.
   - Trust a judge only when its agreement with the reference (Gwet's AC2, `compute_gwet_ac2()`) is at least 0.80.

3. **Treat trace text as untrusted.**
   - Escape `&`, `<` and `>` in prompts, retrieved chunks and answers before wrapping them in `<untrusted_text>` (`escape_sql_xml_expr()` and the matching `REPLACE()` in the SQL views).
   - Judges write their reasoning before their score. Scores use a 4-point scale with no neutral middle; 3 or higher passes.

4. **Cost is per finished outcome, not per trace.**
   - Every span belongs to one of four spend buckets: production, escalation, eval runs, judge.
   - Price each span before adding it up. Spans above 200K input tokens on 1P Gemini models cost more per token (input 2.0x instead of 1.0x, cached 0.2x instead of 0.1x, output 7.5x instead of 4.0x), while 3P models apply SKU-specific weights (`compute_span_burndown_tokens()` and `is_span_long_context_cliff()` in `bq_eval/core/engine/cost.py`).
   - Switch to a cheaper model only if it never turns a correct answer into a wrong one on the paired test set (`compute_pareto_frontier()`).

5. **Package boundaries hold.**
   - `core` imports no other package. `finops` and `deploy` build on `core`; `demo` builds on `core` and `finops`; only `cli` imports everything. `tests/test_isolation_guard.py` and `specs/ast.rules.yaml` enforce this.

6. **Nothing private leaks.**
   - No customer names, internal codenames, internal links, bug IDs, `../` path hacks or workstation paths (`tests/test_repo_hygiene.py`).
   - Use public model names (`gemini-3.8-flash`, `gemini-4-argon`, `gemini-3.5-flash-lite`) and say "Gemini platform".
