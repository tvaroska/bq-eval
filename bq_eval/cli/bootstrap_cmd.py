"""CLI handler for `bq-eval bootstrap --mode light|full|hybrid [--dry-run|--apply]`."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

from bq_eval.deploy.contract.bootstrap import BootstrapMode, BootstrapValidationReport
from bq_eval.deploy.engine.bootstrap import (
    should_skip_dry_run_statement,
    validate_bootstrap_offline,
)
from bq_eval.deploy.engine.bq_client import apply_bootstrap_statements, format_bq_job_labels


def register_bootstrap_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Attach the `bootstrap` subcommand to the root `bq-eval` CLI parser."""
    boot_p = sub.add_parser(
        "bootstrap",
        help="Render, validate, or apply cold-start BigQuery SQL for light, full, or hybrid modes.",
    )
    boot_p.add_argument(
        "--mode",
        choices=("light", "full", "hybrid", "silver"),
        default="full",
        help="Capture/deploy mode: light, full, hybrid, or silver (ADR-048 4-dataset Silver core).",
    )
    boot_p.add_argument(
        "--location",
        default="US",
        help="BigQuery / Cloud Observability location (default: US).",
    )
    boot_p.add_argument(
        "--ensure-observability-link",
        action="store_true",
        help="Opt-in: configure Cloud Observability storage location and `obs_traces` linked dataset during --apply (S-21).",
    )
    boot_p.add_argument(
        "--silver-internal-dataset",
        default="silver_internal",
        help="Silver internal dataset ID (default: silver_internal).",
    )
    boot_p.add_argument(
        "--silver-ref-dataset",
        default="silver_ref",
        help="Silver reference dataset ID (default: silver_ref).",
    )
    boot_p.add_argument(
        "--silver-open-dataset",
        default="silver_open",
        help="Silver open dataset ID (default: silver_open).",
    )
    boot_p.add_argument(
        "--silver-restricted-dataset",
        default="silver_restricted",
        help="Silver restricted dataset ID (default: silver_restricted).",
    )
    boot_p.add_argument(
        "--read-margin-minutes",
        type=int,
        default=60,
        help="Silver read-window margin rendered into TVF bodies (default: 60).",
    )
    boot_p.add_argument(
        "--settle-minutes",
        type=int,
        default=5,
        help="Silver settle time rendered into TVF metadata (default: 5).",
    )
    boot_p.add_argument(
        "--outcome-lookback-days",
        type=int,
        default=30,
        help="Silver outcomes lookback window in days (default: 30).",
    )
    boot_p.add_argument(
        "--traces-dataset-id",
        default="obs_traces",
        help="Linked Cloud Trace dataset ID (default: obs_traces).",
    )
    boot_p.add_argument(
        "--platform-traces-dataset-id",
        default="silver_internal",
        help="Platform gateway trace dataset ID (default: silver_internal single-project stub).",
    )
    boot_p.add_argument(
        "--platform-traces-table-id",
        default="platform_spans",
        help="Platform gateway trace table ID (default: platform_spans).",
    )
    boot_p.add_argument(
        "--logs-dataset-id",
        default="obs_logs",
        help="Linked Cloud Logging dataset ID (default: obs_logs).",
    )
    boot_p.add_argument(
        "--shape3-prefix",
        default="bq-eval/shape3/",
        help="GCS Hive-partitioned prefix for Shape-3 BigLake payloads (default: bq-eval/shape3/).",
    )
    boot_p.add_argument(
        "--shape3-format",
        choices=("JSON", "PARQUET"),
        default="JSON",
        help="BigLake format for Shape-3 payloads (default: JSON).",
    )
    boot_p.add_argument(
        "--project-id",
        default=None,
        help="Target GCP project ID (required for --dry-run or --apply; defaults to showcase-project offline).",
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
        help="Include BigQuery Omni multi-cloud zero-egress spoke views (requires Omni target flags).",
    )
    boot_p.add_argument(
        "--aws-omni-connection-id",
        default=None,
        help="AWS BigQuery Omni connection ID (required with --include-omni).",
    )
    boot_p.add_argument(
        "--aws-s3-bucket",
        default=None,
        help="AWS S3 bucket holding spoke OTel Parquet/Iceberg files (required with --include-omni).",
    )
    boot_p.add_argument(
        "--azure-omni-connection-id",
        default=None,
        help="Azure BigQuery Omni connection ID (required with --include-omni).",
    )
    boot_p.add_argument(
        "--azure-storage-account",
        default=None,
        help="Azure ADLS storage account name (required with --include-omni).",
    )
    boot_p.add_argument(
        "--azure-container",
        default=None,
        help="Azure Blob/ADLS container name (required with --include-omni).",
    )
    exec_group = boot_p.add_mutually_exclusive_group()
    exec_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate checkable statements against live GCP via `bq query --dry_run` (requires --project-id).",
    )
    exec_group.add_argument(
        "--apply",
        action="store_true",
        help="Execute rendered DDL, seeds, procedures, and views on live BigQuery (requires --project-id).",
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


def _run_gcp_dry_run(
    project_id: str,
    stmts: list[tuple[str, str]],
    *,
    location: str = "US",
) -> list[str]:
    bq_bin = shutil.which("bq")
    if bq_bin is None:
        raise ValueError(
            "`bq` CLI not found on PATH; omit --dry-run for credential-free offline validation."
        )
    label_flags = format_bq_job_labels()
    errors: list[str] = []
    for label, stmt in stmts:
        if should_skip_dry_run_statement(stmt):
            continue
        proc = subprocess.run(  # noqa: S603
            [
                bq_bin,
                "query",
                f"--project_id={project_id}",
                f"--location={location}",
                "--use_legacy_sql=false",
                "--dry_run",
                *label_flags,
            ],
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
        f"- **Live GCP `--dry_run` Executed:** `{report.live_dry_run_executed}`",
        f"- **Live GCP `--apply` Executed:** `{report.live_apply_executed}`",
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
    """Execute `bq-eval bootstrap` across offline validation, SQL rendering, `--dry-run`, or `--apply`."""
    if (args.dry_run or args.apply) and not args.project_id:
        flag_name = "--apply" if args.apply else "--dry-run"
        raise ValueError(f"{flag_name} requires an explicit --project-id.")
    effective_project = args.project_id or "showcase-project"
    mode = cast(BootstrapMode, args.mode)
    report, stmts = validate_bootstrap_offline(
        mode=mode,
        project_id=effective_project,
        dataset_id=args.dataset_id,
        location=args.location,
        connection_id=args.connection_id,
        payload_bucket=args.payload_bucket,
        payload_prefix=args.payload_prefix,
        traces_dataset_id=args.traces_dataset_id,
        platform_traces_dataset_id=args.platform_traces_dataset_id,
        platform_traces_table_id=args.platform_traces_table_id,
        logs_dataset_id=args.logs_dataset_id,
        silver_internal_dataset=args.silver_internal_dataset,
        silver_ref_dataset=args.silver_ref_dataset,
        silver_open_dataset=args.silver_open_dataset,
        silver_restricted_dataset=args.silver_restricted_dataset,
        read_margin_minutes=args.read_margin_minutes,
        settle_minutes=args.settle_minutes,
        outcome_lookback_days=args.outcome_lookback_days,
        shape3_prefix=args.shape3_prefix,
        shape3_format=args.shape3_format,
        include_omni=bool(args.include_omni),
        aws_omni_connection_id=args.aws_omni_connection_id,
        aws_s3_bucket=args.aws_s3_bucket,
        azure_omni_connection_id=args.azure_omni_connection_id,
        azure_storage_account=args.azure_storage_account,
        azure_container=args.azure_container,
    )

    extra_errors: list[str] = list(report.errors)
    live_dry_run = False
    live_apply = False
    if report.passed and args.dry_run:
        live_dry_run = True
        extra_errors.extend(_run_gcp_dry_run(effective_project, stmts, location=args.location))
    elif report.passed and args.apply:
        live_apply = True
        extra_errors.extend(
            apply_bootstrap_statements(
                effective_project,
                stmts,
                mode=mode,
                location=args.location,
                ensure_traces_link=bool(args.ensure_observability_link),
                silver_internal_dataset=args.silver_internal_dataset,
                silver_ref_dataset=args.silver_ref_dataset,
                silver_open_dataset=args.silver_open_dataset,
                silver_restricted_dataset=args.silver_restricted_dataset,
                traces_dataset_id=args.traces_dataset_id,
                platform_traces_dataset_id=args.platform_traces_dataset_id,
                logs_dataset_id=args.logs_dataset_id,
            )
        )

    final_report = report.model_copy(
        update={
            "errors": tuple(extra_errors),
            "live_dry_run_executed": live_dry_run,
            "live_apply_executed": live_apply,
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
