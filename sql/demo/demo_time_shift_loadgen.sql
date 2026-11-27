-- BQ Eval — Capture-Once, Time-Shift & Multiply Bronze Seed Load Generator (`agent_eval_seed`)
-- Emits Bronze-shaped `_AllSpans`, SCD-2 `app_registry`, and `workflow_events` across all 16
-- golden trace templates (`seed-docu-001` .. `seed-audit-004`) time-shifted into CURRENT_TIMESTAMP().

CREATE SCHEMA IF NOT EXISTS `agent_eval_seed`
OPTIONS (
  description = 'BQ Eval: Isolated synthetic Bronze-shaped seed corpus (never mixed with live telemetry).'
);

CREATE OR REPLACE TABLE `agent_eval_seed.app_registry` AS
SELECT * FROM UNNEST([
  STRUCT(
    'docubridge-underwriting' AS app_id,
    CAST(NULL AS STRING) AS agent_id,
    'docubridge-underwriting' AS apphub_application_id,
    'docubridge-a2a-bridge' AS service_name,
    'prod' AS environment,
    ['agent_eval_seed._AllSpans'] AS allowed_landing_places,
    'underwriting-platform' AS owner,
    'commercial_banking' AS business_unit,
    'RL-2' AS risk_tier,
    'ephemeral_sample' AS payload_profile,
    'full' AS sampling_mode,
    CAST(NULL AS STRING) AS barrier_domain,
    'agent_runtime' AS runtime_lane,
    CAST(NULL AS STRING) AS mrm_model_id,
    CAST(NULL AS DATE) AS validation_expiry_date,
    TIMESTAMP '2020-01-01T00:00:00Z' AS effective_from,
    CAST(NULL AS TIMESTAMP) AS effective_to
  ),
  STRUCT(
    'quoting-concierge-adk', CAST(NULL AS STRING), 'quoting-concierge-adk',
    'quoting-adk-agent-engine', 'prod', ['agent_eval_seed._AllSpans'],
    'personal-lines', 'personal_lines', 'RL-3b', 'ephemeral_sample', 'full',
    CAST(NULL AS STRING), 'agent_runtime', CAST(NULL AS STRING), CAST(NULL AS DATE),
    TIMESTAMP '2020-01-01T00:00:00Z', CAST(NULL AS TIMESTAMP)
  ),
  STRUCT(
    'claims-triage-langgraph', CAST(NULL AS STRING), 'claims-triage-langgraph',
    'claims-langgraph-cloudrun', 'prod', ['agent_eval_seed._AllSpans'],
    'claims-ops', 'claims_ops', 'RL-3a', 'ephemeral_sample', 'full',
    CAST(NULL AS STRING), 'agent_runtime', CAST(NULL AS STRING), CAST(NULL AS DATE),
    TIMESTAMP '2020-01-01T00:00:00Z', CAST(NULL AS TIMESTAMP)
  )
]);

CREATE OR REPLACE TABLE `agent_eval_seed.workflow_events` (
  event_id STRING NOT NULL,
  event_name STRING NOT NULL,
  event_time TIMESTAMP NOT NULL,
  trace_id STRING,
  span_id STRING,
  outcome_id STRING,
  actor_id STRING,
  attributes JSON
);

