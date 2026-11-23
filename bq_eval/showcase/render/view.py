"""Typed, pure-view presentation models for the GM3 Light Workbench.

Separates view construction from HTML serialization so every numeric cell, calibration
badge, and fail-closed `NULL` string is deterministically testable without engine imports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from bq_eval.contract.results import (
    LiveFleetReceipt,
    ModelOption,
    ParetoEvalResult,
    ShowcaseRunArtifacts,
    SQLTraceEvalSummary,
)

BadgeTone = Literal["info", "ok", "warn", "err"]


class BadgeView(BaseModel):
    """Semantic status pill rendered in headers and table cells."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    tone: BadgeTone = "info"


class CellView(BaseModel):
    """Typed table cell supporting monospace code, numeric alignment, or status badges."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    align_right: bool = False
    code: bool = False
    strong: bool = False
    badge_tone: BadgeTone | None = None


class ColumnHeaderView(BaseModel):
    """Typed table column header."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    align_right: bool = False


class TableView(BaseModel):
    """Typed data table rendered inside a GM3 card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    columns: tuple[ColumnHeaderView, ...]
    rows: tuple[tuple[CellView, ...], ...]


class BannerView(BaseModel):
    """Callout banner displayed below a card's table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    tone: BadgeTone = "info"


class CardView(BaseModel):
    """Section card containing a heading, subtitle, optional table, and optional banner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    subtitle: str
    table: TableView | None = None
    banner: BannerView | None = None


class KpiCardView(BaseModel):
    """Top-row headline KPI card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    value: str
    caption: str
    badge: BadgeView | None = None


class TabView(BaseModel):
    """Single workbench tab panel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    panel_id: str
    label: str
    cards: tuple[CardView, ...]


class PageView(BaseModel):
    """Complete view model for the 4-tab GM3 Light BQ Eval & FinOps Workbench."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_title: str
    brand_heading: str
    brand_subtitle: str
    active_corpus_id: str
    corpus_choices: tuple[tuple[str, str], ...]
    header_badges: tuple[BadgeView, ...]
    kpi_cards: tuple[KpiCardView, ...]
    tabs: tuple[TabView, ...]
    live_api: bool = False


def format_raw_judge_pass(summary: SQLTraceEvalSummary) -> str:
    """Format raw judge pass rate without coalescing `None` to `0.0%`."""
    if summary.raw_judge_pass_rate is None:
        return "NULL"
    return f"{summary.raw_judge_pass_rate * 100:.1f}%"


def format_calibrated_pass(summary: SQLTraceEvalSummary) -> str:
    """Format Rogan-Gladen calibrated pass rate + 95% CI, failing closed on `None`."""
    if summary.calibrated_pass_rate is None:
        return "NULL (Fail-Closed)"
    if summary.calibrated_ci_95_lower is not None and summary.calibrated_ci_95_upper is not None:
        return (
            f"{summary.calibrated_pass_rate * 100:.2f}% "
            f"[{summary.calibrated_ci_95_lower * 100:.2f}%, {summary.calibrated_ci_95_upper * 100:.2f}%]"
        )
    return f"{summary.calibrated_pass_rate * 100:.2f}%"


def format_cpvo_usd(cpvo_usd: float | None) -> str:
    """Format Cost per Verified Outcome, failing closed on `None`."""
    if cpvo_usd is None:
        return "NULL (Fail-Closed)"
    return f"${cpvo_usd:.4f}"


def format_irr_gate_verdict(
    gwet_ac2: float | None,
    passes_irr_gate: bool | None = None,
) -> tuple[str, str, BadgeTone]:
    """Derive IRR value, gate caption, and badge tone strictly from data."""
    if gwet_ac2 is None:
        return ("NULL (Unjudged)", "UNJUDGED — no rater pairs evaluated (fail-closed)", "warn")
    passed = passes_irr_gate if passes_irr_gate is not None else (gwet_ac2 >= 0.80)
    if passed:
        return (f"{gwet_ac2:.4f}", "PASS (>= 0.8000 calibration gate)", "ok")
    return (f"{gwet_ac2:.4f}", "FAIL (< 0.8000 calibration gate — fail-closed)", "err")


def _status_badge_tone(status: str) -> BadgeTone:
    if status == "CALIBRATED":
        return "ok"
    if status == "UNCALIBRATED":
        return "err"
    return "warn"


