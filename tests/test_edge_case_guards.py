"""Regression guards for pricing/input edge cases (`A-10`, `A-20`), CLI errors, and receipt verification."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.engine.cost import compute_span_burndown_tokens, compute_span_cost_usd
from bq_eval.demo.bundles import verify_receipt_sha256
from bq_eval.finops.engine.spend import allocate_focus_hourly_chargeback
from tests.conftest import REPO_ROOT, eval_traces, make_span, open_runner_for_spans


def test_1p_long_context_cache_write_cliff_pricing_parity() -> None:
    """1P `>200K` cache-write tokens price at cliff tier and 3P stays flat across Python and Silver+Gold SQL."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    span_1p = OTelGenAISpanRecord(
        trace_id="tr-cw-1p",
        span_id="sp-cw-1p",
        start_time=base,
        end_time=base + timedelta(seconds=1),
        latency_ms=1000.0,
        gcp_apphub_application_id="cw-1p-app",
        model_name="gemini-3.8-flash",
        input_tokens=250_000,
        cached_input_tokens=50_000,
        cache_write_5m_tokens=40_000,
        cache_write_1h_tokens=10_000,
        output_tokens=1_000,
        thinking_tokens=0,
    )
    span_3p = span_1p.model_copy(
        update={
            "trace_id": "tr-cw-3p",
            "span_id": "sp-cw-3p",
            "gcp_apphub_application_id": "cw-3p-app",
            "model_name": "claude-sonnet-4-6",
        }
    )

    assert compute_span_burndown_tokens(span_1p) == pytest.approx(417_500.0, abs=1e-12)
    assert compute_span_cost_usd(span_1p) == pytest.approx(0.06375, abs=1e-12)
    assert compute_span_burndown_tokens(span_3p) == pytest.approx(230_000.0, abs=1e-12)
    assert compute_span_cost_usd(span_3p) == pytest.approx(0.690, abs=1e-12)

    twin = eval_traces(spans=[span_1p, span_3p])
    assert twin["cw-1p-app"].total_weighted_burndown_tokens == pytest.approx(417_500.0, abs=1e-12)
    assert twin["cw-1p-app"].production_inference_usd == pytest.approx(0.06375, abs=1e-12)
    assert twin["cw-3p-app"].total_weighted_burndown_tokens == pytest.approx(230_000.0, abs=1e-12)
    assert twin["cw-3p-app"].production_inference_usd == pytest.approx(0.690, abs=1e-12)

    with open_runner_for_spans([span_1p, span_3p]) as runner:
        rows = {
            str(r["trace_id"]): r
            for r in runner.query(
                "SELECT trace_id, weighted_burndown_tokens, span_cost_usd "
                "FROM `agent_eval.v_trace_waterfall` WHERE model_name != 'none'"
            ).rows
        }
    assert float(str(rows["tr-cw-1p"]["weighted_burndown_tokens"])) == pytest.approx(
        417_500.0, abs=1e-12
    )
    assert float(str(rows["tr-cw-1p"]["span_cost_usd"])) == pytest.approx(0.06375, abs=1e-12)
    assert float(str(rows["tr-cw-3p"]["weighted_burndown_tokens"])) == pytest.approx(
        230_000.0, abs=1e-12
    )
    assert float(str(rows["tr-cw-3p"]["span_cost_usd"])) == pytest.approx(0.690, abs=1e-12)


