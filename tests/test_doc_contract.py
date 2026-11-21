"""Documentation-to-code contract tests (`F19`, `M6-4`, `M6-5`).

Pins the headline README KPI row, canonical rubric count, fail-closed `EVAL.yaml`,
cold-clone quickstart commands, and license presence against live code.
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

from bq_eval.rubrics import CANONICAL_RUBRICS
from bq_eval.showcase import run_showcase_pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_readme_kpi_row_matches_showcase_pipeline() -> None:
    """README offline twin KPI row matches `run_showcase_pipeline()` outputs."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    artifacts = run_showcase_pipeline()
    order = artifacts.summaries["order-support-agent"]
    assert order.calibrated_pass_rate is not None
    assert order.calibrated_ci_95_lower is not None
    assert order.calibrated_ci_95_upper is not None
    assert order.cost_per_verified_outcome_usd is not None

    cal_pct = f"{order.calibrated_pass_rate * 100:.1f}%"
    ci_low = f"{order.calibrated_ci_95_lower * 100:.1f}%"
    ci_high = f"{order.calibrated_ci_95_upper * 100:.0f}%"
    cpvo = f"${order.cost_per_verified_outcome_usd:.4f}"
    savings = f"{artifacts.eval_roi.savings_pct:.1f}%"

    assert f"`{cal_pct}` calibrated `[{ci_low}, {ci_high}]`" in readme
    assert f"`{cpvo}`/outcome" in readme
    assert f"`{savings}`" in readme
    assert "uv sync --extra dev" in readme
    assert "rfp_quote_accuracy_4pt_v1" not in readme


def test_rubric_count_and_names_match_docs_csv_and_code() -> None:
    """Canonical rubric count matches Python, CSV, `AGENTS.md`, `DECISIONS.md`, and `docs/architecture-and-sql.md`."""
    expected_count = len(CANONICAL_RUBRICS)
    assert expected_count == 8

    with (REPO_ROOT / "sql" / "step2_rubrics.csv").open("r", encoding="utf-8", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == expected_count

    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    decisions_md = (REPO_ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    arch_md = (REPO_ROOT / "docs" / "architecture-and-sql.md").read_text(encoding="utf-8")

    assert f"{expected_count} canonical 4-point ordinal rubrics" in agents_md
    assert f"{expected_count} `CANONICAL_RUBRICS`" in agents_md
    assert "rfp_quote_accuracy_4pt_v1" not in agents_md
    assert "rfp_quote_completeness_4pt_v1" in agents_md
    assert f"{expected_count} canonical 4-point rubrics" in decisions_md
    assert f"{expected_count} canonical 4-point rubrics" in arch_md
    assert "## 6B. Multi-Agent Outcome Attribution Wire Contract (`EVAL-P11`)" in arch_md


def test_license_and_skill_eval_yaml_fail_closed_contract() -> None:
    """Repo ships Apache-2.0 `LICENSE` and fail-closed `skills/bq-eval/EVAL.yaml` (`F16`, `M6-4`)."""
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file()
    assert "Apache License" in license_path.read_text(encoding="utf-8")

    eval_yaml_path = REPO_ROOT / "skills" / "bq-eval" / "EVAL.yaml"
    assert eval_yaml_path.is_file()
    eval_text = eval_yaml_path.read_text(encoding="utf-8")
    assert "fail_closed_on_zero_scored: true" in eval_text
    assert re.search(r"(?m)^min_scored_cases:\s*[1-9]\d*\s*$", eval_text)
    case_ids = re.findall(r"(?m)^\s+-\s+id:\s+(\S+)", eval_text)
    assert len(case_ids) >= 4
    assert "EVAL-P10" in eval_text


def test_documented_test_count_matches_pytest_collection() -> None:
    """Test count cited in `README.md` and `AGENTS.md` matches actual collected pytest items (`F19`)."""
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    m = re.search(r"(\d+)\s+tests?\s+collected", res.stdout)
    assert m is not None, f"Could not parse pytest collection output:\n{res.stdout}"
    collected = int(m.group(1))

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"`{collected}/{collected}` tests" in readme, (
        f"README.md test count out of sync with collected={collected}"
    )
    assert f"`{collected}` tests" in agents_md, (
        f"AGENTS.md test count out of sync with collected={collected}"
    )
