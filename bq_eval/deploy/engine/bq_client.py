"""BigQuery CLI subprocess `QueryRunner` and bootstrap applier (isolated inside `deploy/engine`)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from bq_eval.core.engine.ports import QueryRunner
from bq_eval.core.engine.sql_eval import _validate_sql_identifier
from bq_eval.deploy.contract.bootstrap import BootstrapMode
from bq_eval.deploy.engine.silver_deploy import build_silver_routine_authorizations

_BQ_LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_BQ_LABEL_VAL_RE = re.compile(r"^[a-z0-9_-]{0,63}$")
_BENIGN_NON_JSON_PREFIXES: tuple[str, ...] = (
    "number of affected rows",
    "waiting on bqjob",
    "current status:",
)


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
) -> QueryRunner:
    """Return a `QueryRunner` that executes GoogleSQL via `bq` with `workload:bq-eval` job labels."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    label_flags = format_bq_job_labels(labels)

    def _run(sql: str) -> Sequence[Mapping[str, object]]:
        bq_bin = shutil.which("bq")
        if bq_bin is None:
            raise OSError("`bq` CLI not found on PATH; install Google Cloud SDK or pass a runner.")
        proc = subprocess.run(  # noqa: S603
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


def ensure_observability_traces_link(project_id: str, *, location: str = "us") -> list[str]:
    """Idempotently configure `_Trace` storage location and `obs_traces` linked BigQuery dataset."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_loc = _validate_sql_identifier(location.lower(), "location", allow_hyphen=True)
    gcloud_bin = shutil.which("gcloud")
    if gcloud_bin is None:
        return ["`gcloud` CLI not found on PATH; cannot ensure `obs_traces` observability link."]

    settings_proc = subprocess.run(  # noqa: S603
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

    desc = subprocess.run(  # noqa: S603
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

    created = subprocess.run(  # noqa: S603
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


def apply_dataset_routine_authorizations(
    project_id: str,
    authorizations: Mapping[str, Sequence[tuple[str, str, str]]],
) -> list[str]:
    """Re-apply BigQuery dataset routine authorizations after `CREATE OR REPLACE TABLE FUNCTION` (`S-21`)."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    bq_bin = shutil.which("bq")
    if bq_bin is None:
        return ["`bq` CLI not found on PATH; cannot apply dataset routine authorizations."]

    for target_dataset, routines in authorizations.items():
        if not routines:
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
        show = subprocess.run(  # noqa: S603
            [bq_bin, "show", "--format=prettyjson", f"{valid_project}:{valid_target}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if show.returncode != 0:
            detail = (show.stderr or show.stdout).strip()
            return [f"routine-auth show {valid_target} failed: {detail[:240]}"]
        meta = json.loads(show.stdout)
        access: list[dict[str, object]] = list(meta.get("access", []))
        # Strip stale routine entries for routines we are re-applying so BigQuery re-evaluates auth.
        desired_keys = set(validated_routines)
        filtered: list[dict[str, object]] = []
        for entry in access:
            routine_obj = entry.get("routine")
            if isinstance(routine_obj, dict):
                rkey = (
                    routine_obj.get("projectId"),
                    routine_obj.get("datasetId"),
                    routine_obj.get("routineId"),
                )
                if rkey in desired_keys:
                    continue
            filtered.append(entry)
        for rp, rd, rn in validated_routines:
            filtered.append({"routine": {"projectId": rp, "datasetId": rd, "routineId": rn}})
        meta["access"] = filtered
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        ) as tmp:
            json.dump(meta, tmp)
            tmp_path = Path(tmp.name)
        try:
            upd = subprocess.run(  # noqa: S603
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


def apply_bootstrap_statements(  # noqa: PLR0913
    project_id: str,
    stmts: Sequence[tuple[str, str]],
    *,
    mode: BootstrapMode = "full",
    location: str = "us",
    ensure_traces_link: bool = False,
    apply_silver_routine_auth: bool = True,
    silver_internal_dataset: str = "silver_internal",
    silver_ref_dataset: str = "silver_ref",
    silver_open_dataset: str = "silver_open",
    silver_restricted_dataset: str = "silver_restricted",
    traces_dataset_id: str = "obs_traces",
    platform_traces_dataset_id: str = "silver_internal",
    logs_dataset_id: str = "obs_logs",
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    """Execute rendered bootstrap DDL, functions, and views sequentially, stopping on first failure (`S-21`)."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_loc = _validate_sql_identifier(location, "location", allow_hyphen=True)
    bq_bin = shutil.which("bq")
    if bq_bin is None:
        raise ValueError("`bq` CLI not found on PATH; install Google Cloud SDK to use --apply.")

    if ensure_traces_link and mode in ("full", "hybrid", "silver"):
        link_errors = ensure_observability_traces_link(valid_project, location=valid_loc)
        if link_errors:
            return link_errors

    label_flags = format_bq_job_labels(labels)
    for label, stmt in stmts:
        proc = subprocess.run(  # noqa: S603
            [
                bq_bin,
                "query",
                f"--project_id={valid_project}",
                f"--location={valid_loc}",
                "--use_legacy_sql=false",
                "--format=none",
                *label_flags,
            ],
            input=stmt,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stdout + "\n" + proc.stderr).strip()
            return [f"{label}: bq apply failed: {detail[:240]}"]

    if mode == "silver" and apply_silver_routine_auth:
        auth_map = build_silver_routine_authorizations(
            project_id=valid_project,
            silver_internal_dataset=silver_internal_dataset,
            silver_ref_dataset=silver_ref_dataset,
            silver_open_dataset=silver_open_dataset,
            silver_restricted_dataset=silver_restricted_dataset,
            traces_dataset_id=traces_dataset_id,
            platform_traces_dataset_id=platform_traces_dataset_id,
            logs_dataset_id=logs_dataset_id,
        )
        return apply_dataset_routine_authorizations(valid_project, auth_map)
    return []
