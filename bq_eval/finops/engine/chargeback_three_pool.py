"""Three-pool hourly chargeback (prod, eval, optimize) and the capacity scheduling export."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from bq_eval.finops.contract.chargeback import (
    CapacityDeadlineJobPayload,
    CapacityResidualSchedulePayload,
    ThreePoolChargebackSummary,
    ThreePoolFocusRow,
)
from bq_eval.finops.contract.results import HourlyBurndownExportRow

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


@dataclass
class _PoolLedger:
    """Running USD and token totals per (app, lane) across the hours being allocated."""

    keys: list[tuple[str, TrafficLane]]
    tokens: dict[tuple[str, TrafficLane], float] = field(default_factory=dict)
    pool1: dict[tuple[str, TrafficLane], float] = field(default_factory=dict)
    pool2: dict[tuple[str, TrafficLane], float] = field(default_factory=dict)
    pool3_involuntary: dict[tuple[str, TrafficLane], float] = field(default_factory=dict)
    pool3_burst: dict[tuple[str, TrafficLane], float] = field(default_factory=dict)
    idle_tokens: float = 0.0
    idle_usd: float = 0.0

    def __post_init__(self) -> None:
        for book in (self.tokens, self.pool1, self.pool2, self.pool3_involuntary, self.pool3_burst):
            book.update(dict.fromkeys(self.keys, 0.0))

    def row_total(self, k: tuple[str, TrafficLane]) -> float:
        return self.pool1[k] + self.pool2[k] + self.pool3_involuntary[k] + self.pool3_burst[k]


def _validate_three_pool_scalars(
    capacity_tokens: float,
    fixed_cost_usd: float,
    funded_gsus_by_app: Mapping[str, float],
) -> None:
    if not math.isfinite(capacity_tokens) or capacity_tokens <= 0:
        raise ValueError("hourly_reserved_pt_capacity_tokens must be finite and positive.")
    if not math.isfinite(fixed_cost_usd) or fixed_cost_usd < 0.0:
        raise ValueError("reserved_pt_fixed_cost_usd must be finite and non-negative.")
    for app_id, gsus in funded_gsus_by_app.items():
        if not math.isfinite(gsus) or gsus < 0.0:
            raise ValueError(f"funded_gsus_by_app[{app_id!r}] must be finite and non-negative.")


def _is_involuntary_spill(
    app_id: str,
    app_total_tokens: float,
    funded_gsus_by_app: Mapping[str, float],
    capacity_tokens: float,
) -> bool:
    """App-level spillover rule: involuntary when the app's combined demand across all lanes stays within its funded share of the reservation."""
    funded = funded_gsus_by_app.get(app_id, 0.0)
    total_funded = sum(funded_gsus_by_app.values())
    if funded <= 0.0 or total_funded <= 0.0:
        return False
    entitlement = (funded / total_funded) * capacity_tokens
    return app_total_tokens <= entitlement + 1e-9


def _allocate_hour(  # noqa: PLR0913
    ledger: _PoolLedger,
    padded: Mapping[tuple[str, TrafficLane], Sequence[float]],
    hour: int,
    capacity_tokens: float,
    hourly_commit_usd: float,
    funded_gsus_by_app: Mapping[str, float],
    paygo_rates: Mapping[str, float] | float,
) -> None:
    prod_keys = [k for k in ledger.keys if k[1] == "prod"]
    wf_keys = [k for k in ledger.keys if k[1] in ("eval", "optimize")]
    prod_total = sum(padded[k][hour] for k in prod_keys)
    wf_total = sum(padded[k][hour] for k in wf_keys)
    app_hour_totals: dict[str, float] = {}
    for k in ledger.keys:
        app_hour_totals[k[0]] = app_hour_totals.get(k[0], 0.0) + padded[k][hour]

    # Production fills the reservation first; eval/optimize take what is left; the rest is idle.
    prod_on_pt = min(capacity_tokens, prod_total)
    spare_after_prod = max(0.0, capacity_tokens - prod_on_pt)
    wf_on_pt = min(spare_after_prod, wf_total)
    idle = max(0.0, spare_after_prod - wf_on_pt)

    pool1_usd = hourly_commit_usd * (prod_on_pt / capacity_tokens)
    pool2_usd = hourly_commit_usd * (wf_on_pt / capacity_tokens)
    ledger.idle_usd += hourly_commit_usd * (idle / capacity_tokens)
    ledger.idle_tokens += idle
    prod_overflow = max(0.0, prod_total - capacity_tokens)
    wf_overflow = max(0.0, wf_total - spare_after_prod)

    for k in prod_keys:
        app_id, tok = k[0], padded[k][hour]
        ledger.tokens[k] += tok
        if prod_total <= 0.0:
            continue
        share = tok / prod_total
        ledger.pool1[k] += pool1_usd * share
        if prod_overflow > 0.0:
            spill_usd = (prod_overflow * share / 1e6) * _rate_for_app(paygo_rates, app_id)
            if _is_involuntary_spill(
                app_id, app_hour_totals[app_id], funded_gsus_by_app, capacity_tokens
            ):
                ledger.pool3_involuntary[k] += spill_usd
            else:
                ledger.pool3_burst[k] += spill_usd

    for k in wf_keys:
        app_id, tok = k[0], padded[k][hour]
        ledger.tokens[k] += tok
        if wf_total <= 0.0:
            continue
        share = tok / wf_total
        ledger.pool2[k] += pool2_usd * share
        if wf_overflow > 0.0:
            spill_usd = (wf_overflow * share / 1e6) * _rate_for_app(paygo_rates, app_id)
            if _is_involuntary_spill(
                app_id, app_hour_totals[app_id], funded_gsus_by_app, capacity_tokens
            ):
                ledger.pool3_involuntary[k] += spill_usd
            else:
                ledger.pool3_burst[k] += spill_usd


