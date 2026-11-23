"""SQL pricing matches the Python price list, including empty-catalog fallbacks."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from bq_eval.core.contract.otel import (
    OTelGenAISpanRecord,
)
from bq_eval.core.contract.pricing import DEFAULT_MODEL_SKUS
from bq_eval.core.engine.cost import compute_span_cost_usd
from tests.sql_helpers import (
    ENTERPRISE_OTEL_SPANS_BASE_INSERT,
    REPO_ROOT,
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
)


def test_pricing_empty_catalog_fallback_suffix_stripping_and_ddl_seed() -> None:
    """`v_trace_waterfall` with an empty catalog prices Argon/suffixed Argon at $0.175 and marks unknown NULL."""
    ddl_text = (SQL_DIR / "core" / "ddl.sql").read_text(encoding="utf-8")
    assert "MERGE INTO `agent_eval.model_pricing_catalog`" in ddl_text
    for sku in (
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-4-argon",
        "ai_evaluate_agent_managed",
    ):
        assert f"'{sku}'" in ddl_text

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    conn.executemany(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        [
            (
                "tr-argon",
                "sp-1",
                None,
                "chat",
                "app-1",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "chat",
                "gemini-4-argon",
                None,
                None,
                "OK",
                1,
                "production_inference",
                "out-1",
                "app-1",
                None,
                None,
                None,
                None,
                t0,
                t1,
                t1,
                1000.0,
                100_000,
                0,
                10_000,
                0,
                "{}",
            ),
            (
                "tr-argon-001",
                "sp-2",
                None,
                "chat",
                "app-1",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "chat",
                "gemini-4-argon-001",
                None,
                None,
                "OK",
                1,
                "production_inference",
                "out-2",
                "app-1",
                None,
                None,
                None,
                None,
                t0,
                t1,
                t1,
                1000.0,
                100_000,
                0,
                10_000,
                0,
                "{}",
            ),
            (
                "tr-unknown",
                "sp-3",
                None,
                "chat",
                "app-1",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "chat",
                "mystery-model-99",
                None,
                None,
                "OK",
                1,
                "production_inference",
                "out-3",
                "app-1",
                None,
                None,
                None,
                None,
                t0,
                t1,
                t1,
                1000.0,
                100_000,
                0,
                10_000,
                0,
                "{}",
            ),
        ],
    )
    costs = {
        str(row["trace_id"]): row["span_cost_usd"]
        for row in conn.execute("SELECT trace_id, span_cost_usd FROM v_trace_waterfall").fetchall()
    }
    assert float(costs["tr-argon"]) == pytest.approx(0.175, abs=1e-12)
    assert float(costs["tr-argon-001"]) == pytest.approx(0.175, abs=1e-12)
    assert costs["tr-unknown"] is None
    unpriced = conn.execute(
        "SELECT unpriced_spans FROM v_trace_sql_evaluation WHERE trace_id = 'tr-unknown'"
    ).fetchone()
    assert int(unpriced["unpriced_spans"]) == 1
    conn.close()

    base_dt = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
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
    """Parse SQL pricing constants across `sql/*.sql` and verify single Python cost implementation."""
    cost_defs = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted((REPO_ROOT / "bq_eval").rglob("*.py"))
        if "def compute_span_cost_usd(" in p.read_text(encoding="utf-8")
    ]
    assert cost_defs == ["bq_eval/core/engine/cost.py"]

    ddl_text = (SQL_DIR / "core" / "ddl.sql").read_text(encoding="utf-8")
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
    for m in select_sku_re.finditer(ddl_text):
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

    views_text = (SQL_DIR / "core" / "views.sql").read_text(encoding="utf-8")
    assert "CASE REGEXP_REPLACE(" not in views_text, (
        "sql/core/views.sql must join v_default_model_pricing instead of duplicating CASE pricing ladders"
    )

    default_pricing_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(
            SQL_DIR / "core" / "step1_trace_views.sql", "agent_eval.v_default_model_pricing"
        )
    )
    step1_wf = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(
            SQL_DIR / "core" / "step1_trace_views.sql", "agent_eval.v_step1_trace_waterfall"
        )
    )
    ent_wf = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(SQL_DIR / "core" / "views.sql", "agent_eval.v_trace_waterfall")
    )
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    conn.execute(f"CREATE VIEW v_default_model_pricing AS {default_pricing_sql}")
    conn.execute(f"CREATE VIEW v_step1_wf AS {step1_wf}")
    conn.execute(f"CREATE VIEW v_ent_wf AS {ent_wf}")
    base_dt = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    for idx, model_id in enumerate(DEFAULT_MODEL_SKUS):
        for cliff_idx, in_tok in enumerate((100_000, 250_000)):
            tid = f"tr-p-{idx}-{cliff_idx}"
            py_span = OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-p-{idx}-{cliff_idx}",
                start_time=base_dt,
                end_time=base_dt + timedelta(seconds=1),
                latency_ms=1000.0,
                model_name=model_id,
                input_tokens=in_tok,
                cached_input_tokens=20_000,
                output_tokens=5_000,
                thinking_tokens=1_000,
            )
            conn.execute(
                ENTERPRISE_OTEL_SPANS_BASE_INSERT,
                (
                    tid,
                    py_span.span_id,
                    None,
                    "chat",
                    "app-p",
                    "svc",
                    "agent",
                    "SPAN_KIND_SERVER",
                    "chat",
                    model_id,
                    None,
                    None,
                    "OK",
                    1,
                    "production_inference",
                    None,
                    "app-p",
                    None,
                    None,
                    None,
                    None,
                    base_dt.isoformat(),
                    (base_dt + timedelta(seconds=1)).isoformat(),
                    (base_dt + timedelta(seconds=1)).isoformat(),
                    1000.0,
                    in_tok,
                    20_000,
                    5_000,
                    1_000,
                    "{}",
                ),
            )
            py_cost = compute_span_cost_usd(py_span)
            s1_cost = conn.execute(
                "SELECT span_cost_usd FROM v_step1_wf WHERE trace_id = ?", (tid,)
            ).fetchone()[0]
            ent_cost = conn.execute(
                "SELECT span_cost_usd FROM v_ent_wf WHERE trace_id = ?", (tid,)
            ).fetchone()[0]
            assert float(s1_cost) == pytest.approx(py_cost, abs=1e-12)
            assert float(ent_cost) == pytest.approx(py_cost, abs=1e-12)
    conn.close()


def test_unpriced_flagged() -> None:
    """Unknown model and token-bearing `none`/NULL model spans surface `unpriced_spans` and NULL costs (S-6)."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    step1_path = SQL_DIR / "core" / "step1_trace_views.sql"
    for view_name in (
        "agent_eval.v_step1_cost_per_outcome",
        "agent_eval.v_cost_per_verified_outcome",
    ):
        short_name = view_name.split(".", maxsplit=1)[1]
        conn.execute(
            f"CREATE VIEW {short_name} AS "
            f"{_transpile_bq_view_to_sqlite(_extract_view_sql_text(step1_path, view_name))}"
        )

    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    conn.executemany(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        [
            # App 1: 10M-token span with model_name='none' -> MUST be NULL (not $0.0) and flagged
            (
                "tr-none-tok",
                "sp-none-tok",
                None,
                "chat",
                "app-unpriced-none",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "chat",
                "none",
                None,
                None,
                "OK",
                1,
                "production_inference",
                "out-none-tok",
                "app-unpriced-none",
                "PDF",
                None,
                None,
                None,
                t0,
                t1,
                t1,
                1000.0,
                10_000_000,
                0,
                50_000,
                0,
                "{}",
            ),
            (
                "tr-none-tok",
                "sp-none-close",
                "sp-none-tok",
                "outcome.closed",
                "app-unpriced-none",
                "svc",
                "agent",
                "SPAN_KIND_INTERNAL",
                "invoke_agent",
                "none",
                None,
                None,
                "OK",
                1,
                "production_inference",
                "out-none-tok",
                "app-unpriced-none",
                "PDF",
                None,
                None,
                None,
                t1,
                t1,
                t1,
                0.0,
                0,
                0,
                0,
                0,
                '{"outcome.status": "succeeded", "outcome.verified": "true"}',
            ),
            # App 2: unknown model name with tokens -> NULL and flagged
            (
                "tr-unk-sku",
                "sp-unk-sku",
                None,
                "chat",
                "app-unpriced-unk",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "chat",
                "mystery-llm-v99",
                None,
                None,
                "OK",
                1,
                "production_inference",
                "out-unk-sku",
                "app-unpriced-unk",
                "PDF",
                None,
                None,
                None,
                t0,
                t1,
                t1,
                1000.0,
                100_000,
                0,
                10_000,
                0,
                "{}",
            ),
            # App 3: genuine 0-token tool span with model_name='none' -> priced at $0.0, unpriced_spans=0
            (
                "tr-zero-ok",
                "sp-zero-ok",
                None,
                "execute_tool",
                "app-zero-ok",
                "svc",
                "agent",
                "SPAN_KIND_INTERNAL",
                "execute_tool",
                "none",
                "lookup",
                "SUCCESS",
                "OK",
                1,
                "production_inference",
                None,
                "app-zero-ok",
                None,
                None,
                None,
                None,
                t0,
                t1,
                t1,
                50.0,
                0,
                0,
                0,
                0,
                "{}",
            ),
        ],
    )

    for app_id in ("app-unpriced-none", "app-unpriced-unk"):
        s1_eval = conn.execute(
            "SELECT unpriced_spans, trace_cost_usd FROM v_step1_trace_eval WHERE apphub_application_id = ?",
            (app_id,),
        ).fetchone()
        s1_cpo = conn.execute(
            "SELECT unpriced_spans, total_cost_usd, step1_cost_per_outcome_usd FROM v_step1_cost_per_outcome WHERE apphub_application_id = ?",
            (app_id,),
        ).fetchone()
        s1_out = conn.execute(
            "SELECT unpriced_spans, measured_model_cost_usd, total_ai_cost_usd, cost_per_verified_outcome_usd FROM v_cost_per_verified_outcome WHERE apphub_application_id = ?",
            (app_id,),
        ).fetchone()
        ent_eval = conn.execute(
            "SELECT unpriced_spans, production_inference_usd, trace_span_cost_usd FROM v_trace_sql_evaluation WHERE apphub_application_id = ?",
            (app_id,),
        ).fetchone()
        ent_out = conn.execute(
            "SELECT unpriced_spans, measured_model_cost_usd, total_ai_cost_usd, cost_per_verified_outcome_usd FROM v_enterprise_cost_per_verified_outcome WHERE apphub_application_id = ?",
            (app_id,),
        ).fetchone()
        ent_sum = conn.execute(
            "SELECT unpriced_spans, production_inference_usd, total_spend_usd, cost_per_verified_outcome_usd FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = ?",
            (app_id,),
        ).fetchone()
        assert int(s1_eval["unpriced_spans"]) == 1 and s1_eval["trace_cost_usd"] is None
        assert int(s1_cpo["unpriced_spans"]) == 1 and s1_cpo["total_cost_usd"] is None
        assert s1_cpo["step1_cost_per_outcome_usd"] is None
        assert int(s1_out["unpriced_spans"]) == 1 and s1_out["measured_model_cost_usd"] is None
        assert (
            s1_out["total_ai_cost_usd"] is None and s1_out["cost_per_verified_outcome_usd"] is None
        )
        assert int(ent_eval["unpriced_spans"]) == 1
        assert ent_eval["production_inference_usd"] is None
        assert ent_eval["trace_span_cost_usd"] is None
        assert int(ent_out["unpriced_spans"]) == 1 and ent_out["measured_model_cost_usd"] is None
        assert ent_out["total_ai_cost_usd"] is None
        assert int(ent_sum["unpriced_spans"]) == 1
        assert ent_sum["production_inference_usd"] is None
        assert ent_sum["total_spend_usd"] is None
        assert ent_sum["cost_per_verified_outcome_usd"] is None

    zero_ok = conn.execute(
        "SELECT unpriced_spans, total_spend_usd FROM v_agent_eval_and_finops_summary "
        "WHERE apphub_application_id = 'app-zero-ok'"
    ).fetchone()
    conn.close()
    assert int(zero_ok["unpriced_spans"]) == 0
    assert float(zero_ok["total_spend_usd"]) == pytest.approx(0.0, abs=1e-12)


