"""BigQuery DDL, ALTER TABLE, PROCEDURE/MODEL stubs, and MERGE execution for `OfflineSqlRunner`."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from bq_eval.runner.engine.tvf import TableFunctionRegistry, transpile_bigquery_staged

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STAGING_TABLE = "_bq_merge_src"


@dataclass(frozen=True)
class MergeContext:
    """Execution callbacks and registry context for offline `MERGE INTO` evaluation."""

    conn: sqlite3.Connection
    registry: TableFunctionRegistry
    default_dataset: str | None
    resolve_name: Callable[[str], str]


def _scan_single_quoted(sql: str, idx: int) -> int:
    n = len(sql)
    quote = sql[idx]
    j = idx + 1
    while j < n:
        if sql[j] == "\\":
            j += 2
            continue
        if sql[j] == quote:
            return j + 1
        j += 1
    return n


def _skip_sql_token(sql: str, idx: int) -> int | None:
    n = len(sql)
    next_idx: int | None = None
    if sql.startswith(("--", "#"), idx):
        nl = sql.find("\n", idx)
        next_idx = n if nl == -1 else nl + 1
    elif sql.startswith("/*", idx):
        end = sql.find("*/", idx + 2)
        next_idx = n if end == -1 else end + 2
    else:
        p_off = 1 if sql[idx] in {"r", "R", "b", "B"} else 0
        if sql.startswith(('"""', "'''"), idx + p_off):
            delim = sql[idx + p_off : idx + p_off + 3]
            end_tq = sql.find(delim, idx + p_off + 3)
            next_idx = n if end_tq == -1 else end_tq + 3
        elif p_off == 0 and sql[idx] in {"'", '"'}:
            next_idx = _scan_single_quoted(sql, idx)
        elif sql[idx] == "`":
            end_bt = sql.find("`", idx + 1)
            next_idx = n if end_bt == -1 else end_bt + 1
    return next_idx


