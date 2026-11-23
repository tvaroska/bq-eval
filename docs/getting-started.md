# Getting Started

All commands run locally on the SQLite copy unless they take `--project-id`.

## CLI

Run the CLI, workbench, loadgen, judge SQL, bootstrap, or dry-run:

```bash
uv sync --extra dev
uv run python -m bq_eval.cli demo --html-out /tmp/bq_eval_scorecard.html
uv run python -m bq_eval.cli serve --port 8080
uv run python -m bq_eval.cli loadgen --replicas 50 --last-hour-replicas 15
uv run python -m bq_eval.cli judge-sql --engine generate-table --project-id my-gcp-project --canary-pct 5
uv run python -m bq_eval.cli bootstrap --mode hybrid --project-id my-gcp-project
uv run python scripts/bq_dry_run.py --offline-manifest /tmp/bq_dry_run_manifest.json
uv run python scripts/bq_dry_run.py --project-id YOUR_GCP_PROJECT  # requires authenticated `bq` CLI
```

Try `--corpus retail-2app`, `--format json`, or `uv run pytest -p no:cacheprovider -v`.

## Light Capture with BigQuery Agent Analytics

Three lines of ADK setup stream `agent_events` through the Storage Write API:

```python
bqaa_cfg = BigQueryLoggerConfig(custom_metadata={"apphub.application.id": "my-app"})
bqaa_plugin = BigQueryAgentAnalyticsPlugin(project_id="my-gcp-project", dataset_id="agent_eval", config=bqaa_cfg)
runner = Runner(agent=root_agent, app_name="my-app", session_service=session_svc, plugins=[bqaa_plugin])
```

Full Capture over OpenTelemetry `_AllSpans`, App Hub ownership and the adapter views are covered in [BQAA and dual-path architecture](bqaa-and-dual-path-architecture.md).

## Live GCP runs

Local runs use SQLite, `bq-eval bootstrap`, and `terraform test`. Live GCP `--dry_run` and `terraform apply` require `--project-id`, `bq`, and `gcloud auth`.
