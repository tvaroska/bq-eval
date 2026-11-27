"""FinOps boundaries, Step-3 pairing, StratPPI parity, and multi-app W3C outcomes on Silver + Gold."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import (
    OTelGenAISpanRecord,
    compute_weighted_burndown_tokens,
)
from bq_eval.core.contract.pricing import ModelPricingSKU
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.calibration import (
    rogan_gladen_calibrate,
    rogan_gladen_wilson_ci,
    stratified_rogan_gladen_calibrate,
)
from bq_eval.core.engine.cost import compute_span_cost_usd
from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)
from bq_eval.finops.engine.spend import (
    allocate_focus_two_pool_chargeback,
    diagnose_prefix_cache_dilution,
)
from tests.conftest import REPO_ROOT, eval_traces, make_span, open_runner_for_spans


def test_engine_b_and_cli_managed_metrics_and_judge_model_override(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify --managed-metrics, --run-id, and --judge-model flags on `bq-eval judge-sql` over Silver."""
    sql_b = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        dataset_id="agent_eval",
        managed_metrics=("hallucination_v1", "safety_v1"),
        canary_sample_pct=15,
        run_id="run-m5-01",
    )
    assert "'hallucination_v1'" in sql_b
    assert "'safety_v1'" in sql_b
    assert "'tool_use_quality_v1'" not in sql_b
    assert "v_eval_agent_turns" in sql_b
    assert "WHERE run_id = 'run-m5-01'" in sql_b

    rc_b = cli_main(
        [
            "judge-sql",
            "--engine",
            "evaluate-agent",
            "--project-id",
            "demo-proj",
            "--managed-metrics",
            "hallucination_v1,safety_v1",
            "--run-id",
            "run-m5-01",
        ]
    )
    assert rc_b == 0
    out_b = capsys.readouterr().out
    assert "'hallucination_v1'" in out_b
    assert "run-m5-01" in out_b

    rc_a = cli_main(
        [
            "judge-sql",
            "--project-id",
            "demo-proj",
            "--rubric-id",
            "groundedness_4pt_v1",
            "--judge-model",
            "gemini-4-argon",
        ]
    )
    assert rc_a == 0
    out_a = capsys.readouterr().out
    assert "gemini_4_argon_judge" in out_a
    assert "'gemini-4-argon' AS judge_model" in out_a

    with pytest.raises(ValueError, match="Unsupported judge_model"):
        build_ai_generate_table_judge_sql(project_id="demo-proj", judge_model="unknown-model")


def test_weighted_burndown_and_cost_200k_cliff_boundary() -> None:
    """Exact 200,000 total context stays standard; 200,001 crosses into >200K cliff and dilution."""
    bd_200k = compute_weighted_burndown_tokens(
        input_tokens=200_000, cached_input_tokens=0, output_tokens=0, thinking_tokens=0
    )
    bd_200k_plus1 = compute_weighted_burndown_tokens(
        input_tokens=200_001, cached_input_tokens=0, output_tokens=0, thinking_tokens=0
    )
    assert bd_200k == pytest.approx(200_000.0)
    assert bd_200k_plus1 == pytest.approx(400_002.0)

    sku = ModelPricingSKU(
        model_name="gemini-3.8-flash",
        effective_start=datetime(2026, 1, 1, tzinfo=UTC),
        effective_end=datetime(2099, 12, 31, tzinfo=UTC),
        input_usd_per_1m=0.15,
        cached_input_usd_per_1m=0.015,
        output_usd_per_1m=0.60,
        long_context_input_usd_per_1m=0.30,
        long_context_cached_usd_per_1m=0.030,
        long_context_output_usd_per_1m=2.25,
    )
    std_span = make_span(
        "trace-std-001",
        "span-std-001",
        input_tokens=200_000,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
    )
    cliff_span = make_span(
        "trace-clif-001",
        "span-clif-001",
        input_tokens=200_001,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
    )
    assert compute_span_cost_usd(std_span, sku) == pytest.approx(0.03)
    assert compute_span_cost_usd(cliff_span, sku) == pytest.approx((200_001 / 1e6) * 0.30)

    cached_cliff_span = make_span(
        "trace-clif-002",
        "span-clif-002",
        input_tokens=200_001,
        cached_input_tokens=100_000,
        output_tokens=0,
        thinking_tokens=0,
    )
    assert compute_span_cost_usd(cached_cliff_span, sku) == pytest.approx(
        (100_001 / 1e6) * 0.30 + (100_000 / 1e6) * 0.030
    )

    long_span = make_span(
        "trace-dil-001",
        "span-dil-001",
        input_tokens=1_000_000,
        cached_input_tokens=200_000,
        output_tokens=0,
        thinking_tokens=0,
    )
    dil = diagnose_prefix_cache_dilution(
        [long_span], distinct_prompt_variants=5, target_cache_hit_ratio=0.75
    )
    assert dil.recoverable_weighted_burndown_tokens == pytest.approx(550_000 * 1.8)
    assert dil.recoverable_window_usd == pytest.approx((550_000 / 1e6) * (0.30 - 0.03))