def _build_summaries_table(summaries: dict[str, SQLTraceEvalSummary]) -> TableView:
    cols = (
        ColumnHeaderView(label="Application ID"),
        ColumnHeaderView(label="Rubric"),
        ColumnHeaderView(label="Traces", align_right=True),
        ColumnHeaderView(label="Rule 0 Pass", align_right=True),
        ColumnHeaderView(label="Status"),
        ColumnHeaderView(label="Raw Judge", align_right=True),
        ColumnHeaderView(label="Calibrated Pass [95% CI]", align_right=True),
        ColumnHeaderView(label="IRR (AC2)", align_right=True),
        ColumnHeaderView(label="Weighted Burndown", align_right=True),
        ColumnHeaderView(label="Total Spend", align_right=True),
        ColumnHeaderView(label="CPVO (USD)", align_right=True),
    )
    rows: list[tuple[CellView, ...]] = []
    for summary in summaries.values():
        irr_text = f"{summary.gwet_ac2:.4f}" if summary.gwet_ac2 is not None else "NULL"
        rows.append(
            (
                CellView(text=summary.apphub_application_id, code=True),
                CellView(text=summary.rubric_id, code=True),
                CellView(text=f"{summary.total_traces:,}", align_right=True),
                CellView(text=f"{summary.rule0_pass_rate * 100:.1f}%", align_right=True),
                CellView(
                    text=summary.calibration_status,
                    badge_tone=_status_badge_tone(summary.calibration_status),
                ),
                CellView(text=format_raw_judge_pass(summary), align_right=True),
                CellView(text=format_calibrated_pass(summary), align_right=True),
                CellView(text=irr_text, align_right=True),
                CellView(
                    text=f"{summary.total_weighted_burndown_tokens:,.0f}",
                    align_right=True,
                ),
                CellView(text=f"${summary.total_spend_usd:.4f}", align_right=True),
                CellView(
                    text=format_cpvo_usd(summary.cost_per_verified_outcome_usd),
                    align_right=True,
                    strong=summary.cost_per_verified_outcome_usd is not None,
                ),
            )
        )
    return TableView(columns=cols, rows=tuple(rows))


def _build_pareto_table(pareto: ParetoEvalResult) -> TableView:
    frontier_ids = {opt.config_id for opt in pareto.pareto_frontier}
    rec_id = pareto.recommended_default.config_id if pareto.recommended_default else None

    cols = (
        ColumnHeaderView(label="Config ID"),
        ColumnHeaderView(label="Model"),
        ColumnHeaderView(label="Rule 0 Pass", align_right=True),
        ColumnHeaderView(label="Fabrication Rate", align_right=True),
        ColumnHeaderView(label="Calibrated Pass", align_right=True),
        ColumnHeaderView(label="P95 Latency", align_right=True),
        ColumnHeaderView(label="CPVO (USD)", align_right=True),
        ColumnHeaderView(label="Disposition"),
    )

    def _row_for_option(opt: ModelOption, *, eligible: bool) -> tuple[CellView, ...]:
        if not eligible:
            reasons: list[str] = []
            if opt.fabrication_rate > 0.0:
                reasons.append(f"fabrication {opt.fabrication_rate * 100:.1f}% > 0.0%")
            if opt.rule0_pass_rate < 0.98:
                reasons.append(f"Rule 0 {opt.rule0_pass_rate * 100:.1f}% < 98.0%")
            disp_label = f"DISQUALIFIED ({', '.join(reasons) or 'gate failed'})"
            disp_tone: BadgeTone = "err"
        elif opt.config_id == rec_id:
            disp_label = "RECOMMENDED FRONTIER"
            disp_tone = "ok"
        elif opt.config_id in frontier_ids:
            disp_label = "PARETO FRONTIER"
            disp_tone = "info"
        else:
            disp_label = "DOMINATED"
            disp_tone = "warn"

        return (
            CellView(text=opt.config_id, code=True),
            CellView(text=opt.model_name, code=True),
            CellView(text=f"{opt.rule0_pass_rate * 100:.1f}%", align_right=True),
            CellView(text=f"{opt.fabrication_rate * 100:.2f}%", align_right=True),
            CellView(text=f"{opt.calibrated_pass_rate * 100:.1f}%", align_right=True),
            CellView(text=f"{opt.p95_latency_ms:,.0f} ms", align_right=True),
            CellView(
                text=f"${opt.cost_per_verified_outcome_usd:.4f}",
                align_right=True,
                strong=(opt.config_id == rec_id),
            ),
            CellView(text=disp_label, badge_tone=disp_tone),
        )

    rows = [
        *(_row_for_option(opt, eligible=True) for opt in pareto.eligible_options),
        *(_row_for_option(opt, eligible=False) for opt in pareto.disqualified_options),
    ]
    return TableView(columns=cols, rows=tuple(rows))


