"""Gold-on-Silver SQL boundary, attribution gate (`S-16`), WORM, and mutant-killing guard tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from bq_eval.core.contract.otel import FinOpsTrafficType, GenAISpanKind, OTelGenAISpanRecord
from bq_eval.core.contract.results import ConfusionMatrix
from tests.conftest import make_span, open_runner_for_spans, seed_sme_confusion


def test_attribution_coverage_unpriced_unattributed_fails_gate() -> None:
    """`v_attribution_coverage` fails `passes_coverage_gate` on unpriced, unattributed, or unregistered apps (`S-16`)."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans = [
        make_span(
            "tr-cov-clean",
            "sp-cov-clean",
            app_id="app-clean",
            outcome_id="out-clean-1",
            outcome_owner="app-clean",
            input_tokens=50_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            base_time=base,
        ),
        make_span(
            "tr-cov-unpriced",
            "sp-cov-unpriced",
            app_id="app-unpriced",
            model_name="mystery-unpriced-sku",
            outcome_id="out-unpriced-1",
            outcome_owner="app-unpriced",
            input_tokens=50_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            offset_s=1,
            base_time=base,
        ),
        make_span(
            "tr-cov-half-1",
            "sp-cov-half-1",
            app_id="app-low-cov",
            outcome_id="out-half-1",
            outcome_owner="app-low-cov",
            input_tokens=50_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            offset_s=2,
            base_time=base,
        ),
        make_span(
            "tr-cov-half-2",
            "sp-cov-half-2",
            app_id="app-low-cov",
            outcome_id=None,
            input_tokens=50_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            offset_s=3,
            base_time=base,
        ),
        make_span(
            "tr-cov-unreg",
            "sp-cov-unreg",
            app_id=None,
            outcome_id="out-unreg-1",
            input_tokens=50_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            offset_s=4,
            base_time=base,
        ),
    ]
    with open_runner_for_spans(spans) as runner:
        by_app = {
            str(r["apphub_application_id"]): dict(r)
            for r in runner.query("SELECT * FROM `agent_eval.v_attribution_coverage`").rows
        }

    assert int(str(by_app["app-clean"]["unpriced_spans"])) == 0
    assert float(str(by_app["app-clean"]["attribution_coverage_pct"])) == pytest.approx(100.0)
    assert bool(by_app["app-clean"]["passes_coverage_gate"]) is True

    assert int(str(by_app["app-unpriced"]["unpriced_spans"])) == 1
    assert by_app["app-unpriced"]["total_spend_usd"] is None
    assert bool(by_app["app-unpriced"]["passes_coverage_gate"]) is False

    assert float(str(by_app["app-low-cov"]["attribution_coverage_pct"])) == pytest.approx(50.0)
    assert bool(by_app["app-low-cov"]["passes_coverage_gate"]) is False

    assert bool(by_app["unregistered_app"]["passes_coverage_gate"]) is False


