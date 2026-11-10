"""Browser coverage for the DLQ Management tab's Replay button.

Replay sits beside Purge for each dead-letter queue. The console asks the operator to confirm, posts
to its own ``/admin/api/dlq/replay/{queue}`` proxy, and reports catalog-api's answer: how many messages
moved and how many remain, or why nothing (409) or only part (502) moved. It runs against the standalone
fake catalog-api, never a real deployment.
"""
# mypy: disable-error-code="no-untyped-def"

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_console import CATALOG_URL, fake_catalog, sign_in


def open_dlq_tab(page: Page) -> None:
    sign_in(page)
    page.click("#tab-btn-dlq")
    expect(page.locator("#dlq-list")).to_be_visible(timeout=10000)
    expect(page.locator("#dlq-list button", has_text="Replay").first).to_be_visible(timeout=10000)


def replay_first_queue(page: Page, *, accept: bool = True) -> str:
    """Click the first Replay button; return the confirm text the operator saw."""
    seen: list[str] = []

    def on_dialog(dialog) -> None:
        seen.append(dialog.message)
        dialog.accept() if accept else dialog.dismiss()

    page.once("dialog", on_dialog)
    page.locator("#dlq-list button", has_text="Replay").first.click()
    return seen[0] if seen else ""


def replays() -> dict:
    return httpx.get(f"{CATALOG_URL}/__fake__/dlq-replays", timeout=5.0).json()


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestDlqReplayButton:
    def test_every_queue_has_replay_beside_purge(self, page: Page) -> None:
        open_dlq_tab(page)
        replay = page.locator("#dlq-list button", has_text="Replay")
        purge = page.locator("#dlq-list button", has_text="Purge")
        expect(replay).not_to_have_count(0)
        expect(replay).to_have_count(purge.count())

    def test_replay_confirms_then_reports_moved_and_remaining(self, page: Page) -> None:
        fake_catalog("dlq", {"parked": 97, "mode": "ok"})
        open_dlq_tab(page)
        confirm_text = replay_first_queue(page)
        assert "Replay" in confirm_text
        expect(page.locator("#toast")).to_contain_text("Replayed 97 message(s)", timeout=10000)
        expect(page.locator("#toast")).to_contain_text("0 remaining")
        assert replays()["parked"] == 0
        assert len(replays()["calls"]) == 1

    def test_declining_the_confirm_sends_nothing(self, page: Page) -> None:
        fake_catalog("dlq", {"parked": 4, "mode": "ok"})
        open_dlq_tab(page)
        replay_first_queue(page, accept=False)
        page.wait_for_timeout(500)
        assert replays() == {"calls": [], "parked": 4}

    def test_missing_work_queue_says_nothing_moved(self, page: Page) -> None:
        fake_catalog("dlq", {"parked": 5, "mode": "work-queue-missing"})
        open_dlq_tab(page)
        replay_first_queue(page)
        expect(page.locator("#toast")).to_contain_text("Work queue", timeout=10000)
        expect(page.locator("#toast")).to_contain_text("nothing was moved")
        assert replays()["parked"] == 5

    def test_partial_replay_shows_the_exact_moved_and_remaining_counts(self, page: Page) -> None:
        fake_catalog("dlq", {"parked": 5, "mode": "partial"})
        open_dlq_tab(page)
        replay_first_queue(page)
        expect(page.locator("#toast")).to_contain_text("Partial replay of", timeout=10000)
        expect(page.locator("#toast")).to_contain_text("2 moved, 3 still parked")
        assert replays()["parked"] == 3

    def test_partial_replay_with_unknown_depth_says_so(self, page: Page) -> None:
        fake_catalog("dlq", {"parked": 5, "mode": "partial-unknown"})
        open_dlq_tab(page)
        replay_first_queue(page)
        expect(page.locator("#toast")).to_contain_text("2 moved", timeout=10000)
        expect(page.locator("#toast")).to_contain_text("remaining depth is unknown")
        assert replays()["parked"] == 3

    def test_legacy_partial_replay_still_reports_the_prose_count(self, page: Page) -> None:
        fake_catalog("dlq", {"parked": 5, "mode": "partial-legacy"})
        open_dlq_tab(page)
        replay_first_queue(page)
        expect(page.locator("#toast")).to_contain_text("Partial replay", timeout=10000)
        expect(page.locator("#toast")).to_contain_text("2 message(s) moved")
        expect(page.locator("#toast")).to_contain_text("stay parked")
        assert replays()["parked"] == 3
