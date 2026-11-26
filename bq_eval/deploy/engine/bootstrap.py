"""Zero-manual-edit SQL bootstrap renderer and offline schema validator."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from bq_eval.core.engine.resources import resolve_packaged_data
from bq_eval.deploy.contract.bootstrap import BootstrapMode, BootstrapValidationReport
from bq_eval.deploy.engine.silver_deploy import (
    SILVER_SQL_ORDER,
    render_silver_params,
    validate_silver_statements_offline,
    validate_view_columns_ordered,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SQL_DIR = resolve_packaged_data("sql")

_PROJECT_RE = re.compile(r"^[a-zA-Z0-9_-]{2,64}$")
_DATASET_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")
_CONNECTION_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,128}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_PREFIX_RE = re.compile(r"^[a-zA-Z0-9_./-]*$")
_UNRESOLVED_TOKEN_RE = re.compile(r"\$\{[A-Z0-9_]+\}|YOUR_GENAI_PAYLOAD_BUCKET")

SKIP_DRY_RUN_PREFIXES: tuple[str, ...] = (
    "CREATE SCHEMA",
    "CREATE EXTERNAL TABLE",
    "CREATE MODEL",
    "CREATE OR REPLACE FUNCTION",
    "CREATE FUNCTION",
    "CREATE OR REPLACE PROCEDURE",
    "CREATE PROCEDURE",
    "ALTER TABLE",
)

ALLOWED_BOOTSTRAP_PREFIXES: tuple[str, ...] = (
    "CREATE SCHEMA",
    "CREATE TABLE",
    "CREATE OR REPLACE TABLE",
    "CREATE EXTERNAL TABLE",
    "CREATE OR REPLACE TABLE FUNCTION",
    "CREATE TABLE FUNCTION",
    "CREATE OR REPLACE FUNCTION",
    "CREATE FUNCTION",
    "CREATE OR REPLACE VIEW",
    "CREATE VIEW",
    "CREATE MODEL",
    "CREATE OR REPLACE PROCEDURE",
    "CREATE PROCEDURE",
    "ALTER TABLE",
    "MERGE INTO",
)

_VIEW_NAME_RE = re.compile(
    r"(?is)^CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+`?([a-zA-Z0-9_.-]+)`?\s+AS\b"
)
_FULL_CAPTURE_ONLY_OBJECTS: frozenset[str] = frozenset(
    {
        "completions",
        "v_otel_allspans_only",
        "v_otel_logs_payloads_only",
        "otel_spans",
        "trace_payloads",
        "v_mode_c_hybrid_otel_spans",
        "v_mode_c_hybrid_trace_payloads",
    }
)


def _validate_param(value: str, pattern: re.Pattern[str], name: str) -> str:
    cleaned = value.strip()
    if not cleaned or not pattern.fullmatch(cleaned):
        raise ValueError(f"Invalid {name} {value!r}: must match {pattern.pattern}")
    return cleaned


def normalize_payload_prefix(prefix: str) -> str:
    """Normalize a GCS object prefix (`''` or trailing `'/'`, never leading `'/'`)."""
    cleaned = prefix.strip().lstrip("/")
    if not _PREFIX_RE.fullmatch(cleaned) or ".." in cleaned:
        raise ValueError(f"Invalid payload_prefix {prefix!r}")
    if cleaned and not cleaned.endswith("/"):
        cleaned = f"{cleaned}/"
    return cleaned


def strip_leading_sql_comments(sql: str) -> str:
    """Strip leading whitespace, `--`/`#` line comments, and `/* ... */` block comments."""
    i, n = 0, len(sql)
    while i < n:
        if sql[i].isspace():
            i += 1
        elif sql.startswith("--", i) or sql.startswith("#", i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                return ""
            i = end + 2
        else:
            break
    return sql[i:].strip()


def _advance_word_depth(
    word: str, next_word: str, block_depth: int, case_depth: int
) -> tuple[int, int]:
    if word == "CASE":
        return block_depth, case_depth + 1
    if word == "BEGIN":
        return (block_depth + 1 if next_word != "TRANSACTION" else block_depth), case_depth
    if word == "END":
        if next_word in {"IF", "LOOP", "WHILE", "FOR"}:
            return block_depth, case_depth
        if case_depth > 0:
            return block_depth, case_depth - 1
        return max(0, block_depth - 1), case_depth
    return block_depth, case_depth


def _skip_quoted_literal(sql: str, i: int) -> int:
    """Advance past a triple-quoted or single/double-quoted BigQuery string literal."""
    n = len(sql)
    offset = 1 if sql[i] in {"r", "R", "b", "B"} else 0
    if sql.startswith('"""', i + offset) or sql.startswith("'''", i + offset):
        delim = sql[i + offset : i + offset + 3]
        end_tq = sql.find(delim, i + offset + 3)
        return -1 if end_tq == -1 else end_tq + 3
    quote = sql[i]
    j = i + 1
    while j < n:
        if sql[j] == "\\":
            j += 2
            continue
        if sql[j] == quote:
            return j + 1
        j += 1
    return -1


