"""BQ Eval & FinOps: BigQuery Bronze/Silver/Gold Agent Evaluation, Observability & FinOps Showcase.

Layered Ports, Adapters and Contracts across `bq_eval.{core,finops,deploy,runner,upload,demo,cli}`.
Top-level package stays free of heavy showcase or driving-port imports so importing `bq_eval`
never drags in seed corpora or HTTP servers.
"""

__version__ = "0.7.0rc1"

__all__ = ["__version__"]
