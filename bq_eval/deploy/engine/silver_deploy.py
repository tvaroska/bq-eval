"""Silver SQL rendering, stdlib top-level SELECT alias extraction (`S-25`), and routine auth."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from bq_eval.deploy.contract.bootstrap import BootstrapValidationReport

SILVER_SQL_ORDER: tuple[str, ...] = (
    "silver/reference.sql",
    "silver/workflow_events.sql",
    "silver/shape3.sql",
    "silver/jcs.sql",
    "silver/spans.sql",
    "silver/model_calls.sql",
    "silver/tool_calls.sql",
    "silver/events.sql",
    "silver/traces.sql",
    "silver/trace_payloads.sql",
    "silver/outcomes.sql",
    "silver/capacity/tables.sql",
    "silver/capacity/requests.sql",
    "silver/wrapper_views.sql",
)

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")
_LOCATION_RE = re.compile(r"^[a-zA-Z0-9_-]{2,32}$")
_SHAPE3_FMT_ALLOWED = frozenset({"JSON", "PARQUET", "NEWLINE_DELIMITED_JSON"})
_TOKEN_RE = re.compile(
    r"""
    (?P<comment_line>(?:--|\#)[^\n]*)
    |(?P<comment_block>/\*.*?\*/)
    |(?P<triple_dq>[rRbB]?(?:\"\"\".*?\"\"\"))
    |(?P<triple_sq>[rRbB]?(?:'''.*?'''))
    |(?P<sq>[rRbB]?'(?:\\.|[^'\\])*')
    |(?P<dq>[rRbB]?"(?:\\.|[^"\\])*")
    |(?P<bt>`[^`]*`)
    |(?P<word>[A-Za-z_][A-Za-z0-9_]*)
    |(?P<punct>[(),.;])
    |(?P<other>\S)
    """,
    re.VERBOSE | re.DOTALL,
)


def _tokenize_sql(sql: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for match in _TOKEN_RE.finditer(sql):
        kind = match.lastgroup
        if kind in (None, "comment_line", "comment_block"):
            continue
        tokens.append((kind, match.group(0)))
    return tokens


def _skip_cte_block(tokens: Sequence[tuple[str, str]], start: int) -> int:
    """Advance past `WITH cte AS (...), cte2 AS (...)` to the outer `SELECT` token."""
    idx = start + 1
    n = len(tokens)
    while idx < n:
        if idx < n and tokens[idx][0] == "word" and tokens[idx][1].upper() == "RECURSIVE":
            idx += 1
        # Expect CTE name -> AS -> ( ... )
        if idx < n and tokens[idx][0] in {"word", "bt"}:
            idx += 1
        if idx < n and tokens[idx][0] == "word" and tokens[idx][1].upper() == "AS":
            idx += 1
        if idx < n and tokens[idx] == ("punct", "("):
            depth = 1
            idx += 1
            while idx < n and depth > 0:
                if tokens[idx] == ("punct", "("):
                    depth += 1
                elif tokens[idx] == ("punct", ")"):
                    depth -= 1
                idx += 1
        if idx < n and tokens[idx] == ("punct", ","):
            idx += 1
            continue
        break
    return idx


def _extract_item_alias(item_tokens: Sequence[tuple[str, str]]) -> str | None:
    """Extract the projected column name of a single top-level SELECT item."""
    if not item_tokens:
        return None
    # Scan top-level (paren_depth == 0) for explicit `AS <ident>` from right to left.
    depth = 0
    last_as_idx = -1
    for i, tok in enumerate(item_tokens):
        if tok == ("punct", "("):
            depth += 1
        elif tok == ("punct", ")"):
            depth -= 1
        elif depth == 0 and tok[0] == "word" and tok[1].upper() == "AS":
            last_as_idx = i
    if last_as_idx != -1 and last_as_idx + 1 < len(item_tokens):
        alias_tok = item_tokens[last_as_idx + 1]
        if alias_tok[0] in {"word", "bt"}:
            return alias_tok[1].strip("`")

    # Otherwise check trailing bare alias or dotted column reference (`t.col` or `col`).
    last = item_tokens[-1]
    if last[0] in {"word", "bt"}:
        cand = last[1].strip("`")
        if cand.upper() not in {"END", "NULL", "TRUE", "FALSE"}:
            return cand
    return None


def _advance_to_outer_select(tokens: Sequence[tuple[str, str]]) -> int:
    n = len(tokens)
    idx = 0
    if n >= 2 and tokens[0][0] == "word" and tokens[0][1].upper() == "CREATE":
        depth = 0
        while idx < n:
            if tokens[idx] == ("punct", "("):
                depth += 1
            elif tokens[idx] == ("punct", ")"):
                depth = max(0, depth - 1)
            elif depth == 0 and tokens[idx][0] == "word" and tokens[idx][1].upper() == "AS":
                idx += 1
                break
            idx += 1
        if idx < n and tokens[idx] == ("punct", "("):
            idx += 1
    if idx < n and tokens[idx][0] == "word" and tokens[idx][1].upper() == "WITH":
        idx = _skip_cte_block(tokens, idx)
    return idx


def _split_select_items(
    tokens: Sequence[tuple[str, str]],
    start: int,
) -> list[list[tuple[str, str]]]:
    items: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    depth = 0
    stop_keywords = {"FROM", "WHERE", "GROUP", "ORDER", "LIMIT", "UNION", "EXCEPT", "INTERSECT"}
    for tok in tokens[start:]:
        if tok == ("punct", "("):
            depth += 1
            cur.append(tok)
        elif tok == ("punct", ")"):
            if depth == 0:
                break
            depth -= 1
            cur.append(tok)
        elif depth == 0 and tok == ("punct", ","):
            items.append(cur)
            cur = []
        elif depth == 0 and (
            tok == ("punct", ";") or (tok[0] == "word" and tok[1].upper() in stop_keywords)
        ):
            break
        else:
            cur.append(tok)
    if cur:
        items.append(cur)
    return items


def extract_top_level_select_aliases(sql: str) -> list[str]:
    """Return ordered output column names projected by the outermost SELECT of a view or query (`S-25`)."""
    tokens = _tokenize_sql(sql)
    idx = _advance_to_outer_select(tokens)
    if idx >= len(tokens) or tokens[idx][0] != "word" or tokens[idx][1].upper() != "SELECT":
        return []
    idx += 1
    if (
        idx < len(tokens)
        and tokens[idx][0] == "word"
        and tokens[idx][1].upper() in {"DISTINCT", "ALL"}
    ):
        idx += 1
    aliases: list[str] = []
    for item in _split_select_items(tokens, idx):
        alias = _extract_item_alias(item)
        if alias is not None:
            aliases.append(alias)
    return aliases


_CREATED_OBJ_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+(?:TABLE\s+FUNCTION|VIEW)\s+`?([^`\s(]+)`?",
    re.IGNORECASE,
)
_SILVER_TVF_SCHEMAS: frozenset[str] = frozenset(
    {
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
    }
)
_SILVER_INTERNAL_BASES: frozenset[str] = frozenset(
    {"spans", "tool_calls_base", "events_base", "traces_base", "outcomes_base"}
)
_FORBIDDEN_OPEN_COLS: frozenset[str] = frozenset(
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


def validate_view_columns_ordered(
    view_sql: str,
    expected_columns: Sequence[str],
    *,
    view_name: str,
    exact: bool = False,
) -> list[str]:
    """Verify expected schema columns appear in the outermost SELECT alias list in order (`S-25`)."""
    projected = extract_top_level_select_aliases(view_sql)
    if not projected:
        return [f"{view_name}: failed to parse top-level SELECT column aliases"]
    seen: set[str] = set()
    dups = [c for c in projected if c in seen or seen.add(c)]  # type: ignore[func-returns-value]
    if dups:
        return [f"{view_name}: duplicate projected SELECT aliases {sorted(set(dups))}"]
    expected_list = list(expected_columns)
    projected_set = set(projected)
    missing = [c for c in expected_list if c not in projected_set]
    if missing:
        return [f"{view_name}: missing top-level SELECT columns {missing}"]
    if exact:
        extra = [c for c in projected if c not in set(expected_list)]
        if extra:
            return [f"{view_name}: unexpected extra SELECT columns {extra}"]
    positions = {name: i for i, name in enumerate(projected)}
    prev = -1
    for col in expected_list:
        pos = positions[col]
        if pos < prev:
            return [f"{view_name}: column {col!r} out of schema order"]
        prev = pos
    return []


def build_silver_routine_authorizations(  # noqa: PLR0913
    *,
    project_id: str,
    silver_internal_dataset: str = "silver_internal",
    silver_ref_dataset: str = "silver_ref",
    silver_open_dataset: str = "silver_open",
    silver_restricted_dataset: str = "silver_restricted",
    traces_dataset_id: str = "obs_traces",
    platform_traces_dataset_id: str = "silver_internal",
    logs_dataset_id: str = "obs_logs",
    gold_dataset_id: str | None = None,
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Return `{target_dataset: ((project_id, routine_dataset, routine_id), ...)}` for 2-hop TVF auth."""
    internal_bronze_tvfs = (
        (project_id, silver_internal_dataset, "spans"),
        (project_id, silver_internal_dataset, "tool_calls_base"),
        (project_id, silver_internal_dataset, "events_base"),
        (project_id, silver_internal_dataset, "traces_base"),
    )
    restricted_bronze_tvfs = (
        (project_id, silver_restricted_dataset, "tool_calls"),
        (project_id, silver_restricted_dataset, "trace_payloads"),
    )
    ref_reading_tvfs: list[tuple[str, str, str]] = [
        (project_id, silver_internal_dataset, "spans"),
        (project_id, silver_internal_dataset, "tool_calls_base"),
        (project_id, silver_internal_dataset, "events_base"),
        (project_id, silver_internal_dataset, "traces_base"),
        (project_id, silver_internal_dataset, "outcomes_base"),
        (project_id, silver_open_dataset, "model_calls"),
        (project_id, silver_open_dataset, "requests"),
        (project_id, silver_restricted_dataset, "tool_calls"),
        (project_id, silver_restricted_dataset, "trace_payloads"),
    ]
    outer_open_tvfs = (
        (project_id, silver_open_dataset, "model_calls"),
        (project_id, silver_open_dataset, "tool_calls_open"),
        (project_id, silver_open_dataset, "events_open"),
        (project_id, silver_open_dataset, "outcome_links"),
        (project_id, silver_open_dataset, "traces_open"),
        (project_id, silver_open_dataset, "outcomes_open"),
    )
    outer_restricted_tvfs = (
        (project_id, silver_restricted_dataset, "tool_calls"),
        (project_id, silver_restricted_dataset, "events"),
        (project_id, silver_restricted_dataset, "traces"),
        (project_id, silver_restricted_dataset, "trace_payloads"),
        (project_id, silver_restricted_dataset, "outcomes"),
    )
    auth_lists: dict[str, list[tuple[str, str, str]]] = {}

    def _add(target_ds: str, routines: Sequence[tuple[str, str, str]]) -> None:
        bucket = auth_lists.setdefault(target_ds, [])
        for entry in routines:
            if entry[1] == target_ds:
                continue
            if entry not in bucket:
                bucket.append(entry)

    _add(traces_dataset_id, (*internal_bronze_tvfs, *restricted_bronze_tvfs))
    _add(platform_traces_dataset_id, (*internal_bronze_tvfs, *restricted_bronze_tvfs))
    _add(
        logs_dataset_id,
        (
            (project_id, silver_internal_dataset, "events_base"),
            (project_id, silver_open_dataset, "requests"),
        ),
    )
    if gold_dataset_id is not None:
        gold_routine = (project_id, gold_dataset_id, "gold_call_costs")
        ref_reading_tvfs.append(gold_routine)
        _add(silver_open_dataset, (gold_routine,))
    _add(silver_ref_dataset, ref_reading_tvfs)
    _add(silver_internal_dataset, (*outer_open_tvfs, *outer_restricted_tvfs))
    return {ds: tuple(entries) for ds, entries in auth_lists.items() if entries}


def render_silver_params(  # noqa: PLR0913
    *,
    project_id: str,
    location: str = "US",
    silver_internal_dataset: str = "silver_internal",
    silver_ref_dataset: str = "silver_ref",
    silver_open_dataset: str = "silver_open",
    silver_restricted_dataset: str = "silver_restricted",
    traces_dataset_id: str = "obs_traces",
    platform_traces_dataset_id: str = "silver_internal",
    platform_traces_table_id: str = "platform_spans",
    logs_dataset_id: str = "obs_logs",
    connection_id: str = "us.vertex_ai_connection",
    payload_bucket: str | None = None,
    shape3_prefix: str = "bq-eval/shape3/",
    shape3_format: str = "JSON",
    read_margin_minutes: int = 60,
    settle_minutes: int = 5,
    outcome_lookback_days: int = 30,
) -> dict[str, str]:
    """Validate and return deploy-time template constants for `sql/silver/*.sql` (`ADR-048`)."""
    if not _LOCATION_RE.fullmatch(location.strip()):
        raise ValueError(f"Invalid location: {location!r}")
    for name, val in (
        ("silver_internal_dataset", silver_internal_dataset),
        ("silver_ref_dataset", silver_ref_dataset),
        ("silver_open_dataset", silver_open_dataset),
        ("silver_restricted_dataset", silver_restricted_dataset),
        ("traces_dataset_id", traces_dataset_id),
        ("platform_traces_dataset_id", platform_traces_dataset_id),
        ("platform_traces_table_id", platform_traces_table_id),
        ("logs_dataset_id", logs_dataset_id),
    ):
        if not _IDENT_RE.fullmatch(val.strip()):
            raise ValueError(f"Invalid {name}: {val!r}")
    fmt = shape3_format.strip().upper()
    if fmt not in _SHAPE3_FMT_ALLOWED:
        raise ValueError(f"Invalid shape3_format: {shape3_format!r}")
    for name, num in (
        ("read_margin_minutes", read_margin_minutes),
        ("settle_minutes", settle_minutes),
        ("outcome_lookback_days", outcome_lookback_days),
    ):
        if num <= 0:
            raise ValueError(f"{name} must be positive, got {num}")
    clean_prefix = shape3_prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"
    return {
        "PROJECT_ID": project_id.strip(),
        "LOCATION": location.strip(),
        "SILVER_INTERNAL_DATASET": silver_internal_dataset.strip(),
        "SILVER_REF_DATASET": silver_ref_dataset.strip(),
        "SILVER_OPEN_DATASET": silver_open_dataset.strip(),
        "SILVER_RESTRICTED_DATASET": silver_restricted_dataset.strip(),
        "TRACES_DATASET_ID": traces_dataset_id.strip(),
        "PLATFORM_TRACES_DATASET_ID": platform_traces_dataset_id.strip(),
        "PLATFORM_TRACES_TABLE_ID": platform_traces_table_id.strip(),
        "LOGS_DATASET_ID": logs_dataset_id.strip(),
        "CONNECTION_ID": connection_id.strip(),
        "PAYLOAD_BUCKET": (payload_bucket or project_id).strip(),
        "SHAPE3_PREFIX": clean_prefix,
        "SHAPE3_FORMAT": fmt,
        "READ_MARGIN_MINUTES": str(read_margin_minutes),
        "SETTLE_MINUTES": str(settle_minutes),
        "OUTCOME_LOOKBACK_DAYS": str(outcome_lookback_days),
    }


def _validate_one_silver_statement(  # noqa: PLR0913
    *,
    label: str,
    stmt: str,
    silver_internal_dataset: str,
    silver_open_dataset: str,
    silver_restricted_dataset: str,
    schema_cols_by_name: Mapping[str, list[str]],
    verified_tvfs: set[str],
) -> list[str]:
    stmt_errors: list[str] = []
    if "omni_spoke_views" in label:
        stmt_errors.append(f"{label}: Silver bootstrap must exclude retired omni spoke SQL")
    upper_stmt = stmt.upper()
    if "CREATE OR REPLACE TABLE FUNCTION" in upper_stmt or (
        "CREATE OR REPLACE FUNCTION" in upper_stmt
    ):
        # BigQuery enforces a hard 32,768-byte limit on routine definition bodies.
        body_bytes = len(stmt.encode("utf-8"))
        if body_bytes > 32_768:
            stmt_errors.append(
                f"{label}: BigQuery routine body {body_bytes} B exceeds 32,768 B limit"
            )
    created = _CREATED_OBJ_RE.search(stmt)
    if created is None:
        return stmt_errors
    parts = created.group(1).split(".")
    obj_dataset = parts[-2] if len(parts) >= 2 else ""
    obj_short = parts[-1]
    body_after_as = stmt[created.end() :]
    is_open_or_base = obj_dataset == silver_open_dataset or (
        obj_dataset == silver_internal_dataset and obj_short in _SILVER_INTERNAL_BASES
    )
    if is_open_or_base and (
        f"{silver_restricted_dataset}." in body_after_as or "shape3_payloads" in body_after_as
    ):
        stmt_errors.append(
            f"{label}: {obj_dataset}.{obj_short} must never read restricted/Shape-3 sources"
        )
    if obj_short in _SILVER_TVF_SCHEMAS and "TABLE FUNCTION" in upper_stmt[: created.start() + 40]:
        verified_tvfs.add(obj_short)
        expected = schema_cols_by_name.get(obj_short)
        if expected is not None:
            stmt_errors.extend(
                validate_view_columns_ordered(
                    stmt, expected, view_name=f"{label} ({obj_short})", exact=True
                )
            )
        if obj_dataset == silver_open_dataset:
            leaked = sorted(set(extract_top_level_select_aliases(stmt)) & _FORBIDDEN_OPEN_COLS)
            if leaked:
                stmt_errors.append(f"{label}: {obj_short} leaks restricted columns {leaked}")
    return stmt_errors


def validate_silver_statements_offline(  # noqa: PLR0913
    *,
    sql_dir: Path,
    stmts: Sequence[tuple[str, str]],
    project_id: str,
    silver_internal_dataset: str = "silver_internal",
    silver_open_dataset: str,
    silver_restricted_dataset: str,
    connection_id: str,
    payload_bucket: str,
    shape3_prefix: str,
    unresolved: set[str],
    errors: list[str],
    checked: int,
    skipped: int,
) -> BootstrapValidationReport:
    """Validate Silver SQL statements against schema files, column order (`S-25`), and open isolation."""
    schema_dir = sql_dir / "silver" / "schema"
    schema_cols_by_name: dict[str, list[str]] = {}
    for schema_path in sorted(schema_dir.glob("*.json")):
        if schema_path.name == "labels_registry.json":
            continue
        cols = [str(c["name"]) for c in json.loads(schema_path.read_text(encoding="utf-8"))]
        schema_cols_by_name[schema_path.stem] = cols
    total_cols = sum(len(cols) for cols in schema_cols_by_name.values())
    verified_tvfs: set[str] = set()

    for label, stmt in stmts:
        errors.extend(
            _validate_one_silver_statement(
                label=label,
                stmt=stmt,
                silver_internal_dataset=silver_internal_dataset,
                silver_open_dataset=silver_open_dataset,
                silver_restricted_dataset=silver_restricted_dataset,
                schema_cols_by_name=schema_cols_by_name,
                verified_tvfs=verified_tvfs,
            )
        )

    missing_tvfs = sorted(_SILVER_TVF_SCHEMAS - verified_tvfs)
    if missing_tvfs:
        errors.append(f"Silver bootstrap missing expected TVF definitions: {missing_tvfs}")

    clean_prefix = shape3_prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"
    return BootstrapValidationReport(
        mode="silver",
        project_id=project_id,
        dataset_id=silver_open_dataset,
        connection_id=connection_id,
        payload_uri_pattern=f"gs://{payload_bucket}/{clean_prefix}*",
        statement_count=len(stmts),
        checked_count=checked,
        skipped_count=skipped,
        schema_column_count=total_cols,
        unresolved_placeholders=tuple(sorted(unresolved)),
        errors=tuple(errors),
    )