def _skip_sql_token_or_comment(sql: str, i: int) -> int | None:
    """Advance past a quoted literal, backtick identifier, or SQL comment if present."""
    n = len(sql)
    if sql.startswith("--", i) or sql.startswith("#", i):
        nl = sql.find("\n", i)
        return n if nl == -1 else nl + 1
    if sql.startswith("/*", i):
        end = sql.find("*/", i + 2)
        return -1 if end == -1 else end + 2
    is_prefixed_triple = sql[i] in {"r", "R", "b", "B"} and (
        sql.startswith('"""', i + 1) or sql.startswith("'''", i + 1)
    )
    if is_prefixed_triple or sql[i] in {"'", '"'}:
        return _skip_quoted_literal(sql, i)
    if sql[i] == "`":
        end_bt = sql.find("`", i + 1)
        return -1 if end_bt == -1 else end_bt + 1
    return None


def _read_identifier(sql: str, i: int) -> tuple[str, int]:
    n = len(sql)
    j = i + 1
    while j < n and (sql[j].isalnum() or sql[j] == "_"):
        j += 1
    return sql[i:j].upper(), j


def split_sql_statements_stdlib(sql: str) -> list[str]:
    """Split a multi-statement BigQuery SQL script without external dependencies.

    Preserves `BEGIN ... END;` stored-procedure bodies as single statements while respecting
    single-quoted strings, backtick identifiers, and `--` / `/* ... */` comments.
    """
    stmts: list[str] = []
    n = len(sql)
    i = 0
    chunk_start = 0
    block_depth = 0
    case_depth = 0

    while i < n:
        skipped = _skip_sql_token_or_comment(sql, i)
        if skipped is not None:
            i = n if skipped == -1 else skipped
            continue
        ch = sql[i]
        if ch == ";" and block_depth == 0:
            stmt = strip_leading_sql_comments(sql[chunk_start:i])
            if stmt:
                stmts.append(stmt)
            chunk_start = i + 1
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            word, j = _read_identifier(sql, i)
            if word in {"BEGIN", "END", "CASE"}:
                k = j
                while k < n and sql[k].isspace():
                    k += 1
                next_word = (
                    _read_identifier(sql, k)[0]
                    if k < n and (sql[k].isalpha() or sql[k] == "_")
                    else ""
                )
                block_depth, case_depth = _advance_word_depth(
                    word, next_word, block_depth, case_depth
                )
            i = j
            continue
        i += 1

    tail = strip_leading_sql_comments(sql[chunk_start:])
    if tail:
        stmts.append(tail)
    return stmts


def should_skip_dry_run_statement(statement: str) -> bool:
    """Return True for DDL/procedure statements skipped in `bq query --dry_run`."""
    cleaned = strip_leading_sql_comments(statement)
    if not cleaned:
        return True
    upper = cleaned.upper()
    return any(upper.startswith(prefix) for prefix in SKIP_DRY_RUN_PREFIXES)


