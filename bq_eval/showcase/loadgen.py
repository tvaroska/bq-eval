"""Capture-Once, Time-Shift & Multiply Demo Load Generator (`bq_eval.showcase.loadgen`)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bq_eval.contract.otel import GenAISpanKind, OTelGenAISpanRecord, TracePayloadRecord
from bq_eval.contract.results import (
    GoldenSpanTemplate,
    GoldenTraceTemplate,
    Step1CostOutcomeRow,
    TimeShiftLoadgenReport,
)
from bq_eval.engine.finops import compute_default_span_cost_usd
from bq_eval.showcase.seeds_banking import build_banking_and_quoting_templates
from bq_eval.showcase.seeds_claims import build_claims_and_audit_templates

DEFAULT_SEED_DATASET_ID: str = "agent_eval_seed"
_REPLICA_TAG_RE = re.compile(r"-r(\d+)-\d{8}$")

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


def build_golden_seed_templates() -> tuple[GoldenTraceTemplate, ...]:
    """Return all 16 golden trace templates across 4 enterprise agent workloads."""
    return build_banking_and_quoting_templates() + build_claims_and_audit_templates()


def _deterministic_bucket_100(trace_id: str) -> int:
    """Deterministic [0..99] SHA-256 preview bucket (volume proxy for BigQuery FARM_FINGERPRINT)."""
    digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return val % 100


def generate_time_shifted_corpus(
    reference_time: datetime | None = None,
    total_replicas: int = 50,
    last_hour_replicas: int = 15,
) -> tuple[list[OTelGenAISpanRecord], list[TracePayloadRecord], datetime]:
    """Time-shift and multiply the 16 golden trace templates into `reference_time`."""
    if total_replicas < 1:
        raise ValueError("total_replicas must be >= 1.")
    if not (0 <= last_hour_replicas <= total_replicas):
        raise ValueError("last_hour_replicas must be between 0 and total_replicas.")

    ref_time = reference_time or datetime.now(UTC)
    date_tag = ref_time.strftime("%Y%m%d")
    templates = build_golden_seed_templates()

    spans: list[OTelGenAISpanRecord] = []
    payloads: list[TracePayloadRecord] = []
    last_hour_step_minutes = min(3.0, 52.0 / last_hour_replicas) if last_hour_replicas > 0 else 3.0

    for replica_id in range(1, total_replicas + 1):
        if replica_id <= last_hour_replicas:
            base_age_seconds = round(replica_id * last_hour_step_minutes * 60.0)
        else:
            base_age_seconds = (60 + ((replica_id - last_hour_replicas) * 35)) * 60

        for tmpl in templates:
            trace_id = f"{tmpl.base_trace_id}-r{replica_id:03d}-{date_tag}"
            trace_base_start = ref_time - timedelta(
                seconds=base_age_seconds + (tmpl.template_idx * 7)
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


def _select_root_span(trace_spans: Sequence[OTelGenAISpanRecord]) -> OTelGenAISpanRecord:
    """Select the root span (`parent_span_id is None`), falling back to earliest start (`F17`)."""
    roots = [s for s in trace_spans if s.parent_span_id is None]
    candidates = roots if roots else trace_spans
    return min(candidates, key=lambda s: (s.start_time, s.span_id))


def _summarize_app_group(
    group_key: tuple[str, str, str, str],
    t_list: list[dict[str, float | int | bool]],
) -> Step1CostOutcomeRow:
    app_id, bu, cc, fw = group_key
    n_traces = len(t_list)
    n_last_1h = sum(1 for x in t_list if x["in_last_1h"])
    n_pass = sum(1 for x in t_list if x["step1_pass"])
    latencies = sorted(float(x["latency_ms"]) for x in t_list)
    p95_idx = min(len(latencies) - 1, max(0, math.ceil(0.95 * len(latencies)) - 1))
    tot_cost = sum(float(x["cost_usd"]) for x in t_list)
    return Step1CostOutcomeRow(
        apphub_application_id=app_id,
        business_unit=bu,
        cost_center=cc,
        framework_runtime=fw,
        total_traces=n_traces,
        last_1h_traces=n_last_1h,
        step1_passing_traces=n_pass,
        step1_pass_rate=n_pass / n_traces,
        tool_success_rate=sum(1 for x in t_list if x["tool_ok"]) / n_traces,
        p95_latency_ms=latencies[p95_idx],
        avg_cache_hit_ratio=sum(float(x["cache_ratio"]) for x in t_list) / n_traces,
        total_cost_usd=tot_cost,
        cost_per_trace_usd=tot_cost / n_traces,
        step1_cost_per_outcome_usd=(tot_cost / n_pass) if n_pass > 0 else None,
    )


def evaluate_time_shifted_corpus(
    spans: Sequence[OTelGenAISpanRecord],
    reference_time: datetime,
) -> TimeShiftLoadgenReport:
    """Evaluate Step 1 Cost per App/BU/Outcome and Step 2/2.5 slices on time-shifted traces."""
    if not spans:
        raise ValueError("spans must be non-empty.")

    one_hour_ago = reference_time - timedelta(hours=1)
    traces_by_id: dict[str, list[OTelGenAISpanRecord]] = {}
    for s in spans:
        traces_by_id.setdefault(s.trace_id, []).append(s)

    app_groups: dict[tuple[str, str, str, str], list[dict[str, float | int | bool]]] = {}
    last_1h_count = 0
    sample_10pct_count = 0
    sample_5pct_count = 0
    last_1h_and_5pct_count = 0
    all_replicas: set[int] = set()
    last_hour_replica_ids: set[int] = set()

    for trace_id, t_spans in traces_by_id.items():
        root = _select_root_span(t_spans)
        group_key = (
            root.resolved_application_id,
            str(root.attributes.get("business_unit", "unassigned_bu")),
            str(root.attributes.get("cost_center", "UNASSIGNED")),
            f"{root.attributes.get('framework', 'unknown')} ({root.attributes.get('runtime', 'unknown')})",
        )
        trace_start = min(s.start_time for s in t_spans)
        trace_latency_ms = (max(s.end_time for s in t_spans) - trace_start).total_seconds() * 1000.0
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
        in_last_1h = trace_start >= one_hour_ago
        bucket = _deterministic_bucket_100(trace_id)
        last_1h_count += int(in_last_1h)
        sample_10pct_count += int(bucket < 10)
        sample_5pct_count += int(bucket < 5)
        last_1h_and_5pct_count += int(in_last_1h and bucket < 5)

        m = _REPLICA_TAG_RE.search(trace_id)
        if m is not None:
            rep = int(m.group(1))
            all_replicas.add(rep)
            if in_last_1h:
                last_hour_replica_ids.add(rep)

        app_groups.setdefault(group_key, []).append(
            {
                "in_last_1h": in_last_1h,
                "step1_pass": step1_pass,
                "tool_ok": tool_errors == 0,
                "latency_ms": trace_latency_ms,
                "cache_ratio": (sum(s.cached_input_tokens for s in t_spans) / total_in)
                if total_in > 0
                else 0.0,
                "cost_usd": sum(compute_default_span_cost_usd(s) for s in t_spans),
            }
        )

    step1_rows = [
        _summarize_app_group(group_key, t_list)
        for group_key, t_list in sorted(
            app_groups.items(),
            key=lambda item: sum(float(x["cost_usd"]) for x in item[1]),
            reverse=True,
        )
    ]
    derived_total_replicas = len(all_replicas) if all_replicas else max(1, len(traces_by_id))
    derived_last_hour_replicas = len(last_hour_replica_ids) if all_replicas else last_1h_count

    return TimeShiftLoadgenReport(
        reference_time=reference_time,
        total_replicas=derived_total_replicas,
        last_hour_replicas=derived_last_hour_replicas,
        total_traces=len(traces_by_id),
        total_spans=len(spans),
        last_1h_traces=last_1h_count,
        sample_10pct_traces=sample_10pct_count,
        sample_5pct_traces=sample_5pct_count,
        last_1h_and_5pct_traces=last_1h_and_5pct_count,
        step1_rows=tuple(step1_rows),
    )


def render_loadgen_markdown_report(report: TimeShiftLoadgenReport) -> str:
    """Format a Markdown report for `bq-eval loadgen`."""
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
    step1_passing_total = sum(r.step1_passing_traces for r in report.step1_rows)

    return f"""# BQ Eval Time-Shift Demo Load Generator (`bq-eval loadgen`)

