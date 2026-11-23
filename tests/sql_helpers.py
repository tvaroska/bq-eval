"""Shared helpers for the SQL tests: BigQuery parsing, view extraction, and a
BigQuery-to-SQLite transpiler with fixtures for the enterprise views."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlglot
from sqlglot import exp

from bq_eval.core.contract.otel import (
    OTelGenAISpanRecord,
)
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


def _sqlite_safe_cast_bool(value: object) -> int | None:
    """Mirror BigQuery `SAFE_CAST(x AS BOOL)` (case-insensitive exact 'true'/'false', no whitespace trim)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 0 if value == 0 else 1
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return 1
        if lowered == "false":
            return 0
    return None


def _sqlite_safe_cast_float64(value: object) -> float | None:
    """Mirror BigQuery `SAFE_CAST(x AS FLOAT64)` (invalid strings return NULL, never 0.0)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    try:
        parsed = float(str(value).strip())
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _sqlite_safe_cast_int64(value: object) -> int | None:
    """Mirror BigQuery `SAFE_CAST(x AS INT64)` (invalid or decimal strings return NULL)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value) if math.isfinite(value) else None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _sqlite_farm_fingerprint(value: object) -> int | None:
    """Deterministic 64-bit signed fingerprint emulating BigQuery `FARM_FINGERPRINT`."""
    if value is None:
        return None
    digest = hashlib.sha256(str(value).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _sqlite_mod(num: int | float | None, den: int | float | None) -> int | None:
    if num is None or den is None or den == 0:
        return None
    return int(num) % int(den)


def _sqlite_date_sub_days(date_str: str | None, days: int | float | None) -> str | None:
    if date_str is None:
        return None
    dt = datetime.fromisoformat(str(date_str)[:10])
    return (dt - timedelta(days=int(days or 0))).strftime("%Y-%m-%d")


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


def _new_sqlite_bq_conn(
    current_date_utc: str = "2026-11-21",
    current_timestamp_utc: str | None = None,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ts_now = current_timestamp_utc or f"{current_date_utc}T12:00:00+00:00"
    conn.create_function("JSON_VALUE", 2, _sqlite_json_value)
    conn.create_function("REGEXP_REPLACE", 3, _sqlite_regexp_replace)
    conn.create_function("BQ_REGEXP_CONTAINS", 2, _sqlite_regexp_contains)
    conn.create_function("BQ_REGEXP_EXTRACT", 2, _sqlite_regexp_extract)
    conn.create_function("BQ_STARTS_WITH", 2, _sqlite_starts_with)
    conn.create_function("BQ_JSON_SET", -1, _sqlite_json_set)
    conn.create_function("BQ_SAFE_CAST_BOOL", 1, _sqlite_safe_cast_bool)
    conn.create_function("BQ_SAFE_CAST_FLOAT64", 1, _sqlite_safe_cast_float64)
    conn.create_function("BQ_SAFE_CAST_INT64", 1, _sqlite_safe_cast_int64)
    conn.create_function("BQ_FARM_FINGERPRINT", 1, _sqlite_farm_fingerprint)
    conn.create_function("BQ_MOD", 2, _sqlite_mod)
    conn.create_function("BQ_DATE_SUB_DAYS", 2, _sqlite_date_sub_days)
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
    conn.create_function("BQ_CURRENT_TIMESTAMP", 0, lambda: ts_now)
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


def _rewrite_bq_try_cast(node: exp.TryCast) -> exp.Expr:
    if isinstance(node.to, exp.DataType):
        if node.to.is_type(exp.DataType.Type.BOOLEAN):
            return exp.Anonymous(this="BQ_SAFE_CAST_BOOL", expressions=[node.this.copy()])
        if node.to.is_type(exp.DataType.Type.FLOAT, exp.DataType.Type.DOUBLE):
            return exp.Anonymous(this="BQ_SAFE_CAST_FLOAT64", expressions=[node.this.copy()])
        if node.to.is_type(
            exp.DataType.Type.INT,
            exp.DataType.Type.BIGINT,
            exp.DataType.Type.SMALLINT,
            exp.DataType.Type.TINYINT,
        ):
            return exp.Anonymous(this="BQ_SAFE_CAST_INT64", expressions=[node.this.copy()])
    return exp.Cast(this=node.this, to=node.to)


_TIMESTAMP_UNIT_TO_MS: dict[str, int] = {
    "MILLISECOND": 1,
    "SECOND": 1_000,
    "MINUTE": 60_000,
    "HOUR": 3_600_000,
    "DAY": 86_400_000,
}

_ANONYMOUS_SQLITE_FN_MAP: dict[str, str] = {
    "JSON_SET": "BQ_JSON_SET",
    "STARTS_WITH": "BQ_STARTS_WITH",
    "UNIX_SECONDS": "BQ_UNIX_SECONDS",
    "TIMESTAMP_SECONDS": "BQ_TIMESTAMP_SECONDS",
    "DIV": "BQ_INT_DIV",
    "FARM_FINGERPRINT": "BQ_FARM_FINGERPRINT",
}

_TEMPORAL_SINGLE_ARG_MAP: tuple[tuple[type[exp.Expr], str], ...] = (
    (exp.TimestampTrunc, "BQ_TIMESTAMP_TRUNC_HOUR"),
    (exp.Date, "BQ_DATE"),
    (exp.UnixToTime, "BQ_TIMESTAMP_SECONDS"),
    (exp.TimeToUnix, "BQ_UNIX_SECONDS"),
    (exp.UnixSeconds, "BQ_UNIX_SECONDS"),
)


def _rewrite_bq_timestamp_sub(node: exp.TimestampSub) -> exp.Expr:
    unit_node = node.args.get("unit")
    unit_name = unit_node.name.upper() if isinstance(unit_node, exp.Expr) else "MILLISECOND"
    factor = _TIMESTAMP_UNIT_TO_MS.get(unit_name, 1)
    delta_expr: exp.Expr = (
        node.expression.copy()
        if factor == 1
        else exp.Mul(this=node.expression.copy(), expression=exp.Literal.number(factor))
    )
    return exp.Anonymous(
        this="BQ_TIMESTAMP_SUB_MS",
        expressions=[node.this.copy(), delta_expr],
    )


_TEMPORAL_TWO_ARG_MAP: tuple[tuple[type[exp.Expr], str], ...] = (
    (exp.TimestampDiff, "TIMESTAMP_DIFF_MS"),
    (exp.DateSub, "BQ_DATE_SUB_DAYS"),
    (exp.IntDiv, "BQ_INT_DIV"),
)


def _rewrite_bq_temporal(node: exp.Expr) -> exp.Expr | None:
    single = dict(_TEMPORAL_SINGLE_ARG_MAP).get(type(node))
    if single is not None:
        return exp.Anonymous(this=single, expressions=[node.this.copy()])
    two = dict(_TEMPORAL_TWO_ARG_MAP).get(type(node))
    if two is not None:
        return exp.Anonymous(this=two, expressions=[node.this.copy(), node.expression.copy()])
    if isinstance(node, exp.TimestampSub):
        return _rewrite_bq_timestamp_sub(node)
    if isinstance(node, (exp.CurrentDate, exp.CurrentTimestamp)):
        fn = "BQ_CURRENT_DATE" if isinstance(node, exp.CurrentDate) else "BQ_CURRENT_TIMESTAMP"
        return exp.Anonymous(this=fn, expressions=[])
    if isinstance(node, exp.TimeToStr):
        return exp.Anonymous(
            this="FORMAT_TIMESTAMP",
            expressions=[node.args["format"].copy(), node.this.copy()],
        )
    return None


def _rewrite_bq_anonymous(node: exp.Anonymous) -> exp.Expr:
    upper_name = node.name.upper()
    mapped = _ANONYMOUS_SQLITE_FN_MAP.get(upper_name)
    if mapped is not None:
        return exp.Anonymous(this=mapped, expressions=[e.copy() for e in node.expressions])
    cls = _SQLITE_AGG_MAP.get(upper_name)
    if cls is not None:
        return cls(this=node.expressions[0])
    return node


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
        result = _rewrite_bq_try_cast(node)
    elif isinstance(node, exp.Mod):
        result = exp.Anonymous(
            this="BQ_MOD",
            expressions=[node.this.copy(), node.expression.copy()],
        )
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
        result = _rewrite_bq_anonymous(node)
    elif isinstance(node, exp.FarmFingerprint):
        fp_args = (
            [e.copy() for e in node.expressions]
            if node.expressions
            else ([node.this.copy()] if node.this is not None else [])
        )
        result = exp.Anonymous(this="BQ_FARM_FINGERPRINT", expressions=fp_args)
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
        (step1_path, "agent_eval.v_step1_trace_eval"),
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