def _assert_unregistered_and_cliff_and_tool_loop_sql_guards() -> None:
    """Guard `'unregistered_app'` fallback, exact `200K` (`input + cache_read`) cliff, `min_cache_tokens`, and `8`-vs-`9` tools."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = [
        make_span(
            "tr-unreg-1",
            "sp-unreg-1",
            app_id=None,
            service_name="self-reported-svc",
            otel_span_kind="SPAN_KIND_SERVER",
            input_tokens=200_000,
            cached_input_tokens=0,
            output_tokens=10_000,
            thinking_tokens=0,
            base_time=base,
        ),
        make_span(
            "tr-cliff-200k-cached",
            "sp-cliff-200k-cached",
            app_id="cliff-app",
            input_tokens=200_000,
            cached_input_tokens=50_000,
            output_tokens=10_000,
            thinking_tokens=0,
            offset_s=1,
            base_time=base,
        ),
        make_span(
            "tr-cliff-200001",
            "sp-cliff-200001",
            app_id="cliff-app",
            input_tokens=200_001,
            cached_input_tokens=50_000,
            output_tokens=10_000,
            thinking_tokens=0,
            offset_s=2,
            base_time=base,
        ),
        make_span(
            "tr-sub-cache-floor",
            "sp-sub-cache-floor",
            app_id="cliff-app",
            input_tokens=512,
            cached_input_tokens=0,
            output_tokens=100,
            thinking_tokens=0,
            offset_s=3,
            base_time=base,
        ),
    ]
    for count_tag, n_tools in (("tr-tools-8", 8), ("tr-tools-9", 9)):
        root_id = f"sp-{count_tag}-root"
        spans.append(
            make_span(
                count_tag,
                root_id,
                app_id="tool-app",
                span_kind=GenAISpanKind.INVOKE_AGENT,
                input_tokens=2_000,
                cached_input_tokens=0,
                output_tokens=100,
                thinking_tokens=0,
                offset_s=10,
                base_time=base,
            )
        )
        for i in range(n_tools):
            spans.append(
                make_span(
                    count_tag,
                    f"sp-{count_tag}-{i}",
                    parent_span_id=root_id,
                    span_name="execute_tool",
                    app_id="tool-app",
                    span_kind=GenAISpanKind.EXECUTE_TOOL,
                    model_name="none",
                    tool_name="lookup",
                    tool_call_status="SUCCESS",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    thinking_tokens=0,
                    latency_ms=10.0,
                    offset_s=10,
                    offset_ms=10 + i,
                    attributes={"gen_ai.tool.call.arguments": f'{{"idx": {i}}}'},
                    base_time=base,
                )
            )

    with open_runner_for_spans(spans) as runner:
        wf_200k = runner.query(
            "SELECT apphub_application_id, is_long_context_cliff, meets_min_cache_floor, span_cost_usd "
            "FROM `agent_eval.v_trace_waterfall` WHERE trace_id = 'tr-unreg-1'"
        ).rows[0]
        wf_200k_cached = runner.query(
            "SELECT is_long_context_cliff FROM `agent_eval.v_trace_waterfall` "
            "WHERE trace_id = 'tr-cliff-200k-cached'"
        ).rows[0]
        wf_200001 = runner.query(
            "SELECT is_long_context_cliff FROM `agent_eval.v_trace_waterfall` "
            "WHERE trace_id = 'tr-cliff-200001'"
        ).rows[0]
        wf_sub_floor = runner.query(
            "SELECT meets_min_cache_floor, min_cache_tokens FROM `agent_eval.v_trace_waterfall` "
            "WHERE trace_id = 'tr-sub-cache-floor'"
        ).rows[0]
        eval_unreg = runner.query(
            "SELECT apphub_application_id, effective_risk_tier "
            "FROM `agent_eval.v_trace_sql_evaluation` WHERE trace_id = 'tr-unreg-1'"
        ).rows[0]

        assert wf_200k["apphub_application_id"] == "unregistered_app"
        assert eval_unreg["apphub_application_id"] == "unregistered_app"
        assert eval_unreg["effective_risk_tier"] == "RL-1"
        assert int(str(wf_200k["is_long_context_cliff"])) == 0
        assert int(str(wf_200k["meets_min_cache_floor"])) == 1
        assert float(str(wf_200k["span_cost_usd"])) == pytest.approx(0.036, abs=1e-12)
        assert int(str(wf_200k_cached["is_long_context_cliff"])) == 0
        assert int(str(wf_200001["is_long_context_cliff"])) == 1
        assert int(str(wf_sub_floor["min_cache_tokens"])) == 1024
        assert int(str(wf_sub_floor["meets_min_cache_floor"])) == 0

        s1_t8 = runner.query(
            "SELECT step1_pass FROM `agent_eval.v_step1_trace_eval` WHERE trace_id = 'tr-tools-8'"
        ).rows[0]
        s1_t9 = runner.query(
            "SELECT step1_pass FROM `agent_eval.v_step1_trace_eval` WHERE trace_id = 'tr-tools-9'"
        ).rows[0]
        ent_t8 = runner.query(
            "SELECT rule0_pass FROM `agent_eval.v_trace_sql_evaluation` WHERE trace_id = 'tr-tools-8'"
        ).rows[0]
        ent_t9 = runner.query(
            "SELECT rule0_pass FROM `agent_eval.v_trace_sql_evaluation` WHERE trace_id = 'tr-tools-9'"
        ).rows[0]
        assert int(str(s1_t8["step1_pass"])) == 1 and int(str(ent_t8["rule0_pass"])) == 1
        assert int(str(s1_t9["step1_pass"])) == 0 and int(str(ent_t9["rule0_pass"])) == 0

        worm_row = runner.query(
            "SELECT silver_sql_version, snapshot_sha256, policy_verdicts_json "
            "FROM `agent_eval.v_gold_worm_snapshot_candidates` WHERE trace_id = 'tr-tools-8'"
        ).rows[0]
        assert worm_row["silver_sql_version"] == "v0.7.0"
        assert len(str(worm_row["snapshot_sha256"])) == 64
        worm_verdicts = json.loads(str(worm_row["policy_verdicts_json"]))
        assert worm_verdicts["rule0_pass"] is True
        assert worm_verdicts["identity_gate_pass"] is True
        assert worm_verdicts["skill_gate_pass"] is True
        worm_t9 = runner.query(
            "SELECT policy_verdicts_json FROM `agent_eval.v_gold_worm_snapshot_candidates` "
            "WHERE trace_id = 'tr-tools-9'"
        ).rows[0]
        assert json.loads(str(worm_t9["policy_verdicts_json"]))["rule0_pass"] is False
        opt_row = runner.query(
            "SELECT mounted_skills FROM `agent_eval.v_optimizer_gold_export` "
            "WHERE apphub_application_id = 'tool-app'"
        ).rows[0]
        assert opt_row["mounted_skills"] is None
        assert runner.query("SELECT TO_JSON_STRING(CAST(NULL AS JSON)) AS s").rows[0]["s"] == "null"

        runner.load_rows(
            "silver_internal.workflow_events_sor",
            [
                {
                    "event_id": "ev-orphan-1",
                    "event_time": "2026-11-21T12:00:15+00:00",
                    "event_type": "action.committed",
                    "source_system": "core_ledger",
                    "trace_id": "tr-tools-8",
                    "span_id": None,
                    "response_id": None,
                    "outcome_id": None,
                    "conversation_id": None,
                    "action_id": "act-missing-tool-99",
                    "nudge_id": None,
                    "control_id": None,
                    "control_layer": None,
                    "control_action": None,
                    "action_args": '{"amount":100}',
                    "approved_expires_at": None,
                    "user_id": None,
                    "tenant_id": None,
                    "actor_id": "system",
                    "payload_json": None,
                }
            ],
            append=False,
        )
        orphans = runner.query("SELECT * FROM `agent_eval.v_orphan_committed_actions`").rows
        assert len(orphans) == 1
        assert orphans[0]["orphan_status"] == "MISSING_TOOL_SPAN"
        ent_t8_after_orphan = runner.query(
            "SELECT orphan_commit_count, rule0_pass FROM `agent_eval.v_trace_sql_evaluation` "
            "WHERE trace_id = 'tr-tools-8'"
        ).rows[0]
        assert int(str(ent_t8_after_orphan["orphan_commit_count"])) == 1
        assert int(str(ent_t8_after_orphan["rule0_pass"])) == 0


def _assert_dedup_cpvo_rule0_and_nfr_sql_guards() -> None:
    """Guard eval dedup DESC order, unverified CPVO denominator, Rule-0 filters, NFR gate fail, and lane default."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = []
    for i in range(12):
        r0 = i < 10
        oid = f"out-g-{i:02d}" if i < 2 else None
        spans.append(
            make_span(
                f"trace-g-{i:02d}",
                f"span-g-{i:02d}",
                app_id="guard-app",
                outcome_id=oid,
                outcome_owner="guard-app" if oid else None,
                status_code="OK" if r0 else "ERROR",
                rule0_pass=r0,
                input_tokens=100_000,
                cached_input_tokens=0,
                output_tokens=10_000,
                thinking_tokens=0,
                latency_ms=500.0,
                offset_s=i * 2,
                base_time=base,
            )
        )
        if oid is not None:
            spans.append(
                make_span(
                    f"trace-g-{i:02d}",
                    f"span-g-close-{i:02d}",
                    parent_span_id=f"span-g-{i:02d}",
                    span_name="outcome.closed",
                    app_id="guard-app",
                    outcome_id=oid,
                    outcome_owner="guard-app",
                    model_name="none",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    thinking_tokens=0,
                    latency_ms=0.0,
                    offset_s=i * 2 + 1,
                    attributes={
                        "outcome.status": "succeeded" if i == 0 else "escalated",
                        "outcome.verified": "true" if i == 0 else "false",
                    },
                    base_time=base,
                )
            )

    spans.append(
        make_span(
            "tr-unk-lane",
            "sp-unk-lane",
            app_id="lane-app",
            finops_traffic_type=FinOpsTrafficType.UNCLASSIFIED,
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=500,
            thinking_tokens=0,
            offset_s=30,
            base_time=base,
        )
    )

    with open_runner_for_spans(spans) as runner:
        eval_rows: list[dict[str, object]] = [
            {
                "eval_id": "ev-g-00-old",
                "eval_run_id": "run-1",
                "trace_id": "trace-g-00",
                "outcome_id": "out-g-00",
                "apphub_application_id": "guard-app",
                "rubric_id": "groundedness_4pt_v1",
                "judge_model": "gemini-3.8-flash",
                "sampling_stratum": "CANARY_5PCT",
                "rule0_pass": True,
                "reasoning_trace": "old fail",
                "ordinal_score": 1,
                "binary_pass": False,
                "judge_status": "SCORED",
                "judge_input_tokens": 100_000,
                "judge_output_tokens": 10_000,
                "judge_thinking_tokens": 0,
                "evaluated_at": "2026-11-21T12:01:00+00:00",
                "policy_sha256": "sha-v1",
                "amended_clause_ids": None,
                "target_clause_id": "CL-1",
            },
            {
                "eval_id": "ev-g-00-new",
                "eval_run_id": "run-1",
                "trace_id": "trace-g-00",
                "outcome_id": "out-g-00",
                "apphub_application_id": "guard-app",
                "rubric_id": "groundedness_4pt_v1",
                "judge_model": "gemini-3.8-flash",
                "sampling_stratum": "CANARY_5PCT",
                "rule0_pass": True,
                "reasoning_trace": "new pass",
                "ordinal_score": 4,
                "binary_pass": True,
                "judge_status": "SCORED",
                "judge_input_tokens": 1_000,
                "judge_output_tokens": 100,
                "judge_thinking_tokens": 0,
                "evaluated_at": "2026-11-21T12:05:00+00:00",
                "policy_sha256": "sha-v1",
                "amended_clause_ids": None,
                "target_clause_id": "CL-1",
            },
        ]
        for i in range(1, 10):
            eval_rows.append(
                {
                    "eval_id": f"ev-g-{i:02d}",
                    "eval_run_id": "run-1",
                    "trace_id": f"trace-g-{i:02d}",
                    "outcome_id": f"out-g-{i:02d}" if i < 2 else None,
                    "apphub_application_id": "guard-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "CANARY_5PCT",
                    "rule0_pass": True,
                    "reasoning_trace": "pass",
                    "ordinal_score": 4,
                    "binary_pass": True,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 1_000,
                    "judge_output_tokens": 100,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T12:05:00+00:00",
                    "policy_sha256": "sha-v1",
                    "amended_clause_ids": None,
                    "target_clause_id": "CL-1",
                }
            )
        for i in (10, 11):
            eval_rows.append(
                {
                    "eval_id": f"ev-g-r0f-{i}",
                    "eval_run_id": "run-1",
                    "trace_id": f"trace-g-{i:02d}",
                    "outcome_id": None,
                    "apphub_application_id": "guard-app",
                    "rubric_id": "groundedness_4pt_v1",
                    "judge_model": "gemini-3.8-flash",
                    "sampling_stratum": "ERROR_OR_SLA_TAIL",
                    "rule0_pass": False,
                    "reasoning_trace": "r0 fail",
                    "ordinal_score": 1,
                    "binary_pass": False,
                    "judge_status": "SCORED",
                    "judge_input_tokens": 1_000,
                    "judge_output_tokens": 100,
                    "judge_thinking_tokens": 0,
                    "evaluated_at": "2026-11-21T12:05:00+00:00",
                    "policy_sha256": "sha-v1",
                    "amended_clause_ids": None,
                    "target_clause_id": "CL-1",
                }
            )
        eval_rows.append(
            {
                "eval_id": "ev-g-01-flip",
                "eval_run_id": "run-2",
                "trace_id": "trace-g-01",
                "outcome_id": "out-g-01",
                "apphub_application_id": "guard-app",
                "rubric_id": "groundedness_4pt_v1",
                "judge_model": "gemini-3.8-flash",
                "sampling_stratum": "CANARY_5PCT",
                "rule0_pass": True,
                "reasoning_trace": "regressed",
                "ordinal_score": 1,
                "binary_pass": False,
                "judge_status": "SCORED",
                "judge_input_tokens": 1_000,
                "judge_output_tokens": 100,
                "judge_thinking_tokens": 0,
                "evaluated_at": "2026-11-21T13:00:00+00:00",
                "policy_sha256": "sha-v2",
                "amended_clause_ids": "CL-9",
                "target_clause_id": "CL-1",
            }
        )
        runner.load_rows("agent_eval.eval_results", eval_rows, append=False)
        seed_sme_confusion(runner, "guard-app", ConfusionMatrix(tp=10, fn=0, tn=10, fp=0))
        corrupt_sme = [
            {
                "label_id": f"corrupt-{idx}",
                "trace_id": f"corrupt-tr-{idx}",
                "apphub_application_id": "guard-app",
                "rubric_id": "groundedness_4pt_v1",
                "rater_id": "sme_corrupt",
                "rule0_pass": False,
                "human_ordinal_score": 4 if idx < 25 else 1,
                "human_pass": idx < 25,
                "judge_ordinal_score": 1 if idx < 25 else 4,
                "judge_pass": idx >= 25,
                "labeled_at": "2026-11-21T12:10:00+00:00",
            }
            for idx in range(50)
        ]
        runner.load_rows("agent_eval.sme_labels", corrupt_sme, append=True)

        cpvo = runner.query(
            "SELECT total_outcomes, verified_outcomes, total_ai_cost_usd, "
            "cost_per_verified_outcome_ai_usd "
            "FROM `agent_eval.v_enterprise_cost_per_verified_outcome` "
            "WHERE apphub_application_id = 'guard-app'"
        ).rows[0]
        summ = runner.query(
            "SELECT judged_samples, raw_judge_pass_rate, calibrated_pass_rate, gwet_ac2 "
            "FROM `agent_eval.v_agent_eval_and_finops_summary` "
            "WHERE apphub_application_id = 'guard-app'"
        ).rows[0]
        nfr = runner.query(
            "SELECT unamended_negative_flips, passes_nfr_gate "
            "FROM `agent_eval.v_step3_policy_nfr_gate` "
            "WHERE apphub_application_id = 'guard-app'"
        ).rows[0]
        lane_row = runner.query(
            "SELECT traffic_lane FROM `agent_eval.v_focus_three_pool_hourly_chargeback` "
            "WHERE apphub_application_id = 'lane-app'"
        ).rows[0]

    assert int(str(cpvo["total_outcomes"])) == 2
    assert int(str(cpvo["verified_outcomes"])) == 1
    assert float(str(cpvo["cost_per_verified_outcome_ai_usd"])) == pytest.approx(
        float(str(cpvo["total_ai_cost_usd"])), abs=1e-12
    )
    assert int(str(summ["judged_samples"])) == 10
    assert float(str(summ["raw_judge_pass_rate"])) == pytest.approx(0.90, abs=1e-12)
    assert float(str(summ["calibrated_pass_rate"])) == pytest.approx(0.90, abs=1e-12)
    assert float(str(summ["gwet_ac2"])) == pytest.approx(1.0, abs=1e-12)
    assert int(str(nfr["unamended_negative_flips"])) == 1
    assert int(str(nfr["passes_nfr_gate"])) == 0
    assert lane_row["traffic_lane"] == "prod"