def substitute_sql_parameters(  # noqa: PLR0913
    sql: str,
    *,
    project_id: str,
    dataset_id: str = "agent_eval",
    connection_id: str = "us.vertex_ai_connection",
    payload_bucket: str | None = None,
    payload_prefix: str = "bq-eval/",
    traces_dataset_id: str = "obs_traces",
    logs_dataset_id: str = "obs_logs",
    seed_dataset_id: str = "agent_eval_seed",
    aws_omni_connection_id: str | None = None,
    aws_s3_bucket: str | None = None,
    azure_omni_connection_id: str | None = None,
    azure_storage_account: str | None = None,
    azure_container: str | None = None,
) -> str:
    """Render parameterized BigQuery SQL with concrete project, dataset, connection, and bucket."""
    valid_project = _validate_param(project_id, _PROJECT_RE, "project_id")
    valid_dataset = _validate_param(dataset_id, _DATASET_RE, "dataset_id")
    valid_conn = _validate_param(connection_id, _CONNECTION_RE, "connection_id")
    valid_bucket = _validate_param(payload_bucket or valid_project, _BUCKET_RE, "payload_bucket")
    valid_prefix = normalize_payload_prefix(payload_prefix)
    valid_traces = _validate_param(traces_dataset_id, _DATASET_RE, "traces_dataset_id")
    valid_logs = _validate_param(logs_dataset_id, _DATASET_RE, "logs_dataset_id")
    valid_seed = _validate_param(seed_dataset_id, _DATASET_RE, "seed_dataset_id")

    rendered = (
        sql.replace("${PROJECT_ID}", valid_project)
        .replace("${DATASET_ID}", valid_dataset)
        .replace("${CONNECTION_ID}", valid_conn)
        .replace("${PAYLOAD_BUCKET}", valid_bucket)
        .replace("${PREFIX}", valid_prefix)
    )
    if aws_omni_connection_id is not None:
        valid_aws_conn = _validate_param(
            aws_omni_connection_id, _CONNECTION_RE, "aws_omni_connection_id"
        )
        rendered = rendered.replace("${AWS_OMNI_CONNECTION_ID}", valid_aws_conn)
    if aws_s3_bucket is not None:
        valid_aws_bucket = _validate_param(aws_s3_bucket, _BUCKET_RE, "aws_s3_bucket")
        rendered = rendered.replace("${AWS_S3_BUCKET}", valid_aws_bucket)
    if azure_omni_connection_id is not None:
        valid_az_conn = _validate_param(
            azure_omni_connection_id, _CONNECTION_RE, "azure_omni_connection_id"
        )
        rendered = rendered.replace("${AZURE_OMNI_CONNECTION_ID}", valid_az_conn)
    if azure_storage_account is not None:
        valid_az_acct = _validate_param(azure_storage_account, _BUCKET_RE, "azure_storage_account")
        rendered = rendered.replace("${AZURE_STORAGE_ACCOUNT}", valid_az_acct)
    if azure_container is not None:
        valid_az_ctr = _validate_param(azure_container, _BUCKET_RE, "azure_container")
        rendered = rendered.replace("${AZURE_CONTAINER}", valid_az_ctr)
    rendered = rendered.replace("`agent_eval`", f"`{valid_project}.{valid_dataset}`")
    rendered = rendered.replace("`agent_eval_seed`", f"`{valid_project}.{valid_seed}`")
    rendered = rendered.replace("`agent_eval.", f"`{valid_project}.{valid_dataset}.")
    rendered = rendered.replace("`agent_eval_seed.", f"`{valid_project}.{valid_seed}.")
    rendered = rendered.replace("`obs_traces.", f"`{valid_project}.{valid_traces}.")
    rendered = rendered.replace("`obs_logs.", f"`{valid_project}.{valid_logs}.")
    return rendered


def _target_object_short_name(stmt: str) -> str:
    match = re.match(
        r"(?is)^CREATE\s+(?:OR\s+REPLACE\s+)?(?:EXTERNAL\s+)?(?:TABLE|VIEW|MODEL|PROCEDURE)\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?`?([a-zA-Z0-9_.-]+)`?",
        stmt,
    )
    if match is None:
        return ""
    return match.group(1).rsplit(".", 1)[-1].lower()


def _filter_light_mode_statements(
    stmts: Sequence[str],
) -> list[str]:
    kept: list[str] = []
    for stmt in stmts:
        short = _target_object_short_name(stmt)
        if short in _FULL_CAPTURE_ONLY_OBJECTS:
            continue
        if "_AllSpans" in stmt or "_AllLogs" in stmt:
            continue
        kept.append(stmt)
    return kept


