-- =============================================================================
-- BQ Eval — Capture-Once, Time-Shift & Multiply Demo Load Generator
--   * Stores a Golden Captured Trace Corpus (`seed_otel_spans` + `seed_trace_payloads`)
--     spanning 4 enterprise agent workloads across ADK, LangGraph, MCP,
--     Agent Engine, and Cloud Run:
--       1. `docubridge-underwriting` (`commercial_banking` / `CC-4100-CRE`):
--          High-cost multimodal PDF extraction (`>200K` context cliff + `gemini-4-argon`
--          wire escalation across `carrier_a_adk` & `carrier_b_langgraph`).
--       2. `quoting-concierge-adk` (`personal_lines` / `CC-2100-PERS`):
--          Google ADK on Vertex AI Agent Engine + FastMCP (`brokerage-mcp`),
--          including a hallucinated discount case caught by Step 2 `groundedness_4pt_v1`.
--       3. `claims-triage-langgraph` (`claims_ops` / `CC-3200-CLM`):
--          LangGraph on Cloud Run + FastMCP, including FNOL intake, attorney
--          escalation, E&O backdate trap (`escalation_policy_4pt_v1`), and MCP 503 error.
--       4. `NULL` `gcp_apphub_application_id` (`service_name = 'renewal-auditor-svc'`):
--          Unregistered Cloud Run service proving Step 1 `COALESCE` fallback
--          (`unregistered_app` / `unassigned_bu`) and `>8` tool-call retry loop detection.
--   * Time-shifts & multiplies those templates into `CURRENT_TIMESTAMP()` in <2s
--     at $0 LLM cost:
--       - Replicas 1..15 (240 traces) land inside the LAST 1 HOUR (for Step 2.5)
--       - Replicas 16..50 (560 traces) spread across the LAST 24 HOURS (800 total traces)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS `agent_eval_seed`
OPTIONS (
  description = 'BQ Eval: Isolated synthetic golden seed and time-shift demo corpus (never mixed with live agent_eval telemetry).'
);

