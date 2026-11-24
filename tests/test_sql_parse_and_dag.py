"""SQL pack parses as BigQuery, has no duplicate views, deploys in DAG order, and stays in sync with Python."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlglot import exp

from bq_eval.core.contract.otel import (
    BQAAAgentEvent,
)
from bq_eval.core.engine.bqaa import bqaa_events_to_trace_payloads
from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)
from bq_eval.demo.loadgen import export_time_shifted_jsonl, generate_time_shifted_corpus
from scripts.bq_dry_run import split_sql_statements
from tests.sql_helpers import (
    SQL_DIR,
    _extract_mode_c_views,
    _extract_view_definitions,
    _extract_view_sql_text,
    _find_graph_cycles,
    _find_unescaped_multiline_single_quoted_strings,
    _new_sqlite_bq_conn,
    _parse_bigquery_statements,
    _referenced_tables,
    _transpile_bq_view_to_sqlite,
)


def test_all_sql_files_parse_as_bigquery_ast_and_valid_googlesql_strings() -> None:
    """Every `sql/*.sql` file parses cleanly as BigQuery AST with valid GoogleSQL strings."""
    sql_files = sorted(SQL_DIR.rglob("*.sql"))
    assert sql_files, "No SQL files found under sql/"
    for sql_file in sql_files:
        text = sql_file.read_text(encoding="utf-8")
        bad_strings = _find_unescaped_multiline_single_quoted_strings(text)
        assert not bad_strings, (
            f"{sql_file.name} has raw newlines in single-quoted SQL: {bad_strings}"
        )
        _parse_bigquery_statements(text, sql_file.name)


def test_generated_judge_sql_valid_googlesql_and_contract() -> None:
    """`build_ai_generate_table_judge_sql` & `build_ai_evaluate_agent_sql` emit valid GoogleSQL."""
    for run_id in (None, "m2-live-20261113b"):
        gen_sql = build_ai_generate_table_judge_sql(
            project_id="demo-proj",
            dataset_id="agent_eval",
            rubric_id="groundedness_4pt_v1",
            canary_sample_pct=5,
            run_id=run_id,
        )
        bad_strings = _find_unescaped_multiline_single_quoted_strings(gen_sql)
        assert not bad_strings, (
            f"build_ai_generate_table_judge_sql emitted raw newlines in single-quoted literal: {bad_strings}"
        )
        _parse_bigquery_statements(gen_sql, f"build_ai_generate_table_judge_sql(run_id={run_id})")

        assert (
            "'reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL' AS output_schema"
            in gen_sql
        ), "output_schema must match SKILL.md contract (no redundant binary_pass in output_schema)"
        assert "INNER JOIN deduped_payloads" in gen_sql, (
            "trace_payloads join must be INNER JOIN on deduplicated non-empty payloads"
        )
        assert "COALESCE(status, '') = ''" in gen_sql
        assert "ordinal_score BETWEEN 1 AND 4" in gen_sql
        for token_col in ("judge_input_tokens", "judge_output_tokens", "judge_thinking_tokens"):
            assert token_col in gen_sql, f"Missing eval_results column {token_col} in judge SQL"

    eval_sql = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        dataset_id="agent_eval",
        canary_sample_pct=10,
        run_id="m2-live-20261113b",
    )
    assert not _find_unescaped_multiline_single_quoted_strings(eval_sql)
    _parse_bigquery_statements(eval_sql, "build_ai_evaluate_agent_sql")

    for bad_ident in ("bad; DROP TABLE x--", "proj`x", ""):
        with pytest.raises(ValueError, match="Invalid"):
            build_ai_generate_table_judge_sql(project_id=bad_ident)
        with pytest.raises(ValueError, match="Invalid"):
            build_ai_evaluate_agent_sql(project_id="demo-proj", eval_view=bad_ident)


def test_zero_duplicate_view_definitions_across_sql_pack() -> None:
    """No two `sql/*.sql` files redefine the same `CREATE OR REPLACE VIEW` name."""
    views = _extract_view_definitions()
    duplicates = {
        name: [fname for fname, _ in defs] for name, defs in views.items() if len(defs) > 1
    }
    assert not duplicates, f"Duplicate view definitions across sql/*.sql: {duplicates}"

    required_views = {
        "agent_eval.otel_spans",
        "agent_eval.trace_payloads",
        "agent_eval.v_step1_trace_waterfall",
        "agent_eval.v_step1_trace_eval",
        "agent_eval.v_step1_cost_per_outcome",
        "agent_eval.v_outcomes",
        "agent_eval.v_infra_cost_estimate",
        "agent_eval.v_cost_per_verified_outcome",
        "agent_eval.v_attribution_coverage",
        "agent_eval.v_skill_lineage_roi",
        "agent_eval.v_otel_spans_coverage",
        "agent_eval.v_trace_waterfall",
        "agent_eval.v_trace_sql_evaluation",
        "agent_eval.v_counterparty_spend",
        "agent_eval.v_agent_eval_and_finops_summary",
        "agent_eval.v_capacity_hourly_burndown_export",
    }
    missing = required_views - set(views)
    assert not missing, f"Missing required canonical views: {sorted(missing)}"

    step1_cpo_sql = _extract_view_sql_text(
        SQL_DIR / "core" / "step1_trace_views.sql", "agent_eval.v_step1_cost_per_outcome"
    )
    assert "avg_cache_hit_ratio" in step1_cpo_sql


def test_view_dependency_dag_is_acyclic_and_light_capture_deployable() -> None:
    """View graph has zero cycles, Light Capture deploys in one pass, and live Mode A/C views are acyclic."""
    views = _extract_view_definitions()
    view_names = set(views)
    deps = {
        name: {ref for _, stmt in defs for ref in (_referenced_tables(stmt) & view_names)}
        for name, defs in views.items()
    }
    cyclic = _find_graph_cycles(deps)
    assert not cyclic, f"View dependency cycle detected among: {cyclic}"

    bqaa_sql = (SQL_DIR / "core" / "bqaa_adapter_views.sql").read_text(encoding="utf-8")
    step1_views = {
        name for name, defs in views.items() if any(f == "step1_trace_views.sql" for f, _ in defs)
    }
    for stmt in _parse_bigquery_statements(bqaa_sql, "bqaa_adapter_views.sql"):
        cycle_refs = _referenced_tables(stmt) & step1_views
        assert not cycle_refs, (
            f"bqaa_adapter_views.sql depends on step1_trace_views.sql views {cycle_refs}, "
            "creating a deployment cycle on fresh Light Capture projects"
        )

    mode_views = _extract_mode_c_views(bqaa_sql)
    for expected_view in (
        "agent_eval.v_mode_a_otel_spans",
        "agent_eval.v_mode_a_trace_payloads",
        "agent_eval.v_mode_c_hybrid_otel_spans",
        "agent_eval.v_mode_c_hybrid_trace_payloads",
    ):
        assert expected_view in mode_views
    for vname, refs in mode_views.items():
        assert vname not in refs, f"Mode view {vname} directly references itself"
        mutual = [t for t in refs if t in mode_views and vname in mode_views[t]]
        assert not mutual, f"Mode circular view cycle: {vname} <-> {mutual}"


def test_bqaa_sql_adapter_matches_python_custom_metadata_and_outcome_closed() -> None:
    """`v_bqaa_otel_spans` promotes `custom_metadata` keys and maps `OUTCOME_CLOSED` to `invoke_agent`."""
    view_sql = _extract_view_sql_text(
        SQL_DIR / "core" / "bqaa_adapter_views.sql", "agent_eval.v_bqaa_otel_spans"
    )
    assert re.search(
        r"WHEN\s+event_type\s+IN\s*\([^)]*'OUTCOME_CLOSED'[^)]*\)\s*(?:OR[^\n]*)?THEN\s+'invoke_agent'",
        view_sql,
        flags=re.IGNORECASE | re.DOTALL,
    ), "v_bqaa_otel_spans must map OUTCOME_CLOSED to span_kind = 'invoke_agent'"

    for key in (
        "business_unit",
        "cost_center",
        "outcome.status",
        "outcome.verified",
        "outcome.subject",
        "outcome.parent_id",
    ):
        assert key in view_sql, (
            f"v_bqaa_otel_spans must promote custom_metadata key {key!r} into top-level attributes JSON"
        )


def test_demo_loadgen_sql_and_jsonl_emit_all_step1_columns(tmp_path: Path) -> None:
    """`demo_time_shift_loadgen.sql` & `export_time_shifted_jsonl` emit every column Step 1 selects."""
    wf_sql = _extract_view_sql_text(
        SQL_DIR / "core" / "step1_trace_views.sql", "agent_eval.v_step1_trace_waterfall"
    )
    wf_ast = _parse_bigquery_statements(wf_sql, "v_step1_trace_waterfall")[0]
    required_span_cols = {
        col.name
        for col in wf_ast.find_all(exp.Column)
        if (col.table or "").lower() == "s" and col.name
    }
    assert required_span_cols, "Failed to extract s.<col> references from v_step1_trace_waterfall"

    loadgen_sql = (SQL_DIR / "demo" / "demo_time_shift_loadgen.sql").read_text(encoding="utf-8")
    loadgen_stmts = _parse_bigquery_statements(loadgen_sql, "demo_time_shift_loadgen.sql")
    ctas_cols: set[str] = set()
    for stmt in loadgen_stmts:
        if isinstance(stmt, exp.Create) and stmt.expression is not None:
            target = stmt.this.sql(dialect="bigquery").replace("`", "").strip()
            if target == "agent_eval_seed.otel_spans":
                shifted_cte = next(
                    (c for c in stmt.find_all(exp.CTE) if c.alias_or_name == "shifted_spans"),
                    None,
                )
                assert shifted_cte is not None
                select_node = shifted_cte.this
                assert isinstance(select_node, exp.Select)
                for proj in select_node.expressions:
                    ctas_cols.add(proj.alias_or_name)

    missing_sql_cols = sorted(required_span_cols - ctas_cols)
    assert not missing_sql_cols, (
        f"demo_time_shift_loadgen.sql agent_eval_seed.otel_spans missing columns: {missing_sql_cols}"
    )

    spans, payloads, _ = generate_time_shifted_corpus(
        reference_time=datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC),
        total_replicas=1,
        last_hour_replicas=1,
    )
    spans_path, _ = export_time_shifted_jsonl(spans, payloads, tmp_path)
    first_row = json.loads(spans_path.read_text(encoding="utf-8").splitlines()[0])
    missing_jsonl_cols = sorted(required_span_cols - set(first_row))
    assert not missing_jsonl_cols, (
        f"export_time_shifted_jsonl missing columns: {missing_jsonl_cols}"
    )


def test_step2_stateless_sql_safe_deploy_and_reachable_error_tail() -> None:
    """`step2_stateless_eval.sql` has zero uncommented live-billing SELECTs and live error-tail views."""
    step2_text = (SQL_DIR / "core" / "step2_stateless_eval.sql").read_text(encoding="utf-8")
    stmts = _parse_bigquery_statements(step2_text, "step2_stateless_eval.sql")
    for stmt in stmts:
        if isinstance(stmt, exp.Select):
            raw = stmt.sql(dialect="bigquery", comments=False).upper()
            assert "AI.GENERATE_TABLE" not in raw, (
                "Uncommented AI.GENERATE_TABLE SELECT in sql/core/step2_stateless_eval.sql triggers "
                "accidental live billing when deploying the SQL file"
            )

    canary_err_sql = _extract_view_sql_text(
        SQL_DIR / "core" / "step2_stateless_eval.sql",
        "agent_eval.v_step2_5_example_last_1h_canary_and_errors",
    )
    assert "NOT step1_pass" in canary_err_sql
    canary_ast = _parse_bigquery_statements(canary_err_sql, "canary_and_errors")[0]
    source_views = _referenced_tables(canary_ast)
    for src in source_views:
        src_sql = _extract_view_sql_text(SQL_DIR / "core" / "step2_stateless_eval.sql", src)
        assert not re.search(r"WHERE\s+t\.step1_pass\s*=\s*TRUE", src_sql, re.IGNORECASE), (
            f"v_step2_5_example_last_1h_canary_and_errors filters NOT step1_pass on {src}, "
            "which already filters WHERE t.step1_pass = TRUE (dead error-tail branch)"
        )


def test_views_sql_rule0_and_pricing_catalog_parity() -> None:
    """`sql/core/views.sql` enforces Rule 0 tool limits, reads pricing burndown weights, and unifies judge fallbacks."""
    eval_view = _extract_view_sql_text(
        SQL_DIR / "core" / "views.sql", "agent_eval.v_trace_sql_evaluation"
    )
    assert "COUNTIF(w.span_kind = 'execute_tool') <= 8" in eval_view, (
        "v_trace_sql_evaluation must enforce <= 8 execute_tool spans in rule0_pass"
    )
    assert "COALESCE(w.tool_call_status, 'SUCCESS') != 'SUCCESS'" in eval_view

    wf_view = _extract_view_sql_text(SQL_DIR / "core" / "views.sql", "agent_eval.v_trace_waterfall")
    for catalog_col in (
        "m.long_context_threshold_tokens",
        "m.burndown_uncached_weight",
        "m.burndown_cached_weight",
        "m.burndown_output_weight",
        "m.burndown_long_uncached_weight",
        "m.burndown_long_cached_weight",
        "m.burndown_long_output_weight",
    ):
        assert catalog_col in wf_view, (
            f"v_trace_waterfall must read {catalog_col} from model_pricing_catalog"
        )

    views_text = (SQL_DIR / "core" / "views.sql").read_text(encoding="utf-8") + (
        SQL_DIR / "core" / "summary_views.sql"
    ).read_text(encoding="utf-8")
    assert "COALESCE(m.input_usd_per_1m, 1.25)" not in views_text, (
        "Hardcoded Argon fallback $1.25 conflicts with Flash fallback $0.15; dispatch by judge_model"
    )


def test_bq_dry_run_sqlglot_splitter_and_bqaa_chronological_app_id() -> None:
    """`split_sql_statements` handles block comments/double quotes; `v_bqaa_trace_payloads` matches Python chronological app ID."""
    tricky_sql = (
        "/* block comment with ; inside */\n"
        "SELECT \"col;semi\" AS c1, 'single;semi' AS c2 FROM `proj.ds.tbl`;\n"
        "-- line comment with ; inside\n"
        "CREATE OR REPLACE VIEW `proj.ds.v` AS SELECT 1 AS x;\n"
    )
    parts = split_sql_statements(tricky_sql)
    assert len(parts) == 2
    assert '"col;semi"' in parts[0]
    assert "CREATE OR REPLACE VIEW" in parts[1]

    bqaa_sql = (SQL_DIR / "core" / "bqaa_adapter_views.sql").read_text(encoding="utf-8")
    assert "IGNORE NULLS ORDER BY e.timestamp ASC, e.event_id ASC LIMIT 1" in bqaa_sql
    assert (
        "REGEXP_CONTAINS(LOWER(COALESCE(error_message, '')), r'timeout|timed\\s*out|503|504')"
        in bqaa_sql
    )

    t0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    events = [
        BQAAAgentEvent(
            event_id="ev-1-unreg",
            timestamp=t0,
            event_type="USER_MESSAGE_RECEIVED",
            agent="intake",
            invocation_id="inv-det-1",
            trace_id="trace-det-1",
            content={"text": "Hello"},
            attributes={},
        ),
        BQAAAgentEvent(
            event_id="ev-2-first-explicit",
            timestamp=t0 + timedelta(seconds=1),
            event_type="TOOL_COMPLETED",
            agent="intake",
            invocation_id="inv-det-1",
            trace_id="trace-det-1",
            content={"response": "ctx"},
            attributes={"custom_metadata": {"apphub.application.id": "alpha-chronological-first"}},
        ),
        BQAAAgentEvent(
            event_id="ev-3-second-explicit",
            timestamp=t0 + timedelta(seconds=2),
            event_type="AGENT_RESPONSE",
            agent="intake",
            invocation_id="inv-det-1",
            trace_id="trace-det-1",
            content={"text": "Done"},
            attributes={"custom_metadata": {"apphub.application.id": "zzz-later-app"}},
        ),
    ]
    py_payloads = bqaa_events_to_trace_payloads(events)
    assert len(py_payloads) == 1
    assert py_payloads[0].apphub_application_id == "alpha-chronological-first"

    payload_view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(
            SQL_DIR / "core" / "bqaa_adapter_views.sql", "agent_eval.v_bqaa_trace_payloads"
        )
    )
    conn = _new_sqlite_bq_conn()
    conn.execute(
        "CREATE TABLE agent_events (event_id TEXT, timestamp TEXT, event_type TEXT, "
        "invocation_id TEXT, trace_id TEXT, content TEXT, attributes TEXT)"
    )
    for ev in reversed(events):
        conn.execute(
            "INSERT INTO agent_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.event_id,
                ev.timestamp.isoformat(),
                ev.event_type,
                ev.invocation_id,
                ev.trace_id,
                json.dumps(ev.content),
                json.dumps(ev.attributes),
            ),
        )
    conn.execute(f"CREATE VIEW v_bqaa_trace_payloads AS {payload_view_sql}")
    sql_row = conn.execute("SELECT apphub_application_id FROM v_bqaa_trace_payloads").fetchone()
    conn.close()
    assert sql_row["apphub_application_id"] == "alpha-chronological-first"
