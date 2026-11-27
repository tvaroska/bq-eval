"""SQLite UDF shims and RFC 8785 JCS functions for BigQuery Silver SQL execution."""

from __future__ import annotations

import functools
import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Mapping

from sqlglot import exp

JsonWalkFn = Callable[[str], list[str | int]]
JsonStepFn = Callable[[object, list[str | int]], tuple[bool, object]]

MAX_JCS_INPUT_BYTES = 65_532
MAX_JCS_DEPTH = 64
_BQ_JSON_BOOL_TRUE = "__BQ_JSON_BOOL_TRUE__"
_BQ_JSON_BOOL_FALSE = "__BQ_JSON_BOOL_FALSE__"

DEFAULT_SILVER_SQL_PARAMS: Mapping[str, str] = {
    "PROJECT_ID": "showcase-project",
    "LOCATION": "US",
    "SILVER_INTERNAL_DATASET": "silver_internal",
    "SILVER_REF_DATASET": "silver_ref",
    "SILVER_OPEN_DATASET": "silver_open",
    "SILVER_RESTRICTED_DATASET": "silver_restricted",
    "TRACES_DATASET_ID": "obs_traces",
    "PLATFORM_TRACES_DATASET_ID": "platform_traces",
    "PLATFORM_TRACES_TABLE_ID": "_AllSpans",
    "LOGS_DATASET_ID": "obs_logs",
    "READ_MARGIN_MINUTES": "60",
    "SETTLE_MINUTES": "5",
    "OUTCOME_LOOKBACK_DAYS": "30",
    "CONNECTION_ID": "us.vertex_ai_connection",
    "PAYLOAD_BUCKET": "showcase-project-payloads",
    "SHAPE3_PREFIX": "",
    "SHAPE3_FORMAT": "JSON",
    "DATASET_ID": "agent_eval",
    "GOLD_LOOKBACK_HOURS": "720",
}

SILVER_MANIFEST_TABLE_MAP: Mapping[str, str] = {
    "app_registry": "silver_ref.app_registry",
    "tools": "silver_ref.tools",
    "skill_registry": "silver_ref.skill_registry",
    "capacity_pools": "silver_ref.capacity_pools",
    "capacity_allocations": "silver_ref.capacity_allocations",
    "allowed_models": "silver_ref.allowed_models",
    "burst_metrics_1m": "silver_open.burst_metrics_1m",
    "unit_entitlements": "silver_internal.unit_entitlements",
    "workflow_events_sor": "silver_internal.workflow_events_sor",
    "workflow_events_platform": "silver_internal.workflow_events_platform",
    "workflow_events_app": "silver_internal.workflow_events_app",
}

_SCALAR_UDF_MAP: Mapping[str, str] = {
    "jcs_canonicalize": "BQ_JCS_CANONICALIZE",
    "jcs_sha256": "BQ_JCS_SHA256",
    "jcs_tool_args_sha256": "BQ_JCS_TOOL_ARGS_SHA256",
}


def render_silver_sql(sql: str, params: Mapping[str, str] | None = None) -> str:
    """Substitute `${PARAM}` placeholders in Silver SQL files using ADR-048 defaults."""
    merged = dict(DEFAULT_SILVER_SQL_PARAMS)
    if params:
        merged.update(params)
    rendered = sql
    for key, val in merged.items():
        rendered = rendered.replace(f"${{{key}}}", val)
    return rendered


def _compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _format_es6_number(value: int | float) -> str | None:
    f = float(value)
    if not math.isfinite(f):
        return None
    if f == 0.0:
        return "0"
    sign = "-" if f < 0 else ""
    raw = repr(abs(f))
    if "e" in raw or "E" in raw:
        mantissa_s, exp_s = raw.lower().split("e", 1)
        exp_val = int(exp_s)
        digits = mantissa_s.replace(".", "")
    else:
        int_part, _, frac_part = raw.partition(".")
        if int_part == "0":
            stripped = frac_part.lstrip("0")
            exp_val = -(len(frac_part) - len(stripped) + 1)
            digits = stripped
        else:
            exp_val = len(int_part) - 1
            digits = int_part + frac_part
    digits = digits.rstrip("0") or "0"
    if -6 <= exp_val < 21:
        pos = exp_val + 1
        if pos >= len(digits):
            return sign + digits + ("0" * (pos - len(digits)))
        if pos > 0:
            return sign + digits[:pos] + "." + digits[pos:]
        return sign + "0." + ("0" * (-pos)) + digits
    exp_sign = "+" if exp_val >= 0 else "-"
    mant = digits if len(digits) == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{mant}e{exp_sign}{abs(exp_val)}"


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _jcs_canonicalize_dict(mapping: dict[object, object], depth: int) -> str | None:
    if depth > MAX_JCS_DEPTH:
        return None
    str_items: list[tuple[str, object]] = []
    for k, v in mapping.items():
        ks = str(k)
        if _has_lone_surrogate(ks):
            return None
        str_items.append((ks, v))
    items = sorted(str_items, key=lambda kv: kv[0].encode("utf-16-be"))
    pairs: list[str] = []
    for k, v in items:
        cv = _jcs_canonicalize_obj(v, depth + 1)
        if cv is None:
            return None
        pairs.append(f"{json.dumps(k, ensure_ascii=False)}:{cv}")
    return "{" + ",".join(pairs) + "}"