def _build_mode_core_statements(
    *,
    mode: BootstrapMode,
    valid_project: str,
    valid_dataset: str,
    loaded: dict[str, list[str]],
) -> list[tuple[str, str]]:
    ddl_stmts = loaded["core/ddl.sql"]
    pricing_stmts = loaded["core/pricing_seed.sql"]
    compat_stmts = loaded.get("core/otel_compat_views.sql", [])
    bqaa_stmts = loaded.get("core/bqaa_adapter_views.sql", [])

    if mode == "full":
        out = [(f"core/ddl.sql#{i}", s) for i, s in enumerate(ddl_stmts, start=1)]
        out.extend((f"core/pricing_seed.sql#{i}", s) for i, s in enumerate(pricing_stmts, start=1))
        out.extend(
            (f"core/otel_compat_views.sql#{i}", s) for i, s in enumerate(compat_stmts, start=1)
        )
        return out
    if mode == "light":
        out = [
            (f"core/ddl.sql#{i}", s)
            for i, s in enumerate(_filter_light_mode_statements(ddl_stmts), start=1)
        ]
        out.extend((f"core/pricing_seed.sql#{i}", s) for i, s in enumerate(pricing_stmts, start=1))
        out.extend(
            (f"core/bqaa_adapter_views.sql#{i}", s)
            for i, s in enumerate(_filter_light_mode_statements(bqaa_stmts), start=1)
        )
        out.append(
            (
                "mode_a_binding#1",
                f"CREATE OR REPLACE VIEW `{valid_project}.{valid_dataset}.otel_spans` AS\n"
                f"SELECT * FROM `{valid_project}.{valid_dataset}.v_mode_a_otel_spans`",
            )
        )
        out.append(
            (
                "mode_a_binding#2",
                f"CREATE OR REPLACE VIEW `{valid_project}.{valid_dataset}.trace_payloads` AS\n"
                f"SELECT * FROM `{valid_project}.{valid_dataset}.v_mode_a_trace_payloads`",
            )
        )
        return out

    out = [(f"core/ddl.sql#{i}", s) for i, s in enumerate(ddl_stmts, start=1)]
    out.extend((f"core/pricing_seed.sql#{i}", s) for i, s in enumerate(pricing_stmts, start=1))
    out.extend(
        (f"core/otel_compat_views.sql#{i}", s)
        for i, s in enumerate(compat_stmts, start=1)
        if _target_object_short_name(s) not in {"otel_spans", "trace_payloads"}
    )
    out.extend((f"core/bqaa_adapter_views.sql#{i}", s) for i, s in enumerate(bqaa_stmts, start=1))
    out.append(
        (
            "mode_c_binding#1",
            f"CREATE OR REPLACE VIEW `{valid_project}.{valid_dataset}.otel_spans` AS\n"
            f"SELECT * FROM `{valid_project}.{valid_dataset}.v_mode_c_hybrid_otel_spans`",
        )
    )
    out.append(
        (
            "mode_c_binding#2",
            f"CREATE OR REPLACE VIEW `{valid_project}.{valid_dataset}.trace_payloads` AS\n"
            f"SELECT * FROM `{valid_project}.{valid_dataset}.v_mode_c_hybrid_trace_payloads`",
        )
    )
    return out


def _render_silver_sql_files(
    resolved_sql_dir: Path,
    silver_params: dict[str, str],
) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    for rel_name in SILVER_SQL_ORDER:
        rendered = (resolved_sql_dir / rel_name).read_text(encoding="utf-8")
        for key, val in silver_params.items():
            rendered = rendered.replace(f"${{{key}}}", val)
        for idx, stmt in enumerate(split_sql_statements_stdlib(rendered), start=1):
            ordered.append((f"{rel_name}#{idx}", stmt))
    return ordered


