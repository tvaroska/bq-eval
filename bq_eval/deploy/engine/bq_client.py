"""BigQuery CLI subprocess `QueryRunner` and bootstrap applier (isolated inside `deploy/engine`)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from bq_eval.core.engine.ports import QueryRunner
from bq_eval.core.engine.sql_eval import _validate_sql_identifier
from bq_eval.deploy.contract.bootstrap import BootstrapMode
from bq_eval.deploy.engine.silver_deploy import (
    build_gold_view_authorizations,
    build_silver_routine_authorizations,
)

_BQ_LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_BQ_LABEL_VAL_RE = re.compile(r"^[a-z0-9_-]{0,63}$")
_MAX_BATCH_SCRIPT_BYTES: int = 512_000
_SILVER_BASE_FILES: frozenset[str] = frozenset(
    {"silver/reference.sql", "silver/workflow_events.sql"}
)
_GOLD_CORE_FILES: frozenset[str] = frozenset(
    {
        "core/ddl.sql",
        "core/pricing_seed.sql",
        "core/gold_cost_views.sql",
        "core/gold_eval_gates.sql",
        "core/step2_stateless_eval.sql",
    }
)
_BENIGN_NON_JSON_PREFIXES: tuple[str, ...] = (
    "number of affected rows",
    "waiting on bqjob",
    "current status:",
)

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]
WhichFn = Callable[[str], str | None]


def _bootstrap_stage_for_label(label: str) -> str:
    rel_file = label.split("#", 1)[0]
    if rel_file in _SILVER_BASE_FILES:
        return "silver_base"
    if rel_file == "silver/shape3.sql":
        return "silver_shape3"
    if rel_file.startswith("silver/"):
        return "silver_routines"
    if rel_file in _GOLD_CORE_FILES:
        return "gold_core"
    if rel_file.startswith(("core/", "finops/")):
        return "gold_summary_and_finops"
    return "custom"


def format_batch_error_label(batch_name: str, labels: Sequence[str]) -> str:
    """Return single statement label for 1-stmt batches or `<stage> [<first>..<last>]` for multi-stmt batches."""
    if len(labels) == 1:
        return labels[0]
    return f"{batch_name} [{labels[0]}..{labels[-1]}]"


def batch_bootstrap_statements(
    stmts: Sequence[tuple[str, str]],
    *,
    max_script_bytes: int = _MAX_BATCH_SCRIPT_BYTES,
) -> list[tuple[str, tuple[str, ...], str]]:
    """Group ordered statements into `3-5` topological multi-statement BigQuery scripts.

    Preserves exact statement order, isolates `silver/shape3.sql` for Day-0 stand-in fallback,
    and enforces a per-script byte ceiling below BigQuery's 1 MB script query limit.
    """
    if max_script_bytes < 1:
        raise ValueError(f"max_script_bytes must be positive, got {max_script_bytes}")
    batches: list[tuple[str, tuple[str, ...], str]] = []
    cur_stage: str | None = None
    cur_labels: list[str] = []
    cur_chunks: list[str] = []
    cur_bytes = 0

    def _flush() -> None:
        nonlocal cur_stage, cur_labels, cur_chunks, cur_bytes
        if cur_stage is not None and cur_chunks:
            script = "\n;\n\n".join(cur_chunks) + "\n;\n"
            batches.append((cur_stage, tuple(cur_labels), script))
        cur_stage = None
        cur_labels = []
        cur_chunks = []
        cur_bytes = 0

    for label, raw_stmt in stmts:
        cleaned = raw_stmt.strip().rstrip(";").strip()
        if not cleaned:
            continue
        stage = _bootstrap_stage_for_label(label)
        chunk_bytes = len(cleaned.encode("utf-8")) + 4
        if cur_stage is not None and (
            stage != cur_stage or (cur_chunks and cur_bytes + chunk_bytes > max_script_bytes)
        ):
            _flush()
        cur_stage = stage
        cur_labels.append(label)
        cur_chunks.append(cleaned)
        cur_bytes += chunk_bytes

    _flush()
    return batches


def format_bq_job_labels(extra_labels: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Build validated `--label=k:v` flags always stamping `workload:bq-eval` (`S-26`)."""
    merged: dict[str, str] = {"workload": "bq-eval"}
    if extra_labels:
        for raw_k, raw_v in extra_labels.items():
            k = raw_k.strip().lower()
            v = raw_v.strip().lower()
            if not _BQ_LABEL_KEY_RE.fullmatch(k) or not _BQ_LABEL_VAL_RE.fullmatch(v):
                raise ValueError(f"Invalid BigQuery job label {raw_k!r}={raw_v!r}")
            merged[k] = v
    return tuple(f"--label={k}:{v}" for k, v in sorted(merged.items()))


