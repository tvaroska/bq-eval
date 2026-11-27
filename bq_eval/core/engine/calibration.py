"""Rogan-Gladen Prevalence Correction, 95% Delta/Wilson CIs, Quadratic Gwet's AC2, and ADR-049 Gates."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from bq_eval.core.contract.results import CalibrationResult, ConfusionMatrix, RoganGladenResult

MIN_CALIBRATED_JUDGED_SAMPLES: int = 10
MIN_RL1_CALIBRATED_SAMPLES: int = 100
MIN_CALIBRATED_REF_PER_CLASS: int = 5
DEFAULT_BINARY_CUTPOINT_AGREEMENT: float = 0.85

RISK_TIER_AC2_THRESHOLDS: dict[str, float] = {
    "RL-1": 0.85,
    "RL-2": 0.85,
    "RL-3": 0.80,
    "RL-3a": 0.80,
    "RL-3b": 0.80,
    "RL-4": 0.75,
    "RL-5": 0.75,
}


@dataclass(frozen=True)
class AgreementGateVerdict:
    """Fail-closed ADR-049 agreement gate verdict across ordinal AC2, cut-point, and sample floors."""

    passes_irr_gate: bool
    judge_vs_sme_ac2: float
    sme_vs_sme_ac2: float | None
    binary_cutpoint_agreement: float
    paired_count: int
    required_ac2: float
    required_pairs: int


def required_ac2_for_risk_tier(risk_tier: str | None) -> float:
    """Return tiered AC2 agreement threshold (`0.85` RL-1/2, `0.80` RL-3*, `0.75` RL-4/5)."""
    if risk_tier is None:
        return 0.80
    return RISK_TIER_AC2_THRESHOLDS.get(risk_tier.strip(), 0.80)


def required_min_samples_for_risk_tier(risk_tier: str | None) -> int:
    """Return minimum judged / paired SME label floor (`100` for `RL-1`, `10` otherwise)."""
    if risk_tier is not None and risk_tier.strip() == "RL-1":
        return MIN_RL1_CALIBRATED_SAMPLES
    return MIN_CALIBRATED_JUDGED_SAMPLES


def compute_confusion_matrix(
    human_labels: Sequence[bool],
    judge_labels: Sequence[bool],
) -> ConfusionMatrix:
    """Compute binary confusion matrix between human ground truth and autorater."""
    if len(human_labels) != len(judge_labels) or not human_labels:
        raise ValueError("human_labels and judge_labels must be non-empty and equal length.")
    tp = sum(1 for h, j in zip(human_labels, judge_labels, strict=True) if h and j)
    fn = sum(1 for h, j in zip(human_labels, judge_labels, strict=True) if h and not j)
    tn = sum(1 for h, j in zip(human_labels, judge_labels, strict=True) if not h and not j)
    fp = sum(1 for h, j in zip(human_labels, judge_labels, strict=True) if not h and j)
    return ConfusionMatrix(tp=tp, fn=fn, tn=tn, fp=fp)


def compute_binary_cutpoint_agreement(
    scores_a: Sequence[int],
    scores_b: Sequence[int],
    *,
    passing_score: int = 3,
) -> float:
    """Compute exact pass/fail cut-point agreement (`>= passing_score`) across paired ordinal scores."""
    if len(scores_a) != len(scores_b) or not scores_a:
        raise ValueError("Rater score sequences must be non-empty and equal length.")
    matches = sum(
        1
        for a, b in zip(scores_a, scores_b, strict=True)
        if (a >= passing_score) is (b >= passing_score)
    )
    return matches / len(scores_a)


def _validate_rg_inputs(
    observed_pass_rate: float,
    sample_size: int,
    z_value: float,
    *,
    confusion: ConfusionMatrix,
    risk_tier: str | None = None,
) -> None:
    min_samples = required_min_samples_for_risk_tier(risk_tier)
    if sample_size < min_samples:
        raise ValueError(
            f"sample_size ({sample_size}) must be >= {min_samples} for Rogan-Gladen calibration."
        )
    if not math.isfinite(observed_pass_rate) or not (0.0 <= observed_pass_rate <= 1.0):
        raise ValueError("observed_pass_rate must be finite and in [0.0, 1.0].")
    if not math.isfinite(z_value) or z_value <= 0.0:
        raise ValueError("z_value must be finite and positive.")
    n_pos = confusion.tp + confusion.fn
    n_neg = confusion.tn + confusion.fp
    if n_pos < MIN_CALIBRATED_REF_PER_CLASS or n_neg < MIN_CALIBRATED_REF_PER_CLASS:
        raise ValueError(
            f"ConfusionMatrix requires at least {MIN_CALIBRATED_REF_PER_CLASS} positive and "
            f"{MIN_CALIBRATED_REF_PER_CLASS} negative reference labels (got pos={n_pos}, neg={n_neg})."
        )
    if confusion.total < min_samples:
        raise ValueError(
            f"ConfusionMatrix total ({confusion.total}) must be >= {min_samples} paired labels."
        )


def rogan_gladen_wilson_ci(  # noqa: PLR0913
    observed_pass_rate: float,
    sample_size: int,
    confusion: ConfusionMatrix,
    z_value: float = 1.959963984540054,
    *,
    stratified_var_p_obs: float | None = None,
    kish_eff_n: float | None = None,
    risk_tier: str | None = None,
) -> tuple[float, float]:
    """Compute Lang & Reiczigel (2014) Eq. (6), (7), (16), (17) Rogan-Gladen CI (boundary-safe)."""
    _validate_rg_inputs(
        observed_pass_rate,
        sample_size,
        z_value,
        confusion=confusion,
        risk_tier=risk_tier,
    )
    if stratified_var_p_obs is not None and (
        not math.isfinite(stratified_var_p_obs) or stratified_var_p_obs < 0.0
    ):
        raise ValueError("stratified_var_p_obs must be finite and non-negative.")
    if kish_eff_n is not None and (not math.isfinite(kish_eff_n) or kish_eff_n <= 0.0):
        raise ValueError("kish_eff_n must be finite and positive.")
    n_pos = confusion.tp + confusion.fn
    n_neg = confusion.tn + confusion.fp

    if (
        stratified_var_p_obs is not None
        and stratified_var_p_obs > 0.0
        and 0.0 < observed_pass_rate < 1.0
    ):
        eff_n = max(1.0, (observed_pass_rate * (1.0 - observed_pass_rate)) / stratified_var_p_obs)
    elif kish_eff_n is not None:
        eff_n = max(1.0, kish_eff_n)
    else:
        eff_n = float(sample_size)

    z2 = z_value * z_value
    n_adj = eff_n + z2
    p_adj = (observed_pass_rate * eff_n + 0.5 * z2) / n_adj

    n_se_adj = n_pos + 2.0
    n_sp_adj = n_neg + 2.0
    se_adj = (confusion.tp + 1.0) / n_se_adj
    sp_adj = (confusion.tn + 1.0) / n_sp_adj
    j_adj = se_adj + sp_adj - 1.0
    if j_adj <= 1e-6:
        raise ValueError(
            f"Autorater is uncalibrated after Wilson adjustment (J' = {j_adj:.4f} <= 0)."
        )

    theta_adj = min(1.0, max(0.0, (p_adj + sp_adj - 1.0) / j_adj))
    var_p_adj = (p_adj * (1.0 - p_adj)) / n_adj
    var_se_adj = (se_adj * (1.0 - se_adj)) / n_se_adj
    var_sp_adj = (sp_adj * (1.0 - sp_adj)) / n_sp_adj

    # Lang & Reiczigel (2014) Eq. (17) center shift matching CRAN asht::prevSeSp.
    dp_shift = 2.0 * z2 * (theta_adj * var_se_adj - (1.0 - theta_adj) * var_sp_adj)
    se_theta = (
        math.sqrt(var_p_adj + (theta_adj**2) * var_se_adj + ((1.0 - theta_adj) ** 2) * var_sp_adj)
        / j_adj
    )
    ci_lower = min(1.0, max(0.0, theta_adj + dp_shift - z_value * se_theta))
    ci_upper = min(1.0, max(0.0, theta_adj + dp_shift + z_value * se_theta))
    return ci_lower, ci_upper


def rogan_gladen_calibrate(  # noqa: PLR0913
    observed_pass_rate: float,
    sample_size: int,
    confusion: ConfusionMatrix,
    z_value: float = 1.959963984540054,
    *,
    ci_method: Literal["delta", "wilson_plugin"] = "wilson_plugin",
    stratified_var_p_obs: float | None = None,
    kish_eff_n: float | None = None,
    risk_tier: str | None = None,
) -> RoganGladenResult:
    """Apply Rogan-Gladen prevalence adjustment with Lang & Reiczigel (2014) or Delta 95% CI."""
    _validate_rg_inputs(
        observed_pass_rate,
        sample_size,
        z_value,
        confusion=confusion,
        risk_tier=risk_tier,
    )

    tpr = confusion.sensitivity_tpr
    tnr = confusion.specificity_tnr
    j_stat = tpr + tnr - 1.0
    if j_stat <= 1e-6:
        raise ValueError(
            f"Autorater is uncalibrated (Youden's J = {j_stat:.4f} <= 0); "
            "cannot apply Rogan-Gladen division."
        )

    theta_rg = min(1.0, max(0.0, (observed_pass_rate + tnr - 1.0) / j_stat))

    if ci_method == "wilson_plugin":
        ci_lower, ci_upper = rogan_gladen_wilson_ci(
            observed_pass_rate=observed_pass_rate,
            sample_size=sample_size,
            confusion=confusion,
            z_value=z_value,
            stratified_var_p_obs=stratified_var_p_obs,
            kish_eff_n=kish_eff_n,
            risk_tier=risk_tier,
        )
    else:
        if stratified_var_p_obs is not None:
            if not math.isfinite(stratified_var_p_obs) or stratified_var_p_obs < 0.0:
                raise ValueError("stratified_var_p_obs must be finite and non-negative.")
            var_p_obs = stratified_var_p_obs
        else:
            var_p_obs = (observed_pass_rate * (1.0 - observed_pass_rate)) / sample_size
        n_pos = confusion.tp + confusion.fn
        n_neg = confusion.tn + confusion.fp
        var_tpr = (tpr * (1.0 - tpr)) / n_pos
        var_tnr = (tnr * (1.0 - tnr)) / n_neg
        se_theta = (
            math.sqrt(var_p_obs + (theta_rg**2) * var_tpr + ((1.0 - theta_rg) ** 2) * var_tnr)
            / j_stat
        )
        ci_lower = min(1.0, max(0.0, theta_rg - z_value * se_theta))
        ci_upper = min(1.0, max(0.0, theta_rg + z_value * se_theta))

    return RoganGladenResult(
        raw_pass_rate=observed_pass_rate,
        calibrated_pass_rate=theta_rg,
        ci_95_lower=ci_lower,
        ci_95_upper=ci_upper,
        sensitivity_tpr=tpr,
        specificity_tnr=tnr,
        youden_j=j_stat,
        sample_size=sample_size,
    )


def _validate_strata_triples(strata: Sequence[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    if not strata:
        raise ValueError("strata must be non-empty.")
    active: list[tuple[int, int, int]] = []
    for pop_n, judged_n, passed_n in strata:
        if pop_n < 0 or judged_n < 0:
            raise ValueError("Stratum population_count and judged_n must be non-negative.")
        if pop_n > 0 and judged_n == 0:
            raise ValueError(
                "Unjudged population stratum: every stratum with pop_n > 0 must have judged_n > 0."
            )
        if judged_n > 0 and pop_n == 0:
            raise ValueError("Each judged stratum must have population_count > 0.")
        if judged_n > pop_n:
            raise ValueError("judged_n cannot exceed population_count in any stratum.")
        if judged_n > 0:
            if not (0 <= passed_n <= judged_n):
                raise ValueError("Each stratum must satisfy 0 <= passed_n <= judged_n.")
            active.append((pop_n, judged_n, passed_n))
    if not active:
        raise ValueError("At least one stratum must have judged_n > 0.")
    return active


def stratified_rogan_gladen_calibrate(
    strata: Sequence[tuple[int, int, int]],
    confusion: ConfusionMatrix,
    z_value: float = 1.959963984540054,
    *,
    ci_method: Literal["delta", "wilson_plugin"] = "wilson_plugin",
    risk_tier: str | None = None,
) -> RoganGladenResult:
    """Apply StratPPI post-stratified Rogan-Gladen calibration across `(pop_n, judged_n, passed_n)` strata."""
    active = _validate_strata_triples(strata)
    total_pop = sum(pop_n for pop_n, _, _ in active)
    total_judged = sum(judged_n for _, judged_n, _ in active)

    weighted_p_obs = 0.0
    var_p_obs = 0.0
    kish_denom = 0.0
    for pop_n, judged_n, passed_n in active:
        w_s = pop_n / total_pop
        p_s = passed_n / judged_n
        weighted_p_obs += w_s * p_s
        var_p_obs += (w_s**2) * (p_s * (1.0 - p_s)) / judged_n
        kish_denom += (w_s**2) / judged_n

    kish_eff_n = 1.0 / kish_denom
    return rogan_gladen_calibrate(
        observed_pass_rate=weighted_p_obs,
        sample_size=total_judged,
        confusion=confusion,
        z_value=z_value,
        ci_method=ci_method,
        stratified_var_p_obs=var_p_obs,
        kish_eff_n=kish_eff_n,
        risk_tier=risk_tier,
    )


def stratum_specific_rogan_gladen_calibrate(
    strata: Sequence[tuple[int, int, int, ConfusionMatrix]],
    z_value: float = 1.959963984540054,
    *,
    risk_tier: str | None = None,
) -> RoganGladenResult:
    """Compute stratum-specific Rogan-Gladen prevalence using per-stratum `(Se_h, Sp_h)` (`ADR-033`)."""
    triples = _validate_strata_triples([(p, j, k) for p, j, k, _ in strata])
    active_cm = [cm for (p, j, _, cm) in strata if p > 0 and j > 0]
    total_pop = sum(p for p, _, _ in triples)
    total_judged = sum(j for _, j, _ in triples)
    pooled_cm = ConfusionMatrix(
        tp=sum(c.tp for c in active_cm),
        fn=sum(c.fn for c in active_cm),
        tn=sum(c.tn for c in active_cm),
        fp=sum(c.fp for c in active_cm),
    )
    _validate_rg_inputs(0.5, total_judged, z_value, confusion=pooled_cm, risk_tier=risk_tier)

    raw_weighted = 0.0
    theta_weighted = 0.0
    var_theta = 0.0
    z2 = z_value * z_value
    for (pop_n, judged_n, passed_n), cm_h in zip(triples, active_cm, strict=True):
        w_h = pop_n / total_pop
        p_h = passed_n / judged_n
        raw_weighted += w_h * p_h
        se_h = cm_h.sensitivity_tpr
        sp_h = cm_h.specificity_tnr
        j_h = se_h + sp_h - 1.0
        if j_h <= 1e-6:
            raise ValueError(f"Stratum autorater is uncalibrated (J_h = {j_h:.4f} <= 0).")
        theta_h = min(1.0, max(0.0, (p_h + sp_h - 1.0) / j_h))
        theta_weighted += w_h * theta_h

        p_adj = (passed_n + 0.5 * z2) / (judged_n + z2)
        n_pos_h = cm_h.tp + cm_h.fn
        n_neg_h = cm_h.tn + cm_h.fp
        se_adj = (cm_h.tp + 1.0) / (n_pos_h + 2.0)
        sp_adj = (cm_h.tn + 1.0) / (n_neg_h + 2.0)
        j_adj = se_adj + sp_adj - 1.0
        if j_adj <= 1e-6:
            raise ValueError(
                f"Stratum autorater uncalibrated after Wilson adjustment (J'_h = {j_adj:.4f})."
            )
        var_h = (
            (p_adj * (1.0 - p_adj)) / (judged_n + z2)
            + (theta_h**2) * (se_adj * (1.0 - se_adj)) / (n_pos_h + 2.0)
            + ((1.0 - theta_h) ** 2) * (sp_adj * (1.0 - sp_adj)) / (n_neg_h + 2.0)
        ) / (j_adj**2)
        var_theta += (w_h**2) * var_h

    se_joint = math.sqrt(var_theta)
    ci_lower = min(1.0, max(0.0, theta_weighted - z_value * se_joint))
    ci_upper = min(1.0, max(0.0, theta_weighted + z_value * se_joint))
    return RoganGladenResult(
        raw_pass_rate=raw_weighted,
        calibrated_pass_rate=min(1.0, max(0.0, theta_weighted)),
        ci_95_lower=ci_lower,
        ci_95_upper=ci_upper,
        sensitivity_tpr=pooled_cm.sensitivity_tpr,
        specificity_tnr=pooled_cm.specificity_tnr,
        youden_j=pooled_cm.youden_j,
        sample_size=total_judged,
    )


def compute_gwet_ac2(
    rater_a: Sequence[int],
    rater_b: Sequence[int],
    categories: Sequence[int] = (1, 2, 3, 4),
) -> float:
    """Compute quadratic-weighted Gwet's AC2 inter-rater reliability."""
    if len(rater_a) != len(rater_b) or not rater_a:
        raise ValueError("Rater sequences must be non-empty and equal length.")

    n = len(rater_a)
    q = len(categories)
    if q < 2:
        raise ValueError("At least 2 ordinal categories are required.")

    valid_categories = set(categories)
    invalid_scores = [
        s
        for s in (*rater_a, *rater_b)
        if isinstance(s, bool) or not isinstance(s, int) or s not in valid_categories
    ]
    if invalid_scores:
        raise ValueError(
            f"Rater scores {invalid_scores!r} outside allowed integer categories {tuple(categories)}."
        )

    span = float(max(categories) - min(categories))

    def weight(c1: int, c2: int) -> float:
        return 1.0 - ((float(c1 - c2) / span) ** 2)

    p_a = sum(weight(a, b) for a, b in zip(rater_a, rater_b, strict=True)) / n
    pi_k = {
        k: ((sum(1 for a in rater_a if a == k) / n) + (sum(1 for b in rater_b if b == k) / n)) / 2.0
        for k in categories
    }

    t_w = sum(weight(k, m) for k in categories for m in categories)
    p_e = (t_w / (q * (q - 1))) * sum(pi_k[k] * (1.0 - pi_k[k]) for k in categories)
    if abs(1.0 - p_e) < 1e-12:
        return 1.0
    return (p_a - p_e) / (1.0 - p_e)


