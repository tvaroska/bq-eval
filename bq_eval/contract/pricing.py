"""Single canonical 1P & 3P model pricing SKU catalog and version-suffix normalizer."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bq_eval.contract.otel import LONG_CONTEXT_THRESHOLD_TOKENS
from bq_eval.contract.types import NonNegFloat, PosFloat

_MODEL_VERSION_SUFFIX_RE = re.compile(r"-(?:\d{3}|ga|rc|qcd|preview)$")

CATALOG_EFFECTIVE_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
CATALOG_EFFECTIVE_END = datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC)

ModelProvider = Literal["google", "anthropic", "self_hosted"]
ModelLifecycleTrack = Literal["long_term_ga", "short_term_ga", "preview", "legacy_sunset"]

PRIORITY_PAYGO_MULTIPLIER: float = 1.8
STANDARD_PAYGO_MULTIPLIER: float = 1.0
FLEX_PAYGO_MULTIPLIER: float = 0.5


class ModelPricingSKU(BaseModel):
    """Effective-dated 1P/3P model pricing, PayGo multipliers, and GSU burndown weights."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str
    provider: ModelProvider = "google"
    lifecycle_track: ModelLifecycleTrack = "long_term_ga"
    effective_start: datetime
    effective_end: datetime
    input_usd_per_1m: NonNegFloat
    cached_input_usd_per_1m: NonNegFloat
    cache_write_5m_usd_per_1m: NonNegFloat | None = None
    cache_write_1h_usd_per_1m: NonNegFloat | None = None
    output_usd_per_1m: NonNegFloat
    long_context_threshold_tokens: int | None = LONG_CONTEXT_THRESHOLD_TOKENS
    long_context_input_usd_per_1m: NonNegFloat
    long_context_cached_usd_per_1m: NonNegFloat
    long_context_output_usd_per_1m: NonNegFloat
    burndown_uncached_weight: PosFloat = 1.0
    burndown_cached_weight: NonNegFloat = 0.1
    burndown_cache_write_5m_weight: PosFloat = 1.0
    burndown_cache_write_1h_weight: PosFloat = 1.0
    burndown_output_weight: PosFloat = 4.0
    burndown_long_uncached_weight: PosFloat = 2.0
    burndown_long_cached_weight: NonNegFloat = 0.2
    burndown_long_output_weight: PosFloat = 7.5
    priority_paygo_multiplier: PosFloat = Field(default=PRIORITY_PAYGO_MULTIPLIER)
    flex_paygo_multiplier: PosFloat = Field(default=FLEX_PAYGO_MULTIPLIER)

    @property
    def effective_cache_write_5m_usd_per_1m(self) -> float:
        return (
            self.cache_write_5m_usd_per_1m
            if self.cache_write_5m_usd_per_1m is not None
            else self.input_usd_per_1m * self.burndown_cache_write_5m_weight
        )

    @property
    def effective_cache_write_1h_usd_per_1m(self) -> float:
        return (
            self.cache_write_1h_usd_per_1m
            if self.cache_write_1h_usd_per_1m is not None
            else self.input_usd_per_1m * self.burndown_cache_write_1h_weight
        )