def test_focus_chargeback_payg_overflow_and_removed_legacy_kwarg() -> None:
    """`allocate_focus_two_pool_chargeback` allocates PAYG overflow and rejects removed `reserved_pt_Fixed`."""
    overflow_rows = allocate_focus_two_pool_chargeback(
        app_burndown_tokens={"app-a": 100_000_000.0, "app-b": 50_000_000.0},
        app_cost_centers={"app-a": "CC-A", "app-b": "CC-B"},
        reserved_pt_capacity_tokens=100_000_000.0,
        reserved_pt_fixed_cost_usd=100.0,
        payg_overflow_usd_per_1m_weighted=0.20,
    )
    assert len(overflow_rows) == 2
    assert sum(r.payg_overflow_cost_usd for r in overflow_rows) == pytest.approx(10.0)
    assert sum(r.total_allocated_cost_usd for r in overflow_rows) == pytest.approx(110.0)

    with pytest.raises(TypeError):
        allocate_focus_two_pool_chargeback(  # type: ignore[call-arg]
            app_burndown_tokens={"app-a": 50_000_000.0},
            app_cost_centers={"app-a": "CC-A"},
            reserved_pt_capacity_tokens=100_000_000.0,
            reserved_pt_Fixed_cost_usd=100.0,
        )


def test_step3_paired_run_comparison_temporal_order_and_dedup() -> None:
    """`v_step3_paired_run_comparison` orders `run_9` -> `run_10` temporally and pairs `shadow_of_trace_id`."""
    with open_runner_for_spans() as runner:
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "e-r9-stale",
                    "apphub_application_id": "app-1",
                    "rubric_id": "groundedness_4pt_v1",
                    "eval_run_id": "run_9",
                    "trace_id": "tr-1",
                    "sampling_stratum": "FULL_100PCT",
                    "rule0_pass": True,
                    "ordinal_score": 2,
                    "binary_pass": False,
                    "judge_status": "SCORED",
                    "reasoning_trace": "stale",
                    "judge_model": "gemini-3.8-flash",
                    "judge_input_tokens": 100,
                    "judge_output_tokens": 20,
                    "judge_thinking_tokens": 0,
                    "policy_sha256": "a" * 64,
                    "amended_clause_ids": None,
                    "evaluated_at": "2026-11-10T09:00:00+00:00",
                },
                {
                    "eval_id": "e-r9-good",
                    "apphub_application_id": "app-1",
                    "rubric_id": "groundedness_4pt_v1",
                    "eval_run_id": "run_9",
                    "trace_id": "tr-1",
                    "sampling_stratum": "FULL_100PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "reasoning_trace": "good",
                    "judge_model": "gemini-3.8-flash",
                    "judge_input_tokens": 100,
                    "judge_output_tokens": 20,
                    "judge_thinking_tokens": 0,
                    "policy_sha256": "a" * 64,
                    "amended_clause_ids": None,
                    "evaluated_at": "2026-11-10T10:00:00+00:00",
                },
                {
                    "eval_id": "e-r10-shadow",
                    "apphub_application_id": "app-1",
                    "rubric_id": "groundedness_4pt_v1",
                    "eval_run_id": "run_10",
                    "trace_id": "tr-shadow-1",
                    "shadow_of_trace_id": "tr-1",
                    "sampling_stratum": "FULL_100PCT",
                    "rule0_pass": True,
                    "ordinal_score": 2,
                    "binary_pass": False,
                    "judge_status": "SCORED",
                    "reasoning_trace": "regressed",
                    "judge_model": "gemini-3.8-flash",
                    "judge_input_tokens": 100,
                    "judge_output_tokens": 20,
                    "judge_thinking_tokens": 0,
                    "policy_sha256": "b" * 64,
                    "amended_clause_ids": "c_02",
                    "evaluated_at": "2026-11-11T10:00:00+00:00",
                },
            ],
            append=True,
        )
        rows = runner.query(
            "SELECT baseline_run_id, candidate_run_id, is_negative_flip, is_positive_flip, "
            "policy_changed, amended_clause_ids "
            "FROM `agent_eval.v_step3_paired_run_comparison`"
        ).rows
    assert len(rows) == 1
    row = rows[0]
    assert (
        row["baseline_run_id"],
        row["candidate_run_id"],
        bool(row["is_negative_flip"]),
        bool(row["is_positive_flip"]),
        bool(row["policy_changed"]),
        row["amended_clause_ids"],
    ) == ("run_9", "run_10", True, False, True, "c_02")


