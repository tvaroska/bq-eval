"""Trace-store JSONL loader and in-memory offline SQL runner (`ADR-010`, `ADR-032`)."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

import sqlglot
from sqlglot import exp

from bq_eval.runner.contract.spec import (
    FixtureTableLoadResult,
    RunnerConfig,
    RunnerQueryResult,
)
from bq_eval.runner.engine.transpiler import (
    SqliteValue,
    _new_sqlite_bq_conn,
    _normalize_bq_timestamp,
)
from bq_eval.runner.engine.tvf import (
    TableFunctionRegistry,
    parse_qualified_parts,
    sqlite_table_name,
    transpile_bigquery_ast,
)

_INT_STR_RE = re.compile(r"^-?\d+$")
_SAFE_COL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXPLICIT_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|z|\s+UTC|[+-]\d{2}:\d{2})$"
)

TRACE_INTEGER_COLUMNS: frozenset[str] = frozenset(
    {
        "dropped_attributes_count",
        "dropped_events_count",
        "dropped_links_count",
        "duration_nano",
        "end_time_unix_nano",
        "kind",
        "receive_time_unix_nano",
        "severity_number",
        "start_time_unix_nano",
        "storage_bytes",
    }
)
NESTED_INTEGER_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("status", "code"),
        ("events", "dropped_attributes_count"),
        ("events", "time_unix_nano"),
        ("links", "dropped_attributes_count"),
    }
)
TIMESTAMP_COLUMNS: frozenset[str] = frozenset(
    {
        "approved_expires_at",
        "effective_from",
        "effective_to",
        "end_time",
        "event_time",
        "receive_time",
        "receive_timestamp",
        "start_time",
        "timestamp",
    }
)
NESTED_TIMESTAMP_PATHS: frozenset[tuple[str, ...]] = frozenset({("events", "time")})


class EmptyQueryResultError(AssertionError):
    """Raised when a query returns zero rows against a non-empty expected dataset."""


def _coerce_nested(value: object, *, path: tuple[str, ...] = ()) -> object:
    if value is None or "attributes" in path:
        return value
    if isinstance(value, dict):
        return {str(k): _coerce_nested(v, path=(*path, str(k))) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_nested(item, path=path) for item in value]
    if isinstance(value, str):
        if path in NESTED_INTEGER_PATHS and _INT_STR_RE.fullmatch(value):
            return int(value)
        if path in NESTED_TIMESTAMP_PATHS:
            return _normalize_bq_timestamp(value)
    return value


def _coerce_cell(col: str, value: object) -> SqliteValue:
    result: SqliteValue
    if value is None:
        result = None
    elif isinstance(value, bool):
        result = 1 if value else 0
    elif isinstance(value, (dict, list)):
        result = json.dumps(_coerce_nested(value, path=(col,)), sort_keys=True)
    elif isinstance(value, str):
        if col in TRACE_INTEGER_COLUMNS and _INT_STR_RE.fullmatch(value):
            result = int(value)
        elif col in TIMESTAMP_COLUMNS or _EXPLICIT_TS_RE.fullmatch(value):
            result = _normalize_bq_timestamp(value)
        else:
            result = value
    elif isinstance(value, (int, float, bytes)):
        result = value
    else:
        result = str(value)
    return result


def _infer_column_type(col: str, values: Sequence[SqliteValue]) -> str:
    if col in TRACE_INTEGER_COLUMNS:
        return "INTEGER"
    if col in TIMESTAMP_COLUMNS:
        return "TEXT"
    non_null = [v for v in values if v is not None]
    if not non_null:
        # SQLite BLOB/NONE affinity preserves exact Python types if later appended rows add ints/floats.
        return "BLOB"
    if all(isinstance(v, int) for v in non_null):
        return "INTEGER"
    if all(isinstance(v, (int, float)) for v in non_null):
        return "REAL"
    return "TEXT"


def _build_create_table_sql(table_name: str, col_types: Sequence[tuple[str, str]]) -> str:
    col_defs = [
        exp.ColumnDef(
            this=exp.to_identifier(col, quoted=False),
            kind=exp.DataType.build(dtype, dialect="sqlite"),
        )
        for col, dtype in col_types
    ]
    stmt = exp.Create(
        this=exp.Schema(
            this=exp.Table(this=exp.to_identifier(table_name, quoted=False)),
            expressions=col_defs,
        ),
        kind="TABLE",
        exists=True,
    )
    return stmt.sql(dialect="sqlite")


def _build_insert_sql(table_name: str, columns: Sequence[str]) -> str:
    stmt = exp.Insert(
        this=exp.Schema(
            this=exp.Table(this=exp.to_identifier(table_name, quoted=False)),
            expressions=[exp.to_identifier(c, quoted=False) for c in columns],
        ),
        expression=exp.Values(
            expressions=[exp.Tuple(expressions=[exp.Placeholder() for _ in columns])]
        ),
    )
    return stmt.sql(dialect="sqlite")


def _build_drop_sql(kind: str, name: str) -> str:
    if kind not in {"TABLE", "VIEW"} or not _SAFE_COL_RE.fullmatch(name):
        raise ValueError(f"Invalid DROP target: {kind!r} {name!r}")
    return sqlglot.parse_one(f"DROP {kind} IF EXISTS {name}", read="sqlite").sql(dialect="sqlite")


def _build_drop_and_create_view_sql(view_name: str, select_sql: str) -> tuple[str, str]:
    drop_stmt = _build_drop_sql("VIEW", view_name)
    create_stmt = exp.Create(
        this=exp.Table(this=exp.to_identifier(view_name, quoted=False)),
        kind="VIEW",
        expression=sqlglot.parse_one(select_sql, read="sqlite"),
    ).sql(dialect="sqlite")
    return drop_stmt, create_stmt


def load_jsonl_rows(path: Path) -> list[dict[str, object]]:
    """Read newline-delimited JSON objects from `path`."""
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object per line in {path}")
        rows.append(parsed)
    return rows


def _json_strings_equal(actual: str, expected: str) -> bool:
    if not (actual[:1] in "{[" and expected[:1] in "{["):
        return False
    try:
        return bool(json.loads(actual) == json.loads(expected))
    except json.JSONDecodeError:
        return False


def _values_match(actual: object, expected: object) -> bool:
    matched = False
    if isinstance(expected, bool):
        matched = actual in (0, 1, True, False) and bool(actual) is expected
    elif isinstance(expected, int):
        matched = type(actual) is int and actual == expected
    elif isinstance(expected, (dict, list)) and isinstance(actual, str):
        try:
            matched = json.loads(actual) == _coerce_nested(expected)
        except json.JSONDecodeError:
            matched = False
    elif isinstance(expected, str) and isinstance(actual, str):
        if actual == expected:
            matched = True
        elif _EXPLICIT_TS_RE.fullmatch(expected) and _EXPLICIT_TS_RE.fullmatch(actual):
            matched = _normalize_bq_timestamp(actual) == _normalize_bq_timestamp(expected)
        else:
            matched = _json_strings_equal(actual, expected)
    else:
        matched = actual == expected
    return matched


def _rows_match(actual: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    if set(actual.keys()) != set(expected.keys()):
        return False
    return all(_values_match(actual[k], expected[k]) for k in expected)


class OfflineSqlRunner:
    """In-memory BigQuery-to-SQLite runner supporting TVFs, dataset names, and record/list rows."""

    def __init__(self, config: RunnerConfig | None = None) -> None:
        self.config = config or RunnerConfig()
        self.conn = _new_sqlite_bq_conn(
            current_date_utc=self.config.current_date_utc,
            current_timestamp_utc=self.config.current_timestamp_utc,
            session_user=self.config.session_user,
        )
        self.registry = TableFunctionRegistry(default_dataset=self.config.default_dataset)
        self._table_columns: dict[str, tuple[str, ...]] = {}

    def _resolve_sqlite_table_name(self, raw_name: str) -> str:
        dataset, obj = parse_qualified_parts(raw_name)
        effective_dataset = dataset or self.config.default_dataset
        return f"{effective_dataset}__{obj}" if effective_dataset else sqlite_table_name(raw_name)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> OfflineSqlRunner:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def load_rows(
        self,
        target_table: str,
        rows: Sequence[Mapping[str, object]],
        *,
        append: bool = False,
    ) -> FixtureTableLoadResult:
        """Load JSON/trace-store rows with nested record and list fields into SQLite."""
        sqlite_name = self._resolve_sqlite_table_name(target_table)
        if not rows:
            if not append and sqlite_name in self._table_columns:
                self.conn.execute(_build_drop_sql("TABLE", sqlite_name))
                self.conn.commit()
                self._table_columns.pop(sqlite_name, None)
            return FixtureTableLoadResult(
                target_table=target_table,
                sqlite_table=sqlite_name,
                row_count=0,
                columns=self._table_columns.get(sqlite_name, ()),
            )
        ordered_cols: list[str] = list(self._table_columns.get(sqlite_name, ())) if append else []
        seen = set(ordered_cols)
        for row in rows:
            for col in row:
                if not _SAFE_COL_RE.fullmatch(col):
                    raise ValueError(f"Invalid column identifier {col!r} in {target_table!r}")
                if col not in seen:
                    seen.add(col)
                    ordered_cols.append(col)

        coerced_rows: list[tuple[SqliteValue, ...]] = [
            tuple(_coerce_cell(col, row.get(col)) for col in ordered_cols) for row in rows
        ]
        if not append or sqlite_name not in self._table_columns:
            self.conn.execute(_build_drop_sql("TABLE", sqlite_name))
            col_types = [
                (col, _infer_column_type(col, [r[idx] for r in coerced_rows]))
                for idx, col in enumerate(ordered_cols)
            ]
            self.conn.execute(_build_create_table_sql(sqlite_name, col_types))
            self._table_columns[sqlite_name] = tuple(ordered_cols)
        else:
            existing = set(self._table_columns[sqlite_name])
            for idx, col in enumerate(ordered_cols):
                if col not in existing:
                    col_type = _infer_column_type(col, [r[idx] for r in coerced_rows])
                    alter_sql = exp.Alter(
                        this=exp.Table(this=exp.to_identifier(sqlite_name, quoted=False)),
                        kind="TABLE",
                        actions=[
                            exp.ColumnDef(
                                this=exp.to_identifier(col, quoted=False),
                                kind=exp.DataType.build(col_type, dialect="sqlite"),
                            )
                        ],
                    ).sql(dialect="sqlite")
                    self.conn.execute(alter_sql)
            self._table_columns[sqlite_name] = tuple(ordered_cols)

        insert_sql = _build_insert_sql(sqlite_name, ordered_cols)
        self.conn.executemany(insert_sql, coerced_rows)
        self.conn.commit()
        return FixtureTableLoadResult(
            target_table=target_table,
            sqlite_table=sqlite_name,
            row_count=len(coerced_rows),
            columns=tuple(ordered_cols),
        )

    def load_jsonl(
        self,
        target_table: str,
        path: Path,
        *,
        append: bool = False,
    ) -> FixtureTableLoadResult:
        """Load a JSONL fixture file into `target_table`."""
        return self.load_rows(target_table, load_jsonl_rows(path), append=append)

    def load_manifest(self, manifest_path: Path) -> list[FixtureTableLoadResult]:
        """Load every fixture table listed in a Silver fixture `manifest.json`."""
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        base_dir = manifest_path.parent
        loaded_targets: set[str] = set()
        results: list[FixtureTableLoadResult] = []
        for entry in manifest.get("fixtures", ()):
            target_table = str(entry["target_table"])
            fixture_path = base_dir / str(entry["file"])
            append = target_table in loaded_targets
            results.append(self.load_jsonl(target_table, fixture_path, append=append))
            loaded_targets.add(target_table)
        return results

    def register_sql(self, sql: str) -> list[str]:
        """Register BigQuery table functions, views, or tables from a SQL script."""
        registered: list[str] = []
        for stmt in sqlglot.parse(sql, read="bigquery"):
            if stmt is None:
                continue
            if isinstance(stmt, exp.Create) and isinstance(stmt.this, exp.UserDefinedFunction):
                defn = self.registry.register_ast(stmt)
                registered.append(defn.to_spec().canonical_key)
                continue
            if (
                isinstance(stmt, exp.Create)
                and str(stmt.args.get("kind", "")).upper() == "VIEW"
                and isinstance(stmt.this, exp.Table)
                and stmt.expression is not None
            ):
                raw_view = ".".join(
                    p for p in (stmt.this.catalog, stmt.this.db, stmt.this.name) if p
                )
                view_name = self._resolve_sqlite_table_name(raw_view)
                select_sql = transpile_bigquery_ast(
                    stmt.expression,
                    registry=self.registry,
                    default_dataset=self.config.default_dataset,
                )
                drop_sql, create_sql = _build_drop_and_create_view_sql(view_name, select_sql)
                self.conn.execute(drop_sql)
                self.conn.execute(create_sql)
                registered.append(view_name)
                continue
            transpiled = transpile_bigquery_ast(
                stmt,
                registry=self.registry,
                default_dataset=self.config.default_dataset,
            )
            self.conn.execute(transpiled)
            registered.append(stmt.key)
        self.conn.commit()
        return registered

    def query(self, sql: str) -> RunnerQueryResult:
        """Execute a BigQuery query on SQLite with TVF inlining and dataset-name mapping."""
        t0 = time.perf_counter()
        stmt = sqlglot.parse_one(sql, read="bigquery")
        sqlite_sql = transpile_bigquery_ast(
            stmt,
            registry=self.registry,
            default_dataset=self.config.default_dataset,
        )
        cur = self.conn.execute(sqlite_sql)
        cols = tuple(desc[0] for desc in (cur.description or ()))
        rows = tuple({col: row[col] for col in cols} for row in cur.fetchall())
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return RunnerQueryResult(
            sql=sql,
            sqlite_sql=sqlite_sql,
            columns=cols,
            rows=rows,
            elapsed_ms=elapsed_ms,
        )

    def assert_query_matches(
        self,
        sql: str,
        expected: Sequence[Mapping[str, object]] | Path,
    ) -> RunnerQueryResult:
        """Run `sql` and fail loudly if empty for non-empty expected rows or on row mismatch."""
        expected_rows: list[dict[str, object]] = (
            load_jsonl_rows(expected)
            if isinstance(expected, Path)
            else [dict(row) for row in expected]
        )
        result = self.query(sql)
        if expected_rows and not result.rows:
            raise EmptyQueryResultError(
                f"Query returned 0 rows for non-empty expected set ({len(expected_rows)} rows). "
                f"Transpiled SQLite SQL: {result.sqlite_sql}"
            )
        actual_rows = [dict(row) for row in result.rows]
        if len(actual_rows) != len(expected_rows) or not all(
            _rows_match(a, e) for a, e in zip(actual_rows, expected_rows, strict=True)
        ):
            first_diff = next(
                (
                    f"row {idx}: actual={a!r} expected={e!r}"
                    for idx, (a, e) in enumerate(zip(actual_rows, expected_rows, strict=False))
                    if not _rows_match(a, e)
                ),
                f"actual_len={len(actual_rows)} expected_len={len(expected_rows)}",
            )
            raise AssertionError(
                f"Query rows did not match expected ({len(actual_rows)} vs {len(expected_rows)}): "
                f"{first_diff}"
            )
        return result


def smoke_silver_fixtures(manifest_path: Path, *, max_seconds: float = 5.0) -> dict[str, object]:
    """Load all manifest fixtures, run nested TVF + view checks, and enforce `< max_seconds`."""
    t0 = time.perf_counter()
    with OfflineSqlRunner(RunnerConfig(current_date_utc="2026-11-26")) as runner:
        loaded = runner.load_manifest(manifest_path)
        runner.register_sql(
            """
            CREATE OR REPLACE TABLE FUNCTION `silver.smoke_spans`(
              from_time TIMESTAMP,
              to_time TIMESTAMP
            ) AS (
              SELECT
                s.trace_id,
                s.span_id,
                s.start_time,
                s.kind,
                s.status.code AS status_code,
                s.instrumentation_scope.name AS scope_name,
                JSON_VALUE(s.resource.attributes, '$."service.name"') AS service_name,
                ARRAY_LENGTH(s.events) AS event_count
              FROM `obs_traces._AllSpans` AS s
              WHERE s.start_time >= from_time AND s.start_time < to_time
            );

            CREATE OR REPLACE TABLE FUNCTION `silver_open.smoke_trace_counts`(
              from_time TIMESTAMP,
              to_time TIMESTAMP
            ) AS (
              SELECT
                COUNT(*) AS span_count,
                COUNT(DISTINCT trace_id) AS trace_count
              FROM `silver.smoke_spans`(from_time, to_time)
            );
            """
        )
        res = runner.query(
            """
            SELECT span_count, trace_count
            FROM `silver_open.smoke_trace_counts`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            """
        )
        if not res.rows or int(str(res.rows[0]["span_count"])) < 95:
            raise EmptyQueryResultError(f"Unexpected smoke span count: {res.rows!r}")
    elapsed = time.perf_counter() - t0
    if elapsed >= max_seconds:
        raise TimeoutError(
            f"Offline Silver fixture run took {elapsed:.3f}s (exceeds {max_seconds:.1f}s ceiling)"
        )
    return {
        "fixture_tables": len(loaded),
        "total_rows_loaded": sum(item.row_count for item in loaded),
        "span_count": res.rows[0]["span_count"],
        "trace_count": res.rows[0]["trace_count"],
        "elapsed_sec": round(elapsed, 4),
    }


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run timed offline Silver fixture smoke.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    summary = smoke_silver_fixtures(args.manifest, max_seconds=args.max_seconds)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
