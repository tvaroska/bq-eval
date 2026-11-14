"""Capture-Once, Time-Shift & Multiply Demo Load Generator (`bq_eval.loadgen`).

Provides:
1. A Golden Captured Trace Corpus (`build_golden_seed_templates()`) spanning 4 enterprise
   agent workloads across ADK, LangGraph, FastMCP, Agent Engine, and Cloud Run:
   - `docubridge-underwriting` (`commercial_banking` / `CC-4100-CRE`): High-cost multimodal
     PDF extraction (`>200K` context cliff + `gemini-4-argon` conflict escalation).
   - `quoting-concierge-adk` (`personal_lines` / `CC-2100-PERS`): Google ADK on Agent Engine
     + FastMCP, including an ungrounded discount hallucination for Step 2 `groundedness_4pt_v1`.
   - `claims-triage-langgraph` (`claims_ops` / `CC-3200-CLM`): LangGraph on Cloud Run + FastMCP,
     including FNOL, attorney escalation, E&O backdate trap (`escalation_policy_4pt_v1`), and
     an MCP 503 timeout caught at $0 by Step 1 SQL.
   - `renewal-auditor-svc` (`gcp_apphub_application_id = None`): Unregistered Cloud Run agent
     proving Step 1 `COALESCE` fallback (`unassigned_bu`) and `>8` tool-call loop detection.
2. `generate_time_shifted_corpus()`: Shifts and multiplies the golden templates into the
   current day and the last 1 hour while preserving parent-child span waterfalls.
3. `evaluate_time_shifted_corpus()`: Runs Step 1 Cost per Application/BU/Outcome and Step 2/2.5
   slice counts (`100%`, `10%`, `5%`, `last 1 hour`) over the time-shifted traces.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from bq_eval.loadgen_seeds import (
    GoldenSpanTemplate,
    GoldenTraceTemplate,
    build_golden_seed_templates,
)
from bq_eval.otel import GenAISpanKind, OTelGenAISpanRecord
from bq_eval.sql_eval import DEFAULT_SKU_RATES

DEFAULT_SEED_DATASET_ID: str = "agent_eval_seed"

__all__ = [
    "DEFAULT_SEED_DATASET_ID",
    "GoldenSpanTemplate",
    "GoldenTraceTemplate",
    "Step1CostOutcomeRow",
    "TimeShiftLoadgenReport",
    "TracePayloadRecord",
    "build_golden_seed_templates",
    "evaluate_time_shifted_corpus",
    "export_time_shifted_jsonl",
    "generate_time_shifted_corpus",
    "render_loadgen_markdown_report",
]


class TracePayloadRecord(BaseModel):
    """Time-shifted prompt, retrieved context, and final response payload row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str
    apphub_application_id: str
    ingested_at: datetime
    user_prompt: str
    retrieved_context: str
    final_response: str
    semantic_rubric_id: str = "groundedness_4pt_v1"
    expected_ordinal_score: int = 4


class Step1CostOutcomeRow(BaseModel):
    """Step 1 Cost per Application, Business Unit & Outcome summary row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apphub_application_id: str
    business_unit: str
    cost_center: str
    framework_runtime: str
    total_traces: int
    last_1h_traces: int
    step1_passing_traces: int
    step1_pass_rate: float
    tool_success_rate: float
    p95_latency_ms: float
    avg_cache_hit_ratio: float
    total_cost_usd: float
    cost_per_trace_usd: float
    step1_cost_per_outcome_usd: float | None


class TimeShiftLoadgenReport(BaseModel):
    """Summary of a time-shifted demo load generation run across Step 1, Step 2, and Step 2.5."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_time: datetime
    total_replicas: int
    last_hour_replicas: int
    total_traces: int
    total_spans: int
    last_1h_traces: int
    sample_10pct_traces: int
    sample_5pct_traces: int
    last_1h_and_5pct_traces: int
    step1_rows: tuple[Step1CostOutcomeRow, ...]


