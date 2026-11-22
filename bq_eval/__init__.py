"""BQ Eval & FinOps: Dual-Path BigQuery Agent Evaluation, Observability & FinOps Showcase.

Layered Ports, Adapters and Contracts (`bq_eval.contract` -> `bq_eval.engine` ->
`bq_eval.showcase` -> `bq_eval.cli`). Top-level package stays free of heavy showcase
or driving-port imports so importing `bq_eval` never drags in seed corpora or HTTP servers.
"""

__version__ = "0.3.0"

__all__ = ["__version__"]
