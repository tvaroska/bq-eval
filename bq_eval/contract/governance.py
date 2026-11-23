"""Bootstrap, Risk-Tier Governance (`RL-1`-`RL-5`), 3-Pool FOCUS 1.2 & Capacity Wire Contracts (`M10`, `M11`)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.contract.types import NonNegFloat, PosFloat, UnitFloat

BootstrapMode = Literal["light", "full", "hybrid"]
LifecycleTrack = Literal["long_term_ga", "short_term_ga", "preview", "legacy_sunset"]
SpilloverTier = Literal["priority", "standard", "flex"]


class RiskLevel(StrEnum):
    """5-level enterprise application criticality taxonomy (`RL-1` highest -> `RL-5` lowest)."""

    RL_1 = "RL-1"
    RL_2 = "RL-2"
    RL_3A = "RL-3a"
    RL_3B = "RL-3b"
    RL_4 = "RL-4"
    RL_5 = "RL-5"


class AutonomyTier(StrEnum):
    """Agent operational autonomy envelope paired with `RiskLevel` in `app_registry`."""

    ASSISTIVE = "ASSISTIVE"
    HUMAN_IN_LOOP = "HUMAN_IN_LOOP"
    HUMAN_ON_LOOP = "HUMAN_ON_LOOP"
    BOUNDED_AUTONOMOUS = "BOUNDED_AUTONOMOUS"
    FULLY_AUTONOMOUS = "FULLY_AUTONOMOUS"


class LifecycleTrackPolicy(StrEnum):
    """Policy governing which model GA lifecycle tracks an application may bind PT commitments to."""

    LONG_TERM_GA_ONLY = "long_term_ga_only"
    SHORT_TERM_GA_ALLOWED = "short_term_ga_allowed"
    PREVIEW_ALLOWED = "preview_allowed"


class AppGovernanceRecord(BaseModel):
    """Typed `app_registry` governance row enforcing `RL-1`-`RL-5` `AC2` and PT lifecycle gates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str = Field(..., min_length=1)
    display_name: str = Field(default="")
    business_unit: str = Field(default="unassigned_bu")
    cost_center: str = Field(default="UNASSIGNED")
    risk_tier: RiskLevel = Field(default=RiskLevel.RL_3B)
    autonomy_tier: AutonomyTier = Field(default=AutonomyTier.HUMAN_IN_LOOP)
    lifecycle_track_policy: LifecycleTrackPolicy = Field(
        default=LifecycleTrackPolicy.LONG_TERM_GA_ONLY
    )
    funded_gsus: NonNegFloat = 0.0
    spillover_tier: SpilloverTier = "standard"
    pt_commitment_months: int = Field(default=0, ge=0, le=36)
    sla_latency_ms: PosFloat = 5000.0
    is_external_counterparty: bool = False
    storage_usd_per_outcome: NonNegFloat | None = None


class GovernanceEvaluationResult(BaseModel):
    """Result of evaluating an application against `RL-1`-`RL-5` `AC2` and PT lifecycle policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    risk_tier: RiskLevel
    required_ac2_gate: UnitFloat
    observed_ac2: float | None = None
    passes_ac2_gate: bool | None = None
    model_name: str
    model_lifecycle_track: LifecycleTrack
    pt_commitment_allowed: bool
    violations: tuple[str, ...] = ()

    @property
    def compliant(self) -> bool:
        return not self.violations and (self.passes_ac2_gate is not False)


class BootstrapValidationReport(BaseModel):
    """Deterministic result of `bq-eval bootstrap --offline-schema` or `--dry-run` (`M10-1`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: BootstrapMode
    project_id: str
    dataset_id: str
    connection_id: str
    payload_uri_pattern: str
    statement_count: int = Field(..., ge=0)
    checked_count: int = Field(..., ge=0)
    skipped_count: int = Field(..., ge=0)
    schema_column_count: int = Field(..., ge=0)
    unresolved_placeholders: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    sqlglot_verified: bool = False
    live_dry_run_executed: bool = False

    @property
    def passed(self) -> bool:
        return not self.errors and not self.unresolved_placeholders


class ThreePoolFocusRow(BaseModel):
    """3-Pool Hourly FOCUS 1.2 + Spillover (`Involuntary` vs. `TenantBurst`) chargeback row (`M11-2`)."""

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
    """Reconciled 3-Pool Hourly FOCUS 1.2 report with zero-drift accounting (`M11-2`)."""

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
    """Wire-compatible payload matching `capacity.contract.requests.ResidualScheduleRequest` (`M11-2`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(..., min_length=1)
    provisioned_gsus: int = Field(..., ge=1, le=100_000)
    safety_headroom_ratio: Annotated[float, Field(gt=0.1, le=0.85, allow_inf_nan=False)] = 0.85
    revenue_tpm_by_hour: tuple[NonNegFloat, ...] = Field(..., min_length=1, max_length=168)
    deadline_jobs: tuple[CapacityDeadlineJobPayload, ...] = ()
    evergreen_backfill_tokens: NonNegFloat = 0.0
    paygo_per_1m_burndown_tokens_usd: PosFloat = 0.50


class BqQueryCostRow(BaseModel):
    """Attributed BigQuery `INFORMATION_SCHEMA.JOBS` bytes-scanned / slot query cost (`M10-3`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    apphub_application_id: str
    outcome_id: str | None = None
    total_bytes_billed: int = Field(default=0, ge=0)
    total_slot_ms: int = Field(default=0, ge=0)
    pricing_mode: Literal["on_demand", "capacity_slot"] = "on_demand"
    query_cost_usd: NonNegFloat


class WageSensitivityPoint(BaseModel):
    """Escalation wage sensitivity point across human-review hourly rates (`M10-3`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hourly_wage_usd: NonNegFloat
    ai_cost_per_verified_outcome_usd: NonNegFloat
    judge_cost_per_verified_outcome_usd: NonNegFloat
    bq_query_cost_per_verified_outcome_usd: NonNegFloat
    human_escalation_cost_per_verified_outcome_usd: NonNegFloat
    fully_loaded_cpvo_usd: NonNegFloat
