"""Unit tests for M16 pre-flight helpers (`--apply`, job labels) and M17 `blind_sme_kit.py`."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.demo.bundles import verify_receipt_sha256
from bq_eval.deploy.engine.bq_client import (
    apply_bootstrap_statements,
    bq_cli_query_runner,
    format_bq_job_labels,
)
from scripts.blind_sme_kit import (
    export_blind_sme_kit,
    import_blind_sme_annotations,
)
from scripts.blind_sme_kit import (
    main as blind_sme_main,
)
from scripts.m16_vertex_otel_probe import sanitize_text
from scripts.run_m16_live_proof import M16LiveProofConfig
from tests.sql_helpers import REPO_ROOT


def test_bq_job_labels_and_bootstrap_apply_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bq_cli_query_runner` and `bootstrap --apply` stamp `workload:bq-eval` and require `--project-id`."""
    assert format_bq_job_labels() == ("--label=workload:bq-eval",)
    assert format_bq_job_labels({"apphub_application_id": "kyc-app"}) == (
        "--label=apphub_application_id:kyc-app",
        "--label=workload:bq-eval",
    )
    with pytest.raises(ValueError, match="Invalid BigQuery job label"):
        format_bq_job_labels({"bad label": "val"})

    rc_no_proj = cli_main(["bootstrap", "--mode", "light", "--apply"])
    assert rc_no_proj == 2
    assert "--apply requires an explicit --project-id" in capsys.readouterr().err

    seen_cmds: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        seen_cmds.append(list(cmd))
        res = MagicMock()
        res.returncode = 0
        res.stdout = "Number of affected rows: 1" if len(seen_cmds) == 2 else "[]"
        res.stderr = ""
        return res

    with (
        patch("bq_eval.deploy.engine.bq_client.shutil.which", return_value="/usr/bin/bq"),
        patch("bq_eval.deploy.engine.bq_client.subprocess.run", side_effect=_fake_run),
    ):
        runner = bq_cli_query_runner("demo-proj", labels={"apphub_application_id": "app-1"})
        assert runner("SELECT 1") == []
        assert runner("UPDATE tbl SET x = 1 WHERE TRUE") == []
        assert "--label=workload:bq-eval" in seen_cmds[0]
        assert "--label=apphub_application_id:app-1" in seen_cmds[0]

        errs = apply_bootstrap_statements(
            "demo-proj",
            [("ddl#1", "CREATE SCHEMA IF NOT EXISTS `demo-proj.agent_eval`")],
            mode="light",
            ensure_traces_link=False,
        )
        assert errs == []

        rc_apply = cli_main(
            [
                "bootstrap",
                "--mode",
                "light",
                "--project-id",
                "demo-proj",
                "--apply",
                "--format",
                "json",
            ]
        )
        assert rc_apply == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is True
        assert payload["live_apply_executed"] is True
        assert payload["live_dry_run_executed"] is False

    _ = tmp_path


