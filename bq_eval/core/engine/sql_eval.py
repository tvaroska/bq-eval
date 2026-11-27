"""Gold BigQuery SQL Evaluation Builders (`AI.GENERATE_TABLE` & Preview `AI.EVALUATE_AGENT`) on Silver."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from bq_eval.core.contract.pricing import (
    JUDGE_MODEL_ENDPOINTS,
    MANAGED_EVALUATE_AGENT_METRICS,
)
from bq_eval.core.contract.rubrics import CANONICAL_RUBRICS, build_autorater_system_prompt

__all__ = [
    "build_ai_evaluate_agent_sql",
    "build_ai_generate_table_judge_sql",
    "escape_sql_xml_expr",
]

_COLUMN_EXPR_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$")


def escape_sql_xml_expr(column_expr: str) -> str:
    """Return BigQuery SQL expression that XML-escapes `&`, `<`, and `>` in untrusted columns."""
    cleaned = column_expr.strip()
    if not cleaned or not _COLUMN_EXPR_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid column_expr {column_expr!r}: must be a simple or dotted SQL identifier."
        )
    return (
        f"REPLACE(REPLACE(REPLACE(COALESCE({cleaned}, ''), "
        "'&', '&amp;'), '<', '&lt;'), '>', '&gt;')"
    )


def _validate_sql_identifier(value: str, field_name: str, *, allow_hyphen: bool = False) -> str:
    cleaned = value.strip()
    pattern = r"[a-zA-Z0-9_-]+" if allow_hyphen else r"[a-zA-Z0-9_]+"
    if not cleaned or not re.fullmatch(pattern, cleaned):
        raise ValueError(f"Invalid {field_name} {value!r}: must match ^{pattern}$")
    return cleaned


def _validate_run_id(run_id: str | None) -> str | None:
    if run_id is None:
        return None
    cleaned = run_id.strip()
    if not cleaned:
        return None
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", cleaned):
        raise ValueError(f"Invalid run_id {run_id!r}: must match ^[a-zA-Z0-9_.-]+$")
    return cleaned


def _escape_googlesql_string_literal(text: str) -> str:
    """Escape backslashes, single quotes, and newlines for a single-line GoogleSQL literal."""
    return (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _validate_policy_sha256(policy_sha256: str | None) -> str | None:
    if policy_sha256 is None:
        return None
    cleaned = policy_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        raise ValueError(
            f"Invalid policy_sha256 {policy_sha256!r}: must be 64 lowercase hex chars."
        )
    return cleaned


def _validate_amended_clause_ids(clause_ids: Sequence[str] | None) -> str | None:
    if not clause_ids:
        return None
    validated: list[str] = []
    for cid in clause_ids:
        cleaned = cid.strip()
        if not cleaned or not re.fullmatch(r"[a-zA-Z0-9_.:-]+", cleaned):
            raise ValueError(f"Invalid amended clause ID {cid!r}.")
        validated.append(cleaned)
    return ",".join(validated)


def _validate_canary_pct(canary_sample_pct: int) -> None:
    if (
        isinstance(canary_sample_pct, bool)
        or not isinstance(canary_sample_pct, int)
        or not (1 <= canary_sample_pct <= 100)
    ):
        raise ValueError("canary_sample_pct must be an integer between 1 and 100 (not bool).")


def build_ai_generate_table_judge_sql(  # noqa: PLR0913
    project_id: str,
    dataset_id: str = "agent_eval",
    rubric_id: str = "groundedness_4pt_v1",
    canary_sample_pct: int = 5,
    run_id: str | None = None,
    judge_model: str | None = None,
    include_rule0_failures: bool = False,
    policy_sha256: str | None = None,
    amended_clause_ids: Sequence[str] | None = None,
    *,
    silver_restricted_dataset: str = "silver_restricted",
    silver_ref_dataset: str = "silver_ref",
    seed: int = 42,
    canary_salt: str | None = None,
    judge_model_snapshot_id: str | None = None,
    rubric_version: str = "1.0",
    include_healthy_slow_in_tail: bool = True,
) -> str:
    """Build deterministic Gold `AI.GENERATE_TABLE` SQL query over Silver `trace_payloads`."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_dataset = _validate_sql_identifier(dataset_id, "dataset_id")
    valid_restricted = _validate_sql_identifier(
        silver_restricted_dataset, "silver_restricted_dataset"
    )
    valid_ref = _validate_sql_identifier(silver_ref_dataset, "silver_ref_dataset")
    valid_policy_sha256 = _validate_policy_sha256(policy_sha256)
    valid_clauses_csv = _validate_amended_clause_ids(amended_clause_ids)
    if rubric_id not in CANONICAL_RUBRICS:
        if rubric_id == "rfp_quote_accuracy_4pt_v1":
            raise ValueError(
                "Unknown rubric_id: 'rfp_quote_accuracy_4pt_v1' "
                "(renamed to 'rfp_quote_completeness_4pt_v1')."
            )
        raise ValueError(f"Unknown rubric_id: {rubric_id}")
    _validate_canary_pct(canary_sample_pct)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer (not bool).")
    valid_run_id = _validate_run_id(run_id)
    valid_canary_salt = _validate_run_id(canary_salt) or valid_run_id or f"seed-{seed}"
    valid_rubric_ver = _validate_run_id(rubric_version) or "1.0"

    rubric = CANONICAL_RUBRICS[rubric_id]
    selected_model = judge_model or rubric.judge_model
    if selected_model not in JUDGE_MODEL_ENDPOINTS:
        raise ValueError(
            f"Unsupported judge_model {selected_model!r}: expected one of {sorted(JUDGE_MODEL_ENDPOINTS)}"
        )
    remote_model = JUDGE_MODEL_ENDPOINTS[selected_model]
    snapshot_id = _validate_run_id(judge_model_snapshot_id) or f"{selected_model}@2026-11"
    effective_rubric = (
        rubric
        if selected_model == rubric.judge_model
        else rubric.model_copy(update={"judge_model": selected_model})
    )
    raw_system_prompt = build_autorater_system_prompt(effective_rubric)
    prompt_sha256 = hashlib.sha256(raw_system_prompt.encode("utf-8")).hexdigest()
    system_prompt = _escape_googlesql_string_literal(raw_system_prompt)

    escaped_prompt = escape_sql_xml_expr("p.user_prompt")
    escaped_context = escape_sql_xml_expr("p.retrieved_context")
    escaped_step_outputs = escape_sql_xml_expr("p.step_outputs")
    escaped_response = escape_sql_xml_expr("p.final_response")

    run_filter = (
        f"\n    AND t.outcome_id IN (\n"  # noqa: S608
        f"      SELECT outcome_id\n"
        f"      FROM `{valid_project}.{valid_dataset}.fleet_case_outcomes`\n"
        f"      WHERE run_id = '{valid_run_id}' AND outcome_id IS NOT NULL\n"
        f"    )"
        if valid_run_id
        else ""
    )
    eval_run_col = f"  '{valid_run_id}' AS eval_run_id,\n" if valid_run_id else ""
    policy_sha_expr = f"'{valid_policy_sha256}'" if valid_policy_sha256 else "CAST(NULL AS STRING)"
    amended_expr = f"'{valid_clauses_csv}'" if valid_clauses_csv else "CAST(NULL AS STRING)"
    loop_or_error_tail_expr = (
        "t.tool_error_count > 0 OR t.http_429_count > 0 "
        "OR t.escalated_to_high_reasoning OR t.repeated_tool_call_count > 0 "
        "OR t.silent_empty_rag_count > 0"
    )
    tail_expr = (
        f"({loop_or_error_tail_expr} OR t.trace_latency_ms > t.sla_latency_ms)"
        if include_healthy_slow_in_tail
        else loop_or_error_tail_expr
    )
    canary_expr = (
        f"MOD(ABS(FARM_FINGERPRINT(CONCAT('{valid_canary_salt}:', t.trace_id))), 100) < "
        f"{canary_sample_pct}"
    )
    if include_rule0_failures:
        stratum_condition = f"NOT t.rule0_pass OR {tail_expr}"
        where_clause = (
            "  WHERE t.content_missing_call_count = 0\n"
            "    AND NOT COALESCE(p.missing_session_context, FALSE)\n"
            "    AND zdr.zdr_permitted\n"
            "    AND (\n"
            f"      NOT t.rule0_pass\n      OR {tail_expr}\n"
            f"      OR {canary_expr}\n"
            f"    ){run_filter}"
        )
    else:
        stratum_condition = tail_expr
        where_clause = (
            "  WHERE t.rule0_pass\n"
            "    AND t.content_missing_call_count = 0\n"
            "    AND NOT COALESCE(p.missing_session_context, FALSE)\n"
            "    AND zdr.zdr_permitted\n"
            "    AND (\n"
            f"      {tail_expr}\n"
            f"      OR {canary_expr}\n"
            f"    ){run_filter}"
        )

    return f"""-- Gold Stratified AI.GENERATE_TABLE Judge Execution ({rubric.rubric_id})
WITH zdr_gate AS (
  SELECT
    IF(
      COUNT(*) = 0,
      TRUE,
      LOGICAL_OR(is_allowed AND zdr_allowed AND invokable_status = 'active')
    ) AS zdr_permitted
  FROM `{valid_project}.{valid_ref}.allowed_models`
  WHERE model = '{selected_model}'
    AND effective_to IS NULL
),
silver_payloads AS (
  SELECT
    tp.trace_id,
    COALESCE(
      JSON_VALUE(tp.turn_prompt, '$.text'),
      NULLIF(TO_JSON_STRING(tp.turn_prompt), 'null'),
      ''
    ) AS user_prompt,
    COALESCE(NULLIF(TO_JSON_STRING(tp.turn_context), 'null'), '') AS retrieved_context,
    COALESCE(NULLIF(TO_JSON_STRING(tp.step_outputs), 'null'), '') AS step_outputs,
    COALESCE(
      JSON_VALUE(tp.turn_output, '$.text'),
      NULLIF(TO_JSON_STRING(tp.turn_output), 'null'),
      ''
    ) AS final_response,
    (tp.previous_interaction_id IS NOT NULL AND tp.prior_history IS NULL) AS missing_session_context
  FROM `{valid_project}.{valid_restricted}.trace_payloads`(
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 720 HOUR),
    CURRENT_TIMESTAMP()
  ) AS tp
  WHERE COALESCE(tp.turn_prompt_read_whole, TRUE)
    AND COALESCE(tp.turn_context_read_whole, TRUE)
    AND COALESCE(tp.turn_output_read_whole, TRUE)
    AND COALESCE(tp.step_outputs_read_whole, TRUE)
),
deduped_payloads AS (
  SELECT *
  FROM silver_payloads
  WHERE TRIM(CONCAT(user_prompt, retrieved_context, step_outputs, final_response)) != ''
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY trace_id
    ORDER BY LENGTH(final_response) DESC, LENGTH(user_prompt) DESC
  ) = 1
),
candidate_traces AS (
  SELECT
    t.trace_id,
    t.outcome_id,
    t.apphub_application_id,
    t.rule0_pass,
    t.skill_sha256,
    t.protected_block_sha256,
    t.tail_class,
    t.tail_root_cause,
    CASE
      WHEN {stratum_condition}
        THEN 'ERROR_OR_SLA_TAIL'
      ELSE 'CANARY_{canary_sample_pct}PCT'
    END AS sampling_stratum,
    CONCAT(
      '{system_prompt}\\n\\n',
      'User Prompt:\\n<untrusted_text>\\n', {escaped_prompt}, '\\n</untrusted_text>\\n\\n',
      'Retrieved Context:\\n<untrusted_text>\\n', {escaped_context}, '\\n</untrusted_text>\\n\\n',
      IF(
        TRIM(COALESCE(p.step_outputs, '')) = '',
        '',
        CONCAT(
          'Step Outputs & Tool Trajectory:\\n<untrusted_text>\\n',
          {escaped_step_outputs},
          '\\n</untrusted_text>\\n\\n'
        )
      ),
      'Agent Final Response:\\n<untrusted_text>\\n', {escaped_response}, '\\n</untrusted_text>'
    ) AS prompt
  FROM `{valid_project}.{valid_dataset}.v_trace_sql_evaluation` AS t
  INNER JOIN deduped_payloads AS p
    ON t.trace_id = p.trace_id
  CROSS JOIN zdr_gate AS zdr
{where_clause}
)
SELECT
  GENERATE_UUID() AS eval_id,
{eval_run_col}  trace_id,
  outcome_id,
  apphub_application_id,
  '{rubric.rubric_id}' AS rubric_id,
  '{valid_rubric_ver}' AS rubric_version,
  '{selected_model}' AS judge_model,
  '{snapshot_id}' AS judge_model_snapshot_id,
  '{prompt_sha256}' AS judge_prompt_sha256,
  {seed} AS seed,
  sampling_stratum,
  rule0_pass,
  reasoning_trace,
  ordinal_score,
  (ordinal_score >= {rubric.passing_score}) AS binary_pass,
  'SCORED' AS judge_status,
  COALESCE(
    SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.promptTokenCount') AS INT64),
    DIV(LENGTH(prompt), 4)
  ) AS judge_input_tokens,
  COALESCE(
    SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.candidatesTokenCount') AS INT64),
    DIV(LENGTH(COALESCE(reasoning_trace, '')), 4) + 12
  ) AS judge_output_tokens,
  COALESCE(
    SAFE_CAST(JSON_VALUE(full_response, '$.usageMetadata.thoughtsTokenCount') AS INT64),
    0
  ) AS judge_thinking_tokens,
  {policy_sha_expr} AS policy_sha256,
  {amended_expr} AS amended_clause_ids,
  FALSE AS is_synthetic,
  CURRENT_TIMESTAMP() AS evaluated_at
FROM AI.GENERATE_TABLE(
  MODEL `{valid_project}.{valid_dataset}.{remote_model}`,
  TABLE candidate_traces,
  STRUCT(
    'reasoning_trace STRING NOT NULL, ordinal_score INT64 NOT NULL' AS output_schema,
    '{{"generation_config": {{"temperature": 0.0, "seed": {seed}, "max_output_tokens": 2048, "thinking_config": {{"thinking_level": "LOW"}}}}}}' AS model_params
  )
)
WHERE COALESCE(status, '') = ''
  AND ordinal_score BETWEEN 1 AND 4;
"""  # noqa: S608


