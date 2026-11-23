"""BigQuery Omni cloud-agnostic zero-egress spoke utilities & multi-cloud CPVO reconciliation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from bq_eval.contract.rubrics import CANONICAL_RUBRICS

FORBIDDEN_RAW_PAYLOAD_COLUMNS: frozenset[str] = frozenset(
    {
        "user_prompt",
        "retrieved_context",
        "final_response",
        "raw_prompt",
        "raw_response",
        "prompt_text",
        "completion_text",
        "payload",
        "json_payload",
    }
)

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_-]{2,64}$")
_CONN_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,128}$")
_VIEW_BLOCK_RE = re.compile(
    r"(?is)CREATE\s+OR\s+REPLACE\s+VIEW\s+`?([a-zA-Z0-9_.-]+)`?\s+AS\s+(.*?);"
)


@dataclass(frozen=True)
class RemoteTraceSliceRecord:
    """Zero-egress partial telemetry rollup pushed from an AWS or Azure BigQuery Omni spoke."""

    cloud_provider: Literal["aws", "azure", "on_prem"]
    cloud_region: str
    apphub_application_id: str
    workload_principal: str
    model_name: str
    finops_traffic_type: str
    total_traces: int
    rule0_passing_traces: int
    verified_outcomes: int
    total_spend_usd: float


@dataclass(frozen=True)
class UnifiedMultiCloudCpvoRow:
    """Unified multi-cloud CPVO row mirroring `agent_eval.v_unified_multicloud_cpvo`."""

    apphub_application_id: str
    active_clouds: int
    total_traces: int
    verified_outcomes: int
    total_spend_usd: float
    unified_cost_per_verified_outcome_usd: float | None


def normalize_workload_principal(
    raw_identity: str | None,
    *,
    cloud_provider: Literal["aws", "azure", "on_prem"] = "aws",
) -> str:
    """Normalize `principal://` / `spiffe://` URIs matching `sql/omni_spoke_views.sql`."""
    cleaned = (raw_identity or "").strip()
    if not cleaned:
        return "principal://unattributed/unknown"
    if cleaned.startswith(("spiffe://", "principal://")):
        return cleaned
    return f"principal://iam.googleapis.com/projects/external-{cloud_provider}/subject/{cleaned}"


def normalize_omni_application_id(gcp_apphub_application_id: str | None) -> str:
    """Enforce strict `'unregistered_app'` fallback on BigQuery Omni spoke spans."""
    cleaned = (gcp_apphub_application_id or "").strip()
    return cleaned if cleaned else "unregistered_app"


def verify_zero_raw_payload_egress(sql_text: str) -> tuple[bool, tuple[str, ...]]:
    """Prove `sql/omni_spoke_views.sql` projects zero raw prompt/context/completion columns."""
    violations: list[str] = []
    for match in _VIEW_BLOCK_RE.finditer(sql_text):
        view_name = match.group(1)
        body = match.group(2)
        for forbidden in sorted(FORBIDDEN_RAW_PAYLOAD_COLUMNS):
            if re.search(rf"\b{re.escape(forbidden)}\b", body, flags=re.IGNORECASE):
                violations.append(f"{view_name}: forbidden raw payload reference {forbidden!r}")
    return not violations, tuple(violations)


def build_omni_in_residency_judge_sql(  # noqa: PLR0913
    *,
    project_id: str,
    dataset_id: str = "agent_eval",
    cloud_provider: Literal["aws", "azure"] = "aws",
    spoke_connection_id: str = "aws-us-east-1.omni_connection",
    rubric_id: str = "groundedness_4pt_v1",
    canary_sample_pct: int = 5,
) -> str:
    """Build in-cloud-residency Step-2 BigQuery Omni judging SQL with pushed-down sampling."""
    if not _IDENT_RE.fullmatch(project_id.strip()):
        raise ValueError(f"Invalid project_id: {project_id!r}")
    if not _IDENT_RE.fullmatch(dataset_id.strip()):
        raise ValueError(f"Invalid dataset_id: {dataset_id!r}")
    if not _CONN_RE.fullmatch(spoke_connection_id.strip()):
        raise ValueError(f"Invalid spoke_connection_id: {spoke_connection_id!r}")
    if rubric_id not in CANONICAL_RUBRICS:
        raise ValueError(f"Unknown rubric_id: {rubric_id!r}")
    if isinstance(canary_sample_pct, bool) or not (0 <= canary_sample_pct <= 100):
        raise ValueError("canary_sample_pct must be an integer between 0 and 100.")

    p = project_id.strip()
    d = dataset_id.strip()
    spoke_view = "v_omni_aws_spoke_spans" if cloud_provider == "aws" else "v_omni_azure_spoke_spans"
    return (
        f"-- In-Cloud-Residency BigQuery Omni Step-2 Sample Selection ({cloud_provider.upper()})\n"
        f"-- Pushes FARM_FINGERPRINT down inside {spoke_connection_id}; zero raw prompt egress to Hub.\n"
        f"SELECT\n"
        f"  cloud_provider,\n"
        f"  cloud_region,\n"
        f"  trace_id,\n"
        f"  apphub_application_id,\n"
        f"  workload_principal,\n"
        f"  '{rubric_id}' AS rubric_id,\n"
        f"  rule0_pass,\n"
        f"  weighted_burndown_tokens,\n"
        f"  span_cost_usd\n"
        f"FROM `{p}.{d}.{spoke_view}`\n"
        f"WHERE rule0_pass = TRUE\n"
        f"  AND MOD(ABS(FARM_FINGERPRINT(trace_id)), 100) < {canary_sample_pct};\n"
    )


def reconcile_multicloud_cpvo(
    gcp_outcomes: Sequence[tuple[str, int, int, float]],
    remote_slices: Sequence[RemoteTraceSliceRecord],
) -> dict[str, UnifiedMultiCloudCpvoRow]:
    """Reconcile local GCP `(app_id, traces, verified, spend_usd)` + remote Omni slices into unified CPVO."""
    clouds: dict[str, set[str]] = {}
    traces: dict[str, int] = {}
    verified: dict[str, int] = {}
    spend: dict[str, float] = {}

    for app_id, n_traces, n_verified, usd in gcp_outcomes:
        clouds.setdefault(app_id, set()).add("gcp")
        traces[app_id] = traces.get(app_id, 0) + n_traces
        verified[app_id] = verified.get(app_id, 0) + n_verified
        spend[app_id] = spend.get(app_id, 0.0) + usd

    for sl in remote_slices:
        app_id = normalize_omni_application_id(sl.apphub_application_id)
        clouds.setdefault(app_id, set()).add(sl.cloud_provider)
        traces[app_id] = traces.get(app_id, 0) + sl.total_traces
        verified[app_id] = verified.get(app_id, 0) + sl.verified_outcomes
        spend[app_id] = spend.get(app_id, 0.0) + sl.total_spend_usd

    result: dict[str, UnifiedMultiCloudCpvoRow] = {}
    for app_id in sorted(clouds):
        ver = verified[app_id]
        tot_usd = spend[app_id]
        result[app_id] = UnifiedMultiCloudCpvoRow(
            apphub_application_id=app_id,
            active_clouds=len(clouds[app_id]),
            total_traces=traces[app_id],
            verified_outcomes=ver,
            total_spend_usd=tot_usd,
            unified_cost_per_verified_outcome_usd=(tot_usd / ver) if ver > 0 else None,
        )
    return result
