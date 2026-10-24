"""Run progress: expected totals, per-run acked baselines, and the route that reads them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import history, progress
from tests.dashboard_test_app import create_test_app


if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.fake_catalog_api import FakeCatalogApi


START = 1_790_000_000  # the run's start, epoch seconds
END = START + 3600
START_ISO = "2026-09-21T14:13:20+00:00"
END_ISO = "2026-09-21T15:13:20+00:00"
RELEASES = "groovemap-discogs-tableinator-releases"
GRAPH_RELEASES = "groovemap-discogs-graphinator-releases"


class FakeVictoriaMetrics:
    """Answers instant queries by (query name, evaluation time) and range queries by query name."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.instant: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.ranges: dict[str, list[dict[str, Any]]] = {}
        self.status = 200

    @staticmethod
    def _name(query: str) -> str:
        for name, metric in (("acked", progress.ACKED), ("records", progress.RECORDS), ("file_progress", progress.FILE_PROGRESS)):
            if metric in query:
                return name
        raise AssertionError(query)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="boom")
        params = dict(request.url.params)
        name = self._name(params["query"])
        instant = request.url.path == "/api/v1/query"
        result = self.instant.get((name, int(params["time"])), []) if instant else self.ranges.get(name, [])
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "x", "result": result}})

    def instant_times(self, name: str) -> list[int]:
        return sorted(
            int(dict(r.url.params)["time"])
            for r in self.requests
            if r.url.path == "/api/v1/query" and self._name(dict(r.url.params)["query"]) == name
        )


def sample(label: str, key: str, value: float) -> dict[str, Any]:
    return {"metric": {label: key}, "value": [0, str(value)]}


def series(label: str, key: str, points: list[tuple[int, float]]) -> dict[str, Any]:
    return {"metric": {label: key}, "values": [[ts, str(v)] for ts, v in points]}


@pytest.fixture
def vm(monkeypatch: pytest.MonkeyPatch) -> FakeVictoriaMetrics:
    fake = FakeVictoriaMetrics()
    monkeypatch.setattr(history, "_transport", httpx.MockTransport(fake))
    monkeypatch.delenv("VICTORIA_METRICS_URL", raising=False)
    return fake


def a_run(fake: FakeVictoriaMetrics) -> None:
    """An hour into a Discogs run: releases 40 % read with 2,000,000 extracted; tableinator has loaded 1.5M of them."""
    fake.instant[("acked", START)] = [sample("queue", RELEASES, 9_000_000), sample("queue", GRAPH_RELEASES, 4_000_000)]
    fake.instant[("acked", END)] = [sample("queue", RELEASES, 10_500_000), sample("queue", GRAPH_RELEASES, 4_800_000)]
    fake.ranges["acked"] = [
        series("queue", RELEASES, [(START, 9_000_000), (START + 1800, 9_750_000), (START + 3570, 10_490_000)]),
        series("queue", GRAPH_RELEASES, [(START, 4_000_000), (START + 1800, 4_400_000), (START + 3570, 4_790_000)]),
    ]
    fake.instant[("records", START)] = [sample("entity", "releases", 17_000_000)]
    fake.instant[("records", END)] = [sample("entity", "releases", 19_000_000)]
    fake.ranges["records"] = [series("entity", "releases", [(START, 17_000_000), (START + 3570, 18_990_000)])]
    fake.instant[("file_progress", END)] = [sample("entity", "releases", 0.4)]


def open_console(fake_catalog: FakeCatalogApi) -> TestClient:
    return TestClient(create_test_app(), headers={"Authorization": f"Bearer {fake_catalog.mint()}"})


@pytest.fixture
def client(fake_catalog: FakeCatalogApi) -> Iterator[TestClient]:
    with open_console(fake_catalog) as test_client:
        yield test_client


def get(client: TestClient, pipeline: str = "discogs", **params: str) -> httpx.Response:
    return client.get(f"/api/progress/{pipeline}", params={"start": START_ISO, "end": END_ISO, **params})


