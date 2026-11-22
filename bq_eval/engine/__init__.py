"""Deterministic calibration, FinOps, BQAA adapter, and SQLite evaluation engine (`bq_eval.engine`)."""

from bq_eval.engine.bqaa import (
    SPAN_EVENT_TYPES as SPAN_EVENT_TYPES,
)
from bq_eval.engine.bqaa import (
    bqaa_events_to_otel_spans as bqaa_events_to_otel_spans,
)
from bq_eval.engine.bqaa import (
    bqaa_events_to_trace_payloads as bqaa_events_to_trace_payloads,
)
from bq_eval.engine.bqaa import (
    deduplicate_bqaa_events as deduplicate_bqaa_events,
)
from bq_eval.engine.bqaa import (
    resolve_bqaa_trace_id as resolve_bqaa_trace_id,
)
from bq_eval.engine.calibration import (
    build_calibration_result as build_calibration_result,
)
from bq_eval.engine.calibration import (
    compute_confusion_matrix as compute_confusion_matrix,
)
from bq_eval.engine.calibration import (
    compute_gwet_ac2 as compute_gwet_ac2,
)
from bq_eval.engine.calibration import (
    rogan_gladen_calibrate as rogan_gladen_calibrate,
)
from bq_eval.engine.calibration import (
    rogan_gladen_wilson_ci as rogan_gladen_wilson_ci,
)
from bq_eval.engine.calibration import (
    stratified_rogan_gladen_calibrate as stratified_rogan_gladen_calibrate,
)
from bq_eval.engine.finops import (
    allocate_focus_hourly_chargeback as allocate_focus_hourly_chargeback,
)
from bq_eval.engine.finops import (
    allocate_focus_two_pool_chargeback as allocate_focus_two_pool_chargeback,
)
from bq_eval.engine.finops import (
    compute_default_span_cost_usd as compute_default_span_cost_usd,
)
from bq_eval.engine.finops import (
    compute_eval_finops_roi as compute_eval_finops_roi,
)
from bq_eval.engine.finops import (
    compute_pareto_frontier as compute_pareto_frontier,
)
from bq_eval.engine.finops import (
    compute_span_cost_usd as compute_span_cost_usd,
)
from bq_eval.engine.finops import (
    diagnose_prefix_cache_dilution as diagnose_prefix_cache_dilution,
)
from bq_eval.engine.finops import (
    plan_off_peak_eval_schedule as plan_off_peak_eval_schedule,
)
from bq_eval.engine.sql_eval import (
    build_ai_evaluate_agent_sql as build_ai_evaluate_agent_sql,
)
from bq_eval.engine.sql_eval import (
    build_ai_generate_table_judge_sql as build_ai_generate_table_judge_sql,
)
from bq_eval.engine.sql_eval import (
    escape_sql_xml_expr as escape_sql_xml_expr,
)
from bq_eval.engine.sql_eval import (
    evaluate_hourly_burndown_in_sqlite as evaluate_hourly_burndown_in_sqlite,
)
from bq_eval.engine.sql_eval import (
    evaluate_traces_in_sqlite as evaluate_traces_in_sqlite,
)
