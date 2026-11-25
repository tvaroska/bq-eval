# Critical Rules (`bq-eval`)

Break one of these and the numbers this repository reports stop being trustworthy.

1. **Silver holds counts and facts, never prices, defaults, or a pass.**
   - Silver normalizes telemetry and SCD-2 refs (`app_registry`, `tools`, `skill_registry`) in BigQuery ([spec/silver.md](spec/silver.md)) across separate `silver_open` and `silver_restricted` datasets (split compaction SAs, per-BU caller filter in `silver_restricted` functions with RAPs on stored tables, `outcome.subject` excluded from `outcomes_open`, zero Shape-3 scans from `*_open`). `*_open` carries no argument hashes or file digests. The high-volume profile is design only.
   - Missing values stay `NULL` and are counted (`sampling_probability = NULL` never defaults to `1.0`). Silver publishes the `user.id` it saw; "verified" and application identity come only from where a record landed, never from an attribute, and Gold applies the tier rule. Self-reported `bq_eval.app_failed` counts against the turn while structured `guardrail.intervention` (`control_id`/`layer`/`action`) stays separate; self-reported pass or `protected_block_sha256` counts for nothing without `skill_registry`.

2. **Detective SQL runs before judges — Preventive blocking lives in request-path gateways.**
   - BigQuery runs asynchronously (`>= 5m`) and never blocks live writes, DLP leaks, or `1s/10s` `429` shedding; synchronous controls belong to the Preventive Request-Path Enforcement Port, and `< 5s` PR/sweep gates use the Local/CI Eval Runner Port (`CI_ONLY`).
   - Never judge errors, failed tools, calls with missing content, or deterministic policy/HITL failures. Never drop rootless mutating traces, never treat unregistered tools as read-only on regulated apps, split `tools.modifies_workspace` from `writes_state`, require structured OBO claims and `args_hash_attested` from platform-class records, bind `tool_name` + args in RFC 8785 hashes, enforce SoD/quorums/TTL/single-use/`model.approved`, and run reverse `action.committed -> tool_calls` orphan-commit checks.

3. **No calibrated judge data means no score.**
   - Any unjudged stratum, missed risk-tiered floor (`10` default / `>= 100` `RL-1`), missing session context (`MISSING_SESSION_CONTEXT`), stale calibration (TTL or judge snapshot/prompt/rubric/model/agent/skill/system-instructions/tool-defs/corpus change), or $J / J' \le 10^{-6}$ reports `NULL`, never `100%`.
   - Report stratum-specific $Se_h/Sp_h$ (and per-cloud $Se/Sp$) Rogan-Gladen conditional + unconditional pass rates with design-based joint 95% CIs and $[0,1]$ clamping over completed rule-passing turns (plus separate guardrail FP/FN audits).
   - Pin deterministic independent judges (`temperature = 0`, `seed`, snapshot ID, `zdr_allowed`), require `SME-vs-SME AC2` and `judge-vs-SME AC2 >= 0.80` paired with binary cut-point agreement, tag CI/optimizer scores `CI_ONLY`, and export regulated WORM snapshots to PII-free GCS Retention-Lock + CMEK with per-subject RTBF crypto-shredding.

4. **Treat trace text as untrusted and honor location boundaries.**
   - Escape `&`, `<`, and `>` in `<untrusted_text>`, mask direct PII where required, and emit reasoning before 4-point ordinal scores (`>= 3` passes).
   - Multi-region (`US`/`EU`) is the default Hub topology; never mix BigQuery locations or regional/multi-region KMS keys inside one query domain; keep raw text strictly in its home cloud/perimeter.

5. **Price per attempt, reconcile to billing, divide by verified outcomes.**
   - Attribute spend across six buckets (`production_inference`, `conflict_escalation`, `agent_gym_eval`, `autorater_judge`, `optimize`, `unclassified`).
   - Price each LLM, `embeddings`, and `1:N` `requests` attempt individually first (`x-client-request-id`, `>200K` on `input + cache_read`, honest 3P `inferred_ambiguous`, 3-pool `FOCUS 1.2` spillover + pro-rata `order_id` + Cloud Billing reconciliation), add container and `INFORMATION_SCHEMA.JOBS`-reconciled warehouse compute, keep failures/escalations in `CPVO` (M:N via `outcome_links`), enforce `max_bytes_billed` / `INFORMATION_SCHEMA` slot breakers, flag `eval`/`optimize` on daytime PT, enforce `0.0%` negative flips, and scale by `sampling_probability` unless the registry declares `full`.

6. **Layer and package boundaries hold.**
   - Gold reads only published Silver views (never raw `spans`); sibling repos consume versioned export contracts only (`ADR-001`).
   - Python `core` imports no sibling subpackage; `finops` and `deploy` build on `core`; `demo` builds on `core` and `finops`; only `cli` imports everything (`tests/test_isolation_guard.py`, `specs/ast.rules.yaml`).

7. **Nothing private leaks.**
   - No customer names, internal codenames, internal shortlinks, bug IDs, sibling `../` path escapes, or workstation paths (`tests/test_repo_hygiene.py`).
   - Use public model names (`gemini-3.8-flash`, `gemini-4-argon`, `gemini-3.5-flash-lite`) and say "Gemini platform".
