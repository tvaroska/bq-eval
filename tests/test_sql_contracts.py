"""Executable BigQuery SQL contract, DAG, schema-parity, and multi-key fanout tests.

Validates every SQL file in `sql/` and every Python-generated BigQuery SQL query via
`sqlglot` AST inspection plus live SQLite execution of the transpiled CPVO and Skill
Lineage views so SQL regressions fail fast offline in `< 2s`.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from bq_eval.core.contract.otel import (
    BQAAAgentEvent,
    FinOpsTrafficType,
    GenAISpanKind,
    OTelGenAISpanRecord,
)
from bq_eval.core.contract.pricing import DEFAULT_MODEL_SKUS
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.bqaa import bqaa_events_to_trace_payloads
from bq_eval.core.engine.calibration import rogan_gladen_calibrate
from bq_eval.core.engine.cost import compute_span_cost_usd
from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
    evaluate_traces_in_sqlite,
)
from bq_eval.demo.loadgen import export_time_shifted_jsonl, generate_time_shifted_corpus
from scripts.bq_dry_run import split_sql_statements

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"

ALLOWED_BQ_PREVIEW_COMMANDS = (
    "CREATE EXTERNAL TABLE",
    "CREATE MODEL",
    "ALTER TABLE",
    "CREATE OR REPLACE PROCEDURE",
    "CREATE PROCEDURE",
)

_PROC_INNER_SQL_RE = re.compile(
    r"(?is)\b(CREATE\s+TEMP\s+TABLE\s+.+?;|INSERT\s+INTO\s+.+?;|UPDATE\s+`?[\w.]+`?\s+SET\s+.+?;)"
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


def _validate_procedure_inner_sql(raw_proc_sql: str, label: str) -> None:
    """Parse DML and temp-table SQL statements inside a BigQuery stored procedure body."""
    inner_stmts = _PROC_INNER_SQL_RE.findall(raw_proc_sql)
    assert inner_stmts, f"{label} procedure contained no parseable inner SQL statements"
    for idx, inner in enumerate(inner_stmts):
        parsed = [
            s
            for s in sqlglot.parse(_normalize_bq_ai_tvf_for_sqlglot(inner), read="bigquery")
            if s is not None
        ]
        assert parsed, f"{label} procedure inner SQL #{idx} parsed to zero statements"
        for node in parsed:
            assert not isinstance(node, exp.Command), (
                f"{label} procedure inner SQL #{idx} fell back to Command AST: {inner[:160]}"
            )


def _parse_bigquery_statements(sql: str, label: str) -> list[exp.Expr]:
    """Parse BigQuery SQL into `sqlglot` AST nodes, failing on any unexpected syntax fallback."""
    cleaned = _normalize_bq_ai_tvf_for_sqlglot(sql)
    chunks = split_sql_statements(cleaned)
    assert chunks, f"{label} parsed to zero SQL statements"
    stmts: list[exp.Expr] = []
    for chunk in chunks:
        upper = chunk.lstrip().upper()
        if upper.startswith(("CREATE OR REPLACE PROCEDURE", "CREATE PROCEDURE")):
            _validate_procedure_inner_sql(chunk, label)
            stmts.append(exp.Command(this=chunk))
            continue
        parsed = [s for s in sqlglot.parse(chunk, read="bigquery") if s is not None]
        assert parsed, f"{label} chunk parsed to zero SQL statements: {chunk[:120]}"
        for stmt in parsed:
            if isinstance(stmt, exp.Command):
                raw = stmt.sql(dialect="bigquery", comments=False).strip().upper()
                assert any(raw.startswith(prefix) for prefix in ALLOWED_BQ_PREVIEW_COMMANDS), (
                    f"{label} fell back to unparsed Command AST: {raw[:160]}"
                )
            stmts.append(stmt)
    return stmts


def _extract_view_definitions() -> dict[str, list[tuple[str, exp.Create]]]:
    """Map `dataset.view_name` -> list of `(filename, Create AST)` across `sql/*.sql`."""
    views: dict[str, list[tuple[str, exp.Create]]] = {}
    for sql_file in sorted(SQL_DIR.rglob("*.sql")):
        stmts = _parse_bigquery_statements(sql_file.read_text(encoding="utf-8"), sql_file.name)
        for stmt in stmts:
            if isinstance(stmt, exp.Create) and (stmt.args.get("kind") or "").upper() == "VIEW":
                name = stmt.this.sql(dialect="bigquery").replace("`", "").strip()
                views.setdefault(name, []).append((sql_file.name, stmt))
    return views


def _extract_view_sql_text(sql_file: Path, view_name: str) -> str:
    """Extract the `CREATE OR REPLACE VIEW ... AS ...;` statement text for `view_name`."""
    text = sql_file.read_text(encoding="utf-8")
    header_re = re.compile(
        rf"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?{re.escape(view_name)}`?\s+AS\b",
        flags=re.IGNORECASE,
    )
    match = header_re.search(text)
    assert match is not None, f"View {view_name} not found in {sql_file.name}"
    start = match.start()
    i, n = match.end(), len(text)
    while i < n:
        if text[i : i + 2] == "--":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif text[i : i + 2] == "/*":
            end_c = text.find("*/", i + 2)
            i = n if end_c == -1 else end_c + 2
        elif text[i] == "'":
            i, _ = _scan_single_quoted(text, i)
        elif text[i] == ";":
            return text[start : i + 1]
        else:
            i += 1
    raise AssertionError(f"Unterminated view {view_name} in {sql_file.name}")


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
        SQL_DIR / "core" / "step1_trace_views.sql", "agent_eval.v_step1_cost_per_outcome"
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
    stmts = _parse_bigquery_statements(bqaa_sql, "bqaa_adapter_views.sql")
    return {
        stmt.this.sql(dialect="bigquery").replace("`", "").strip(): _referenced_tables(stmt)
        for stmt in stmts
        if isinstance(stmt, exp.Create)
        and (stmt.args.get("kind") or "").upper() == "VIEW"
        and "v_mode_" in stmt.this.sql(dialect="bigquery")
    }


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

    views_text = (SQL_DIR / "core" / "views.sql").read_text(encoding="utf-8")
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


def _sqlite_regexp_replace(value: str | None, pattern: str, replacement: str) -> str | None:
    if value is None:
        return None
    return re.sub(pattern, replacement, value)


def _sqlite_timestamp_diff_ms(later: str | None, earlier: str | None) -> float | None:
    if later is None or earlier is None:
        return None
    dt_later = datetime.fromisoformat(later.replace("Z", "+00:00"))
    dt_earlier = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
    return (dt_later - dt_earlier).total_seconds() * 1000.0


def _sqlite_format_timestamp(fmt: str, ts: str | None) -> str | None:
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.strftime(fmt)


def _sqlite_timestamp_trunc_hour(ts: str | None) -> str | None:
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(
        minute=0, second=0, microsecond=0
    )
    return dt.isoformat()


def _sqlite_timestamp_sub_ms(ts: str | None, ms: float | int | None) -> str | None:
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    delta_ms = int(ms or 0)
    return (dt - timedelta(milliseconds=delta_ms)).isoformat()


def _sqlite_unix_seconds(ts: str | None) -> int | None:
    if ts is None:
        return None
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def _sqlite_timestamp_seconds(sec: int | float | None) -> str | None:
    if sec is None:
        return None
    return datetime.fromtimestamp(int(sec), tz=UTC).isoformat()


def _sqlite_int_div(num: int | float | None, den: int | float | None) -> int | None:
    if num is None or den is None or den == 0:
        return None
    return int(num) // int(den)


def _sqlite_regexp_contains(value: str | None, pattern: str | None) -> int:
    if value is None or pattern is None:
        return 0
    return 1 if re.search(pattern, value) is not None else 0


def _sqlite_regexp_extract(value: str | None, pattern: str | None) -> str | None:
    if value is None or pattern is None:
        return None
    m = re.search(pattern, value)
    if m is None:
        return None
    return m.group(1) if m.lastindex else m.group(0)


def _sqlite_starts_with(value: str | None, prefix: str | None) -> int:
    if value is None or prefix is None:
        return 0
    return 1 if str(value).startswith(str(prefix)) else 0


def _sqlite_json_set(raw_doc: str | None, *args: object) -> str:
    doc: dict[str, object]
    try:
        parsed = json.loads(raw_doc) if raw_doc else {}
        doc = parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        doc = {}
    for idx in range(0, len(args) - 1, 2):
        path = str(args[idx])
        val = args[idx + 1]
        if val is None:
            continue
        key = path.removeprefix("$.").strip('"')
        doc[key] = val
    return json.dumps(doc, sort_keys=True)


SqliteValue = str | int | float | bytes | None


class _SQLiteQuantileAgg:
    """Exact BigQuery `APPROX_QUANTILES(val, buckets)[OFFSET(offset)]` aggregate for SQLite tests."""

    def __init__(self) -> None:
        self.values: list[float] = []
        self.buckets = 100
        self.offset = 95

    def step(self, *args: object) -> None:
        value, buckets, offset = args
        if isinstance(value, (int, float)):
            self.values.append(float(value))
        elif value is not None:
            self.values.append(float(str(value)))
        self.buckets = int(str(buckets)) if buckets is not None else 100
        self.offset = int(str(offset)) if offset is not None else 95

    def finalize(self) -> float | None:
        if not self.values:
            return None
        ordered = sorted(self.values)
        n = len(ordered)
        idx = min(n - 1, max(0, math.ceil((self.offset / self.buckets) * n) - 1))
        return ordered[idx]


class _SQLiteArrayFirstAgg:
    """Faithful SQLite aggregate for `ARRAY_AGG(val [IGNORE NULLS] ORDER BY k1 [ASC|DESC], ... LIMIT 1)[SAFE_OFFSET(0)]`."""

    def __init__(self) -> None:
        self.best_key: tuple[str, str] | None = None
        self.best_val: SqliteValue = None
        self.has_val = False
        self.descending = False

    def step(self, *args: object) -> None:
        val, k1, k2, ignore_nulls, descending = args
        if ignore_nulls and val is None:
            return
        self.descending = bool(descending)
        key = ("" if k1 is None else str(k1), "" if k2 is None else str(k2))
        typed_val: SqliteValue = (
            val if isinstance(val, (str, int, float, bytes)) or val is None else str(val)
        )
        if not self.has_val or self.best_key is None:
            self.best_key = key
            self.best_val = typed_val
            self.has_val = True
        elif (self.descending and key > self.best_key) or (
            not self.descending and key < self.best_key
        ):
            self.best_key = key
            self.best_val = typed_val

    def finalize(self) -> SqliteValue:
        return self.best_val if self.has_val else None


class _SQLiteStringAggAsc:
    """Faithful SQLite aggregate for `STRING_AGG(val, sep ORDER BY k ASC)`."""

    def __init__(self) -> None:
        self.items: list[tuple[str, int, str]] = []
        self.sep = ","

    def step(self, *args: object) -> None:
        val, sep, order_key = args
        self.sep = str(sep)
        if val is not None:
            self.items.append(
                ("" if order_key is None else str(order_key), len(self.items), str(val))
            )

    def finalize(self) -> str | None:
        if not self.items:
            return None
        self.items.sort(key=lambda item: (item[0], item[1]))
        return self.sep.join(item[2] for item in self.items)


def _new_sqlite_bq_conn(current_date_utc: str = "2026-11-21") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("JSON_VALUE", 2, _sqlite_json_value)
    conn.create_function("REGEXP_REPLACE", 3, _sqlite_regexp_replace)
    conn.create_function("BQ_REGEXP_CONTAINS", 2, _sqlite_regexp_contains)
    conn.create_function("BQ_REGEXP_EXTRACT", 2, _sqlite_regexp_extract)
    conn.create_function("BQ_STARTS_WITH", 2, _sqlite_starts_with)
    conn.create_function("BQ_JSON_SET", -1, _sqlite_json_set)
    conn.create_function("SQRT", 1, math.sqrt)
    conn.create_function("POW", 2, math.pow)
    conn.create_function("CEIL", 1, math.ceil)
    conn.create_function("TIMESTAMP_DIFF_MS", 2, _sqlite_timestamp_diff_ms)
    conn.create_function("BQ_TIMESTAMP_SUB_MS", 2, _sqlite_timestamp_sub_ms)
    conn.create_function("BQ_UNIX_SECONDS", 1, _sqlite_unix_seconds)
    conn.create_function("BQ_TIMESTAMP_SECONDS", 1, _sqlite_timestamp_seconds)
    conn.create_function("BQ_INT_DIV", 2, _sqlite_int_div)
    conn.create_function("FORMAT_TIMESTAMP", 2, _sqlite_format_timestamp)
    conn.create_function("BQ_TIMESTAMP_TRUNC_HOUR", 1, _sqlite_timestamp_trunc_hour)
    conn.create_function("BQ_CURRENT_DATE", 0, lambda: current_date_utc)
    conn.create_function("BQ_DATE", 1, lambda ts: str(ts)[:10] if ts is not None else None)
    conn.create_function("JSON_FORMAT", 1, lambda v: None if v is None else str(v))
    conn.create_function("TO_JSON_STRING", 1, lambda v: None if v is None else str(v))
    # typeshed's sqlite3._AggregateProtocol models only 1-arg step(); these aggregates take 3-5 SQL args.
    conn.create_aggregate("BQ_APPROX_QUANTILE", 3, _SQLiteQuantileAgg)  # type: ignore[arg-type]
    conn.create_aggregate("BQ_ARRAY_FIRST", 5, _SQLiteArrayFirstAgg)  # type: ignore[arg-type]
    conn.create_aggregate("BQ_STRING_AGG_ASC", 3, _SQLiteStringAggAsc)  # type: ignore[arg-type]
    return conn


_SQLITE_AGG_MAP: dict[str, type[exp.Expr]] = {
    "LOGICAL_OR": exp.Max,
    "LOGICAL_AND": exp.Min,
    "ANY_VALUE": exp.Max,
}


def _extract_bracket_offset(node: exp.Bracket, default: int) -> int:
    if not node.expressions:
        return default
    expr = node.expressions[0]
    if isinstance(expr, exp.Literal) and expr.is_int:
        return int(expr.this)
    if isinstance(expr, (exp.Anonymous, exp.Func)):
        inner = expr.expressions[0] if expr.expressions else expr.this
        if isinstance(inner, exp.Literal) and inner.is_int:
            return int(inner.this)
    return default


def _rewrite_bq_bracket(node: exp.Bracket) -> exp.Expr:
    target = node.this
    if isinstance(target, exp.ApproxQuantiles):
        buckets = (
            target.expression.copy() if target.expression is not None else exp.Literal.number(100)
        )
        offset = _extract_bracket_offset(node, default=95)
        return exp.Anonymous(
            this="BQ_APPROX_QUANTILE",
            expressions=[target.this.copy(), buckets, exp.Literal.number(offset)],
        )
    ignore_nulls = False
    if isinstance(target, exp.IgnoreNulls):
        ignore_nulls = True
        target = target.this
    if isinstance(target, exp.ArrayAgg):
        inner = target.this.copy()
        order_node: exp.Order | None = None
        while isinstance(inner, (exp.Limit, exp.Order, exp.IgnoreNulls)):
            if isinstance(inner, exp.IgnoreNulls):
                ignore_nulls = True
            elif isinstance(inner, exp.Order):
                order_node = inner
            inner = inner.this
        k1: exp.Expr = exp.Null()
        k2: exp.Expr = exp.Null()
        descending = False
        if order_node is not None and order_node.expressions:
            first_ord = order_node.expressions[0]
            k1 = first_ord.this.copy()
            descending = bool(first_ord.args.get("desc"))
            if len(order_node.expressions) >= 2:
                k2 = order_node.expressions[1].this.copy()
        return exp.Anonymous(
            this="BQ_ARRAY_FIRST",
            expressions=[
                inner,
                k1,
                k2,
                exp.Literal.number(1 if ignore_nulls else 0),
                exp.Literal.number(1 if descending else 0),
            ],
        )
    return node


def _rewrite_bq_qualify(node: exp.Select) -> exp.Expr:
    qualify_cond = node.args["qualify"].this.copy()
    if not (isinstance(qualify_cond, exp.EQ) and isinstance(qualify_cond.this, exp.Window)):
        return node
    inner = node.copy()
    inner.set("qualify", None)
    inner.expressions.append(exp.alias_(qualify_cond.this.copy(), "_qualify_rn"))
    return (
        exp.select("*")
        .from_(inner.subquery("_q"))
        .where(exp.EQ(this=exp.column("_qualify_rn"), expression=qualify_cond.expression.copy()))
    )


def _rewrite_bq_temporal(node: exp.Expr) -> exp.Expr | None:
    rewritten: exp.Expr | None = None
    if isinstance(node, exp.TimestampTrunc):
        rewritten = exp.Anonymous(this="BQ_TIMESTAMP_TRUNC_HOUR", expressions=[node.this.copy()])
    elif isinstance(node, exp.TimestampDiff):
        rewritten = exp.Anonymous(
            this="TIMESTAMP_DIFF_MS",
            expressions=[node.this.copy(), node.expression.copy()],
        )
    elif isinstance(node, exp.TimestampSub):
        rewritten = exp.Anonymous(
            this="BQ_TIMESTAMP_SUB_MS",
            expressions=[node.this.copy(), node.expression.copy()],
        )
    elif isinstance(node, exp.CurrentDate):
        rewritten = exp.Anonymous(this="BQ_CURRENT_DATE", expressions=[])
    elif isinstance(node, exp.Date):
        rewritten = exp.Anonymous(this="BQ_DATE", expressions=[node.this.copy()])
    elif isinstance(node, exp.TimeToStr):
        rewritten = exp.Anonymous(
            this="FORMAT_TIMESTAMP",
            expressions=[node.args["format"].copy(), node.this.copy()],
        )
    elif isinstance(node, exp.UnixToTime):
        rewritten = exp.Anonymous(this="BQ_TIMESTAMP_SECONDS", expressions=[node.this.copy()])
    elif isinstance(node, (exp.TimeToUnix, exp.UnixSeconds)):
        rewritten = exp.Anonymous(this="BQ_UNIX_SECONDS", expressions=[node.this.copy()])
    elif isinstance(node, exp.IntDiv):
        rewritten = exp.Anonymous(
            this="BQ_INT_DIV",
            expressions=[node.this.copy(), node.expression.copy()],
        )
    return rewritten


def _rewrite_bq_node_for_sqlite(node: exp.Expr) -> exp.Expr:  # noqa: C901, PLR0912
    result: exp.Expr = node
    if isinstance(node, exp.SafeDivide):
        num = node.this.copy().sql("sqlite")
        den = node.expression.copy().sql("sqlite")
        result = sqlglot.parse_one(
            f"CASE WHEN ({den}) IS NULL OR ({den}) = 0 THEN NULL ELSE CAST(({num}) AS REAL) / ({den}) END",
            read="sqlite",
        )
    elif isinstance(node, exp.TryCast):
        result = exp.Cast(this=node.this, to=node.to)
    elif (
        isinstance(node, exp.Cast)
        and isinstance(node.to, exp.DataType)
        and node.to.is_type(exp.DataType.Type.JSON)
    ):
        result = node.this.copy()
    elif isinstance(node, (exp.AnyValue, exp.LogicalOr)):
        result = exp.Max(this=node.this)
    elif isinstance(node, exp.LogicalAnd):
        result = exp.Min(this=node.this)
    elif isinstance(node, exp.Bracket):
        result = _rewrite_bq_bracket(node)
    elif isinstance(node, exp.GroupConcat):
        inner = node.this.copy()
        sep = node.args.get("separator") or exp.Literal.string(",")
        if isinstance(inner, exp.Order) and inner.expressions:
            order_key = inner.expressions[0].this.copy()
            result = exp.Anonymous(
                this="BQ_STRING_AGG_ASC",
                expressions=[inner.this.copy(), sep.copy(), order_key],
            )
    elif isinstance(node, exp.RegexpLike):
        result = exp.Anonymous(
            this="BQ_REGEXP_CONTAINS",
            expressions=[node.this.copy(), node.expression.copy()],
        )
    elif isinstance(node, exp.RegexpExtract):
        result = exp.Anonymous(
            this="BQ_REGEXP_EXTRACT",
            expressions=[node.this.copy(), node.expression.copy()],
        )
    elif isinstance(node, exp.StartsWith):
        result = exp.Anonymous(
            this="BQ_STARTS_WITH",
            expressions=[node.this.copy(), node.expression.copy()],
        )
    elif isinstance(node, exp.Select) and node.args.get("qualify") is not None:
        result = _rewrite_bq_qualify(node)
    elif (temporal := _rewrite_bq_temporal(node)) is not None:
        result = temporal
    elif isinstance(node, exp.Anonymous) and node.expressions:
        upper_name = node.name.upper()
        if upper_name == "JSON_SET":
            result = exp.Anonymous(
                this="BQ_JSON_SET",
                expressions=[e.copy() for e in node.expressions],
            )
        elif upper_name == "STARTS_WITH":
            result = exp.Anonymous(
                this="BQ_STARTS_WITH",
                expressions=[e.copy() for e in node.expressions],
            )
        elif upper_name == "UNIX_SECONDS":
            result = exp.Anonymous(
                this="BQ_UNIX_SECONDS",
                expressions=[e.copy() for e in node.expressions],
            )
        elif upper_name == "TIMESTAMP_SECONDS":
            result = exp.Anonymous(
                this="BQ_TIMESTAMP_SECONDS",
                expressions=[e.copy() for e in node.expressions],
            )
        elif upper_name == "DIV":
            result = exp.Anonymous(
                this="BQ_INT_DIV",
                expressions=[e.copy() for e in node.expressions],
            )
        else:
            cls = _SQLITE_AGG_MAP.get(upper_name)
            if cls is not None:
                result = cls(this=node.expressions[0])
    return result


def _transpile_bq_view_to_sqlite(view_sql: str) -> str:
    """Transpile a BigQuery view body into executable SQLite SQL for value-level testing."""
    body = re.sub(
        r"^CREATE\s+OR\s+REPLACE\s+VIEW\s+`?[^`\s]+`?\s+AS\s+",
        "",
        view_sql.strip(),
        flags=re.IGNORECASE,
    ).rstrip(";")
    body = body.replace("`agent_eval.", "`").replace("agent_eval.", "")
    ast_node = sqlglot.parse_one(body, read="bigquery")
    # Run transform until fixed point so nested temporal/QUALIFY rewrites all resolve cleanly.
    rewritten = ast_node
    for _ in range(4):
        rewritten = rewritten.transform(_rewrite_bq_node_for_sqlite)
    return rewritten.sql(dialect="sqlite")


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


ENTERPRISE_OTEL_SPANS_BASE_INSERT = (
    "INSERT INTO otel_spans ("
    "trace_id, span_id, parent_span_id, span_name, gcp_apphub_application_id, "
    "service_name, agent_name, otel_span_kind, span_kind, model_name, "
    "tool_name, tool_call_status, status_code, rule0_pass, finops_traffic_type, "
    "outcome_id, outcome_owner, source_mode, skill_id, skill_version, entity_id, "
    "start_time, end_time, receive_time, latency_ms, input_tokens, cached_input_tokens, "
    "output_tokens, thinking_tokens, attributes"
    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

ENTERPRISE_EVAL_RESULTS_BASE_INSERT = (
    "INSERT INTO eval_results ("
    "eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, "
    "rubric_id, judge_model, sampling_stratum, rule0_pass, reasoning_trace, "
    "ordinal_score, binary_pass, judge_input_tokens, judge_output_tokens, "
    "judge_thinking_tokens, evaluated_at"
    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def insert_enterprise_sqlite_span(conn: sqlite3.Connection, span: OTelGenAISpanRecord) -> None:
    """Insert a full 39-column `OTelGenAISpanRecord` into the transpiled SQLite fixture."""
    conn.execute(
        "INSERT INTO otel_spans ("
        "trace_id, span_id, parent_span_id, span_name, gcp_apphub_application_id, "
        "gcp_apphub_service_id, gcp_apphub_workload_id, service_name, agent_name, "
        "otel_span_kind, span_kind, model_name, tool_name, tool_call_status, status_code, "
        "rule0_pass, finops_traffic_type, outcome_id, outcome_owner, source_mode, skill_id, "
        "skill_version, entity_id, start_time, end_time, receive_time, latency_ms, "
        "input_tokens, cached_input_tokens, output_tokens, thinking_tokens, "
        "serving_traffic_type, request_type_header, http_status_code, "
        "cache_write_5m_tokens, cache_write_1h_tokens, session_id, turn_index, attributes"
        ") VALUES ("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?"
        ")",
        (
            span.trace_id,
            span.span_id,
            span.parent_span_id,
            span.span_name,
            span.gcp_apphub_application_id,
            span.gcp_apphub_service_id,
            span.gcp_apphub_workload_id,
            span.service_name,
            span.agent_name,
            span.otel_span_kind,
            str(span.span_kind),
            span.model_name,
            span.tool_name,
            span.tool_call_status,
            span.status_code,
            1 if span.rule0_pass else 0,
            str(span.finops_traffic_type),
            span.outcome_id,
            span.outcome_owner,
            span.source_mode,
            span.skill_id,
            span.skill_version,
            span.entity_id,
            span.start_time.isoformat(),
            span.end_time.isoformat(),
            (span.receive_time or span.end_time).isoformat(),
            span.latency_ms,
            span.input_tokens,
            span.cached_input_tokens,
            span.output_tokens,
            span.thinking_tokens,
            str(span.serving_traffic_type),
            span.request_type_header,
            span.http_status_code,
            span.cache_write_5m_tokens,
            span.cache_write_1h_tokens,
            span.session_id,
            span.turn_index,
            json.dumps(span.attributes),
        ),
    )


def _create_enterprise_sqlite_fixture(conn: sqlite3.Connection) -> None:
    """Create base tables required to execute transpiled `sql/core/views.sql` and `sql/core/step1_trace_views.sql`."""
    conn.executescript(
        """
        CREATE TABLE otel_spans (
            trace_id TEXT,
            span_id TEXT,
            parent_span_id TEXT,
            span_name TEXT,
            gcp_apphub_application_id TEXT,
            gcp_apphub_service_id TEXT DEFAULT NULL,
            gcp_apphub_workload_id TEXT DEFAULT NULL,
            service_name TEXT,
            agent_name TEXT,
            otel_span_kind TEXT,
            span_kind TEXT,
            model_name TEXT,
            tool_name TEXT,
            tool_call_status TEXT,
            status_code TEXT,
            rule0_pass INTEGER,
            finops_traffic_type TEXT,
            outcome_id TEXT,
            outcome_owner TEXT,
            source_mode TEXT,
            skill_id TEXT,
            skill_version TEXT,
            entity_id TEXT,
            start_time TEXT,
            end_time TEXT,
            receive_time TEXT,
            latency_ms REAL,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            thinking_tokens INTEGER,
            serving_traffic_type TEXT DEFAULT 'STANDARD',
            request_type_header TEXT DEFAULT NULL,
            http_status_code INTEGER DEFAULT NULL,
            cache_write_5m_tokens INTEGER DEFAULT 0,
            cache_write_1h_tokens INTEGER DEFAULT 0,
            session_id TEXT DEFAULT NULL,
            turn_index INTEGER DEFAULT NULL,
            attributes TEXT
        );
        CREATE TABLE model_pricing_catalog (
            model_name TEXT,
            provider TEXT DEFAULT 'google',
            lifecycle_track TEXT DEFAULT 'long_term_ga',
            effective_start TEXT,
            effective_end TEXT,
            input_usd_per_1m REAL,
            cached_input_usd_per_1m REAL,
            cache_write_5m_usd_per_1m REAL,
            cache_write_1h_usd_per_1m REAL,
            output_usd_per_1m REAL,
            long_context_threshold_tokens INTEGER,
            long_context_input_usd_per_1m REAL,
            long_context_cached_usd_per_1m REAL,
            long_context_output_usd_per_1m REAL,
            burndown_uncached_weight REAL,
            burndown_cached_weight REAL,
            burndown_cache_write_5m_weight REAL DEFAULT 1.0,
            burndown_cache_write_1h_weight REAL DEFAULT 1.0,
            burndown_output_weight REAL,
            burndown_long_uncached_weight REAL,
            burndown_long_cached_weight REAL,
            burndown_long_output_weight REAL,
            priority_paygo_multiplier REAL DEFAULT 1.8,
            flex_paygo_multiplier REAL DEFAULT 0.5
        );
        CREATE TABLE app_registry (
            apphub_application_id TEXT,
            business_unit TEXT,
            sla_latency_ms REAL,
            cost_center TEXT,
            is_external_counterparty INTEGER DEFAULT 0,
            storage_usd_per_outcome REAL,
            risk_tier TEXT DEFAULT 'RL-3b',
            autonomy_tier TEXT DEFAULT 'HUMAN_IN_LOOP',
            lifecycle_track_policy TEXT DEFAULT 'long_term_ga_only',
            funded_gsus REAL DEFAULT 0.0,
            spillover_tier TEXT DEFAULT 'standard',
            pt_commitment_months INTEGER DEFAULT 0
        );
        CREATE TABLE eval_results (
            eval_id TEXT,
            eval_run_id TEXT,
            trace_id TEXT,
            outcome_id TEXT,
            apphub_application_id TEXT,
            rubric_id TEXT,
            judge_model TEXT,
            sampling_stratum TEXT,
            rule0_pass INTEGER,
            reasoning_trace TEXT,
            ordinal_score INTEGER,
            binary_pass INTEGER,
            judge_input_tokens INTEGER,
            judge_output_tokens INTEGER,
            judge_thinking_tokens INTEGER,
            policy_sha256 TEXT DEFAULT NULL,
            amended_clause_ids TEXT DEFAULT NULL,
            target_clause_id TEXT DEFAULT NULL,
            evaluated_at TEXT
        );
        CREATE TABLE sme_labels (
            apphub_application_id TEXT,
            rubric_id TEXT,
            rule0_pass INTEGER,
            human_ordinal_score INTEGER,
            human_pass INTEGER,
            judge_ordinal_score INTEGER,
            judge_pass INTEGER
        );
        CREATE TABLE bq_query_jobs (
            job_id TEXT,
            apphub_application_id TEXT,
            outcome_id TEXT,
            total_bytes_billed INTEGER,
            total_slot_ms INTEGER,
            pricing_mode TEXT,
            query_cost_usd REAL,
            created_at TEXT
        );
        CREATE TABLE inference_request_logs (
            request_id TEXT,
            trace_id TEXT,
            apphub_application_id TEXT,
            model_name TEXT,
            serving_traffic_type TEXT,
            request_type_header TEXT,
            http_status_code INTEGER,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            cache_write_5m_tokens INTEGER,
            cache_write_1h_tokens INTEGER,
            output_tokens INTEGER,
            thinking_tokens INTEGER,
            request_timestamp TEXT
        );
        """
    )


def _install_enterprise_views(conn: sqlite3.Connection) -> None:
    views_path = SQL_DIR / "core" / "views.sql"
    step1_path = SQL_DIR / "core" / "step1_trace_views.sql"
    capacity_path = SQL_DIR / "finops" / "capacity_views.sql"
    for path, name in (
        (step1_path, "agent_eval.v_default_model_pricing"),
        (step1_path, "agent_eval.v_step1_trace_waterfall"),
        (step1_path, "agent_eval.v_outcomes"),
        (step1_path, "agent_eval.v_infra_cost_estimate"),
        (step1_path, "agent_eval.v_attribution_coverage"),
        (views_path, "agent_eval.v_bq_query_cost_attribution"),
        (views_path, "agent_eval.v_trace_waterfall"),
        (views_path, "agent_eval.v_trace_sql_evaluation"),
        (views_path, "agent_eval.v_enterprise_cost_per_verified_outcome"),
        (views_path, "agent_eval.v_counterparty_spend"),
        (views_path, "agent_eval.v_agent_eval_and_finops_summary"),
        (capacity_path, "agent_eval.v_capacity_hourly_burndown_export"),
    ):
        short_name = name.split(".", 1)[1]
        sql = _transpile_bq_view_to_sqlite(_extract_view_sql_text(path, name))
        conn.execute(f"CREATE VIEW {short_name} AS {sql}")


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


def test_shipped_enterprise_views_value_parity_dedup_and_latest_verdict() -> None:
    """Transpiled `sql/core/views.sql` matches Python Rogan-Gladen & SQLite twin, dedups, and uses latest verdict."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)

    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    py_spans: list[OTelGenAISpanRecord] = []
    py_outcomes: dict[str, bool] = {}

    for i in range(22):
        tid = f"trace-f3-{i:03d}"
        r0 = i < 20
        t0 = base + timedelta(seconds=i)
        t1 = t0 + timedelta(milliseconds=500)
        py_spans.append(
            OTelGenAISpanRecord(
                trace_id=tid,
                span_id=f"sp-f3-{i:03d}",
                start_time=t0,
                end_time=t1,
                latency_ms=500.0,
                status_code="OK" if r0 else "ERROR",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                gcp_apphub_application_id="cal-app",
                model_name="gemini-3.8-flash",
                input_tokens=10_000,
                cached_input_tokens=2_000,
                output_tokens=500,
                thinking_tokens=100,
                rule0_pass=r0,
                outcome_id=f"out-f3-{i:03d}",
                outcome_owner="cal-app",
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
            )
        )
        conn.execute(
            ENTERPRISE_OTEL_SPANS_BASE_INSERT,
            (
                tid,
                f"sp-f3-{i:03d}",
                None,
                "invoke_agent",
                "cal-app",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "invoke_agent",
                "gemini-3.8-flash",
                None,
                None,
                "OK" if r0 else "ERROR",
                1 if r0 else 0,
                "production_inference",
                f"out-f3-{i:03d}",
                "cal-app",
                "PDF",
                None,
                None,
                None,
                t0.isoformat(),
                t1.isoformat(),
                t1.isoformat(),
                500.0,
                10_000,
                2_000,
                500,
                100,
                "{}",
            ),
        )
        if i == 0:
            conn.execute(
                ENTERPRISE_OTEL_SPANS_BASE_INSERT,
                (
                    tid,
                    "sp-close-000",
                    f"sp-f3-{i:03d}",
                    "outcome.closed",
                    "cal-app",
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
                    "out-f3-000",
                    "cal-app",
                    "PDF",
                    None,
                    None,
                    None,
                    t1.isoformat(),
                    t1.isoformat(),
                    t1.isoformat(),
                    0.0,
                    0,
                    0,
                    0,
                    0,
                    '{"outcome.status": "succeeded", "outcome.verified": "true"}',
                ),
            )
        if i < 20:
            passed = i < 18
            py_outcomes[tid] = passed
            conn.execute(
                ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
                (
                    f"ev-{i}",
                    "run-1",
                    tid,
                    f"out-f3-{i:03d}",
                    "cal-app",
                    "groundedness_4pt_v1",
                    "gemini-3.8-flash",
                    "CANARY_5PCT",
                    1,
                    "rationale",
                    4 if passed else 2,
                    1 if passed else 0,
                    1000,
                    100,
                    0,
                    "2026-11-21T12:10:00+00:00",
                ),
            )
            # Duplicate append of the same (eval_run_id, trace_id, rubric_id) must be deduplicated.
            if i == 0:
                conn.execute(
                    ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
                    (
                        "ev-0-dup-older",
                        "run-1",
                        tid,
                        "out-f3-000",
                        "cal-app",
                        "groundedness_4pt_v1",
                        "gemini-3.8-flash",
                        "CANARY_5PCT",
                        1,
                        "older dup",
                        4,
                        1,
                        1000,
                        100,
                        0,
                        "2026-11-21T12:05:00+00:00",
                    ),
                )
                # Older failing run for out-f3-000 must be superseded by newer passing run-1.
                conn.execute(
                    ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
                    (
                        "ev-0-old-fail",
                        "run-0-old",
                        tid,
                        "out-f3-000",
                        "cal-app",
                        "groundedness_4pt_v1",
                        "ai_evaluate_agent_managed",
                        "CANARY_5PCT",
                        1,
                        "old fail",
                        1,
                        0,
                        1000,
                        100,
                        0,
                        "2026-11-20T12:00:00+00:00",
                    ),
                )

    # Seed SME [TP=87, FN=2, TN=7, FP=4]
    sme_rows = (
        [("cal-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 87
        + [("cal-app", "groundedness_4pt_v1", 1, 4, 1, 2, 0)] * 2
        + [("cal-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)] * 7
        + [("cal-app", "groundedness_4pt_v1", 1, 1, 0, 4, 1)] * 4
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)

    bq_row = conn.execute(
        "SELECT * FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'cal-app'"
    ).fetchone()
    cm = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    py_rg = rogan_gladen_calibrate(observed_pass_rate=0.90, sample_size=20, confusion=cm)
    twin_summary = evaluate_traces_in_sqlite(
        spans=py_spans,
        judge_outcomes=py_outcomes,
        sme_confusion=cm,
    )["cal-app"]

    assert bq_row["calibration_status"] == "CALIBRATED"
    assert int(bq_row["judged_samples"]) == 20
    assert float(bq_row["calibrated_pass_rate"]) == pytest.approx(
        py_rg.calibrated_pass_rate, abs=1e-12
    )
    assert float(bq_row["calibrated_pass_rate"]) == pytest.approx(
        twin_summary.calibrated_pass_rate, abs=1e-12
    )
    assert float(bq_row["calibrated_ci_95_lower"]) == pytest.approx(py_rg.ci_95_lower, abs=1e-12)
    assert float(bq_row["calibrated_ci_95_upper"]) == pytest.approx(py_rg.ci_95_upper, abs=1e-12)

    # Out-f3-000 had an older failing verdict in run-0-old and a newer passing verdict in run-1 -> verified!
    # Duplicate ev-0-dup-older in run-1 was deduplicated (21 rows total across 20 outcomes, not 22).
    cpvo_row = conn.execute(
        "SELECT verified_outcomes, measured_judge_cost_usd FROM v_enterprise_cost_per_verified_outcome WHERE apphub_application_id = 'cal-app'"
    ).fetchone()
    assert int(cpvo_row["verified_outcomes"]) == 1
    expected_deduped_judge = 21 * ((1000 / 1e6) * 0.15 + (100 / 1e6) * 0.60)
    assert float(cpvo_row["measured_judge_cost_usd"]) == pytest.approx(
        expected_deduped_judge, abs=1e-12
    )
    conn.close()


def test_unclassified_traffic_type_included_in_total_spend_and_coverage() -> None:
    """An unrecognised `finops_traffic_type` lands in `unclassified_usd`, `total_spend_usd`, and `v_attribution_coverage`."""
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    conn.execute(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        (
            "tr-unclass",
            "sp-1",
            None,
            "chat",
            "app-u",
            "svc",
            "agent",
            "SPAN_KIND_SERVER",
            "chat",
            "gemini-3.8-flash",
            None,
            None,
            "OK",
            1,
            "not_a_real_type",
            "out-u",
            "app-u",
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
    )
    expected_cost = (100_000 / 1e6) * 0.15 + (10_000 / 1e6) * 0.60
    trace_row = conn.execute("SELECT * FROM v_trace_sql_evaluation").fetchone()
    summary_row = conn.execute("SELECT * FROM v_agent_eval_and_finops_summary").fetchone()
    cov_row = conn.execute("SELECT * FROM v_attribution_coverage").fetchone()

    assert float(trace_row["unclassified_usd"]) == pytest.approx(expected_cost, abs=1e-12)
    assert float(trace_row["trace_span_cost_usd"]) == pytest.approx(expected_cost, abs=1e-12)
    assert float(summary_row["unclassified_usd"]) == pytest.approx(expected_cost, abs=1e-12)
    assert float(summary_row["total_spend_usd"]) == pytest.approx(
        float(trace_row["trace_span_cost_usd"]), abs=1e-12
    )
    assert int(cov_row["unclassified_traffic_spans"]) == 1
    assert float(cov_row["unclassified_traffic_spend_usd"]) == pytest.approx(
        expected_cost, abs=1e-12
    )
    conn.close()


def test_daily_quota_guard_no_config_fanout_and_engine_b_quota_trip() -> None:
    """`v_step2_daily_quota_status` ignores extra config rows, retains billed retries, and trips on Engine B."""
    quota_path = SQL_DIR / "core" / "step3_daily_quota_guard.sql"
    quota_sql = quota_path.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE PROCEDURE `agent_eval.run_daily_quota_guarded_judge`" in quota_sql
    assert "app_cumulative_tokens <= app_remaining_today" in quota_sql
    assert "global_cumulative_tokens <= global_remaining_today" in quota_sql
    assert "BEGIN TRANSACTION;" in quota_sql
    assert "FAILED_BILLED" in quota_sql

    engine_b_persist_sql = build_ai_evaluate_agent_sql(
        project_id="demo-proj",
        canary_sample_pct=10,
        persist_run_id="run_2026_11_21_bqaa_managed",
    )
    _parse_bigquery_statements(engine_b_persist_sql, "build_ai_evaluate_agent_sql(persist)")
    assert "DIV(CHAR_LENGTH(TO_JSON_STRING(eval_out.trace)), 4)" in engine_b_persist_sql
    assert "DIV(CHAR_LENGTH(COALESCE(metric_row.explanation, '')), 4)" in engine_b_persist_sql
    assert "AND metric_row.metric_name = existing.rubric_id" in engine_b_persist_sql
    assert "WHERE metric_row.score IS NOT NULL" in engine_b_persist_sql
    assert "(ordinal_score >= 3) AS binary_pass" in engine_b_persist_sql

    conn = _new_sqlite_bq_conn(current_date_utc="2026-11-21")
    conn.executescript(
        """
        CREATE TABLE eval_daily_quota_config (
            quota_name TEXT,
            apphub_application_id TEXT,
            daily_judge_token_cap INTEGER,
            per_app_default_token_cap INTEGER,
            max_output_tokens INTEGER,
            thinking_budget_tokens INTEGER,
            updated_at TEXT
        );
        CREATE TABLE eval_daily_quota_reservations (
            reservation_id TEXT,
            quota_date_utc TEXT,
            apphub_application_id TEXT,
            rubric_id TEXT,
            reserved_tokens INTEGER,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE eval_results (
            eval_id TEXT,
            eval_run_id TEXT,
            trace_id TEXT,
            apphub_application_id TEXT,
            rubric_id TEXT,
            judge_model TEXT,
            judge_input_tokens INTEGER,
            judge_output_tokens INTEGER,
            judge_thinking_tokens INTEGER,
            evaluated_at TEXT
        );
        """
    )
    # Two config rows must not fan out tokens_used_today via Cartesian join.
    conn.executemany(
        "INSERT INTO eval_daily_quota_config VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "default_step2_daily_cap",
                "*",
                2_000_000,
                500_000,
                2048,
                1024,
                "2026-11-21T00:00:00+00:00",
            ),
            (
                "secondary_profile",
                "app-a",
                1_500_000,
                500_000,
                2048,
                1024,
                "2026-11-21T00:00:00+00:00",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO eval_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "e1",
                "run-a",
                "t1",
                "app-a",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                700_000,
                100_000,
                0,
                "2026-11-21T08:00:00+00:00",
            ),
            # Non-deduped burn accounting: billed retries still count against daily quota.
            (
                "e1-retry",
                "run-a",
                "t1",
                "app-a",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                200_000,
                0,
                0,
                "2026-11-21T08:05:00+00:00",
            ),
            # Engine B managed row uses 500k tokens -> total 1.5M < 2.0M
            (
                "e2",
                "run-b",
                "t2",
                "app-b",
                "hallucination_v1",
                "ai_evaluate_agent_managed",
                450_000,
                50_000,
                0,
                "2026-11-21T09:00:00+00:00",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO eval_daily_quota_reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "res-1",
            "2026-11-21",
            "app-b",
            "hallucination_v1",
            100_000,
            "RESERVED",
            "2026-11-21T09:05:00+00:00",
            "2026-11-21T09:05:00+00:00",
        ),
    )
    view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(quota_path, "agent_eval.v_step2_daily_quota_status")
    )
    conn.execute(f"CREATE VIEW v_step2_daily_quota_status AS {view_sql}")
    row = conn.execute("SELECT * FROM v_step2_daily_quota_status").fetchone()
    assert int(row["daily_judge_token_cap"]) == 2_000_000
    assert int(row["tokens_used_today"]) == 1_500_000
    assert int(row["tokens_reserved_today"]) == 100_000
    assert int(row["tokens_remaining_today"]) == 400_000
    assert bool(row["quota_available"]) is True

    # Add another 500k Engine B metric row -> 2.0M used + 100k reserved trips the 2M cap
    conn.execute(
        "INSERT INTO eval_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "e3",
            "run-b",
            "t2",
            "app-b",
            "safety_v1",
            "ai_evaluate_agent_managed",
            450_000,
            50_000,
            0,
            "2026-11-21T10:00:00+00:00",
        ),
    )
    tripped = conn.execute("SELECT * FROM v_step2_daily_quota_status").fetchone()
    assert int(tripped["tokens_used_today"]) == 2_000_000
    assert int(tripped["tokens_remaining_today"]) == 0
    assert bool(tripped["quota_available"]) is False
    conn.close()


def test_deterministic_traffic_type_unjudged_rubric_and_multi_rubric_filter() -> None:
    """Explicit `finops_traffic_type` priority, `'groundedness_4pt_v1'` unjudged label, and multi-rubric parity."""
    views_text = (SQL_DIR / "core" / "views.sql").read_text(encoding="utf-8")
    assert "ANY_VALUE(w.finops_traffic_type)" not in views_text
    assert "MAX(w.finops_traffic_type) AS primary_traffic_type" not in views_text
    assert "WHEN 'production_inference' THEN 1" in views_text

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    conn.executemany(
        ENTERPRISE_OTEL_SPANS_BASE_INSERT,
        [
            (
                "tr-unjudged",
                "sp-u1",
                None,
                "invoke_agent",
                "unjudged-parity-app",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "invoke_agent",
                "gemini-3.8-flash",
                None,
                None,
                "OK",
                1,
                "production_inference",
                None,
                "unjudged-parity-app",
                None,
                None,
                None,
                None,
                t0,
                t1,
                t1,
                500.0,
                4_000,
                1_000,
                200,
                0,
                "{}",
            ),
            (
                "tr-multi",
                "sp-m1",
                None,
                "invoke_agent",
                "multi-rubric-app",
                "svc",
                "agent",
                "SPAN_KIND_SERVER",
                "invoke_agent",
                "gemini-3.8-flash",
                None,
                None,
                "OK",
                1,
                "production_inference",
                None,
                "multi-rubric-app",
                None,
                None,
                None,
                None,
                t0,
                t1,
                t1,
                500.0,
                4_000,
                1_000,
                200,
                0,
                "{}",
            ),
        ],
    )
    conn.executemany(
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        [
            (
                "ev-m-g",
                "run-1",
                "tr-multi",
                None,
                "multi-rubric-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "ok",
                4,
                1,
                500,
                50,
                0,
                t1,
            ),
            (
                "ev-m-e",
                "run-1",
                "tr-multi",
                None,
                "multi-rubric-app",
                "escalation_policy_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "failed escalation",
                1,
                0,
                500,
                50,
                0,
                t1,
            ),
        ],
    )

    bq_unjudged = conn.execute(
        "SELECT rubric_id, calibration_status FROM v_agent_eval_and_finops_summary "
        "WHERE apphub_application_id = 'unjudged-parity-app'"
    ).fetchone()
    bq_multi_esc = conn.execute(
        "SELECT raw_judge_pass_rate FROM v_agent_eval_and_finops_summary "
        "WHERE apphub_application_id = 'multi-rubric-app' AND rubric_id = 'escalation_policy_4pt_v1'"
    ).fetchone()
    conn.close()

    base_dt = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    py_spans = [
        OTelGenAISpanRecord(
            trace_id="tr-unjudged",
            span_id="sp-u1",
            start_time=base_dt,
            end_time=base_dt + timedelta(seconds=1),
            latency_ms=500.0,
            gcp_apphub_application_id="unjudged-parity-app",
            input_tokens=4_000,
            cached_input_tokens=1_000,
            output_tokens=200,
        ),
        OTelGenAISpanRecord(
            trace_id="tr-multi",
            span_id="sp-m1",
            start_time=base_dt,
            end_time=base_dt + timedelta(seconds=1),
            latency_ms=500.0,
            gcp_apphub_application_id="multi-rubric-app",
            input_tokens=4_000,
            cached_input_tokens=1_000,
            output_tokens=200,
        ),
    ]
    twin_esc = evaluate_traces_in_sqlite(
        spans=py_spans,
        judge_outcomes={"tr-multi": False},
        extra_rubric_outcomes={"groundedness_4pt_v1": {"tr-multi": True}},
        rubric_id="escalation_policy_4pt_v1",
    )
    twin_unjudged = evaluate_traces_in_sqlite(spans=py_spans[:1])["unjudged-parity-app"]

    assert bq_unjudged["rubric_id"] == twin_unjudged.rubric_id == "groundedness_4pt_v1"
    assert bq_unjudged["calibration_status"] == twin_unjudged.calibration_status == "UNJUDGED"
    assert float(bq_multi_esc["raw_judge_pass_rate"]) == pytest.approx(0.0)
    assert twin_esc["multi-rubric-app"].raw_judge_pass_rate == pytest.approx(0.0)


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


def test_counterparty_classification_uses_explicit_flag_not_app_id_substring() -> None:
    """An internal app named `external-facing-chat` stays in CPVO; explicit flags route to `v_counterparty_spend`."""
    views_text = (SQL_DIR / "core" / "views.sql").read_text(encoding="utf-8")
    assert "LIKE '%external%'" not in views_text

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    conn.executemany(
        "INSERT INTO app_registry (apphub_application_id, business_unit, sla_latency_ms, cost_center, is_external_counterparty) VALUES (?, ?, ?, ?, ?)",
        [
            ("external-facing-chat", "retail_banking", 3000.0, "CC-CHAT", 0),
            ("partner-carrier-hub", "external_b2b", 5000.0, "CC-EXT", 1),
        ],
    )
    t0 = "2026-11-21T12:00:00+00:00"
    t1 = "2026-11-21T12:00:01+00:00"
    for app_id, out_id, attrs in (
        ("external-facing-chat", "out-chat-1", "{}"),
        ("partner-carrier-hub", "out-ext-1", "{}"),
        ("attr-marked-partner", "out-ext-2", '{"apphub.external": "true"}'),
    ):
        conn.executemany(
            ENTERPRISE_OTEL_SPANS_BASE_INSERT,
            [
                (
                    f"tr-{out_id}",
                    f"sp-{out_id}",
                    None,
                    "invoke_agent",
                    app_id,
                    "svc",
                    "agent",
                    "SPAN_KIND_SERVER",
                    "invoke_agent",
                    "gemini-3.8-flash",
                    None,
                    None,
                    "OK",
                    1,
                    "production_inference",
                    out_id,
                    app_id,
                    "A2A",
                    None,
                    None,
                    None,
                    t0,
                    t1,
                    t1,
                    500.0,
                    10_000,
                    0,
                    500,
                    0,
                    attrs,
                ),
                (
                    f"tr-{out_id}",
                    f"sp-close-{out_id}",
                    f"sp-{out_id}",
                    "outcome.closed",
                    app_id,
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
                    out_id,
                    app_id,
                    "A2A",
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

    cpvo_apps = {
        str(r["apphub_application_id"])
        for r in conn.execute(
            "SELECT apphub_application_id FROM v_enterprise_cost_per_verified_outcome"
        ).fetchall()
    }
    counterparty_apps = {
        str(r["apphub_application_id"])
        for r in conn.execute("SELECT apphub_application_id FROM v_counterparty_spend").fetchall()
    }
    conn.close()

    assert "external-facing-chat" in cpvo_apps
    assert "external-facing-chat" not in counterparty_apps
    assert counterparty_apps == {"partner-carrier-hub", "attr-marked-partner"}


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
    assert "IGNORE NULLS ORDER BY timestamp ASC, event_id ASC LIMIT 1" in bqaa_sql
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
