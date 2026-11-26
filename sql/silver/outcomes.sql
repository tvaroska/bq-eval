-- Silver Outcomes (`outcomes_base`, `silver_restricted.outcomes`, `silver_open.outcomes_open`).
-- One row per `outcome.id` active in `[from_time, to_time)`, aggregated over
-- `[from_time - ${OUTCOME_LOOKBACK_DAYS} DAY, to_time + ${READ_MARGIN_MINUTES} MINUTE)`
-- with `extends_outside_window`, `ADR-047` close-winner authority precedence,
-- `min_model_confidence` + `final_model_confidence`, and restricted-only `outcome_subject`.

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.outcomes_base`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  WITH trace_agg AS (
    -- Single scan of `traces_base` over lookback window (carries activity flag + model confidence).
    SELECT
      t.outcome_id,
      MAX(CASE WHEN t.trace_start_time >= from_time AND t.trace_start_time < to_time THEN 1 ELSE 0 END) AS has_active_trace,
      COUNT(*) AS trace_count,
      TO_JSON(ARRAY_AGG(t.trace_id ORDER BY t.trace_start_time ASC, t.trace_id ASC)) AS trace_ids,
      COUNTIF(t.root_status = 'ERROR') AS root_error_trace_count,
      COUNTIF(t.app_failed IS TRUE) AS failed_trace_count,
      COUNTIF(t.has_root_span IS FALSE) AS rootless_trace_count,
      MAX(t.last_span_arrival_time) AS last_span_arrival_time,
      MIN(t.trace_start_time) AS earliest_trace_time,
      MAX(t.page_count) AS page_count,
      MIN(t.min_model_confidence) AS min_model_confidence,
      MAX(CASE WHEN t.conf_rn = 1 AND t.final_model_confidence IS NOT NULL THEN t.final_model_confidence END) AS final_model_confidence,
      MAX(CASE WHEN t.escalated IS TRUE THEN 1 ELSE 0 END) = 1 AS trace_escalated,
      MAX(CASE WHEN t.trace_start_time < from_time OR t.trace_start_time >= to_time THEN 1 ELSE 0 END) = 1 AS trace_outside,
      ARRAY_AGG(t.case_id IGNORE NULLS ORDER BY t.trace_start_time ASC, t.trace_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS case_id,
      ARRAY_AGG(t.eval_run_id IGNORE NULLS ORDER BY t.trace_start_time ASC, t.trace_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS eval_run_id,
      ARRAY_AGG(t.business_unit IGNORE NULLS ORDER BY t.trace_start_time ASC, t.trace_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS earliest_trace_bu,
      ARRAY_AGG(t.tenant_id IGNORE NULLS ORDER BY t.trace_start_time ASC, t.trace_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS earliest_trace_tenant_id
    FROM (
      SELECT
        tb.*,
        ROW_NUMBER() OVER (
          PARTITION BY tb.outcome_id
          ORDER BY
            CASE WHEN tb.final_model_confidence IS NOT NULL THEN 0 ELSE 1 END,
            tb.final_confidence_end_time DESC,
            tb.final_confidence_span_id DESC
        ) AS conf_rn
      FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.traces_base`(
        TIMESTAMP_SUB(from_time, INTERVAL ${OUTCOME_LOOKBACK_DAYS} DAY),
        TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      ) AS tb
      WHERE tb.outcome_id IS NOT NULL
    ) AS t
    GROUP BY t.outcome_id
  ),
  event_agg AS (
    -- Single scan of `events_base` over lookback window (combines activity flag + `ADR-047` close winner).
    SELECT
      e.outcome_id,
      MAX(CASE WHEN e.event_time >= from_time AND e.event_time < to_time THEN 1 ELSE 0 END) AS has_active_event,
      MAX(CASE WHEN e.event_type = 'outcome.closed' THEN 1 ELSE 0 END) = 1 AS is_closed,
      MAX(CASE WHEN e.close_rn = 1 AND e.event_type = 'outcome.closed' THEN e.event_time END) AS close_time,
      MAX(CASE WHEN e.close_rn = 1 AND e.event_type = 'outcome.closed' THEN e.outcome_status END) AS outcome_status,
      MAX(CASE WHEN e.close_rn = 1 AND e.event_type = 'outcome.closed' THEN e.trace_id END) AS closing_trace_id,
      MIN(CASE WHEN e.event_type = 'outcome.opened' THEN e.event_time END) AS open_time,
      MIN(e.event_time) AS earliest_event_time,
      MAX(CASE WHEN e.event_type = 'outcome.escalated' THEN 1 ELSE 0 END) = 1 AS event_escalated,
      MAX(CASE WHEN e.event_time < from_time OR e.event_time >= to_time THEN 1 ELSE 0 END) = 1 AS event_outside,
      ARRAY_AGG(JSON_VALUE(e.payload_json, '$.outcome_owner') IGNORE NULLS ORDER BY e.event_time ASC, e.event_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS event_outcome_owner,
      ARRAY_AGG(JSON_VALUE(e.payload_json, '$.outcome_subject') IGNORE NULLS ORDER BY e.event_time ASC, e.event_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS event_outcome_subject,
      ARRAY_AGG(JSON_VALUE(e.payload_json, '$.outcome_parent_id') IGNORE NULLS ORDER BY e.event_time ASC, e.event_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS event_outcome_parent_id,
      ARRAY_AGG(e.tenant_id IGNORE NULLS ORDER BY e.event_time ASC, e.event_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS event_tenant_id
    FROM (
      SELECT
        ev.*,
        ROW_NUMBER() OVER (
          PARTITION BY ev.outcome_id
          ORDER BY
            CASE WHEN ev.event_type = 'outcome.closed' THEN 0 ELSE 1 END,
            CASE ev.source_class
              WHEN 'system_of_record' THEN 0
              WHEN 'platform' THEN 1
              ELSE 2
            END ASC,
            ev.event_time ASC,
            ev.event_id ASC
        ) AS close_rn
      FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.events_base`(
        TIMESTAMP_SUB(from_time, INTERVAL ${OUTCOME_LOOKBACK_DAYS} DAY),
        TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
      ) AS ev
      WHERE ev.outcome_id IS NOT NULL
    ) AS e
    GROUP BY e.outcome_id
  ),
  span_meta AS (
    SELECT
      s.outcome_id,
      ARRAY_AGG(s.outcome_owner IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS span_outcome_owner,
      ARRAY_AGG(s.outcome_subject IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS span_outcome_subject,
      ARRAY_AGG(s.outcome_parent_id IGNORE NULLS ORDER BY s.start_time ASC, s.span_id ASC LIMIT 1)[SAFE_OFFSET(0)] AS span_outcome_parent_id
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.spans`(
      TIMESTAMP_SUB(from_time, INTERVAL ${OUTCOME_LOOKBACK_DAYS} DAY),
      TIMESTAMP_ADD(to_time, INTERVAL ${READ_MARGIN_MINUTES} MINUTE)
    ) AS s
    WHERE s.outcome_id IS NOT NULL
    GROUP BY s.outcome_id
  ),
  assembled AS (
    SELECT
      COALESCE(ta.outcome_id, ea.outcome_id) AS outcome_id,
      COALESCE(ea.is_closed, FALSE) AS is_closed,
      ea.open_time,
      ea.close_time,
      ea.outcome_status,
      COALESCE(sm.span_outcome_subject, ea.event_outcome_subject) AS outcome_subject,
      COALESCE(sm.span_outcome_owner, ea.event_outcome_owner) AS outcome_owner,
      COALESCE(sm.span_outcome_parent_id, ea.event_outcome_parent_id) AS outcome_parent_id,
      ta.case_id,
      ta.eval_run_id,
      ta.page_count,
      ta.min_model_confidence,
      ta.final_model_confidence,
      (
        COALESCE(ta.trace_escalated, FALSE)
        OR COALESCE(ea.event_escalated, FALSE)
        OR COALESCE(ea.outcome_status, '') = 'escalated'
      ) AS escalated,
      COALESCE(ta.trace_count, 0) AS trace_count,
      ta.trace_ids,
      COALESCE(ta.root_error_trace_count, 0) AS root_error_trace_count,
      COALESCE(ta.failed_trace_count, 0) AS failed_trace_count,
      COALESCE(ta.rootless_trace_count, 0) AS rootless_trace_count,
      ea.closing_trace_id,
      ta.last_span_arrival_time,
      ta.earliest_trace_bu,
      COALESCE(ta.earliest_trace_tenant_id, ea.event_tenant_id) AS tenant_id,
      COALESCE(ea.close_time, ea.open_time, ta.earliest_trace_time, ea.earliest_event_time) AS as_of_time,
      (COALESCE(ta.trace_outside, FALSE) OR COALESCE(ea.event_outside, FALSE)) AS extends_outside_window
    FROM trace_agg AS ta
    FULL OUTER JOIN event_agg AS ea ON ea.outcome_id = ta.outcome_id
    LEFT JOIN span_meta AS sm ON sm.outcome_id = COALESCE(ta.outcome_id, ea.outcome_id)
    WHERE COALESCE(ta.has_active_trace, 0) = 1
      OR COALESCE(ea.has_active_event, 0) = 1
  ),
  with_bu AS (
    SELECT
      a.*,
      r.business_unit AS owner_business_unit,
      ROW_NUMBER() OVER (PARTITION BY a.outcome_id ORDER BY r.effective_from DESC) AS ob_rn
    FROM assembled AS a
    LEFT JOIN `${PROJECT_ID}.${SILVER_REF_DATASET}.app_registry` AS r
      ON a.outcome_owner IS NOT NULL
      AND r.app_id = a.outcome_owner
      AND r.effective_from <= a.as_of_time
      AND (r.effective_to IS NULL OR a.as_of_time < r.effective_to)
  )
  SELECT
    a.outcome_id,
    a.is_closed,
    a.open_time,
    a.close_time,
    a.outcome_status,
    a.outcome_subject,
    a.outcome_owner,
    a.outcome_parent_id,
    a.case_id,
    a.eval_run_id,
    a.page_count,
    a.min_model_confidence,
    a.final_model_confidence,
    a.escalated,
    a.trace_count,
    a.trace_ids,
    a.root_error_trace_count,
    a.failed_trace_count,
    a.rootless_trace_count,
    a.closing_trace_id,
    a.last_span_arrival_time,
    COALESCE(a.owner_business_unit, a.earliest_trace_bu) AS business_unit,
    a.extends_outside_window,
    CAST(NULL AS JSON) AS labels,
    a.tenant_id
  FROM with_bu AS a
  WHERE a.ob_rn = 1
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_RESTRICTED_DATASET}.outcomes`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  SELECT
    o.outcome_id,
    o.is_closed,
    o.open_time,
    o.close_time,
    o.outcome_status,
    o.outcome_subject,
    o.outcome_owner,
    o.outcome_parent_id,
    o.case_id,
    o.eval_run_id,
    o.page_count,
    o.min_model_confidence,
    o.final_model_confidence,
    o.escalated,
    o.trace_count,
    o.trace_ids,
    o.root_error_trace_count,
    o.failed_trace_count,
    o.rootless_trace_count,
    o.closing_trace_id,
    o.last_span_arrival_time,
    o.business_unit,
    o.extends_outside_window,
    o.labels
  FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.outcomes_base`(from_time, to_time) AS o
  WHERE EXISTS (
    SELECT 1
    FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.unit_entitlements` AS ue
    WHERE LOWER(ue.principal_email) = LOWER(SESSION_USER())
      AND ue.business_unit = COALESCE(o.business_unit, '__unowned__')
      AND (ue.tenant_id = '*' OR ue.tenant_id = o.tenant_id)
  )
);

CREATE OR REPLACE TABLE FUNCTION `${PROJECT_ID}.${SILVER_OPEN_DATASET}.outcomes_open`(
  from_time TIMESTAMP,
  to_time TIMESTAMP
) AS (
  SELECT
    outcome_id,
    is_closed,
    open_time,
    close_time,
    outcome_status,
    outcome_owner,
    outcome_parent_id,
    case_id,
    eval_run_id,
    page_count,
    min_model_confidence,
    final_model_confidence,
    escalated,
    trace_count,
    trace_ids,
    root_error_trace_count,
    failed_trace_count,
    rootless_trace_count,
    closing_trace_id,
    last_span_arrival_time,
    business_unit,
    extends_outside_window,
    labels
  FROM `${PROJECT_ID}.${SILVER_INTERNAL_DATASET}.outcomes_base`(from_time, to_time)
);
