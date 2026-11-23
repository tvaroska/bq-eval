"""Evaluation result shapes: confusion matrix, calibration, per-app summary, query cost."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.core.contract.types import FiniteFloat, NonNegFloat, UnitFloat


class ConfusionMatrix(BaseModel):
    """Binary autorater vs. human SME confusion matrix on Rule-0-passing traces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tp: int = Field(..., ge=0)
    fn: int = Field(..., ge=0)
    tn: int = Field(..., ge=0)
    fp: int = Field(..., ge=0)

    @property
    def total(self) -> int:
        return self.tp + self.fn + self.tn + self.fp

    @property
    def sensitivity_tpr(self) -> float:
        denom = self.tp + self.fn
        if denom == 0:
            raise ValueError("Cannot compute sensitivity (TPR) with zero positive human labels.")
        return self.tp / denom

    @property
    def specificity_tnr(self) -> float:
        denom = self.tn + self.fp
        if denom == 0:
            raise ValueError("Cannot compute specificity (TNR) with zero negative human labels.")
        return self.tn / denom

    @property
    def youden_j(self) -> float:
        return self.sensitivity_tpr + self.specificity_tnr - 1.0


class RoganGladenResult(BaseModel):
    """Bias-corrected true pass rate and 95% confidence interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_pass_rate: UnitFloat
    calibrated_pass_rate: UnitFloat
    ci_95_lower: UnitFloat
    ci_95_upper: UnitFloat
    sensitivity_tpr: UnitFloat
    specificity_tnr: UnitFloat
    youden_j: FiniteFloat
    sample_size: int = Field(..., gt=0)


class CalibrationResult(BaseModel):
    """Combined IRR (Gwet's AC2) + Rogan-Gladen calibration summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gwet_ac2: FiniteFloat
    passes_irr_gate: bool
    confusion_matrix: ConfusionMatrix
    rogan_gladen: RoganGladenResult


class SQLTraceEvalSummary(BaseModel):
    """Per-application Tier 1-3 evaluation and 4-bucket FinOps summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    rubric_id: str
    total_traces: int = Field(..., ge=0)
    production_traces: int = Field(..., ge=0)
    rule0_pass_rate: UnitFloat
    tool_success_rate: UnitFloat
    p95_latency_ms: NonNegFloat
    avg_cache_hit_ratio: UnitFloat
    total_weighted_burndown_tokens: NonNegFloat
    judged_samples: int = Field(..., ge=0)
    calibration_status: Literal["CALIBRATED", "UNCALIBRATED", "UNJUDGED"]
    raw_judge_pass_rate: UnitFloat | None
    calibrated_pass_rate: UnitFloat | None
    calibrated_ci_95_lower: UnitFloat | None
    calibrated_ci_95_upper: UnitFloat | None
    gwet_ac2: FiniteFloat | None = None
    passes_irr_gate: bool | None = None
    production_inference_usd: NonNegFloat
    conflict_escalation_usd: NonNegFloat
    agent_gym_eval_usd: NonNegFloat
    autorater_judge_usd: NonNegFloat
    optimize_usd: NonNegFloat = 0.0
    bq_query_cost_usd: NonNegFloat = 0.0
    unclassified_usd: NonNegFloat = 0.0
    total_spend_usd: NonNegFloat
    cost_per_verified_outcome_usd: NonNegFloat | None
    outcome_total_count: int = Field(default=0, ge=0)
    outcome_verified_count: int = Field(default=0, ge=0)
    outcome_cost_per_verified_ai_usd: NonNegFloat | None = None
    outcome_cost_per_verified_usd: NonNegFloat | None = None


class BqQueryCostRow(BaseModel):
    """Attributed BigQuery `INFORMATION_SCHEMA.JOBS` bytes-scanned / slot query cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    apphub_application_id: str
    outcome_id: str | None = None
    total_bytes_billed: int = Field(default=0, ge=0)
    total_slot_ms: int = Field(default=0, ge=0)
    pricing_mode: Literal["on_demand", "capacity_slot"] = "on_demand"
    query_cost_usd: NonNegFloat
