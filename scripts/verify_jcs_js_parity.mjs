#!/usr/bin/env node
// Executes verbatim `LANGUAGE js` bodies from `sql/silver/jcs.sql` inside V8 (Node 22)
// against all committed ADR-041 vectors and edge guards (`M20-0`, closing the SQLite loader skip).

import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const JCS_SQL = fs.readFileSync(path.join(REPO_ROOT, "sql", "silver", "jcs.sql"), "utf8");
const VECTORS = JSON.parse(
  fs.readFileSync(
    path.join(REPO_ROOT, "tests", "fixtures", "silver", "adr041_jcs_vectors.json"),
    "utf8",
  ),
);

function extractJsBody(fnName) {
  const re = new RegExp(
    `\\.${fnName}\`\\([^)]*\\)\\s*RETURNS\\s+STRING\\s+DETERMINISTIC\\s+LANGUAGE\\s+js\\s+AS\\s+R"""([\\s\\S]*?)"""`,
    "m",
  );
  const match = JCS_SQL.match(re);
  assert.ok(match, `Missing DETERMINISTIC LANGUAGE js body for ${fnName} in sql/silver/jcs.sql`);
  return match[1];
}

function sha256Hex(s) {
  return s === null ? null : crypto.createHash("sha256").update(s, "utf8").digest("hex");
}

const jcsCanonicalize = new Function("raw_json", extractJsBody("jcs_canonicalize"));
const jcsToolArgsCanonicalize = new Function(
  "tool_name",
  "raw_args",
  extractJsBody("jcs_tool_args_canonicalize"),
);

// 1. All committed ADR-041 vectors.
for (const vec of VECTORS.vectors) {
  const canon = jcsCanonicalize(vec.input_json);
  assert.equal(canon, vec.canonical_json, `canonical mismatch on ${vec.id}`);
  assert.equal(sha256Hex(canon), vec.sha256, `sha256 mismatch on ${vec.id}`);
}

// 2. Oversize 65,532 B accepted vs 65,533 B rejected.
const g = VECTORS.oversize_guard;
const okPayload = g.template_prefix + g.fill_char.repeat(g.accepted_fill_len) + g.template_suffix;
const badPayload = g.template_prefix + g.fill_char.repeat(g.rejected_fill_len) + g.template_suffix;
assert.equal(Buffer.byteLength(okPayload, "utf8"), 65532);
assert.equal(Buffer.byteLength(badPayload, "utf8"), 65533);
assert.equal(sha256Hex(jcsCanonicalize(okPayload)), g.accepted_sha256);
assert.equal(jcsCanonicalize(badPayload), null);

// 3. Tool-args wrapper parity + special characters in tool_name + wrapped 65,532 B cap.
const baseVec = VECTORS.vectors.find((v) => v.id === "tool_call_canonical_base");
assert.ok(baseVec, "Missing tool_call_canonical_base vector");
const baseParsed = JSON.parse(baseVec.input_json);
const wrappedBase = jcsToolArgsCanonicalize(baseParsed.tool_name, JSON.stringify(baseParsed.args));
assert.equal(wrappedBase, baseVec.canonical_json);
assert.equal(sha256Hex(wrappedBase), baseVec.sha256);

const evilName = 'evil"tool\\name\n';
const evilWrapped = jcsToolArgsCanonicalize(evilName, '{"b":1,"a":2.0}');
assert.equal(evilWrapped, '{"args":{"a":2,"b":1},"tool_name":"evil\\"tool\\\\name\\n"}');

const justFitsTool = jcsToolArgsCanonicalize("bulk_write", '{"blob":"' + "x".repeat(65487) + '"}');
assert.ok(justFitsTool !== null && Buffer.byteLength(justFitsTool, "utf8") === 65532);
assert.equal(jcsToolArgsCanonicalize("bulk_write", '{"blob":"' + "x".repeat(65488) + '"}'), null);

// 4. Depth 64 accepted vs depth 65 rejected; lone surrogates vs valid emoji; __proto__; non-finite; NULL.
const depth64 = "[".repeat(64) + "1" + "]".repeat(64);
const depth65 = "[".repeat(65) + "1" + "]".repeat(65);
assert.equal(jcsCanonicalize(depth64), depth64);
assert.equal(jcsCanonicalize(depth65), null);
assert.equal(jcsToolArgsCanonicalize("d", "[".repeat(63) + "1" + "]".repeat(63)), '{"args":' + "[".repeat(63) + "1" + "]".repeat(63) + ',"tool_name":"d"}');
assert.equal(jcsToolArgsCanonicalize("d", "[".repeat(64) + "1" + "]".repeat(64)), null);

assert.equal(jcsCanonicalize('{"bad":"\\ud800"}'), null);
assert.equal(jcsCanonicalize('{"\\udc00":1}'), null);
assert.equal(jcsCanonicalize('{"ok":"😀"}'), '{"ok":"😀"}');
assert.equal(jcsToolArgsCanonicalize("bad\ud800tool", "{}"), null);
assert.equal(jcsCanonicalize('{"__proto__":{"x":1},"a":1}'), '{"__proto__":{"x":1},"a":1}');
assert.equal(jcsCanonicalize('{"x":1e400}'), null);
assert.equal(jcsCanonicalize('{"z":-0.0}'), '{"z":0}');
assert.equal(jcsCanonicalize(null), null);
assert.equal(jcsToolArgsCanonicalize(null, "{}"), null);
assert.equal(jcsToolArgsCanonicalize("ok", null), null);
assert.equal(jcsCanonicalize("{not json"), null);

console.log(
  JSON.stringify({
    status: "PASS",
    vectors_checked: VECTORS.vectors.length,
    edge_guards_checked: 15,
  }),
);
