"""Runnable 60-second showcase script demonstrating Tier 1-3 BigQuery SQL Eval & FinOps."""

from __future__ import annotations

import tempfile
from pathlib import Path

from bq_eval.showcase import (
    render_markdown_report,
    run_showcase_pipeline,
    write_html_scorecard,
)


def main() -> None:
    artifacts = run_showcase_pipeline()
    print(render_markdown_report(artifacts))
    out_file = Path(tempfile.gettempdir()) / "bq_eval_scorecard.html"
    write_html_scorecard(artifacts, out_file)
    print(f"Generated self-contained HTML scorecard with favicon: {out_file}")


if __name__ == "__main__":
    main()
