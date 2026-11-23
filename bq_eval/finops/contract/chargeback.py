"""Three-pool hourly chargeback rows and capacity scheduling payloads."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.core.contract.types import NonNegFloat, PosFloat

SpilloverTier = Literal["priority", "standard", "flex"]


class ThreePoolFocusRow(BaseModel):
    """3-Pool Hourly FOCUS 1.2 + Spillover (`Involuntary` vs. `TenantBurst`) chargeback row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    cost_center: str
    traffic_lane: Literal["prod", "eval", "optimize", "platform_idle"]
    weighted_burndown_tokens: NonNegFloat
    pool1_active_pt_usd: NonNegFloat
    pool2_waterfill_pt_usd: NonNegFloat
    pool2_unutilized_idle_usd: NonNegFloat
    pool3_involuntary_spillover_usd: NonNegFloat
    pool3_tenant_burst_spillover_usd: NonNegFloat
    total_allocated_usd: NonNegFloat


class ThreePoolChargebackSummary(BaseModel):
    """Reconciled 3-Pool Hourly FOCUS 1.2 report with zero-drift accounting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ThreePoolFocusRow, ...]
    reserved_pt_fixed_cost_usd: NonNegFloat
    total_paygo_spillover_usd: NonNegFloat
    total_allocated_usd: NonNegFloat
    reconciliation_drift_usd: NonNegFloat
    prod_total_usd: NonNegFloat
    eval_total_usd: NonNegFloat
    optimize_total_usd: NonNegFloat
    platform_idle_usd: NonNegFloat


class CapacityDeadlineJobPayload(BaseModel):
    """Wire-compatible deadline job spec matching `capacity.contract.requests.DeadlineJobSpec`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str = Field(..., min_length=1)
    value_usd: PosFloat
    burndown_tokens: PosFloat
    deadline_hour: int = Field(..., ge=1, le=168)


class CapacityResidualSchedulePayload(BaseModel):
    """Wire-compatible payload matching `capacity.contract.requests.ResidualScheduleRequest`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(..., min_length=1)
    provisioned_gsus: int = Field(..., ge=1, le=100_000)
    safety_headroom_ratio: Annotated[float, Field(gt=0.1, le=0.85, allow_inf_nan=False)] = 0.85
    revenue_tpm_by_hour: tuple[NonNegFloat, ...] = Field(..., min_length=1, max_length=168)
    deadline_jobs: tuple[CapacityDeadlineJobPayload, ...] = ()
    evergreen_backfill_tokens: NonNegFloat = 0.0
    paygo_per_1m_burndown_tokens_usd: PosFloat = 0.50
