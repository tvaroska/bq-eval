# Critical Rules (`bq-eval`)

Break one of these and the numbers this repository reports stop being trustworthy.

1. **Silver holds counts and facts, never prices, defaults, or a pass.**
   - Silver normalizes telemetry and SCD-2 reference tables inside BigQuery ([spec/silver.md](spec/silver.md)) across physically separate `silver_open` and `silver_restricted` datasets (`*_open` views never scan restricted payload buckets). It carries no prices, no judge verdicts, no pass, and no limit comparisons.
   - Missing telemetry values stay `NULL` and are counted — including `sampling_probability` (`NULL` never defaults to `1.0`). An application's self-reported failure (`bq_eval.app_failed`) counts against the turn in Gold while safety blocks (`guardrail.intervention`) stay separate from crashes; self-reported pass or "verified" flags count for nothing.

2. **Rule checks run in SQL before any judge call — and never fail open.**
   - Each add-on defines its own pass in SQL over Silver counts and facts, at no model cost. Evaluation's default: a turn is completed when its root span ended without error and the application reported no failure; an outcome is verified only when every trace completed (or the closing trace where retries are expected).
   - Never send a trace to an LLM judge for errors, failed tool calls, or deterministic policy/HITL rules SQL already flags. Never silently drop rootless traces (`has_root_span = false`) containing tool calls or writes, never treat unregistered tools as read-only on regulated workloads, and enforce RFC 8785 JCS hashing, SoD, TTL, and single-use binding on HITL approvals.

3. **No judge data means no score.**
   - An application with no judged traces, any unjudged population stratum, fewer than 10 judged traces, fewer than 5 positive or 5 negative reference labels, or a judge with Youden's $J$ or Wilson-adjusted $J' \le 10^{-6}$ reports `NULL` (`UNJUDGED` or `UNCALIBRATED`), never `100%`.
   - Failing tails are oversampled, so raw judge pass rates are biased. Gold evaluation reports the stratum-weighted Rogan-Gladen rate with a Lang & Reiczigel (2014) boundary-safe 95% confidence interval, computed strictly over completed turns that pass the deterministic gates.
   - Trust a judge only when its agreement with the reference (Quadratic Gwet's `AC2`) meets the application's gate (`>= 0.80` default).

4. **Treat trace text as untrusted.**
   - Escape `&`, `<`, and `>` in prompts, retrieved context, and answers before wrapping them in `<untrusted_text>`.
   - Judges emit reasoning before their score on a 4-point ordinal scale with no neutral middle (`>= 3` passes).

5. **Price per call, divide by verified outcomes.**
   - Gold cost attributes calls across six spend buckets: `production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`, `optimize`, and `unclassified`.
   - Price each model call before aggregating (1P Gemini calls above 200K input tokens pay higher input, cache, and output multipliers; `FLEX` and 3P models apply SKU-specific weights), keep failed/escalated attempts in the numerator, enforce `0.0%` negative flips on regulated/safety paired gates, and evaluate cascaded model/threshold changes by combined `(primary + escalation + eval)` cost above the application's calibrated accuracy floor.
   - Scale counts and cost by the sampling probability, and report "never" checks only for applications that send every trace.

6. **Layer and package boundaries hold.**
   - Gold add-ons read only the published Silver views (never raw `spans` directly); external repositories never name `bq-eval` views ([spec/layers.md](spec/layers.md)).
   - Python `core` imports no sibling subpackage; `finops` and `deploy` build on `core`; `demo` builds on `core` and `finops`; only `cli` imports everything (`tests/test_isolation_guard.py`, `specs/ast.rules.yaml`).

7. **Nothing private leaks.**
   - No customer names, internal codenames, internal shortlinks, bug IDs, sibling `../` path escapes, or workstation paths (`tests/test_repo_hygiene.py`).
   - Use public model names (`gemini-3.8-flash`, `gemini-4-argon`, `gemini-3.5-flash-lite`) and say "Gemini platform".
