"""FinOps Cost Management, Eval ROI, Prefix-Cache Dilution, NFR=0.0% Pareto Frontier, and FOCUS 1.2 Chargeback."""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.otel import LONG_CONTEXT_THRESHOLD_TOKENS, OTelGenAISpanRecord


class ModelPricingSKU(BaseModel):
    """Effective-dated model pricing and GSU serving-physics burndown entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str
    effective_start: datetime
    effective_end: datetime
    input_usd_per_1m: float = Field(..., ge=0.0)
    cached_input_usd_per_1m: float = Field(..., ge=0.0)
    output_usd_per_1m: float = Field(..., ge=0.0)
    long_context_threshold_tokens: int = LONG_CONTEXT_THRESHOLD_TOKENS
    long_context_input_usd_per_1m: float = Field(..., ge=0.0)
    long_context_cached_usd_per_1m: float = Field(..., ge=0.0)
    long_context_output_usd_per_1m: float = Field(..., ge=0.0)


def compute_span_cost_usd(span: OTelGenAISpanRecord, sku: ModelPricingSKU) -> float:
    """Compute USD cost for a single span using `sku` and its long-context pricing cliff."""
    uncached_input = span.uncached_input_tokens
    output_and_thinking = span.output_tokens + span.thinking_tokens
    if span.input_tokens > sku.long_context_threshold_tokens:
        return (
            (uncached_input / 1e6) * sku.long_context_input_usd_per_1m
            + (span.cached_input_tokens / 1e6) * sku.long_context_cached_usd_per_1m
            + (output_and_thinking / 1e6) * sku.long_context_output_usd_per_1m
        )
    return (
        (uncached_input / 1e6) * sku.input_usd_per_1m
        + (span.cached_input_tokens / 1e6) * sku.cached_input_usd_per_1m
        + (output_and_thinking / 1e6) * sku.output_usd_per_1m
    )


class EvalFinOpsROI(BaseModel):
    """Quantified USD savings of the 3-Tier SQL + Stratified Judge Funnel vs. 100% LLM judging."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_production_traces: int
    tier1_sql_evaluated_traces: int
    tier2_canary_traces: int
    tier2_error_tail_traces: int
    tier2_total_judged_traces: int
    baseline_100pct_judge_usd: float
    stratified_3tier_judge_usd: float
    net_savings_usd: float
    savings_pct: float


def compute_eval_finops_roi(
    total_production_traces: int,
    error_or_sla_tail_rate: float = 0.03,
    canary_sample_rate: float = 0.05,
    judge_cost_per_trace_usd: float = 0.0025,
) -> EvalFinOpsROI:
    """Compute USD and percentage savings of the 3-Tier evaluation funnel (`90-95%` savings)."""
    if total_production_traces <= 0:
        raise ValueError("total_production_traces must be positive.")
    if not (0.0 <= error_or_sla_tail_rate <= 1.0) or not (0.0 <= canary_sample_rate <= 1.0):
        raise ValueError("Sampling rates must be between 0.0 and 1.0.")

    error_traces = round(total_production_traces * error_or_sla_tail_rate)
    healthy_traces = max(0, total_production_traces - error_traces)
    canary_traces = round(healthy_traces * canary_sample_rate)
    judged_traces = error_traces + canary_traces

    baseline_usd = total_production_traces * judge_cost_per_trace_usd
    stratified_usd = judged_traces * judge_cost_per_trace_usd
    savings_usd = max(0.0, baseline_usd - stratified_usd)
    savings_pct = (savings_usd / baseline_usd) * 100.0 if baseline_usd > 0 else 0.0

    return EvalFinOpsROI(
        total_production_traces=total_production_traces,
        tier1_sql_evaluated_traces=total_production_traces,
        tier2_canary_traces=canary_traces,
        tier2_error_tail_traces=error_traces,
        tier2_total_judged_traces=judged_traces,
        baseline_100pct_judge_usd=baseline_usd,
        stratified_3tier_judge_usd=stratified_usd,
        net_savings_usd=savings_usd,
        savings_pct=savings_pct,
    )