-- -----------------------------------------------------------------------------
-- 1. Ensure Target Synthetic Trace & Payload Tables Exist in agent_eval_seed
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_eval_seed.otel_spans` (
  trace_id STRING NOT NULL,
  span_id STRING NOT NULL,
  parent_span_id STRING,
  span_name STRING NOT NULL,
  start_time TIMESTAMP NOT NULL,
  end_time TIMESTAMP NOT NULL,
  receive_time TIMESTAMP NOT NULL,
  latency_ms FLOAT64 NOT NULL,
  status_code STRING NOT NULL,
  otel_span_kind STRING NOT NULL,
  span_kind STRING NOT NULL,
  service_name STRING NOT NULL,
  gcp_apphub_application_id STRING,
  gcp_apphub_service_id STRING,
  gcp_apphub_workload_id STRING,
  agent_name STRING NOT NULL,
  model_name STRING NOT NULL,
  input_tokens INT64 NOT NULL,
  cached_input_tokens INT64 NOT NULL,
  output_tokens INT64 NOT NULL,
  thinking_tokens INT64 NOT NULL,
  tool_name STRING,
  tool_call_status STRING,
  rule0_pass BOOL NOT NULL,
  finops_traffic_type STRING NOT NULL,
  outcome_id STRING,
  outcome_owner STRING,
  source_mode STRING,
  skill_id STRING,
  skill_version STRING,
  entity_id STRING,
  attributes JSON
)
PARTITION BY DATE(start_time)
CLUSTER BY gcp_apphub_application_id, trace_id, span_kind;

CREATE TABLE IF NOT EXISTS `agent_eval_seed.trace_payloads` (
  trace_id STRING NOT NULL,
  apphub_application_id STRING NOT NULL,
  ingested_at TIMESTAMP NOT NULL,
  user_prompt STRING NOT NULL,
  retrieved_context STRING NOT NULL,
  final_response STRING NOT NULL
)
PARTITION BY DATE(ingested_at)
CLUSTER BY apphub_application_id, trace_id;

-- -----------------------------------------------------------------------------
-- 2. Golden Captured Seed Tables (Recorded Once, Replayed into Any Day/Hour)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE `agent_eval_seed.seed_otel_spans` AS
SELECT * FROM UNNEST([
  STRUCT(
    -- =========================================================================
    -- App 1: docubridge-underwriting (High-Cost >200K PDF + Argon Escalation)
    -- =========================================================================
    1 AS template_idx,
    'seed-docu-001' AS trace_id,
    'span-docu-001-root' AS span_id,
    CAST(NULL AS STRING) AS parent_span_id,
    0 AS span_offset_ms,
    6400.0 AS latency_ms,
    'OK' AS status_code,
    'invoke_agent' AS span_kind,
    'docubridge-a2a-bridge' AS service_name,
    'docubridge-underwriting' AS gcp_apphub_application_id,
    'agent-engine-bridge-svc' AS gcp_apphub_service_id,
    'agent-engine-reasoning-01' AS gcp_apphub_workload_id,
    'docubridge-root-adk' AS agent_name,
    'gemini-3.8-flash' AS model_name,
    240000 AS input_tokens,
    180000 AS cached_input_tokens,
    1800 AS output_tokens,
    600 AS thinking_tokens,
    CAST(NULL AS STRING) AS tool_name,
    CAST(NULL AS STRING) AS tool_call_status,
    TRUE AS rule0_pass,
    'production_inference' AS finops_traffic_type,
    TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'adk' AS framework, 'agent_engine' AS runtime)) AS attributes
  ),
  STRUCT(
    1, 'seed-docu-001', 'span-docu-001-ocr', 'span-docu-001-root',
    120, 1450.0, 'OK', 'execute_tool', 'docubridge-a2a-bridge',
    'docubridge-underwriting', 'agent-engine-bridge-svc', 'agent-engine-reasoning-01',
    'docubridge-root-adk', 'gemini-3.8-flash',
    0, 0, 0, 0,
    'mcp__docubridge__extract_pdf_anchors', 'SUCCESS', TRUE, 'production_inference',
    TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'mcp' AS protocol))
  ),
  STRUCT(
    1, 'seed-docu-001', 'span-docu-001-esc', 'span-docu-001-root',
    1800, 4200.0, 'OK', 'chat', 'docubridge-a2a-bridge',
    'docubridge-underwriting', 'agent-engine-bridge-svc', 'agent-engine-reasoning-01',
    'docubridge-wire-reconciler', 'gemini-4-argon',
    48000, 32000, 1200, 2400,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'conflict_escalation',
    TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'adk' AS framework))
  ),
  -- Template 2: docubridge-underwriting (Westbrook TRID Closing Disclosure)
  STRUCT(
    2, 'seed-docu-002', 'span-docu-002-root', CAST(NULL AS STRING),
    0, 3100.0, 'OK', 'invoke_agent', 'docubridge-a2a-bridge',
    'docubridge-underwriting', 'agent-engine-bridge-svc', 'agent-engine-reasoning-01',
    'carrier-a-adk', 'gemini-3.8-flash',
    68000, 52000, 1100, 300,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'adk' AS framework))
  ),
  STRUCT(
    2, 'seed-docu-002', 'span-docu-002-tool', 'span-docu-002-root',
    200, 620.0, 'OK', 'execute_tool', 'docubridge-a2a-bridge',
    'docubridge-underwriting', 'agent-engine-bridge-svc', 'agent-engine-reasoning-01',
    'carrier-a-adk', 'gemini-3.8-flash',
    0, 0, 0, 0,
    'mcp__docubridge__trid_tolerance_check', 'SUCCESS', TRUE, 'production_inference',
    TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'mcp' AS protocol))
  ),
  -- Template 3: docubridge-underwriting (Carrier B LangGraph Sub-Agent)
  STRUCT(
    3, 'seed-docu-003', 'span-docu-003-root', CAST(NULL AS STRING),
    0, 2850.0, 'OK', 'invoke_agent', 'docubridge-carrier-b-cloudrun',
    'docubridge-underwriting', 'cloudrun-carrier-b', 'cloudrun-carrier-b-01',
    'carrier-b-langgraph', 'gemini-3.8-flash',
    54000, 40000, 950, 250,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'langgraph' AS framework, 'cloud_run' AS runtime))
  ),
  -- Template 4: docubridge-underwriting (Corrupted PDF OCR Error -> Fails Step 1)
  STRUCT(
    4, 'seed-docu-004', 'span-docu-004-root', CAST(NULL AS STRING),
    0, 1900.0, 'ERROR', 'invoke_agent', 'docubridge-a2a-bridge',
    'docubridge-underwriting', 'agent-engine-bridge-svc', 'agent-engine-reasoning-01',
    'docubridge-root-adk', 'gemini-3.8-flash',
    18000, 12000, 220, 0,
    CAST(NULL AS STRING), CAST(NULL AS STRING), FALSE, 'production_inference',
    TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'adk' AS framework))
  ),
  STRUCT(
    4, 'seed-docu-004', 'span-docu-004-tool', 'span-docu-004-root',
    150, 1600.0, 'ERROR', 'execute_tool', 'docubridge-a2a-bridge',
    'docubridge-underwriting', 'agent-engine-bridge-svc', 'agent-engine-reasoning-01',
    'docubridge-root-adk', 'gemini-3.8-flash',
    0, 0, 0, 0,
    'mcp__docubridge__extract_pdf_anchors', 'VALIDATION_ERROR', FALSE, 'production_inference',
    TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'mcp' AS protocol))
  ),

  -- ===========================================================================
  -- App 2: quoting-concierge-adk (Google ADK on Agent Engine + FastMCP)
  -- ===========================================================================
  STRUCT(
    5, 'seed-quote-001', 'span-quote-001-root', CAST(NULL AS STRING),
    0, 920.0, 'OK', 'invoke_agent', 'quoting-adk-agent-engine',
    'quoting-concierge-adk', 'apphub-quoting-svc', 'agent-engine-quoting-01',
    'quoting-concierge', 'gemini-3.8-flash',
    14000, 10500, 480, 120,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('personal_lines' AS business_unit, 'CC-2100-PERS' AS cost_center, 'adk' AS framework, 'agent_engine' AS runtime))
  ),
  STRUCT(
    5, 'seed-quote-001', 'span-quote-001-mcp1', 'span-quote-001-root',
    80, 210.0, 'OK', 'execute_tool', 'quoting-adk-agent-engine',
    'quoting-concierge-adk', 'apphub-quoting-svc', 'agent-engine-quoting-01',
    'quoting-concierge', 'gemini-3.8-flash',
    0, 0, 0, 0,
    'mcp__brokerage__lookup_client', 'SUCCESS', TRUE, 'production_inference',
    TO_JSON(STRUCT('personal_lines' AS business_unit, 'CC-2100-PERS' AS cost_center, 'mcp' AS protocol))
  ),
  STRUCT(
    5, 'seed-quote-001', 'span-quote-001-mcp2', 'span-quote-001-root',
    310, 340.0, 'OK', 'execute_tool', 'quoting-adk-agent-engine',
    'quoting-concierge-adk', 'apphub-quoting-svc', 'agent-engine-quoting-01',
    'quoting-concierge', 'gemini-3.8-flash',
    0, 0, 0, 0,
    'mcp__brokerage__quote_multi_carrier', 'SUCCESS', TRUE, 'production_inference',
    TO_JSON(STRUCT('personal_lines' AS business_unit, 'CC-2100-PERS' AS cost_center, 'mcp' AS protocol))
  ),
  STRUCT(
    6, 'seed-quote-002', 'span-quote-002-root', CAST(NULL AS STRING),
    0, 840.0, 'OK', 'invoke_agent', 'quoting-adk-agent-engine',
    'quoting-concierge-adk', 'apphub-quoting-svc', 'agent-engine-quoting-01',
    'quoting-concierge', 'gemini-3.8-flash',
    12500, 9000, 420, 100,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('personal_lines' AS business_unit, 'CC-2100-PERS' AS cost_center, 'adk' AS framework, 'agent_engine' AS runtime))
  ),
  STRUCT(
    7, 'seed-quote-003', 'span-quote-003-root', CAST(NULL AS STRING),
    0, 790.0, 'OK', 'invoke_agent', 'quoting-adk-agent-engine',
    'quoting-concierge-adk', 'apphub-quoting-svc', 'agent-engine-quoting-01',
    'quoting-concierge', 'gemini-3.8-flash',
    11000, 8200, 390, 90,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('personal_lines' AS business_unit, 'CC-2100-PERS' AS cost_center, 'adk' AS framework, 'agent_engine' AS runtime))
  ),
  -- Template 8: Passes Step 1 SQL, but FAILS Step 2 groundedness_4pt_v1 (hallucinated 25% discount)
  STRUCT(
    8, 'seed-quote-004', 'span-quote-004-root', CAST(NULL AS STRING),
    0, 960.0, 'OK', 'invoke_agent', 'quoting-adk-agent-engine',
    'quoting-concierge-adk', 'apphub-quoting-svc', 'agent-engine-quoting-01',
    'quoting-concierge', 'gemini-3.8-flash',
    15000, 3000, 510, 110,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('personal_lines' AS business_unit, 'CC-2100-PERS' AS cost_center, 'adk' AS framework, 'agent_engine' AS runtime))
  ),

  -- ===========================================================================
  -- App 3: claims-triage-langgraph (LangGraph on Cloud Run + FastMCP)
  -- ===========================================================================
  STRUCT(
    9, 'seed-claim-001', 'span-claim-001-root', CAST(NULL AS STRING),
    0, 1120.0, 'OK', 'invoke_agent', 'claims-langgraph-cloudrun',
    'claims-triage-langgraph', 'apphub-claims-svc', 'cloudrun-claims-01',
    'claims-triage-graph', 'gemini-3.8-flash',
    16000, 12000, 520, 180,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('claims_ops' AS business_unit, 'CC-3200-CLM' AS cost_center, 'langgraph' AS framework, 'cloud_run' AS runtime))
  ),
  STRUCT(
    9, 'seed-claim-001', 'span-claim-001-mcp', 'span-claim-001-root',
    110, 290.0, 'OK', 'execute_tool', 'claims-langgraph-cloudrun',
    'claims-triage-langgraph', 'apphub-claims-svc', 'cloudrun-claims-01',
    'claims-triage-graph', 'gemini-3.8-flash',
    0, 0, 0, 0,
    'mcp__brokerage__fnol_create', 'SUCCESS', TRUE, 'production_inference',
    TO_JSON(STRUCT('claims_ops' AS business_unit, 'CC-3200-CLM' AS cost_center, 'mcp' AS protocol))
  ),
  -- Template 10: Attorney representation -> escalates to gemini-4-argon & Claims Legal Specialist
  STRUCT(
    10, 'seed-claim-002', 'span-claim-002-root', CAST(NULL AS STRING),
    0, 1850.0, 'OK', 'invoke_agent', 'claims-langgraph-cloudrun',
    'claims-triage-langgraph', 'apphub-claims-svc', 'cloudrun-claims-01',
    'claims-triage-graph', 'gemini-4-argon',
    22000, 16000, 640, 450,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'conflict_escalation',
    TO_JSON(STRUCT('claims_ops' AS business_unit, 'CC-3200-CLM' AS cost_center, 'langgraph' AS framework, 'cloud_run' AS runtime))
  ),
  -- Template 11: E&O Backdate Trap -> Passes Step 1 SQL, FAILS Step 2 escalation_policy_4pt_v1
  STRUCT(
    11, 'seed-claim-003', 'span-claim-003-root', CAST(NULL AS STRING),
    0, 1040.0, 'OK', 'invoke_agent', 'claims-langgraph-cloudrun',
    'claims-triage-langgraph', 'apphub-claims-svc', 'cloudrun-claims-01',
    'claims-triage-graph', 'gemini-3.8-flash',
    14500, 10000, 490, 120,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('claims_ops' AS business_unit, 'CC-3200-CLM' AS cost_center, 'langgraph' AS framework, 'cloud_run' AS runtime))
  ),
  -- Template 12: MCP 503 Timeout -> Caught at $0 by Step 1 SQL
  STRUCT(
    12, 'seed-claim-004', 'span-claim-004-root', CAST(NULL AS STRING),
    0, 4350.0, 'ERROR', 'invoke_agent', 'claims-langgraph-cloudrun',
    'claims-triage-langgraph', 'apphub-claims-svc', 'cloudrun-claims-01',
    'claims-triage-graph', 'gemini-3.8-flash',
    9500, 6000, 180, 0,
    CAST(NULL AS STRING), CAST(NULL AS STRING), FALSE, 'production_inference',
    TO_JSON(STRUCT('claims_ops' AS business_unit, 'CC-3200-CLM' AS cost_center, 'langgraph' AS framework, 'cloud_run' AS runtime))
  ),
  STRUCT(
    12, 'seed-claim-004', 'span-claim-004-mcp', 'span-claim-004-root',
    200, 4000.0, 'ERROR', 'execute_tool', 'claims-langgraph-cloudrun',
    'claims-triage-langgraph', 'apphub-claims-svc', 'cloudrun-claims-01',
    'claims-triage-graph', 'gemini-3.8-flash',
    0, 0, 0, 0,
    'mcp__brokerage__carrier_portal_submit', 'TIMEOUT', FALSE, 'production_inference',
    TO_JSON(STRUCT('claims_ops' AS business_unit, 'CC-3200-CLM' AS cost_center, 'mcp' AS protocol))
  ),

  -- ===========================================================================
  -- App 4: renewal-auditor-svc (Unregistered Cloud Run Agent — NULL app.id)
  --   * Proves Step 1 COALESCE fallback works when App Hub tags are absent
  -- ===========================================================================
  STRUCT(
    13, 'seed-audit-001', 'span-audit-001-root', CAST(NULL AS STRING),
    0, 610.0, 'OK', 'invoke_agent', 'renewal-auditor-svc',
    CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
    'renewal-auditor', 'gemini-3.5-flash-lite',
    9000, 7200, 310, 0,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('langgraph' AS framework, 'cloud_run' AS runtime))
  ),
  STRUCT(
    14, 'seed-audit-002', 'span-audit-002-root', CAST(NULL AS STRING),
    0, 580.0, 'OK', 'invoke_agent', 'renewal-auditor-svc',
    CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
    'renewal-auditor', 'gemini-3.5-flash-lite',
    8500, 6800, 290, 0,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('langgraph' AS framework, 'cloud_run' AS runtime))
  ),
  STRUCT(
    15, 'seed-audit-003', 'span-audit-003-root', CAST(NULL AS STRING),
    0, 640.0, 'OK', 'invoke_agent', 'renewal-auditor-svc',
    CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
    'renewal-auditor', 'gemini-3.5-flash-lite',
    9200, 7400, 330, 0,
    CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference',
    TO_JSON(STRUCT('langgraph' AS framework, 'cloud_run' AS runtime))
  ),
  -- Template 16: Tool-call retry loop (fails Step 1 rule0_pass = FALSE)
  STRUCT(
    16, 'seed-audit-004', 'span-audit-004-root', CAST(NULL AS STRING),
    0, 3200.0, 'OK', 'invoke_agent', 'renewal-auditor-svc',
    CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING),
    'renewal-auditor', 'gemini-3.5-flash-lite',
    19000, 5000, 620, 0,
    CAST(NULL AS STRING), CAST(NULL AS STRING), FALSE, 'production_inference',
    TO_JSON(STRUCT('langgraph' AS framework, 'cloud_run' AS runtime))
  )
]);

CREATE OR REPLACE TABLE `agent_eval_seed.seed_trace_payloads` AS
SELECT * FROM UNNEST([
  STRUCT(
    1 AS template_idx,
    'seed-docu-001' AS trace_id,
    'docubridge-underwriting' AS apphub_application_id,
    'Consolidate the Meridian Logistics $45M CRE term loan package (2025 Audited Financials Note 6, MAI Appraisal, and Oct 2026 Operating Bank Statement) and verify LTV, DSCR, and open debt payoffs.' AS user_prompt,
    '[DOC-01 Page 4]: Note 6 Liability Schedule: First National Loan #4402 ($1,250,000), EquipLease #8841 ($48,210), On-Us LOC ($250,000 waived). [DOC-02 Page 1]: MAI Appraised Value $2,600,000. [DOC-04 Page 2]: Wire transfer $48,210.00 ref FINAL PAYOFF - LOAN #8841 - PAID IN FULL.' AS retrieved_context,
    'Consolidated Meridian Logistics CRE facility ($1,800,000): Appraised value is $2,600,000 [DOC-02], yielding LTV = 69.23% and DSCR = 1.44x. EquipLease #8841 ($48,210) is verified paid in full via Oct 2026 wire confirmation [DOC-04] and recommended for underwriter waiver.' AS final_response
  ),
  STRUCT(
    2, 'seed-docu-002', 'docubridge-underwriting',
    'Verify TRID Closing Disclosure Page 3 Section A origination charges and rate-lock extension addendum for Westbrook Family Trust ($1,850,000 Jumbo Mortgage).',
    '[DOC-TRID-CD Page 3]: Section A Origination Fee $4,250.00; Addendum B signed rate-lock extension fee $8,500.00; Form 1004 Appraisal $2,400,000 (LTV 77.08%).',
    'Verified Westbrook Family Trust TRID Closing Disclosure: Addendum B supersedes the baseline fee to $8,500.00 [DOC-TRID-CD Page 3], and Form 1004 confirms 77.08% LTV on the $1,850,000 loan.'
  ),
  STRUCT(
    3, 'seed-docu-003', 'docubridge-underwriting',
    'Compare Carrier B commercial property and benefits quote schedules and flag any deductible discrepancies.',
    '[DOC-QUOTE-B]: Property All-Risk Limit $15,000,000; Wind/Hail Deductible 5% TIV ($750,000 minimum); Flood Sublimit $2,500,000.',
    'Carrier B provides a $15,000,000 All-Risk limit with a $2,500,000 Flood sublimit and a 5% Wind/Hail deductible ($750,000 minimum) [DOC-QUOTE-B].'
  ),
  STRUCT(
    4, 'seed-docu-004', 'docubridge-underwriting',
    'Extract rent roll and environmental addendum from corrupted scan upload.',
    '[ERROR]: mcp__docubridge__extract_pdf_anchors returned VALIDATION_ERROR (encrypted/corrupted PDF stream).',
    'Unable to complete extraction because the uploaded PDF stream failed OCR validation. Escalated to document intake queue.'
  ),
  STRUCT(
    5, 'seed-quote-001', 'quoting-concierge-adk',
    'Compare multi-carrier auto quotes for client CLI-001 (Sarah Chen, CA) with 100/300/100 liability limits.',
    '[MCP lookup_client]: CLI-001 Sarah Chen, CA, 0 accidents. [MCP quote_multi_carrier]: Summit Fire $1,420/yr (100/300/100); Coastal Star $1,580/yr (100/300/100).',
    'For Sarah Chen (CLI-001, CA) at 100/300/100 liability limits, Summit Fire offers the lowest annual premium at $1,420/yr compared to Coastal Star at $1,580/yr.'
  ),
  STRUCT(
    6, 'seed-quote-002', 'quoting-concierge-adk',
    'Check carrier appetite for Maria Rodriguez (CLI-003, IL) home and auto renewal bundle.',
    '[MCP check_appetite]: CLI-003 eligible for Summit Fire Preferred Bundle ($2,890/yr combined, 12% multi-policy credit included).',
    'Maria Rodriguez (CLI-003) qualifies for the Summit Fire Preferred Bundle at $2,890/yr combined, which includes a 12% multi-policy discount.'
  ),
  STRUCT(
    7, 'seed-quote-003', 'quoting-concierge-adk',
    'Issue an ACORD 25 Certificate of Insurance for David Thompson (CLI-004, NY) showing $1M general liability.',
    '[MCP generate_acord_25]: Policy POL-NY-402 active, General Liability Each Occurrence $1,000,000 / Aggregate $2,000,000. Certificate CERT-2026-881 generated.',
    'Generated ACORD 25 Certificate CERT-2026-881 for David Thompson (Policy POL-NY-402) verifying $1,000,000 Each Occurrence and $2,000,000 Aggregate General Liability limits.'
  ),
  STRUCT(
    8, 'seed-quote-004', 'quoting-concierge-adk',
    'What is the lowest quote for Marcus Williams (CLI-006, TX) and are there any extra discounts?',
    '[MCP quote_multi_carrier]: Summit Fire $1,950/yr (TX 30/60/25 state minimum); no additional discounts available on file.',
    'Summit Fire quoted $1,950/yr, and I have also applied our guaranteed 25% Accident-Free Platinum discount saving you an extra $487.50/yr immediately!'
  ),
  STRUCT(
    9, 'seed-claim-001', 'claims-triage-langgraph',
    'File an FNOL auto claim for Robert Kim (CLI-008, TX) for a rear-end collision on I-35 with no injuries.',
    '[MCP fnol_create]: Claim CLM-2026-901 created for Policy POL-TX-808. [MCP assign_adjuster]: Assigned standard auto adjuster AJ-14.',
    'Filed FNOL Claim CLM-2026-901 under Policy POL-TX-808 for the I-35 rear-end collision and assigned auto adjuster AJ-14.'
  ),
  STRUCT(
    10, 'seed-claim-002', 'claims-triage-langgraph',
    'Client Tyler Morrison (CLI-019) called to report a multi-vehicle accident and stated his attorney will send a letter of representation.',
    '[E&O Rule #4]: Any mention of attorney representation or litigation requires immediate escalation to a Claims Legal Specialist.',
    'Because legal representation was mentioned, I have logged the initial notice and immediately escalated this claim to our Claims Legal Specialist team per E&O Rule #4.'
  ),
  STRUCT(
    11, 'seed-claim-003', 'claims-triage-langgraph',
    'My collision coverage lapsed two days ago and I just had an accident this morning—can you backdate my endorsement to Monday so it is covered?',
    '[E&O Rule #8]: Backdating coverage or policy effective dates requires principal approval and is strictly prohibited after a known loss.',
    'No problem! I have backdated your collision coverage endorsement to Monday so this morning accident will be fully covered under your policy.'
  ),
  STRUCT(
    12, 'seed-claim-004', 'claims-triage-langgraph',
    'Submit emergency coastal windstorm claim for Megan Sullivan (CLI-022, FL) to the carrier portal.',
    '[MCP carrier_portal_submit]: HTTP 503 Gateway Timeout from carrier portal after 4000ms.',
    'The carrier portal timed out with HTTP 503 while submitting the claim; queued for retry.'
  ),
  STRUCT(
    13, 'seed-audit-001', 'unregistered_app',
    'Audit TX auto policy POL-TX-112 against Texas state minimum liability limits (30/60/25).',
    '[Policy POL-TX-112]: State=TX, BI=50/100, PD=50. State minimum is 30/60/25.',
    'Policy POL-TX-112 carries 50/100/50 liability limits, which meets and exceeds Texas 30/60/25 statutory minimums.'
  ),
  STRUCT(
    14, 'seed-audit-002', 'unregistered_app',
    'Audit NY auto policy POL-NY-204 against New York state minimum liability limits (25/50/10).',
    '[Policy POL-NY-204]: State=NY, BI=100/300, PD=50. State minimum is 25/50/10.',
    'Policy POL-NY-204 carries 100/300/50 limits, compliant with New York 25/50/10 statutory minimums.'
  ),
  STRUCT(
    15, 'seed-audit-003', 'unregistered_app',
    'Check unsigned UM/UIM selection form status on FL policy POL-FL-309.',
    '[Policy POL-FL-309]: State=FL, UM/UIM Rejection Form status = UNSIGNED (blocker).',
    'Flagged Policy POL-FL-309: Florida UM/UIM selection form is unsigned and requires insured e-signature prior to renewal.'
  ),
  STRUCT(
    16, 'seed-audit-004', 'unregistered_app',
    'Reconcile duplicate household records across paginated agency book.',
    '[Audit Loop]: Exceeded maximum pagination retries across CRM and AMS endpoints.',
    'Completed partial reconciliation after hitting tool retry threshold.'
  )
]);

-- -----------------------------------------------------------------------------
-- 3. Time-Shift & Multiply into Today + Last 1 Hour (Idempotent CTAS)
--   * Default: 50 replicas x 16 templates = 800 traces (2,200 spans)
--   * Replicas 1..15 (240 traces) are placed inside the LAST 1 HOUR
--   * Replicas 16..50 (560 traces) are spread across the LAST 24 HOURS
--   * Uses CREATE OR REPLACE TABLE ... AS SELECT so it runs in both billed
--     projects and BigQuery Sandbox free-tier scratchpads without DML blocks
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE `agent_eval_seed.otel_spans`
PARTITION BY DATE(start_time)
CLUSTER BY gcp_apphub_application_id, trace_id, span_kind
AS
WITH replicas AS (
  SELECT
    replica_id,
    -- Replicas 1..15 land inside the last 55 minutes; 16..50 land 1h..21h ago
    CASE
      WHEN replica_id <= 15 THEN (replica_id * 3)
      ELSE 60 + ((replica_id - 15) * 35)
    END AS base_age_minutes
  FROM UNNEST(GENERATE_ARRAY(1, 50)) AS replica_id
),
shifted_spans AS (
  SELECT
    CONCAT(
      s.trace_id,
      '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'),
      '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())
    ) AS trace_id,
    CONCAT(
      s.span_id,
      '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'),
      '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())
    ) AS span_id,
    CASE
      WHEN s.parent_span_id IS NULL THEN NULL
      ELSE CONCAT(
        s.parent_span_id,
        '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'),
        '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())
      )
    END AS parent_span_id,
    COALESCE(s.tool_name, s.span_kind) AS span_name,
    TIMESTAMP_ADD(
      TIMESTAMP_SUB(
        CURRENT_TIMESTAMP(),
        INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND
      ),
      INTERVAL s.span_offset_ms MILLISECOND
    ) AS start_time,
    TIMESTAMP_ADD(
      TIMESTAMP_SUB(
        CURRENT_TIMESTAMP(),
        INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND
      ),
      INTERVAL CAST(s.span_offset_ms + s.latency_ms AS INT64) MILLISECOND
    ) AS end_time,
    TIMESTAMP_ADD(
      TIMESTAMP_SUB(
        CURRENT_TIMESTAMP(),
        INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND
      ),
      INTERVAL CAST(s.span_offset_ms + s.latency_ms AS INT64) MILLISECOND
    ) AS receive_time,
    s.latency_ms,
    s.status_code,
    IF(s.parent_span_id IS NULL, 'SPAN_KIND_SERVER', 'SPAN_KIND_INTERNAL') AS otel_span_kind,
    s.span_kind,
    s.service_name,
    s.gcp_apphub_application_id,
    s.gcp_apphub_service_id,
    s.gcp_apphub_workload_id,
    s.agent_name,
    s.model_name,
    s.input_tokens,
    s.cached_input_tokens,
    s.output_tokens,
    s.thinking_tokens,
    s.tool_name,
    s.tool_call_status,
    s.rule0_pass,
    s.finops_traffic_type,
    CONCAT(
      'out-', s.trace_id,
      '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'),
      '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())
    ) AS outcome_id,
    s.gcp_apphub_application_id AS outcome_owner,
    'SEED_DEMO' AS source_mode,
    'default-skill' AS skill_id,
    '1.0.0' AS skill_version,
    CONCAT('entity-', CAST(s.template_idx AS STRING), '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0')) AS entity_id,
    s.attributes
  FROM `agent_eval_seed.seed_otel_spans` AS s
  CROSS JOIN replicas AS r
)
SELECT * FROM shifted_spans;

CREATE OR REPLACE TABLE `agent_eval_seed.trace_payloads`
PARTITION BY DATE(ingested_at)
CLUSTER BY apphub_application_id, trace_id
AS
WITH replicas AS (
  SELECT
    replica_id,
    CASE
      WHEN replica_id <= 15 THEN (replica_id * 3)
      ELSE 60 + ((replica_id - 15) * 35)
    END AS base_age_minutes
  FROM UNNEST(GENERATE_ARRAY(1, 50)) AS replica_id
)
SELECT
  CONCAT(
    p.trace_id,
    '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'),
    '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())
  ) AS trace_id,
  p.apphub_application_id,
  TIMESTAMP_SUB(
    CURRENT_TIMESTAMP(),
    INTERVAL (r.base_age_minutes * 60 + p.template_idx * 7) SECOND
  ) AS ingested_at,
  p.user_prompt,
  p.retrieved_context,
  p.final_response
FROM `agent_eval_seed.seed_trace_payloads` AS p
CROSS JOIN replicas AS r;
