# DESIGN.md — BQ Eval & FinOps Visual & UX Contract (GM3 Light Tier 2)

## 1. Visual Direction & Domain Persona
- **Tier**: Tier 2: Reusable Showcase — GM3 Light (`Google Cloud / Material 3`).
- **Aesthetic Persona**: High-trust lakehouse evaluation, statistical calibration, and AI FinOps workbench (`BigQuery Studio` / `Google Cloud Console` clarity).
- **Theme Polarity**: Strictly **Daylight-first (`GM3 Light`)** so tables, confidence intervals, and cost breakdowns copy cleanly into executive decks and architecture reviews.
- **Tab Identity**: Every HTML surface (`bq-eval serve` live workbench and `bq-eval demo --html-out` snapshot) embeds a high-contrast inline SVG `<link rel="icon">` favicon (`#1a73e8` rounded square with calibration bars and `#137333` verified dot).

## 2. Semantic Color Tokens (`:root`)
```css
:root {
  --surface: #f8f9fa;
  --surface-container: #ffffff;
  --surface-container-low: #f1f3f4;
  --surface-container-high: #e8eaed;
  --on-surface: #202124;
  --on-surface-variant: #5f6368;
  --outline-variant: #dadce0;
  --primary: #1a73e8;
  --primary-hover: #1557b0;
  --on-primary: #ffffff;
  --primary-container: #e8f0fe;
  --on-primary-container: #174ea6;
  --status-ok-fg: #137333;
  --status-ok-bg: #e6f4ea;
  --status-warn-fg: #b06000;
  --status-warn-bg: #fef7e0;
  --status-err-fg: #c5221f;
  --status-err-bg: #fce8e6;
  --radius: 0.5rem;
}
```

## 3. Typography, Accessibility & Escaping Discipline
- **UI & Headings**: `"Google Sans"`, `"Inter Variable"`, `"Inter"`, `system-ui`, `-apple-system`, `sans-serif`.
- **Code, Identifiers & SQL**: `"Roboto Mono"`, `"JetBrains Mono Variable"`, `"JetBrains Mono"`, `ui-monospace`, `monospace`.
- **Tabular Numerals**: Apply `font-variant-numeric: tabular-nums` across all KPI cards, pass-rate tables, confidence intervals, token counts, and USD columns (`text-align: right` on numeric cells).
- **Reduced Motion**: Respect `@media (prefers-reduced-motion: reduce)` by disabling transitions on tabs and controls.
- **Single-Point HTML Escaping & Fail-Closed Truthfulness**: Render exclusively from typed `PageView` objects in `bq_eval/demo/renderer.py` through one `_esc()` (`html.escape(..., quote=True)`) boundary. Never turn `None` (`UNJUDGED` / `UNCALIBRATED`) into `0.0%` or `$0.0000`, and never display `PASS` when `passes_irr_gate` is `False` or `None`.

## 4. Layout & 4-Tab Workbench Architecture (`bq-eval serve` & `--html-out`)
- **Max Container Width**: `1280px`, `24px` outer padding, `16px` grid gap, `8px`–`12px` card border-radius with `1px solid var(--outline-variant)`.
- **Four Workbench Tabs**:
  1. **Tab 1 — Quality & Calibrated CPVO**: Headline KPI cards + per-application Rule 0, Gwet's `AC2` IRR gate, `StratPPI` Rogan-Gladen 95% CI, and 4-bucket USD Cost per Verified Outcome (`CPVO`).
  2. **Tab 2 — Live Fleet Receipts & Eval ROI**: Empirical `m2-live-20261113b` multi-agent receipts (`343` runs, `28,752` spans, `443` outcomes across KYC trap catch, RFP `>200K` cliff, Meridian Day-1/Day-2 skill ROI, and shadow attribution) + 3-Tier Judge Funnel savings.
  3. **Tab 3 — Prefix-Cache & Pareto Frontier**: `1/N` system-prompt prefix cache dilution diagnostics + Zero-Fabrication (`fabrication_rate = 0.0%`) Cost-vs-Quality Pareto frontier (`pareto_frontier` vs. `disqualified_options`).
  4. **Tab 4 — FOCUS 1.2 Chargeback & Off-Peak PT**: Two-Pool Provisioned Throughput chargeback (`Utilized PT` vs. `Idle Capacity Tax` + `PAYG Overflow`) and hourly off-peak PT batch-evaluation water-filling schedule.

## 5. Do Not Generate
- No decorative left-edge or top-edge colored accent bars on cards.
- No icon-in-circle KPI headers, warm/cream paper backgrounds, or serif display headings.
- No `feTurbulence` SVG noise overlays or staggered list reveal animations.
- No embedded evaluation/FinOps math in browser JavaScript (all calculations execute in `bq_eval/core/engine/`).
- No synthetic `0.0%`, `$0.0000`, or hardcoded `PASS` badges on `UNJUDGED`, `UNCALIBRATED`, or failing-IRR rows.
