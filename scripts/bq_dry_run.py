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

import sqlglot
from sqlglot import TokenType

from bq_eval.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"

DATASET_PREFIXES: tuple[str, ...] = (
    "agent_eval",
    "agent_eval_seed",
    "obs_traces",
    "obs_logs",
)

SKIP_DRY_RUN_PREFIXES: tuple[str, ...] = (
    "CREATE SCHEMA",
    "CREATE EXTERNAL TABLE",
    "CREATE MODEL",
    "ALTER TABLE",
)


def qualify_project_datasets(sql: str, project_id: str) -> str:
    """Prefix bare dataset references (`agent_eval.`, etc.) with `project_id.` in memory."""
    qualified = sql
    for dataset in DATASET_PREFIXES:
        qualified = qualified.replace(f"`{dataset}.", f"`{project_id}.{dataset}.")
    return qualified


def strip_leading_sql_comments(sql: str) -> str:
    """Strip leading whitespace, `--`/`#` line comments, and `/* ... */` block comments."""
    i, n = 0, len(sql)
    while i < n:
        if sql[i].isspace():
            i += 1
        elif sql.startswith("--", i) or sql.startswith("#", i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                return ""
            i = end + 2
        else:
            break
    return sql[i:].strip()


def should_skip_dry_run(statement: str) -> bool:
    """Return True for empty/comment-only chunks and bootstrap DDL skipped in `--dry_run`."""
    cleaned = strip_leading_sql_comments(statement)
    if not cleaned:
        return True
    upper = cleaned.upper()
    return any(upper.startswith(prefix) for prefix in SKIP_DRY_RUN_PREFIXES)


def split_sql_statements(sql: str) -> list[str]:
    """Split a multi-statement BigQuery SQL script via `sqlglot`'s BigQuery lexer (`F21`).

    Strips leading `--`/`#` and `/* ... */` comments so comment-only chunks are omitted
    and statements begin cleanly at their SQL keyword.
    """
    tokens = sqlglot.tokenize(sql, read="bigquery")
    if not tokens:
        return []

    statements: list[str] = []
    chunk_start = 0
    has_non_semicolon = False

    for tok in tokens:
        if tok.token_type == TokenType.SEMICOLON:
            if has_non_semicolon:
                stmt = strip_leading_sql_comments(sql[chunk_start : tok.start])
                if stmt:
                    statements.append(stmt)
            chunk_start = tok.end + 1
            has_non_semicolon = False
        else:
            has_non_semicolon = True

    if has_non_semicolon:
        tail = strip_leading_sql_comments(sql[chunk_start:])
        if tail:
            statements.append(tail)
    return statements


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
    """Build a sanitized statement inventory (`checked` vs. `skipped`) across `sql/*.sql`."""
    sql_files = [SQL_DIR / name for name in files] if files else sorted(SQL_DIR.glob("*.sql"))
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
        per_file[sql_file.name] = {
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


def dry_run_statement(bq_bin: str, project_id: str, statement: str) -> tuple[bool, str]:
    """Run `bq query --dry_run --use_legacy_sql=false` on a single BigQuery SQL statement."""
    proc = subprocess.run(  # noqa: S603
        [bq_bin, "query", f"--project_id={project_id}", "--use_legacy_sql=false", "--dry_run"],
        input=statement,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, output


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run live BigQuery --dry_run validation against sql/*.sql and generated SQL.",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Target GCP project ID for live dry-run (defaults to showcase-project in offline manifest mode).",
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


def _run_live_dry_run(
    *,
    bq_bin: str,
    project_id: str,
    files: Sequence[str] | None,
    skip_generated: bool,
    receipt_out: Path | None,
) -> int:
    sql_files = [SQL_DIR / name for name in files] if files else sorted(SQL_DIR.glob("*.sql"))
    failures: list[str] = []
    per_file: dict[str, dict[str, int]] = {}
    checked = 0
    skipped = 0

    for sql_file in sql_files:
        raw = sql_file.read_text(encoding="utf-8")
        qualified = qualify_project_datasets(raw, project_id)
        file_checked = 0
        file_skipped = 0
        for idx, stmt in enumerate(split_sql_statements(qualified), start=1):
            if should_skip_dry_run(stmt):
                file_skipped += 1
                skipped += 1
                continue
            ok, output = dry_run_statement(bq_bin, project_id, stmt)
            checked += 1
            file_checked += 1
            if ok:
                print(f"PASS {sql_file.name}#{idx}")
            else:
                print(f"FAIL {sql_file.name}#{idx}: {output}", file=sys.stderr)
                failures.append(f"{sql_file.name}#{idx}")
        per_file[sql_file.name] = {
            "checked": file_checked,
            "skipped": file_skipped,
            "total": file_checked + file_skipped,
        }

    generated_labels: list[str] = []
    if not skip_generated:
        for label, sql in _generated_sql_cases(project_id):
            generated_labels.append(label)
            ok, output = dry_run_statement(bq_bin, project_id, sql)
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
        files=args.files,
        skip_generated=args.skip_generated,
        receipt_out=args.receipt_out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
