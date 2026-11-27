"""Silver M18-10 Shape-3 BigLake resolution, missing-ref flags, and async upload hook tests."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.loader import OfflineSqlRunner
from bq_eval.upload.contract.hook import ReferenceUploadConfig
from bq_eval.upload.engine.hook import (
    AsyncReferenceUploadHook,
    AuditEnqueueError,
    LocalDirectoryStorageWriter,
    build_completion_hook_from_env,
    compute_execution_args_sha256,
    hive_partition_uri,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SILVER_SQL_DIR = REPO_ROOT / "sql" / "silver"
SCHEMA_DIR = SILVER_SQL_DIR / "schema"
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "silver" / "manifest.json"
VECTORS_PATH = REPO_ROOT / "tests" / "fixtures" / "silver" / "adr041_jcs_vectors.json"
ALL_ADMIN_USER = "admin-all@example.invalid"


def _assert_shape3_sql_rows() -> None:
    with OfflineSqlRunner(
        RunnerConfig(
            current_date_utc="2026-11-26",
            current_timestamp_utc="2026-11-26T18:00:00Z",
            session_user=ALL_ADMIN_USER,
        )
    ) as runner:
        runner.load_silver_manifest(MANIFEST_PATH, schema_dir=SCHEMA_DIR)
        runner.load_rows(
            "silver_internal.unit_entitlements",
            [{"principal_email": ALL_ADMIN_USER, "business_unit": "wealth", "tenant_id": "*"}],
            append=True,
        )
        for sql_file in (
            "shape3.sql",
            "jcs.sql",
            "spans.sql",
            "model_calls.sql",
            "tool_calls.sql",
            "events.sql",
            "trace_aux.sql",
            "traces.sql",
            "trace_payloads.sql",
        ):
            runner.register_silver_file(SILVER_SQL_DIR / sql_file)

        payload_res = runner.query(
            """
            SELECT *
            FROM `showcase-project.silver_restricted.trace_payloads`(
              TIMESTAMP '2026-11-25 09:00:00 UTC',
              TIMESTAMP '2026-11-25 11:00:00 UTC'
            )
            ORDER BY trace_id
            """
        )
        by_tid: dict[str, dict[str, Any]] = {str(r["trace_id"]): r for r in payload_res.rows}

        s3_ok = by_tid["aaaa0000000000000000000000000001"]
        assert s3_ok["trace_owner_app_id"] == "wealth-advisor-uc9"
        assert s3_ok["prior_history"] is None and s3_ok["prior_history_read_whole"] is None
        assert "401k" in json.dumps(json.loads(s3_ok["turn_prompt"]))
        assert s3_ok["turn_prompt_read_whole"] == 1
        ctx = json.loads(s3_ok["turn_context"])
        assert len(ctx) == 1 and ctx[0]["tool_name"] == "fetch_client_portfolio"
        assert ctx[0]["result"] == {"bond_pct": 40, "equity_pct": 60}
        assert ctx[0]["returns_pii"] in (True, 1) and s3_ok["turn_context_read_whole"] == 1
        assert "60% equities" in json.dumps(json.loads(s3_ok["turn_output"]))
        assert s3_ok["turn_output_read_whole"] == 1 and s3_ok["step_outputs_read_whole"] == 1
        assert s3_ok["system_instructions"] is None and s3_ok["system_instructions_ref"].endswith(
            "/sys-v1.json"
        )
        assert s3_ok["tool_definitions"] is None and s3_ok["tool_definitions_ref"].endswith(
            "/tools-v1.json"
        )

        s3_miss = by_tid["aaaa0000000000000000000000000002"]
        assert s3_miss["previous_interaction_id"] == "resp-s3-01"
        assert s3_miss["prior_history"] is None and s3_miss["prior_history_read_whole"] is None
        assert "rebalance" in json.dumps(json.loads(s3_miss["turn_prompt"]))
        assert s3_miss["turn_prompt_read_whole"] == 1
        assert s3_miss["turn_output"] is None and s3_miss["turn_output_read_whole"] == 0
        assert s3_miss["step_outputs"] is None and s3_miss["step_outputs_read_whole"] == 0

        runner.load_rows(
            "silver_restricted.shape3_payloads",
            [
                {
                    "dt": "2026-11-20",
                    "hr": "10",
                    "ref_uri": "gs://showcase-project-payloads/dt=2026-11-25/hr=10/tr-s3-01-out.json",
                    "payload": [
                        {"role": "assistant", "parts": [{"type": "text", "content": "old"}]}
                    ],
                }
            ],
            append=False,
        )
        pruned_res = runner.query(
            """
            SELECT trace_id, turn_output, turn_output_read_whole
            FROM `showcase-project.silver_restricted.trace_payloads`(
              TIMESTAMP '2026-11-25 09:00:00 UTC',
              TIMESTAMP '2026-11-25 11:00:00 UTC'
            )
            WHERE trace_id = 'aaaa0000000000000000000000000001'
            """
        )
        assert pruned_res.rows[0]["turn_output"] is None
        assert pruned_res.rows[0]["turn_output_read_whole"] == 0

        # Also verify the scrubbed live-recorded Shape-3 trace (`a4b966744ba3bbfecdf4fa4ac7349708`).
        rec_dir = REPO_ROOT / "tests" / "fixtures" / "silver" / "recorded_shape3"
        runner.load_jsonl("obs_traces._AllSpans", rec_dir / "spans.jsonl", append=True)
        runner.load_jsonl(
            "silver_restricted.shape3_payloads", rec_dir / "payloads.jsonl", append=True
        )
        live_tp = runner.query(
            """
            SELECT trace_id, trace_owner_app_id, turn_prompt, turn_context, turn_output, turn_output_read_whole
            FROM `showcase-project.silver_restricted.trace_payloads`(
              TIMESTAMP '2026-11-26 19:00:00 UTC',
              TIMESTAMP '2026-11-26 20:00:00 UTC'
            )
            WHERE trace_id = 'a4b966744ba3bbfecdf4fa4ac7349708'
            """
        )
        assert len(live_tp.rows) == 1
        assert live_tp.rows[0]["trace_owner_app_id"] == "wealth-advisor-uc9"
        assert "quarterly portfolio" in str(live_tp.rows[0]["turn_prompt"])
        assert "65% equities" in str(live_tp.rows[0]["turn_output"])
        assert json.loads(str(live_tp.rows[0]["turn_context"]))[0]["result"]["equity_pct"] == 65
        assert live_tp.rows[0]["turn_output_read_whole"] == 1


def _assert_shape3_upload_hook_and_jcs(tmp_path: Path) -> None:
    base_path = "gs://showcase-project-payloads/shape3"
    ts = datetime(2026, 11, 26, 14, 30, tzinfo=UTC)
    dt, hr, uri = hive_partition_uri(base_path, ts, "msg-01", upload_format="jsonl")
    assert (dt, hr, uri) == ("2026-11-26", "14", f"{base_path}/dt=2026-11-26/hr=14/msg-01.jsonl")

    writer = LocalDirectoryStorageWriter(tmp_path / "lake", base_path=base_path)
    hook = build_completion_hook_from_env(
        writer,
        env={
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH": base_path,
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT": "jsonl",
        },
        max_queue_size=8,
    )
    enqueued_uri = hook.enqueue(
        "msg-01",
        [{"role": "user", "parts": [{"type": "text", "content": "hello"}]}],
        timestamp_utc=ts,
    )
    assert enqueued_uri == uri
    final_metrics = hook.close()
    assert final_metrics.enqueued == 1 and final_metrics.written == 1
    assert final_metrics.dropped_queue_full == 0 and final_metrics.write_errors == 0
    written_file = tmp_path / "lake" / "dt=2026-11-26" / "hr=14" / "msg-01.jsonl"
    assert written_file.is_file()
    parsed_row = json.loads(written_file.read_text(encoding="utf-8").strip())
    assert parsed_row["ref_uri"] == uri and parsed_row["payload"][0]["role"] == "user"

    # Exclusive create prevents silent WORM overwrite, and invalid env/parquet formats fail closed.
    with pytest.raises(FileExistsError):
        writer.write_record(uri, '{"ref_uri":"dup","payload":[]}')
    with pytest.raises(ValueError, match="Unsupported OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT"):
        build_completion_hook_from_env(
            writer,
            env={
                "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH": base_path,
                "OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT": "avro",
            },
        )
    with pytest.raises(ValueError, match="Parquet-capable"):
        AsyncReferenceUploadHook(
            ReferenceUploadConfig(base_path=base_path, upload_format="parquet"),
            writer,
        )
    with pytest.raises(AuditEnqueueError, match="unavailable"):
        hook.enqueue("after-close", {"x": 1}, timestamp_utc=ts, payload_profile="regulated_worm")

    # Bounded queue drop counter (`ephemeral_sample`), fail-closed (`regulated_worm`), and deadlock-free close.
    started = threading.Event()
    block = threading.Event()

    class _SlowParquetWriter:
        supports_parquet = True

        def write_record(self, ref_uri: str, serialized_line: str) -> None:
            started.set()
            block.wait(timeout=2.0)

    bounded_cfg = ReferenceUploadConfig(
        base_path=base_path,
        upload_format="parquet",
        max_queue_size=1,
        payload_profile="ephemeral_sample",
    )
    slow_hook = AsyncReferenceUploadHook(bounded_cfg, _SlowParquetWriter(), start_worker=True)
    slow_hook.enqueue("rec-0", {"a": 0}, timestamp_utc=ts)
    assert started.wait(timeout=2.0)
    slow_hook.enqueue("rec-1", {"a": 1}, timestamp_utc=ts)
    dropped_uri = slow_hook.enqueue("rec-2", {"a": 2}, timestamp_utc=ts)
    assert dropped_uri.endswith("rec-2.parquet") and slow_hook.metrics().dropped_queue_full == 1

    with pytest.raises(AuditEnqueueError, match="regulated_worm"):
        slow_hook.enqueue("rec-3", {"a": 3}, timestamp_utc=ts, payload_profile="regulated_worm")
    assert slow_hook.metrics().fail_closed_rejections == 1
    assert slow_hook.flush(timeout_sec=0.05) is False
    slow_hook.close(timeout_sec=0.05)
    block.set()


def test_shape3_rows(tmp_path: Path) -> None:
    """Shape-3 BigLake dereferencing, missing-ref `*_read_whole=false`, tool fallback, and upload hook."""
    _assert_shape3_sql_rows()
    _assert_shape3_upload_hook_and_jcs(tmp_path)

    # Execution-time `bq_eval.args_sha256` matches ADR-041 JCS vectors and guards oversize/surrogates.
    vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]
    checked_vectors = 0
    for vec in vectors:
        inp = json.loads(vec["input_json"])
        if isinstance(inp, dict) and set(inp.keys()) == {"tool_name", "args"}:
            digest = compute_execution_args_sha256(str(inp["tool_name"]), inp["args"])
            assert digest == vec["sha256"], f"Vector {vec['id']} mismatch"
            checked_vectors += 1
    assert checked_vectors == len(vectors)
    assert compute_execution_args_sha256("big", {"s": "a" * 66_000}) is None
    assert compute_execution_args_sha256("surr", r'{"a":"\ud800"}') is None