DEFAULT_MODEL_SKUS: dict[str, ModelPricingSKU] = {
    "gemini-3.5-flash-lite": ModelPricingSKU(
        model_name="gemini-3.5-flash-lite",
        provider="google",
        lifecycle_track="long_term_ga",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=0.075,
        cached_input_usd_per_1m=0.0075,
        cache_write_5m_usd_per_1m=0.075,
        cache_write_1h_usd_per_1m=0.075,
        output_usd_per_1m=0.30,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=0.15,
        long_context_cached_usd_per_1m=0.015,
        long_context_output_usd_per_1m=1.125,
    ),
    "gemini-3.8-flash": ModelPricingSKU(
        model_name="gemini-3.8-flash",
        provider="google",
        lifecycle_track="long_term_ga",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=0.15,
        cached_input_usd_per_1m=0.015,
        cache_write_5m_usd_per_1m=0.15,
        cache_write_1h_usd_per_1m=0.15,
        output_usd_per_1m=0.60,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=0.30,
        long_context_cached_usd_per_1m=0.030,
        long_context_output_usd_per_1m=2.25,
    ),
    "gemini-4-argon": ModelPricingSKU(
        model_name="gemini-4-argon",
        provider="google",
        lifecycle_track="long_term_ga",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=1.25,
        cached_input_usd_per_1m=0.125,
        cache_write_5m_usd_per_1m=1.25,
        cache_write_1h_usd_per_1m=1.25,
        output_usd_per_1m=5.00,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=2.50,
        long_context_cached_usd_per_1m=0.250,
        long_context_output_usd_per_1m=18.75,
    ),
    "gemini-4-carbon": ModelPricingSKU(
        model_name="gemini-4-carbon",
        provider="google",
        lifecycle_track="short_term_ga",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=1.25,
        cached_input_usd_per_1m=0.125,
        cache_write_5m_usd_per_1m=1.25,
        cache_write_1h_usd_per_1m=1.25,
        output_usd_per_1m=5.00,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=2.50,
        long_context_cached_usd_per_1m=0.250,
        long_context_output_usd_per_1m=18.75,
    ),
    "ai_evaluate_agent_managed": ModelPricingSKU(
        model_name="ai_evaluate_agent_managed",
        provider="google",
        lifecycle_track="long_term_ga",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=0.15,
        cached_input_usd_per_1m=0.015,
        cache_write_5m_usd_per_1m=0.15,
        cache_write_1h_usd_per_1m=0.15,
        output_usd_per_1m=0.60,
        long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS,
        long_context_input_usd_per_1m=0.30,
        long_context_cached_usd_per_1m=0.030,
        long_context_output_usd_per_1m=2.25,
    ),
    "claude-sonnet-4-6": ModelPricingSKU(
        model_name="claude-sonnet-4-6",
        provider="anthropic",
        lifecycle_track="long_term_ga",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=3.00,
        cached_input_usd_per_1m=0.30,
        cache_write_5m_usd_per_1m=3.75,
        cache_write_1h_usd_per_1m=6.00,
        output_usd_per_1m=15.00,
        long_context_threshold_tokens=None,
        long_context_input_usd_per_1m=3.00,
        long_context_cached_usd_per_1m=0.30,
        long_context_output_usd_per_1m=15.00,
        burndown_uncached_weight=1.0,
        burndown_cached_weight=0.1,
        burndown_cache_write_5m_weight=1.25,
        burndown_cache_write_1h_weight=2.0,
        burndown_output_weight=5.0,
        burndown_long_uncached_weight=1.0,
        burndown_long_cached_weight=0.1,
        burndown_long_output_weight=5.0,
        priority_paygo_multiplier=PRIORITY_PAYGO_MULTIPLIER,
        flex_paygo_multiplier=FLEX_PAYGO_MULTIPLIER,
    ),
    "claude-opus-4-6": ModelPricingSKU(
        model_name="claude-opus-4-6",
        provider="anthropic",
        lifecycle_track="long_term_ga",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=15.00,
        cached_input_usd_per_1m=1.50,
        cache_write_5m_usd_per_1m=18.75,
        cache_write_1h_usd_per_1m=30.00,
        output_usd_per_1m=75.00,
        long_context_threshold_tokens=None,
        long_context_input_usd_per_1m=15.00,
        long_context_cached_usd_per_1m=1.50,
        long_context_output_usd_per_1m=75.00,
        burndown_uncached_weight=1.0,
        burndown_cached_weight=0.1,
        burndown_cache_write_5m_weight=1.25,
        burndown_cache_write_1h_weight=2.0,
        burndown_output_weight=5.0,
        burndown_long_uncached_weight=1.0,
        burndown_long_cached_weight=0.1,
        burndown_long_output_weight=5.0,
        priority_paygo_multiplier=PRIORITY_PAYGO_MULTIPLIER,
        flex_paygo_multiplier=FLEX_PAYGO_MULTIPLIER,
    ),
    "claude-sonnet-5-5": ModelPricingSKU(
        model_name="claude-sonnet-5-5",
        provider="anthropic",
        lifecycle_track="long_term_ga",
        effective_start=CATALOG_EFFECTIVE_START,
        effective_end=CATALOG_EFFECTIVE_END,
        input_usd_per_1m=3.00,
        cached_input_usd_per_1m=0.30,
        cache_write_5m_usd_per_1m=3.75,
        cache_write_1h_usd_per_1m=6.00,
        output_usd_per_1m=15.00,
        long_context_threshold_tokens=None,
        long_context_input_usd_per_1m=3.00,
        long_context_cached_usd_per_1m=0.30,
        long_context_output_usd_per_1m=15.00,
        burndown_uncached_weight=1.0,
        burndown_cached_weight=0.1,
        burndown_cache_write_5m_weight=1.25,
        burndown_cache_write_1h_weight=2.0,
        burndown_output_weight=5.0,
        burndown_long_uncached_weight=1.0,
        burndown_long_cached_weight=0.1,
        burndown_long_output_weight=5.0,
        priority_paygo_multiplier=PRIORITY_PAYGO_MULTIPLIER,
        flex_paygo_multiplier=FLEX_PAYGO_MULTIPLIER,
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
    """Strip publisher prefixes, `@region`, and release/version suffixes before pricing lookup."""
    cleaned = model_name.strip()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    return _MODEL_VERSION_SUFFIX_RE.sub("", cleaned)
