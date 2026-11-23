"""Spend result shapes: judge ROI, model trade-offs, chargeback rows, hourly burndown."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.core.contract.types import NonNegFloat, UnitFloat


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
    """Wire contract row for `agent_eval.v_capacity_hourly_burndown_export`."""

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
