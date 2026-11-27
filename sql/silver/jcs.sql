-- Silver RFC 8785 (JCS) Canonicalizers & SHA-256 UDFs (`ADR-041`, `ADR-048` amended).
-- Uses inline V8 `DETERMINISTIC LANGUAGE js` (zero GCS libraries, CMEK & Assured Workloads safe,
-- 24h Query Result Cache eligible on quiescent/staged tables, zero `CASE WHEN` batching collapse)
-- wrapped by native GoogleSQL `BYTE_LENGTH(...) <= 65532` and `TO_HEX(SHA256(...))`.

CREATE OR REPLACE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_canonicalize`(
  raw_json STRING
)
RETURNS STRING
DETERMINISTIC
LANGUAGE js
AS R"""
const MAX_INPUT_BYTES = 65532;
const MAX_DEPTH = 64;

function utf8BytesChecked(s) {
  let bytes = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c < 0x80) {
      bytes += 1;
    } else if (c < 0x800) {
      bytes += 2;
    } else if (c >= 0xD800 && c <= 0xDBFF) {
      if (i + 1 >= s.length) return -1;
      const low = s.charCodeAt(i + 1);
      if (low < 0xDC00 || low > 0xDFFF) return -1;
      bytes += 4;
      i++;
    } else if (c >= 0xDC00 && c <= 0xDFFF) {
      return -1;
    } else {
      bytes += 3;
    }
  }
  return bytes;
}

function canon(val, depth) {
  if (depth > MAX_DEPTH) return null;
  if (val === null) return "null";
  const t = typeof val;
  if (t === "boolean") return val ? "true" : "false";
  if (t === "number") {
    if (!Number.isFinite(val)) return null;
    return val === 0 ? "0" : String(val);
  }
  if (t === "string") {
    if (utf8BytesChecked(val) < 0) return null;
    return JSON.stringify(val);
  }
  if (Array.isArray(val)) {
    const parts = new Array(val.length);
    for (let i = 0; i < val.length; i++) {
      const p = canon(val[i], depth + 1);
      if (p === null) return null;
      parts[i] = p;
    }
    return "[" + parts.join(",") + "]";
  }
  if (t === "object") {
    const keys = Object.keys(val).sort();
    const pairs = new Array(keys.length);
    for (let i = 0; i < keys.length; i++) {
      const k = keys[i];
      if (utf8BytesChecked(k) < 0) return null;
      const desc = Object.getOwnPropertyDescriptor(val, k);
      const cv = canon(desc ? desc.value : undefined, depth + 1);
      if (cv === null) return null;
      pairs[i] = JSON.stringify(k) + ":" + cv;
    }
    return "{" + pairs.join(",") + "}";
  }
  return null;
}

if (raw_json === null) return null;
try {
  const b = utf8BytesChecked(raw_json);
  if (b < 0 || b > MAX_INPUT_BYTES) return null;
  const parsed = JSON.parse(raw_json);
  return canon(parsed, 0);
} catch (e) {
  return null;
}
""";

CREATE OR REPLACE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_tool_args_canonicalize`(
  tool_name STRING,
  raw_args STRING
)
RETURNS STRING
DETERMINISTIC
LANGUAGE js
AS R"""
const MAX_INPUT_BYTES = 65532;
const MAX_DEPTH = 64;

function utf8BytesChecked(s) {
  let bytes = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c < 0x80) {
      bytes += 1;
    } else if (c < 0x800) {
      bytes += 2;
    } else if (c >= 0xD800 && c <= 0xDBFF) {
      if (i + 1 >= s.length) return -1;
      const low = s.charCodeAt(i + 1);
      if (low < 0xDC00 || low > 0xDFFF) return -1;
      bytes += 4;
      i++;
    } else if (c >= 0xDC00 && c <= 0xDFFF) {
      return -1;
    } else {
      bytes += 3;
    }
  }
  return bytes;
}

function canon(val, depth) {
  if (depth > MAX_DEPTH) return null;
  if (val === null) return "null";
  const t = typeof val;
  if (t === "boolean") return val ? "true" : "false";
  if (t === "number") {
    if (!Number.isFinite(val)) return null;
    return val === 0 ? "0" : String(val);
  }
  if (t === "string") {
    if (utf8BytesChecked(val) < 0) return null;
    return JSON.stringify(val);
  }
  if (Array.isArray(val)) {
    const parts = new Array(val.length);
    for (let i = 0; i < val.length; i++) {
      const p = canon(val[i], depth + 1);
      if (p === null) return null;
      parts[i] = p;
    }
    return "[" + parts.join(",") + "]";
  }
  if (t === "object") {
    const keys = Object.keys(val).sort();
    const pairs = new Array(keys.length);
    for (let i = 0; i < keys.length; i++) {
      const k = keys[i];
      if (utf8BytesChecked(k) < 0) return null;
      const desc = Object.getOwnPropertyDescriptor(val, k);
      const cv = canon(desc ? desc.value : undefined, depth + 1);
      if (cv === null) return null;
      pairs[i] = JSON.stringify(k) + ":" + cv;
    }
    return "{" + pairs.join(",") + "}";
  }
  return null;
}

if (tool_name === null || raw_args === null) return null;
try {
  if (utf8BytesChecked(tool_name) < 0) return null;
  const rb = utf8BytesChecked(raw_args);
  if (rb < 0 || rb > MAX_INPUT_BYTES) return null;
  const parsedArgs = JSON.parse(raw_args);
  const cArgs = canon(parsedArgs, 1);
  if (cArgs === null) return null;
  const wrapped = '{"args":' + cArgs + ',"tool_name":' + JSON.stringify(tool_name) + '}';
  const wb = utf8BytesChecked(wrapped);
  if (wb < 0 || wb > MAX_INPUT_BYTES) return null;
  return wrapped;
} catch (e) {
  return null;
}
""";

CREATE OR REPLACE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_sha256`(
  raw_json STRING
)
RETURNS STRING
AS (
  CASE
    WHEN raw_json IS NULL OR BYTE_LENGTH(raw_json) > 65532 THEN CAST(NULL AS STRING)
    ELSE TO_HEX(SHA256(`${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_canonicalize`(raw_json)))
  END
);

CREATE OR REPLACE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_tool_args_sha256`(
  tool_name STRING,
  raw_args STRING
)
RETURNS STRING
AS (
  CASE
    WHEN tool_name IS NULL OR raw_args IS NULL OR BYTE_LENGTH(raw_args) > 65532 THEN CAST(NULL AS STRING)
    ELSE TO_HEX(SHA256(`${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.jcs_tool_args_canonicalize`(tool_name, raw_args)))
  END
);
