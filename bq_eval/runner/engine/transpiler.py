"""BigQuery-to-SQLite UDFs, aggregates, and AST transpiler for offline SQL execution."""

from __future__ import annotations

import functools
import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import sqlglot
from sqlglot import exp

from bq_eval.runner.engine.silver_udfs import register_silver_sqlite_udfs, rewrite_silver_bq_expr

SqliteValue = str | int | float | bytes | None

_TZ_SUFFIX_RE = re.compile(r"\s+UTC$", re.IGNORECASE)


@functools.lru_cache(maxsize=4096)
def _normalize_bq_timestamp_str(raw: str) -> str | None:
    cleaned = _TZ_SUFFIX_RE.sub("+00:00", raw)
    if cleaned.endswith(("Z", "z")):
        cleaned = f"{cleaned[:-1]}+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _normalize_bq_timestamp(value: object) -> str | None:
    """Normalize BigQuery timestamp strings to UTC ISO-8601 (`+00:00`) for exact string order."""
    if value is None:
        return None
    raw = str(value).strip()
    return _normalize_bq_timestamp_str(raw) if raw else None


def _sqlite_safe_cast_timestamp(value: object) -> str | None:
    """Mirror BigQuery `SAFE_CAST(x AS TIMESTAMP)` (invalid strings return NULL)."""
    try:
        return _normalize_bq_timestamp(value)
    except ValueError:
        return None


def _json_scalar_to_str(val: object) -> str | None:
    if val is None or isinstance(val, (dict, list)):
        return None
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


_JSON_PATH_TOKEN_RE = re.compile(
    r'\."([^"]+)"|\.\'([^\']+)\'|\["([^"]+)"\]|\[\'([^\']+)\'\]|\[(\d+)\]|\.([^.\[\]]+)'
)


def _parse_json_path_tokens(path: str) -> list[str | int]:
    body = path[1:] if path.startswith("$") else path
    if not body:
        return []
    tokens: list[str | int] = []
    pos = 0
    for match in _JSON_PATH_TOKEN_RE.finditer(body):
        if match.start() != pos:
            return []
        pos = match.end()
        if match.group(5) is not None:
            tokens.append(int(match.group(5)))
        else:
            key = next(g for g in (*match.groups()[:4], match.group(6)) if g is not None)
            tokens.append(key)
    return tokens if pos == len(body) else []


def _walk_json_tokens(data: object, tokens: list[str | int]) -> tuple[bool, object]:
    cur: object = data
    for tok in tokens:
        if isinstance(tok, int):
            if isinstance(cur, list) and 0 <= tok < len(cur):
                cur = cur[tok]
            else:
                return False, None
        elif isinstance(cur, dict) and tok in cur:
            cur = cur[tok]
        else:
            return False, None
    return True, cur


@functools.lru_cache(maxsize=8192)
def _cached_json_value(raw_json: str, path: str) -> str | None:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if path.strip() == "$":
        return _json_scalar_to_str(data)
    tokens = _parse_json_path_tokens(path)
    if tokens:
        matched, cur = _walk_json_tokens(data, tokens)
        if matched:
            return _json_scalar_to_str(cur)
    flat_key = (
        ".".join(str(tok) for tok in tokens)
        if tokens and all(isinstance(tok, str) for tok in tokens)
        else path.removeprefix("$.").replace('"', "")
    )
    if isinstance(data, dict) and flat_key in data:
        return _json_scalar_to_str(data[flat_key])
    return None


def _sqlite_json_value(raw_json: str | None, path: str) -> str | None:
    if not raw_json or not isinstance(raw_json, str) or not isinstance(path, str):
        return None
    return _cached_json_value(raw_json, path)


