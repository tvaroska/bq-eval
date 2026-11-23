"""GM3 Light HTML Workbench Renderer with single-point XSS escaping.

Imports zero calculation modules (`bq_eval.engine` is strictly forbidden here).
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from pathlib import Path

from bq_eval.demo.contract.results import ShowcaseRunArtifacts
from bq_eval.demo.render.view import (
    BadgeView,
    CardView,
    CellView,
    KpiCardView,
    PageView,
    TableView,
    TabView,
    build_showcase_page_view,
)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_PLACEHOLDER_RE = re.compile(
    r"__(?:PAGE_TITLE|FAVICON_URI|STYLESHEET|BRAND_HEADING|BRAND_SUBTITLE|"
    r"HEADER_RIGHT|MAIN_ATTRS|MAIN_CONTENT|SCRIPT_BLOCK)__"
)


def _sha256_csp_hash(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def workbench_csp_header() -> str:
    """Return strict Content-Security-Policy pinning the exact inline style/script hashes."""
    stylesheet = (ASSETS_DIR / "workbench.css").read_text(encoding="utf-8")
    script = (ASSETS_DIR / "workbench.js").read_text(encoding="utf-8")
    style_hash = _sha256_csp_hash(f"\n{stylesheet}\n  ")
    script_hash = _sha256_csp_hash(f"\n{script}\n  ")
    return (
        "default-src 'none'; "
        f"script-src {script_hash}; "
        f"style-src {style_hash}; "
        "img-src data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )


def _esc(value: str) -> str:
    """Single-point HTML/attribute entity escaping (`&`, `<`, `>`, `\"`, `'`)."""
    return html.escape(value, quote=True)


def _favicon_data_uri() -> str:
    svg_bytes = (ASSETS_DIR / "favicon.svg").read_bytes()
    encoded = base64.b64encode(svg_bytes).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _render_badge(badge: BadgeView) -> str:
    return f'<span class="badge badge-{_esc(badge.tone)}">{_esc(badge.label)}</span>'


def _render_cell(cell: CellView) -> str:
    inner = _esc(cell.text)
    if cell.code:
        inner = f"<code>{inner}</code>"
    if cell.badge_tone is not None:
        inner = f'<span class="badge badge-{_esc(cell.badge_tone)}">{inner}</span>'
    if cell.strong:
        inner = f"<strong>{inner}</strong>"
    cls = ' class="num"' if cell.align_right else ""
    return f"<td{cls}>{inner}</td>"


def _render_table(table: TableView) -> str:
    ths = "".join(
        f'<th class="num">{_esc(col.label)}</th>'
        if col.align_right
        else f"<th>{_esc(col.label)}</th>"
        for col in table.columns
    )
    trs = "\n          ".join(
        "<tr>" + "".join(_render_cell(cell) for cell in row) + "</tr>" for row in table.rows
    )
    return (
        "<table>\n"
        f"        <thead><tr>{ths}</tr></thead>\n"
        f"        <tbody>\n          {trs}\n        </tbody>\n"
        "      </table>"
    )


def _render_card(card: CardView) -> str:
    parts = [
        '<section class="card">',
        f"      <h2>{_esc(card.title)}</h2>",
        f'      <p class="sub">{_esc(card.subtitle)}</p>',
    ]
    if card.table is not None:
        parts.append(f"      {_render_table(card.table)}")
    if card.banner is not None:
        parts.append(
            f'      <div class="banner banner-{_esc(card.banner.tone)}">'
            f"{_esc(card.banner.text)}</div>"
        )
    parts.append("    </section>")
    return "\n".join(parts)


def _render_kpi_card(kpi: KpiCardView) -> str:
    badge_html = f" {_render_badge(kpi.badge)}" if kpi.badge is not None else ""
    return (
        '    <div class="kpi-card">\n'
        f'      <div class="kpi-label">{_esc(kpi.label)}{badge_html}</div>\n'
        f'      <div class="kpi-value">{_esc(kpi.value)}</div>\n'
        f'      <div class="kpi-caption">{_esc(kpi.caption)}</div>\n'
        "    </div>"
    )


def _render_tab_panel(tab: TabView, *, is_active: bool) -> str:
    cls = "tab-panel active" if is_active else "tab-panel"
    cards_html = "\n    ".join(_render_card(card) for card in tab.cards)
    return (
        f'  <div id="{_esc(tab.panel_id)}" class="{cls}" role="tabpanel">\n'
        f"    {cards_html}\n"
        "  </div>"
    )


def render_page_view_html(view: PageView) -> str:
    """Serialize a `PageView` into a self-contained GM3 Light HTML document."""
    template = (ASSETS_DIR / "workbench.html").read_text(encoding="utf-8")
    stylesheet = (ASSETS_DIR / "workbench.css").read_text(encoding="utf-8")
    script = (ASSETS_DIR / "workbench.js").read_text(encoding="utf-8")

    header_parts = [_render_badge(b) for b in view.header_badges]
    if view.live_api:
        options = "".join(
            f'<option value="{_esc(cid)}"{" selected" if cid == view.active_corpus_id else ""}>'
            f"{_esc(label)}</option>"
            for cid, label in view.corpus_choices
        )
        header_parts.insert(
            0,
            '<label for="corpus-select" class="kpi-caption">Corpus:</label>'
            f'<select id="corpus-select" class="corpus-select">'
            f"{options}</select>",
        )

    kpi_grid = (
        '  <div class="kpi-grid">\n'
        + "\n".join(_render_kpi_card(k) for k in view.kpi_cards)
        + "\n  </div>"
    )
    tab_buttons = "\n    ".join(
        f'<button type="button" class="tab-btn{" active" if idx == 0 else ""}" '
        f'role="tab" aria-selected="{"true" if idx == 0 else "false"}" '
        f'data-panel-id="{_esc(tab.panel_id)}">{_esc(tab.label)}</button>'
        for idx, tab in enumerate(view.tabs)
    )
    tab_nav = f'  <nav class="tabs" role="tablist">\n    {tab_buttons}\n  </nav>'
    tab_panels = "\n".join(
        _render_tab_panel(tab, is_active=(idx == 0)) for idx, tab in enumerate(view.tabs)
    )

    main_attrs = ' data-live-api="true"' if view.live_api else ' data-live-api="false"'
    main_content = f"{kpi_grid}\n{tab_nav}\n{tab_panels}"
    script_block = f"  <script>\n{script}\n  </script>"

    replacements = {
        "__PAGE_TITLE__": _esc(view.page_title),
        "__FAVICON_URI__": _favicon_data_uri(),
        "__STYLESHEET__": stylesheet,
        "__BRAND_HEADING__": _esc(view.brand_heading),
        "__BRAND_SUBTITLE__": _esc(view.brand_subtitle),
        "__HEADER_RIGHT__": " ".join(header_parts),
        "__MAIN_ATTRS__": main_attrs,
        "__MAIN_CONTENT__": main_content,
        "__SCRIPT_BLOCK__": script_block,
    }
    # Single-pass regex substitution prevents untrusted data matching placeholder tokens from re-expanding.
    return _PLACEHOLDER_RE.sub(lambda m: replacements[m.group(0)], template)


def render_html_scorecard(
    artifacts: ShowcaseRunArtifacts,
    *,
    live_api: bool = False,
) -> str:
    """Build and render the GM3 Light HTML workbench from `ShowcaseRunArtifacts`."""
    page_view = build_showcase_page_view(artifacts, live_api=live_api)
    return render_page_view_html(page_view)


def write_html_scorecard(artifacts: ShowcaseRunArtifacts, output_path: Path) -> Path:
    """Write a self-contained GM3 Light HTML scorecard to `output_path`."""
    html_doc = render_html_scorecard(artifacts, live_api=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_doc, encoding="utf-8")
    return output_path
