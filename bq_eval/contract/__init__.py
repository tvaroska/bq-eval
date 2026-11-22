"""Frozen Pydantic v2 data schemas, pricing constants, and rubric contracts (`bq_eval.contract`)."""

from bq_eval.contract.otel import (
    CACHED_INPUT_BURNDOWN_WEIGHT as CACHED_INPUT_BURNDOWN_WEIGHT,
)
from bq_eval.contract.otel import (
    LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT as LONG_CONTEXT_CACHED_BURNDOWN_WEIGHT,
)
from bq_eval.contract.otel import (
    LONG_CONTEXT_OUTPUT_BURNDOWN_WEIGHT as LONG_CONTEXT_OUTPUT_BURNDOWN_WEIGHT,
)
from bq_eval.contract.otel import (
    LONG_CONTEXT_THRESHOLD_TOKENS as LONG_CONTEXT_THRESHOLD_TOKENS,
)
from bq_eval.contract.otel import (
    LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT as LONG_CONTEXT_UNCACHED_BURNDOWN_WEIGHT,
)
from bq_eval.contract.otel import (
    OUTPUT_THINKING_BURNDOWN_WEIGHT as OUTPUT_THINKING_BURNDOWN_WEIGHT,
)
from bq_eval.contract.otel import (
    UNCACHED_INPUT_BURNDOWN_WEIGHT as UNCACHED_INPUT_BURNDOWN_WEIGHT,
)
from bq_eval.contract.otel import (
    BQAAAgentEvent as BQAAAgentEvent,
)
from bq_eval.contract.otel import (
    FinOpsTrafficType as FinOpsTrafficType,
)
from bq_eval.contract.otel import (
    GenAISpanKind as GenAISpanKind,
)
from bq_eval.contract.otel import (
    OTelGenAISpanRecord as OTelGenAISpanRecord,
)
from bq_eval.contract.otel import (
    TracePayloadRecord as TracePayloadRecord,
)
from bq_eval.contract.otel import (
    compute_weighted_burndown_tokens as compute_weighted_burndown_tokens,
)
from bq_eval.contract.otel import (
    resolve_application_id as resolve_application_id,
)
from bq_eval.contract.pricing import (
    CATALOG_EFFECTIVE_END as CATALOG_EFFECTIVE_END,
)
from bq_eval.contract.pricing import (
    CATALOG_EFFECTIVE_START as CATALOG_EFFECTIVE_START,
)
from bq_eval.contract.pricing import (
    DEFAULT_MODEL_SKUS as DEFAULT_MODEL_SKUS,
)
from bq_eval.contract.pricing import (
    JUDGE_MODEL_ENDPOINTS as JUDGE_MODEL_ENDPOINTS,
)
from bq_eval.contract.pricing import (
    MANAGED_EVALUATE_AGENT_METRICS as MANAGED_EVALUATE_AGENT_METRICS,
)
from bq_eval.contract.pricing import (
    ModelPricingSKU as ModelPricingSKU,
)
from bq_eval.contract.pricing import (
    normalize_model_name as normalize_model_name,
)
from bq_eval.contract.results import (
    CalibrationResult as CalibrationResult,
)
from bq_eval.contract.results import (
    ConfusionMatrix as ConfusionMatrix,
)
from bq_eval.contract.results import (
    EvalFinOpsROI as EvalFinOpsROI,
)
from bq_eval.contract.results import (
    FocusChargebackRow as FocusChargebackRow,
)
from bq_eval.contract.results import (
    GoldenSpanTemplate as GoldenSpanTemplate,
)
from bq_eval.contract.results import (
    GoldenTraceTemplate as GoldenTraceTemplate,
)
from bq_eval.contract.results import (
    HourlyBurndownExportRow as HourlyBurndownExportRow,
)
from bq_eval.contract.results import (
    LiveAttributionCohortReceipt as LiveAttributionCohortReceipt,
)
from bq_eval.contract.results import (
    LiveFleetReceipt as LiveFleetReceipt,
)
from bq_eval.contract.results import (
    LiveKycCohortReceipt as LiveKycCohortReceipt,
)
from bq_eval.contract.results import (
    LiveRfpModalityReceipt as LiveRfpModalityReceipt,
)
from bq_eval.contract.results import (
    LiveSkillLineageReceipt as LiveSkillLineageReceipt,
)
from bq_eval.contract.results import (
    ModelOption as ModelOption,
)
from bq_eval.contract.results import (
    OffPeakSchedulePlan as OffPeakSchedulePlan,
)
from bq_eval.contract.results import (
    ParetoEvalResult as ParetoEvalResult,
)
from bq_eval.contract.results import (
    PrefixCacheDilutionReport as PrefixCacheDilutionReport,
)
from bq_eval.contract.results import (
    RoganGladenResult as RoganGladenResult,
)
from bq_eval.contract.results import (
    ScenarioCorpusBundle as ScenarioCorpusBundle,
)
from bq_eval.contract.results import (
    ShowcaseRunArtifacts as ShowcaseRunArtifacts,
)
from bq_eval.contract.results import (
    SQLTraceEvalSummary as SQLTraceEvalSummary,
)
from bq_eval.contract.results import (
    Step1CostOutcomeRow as Step1CostOutcomeRow,
)
from bq_eval.contract.results import (
    TimeShiftLoadgenReport as TimeShiftLoadgenReport,
)
from bq_eval.contract.rubrics import (
    CANONICAL_RUBRICS as CANONICAL_RUBRICS,
)
from bq_eval.contract.rubrics import (
    OrdinalScaleLevel as OrdinalScaleLevel,
)
from bq_eval.contract.rubrics import (
    OrdinalScoreOutput as OrdinalScoreOutput,
)
from bq_eval.contract.rubrics import (
    RubricSpec as RubricSpec,
)
from bq_eval.contract.rubrics import (
    build_autorater_system_prompt as build_autorater_system_prompt,
)
from bq_eval.contract.rubrics import (
    wrap_untrusted_text as wrap_untrusted_text,
)
