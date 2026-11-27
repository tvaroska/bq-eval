"""Blind dual-human SME calibration kit (`M19-3`: `T-11`, `T-12`, `T-16`) & deploy branch guards (`T-13`, `T-17`)."""

from __future__ import annotations

import csv
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq_eval.cli.bootstrap_cmd import _run_gcp_dry_run
from bq_eval.core.contract.otel import OTelGenAISpanRecord, ServingTrafficType
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.calibration import (
    evaluate_calibration_agreement_gate,
    rogan_gladen_calibrate,
)
from bq_eval.core.engine.cost import compute_span_burndown_tokens, compute_span_cost_usd
from bq_eval.deploy.engine.bootstrap import (
    split_sql_statements_stdlib,
    validate_bootstrap_offline,
)
from bq_eval.deploy.engine.bq_client import (
    apply_bootstrap_statements,
    apply_dataset_routine_authorizations,
    batch_bootstrap_statements,
    bq_cli_query_runner,
    ensure_observability_traces_link,
    format_bq_job_labels,
)
from scripts.blind_sme_kit import (
    export_blind_sme_kit,
    import_blind_sme_annotations,
)
from scripts.blind_sme_kit import (
    main as blind_kit_main,
)
from scripts.empirical_proof import validate_human_rater_id
from scripts.run_m16_live_proof import redact_and_shuffle_traces
from tests.conftest import open_runner_for_spans


def _write_judged_traces_jsonl(path: Path, count: int = 12) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i in range(count):
        is_trap = i >= (count // 2)
        rows.append(
            {
                "trace_id": f"tr-sme-{i:03d}",
                "apphub_application_id": "kyc-address-verification",
                "rule0_pass": i < (count - 1),
                "user_prompt": f"Verify kyc-s2trap-0{i} residential address."
                if is_trap
                else f"Verify kyc-clean-0{i} utility bill.",
                "retrieved_context": f"Applicant doc #{i}",
                "final_response": "Rejected trap." if is_trap else "Approved street address.",
                "judge_ordinal_score": 1 if is_trap else 4,
                "reasoning_trace": "SECRET_JUDGE_REASONING_MUST_NOT_LEAK",
            }
        )
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return rows


def _fill_blind_csv(
    src_csv: Path,
    dst_csv: Path,
    score_by_trace: Mapping[str, object],
) -> None:
    with src_csv.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
        fieldnames = list(rows[0].keys())
    out_rows: list[dict[str, object]] = []
    for row in rows:
        tid = str(row["trace_id"])
        if tid not in score_by_trace:
            continue
        updated = dict(row)
        updated["human_ordinal_score"] = score_by_trace[tid]
        out_rows.append(updated)
    with dst_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)


