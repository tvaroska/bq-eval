"""Tests for `bq_eval/runner/` table-function inlining, trace-store loader, and `< 5s` smoke (`M18-3`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.loader import (
    EmptyQueryResultError,
    OfflineSqlRunner,
    smoke_silver_fixtures,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "silver" / "manifest.json"


def test_runner_inlines_table_functions() -> None:
    """Nested table functions, relative windows, fixed-window views, and dataset isolation execute."""
    with OfflineSqlRunner(
        RunnerConfig(
            current_date_utc="2026-11-26",
            current_timestamp_utc="2026-11-26 12:00:00 UTC",
            session_user="gold-ops@showcase-project.iam.gserviceaccount.com",
        )
    ) as runner:
        runner.load_jsonl(
            "obs_traces._AllSpans",
            REPO_ROOT / "tests" / "fixtures" / "silver" / "recorded_allspans.jsonl",
        )
        runner.load_jsonl(
            "platform_traces._AllSpans",
            REPO_ROOT
            / "tests"
            / "fixtures"
            / "silver"
            / "synthetic_platform_gateway_proxy_spans.jsonl",
        )
        runner.register_sql(
            """
            CREATE OR REPLACE TABLE FUNCTION `showcase-project.silver_internal.fn_spans`(
              from_time TIMESTAMP,
              to_time TIMESTAMP
            ) AS (
              SELECT
                s.trace_id,
                s.span_id,
                s.start_time,
                s.kind,
                s.status.code AS status_code,
                s.instrumentation_scope.name AS scope_name,
                JSON_VALUE(s.resource.attributes, '$."service.name"') AS service_name
              FROM `showcase-project.obs_traces._AllSpans` AS s
              WHERE s.start_time >= from_time AND s.start_time < to_time
            );

            CREATE OR REPLACE TABLE FUNCTION `silver_open.fn_service_counts`(
              from_time TIMESTAMP,
              to_time TIMESTAMP
            ) AS (
              SELECT
                service_name,
                COUNT(*) AS span_count,
                SESSION_USER() AS caller_email
              FROM `silver_internal.fn_spans`(from_time, to_time)
              WHERE service_name IS NOT NULL
              GROUP BY service_name
            );

            CREATE OR REPLACE VIEW `silver_open.v_recent_services` AS
            SELECT service_name, span_count
            FROM `silver_open.fn_service_counts`(
              TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY),
              CURRENT_TIMESTAMP()
            );
            """
        )

        direct = runner.query(
            """
            SELECT service_name, span_count, caller_email
            FROM `silver_open.fn_service_counts`(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            )
            ORDER BY service_name
            """
        )
        by_service = {r["service_name"]: r["span_count"] for r in direct.rows}
        assert by_service == {
            "bq-eval-adk-v1": 11,
            "bq-eval-adk-v2": 8,
            "bridge-kyc": 48,
            "bridge-meridian": 5,
            "showcase-project-address-console": 16,
            "showcase-project-fnb": 2,
        }
        assert all(type(v) is int for v in by_service.values())
        assert all(
            r["caller_email"] == "gold-ops@showcase-project.iam.gserviceaccount.com"
            for r in direct.rows
        )

        via_view = runner.query(
            "SELECT service_name, span_count FROM `silver_open.v_recent_services` ORDER BY service_name"
        )
        assert via_view.rows == tuple(
            {"service_name": r["service_name"], "span_count": r["span_count"]} for r in direct.rows
        )

        unquoted_join = runner.query(
            """
            SELECT a.service_name, a.span_count, b.span_count AS peer_count
            FROM silver_open.fn_service_counts(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            ) AS a
            JOIN silver_open.fn_service_counts(
              TIMESTAMP '2026-11-01 00:00:00 UTC',
              TIMESTAMP '2026-12-01 00:00:00 UTC'
            ) AS b
              ON a.service_name = b.service_name
            ORDER BY a.service_name
            """
        )
        assert unquoted_join.rows == tuple(
            {
                "service_name": r["service_name"],
                "span_count": r["span_count"],
                "peer_count": r["span_count"],
            }
            for r in direct.rows
        )

        unaliased = runner.query(
            """
            SELECT
              COUNT(obs_traces._AllSpans.trace_id) AS n,
              COUNT(_AllSpans.span_id) AS implicit_n,
              SUM(CASE WHEN obs_traces._AllSpans.status.code = 1 THEN 1 ELSE 0 END) AS ok_n,
              SAFE_CAST('not-a-ts' AS TIMESTAMP) AS bad_ts
            FROM `obs_traces._AllSpans`
            """
        )
        assert unaliased.rows == ({"n": 95, "implicit_n": 95, "ok_n": 71, "bad_ts": None},)

        runner.register_sql(
            """
            CREATE OR REPLACE TABLE FUNCTION `silver_internal.fn_with_cte`(
              from_time TIMESTAMP,
              to_time TIMESTAMP,
              scale INT64
            ) AS (
              WITH status AS (
                SELECT
                  span_id,
                  start_time AS from_time,
                  status.code AS status_code,
                  scale * 2 AS doubled
                FROM `platform_traces._AllSpans`
                WHERE start_time >= from_time AND start_time < to_time
              )
              SELECT
                COUNT(*) AS n,
                MIN(from_time) AS earliest,
                MAX(doubled) AS max_doubled
              FROM status
            );
            """
        )
        cte_res = runner.query(
            """
            WITH status AS (SELECT 1 AS dummy)
            SELECT
              f.n,
              f.earliest,
              f.max_doubled,
              s.dummy
            FROM silver_internal.fn_with_cte(
              TIMESTAMP '2026-11-25 00:00:00 UTC',
              TIMESTAMP '2026-11-26 00:00:00 UTC',
              1 + 2
            ) AS f
            CROSS JOIN status AS s
            """
        )
        assert cte_res.rows == (
            {
                "n": 5,
                "earliest": "2026-11-25T10:05:00+00:00",
                "max_doubled": 6,
                "dummy": 1,
            },
        )

        temporal = runner.query(
            """
            SELECT
              TIMESTAMP_TRUNC(TIMESTAMP '2026-11-25 10:17:45.123456 UTC', MILLISECOND) AS trunc_ms,
              TIMESTAMP_TRUNC(TIMESTAMP '2026-11-25 10:17:45 UTC', MINUTE) AS trunc_min,
              TIMESTAMP_TRUNC(TIMESTAMP '2026-11-25 10:17:45 UTC', DAY) AS trunc_day,
              TIMESTAMP_TRUNC(TIMESTAMP '2026-11-25 10:17:45 UTC', WEEK) AS trunc_week,
              TIMESTAMP_DIFF(
                TIMESTAMP '2026-11-25 10:02:30 UTC',
                TIMESTAMP '2026-11-25 10:00:00 UTC',
                SECOND
              ) AS diff_sec,
              TIMESTAMP_DIFF(
                TIMESTAMP '2026-11-25 10:02:30 UTC',
                TIMESTAMP '2026-11-25 10:00:00 UTC',
                MINUTE
              ) AS diff_min,
              TIMESTAMP_ADD(TIMESTAMP '2026-11-25 00:00:00 UTC', INTERVAL 1 + 2 HOUR) AS plus_3h,
              TIMESTAMP_ADD(TIMESTAMP '2026-11-25 00:00:00 UTC', INTERVAL 1 WEEK) AS plus_week,
              TIMESTAMP_ADD(TIMESTAMP '2026-11-25 00:00:00 UTC', INTERVAL 500 MICROSECOND) AS plus_us
            """
        )
        assert temporal.rows == (
            {
                "trunc_ms": "2026-11-25T10:17:45.123000+00:00",
                "trunc_min": "2026-11-25T10:17:00+00:00",
                "trunc_day": "2026-11-25T00:00:00+00:00",
                "trunc_week": "2026-11-22T00:00:00+00:00",
                "diff_sec": 150,
                "diff_min": 2,
                "plus_3h": "2026-11-25T03:00:00+00:00",
                "plus_week": "2026-12-02T00:00:00+00:00",
                "plus_us": "2026-11-25T00:00:00.000500+00:00",
            },
        )
        assert type(temporal.rows[0]["diff_sec"]) is int
        with pytest.raises(ValueError, match="Unsupported TIMESTAMP"):
            runner.query(
                "SELECT TIMESTAMP_ADD(TIMESTAMP '2026-11-25 00:00:00 UTC', INTERVAL 1 YEAR)"
            )
        with pytest.raises(ValueError, match="Unsupported TIMESTAMP_TRUNC"):
            runner.query(
                "SELECT TIMESTAMP_TRUNC(start_time, YEAR) FROM `platform_traces._AllSpans` WHERE 1 = 0"
            )

        two_scopes = runner.query(
            """
            WITH a AS (SELECT COUNT(_AllSpans.span_id) AS n FROM `obs_traces._AllSpans`),
                 b AS (SELECT COUNT(_AllSpans.span_id) AS n FROM `platform_traces._AllSpans`)
            SELECT a.n AS obs_n, b.n AS plat_n FROM a CROSS JOIN b
            """
        )
        assert two_scopes.rows == ({"obs_n": 95, "plat_n": 5},)

        runner.register_sql(
            """
            CREATE OR REPLACE TABLE FUNCTION `silver_internal.fn_group_param`(
              tag STRING
            ) AS (
              SELECT tag, COUNT(*) AS n
              FROM `platform_traces._AllSpans`
              GROUP BY tag
            );
            CREATE OR REPLACE TABLE FUNCTION `silver_internal.fn_union_shadow`(
              from_time TIMESTAMP
            ) AS (
              WITH u AS (
                SELECT start_time AS from_time FROM `platform_traces._AllSpans`
                UNION ALL
                SELECT start_time AS from_time FROM `platform_traces._AllSpans`
              )
              SELECT MIN(from_time) AS min_t, COUNT(*) AS n FROM u
            );
            """
        )
        assert runner.query("SELECT tag, n FROM `silver_internal.fn_group_param`('ok')").rows == (
            {"tag": "ok", "n": 5},
        )
        assert runner.query(
            "SELECT min_t, n FROM `silver_internal.fn_union_shadow`(TIMESTAMP '2099-01-01 00:00:00 UTC')"
        ).rows == ({"min_t": "2026-11-25T10:05:00+00:00", "n": 10},)

        platform_only = runner.query(
            """
            SELECT COUNT(*) AS n
            FROM `platform_traces._AllSpans`
            WHERE start_time >= TIMESTAMP '2026-11-25 00:00:00 UTC'
              AND start_time < TIMESTAMP '2026-11-26 00:00:00 UTC'
            """
        )
        assert platform_only.rows == ({"n": 5},)

        with pytest.raises(ValueError, match="expects 2 args"):
            runner.query(
                "SELECT * FROM `silver_internal.fn_spans`(TIMESTAMP '2026-11-25 00:00:00 UTC')"
            )

        runner.register_sql(
            """
            CREATE OR REPLACE TABLE FUNCTION `silver_internal.fn_cycle_a`(x TIMESTAMP) AS (
              SELECT * FROM `silver_internal.fn_cycle_b`(x)
            );
            CREATE OR REPLACE TABLE FUNCTION `silver_internal.fn_cycle_b`(x TIMESTAMP) AS (
              SELECT * FROM `silver_internal.fn_cycle_a`(x)
            );
            """
        )
        with pytest.raises(ValueError, match="Recursive table-function cycle"):
            runner.query(
                "SELECT * FROM `silver_internal.fn_cycle_a`(TIMESTAMP '2026-11-25 00:00:00 UTC')"
            )


def test_runner_loads_trace_store_records_lists_and_manifest() -> None:
    """Loader handles nested records, repeated lists, integer coercions, and multi-file targets."""
    with OfflineSqlRunner(RunnerConfig(current_date_utc="2026-11-26")) as runner:
        loaded = runner.load_manifest(MANIFEST_PATH)
        assert len(loaded) == 17

        span_check = runner.query(
            """
            SELECT
              COUNT(*) AS total_spans,
              SUM(CASE WHEN kind = 2 THEN 1 ELSE 0 END) AS server_spans,
              SUM(CASE WHEN status.code = 1 THEN 1 ELSE 0 END) AS ok_spans,
              SUM(CASE WHEN instrumentation_scope.name IS NULL AND kind IS NULL THEN 1 ELSE 0 END) AS runtime_spans
            FROM `obs_traces._AllSpans`
            """
        )
        row = span_check.rows[0]
        assert row == {
            "total_spans": 100,
            "server_spans": 46,
            "ok_spans": 76,
            "runtime_spans": 5,
        }
        assert all(type(v) is int for v in row.values())

        nested_check = runner.query(
            """
            SELECT
              s.span_id,
              s.apphub.application.id AS apphub_app_id,
              ARRAY_LENGTH(s.events) AS event_count
            FROM `platform_traces._AllSpans` AS s
            WHERE ARRAY_LENGTH(s.events) > 0
            """
        )
        assert len(nested_check.rows) == 1
        assert nested_check.rows[0]["span_id"] == "b100000000000001"
        assert nested_check.rows[0]["apphub_app_id"] is None
        assert nested_check.rows[0]["event_count"] == 1

        registry_check = runner.query(
            """
            SELECT app_id, ARRAY_LENGTH(allowed_landing_places) AS landing_count
            FROM app_registry
            WHERE app_id = 'payments-agent'
            """
        )
        assert registry_check.rows == ({"app_id": "payments-agent", "landing_count": 1},)

        runner.load_rows(
            "obs_traces.coercion_probe",
            [
                {
                    "span_id": "p1",
                    "kind": "2",
                    "created_at": "2026-11-25 09:00:00 UTC",
                    "nullable_num": None,
                    "status": {"code": "1", "message": "ok"},
                    "custom_struct": {"leaf": "hello", "deep": {"x": {"y": 42}}},
                    "attributes": {
                        "flag": True,
                        "code": "007",
                        "raw_ts": "2026-11-25T09:00:00Z",
                        "status": {"code": "007"},
                        "nested": {"service.name": "payments"},
                        "items": [{"label": "first"}],
                        "obj": {"k": "v"},
                    },
                    "raw_scalar": '"hello"',
                    "bad_json": "not-json",
                }
            ],
        )
        runner.load_rows(
            "obs_traces.coercion_probe",
            [{"span_id": "p2", "nullable_num": 7, "created_at": "2026-11-25T10:00:00Z"}],
            append=True,
        )
        coerced = runner.query(
            """
            SELECT
              s.kind,
              s.status.code AS status_code,
              custom_struct.leaf AS custom_leaf,
              s.custom_struct.deep.x.y AS deep_y,
              JSON_VALUE(s.attributes, '$.flag') AS flag_str,
              JSON_VALUE(s.attributes, '$.code') AS attr_code,
              JSON_VALUE(s.attributes, '$.raw_ts') AS attr_raw_ts,
              JSON_VALUE(s.attributes, '$.status.code') AS attr_nested_code,
              JSON_VALUE(s.attributes, '$.nested."service.name"') AS nested_svc,
              JSON_VALUE(s.attributes, '$.items[0].label') AS item_label,
              JSON_VALUE(s.attributes, '$.obj') AS obj_is_null,
              JSON_VALUE(s.raw_scalar, '$') AS root_scalar,
              JSON_VALUE(s.bad_json, '$.x') AS bad_json_val
            FROM `obs_traces.coercion_probe` AS s
            WHERE s.span_id = 'p1'
            """
        )
        assert coerced.rows == (
            {
                "kind": 2,
                "status_code": 1,
                "custom_leaf": "hello",
                "deep_y": 42,
                "flag_str": "true",
                "attr_code": "007",
                "attr_raw_ts": "2026-11-25T09:00:00Z",
                "attr_nested_code": "007",
                "nested_svc": "payments",
                "item_label": "first",
                "obj_is_null": None,
                "root_scalar": "hello",
                "bad_json_val": None,
            },
        )
        assert type(coerced.rows[0]["status_code"]) is int

        appended = runner.query(
            """
            SELECT span_id, nullable_num, created_at
            FROM `obs_traces.coercion_probe`
            ORDER BY created_at
            """
        )
        assert appended.rows == (
            {"span_id": "p1", "nullable_num": None, "created_at": "2026-11-25T09:00:00+00:00"},
            {"span_id": "p2", "nullable_num": 7, "created_at": "2026-11-25T10:00:00+00:00"},
        )
        assert type(appended.rows[1]["nullable_num"]) is int

        runner.load_rows("obs_traces.coercion_probe", [{"span_id": "p3"}], append=False)
        replaced = runner.query("SELECT * FROM `obs_traces.coercion_probe`")
        assert replaced.columns == ("span_id",)
        assert replaced.rows == ({"span_id": "p3"},)

    with OfflineSqlRunner(RunnerConfig(default_dataset="silver_ref")) as scoped_runner:
        scoped_runner.load_rows("app_registry", [{"app_id": "demo-app"}])
        scoped_runner.register_sql(
            "CREATE OR REPLACE TABLE FUNCTION fn_apps() AS (SELECT app_registry.app_id FROM app_registry);"
        )
        assert scoped_runner.query(
            "SELECT silver_ref.app_registry.app_id FROM app_registry"
        ).rows == ({"app_id": "demo-app"},)
        assert scoped_runner.query("SELECT app_id FROM fn_apps()").rows == ({"app_id": "demo-app"},)
        assert scoped_runner.query("SELECT app_id FROM `silver_ref.fn_apps`()").rows == (
            {"app_id": "demo-app"},
        )


def test_runner_fails_when_query_returns_no_rows_for_non_empty_expected(tmp_path: Path) -> None:
    """`assert_query_matches` rejects silent 0-row outputs against non-empty expected files."""
    expected_file = tmp_path / "expected.jsonl"
    expected_file.write_text(json.dumps({"span_id": "b100000000000001"}) + "\n", encoding="utf-8")

    with OfflineSqlRunner() as runner:
        runner.load_jsonl(
            "platform_traces._AllSpans",
            REPO_ROOT
            / "tests"
            / "fixtures"
            / "silver"
            / "synthetic_platform_gateway_proxy_spans.jsonl",
        )
        with pytest.raises(EmptyQueryResultError, match="Query returned 0 rows"):
            runner.assert_query_matches(
                """
                SELECT span_id
                FROM `platform_traces._AllSpans`
                WHERE start_time >= TIMESTAMP '2099-01-01 00:00:00 UTC'
                """,
                expected_file,
            )

        matched = runner.assert_query_matches(
            """
            SELECT span_id
            FROM `platform_traces._AllSpans`
            WHERE span_id = 'b100000000000001'
            """,
            expected_file,
        )
        assert len(matched.rows) == 1

        empty_ok = runner.assert_query_matches(
            "SELECT span_id FROM `platform_traces._AllSpans` WHERE span_id = 'missing'",
            [],
        )
        assert empty_ok.rows == ()

        runner.load_rows(
            "obs_traces.match_probe",
            [
                {
                    "ok": True,
                    "count": 1,
                    "tags": ["a", "b"],
                    "meta": {"b": 2, "a": 1},
                    "ts": "2026-11-25 00:00:00 UTC",
                }
            ],
        )
        runner.assert_query_matches(
            "SELECT ok, count, tags, meta, ts FROM `obs_traces.match_probe`",
            [
                {
                    "ok": True,
                    "count": 1,
                    "tags": ["a", "b"],
                    "meta": '{"a": 1, "b": 2}',
                    "ts": "2026-11-25T00:00:00Z",
                }
            ],
        )
        with pytest.raises(AssertionError, match="row 0"):
            runner.assert_query_matches(
                "SELECT ok FROM `obs_traces.match_probe`",
                [{"ok": False}],
            )


def test_runner_full_offline_fixture_run_under_five_seconds() -> None:
    """Full manifest load + nested TVF execution finishes well under the 5-second ceiling."""
    summary = smoke_silver_fixtures(MANIFEST_PATH, max_seconds=5.0)
    assert summary["fixture_tables"] == 17
    assert summary["span_count"] == 100
    assert float(str(summary["elapsed_sec"])) < 5.0


def test_runner_struct_array_agg_unnest_and_byte_length() -> None:
    """Offline runner supports `STRUCT`, `ARRAY_AGG(STRUCT(...))` self-unnest, and UTF-8 `BYTE_LENGTH`."""
    with OfflineSqlRunner(RunnerConfig(current_date_utc="2026-11-27")) as runner:
        runner.load_rows(
            "obs_traces.span_tree",
            [
                {
                    "trace_id": "t1",
                    "span_id": "s_root",
                    "parent_span_id": None,
                    "agent_name": "planner",
                    "is_model": False,
                    "raw_args": '{"b": 1, "a": 2}',
                },
                {
                    "trace_id": "t1",
                    "span_id": "s_child",
                    "parent_span_id": "s_root",
                    "agent_name": None,
                    "is_model": True,
                    "raw_args": '{"emoji":"😀"}',
                },
            ],
        )
        res = runner.query(
            """
            WITH by_trace AS (
              SELECT
                trace_id,
                ARRAY_AGG(
                  STRUCT(
                    span_id,
                    parent_span_id,
                    agent_name,
                    is_model,
                    raw_args
                  )
                ) AS spans_arr
              FROM `obs_traces.span_tree`
              GROUP BY trace_id
            )
            SELECT
              bt.trace_id,
              s.span_id,
              COALESCE(s.agent_name, p.agent_name) AS resolved_agent,
              p.raw_args AS parent_raw_args,
              BYTE_LENGTH(s.raw_args) AS utf8_bytes,
              CHAR_LENGTH(s.raw_args) AS char_len,
              BYTE_LENGTH(CAST(NULL AS STRING)) AS null_bytes
            FROM by_trace AS bt
            CROSS JOIN UNNEST(bt.spans_arr) AS s
            LEFT JOIN UNNEST(bt.spans_arr) AS p
              ON p.span_id = s.parent_span_id
            WHERE s.is_model IS TRUE
            """
        )
        assert res.rows == (
            {
                "trace_id": "t1",
                "span_id": "s_child",
                "resolved_agent": "planner",
                "parent_raw_args": '{"b": 1, "a": 2}',
                "utf8_bytes": 16,
                "char_len": 13,
                "null_bytes": None,
            },
        )
