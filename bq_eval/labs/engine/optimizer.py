"""Failure loss-bank clustering, sanitized `ATIF-v1.8` export & clause-scoped NFR gate."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from bq_eval.core.contract.otel import OTelGenAISpanRecord, TracePayloadRecord
from bq_eval.core.engine.cost import compute_span_cost_usd
from bq_eval.labs.contract.optimizer import (
    AtifTrajectoryStep,
    AtifV18Trajectory,
    ClauseScopedNfrGateResult,
    FailureLossBankCluster,
    LossBankStatus,
    PairedEvalOutcome,
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SECRET_TOKEN_RE = re.compile(r"\b(?:sk-|ghp_|AIza)[A-Za-z0-9_-]{12,}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")


def sanitize_atif_text(text: str) -> str:
    """Replace PII and secret patterns with format-preserving synthetic tokens for `ATIF-v1.8` export."""
    if not text:
        return ""
    scrubbed = _EMAIL_RE.sub("user.synthetic@example.invalid", text)
    scrubbed = _SSN_RE.sub("000-00-0000", scrubbed)
    scrubbed = _SECRET_TOKEN_RE.sub("SYNTHETIC_CREDENTIAL_REDACTED", scrubbed)
    scrubbed = _CARD_RE.sub("4000-0000-0000-0000", scrubbed)
    return scrubbed


def _is_clause_amended(outcome: PairedEvalOutcome) -> bool:
    policy_changed = outcome.baseline_policy_sha256 != outcome.candidate_policy_sha256
    return (
        policy_changed
        and outcome.target_clause_id is not None
        and outcome.target_clause_id in outcome.amended_clause_ids
    )


def evaluate_clause_scoped_nfr_gate(
    outcomes: Sequence[PairedEvalOutcome],
) -> dict[tuple[str, str], ClauseScopedNfrGateResult]:
    """Enforce clause-scoped `policy_sha256` `NFR == 0.0%` gate (`v_step3_policy_nfr_gate`)."""
    grouped: dict[tuple[str, str], list[PairedEvalOutcome]] = {}
    for item in outcomes:
        grouped.setdefault((item.apphub_application_id, item.rubric_id), []).append(item)

    results: dict[tuple[str, str], ClauseScopedNfrGateResult] = {}
    for (app_id, rubric_id), rows in sorted(grouped.items()):
        paired = len(rows)
        base_pass = sum(1 for r in rows if r.baseline_pass)
        tot_neg = 0
        unamended_neg = 0
        amended_flips = 0
        pos_flips = 0
        unamended_base_pass = 0

        for r in rows:
            amended = _is_clause_amended(r)
            if r.baseline_pass and not amended:
                unamended_base_pass += 1
            if r.baseline_pass and not r.candidate_pass:
                tot_neg += 1
                if amended:
                    amended_flips += 1
                else:
                    unamended_neg += 1
            elif not r.baseline_pass and r.candidate_pass:
                pos_flips += 1

        unamended_nfr = (unamended_neg / unamended_base_pass) if unamended_base_pass > 0 else 0.0
        results[(app_id, rubric_id)] = ClauseScopedNfrGateResult(
            apphub_application_id=app_id,
            rubric_id=rubric_id,
            paired_traces=paired,
            baseline_passing_traces=base_pass,
            total_negative_flips=tot_neg,
            unamended_negative_flips=unamended_neg,
            amended_clause_flips=amended_flips,
            positive_flips=pos_flips,
            unamended_nfr=unamended_nfr,
            passes_nfr_gate=(unamended_neg == 0),
        )
    return results


def cluster_failure_loss_bank(
    outcomes: Sequence[PairedEvalOutcome],
) -> tuple[FailureLossBankCluster, ...]:
    """Cluster failing traces into `NEW`, `RECURRING`, and `RESOLVED` optimizer loss-bank entries."""
    buckets: dict[tuple[str, str, str], list[PairedEvalOutcome]] = {}
    for item in outcomes:
        if item.baseline_pass and item.candidate_pass:
            continue
        key = (item.apphub_application_id, item.rubric_id, item.failure_signature)
        buckets.setdefault(key, []).append(item)

    clusters: list[FailureLossBankCluster] = []
    for (app_id, rubric_id, sig), rows in sorted(buckets.items()):
        base_fails = sum(1 for r in rows if not r.baseline_pass)
        cand_fails = sum(1 for r in rows if not r.candidate_pass)
        status: LossBankStatus
        if base_fails == 0 and cand_fails > 0:
            status = "NEW"
        elif base_fails > 0 and cand_fails == 0:
            status = "RESOLVED"
        else:
            status = "RECURRING"
        digest = hashlib.sha256(f"{app_id}:{rubric_id}:{sig}".encode()).hexdigest()[:12]
        clusters.append(
            FailureLossBankCluster(
                cluster_id=f"lb-{digest}",
                apphub_application_id=app_id,
                rubric_id=rubric_id,
                failure_signature=sig,
                status=status,
                baseline_failure_count=base_fails,
                candidate_failure_count=cand_fails,
                sample_trace_ids=tuple(sorted({r.trace_id for r in rows})[:10]),
                cost_of_failure_usd=sum(r.trace_cost_usd for r in rows if not r.candidate_pass),
            )
        )
    return tuple(clusters)


def export_atif_v1_8_trajectories(
    spans: Sequence[OTelGenAISpanRecord],
    payloads: Mapping[str, TracePayloadRecord],
    outcomes: Sequence[PairedEvalOutcome],
) -> tuple[AtifV18Trajectory, ...]:
    """Export sanitized `ATIF-v1.8` failure trajectories ready for autonomous harness/skill optimizers."""
    spans_by_trace: dict[str, list[OTelGenAISpanRecord]] = {}
    for sp in spans:
        spans_by_trace.setdefault(sp.trace_id, []).append(sp)

    clusters = {
        (c.apphub_application_id, c.rubric_id, c.failure_signature): c.status
        for c in cluster_failure_loss_bank(outcomes)
    }

    exported: list[AtifV18Trajectory] = []
    for outcome in outcomes:
        if outcome.baseline_pass and outcome.candidate_pass:
            continue
        t_spans = sorted(
            spans_by_trace.get(outcome.trace_id, []),
            key=lambda s: (s.start_time, s.span_id),
        )
        steps = tuple(
            AtifTrajectoryStep(
                step_index=idx,
                span_id=sp.span_id,
                span_kind=str(sp.span_kind),
                agent_name=sp.agent_name,
                model_name=sp.model_name,
                tool_name=sp.tool_name,
                tool_call_status=sp.tool_call_status,
                status_code=sp.status_code,
                latency_ms=sp.latency_ms,
                input_tokens=sp.input_tokens,
                output_tokens=sp.output_tokens,
                rule0_pass=sp.rule0_pass,
            )
            for idx, sp in enumerate(t_spans)
        )
        payload = payloads.get(outcome.trace_id)
        session_id = next((s.session_id for s in t_spans if s.session_id), None)
        rule0_ok = (
            all(s.rule0_pass and s.status_code != "ERROR" for s in t_spans) if t_spans else True
        )
        total_cost = (
            sum(compute_span_cost_usd(s) for s in t_spans) if t_spans else outcome.trace_cost_usd
        )
        lb_status = clusters.get(
            (outcome.apphub_application_id, outcome.rubric_id, outcome.failure_signature),
            "NEW",
        )
        exported.append(
            AtifV18Trajectory(
                trace_id=outcome.trace_id,
                session_id=session_id,
                apphub_application_id=outcome.apphub_application_id,
                rubric_id=outcome.rubric_id,
                policy_sha256=outcome.candidate_policy_sha256 or outcome.baseline_policy_sha256,
                loss_bank_status=lb_status,
                failure_signature=outcome.failure_signature,
                rule0_pass=rule0_ok,
                sanitized_prompt=sanitize_atif_text(payload.user_prompt if payload else ""),
                sanitized_response=sanitize_atif_text(payload.final_response if payload else ""),
                sanitized_reasoning=sanitize_atif_text(outcome.reasoning_trace),
                steps=steps,
                total_cost_usd=total_cost,
            )
        )
    return tuple(exported)
