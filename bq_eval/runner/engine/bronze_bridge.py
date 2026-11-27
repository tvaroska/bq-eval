"""Bronze-shaped fixture bridge and cached Silver + Gold warehouse template (`bq_eval.runner`)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from bq_eval.core.contract.otel import (
    GenAISpanKind,
    OTelGenAISpanRecord,
    TracePayloadRecord,
)
from bq_eval.core.contract.results import BqQueryCostRow
from bq_eval.core.engine.resources import resolve_packaged_data
from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.loader import OfflineSqlRunner
from bq_eval.runner.engine.tvf import TableFunctionDef

SILVER_BOOT_ORDER: tuple[str, ...] = (
    "silver/reference.sql",
    "silver/workflow_events.sql",
    "silver/shape3.sql",
    "silver/jcs.sql",
    "silver/spans.sql",
    "silver/model_calls.sql",
    "silver/tool_calls.sql",
    "silver/events.sql",
    "silver/trace_aux.sql",
    "silver/traces.sql",
    "silver/trace_payloads.sql",
    "silver/outcomes.sql",
    "silver/capacity/tables.sql",
    "silver/capacity/requests.sql",
    "silver/wrapper_views.sql",
)

GOLD_BOOT_ORDER: tuple[str, ...] = (
    "core/ddl.sql",
    "core/pricing_seed.sql",
    "core/gold_cost_views.sql",
    "core/gold_eval_gates.sql",
    "core/step2_stateless_eval.sql",
    "core/step3_saved_results.sql",
    "core/step3_daily_quota_guard.sql",
    "core/summary_views.sql",
    "core/gold_staging_open.sql",
    "core/gold_staging_restricted.sql",
    "finops/capacity_views.sql",
)

BRONZE_SPAN_EXTRA_COLS: tuple[str, ...] = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "kind",
    "start_time",
    "end_time",
    "receive_time",
    "status",
    "trace_state",
    "attributes",
    "resource",
    "instrumentation_scope",
    "events",
    "links",
)

BRONZE_LOG_EXTRA_COLS: tuple[str, ...] = (
    "insert_id",
    "timestamp",
    "receive_timestamp",
    "log_id",
    "log_name",
    "trace",
    "span_id",
    "labels",
    "json_payload",
    "otel",
)

SHAPE3_EXTRA_COLS: tuple[str, ...] = ("dt", "hr", "ref_uri", "payload")


@dataclass(frozen=True)
class _CachedWarehouseTemplate:
    conn: sqlite3.Connection
    defs: dict[tuple[str | None, str], TableFunctionDef]
    views: dict[tuple[str | None, str], TableFunctionDef]
    table_columns: dict[str, tuple[str, ...]]
    ddl_table_columns: dict[str, tuple[str, ...]]
    version_token: int


_WARMUP_QUERIES: tuple[str, ...] = (
    "SELECT * FROM `agent_eval.v_trace_sql_evaluation`",
    "SELECT trace_id, apphub_application_id, outcome_id, rule0_pass FROM `agent_eval.v_trace_sql_evaluation`",
    "SELECT * FROM `agent_eval.v_agent_eval_and_finops_summary` ORDER BY apphub_application_id, rubric_id",
    "SELECT * FROM `agent_eval.v_capacity_hourly_burndown_export` ORDER BY timestamp, tenant, model, finops_traffic_type, serving_traffic_type",
    "SELECT * FROM `agent_eval.v_optimizer_gold_export`",
)
_TEMPLATE_CACHE: list[_CachedWarehouseTemplate] = []
_TEMPLATE_LOCK = threading.Lock()


def _get_warehouse_template() -> _CachedWarehouseTemplate:
    if _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[0]
    with _TEMPLATE_LOCK:
        if _TEMPLATE_CACHE:
            return _TEMPLATE_CACHE[0]
        sql_dir = resolve_packaged_data("sql")
        params = {"GOLD_LOOKBACK_HOURS": "87600"}
        runner = OfflineSqlRunner(RunnerConfig(default_dataset="agent_eval"))
        runner.load_rows("obs_traces._AllSpans", (), extra_columns=BRONZE_SPAN_EXTRA_COLS)
        runner.load_rows("platform_traces._AllSpans", (), extra_columns=BRONZE_SPAN_EXTRA_COLS)
        runner.load_rows("obs_logs._AllLogs", (), extra_columns=BRONZE_LOG_EXTRA_COLS)
        runner.load_rows("silver_restricted.shape3_payloads", (), extra_columns=SHAPE3_EXTRA_COLS)
        for rel in (*SILVER_BOOT_ORDER, *GOLD_BOOT_ORDER):
            runner.register_silver_file(Path(sql_dir) / rel, params=params)
        for q in _WARMUP_QUERIES:
            runner.query(q)
        runner.clear_temp_stages()
        built = _CachedWarehouseTemplate(
            conn=runner.conn,
            defs=dict(runner.registry._defs),
            views=dict(runner.registry._views),
            table_columns=dict(runner._table_columns),
            ddl_table_columns=dict(runner._ddl_table_columns),
            version_token=runner.registry.version_token,
        )
        _TEMPLATE_CACHE.append(built)
        return built


def open_silver_gold_runner(config: RunnerConfig | None = None) -> OfflineSqlRunner:
    """Create an in-memory `OfflineSqlRunner` pre-populated with all Silver + Gold schemas & views."""
    tmpl = _get_warehouse_template()
    cfg = config or RunnerConfig(default_dataset="agent_eval")
    if cfg.default_dataset is None:
        cfg = RunnerConfig(
            current_date_utc=cfg.current_date_utc,
            current_timestamp_utc=cfg.current_timestamp_utc,
            session_user=cfg.session_user,
            default_dataset="agent_eval",
        )
    runner = OfflineSqlRunner(cfg)
    with _TEMPLATE_LOCK:
        tmpl.conn.backup(runner.conn)
    runner.registry._defs = dict(tmpl.defs)
    runner.registry._views = dict(tmpl.views)
    runner.registry.version_token = tmpl.version_token
    runner._table_columns = dict(tmpl.table_columns)
    runner._ddl_table_columns = dict(tmpl.ddl_table_columns)
    return runner


def _otel_kind_int(raw: str) -> int:
    mapping = {
        "1": 1,
        "INTERNAL": 1,
        "SPAN_KIND_INTERNAL": 1,
        "2": 2,
        "SERVER": 2,
        "SPAN_KIND_SERVER": 2,
        "3": 3,
        "CLIENT": 3,
        "SPAN_KIND_CLIENT": 3,
        "4": 4,
        "PRODUCER": 4,
        "SPAN_KIND_PRODUCER": 4,
        "5": 5,
        "CONSUMER": 5,
        "SPAN_KIND_CONSUMER": 5,
    }
    return mapping.get(raw.strip().upper(), 1)


def _has_positive_tokens(span: OTelGenAISpanRecord) -> bool:
    return (span.prompt_context_tokens + span.output_tokens + span.thinking_tokens) > 0


def _build_gateway_log_row(
    span: OTelGenAISpanRecord,
    eff_span_id: str,
    *,
    is_err: bool,
) -> tuple[str, dict[str, object]]:
    req_id = f"req:{span.trace_id}:{eff_span_id}"
    req_iso = span.start_time.astimezone(UTC).isoformat()
    resolved_app = span.resolved_application_id
    return req_id, {
        "insert_id": f"gw:{req_id}:1",
        "timestamp": req_iso,
        "receive_timestamp": req_iso,
        "log_id": "inference_request",
        "log_name": "projects/showcase-project/logs/inference_request",
        "trace": span.trace_id,
        "span_id": eff_span_id,
        "labels": (
            {"request_type_header": span.request_type_header} if span.request_type_header else {}
        ),
        "json_payload": {
            "client_request_id": req_id,
            "trace_id": span.trace_id,
            "span_id": eff_span_id,
            "attempt_index": 1,
            "caller_app_id": None if resolved_app == "unregistered_app" else resolved_app,
            "request_model": span.model_name,
            "response_model": span.model_name,
            "serving_tier": str(span.serving_traffic_type),
            "server_traffic_type_present": True,
            "http_status_code": span.http_status_code or (500 if is_err else 200),
            "input_tokens": span.prompt_context_tokens,
            "cache_read_tokens": span.cached_input_tokens,
            "cache_write_5m_tokens": span.cache_write_5m_tokens,
            "cache_write_1h_tokens": span.cache_write_1h_tokens,
            "output_tokens": span.output_tokens,
            "reasoning_tokens": span.thinking_tokens,
            "traffic_type": str(span.finops_traffic_type),
        },
        "otel": {"resource": {"attributes": {"service.name": span.service_name}}},
    }


def _build_bronze_envelope(  # noqa: PLR0913
    span: OTelGenAISpanRecord,
    *,
    eff_span_id: str,
    eff_parent: str | None,
    name: str,
    kind: int,
    is_err: bool,
    attrs: dict[str, object],
    res_attrs: dict[str, object],
) -> dict[str, object]:
    return {
        "trace_id": span.trace_id,
        "span_id": eff_span_id,
        "parent_span_id": eff_parent,
        "name": name,
        "kind": kind,
        "start_time": span.start_time.astimezone(UTC).isoformat(),
        "end_time": span.end_time.astimezone(UTC).isoformat(),
        "receive_time": (span.receive_time or span.end_time).astimezone(UTC).isoformat(),
        "status": {"code": 2 if is_err else 1},
        "trace_state": None,
        "attributes": attrs,
        "resource": {"attributes": res_attrs},
        "instrumentation_scope": {"name": "bq_eval.offline"},
        "events": [],
        "links": [],
    }


def _populate_llm_bronze_attrs(
    span: OTelGenAISpanRecord,
    attrs: dict[str, object],
    *,
    eff_span_id: str,
    is_err: bool,
    payload: TracePayloadRecord | None,
) -> dict[str, object] | None:
    attrs["gen_ai.request.model"] = span.model_name
    attrs["gen_ai.response.model"] = span.model_name
    attrs["gen_ai.usage.input_tokens"] = span.uncached_input_tokens
    attrs["gen_ai.usage.cache_read.input_tokens"] = span.cached_input_tokens
    attrs["gen_ai.usage.cache_write.input_tokens"] = (
        span.cache_write_5m_tokens + span.cache_write_1h_tokens
    )
    attrs["gen_ai.usage.output_tokens"] = span.output_tokens
    attrs["gen_ai.usage.reasoning.output_tokens"] = span.thinking_tokens
    if payload is not None:
        attrs.setdefault("gen_ai.input.messages", [{"role": "user", "text": payload.user_prompt}])
        attrs.setdefault(
            "gen_ai.output.messages", [{"role": "assistant", "text": payload.final_response}]
        )
    elif "bq_eval.force_content_missing" not in attrs:
        if "gen_ai.input.messages_ref" not in attrs:
            attrs.setdefault("gen_ai.input.messages", [{"role": "user", "text": "prompt"}])
        if "gen_ai.output.messages_ref" not in attrs:
            attrs.setdefault("gen_ai.output.messages", [{"role": "assistant", "text": "response"}])
    if (
        span.cache_write_5m_tokens > 0
        or span.cache_write_1h_tokens > 0
        or span.request_type_header is not None
    ):
        req_id, gateway_log = _build_gateway_log_row(span, eff_span_id, is_err=is_err)
        attrs["bq_eval.client_request_id"] = req_id
        return gateway_log
    return None


def _populate_standard_span_attrs(
    span: OTelGenAISpanRecord,
    attrs: dict[str, object],
    *,
    force_chat_leaf: bool,
    payload: TracePayloadRecord | None,
) -> bool:
    attrs["gen_ai.operation.name"] = "chat" if force_chat_leaf else str(span.span_kind)
    attrs["gen_ai.agent.name"] = span.agent_name
    optional_pairs = (
        ("outcome.id", span.outcome_id),
        ("outcome.owner", span.outcome_owner),
        ("gen_ai.workflow.name", span.source_mode),
        ("gen_ai.skill.name", span.skill_id),
        ("bq_eval.skill_version", span.skill_version),
        ("gen_ai.conversation.id", span.session_id),
        ("bq_eval.turn_index", span.turn_index),
        ("http.response.status_code", span.http_status_code),
    )
    attrs.update({k: v for k, v in optional_pairs if v is not None and v != ""})
    attrs["bq_eval.traffic_type"] = str(span.finops_traffic_type)
    attrs["bq_eval.serving_tier"] = str(span.serving_traffic_type)
    if "gen_ai.request.previous_interaction_id" in attrs:
        attrs.setdefault(
            "bq_eval.previous_interaction_id",
            attrs["gen_ai.request.previous_interaction_id"],
        )

    tool_failed = (
        span.span_kind == GenAISpanKind.EXECUTE_TOOL
        and (span.tool_call_status or "SUCCESS") != "SUCCESS"
    )
    is_err = span.status_code == "ERROR" or tool_failed
    if not span.rule0_pass or tool_failed:
        attrs["bq_eval.app_failed"] = "true"
    if is_err:
        attrs.setdefault("error.type", span.tool_call_status or "ERROR")
    if span.span_kind == GenAISpanKind.EXECUTE_TOOL and not force_chat_leaf:
        attrs["gen_ai.tool.name"] = span.tool_name or "tool"
        attrs.setdefault("gen_ai.tool.call.arguments", "{}")
        attrs.setdefault(
            "gen_ai.tool.call.result",
            payload.retrieved_context if payload is not None else '{"status":"ok"}',
        )
    return is_err


def _build_bronze_span_row(  # noqa: PLR0913
    span: OTelGenAISpanRecord,
    *,
    span_id_override: str | None = None,
    parent_override: str | None = None,
    keep_parent_override: bool = False,
    zero_out_tokens: bool = False,
    force_chat_leaf: bool = False,
    payload: TracePayloadRecord | None = None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    eff_span_id = span_id_override or span.span_id
    eff_parent = parent_override if keep_parent_override else span.parent_span_id
    attrs: dict[str, object] = dict(span.attributes)
    resolved_app = span.resolved_application_id
    res_pairs = (
        ("service.name", span.service_name),
        ("apphub.application.id", None if resolved_app == "unregistered_app" else resolved_app),
        ("apphub.service.id", span.gcp_apphub_service_id),
        ("apphub.workload.id", span.gcp_apphub_workload_id),
    )
    res_attrs: dict[str, object] = {k: v for k, v in res_pairs if v}

    if span.span_name == "outcome.closed":
        if span.outcome_id:
            attrs.setdefault("outcome.id", span.outcome_id)
        if span.outcome_owner:
            attrs.setdefault("outcome.owner", span.outcome_owner)
        if not span.rule0_pass:
            attrs["bq_eval.app_failed"] = "true"
        eval_log: dict[str, object] | None = None
        raw_ver = attrs.get("outcome.verified")
        if raw_ver is not None and str(raw_ver).strip().lower() not in {"true", "verified", "pass"}:
            req_iso = span.start_time.astimezone(UTC).isoformat()
            eval_log = {
                "insert_id": f"ev:{span.trace_id}:{eff_span_id}",
                "timestamp": req_iso,
                "receive_timestamp": req_iso,
                "log_id": "gen_ai.evaluation.result",
                "log_name": "projects/showcase-project/logs/gen_ai.evaluation.result",
                "trace": span.trace_id,
                "span_id": eff_span_id,
                "labels": {
                    "outcome.id": str(attrs.get("outcome.id") or ""),
                    "gen_ai.evaluation.score.label": str(attrs["outcome.verified"]),
                },
                "json_payload": None,
                "otel": {"resource": {"attributes": {"service.name": span.service_name}}},
            }
        return (
            _build_bronze_envelope(
                span,
                eff_span_id=eff_span_id,
                eff_parent=eff_parent,
                name="outcome.closed",
                kind=_otel_kind_int(span.otel_span_kind),
                is_err=span.status_code == "ERROR",
                attrs=attrs,
                res_attrs=res_attrs,
            ),
            eval_log,
        )

    is_err = _populate_standard_span_attrs(
        span,
        attrs,
        force_chat_leaf=force_chat_leaf,
        payload=payload,
    )
    gateway_log: dict[str, object] | None = None
    is_llm = _has_positive_tokens(span) or (
        bool(span.model_name)
        and span.model_name != "none"
        and span.span_kind != GenAISpanKind.EXECUTE_TOOL
    )
    if not zero_out_tokens and is_llm:
        gateway_log = _populate_llm_bronze_attrs(
            span,
            attrs,
            eff_span_id=eff_span_id,
            is_err=is_err,
            payload=payload,
        )

    return (
        _build_bronze_envelope(
            span,
            eff_span_id=eff_span_id,
            eff_parent=eff_parent,
            name="chat" if force_chat_leaf else span.span_name,
            kind=3 if force_chat_leaf else _otel_kind_int(span.otel_span_kind),
            is_err=is_err,
            attrs=attrs,
            res_attrs=res_attrs,
        ),
        gateway_log,
    )


def _resolve_app_storage_usd(spans: Sequence[OTelGenAISpanRecord], app_id: str) -> float:
    for s in spans:
        if s.resolved_application_id == app_id and "outcome.storage_usd" in s.attributes:
            try:
                parsed = float(str(s.attributes["outcome.storage_usd"]))
            except ValueError:
                return 0.000020
            return parsed if parsed >= 0.0 else 0.000020
    return 0.000020


def spans_to_bronze_bundle(
    spans: Sequence[OTelGenAISpanRecord],
    *,
    payloads: Sequence[TracePayloadRecord] = (),
    sla_latency_ms: float = 5000.0,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Convert `OTelGenAISpanRecord` inputs into Bronze `_AllSpans`, `_AllLogs`, `app_registry`, and threshold rows."""
    payload_by_trace = {p.trace_id: p for p in payloads}
    by_trace: dict[str, list[OTelGenAISpanRecord]] = {}
    for sp in spans:
        by_trace.setdefault(sp.trace_id, []).append(sp)

    enclosing_tok_parents: set[tuple[str, str]] = set()
    for tid, t_spans in by_trace.items():
        tok_ids = {s.span_id for s in t_spans if _has_positive_tokens(s)}
        for s in t_spans:
            if _has_positive_tokens(s) and s.parent_span_id in tok_ids:
                enclosing_tok_parents.add((tid, s.parent_span_id))

    bronze_spans: list[dict[str, object]] = []
    bronze_logs: list[dict[str, object]] = []
    app_meta: dict[str, tuple[str, str]] = {}

    for sp in spans:
        app_id = sp.resolved_application_id
        if app_id != "unregistered_app" and app_id not in app_meta:
            bu = str(
                sp.attributes.get("gcp.apphub.business_unit")
                or sp.attributes.get("business_unit")
                or "unassigned_bu"
            )
            cc = str(
                sp.attributes.get("gcp.apphub.cost_center")
                or sp.attributes.get("cost_center")
                or "UNASSIGNED"
            )
            app_meta[app_id] = (bu, cc)

        pl = payload_by_trace.get(sp.trace_id)
        if (sp.trace_id, sp.span_id) in enclosing_tok_parents:
            wrap_row, _ = _build_bronze_span_row(sp, zero_out_tokens=True, payload=pl)
            leaf_row, gw_log = _build_bronze_span_row(
                sp,
                span_id_override=f"{sp.span_id}__llm",
                parent_override=sp.span_id,
                keep_parent_override=True,
                force_chat_leaf=True,
                payload=pl,
            )
            bronze_spans.extend((wrap_row, leaf_row))
            if gw_log is not None:
                bronze_logs.append(gw_log)
        else:
            row, gw_log = _build_bronze_span_row(sp, payload=pl)
            bronze_spans.append(row)
            if gw_log is not None:
                bronze_logs.append(gw_log)

    app_rows: list[dict[str, object]] = [
        {
            "app_id": app_id,
            "agent_id": None,
            "apphub_application_id": app_id,
            "service_name": None,
            "environment": None,
            "allowed_landing_places": [
                "test-proj.obs_traces._AllSpans",
                "showcase-project.obs_traces._AllSpans",
            ],
            "owner": "offline-owner",
            "business_unit": bu,
            "risk_tier": "RL-3b",
            "payload_profile": "ephemeral_sample",
            "sampling_mode": "full",
            "barrier_domain": None,
            "runtime_lane": "agent_runtime",
            "mrm_model_id": None,
            "validation_expiry_date": None,
            "effective_from": "2020-01-01T00:00:00+00:00",
            "effective_to": None,
        }
        for app_id, (bu, _) in sorted(app_meta.items())
    ]
    threshold_rows: list[dict[str, object]] = [
        {
            "apphub_application_id": app_id,
            "display_name": app_id,
            "cost_center": cc,
            "sla_latency_ms": float(sla_latency_ms),
            "max_tool_calls": 8,
            "monthly_budget_usd": None,
            "is_external_counterparty": (
                str(
                    next(
                        (
                            s.attributes.get("apphub.external")
                            for s in spans
                            if s.resolved_application_id == app_id
                        ),
                        "false",
                    )
                ).lower()
                == "true"
            ),
            "storage_usd_per_outcome": _resolve_app_storage_usd(spans, app_id),
            "autonomy_tier": "HUMAN_IN_LOOP",
            "lifecycle_track_policy": "long_term_ga_only",
            "funded_gsus": 0.0,
            "spillover_tier": "standard",
            "pt_commitment_months": 0,
            "owner_email": "offline@example.invalid",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        for app_id, (_, cc) in sorted(app_meta.items())
    ]
    return bronze_spans, bronze_logs, app_rows, threshold_rows


def populate_runner_from_spans(
    runner: OfflineSqlRunner,
    spans: Sequence[OTelGenAISpanRecord],
    *,
    payloads: Sequence[TracePayloadRecord] = (),
    sla_latency_ms: float = 5000.0,
    bq_query_jobs: Sequence[BqQueryCostRow] = (),
) -> None:
    """Populate `runner` with Bronze/Silver/Gold rows derived from `spans`."""
    bronze_spans, bronze_logs, app_rows, threshold_rows = spans_to_bronze_bundle(
        spans,
        payloads=payloads,
        sla_latency_ms=sla_latency_ms,
    )
    runner.load_rows("obs_traces._AllSpans", bronze_spans, extra_columns=BRONZE_SPAN_EXTRA_COLS)
    if bronze_logs:
        runner.load_rows("obs_logs._AllLogs", bronze_logs, extra_columns=BRONZE_LOG_EXTRA_COLS)
    if app_rows:
        runner.load_rows("silver_ref.app_registry", app_rows, append=True)
    skill_keys: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    for sp in spans:
        app_id = sp.resolved_application_id
        if app_id != "unregistered_app" and sp.skill_id:
            sha = str(sp.attributes.get("bq_eval.skill.sha256") or ("0" * 64))
            prot = (
                str(sp.attributes["bq_eval.skill.protected_block_sha256"])
                if sp.attributes.get("bq_eval.skill.protected_block_sha256")
                else None
            )
            skill_keys[(app_id, sp.skill_id)] = (sp.skill_version or "1.0.0", sha, prot)
    if skill_keys:
        runner.load_rows(
            "silver_ref.skill_registry",
            [
                {
                    "app_id": app_id,
                    "skill_name": sk_name,
                    "skill_version": ver,
                    "skill_sha256": sha,
                    "protected_block_sha256": prot,
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_to": None,
                }
                for (app_id, sk_name), (ver, sha, prot) in sorted(skill_keys.items())
            ],
            append=True,
        )
    if threshold_rows:
        runner.load_rows("agent_eval.workload_thresholds", threshold_rows, append=True)
    if bq_query_jobs:
        runner.load_rows(
            "agent_eval.bq_query_jobs",
            [
                {
                    "job_id": j.job_id,
                    "apphub_application_id": j.apphub_application_id,
                    "outcome_id": j.outcome_id,
                    "total_bytes_billed": j.total_bytes_billed,
                    "total_slot_ms": j.total_slot_ms,
                    "pricing_mode": j.pricing_mode,
                    "query_cost_usd": j.query_cost_usd,
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
                for j in bq_query_jobs
            ],
            append=True,
        )

    bus = {"__unowned__", *(str(r["business_unit"]) for r in app_rows)}
    runner.load_rows(
        "silver_internal.unit_entitlements",
        [
            {
                "principal_email": runner.config.session_user,
                "business_unit": bu,
                "tenant_id": "*",
            }
            for bu in sorted(bus)
        ],
        append=True,
    )
