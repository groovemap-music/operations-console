"""Structure of the single console shell served at ``/``.

The live landing page and the admin page were merged into one document: a login form, and a
console whose first tab is the unchanged live Overview followed by the existing admin tabs.
Two pages' worth of element ids now share one document, so a duplicate id would silently bind
one tab's script to the other tab's element.
"""

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static"
SHELL = (STATIC / "index.html").read_text()
ADMIN_TABS = ["extractions", "dlq", "users", "storage", "queue-trends", "system-health", "audit-log", "extraction-analysis"]


class _Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.tabs: list[str] = []
        self.scripts: list[str] = []
        self.styles: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ids.append(element_id)
            self.styles[element_id] = attributes.get("style") or ""
        if tag == "button" and "tab-btn" in (attributes.get("class") or "").split():
            self.tabs.append(attributes.get("data-tab") or "")
        if tag == "script" and attributes.get("src"):
            self.scripts.append(attributes["src"] or "")


def _parse() -> _Elements:
    parser = _Elements()
    parser.feed(SHELL)
    return parser


def test_element_ids_are_unique() -> None:
    duplicates = [element_id for element_id, count in Counter(_parse().ids).items() if count > 1]
    assert duplicates == []


def test_overview_is_the_first_tab_and_the_admin_tabs_follow_in_order() -> None:
    assert _parse().tabs == ["overview", *ADMIN_TABS]
    assert re.search(r'class="tab-btn active" data-tab="overview"', SHELL)


def test_every_tab_has_a_panel_and_only_the_overview_starts_visible() -> None:
    elements = _parse()
    assert "display:none" not in elements.styles["tab-overview"]
    for tab in ADMIN_TABS:
        assert "display:none" in elements.styles[f"tab-{tab}"], tab


def test_the_overview_keeps_the_live_view_content() -> None:
    overview = SHELL[SHELL.index('id="tab-overview"') : SHELL.index("<!-- /tab-overview -->")]
    for element_id in ("pipeline-discogs", "pipeline-musicbrainz", "db-neo4j", "db-postgresql", "activityLog", "lastUpdated"):
        assert f'id="{element_id}"' in overview, element_id
    assert 'id="dlq-toggle"' in SHELL


def test_the_console_and_login_are_both_hidden_until_the_script_decides() -> None:
    elements = _parse()
    assert "display:none" in elements.styles["console-view"]
    assert "display:none" in elements.styles["login-view"]
    assert "login-reason" in elements.ids


def test_the_live_view_script_loads_before_the_session_owner() -> None:
    """dashboard.js must be listening before admin.js can announce a session."""
    assert _parse().scripts[-2:] == ["/dashboard.js", "/admin.js"]


def test_there_is_no_separate_admin_page() -> None:
    assert not (STATIC / "admin.html").exists()
    assert 'href="/admin"' not in SHELL