def test_model_none_with_tokens_and_negative_span_duration_fail_closed() -> None:
    """`A-10` / `A-20`: Token-bearing `none` raises `ValueError`; `end_time < start_time` raises `ValidationError`."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    zero_span = make_span(
        "tr-none-0",
        "sp-none-0",
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        base_time=base,
    )
    assert compute_span_cost_usd(zero_span) == 0.0

    with pytest.raises(ValueError, match="Cannot price non-zero tokens"):
        compute_span_cost_usd(zero_span.model_copy(update={"input_tokens": 1_000}))
    with pytest.raises(ValueError, match="Cannot price non-zero tokens"):
        compute_span_cost_usd(
            zero_span.model_copy(update={"input_tokens": 500, "cache_write_5m_tokens": 500})
        )
    with pytest.raises(ValueError, match="Cannot price non-zero tokens"):
        compute_span_cost_usd(zero_span.model_copy(update={"model_name": "", "output_tokens": 10}))

    with pytest.raises(ValidationError, match="end_time"):
        OTelGenAISpanRecord(
            trace_id="tr-neg-dur",
            span_id="sp-neg-dur",
            start_time=base,
            end_time=base - timedelta(milliseconds=10),
            latency_ms=10.0,
        )

    rows = allocate_focus_hourly_chargeback(
        hourly_burndown_by_app={"app-a": [2_000_000.0]},
        app_cost_centers={"app-a": "CC-A"},
        hourly_reserved_pt_capacity_tokens=1_000_000.0,
        reserved_pt_fixed_cost_usd=10.0,
    )
    by_app = {r.apphub_application_id: r for r in rows}
    assert by_app["app-a"].payg_overflow_cost_usd == pytest.approx(0.15, abs=1e-12)


def test_unjudged_rubric_id_preserved_and_negative_infra_attributes_guarded() -> None:
    """Unjudged summaries honor `rubric_id`, and negative sandbox/storage attributes fall back cleanly."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    root = make_span(
        "tr-neg-infra",
        "sp-neg-root",
        app_id="neg-infra-app",
        outcome_id="out-neg-1",
        outcome_owner="neg-infra-app",
        input_tokens=10_000,
        cached_input_tokens=0,
        output_tokens=1_000,
        thinking_tokens=0,
        latency_ms=1000.0,
        attributes={
            "bq_eval.sandbox.cpu_sec": "-4.0",
            "bq_eval.sandbox.mem_gb_sec": "-2.0",
            "outcome.storage_usd": "-0.50",
        },
        base_time=base,
    )
    close = make_span(
        "tr-neg-infra",
        "sp-neg-close",
        parent_span_id="sp-neg-root",
        span_name="outcome.closed",
        app_id="neg-infra-app",
        outcome_id="out-neg-1",
        outcome_owner="neg-infra-app",
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        latency_ms=0.0,
        offset_s=1,
        attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
        base_time=base,
    )

    twin = eval_traces(
        spans=[root, close],
        rubric_id="completeness_4pt_v1",
    )["neg-infra-app"]
    assert twin.calibration_status == "UNJUDGED"
    assert twin.rubric_id == "completeness_4pt_v1"

    expected_infra = 0.000073
    expected_ai = compute_span_cost_usd(root)
    assert twin.outcome_cost_per_verified_ai_usd == pytest.approx(expected_ai, abs=1e-12)
    assert twin.outcome_cost_per_verified_usd == pytest.approx(
        expected_ai + expected_infra, abs=1e-12
    )

    with open_runner_for_spans([root, close]) as runner:
        inf_row = runner.query(
            "SELECT estimated_compute_usd, estimated_storage_usd, estimated_infra_cost_usd "
            "FROM `agent_eval.v_infra_cost_estimate` WHERE outcome_id = 'out-neg-1'"
        ).rows[0]
    assert float(str(inf_row["estimated_compute_usd"])) == pytest.approx(0.000053, abs=1e-12)
    assert float(str(inf_row["estimated_storage_usd"])) == pytest.approx(0.000020, abs=1e-12)
    assert float(str(inf_row["estimated_infra_cost_usd"])) == pytest.approx(
        expected_infra, abs=1e-12
    )


def test_verify_receipt_sha256_fails_closed_on_missing_manifest_or_entry(tmp_path: Path) -> None:
    """`verify_receipt_sha256` fails closed when `SHA256SUMS` is missing or omits the receipt."""
    receipt = tmp_path / "custom_receipt.json"
    receipt.write_text('{"ok": true}\n', encoding="utf-8")
    missing_manifest = tmp_path / "SHA256SUMS"

    with pytest.raises(ValueError, match="SHA-256 manifest file not found"):
        verify_receipt_sha256(receipt, missing_manifest)

    manifest_without_entry = tmp_path / "SHA256SUMS"
    manifest_without_entry.write_text(f"{'a' * 64}  other_file.json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"has no entry for custom_receipt\.json"):
        verify_receipt_sha256(receipt, manifest_without_entry)


def test_zero_span_scenario_file_exits_2_not_stop_iteration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A zero-span `--scenario-file` exits cleanly with status 2 instead of `StopIteration`."""
    empty_bundle = tmp_path / "empty_bundle.json"
    empty_bundle.write_text(
        json.dumps({"corpus_id": "empty-probe", "title": "Empty", "spans": []}),
        encoding="utf-8",
    )
    rc = cli_main(["demo", "--scenario-file", str(empty_bundle), "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "zero spans" in captured.err


def test_every_committed_receipt_has_manifest_entry() -> None:
    """Every `.json`/`.jsonl` receipt under `benchmarks/results/` matches `SHA256SUMS`."""
    res_dir = REPO_ROOT / "benchmarks" / "results"
    sha_path = res_dir / "SHA256SUMS"
    actual_files = {
        p.name for p in res_dir.iterdir() if p.is_file() and p.suffix in {".json", ".jsonl"}
    }
    assert actual_files == {
        "bq_dry_run_receipt.json",
        "m2_live_receipt.json",
        "m16_live_cloud_proof.json",
        "m16_anonymized_traces_100.jsonl",
        "m20_silver_gold_live_receipt.json",
    }

    manifest: dict[str, str] = {}
    for raw_line in sha_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        digest, filename = raw_line.split(None, 1)
        clean_name = filename.strip()
        manifest[clean_name] = digest.strip()
        verify_receipt_sha256(res_dir / clean_name, sha_path)

    assert set(manifest) == actual_files
    proof = json.loads((res_dir / "m16_live_cloud_proof.json").read_text(encoding="utf-8"))
    export_info = proof["apphub_fleet_export"]
    assert export_info["traces_sha256"] == manifest["m16_anonymized_traces_100.jsonl"]
    assert len(export_info["raw_traces_sha256"]) == 64
    assert export_info["raw_traces_sha256"] != export_info["traces_sha256"]
