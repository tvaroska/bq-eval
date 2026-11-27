"""Command-Line Interface (`bq-eval`) for BigQuery Silver + Gold Agent Evaluation & FinOps."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from bq_eval import __version__
from bq_eval.cli.bootstrap_cmd import register_bootstrap_subparser, run_bootstrap_command
from bq_eval.cli.capacity_cmd import (
    register_capacity_export_subparser,
    run_capacity_export_command,
)
from bq_eval.cli.server import create_workbench_server
from bq_eval.core.contract.pricing import MANAGED_EVALUATE_AGENT_METRICS
from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)
from bq_eval.demo.loadgen import (
    evaluate_time_shifted_corpus,
    export_time_shifted_jsonl,
    generate_time_shifted_corpus,
    render_loadgen_markdown_report,
)
from bq_eval.demo.pipeline import (
    CorpusChoice,
    format_demo_json,
    render_markdown_report,
    run_showcase_pipeline,
)
from bq_eval.demo.render.renderer import write_html_scorecard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bq-eval",
        description="BQ Eval & FinOps: BigQuery Silver Core + Gold Evaluation, Monitoring & Cost CLI.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo_p = sub.add_parser(
        "demo",
        help="Replay reconstructed fleet receipts or offline corpora over Silver/Gold SQL and emit scorecards.",
    )
    demo_p.add_argument(
        "--corpus",
        choices=("m2-live", "golden-4app", "retail-2app"),
        default="m2-live",
        help="Built-in scenario corpus to replay (default: m2-live).",
    )
    demo_p.add_argument(
        "--scenario-file",
        type=Path,
        default=None,
        help="Optional path to a custom ScenarioCorpusBundle JSON file.",
    )
    demo_p.add_argument(
        "--format",
        dest="output_format",
        choices=("markdown", "json"),
        default="markdown",
        help="Stdout output format: markdown or deterministic json (default: markdown).",
    )
    demo_p.add_argument(
        "--html-out",
        type=Path,
        default=None,
        help="Optional file path to write the self-contained GM3 Light HTML workbench.",
    )

    serve_p = sub.add_parser(
        "serve",
        help="Serve the interactive 4-tab GM3 Light BQ Eval & FinOps Workbench over HTTP.",
    )
    serve_p.add_argument(
        "--host", default="127.0.0.1", help="Loopback bind host (default: 127.0.0.1)."
    )
    serve_p.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicit opt-in required to bind the HTTP server on a non-loopback host.",
    )
    serve_p.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080).")
    serve_p.add_argument(
        "--corpus",
        choices=("m2-live", "golden-4app", "retail-2app"),
        default="m2-live",
        help="Initial corpus displayed in the workbench (default: m2-live).",
    )
    serve_p.add_argument(
        "--scenario-file",
        type=Path,
        default=None,
        help="Optional path to a custom ScenarioCorpusBundle JSON file.",
    )

    loadgen_p = sub.add_parser(
        "loadgen",
        help="Time-shift and multiply the 4-app Golden Captured Trace Corpus into today / last 1 hour.",
    )
    loadgen_p.add_argument(
        "--replicas",
        type=int,
        default=50,
        help="Number of replicas per golden trace template (default: 50 -> 800 traces).",
    )
    loadgen_p.add_argument(
        "--last-hour-replicas",
        type=int,
        default=15,
        help="Replicas placed inside the last 1 hour for Step 2.5 queries (default: 15 -> 240 traces).",
    )
    loadgen_p.add_argument(
        "--jsonl-out",
        type=Path,
        default=None,
        help="Optional directory to export Bronze-shaped spans, registry, and trace payloads JSONL.",
    )

    sql_p = sub.add_parser(
        "judge-sql",
        help="Generate stratified BigQuery judge SQL (AI.GENERATE_TABLE or Preview AI.EVALUATE_AGENT).",
    )
    sql_p.add_argument(
        "--engine",
        choices=("generate-table", "evaluate-agent"),
        default="generate-table",
        help="Judge SQL engine: generate-table (4-pt rubrics) or evaluate-agent (Preview managed metrics).",
    )
    sql_p.add_argument("--project-id", default="enterprise-ai-prod", help="GCP Project ID.")
    sql_p.add_argument("--dataset-id", default="agent_eval", help="BigQuery Dataset ID.")
    sql_p.add_argument(
        "--rubric-id",
        default="groundedness_4pt_v1",
        help="Canonical 4-point ordinal rubric ID (for --engine generate-table).",
    )
    sql_p.add_argument(
        "--judge-model",
        choices=("gemini-3.8-flash", "gemini-4-argon"),
        default=None,
        help="Optional autorater model override for --engine generate-table (defaults to rubric setting).",
    )
    sql_p.add_argument(
        "--eval-view",
        default="v_eval_agent_turns",
        help="Gold AgentData turn view name over Silver traces/payloads (for --engine evaluate-agent).",
    )
    sql_p.add_argument(
        "--managed-metrics",
        default=None,
        help="Comma-separated Preview AI.EVALUATE_AGENT managed metric names (for --engine evaluate-agent).",
    )
    sql_p.add_argument(
        "--canary-pct",
        type=int,
        default=None,
        help="Canary sampling percentage for healthy traces (defaults to 5 for generate-table, 10 for evaluate-agent).",
    )
    sql_p.add_argument(
        "--run-id",
        default=None,
        help="Optional fleet run_id filter against fleet_case_outcomes.",
    )
    sql_p.add_argument(
        "--include-rule0-failures",
        action="store_true",
        help="Include traces failing Rule 0 / Step-1 checks in judge-sql (default: routine mode skips them).",
    )

    register_bootstrap_subparser(sub)
    register_capacity_export_subparser(sub)
    return parser


def _run_demo(args: argparse.Namespace) -> int:
    artifacts = run_showcase_pipeline(
        corpus=cast(CorpusChoice, args.corpus),
        scenario_file=args.scenario_file,
    )
    if args.html_out is not None:
        out_path = write_html_scorecard(artifacts, args.html_out)
        if args.output_format != "json":
            print(render_markdown_report(artifacts))
            print(f"\nHTML scorecard written to: {out_path}")
            return 0
    if args.output_format == "json":
        print(format_demo_json(artifacts))
    else:
        print(render_markdown_report(artifacts))
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    corpus = cast(CorpusChoice, args.corpus)
    server = create_workbench_server(
        host=args.host,
        port=args.port,
        corpus=corpus,
        scenario_file=args.scenario_file,
        allow_non_loopback=args.allow_non_loopback,
    )
    try:
        server.get_artifacts(corpus)
    except (ValueError, OSError):
        server.server_close()
        raise
    raw_host, actual_port = server.server_address[:2]
    actual_host = (
        raw_host.decode("ascii") if isinstance(raw_host, (bytes, bytearray)) else str(raw_host)
    )
    print(f"BQ Eval & FinOps Workbench listening on http://{actual_host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _run_loadgen(args: argparse.Namespace) -> int:
    spans, payloads, ref_time = generate_time_shifted_corpus(
        total_replicas=args.replicas,
        last_hour_replicas=args.last_hour_replicas,
    )
    report = evaluate_time_shifted_corpus(spans=spans, reference_time=ref_time)
    print(render_loadgen_markdown_report(report))
    if args.jsonl_out is not None:
        spans_path, payloads_path = export_time_shifted_jsonl(
            spans=spans,
            payloads=payloads,
            output_dir=args.jsonl_out,
        )
        print(f"\nExported time-shifted JSONL files:\n- {spans_path}\n- {payloads_path}")
    return 0


def _run_judge_sql(args: argparse.Namespace) -> int:
    if args.engine == "evaluate-agent":
        metrics = (
            tuple(m.strip() for m in args.managed_metrics.split(",") if m.strip())
            if args.managed_metrics
            else MANAGED_EVALUATE_AGENT_METRICS
        )
        canary_pct = 10 if args.canary_pct is None else args.canary_pct
        sql = build_ai_evaluate_agent_sql(
            project_id=args.project_id,
            dataset_id=args.dataset_id,
            eval_view=args.eval_view,
            managed_metrics=metrics,
            canary_sample_pct=canary_pct,
            run_id=args.run_id,
        )
    else:
        canary_pct = 5 if args.canary_pct is None else args.canary_pct
        sql = build_ai_generate_table_judge_sql(
            project_id=args.project_id,
            dataset_id=args.dataset_id,
            rubric_id=args.rubric_id,
            canary_sample_pct=canary_pct,
            run_id=args.run_id,
            judge_model=args.judge_model,
            include_rule0_failures=bool(args.include_rule0_failures),
        )
    print(sql)
    return 0


def _run_command(args: argparse.Namespace) -> int:
    handlers = {
        "demo": _run_demo,
        "serve": _run_serve,
        "loadgen": _run_loadgen,
        "judge-sql": _run_judge_sql,
        "bootstrap": run_bootstrap_command,
        "capacity-export": run_capacity_export_command,
    }
    handler = handlers.get(args.command)
    return handler(args) if handler is not None else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run_command(args)
    except (ValueError, OSError) as exc:
        print(f"bq-eval: error: {exc}", file=sys.stderr)
        return 2
