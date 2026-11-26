-- Silver RFC 8785 (JCS) Canonicalizer & SHA-256 Python UDFs (`ADR-041`, `ADR-048`).
-- Enforces the 65,532-byte UTF-8 ceiling, max nesting depth (64), lone-surrogate rejection, and fail-safe NULL.

CREATE OR REPLACE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_canonicalize`(raw_json STRING)
RETURNS STRING
LANGUAGE python
OPTIONS (
  runtime_version = 'python-3.11',
  entry_point = 'jcs_canonicalize'
)
AS R"""
import json
import math

MAX_INPUT_BYTES = 65532
MAX_DEPTH = 64


def _has_surrogate(s):
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _format_es6_number(value):
    f = float(value)
    if not math.isfinite(f):
        return None
    if f == 0.0:
        return "0"
    sign = "-" if f < 0 else ""
    raw = repr(abs(f))
    if "e" in raw or "E" in raw:
        mantissa_s, exp_s = raw.lower().split("e", 1)
        exp = int(exp_s)
        digits = mantissa_s.replace(".", "")
    else:
        int_part, _, frac_part = raw.partition(".")
        if int_part == "0":
            stripped = frac_part.lstrip("0")
            exp = -(len(frac_part) - len(stripped) + 1)
            digits = stripped
        else:
            exp = len(int_part) - 1
            digits = int_part + frac_part
    digits = digits.rstrip("0") or "0"
    if -6 <= exp < 21:
        pos = exp + 1
        if pos >= len(digits):
            return sign + digits + ("0" * (pos - len(digits)))
        if pos > 0:
            return sign + digits[:pos] + "." + digits[pos:]
        return sign + "0." + ("0" * (-pos)) + digits
    exp_sign = "+" if exp >= 0 else "-"
    mant = digits if len(digits) == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{mant}e{exp_sign}{abs(exp)}"


def _canonicalize(value, depth=0):
    if depth > MAX_DEPTH:
        return None
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_es6_number(value)
    if isinstance(value, str):
        if _has_surrogate(value):
            return None
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        parts = []
        for item in value:
            p = _canonicalize(item, depth + 1)
            if p is None:
                return None
            parts.append(p)
        return "[" + ",".join(parts) + "]"
    if isinstance(value, dict):
        str_items = []
        for k, v in value.items():
            ks = str(k)
            if _has_surrogate(ks):
                return None
            str_items.append((ks, v))
        items = sorted(str_items, key=lambda kv: kv[0].encode("utf-16-be"))
        pairs = []
        for k, v in items:
            cv = _canonicalize(v, depth + 1)
            if cv is None:
                return None
            pairs.append(f"{json.dumps(k, ensure_ascii=False)}:{cv}")
        return "{" + ",".join(pairs) + "}"
    return None


def jcs_canonicalize(raw_json):
    if raw_json is None:
        return None
    try:
        if _has_surrogate(raw_json) or len(raw_json.encode("utf-8")) > MAX_INPUT_BYTES:
            return None
        parsed = json.loads(raw_json)
        return _canonicalize(parsed)
    except Exception:
        return None
""";

CREATE OR REPLACE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_sha256`(raw_json STRING)
RETURNS STRING
LANGUAGE python
OPTIONS (
  runtime_version = 'python-3.11',
  entry_point = 'jcs_sha256'
)
AS R"""
import hashlib
import json
import math

MAX_INPUT_BYTES = 65532
MAX_DEPTH = 64


def _has_surrogate(s):
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _format_es6_number(value):
    f = float(value)
    if not math.isfinite(f):
        return None
    if f == 0.0:
        return "0"
    sign = "-" if f < 0 else ""
    raw = repr(abs(f))
    if "e" in raw or "E" in raw:
        mantissa_s, exp_s = raw.lower().split("e", 1)
        exp = int(exp_s)
        digits = mantissa_s.replace(".", "")
    else:
        int_part, _, frac_part = raw.partition(".")
        if int_part == "0":
            stripped = frac_part.lstrip("0")
            exp = -(len(frac_part) - len(stripped) + 1)
            digits = stripped
        else:
            exp = len(int_part) - 1
            digits = int_part + frac_part
    digits = digits.rstrip("0") or "0"
    if -6 <= exp < 21:
        pos = exp + 1
        if pos >= len(digits):
            return sign + digits + ("0" * (pos - len(digits)))
        if pos > 0:
            return sign + digits[:pos] + "." + digits[pos:]
        return sign + "0." + ("0" * (-pos)) + digits
    exp_sign = "+" if exp >= 0 else "-"
    mant = digits if len(digits) == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{mant}e{exp_sign}{abs(exp)}"


def _canonicalize(value, depth=0):
    if depth > MAX_DEPTH:
        return None
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_es6_number(value)
    if isinstance(value, str):
        if _has_surrogate(value):
            return None
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        parts = []
        for item in value:
            p = _canonicalize(item, depth + 1)
            if p is None:
                return None
            parts.append(p)
        return "[" + ",".join(parts) + "]"
    if isinstance(value, dict):
        str_items = []
        for k, v in value.items():
            ks = str(k)
            if _has_surrogate(ks):
                return None
            str_items.append((ks, v))
        items = sorted(str_items, key=lambda kv: kv[0].encode("utf-16-be"))
        pairs = []
        for k, v in items:
            cv = _canonicalize(v, depth + 1)
            if cv is None:
                return None
            pairs.append(f"{json.dumps(k, ensure_ascii=False)}:{cv}")
        return "{" + ",".join(pairs) + "}"
    return None


def jcs_sha256(raw_json):
    if raw_json is None:
        return None
    try:
        if _has_surrogate(raw_json) or len(raw_json.encode("utf-8")) > MAX_INPUT_BYTES:
            return None
        parsed = json.loads(raw_json)
        canon = _canonicalize(parsed)
        if canon is None:
            return None
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()
    except Exception:
        return None
""";

