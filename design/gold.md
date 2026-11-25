---
type: atom
up: "layers.md"
tags: [bq-eval, gold]
---

# Gold Add-Ons

> How the Evaluation, Monitoring, and Cost add-ons are built over the published Silver views. Contract: [spec/gold.md](../spec/gold.md).

## Shared structure

Each Gold add-on lives in its own SQL module, with stored tables only for Gold-owned state: daily results, judge outputs, blind SME calibration labels, audit snapshots, price catalogs, and the settings an add-on compares against (latency targets, loop budgets, approval thresholds, the settle time). Every add-on reads the six published Silver views, their open twins and the reference tables, never `spans` or Bronze.

- **Horizons and kept results:** A scheduled add-on job reads the shortest Silver horizon (or incremental date partition in `enterprise` mode) that covers its question and appends its results to its own daily tables. Each stored result carries the SCD-2 reference values and population counts it was computed from.
- **Fail-closed sampling & rootless safety:** Counts and cost divide by `traces.sampling_probability` (`NULL` never defaults to `1.0`); "never" checks require `app_registry.sampling_mode = 'full'`. Rootless traces (`has_root_span = false`) with tool calls or state writes route to an explicit orphaned-activity safety alert rather than being silently dropped.
- **Restricted and open datasets:** Checks and judges needing text or raw ids read `silver_restricted`; everything else reads `silver_open`.

## Evaluation pipeline

1. **Completed turns & guardrail separation:** A turn is completed when its root span ended without error and `bq_eval.app_failed` is not true. Policy/DLP/safety blocks (`guardrail.intervention`) are tracked in their own stratum distinct from runtime crashes (`app_failed`).
2. **Deterministic gate view:** Evaluates workload SQL rules (self-only/tenant match or `token_enforced` OBO claims, pre-ticket reject, runbook lookup vs. escalation, sandbox boundary status, `1x` turn-delta secret/PII checks, JSON schema/citation/disclaimer checks, scoped `allowed_models` enforcement when the capacity extension is installed, **fail-closed unregistered-tool blocking on regulated workloads**, and HITL `writes_state` rules verifying `action_id`, RFC 8785 `approved_args_sha256 = args_sha256`, `event_time < start_time <= approved_expires_at`, Maker-Checker SoD `actor_id != user_id`, single-use `event_id` binding, and duplicate-write checks against `action.committed`).
3. **Stratified sampling & judging:** Selects completed turns that pass deterministic gates across baseline (`5–10%`) and tail (`100%` slow, throttled, escalated, low-confidence) strata, XML-escapes `<untrusted_text>` fields from `trace_payloads` (attaching `media_uris` in place for PDFs), invokes the 4-point ordinal judge inside the text's home cloud, and stores results keyed by `(eval_run_id, trace_id, rubric_id)`.
4. **Calibration, verification, WORM snapshots & regression:** Joins judge verdicts against Gold's blind SME label table to compute fail-closed `StratPPI` Rogan-Gladen estimates and `AC2` gates, marks verified outcomes (supporting `rejected` and `denied` terminal statuses), writes **immutable WORM Gold Audit Snapshots** (pinning SQL, rubric, judge, and SCD-2 reference versions plus SoR signature refs for regulated workloads), and pairs candidate vs. baseline runs on `case_id` across `traces` and `outcomes`.

## Monitoring pipeline

SQL views over `traces`, `outcomes`, `model_calls`, `tool_calls`, and `events` compute SLAs measured from trace start (time to acknowledgement, net closure minus customer wait computed from `events`), missing-requester rates on turns with a root span, end-to-end latency and first-token percentiles built from per-call values, `429` burst throttling, per-app tool-loop/sandbox-retry alerts, thumbs rates, and session turn-length distributions. A trace is settled when its last span arrived longer ago than the settle setting.

Checks run as scheduled queries, at most every 5 minutes. BQ Eval has no faster path: a guard that must act in under a minute is a collector rule or application code in the request path.

## Cost pipeline & `capacity` export

1. **Call pricing view:** Applies the Gold price catalog to each `model_calls` row from the token counts the application reported (`>200K` cliffs, `FLEX` and 3P SKU multipliers), adds sandbox compute from `tool_calls`, and assigns the six spend buckets.
2. **Attribution views:** Joins priced calls via `trace_owner_app_id`, `outcome_id`, `agent_name`, `page_count`, and `user_id` to report `CPVO`, cascade total-cost comparisons, and per-user/context/cache efficiency. Org-unit roll-ups join a directory table the deployment provides.
3. **Versioned `capacity` export view:** Projects hourly per-app token burndown, request concurrency, `429` spillover, and turn/first-token latency quantiles into a stable JSONL/table wire contract consumed by the `capacity` repository for SLO sizing and gateway routing.
4. **With the capacity extension:** Collapses `requests` (`1:N` across failover/retries) to one billing/attempt summary per `model_calls` row before joining on `request_id` / `span_id`, and attributes `capacity_pools` commitments across `capacity_allocations` for `FOCUS 1.2` multi-pool chargeback and the per-pool part of the export ([capacity.md](capacity.md)).

## More than one cloud

With the [clouds extension](clouds.md), text checks and judges run inside each cloud's **In-Cloud Judge Port** (using that cloud's native model endpoint so non-GCP text never crosses to GCP) and stage text-free verdict rows alongside open twins into the hub via the scheduled **Hub Transfer Port**, where global calibration, cross-cloud monitoring, split-trace correlation, pricing, and `CPVO` execute.
