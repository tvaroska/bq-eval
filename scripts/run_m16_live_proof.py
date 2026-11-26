"""Live GCP M16 proof runner: single-model >200K arms, S-5 concurrency lock, S-15 views & App Hub export."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.deploy.engine.bootstrap import (
    split_sql_statements_stdlib,
    substitute_sql_parameters,
)
from bq_eval.deploy.engine.bq_client import bq_cli_query_runner, format_bq_job_labels
from scripts.empirical_proof import (
    _IDENT_RE,
    analyze_unconfounded_context_cliff_arms,
    build_information_schema_jobs_sync_sql,
)
from scripts.m16_vertex_otel_probe import (
    VertexEndpointConfig,
    call_vertex_gemini_single_case,
    export_live_otlp_and_logs,
    gcloud_access_token,
    load_ingested_cliff_spans,
    sanitize_text,
    to_float,
    to_int,
    upload_seed_completion_jsonl,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class M16LiveProofConfig:
    target_project_id: str
    source_project_id: str
    dataset_id: str = "agent_eval"
    model_name: str = "gemini-3.8-flash"
    vertex_location: str = "global"
    per_arm: int = 30
    provision_infra: bool = False
    reuse_ingested_cliff: bool = False
    receipt_out: Path = Path("benchmarks/results/m16_live_cloud_proof.json")
    traces_out: Path = Path("benchmarks/results/m16_live_traces_120.jsonl")
    anonymized_traces_out: Path | None = None


_BLIND_SLUG_RE = re.compile(
    r"\bkyc-(?:s2trap|clean|pobox|ocr|struct)[a-z0-9-]*\b",
    re.IGNORECASE,
)
_CTX_ASSIGN_RE = re.compile(r"\bcontext=([A-Za-z0-9_-]+)")
_CTX_JSON_RE = re.compile(r'"context_id"\s*:\s*"([^"]+)"')
_SENSITIVE_TRACE_KEYS = ("judge_ordinal_score", "reasoning_trace")


def _opaque_digest(namespace: str, raw_value: str, length: int = 16) -> str:
    return hashlib.sha256(f"m16-{namespace}:{raw_value}".encode()).hexdigest()[:length]


def _opaque_id(namespace: str, prefix: str, raw_value: str) -> str:
    if raw_value.startswith(prefix):
        return raw_value
    return f"{prefix}{_opaque_digest(namespace, raw_value)}"


def _scrub_blind_payload(
    text: str,
    raw_to_opaque: dict[str, str],
) -> str:
    scrubbed = _BLIND_SLUG_RE.sub(
        lambda m: f"kyc-case-redacted-{_opaque_digest('slug', m.group(0).lower(), 8)}",
        text,
    )
    scrubbed = _CTX_ASSIGN_RE.sub(
        lambda m: f"context={_opaque_id('context', 'ctx-opaque-', m.group(1))}",
        scrubbed,
    )
    scrubbed = _CTX_JSON_RE.sub(
        lambda m: m.group(0).replace(
            m.group(1),
            _opaque_id("context", "ctx-opaque-", m.group(1)),
        ),
        scrubbed,
    )
    for raw_id, opaque_id in raw_to_opaque.items():
        if len(raw_id) >= 4 and raw_id in scrubbed:
            scrubbed = scrubbed.replace(raw_id, opaque_id)
    return scrubbed


def redact_and_shuffle_traces(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Strip judge scores/reasoning, opacify trace/outcome/context IDs & scenario slugs, and shuffle."""
    keyed_rows: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        item = {k: v for k, v in row.items() if k not in _SENSITIVE_TRACE_KEYS}
        raw_trace_id = str(item["trace_id"])
        shuffle_key = hashlib.sha256(f"m16-blind-shuffle:{raw_trace_id}".encode()).hexdigest()

        raw_to_opaque: dict[str, str] = {}
        opaque_trace_id = _opaque_id("trace", "tr-opaque-", raw_trace_id)
        raw_to_opaque[raw_trace_id] = opaque_trace_id
        item["trace_id"] = opaque_trace_id

        raw_outcome_id = item.get("outcome_id")
        if raw_outcome_id:
            raw_out_str = str(raw_outcome_id)
            opaque_outcome_id = _opaque_id("outcome", "out-opaque-", raw_out_str)
            raw_to_opaque[raw_out_str] = opaque_outcome_id
            item["outcome_id"] = opaque_outcome_id

        raw_context_id = item.get("context_id")
        if raw_context_id:
            raw_ctx_str = str(raw_context_id)
            opaque_context_id = _opaque_id("context", "ctx-opaque-", raw_ctx_str)
            raw_to_opaque[raw_ctx_str] = opaque_context_id
            item["context_id"] = opaque_context_id

        for text_key in ("user_prompt", "retrieved_context", "final_response"):
            if text_key in item and item[text_key] is not None:
                item[text_key] = _scrub_blind_payload(str(item[text_key]), raw_to_opaque)

        keyed_rows.append((shuffle_key, item))

    keyed_rows.sort(key=lambda pair: pair[0])
    return [item for _, item in keyed_rows]


