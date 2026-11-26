---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 6: Front-Office Advisory & Continual Optimization

> A multi-cloud / hybrid advisory agent (`RL-2`) supporting client-facing analysts and sales specialists across cloud and on-premises data tools, producing multi-part structured recommendations (`nudge_id`), capturing structured feedback and delayed `T+1` conversion signals, and feeding a closed-loop automated prompt/skill optimizer gated on `< 5s` offline search loops, authoritative `skill_registry` compliance pins, holdout accuracy, and prefix-cache stability.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Synthesize market, client, and research context into multi-part structured recommendations and proactive nudges for human advisors, while continuously improving mounted prompt skills via an automated offline optimizer (`400–800` candidate rollouts). |
| **Topology & tools** | Multi-step agent orchestrating cloud LLM inference alongside hybrid cloud and on-premises enterprise data tools (via MCP or transit-cloud service proxies), producing multiple structured outputs/nudges within a single turn. |
| **Two-speed optimization loop** | Fast candidate search (`bq_eval.traffic_type = 'optimize'`, `eval_split = 'train'`) executes sub-5-second scoring via the **`< 5s` Local/CI Eval Runner Port** (`CI_ONLY` deterministic composite Pareto scorers `r_task * r_eff`) — never polling `>= 5m` BigQuery during search — while candidate promotion gates run in BigQuery on disjoint `holdout` splits with calibrated verification. Supports both in-origin (`optimizer_mode = 'in_origin'`) and twin-sandbox (`optimizer_mode = 'twin_sandbox'`) execution. |
| **Cloud & serving** | Multi-cloud / hybrid (`US` multi-region Hub + non-GCP/on-prem spokes via Hub Transfer Port); interactive business-hours Provisioned Throughput (`RL-2`) strictly isolated at the Preventive Request-Path Enforcement Port from off-peak or `FLEX` optimizer sweeps. |
| **Traffic shape** | Interactive peak during market/business hours; bursty offline optimization batches outside daytime production windows. |

## Telemetry & Data Expectations

- **Hybrid trace correlation & skew tracking**: Propagate W3C trace context (`traceparent`) and `x-client-request-id` across cloud and on-premises tool hops (via `On-Prem -> Transit Cloud -> Hub` mTLS Collector relays) so remote tool errors, latencies, and `requests` stitch in the text-free Hub on `(trace_id, parent_span_id)` with `remote_fragment_pending` skew flagging.
- **Authoritative skill & optimizer lineage**: Record `gen_ai.skill.name`, `bq_eval.skill_version`, `bq_eval.skill_sha256`, `bq_eval.protected_block_sha256`, `bq_eval.mounted_skills`, `bq_eval.eval_split` (`train | holdout`), `eval_run_id`, `candidate_id`, `optimizer_mode`, and `system_instructions`/`tool_definitions` refs — verified against authoritative SCD-2 **`skill_registry`** (synced via GitOps, with `CODEOWNERS`/MRM gates on compliance-block edits).
- **Per-nudge feedback & delayed `T+1` conversions**: Emit **`bq_eval.nudge_id`** on `step_outputs`, advisor `feedback.thumbs_up`/`thumbs_down` (`feedback_reason`), and downstream `recommendation.accepted`/`rejected` events; support delayed `T+0`/`T+1` external order/conversion reconciliation (`client_id` / `instrument` / `time_window`) when downstream systems of record lack W3C `outcome.id` baggage.
- **Cache & model-call telemetry**: Emit `thought_signature_present`, `upstream_endpoint_location`, and prefix-cache read/write tokens on core `model_calls`, compared against SKU `min_cache_tokens` thresholds (`4,096` regional vs. `6,144` global floors).

## Required Checks, Alerts & Views

1. **Authoritative compliance-block & guardrail non-drift**
   - Verify span `protected_block_sha256` against authoritative SCD-2 `skill_registry` (never self-certified) and fail closed if an optimizer candidate alters protected compliance/disclaimer text or degrades `guardrail.intervention` enforcement.
2. **Holdout-gated continual optimization & isolated skill promotion**
   - Publish the fixed **Optimizer Gold Export Port** (`eval_run_id`, `candidate_id`, `optimizer_mode`, `train`/`holdout` `case_id` disjointness check, isolated vs. co-mounted `mounted_skills` contamination check, `CI_ONLY` Pareto score, calibrated holdout pass rate); block promotion on holdout regression or train/holdout leakage.
3. **Prefix-cache floor & cost-efficiency guard**
   - Compare compacted prompt/skill prefixes against model/region `min_cache_tokens` and monitor `thought_signature_present` to catch cache-hit collapse (`89.9% -> <10%`) before promotion.
4. **Per-nudge structured feedback & delayed `T+1` conversion attribution**
   - Track advisor feedback reason codes and conversion/rejection rates per `nudge_id` (including delayed `T+1` conversion reconciliation), oversampling `thumbs_down` and rejected nudges for review.
5. **Preventive & detective eval/optimizer PT isolation**
   - Block `optimize` / `agent_gym_eval` traffic from daytime `RL-1`/`RL-2` PT pools at the gateway (Preventive Port) and flag any violation in Gold Cost (`traffic_type_attested`).
