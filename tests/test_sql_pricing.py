"""Gold-on-Silver SQL pricing matches Python `DEFAULT_MODEL_SKUS`, unpriced guards (`S-6`), and catalog overlap (`S-7`)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from bq_eval.core.contract.otel import GenAISpanKind, OTelGenAISpanRecord
from bq_eval.core.contract.pricing import DEFAULT_MODEL_SKUS
from bq_eval.core.engine.cost import compute_span_cost_usd
from tests.conftest import REPO_ROOT, SQL_DIR, make_span, open_runner_for_spans


def test_pricing_empty_catalog_fallback_suffix_stripping_and_ddl_seed() -> None:
    """`v_trace_waterfall` with an empty catalog prices Argon/suffixed Argon at $0.175 and marks unknown NULL."""
    seed_text = (SQL_DIR / "core" / "pricing_seed.sql").read_text(encoding="utf-8")
    assert "MERGE INTO `agent_eval.model_pricing_catalog`" in seed_text
    for sku in (
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-4-argon",
        "text-embedding-005",
        "ai_evaluate_agent_managed",
    ):
        assert f"'{sku}'" in seed_text

    base_dt = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-argon",
            "sp-1",
            app_id="app-1",
            model_name="gemini-4-argon",
            outcome_id="out-1",
            outcome_owner="app-1",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            base_time=base_dt,
        ),
        make_span(
            "tr-argon-001",
            "sp-2",
            app_id="app-1",
            model_name="gemini-4-argon-001",
            outcome_id="out-2",
            outcome_owner="app-1",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            offset_s=1,
            base_time=base_dt,
        ),
        make_span(
            "tr-unknown",
            "sp-3",
            app_id="app-1",
            model_name="mystery-model-99",
            outcome_id="out-3",
            outcome_owner="app-1",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            offset_s=2,
            base_time=base_dt,
        ),
    ]
    with open_runner_for_spans(spans) as runner:
        runner.load_rows("agent_eval.model_pricing_catalog", (), append=False)
        costs = {
            str(r["trace_id"]): r["span_cost_usd"]
            for r in runner.query(
                "SELECT trace_id, span_cost_usd FROM `agent_eval.v_trace_waterfall`"
            ).rows
        }
        unpriced = runner.query(
            "SELECT unpriced_spans FROM `agent_eval.v_trace_sql_evaluation` "
            "WHERE trace_id = 'tr-unknown'"
        ).rows[0]

    assert float(str(costs["tr-argon"])) == pytest.approx(0.175, abs=1e-12)
    assert float(str(costs["tr-argon-001"])) == pytest.approx(0.175, abs=1e-12)
    assert costs["tr-unknown"] is None
    assert int(str(unpriced["unpriced_spans"])) == 1

    argon_suffixed = OTelGenAISpanRecord(
        trace_id="trace-arg-001",
        span_id="span-arg-001",
        start_time=base_dt,
        end_time=base_dt + timedelta(seconds=1),
        latency_ms=1000.0,
        model_name="gemini-4-argon-001",
        input_tokens=100_000,
        cached_input_tokens=0,
        output_tokens=10_000,
    )
    assert compute_span_cost_usd(argon_suffixed) == pytest.approx(0.175, abs=1e-12)
    unknown_span = argon_suffixed.model_copy(update={"model_name": "mystery-model-99"})
    with pytest.raises(ValueError, match="Unknown model_name"):
        compute_span_cost_usd(unknown_span)


def test_sql_pricing_constants_match_python_default_model_skus() -> None:
    """Parse SQL pricing constants in `pricing_seed.sql` and verify exact parity with `DEFAULT_MODEL_SKUS`."""
    cost_defs = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted((REPO_ROOT / "bq_eval").rglob("*.py"))
        if "def compute_span_cost_usd(" in p.read_text(encoding="utf-8")
    ]
    assert cost_defs == ["bq_eval/core/engine/cost.py"]

    seed_text = (SQL_DIR / "core" / "pricing_seed.sql").read_text(encoding="utf-8")
    merge_section = seed_text.split("MERGE INTO `agent_eval.model_pricing_catalog`", maxsplit=1)[1]
    select_sku_re = re.compile(
        r"SELECT\s+'([^']+)'(?:\s+AS\s+model_name)?,\s*"
        r"'([^']+)'(?:\s+AS\s+provider)?,\s*"
        r"'([^']+)'(?:\s+AS\s+lifecycle_track)?,\s*"
        r"TIMESTAMP\s+'[^']+'(?:\s+AS\s+effective_start)?,\s*"
        r"TIMESTAMP\s+'[^']+'(?:\s+AS\s+effective_end)?,\s*"
        r"([0-9.]+)(?:\s+AS\s+input_usd_per_1m)?,\s*"
        r"([0-9.]+)(?:\s+AS\s+cached_input_usd_per_1m)?,\s*"
        r"([0-9.]+)(?:\s+AS\s+cache_write_5m_usd_per_1m)?,\s*"
        r"([0-9.]+)(?:\s+AS\s+cache_write_1h_usd_per_1m)?,\s*"
        r"([0-9.]+)(?:\s+AS\s+output_usd_per_1m)?,\s*"
        r"(\d+|CAST\(NULL\s+AS\s+INT64\))(?:\s+AS\s+long_context_threshold_tokens)?,\s*"
        r"([0-9.]+)(?:\s+AS\s+long_context_input_usd_per_1m)?,\s*"
        r"([0-9.]+)(?:\s+AS\s+long_context_cached_usd_per_1m)?,\s*"
        r"([0-9.]+)(?:\s+AS\s+long_context_output_usd_per_1m)?",
        re.DOTALL,
    )
    parsed_ddl: dict[
        str, tuple[str, str, float, float, float, float, float, float | None, float, float, float]
    ] = {}
    for m in select_sku_re.finditer(merge_section):
        thresh_raw = m.group(9)
        thresh: float | None = None if "NULL" in thresh_raw else float(thresh_raw)
        parsed_ddl[m.group(1)] = (
            m.group(2),
            m.group(3),
            float(m.group(4)),
            float(m.group(5)),
            float(m.group(6)),
            float(m.group(7)),
            float(m.group(8)),
            thresh,
            float(m.group(10)),
            float(m.group(11)),
            float(m.group(12)),
        )
    assert len(DEFAULT_MODEL_SKUS) == 9
    assert set(parsed_ddl) == set(DEFAULT_MODEL_SKUS)
    for model_id, sku in DEFAULT_MODEL_SKUS.items():
        row = parsed_ddl[model_id]
        assert row[0] == sku.provider
        assert row[1] == sku.lifecycle_track
        assert row[7] == (
            float(sku.long_context_threshold_tokens)
            if sku.long_context_threshold_tokens is not None
            else None
        )
        assert (row[2], row[3], row[4], row[5], row[6], row[8], row[9], row[10]) == pytest.approx(
            (
                sku.input_usd_per_1m,
                sku.cached_input_usd_per_1m,
                sku.effective_cache_write_5m_usd_per_1m,
                sku.effective_cache_write_1h_usd_per_1m,
                sku.output_usd_per_1m,
                sku.long_context_input_usd_per_1m,
                sku.long_context_cached_usd_per_1m,
                sku.long_context_output_usd_per_1m,
            ),
            abs=1e-12,
        )

    base_dt = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    test_spans: list[OTelGenAISpanRecord] = []
    expected_by_tid: dict[str, float] = {}
    for idx, model_id in enumerate(DEFAULT_MODEL_SKUS):
        for cliff_idx, in_tok in enumerate((100_000, 250_000)):
            tid = f"tr-p-{idx}-{cliff_idx}"
            is_emb = model_id == "text-embedding-005"
            py_span = OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-p-{idx}-{cliff_idx}",
                start_time=base_dt + timedelta(seconds=idx * 2 + cliff_idx),
                end_time=base_dt + timedelta(seconds=idx * 2 + cliff_idx + 1),
                latency_ms=1000.0,
                span_kind=GenAISpanKind.EMBEDDINGS if is_emb else GenAISpanKind.CHAT,
                gcp_apphub_application_id="app-p",
                model_name=model_id,
                input_tokens=in_tok,
                cached_input_tokens=0 if is_emb else 20_000,
                output_tokens=0 if is_emb else 5_000,
                thinking_tokens=0 if is_emb else 1_000,
            )
            test_spans.append(py_span)
            expected_by_tid[tid] = compute_span_cost_usd(py_span)

    with open_runner_for_spans(test_spans) as runner:
        wf_costs = {
            str(r["trace_id"]): float(str(r["span_cost_usd"]))
            for r in runner.query(
                "SELECT trace_id, span_cost_usd FROM `agent_eval.v_trace_waterfall` "
                "WHERE model_name != 'none'"
            ).rows
        }
    for tid, py_cost in expected_by_tid.items():
        assert wf_costs[tid] == pytest.approx(py_cost, abs=1e-12)


def test_unpriced_flagged() -> None:
    """Unknown model and token-bearing `none` model spans surface `unpriced_spans` and NULL costs (`S-6`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-none-tok",
            "sp-none-tok",
            app_id="app-unpriced-none",
            model_name="none",
            outcome_id="out-none-tok",
            outcome_owner="app-unpriced-none",
            source_mode="PDF",
            input_tokens=10_000_000,
            cached_input_tokens=0,
            output_tokens=50_000,
            thinking_tokens=0,
            base_time=base,
        ),
        make_span(
            "tr-none-tok",
            "sp-none-close",
            parent_span_id="sp-none-tok",
            span_name="outcome.closed",
            app_id="app-unpriced-none",
            outcome_id="out-none-tok",
            outcome_owner="app-unpriced-none",
            model_name="none",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=0.0,
            offset_ms=500,
            attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
            base_time=base,
        ),
        make_span(
            "tr-unk-sku",
            "sp-unk-sku",
            app_id="app-unpriced-unk",
            model_name="mystery-llm-v99",
            outcome_id="out-unk-sku",
            outcome_owner="app-unpriced-unk",
            source_mode="PDF",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            offset_s=2,
            base_time=base,
        ),
        make_span(
            "tr-zero-ok",
            "sp-zero-ok",
            app_id="app-zero-ok",
            span_kind=GenAISpanKind.EXECUTE_TOOL,
            model_name="none",
            tool_name="lookup",
            tool_call_status="SUCCESS",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=50.0,
            offset_s=4,
            base_time=base,
        ),
    ]
    with open_runner_for_spans(spans) as runner:
        s1_eval_by_app = {
            str(r["apphub_application_id"]): r
            for r in runner.query(
                "SELECT apphub_application_id, unpriced_spans, trace_cost_usd "
                "FROM `agent_eval.v_step1_trace_eval`"
            ).rows
        }
        s1_cpo_by_app = {
            str(r["apphub_application_id"]): r
            for r in runner.query(
                "SELECT apphub_application_id, unpriced_spans, total_cost_usd, "
                "step1_cost_per_outcome_usd FROM `agent_eval.v_step1_cost_per_outcome`"
            ).rows
        }
        s1_out_by_app = {
            str(r["apphub_application_id"]): r
            for r in runner.query(
                "SELECT apphub_application_id, unpriced_spans, measured_model_cost_usd, "
                "total_ai_cost_usd, cost_per_verified_outcome_usd "
                "FROM `agent_eval.v_cost_per_verified_outcome`"
            ).rows
        }
        ent_eval_by_app = {
            str(r["apphub_application_id"]): r
            for r in runner.query(
                "SELECT apphub_application_id, unpriced_spans, production_inference_usd, "
                "trace_span_cost_usd FROM `agent_eval.v_trace_sql_evaluation`"
            ).rows
        }
        ent_out_by_app = {
            str(r["apphub_application_id"]): r
            for r in runner.query(
                "SELECT apphub_application_id, unpriced_spans, measured_model_cost_usd, "
                "total_ai_cost_usd, cost_per_verified_outcome_usd "
                "FROM `agent_eval.v_enterprise_cost_per_verified_outcome`"
            ).rows
        }
        ent_sum_by_app = {
            str(r["apphub_application_id"]): r
            for r in runner.query(
                "SELECT apphub_application_id, unpriced_spans, production_inference_usd, "
                "total_spend_usd, cost_per_verified_outcome_usd "
                "FROM `agent_eval.v_agent_eval_and_finops_summary`"
            ).rows
        }
        for app_id in ("app-unpriced-none", "app-unpriced-unk"):
            s1_eval = s1_eval_by_app[app_id]
            s1_cpo = s1_cpo_by_app[app_id]
            s1_out = s1_out_by_app[app_id]
            ent_eval = ent_eval_by_app[app_id]
            ent_out = ent_out_by_app[app_id]
            ent_sum = ent_sum_by_app[app_id]
            assert int(str(s1_eval["unpriced_spans"])) == 1 and s1_eval["trace_cost_usd"] is None
            assert int(str(s1_cpo["unpriced_spans"])) == 1 and s1_cpo["total_cost_usd"] is None
            assert s1_cpo["step1_cost_per_outcome_usd"] is None
            assert (
                int(str(s1_out["unpriced_spans"])) == 1
                and s1_out["measured_model_cost_usd"] is None
            )
            assert (
                s1_out["total_ai_cost_usd"] is None
                and s1_out["cost_per_verified_outcome_usd"] is None
            )
            assert int(str(ent_eval["unpriced_spans"])) == 1
            assert ent_eval["production_inference_usd"] is None
            assert ent_eval["trace_span_cost_usd"] is None
            assert (
                int(str(ent_out["unpriced_spans"])) == 1
                and ent_out["measured_model_cost_usd"] is None
            )
            assert ent_out["total_ai_cost_usd"] is None
            assert int(str(ent_sum["unpriced_spans"])) == 1
            assert ent_sum["production_inference_usd"] is None
            assert ent_sum["total_spend_usd"] is None
            assert ent_sum["cost_per_verified_outcome_usd"] is None

        zero_ok = ent_sum_by_app["app-zero-ok"]
        assert int(str(zero_ok["unpriced_spans"])) == 0
        assert float(str(zero_ok["total_spend_usd"])) == pytest.approx(0.0, abs=1e-12)