def _jcs_canonicalize_obj(value: object, depth: int = 0) -> str | None:
    if depth > MAX_JCS_DEPTH:
        return None
    result: str | None = None
    if value is None:
        result = "null"
    elif isinstance(value, bool):
        result = "true" if value else "false"
    elif isinstance(value, (int, float)):
        result = _format_es6_number(value)
    elif isinstance(value, str):
        result = None if _has_lone_surrogate(value) else json.dumps(value, ensure_ascii=False)
    elif isinstance(value, list):
        parts = [_jcs_canonicalize_obj(item, depth + 1) for item in value]
        if all(p is not None for p in parts):
            result = "[" + ",".join(p for p in parts if p is not None) + "]"
    elif isinstance(value, dict):
        result = _jcs_canonicalize_dict(value, depth)
    return result


@functools.lru_cache(maxsize=4096)
def _cached_jcs_canonicalize(raw_json: str) -> str | None:
    try:
        if _has_lone_surrogate(raw_json) or len(raw_json.encode("utf-8")) > MAX_JCS_INPUT_BYTES:
            return None
        return _jcs_canonicalize_obj(json.loads(raw_json))
    except Exception:  # noqa: BLE001
        return None


def jcs_canonicalize_str(raw_json: object) -> str | None:
    """RFC 8785 JCS canonicalization with 65,532-byte UTF-8, depth, and surrogate guards (`ADR-041`)."""
    if raw_json is None or not isinstance(raw_json, str):
        return None
    return _cached_jcs_canonicalize(raw_json)


@functools.lru_cache(maxsize=4096)
def _cached_jcs_sha256(raw_json: str) -> str | None:
    canon = _cached_jcs_canonicalize(raw_json)
    return None if canon is None else hashlib.sha256(canon.encode("utf-8")).hexdigest()


def jcs_sha256_str(raw_json: object) -> str | None:
    """RFC 8785 JCS SHA-256 hex digest with 65,532-byte, depth, and surrogate guards (`ADR-041`)."""
    if raw_json is None or not isinstance(raw_json, str):
        return None
    return _cached_jcs_sha256(raw_json)


@functools.lru_cache(maxsize=4096)
def _cached_jcs_tool_args_sha256(tool_name: str, raw_args: str) -> str | None:
    try:
        if (
            _has_lone_surrogate(tool_name)
            or _has_lone_surrogate(raw_args)
            or len(raw_args.encode("utf-8")) > MAX_JCS_INPUT_BYTES
        ):
            return None
        canon = _jcs_canonicalize_obj({"tool_name": tool_name, "args": json.loads(raw_args)})
        if canon is None:
            return None
        encoded = canon.encode("utf-8")
        return None if len(encoded) > MAX_JCS_INPUT_BYTES else hashlib.sha256(encoded).hexdigest()
    except Exception:  # noqa: BLE001
        return None


def jcs_tool_args_sha256_str(tool_name: object, raw_args: object) -> str | None:
    """RFC 8785 JCS SHA-256 of `{\"tool_name\": ..., \"args\": ...}` (`ADR-041`)."""
    if not isinstance(tool_name, str) or not isinstance(raw_args, str):
        return None
    return _cached_jcs_tool_args_sha256(tool_name, raw_args)


