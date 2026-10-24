"""Playwright tests for run progress: the ETA headline above each matrix and the Run progress view."""
# mypy: disable-error-code="no-untyped-def"

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_console import freeze_live_feed, sign_in
from tests.test_dashboard_ui import DESKTOP, PHONE, SCENARIO


STARTED = datetime.now(UTC) - timedelta(hours=2)
RUNNING = {"id": "11111111-1111-4111-8111-111111111111", "status": "running", "started_at": STARTED.isoformat(), "completed_at": None}
COMPLETED = {
    "id": "22222222-2222-4222-8222-222222222222",
    "status": "completed",
    "started_at": STARTED.isoformat(),
    "completed_at": (STARTED + timedelta(seconds=11520)).isoformat(),
    "duration_seconds": 11520,
    "record_counts": {"masters": 900000, "releases": 1000000, "artists": 800000, "labels": 300000, "total": 3000000},
    "error_message": None,
}

# 36 readings 2 s apart for every queue: graphinator acknowledges 400/s and tableinator 800/s.
HEALTHY = """
() => {
  const dashboard = window.dashboard;
  dashboard._resetHistory();
  const start = Date.now() + 1000;
  for (let i = 0; i < 36; i++) {
    const pipelines = {};
    for (const p of dashboard.topology.pipelines) {
      const queues = [];
      p.consumers.forEach((c, ci) => c.queues.forEach((q) => {
        const ack = ci === 0 ? 400 : 800;
        queues.push({ name: q.name, messages: 5000, messages_ready: 4800, messages_unacknowledged: 200, consumers: 1, message_rate: ack, ack_rate: ack });
        queues.push({ name: q.dead_letter_queue, messages: 0, messages_ready: 0, messages_unacknowledged: 0, consumers: 0, message_rate: 0, ack_rate: 0 });
      }));
      pipelines[p.id] = { services: [], queues, queues_available: true };
    }
    dashboard.updateDashboard({ timestamp: new Date(start + i * 2000).toISOString(), pipelines, databases: [] });
  }
}
"""


def serve_runs(page: Page, runs: list[dict]) -> None:
    """Serve the extraction history the console proxies from catalog-api."""
    page.route("**/admin/api/extractions", lambda route: route.fulfill(json={"extractions": runs, "total": len(runs), "offset": 0, "limit": 20}))


def serve_progress(page: Page, *, releases_ratio: float | None = 0.4) -> list[str]:
    """Answer /api/progress the way the console derives it from VictoriaMetrics: every entity expects
    1,000,000 records and every queue has loaded 400,000 since the run started. Returns the URLs asked."""
    asked: list[str] = []

    def fulfill(route: Route) -> None:
        asked.append(route.request.url)
        topology = route.fetch(url=route.request.url.split("/api/progress/")[0] + "/api/topology").json()
        pipeline_id = route.request.url.split("/api/progress/")[1].split("?")[0]
        pipeline = next(p for p in topology["pipelines"] if p["id"] == pipeline_id)
        end = datetime.now(UTC)
        entities = [
            {"entity": e, "extracted": 400000, "file_progress": None, "expected": None}
            if e == "releases" and releases_ratio is None
            else {"entity": e, "extracted": 400000, "file_progress": 0.4, "expected": 1000000}
            for e in pipeline["entity_types"]
        ]
        queues = [
            {
                "queue": q["name"],
                "consumer": c["name"],
                "entity": q["entity"],
                "baseline": 10,
                "loaded": 400000,
                "points": [[(STARTED + timedelta(hours=1)).isoformat(), 200000], [end.isoformat(), 400000]],
            }
            for c in pipeline["consumers"]
            for q in c["queues"]
        ]
        body = {"pipeline": pipeline_id, "start": STARTED.isoformat(), "end": end.isoformat(), "entities": entities, "queues": queues}
        route.fulfill(body=json.dumps(body), content_type="application/json")

    page.route("**/api/progress/**", fulfill)
    return asked


def open_console(page: Page, viewport: dict[str, int], scenario: str) -> None:
    page.set_viewport_size(viewport)
    sign_in(page)
    freeze_live_feed(page)
    page.evaluate(scenario)


