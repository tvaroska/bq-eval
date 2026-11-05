"""Command-Line Interface (`bq-eval`) for BigQuery Agent Evaluation & FinOps Showcase."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from bq_eval.showcase import (
    render_markdown_report,
    run_showcase_pipeline,
    write_html_scorecard,
)
from bq_eval.sql_eval import build_ai_generate_table_judge_sql


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bq-eval",
        description="BigQuery-Native Enterprise Agent Evaluation, Observability & FinOps Showcase CLI.",
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

    sql_p = sub.add_parser(
        "judge-sql",
        help="Generate stratified BigQuery AI.GENERATE_TABLE judge SQL with XML escaping.",
    )
    sql_p.add_argument("--project-id", default="enterprise-ai-prod", help="GCP Project ID.")
    sql_p.add_argument("--dataset-id", default="agent_eval", help="BigQuery Dataset ID.")
    sql_p.add_argument(
        "--rubric-id",
        default="groundedness_4pt_v1",
        help="Canonical 4-point ordinal rubric ID.",
    )
    sql_p.add_argument(
        "--canary-pct",
        type=int,
        default=5,
        help="Canary sampling percentage for healthy traces (default: 5).",
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

    if args.command == "judge-sql":
        sql = build_ai_generate_table_judge_sql(
            project_id=args.project_id,
            dataset_id=args.dataset_id,
            rubric_id=args.rubric_id,
            canary_sample_pct=args.canary_pct,
        )
        print(sql)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