def allocate_focus_three_pool_hourly_chargeback(  # noqa: PLR0913
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
    """Split a reserved-capacity bill and its overflow across apps, hour by hour.

    * Pool 1: production's share of the reservation.
    * Pool 2: eval and optimize traffic that fits in the capacity production left unused;
      whatever is still unused is billed to the platform as idle.
    * Pool 3: pay-as-you-go overflow, split into involuntary (the app stayed within its funded
      share) and tenant burst (it went above it, or had no funded share).
    The pools add up to the fixed cost plus overflow within `1e-12` USD.
    """
    _validate_three_pool_scalars(
        hourly_reserved_pt_capacity_tokens, reserved_pt_fixed_cost_usd, funded_gsus_by_app
    )
    padded, max_hours = _normalize_hourly_lane_matrix(hourly_demand_by_app_lane)
    hourly_commit_usd = reserved_pt_fixed_cost_usd / max_hours
    ledger = _PoolLedger(keys=sorted(padded))
    for hour in range(max_hours):
        _allocate_hour(
            ledger,
            padded,
            hour,
            hourly_reserved_pt_capacity_tokens,
            hourly_commit_usd,
            funded_gsus_by_app,
            paygo_spillover_usd_per_1m_by_app,
        )

    rows = [
        ThreePoolFocusRow(
            apphub_application_id=app_id,
            cost_center=app_cost_centers.get(app_id, "UNASSIGNED"),
            traffic_lane=lane,
            weighted_burndown_tokens=ledger.tokens[(app_id, lane)],
            pool1_active_pt_usd=ledger.pool1[(app_id, lane)],
            pool2_waterfill_pt_usd=ledger.pool2[(app_id, lane)],
            pool2_unutilized_idle_usd=0.0,
            pool3_involuntary_spillover_usd=ledger.pool3_involuntary[(app_id, lane)],
            pool3_tenant_burst_spillover_usd=ledger.pool3_burst[(app_id, lane)],
            total_allocated_usd=ledger.row_total((app_id, lane)),
        )
        for app_id, lane in ledger.keys
    ]
    lane_totals = {"prod": 0.0, "eval": 0.0, "optimize": 0.0}
    for k in ledger.keys:
        lane_totals[k[1]] += ledger.row_total(k)

    if ledger.idle_usd > 0.0:
        rows.append(
            ThreePoolFocusRow(
                apphub_application_id=platform_owner_id,
                cost_center=platform_cost_center,
                traffic_lane="platform_idle",
                weighted_burndown_tokens=ledger.idle_tokens,
                pool1_active_pt_usd=0.0,
                pool2_waterfill_pt_usd=0.0,
                pool2_unutilized_idle_usd=ledger.idle_usd,
                pool3_involuntary_spillover_usd=0.0,
                pool3_tenant_burst_spillover_usd=0.0,
                total_allocated_usd=ledger.idle_usd,
            )
        )

    total_paygo = sum(
        r.pool3_involuntary_spillover_usd + r.pool3_tenant_burst_spillover_usd for r in rows
    )
    total_allocated = sum(r.total_allocated_usd for r in rows)
    return ThreePoolChargebackSummary(
        rows=tuple(rows),
        reserved_pt_fixed_cost_usd=reserved_pt_fixed_cost_usd,
        total_paygo_spillover_usd=total_paygo,
        total_allocated_usd=total_allocated,
        reconciliation_drift_usd=abs(total_allocated - (reserved_pt_fixed_cost_usd + total_paygo)),
        prod_total_usd=lane_totals["prod"],
        eval_total_usd=lane_totals["eval"],
        optimize_total_usd=lane_totals["optimize"],
        platform_idle_usd=ledger.idle_usd,
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
