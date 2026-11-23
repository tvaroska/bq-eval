"""3-Pool Hourly FOCUS 1.2 Chargeback (`prod|eval|optimize`) & `capacity` BQ-G1 wire sync (`M11-2`)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal

from bq_eval.contract.governance import (
    CapacityDeadlineJobPayload,
    CapacityResidualSchedulePayload,
    ThreePoolChargebackSummary,
    ThreePoolFocusRow,
)
from bq_eval.contract.results import HourlyBurndownExportRow

TrafficLane = Literal["prod", "eval", "optimize"]

_VALID_LANES: frozenset[str] = frozenset({"prod", "eval", "optimize"})
_PROD_TRAFFIC_TYPES: frozenset[str] = frozenset({"production_inference", "conflict_escalation"})


def traffic_lane_for_finops_type(finops_traffic_type: str) -> TrafficLane:
    """Map canonical `FinOpsTrafficType` strings onto isolated `prod | eval | optimize` lanes."""
    norm = finops_traffic_type.strip().lower()
    if norm in ("agent_gym_eval", "autorater_judge"):
        return "eval"
    if norm == "optimize":
        return "optimize"
    return "prod"


def _rate_for_app(
    rates: Mapping[str, float] | float,
    app_id: str,
) -> float:
    val = rates.get(app_id, 0.15) if isinstance(rates, Mapping) else float(rates)
    if not math.isfinite(val) or val < 0.0:
        raise ValueError(f"Invalid PayGo spillover rate for {app_id!r}: {val!r}")
    return float(val)


def _normalize_hourly_lane_matrix(
    hourly_demand_by_app_lane: Mapping[tuple[str, TrafficLane], Sequence[float]],
) -> tuple[dict[tuple[str, TrafficLane], list[float]], int]:
    if not hourly_demand_by_app_lane:
        raise ValueError("hourly_demand_by_app_lane must be non-empty.")
    max_hours = max(len(series) for series in hourly_demand_by_app_lane.values())
    if max_hours < 1:
        raise ValueError("hourly_demand_by_app_lane must have at least 1 hour.")
    padded: dict[tuple[str, TrafficLane], list[float]] = {}
    for (app_id, lane), series in hourly_demand_by_app_lane.items():
        if lane not in _VALID_LANES:
            raise ValueError(
                f"Unsupported traffic lane {lane!r}: expected prod, eval, or optimize."
            )
        clean: list[float] = []
        for idx, tok in enumerate(series):
            if not math.isfinite(tok) or tok < 0.0:
                raise ValueError(
                    f"Demand for ({app_id!r}, {lane!r}) at hour {idx} must be finite and >= 0."
                )
            clean.append(float(tok))
        clean.extend([0.0] * (max_hours - len(clean)))
        padded[(app_id, lane)] = clean
    return padded, max_hours


def allocate_focus_three_pool_hourly_chargeback(  # noqa: C901, PLR0912, PLR0913, PLR0915
    hourly_demand_by_app_lane: Mapping[tuple[str, TrafficLane], Sequence[float]],
    app_cost_centers: Mapping[str, str],
    funded_gsus_by_app: Mapping[str, float],
    hourly_reserved_pt_capacity_tokens: float,
    reserved_pt_fixed_cost_usd: float,
    paygo_spillover_usd_per_1m_by_app: Mapping[str, float] | float = 0.15,
    *,
    platform_owner_id: str = "platform-shared-reservation",
    platform_cost_center: str = "CC-PLATFORM-IDLE",
) -> ThreePoolChargebackSummary:
    """Compute 3-Pool Hourly FOCUS 1.2 Chargeback with `prod|eval|optimize` isolation (`M11-2`).

    * Pool 1 (Active PT): Interactive `prod` share of committed hourly PT capacity.
    * Pool 2 (Water-Fill / Idle PT): `eval` and `optimize` valley reclamation plus central idle tax.
    * Pool 3 (PayGo Spillover): Splits overflow into `Involuntary` (within app's `funded_gsus`
      entitlement) vs. `TenantBurst` (above app's `funded_gsus` entitlement or unreserved burst).
    Reconciles `Pool 1 + Pool 2 + Pool 3` to `< 1e-12` USD drift.
    """
    if (
        not math.isfinite(hourly_reserved_pt_capacity_tokens)
        or hourly_reserved_pt_capacity_tokens <= 0
    ):
        raise ValueError("hourly_reserved_pt_capacity_tokens must be finite and positive.")
    if not math.isfinite(reserved_pt_fixed_cost_usd) or reserved_pt_fixed_cost_usd < 0.0:
        raise ValueError("reserved_pt_fixed_cost_usd must be finite and non-negative.")

    for app_id, gsus in funded_gsus_by_app.items():
        if not math.isfinite(gsus) or gsus < 0.0:
            raise ValueError(f"funded_gsus_by_app[{app_id!r}] must be finite and non-negative.")

    padded, max_hours = _normalize_hourly_lane_matrix(hourly_demand_by_app_lane)
    hourly_commit_usd = reserved_pt_fixed_cost_usd / max_hours
    total_funded_gsus = sum(funded_gsus_by_app.values())

    keys = sorted(padded)
    tot_tokens = dict.fromkeys(keys, 0.0)
    pool1_usd = dict.fromkeys(keys, 0.0)
    pool2_wf_usd = dict.fromkeys(keys, 0.0)
    pool3_invol_usd = dict.fromkeys(keys, 0.0)
    pool3_burst_usd = dict.fromkeys(keys, 0.0)
    idle_tokens_total = 0.0
    idle_usd_total = 0.0

    for h in range(max_hours):
        prod_keys = [k for k in keys if k[1] == "prod"]
        wf_keys = [k for k in keys if k[1] in ("eval", "optimize")]

        prod_total_h = sum(padded[k][h] for k in prod_keys)
        wf_total_h = sum(padded[k][h] for k in wf_keys)

        prod_on_pt_h = min(hourly_reserved_pt_capacity_tokens, prod_total_h)
        spare_after_prod_h = max(0.0, hourly_reserved_pt_capacity_tokens - prod_on_pt_h)
        wf_on_pt_h = min(spare_after_prod_h, wf_total_h)
        idle_h = max(0.0, spare_after_prod_h - wf_on_pt_h)

        pool1_h_usd = hourly_commit_usd * (prod_on_pt_h / hourly_reserved_pt_capacity_tokens)
        pool2_wf_h_usd = hourly_commit_usd * (wf_on_pt_h / hourly_reserved_pt_capacity_tokens)
        idle_usd_total += hourly_commit_usd * (idle_h / hourly_reserved_pt_capacity_tokens)
        idle_tokens_total += idle_h

        prod_overflow_h = max(0.0, prod_total_h - hourly_reserved_pt_capacity_tokens)
        wf_overflow_h = max(0.0, wf_total_h - spare_after_prod_h)

        for k in prod_keys:
            app_id = k[0]
            tok = padded[k][h]
            tot_tokens[k] += tok
            if prod_total_h > 0.0:
                share = tok / prod_total_h
                pool1_usd[k] += pool1_h_usd * share
                if prod_overflow_h > 0.0:
                    app_overflow_tok = prod_overflow_h * share
                    app_entitlement = (
                        (funded_gsus_by_app.get(app_id, 0.0) / total_funded_gsus)
                        * hourly_reserved_pt_capacity_tokens
                        if total_funded_gsus > 0.0
                        else 0.0
                    )
                    rate = _rate_for_app(paygo_spillover_usd_per_1m_by_app, app_id)
                    spill_usd = (app_overflow_tok / 1e6) * rate
                    if funded_gsus_by_app.get(app_id, 0.0) > 0.0 and tok <= app_entitlement + 1e-9:
                        pool3_invol_usd[k] += spill_usd
                    else:
                        pool3_burst_usd[k] += spill_usd

        for k in wf_keys:
            app_id = k[0]
            tok = padded[k][h]
            tot_tokens[k] += tok
            if wf_total_h > 0.0:
                share = tok / wf_total_h
                pool2_wf_usd[k] += pool2_wf_h_usd * share
                if wf_overflow_h > 0.0:
                    wf_overflow_tok = wf_overflow_h * share
                    rate = _rate_for_app(paygo_spillover_usd_per_1m_by_app, app_id)
                    pool3_burst_usd[k] += (wf_overflow_tok / 1e6) * rate

    rows: list[ThreePoolFocusRow] = []
    prod_sum = 0.0
    eval_sum = 0.0
    opt_sum = 0.0

    for k in keys:
        app_id, lane = k
        total_row_usd = pool1_usd[k] + pool2_wf_usd[k] + pool3_invol_usd[k] + pool3_burst_usd[k]
        if lane == "prod":
            prod_sum += total_row_usd
        elif lane == "eval":
            eval_sum += total_row_usd
        else:
            opt_sum += total_row_usd
        rows.append(
            ThreePoolFocusRow(
                apphub_application_id=app_id,
                cost_center=app_cost_centers.get(app_id, "UNASSIGNED"),
                traffic_lane=lane,
                weighted_burndown_tokens=tot_tokens[k],
                pool1_active_pt_usd=pool1_usd[k],
                pool2_waterfill_pt_usd=pool2_wf_usd[k],
                pool2_unutilized_idle_usd=0.0,
                pool3_involuntary_spillover_usd=pool3_invol_usd[k],
                pool3_tenant_burst_spillover_usd=pool3_burst_usd[k],
                total_allocated_usd=total_row_usd,
            )
        )

    if idle_usd_total > 0.0:
        rows.append(
            ThreePoolFocusRow(
                apphub_application_id=platform_owner_id,
                cost_center=platform_cost_center,
                traffic_lane="platform_idle",
                weighted_burndown_tokens=idle_tokens_total,
                pool1_active_pt_usd=0.0,
                pool2_waterfill_pt_usd=0.0,
                pool2_unutilized_idle_usd=idle_usd_total,
                pool3_involuntary_spillover_usd=0.0,
                pool3_tenant_burst_spillover_usd=0.0,
                total_allocated_usd=idle_usd_total,
            )
        )

    total_paygo = sum(
        r.pool3_involuntary_spillover_usd + r.pool3_tenant_burst_spillover_usd for r in rows
    )
    total_allocated = sum(r.total_allocated_usd for r in rows)
    expected_total = reserved_pt_fixed_cost_usd + total_paygo
    drift = abs(total_allocated - expected_total)

    return ThreePoolChargebackSummary(
        rows=tuple(rows),
        reserved_pt_fixed_cost_usd=reserved_pt_fixed_cost_usd,
        total_paygo_spillover_usd=total_paygo,
        total_allocated_usd=total_allocated,
        reconciliation_drift_usd=drift,
        prod_total_usd=prod_sum,
        eval_total_usd=eval_sum,
        optimize_total_usd=opt_sum,
        platform_idle_usd=idle_usd_total,
    )


def build_capacity_residual_schedule_request(  # noqa: PLR0913
    hourly_rows: Sequence[HourlyBurndownExportRow],
    *,
    model: str,
    provisioned_gsus: int,
    safety_headroom_ratio: float = 0.85,
    deadline_jobs: Sequence[CapacityDeadlineJobPayload] = (),
    evergreen_backfill_tokens: float = 0.0,
    paygo_per_1m_burndown_tokens_usd: float = 0.50,
) -> CapacityResidualSchedulePayload:
    """Export a `<= 85%` off-peak PT valley scavenging payload matching `capacity` `ResidualScheduleRequest`."""
    if not hourly_rows:
        raise ValueError("hourly_rows must be non-empty.")
    if not (0.1 < safety_headroom_ratio <= 0.85):
        raise ValueError("safety_headroom_ratio must satisfy 0.1 < ratio <= 0.85.")

    revenue_by_hour: dict[str, float] = {}
    inferred_evergreen = 0.0
    for row in hourly_rows:
        if row.model_name != model and row.model != model:
            continue
        hour_key = row.timestamp or row.usage_hour
        revenue_by_hour.setdefault(hour_key, 0.0)
        if row.finops_traffic_type in _PROD_TRAFFIC_TYPES:
            revenue_by_hour[hour_key] += row.weighted_burndown_tokens / 60.0
        else:
            inferred_evergreen += row.weighted_burndown_tokens

    if not revenue_by_hour:
        raise ValueError(f"No hourly burndown rows matched model {model!r}.")

    ordered_tpm = tuple(round(revenue_by_hour[h], 2) for h in sorted(revenue_by_hour))
    effective_backfill = (
        evergreen_backfill_tokens if evergreen_backfill_tokens > 0.0 else inferred_evergreen
    )

    return CapacityResidualSchedulePayload(
        model=model,
        provisioned_gsus=provisioned_gsus,
        safety_headroom_ratio=safety_headroom_ratio,
        revenue_tpm_by_hour=ordered_tpm[:168],
        deadline_jobs=tuple(deadline_jobs),
        evergreen_backfill_tokens=effective_backfill,
        paygo_per_1m_burndown_tokens_usd=paygo_per_1m_burndown_tokens_usd,
    )
