"""Skill SQL recipe validation, EVAL.yaml execution, and `bq_dry_run.py` tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp

from bq_eval.core.engine.sql_eval import build_ai_generate_table_judge_sql
from scripts import bq_dry_run

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "bq-eval"
RECIPES_PATH = SKILL_DIR / "references" / "sql_recipes.md"
SKILL_MD_PATH = SKILL_DIR / "SKILL.md"
EVAL_YAML_PATH = SKILL_DIR / "EVAL.yaml"
DRY_RUN_RECEIPT_PATH = REPO_ROOT / "benchmarks" / "results" / "bq_dry_run_receipt.json"

YamlScalar = str | bool | int | float


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    workflow: str
    prompt: str
    required_fragments: list[str]
    forbidden_fragments: list[str]


@dataclass(frozen=True)
class SkillEvalSpec:
    fail_closed_on_zero_scored: bool
    min_scored_cases: int
    pass_threshold: float
    principles: list[str]
    cases: list[EvalCase]


def _normalize_ai_tvfs(sql: str) -> str:
    normalized = re.sub(
        r"(AI\.GENERATE_TABLE\s*\(\s*)MODEL\s+",
        r"\1",
        sql,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(AI\.(?:GENERATE_TABLE|EVALUATE_AGENT)\s*\([^;]*?,\s*)TABLE\s+",
        r"\1",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(
        r"(AI\.EVALUATE_AGENT\s*\(\s*)TABLE\s+",
        r"\1",
        normalized,
        flags=re.IGNORECASE,
    )


def _unquote_yaml_scalar(raw: str) -> YamlScalar:
    val = raw.strip()
    parsed: YamlScalar = val
    if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
        inner = val[1:-1]
        parsed = bytes(inner, "utf-8").decode("unicode_escape") if val[0] == '"' else inner
    elif val in {"true", "false"}:
        parsed = val == "true"
    elif re.fullmatch(r"-?\d+", val):
        parsed = int(val)
    elif re.fullmatch(r"-?\d+\.\d+", val):
        parsed = float(val)
    return parsed


def _parse_case_block(lines: list[str]) -> EvalCase:
    fields: dict[str, str] = {}
    lists: dict[str, list[str]] = {"required_fragments": [], "forbidden_fragments": []}
    active_list: str | None = None

    for stripped in lines:
        if stripped.startswith("- id:"):
            fields["id"] = str(_unquote_yaml_scalar(stripped.split(":", 1)[1]))
            active_list = None
        elif stripped.startswith("- ") and active_list is not None:
            lists[active_list].append(str(_unquote_yaml_scalar(stripped[2:])))
        elif ":" in stripped:
            key, _, val = stripped.partition(":")
            if val.strip():
                fields[key] = str(_unquote_yaml_scalar(val))
                active_list = None
            else:
                active_list = key
                lists.setdefault(key, [])

    return EvalCase(
        case_id=fields["id"],
        workflow=fields["workflow"],
        prompt=fields["prompt"],
        required_fragments=lists["required_fragments"],
        forbidden_fragments=lists["forbidden_fragments"],
    )


def _parse_skill_eval_yaml(text: str) -> SkillEvalSpec:
    scalars: dict[str, YamlScalar] = {}
    principles: list[str] = []
    case_chunks: list[list[str]] = []
    section = ""

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent == 0:
            key, _, val = stripped.partition(":")
            section = key if key in {"principles", "cases"} else ""
            if not section:
                scalars[key] = _unquote_yaml_scalar(val)
        elif section == "principles" and stripped.startswith("- "):
            principles.append(str(_unquote_yaml_scalar(stripped[2:])))
        elif section == "cases":
            if stripped.startswith("- id:"):
                case_chunks.append([stripped])
            elif case_chunks:
                case_chunks[-1].append(stripped)

    return SkillEvalSpec(
        fail_closed_on_zero_scored=bool(scalars.get("fail_closed_on_zero_scored", False)),
        min_scored_cases=int(scalars["min_scored_cases"]),
        pass_threshold=float(scalars["pass_threshold"]),
        principles=principles,
        cases=[_parse_case_block(chunk) for chunk in case_chunks],
    )


def test_skill_sql_recipes_parse_cleanly_as_bigquery_ast() -> None:
    """Every SQL block in `skills/bq-eval/references/sql_recipes.md` parses via `sqlglot`."""
    recipes_text = RECIPES_PATH.read_text(encoding="utf-8")
    sql_blocks = re.findall(r"```sql\n(.*?)```", recipes_text, flags=re.DOTALL)
    assert len(sql_blocks) >= 8

    for idx, block in enumerate(sql_blocks, start=1):
        stripped = bq_dry_run.strip_leading_sql_comments(block)
        assert stripped, f"SQL recipe block #{idx} is empty"
        first_word = stripped.split(None, 1)[0].upper()
        if first_word in {"MERGE", "SELECT", "INSERT", "WITH", "CREATE"}:
            stmts = [s for s in sqlglot.parse(_normalize_ai_tvfs(block), read="bigquery") if s]
            assert stmts, f"SQL recipe block #{idx} parsed to zero statements"
            for stmt in stmts:
                assert not isinstance(stmt, exp.Command), (
                    f"SQL recipe block #{idx} fell back to unparsed Command AST: {stmt}"
                )
        else:
            patterns = [
                part.strip()
                for part in re.split(r"(?m)^--\s*Pattern\s+[A-Z]:.*$", block)
                if part.strip()
            ]
            assert patterns, f"Predicate block #{idx} had no testable SQL patterns"
            for p_idx, pat in enumerate(patterns, start=1):
                wrapped = f"SELECT (\n{pat}\n) AS passes_check"
                parsed = sqlglot.parse_one(wrapped, read="bigquery")
                assert not isinstance(parsed, exp.Command), (
                    f"SQL recipe block #{idx} pattern #{p_idx} fell back to Command AST"
                )


def test_skill_eval_yaml_runs_and_passes_fail_closed() -> None:
    """Automated fail-closed execution of `skills/bq-eval/EVAL.yaml` against `SKILL.md` + recipes."""
    spec = _parse_skill_eval_yaml(EVAL_YAML_PATH.read_text(encoding="utf-8"))
    assert spec.fail_closed_on_zero_scored is True
    assert spec.min_scored_cases >= 1
    assert "EVAL-P10" in spec.principles

    skill_text = SKILL_MD_PATH.read_text(encoding="utf-8")
    recipes_text = RECIPES_PATH.read_text(encoding="utf-8")
    corpus = f"{skill_text}\n\n{recipes_text}"

    assert len(spec.cases) >= spec.min_scored_cases

    passed_cases = 0
    for case in spec.cases:
        assert f"## {case.workflow}" in skill_text, f"Missing {case.workflow} in SKILL.md"
        target_corpus = recipes_text if case.case_id == "define-4pt-rubric-merge" else corpus
        missing = [frag for frag in case.required_fragments if frag not in target_corpus]
        forbidden = [frag for frag in case.forbidden_fragments if frag in target_corpus]
        assert not missing, f"{case.case_id} missing fragments: {missing}"
        assert not forbidden, f"{case.case_id} matched forbidden fragments: {forbidden}"
        passed_cases += 1

    assert passed_cases >= spec.min_scored_cases
    assert (passed_cases / len(spec.cases)) >= spec.pass_threshold


def test_bq_dry_run_backref_comment_stripping_and_receipt_manifest(tmp_path: Path) -> None:
    """`bq_dry_run.py` survives `\\1` IDs, strips leading comments, and matches committed receipt."""
    tricky_project = r"proj-\1-g<0>"
    qualified = bq_dry_run.qualify_project_datasets(
        "SELECT * FROM `agent_eval.otel_spans`", tricky_project
    )
    assert qualified == r"SELECT * FROM `proj-\1-g<0>.agent_eval.otel_spans`"

    comment_ddl = (
        "-- banner comment with ; ignored\n"
        "# hash comment\n"
        "/* multi-line block\n"
        "   CREATE VIEW dummy */\n"
        "CREATE MODEL IF NOT EXISTS `agent_eval.m` REMOTE WITH CONNECTION `us.c` OPTIONS (endpoint = 'gemini-3.8-flash');\n"
        "-- only comment tail without SQL;\n"
    )
    stmts = bq_dry_run.split_sql_statements(comment_ddl)
    assert len(stmts) == 1
    assert stmts[0].startswith("CREATE MODEL IF NOT EXISTS")
    assert bq_dry_run.should_skip_dry_run(comment_ddl) is True
    assert bq_dry_run.should_skip_dry_run("-- only comment\n/* block */") is True
    assert bq_dry_run.should_skip_dry_run("-- header\nSELECT 1;") is False

    out_manifest = tmp_path / "offline_manifest.json"
    rc = bq_dry_run.main(
        [
            "--offline-manifest",
            str(out_manifest),
            "--project-id",
            r"safe-\1-proj",
            "--skip-generated",
        ]
    )
    assert rc == 0
    generated_payload = json.loads(out_manifest.read_text(encoding="utf-8"))
    assert generated_payload["project_id"] == r"safe-\1-proj"
    assert generated_payload["checked"] > 0
    assert generated_payload["skipped"] > 0
    assert generated_payload["generated_statements"] == []

    full_manifest = tmp_path / "full_manifest.json"
    rc_full = bq_dry_run.main(
        ["--offline-manifest", str(full_manifest), "--project-id", "showcase-project"]
    )
    assert rc_full == 0

    committed = json.loads(DRY_RUN_RECEIPT_PATH.read_text(encoding="utf-8"))
    expected = bq_dry_run.collect_dry_run_manifest(project_id="showcase-project")
    assert json.loads(full_manifest.read_text(encoding="utf-8")) == expected
    assert committed == expected
    assert committed["live_bq_dry_run_executed"] is False
    assert committed["checked"] == 74
    assert committed["skipped"] == 15


def test_generate_table_judge_sql_rule0_failure_mode() -> None:
    """`build_ai_generate_table_judge_sql` defaults to routine Rule-0-passing traces; opt-in includes rejects."""
    default_sql = build_ai_generate_table_judge_sql(project_id="test-proj")
    debug_sql = build_ai_generate_table_judge_sql(
        project_id="test-proj",
        include_rule0_failures=True,
    )

    assert "WHERE t.rule0_pass" in default_sql
    assert "NOT t.rule0_pass" not in default_sql
    assert "NOT t.rule0_pass" in debug_sql

    parsed = sqlglot.parse(_normalize_ai_tvfs(default_sql), read="bigquery")
    assert len(parsed) == 1
    assert parsed[0] is not None
    assert not isinstance(parsed[0], exp.Command)


def test_judge_sql_requires_payload_and_status() -> None:
    """Judge SQL inner-joins deduped non-empty payloads, checks status/score 1..4, and views ignore NULL verdicts (A-5)."""
    from datetime import UTC, datetime

    from bq_eval.cli.app import main as cli_main
    from tests.conftest import make_span
    from tests.sql_helpers import (
        ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
        _create_enterprise_sqlite_fixture,
        _install_enterprise_views,
        _new_sqlite_bq_conn,
        _transpile_bq_view_to_sqlite,
        insert_enterprise_sqlite_span,
    )

    sql = build_ai_generate_table_judge_sql(project_id="test-proj")
    assert "INNER JOIN deduped_payloads" in sql
    assert "LEFT JOIN" not in sql
    assert "COALESCE(status, '') = ''" in sql
    assert "ordinal_score BETWEEN 1 AND 4" in sql
    assert "usageMetadata.thoughtsTokenCount" in sql

    # CLI exposes --include-rule0-failures and defaults to routine mode
    assert cli_main(["judge-sql", "--project-id", "test-proj"]) == 0
    assert cli_main(["judge-sql", "--project-id", "test-proj", "--include-rule0-failures"]) == 0

    # Execute candidate_traces CTE + outer status/score filter on transpiled SQLite
    conn = _new_sqlite_bq_conn()
    _create_enterprise_sqlite_fixture(conn)
    _install_enterprise_views(conn)
    conn.execute(
        "CREATE TABLE trace_payloads (trace_id TEXT, user_prompt TEXT, retrieved_context TEXT, final_response TEXT)"
    )
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    for idx, (tid, r0) in enumerate(
        [("tr-ok-dup", True), ("tr-empty", True), ("tr-missing", True), ("tr-r0-fail", False)]
    ):
        insert_enterprise_sqlite_span(
            conn,
            make_span(
                tid,
                f"sp-a5-{idx}",
                app_id="a5-app",
                status_code="OK" if r0 else "ERROR",
                rule0_pass=r0,
                offset_s=idx,
                base_time=base,
            ),
        )
    conn.executemany(
        "INSERT INTO trace_payloads VALUES (?,?,?,?)",
        [
            ("tr-ok-dup", "hello", "ctx", "short"),
            ("tr-ok-dup", "hello", "ctx", "longer duplicate payload"),
            ("tr-empty", "   ", "", None),
            ("tr-r0-fail", "bad", "ctx", "resp"),
        ],
    )
    cand_sql = build_ai_generate_table_judge_sql(
        project_id="test-proj", canary_sample_pct=100
    ).split("SELECT\n  GENERATE_UUID()")[0]
    cand_query = "\n".join(
        (
            cand_sql.replace("`test-proj.agent_eval.", "`"),
            "SELECT trace_id, prompt FROM candidate_traces",
        )
    )
    cand_rows = conn.execute(_transpile_bq_view_to_sqlite(cand_query)).fetchall()
    assert [str(r["trace_id"]) for r in cand_rows] == ["tr-ok-dup"]
    assert "longer duplicate payload" in str(cand_rows[0]["prompt"])

    # Verify outer filter drops failed status and out-of-range ordinal_score (9, 0, NULL)
    conn.execute(
        "CREATE TABLE mock_ai_out (trace_id TEXT, status TEXT, ordinal_score INTEGER, "
        "full_response TEXT, prompt TEXT, reasoning_trace TEXT)"
    )
    conn.executemany(
        "INSERT INTO mock_ai_out VALUES (?,?,?,?,?,?)",
        [
            ("tr-good", "", 4, '{"usageMetadata":{"thoughtsTokenCount":128}}', "abcd", "ok"),
            ("tr-err", "RESOURCE_EXHAUSTED", 4, "{}", "abcd", "err"),
            ("tr-hi", "", 9, "{}", "abcd", "bad range"),
            ("tr-null", "", None, "{}", "abcd", "null score"),
        ],
    )
    filtered = conn.execute(
        _transpile_bq_view_to_sqlite(
            "SELECT trace_id, COALESCE(SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.thoughtsTokenCount') AS INT64), 0) AS tk "
            "FROM mock_ai_out WHERE COALESCE(status, '') = '' AND ordinal_score BETWEEN 1 AND 4"
        )
    ).fetchall()
    assert [(str(r["trace_id"]), int(r["tk"])) for r in filtered] == [("tr-good", 128)]

    # NULL verdicts in eval_results must NOT count as judged failures in v_agent_eval_and_finops_summary
    for i in range(10):
        insert_enterprise_sqlite_span(
            conn,
            make_span(
                f"tr-nv-{i:02d}", f"sp-nv-{i:02d}", app_id="nv-app", offset_s=20 + i, base_time=base
            ),
        )
        conn.execute(
            ENTERPRISE_EVAL_RESULTS_BASE_INSERT,
            (
                f"ev-nv-{i:02d}",
                "run-nv",
                f"tr-nv-{i:02d}",
                None,
                "nv-app",
                "groundedness_4pt_v1",
                "gemini-3.8-flash",
                "CANARY_5PCT",
                1,
                "ok" if i < 4 else "failed",
                4 if i < 4 else None,
                1 if i < 4 else None,
                100,
                20,
                0,
                "2026-11-21T12:10:00+00:00",
            ),
        )
    nv_row = conn.execute(
        "SELECT judged_samples, raw_judge_pass_rate FROM v_agent_eval_and_finops_summary "
        "WHERE apphub_application_id = 'nv-app'"
    ).fetchone()
    conn.close()
    assert int(nv_row["judged_samples"]) == 4
    assert float(nv_row["raw_judge_pass_rate"]) == 1.0
