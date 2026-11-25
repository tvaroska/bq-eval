---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 5: Regulated Action-Taking Workflow

> A regulated enterprise workflow (`RL-1`) where agents make policy-governed decisions and execute state-changing writes into systems of record (such as approvals, payouts, credits, or access changes) under Preventive Request-Path enforcement, detective fail-closed HITL/SoD/quorum audits, reverse orphan-commit reconciliation, dual-SME `SR 11-7` calibration, PII-free GCS Object-Lock WORM snapshots, and `0.0%` regression gates.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Execute high-impact, policy-governed enterprise transactions that mutate systems of record (for example, claims/credit adjustments, financial/procurement approvals, or access provisioning). |
| **Tools & actions** | **State-mutating tools (`tools.writes_state = true`) and downstream APIs**: read context, verify policy rules, create/update records, and park or route decisions for **human-in-the-loop (HITL)** sign-off (including notional/risk-tiered multi-approver quorums). |
| **Lifecycle** | Multi-step workflow that may pause for human review and resume hours or days later before closing a single business outcome. |
| **Preventive vs. detective split** | Live write authorization (inline RFC 8785 hash verification, Maker-Checker SoD, TTL, `allowed_models` blocking) runs in the **Preventive Request-Path Enforcement Port** (tool/gateway proxy); BQ Eval runs independent detective audits (`>= 5m`) and persists regulatory evidence. |
| **Governance & risk** | Regulated (`RL-1`, `payload_profile = 'regulated_worm'`, `sampling_mode = 'full'`, mandatory SCD-2) requiring MRM release sign-off (`model.approved`), stratified dual-SME calibration (`>= 100` judged floor), PII-free **GCS Object-Lock + CMEK** WORM snapshots with RTBF crypto-shredding, and `0.0%` negative-flip release gates. |
| **Cloud, models & serving** | Multi-region Hub (`US`/`EU`) with location-matched CMEK; private connectivity into isolated SoR `workflow_events` perimeter; cross-cloud/on-prem SoR relays carry attested OBO delegation (`RFC 8787` `aud`). |

## Telemetry & Data Expectations

- **Business outcome & pause correlation**: Link all agent steps, policy checks, HITL review/approval events, MRM `model.approved` release markers, and final writes across pauses/resumes to one `outcome.id` (including `rejected`, `denied`, `abandoned`, and `escalated`).
- **Isolated authoritative SoR ingestion**: Ingest authoritative approval (`outcome.approved` with `action_args`, the tool name plus the arguments as the tool received them, `approved_expires_at`, approver `actor_id`s), MRM release (`model.approved`), and committed-write (`action.committed` with the same `action_args` object and `sor_signature_ref`; Silver computes both hashes) events into a **separate VPC-SC-isolated system-of-record `workflow_events` table** over private connectivity.
- **Attested write & OBO signals**: Emit execution-time RFC 8785 `bq_eval.args_sha256` (`args_hash_attested = true`), `action_id`, `idempotency_key`, and write-IAM-attested structured OBO claims (`identity_attested = true`, `auth_method`, `issuer`, `aud`, `delegation_chain_hash`; unattested fallback prohibited on `RL-1`).
- **PII-free WORM audit snapshots**: Export immutable snapshots to **GCS Bucket Retention-Lock + CMEK** archives (freezing SQL, rubric, judge snapshot, prompt hash, `calibration_snapshot_id`, SCD-2 reference JSON, policy verdicts, input digests, hashes, `sor_signature_ref`, and `snapshot_sha256` — zero raw PII, paired with per-subject crypto-shredding DEKs for RTBF).

## Required Checks, Alerts & Views

1. **Fail-closed authorization, Maker-Checker SoD, notional quorums & reverse orphan-commit audit**
   - Fail closed on unregistered tools, unattested `user.id`, missing sampling metadata, or rootless mutating traces; verify every `writes_state` execution matches authoritative `outcome.approved` on `action_id`, RFC 8785 `approved_args_sha256 = args_sha256`, valid TTL (`approved_expires_at`), Maker-Checker SoD (`actor_id != user_id`) plus notional-tiered multi-approver quorums, single-use `event_id`, and authoritative `model.approved` release status.
   - Run **reverse `action.committed -> tool_calls` reconciliation** to detect any committed SoR write whose `execute_tool` span was dropped at the `65,533 B` OTLP span cap.
2. **Idempotency & duplicate-mutation prevention**
   - Detect duplicate write tool calls or double-committed actions across retries, timeouts, or loops by cross-checking `tool_calls` against authoritative `action.committed`.
3. **Audit-grade `SR 11-7` judge calibration & MRM lifecycle**
   - Enforce stratum-specific $Se_h/Sp_h$ Rogan-Gladen + design-based joint CIs, dual-blind `SME-vs-SME AC2` and `judge-vs-SME AC2 >= 0.80` **plus binary pass/fail cut-point agreement**, `RL-1` (`>= 100` judged) sample floor, deterministic pinned independent judges (`temperature = 0`, `seed`, snapshot ID), and automatic expiry on TTL or judge/prompt/rubric/model/agent/skill/system-instructions/tool-defs/corpus changes.
4. **Pre-release non-regression gating**
   - Pair candidate vs. baseline on `case_id` enforcing `0.0%` negative flips on compliance/policy gates.
5. **True cost per verified outcome (`CPVO`)**
   - Divide total spend (including failed, rejected, denied, escalated, and judge spend) by verified outcomes.
