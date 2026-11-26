---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 9: Stateful Fiduciary & Wealth Advisory

> A long-lived, multi-week stateful fiduciary/financial-coaching agent (`RL-1`) serving individuals across persistent cross-session memory (`Memory Bank`), stateful delta-turn APIs (`previous_interaction_id`), ACL-filtered vector retrieval, `Guide`-vs-`Answer` state machines, and dozens of layered compliance controls — requiring bounded Gold session-context reconstruction, full memory-mutation lineage, guardrail false-positive/false-negative stratified judging, deterministic numeric/prohibited-entity gates, per-tenant Row Access Policies, and PII-free GCS WORM + RTBF crypto-shredding.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Deliver personalized, regulated fiduciary/wealth-wellness guidance across multi-week client journeys where pure-text advice carries regulatory liability even with zero system-of-record writes. |
| **Topology & state** | Stateful turn APIs (`bq_eval.previous_interaction_id`, `state_mode = 'stateful_cmek_platform' | 'stateful_cmek_gateway'`), persistent cross-session memory operations (`memory_*`), ACL-filtered vector retrieval (`data_source_id`, `corpus_digest`, `acl_dropped_count`), explicit `Guide` -> `Answer` state transitions, and warm handoffs to licensed human advisors. |
| **Custody, privacy & RTBF** | Regulated (`RL-1`, `payload_profile = 'regulated_worm'`, `sampling_mode = 'full'`, mandatory SCD-2) under strict client-PII isolation (per-tenant `unit_entitlements.tenant_id` caller filtering on `light` `silver_restricted` functions plus BigQuery Row Access Policies on stored tables per `ADR-044`/`ADR-047`, PII-masked LLM judge prompts, and PII-free **GCS Object-Lock + CMEK** WORM snapshots paired with per-client crypto-shredding DEKs for GDPR/CCPA Right-to-Be-Forgotten deletion). |
| **Compliance controls** | Dozens of layered deterministic, DLP, safety, and policy guardrails (`guardrail.intervention` with `control_id`, `layer`, `action`) plus `RL-1` (`>= 100` judged) dual-SME calibration and `< 2%` cohort/demographic parity monitoring. |

## Telemetry & Data Expectations

- **Cross-session memory & state-machine lineage**: Emit `memory_op` (`read | write | update | delete`), `memory_id`, `memory_sha256`, and `memory_source_provenance` on `tool_calls` (preventing ungrounded or stale memory carry-over), plus `state.transition` (`Guide` vs. `Answer`) events in `events` and `previous_interaction_id` on delta turns.
- **Employer/tenant-bound ACL retrieval & structured identity**: Emit write-IAM-attested structured OBO claims (`identity_attested = true`, `auth_method`, `issuer`, `aud`, `tenant_id` / employer-tier binding; unattested fallback prohibited on `RL-1`) alongside retrieval `data_source_id`, `corpus_digest`, candidate count, and `acl_dropped_count`.
- **Structured guardrail verdicts**: Emit `guardrail.intervention` carrying exact `control_id`, `control_layer` (`gateway | dlp | model_armor | app_rule`), and `control_action` (`block | redact | shadow`) so false-positive over-refusals and false-negative escapes are auditable per control.
- **Warm advisor handoffs**: Emit `handling.started` / `handling.ended` with verified memory/context packet digests.

## Required Checks, Alerts & Views

1. **Bounded Gold session-context reconstruction (fail-closed `MISSING_SESSION_CONTEXT`)**
   - Reconstruct Turns `1..k-1` and active memory state over a bounded lookback window into PII-masked `<untrusted_text>` before judging Turn `k` (catching contradictions against earlier age, tax-filing, or account disclosures and Turn `k-1` memory poisoning); fail closed with `MISSING_SESSION_CONTEXT` when prior context is missing.
2. **Deterministic fiduciary, silent empty-RAG & state-machine gates (100% coverage)**
   - Run 100%-coverage SQL rules checking statutory numeric limits / plan rules against retrieved `turn_context`, prohibited-entity/ticker rules, `Guide`/`Answer` `state.transition` invariants, and **silent empty-RAG alerts** (`acl_dropped_count == retrieved_count` when documents existed prior to ACL filtering).
3. **Guardrail FP/FN stratified judging & `RL-1` (`>= 100`) `SR 11-7` calibration**
   - Judge stratified samples of `guardrail.intervention` (`action = 'block'`) turns to measure false-positive over-refusal per `control_id`, alongside false-negative audits on passing turns; enforce stratum-specific $Se_h/Sp_h$, binary cut-point agreement alongside `AC2 >= 0.80`, `RL-1` (`>= 100`) SME floors, and `< 2%` cohort/demographic parity checks.
4. **PII isolation, GCS Object-Lock WORM & RTBF compliance**
   - Enforce per-tenant isolation via `unit_entitlements.tenant_id` inside `light` `silver_restricted` functions (`ADR-044`, `ADR-047`) plus Row Access Policies on stored tables, PII-masked judge prompts, PII-free GCS Retention-Lock WORM snapshots (`snapshot_sha256`), and per-subject crypto-shredding keys for RTBF erasure.
5. **Memory, vector-retrieval & session-cache cost attribution**
   - Attribute vector-retrieval, memory-store, and `previous_interaction_id` prefix-cache cliff costs alongside LLM inference into per-client advisory `CPVO`.
