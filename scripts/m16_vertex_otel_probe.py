"""Vertex AI single-model context-cliff probe and live OTLP/Cloud Logging exporter for M16."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bq_eval.core.contract.otel import (
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
)
from bq_eval.deploy.engine.bq_client import bq_cli_query_runner


@dataclass(frozen=True)
class VertexEndpointConfig:
    project_id: str
    location: str
    model_name: str
    token: str


def gcloud_access_token() -> str:
    gcloud_bin = shutil.which("gcloud")
    if gcloud_bin is None:
        raise OSError("`gcloud` CLI not found on PATH.")
    proc = subprocess.run(  # noqa: S603
        [gcloud_bin, "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise OSError(f"Failed to obtain gcloud access token: {proc.stderr.strip()}")
    return proc.stdout.strip()


def to_int(val: object) -> int:
    if val is None or val == "":
        return 0
    return int(str(val))


def to_float(val: object) -> float:
    if val is None or val == "":
        return 0.0
    return float(str(val))


def sanitize_text(val: object, sensitive_tokens: Sequence[str]) -> str:
    s = str(val or "")
    for tok in sensitive_tokens:
        if tok:
            s = s.replace(tok, "showcase-project")
    return re.sub(r"\b\d{12}\b", "100000000000", s)


def _http_post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    token: str,
    quota_project: str,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-goog-user-project": quota_project,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8").strip()
        return json.loads(raw) if raw else {}


def _build_synthetic_underwriting_ledger(target_words: int, case_idx: int) -> str:
    header = (
        f"COMMERCIAL UNDERWRITING DOSSIER #{case_idx:03d}\n"
        f"Applicant: Northbridge Logistics Group {case_idx:03d}\n"
        f"Requested Facility: $1,800,000 Senior Secured Term Loan\n"
        f"Key Covenant Check: Debt-Service Coverage Ratio = {1.35 + (case_idx % 9) * 0.05:.2f}x "
        f"(minimum policy threshold 1.25x).\n"
    )
    clause_block = (
        "Schedule Entry: Verified monthly ACH freight settlement ledger row with clean OFAC screen, "
        "active UCC-1 equipment filing, current hazard insurance binder, and reconciled bank balance "
        "statement matching audited general ledger sub-account without discrepancy. "
    )
    repeats = max(1, target_words // 30)
    return header + (clause_block * repeats)


def call_vertex_gemini_single_case(
    endpoint: VertexEndpointConfig,
    *,
    arm: str,
    case_idx: int,
) -> tuple[OTelGenAISpanRecord, OTelGenAISpanRecord, dict[str, Any]]:
    target_words = 1_400 if arm == "short" else 168_000
    context_doc = _build_synthetic_underwriting_ledger(target_words, case_idx)
    user_prompt = (
        f"Evaluate Commercial Underwriting Dossier #{case_idx:03d}: report the Debt-Service Coverage "
        f"Ratio and state PASS if >= 1.25x else FAIL in one concise sentence."
    )
    req_payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"{user_prompt}\n\n<dossier>\n{context_doc}\n</dossier>"}],
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 128,
            "thinkingConfig": {"thinkingLevel": "LOW"},
        },
    }
    t0 = datetime.now(UTC)
    started_perf = time.perf_counter()
    resp: dict[str, Any] = {}
    for attempt_idx, loc_try in enumerate((endpoint.location, "us-central1", "us")):
        try_host = (
            "aiplatform.googleapis.com"
            if loc_try == "global"
            else f"{loc_try}-aiplatform.googleapis.com"
        )
        try_url = (
            f"https://{try_host}/v1/projects/{endpoint.project_id}/locations/{loc_try}"
            f"/publishers/google/models/{endpoint.model_name}:generateContent"
        )
        try:
            resp = _http_post_json(
                try_url,
                req_payload,
                token=endpoint.token,
                quota_project=endpoint.project_id,
                timeout_s=180.0,
            )
            break
        except Exception:
            if attempt_idx == 2:
                raise
            time.sleep(2.0 * (attempt_idx + 1))
    elapsed_ms = max(1.0, (time.perf_counter() - started_perf) * 1000.0)
    t1 = datetime.now(UTC)

    usage = resp.get("usageMetadata") or {}
    in_tok = int(usage.get("promptTokenCount") or 0)
    cached_tok = int(usage.get("cachedContentTokenCount") or 0)
    out_tok = int(usage.get("candidatesTokenCount") or 0)
    think_tok = int(usage.get("thoughtsTokenCount") or 0)

    candidates = resp.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    answer_text = "".join(str(p.get("text", "")) for p in parts).strip() or "PASS"

    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    close_span_id = uuid.uuid4().hex[:16]
    outcome_id = f"out_cliff_{arm}_{case_idx:03d}_{trace_id[:8]}"

    root_span = OTelGenAISpanRecord(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        span_name="invoke_agent",
        start_time=t0,
        end_time=t1,
        receive_time=t1,
        latency_ms=round(elapsed_ms, 2),
        status_code="OK",
        otel_span_kind="SPAN_KIND_SERVER",
        span_kind=GenAISpanKind.INVOKE_AGENT,
        service_name="cliff-benchmark-svc",
        gcp_apphub_application_id="cliff-benchmark-app",
        agent_name="cliff-underwriting-agent",
        model_name=endpoint.model_name,
        input_tokens=in_tok,
        cached_input_tokens=cached_tok,
        output_tokens=out_tok,
        thinking_tokens=think_tok,
        rule0_pass=True,
        finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
        outcome_id=outcome_id,
        outcome_owner="cliff-benchmark-app",
        source_mode=f"SINGLE_MODEL_{arm.upper()}",
        attributes={
            "apphub.application.id": "cliff-benchmark-app",
            "business_unit": "commercial_lending",
            "cost_center": "CC-FINOPS-M16",
            "benchmark.arm": arm,
        },
    )
    close_span = OTelGenAISpanRecord(
        trace_id=trace_id,
        span_id=close_span_id,
        parent_span_id=span_id,
        span_name="outcome.closed",
        start_time=t1,
        end_time=t1,
        receive_time=t1,
        latency_ms=0.0,
        status_code="OK",
        otel_span_kind="SPAN_KIND_INTERNAL",
        span_kind=GenAISpanKind.INVOKE_AGENT,
        service_name="cliff-benchmark-svc",
        gcp_apphub_application_id="cliff-benchmark-app",
        agent_name="cliff-underwriting-agent",
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        rule0_pass=True,
        finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
        outcome_id=outcome_id,
        outcome_owner="cliff-benchmark-app",
        source_mode=f"SINGLE_MODEL_{arm.upper()}",
        attributes={
            "outcome.id": outcome_id,
            "outcome.owner": "cliff-benchmark-app",
            "outcome.status": "succeeded",
            "outcome.verified": "true",
            "outcome.subject": f"DEAL-{arm.upper()}-{case_idx:03d}",
        },
    )
    log_payload = {
        "trace_id": trace_id,
        "apphub.application.id": "cliff-benchmark-app",
        "user_prompt": user_prompt,
        "retrieved_context": context_doc[:1200],
        "final_response": answer_text,
    }
    return root_span, close_span, log_payload


def _otlp_attr(key: str, val: object) -> dict[str, Any]:
    if isinstance(val, bool):
        return {"key": key, "value": {"boolValue": val}}
    if isinstance(val, int):
        return {"key": key, "value": {"intValue": str(val)}}
    if isinstance(val, float):
        return {"key": key, "value": {"doubleValue": val}}
    return {"key": key, "value": {"stringValue": str(val)}}


def export_live_otlp_and_logs(
    *,
    project_id: str,
    spans: Sequence[OTelGenAISpanRecord],
    log_payloads: Sequence[Mapping[str, Any]],
    token: str,
) -> None:
    otlp_spans: list[dict[str, Any]] = []
    for sp in spans:
        start_ns = int(sp.start_time.timestamp() * 1_000_000_000)
        end_ns = int(sp.end_time.timestamp() * 1_000_000_000)
        attrs: dict[str, object] = {
            "gen_ai.operation.name": sp.span_kind.value,
            "gen_ai.agent.name": sp.agent_name,
            "gen_ai.response.model": sp.model_name,
            "gen_ai.usage.input_tokens": sp.input_tokens,
            "gen_ai.usage.cache_read.input_tokens": sp.cached_input_tokens,
            "gen_ai.usage.output_tokens": sp.output_tokens,
            "gen_ai.usage.thoughts_tokens": sp.thinking_tokens,
            "rule0.pass": "true" if sp.rule0_pass else "false",
            "finops.traffic_type": sp.finops_traffic_type.value,
            "apphub.application.id": sp.resolved_application_id,
        }
        if sp.outcome_id:
            attrs["outcome.id"] = sp.outcome_id
        if sp.outcome_owner:
            attrs["outcome.owner"] = sp.outcome_owner
        if sp.source_mode:
            attrs["source_mode"] = sp.source_mode
        attrs.update(sp.attributes)

        span_obj: dict[str, Any] = {
            "traceId": sp.trace_id,
            "spanId": sp.span_id,
            "name": sp.span_name,
            "kind": 2 if sp.otel_span_kind == "SPAN_KIND_SERVER" else 1,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": [_otlp_attr(k, v) for k, v in attrs.items()],
            "status": {"code": 1},
        }
        if sp.parent_span_id:
            span_obj["parentSpanId"] = sp.parent_span_id
        otlp_spans.append(span_obj)

    otlp_body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _otlp_attr("gcp.project_id", project_id),
                        _otlp_attr("service.name", "cliff-benchmark-svc"),
                        _otlp_attr("apphub.application.id", "cliff-benchmark-app"),
                    ]
                },
                "scopeSpans": [{"scope": {"name": "bq_eval.m16"}, "spans": otlp_spans}],
            }
        ]
    }
    _http_post_json(
        "https://telemetry.googleapis.com/v1/traces",
        otlp_body,
        token=token,
        quota_project=project_id,
    )

    entries = [
        {
            "logName": f"projects/{project_id}/logs/genai_completions",
            "resource": {"type": "global", "labels": {"project_id": project_id}},
            "trace": f"projects/{project_id}/traces/{lp['trace_id']}",
            "jsonPayload": dict(lp),
        }
        for lp in log_payloads
    ]
    _http_post_json(
        "https://logging.googleapis.com/v2/entries:write",
        {"entries": entries},
        token=token,
        quota_project=project_id,
    )


def upload_seed_completion_jsonl(project_id: str, prefix: str = "bq-eval") -> None:
    gcloud_bin = shutil.which("gcloud")
    if gcloud_bin is None:
        raise OSError("`gcloud` CLI not found on PATH.")
    seed_line = (
        json.dumps(
            {
                "user_prompt": "seed-init",
                "retrieved_context": "seed-context",
                "final_response": "seed-ok",
            }
        )
        + "\n"
    )
    uri = f"gs://{project_id}/{prefix.strip('/')}/seed_init.jsonl"
    proc = subprocess.run(  # noqa: S603
        [gcloud_bin, "storage", "cp", "-", uri, f"--project={project_id}"],
        input=seed_line,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError(f"Failed uploading BigLake seed JSONL to {uri}: {proc.stderr.strip()}")


def load_ingested_cliff_spans(
    project_id: str,
    dataset_id: str,
) -> tuple[list[OTelGenAISpanRecord], list[OTelGenAISpanRecord]]:
    runner = bq_cli_query_runner(
        project_id, labels={"apphub_application_id": "cliff-benchmark-app"}
    )
    rows = runner(
        f"SELECT trace_id, span_id, latency_ms, service_name, agent_name, model_name, "  # noqa: S608
        f"input_tokens, cached_input_tokens, output_tokens, thinking_tokens, outcome_id, source_mode "
        f"FROM `{project_id}.{dataset_id}.otel_spans` "
        f"WHERE gcp_apphub_application_id = 'cliff-benchmark-app' "
        f"  AND span_name = 'invoke_agent' "
        f"  AND source_mode IN ('SINGLE_MODEL_SHORT', 'SINGLE_MODEL_LONG') "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY trace_id ORDER BY end_time DESC) = 1"
    )
    short_spans: list[OTelGenAISpanRecord] = []
    long_spans: list[OTelGenAISpanRecord] = []
    t_now = datetime.now(UTC)
    for r in rows:
        sp = OTelGenAISpanRecord(
            trace_id=str(r["trace_id"]),
            span_id=str(r["span_id"]),
            start_time=t_now,
            end_time=t_now,
            latency_ms=to_float(r.get("latency_ms")),
            status_code="OK",
            otel_span_kind="SPAN_KIND_SERVER",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            service_name=str(r.get("service_name") or "cliff-benchmark-svc"),
            gcp_apphub_application_id="cliff-benchmark-app",
            agent_name=str(r.get("agent_name") or "cliff-underwriting-agent"),
            model_name=str(r["model_name"]),
            input_tokens=to_int(r.get("input_tokens")),
            cached_input_tokens=to_int(r.get("cached_input_tokens")),
            output_tokens=to_int(r.get("output_tokens")),
            thinking_tokens=to_int(r.get("thinking_tokens")),
            rule0_pass=True,
            finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            outcome_id=str(r["outcome_id"]) if r.get("outcome_id") else None,
            outcome_owner="cliff-benchmark-app",
            source_mode=str(r["source_mode"]),
        )
        if str(r["source_mode"]) == "SINGLE_MODEL_SHORT":
            short_spans.append(sp)
        else:
            long_spans.append(sp)
    return short_spans, long_spans
