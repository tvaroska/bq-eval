"""Folder-level AST Import & Layer Isolation Guard (`Rule 0`, `M7-1`, `F13`).

Enforces strict Ports, Adapters and Contracts across every `bq_eval/**/*.py` file:
- `__init__.py` (`root`): imports nothing from `bq_eval.*` or third-party libraries.
- `contract/`: imports only stdlib, `pydantic`, and `bq_eval.contract`.
- `engine/`: imports only stdlib, `bq_eval.contract`, and `bq_eval.engine` (zero `pydantic`,
  zero `showcase`, zero `cli`, zero LLM SDKs).
- `showcase/{__init__,loadgen,pipeline,seeds_banking,seeds_claims}.py`: imports only stdlib,
  `bq_eval.contract`, `bq_eval.engine`, and `bq_eval.showcase` (zero `cli`, zero LLM SDKs).
- `showcase/{view,renderer}.py`: imports zero calculations — only stdlib, `pydantic`,
  `bq_eval.contract`, and sibling renderer modules.
- `cli/`: driving ports — may import `bq_eval.*` and stdlib, with zero LLM SDK imports.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "bq_eval"
AST_RULES_PATH = REPO_ROOT / "specs" / "ast.rules.yaml"

DYNAMIC_IMPORT = "<dynamic import>"
ESCAPED_RELATIVE_IMPORT = "<escaped relative import>"
UNCLASSIFIED_MODULE = "<unclassified module>"
STDLIB_ROOTS: frozenset[str] = sys.stdlib_module_names

FORBIDDEN_LLM_SDK_PREFIXES: tuple[str, ...] = (
    "google.genai",
    "vertexai",
    "openai",
    "anthropic",
    "langchain",
    "llama_index",
)

SHOWCASE_EVAL_FILES: frozenset[str] = frozenset(
    {
        "showcase/__init__.py",
        "showcase/loadgen.py",
        "showcase/pipeline.py",
        "showcase/seeds_banking.py",
        "showcase/seeds_claims.py",
    }
)


@dataclass(frozen=True)
class FolderRule:
    name: str
    allowed_bq_eval_prefixes: tuple[str, ...]
    allowed_third_party_roots: frozenset[str] = frozenset()
    forbidden_bq_eval_prefixes: tuple[str, ...] = ()


FOLDER_RULES: dict[str, FolderRule] = {
    "root": FolderRule(name="root", allowed_bq_eval_prefixes=()),
    "contract": FolderRule(
        name="contract",
        allowed_bq_eval_prefixes=("bq_eval.contract",),
        allowed_third_party_roots=frozenset({"pydantic"}),
    ),
    "engine": FolderRule(
        name="engine",
        allowed_bq_eval_prefixes=("bq_eval.contract", "bq_eval.engine"),
    ),
    "showcase_eval": FolderRule(
        name="showcase_eval",
        allowed_bq_eval_prefixes=("bq_eval.contract", "bq_eval.engine", "bq_eval.showcase"),
        forbidden_bq_eval_prefixes=("bq_eval.cli",),
    ),
    "showcase_renderer": FolderRule(
        name="showcase_renderer",
        allowed_bq_eval_prefixes=(
            "bq_eval.contract",
            "bq_eval.showcase.view",
            "bq_eval.showcase.renderer",
        ),
        allowed_third_party_roots=frozenset({"pydantic"}),
        forbidden_bq_eval_prefixes=(
            "bq_eval.engine",
            "bq_eval.cli",
            "bq_eval.showcase.loadgen",
            "bq_eval.showcase.pipeline",
            "bq_eval.showcase.seeds_banking",
            "bq_eval.showcase.seeds_claims",
        ),
    ),
    "cli": FolderRule(
        name="cli",
        allowed_bq_eval_prefixes=("bq_eval",),
    ),
}

_EXACT_ROLE_MAP: dict[str, str] = {
    "__init__.py": "root",
    **dict.fromkeys(SHOWCASE_EVAL_FILES, "showcase_eval"),
}
_PREFIX_ROLE_MAP: tuple[tuple[str, str], ...] = (
    ("contract/", "contract"),
    ("engine/", "engine"),
    ("showcase/", "showcase_renderer"),
    ("cli/", "cli"),
)


def _all_package_modules(root: Path = PACKAGE_DIR) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if "__pycache__" not in p.parts and not p.name.startswith(".")
    )


def _classify_rel_path(rel_posix: str) -> str | None:
    if rel_posix in _EXACT_ROLE_MAP:
        return _EXACT_ROLE_MAP[rel_posix]
    for prefix, role in _PREFIX_ROLE_MAP:
        if rel_posix.startswith(prefix):
            return role
    return None


def _dotted_module_name(path: Path, repo_root: Path = REPO_ROOT) -> str:
    rel = path.relative_to(repo_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _matches_prefix(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(f"{prefix}.")


def _import_targets(
    source: str,
    *,
    module_rel_path: str = "bq_eval/engine/probe.py",
) -> list[str]:
    pkg_parts = list(Path(module_rel_path).parent.parts)
    targets: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = node.level - 1
                if up >= len(pkg_parts):
                    targets.append(ESCAPED_RELATIVE_IMPORT)
                    continue
                anchor = ".".join(pkg_parts[: len(pkg_parts) - up])
            else:
                anchor = ""
            base = ".".join(part for part in (anchor, node.module or "") if part)
            targets.append(base)
            targets.extend(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            if name in {"import_module", "__import__"}:
                targets.append(DYNAMIC_IMPORT)
    return targets


def _is_bq_eval_target_allowed(target: str, rule: FolderRule) -> bool:
    if target == "bq_eval":
        return "bq_eval" in rule.allowed_bq_eval_prefixes
    if any(_matches_prefix(target, pfx) for pfx in rule.forbidden_bq_eval_prefixes):
        return False
    return any(_matches_prefix(target, pfx) for pfx in rule.allowed_bq_eval_prefixes)


def _is_target_allowed(target: str, rule: FolderRule) -> bool:
    if target in {DYNAMIC_IMPORT, ESCAPED_RELATIVE_IMPORT}:
        return False
    if any(_matches_prefix(target, sdk) for sdk in FORBIDDEN_LLM_SDK_PREFIXES):
        return False
    top = target.split(".", 1)[0]
    if top in {"examples", "benchmarks"}:
        return False
    if top == "bq_eval":
        return _is_bq_eval_target_allowed(target, rule)
    return top in STDLIB_ROOTS or top in rule.allowed_third_party_roots


def _violations_for_module(source: str, *, module_rel_path: str) -> list[str]:
    rel_inside_pkg = Path(module_rel_path).relative_to("bq_eval").as_posix()
    role = _classify_rel_path(rel_inside_pkg)
    if role is None:
        return [f"{UNCLASSIFIED_MODULE}:{rel_inside_pkg}"]

    rule = FOLDER_RULES[role]
    return [
        target
        for target in _import_targets(source, module_rel_path=module_rel_path)
        if not _is_target_allowed(target, rule)
    ]


def _scan_package_tree(pkg_dir: Path) -> tuple[set[str], list[str]]:
    seen_roles: set[str] = set()
    problems: list[str] = []
    parent_root = pkg_dir.parent
    for path in _all_package_modules(pkg_dir):
        rel_inside = path.relative_to(pkg_dir).as_posix()
        role = _classify_rel_path(rel_inside)
        if role is not None:
            seen_roles.add(role)
        rel_repo = path.relative_to(parent_root).as_posix()
        for bad_target in _violations_for_module(
            path.read_text(encoding="utf-8"),
            module_rel_path=rel_repo,
        ):
            problems.append(f"{rel_repo} ({role or 'unclassified'}) -> {bad_target}")
    return seen_roles, problems


def test_every_folder_has_one_rule_and_zero_violations() -> None:
    seen_roles, problems = _scan_package_tree(PACKAGE_DIR)
    assert seen_roles == set(FOLDER_RULES), f"Unused folder rules: {set(FOLDER_RULES) - seen_roles}"
    assert not problems, "\n".join(problems)


def test_unlisted_module_and_leaked_import_positive_controls(tmp_path: Path) -> None:
    """F13 positive control: an unlisted root module `bq_eval/extra_core.py` cannot slip past the guard."""
    pkg = tmp_path / "bq_eval"
    (pkg / "engine").mkdir(parents=True)
    (pkg / "__init__.py").write_text("__version__ = '0.3.0'\n", encoding="utf-8")
    (pkg / "extra_core.py").write_text(
        "from bq_eval.showcase.pipeline import run_showcase_pipeline\n",
        encoding="utf-8",
    )
    (pkg / "engine" / "leaked.py").write_text(
        "from bq_eval.showcase.loadgen import generate_time_shifted_corpus\n",
        encoding="utf-8",
    )
    _, problems = _scan_package_tree(pkg)
    assert any("extra_core.py" in p and UNCLASSIFIED_MODULE in p for p in problems)
    assert any("engine/leaked.py" in p and "bq_eval.showcase.loadgen" in p for p in problems)


@pytest.mark.parametrize(
    ("module_rel_path", "statement"),
    [
        ("bq_eval/contract/probe.py", "from bq_eval.engine.finops import compute_span_cost_usd"),
        (
            "bq_eval/contract/probe.py",
            "from bq_eval.showcase.pipeline import run_showcase_pipeline",
        ),
        ("bq_eval/engine/probe.py", "from pydantic import BaseModel"),
        (
            "bq_eval/engine/probe.py",
            "from bq_eval.showcase.loadgen import build_golden_seed_templates",
        ),
        ("bq_eval/engine/probe.py", "from bq_eval.cli.app import main"),
        ("bq_eval/engine/probe.py", "from ..showcase import loadgen"),
        ("bq_eval/showcase/pipeline.py", "from bq_eval.cli.app import main"),
        (
            "bq_eval/showcase/renderer.py",
            "from bq_eval.engine.finops import compute_pareto_frontier",
        ),
        (
            "bq_eval/showcase/renderer.py",
            "from bq_eval.showcase.pipeline import run_showcase_pipeline",
        ),
        ("bq_eval/showcase/renderer.py", "from . import loadgen"),
        ("bq_eval/showcase/view.py", "from bq_eval.engine.calibration import compute_gwet_ac2"),
        ("bq_eval/cli/app.py", "from google.genai import Client"),
        ("bq_eval/cli/app.py", "import vertexai.preview"),
        ("bq_eval/showcase/pipeline.py", "import openai"),
        ("bq_eval/engine/probe.py", "import benchmarks.results"),
        ("bq_eval/engine/probe.py", "import importlib\nimportlib.import_module('bq_eval.cli')"),
        ("bq_eval/engine/probe.py", "__import__('bq_eval.cli')"),
        ("bq_eval/engine/probe.py", "from ... import anything"),
        ("bq_eval/extra_unlisted.py", "import math"),
    ],
)
def test_folder_guard_catches_every_forbidden_import(
    module_rel_path: str,
    statement: str,
) -> None:
    assert _violations_for_module(statement, module_rel_path=module_rel_path), (
        f"{module_rel_path}: {statement}"
    )


@pytest.mark.parametrize(
    ("module_rel_path", "statement"),
    [
        ("bq_eval/contract/probe.py", "from pydantic import BaseModel"),
        ("bq_eval/contract/probe.py", "from bq_eval.contract.otel import OTelGenAISpanRecord"),
        ("bq_eval/engine/probe.py", "from bq_eval.contract.otel import OTelGenAISpanRecord"),
        ("bq_eval/engine/probe.py", "from .finops import compute_span_cost_usd"),
        (
            "bq_eval/showcase/renderer.py",
            "from bq_eval.contract.results import ShowcaseRunArtifacts",
        ),
        ("bq_eval/showcase/renderer.py", "from .view import PageView"),
        (
            "bq_eval/showcase/pipeline.py",
            "from bq_eval.engine.sql_eval import evaluate_traces_in_sqlite",
        ),
        ("bq_eval/cli/app.py", "from bq_eval.showcase.pipeline import run_showcase_pipeline"),
    ],
)
def test_folder_guard_allows_declared_dependencies(
    module_rel_path: str,
    statement: str,
) -> None:
    assert not _violations_for_module(statement, module_rel_path=module_rel_path), (
        f"{module_rel_path}: {statement}"
    )


def test_subfolder_probes_fail_both_python_and_ast_grep_guards(tmp_path: Path) -> None:
    venv_ast_grep = REPO_ROOT / ".venv" / "bin" / "ast-grep"
    ast_grep = (
        venv_ast_grep
        if venv_ast_grep.is_file()
        else Path(shutil.which("ast-grep") or str(Path.home() / ".local" / "bin" / "ast-grep"))
    )
    assert ast_grep.is_file(), "ast-grep binary required"

    cases = (
        ("bq_eval/contract/probe.py", "from ..engine import finops\n"),
        ("bq_eval/engine/probe.py", "from pydantic import BaseModel\n"),
        ("bq_eval/engine/probe.py", "from ..showcase import loadgen\n"),
        (
            "bq_eval/showcase/renderer_probe.py",
            "from ..engine.finops import compute_span_cost_usd\n",
        ),
        ("bq_eval/showcase/renderer_probe.py", "from . import pipeline\n"),
        ("bq_eval/cli/probe.py", "from google.genai import Client\n"),
    )
    for rel_path, bad_import in cases:
        probe_file = tmp_path / rel_path
        probe_file.parent.mkdir(parents=True, exist_ok=True)
        probe_file.write_text(bad_import, encoding="utf-8")
        assert _violations_for_module(bad_import, module_rel_path=rel_path), (rel_path, bad_import)

        res = subprocess.run(  # noqa: S603
            [str(ast_grep), "scan", "--rule", str(AST_RULES_PATH), "."],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 1, f"ast-grep missed {rel_path}: {bad_import!r}"
        probe_file.unlink()


def test_importing_contract_and_engine_loads_no_showcase_or_cli() -> None:
    """Importing `bq_eval`, `bq_eval.contract`, and `bq_eval.engine` pulls in zero showcase/CLI modules."""
    pure_modules = [
        _dotted_module_name(path)
        for path in _all_package_modules()
        if _classify_rel_path(path.relative_to(PACKAGE_DIR).as_posix())
        in {"root", "contract", "engine"}
    ]
    probe = (
        "import sys\n"
        + "".join(f"import {mod}\n" for mod in pure_modules)
        + "print(sorted(m for m in sys.modules if m.startswith(("
        "'bq_eval.showcase', 'bq_eval.cli', 'google.genai', 'vertexai', 'openai', 'anthropic'"
        "))))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_all_production_modules_and_seed_files_respect_loc_ceilings() -> None:
    """All `bq_eval/**/*.py` stay <= 800 LOC and split `seeds_*.py` stay < 500 LOC (`M7-1`)."""
    oversized: list[str] = []
    for py_path in _all_package_modules():
        loc = sum(
            1
            for line in py_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        rel = py_path.relative_to(PACKAGE_DIR).as_posix()
        if loc > 800:
            oversized.append(f"{rel}: {loc} non-blank/non-comment lines (> 800 ceiling)")
        if py_path.name.startswith("seeds_") and loc >= 500:
            oversized.append(f"{rel}: {loc} non-blank/non-comment lines (>= 500 seed ceiling)")
    assert not oversized, "Structural LOC ceiling exceeded:\n" + "\n".join(oversized)
