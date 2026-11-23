"""Failure loss-bank clustering, `ATIF-v1.8` trajectory export & clause-scoped contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.core.contract.types import NonNegFloat, UnitFloat

LossBankStatus = Literal["NEW", "RECURRING", "RESOLVED"]


class PairedEvalOutcome(BaseModel):
    """Paired baseline vs. candidate evaluation verdict for clause-scoped NFR gating & loss-bank clustering."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(..., min_length=4)
    apphub_application_id: str = Field(..., min_length=1)
    rubric_id: str = Field(..., min_length=1)
    baseline_pass: bool
    candidate_pass: bool
    baseline_policy_sha256: str | None = None
    candidate_policy_sha256: str | None = None
    target_clause_id: str | None = None
    amended_clause_ids: tuple[str, ...] = ()
    failure_signature: str = "unclassified_failure"
    reasoning_trace: str = ""
    trace_cost_usd: NonNegFloat = 0.0


class FailureLossBankCluster(BaseModel):
    """Failure cluster categorized as `NEW`, `RECURRING`, or `RESOLVED` across paired runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster_id: str
    apphub_application_id: str
    rubric_id: str
    failure_signature: str
    status: LossBankStatus
    baseline_failure_count: int = Field(..., ge=0)
    candidate_failure_count: int = Field(..., ge=0)
    sample_trace_ids: tuple[str, ...] = ()
    cost_of_failure_usd: NonNegFloat = 0.0


class AtifTrajectoryStep(BaseModel):
    """Single sanitized execution step inside an `ATIF-v1.8` trajectory export."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_index: int = Field(..., ge=0)
    span_id: str
    span_kind: str
    agent_name: str
    model_name: str
    tool_name: str | None = None
    tool_call_status: str | None = None
    status_code: str
    latency_ms: NonNegFloat
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    rule0_pass: bool


class AtifV18Trajectory(BaseModel):
    """Sanitized `ATIF-v1.8` agent trajectory schema feeding autonomous skill/harness optimizer loops."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["ATIF-v1.8"] = "ATIF-v1.8"
    trace_id: str
    session_id: str | None = None
    apphub_application_id: str
    rubric_id: str
    policy_sha256: str | None = None
    loss_bank_status: LossBankStatus
    failure_signature: str
    rule0_pass: bool
    sanitized_prompt: str
    sanitized_response: str
    sanitized_reasoning: str
    steps: tuple[AtifTrajectoryStep, ...]
    total_cost_usd: NonNegFloat = 0.0


class ClauseScopedNfrGateResult(BaseModel):
    """Clause-scoped `policy_sha256` `NFR == 0.0%` gate evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    rubric_id: str
    paired_traces: int = Field(..., ge=0)
    baseline_passing_traces: int = Field(..., ge=0)
    total_negative_flips: int = Field(..., ge=0)
    unamended_negative_flips: int = Field(..., ge=0)
    amended_clause_flips: int = Field(..., ge=0)
    positive_flips: int = Field(..., ge=0)
    unamended_nfr: UnitFloat
    passes_nfr_gate: bool