@pytest.mark.e2e
@pytest.mark.usefixtures("test_server")
class TestRunProgress:
    """A Discogs run in progress, or finished, with its progress read from the console."""

    def test_a_running_extraction_heads_its_matrix_with_the_finish_time_and_bottleneck(self, page: Page) -> None:
        serve_runs(page, [RUNNING])
        asked = serve_progress(page)
        open_console(page, DESKTOP, HEALTHY)
        headline = page.locator('[data-run-headline="discogs"]')
        # 600,000 records left per queue: graphinator at 400/s needs 25 minutes, tableinator 12.5.
        expect(headline).to_have_text("Finishes in 25m, bottleneck Graphinator")
        expect(headline).to_have_attribute("data-kind", "finishes")
        assert headline.bounding_box()["y"] < page.locator('[data-matrix="discogs"]').bounding_box()["y"]
        # No MusicBrainz run is recorded, so its matrix has no headline.
        expect(page.locator('[data-run-headline="musicbrainz"]')).to_be_hidden()
        assert any("/api/progress/discogs?start=" in url and "end=" not in url for url in asked)

    def test_a_stalled_consumer_means_the_run_cannot_finish(self, page: Page) -> None:
        serve_runs(page, [RUNNING])
        serve_progress(page)
        open_console(page, DESKTOP, SCENARIO)
        headline = page.locator('[data-run-headline="discogs"]')
        expect(headline).to_have_text("◆ Cannot finish: Tableinator · Releases stalled")
        expect(headline).to_have_attribute("data-state", "crit")

    def test_the_progress_view_shows_the_burn_up_and_bars_and_links_the_run(self, page: Page) -> None:
        serve_runs(page, [RUNNING])
        serve_progress(page)
        open_console(page, DESKTOP, HEALTHY)
        page.get_by_role("button", name="Run progress").click()
        expect(page.locator("#pipelines")).to_be_hidden()
        section = page.locator("#run-progress-discogs")
        expect(section).to_be_visible()
        expect(section.locator("[data-run-summary]")).to_have_text("Finishes in 25m, bottleneck Graphinator")
        expect(section.locator(".rp-burnup polyline.rp-line")).to_have_count(2)
        expect(section.locator(".rp-burnup polyline.rp-projection")).to_have_count(2)
        releases = section.locator('[data-run-entity="releases"]')
        expect(releases.locator(".rp-entity-head")).to_have_text("Releases · 1.00M expected · 40% read")
        row = releases.locator('[data-run-cell="tableinator-releases"]')
        expect(row.locator(".rp-pct")).to_have_text("40%")
        expect(row.locator(".rp-eta")).to_have_text(re.compile(r"^ETA 1[23]m$"))
        expect(page.locator("#run-progress-musicbrainz")).to_contain_text("No extraction run recorded")

        section.get_by_role("button", name=re.compile("run details")).click()
        expect(page.locator("#tab-extractions")).to_be_visible()
        run_row = page.locator(f"#extraction-run-{RUNNING['id']}")
        expect(run_row).to_have_attribute("data-highlight", "true")
        expect(run_row).to_be_focused()

    def test_a_missing_file_progress_gauge_says_progress_unavailable(self, page: Page) -> None:
        serve_runs(page, [RUNNING])
        serve_progress(page, releases_ratio=None)
        open_console(page, DESKTOP, HEALTHY)
        expect(page.locator('[data-run-headline="discogs"]')).to_have_text("Progress unavailable: no file progress for Releases")
        page.get_by_role("button", name="Run progress").click()
        section = page.locator("#run-progress-discogs")
        expect(section.locator(".rp-unavailable")).to_have_text("Progress unavailable: no file progress for Releases")
        expect(section.locator(".rp-burnup")).to_have_count(0)
        expect(section.locator('[data-run-entity="releases"] .rp-gap')).to_have_text(["progress unavailable", "progress unavailable"])

    def test_a_completed_run_shows_its_duration_and_final_counts_and_no_headline(self, page: Page) -> None:
        serve_runs(page, [COMPLETED])
        asked = serve_progress(page)
        open_console(page, DESKTOP, HEALTHY)
        page.get_by_role("button", name="Run progress").click()
        section = page.locator("#run-progress-discogs")
        expect(section).to_have_attribute("data-run-state", "completed")
        expect(section.locator("[data-run-summary]")).to_have_text("Completed in 3h12m · 3,000,000 records")
        expect(section.locator('[data-run-entity="releases"] .rp-entity-head')).to_have_text("Releases · 1,000,000 extracted")
        expect(section.locator('[data-run-cell="graphinator-releases"] .rp-eta')).to_have_text("400,000 loaded")
        expect(section.locator(".rp-burnup polyline.rp-projection")).to_have_count(0)
        assert any("end=" in url for url in asked)

        page.get_by_role("button", name="Matrix").click()
        expect(page.locator('[data-run-headline="discogs"]')).to_be_hidden()

        page.click("#tab-btn-extractions")
        row = page.locator(f"#extraction-run-{COMPLETED['id']}")
        expect(row).to_contain_text("3h12m")
        expect(row).to_contain_text("3,000,000")

    def test_the_progress_view_fits_a_phone(self, page: Page) -> None:
        serve_runs(page, [RUNNING])
        serve_progress(page)
        open_console(page, PHONE, HEALTHY)
        page.get_by_role("button", name="Run progress").click()
        expect(page.locator("#run-progress-discogs .rp-row").first).to_be_visible()
        assert page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
