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

from bq_eval.core.engine.sql_eval import (
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
    "CREATE OR REPLACE PROCEDURE",
    "CREATE PROCEDURE",
    "ALTER TABLE",
)


def qualify_project_datasets(  # noqa: PLR0913
    sql: str,
    project_id: str,
    *,
    dataset_id: str = "agent_eval",
    connection_id: str = "us.vertex_ai_connection",
    payload_bucket: str | None = None,
    payload_prefix: str = "bq-eval/",
) -> str:
    """Substitute bootstrap placeholders and prefix bare dataset refs with `project_id.` in memory."""
    bucket = payload_bucket or project_id
    prefix = payload_prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    qualified = (
        sql.replace("${PROJECT_ID}", project_id)
        .replace("${DATASET_ID}", dataset_id)
        .replace("${CONNECTION_ID}", connection_id)
        .replace("${PAYLOAD_BUCKET}", bucket)
        .replace("${PREFIX}", prefix)
        .replace("${AWS_OMNI_CONNECTION_ID}", "aws-us-east-1.omni_connection")
        .replace("${AZURE_OMNI_CONNECTION_ID}", "azure-eastus2.omni_connection")
        .replace("${AWS_S3_BUCKET}", "enterprise-aws-otel-bucket")
        .replace("${AZURE_STORAGE_ACCOUNT}", "enterpriseazureotel")
        .replace("${AZURE_CONTAINER}", "otel-spans")
    )
    for dataset in DATASET_PREFIXES:
        target_ds = dataset_id if dataset == "agent_eval" else dataset
        qualified = qualified.replace(f"`{dataset}.", f"`{project_id}.{target_ds}.")
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


def _advance_block_depths(
    tokens: Sequence[sqlglot.Token],
    idx: int,
    block_depth: int,
    case_depth: int,
) -> tuple[int, int]:
    tok = tokens[idx]
    if tok.token_type == TokenType.CASE:
        return block_depth, case_depth + 1
    upper_text = tok.text.upper()
    if upper_text == "BEGIN":
        next_text = tokens[idx + 1].text.upper() if idx + 1 < len(tokens) else ""
        return (block_depth + 1 if next_text != "TRANSACTION" else block_depth), case_depth
    if upper_text == "END":
        next_text = tokens[idx + 1].text.upper() if idx + 1 < len(tokens) else ""
        if next_text in {"IF", "LOOP", "WHILE", "FOR"}:
            return block_depth, case_depth
        if case_depth > 0:
            return block_depth, case_depth - 1
        return max(0, block_depth - 1), case_depth
    return block_depth, case_depth


def split_sql_statements(sql: str) -> list[str]:
    """Split a multi-statement BigQuery SQL script via `sqlglot`'s BigQuery lexer.

    Preserves `BEGIN ... END;` procedure blocks as single statements while stripping leading
    `--`/`#` and `/* ... */` comments so statements begin cleanly at their SQL keyword.
    """
    tokens = sqlglot.tokenize(sql, read="bigquery")
    if not tokens:
        return []

    statements: list[str] = []
    chunk_start = 0
    has_non_semicolon = False
    block_depth = 0
    case_depth = 0

    for idx, tok in enumerate(tokens):
        if tok.token_type == TokenType.SEMICOLON and block_depth == 0:
            if has_non_semicolon:
                stmt = strip_leading_sql_comments(sql[chunk_start : tok.start])
                if stmt:
                    statements.append(stmt)
            chunk_start = tok.end + 1
            has_non_semicolon = False
            continue
        block_depth, case_depth = _advance_block_depths(tokens, idx, block_depth, case_depth)
        if tok.token_type != TokenType.SEMICOLON:
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
    sql_files = [SQL_DIR / name for name in files] if files else sorted(SQL_DIR.rglob("*.sql"))
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
