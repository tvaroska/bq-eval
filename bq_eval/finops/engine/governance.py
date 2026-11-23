"""`RL-1`-`RL-5` application risk governance and inference_request reconciliation engine."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from bq_eval.core.contract.otel import OTelGenAISpanRecord, ServingTrafficType
from bq_eval.core.contract.pricing import DEFAULT_MODEL_SKUS, ModelPricingSKU, normalize_model_name
from bq_eval.finops.contract.governance import (
    AppGovernanceRecord,
    GovernanceEvaluationResult,
    LifecycleTrackPolicy,
    RiskLevel,
)

RISK_LEVEL_AC2_GATES: dict[RiskLevel, float] = {
    RiskLevel.RL_1: 0.85,
    RiskLevel.RL_2: 0.85,
    RiskLevel.RL_3A: 0.80,
    RiskLevel.RL_3B: 0.80,
    RiskLevel.RL_4: 0.75,
    RiskLevel.RL_5: 0.75,
}

_NON_LONG_TERM_TRACKS: frozenset[str] = frozenset({"short_term_ga", "preview", "legacy_sunset"})


@dataclass(frozen=True)
class InferenceRequestLogRecord:
    """Server-side `inference_request` log entry for OTel-vs-billing reconciliation."""

    request_id: str
    apphub_application_id: str
    model_name: str
    serving_traffic_type: ServingTrafficType
    http_status_code: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    request_type_header: str | None = None
    request_timestamp: datetime | None = None


@dataclass(frozen=True)
class InferenceReconciliationRow:
    """Hourly OTel-vs-server reconciliation row mirroring `v_inference_request_reconciliation`."""

    usage_hour: str
    apphub_application_id: str
    model_name: str
    otel_span_count: int
    server_request_count: int
    otel_429_count: int
    server_429_count: int
    otel_total_tokens: int
    server_total_tokens: int
    token_drift_pct: float
    pt_eligible_involuntary_spillover_count: int
    intentional_shared_bypass_count: int
    reconciliation_status: str


def required_ac2_for_risk_level(risk_tier: RiskLevel) -> float:
    """Return tiered Quadratic Gwet's `AC2` IRR gate (`0.85` / `0.80` / `0.75`) for `risk_tier`."""
    return RISK_LEVEL_AC2_GATES[risk_tier]


def evaluate_app_governance(
    app: AppGovernanceRecord,
    model_name: str,
    *,
    observed_ac2: float | None = None,
    sku: ModelPricingSKU | None = None,
) -> GovernanceEvaluationResult:
    """Evaluate `RL-1`-`RL-5` tiered `AC2` gate and block long-term PT commitments on short-term GA models."""
    resolved_sku = sku or DEFAULT_MODEL_SKUS.get(normalize_model_name(model_name))
    lifecycle_track = resolved_sku.lifecycle_track if resolved_sku is not None else "long_term_ga"
    ac2_gate = required_ac2_for_risk_level(app.risk_tier)
    passes_ac2 = (observed_ac2 >= ac2_gate) if observed_ac2 is not None else None

    violations: list[str] = []
    has_long_term_pt = app.pt_commitment_months >= 3 or (
        app.funded_gsus > 0.0
        and app.lifecycle_track_policy == LifecycleTrackPolicy.LONG_TERM_GA_ONLY
    )
    pt_allowed = not (has_long_term_pt and lifecycle_track in _NON_LONG_TERM_TRACKS)
    if not pt_allowed:
        violations.append(
            f"Blocked PT commitment ({app.pt_commitment_months}m, {app.funded_gsus:g} GSUs) "
            f"for {app.apphub_application_id} on {lifecycle_track} model {model_name}."
        )
    if passes_ac2 is False and observed_ac2 is not None:
        violations.append(
            f"Gwet AC2 {observed_ac2:.4f} below {app.risk_tier} gate ({ac2_gate:.2f})."
        )

    return GovernanceEvaluationResult(
        apphub_application_id=app.apphub_application_id,
        risk_tier=app.risk_tier,
        required_ac2_gate=ac2_gate,
        observed_ac2=observed_ac2,
        passes_ac2_gate=passes_ac2,
        model_name=model_name,
        model_lifecycle_track=lifecycle_track,
        pt_commitment_allowed=pt_allowed,
        violations=tuple(violations),
    )


