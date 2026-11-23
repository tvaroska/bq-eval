"""Frozen, NaN/Inf-rejecting result, calibration, FinOps, loadgen, and receipt contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.contract.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.contract.types import FiniteFloat, NonNegFloat, PosFloat, UnitFloat


class ConfusionMatrix(BaseModel):
    """Binary autorater vs. human SME confusion matrix on Rule-0-passing traces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tp: int = Field(..., ge=0)
    fn: int = Field(..., ge=0)
    tn: int = Field(..., ge=0)
    fp: int = Field(..., ge=0)

    @property
    def total(self) -> int:
        return self.tp + self.fn + self.tn + self.fp

    @property
    def sensitivity_tpr(self) -> float:
        denom = self.tp + self.fn
        if denom == 0:
            raise ValueError("Cannot compute sensitivity (TPR) with zero positive human labels.")
        return self.tp / denom

    @property
    def specificity_tnr(self) -> float:
        denom = self.tn + self.fp
        if denom == 0:
            raise ValueError("Cannot compute specificity (TNR) with zero negative human labels.")
        return self.tn / denom

    @property
    def youden_j(self) -> float:
        return self.sensitivity_tpr + self.specificity_tnr - 1.0


class RoganGladenResult(BaseModel):
    """Bias-corrected true pass rate and 95% confidence interval (EVAL-P04)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_pass_rate: UnitFloat
    calibrated_pass_rate: UnitFloat
    ci_95_lower: UnitFloat
    ci_95_upper: UnitFloat
    sensitivity_tpr: UnitFloat
    specificity_tnr: UnitFloat
    youden_j: FiniteFloat
    sample_size: int = Field(..., gt=0)


class CalibrationResult(BaseModel):
    """Combined IRR (Gwet's AC2) + Rogan-Gladen calibration summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gwet_ac2: FiniteFloat
    passes_irr_gate: bool
    confusion_matrix: ConfusionMatrix
    rogan_gladen: RoganGladenResult


class SQLTraceEvalSummary(BaseModel):
    """Per-application Tier 1-3 evaluation and 4-bucket FinOps summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    rubric_id: str
    total_traces: int = Field(..., ge=0)
    production_traces: int = Field(..., ge=0)
    rule0_pass_rate: UnitFloat
    tool_success_rate: UnitFloat
    p95_latency_ms: NonNegFloat
    avg_cache_hit_ratio: UnitFloat
    total_weighted_burndown_tokens: NonNegFloat
    judged_samples: int = Field(..., ge=0)
    calibration_status: Literal["CALIBRATED", "UNCALIBRATED", "UNJUDGED"]
    raw_judge_pass_rate: UnitFloat | None
    calibrated_pass_rate: UnitFloat | None
    calibrated_ci_95_lower: UnitFloat | None
    calibrated_ci_95_upper: UnitFloat | None
    gwet_ac2: FiniteFloat | None = None
    passes_irr_gate: bool | None = None
    production_inference_usd: NonNegFloat
    conflict_escalation_usd: NonNegFloat
    agent_gym_eval_usd: NonNegFloat
    autorater_judge_usd: NonNegFloat
    optimize_usd: NonNegFloat = 0.0
    bq_query_cost_usd: NonNegFloat = 0.0
    unclassified_usd: NonNegFloat = 0.0
    total_spend_usd: NonNegFloat
    cost_per_verified_outcome_usd: NonNegFloat | None
    outcome_total_count: int = Field(default=0, ge=0)
    outcome_verified_count: int = Field(default=0, ge=0)
    outcome_cost_per_verified_ai_usd: NonNegFloat | None = None
    outcome_cost_per_verified_usd: NonNegFloat | None = None


class EvalFinOpsROI(BaseModel):
    """Quantified USD savings of the 3-Tier SQL + Stratified Judge Funnel vs. 100% LLM judging."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_production_traces: int = Field(..., gt=0)
    tier1_sql_evaluated_traces: int = Field(..., gt=0)
    tier2_canary_traces: int = Field(..., ge=0)
    tier2_error_tail_traces: int = Field(..., ge=0)
    tier2_total_judged_traces: int = Field(..., ge=0)
    baseline_100pct_judge_usd: NonNegFloat
    stratified_3tier_judge_usd: NonNegFloat
    net_savings_usd: NonNegFloat
    savings_pct: Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]