def _build_live_receipt_cards(receipt: LiveFleetReceipt) -> tuple[CardView, ...]:
    kyc = receipt.kyc_cohort
    kyc_card = CardView(
        title=f"Empirical KYC 2-Stage Funnel & Argon Calibration ({receipt.run_id})",
        subtitle=(
            f"{receipt.labeled_fleet_cases:,} labeled fleet cases · "
            f"{receipt.cumulative_closed_outcomes:,} closed outcomes · "
            f"{receipt.cumulative_allspans_count:,} spans "
            f"(lag p50={receipt.allspans_ingestion_lag_p50_ms:.0f} ms, "
            f"p95={receipt.allspans_ingestion_lag_p95_ms / 1000.0:.2f} s)"
        ),
        table=TableView(
            columns=(
                ColumnHeaderView(label="Metric"),
                ColumnHeaderView(label="Measured Value", align_right=True),
                ColumnHeaderView(label="Verification Note"),
            ),
            rows=(
                (
                    CellView(text="Step 1 Rule-Check Rejects"),
                    CellView(
                        text=(
                            f"{kyc.structural_step1_rejects} structural + "
                            f"{kyc.po_box_step1_traps_caught} PO Box + "
                            f"{kyc.ocr_step1_rejects} OCR"
                        ),
                        align_right=True,
                        strong=True,
                    ),
                    # The receipt records reject counts by reason, not a planted total,
                    # so these are counts rather than caught/planted ratios.
                    CellView(
                        text=(
                            f"{kyc.structural_step1_rejects + kyc.po_box_step1_traps_caught + kyc.ocr_step1_rejects}"
                            f" of {kyc.total_cases} cases rejected at $0 judge spend"
                        )
                    ),
                ),
                (
                    CellView(text="Step 2 Semantic Trap Recall (Flash)"),
                    CellView(
                        text=(
                            f"{kyc.step2_semantic_traps_caught}/{kyc.step2_semantic_traps_total} "
                            f"({kyc.trap_recall * 100:.1f}% "
                            f"[{kyc.trap_recall_wilson_ci_95[0] * 100:.1f}%, "
                            f"{kyc.trap_recall_wilson_ci_95[1] * 100:.1f}%])"
                        ),
                        align_right=True,
                        strong=True,
                    ),
                    CellView(
                        text=(
                            f"{kyc.false_positives}/{kyc.clean_cases_evaluated} false positives; "
                            f"{kyc.held_out_traps_caught}/{kyc.held_out_traps_total} held-out traps"
                        )
                    ),
                ),
                (
                    CellView(text="Flash vs. Argon Reference Agreement"),
                    CellView(
                        text=f"{kyc.flash_argon_binary_agreed}/{kyc.argon_calibration_pairs} binary pass",
                        align_right=True,
                        strong=True,
                    ),
                    CellView(
                        text="Model-vs-model reference check (regulated MRM still requires ~100 human SME labels)"
                    ),
                ),
            ),
        ),
    )

    rfp_card = CardView(
        title="Empirical RFP Modality & >200K Context Cliff",
        subtitle="Comparing structured A2A exchange, workhorse Flash PDF extraction, and >200K Argon PDF cliff.",
        table=TableView(
            columns=(
                ColumnHeaderView(label="Source Mode"),
                ColumnHeaderView(label="Agent"),
                ColumnHeaderView(label="Verified Outcomes", align_right=True),
                ColumnHeaderView(label="Input Tokens", align_right=True),
                ColumnHeaderView(label="AI Cost / Outcome", align_right=True),
                ColumnHeaderView(label="Total Cost / Outcome", align_right=True),
            ),
            rows=tuple(
                (
                    CellView(text=m.source_mode, code=True),
                    CellView(text=m.agent_name, code=True),
                    CellView(text=f"{m.verified_outcomes:,}", align_right=True),
                    CellView(text=f"{m.input_tokens:,}", align_right=True),
                    CellView(
                        text=f"${m.ai_cost_per_verified_outcome_usd:.5f}",
                        align_right=True,
                        strong=True,
                    ),
                    CellView(
                        text=f"${m.total_cost_per_verified_outcome_usd:.5f}",
                        align_right=True,
                    ),
                )
                for m in receipt.rfp_modalities
            ),
        ),
    )

    sl = receipt.skill_lineage
    skill_card = CardView(
        title=f"Compiled Skill Lineage ROI ({sl.skill_id})",
        subtitle=(
            f"Version {sl.day1_version} to {sl.day2_version} ({sl.cpvo_reduction_pct:.1f}% CPVO reduction, "
            f"excluding human reviewer wages)."
        ),
        table=TableView(
            columns=(
                ColumnHeaderView(label="Skill Version"),
                ColumnHeaderView(label="CPVO (USD)", align_right=True),
                ColumnHeaderView(label="Human Reviews / Outcome", align_right=True),
                ColumnHeaderView(label="Avg Cycle Time", align_right=True),
                ColumnHeaderView(label="Policy Gate Outcomes"),
            ),
            rows=(
                (
                    CellView(text=sl.day1_version, code=True),
                    CellView(text=f"${sl.day1_cpvo_usd:.5f}", align_right=True),
                    CellView(text=f"{sl.day1_human_reviews_per_outcome:.2f}", align_right=True),
                    CellView(text=f"{sl.day1_avg_cycle_seconds:.2f} s", align_right=True),
                    CellView(text="Baseline Argon escalation + mandatory human review"),
                ),
                (
                    CellView(text=sl.day2_version, code=True),
                    CellView(text=f"${sl.day2_cpvo_usd:.6f}", align_right=True, strong=True),
                    CellView(text=f"{sl.day2_human_reviews_per_outcome:.2f}", align_right=True),
                    CellView(text=f"{sl.day2_avg_cycle_seconds:.2f} s", align_right=True),
                    CellView(
                        text=(
                            f"{sl.day2_in_policy_auto_approved}/{sl.day2_in_policy_total} in-policy auto, "
                            f"{sl.day2_out_of_policy_escalated}/{sl.day2_out_of_policy_total} out-of-policy escalated"
                        )
                    ),
                ),
            ),
        ),
    )

    attr_card = CardView(
        title="App Hub Attribution Coverage & Unregistered Shadow Isolation",
        subtitle="Fail-closed fallback isolates unregistered services to unregistered_app.",
        table=TableView(
            columns=(
                ColumnHeaderView(label="App Hub Application ID"),
                ColumnHeaderView(label="Total Traces", align_right=True),
                ColumnHeaderView(label="Attributed Spend", align_right=True),
                ColumnHeaderView(label="Unattributed Spend", align_right=True),
                ColumnHeaderView(label="Coverage", align_right=True),
                ColumnHeaderView(label="Gate"),
            ),
            rows=tuple(
                (
                    CellView(text=c.apphub_application_id, code=True),
                    CellView(text=f"{c.total_traces:,}", align_right=True),
                    CellView(text=f"${c.attributed_spend_usd:.4f}", align_right=True),
                    CellView(text=f"${c.unattributed_spend_usd:.5f}", align_right=True),
                    CellView(text=f"{c.attribution_coverage_pct:.1f}%", align_right=True),
                    CellView(
                        text="PASS" if c.passes_coverage_gate else "ISOLATED SHADOW",
                        badge_tone="ok" if c.passes_coverage_gate else "warn",
                    ),
                )
                for c in receipt.attribution_cohorts
            ),
        ),
    )
    return (kyc_card, rfp_card, skill_card, attr_card)