def by(items: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    return next(item for item in items if item[key] == value)


class TestQueries:
    def test_the_promql_is_pinned(self) -> None:
        assert progress.progress_queries("discogs", "groovemap-discogs", 3600) == {
            "acked": 'sum by (queue) (rabbitmq_detailed_queue_messages_acked_total{queue=~"groovemap\\\\-discogs-.*"})',
            "records": 'sum by (entity) (groovemap_extraction_records_total{source="discogs"})',
            "file_progress": 'max by (entity) (last_over_time(groovemap_extraction_file_progress_ratio{source="discogs"}[3600s]))',
        }

    def test_the_burn_up_step_follows_the_run_and_never_beats_the_scrape(self) -> None:
        assert progress.step_seconds(60) == 15
        assert progress.step_seconds(3600) == 30
        assert progress.step_seconds(24 * 3600) == 720


class TestExpectedTotal:
    def test_expected_is_extracted_over_the_fraction_of_the_file_read(self) -> None:
        assert progress.expected_records(2_000_000, 0.4) == 5_000_000
        assert progress.expected_records(2_400_000, 1.0) == 2_400_000

    @pytest.mark.parametrize(("extracted", "ratio"), [(None, 0.5), (1000, None), (0, 0.5), (1000, 0.0)])
    def test_no_total_is_invented_without_both_numbers(self, extracted: float | None, ratio: float | None) -> None:
        assert progress.expected_records(extracted, ratio) is None

    def test_a_ratio_overshooting_one_reads_as_a_finished_file(self) -> None:
        assert progress.expected_records(1000, 1.02) == 1000


class TestCounterIncrease:
    def test_the_increase_is_measured_from_the_baseline(self) -> None:
        assert progress.counter_increase(100, [(10, 100), (20, 150), (30, 200)], start=10) == [(20, 50), (30, 100)]

    def test_a_counter_reset_keeps_what_was_counted_before_it(self) -> None:
        # The broker restarted between 20 and 30: its counter began again at 0 and reached 40.
        assert progress.counter_increase(100, [(20, 150), (30, 40), (40, 90)], start=10) == [(20, 50), (30, 90), (40, 140)]

    def test_there_is_no_increase_without_a_baseline(self) -> None:
        assert progress.counter_increase(None, [(20, 150)], start=10) is None


class TestRoute:
    def test_loaded_is_acked_since_the_baseline_read_at_the_run_start(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        a_run(vm)
        body = get(client).json()
        assert (body["pipeline"], body["start"], body["end"]) == ("discogs", START_ISO, END_ISO)
        tableinator = by(body["queues"], "queue", RELEASES)
        assert tableinator == {
            "queue": RELEASES,
            "consumer": "tableinator",
            "entity": "releases",
            "baseline": 9_000_000,
            "loaded": 1_500_000,
            "points": [["2026-09-21T14:43:20+00:00", 750_000], ["2026-09-21T15:12:50+00:00", 1_490_000], [END_ISO, 1_500_000]],
        }
        assert by(body["queues"], "queue", GRAPH_RELEASES)["loaded"] == 800_000
        assert vm.instant_times("acked") == [START, END]

    def test_expected_is_the_runs_extracted_records_over_its_file_progress(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        a_run(vm)
        releases = by(get(client).json()["entities"], "entity", "releases")
        assert releases == {"entity": "releases", "extracted": 2_000_000, "file_progress": 0.4, "expected": 5_000_000}
        assert vm.instant_times("records") == [START, END]
        assert vm.instant_times("file_progress") == [END]

    def test_an_entity_without_a_gauge_sample_in_the_run_has_no_expected_total(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        a_run(vm)
        masters = by(get(client).json()["entities"], "entity", "masters")
        assert masters == {"entity": "masters", "extracted": 0, "file_progress": None, "expected": None}

    def test_a_queue_without_a_baseline_has_no_loaded_count(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        a_run(vm)
        masters = by(get(client).json()["queues"], "queue", "groovemap-discogs-tableinator-masters")
        assert (masters["baseline"], masters["loaded"], masters["points"]) == (None, None, [])

    def test_with_no_data_at_the_start_nothing_is_derived(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        a_run(vm)
        del vm.instant[("acked", START)]
        body = get(client).json()
        assert {q["loaded"] for q in body["queues"]} == {None}
        assert {e["extracted"] for e in body["entities"]} == {None}
        assert {e["expected"] for e in body["entities"]} == {None}

    def test_a_console_restart_mid_run_reads_the_same_baselines(self, fake_catalog: FakeCatalogApi, vm: FakeVictoriaMetrics) -> None:
        a_run(vm)
        with open_console(fake_catalog) as before:
            first = get(before).json()
        # A fresh console process: no state carries over, and none is needed.
        with open_console(fake_catalog) as after:
            second = get(after).json()
        assert first == second
        assert by(second["queues"], "queue", RELEASES)["baseline"] == 9_000_000
        # Both read the baseline at the run's start, not at the console's.
        assert vm.instant_times("acked") == [START, START, END, END]

    def test_a_running_run_is_read_up_to_now(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        response = client.get("/api/progress/discogs", params={"start": START_ISO})
        assert response.status_code == 200
        assert response.json()["end"] > END_ISO

    def test_victoria_metrics_down_is_unavailable(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        vm.status = 500
        response = get(client)
        assert response.status_code == 503
        assert response.json()["detail"] == "history unavailable"

    def test_an_unknown_pipeline_is_not_found(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        assert get(client, pipeline="spotify").status_code == 404
        assert vm.requests == []

    @pytest.mark.parametrize(
        ("start", "end"), [(END_ISO, START_ISO), ("2999-01-01T00:00:00+00:00", None), ("2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")]
    )
    def test_a_window_that_is_not_a_past_run_is_refused(self, client: TestClient, vm: FakeVictoriaMetrics, start: str, end: str | None) -> None:
        params = {"start": start, **({"end": end} if end else {})}
        assert client.get("/api/progress/discogs", params=params).status_code == 400
        assert vm.requests == []

    def test_a_naive_time_is_read_as_utc(self, client: TestClient, vm: FakeVictoriaMetrics) -> None:
        a_run(vm)
        body = client.get("/api/progress/discogs", params={"start": START_ISO[:-6], "end": END_ISO[:-6]}).json()
        assert by(body["queues"], "queue", RELEASES)["loaded"] == 1_500_000