def render_bootstrap_statements(  # noqa: PLR0913
    *,
    mode: BootstrapMode,
    project_id: str,
    dataset_id: str = "agent_eval",
    location: str = "US",
    connection_id: str = "us.vertex_ai_connection",
    payload_bucket: str | None = None,
    payload_prefix: str = "bq-eval/",
    traces_dataset_id: str = "obs_traces",
    platform_traces_dataset_id: str = "silver_internal",
    platform_traces_table_id: str = "platform_spans",
    logs_dataset_id: str = "obs_logs",
    silver_internal_dataset: str = "silver_internal",
    silver_ref_dataset: str = "silver_ref",
    silver_open_dataset: str = "silver_open",
    silver_restricted_dataset: str = "silver_restricted",
    read_margin_minutes: int = 60,
    settle_minutes: int = 5,
    outcome_lookback_days: int = 30,
    shape3_prefix: str = "bq-eval/shape3/",
    shape3_format: str = "JSON",
    include_omni: bool = False,
    aws_omni_connection_id: str | None = None,
    aws_s3_bucket: str | None = None,
    azure_omni_connection_id: str | None = None,
    azure_storage_account: str | None = None,
    azure_container: str | None = None,
    sql_dir: Path | None = None,
) -> list[tuple[str, str]]:
    """Build ordered `(label, sql_statement)` list for `light`, `full`, `hybrid`, or `silver` deploy."""
    if mode not in ("light", "full", "hybrid", "silver"):
        raise ValueError(
            f"Unsupported bootstrap mode {mode!r}: expected light, full, hybrid, or silver."
        )

    resolved_sql_dir = sql_dir if sql_dir is not None else resolve_packaged_data("sql")
    valid_project = _validate_param(project_id, _PROJECT_RE, "project_id")
    if mode == "silver":
        valid_conn = _validate_param(connection_id, _CONNECTION_RE, "connection_id")
        valid_bucket = _validate_param(
            payload_bucket or valid_project, _BUCKET_RE, "payload_bucket"
        )
        silver_params = render_silver_params(
            project_id=valid_project,
            location=location,
            silver_internal_dataset=silver_internal_dataset,
            silver_ref_dataset=silver_ref_dataset,
            silver_open_dataset=silver_open_dataset,
            silver_restricted_dataset=silver_restricted_dataset,
            traces_dataset_id=traces_dataset_id,
            platform_traces_dataset_id=platform_traces_dataset_id,
            platform_traces_table_id=platform_traces_table_id,
            logs_dataset_id=logs_dataset_id,
            connection_id=valid_conn,
            payload_bucket=valid_bucket,
            shape3_prefix=shape3_prefix,
            shape3_format=shape3_format,
            read_margin_minutes=read_margin_minutes,
            settle_minutes=settle_minutes,
            outcome_lookback_days=outcome_lookback_days,
        )
        return _render_silver_sql_files(resolved_sql_dir, silver_params)

    valid_dataset = _validate_param(dataset_id, _DATASET_RE, "dataset_id")

    def _load(filename: str) -> list[str]:
        raw = (resolved_sql_dir / filename).read_text(encoding="utf-8")
        rendered = substitute_sql_parameters(
            raw,
            project_id=valid_project,
            dataset_id=valid_dataset,
            connection_id=connection_id,
            payload_bucket=payload_bucket,
            payload_prefix=payload_prefix,
            traces_dataset_id=traces_dataset_id,
            logs_dataset_id=logs_dataset_id,
            aws_omni_connection_id=aws_omni_connection_id,
            aws_s3_bucket=aws_s3_bucket,
            azure_omni_connection_id=azure_omni_connection_id,
            azure_storage_account=azure_storage_account,
            azure_container=azure_container,
        )
        return split_sql_statements_stdlib(rendered)

    loaded_core: dict[str, list[str]] = {
        "core/ddl.sql": _load("core/ddl.sql"),
        "core/pricing_seed.sql": _load("core/pricing_seed.sql"),
    }
    if mode != "light":
        loaded_core["core/otel_compat_views.sql"] = _load("core/otel_compat_views.sql")
    if mode != "full":
        loaded_core["core/bqaa_adapter_views.sql"] = _load("core/bqaa_adapter_views.sql")

    ordered = _build_mode_core_statements(
        mode=mode,
        valid_project=valid_project,
        valid_dataset=valid_dataset,
        loaded=loaded_core,
    )
    common_files = [
        "core/step1_trace_views.sql",
        "core/step2_stateless_eval.sql",
        "core/step3_saved_results.sql",
        "core/step3_daily_quota_guard.sql",
        "core/views.sql",
        "core/summary_views.sql",
        "finops/capacity_views.sql",
    ]
    if include_omni and (resolved_sql_dir / "finops" / "omni_spoke_views.sql").is_file():
        common_files.append("finops/omni_spoke_views.sql")

    for fname in common_files:
        ordered.extend((f"{fname}#{idx}", stmt) for idx, stmt in enumerate(_load(fname), start=1))
    return ordered


