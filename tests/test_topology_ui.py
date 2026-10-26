"""Playwright tests for the topology drill-down: one pipeline's flow, opened from its matrix head on desktop widths."""
# mypy: disable-error-code="no-untyped-def"

import pytest
from playwright.sync_api import Page, expect

from tests.test_dashboard_ui import DESKTOP, LAYOUT_FAULTS, PHONE, show_scenario


# The narrowest viewport that has the Topology control (TopologyFlow.MIN_WIDTH), and one just below.
BREAKPOINT = {"width": 1024, "height": 768}
TABLET = {"width": 1023, "height": 768}

# Horizontal overflow inside the open dialog: the flow must fit without panning.
DIALOG_OVERFLOW = "() => { const d = document.getElementById('topology-dialog'); return d.scrollWidth - d.clientWidth; }"


def node(page: Page, key: str):
    return page.locator(f'#topology-flow [data-node="{key}"]')


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestTopologyDrillDown:
    """The flow is optional: opened from a pipeline's matrix head at 1024 px or wider, absent below."""

    def test_desktop_opens_the_flow_from_the_matrix_head_and_closes_it(self, page: Page) -> None:
        """The control opens the pipeline's flow with the matrix's states; Escape closes it and focus returns."""
        show_scenario(page, DESKTOP)
        button = page.locator('#pipeline-discogs .mx-pipeline-head [data-topology="discogs"]')
        expect(button).to_be_visible()
        button.click()

        dialog = page.locator("#topology-dialog")
        expect(dialog).to_be_visible()
        expect(page.locator("#topology-title")).to_have_text("Discogs pipeline topology")
        expect(page.locator("#topology-flow .tp-queue")).to_have_count(8)
        expect(page.locator("#topology-flow [data-kind=exchange]")).to_have_count(4)
        expect(page.locator("#topology-flow [data-kind=datastore]")).to_have_count(2)
        # Producer → 4 exchanges → 8 queues → 2 consumers → 2 datastores.
        expect(page.locator("#topology-flow [data-edge]")).to_have_count(4 + 8 + 8 + 2)

        stalled = node(page, "queue:tableinator:releases")
        expect(stalled).to_have_attribute("data-state", "crit")
        expect(stalled).to_contain_text("◆ STALLED · no acks 1m")
        expect(node(page, "queue:graphinator:artists")).to_have_attribute("data-state", "warn")
        expect(node(page, "queue:graphinator:artists")).to_contain_text("▲ DLQ 12")
        expect(node(page, "consumer:tableinator")).to_contain_text("◆ 1 critical")
        # Each queue node carries the state of its matrix cell, under the same rules.
        states = page.evaluate(
            """() => [...document.querySelectorAll('#topology-flow .tp-queue')].map((g) => [
                g.dataset.state,
                document.getElementById(`discogs-${g.dataset.consumer}-${g.dataset.entity}-cell`).dataset.state,
            ])"""
        )
        assert all(flow == cell for flow, cell in states), states
        # The stalled queue acknowledges nothing: its consumer edge is a dashed hairline.
        expect(page.locator('[data-edge="queue:tableinator:releases>consumer:tableinator"]')).to_have_class("tp-edge tp-edge-idle")
        expect(page.locator('[data-edge="queue:graphinator:releases>consumer:graphinator"]')).to_have_class("tp-edge tp-edge-flowing")
        assert page.evaluate(DIALOG_OVERFLOW) <= 0

        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(button).to_be_focused()

    def test_a_queue_node_opens_the_existing_drawer_over_the_flow(self, page: Page) -> None:
        """Selecting a queue opens its matrix drawer; closing the drawer leaves the flow open."""
        show_scenario(page, DESKTOP)
        page.locator('[data-topology="musicbrainz"]').click()
        expect(page.locator("#topology-title")).to_have_text("MusicBrainz pipeline topology")
        growing = node(page, "queue:brainzgraphinator:releases")
        expect(growing).to_have_attribute("data-state", "crit")
        expect(growing).to_contain_text("◆ DLQ +1.4k / 5m")
        growing.click()

        drawer = page.locator("#queue-drawer")
        expect(drawer).to_be_visible()
        expect(page.locator("#queue-drawer-title")).to_have_text("Releases → Brainzgraphinator")
        page.keyboard.press("Escape")
        expect(drawer).to_be_hidden()
        expect(page.locator("#topology-dialog")).to_be_visible()
        page.click("#topology-close")
        expect(page.locator("#topology-dialog")).to_be_hidden()

    def test_the_flow_fits_the_narrowest_desktop_and_closes_below_it(self, page: Page) -> None:
        """At 1024 px the flow fits without panning; narrowing the window closes it and hides the control."""
        show_scenario(page, BREAKPOINT)
        if not page.evaluate(f"() => matchMedia('(min-width: {BREAKPOINT['width']}px)').matches"):
            # WebKit with a classic scrollbar measures the media query without it: 1024 px of page.
            scrollbar = page.evaluate("() => window.innerWidth - document.documentElement.clientWidth")
            page.set_viewport_size({"width": BREAKPOINT["width"] + scrollbar, "height": BREAKPOINT["height"]})
        page.locator('[data-topology="discogs"]').click()
        expect(page.locator("#topology-dialog")).to_be_visible()
        assert page.evaluate(DIALOG_OVERFLOW) <= 0

        page.set_viewport_size(TABLET)
        expect(page.locator("#topology-dialog")).to_be_hidden()
        expect(page.locator("[data-topology]")).to_have_count(2)
        for control in page.locator("[data-topology]").all():
            expect(control).to_be_hidden()

    def test_phone_has_no_topology_control_and_the_overview_is_unchanged(self, page: Page) -> None:
        """At 390 px there is no control to open the flow; the matrix still marks the fault without overflow."""
        show_scenario(page, PHONE)
        for control in page.locator("[data-topology]").all():
            expect(control).to_be_hidden()
        expect(page.locator("#topology-dialog")).to_be_hidden()
        expect(page.locator("#discogs-tableinator-releases-cell")).to_have_attribute("data-state", "crit")
        assert page.evaluate(LAYOUT_FAULTS) == {"overflow": 0, "broken": [], "spills": []}

    @pytest.mark.parametrize(
        ("theme", "text", "edge", "crit"),
        [
            ("dark", "rgb(242, 237, 228)", "rgb(143, 125, 87)", "rgb(255, 92, 138)"),
            ("light", "rgb(26, 23, 20)", "rgb(122, 106, 69)", "rgb(194, 24, 91)"),
        ],
    )
    def test_the_flow_draws_with_the_theme_tokens(self, page: Page, theme: str, text: str, edge: str, crit: str) -> None:
        """Text and edges take the matrix's checked tokens in both themes, so the flow is legible in either."""
        show_scenario(page, DESKTOP)
        page.evaluate(f"() => {{ document.documentElement.dataset.theme = '{theme}'; }}")
        page.locator('[data-topology="discogs"]').click()
        name = page.locator('#topology-flow [data-node="producer"] .tp-name')
        expect(name).to_have_css("fill", text)
        expect(page.locator('#topology-flow [data-edge="producer>exchange:releases"]')).to_have_css("stroke", edge)
        expect(node(page, "queue:tableinator:releases").locator(".tp-sub")).to_have_css("fill", crit)
