"""Blind dual-human SME calibration kit (`export` blind sheets/HTML + `import` validated receipts).

Guarantees zero leakage of LLM judge scores or reasoning into human rater sheets (`M19-3`),
produces independently shuffled deterministic CSV + offline HTML annotation workbenches for
two human raters, enforces sealed-key SHA-256 + complete sheet coverage (`T-11`), persists
agreement receipts even on failing judges (`T-12`), and emits idempotent `MERGE` upserts (`T-16`).
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
from pathlib import Path
from typing import Any

from bq_eval.core.contract.rubrics import CANONICAL_RUBRICS, build_autorater_system_prompt
from bq_eval.core.engine.calibration import (
    AgreementGateVerdict,
    compute_confusion_matrix,
    evaluate_calibration_agreement_gate,
    rogan_gladen_calibrate,
)
from scripts.empirical_proof import (
    BlindHumanLabelRow,
    validate_blind_dual_human_labels,
    validate_human_rater_id,
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
_IDENT_RE = re.compile(r"^[a-zA-Z0-9_-]{2,64}$")
_STRICT_SCORE_STR_RE = re.compile(r"^[1-4]$")
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


def _parse_strict_ordinal_score(raw: object, *, context: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"Boolean score rejected for {context}: {raw!r}")
    if isinstance(raw, int):
        if 1 <= raw <= 4:
            return raw
        raise ValueError(f"Ordinal score out of 1..4 range for {context}: {raw!r}")
    if isinstance(raw, str):
        cleaned = raw.strip()
        if _STRICT_SCORE_STR_RE.fullmatch(cleaned):
            return int(cleaned)
    raise ValueError(f"Invalid non-integer ordinal score for {context}: {raw!r}")


def _parse_strict_bool(raw: object, *, default: bool = True) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    raise ValueError(f"Invalid boolean flag {raw!r}; expected bool or 'true'/'false'.")


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
    :root {{ --surface: #f8fafc; --card: #ffffff; --ink: #0f172a; --muted: #475569; --border: #cbd5e1; --primary: #1a73e8; --ok: #137333; }}
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
        <input id="raterId" type="text" placeholder="Your human rater ID (e.g. alice_sme)" />
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
      try {{ const v = window.localStorage.getItem(k); if (v !== null) return v; }} catch (_e) {{}}
      try {{
        if (window.name && window.name.startsWith("BQ_EVAL_SME:")) {{
          const bag = JSON.parse(window.name.slice(12));
          if (bag && Object.prototype.hasOwnProperty.call(bag, k)) return bag[k];
        }}
      }} catch (_e) {{}}
      return Object.prototype.hasOwnProperty.call(MEM_STORE, k) ? MEM_STORE[k] : null;
    }}
    function safeSet(k, v) {{
      MEM_STORE[k] = v;
      try {{ window.localStorage.setItem(k, v); }} catch (_e) {{}}
      try {{
        const bag = (window.name && window.name.startsWith("BQ_EVAL_SME:")) ? (JSON.parse(window.name.slice(12)) || {{}}) : {{}};
        bag[k] = v;
        window.name = "BQ_EVAL_SME:" + JSON.stringify(bag);
      }} catch (_e) {{}}
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
    max_cases: int | None = None,
) -> list[dict[str, Any]]:
    if min_cases < 1:
        raise ValueError("min_cases must be >= 1.")
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
        if not tid or j_score is None or not prompt or not resp:
            continue
        if tid in seen_traces:
            raise ValueError(f"Duplicate trace_id {tid!r} inside {traces_jsonl}")
        score_int = _parse_strict_ordinal_score(
            j_score, context=f"trace {tid!r} judge_ordinal_score"
        )
        raw_r0 = row.get("rule0_pass", row.get("step1_pass", True))
        r0_bool = _parse_strict_bool(raw_r0, default=True)
        seen_traces.add(tid)
        eligible.append(
            {
                "trace_id": tid,
                "apphub_application_id": str(
                    row.get("apphub_application_id") or "unregistered_app"
                ),
                "rubric_id": rubric_id,
                "passing_score": passing_score,
                "rule0_pass": r0_bool,
                "user_prompt": _scrub_blind_text(prompt),
                "retrieved_context": _scrub_blind_text(
                    str(row.get("retrieved_context", "")).strip()
                ),
                "final_response": _scrub_blind_text(resp),
                "judge_ordinal_score": score_int,
            }
        )
    if len(eligible) < min_cases:
        raise ValueError(
            f"Need >= {min_cases} judged traces with non-empty payloads; found {len(eligible)}."
        )
    sampled = _deterministic_order(eligible, f"sample:{rubric_id}")
    limit = max_cases if max_cases is not None else min_cases
    if limit < min_cases:
        raise ValueError("max_cases must be >= min_cases.")
    return sampled[:limit]


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


def export_blind_sme_kit(  # noqa: PLR0913
    *,
    traces_jsonl: Path,
    out_dir: Path,
    sealed_key_out: Path,
    rubric_id: str = "kyc_policy_compliance_4pt_v1",
    min_cases: int = 100,
    max_cases: int | None = None,
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
        max_cases=max_cases,
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
                "rule0_pass": bool(c["rule0_pass"]),
            }
            for c in eligible
        },
    }
    sealed_bytes = (json.dumps(sealed_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    sealed_key_out.write_bytes(sealed_bytes)
    sealed_sha = hashlib.sha256(sealed_bytes).hexdigest()
    manifest = {
        "rubric_id": rubric_id,
        "case_count": len(eligible),
        "out_dir": str(out_dir),
        "sealed_key_sha256": sealed_sha,
    }
    (out_dir / "export_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
            raw_sc = row.get("human_ordinal_score", "")
            if not tid:
                raise ValueError(f"Empty trace_id row inside {csv_path}")
            if tid in scores:
                raise ValueError(f"Duplicate trace_id {tid!r} inside {csv_path}")
            if raw_sc is None or str(raw_sc).strip() == "":
                raise ValueError(f"Missing human_ordinal_score for trace {tid!r} in {csv_path}")
            scores[tid] = _parse_strict_ordinal_score(
                raw_sc,
                context=f"trace {tid!r} in {csv_path.name}",
            )
    if not scores:
        raise ValueError(f"No completed annotations found inside {csv_path} for {rater_id!r}")
    return scores


def _sql_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _verify_sealed_key_integrity(
    sealed_key_path: Path,
    *,
    expected_sealed_key_sha256: str | None,
    export_manifest_path: Path | None,
) -> dict[str, Any]:
    raw_bytes = sealed_key_path.read_bytes()
    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
    expected_sha = (
        expected_sealed_key_sha256.strip().lower() if expected_sealed_key_sha256 else None
    )
    if export_manifest_path is not None:
        manifest_obj = json.loads(export_manifest_path.read_text(encoding="utf-8"))
        manifest_sha = str(manifest_obj.get("sealed_key_sha256", "")).strip().lower()
        if not manifest_sha:
            raise ValueError(
                f"export_manifest_path {export_manifest_path} missing sealed_key_sha256."
            )
        if expected_sha is not None and expected_sha != manifest_sha:
            raise ValueError("expected_sealed_key_sha256 disagrees with export_manifest_path.")
        expected_sha = manifest_sha
    if expected_sha is not None and actual_sha != expected_sha:
        raise ValueError(
            f"Tampered sealed key {sealed_key_path}: expected SHA-256 {expected_sha}, got {actual_sha}."
        )
    parsed: dict[str, Any] = json.loads(raw_bytes.decode("utf-8"))
    return parsed


def _write_sme_labels_merge_sql(  # noqa: PLR0913
    bq_sql_out: Path,
    *,
    project_id: str,
    dataset_id: str,
    rubric_id: str,
    passing_score: int,
    rater_a_id: str,
    rater_b_id: str,
    cases_map: dict[str, dict[str, Any]],
    scores_a: dict[str, int],
    scores_b: dict[str, int],
    ordered_traces: Sequence[str],
) -> None:
    selects: list[str] = []
    combined_rater = f"{rater_a_id}+{rater_b_id}"
    for tid in ordered_traces:
        meta = cases_map[tid]
        app_id = str(meta["apphub_application_id"])
        j_sc = _parse_strict_ordinal_score(
            meta["judge_ordinal_score"], context=f"sealed {tid!r} judge_ordinal_score"
        )
        r0_sql = (
            "TRUE" if _parse_strict_bool(meta.get("rule0_pass", True), default=True) else "FALSE"
        )
        j_pass = "TRUE" if j_sc >= passing_score else "FALSE"
        sc_a = scores_a[tid]
        sc_b = scores_b[tid]
        consensus = min(sc_a, sc_b)
        h_pass = "TRUE" if consensus >= passing_score else "FALSE"
        label_id = f"sme_{rubric_id}_{tid}"
        selects.append(
            f"SELECT {_sql_str(label_id)} AS label_id, {_sql_str(tid)} AS trace_id, "
            f"{_sql_str(app_id)} AS apphub_application_id, {_sql_str(rubric_id)} AS rubric_id, "
            f"{r0_sql} AS rule0_pass, {consensus} AS human_ordinal_score, {h_pass} AS human_pass, "
            f"{j_sc} AS judge_ordinal_score, {j_pass} AS judge_pass, "
            f"{_sql_str(combined_rater)} AS rater_id, {_sql_str(rater_a_id)} AS rater_a_id, "
            f"{_sql_str(rater_b_id)} AS rater_b_id, {sc_a} AS rater_a_score, {sc_b} AS rater_b_score"
        )
    union_sql = "\n  UNION ALL\n  ".join(selects)
    merge_sql = (
        f"MERGE INTO `{project_id}.{dataset_id}.sme_labels` AS target\n"  # noqa: S608
        f"USING (\n  {union_sql}\n) AS src\n"
        f"ON target.label_id = src.label_id\n"
        f"WHEN MATCHED THEN\n"
        f"  UPDATE SET\n"
        f"    apphub_application_id = src.apphub_application_id,\n"
        f"    rule0_pass = src.rule0_pass,\n"
        f"    human_ordinal_score = src.human_ordinal_score,\n"
        f"    human_pass = src.human_pass,\n"
        f"    judge_ordinal_score = src.judge_ordinal_score,\n"
        f"    judge_pass = src.judge_pass,\n"
        f"    rater_id = src.rater_id,\n"
        f"    rater_a_id = src.rater_a_id,\n"
        f"    rater_b_id = src.rater_b_id,\n"
        f"    rater_a_score = src.rater_a_score,\n"
        f"    rater_b_score = src.rater_b_score,\n"
        f"    labeled_at = CURRENT_TIMESTAMP()\n"
        f"WHEN NOT MATCHED THEN\n"
        f"  INSERT (label_id, trace_id, apphub_application_id, rubric_id, rule0_pass, "
        f"human_ordinal_score, human_pass, judge_ordinal_score, judge_pass, rater_id, "
        f"rater_a_id, rater_b_id, rater_a_score, rater_b_score, labeled_at)\n"
        f"  VALUES (src.label_id, src.trace_id, src.apphub_application_id, src.rubric_id, "
        f"src.rule0_pass, src.human_ordinal_score, src.human_pass, src.judge_ordinal_score, "
        f"src.judge_pass, src.rater_id, src.rater_a_id, src.rater_b_id, src.rater_a_score, "
        f"src.rater_b_score, CURRENT_TIMESTAMP());\n"
    )
    bq_sql_out.parent.mkdir(parents=True, exist_ok=True)
    bq_sql_out.write_text(merge_sql, encoding="utf-8")


def _load_validated_blind_pair(
    rater_a_csv: Path,
    rater_b_csv: Path,
    clean_a: str,
    clean_b: str,
    sealed_ids: set[str],
) -> tuple[dict[str, int], dict[str, int]]:
    if clean_a == clean_b:
        raise ValueError("rater_a_id and rater_b_id must be two distinct human raters.")
    if rater_a_csv.resolve() == rater_b_csv.resolve():
        raise ValueError("rater_a_csv and rater_b_csv must not point to the same file.")
    if rater_a_csv.read_bytes() == rater_b_csv.read_bytes():
        raise ValueError(
            "rater_a_csv and rater_b_csv have identical file contents (same-sheet reuse)."
        )
    scores_a = _read_completed_rater_csv(rater_a_csv, clean_a)
    scores_b = _read_completed_rater_csv(rater_b_csv, clean_b)
    for label, scores in (("Rater A", scores_a), ("Rater B", scores_b)):
        if set(scores) != sealed_ids:
            missing = sorted(sealed_ids - set(scores))[:5]
            extra = sorted(set(scores) - sealed_ids)[:5]
            raise ValueError(
                f"{label} CSV case mismatch vs sealed key (missing={missing}, extra={extra})."
            )
    return scores_a, scores_b


def _collect_gate_failure_reasons(gate: AgreementGateVerdict) -> list[str]:
    reasons: list[str] = []
    if gate.paired_count < gate.required_pairs:
        reasons.append(f"paired_count {gate.paired_count} < {gate.required_pairs}")
    if gate.judge_vs_sme_ac2 < gate.required_ac2:
        reasons.append(f"judge_vs_sme_ac2 {gate.judge_vs_sme_ac2:.4f} < {gate.required_ac2:.2f}")
    if gate.sme_vs_sme_ac2 is not None and gate.sme_vs_sme_ac2 < gate.required_ac2:
        reasons.append(f"sme_vs_sme_ac2 {gate.sme_vs_sme_ac2:.4f} < {gate.required_ac2:.2f}")
    if gate.binary_cutpoint_agreement < 0.85:
        reasons.append(f"binary_cutpoint_agreement {gate.binary_cutpoint_agreement:.4f} < 0.85")
    return reasons


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
    expected_sealed_key_sha256: str | None = None,
    export_manifest_path: Path | None = None,
    population_pass_rate: float | None = None,
    population_sample_size: int | None = None,
    risk_tier: str = "RL-3b",
) -> dict[str, Any]:
    """Validate dual-human blind CSVs against the sealed judge key and emit receipts + SQL."""
    clean_a = validate_human_rater_id(rater_a_id)
    clean_b = validate_human_rater_id(rater_b_id)
    if not _IDENT_RE.fullmatch(project_id.strip()) or not _IDENT_RE.fullmatch(dataset_id.strip()):
        raise ValueError("Invalid project_id or dataset_id.")
    sealed = _verify_sealed_key_integrity(
        sealed_key_path,
        expected_sealed_key_sha256=expected_sealed_key_sha256,
        export_manifest_path=export_manifest_path,
    )
    rubric_id = str(sealed["rubric_id"])
    passing_score = int(sealed["passing_score"])
    cases_map: dict[str, dict[str, Any]] = sealed["cases"]
    ordered_traces = sorted(cases_map)
    scores_a, scores_b = _load_validated_blind_pair(
        rater_a_csv, rater_b_csv, clean_a, clean_b, set(ordered_traces)
    )
    judge_by_tid = {
        tid: _parse_strict_ordinal_score(
            cases_map[tid]["judge_ordinal_score"], context=f"sealed {tid!r} judge_ordinal_score"
        )
        for tid in ordered_traces
    }
    label_rows = [
        BlindHumanLabelRow(
            trace_id=tid,
            rater_id=rater_id,
            human_ordinal_score=scores[tid],
            judge_ordinal_score=judge_by_tid[tid],
            blind_to_judge=True,
            passing_score=passing_score,
        )
        for tid in ordered_traces
        for rater_id, scores in ((clean_a, scores_a), (clean_b, scores_b))
    ]
    report = validate_blind_dual_human_labels(
        label_rows, min_cases=min_cases, min_raters_per_case=2, passing_score=passing_score
    )
    r0_traces = [
        tid
        for tid in ordered_traces
        if _parse_strict_bool(cases_map[tid].get("rule0_pass", True), default=True)
    ]
    if not r0_traces:
        raise ValueError("Zero rule0_pass=True traces in sealed key; cannot calibrate.")
    consensus_scores = [min(scores_a[tid], scores_b[tid]) for tid in r0_traces]
    judge_scores = [judge_by_tid[tid] for tid in r0_traces]
    r0_cm = compute_confusion_matrix(
        [s >= passing_score for s in consensus_scores],
        [s >= passing_score for s in judge_scores],
    )
    gate = evaluate_calibration_agreement_gate(
        consensus_scores,
        judge_scores,
        passing_score=passing_score,
        risk_tier=risk_tier,
        rater_a_ordinal_scores=[scores_a[tid] for tid in r0_traces],
        rater_b_ordinal_scores=[scores_b[tid] for tid in r0_traces],
    )
    failure_reasons = _collect_gate_failure_reasons(gate)
    obs_rate = (
        population_pass_rate
        if population_pass_rate is not None
        else (r0_cm.tp + r0_cm.fp) / len(r0_traces)
    )
    obs_n = population_sample_size if population_sample_size is not None else len(r0_traces)
    cal_rate: float | None = None
    ci_lo: float | None = None
    ci_hi: float | None = None
    if gate.passes_irr_gate:
        try:
            cal = rogan_gladen_calibrate(
                observed_pass_rate=obs_rate,
                sample_size=obs_n,
                confusion=r0_cm,
                risk_tier=risk_tier,
            )
            cal_rate, ci_lo, ci_hi = (
                round(cal.calibrated_pass_rate, 6),
                round(cal.ci_95_lower, 6),
                round(cal.ci_95_upper, 6),
            )
        except ValueError as exc:
            failure_reasons.append(str(exc))

    receipt = {
        "rubric_id": rubric_id,
        "risk_tier": risk_tier,
        "passing_score": passing_score,
        "trace_count": report.trace_count,
        "rule0_passing_trace_count": len(r0_traces),
        "population_sample_size": obs_n,
        "population_pass_rate": round(obs_rate, 6),
        "distinct_raters": report.distinct_raters,
        "human_vs_human_ac2": round(
            gate.sme_vs_sme_ac2 if gate.sme_vs_sme_ac2 is not None else report.human_vs_human_ac2,
            6,
        ),
        "human_vs_llm_ac2": round(gate.judge_vs_sme_ac2, 6),
        "binary_cutpoint_agreement": round(gate.binary_cutpoint_agreement, 6),
        "passes_irr_gate": gate.passes_irr_gate and cal_rate is not None,
        "calibration_status": "CALIBRATED" if cal_rate is not None else "UNCALIBRATED",
        "failure_reasons": failure_reasons,
        "human_pass_rate": round((r0_cm.tp + r0_cm.fn) / len(r0_traces), 6),
        "judge_pass_rate": round((r0_cm.tp + r0_cm.fp) / len(r0_traces), 6),
        "calibrated_pass_rate": cal_rate,
        "calibrated_ci_95_lower": ci_lo,
        "calibrated_ci_95_upper": ci_hi,
        "confusion_matrix": r0_cm.model_dump(mode="json"),
        "raw_report": {
            "trace_count": report.trace_count,
            "distinct_raters": report.distinct_raters,
            "human_vs_human_ac2": report.human_vs_human_ac2,
            "human_vs_llm_ac2": report.human_vs_llm_ac2,
            "binary_cutpoint_agreement": report.binary_cutpoint_agreement,
            "human_pass_rate": report.human_pass_rate,
            "judge_pass_rate": report.judge_pass_rate,
        },
    }
    receipt_out.parent.mkdir(parents=True, exist_ok=True)
    receipt_out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if bq_sql_out is not None:
        _write_sme_labels_merge_sql(
            bq_sql_out,
            project_id=project_id.strip(),
            dataset_id=dataset_id.strip(),
            rubric_id=rubric_id,
            passing_score=passing_score,
            rater_a_id=clean_a,
            rater_b_id=clean_b,
            cases_map=cases_map,
            scores_a=scores_a,
            scores_b=scores_b,
            ordered_traces=ordered_traces,
        )

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
    exp.add_argument("--max-cases", type=int, default=None)

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
    imp.add_argument("--expected-sealed-key-sha256", default=None)
    imp.add_argument("--export-manifest", type=Path, default=None)
    imp.add_argument("--population-pass-rate", type=float, default=None)
    imp.add_argument("--population-sample-size", type=int, default=None)
    imp.add_argument("--risk-tier", default="RL-3b")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "export":
            res = export_blind_sme_kit(
                traces_jsonl=args.traces_jsonl,
                out_dir=args.out_dir,
                sealed_key_out=args.sealed_key_out,
                rubric_id=args.rubric_id,
                min_cases=args.min_cases,
                max_cases=args.max_cases,
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
                expected_sealed_key_sha256=args.expected_sealed_key_sha256,
                export_manifest_path=args.export_manifest,
                population_pass_rate=args.population_pass_rate,
                population_sample_size=args.population_sample_size,
                risk_tier=args.risk_tier,
            )
    except (OSError, ValueError, KeyError) as exc:
        print(f"blind_sme_kit: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
