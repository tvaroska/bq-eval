# Architectural Decisions (`DECISIONS.md`)

## ADR-001: Dedicated Showcase Separation (`bq-eval` vs. `agent-evals`)
- **Context**: Combining a runnable BigQuery evaluation and FinOps software toolkit with deep Financial Services Model Risk Management (`SR 11-7`, `SR 26-2`) regulatory playbooks and 60-paper academic literature surveys in a single repository created an onboarding cliff for product and platform engineers.
- **Decision**: Split the concerns cleanly into two focused repositories:
  1. **`bq-eval` (`tvaroska/bq-eval`)**: The standalone, runnable **BigQuery-Native Enterprise Agent Evaluation, Observability & Cost Management Showcase** with a progressive `Step 1 -> Step 2 -> Step 2.5 (Agentic Skill) -> Step 3` SQL pack, a 60-second `bq-eval demo` CLI, multi-cloud USD attribution, and `FOCUS 1.2` capacity bridge.
  2. **`agent-evals`**: Remains the **Regulated Banking & Financial Services AI Evaluation Standard, MRM Playbook Compendium & Cross-Workspace Compliance Authority**.

## ADR-002: BigQuery as Pure Execution Engine & Progressive Step 1 → Step 2 → Step 2.5 → Step 3 Onboarding
- **Context**: Requiring product teams to provision 7 tables, register in App Hub, or set up human labeling panels on Day 1 creates a steep setup curve. Meanwhile, both live production traffic and offline/staging test runs already emit OpenTelemetry traces.
- **Decision**:
  1. **Source Is Always Traces (`otel_spans` + `trace_payloads`)**: Workload agents emit standard OpenTelemetry `gen_ai.*` spans (`0` custom evaluation or FinOps SDK code).
  2. **Step 1 (`0` New Tables, No Skill Needed — `sql/step1_trace_views.sql`)**: Predefined static SQL views (`v_step1_trace_waterfall`, `v_step1_trace_eval`, `v_step1_cost_per_outcome`) run directly over existing traces (`gcp.apphub.application.id` used when present, automatic fallback when absent) with built-in default Gemini rates (`<=200K` and `>200K` cliffs) to deliver waterfalls, `$0` SQL checks, and **Day-1 Cost per Application, Business Unit & Outcome**.
  3. **Step 2 (`1` Rubric Table / CSV, `0` Result Tables, No Skill Needed — `sql/step2_rubrics.csv` + `sql/step2_stateless_eval.sql`)**: Add a single `rubrics` table (mounted from a Cloud Storage CSV or created as a native BigQuery table seeded with 4 example 4-point rubrics) and run static `AI.GENERATE_TABLE` evaluations on **100% of traces** or a **10% sample** without saving results.
  4. **Step 2.5 (Agentic Skill — `skills/bq-eval/SKILL.md`)**: Introduce the Agentic Skill when teams want plain-English authoring of new 4-point rubrics, custom trace slices (**5% sample**, **only the last 1 hour of traces**, error/SLA tail), or custom domain Step-1+ SQL checks.
  5. **Step 3 (`2` Tables: `rubrics` + `eval_results` — `sql/step3_saved_results.sql`) & Open Extensions (`sql/ddl.sql`)**: Persist verdicts into `eval_results` (`eval_run_id`) for `0%` negative-flip regression gates and longitudinal trends, while keeping `~100` blind human spot-checks (`sme_labels`), `app_registry`, `model_pricing_catalog`, and `FOCUS 1.2` capacity exports open as optional enterprise extensions.

## ADR-003: Hybrid & Multi-Cloud Coverage via Keyless WIF Collector & `COALESCE` Resolution
- **Context**: In-GCP workloads (`Agent Engine`, `Cloud Run`, `GKE`) receive automatic `gcp.apphub.application.id` resource attributes via `telemetry.googleapis.com`, whereas external workloads (`AWS EKS`, `Azure AKS`, on-prem Kubernetes, local dev) run outside GCP metadata servers and invoke multi-provider models.
- **Decision**:
  1. External agents emit plain local OTLP (`:4317`) to a cluster OpenTelemetry Collector authenticating keylessly via **Workload Identity Federation** (`sts.googleapis.com`).
  2. Both BigQuery SQL (`sql/step1_trace_views.sql`, `sql/views.sql`) and Python (`bq_eval/otel.py`) resolve application identity via `COALESCE(gcp_apphub_application_id, JSON_VALUE(attributes, '$."gcp.apphub.application.id"'), JSON_VALUE(attributes, '$."apphub.application.id"'), 'unregistered_app')`, so any workload without App Hub registration surfaces directly under `'unregistered_app'`.

