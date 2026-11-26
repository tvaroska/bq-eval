"""Asynchronous Hive-partitioned Shape-3 upload hook and RFC 8785 tool-args hasher (`M18-10`)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from bq_eval.upload.contract.hook import (
    PayloadProfile,
    ReferenceUploadConfig,
    UploadFormat,
    UploadMetrics,
)

MAX_JCS_INPUT_BYTES = 65_532
MAX_JCS_DEPTH = 64
_SAFE_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class AuditEnqueueError(RuntimeError):
    """Raised when `regulated_worm` enqueue fails closed because the bounded queue is full."""


class StorageWriter(Protocol):
    """Port for writing one Hive-partitioned Shape-3 record object to storage."""

    def write_record(self, ref_uri: str, serialized_line: str) -> None:
        """Persist one serialized BigLake row for `ref_uri`."""


class LocalDirectoryStorageWriter:
    """Adapter writing Hive-partitioned JSONL files under a local root directory."""

    def __init__(self, root_dir: Path, *, base_path: str) -> None:
        self.root_dir = root_dir
        self.base_prefix = base_path.rstrip("/") + "/"
        self.written_paths: list[Path] = []

    def write_record(self, ref_uri: str, serialized_line: str) -> None:
        if not ref_uri.startswith(self.base_prefix):
            raise ValueError(f"ref_uri {ref_uri!r} does not start with {self.base_prefix!r}")
        rel = ref_uri[len(self.base_prefix) :]
        target = (self.root_dir / rel).resolve()
        root_resolved = self.root_dir.resolve()
        if not target.is_relative_to(root_resolved):
            raise ValueError(f"Escaping target path: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as fh:
            fh.write(serialized_line + "\n")
        self.written_paths.append(target)


def _format_es6_number(value: int | float) -> str | None:
    f = float(value)
    if not math.isfinite(f):
        return None
    if f == 0.0:
        return "0"
    sign = "-" if f < 0 else ""
    raw = repr(abs(f))
    if "e" in raw or "E" in raw:
        mantissa_s, exp_s = raw.lower().split("e", 1)
        exp_val = int(exp_s)
        digits = mantissa_s.replace(".", "")
    else:
        int_part, _, frac_part = raw.partition(".")
        if int_part == "0":
            stripped = frac_part.lstrip("0")
            exp_val = -(len(frac_part) - len(stripped) + 1)
            digits = stripped
        else:
            exp_val = len(int_part) - 1
            digits = int_part + frac_part
    digits = digits.rstrip("0") or "0"
    if -6 <= exp_val < 21:
        pos = exp_val + 1
        if pos >= len(digits):
            return sign + digits + ("0" * (pos - len(digits)))
        if pos > 0:
            return sign + digits[:pos] + "." + digits[pos:]
        return sign + "0." + ("0" * (-pos)) + digits
    exp_sign = "+" if exp_val >= 0 else "-"
    mant = digits if len(digits) == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{mant}e{exp_sign}{abs(exp_val)}"


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _jcs_canonicalize_dict(mapping: dict[object, object], depth: int) -> str | None:
    if depth > MAX_JCS_DEPTH:
        return None
    str_items: list[tuple[str, object]] = []
    for k, v in mapping.items():
        ks = str(k)
        if _has_lone_surrogate(ks):
            return None
        str_items.append((ks, v))
    items = sorted(str_items, key=lambda kv: kv[0].encode("utf-16-be"))
    pairs: list[str] = []
    for k, v in items:
        cv = _jcs_canonicalize(v, depth + 1)
        if cv is None:
            return None
        pairs.append(f"{json.dumps(k, ensure_ascii=False)}:{cv}")
    return "{" + ",".join(pairs) + "}"


def _jcs_canonicalize(value: object, depth: int = 0) -> str | None:
    if depth > MAX_JCS_DEPTH:
        return None
    result: str | None = None
    if value is None:
        result = "null"
    elif isinstance(value, bool):
        result = "true" if value else "false"
    elif isinstance(value, (int, float)):
        result = _format_es6_number(value)
    elif isinstance(value, str):
        result = None if _has_lone_surrogate(value) else json.dumps(value, ensure_ascii=False)
    elif isinstance(value, list):
        parts = [_jcs_canonicalize(item, depth + 1) for item in value]
        if all(p is not None for p in parts):
            result = "[" + ",".join(p for p in parts if p is not None) + "]"
    elif isinstance(value, dict):
        result = _jcs_canonicalize_dict(value, depth)
    return result


def compute_execution_args_sha256(tool_name: str, arguments: object) -> str | None:
    """Compute execution-time RFC 8785 JCS SHA-256 of `{\"tool_name\": ..., \"args\": ...}` (`ADR-041`)."""
    try:
        if _has_lone_surrogate(tool_name):
            return None
        parsed_args: object = arguments
        if isinstance(arguments, str):
            if (
                _has_lone_surrogate(arguments)
                or len(arguments.encode("utf-8")) > MAX_JCS_INPUT_BYTES
            ):
                return None
            parsed_args = json.loads(arguments)
        canon = _jcs_canonicalize({"tool_name": tool_name, "args": parsed_args})
        if canon is None:
            return None
        encoded = canon.encode("utf-8")
        if len(encoded) > MAX_JCS_INPUT_BYTES:
            return None
        return hashlib.sha256(encoded).hexdigest()
    except Exception:  # noqa: BLE001
        return None


def hive_partition_uri(
    base_path: str,
    timestamp_utc: datetime,
    record_id: str,
    *,
    upload_format: UploadFormat = "jsonl",
) -> tuple[str, str, str]:
    """Build `(dt, hr, ref_uri)` under Hive `dt=YYYY-MM-DD/hr=HH/` layout (`specs/shapes.md`)."""
    if not _SAFE_RECORD_ID_RE.fullmatch(record_id):
        raise ValueError(f"Unsafe record_id: {record_id!r}")
    ts = timestamp_utc.astimezone(UTC)
    dt = ts.strftime("%Y-%m-%d")
    hr = ts.strftime("%H")
    ext = "parquet" if upload_format == "parquet" else "jsonl"
    prefix = base_path.rstrip("/")
    ref_uri = f"{prefix}/dt={dt}/hr={hr}/{record_id}.{ext}"
    return dt, hr, ref_uri


@dataclass(frozen=True)
class _QueuedItem:
    ref_uri: str
    serialized_line: str


class AsyncReferenceUploadHook:
    """Bounded async Shape-3 upload hook with Hive partitioning, drop counters, and WORM fail-closed."""

    def __init__(
        self,
        config: ReferenceUploadConfig,
        writer: StorageWriter,
        *,
        start_worker: bool = True,
    ) -> None:
        if config.upload_format == "parquet" and not getattr(writer, "supports_parquet", False):
            raise ValueError(
                "upload_format='parquet' requires a Parquet-capable StorageWriter adapter "
                "(default JSONL writers cannot emit binary Parquet files)."
            )
        self.config = config
        self._writer = writer
        self._queue: queue.Queue[_QueuedItem] = queue.Queue(maxsize=config.max_queue_size)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._pending = 0
        self._enqueued = 0
        self._written = 0
        self._dropped_queue_full = 0
        self._write_errors = 0
        self._fail_closed_rejections = 0
        self._closed = False
        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._run_worker,
                name="bq-eval-shape3-uploader",
                daemon=True,
            )
            self._worker.start()

    def _run_worker(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                try:
                    self._writer.write_record(item.ref_uri, item.serialized_line)
                except Exception:  # noqa: BLE001
                    with self._lock:
                        self._write_errors += 1
                else:
                    with self._lock:
                        self._written += 1
            finally:
                with self._cond:
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._cond.notify_all()
                self._queue.task_done()

    def enqueue(
        self,
        record_id: str,
        payload: object,
        *,
        timestamp_utc: datetime | None = None,
        payload_profile: PayloadProfile | None = None,
    ) -> str:
        """Enqueue one payload off the hot path and return its Hive `ref_uri`."""
        ts = timestamp_utc or datetime.now(UTC)
        dt, hr, ref_uri = hive_partition_uri(
            self.config.base_path,
            ts,
            record_id,
            upload_format=self.config.upload_format,
        )
        row_obj = {"ref_uri": ref_uri, "payload": payload}
        serialized = json.dumps(row_obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        effective_profile = payload_profile or self.config.payload_profile
        item = _QueuedItem(ref_uri=ref_uri, serialized_line=serialized)
        with self._lock:
            if self._closed or self._worker is None:
                if effective_profile == "regulated_worm":
                    self._fail_closed_rejections += 1
                    raise AuditEnqueueError(
                        f"Shape-3 regulated_worm uploader unavailable (closed={self._closed}) "
                        f"for dt={dt}/hr={hr} ref={ref_uri}"
                    )
                if self._closed:
                    self._dropped_queue_full += 1
                    return ref_uri
            try:
                self._queue.put_nowait(item)
            except queue.Full as exc:
                if effective_profile == "regulated_worm":
                    self._fail_closed_rejections += 1
                    raise AuditEnqueueError(
                        f"Shape-3 regulated_worm audit queue full ({self.config.max_queue_size}) "
                        f"for dt={dt}/hr={hr} ref={ref_uri}"
                    ) from exc
                self._dropped_queue_full += 1
                return ref_uri
            self._enqueued += 1
            self._pending += 1
        return ref_uri

    def flush(self, *, timeout_sec: float = 5.0) -> bool:
        """Wait for queued writes to drain (returns False if timeout expires)."""
        deadline = time.monotonic() + max(0.0, timeout_sec)
        with self._cond:
            while self._pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._cond.wait(timeout=remaining)
            return True

    def metrics(self) -> UploadMetrics:
        """Return a snapshot of explicit enqueue, write, drop, error, and fail-closed counters."""
        with self._lock:
            return UploadMetrics(
                enqueued=self._enqueued,
                written=self._written,
                dropped_queue_full=self._dropped_queue_full,
                write_errors=self._write_errors,
                fail_closed_rejections=self._fail_closed_rejections,
            )

    def close(self, *, timeout_sec: float = 5.0) -> UploadMetrics:
        """Drain pending items, stop the background worker without deadlocking, and return final metrics."""
        worker: threading.Thread | None
        with self._lock:
            if self._closed:
                return UploadMetrics(
                    enqueued=self._enqueued,
                    written=self._written,
                    dropped_queue_full=self._dropped_queue_full,
                    write_errors=self._write_errors,
                    fail_closed_rejections=self._fail_closed_rejections,
                )
            self._closed = True
            worker = self._worker
        if worker is not None:
            self.flush(timeout_sec=timeout_sec)
            self._stop_event.set()
            worker.join(timeout=timeout_sec)
        return self.metrics()


def build_completion_hook_from_env(
    writer: StorageWriter,
    env: Mapping[str, str] | None = None,
    *,
    payload_profile: PayloadProfile = "ephemeral_sample",
    max_queue_size: int = 1024,
) -> AsyncReferenceUploadHook:
    """Instantiate the reference hook from the three OTel GenAI upload environment settings."""
    source = os.environ if env is None else env
    base_path = source.get("OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH", "").strip()
    raw_fmt = source.get("OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT", "jsonl").strip().lower()
    if raw_fmt == "":
        raw_fmt = "jsonl"
    if raw_fmt not in ("jsonl", "parquet"):
        raise ValueError(
            f"Unsupported OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT: {raw_fmt!r} "
            "(expected 'jsonl' or 'parquet')."
        )
    upload_format: UploadFormat = "parquet" if raw_fmt == "parquet" else "jsonl"
    config = ReferenceUploadConfig(
        base_path=base_path,
        upload_format=upload_format,
        max_queue_size=max_queue_size,
        payload_profile=payload_profile,
    )
    return AsyncReferenceUploadHook(config=config, writer=writer)
