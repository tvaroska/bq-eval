"""CLI handler for `bq-eval bootstrap --mode light|full|hybrid [--dry-run|--offline-schema]`."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

from bq_eval.contract.governance import BootstrapMode, BootstrapValidationReport
from bq_eval.engine.bootstrap import (
    should_skip_dry_run_statement,
    validate_bootstrap_offline,
)


def register_bootstrap_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Attach the `bootstrap` subcommand to the root `bq-eval` CLI parser."""
    boot_p = sub.add_parser(
        "bootstrap",
        help="Render and validate cold-start BigQuery SQL for light, full, or hybrid capture modes.",
    )
    boot_p.add_argument(
        "--mode",
        choices=("light", "full", "hybrid"),
        default="full",
        help="Capture mode: light (BQAA only), full (OTel _AllSpans/_AllLogs), or hybrid (default: full).",
    )
    boot_p.add_argument(
        "--project-id",
        default="showcase-project",
        help="Target GCP project ID (default: showcase-project for --offline-schema).",
    )
    boot_p.add_argument(
        "--dataset-id",
        default="agent_eval",
        help="Target BigQuery dataset ID (default: agent_eval).",
    )
    boot_p.add_argument(
        "--connection-id",
        default="us.vertex_ai_connection",
        help="BigQuery Cloud Resource / Vertex AI connection ID (default: us.vertex_ai_connection).",
    )
    boot_p.add_argument(
        "--payload-bucket",
        default=None,
        help="GCS bucket for GenAI completion payloads (defaults to <project-id> single-bucket convention).",
    )
    boot_p.add_argument(
        "--payload-prefix",
        default="bq-eval/",
        help="GCS object prefix inside payload bucket (default: bq-eval/).",
    )
    boot_p.add_argument(
        "--include-omni",
        action="store_true",
        help="Include BigQuery Omni multi-cloud zero-egress spoke views in the rendered pack.",
    )
    boot_p.add_argument(
        "--offline-schema",
        action="store_true",
        help="Run zero-credential offline schema, placeholder, DAG, and AST validation.",
    )
    boot_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate checkable statements against live GCP via `bq query --dry_run`.",
    )
    boot_p.add_argument(
        "--sql-out",
        type=Path,
        default=None,
        help="Optional file path to write the rendered, parameter-substituted SQL script.",
    )
    boot_p.add_argument(
        "--format",
        dest="output_format",
        choices=("markdown", "json"),
        default="markdown",
        help="Stdout output format: markdown or json (default: markdown).",
    )


def _run_gcp_dry_run(project_id: str, stmts: list[tuple[str, str]]) -> list[str]:
    bq_bin = shutil.which("bq")
    if bq_bin is None:
        raise ValueError(
            "`bq` CLI not found on PATH; use --offline-schema for credential-free checks."
        )
    errors: list[str] = []
    for label, stmt in stmts:
        if should_skip_dry_run_statement(stmt):
            continue
        proc = subprocess.run(  # noqa: S603
            [bq_bin, "query", f"--project_id={project_id}", "--use_legacy_sql=false", "--dry_run"],
            input=stmt,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stdout + "\n" + proc.stderr).strip()
            errors.append(f"{label}: bq --dry_run failed: {detail[:240]}")
    return errors


def _format_bootstrap_markdown(report: BootstrapValidationReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    lines = [
        f"# BQ Eval Bootstrap (`--mode {report.mode}` — `{status}`)",
        "",
        f"- **Project / Dataset:** `{report.project_id}.{report.dataset_id}`",
        f"- **Connection ID:** `{report.connection_id}`",
        f"- **Payload URI Pattern:** `{report.payload_uri_pattern}`",
        f"- **Statements:** `{report.statement_count}` total (`{report.checked_count}` checkable, `{report.skipped_count}` DDL/proc skipped)",
        f"- **Span Schema Columns Verified:** `{report.schema_column_count}`",
        f"- **AST (`sqlglot`) Verified:** `{report.sqlglot_verified}`",
        f"- **Live GCP `--dry_run` Executed:** `{report.live_dry_run_executed}`",
    ]
    if report.unresolved_placeholders:
        lines.append(
            f"- **Unresolved Placeholders:** `{', '.join(report.unresolved_placeholders)}`"
        )
    if report.errors:
        lines.append("")
        lines.append("## Errors")
        for err in report.errors:
            lines.append(f"- {err}")
    return "\n".join(lines)


def run_bootstrap_command(args: argparse.Namespace) -> int:
    """Execute `bq-eval bootstrap` across offline schema validation, SQL rendering, and GCP `--dry_run`."""
    mode = cast(BootstrapMode, args.mode)
    report, stmts = validate_bootstrap_offline(
        mode=mode,
        project_id=args.project_id,
        dataset_id=args.dataset_id,
        connection_id=args.connection_id,
        payload_bucket=args.payload_bucket,
        payload_prefix=args.payload_prefix,
        include_omni=bool(args.include_omni),
    )

    extra_errors: list[str] = list(report.errors)
    live_executed = False
    if args.dry_run:
        live_executed = True
        extra_errors.extend(_run_gcp_dry_run(args.project_id, stmts))

    final_report = report.model_copy(
        update={
            "errors": tuple(extra_errors),
            "live_dry_run_executed": live_executed,
        }
    )

    if args.sql_out is not None:
        args.sql_out.parent.mkdir(parents=True, exist_ok=True)
        script_body = "\n\n".join(f"-- [{label}]\n{stmt};" for label, stmt in stmts) + "\n"
        args.sql_out.write_text(script_body, encoding="utf-8")

    if args.output_format == "json":
        payload = final_report.model_dump(mode="json")
        payload["passed"] = final_report.passed
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_format_bootstrap_markdown(final_report))
        if args.sql_out is not None:
            print(f"\nRendered SQL written to: {args.sql_out}")

    if not final_report.passed:
        print("bq-eval bootstrap: validation failed.", file=sys.stderr)
        return 1
    return 0