class PrefixCacheDilutionReport(BaseModel):
    """Diagnostics for 1/N prompt-prefix cache dilution and recoverable window USD savings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    distinct_prompt_variants: int = Field(..., ge=1)
    theoretical_dilution_ceiling: UnitFloat
    observed_cache_hit_ratio: UnitFloat
    target_cache_hit_ratio: UnitFloat
    dilution_detected: bool
    recoverable_weighted_burndown_tokens: NonNegFloat
    recoverable_window_usd: NonNegFloat
    recommendation: str


class ModelOption(BaseModel):
    """Candidate model/routing configuration for Cost-vs-Quality Pareto analysis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: str
    model_name: str
    rule0_pass_rate: UnitFloat
    fabrication_rate: UnitFloat
    calibrated_pass_rate: UnitFloat
    cost_per_verified_outcome_usd: NonNegFloat
    p95_latency_ms: NonNegFloat


class ParetoEvalResult(BaseModel):
    """Result of Zero-Fabrication (`fabrication_rate = 0.0%`) Cost-vs-Quality Pareto selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible_options: tuple[ModelOption, ...]
    disqualified_options: tuple[ModelOption, ...]
    pareto_frontier: tuple[ModelOption, ...]
    recommended_default: ModelOption | None


class FocusChargebackRow(BaseModel):
    """FOCUS 1.2 Two-Pool Chargeback allocation row for a tenant application or platform pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    cost_center: str
    weighted_burndown_tokens: NonNegFloat
    utilization_share: UnitFloat
    utilized_pt_cost_usd: NonNegFloat
    unutilized_idle_capacity_tax_usd: NonNegFloat
    payg_overflow_cost_usd: NonNegFloat
    total_allocated_cost_usd: NonNegFloat


class HourlyBurndownExportRow(BaseModel):
    """Wire contract row for `agent_eval.v_capacity_hourly_burndown_export` (`M7-3` / `BQ-G1`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage_hour: str
    timestamp: str
    apphub_application_id: str
    tenant: str
    model_name: str
    model: str
    finops_traffic_type: str
    serving_traffic_type: str = "STANDARD"
    distinct_traces: int = Field(..., ge=0)
    span_count: int = Field(..., ge=0)
    ok_span_count: int = Field(default=0, ge=0)
    error_span_count: int = Field(default=0, ge=0)
    http_429_count: int = Field(default=0, ge=0)
    spillover_span_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(..., ge=0)
    cached_input_tokens: int = Field(..., ge=0)
    cached_tokens: int = Field(..., ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    uncached_input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    thinking_tokens: int = Field(..., ge=0)
    thoughts_tokens: int = Field(..., ge=0)
    long_context_cliff_spans: int = Field(..., ge=0)
    weighted_burndown_tokens: NonNegFloat
    precomputed_weighted_burndown_tokens: NonNegFloat
    total_cost_usd: NonNegFloat


class OffPeakSchedulePlan(BaseModel):
    """Water-filling schedule for running batch Agent Gym & Judge jobs in off-peak PT valleys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hourly_scheduled_eval_tokens: tuple[NonNegFloat, ...]
    total_batch_eval_tokens_requested: NonNegFloat
    scheduled_in_free_pt_valley_tokens: NonNegFloat
    remaining_payg_spillover_tokens: NonNegFloat
    zero_marginal_cost_coverage_pct: Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
    avoided_payg_cost_usd: NonNegFloat