def _hour_iso(dt: datetime) -> str:
    utc_dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return utc_dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _accumulate_bucket(  # noqa: PLR0913
    buckets: dict[tuple[str, str, str], dict[str, int]],
    *,
    key: tuple[str, str, str],
    http_status_code: int | None,
    total_tokens: int,
    request_type_header: str | None,
    serving_traffic_type: ServingTrafficType,
) -> None:
    b = buckets.setdefault(key, {"count": 0, "c429": 0, "tokens": 0, "involuntary": 0, "shared": 0})
    b["count"] += 1
    if http_status_code == 429:
        b["c429"] += 1
    b["tokens"] += total_tokens
    hdr = (request_type_header or "").strip().lower()
    if hdr == "shared":
        b["shared"] += 1
    elif serving_traffic_type != ServingTrafficType.PROVISIONED_THROUGHPUT:
        b["involuntary"] += 1


def reconcile_inference_requests(
    otel_spans: Sequence[OTelGenAISpanRecord],
    server_logs: Sequence[InferenceRequestLogRecord],
) -> list[InferenceReconciliationRow]:
    """Deterministic Python twin of `agent_eval.v_inference_request_reconciliation`."""
    otel_buckets: dict[tuple[str, str, str], dict[str, int]] = {}
    for sp in otel_spans:
        if not sp.model_name or sp.model_name == "none":
            continue
        _accumulate_bucket(
            otel_buckets,
            key=(_hour_iso(sp.start_time), sp.resolved_application_id, sp.model_name),
            http_status_code=sp.http_status_code,
            total_tokens=sp.input_tokens + sp.output_tokens + sp.thinking_tokens,
            request_type_header=sp.request_type_header,
            serving_traffic_type=sp.serving_traffic_type,
        )

    server_buckets: dict[tuple[str, str, str], dict[str, int]] = {}
    for log in server_logs:
        ts = log.request_timestamp or datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
        _accumulate_bucket(
            server_buckets,
            key=(_hour_iso(ts), log.apphub_application_id, log.model_name),
            http_status_code=log.http_status_code,
            total_tokens=log.input_tokens + log.output_tokens + log.thinking_tokens,
            request_type_header=log.request_type_header,
            serving_traffic_type=log.serving_traffic_type,
        )

    all_keys = sorted(set(otel_buckets) | set(server_buckets))
    rows: list[InferenceReconciliationRow] = []
    for hour, app_id, model in all_keys:
        ob = otel_buckets.get((hour, app_id, model))
        sb = server_buckets.get((hour, app_id, model))
        o_tok = ob["tokens"] if ob else 0
        s_tok = sb["tokens"] if sb else 0
        denom = s_tok if sb and s_tok > 0 else o_tok
        drift_pct = (100.0 * (o_tok - s_tok) / denom) if denom > 0 else 0.0
        rows.append(
            InferenceReconciliationRow(
                usage_hour=hour,
                apphub_application_id=app_id,
                model_name=model,
                otel_span_count=ob["count"] if ob else 0,
                server_request_count=sb["count"] if sb else 0,
                otel_429_count=ob["c429"] if ob else 0,
                server_429_count=sb["c429"] if sb else 0,
                otel_total_tokens=o_tok,
                server_total_tokens=s_tok,
                token_drift_pct=drift_pct,
                pt_eligible_involuntary_spillover_count=(
                    sb["involuntary"] if sb else (ob["involuntary"] if ob else 0)
                ),
                intentional_shared_bypass_count=(
                    sb["shared"] if sb else (ob["shared"] if ob else 0)
                ),
                reconciliation_status="RECONCILED" if abs(drift_pct) <= 2.0 else "DRIFT_ALERT",
            )
        )
    return rows
