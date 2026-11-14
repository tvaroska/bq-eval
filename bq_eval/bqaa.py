"""Light Capture (`BQAA`) event contracts and deterministic Python adapter twin (`M5`).

Maps BigQuery Agent Analytics (`BigQueryAgentAnalyticsPlugin` / `BigQueryCallbackHandler`)
`agent_events` rows onto `OTelGenAISpanRecord` and `TracePayloadRecord` with exact parity
against `sql/bqaa_adapter_views.sql`:
- Deduplicates BigQuery Storage Write API retry rows by `event_id` (latest `timestamp` wins).
- Enforces `ADR-003` / `EVAL-P07` strict `'unregistered_app'` fallback when explicit
  `custom_metadata` App Hub identifiers are absent (never collapsing into `adk.app_name` or `agent`).
- Converts `usage_metadata` token counts for span-level `<=200K` and `>200K` burndown cliffs.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.loadgen import TracePayloadRecord
from bq_eval.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord

SPAN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "LLM_RESPONSE",
        "LLM_ERROR",
        "TOOL_COMPLETED",
        "TOOL_ERROR",
        "AGENT_COMPLETED",
        "AGENT_ERROR",
        "OUTCOME_CLOSED",
    }
)


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


def _first_non_empty(*candidates: object) -> str | None:
    for val in candidates:
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _resolve_strict_apphub_id(custom_metadata: dict[str, Any]) -> str | None:
    """Return explicit App Hub ID from `custom_metadata`, or `None` (`'unregistered_app'`)."""
    return _first_non_empty(
        custom_metadata.get("gcp.apphub.application.id"),
        custom_metadata.get("apphub.application.id"),
        custom_metadata.get("apphub_application_id"),
    )


def _extract_usage_tokens(attrs: dict[str, Any]) -> tuple[int, int, int, int]:
    usage_raw = attrs.get("usage_metadata")
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    input_tokens = max(0, int(usage.get("prompt_token_count", 0) or 0))
    cached_tokens = min(
        input_tokens,
        max(0, int(usage.get("cached_content_token_count", 0) or 0)),
    )
    output_tokens = max(0, int(usage.get("candidates_token_count", 0) or 0))
    thinking_tokens = max(0, int(usage.get("thoughts_token_count", 0) or 0))
    return input_tokens, cached_tokens, output_tokens, thinking_tokens


def _resolve_traffic_type(meta: dict[str, Any]) -> FinOpsTrafficType:
    raw_traffic = _first_non_empty(meta.get("finops.traffic_type"))
    if raw_traffic is None:
        return FinOpsTrafficType.PRODUCTION_INFERENCE
    try:
        return FinOpsTrafficType(raw_traffic)
    except ValueError:
        return FinOpsTrafficType.PRODUCTION_INFERENCE


def _bqaa_event_to_span(ev: BQAAAgentEvent) -> OTelGenAISpanRecord | None:
    attrs = ev.attributes
    custom_meta = attrs.get("custom_metadata")
    meta = custom_meta if isinstance(custom_meta, dict) else {}
    is_outcome_closed = (
        ev.event_type == "OUTCOME_CLOSED" or str(meta.get("outcome.closed", "")).lower() == "true"
    )
    if ev.event_type not in SPAN_EVENT_TYPES and not is_outcome_closed:
        return None

    otel_ctx = attrs.get("otel")
    otel_map = otel_ctx if isinstance(otel_ctx, dict) else {}
    resolved_trace_id = (
        _first_non_empty(otel_map.get("trace_id"), ev.trace_id, ev.invocation_id)
        or ev.invocation_id
    )
    resolved_span_id = (
        _first_non_empty(otel_map.get("span_id"), ev.span_id, ev.event_id) or ev.event_id
    )

    raw_lat = ev.latency_ms.get("total_ms", 0.0)
    total_ms = max(0.0, float(raw_lat)) if isinstance(raw_lat, (int, float)) else 0.0
    is_error = (ev.status or "OK").upper() == "ERROR" or ev.event_type.endswith("_ERROR")
    if ev.event_type in ("LLM_RESPONSE", "LLM_ERROR"):
        span_kind = GenAISpanKind.CHAT
    elif ev.event_type in ("TOOL_COMPLETED", "TOOL_ERROR"):
        span_kind = GenAISpanKind.EXECUTE_TOOL
    else:
        span_kind = GenAISpanKind.INVOKE_AGENT

    adk_block = attrs.get("adk")
    adk_map = adk_block if isinstance(adk_block, dict) else {}
    service_name = (
        _first_non_empty(adk_map.get("app_name"), attrs.get("root_agent_name"), ev.agent)
        or "unknown_service"
    )
    apphub_app_id = _resolve_strict_apphub_id(meta)
    input_tokens, cached_tokens, output_tokens, thinking_tokens = _extract_usage_tokens(attrs)
    tool_status: str | None = (
        "VALIDATION_ERROR"
        if ev.event_type == "TOOL_ERROR"
        else ("SUCCESS" if ev.event_type == "TOOL_COMPLETED" else None)
    )
    promoted_attrs: dict[str, Any] = dict(attrs)
    for pass_key in (
        "business_unit",
        "cost_center",
        "outcome.status",
        "outcome.verified",
        "outcome.subject",
        "outcome.parent_id",
    ):
        if pass_key in meta and pass_key not in promoted_attrs:
            promoted_attrs[pass_key] = meta[pass_key]

    return OTelGenAISpanRecord(
        trace_id=resolved_trace_id,
        span_id=resolved_span_id,
        parent_span_id=ev.parent_span_id,
        span_name="outcome.closed" if is_outcome_closed else ev.event_type,
        start_time=ev.timestamp - timedelta(milliseconds=int(total_ms)),
        end_time=ev.timestamp,
        receive_time=ev.timestamp,
        latency_ms=total_ms,
        status_code="ERROR" if is_error else "OK",
        otel_span_kind="SPAN_KIND_INTERNAL",
        span_kind=span_kind,
        service_name=service_name,
        gcp_apphub_application_id=apphub_app_id,
        gcp_apphub_service_id=_first_non_empty(meta.get("apphub.service.id")),
        gcp_apphub_workload_id=_first_non_empty(meta.get("apphub.workload.id")),
        agent_name=_first_non_empty(ev.agent) or "unknown_agent",
        model_name=_first_non_empty(attrs.get("model_version"), attrs.get("model")) or "none",
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        tool_name=_first_non_empty(
            attrs.get("tool_name"), ev.content.get("tool_name"), ev.content.get("name")
        ),
        tool_call_status=tool_status,
        rule0_pass=not is_error,
        finops_traffic_type=_resolve_traffic_type(meta),
        outcome_id=_first_non_empty(meta.get("outcome.id"), ev.trace_id, ev.invocation_id),
        outcome_owner=_first_non_empty(meta.get("outcome.owner"), apphub_app_id),
        source_mode=_first_non_empty(meta.get("source_mode")),
        skill_id=_first_non_empty(meta.get("skill_id")),
        skill_version=_first_non_empty(meta.get("skill_version")),
        entity_id=_first_non_empty(meta.get("entity_id"), ev.session_id),
        attributes=promoted_attrs,
    )


def deduplicate_bqaa_events(events: Sequence[BQAAAgentEvent]) -> list[BQAAAgentEvent]:
    """Deduplicate Storage Write API retry rows by `event_id`, keeping latest `timestamp`."""
    by_id: dict[str, BQAAAgentEvent] = {}
    for event in events:
        existing = by_id.get(event.event_id)
        if existing is None or event.timestamp >= existing.timestamp:
            by_id[event.event_id] = event
    return sorted(by_id.values(), key=lambda e: (e.timestamp, e.event_id))


def bqaa_events_to_otel_spans(
    events: Sequence[BQAAAgentEvent],
) -> list[OTelGenAISpanRecord]:
    """Convert `BQAA` `agent_events` rows into `OTelGenAISpanRecord` rows (`v_bqaa_otel_spans`)."""
    spans: list[OTelGenAISpanRecord] = []
    for ev in deduplicate_bqaa_events(events):
        span = _bqaa_event_to_span(ev)
        if span is not None:
            spans.append(span)
    return spans


def _extract_content_text(content: dict[str, Any], preferred_key: str) -> str:
    val = content.get(preferred_key)
    if isinstance(val, str):
        return val
    if not content:
        return ""
    return json.dumps(content, sort_keys=True)


def _aggregate_trace_payload(
    tid: str, trace_events: Sequence[BQAAAgentEvent]
) -> TracePayloadRecord:
    app_id = "unregistered_app"
    prompts: list[str] = []
    contexts: list[str] = []
    final_resp = ""
    ingested_at = trace_events[0].timestamp

    for ev in trace_events:
        ingested_at = max(ingested_at, ev.timestamp)
        custom_meta = ev.attributes.get("custom_metadata")
        explicit_app = (
            _resolve_strict_apphub_id(custom_meta) if isinstance(custom_meta, dict) else None
        )
        if explicit_app is not None and app_id == "unregistered_app":
            app_id = explicit_app
        if ev.event_type == "USER_MESSAGE_RECEIVED":
            text = _extract_content_text(ev.content, "text")
            if text:
                prompts.append(text)
        elif ev.event_type == "TOOL_COMPLETED":
            text = _extract_content_text(ev.content, "response")
            if text:
                contexts.append(text)
        elif ev.event_type == "AGENT_RESPONSE":
            text = _extract_content_text(ev.content, "text")
            if text:
                final_resp = text

    return TracePayloadRecord(
        trace_id=tid,
        apphub_application_id=app_id,
        ingested_at=ingested_at,
        user_prompt="\n".join(prompts),
        retrieved_context="\n".join(contexts),
        final_response=final_resp,
    )


def bqaa_events_to_trace_payloads(
    events: Sequence[BQAAAgentEvent],
) -> list[TracePayloadRecord]:
    """Aggregate `BQAA` `agent_events` into 1 `TracePayloadRecord` per `trace_id`."""
    grouped: dict[str, list[BQAAAgentEvent]] = {}
    for ev in deduplicate_bqaa_events(events):
        otel_ctx = ev.attributes.get("otel")
        otel_map = otel_ctx if isinstance(otel_ctx, dict) else {}
        tid = (
            _first_non_empty(otel_map.get("trace_id"), ev.trace_id, ev.invocation_id)
            or ev.invocation_id
        )
        grouped.setdefault(tid, []).append(ev)

    return [
        _aggregate_trace_payload(tid, trace_events) for tid, trace_events in sorted(grouped.items())
    ]
