#!/usr/bin/env bash
# Canonical verification gate for bq-eval.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PATH="${HOME}/.local/bin:${PATH}"
export PYTHONDONTWRITEBYTECODE=1

if [[ ! -x ".venv/bin/pytest" ]]; then
  uv sync --extra dev --offline --no-config
fi
uv lock --check --offline --no-config

if [[ -x ".venv/bin/ast-grep" ]]; then
  .venv/bin/ast-grep scan --rule specs/ast.rules.yaml
elif command -v ast-grep >/dev/null 2>&1; then
  ast-grep scan --rule specs/ast.rules.yaml
else
  echo "ERROR: ast-grep binary missing (run uv sync --extra dev)." >&2
  exit 1
fi

uv run --no-sync ruff format --check --no-cache bq_eval tests examples scripts
uv run --no-sync ruff check --no-cache bq_eval tests examples scripts
uv run --no-sync mypy --strict --cache-dir=/tmp/top3-mypy-bq-eval bq_eval tests examples scripts
uv run --no-sync pytest -p no:cacheprovider -v "$@"

if [[ $# -eq 0 ]]; then
  RUN_TMP="$(mktemp -d /tmp/bq_eval_gate.XXXXXX)"
  trap 'rm -rf "${RUN_TMP}"' EXIT
  uv run --no-sync bq-eval demo --html-out "${RUN_TMP}/scorecard.html" >/dev/null
  uv run --no-sync bq-eval loadgen --replicas 5 --last-hour-replicas 2 >/dev/null
  uv run --no-sync bq-eval judge-sql --project-id demo-proj >/dev/null
  uv run --no-sync bq-eval judge-sql --engine evaluate-agent --project-id demo-proj >/dev/null
  uv run --no-sync python examples/run_showcase_demo.py >/dev/null
fi