def test_blind_import_rejects_duplicate_same_sheet_dropped_and_tampered_key(
    tmp_path: Path,
) -> None:
    """`T-11`: Blind kit prevents judge leakage and rejects same-sheet, duplicate, dropped, and tampered inputs."""
    traces_jsonl = tmp_path / "judged_traces.jsonl"
    _write_judged_traces_jsonl(traces_jsonl, count=12)
    pack_dir = tmp_path / "blind_pack"
    sealed_key = tmp_path / "sealed" / "sealed_key.json"

    with pytest.raises(ValueError, match="outside out_dir"):
        export_blind_sme_kit(
            traces_jsonl=traces_jsonl,
            out_dir=pack_dir,
            sealed_key_out=pack_dir / "leaked_key.json",
            min_cases=10,
        )

    manifest = export_blind_sme_kit(
        traces_jsonl=traces_jsonl,
        out_dir=pack_dir,
        sealed_key_out=sealed_key,
        min_cases=12,
    )
    assert manifest["case_count"] == 12
    sheet_a = pack_dir / "rater_a_blind_sheet.csv"
    sheet_b = pack_dir / "rater_b_blind_sheet.csv"
    html_a = (pack_dir / "rater_a_blind_workbench.html").read_text(encoding="utf-8")
    for text in (sheet_a.read_text(encoding="utf-8"), sheet_b.read_text(encoding="utf-8"), html_a):
        assert "SECRET_JUDGE_REASONING_MUST_NOT_LEAK" not in text
        assert "judge_ordinal_score" not in text
        assert "kyc-s2trap" not in text
        assert "kyc-clean" not in text

    with sheet_a.open("r", encoding="utf-8", newline="") as fa:
        order_a = [r["trace_id"] for r in csv.DictReader(fa)]
    with sheet_b.open("r", encoding="utf-8", newline="") as fb:
        order_b = [r["trace_id"] for r in csv.DictReader(fb)]
    assert order_a != order_b

    scores_ok = {f"tr-sme-{i:03d}": (1 if i >= 6 else 4) for i in range(12)}
    done_a = tmp_path / "done_a.csv"
    done_b = tmp_path / "done_b.csv"
    _fill_blind_csv(sheet_a, done_a, scores_ok)
    _fill_blind_csv(sheet_b, done_b, scores_ok)

    with pytest.raises(ValueError, match="same file"):
        import_blind_sme_annotations(
            rater_a_csv=done_a,
            rater_b_csv=done_a,
            rater_a_id="alice_sme",
            rater_b_id="carol_sme",
            sealed_key_path=sealed_key,
            receipt_out=tmp_path / "r.json",
            min_cases=12,
        )

    same_bytes = tmp_path / "copy_a.csv"
    same_bytes.write_bytes(done_a.read_bytes())
    with pytest.raises(ValueError, match="identical file contents"):
        import_blind_sme_annotations(
            rater_a_csv=done_a,
            rater_b_csv=same_bytes,
            rater_a_id="alice_sme",
            rater_b_id="carol_sme",
            sealed_key_path=sealed_key,
            receipt_out=tmp_path / "r.json",
            min_cases=12,
        )

    dup_csv = tmp_path / "dup_a.csv"
    lines = done_a.read_text(encoding="utf-8").splitlines()
    dup_csv.write_text("\n".join([*lines, lines[1]]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate trace_id"):
        import_blind_sme_annotations(
            rater_a_csv=dup_csv,
            rater_b_csv=done_b,
            rater_a_id="alice_sme",
            rater_b_id="carol_sme",
            sealed_key_path=sealed_key,
            receipt_out=tmp_path / "r.json",
            min_cases=12,
        )

    dropped_csv = tmp_path / "dropped_b.csv"
    _fill_blind_csv(sheet_b, dropped_csv, {k: v for k, v in scores_ok.items() if k != "tr-sme-000"})
    with pytest.raises(ValueError, match="case mismatch vs sealed key"):
        import_blind_sme_annotations(
            rater_a_csv=done_a,
            rater_b_csv=dropped_csv,
            rater_a_id="alice_sme",
            rater_b_id="carol_sme",
            sealed_key_path=sealed_key,
            receipt_out=tmp_path / "r.json",
            min_cases=12,
        )

    with pytest.raises(ValueError, match="Tampered sealed key"):
        import_blind_sme_annotations(
            rater_a_csv=done_a,
            rater_b_csv=done_b,
            rater_a_id="alice_sme",
            rater_b_id="carol_sme",
            sealed_key_path=sealed_key,
            receipt_out=tmp_path / "r.json",
            min_cases=12,
            expected_sealed_key_sha256="0" * 64,
        )

    tampered_key = tmp_path / "tampered_key.json"
    tampered_obj = json.loads(sealed_key.read_text(encoding="utf-8"))
    tampered_obj["cases"]["tr-sme-000"]["judge_ordinal_score"] = 2
    tampered_key.write_text(json.dumps(tampered_obj), encoding="utf-8")
    with pytest.raises(ValueError, match="Tampered sealed key"):
        import_blind_sme_annotations(
            rater_a_csv=done_a,
            rater_b_csv=done_b,
            rater_a_id="alice_sme",
            rater_b_id="carol_sme",
            sealed_key_path=tampered_key,
            receipt_out=tmp_path / "r.json",
            min_cases=12,
            export_manifest_path=pack_dir / "export_manifest.json",
        )


def test_blind_import_receipt_values_and_negative_result(tmp_path: Path) -> None:
    """`T-12`: Calibrates full judged population and persists auditable `UNCALIBRATED` receipts when judges fail."""
    traces_jsonl = tmp_path / "traces_20.jsonl"
    _write_judged_traces_jsonl(traces_jsonl, count=20)
    pack_dir = tmp_path / "pack_20"
    sealed_key = tmp_path / "sealed_20.json"
    export_blind_sme_kit(
        traces_jsonl=traces_jsonl,
        out_dir=pack_dir,
        sealed_key_out=sealed_key,
        min_cases=20,
    )

    agree_scores = {f"tr-sme-{i:03d}": (1 if i >= 10 else 4) for i in range(20)}
    done_a = tmp_path / "agree_a.csv"
    done_b = tmp_path / "agree_b.csv"
    _fill_blind_csv(pack_dir / "rater_a_blind_sheet.csv", done_a, agree_scores)
    _fill_blind_csv(pack_dir / "rater_b_blind_sheet.csv", done_b, agree_scores)

    pos_receipt_path = tmp_path / "pos_receipt.json"
    pos_receipt = import_blind_sme_annotations(
        rater_a_csv=done_a,
        rater_b_csv=done_b,
        rater_a_id="abbott_sme",
        rater_b_id="talbot_risk",
        sealed_key_path=sealed_key,
        receipt_out=pos_receipt_path,
        min_cases=20,
        export_manifest_path=pack_dir / "export_manifest.json",
        population_pass_rate=0.82,
        population_sample_size=250,
    )
    assert pos_receipt_path.is_file()
    assert pos_receipt["calibration_status"] == "CALIBRATED"
    assert pos_receipt["passes_irr_gate"] is True
    assert pos_receipt["population_sample_size"] == 250
    assert pos_receipt["population_pass_rate"] == pytest.approx(0.82)
    assert pos_receipt["calibrated_pass_rate"] == pytest.approx(0.82, abs=1e-6)
    assert pos_receipt["rule0_passing_trace_count"] == 19

    # Invert human labels against judge -> humans agree 100%, judge fails AC2 & cut-point, receipt STILL written!
    disagree_scores = {f"tr-sme-{i:03d}": (4 if i >= 10 else 1) for i in range(20)}
    neg_a = tmp_path / "neg_a.csv"
    neg_b = tmp_path / "neg_b.csv"
    _fill_blind_csv(pack_dir / "rater_a_blind_sheet.csv", neg_a, disagree_scores)
    _fill_blind_csv(pack_dir / "rater_b_blind_sheet.csv", neg_b, disagree_scores)

    neg_receipt_path = tmp_path / "neg_receipt.json"
    rc = blind_kit_main(
        [
            "import",
            "--rater-a-csv",
            str(neg_a),
            "--rater-b-csv",
            str(neg_b),
            "--rater-a-id",
            "abbott_sme",
            "--rater-b-id",
            "talbot_risk",
            "--sealed-key",
            str(sealed_key),
            "--receipt-out",
            str(neg_receipt_path),
            "--min-cases",
            "20",
        ]
    )
    assert rc == 0
    neg_receipt = json.loads(neg_receipt_path.read_text(encoding="utf-8"))
    assert neg_receipt["calibration_status"] == "UNCALIBRATED"
    assert neg_receipt["passes_irr_gate"] is False
    assert neg_receipt["calibrated_pass_rate"] is None
    assert neg_receipt["human_vs_human_ac2"] == pytest.approx(1.0)
    assert neg_receipt["binary_cutpoint_agreement"] == pytest.approx(0.0)
    assert len(neg_receipt["failure_reasons"]) >= 2


def test_blind_kit_merge_updates_and_strict_inputs(tmp_path: Path) -> None:
    """`T-16`: Explicit human allow-list, strict 1..4 integer parsing, full-file deterministic sampling, and `MERGE` upsert."""
    assert validate_human_rater_id("abbott_sme") == "abbott_sme"
    assert validate_human_rater_id("talbot_risk") == "talbot_risk"
    for synthetic_id in ("gemini_bot_1", "llm_rater", "judge-01", "autorater.v2", "a"):
        with pytest.raises(ValueError, match="non-human/synthetic"):
            validate_human_rater_id(synthetic_id)

    traces_a = tmp_path / "order_a.jsonl"
    traces_b = tmp_path / "order_b.jsonl"
    raw_rows = _write_judged_traces_jsonl(traces_a, count=25)
    traces_b.write_text("".join(json.dumps(r) + "\n" for r in reversed(raw_rows)), encoding="utf-8")

    key_1 = tmp_path / "k1.json"
    key_2 = tmp_path / "k2.json"
    export_blind_sme_kit(
        traces_jsonl=traces_a,
        out_dir=tmp_path / "p1",
        sealed_key_out=key_1,
        min_cases=12,
        max_cases=12,
    )
    export_blind_sme_kit(
        traces_jsonl=traces_b,
        out_dir=tmp_path / "p2",
        sealed_key_out=key_2,
        min_cases=12,
        max_cases=12,
    )
    sealed_cases = json.loads(key_1.read_text(encoding="utf-8"))["cases"]
    assert set(sealed_cases) == set(json.loads(key_2.read_text(encoding="utf-8"))["cases"])

    bad_float_csv = tmp_path / "bad_float.csv"
    good_b_csv = tmp_path / "good_b.csv"
    base_scores: dict[str, int] = dict.fromkeys(sealed_cases, 4)
    first_tid = sorted(sealed_cases)[0]
    _fill_blind_csv(
        tmp_path / "p1" / "rater_a_blind_sheet.csv",
        bad_float_csv,
        {**base_scores, first_tid: "3.5"},
    )
    _fill_blind_csv(tmp_path / "p1" / "rater_b_blind_sheet.csv", good_b_csv, base_scores)
    with pytest.raises(ValueError, match="Invalid non-integer ordinal score"):
        import_blind_sme_annotations(
            rater_a_csv=bad_float_csv,
            rater_b_csv=good_b_csv,
            rater_a_id="abbott_sme",
            rater_b_id="talbot_risk",
            sealed_key_path=key_1,
            receipt_out=tmp_path / "bad.json",
            min_cases=12,
        )

    # Ensure rule0_pass=False case is in the merged SQL and execute MERGE twice on OfflineSqlRunner!
    traces_12 = tmp_path / "traces_12.jsonl"
    _write_judged_traces_jsonl(traces_12, count=12)
    key_12 = tmp_path / "k12.json"
    pack_12 = tmp_path / "p12"
    export_blind_sme_kit(
        traces_jsonl=traces_12,
        out_dir=pack_12,
        sealed_key_out=key_12,
        min_cases=12,
    )
    scores_v1 = {f"tr-sme-{i:03d}": (1 if i >= 6 else 4) for i in range(12)}
    done_a1 = tmp_path / "a1.csv"
    done_b1 = tmp_path / "b1.csv"
    _fill_blind_csv(pack_12 / "rater_a_blind_sheet.csv", done_a1, scores_v1)
    _fill_blind_csv(pack_12 / "rater_b_blind_sheet.csv", done_b1, scores_v1)
    sql_v1 = tmp_path / "merge_v1.sql"
    import_blind_sme_annotations(
        rater_a_csv=done_a1,
        rater_b_csv=done_b1,
        rater_a_id="abbott_sme",
        rater_b_id="talbot_risk",
        sealed_key_path=key_12,
        receipt_out=tmp_path / "r1.json",
        bq_sql_out=sql_v1,
        project_id="test-proj",
        dataset_id="agent_eval",
        min_cases=12,
    )

    scores_v2 = {**scores_v1, "tr-sme-000": 2}
    done_a2 = tmp_path / "a2.csv"
    _fill_blind_csv(pack_12 / "rater_a_blind_sheet.csv", done_a2, scores_v2)
    sql_v2 = tmp_path / "merge_v2.sql"
    import_blind_sme_annotations(
        rater_a_csv=done_a2,
        rater_b_csv=done_b1,
        rater_a_id="abbott_sme",
        rater_b_id="talbot_risk",
        sealed_key_path=key_12,
        receipt_out=tmp_path / "r2.json",
        bq_sql_out=sql_v2,
        project_id="test-proj",
        dataset_id="agent_eval",
        min_cases=12,
    )

    with open_runner_for_spans() as runner:
        runner.register_sql(sql_v1.read_text(encoding="utf-8"))
        runner.register_sql(sql_v2.read_text(encoding="utf-8"))
        rows = {
            str(r["trace_id"]): r
            for r in runner.query(
                "SELECT trace_id, rule0_pass, human_ordinal_score, rater_a_score, rater_b_score "
                "FROM `agent_eval.sme_labels`"
            ).rows
        }
    assert len(rows) == 12
    assert int(str(rows["tr-sme-000"]["human_ordinal_score"])) == 2
    assert int(str(rows["tr-sme-000"]["rater_a_score"])) == 2
    assert int(str(rows["tr-sme-000"]["rater_b_score"])) == 4
    assert bool(rows["tr-sme-011"]["rule0_pass"]) is False
    assert bool(rows["tr-sme-000"]["rule0_pass"]) is True


def _assert_shape3_standin_and_endpoint_overrides() -> None:
    shape3_sqls: list[str] = []

    def _shape3_empty_fallback(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        sql_in = str(kwargs.get("input", ""))
        shape3_sqls.append(sql_in)
        if sql_in.startswith("CREATE EXTERNAL TABLE"):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="Cannot query hive partitioned data for table shape3_payloads without any associated files.",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    assert (
        apply_bootstrap_statements(
            "demo-proj",
            [
                (
                    "silver/shape3.sql#1",
                    "CREATE EXTERNAL TABLE `demo-proj.silver_restricted.shape3_payloads`",
                )
            ],
            apply_silver_routine_auth=False,
            which_fn=lambda _: "/usr/bin/bq",
            run_cmd=_shape3_empty_fallback,
        )
        == []
    )
    assert len(shape3_sqls) == 2
    assert (
        "CREATE TABLE IF NOT EXISTS `demo-proj.silver_restricted.shape3_payloads`" in shape3_sqls[1]
    )

    ep_report, ep_stmts = validate_bootstrap_offline(
        mode="gold",
        project_id="demo-proj",
        flash_judge_endpoint="gemini-2.5-flash",
        argon_judge_endpoint="gemini-2.5-pro",
    )
    assert ep_report.passed
    step2_models = [s for lbl, s in ep_stmts if lbl.startswith("core/step2_stateless_eval.sql#")][
        :2
    ]
    assert "endpoint = 'gemini-2.5-flash'" in step2_models[0]
    assert "endpoint = 'gemini-2.5-pro'" in step2_models[1]
    with pytest.raises(ValueError, match="flash_judge_endpoint"):
        validate_bootstrap_offline(mode="gold", flash_judge_endpoint="bad endpoint!")


def test_blind_kit_and_apply_failure_branches() -> None:
    """`T-13` / `T-17`: Exercise CLI/deploy failure branches via injected callables (zero `patch(`) and mutant boundaries."""
    assert format_bq_job_labels({"env": "test"}) == ("--label=env:test", "--label=workload:bq-eval")
    with pytest.raises(ValueError, match="Invalid BigQuery job label"):
        format_bq_job_labels({"BAD KEY!": "val"})

    missing_runner = bq_cli_query_runner("demo-proj", which_fn=lambda _: None)
    with pytest.raises(OSError, match="not found on PATH"):
        missing_runner("SELECT 1")

    benign_runner = bq_cli_query_runner(
        "demo-proj",
        which_fn=lambda _: "/usr/bin/bq",
        run_cmd=lambda *_, **__: subprocess.CompletedProcess(
            args=["bq"],
            returncode=0,
            stdout="Waiting on bqjob_r123 ... Current status: DONE\n",
            stderr="",
        ),
    )
    assert benign_runner("SELECT 1") == []

    bad_json_runner = bq_cli_query_runner(
        "demo-proj",
        which_fn=lambda _: "/usr/bin/bq",
        run_cmd=lambda *_, **__: subprocess.CompletedProcess(
            args=["bq"], returncode=0, stdout="UNEXPECTED_BANNER", stderr=""
        ),
    )
    with pytest.raises(ValueError, match="Unexpected non-JSON"):
        bad_json_runner("SELECT 1")

    assert ensure_observability_traces_link("demo-proj", which_fn=lambda _: None) != []
    link_fail = ensure_observability_traces_link(
        "demo-proj",
        which_fn=lambda _: "/usr/bin/gcloud",
        run_cmd=lambda *_, **__: subprocess.CompletedProcess(
            args=["gcloud"], returncode=1, stdout="", stderr="permission denied"
        ),
    )
    assert any("settings update failed" in e for e in link_fail)

    captured_updates: list[dict[str, object]] = []

    def _fake_auth_cmd(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "show" in cmd:
            meta = {
                "access": [
                    {"role": "OWNER", "userByEmail": "owner@example.invalid"},
                    {
                        "routine": {
                            "projectId": "demo-proj",
                            "datasetId": "silver_open",
                            "routineId": "spans",
                        }
                    },
                ]
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(meta), stderr=""
            )
        src_arg = next(a for a in cmd if a.startswith("--source="))
        src_path = Path(src_arg.split("=", 1)[1])
        captured_updates.append(json.loads(src_path.read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    auth_errs = apply_dataset_routine_authorizations(
        "demo-proj",
        {"obs_traces": [("demo-proj", "silver_open", "spans")]},
        which_fn=lambda _: "/usr/bin/bq",
        run_cmd=_fake_auth_cmd,
    )
    assert auth_errs == []
    assert len(captured_updates[0]["access"]) == 2  # type: ignore[arg-type]

    stop_calls: list[str] = []

    def _fail_first_apply(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stop_calls.append(str(kwargs.get("input", "")))
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="syntax error")

    apply_errs = apply_bootstrap_statements(
        "demo-proj",
        [("stmt#1", "SELECT 1"), ("stmt#2", "SELECT 2")],
        apply_silver_routine_auth=False,
        which_fn=lambda _: "/usr/bin/bq",
        run_cmd=_fail_first_apply,
    )
    assert len(stop_calls) == 1
    assert any("stmt#1" in e for e in apply_errs)
    _assert_shape3_standin_and_endpoint_overrides()

    dry_errs = _run_gcp_dry_run(
        "demo-proj",
        [("ddl#1", "CREATE SCHEMA IF NOT EXISTS `s`"), ("view#1", "SELECT 1")],
        which_fn=lambda _: "/usr/bin/bq",
        run_cmd=lambda cmd, **_: subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="dry-run rejected"
        ),
    )
    assert len(dry_errs) == 1 and "view#1" in dry_errs[0]

    redacted = redact_and_shuffle_traces(
        [
            {
                "trace_id": "raw-tr-1001",
                "outcome_id": "raw-out-1001",
                "context_id": "raw-ctx-1001",
                "user_prompt": "Check kyc-s2trap-99 for raw-tr-1001 context=raw-ctx-1001",
                "retrieved_context": '{"context_id": "raw-ctx-1001"}',
                "final_response": "Outcome raw-out-1001 rejected",
                "judge_ordinal_score": 1,
                "reasoning_trace": "secret",
            }
        ]
    )
    assert "judge_ordinal_score" not in redacted[0]
    assert "reasoning_trace" not in redacted[0]
    assert redacted[0]["trace_id"].startswith("tr-opaque-")
    assert "raw-tr-1001" not in redacted[0]["user_prompt"]
    assert "kyc-s2trap-99" not in redacted[0]["user_prompt"]

    # Mutant boundaries on `otel` token semantics (`input + cache_read > 200K`) and risk-tier gates
    t0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    otel_span = OTelGenAISpanRecord(
        trace_id="tr-otel-sem",
        span_id="sp-otel-sem",
        start_time=t0,
        end_time=t0 + timedelta(seconds=1),
        latency_ms=1000.0,
        model_name="gemini-3.8-flash",
        token_semantics="otel",  # noqa: S106
        input_tokens=50_000,
        cached_input_tokens=160_000,
        output_tokens=1_000,
        serving_traffic_type=ServingTrafficType.PRIORITY,
    )
    assert otel_span.prompt_context_tokens == 210_000
    assert compute_span_burndown_tokens(otel_span) == pytest.approx(
        50_000 * 2.0 + 160_000 * 0.2 + 1_000 * 7.5
    )
    assert compute_span_cost_usd(otel_span) == pytest.approx(
        1.8 * ((50_000 * 0.30 + 160_000 * 0.030 + 1_000 * 2.25) / 1e6)
    )

    rl1_gate = evaluate_calibration_agreement_gate(
        [4] * 10 + [1] * 10, [4] * 10 + [1] * 10, risk_tier="RL-1"
    )
    assert rl1_gate.passes_irr_gate is False
    assert rl1_gate.required_pairs == 100
    with pytest.raises(ValueError, match=">= 100"):
        rogan_gladen_calibrate(
            0.8,
            50,
            ConfusionMatrix(tp=10, fn=0, tn=10, fp=0),
            risk_tier="RL-1",
        )


def test_bootstrap_batching_reduces_spawns_and_pt_committed_cost_guard() -> None:
    """Topological bootstrap batching compacts `silver`/`gold` into 3-5 scripts and `cost.py` guards PT double-counts."""
    _, silver_stmts = validate_bootstrap_offline(mode="silver", project_id="demo-proj")
    _, gold_stmts = validate_bootstrap_offline(mode="gold", project_id="demo-proj")

    silver_batches = batch_bootstrap_statements(silver_stmts)
    gold_batches = batch_bootstrap_statements(gold_stmts)
    assert [name for name, _, _ in silver_batches] == [
        "silver_base",
        "silver_shape3",
        "silver_routines",
    ]
    assert [name for name, _, _ in gold_batches] == [
        "silver_base",
        "silver_shape3",
        "silver_routines",
        "gold_core",
        "gold_summary_and_finops",
    ]
    assert sum(len(lbls) for _, lbls, _ in gold_batches) == len(gold_stmts)
    assert all(len(sql.encode("utf-8")) <= 512_000 for _, _, sql in gold_batches)
    comment_batches = batch_bootstrap_statements(
        [("c#1", "SELECT 1 -- trailing comment"), ("c#2", "SELECT 2")]
    )
    assert len(split_sql_statements_stdlib(comment_batches[0][2])) == 2

    apply_calls: list[str] = []

    def _run_with_empty_shape3(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        sql_in = str(kwargs.get("input", ""))
        apply_calls.append(sql_in)
        if sql_in.startswith("CREATE EXTERNAL TABLE"):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="Cannot query hive partitioned data without any associated files.",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    assert (
        apply_bootstrap_statements(
            "demo-proj",
            gold_stmts,
            mode="gold",
            apply_silver_routine_auth=False,
            which_fn=lambda _: "/usr/bin/bq",
            run_cmd=_run_with_empty_shape3,
        )
        == []
    )
    assert len(apply_calls) == 6
    assert (
        "CREATE TABLE IF NOT EXISTS `demo-proj.silver_restricted.shape3_payloads`" in apply_calls[2]
    )

    fail_fast_calls: list[str] = []

    def _fail_first_batch(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        fail_fast_calls.append(str(kwargs.get("input", "")))
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")

    errs = apply_bootstrap_statements(
        "demo-proj",
        gold_stmts,
        mode="gold",
        apply_silver_routine_auth=False,
        which_fn=lambda _: "/usr/bin/bq",
        run_cmd=_fail_first_batch,
    )
    assert len(fail_fast_calls) == 1
    assert len(errs) == 1 and "silver_base" in errs[0]

    dry_calls: list[str] = []

    def _record_dry_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        dry_calls.append(str(kwargs.get("input", "")))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    dry_errs = _run_gcp_dry_run(
        "demo-proj",
        gold_stmts,
        which_fn=lambda _: "/usr/bin/bq",
        run_cmd=_record_dry_run,
    )
    assert dry_errs == []
    assert 3 <= len(dry_calls) <= 5

    t0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    pt_span = OTelGenAISpanRecord(
        trace_id="tr-pt-guard",
        span_id="sp-pt-guard",
        start_time=t0,
        end_time=t0 + timedelta(seconds=1),
        latency_ms=1000.0,
        model_name="gemini-3.8-flash",
        input_tokens=100_000,
        output_tokens=5_000,
        serving_traffic_type=ServingTrafficType.PROVISIONED_THROUGHPUT,
    )
    paygo_span = pt_span.model_copy(update={"serving_traffic_type": ServingTrafficType.STANDARD})
    assert compute_span_cost_usd(pt_span) > 0.0
    assert compute_span_cost_usd(pt_span, include_committed_list_equivalent=False) == 0.0
    assert compute_span_cost_usd(
        paygo_span, include_committed_list_equivalent=False
    ) == pytest.approx(compute_span_cost_usd(paygo_span), abs=1e-12)
    bad_pt_span = pt_span.model_copy(update={"model_name": "unknown-model-sku"})
    with pytest.raises(ValueError, match="Unknown model_name"):
        compute_span_cost_usd(bad_pt_span, include_committed_list_equivalent=False)
