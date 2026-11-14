"""Self-contained synthetic showcase generator for BigQuery Agent Evaluation & FinOps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from bq_eval.calibration import ConfusionMatrix, compute_gwet_ac2
from bq_eval.finops import (
    EvalFinOpsROI,
    FocusChargebackRow,
    ModelOption,
    OffPeakSchedulePlan,
    ParetoEvalResult,
    PrefixCacheDilutionReport,
    allocate_focus_two_pool_chargeback,
    compute_eval_finops_roi,
    compute_pareto_frontier,
    diagnose_prefix_cache_dilution,
    plan_off_peak_eval_schedule,
)
from bq_eval.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.sql_eval import SQLTraceEvalSummary, evaluate_traces_in_sqlite


class ShowcaseRunArtifacts(BaseModel):
    """Complete deterministic output bundle from a `bq-eval demo` showcase run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summaries: dict[str, SQLTraceEvalSummary]
    gwet_ac2: float
    eval_roi: EvalFinOpsROI
    cache_dilution: PrefixCacheDilutionReport
    pareto: ParetoEvalResult
    focus_chargeback: tuple[FocusChargebackRow, ...]
    off_peak_plan: OffPeakSchedulePlan


def build_showcase_dataset() -> tuple[
    list[OTelGenAISpanRecord],
    dict[str, bool],
    ConfusionMatrix,
]:
    """Generate deterministic multi-app OTel spans, Tier 2 judge outcomes, and SME confusion matrix."""
    base_time = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    judge_outcomes: dict[str, bool] = {}

    # App 1: order-support-agent (Calibrated production workload with 5% canary + error tail)
    for i in range(40):
        trace_id = f"trace-order-{i:04d}"
        is_error_tail = i in (38, 39)
        is_escalated = i in (35, 36, 37)
        traffic_type = (
            FinOpsTrafficType.CONFLICT_ESCALATION
            if is_escalated
            else FinOpsTrafficType.PRODUCTION_INFERENCE
        )
        model_name = "gemini-4-argon" if is_escalated else "gemini-3.8-flash"
        input_tokens = 240_000 if i == 37 else 12_000
        cached_tokens = 2_400 if i == 37 else 3_000

        spans.append(
            OTelGenAISpanRecord(
                trace_id=trace_id,
                span_id=f"span-root-{i:04d}",
                parent_span_id=None,
                start_time=base_time + timedelta(seconds=i * 15),
                end_time=base_time + timedelta(seconds=i * 15, milliseconds=850 + (i * 20)),
                latency_ms=850.0 + (i * 20),
                status_code="OK",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                service_name="order-support-service",
                gcp_apphub_application_id="order-support-agent",
                agent_name="order-concierge",
                model_name=model_name,
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                output_tokens=650,
                thinking_tokens=350 if is_escalated else 150,
                rule0_pass=not is_error_tail,
                finops_traffic_type=traffic_type,
            )
        )
        spans.append(
            OTelGenAISpanRecord(
                trace_id=trace_id,
                span_id=f"span-tool-{i:04d}",
                parent_span_id=f"span-root-{i:04d}",
                start_time=base_time + timedelta(seconds=i * 15, milliseconds=100),
                end_time=base_time + timedelta(seconds=i * 15, milliseconds=320),
                latency_ms=220.0,
                status_code="ERROR" if is_error_tail else "OK",
                span_kind=GenAISpanKind.EXECUTE_TOOL,
                service_name="order-support-service",
                gcp_apphub_application_id="order-support-agent",
                agent_name="order-concierge",
                model_name=model_name,
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                thinking_tokens=0,
                tool_name="lookup_order_ledger",
                tool_call_status="VALIDATION_ERROR" if is_error_tail else "SUCCESS",
                rule0_pass=not is_error_tail,
                finops_traffic_type=traffic_type,
            )
        )
        if i < 20:
            judge_outcomes[trace_id] = i not in (18, 19)
        elif is_error_tail:
            judge_outcomes[trace_id] = False

    # App 2: catalog-enrichment-agent (Off-GCP agent resolved via attributes["gcp.apphub.application.id"], unjudged)
    for j in range(10):
        trace_id = f"trace-catalog-{j:04d}"
        spans.append(
            OTelGenAISpanRecord(
                trace_id=trace_id,
                span_id=f"span-cat-{j:04d}",
                parent_span_id=None,
                start_time=base_time + timedelta(minutes=15, seconds=j * 10),
                end_time=base_time + timedelta(minutes=15, seconds=j * 10, milliseconds=420),
                latency_ms=420.0,
                status_code="OK",
                span_kind=GenAISpanKind.CHAT,
                service_name="agent-service",
                gcp_apphub_application_id=None,
                agent_name="catalog-enricher",
                model_name="gemini-3.5-flash-lite",
                input_tokens=8_000,
                cached_input_tokens=6_400,
                output_tokens=300,
                thinking_tokens=0,
                rule0_pass=True,
                finops_traffic_type=FinOpsTrafficType.PRODUCTION_INFERENCE,
                attributes={"gcp.apphub.application.id": "catalog-enrichment-agent"},
            )
        )

    # Canonical SME calibration matrix [TP=87, FN=2, TN=7, FP=4] (TPR=87/89, TNR=7/11)
    confusion = ConfusionMatrix(tp=87, fn=2, tn=7, fp=4)
    return spans, judge_outcomes, confusion


