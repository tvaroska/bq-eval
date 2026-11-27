"""Per-span token burndown and USD cost, including the price step above 200K total context."""

from __future__ import annotations

from bq_eval.core.contract.otel import (
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


def resolve_span_sku(
    span: OTelGenAISpanRecord,
    sku: ModelPricingSKU | None = None,
) -> ModelPricingSKU | None:
    """Resolve effective `ModelPricingSKU` for a span (returns `None` only for zero-token spans)."""
    if sku is not None:
        return sku
    has_tokens = (
        span.prompt_context_tokens > 0 or span.output_tokens > 0 or span.thinking_tokens > 0
    )
    if not has_tokens:
        return None
    if not span.model_name or span.model_name == "none":
        raise ValueError(
            f"Cannot price non-zero tokens on span {span.span_id!r} with model_name={span.model_name!r}."
        )
    normalized = normalize_model_name(span.model_name)
    resolved = DEFAULT_MODEL_SKUS.get(normalized)
    if resolved is None:
        raise ValueError(
            f"Unknown model_name {span.model_name!r} (normalized {normalized!r}): "
            f"expected one of {sorted(DEFAULT_MODEL_SKUS)}."
        )
    return resolved


def meets_min_cache_floor(
    span: OTelGenAISpanRecord,
    sku: ModelPricingSKU | None = None,
) -> bool:
    """Return True when total prompt context meets or exceeds SKU `min_cache_tokens`."""
    resolved = resolve_span_sku(span, sku)
    if resolved is None:
        return True
    return span.prompt_context_tokens >= resolved.min_cache_tokens


def is_span_long_context_cliff(
    span: OTelGenAISpanRecord,
    sku: ModelPricingSKU | None = None,
) -> bool:
    """Return True iff total prompt context exceeds its resolved SKU's long-context cliff."""
    resolved = resolve_span_sku(span, sku)
    if resolved is None:
        return False
    return (
        resolved.long_context_threshold_tokens is not None
        and span.prompt_context_tokens > resolved.long_context_threshold_tokens
    )


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
            token_semantics=span.token_semantics,
            cache_write_5m_tokens=span.cache_write_5m_tokens,
            cache_write_1h_tokens=span.cache_write_1h_tokens,
        )
    return compute_weighted_burndown_tokens(
        input_tokens=span.input_tokens,
        cached_input_tokens=span.cached_input_tokens,
        output_tokens=span.output_tokens,
        thinking_tokens=span.thinking_tokens,
        long_context_threshold=resolved.long_context_threshold_tokens,
        token_semantics=span.token_semantics,
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
    *,
    include_committed_list_equivalent: bool = True,
) -> float:
    """Single canonical 1P/3P span USD cost formula keyed on `prompt_context_tokens`.

    Set `include_committed_list_equivalent=False` to price `PROVISIONED_THROUGHPUT` spans at
    `0.0` incremental PayGo USD and prevent double-counting against committed PT fixed costs.
    """
    resolved = resolve_span_sku(span, sku)
    if resolved is None:
        return 0.0
    if (
        not include_committed_list_equivalent
        and span.serving_traffic_type == ServingTrafficType.PROVISIONED_THROUGHPUT
    ):
        return 0.0

    uncached_input = span.uncached_input_tokens
    output_and_thinking = span.output_tokens + span.thinking_tokens
    if is_span_long_context_cliff(span, resolved):
        cw5_rate = resolved.long_context_input_usd_per_1m * resolved.burndown_cache_write_5m_weight
        cw1_rate = resolved.long_context_input_usd_per_1m * resolved.burndown_cache_write_1h_weight
        base_usd = (
            (uncached_input / 1e6) * resolved.long_context_input_usd_per_1m
            + (span.cached_input_tokens / 1e6) * resolved.long_context_cached_usd_per_1m
            + (span.cache_write_5m_tokens / 1e6) * cw5_rate
            + (span.cache_write_1h_tokens / 1e6) * cw1_rate
            + (output_and_thinking / 1e6) * resolved.long_context_output_usd_per_1m
        )
    else:
        base_usd = (
            (uncached_input / 1e6) * resolved.input_usd_per_1m
            + (span.cached_input_tokens / 1e6) * resolved.cached_input_usd_per_1m
            + (span.cache_write_5m_tokens / 1e6) * resolved.effective_cache_write_5m_usd_per_1m
            + (span.cache_write_1h_tokens / 1e6) * resolved.effective_cache_write_1h_usd_per_1m
            + (output_and_thinking / 1e6) * resolved.output_usd_per_1m
        )
    return base_usd * paygo_multiplier_for_span(span, resolved)
