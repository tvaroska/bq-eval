"""Demo data shapes: seed templates, load-generator report, live receipts, run artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.core.contract.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.core.contract.results import ConfusionMatrix, SQLTraceEvalSummary
from bq_eval.core.contract.types import FiniteFloat, NonNegFloat, PosFloat, UnitFloat
from bq_eval.finops.contract.results import (
    EvalFinOpsROI,
    FocusChargebackRow,
    OffPeakSchedulePlan,
    ParetoEvalResult,
    PrefixCacheDilutionReport,
)


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
    """Portable scenario/corpus file contract for `bq-eval demo --scenario-file`."""

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
