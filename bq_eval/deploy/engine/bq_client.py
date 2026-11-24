"""BigQuery CLI subprocess `QueryRunner` and bootstrap applier (isolated inside `deploy/engine`)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from bq_eval.core.engine.ports import QueryRunner
from bq_eval.core.engine.sql_eval import _validate_sql_identifier
from bq_eval.deploy.contract.bootstrap import BootstrapMode

_BQ_LABEL_RE = re.compile(r"^[a-z0-9_-]{1,63}$")


def format_bq_job_labels(extra_labels: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Build validated `--label=k:v` flags always stamping `workload:bq-eval`."""
    merged: dict[str, str] = {"workload": "bq-eval"}
    if extra_labels:
        for raw_k, raw_v in extra_labels.items():
            k = raw_k.strip().lower()
            v = raw_v.strip().lower()
            if not _BQ_LABEL_RE.fullmatch(k) or not _BQ_LABEL_RE.fullmatch(v):
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
        if not raw or not raw.startswith("["):
            return []
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

    subprocess.run(  # noqa: S603
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


def apply_bootstrap_statements(
    project_id: str,
    stmts: Sequence[tuple[str, str]],
    *,
    mode: BootstrapMode = "full",
    ensure_traces_link: bool = True,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    """Execute rendered bootstrap DDL, seeds, procedures, and views sequentially on BigQuery."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    bq_bin = shutil.which("bq")
    if bq_bin is None:
        raise ValueError("`bq` CLI not found on PATH; install Google Cloud SDK to use --apply.")

    errors: list[str] = []
    if ensure_traces_link and mode in ("full", "hybrid"):
        errors.extend(ensure_observability_traces_link(valid_project))
        if errors:
            return errors

    label_flags = format_bq_job_labels(labels)
    for label, stmt in stmts:
        proc = subprocess.run(  # noqa: S603
            [
                bq_bin,
                "query",
                f"--project_id={valid_project}",
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
            errors.append(f"{label}: bq apply failed: {detail[:240]}")
    return errors