class GoldenSpanTemplate(BaseModel):
    """Single span template with relative millisecond offset from its trace root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    span_suffix: str
    parent_span_suffix: str | None = None
    span_offset_ms: int = 0
    latency_ms: NonNegFloat
    status_code: str = "OK"
    span_kind: GenAISpanKind = GenAISpanKind.INVOKE_AGENT
    service_name: str
    gcp_apphub_application_id: str | None = None
    agent_name: str
    model_name: str = "gemini-3.8-flash"
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    thinking_tokens: int = Field(default=0, ge=0)
    tool_name: str | None = None
    tool_call_status: str | None = None
    rule0_pass: bool = True
    finops_traffic_type: FinOpsTrafficType = FinOpsTrafficType.PRODUCTION_INFERENCE
    attributes: dict[str, str]


class GoldenTraceTemplate(BaseModel):
    """Captured multi-span trace template and its associated prompt/context/response payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_idx: int
    base_trace_id: str
    apphub_application_id: str
    user_prompt: str
    retrieved_context: str
    final_response: str
    semantic_rubric_id: str = "groundedness_4pt_v1"
    expected_ordinal_score: int = Field(default=4, ge=1, le=4)
    spans: tuple[GoldenSpanTemplate, ...]


class Step1CostOutcomeRow(BaseModel):
    """Step 1 Cost per Application, Business Unit & Outcome summary row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    business_unit: str
    cost_center: str
    framework_runtime: str
    total_traces: int = Field(..., ge=0)
    last_1h_traces: int = Field(..., ge=0)
    step1_passing_traces: int = Field(..., ge=0)
    step1_pass_rate: UnitFloat
    tool_success_rate: UnitFloat
    p95_latency_ms: NonNegFloat
    avg_cache_hit_ratio: UnitFloat
    total_cost_usd: NonNegFloat
    cost_per_trace_usd: NonNegFloat
    step1_cost_per_outcome_usd: NonNegFloat | None


class TimeShiftLoadgenReport(BaseModel):
    """Summary of a time-shifted demo load generation run across Step 1, Step 2, and Step 2.5."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_time: datetime
    total_replicas: int = Field(..., ge=1)
    last_hour_replicas: int = Field(..., ge=0)
    total_traces: int = Field(..., ge=0)
    total_spans: int = Field(..., ge=0)
    last_1h_traces: int = Field(..., ge=0)
    sample_10pct_traces: int = Field(..., ge=0)
    sample_5pct_traces: int = Field(..., ge=0)
    last_1h_and_5pct_traces: int = Field(..., ge=0)
    step1_rows: tuple[Step1CostOutcomeRow, ...]


