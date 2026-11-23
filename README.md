# BQ Eval & FinOps: Stop Mistaking Token Spend for Working Agents

Agents don't fail loudly on monitoring dashboards: a looping tool or 200K-token PDF burns budget without finishing the job. Judging every trace with LLMs rivals production spend, and oversampling failing tails skews raw pass rates. I built BQ Eval & FinOps so Day-1 BigQuery views need zero new tables, mirror in SQLite, and return NULL when unjudged.

![BQ Eval and FinOps In-Warehouse Views](docs/assets/architecture_card.svg)

## How It Works

Agent traces already land in BigQuery, through BigQuery Agent Analytics or OpenTelemetry. I evaluate them there instead of copying them into a separate eval tool.

1. **Rule checks on every trace.** SQL views flag tool errors, loops, slow runs and broken outputs. No new tables and no AI cost, only BigQuery scan bytes.
2. **An AI judge on a sample.** A judge model scores the failing tail plus a small random sample against a 4-point rubric. Failing traces are oversampled, so the raw pass rate is biased; the views correct it and report a confidence interval. An app with no judged traces gets an empty result, never a fake zero.
3. **Saved runs and cost per outcome.** Scores are saved per run, so two agent versions can be compared. Agent spend and judge spend are divided by finished business outcomes, not by traces.

The same SQL runs on a local SQLite copy, so the demo and the tests need no cloud account.

## Results

These numbers replay 343 reconstructed cases (676 spans). There are no human labels yet: the judge is checked against a stronger reference model. Full figures and denominators are in [results](docs/results.md), and CI fails if they drift from the demo output.

| Scenario | What it shows | Headline |
| :--- | :--- | :--- |
| Judge cost model, 100k traces | Screening with rules before judging | Judge spend down 92.2% ($250 to $19.62) |
| Address checks, 280 cases | Rules catch the cheap failures first | 44 rejects at no AI cost; 20 of 20 planted traps caught, 0 false alarms in 216 |
| Address checks, judged rows | Fast judge against a stronger model | 30 of 30 pass/fail matches; corrected pass rate 91.5% (88.0% to 95.1%) |
| Order support, 50 traces | Why raw pass rates mislead | Raw 81.8%, corrected 87.4% (wide interval, 65% to 100%) |
| Skill upgrade, 18 cases | Cost per outcome across versions | AI cost per outcome down 99.3% (8 vs. 10 cases) |

## Why Not Langfuse, LangSmith, Datadog or Cloud Billing?

Use them for prompt work, annotation and alerts. This repo covers what they leave out: evaluating where the traces already live, and tying spend to finished work.

| Tool | Good at | Missing for this job |
| :--- | :--- | :--- |
| Langfuse / LangSmith | Prompt iteration, annotation, dev tracing | Copies traces out of the warehouse; no bias correction for sampled scores |
| Datadog LLM Observability | Alerts, latency, token spend | Stops at cost per trace; no finished outcomes or judge spend |
| Cloud Billing (FOCUS 1.2) | Standard invoices across clouds | No reserved capacity split per app; misses the higher price above 200K tokens; mixes production and eval spend |
| BigQuery Agent Analytics | Three-line capture and managed scoring (AI.EVALUATE_AGENT, private preview) | No app ownership, bias correction, free rule checks or cost per outcome |

## Where This Breaks

1. **No human labels yet.** Judge agreement (AC2 0.86) compares a fast model with a stronger model. Regulated use needs about 100 blind human labels.
2. **Small, mixed slices.** The skill upgrade compares 8 and 10 cases and leaves out engineering time. The long-document test (35 cases) changes model and price tier at once, so it can't isolate either.
3. **Caps and preview limits.** Saved judge runs stop at a 2M-token daily cap. AI.EVALUATE_AGENT is a private preview in 14 single regions and estimates tokens as characters divided by 4.
4. **Live runs need a GCP project.** Everything local runs on SQLite and offline schema checks. Deploying needs a project, the bq CLI and gcloud auth.

## Quick Start

```bash
uv sync --extra dev
uv run python -m bq_eval.cli demo --html-out /tmp/bq_eval_scorecard.html
uv run python -m bq_eval.cli serve --port 8080
```

The demo replays the 343 cases and writes a scorecard; serve opens the same workbench locally. Capture setup, judge SQL, deployment and dry runs are in [getting started](docs/getting-started.md).

### Deep Dives

- [Results](docs/results.md) · [Getting started](docs/getting-started.md) · [SQL & calibration](docs/architecture-and-sql.md) · [BQAA & OpenTelemetry capture](docs/bqaa-and-dual-path-architecture.md) · [Cost & chargeback](docs/finops-and-cost-management.md) · [Rubric skill](skills/bq-eval/SKILL.md) · [Invariants](CRITICAL.md) · [Decisions](DECISIONS.md) · [Roadmap](docs/roadmap.md)
