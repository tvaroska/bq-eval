"""Resolve repo-root or wheel-bundled SQL and receipt data files without extra dependencies."""

from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[3]


def resolve_packaged_data(relative_path: str) -> Path:
    """Return path from repo checkout when present, falling back to wheel-bundled `_bundled/` data."""
    repo_candidate = _REPO_ROOT / relative_path
    if repo_candidate.exists():
        return repo_candidate
    bundled_candidate = _PACKAGE_ROOT / "_bundled" / relative_path
    if bundled_candidate.exists():
        return bundled_candidate
    return repo_candidate