def _strip_leading_comments(sql: str) -> str:
    i, n = 0, len(sql)
    while i < n:
        if sql[i].isspace():
            i += 1
        elif sql.startswith(("--", "#"), i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            break
    return sql[i:].strip()


def _peek_next_word(sql: str, start: int) -> str:
    n = len(sql)
    k = start
    while k < n and sql[k].isspace():
        k += 1
    m = k
    while m < n and (sql[m].isalnum() or sql[m] == "_"):
        m += 1
    return sql[k:m].upper() if m > k else ""


def _advance_word_depth(
    sql: str,
    idx: int,
    block_depth: int,
    case_depth: int,
) -> tuple[int, int, int]:
    n = len(sql)
    j = idx + 1
    while j < n and (sql[j].isalnum() or sql[j] == "_"):
        j += 1
    word = sql[idx:j].upper()
    if word == "CASE":
        case_depth += 1
    elif word == "BEGIN" and _peek_next_word(sql, j) != "TRANSACTION":
        block_depth += 1
    elif word == "END" and _peek_next_word(sql, j) not in {"IF", "LOOP", "WHILE", "FOR"}:
        if case_depth > 0:
            case_depth -= 1
        else:
            block_depth = max(0, block_depth - 1)
    return j, block_depth, case_depth


def split_bigquery_script_statements(sql: str) -> list[str]:
    """Split a multi-statement BigQuery script while preserving `BEGIN ... END;` procedure blocks."""
    stmts: list[str] = []
    n = len(sql)
    i = 0
    chunk_start = 0
    block_depth = 0
    case_depth = 0

    while i < n:
        skipped = _skip_sql_token(sql, i)
        if skipped is not None:
            i = skipped
        elif sql[i] == ";" and block_depth == 0:
            cleaned = _strip_leading_comments(sql[chunk_start:i])
            if cleaned:
                stmts.append(cleaned)
            chunk_start = i + 1
            i += 1
        elif sql[i].isalpha() or sql[i] == "_":
            i, block_depth, case_depth = _advance_word_depth(sql, i, block_depth, case_depth)
        else:
            i += 1

    tail = _strip_leading_comments(sql[chunk_start:])
    if tail:
        stmts.append(tail)
    return stmts


def _bq_dtype_to_sqlite(dtype: exp.DataType | None) -> str:
    if dtype is None:
        return "BLOB"
    if dtype.is_type(
        exp.DataType.Type.INT,
        exp.DataType.Type.BIGINT,
        exp.DataType.Type.SMALLINT,
        exp.DataType.Type.TINYINT,
        exp.DataType.Type.BOOLEAN,
    ):
        return "INTEGER"
    if dtype.is_type(
        exp.DataType.Type.FLOAT,
        exp.DataType.Type.DOUBLE,
        exp.DataType.Type.DECIMAL,
        exp.DataType.Type.BIGDECIMAL,
    ):
        return "REAL"
    return "TEXT"


def _build_drop_table_sql(table_name: str) -> str:
    if not _SAFE_IDENT_RE.fullmatch(table_name):
        raise ValueError(f"Unsafe SQLite table identifier: {table_name!r}")
    return sqlglot.parse_one(f"DROP TABLE IF EXISTS {table_name}", read="sqlite").sql(
        dialect="sqlite"
    )


def _build_temp_ctas_sql(table_name: str, select_sql: str) -> str:
    if not _SAFE_IDENT_RE.fullmatch(table_name):
        raise ValueError(f"Unsafe SQLite temp table identifier: {table_name!r}")
    return exp.Create(
        this=exp.Table(this=exp.to_identifier(table_name, quoted=False)),
        kind="TABLE",
        properties=exp.Properties(expressions=[exp.TemporaryProperty()]),
        expression=sqlglot.parse_one(select_sql, read="sqlite"),
    ).sql(dialect="sqlite")


def _build_rowid_update_sql(table_name: str, cols: Sequence[str]) -> str:
    return exp.Update(
        this=exp.Table(this=exp.to_identifier(table_name, quoted=False)),
        expressions=[
            exp.EQ(this=exp.to_identifier(c, quoted=False), expression=exp.Placeholder())
            for c in cols
        ],
        where=exp.Where(
            this=exp.EQ(
                this=exp.to_identifier("rowid", quoted=False),
                expression=exp.Placeholder(),
            )
        ),
    ).sql(dialect="sqlite")


def _build_placeholder_insert_sql(table_name: str, cols: Sequence[str]) -> str:
    return exp.Insert(
        this=exp.Schema(
            this=exp.Table(this=exp.to_identifier(table_name, quoted=False)),
            expressions=[exp.to_identifier(c, quoted=False) for c in cols],
        ),
        expression=exp.Values(
            expressions=[exp.Tuple(expressions=[exp.Placeholder() for _ in cols])]
        ),
    ).sql(dialect="sqlite")


def _existing_sqlite_columns(conn: sqlite3.Connection, sqlite_name: str) -> tuple[str, ...]:
    if not _SAFE_IDENT_RE.fullmatch(sqlite_name):
        raise ValueError(f"Unsafe SQLite table identifier: {sqlite_name!r}")
    pragma_sql = f"PRAGMA table_info({sqlite_name})"
    rows = conn.execute(pragma_sql).fetchall()
    return tuple(str(r["name"]) for r in rows)


def _table_fqn_without_alias(tbl: exp.Table) -> str:
    parts = [p for p in (tbl.catalog, tbl.db, tbl.name) if p]
    return ".".join(parts)


def execute_create_table_ddl(
    conn: sqlite3.Connection,
    table_columns: dict[str, tuple[str, ...]],
    stmt: exp.Create,
    resolve_name: Callable[[str], str],
) -> str | None:
    """Execute BigQuery `CREATE [OR REPLACE] TABLE [IF NOT EXISTS]` DDL on SQLite."""
    if not isinstance(stmt.this, exp.Schema) or not isinstance(stmt.this.this, exp.Table):
        return None
    raw_table = _table_fqn_without_alias(stmt.this.this)
    sqlite_name = resolve_name(raw_table)
    if not _SAFE_IDENT_RE.fullmatch(sqlite_name):
        raise ValueError(f"Unsafe SQLite table name: {sqlite_name!r}")

    if bool(stmt.args.get("replace")):
        conn.execute(_build_drop_table_sql(sqlite_name))
        table_columns.pop(sqlite_name, None)

    col_defs: list[exp.ColumnDef] = []
    col_names: list[str] = []
    for expr in stmt.this.expressions:
        if not isinstance(expr, exp.ColumnDef):
            continue
        col_name = expr.name
        if not _SAFE_IDENT_RE.fullmatch(col_name):
            raise ValueError(f"Invalid column name {col_name!r} in {raw_table!r}")
        sqlite_type = _bq_dtype_to_sqlite(
            expr.kind if isinstance(expr.kind, exp.DataType) else None
        )
        col_defs.append(
            exp.ColumnDef(
                this=exp.to_identifier(col_name, quoted=False),
                kind=exp.DataType.build(sqlite_type, dialect="sqlite"),
            )
        )
        col_names.append(col_name)

    existing = _existing_sqlite_columns(conn, sqlite_name)
    if not existing:
        create_sql = exp.Create(
            this=exp.Schema(
                this=exp.Table(this=exp.to_identifier(sqlite_name, quoted=False)),
                expressions=col_defs,
            ),
            kind="TABLE",
            exists=True,
        ).sql(dialect="sqlite")
        conn.execute(create_sql)
        table_columns[sqlite_name] = tuple(col_names)
    elif sqlite_name not in table_columns:
        table_columns[sqlite_name] = existing
    return sqlite_name


def _extract_alter_column_def(action: object) -> exp.ColumnDef | None:
    if isinstance(action, exp.ColumnDef):
        return action
    if isinstance(action, exp.Expr) and isinstance(action.this, exp.ColumnDef):
        return action.this
    return None


def execute_alter_table_ddl(
    conn: sqlite3.Connection,
    table_columns: dict[str, tuple[str, ...]],
    stmt: exp.Alter,
    resolve_name: Callable[[str], str],
) -> str:
    """Execute BigQuery `ALTER TABLE ... ADD COLUMN [IF NOT EXISTS]` on SQLite."""
    if not isinstance(stmt.this, exp.Table):
        raise ValueError(f"Unsupported ALTER target: {stmt.sql('bigquery')}")
    sqlite_name = resolve_name(_table_fqn_without_alias(stmt.this))
    existing = list(_existing_sqlite_columns(conn, sqlite_name))
    existing_lower = {c.lower() for c in existing}
    for action in stmt.args.get("actions") or []:
        col_def = _extract_alter_column_def(action)
        if col_def is None or col_def.name.lower() in existing_lower:
            continue
        sqlite_type = _bq_dtype_to_sqlite(
            col_def.kind if isinstance(col_def.kind, exp.DataType) else None
        )
        alter_sql = exp.Alter(
            this=exp.Table(this=exp.to_identifier(sqlite_name, quoted=False)),
            kind="TABLE",
            actions=[
                exp.ColumnDef(
                    this=exp.to_identifier(col_def.name, quoted=False),
                    kind=exp.DataType.build(sqlite_type, dialect="sqlite"),
                )
            ],
        ).sql(dialect="sqlite")
        conn.execute(alter_sql)
        existing.append(col_def.name)
        existing_lower.add(col_def.name.lower())
    table_columns[sqlite_name] = tuple(existing)
    return sqlite_name


def _execute_merge_matched_update(
    ctx: MergeContext,
    target_sqlite: str,
    aliases: tuple[str, str],
    full_cond: exp.Expr,
    then_expr: exp.Update,
) -> None:
    target_alias, src_alias = aliases
    select_exprs: list[exp.Expr] = [
        exp.alias_(
            exp.Column(this=exp.to_identifier("rowid"), table=exp.to_identifier(target_alias)),
            "_target_rowid",
        )
    ]
    set_cols: list[str] = []
    for idx, eq_node in enumerate(then_expr.expressions):
        if isinstance(eq_node, exp.EQ) and isinstance(eq_node.this, exp.Column):
            set_cols.append(eq_node.this.name)
            select_exprs.append(exp.alias_(eq_node.expression.copy(), f"_upd_{idx}"))
    match_select = (
        exp.select(*select_exprs)
        .from_(exp.alias_(exp.to_table(target_sqlite), target_alias, table=True))
        .join(exp.alias_(exp.to_table(_STAGING_TABLE), src_alias, table=True), on=full_cond)
    )
    _, match_sql = transpile_bigquery_staged(
        match_select,
        registry=ctx.registry,
        default_dataset=None,
    )
    upd_rows = ctx.conn.execute(match_sql).fetchall()
    if upd_rows and set_cols:
        upd_sql = _build_rowid_update_sql(target_sqlite, set_cols)
        params = [
            (*(r[f"_upd_{i}"] for i in range(len(set_cols))), r["_target_rowid"]) for r in upd_rows
        ]
        ctx.conn.executemany(upd_sql, params)


def _extract_merge_insert_exprs(
    then_expr: exp.Insert,
    ins_cols: Sequence[str],
    src_alias: str,
) -> list[exp.Expr]:
    expr = then_expr.expression
    if (
        isinstance(expr, exp.Tuple)
        and expr.expressions
        and isinstance(expr.expressions[0], exp.Tuple)
    ):
        return list(expr.expressions[0].expressions)
    if isinstance(expr, exp.Tuple):
        return list(expr.expressions)
    if (
        isinstance(expr, exp.Values)
        and expr.expressions
        and isinstance(expr.expressions[0], exp.Tuple)
    ):
        return list(expr.expressions[0].expressions)
    return [
        exp.Column(this=exp.to_identifier(c), table=exp.to_identifier(src_alias)) for c in ins_cols
    ]


def _execute_merge_not_matched_insert(
    ctx: MergeContext,
    target_sqlite: str,
    aliases: tuple[str, str],
    full_cond: exp.Expr,
    then_expr: exp.Insert,
) -> None:
    target_alias, src_alias = aliases
    ins_cols = [c.name for c in (then_expr.this.expressions if then_expr.this else [])]
    if not ins_cols:
        return
    val_exprs = _extract_merge_insert_exprs(then_expr, ins_cols, src_alias)
    exists_subq = (
        exp.select(exp.Literal.number(1))
        .from_(exp.alias_(exp.to_table(target_sqlite), target_alias, table=True))
        .where(full_cond)
    )
    ins_select = (
        exp.select(
            *(
                exp.alias_(expr.copy(), col_name)
                for col_name, expr in zip(ins_cols, val_exprs, strict=True)
            )
        )
        .from_(exp.alias_(exp.to_table(_STAGING_TABLE), src_alias, table=True))
        .where(exp.Not(this=exp.Exists(this=exists_subq)))
    )
    _, ins_sel_sql = transpile_bigquery_staged(
        ins_select,
        registry=ctx.registry,
        default_dataset=None,
    )
    to_insert = ctx.conn.execute(ins_sel_sql).fetchall()
    if to_insert:
        ins_sql = _build_placeholder_insert_sql(target_sqlite, ins_cols)
        ctx.conn.executemany(ins_sql, [tuple(r[c] for c in ins_cols) for r in to_insert])


def _apply_merge_whens(
    ctx: MergeContext,
    stmt: exp.Merge,
    target_sqlite: str,
    aliases: tuple[str, str],
) -> None:
    on_expr = stmt.args.get("on")
    if not isinstance(on_expr, exp.Expr):
        raise ValueError("MERGE statement missing ON predicate.")
    whens_node = stmt.args.get("whens")
    when_list = whens_node.expressions if isinstance(whens_node, exp.Whens) else []
    for when in when_list:
        if not isinstance(when, exp.When):
            continue
        extra_cond = when.args.get("condition")
        full_cond = (
            exp.and_(on_expr.copy(), extra_cond.copy())
            if isinstance(extra_cond, exp.Expr)
            else on_expr.copy()
        )
        then_expr = when.args.get("then")
        if bool(when.args.get("matched")) and isinstance(then_expr, exp.Update):
            _execute_merge_matched_update(ctx, target_sqlite, aliases, full_cond, then_expr)
        elif not bool(when.args.get("matched")) and isinstance(then_expr, exp.Insert):
            _execute_merge_not_matched_insert(ctx, target_sqlite, aliases, full_cond, then_expr)


def execute_merge_statement(stmt: exp.Merge, ctx: MergeContext) -> str:
    """Execute BigQuery `MERGE INTO` (`WHEN MATCHED THEN UPDATE` & `WHEN NOT MATCHED THEN INSERT`)."""
    if not isinstance(stmt.this, exp.Table):
        raise ValueError("MERGE target must be a table.")
    target_sqlite = ctx.resolve_name(_table_fqn_without_alias(stmt.this))
    target_alias = stmt.this.alias or stmt.this.name

    using_expr = stmt.args.get("using")
    if not isinstance(using_expr, exp.Expr):
        raise ValueError("MERGE statement missing USING source.")
    src_alias = using_expr.alias or (
        using_expr.name if isinstance(using_expr, exp.Table) else "src"
    )
    src_query = (
        using_expr.this.copy()
        if isinstance(using_expr, exp.Subquery) and using_expr.this is not None
        else exp.select("*").from_(using_expr.copy())
    )
    stages, src_sqlite_sql = transpile_bigquery_staged(
        src_query,
        registry=ctx.registry,
        default_dataset=ctx.default_dataset,
    )
    created_stages: list[str] = []
    try:
        for stg_name, stg_sql in stages:
            ctx.conn.execute(_build_drop_table_sql(stg_name))
            ctx.conn.execute(_build_temp_ctas_sql(stg_name, stg_sql))
            created_stages.append(stg_name)
        ctx.conn.execute(_build_drop_table_sql(_STAGING_TABLE))
        ctx.conn.execute(_build_temp_ctas_sql(_STAGING_TABLE, src_sqlite_sql))
        if ctx.conn.execute("SELECT 1 FROM _bq_merge_src LIMIT 1").fetchone() is not None:
            _apply_merge_whens(ctx, stmt, target_sqlite, (target_alias, src_alias))
    finally:
        ctx.conn.execute(_build_drop_table_sql(_STAGING_TABLE))
        for stg_name in reversed(created_stages):
            ctx.conn.execute(_build_drop_table_sql(stg_name))
    return target_sqlite
