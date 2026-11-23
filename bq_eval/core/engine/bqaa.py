"""Light Capture (`BQAA`) deterministic Python adapter twin."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from bq_eval.core.contract.otel import (
    BQAAAgentEvent,
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
    TracePayloadRecord,
    normalize_serving_traffic_type,
)

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

_TIMEOUT_RE = re.compile(r"timeout|timed\s*out|503|504", re.IGNORECASE)
_RATE_LIMIT_RE = re.compile(r"rate[_\s-]?limit|429", re.IGNORECASE)
_VALIDATION_RE = re.compile(r"validat|schema|invalid", re.IGNORECASE)


def _first_non_empty(*candidates: object) -> str | None:
    for val in candidates:
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _parse_optional_int(raw: object) -> int | None:
    """Parse int, whole float, or numeric string matching SQL `SAFE_CAST(... AS INT64)`."""
    result: int | None = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        result = raw
    elif isinstance(raw, float) and raw.is_integer():
        result = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                result = int(text)
            except ValueError:
                result = None
    return result


def resolve_bqaa_trace_id(ev: BQAAAgentEvent) -> str:
    """Resolve canonical trace ID (`attributes.otel.trace_id` -> `trace_id` -> `invocation_id`)."""
    otel_ctx = ev.attributes.get("otel")
    otel_map = otel_ctx if isinstance(otel_ctx, dict) else {}
    return (
        _first_non_empty(otel_map.get("trace_id"), ev.trace_id, ev.invocation_id)
        or ev.invocation_id
    )


def _resolve_strict_apphub_id(custom_metadata: dict[str, Any]) -> str | None:
    """Return explicit App Hub ID from `custom_metadata`, or `None` (`'unregistered_app'`)."""
    return _first_non_empty(
        custom_metadata.get("gcp.apphub.application.id"),
        custom_metadata.get("apphub.application.id"),
        custom_metadata.get("apphub_application_id"),
    )


def _extract_usage_tokens(attrs: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    usage_raw = attrs.get("usage_metadata")
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    input_tokens = max(0, int(usage.get("prompt_token_count", 0) or 0))
    cached_tokens = min(
        input_tokens,
        max(0, int(usage.get("cached_content_token_count", 0) or 0)),
    )
    rem = max(0, input_tokens - cached_tokens)
    cw5_raw = usage.get("cache_write_5m_tokens", usage.get("ephemeral_5m_input_tokens", 0))
    cache_write_5m = min(rem, max(0, int(cw5_raw or 0)))
    rem -= cache_write_5m
    cw1_raw = usage.get("cache_write_1h_tokens", usage.get("ephemeral_1h_input_tokens", 0))
    cache_write_1h = min(rem, max(0, int(cw1_raw or 0)))
    output_tokens = max(0, int(usage.get("candidates_token_count", 0) or 0))
    thinking_tokens = max(0, int(usage.get("thoughts_token_count", 0) or 0))
    return (
        input_tokens,
        cached_tokens,
        output_tokens,
        thinking_tokens,
        cache_write_5m,
        cache_write_1h,
    )


def _resolve_traffic_type(meta: dict[str, Any]) -> FinOpsTrafficType:
    raw_traffic = _first_non_empty(meta.get("finops.traffic_type"))
    if raw_traffic is None:
        return FinOpsTrafficType.PRODUCTION_INFERENCE
    try:
        return FinOpsTrafficType(raw_traffic)
    except ValueError:
        return FinOpsTrafficType.UNCLASSIFIED


def _resolve_tool_call_status(ev: BQAAAgentEvent) -> str | None:
    """Classify `TOOL_COMPLETED` / `TOOL_ERROR` matching `sql/core/bqaa_adapter_views.sql`."""
    if ev.event_type == "TOOL_COMPLETED":
        return "SUCCESS"
    if ev.event_type != "TOOL_ERROR":
        return None

    explicit = _first_non_empty(
        ev.attributes.get("tool.call.status"),
        ev.attributes.get("error.type"),
        ev.attributes.get("error_type"),
        ev.content.get("error_type"),
    )
    if explicit is not None and explicit.upper() not in {"OK", "SUCCESS"}:
        return explicit.upper()

    raw_code = ev.content.get("code")
    code_int = _parse_optional_int(raw_code)
    msg = ev.error_message or ""
    status = "TOOL_ERROR"
    if code_int in {408, 503, 504} or _TIMEOUT_RE.search(msg):
        status = "TIMEOUT"
    elif code_int == 429 or _RATE_LIMIT_RE.search(msg):
        status = "RATE_LIMITED"
    elif code_int in {400, 422} or _VALIDATION_RE.search(msg):
        status = "VALIDATION_ERROR"
    return status


def _resolve_span_kind(ev: BQAAAgentEvent) -> GenAISpanKind:
    if ev.event_type in ("LLM_RESPONSE", "LLM_ERROR"):
        return GenAISpanKind.CHAT
    if ev.event_type in ("TOOL_COMPLETED", "TOOL_ERROR"):
        return GenAISpanKind.EXECUTE_TOOL
    return GenAISpanKind.INVOKE_AGENT


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
    resolved_trace_id = resolve_bqaa_trace_id(ev)
    resolved_span_id = (
        _first_non_empty(otel_map.get("span_id"), ev.span_id, ev.event_id) or ev.event_id
    )

    raw_lat = ev.latency_ms.get("total_ms", 0.0)
    total_ms = max(0.0, float(raw_lat)) if isinstance(raw_lat, (int, float)) else 0.0
    is_error = (ev.status or "OK").upper() == "ERROR" or ev.event_type.endswith("_ERROR")

    adk_block = attrs.get("adk")
    adk_map = adk_block if isinstance(adk_block, dict) else {}
    service_name = (
        _first_non_empty(adk_map.get("app_name"), attrs.get("root_agent_name"), ev.agent)
        or "unknown_service"
    )
    apphub_app_id = _resolve_strict_apphub_id(meta)
    (
        input_tokens,
        cached_tokens,
        output_tokens,
        thinking_tokens,
        cache_write_5m,
        cache_write_1h,
    ) = _extract_usage_tokens(attrs)
    usage_raw = attrs.get("usage_metadata")
    usage_map = usage_raw if isinstance(usage_raw, dict) else {}
    serving_tier = normalize_serving_traffic_type(
        usage_map.get("traffic_type") or meta.get("serving.traffic_type")
    )
    req_hdr = _first_non_empty(
        meta.get("x-vertex-ai-llm-request-type"),
        meta.get("request_type_header"),
    )
    raw_http = ev.content.get("code", attrs.get("http.response.status_code"))
    parsed_http = _parse_optional_int(raw_http)
    http_code = (
        parsed_http
        if parsed_http is not None and 100 <= parsed_http <= 599
        else (500 if is_error else 200)
    )
    raw_turn = meta.get("interaction.turn_index", attrs.get("turn_index"))
    turn_idx = (
        int(raw_turn)
        if isinstance(raw_turn, (int, float))
        and not isinstance(raw_turn, bool)
        and int(raw_turn) >= 0
        else None
    )
    promoted_attrs: dict[str, Any] = dict(attrs)
    for pass_key in (
        "business_unit",
        "cost_center",
        "outcome.status",
        "outcome.verified",
        "outcome.subject",
        "outcome.parent_id",
        "hitl.review",
        "apphub.external",
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
        span_kind=_resolve_span_kind(ev),
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
        tool_call_status=_resolve_tool_call_status(ev),
        rule0_pass=not is_error,
        finops_traffic_type=_resolve_traffic_type(meta),
        outcome_id=_first_non_empty(meta.get("outcome.id")),
        outcome_owner=_first_non_empty(meta.get("outcome.owner"), apphub_app_id),
        source_mode=_first_non_empty(meta.get("source_mode")),
        skill_id=_first_non_empty(meta.get("skill_id")),
        skill_version=_first_non_empty(meta.get("skill_version")),
        entity_id=_first_non_empty(meta.get("entity_id"), ev.session_id),
        serving_traffic_type=serving_tier,
        request_type_header=req_hdr,
        http_status_code=http_code,
        cache_write_5m_tokens=cache_write_5m,
        cache_write_1h_tokens=cache_write_1h,
        session_id=_first_non_empty(meta.get("interaction.session_id"), ev.session_id),
        turn_index=turn_idx,
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
        grouped.setdefault(resolve_bqaa_trace_id(ev), []).append(ev)
    return [
        _aggregate_trace_payload(tid, trace_events) for tid, trace_events in sorted(grouped.items())
    ]
