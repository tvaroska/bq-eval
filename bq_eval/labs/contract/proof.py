"""Experimental result shapes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from bq_eval.core.contract.types import NonNegFloat


class WageSensitivityPoint(BaseModel):
    """Escalation wage sensitivity point across human-review hourly rates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hourly_wage_usd: NonNegFloat
    ai_cost_per_verified_outcome_usd: NonNegFloat
    judge_cost_per_verified_outcome_usd: NonNegFloat
    bq_query_cost_per_verified_outcome_usd: NonNegFloat
    human_escalation_cost_per_verified_outcome_usd: NonNegFloat
    fully_loaded_cpvo_usd: NonNegFloat