def _wait_for_live_traces(
    project_id: str,
    dataset_id: str,
    *,
    min_spans: int,
    timeout_s: int = 90,
) -> dict[str, int]:
    runner = bq_cli_query_runner(
        project_id, labels={"apphub_application_id": "cliff-benchmark-app"}
    )
    deadline = time.monotonic() + timeout_s
    last_spans = 0
    last_payloads = 0
    while time.monotonic() < deadline:
        rows = runner(
            f"SELECT "  # noqa: S608
            f"(SELECT COUNT(*) FROM `{project_id}.{dataset_id}.otel_spans` "
            f"WHERE gcp_apphub_application_id = 'cliff-benchmark-app') AS span_cnt, "
            f"(SELECT COUNT(*) FROM `{project_id}.{dataset_id}.trace_payloads` "
            f"WHERE apphub_application_id = 'cliff-benchmark-app') AS payload_cnt"
        )
        if rows:
            last_spans = to_int(rows[0].get("span_cnt"))
            last_payloads = to_int(rows[0].get("payload_cnt"))
            if last_spans >= min_spans and last_payloads >= (min_spans // 2):
                return {"span_count": last_spans, "payload_count": last_payloads}
        time.sleep(5.0)
    return {"span_count": last_spans, "payload_count": last_payloads}


def verify_concurrent_quota_lock_live(project_id: str, dataset_id: str) -> dict[str, Any]:
    """Run two simultaneous `CALL run_daily_quota_guarded_judge(...)` processes under a tight cap (`S-5`)."""
    bq_bin = shutil.which("bq")
    if bq_bin is None:
        raise OSError("`bq` CLI not found on PATH.")
    runner = bq_cli_query_runner(
        project_id, labels={"apphub_application_id": "cliff-benchmark-app"}
    )
    quota_sql = substitute_sql_parameters(
        (REPO_ROOT / "sql" / "core" / "step3_daily_quota_guard.sql").read_text(encoding="utf-8"),
        project_id=project_id,
        dataset_id=dataset_id,
    )
    for stmt in split_sql_statements_stdlib(quota_sql):
        if stmt.upper().startswith("CREATE OR REPLACE PROCEDURE"):
            runner(stmt)

    runner(
        f"DELETE FROM `{project_id}.{dataset_id}.eval_results` "  # noqa: S608
        f"WHERE eval_run_id IN ('m16_conc_call_a', 'm16_conc_call_b'); "
        f"DELETE FROM `{project_id}.{dataset_id}.eval_daily_quota_reservations` "
        f"WHERE rubric_id = 'groundedness_4pt_v1' AND apphub_application_id = 'cliff-benchmark-app'; "
        f"DELETE FROM `{project_id}.{dataset_id}.eval_daily_quota_state` "
        f"WHERE quota_date_utc = CURRENT_DATE('UTC'); "
        f"UPDATE `{project_id}.{dataset_id}.eval_daily_quota_config` "
        f"SET daily_judge_token_cap = 35000, per_app_default_token_cap = 35000, updated_at = CURRENT_TIMESTAMP() "
        f"WHERE quota_name = 'default_step2_daily_cap';"
    )

    label_flags = format_bq_job_labels({"apphub_application_id": "cliff-benchmark-app"})

    def _invoke_call(tag: str) -> dict[str, Any]:
        call_sql = (
            f"CALL `{project_id}.{dataset_id}.run_daily_quota_guarded_judge`"
            f"('groundedness_4pt_v1', 100, 512, 'LOW', 24, 'm16_conc_{tag}');"
        )
        t_start = time.perf_counter()
        proc = subprocess.run(  # noqa: S603
            [
                bq_bin,
                "query",
                f"--project_id={project_id}",
                "--use_legacy_sql=false",
                "--format=none",
                *label_flags,
            ],
            input=call_sql,
            text=True,
            capture_output=True,
            check=False,
        )
        combined_out = "\n".join(
            line
            for line in (proc.stdout + "\n" + proc.stderr).splitlines()
            if "RequestsDependencyWarning" not in line and "warnings.warn(" not in line
        ).strip()
        return {
            "tag": tag,
            "returncode": proc.returncode,
            "duration_s": round(time.perf_counter() - t_start, 3),
            "detail": sanitize_text(combined_out[:280], (project_id,)),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_a = pool.submit(_invoke_call, "call_a")
        f_b = pool.submit(_invoke_call, "call_b")
        call_results = [f_a.result(), f_b.result()]

    status_rows = runner(
        f"SELECT daily_judge_token_cap, per_app_default_token_cap, tokens_used_today, "  # noqa: S608
        f"tokens_reserved_today, tokens_burned_and_reserved_today "
        f"FROM `{project_id}.{dataset_id}.v_step2_daily_quota_status`"
    )
    res_rows = runner(
        f"SELECT status, COUNT(*) AS batch_count, COALESCE(SUM(reserved_tokens), 0) AS reserved_tokens "  # noqa: S608
        f"FROM `{project_id}.{dataset_id}.eval_daily_quota_reservations` "
        f"WHERE quota_date_utc = CURRENT_DATE('UTC') GROUP BY status ORDER BY status"
    )
    scored_rows = runner(
        f"SELECT COUNT(*) AS scored_count FROM `{project_id}.{dataset_id}.eval_results` "  # noqa: S608
        f"WHERE eval_run_id IN ('m16_conc_call_a', 'm16_conc_call_b')"
    )

    runner(
        f"UPDATE `{project_id}.{dataset_id}.eval_daily_quota_config` "  # noqa: S608
        f"SET daily_judge_token_cap = 2000000, per_app_default_token_cap = 500000, updated_at = CURRENT_TIMESTAMP() "
        f"WHERE quota_name = 'default_step2_daily_cap';"
    )

    cap = 35_000
    total_reserved = sum(to_int(r.get("reserved_tokens")) for r in res_rows)
    burned_and_reserved = (
        to_int(status_rows[0].get("tokens_burned_and_reserved_today")) if status_rows else 0
    )
    scored_cnt = to_int(scored_rows[0]["scored_count"]) if scored_rows else 0
    winner_ok = any(c["returncode"] == 0 for c in call_results)
    losers_occ_only = all(
        c["returncode"] == 0
        or "concurrent update" in str(c.get("detail", "")).lower()
        or "aborted" in str(c.get("detail", "")).lower()
        for c in call_results
    )
    if (
        not winner_ok
        or not losers_occ_only
        or burned_and_reserved > cap
        or (total_reserved == 0 and scored_cnt == 0)
    ):
        raise RuntimeError(
            f"Concurrent quota check failed: calls={call_results}, "
            f"burned_and_reserved={burned_and_reserved}, reserved={total_reserved}, cap={cap}"
        )

    return {
        "cap_tokens": cap,
        "concurrent_calls": call_results,
        "reservations_by_status": [dict(r) for r in res_rows],
        "total_reserved_tokens": total_reserved,
        "tokens_burned_and_reserved_today": burned_and_reserved,
        "zero_overspend": burned_and_reserved <= cap,
        "scored_rows_committed": scored_cnt,
        "quota_status_snapshot": dict(status_rows[0]) if status_rows else {},
    }


def verify_live_views_and_sync_jobs(project_id: str, dataset_id: str) -> dict[str, Any]:
    """Query live `S-15` views and fold `INFORMATION_SCHEMA.JOBS_BY_PROJECT` into `bq_query_jobs`."""
    runner = bq_cli_query_runner(
        project_id, labels={"apphub_application_id": "cliff-benchmark-app"}
    )
    sync_sql = build_information_schema_jobs_sync_sql(
        project_id=project_id,
        dataset_id=dataset_id,
        region="us",
        lookback_days=7,
    )
    runner(sync_sql)

    view_cnt_rows = runner(
        f"SELECT COUNT(*) AS view_count FROM `{project_id}.{dataset_id}.INFORMATION_SCHEMA.VIEWS`"  # noqa: S608
    )
    key_views = (
        "v_step1_trace_eval",
        "v_outcomes",
        "v_infra_cost_estimate",
        "v_cost_per_verified_outcome",
        "v_bq_query_cost_attribution",
        "v_agent_eval_and_finops_summary",
        "v_focus_three_pool_hourly_chargeback",
    )
    view_row_counts: dict[str, int] = {}
    for vname in key_views:
        r = runner(f"SELECT COUNT(*) AS cnt FROM `{project_id}.{dataset_id}.{vname}`")  # noqa: S608
        view_row_counts[vname] = to_int(r[0]["cnt"]) if r else 0

    job_cost_rows = runner(
        f"SELECT apphub_application_id, query_job_count, total_bytes_billed, total_slot_ms, bq_query_cost_usd "  # noqa: S608
        f"FROM `{project_id}.{dataset_id}.v_bq_query_cost_attribution` ORDER BY apphub_application_id"
    )
    cpvo_rows = runner(
        f"SELECT apphub_application_id, source_mode, total_outcomes, verified_outcomes, "  # noqa: S608
        f"measured_model_cost_usd, estimated_infra_cost_usd, cost_per_verified_outcome_ai_usd, cost_per_verified_outcome_usd "
        f"FROM `{project_id}.{dataset_id}.v_cost_per_verified_outcome` "
        f"WHERE apphub_application_id = 'cliff-benchmark-app' ORDER BY source_mode"
    )
    return {
        "deployed_view_count": to_int(view_cnt_rows[0]["view_count"]) if view_cnt_rows else 0,
        "queried_view_row_counts": view_row_counts,
        "bq_query_cost_attribution": [dict(r) for r in job_cost_rows],
        "cliff_cpvo_by_arm": [dict(r) for r in cpvo_rows],
    }


def export_apphub_traces_and_sme_candidates(
    *,
    source_project_id: str,
    source_dataset_id: str,
    traces_out: Path,
    anonymized_traces_out: Path | None = None,
    limit: int = 120,
) -> dict[str, Any]:
    """Export `N >= 100` anonymized real traces + App Hub ownership join receipts from Scratchpad fleet."""
    runner = bq_cli_query_runner(source_project_id)
    sensitive = (source_project_id,)

    coverage_rows = runner(
        f"SELECT apphub_application_id, total_traces, attributed_traces, attribution_coverage_pct, "  # noqa: S608
        f"ROUND(attributed_spend_usd, 6) AS attributed_spend_usd, "
        f"ROUND(unattributed_spend_usd, 6) AS unattributed_spend_usd "
        f"FROM `{source_project_id}.{source_dataset_id}.v_attribution_coverage` "
        f"ORDER BY apphub_application_id"
    )

    joined_rows = runner(
        f"WITH dedup_eval AS ("  # noqa: S608
        f"  SELECT trace_id, rubric_id, ordinal_score, binary_pass "
        f"  FROM `{source_project_id}.{source_dataset_id}.eval_results` "
        f"  WHERE ordinal_score BETWEEN 1 AND 4 "
        f"  QUALIFY ROW_NUMBER() OVER (PARTITION BY trace_id, rubric_id ORDER BY evaluated_at DESC) = 1"
        f") "
        f"SELECT "
        f"  t.trace_id, "
        f"  t.apphub_application_id, "
        f"  t.outcome_id, "
        f"  t.source_mode, "
        f"  t.step1_pass, "
        f"  t.total_input_tokens, "
        f"  t.total_output_tokens, "
        f"  ROUND(t.trace_cost_usd, 6) AS trace_cost_usd, "
        f"  COALESCE(NULLIF(p.user_prompt, ''), 'Verify applicant residential address against policy.') AS user_prompt, "
        f"  COALESCE(NULLIF(p.retrieved_context, ''), CONCAT('Application=', t.apphub_application_id, ' mode=', COALESCE(t.source_mode, 'PDF_PATH_B_FLASH'))) AS retrieved_context, "
        f"  COALESCE(NULLIF(p.final_response, ''), IF(COALESCE(e.binary_pass, t.step1_pass), 'Approved within policy.', 'Rejected: policy defect detected.')) AS final_response, "
        f"  e.rubric_id, "
        f"  e.ordinal_score AS judge_ordinal_score "
        f"FROM `{source_project_id}.{source_dataset_id}.v_step1_trace_eval` AS t "
        f"LEFT JOIN `{source_project_id}.{source_dataset_id}.trace_payloads` AS p "
        f"  ON t.trace_id = p.trace_id "
        f"INNER JOIN dedup_eval AS e "
        f"  ON t.trace_id = e.trace_id "
        f"WHERE t.step1_pass = TRUE "
        f"ORDER BY e.ordinal_score ASC, t.trace_id ASC "
        f"LIMIT {int(limit)}"
    )
    if len(joined_rows) < 100:
        raise RuntimeError(
            f"Expected >= 100 judged Step-1 passing traces in {source_project_id}.{source_dataset_id}, got {len(joined_rows)}"
        )

    traces_out.parent.mkdir(parents=True, exist_ok=True)
    clean_rows: list[dict[str, Any]] = []
    sanitized_lines: list[str] = []
    for row in joined_rows:
        clean_row = {
            "trace_id": str(row["trace_id"]),
            "apphub_application_id": sanitize_text(row["apphub_application_id"], sensitive),
            "outcome_id": str(row["outcome_id"]) if row.get("outcome_id") else None,
            "source_mode": str(row["source_mode"]) if row.get("source_mode") else None,
            "step1_pass": bool(row["step1_pass"]),
            "total_input_tokens": to_int(row.get("total_input_tokens")),
            "total_output_tokens": to_int(row.get("total_output_tokens")),
            "trace_cost_usd": to_float(row.get("trace_cost_usd")),
            "user_prompt": sanitize_text(row["user_prompt"], sensitive),
            "retrieved_context": sanitize_text(row["retrieved_context"], sensitive),
            "final_response": sanitize_text(row["final_response"], sensitive),
            "rubric_id": str(row["rubric_id"]),
            "judge_ordinal_score": to_int(row.get("judge_ordinal_score")),
        }
        clean_rows.append(clean_row)
        sanitized_lines.append(json.dumps(clean_row, sort_keys=True))

    traces_bytes = ("\n".join(sanitized_lines) + "\n").encode("utf-8")
    traces_out.write_bytes(traces_bytes)

    anon_path = (
        anonymized_traces_out
        if anonymized_traces_out is not None
        else traces_out.with_name(f"{traces_out.stem}_anonymized.jsonl")
    )
    anon_path.parent.mkdir(parents=True, exist_ok=True)
    blind_rows = redact_and_shuffle_traces(clean_rows)
    anon_lines = [json.dumps(item, sort_keys=True) for item in blind_rows]
    anon_bytes = ("\n".join(anon_lines) + "\n").encode("utf-8")
    anon_path.write_bytes(anon_bytes)

    return {
        "exported_trace_count": len(blind_rows),
        "raw_traces_sha256": hashlib.sha256(traces_bytes).hexdigest(),
        "traces_sha256": hashlib.sha256(anon_bytes).hexdigest(),
        "apphub_attribution_coverage": [
            {
                "apphub_application_id": sanitize_text(c["apphub_application_id"], sensitive),
                "total_traces": to_int(c.get("total_traces")),
                "attributed_traces": to_int(c.get("attributed_traces")),
                "attribution_coverage_pct": to_float(c.get("attribution_coverage_pct")),
                "attributed_spend_usd": to_float(c.get("attributed_spend_usd")),
                "unattributed_spend_usd": to_float(c.get("unattributed_spend_usd")),
            }
            for c in coverage_rows
        ],
    }


def apply_terraform_and_bootstrap(
    *,
    project_id: str,
    dataset_id: str,
    create_shared_bucket: bool = False,
) -> dict[str, Any]:
    """Run `terraform apply` (with OAuth token) + `bq-eval bootstrap --mode full --apply` + `--dry-run`."""
    tf_bin = shutil.which("terraform")
    if tf_bin is None:
        raise OSError("`terraform` CLI not found on PATH.")
    token = gcloud_access_token()
    tf_dir = REPO_ROOT / "terraform"
    state_path = Path(f"/tmp/bq_eval_m16_{project_id}.tfstate")  # noqa: S108
    env = {**os.environ, "GOOGLE_OAUTH_ACCESS_TOKEN": token}

    proc = subprocess.run(  # noqa: S603
        [
            tf_bin,
            f"-chdir={tf_dir}",
            "apply",
            "-input=false",
            "-auto-approve",
            f"-state={state_path}",
            f"-var=project_id={project_id}",
            f"-var=eval_dataset_id={dataset_id}",
            f"-var=create_shared_bucket={'true' if create_shared_bucket else 'false'}",
            "-var=enable_scheduled_judge=false",
            "-var=power_policy=hibernate",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    tf_rc = int(proc.returncode)
    if tf_rc != 0:
        raise RuntimeError(f"terraform apply failed (rc={tf_rc}):\n{proc.stdout}\n{proc.stderr}")

    bootstrap_rcs: dict[str, int] = {}
    for flag, label in (("--apply", "apply"), ("--dry-run", "dry-run")):
        rc = 1
        for attempt in range(4):
            rc = int(
                cli_main(
                    [
                        "bootstrap",
                        "--mode",
                        "full",
                        "--project-id",
                        project_id,
                        "--dataset-id",
                        dataset_id,
                        flag,
                        "--format",
                        "json",
                    ]
                )
            )
            if rc == 0:
                break
            time.sleep(10.0 * (attempt + 1))
        bootstrap_rcs[label] = rc
        if rc != 0:
            raise RuntimeError(f"`bq-eval bootstrap --mode full {label}` exited {rc}.")

    return {
        "provision_infra_requested": True,
        "terraform_applied": tf_rc == 0,
        "terraform_exit_code": tf_rc,
        "power_policy": "hibernate",
        "bootstrap_apply_passed": bootstrap_rcs["apply"] == 0,
        "bootstrap_apply_exit_code": bootstrap_rcs["apply"],
        "bootstrap_dry_run_passed": bootstrap_rcs["dry-run"] == 0,
        "bootstrap_dry_run_exit_code": bootstrap_rcs["dry-run"],
    }


def _ensure_cliff_arms(
    cfg: M16LiveProofConfig,
) -> tuple[list[OTelGenAISpanRecord], list[OTelGenAISpanRecord]]:
    short_spans: list[OTelGenAISpanRecord] = []
    long_spans: list[OTelGenAISpanRecord] = []
    if cfg.reuse_ingested_cliff:
        short_spans, long_spans = load_ingested_cliff_spans(cfg.target_project_id, cfg.dataset_id)
    if len(short_spans) >= cfg.per_arm and len(long_spans) >= cfg.per_arm:
        return short_spans, long_spans

    token = gcloud_access_token()
    short_spans = []
    long_spans = []
    all_otel_spans: list[OTelGenAISpanRecord] = []
    all_logs: list[dict[str, Any]] = []
    endpoint = VertexEndpointConfig(
        project_id=cfg.target_project_id,
        location=cfg.vertex_location,
        model_name=cfg.model_name,
        token=token,
    )
    tasks = [("short", i) for i in range(cfg.per_arm)] + [("long", i) for i in range(cfg.per_arm)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(call_vertex_gemini_single_case, endpoint, arm=arm, case_idx=idx): arm
            for arm, idx in tasks
        }
        for fut in concurrent.futures.as_completed(futures):
            arm = futures[fut]
            root_sp, close_sp, log_rec = fut.result()
            if arm == "short":
                short_spans.append(root_sp)
            else:
                long_spans.append(root_sp)
            all_otel_spans.extend((root_sp, close_sp))
            all_logs.append(log_rec)
    export_live_otlp_and_logs(
        project_id=cfg.target_project_id,
        spans=all_otel_spans,
        log_payloads=all_logs,
        token=gcloud_access_token(),
    )
    return short_spans, long_spans


def run_live_m16_proof(cfg: M16LiveProofConfig) -> dict[str, Any]:
    for pid in (cfg.target_project_id, cfg.source_project_id, cfg.dataset_id):
        if not _IDENT_RE.fullmatch(pid):
            raise ValueError(f"Invalid identifier: {pid!r}")

    upload_seed_completion_jsonl(cfg.target_project_id)

    deploy_status: dict[str, Any] = {
        "provision_infra_requested": False,
        "terraform_applied": False,
        "terraform_exit_code": None,
        "power_policy": None,
        "bootstrap_apply_passed": False,
        "bootstrap_apply_exit_code": None,
        "bootstrap_dry_run_passed": False,
        "bootstrap_dry_run_exit_code": None,
    }
    if cfg.provision_infra:
        deploy_status = apply_terraform_and_bootstrap(
            project_id=cfg.target_project_id,
            dataset_id=cfg.dataset_id,
            create_shared_bucket=False,
        )

    short_spans, long_spans = _ensure_cliff_arms(cfg)
    cliff_report = analyze_unconfounded_context_cliff_arms(
        short_spans,
        long_spans,
        min_per_arm=cfg.per_arm,
    )
    ingested = _wait_for_live_traces(
        cfg.target_project_id,
        cfg.dataset_id,
        min_spans=cfg.per_arm * 4,
        timeout_s=180,
    )
    concurrency_proof = verify_concurrent_quota_lock_live(cfg.target_project_id, cfg.dataset_id)
    views_and_jobs = verify_live_views_and_sync_jobs(cfg.target_project_id, cfg.dataset_id)
    apphub_export = export_apphub_traces_and_sme_candidates(
        source_project_id=cfg.source_project_id,
        source_dataset_id=cfg.dataset_id,
        traces_out=cfg.traces_out,
        anonymized_traces_out=cfg.anonymized_traces_out,
        limit=120,
    )

    receipt = {
        "schema_version": "m16-live-proof-v1",
        "captured_date_utc": datetime.now(UTC).strftime("%Y-%m-%d"),
        "target_environment": "showcases-standalone-bigquery",
        "infrastructure_bootstrap": deploy_status,
        "unconfounded_context_cliff": asdict(cliff_report),
        "live_otlp_ingestion": ingested,
        "s5_concurrent_quota_lock": concurrency_proof,
        "s15_views_and_job_costs": views_and_jobs,
        "apphub_fleet_export": apphub_export,
    }
    cfg.receipt_out.parent.mkdir(parents=True, exist_ok=True)
    cfg.receipt_out.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute M16 live BigQuery & Vertex AI proof.")
    parser.add_argument("--target-project-id", required=True)
    parser.add_argument("--source-project-id", required=True)
    parser.add_argument("--dataset-id", default="agent_eval")
    parser.add_argument("--model-name", default="gemini-3.8-flash")
    parser.add_argument("--vertex-location", default="global")
    parser.add_argument("--per-arm", type=int, default=30)
    parser.add_argument("--provision-infra", action="store_true")
    parser.add_argument("--reuse-ingested-cliff", action="store_true")
    parser.add_argument(
        "--receipt-out",
        type=Path,
        default=Path("benchmarks/results/m16_live_cloud_proof.json"),
    )
    parser.add_argument(
        "--traces-out",
        type=Path,
        default=Path("benchmarks/results/m16_live_traces_120.jsonl"),
    )
    parser.add_argument(
        "--anonymized-traces-out",
        type=Path,
        default=Path("benchmarks/results/m16_anonymized_traces_100.jsonl"),
    )
    args = parser.parse_args(argv)
    receipt = run_live_m16_proof(
        M16LiveProofConfig(
            target_project_id=args.target_project_id,
            source_project_id=args.source_project_id,
            dataset_id=args.dataset_id,
            model_name=args.model_name,
            vertex_location=args.vertex_location,
            per_arm=args.per_arm,
            provision_infra=bool(args.provision_infra),
            reuse_ingested_cliff=bool(args.reuse_ingested_cliff),
            receipt_out=args.receipt_out,
            traces_out=args.traces_out,
            anonymized_traces_out=args.anonymized_traces_out,
        )
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