def test_blind_sme_kit_export_zero_leakage_and_import_validation(tmp_path: Path) -> None:
    """`blind_sme_kit.py` exports leak-free shuffled rater sheets and validates dual-human imports."""
    traces_jsonl = tmp_path / "traces.jsonl"
    lines: list[str] = []
    for idx in range(100):
        j_score = 4 if idx < 80 else 1
        lines.append(
            json.dumps(
                {
                    "trace_id": f"tr-sme-{idx:03d}",
                    "apphub_application_id": "kyc-address-verification",
                    "user_prompt": f"Verify utility bill #{idx}",
                    "retrieved_context": f"Street: {100 + idx} Main St, issued within 30 days.",
                    "final_response": "Approved street proof." if idx < 80 else "PO Box rejected.",
                    "judge_ordinal_score": j_score,
                    "reasoning_trace": "SECRET_JUDGE_REASONING_DO_NOT_LEAK",
                }
            )
        )
    traces_jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

    pack_dir = tmp_path / "blind_pack"
    sealed_key = tmp_path / "sealed" / "judge_key.json"

    with pytest.raises(ValueError, match="outside out_dir"):
        export_blind_sme_kit(
            traces_jsonl=traces_jsonl,
            out_dir=pack_dir,
            sealed_key_out=pack_dir / "inside_key.json",
            min_cases=100,
        )

    rc_exp = blind_sme_main(
        [
            "export",
            "--traces-jsonl",
            str(traces_jsonl),
            "--out-dir",
            str(pack_dir),
            "--sealed-key-out",
            str(sealed_key),
            "--min-cases",
            "100",
        ]
    )
    assert rc_exp == 0
    assert sealed_key.is_file()

    csv_a = pack_dir / "rater_a_blind_sheet.csv"
    csv_b = pack_dir / "rater_b_blind_sheet.csv"
    html_a = (pack_dir / "rater_a_blind_workbench.html").read_text(encoding="utf-8")
    html_b = (pack_dir / "rater_b_blind_workbench.html").read_text(encoding="utf-8")

    for artifact_text in (
        csv_a.read_text(encoding="utf-8"),
        csv_b.read_text(encoding="utf-8"),
        html_a,
        html_b,
    ):
        assert "SECRET_JUDGE_REASONING_DO_NOT_LEAK" not in artifact_text
        assert "judge_ordinal_score" not in artifact_text

    rows_a = list(csv.DictReader(csv_a.open("r", encoding="utf-8", newline="")))
    rows_b = list(csv.DictReader(csv_b.open("r", encoding="utf-8", newline="")))
    assert len(rows_a) == len(rows_b) == 100
    assert [r["trace_id"] for r in rows_a] != [r["trace_id"] for r in rows_b]

    done_a = tmp_path / "done_a.csv"
    done_b = tmp_path / "done_b.csv"
    for src_rows, dst_path, tweak_tid in (
        (rows_a, done_a, "tr-sme-005"),
        (rows_b, done_b, "tr-sme-085"),
    ):
        with dst_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(src_rows[0].keys()))
            writer.writeheader()
            for r in src_rows:
                idx_num = int(r["trace_id"].rsplit("-", 1)[-1])
                base_sc = 4 if idx_num < 80 else 1
                if r["trace_id"] == tweak_tid:
                    base_sc = 3 if base_sc == 4 else 2
                r["human_ordinal_score"] = str(base_sc)
                writer.writerow(r)

    receipt_out = tmp_path / "sme_receipt.json"
    sql_out = tmp_path / "sme_merge.sql"

    rc_bot = blind_sme_main(
        [
            "import",
            "--rater-a-csv",
            str(done_a),
            "--rater-b-csv",
            str(done_b),
            "--rater-a-id",
            "gemini_bot_1",
            "--rater-b-id",
            "alice_sme",
            "--sealed-key",
            str(sealed_key),
            "--receipt-out",
            str(receipt_out),
        ]
    )
    assert rc_bot == 2

    res = import_blind_sme_annotations(
        rater_a_csv=done_a,
        rater_b_csv=done_b,
        rater_a_id="alice_ops_lead",
        rater_b_id="bob_risk_officer",
        sealed_key_path=sealed_key,
        receipt_out=receipt_out,
        bq_sql_out=sql_out,
        project_id="demo-proj",
    )
    assert res["trace_count"] == 100
    assert res["distinct_raters"] == 2
    assert res["human_vs_human_ac2"] > 0.90
    assert res["human_vs_llm_ac2"] > 0.90
    assert "`demo-proj.agent_eval.sme_labels`" in sql_out.read_text(encoding="utf-8")


def test_committed_m16_live_cloud_proof_receipt_and_blind_traces() -> None:
    """Committed M16 live cloud receipt and 100 anonymized traces pass SHA-256 and blinding checks."""
    res_dir = REPO_ROOT / "benchmarks" / "results"
    sha_path = res_dir / "SHA256SUMS"
    proof_path = res_dir / "m16_live_cloud_proof.json"
    anon_path = res_dir / "m16_anonymized_traces_100.jsonl"

    verify_receipt_sha256(proof_path, sha_path)
    verify_receipt_sha256(anon_path, sha_path)

    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["schema_version"] == "m16-live-proof-v1"
    assert proof["infrastructure_bootstrap"]["terraform_applied"] is True
    assert proof["infrastructure_bootstrap"]["bootstrap_apply_passed"] is True
    assert proof["infrastructure_bootstrap"]["bootstrap_dry_run_passed"] is True
    assert proof["infrastructure_bootstrap"]["power_policy"] == "hibernate"

    cliff = proof["unconfounded_context_cliff"]
    assert cliff["model_name"] == "gemini-3.8-flash"
    assert cliff["short_arm_count"] == 30
    assert cliff["long_arm_count"] == 30
    assert cliff["cost_multiplier"] > 200.0
    assert cliff["burndown_multiplier"] > 200.0

    s5 = proof["s5_concurrent_quota_lock"]
    assert s5["zero_overspend"] is True
    assert s5["tokens_burned_and_reserved_today"] <= s5["cap_tokens"]
    assert all(c["returncode"] == 0 for c in s5["concurrent_calls"])

    s15 = proof["s15_views_and_job_costs"]
    assert s15["deployed_view_count"] >= 37
    assert s15["queried_view_row_counts"]["v_outcomes"] == 60
    assert s15["queried_view_row_counts"]["v_infra_cost_estimate"] == 60
    assert len(s15["bq_query_cost_attribution"]) >= 1

    anon_rows = [
        json.loads(line)
        for line in anon_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(anon_rows) == 100
    assert all("judge_ordinal_score" not in row for row in anon_rows)

    cfg = M16LiveProofConfig(target_project_id="proj-a", source_project_id="proj-b")
    assert cfg.per_arm == 30
    assert sanitize_text("projects/secret-id/123456789012", ("secret-id",)) == (
        "projects/showcase-project/100000000000"
    )
