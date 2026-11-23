"""Rogan-Gladen Prevalence Correction, 95% Delta/Wilson CIs, and Quadratic Gwet's AC2."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from bq_eval.contract.results import CalibrationResult, ConfusionMatrix, RoganGladenResult


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


def _validate_rg_inputs(observed_pass_rate: float, sample_size: int, z_value: float) -> None:
    if sample_size <= 0:
        raise ValueError("sample_size must be positive for Rogan-Gladen calibration.")
    if not math.isfinite(observed_pass_rate) or not (0.0 <= observed_pass_rate <= 1.0):
        raise ValueError("observed_pass_rate must be finite and in [0.0, 1.0].")
    if not math.isfinite(z_value) or z_value <= 0.0:
        raise ValueError("z_value must be finite and positive.")


def rogan_gladen_wilson_ci(
    observed_pass_rate: float,
    sample_size: int,
    confusion: ConfusionMatrix,
    z_value: float = 1.959963984540054,
    *,
    stratified_var_p_obs: float | None = None,
) -> tuple[float, float]:
    """Compute Lang & Reiczigel (2014) Wilson plug-in Rogan-Gladen CI (boundary-safe)."""
    _validate_rg_inputs(observed_pass_rate, sample_size, z_value)
    if stratified_var_p_obs is not None and (
        not math.isfinite(stratified_var_p_obs) or stratified_var_p_obs < 0.0
    ):
        raise ValueError("stratified_var_p_obs must be finite and non-negative.")
    n_pos = confusion.tp + confusion.fn
    n_neg = confusion.tn + confusion.fp
    if n_pos == 0 or n_neg == 0:
        raise ValueError("ConfusionMatrix must have positive counts for both human classes.")

    if (
        stratified_var_p_obs is not None
        and stratified_var_p_obs > 0.0
        and 0.0 < observed_pass_rate < 1.0
    ):
        eff_n = max(1.0, (observed_pass_rate * (1.0 - observed_pass_rate)) / stratified_var_p_obs)
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

    se_theta = (
        math.sqrt(var_p_adj + (theta_adj**2) * var_se_adj + ((1.0 - theta_adj) ** 2) * var_sp_adj)
        / j_adj
    )
    ci_lower = min(1.0, max(0.0, theta_adj - z_value * se_theta))
    ci_upper = min(1.0, max(0.0, theta_adj + z_value * se_theta))
    return ci_lower, ci_upper


def rogan_gladen_calibrate(  # noqa: PLR0913
    observed_pass_rate: float,
    sample_size: int,
    confusion: ConfusionMatrix,
    z_value: float = 1.959963984540054,
    *,
    ci_method: Literal["delta", "wilson_plugin"] = "delta",
    stratified_var_p_obs: float | None = None,
) -> RoganGladenResult:
    """Apply Rogan-Gladen prevalence adjustment with Delta-method or Wilson plug-in 95% CI."""
    _validate_rg_inputs(observed_pass_rate, sample_size, z_value)

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


def stratified_rogan_gladen_calibrate(
    strata: Sequence[tuple[int, int, int]],
    confusion: ConfusionMatrix,
    z_value: float = 1.959963984540054,
    *,
    ci_method: Literal["delta", "wilson_plugin"] = "delta",
) -> RoganGladenResult:
    """Apply StratPPI post-stratified Rogan-Gladen calibration across `(pop_n, judged_n, passed_n)` strata."""
    if not strata:
        raise ValueError("strata must be non-empty.")

    active = [(pop_n, judged_n, passed_n) for pop_n, judged_n, passed_n in strata if judged_n > 0]
    if not active:
        raise ValueError("At least one stratum must have judged_n > 0.")

    total_pop = 0
    total_judged = 0
    for pop_n, judged_n, passed_n in active:
        if pop_n <= 0:
            raise ValueError("Each judged stratum must have population_count > 0.")
        if not (0 <= passed_n <= judged_n):
            raise ValueError("Each stratum must satisfy 0 <= passed_n <= judged_n.")
        total_pop += pop_n
        total_judged += judged_n

    weighted_p_obs = 0.0
    var_p_obs = 0.0
    for pop_n, judged_n, passed_n in active:
        w_s = pop_n / total_pop
        p_s = passed_n / judged_n
        weighted_p_obs += w_s * p_s
        var_p_obs += (w_s**2) * (p_s * (1.0 - p_s)) / judged_n

    return rogan_gladen_calibrate(
        observed_pass_rate=weighted_p_obs,
        sample_size=total_judged,
        confusion=confusion,
        z_value=z_value,
        ci_method=ci_method,
        stratified_var_p_obs=var_p_obs,
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
    invalid_scores = [s for s in (*rater_a, *rater_b) if s not in valid_categories]
    if invalid_scores:
        raise ValueError(
            f"Rater scores {sorted(set(invalid_scores))} outside allowed categories {tuple(categories)}."
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


def build_calibration_result(  # noqa: PLR0913
    human_ordinal_scores: Sequence[int],
    judge_ordinal_scores: Sequence[int],
    observed_pass_rate: float,
    sample_size: int,
    passing_score: int = 3,
    irr_threshold: float = 0.80,
    categories: Sequence[int] = (1, 2, 3, 4),
) -> CalibrationResult:
    """Build combined Quadratic Gwet's AC2 IRR gate and Rogan-Gladen calibration summary."""
    ac2 = compute_gwet_ac2(human_ordinal_scores, judge_ordinal_scores, categories=categories)
    confusion = compute_confusion_matrix(
        human_labels=[score >= passing_score for score in human_ordinal_scores],
        judge_labels=[score >= passing_score for score in judge_ordinal_scores],
    )
    rg = rogan_gladen_calibrate(
        observed_pass_rate=observed_pass_rate,
        sample_size=sample_size,
        confusion=confusion,
    )
    return CalibrationResult(
        gwet_ac2=ac2,
        passes_irr_gate=ac2 >= irr_threshold,
        confusion_matrix=confusion,
        rogan_gladen=rg,
    )
