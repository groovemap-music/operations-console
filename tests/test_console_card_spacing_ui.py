"""Browser coverage for the vertical gap between cards on every console tab and sub-view.

The operator wants the same spacing between cards everywhere in the console. Each tab root
spaces its children with ``space-y-*``, but a nested wrapper without that class (the Extraction
Analysis report sub-view) leaves its own cards touching. This walks every tab and sub-view in a
real browser, in both themes, with the optional sections shown and hidden, and measures the gap
between each pair of stacked visible cards. Every such gap must be one value.
"""
# mypy: disable-error-code="no-untyped-def"

import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_console import sign_in


# Measured in the browser: the visible children of every visible container inside one tab panel,
# taken pairwise. A pair counts when both sides are cards (``.dashboard-card`` or a ``section``) and
# the two stack vertically, so side-by-side cards in a row and card-to-legend gaps are not measured.
CARD_GAPS = """
(panelId) => {
  const panel = document.getElementById(panelId);
  const shown = (el) => el.getClientRects().length > 0;
  const floating = (el) => ['absolute', 'fixed'].includes(getComputedStyle(el).position);
  const isCard = (el) => el.classList.contains('dashboard-card') || el.tagName === 'SECTION';
  const label = (el) => {
    if (el.id) return '#' + el.id;
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
    return el.tagName.toLowerCase() + (cls ? '.' + cls : '');
  };
  const out = [];
  const containers = [panel, ...panel.querySelectorAll('*')].filter((el) => shown(el) && el.children.length > 1);
  for (const container of containers) {
    const kids = [...container.children].filter((k) => shown(k) && !floating(k) && k.tagName !== 'SCRIPT' && k.tagName !== 'STYLE');
    for (let i = 0; i + 1 < kids.length; i++) {
      const a = kids[i];
      const b = kids[i + 1];
      if (!isCard(a) || !isCard(b)) continue;
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      const overlap = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      if (overlap < 0.5 * Math.min(ra.width, rb.width)) continue;
      out.push({ container: label(container), a: label(a), b: label(b), gap: Math.round((rb.top - ra.bottom) * 100) / 100 });
    }
  }
  return out;
}
"""

# The optional sections the Extraction Analysis report hides until it has something to show.
SHOW_OPTIONAL = """
() => ['ea-skipped-section', 'ea-violations-detail'].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.style.display = '';
})
"""
HIDE_OPTIONAL = """
() => ['ea-skipped-section', 'ea-violations-detail'].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.style.display = 'none';
})
"""

ADMIN_TABS = ["extractions", "dlq", "users", "storage", "queue-trends", "system-health", "audit-log", "extraction-analysis", "ledger"]
SHELL = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "index.html").read_text()
# Every element that stacks cards inside a tab, including the nested wrappers a sub-view adds.
CARD_STACKS = ["pipelines", "run-progress", "platform-health", "ea-view-report", "ea-view-compare", "ea-view-prompt"]


def _open_tab(page: Page, tab: str) -> str:
    page.click(f"#tab-btn-{tab}")
    panel = f"tab-{tab}"
    expect(page.locator(f"#{panel}")).to_be_visible(timeout=10000)
    page.wait_for_timeout(300)
    return panel


def _measure_all(page: Page) -> dict[str, list[dict[str, Any]]]:
    """Every tab and sub-view in the page as it stands in this theme, keyed by a readable state name."""
    states: dict[str, list[dict[str, Any]]] = {}
    for tab in ["overview", *ADMIN_TABS]:
        panel = _open_tab(page, tab)
        states[tab] = page.evaluate(CARD_GAPS, panel)
        if tab == "overview":
            for view in ["progress", "matrix"]:
                page.click(f'[data-overview-view="{view}"]')
                expect(page.locator(f'[data-overview-view="{view}"]')).to_have_attribute("aria-pressed", "true")
                page.wait_for_timeout(200)
                states[f"overview/{view}"] = page.evaluate(CARD_GAPS, panel)
            page.click('[data-overview-view="matrix"]')
        if tab == "extraction-analysis":
            page.click("#ea-btn-report")
            expect(page.locator("#ea-view-report")).to_be_visible()
            states["extraction-analysis/report"] = page.evaluate(CARD_GAPS, panel)
            page.evaluate(SHOW_OPTIONAL)
            states["extraction-analysis/report+optional"] = page.evaluate(CARD_GAPS, panel)
            page.evaluate(HIDE_OPTIONAL)
            for view in ["compare", "prompt"]:
                page.click(f"#ea-btn-{view}")
                expect(page.locator(f"#ea-view-{view}")).to_be_visible()
                page.wait_for_timeout(200)
                states[f"extraction-analysis/{view}"] = page.evaluate(CARD_GAPS, panel)
            page.click("#ea-btn-report")
    return states


def _distinct_gaps(states: dict[str, list[dict[str, Any]]]) -> dict[float, list[str]]:
    seen: dict[float, list[str]] = {}
    for state, pairs in states.items():
        for pair in pairs:
            seen.setdefault(pair["gap"], []).append(f"{state}: {pair['container']} {pair['a']} -> {pair['b']}")
    return seen


@pytest.mark.parametrize("wrapper", CARD_STACKS)
def test_every_card_stack_in_the_shell_has_the_shared_spacing(wrapper: str) -> None:
    """Fast guard without a browser: the wrapper that stacks cards carries the same ``space-y-6`` as the tab roots."""
    tag = re.search(rf'<(?:div|ul)\s+id="{wrapper}"([^>]*)>', SHELL)
    assert tag, f"#{wrapper} is missing from the shell"
    classes = re.search(r'\sclass="([^"]*)"', tag.group(1))
    assert classes and "space-y-6" in classes.group(1).split(), f"#{wrapper} has classes {classes.group(1) if classes else None!r}, not space-y-6"


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_every_tab_and_sub_view_spaces_cards_the_same(page: Page, scheme: str) -> None:
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    sign_in(page)
    states = _measure_all(page)

    distinct = _distinct_gaps(states)
    print(f"\n[{scheme}] card-to-card gaps by value (px): " + ", ".join(f"{gap}: {len(where)} pairs" for gap, where in sorted(distinct.items())))
    for gap, where in sorted(distinct.items()):
        print(f"  {gap}px")
        for line in where[:12]:
            print(f"    {line}")

    assert states["ledger"], "the Identity Ledger tab must have measurable cards"
    assert len(distinct) == 1, f"card gaps differ across the console: {sorted(distinct)}"
