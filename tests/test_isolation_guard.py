"""Package and layer import guard.

Two axes, both enforced on every `bq_eval/**/*.py` file:

Packages (what may depend on what):
- `core`: rule checks, judge SQL, calibration, cost per outcome. Depends on nothing else.
- `finops`: chargeback and capacity export. Depends on `core`.
- `deploy`: bootstrap SQL rendering. Depends on `core`.
- `runner`: offline BigQuery-to-SQLite SQL runner (`sqlglot` allowed in `runner/engine/`). Depends on `core`.
- `demo`: corpus replay and workbench. Depends on `core`, `finops`, and `runner`.
- `cli`: may import any package.

Layers inside a package:
- `contract/`: only stdlib, `pydantic`, and the `contract` layers of allowed packages.
- `engine/`: only stdlib plus the `contract` and `engine` layers of allowed packages (no pydantic).
- `demo/render/`: only stdlib, `pydantic`, allowed `contract` layers and `demo.render`.
- package `__init__.py` files (except `demo` and `cli`) import nothing.

No file may import LLM SDKs, `examples/`, `benchmarks/`, or use dynamic imports.
"""

from __future__ import annotations

import ast
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

PACKAGE_DEPS: dict[str, tuple[str, ...]] = {
    "core": ("core",),
    "finops": ("core", "finops"),
    "deploy": ("core", "deploy"),
    "runner": ("core", "runner"),
    "upload": ("core", "upload"),
    "demo": ("core", "finops", "runner", "demo"),
}

_PURE_FORBIDDEN_STDLIB: frozenset[str] = frozenset({"subprocess", "urllib", "importlib"})
_BASE_FORBIDDEN_STDLIB: frozenset[str] = frozenset({"importlib"})


@dataclass(frozen=True)
class FolderRule:
    name: str
    allowed_bq_eval_prefixes: tuple[str, ...]
    allowed_third_party_roots: frozenset[str] = frozenset()
    forbidden_bq_eval_prefixes: tuple[str, ...] = ()
    forbidden_stdlib_roots: frozenset[str] = frozenset()


def _layer_prefixes(pkg: str, layers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"bq_eval.{dep}.{layer}" for dep in PACKAGE_DEPS[pkg] for layer in layers)


def _build_folder_rules() -> dict[str, FolderRule]:
    rules: dict[str, FolderRule] = {
        "root": FolderRule(
            name="root",
            allowed_bq_eval_prefixes=(),
            forbidden_stdlib_roots=_PURE_FORBIDDEN_STDLIB,
        ),
        "cli": FolderRule(
            name="cli",
            allowed_bq_eval_prefixes=("bq_eval",),
            forbidden_stdlib_roots=_BASE_FORBIDDEN_STDLIB,
        ),
        "demo_app": FolderRule(
            name="demo_app",
            allowed_bq_eval_prefixes=tuple(f"bq_eval.{d}" for d in PACKAGE_DEPS["demo"]),
            forbidden_bq_eval_prefixes=("bq_eval.cli",),
            forbidden_stdlib_roots=_BASE_FORBIDDEN_STDLIB,
        ),
        "demo_render": FolderRule(
            name="demo_render",
            allowed_bq_eval_prefixes=(
                *_layer_prefixes("demo", ("contract",)),
                "bq_eval.demo.render",
            ),
            allowed_third_party_roots=frozenset({"pydantic"}),
            forbidden_stdlib_roots=_PURE_FORBIDDEN_STDLIB,
        ),
    }
    for pkg in PACKAGE_DEPS:
        is_pure = pkg in {"core", "finops", "runner", "upload"}
        rules[f"{pkg}_contract"] = FolderRule(
            name=f"{pkg}_contract",
            allowed_bq_eval_prefixes=_layer_prefixes(pkg, ("contract",)),
            allowed_third_party_roots=frozenset({"pydantic"}),
            forbidden_stdlib_roots=_PURE_FORBIDDEN_STDLIB if is_pure else _BASE_FORBIDDEN_STDLIB,
        )
        if pkg != "demo":
            rules[f"{pkg}_engine"] = FolderRule(
                name=f"{pkg}_engine",
                allowed_bq_eval_prefixes=_layer_prefixes(pkg, ("contract", "engine")),
                allowed_third_party_roots=(
                    frozenset({"sqlglot"}) if pkg == "runner" else frozenset()
                ),
                forbidden_stdlib_roots=(
                    _PURE_FORBIDDEN_STDLIB if is_pure else _BASE_FORBIDDEN_STDLIB
                ),
            )
    return rules


