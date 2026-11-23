"""FinOps Cost Management, Eval ROI, Prefix-Cache Dilution, Pareto Frontier, and FOCUS 1.2 Chargeback."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from bq_eval.contract.otel import (
    CACHED_INPUT_BURNDOWN_WEIGHT,
    LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT,
    LONG_CONTEXT_THRESHOLD_TOKENS,
    LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT,
    UNCACHED_INPUT_BURNDOWN_WEIGHT,
    OTelGenAISpanRecord,
)
from bq_eval.contract.pricing import DEFAULT_MODEL_SKUS, ModelPricingSKU, normalize_model_name
from bq_eval.contract.results import (
    EvalFinOpsROI,
    FocusChargebackRow,
    ModelOption,
    OffPeakSchedulePlan,
    ParetoEvalResult,
    PrefixCacheDilutionReport,
)

_STD_CACHE_BURNDOWN_DELTA: float = UNCACHED_INPUT_BURNDOWN_WEIGHT - CACHED_INPUT_BURNDOWN_WEIGHT
_LONG_CACHE_BURNDOWN_DELTA: float = (
    LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT - LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT
)


def compute_span_cost_usd(
    span: OTelGenAISpanRecord,
    sku: ModelPricingSKU | None = None,
) -> float:
    """Single canonical span USD cost formula (`F12`).

    Uses `sku` when supplied; otherwise resolves `span.model_name` (after stripping release
    suffixes) against `DEFAULT_MODEL_SKUS` and fails closed on unknown non-zero-token models.
    """
    if sku is None:
        if (
            not span.model_name
            or span.model_name == "none"
            or (span.input_tokens == 0 and span.output_tokens == 0 and span.thinking_tokens == 0)
        ):
            return 0.0
        normalized = normalize_model_name(span.model_name)
        resolved = DEFAULT_MODEL_SKUS.get(normalized)
        if resolved is None:
            raise ValueError(
                f"Unknown model_name {span.model_name!r} (normalized {normalized!r}): "
                f"expected one of {sorted(DEFAULT_MODEL_SKUS)}."
            )
    else:
        resolved = sku

    uncached_input = span.uncached_input_tokens
    output_and_thinking = span.output_tokens + span.thinking_tokens
    if span.input_tokens > resolved.long_context_threshold_tokens:
        return (
            (uncached_input / 1e6) * resolved.long_context_input_usd_per_1m
            + (span.cached_input_tokens / 1e6) * resolved.long_context_cached_usd_per_1m
            + (output_and_thinking / 1e6) * resolved.long_context_output_usd_per_1m
        )
    return (
        (uncached_input / 1e6) * resolved.input_usd_per_1m
        + (span.cached_input_tokens / 1e6) * resolved.cached_input_usd_per_1m
        + (output_and_thinking / 1e6) * resolved.output_usd_per_1m
    )


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
    if not math.isfinite(judge_cost_per_trace_usd) or judge_cost_per_trace_usd < 0.0:
        raise ValueError("judge_cost_per_trace_usd must be finite and non-negative.")

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
    """Detect 1/N system-prompt prefix fragmentation and quantify cliff-aware window savings."""
    if not spans:
        raise ValueError("spans must be non-empty.")
    if distinct_prompt_variants < 1:
        raise ValueError("distinct_prompt_variants must be >= 1.")
    if not math.isfinite(target_cache_hit_ratio) or not (0.0 <= target_cache_hit_ratio <= 1.0):
        raise ValueError("target_cache_hit_ratio must be finite and in [0.0, 1.0].")

    app_ids = sorted({s.resolved_application_id for s in spans})
    if len(app_ids) > 1:
        raise ValueError(
            f"diagnose_prefix_cache_dilution requires single-application spans, got {app_ids}."
        )
    app_id = app_ids[0]

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
                recoverable_burndown += shortfall * _LONG_CACHE_BURNDOWN_DELTA
                recoverable_usd += (shortfall / 1e6) * long_rate_delta
            else:
                recoverable_burndown += shortfall * _STD_CACHE_BURNDOWN_DELTA
                recoverable_usd += (shortfall / 1e6) * std_rate_delta

    dilution_detected = (
        distinct_prompt_variants > 1
        and observed_ratio < target_cache_hit_ratio
        and observed_ratio <= ceiling
    )
    rec = (
        f"Consolidate {distinct_prompt_variants} prompt variants into 1 canonical static "
        "system prefix and move dynamic timestamps/turn state below the cache boundary."
        if dilution_detected
        else "Prefix cache hit ratio meets or exceeds target threshold."
    )

    return PrefixCacheDilutionReport(
        apphub_application_id=app_id,
        distinct_prompt_variants=distinct_prompt_variants,
        theoretical_dilution_ceiling=ceiling,
        observed_cache_hit_ratio=observed_ratio,
        target_cache_hit_ratio=target_cache_hit_ratio,
        dilution_detected=dilution_detected,
        recoverable_weighted_burndown_tokens=recoverable_burndown,
        recoverable_window_usd=recoverable_usd,
        recommendation=rec,
    )


def compute_pareto_frontier(
    candidates: Sequence[ModelOption],
    min_rule0_pass_rate: float = 0.98,
) -> ParetoEvalResult:
    """Filter candidates by `fabrication_rate == 0.0` + `rule0_pass_rate` and return the frontier."""
    if not math.isfinite(min_rule0_pass_rate) or not (0.0 <= min_rule0_pass_rate <= 1.0):
        raise ValueError("min_rule0_pass_rate must be finite and in [0.0, 1.0].")
    eligible: list[ModelOption] = []
    disqualified: list[ModelOption] = []

    for cand in candidates:
        if cand.fabrication_rate > 0.0 or cand.rule0_pass_rate < min_rule0_pass_rate:
            disqualified.append(cand)
        else:
            eligible.append(cand)

    frontier: list[ModelOption] = []
    for cand in eligible:
        dominated = any(
            other.config_id != cand.config_id
            and other.cost_per_verified_outcome_usd <= cand.cost_per_verified_outcome_usd
            and other.calibrated_pass_rate >= cand.calibrated_pass_rate
            and other.p95_latency_ms <= cand.p95_latency_ms
            and (
                other.cost_per_verified_outcome_usd < cand.cost_per_verified_outcome_usd
                or other.calibrated_pass_rate > cand.calibrated_pass_rate
                or other.p95_latency_ms < cand.p95_latency_ms
            )
            for other in eligible
        )
        if not dominated:
            frontier.append(cand)

    frontier.sort(key=lambda x: x.cost_per_verified_outcome_usd)
    return ParetoEvalResult(
        eligible_options=tuple(eligible),
        disqualified_options=tuple(disqualified),
        pareto_frontier=tuple(frontier),
        recommended_default=frontier[0] if frontier else None,
    )


def _validate_chargeback_scalars(
    reserved_capacity_tokens: float,
    reserved_fixed_cost_usd: float,
    payg_overflow_usd_per_1m_weighted: float,
) -> None:
    if not math.isfinite(reserved_capacity_tokens) or reserved_capacity_tokens <= 0:
        raise ValueError("reserved_pt_capacity_tokens must be finite and positive.")
    if not math.isfinite(reserved_fixed_cost_usd) or reserved_fixed_cost_usd < 0.0:
        raise ValueError("reserved_pt_fixed_cost_usd must be finite and non-negative.")
    if (
        not math.isfinite(payg_overflow_usd_per_1m_weighted)
        or payg_overflow_usd_per_1m_weighted < 0.0
    ):
        raise ValueError("payg_overflow_usd_per_1m_weighted must be finite and non-negative.")


def allocate_focus_two_pool_chargeback(  # noqa: PLR0913
    app_burndown_tokens: Mapping[str, float],
    app_cost_centers: Mapping[str, str],
    reserved_pt_capacity_tokens: float,
    reserved_pt_fixed_cost_usd: float,
    payg_overflow_usd_per_1m_weighted: float = 0.15,
    platform_owner_id: str = "platform-shared-reservation",
    platform_cost_center: str = "CC-PLATFORM-IDLE",
) -> list[FocusChargebackRow]:
    """Allocate Provisioned Throughput (PT) + PAYG overflow using aggregate FOCUS 1.2 Two-Pool rules."""
    _validate_chargeback_scalars(
        reserved_pt_capacity_tokens,
        reserved_pt_fixed_cost_usd,
        payg_overflow_usd_per_1m_weighted,
    )
    for app_id, tokens in app_burndown_tokens.items():
        if not math.isfinite(tokens) or tokens < 0.0:
            raise ValueError(
                f"app_burndown_tokens[{app_id!r}] must be finite and non-negative, got {tokens!r}."
            )

    total_demand = sum(app_burndown_tokens.values())
    utilized_tokens = min(reserved_pt_capacity_tokens, total_demand)
    idle_tokens = max(0.0, reserved_pt_capacity_tokens - total_demand)
    overflow_tokens = max(0.0, total_demand - reserved_pt_capacity_tokens)

    utilized_ratio = utilized_tokens / reserved_pt_capacity_tokens
    idle_ratio = idle_tokens / reserved_pt_capacity_tokens

    utilized_pool_usd = reserved_pt_fixed_cost_usd * utilized_ratio
    idle_tax_usd = reserved_pt_fixed_cost_usd * idle_ratio
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


def _pad_hourly_burndown(
    hourly_burndown_by_app: Mapping[str, Sequence[float]],
) -> tuple[dict[str, list[float]], int]:
    if not hourly_burndown_by_app:
        raise ValueError("hourly_burndown_by_app must be non-empty.")
    max_hours = max(len(series) for series in hourly_burndown_by_app.values())
    if max_hours < 1:
        raise ValueError("hourly_burndown_by_app must have at least 1 hour.")
    padded: dict[str, list[float]] = {}
    for app_id, series in hourly_burndown_by_app.items():
        clean: list[float] = []
        for idx, tok in enumerate(series):
            if not math.isfinite(tok) or tok < 0.0:
                raise ValueError(
                    f"hourly_burndown_by_app[{app_id!r}][{idx}] must be finite and non-negative."
                )
            clean.append(float(tok))
        clean.extend([0.0] * (max_hours - len(clean)))
        padded[app_id] = clean
    return padded, max_hours


def allocate_focus_hourly_chargeback(  # noqa: PLR0913
    hourly_burndown_by_app: Mapping[str, Sequence[float]],
    app_cost_centers: Mapping[str, str],
    hourly_reserved_pt_capacity_tokens: float,
    reserved_pt_fixed_cost_usd: float,
    payg_overflow_usd_per_1m_weighted: float = 0.0,
    platform_owner_id: str = "platform-shared-reservation",
    platform_cost_center: str = "CC-PLATFORM-IDLE",
) -> list[FocusChargebackRow]:
    """Hourly-clamped FOCUS 1.2 Two-Pool Chargeback aligned with `capacity` (`M7-3` / `BQ-G1`)."""
    _validate_chargeback_scalars(
        hourly_reserved_pt_capacity_tokens,
        reserved_pt_fixed_cost_usd,
        payg_overflow_usd_per_1m_weighted,
    )
    padded, max_hours = _pad_hourly_burndown(hourly_burndown_by_app)
    hourly_commit_usd = reserved_pt_fixed_cost_usd / max_hours

    app_tokens = dict.fromkeys(padded, 0.0)
    app_pt_usd = dict.fromkeys(padded, 0.0)
    app_overflow_usd = dict.fromkeys(padded, 0.0)
    idle_tokens_total = 0.0
    idle_tax_total = 0.0

    for h in range(max_hours):
        hour_total = sum(padded[app_id][h] for app_id in padded)
        utilized_h = min(hourly_reserved_pt_capacity_tokens, hour_total)
        idle_h = max(0.0, hourly_reserved_pt_capacity_tokens - hour_total)
        overflow_h = max(0.0, hour_total - hourly_reserved_pt_capacity_tokens)

        active_cost_h = hourly_commit_usd * (utilized_h / hourly_reserved_pt_capacity_tokens)
        idle_tax_total += hourly_commit_usd * (idle_h / hourly_reserved_pt_capacity_tokens)
        idle_tokens_total += idle_h
        overflow_cost_h = (overflow_h / 1e6) * payg_overflow_usd_per_1m_weighted

        for app_id, series in padded.items():
            tok_h = series[h]
            app_tokens[app_id] += tok_h
            if hour_total > 0.0:
                share_h = tok_h / hour_total
                app_pt_usd[app_id] += active_cost_h * share_h
                app_overflow_usd[app_id] += overflow_cost_h * share_h

    total_demand = sum(app_tokens.values())
    total_capacity = hourly_reserved_pt_capacity_tokens * max_hours
    rows = [
        FocusChargebackRow(
            apphub_application_id=app_id,
            cost_center=app_cost_centers.get(app_id, "UNASSIGNED"),
            weighted_burndown_tokens=app_tokens[app_id],
            utilization_share=(app_tokens[app_id] / total_demand) if total_demand > 0.0 else 0.0,
            utilized_pt_cost_usd=app_pt_usd[app_id],
            unutilized_idle_capacity_tax_usd=0.0,
            payg_overflow_cost_usd=app_overflow_usd[app_id],
            total_allocated_cost_usd=app_pt_usd[app_id] + app_overflow_usd[app_id],
        )
        for app_id in sorted(padded)
    ]

    if idle_tax_total > 0.0:
        rows.append(
            FocusChargebackRow(
                apphub_application_id=platform_owner_id,
                cost_center=platform_cost_center,
                weighted_burndown_tokens=idle_tokens_total,
                utilization_share=idle_tokens_total / total_capacity,
                utilized_pt_cost_usd=0.0,
                unutilized_idle_capacity_tax_usd=idle_tax_total,
                payg_overflow_cost_usd=0.0,
                total_allocated_cost_usd=idle_tax_total,
            )
        )
    return rows


def _solve_water_level(valley_loads: list[float], capacity: float, budget: float) -> float:
    levels = [*sorted(valley_loads), capacity]
    remaining = budget
    for k in range(1, len(levels)):
        band_capacity = k * (levels[k] - levels[k - 1])
        if remaining <= band_capacity + 1e-12:
            return min(capacity, levels[k - 1] + (remaining / k))
        remaining -= band_capacity
    return capacity


def plan_off_peak_eval_schedule(
    hourly_production_burndown: Sequence[float],
    hourly_reserved_pt_capacity: float,
    total_batch_eval_tokens: float,
    payg_usd_per_1m_weighted: float = 0.15,
) -> OffPeakSchedulePlan:
    """Water-fill batch evaluation workloads into the deepest off-peak PT valleys (`F21`)."""
    if not hourly_production_burndown:
        raise ValueError("hourly_production_burndown must be non-empty.")
    if (
        not math.isfinite(hourly_reserved_pt_capacity)
        or hourly_reserved_pt_capacity <= 0
        or not math.isfinite(total_batch_eval_tokens)
        or total_batch_eval_tokens < 0
    ):
        raise ValueError("Capacity must be finite/positive and batch tokens finite/non-negative.")
    for idx, load in enumerate(hourly_production_burndown):
        if not math.isfinite(load) or load < 0.0:
            raise ValueError(f"hourly_production_burndown[{idx}] must be finite and non-negative.")

    spare_per_hour = [
        max(0.0, hourly_reserved_pt_capacity - load) for load in hourly_production_burndown
    ]
    total_spare = sum(spare_per_hour)

    if total_batch_eval_tokens <= 0.0 or total_spare <= 0.0:
        scheduled_per_hour = [0.0 for _ in hourly_production_burndown]
    elif total_batch_eval_tokens >= total_spare:
        scheduled_per_hour = spare_per_hour
    else:
        valleys = [
            float(load) for load in hourly_production_burndown if load < hourly_reserved_pt_capacity
        ]
        water_level = _solve_water_level(
            valleys, hourly_reserved_pt_capacity, total_batch_eval_tokens
        )
        scheduled_per_hour = [
            max(0.0, min(hourly_reserved_pt_capacity, water_level) - load)
            if load < hourly_reserved_pt_capacity
            else 0.0
            for load in hourly_production_burndown
        ]

    scheduled_free = sum(scheduled_per_hour)
    remaining = max(0.0, total_batch_eval_tokens - scheduled_free)
    coverage_pct = (
        min(100.0, (scheduled_free / total_batch_eval_tokens) * 100.0)
        if total_batch_eval_tokens > 0
        else 100.0
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