def run_showcase_pipeline() -> ShowcaseRunArtifacts:
    """Execute the full Tier 1-3 SQL evaluation and FinOps optimization pipeline."""
    spans, judge_outcomes, confusion = build_showcase_dataset()
    summaries = evaluate_traces_in_sqlite(
        spans=spans,
        judge_outcomes=judge_outcomes,
        sme_confusion=confusion,
    )

    rater_a = [4, 4, 3, 4, 3, 4, 1, 2, 4, 3, 4, 4, 3, 2, 4, 4, 3, 4, 1, 4]
    rater_b = [4, 4, 3, 4, 3, 3, 1, 2, 4, 3, 4, 4, 3, 2, 4, 4, 3, 4, 1, 4]
    ac2 = compute_gwet_ac2(rater_a, rater_b)

    eval_roi = compute_eval_finops_roi(
        total_production_traces=100_000,
        error_or_sla_tail_rate=0.03,
        canary_sample_rate=0.05,
        judge_cost_per_trace_usd=0.0025,
    )

    order_spans = [s for s in spans if s.resolved_application_id == "order-support-agent"]
    cache_dilution = diagnose_prefix_cache_dilution(
        spans=order_spans,
        distinct_prompt_variants=4,
        target_cache_hit_ratio=0.75,
    )

    pareto = compute_pareto_frontier(
        candidates=[
            ModelOption(
                config_id="flash-3.8-cached-prefix",
                model_name="gemini-3.8-flash",
                rule0_pass_rate=0.992,
                non_fabrication_rate_nfr=0.0,
                calibrated_pass_rate=0.915,
                cost_per_verified_outcome_usd=0.0019,
                p95_latency_ms=980.0,
            ),
            ModelOption(
                config_id="argon-4-high-reasoning",
                model_name="gemini-4-argon",
                rule0_pass_rate=0.998,
                non_fabrication_rate_nfr=0.0,
                calibrated_pass_rate=0.968,
                cost_per_verified_outcome_usd=0.0084,
                p95_latency_ms=2150.0,
            ),
            ModelOption(
                config_id="flash-3.8-uncached-fragmented",
                model_name="gemini-3.8-flash",
                rule0_pass_rate=0.985,
                non_fabrication_rate_nfr=0.0,
                calibrated_pass_rate=0.890,
                cost_per_verified_outcome_usd=0.0034,
                p95_latency_ms=1280.0,
            ),
            ModelOption(
                config_id="unverified-draft-prompt",
                model_name="gemini-3.5-flash-lite",
                rule0_pass_rate=0.940,
                non_fabrication_rate_nfr=0.025,
                calibrated_pass_rate=0.820,
                cost_per_verified_outcome_usd=0.0009,
                p95_latency_ms=540.0,
            ),
        ]
    )

    focus_rows = allocate_focus_two_pool_chargeback(
        app_burndown_tokens={
            "order-support-agent": 65_000_000.0,
            "catalog-enrichment-agent": 15_000_000.0,
        },
        app_cost_centers={
            "order-support-agent": "CC-RETAIL-OPS",
            "catalog-enrichment-agent": "CC-MERCH-CATALOG",
        },
        reserved_pt_capacity_tokens=100_000_000.0,
        reserved_pt_fixed_cost_usd=120.0,
    )

    off_peak_plan = plan_off_peak_eval_schedule(
        hourly_production_burndown=[
            2_000_000.0,
            1_500_000.0,
            1_200_000.0,
            1_500_000.0,
            4_500_000.0,
            5_000_000.0,
            4_800_000.0,
            3_200_000.0,
        ],
        hourly_reserved_pt_capacity=5_000_000.0,
        total_batch_eval_tokens=12_000_000.0,
    )

    return ShowcaseRunArtifacts(
        summaries=summaries,
        gwet_ac2=ac2,
        eval_roi=eval_roi,
        cache_dilution=cache_dilution,
        pareto=pareto,
        focus_chargeback=tuple(focus_rows),
        off_peak_plan=off_peak_plan,
    )