def test_stratppi_20_to_1_tail_sampling_sql_python_parity() -> None:
    """`20:1` tail-skewed fixture proves `< 1e-12` Silver+Gold SQL vs Python parity and unbiased `calibrated_pass_rate`."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    judge_outcomes: dict[str, bool] = {}
    sampling_strata: dict[str, str] = {}

    for i in range(1000):
        is_tail = i < 50
        tid = f"tr-strat-{i:04d}"
        latency_ms = 6_000.0 if is_tail else 500.0
        spans.append(
            make_span(
                tid,
                f"sp-strat-{i:04d}",
                app_id="strat-app",
                latency_ms=latency_ms,
                input_tokens=1_000,
                cached_input_tokens=0,
                output_tokens=100,
                thinking_tokens=0,
                offset_s=i,
                base_time=base,
            )
        )
        if is_tail or i < 100:
            stratum = "ERROR_OR_SLA_TAIL" if is_tail else "CANARY_5PCT"
            passed = (i < 10) if is_tail else ((i - 50) < 48)
            judge_outcomes[tid] = passed
            sampling_strata[tid] = stratum

    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    py_strat = stratified_rogan_gladen_calibrate(
        strata=[(50, 50, 10), (950, 50, 48)],
        confusion=cm,
    )
    sql_summary = eval_traces(
        spans=spans,
        judge_outcomes=judge_outcomes,
        sme_confusion=cm,
        sampling_strata=sampling_strata,
        sla_latency_ms=5000.0,
    )["strat-app"]

    assert py_strat.raw_pass_rate == pytest.approx(0.922, abs=1e-12)
    assert sql_summary.raw_judge_pass_rate == pytest.approx(py_strat.raw_pass_rate, abs=1e-12)
    assert sql_summary.calibrated_pass_rate == pytest.approx(
        py_strat.calibrated_pass_rate, abs=1e-12
    )
    assert sql_summary.calibrated_ci_95_lower == pytest.approx(py_strat.ci_95_lower, abs=1e-12)
    assert sql_summary.calibrated_ci_95_upper == pytest.approx(py_strat.ci_95_upper, abs=1e-12)

    perfect_cm = ConfusionMatrix(tp=50, fn=0, tn=20, fp=0)
    wilson_lo, wilson_hi = rogan_gladen_wilson_ci(1.0, 20, perfect_cm)
    assert 0.0 < wilson_lo < wilson_hi <= 1.0
    default_res = rogan_gladen_calibrate(1.0, 20, perfect_cm)
    assert default_res.ci_95_lower == pytest.approx(wilson_lo, abs=1e-12)
    assert default_res.ci_95_upper == pytest.approx(wilson_hi, abs=1e-12)


def test_multi_app_w3c_trace_per_app_sme_and_outcome_cpvo() -> None:
    """Cross-app W3C trace stays 1 trace under root `outcome_owner`, SME keys isolate per app, and CPVO computes."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "trace-cross-001",
            "span-root-001",
            app_id="app-alpha",
            service_name="svc-alpha",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            latency_ms=600.0,
            base_time=base,
        ),
        make_span(
            "trace-cross-001",
            "span-child-001",
            parent_span_id="span-root-001",
            app_id="app-downstream",
            service_name="svc-downstream",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            latency_ms=300.0,
            offset_ms=100,
            base_time=base,
        ),
        make_span(
            "trace-cross-001",
            "span-close-001",
            parent_span_id="span-root-001",
            span_name="outcome.closed",
            app_id="app-alpha",
            service_name="svc-alpha",
            outcome_id="out-alpha-1",
            outcome_owner="app-alpha",
            model_name="none",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=0.0,
            offset_ms=600,
            attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
            base_time=base,
        ),
        make_span("trace-beta-001", "span-beta-001", app_id="app-beta", offset_s=5, base_time=base),
    ]
    summaries = eval_traces(
        spans=spans,
        judge_outcomes={"trace-cross-001": True, "trace-beta-001": True},
        sme_confusion={"app-alpha": ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)},
        sme_ordinal_pairs={"app-alpha": [(4, 4), (4, 3), (3, 3), (2, 2), (1, 1)] * 2},
        judge_cost_per_call_usd=0.0025,
    )
    assert set(summaries) == {"app-alpha", "app-beta"}
    alpha = summaries["app-alpha"]
    beta = summaries["app-beta"]
    assert alpha.total_traces == 1
    assert alpha.judged_samples == 1
    assert alpha.calibration_status == "UNCALIBRATED"
    assert alpha.calibrated_pass_rate is None
    assert alpha.gwet_ac2 is not None
    assert alpha.passes_irr_gate is True
    assert alpha.outcome_total_count == 1
    assert alpha.outcome_verified_count == 1
    expected_span_cost = 2 * ((100_000 / 1e6) * 0.15 + (10_000 / 1e6) * 0.60)
    expected_infra_cost = (0.6 + 0.3) * (2.0 * 0.00002400 + 2.0 * 0.00000250) + 0.000020
    assert alpha.outcome_cost_per_verified_ai_usd == pytest.approx(
        expected_span_cost + 0.0025, abs=1e-12
    )
    assert alpha.outcome_cost_per_verified_usd == pytest.approx(
        expected_span_cost + 0.0025 + expected_infra_cost, abs=1e-12
    )
    assert beta.calibration_status == "UNCALIBRATED"
    assert beta.gwet_ac2 is None
    assert beta.calibrated_pass_rate is None