def build_showcase_page_view(
    artifacts: ShowcaseRunArtifacts,
    *,
    live_api: bool = False,
) -> PageView:
    """Construct the complete 4-tab `PageView` from `ShowcaseRunArtifacts`."""
    calibrated_primary = next(
        (
            s
            for s in artifacts.summaries.values()
            if s.calibration_status == "CALIBRATED" and s.calibrated_pass_rate is not None
        ),
        None,
    )
    if calibrated_primary is not None and calibrated_primary.calibrated_pass_rate is not None:
        cal_val = f"{calibrated_primary.calibrated_pass_rate * 100:.2f}%"
        if (
            calibrated_primary.calibrated_ci_95_lower is not None
            and calibrated_primary.calibrated_ci_95_upper is not None
        ):
            cal_cap = (
                f"{calibrated_primary.apphub_application_id} · 95% CI "
                f"[{calibrated_primary.calibrated_ci_95_lower * 100:.2f}%, "
                f"{calibrated_primary.calibrated_ci_95_upper * 100:.2f}%]"
            )
        else:
            cal_cap = f"{calibrated_primary.apphub_application_id} · Rogan-Gladen calibrated"
        cal_badge = BadgeView(label="CALIBRATED", tone="ok")
    else:
        cal_val = "NULL (Fail-Closed)"
        cal_cap = "No calibrated application in corpus (fail-closed)"
        cal_badge = BadgeView(label="FAIL-CLOSED", tone="warn")

    primary_passes_gate = (
        calibrated_primary.passes_irr_gate if calibrated_primary is not None else None
    )
    irr_val, irr_cap, irr_tone = format_irr_gate_verdict(artifacts.gwet_ac2, primary_passes_gate)

    roi = artifacts.eval_roi
    dil = artifacts.cache_dilution
    plan = artifacts.off_peak_plan

    kpi_cards = (
        KpiCardView(
            label="Calibrated True Pass Rate",
            value=cal_val,
            caption=cal_cap,
            badge=cal_badge,
        ),
        KpiCardView(
            label="3-Tier Eval FinOps Savings",
            value=f"{roi.savings_pct:.1f}%",
            caption=(
                f"${roi.net_savings_usd:,.2f} saved vs. ${roi.baseline_100pct_judge_usd:,.2f} "
                f"({roi.tier2_total_judged_traces:,}/{roi.total_production_traces:,} judged)"
            ),
            badge=BadgeView(label="TIERED FUNNEL", tone="info"),
        ),
        KpiCardView(
            label="Ordinal IRR (Gwet's AC2)",
            value=irr_val,
            caption=irr_cap,
            badge=BadgeView(
                label="PASS" if irr_tone == "ok" else ("FAIL" if irr_tone == "err" else "UNJUDGED"),
                tone=irr_tone,
            ),
        ),
        KpiCardView(
            label="Off-Peak PT Valley Coverage",
            value=f"{plan.zero_marginal_cost_coverage_pct:.1f}%",
            caption=(
                f"{plan.scheduled_in_free_pt_valley_tokens:,.0f}/"
                f"{plan.total_batch_eval_tokens_requested:,.0f} tokens (${plan.avoided_payg_cost_usd:.2f} saved)"
            ),
            badge=BadgeView(label="WATER-FILLING", tone="ok"),
        ),
    )

    quality_cards: list[CardView] = [
        CardView(
            title=f"Application Quality & Cost per Verified Outcome ({artifacts.corpus_id})",
            subtitle=(
                "Evaluated where traces land: deterministic SQL screening on 100% of traces, "
                "StratPPI-weighted Rogan-Gladen calibration, and fail-closed NULL guards."
            ),
            table=_build_summaries_table(artifacts.summaries),
        )
    ]
    if artifacts.offline_twin_summaries:
        quality_cards.append(
            CardView(
                title="50-Trace Offline SQLite Reference Twin",
                subtitle="Deterministic 2-application reference twin verifying < 1e-12 SQLite-vs-Python parity.",
                table=_build_summaries_table(artifacts.offline_twin_summaries),
            )
        )

    roi_card = CardView(
        title="3-Tier Evaluation Funnel FinOps ROI",
        subtitle="Comparing 100% LLM-as-a-Judge spend against deterministic SQL screening + canary/tail sampling.",
        table=TableView(
            columns=(
                ColumnHeaderView(label="Funnel Stage"),
                ColumnHeaderView(label="Evaluated Traces", align_right=True),
                ColumnHeaderView(label="LLM Judge Spend (USD)", align_right=True),
                ColumnHeaderView(label="Notes"),
            ),
            rows=(
                (
                    CellView(text="Baseline (100% LLM-as-a-Judge)"),
                    CellView(text=f"{roi.total_production_traces:,}", align_right=True),
                    CellView(text=f"${roi.baseline_100pct_judge_usd:,.2f}", align_right=True),
                    CellView(text="Every production trace sent to LLM judge"),
                ),
                (
                    CellView(text="Tier 1 ($0 Deterministic SQL Checks)"),
                    CellView(text=f"{roi.tier1_sql_evaluated_traces:,}", align_right=True),
                    CellView(text="$0.00", align_right=True, strong=True),
                    CellView(
                        text="Screens schema, tool status, loop count, and structural policies"
                    ),
                ),
                (
                    CellView(text="Tier 2 Stratified Judge (Canary + Error/SLA Tail)"),
                    CellView(text=f"{roi.tier2_total_judged_traces:,}", align_right=True),
                    CellView(
                        text=f"${roi.stratified_3tier_judge_usd:,.2f}",
                        align_right=True,
                        strong=True,
                    ),
                    CellView(
                        text=(
                            f"{roi.tier2_canary_traces:,} healthy canary + "
                            f"{roi.tier2_error_tail_traces:,} error/SLA tail"
                        )
                    ),
                ),
            ),
        ),
        banner=BannerView(
            text=(
                f"Net evaluation judge savings: ${roi.net_savings_usd:,.2f} "
                f"({roi.savings_pct:.1f}% reduction vs. 100% LLM judging)."
            ),
            tone="ok",
        ),
    )

    receipt_cards: list[CardView] = [roi_card]
    if artifacts.live_receipt is not None:
        receipt_cards.extend(_build_live_receipt_cards(artifacts.live_receipt))

    rec = artifacts.pareto.recommended_default
    pareto_banner_text = (
        f"Recommended default routing: {rec.config_id} ({rec.model_name}, "
        f"CPVO ${rec.cost_per_verified_outcome_usd:.4f}, calibrated pass {rec.calibrated_pass_rate * 100:.1f}%)."
        if rec is not None
        else "No candidate satisfied zero-fabrication and Rule-0 >= 98.0% eligibility gates."
    )
    pareto_cards = (
        CardView(
            title=f"Prefix-Cache 1/N Dilution Diagnostics ({dil.apphub_application_id})",
            subtitle="Detects dynamic system-prompt interpolation that fragments implicit/explicit prefix caching.",
            table=TableView(
                columns=(
                    ColumnHeaderView(label="Application ID"),
                    ColumnHeaderView(label="Prompt Variants (N)", align_right=True),
                    ColumnHeaderView(label="1/N Ceiling", align_right=True),
                    ColumnHeaderView(label="Observed Hit Ratio", align_right=True),
                    ColumnHeaderView(label="Target Hit Ratio", align_right=True),
                    ColumnHeaderView(label="Recoverable Burndown", align_right=True),
                    ColumnHeaderView(label="Recoverable Window USD", align_right=True),
                ),
                rows=(
                    (
                        CellView(text=dil.apphub_application_id, code=True),
                        CellView(text=f"{dil.distinct_prompt_variants}", align_right=True),
                        CellView(
                            text=f"{dil.theoretical_dilution_ceiling * 100:.1f}%", align_right=True
                        ),
                        CellView(
                            text=f"{dil.observed_cache_hit_ratio * 100:.1f}%", align_right=True
                        ),
                        CellView(text=f"{dil.target_cache_hit_ratio * 100:.1f}%", align_right=True),
                        CellView(
                            text=f"{dil.recoverable_weighted_burndown_tokens:,.0f}",
                            align_right=True,
                        ),
                        CellView(
                            text=f"${dil.recoverable_window_usd:.4f}",
                            align_right=True,
                            strong=True,
                        ),
                    ),
                ),
            ),
            banner=BannerView(
                text=dil.recommendation,
                tone="warn" if dil.dilution_detected else "ok",
            ),
        ),
        CardView(
            title="Zero-Fabrication (0.00%) Cost-vs-Quality Pareto Frontier",
            subtitle="Disqualifies any model option with fabrication_rate > 0.0% or rule0_pass_rate < 98.0%.",
            table=_build_pareto_table(artifacts.pareto),
            banner=BannerView(
                text=pareto_banner_text,
                tone="ok" if rec is not None else "warn",
            ),
        ),
    )

    finops_cards = (
        CardView(
            title="FOCUS 1.2 Two-Pool Provisioned Throughput Chargeback",
            subtitle=(
                "Separates utilized PT burndown from unutilized idle capacity tax "
                "(platform-shared-reservation) and PAYG spillover."
            ),
            table=TableView(
                columns=(
                    ColumnHeaderView(label="Allocation Owner"),
                    ColumnHeaderView(label="Cost Center"),
                    ColumnHeaderView(label="Weighted Burndown", align_right=True),
                    ColumnHeaderView(label="Share", align_right=True),
                    ColumnHeaderView(label="Utilized PT", align_right=True),
                    ColumnHeaderView(label="Idle Capacity Tax", align_right=True),
                    ColumnHeaderView(label="PAYG Overflow", align_right=True),
                    ColumnHeaderView(label="Total Allocated", align_right=True),
                ),
                rows=tuple(
                    (
                        CellView(text=r.apphub_application_id, code=True),
                        CellView(text=r.cost_center, code=True),
                        CellView(text=f"{r.weighted_burndown_tokens:,.0f}", align_right=True),
                        CellView(text=f"{r.utilization_share * 100:.1f}%", align_right=True),
                        CellView(text=f"${r.utilized_pt_cost_usd:.2f}", align_right=True),
                        CellView(
                            text=f"${r.unutilized_idle_capacity_tax_usd:.2f}", align_right=True
                        ),
                        CellView(text=f"${r.payg_overflow_cost_usd:.2f}", align_right=True),
                        CellView(
                            text=f"${r.total_allocated_cost_usd:.2f}",
                            align_right=True,
                            strong=True,
                        ),
                    )
                    for r in artifacts.focus_chargeback
                ),
            ),
        ),
        CardView(
            title="Off-Peak Provisioned Throughput Valley Water-Filling",
            subtitle="Equalizes total load from the deepest off-peak PT valleys upward before spilling to PAYG.",
            table=TableView(
                columns=(
                    ColumnHeaderView(label="Hour Slot"),
                    ColumnHeaderView(
                        label="Scheduled Batch Eval Burndown Tokens", align_right=True
                    ),
                ),
                rows=tuple(
                    (
                        CellView(text=f"Hour {idx:02d}", code=True),
                        CellView(text=f"{tokens:,.1f}", align_right=True),
                    )
                    for idx, tokens in enumerate(plan.hourly_scheduled_eval_tokens)
                ),
            ),
            banner=BannerView(
                text=(
                    f"Scheduled {plan.scheduled_in_free_pt_valley_tokens:,.0f} of "
                    f"{plan.total_batch_eval_tokens_requested:,.0f} batch eval tokens into zero-marginal-cost "
                    f"PT valleys ({plan.zero_marginal_cost_coverage_pct:.1f}% coverage; "
                    f"${plan.avoided_payg_cost_usd:.2f} PAYG avoided; "
                    f"{plan.remaining_payg_spillover_tokens:,.0f} spillover tokens)."
                ),
                tone="ok" if plan.remaining_payg_spillover_tokens == 0.0 else "info",
            ),
        ),
    )

    tabs = (
        TabView(panel_id="tab-quality", label="1. Quality & CPVO", cards=tuple(quality_cards)),
        TabView(
            panel_id="tab-receipts",
            label="2. Live Fleet & 3-Tier ROI",
            cards=tuple(receipt_cards),
        ),
        TabView(panel_id="tab-pareto", label="3. Prefix Cache & Pareto", cards=pareto_cards),
        TabView(panel_id="tab-finops", label="4. FOCUS 1.2 & PT Valleys", cards=finops_cards),
    )

    return PageView(
        page_title="BQ Eval & FinOps Workbench (bq-eval)",
        brand_heading="BQ Eval & FinOps Workbench",
        brand_subtitle=(
            "Evaluate AI agents where traces land · Tiered SQL + Calibrated LLM Judges · "
            "Cost per Verified Outcome & FOCUS 1.2 Chargeback"
        ),
        active_corpus_id=artifacts.corpus_id,
        corpus_choices=(
            ("m2-live", "m2-live (343-Case Fleet Receipt)"),
            ("golden-4app", "golden-4app (4-App Seed Corpus)"),
            ("retail-2app", "retail-2app (50-Trace Twin)"),
        ),
        header_badges=(
            BadgeView(label=f"Corpus: {artifacts.corpus_id}", tone="info"),
            BadgeView(
                label="Live HTTP Server" if live_api else "Static HTML Export",
                tone="ok" if live_api else "info",
            ),
        ),
        kpi_cards=kpi_cards,
        tabs=tabs,
        live_api=live_api,
    )
