"""OpenTelemetry GenAI span contracts, portable App ID resolution, and serving-physics burndown."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

UNCACHED_INPUT_BURNDOWN_WEIGHT: float = 1.0
CACHED_INPUT_BURNDOWN_WEIGHT: float = 0.1
OUTPUT_THINKING_BURNDOWN_WEIGHT: float = 4.0

LONG_CONTEXT_THRESHOLD_TOKENS: int = 200_000
LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT: float = 2.0
LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT: float = 0.2
LONG_CONTEXT_OUTPUT_BURNDOWN_WEIGHT: float = 7.5


class GenAISpanKind(StrEnum):
    """Canonical OpenTelemetry GenAI operation kinds."""

    INVOKE_AGENT = "invoke_agent"
    EXECUTE_TOOL = "execute_tool"
    CHAT = "chat"
    EMBEDDINGS = "embeddings"
    RETRIEVAL = "retrieval"


class FinOpsTrafficType(StrEnum):
    """4-Bucket FinOps cost attribution taxonomy."""

    PRODUCTION_INFERENCE = "production_inference"
    CONFLICT_ESCALATION = "conflict_escalation"
    AGENT_GYM_EVAL = "agent_gym_eval"
    AUTORATER_JUDGE = "autorater_judge"


def resolve_application_id(
    gcp_apphub_application_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    service_name: str | None = None,
) -> str:
    """Resolve portable application ID matching SQL COALESCE precedence (EVAL-P08).

    Order of precedence:
    1. Explicit `gcp_apphub_application_id` column
    2. `attributes["gcp.apphub.application.id"]`
    3. `attributes["apphub.application.id"]`
    4. `'unregistered_app'`
    """
    del service_name
    if gcp_apphub_application_id and gcp_apphub_application_id.strip():
        return gcp_apphub_application_id.strip()
    attrs = attributes or {}
    for key in ("gcp.apphub.application.id", "apphub.application.id"):
        val = attrs.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "unregistered_app"


def compute_weighted_burndown_tokens(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
    long_context_threshold: int = LONG_CONTEXT_THRESHOLD_TOKENS,
) -> float:
    """Compute span-level weighted burndown tokens with >200K long-context cliff (EVAL-P08).

    * Standard (<= 200,000 input tokens): 1.0x uncached + 0.1x cached + 4.0x (output + thinking)
    * Long-context (> 200,000 input tokens): 2.0x uncached + 0.2x cached + 7.5x (output + thinking)
    """
    if input_tokens < 0 or cached_input_tokens < 0 or output_tokens < 0 or thinking_tokens < 0:
        raise ValueError("Token counts must be non-negative.")
    if cached_input_tokens > input_tokens:
        raise ValueError("cached_input_tokens cannot exceed input_tokens.")

    uncached_input = max(0, input_tokens - cached_input_tokens)
    if input_tokens > long_context_threshold:
        return (
            LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT * uncached_input
            + LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT * cached_input_tokens
            + LONG_CONTEXT_OUTPUT_BURNDOWN_WEIGHT * (output_tokens + thinking_tokens)
        )
    return (
        UNCACHED_INPUT_BURNDOWN_WEIGHT * uncached_input
        + CACHED_INPUT_BURNDOWN_WEIGHT * cached_input_tokens
        + OUTPUT_THINKING_BURNDOWN_WEIGHT * (output_tokens + thinking_tokens)
    )


class OTelGenAISpanRecord(BaseModel):
    """Pydantic v2 contract for `agent_eval.otel_spans` rows."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(..., min_length=8)
    span_id: str = Field(..., min_length=4)
    parent_span_id: str | None = None
    span_name: str = Field(default="invoke_agent")
    start_time: datetime
    end_time: datetime
    receive_time: datetime | None = None
    latency_ms: float = Field(..., ge=0.0)
    status_code: str = Field(default="OK")
    otel_span_kind: str = Field(default="SPAN_KIND_INTERNAL")
    span_kind: GenAISpanKind = Field(default=GenAISpanKind.INVOKE_AGENT)
    service_name: str = Field(default="agent-service")
    gcp_apphub_application_id: str | None = None
    gcp_apphub_service_id: str | None = None
    gcp_apphub_workload_id: str | None = None
    agent_name: str = Field(default="primary-agent")
    model_name: str = Field(default="gemini-3.8-flash")
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    thinking_tokens: int = Field(default=0, ge=0)
    tool_name: str | None = None
    tool_call_status: str | None = "SUCCESS"
    rule0_pass: bool = True
    finops_traffic_type: FinOpsTrafficType = Field(default=FinOpsTrafficType.PRODUCTION_INFERENCE)
    outcome_id: str | None = None
    outcome_owner: str | None = None
    source_mode: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    entity_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_tokens(self) -> OTelGenAISpanRecord:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens.")
        return self

    @property
    def resolved_application_id(self) -> str:
        """Return portable application ID via COALESCE precedence."""
        return resolve_application_id(
            self.gcp_apphub_application_id,
            self.attributes,
            self.service_name,
        )

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def is_long_context_cliff(self) -> bool:
        return self.input_tokens > LONG_CONTEXT_THRESHOLD_TOKENS

    @property
    def weighted_burndown_tokens(self) -> float:
        return compute_weighted_burndown_tokens(
            input_tokens=self.input_tokens,
            cached_input_tokens=self.cached_input_tokens,
            output_tokens=self.output_tokens,
            thinking_tokens=self.thinking_tokens,
        )
