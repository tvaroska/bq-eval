"""Transpiled SQL boundary and mutant-killing guard helpers for core, adapter, and FinOps views."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import make_span
from tests.sql_helpers import (
    ENTERPRISE_OTEL_SPANS_BASE_INSERT,
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
)


def _assert_unregistered_and_cliff_and_tool_loop_sql_guards() -> None:
    """Guard `'unregistered_app'` fallback, exact 200K boundary, 8-vs-9 tool limit, and BQAA retry dedup."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    adapter_path = SQL_DIR / "core" / "bqaa_adapter_views.sql"
    conn.execute(
        """
        CREATE TABLE agent_events (
            event_id TEXT, timestamp TEXT, event_type TEXT, agent TEXT,
            session_id TEXT, invocation_id TEXT, trace_id TEXT, span_id TEXT,
            parent_span_id TEXT, content TEXT, attributes TEXT, latency_ms TEXT,
            status TEXT, error_message TEXT
        )
        """
    )
    conn.execute(
        "CREATE TABLE agent_events_eval_turn (trace_id TEXT, turn_id TEXT, has_tool_loop INTEGER)"
    )
    for view_name in (
        "agent_eval.v_bqaa_otel_spans",
        "agent_eval.v_bqaa_trace_payloads",
        "agent_eval.v_bqaa_eval_turn_filtered",
    ):
        short = view_name.split(".", maxsplit=1)[1]
        conn.execute(
            f"CREATE VIEW {short} AS "
            f"{_transpile_bq_view_to_sqlite(_extract_view_sql_text(adapter_path, view_name))}"
        )

    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    # Seed unregistered fallback, exact 200,000 vs 200,001 cliff boundary, and 8-vs-9 tool spans.
    span_rows: list[tuple[object, ...]] = [
        (
            "tr-unreg-1",
            "sp-unreg-1",
            None,
            "chat",
            None,
            "self-reported-svc",
            "agent",
            "SPAN_KIND_SERVER",
            "chat",
            "gemini-3.8-flash",
            None,
            None,
            "OK",
            1,
            "production_inference",
            None,
            None,
            None,
            None,
            None,
            None,
            t0,
            t1,
            t1,
            100.0,
            200_000,
            0,
            10_000,
            0,
            "{}",
        ),
        (
            "tr-cliff-200001",
            "sp-cliff-200001",
            None,
            "chat",
            "cliff-app",
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
            None,
            "cliff-app",
            None,
            None,
            None,
            None,
            t0,
            t1,
            t1,
            100.0,
            200_001,
            0,
            10_000,
            0,
            "{}",
        ),
    ]
    for count_tag, n_tools in (("tr-tools-8", 8), ("tr-tools-9", 9)):
        for i in range(n_tools):
            span_rows.append(
                (
                    count_tag,
                    f"sp-{count_tag}-{i}",
                    None if i == 0 else f"sp-{count_tag}-0",
                    "execute_tool",
                    "tool-app",
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
                    "tool-app",
                    None,
                    None,
                    None,
                    None,
                    t0,
                    t1,
                    t1,
                    10.0,
                    0,
                    0,
                    0,
                    0,
                    "{}",
                )
            )
    conn.executemany(ENTERPRISE_OTEL_SPANS_BASE_INSERT, span_rows)

    # BQAA retry dedup (same event_id, older 1K tokens vs newer 5K tokens) + unregistered fallback
    conn.executemany(
        "INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "ev-retry-1",
                "2026-11-21T12:00:01+00:00",
                "LLM_RESPONSE",
                "rogue_agent",
                "s1",
                "inv1",
                "tr-bqaa-dedup",
                "sp-d1",
                None,
                "{}",
                json.dumps(
                    {
                        "adk": {"app_name": "self_reported_adk"},
                        "model": "gemini-3.8-flash",
                        "usage_metadata": {
                            "prompt_token_count": 1_000,
                            "candidates_token_count": 50,
                        },
                    }
                ),
                '{"total_ms": 100}',
                "OK",
                None,
            ),
            (
                "ev-retry-1",
                "2026-11-21T12:00:05+00:00",
                "LLM_RESPONSE",
                "rogue_agent",
                "s1",
                "inv1",
                "tr-bqaa-dedup",
                "sp-d1",
                None,
                "{}",
                json.dumps(
                    {
                        "adk": {"app_name": "self_reported_adk"},
                        "model": "gemini-3.8-flash",
                        "usage_metadata": {
                            "prompt_token_count": 5_000,
                            "candidates_token_count": 50,
                        },
                    }
                ),
                '{"total_ms": 120}',
                "OK",
                None,
            ),
        ],
    )
    conn.execute("INSERT INTO agent_events_eval_turn VALUES ('tr-bqaa-dedup', 't1', 0)")

    # Check empty-catalog 200K boundary + unregistered fallback
    wf_200k = conn.execute(
        "SELECT apphub_application_id, is_long_context_cliff, span_cost_usd "
        "FROM v_trace_waterfall WHERE trace_id = 'tr-unreg-1'"
    ).fetchone()
    s1_wf_200k = conn.execute(
        "SELECT apphub_application_id, is_long_context_cliff, span_cost_usd "
        "FROM v_step1_trace_waterfall WHERE trace_id = 'tr-unreg-1'"
    ).fetchone()
    wf_200001 = conn.execute(
        "SELECT is_long_context_cliff FROM v_trace_waterfall WHERE trace_id = 'tr-cliff-200001'"
    ).fetchone()
    eval_unreg = conn.execute(
        "SELECT apphub_application_id FROM v_trace_sql_evaluation WHERE trace_id = 'tr-unreg-1'"
    ).fetchone()
    assert wf_200k["apphub_application_id"] == "unregistered_app"
    assert s1_wf_200k["apphub_application_id"] == "unregistered_app"
    assert eval_unreg["apphub_application_id"] == "unregistered_app"
    assert int(wf_200k["is_long_context_cliff"]) == 0
    assert int(s1_wf_200k["is_long_context_cliff"]) == 0
    assert float(wf_200k["span_cost_usd"]) == pytest.approx(0.036, abs=1e-12)
    assert float(s1_wf_200k["span_cost_usd"]) == pytest.approx(0.036, abs=1e-12)
    assert int(wf_200001["is_long_context_cliff"]) == 1

    # Also test populated catalog at exact 200K boundary (kills m.long_context_threshold_tokens >= mutant)
    conn.execute(
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
        ) VALUES (
            'gemini-3.8-flash', 'google', 'long_term_ga',
            '2026-01-01T00:00:00+00:00', '2099-12-31T23:59:59+00:00',
            0.15, 0.015, 0.15, 0.15, 0.60, 200000, 0.30, 0.030, 2.25,
            1.0, 0.1, 1.0, 1.0, 4.0, 2.0, 0.2, 7.5, 1.8, 0.5
        )
        """
    )
    wf_200k_cat = conn.execute(
        "SELECT is_long_context_cliff, span_cost_usd FROM v_trace_waterfall WHERE trace_id = 'tr-unreg-1'"
    ).fetchone()
    assert int(wf_200k_cat["is_long_context_cliff"]) == 0
    assert float(wf_200k_cat["span_cost_usd"]) == pytest.approx(0.036, abs=1e-12)

    s1_t8 = conn.execute(
        "SELECT step1_pass FROM v_step1_trace_eval WHERE trace_id = 'tr-tools-8'"
    ).fetchone()
    s1_t9 = conn.execute(
        "SELECT step1_pass FROM v_step1_trace_eval WHERE trace_id = 'tr-tools-9'"
    ).fetchone()
    ent_t8 = conn.execute(
        "SELECT rule0_pass FROM v_trace_sql_evaluation WHERE trace_id = 'tr-tools-8'"
    ).fetchone()
    ent_t9 = conn.execute(
        "SELECT rule0_pass FROM v_trace_sql_evaluation WHERE trace_id = 'tr-tools-9'"
    ).fetchone()
    assert int(s1_t8["step1_pass"]) == 1 and int(ent_t8["rule0_pass"]) == 1
    assert int(s1_t9["step1_pass"]) == 0 and int(ent_t9["rule0_pass"]) == 0

    bqaa_spans = conn.execute("SELECT input_tokens FROM v_bqaa_otel_spans").fetchall()
    bqaa_pl = conn.execute("SELECT apphub_application_id FROM v_bqaa_trace_payloads").fetchone()
    bqaa_turn = conn.execute(
        "SELECT apphub_application_id FROM v_bqaa_eval_turn_filtered"
    ).fetchone()
    conn.close()
    assert len(bqaa_spans) == 1
    assert int(bqaa_spans[0]["input_tokens"]) == 5_000
    assert bqaa_pl["apphub_application_id"] == "unregistered_app"
    assert bqaa_turn["apphub_application_id"] == "unregistered_app"


def _assert_dedup_cpvo_rule0_and_nfr_sql_guards() -> None:
    """Guard eval dedup DESC order, unverified CPVO denominator, Rule-0 filters, NFR gate fail, and lane default."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    step3_path = SQL_DIR / "core" / "step3_saved_results.sql"
    cap_path = SQL_DIR / "finops" / "capacity_views.sql"
    for path, vname in (
        (step3_path, "agent_eval.v_step3_paired_run_comparison"),
        (step3_path, "agent_eval.v_step3_policy_nfr_gate"),
        (cap_path, "agent_eval.v_focus_three_pool_hourly_chargeback"),
    ):
        short = vname.split(".", maxsplit=1)[1]
        conn.execute(
            f"CREATE VIEW {short} AS "
            f"{_transpile_bq_view_to_sqlite(_extract_view_sql_text(path, vname))}"
        )

    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    # 10 Rule-0 passing traces + 2 Rule-0 failing traces; 2 outcomes (1 verified, 1 unverified)
    for i in range(12):
        r0 = i < 10
        oid = f"out-g-{i:02d}" if i < 2 else None
        insert_enterprise_sqlite_span(
            conn,
            make_span(
                f"trace-g-{i:02d}",
                f"span-g-{i:02d}",
                app_id="guard-app",
                outcome_id=oid,
                outcome_owner="guard-app" if oid else None,
                status_code="OK" if r0 else "ERROR",
                rule0_pass=r0,
                input_tokens=100_000,
                cached_input_tokens=0,
                output_tokens=10_000,
                thinking_tokens=0,
                latency_ms=500.0,
                offset_s=i * 2,
                base_time=base,
            ),
        )
        if oid is not None:
            insert_enterprise_sqlite_span(
                conn,
                make_span(
                    f"trace-g-{i:02d}",
                    f"span-g-close-{i:02d}",
                    parent_span_id=f"span-g-{i:02d}",
                    span_name="outcome.closed",
                    app_id="guard-app",
                    outcome_id=oid,
                    outcome_owner="guard-app",
                    model_name="none",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    thinking_tokens=0,
                    latency_ms=0.0,
                    offset_s=i * 2 + 1,
                    attributes={
                        "outcome.status": "succeeded" if i == 0 else "escalated",
                        "outcome.verified": "true" if i == 0 else "false",
                    },
                    base_time=base,
                ),
            )

    # Unknown traffic type -> lane defaults to 'prod' (kills ELSE 'eval' mutant)
    conn.execute(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        (
            "tr-unk-lane",
            "sp-unk-lane",
            None,
            "chat",
            "lane-app",
            "svc",
            "agent",
            "SPAN_KIND_SERVER",
            "chat",
            "gemini-3.8-flash",
            None,
            None,
            "OK",
            1,
            "custom_unknown_lane",
            None,
            "lane-app",
            None,
            None,
            None,
            None,
            base.isoformat(),
            (base + timedelta(seconds=1)).isoformat(),
            (base + timedelta(seconds=1)).isoformat(),
            100.0,
            10_000,
            0,
            500,
            0,
            "{}",
        ),
    )

    # eval_results:
    # - tr-g-00 has older FAIL (100K tokens) and newer PASS (1K tokens) in SAME run-1 -> DESC must win!
    # - tr-g-10, tr-g-11 have rule0_pass=0, binary_pass=0 -> MUST be excluded from judged_samples/raw rate!
    # - run-2 flips tr-g-01 from PASS -> FAIL on unamended clause -> passes_nfr_gate MUST be 0!
    eval_rows: list[tuple[object, ...]] = [
        (
            "ev-g-00-old",
            "run-1",
            "trace-g-00",
            "out-g-00",
            "guard-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            "CANARY_5PCT",
            1,
            "old fail",
            1,
            0,
            100_000,
            10_000,
            0,
            "2026-11-21T12:01:00+00:00",
            "sha-v1",
            None,
            "CL-1",
        ),
        (
            "ev-g-00-new",
            "run-1",
            "trace-g-00",
            "out-g-00",
            "guard-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            "CANARY_5PCT",
            1,
            "new pass",
            4,
            1,
            1_000,
            100,
            0,
            "2026-11-21T12:05:00+00:00",
            "sha-v1",
            None,
            "CL-1",
        ),
    ]
    for i in range(1, 10):
        eval_rows.append(
            (
                f"ev-g-{i:02d}",
                "run-1",
                f"trace-g-{i:02d}",
                f"out-g-{i:02d}" if i < 2 else None,
                "guard-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "pass",
                4,
                1,
                1_000,
                100,
                0,
                "2026-11-21T12:05:00+00:00",
                "sha-v1",
                None,
                "CL-1",
            )
        )
    for i in (10, 11):
        eval_rows.append(
            (
                f"ev-g-r0f-{i}",
                "run-1",
                f"trace-g-{i:02d}",
                None,
                "guard-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "ERROR_OR_SLA_TAIL",
                0,
                "r0 fail",
                1,
                0,
                1_000,
                100,
                0,
                "2026-11-21T12:05:00+00:00",
                "sha-v1",
                None,
                "CL-1",
            )
        )
    # Candidate run-2 flips trace-g-01 from PASS -> FAIL on unamended clause CL-1
    eval_rows.append(
        (
            "ev-g-01-flip",
            "run-2",
            "trace-g-01",
            "out-g-01",
            "guard-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            "CANARY_5PCT",
            1,
            "regressed",
            1,
            0,
            1_000,
            100,
            0,
            "2026-11-21T13:00:00+00:00",
            "sha-v2",
            "CL-9",
            "CL-1",
        )
    )
    conn.executemany(
        "INSERT INTO eval_results ("
        "eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id, "
        "judge_model, sampling_stratum, rule0_pass, reasoning_trace, ordinal_score, "
        "binary_pass, judge_input_tokens, judge_output_tokens, judge_thinking_tokens, "
        "evaluated_at, policy_sha256, amended_clause_ids, target_clause_id"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        eval_rows,
    )
    # SME labels: valid rule0_pass=1 (perfect 10/10) + corrupted rule0_pass=0 (must be ignored!)
    sme_rows = (
        [("guard-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 10
        + [("guard-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)] * 10
        + [("guard-app", "groundedness_4pt_v1", 0, 4, 1, 1, 0)] * 25
        + [("guard-app", "groundedness_4pt_v1", 0, 1, 0, 4, 1)] * 25
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)

    cpvo = conn.execute(
        "SELECT total_outcomes, verified_outcomes, total_ai_cost_usd, cost_per_verified_outcome_ai_usd "
        "FROM v_enterprise_cost_per_verified_outcome WHERE apphub_application_id = 'guard-app'"
    ).fetchone()
    summ = conn.execute(
        "SELECT judged_samples, raw_judge_pass_rate, calibrated_pass_rate, gwet_ac2 "
        "FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'guard-app'"
    ).fetchone()
    nfr = conn.execute(
        "SELECT unamended_negative_flips, passes_nfr_gate FROM v_step3_policy_nfr_gate "
        "WHERE apphub_application_id = 'guard-app'"
    ).fetchone()
    lane_row = conn.execute(
        "SELECT traffic_lane FROM v_focus_three_pool_hourly_chargeback "
        "WHERE apphub_application_id = 'lane-app'"
    ).fetchone()
    conn.close()

    # Only out-g-00 is verified (tr-g-00 latest verdict=PASS); out-g-01 is unverified -> denom = 1
    assert int(cpvo["total_outcomes"]) == 2
    assert int(cpvo["verified_outcomes"]) == 1
    assert float(cpvo["cost_per_verified_outcome_ai_usd"]) == pytest.approx(
        float(cpvo["total_ai_cost_usd"]), abs=1e-12
    )
    # 10 rule0-passing judged (tr-g-00 latest=PASS, tr-g-01 latest in run-2=FAIL -> 9/10=0.90)
    assert int(summ["judged_samples"]) == 10
    assert float(summ["raw_judge_pass_rate"]) == pytest.approx(0.90, abs=1e-12)
    # sme_raw ignored rule0_pass=0 corruption -> TPR=TNR=1.0 -> calibrated == raw == 0.90 and AC2 == 1.0
    assert float(summ["calibrated_pass_rate"]) == pytest.approx(0.90, abs=1e-12)
    assert float(summ["gwet_ac2"]) == pytest.approx(1.0, abs=1e-12)
    assert int(nfr["unamended_negative_flips"]) == 1
    assert int(nfr["passes_nfr_gate"]) == 0
    assert lane_row["traffic_lane"] == "prod"
