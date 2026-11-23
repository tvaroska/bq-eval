"""Zero-extra-dependency GM3 Light Workbench HTTP Server (`bq-eval serve`, `M7-1`, `M9-1`)."""

from __future__ import annotations

import ipaddress
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

from bq_eval.contract.results import ShowcaseRunArtifacts
from bq_eval.showcase.pipeline import CorpusChoice, format_demo_json, run_showcase_pipeline
from bq_eval.showcase.render.renderer import render_html_scorecard, workbench_csp_header

_VALID_CORPORA: frozenset[str] = frozenset({"m2-live", "golden-4app", "retail-2app"})


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


class WorkbenchHTTPServer(ThreadingHTTPServer):
    """Threaded loopback HTTP server carrying cached corpus/scenario configuration."""

    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        default_corpus: CorpusChoice = "m2-live",
        scenario_file: Path | None = None,
    ) -> None:
        self.default_corpus: CorpusChoice = default_corpus
        self.scenario_file: Path | None = scenario_file
        self.csp_header: str = workbench_csp_header()
        self._cache_lock = threading.Lock()
        self._corpus_cache: dict[CorpusChoice, ShowcaseRunArtifacts] = {}
        self._scenario_cache: tuple[int, ShowcaseRunArtifacts] | None = None
        super().__init__(server_address, _WorkbenchRequestHandler)

    def get_artifacts(self, corpus: CorpusChoice) -> ShowcaseRunArtifacts:
        """Return cached showcase artifacts for built-in corpora or an unchanged scenario file."""
        with self._cache_lock:
            if self.scenario_file is not None:
                mtime_ns = self.scenario_file.stat().st_mtime_ns
                if self._scenario_cache is None or self._scenario_cache[0] != mtime_ns:
                    artifacts = run_showcase_pipeline(
                        corpus=corpus,
                        scenario_file=self.scenario_file,
                    )
                    self._scenario_cache = (mtime_ns, artifacts)
                return self._scenario_cache[1]
            cached = self._corpus_cache.get(corpus)
            if cached is None:
                cached = run_showcase_pipeline(corpus=corpus)
                self._corpus_cache[corpus] = cached
            return cached


class _WorkbenchRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_bytes(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
    ) -> None:
        srv = cast(WorkbenchHTTPServer, self.server)
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", srv.csp_header)
        self.end_headers()
        self.wfile.write(body)

    def _resolve_corpus(self, query_string: str) -> CorpusChoice:
        srv = cast(WorkbenchHTTPServer, self.server)
        params = parse_qs(query_string, keep_blank_values=False)
        requested = params.get("corpus", [srv.default_corpus])[0]
        if requested not in _VALID_CORPORA:
            raise ValueError(
                f"Unsupported corpus '{requested}'. Expected one of {sorted(_VALID_CORPORA)}."
            )
        return cast(CorpusChoice, requested)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        srv = cast(WorkbenchHTTPServer, self.server)

        if parsed.path == "/healthz":
            payload = json.dumps({"status": "ok", "service": "bq-eval-workbench"}).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, "application/json; charset=utf-8", payload)
            return

        if parsed.path in {"/", "/index.html"}:
            try:
                corpus = self._resolve_corpus(parsed.query)
                artifacts = srv.get_artifacts(corpus)
                html_doc = render_html_scorecard(artifacts, live_api=(srv.scenario_file is None))
            except (ValueError, OSError) as exc:
                err_body = json.dumps({"error": str(exc)}).encode("utf-8")
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8", err_body
                )
                return
            self._send_bytes(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                html_doc.encode("utf-8"),
            )
            return

        if parsed.path == "/api/v1/demo":
            try:
                corpus = self._resolve_corpus(parsed.query)
                artifacts = srv.get_artifacts(corpus)
                json_doc = format_demo_json(artifacts)
            except (ValueError, OSError) as exc:
                err_body = json.dumps({"error": str(exc)}).encode("utf-8")
                self._send_bytes(
                    HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8", err_body
                )
                return
            self._send_bytes(
                HTTPStatus.OK,
                "application/json; charset=utf-8",
                json_doc.encode("utf-8"),
            )
            return

        not_found = json.dumps({"error": f"Unknown route: {parsed.path}"}).encode("utf-8")
        self._send_bytes(HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", not_found)


def create_workbench_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    corpus: CorpusChoice = "m2-live",
    scenario_file: Path | None = None,
    allow_non_loopback: bool = False,
) -> WorkbenchHTTPServer:
    """Create a bound `WorkbenchHTTPServer` instance without blocking the caller."""
    if not _is_loopback_host(host) and not allow_non_loopback:
        raise ValueError(
            f"Refusing to bind non-loopback host {host!r} without allow_non_loopback=True "
            "(--allow-non-loopback)."
        )
    if isinstance(port, bool) or not (0 <= port <= 65535):
        raise ValueError(f"port must be in [0, 65535], got {port}.")
    return WorkbenchHTTPServer(
        (host, port),
        default_corpus=corpus,
        scenario_file=scenario_file,
    )
