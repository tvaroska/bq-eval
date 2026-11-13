"""Command-Line Interface (`bq-eval`) for BigQuery Agent Evaluation & FinOps Showcase."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from bq_eval.loadgen import (
    evaluate_time_shifted_corpus,
    export_time_shifted_jsonl,
    generate_time_shifted_corpus,
    render_loadgen_markdown_report,
)
from bq_eval.showcase import (
    render_markdown_report,
    run_showcase_pipeline,
    write_html_scorecard,
)
from bq_eval.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bq-eval",
        description="BQ Eval & FinOps: Dual-Path BigQuery Agent Evaluation, Observability & FinOps CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo_p = sub.add_parser(
        "demo",
        help="Run the offline SQLite twin showcase and emit Markdown + HTML scorecards.",
    )
    demo_p.add_argument(
        "--html-out",
        type=Path,
        default=None,
        help="Optional file path to write the self-contained HTML scorecard.",
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
        help="Optional directory to export time-shifted otel_spans.jsonl and trace_payloads.jsonl.",
    )

    sql_p = sub.add_parser(
        "judge-sql",
        help="Generate stratified BigQuery judge SQL (Engine A AI.GENERATE_TABLE or Engine B AI.EVALUATE_AGENT).",
    )
    sql_p.add_argument(
        "--engine",
        choices=("generate-table", "evaluate-agent"),
        default="generate-table",
        help="Judge SQL engine: generate-table (all regions, 4-pt rubrics) or evaluate-agent (BQAA AgentData TVF).",
    )
    sql_p.add_argument("--project-id", default="enterprise-ai-prod", help="GCP Project ID.")
    sql_p.add_argument("--dataset-id", default="agent_eval", help="BigQuery Dataset ID.")
    sql_p.add_argument(
        "--rubric-id",
        default="groundedness_4pt_v1",
        help="Canonical 4-point ordinal rubric ID (for --engine generate-table).",
    )
    sql_p.add_argument(
        "--eval-view",
        default="v_bqaa_eval_turn_filtered",
        help="BQAA AgentData turn/session view name (for --engine evaluate-agent).",
    )
    sql_p.add_argument(
        "--canary-pct",
        type=int,
        default=5,
        help="Canary sampling percentage for healthy traces (default: 5).",
    )
    sql_p.add_argument(
        "--run-id",
        default=None,
        help="Optional fleet run_id filter against fleet_case_outcomes.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "demo":
        artifacts = run_showcase_pipeline()
        print(render_markdown_report(artifacts))
        if args.html_out is not None:
            out_path = write_html_scorecard(artifacts, args.html_out)
            print(f"\nHTML scorecard written to: {out_path}")
        return 0

    if args.command == "loadgen":
        spans, payloads, ref_time = generate_time_shifted_corpus(
            total_replicas=args.replicas,
            last_hour_replicas=args.last_hour_replicas,
        )
        report = evaluate_time_shifted_corpus(
            spans=spans,
            payloads=payloads,
            reference_time=ref_time,
            total_replicas=args.replicas,
            last_hour_replicas=args.last_hour_replicas,
        )
        print(render_loadgen_markdown_report(report))
        if args.jsonl_out is not None:
            spans_path, payloads_path = export_time_shifted_jsonl(
                spans=spans,
                payloads=payloads,
                output_dir=args.jsonl_out,
            )
            print(f"\nExported time-shifted JSONL files:\n- {spans_path}\n- {payloads_path}")
        return 0

    if args.command == "judge-sql":
        if args.engine == "evaluate-agent":
            sql = build_ai_evaluate_agent_sql(
                project_id=args.project_id,
                dataset_id=args.dataset_id,
                eval_view=args.eval_view,
                canary_sample_pct=args.canary_pct,
                run_id=args.run_id,
            )
        else:
            sql = build_ai_generate_table_judge_sql(
                project_id=args.project_id,
                dataset_id=args.dataset_id,
                rubric_id=args.rubric_id,
                canary_sample_pct=args.canary_pct,
                run_id=args.run_id,
            )
        print(sql)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
