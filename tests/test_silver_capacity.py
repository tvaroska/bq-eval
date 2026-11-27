"""Silver `M19-0` capacity and governance objects (`requests`, `burst_metrics_1m`, SCD-2 tables)."""

from __future__ import annotations

import json
from pathlib import Path

from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.loader import OfflineSqlRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
SILVER_SQL_DIR = REPO_ROOT / "sql" / "silver"
SCHEMA_DIR = SILVER_SQL_DIR / "schema"
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "silver" / "manifest.json"


def _expected_columns(schema_name: str) -> tuple[str, ...]:
    raw = json.loads((SCHEMA_DIR / f"{schema_name}.json").read_text(encoding="utf-8"))
    return tuple(str(col["name"]) for col in raw)


def test_requests_rows() -> None:
    """`silver_open.requests` deduplicates 1:N gateway attempts, resolves caller app, and enforces 3P `inferred_ambiguous`."""
    with OfflineSqlRunner(RunnerConfig(current_date_utc="2026-11-26")) as runner:
        runner.load_silver_manifest(MANIFEST_PATH, schema_dir=SCHEMA_DIR)
        runner.register_silver_file(SILVER_SQL_DIR / "capacity" / "requests.sql")

        res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_open.requests`(
              TIMESTAMP '2026-11-25 00:00:00 UTC',
              TIMESTAMP '2026-11-26 00:00:00 UTC'
            )
            ORDER BY client_request_id ASC, attempt_index ASC
            """
        )
        assert res.columns == _expected_columns("requests")
        assert len(res.rows) == 4

        by_key = {(str(r["client_request_id"]), int(str(r["attempt_index"]))): r for r in res.rows}

        # 1:N failover leg 1 (deduped latest receive_timestamp latency_ms=80.0 over 85.0)
        leg1 = by_key[("req-failover-001", 1)]
        assert leg1["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert leg1["span_id"] == "span_gw_call_01"
        assert leg1["response_id"] is None
        assert leg1["trace_owner_app_id"] == "bq-eval-adk-v2"
        assert leg1["serving_tier"] == "PROVISIONED_THROUGHPUT"
        assert leg1["tier_source"] == "server"
        assert leg1["http_status_code"] == 429
        assert leg1["latency_ms"] == 80.0
        assert leg1["state_mode"] == "stateful_cmek_gateway"
        assert leg1["runtime_pool_id"] == "rt-flash-us-c1"
        assert bool(leg1["thought_signature_present"]) is True
        assert bool(leg1["traffic_type_attested"]) is True

        # 1:N failover leg 2 (PRIORITY spillover HTTP 200 with 5m cache write)
        leg2 = by_key[("req-failover-001", 2)]
        assert leg2["response_id"] == "resp_gw_001"
        assert leg2["serving_tier"] == "PRIORITY"
        assert leg2["tier_source"] == "server"
        assert leg2["http_status_code"] == 200
        assert leg2["cache_write_5m_tokens"] == 500
        assert leg2["cache_write_1h_tokens"] == 0
        assert leg2["output_tokens"] == 300
        assert leg2["reasoning_tokens"] == 100

        # Honest 3P shared spillover pool without server TrafficType -> forced inferred_ambiguous
        claude_shared = by_key[("req-3p-claude-001", 1)]
        assert claude_shared["provider"] == "anthropic"
        assert claude_shared["tier_source"] == "inferred_ambiguous"
        assert claude_shared["runtime_pool_id"] == "rt-claude-us-e5"
        assert claude_shared["state_mode"] == "zdr_stateless"
        assert claude_shared["cache_write_1h_tokens"] == 1000

        # Dedicated no-spillover 3P pool + unregistered shadow caller -> inferred + NULL trace_owner_app_id
        claude_ded = by_key[("req-3p-claude-002", 1)]
        assert claude_ded["tier_source"] == "inferred"
        assert claude_ded["trace_owner_app_id"] is None
        assert claude_ded["traffic_type"] is None
        assert claude_ded["traffic_type_attested"] is None


def test_capacity_tables_and_burst_metrics_loaded_columns() -> None:
    """SCD-2 capacity governance tables and `burst_metrics_1m` match their JSON schemas."""
    with OfflineSqlRunner(RunnerConfig(current_date_utc="2026-11-26")) as runner:
        runner.load_silver_manifest(MANIFEST_PATH, schema_dir=SCHEMA_DIR)
        for dataset, table_name in (
            ("silver_ref", "capacity_pools"),
            ("silver_ref", "capacity_allocations"),
            ("silver_ref", "allowed_models"),
            ("silver_open", "burst_metrics_1m"),
        ):
            res = runner.query(f"SELECT * FROM `showcase-project.{dataset}.{table_name}`")  # noqa: S608
            assert res.columns == _expected_columns(table_name)
            assert len(res.rows) >= 2