## ADR-004: Fail-Closed Rogan-Gladen Calibration, Conditional `Rule 0` Conditioning & Dual IRR
- **Context**: Because `100%` of `Rule 0` failures and only `5–10%` of `Rule 0` passes are routed to the LLM judge, pooling failed and passed traces in the judge pass rate $\hat{p}_{\text{judge}}$ overweights deterministic errors by `10x–20x`. Furthermore, defaulting `judged_samples == 0` to `calibrated_pass_rate = 1.0` makes unjudged apps appear `100%` accurate.
- **Decision**:
  1. Estimate $\hat{\theta}_{\text{RG}} = \hat{P}(\text{Semantic Pass} \mid \text{Rule 0 Pass})$ strictly over `rule0_pass = TRUE` judged traces while billing `100%` of judge runs (`autorater_judge_usd`) to the application's evaluation overhead.
  2. Evaluate `calibrated_pass_rate` and `cost_per_verified_outcome_usd` fail-closed to `None` / SQL `NULL` whenever `judged_samples == 0` or Youden's $J = \widehat{\text{TPR}} + \widehat{\text{TNR}} - 1 \le 10^{-6}$ (`rogan_gladen_calibrate()`).
  3. Enforce `AND judge_pass IS NOT NULL` on paired SME calibration rows and report delta-method 95% confidence intervals alongside prevalence-robust **Gwet's AC2** (`compute_gwet_ac2()`).

## ADR-005: Portable, Customer-Agnostic Repository Hygiene
- **Context**: Showcase repositories must be safe to clone, run in isolated Cloud Build containers, and share externally without leaking customer identifiers, internal shortlinks, or workstation paths.
- **Decision**: Enforce automated AST and text hygiene gates (`tests/test_repo_hygiene.py`, `tests/test_isolation_guard.py`) blocking internal codenames, internal shortlinks, hardcoded workstation home directory paths, local file URI schemes, `.github/workflows/`, and sibling `../` imports.

## ADR-006: Outcome-Adjusted FinOps, `>200K` Burndown Cliffs & `NFR = 0.0%` Pareto Right-Sizing
- **Context**: Raw token dashboards hide whether an agent succeeded, miss the `>200K` long-context PT burndown cliff (`2.0x` input / `7.5x` output), ignore `1/N` round-robin prefix-cache dilution, and cannot prove whether downgrading a model breaks working cases.
- **Decision**: Center FinOps on **USD Cost per Verified Outcome** (`bq_eval/finops.py`), separating `production_inference`, `conflict_escalation`, `agent_gym_eval`, and `autorater_judge` spend, evaluating `>200K` burndown cliffs (`compute_weighted_burndown_tokens()`) at the span level before `GROUP BY trace_id`, quantifying **Eval FinOps ROI** (`compute_eval_finops_roi()`, `90–95%` judge savings), and gating model right-sizing on `Non-Fabrication / Negative Flip Rate (NFR) == 0.0%` (`compute_pareto_frontier()`).

## ADR-007: Capacity & `FOCUS 1.2` Two-Pool Chargeback Actuation Bridge
- **Context**: `8x5` business-hours applications active `40h/week` (`23.8%` duty cycle) are penalized `3x` when charged for `24x7` idle Provisioned Throughput commitments, while overnight idle PT GSUs sit unused.
- **Decision**: Export hourly regional/BU burndown (`v_capacity_hourly_burndown_export`), compute **FinOps Foundation `FOCUS 1.2` Two-Pool Chargeback** (`allocate_focus_two_pool_chargeback()`: `Utilized PT` billed to BUs vs. `Unutilized Idle` billed to central platform), and schedule batch `AI.GENERATE_TABLE` evaluations into overnight idle PT valleys at `$0.00` marginal commitment cost (`plan_off_peak_eval_schedule()`).

## ADR-008: Dual-Path Capture (`Light Capture BQAA` + `Full Capture OTel`) -> Single Evaluation & FinOps Engine
- **Context**: Individual ADK and LangGraph developers adopt 3-line **BigQuery Agent Analytics (`BQAA`)** (`BigQueryAgentAnalyticsPlugin` / `BigQueryCallbackHandler` -> `agent_events` via Storage Write API) to avoid multi-resource OpenTelemetry setup and capture candidate tool schemas for `AI.EVALUATE_AGENT`, whereas multi-service and multi-cloud enterprise fleets require server-stamped App Hub attribution and W3C `baggage` propagation via native OpenTelemetry (`telemetry.googleapis.com` -> `_AllSpans`).
- **Decision**:
  1. Keep repository slug `bq-eval` and retitle to **BQ Eval & FinOps**, positioning it as the unified evaluation, calibration, and FinOps execution engine over **Light Capture (`BQAA`)**, **Full Capture (`OTel` `_AllSpans`)**, or **Hybrid `UNION ALL`** (`sql/bqaa_adapter_views.sql` + `bq_eval/bqaa.py`).
  2. Enforce `ADR-003` strict `'unregistered_app'` fallback on `v_bqaa_otel_spans` (`gcp_apphub_application_id` and `outcome_owner` resolve strictly from `custom_metadata` App Hub keys or `_AllSpans` join, never collapsing into self-reported `adk.app_name` or `agent`), deduplicate Storage Write API retries via `QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ...) = 1`, and support both **Engine A (`AI.GENERATE_TABLE`)** and **Engine B (`AI.EVALUATE_AGENT` via `v_bqaa_eval_turn_filtered`)** behind Step-1 `$0` SQL pre-filtering.
