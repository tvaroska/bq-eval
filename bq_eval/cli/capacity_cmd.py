"""CLI handler for `bq-eval capacity-export` (versioned 3-pool FOCUS, governance & lifecycle export)."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, timedelta
from pathlib import Path
from typing import cast

from bq_eval.demo.contract.results import ScenarioCorpusBundle
from bq_eval.demo.pipeline import (
    CorpusChoice,
    build_golden_4app_bundle,
    build_retail_2app_bundle,
    load_m2_live_bundle,
    load_scenario_bundle,
)
from bq_eval.finops.engine.capacity_export import hourly_burndown_rows
from bq_eval.finops.engine.chargeback_three_pool import (
    TrafficLane,
    allocate_focus_three_pool_hourly_chargeback,
    build_capacity_residual_schedule_request,
    contiguous_utc_hours,
    parse_utc_hour,
    traffic_lane_for_finops_type,
)
from bq_eval.runner.contract.spec import RunnerConfig
from bq_eval.runner.engine.eval_adapter import (
    OfflineEvalBackend,
    open_silver_gold_runner,
    populate_runner_from_spans,
)


def register_capacity_export_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Attach the `capacity-export` subcommand to the root `bq-eval` CLI parser."""
    cap_p = sub.add_parser(
        "capacity-export",
        help="Emit versioned 3-pool FOCUS 1.2 chargeback, governance, lifecycle, and schedule JSON.",
    )
    cap_p.add_argument(
        "--corpus",
        choices=("m2-live", "golden-4app", "retail-2app"),
        default="m2-live",
        help="Built-in scenario corpus to export (default: m2-live).",
    )
    cap_p.add_argument(
        "--scenario-file",
        type=Path,
        default=None,
        help="Optional path to a custom ScenarioCorpusBundle JSON file.",
    )
    cap_p.add_argument(
        "--model",
        default=None,
        help="Model name for the residual schedule payload (defaults to highest-burndown model).",
    )
    cap_p.add_argument(
        "--provisioned-gsus",
        type=int,
        default=4,
        help="Provisioned GSU count for the residual schedule request (default: 4).",
    )
    cap_p.add_argument(
        "--reserved-fixed-cost-usd",
        type=float,
        default=None,
        help="Optional fixed reserved PT cost override in USD (defaults to corpus setting).",
    )
    cap_p.add_argument(
        "--safety-headroom",
        type=float,
        default=0.85,
        help="Maximum utilization ratio for off-peak scavenging (0.1 < r <= 1.0, default: 0.85).",
    )
    cap_p.add_argument(
        "--max-hours",
        type=int,
        default=168,
        help="Maximum contiguous UTC hours permitted in one export window (default: 168).",
    )


def _load_corpus_bundle(corpus: CorpusChoice, scenario_file: Path | None) -> ScenarioCorpusBundle:
    if scenario_file is not None:
        return load_scenario_bundle(scenario_file)
    if corpus == "retail-2app":
        return build_retail_2app_bundle()
    if corpus == "golden-4app":
        return build_golden_4app_bundle()
    if corpus == "m2-live":
        return load_m2_live_bundle()
    raise ValueError(
        f"Unsupported corpus '{corpus}'. Expected m2-live, golden-4app, or retail-2app."
    )