CREATE OR REPLACE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_tool_args_sha256`(
  tool_name STRING,
  raw_args STRING
)
RETURNS STRING
LANGUAGE python
OPTIONS (
  runtime_version = 'python-3.11',
  entry_point = 'jcs_tool_args_sha256'
)
AS R"""
import hashlib
import json
import math

MAX_INPUT_BYTES = 65532
MAX_DEPTH = 64


def _has_surrogate(s):
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _format_es6_number(value):
    f = float(value)
    if not math.isfinite(f):
        return None
    if f == 0.0:
        return "0"
    sign = "-" if f < 0 else ""
    raw = repr(abs(f))
    if "e" in raw or "E" in raw:
        mantissa_s, exp_s = raw.lower().split("e", 1)
        exp = int(exp_s)
        digits = mantissa_s.replace(".", "")
    else:
        int_part, _, frac_part = raw.partition(".")
        if int_part == "0":
            stripped = frac_part.lstrip("0")
            exp = -(len(frac_part) - len(stripped) + 1)
            digits = stripped
        else:
            exp = len(int_part) - 1
            digits = int_part + frac_part
    digits = digits.rstrip("0") or "0"
    if -6 <= exp < 21:
        pos = exp + 1
        if pos >= len(digits):
            return sign + digits + ("0" * (pos - len(digits)))
        if pos > 0:
            return sign + digits[:pos] + "." + digits[pos:]
        return sign + "0." + ("0" * (-pos)) + digits
    exp_sign = "+" if exp >= 0 else "-"
    mant = digits if len(digits) == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{mant}e{exp_sign}{abs(exp)}"


def _canonicalize(value, depth=0):
    if depth > MAX_DEPTH:
        return None
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_es6_number(value)
    if isinstance(value, str):
        if _has_surrogate(value):
            return None
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        parts = []
        for item in value:
            p = _canonicalize(item, depth + 1)
            if p is None:
                return None
            parts.append(p)
        return "[" + ",".join(parts) + "]"
    if isinstance(value, dict):
        str_items = []
        for k, v in value.items():
            ks = str(k)
            if _has_surrogate(ks):
                return None
            str_items.append((ks, v))
        items = sorted(str_items, key=lambda kv: kv[0].encode("utf-16-be"))
        pairs = []
        for k, v in items:
            cv = _canonicalize(v, depth + 1)
            if cv is None:
                return None
            pairs.append(f"{json.dumps(k, ensure_ascii=False)}:{cv}")
        return "{" + ",".join(pairs) + "}"
    return None


def jcs_tool_args_sha256(tool_name, raw_args):
    if tool_name is None or raw_args is None:
        return None
    try:
        if (
            _has_surrogate(tool_name)
            or _has_surrogate(raw_args)
            or len(raw_args.encode("utf-8")) > MAX_INPUT_BYTES
        ):
            return None
        parsed_args = json.loads(raw_args)
        canon = _canonicalize({"tool_name": tool_name, "args": parsed_args})
        if canon is None:
            return None
        encoded = canon.encode("utf-8")
        if len(encoded) > MAX_INPUT_BYTES:
            return None
        return hashlib.sha256(encoded).hexdigest()
    except Exception:
        return None
""";
