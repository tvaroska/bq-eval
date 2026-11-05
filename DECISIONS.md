# Architectural Decisions (`DECISIONS.md`)

## ADR-001: Dedicated Showcase Separation (`bq-eval` vs. `agent-evals`)
- **Context**: Combining a runnable BigQuery evaluation and FinOps software toolkit with deep Financial Services Model Risk Management (`SR 11-7`, `SR 26-2`) regulatory playbooks and 60-paper academic literature surveys in a single repository created an onboarding cliff for product and platform engineers.
- **Decision**: Split the concerns cleanly into two focused repositories:
  1. **`bq-eval` (`tvaroska/bq-eval`)**: The standalone, runnable **BigQuery-Native Enterprise Agent Evaluation, Observability & Cost Management Showcase** with a 60-second `bq-eval demo` CLI, complete 7-table BigQuery DDL & 4 views, multi-cloud USD attribution, and `FOCUS 1.2` capacity bridge.
  2. **`agent-evals`**: Remains the **Regulated Banking & Financial Services AI Evaluation Standard, MRM Playbook Compendium & Cross-Workspace Compliance Authority**.

## ADR-002: App Hub Registration + BigQuery Single Store of Record (Zero Workload SDK Coupling)
- **Context**: Requiring product teams to embed custom evaluation/billing SDKs inside every agent or operating secondary operational KV databases (Redis, Postgres, Cloud Bigtable) slows adoption and fragments enterprise telemetry.
- **Decision**:
  1. **Register Once in App Hub (`app_registry`)**: Teams register each AI application once in Google Cloud **App Hub** (`business_unit`, `cost_center`, `criticality_tier`, `environment`, `latency_sla_ms`, `default_rubric_id`, `sample_rate_pct`).
  2. **Zero Custom Agent Code**: Workload agents emit standard OpenTelemetry `gen_ai.*` spans (`otel_spans`) with full prompts and retrieved context archived in Cloud Storage Object Tables (`trace_payloads`) at `$0.02/GiB/month` (`96%` cheaper than `$0.50/GiB` log ingestion).
  3. **BigQuery Single Store (`sql/ddl.sql` & `sql/views.sql`)**: Operate trace waterfalls (`v_trace_waterfall`), `$0` `Rule 0` health and burndown rollups (`v_trace_sql_evaluation`), 1-step golden dataset promotion (`golden_cases`), scheduled/on-demand Gemini judging (`AI.GENERATE_TABLE` $\to$ `eval_results`), blind SME calibration (`sme_labels`), and multi-cloud USD pricing (`model_pricing_catalog`) entirely inside BigQuery.

## ADR-003: Hybrid & Multi-Cloud Coverage via Keyless WIF Collector & `COALESCE` Resolution
- **Context**: In-GCP workloads (`Agent Engine`, `Cloud Run`, `GKE`) receive automatic `gcp.apphub.application.id` resource attributes via `telemetry.googleapis.com`, whereas external workloads (`AWS EKS`, `Azure AKS`, on-prem Kubernetes, local dev) run outside GCP metadata servers and invoke multi-provider models.
- **Decision**:
  1. External agents emit plain local OTLP (`:4317`) to a cluster OpenTelemetry Collector authenticating keylessly via **Workload Identity Federation** (`sts.googleapis.com`).
  2. Both BigQuery SQL (`sql/views.sql`) and Python (`bq_eval/sql_eval.py`) resolve application identity via `COALESCE(gcp.apphub.application.id, gcp.agent.id, NULLIF(service_name, 'agent-service'), 'unregistered_app')` and join `model_pricing_catalog` for multi-cloud USD attribution.

## ADR-004: Fail-Closed Rogan-Gladen Calibration, Conditional `Rule 0` Conditioning & Dual IRR
- **Context**: Because `100%` of `Rule 0` failures and only `5–10%` of `Rule 0` passes are routed to the LLM judge, pooling failed and passed traces in the judge pass rate $\hat{p}_{\text{judge}}$ overweights deterministic errors by `10x–20x`. Furthermore, defaulting `judged_samples == 0` to `calibrated_pass_rate = 1.0` makes unjudged apps appear `100%` accurate.
- **Decision**:
  1. Estimate $\hat{\theta}_{\text{RG}} = \hat{P}(\text{Semantic Pass} \mid \text{Rule 0 Pass})$ strictly over `rule0_pass = TRUE` judged traces while billing `100%` of judge runs (`autorater_judge_usd`) to the application's evaluation overhead.
  2. Evaluate `calibrated_pass_rate` and `cost_per_verified_outcome_usd` fail-closed to `None` / SQL `NULL` whenever `judged_samples == 0` or Youden's $J = \widehat{\text{TPR}} + \widehat{\text{TNR}} - 1 \le 10^{-6}$.
  3. Enforce `AND judge_pass IS NOT NULL` on paired SME calibration rows and report delta-method 95% confidence intervals alongside both **Krippendorff's $\alpha$** and prevalence-robust **Gwet's AC2**.

## ADR-005: Portable, Customer-Agnostic Repository Hygiene
- **Context**: Showcase repositories must be safe to clone, run in isolated Cloud Build containers, and share externally without leaking customer identifiers, internal shortlinks, or workstation paths.
- **Decision**: Enforce automated AST and text hygiene gates (`tests/test_repo_hygiene.py`, `tests/test_isolation_guard.py`) blocking internal codenames, internal shortlinks, hardcoded workstation home directory paths, local file URI schemes, `.github/workflows/`, and sibling `../` imports.

## ADR-006: Outcome-Adjusted FinOps, `>200K` Burndown Cliffs & `NFR = 0.0%` Pareto Right-Sizing
- **Context**: Raw token dashboards hide whether an agent succeeded, miss the `>200K` long-context PT burndown cliff (`2.0x` input / `7.5x` output), ignore `1/N` round-robin prefix-cache dilution, and cannot prove whether downgrading a model breaks working cases.
- **Decision**: Center FinOps on **USD Cost per Verified Outcome** (`bq_eval/finops.py`), separating `production_inference`, `conflict_escalation`, `agent_gym_eval`, and `autorater_judge` spend, evaluating `>200K` burndown cliffs at the span level before `GROUP BY trace_id`, quantifying **Eval FinOps ROI** (`90–95%` judge savings), and gating model right-sizing on `Negative Flip Rate (NFR) == 0.0%`.

## ADR-007: Capacity & `FOCUS 1.2` Two-Pool Chargeback Actuation Bridge
- **Context**: `8x5` business-hours applications active `40h/week` (`23.8%` duty cycle) are penalized `3x` when charged for `24x7` idle Provisioned Throughput commitments, while overnight idle PT GSUs sit unused.
- **Decision**: Export both 1-minute (`minute_burndown_tpm`) and 1-hour (`hourly_burndown_tokens`) regional/BU series (`v_capacity_hourly_burndown_export` & `export_capacity_hourly_series()`), compute **FinOps Foundation `FOCUS 1.2` Two-Pool Chargeback** (`Active Consumed` billed to BUs vs. `Unallocated Idle` billed to central platform), and schedule batch `AI.GENERATE_TABLE` evaluations into overnight idle PT valleys at `$0.00` marginal commitment cost (`plan_offpeak_eval_waterfill()`).
