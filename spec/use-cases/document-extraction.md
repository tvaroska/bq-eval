---
type: atom
related:
  - "../use-cases.md"
tags: [bq-eval, use-cases]
---

# Use Case 4: High-Volume Document Extraction

> A pure document management and structured information-extraction workload running in batch over massive volumes of PDFs (from a few pages up to 250 pages) where total cost is the top constraint, using a primary-to-stronger-model escalation cascade, stratified LLM and confidence-gated human review, and continuous evaluation of whether cheaper models lower total cost.

## Agent & Workload Profile

| Dimension | Description |
|---|---|
| **Purpose** | Pure document management and structured information extraction across large document corpora. |
| **Inputs & format** | **Mostly PDFs**, spanning from a **few pages up to 250 pages** per document. |
| **Scale & execution mode** | **Agent batch** processing at **massive volumes** (offline/scheduled traced agent runs per document, optionally using discounted **Flex** serving tiers such as GCP Flex — not spanless Batch Prediction APIs); **total cost** is the most critical constraint (cloud/residency is not a distinguishing requirement). |
| **Model cascade** | **Two-model fallback:**<br>1. **Primary model**: processes every document by default.<br>2. **Escalation model (more powerful)**: invoked when the primary model **fails** or reports **low confidence**. |
| **Review & eval tiers** | **Stratified / tiered evaluation**: deterministic checks -> stratified LLM evaluation -> **human/user review gated by confidence** (low/uncertain extractions reviewed by users; high-confidence extractions auto-accepted). |
| **Primary decision question** | **Can cheaper models lower total cost?** Continuously evaluate cheaper model options and confidence thresholds to minimize combined cost (primary + escalation + eval) while maintaining acceptable extraction accuracy. |

## Telemetry & Data Expectations

- **Document & cascade correlation**: Link primary model attempt(s), fallback escalation to the stronger model, failure flags, **model-reported confidence**, stratified LLM judge verdicts, and human/user review outcomes to one document/extraction outcome.
- **Model, page-size & token granularity**: Record exact model used, document page/size band (few pages vs. up to 250 pages), input/output/cached tokens, and whether fallback escalation triggered.
- **Confidence & review outcomes**: Capture raw model-reported confidence alongside human/user corrections or audit labels.
- **Inspectable payloads**: Retain source document content/excerpts and extracted structured output so paired cheaper-model trials and stratified LLM/human audits can verify extraction accuracy.

## Required Checks, Alerts & Views

1. **Cheaper-model total-cost evaluation (main eval)**
   - Compare candidate cheaper models (and confidence thresholds) on paired samples to verify whether switching models yields **lower total cost** `(primary inference + escalation fallback + eval)` without unacceptable accuracy loss, accounting for any surge in fallback rate.
2. **Stratified LLM & confidence-gated human evaluation**
   - Avoid judging 100% of massive volume with an LLM; run deterministic validation on all documents, stratified LLM evaluation across confidence/escalation bands, route low-confidence items to user review, and verify error rates within auto-accepted high-confidence documents.
3. **Escalation rate & document-size cost visibility**
   - Track failure-triggered vs. confidence-triggered escalation rates from Model 1 to Model 2, cost per extracted document across page lengths (few pages vs. up to 250 pages), and evaluation overhead as a share of total cost.
4. **Extraction validity & reliability**
   - Check schema conformance, missing required fields, and batch failure/retry behavior across large PDFs.