FOLDER_RULES: dict[str, FolderRule] = _build_folder_rules()

_PREFIX_ROLE_MAP: tuple[tuple[str, str], ...] = (
    *((f"{pkg}/contract/", f"{pkg}_contract") for pkg in PACKAGE_DEPS),
    *((f"{pkg}/engine/", f"{pkg}_engine") for pkg in PACKAGE_DEPS if pkg != "demo"),
    ("demo/render/", "demo_render"),
    ("demo/", "demo_app"),
    ("cli/", "cli"),
)
_EXACT_ROLE_MAP: dict[str, str] = {
    "__init__.py": "root",
    **{f"{pkg}/__init__.py": "root" for pkg in PACKAGE_DEPS if pkg != "demo"},
}


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
    module_rel_path: str = "bq_eval/core/engine/probe.py",
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
    if top in rule.forbidden_stdlib_roots:
        return False
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
    """An unlisted root module `bq_eval/extra_core.py` cannot slip past the guard."""
    pkg = tmp_path / "bq_eval"
    (pkg / "core" / "engine").mkdir(parents=True)
    (pkg / "__init__.py").write_text("__version__ = '0.3.0'\n", encoding="utf-8")
    (pkg / "extra_core.py").write_text(
        "from bq_eval.demo.pipeline import run_showcase_pipeline\n",
        encoding="utf-8",
    )
    (pkg / "core" / "engine" / "leaked.py").write_text(
        "from bq_eval.finops.engine.spend import compute_pareto_frontier\n",
        encoding="utf-8",
    )
    _, problems = _scan_package_tree(pkg)
    assert any("extra_core.py" in p and UNCLASSIFIED_MODULE in p for p in problems)
    assert any(
        "core/engine/leaked.py" in p and "bq_eval.finops.engine.spend" in p for p in problems
    )