def bq_cli_query_runner(
    project_id: str,
    *,
    labels: Mapping[str, str] | None = None,
    which_fn: WhichFn = shutil.which,
    run_cmd: SubprocessRunner = subprocess.run,
) -> QueryRunner:
    """Return a `QueryRunner` that executes GoogleSQL via `bq` with `workload:bq-eval` job labels."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    label_flags = format_bq_job_labels(labels)

    def _run(sql: str) -> Sequence[Mapping[str, object]]:
        bq_bin = which_fn("bq")
        if bq_bin is None:
            raise OSError("`bq` CLI not found on PATH; install Google Cloud SDK or pass a runner.")
        proc = run_cmd(
            [
                bq_bin,
                "query",
                "--quiet",
                f"--project_id={valid_project}",
                "--use_legacy_sql=false",
                "--format=json",
                *label_flags,
            ],
            input=sql,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise OSError(f"BigQuery query failed: {(proc.stderr or proc.stdout).strip()}")
        raw = proc.stdout.strip()
        if not raw:
            return []
        if not raw.startswith("["):
            if raw.lower().startswith(_BENIGN_NON_JSON_PREFIXES):
                return []
            raise ValueError(f"Unexpected non-JSON stdout from `bq query`: {raw[:200]!r}")
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("Expected JSON list output from `bq query --format=json`.")
        return parsed

    return _run


def ensure_observability_traces_link(
    project_id: str,
    *,
    location: str = "us",
    which_fn: WhichFn = shutil.which,
    run_cmd: SubprocessRunner = subprocess.run,
) -> list[str]:
    """Idempotently configure `_Trace` storage location and `obs_traces` linked BigQuery dataset."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_loc = _validate_sql_identifier(location.lower(), "location", allow_hyphen=True)
    gcloud_bin = which_fn("gcloud")
    if gcloud_bin is None:
        return ["`gcloud` CLI not found on PATH; cannot ensure `obs_traces` observability link."]

    settings_proc = run_cmd(
        [
            gcloud_bin,
            "observability",
            "settings",
            "update",
            "--location=global",
            "--update-mask=default_storage_location",
            f"--default-storage-location={valid_loc}",
            f"--project={valid_project}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if settings_proc.returncode != 0:
        detail = (settings_proc.stderr or settings_proc.stdout).strip()
        return [f"observability settings update failed: {detail[:240]}"]

    desc = run_cmd(
        [
            gcloud_bin,
            "observability",
            "buckets",
            "datasets",
            "links",
            "describe",
            "obs_traces",
            "--bucket=_Trace",
            "--dataset=Spans",
            f"--location={valid_loc}",
            f"--project={valid_project}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if desc.returncode == 0:
        return []

    created = run_cmd(
        [
            gcloud_bin,
            "observability",
            "buckets",
            "datasets",
            "links",
            "create",
            "obs_traces",
            "--bucket=_Trace",
            "--dataset=Spans",
            f"--location={valid_loc}",
            f"--project={valid_project}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        detail = (created.stderr or created.stdout).strip()
        if "already exists" not in detail.lower():
            return [f"obs_traces link creation failed: {detail[:240]}"]
    return []


def _merge_dataset_access_entries(
    access: Sequence[dict[str, object]],
    validated_routines: Sequence[tuple[str, str, str]],
    validated_views: Sequence[tuple[str, str, str]],
) -> list[dict[str, object]]:
    desired_rkeys = set(validated_routines)
    desired_vkeys = set(validated_views)
    filtered: list[dict[str, object]] = []
    for entry in access:
        routine_obj = entry.get("routine")
        if (
            isinstance(routine_obj, dict)
            and (
                routine_obj.get("projectId"),
                routine_obj.get("datasetId"),
                routine_obj.get("routineId"),
            )
            in desired_rkeys
        ):
            continue
        view_obj = entry.get("view")
        if (
            isinstance(view_obj, dict)
            and (
                view_obj.get("projectId"),
                view_obj.get("datasetId"),
                view_obj.get("tableId"),
            )
            in desired_vkeys
        ):
            continue
        filtered.append(entry)
    filtered.extend(
        {"routine": {"projectId": rp, "datasetId": rd, "routineId": rn}}
        for rp, rd, rn in validated_routines
    )
    filtered.extend(
        {"view": {"projectId": vp, "datasetId": vd, "tableId": vt}}
        for vp, vd, vt in validated_views
    )
    return filtered


def apply_dataset_routine_authorizations(
    project_id: str,
    authorizations: Mapping[str, Sequence[tuple[str, str, str]]],
    *,
    view_authorizations: Mapping[str, Sequence[tuple[str, str, str]]] | None = None,
    which_fn: WhichFn = shutil.which,
    run_cmd: SubprocessRunner = subprocess.run,
) -> list[str]:
    """Re-apply BigQuery dataset routine & authorized-view ACLs after DDL updates (`S-21`)."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    bq_bin = which_fn("bq")
    if bq_bin is None:
        return ["`bq` CLI not found on PATH; cannot apply dataset routine authorizations."]

    view_map = view_authorizations or {}
    for target_dataset in dict.fromkeys((*authorizations.keys(), *view_map.keys())):
        routines = authorizations.get(target_dataset, ())
        views = view_map.get(target_dataset, ())
        if not routines and not views:
            continue
        valid_target = _validate_sql_identifier(target_dataset, "target_dataset")
        validated_routines = tuple(
            (
                _validate_sql_identifier(rp, "routine_project", allow_hyphen=True),
                _validate_sql_identifier(rd, "routine_dataset"),
                _validate_sql_identifier(rn, "routine_id"),
            )
            for rp, rd, rn in routines
        )
        validated_views = tuple(
            (
                _validate_sql_identifier(vp, "view_project", allow_hyphen=True),
                _validate_sql_identifier(vd, "view_dataset"),
                _validate_sql_identifier(vt, "table_id"),
            )
            for vp, vd, vt in views
        )
        show = run_cmd(
            [bq_bin, "show", "--format=prettyjson", f"{valid_project}:{valid_target}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if show.returncode != 0:
            detail = (show.stderr or show.stdout).strip()
            return [f"routine-auth show {valid_target} failed: {detail[:240]}"]
        meta = json.loads(show.stdout)
        merged_access = _merge_dataset_access_entries(
            list(meta.get("access", [])), validated_routines, validated_views
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        ) as tmp:
            json.dump({"access": merged_access}, tmp)
            tmp_path = Path(tmp.name)
        try:
            upd = run_cmd(
                [bq_bin, "update", f"--source={tmp_path}", f"{valid_project}:{valid_target}"],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            tmp_path.unlink(missing_ok=True)
        if upd.returncode != 0:
            detail = (upd.stderr or upd.stdout).strip()
            return [f"routine-auth update {valid_target} failed: {detail[:240]}"]
    return []


def _apply_empty_shape3_standin(
    bq_cmd_base: Sequence[str],
    project_id: str,
    silver_restricted_dataset: str,
    run_cmd: SubprocessRunner,
) -> list[str]:
    """Create a Day-0 native `shape3_payloads` stand-in when the GCS prefix has zero objects."""
    valid_restricted = _validate_sql_identifier(
        silver_restricted_dataset, "silver_restricted_dataset"
    )
    standin_ddl = (
        f"CREATE TABLE IF NOT EXISTS `{project_id}.{valid_restricted}.shape3_payloads` ("
        "ref_uri STRING, payload JSON, dt DATE, hr STRING"
        ") PARTITION BY dt CLUSTER BY hr "
        "OPTIONS (description = 'Day-0 stand-in table when Shape-3 GCS prefix has zero files.')"
    )
    fallback = run_cmd(
        list(bq_cmd_base),
        input=standin_ddl,
        text=True,
        capture_output=True,
        check=False,
    )
    if fallback.returncode != 0:
        detail = (fallback.stdout + "\n" + fallback.stderr).strip()
        return [f"silver/shape3.sql#1 (stand-in): bq apply failed: {detail[:240]}"]
    return []


def apply_bootstrap_statements(  # noqa: PLR0913
    project_id: str,
    stmts: Sequence[tuple[str, str]],
    *,
    mode: BootstrapMode = "gold",
    dataset_id: str = "agent_eval",
    location: str = "us",
    ensure_traces_link: bool = False,
    apply_silver_routine_auth: bool = True,
    silver_internal_dataset: str = "silver_internal",
    silver_ref_dataset: str = "silver_ref",
    silver_open_dataset: str = "silver_open",
    silver_restricted_dataset: str = "silver_restricted",
    gold_internal_dataset: str = "agent_eval_internal",
    traces_dataset_id: str = "obs_traces",
    platform_traces_dataset_id: str = "silver_internal",
    logs_dataset_id: str = "obs_logs",
    labels: Mapping[str, str] | None = None,
    which_fn: WhichFn = shutil.which,
    run_cmd: SubprocessRunner = subprocess.run,
) -> list[str]:
    """Execute rendered bootstrap DDL, functions, and views sequentially, stopping on first failure (`S-21`)."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_dataset = _validate_sql_identifier(dataset_id, "dataset_id")
    valid_loc = _validate_sql_identifier(location, "location", allow_hyphen=True)
    bq_bin = which_fn("bq")
    if bq_bin is None:
        raise ValueError("`bq` CLI not found on PATH; install Google Cloud SDK to use --apply.")

    if ensure_traces_link and mode in ("silver", "gold"):
        link_errors = ensure_observability_traces_link(
            valid_project,
            location=valid_loc,
            which_fn=which_fn,
            run_cmd=run_cmd,
        )
        if link_errors:
            return link_errors

    bq_cmd_base = [
        bq_bin,
        "query",
        f"--project_id={valid_project}",
        f"--location={valid_loc}",
        "--use_legacy_sql=false",
        "--format=none",
        *format_bq_job_labels(labels),
    ]
    for batch_name, batch_labels, script_sql in batch_bootstrap_statements(stmts):
        proc = run_cmd(
            bq_cmd_base,
            input=script_sql,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stdout + "\n" + proc.stderr).strip()
            lower_detail = detail.lower()
            if batch_name == "silver_shape3" and (
                "without any associated files" in lower_detail
                or "not an external table" in lower_detail
            ):
                standin_errs = _apply_empty_shape3_standin(
                    bq_cmd_base, valid_project, silver_restricted_dataset, run_cmd
                )
                if standin_errs:
                    return standin_errs
                continue
            err_label = format_batch_error_label(batch_name, batch_labels)
            return [f"{err_label}: bq apply failed: {detail[:240]}"]

    if mode in ("silver", "gold") and apply_silver_routine_auth:
        auth_map = build_silver_routine_authorizations(
            project_id=valid_project,
            silver_internal_dataset=silver_internal_dataset,
            silver_ref_dataset=silver_ref_dataset,
            silver_open_dataset=silver_open_dataset,
            silver_restricted_dataset=silver_restricted_dataset,
            traces_dataset_id=traces_dataset_id,
            platform_traces_dataset_id=platform_traces_dataset_id,
            logs_dataset_id=logs_dataset_id,
            gold_dataset_id=valid_dataset if mode == "gold" else None,
        )
        view_auth_map = (
            build_gold_view_authorizations(
                project_id=valid_project,
                gold_dataset_id=valid_dataset,
                gold_internal_dataset=gold_internal_dataset,
                silver_internal_dataset=silver_internal_dataset,
            )
            if mode == "gold"
            else None
        )
        return apply_dataset_routine_authorizations(
            valid_project,
            auth_map,
            view_authorizations=view_auth_map,
            which_fn=which_fn,
            run_cmd=run_cmd,
        )
    return []
