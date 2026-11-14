"""Rogan-Gladen Prevalence Correction, 95% Delta-Method CI, and Quadratic Gwet's AC2."""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict


class ConfusionMatrix(BaseModel):
    """Binary autorater vs. human SME confusion matrix on Rule-0-passing traces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tp: int
    fn: int
    tn: int
    fp: int

    @property
    def total(self) -> int:
        return self.tp + self.fn + self.tn + self.fp

    @property
    def sensitivity_tpr(self) -> float:
        denom = self.tp + self.fn
        if denom == 0:
            raise ValueError("Cannot compute sensitivity (TPR) with zero positive human labels.")
        return self.tp / denom

    @property
    def specificity_tnr(self) -> float:
        denom = self.tn + self.fp
        if denom == 0:
            raise ValueError("Cannot compute specificity (TNR) with zero negative human labels.")
        return self.tn / denom

    @property
    def youden_j(self) -> float:
        return self.sensitivity_tpr + self.specificity_tnr - 1.0


class RoganGladenResult(BaseModel):
    """Bias-corrected true pass rate and 95% confidence interval (EVAL-P04)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_pass_rate: float
    calibrated_pass_rate: float
    ci_95_lower: float
    ci_95_upper: float
    sensitivity_tpr: float
    specificity_tnr: float
    youden_j: float
    sample_size: int


class CalibrationResult(BaseModel):
    """Combined IRR (Gwet's AC2) + Rogan-Gladen calibration summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gwet_ac2: float
    passes_irr_gate: bool
    confusion_matrix: ConfusionMatrix
    rogan_gladen: RoganGladenResult


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


def rogan_gladen_calibrate(
    observed_pass_rate: float,
    sample_size: int,
    confusion: ConfusionMatrix,
    z_value: float = 1.959963984540054,
) -> RoganGladenResult:
    """Apply Rogan-Gladen prevalence adjustment with Delta-method 95% CI (EVAL-P04).

    Formula:
        theta_RG = clip((p_obs + TNR - 1) / (TPR + TNR - 1), 0, 1)
    """
    if sample_size <= 0:
        raise ValueError("sample_size must be positive for Rogan-Gladen calibration.")
    if not (0.0 <= observed_pass_rate <= 1.0):
        raise ValueError("observed_pass_rate must be in [0.0, 1.0].")

    tpr = confusion.sensitivity_tpr
    tnr = confusion.specificity_tnr
    j_stat = tpr + tnr - 1.0
    if j_stat <= 1e-6:
        raise ValueError(
            f"Autorater is uncalibrated (Youden's J = {j_stat:.4f} <= 0); "
            "cannot apply Rogan-Gladen division."
        )

    raw_theta = (observed_pass_rate + tnr - 1.0) / j_stat
    theta_rg = min(1.0, max(0.0, raw_theta))

    var_p_obs = (observed_pass_rate * (1.0 - observed_pass_rate)) / sample_size
    n_pos = confusion.tp + confusion.fn
    n_neg = confusion.tn + confusion.fp
    var_tpr = (tpr * (1.0 - tpr)) / n_pos
    var_tnr = (tnr * (1.0 - tnr)) / n_neg

    se_theta = (
        math.sqrt(var_p_obs + (theta_rg**2) * var_tpr + ((1.0 - theta_rg) ** 2) * var_tnr) / j_stat
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


def compute_gwet_ac2(
    rater_a: Sequence[int],
    rater_b: Sequence[int],
    categories: Sequence[int] = (1, 2, 3, 4),
) -> float:
    """Compute quadratic-weighted Gwet's AC2 inter-rater reliability (EVAL-P03).

    Resistant to the high-prevalence Cohen's Kappa paradox on skewed production distributions.
    """
    if len(rater_a) != len(rater_b) or not rater_a:
        raise ValueError("Rater sequences must be non-empty and equal length.")

    n = len(rater_a)
    q = len(categories)
    if q < 2:
        raise ValueError("At least 2 ordinal categories are required.")

    cat_min = min(categories)
    cat_max = max(categories)
    span = float(cat_max - cat_min)

    def weight(c1: int, c2: int) -> float:
        return 1.0 - ((float(c1 - c2) / span) ** 2)

    p_a = sum(weight(a, b) for a, b in zip(rater_a, rater_b, strict=True)) / n

    pi_k: dict[int, float] = {}
    for k in categories:
        count_a = sum(1 for a in rater_a if a == k)
        count_b = sum(1 for b in rater_b if b == k)
        pi_k[k] = ((count_a / n) + (count_b / n)) / 2.0

    t_w = sum(weight(k, m) for k in categories for m in categories)
    denom_norm = q * (q - 1)
    p_e = (t_w / denom_norm) * sum(pi_k[k] * (1.0 - pi_k[k]) for k in categories)

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
