"""Firing Grafana alerts: configured, unconfigured, and failing, and the token never leaving the server."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from dashboard import grafana_alerts
from tests.dashboard_test_app import create_test_app


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.fake_catalog_api import FakeCatalogApi


BASE = "https://grafana.test"
TOKEN = "glsa_super_secret_token_value"


class FakeGrafana:
    """Records every request and answers with a fixed status and body."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: Any = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, Exception):
            raise self.body
        return httpx.Response(self.status, content=self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode())


@pytest.fixture
def grafana(monkeypatch: pytest.MonkeyPatch) -> FakeGrafana:
    fake = FakeGrafana()
    monkeypatch.setattr(grafana_alerts, "_transport", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    token_file = tmp_path / "grafana-token"
    token_file.write_text(f"{TOKEN}\n")
    monkeypatch.setenv("GRAFANA_URL", f"{BASE}/")
    monkeypatch.setenv("GRAFANA_TOKEN_FILE", str(token_file))
    return token_file


@pytest.fixture
def client(fake_catalog: FakeCatalogApi) -> Iterator[TestClient]:
    with TestClient(create_test_app(), headers={"Authorization": f"Bearer {fake_catalog.mint()}"}) as test_client:
        yield test_client


def _alert(name: str, severity: str | None = None, **extra: Any) -> dict[str, Any]:
    labels = {"alertname": name, **({"severity": severity} if severity else {})}
    return {"labels": labels, "annotations": {"summary": f"{name} is firing"}, "startsAt": "2026-10-24T10:00:00Z", **extra}


class TestUnconfigured:
    @pytest.mark.parametrize("variables", [{}, {"GRAFANA_URL": BASE}, {"GRAFANA_TOKEN_FILE": "/run/secrets/grafana"}])
    def test_alerts_need_both_the_url_and_the_token_file(
        self, client: TestClient, grafana: FakeGrafana, monkeypatch: pytest.MonkeyPatch, variables: dict[str, str]
    ) -> None:
        for name in ("GRAFANA_URL", "GRAFANA_API_URL", "GRAFANA_TOKEN_FILE"):
            monkeypatch.delenv(name, raising=False)
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        response = client.get("/api/alerts")
        assert response.status_code == 200
        assert response.json() == {"configured": False, "available": False, "alerts": [], "note": None}
        assert grafana.requests == []


class TestConfigured:
    def test_firing_alerts_are_served_with_severity_and_link(self, client: TestClient, grafana: FakeGrafana, configured: Path) -> None:
        grafana.body = [
            _alert("HighLag", "critical", generatorURL=f"{BASE}/alerting/grafana/abc/view"),
            _alert("DiskFull", "warning", generatorURL="https://evil.example/phish"),
            _alert("NoSeverity"),
            "not an alert",
        ]
        body = client.get("/api/alerts").json()
        assert body["configured"] is True
        assert body["available"] is True
        assert [(a["name"], a["severity"]) for a in body["alerts"]] == [("HighLag", "crit"), ("DiskFull", "warn"), ("NoSeverity", "warn")]
        assert body["alerts"][0]["url"] == f"{BASE}/alerting/grafana/abc/view"
        assert body["alerts"][1]["url"] == f"{BASE}/alerting/list"
        assert body["alerts"][2]["url"] == f"{BASE}/alerting/list"
        assert body["alerts"][0]["since"] == "2026-10-24T10:00:00Z"

    def test_grafana_is_asked_for_active_alerts_with_the_bearer(self, client: TestClient, grafana: FakeGrafana, configured: Path) -> None:
        client.get("/api/alerts")
        (request,) = grafana.requests
        assert request.url.path == "/api/alertmanager/grafana/api/v2/alerts"
        assert dict(request.url.params) == {"active": "true", "silenced": "false", "inhibited": "false"}
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_a_flood_is_capped(self, client: TestClient, grafana: FakeGrafana, configured: Path) -> None:
        grafana.body = [_alert(f"A{i}") for i in range(grafana_alerts.ALERT_LIMIT + 10)]
        assert len(client.get("/api/alerts").json()["alerts"]) == grafana_alerts.ALERT_LIMIT

    def test_a_bad_timestamp_is_dropped(self, client: TestClient, grafana: FakeGrafana, configured: Path) -> None:
        grafana.body = [_alert("A", startsAt="yesterday-ish")]
        assert client.get("/api/alerts").json()["alerts"][0]["since"] is None


class TestErrors:
    @pytest.mark.parametrize(
        "failure",
        [
            ("status", 500, b"boom"),
            ("status", 401, b"unauthorized"),
            ("status", 200, b"<html>not json</html>"),
            ("status", 200, b'{"message": "an object, not a list"}'),
            ("raise", httpx.ConnectError("refused"), None),
        ],
    )
    def test_a_grafana_failure_degrades_with_a_visible_note(
        self, client: TestClient, grafana: FakeGrafana, configured: Path, failure: tuple[Any, ...]
    ) -> None:
        if failure[0] == "raise":
            grafana.body = failure[1]
        else:
            grafana.status, grafana.body = failure[1], failure[2]
        response = client.get("/api/alerts")
        assert response.status_code == 200
        assert response.json() == {"configured": True, "available": False, "alerts": [], "note": grafana_alerts.UNAVAILABLE_NOTE}

    @pytest.mark.parametrize("content", [None, ""])
    def test_a_missing_or_empty_token_file_is_unconfigured_without_asking_grafana(
        self, client: TestClient, grafana: FakeGrafana, configured: Path, content: str | None
    ) -> None:
        if content is None:
            configured.unlink()
        else:
            configured.write_text(content)
        assert client.get("/api/alerts").json() == {"configured": False, "available": False, "alerts": [], "note": None}
        assert grafana.requests == []

    def test_an_empty_token_file_variable_is_unconfigured(
        self, client: TestClient, grafana: FakeGrafana, configured: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRAFANA_TOKEN_FILE", "")
        assert client.get("/api/alerts").json()["configured"] is False
        assert grafana.requests == []


class TestApiUrl:
    def test_the_server_asks_grafana_api_url_and_links_use_grafana_url(
        self, client: TestClient, grafana: FakeGrafana, configured: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRAFANA_API_URL", "http://grafana:3000/")
        grafana.body = [_alert("A", generatorURL=f"{BASE}/alerting/grafana/x/view"), _alert("B", generatorURL="http://grafana:3000/internal")]
        body = client.get("/api/alerts").json()
        (request,) = grafana.requests
        assert (request.url.scheme, request.url.host, request.url.port) == ("http", "grafana", 3000)
        assert body["configured"] is True
        assert [a["url"] for a in body["alerts"]] == [f"{BASE}/alerting/grafana/x/view", f"{BASE}/alerting/list"]

    def test_it_defaults_to_grafana_url(self, client: TestClient, grafana: FakeGrafana, configured: Path) -> None:
        client.get("/api/alerts")
        (request,) = grafana.requests
        assert request.url.host == "grafana.test"

    def test_grafana_api_url_alone_configures_alerts(
        self, client: TestClient, grafana: FakeGrafana, configured: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GRAFANA_URL")
        monkeypatch.setenv("GRAFANA_API_URL", BASE)
        assert client.get("/api/alerts").json()["configured"] is True


class TestAuthFailures:
    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refused_token_stays_configured_but_unavailable(self, client: TestClient, grafana: FakeGrafana, configured: Path, status: int) -> None:
        grafana.status, grafana.body = status, b"forbidden"
        assert client.get("/api/alerts").json() == {"configured": True, "available": False, "alerts": [], "note": grafana_alerts.UNAVAILABLE_NOTE}


class TestTheTokenStaysOnTheServer:
    def test_it_is_in_no_response_and_no_log_whether_grafana_answers_or_fails(
        self, client: TestClient, grafana: FakeGrafana, configured: Path
    ) -> None:
        seen: list[str] = []
        with capture_logs() as logs:
            for status, body in ((200, [_alert("A", "critical")]), (500, b"echo " + TOKEN.encode()), (200, b"Bearer " + TOKEN.encode())):
                grafana.status, grafana.body = status, body
                response = client.get("/api/alerts")
                seen.append(response.text)
                seen.append(json.dumps(dict(response.headers)))
            grafana.body = httpx.ConnectError(f"cannot reach with Bearer {TOKEN}")
            seen.append(client.get("/api/alerts").text)
        seen.append(json.dumps(logs, default=str))
        assert TOKEN not in "\n".join(seen)
        assert any(entry["event"] == "grafana_alerts_unavailable" for entry in logs)

    def test_the_page_never_asks_for_the_token(self, client: TestClient) -> None:
        assert TOKEN not in client.get("/").text
        assert "GRAFANA_TOKEN" not in client.get("/dashboard.js").text


class TestTheRouteIsGated:
    def test_an_anonymous_caller_is_refused(self, fake_catalog: FakeCatalogApi, grafana: FakeGrafana, configured: Path) -> None:
        with TestClient(create_test_app()) as anonymous:
            response = anonymous.get("/api/alerts")
        assert response.status_code == 401
        assert grafana.requests == []
