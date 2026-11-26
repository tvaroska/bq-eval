---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 4: High-Volume Document Extraction

> A pure document management and structured information-extraction workload running in batch or gateway-ingested pipelines over massive volumes of PDFs (from a few pages up to 250 pages) where total cost is the top constraint, using a primary-to-stronger-model escalation cascade, stratified LLM and confidence-gated human review, `>200K` long-context cliff tracking, and continuous evaluation of whether cheaper models lower total cost.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Pure document management and structured information extraction across large document corpora. |
| **Inputs & format** | **Mostly PDFs**, spanning from a **few pages up to 250 pages** per document — arriving either as pre-staged object-storage URIs (`media_uris`) or gateway-ingested inline PDFs streamed via the Gateway Inline-Artifact Port. |
| **Scale & execution mode** | **Agent batch** processing at **massive volumes** (`RL-3` / `RL-4`; offline/scheduled traced runs or upstream batch-job records normalized via the Bronze Adapter Port, enforcing proxy `FLEX` headers or idle off-peak dedicated capacity); **total cost** is the primary constraint. |
| **Model cascade** | **Two-model fallback:**<br>1. **Primary model**: processes every document by default.<br>2. **Escalation model (more powerful)**: invoked when the primary model **fails** or reports **low confidence**. |
| **Review & eval tiers** | **Stratified / tiered evaluation**: deterministic checks -> stratified LLM evaluation -> **human/user review gated by confidence** (low/uncertain extractions reviewed by users; high-confidence extractions auto-accepted, with calibration checks on model confidence and `outcome.parent_id` chaining into downstream action workflows). |
| **Primary decision question** | **Can cheaper models lower total cost?** Continuously evaluate cheaper model options, confidence thresholds, and `FLEX`-vs-idle-dedicated-capacity arbitrage to minimize combined cost (primary + escalation + eval) while holding calibrated extraction accuracy. |

## Telemetry & Data Expectations

- **Document & cascade correlation**: Link primary model attempt(s), fallback escalation, failure flags, **per-call model-reported confidence** (defaulting to worst-chunk **`min_model_confidence`** on `outcomes` for multi-chunk/map-reduce PDFs alongside `final_model_confidence`), stratified LLM judge verdicts, human review outcomes, and optional `outcome.parent_id` under one document outcome.
- **Model, page-size & `>200K` cliff granularity**: Record exact model used, `document.page_count`, `document.bytes`, `attachment_sha256`, `serving_tier` (`FLEX` vs. off-peak PT vs. `STANDARD`), input/output/cached tokens (keying `>200K` cliffs on total context `input_tokens + cache_read_tokens`), and escalation reason (`failure | low_confidence`).
- **Confidence & review outcomes**: Capture raw model-reported confidence alongside human/user corrections or blind SME audit labels for reliability/ECE calibration.
- **Inspectable payloads**: Retain source document URIs in place (`media_uris`) and extracted structured output so paired cheaper-model trials and stratified audits run without copying PDFs.

## Required Checks, Alerts & Views

1. **Cheaper-model total-cost & capacity-arbitrage evaluation (main eval)**
   - Compare candidate cheaper models and confidence thresholds on paired samples (`case_id`) to verify whether switching yields **lower total cost** `(primary inference + escalation fallback + eval)` above the calibrated accuracy floor, explicitly modeling `>200K` long-context cliffs on 250-page PDFs and `FLEX` vs. idle off-peak dedicated capacity arbitrage.
2. **Stratified LLM, confidence calibration (ECE) & human evaluation**
   - Avoid judging 100% of massive volume; run deterministic schema validation on all documents, stratified LLM evaluation across confidence/escalation bands, verify model-confidence calibration (ECE / overconfidence on auto-accepted documents using worst-chunk `min_model_confidence`), and route low-confidence items to human review.
3. **Escalation rate, `FLEX` compliance & document-size cost visibility**
   - Track failure-triggered vs. confidence-triggered escalation rates, verify proxy-enforced `FLEX` / off-peak routing (alerting if batch extraction spills onto daytime PT), and slice cost per document across page-length bands (few pages vs. up to 250 pages) and `>200K` cliff crossings.
4. **Extraction validity & reliability**
   - Check JSON schema conformance, missing required fields, and batch failure/retry behavior across large PDFs (requires live recorded Batch API trace fixtures before marking batch adapters verified).
