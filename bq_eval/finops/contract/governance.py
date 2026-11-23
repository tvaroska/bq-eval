"""App risk tiers and lifecycle policy used to gate judge agreement."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.core.contract.types import NonNegFloat, PosFloat, UnitFloat
from bq_eval.finops.contract.chargeback import SpilloverTier

LifecycleTrack = Literal["long_term_ga", "short_term_ga", "preview", "legacy_sunset"]


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
