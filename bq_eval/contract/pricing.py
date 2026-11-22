"""Single canonical model pricing SKU catalog and version-suffix normalizer (`F12`)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from bq_eval.contract.otel import LONG_CONTEXT_THRESHOLD_TOKENS
from bq_eval.contract.types import NonNegFloat

_MODEL_VERSION_SUFFIX_RE = re.compile(r"-(?:\d{3}|ga|rc|qcd|preview)$")

CATALOG_EFFECTIVE_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
CATALOG_EFFECTIVE_END = datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC)


class ModelPricingSKU(BaseModel):
    """Effective-dated model pricing and GSU serving-physics burndown entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str
    effective_start: datetime
    effective_end: datetime
    input_usd_per_1m: NonNegFloat
    cached_input_usd_per_1m: NonNegFloat
    output_usd_per_1m: NonNegFloat
    long_context_threshold_tokens: int = LONG_CONTEXT_THRESHOLD_TOKENS
    long_context_input_usd_per_1m: NonNegFloat
    long_context_cached_usd_per_1m: NonNegFloat
    long_context_output_usd_per_1m: NonNegFloat


DEFAULT_MODEL_SKUS: dict[str, ModelPricingSKU] = {
    "gemini-3.5-flash-lite": ModelPricingSKU(
        model_name="gemini-3.5-flash-lite",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=0.075,
        cached_input_usd_per_1m=0.0075,
        output_usd_per_1m=0.30,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=0.15,
        long_context_cached_usd_per_1m=0.015,
        long_context_output_usd_per_1m=1.125,
    ),
    "gemini-3.8-flash": ModelPricingSKU(
        model_name="gemini-3.8-flash",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=0.15,
        cached_input_usd_per_1m=0.015,
        output_usd_per_1m=0.60,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=0.30,
        long_context_cached_usd_per_1m=0.030,
        long_context_output_usd_per_1m=2.25,
    ),
    "gemini-4-argon": ModelPricingSKU(
        model_name="gemini-4-argon",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=1.25,
        cached_input_usd_per_1m=0.125,
        output_usd_per_1m=5.00,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=2.50,
        long_context_cached_usd_per_1m=0.250,
        long_context_output_usd_per_1m=18.75,
    ),
    "ai_evaluate_agent_managed": ModelPricingSKU(
        model_name="ai_evaluate_agent_managed",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=0.15,
        cached_input_usd_per_1m=0.015,
        output_usd_per_1m=0.60,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=0.30,
        long_context_cached_usd_per_1m=0.030,
        long_context_output_usd_per_1m=2.25,
    ),
}

JUDGE_MODEL_ENDPOINTS: dict[str, str] = {
    "gemini-3.8-flash": "gemini_3_8_flash_judge",
    "gemini-4-argon": "gemini_4_argon_judge",
}

MANAGED_EVALUATE_AGENT_METRICS: tuple[str, ...] = (
    "tool_use_quality_v1",
    "final_response_quality_v1",
    "hallucination_v1",
    "safety_v1",
    "multi_turn_task_success_v1",
    "multi_turn_tool_use_quality_v1",
    "multi_turn_trajectory_quality_v1",
)


def normalize_model_name(model_name: str) -> str:
    """Strip release/version suffixes (`-001`, `-ga`, `-rc`, `-qcd`, `-preview`) before pricing lookup."""
    return _MODEL_VERSION_SUFFIX_RE.sub("", model_name.strip())
