"""Empirical proof validation, INFORMATION_SCHEMA.JOBS cost attribution & wage sensitivity."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from bq_eval.contract.governance import BqQueryCostRow, WageSensitivityPoint
from bq_eval.contract.otel import LONG_CONTEXT_THRESHOLD_TOKENS, OTelGenAISpanRecord
from bq_eval.contract.pricing import normalize_model_name
from bq_eval.contract.results import ConfusionMatrix
from bq_eval.engine.calibration import compute_gwet_ac2
from bq_eval.engine.finops import compute_span_cost_usd

BYTES_PER_TIB: float = float(1024**4)
MS_PER_HOUR: float = 3_600_000.0
BQ_ON_DEMAND_USD_PER_TIB: float = 6.25
BQ_SLOT_HOUR_USD: float = 0.04

_FORBIDDEN_HUMAN_RATER_RE = re.compile(
    r"(?:llm|gemini|argon|flash|claude|gpt|synthetic|autorater|judge|bot)",
    re.IGNORECASE,
)
_REGION_RE = re.compile(r"^[a-z0-9-]{2,32}$")
_IDENT_RE = re.compile(r"^[a-zA-Z0-9_-]{2,64}$")


@dataclass(frozen=True)
class BlindHumanLabelRow:
    """Single blind human SME annotation row for dual-rater calibration."""

    trace_id: str
    rater_id: str
    human_ordinal_score: int
    judge_ordinal_score: int
    blind_to_judge: bool = True
    passing_score: int = 3


@dataclass(frozen=True)
class DualHumanCalibrationReport:
    """Validated dual-human blind SME calibration summary (`N >= 100`, `>= 2` raters/case)."""

    trace_count: int
    distinct_raters: int
    human_vs_human_ac2: float
    human_vs_llm_ac2: float
    confusion_matrix: ConfusionMatrix
    human_pass_rate: float
    judge_pass_rate: float


@dataclass(frozen=True)
class ContextCliffArmReport:
    """Unconfounded single-model `<=200K` vs. `>200K` benchmark comparison."""

    model_name: str
    short_arm_count: int
    long_arm_count: int
    short_mean_cost_usd: float
    long_mean_cost_usd: float
    short_mean_burndown_tokens: float
    long_mean_burndown_tokens: float
    cost_multiplier: float
    burndown_multiplier: float


def compute_bq_query_cost_usd(
    *,
    total_bytes_billed: int = 0,
    total_slot_ms: int = 0,
    pricing_mode: Literal["on_demand", "capacity_slot"] = "on_demand",
    on_demand_usd_per_tib: float = BQ_ON_DEMAND_USD_PER_TIB,
    slot_hour_usd: float = BQ_SLOT_HOUR_USD,
) -> float:
    """Compute BigQuery `INFORMATION_SCHEMA.JOBS` USD cost from bytes billed or slot-ms."""
    if total_bytes_billed < 0 or total_slot_ms < 0:
        raise ValueError("total_bytes_billed and total_slot_ms must be non-negative.")
    if pricing_mode == "capacity_slot":
        return (total_slot_ms / MS_PER_HOUR) * slot_hour_usd
    return (total_bytes_billed / BYTES_PER_TIB) * on_demand_usd_per_tib


def build_bq_query_cost_row(  # noqa: PLR0913
    *,
    job_id: str,
    apphub_application_id: str,
    total_bytes_billed: int = 0,
    total_slot_ms: int = 0,
    pricing_mode: Literal["on_demand", "capacity_slot"] = "on_demand",
    outcome_id: str | None = None,
) -> BqQueryCostRow:
    """Construct a typed `BqQueryCostRow` from raw `INFORMATION_SCHEMA.JOBS` counters."""
    return BqQueryCostRow(
        job_id=job_id,
        apphub_application_id=apphub_application_id,
        outcome_id=outcome_id,
        total_bytes_billed=total_bytes_billed,
        total_slot_ms=total_slot_ms,
        pricing_mode=pricing_mode,
        query_cost_usd=compute_bq_query_cost_usd(
            total_bytes_billed=total_bytes_billed,
            total_slot_ms=total_slot_ms,
            pricing_mode=pricing_mode,
        ),
    )


def build_information_schema_jobs_sync_sql(
    *,
    project_id: str,
    dataset_id: str = "agent_eval",
    region: str = "us",
    lookback_days: int = 7,
) -> str:
    """Emit idempotent `MERGE` SQL folding `INFORMATION_SCHEMA.JOBS_BY_PROJECT` cost into `bq_query_jobs`."""
    if not _IDENT_RE.fullmatch(project_id.strip()):
        raise ValueError(f"Invalid project_id: {project_id!r}")
    if not _IDENT_RE.fullmatch(dataset_id.strip()):
        raise ValueError(f"Invalid dataset_id: {dataset_id!r}")
    norm_region = region.strip().lower()
    if not _REGION_RE.fullmatch(norm_region):
        raise ValueError(f"Invalid BigQuery region: {region!r}")
    if lookback_days < 1 or lookback_days > 180:
        raise ValueError("lookback_days must be between 1 and 180.")

    p = project_id.strip()
    d = dataset_id.strip()
    return (
        f"MERGE INTO `{p}.{d}.bq_query_jobs` AS target\n"  # noqa: S608
        f"USING (\n"
        f"  SELECT\n"
        f"    job_id,\n"
        f"    COALESCE(\n"
        f"      (SELECT value FROM UNNEST(labels) WHERE key = 'apphub_application_id' LIMIT 1),\n"
        f"      'unregistered_app'\n"
        f"    ) AS apphub_application_id,\n"
        f"    (SELECT value FROM UNNEST(labels) WHERE key = 'outcome_id' LIMIT 1) AS outcome_id,\n"
        f"    COALESCE(total_bytes_billed, 0) AS total_bytes_billed,\n"
        f"    COALESCE(total_slot_ms, 0) AS total_slot_ms,\n"
        f"    IF(reservation_id IS NOT NULL, 'capacity_slot', 'on_demand') AS pricing_mode,\n"
        f"    IF(\n"
        f"      reservation_id IS NOT NULL,\n"
        f"      (COALESCE(total_slot_ms, 0) / 3600000.0) * {BQ_SLOT_HOUR_USD},\n"
        f"      (COALESCE(total_bytes_billed, 0) / 1099511627776.0) * {BQ_ON_DEMAND_USD_PER_TIB}\n"
        f"    ) AS query_cost_usd,\n"
        f"    creation_time AS created_at\n"
        f"  FROM `{p}.region-{norm_region}.INFORMATION_SCHEMA.JOBS_BY_PROJECT`\n"
        f"  WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {lookback_days} DAY)\n"
        f"    AND state = 'DONE'\n"
        f"    AND job_type = 'QUERY'\n"
        f"    AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'workload' AND value = 'bq-eval')\n"
        f") AS src\n"
        f"ON target.job_id = src.job_id\n"
        f"WHEN NOT MATCHED THEN\n"
        f"  INSERT (job_id, apphub_application_id, outcome_id, total_bytes_billed, total_slot_ms, pricing_mode, query_cost_usd, created_at)\n"
        f"  VALUES (src.job_id, src.apphub_application_id, src.outcome_id, src.total_bytes_billed, src.total_slot_ms, src.pricing_mode, src.query_cost_usd, src.created_at);\n"
    )


def _group_and_validate_blind_rows(
    rows: Sequence[BlindHumanLabelRow],
) -> tuple[dict[str, list[BlindHumanLabelRow]], set[str]]:
    by_trace: dict[str, list[BlindHumanLabelRow]] = {}
    all_raters: set[str] = set()
    for row in rows:
        if not row.blind_to_judge:
            raise ValueError(
                f"Trace {row.trace_id!r} rater {row.rater_id!r} was not blind to judge."
            )
        if _FORBIDDEN_HUMAN_RATER_RE.search(row.rater_id):
            raise ValueError(
                f"Rater ID {row.rater_id!r} looks non-human/synthetic; blind human SME IDs required."
            )
        if not (1 <= row.human_ordinal_score <= 4) or not (1 <= row.judge_ordinal_score <= 4):
            raise ValueError("Ordinal scores must be in 1..4.")
        by_trace.setdefault(row.trace_id, []).append(row)
        all_raters.add(row.rater_id)
    return by_trace, all_raters


def validate_blind_dual_human_labels(
    rows: Sequence[BlindHumanLabelRow],
    *,
    min_cases: int = 100,
    min_raters_per_case: int = 2,
    passing_score: int = 3,
) -> DualHumanCalibrationReport:
    """Validate blind dual-human SME calibration panels (`N >= 100`, `>= 2` human raters/case)."""
    if min_cases < 1 or min_raters_per_case < 2:
        raise ValueError("min_cases must be >= 1 and min_raters_per_case must be >= 2.")

    by_trace, all_raters = _group_and_validate_blind_rows(rows)
    if len(by_trace) < min_cases:
        raise ValueError(
            f"Dual-human SME panel has {len(by_trace)} traces; required >= {min_cases}."
        )

    h_h_pairs: list[tuple[int, int]] = []
    h_llm_pairs: list[tuple[int, int]] = []
    counts = {"tp": 0, "fn": 0, "tn": 0, "fp": 0}

    for trace_id, trace_rows in sorted(by_trace.items()):
        raters = {r.rater_id for r in trace_rows}
        if len(raters) < min_raters_per_case:
            raise ValueError(
                f"Trace {trace_id!r} has only {len(raters)} distinct blind human raters "
                f"(required >= {min_raters_per_case})."
            )
        ordered = sorted(trace_rows, key=lambda r: r.rater_id)
        h_h_pairs.append((ordered[0].human_ordinal_score, ordered[1].human_ordinal_score))

        # Conservative lower-half consensus on ties keeps regulated MRM fail-closed.
        consensus_score = sorted(r.human_ordinal_score for r in ordered)[(len(ordered) - 1) // 2]
        judge_score = ordered[0].judge_ordinal_score
        h_llm_pairs.append((consensus_score, judge_score))

        cell = (
            ("tp" if judge_score >= passing_score else "fn")
            if consensus_score >= passing_score
            else ("fp" if judge_score >= passing_score else "tn")
        )
        counts[cell] += 1

    total = len(by_trace)
    return DualHumanCalibrationReport(
        trace_count=total,
        distinct_raters=len(all_raters),
        human_vs_human_ac2=compute_gwet_ac2([a for a, _ in h_h_pairs], [b for _, b in h_h_pairs]),
        human_vs_llm_ac2=compute_gwet_ac2([a for a, _ in h_llm_pairs], [b for _, b in h_llm_pairs]),
        confusion_matrix=ConfusionMatrix(**counts),
        human_pass_rate=(counts["tp"] + counts["fn"]) / total,
        judge_pass_rate=(counts["tp"] + counts["fp"]) / total,
    )


def analyze_unconfounded_context_cliff_arms(
    short_arm_spans: Sequence[OTelGenAISpanRecord],
    long_arm_spans: Sequence[OTelGenAISpanRecord],
    *,
    min_per_arm: int = 30,
    threshold_tokens: int = LONG_CONTEXT_THRESHOLD_TOKENS,
) -> ContextCliffArmReport:
    """Verify unconfounded single-model `<=200K` vs. `>200K` benchmark arms."""
    if len(short_arm_spans) < min_per_arm or len(long_arm_spans) < min_per_arm:
        raise ValueError(
            f"Unconfounded cliff benchmark requires >= {min_per_arm} spans per arm "
            f"(got short={len(short_arm_spans)}, long={len(long_arm_spans)})."
        )

    models = {normalize_model_name(s.model_name) for s in (*short_arm_spans, *long_arm_spans)}
    if len(models) != 1:
        raise ValueError(
            f"Confounded arms: both arms must share a single normalized model, got {sorted(models)}."
        )
    if any(s.input_tokens > threshold_tokens for s in short_arm_spans):
        raise ValueError(f"short_arm_spans contains spans above {threshold_tokens} input tokens.")
    if any(s.input_tokens <= threshold_tokens for s in long_arm_spans):
        raise ValueError(
            f"long_arm_spans contains spans at or below {threshold_tokens} input tokens."
        )

    short_mean_cost = sum(compute_span_cost_usd(s) for s in short_arm_spans) / len(short_arm_spans)
    long_mean_cost = sum(compute_span_cost_usd(s) for s in long_arm_spans) / len(long_arm_spans)
    short_mean_bd = sum(s.weighted_burndown_tokens for s in short_arm_spans) / len(short_arm_spans)
    long_mean_bd = sum(s.weighted_burndown_tokens for s in long_arm_spans) / len(long_arm_spans)

    return ContextCliffArmReport(
        model_name=next(iter(models)),
        short_arm_count=len(short_arm_spans),
        long_arm_count=len(long_arm_spans),
        short_mean_cost_usd=short_mean_cost,
        long_mean_cost_usd=long_mean_cost,
        short_mean_burndown_tokens=short_mean_bd,
        long_mean_burndown_tokens=long_mean_bd,
        cost_multiplier=(long_mean_cost / short_mean_cost) if short_mean_cost > 0 else 0.0,
        burndown_multiplier=(long_mean_bd / short_mean_bd) if short_mean_bd > 0 else 0.0,
    )


def compute_escalation_wage_sensitivity(  # noqa: PLR0913
    *,
    verified_outcomes: int,
    agent_model_usd: float,
    judge_usd: float,
    bq_query_usd: float,
    escalated_cases: int,
    avg_review_minutes_per_escalation: float,
    hourly_wages_usd: Sequence[float] = (50.0, 100.0, 150.0, 250.0),
) -> tuple[WageSensitivityPoint, ...]:
    """Evaluate fully loaded CPVO across SME hourly wages with explicit agent/judge/BQ/human splits."""
    if verified_outcomes <= 0:
        raise ValueError("verified_outcomes must be positive.")
    for name, val in (
        ("agent_model_usd", agent_model_usd),
        ("judge_usd", judge_usd),
        ("bq_query_usd", bq_query_usd),
        ("avg_review_minutes_per_escalation", avg_review_minutes_per_escalation),
    ):
        if not math.isfinite(val) or val < 0.0:
            raise ValueError(f"{name} must be finite and non-negative.")
    if escalated_cases < 0:
        raise ValueError("escalated_cases must be non-negative.")

    ai_cpvo = agent_model_usd / verified_outcomes
    judge_cpvo = judge_usd / verified_outcomes
    bq_cpvo = bq_query_usd / verified_outcomes
    review_hours = (escalated_cases * avg_review_minutes_per_escalation) / 60.0

    points: list[WageSensitivityPoint] = []
    for wage in hourly_wages_usd:
        if not math.isfinite(wage) or wage < 0.0:
            raise ValueError(f"Invalid hourly wage: {wage!r}")
        human_cpvo = (review_hours * wage) / verified_outcomes
        points.append(
            WageSensitivityPoint(
                hourly_wage_usd=wage,
                ai_cost_per_verified_outcome_usd=ai_cpvo,
                judge_cost_per_verified_outcome_usd=judge_cpvo,
                bq_query_cost_per_verified_outcome_usd=bq_cpvo,
                human_escalation_cost_per_verified_outcome_usd=human_cpvo,
                fully_loaded_cpvo_usd=ai_cpvo + judge_cpvo + bq_cpvo + human_cpvo,
            )
        )
    return tuple(points)


def sum_bq_query_costs_by_app(rows: Sequence[BqQueryCostRow]) -> Mapping[str, float]:
    """Aggregate `BqQueryCostRow` USD totals by `apphub_application_id`."""
    totals: dict[str, float] = {}
    for row in rows:
        totals[row.apphub_application_id] = (
            totals.get(row.apphub_application_id, 0.0) + row.query_cost_usd
        )
    return totals
