"""`bq-eval bootstrap`, dual-schema `completions`, Terraform/Docker/Trivy & plumbing."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq_eval.cli.app import main as cli_main
from bq_eval.core.contract.otel import OTelGenAISpanRecord
from bq_eval.core.contract.results import ConfusionMatrix
from bq_eval.core.engine.sql_eval import evaluate_traces_in_sqlite
from bq_eval.deploy.engine.bootstrap import (
    normalize_payload_prefix,
    substitute_sql_parameters,
    validate_bootstrap_offline,
)
from bq_eval.labs.engine.empirical_proof import (
    BlindHumanLabelRow,
    analyze_unconfounded_context_cliff_arms,
    build_bq_query_cost_row,
    build_information_schema_jobs_sync_sql,
    compute_bq_query_cost_usd,
    compute_escalation_wage_sensitivity,
    sum_bq_query_costs_by_app,
    validate_blind_dual_human_labels,
)
from tests.conftest import make_span
from tests.test_sql_contracts import (
    ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
    REPO_ROOT,
    SQL_DIR,
    _create_enterprise_sqlite_fixture,
    _extract_view_sql_text,
    _install_enterprise_views,
    _new_sqlite_bq_conn,
    _parse_bigquery_statements,
    _transpile_bq_view_to_sqlite,
    insert_enterprise_sqlite_span,
)


def test_bootstrap_offline_schema_all_modes_and_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bq-eval bootstrap --mode light|full|hybrid --offline-schema` renders clean SQL with zero placeholders."""
    for mode in ("light", "full", "hybrid"):
        report, stmts = validate_bootstrap_offline(
            mode=mode,
            project_id="cold-proj-01",
            dataset_id="agent_eval",
            connection_id="us.vertex_ai_connection",
            payload_bucket="cold-proj-01",
            payload_prefix="bq-eval",
            include_omni=True,
        )
        assert report.passed, f"{mode} validation errors: {report.errors}"
        assert report.unresolved_placeholders == ()
        assert report.schema_column_count == 39
        assert report.statement_count == len(stmts)
        assert report.payload_uri_pattern == "gs://cold-proj-01/bq-eval/*.jsonl"

        joined = "\n;\n".join(stmt for _, stmt in stmts)
        assert "YOUR_GENAI_PAYLOAD_BUCKET" not in joined
        assert "${" not in joined
        if mode == "light":
            assert "_AllSpans" not in joined
            assert "_AllLogs" not in joined
            assert "v_mode_a_otel_spans" in joined
        elif mode == "full":
            assert "v_otel_allspans_only" in joined
            assert "gs://cold-proj-01/bq-eval/*.jsonl" in joined
        else:
            assert "v_mode_c_hybrid_otel_spans" in joined
            assert "v_mode_c_hybrid_trace_payloads" in joined

    sql_out = tmp_path / "rendered_hybrid.sql"
    rc = cli_main(
        [
            "bootstrap",
            "--mode",
            "hybrid",
            "--project-id",
            "cold-proj-01",
            "--payload-bucket",
            "cold-proj-01",
            "--payload-prefix",
            "bq-eval/",
            "--offline-schema",
            "--include-omni",
            "--sql-out",
            str(sql_out),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["mode"] == "hybrid"
    assert payload["schema_column_count"] == 39
    assert sql_out.is_file()
    rendered_text = sql_out.read_text(encoding="utf-8")
    assert "gs://cold-proj-01/bq-eval/*.jsonl" in rendered_text
    assert "${" not in rendered_text

    assert normalize_payload_prefix("/bq-eval") == "bq-eval/"
    assert normalize_payload_prefix("") == ""
    with pytest.raises(ValueError, match="Invalid"):
        substitute_sql_parameters("SELECT 1", project_id="bad; DROP TABLE x")
    with pytest.raises(ValueError, match="Invalid payload_prefix"):
        normalize_payload_prefix("../escape")


def test_dual_schema_completions_top_level_and_nested_json_payloads() -> None:
    """`v_otel_logs_payloads_only` reads both top-level `completions` columns and nested `payload` JSON."""
    ddl_path = SQL_DIR / "core" / "ddl.sql"
    logs_view_sql = _transpile_bq_view_to_sqlite(
        _extract_view_sql_text(ddl_path, "agent_eval.v_otel_logs_payloads_only")
        .replace("`obs_logs._AllLogs`", "`_AllLogs`")
        .replace("l.resource.labels", "l.resource_labels")
    )
    conn = _new_sqlite_bq_conn()
    conn.executescript(
        """
        CREATE TABLE _AllLogs (
            trace TEXT,
            timestamp TEXT,
            resource_labels TEXT,
            json_payload TEXT
        );
        CREATE TABLE completions (
            _FILE_NAME TEXT,
            payload TEXT,
            user_prompt TEXT,
            retrieved_context TEXT,
            final_response TEXT
        );
        """
    )
    conn.execute(f"CREATE VIEW v_otel_logs_payloads_only AS {logs_view_sql}")
    conn.executemany(
        "INSERT INTO _AllLogs VALUES (?,?,?,?)",
        [
            (
                "projects/p/traces/aaaa1111aaaa1111",
                "2026-11-22T10:00:00+00:00",
                json.dumps({"apphub.application.id": "app-top"}),
                json.dumps({"gen_ai.completion.uri": "gs://bucket/bq-eval/top.jsonl"}),
            ),
            (
                "projects/p/traces/bbbb2222bbbb2222",
                "2026-11-22T10:01:00+00:00",
                json.dumps({"apphub.application.id": "app-nested"}),
                json.dumps({"gen_ai.completion.uri": "gs://bucket/bq-eval/nested.jsonl"}),
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO completions VALUES (?,?,?,?,?)",
        [
            (
                "gs://bucket/bq-eval/top.jsonl",
                None,
                "Top-level prompt",
                "Top-level context",
                "Top-level answer",
            ),
            (
                "gs://bucket/bq-eval/nested.jsonl",
                json.dumps(
                    {
                        "user_prompt": "Nested JSON prompt",
                        "retrieved_context": "Nested JSON context",
                        "final_response": "Nested JSON answer",
                    }
                ),
                None,
                None,
                None,
            ),
        ],
    )
    rows = {
        str(r["trace_id"]): dict(r)
        for r in conn.execute("SELECT * FROM v_otel_logs_payloads_only").fetchall()
    }
    conn.close()

    assert rows["aaaa1111aaaa1111"]["user_prompt"] == "Top-level prompt"
    assert rows["aaaa1111aaaa1111"]["retrieved_context"] == "Top-level context"
    assert rows["aaaa1111aaaa1111"]["final_response"] == "Top-level answer"
    assert rows["bbbb2222bbbb2222"]["user_prompt"] == "Nested JSON prompt"
    assert rows["bbbb2222bbbb2222"]["retrieved_context"] == "Nested JSON context"
    assert rows["bbbb2222bbbb2222"]["final_response"] == "Nested JSON answer"


def test_terraform_dockerfile_and_cloudbuild_security_contracts() -> None:
    """Verify Terraform standalone/co-located layout, non-root Dockerfile & Trivy Cloud Build gate."""
    tf_dir = REPO_ROOT / "terraform"
    for rel in (
        "versions.tf",
        "variables.tf",
        "main.tf",
        "outputs.tf",
        "tests/standalone.tftest.hcl",
        "tests/colocated.tftest.hcl",
    ):
        assert (tf_dir / rel).is_file(), f"Missing terraform/{rel}"

    tf_main = (tf_dir / "main.tf").read_text(encoding="utf-8")
    tf_vars = (tf_dir / "variables.tf").read_text(encoding="utf-8")
    assert "google_storage_bucket" in tf_main
    assert "google_bigquery_connection" in tf_main
    assert "google_bigquery_data_transfer_config" in tf_main
    assert "disabled               = local.scheduled_judge_disabled" in tf_main
    assert 'default     = "hibernate"' in tf_vars
    assert 'default     = "bq-eval/"' in tf_vars
    for flag in ("create_shared_bucket", "create_eval_dataset", "create_vertex_connection"):
        assert f'variable "{flag}"' in tf_vars

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ghcr.io/astral-sh/uv:0.12.24" in dockerfile
    assert "USER 10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/healthz" in dockerfile
    assert "--allow-non-loopback" in dockerfile

    cb_text = (REPO_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "bootstrap --mode light --offline-schema" in cb_text
    assert "bootstrap --mode full --offline-schema" in cb_text
    assert "bootstrap --mode hybrid --offline-schema" in cb_text
    assert "hashicorp/terraform:" in cb_text
    assert "aquasec/trivy:" in cb_text
    assert "--pkg-types" in cb_text
    assert "os,library" in cb_text
    assert re.search(r"--exit-code\s+1|--exit-code\",\s*\"1\"", cb_text)


def test_bq_query_cost_folded_into_cpvo_sql_sqlite_parity() -> None:
    """`INFORMATION_SCHEMA.JOBS` query cost folds into `total_spend_usd` and CPVO across BigQuery SQL & SQLite."""
    sync_sql = build_information_schema_jobs_sync_sql(
        project_id="demo-proj",
        dataset_id="agent_eval",
        region="us",
        lookback_days=7,
    )
    _parse_bigquery_statements(sync_sql, "information_schema_jobs_sync_sql")
    assert "INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sync_sql

    on_demand_row = build_bq_query_cost_row(
        job_id="job-od-1",
        apphub_application_id="query-cost-app",
        outcome_id="out-qc-1",
        total_bytes_billed=1024**4,
        pricing_mode="on_demand",
    )
    slot_row = build_bq_query_cost_row(
        job_id="job-slot-1",
        apphub_application_id="query-cost-app",
        outcome_id="out-qc-1",
        total_slot_ms=3_600_000,
        pricing_mode="capacity_slot",
    )
    assert on_demand_row.query_cost_usd == pytest.approx(6.25, abs=1e-12)
    assert slot_row.query_cost_usd == pytest.approx(0.04, abs=1e-12)
    assert compute_bq_query_cost_usd(total_bytes_billed=0, total_slot_ms=0) == 0.0
    cost_by_app = sum_bq_query_costs_by_app([on_demand_row, slot_row])
    assert cost_by_app["query-cost-app"] == pytest.approx(6.29, abs=1e-12)

    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    root_span = make_span(
        "trace-qc-01",
        "sp-qc-01",
        app_id="query-cost-app",
        outcome_id="out-qc-1",
        outcome_owner="query-cost-app",
        input_tokens=10_000,
        cached_input_tokens=0,
        output_tokens=1_000,
        thinking_tokens=0,
        latency_ms=1000.0,
        base_time=base,
    )
    close_span = make_span(
        "trace-qc-01",
        "sp-qc-close",
        parent_span_id="sp-qc-01",
        span_name="outcome.closed",
        app_id="query-cost-app",
        outcome_id="out-qc-1",
        outcome_owner="query-cost-app",
        model_name="none",
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        latency_ms=0.0,
        offset_s=1,
        attributes={"outcome.status": "succeeded", "outcome.verified": "true"},
        base_time=base,
    )

    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    insert_enterprise_sqlite_span(conn, root_span)
    insert_enterprise_sqlite_span(conn, close_span)
    conn.execute(
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        (
            "ev-qc-1",
            "run-qc",
            "trace-qc-01",
            "out-qc-1",
            "query-cost-app",
            "groundedness_4pt_v1",
            "gemini-3.8-flash",
            "CANARY_5PCT",
            1,
            "ok",
            4,
            1,
            1_000,
            100,
            0,
            (base + timedelta(seconds=5)).isoformat(),
        ),
    )
    sme_rows = (
        [("query-cost-app", "groundedness_4pt_v1", 1, 4, 1, 4, 1)] * 87
        + [("query-cost-app", "groundedness_4pt_v1", 1, 4, 1, 2, 0)] * 2
        + [("query-cost-app", "groundedness_4pt_v1", 1, 1, 0, 1, 0)] * 7
        + [("query-cost-app", "groundedness_4pt_v1", 1, 1, 0, 4, 1)] * 4
    )
    conn.executemany("INSERT INTO sme_labels VALUES (?,?,?,?,?,?,?)", sme_rows)
    conn.executemany(
        "INSERT INTO bq_query_jobs VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                on_demand_row.job_id,
                on_demand_row.apphub_application_id,
                on_demand_row.outcome_id,
                on_demand_row.total_bytes_billed,
                on_demand_row.total_slot_ms,
                on_demand_row.pricing_mode,
                on_demand_row.query_cost_usd,
                base.isoformat(),
            ),
            (
                slot_row.job_id,
                slot_row.apphub_application_id,
                slot_row.outcome_id,
                slot_row.total_bytes_billed,
                slot_row.total_slot_ms,
                slot_row.pricing_mode,
                slot_row.query_cost_usd,
                base.isoformat(),
            ),
        ],
    )

    bq_sum = conn.execute(
        "SELECT bq_query_cost_usd, total_spend_usd, cost_per_verified_outcome_usd "
        "FROM v_agent_eval_and_finops_summary WHERE apphub_application_id = 'query-cost-app'"
    ).fetchone()
    bq_cpvo = conn.execute(
        "SELECT bq_query_cost_usd, cost_per_verified_outcome_usd "
        "FROM v_enterprise_cost_per_verified_outcome WHERE apphub_application_id = 'query-cost-app'"
    ).fetchone()
    conn.close()

    twin = evaluate_traces_in_sqlite(
        spans=[root_span, close_span],
        judge_outcomes={"trace-qc-01": True},
        sme_confusion=ConfusionMatrix(tp=87, fn=2, tn=7, fp=4),
        bq_query_jobs=[on_demand_row, slot_row],
    )["query-cost-app"]

    assert float(bq_sum["bq_query_cost_usd"]) == pytest.approx(6.29, abs=1e-12)
    assert float(bq_cpvo["bq_query_cost_usd"]) == pytest.approx(6.29, abs=1e-12)
    assert twin.bq_query_cost_usd == pytest.approx(6.29, abs=1e-12)
    assert float(bq_cpvo["cost_per_verified_outcome_usd"]) > 6.29
    assert twin.cost_per_verified_outcome_usd is not None
    assert twin.cost_per_verified_outcome_usd > 6.29


def test_dual_human_validation_unconfounded_cliff_and_wage_sensitivity() -> None:
    """Validators reject synthetic raters/confounded arms and compute wage sensitivity cleanly."""
    valid_rows: list[BlindHumanLabelRow] = []
    for i in range(100):
        human_score = 4 if i < 82 else 2
        judge_score = 4 if i < 80 else (4 if i == 99 else 2)
        valid_rows.append(
            BlindHumanLabelRow(
                trace_id=f"tr-sme-{i:03d}",
                rater_id="sme_alice",
                human_ordinal_score=human_score,
                judge_ordinal_score=judge_score,
                blind_to_judge=True,
            )
        )
        valid_rows.append(
            BlindHumanLabelRow(
                trace_id=f"tr-sme-{i:03d}",
                rater_id="sme_bob",
                human_ordinal_score=human_score,
                judge_ordinal_score=judge_score,
                blind_to_judge=True,
            )
        )

    panel = validate_blind_dual_human_labels(valid_rows)
    assert panel.trace_count == 100
    assert panel.distinct_raters == 2
    assert panel.human_vs_human_ac2 == pytest.approx(1.0, abs=1e-12)
    assert panel.human_vs_llm_ac2 > 0.90
    assert panel.confusion_matrix.total == 100

    with pytest.raises(ValueError, match="non-human/synthetic"):
        validate_blind_dual_human_labels(
            [BlindHumanLabelRow("t1", "gemini-4-argon", 4, 4, blind_to_judge=True)],
            min_cases=1,
        )
    with pytest.raises(ValueError, match="not blind"):
        validate_blind_dual_human_labels(
            [BlindHumanLabelRow("t1", "sme_alice", 4, 4, blind_to_judge=False)],
            min_cases=1,
        )
    with pytest.raises(ValueError, match="required >= 100"):
        validate_blind_dual_human_labels(valid_rows[:20])

    base = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)
    short_arm: list[OTelGenAISpanRecord] = [
        make_span(
            f"tr-short-{i}",
            f"sp-short-{i}",
            model_name="gemini-4-argon",
            input_tokens=150_000,
            cached_input_tokens=0,
            output_tokens=2_000,
            thinking_tokens=0,
            base_time=base,
        )
        for i in range(30)
    ]
    long_arm: list[OTelGenAISpanRecord] = [
        make_span(
            f"tr-long-{i}",
            f"sp-long-{i}",
            model_name="gemini-4-argon",
            input_tokens=250_000,
            cached_input_tokens=0,
            output_tokens=2_000,
            thinking_tokens=0,
            base_time=base,
        )
        for i in range(30)
    ]
    cliff = analyze_unconfounded_context_cliff_arms(short_arm, long_arm)
    assert cliff.model_name == "gemini-4-argon"
    assert cliff.cost_multiplier > 3.0
    assert cliff.burndown_multiplier > 3.0

    mixed_long = [long_arm[0].model_copy(update={"model_name": "gemini-3.8-flash"}), *long_arm[1:]]
    with pytest.raises(ValueError, match="Confounded arms"):
        analyze_unconfounded_context_cliff_arms(short_arm, mixed_long)

    curve = compute_escalation_wage_sensitivity(
        verified_outcomes=100,
        agent_model_usd=10.0,
        judge_usd=1.0,
        bq_query_usd=0.5,
        escalated_cases=10,
        avg_review_minutes_per_escalation=12.0,
        hourly_wages_usd=(50.0, 150.0, 250.0),
    )
    assert len(curve) == 3
    assert curve[0].human_escalation_cost_per_verified_outcome_usd == pytest.approx(1.0, abs=1e-12)
    assert curve[2].fully_loaded_cpvo_usd == pytest.approx(0.10 + 0.01 + 0.005 + 5.0, abs=1e-12)
