"""V8 JavaScript RFC 8785 JCS UDF parity tests (`M20-0`, `ADR-041`, `ADR-048`)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from bq_eval.runner.engine.silver_udfs import (
    jcs_canonicalize_str,
    jcs_sha256_str,
    jcs_tool_args_canonicalize_str,
    jcs_tool_args_sha256_str,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
JCS_SQL_PATH = REPO_ROOT / "sql" / "silver" / "jcs.sql"
NODE_PARITY_SCRIPT = REPO_ROOT / "scripts" / "verify_jcs_js_parity.mjs"
CLOUDBUILD_PATH = REPO_ROOT / "cloudbuild.yaml"


def test_jcs_sql_uses_inline_deterministic_v8_js_and_sql_sha256() -> None:
    """`sql/silver/jcs.sql` uses inline `DETERMINISTIC LANGUAGE js` (CMEK-safe) + SQL `TO_HEX(SHA256)`."""
    sql_text = JCS_SQL_PATH.read_text(encoding="utf-8")
    assert "LANGUAGE python" not in sql_text
    assert sql_text.count("DETERMINISTIC\nLANGUAGE js") == 2
    assert "jcs_canonicalize" in sql_text
    assert "jcs_tool_args_canonicalize" in sql_text
    assert "BYTE_LENGTH(raw_json) > 65532" in sql_text
    assert "BYTE_LENGTH(raw_args) > 65532" in sql_text
    assert "TO_HEX(SHA256(" in sql_text

    # Cloud Build must execute the verbatim JS bodies in a Node 22 V8 container step.
    cb_text = CLOUDBUILD_PATH.read_text(encoding="utf-8")
    assert 'name: "node:22-slim"' in cb_text
    assert "scripts/verify_jcs_js_parity.mjs" in cb_text


def test_jcs_js_bodies_match_python_runner_in_v8() -> None:
    """Verbatim JS bodies in `sql/silver/jcs.sql` match `silver_udfs.py` across vectors & edge cases."""
    node_bin = shutil.which("node")
    if node_bin is not None:
        proc = subprocess.run(  # noqa: S603
            [node_bin, str(NODE_PARITY_SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Node V8 JCS parity failed:\n{proc.stdout}\n{proc.stderr}"
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        assert summary["status"] == "PASS"
        assert summary["vectors_checked"] >= 11

    # Always verify Python runner `jcs_tool_args_canonicalize` & edge-case parity even without `node`.
    sql_text = JCS_SQL_PATH.read_text(encoding="utf-8")
    js_bodies = re.findall(r'LANGUAGE\s+js\s+AS\s+R"""([\s\S]*?)"""', sql_text)
    assert len(js_bodies) == 2

    evil_name = 'evil"tool\\name\n'
    assert (
        jcs_tool_args_canonicalize_str(evil_name, '{"b": 1, "a": 2.0}')
        == '{"args":{"a":2,"b":1},"tool_name":"evil\\"tool\\\\name\\n"}'
    )
    assert jcs_tool_args_sha256_str(evil_name, '{"b": 1, "a": 2.0}') == jcs_sha256_str(
        '{"args":{"a":2,"b":1},"tool_name":"evil\\"tool\\\\name\\n"}'
    )
    assert jcs_canonicalize_str('{"__proto__":{"x":1},"a":1}') == '{"__proto__":{"x":1},"a":1}'
    assert jcs_canonicalize_str('{"x":1e400}') is None
    assert jcs_canonicalize_str('{"ok":"😀"}') == '{"ok":"😀"}'
    assert jcs_tool_args_canonicalize_str("d", "[" * 63 + "1" + "]" * 63) is not None
    assert jcs_tool_args_canonicalize_str("d", "[" * 64 + "1" + "]" * 64) is None