class PrefixCacheDilutionReport(BaseModel):
    """Diagnostics for 1/N prompt-prefix cache dilution and recoverable USD savings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    distinct_prompt_variants: int
    theoretical_dilution_ceiling: float
    observed_cache_hit_ratio: float
    target_cache_hit_ratio: float
    dilution_detected: bool
    recoverable_weighted_burndown_tokens: float
    recoverable_monthly_usd: float
    recommendation: str


def diagnose_prefix_cache_dilution(  # noqa: PLR0913
    spans: Sequence[OTelGenAISpanRecord],
    distinct_prompt_variants: int = 1,
    target_cache_hit_ratio: float = 0.75,
    input_usd_per_1m: float = 0.15,
    cached_usd_per_1m: float = 0.015,
    long_context_threshold_tokens: int = LONG_CONTEXT_THRESHOLD_TOKENS,
    long_context_input_usd_per_1m: float | None = None,
    long_context_cached_usd_per_1m: float | None = None,
) -> PrefixCacheDilutionReport:
    """Detect 1/N system-prompt prefix fragmentation and quantify cliff-aware savings."""
    if not spans:
        raise ValueError("spans must be non-empty.")
    if distinct_prompt_variants < 1:
        raise ValueError("distinct_prompt_variants must be >= 1.")

    app_id = spans[0].resolved_application_id
    total_input = sum(s.input_tokens for s in spans)
    total_cached = sum(s.cached_input_tokens for s in spans)
    observed_ratio = (total_cached / total_input) if total_input > 0 else 0.0
    ceiling = 1.0 / float(distinct_prompt_variants)

    long_in_rate = (
        long_context_input_usd_per_1m
        if long_context_input_usd_per_1m is not None
        else input_usd_per_1m * 2.0
    )
    long_cached_rate = (
        long_context_cached_usd_per_1m
        if long_context_cached_usd_per_1m is not None
        else cached_usd_per_1m * 2.0
    )
    std_rate_delta = max(0.0, input_usd_per_1m - cached_usd_per_1m)
    long_rate_delta = max(0.0, long_in_rate - long_cached_rate)

    recoverable_burndown = 0.0
    recoverable_usd = 0.0
    if observed_ratio < target_cache_hit_ratio:
        for span in spans:
            shortfall = max(
                0.0, target_cache_hit_ratio * span.input_tokens - span.cached_input_tokens
            )
            if shortfall <= 0.0:
                continue
            if span.input_tokens > long_context_threshold_tokens:
                # Moving 1 >200K token from uncached (2.0x) to cached (0.2x) saves 1.8x burndown.
                recoverable_burndown += shortfall * 1.8
                recoverable_usd += (shortfall / 1e6) * long_rate_delta
            else:
                # Moving 1 <=200K token from uncached (1.0x) to cached (0.1x) saves 0.9x burndown.
                recoverable_burndown += shortfall * 0.9
                recoverable_usd += (shortfall / 1e6) * std_rate_delta

    dilution_detected = distinct_prompt_variants > 1 and observed_ratio < target_cache_hit_ratio
    if dilution_detected:
        rec = (
            f"Consolidate {distinct_prompt_variants} prompt variants into 1 canonical static "
            "system prefix and move dynamic timestamps/turn state below the cache boundary."
        )
    else:
        rec = "Prefix cache hit ratio meets or exceeds target threshold."

    return PrefixCacheDilutionReport(
        apphub_application_id=app_id,
        distinct_prompt_variants=distinct_prompt_variants,
        theoretical_dilution_ceiling=ceiling,
        observed_cache_hit_ratio=observed_ratio,
        target_cache_hit_ratio=target_cache_hit_ratio,
        dilution_detected=dilution_detected,
        recoverable_weighted_burndown_tokens=recoverable_burndown,
        recoverable_monthly_usd=recoverable_usd,
        recommendation=rec,
    )


class ModelOption(BaseModel):
    """Candidate model/routing configuration for Cost-vs-Quality Pareto analysis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: str
    model_name: str
    rule0_pass_rate: float
    non_fabrication_rate_nfr: float
    calibrated_pass_rate: float
    cost_per_verified_outcome_usd: float
    p95_latency_ms: float