def _query_governance_and_lifecycle(
    bundle: ScenarioCorpusBundle,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    max_end = max(s.end_time.astimezone(UTC) for s in bundle.spans)
    now_dt = max_end + timedelta(minutes=10)
    cfg = RunnerConfig(
        current_date_utc=now_dt.strftime("%Y-%m-%d"),
        current_timestamp_utc=now_dt.isoformat(),
        default_dataset="agent_eval",
    )
    with open_silver_gold_runner(cfg) as runner:
        populate_runner_from_spans(runner, bundle.spans)
        burst = [
            dict(r)
            for r in runner.query(
                "SELECT * FROM `silver_open.burst_metrics_1m` ORDER BY minute_utc, runtime_pool_id"
            ).rows
        ]
        gov = [
            dict(r)
            for r in runner.query(
                "SELECT * FROM `agent_eval.v_app_risk_governance` ORDER BY apphub_application_id"
            ).rows
        ]
        lob = [
            dict(r)
            for r in runner.query(
                "SELECT * FROM `agent_eval.v_lob_model_lifecycle_export` "
                "ORDER BY business_unit, apphub_application_id, model_name"
            ).rows
        ]
    return burst, gov, lob


def run_capacity_export_command(args: argparse.Namespace) -> int:
    """Run `bq-eval capacity-export` and print deterministic schema v1.1 JSON."""
    bundle = _load_corpus_bundle(cast(CorpusChoice, args.corpus), args.scenario_file)
    hourly_rows = hourly_burndown_rows(OfflineEvalBackend(), bundle.spans)
    if not hourly_rows:
        raise ValueError("Selected corpus produced no hourly burndown rows.")

    grid = contiguous_utc_hours(
        [r.timestamp or r.usage_hour for r in hourly_rows],
        max_hours=int(args.max_hours),
    )
    hour_index = {h: idx for idx, h in enumerate(grid)}

    demand_by_app_lane: dict[tuple[str, TrafficLane], list[float]] = {}
    model_totals: dict[str, float] = {}
    registered_apps: set[str] = set()
    for row in hourly_rows:
        h_dt = parse_utc_hour(row.timestamp or row.usage_hour)
        idx = hour_index.get(h_dt)
        if idx is None:
            continue
        lane = traffic_lane_for_finops_type(row.finops_traffic_type)
        key = (row.apphub_application_id, lane)
        series = demand_by_app_lane.setdefault(key, [0.0] * len(grid))
        series[idx] += row.weighted_burndown_tokens
        if row.model_name and row.model_name != "none":
            model_totals[row.model_name] = (
                model_totals.get(row.model_name, 0.0) + row.weighted_burndown_tokens
            )
        if row.apphub_application_id != "unregistered_app":
            registered_apps.add(row.apphub_application_id)

    selected_model = args.model
    if selected_model is None:
        if not model_totals:
            raise ValueError("No priced model spans found for capacity export.")
        selected_model = max(sorted(model_totals), key=lambda m: model_totals[m])

    per_app_gsus = float(args.provisioned_gsus) / len(registered_apps) if registered_apps else 0.0
    funded_gsus = dict.fromkeys(sorted(registered_apps), per_app_gsus)
    fixed_cost = (
        float(args.reserved_fixed_cost_usd)
        if args.reserved_fixed_cost_usd is not None
        else bundle.reserved_pt_fixed_cost_usd
    )
    chargeback = allocate_focus_three_pool_hourly_chargeback(
        hourly_demand_by_app_lane=demand_by_app_lane,
        app_cost_centers=bundle.app_cost_centers,
        funded_gsus_by_app=funded_gsus,
        hourly_reserved_pt_capacity_tokens=bundle.reserved_pt_capacity_tokens / len(grid),
        reserved_pt_fixed_cost_usd=fixed_cost,
    )
    schedule_req = build_capacity_residual_schedule_request(
        hourly_rows,
        model=selected_model,
        provisioned_gsus=args.provisioned_gsus,
        safety_headroom_ratio=args.safety_headroom,
        max_hours=int(args.max_hours),
    )
    burst_rows, gov_rows, lob_rows = _query_governance_and_lifecycle(bundle)
    payload = {
        "schema_version": "1.1",
        "corpus_id": bundle.corpus_id,
        "model": selected_model,
        "hours": [h.strftime("%Y-%m-%dT%H:%M:%SZ") for h in grid],
        "three_pool_chargeback": chargeback.model_dump(mode="json"),
        "residual_schedule_request": schedule_req.model_dump(mode="json"),
        "burst_metrics_1m": burst_rows,
        "app_risk_governance": gov_rows,
        "lob_model_lifecycle": lob_rows,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
