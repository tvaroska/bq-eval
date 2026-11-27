"""Silver + Gold SQL pack parses as BigQuery AST, has zero duplicate views, deploys acyclically, and stays in sync."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlglot import exp

from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
)
from bq_eval.demo.loadgen import export_time_shifted_jsonl, generate_time_shifted_corpus
from bq_eval.deploy.engine.bootstrap import validate_bootstrap_offline
from scripts.bq_dry_run import split_sql_statements
from tests.conftest import SQL_DIR, parse_bigquery_sql

_VIEW_DEF_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?([a-zA-Z0-9_${}._-]+)`?\s+AS",
    re.IGNORECASE,
)


_SQL_TOKEN_RE = re.compile(
    r"--[^\n]*|#[^\n]*|/\*[\s\S]*?\*/|'''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\""
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
)


def _find_unescaped_multiline_single_quoted_strings(sql: str) -> list[str]:
    return [
        m.group(0)[1:-1][:80]
        for m in _SQL_TOKEN_RE.finditer(sql)
        if m.group(0).startswith("'") and not m.group(0).startswith("'''") and "\n" in m.group(0)
    ]


def _extract_view_definitions() -> dict[str, list[tuple[str, str]]]:
    defs: dict[str, list[tuple[str, str]]] = {}
    for sql_file in sorted(SQL_DIR.rglob("*.sql")):
        rel = sql_file.relative_to(SQL_DIR).as_posix()
        for stmt in split_sql_statements(sql_file.read_text(encoding="utf-8")):
            m = _VIEW_DEF_RE.search(stmt)
            if m is not None:
                raw_name = m.group(1)
                norm = raw_name.replace("${PROJECT_ID}.", "").replace(
                    "${SILVER_OPEN_DATASET}", "silver_open"
                )
                defs.setdefault(norm, []).append((rel, stmt))
    return defs


def _referenced_views(stmt_sql: str, known_views: set[str]) -> set[str]:
    found: set[str] = set()
    for node in parse_bigquery_sql(stmt_sql, "view_deps"):
        for tbl in node.find_all(exp.Table):
            parts = [p for p in (tbl.db, tbl.name) if p]
            short = ".".join(parts[-2:]) if len(parts) >= 2 else tbl.name
            if short in known_views:
                found.add(short)
    return found


def test_all_sql_files_parse_as_bigquery_ast_and_valid_googlesql_strings() -> None:
    """Every `sql/**/*.sql` file parses cleanly as BigQuery AST with valid GoogleSQL strings."""
    sql_files = sorted(SQL_DIR.rglob("*.sql"))
    assert len(sql_files) >= 24
    for sql_file in sql_files:
        text = sql_file.read_text(encoding="utf-8")
        bad_strings = _find_unescaped_multiline_single_quoted_strings(text)
        assert not bad_strings, (
            f"{sql_file.name} has raw newlines in single-quoted SQL: {bad_strings}"
        )
        parse_bigquery_sql(text, sql_file.name)


def test_generated_judge_sql_valid_googlesql_and_contract() -> None:
    """`build_ai_generate_table_judge_sql` & `build_ai_evaluate_agent_sql` emit valid GoogleSQL on Silver."""
    for run_id in (None, "m2-live-20261113b"):
        gen_sql = build_ai_generate_table_judge_sql(
            project_id="demo-proj",
            dataset_id="agent_eval",
            rubric_id="groundedness_4pt_v1",
            canary_sample_pct=5,
            run_id=run_id,
        )
        assert not _find_unescaped_multiline_single_quoted_strings(gen_sql)
        parse_bigquery_sql(gen_sql, f"build_ai_generate_table_judge_sql(run_id={run_id})")
        assert (
            "'reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL' AS output_schema"
            in gen_sql
        )
        assert "INNER JOIN deduped_payloads" in gen_sql
        assert "COALESCE(status, '') = ''" in gen_sql
        assert "ordinal_score BETWEEN 1 AND 4" in gen_sql
        for col in (
            "judge_input_tokens",
            "judge_output_tokens",
            "judge_thinking_tokens",
            "judge_model_snapshot_id",
            "judge_prompt_sha256",
            "seed",
        ):
            assert col in gen_sql

    eval_sql = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        dataset_id="agent_eval",
        canary_sample_pct=10,
        run_id="m2-live-20261113b",
    )
    assert not _find_unescaped_multiline_single_quoted_strings(eval_sql)
    assert "v_eval_agent_turns" in eval_sql
    assert "v_bqaa_" not in eval_sql
    parse_bigquery_sql(eval_sql, "build_ai_evaluate_agent_sql")

    for bad_ident in ("bad; DROP TABLE x--", "proj`x", ""):
        with pytest.raises(ValueError, match="Invalid"):
            build_ai_generate_table_judge_sql(project_id=bad_ident)
        with pytest.raises(ValueError, match="Invalid"):
            build_ai_evaluate_agent_sql(project_id="demo-proj", eval_view=bad_ident)


def test_zero_duplicate_view_definitions_and_acyclic_silver_gold_dag() -> None:
    """No two `sql/**/*.sql` files redefine a view, Gold views are acyclic, and `bootstrap --mode gold` validates."""
    views = _extract_view_definitions()
    duplicates = {
        name: [fname for fname, _ in defs] for name, defs in views.items() if len(defs) > 1
    }
    assert not duplicates, f"Duplicate view definitions across sql/**/*.sql: {duplicates}"

    required_views = {
        "agent_eval.v_default_model_pricing",
        "agent_eval.v_trace_waterfall",
        "agent_eval.v_bq_query_cost_attribution",
        "agent_eval.v_infra_cost_estimate",
        "agent_eval.v_attribution_coverage",
        "agent_eval.v_counterparty_spend",
        "agent_eval.v_orphan_committed_actions",
        "agent_eval.v_trace_sql_evaluation",
        "agent_eval.v_step1_trace_eval",
        "agent_eval.v_eval_agent_turns",
        "agent_eval.v_gold_worm_snapshot_candidates",
        "agent_eval.v_optimizer_gold_export",
        "agent_eval.v_outcomes",
        "agent_eval.v_step1_cost_per_outcome",
        "agent_eval.v_cost_per_verified_outcome",
        "agent_eval.v_skill_lineage_roi",
        "agent_eval.v_enterprise_cost_per_verified_outcome",
        "agent_eval.v_agent_eval_and_finops_summary",
        "agent_eval.v_capacity_hourly_burndown_export",
        "agent_eval.v_inference_request_reconciliation",
        "agent_eval.v_app_risk_governance",
        "agent_eval.v_focus_three_pool_hourly_chargeback",
        "agent_eval.v_interaction_session_lineage",
        "agent_eval.v_nudge_feedback_summary",
        "agent_eval.v_live_slo_and_429_breaches",
        "agent_eval.v_lob_model_lifecycle_export",
        "silver_open.v_requests_24h",
    }
    missing = required_views - set(views)
    assert not missing, f"Missing required canonical Silver/Gold views: {sorted(missing)}"

    known = set(views)
    deps = {
        name: {ref for _, stmt in defs for ref in _referenced_views(stmt, known) if ref != name}
        for name, defs in views.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(node: str) -> None:
        assert node not in visiting, f"Cycle detected at {node}"
        if node in visited:
            return
        visiting.add(node)
        for nxt in deps.get(node, ()):
            _visit(nxt)
        visiting.remove(node)
        visited.add(node)

    for vname in sorted(deps):
        _visit(vname)

    report, _ = validate_bootstrap_offline(mode="gold", project_id="showcase-project")
    assert report.passed, f"Offline Gold bootstrap validation failed: {report.errors}"


def test_demo_loadgen_sql_and_jsonl_emit_bronze_and_wire_columns(tmp_path: Path) -> None:
    """`demo_time_shift_loadgen.sql` & `export_time_shifted_jsonl` emit Bronze `_AllSpans`, `app_registry`, and wire JSONL."""
    loadgen_sql = (SQL_DIR / "demo" / "demo_time_shift_loadgen.sql").read_text(encoding="utf-8")
    loadgen_stmts = parse_bigquery_sql(loadgen_sql, "demo_time_shift_loadgen.sql")
    created_tables = {
        (stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this)
        .sql(dialect="bigquery")
        .replace("`", "")
        .removeprefix("showcase-project.")
        .strip()
        for stmt in loadgen_stmts
        if isinstance(stmt, exp.Create) and stmt.this is not None
    }
    assert {
        "agent_eval_seed.app_registry",
        "agent_eval_seed.workflow_events",
        "agent_eval_seed._AllSpans",
    }.issubset(created_tables)

    spans, payloads, _ = generate_time_shifted_corpus(
        reference_time=datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC),
        total_replicas=1,
        last_hour_replicas=1,
    )
    spans_path, payloads_path = export_time_shifted_jsonl(spans, payloads, tmp_path)
    assert spans_path.is_file() and payloads_path.is_file()
    bronze_path = tmp_path / "obs_traces_all_spans.jsonl"
    reg_path = tmp_path / "app_registry.jsonl"
    wf_path = tmp_path / "workflow_events.jsonl"
    assert bronze_path.is_file() and reg_path.is_file() and wf_path.is_file()

    first_bronze = json.loads(bronze_path.read_text(encoding="utf-8").splitlines()[0])
    for required_col in ("trace_id", "span_id", "start_time", "end_time", "attributes", "resource"):
        assert required_col in first_bronze


def test_step2_stateless_sql_safe_deploy_and_reachable_error_tail() -> None:
    """`step2_stateless_eval.sql` has zero uncommented live-billing SELECTs and reachable error-tail branch."""
    step2_text = (SQL_DIR / "core" / "step2_stateless_eval.sql").read_text(encoding="utf-8")
    stmts = parse_bigquery_sql(step2_text, "step2_stateless_eval.sql")
    for stmt in stmts:
        if isinstance(stmt, exp.Select):
            raw = stmt.sql(dialect="bigquery", comments=False).upper()
            assert "AI.GENERATE_TABLE" not in raw

    views = _extract_view_definitions()
    canary_err_sql = views["agent_eval.v_step2_5_example_last_1h_canary_and_errors"][0][1]
    assert "NOT step1_pass" in canary_err_sql
    assert "v_step2_all_trace_rubric_prompts" in canary_err_sql
    all_prompts_sql = views["agent_eval.v_step2_all_trace_rubric_prompts"][0][1]
    assert not re.search(r"WHERE\s+.*step1_pass\s*=\s*TRUE", all_prompts_sql, re.IGNORECASE)


def test_gold_cost_views_strict_open_isolation_and_splitter() -> None:
    """`gold_cost_views.sql` & `capacity_views.sql` never touch restricted/Bronze tables; splitter handles comments."""
    for rel in ("core/gold_cost_views.sql", "finops/capacity_views.sql"):
        sql_text = (SQL_DIR / rel).read_text(encoding="utf-8")
        for forbidden in (
            "silver_restricted",
            "silver_internal",
            "shape3_payloads",
            "_AllSpans",
            "_AllLogs",
        ):
            sql_body = "\n".join(
                line for line in sql_text.splitlines() if not line.lstrip().startswith("--")
            )
            assert forbidden not in sql_body, (
                f"{rel} leaked {forbidden!r} across open-only boundary"
            )

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
