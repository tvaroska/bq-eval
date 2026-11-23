"""Per-span token burndown and USD cost, including the price step above 200K tokens."""

from __future__ import annotations

from bq_eval.core.contract.otel import (
    CACHED_INPUT_BURNDOWN_WEIGHT,
    LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT,
    LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT,
    UNCACHED_INPUT_BURNDOWN_WEIGHT,
    OTelGenAISpanRecord,
    ServingTrafficType,
    compute_weighted_burndown_tokens,
)
from bq_eval.core.contract.pricing import (
    DEFAULT_MODEL_SKUS,
    STANDARD_PAYGO_MULTIPLIER,
    ModelPricingSKU,
    normalize_model_name,
)

_STD_CACHE_BURNDOWN_DELTA: float = UNCACHED_INPUT_BURNDOWN_WEIGHT - CACHED_INPUT_BURNDOWN_WEIGHT


_LONG_CACHE_BURNDOWN_DELTA: float = (
    LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT - LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT
)


def resolve_span_sku(
    span: OTelGenAISpanRecord,
    sku: ModelPricingSKU | None = None,
) -> ModelPricingSKU | None:
    """Resolve effective `ModelPricingSKU` for a span (returns `None` for zero-token/`none` spans)."""
    if sku is not None:
        return sku
    if (
        not span.model_name
        or span.model_name == "none"
        or (span.input_tokens == 0 and span.output_tokens == 0 and span.thinking_tokens == 0)
    ):
        return None
    normalized = normalize_model_name(span.model_name)
    resolved = DEFAULT_MODEL_SKUS.get(normalized)
    if resolved is None:
        raise ValueError(
            f"Unknown model_name {span.model_name!r} (normalized {normalized!r}): "
            f"expected one of {sorted(DEFAULT_MODEL_SKUS)}."
        )
    return resolved


def compute_span_burndown_tokens(
    span: OTelGenAISpanRecord,
    sku: ModelPricingSKU | None = None,
) -> float:
    """Compute SKU-aware serving burndown tokens (1P cliffs + 3P cache-write/5x output weights)."""
    resolved = resolve_span_sku(span, sku)
    if resolved is None:
        return compute_weighted_burndown_tokens(
            input_tokens=span.input_tokens,
            cached_input_tokens=span.cached_input_tokens,
            output_tokens=span.output_tokens,
            thinking_tokens=span.thinking_tokens,
            cache_write_5m_tokens=span.cache_write_5m_tokens,
            cache_write_1h_tokens=span.cache_write_1h_tokens,
        )
    return compute_weighted_burndown_tokens(
        input_tokens=span.input_tokens,
        cached_input_tokens=span.cached_input_tokens,
        output_tokens=span.output_tokens,
        thinking_tokens=span.thinking_tokens,
        long_context_threshold=resolved.long_context_threshold_tokens,
        cache_write_5m_tokens=span.cache_write_5m_tokens,
        cache_write_1h_tokens=span.cache_write_1h_tokens,
        uncached_weight=resolved.burndown_uncached_weight,
        cached_weight=resolved.burndown_cached_weight,
        cache_write_5m_weight=resolved.burndown_cache_write_5m_weight,
        cache_write_1h_weight=resolved.burndown_cache_write_1h_weight,
        output_weight=resolved.burndown_output_weight,
        long_uncached_weight=resolved.burndown_long_uncached_weight,
        long_cached_weight=resolved.burndown_long_cached_weight,
        long_output_weight=resolved.burndown_long_output_weight,
    )


def paygo_multiplier_for_span(
    span: OTelGenAISpanRecord,
    sku: ModelPricingSKU,
) -> float:
    """Return PayGo billing multiplier (`1.8x` Priority, `0.5x` Flex, `1.0x` Standard/PT)."""
    if span.serving_traffic_type == ServingTrafficType.PRIORITY:
        return sku.priority_paygo_multiplier
    if span.serving_traffic_type == ServingTrafficType.FLEX:
        return sku.flex_paygo_multiplier
    return STANDARD_PAYGO_MULTIPLIER


def compute_span_cost_usd(
    span: OTelGenAISpanRecord,
    sku: ModelPricingSKU | None = None,
) -> float:
    """Single canonical 1P/3P span USD cost formula.

    Separates SKU burndown weights from PayGo billing multipliers (`1.8x` Priority / `0.5x` Flex)
    and prices 5m/1h ephemeral cache-write tokens on 3P models cleanly.
    """
    resolved = resolve_span_sku(span, sku)
    if resolved is None:
        return 0.0

    uncached_input = span.uncached_input_tokens
    output_and_thinking = span.output_tokens + span.thinking_tokens
    cw5_usd = (span.cache_write_5m_tokens / 1e6) * resolved.effective_cache_write_5m_usd_per_1m
    cw1_usd = (span.cache_write_1h_tokens / 1e6) * resolved.effective_cache_write_1h_usd_per_1m
    is_cliff = (
        resolved.long_context_threshold_tokens is not None
        and span.input_tokens > resolved.long_context_threshold_tokens
    )
    if is_cliff:
        base_usd = (
            (uncached_input / 1e6) * resolved.long_context_input_usd_per_1m
            + (span.cached_input_tokens / 1e6) * resolved.long_context_cached_usd_per_1m
            + cw5_usd
            + cw1_usd
            + (output_and_thinking / 1e6) * resolved.long_context_output_usd_per_1m
        )
    else:
        base_usd = (
            (uncached_input / 1e6) * resolved.input_usd_per_1m
            + (span.cached_input_tokens / 1e6) * resolved.cached_input_usd_per_1m
            + cw5_usd
            + cw1_usd
            + (output_and_thinking / 1e6) * resolved.output_usd_per_1m
        )
    return base_usd * paygo_multiplier_for_span(span, resolved)
