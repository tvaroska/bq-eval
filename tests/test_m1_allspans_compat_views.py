"""M1 compatibility-view contract tests (authored in M0; red/xfailed until M1 lands).

Asserts the Step-5 and Step-8 BigQuery SQL contract for native Telemetry API +
Observability Analytics linked datasets (`obs_traces._AllSpans` and
`obs_logs._AllLogs` joined with the `completions` BigLake external table):

1. `agent_eval.otel_spans` and `agent_eval.trace_payloads` are defined in
   `sql/ddl.sql` as `CREATE OR REPLACE VIEW` over the linked datasets rather than
   custom-ingested physical tables.
2. `agent_eval.otel_spans` projects all 25 mapped columns required by Step-1/Step-2
   views plus M1 outcome attribution (`span_name` <- `name`, `outcome_id` <-
   `outcome.id` baggage, `rule0_pass`, `finops_traffic_type`, `thinking_tokens`, `gcp_apphub_*`).
3. Outcome and attribution views (`v_outcomes` reading `span_name = 'outcome.closed'`,
   `v_cost_per_verified_outcome`, `v_attribution_coverage`, and
   `v_otel_spans_coverage`) are defined in `sql/`.
4. Synthetic seed tables (`seed_otel_spans`, `seed_trace_payloads`) and
   `sql/demo_time_shift_loadgen.sql` are isolated to dataset `agent_eval_seed`,
   never writing synthetic rows into `agent_eval`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "sql"

M1_XFAIL = pytest.mark.xfail(
    strict=True,
    reason=(
        "M1 pending: agent_eval.otel_spans / trace_payloads _AllSpans compatibility "
        "views, outcome attribution views, and agent_eval_seed isolation land in M1"
    ),
)

REQUIRED_OTEL_SPANS_VIEW_COLUMNS: tuple[str, ...] = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "span_name",
    "start_time",
    "end_time",
    "latency_ms",
    "status_code",
    "span_kind",
    "service_name",
    "gcp_apphub_application_id",
    "gcp_apphub_service_id",
    "gcp_apphub_workload_id",
    "agent_name",
    "model_name",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "thinking_tokens",
    "tool_name",
    "tool_call_status",
    "rule0_pass",
    "finops_traffic_type",
    "outcome_id",
    "attributes",
)


def _all_sql_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(SQL_DIR.glob("*.sql"))
    )


@M1_XFAIL
def test_otel_spans_and_trace_payloads_are_views_over_linked_datasets() -> None:
    """`ddl.sql` defines `otel_spans` and `trace_payloads` as views over `_AllSpans` and `_AllLogs`."""
    ddl = (SQL_DIR / "ddl.sql").read_text(encoding="utf-8")

    assert re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.otel_spans`?",
        ddl,
        re.IGNORECASE,
    ), "agent_eval.otel_spans must be a CREATE OR REPLACE VIEW in sql/ddl.sql"
    assert not re.search(
        r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.otel_spans`?",
        ddl,
        re.IGNORECASE,
    ), "agent_eval.otel_spans must not be a physical CREATE TABLE in sql/ddl.sql"
    assert "_AllSpans" in ddl, "otel_spans view must read from obs_traces._AllSpans"

    assert re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.trace_payloads`?",
        ddl,
        re.IGNORECASE,
    ), "agent_eval.trace_payloads must be a CREATE OR REPLACE VIEW in sql/ddl.sql"
    assert "_AllLogs" in ddl, "trace_payloads view must read from obs_logs._AllLogs"
    assert "completions" in ddl and "_FILE_NAME" in ddl, (
        "trace_payloads view must join _AllLogs with BigLake completions on _FILE_NAME"
    )


@M1_XFAIL
def test_otel_spans_compat_view_projects_all_m1_columns() -> None:
    """`agent_eval.otel_spans` view maps all required `_AllSpans` columns and semconv keys."""
    ddl = (SQL_DIR / "ddl.sql").read_text(encoding="utf-8")

    view_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+`?(?:[a-zA-Z0-9_.-]+\.)?agent_eval\.otel_spans`?\s+AS(.*?);",
        ddl,
        re.IGNORECASE | re.DOTALL,
    )
    assert view_match is not None, "Could not locate agent_eval.otel_spans view body"
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


@M1_XFAIL
def test_m1_outcome_and_coverage_views_defined() -> None:
    """M1 defines `v_outcomes`, `v_cost_per_verified_outcome`, `v_attribution_coverage`, and `v_otel_spans_coverage`."""
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
        ), f"Missing required M1 BigQuery view agent_eval.{view_name}"

    assert "outcome.closed" in sql_text, (
        "v_outcomes must read zero-duration spans where span_name = 'outcome.closed'"
    )


@M1_XFAIL
def test_synthetic_seed_and_loadgen_isolated_to_agent_eval_seed() -> None:
    """Synthetic golden seed tables and loadgen target `agent_eval_seed`, not `agent_eval`."""
    loadgen_sql = (SQL_DIR / "demo_time_shift_loadgen.sql").read_text(encoding="utf-8")

    assert "agent_eval_seed" in loadgen_sql, (
        "demo_time_shift_loadgen.sql must write synthetic rows to dataset agent_eval_seed"
    )
    assert "agent_eval.seed_otel_spans" not in loadgen_sql, (
        "seed_otel_spans must move out of agent_eval into agent_eval_seed"
    )
    assert "INSERT INTO `agent_eval.otel_spans`" not in loadgen_sql, (
        "Synthetic loadgen must never insert into live agent_eval.otel_spans"
    )