def _deterministic_bucket_100(trace_id: str) -> int:
    """Deterministic [0..99] hash bucket mirroring `MOD(ABS(FARM_FINGERPRINT(trace_id)), 100)`."""
    digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return val % 100


def generate_time_shifted_corpus(
    reference_time: datetime | None = None,
    total_replicas: int = 50,
    last_hour_replicas: int = 15,
) -> tuple[list[OTelGenAISpanRecord], list[TracePayloadRecord], datetime]:
    """Time-shift and multiply the 16 golden trace templates into `reference_time`.

    - Replicas `1..last_hour_replicas` are placed inside the LAST 1 HOUR
      (`reference_time - 3..55 minutes`), so Step 2.5 (`INTERVAL 1 HOUR`) always has
      fresh multi-agent traffic.
    - Replicas `(last_hour_replicas + 1)..total_replicas` are spread across the
      prior 23 hours (`reference_time - 1h..23h`), preserving exact child-span
      millisecond waterfalls (`span_offset_ms`).
    """
    if total_replicas < 1:
        raise ValueError("total_replicas must be >= 1.")
    if not (0 <= last_hour_replicas <= total_replicas):
        raise ValueError("last_hour_replicas must be between 0 and total_replicas.")

    ref_time = reference_time or datetime.now(UTC)
    date_tag = ref_time.strftime("%Y%m%d")
    templates = build_golden_seed_templates()

    spans: list[OTelGenAISpanRecord] = []
    payloads: list[TracePayloadRecord] = []

    for replica_id in range(1, total_replicas + 1):
        if replica_id <= last_hour_replicas:
            base_age_minutes = replica_id * 3
        else:
            base_age_minutes = 60 + ((replica_id - last_hour_replicas) * 35)

        for tmpl in templates:
            trace_id = f"{tmpl.base_trace_id}-r{replica_id:03d}-{date_tag}"
            trace_base_start = ref_time - timedelta(
                seconds=(base_age_minutes * 60) + (tmpl.template_idx * 7)
            )
            payloads.append(
                TracePayloadRecord(
                    trace_id=trace_id,
                    apphub_application_id=tmpl.apphub_application_id,
                    ingested_at=trace_base_start,
                    user_prompt=tmpl.user_prompt,
                    retrieved_context=tmpl.retrieved_context,
                    final_response=tmpl.final_response,
                    semantic_rubric_id=tmpl.semantic_rubric_id,
                    expected_ordinal_score=tmpl.expected_ordinal_score,
                )
            )

            for s_tmpl in tmpl.spans:
                span_id = (
                    f"span-{tmpl.base_trace_id}-{s_tmpl.span_suffix}-r{replica_id:03d}-{date_tag}"
                )
                parent_id = (
                    f"span-{tmpl.base_trace_id}-{s_tmpl.parent_span_suffix}-r{replica_id:03d}-{date_tag}"
                    if s_tmpl.parent_span_suffix is not None
                    else None
                )
                s_start = trace_base_start + timedelta(milliseconds=s_tmpl.span_offset_ms)
                s_end = s_start + timedelta(milliseconds=s_tmpl.latency_ms)
                spans.append(
                    OTelGenAISpanRecord(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent_id,
                        start_time=s_start,
                        end_time=s_end,
                        latency_ms=s_tmpl.latency_ms,
                        status_code=s_tmpl.status_code,
                        span_kind=s_tmpl.span_kind,
                        service_name=s_tmpl.service_name,
                        gcp_apphub_application_id=s_tmpl.gcp_apphub_application_id,
                        agent_name=s_tmpl.agent_name,
                        model_name=s_tmpl.model_name,
                        input_tokens=s_tmpl.input_tokens,
                        cached_input_tokens=s_tmpl.cached_input_tokens,
                        output_tokens=s_tmpl.output_tokens,
                        thinking_tokens=s_tmpl.thinking_tokens,
                        tool_name=s_tmpl.tool_name,
                        tool_call_status=s_tmpl.tool_call_status,
                        rule0_pass=s_tmpl.rule0_pass,
                        finops_traffic_type=s_tmpl.finops_traffic_type,
                        attributes=dict(s_tmpl.attributes),
                    )
                )

    return spans, payloads, ref_time


