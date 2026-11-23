"""In-memory `RingBufferSpanExporter` and W3C trace context parity with `agent-evals` (`M11-4`)."""

from __future__ import annotations

import re
import secrets
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from bq_eval.contract.otel import (
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
    ServingTrafficType,
)

_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_XCTC_RE = re.compile(
    r"^(?P<trace_id>[0-9a-f]{32})(?:/(?P<span_dec>\d+))?(?:;o=(?P<sampled>[01]))?$"
)


def parse_trace_context_headers(headers: Mapping[str, str]) -> tuple[str, str | None, bool]:
    """Parse W3C `traceparent` or `X-Cloud-Trace-Context` into `(trace_id, parent_span_id, sampled)`."""
    normalized = {k.lower(): v.strip() for k, v in headers.items() if v}

    if "traceparent" in normalized:
        match = _TRACEPARENT_RE.match(normalized["traceparent"].lower())
        if match:
            trace_id = match.group("trace_id")
            span_id = match.group("span_id")
            sampled = (int(match.group("flags"), 16) & 1) == 1
            if trace_id != "0" * 32 and span_id != "0" * 16:
                return trace_id, span_id, sampled

    if "x-cloud-trace-context" in normalized:
        match = _XCTC_RE.match(normalized["x-cloud-trace-context"])
        if match:
            trace_id = match.group("trace_id").lower()
            span_dec = match.group("span_dec")
            sampled_flag = match.group("sampled")
            span_id = f"{int(span_dec):016x}" if span_dec else None
            sampled = sampled_flag != "0"
            if trace_id != "0" * 32:
                return trace_id, span_id, sampled

    return secrets.token_hex(16), None, True


def format_x_cloud_trace_context(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """Format `(trace_id, span_id)` into `X-Cloud-Trace-Context: TRACE_ID/SPAN_DEC;o=1`."""
    span_dec = int(span_id, 16)
    flag = "1" if sampled else "0"
    return f"{trace_id}/{span_dec};o={flag}"


class RingBufferSpanExporter:
    """Thread-safe bounded ring buffer emitting native `OTelGenAISpanRecord` rows (`<2s` CI parity)."""

    def __init__(self, maxlen: int = 2000) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1.")
        self._buffer: deque[OTelGenAISpanRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def export_span(self, span: OTelGenAISpanRecord) -> None:
        with self._lock:
            self._buffer.append(span)

    def get_spans(self, trace_id: str | None = None) -> list[OTelGenAISpanRecord]:
        with self._lock:
            spans = list(self._buffer)
        if trace_id is not None:
            return [s for s in spans if s.trace_id == trace_id]
        return spans

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)


class GenAISpanRecorder:
    """Lightweight context-manager span recorder populating `RingBufferSpanExporter`."""

    def __init__(
        self,
        exporter: RingBufferSpanExporter,
        service_name: str = "agent-service",
        apphub_application_id: str | None = None,
    ) -> None:
        self._exporter = exporter
        self._service_name = service_name
        self._apphub_application_id = apphub_application_id

    @contextmanager
    def start_span(  # noqa: PLR0913
        self,
        name: str,
        *,
        span_kind: GenAISpanKind = GenAISpanKind.INVOKE_AGENT,
        agent_name: str = "primary-agent",
        model_name: str = "gemini-3.8-flash",
        finops_traffic_type: FinOpsTrafficType = FinOpsTrafficType.PRODUCTION_INFERENCE,
        serving_traffic_type: ServingTrafficType = ServingTrafficType.STANDARD,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        session_id: str | None = None,
        turn_index: int | None = None,
        outcome_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        resolved_trace_id = trace_id or secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        start_dt = datetime.now(UTC)
        state: dict[str, Any] = {
            "trace_id": resolved_trace_id,
            "span_id": span_id,
            "status_code": "OK",
            "http_status_code": 200,
            "rule0_pass": True,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "tool_name": None,
            "tool_call_status": None,
            "attributes": {},
        }
        try:
            yield state
        except Exception:
            state["status_code"] = "ERROR"
            state["http_status_code"] = 500
            state["rule0_pass"] = False
            raise
        finally:
            end_dt = datetime.now(UTC)
            latency_ms = max(0.001, (end_dt - start_dt).total_seconds() * 1000.0)
            record = OTelGenAISpanRecord(
                trace_id=resolved_trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                span_name=name,
                start_time=start_dt,
                end_time=end_dt,
                receive_time=end_dt,
                latency_ms=latency_ms,
                status_code=str(state["status_code"]),
                span_kind=span_kind,
                service_name=self._service_name,
                gcp_apphub_application_id=self._apphub_application_id,
                agent_name=agent_name,
                model_name=model_name,
                input_tokens=int(state["input_tokens"]),
                cached_input_tokens=int(state["cached_input_tokens"]),
                cache_write_5m_tokens=int(state["cache_write_5m_tokens"]),
                cache_write_1h_tokens=int(state["cache_write_1h_tokens"]),
                output_tokens=int(state["output_tokens"]),
                thinking_tokens=int(state["thinking_tokens"]),
                tool_name=state["tool_name"],
                tool_call_status=state["tool_call_status"],
                rule0_pass=bool(state["rule0_pass"]),
                finops_traffic_type=finops_traffic_type,
                serving_traffic_type=serving_traffic_type,
                http_status_code=int(state["http_status_code"]),
                outcome_id=outcome_id,
                outcome_owner=self._apphub_application_id,
                session_id=session_id,
                turn_index=turn_index,
                attributes=dict(state["attributes"]),
            )
            self._exporter.export_span(record)