class ParetoEvalResult(BaseModel):
    """Result of Zero-Fabrication (`NFR = 0.0%`) Cost-vs-Quality Pareto frontier selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible_options: tuple[ModelOption, ...]
    disqualified_for_fabrication: tuple[ModelOption, ...]
    pareto_frontier: tuple[ModelOption, ...]
    recommended_default: ModelOption | None


def compute_pareto_frontier(
    candidates: Sequence[ModelOption],
    min_rule0_pass_rate: float = 0.98,
) -> ParetoEvalResult:
    """Filter candidates by `NFR == 0.0` + `rule0_pass_rate` and return the non-dominated frontier."""
    eligible: list[ModelOption] = []
    disqualified: list[ModelOption] = []

    for c in candidates:
        if c.non_fabrication_rate_nfr > 0.0 or c.rule0_pass_rate < min_rule0_pass_rate:
            disqualified.append(c)
        else:
            eligible.append(c)

    frontier: list[ModelOption] = []
    for cand in eligible:
        dominated = False
        for other in eligible:
            if other.config_id == cand.config_id:
                continue
            better_or_equal = (
                other.cost_per_verified_outcome_usd <= cand.cost_per_verified_outcome_usd
                and other.calibrated_pass_rate >= cand.calibrated_pass_rate
                and other.p95_latency_ms <= cand.p95_latency_ms
            )
            strictly_better = (
                other.cost_per_verified_outcome_usd < cand.cost_per_verified_outcome_usd
                or other.calibrated_pass_rate > cand.calibrated_pass_rate
                or other.p95_latency_ms < cand.p95_latency_ms
            )
            if better_or_equal and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(cand)

    frontier.sort(key=lambda x: x.cost_per_verified_outcome_usd)
    recommended = frontier[0] if frontier else None

    return ParetoEvalResult(
        eligible_options=tuple(eligible),
        disqualified_for_fabrication=tuple(disqualified),
        pareto_frontier=tuple(frontier),
        recommended_default=recommended,
    )


class FocusChargebackRow(BaseModel):
    """FOCUS 1.2 Two-Pool Chargeback allocation row for a tenant application or platform pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    cost_center: str
    weighted_burndown_tokens: float
    utilization_share: float
    utilized_pt_cost_usd: float
    unutilized_idle_capacity_tax_usd: float
    payg_overflow_cost_usd: float
    total_allocated_cost_usd: float


