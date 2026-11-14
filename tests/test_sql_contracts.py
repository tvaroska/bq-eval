"""Executable BigQuery SQL contract, DAG, schema-parity, and multi-key fanout tests.

Validates every SQL file in `sql/` and every Python-generated BigQuery SQL query via
`sqlglot` AST inspection plus live SQLite execution of the transpiled CPVO and Skill
Lineage views so SQL regressions fail fast offline in `< 2s`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from bq_eval.loadgen import export_time_shifted_jsonl, generate_time_shifted_corpus
from bq_eval.sql_eval import build_ai_evaluate_agent_sql, build_ai_generate_table_judge_sql

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"

ALLOWED_BQ_PREVIEW_COMMANDS = (
    "CREATE EXTERNAL TABLE",
    "CREATE MODEL",
    "ALTER TABLE",
)


def _normalize_bq_ai_tvf_for_sqlglot(sql: str) -> str:
    """Normalize BigQuery preview `AI.*` TVF `MODEL`/`TABLE` keyword prefixes for AST parsing.

    Only rewrites `MODEL <ident>` and `TABLE <ident|(subquery)>` immediately inside
    `AI.GENERATE_TABLE(` or `AI.EVALUATE_AGENT(` calls so real SQL syntax errors elsewhere
    are never masked.
    """
    normalized = re.sub(
        r"(AI\.GENERATE_TABLE\s*\(\s*)MODEL\s+",
        r"\1",
        sql,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(AI\.(?:GENERATE_TABLE|EVALUATE_AGENT)\s*\([^;]*?,\s*)TABLE\s+",
        r"\1",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    normalized = re.sub(
        r"(AI\.EVALUATE_AGENT\s*\(\s*)TABLE\s+",
        r"\1",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized


def _skip_triple_quoted(sql: str, start: int, delim: str) -> int:
    i, n = start + 3, len(sql)
    while i < n:
        if sql[i] == "\\":
            i += 2
        elif sql[i : i + 3] == delim:
            return i + 3
        else:
            i += 1
    return n


def _scan_single_quoted(sql: str, start: int) -> tuple[int, bool]:
    i, n, has_newline = start + 1, len(sql), False
    while i < n:
        if sql[i] == "\\":
            i += 2
            continue
        if sql[i] == "\n":
            has_newline = True
        if sql[i] == "'":
            return i + 1, has_newline
        i += 1
    return n, has_newline


def _find_unescaped_multiline_single_quoted_strings(sql: str) -> list[str]:
    """Return snippets of invalid GoogleSQL single-quoted `'...'` literals containing raw newlines."""
    violations: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql[i : i + 2] == "--":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif sql[i : i + 2] == "/*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif sql[i : i + 3] in ("'''", '"""'):
            i = _skip_triple_quoted(sql, i, sql[i : i + 3])
        elif sql[i] == "'":
            end_i, has_newline = _scan_single_quoted(sql, i)
            if has_newline:
                violations.append(sql[i : min(end_i, i + 120)])
            i = end_i
        else:
            i += 1
    return violations


def _parse_bigquery_statements(sql: str, label: str) -> list[exp.Expr]:
    """Parse BigQuery SQL into `sqlglot` AST nodes, failing on any unexpected syntax fallback."""
    cleaned = _normalize_bq_ai_tvf_for_sqlglot(sql)
    stmts = [s for s in sqlglot.parse(cleaned, read="bigquery") if s is not None]
    assert stmts, f"{label} parsed to zero SQL statements"
    for stmt in stmts:
        if isinstance(stmt, exp.Command):
            raw = stmt.sql(dialect="bigquery", comments=False).strip().upper()
            assert any(raw.startswith(prefix) for prefix in ALLOWED_BQ_PREVIEW_COMMANDS), (
                f"{label} fell back to unparsed Command AST: {raw[:160]}"
            )
    return stmts