def _has_balanced_delimiters(stmt: str) -> bool:
    paren_depth = 0
    i, n = 0, len(stmt)
    while i < n:
        skipped = _skip_sql_token_or_comment(stmt, i)
        if skipped is not None:
            if skipped == -1:
                return False
            i = skipped
            continue
        if stmt[i] == "(":
            paren_depth += 1
        elif stmt[i] == ")":
            paren_depth -= 1
            if paren_depth < 0:
                return False
        i += 1
    return paren_depth == 0


def _detect_view_cycles(views: dict[str, str]) -> list[str]:
    view_names = set(views)
    deps: dict[str, set[str]] = {}
    for vname, body in views.items():
        refs = {
            m.group(1)
            for m in re.finditer(r"`([a-zA-Z0-9_.-]+)`", body)
            if m.group(1) in view_names and m.group(1) != vname
        }
        deps[vname] = refs
    in_deg = {k: len(v) for k, v in deps.items()}
    ready = sorted(k for k, d in in_deg.items() if d == 0)
    visited: set[str] = set()
    while ready:
        cur = ready.pop(0)
        visited.add(cur)
        for cand, refs in deps.items():
            if cur in refs:
                in_deg[cand] -= 1
                if in_deg[cand] == 0:
                    ready.append(cand)
                    ready.sort()
    return sorted(view_names - visited)


def _validate_mode_bindings(  # noqa: PLR0913
    *,
    mode: BootstrapMode,
    project_id: str,
    dataset_id: str,
    stmts: Sequence[tuple[str, str]],
    views: dict[str, str],
    schema_cols: Sequence[str],
) -> list[str]:
    errors: list[str] = []
    expected_otel = f"{project_id}.{dataset_id}.otel_spans"
    expected_payload = f"{project_id}.{dataset_id}.trace_payloads"
    if expected_otel not in views:
        errors.append(f"Missing canonical view binding: {expected_otel}")
    if expected_payload not in views:
        errors.append(f"Missing canonical view binding: {expected_payload}")

    expected_targets: dict[BootstrapMode, tuple[str, tuple[str, ...]]] = {
        "light": ("v_mode_a_otel_spans", (f"{project_id}.{dataset_id}.v_bqaa_otel_spans",)),
        "full": ("v_otel_allspans_only", (f"{project_id}.{dataset_id}.v_otel_allspans_only",)),
        "hybrid": (
            "v_mode_c_hybrid_otel_spans",
            (
                f"{project_id}.{dataset_id}.v_otel_allspans_only",
                f"{project_id}.{dataset_id}.v_bqaa_otel_spans",
            ),
        ),
    }
    binding_ref, adapter_views = expected_targets[mode]
    if mode == "light":
        for label, stmt in stmts:
            if "_AllSpans" in stmt or "_AllLogs" in stmt:
                errors.append(f"{label}: light mode must not reference _AllSpans or _AllLogs")
    if expected_otel in views and binding_ref not in views[expected_otel]:
        errors.append(f"{mode} mode otel_spans must point to {binding_ref}")

    for target_view in adapter_views:
        if target_view not in views:
            errors.append(f"Missing required span adapter view: {target_view}")
            continue
        errors.extend(
            validate_view_columns_ordered(
                views[target_view],
                schema_cols,
                view_name=target_view,
            )
        )
    return errors


