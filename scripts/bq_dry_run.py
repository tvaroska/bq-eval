"""Opt-in live BigQuery dry-run validator for `sql/*.sql` and generated judge SQL.

Substitutes `agent_eval.`, `agent_eval_seed.`, `obs_traces.`, and `obs_logs.` dataset
prefixes in memory with `--project-id` so repository SQL files remain 100% project-agnostic
while verifying every statement against BigQuery's real SQL parser and schema binder.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from bq_eval.sql_eval import build_ai_evaluate_agent_sql, build_ai_generate_table_judge_sql

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
        qualified = re.sub(
            rf"`{dataset}\.",
            f"`{project_id}.{dataset}.",
            qualified,
        )
    return qualified


def split_sql_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL script on semicolons outside quotes and line comments."""
    statements: list[str] = []
    buf: list[str] = []
    in_single = False
    in_triple_single = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if not in_single and not in_triple_single and ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if not in_single and sql[i : i + 3] == "'''":
            in_triple_single = not in_triple_single
            buf.append("'''")
            i += 3
            continue
        if not in_triple_single and ch == "'" and (i == 0 or sql[i - 1] != "\\"):
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_triple_single and ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run live BigQuery --dry_run validation against sql/*.sql and generated SQL.",
    )
    parser.add_argument("--project-id", required=True, help="Target GCP project ID for dry-run.")
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
    args = parser.parse_args(argv)

    bq_bin = shutil.which("bq")
    if bq_bin is None:
        print("ERROR: `bq` CLI not found on PATH.", file=sys.stderr)
        return 2

    sql_files = (
        [SQL_DIR / name for name in args.files] if args.files else sorted(SQL_DIR.glob("*.sql"))
    )
    failures: list[str] = []
    checked = 0

    for sql_file in sql_files:
        raw = sql_file.read_text(encoding="utf-8")
        qualified = qualify_project_datasets(raw, args.project_id)
        for idx, stmt in enumerate(split_sql_statements(qualified), start=1):
            upper = stmt.lstrip().upper()
            if any(upper.startswith(prefix) for prefix in SKIP_DRY_RUN_PREFIXES):
                continue
            ok, output = dry_run_statement(bq_bin, args.project_id, stmt)
            checked += 1
            if ok:
                print(f"PASS {sql_file.name}#{idx}")
            else:
                print(f"FAIL {sql_file.name}#{idx}: {output}", file=sys.stderr)
                failures.append(f"{sql_file.name}#{idx}")

    if not args.skip_generated:
        generated_cases = [
            (
                "build_ai_generate_table_judge_sql",
                build_ai_generate_table_judge_sql(project_id=args.project_id, canary_sample_pct=5),
            ),
            (
                "build_ai_evaluate_agent_sql",
                build_ai_evaluate_agent_sql(project_id=args.project_id, canary_sample_pct=10),
            ),
        ]
        for label, sql in generated_cases:
            ok, output = dry_run_statement(bq_bin, args.project_id, sql)
            checked += 1
            if ok:
                print(f"PASS {label}")
            else:
                print(f"FAIL {label}: {output}", file=sys.stderr)
                failures.append(label)

    print(f"Checked {checked} BigQuery statements ({len(failures)} failures).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