def _sqlite_struct_field(raw_json: object, dotted_path: str) -> SqliteValue:
    """Extract a nested field from a JSON-encoded BigQuery STRUCT stored in SQLite."""
    if not isinstance(raw_json, str):
        return None
    try:
        cur: object = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    for part in dotted_path.split("."):
        if not (isinstance(cur, dict) and part in cur):
            return None
        cur = cur[part]
    result: SqliteValue
    if cur is None:
        result = None
    elif isinstance(cur, bool):
        result = 1 if cur else 0
    elif isinstance(cur, (dict, list)):
        result = json.dumps(cur, sort_keys=True)
    elif isinstance(cur, (str, int, float, bytes)):
        result = cur
    else:
        result = str(cur)
    return result


def _sqlite_array_length(raw_json: object) -> int | None:
    if raw_json is None:
        return None
    if not isinstance(raw_json, str):
        return None
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    return len(parsed) if isinstance(parsed, list) else None


@functools.lru_cache(maxsize=512)
def _compiled_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


@functools.lru_cache(maxsize=4096)
def _cached_regexp_replace(value: str, pattern: str, replacement: str) -> str:
    return _compiled_regex(pattern).sub(replacement, value)


def _sqlite_regexp_replace(value: str | None, pattern: str, replacement: str) -> str | None:
    if value is None:
        return None
    return _cached_regexp_replace(str(value), str(pattern), str(replacement))


def _parse_utc_dt(ts: str) -> datetime:
    norm = _normalize_bq_timestamp(ts)
    if norm is None:
        raise ValueError(f"Empty timestamp: {ts!r}")
    return datetime.fromisoformat(norm)


_TIMESTAMP_UNIT_TO_US: dict[str, int] = {
    "MICROSECOND": 1,
    "MILLISECOND": 1_000,
    "SECOND": 1_000_000,
    "MINUTE": 60_000_000,
    "HOUR": 3_600_000_000,
    "DAY": 86_400_000_000,
    "WEEK": 604_800_000_000,
}


def _sqlite_timestamp_diff(
    later: str | None,
    earlier: str | None,
    unit: str,
) -> int | None:
    if later is None or earlier is None:
        return None
    unit_key = unit.upper()
    divisor_us = _TIMESTAMP_UNIT_TO_US.get(unit_key)
    if divisor_us is None:
        raise ValueError(f"Unsupported TIMESTAMP_DIFF unit: {unit!r}")
    delta = _parse_utc_dt(later) - _parse_utc_dt(earlier)
    total_us = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return int(total_us / divisor_us)


def _sqlite_timestamp_diff_ms(later: str | None, earlier: str | None) -> float | None:
    if later is None or earlier is None:
        return None
    return (_parse_utc_dt(later) - _parse_utc_dt(earlier)).total_seconds() * 1000.0


def _sqlite_format_timestamp(fmt: str, ts: str | None) -> str | None:
    if ts is None:
        return None
    py_fmt = fmt.replace("%E6S", "%S.%f").replace("%E3S", "%S.%f")
    return _parse_utc_dt(ts).strftime(py_fmt)


_TIMESTAMP_TRUNC_UNITS: frozenset[str] = frozenset(
    {"MICROSECOND", "MILLISECOND", "SECOND", "MINUTE", "HOUR", "DAY", "WEEK"}
)