def make_json_query_fn(
    parse_tokens: JsonWalkFn,
    walk_tokens: JsonStepFn,
) -> Callable[[str | None, str], str | None]:
    """Build SQLite `BQ_JSON_QUERY(raw_json, path)` returning compact JSON or NULL."""

    @functools.lru_cache(maxsize=4096)
    def _cached_query(raw_json: str, path: str) -> str | None:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            return None
        if path.strip() == "$":
            return _compact_json(data)
        tokens = parse_tokens(path)
        if tokens:
            matched, cur = walk_tokens(data, tokens)
            if matched and cur is not None:
                return _compact_json(cur)
        flat_key = (
            ".".join(str(tok) for tok in tokens)
            if tokens and all(isinstance(tok, str) for tok in tokens)
            else path.removeprefix("$.").replace('"', "")
        )
        if isinstance(data, dict) and flat_key in data and data[flat_key] is not None:
            return _compact_json(data[flat_key])
        return None

    def _sqlite_json_query(raw_json: str | None, path: str) -> str | None:
        if not raw_json or not isinstance(raw_json, str) or not isinstance(path, str):
            return None
        return _cached_query(raw_json, path)

    return _sqlite_json_query


@functools.lru_cache(maxsize=4096)
def _cached_parse_json(stripped: str) -> str | None:
    try:
        return _compact_json(json.loads(stripped))
    except json.JSONDecodeError:
        return None


def _sqlite_parse_json(raw: object) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return _cached_parse_json(stripped) if stripped else None


def _sqlite_json_array(*args: object) -> str:
    return _compact_json(list(args))


def _sqlite_json_object(*args: object) -> str:
    out: dict[str, object] = {}
    for idx in range(0, len(args) - 1, 2):
        key = args[idx]
        if key is None:
            continue
        val = args[idx + 1]
        if val == _BQ_JSON_BOOL_TRUE:
            out[str(key)] = True
            continue
        if val == _BQ_JSON_BOOL_FALSE:
            out[str(key)] = False
            continue
        if isinstance(val, str) and val[:1] in "{[":
            try:
                parsed = json.loads(val)
                if isinstance(parsed, (dict, list)):
                    out[str(key)] = parsed
                    continue
            except json.JSONDecodeError:
                pass
        out[str(key)] = val
    return _compact_json(out)