def _compute_span_cost_usd(span: OTelGenAISpanRecord) -> float:
    rates = DEFAULT_SKU_RATES.get(span.model_name, DEFAULT_SKU_RATES["gemini-3.8-flash"])
    uncached = span.uncached_input_tokens
    if span.is_long_context_cliff:
        return (
            (uncached / 1e6) * rates[3]
            + (span.cached_input_tokens / 1e6) * rates[4]
            + ((span.output_tokens + span.thinking_tokens) / 1e6) * rates[5]
        )
    return (
        (uncached / 1e6) * rates[0]
        + (span.cached_input_tokens / 1e6) * rates[1]
        + ((span.output_tokens + span.thinking_tokens) / 1e6) * rates[2]
    )


def evaluate_time_shifted_corpus(
    spans: Sequence[OTelGenAISpanRecord],
    payloads: Sequence[TracePayloadRecord],
    reference_time: datetime,
    total_replicas: int = 50,
    last_hour_replicas: int = 15,
) -> TimeShiftLoadgenReport:
    """Evaluate Step 1 Cost per App/BU/Outcome and Step 2/2.5 slices on time-shifted traces."""
    one_hour_ago = reference_time - timedelta(hours=1)

    # Group spans by trace_id
    traces_by_id: dict[str, list[OTelGenAISpanRecord]] = {}
    for s in spans:
        traces_by_id.setdefault(s.trace_id, []).append(s)

    # Group trace rollups by (apphub_application_id, business_unit, cost_center)
    app_groups: dict[tuple[str, str, str, str], list[dict[str, float | int | bool]]] = {}
    last_1h_count = 0
    sample_10pct_count = 0
    sample_5pct_count = 0
    last_1h_and_5pct_count = 0

    for trace_id, t_spans in traces_by_id.items():
        root = t_spans[0]
        app_id = root.resolved_application_id
        bu = str(root.attributes.get("business_unit", "unassigned_bu"))
        cc = str(root.attributes.get("cost_center", "UNASSIGNED"))
        fw = f"{root.attributes.get('framework', 'unknown')} ({root.attributes.get('runtime', 'unknown')})"

        trace_start = min(s.start_time for s in t_spans)
        trace_end = max(s.end_time for s in t_spans)
        trace_latency_ms = (trace_end - trace_start).total_seconds() * 1000.0

        tool_calls = sum(1 for s in t_spans if s.span_kind == GenAISpanKind.EXECUTE_TOOL)
        tool_errors = sum(
            1
            for s in t_spans
            if s.span_kind == GenAISpanKind.EXECUTE_TOOL
            and (s.status_code == "ERROR" or (s.tool_call_status or "SUCCESS") != "SUCCESS")
        )
        step1_pass = (
            all(s.rule0_pass for s in t_spans)
            and all(s.status_code != "ERROR" for s in t_spans)
            and tool_errors == 0
            and tool_calls <= 8
        )
        total_in = sum(s.input_tokens for s in t_spans)
        total_cached = sum(s.cached_input_tokens for s in t_spans)
        cache_ratio = (total_cached / total_in) if total_in > 0 else 0.0
        trace_cost = sum(_compute_span_cost_usd(s) for s in t_spans)

        in_last_1h = trace_start >= one_hour_ago
        bucket = _deterministic_bucket_100(trace_id)
        in_10pct = bucket < 10
        in_5pct = bucket < 5

        if in_last_1h:
            last_1h_count += 1
        if in_10pct:
            sample_10pct_count += 1
        if in_5pct:
            sample_5pct_count += 1
        if in_last_1h and in_5pct:
            last_1h_and_5pct_count += 1

        app_groups.setdefault((app_id, bu, cc, fw), []).append(
            {
                "in_last_1h": in_last_1h,
                "step1_pass": step1_pass,
                "tool_ok": tool_errors == 0,
                "latency_ms": trace_latency_ms,
                "cache_ratio": cache_ratio,
                "cost_usd": trace_cost,
            }
        )

    step1_rows: list[Step1CostOutcomeRow] = []
    for (app_id, bu, cc, fw), t_list in sorted(
        app_groups.items(),
        key=lambda item: sum(float(x["cost_usd"]) for x in item[1]),
        reverse=True,
    ):
        n_traces = len(t_list)
        n_last_1h = sum(1 for x in t_list if x["in_last_1h"])
        n_pass = sum(1 for x in t_list if x["step1_pass"])
        pass_rate = n_pass / n_traces
        tool_ok_rate = sum(1 for x in t_list if x["tool_ok"]) / n_traces
        latencies = sorted(float(x["latency_ms"]) for x in t_list)
        p95_idx = min(len(latencies) - 1, max(0, math.ceil(0.95 * len(latencies)) - 1))
        p95_ms = latencies[p95_idx]
        avg_cache = sum(float(x["cache_ratio"]) for x in t_list) / n_traces
        tot_cost = sum(float(x["cost_usd"]) for x in t_list)
        cost_per_trace = tot_cost / n_traces
        cost_per_outcome = (tot_cost / n_pass) if n_pass > 0 else None

        step1_rows.append(
            Step1CostOutcomeRow(
                apphub_application_id=app_id,
                business_unit=bu,
                cost_center=cc,
                framework_runtime=fw,
                total_traces=n_traces,
                last_1h_traces=n_last_1h,
                step1_passing_traces=n_pass,
                step1_pass_rate=pass_rate,
                tool_success_rate=tool_ok_rate,
                p95_latency_ms=p95_ms,
                avg_cache_hit_ratio=avg_cache,
                total_cost_usd=tot_cost,
                cost_per_trace_usd=cost_per_trace,
                step1_cost_per_outcome_usd=cost_per_outcome,
            )
        )

    return TimeShiftLoadgenReport(
        reference_time=reference_time,
        total_replicas=total_replicas,
        last_hour_replicas=last_hour_replicas,
        total_traces=len(traces_by_id),
        total_spans=len(spans),
        last_1h_traces=last_1h_count,
        sample_10pct_traces=sample_10pct_count,
        sample_5pct_traces=sample_5pct_count,
        last_1h_and_5pct_traces=last_1h_and_5pct_count,
        step1_rows=tuple(step1_rows),
    )


