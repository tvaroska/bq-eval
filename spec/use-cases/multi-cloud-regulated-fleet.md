---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 10: Multi-Cloud Regulated Fleet Governance

> Enterprise-wide regulatory governance across hundreds of agent applications spanning AWS, GCP, and on-premises environments across risk tiers (`RL-1`..`RL-5`), multiple execution lanes (`managed_agents_api`, `agent_runtime`, `agent_vms`, `aws_agentcore`, `on_prem`), and regulated surveillance/AML/credit domains — anchored on a `US`/`EU` multi-region Hub with location-aligned spokes, Two-Speed CI/CD Dual-Registry dark-agent reconciliation, MNPI information barriers, scoped `allowed_models` EOL governance, and cross-cloud calibration without text egress.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Maintain a live, regulator-examinable inventory and governance plane across all enterprise agents (including high-risk trade surveillance, AML investigation, and credit decisioning workloads) spanning multiple clouds, regions, and business units without moving raw text or identities across perimeters. |
| **Topology & lanes** | Multi-cloud / multi-region fleet running across distinct `bq_eval.runtime_lane` targets (`managed_agents_api` Pre-GA synthetic-only, `agent_runtime` GA, `agent_vms` Preview, `aws_agentcore`, `on_prem`), with a **multi-region (`US` / `EU`) Hub** default (or single-region CCI-aligned Hub) and tiered spoke routing via **Borderless Lakehouse** `*_open` Iceberg V2 federation, the staged **Hub Transfer Port**, Direct **Cross-Cloud Connections** (`RL-4`–`RL-5` only), and the **In-Cloud Judge Port** (`ADR-031`, `ADR-046`). |
| **Registry & inventory** | SCD-2 `app_registry`, `tools`, `skill_registry`, and `allowed_models` maintained via the **Two-Speed CI/CD Dual-Registry Ingestion Port** (fast-lane GitOps sync for `RL-3`–`RL-5`, dual-signed `CODEOWNERS`/MRM approval for `RL-1`/`RL-2` and flag downgrades) and continuously reconciled against cloud agent registries (`AWS`, `GCP`) to detect uninstrumented dark agents. |
| **Custody & compliance** | Multi-project landing zones under locked-down VPC-SC + location-matched CMEK, split open/restricted compaction SAs, per-BU KMS-HMAC (`hmac_key_version`), MNPI `barrier_domain` enforcement, and `SR 11-7` stratified dual-SME calibration (`RL-1` `>= 100` floor, per-cloud $Se/Sp$). |

## Telemetry & Data Expectations

- **Multi-cloud Hub federation & staging with skew watermarks**: Strict-residency (`RL-1`–`RL-3`) non-GCP spokes compute Silver in-cloud (Spark/Trino/Snowflake passing `ADR-041` JCS vectors, since BigQuery Omni lacks UDFs) and expose text-free `*_open` Iceberg V2 tables federated zero-copy via **Borderless Lakehouse** over regional Cross-Cloud Interconnect — or stage text-free open rows (`model_calls`, `outcomes_open`, `outcome_links`, `*_open`, `requests`, text-free judge verdicts) alongside sovereign single-region GCP spokes via the **Hub Transfer Port** into a `US`/`EU` Hub carrying `(spoke_id, transfer_watermark_utc, batch_id)` and `remote_fragment_pending` skew flags (`>= 300s` catalog refresh / transfer lag). Permissive (`RL-4`–`RL-5`) workloads may query compacted S3/ADLS Bronze directly via **BigQuery Cross-Cloud Connections**.
- **On-premises relay**: On-prem services emit OTel spans through an mTLS, egress-filtered `On-Prem -> Transit Cloud -> Hub` Collector relay stripping restricted payloads per `payload_profile`.
- **Governance & barrier metadata**: SCD-2 `app_registry` carries `risk_tier` (`RL-1`..`RL-5`), `payload_profile`, `barrier_domain`, `runtime_lane`, `mrm_model_id`, and `validation_expiry_date`; extended `allowed_models` carries `(provider, model, region, environment, business_unit, risk_tier, app_id)`, `invokable_status`, `zdr_allowed`, `lifecycle_tier` (`short_term_ga | long_term_ga | preview`), `eol_date`, `deprecation_notice_days`, and `migration_target_model`.
- **Non-LLM sibling telemetry**: Time-series and non-LLM GPU/TPU inference workloads report through a dedicated sibling metric table so LLM token semantics in `model_calls` stay clean.

## Required Checks, Alerts & Views

1. **Active-agent & dark-agent regulatory inventory**
   - Reconcile `app_registry` against cloud agent registries and live Silver emitters from the Hub without text egress — flagging both unregistered live agents and registered dark agents emitting zero telemetry, plus any production traffic hitting a Pre-GA `runtime_lane`.
2. **Scoped `allowed_models`, EOL forecasting & sanitized LOB lifecycle exports**
   - Flag unapproved model/region/BU/risk-tier usage, block `short_term_ga` models on `RL-1`/`RL-2` production workloads, verify `zdr_allowed = true` on Gold judges for ZDR workloads, and publish sanitized LOB lifecycle exports (`model`, `lifecycle_tier`, `eol_date`, `deprecation_notice_days`, `migration_target_model` — zero raw project/order IDs).
3. **MNPI information-barrier & domain-specific regulated checks**
   - Audit tool/retrieval access against `app_registry.barrier_domain` (zero cross-wall leakage), verify authoritative `model.approved` MRM releases, and support Trade Surveillance / AML (`SAR` completeness, negative-class miss-rate backtesting) and Credit Decisioning (adverse-action reason codes, fairness/explainability) deterministic & calibrated rubrics.
4. **Cross-cloud `SR 11-7` calibration & WORM governance**
   - Calibrate per-cloud judge $Se/Sp$ separately whenever non-GCP In-Cloud Judge Ports use different judge model families, enforce expanded `CALIBRATED` expiry triggers, and archive PII-free GCS Object-Lock WORM snapshots.