@pytest.mark.parametrize(
    ("module_rel_path", "statement"),
    [
        # Package direction: core depends on nothing else.
        (
            "bq_eval/core/engine/probe.py",
            "from bq_eval.finops.engine.spend import compute_pareto_frontier",
        ),
        (
            "bq_eval/core/contract/probe.py",
            "from bq_eval.finops.contract.results import ModelOption",
        ),
        ("bq_eval/core/engine/probe.py", "from bq_eval.deploy.engine.bootstrap import REPO_ROOT"),
        (
            "bq_eval/core/engine/probe.py",
            "from bq_eval.demo.loadgen import build_golden_seed_templates",
        ),
        (
            "bq_eval/finops/engine/probe.py",
            "from bq_eval.deploy.engine.bootstrap import REPO_ROOT",
        ),
        (
            "bq_eval/finops/engine/probe.py",
            "from bq_eval.demo.pipeline import run_showcase_pipeline",
        ),
        (
            "bq_eval/deploy/engine/probe.py",
            "from bq_eval.finops.engine.spend import compute_pareto_frontier",
        ),
        (
            "bq_eval/demo/pipeline.py",
            "from bq_eval.deploy.engine.bootstrap import REPO_ROOT",
        ),
        ("bq_eval/demo/pipeline.py", "from bq_eval.cli.app import main"),
        # Layer direction inside a package.
        (
            "bq_eval/core/contract/probe.py",
            "from bq_eval.core.engine.cost import compute_span_cost_usd",
        ),
        ("bq_eval/core/engine/probe.py", "from pydantic import BaseModel"),
        ("bq_eval/core/engine/probe.py", "from bq_eval.cli.app import main"),
        ("bq_eval/core/engine/probe.py", "from ...demo import loadgen"),
        (
            "bq_eval/demo/render/renderer.py",
            "from bq_eval.core.engine.calibration import compute_gwet_ac2",
        ),
        (
            "bq_eval/demo/render/renderer.py",
            "from bq_eval.demo.pipeline import run_showcase_pipeline",
        ),
        ("bq_eval/demo/render/renderer.py", "from .. import loadgen"),
        ("bq_eval/core/__init__.py", "from bq_eval.core.engine.cost import compute_span_cost_usd"),
        # Global bans.
        ("bq_eval/cli/app.py", "from google.genai import Client"),
        ("bq_eval/cli/app.py", "import vertexai.preview"),
        ("bq_eval/demo/pipeline.py", "import openai"),
        ("bq_eval/core/engine/probe.py", "import benchmarks.results"),
        (
            "bq_eval/core/engine/probe.py",
            "import importlib\nimportlib.import_module('bq_eval.cli')",
        ),
        ("bq_eval/core/engine/probe.py", "__import__('bq_eval.cli')"),
        (
            "bq_eval/core/engine/probe.py",
            "from bq_eval.runner.engine.loader import OfflineSqlRunner",
        ),
        ("bq_eval/core/engine/probe.py", "import sqlglot"),
        ("bq_eval/finops/engine/probe.py", "import sqlglot"),
        (
            "bq_eval/finops/engine/probe.py",
            "from bq_eval.runner.engine.loader import OfflineSqlRunner",
        ),
        (
            "bq_eval/deploy/engine/probe.py",
            "from bq_eval.runner.engine.loader import OfflineSqlRunner",
        ),
        ("bq_eval/runner/engine/probe.py", "from bq_eval.finops.engine import spend"),
        ("bq_eval/runner/engine/probe.py", "from bq_eval.deploy.engine import bootstrap"),
        ("bq_eval/runner/engine/probe.py", "from bq_eval.demo import pipeline"),
        ("bq_eval/runner/engine/probe.py", "from bq_eval.cli.app import main"),
        ("bq_eval/runner/engine/probe.py", "import subprocess"),
        ("bq_eval/runner/engine/probe.py", "import urllib.request as req"),
        ("bq_eval/core/engine/probe.py", "import urllib.request as req"),
        ("bq_eval/core/engine/probe.py", "import importlib as il"),
        ("bq_eval/core/engine/probe.py", "from .... import anything"),
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
        ("bq_eval/core/contract/probe.py", "from pydantic import BaseModel"),
        (
            "bq_eval/core/contract/probe.py",
            "from bq_eval.core.contract.otel import OTelGenAISpanRecord",
        ),
        (
            "bq_eval/core/engine/probe.py",
            "from bq_eval.core.contract.otel import OTelGenAISpanRecord",
        ),
        ("bq_eval/core/engine/probe.py", "from .cost import compute_span_cost_usd"),
        (
            "bq_eval/finops/engine/probe.py",
            "from bq_eval.core.engine.cost import compute_span_cost_usd",
        ),
        (
            "bq_eval/finops/contract/probe.py",
            "from bq_eval.core.contract.results import ConfusionMatrix",
        ),
        (
            "bq_eval/deploy/engine/probe.py",
            "from bq_eval.deploy.contract.bootstrap import BootstrapMode",
        ),
        (
            "bq_eval/demo/render/renderer.py",
            "from bq_eval.demo.contract.results import ShowcaseRunArtifacts",
        ),
        ("bq_eval/demo/render/renderer.py", "from .view import PageView"),
        (
            "bq_eval/demo/pipeline.py",
            "from bq_eval.core.engine.sql_eval import evaluate_traces_in_sqlite",
        ),
        ("bq_eval/runner/contract/probe.py", "from pydantic import BaseModel"),
        ("bq_eval/runner/engine/probe.py", "import sqlglot"),
        ("bq_eval/runner/engine/probe.py", "from bq_eval.runner.contract.spec import RunnerConfig"),
        ("bq_eval/demo/pipeline.py", "from bq_eval.runner.engine.loader import OfflineSqlRunner"),
        ("bq_eval/cli/app.py", "from bq_eval.demo.pipeline import run_showcase_pipeline"),
        (
            "bq_eval/cli/app.py",
            "from bq_eval.finops.engine.chargeback_three_pool import allocate_focus_three_pool_hourly_chargeback",
        ),
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
    ast_grep = REPO_ROOT / ".venv" / "bin" / "ast-grep"
    assert ast_grep.is_file(), "ast-grep binary required in .venv/bin/ast-grep"

    cases = (
        ("bq_eval/core/contract/probe.py", "from ..engine import cost\n"),
        ("bq_eval/core/engine/probe.py", "from pydantic import BaseModel\n"),
        ("bq_eval/core/engine/probe.py", "from bq_eval.finops.engine import spend\n"),
        ("bq_eval/core/engine/probe.py", "from ...demo import loadgen\n"),
        ("bq_eval/finops/engine/probe.py", "from bq_eval.demo import pipeline\n"),
        ("bq_eval/deploy/engine/probe.py", "from bq_eval.finops.engine import spend\n"),
        (
            "bq_eval/demo/render/renderer_probe.py",
            "from bq_eval.core.engine.cost import compute_span_cost_usd\n",
        ),
        ("bq_eval/demo/render/renderer_probe.py", "from .. import pipeline\n"),
        ("bq_eval/cli/probe.py", "from google.genai import Client\n"),
        ("bq_eval/runner/engine/probe.py", "from bq_eval.deploy.engine import bootstrap\n"),
        ("bq_eval/core/engine/probe.py", "from bq_eval.runner.engine import loader\n"),
        ("bq_eval/upload/engine/probe.py", "from bq_eval.runner.engine import loader\n"),
        ("bq_eval/demo/probe.py", "from bq_eval.upload.engine import hook\n"),
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


def test_ast_grep_flags_subprocess_in_core(tmp_path: Path) -> None:
    """`ast-grep` catches list-subprocess, var/format SQL, aliased importlib/urllib, 4-dot escapes, and abs paths (`A-21`)."""
    ast_grep = REPO_ROOT / ".venv" / "bin" / "ast-grep"
    assert ast_grep.is_file(), "ast-grep binary required in .venv/bin/ast-grep"

    a21_cases = (
        ("bq_eval/core/engine/probe.py", 'subprocess.run(["bq", "version"], check=True)\n'),
        ("bq_eval/core/engine/probe.py", "import subprocess as sp\n"),
        ("bq_eval/core/engine/probe.py", "import urllib.request as req\n"),
        ("bq_eval/core/engine/probe.py", "import importlib as il\n"),
        ("bq_eval/core/engine/probe.py", "from ....demo import loadgen\n"),
        (
            "bq_eval/core/engine/probe.py",
            'q = f"SELECT * FROM spans WHERE id = {tid}"\nconn.execute(q)\n',
        ),
        (
            "bq_eval/core/engine/probe.py",
            'conn.execute("SELECT * FROM spans WHERE id = {}".format(tid))\n',
        ),
        ("bq_eval/finops/engine/probe.py", 'subprocess.run(["echo", "hi"], check=False)\n'),
        ("bq_eval/runner/engine/probe.py", 'subprocess.run(["echo", "hi"], check=False)\n'),
        ("bq_eval/upload/engine/probe.py", 'subprocess.run(["echo", "hi"], check=False)\n'),
    )
    for rel_path, snippet in a21_cases:
        probe_file = tmp_path / rel_path
        probe_file.parent.mkdir(parents=True, exist_ok=True)
        probe_file.write_text(snippet, encoding="utf-8")
        for scan_target in (".", str(probe_file)):
            res = subprocess.run(  # noqa: S603
                [str(ast_grep), "scan", "--rule", str(AST_RULES_PATH), scan_target],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 1, (
                f"ast-grep missed {rel_path} (target={scan_target}): {snippet!r}\n{res.stdout}"
            )
        probe_file.unlink()


def test_importing_core_loads_no_other_package() -> None:
    """Importing every `bq_eval.core` module pulls in no other bq_eval package, sqlglot, or LLM SDK."""
    pure_modules = [
        _dotted_module_name(path)
        for path in _all_package_modules()
        if path.relative_to(PACKAGE_DIR).as_posix().startswith("core/")
        or path == PACKAGE_DIR / "__init__.py"
    ]
    probe = (
        "import sys\n"
        + "".join(f"import {mod}\n" for mod in pure_modules)
        + "print(sorted(m for m in sys.modules if m.startswith(("
        "'bq_eval.finops', 'bq_eval.deploy', 'bq_eval.runner', 'bq_eval.upload', 'bq_eval.demo', 'bq_eval.cli', "
        "'sqlglot', 'google.genai', 'vertexai', 'openai', 'anthropic'"
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
    """All `bq_eval/**/*.py` stay <= 800 LOC and seed files (`*_seeds.py`) stay < 500 LOC."""
    oversized: list[str] = []
    seed_files_checked = 0
    for py_path in _all_package_modules():
        loc = sum(
            1
            for line in py_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        rel = py_path.relative_to(PACKAGE_DIR).as_posix()
        if loc > 800:
            oversized.append(f"{rel}: {loc} non-blank/non-comment lines (> 800 ceiling)")
        if py_path.name.endswith("_seeds.py"):
            seed_files_checked += 1
            if loc >= 500:
                oversized.append(f"{rel}: {loc} non-blank/non-comment lines (>= 500 seed ceiling)")
    assert seed_files_checked >= 1, "Expected at least one *_seeds.py module to guard"
    assert not oversized, "Structural LOC ceiling exceeded:\n" + "\n".join(oversized)