def _strip_nulls_recursive(obj: object) -> object:
    if isinstance(obj, dict):
        return {k: _strip_nulls_recursive(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls_recursive(item) for item in obj]
    return obj


def _sqlite_json_strip_nulls(raw: object) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _compact_json(_strip_nulls_recursive(parsed))


def _sqlite_json_array_contains(raw_json_array: object, target: object) -> int:
    if raw_json_array is None or target is None or not isinstance(raw_json_array, str):
        return 0
    try:
        parsed = json.loads(raw_json_array)
    except json.JSONDecodeError:
        return 0
    if not isinstance(parsed, list):
        return 0
    target_str = str(target)
    return 1 if any(item is not None and str(item) == target_str for item in parsed) else 0


def _sqlite_json_ensure_array(raw: object) -> str:
    if raw is None or not isinstance(raw, str):
        return "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "[]"
    if isinstance(parsed, list):
        return _compact_json(parsed)
    return "[]"


def _sqlite_greatest(*args: object) -> str | int | float | bytes | None:
    if not args or any(a is None for a in args):
        return None
    best = args[0]
    for item in args[1:]:
        if item > best:  # type: ignore[operator]
            best = item
    return best if isinstance(best, (str, int, float, bytes)) else str(best)


def _sqlite_least(*args: object) -> str | int | float | bytes | None:
    if not args or any(a is None for a in args):
        return None
    best = args[0]
    for item in args[1:]:
        if item < best:  # type: ignore[operator]
            best = item
    return best if isinstance(best, (str, int, float, bytes)) else str(best)


def _sqlite_sha256_hex(raw: object) -> str | None:
    if raw is None:
        return None
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()


def _sqlite_json_array_agg_elem(raw: object) -> object:
    if raw is None:
        return None
    if isinstance(raw, str) and raw[:1] in "{[":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _sqlite_lpad(value: object, length: object, pad: object = " ") -> str | None:
    if value is None or length is None or pad is None:
        return None
    text = str(value)
    target_len = int(str(length))
    if target_len <= 0:
        return ""
    if len(text) >= target_len:
        return text[:target_len]
    pad_str = str(pad) or " "
    needed = target_len - len(text)
    repeats = (needed // len(pad_str)) + 1
    return (pad_str * repeats)[:needed] + text


def _sqlite_rpad(value: object, length: object, pad: object = " ") -> str | None:
    if value is None or length is None or pad is None:
        return None
    text = str(value)
    target_len = int(str(length))
    if target_len <= 0:
        return ""
    if len(text) >= target_len:
        return text[:target_len]
    pad_str = str(pad) or " "
    needed = target_len - len(text)
    repeats = (needed // len(pad_str)) + 1
    return text + (pad_str * repeats)[:needed]


def _sort_key_cell(raw: object) -> tuple[int, float, str]:
    if raw is None:
        return (2, 0.0, "")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return (0, float(raw), "")
    return (1, 0.0, str(raw))


class _SQLiteJsonArrayAgg:
    """SQLite aggregate emitting a JSON array ordered by `(k1, k2)` (`ARRAY_AGG` -> `TO_JSON`)."""

    def __init__(self) -> None:
        self.items: list[tuple[tuple[int, float, str], tuple[int, float, str], int, object]] = []
        self.distinct = False

    def step(self, *args: object) -> None:
        val, k1, k2, distinct_flag = args
        if val is None:
            return
        self.distinct = bool(distinct_flag)
        self.items.append(
            (
                _sort_key_cell(k1),
                _sort_key_cell(k2),
                len(self.items),
                _sqlite_json_array_agg_elem(val),
            )
        )

    def finalize(self) -> str | None:
        if not self.items:
            return None
        self.items.sort(key=lambda item: (item[0], item[1], item[2]))
        vals: list[object] = []
        seen: set[str] = set()
        for _, _, _, elem in self.items:
            if self.distinct:
                sig = _compact_json(elem)
                if sig in seen:
                    continue
                seen.add(sig)
            vals.append(elem)
        return _compact_json(vals)


def register_silver_sqlite_udfs(
    conn: sqlite3.Connection,
    *,
    parse_tokens: JsonWalkFn,
    walk_tokens: JsonStepFn,
) -> None:
    """Register Silver JSON, JCS, array, and scalar compatibility UDFs on a SQLite connection."""
    json_query_fn = make_json_query_fn(parse_tokens, walk_tokens)
    conn.create_function("JSON_QUERY", 2, json_query_fn)
    conn.create_function("BQ_JSON_QUERY", 2, json_query_fn)
    conn.create_function("BQ_PARSE_JSON", 1, _sqlite_parse_json)
    conn.create_function("BQ_JSON_ARRAY", -1, _sqlite_json_array)
    conn.create_function("BQ_JSON_OBJECT", -1, _sqlite_json_object)
    conn.create_function("JSON_STRIP_NULLS", 1, _sqlite_json_strip_nulls)
    conn.create_function("BQ_JSON_STRIP_NULLS", 1, _sqlite_json_strip_nulls)
    conn.create_function("BQ_JSON_ARRAY_CONTAINS", 2, _sqlite_json_array_contains)
    conn.create_function("BQ_JSON_ENSURE_ARRAY", 1, _sqlite_json_ensure_array)
    conn.create_function("BQ_GREATEST", -1, _sqlite_greatest)
    conn.create_function("BQ_LEAST", -1, _sqlite_least)
    conn.create_function("BQ_JCS_CANONICALIZE", 1, jcs_canonicalize_str)
    conn.create_function("BQ_JCS_SHA256", 1, jcs_sha256_str)
    conn.create_function("BQ_JCS_TOOL_ARGS_SHA256", 2, jcs_tool_args_sha256_str)
    conn.create_function("BQ_SHA256_HEX", 1, _sqlite_sha256_hex)
    conn.create_function("LPAD", 3, _sqlite_lpad)
    conn.create_function("LPAD", 2, _sqlite_lpad)
    conn.create_function("RPAD", 3, _sqlite_rpad)
    conn.create_function("RPAD", 2, _sqlite_rpad)
    conn.create_aggregate("BQ_JSON_ARRAY_AGG", 4, _SQLiteJsonArrayAgg)  # type: ignore[arg-type]


def _extract_udf_short_name(node: exp.Expr) -> tuple[str, list[exp.Expr]] | None:
    if isinstance(node, exp.Anonymous):
        short = node.name.replace("`", "").rsplit(".", 1)[-1].lower()
        if short in _SCALAR_UDF_MAP:
            return _SCALAR_UDF_MAP[short], [e.copy() for e in node.expressions]
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Anonymous):
        short = node.expression.name.replace("`", "").rsplit(".", 1)[-1].lower()
        if short in _SCALAR_UDF_MAP:
            return _SCALAR_UDF_MAP[short], [e.copy() for e in node.expression.expressions]
    return None


def _rewrite_to_json_array_agg(target: exp.Expr) -> exp.Expr | None:
    if isinstance(target, exp.IgnoreNulls):
        target = target.this
    if not isinstance(target, exp.ArrayAgg):
        return None
    inner = target.this.copy()
    distinct = False
    order_node: exp.Order | None = None
    while isinstance(inner, (exp.Limit, exp.Order, exp.IgnoreNulls, exp.Distinct)):
        if isinstance(inner, exp.Order):
            order_node = inner
        elif isinstance(inner, exp.Distinct):
            distinct = True
            if inner.expressions:
                inner = inner.expressions[0]
                continue
        inner = inner.this
    k1: exp.Expr = exp.Null()
    k2: exp.Expr = exp.Null()
    if order_node is not None and order_node.expressions:
        k1 = order_node.expressions[0].this.copy()
        if len(order_node.expressions) >= 2:
            k2 = order_node.expressions[1].this.copy()
    return exp.Anonymous(
        this="BQ_JSON_ARRAY_AGG",
        expressions=[inner, k1, k2, exp.Literal.number(1 if distinct else 0)],
    )


def _rewrite_in_unnest(node: exp.In) -> exp.Expr | None:
    unnest = node.args.get("unnest")
    if not (isinstance(unnest, exp.Unnest) and unnest.expressions):
        return None
    target = unnest.expressions[0]
    arr_expr: exp.Expr | None = None
    if isinstance(target, exp.JSONValueArray) and target.this is not None:
        arr_expr = target.this.copy()
    elif isinstance(target, exp.Anonymous) and target.name.upper() in {
        "JSON_VALUE_ARRAY",
        "JSON_EXTRACT_STRING_ARRAY",
    }:
        arr_expr = target.expressions[0].copy()
    if arr_expr is None:
        return None
    return exp.EQ(
        this=exp.Anonymous(this="BQ_JSON_ARRAY_CONTAINS", expressions=[arr_expr, node.this.copy()]),
        expression=exp.Literal.number(1),
    )


def _extract_sha256_hex_expr(inner: exp.Expr) -> exp.Expr | None:
    if isinstance(inner, (exp.SHA2, exp.SHA2Digest)) and inner.this is not None:
        return exp.Anonymous(this="BQ_SHA256_HEX", expressions=[inner.this.copy()])
    if isinstance(inner, exp.Anonymous) and inner.name.upper() == "SHA256" and inner.expressions:
        return exp.Anonymous(this="BQ_SHA256_HEX", expressions=[inner.expressions[0].copy()])
    return None


_BOOL_EXPR_TYPES: tuple[type[exp.Expr], ...] = (
    exp.Boolean,
    exp.Not,
    exp.And,
    exp.Or,
    exp.Is,
    exp.In,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
)


def _is_bool_ast_expr(expr: exp.Expr) -> bool:
    if isinstance(expr, _BOOL_EXPR_TYPES):
        return True
    if isinstance(expr, exp.Paren) and expr.this is not None:
        return _is_bool_ast_expr(expr.this)
    if isinstance(expr, exp.If):
        t_branch = expr.args.get("true")
        f_branch = expr.args.get("false")
        return (isinstance(t_branch, exp.Expr) and _is_bool_ast_expr(t_branch)) or (
            isinstance(f_branch, exp.Expr) and _is_bool_ast_expr(f_branch)
        )
    if isinstance(expr, exp.Coalesce):
        c_args = [expr.this, *expr.expressions]
        return any(isinstance(a, exp.Expr) and _is_bool_ast_expr(a) for a in c_args)
    return False


def _wrap_json_object_val(expr: exp.Expr) -> exp.Expr:
    copied = expr.copy()
    if not _is_bool_ast_expr(copied):
        return copied
    if isinstance(copied, exp.Boolean):
        return exp.Literal.string(_BQ_JSON_BOOL_TRUE if copied.this else _BQ_JSON_BOOL_FALSE)
    return exp.Case(
        ifs=[
            exp.If(this=exp.Is(this=copied.copy(), expression=exp.Null()), true=exp.Null()),
            exp.If(this=copied.copy(), true=exp.Literal.string(_BQ_JSON_BOOL_TRUE)),
        ],
        default=exp.Literal.string(_BQ_JSON_BOOL_FALSE),
    )


def _rewrite_json_object_pairs(exprs: list[exp.Expr]) -> list[exp.Expr]:
    return [
        _wrap_json_object_val(item) if idx % 2 == 1 else item.copy()
        for idx, item in enumerate(exprs)
    ]


def _rewrite_json_object_ast(node: exp.JSONObject) -> exp.Expr:
    raw_kv: list[exp.Expr] = []
    for item in node.expressions:
        if isinstance(item, exp.JSONKeyValue):
            raw_kv.extend([item.this, item.expression])
        else:
            raw_kv.append(item)
    return exp.Anonymous(this="BQ_JSON_OBJECT", expressions=_rewrite_json_object_pairs(raw_kv))


def _rewrite_silver_anonymous(node: exp.Anonymous) -> exp.Expr | None:
    upper = node.name.upper()
    out: exp.Expr | None = None
    single_arg_map = {
        "JSON_STRIP_NULLS": "BQ_JSON_STRIP_NULLS",
        "PARSE_JSON": "BQ_PARSE_JSON",
    }
    if upper in single_arg_map and node.expressions:
        out = exp.Anonymous(this=single_arg_map[upper], expressions=[node.expressions[0].copy()])
    elif upper == "JSON_ARRAY":
        out = exp.Anonymous(this="BQ_JSON_ARRAY", expressions=[e.copy() for e in node.expressions])
    elif upper == "JSON_OBJECT":
        out = exp.Anonymous(
            this="BQ_JSON_OBJECT",
            expressions=_rewrite_json_object_pairs(list(node.expressions)),
        )
    elif upper == "TO_JSON" and node.expressions:
        agg_rewrite = _rewrite_to_json_array_agg(node.expressions[0])
        out = agg_rewrite if agg_rewrite is not None else node.expressions[0].copy()
    elif upper == "TO_HEX" and node.expressions:
        out = _extract_sha256_hex_expr(node.expressions[0])
    return out


def _rewrite_silver_json_ast(
    node: exp.Expr,
    json_path_fn: Callable[[exp.Expr], exp.Expr],
) -> exp.Expr | None:
    out: exp.Expr | None = None
    if isinstance(node, exp.JSONFormat) and node.args.get("to_json") and node.this is not None:
        agg_rewrite = _rewrite_to_json_array_agg(node.this)
        out = agg_rewrite if agg_rewrite is not None else node.this.copy()
    elif isinstance(node, exp.Hex) and node.this is not None:
        out = _extract_sha256_hex_expr(node.this)
    elif isinstance(node, exp.JSONExtract) and node.expression is not None:
        out = exp.Anonymous(
            this="BQ_JSON_QUERY",
            expressions=[node.this.copy(), json_path_fn(node.expression)],
        )
    elif isinstance(node, exp.ParseJSON):
        out = exp.Anonymous(this="BQ_PARSE_JSON", expressions=[node.this.copy()])
    elif isinstance(node, exp.JSONArray):
        out = exp.Anonymous(this="BQ_JSON_ARRAY", expressions=[e.copy() for e in node.expressions])
    elif isinstance(node, exp.JSONObject):
        out = _rewrite_json_object_ast(node)
    return out


def rewrite_silver_bq_expr(
    node: exp.Expr,
    *,
    json_path_fn: Callable[[exp.Expr], exp.Expr],
) -> exp.Expr | None:
    """Rewrite BigQuery Silver JSON/JCS/UNNEST/SAFE/TO_JSON AST nodes for SQLite execution."""
    out: exp.Expr | None = None
    udf_hit = _extract_udf_short_name(node)
    if udf_hit is not None:
        out = exp.Anonymous(this=udf_hit[0], expressions=udf_hit[1])
    elif isinstance(node, exp.SafeFunc) and node.this is not None:
        inner = node.this.copy()
        out = rewrite_silver_bq_expr(inner, json_path_fn=json_path_fn) or inner
    elif isinstance(node, exp.In):
        out = _rewrite_in_unnest(node)
    elif isinstance(node, (exp.Greatest, exp.Least)):
        fn = "BQ_GREATEST" if isinstance(node, exp.Greatest) else "BQ_LEAST"
        out = exp.Anonymous(
            this=fn, expressions=[node.this.copy(), *(e.copy() for e in node.expressions)]
        )
    elif isinstance(node, exp.CountIf):
        out = exp.Sum(
            this=exp.Case(
                ifs=[exp.If(this=node.this.copy(), true=exp.Literal.number(1))],
                default=exp.Literal.number(0),
            )
        )
    elif isinstance(node, exp.Anonymous):
        out = _rewrite_silver_anonymous(node)
    else:
        out = _rewrite_silver_json_ast(node, json_path_fn)
    return out
