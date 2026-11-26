#!/usr/bin/env bash
# Canonical verification gate for bq-eval.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PATH="${HOME}/.local/bin:${PATH}"
export PYTHONDONTWRITEBYTECODE=1

if [[ "${1:-}" == "cli" ]]; then
  shift
  if [[ ! -x ".venv/bin/python" ]]; then
    uv sync --frozen --extra dev --offline --no-config
  fi
  exec uv run --no-sync python -m bq_eval.cli "$@"
fi

uv lock --check --offline --no-config
if [[ "${BQ_EVAL_ONLINE_SYNC:-0}" == "1" ]]; then
  uv sync --frozen --extra dev --no-config
else
  uv sync --frozen --extra dev --offline --no-config
fi

if [[ ! -x ".venv/bin/ast-grep" ]]; then
  echo "ERROR: .venv/bin/ast-grep missing (run uv sync --frozen --extra dev)." >&2
  exit 1
fi
.venv/bin/ast-grep scan --rule specs/ast.rules.yaml

uv run --no-sync ruff format --check --no-cache bq_eval tests examples scripts
uv run --no-sync ruff check --no-cache bq_eval tests examples scripts
uv run --no-sync mypy --strict --cache-dir=/tmp/top3-mypy-bq-eval bq_eval tests examples scripts
uv run --no-sync pytest -p no:cacheprovider -v -rs "$@"

if [[ $# -eq 0 ]]; then
  RUN_TMP="$(mktemp -d /tmp/bq_eval_gate.XXXXXX)"
  trap 'rm -rf "${RUN_TMP}"' EXIT
  uv run --no-sync bq-eval demo --html-out "${RUN_TMP}/scorecard.html" >/dev/null
  uv run --no-sync bq-eval loadgen --replicas 5 --last-hour-replicas 2 >/dev/null
  uv run --no-sync bq-eval judge-sql --project-id demo-proj >/dev/null
  uv run --no-sync bq-eval judge-sql --engine evaluate-agent --project-id demo-proj >/dev/null
  uv run --no-sync bq-eval bootstrap --mode light --project-id demo-proj >/dev/null
  uv run --no-sync bq-eval bootstrap --mode full --project-id demo-proj >/dev/null
  uv run --no-sync bq-eval bootstrap --mode hybrid --project-id demo-proj >/dev/null
  uv run --no-sync bq-eval capacity-export --corpus m2-live >/dev/null
  uv run --no-sync python -m bq_eval.runner.engine.loader --manifest tests/fixtures/silver/manifest.json --max-seconds 5.0 >/dev/null
  uv run --no-sync python examples/run_showcase_demo.py >/dev/null
fi
