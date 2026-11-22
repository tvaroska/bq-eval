"""Zero-extra-dependency GM3 Light Workbench HTTP Server (`bq-eval serve`, `M7-1`)."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

from bq_eval.showcase.pipeline import CorpusChoice, format_demo_json, run_showcase_pipeline
from bq_eval.showcase.renderer import render_html_scorecard

_VALID_CORPORA: frozenset[str] = frozenset({"m2-live", "golden-4app", "retail-2app"})


class WorkbenchHTTPServer(ThreadingHTTPServer):
    """Threaded loopback HTTP server carrying default corpus/scenario configuration."""

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
        super().__init__(server_address, _WorkbenchRequestHandler)


class _WorkbenchRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_bytes(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
                artifacts = run_showcase_pipeline(corpus=corpus, scenario_file=srv.scenario_file)
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
                artifacts = run_showcase_pipeline(corpus=corpus, scenario_file=srv.scenario_file)
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
) -> WorkbenchHTTPServer:
    """Create a bound `WorkbenchHTTPServer` instance without blocking the caller."""
    if not (0 <= port <= 65535):
        raise ValueError(f"port must be in [0, 65535], got {port}.")
    return WorkbenchHTTPServer(
        (host, port),
        default_corpus=corpus,
        scenario_file=scenario_file,
    )
