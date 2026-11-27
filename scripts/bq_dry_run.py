"""Opt-in live BigQuery dry-run validator and offline statement manifest generator.

Substitutes `agent_eval.`, `agent_eval_seed.`, `obs_traces.`, and `obs_logs.` dataset
prefixes in memory with `--project-id` so repository SQL files remain 100% project-agnostic
while verifying every statement against BigQuery's real SQL parser and schema binder.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)
from bq_eval.deploy.engine.bootstrap import (
    SKIP_DRY_RUN_PREFIXES as SKIP_DRY_RUN_PREFIXES,
)
from bq_eval.deploy.engine.bootstrap import (
    normalize_payload_prefix,
    should_skip_dry_run_statement,
    split_sql_statements_stdlib,
)
from bq_eval.deploy.engine.bootstrap import (
    strip_leading_sql_comments as strip_leading_sql_comments,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"

DATASET_PREFIXES: tuple[str, ...] = (
    "agent_eval",
    "agent_eval_seed",
    "obs_traces",
    "obs_logs",
)


def qualify_project_datasets(  # noqa: PLR0913
    sql: str,
    project_id: str,
    *,
    dataset_id: str = "agent_eval",
    location: str = "US",
    connection_id: str = "us.vertex_ai_connection",
    payload_bucket: str | None = None,
    payload_prefix: str = "bq-eval/",
) -> str:
    """Substitute bootstrap placeholders and prefix bare dataset refs with `project_id.` in memory."""
    bucket = payload_bucket or project_id
    prefix = normalize_payload_prefix(payload_prefix)
    qualified = (
        sql.replace("${PROJECT_ID}", project_id)
        .replace("${LOCATION}", location)
        .replace("${DATASET_ID}", dataset_id)
        .replace("${CONNECTION_ID}", connection_id)
        .replace("${PAYLOAD_BUCKET}", bucket)
        .replace("${PREFIX}", prefix)
        .replace("${SILVER_INTERNAL_DATASET}", "silver_internal")
        .replace("${SILVER_REF_DATASET}", "silver_ref")
        .replace("${SILVER_OPEN_DATASET}", "silver_open")
        .replace("${SILVER_RESTRICTED_DATASET}", "silver_restricted")
        .replace("${TRACES_DATASET_ID}", "obs_traces")
        .replace("${PLATFORM_TRACES_DATASET_ID}", "platform_traces")
        .replace("${PLATFORM_TRACES_TABLE_ID}", "_AllSpans")
        .replace("${LOGS_DATASET_ID}", "obs_logs")
        .replace("${READ_MARGIN_MINUTES}", "60")
        .replace("${SETTLE_MINUTES}", "5")
        .replace("${OUTCOME_LOOKBACK_DAYS}", "30")
        .replace("${GOLD_LOOKBACK_HOURS}", "720")
        .replace("${SHAPE3_PREFIX}", "bq-eval/shape3/")
        .replace("${SHAPE3_FORMAT}", "JSON")
        .replace("${SHAPE3_CACHE_OPTIONS}", "")
        .replace("${GOLD_INTERNAL_DATASET}", "agent_eval_internal")
    )
    for dataset in DATASET_PREFIXES:
        target_ds = dataset_id if dataset == "agent_eval" else dataset
        qualified = qualified.replace(f"`{dataset}`", f"`{project_id}.{target_ds}`")
        qualified = qualified.replace(f"`{dataset}.", f"`{project_id}.{target_ds}.")
    return qualified


def should_skip_dry_run(statement: str) -> bool:
    """Return True for empty/comment-only chunks and bootstrap DDL skipped in `--dry_run`."""
    return should_skip_dry_run_statement(statement)


def split_sql_statements(sql: str) -> list[str]:
    """Split a multi-statement BigQuery SQL script via `bootstrap.split_sql_statements_stdlib`."""
    return split_sql_statements_stdlib(sql)


def _generated_sql_cases(project_id: str) -> list[tuple[str, str]]:
    return [
        (
            "build_ai_generate_table_judge_sql",
            build_ai_generate_table_judge_sql(project_id=project_id, canary_sample_pct=5),
        ),
        (
            "build_ai_evaluate_agent_sql",
            build_ai_evaluate_agent_sql(project_id=project_id, canary_sample_pct=10),
        ),
    ]


def collect_dry_run_manifest(
    project_id: str = "showcase-project",
    files: Sequence[str] | None = None,
    *,
    skip_generated: bool = False,
) -> dict[str, Any]:
    """Build a sanitized statement inventory (`checked` vs. `skipped`) across `sql/**/*.sql`."""
    sql_files = [SQL_DIR / name for name in files] if files else sorted(SQL_DIR.rglob("*.sql"))
    per_file: dict[str, dict[str, int]] = {}
    total_checked = 0
    total_skipped = 0

    for sql_file in sql_files:
        raw = sql_file.read_text(encoding="utf-8")
        qualified = qualify_project_datasets(raw, project_id)
        file_checked = 0
        file_skipped = 0
        for stmt in split_sql_statements(qualified):
            if should_skip_dry_run(stmt):
                file_skipped += 1
            else:
                file_checked += 1
        per_file[sql_file.relative_to(SQL_DIR).as_posix()] = {
            "checked": file_checked,
            "skipped": file_skipped,
            "total": file_checked + file_skipped,
        }
        total_checked += file_checked
        total_skipped += file_skipped

    generated_labels: list[str] = []
    if not skip_generated:
        for label, _ in _generated_sql_cases(project_id):
            generated_labels.append(label)
            total_checked += 1

    return {
        "schema_version": "1.0",
        "mode": "offline_sqlglot_manifest",
        "live_bq_dry_run_executed": False,
        "project_id": project_id,
        "skip_prefixes": list(SKIP_DRY_RUN_PREFIXES),
        "checked": total_checked,
        "skipped": total_skipped,
        "generated_statements": generated_labels,
        "per_file": per_file,
    }


def dry_run_statement(
    bq_bin: str,
    project_id: str,
    statement: str,
    *,
    location: str = "US",
) -> tuple[bool, str]:
    """Run `bq query --dry_run --use_legacy_sql=false` on a single BigQuery SQL statement."""
    proc = subprocess.run(  # noqa: S603
        [
            bq_bin,
            "query",
            f"--project_id={project_id}",
            f"--location={location}",
            "--use_legacy_sql=false",
            "--dry_run",
        ],
        input=statement,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, output


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run live BigQuery --dry_run validation against sql/**/*.sql and generated SQL.",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Target GCP project ID for live dry-run (defaults to showcase-project in offline manifest mode).",
    )
    parser.add_argument(
        "--location",
        default="US",
        help="BigQuery location for live --dry_run queries (default: US).",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Optional subset of SQL basenames under sql/ to validate.",
    )
    parser.add_argument(
        "--skip-generated",
        action="store_true",
        help="Skip validating Python-generated AI.GENERATE_TABLE / AI.EVALUATE_AGENT queries.",
    )
    parser.add_argument(
        "--offline-manifest",
        type=Path,
        default=None,
        help="Write sanitized offline statement count manifest JSON without invoking `bq`.",
    )
    parser.add_argument(
        "--receipt-out",
        type=Path,
        default=None,
        help="Optional path to write JSON dry-run receipt (sanitizing project_id to showcase-project).",
    )
    return parser


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_live_dry_run(  # noqa: PLR0913
    *,
    bq_bin: str,
    project_id: str,
    location: str = "US",
    files: Sequence[str] | None,
    skip_generated: bool,
    receipt_out: Path | None,
) -> int:
    sql_files = [SQL_DIR / name for name in files] if files else sorted(SQL_DIR.rglob("*.sql"))
    failures: list[str] = []
    per_file: dict[str, dict[str, int]] = {}
    checked = 0
    skipped = 0

    for sql_file in sql_files:
        raw = sql_file.read_text(encoding="utf-8")
        qualified = qualify_project_datasets(raw, project_id, location=location)
        file_checked = 0
        file_skipped = 0
        for idx, stmt in enumerate(split_sql_statements(qualified), start=1):
            if should_skip_dry_run(stmt):
                file_skipped += 1
                skipped += 1
                continue
            ok, output = dry_run_statement(bq_bin, project_id, stmt, location=location)
            checked += 1
            file_checked += 1
            if ok:
                print(f"PASS {sql_file.relative_to(SQL_DIR).as_posix()}#{idx}")
            else:
                print(
                    f"FAIL {sql_file.relative_to(SQL_DIR).as_posix()}#{idx}: {output}",
                    file=sys.stderr,
                )
                failures.append(f"{sql_file.relative_to(SQL_DIR).as_posix()}#{idx}")
        per_file[sql_file.relative_to(SQL_DIR).as_posix()] = {
            "checked": file_checked,
            "skipped": file_skipped,
            "total": file_checked + file_skipped,
        }

    generated_labels: list[str] = []
    if not skip_generated:
        for label, sql in _generated_sql_cases(project_id):
            generated_labels.append(label)
            ok, output = dry_run_statement(bq_bin, project_id, sql, location=location)
            checked += 1
            if ok:
                print(f"PASS {label}")
            else:
                print(f"FAIL {label}: {output}", file=sys.stderr)
                failures.append(label)

    if receipt_out is not None:
        _write_json(
            receipt_out,
            {
                "schema_version": "1.0",
                "mode": "live_bq_dry_run",
                "live_bq_dry_run_executed": True,
                "project_id": "showcase-project",
                "skip_prefixes": list(SKIP_DRY_RUN_PREFIXES),
                "checked": checked,
                "skipped": skipped,
                "failures": failures,
                "generated_statements": generated_labels,
                "per_file": per_file,
            },
        )

    print(f"Checked {checked} BigQuery statements ({skipped} skipped, {len(failures)} failures).")
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.offline_manifest is not None:
        manifest_proj = args.project_id or "showcase-project"
        manifest = collect_dry_run_manifest(
            project_id=manifest_proj,
            files=args.files,
            skip_generated=args.skip_generated,
        )
        _write_json(args.offline_manifest, manifest)
        if args.receipt_out is not None and args.receipt_out != args.offline_manifest:
            _write_json(args.receipt_out, manifest)
        print(
            f"Wrote offline BigQuery dry-run manifest to {args.offline_manifest} "
            f"({manifest['checked']} checked, {manifest['skipped']} skipped)."
        )
        return 0

    if not args.project_id:
        print("ERROR: `--project-id` is required for live `bq --dry_run` mode.", file=sys.stderr)
        return 2

    bq_bin = shutil.which("bq")
    if bq_bin is None:
        print("ERROR: `bq` CLI not found on PATH.", file=sys.stderr)
        return 2

    return _run_live_dry_run(
        bq_bin=bq_bin,
        project_id=args.project_id,
        location=args.location,
        files=args.files,
        skip_generated=args.skip_generated,
        receipt_out=args.receipt_out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
