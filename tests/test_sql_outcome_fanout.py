"""Multi-agent outcomes split infra and judge cost exactly once across agents."""

from __future__ import annotations

import re

import pytest

from tests.sql_helpers import (
    SQL_DIR,
    _extract_view_sql_text,
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
)


def test_multi_key_outcome_fanout_exact_1x_infra_and_judge_cost() -> None:
    """Multi-agent, multi-source_mode, multi-app & multi-skill slices sum to exact 1.0x outcome cost."""
    step1_path = SQL_DIR / "core" / "step1_trace_views.sql"
    infra_sql = _extract_view_sql_text(step1_path, "agent_eval.v_infra_cost_estimate")
    cpvo_sql = _extract_view_sql_text(step1_path, "agent_eval.v_cost_per_verified_outcome")
    skill_sql = _extract_view_sql_text(step1_path, "agent_eval.v_skill_lineage_roi")

    assert re.search(
        r"GROUP\s+BY\s+w\.outcome_id\s*\)",
        infra_sql,
        flags=re.IGNORECASE,
    ), "v_infra_cost_estimate must group strictly by w.outcome_id (1:1 outcome key)"

    assert "OVER (PARTITION BY" in cpvo_sql, (
        "v_cost_per_verified_outcome must proportionally allocate outcome infra cost across slices"
    )
    assert "OVER (PARTITION BY" in skill_sql, (
        "v_skill_lineage_roi must proportionally allocate outcome infra cost across skill slices"
    )

    conn = _new_sqlite_bq_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE v_step1_trace_waterfall (
            trace_id TEXT,
            span_id TEXT,
            span_name TEXT,
            apphub_application_id TEXT,
            agent_name TEXT,
            otel_span_kind TEXT,
            model_name TEXT,
            finops_traffic_type TEXT,
            outcome_id TEXT,
            outcome_owner TEXT,
            source_mode TEXT,
            skill_id TEXT,
            skill_version TEXT,
            latency_ms REAL,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            thinking_tokens INTEGER,
            span_cost_usd REAL,
            attributes TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE v_outcomes (
            outcome_id TEXT,
            outcome_status TEXT,
            outcome_subject TEXT,
            outcome_parent_id TEXT,
            outcome_owner TEXT,
            source_mode TEXT,
            skill_id TEXT,
            skill_version TEXT,
            is_verified INTEGER
        )
        """
    )

    cur.execute(
        """
        INSERT INTO v_outcomes VALUES (
            'out-multi-001', 'succeeded', 'ent-1', 'deal-1',
            'parent-app', 'PDF_PATH_B_FLASH', 'skill-a', '1.0.0', 1
        )
        """
    )
    # 4 server spans for the same outcome_id spanning 3 agents, 2 source_modes, 2 raw apps, 2 skills.
    # Each span has latency_ms = 1000ms (1s) at 2 vCPU / 2 GiB ($0.000053/s) => compute = $0.000212
    # Plus default storage $0.000020 => exact 1.0x outcome infra cost = $0.000232.
    rows = [
        (
            "t1",
            "s1",
            "invoke_agent",
            "app-sub-a",
            "agent-intake",
            "SPAN_KIND_SERVER",
            "gemini-3.8-flash",
            "production_inference",
            "out-multi-001",
            "parent-app",
            "A2A_PATH_A",
            "skill-a",
            "1.0.0",
            1000.0,
            1000,
            0,
            100,
            0,
            0.010,
            "{}",
        ),
        (
            "t1",
            "s2",
            "invoke_agent",
            "app-sub-b",
            "agent-extract",
            "SPAN_KIND_SERVER",
            "gemini-3.8-flash",
            "production_inference",
            "out-multi-001",
            "parent-app",
            "PDF_PATH_B_FLASH",
            "skill-a",
            "1.0.0",
            1000.0,
            2000,
            0,
            200,
            0,
            0.020,
            "{}",
        ),
        (
            "t2",
            "s3",
            "invoke_agent",
            "app-sub-a",
            "agent-adjudicate",
            "SPAN_KIND_SERVER",
            "gemini-4-argon",
            "conflict_escalation",
            "out-multi-001",
            "parent-app",
            "PDF_PATH_B_FLASH",
            "skill-b",
            "1.1.0",
            1000.0,
            3000,
            0,
            300,
            0,
            0.030,
            '{"hitl.review": "true"}',
        ),
        (
            "t2",
            "s4",
            "invoke_agent",
            "app-sub-b",
            "agent-adjudicate",
            "SPAN_KIND_SERVER",
            "gemini-3.8-flash",
            "autorater_judge",
            "out-multi-001",
            "parent-app",
            "PDF_PATH_B_FLASH",
            "skill-b",
            "1.1.0",
            1000.0,
            500,
            0,
            50,
            0,
            0.005,
            "{}",
        ),
    ]
    cur.executemany(
        "INSERT INTO v_step1_trace_waterfall VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )

    cur.execute(f"CREATE VIEW v_infra_cost_estimate AS {_transpile_bq_view_to_sqlite(infra_sql)}")
    cur.execute(
        f"CREATE VIEW v_cost_per_verified_outcome AS {_transpile_bq_view_to_sqlite(cpvo_sql)}"
    )
    cur.execute(f"CREATE VIEW v_skill_lineage_roi AS {_transpile_bq_view_to_sqlite(skill_sql)}")

    infra_rows = cur.execute(
        "SELECT outcome_id, estimated_infra_cost_usd FROM v_infra_cost_estimate"
    ).fetchall()
    assert len(infra_rows) == 1, f"Expected 1 infra row per outcome_id, got {len(infra_rows)}"
    expected_infra_usd = float(infra_rows[0][1])
    assert expected_infra_usd == pytest.approx(4 * 0.000053 + 0.000020, abs=1e-12)

    cpvo_sum = cur.execute(
        "SELECT SUM(estimated_infra_cost_usd), SUM(measured_model_cost_usd), SUM(measured_judge_cost_usd) "
        "FROM v_cost_per_verified_outcome"
    ).fetchone()
    assert float(cpvo_sum[0]) == pytest.approx(expected_infra_usd, abs=1e-12)
    assert float(cpvo_sum[1]) == pytest.approx(0.060, abs=1e-12)
    assert float(cpvo_sum[2]) == pytest.approx(0.005, abs=1e-12)

    skill_sum = cur.execute(
        "SELECT SUM(estimated_infra_cost_usd), SUM(measured_model_cost_usd) FROM v_skill_lineage_roi"
    ).fetchone()
    assert float(skill_sum[0]) == pytest.approx(expected_infra_usd, abs=1e-12)
    assert float(skill_sum[1]) == pytest.approx(0.065, abs=1e-12)
    conn.close()
