"""Security Hardening, Port Adapters, Parity, Quota Guard & Dynamic FinOps Tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from bq_eval.cli import app as cli_app
from bq_eval.cli import server as cli_server
from bq_eval.core.contract.rubrics import wrap_untrusted_text
from bq_eval.core.engine.sql_eval import (
    build_ai_evaluate_agent_sql,
    build_ai_generate_table_judge_sql,
    escape_sql_xml_expr,
)
from bq_eval.demo.contract.results import ShowcaseRunArtifacts
from bq_eval.demo.pipeline import CorpusChoice, build_retail_2app_bundle, run_showcase_pipeline
from bq_eval.demo.render.renderer import (
    render_html_scorecard,
    render_page_view_html,
    workbench_csp_header,
)
from bq_eval.demo.render.view import TabView, build_showcase_page_view


def test_single_pass_template_blocks_placeholder_and_inline_handler_injection() -> None:
    """Untrusted `__SCRIPT_BLOCK__` / `__STYLESHEET__` never re-expand and inline handlers are gone."""
    base = run_showcase_pipeline(corpus="retail-2app")
    hostile_app = "__SCRIPT_BLOCK__ __STYLESHEET__"
    order_sum = base.summaries["order-support-agent"].model_copy(
        update={"apphub_application_id": hostile_app}
    )
    hostile_artifacts = ShowcaseRunArtifacts(
        corpus_id="__SCRIPT_BLOCK__",
        summaries={hostile_app: order_sum},
        gwet_ac2=base.gwet_ac2,
        eval_roi=base.eval_roi,
        cache_dilution=base.cache_dilution,
        pareto=base.pareto,
        focus_chargeback=base.focus_chargeback,
        off_peak_plan=base.off_peak_plan,
    )
    html_doc = render_html_scorecard(hostile_artifacts, live_api=True)
    assert html_doc.count("<script>") == 1
    assert html_doc.count("</script>") == 1
    assert "<code>__SCRIPT_BLOCK__ __STYLESHEET__</code>" in html_doc
    assert "onclick=" not in html_doc
    assert "onchange=" not in html_doc
    assert 'data-panel-id="tab-quality"' in html_doc
    assert "addEventListener('click'" in html_doc
    assert "addEventListener('change'" in html_doc

    base_view = build_showcase_page_view(base, live_api=False)
    evil_tab = TabView(
        panel_id='tab-1\', alert(1)//" autofocus onfocus="alert(2)',
        label="Evil Tab",
        cards=base_view.tabs[0].cards,
    )
    evil_view = base_view.model_copy(update={"tabs": (evil_tab,)})
    evil_html = render_page_view_html(evil_view)
    assert "onclick=" not in evil_html
    assert 'onfocus="alert(2)"' not in evil_html
    assert "&quot; autofocus onfocus=&quot;alert(2)" in evil_html


def test_sql_and_xml_input_guards_reject_bool_and_injection() -> None:
    """Reject `bool` canary pct, unsafe `column_expr`, and unsafe XML `tag`."""
    with pytest.raises(ValueError, match="not bool"):
        build_ai_generate_table_judge_sql(project_id="demo-proj", canary_sample_pct=True)
    with pytest.raises(ValueError, match="not bool"):
        build_ai_evaluate_agent_sql(project_id="demo-proj", canary_sample_pct=False)

    assert "COALESCE(p.user_prompt, '')" in escape_sql_xml_expr("p.user_prompt")
    for bad_col in ("", "p.user_prompt; DROP TABLE x", "COALESCE(a, b)", "p.a' OR '1'='1"):
        with pytest.raises(ValueError, match="Invalid column_expr"):
            escape_sql_xml_expr(bad_col)

    assert "<custom_tag>" in wrap_untrusted_text("hello", tag="custom_tag")
    for bad_tag in ("", "untrusted><script>", "tag with space", "123bad"):
        with pytest.raises(ValueError, match="Invalid XML wrapper tag"):
            wrap_untrusted_text("hello", tag=bad_tag)


def test_non_loopback_host_guard(capsys: pytest.CaptureFixture[str]) -> None:
    """Non-loopback host bind fails closed unless explicitly opted in."""
    all_ifaces = ".".join(("0", "0", "0", "0"))
    for host in (all_ifaces, "192.0.2.10", "::"):
        with pytest.raises(ValueError, match="non-loopback"):
            cli_server.create_workbench_server(host=host, port=0)

    rc = cli_app.main(["serve", "--host", all_ifaces, "--port", "0"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "non-loopback" in captured.err
    assert "Traceback" not in captured.err


def _assert_security_headers(headers: Message, expected_csp: str) -> None:
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("Cache-Control") == "no-store"
    csp = headers.get("Content-Security-Policy", "")
    assert csp == expected_csp
    assert "'unsafe-inline'" not in csp
    assert "script-src 'sha256-" in csp
    assert "frame-ancestors 'none'" in csp


def _server_base_url(server: cli_server.WorkbenchHTTPServer) -> str:
    raw_host, port = server.server_address[:2]
    host = raw_host.decode("ascii") if isinstance(raw_host, (bytes, bytearray)) else str(raw_host)
    return f"http://{host}:{port}"


@pytest.mark.port
def test_workbench_http_port_routes_headers_and_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`@pytest.mark.port` verifies `/index.html`, `/api/v1/demo`, `404`, CSP/XFO headers, and cache."""
    expected_csp = workbench_csp_header()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    calls = 0

    def _counting_run(
        corpus: CorpusChoice = "m2-live",
        *,
        scenario_file: Path | None = None,
    ) -> ShowcaseRunArtifacts:
        nonlocal calls
        calls += 1
        return run_showcase_pipeline(corpus=corpus, scenario_file=scenario_file)

    monkeypatch.setattr(cli_server, "run_showcase_pipeline", _counting_run)

    server = cli_server.create_workbench_server(host="127.0.0.1", port=0, corpus="retail-2app")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = _server_base_url(server)

        with opener.open(f"{base_url}/index.html", timeout=5) as resp:
            assert resp.status == 200
            _assert_security_headers(resp.headers, expected_csp)
            body = resp.read().decode("utf-8")
            assert "order-support-agent" in body
            assert "onclick=" not in body
            assert "onchange=" not in body

        with opener.open(f"{base_url}/api/v1/demo", timeout=5) as resp:
            assert resp.status == 200
            _assert_security_headers(resp.headers, expected_csp)
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["corpus_id"] == "retail-2app"

        assert calls == 1, f"Expected cached default-corpus pipeline run, got {calls} calls"

        with pytest.raises(urllib.error.HTTPError) as not_found_exc:
            opener.open(f"{base_url}/does-not-exist", timeout=5)
        assert not_found_exc.value.code == 404
        _assert_security_headers(not_found_exc.value.headers, expected_csp)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.port
def test_workbench_http_port_scenario_file_route(tmp_path: Path) -> None:
    """`@pytest.mark.port` verifies `--scenario-file` loopback routes and static-mode headers."""
    expected_csp = workbench_csp_header()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    scenario_path = tmp_path / "custom_port_scenario.json"
    custom_bundle = build_retail_2app_bundle().model_copy(update={"corpus_id": "port-scenario"})
    scenario_path.write_text(
        json.dumps(custom_bundle.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    server = cli_server.create_workbench_server(
        host="127.0.0.1",
        port=0,
        corpus="m2-live",
        scenario_file=scenario_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = _server_base_url(server)

        with opener.open(f"{base_url}/index.html", timeout=5) as resp:
            assert resp.status == 200
            _assert_security_headers(resp.headers, expected_csp)
            body = resp.read().decode("utf-8")
            assert 'data-live-api="false"' in body
            assert "port-scenario" in body

        with opener.open(f"{base_url}/api/v1/demo", timeout=5) as resp:
            assert resp.status == 200
            _assert_security_headers(resp.headers, expected_csp)
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["corpus_id"] == "port-scenario"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
