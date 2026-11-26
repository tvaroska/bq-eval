"""Shared helpers for SQL tests: BigQuery parsing, view extraction, and enterprise SQLite fixtures."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp

from bq_eval.core.contract.otel import (
    OTelGenAISpanRecord,
)
from bq_eval.runner.engine.transpiler import (
    _new_sqlite_bq_conn,
    _transpile_bq_view_to_sqlite,
)
from scripts.bq_dry_run import split_sql_statements

__all__ = [
    "ALLOWED_BQ_PREVIEW_COMMANDS",
    "ENTERPRISE_EVAL_RESULTS_BASE_INSERT",
    "ENTERPRISE_OTEL_SPANS_BASE_INSERT",
    "REPO_ROOT",
    "SQL_DIR",
    "_create_enterprise_sqlite_fixture",
    "_extract_mode_c_views",
    "_extract_view_definitions",
    "_extract_view_sql_text",
    "_find_graph_cycles",
    "_find_unescaped_multiline_single_quoted_strings",
    "_install_enterprise_views",
    "_new_sqlite_bq_conn",
    "_normalize_bq_ai_tvf_for_sqlglot",
    "_parse_bigquery_statements",
    "_referenced_tables",
    "_transpile_bq_view_to_sqlite",
    "insert_enterprise_sqlite_span",
]

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
    r"(?is)\b(CREATE\s+(?:OR\s+REPLACE\s+)?TEMP\s+TABLE\s+.+?;|"
    r"INSERT\s+INTO\s+.+?;|"
    r"UPDATE\s+`?[\w.]+`?\s+SET\s+.+?;)"
)
_EXEC_IMMEDIATE_START_RE = re.compile(r"(?is)\bEXECUTE\s+IMMEDIATE\s+CONCAT\s*\(")


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


def _extract_one_concat_call(sql: str, pos: int) -> tuple[int, str]:
    """Scan balanced `CONCAT(...)` args from `pos`, joining triple-quoted SQL chunks."""
    depth = 1
    n = len(sql)
    segments: list[str] = []
    while pos < n and depth > 0:
        if sql[pos : pos + 3] in ('"""', "'''"):
            end_q = _skip_triple_quoted(sql, pos, sql[pos : pos + 3])
            segments.append(sql[pos + 3 : end_q - 3])
            pos = end_q
        elif sql[pos] == "'":
            pos, _ = _scan_single_quoted(sql, pos)
        else:
            if sql[pos] == "(":
                depth += 1
            elif sql[pos] == ")":
                depth -= 1
            pos += 1
    semi = sql.find(";", pos)
    end_stmt = n if semi == -1 else semi + 1
    return end_stmt, "512".join(segments)


def _strip_and_render_exec_immediate(raw_proc_sql: str) -> tuple[str, list[str]]:
    """Strip `EXECUTE IMMEDIATE CONCAT(...);` blocks and return rendered inner SQL strings."""
    rendered: list[str] = []
    remaining = raw_proc_sql
    while match := _EXEC_IMMEDIATE_START_RE.search(remaining):
        end_stmt, inner_sql = _extract_one_concat_call(remaining, match.end())
        if inner_sql.strip():
            rendered.append(inner_sql)
        remaining = remaining[: match.start()] + remaining[end_stmt:]
    return remaining, rendered


def _validate_procedure_inner_sql(raw_proc_sql: str, label: str) -> None:
    """Parse DML and temp-table SQL statements inside a BigQuery stored procedure body."""
    stripped_proc, dynamic_stmts = _strip_and_render_exec_immediate(raw_proc_sql)
    inner_stmts = [*_PROC_INNER_SQL_RE.findall(stripped_proc), *dynamic_stmts]
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
    pricing_path = SQL_DIR / "core" / "pricing_seed.sql"
    views_path = SQL_DIR / "core" / "views.sql"
    summary_path = SQL_DIR / "core" / "summary_views.sql"
    step1_path = SQL_DIR / "core" / "step1_trace_views.sql"
    capacity_path = SQL_DIR / "finops" / "capacity_views.sql"
    for path, name in (
        (pricing_path, "agent_eval.v_default_model_pricing"),
        (step1_path, "agent_eval.v_step1_trace_waterfall"),
        (step1_path, "agent_eval.v_step1_trace_eval"),
        (step1_path, "agent_eval.v_outcomes"),
        (step1_path, "agent_eval.v_infra_cost_estimate"),
        (step1_path, "agent_eval.v_attribution_coverage"),
        (views_path, "agent_eval.v_bq_query_cost_attribution"),
        (views_path, "agent_eval.v_trace_waterfall"),
        (views_path, "agent_eval.v_trace_sql_evaluation"),
        (views_path, "agent_eval.v_enterprise_cost_per_verified_outcome"),
        (views_path, "agent_eval.v_counterparty_spend"),
        (summary_path, "agent_eval.v_agent_eval_and_finops_summary"),
        (capacity_path, "agent_eval.v_capacity_hourly_burndown_export"),
    ):
        short_name = name.split(".", 1)[1]
        sql = _transpile_bq_view_to_sqlite(_extract_view_sql_text(path, name))
        conn.execute(f"CREATE VIEW {short_name} AS {sql}")
