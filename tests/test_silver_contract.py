"""Silver core BigQuery column schemas and `labels` registry contract tests (`ADR-045`)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "sql" / "silver" / "schema"

EXPECTED_SCHEMA_OBJECTS: tuple[str, ...] = (
    "spans",
    "model_calls",
    "tool_calls",
    "tool_calls_open",
    "events",
    "events_open",
    "trace_payloads",
    "traces",
    "traces_open",
    "outcomes",
    "outcomes_open",
    "outcome_links",
    "requests",
)

OPEN_OBJECTS: frozenset[str] = frozenset(
    {
        "model_calls",
        "tool_calls_open",
        "events_open",
        "traces_open",
        "outcomes_open",
        "outcome_links",
        "requests",
    }
)

RESTRICTED_TWIN_PAIRS: tuple[tuple[str, str], ...] = (
    ("tool_calls", "tool_calls_open"),
    ("events", "events_open"),
    ("traces", "traces_open"),
    ("outcomes", "outcomes_open"),
)

EXPECTED_TABLE_SCHEMAS: tuple[str, ...] = (
    "app_registry",
    "tools",
    "skill_registry",
    "unit_entitlements",
    "workflow_events",
    "burst_metrics_1m",
    "capacity_pools",
    "capacity_allocations",
    "allowed_models",
)

ALLOWED_BQ_SCALAR_OR_JSON_TYPES: frozenset[str] = frozenset(
    {"STRING", "INT64", "FLOAT64", "BOOL", "TIMESTAMP", "DATE", "JSON"}
)

FORBIDDEN_IN_OPEN_SCHEMAS: frozenset[str] = frozenset(
    {
        "user_id",
        "platform_user_id",
        "tenant_id",
        "actor_id",
        "act_chain",
        "arguments",
        "results",
        "action_args",
        "payload_json",
        "prior_history",
        "turn_prompt",
        "turn_context",
        "turn_output",
        "step_outputs",
        "app_failure_reason",
        "outcome_subject",
        "idempotency_key",
        "memory_id",
        "args_sha256",
        "approved_args_sha256",
        "committed_args_sha256",
        "attachment_sha256",
        "corpus_digest",
        "memory_sha256",
        "memory_source_provenance",
        "devai_diff_stats",
        "devai_test_summary",
        "devai_sast_findings",
    }
)

SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _load_schema(name: str) -> list[dict[str, str]]:
    path = SCHEMA_DIR / f"{name}.json"
    assert path.is_file(), f"Missing Silver schema file: {path.relative_to(REPO_ROOT)}"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, list) and raw, f"{name}.json must be a non-empty JSON list"
    return raw


def _col_map(schema: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {col["name"]: col for col in schema}


def test_silver_schema_files_parse_and_avoid_array_types() -> None:
    """Every core Silver schema file parses cleanly and avoids BigQuery ARRAY types (`ADR-045`)."""
    all_expected = set(EXPECTED_SCHEMA_OBJECTS) | set(EXPECTED_TABLE_SCHEMAS)
    actual_json = {p.stem for p in SCHEMA_DIR.glob("*.json") if p.name != "labels_registry.json"}
    assert actual_json == all_expected

    for obj_name in (*EXPECTED_SCHEMA_OBJECTS, *EXPECTED_TABLE_SCHEMAS):
        cols = _load_schema(obj_name)
        seen: set[str] = set()
        for col in cols:
            assert set(col.keys()) == {"name", "type", "mode", "description"}
            col_name = col["name"]
            assert SNAKE_CASE_RE.match(col_name), f"{obj_name}.{col_name} must be snake_case"
            assert col_name not in seen, f"Duplicate column {obj_name}.{col_name}"
            seen.add(col_name)
            assert col["type"] in ALLOWED_BQ_SCALAR_OR_JSON_TYPES, (
                f"{obj_name}.{col_name} uses disallowed type {col['type']!r} "
                "(lists/structured payloads must use JSON so NULL stays distinct from [])"
            )
            assert col["mode"] in {"REQUIRED", "NULLABLE"}
            assert len(col["description"].strip()) >= 12


def test_open_twins_are_strict_subsets_and_omit_restricted_columns() -> None:
    """Open twins preserve column order/types/modes and drop all restricted text, IDs, and digests."""
    for restricted_name, open_name in RESTRICTED_TWIN_PAIRS:
        restricted_cols = _load_schema(restricted_name)
        open_cols = _load_schema(open_name)
        restricted_by_name = _col_map(restricted_cols)
        assert len(open_cols) < len(restricted_cols)

        restricted_positions = {c["name"]: idx for idx, c in enumerate(restricted_cols)}
        prev_pos = -1
        for col in open_cols:
            cname = col["name"]
            assert cname in restricted_by_name, (
                f"{open_name}.{cname} missing from {restricted_name}"
            )
            rc = restricted_by_name[cname]
            assert col["type"] == rc["type"], f"{open_name}.{cname} type mismatch"
            assert col["mode"] == rc["mode"], f"{open_name}.{cname} mode mismatch"
            pos = restricted_positions[cname]
            assert pos > prev_pos, f"{open_name}.{cname} out of order relative to {restricted_name}"
            prev_pos = pos

    for open_obj in OPEN_OBJECTS:
        names = {c["name"] for c in _load_schema(open_obj)}
        leaked = sorted(names & FORBIDDEN_IN_OPEN_SCHEMAS)
        assert not leaked, f"{open_obj} leaks restricted columns: {leaked}"

    spans_names = {c["name"] for c in _load_schema("spans")}
    assert not (
        spans_names
        & {
            "arguments",
            "results",
            "prior_history",
            "turn_prompt",
            "turn_context",
            "turn_output",
            "step_outputs",
            "system_instructions",
            "tool_definitions",
        }
    ), "Internal spans base must stay text-free"


def test_tool_calls_repeat_number_and_hash_source_contract() -> None:
    """`repeat_number` sits in both tool twins, `args_sha256` only in restricted, with 4 hash sources."""
    tc = _col_map(_load_schema("tool_calls"))
    tco = _col_map(_load_schema("tool_calls_open"))

    assert "args_sha256" in tc and "args_sha256" not in tco
    assert tc["args_sha256"]["type"] == "STRING" and tc["args_sha256"]["mode"] == "NULLABLE"

    for twin_name, cmap in (("tool_calls", tc), ("tool_calls_open", tco)):
        assert cmap["repeat_number"]["type"] == "INT64"
        assert cmap["repeat_number"]["mode"] == "NULLABLE"
        assert cmap["args_hash_source"]["type"] == "STRING"
        assert cmap["args_hash_source"]["mode"] == "REQUIRED"
        for src in ("proxy", "silver", "application", "none"):
            assert src in cmap["args_hash_source"]["description"], (
                f"{twin_name}.args_hash_source description omits {src!r}"
            )
        assert cmap["args_hash_attested"]["type"] == "BOOL"
        assert cmap["args_hash_attested"]["mode"] == "NULLABLE"


def test_json_list_columns_and_five_read_whole_flags() -> None:
    """`trace_payloads` exposes 5 `*_read_whole` flags, address-only instruction/tool refs, and JSON lists."""
    tp = _col_map(_load_schema("trace_payloads"))
    read_whole_cols = sorted(k for k in tp if k.endswith("_read_whole"))
    assert read_whole_cols == [
        "prior_history_read_whole",
        "step_outputs_read_whole",
        "turn_context_read_whole",
        "turn_output_read_whole",
        "turn_prompt_read_whole",
    ]
    for flag in read_whole_cols:
        assert tp[flag]["type"] == "BOOL" and tp[flag]["mode"] == "NULLABLE"

    assert tp["system_instructions_ref"]["type"] == "STRING"
    assert tp["tool_definitions_ref"]["type"] == "STRING"

    json_expectations: tuple[tuple[str, str], ...] = (
        ("trace_payloads", "prior_history"),
        ("trace_payloads", "turn_prompt"),
        ("trace_payloads", "turn_context"),
        ("trace_payloads", "turn_output"),
        ("trace_payloads", "step_outputs"),
        ("trace_payloads", "media_uris"),
        ("model_calls", "selected_tool_names"),
        ("traces", "mounted_skills"),
        ("traces", "act_chain"),
        ("outcomes", "trace_ids"),
        ("events", "payload_json"),
    )
    for obj_name, col_name in json_expectations:
        cmap = _col_map(_load_schema(obj_name))
        assert cmap[col_name]["type"] == "JSON" and cmap[col_name]["mode"] == "NULLABLE"


def test_nullability_conventions_counts_tokens_and_provenance_flags() -> None:
    """Child-set counts are REQUIRED INT64, token sums and subject-less attested flags are NULLABLE."""
    traces = _col_map(_load_schema("traces"))
    assert "turn_starter" not in traces

    required_trace_counts = (
        "participating_app_count",
        "missing_parent_count",
        "user_id_conflict_count",
        "span_count",
        "model_call_count",
        "tool_call_count",
        "mutating_tool_call_count",
        "workspace_edit_count",
        "unregistered_tool_count",
        "guardrail_intervention_count",
        "content_missing_call_count",
        "user_turn_count",
        "duplicate_span_count",
        "repeated_tool_call_count",
        "error_count",
        "throttle_count",
        "missing_user_id_span_count",
        "missing_outcome_id_span_count",
        "missing_conversation_id_span_count",
    )
    for cname in required_trace_counts:
        assert traces[cname]["type"] == "INT64" and traces[cname]["mode"] == "REQUIRED"

    for tok_col in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    ):
        assert traces[tok_col]["type"] == "INT64" and traces[tok_col]["mode"] == "NULLABLE"

    mc = _col_map(_load_schema("model_calls"))
    assert mc["caller_retry_count"]["mode"] == "REQUIRED"
    assert mc["traffic_type_attested"]["mode"] == "NULLABLE"
    assert traces["identity_attested"]["mode"] == "NULLABLE"
    assert traces["app_identity_bound"]["mode"] == "NULLABLE"


def test_labels_registry_and_events_outcomes_columns() -> None:
    """`labels_registry.json` covers all 12 objects and registers non-overlapping slice-only keys."""
    reg_path = SCHEMA_DIR / "labels_registry.json"
    reg: dict[str, Any] = json.loads(reg_path.read_text(encoding="utf-8"))
    assert set(reg["objects"].keys()) == set(EXPECTED_SCHEMA_OBJECTS)

    schemas_by_obj = {obj: _col_map(_load_schema(obj)) for obj in EXPECTED_SCHEMA_OBJECTS}
    keys: list[dict[str, Any]] = reg["registered_label_keys"]
    assert len(keys) >= 25
    seen_keys: set[str] = set()
    for entry in keys:
        kname = str(entry["key"])
        assert SNAKE_CASE_RE.match(kname) and kname not in seen_keys
        seen_keys.add(kname)
        assert entry["value_type"] in ALLOWED_BQ_SCALAR_OR_JSON_TYPES
        objs: list[str] = entry["objects"]
        assert objs
        if not entry["open_safe"]:
            assert not (set(objs) & OPEN_OBJECTS), (
                f"Restricted label key {kname} listed on open view"
            )
        for target_obj in objs:
            assert "labels" in schemas_by_obj[target_obj]
            assert kname not in schemas_by_obj[target_obj], (
                f"Label key {kname} duplicates typed column on {target_obj}"
            )

    ev = _col_map(_load_schema("events"))
    for lifted in ("linked_outcome_id", "outcome_status", "state_transition_to", "feedback_reason"):
        assert lifted in ev and ev[lifted]["mode"] == "NULLABLE"

    out = _col_map(_load_schema("outcomes"))
    assert out["extends_outside_window"]["type"] == "BOOL"
    assert out["extends_outside_window"]["mode"] == "REQUIRED"
    assert out["business_unit"]["type"] == "STRING"
    assert out["min_model_confidence"]["type"] == "FLOAT64"
    assert out["final_model_confidence"]["type"] == "FLOAT64"