def validate_bootstrap_offline(  # noqa: PLR0913
    *,
    mode: BootstrapMode,
    project_id: str = "showcase-project",
    dataset_id: str = "agent_eval",
    location: str = "US",
    connection_id: str = "us.vertex_ai_connection",
    payload_bucket: str | None = None,
    payload_prefix: str = "bq-eval/",
    traces_dataset_id: str = "obs_traces",
    platform_traces_dataset_id: str = "silver_internal",
    platform_traces_table_id: str = "platform_spans",
    logs_dataset_id: str = "obs_logs",
    silver_internal_dataset: str = "silver_internal",
    silver_ref_dataset: str = "silver_ref",
    silver_open_dataset: str = "silver_open",
    silver_restricted_dataset: str = "silver_restricted",
    read_margin_minutes: int = 60,
    settle_minutes: int = 5,
    outcome_lookback_days: int = 30,
    shape3_prefix: str = "bq-eval/shape3/",
    shape3_format: str = "JSON",
    include_omni: bool = False,
    aws_omni_connection_id: str | None = None,
    aws_s3_bucket: str | None = None,
    azure_omni_connection_id: str | None = None,
    azure_storage_account: str | None = None,
    azure_container: str | None = None,
    sql_dir: Path | None = None,
) -> tuple[BootstrapValidationReport, list[tuple[str, str]]]:
    """Validate rendered bootstrap SQL offline (zero GCP credentials) across placeholders, schema & DAG."""
    resolved_sql_dir = sql_dir if sql_dir is not None else resolve_packaged_data("sql")
    stmts = render_bootstrap_statements(
        mode=mode,
        project_id=project_id,
        dataset_id=dataset_id,
        location=location,
        connection_id=connection_id,
        payload_bucket=payload_bucket,
        payload_prefix=payload_prefix,
        traces_dataset_id=traces_dataset_id,
        platform_traces_dataset_id=platform_traces_dataset_id,
        platform_traces_table_id=platform_traces_table_id,
        logs_dataset_id=logs_dataset_id,
        silver_internal_dataset=silver_internal_dataset,
        silver_ref_dataset=silver_ref_dataset,
        silver_open_dataset=silver_open_dataset,
        silver_restricted_dataset=silver_restricted_dataset,
        read_margin_minutes=read_margin_minutes,
        settle_minutes=settle_minutes,
        outcome_lookback_days=outcome_lookback_days,
        shape3_prefix=shape3_prefix,
        shape3_format=shape3_format,
        include_omni=include_omni,
        aws_omni_connection_id=aws_omni_connection_id,
        aws_s3_bucket=aws_s3_bucket,
        azure_omni_connection_id=azure_omni_connection_id,
        azure_storage_account=azure_storage_account,
        azure_container=azure_container,
        sql_dir=resolved_sql_dir,
    )
    unresolved: set[str] = set()
    errors: list[str] = []
    views: dict[str, str] = {}
    checked = 0
    skipped = 0

    for label, stmt in stmts:
        for m in _UNRESOLVED_TOKEN_RE.finditer(stmt):
            unresolved.add(m.group(0))
        if not any(stmt.upper().startswith(p) for p in ALLOWED_BOOTSTRAP_PREFIXES):
            errors.append(f"{label}: unsupported statement prefix ({stmt[:48]!r})")
        if not _has_balanced_delimiters(stmt):
            errors.append(f"{label}: unbalanced SQL quotes, backticks, or parentheses")
        if should_skip_dry_run_statement(stmt):
            skipped += 1
        else:
            checked += 1
        vm = _VIEW_NAME_RE.match(stmt)
        if vm is not None:
            views[vm.group(1)] = stmt

    bucket = payload_bucket or project_id
    if mode == "silver":
        report = validate_silver_statements_offline(
            sql_dir=resolved_sql_dir,
            stmts=stmts,
            project_id=project_id,
            silver_internal_dataset=silver_internal_dataset,
            silver_open_dataset=silver_open_dataset,
            silver_restricted_dataset=silver_restricted_dataset,
            connection_id=connection_id,
            payload_bucket=bucket,
            shape3_prefix=shape3_prefix,
            unresolved=unresolved,
            errors=errors,
            checked=checked,
            skipped=skipped,
        )
        return report, stmts

    schema_path = resolved_sql_dir / "core" / "otel_spans_schema.json"
    schema_cols = [str(col["name"]) for col in json.loads(schema_path.read_text(encoding="utf-8"))]

    errors.extend(
        _validate_mode_bindings(
            mode=mode,
            project_id=project_id,
            dataset_id=dataset_id,
            stmts=stmts,
            views=views,
            schema_cols=schema_cols,
        )
    )
    cycles = _detect_view_cycles(views)
    if cycles:
        errors.append(f"Cyclic view dependency detected: {cycles}")

    prefix = normalize_payload_prefix(payload_prefix)
    report = BootstrapValidationReport(
        mode=mode,
        project_id=project_id,
        dataset_id=dataset_id,
        connection_id=connection_id,
        payload_uri_pattern=f"gs://{bucket}/{prefix}*.jsonl",
        statement_count=len(stmts),
        checked_count=checked,
        skipped_count=skipped,
        schema_column_count=len(schema_cols),
        unresolved_placeholders=tuple(sorted(unresolved)),
        errors=tuple(errors),
    )
    return report, stmts
