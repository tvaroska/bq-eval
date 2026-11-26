FROM python:3.12-slim AS base

RUN apt-get update \
  && apt-get upgrade -y --no-install-recommends \
  && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --no-config

COPY bq_eval/ bq_eval/
COPY sql/ sql/
COPY benchmarks/results/m2_live_receipt.json \
     benchmarks/results/m16_live_cloud_proof.json \
     benchmarks/results/m16_anonymized_traces_100.jsonl \
     benchmarks/results/bq_dry_run_receipt.json \
     benchmarks/results/SHA256SUMS \
     benchmarks/results/
RUN uv sync --frozen --no-dev --no-config \
  && rm -rf /usr/local/lib/python3.12/site-packages/pip* \
            /usr/local/lib/python3.12/site-packages/setuptools* \
            /usr/local/lib/python3.12/site-packages/wheel* \
            /usr/local/lib/python3.12/ensurepip \
            /bin/uv /bin/uvx

RUN useradd --system --uid 10001 --no-create-home app
USER 10001

ENV PORT=8080
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/app/.venv/bin/python", "-c", "import os, urllib.request; port = os.environ.get('PORT', '8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3)"]

CMD ["sh", "-c", "exec /app/.venv/bin/bq-eval serve --host 0.0.0.0 --allow-non-loopback --port ${PORT:-8080}"]
