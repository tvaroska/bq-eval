"""Blind dual-human SME calibration kit (`export` blind sheets/HTML + `import` validated receipts).

Guarantees zero leakage of LLM judge scores or reasoning into human rater sheets (`M17-1`),
produces independently shuffled deterministic CSV + offline HTML annotation workbenches for
two human raters, and validates completed sheets via `validate_blind_dual_human_labels()`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bq_eval.core.contract.rubrics import CANONICAL_RUBRICS, build_autorater_system_prompt
from bq_eval.core.engine.calibration import rogan_gladen_calibrate
from scripts.empirical_proof import (
    _IDENT_RE,
    BlindHumanLabelRow,
    validate_blind_dual_human_labels,
)

BLIND_CSV_COLUMNS: tuple[str, ...] = (
    "case_index",
    "trace_id",
    "apphub_application_id",
    "rubric_id",
    "passing_score",
    "user_prompt",
    "retrieved_context",
    "final_response",
    "human_ordinal_score",
    "rater_notes",
)
_TRAP_CASE_SLUG_RE = re.compile(
    r"\bkyc-(?:s2trap|clean|pobox|ocr|struct)[a-z0-9-]*\b",
    re.IGNORECASE,
)


def _scrub_blind_text(text: str) -> str:
    """Mask synthetic scenario/trap slugs so human raters stay blind to seeded labels."""
    return _TRAP_CASE_SLUG_RE.sub("kyc-case-redacted", text)


def _deterministic_order(items: Sequence[dict[str, Any]], seed_tag: str) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda c: hashlib.sha256(f"{seed_tag}:{c['trace_id']}".encode()).hexdigest(),
    )


def _render_blind_html_workbench(
    *,
    rater_slot: str,
    rubric_id: str,
    rubric_title: str,
    rubric_prompt: str,
    cases: Sequence[dict[str, Any]],
) -> str:
    safe_cases = [
        {
            "case_index": idx,
            "trace_id": str(c["trace_id"]),
            "apphub_application_id": str(c["apphub_application_id"]),
            "rubric_id": rubric_id,
            "passing_score": int(c["passing_score"]),
            "user_prompt": str(c["user_prompt"]),
            "retrieved_context": str(c["retrieved_context"]),
            "final_response": str(c["final_response"]),
        }
        for idx, c in enumerate(cases, start=1)
    ]
    cases_json = json.dumps(safe_cases, ensure_ascii=False).replace("</", "<\\/")
    esc_slot = html.escape(rater_slot)
    esc_title = html.escape(rubric_title)
    esc_prompt = html.escape(rubric_prompt)
    esc_rubric = html.escape(rubric_id)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%231a73e8'/%3E%3Cpath d='M9 17l5 5 9-11' fill='none' stroke='white' stroke-width='3.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E" />
  <title>Blind SME Calibration Workbench — {esc_slot}</title>
  <style>
    :root {{
      --surface: #f8fafc;
      --card: #ffffff;
      --ink: #0f172a;
      --muted: #475569;
      --border: #cbd5e1;
      --primary: #1a73e8;
      --ok: #137333;
    }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--surface); color: var(--ink); margin: 0; padding: 24px; line-height: 1.45; }}
    .wrap {{ max-width: 980px; margin: 0 auto; }}
    .bar {{ position: sticky; top: 0; background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; margin-bottom: 18px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; z-index: 10; }}
    .rubric {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin-bottom: 18px; }}
    .case {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin-bottom: 14px; }}
    .meta {{ font-size: 13px; color: var(--muted); font-family: ui-monospace, monospace; margin-bottom: 8px; }}
    pre {{ background: #f1f5f9; border-radius: 6px; padding: 10px; white-space: pre-wrap; word-break: break-word; font-size: 13px; margin: 6px 0 12px; }}
    .scores {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .scores label {{ border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px; cursor: pointer; font-weight: 600; }}
    input[type="text"] {{ flex: 1; min-width: 220px; padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px; }}
    button {{ background: var(--primary); color: #fff; border: none; border-radius: 6px; padding: 9px 14px; font-weight: 600; cursor: pointer; }}
    .done {{ color: var(--ok); font-weight: 700; font-variant-numeric: tabular-nums; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="bar">
      <div>
        <strong>Blind Human SME Panel ({esc_slot})</strong> · Rubric: <code>{esc_rubric}</code>
        · Progress: <span id="prog" class="done">0 / {len(safe_cases)}</span>
      </div>
      <div style="display:flex;gap:8px;align-items:center;">
        <input id="raterId" type="text" placeholder="Your human rater ID (e.g. boris_sme)" />
        <button id="dlBtn" type="button">Download Completed CSV</button>
      </div>
    </div>
    <div class="rubric">
      <strong>{esc_title}</strong>
      <p style="margin:8px 0 0;font-size:14px;color:var(--muted);">{esc_prompt}</p>
    </div>
    <div id="list"></div>
  </div>
  <script>
    const CASES = {cases_json};
    const STORAGE_KEY = {json.dumps(f"bq_eval_blind_sme_{rater_slot}_{rubric_id}")};
    const MEM_STORE = {{}};
    function safeGet(k) {{
      try {{
        const v = window.localStorage.getItem(k);
        if (v !== null) return v;
      }} catch (_err) {{}}
      try {{
        if (window.name && window.name.startsWith("BQ_EVAL_SME:")) {{
          const bag = JSON.parse(window.name.slice(12));
          if (bag && Object.prototype.hasOwnProperty.call(bag, k)) return bag[k];
        }}
      }} catch (_err) {{}}
      return Object.prototype.hasOwnProperty.call(MEM_STORE, k) ? MEM_STORE[k] : null;
    }}
    function safeSet(k, v) {{
      MEM_STORE[k] = v;
      try {{ window.localStorage.setItem(k, v); }} catch (_err) {{}}
      try {{
        let bag = {{}};
        if (window.name && window.name.startsWith("BQ_EVAL_SME:")) {{
          bag = JSON.parse(window.name.slice(12)) || {{}};
        }}
        bag[k] = v;
        window.name = "BQ_EVAL_SME:" + JSON.stringify(bag);
      }} catch (_err) {{}}
    }}
    let state = {{}};
    try {{ state = JSON.parse(safeGet(STORAGE_KEY) || "{{}}") || {{}}; }} catch (_err) {{ state = {{}}; }}
    function save() {{
      safeSet(STORAGE_KEY, JSON.stringify(state));
      const rated = CASES.filter(c => state[c.trace_id] && state[c.trace_id].score).length;
      document.getElementById("prog").textContent = rated + " / " + CASES.length;
    }}
    function escCsv(val) {{
      const s = String(val ?? "");
      return /[",\\n\\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }}
    const listEl = document.getElementById("list");
    CASES.forEach(c => {{
      const card = document.createElement("div");
      card.className = "case";
      const cur = state[c.trace_id] || {{}};
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = "#" + c.case_index + " · app=" + c.apphub_application_id + " · trace=" + c.trace_id;
      card.appendChild(meta);
      [["Prompt", c.user_prompt], ["Retrieved Context", c.retrieved_context], ["Agent Final Response", c.final_response]].forEach(([lbl, txt]) => {{
        const b = document.createElement("strong");
        b.textContent = lbl;
        const p = document.createElement("pre");
        p.textContent = txt;
        card.appendChild(b);
        card.appendChild(p);
      }});
      const row = document.createElement("div");
      row.className = "scores";
      [1, 2, 3, 4].forEach(sc => {{
        const lbl = document.createElement("label");
        const rb = document.createElement("input");
        rb.type = "radio";
        rb.name = "sc_" + c.trace_id;
        rb.value = String(sc);
        if (Number(cur.score) === sc) rb.checked = true;
        rb.addEventListener("change", () => {{
          state[c.trace_id] = Object.assign({{}}, state[c.trace_id] || {{}}, {{ score: sc }});
          save();
        }});
        lbl.appendChild(rb);
        lbl.appendChild(document.createTextNode(" Score " + sc));
        row.appendChild(lbl);
      }});
      const note = document.createElement("input");
      note.type = "text";
      note.placeholder = "Optional note";
      note.value = cur.notes || "";
      note.addEventListener("input", () => {{
        state[c.trace_id] = Object.assign({{}}, state[c.trace_id] || {{}}, {{ notes: note.value }});
        save();
      }});
      row.appendChild(note);
      card.appendChild(row);
      listEl.appendChild(card);
    }});
    if (state._rater_id) document.getElementById("raterId").value = state._rater_id;
    document.getElementById("raterId").addEventListener("input", (e) => {{
      state._rater_id = e.target.value;
      save();
    }});
    document.getElementById("dlBtn").addEventListener("click", () => {{
      const missing = CASES.filter(c => !(state[c.trace_id] && state[c.trace_id].score));
      if (missing.length > 0 && !confirm(missing.length + " cases are still unrated. Download partial CSV anyway?")) return;
      const cols = {json.dumps(list(BLIND_CSV_COLUMNS))};
      const lines = [cols.join(",")];
      CASES.forEach(c => {{
        const st = state[c.trace_id] || {{}};
        lines.push([
          c.case_index, c.trace_id, c.apphub_application_id, c.rubric_id, c.passing_score,
          c.user_prompt, c.retrieved_context, c.final_response, st.score || "", st.notes || ""
        ].map(escCsv).join(","));
      }});
      const blob = new Blob([lines.join("\\n") + "\\n"], {{ type: "text/csv;charset=utf-8" }});
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = {json.dumps(f"{rater_slot}_completed.csv")};
      a.click();
    }});
    save();
  </script>
</body>
</html>
"""