def render_markdown_report(artifacts: ShowcaseRunArtifacts) -> str:
    """Format a concise Markdown executive scorecard from `ShowcaseRunArtifacts`."""
    order = artifacts.summaries["order-support-agent"]
    catalog = artifacts.summaries["catalog-enrichment-agent"]
    roi = artifacts.eval_roi
    dil = artifacts.cache_dilution
    rec = artifacts.pareto.recommended_default

    cal_str = (
        f"{order.calibrated_pass_rate * 100:.1f}% "
        f"(95% CI: [{order.calibrated_ci_95_lower * 100:.1f}%, {order.calibrated_ci_95_upper * 100:.1f}%])"
        if order.calibrated_pass_rate is not None
        and order.calibrated_ci_95_lower is not None
        and order.calibrated_ci_95_upper is not None
        else "UNJUDGED (NULL)"
    )
    cpvo_str = (
        f"${order.cost_per_verified_outcome_usd:.4f}"
        if order.cost_per_verified_outcome_usd is not None
        else "NULL (Fail-Closed)"
    )

    focus_lines = "\n".join(
        f"| `{row.apphub_application_id}` | `{row.cost_center}` | "
        f"{row.weighted_burndown_tokens:,.0f} | {row.utilization_share * 100:.1f}% | "
        f"${row.utilized_pt_cost_usd:.2f} | ${row.unutilized_idle_capacity_tax_usd:.2f} | "
        f"${row.payg_overflow_cost_usd:.2f} | ${row.total_allocated_cost_usd:.2f} |"
        for row in artifacts.focus_chargeback
    )

    return f"""# BQ Eval & FinOps Executive Scorecard (`bq-eval`)

## 1. Tier 1-3 Application Quality & Cost per Verified Outcome (`CPVO`)

| Application ID | Traces | Rule 0 Pass | Calibration Status | Raw Judge Pass | Rogan-Gladen Calibrated Pass | Weighted Burndown | Total Spend | CPVO |
| :--- | ---: | ---: | :--- | ---: | :--- | ---: | ---: | ---: |
| `order-support-agent` | {order.total_traces} | {order.rule0_pass_rate * 100:.1f}% | `{order.calibration_status}` | {(order.raw_judge_pass_rate or 0.0) * 100:.1f}% | {cal_str} | {order.total_weighted_burndown_tokens:,.0f} | ${order.total_spend_usd:.4f} | {cpvo_str} |
| `catalog-enrichment-agent` | {catalog.total_traces} | {catalog.rule0_pass_rate * 100:.1f}% | `{catalog.calibration_status}` | `NULL` | `NULL (Fail-Closed)` | {catalog.total_weighted_burndown_tokens:,.0f} | ${catalog.total_spend_usd:.4f} | `NULL` |

- **Inter-Rater Reliability (`Gwet's AC2`):** `{artifacts.gwet_ac2:.3f}` (Gate: `>= 0.800` PASS)

## 2. 3-Tier Evaluation Funnel FinOps ROI (`100,000` Monthly Production Traces)

- **Baseline (`100%` LLM-as-a-Judge):** `${roi.baseline_100pct_judge_usd:,.2f}` (`{roi.total_production_traces:,}` judge calls)
- **3-Tier Stratified Funnel (`5%` Canary + `100%` Error/SLA-Tail):** `${roi.stratified_3tier_judge_usd:,.2f}` (`{roi.tier2_total_judged_traces:,}` judge calls)
- **Net Evaluation Cost Savings:** **`${roi.net_savings_usd:,.2f}` (`{roi.savings_pct:.1f}%` reduction)**

## 3. Prefix-Cache `1/N` Dilution & `NFR = 0.0%` Pareto Frontier

- **Observed Prefix-Cache Hit Ratio (`order-support-agent`):** `{dil.observed_cache_hit_ratio * 100:.1f}%` across `{dil.distinct_prompt_variants}` prompt variants (`1/N` ceiling `{dil.theoretical_dilution_ceiling * 100:.1f}%`)
- **Action:** {dil.recommendation}
- **Recommended Default Routing (`NFR = 0.0%` Gate):** `{rec.config_id if rec else "none"}` (`{rec.model_name if rec else "N/A"}`, CPVO `${rec.cost_per_verified_outcome_usd if rec else 0.0:.4f}`)

## 4. FOCUS 1.2 Two-Pool Chargeback & Off-Peak PT Water-Filling

| Allocation Owner | Cost Center | Weighted Burndown | Share | Utilized PT | Idle Capacity Tax | PAYG Overflow | Total Allocated |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
{focus_lines}

- **Off-Peak PT Batch-Eval Coverage:** `{artifacts.off_peak_plan.zero_marginal_cost_coverage_pct:.1f}%` of `{artifacts.off_peak_plan.total_batch_eval_tokens_requested:,.0f}` batch eval tokens scheduled into zero-marginal-cost PT valleys (saving `${artifacts.off_peak_plan.avoided_payg_cost_usd:.2f}`).
"""