- **Reference Timestamp (`CURRENT_TIMESTAMP()`):** `{report.reference_time.isoformat()}`
- **Time-Shifted Volume:** `{report.total_traces:,}` total traces (`{step1_passing_total:,}` Step-1 passing; `{report.total_spans:,}` spans across `{report.total_replicas}` replicas of 16 golden templates)

## 1. Step 1: Day-1 Cost per Application, Business Unit & Outcome (`0` Extra Tables)

| Application (`app.id` / Fallback) | Business Unit | Framework & Runtime | Traces (Last 1h) | Step-1 Pass | Latency P95 | Total Cost | Cost / Trace | Cost / Outcome |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
{table_body}

## 2. Step 2 & Step 2.5 Trace Slice Volumes Ready for `AI.GENERATE_TABLE`

> Note: Counts below reflect all traces in `v_step2_all_trace_rubric_prompts` (`{report.total_traces:,}` total; `{step1_passing_total:,}` pass Step-1 gates in `v_step2_trace_rubric_prompts`). Sample slice counts use a local SHA-256 preview proxy; BigQuery `FARM_FINGERPRINT` selects an independent subset of similar size.

| Evaluation Stage & Slice | BigQuery SQL Filter on `v_step2_all_trace_rubric_prompts` | Local Preview Count (SHA-256 Proxy) |
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


def _http_status_from_span(span: OTelGenAISpanRecord) -> int:
    """Map OTel/tool status strings to integer HTTP status codes for `capacity` wire parity (`M7-3`)."""
    if span.tool_call_status == "RATE_LIMITED" or span.status_code == "RATE_LIMITED":
        return 429
    if span.tool_call_status == "TIMEOUT":
        return 504
    if span.status_code == "ERROR":
        return 500
    return 200


def export_time_shifted_jsonl(
    spans: Sequence[OTelGenAISpanRecord],
    payloads: Sequence[TracePayloadRecord],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write time-shifted `otel_spans.jsonl` and `trace_payloads.jsonl` (`M7-3` wire-aligned)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    spans_path = output_dir / "otel_spans.jsonl"
    payloads_path = output_dir / "trace_payloads.jsonl"

    with spans_path.open("w", encoding="utf-8") as sf:
        for s in spans:
            row = {
                "timestamp": s.start_time.isoformat(),
                "tenant": s.resolved_application_id,
                "model": s.model_name,
                "status_code": s.status_code,
                "http_status_code": _http_status_from_span(s),
                "otel_status_code": s.status_code,
                "cached_tokens": s.cached_input_tokens,
                "thoughts_tokens": s.thinking_tokens,
                "precomputed_weighted_burndown_tokens": s.weighted_burndown_tokens,
                "trace_id": s.trace_id,
                "span_id": s.span_id,
                "parent_span_id": s.parent_span_id,
                "span_name": s.span_name or s.tool_name or str(s.span_kind),
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "receive_time": (s.receive_time or s.end_time).isoformat(),
                "latency_ms": s.latency_ms,
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
