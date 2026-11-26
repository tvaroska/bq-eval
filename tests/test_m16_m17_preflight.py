"""Unit tests for M16 pre-flight helpers (`--apply`, job labels) and M17 `blind_sme_kit.py`."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.demo.bundles import verify_receipt_sha256
from bq_eval.deploy.engine.bq_client import (
    apply_bootstrap_statements,
    apply_dataset_routine_authorizations,
    bq_cli_query_runner,
    ensure_observability_traces_link,
    format_bq_job_labels,
)
from scripts.blind_sme_kit import (
    _TRAP_CASE_SLUG_RE,
    export_blind_sme_kit,
    import_blind_sme_annotations,
)
from scripts.blind_sme_kit import (
    main as blind_sme_main,
)
from scripts.m16_vertex_otel_probe import sanitize_text
from scripts.run_m16_live_proof import (
    M16LiveProofConfig,
    redact_and_shuffle_traces,
    run_live_m16_proof,
)
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


def test_apply_link_location_and_stop_on_first_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`S-21`/`S-26`/`T-13`: opt-in link, `--location`, stop on first DDL failure, routine auth & label/JSON guards."""
    # S-26: BigQuery label keys cannot start with a digit.
    with pytest.raises(ValueError, match="Invalid BigQuery job label"):
        format_bq_job_labels({"1badkey": "ok"})
    assert format_bq_job_labels({"env_1": "2026"}) == (
        "--label=env_1:2026",
        "--label=workload:bq-eval",
    )

    # S-26: Unexpected non-JSON stdout raises ValueError instead of silently returning [].
    bad_stdout_proc = MagicMock(returncode=0, stdout="<html>proxy error</html>", stderr="")
    with (
        patch("bq_eval.deploy.engine.bq_client.shutil.which", return_value="/usr/bin/bq"),
        patch("bq_eval.deploy.engine.bq_client.subprocess.run", return_value=bad_stdout_proc),
    ):
        runner = bq_cli_query_runner("demo-proj")
        with pytest.raises(ValueError, match="Unexpected non-JSON stdout"):
            runner("SELECT 1")

    # S-21: `ensure_observability_traces_link` checks `gcloud observability settings update` rc != 0.
    fail_settings = MagicMock(returncode=1, stdout="", stderr="PERMISSION_DENIED on settings")
    with (
        patch("bq_eval.deploy.engine.bq_client.shutil.which", return_value="/usr/bin/gcloud"),
        patch(
            "bq_eval.deploy.engine.bq_client.subprocess.run", return_value=fail_settings
        ) as m_run,
    ):
        errs = ensure_observability_traces_link("demo-proj", location="eu")
        assert len(errs) == 1 and "observability settings update failed" in errs[0]
        assert "--default-storage-location=eu" in m_run.call_args.args[0]

    # S-21: `apply_bootstrap_statements` passes `--location` and stops on FIRST DDL failure.
    executed: list[list[str]] = []

    def _fail_first(cmd: list[str], **_kwargs: object) -> MagicMock:
        executed.append(list(cmd))
        return MagicMock(returncode=1, stdout="", stderr="Syntax error on stmt 1")

    with (
        patch("bq_eval.deploy.engine.bq_client.shutil.which", return_value="/usr/bin/bq"),
        patch("bq_eval.deploy.engine.bq_client.subprocess.run", side_effect=_fail_first),
    ):
        errs = apply_bootstrap_statements(
            "demo-proj",
            [("stmt#1", "SELECT 1"), ("stmt#2", "SELECT 2"), ("stmt#3", "SELECT 3")],
            mode="full",
            location="EU",
            ensure_traces_link=False,
        )
        assert len(errs) == 1 and "stmt#1" in errs[0]
        assert len(executed) == 1  # Stopped immediately on first failure!
        assert "--location=EU" in executed[0]

    # S-21: `apply_dataset_routine_authorizations` re-applies routine entries cleanly & stops on error.
    show_ok = MagicMock(
        returncode=0,
        stdout=json.dumps({"access": [{"role": "OWNER", "specialGroup": "projectOwners"}]}),
        stderr="",
    )
    upd_ok = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch("bq_eval.deploy.engine.bq_client.shutil.which", return_value="/usr/bin/bq"),
        patch("bq_eval.deploy.engine.bq_client.subprocess.run", side_effect=[show_ok, upd_ok]),
    ):
        auth_errs = apply_dataset_routine_authorizations(
            "demo-proj",
            {"obs_traces": (("demo-proj", "silver_internal", "spans"),)},
        )
        assert auth_errs == []

    # S-21: CLI `--ensure-observability-link` is opt-in (off by default) and forwards `--location`.
    cli_cmds: list[list[str]] = []

    def _cli_ok(cmd: list[str], **_kwargs: object) -> MagicMock:
        cli_cmds.append(list(cmd))
        if "show" in cmd:
            return MagicMock(returncode=0, stdout='{"access":[]}', stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("bq_eval.deploy.engine.bq_client.shutil.which", return_value="/usr/bin/bq"),
        patch("bq_eval.deploy.engine.bq_client.subprocess.run", side_effect=_cli_ok),
    ):
        rc = cli_main(
            [
                "bootstrap",
                "--mode",
                "silver",
                "--project-id",
                "demo-proj",
                "--location",
                "EU",
                "--apply",
                "--format",
                "json",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "silver"
        assert payload["live_apply_executed"] is True
        assert any("--location=EU" in c for c in cli_cmds)
        assert not any("observability" in c for c in cli_cmds)  # opt-in only!
        assert any("update" in c and "demo-proj:silver_internal" in c for c in cli_cmds)

    # S-21: CLI `--dry-run` also forwards `--location`.
    dry_cmds: list[list[str]] = []

    def _dry_ok(cmd: list[str], **_kwargs: object) -> MagicMock:
        dry_cmds.append(list(cmd))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("bq_eval.cli.bootstrap_cmd.shutil.which", return_value="/usr/bin/bq"),
        patch("bq_eval.cli.bootstrap_cmd.subprocess.run", side_effect=_dry_ok),
    ):
        rc_dry = cli_main(
            [
                "bootstrap",
                "--mode",
                "silver",
                "--project-id",
                "demo-proj",
                "--location",
                "EU",
                "--dry-run",
                "--format",
                "json",
            ]
        )
        assert rc_dry == 0
        dry_payload = json.loads(capsys.readouterr().out)
        assert dry_payload["live_dry_run_executed"] is True
        assert dry_cmds and all("--location=EU" in c for c in dry_cmds)


def _assert_blind_pack_hygiene(
    artifact_texts: tuple[str, ...],
    html_texts: tuple[str, ...],
) -> None:
    leaked_markers = (
        "SECRET_JUDGE_REASONING_DO_NOT_LEAK",
        "judge_ordinal_score",
        "kyc-s2trap",
        "kyc-clean",
        "kyc-pobox",
        "kyc-ocr",
        "kyc-struct",
    )
    for artifact_text in artifact_texts:
        assert "kyc-address-verification" in artifact_text
        assert "kyc-case-redacted" in artifact_text
        for marker in leaked_markers:
            assert marker not in artifact_text

    for html_text in html_texts:
        assert '<link rel="icon"' in html_text
        assert "function safeGet(k)" in html_text
        assert "function safeSet(k, v)" in html_text
        assert "window.name" in html_text
        assert "const state = JSON.parse(localStorage" not in html_text
        assert html_text.count("window.localStorage.") == 2


def test_blind_sme_kit_export_zero_leakage_and_import_validation(tmp_path: Path) -> None:
    """`blind_sme_kit.py` exports leak-free shuffled rater sheets and validates dual-human imports."""
    traces_jsonl = tmp_path / "traces.jsonl"
    slug_variants = (
        "kyc-s2trap-001",
        "kyc-clean-pdf-002-b1",
        "kyc-pobox-003",
        "kyc-ocr-004",
        "kyc-struct-005-t2",
    )
    lines: list[str] = []
    for idx in range(100):
        j_score = 4 if idx < 80 else 1
        slug = slug_variants[idx % len(slug_variants)]
        lines.append(
            json.dumps(
                {
                    "trace_id": f"tr-sme-{idx:03d}",
                    "apphub_application_id": "kyc-address-verification",
                    "user_prompt": f"Verify utility bill #{idx} (case={slug})",
                    "retrieved_context": (
                        f"doc={slug}-d1 Street: {100 + idx} Main St, uri=gs://b/{slug}-d1.pdf"
                    ),
                    "final_response": (
                        f"Approved {slug} proof." if idx < 80 else f"PO Box rejected ({slug})."
                    ),
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

    _assert_blind_pack_hygiene(
        (
            csv_a.read_text(encoding="utf-8"),
            csv_b.read_text(encoding="utf-8"),
            html_a,
            html_b,
        ),
        (html_a, html_b),
    )

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


@dataclass(frozen=True)
class _DummyCliffReport:
    model_name: str = "gemini-3.8-flash"
    short_arm_count: int = 30
    long_arm_count: int = 30
    cost_multiplier: float = 250.0


def test_committed_blind_traces_hide_score_order_and_slugs(tmp_path: Path) -> None:
    """Committed blind traces strip scores/slugs, shuffle rows, and default deploy status to False."""
    anon_path = REPO_ROOT / "benchmarks" / "results" / "m16_anonymized_traces_100.jsonl"
    raw_lines = [
        line for line in anon_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(raw_lines) == 100
    rows = [json.loads(line) for line in raw_lines]

    for line, row in zip(raw_lines, rows, strict=True):
        assert str(row["trace_id"]).startswith("tr-opaque-")
        if row.get("outcome_id") is not None:
            assert str(row["outcome_id"]).startswith("out-opaque-")
        assert "judge_ordinal_score" not in row
        assert "reasoning_trace" not in row
        assert _TRAP_CASE_SLUG_RE.search(line) is None

    assert any("kyc-case-redacted-" in line for line in raw_lines)
    trace_ids = [str(r["trace_id"]) for r in rows]
    assert trace_ids != sorted(trace_ids)

    raw_sample = [
        {
            "trace_id": f"raw-tr-{idx:03d}",
            "outcome_id": f"raw-out-{idx:03d}",
            "context_id": f"ctx-raw-{idx:03d}",
            "user_prompt": f"context=ctx-raw-{idx:03d} case=kyc-s2trap-{idx:03d}",
            "retrieved_context": f'{{"context_id":"ctx-raw-{idx:03d}","doc_id":"kyc-clean-{idx:03d}"}}',
            "final_response": f"trace raw-tr-{idx:03d} outcome raw-out-{idx:03d}",
            "judge_ordinal_score": 1 if idx < 5 else 4,
            "reasoning_trace": "SECRET_REASONING",
        }
        for idx in range(10)
    ]
    blind_a = redact_and_shuffle_traces(raw_sample)
    blind_b = redact_and_shuffle_traces(raw_sample)
    assert blind_a == blind_b
    assert [r["trace_id"] for r in blind_a] != sorted(r["trace_id"] for r in blind_a)
    for item in blind_a:
        blob = json.dumps(item)
        assert "raw-tr-" not in blob
        assert "raw-out-" not in blob
        assert "ctx-raw-" not in blob
        assert "SECRET_REASONING" not in blob
        assert "judge_ordinal_score" not in item
        assert "ctx-opaque-" in item["user_prompt"]
        assert "ctx-opaque-" in item["retrieved_context"]
        assert _TRAP_CASE_SLUG_RE.search(blob) is None

    cfg = M16LiveProofConfig(
        target_project_id="demo-target",
        source_project_id="demo-src",
        provision_infra=False,
        reuse_ingested_cliff=True,
        receipt_out=tmp_path / "proof.json",
        traces_out=tmp_path / "raw.jsonl",
        anonymized_traces_out=tmp_path / "anon.jsonl",
    )
    with patch.multiple(
        "scripts.run_m16_live_proof",
        upload_seed_completion_jsonl=MagicMock(),
        _ensure_cliff_arms=MagicMock(return_value=([], [])),
        analyze_unconfounded_context_cliff_arms=MagicMock(return_value=_DummyCliffReport()),
        _wait_for_live_traces=MagicMock(return_value={"span_count": 120}),
        verify_concurrent_quota_lock_live=MagicMock(return_value={"zero_overspend": True}),
        verify_live_views_and_sync_jobs=MagicMock(return_value={"deployed_view_count": 37}),
        export_apphub_traces_and_sme_candidates=MagicMock(
            return_value={"exported_trace_count": 100, "traces_sha256": "a" * 64}
        ),
    ):
        receipt = run_live_m16_proof(cfg)

    infra = receipt["infrastructure_bootstrap"]
    assert infra["provision_infra_requested"] is False
    assert infra["terraform_applied"] is False
    assert infra["terraform_exit_code"] is None
    assert infra["power_policy"] is None
    assert infra["bootstrap_apply_passed"] is False
    assert infra["bootstrap_apply_exit_code"] is None
    assert infra["bootstrap_dry_run_passed"] is False
    assert infra["bootstrap_dry_run_exit_code"] is None