def _extract_view_definitions() -> dict[str, list[tuple[str, exp.Create]]]:
    """Map `dataset.view_name` -> list of `(filename, Create AST)` across `sql/*.sql`."""
    views: dict[str, list[tuple[str, exp.Create]]] = {}
    for sql_file in sorted(SQL_DIR.glob("*.sql")):
        stmts = _parse_bigquery_statements(sql_file.read_text(encoding="utf-8"), sql_file.name)
        for stmt in stmts:
            if isinstance(stmt, exp.Create) and (stmt.args.get("kind") or "").upper() == "VIEW":
                name = stmt.this.sql(dialect="bigquery").replace("`", "").strip()
                views.setdefault(name, []).append((sql_file.name, stmt))
    return views


def _extract_view_sql_text(sql_file: Path, view_name: str) -> str:
    """Extract the `CREATE OR REPLACE VIEW ... AS ...;` statement text for `view_name`."""
    text = sql_file.read_text(encoding="utf-8")
    pattern = rf"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?{re.escape(view_name)}`?\s+AS\s+.*?;"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    assert match is not None, f"View {view_name} not found in {sql_file.name}"
    return match.group(0)


def _referenced_tables(stmt: exp.Expr) -> set[str]:
    """Return qualified table/view identifiers referenced outside local CTE names."""
    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    refs: set[str] = set()
    query_body = stmt.expression if isinstance(stmt, exp.Create) else stmt
    if query_body is None:
        return refs
    for tbl in query_body.find_all(exp.Table):
        parts = [p.strip("`") for p in (tbl.catalog, tbl.db, tbl.name) if p]
        ident = ".".join(parts).strip()
        if not ident or ident.lower() in cte_names:
            continue
        if "." in ident:
            refs.add(ident)
    return refs


def test_all_sql_files_parse_as_bigquery_ast_and_valid_googlesql_strings() -> None:
    """Every `sql/*.sql` file parses cleanly as BigQuery AST with valid GoogleSQL strings."""
    sql_files = sorted(SQL_DIR.glob("*.sql"))
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
        assert "LEFT JOIN `demo-proj.agent_eval.trace_payloads`" in gen_sql, (
            "trace_payloads join must be LEFT JOIN so 0-token Path-A traces are preserved"
        )
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
        SQL_DIR / "step1_trace_views.sql", "agent_eval.v_step1_cost_per_outcome"
    )
    assert "avg_cache_hit_ratio" in step1_cpo_sql


def _find_graph_cycles(deps: dict[str, set[str]]) -> list[str]:
    in_degree = {name: len(refs) for name, refs in deps.items()}
    ready = sorted(name for name, deg in in_degree.items() if deg == 0)
    visited: set[str] = set()
    while ready:
        node = ready.pop(0)
        visited.add(node)
        for candidate, refs in deps.items():
            if node in refs:
                in_degree[candidate] -= 1
                if in_degree[candidate] == 0:
                    ready.append(candidate)
                    ready.sort()
    return sorted(set(deps) - visited)


def _extract_mode_c_views(bqaa_sql: str) -> dict[str, set[str]]:
    mode_c_marker = "-- Mode C:"
    assert mode_c_marker in bqaa_sql
    lines: list[str] = []
    started = False
    for line in bqaa_sql.split(mode_c_marker, 1)[1].splitlines():
        stripped = line.strip()
        candidate = (
            stripped[3:] if stripped.startswith("-- ") else ("" if stripped == "--" else stripped)
        )
        if not started and not candidate.strip().upper().startswith("CREATE "):
            continue
        started = True
        lines.append(candidate)
    mode_c_stmts = _parse_bigquery_statements("\n".join(lines), "bqaa_adapter_views.sql Mode C")
    return {
        stmt.this.sql(dialect="bigquery").replace("`", "").strip(): _referenced_tables(stmt)
        for stmt in mode_c_stmts
        if isinstance(stmt, exp.Create) and (stmt.args.get("kind") or "").upper() == "VIEW"
    }


