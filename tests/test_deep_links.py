"""The base URLs the console serves for its Grafana and traces deep links."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from dashboard.deep_links import base_url
from tests.dashboard_test_app import create_test_app


if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.fake_catalog_api import FakeCatalogApi


@pytest.fixture
def client(fake_catalog: FakeCatalogApi, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for name in ("GRAFANA_URL", "TRACES_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_test_app(), headers={"Authorization": f"Bearer {fake_catalog.mint()}"}) as test_client:
        yield test_client


class TestBaseUrl:
    def test_trailing_slashes_and_whitespace_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRAFANA_URL", "  https://grafana.test/sub//  ")
        assert base_url("GRAFANA_URL") == "https://grafana.test/sub"

    @pytest.mark.parametrize("value", ["", "   ", "javascript:alert(1)", "grafana.test", "ftp://grafana.test", "https://"])
    def test_anything_that_is_not_an_http_url_is_unset(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("GRAFANA_URL", value)
        assert base_url("GRAFANA_URL") is None

    def test_an_unset_variable_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GRAFANA_URL", raising=False)
        assert base_url("GRAFANA_URL") is None


class TestRoute:
    def test_both_are_absent_when_unset(self, client: TestClient) -> None:
        assert client.get("/api/links").json() == {"grafana": None, "traces": None}

    def test_traces_follow_grafana_unless_overridden(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRAFANA_URL", "https://grafana.test/")
        assert client.get("/api/links").json() == {"grafana": "https://grafana.test", "traces": "https://grafana.test"}
        monkeypatch.setenv("TRACES_URL", "https://traces.test")
        assert client.get("/api/links").json() == {"grafana": "https://grafana.test", "traces": "https://traces.test"}

    def test_traces_alone_gives_no_grafana_link(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACES_URL", "https://traces.test")
        assert client.get("/api/links").json() == {"grafana": None, "traces": "https://traces.test"}

    def test_an_anonymous_caller_is_refused(self, fake_catalog: FakeCatalogApi, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRAFANA_URL", "https://grafana.test")
        with TestClient(create_test_app()) as anonymous:
            assert anonymous.get("/api/links").status_code == 401