def render_loadgen_markdown_report(report: TimeShiftLoadgenReport) -> str:
    """Format a Markdown report for `python3 -m bq_eval.cli loadgen`."""
    rows_md: list[str] = []
    for r in report.step1_rows:
        cpo = (
            f"${r.step1_cost_per_outcome_usd:.4f}"
            if r.step1_cost_per_outcome_usd is not None
            else "NULL"
        )
        rows_md.append(
            f"| `{r.apphub_application_id}` | `{r.business_unit}` | {r.framework_runtime} | "
            f"{r.total_traces} ({r.last_1h_traces} in 1h) | {r.step1_pass_rate * 100:.1f}% | "
            f"{r.p95_latency_ms:,.0f} ms | ${r.total_cost_usd:.2f} | "
            f"${r.cost_per_trace_usd:.4f} | **{cpo}** |"
        )
    table_body = "\n".join(rows_md)

    return f"""# BQ Eval Time-Shift Demo Load Generator (`bq-eval loadgen`)

- **Reference Timestamp (`CURRENT_TIMESTAMP()`):** `{report.reference_time.isoformat()}`
- **Time-Shifted Volume:** `{report.total_traces:,}` traces (`{report.total_spans:,}` spans across `{report.total_replicas}` replicas of 16 golden templates)

## 1. Step 1: Day-1 Cost per Application, Business Unit & Outcome (`0` Extra Tables)

| Application (`app.id` / Fallback) | Business Unit | Framework & Runtime | Traces (Last 1h) | Step-1 Pass | Latency P95 | Total Cost | Cost / Trace | Cost / Outcome |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
{table_body}

## 2. Step 2 & Step 2.5 Trace Slice Volumes Ready for `AI.GENERATE_TABLE`

| Evaluation Stage & Slice | SQL Filter on `v_step2_trace_rubric_prompts` | Matching Traces |
| :--- | :--- | ---: |
| **Step 2A — Full Trace Eval (`100%`)** | *(No sample filter)* | `{report.total_traces:,}` |
| **Step 2B — Default `10%` Sample** | `AND MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 10` | `{report.sample_10pct_traces:,}` |
| **Step 2.5 — Custom `5%` Sample (Skill)** | `AND MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < 5` | `{report.sample_5pct_traces:,}` |
| **Step 2.5 — Only Last `1 Hour` of Traces (Skill)** | `AND trace_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)` | `{report.last_1h_traces:,}` |
| **Step 2.5 — Combined (`5%` of Last `1 Hour`)** | `INTERVAL 1 HOUR AND FARM_FINGERPRINT < 5` | `{report.last_1h_and_5pct_traces:,}` |

## 3. Refresh Synthetic Seed Dataset (`agent_eval_seed`) Before Offline Demos (`< 2 Seconds`, `$0` LLM Cost)

```bash
bq query --project_id=YOUR_PROJECT --use_legacy_sql=false < sql/demo_time_shift_loadgen.sql
```
"""


