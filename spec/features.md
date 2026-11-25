---
type: reference
related:
  - "layers.md"
  - "silver.md"
  - "gold.md"
  - "clouds.md"
  - "capacity.md"
  - "use-cases.md"
tags: [bq-eval, features, use-cases]
---

# Architecture Feature Matrix

> Complete capability list across Bronze/Silver Core, the three Gold add-ons, and the two extensions, mapped to all 11 [target use cases](use-cases.md) (`UC1` Corporate Self-Service, `UC2` Customer Support, `UC3` Knowledge Worker, `UC4` Document Extraction, `UC5` Regulated Action Workflow, `UC6` Front-Office Advisory & Optimizer, `UC7` Agentic SRE RCA, `UC8` Enterprise DevAI, `UC9` Stateful Fiduciary Advisory, `UC10` Multi-Cloud Regulated Fleet, `UC11` Shared Multi-Tenant LLM Gateway & Bulk Embeddings).

## 1. Bronze & Silver Core ([silver.md](silver.md))

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F01 | Single-pass multi-shape span normalization & Collector Port | Up to 3 in-BQ SQL shapes routed in one single `SELECT` pass over `_AllSpans` (folding ADK Format 1 `call_llm` + child `generate_content`) plus an upstream OTel Collector / Bronze Adapter Port for custom proxies, coding SDKs, and non-conforming frameworks; missing fields stay `NULL` and are counted. | UC1–UC11 |
| F02 | Turn-scoped delta payloads, BigLake Parquet Shape 3 & inline artifact port | `trace_payloads` splits `prior_history` from `turn_prompt`, `turn_context`, `turn_output`, `step_outputs` (with `nudge_id`), and `media_uris`; Shape 3 runs over Hive-partitioned BigLake Parquet/Iceberg with risk-tiered async uploads; Gateway Inline-Artifact Port streams `100 MB` attachments upstream and emits inline verdicts. Content is required; zero-payload traffic is outside the core. | UC1–UC11 |
| F03 | Call-level model/tool views & open-twin-safe RFC 8785 hashing | `model_calls` (`chat`, `generate_content`, `embeddings` and any other span that reports tokens, incl. `thought_signature_present`, `upstream_endpoint_location`, `skill_sha256`, `caller_retry_count`, `state_mode`, `runtime_lane`, `tool_definitions_sha256`) and `tool_calls` (`execute_tool`, `retrieval`, `memory_*`) carry execution-time RFC 8785 `args_sha256` and `authz_mode` without scanning restricted buckets; the open twin carries a repeat number per trace instead of the hash (`ADR-038`). | UC1–UC11 |
| F04 | Lifecycle, structured guardrail, SDLC & M:N `events` | Three VPC-SC-isolated copies of `workflow_events` (SoR authoritative, platform controls platform-verified, app unverified), `outcome.linked` -> `outcome_links`, structured `guardrail.intervention` (`control_id`/`layer`/`action`), `state.transition`, `model.approved`, SDLC events (`code.accepted`/`reverted`/`pr.abandoned`), `action.committed`, and OTel `gen_ai.evaluation.result` feedback (`nudge_id`). | UC2–UC11 |
| F05 | Open, multi-day & M:N `outcomes` + `outcome_links` | `outcomes` (restricted, holding `outcome.subject`) and `outcomes_open` (open, zero `outcome.subject`) emit 1 row per `outcome.id` with `min_model_confidence` (default) + `final_model_confidence`, while `outcome_links` exposes M:N correlation for incident/case `CPVO`. | UC2, UC4–UC11 |
| F06 | Fail-closed completeness, structured OBO identity & proxy ownership | `traces` publishes 3-principal identity + structured OBO claims (`auth_method`, `issuer`, `aud`, `delegation_chain_hash`; the `user_id` as seen with its source, verified facts and application identity derived from provenance), caller `trace_owner_app_id` precedence over shared proxies, `has_root_span`, `mutating_tool_call_count`, `workspace_edit_count`, and `sampling_probability` (`NULL` fails closed). | UC1–UC11 |
| F07 | Two-Speed SCD-2 reference tables (`app_registry`, `tools`, `skill_registry`) | Mandatory SCD-2 on `RL-1`–`RL-3` (`app_registry` with `risk_tier`/`barrier_domain`, `tools` splitting `writes_state` vs. `modifies_workspace`, and authoritative `skill_registry` pinning `protected_block_sha256`), synced via Two-Speed CI/CD Dual-Registry Ingestion Port + cloud-registry dark-agent reconciliation. | UC1–UC11 |
| F08 | Multi-project split datasets, split SAs & per-BU RAPs | Physically separate `silver_open` and `silver_restricted` datasets across multi-project landing zones, split `enterprise` compaction SAs (open SA vs. restricted SA), per-BU/tenant Row Access Policies on `silver_restricted`, and BigLake Connections with zero restricted-bucket scans from `*_open`. | UC1–UC11 |
| F09 | Bounded `light`/`enterprise` SQL & `< 5s` Local/CI Runner Port | Single BigQuery SQL contract deployed as table functions of an explicit time window (`light`), a design-only high-volume profile (`enterprise`), or offline `< 5s` Local/CI Eval Runner Port (`CI_ONLY`). | UC1–UC11 |