def allocate_focus_two_pool_chargeback(  # noqa: PLR0913
    app_burndown_tokens: dict[str, float],
    app_cost_centers: dict[str, str],
    reserved_pt_capacity_tokens: float,
    reserved_pt_fixed_cost_usd: float | None = None,
    payg_overflow_usd_per_1m_weighted: float = 0.15,
    platform_owner_id: str = "platform-shared-reservation",
    platform_cost_center: str = "CC-PLATFORM-IDLE",
    *,
    reserved_pt_Fixed_cost_usd: float | None = None,
) -> list[FocusChargebackRow]:
    """Allocate Provisioned Throughput (PT) + PAYG overflow using FOCUS 1.2 Two-Pool rules.

    * Pool 1 (Utilized PT): Allocated to tenant apps proportional to active weighted burndown.
    * Pool 2 (Unutilized Idle PT Tax): Allocated to `platform_owner_id` so tenant apps are
      never penalized when total demand is below reserved PT capacity.
    * Overflow (PAYG): When total burndown exceeds `reserved_pt_capacity_tokens`, excess tokens
      are billed at `payg_overflow_usd_per_1m_weighted` proportional to usage share.
    """
    if reserved_pt_capacity_tokens <= 0:
        raise ValueError("reserved_pt_capacity_tokens must be positive.")
    if reserved_pt_Fixed_cost_usd is not None:
        warnings.warn(
            "reserved_pt_Fixed_cost_usd is deprecated; use reserved_pt_fixed_cost_usd instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if reserved_pt_fixed_cost_usd is not None:
            raise ValueError(
                "Specify either reserved_pt_fixed_cost_usd or reserved_pt_Fixed_cost_usd, not both."
            )
        fixed_cost_usd = reserved_pt_Fixed_cost_usd
    elif reserved_pt_fixed_cost_usd is None:
        raise ValueError("reserved_pt_fixed_cost_usd is required.")
    else:
        fixed_cost_usd = reserved_pt_fixed_cost_usd

    total_demand = sum(max(0.0, v) for v in app_burndown_tokens.values())
    utilized_tokens = min(reserved_pt_capacity_tokens, total_demand)
    idle_tokens = max(0.0, reserved_pt_capacity_tokens - total_demand)
    overflow_tokens = max(0.0, total_demand - reserved_pt_capacity_tokens)

    utilized_ratio = utilized_tokens / reserved_pt_capacity_tokens
    idle_ratio = idle_tokens / reserved_pt_capacity_tokens

    utilized_pool_usd = fixed_cost_usd * utilized_ratio
    idle_tax_usd = fixed_cost_usd * idle_ratio
    total_overflow_usd = (overflow_tokens / 1e6) * payg_overflow_usd_per_1m_weighted

    rows: list[FocusChargebackRow] = []
    for app_id, tokens in sorted(app_burndown_tokens.items()):
        share = (tokens / total_demand) if total_demand > 0 else 0.0
        app_pt_usd = utilized_pool_usd * share
        app_overflow_usd = total_overflow_usd * share
        rows.append(
            FocusChargebackRow(
                apphub_application_id=app_id,
                cost_center=app_cost_centers.get(app_id, "UNASSIGNED"),
                weighted_burndown_tokens=tokens,
                utilization_share=share,
                utilized_pt_cost_usd=app_pt_usd,
                unutilized_idle_capacity_tax_usd=0.0,
                payg_overflow_cost_usd=app_overflow_usd,
                total_allocated_cost_usd=app_pt_usd + app_overflow_usd,
            )
        )

    if idle_tax_usd > 0.0:
        rows.append(
            FocusChargebackRow(
                apphub_application_id=platform_owner_id,
                cost_center=platform_cost_center,
                weighted_burndown_tokens=idle_tokens,
                utilization_share=idle_ratio,
                utilized_pt_cost_usd=0.0,
                unutilized_idle_capacity_tax_usd=idle_tax_usd,
                payg_overflow_cost_usd=0.0,
                total_allocated_cost_usd=idle_tax_usd,
            )
        )

    return rows


class OffPeakSchedulePlan(BaseModel):
    """Water-filling schedule for running batch Agent Gym & Judge jobs in off-peak PT valleys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hourly_scheduled_eval_tokens: tuple[float, ...]
    total_batch_eval_tokens_requested: float
    scheduled_in_free_pt_valley_tokens: float
    remaining_payg_spillover_tokens: float
    zero_marginal_cost_coverage_pct: float
    avoided_payg_cost_usd: float


def plan_off_peak_eval_schedule(
    hourly_production_burndown: Sequence[float],
    hourly_reserved_pt_capacity: float,
    total_batch_eval_tokens: float,
    payg_usd_per_1m_weighted: float = 0.15,
) -> OffPeakSchedulePlan:
    """Water-fill batch evaluation workloads into off-peak Provisioned Throughput valleys."""
    if not hourly_production_burndown:
        raise ValueError("hourly_production_burndown must be non-empty.")
    if hourly_reserved_pt_capacity <= 0 or total_batch_eval_tokens < 0:
        raise ValueError("Capacity must be positive and batch tokens non-negative.")

    remaining = total_batch_eval_tokens
    scheduled_per_hour: list[float] = []

    for prod_load in hourly_production_burndown:
        spare_pt = max(0.0, hourly_reserved_pt_capacity - prod_load)
        allocated = min(remaining, spare_pt)
        scheduled_per_hour.append(allocated)
        remaining -= allocated

    scheduled_free = total_batch_eval_tokens - remaining
    coverage_pct = (
        (scheduled_free / total_batch_eval_tokens) * 100.0 if total_batch_eval_tokens > 0 else 100.0
    )
    avoided_usd = (scheduled_free / 1e6) * payg_usd_per_1m_weighted

    return OffPeakSchedulePlan(
        hourly_scheduled_eval_tokens=tuple(scheduled_per_hour),
        total_batch_eval_tokens_requested=total_batch_eval_tokens,
        scheduled_in_free_pt_valley_tokens=scheduled_free,
        remaining_payg_spillover_tokens=remaining,
        zero_marginal_cost_coverage_pct=coverage_pct,
        avoided_payg_cost_usd=avoided_usd,
    )