def test_price_row_overlap() -> None:
    """Overlapping effective-dated catalog rows take the latest `effective_start` without duplicating spans or judge runs (S-7)."""
    from tests.sql_helpers import ENTERPRISE_EVAL_RESULTS_BASE_INSERT

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    # Seed two overlapping catalog rows for gemini-3.8-flash:
    # Old seed (2026-01-01..2099-12-31): $0.15 in / $0.60 out
    # New price update (2026-11-01..2099-12-31): $0.25 in / $1.00 out
    conn.executemany(
        """
        INSERT INTO model_pricing_catalog (
            model_name, provider, lifecycle_track, effective_start, effective_end,
            input_usd_per_1m, cached_input_usd_per_1m, cache_write_5m_usd_per_1m,
            cache_write_1h_usd_per_1m, output_usd_per_1m, long_context_threshold_tokens,
            long_context_input_usd_per_1m, long_context_cached_usd_per_1m,
            long_context_output_usd_per_1m, burndown_uncached_weight,
            burndown_cached_weight, burndown_cache_write_5m_weight,
            burndown_cache_write_1h_weight, burndown_output_weight,
            burndown_long_uncached_weight, burndown_long_cached_weight,
            burndown_long_output_weight, priority_paygo_multiplier, flex_paygo_multiplier
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "gemini-3.8-flash",
                "google",
                "long_term_ga",
                "2026-01-01T00:00:00+00:00",
                "2099-12-31T23:59:59+00:00",
                0.15,
                0.015,
                0.15,
                0.15,
                0.60,
                200000,
                0.30,
                0.030,
                2.25,
                1.0,
                0.1,
                1.0,
                1.0,
                4.0,
                2.0,
                0.2,
                7.5,
                1.8,
                0.5,
            ),
            (
                "gemini-3.8-flash",
                "google",
                "long_term_ga",
                "2026-11-01T00:00:00+00:00",
                "2099-12-31T23:59:59+00:00",
                0.25,
                0.025,
                0.25,
                0.25,
                1.00,
                200000,
                0.50,
                0.050,
                3.75,
                1.0,
                0.1,
                1.0,
                1.0,
                4.0,
                2.0,
                0.2,
                7.5,
                1.8,
                0.5,
            ),
        ],
    )
    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    conn.executemany(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        [
            (
                "tr-overlap-1",
                "sp-overlap-1",
                None,
                "chat",
                "overlap-app",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "chat",
                "gemini-3.8-flash",
                None,
                None,
                "OK",
                1,
                "production_inference",
                "out-overlap-1",
                "overlap-app",
                "PDF",
                None,
                None,
                None,
                t0,
                t1,
                t1,
                1000.0,
                100_000,
                0,
                10_000,
                0,
                "{}",
            ),
            (
                "tr-overlap-1",
                "sp-overlap-close",
                "sp-overlap-1",
                "outcome.closed",
                "overlap-app",
                "svc",
                "agent",
                "SPAN_KIND_INTERNAL",
                "invoke_agent",
                "none",
                None,
                None,
                "OK",
                1,
                "production_inference",
                "out-overlap-1",
                "overlap-app",
                "PDF",
                None,
                None,
                None,
                t1,
                t1,
                t1,
                0.0,
                0,
                0,
                0,
                0,
                '{"outcome.status": "succeeded", "outcome.verified": "true"}',
            ),
        ],
    )
    conn.execute(
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        (
            "ev-overlap-1",
            "run-ov",
            "tr-overlap-1",
            "out-overlap-1",
            "overlap-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            "CANARY_5PCT",
            1,
            "ok",
            4,
            1,
            1_000_000,
            100_000,
            0,
            "2026-11-21T12:05:00+00:00",
        ),
    )

    wf_rows = conn.execute(
        "SELECT span_id, span_cost_usd, weighted_burndown_tokens FROM v_trace_waterfall WHERE trace_id = 'tr-overlap-1'"
    ).fetchall()
    sum_row = conn.execute(
        "SELECT production_inference_usd, autorater_judge_usd, total_weighted_burndown_tokens "
        "FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'overlap-app'"
    ).fetchone()
    cpvo_row = conn.execute(
        "SELECT measured_model_cost_usd, measured_judge_cost_usd "
        "FROM v_enterprise_cost_per_verified_outcome WHERE apphub_application_id = 'overlap-app'"
    ).fetchone()
    conn.close()

    # Latest row ($0.25/1M in + $1.00/1M out):
    #   span (100K in, 10K out, <=200K): $0.025 + $0.010 = $0.035 (NOT old $0.021 or doubled $0.056)
    #   judge (1M in, 100K out): $0.25 + $0.10 = $0.35 (NOT old $0.21 or doubled $0.56)
    assert len(wf_rows) == 2
    by_span = {str(r["span_id"]): r for r in wf_rows}
    assert float(by_span["sp-overlap-1"]["span_cost_usd"]) == pytest.approx(0.035, abs=1e-12)
    assert float(by_span["sp-overlap-1"]["weighted_burndown_tokens"]) == pytest.approx(
        140_000.0, abs=1e-12
    )
    assert float(sum_row["production_inference_usd"]) == pytest.approx(0.035, abs=1e-12)
    assert float(sum_row["autorater_judge_usd"]) == pytest.approx(0.35, abs=1e-12)
    assert float(sum_row["total_weighted_burndown_tokens"]) == pytest.approx(140_000.0, abs=1e-12)
    assert float(cpvo_row["measured_model_cost_usd"]) == pytest.approx(0.035, abs=1e-12)
    assert float(cpvo_row["measured_judge_cost_usd"]) == pytest.approx(0.35, abs=1e-12)