def test_view_dependency_dag_is_acyclic_and_light_capture_deployable() -> None:
    """View graph has zero cycles, Light Capture deploys in one pass, and Mode C is acyclic."""
    views = _extract_view_definitions()
    view_names = set(views)
    deps = {
        name: {ref for _, stmt in defs for ref in (_referenced_tables(stmt) & view_names)}
        for name, defs in views.items()
    }
    cyclic = _find_graph_cycles(deps)
    assert not cyclic, f"View dependency cycle detected among: {cyclic}"

    bqaa_sql = (SQL_DIR / "bqaa_adapter_views.sql").read_text(encoding="utf-8")
    step1_views = {
        name for name, defs in views.items() if any(f == "step1_trace_views.sql" for f, _ in defs)
    }
    for stmt in _parse_bigquery_statements(bqaa_sql, "bqaa_adapter_views.sql"):
        cycle_refs = _referenced_tables(stmt) & step1_views
        assert not cycle_refs, (
            f"bqaa_adapter_views.sql depends on step1_trace_views.sql views {cycle_refs}, "
            "creating a deployment cycle on fresh Light Capture projects"
        )

    mode_c_views = _extract_mode_c_views(bqaa_sql)
    assert "agent_eval.otel_spans" in mode_c_views
    assert "agent_eval.trace_payloads" in mode_c_views
    for vname, refs in mode_c_views.items():
        assert vname not in refs, f"Mode C view {vname} directly references itself"
        mutual = [t for t in refs if t in mode_c_views and vname in mode_c_views[t]]
        assert not mutual, f"Mode C circular view cycle: {vname} <-> {mutual}"


def test_bqaa_sql_adapter_matches_python_custom_metadata_and_outcome_closed() -> None:
    """`v_bqaa_otel_spans` promotes `custom_metadata` keys and maps `OUTCOME_CLOSED` to `invoke_agent`."""
    view_sql = _extract_view_sql_text(
        SQL_DIR / "bqaa_adapter_views.sql", "agent_eval.v_bqaa_otel_spans"
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


def test_demo_loadgen_sql_and_jsonl_emit_all_step1_m1_columns(tmp_path: Path) -> None:
    """`demo_time_shift_loadgen.sql` & `export_time_shifted_jsonl` emit every column Step 1 selects."""
    wf_sql = _extract_view_sql_text(
        SQL_DIR / "step1_trace_views.sql", "agent_eval.v_step1_trace_waterfall"
    )
    wf_ast = _parse_bigquery_statements(wf_sql, "v_step1_trace_waterfall")[0]
    required_span_cols = {
        col.name
        for col in wf_ast.find_all(exp.Column)
        if (col.table or "").lower() == "s" and col.name
    }
    assert required_span_cols, "Failed to extract s.<col> references from v_step1_trace_waterfall"

    loadgen_sql = (SQL_DIR / "demo_time_shift_loadgen.sql").read_text(encoding="utf-8")
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
        f"demo_time_shift_loadgen.sql agent_eval_seed.otel_spans missing M1 columns: {missing_sql_cols}"
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
        f"export_time_shifted_jsonl missing M1 columns: {missing_jsonl_cols}"
    )


def test_step2_stateless_sql_safe_deploy_and_reachable_error_tail() -> None:
    """`step2_stateless_eval.sql` has zero uncommented live-billing SELECTs and live error-tail views."""
    step2_text = (SQL_DIR / "step2_stateless_eval.sql").read_text(encoding="utf-8")
    stmts = _parse_bigquery_statements(step2_text, "step2_stateless_eval.sql")
    for stmt in stmts:
        if isinstance(stmt, exp.Select):
            raw = stmt.sql(dialect="bigquery", comments=False).upper()
            assert "AI.GENERATE_TABLE" not in raw, (
                "Uncommented AI.GENERATE_TABLE SELECT in sql/step2_stateless_eval.sql triggers "
                "accidental live billing when deploying the SQL file"
            )

    canary_err_sql = _extract_view_sql_text(
        SQL_DIR / "step2_stateless_eval.sql",
        "agent_eval.v_step2_5_example_last_1h_canary_and_errors",
    )
    assert "NOT step1_pass" in canary_err_sql
    canary_ast = _parse_bigquery_statements(canary_err_sql, "canary_and_errors")[0]
    source_views = _referenced_tables(canary_ast)
    for src in source_views:
        src_sql = _extract_view_sql_text(SQL_DIR / "step2_stateless_eval.sql", src)
        assert not re.search(r"WHERE\s+t\.step1_pass\s*=\s*TRUE", src_sql, re.IGNORECASE), (
            f"v_step2_5_example_last_1h_canary_and_errors filters NOT step1_pass on {src}, "
            "which already filters WHERE t.step1_pass = TRUE (dead error-tail branch)"
        )


