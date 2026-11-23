"""Direct unit tests for `tests/sql_helpers.py` and execution tests for all remaining SQL views."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import make_span
from tests.sql_helpers import (
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _sqlite_farm_fingerprint,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"


def test_sql_helpers_bigquery_safe_cast_and_temporal_semantics() -> None:
    """Emulator `SAFE_CAST` and `TIMESTAMP_SUB` match BigQuery probe behavior."""
    conn = _new_sqlite_bq_conn(
        current_date_utc="2026-11-21",
        current_timestamp_utc="2026-11-21T12:00:00+00:00",
    )
    row = conn.execute(
        _transpile_bq_view_to_sqlite(
            """
            SELECT
              SAFE_CAST('true' AS BOOL) AS c_true,
              SAFE_CAST('false' AS BOOL) AS c_false,
              SAFE_CAST('TRUE' AS BOOL) AS c_upper_true,
              SAFE_CAST(' false ' AS BOOL) AS c_space_false,
              SAFE_CAST('0' AS BOOL) AS c_zero,
              SAFE_CAST('1' AS BOOL) AS c_one,
              SAFE_CAST('no' AS BOOL) AS c_no,
              SAFE_CAST('abc' AS FLOAT64) AS c_bad_float,
              SAFE_CAST('3.5' AS FLOAT64) AS c_ok_float,
              SAFE_CAST('1.5' AS INT64) AS c_bad_int,
              SAFE_CAST('42' AS INT64) AS c_ok_int,
              TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR) AS minus_1h,
              TIMESTAMP_SUB('2026-11-21T12:00:01+00:00', INTERVAL 500 MILLISECOND) AS minus_500ms
            """
        )
    ).fetchone()
    conn.close()

    assert row["c_true"] == 1
    assert row["c_false"] == 0
    assert row["c_upper_true"] == 1
    assert row["c_space_false"] is None
    assert row["c_zero"] is None
    assert row["c_one"] is None
    assert row["c_no"] is None
    assert row["c_bad_float"] is None
    assert float(row["c_ok_float"]) == pytest.approx(3.5, abs=1e-12)
    assert row["c_bad_int"] is None
    assert row["c_ok_int"] == 42
    assert row["minus_1h"] == "2026-11-21T11:00:00+00:00"
    assert row["minus_500ms"] == "2026-11-21T12:00:00.500000+00:00"


def _find_trace_id_with_mod(predicate_upper: int, *, include: bool) -> str:
    for idx in range(5000):
        candidate = f"tr-fp-{idx:04d}"
        fp = _sqlite_farm_fingerprint(candidate)
        assert fp is not None
        bucket = abs(fp) % 100
        if (bucket < predicate_upper) is include:
            return candidate
    raise AssertionError("Failed to find matching trace_id")


def test_step1_coverage_cost_and_step2_slice_views_execute() -> None:
    """Execute `v_otel_spans_coverage`, `v_step1_cost_per_outcome`, and all 5 Step-2 prompt/slice views."""
    step1_path = SQL_DIR / "core" / "step1_trace_views.sql"
    step2_path = SQL_DIR / "core" / "step2_stateless_eval.sql"

    conn = _new_sqlite_bq_conn(
        current_date_utc="2026-11-21",
        current_timestamp_utc="2026-11-21T12:00:00+00:00",
    )
    _create_enterprise_sqlite_fixture(conn)
    conn.execute(
        "CREATE TABLE trace_payloads (trace_id TEXT, user_prompt TEXT, retrieved_context TEXT, final_response TEXT)"
    )
    conn.execute(
        "CREATE TABLE rubrics (rubric_id TEXT, title TEXT, dimension TEXT, passing_score INTEGER, judge_model TEXT, rubric_prompt TEXT)"
    )

    for sql_path, view_name in (
        (step1_path, "agent_eval.v_default_model_pricing"),
        (step1_path, "agent_eval.v_step1_trace_waterfall"),
        (step1_path, "agent_eval.v_step1_trace_eval"),
        (step1_path, "agent_eval.v_step1_cost_per_outcome"),
        (step1_path, "agent_eval.v_otel_spans_coverage"),
        (step2_path, "agent_eval.v_step2_all_trace_rubric_prompts"),
        (step2_path, "agent_eval.v_step2_trace_rubric_prompts"),
        (step2_path, "agent_eval.v_step2_example_10pct_sample"),
        (step2_path, "agent_eval.v_step2_5_example_5pct_slice"),
        (step2_path, "agent_eval.v_step2_5_example_last_1h_slice"),
        (step2_path, "agent_eval.v_step2_5_example_last_1h_canary_and_errors"),
    ):
        short_name = view_name.split(".", maxsplit=1)[1]
        view_sql = _transpile_bq_view_to_sqlite(_extract_view_sql_text(sql_path, view_name))
        conn.execute(f"CREATE VIEW {short_name} AS {view_sql}")

    tr_in_5pct = _find_trace_id_with_mod(5, include=True)
    tr_out_10pct = _find_trace_id_with_mod(10, include=False)
    tr_old = _find_trace_id_with_mod(5, include=True)
    tr_err = f"{tr_out_10pct}-err"

    recent_time = datetime(2026, 11, 21, 11, 30, 0, tzinfo=UTC)
    old_time = datetime(2026, 11, 21, 9, 0, 0, tzinfo=UTC)

    spans = [
        make_span(
            tr_in_5pct,
            "sp-in5",
            app_id="slice-app",
            service_name="svc-slice",
            outcome_id="out-in5",
            input_tokens=10_000,
            cached_input_tokens=2_000,
            output_tokens=500,
            base_time=recent_time,
        ),
        make_span(
            tr_out_10pct,
            "sp-out10",
            app_id="slice-app",
            service_name="svc-slice",
            outcome_id=None,
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=500,
            base_time=recent_time,
        ),
        make_span(
            f"{tr_old}-old",
            "sp-old",
            app_id="slice-app",
            service_name="svc-slice",
            outcome_id="out-old",
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=500,
            base_time=old_time,
        ),
        make_span(
            tr_err,
            "sp-err",
            app_id="slice-app",
            service_name="svc-slice",
            status_code="ERROR",
            rule0_pass=False,
            outcome_id="out-err",
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=500,
            base_time=recent_time,
        ),
    ]
    for sp in spans:
        insert_enterprise_sqlite_span(conn, sp)

    conn.executemany(
        "INSERT INTO trace_payloads VALUES (?,?,?,?)",
        [
            (tr_in_5pct, "hi <u1> & >", "ctx <c1> & >", "resp <r1> & >"),
            (tr_out_10pct, "hi 2", "ctx 2", "resp 2"),
            (f"{tr_old}-old", "hi old", "ctx old", "resp old"),
            (tr_err, "hi err", "ctx err", "resp err"),
        ],
    )
    conn.executemany(
        "INSERT INTO rubrics VALUES (?,?,?,?,?,?)",
        [
            (
                "groundedness_4pt_v1",
                "Groundedness",
                "groundedness",
                3,
                "gemini-3.8-flash",
                "Score groundedness.",
            ),
            (
                "tool_trajectory_4pt_v1",
                "Tool Trajectory",
                "tool_trajectory",
                3,
                "gemini-3.8-flash",
                "Score tools.",
            ),
            (
                "escalation_policy_4pt_v1",
                "Escalation",
                "escalation_policy",
                3,
                "gemini-3.8-flash",
                "Score escalation.",
            ),
        ],
    )

    cov = conn.execute(
        "SELECT * FROM v_otel_spans_coverage WHERE service_name = 'svc-slice'"
    ).fetchone()
    s1_cpo = conn.execute(
        "SELECT * FROM v_step1_cost_per_outcome WHERE apphub_application_id = 'slice-app'"
    ).fetchone()
    s2_prompt_rows = conn.execute(
        "SELECT trace_id, prompt FROM v_step2_trace_rubric_prompts"
    ).fetchall()
    s2_pass_only = {str(r["trace_id"]) for r in s2_prompt_rows}
    s2_in5_prompt = next(
        str(r["prompt"]) for r in s2_prompt_rows if str(r["trace_id"]) == tr_in_5pct
    )
    s2_10pct = {
        str(r["trace_id"])
        for r in conn.execute("SELECT trace_id FROM v_step2_example_10pct_sample").fetchall()
    }
    s2_5pct = {
        str(r["trace_id"])
        for r in conn.execute("SELECT trace_id FROM v_step2_5_example_5pct_slice").fetchall()
    }
    s2_1h = {
        str(r["trace_id"])
        for r in conn.execute("SELECT trace_id FROM v_step2_5_example_last_1h_slice").fetchall()
    }
    s2_1h_ce = {
        str(r["trace_id"]): str(r["sampling_reason"])
        for r in conn.execute(
            "SELECT trace_id, sampling_reason FROM v_step2_5_example_last_1h_canary_and_errors"
        ).fetchall()
    }
    conn.close()

    assert int(cov["model_call_spans"]) == 4
    assert float(cov["outcome_id_coverage_pct"]) == pytest.approx(75.0, abs=1e-12)
    assert float(cov["tokens_coverage_pct"]) == pytest.approx(100.0, abs=1e-12)

    assert int(s1_cpo["total_traces"]) == 4
    assert int(s1_cpo["step1_passing_traces"]) == 3
    assert float(s1_cpo["step1_pass_rate"]) == pytest.approx(0.75, abs=1e-12)
    assert float(s1_cpo["step1_cost_per_outcome_usd"]) == pytest.approx(
        float(s1_cpo["total_cost_usd"]) / 3.0, abs=1e-12
    )

    assert tr_err not in s2_pass_only
    assert tr_in_5pct in s2_pass_only
    assert all(
        tok in s2_in5_prompt
        for tok in (
            "hi &lt;u1&gt; &amp; &gt;",
            "ctx &lt;c1&gt; &amp; &gt;",
            "resp &lt;r1&gt; &amp; &gt;",
        )
    )
    assert "<u1>" not in s2_in5_prompt
    assert tr_in_5pct in s2_10pct
    assert tr_out_10pct not in s2_10pct
    assert tr_in_5pct in s2_5pct
    assert tr_out_10pct not in s2_5pct
    assert s2_1h == {tr_in_5pct, tr_out_10pct}
    assert s2_1h_ce[tr_in_5pct] == "LAST_1H_CANARY_5PCT"
    assert s2_1h_ce[tr_err] == "ERROR_OR_SLA_TAIL_100PCT"
    assert f"{tr_old}-old" not in s2_1h_ce


def test_bqaa_eval_turn_filtered_and_omni_views_execute() -> None:
    """Execute `v_bqaa_eval_turn_filtered` and all 4 BigQuery Omni multi-cloud views."""
    adapter_path = SQL_DIR / "core" / "bqaa_adapter_views.sql"
    omni_path = SQL_DIR / "finops" / "omni_spoke_views.sql"

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
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
    spoke_cols = (
        "trace_id TEXT, span_id TEXT, parent_span_id TEXT, span_name TEXT, "
        "start_time TEXT, end_time TEXT, latency_ms REAL, status_code TEXT, span_kind TEXT, "
        "service_name TEXT, gcp_apphub_application_id TEXT, workload_identity TEXT, "
        "model_name TEXT, input_tokens INTEGER, cached_input_tokens INTEGER, "
        "cache_write_5m_tokens INTEGER, cache_write_1h_tokens INTEGER, output_tokens INTEGER, "
        "thinking_tokens INTEGER, rule0_pass INTEGER, finops_traffic_type TEXT, "
        "serving_traffic_type TEXT, http_status_code INTEGER, outcome_id TEXT, "
        "outcome_verified INTEGER, cloud_region TEXT, span_cost_usd REAL"
    )
    conn.execute(f"CREATE TABLE aws_omni_otel_spans ({spoke_cols})")
    conn.execute(f"CREATE TABLE azure_omni_otel_spans ({spoke_cols})")
    conn.execute(
        """
        CREATE TABLE remote_trace_slices (
            cloud_provider TEXT, cloud_region TEXT, usage_hour TEXT,
            apphub_application_id TEXT, workload_principal TEXT, model_name TEXT,
            finops_traffic_type TEXT, total_traces INTEGER, rule0_passing_traces INTEGER,
            verified_outcomes INTEGER, total_input_tokens INTEGER, total_output_tokens INTEGER,
            weighted_burndown_tokens REAL, total_spend_usd REAL
        )
        """
    )

    for sql_path, view_name in (
        (adapter_path, "agent_eval.v_bqaa_otel_spans"),
        (adapter_path, "agent_eval.v_bqaa_eval_turn_filtered"),
        (omni_path, "agent_eval.v_omni_aws_spoke_spans"),
        (omni_path, "agent_eval.v_omni_azure_spoke_spans"),
        (omni_path, "agent_eval.v_omni_remote_trace_slice_rollup"),
        (omni_path, "agent_eval.v_unified_multicloud_cpvo"),
    ):
        short_name = view_name.split(".", maxsplit=1)[1]
        conn.execute(
            f"CREATE VIEW {short_name} AS "
            f"{_transpile_bq_view_to_sqlite(_extract_view_sql_text(sql_path, view_name))}"
        )

    conn.executemany(
        "INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "ev-pass",
                "2026-11-21T12:00:01+00:00",
                "LLM_RESPONSE",
                "agent_a",
                "s1",
                "inv1",
                "tr-turn-pass",
                "sp1",
                None,
                "{}",
                json.dumps(
                    {
                        "model": "gemini-3.8-flash",
                        "usage_metadata": {
                            "prompt_token_count": 10_000,
                            "candidates_token_count": 500,
                        },
                        "custom_metadata": {"apphub.application.id": "turn-app"},
                    }
                ),
                '{"total_ms": 100}',
                "OK",
                None,
            ),
            (
                "ev-loop",
                "2026-11-21T12:00:02+00:00",
                "LLM_RESPONSE",
                "agent_a",
                "s2",
                "inv2",
                "tr-turn-loop",
                "sp2",
                None,
                "{}",
                json.dumps({"model": "gemini-3.8-flash"}),
                '{"total_ms": 100}',
                "OK",
                None,
            ),
            (
                "ev-err",
                "2026-11-21T12:00:03+00:00",
                "TOOL_ERROR",
                "agent_a",
                "s3",
                "inv3",
                "tr-turn-err",
                "sp3",
                None,
                "{}",
                "{}",
                '{"total_ms": 100}',
                "ERROR",
                "boom",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO agent_events_eval_turn VALUES (?,?,?)",
        [
            ("tr-turn-pass", "t1", 0),
            ("tr-turn-loop", "t2", 1),
            ("tr-turn-err", "t3", 0),
        ],
    )

    conn.execute(
        "INSERT INTO aws_omni_otel_spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "tr-aws-1",
            "sp-aws-1",
            None,
            "chat",
            "2026-11-21T12:15:00+00:00",
            "2026-11-21T12:15:01+00:00",
            500.0,
            "OK",
            "chat",
            "svc-aws",
            "multi-app",
            "arn:aws:iam::123:role/agent",
            "claude-sonnet-4-6",
            10_000,
            2_000,
            0,
            0,
            1_000,
            0,
            1,
            "production_inference",
            "STANDARD",
            200,
            "out-aws-1",
            1,
            "aws-us-east-1",
            0.05,
        ),
    )
    conn.execute(
        "INSERT INTO azure_omni_otel_spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "tr-az-1",
            "sp-az-1",
            None,
            "chat",
            "2026-11-21T12:20:00+00:00",
            "2026-11-21T12:20:01+00:00",
            400.0,
            "OK",
            "chat",
            "svc-az",
            "",
            "spiffe://cluster.local/ns/ai/sa/agent",
            "gemini-3.8-flash",
            5_000,
            0,
            0,
            0,
            500,
            0,
            1,
            "production_inference",
            "STANDARD",
            200,
            "out-az-1",
            1,
            "",
            0.02,
        ),
    )

    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    insert_enterprise_sqlite_span(
        conn,
        make_span(
            "tr-gcp-1",
            "sp-gcp-1",
            app_id="multi-app",
            outcome_id="out-gcp-1",
            outcome_owner="multi-app",
            input_tokens=100_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            latency_ms=1000.0,
            base_time=base,
        ),
    )
    insert_enterprise_sqlite_span(
        conn,
        make_span(
            "tr-gcp-1",
            "sp-gcp-close",
            parent_span_id="sp-gcp-1",
            span_name="outcome.closed",
            app_id="multi-app",
            outcome_id="out-gcp-1",
            outcome_owner="multi-app",
            model_name="none",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            latency_ms=0.0,
            offset_s=1,
            attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
            base_time=base,
        ),
    )

    # Materialize rollup into remote_trace_slices to exercise v_unified_multicloud_cpvo
    conn.execute("INSERT INTO remote_trace_slices SELECT * FROM v_omni_remote_trace_slice_rollup")

    filtered_turns = [
        str(r["trace_id"])
        for r in conn.execute("SELECT trace_id FROM v_bqaa_eval_turn_filtered").fetchall()
    ]
    aws_row = conn.execute("SELECT * FROM v_omni_aws_spoke_spans").fetchone()
    az_row = conn.execute("SELECT * FROM v_omni_azure_spoke_spans").fetchone()
    rollup_rows = {
        str(r["cloud_provider"]): r
        for r in conn.execute("SELECT * FROM v_omni_remote_trace_slice_rollup").fetchall()
    }
    unified = {
        str(r["apphub_application_id"]): r
        for r in conn.execute("SELECT * FROM v_unified_multicloud_cpvo").fetchall()
    }
    conn.close()

    assert filtered_turns == ["tr-turn-pass"]
    assert aws_row["workload_principal"] == (
        "principal://iam.googleapis.com/projects/external-aws/subject/arn:aws:iam::123:role/agent"
    )
    assert az_row["apphub_application_id"] == "unregistered_app"
    assert az_row["cloud_region"] == "azure-eastus2"
    assert az_row["workload_principal"] == "spiffe://cluster.local/ns/ai/sa/agent"
    assert set(rollup_rows) == {"aws", "azure"}
    assert int(rollup_rows["aws"]["verified_outcomes"]) == 1
    assert int(unified["multi-app"]["active_clouds"]) == 2
    assert int(unified["multi-app"]["verified_outcomes"]) == 2
    assert float(unified["multi-app"]["unified_cost_per_verified_outcome_usd"]) > 0.0
