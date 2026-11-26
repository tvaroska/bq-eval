"""Silver M18-1 fixture manifest, scrubbing hygiene, and ADR-041 JCS vector contract tests."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "silver"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"

ALLOWED_FIXTURE_PROVENANCE: frozenset[str] = frozenset({"recorded", "SYNTHETIC_UNTIL_RECORDED"})

WORKFLOW_EVENT_COLUMNS: frozenset[str] = frozenset(
    {
        "event_id",
        "event_time",
        "event_type",
        "source_system",
        "trace_id",
        "span_id",
        "response_id",
        "outcome_id",
        "conversation_id",
        "action_id",
        "nudge_id",
        "control_id",
        "control_layer",
        "control_action",
        "action_args",
        "approved_expires_at",
        "user_id",
        "tenant_id",
        "actor_id",
        "payload_json",
    }
)

_ALLOWED_SYNTHETIC_EMAIL_DOMAINS: tuple[str, ...] = (
    "@example.invalid",
    "@showcase-project.iam.gserviceaccount.com",
)

_UNSCRUBBED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Scratch project identifier", re.compile(r"\b" + "btv" + r"-[a-z0-9-]+\b", re.IGNORECASE)),
    (
        "Non-placeholder 12-digit GCP project number",
        re.compile(r"(?<!\d)(?!100000000000\b)\d{12}(?!\d)"),
    ),
    (
        "Unscrubbed projects/ path",
        re.compile(r"\bprojects/(?!showcase-project(?:/|\b|-))[a-zA-Z0-9._-]+"),
    ),
    ("Workstation or corp host", re.compile(r"/usr/local/" + r"google/home|\.c\.googlers\.com")),
    ("Internal Buganizer ID", re.compile(r"\bb/\d{6,}\b")),
    (
        "Internal shortlink",
        re.compile(r"\bgo/(?!mod\b|sum\b|work\b|version\b|test\b|build\b|run\b)[a-zA-Z0-9_-]{3,}"),
    ),
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _find_unscrubbed_ids(text: str) -> list[str]:
    hits: list[str] = []
    for label, pattern in _UNSCRUBBED_PATTERNS:
        m = pattern.search(text)
        if m:
            hits.append(f"{label}: {m.group(0)!r}")
    for m in _EMAIL_RE.finditer(text):
        addr = m.group(0).lower()
        if not addr.endswith(_ALLOWED_SYNTHETIC_EMAIL_DOMAINS):
            hits.append(f"Unscrubbed email: {addr!r}")
    return hits


def _format_es6_number(value: int | float) -> str:
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"Non-finite number not allowed in RFC 8785: {value!r}")
    if f == 0.0:
        return "0"
    sign = "-" if f < 0 else ""
    af = abs(f)
    raw = repr(af)
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


def _jcs_canonicalize(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_es6_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_jcs_canonicalize(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: str(kv[0]).encode("utf-16-be"))
        pairs = [
            f"{json.dumps(str(k), ensure_ascii=False)}:{_jcs_canonicalize(v)}" for k, v in items
        ]
        return "{" + ",".join(pairs) + "}"
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _assert_recorded_coverage(by_name: dict[str, dict[str, Any]]) -> None:
    rec_spans = _load_jsonl(FIXTURES_DIR / "recorded_allspans.jsonl")
    assert by_name["recorded_allspans.jsonl"]["provenance"] == "recorded"
    services: set[str] = set()
    ops: set[str] = set()
    runtime_count = 0
    token_no_content = 0
    key_counts: dict[tuple[str, str], int] = {}
    for row in rec_spans:
        key = (str(row["trace_id"]), str(row["span_id"]))
        key_counts[key] = key_counts.get(key, 0) + 1
        res_attrs = json.loads((row.get("resource") or {}).get("attributes") or "{}")
        if res_attrs.get("service.name"):
            services.add(str(res_attrs["service.name"]))
        attrs = json.loads(row.get("attributes") or "{}")
        if attrs.get("gen_ai.operation.name"):
            ops.add(str(attrs["gen_ai.operation.name"]))
        scope = (row.get("instrumentation_scope") or {}).get("name")
        if scope is None and row.get("kind") is None:
            runtime_count += 1
        in_tok = attrs.get("gen_ai.usage.input_tokens")
        if (
            in_tok is not None
            and int(str(in_tok)) > 0
            and "gcp.vertex.agent.llm_request" not in attrs
            and "gen_ai.input.messages" not in attrs
        ):
            token_no_content += 1

    assert {"bq-eval-adk-v1", "bq-eval-adk-v2"} <= services
    assert runtime_count >= 1
    assert any(cnt > 1 for cnt in key_counts.values())
    assert token_no_content >= 1
    assert "invoke_node" in ops

    rec_logs = _load_jsonl(FIXTURES_DIR / "recorded_alllogs_eval.jsonl")
    assert by_name["recorded_alllogs_eval.jsonl"]["provenance"] == "recorded"
    assert any(r.get("log_id") == "gen_ai.evaluation.result" for r in rec_logs)


def _assert_synthetic_and_scrubber_guards(by_name: dict[str, dict[str, Any]]) -> None:
    for wf_file in (
        "synthetic_workflow_events_sor.jsonl",
        "synthetic_workflow_events_platform.jsonl",
        "synthetic_workflow_events_app.jsonl",
    ):
        assert by_name[wf_file]["provenance"] == "SYNTHETIC_UNTIL_RECORDED"
        for row in _load_jsonl(FIXTURES_DIR / wf_file):
            assert set(row.keys()) == WORKFLOW_EVENT_COLUMNS

    entitlements = _load_jsonl(FIXTURES_DIR / "synthetic_registry_unit_entitlements.jsonl")
    assert all(
        set(r.keys()) == {"principal_email", "business_unit", "tenant_id"} for r in entitlements
    )
    assert any(r["business_unit"] == "__unowned__" for r in entitlements)
    assert any(r["tenant_id"] != "*" for r in entitlements)

    dirty_samples = (
        "projects/" + "btv" + "-scratch-01/logs/test",
        '{"cloud.account.id": "' + "883686" + '070419"}',
        '{"principal": "alice@corp' + '.example.com"}',
        "projects/real-customer-proj-99/traces",
    )
    for sample in dirty_samples:
        assert _find_unscrubbed_ids(sample), f"Scrubber failed to reject dirty sample: {sample!r}"


def test_silver_fixture_manifest() -> None:
    """Every Silver fixture is SHA-256 pinned, provenance-labeled, scrubbed, and covers M18-1 cases."""
    assert MANIFEST_PATH.is_file(), "Missing tests/fixtures/silver/manifest.json"
    manifest: dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    disk_files = {
        p.name
        for p in FIXTURES_DIR.iterdir()
        if p.is_file() and p.name != "manifest.json" and p.suffix in {".json", ".jsonl"}
    }
    pinned_files: set[str] = set()
    by_name: dict[str, dict[str, Any]] = {}

    for entry in manifest["fixtures"]:
        fname = str(entry["file"])
        assert fname not in pinned_files, f"Duplicate manifest entry: {fname}"
        pinned_files.add(fname)
        by_name[fname] = entry

        assert entry["provenance"] in ALLOWED_FIXTURE_PROVENANCE, (
            f"{fname} has invalid provenance {entry['provenance']!r}"
        )
        fpath = FIXTURES_DIR / fname
        assert fpath.is_file(), f"Pinned fixture missing on disk: {fname}"
        raw = fpath.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"], f"SHA-256 mismatch for {fname}"
        leaks = _find_unscrubbed_ids(raw.decode("utf-8"))
        assert not leaks, f"Unscrubbed IDs in {fname}: {leaks}"
        rows = _load_jsonl(fpath)
        assert len(rows) == entry["row_count"] and rows

    vec_entry: dict[str, Any] = manifest["hash_vectors"]
    vec_name = str(vec_entry["file"])
    pinned_files.add(vec_name)
    vec_bytes = (FIXTURES_DIR / vec_name).read_bytes()
    assert hashlib.sha256(vec_bytes).hexdigest() == vec_entry["sha256"]
    assert not _find_unscrubbed_ids(vec_bytes.decode("utf-8"))
    assert pinned_files == disk_files

    _assert_recorded_coverage(by_name)
    _assert_synthetic_and_scrubber_guards(by_name)


def test_adr041_jcs_vectors_contract() -> None:
    """Python RFC 8785 canonicalizer matches committed ADR-041 vectors and synthetic tool/event hashes."""
    vec_doc: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "adr041_jcs_vectors.json").read_text(encoding="utf-8")
    )
    assert vec_doc["max_input_bytes"] == 65_532

    digests_by_group: dict[str, set[str]] = {}
    digests_by_id: dict[str, str] = {}
    for vec in vec_doc["vectors"]:
        canonical = _jcs_canonicalize(json.loads(vec["input_json"]))
        assert canonical == vec["canonical_json"], f"Canonical mismatch on {vec['id']}"
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert digest == vec["sha256"], f"SHA-256 mismatch on {vec['id']}"
        digests_by_group.setdefault(str(vec["group"]), set()).add(digest)
        digests_by_id[str(vec["id"])] = digest

    assert len(digests_by_group["transfer_funds_100"]) == 1
    assert len(digests_by_group["zero_normalization"]) == 1
    assert digests_by_id["tool_name_binding_approve"] != digests_by_id["tool_name_binding_cancel"]

    guard = vec_doc["oversize_guard"]
    exact_payload = (
        str(guard["template_prefix"])
        + str(guard["fill_char"]) * int(guard["accepted_fill_len"])
        + str(guard["template_suffix"])
    )
    assert len(exact_payload.encode("utf-8")) == 65_532
    assert hashlib.sha256(exact_payload.encode("utf-8")).hexdigest() == guard["accepted_sha256"]
    rejected_payload = (
        str(guard["template_prefix"])
        + str(guard["fill_char"]) * int(guard["rejected_fill_len"])
        + str(guard["template_suffix"])
    )
    assert len(rejected_payload.encode("utf-8")) == 65_533

    expected_wire_sha = digests_by_id["tool_call_canonical_base"]
    proxy_spans = _load_jsonl(FIXTURES_DIR / "synthetic_platform_gateway_proxy_spans.jsonl")
    proxy_hashes = [
        json.loads(s["attributes"]).get("bq_eval.args_sha256")
        for s in proxy_spans
        if "bq_eval.args_sha256" in json.loads(s["attributes"])
    ]
    assert proxy_hashes == [expected_wire_sha]

    sor_events = _load_jsonl(FIXTURES_DIR / "synthetic_workflow_events_sor.jsonl")
    action_arg_hashes = [
        hashlib.sha256(_jcs_canonicalize(json.loads(ev["action_args"])).encode("utf-8")).hexdigest()
        for ev in sor_events
        if ev.get("action_args") is not None
    ]
    assert action_arg_hashes == [expected_wire_sha, expected_wire_sha]
