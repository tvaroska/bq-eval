---
type: atom
up: "layers.md"
tags: [bq-eval, gold]
---

# Gold Add-Ons

> How the Evaluation, Monitoring, and Cost add-ons are built over the published Silver views. Contract: [spec/gold.md](../spec/gold.md).

## Shared structure

Each Gold add-on lives in its own SQL module, with stored tables only for Gold-owned state: daily results, judge outputs, blind SME calibration labels, GCS WORM export manifests, price catalogs, and workload thresholds. Every add-on reads published Silver views, open twins, and SCD-2 reference tables — never raw `spans` or Bronze.

- **Explicit windows & kept results:** Scheduled add-on jobs call the Silver functions with an explicit time window, once it has settled, and store daily results together with frozen SCD-2 reference values and population counts.
- **Fail-closed sampling & orphan-write safety:** Applications the registry declares `sampling_mode = 'full'` are not scaled; otherwise counts and cost divide by `traces.sampling_probability`, and a missing probability leaves the scaled number empty beside the observed one (`NULL` never defaults to `1.0`); "never" checks require `sampling_mode = 'full'`. Settled mutating traces whose parent was lost (`missing_parent_count`) route to orphaned-activity alerts, paired with reverse `action.committed -> tool_calls` reconciliation.
- **Concrete slot & PT backpressure circuit breaker (`ADR-031`):** All compaction, evaluation, and judging jobs run inside isolated reservations enforcing per-query `max_bytes_billed` ceilings and pre-flight `INFORMATION_SCHEMA.JOBS` utilization checks (shedding non-critical `RL-3`–`RL-5` judging and capping tail-sampling fan-out during `5–10x` outages), while gateways route `autorater_judge` / `optimize` traffic to off-peak or `FLEX` pools.
- **Preventive vs. detective boundary (`ADR-032`):** BigQuery runs on `>= 5m` schedules; live blocking runs in the Preventive Request-Path Enforcement Port (gateways/proxies), emitting attested receipts audited here.

## Evaluation pipeline

1. **Completed turns & structured guardrail separation:** A turn is a trace with at least one agent, model-call or tool span; completed turns have root span OK and `bq_eval.app_failed` not true, and calls with content missing are never scored. Structured `guardrail.intervention` rows (`control_id`, `control_layer`, `control_action`) stay distinct from crashes; they are platform-verified only when they sit in a platform-only landing place.
2. **Deterministic gate view (100% of rows before judging):** Evaluates workload SQL rules:
   - Structured OBO identity (`RL-1`–`RL-3` fails closed on `identity_attested != true`), self-only/tenant match (`parameter_matched` or `token_enforced`), and `UC2` `DKIM`/`DMARC` sender auth.
   - Pre-ticket rejection, runbook escalation, sandbox status (`executes_code` vs. `modifies_workspace` split), `1x` turn-delta DLP or inline guardrail verdicts, JSON schemas, citations, disclaimers.
   - Authoritative `skill_registry` check on `protected_block_sha256` (ending self-certified compliance hashes), MNPI `barrier_domain` wall checks, silent empty-RAG detection (`acl_dropped_count == retrieved_count`), `Guide`/`Answer` `state.transition` invariants, and deterministic numeric/prohibited-entity checks.
   - Fail-closed unregistered-tool blocking, HITL write verification (`action_id`, RFC 8785 hash, TTL, Maker-Checker SoD + notional quorums, single-use `event_id`, `model.approved` MRM release), **reverse `action.committed -> tool_calls` orphan-commit checks**, extended `allowed_models` + Pre-GA `runtime_lane` enforcement, and dark-agent registry reconciliation.
3. **Bounded session reconstruction, deterministic judging & guardrail FP/FN:**
   - For `previous_interaction_id` stateful turns, reconstructs Turns `1..k-1` + memory state over a bounded lookback window into PII-masked `<untrusted_text>` (failing closed `MISSING_SESSION_CONTEXT` if incomplete).
   - Samples `5–10%` baseline + `100%` slow/throttled/escalated/low-confidence/`thumbs_down` tails, plus a separate **stratified guardrail FP/FN audit** over `guardrail.intervention` (`action = 'block'`) and passing turns.
   - Pins deterministic judges (`temperature = 0`, `seed`, immutable `judge_model_snapshot_id`, `judge_prompt_sha256`, independent judge-model family, `allowed_models.zdr_allowed = true` where the allow-list requires it), runs inside the text's home cloud (`ATIF-v1.8` / `AI.EVALUATE_AGENT` preview aligned), and stores keyed by `(eval_run_id, trace_id, rubric_id, rubric_version, judge_model_snapshot_id)`.
