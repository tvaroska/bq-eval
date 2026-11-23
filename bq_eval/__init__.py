"""BQ Eval & FinOps: Dual-Path BigQuery Agent Evaluation, Observability & FinOps Showcase.

Layered Ports, Adapters and Contracts across `bq_eval.{core,finops,deploy,demo,cli}`.
Top-level package stays free of heavy showcase or driving-port imports so importing `bq_eval`
never drags in seed corpora or HTTP servers.
"""

__version__ = "0.4.0"

__all__ = ["__version__"]