## 2. Gold — Evaluation Add-On ([gold.md](gold.md#1-evaluation-add-on))

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F10 | Self-only, structured OBO & cross-tenant access audit | Deterministic in-cloud SQL checking `traces.user_id`/`tenant_id` against `tool_calls.arguments` (`parameter_matched`) or structured OBO claims (`token_enforced`), failing closed on unattested `RL-1`–`RL-3` calls and missing arguments. | UC1, UC2, UC9, UC10 |
| F11 | Pre-ticket relevance, sender auth & rejection check | Deterministic SQL verifying inbound `DKIM`/`DMARC` sender auth (where configured), `outcome.rejected` without `creates_ticket`, plus external reply, paired with sampled false-rejection judging. | UC2 |
| F12 | Objective runbook-missing human escalation check | SQL comparing runbook `tool_calls` results (empty vs. matched) against `outcome.escalated` in `events`. | UC2 |
| F13 | Code sandbox safety, workspace split & SDLC verification | Audits `executes_code` / `sandbox.status`, keeps `tools.modifies_workspace` out of SoR HITL SoD gates, and verifies authoritative CI/test/SAST events (`ci.verified`, `sast.blocked`, `code.reverted`, `pr.abandoned`). | UC2, UC8 |
| F14 | Turn-delta PII/secrets & inline DLP audit | Scans `1x` turn deltas when payloads are stored, and audits structured `guardrail.intervention` (`control_id`/`layer`/`action`) verdicts; unreadable payloads never pass. | UC3, UC6, UC9–UC11 |
| F15 | Schema, citations, disclaimers, fiduciary & MNPI barrier checks | Validates JSON schemas, disclaimers, URL/corpus-digest citations, MNPI `barrier_domain` isolation, silent empty-RAG (`acl_dropped_count == retrieved_count`), `Guide`/`Answer` `state.transition`, numeric-threshold / prohibited-entity rules, and authoritative `skill_registry` `protected_block_sha256` pins. | UC1–UC6, UC9, UC10 |
| F16 | Fail-closed HITL write gate, reverse commit check & GCS Object-Lock WORM | Fails closed on unregistered tools; verifies `outcome.approved` (`action_id`, RFC 8785 hash, TTL, Maker-Checker SoD + notional quorums, single-use `event_id`, `model.approved`), runs reverse `action.committed -> tool_calls` orphan-commit checks, and exports PII-free **GCS Retention-Lock + CMEK** WORM snapshots with per-subject RTBF crypto-shredding. | UC5, UC9, UC10 |
| F17 | Idempotency & duplicate-mutation prevention | SQL cross-check across `tool_calls` (`writes_state`, `idempotency_key`, `args_sha256`) and authoritative `action.committed` (`committed_args_sha256`) across retries and resumes. | UC5 |
| F18 | Stateful session reconstruction, deterministic judges & guardrail FP/FN | Reconstructs bounded `previous_interaction_id` history + memory state (failing closed `MISSING_SESSION_CONTEXT`), masks direct PII, pins `temperature = 0` / `seed` / snapshot ID / prompt hash / independent judge family / `zdr_allowed`, runs routine tail-oversampled judging + separate guardrail FP/FN stratified judging (`ATIF-v1.8` / `AI.EVALUATE_AGENT` preview aligned). | UC1–UC6, UC9, UC10 |
| F19 | Stratified `SR 11-7` calibration, binary cut-point & expanded expiry | Stratum-specific $Se_h/Sp_h$ (and per-cloud $Se/Sp$) Rogan-Gladen + design-based joint CIs + $[0,1]$ clamping + `SME-vs-SME` & `judge-vs-SME` `AC2 >= 0.80` paired with binary pass/fail cut-point agreement, `RL-1` (`>= 100`) floors, SME tie-break, and expiry on TTL or judge/prompt/rubric/model/agent/skill/system-instructions/tool-defs/corpus changes. | UC1–UC6, UC9, UC10 |
| F20 | Paired regression, live A/B migrations, delayed truth & `< 5s` Local/CI gate | Pairs on `case_id` (offline/CI) and `shadow_of_trace_id`/`experiment_id`/`variant_id` (live interactive A/B), enforces `0.0%` negative flips (`UC5`), `< 2%` cohort parity (`UC9`), delayed ground-truth joins (`T+1` conversion, `T+7..30d` post-mortem), cascade total-cost vs. accuracy floor (`UC4`), and `< 5s` `CI_ONLY` local PR/sweep gates (`UC6`, `UC8`). | UC3–UC9 |