def test_rule0_repeat_loop_and_silent_empty_rag_gates() -> None:
    """`rule0_pass` blocks identical tool-repeat loops (unless overridden) and silent empty-RAG traces."""
    base = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
    spans: list[OTelGenAISpanRecord] = [
        make_span(
            "tr-repeat-loop",
            "sp-rep-root",
            app_id="loop-app",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            input_tokens=1_500,
            cached_input_tokens=0,
            output_tokens=100,
            base_time=base,
        ),
        *[
            make_span(
                "tr-repeat-loop",
                f"sp-rep-{idx}",
                parent_span_id="sp-rep-root",
                span_name="execute_tool",
                app_id="loop-app",
                span_kind=GenAISpanKind.EXECUTE_TOOL,
                model_name="none",
                tool_name="lookup_customer",
                tool_call_status="SUCCESS",
                latency_ms=25.0,
                offset_ms=10 + idx,
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                thinking_tokens=0,
                attributes={"gen_ai.tool.call.arguments": '{"id": "A-1"}'},
                base_time=base,
            )
            for idx in range(3)
        ],
        make_span(
            "tr-empty-rag",
            "sp-rag-root",
            app_id="rag-app",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            input_tokens=1_500,
            cached_input_tokens=0,
            output_tokens=100,
            offset_s=10,
            base_time=base,
        ),
        make_span(
            "tr-empty-rag",
            "sp-rag-ret",
            parent_span_id="sp-rag-root",
            span_name="retrieval",
            app_id="rag-app",
            span_kind=GenAISpanKind.RETRIEVAL,
            model_name="none",
            tool_name="policy_retriever",
            tool_call_status="SUCCESS",
            latency_ms=30.0,
            offset_s=10,
            offset_ms=20,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            attributes={"bq_eval.acl_dropped_count": 4, "gen_ai.tool.call.result": "[]"},
            base_time=base,
        ),
        make_span(
            "tr-nonempty-rag",
            "sp-ok-rag-root",
            app_id="rag-app",
            span_kind=GenAISpanKind.INVOKE_AGENT,
            input_tokens=1_500,
            cached_input_tokens=0,
            output_tokens=100,
            offset_s=20,
            base_time=base,
        ),
        make_span(
            "tr-nonempty-rag",
            "sp-ok-rag-ret",
            parent_span_id="sp-ok-rag-root",
            span_name="retrieval",
            app_id="rag-app",
            span_kind=GenAISpanKind.RETRIEVAL,
            model_name="none",
            tool_name="policy_retriever",
            tool_call_status="SUCCESS",
            latency_ms=30.0,
            offset_s=20,
            offset_ms=20,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            attributes={
                "bq_eval.acl_dropped_count": 2,
                "gen_ai.tool.call.result": '["chunk-visible"]',
            },
            base_time=base,
        ),
    ]

    with open_runner_for_spans(spans) as runner:
        by_trace = {
            str(r["trace_id"]): dict(r)
            for r in runner.query(
                "SELECT trace_id, rule0_pass, repeated_tool_call_count, "
                "silent_empty_rag_count, tail_root_cause, tail_class "
                "FROM `agent_eval.v_trace_sql_evaluation`"
            ).rows
        }
        assert int(str(by_trace["tr-repeat-loop"]["repeated_tool_call_count"])) == 2
        assert int(str(by_trace["tr-repeat-loop"]["rule0_pass"])) == 0
        assert by_trace["tr-repeat-loop"]["tail_root_cause"] == "REPEATED_TOOL_LOOP"
        assert by_trace["tr-repeat-loop"]["tail_class"] == "LOOP_OR_ERROR_TAIL"

        assert int(str(by_trace["tr-empty-rag"]["silent_empty_rag_count"])) == 1
        assert int(str(by_trace["tr-empty-rag"]["rule0_pass"])) == 0
        assert by_trace["tr-empty-rag"]["tail_root_cause"] == "SILENT_EMPTY_RAG"
        assert by_trace["tr-empty-rag"]["tail_class"] == "LOOP_OR_ERROR_TAIL"

        assert int(str(by_trace["tr-nonempty-rag"]["silent_empty_rag_count"])) == 0
        assert int(str(by_trace["tr-nonempty-rag"]["rule0_pass"])) == 1
        assert by_trace["tr-nonempty-rag"]["tail_class"] == "HEALTHY"

        runner.conn.execute(
            "UPDATE agent_eval__workload_thresholds "
            "SET max_repeated_tool_calls = 2 WHERE apphub_application_id = 'loop-app'"
        )
        loop_override = runner.query(
            "SELECT rule0_pass FROM `agent_eval.v_trace_sql_evaluation` "
            "WHERE trace_id = 'tr-repeat-loop'"
        ).rows[0]
        assert int(str(loop_override["rule0_pass"])) == 1
