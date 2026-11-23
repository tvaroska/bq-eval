"""Day-2 Judge Token Quota Guard twin mirroring `sql/core/step3_daily_quota_guard.sql`."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

THINKING_LEVEL_HEADROOM_TOKENS: dict[str, int] = {
    "NONE": 0,
    "LOW": 1024,
    "MEDIUM": 4096,
    "HIGH": 8192,
}

_QUOTA_SCHEMA_DDL = """
CREATE TABLE eval_daily_quota_config (
    quota_name TEXT NOT NULL PRIMARY KEY,
    apphub_application_id TEXT NOT NULL,
    daily_judge_token_cap INTEGER NOT NULL,
    per_app_default_token_cap INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    thinking_budget_tokens INTEGER NOT NULL
);
CREATE TABLE eval_daily_quota_reservations (
    reservation_id TEXT NOT NULL,
    quota_date_utc TEXT NOT NULL,
    apphub_application_id TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE eval_results_burn (
    eval_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    apphub_application_id TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    judge_input_tokens INTEGER NOT NULL,
    judge_output_tokens INTEGER NOT NULL,
    judge_thinking_tokens INTEGER NOT NULL,
    evaluated_date_utc TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class QuotaCandidateTrace:
    """Candidate trace waiting for Day-2 scheduled judge execution."""

    trace_id: str
    apphub_application_id: str
    rubric_id: str
    prompt_text: str
    trace_start_iso: str


@dataclass(frozen=True)
class QuotaReservationResult:
    """Atomic pre-flight quota reservation outcome for a scheduled judge tick."""

    reservation_id: str | None
    selected_trace_ids: tuple[str, ...]
    skipped_trace_ids: tuple[str, ...]
    reserved_tokens_by_app: dict[str, int]
    total_reserved_tokens: int
    global_remaining_after: int


def thinking_level_headroom_tokens(thinking_level: str) -> int:
    """Return pre-flight thinking token headroom for `thinking_level` (`NONE|LOW|MEDIUM|HIGH`)."""
    cleaned = thinking_level.strip().upper()
    if cleaned not in THINKING_LEVEL_HEADROOM_TOKENS:
        raise ValueError(
            f"Unsupported thinking_level {thinking_level!r}: expected one of "
            f"{sorted(THINKING_LEVEL_HEADROOM_TOKENS)}."
        )
    return THINKING_LEVEL_HEADROOM_TOKENS[cleaned]


def estimate_judge_call_tokens(
    prompt_text: str,
    *,
    max_output_tokens: int = 2048,
    thinking_level: str = "LOW",
) -> int:
    """Estimate pre-flight prompt + `max_output_tokens` + `thinking_level` tokens per judge call."""
    if isinstance(max_output_tokens, bool) or max_output_tokens < 0:
        raise ValueError("max_output_tokens must be a non-negative integer.")
    prompt_tokens = len(prompt_text) // 4
    return prompt_tokens + max_output_tokens + thinking_level_headroom_tokens(thinking_level)


class DailyQuotaGuard:
    """SQLite-backed atomic twin of `agent_eval.run_daily_quota_guarded_judge`."""

    def __init__(
        self,
        *,
        daily_judge_token_cap: int = 2_000_000,
        per_app_default_token_cap: int = 500_000,
        per_app_caps: dict[str, int] | None = None,
        max_output_tokens: int = 2048,
        thinking_level: str = "LOW",
    ) -> None:
        if daily_judge_token_cap <= 0 or per_app_default_token_cap <= 0:
            raise ValueError("Daily token caps must be positive integers.")
        thinking_budget = thinking_level_headroom_tokens(thinking_level)
        self._max_output_tokens = max_output_tokens
        self._thinking_level = thinking_level
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_QUOTA_SCHEMA_DDL)
        self._conn.execute(
            "INSERT INTO eval_daily_quota_config VALUES (?, ?, ?, ?, ?, ?)",
            (
                "default_step2_daily_cap",
                "*",
                daily_judge_token_cap,
                per_app_default_token_cap,
                max_output_tokens,
                thinking_budget,
            ),
        )
        for app_id, cap in (per_app_caps or {}).items():
            if cap <= 0:
                raise ValueError(f"Per-app cap for {app_id!r} must be positive.")
            self._conn.execute(
                "INSERT INTO eval_daily_quota_config VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"app_cap_{app_id}",
                    app_id,
                    cap,
                    per_app_default_token_cap,
                    max_output_tokens,
                    thinking_budget,
                ),
            )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record_billed_eval(  # noqa: PLR0913
        self,
        *,
        eval_id: str,
        trace_id: str,
        apphub_application_id: str,
        rubric_id: str,
        judge_input_tokens: int,
        judge_output_tokens: int,
        judge_thinking_tokens: int = 0,
        evaluated_date_utc: str = "2026-11-21",
    ) -> None:
        """Append a billed judge execution row (non-deduped burn accounting retains retries)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO eval_results_burn VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    eval_id,
                    trace_id,
                    apphub_application_id,
                    rubric_id,
                    judge_input_tokens,
                    judge_output_tokens,
                    judge_thinking_tokens,
                    evaluated_date_utc,
                ),
            )
            self._conn.commit()

    def _remaining_unlocked(self, quota_date_utc: str) -> tuple[int, int, dict[str, int]]:
        global_row = self._conn.execute(
            "SELECT daily_judge_token_cap, per_app_default_token_cap "
            "FROM eval_daily_quota_config WHERE apphub_application_id = '*'"
        ).fetchone()
        global_cap = int(global_row["daily_judge_token_cap"])
        default_app_cap = int(global_row["per_app_default_token_cap"])

        used_global = int(
            self._conn.execute(
                "SELECT COALESCE(SUM(judge_input_tokens + judge_output_tokens + judge_thinking_tokens), 0) "
                "FROM eval_results_burn WHERE evaluated_date_utc = ?",
                (quota_date_utc,),
            ).fetchone()[0]
        )
        res_global = int(
            self._conn.execute(
                "SELECT COALESCE(SUM(reserved_tokens), 0) FROM eval_daily_quota_reservations "
                "WHERE quota_date_utc = ? AND status IN ('RESERVED', 'FAILED_BILLED')",
                (quota_date_utc,),
            ).fetchone()[0]
        )
        global_rem = max(0, global_cap - (used_global + res_global))

        app_caps = {
            str(r["apphub_application_id"]): int(r["daily_judge_token_cap"])
            for r in self._conn.execute(
                "SELECT apphub_application_id, daily_judge_token_cap "
                "FROM eval_daily_quota_config WHERE apphub_application_id != '*'"
            ).fetchall()
        }
        used_by_app = {
            str(r[0]): int(r[1])
            for r in self._conn.execute(
                "SELECT apphub_application_id, "
                "COALESCE(SUM(judge_input_tokens + judge_output_tokens + judge_thinking_tokens), 0) "
                "FROM eval_results_burn WHERE evaluated_date_utc = ? GROUP BY apphub_application_id",
                (quota_date_utc,),
            ).fetchall()
        }
        res_by_app = {
            str(r[0]): int(r[1])
            for r in self._conn.execute(
                "SELECT apphub_application_id, COALESCE(SUM(reserved_tokens), 0) "
                "FROM eval_daily_quota_reservations "
                "WHERE quota_date_utc = ? AND status IN ('RESERVED', 'FAILED_BILLED') "
                "GROUP BY apphub_application_id",
                (quota_date_utc,),
            ).fetchall()
        }
        all_apps = set(app_caps) | set(used_by_app) | set(res_by_app)
        app_rem = {
            app: max(
                0,
                app_caps.get(app, default_app_cap)
                - (used_by_app.get(app, 0) + res_by_app.get(app, 0)),
            )
            for app in all_apps
        }
        return global_rem, default_app_cap, app_rem

    def reserve_batch(
        self,
        candidates: Sequence[QuotaCandidateTrace],
        *,
        rubric_id: str,
        quota_date_utc: str = "2026-11-21",
        max_output_tokens: int | None = None,
        thinking_level: str | None = None,
    ) -> QuotaReservationResult:
        """Atomically select and reserve candidates within per-app sub-caps and global budget."""
        eff_max_output = self._max_output_tokens if max_output_tokens is None else max_output_tokens
        eff_thinking = self._thinking_level if thinking_level is None else thinking_level
        with self._lock:
            global_rem, default_app_cap, app_rem = self._remaining_unlocked(quota_date_utc)
            already_scored = {
                str(r[0])
                for r in self._conn.execute(
                    "SELECT DISTINCT trace_id FROM eval_results_burn WHERE rubric_id = ?",
                    (rubric_id,),
                ).fetchall()
            }

            ordered = sorted(
                (c for c in candidates if c.rubric_id == rubric_id),
                key=lambda c: (c.trace_start_iso, c.apphub_application_id, c.trace_id),
            )
            selected: list[str] = []
            skipped: list[str] = []
            reserved_by_app: dict[str, int] = {}
            total_reserved = 0

            for cand in ordered:
                if cand.trace_id in already_scored:
                    skipped.append(cand.trace_id)
                    continue
                est_tokens = estimate_judge_call_tokens(
                    cand.prompt_text,
                    max_output_tokens=eff_max_output,
                    thinking_level=eff_thinking,
                )
                rem_for_app = app_rem.get(cand.apphub_application_id, default_app_cap)
                used_in_batch = reserved_by_app.get(cand.apphub_application_id, 0)
                if (
                    used_in_batch + est_tokens <= rem_for_app
                    and total_reserved + est_tokens <= global_rem
                ):
                    selected.append(cand.trace_id)
                    reserved_by_app[cand.apphub_application_id] = used_in_batch + est_tokens
                    total_reserved += est_tokens
                else:
                    skipped.append(cand.trace_id)

            if not selected:
                return QuotaReservationResult(
                    reservation_id=None,
                    selected_trace_ids=(),
                    skipped_trace_ids=tuple(skipped),
                    reserved_tokens_by_app={},
                    total_reserved_tokens=0,
                    global_remaining_after=global_rem,
                )

            reservation_id = f"res-{uuid.uuid4().hex[:12]}"
            self._conn.executemany(
                "INSERT INTO eval_daily_quota_reservations VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (reservation_id, quota_date_utc, app_id, rubric_id, tokens, "RESERVED")
                    for app_id, tokens in sorted(reserved_by_app.items())
                ],
            )
            self._conn.commit()
            return QuotaReservationResult(
                reservation_id=reservation_id,
                selected_trace_ids=tuple(selected),
                skipped_trace_ids=tuple(skipped),
                reserved_tokens_by_app=reserved_by_app,
                total_reserved_tokens=total_reserved,
                global_remaining_after=max(0, global_rem - total_reserved),
            )

    def finalize_reservation(
        self,
        reservation_id: str,
        *,
        status: str = "COMMITTED",
    ) -> None:
        """Transition a reservation to `COMMITTED`, `FAILED_BILLED`, or `RELEASED`."""
        if status not in {"COMMITTED", "FAILED_BILLED", "RELEASED"}:
            raise ValueError(f"Unsupported reservation status {status!r}.")
        with self._lock:
            self._conn.execute(
                "UPDATE eval_daily_quota_reservations SET status = ? WHERE reservation_id = ?",
                (status, reservation_id),
            )
            self._conn.commit()
