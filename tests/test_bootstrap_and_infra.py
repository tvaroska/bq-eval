"""`bq-eval bootstrap --mode silver|gold`, WORM/RTBF Terraform, wheel packaging & `bq_query_jobs` CPVO."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.deploy.engine.bootstrap import (
    normalize_payload_prefix,
    substitute_sql_parameters,
    validate_bootstrap_offline,
)
from bq_eval.deploy.engine.silver_deploy import validate_view_columns_ordered
from scripts.empirical_proof import (
    BlindHumanLabelRow,
    analyze_unconfounded_context_cliff_arms,
    build_bq_query_cost_row,
    build_information_schema_jobs_sync_sql,
    compute_bq_query_cost_usd,
    compute_escalation_wage_sensitivity,
    sum_bq_query_costs_by_app,
    validate_blind_dual_human_labels,
)
from scripts.empirical_proof import main as empirical_proof_main
from tests.conftest import (
    REPO_ROOT,
    SQL_DIR,
    eval_traces,
    make_span,
    open_runner_for_spans,
    parse_bigquery_sql,
    seed_sme_confusion,
)


def test_bootstrap_offline_schema_all_modes_and_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bq-eval bootstrap --mode silver|gold` validates SQL, enforces open-only boundaries, and requires `--project-id` on `--dry-run`/`--apply`."""
    for mode in ("silver", "gold"):
        report, stmts = validate_bootstrap_offline(
            mode=mode,
            project_id="cold-proj-01",
            dataset_id="agent_eval",
            connection_id="us.vertex_ai_connection",
            payload_bucket="cold-proj-01",
            payload_prefix="bq-eval",
        )
        assert report.passed, f"{mode} validation errors: {report.errors}"
        assert report.unresolved_placeholders == ()
        assert report.statement_count == len(stmts)
        joined = "\n;\n".join(stmt for _, stmt in stmts)
        assert "YOUR_GENAI_PAYLOAD_BUCKET" not in joined
        assert "${" not in joined
        assert "omni_spoke_views" not in joined
        if mode == "silver":
            assert all(label.startswith("silver/") for label, _ in stmts)
            assert any("silver/capacity/requests.sql" in label for label, _ in stmts)
        else:
            assert "MERGE INTO `cold-proj-01.agent_eval.rubrics`" in joined
            assert (
                "CREATE OR REPLACE VIEW `cold-proj-01.agent_eval.v_agent_eval_and_finops_summary`"
                in joined
            )

    assert cli_main(["bootstrap", "--mode", "gold", "--dry-run"]) == 2
    capsys.readouterr()
    assert cli_main(["bootstrap", "--mode", "silver", "--apply"]) == 2
    capsys.readouterr()

    sql_out = tmp_path / "rendered_gold.sql"
    rc = cli_main(
        [
            "bootstrap",
            "--mode",
            "gold",
            "--project-id",
            "cold-proj-01",
            "--payload-bucket",
            "cold-proj-01",
            "--payload-prefix",
            "bq-eval/",
            "--sql-out",
            str(sql_out),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["mode"] == "gold"
    assert sql_out.is_file()
    rendered_text = sql_out.read_text(encoding="utf-8")
    assert "${" not in rendered_text

    assert normalize_payload_prefix("/bq-eval") == "bq-eval/"
    assert normalize_payload_prefix("") == ""
    with pytest.raises(ValueError, match="Invalid"):
        substitute_sql_parameters("SELECT 1", project_id="bad; DROP TABLE x")
    with pytest.raises(ValueError, match="Invalid payload_prefix"):
        normalize_payload_prefix("../escape")


def test_offline_validator_rejects_renamed_span_column(tmp_path: Path) -> None:
    """`S-25` ordered SELECT alias check catches renamed Silver columns and open-only Gold boundary leaks."""
    mutated_sql = tmp_path / "sql"
    shutil.copytree(SQL_DIR, mutated_sql)

    # Open-only Gold file referencing silver_restricted MUST fail offline validation (`ADR-049`).
    cost_file = mutated_sql / "core" / "gold_cost_views.sql"
    orig_cost = cost_file.read_text(encoding="utf-8")
    cost_file.write_text(
        orig_cost  # noqa: S608
        + "\nCREATE OR REPLACE VIEW `agent_eval.v_bad_leak` AS SELECT 1 AS x FROM `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.traces`(CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP());\n",
        encoding="utf-8",
    )
    leak_report, _ = validate_bootstrap_offline(
        mode="gold",
        project_id="cold-proj-01",
        sql_dir=mutated_sql,
    )
    assert not leak_report.passed
    assert any("open-only Gold file" in err for err in leak_report.errors), leak_report.errors
    cost_file.write_text(orig_cost, encoding="utf-8")

    swapped_errors = validate_view_columns_ordered(
        "CREATE OR REPLACE VIEW v AS SELECT b AS col_b, a AS col_a FROM t",
        ("col_a", "col_b"),
        view_name="v",
    )
    assert any("out of schema order" in err for err in swapped_errors), swapped_errors
    dup_errors = validate_view_columns_ordered(
        "CREATE OR REPLACE VIEW v AS SELECT b AS col_b, a AS col_a, c AS col_b FROM t",
        ("col_a", "col_b"),
        view_name="v",
    )
    assert any("duplicate" in err for err in dup_errors), dup_errors
    extra_errors = validate_view_columns_ordered(
        "CREATE OR REPLACE VIEW v AS SELECT a AS col_a, b AS col_b, c AS col_extra FROM t",
        ("col_a", "col_b"),
        view_name="v",
        exact=True,
    )
    assert any("unexpected extra" in err for err in extra_errors), extra_errors

    mc_file = mutated_sql / "silver" / "model_calls.sql"
    mc_text = mc_file.read_text(encoding="utf-8")
    assert "\n    latency_ms,\n" in mc_text
    mc_file.write_text(
        mc_text.replace("\n    latency_ms,\n", "\n    latency_ms AS latency_ms_renamed,\n", 1),
        encoding="utf-8",
    )
    bad_silver_report, _ = validate_bootstrap_offline(
        mode="silver",
        project_id="cold-proj-01",
        sql_dir=mutated_sql,
    )
    assert not bad_silver_report.passed
    assert any("latency_ms" in err for err in bad_silver_report.errors), bad_silver_report.errors


def test_terraform_dockerfile_and_cloudbuild_security_contracts() -> None:
    """Verify Terraform WORM/RTBF + standalone/co-located layout, non-root Dockerfile & Trivy Cloud Build gate."""
    tf_dir = REPO_ROOT / "terraform"
    for rel in (
        "versions.tf",
        "variables.tf",
        "main.tf",
        "outputs.tf",
        "tests/standalone.tftest.hcl",
        "tests/colocated.tftest.hcl",
    ):
        assert (tf_dir / rel).is_file(), f"Missing terraform/{rel}"

    tf_main = (tf_dir / "main.tf").read_text(encoding="utf-8")
    tf_vars = (tf_dir / "variables.tf").read_text(encoding="utf-8")
    tf_vers = (tf_dir / "versions.tf").read_text(encoding="utf-8")
    assert 'resource "google_storage_bucket" "gold_worm_archive"' in tf_main
    assert "retention_policy" in tf_main
    assert 'resource "google_kms_key_ring" "rtbf_subject_keys"' in tf_main
    assert 'resource "time_sleep" "biglake_iam_propagation"' in tf_main
    assert "hashicorp/time" in tf_vers
    assert "google_bigquery_table" not in tf_main or "completions" not in tf_main
    assert "google_bigquery_connection" in tf_main
    assert "google_bigquery_data_transfer_config" in tf_main
    assert 'default     = "hibernate"' in tf_vars
    for flag in (
        "create_shared_bucket",
        "create_eval_dataset",
        "create_vertex_connection",
        "create_worm_archive_bucket",
        "lock_worm_retention",
        "create_rtbf_kms_keyring",
        "iam_propagation_wait_duration",
    ):
        assert f'variable "{flag}"' in tf_vars

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ghcr.io/astral-sh/uv:0.12.24" in dockerfile
    assert "USER 10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/healthz" in dockerfile

    cb_text = (REPO_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    agents_text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    gate_text = (REPO_ROOT / "scripts" / "gate.sh").read_text(encoding="utf-8")
    assert "scripts/gate.sh" in cb_text
    assert "scripts/gate.sh" in agents_text
    assert "uv sync --frozen --extra dev" in gate_text
    assert ".venv/bin/ast-grep scan --rule specs/ast.rules.yaml" in gate_text
    assert "pytest -p no:cacheprovider -v -rs" in gate_text
    assert "bootstrap --mode silver --project-id demo-proj" in gate_text
    assert "bootstrap --mode gold --project-id demo-proj" in gate_text
    assert "blind_sme_kit.py export" in gate_text
    assert "hashicorp/terraform:" in cb_text
    assert "aquasec/trivy:" in cb_text
    assert "--pkg-types" in cb_text and "os,library" in cb_text
    assert re.search(r"--exit-code\s+1|--exit-code\",\s*\"1\"", cb_text)


def test_wheel_contains_sql_and_receipt(tmp_path: Path) -> None:
    """Built wheel ships bundled Silver + Gold SQL & receipt data and runs `demo` + `bootstrap --mode gold` outside checkout (`T-7`)."""
    uv_bin = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    assert Path(uv_bin).is_file(), "uv binary required to build wheel offline"

    wheel_dir = tmp_path / "dist"
    wheel_dir.mkdir()
    build = subprocess.run(  # noqa: S603
        [
            uv_bin,
            "build",
            "--wheel",
            "--offline",
            "--no-config",
            "--out-dir",
            str(wheel_dir),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Offline wheel build failed:\n{build.stdout}\n{build.stderr}"

    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
        for required in (
            "bq_eval/_bundled/sql/silver/spans.sql",
            "bq_eval/_bundled/sql/silver/capacity/requests.sql",
            "bq_eval/_bundled/sql/core/ddl.sql",
            "bq_eval/_bundled/sql/core/pricing_seed.sql",
            "bq_eval/_bundled/sql/core/gold_cost_views.sql",
            "bq_eval/_bundled/sql/core/gold_eval_gates.sql",
            "bq_eval/_bundled/sql/core/summary_views.sql",
            "bq_eval/_bundled/sql/core/step2_stateless_eval.sql",
            "bq_eval/_bundled/sql/finops/capacity_views.sql",
            "bq_eval/_bundled/benchmarks/results/m2_live_receipt.json",
            "bq_eval/_bundled/benchmarks/results/m16_live_cloud_proof.json",
            "bq_eval/_bundled/benchmarks/results/SHA256SUMS",
            "bq_eval/_bundled/benchmarks/results/bq_dry_run_receipt.json",
        ):
            assert required in names, f"Missing {required} inside wheel"
        zf.extractall(unpacked)

    purelib = sysconfig.get_path("purelib")
    platlib = sysconfig.get_path("platlib")
    assert purelib is not None and platlib is not None
    probe = (
        "import io, json, pathlib, sys\n"
        "from contextlib import redirect_stdout\n"
        f"unpacked = pathlib.Path({str(unpacked.resolve())!r})\n"
        f"sys.path[:0] = [str(unpacked), {purelib!r}, {platlib!r}]\n"
        "import bq_eval\n"
        "assert pathlib.Path(bq_eval.__file__).resolve().is_relative_to(unpacked), bq_eval.__file__\n"
        "from bq_eval.cli.app import main\n"
        "buf_demo = io.StringIO()\n"
        "with redirect_stdout(buf_demo):\n"
        "    rc_demo = main(['demo', '--format', 'json'])\n"
        "assert rc_demo == 0, rc_demo\n"
        "assert 'kpi_table' in json.loads(buf_demo.getvalue())\n"
        "buf_boot = io.StringIO()\n"
        "with redirect_stdout(buf_boot):\n"
        "    rc_boot = main(['bootstrap', '--mode', 'gold', '--format', 'json'])\n"
        "assert rc_boot == 0, rc_boot\n"
        "boot_payload = json.loads(buf_boot.getvalue())\n"
        "assert boot_payload['passed'] is True and boot_payload['statement_count'] > 0\n"
        "print('WHEEL_OK')\n"
    )
    run = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, f"Unpacked wheel CLI failed:\n{run.stdout}\n{run.stderr}"
    assert run.stdout.strip() == "WHEEL_OK"


def test_bq_query_cost_folded_into_cpvo_runner_parity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`INFORMATION_SCHEMA.JOBS` query cost folds into `total_spend_usd` and CPVO across Gold SQL & `OfflineEvalBackend`."""
    sync_sql = build_information_schema_jobs_sync_sql(
        project_id="demo-proj",
        dataset_id="agent_eval",
        region="us",
        lookback_days=7,
    )
    parse_bigquery_sql(sync_sql, "information_schema_jobs_sync_sql")
    assert "INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sync_sql
    assert empirical_proof_main(["--project-id", "demo-proj"]) == 0
    assert "INFORMATION_SCHEMA.JOBS_BY_PROJECT" in capsys.readouterr().out

    on_demand_row = build_bq_query_cost_row(
        job_id="job-od-1",
        apphub_application_id="query-cost-app",
        outcome_id="out-qc-1",
        total_bytes_billed=1024**4,
        pricing_mode="on_demand",
    )
    slot_row = build_bq_query_cost_row(
        job_id="job-slot-1",
        apphub_application_id="query-cost-app",
        outcome_id="out-qc-1",
        total_slot_ms=3_600_000,
        pricing_mode="capacity_slot",
    )
    assert on_demand_row.query_cost_usd == pytest.approx(6.25, abs=1e-12)
    assert slot_row.query_cost_usd == pytest.approx(0.04, abs=1e-12)
    assert compute_bq_query_cost_usd(total_bytes_billed=0, total_slot_ms=0) == 0.0
    assert sum_bq_query_costs_by_app([on_demand_row, slot_row])["query-cost-app"] == pytest.approx(
        6.29, abs=1e-12
    )

    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            f"trace-qc-{i:02d}",
            f"sp-qc-{i:02d}",
            app_id="query-cost-app",
            outcome_id="out-qc-1" if i == 1 else f"out-qc-{i:02d}",
            outcome_owner="query-cost-app",
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            latency_ms=1000.0,
            offset_s=i * 2,
            base_time=base,
        )
        for i in range(1, 11)
    ]
    close_span = make_span(
        "trace-qc-01",
        "sp-qc-close",
        parent_span_id="sp-qc-01",
        span_name="outcome.closed",
        app_id="query-cost-app",
        outcome_id="out-qc-1",
        outcome_owner="query-cost-app",
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        latency_ms=0.0,
        offset_s=3,
        attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
        base_time=base,
    )
    all_spans = [*spans, close_span]
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)

    with open_runner_for_spans(all_spans, bq_query_jobs=[on_demand_row, slot_row]) as runner:
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": f"ev-qc-{i:02d}",
                    "eval_run_id": "run-qc",
                    "trace_id": f"trace-qc-{i:02d}",
                    "outcome_id": "out-qc-1" if i == 1 else f"out-qc-{i:02d}",
                    "apphub_application_id": "query-cost-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "reasoning_trace": "ok",
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 1_000,
                    "judge_output_tokens": 100,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-22T12:02:00+00:00",
                }
                for i in range(1, 11)
            ],
            append=True,
        )
        seed_sme_confusion(runner, "query-cost-app", cm)
        bq_sum = runner.query(
            "SELECT bq_query_cost_usd, total_spend_usd, cost_per_verified_outcome_usd "
            "FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'query-cost-app'"
        ).rows[0]
        bq_cpvo = runner.query(
            "SELECT bq_query_cost_usd, cost_per_verified_outcome_usd "
            "FROM `agent_eval.v_enterprise_cost_per_verified_outcome` "
            "WHERE apphub_application_id = 'query-cost-app'"
        ).rows[0]

    twin = eval_traces(
        all_spans,
        judge_outcomes={f"trace-qc-{i:02d}": True for i in range(1, 11)},
        sme_confusion=cm,
        bq_query_jobs=[on_demand_row, slot_row],
    )["query-cost-app"]

    assert float(str(bq_sum["bq_query_cost_usd"])) == pytest.approx(6.29, abs=1e-12)
    assert float(str(bq_cpvo["bq_query_cost_usd"])) == pytest.approx(6.29, abs=1e-12)
    assert twin.bq_query_cost_usd == pytest.approx(6.29, abs=1e-12)
    assert float(str(bq_cpvo["cost_per_verified_outcome_usd"])) > 6.29
    assert (
        twin.cost_per_verified_outcome_usd is not None
        and twin.cost_per_verified_outcome_usd > 0.629
    )
    assert (
        twin.outcome_cost_per_verified_usd is not None and twin.outcome_cost_per_verified_usd > 6.29
    )


def test_dual_human_validation_unconfounded_cliff_and_wage_sensitivity() -> None:
    """Validators reject synthetic raters/confounded arms, compute cut-point agreement, and model wage sensitivity."""
    valid_rows: list[BlindHumanLabelRow] = []
    for i in range(100):
        human_score = 4 if i < 82 else 2
        judge_score = 4 if i < 80 else (4 if i == 99 else 2)
        for rater in ("sme_alice", "sme_bob"):
            valid_rows.append(
                BlindHumanLabelRow(
                    trace_id=f"tr-sme-{i:03d}",
                    rater_id=rater,
                    human_ordinal_score=human_score,
                    judge_ordinal_score=judge_score,
                    blind_to_judge=True,
                )
            )

    panel = validate_blind_dual_human_labels(valid_rows)
    assert panel.trace_count == 100
    assert panel.distinct_raters == 2
    assert panel.human_vs_human_ac2 == pytest.approx(1.0, abs=1e-12)
    assert panel.human_vs_llm_ac2 > 0.90
    assert panel.binary_cutpoint_agreement == pytest.approx(0.97, abs=1e-12)
    assert panel.confusion_matrix.total == 100

    with pytest.raises(ValueError, match="non-human/synthetic"):
        validate_blind_dual_human_labels(
            [BlindHumanLabelRow("t1", "gemini-4-argon", 4, 4, blind_to_judge=True)],
            min_cases=1,
        )
    with pytest.raises(ValueError, match="not blind"):
        validate_blind_dual_human_labels(
            [BlindHumanLabelRow("t1", "sme_alice", 4, 4, blind_to_judge=False)],
            min_cases=1,
        )
    with pytest.raises(ValueError, match="required >= 100"):
        validate_blind_dual_human_labels(valid_rows[:20])

    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    short_arm: list[OTelGenAISpanRecord] = [
        make_span(
            f"tr-short-{i}",
            f"sp-short-{i}",
            model_name="gemini-4-argon",
            input_tokens=150_000,
            cached_input_tokens=0,
            output_tokens=2_000,
            thinking_tokens=0,
            base_time=base,
        )
        for i in range(30)
    ]
    long_arm: list[OTelGenAISpanRecord] = [
        make_span(
            f"tr-long-{i}",
            f"sp-long-{i}",
            model_name="gemini-4-argon",
            input_tokens=250_000,
            cached_input_tokens=0,
            output_tokens=2_000,
            thinking_tokens=0,
            base_time=base,
        )
        for i in range(30)
    ]
    cliff = analyze_unconfounded_context_cliff_arms(short_arm, long_arm)
    assert cliff.model_name == "gemini-4-argon"
    assert cliff.cost_multiplier > 3.0
    assert cliff.burndown_multiplier > 3.0

    mixed_long = [long_arm[0].model_copy(update={"model_name": "gemini-3.8-flash"}), *long_arm[1:]]
    with pytest.raises(ValueError, match="Confounded arms"):
        analyze_unconfounded_context_cliff_arms(short_arm, mixed_long)

    curve = compute_escalation_wage_sensitivity(
        verified_outcomes=100,
        agent_model_usd=10.0,
        judge_usd=1.0,
        bq_query_usd=0.5,
        escalated_cases=10,
        avg_review_minutes_per_escalation=12.0,
        hourly_wages_usd=(50.0, 150.0, 250.0),
    )
    assert len(curve) == 3
    assert curve[0].human_escalation_cost_per_verified_outcome_usd == pytest.approx(1.0, abs=1e-12)
    assert curve[2].fully_loaded_cpvo_usd == pytest.approx(0.10 + 0.01 + 0.005 + 5.0, abs=1e-12)