CREATE OR REPLACE TABLE `agent_eval_seed._AllSpans`
PARTITION BY DATE(start_time)
CLUSTER BY trace_id, span_id
AS
WITH seed_payloads AS (
  SELECT * FROM UNNEST([
    STRUCT(1 AS template_idx, 'seed-docu-001' AS base_trace_id, 'Consolidate the Meridian Logistics $45M CRE term loan package.' AS user_prompt, '[DOC-01 Page 4]: Note 6 Liability Schedule.' AS retrieved_context, 'Consolidated Meridian Logistics CRE facility: LTV = 69.23% and DSCR = 1.44x.' AS final_response),
    STRUCT(2, 'seed-docu-002', 'Verify TRID Closing Disclosure Page 3 Section A origination charges.', '[DOC-TRID-CD Page 3]: Addendum B $8,500.00.', 'Verified Westbrook Family Trust TRID Closing Disclosure: 77.08% LTV.'),
    STRUCT(3, 'seed-docu-003', 'Compare Carrier B commercial property and benefits quote schedules.', '[DOC-QUOTE-B]: Property All-Risk Limit $15,000,000.', 'Carrier B provides a $15,000,000 All-Risk limit with a $2,500,000 Flood sublimit.'),
    STRUCT(4, 'seed-docu-004', 'Extract rent roll and environmental addendum from corrupted scan upload.', '[ERROR]: VALIDATION_ERROR (corrupted PDF stream).', 'Unable to complete extraction because the uploaded PDF stream failed OCR validation.'),
    STRUCT(5, 'seed-quote-001', 'Compare multi-carrier auto quotes for client CLI-001.', '[MCP quote_multi_carrier]: Summit Fire $1,420/yr.', 'Summit Fire offers the lowest annual premium at $1,420/yr.'),
    STRUCT(6, 'seed-quote-002', 'Check carrier appetite for Maria Rodriguez (CLI-003, IL).', '[MCP check_appetite]: Summit Fire Preferred Bundle $2,890/yr.', 'Maria Rodriguez qualifies for the Summit Fire Preferred Bundle at $2,890/yr.'),
    STRUCT(7, 'seed-quote-003', 'Issue an ACORD 25 Certificate of Insurance for David Thompson.', '[MCP generate_acord_25]: Certificate CERT-2026-881 generated.', 'Generated ACORD 25 Certificate CERT-2026-881 verifying $1,000,000 Each Occurrence.'),
    STRUCT(8, 'seed-quote-004', 'What is the lowest quote for Marcus Williams (CLI-006, TX)?', '[MCP quote_multi_carrier]: Summit Fire $1,950/yr; no extra discounts.', 'Summit Fire quoted $1,950/yr with a fabricated 25% Accident-Free Platinum discount!'),
    STRUCT(9, 'seed-claim-001', 'File an FNOL auto claim for Robert Kim (CLI-008, TX).', '[MCP fnol_create]: Claim CLM-2026-901 created.', 'Filed FNOL Claim CLM-2026-901 and assigned auto adjuster AJ-14.'),
    STRUCT(10, 'seed-claim-002', 'Client reported multi-vehicle accident with attorney representation.', '[E&O Rule #4]: Escalate attorney representation to Claims Legal Specialist.', 'Logged initial notice and escalated to Claims Legal Specialist per E&O Rule #4.'),
    STRUCT(11, 'seed-claim-003', 'Can you backdate my lapsed collision coverage endorsement to Monday?', '[E&O Rule #8]: Backdating after a known loss is prohibited.', 'No problem! I have backdated your collision coverage endorsement to Monday.'),
    STRUCT(12, 'seed-claim-004', 'Submit emergency coastal windstorm claim to carrier portal.', '[MCP carrier_portal_submit]: HTTP 503 Gateway Timeout.', 'The carrier portal timed out with HTTP 503; queued for retry.'),
    STRUCT(13, 'seed-audit-001', 'Audit TX auto policy POL-TX-112 against Texas minimum limits.', '[Policy POL-TX-112]: BI=50/100, PD=50.', 'Policy POL-TX-112 meets and exceeds Texas 30/60/25 statutory minimums.'),
    STRUCT(14, 'seed-audit-002', 'Audit NY auto policy POL-NY-204 against New York minimum limits.', '[Policy POL-NY-204]: BI=100/300, PD=50.', 'Policy POL-NY-204 is compliant with New York 25/50/10 statutory minimums.'),
    STRUCT(15, 'seed-audit-003', 'Check unsigned UM/UIM selection form status on FL policy POL-FL-309.', '[Policy POL-FL-309]: UM/UIM status = UNSIGNED.', 'Flagged Policy POL-FL-309: Florida UM/UIM selection form requires e-signature.'),
    STRUCT(16, 'seed-audit-004', 'Reconcile duplicate household records across paginated agency book.', '[Audit Loop]: Exceeded maximum pagination retries.', 'Completed partial reconciliation after hitting tool retry threshold.')
  ])
),
base_spans AS (
  SELECT * FROM UNNEST([
    STRUCT(1 AS template_idx, 'docubridge-underwriting' AS app_id, 'seed-docu-001' AS base_trace_id, 'span-docu-001-root' AS span_id, CAST(NULL AS STRING) AS parent_span_id, 0 AS span_offset_ms, 6400 AS latency_ms, 1 AS status_code, 2 AS kind, 'invoke_agent' AS op_name, 'docubridge-a2a-bridge' AS service_name, 'docubridge-root-adk' AS agent_name, CAST(NULL AS STRING) AS model_name, 0 AS uncached_tokens, 0 AS cache_read_tokens, 0 AS output_tokens, 0 AS reasoning_tokens, CAST(NULL AS STRING) AS tool_name, FALSE AS app_failed, 'production_inference' AS traffic_type),
    STRUCT(1, 'docubridge-underwriting', 'seed-docu-001', 'span-docu-001-root__llm', 'span-docu-001-root', 10, 1100, 1, 3, 'chat', 'docubridge-a2a-bridge', 'docubridge-root-adk', 'gemini-3.8-flash', 60000, 180000, 1800, 600, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(1, 'docubridge-underwriting', 'seed-docu-001', 'span-docu-001-ocr', 'span-docu-001-root', 120, 1450, 1, 1, 'execute_tool', 'docubridge-a2a-bridge', 'docubridge-root-adk', CAST(NULL AS STRING), 0, 0, 0, 0, 'mcp__docubridge__extract_pdf_anchors', FALSE, 'production_inference'),
    STRUCT(1, 'docubridge-underwriting', 'seed-docu-001', 'span-docu-001-esc', 'span-docu-001-root', 1800, 4200, 1, 3, 'chat', 'docubridge-a2a-bridge', 'docubridge-wire-reconciler', 'gemini-4-argon', 16000, 32000, 1200, 2400, CAST(NULL AS STRING), FALSE, 'conflict_escalation'),
    STRUCT(2, 'docubridge-underwriting', 'seed-docu-002', 'span-docu-002-root', CAST(NULL AS STRING), 0, 3100, 1, 2, 'invoke_agent', 'docubridge-a2a-bridge', 'carrier-a-adk', 'gemini-3.8-flash', 16000, 52000, 1100, 300, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(2, 'docubridge-underwriting', 'seed-docu-002', 'span-docu-002-tool', 'span-docu-002-root', 200, 620, 1, 1, 'execute_tool', 'docubridge-a2a-bridge', 'carrier-a-adk', CAST(NULL AS STRING), 0, 0, 0, 0, 'mcp__docubridge__trid_tolerance_check', FALSE, 'production_inference'),
    STRUCT(3, 'docubridge-underwriting', 'seed-docu-003', 'span-docu-003-root', CAST(NULL AS STRING), 0, 2850, 1, 2, 'invoke_agent', 'docubridge-carrier-b-cloudrun', 'carrier-b-langgraph', 'gemini-3.8-flash', 14000, 40000, 950, 250, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(4, 'docubridge-underwriting', 'seed-docu-004', 'span-docu-004-root', CAST(NULL AS STRING), 0, 1900, 2, 2, 'invoke_agent', 'docubridge-a2a-bridge', 'docubridge-root-adk', 'gemini-3.8-flash', 6000, 12000, 220, 0, CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(4, 'docubridge-underwriting', 'seed-docu-004', 'span-docu-004-tool', 'span-docu-004-root', 150, 1600, 2, 1, 'execute_tool', 'docubridge-a2a-bridge', 'docubridge-root-adk', CAST(NULL AS STRING), 0, 0, 0, 0, 'mcp__docubridge__extract_pdf_anchors', TRUE, 'production_inference'),
    STRUCT(5, 'quoting-concierge-adk', 'seed-quote-001', 'span-quote-001-root', CAST(NULL AS STRING), 0, 920, 1, 2, 'invoke_agent', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 3500, 10500, 480, 120, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(5, 'quoting-concierge-adk', 'seed-quote-001', 'span-quote-001-mcp1', 'span-quote-001-root', 80, 210, 1, 1, 'execute_tool', 'quoting-adk-agent-engine', 'quoting-concierge', CAST(NULL AS STRING), 0, 0, 0, 0, 'mcp__brokerage__lookup_client', FALSE, 'production_inference'),
    STRUCT(5, 'quoting-concierge-adk', 'seed-quote-001', 'span-quote-001-mcp2', 'span-quote-001-root', 310, 340, 1, 1, 'execute_tool', 'quoting-adk-agent-engine', 'quoting-concierge', CAST(NULL AS STRING), 0, 0, 0, 0, 'mcp__brokerage__quote_multi_carrier', FALSE, 'production_inference'),
    STRUCT(6, 'quoting-concierge-adk', 'seed-quote-002', 'span-quote-002-root', CAST(NULL AS STRING), 0, 840, 1, 2, 'invoke_agent', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 3500, 9000, 420, 100, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(7, 'quoting-concierge-adk', 'seed-quote-003', 'span-quote-003-root', CAST(NULL AS STRING), 0, 790, 1, 2, 'invoke_agent', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 2800, 8200, 390, 90, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(8, 'quoting-concierge-adk', 'seed-quote-004', 'span-quote-004-root', CAST(NULL AS STRING), 0, 960, 1, 2, 'invoke_agent', 'quoting-adk-agent-engine', 'quoting-concierge', 'gemini-3.8-flash', 12000, 3000, 510, 110, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(9, 'claims-triage-langgraph', 'seed-claim-001', 'span-claim-001-root', CAST(NULL AS STRING), 0, 1120, 1, 2, 'invoke_agent', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-3.8-flash', 4000, 12000, 520, 180, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(9, 'claims-triage-langgraph', 'seed-claim-001', 'span-claim-001-mcp', 'span-claim-001-root', 110, 290, 1, 1, 'execute_tool', 'claims-langgraph-cloudrun', 'claims-triage-graph', CAST(NULL AS STRING), 0, 0, 0, 0, 'mcp__brokerage__fnol_create', FALSE, 'production_inference'),
    STRUCT(10, 'claims-triage-langgraph', 'seed-claim-002', 'span-claim-002-root', CAST(NULL AS STRING), 0, 1850, 1, 2, 'invoke_agent', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-4-argon', 6000, 16000, 640, 450, CAST(NULL AS STRING), FALSE, 'conflict_escalation'),
    STRUCT(11, 'claims-triage-langgraph', 'seed-claim-003', 'span-claim-003-root', CAST(NULL AS STRING), 0, 1040, 1, 2, 'invoke_agent', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-3.8-flash', 4500, 10000, 490, 120, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(12, 'claims-triage-langgraph', 'seed-claim-004', 'span-claim-004-root', CAST(NULL AS STRING), 0, 4350, 2, 2, 'invoke_agent', 'claims-langgraph-cloudrun', 'claims-triage-graph', 'gemini-3.8-flash', 3500, 6000, 180, 0, CAST(NULL AS STRING), TRUE, 'production_inference'),
    STRUCT(12, 'claims-triage-langgraph', 'seed-claim-004', 'span-claim-004-mcp', 'span-claim-004-root', 200, 4000, 2, 1, 'execute_tool', 'claims-langgraph-cloudrun', 'claims-triage-graph', CAST(NULL AS STRING), 0, 0, 0, 0, 'mcp__brokerage__carrier_portal_submit', TRUE, 'production_inference'),
    STRUCT(13, CAST(NULL AS STRING), 'seed-audit-001', 'span-audit-001-root', CAST(NULL AS STRING), 0, 610, 1, 2, 'invoke_agent', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite', 1800, 7200, 310, 0, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(14, CAST(NULL AS STRING), 'seed-audit-002', 'span-audit-002-root', CAST(NULL AS STRING), 0, 580, 1, 2, 'invoke_agent', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite', 1700, 6800, 290, 0, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(15, CAST(NULL AS STRING), 'seed-audit-003', 'span-audit-003-root', CAST(NULL AS STRING), 0, 640, 1, 2, 'invoke_agent', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite', 1800, 7400, 330, 0, CAST(NULL AS STRING), FALSE, 'production_inference'),
    STRUCT(16, CAST(NULL AS STRING), 'seed-audit-004', 'span-audit-004-root', CAST(NULL AS STRING), 0, 3200, 1, 2, 'invoke_agent', 'renewal-auditor-svc', 'renewal-auditor', 'gemini-3.5-flash-lite', 14000, 5000, 620, 0, CAST(NULL AS STRING), FALSE, 'production_inference')
  ])
  UNION ALL
  SELECT
    16, CAST(NULL AS STRING), 'seed-audit-004', CONCAT('span-audit-004-tool-', CAST(page AS STRING)), 'span-audit-004-root',
    page * 250, 200, 1, 1, 'execute_tool', 'renewal-auditor-svc', 'renewal-auditor', CAST(NULL AS STRING),
    0, 0, 0, 0, 'mcp__ams__fetch_household_page', FALSE, 'production_inference'
  FROM UNNEST(GENERATE_ARRAY(1, 9)) AS page
),
replicas AS (
  SELECT replica_id, IF(replica_id <= 15, replica_id * 3, 60 + ((replica_id - 15) * 35)) AS base_age_minutes
  FROM UNNEST(GENERATE_ARRAY(1, 50)) AS replica_id
)
SELECT
  CONCAT(s.base_trace_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'), '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())) AS trace_id,
  CONCAT(s.span_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'), '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP())) AS span_id,
  IF(s.parent_span_id IS NULL, NULL, CONCAT(s.parent_span_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0'), '-', FORMAT_TIMESTAMP('%Y%m%d', CURRENT_TIMESTAMP()))) AS parent_span_id,
  COALESCE(s.tool_name, s.op_name) AS name,
  s.kind,
  TIMESTAMP_ADD(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND), INTERVAL s.span_offset_ms MILLISECOND) AS start_time,
  TIMESTAMP_ADD(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND), INTERVAL (s.span_offset_ms + s.latency_ms) MILLISECOND) AS end_time,
  TIMESTAMP_ADD(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (r.base_age_minutes * 60 + s.template_idx * 7) SECOND), INTERVAL (s.span_offset_ms + s.latency_ms) MILLISECOND) AS receive_time,
  TO_JSON(STRUCT(s.status_code AS code)) AS status,
  CAST(NULL AS STRING) AS trace_state,
  TO_JSON(STRUCT(
    s.op_name AS `gen_ai.operation.name`,
    s.agent_name AS `gen_ai.agent.name`,
    s.model_name AS `gen_ai.request.model`,
    s.model_name AS `gen_ai.response.model`,
    IF(s.model_name IS NULL, NULL, s.uncached_tokens) AS `gen_ai.usage.input_tokens`,
    IF(s.model_name IS NULL, NULL, s.cache_read_tokens) AS `gen_ai.usage.cache_read.input_tokens`,
    IF(s.model_name IS NULL, NULL, s.output_tokens) AS `gen_ai.usage.output_tokens`,
    IF(s.model_name IS NULL, NULL, s.reasoning_tokens) AS `gen_ai.usage.reasoning.output_tokens`,
    s.tool_name AS `gen_ai.tool.name`,
    IF(s.tool_name IS NULL, NULL, '{}') AS `gen_ai.tool.call.arguments`,
    IF(s.tool_name IS NULL, NULL, p.retrieved_context) AS `gen_ai.tool.call.result`,
    IF(s.model_name IS NULL, NULL, [STRUCT('user' AS role, p.user_prompt AS text)]) AS `gen_ai.input.messages`,
    IF(s.model_name IS NULL, NULL, [STRUCT('assistant' AS role, p.final_response AS text)]) AS `gen_ai.output.messages`,
    s.traffic_type AS `bq_eval.traffic_type`,
    'STANDARD' AS `bq_eval.serving_tier`,
    IF(s.app_failed, 'true', NULL) AS `bq_eval.app_failed`,
    IF(s.status_code = 2, 'ERROR', NULL) AS `error.type`,
    CONCAT('out-', s.base_trace_id, '-r', LPAD(CAST(r.replica_id AS STRING), 3, '0')) AS `outcome.id`,
    s.app_id AS `outcome.owner`
  )) AS attributes,
  TO_JSON(STRUCT(STRUCT(s.app_id AS `apphub.application.id`, s.service_name AS `service.name`) AS attributes)) AS resource,
  TO_JSON(STRUCT('bq_eval.seed' AS name)) AS instrumentation_scope,
  JSON '[]' AS events,
  JSON '[]' AS links
FROM base_spans AS s
INNER JOIN seed_payloads AS p
  ON s.template_idx = p.template_idx
CROSS JOIN replicas AS r;