def _load_eligible_sme_cases(
    traces_jsonl: Path,
    *,
    rubric_id: str,
    passing_score: int,
    min_cases: int,
) -> list[dict[str, Any]]:
    raw_rows = [
        json.loads(line)
        for line in traces_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    seen_traces: set[str] = set()
    eligible: list[dict[str, Any]] = []
    for row in raw_rows:
        tid = str(row.get("trace_id", "")).strip()
        j_score = row.get("judge_ordinal_score")
        prompt = str(row.get("user_prompt", "")).strip()
        resp = str(row.get("final_response", "")).strip()
        if not tid or tid in seen_traces or j_score is None or not prompt or not resp:
            continue
        score_int = int(j_score)
        if not (1 <= score_int <= 4):
            raise ValueError(f"Trace {tid!r} has invalid judge_ordinal_score {j_score!r}")
        seen_traces.add(tid)
        eligible.append(
            {
                "trace_id": tid,
                "apphub_application_id": str(
                    row.get("apphub_application_id") or "unregistered_app"
                ),
                "rubric_id": rubric_id,
                "passing_score": passing_score,
                "user_prompt": _scrub_blind_text(prompt),
                "retrieved_context": _scrub_blind_text(
                    str(row.get("retrieved_context", "")).strip()
                ),
                "final_response": _scrub_blind_text(resp),
                "judge_ordinal_score": score_int,
            }
        )
        if len(eligible) >= min_cases:
            break
    if len(eligible) < min_cases:
        raise ValueError(
            f"Need >= {min_cases} judged traces with non-empty payloads; found {len(eligible)}."
        )
    return eligible


def _write_blind_rater_csv(
    csv_path: Path,
    *,
    rubric_id: str,
    passing_score: int,
    ordered: Sequence[dict[str, Any]],
) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=BLIND_CSV_COLUMNS)
        writer.writeheader()
        for idx, item in enumerate(ordered, start=1):
            writer.writerow(
                {
                    "case_index": idx,
                    "trace_id": item["trace_id"],
                    "apphub_application_id": item["apphub_application_id"],
                    "rubric_id": rubric_id,
                    "passing_score": passing_score,
                    "user_prompt": item["user_prompt"],
                    "retrieved_context": item["retrieved_context"],
                    "final_response": item["final_response"],
                    "human_ordinal_score": "",
                    "rater_notes": "",
                }
            )