## 3. Gold — Monitoring Add-On ([gold.md](gold.md#2-monitoring-add-on))

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F21 | Missing-user audit & `1m` / `24h` SLA tracking | 5-minute scheduled audit (with `< 1m` blocking in the Preventive Request-Path Enforcement Port); ack and net closure SLAs from trace start minus customer wait (`events`). | UC1, UC2 |
| F22 | Latency, TTFT, throttling & cross-cloud skew | Monitors turn/call latency, TTFT, `429` throttling, settled traces, and `remote_fragment_pending` cross-cloud transfer skew watermarks. | UC1–UC3, UC6, UC10, UC11 |
| F23 | Loop, retry-oscillation & dynamic tool-churn budgets | Enforces per-app call caps, repeated-call budgets, edit-test/query retry caps, and `tool_definitions_sha256` per-turn churn alerts. | UC1–UC3, UC7, UC8, UC11 |
| F24 | Bounded session, `thought_signature` & per-nudge feedback diagnostics | Bounded `gen_ai.conversation.id` rollups reading `thought_signature_present` directly from core `model_calls`, prefix-cache cliff alerts, and per-`nudge_id` thumbs/conversion rates with reason codes. | UC3, UC6, UC9, UC11 |

## 4. Gold — Cost Add-On & `capacity` Export ([gold.md](gold.md#3-cost-add-on--capacity-export-contract))

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F25 | Per-call cliff (`input + cache_read`), embeddings, sandbox & reconciled BQ compute | Prices LLM & `embeddings` calls (`>200K` on `input + cache_read`, `FLEX`, 3P), adds container `cpu_sec`/`mem_gb_sec` and warehouse compute reconciled against `INFORMATION_SCHEMA.JOBS` (`db.query.job_id`/`bytes_billed`), across 6 spend buckets scaled by `sampling_probability`. | UC1–UC11 |
| F26 | True `CPVO`, M:N incident allocation, cascade & specialist rollups | Divides total spend (incl. failed/rejected/denied/escalated + `100%` judge + downstream compute) by Gold-verified outcomes, allocating M:N linked incidents via `outcome_links`, sliced by specialist, PDF page band, escalation reason, and candidate model. | UC2, UC4–UC11 |
| F27 | Per-user spend, context/cache efficiency & org rollups | Reports per-user spend, context growth, `1/N` prefix-cache dilution, and vector/memory-store attribution, with optional directory org-unit joins. | UC3, UC6, UC9, UC11 |
| F28 | `capacity` export, 3-pool `FOCUS 1.2` & invoice reconciliation | Exports versioned hourly + `burst_metrics_1m` + `RL-1`..`RL-5` payloads to `capacity` (`ADR-001`); computes 3-pool spillover (PT / Priority / Standard), active-hours idle allocation, pro-rata `FIXED_TERM`/`AUTO_RENEW` `order_id` splits inside merged FCFS `runtime_pool_id`, and Cloud Billing invoice reconciliation. | UC2, UC4–UC11 |

