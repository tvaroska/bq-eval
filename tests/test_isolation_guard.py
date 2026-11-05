"""AST Import Isolation Guard (`Rule 0`) for bq-eval."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "bq_eval"

DETERMINISTIC_CORE_MODULES = (
    "otel.py",
    "rubrics.py",
    "calibration.py",
    "sql_eval.py",
    "finops.py",
)

FORBIDDEN_IMPORT_PREFIXES = (
    "google.genai",
    "vertexai",
    "openai",
    "anthropic",
    "langchain",
    "llama_index",
    "bq_eval.cli",
    "bq_eval.showcase",
)


def find_forbidden_imports(source_code: str, filename: str = "<memory>") -> list[str]:
    """Scan Python AST for prohibited SDK or upward layer imports."""
    tree = ast.parse(source_code, filename=filename)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == prefix or alias.name.startswith(f"{prefix}.")
                    for prefix in FORBIDDEN_IMPORT_PREFIXES
                ):
                    violations.append(f"{filename}:{node.lineno} imports '{alias.name}'")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if any(
                node.module == prefix or node.module.startswith(f"{prefix}.")
                for prefix in FORBIDDEN_IMPORT_PREFIXES
            ):
                violations.append(f"{filename}:{node.lineno} imports from '{node.module}'")
    return violations


@pytest.mark.parametrize("module_name", DETERMINISTIC_CORE_MODULES)
def test_deterministic_core_has_zero_forbidden_imports(module_name: str) -> None:
    module_path = PACKAGE_DIR / module_name
    assert module_path.exists(), f"Missing core module: {module_name}"
    source = module_path.read_text(encoding="utf-8")
    violations = find_forbidden_imports(source, filename=module_name)
    assert not violations, f"AST Isolation Violation in {module_name}: {violations}"


def test_isolation_scanner_positive_control() -> None:
    """Verify the AST scanner catches prohibited imports when injected."""
    synthetic_bad_code = (
        "import math\n"
        "import vertexai.preview\n"
        "from google.genai import Client\n"
        "from bq_eval.cli import main\n"
    )
    violations = find_forbidden_imports(synthetic_bad_code, filename="synthetic.py")
    assert len(violations) == 3
    assert any("vertexai.preview" in v for v in violations)
    assert any("google.genai" in v for v in violations)
    assert any("bq_eval.cli" in v for v in violations)
