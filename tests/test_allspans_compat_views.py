"""Compatibility-view contract tests.

Asserts the Step-5 and Step-8 BigQuery SQL contract for native Telemetry API +
Observability Analytics linked datasets (`obs_traces._AllSpans` and
`obs_logs._AllLogs` joined with the `completions` BigLake external table):

1. `agent_eval.otel_spans` and `agent_eval.trace_payloads` are defined in
   `sql/core/ddl.sql` as `CREATE OR REPLACE VIEW` over the linked datasets rather than
   custom-ingested physical tables.
2. `agent_eval.otel_spans` projects all 25 mapped columns required by Step-1/Step-2
   views plus outcome attribution (`span_name` <- `name`, `outcome_id` <-
   `outcome.id` baggage, `rule0_pass`, `finops_traffic_type`, `thinking_tokens`, `gcp_apphub_*`).
3. Outcome and attribution views (`v_outcomes` reading `span_name = 'outcome.closed'`,
   `v_cost_per_verified_outcome`, `v_attribution_coverage`, and
   `v_otel_spans_coverage`) are defined in `sql/`.
4. Synthetic seed tables (`seed_otel_spans`, `seed_trace_payloads`) and
   `sql/demo/demo_time_shift_loadgen.sql` are isolated to dataset `agent_eval_seed`,
   never writing synthetic rows into `agent_eval`.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import REQUIRED_OTEL_SPANS_VIEW_COLUMNS

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"


def _all_sql_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(SQL_DIR.rglob("*.sql")))


def test_otel_spans_and_trace_payloads_are_views_over_linked_datasets() -> None:
    """`otel_compat_views.sql` defines `otel_spans` and `trace_payloads` as views over `_AllSpans` and `_AllLogs`."""
    ddl = (SQL_DIR / "core" / "ddl.sql").read_text(encoding="utf-8")
    compat = (SQL_DIR / "core" / "otel_compat_views.sql").read_text(encoding="utf-8")

    assert re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.otel_spans`?",
        compat,
        re.IGNORECASE,
    ), "agent_eval.otel_spans must be a CREATE OR REPLACE VIEW in sql/core/otel_compat_views.sql"
    assert not re.search(
        r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.otel_spans`?",
        ddl + "\n" + compat,
        re.IGNORECASE,
    ), "agent_eval.otel_spans must not be a physical CREATE TABLE"
    assert "_AllSpans" in compat, "otel_spans view must read from obs_traces._AllSpans"

    assert re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.trace_payloads`?",
        compat,
        re.IGNORECASE,
    ), (
        "agent_eval.trace_payloads must be a CREATE OR REPLACE VIEW in sql/core/otel_compat_views.sql"
    )
    assert "_AllLogs" in compat, "trace_payloads view must read from obs_logs._AllLogs"
    assert "completions" in compat and "_FILE_NAME" in compat, (
        "trace_payloads view must join _AllLogs with BigLake completions on _FILE_NAME"
    )


def test_otel_spans_compat_view_projects_all_columns() -> None:
    """`agent_eval.v_otel_allspans_only` & `agent_eval.otel_spans` map all required `_AllSpans` columns."""
    compat = (SQL_DIR / "core" / "otel_compat_views.sql").read_text(encoding="utf-8")

    wrapper_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.otel_spans`?\s+AS(.*?);",
        compat,
        re.IGNORECASE | re.DOTALL,
    )
    assert wrapper_match is not None, "Could not locate agent_eval.otel_spans view body"
    assert "v_otel_allspans_only" in wrapper_match.group(1)

    view_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.v_otel_allspans_only`?\s+AS(.*?);",
        compat,
        re.IGNORECASE | re.DOTALL,
    )
    assert view_match is not None, "Could not locate agent_eval.v_otel_allspans_only view body"
    view_body = view_match.group(1)

    for col in REQUIRED_OTEL_SPANS_VIEW_COLUMNS:
        assert re.search(rf"\b{re.escape(col)}\b", view_body), (
            f"agent_eval.otel_spans compatibility view is missing column {col!r}"
        )

    for semconv_source in (
        "duration_nano",
        "gen_ai.operation.name",
        "service.name",
        "apphub.application.id",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "finops.traffic_type",
        "outcome.id",
    ):
        assert semconv_source in view_body, (
            f"otel_spans view must map OTel semconv / _AllSpans field {semconv_source!r}"
        )


def test_outcome_and_coverage_views_defined() -> None:
    """Defines `v_outcomes`, `v_cost_per_verified_outcome`, `v_attribution_coverage`, and `v_otel_spans_coverage`."""
    sql_text = _all_sql_text()

    for view_name in (
        "v_outcomes",
        "v_cost_per_verified_outcome",
        "v_attribution_coverage",
        "v_otel_spans_coverage",
    ):
        assert re.search(
            rf"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.{re.escape(view_name)}`?",
            sql_text,
            re.IGNORECASE,
        ), f"Missing required BigQuery view agent_eval.{view_name}"

    assert "outcome.closed" in sql_text, (
        "v_outcomes must read zero-duration spans where span_name = 'outcome.closed'"
    )


def test_synthetic_seed_and_loadgen_isolated_to_agent_eval_seed() -> None:
    """Synthetic golden seed tables and loadgen target `agent_eval_seed`, not `agent_eval`."""
    loadgen_sql = (SQL_DIR / "demo" / "demo_time_shift_loadgen.sql").read_text(encoding="utf-8")

    assert "agent_eval_seed" in loadgen_sql, (
        "demo_time_shift_loadgen.sql must write synthetic rows to dataset agent_eval_seed"
    )
    assert "agent_eval.seed_otel_spans" not in loadgen_sql, (
        "seed_otel_spans must move out of agent_eval into agent_eval_seed"
    )
    assert "INSERT INTO `agent_eval.otel_spans`" not in loadgen_sql, (
        "Synthetic loadgen must never insert into live agent_eval.otel_spans"
    )
