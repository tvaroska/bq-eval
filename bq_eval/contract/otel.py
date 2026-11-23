"""OpenTelemetry GenAI span, payload, and BQAA event contracts with serving-physics burndown."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bq_eval.contract.types import NonNegFloat

UNCACHED_INPUT_BURNDOWN_WEIGHT: float = 1.0
CACHED_INPUT_BURNDOWN_WEIGHT: float = 0.1
CACHE_WRITE_5M_BURNDOWN_WEIGHT_1P: float = 1.0
CACHE_WRITE_1H_BURNDOWN_WEIGHT_1P: float = 1.0
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
    """5-Bucket FinOps cost attribution taxonomy (`prod`, `eval`, `optimize`) plus unclassified."""

    PRODUCTION_INFERENCE = "production_inference"
    CONFLICT_ESCALATION = "conflict_escalation"
    AGENT_GYM_EVAL = "agent_gym_eval"
    AUTORATER_JUDGE = "autorater_judge"
    OPTIMIZE = "optimize"
    UNCLASSIFIED = "unclassified"


class ServingTrafficType(StrEnum):
    """Serving capacity fulfillment tier returned in `usage_metadata.traffic_type` (`M11-1`)."""

    PROVISIONED_THROUGHPUT = "PROVISIONED_THROUGHPUT"
    PRIORITY = "PRIORITY"
    STANDARD = "STANDARD"
    FLEX = "FLEX"


_SERVING_TRAFFIC_ALIASES: dict[str, ServingTrafficType] = {
    "PROVISIONED_THROUGHPUT": ServingTrafficType.PROVISIONED_THROUGHPUT,
    "PROVISIONED": ServingTrafficType.PROVISIONED_THROUGHPUT,
    "PT": ServingTrafficType.PROVISIONED_THROUGHPUT,
    "DEDICATED": ServingTrafficType.PROVISIONED_THROUGHPUT,
    "PRIORITY": ServingTrafficType.PRIORITY,
    "PRIORITY_PAYGO": ServingTrafficType.PRIORITY,
    "ON_DEMAND_PRIORITY": ServingTrafficType.PRIORITY,
    "STANDARD": ServingTrafficType.STANDARD,
    "STANDARD_PAYGO": ServingTrafficType.STANDARD,
    "ON_DEMAND": ServingTrafficType.STANDARD,
    "SHARED": ServingTrafficType.STANDARD,
    "UNSPECIFIED": ServingTrafficType.STANDARD,
    "FLEX": ServingTrafficType.FLEX,
    "FLEX_PAYGO": ServingTrafficType.FLEX,
    "ON_DEMAND_FLEX": ServingTrafficType.FLEX,
    "BATCH": ServingTrafficType.FLEX,
}


def normalize_serving_traffic_type(raw_value: object) -> ServingTrafficType:
    """Normalize Vertex / OpenTelemetry serving traffic strings to canonical `ServingTrafficType`."""
    if isinstance(raw_value, ServingTrafficType):
        return raw_value
    if not isinstance(raw_value, str) or not raw_value.strip():
        return ServingTrafficType.STANDARD
    return _SERVING_TRAFFIC_ALIASES.get(raw_value.strip().upper(), ServingTrafficType.STANDARD)


def resolve_application_id(
    gcp_apphub_application_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> str:
    """Resolve portable application ID matching SQL COALESCE precedence (EVAL-P08 / ADR-003).

    Order of precedence:
    1. Explicit `gcp_apphub_application_id` column
    2. `attributes["gcp.apphub.application.id"]`
    3. `attributes["apphub.application.id"]`
    4. `'unregistered_app'`
    """
    if gcp_apphub_application_id and gcp_apphub_application_id.strip():
        return gcp_apphub_application_id.strip()
    attrs = attributes or {}
    for key in ("gcp.apphub.application.id", "apphub.application.id"):
        val = attrs.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "unregistered_app"


def compute_weighted_burndown_tokens(  # noqa: PLR0913
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
    long_context_threshold: int | None = LONG_CONTEXT_THRESHOLD_TOKENS,
    *,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    uncached_weight: float = UNCACHED_INPUT_BURNDOWN_WEIGHT,
    cached_weight: float = CACHED_INPUT_BURNDOWN_WEIGHT,
    cache_write_5m_weight: float = CACHE_WRITE_5M_BURNDOWN_WEIGHT_1P,
    cache_write_1h_weight: float = CACHE_WRITE_1H_BURNDOWN_WEIGHT_1P,
    output_weight: float = OUTPUT_THINKING_BURNDOWN_WEIGHT,
    long_uncached_weight: float = LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT,
    long_cached_weight: float = LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT,
    long_output_weight: float = LONG_CONTEXT_OUTPUT_BURNDOWN_WEIGHT,
) -> float:
    """Compute span-level weighted burndown tokens with 1P `>200K` cliff and 3P cache-write weights.

    * Standard 1P (`<= 200,000` input): `1.0x` uncached + `0.1x` cached + `1.0x` writes + `4.0x` output
    * Long-context 1P (`> 200,000` input): `2.0x` uncached + `0.2x` cached + `7.5x` output
    * 3P multi-provider (e.g. Claude): `1.0x` uncached + `0.1x` cached + `1.25x` 5m write + `2.0x` 1h write + `5.0x` output
    """
    if (
        input_tokens < 0
        or cached_input_tokens < 0
        or cache_write_5m_tokens < 0
        or cache_write_1h_tokens < 0
        or output_tokens < 0
        or thinking_tokens < 0
    ):
        raise ValueError("Token counts must be non-negative.")
    if cached_input_tokens + cache_write_5m_tokens + cache_write_1h_tokens > input_tokens:
        raise ValueError(
            "cached_input_tokens + cache_write_5m_tokens + cache_write_1h_tokens cannot exceed input_tokens."
        )

    uncached_input = max(
        0, input_tokens - cached_input_tokens - cache_write_5m_tokens - cache_write_1h_tokens
    )
    if long_context_threshold is not None and input_tokens > long_context_threshold:
        return (
            long_uncached_weight * uncached_input
            + long_cached_weight * cached_input_tokens
            + cache_write_5m_weight * cache_write_5m_tokens
            + cache_write_1h_weight * cache_write_1h_tokens
            + long_output_weight * (output_tokens + thinking_tokens)
        )
    return (
        uncached_weight * uncached_input
        + cached_weight * cached_input_tokens
        + cache_write_5m_weight * cache_write_5m_tokens
        + cache_write_1h_weight * cache_write_1h_tokens
        + output_weight * (output_tokens + thinking_tokens)
    )


class OTelGenAISpanRecord(BaseModel):
    """Pydantic v2 contract for `agent_eval.otel_spans` rows (39-column dual-path & multi-cloud schema)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(..., min_length=8)
    span_id: str = Field(..., min_length=4)
    parent_span_id: str | None = None
    span_name: str = Field(default="invoke_agent")
    start_time: datetime
    end_time: datetime
    receive_time: datetime | None = None
    latency_ms: NonNegFloat
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
    serving_traffic_type: ServingTrafficType = Field(default=ServingTrafficType.STANDARD)
    request_type_header: str | None = None
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    session_id: str | None = None
    turn_index: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_tokens(self) -> OTelGenAISpanRecord:
        if (
            self.cached_input_tokens + self.cache_write_5m_tokens + self.cache_write_1h_tokens
            > self.input_tokens
        ):
            raise ValueError(
                "cached_input_tokens + cache_write_5m_tokens + cache_write_1h_tokens cannot exceed input_tokens."
            )
        return self

    @property
    def resolved_application_id(self) -> str:
        """Return portable application ID via COALESCE precedence."""
        return resolve_application_id(
            self.gcp_apphub_application_id,
            self.attributes,
        )

    @property
    def uncached_input_tokens(self) -> int:
        return max(
            0,
            self.input_tokens
            - self.cached_input_tokens
            - self.cache_write_5m_tokens
            - self.cache_write_1h_tokens,
        )

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
            cache_write_5m_tokens=self.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens,
        )


class TracePayloadRecord(BaseModel):
    """Prompt, retrieved context, and final response payload row (`agent_eval.trace_payloads`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(..., min_length=4)
    apphub_application_id: str
    ingested_at: datetime
    user_prompt: str
    retrieved_context: str
    final_response: str
    semantic_rubric_id: str = "groundedness_4pt_v1"
    expected_ordinal_score: int = Field(default=4, ge=1, le=4)


class BQAAAgentEvent(BaseModel):
    """Pydantic v2 contract for BigQuery Agent Analytics (`BQAA`) `agent_events` rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(..., min_length=4)
    timestamp: datetime
    event_type: str = Field(..., min_length=2)
    agent: str | None = None
    session_id: str | None = None
    invocation_id: str = Field(..., min_length=4)
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    status: str | None = "OK"
    error_message: str | None = None
    latency_ms: dict[str, Any] = Field(default_factory=dict)
    content: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)