def export_time_shifted_jsonl(
    spans: Sequence[OTelGenAISpanRecord],
    payloads: Sequence[TracePayloadRecord],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write time-shifted `otel_spans.jsonl` and `trace_payloads.jsonl` for `bq load`."""
    output_dir.mkdir(parents=True, exist_ok=True)
    spans_path = output_dir / "otel_spans.jsonl"
    payloads_path = output_dir / "trace_payloads.jsonl"

    with spans_path.open("w", encoding="utf-8") as sf:
        for s in spans:
            row = {
                "trace_id": s.trace_id,
                "span_id": s.span_id,
                "parent_span_id": s.parent_span_id,
                "span_name": s.span_name or s.tool_name or str(s.span_kind),
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "receive_time": (s.receive_time or s.end_time).isoformat(),
                "latency_ms": s.latency_ms,
                "status_code": s.status_code,
                "otel_span_kind": s.otel_span_kind,
                "span_kind": str(s.span_kind),
                "service_name": s.service_name,
                "gcp_apphub_application_id": s.gcp_apphub_application_id,
                "gcp_apphub_service_id": s.gcp_apphub_service_id,
                "gcp_apphub_workload_id": s.gcp_apphub_workload_id,
                "agent_name": s.agent_name,
                "model_name": s.model_name,
                "input_tokens": s.input_tokens,
                "cached_input_tokens": s.cached_input_tokens,
                "output_tokens": s.output_tokens,
                "thinking_tokens": s.thinking_tokens,
                "tool_name": s.tool_name,
                "tool_call_status": s.tool_call_status,
                "rule0_pass": s.rule0_pass,
                "finops_traffic_type": str(s.finops_traffic_type),
                "outcome_id": s.outcome_id or f"out-{s.trace_id}",
                "outcome_owner": s.outcome_owner or s.gcp_apphub_application_id,
                "source_mode": s.source_mode or "SEED_DEMO",
                "skill_id": s.skill_id or "default-skill",
                "skill_version": s.skill_version or "1.0.0",
                "entity_id": s.entity_id or s.trace_id,
                "attributes": json.dumps(s.attributes),
            }
            sf.write(json.dumps(row) + "\n")

    with payloads_path.open("w", encoding="utf-8") as pf:
        for p in payloads:
            row = {
                "trace_id": p.trace_id,
                "apphub_application_id": p.apphub_application_id,
                "ingested_at": p.ingested_at.isoformat(),
                "user_prompt": p.user_prompt,
                "retrieved_context": p.retrieved_context,
                "final_response": p.final_response,
            }
            pf.write(json.dumps(row) + "\n")

    return spans_path, payloads_path