def evaluate_calibration_agreement_gate(  # noqa: PLR0913
    human_ordinal_scores: Sequence[int],
    judge_ordinal_scores: Sequence[int],
    *,
    passing_score: int = 3,
    irr_threshold: float | None = None,
    cutpoint_threshold: float = DEFAULT_BINARY_CUTPOINT_AGREEMENT,
    risk_tier: str | None = None,
    rater_a_ordinal_scores: Sequence[int] | None = None,
    rater_b_ordinal_scores: Sequence[int] | None = None,
    categories: Sequence[int] = (1, 2, 3, 4),
) -> AgreementGateVerdict:
    """Evaluate ADR-049 fail-closed agreement gate across AC2, binary cut-point, and sample floors."""
    judge_ac2 = compute_gwet_ac2(human_ordinal_scores, judge_ordinal_scores, categories=categories)
    cutpoint = compute_binary_cutpoint_agreement(
        human_ordinal_scores, judge_ordinal_scores, passing_score=passing_score
    )
    sme_ac2: float | None = None
    dual_pairs = 0
    if rater_a_ordinal_scores is not None or rater_b_ordinal_scores is not None:
        if rater_a_ordinal_scores is None or rater_b_ordinal_scores is None:
            raise ValueError(
                "Both rater_a_ordinal_scores and rater_b_ordinal_scores must be supplied together."
            )
        dual_pairs = len(rater_a_ordinal_scores)
        sme_ac2 = compute_gwet_ac2(
            rater_a_ordinal_scores, rater_b_ordinal_scores, categories=categories
        )

    eff_ac2 = irr_threshold if irr_threshold is not None else required_ac2_for_risk_tier(risk_tier)
    min_pairs = required_min_samples_for_risk_tier(risk_tier)
    n_pos = sum(1 for s in human_ordinal_scores if s >= passing_score)
    n_neg = len(human_ordinal_scores) - n_pos
    passes = (
        len(human_ordinal_scores) >= min_pairs
        and n_pos >= MIN_CALIBRATED_REF_PER_CLASS
        and n_neg >= MIN_CALIBRATED_REF_PER_CLASS
        and judge_ac2 >= eff_ac2
        and cutpoint >= cutpoint_threshold
        and (sme_ac2 is None or (dual_pairs >= min_pairs and sme_ac2 >= eff_ac2))
    )
    return AgreementGateVerdict(
        passes_irr_gate=passes,
        judge_vs_sme_ac2=judge_ac2,
        sme_vs_sme_ac2=sme_ac2,
        binary_cutpoint_agreement=cutpoint,
        paired_count=len(human_ordinal_scores),
        required_ac2=eff_ac2,
        required_pairs=min_pairs,
    )