4. **Stratified `SR 11-7` calibration, GCS Object-Lock WORM, live A/B, delayed truth & Optimizer Port:**
   - Computes **stratum-specific $Se_h/Sp_h$** (and per-cloud $Se/Sp$) Rogan-Gladen conditional + unconditional prevalence with design-based joint 95% CIs and $[0,1]$ clamping, `SME-vs-SME AC2` and `judge-vs-SME AC2 >= 0.80` **paired with binary cut-point agreement**, SME tie-break, `RL-1` (`>= 100`) floors, confidence ECE (`UC4`), cohort parity (`< 2%` delta, `UC9`), and expanded expiry triggers (TTL or judge snapshot/prompt/rubric/model/agent/skill/system-instructions/tool-defs/corpus change).
   - Exports PII-free **GCS Bucket Retention-Lock + CMEK** WORM snapshots (`snapshot_sha256`, `calibration_snapshot_id`, input digests, frozen SCD-2 JSON) paired with per-subject crypto-shredding DEKs for RTBF.
   - Pairs offline runs on `case_id` and live interactive migrations on `shadow_of_trace_id` / `experiment_id` / `variant_id`, reconciles delayed `T+1` conversions (`UC6`) and `T+7..30d` `postmortem.verified` (`UC7`), exposes the `< 5s` **Local/CI Eval Runner Port** (`CI_ONLY`), and publishes the versioned **Optimizer Gold Export Port** (`eval_run_id`, `candidate_id`, `optimizer_mode`, `train`/`holdout` `case_id` disjointness, `nudge_id`, `CI_ONLY` Pareto score, `min_cache_tokens` floor guard).

## Monitoring pipeline

SQL views over `silver_open` (`traces_open`, `outcomes_open`, `model_calls`, `tool_calls_open`, `events_open`) compute SLAs from trace start (net closure minus customer wait), 5-minute missing-requester audits, E2E & TTFT percentiles, `429` throttling, settled-trace & `remote_fragment_pending` skew flags, tool-loop/edit-test/query-retry caps, dynamic `tool_definitions_sha256` churn alerts, per-`nudge_id` thumbs/conversion rates, and bounded session rollups reading **`thought_signature_present` directly from core `model_calls`**.

## Cost pipeline & `capacity` export

1. **Call & downstream compute pricing view:** Prices each LLM and `embeddings` `model_calls` row (`>200K` cliffs keyed on `input + cache_read`, `FLEX`, 3P), adds container compute (`sandbox.cpu_sec` + `sandbox.mem_gb_sec`), vector/memory-store attribution, and downstream warehouse compute **reconciled against `INFORMATION_SCHEMA.JOBS` via `db.query.job_id` / `bytes_billed`**, across six spend buckets.
2. **Attribution & M:N `CPVO` views:** Joins via `trace_owner_app_id`, `outcome_id`, and `outcome_links` (allocating M:N incident cost across linked outcomes) to report `CPVO`, cascade total-cost & `FLEX`-vs-idle-PT arbitrage, page-band spend, and user/org rollups.
3. **Versioned `capacity` export (`ADR-001`):** Exports hourly chargeback/burndown, `burst_metrics_1m`, and `RL-1`..`RL-5` risk tiers consumed by the sibling `capacity` repository.
4. **With the capacity extension:** Joins `requests` on `x-client-request-id` + `(trace_id, span_id)`, prices each attempt individually before collapsing, runs explicit **3-pool `FOCUS 1.2` spillover, pro-rata `FIXED_TERM`/`AUTO_RENEW` `order_id` attribution, and Cloud Billing invoice reconciliation**, flags `eval`/`optimize` hitting daytime PT, separates `gateway_failover_attempts` from `caller_retry_count`, and attributes `embeddings` pools ([capacity.md](capacity.md)).

## More than one cloud or region

With [clouds](clouds.md), in-cloud text checks and judges execute inside each cloud/spoke's **In-Cloud Judge Port** (dedicated in-VPC compute on non-GCP clouds) and stage text-free verdicts + open twins into the multi-region Hub via the scheduled **Hub Transfer Port**.