## 5. Cross-Cutting Rules & Extensions

| # | Feature | How It Is Addressed | Use Cases |
|---|---|---|---|
| F29 | Completed turns, verified outcomes & kept results | Per-add-on pass rules, daily stored results with frozen SCD-2 reference snapshots, fail-closed `sampling_probability = NULL`, and rootless + reverse `action.committed` safety alerts. | UC1–UC11 |
| F30 | Multi-region Hub default, Location-Alignment & Multi-Cloud Ports ([clouds.md](clouds.md)) | Default `US`/`EU` multi-region Hub with strict per-domain location/CMEK alignment; non-GCP (S3/Iceberg Omni) and sovereign single-region GCP spokes sync via private Hub Transfer Port (`remote_fragment_pending`), cross-cloud per-BU KMS-HMAC (`hmac_key_version`), non-GCP VPC In-Cloud Judge Port, and mTLS On-Prem Collector relay. | UC6, UC9, UC10, UC11 |
| F31 | Central Gateway & Governance Extension ([capacity.md](capacity.md)) | `1:N` `requests` joined via `x-client-request-id` + `(trace_id, span_id)`, per-attempt pricing, honest 3P `inferred_ambiguous` tiering, `burst_metrics_1m` schema, Option A/B `state_mode` neutrality, extended `allowed_models` (`risk_tier`/`app_id`/`invokable_status`/EOL/sanitized LOB export), and `embeddings` pools. | UC1–UC11 |
| F32 | Concrete slot & PT backpressure circuit breaker | Isolated BQ reservations with `max_bytes_billed`, `INFORMATION_SCHEMA.JOBS` utilization checks shedding non-critical judging and capping tail-sampling fan-out during `5–10x` outages, plus gateway daytime-PT isolation for `eval`/`optimize`. | UC6–UC11 |
| F33 | Optimizer Gold Export Port & `skill_registry` | Versioned Optimizer Gold Export (`eval_run_id`, `candidate_id`, `optimizer_mode`, `train`/`holdout` `case_id` disjointness, `nudge_id`, `CI_ONLY` Pareto score, `min_cache_tokens` floor guard) anchored on authoritative SCD-2 `skill_registry`. | UC6 |
| F34 | Preventive Request-Path Enforcement Port boundary | Explicit **Ports, Adapters and Contracts** boundary keeping `< 1m` identity blocking, inline RFC 8785/SoD/TTL, `Model Armor`/DLP blocking, `allowed_models` rejection, `1s/10s` `429` shedding, and `optimize` PT blocking in Gateways/Proxies while BQ Eval audits attested receipts. | UC1, UC2, UC5, UC6, UC9–UC11 |

## 6. Covered in Part by Decision (Preventive Port Boundary)

| Requirement | What BQ Eval delivers (Detective `>= 5m` + Offline CI) | What belongs to the Preventive Request-Path Port / Gateway / App |
|---|---|---|
| `UC1` `< 1m` missing-`user.id` guard & `attempts=1` spike cap | 5-minute audit + `caller_retry_count` storm alerts | Inline gateway/app admission rejection & `attempts=1` SDK config |
| `UC2` `1m` ack SLA & inline sandbox/DLP containment | Net SLA calculation from trace start + detective sandbox/DLP audit | Starting trace on mail arrival; sandbox kernel/egress & inline DLP blocking |
| `UC5` Live write authorization blocking | Fail-closed HITL + reverse `action.committed` audit + GCS WORM snapshots | Inline RFC 8785/SoD/TTL enforcement at the tool/gateway proxy |
| `UC6` / `UC8` `< 5s` PR merge gates & 400–800-rollout sweeps | `< 5s` Local/CI Eval Runner Port (`CI_ONLY`) + BigQuery holdout/prod audit | Executing sweeps/CI pipelines and gating daytime PT at the gateway |
| `UC11` Live `1s/10s` `429` PT shedding & `100 MB` streaming | `burst_metrics_1m` + 3-pool `FOCUS 1.2` chargeback + `capacity` export | Gateway `~2 ms` admission shedding & pre-signed storage streaming |
| "Never" checks on sampled applications | Fails closed (`NULL`) | Sending every trace (`sampling_mode = 'full'`) |