def test_views_sql_rule0_and_pricing_catalog_parity() -> None:
    """`sql/views.sql` enforces Rule 0 tool limits, reads pricing burndown weights, and unifies judge fallbacks."""
    eval_view = _extract_view_sql_text(SQL_DIR / "views.sql", "agent_eval.v_trace_sql_evaluation")
    assert "COUNTIF(w.span_kind = 'execute_tool') <= 8" in eval_view, (
        "v_trace_sql_evaluation must enforce <= 8 execute_tool spans in rule0_pass"
    )
    assert "COALESCE(w.tool_call_status, 'SUCCESS') != 'SUCCESS'" in eval_view

    wf_view = _extract_view_sql_text(SQL_DIR / "views.sql", "agent_eval.v_trace_waterfall")
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

    views_text = (SQL_DIR / "views.sql").read_text(encoding="utf-8")
    assert "COALESCE(m.input_usd_per_1m, 1.25)" not in views_text, (
        "Hardcoded Argon fallback $1.25 conflicts with Flash fallback $0.15; dispatch by judge_model"
    )


def _sqlite_json_value(raw_json: str | None, path: str) -> str | None:
    if not raw_json:
        return None
    data = json.loads(raw_json)
    cleaned = path.removeprefix("$.").replace('"', "")
    if cleaned in data:
        val = data[cleaned]
        return None if val is None else str(val)
    cur = data
    for part in cleaned.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return None if cur is None else str(cur)


_SQLITE_AGG_MAP: dict[str, type[exp.Expr]] = {
    "LOGICAL_OR": exp.Max,
    "LOGICAL_AND": exp.Min,
    "ANY_VALUE": exp.Max,
}


def _rewrite_bq_node_for_sqlite(node: exp.Expr) -> exp.Expr:
    if isinstance(node, exp.SafeDivide):
        num = node.this.copy().sql("sqlite")
        den = node.expression.copy().sql("sqlite")
        return sqlglot.parse_one(
            f"CASE WHEN ({den}) IS NULL OR ({den}) = 0 THEN NULL ELSE CAST(({num}) AS REAL) / ({den}) END",
            read="sqlite",
        )
    if isinstance(node, exp.TryCast):
        return exp.Cast(this=node.this, to=node.to)
    if isinstance(node, (exp.AnyValue, exp.LogicalOr)):
        return exp.Max(this=node.this)
    if isinstance(node, exp.LogicalAnd):
        return exp.Min(this=node.this)
    if isinstance(node, exp.Anonymous) and node.expressions:
        cls = _SQLITE_AGG_MAP.get(node.name.upper())
        if cls is not None:
            return cls(this=node.expressions[0])
    return node


def _transpile_bq_view_to_sqlite(view_sql: str) -> str:
    """Transpile a BigQuery view body into executable SQLite SQL for relational fanout testing."""
    body = re.sub(
        r"^CREATE\s+OR\s+REPLACE\s+VIEW\s+`?[^`\s]+`?\s+AS\s+",
        "",
        view_sql.strip(),
        flags=re.IGNORECASE,
    ).rstrip(";")
    body = body.replace("`agent_eval.", "`").replace("agent_eval.", "")
    ast_node = sqlglot.parse_one(body, read="bigquery")
    return ast_node.transform(_rewrite_bq_node_for_sqlite).sql(dialect="sqlite")


def test_multi_key_outcome_fanout_exact_1x_infra_and_judge_cost() -> None:
    """Multi-agent, multi-source_mode, multi-app & multi-skill slices sum to exact 1.0x outcome cost."""
    step1_path = SQL_DIR / "step1_trace_views.sql"
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

    conn = sqlite3.connect(":memory:")
    conn.create_function("JSON_VALUE", 2, _sqlite_json_value)
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