def export_blind_sme_kit(
    *,
    traces_jsonl: Path,
    out_dir: Path,
    sealed_key_out: Path,
    rubric_id: str = "kyc_policy_compliance_4pt_v1",
    min_cases: int = 100,
) -> dict[str, Any]:
    """Export blind Rater A & Rater B CSV + HTML sheets with zero judge-score leakage."""
    if rubric_id not in CANONICAL_RUBRICS:
        raise ValueError(f"Unknown canonical rubric_id {rubric_id!r}")
    rubric = CANONICAL_RUBRICS[rubric_id]
    rubric_prompt = build_autorater_system_prompt(rubric)

    out_dir.mkdir(parents=True, exist_ok=True)
    sealed_key_out.parent.mkdir(parents=True, exist_ok=True)
    if sealed_key_out.resolve().is_relative_to(out_dir.resolve()):
        raise ValueError("sealed_key_out must sit outside out_dir so raters remain blind.")

    eligible = _load_eligible_sme_cases(
        traces_jsonl,
        rubric_id=rubric_id,
        passing_score=rubric.passing_score,
        min_cases=min_cases,
    )

    for slot in ("rater_a", "rater_b"):
        ordered = _deterministic_order(eligible, f"{rubric_id}:{slot}")
        _write_blind_rater_csv(
            out_dir / f"{slot}_blind_sheet.csv",
            rubric_id=rubric_id,
            passing_score=rubric.passing_score,
            ordered=ordered,
        )
        html_path = out_dir / f"{slot}_blind_workbench.html"
        html_path.write_text(
            _render_blind_html_workbench(
                rater_slot=slot,
                rubric_id=rubric_id,
                rubric_title=rubric.title,
                rubric_prompt=rubric_prompt,
                cases=ordered,
            ),
            encoding="utf-8",
        )

    sealed_payload = {
        "rubric_id": rubric_id,
        "passing_score": rubric.passing_score,
        "case_count": len(eligible),
        "cases": {
            c["trace_id"]: {
                "apphub_application_id": c["apphub_application_id"],
                "judge_ordinal_score": c["judge_ordinal_score"],
            }
            for c in eligible
        },
    }
    sealed_bytes = (json.dumps(sealed_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    sealed_key_out.write_bytes(sealed_bytes)
    manifest = {
        "rubric_id": rubric_id,
        "case_count": len(eligible),
        "out_dir": str(out_dir),
        "sealed_key_sha256": hashlib.sha256(sealed_bytes).hexdigest(),
    }
    (out_dir / "README_RATERS.md").write_text(
        f"# Blind Dual-Human SME Calibration Pack (`{rubric_id}`)\n\n"
        f"- **Cases:** `{len(eligible)}` traces (independently shuffled per rater; zero LLM judge verdicts included).\n"
        f"- **Rater A:** Open `rater_a_blind_workbench.html` in any browser (or edit `rater_a_blind_sheet.csv`), assign `1..4` to every trace, and click **Download Completed CSV**.\n"
        f"- **Rater B:** Open `rater_b_blind_workbench.html` independently without viewing Rater A's scores, assign `1..4` to every trace, and click **Download Completed CSV**.\n",
        encoding="utf-8",
    )
    return manifest


def _read_completed_rater_csv(csv_path: Path, rater_id: str) -> dict[str, int]:
    scores: dict[str, int] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            tid = str(row.get("trace_id", "")).strip()
            raw_sc = str(row.get("human_ordinal_score", "")).strip()
            if not tid or not raw_sc:
                raise ValueError(f"Missing human_ordinal_score for trace {tid!r} in {csv_path}")
            scores[tid] = int(raw_sc)
    if not scores:
        raise ValueError(f"No completed annotations found inside {csv_path} for {rater_id!r}")
    return scores


def _sql_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def import_blind_sme_annotations(  # noqa: PLR0913
    *,
    rater_a_csv: Path,
    rater_b_csv: Path,
    rater_a_id: str,
    rater_b_id: str,
    sealed_key_path: Path,
    receipt_out: Path,
    bq_sql_out: Path | None = None,
    project_id: str = "showcase-project",
    dataset_id: str = "agent_eval",
    min_cases: int = 100,
) -> dict[str, Any]:
    """Validate dual-human blind CSVs against the sealed judge key and emit receipts + SQL."""
    if rater_a_id.strip() == rater_b_id.strip():
        raise ValueError("rater_a_id and rater_b_id must be two distinct human raters.")
    if not _IDENT_RE.fullmatch(project_id.strip()) or not _IDENT_RE.fullmatch(dataset_id.strip()):
        raise ValueError("Invalid project_id or dataset_id.")

    sealed = json.loads(sealed_key_path.read_text(encoding="utf-8"))
    rubric_id = str(sealed["rubric_id"])
    passing_score = int(sealed["passing_score"])
    cases_map: dict[str, dict[str, Any]] = sealed["cases"]

    scores_a = _read_completed_rater_csv(rater_a_csv, rater_a_id)
    scores_b = _read_completed_rater_csv(rater_b_csv, rater_b_id)
    common_traces = sorted(set(cases_map) & set(scores_a) & set(scores_b))

    label_rows: list[BlindHumanLabelRow] = []
    for tid in common_traces:
        j_score = int(cases_map[tid]["judge_ordinal_score"])
        label_rows.append(
            BlindHumanLabelRow(
                trace_id=tid,
                rater_id=rater_a_id.strip(),
                human_ordinal_score=scores_a[tid],
                judge_ordinal_score=j_score,
                blind_to_judge=True,
                passing_score=passing_score,
            )
        )
        label_rows.append(
            BlindHumanLabelRow(
                trace_id=tid,
                rater_id=rater_b_id.strip(),
                human_ordinal_score=scores_b[tid],
                judge_ordinal_score=j_score,
                blind_to_judge=True,
                passing_score=passing_score,
            )
        )

    report = validate_blind_dual_human_labels(
        label_rows,
        min_cases=min_cases,
        min_raters_per_case=2,
        passing_score=passing_score,
    )
    cal = rogan_gladen_calibrate(
        observed_pass_rate=report.judge_pass_rate,
        sample_size=report.trace_count,
        confusion=report.confusion_matrix,
    )
    receipt = {
        "rubric_id": rubric_id,
        "passing_score": passing_score,
        "trace_count": report.trace_count,
        "distinct_raters": report.distinct_raters,
        "human_vs_human_ac2": round(report.human_vs_human_ac2, 6),
        "human_vs_llm_ac2": round(report.human_vs_llm_ac2, 6),
        "human_pass_rate": round(report.human_pass_rate, 6),
        "judge_pass_rate": round(report.judge_pass_rate, 6),
        "calibrated_pass_rate": round(cal.calibrated_pass_rate, 6),
        "calibrated_ci_95_lower": round(cal.ci_95_lower, 6),
        "calibrated_ci_95_upper": round(cal.ci_95_upper, 6),
        "confusion_matrix": report.confusion_matrix.model_dump(mode="json"),
        "raw_report": {k: v for k, v in asdict(report).items() if k != "confusion_matrix"},
    }
    receipt_out.parent.mkdir(parents=True, exist_ok=True)
    receipt_out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if bq_sql_out is not None:
        selects: list[str] = []
        for tid in common_traces:
            app_id = str(cases_map[tid]["apphub_application_id"])
            j_sc = int(cases_map[tid]["judge_ordinal_score"])
            j_pass = "TRUE" if j_sc >= passing_score else "FALSE"
            consensus = sorted((scores_a[tid], scores_b[tid]))[0]
            h_pass = "TRUE" if consensus >= passing_score else "FALSE"
            label_id = f"sme_{rubric_id}_{tid}"
            selects.append(
                f"SELECT {_sql_str(label_id)} AS label_id, {_sql_str(tid)} AS trace_id, "
                f"{_sql_str(app_id)} AS apphub_application_id, {_sql_str(rubric_id)} AS rubric_id, "
                f"TRUE AS rule0_pass, {consensus} AS human_ordinal_score, {h_pass} AS human_pass, "
                f"{j_sc} AS judge_ordinal_score, {j_pass} AS judge_pass, "
                f"{_sql_str(f'{rater_a_id.strip()}+{rater_b_id.strip()}')} AS rater_id"
            )
        union_sql = "\n  UNION ALL\n  ".join(selects)
        merge_sql = (
            f"MERGE INTO `{project_id.strip()}.{dataset_id.strip()}.sme_labels` AS target\n"  # noqa: S608
            f"USING (\n  {union_sql}\n) AS src\n"
            f"ON target.label_id = src.label_id\n"
            f"WHEN NOT MATCHED THEN\n"
            f"  INSERT (label_id, trace_id, apphub_application_id, rubric_id, rule0_pass, "
            f"human_ordinal_score, human_pass, judge_ordinal_score, judge_pass, rater_id, labeled_at)\n"
            f"  VALUES (src.label_id, src.trace_id, src.apphub_application_id, src.rubric_id, "
            f"src.rule0_pass, src.human_ordinal_score, src.human_pass, src.judge_ordinal_score, "
            f"src.judge_pass, src.rater_id, CURRENT_TIMESTAMP());\n"
        )
        bq_sql_out.parent.mkdir(parents=True, exist_ok=True)
        bq_sql_out.write_text(merge_sql, encoding="utf-8")

    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blind dual-human SME calibration kit.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    exp = sub.add_parser("export", help="Export blind Rater A & Rater B CSV + HTML sheets.")
    exp.add_argument("--traces-jsonl", type=Path, required=True)
    exp.add_argument("--out-dir", type=Path, required=True)
    exp.add_argument("--sealed-key-out", type=Path, required=True)
    exp.add_argument("--rubric-id", default="kyc_policy_compliance_4pt_v1")
    exp.add_argument("--min-cases", type=int, default=100)

    imp = sub.add_parser("import", help="Validate completed Rater A & B CSVs against sealed key.")
    imp.add_argument("--rater-a-csv", type=Path, required=True)
    imp.add_argument("--rater-b-csv", type=Path, required=True)
    imp.add_argument("--rater-a-id", required=True)
    imp.add_argument("--rater-b-id", required=True)
    imp.add_argument("--sealed-key", type=Path, required=True)
    imp.add_argument("--receipt-out", type=Path, required=True)
    imp.add_argument("--bq-sql-out", type=Path, default=None)
    imp.add_argument("--project-id", default="showcase-project")
    imp.add_argument("--dataset-id", default="agent_eval")
    imp.add_argument("--min-cases", type=int, default=100)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "export":
            res = export_blind_sme_kit(
                traces_jsonl=args.traces_jsonl,
                out_dir=args.out_dir,
                sealed_key_out=args.sealed_key_out,
                rubric_id=args.rubric_id,
                min_cases=args.min_cases,
            )
        else:
            res = import_blind_sme_annotations(
                rater_a_csv=args.rater_a_csv,
                rater_b_csv=args.rater_b_csv,
                rater_a_id=args.rater_a_id,
                rater_b_id=args.rater_b_id,
                sealed_key_path=args.sealed_key,
                receipt_out=args.receipt_out,
                bq_sql_out=args.bq_sql_out,
                project_id=args.project_id,
                dataset_id=args.dataset_id,
                min_cases=args.min_cases,
            )
    except (OSError, ValueError, KeyError) as exc:
        print(f"blind_sme_kit: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
