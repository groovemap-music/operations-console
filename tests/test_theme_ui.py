"""Playwright tests for the Label web theme: the OS default, the header toggle, persistence, and the brand marks."""
# mypy: disable-error-code="no-untyped-def"

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_console import CONSOLE_URL, sign_in


DARK = {"canvas": "#0e0d0b", "accent": "rgb(184, 150, 42)", "text": "rgb(242, 237, 228)", "scheme": "dark"}
LIGHT = {"canvas": "#eeeae3", "accent": "rgb(77, 0, 24)", "text": "rgb(26, 23, 20)", "scheme": "light"}

ROOT = "(name) => getComputedStyle(document.documentElement)[name]"
THEME = "() => document.documentElement.dataset.theme"
META = "() => [...document.querySelectorAll('meta[name=theme-color]')].map((m) => m.content)"
STORED = "() => localStorage.getItem('groovemap-theme')"
VISIBLE_MARK = "() => [...document.querySelectorAll('#app-logo img')].filter((i) => i.offsetParent).map((i) => i.getAttribute('src'))"


def open_console(page: Page, scheme: str | None) -> None:
    """Sign in with the OS asked for ``scheme`` (None = the browser's default) and no stored choice."""
    if scheme:
        page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    sign_in(page)


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestTheme:
    """Default = the OS; a click wins, persists, and is mirrored in theme-color, the mark and the native controls."""

    @pytest.mark.parametrize(("scheme", "expected"), [("dark", DARK), ("light", LIGHT)])
    def test_the_default_follows_the_os(self, page: Page, scheme: str, expected: dict[str, str]) -> None:
        open_console(page, scheme)
        assert page.evaluate(THEME) == scheme
        assert page.evaluate(STORED) is None
        assert page.evaluate(META) == [expected["canvas"]] * 2
        assert page.evaluate(ROOT, "colorScheme") == expected["scheme"]
        assert page.evaluate(ROOT, "accentColor") == expected["accent"]
        expect(page.locator("h1 .wordmark")).to_have_css("color", expected["text"])
        assert page.evaluate(VISIBLE_MARK) == [f"/brand/icon-{scheme}.svg"]

    def test_the_theme_is_applied_before_the_first_paint(self, page: Page) -> None:
        """The inline script sits in <head>, ahead of every stylesheet and the body, so there is no flash."""
        html = page.request.get(CONSOLE_URL).text()
        stylesheet = html.index('href="/tailwind.css"')
        assert html.index("window.GrooveMapTheme") < stylesheet < html.index("<body")
        assert "matchMedia('(prefers-color-scheme: light)')" in html[:stylesheet]

    def test_the_toggle_switches_persists_and_wins_over_the_os(self, page: Page) -> None:
        open_console(page, "dark")
        toggle = page.locator("#theme-toggle")
        expect(toggle).to_be_visible()
        expect(toggle).to_have_attribute("aria-label", "Switch to light theme")
        toggle.click()
        assert page.evaluate(THEME) == "light"
        assert page.evaluate(STORED) == "light"
        assert page.evaluate(META) == [LIGHT["canvas"]] * 2
        assert page.evaluate(ROOT, "colorScheme") == "light"
        assert page.evaluate(VISIBLE_MARK) == ["/brand/icon-light.svg"]
        expect(toggle).to_have_attribute("aria-label", "Switch to dark theme")

        page.reload()  # the OS still says dark; the stored choice wins
        expect(page.locator("#console-view")).to_be_visible(timeout=10000)
        assert page.evaluate(THEME) == "light"

        page.locator("#theme-toggle").click()
        assert page.evaluate(THEME) == "dark"
        assert page.evaluate(STORED) == "dark"

    def test_live_os_changes_apply_only_without_a_stored_choice(self, page: Page) -> None:
        open_console(page, "dark")
        page.emulate_media(color_scheme="light")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'light'")
        assert page.evaluate(META) == [LIGHT["canvas"]] * 2

        page.locator("#theme-toggle").click()  # an explicit choice (dark) now beats the OS
        page.emulate_media(color_scheme="dark")
        page.emulate_media(color_scheme="light")
        assert page.evaluate(THEME) == "dark"

    @pytest.mark.parametrize(("scheme", "card"), [("dark", "rgb(18, 17, 14)"), ("light", "rgb(251, 249, 245)")])
    def test_the_card_surface_comes_from_the_warm_scale(self, page: Page, scheme: str, card: str) -> None:
        open_console(page, scheme)
        expect(page.locator("nav.dashboard-card").first).to_have_css("background-color", card)

    @pytest.mark.parametrize("scheme", ["dark", "light"])
    def test_the_wordmark_is_jura_300_with_the_brand_tagline(self, page: Page, scheme: str) -> None:
        open_console(page, scheme)
        wordmark = page.locator("h1 .wordmark")
        expect(wordmark).to_have_text("groovemap")
        expect(wordmark).to_have_css("font-weight", "300")
        expect(wordmark).to_have_css("font-family", re.compile(r'^"?Jura"?, sans-serif$'))
        expect(wordmark).to_have_css("letter-spacing", "2.88px")  # 0.12em of the 24px (text-2xl) size
        expect(page.locator("h1 .brand-tagline")).to_have_text("explore the connections behind music")
        assert page.evaluate("async () => (await document.fonts.load('300 24px Jura')).length") == 1
        assert page.evaluate("() => document.fonts.check('300 24px Jura')")

    @pytest.mark.parametrize("scheme", ["dark", "light"])
    def test_native_controls_follow_the_theme(self, page: Page, scheme: str) -> None:
        """Date inputs take the theme's colour scheme and fields, and the focus ring is the accent."""
        open_console(page, scheme)
        page.locator("#tab-btn-ledger").click()
        since = page.locator("#ledger-since")
        expect(since).to_be_visible()
        assert page.evaluate("(el) => getComputedStyle(el).colorScheme", since.element_handle()) == scheme
        field = {"dark": "rgb(20, 19, 16)", "light": "rgb(243, 239, 232)"}[scheme]
        expect(since).to_have_css("background-color", field)
        since.focus()
        expect(since).to_have_css("border-top-color", {"dark": DARK, "light": LIGHT}[scheme]["accent"])