def build_calibration_result(  # noqa: PLR0913
    human_ordinal_scores: Sequence[int],
    judge_ordinal_scores: Sequence[int],
    observed_pass_rate: float,
    sample_size: int,
    passing_score: int = 3,
    irr_threshold: float | None = None,
    categories: Sequence[int] = (1, 2, 3, 4),
    *,
    risk_tier: str | None = None,
    rater_a_ordinal_scores: Sequence[int] | None = None,
    rater_b_ordinal_scores: Sequence[int] | None = None,
    cutpoint_threshold: float = DEFAULT_BINARY_CUTPOINT_AGREEMENT,
) -> CalibrationResult:
    """Build combined ADR-049 IRR gate and Rogan-Gladen calibration summary."""
    gate = evaluate_calibration_agreement_gate(
        human_ordinal_scores,
        judge_ordinal_scores,
        passing_score=passing_score,
        irr_threshold=irr_threshold,
        cutpoint_threshold=cutpoint_threshold,
        risk_tier=risk_tier,
        rater_a_ordinal_scores=rater_a_ordinal_scores,
        rater_b_ordinal_scores=rater_b_ordinal_scores,
        categories=categories,
    )
    confusion = compute_confusion_matrix(
        human_labels=[score >= passing_score for score in human_ordinal_scores],
        judge_labels=[score >= passing_score for score in judge_ordinal_scores],
    )
    rg = rogan_gladen_calibrate(
        observed_pass_rate=observed_pass_rate,
        sample_size=sample_size,
        confusion=confusion,
        risk_tier=risk_tier,
    )
    return CalibrationResult(
        gwet_ac2=gate.judge_vs_sme_ac2,
        sme_vs_sme_ac2=gate.sme_vs_sme_ac2,
        binary_cutpoint_agreement=gate.binary_cutpoint_agreement,
        passes_irr_gate=gate.passes_irr_gate,
        confusion_matrix=confusion,
        rogan_gladen=rg,
    )