class LiveKycCohortReceipt(BaseModel):
    """Measured KYC 2-stage funnel, Wilson CI, and Flash-vs-Argon calibration receipt (`m2-live`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_cases: int = Field(..., gt=0)
    structural_step1_rejects: int = Field(..., ge=0)
    po_box_step1_traps_caught: int = Field(..., ge=0)
    ocr_step1_rejects: int = Field(default=0, ge=0)
    step2_semantic_traps_caught: int = Field(..., ge=0)
    step2_semantic_traps_total: int = Field(..., gt=0)
    held_out_traps_caught: int = Field(..., ge=0)
    held_out_traps_total: int = Field(..., ge=0)
    clean_cases_evaluated: int = Field(..., gt=0)
    false_positives: int = Field(..., ge=0)
    trap_recall: UnitFloat
    trap_recall_wilson_ci_95: tuple[UnitFloat, UnitFloat]
    trap_precision: UnitFloat
    argon_calibration_pairs: int = Field(..., gt=0)
    flash_argon_binary_agreed: int = Field(..., ge=0)
    ordinal_pairs: tuple[tuple[int, int], ...]
    confusion_matrix: ConfusionMatrix


class LiveRfpModalityReceipt(BaseModel):
    """Measured Benefits RFP modality & >200K context cliff receipt (`m2-live`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_mode: str
    agent_name: str
    verified_outcomes: int = Field(..., gt=0)
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    ai_cost_per_verified_outcome_usd: NonNegFloat
    total_cost_per_verified_outcome_usd: NonNegFloat


class LiveSkillLineageReceipt(BaseModel):
    """Measured Day-1 vs. Day-2 compiled skill CPVO & human review receipt (`m2-live`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_id: str
    day1_version: str
    day2_version: str
    day1_cpvo_usd: PosFloat
    day2_cpvo_usd: PosFloat
    cpvo_reduction_pct: Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
    day1_human_reviews_per_outcome: NonNegFloat
    day2_human_reviews_per_outcome: NonNegFloat
    day2_in_policy_auto_approved: int = Field(..., ge=0)
    day2_in_policy_total: int = Field(..., ge=0)
    day2_out_of_policy_escalated: int = Field(..., ge=0)
    day2_out_of_policy_total: int = Field(..., ge=0)
    day1_avg_cycle_seconds: PosFloat
    day2_avg_cycle_seconds: PosFloat


class LiveAttributionCohortReceipt(BaseModel):
    """Measured App Hub attribution coverage cohort row (`m2-live`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    total_traces: int = Field(..., ge=0)
    attributed_traces: int = Field(..., ge=0)
    attributed_spend_usd: NonNegFloat
    unattributed_spend_usd: NonNegFloat
    attribution_coverage_pct: Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
    passes_coverage_gate: bool


class LiveFleetReceipt(BaseModel):
    """Sanitized empirical multi-agent fleet telemetry receipt (`m2-live-20261113b`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    run_id: str
    recorded_date_utc: str
    summary_provenance: str = "reconstructed_from_bundle_spans"
    labeled_fleet_cases: int = Field(..., gt=0)
    cumulative_closed_outcomes: int = Field(..., gt=0)
    cumulative_allspans_count: int = Field(..., gt=0)
    cumulative_traces_count: int = Field(..., gt=0)
    allspans_ingestion_lag_p50_ms: NonNegFloat
    allspans_ingestion_lag_p95_ms: NonNegFloat
    kyc_cohort: LiveKycCohortReceipt
    rfp_modalities: tuple[LiveRfpModalityReceipt, ...]
    skill_lineage: LiveSkillLineageReceipt
    attribution_cohorts: tuple[LiveAttributionCohortReceipt, ...]


class ScenarioCorpusBundle(BaseModel):
    """Portable scenario/corpus file contract for `bq-eval demo --scenario-file` (`M7-2`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str
    title: str
    rubric_id: str = "groundedness_4pt_v1"
    spans: tuple[OTelGenAISpanRecord, ...]
    judge_outcomes: dict[str, bool] = Field(default_factory=dict)
    sampling_strata: dict[str, str] = Field(default_factory=dict)
    sme_confusion_by_app: dict[str, ConfusionMatrix] = Field(default_factory=dict)
    sme_ordinal_pairs_by_app: dict[str, tuple[tuple[int, int], ...]] = Field(default_factory=dict)
    distinct_prompt_variants: int = Field(default=4, ge=1)
    dilution_target_app_id: str | None = None
    reserved_pt_capacity_tokens: PosFloat = 100_000_000.0
    reserved_pt_fixed_cost_usd: NonNegFloat = 120.0
    app_cost_centers: dict[str, str] = Field(default_factory=dict)
    live_receipt: LiveFleetReceipt | None = None


class ShowcaseRunArtifacts(BaseModel):
    """Complete deterministic output bundle from a `bq-eval demo` or `serve` showcase run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str = "m2-live"
    summaries: dict[str, SQLTraceEvalSummary]
    gwet_ac2: FiniteFloat | None
    eval_roi: EvalFinOpsROI
    cache_dilution: PrefixCacheDilutionReport
    pareto: ParetoEvalResult
    focus_chargeback: tuple[FocusChargebackRow, ...]
    off_peak_plan: OffPeakSchedulePlan
    live_receipt: LiveFleetReceipt | None = None
    offline_twin_summaries: dict[str, SQLTraceEvalSummary] | None = None