def write_html_scorecard(artifacts: ShowcaseRunArtifacts, output_path: Path) -> Path:
    """Write a self-contained GM3-styled HTML scorecard with high-contrast SVG favicon."""
    order = artifacts.summaries["order-support-agent"]
    catalog = artifacts.summaries["catalog-enrichment-agent"]
    roi = artifacts.eval_roi
    dil = artifacts.cache_dilution

    focus_html_rows = "\n          ".join(
        f"<tr><td><code>{row.apphub_application_id}</code></td>"
        f"<td><code>{row.cost_center}</code></td>"
        f"<td>{row.weighted_burndown_tokens:,.0f}</td>"
        f"<td>{row.utilization_share * 100:.1f}%</td>"
        f"<td>${row.utilized_pt_cost_usd:.2f}</td>"
        f"<td>${row.unutilized_idle_capacity_tax_usd:.2f}</td>"
        f"<td>${row.payg_overflow_cost_usd:.2f}</td>"
        f"<td><strong>${row.total_allocated_cost_usd:.2f}</strong></td></tr>"
        for row in artifacts.focus_chargeback
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>BQ Eval &amp; FinOps Scorecard (bq-eval)</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%230B57D0'/%3E%3Cpath d='M16 44V26h8v18h-8zm12 0V16h8v28h-8zm12 0V32h8v12h-8z' fill='%23FFFFFF'/%3E%3Ccircle cx='48' cy='20' r='6' fill='%2334A853'/%3E%3C/svg%3E" />
  <style>
    :root {{
      --bg: #F8FAFD;
      --surface: #FFFFFF;
      --text: #1F1F1F;
      --muted: #444746;
      --primary: #0B57D0;
      --success: #146C2E;
      --warning: #B06000;
      --border: #E0E3E7;
    }}
    body {{
      font-family: "Google Sans", "Inter", system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 32px 24px;
      line-height: 1.5;
    }}
    .container {{
      max-width: 1120px;
      margin: 0 auto;
    }}
    header {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 24px 28px;
      margin-bottom: 24px;
    }}
    h1 {{
      margin: 0 0 6px 0;
      font-size: 1.65rem;
      color: var(--primary);
    }}
    .subtitle {{
      margin: 0;
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
    }}
    .kpi-label {{
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .kpi-value {{
      font-size: 1.75rem;
      font-weight: 700;
      color: var(--text);
    }}
    .kpi-note {{
      font-size: 0.85rem;
      color: var(--success);
      margin-top: 4px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.9rem;
    }}
    th, td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--border);
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      background: #E8F0FE;
      color: var(--primary);
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>BQ Eval &amp; FinOps Executive Scorecard</h1>
      <p class="subtitle">Dual-Path Capture (Light BQAA + Full OTel) · 3-Tier SQL Judge Funnel · Rogan-Gladen Calibration · FOCUS 1.2 Chargeback</p>
    </header>

    <div class="kpi-grid">
      <div class="card">
        <div class="kpi-label">Calibrated True Pass Rate</div>
        <div class="kpi-value">{(order.calibrated_pass_rate or 0.0) * 100:.1f}%</div>
        <div class="kpi-note">95% CI: [{(order.calibrated_ci_95_lower or 0.0) * 100:.1f}%, {(order.calibrated_ci_95_upper or 0.0) * 100:.1f}%] (Rogan-Gladen)</div>
      </div>
      <div class="card">
        <div class="kpi-label">3-Tier Eval FinOps Savings</div>
        <div class="kpi-value">{roi.savings_pct:.1f}%</div>
        <div class="kpi-note">${roi.net_savings_usd:,.2f}/mo saved vs. 100% LLM judging</div>
      </div>
      <div class="card">
        <div class="kpi-label">SME IRR (Gwet's AC2)</div>
        <div class="kpi-value">{artifacts.gwet_ac2:.3f}</div>
        <div class="kpi-note">Exceeds &gt;= 0.80 production calibration gate</div>
      </div>
      <div class="card">
        <div class="kpi-label">Off-Peak PT Valley Coverage</div>
        <div class="kpi-value">{artifacts.off_peak_plan.zero_marginal_cost_coverage_pct:.1f}%</div>
        <div class="kpi-note">Batch Agent Gym &amp; Judge runs in free PT slots</div>
      </div>
    </div>

    <div class="card">
      <h2>Application Quality &amp; Cost per Verified Outcome (CPVO)</h2>
      <table>
        <thead>
          <tr>
            <th>Application ID</th>
            <th>Traces</th>
            <th>Rule 0 Pass</th>
            <th>Status</th>
            <th>Calibrated Pass</th>
            <th>Weighted Burndown</th>
            <th>CPVO (USD)</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><code>{order.apphub_application_id}</code></td>
            <td>{order.total_traces}</td>
            <td>{order.rule0_pass_rate * 100:.1f}%</td>
            <td><span class="badge">{order.calibration_status}</span></td>
            <td>{(order.calibrated_pass_rate or 0.0) * 100:.1f}%</td>
            <td>{order.total_weighted_burndown_tokens:,.0f}</td>
            <td>${order.cost_per_verified_outcome_usd or 0.0:.4f}</td>
          </tr>
          <tr>
            <td><code>{catalog.apphub_application_id}</code></td>
            <td>{catalog.total_traces}</td>
            <td>{catalog.rule0_pass_rate * 100:.1f}%</td>
            <td><span class="badge">{catalog.calibration_status}</span></td>
            <td>NULL (Fail-Closed)</td>
            <td>{catalog.total_weighted_burndown_tokens:,.0f}</td>
            <td>NULL</td>
          </tr>
        </tbody>
      </table>
      <p style="margin-top: 16px; color: var(--muted); font-size: 0.88rem;">
        <strong>Prefix-Cache Diagnostics:</strong> Observed hit rate {dil.observed_cache_hit_ratio * 100:.1f}% across {dil.distinct_prompt_variants} prompt variants. {dil.recommendation}
      </p>
    </div>

    <div class="card" style="margin-top: 24px;">
      <h2>FOCUS 1.2 Two-Pool Chargeback Allocation</h2>
      <table>
        <thead>
          <tr>
            <th>Allocation Owner</th>
            <th>Cost Center</th>
            <th>Weighted Burndown</th>
            <th>Share</th>
            <th>Utilized PT</th>
            <th>Idle Capacity Tax</th>
            <th>PAYG Overflow</th>
            <th>Total Allocated</th>
          </tr>
        </thead>
        <tbody>
          {focus_html_rows}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_doc, encoding="utf-8")
    return output_path
