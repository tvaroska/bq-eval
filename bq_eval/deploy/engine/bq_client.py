"""BigQuery CLI subprocess `QueryRunner` adapter (isolated inside `deploy/engine`)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from bq_eval.core.engine.ports import QueryRunner
from bq_eval.core.engine.sql_eval import _validate_sql_identifier


def bq_cli_query_runner(project_id: str) -> QueryRunner:
    """Return a `QueryRunner` that executes read-only GoogleSQL via the `bq` CLI."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)

    def _run(sql: str) -> Sequence[Mapping[str, object]]:
        bq_bin = shutil.which("bq")
        if bq_bin is None:
            raise OSError("`bq` CLI not found on PATH; install Google Cloud SDK or pass a runner.")
        proc = subprocess.run(  # noqa: S603
            [
                bq_bin,
                "query",
                f"--project_id={valid_project}",
                "--use_legacy_sql=false",
                "--format=json",
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
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("Expected JSON list output from `bq query --format=json`.")
        return parsed

    return _run