def _sqlite_timestamp_trunc(ts: str | None, unit: str) -> str | None:
    if ts is None:
        return None
    dt = _parse_utc_dt(ts)
    unit_key = unit.upper()
    if unit_key == "MICROSECOND":
        out = dt
    elif unit_key == "MILLISECOND":
        out = dt.replace(microsecond=(dt.microsecond // 1000) * 1000)
    elif unit_key == "SECOND":
        out = dt.replace(microsecond=0)
    elif unit_key == "MINUTE":
        out = dt.replace(second=0, microsecond=0)
    elif unit_key == "HOUR":
        out = dt.replace(minute=0, second=0, microsecond=0)
    elif unit_key == "DAY":
        out = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif unit_key == "WEEK":
        days_since_sunday = (dt.weekday() + 1) % 7
        out = (dt - timedelta(days=days_since_sunday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        raise ValueError(f"Unsupported TIMESTAMP_TRUNC unit: {unit!r}")
    return out.isoformat()


def _sqlite_timestamp_trunc_hour(ts: str | None) -> str | None:
    return _sqlite_timestamp_trunc(ts, "HOUR")


def _sqlite_timestamp_sub_us(ts: str | None, us: float | int | None) -> str | None:
    if ts is None:
        return None
    return (_parse_utc_dt(ts) - timedelta(microseconds=int(float(us or 0)))).isoformat()


def _sqlite_timestamp_add_us(ts: str | None, us: float | int | None) -> str | None:
    if ts is None:
        return None
    return (_parse_utc_dt(ts) + timedelta(microseconds=int(float(us or 0)))).isoformat()


def _sqlite_timestamp_sub_ms(ts: str | None, ms: float | int | None) -> str | None:
    if ts is None:
        return None
    delta_ms = int(float(ms or 0))
    return (_parse_utc_dt(ts) - timedelta(milliseconds=delta_ms)).isoformat()


def _sqlite_timestamp_add_ms(ts: str | None, ms: float | int | None) -> str | None:
    if ts is None:
        return None
    delta_ms = int(float(ms or 0))
    return (_parse_utc_dt(ts) + timedelta(milliseconds=delta_ms)).isoformat()


def _sqlite_unix_seconds(ts: str | None) -> int | None:
    if ts is None:
        return None
    return int(_parse_utc_dt(ts).timestamp())


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
    return 1 if _compiled_regex(str(pattern)).search(str(value)) is not None else 0


def _sqlite_regexp_extract(value: str | None, pattern: str | None) -> str | None:
    if value is None or pattern is None:
        return None
    m = _compiled_regex(str(pattern)).search(str(value))
    if m is None:
        return None
    return m.group(1) if m.lastindex else m.group(0)


def _sqlite_starts_with(value: str | None, prefix: str | None) -> int:
    if value is None or prefix is None:
        return 0
    return 1 if str(value).startswith(str(prefix)) else 0


def _sqlite_safe_cast_bool(value: object) -> int | None:
    """Mirror BigQuery `SAFE_CAST(x AS BOOL)` (case-insensitive exact 'true'/'false', no trim)."""
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
        text = str(value).strip()
        if text.lower().startswith("0x"):
            return int(text, 16)
        return int(text)
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


def _sqlite_date_add_days(date_str: str | None, days: int | float | None) -> str | None:
    if date_str is None:
        return None
    dt = datetime.fromisoformat(str(date_str)[:10])
    return (dt + timedelta(days=int(days or 0))).strftime("%Y-%m-%d")


def _sqlite_format_date(fmt: str, date_str: str | None) -> str | None:
    if date_str is None:
        return None
    dt = datetime.fromisoformat(str(date_str)[:10])
    return dt.strftime(fmt)


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


class _SQLiteQuantileAgg:
    """Exact BigQuery `APPROX_QUANTILES(val, buckets)[OFFSET(offset)]` aggregate for SQLite."""

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
    """SQLite aggregate for `ARRAY_AGG(val [IGNORE NULLS] ORDER BY k1, k2 LIMIT 1)[SAFE_OFFSET(0)]`."""

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
    """SQLite aggregate for `STRING_AGG(val, sep ORDER BY k ASC)`."""

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
    session_user: str = "runner@example.invalid",
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ts_now = _normalize_bq_timestamp(current_timestamp_utc or f"{current_date_utc}T12:00:00+00:00")
    conn.create_function("JSON_VALUE", 2, _sqlite_json_value)
    conn.create_function("BQ_JSON_VALUE", 2, _sqlite_json_value)
    conn.create_function("BQ_STRUCT_FIELD", 2, _sqlite_struct_field)
    conn.create_function("BQ_ARRAY_LENGTH", 1, _sqlite_array_length)
    conn.create_function("BQ_TIMESTAMP", 1, _normalize_bq_timestamp)
    conn.create_function("BQ_SAFE_CAST_TIMESTAMP", 1, _sqlite_safe_cast_timestamp)
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
    conn.create_function("BQ_DATE_ADD_DAYS", 2, _sqlite_date_add_days)
    conn.create_function("BQ_FORMAT_DATE", 2, _sqlite_format_date)
    conn.create_function("BQ_GENERATE_UUID", 0, lambda: str(uuid.uuid4()))
    conn.create_function("GENERATE_UUID", 0, lambda: str(uuid.uuid4()))
    conn.create_function("SQRT", 1, math.sqrt)
    conn.create_function("POW", 2, math.pow)
    conn.create_function("CEIL", 1, math.ceil)
    conn.create_function("TIMESTAMP_DIFF_MS", 2, _sqlite_timestamp_diff_ms)
    conn.create_function("BQ_TIMESTAMP_DIFF", 3, _sqlite_timestamp_diff)
    conn.create_function("BQ_TIMESTAMP_SUB_MS", 2, _sqlite_timestamp_sub_ms)
    conn.create_function("BQ_TIMESTAMP_ADD_MS", 2, _sqlite_timestamp_add_ms)
    conn.create_function("BQ_TIMESTAMP_SUB_US", 2, _sqlite_timestamp_sub_us)
    conn.create_function("BQ_TIMESTAMP_ADD_US", 2, _sqlite_timestamp_add_us)
    conn.create_function("BQ_UNIX_SECONDS", 1, _sqlite_unix_seconds)
    conn.create_function("BQ_TIMESTAMP_SECONDS", 1, _sqlite_timestamp_seconds)
    conn.create_function("BQ_INT_DIV", 2, _sqlite_int_div)
    conn.create_function("FORMAT_TIMESTAMP", 2, _sqlite_format_timestamp)
    conn.create_function("BQ_TIMESTAMP_TRUNC", 2, _sqlite_timestamp_trunc)
    conn.create_function("BQ_TIMESTAMP_TRUNC_HOUR", 1, _sqlite_timestamp_trunc_hour)
    conn.create_function("BQ_CURRENT_DATE", 0, lambda: current_date_utc)
    conn.create_function("BQ_CURRENT_TIMESTAMP", 0, lambda: ts_now)
    conn.create_function("BQ_SESSION_USER", 0, lambda: session_user)
    conn.create_function("SESSION_USER", 0, lambda: session_user)
    conn.create_function("BQ_DATE", 1, lambda ts: str(ts)[:10] if ts is not None else None)
    conn.create_function("JSON_FORMAT", 1, lambda v: "null" if v is None else str(v))
    conn.create_function("TO_JSON_STRING", 1, lambda v: "null" if v is None else str(v))
    # typeshed's sqlite3._AggregateProtocol models only 1-arg step(); these aggregates take 3-5 args.
    conn.create_aggregate("BQ_APPROX_QUANTILE", 3, _SQLiteQuantileAgg)  # type: ignore[arg-type]
    conn.create_aggregate("BQ_ARRAY_FIRST", 5, _SQLiteArrayFirstAgg)  # type: ignore[arg-type]
    conn.create_aggregate("BQ_STRING_AGG_ASC", 3, _SQLiteStringAggAsc)  # type: ignore[arg-type]
    register_silver_sqlite_udfs(
        conn,
        parse_tokens=_parse_json_path_tokens,
        walk_tokens=_walk_json_tokens,
    )
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
        if node.to.is_type(
            exp.DataType.Type.TIMESTAMP,
            exp.DataType.Type.TIMESTAMPTZ,
            exp.DataType.Type.DATETIME,
        ):
            return exp.Anonymous(this="BQ_SAFE_CAST_TIMESTAMP", expressions=[node.this.copy()])
    return exp.Cast(this=node.this, to=node.to)


def _rewrite_bq_cast(node: exp.Cast) -> exp.Expr:
    if not isinstance(node.to, exp.DataType):
        return node
    if node.to.is_type(exp.DataType.Type.JSON) and isinstance(node.this, exp.Expr):
        return node.this.copy()
    if node.to.is_type(
        exp.DataType.Type.TIMESTAMP,
        exp.DataType.Type.TIMESTAMPTZ,
        exp.DataType.Type.DATETIME,
    ):
        return exp.Anonymous(this="BQ_TIMESTAMP", expressions=[node.this.copy()])
    return node


_ANONYMOUS_SQLITE_FN_MAP: dict[str, str] = {
    "JSON_SET": "BQ_JSON_SET",
    "STARTS_WITH": "BQ_STARTS_WITH",
    "UNIX_SECONDS": "BQ_UNIX_SECONDS",
    "TIMESTAMP_SECONDS": "BQ_TIMESTAMP_SECONDS",
    "DIV": "BQ_INT_DIV",
    "FARM_FINGERPRINT": "BQ_FARM_FINGERPRINT",
    "TIMESTAMP": "BQ_TIMESTAMP",
    "ARRAY_LENGTH": "BQ_ARRAY_LENGTH",
    "CHAR_LENGTH": "LENGTH",
    "CHARACTER_LENGTH": "LENGTH",
    "STRPOS": "INSTR",
    "FORMAT_DATE": "BQ_FORMAT_DATE",
    "GENERATE_UUID": "BQ_GENERATE_UUID",
}

_TEMPORAL_SINGLE_ARG_MAP: tuple[tuple[type[exp.Expr], str], ...] = (
    (exp.Date, "BQ_DATE"),
    (exp.UnixToTime, "BQ_TIMESTAMP_SECONDS"),
    (exp.TimeToUnix, "BQ_UNIX_SECONDS"),
    (exp.UnixSeconds, "BQ_UNIX_SECONDS"),
    (exp.Timestamp, "BQ_TIMESTAMP"),
)


def _extract_temporal_unit(node: exp.Expr, *, default: str | None = None) -> str:
    unit_node = node.args.get("unit")
    if isinstance(unit_node, exp.WeekStart):
        day = unit_node.name.upper() or "SUNDAY"
        return "WEEK" if day == "SUNDAY" else f"WEEK({day})"
    if isinstance(unit_node, exp.Expr) and unit_node.name:
        return unit_node.name.upper()
    if default is not None:
        return default
    raise ValueError(f"Missing temporal unit on {type(node).__name__}: {node.sql('bigquery')}")


def _rewrite_bq_timestamp_delta(
    node: exp.TimestampSub | exp.TimestampAdd,
    *,
    fn_name: str,
) -> exp.Expr:
    unit_name = _extract_temporal_unit(node)
    factor = _TIMESTAMP_UNIT_TO_US.get(unit_name)
    if factor is None:
        raise ValueError(f"Unsupported TIMESTAMP_ADD/SUB unit: {unit_name!r}")
    raw_qty = node.expression.copy()
    if factor == 1:
        delta_expr: exp.Expr = raw_qty
    else:
        qty = (
            raw_qty
            if isinstance(raw_qty, (exp.Literal, exp.Column, exp.Paren))
            else exp.Paren(this=raw_qty)
        )
        delta_expr = exp.Mul(this=qty, expression=exp.Literal.number(factor))
    return exp.Anonymous(this=fn_name, expressions=[node.this.copy(), delta_expr])


_TEMPORAL_TWO_ARG_MAP: tuple[tuple[type[exp.Expr], str], ...] = (
    (exp.DateSub, "BQ_DATE_SUB_DAYS"),
    (exp.DateAdd, "BQ_DATE_ADD_DAYS"),
    (exp.IntDiv, "BQ_INT_DIV"),
)


def _rewrite_bq_timestamp_unit_fn(node: exp.TimestampTrunc | exp.TimestampDiff) -> exp.Expr:
    unit_name = _extract_temporal_unit(node)
    if isinstance(node, exp.TimestampTrunc):
        if unit_name not in _TIMESTAMP_TRUNC_UNITS:
            raise ValueError(f"Unsupported TIMESTAMP_TRUNC unit: {unit_name!r}")
        return exp.Anonymous(
            this="BQ_TIMESTAMP_TRUNC",
            expressions=[node.this.copy(), exp.Literal.string(unit_name)],
        )
    if unit_name not in _TIMESTAMP_UNIT_TO_US:
        raise ValueError(f"Unsupported TIMESTAMP_DIFF unit: {unit_name!r}")
    return exp.Anonymous(
        this="BQ_TIMESTAMP_DIFF",
        expressions=[node.this.copy(), node.expression.copy(), exp.Literal.string(unit_name)],
    )


def _rewrite_bq_temporal(node: exp.Expr) -> exp.Expr | None:
    rewritten: exp.Expr | None = None
    single = dict(_TEMPORAL_SINGLE_ARG_MAP).get(type(node))
    two = dict(_TEMPORAL_TWO_ARG_MAP).get(type(node))
    if single is not None:
        rewritten = exp.Anonymous(this=single, expressions=[node.this.copy()])
    elif two is not None:
        rewritten = exp.Anonymous(this=two, expressions=[node.this.copy(), node.expression.copy()])
    elif isinstance(node, (exp.TimestampTrunc, exp.TimestampDiff)):
        rewritten = _rewrite_bq_timestamp_unit_fn(node)
    elif isinstance(node, exp.TimestampSub):
        rewritten = _rewrite_bq_timestamp_delta(node, fn_name="BQ_TIMESTAMP_SUB_US")
    elif isinstance(node, exp.TimestampAdd):
        rewritten = _rewrite_bq_timestamp_delta(node, fn_name="BQ_TIMESTAMP_ADD_US")
    elif isinstance(node, (exp.CurrentDate, exp.CurrentTimestamp)):
        fn = "BQ_CURRENT_DATE" if isinstance(node, exp.CurrentDate) else "BQ_CURRENT_TIMESTAMP"
        rewritten = exp.Anonymous(this=fn, expressions=[])
    elif isinstance(node, exp.TimeToStr):
        fn_out = (
            "BQ_FORMAT_DATE"
            if isinstance(node.this, (exp.Date, exp.CurrentDate))
            else "FORMAT_TIMESTAMP"
        )
        rewritten = exp.Anonymous(
            this=fn_out,
            expressions=[node.args["format"].copy(), node.this.copy()],
        )
    elif isinstance(node, exp.Uuid):
        rewritten = exp.Anonymous(this="BQ_GENERATE_UUID", expressions=[])
    return rewritten


def _rewrite_bq_anonymous(node: exp.Anonymous) -> exp.Expr:
    upper_name = node.name.upper()
    if upper_name in {"SESSION_USER", "CURRENT_USER"}:
        return exp.Anonymous(this="BQ_SESSION_USER", expressions=[])
    mapped = _ANONYMOUS_SQLITE_FN_MAP.get(upper_name)
    if mapped is not None:
        return exp.Anonymous(this=mapped, expressions=[e.copy() for e in node.expressions])
    cls = _SQLITE_AGG_MAP.get(upper_name)
    if cls is not None and node.expressions:
        return cls(this=node.expressions[0])
    return node


_TWO_ARG_STRING_MAP: tuple[tuple[type[exp.Expr], str], ...] = (
    (exp.RegexpLike, "BQ_REGEXP_CONTAINS"),
    (exp.RegexpExtract, "BQ_REGEXP_EXTRACT"),
    (exp.StartsWith, "BQ_STARTS_WITH"),
    (exp.StrPosition, "INSTR"),
)


def _json_path_expr(node: exp.Expr) -> exp.Expr:
    if not isinstance(node, exp.JSONPath):
        return node.copy()
    parts: list[str] = ["$"]
    for part in node.expressions:
        if isinstance(part, exp.JSONPathKey):
            escaped = str(part.this).replace('"', '\\"')
            parts.append(f'["{escaped}"]')
        elif isinstance(part, exp.JSONPathSubscript):
            parts.append(f"[{part.this}]")
    return exp.Literal.string("".join(parts))


def _rewrite_bq_string_and_agg(node: exp.Expr) -> exp.Expr | None:
    rewritten: exp.Expr | None = None
    two_arg = dict(_TWO_ARG_STRING_MAP).get(type(node))
    if two_arg is not None:
        rhs = node.expression if node.expression is not None else node.args.get("substr")
        if isinstance(rhs, exp.Expr):
            rewritten = exp.Anonymous(this=two_arg, expressions=[node.this.copy(), rhs.copy()])
    elif isinstance(node, exp.JSONExtractScalar) and node.expression is not None:
        rewritten = exp.Anonymous(
            this="BQ_JSON_VALUE",
            expressions=[node.this.copy(), _json_path_expr(node.expression)],
        )
    elif isinstance(node, (exp.AnyValue, exp.LogicalOr)):
        rewritten = exp.Max(this=node.this)
    elif isinstance(node, exp.LogicalAnd):
        rewritten = exp.Min(this=node.this)
    elif isinstance(node, exp.Bracket):
        rewritten = _rewrite_bq_bracket(node)
    elif isinstance(node, exp.GroupConcat):
        inner = node.this.copy()
        sep = node.args.get("separator") or exp.Literal.string(",")
        if isinstance(inner, exp.Order) and inner.expressions:
            rewritten = exp.Anonymous(
                this="BQ_STRING_AGG_ASC",
                expressions=[inner.this.copy(), sep.copy(), inner.expressions[0].this.copy()],
            )
    elif isinstance(node, exp.ArraySize):
        rewritten = exp.Anonymous(this="BQ_ARRAY_LENGTH", expressions=[node.this.copy()])
    return rewritten


def _rewrite_bq_scalar(node: exp.Expr) -> exp.Expr | None:
    rewritten: exp.Expr | None = None
    if isinstance(node, exp.SafeDivide):
        num = node.this.copy().sql("sqlite")
        den = node.expression.copy().sql("sqlite")
        rewritten = sqlglot.parse_one(
            f"CASE WHEN ({den}) IS NULL OR ({den}) = 0 THEN NULL ELSE CAST(({num}) AS REAL) / ({den}) END",
            read="sqlite",
        )
    elif isinstance(node, exp.TryCast):
        rewritten = _rewrite_bq_try_cast(node)
    elif isinstance(node, exp.Cast):
        rewritten = _rewrite_bq_cast(node)
    elif isinstance(node, exp.Mod):
        rewritten = exp.Anonymous(
            this="BQ_MOD", expressions=[node.this.copy(), node.expression.copy()]
        )
    elif isinstance(node, (exp.SessionUser, exp.CurrentUser)):
        rewritten = exp.Anonymous(this="BQ_SESSION_USER", expressions=[])
    elif isinstance(node, exp.FarmFingerprint):
        fp_args = (
            [e.copy() for e in node.expressions]
            if node.expressions
            else ([node.this.copy()] if node.this is not None else [])
        )
        rewritten = exp.Anonymous(this="BQ_FARM_FINGERPRINT", expressions=fp_args)
    return rewritten


def _rewrite_bq_node_for_sqlite(node: exp.Expr) -> exp.Expr:
    silver_rewritten = rewrite_silver_bq_expr(node, json_path_fn=_json_path_expr)
    if silver_rewritten is not None:
        return silver_rewritten
    for helper in (_rewrite_bq_scalar, _rewrite_bq_string_and_agg, _rewrite_bq_temporal):
        rewritten = helper(node)
        if rewritten is not None:
            return rewritten
    if isinstance(node, exp.Select) and node.args.get("qualify") is not None:
        return _rewrite_bq_qualify(node)
    if isinstance(node, exp.Anonymous):
        return _rewrite_bq_anonymous(node)
    return node


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
    rewritten = ast_node
    for _ in range(4):
        rewritten = rewritten.transform(_rewrite_bq_node_for_sqlite)
    return rewritten.sql(dialect="sqlite")