def build_ai_evaluate_agent_sql(  # noqa: PLR0913
    project_id: str,
    dataset_id: str = "agent_eval",
    eval_view: str = "v_eval_agent_turns",
    managed_metrics: Sequence[str] = (
        "tool_use_quality_v1",
        "final_response_quality_v1",
        "hallucination_v1",
        "safety_v1",
    ),
    canary_sample_pct: int = 10,
    run_id: str | None = None,
    persist_run_id: str | None = None,
    canary_salt: str | None = None,
) -> str:
    """Build Preview `AI.EVALUATE_AGENT` SQL query over Silver-backed `v_eval_agent_turns`."""
    valid_project = _validate_sql_identifier(project_id, "project_id", allow_hyphen=True)
    valid_dataset = _validate_sql_identifier(dataset_id, "dataset_id")
    valid_view = _validate_sql_identifier(eval_view, "eval_view")
    if not managed_metrics:
        raise ValueError("At least one managed metric is required.")
    unknown = [m for m in managed_metrics if m not in MANAGED_EVALUATE_AGENT_METRICS]
    if unknown:
        raise ValueError(f"Unknown AI.EVALUATE_AGENT managed metric(s): {unknown}")
    _validate_canary_pct(canary_sample_pct)
    valid_run_id = _validate_run_id(run_id)
    valid_persist_run_id = _validate_run_id(persist_run_id)
    valid_canary_salt = (
        _validate_run_id(canary_salt) or valid_persist_run_id or valid_run_id or "default"
    )

    metrics_sql = ",\n      ".join(f"'{m}'" for m in managed_metrics)

    def _build_predicates(trace_col: str) -> list[str]:
        preds: list[str] = []
        if canary_sample_pct < 100:
            preds.append(
                f"MOD(ABS(FARM_FINGERPRINT(CONCAT('{valid_canary_salt}:', {trace_col}))), 100) < "
                f"{canary_sample_pct}"
            )
        if valid_run_id:
            preds.append(
                f"{trace_col} IN ("  # noqa: S608
                f"SELECT trace_id FROM `{valid_project}.{valid_dataset}.fleet_case_outcomes` "
                f"WHERE run_id = '{valid_run_id}' AND trace_id IS NOT NULL)"
            )
        return preds

    predicates = _build_predicates("trace_id")
    sample_clause = f"\n    WHERE {' AND '.join(predicates)}" if predicates else ""

    if valid_persist_run_id is not None:
        cand_preds = _build_predicates("cand.trace_id")
        cand_where = f"WHERE {' AND '.join(cand_preds)} AND " if cand_preds else "WHERE "
        return f"""-- Preview AI.EVALUATE_AGENT Persistence over Silver `v_eval_agent_turns` ({canary_sample_pct}% Sample)
INSERT INTO `{valid_project}.{valid_dataset}.eval_results` (
  eval_id, eval_run_id, trace_id, outcome_id, apphub_application_id, rubric_id, judge_model,
  sampling_stratum, rule0_pass, reasoning_trace, ordinal_score, binary_pass, judge_status,
  judge_input_tokens, judge_output_tokens, judge_thinking_tokens, is_synthetic, evaluated_at
)
WITH evaluated AS (
  SELECT
    eval_out.trace_id,
    eval_out.outcome_id,
    eval_out.apphub_application_id,
    metric_row.metric_name AS rubric_id,
    eval_out.step1_pass AS rule0_pass,
    COALESCE(metric_row.explanation, 'Managed AI.EVALUATE_AGENT preview verdict') AS reasoning_trace,
    CAST(ROUND(LEAST(4.0, GREATEST(1.0, metric_row.score * 3.0 + 1.0))) AS INT64) AS ordinal_score,
    DIV(CHAR_LENGTH(TO_JSON_STRING(eval_out.trace)), 4) AS judge_input_tokens,
    DIV(CHAR_LENGTH(COALESCE(metric_row.explanation, '')), 4) AS judge_output_tokens
  FROM AI.EVALUATE_AGENT(
    TABLE (
      SELECT cand.*
      FROM `{valid_project}.{valid_dataset}.{valid_view}` AS cand
      LEFT JOIN (
        SELECT trace_id, COUNT(DISTINCT rubric_id) AS scored_metrics
        FROM `{valid_project}.{valid_dataset}.eval_results`
        WHERE eval_run_id = '{valid_persist_run_id}'
          AND rubric_id IN ({metrics_sql})
        GROUP BY trace_id
      ) AS completed
        ON cand.trace_id = completed.trace_id
      {cand_where}COALESCE(completed.scored_metrics, 0) < {len(managed_metrics)}
    ),
    trace_col => 'trace',
    managed_metrics => [
      {metrics_sql}
    ]
  ) AS eval_out,
  UNNEST(eval_out.evaluation_results) AS metric_row
  LEFT JOIN `{valid_project}.{valid_dataset}.eval_results` AS existing
    ON eval_out.trace_id = existing.trace_id
    AND metric_row.metric_name = existing.rubric_id
    AND existing.eval_run_id = '{valid_persist_run_id}'
  WHERE metric_row.score IS NOT NULL
    AND existing.eval_id IS NULL
)
SELECT
  GENERATE_UUID() AS eval_id,
  '{valid_persist_run_id}' AS eval_run_id,
  trace_id,
  outcome_id,
  apphub_application_id,
  rubric_id,
  'ai_evaluate_agent_managed' AS judge_model,
  'SAMPLE_{canary_sample_pct}PCT' AS sampling_stratum,
  rule0_pass,
  reasoning_trace,
  ordinal_score,
  (ordinal_score >= 3) AS binary_pass,
  'SCORED' AS judge_status,
  judge_input_tokens,
  judge_output_tokens,
  0 AS judge_thinking_tokens,
  FALSE AS is_synthetic,
  CURRENT_TIMESTAMP() AS evaluated_at
FROM evaluated;
"""  # noqa: S608

    return f"""-- Preview AI.EVALUATE_AGENT Execution over Silver `v_eval_agent_turns` ({canary_sample_pct}% Sample)
WITH candidate_turns AS (
  SELECT *
  FROM `{valid_project}.{valid_dataset}.{valid_view}`{sample_clause}
)
SELECT *
FROM AI.EVALUATE_AGENT(
  TABLE candidate_turns,
  trace_col => 'trace',
  managed_metrics => [
      {metrics_sql}
  ]
);
"""  # noqa: S608