def test_rfp_quote_accuracy_migration_hint() -> None:
    """Removed legacy rubric name raises an explicit migration hint."""
    with pytest.raises(ValueError, match="rfp_quote_completeness_4pt_v1"):
        build_ai_generate_table_judge_sql(
            project_id="demo-proj",
            rubric_id="rfp_quote_accuracy_4pt_v1",
        )


def test_policy_sha256_and_amended_clause_ids_in_judge_sql_and_ddl() -> None:
    """Policy digest and amended clause IDs wire through DDL, Step-3, and judge SQL."""
    digest = "a" * 64
    sql = build_ai_generate_table_judge_sql(
        project_id="demo-proj",
        policy_sha256=digest,
        amended_clause_ids=("c_03", "c_07"),
    )
    assert f"'{digest}' AS policy_sha256" in sql
    assert "'c_03,c_07' AS amended_clause_ids" in sql
    with pytest.raises(ValueError, match="policy_sha256"):
        build_ai_generate_table_judge_sql(project_id="demo-proj", policy_sha256="not-a-sha")

    step3 = (REPO_ROOT / "sql" / "core" / "step3_saved_results.sql").read_text(encoding="utf-8")
    assert "policy_sha256 STRING" in step3 and "amended_clause_ids STRING" in step3
    assert "policy_changed" in step3


def test_public_doc_hygiene_marks_evaluate_agent_preview() -> None:
    """All files citing `AI.EVALUATE_AGENT` explicitly note preview/non-public status."""
    for md_path in sorted(REPO_ROOT.rglob("*.md")):
        if ".venv" in md_path.parts or md_path.name in {"roadmap.md", "roadmap-history.md"}:
            continue
        lower = md_path.read_text(encoding="utf-8").lower()
        if "ai.evaluate_agent" in lower:
            assert "preview" in lower or "non-public" in lower, (
                f"{md_path.relative_to(REPO_ROOT)} cites AI.EVALUATE_AGENT without preview note"
            )