def test_price_row_overlap() -> None:
    """Overlapping effective-dated catalog rows take the latest `effective_start` without duplicating spans (`S-7`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-overlap-1",
            "sp-overlap-1",
            app_id="overlap-app",
            model_name="gemini-3.8-flash",
            outcome_id="out-overlap-1",
            outcome_owner="overlap-app",
            source_mode="PDF",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            base_time=base,
        ),
        make_span(
            "tr-overlap-1",
            "sp-overlap-close",
            parent_span_id="sp-overlap-1",
            span_name="outcome.closed",
            app_id="overlap-app",
            outcome_id="out-overlap-1",
            outcome_owner="overlap-app",
            model_name="none",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=0.0,
            offset_ms=500,
            attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
            base_time=base,
        ),
    ]
    with open_runner_for_spans(spans) as runner:
        runner.load_rows(
            "agent_eval.model_pricing_catalog",
            [
                {
                    "model_name": "gemini-3.8-flash",
                    "provider": "google",
                    "lifecycle_track": "long_term_ga",
                    "effective_start": "2026-11-01T00:00:00+00:00",
                    "effective_end": "2099-12-31T23:59:59+00:00",
                    "input_usd_per_1m": 0.25,
                    "cached_input_usd_per_1m": 0.025,
                    "cache_write_5m_usd_per_1m": 0.25,
                    "cache_write_1h_usd_per_1m": 0.25,
                    "output_usd_per_1m": 1.00,
                    "long_context_threshold_tokens": 200_000,
                    "long_context_input_usd_per_1m": 0.50,
                    "long_context_cached_usd_per_1m": 0.050,
                    "long_context_output_usd_per_1m": 3.75,
                    "burndown_uncached_weight": 1.0,
                    "burndown_cached_weight": 0.1,
                    "burndown_cache_write_5m_weight": 1.0,
                    "burndown_cache_write_1h_weight": 1.0,
                    "burndown_output_weight": 4.0,
                    "burndown_long_uncached_weight": 2.0,
                    "burndown_long_cached_weight": 0.2,
                    "burndown_long_output_weight": 7.5,
                    "priority_paygo_multiplier": 1.8,
                    "flex_paygo_multiplier": 0.5,
                    "min_cache_tokens": 1024,
                }
            ],
            append=True,
        )
        runner.load_rows(
            "agent_eval.eval_results",
            [
                {
                    "eval_id": "ev-overlap-1",
                    "eval_run_id": "run-ov",
                    "trace_id": "tr-overlap-1",
                    "outcome_id": "out-overlap-1",
                    "apphub_application_id": "overlap-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 1_000_000,
                    "judge_output_tokens": 100_000,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T12:05:00+00:00",
                }
            ],
            append=False,
        )

        wf_rows = runner.query(
            "SELECT span_id, span_cost_usd, weighted_burndown_tokens "
            "FROM `agent_eval.v_trace_waterfall` WHERE trace_id = 'tr-overlap-1'"
        ).rows
        sum_row = runner.query(
            "SELECT production_inference_usd, autorater_judge_usd, total_weighted_burndown_tokens "
            "FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'overlap-app'"
        ).rows[0]
        cpvo_row = runner.query(
            "SELECT measured_model_cost_usd, measured_judge_cost_usd "
            "FROM `agent_eval.v_enterprise_cost_per_verified_outcome` "
            "WHERE apphub_application_id = 'overlap-app'"
        ).rows[0]

    assert len(wf_rows) == 1
    assert float(str(wf_rows[0]["span_cost_usd"])) == pytest.approx(0.035, abs=1e-12)
    assert float(str(wf_rows[0]["weighted_burndown_tokens"])) == pytest.approx(140_000.0, abs=1e-12)
    assert float(str(sum_row["production_inference_usd"])) == pytest.approx(0.035, abs=1e-12)
    assert float(str(sum_row["autorater_judge_usd"])) == pytest.approx(0.35, abs=1e-12)
    assert float(str(sum_row["total_weighted_burndown_tokens"])) == pytest.approx(
        140_000.0, abs=1e-12
    )
    assert float(str(cpvo_row["measured_model_cost_usd"])) == pytest.approx(0.035, abs=1e-12)
    assert float(str(cpvo_row["measured_judge_cost_usd"])) == pytest.approx(0.35, abs=1e-12)
