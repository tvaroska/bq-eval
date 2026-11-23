-- BQ Eval — Capture-Once, Time-Shift & Multiply Demo Load Generator (`agent_eval_seed`)
-- Time-shifts 16 golden templates across 4 enterprise workloads into CURRENT_TIMESTAMP()
-- (50 replicas = 800 traces; replicas 1..15 land in the last 1h for Step 2.5 slices).

CREATE SCHEMA IF NOT EXISTS `agent_eval_seed`
OPTIONS (
  description = 'BQ Eval: Isolated synthetic golden seed and time-shift demo corpus (never mixed with live agent_eval telemetry).'
);

CREATE OR REPLACE TABLE `agent_eval_seed.seed_otel_spans` AS
WITH app_profiles AS (
  SELECT 'docubridge-underwriting' AS app_key, 'docubridge-underwriting' AS gcp_apphub_application_id, 'agent-engine-bridge-svc' AS gcp_apphub_service_id, 'agent-engine-reasoning-01' AS gcp_apphub_workload_id, TO_JSON(STRUCT('commercial_banking' AS business_unit, 'CC-4100-CRE' AS cost_center, 'adk' AS framework, 'agent_engine' AS runtime)) AS attributes
  UNION ALL
  SELECT 'quoting-concierge-adk', 'quoting-concierge-adk', 'apphub-quoting-svc', 'agent-engine-quoting-01', TO_JSON(STRUCT('personal_lines' AS business_unit, 'CC-2100-PERS' AS cost_center, 'adk' AS framework, 'agent_engine' AS runtime))
  UNION ALL
  SELECT 'claims-triage-langgraph', 'claims-triage-langgraph', 'apphub-claims-svc', 'cloudrun-claims-01', TO_JSON(STRUCT('claims_ops' AS business_unit, 'CC-3200-CLM' AS cost_center, 'langgraph' AS framework, 'cloud_run' AS runtime))
  UNION ALL
  SELECT 'unregistered_app', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), TO_JSON(STRUCT('langgraph' AS framework, 'cloud_run' AS runtime))
),
base_spans AS (
  SELECT * FROM UNNEST([
    STRUCT(1 AS template_idx, 'docubridge-underwriting' AS app_key, 'seed-docu-001' AS trace_id, 'span-docu-001-root' AS span_id, CAST(NULL AS STRING) AS parent_span_id, 0 AS span_offset_ms, 6400.0 AS latency_ms, 'OK' AS status_code, 'invoke_agent' AS span_kind, 'docubridge-a2a-bridge' AS service_name, 'docubridge-root-adk' AS agent_name, 'gemini-3.8-flash' AS model_name, 240000 AS input_tokens, 180000 AS cached_input_tokens, 1800 AS output_tokens, 600 AS thinking_tokens, CAST(NULL AS STRING) AS tool_name, CAST(NULL AS STRING) AS tool_call_status, TRUE AS rule0_pass, 'production_inference' AS finops_traffic_type),
    STRUCT(1, 'docubridge-underwriting', 'seed-docu-001', 'span-docu-001-ocr', 'span-docu-001-root', 120, 1450.0, 'OK', 'execute_tool', 'docubridge-a2a-bridge', 'docubridge-root-adk', 'gemini-3.8-flash', 0, 0, 0, 0, 'mcp__docubridge__extract_pdf_anchors', 'SUCCESS', TRUE, 'production_inference'),
    STRUCT(1, 'docubridge-underwriting', 'seed-docu-001', 'span-docu-001-esc', 'span-docu-001-root', 1800, 4200.0, 'OK', 'chat', 'docubridge-a2a-bridge', 'docubridge-wire-reconciler', 'gemini-4-argon', 48000, 32000, 1200, 2400, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'conflict_escalation'),
    STRUCT(2, 'docubridge-underwriting', 'seed-docu-002', 'span-docu-002-root', CAST(NULL AS STRING), 0, 3100.0, 'OK', 'invoke_agent', 'docubridge-a2a-bridge', 'carrier-a-adk', 'gemini-3.8-flash', 68000, 52000, 1100, 300, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(2, 'docubridge-underwriting', 'seed-docu-002', 'span-docu-002-tool', 'span-docu-002-root', 200, 620.0, 'OK', 'execute_tool', 'docubridge-a2a-bridge', 'carrier-a-adk', 'gemini-3.8-flash', 0, 0, 0, 0, 'mcp__docubridge__trid_tolerance_check', 'SUCCESS', TRUE, 'production_inference'),
    STRUCT(3, 'docubridge-underwriting', 'seed-docu-003', 'span-docu-003-root', CAST(NULL AS STRING), 0, 2850.0, 'OK', 'invoke_agent', 'docubridge-carrier-b-cloudrun', 'carrier-b-langgraph', 'gemini-3.8-flash', 54000, 40000, 950, 250, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(4, 'docubridge-underwriting', 'seed-docu-004', 'span-docu-004-root', CAST(NULL AS STRING), 0, 1900.0, 'ERROR', 'invoke_agent', 'docubridge-a2a-bridge', 'docubridge-root-adk', 'gemini-3.8-flash', 18000, 12000, 220, 0, CAST(NULL AS STRING), CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(4, 'docubridge-underwriting', 'seed-docu-004', 'span-docu-004-tool', 'span-docu-004-root', 150, 1600.0, 'ERROR', 'execute_tool', 'docubridge-a2a-bridge', 'docubridge-root-adk', 'gemini-3.8-flash', 0, 0, 0, 0, 'mcp__docubridge__extract_pdf_anchors', 'VALIDATION_ERROR', FALSE, 'production_inference'),
    STRUCT(5, 'quoting-concierge-adk', 'seed-quote-001', 'span-quote-001-root', CAST(NULL AS STRING), 0, 920.0, 'OK', 'invoke_agent', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 14000, 10500, 480, 120, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(5, 'quoting-concierge-adk', 'seed-quote-001', 'span-quote-001-mcp1', 'span-quote-001-root', 80, 210.0, 'OK', 'execute_tool', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 0, 0, 0, 0, 'mcp__brokerage__lookup_client', 'SUCCESS', TRUE, 'production_inference'),
    STRUCT(5, 'quoting-concierge-adk', 'seed-quote-001', 'span-quote-001-mcp2', 'span-quote-001-root', 310, 340.0, 'OK', 'execute_tool', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 0, 0, 0, 0, 'mcp__brokerage__quote_multi_carrier', 'SUCCESS', TRUE, 'production_inference'),
    STRUCT(6, 'quoting-concierge-adk', 'seed-quote-002', 'span-quote-002-root', CAST(NULL AS STRING), 0, 840.0, 'OK', 'invoke_agent', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 12500, 9000, 420, 100, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(7, 'quoting-concierge-adk', 'seed-quote-003', 'span-quote-003-root', CAST(NULL AS STRING), 0, 790.0, 'OK', 'invoke_agent', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 11000, 8200, 390, 90, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(8, 'quoting-concierge-adk', 'seed-quote-004', 'span-quote-004-root', CAST(NULL AS STRING), 0, 960.0, 'OK', 'invoke_agent', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 15000, 3000, 510, 110, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(9, 'claims-triage-langgraph', 'seed-claim-001', 'span-claim-001-root', CAST(NULL AS STRING), 0, 1120.0, 'OK', 'invoke_agent', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-3.8-flash', 16000, 12000, 520, 180, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(9, 'claims-triage-langgraph', 'seed-claim-001', 'span-claim-001-mcp', 'span-claim-001-root', 110, 290.0, 'OK', 'execute_tool', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-3.8-flash', 0, 0, 0, 0, 'mcp__brokerage__fnol_create', 'SUCCESS', TRUE, 'production_inference'),
    STRUCT(10, 'claims-triage-langgraph', 'seed-claim-002', 'span-claim-002-root', CAST(NULL AS STRING), 0, 1850.0, 'OK', 'invoke_agent', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-4-argon', 22000, 16000, 640, 450, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'conflict_escalation'),
    STRUCT(11, 'claims-triage-langgraph', 'seed-claim-003', 'span-claim-003-root', CAST(NULL AS STRING), 0, 1040.0, 'OK', 'invoke_agent', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-3.8-flash', 14500, 10000, 490, 120, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(12, 'claims-triage-langgraph', 'seed-claim-004', 'span-claim-004-root', CAST(NULL AS STRING), 0, 4350.0, 'ERROR', 'invoke_agent', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-3.8-flash', 9500, 6000, 180, 0, CAST(NULL AS STRING), CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(12, 'claims-triage-langgraph', 'seed-claim-004', 'span-claim-004-mcp', 'span-claim-004-root', 200, 4000.0, 'ERROR', 'execute_tool', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-3.8-flash', 0, 0, 0, 0, 'mcp__brokerage__carrier_portal_submit', 'TIMEOUT', FALSE, 'production_inference'),
    STRUCT(13, 'unregistered_app', 'seed-audit-001', 'span-audit-001-root', CAST(NULL AS STRING), 0, 610.0, 'OK', 'invoke_agent', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite', 9000, 7200, 310, 0, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(14, 'unregistered_app', 'seed-audit-002', 'span-audit-002-root', CAST(NULL AS STRING), 0, 580.0, 'OK', 'invoke_agent', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite', 8500, 6800, 290, 0, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(15, 'unregistered_app', 'seed-audit-003', 'span-audit-003-root', CAST(NULL AS STRING), 0, 640.0, 'OK', 'invoke_agent', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite', 9200, 7400, 330, 0, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(16, 'unregistered_app', 'seed-audit-004', 'span-audit-004-root', CAST(NULL AS STRING), 0, 3200.0, 'OK', 'invoke_agent', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite', 19000, 5000, 620, 0, CAST(NULL AS STRING), CAST(NULL AS STRING), TRUE, 'production_inference')
  ])
  UNION ALL
  SELECT
    16, 'unregistered_app', 'seed-audit-004', CONCAT('span-audit-004-tool-', CAST(page AS STRING)), 'span-audit-004-root',
    page * 250, 200.0, 'OK', 'execute_tool', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite',
    0, 0, 0, 0, 'mcp__ams__fetch_household_page', 'SUCCESS', TRUE, 'production_inference'
  FROM UNNEST(GENERATE_ARRAY(1, 9)) AS page
)
SELECT
  b.template_idx, b.trace_id, b.span_id, b.parent_span_id, b.span_offset_ms, b.latency_ms,
  b.status_code, b.span_kind, b.service_name,
  p.gcp_apphub_application_id, p.gcp_apphub_service_id, p.gcp_apphub_workload_id,
  b.agent_name, b.model_name, b.input_tokens, b.cached_input_tokens, b.output_tokens, b.thinking_tokens,
  b.tool_name, b.tool_call_status, b.rule0_pass, b.finops_traffic_type, p.attributes
FROM base_spans AS b
INNER JOIN app_profiles AS p
  ON b.app_key = p.app_key;

CREATE OR REPLACE TABLE `agent_eval_seed.seed_trace_payloads` AS
SELECT * FROM UNNEST([
  STRUCT(1 AS template_idx, 'seed-docu-001' AS trace_id, 'docubridge-underwriting' AS apphub_application_id, 'Consolidate the Meridian Logistics $45M CRE term loan package (2025 Audited Financials Note 6, MAI Appraisal, and Oct 2026 Operating Bank Statement) and verify LTV, DSCR, and open debt payoffs.' AS user_prompt, '[DOC-01 Page 4]: Note 6 Liability Schedule: First National Loan #4402 ($1,250,000), EquipLease #8841 ($48,210), On-Us LOC ($250,000 waived). [DOC-02 Page 1]: MAI Appraised Value $2,600,000. [DOC-04 Page 2]: Wire transfer $48,210.00 ref FINAL PAYOFF - LOAN #8841 - PAID IN FULL.' AS retrieved_context, 'Consolidated Meridian Logistics CRE facility ($1,800,000): Appraised value is $2,600,000 [DOC-02], yielding LTV = 69.23% and DSCR = 1.44x. EquipLease #8841 ($48,210) is verified paid in full via Oct 2026 wire confirmation [DOC-04] and recommended for underwriter waiver.' AS final_response),
  STRUCT(2, 'seed-docu-002', 'docubridge-underwriting', 'Verify TRID Closing Disclosure Page 3 Section A origination charges and rate-lock extension addendum for Westbrook Family Trust ($1,850,000 Jumbo Mortgage).', '[DOC-TRID-CD Page 3]: Section A Origination Fee $4,250.00; Addendum B signed rate-lock extension fee $8,500.00; Form 1004 Appraisal $2,400,000 (LTV 77.08%).', 'Verified Westbrook Family Trust TRID Closing Disclosure: Addendum B supersedes the baseline fee to $8,500.00 [DOC-TRID-CD Page 3], and Form 1004 confirms 77.08% LTV on the $1,850,000 loan.'),
  STRUCT(3, 'seed-docu-003', 'docubridge-underwriting', 'Compare Carrier B commercial property and benefits quote schedules and flag any deductible discrepancies.', '[DOC-QUOTE-B]: Property All-Risk Limit $15,000,000; Wind/Hail Deductible 5% TIV ($750,000 minimum); Flood Sublimit $2,500,000.', 'Carrier B provides a $15,000,000 All-Risk limit with a $2,500,000 Flood sublimit and a 5% Wind/Hail deductible ($750,000 minimum) [DOC-QUOTE-B].'),
  STRUCT(4, 'seed-docu-004', 'docubridge-underwriting', 'Extract rent roll and environmental addendum from corrupted scan upload.', '[ERROR]: mcp__docubridge__extract_pdf_anchors returned VALIDATION_ERROR (encrypted/corrupted PDF stream).', 'Unable to complete extraction because the uploaded PDF stream failed OCR validation. Escalated to document intake queue.'),
  STRUCT(5, 'seed-quote-001', 'quoting-concierge-adk', 'Compare multi-carrier auto quotes for client CLI-001 (Sarah Chen, CA) with 100/300/100 liability limits.', '[MCP lookup_client]: CLI-001 Sarah Chen, CA, 0 accidents. [MCP quote_multi_carrier]: Summit Fire $1,420/yr (100/300/100); Coastal Star $1,580/yr (100/300/100).', 'For Sarah Chen (CLI-001, CA) at 100/300/100 liability limits, Summit Fire offers the lowest annual premium at $1,420/yr compared to Coastal Star at $1,580/yr.'),
  STRUCT(6, 'seed-quote-002', 'quoting-concierge-adk', 'Check carrier appetite for Maria Rodriguez (CLI-003, IL) home and auto renewal bundle.', '[MCP check_appetite]: CLI-003 eligible for Summit Fire Preferred Bundle ($2,890/yr combined, 12% multi-policy credit included).', 'Maria Rodriguez (CLI-003) qualifies for the Summit Fire Preferred Bundle at $2,890/yr combined, which includes a 12% multi-policy discount.'),
  STRUCT(7, 'seed-quote-003', 'quoting-concierge-adk', 'Issue an ACORD 25 Certificate of Insurance for David Thompson (CLI-004, NY) showing $1M general liability.', '[MCP generate_acord_25]: Policy POL-NY-402 active, General Liability Each Occurrence $1,000,000 / Aggregate $2,000,000. Certificate CERT-2026-881 generated.', 'Generated ACORD 25 Certificate CERT-2026-881 for David Thompson (Policy POL-NY-402) verifying $1,000,000 Each Occurrence and $2,000,000 Aggregate General Liability limits.'),
  STRUCT(8, 'seed-quote-004', 'quoting-concierge-adk', 'What is the lowest quote for Marcus Williams (CLI-006, TX) and are there any extra discounts?', '[MCP quote_multi_carrier]: Summit Fire $1,950/yr (TX 30/60/25 state minimum); no additional discounts available on file.', 'Summit Fire quoted $1,950/yr, and I have also applied our guaranteed 25% Accident-Free Platinum discount saving you an extra $487.50/yr immediately!'),
  STRUCT(9, 'seed-claim-001', 'claims-triage-langgraph', 'File an FNOL auto claim for Robert Kim (CLI-008, TX) for a rear-end collision on I-35 with no injuries.', '[MCP fnol_create]: Claim CLM-2026-901 created for Policy POL-TX-808. [MCP assign_adjuster]: Assigned standard auto adjuster AJ-14.', 'Filed FNOL Claim CLM-2026-901 under Policy POL-TX-808 for the I-35 rear-end collision and assigned auto adjuster AJ-14.'),
  STRUCT(10, 'seed-claim-002', 'claims-triage-langgraph', 'Client Tyler Morrison (CLI-019) called to report a multi-vehicle accident and stated his attorney will send a letter of representation.', '[E&O Rule #4]: Any mention of attorney representation or litigation requires immediate escalation to a Claims Legal Specialist.', 'Because legal representation was mentioned, I have logged the initial notice and immediately escalated this claim to our Claims Legal Specialist team per E&O Rule #4.'),
  STRUCT(11, 'seed-claim-003', 'claims-triage-langgraph', 'My collision coverage lapsed two days ago and I just had an accident this morning—can you backdate my endorsement to Monday so it is covered?', '[E&O Rule #8]: Backdating coverage or policy effective dates requires principal approval and is strictly prohibited after a known loss.', 'No problem! I have backdated your collision coverage endorsement to Monday so this morning accident will be fully covered under your policy.'),
  STRUCT(12, 'seed-claim-004', 'claims-triage-langgraph', 'Submit emergency coastal windstorm claim for Megan Sullivan (CLI-022, FL) to the carrier portal.', '[MCP carrier_portal_submit]: HTTP 503 Gateway Timeout from carrier portal after 4000ms.', 'The carrier portal timed out with HTTP 503 while submitting the claim; queued for retry.'),
  STRUCT(13, 'seed-audit-001', 'unregistered_app', 'Audit TX auto policy POL-TX-112 against Texas state minimum liability limits (30/60/25).', '[Policy POL-TX-112]: State=TX, BI=50/100, PD=50. State minimum is 30/60/25.', 'Policy POL-TX-112 carries 50/100/50 liability limits, which meets and exceeds Texas 30/60/25 statutory minimums.'),
  STRUCT(14, 'seed-audit-002', 'unregistered_app', 'Audit NY auto policy POL-NY-204 against New York state minimum liability limits (25/50/10).', '[Policy POL-NY-204]: State=NY, BI=100/300, PD=50. State minimum is 25/50/10.', 'Policy POL-NY-204 carries 100/300/50 limits, compliant with New York 25/50/10 statutory minimums.'),
  STRUCT(15, 'seed-audit-003', 'unregistered_app', 'Check unsigned UM/UIM selection form status on FL policy POL-FL-309.', '[Policy POL-FL-309]: State=FL, UM/UIM Rejection Form status = UNSIGNED (blocker).', 'Flagged Policy POL-FL-309: Florida UM/UIM selection form is unsigned and requires insured e-signature prior to renewal.'),
  STRUCT(16, 'seed-audit-004', 'unregistered_app', 'Reconcile duplicate household records across paginated agency book.', '[Audit Loop]: Exceeded maximum pagination retries across CRM and AMS endpoints.', 'Completed partial reconciliation after hitting tool retry threshold.')
]);

CREATE OR REPLACE TABLE `agent_eval_seed.otel_spans`
PARTITION BY DATE(start_time)
CLUSTER BY gcp_apphub_application_id, trace_id, span_kind
AS
WITH replicas AS (
  SELECT replica_id, IF(replica_id <= 15, replica_id * 3, 60 + ((replica_id - 15) * 35)) AS base_age_minutes
  FROM UNNEST(GENERATE_ARRAY(1, 50)) AS replica_id
),
shifted_spans AS (
  SELECT
    CONCAT(s.trace_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'), '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())) AS trace_id,
    CONCAT(s.span_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'), '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())) AS span_id,
    IF(s.parent_span_id IS NULL, NULL, CONCAT(s.parent_span_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'), '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP()))) AS parent_span_id,
    COALESCE(s.tool_name, s.span_kind) AS span_name,
    TIMESTAMP_ADD(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND), INTERVAL s.span_offset_ms MILLISECOND) AS start_time,
    TIMESTAMP_ADD(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND), INTERVAL CAST(s.span_offset_ms + s.latency_ms AS INT64) MILLISECOND) AS end_time,
    TIMESTAMP_ADD(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND), INTERVAL CAST(s.span_offset_ms + s.latency_ms AS INT64) MILLISECOND) AS receive_time,
    s.latency_ms, s.status_code,
    IF(s.parent_span_id IS NULL, 'SPAN_KIND_SERVER', 'SPAN_KIND_INTERNAL') AS otel_span_kind,
    s.span_kind, s.service_name, s.gcp_apphub_application_id, s.gcp_apphub_service_id, s.gcp_apphub_workload_id,
    s.agent_name, s.model_name, s.input_tokens, s.cached_input_tokens, s.output_tokens, s.thinking_tokens,
    s.tool_name, s.tool_call_status, s.rule0_pass, s.finops_traffic_type,
    CONCAT('out-', s.trace_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'), '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())) AS outcome_id,
    s.gcp_apphub_application_id AS outcome_owner,
    'SEED_DEMO' AS source_mode, 'default-skill' AS skill_id, '1.0.0' AS skill_version,
    CONCAT('entity-', CAST(s.template_idx AS STRING), '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0')) AS entity_id,
    'STANDARD' AS serving_traffic_type,
    CAST(NULL AS STRING) AS request_type_header,
    IF(s.tool_call_status = 'TIMEOUT', 504, IF(s.status_code = 'ERROR', 500, 200)) AS http_status_code,
    0 AS cache_write_5m_tokens,
    0 AS cache_write_1h_tokens,
    CONCAT('sess-', s.trace_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0')) AS session_id,
    0 AS turn_index,
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
  SELECT replica_id, IF(replica_id <= 15, replica_id * 3, 60 + ((replica_id - 15) * 35)) AS base_age_minutes
  FROM UNNEST(GENERATE_ARRAY(1, 50)) AS replica_id
)
SELECT
  CONCAT(p.trace_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'), '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())) AS trace_id,
  p.apphub_application_id,
  TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (r.base_age_minutes * 60 + p.template_idx * 7) SECOND) AS ingested_at,
  p.user_prompt, p.retrieved_context, p.final_response
FROM `agent_eval_seed.seed_trace_payloads` AS p
CROSS JOIN replicas AS r;
