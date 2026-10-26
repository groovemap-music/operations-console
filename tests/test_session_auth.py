"""The console's admin-session boundary: who can read data, open the live feed, and when.

Every data route and the WebSocket sit behind catalog-api's ``admin_session`` operation, served
here by the in-process fake catalog-api (``fake_catalog``), which answers with catalog-api's
statuses. The negative cases are the point: an anonymous, expired, revoked, non-admin, or
unverifiable request reads nothing, and a WebSocket ticket opens exactly one socket within 30
seconds and never reaches a log line or a span.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.websockets import WebSocketDisconnect

from dashboard import session_auth
from dashboard.dashboard import ConsoleApp, install_cors
from dashboard.session_auth import (
    WS_CLOSE_UNAUTHENTICATED,
    WS_TICKET_SCOPE_KEY,
    AdminSession,
    SessionCache,
    TicketRedactingFilter,
    TicketStore,
    gate,
    take_ws_ticket,
)
from tests.dashboard_test_app import create_test_app
from tests.fake_catalog_api import ADMIN_EMAIL, ADMIN_PASSWORD


if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.fake_catalog_api import FakeCatalogApi


DATA_ROUTES = [
    "/api/metrics",
    "/api/services",
    "/api/queues",
    "/api/databases",
    "/api/queue-prefixes",
    "/api/topology",
    "/api/session",
]
# Answer 503 without VictoriaMetrics, so they are probed for the gate only.
HISTORY_ROUTES = ["/api/history/queues", "/api/history/health", "/api/progress/discogs?start=2026-01-01T00:00:00Z"]


# What the console's own pages send with every request; without it a state-changing request
# that relies on the cookie is refused before the session gate runs.
CONSOLE = {"X-GrooveMap-Console": "1"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _session(seconds: float = 3600) -> AdminSession:
    return AdminSession(admin_id="admin-1", email="admin@example.com", expires_at=datetime.now(UTC) + timedelta(seconds=seconds))


@pytest.fixture
def client(fake_catalog: FakeCatalogApi) -> Iterator[TestClient]:
    """The console's production routes over fixed metrics, with no session attached.

    Over HTTPS, as a browser reaches a deployed console: the session cookie is Secure, so the
    client's cookie jar sends it back only on an https URL.
    """
    with TestClient(create_test_app(), base_url="https://console.test") as test_client:
        yield test_client


def _assert_denied(client: TestClient, headers: dict[str, str], status: int, reason: str) -> None:
    for path in [*DATA_ROUTES, *HISTORY_ROUTES]:
        response = client.get(path, headers=headers)
        assert response.status_code == status, path
        assert response.json()["reason"] == reason, path
        assert "pipelines" not in response.text and "discogs" not in response.text, path
    ticket = client.post("/api/ws-ticket", headers={**CONSOLE, **headers})
    assert ticket.status_code == status
    assert "ticket" not in ticket.json()


class TestAnonymousReadsNothing:
    def test_every_data_route_rejects_a_request_without_a_session(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        _assert_denied(client, {}, 401, "missing")
        assert fake_catalog.session_calls == 0, "a request with no bearer must not cost a catalog-api call"

    def test_rejection_names_the_bearer_scheme(self, client: TestClient) -> None:
        response = client.get("/api/metrics")
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("header", ["Basic YWRtaW46cGFzcw==", "Bearer", "Bearer not a token", "Bearer " + "a" * 5000, "token-without-scheme"])
    def test_a_malformed_authorization_header_is_rejected_without_a_catalog_call(
        self, client: TestClient, fake_catalog: FakeCatalogApi, header: str
    ) -> None:
        response = client.get("/api/metrics", headers={"Authorization": header})
        assert response.status_code == 401
        assert response.json()["reason"] == "invalid"
        assert fake_catalog.session_calls == 0

    def test_health_and_the_login_shell_stay_public(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200
        shell = client.get("/")
        assert shell.status_code == 200
        assert 'id="login-view"' in shell.text
        # The shell is static markup: no live reading is rendered into it server-side.
        assert "groovemap-discogs-graphinator-artists" not in shell.text

    @pytest.mark.parametrize("path", ["/admin", "/admin.html"])
    def test_the_old_admin_page_redirects_to_the_shell(self, client: TestClient, path: str) -> None:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/"


class TestSessionStates:
    def test_a_valid_admin_session_reads_every_data_route(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        headers = _bearer(fake_catalog.mint())
        for path in DATA_ROUTES:
            assert client.get(path, headers=headers).status_code == 200, path
        assert "discogs" in client.get("/api/metrics", headers=headers).json()["pipelines"]

    def test_the_session_route_reports_the_live_session(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint("ops@example.com")
        response = client.get("/api/session", headers=_bearer(token))
        body = response.json()
        assert body["email"] == "ops@example.com"
        assert datetime.fromisoformat(body["expires_at"]) == fake_catalog.tokens[token].expires_at
        assert response.headers["cache-control"] == "no-store"

    def test_an_expired_session_is_rejected(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        _assert_denied(client, _bearer(fake_catalog.mint(lifetime_seconds=-1)), 401, "invalid")

    def test_a_revoked_session_is_rejected(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        fake_catalog.revoke(token)
        _assert_denied(client, _bearer(token), 401, "invalid")

    def test_an_unknown_token_is_rejected(self, client: TestClient) -> None:
        _assert_denied(client, _bearer("never-issued-token"), 401, "invalid")

    def test_an_authenticated_non_admin_is_rejected(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        """catalog-api answers 403 for a valid non-admin token; the console treats it as no session."""
        _assert_denied(client, _bearer(fake_catalog.mint(is_admin=False)), 401, "invalid")

    def test_a_catalog_api_error_denies_access_as_unavailable(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        fake_catalog.outage = True
        _assert_denied(client, _bearer(token), 503, "unavailable")

    def test_an_unreachable_catalog_api_denies_access_as_unavailable(self, client: TestClient) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        gate.use_transport(httpx.MockTransport(refuse))
        _assert_denied(client, _bearer("some-live-looking-token"), 503, "unavailable")

    @pytest.mark.parametrize(
        "body",
        [
            {"admin_id": "a", "email": "e"},
            {"admin_id": "a", "email": "e", "expires_at": "not-a-date"},
            {"admin_id": 1, "email": "e", "expires_at": "2099-01-01T00:00:00+00:00"},
            ["not", "an", "object"],
        ],
    )
    def test_a_malformed_session_body_denies_access(self, client: TestClient, body: Any) -> None:
        gate.use_transport(httpx.MockTransport(lambda _request: httpx.Response(200, json=body)))
        response = client.get("/api/metrics", headers=_bearer("some-token"))
        assert response.status_code == 503
        assert response.json()["reason"] == "unavailable"

    def test_a_200_for_an_already_expired_session_is_rejected(self, client: TestClient) -> None:
        expired = {"admin_id": "a", "email": "e", "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
        gate.use_transport(httpx.MockTransport(lambda _request: httpx.Response(200, json=expired)))
        assert client.get("/api/metrics", headers=_bearer("some-token")).json()["reason"] == "invalid"

    def test_a_naive_expiry_is_read_as_utc(self, client: TestClient) -> None:
        later = {"admin_id": "a", "email": "e", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()}
        gate.use_transport(httpx.MockTransport(lambda _request: httpx.Response(200, json=later)))
        assert client.get("/api/metrics", headers=_bearer("some-token")).status_code == 200


class TestSessionCache:
    def test_a_live_session_is_validated_once_within_the_cache_window(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        headers = _bearer(fake_catalog.mint())
        for path in DATA_ROUTES:
            client.get(path, headers=headers)
        assert fake_catalog.session_calls == 1

    def test_failures_are_never_cached(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        fake_catalog.outage = True
        assert client.get("/api/metrics", headers=_bearer(token)).status_code == 503
        fake_catalog.outage = False
        assert client.get("/api/metrics", headers=_bearer(token)).status_code == 200

    def test_the_cache_is_keyed_by_a_token_digest(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        client.get("/api/metrics", headers=_bearer(token))
        assert token not in repr(gate.cache._entries)
        assert gate.cache.get(session_auth.token_digest(token)) is not None

    def test_logout_through_the_console_drops_the_cached_session(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        """Without the eviction a revoked bearer would keep reading data for up to 30 seconds."""
        token = fake_catalog.mint()
        assert client.get("/api/metrics", headers=_bearer(token)).status_code == 200

        async def upstream_logout(*_args: Any, **_kwargs: Any) -> httpx.Response:
            fake_catalog.revoke(token)
            return httpx.Response(200, json={"logged_out": True})

        with patch("dashboard.admin_proxy._proxy_request", side_effect=upstream_logout):
            client.post("/admin/api/logout", headers=_bearer(token))

        assert client.get("/api/metrics", headers=_bearer(token)).status_code == 401

    def test_ttl_is_capped_at_thirty_seconds(self) -> None:
        assert SessionCache(300).ttl_seconds == 30
        assert SessionCache(-5).ttl_seconds == 0

    def test_an_entry_expires_after_the_ttl(self) -> None:
        now = [1000.0]
        cache = SessionCache(30, monotonic=lambda: now[0])
        cache.put("key", _session())
        now[0] += 29.9
        assert cache.get("key") is not None
        now[0] += 0.2
        assert cache.get("key") is None

    def test_an_entry_never_outlives_its_session(self) -> None:
        now = [1000.0]
        cache = SessionCache(30, monotonic=lambda: now[0])
        cache.put("key", _session(seconds=5))
        now[0] += 5.1
        assert cache.get("key") is None

    def test_a_zero_ttl_caches_nothing(self) -> None:
        cache = SessionCache(0)
        cache.put("key", _session())
        assert cache.get("key") is None
        assert len(cache) == 0

    def test_the_cache_is_bounded(self) -> None:
        cache = SessionCache(30, max_entries=2)
        for key in ("a", "b", "c"):
            cache.put(key, _session())
        assert len(cache) == 2
        assert cache.get("a") is None


class TestTicketStore:
    def test_a_ticket_redeems_exactly_once(self) -> None:
        store = TicketStore()
        ticket, lifetime = store.issue(_session(), "bearer")
        assert lifetime == 30
        grant = store.redeem(ticket)
        assert grant is not None and grant.token == "bearer"
        assert store.redeem(ticket) is None

    def test_a_ticket_expires_after_thirty_seconds(self) -> None:
        now = [1000.0]
        store = TicketStore(monotonic=lambda: now[0])
        ticket, _ = store.issue(_session(), "bearer")
        now[0] += 30.0
        assert store.redeem(ticket) is None

    def test_a_ticket_never_outlives_its_session(self) -> None:
        store = TicketStore()
        _ticket, lifetime = store.issue(_session(seconds=4), "bearer")
        assert lifetime <= 4

    def test_unknown_and_empty_tickets_are_refused(self) -> None:
        store = TicketStore()
        assert store.redeem(None) is None
        assert store.redeem("") is None
        assert store.redeem("never-issued") is None

    def test_tickets_are_stored_by_digest_and_bounded(self) -> None:
        store = TicketStore(max_outstanding=2)
        tickets = [store.issue(_session(), "bearer")[0] for _ in range(3)]
        assert len(store) == 2
        assert not any(ticket in repr(store._tickets) for ticket in tickets)
        assert store.redeem(tickets[0]) is None

    def test_expired_tickets_are_pruned_on_issue(self) -> None:
        now = [1000.0]
        store = TicketStore(monotonic=lambda: now[0])
        store.issue(_session(), "bearer")
        now[0] += 31
        store.issue(_session(), "bearer")
        assert len(store) == 1


def _close_code(client: TestClient, url: str) -> int:
    with client.websocket_connect(url) as websocket, pytest.raises(WebSocketDisconnect) as closed:
        while True:
            websocket.receive_text()
    return closed.value.code


class TestWebSocketTickets:
    def test_a_ticket_from_an_authenticated_post_opens_the_live_feed(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        response = client.post("/api/ws-ticket", headers=_bearer(fake_catalog.mint()))
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["expires_in"] == 30
        with client.websocket_connect(f"/ws?ticket={body['ticket']}") as websocket:
            assert websocket.receive_json()["type"] == "metrics_update"

    def test_a_reused_ticket_is_closed_with_4401(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        ticket = client.post("/api/ws-ticket", headers=_bearer(fake_catalog.mint())).json()["ticket"]
        with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            websocket.receive_json()
        assert _close_code(client, f"/ws?ticket={ticket}") == WS_CLOSE_UNAUTHENTICATED

    def test_an_expired_ticket_is_closed_with_4401(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        gate.tickets.ttl_seconds = 0
        ticket = client.post("/api/ws-ticket", headers=_bearer(fake_catalog.mint())).json()["ticket"]
        assert _close_code(client, f"/ws?ticket={ticket}") == WS_CLOSE_UNAUTHENTICATED

    @pytest.mark.parametrize("url", ["/ws", "/ws?ticket=", "/ws?ticket=forged", "/ws?other=1"])
    def test_a_missing_or_forged_ticket_is_closed_with_4401(self, client: TestClient, url: str) -> None:
        assert _close_code(client, url) == WS_CLOSE_UNAUTHENTICATED

    def test_a_ticket_left_in_the_query_string_does_not_authenticate(self) -> None:
        """Only a ticket stripped from the loggable query string is honoured, so the gate fails closed."""
        from dashboard.dashboard import websocket_endpoint

        unstripped = FastAPI()
        unstripped.websocket("/ws")(websocket_endpoint)
        ticket, _ = gate.tickets.issue(_session(), "bearer")
        with TestClient(unstripped) as client:
            assert _close_code(client, f"/ws?ticket={ticket}") == WS_CLOSE_UNAUTHENTICATED

    def test_a_socket_whose_session_is_revoked_is_closed_with_4401(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        fake_catalog.revoke(token)
        gate.cache.evict(session_auth.token_digest(token))
        with patch("dashboard.dashboard._WS_SESSION_RECHECK_SECONDS", 0.05):
            assert _close_code(client, f"/ws?ticket={ticket}") == WS_CLOSE_UNAUTHENTICATED

    def test_a_socket_is_closed_with_4401_when_its_session_expires(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint(lifetime_seconds=0.5)
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        started = time.monotonic()
        assert _close_code(client, f"/ws?ticket={ticket}") == WS_CLOSE_UNAUTHENTICATED
        assert time.monotonic() - started < 5

    def test_a_live_session_keeps_its_socket_across_rechecks(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        ticket = client.post("/api/ws-ticket", headers=_bearer(fake_catalog.mint())).json()["ticket"]
        with patch("dashboard.dashboard._WS_SESSION_RECHECK_SECONDS", 0.05), client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            assert websocket.receive_json()["type"] == "metrics_update"
            time.sleep(0.3)
            # The 2-second broadcast loop keeps delivering to a socket that passed its rechecks.
            messages = [websocket.receive_json()["type"] for _ in range(2)]
            assert "metrics_update" in messages


class TestEventBacklog:
    @staticmethod
    def _seed(events: Any) -> None:
        from tests.test_events import _reading

        events.observe(_reading())
        events.observe(_reading(databases={"Neo4j": "unhealthy"}))

    def test_a_connecting_client_receives_the_recent_events_after_the_first_reading(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        from dashboard import dashboard as console

        # The live loop runs in another thread: let it record its own first reading (the baseline) before seeding,
        # or that reading can land between the seeded ones and add an event of its own.
        deadline = time.monotonic() + 5
        while console.dashboard.latest_metrics is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self._seed(console.dashboard.events)
        ticket = client.post("/api/ws-ticket", headers=_bearer(fake_catalog.mint())).json()["ticket"]
        with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            assert websocket.receive_json()["type"] == "metrics_update"
            backlog = websocket.receive_json()
        assert backlog["type"] == "event_backlog"
        assert [(e["kind"], e["subject"]) for e in backlog["data"]] == [("datastore-down", "Neo4j")]

    def test_a_client_with_nothing_to_catch_up_on_still_gets_an_empty_backlog(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        ticket = client.post("/api/ws-ticket", headers=_bearer(fake_catalog.mint())).json()["ticket"]
        with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            websocket.receive_json()
            assert websocket.receive_json() == {"type": "event_backlog", "data": []}


class TestTicketNeverLogged:
    def test_take_ws_ticket_moves_the_ticket_out_of_the_query_string(self) -> None:
        scope: dict[str, Any] = {"type": "websocket", "query_string": b"a=1&ticket=SECRET&b=2"}
        take_ws_ticket(scope)
        assert scope["query_string"] == b"a=1&b=2"
        assert scope[WS_TICKET_SCOPE_KEY] == "SECRET"

    def test_every_ticket_parameter_is_stripped_and_the_first_is_kept(self) -> None:
        scope: dict[str, Any] = {"type": "websocket", "query_string": b"ticket=FIRST&ticket=SECOND"}
        take_ws_ticket(scope)
        assert scope["query_string"] == b""
        assert scope[WS_TICKET_SCOPE_KEY] == "FIRST"

    def test_http_scopes_are_untouched(self) -> None:
        scope: dict[str, Any] = {"type": "http", "query_string": b"ticket=abc"}
        take_ws_ticket(scope)
        assert scope == {"type": "http", "query_string": b"ticket=abc"}

    def test_the_redacting_filter_rewrites_any_record_carrying_a_ticket(self) -> None:
        record = logging.LogRecord(
            "uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]', ("1.2.3.4", "/ws?ticket=SECRET&x=1"), None
        )
        assert TicketRedactingFilter().filter(record) is True
        assert record.getMessage() == '1.2.3.4 - "WebSocket /ws?ticket=[redacted]&x=1" [accepted]'

    def test_the_redacting_filter_is_installed_on_the_request_target_loggers(self) -> None:
        for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi", "websockets.server"):
            assert any(isinstance(existing, TicketRedactingFilter) for existing in logging.getLogger(name).filters), name

    def test_request_spans_never_carry_the_ticket(self) -> None:
        """OpenTelemetry's middleware wraps the whole stack; ConsoleApp strips the ticket before it."""

        def spans_for(app: FastAPI) -> str:
            exporter = InMemorySpanExporter()
            provider = SdkTracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))

            @app.websocket("/ws")
            async def echo(websocket: WebSocket) -> None:
                await websocket.accept()
                await websocket.close()

            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
            try:
                with TestClient(app) as client, client.websocket_connect("/ws?ticket=SECRET-TICKET&x=1"):
                    pass
            finally:
                FastAPIInstrumentor.uninstrument_app(app)
            return repr([(span.name, dict(span.attributes or {})) for span in exporter.get_finished_spans()])

        assert "SECRET-TICKET" in spans_for(FastAPI()), "control: an unstripped app records the query string"
        assert "SECRET-TICKET" not in spans_for(ConsoleApp())

    @pytest.mark.parametrize("log_level", ["info", "trace"])
    def test_a_real_uvicorn_server_logs_no_ticket(self, fake_catalog: FakeCatalogApi, log_level: str) -> None:
        """Run the console under uvicorn and read every record every logger emitted for one ticketed socket."""
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                # The test's own websockets client logs its request line; only the server's count.
                if not record.name.startswith("websockets.client"):
                    records.append(f"{record.name}: {self.format(record)}")

        capture = Capture(level=logging.DEBUG)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = uvicorn.Config(create_test_app(), host="127.0.0.1", port=port, log_level=log_level, ws="websockets", lifespan="on")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
                logging.getLogger(name).addHandler(capture)

            token = fake_catalog.mint()

            async def open_feed() -> str:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                    ticket: str = (await http.post("/api/ws-ticket", headers=_bearer(token))).json()["ticket"]
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws?ticket={ticket}") as feed:
                    await feed.recv()
                return ticket

            ticket = asyncio.run(open_feed())
            time.sleep(0.2)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
                logging.getLogger(name).removeHandler(capture)
            root.removeHandler(capture)
            root.setLevel(previous_level)

        assert any("WebSocket /ws" in line for line in records), "control: the socket was logged at all"
        assert not [line for line in records if ticket in line]


PUBLIC_ROUTES = {"/health", "/", "/index.html", "/admin", "/admin.html", "/admin/api/login", "/admin/api/logout"}

# Every file the static mount may serve anonymously. A file dropped into dashboard/static/ that
# is not listed here fails the suite. tailwind.css is a build output, present only once built.
STATIC_ALLOWLIST = {
    "index.html",
    "dashboard.js",
    "admin.js",
    "tailwind.css",
    "fonts/jura-latin-300-normal.woff2",
    "fonts/jura-latin-400-normal.woff2",
    "fonts/license.txt",
    "fonts/source.json",
    "brand/app-icon-192.svg",
    "brand/app-icon-512.svg",
    "brand/avatar.svg",
    "brand/banner-dark.svg",
    "brand/banner-light.svg",
    "brand/favicon.svg",
    "brand/icon-dark.svg",
    "brand/icon-light.svg",
    "brand/og-image.svg",
    "brand/site.webmanifest",
    "brand/social-banner.svg",
    "brand/source.json",
    "brand/tokens.css",
}


def _flat_routes(routes: list[Any], prefix: str = "") -> list[tuple[str, Any]]:
    """(path, route) for every route, descending into routers included with include_router."""
    flat: list[tuple[str, Any]] = []
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            flat += _flat_routes(included.routes, prefix + getattr(route.include_context, "prefix", ""))
        else:
            flat.append((prefix + getattr(route, "path", ""), route))
    return flat


class TestEveryRouteIsGated:
    """A route added later without the session gate fails here, whatever it is."""

    def test_the_test_app_serves_exactly_the_production_routes(self) -> None:
        from dashboard.dashboard import app as production

        assert [getattr(route, "path", None) for route in create_test_app().routes] == [getattr(route, "path", None) for route in production.routes]

    def test_every_route_outside_the_public_allowlist_refuses_an_anonymous_caller(self, client: TestClient) -> None:
        from fastapi.routing import APIRoute, APIWebSocketRoute
        from starlette.routing import Mount

        from dashboard.dashboard import app as production

        probed: list[str] = []
        for path, route in _flat_routes(production.routes):
            if isinstance(route, Mount):
                continue  # the static shell assets; covered below
            concrete = re.sub(r"\{[^}]+\}", "probe", path)
            if isinstance(route, APIWebSocketRoute):
                assert _close_code(client, concrete) == WS_CLOSE_UNAUTHENTICATED, path
                probed.append(path)
                continue
            assert isinstance(route, APIRoute), path
            if path in PUBLIC_ROUTES:
                continue
            for method in sorted(route.methods - {"HEAD"}):
                response = client.request(method, concrete, headers=CONSOLE)
                assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
                assert response.json()["reason"] == "missing", f"{method} {path}"
                probed.append(f"{method} {path}")
        assert "GET /api/metrics" in probed and "GET /admin/api/storage" in probed and "/ws" in probed

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
    def test_api_documentation_is_not_served(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 404

    def test_the_static_mount_serves_only_the_allowlisted_files(self, client: TestClient) -> None:
        from starlette.routing import Mount

        from dashboard.dashboard import app as production

        mounts = [route for _path, route in _flat_routes(production.routes) if isinstance(route, Mount)]
        assert len(mounts) == 1
        directory = Path(mounts[0].app.directory)
        served = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        assert served <= STATIC_ALLOWLIST, f"unlisted files would be public: {sorted(served - STATIC_ALLOWLIST)}"
        assert served >= STATIC_ALLOWLIST - {"tailwind.css"}
        for name in served:
            assert client.get(f"/{name}").status_code == 200, name
        assert client.get("/api").status_code == 404

    def test_anonymous_gated_proxy_calls_never_reach_catalog_api(self, client: TestClient) -> None:
        with patch("dashboard.admin_proxy._proxy_request") as proxied:
            assert client.get("/admin/api/extractions").status_code == 401
            assert client.post("/admin/api/dlq/purge/q", headers=CONSOLE).status_code == 401
        proxied.assert_not_called()


def _flood(websocket: Any, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            websocket.send_text("x")
        except Exception:
            return


def _receive_until_closed(websocket: Any, within_seconds: float) -> WebSocketDisconnect:
    """Drain frames until the server closes; fail instead of hanging if it never does."""
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        try:
            websocket.receive_text()  # the 2-second broadcast keeps this from blocking for long
        except WebSocketDisconnect as closed:
            return closed
    pytest.fail(f"the server kept the socket open for {within_seconds} s")


class TestOpenSocketRechecks:
    """An open socket re-validates on a fixed schedule that client frames cannot postpone."""

    def test_a_flooding_client_is_still_closed_when_its_session_is_revoked(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        with patch("dashboard.dashboard._WS_SESSION_RECHECK_SECONDS", 0.2), client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            assert websocket.receive_json()["type"] == "metrics_update"
            # Revoked upstream, but still in the console's positive cache: the re-check must not trust it.
            fake_catalog.revoke(token)
            assert gate.cache.get(session_auth.token_digest(token)) is not None
            stop = threading.Event()
            flooder = threading.Thread(target=_flood, args=(websocket, stop), daemon=True)
            flooder.start()
            started = time.monotonic()
            try:
                closed = _receive_until_closed(websocket, within_seconds=5)
            finally:
                stop.set()
                flooder.join(timeout=5)
        assert closed.code == WS_CLOSE_UNAUTHENTICATED
        assert time.monotonic() - started < 3

    def test_a_flooding_client_is_still_closed_when_its_session_expires(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint(lifetime_seconds=1.0)
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        with client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            stop = threading.Event()
            flooder = threading.Thread(target=_flood, args=(websocket, stop), daemon=True)
            flooder.start()
            try:
                closed = _receive_until_closed(websocket, within_seconds=5)
            finally:
                stop.set()
                flooder.join(timeout=5)
        assert closed.code == WS_CLOSE_UNAUTHENTICATED

    def test_rechecks_ask_catalog_api_instead_of_the_cache(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        calls_before_open = fake_catalog.session_calls
        with patch("dashboard.dashboard._WS_SESSION_RECHECK_SECONDS", 0.1), client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            websocket.receive_json()
            time.sleep(0.5)
        # One fresh check on open plus several scheduled ones, none served from the warm cache.
        assert fake_catalog.session_calls - calls_before_open >= 3


class TestTicketsDieWithTheSession:
    def test_a_ticket_minted_before_logout_is_refused(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]

        async def upstream_logout(*_args: Any, **_kwargs: Any) -> httpx.Response:
            fake_catalog.revoke(token)
            return httpx.Response(200, json={"logged_out": True})

        with patch("dashboard.admin_proxy._proxy_request", side_effect=upstream_logout):
            assert client.post("/admin/api/logout", headers=_bearer(token)).status_code == 200

        assert len(gate.tickets) == 0, "logout purges the bearer's outstanding tickets"
        assert _close_code(client, f"/ws?ticket={ticket}") == WS_CLOSE_UNAUTHENTICATED

    def test_a_ticket_whose_bearer_was_revoked_elsewhere_is_refused(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        """No logout through the console, so the ticket survives and the cache is warm; redeem still asks catalog-api."""
        token = fake_catalog.mint()
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        fake_catalog.revoke(token)
        assert gate.cache.get(session_auth.token_digest(token)) is not None
        assert _close_code(client, f"/ws?ticket={ticket}") == WS_CLOSE_UNAUTHENTICATED

    def test_purge_drops_only_that_bearers_tickets(self) -> None:
        store = TicketStore()
        mine, _ = store.issue(_session(), "mine")
        theirs, _ = store.issue(_session(), "theirs")
        store.purge(session_auth.token_digest("mine"))
        assert store.redeem(mine) is None
        assert store.redeem(theirs) is not None


class TestCacheSecondsConfiguration:
    @pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "thirty"])
    def test_a_non_finite_or_non_numeric_value_fails_startup(self, raw: str) -> None:
        with pytest.raises(ValueError):
            session_auth.cache_seconds_from_env(raw)

    @pytest.mark.parametrize(("raw", "expected"), [(None, 30.0), ("", 30.0), ("5", 5.0), ("0", 0.0), ("300", 300.0)])
    def test_finite_values_are_accepted_and_the_cache_clamps_them(self, raw: str | None, expected: float) -> None:
        assert session_auth.cache_seconds_from_env(raw) == expected
        assert SessionCache(expected).ttl_seconds == min(expected, 30.0)

    def test_the_cache_itself_refuses_a_non_finite_ttl(self) -> None:
        with pytest.raises(ValueError):
            SessionCache(float("nan"))

    def test_the_console_reads_the_setting_through_the_finite_check(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "dashboard" / "dashboard.py").read_text()
        assert 'cache_seconds=cache_seconds_from_env(os.getenv("SESSION_CACHE_SECONDS"))' in source


class TestUpstreamAmplification:
    def test_checks_share_one_client_for_the_lifespan(self, fake_catalog: FakeCatalogApi) -> None:
        with TestClient(create_test_app()) as client:
            shared = gate._client
            assert shared is not None
            for _ in range(3):
                client.get("/api/metrics", headers=_bearer(fake_catalog.mint()))
            assert gate._client is shared
        assert gate._client is None
        assert shared.is_closed

    def test_the_same_dead_bearer_is_not_rechecked_for_a_while(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        for _ in range(5):
            assert client.get("/api/metrics", headers=_bearer("dead-bearer")).status_code == 401
        assert fake_catalog.session_calls == 1

    def test_junk_bearers_never_lock_out_a_fresh_sign_in(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        """Junk passes through 1:1 (catalog-api is reachable directly anyway); nobody is locked out by address."""
        for index in range(50):
            assert client.get("/api/metrics", headers=_bearer(f"junk-{index}")).status_code == 401
        assert fake_catalog.session_calls == 50
        fresh = fake_catalog.mint()
        assert client.get("/api/session", headers=_bearer(fresh)).status_code == 200
        assert client.post("/api/ws-ticket", headers=_bearer(fresh)).status_code == 200

    def test_the_shared_client_has_a_bounded_pool(self, fake_catalog: FakeCatalogApi) -> None:
        built: list[dict[str, Any]] = []
        real = session_auth.AsyncClient

        def capture(**kwargs: Any) -> httpx.AsyncClient:
            built.append(kwargs)
            return real(**kwargs)

        with patch("dashboard.session_auth.AsyncClient", side_effect=capture), TestClient(create_test_app()):
            pass
        limits = built[0]["limits"]
        assert limits.max_connections == 16
        assert limits.max_keepalive_connections == 8
        assert built[0]["timeout"] == 5.0

    def test_an_invalid_answer_is_reused_only_briefly(self) -> None:
        now = [1000.0]
        answers = session_auth.InvalidAnswers(10, monotonic=lambda: now[0])
        answers.add("digest")
        assert answers.holds("digest")
        now[0] += 10.1
        assert not answers.holds("digest")
        assert len(answers) == 0

    def test_invalid_answers_are_bounded(self) -> None:
        answers = session_auth.InvalidAnswers(10, max_entries=2)
        for key in ("a", "b", "c"):
            answers.add(key)
        assert len(answers) == 2
        assert not answers.holds("a")

    def test_unavailable_is_never_remembered(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        fake_catalog.outage = True
        assert client.get("/api/metrics", headers=_bearer(token)).status_code == 503
        assert len(gate.invalid) == 0


class TestLogoutAlwaysReachesCatalogApi:
    def test_logout_is_forwarded_even_when_the_console_cannot_confirm_the_session(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        fake_catalog.outage = True  # the console's own check would say "unavailable"
        with patch("dashboard.admin_proxy._proxy_request", return_value=httpx.Response(200, json={"logged_out": True})) as proxied:
            assert client.post("/admin/api/logout", headers=_bearer(token)).status_code == 200
        proxied.assert_called_once()

    def test_logout_drops_cached_state_even_when_catalog_api_fails(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        client.post("/api/ws-ticket", headers=_bearer(token))
        assert gate.cache.get(session_auth.token_digest(token)) is not None
        with patch("dashboard.admin_proxy._proxy_request", side_effect=httpx.ConnectError("down")), pytest.raises(httpx.ConnectError):
            client.post("/admin/api/logout", headers=_bearer(token))
        assert gate.cache.get(session_auth.token_digest(token)) is None
        assert len(gate.tickets) == 0


class TestUnavailableClosesWith4503:
    def test_a_socket_opened_during_an_outage_closes_with_4503(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        fake_catalog.outage = True
        assert client.get("/api/session", headers=_bearer(token)).status_code == 200, "the cache still vouches for it"
        assert _close_code(client, f"/ws?ticket={ticket}") == session_auth.WS_CLOSE_UNAVAILABLE

    def test_a_recheck_during_an_outage_closes_with_4503(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        with patch("dashboard.dashboard._WS_SESSION_RECHECK_SECONDS", 0.1), client.websocket_connect(f"/ws?ticket={ticket}") as websocket:
            websocket.receive_json()
            fake_catalog.outage = True
            closed = _receive_until_closed(websocket, within_seconds=5)
        assert closed.code == session_auth.WS_CLOSE_UNAVAILABLE

    def test_a_dead_session_still_closes_with_4401(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        token = fake_catalog.mint()
        ticket = client.post("/api/ws-ticket", headers=_bearer(token)).json()["ticket"]
        fake_catalog.revoke(token)
        assert _close_code(client, f"/ws?ticket={ticket}") == WS_CLOSE_UNAUTHENTICATED


# The default, Secure cookie; the plain name exists only for SESSION_COOKIE_SECURE=false.
COOKIE = session_auth.SECURE_SESSION_COOKIE
PLAIN_COOKIE = session_auth.SESSION_COOKIE


@pytest.fixture
def proxied_catalog(fake_catalog: FakeCatalogApi) -> Iterator[FakeCatalogApi]:
    """Send the admin proxy's calls (login, logout, extractions) to the in-process fake too."""
    real = httpx.AsyncClient

    def client_for_fake(**kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.ASGITransport(app=fake_catalog.app), timeout=kwargs.get("timeout"))

    with patch("dashboard.admin_proxy.httpx.AsyncClient", side_effect=client_for_fake):
        yield fake_catalog


def _sign_in(client: TestClient) -> str:
    """Sign in through the console as the browser does; return the bearer the cookie now holds."""
    response = client.post("/admin/api/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=CONSOLE)
    assert response.status_code == 200, response.text
    return client.cookies[COOKIE]


def _set_cookies(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _clears(response: httpx.Response, name: str = COOKIE) -> bool:
    return any(header.startswith(f"{name}=") and "Max-Age=0" in header for header in _set_cookies(response))


class TestCookieSession:
    def test_login_keeps_the_bearer_in_an_httponly_strict_cookie_and_out_of_the_body(
        self, client: TestClient, proxied_catalog: FakeCatalogApi
    ) -> None:
        response = client.post("/admin/api/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=CONSOLE)

        assert response.status_code == 200
        assert response.json() == {"expires_in": 3600}
        assert response.headers["cache-control"] == "no-store"
        [cookie] = _set_cookies(response)
        token = cookie.split(";", 1)[0].removeprefix(f"{COOKIE}=")
        assert token in proxied_catalog.tokens, "the cookie carries the bearer catalog-api issued"
        assert token not in response.text
        attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
        assert {"httponly", "samesite=strict", "path=/", "max-age=3600", "secure"} <= attributes
        assert not any(part.startswith("domain=") for part in attributes), "host-only, as __Host- requires"

    def test_a_failed_login_sets_no_cookie_and_keeps_an_existing_session(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        token = _sign_in(client)
        response = client.post("/admin/api/login", json={"email": ADMIN_EMAIL, "password": "wrong"}, headers=CONSOLE)
        assert response.status_code == 401
        assert not _set_cookies(response)
        assert client.get("/api/session").status_code == 200
        assert client.cookies[COOKIE] == token

    def test_every_data_route_the_proxy_and_the_live_feed_work_from_the_cookie(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        _sign_in(client)
        for path in DATA_ROUTES:
            assert client.get(path).status_code == 200, path
        # The fake answers 200 only for a live bearer, so this proves the proxy attached it.
        assert client.get("/admin/api/extractions").json() == {"extractions": []}
        ticket = client.post("/api/ws-ticket", headers=CONSOLE)
        assert ticket.status_code == 200
        with client.websocket_connect(f"/ws?ticket={ticket.json()['ticket']}") as websocket:
            assert websocket.receive_json()["type"] == "metrics_update"

    def test_the_websocket_ignores_the_cookie(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        _sign_in(client)
        assert _close_code(client, "/ws") == WS_CLOSE_UNAUTHENTICATED

    def test_an_explicit_header_wins_over_the_cookie(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        _sign_in(client)
        revoked = proxied_catalog.mint()
        proxied_catalog.revoke(revoked)
        response = client.get("/api/session", headers=_bearer(revoked))
        assert response.json()["reason"] == "invalid"
        assert not _set_cookies(response), "a header-authenticated request never touches the cookie"
        assert client.get("/api/session").status_code == 200

    def test_a_session_catalog_api_calls_invalid_clears_the_cookie(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        token = _sign_in(client)
        gate.cache.evict(session_auth.token_digest(token))
        proxied_catalog.revoke(token)
        response = client.get("/api/session")
        assert response.json()["reason"] == "invalid"
        assert _clears(response)
        assert COOKIE not in client.cookies
        assert client.get("/api/session").json()["reason"] == "missing"

    def test_an_unanswered_session_keeps_the_cookie(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        token = _sign_in(client)
        gate.cache.evict(session_auth.token_digest(token))
        proxied_catalog.outage = True
        response = client.get("/api/session")
        assert response.status_code == 503
        assert not _set_cookies(response)
        proxied_catalog.outage = False
        assert client.get("/api/session").status_code == 200

    def test_a_401_from_catalog_api_behind_the_proxy_clears_the_cookie(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        token = _sign_in(client)
        assert client.get("/api/session").status_code == 200  # cached for the next request
        proxied_catalog.revoke(token)
        response = client.get("/admin/api/extractions")
        assert response.status_code == 401
        assert _clears(response)
        assert gate.cache.get(session_auth.token_digest(token)) is None

    @pytest.mark.parametrize("outage", [False, True])
    def test_logout_clears_the_cookie_whatever_catalog_api_answers(self, client: TestClient, proxied_catalog: FakeCatalogApi, outage: bool) -> None:
        token = _sign_in(client)
        proxied_catalog.outage = outage
        response = client.post("/admin/api/logout", headers=CONSOLE)
        assert response.status_code == (503 if outage else 200)
        assert _clears(response)
        assert len([header for header in _set_cookies(response) if header.startswith(f"{COOKIE}=")]) == 1
        assert proxied_catalog.tokens[token].revoked is not outage
        assert gate.cache.get(session_auth.token_digest(token)) is None

    def test_signing_out_a_header_bearer_leaves_the_cookie_session_alone(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        _sign_in(client)
        response = client.post("/admin/api/logout", headers=_bearer(proxied_catalog.mint()))
        assert response.status_code == 200
        assert not _set_cookies(response)
        assert client.get("/api/session").status_code == 200

    def test_a_login_answer_without_a_usable_bearer_is_refused(self, client: TestClient) -> None:
        with patch("dashboard.admin_proxy._proxy_request", return_value=JSONResponse({"access_token": "not a token"})):
            response = client.post("/admin/api/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=CONSOLE)
        assert response.status_code == 502
        assert not _set_cookies(response)


class TestSecureCookie:
    @pytest.fixture(autouse=True)
    def _restore_mode(self) -> Iterator[None]:
        saved = session_auth.session_cookie.secure
        yield
        session_auth.session_cookie.secure = saved

    def _login(self, client: TestClient) -> httpx.Response:
        return client.post("/admin/api/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=CONSOLE)

    def test_the_cookie_is_secure_by_default_whatever_scheme_the_console_sees(self, proxied_catalog: FakeCatalogApi) -> None:
        """Behind a TLS-terminating proxy the console sees plain HTTP; the cookie is Secure anyway."""
        assert session_auth.SessionCookie().secure is True
        with TestClient(create_test_app(), base_url="http://console.test") as plain:
            [cookie] = _set_cookies(self._login(plain))
        assert cookie.startswith(f"{COOKIE}=")
        assert "secure" in {part.strip().lower() for part in cookie.split(";")}

    def test_a_cookie_without_the_host_prefix_is_not_a_session(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        """A sibling subdomain or a plain-HTTP response could plant the plain name; only `__Host-` counts."""
        client.cookies.set(PLAIN_COOKIE, proxied_catalog.mint())
        assert client.get("/api/session").json()["reason"] == "missing"

    def test_only_an_explicit_opt_out_drops_secure(self, proxied_catalog: FakeCatalogApi) -> None:
        session_auth.session_cookie.secure = False
        with TestClient(create_test_app(), base_url="http://console.test") as plain:
            [cookie] = _set_cookies(self._login(plain))
            assert cookie.startswith(f"{PLAIN_COOKIE}=")
            assert "secure" not in {part.strip().lower() for part in cookie.split(";")}
            assert plain.get("/api/session").status_code == 200
            assert _clears(plain.post("/admin/api/logout", headers=CONSOLE), PLAIN_COOKIE)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, True),
            ("", True),
            (" ", True),
            ("true", True),
            ("1", True),
            ("yes", True),
            ("false", False),
            (" FALSE ", False),
            ("0", False),
            ("no", False),
        ],
    )
    def test_the_setting_parses_and_defaults_to_secure(self, raw: str | None, expected: bool) -> None:
        assert session_auth.cookie_secure_from_env(raw) is expected

    @pytest.mark.parametrize("raw", ["auto", "on", "secure", "maybe"])
    def test_an_unknown_setting_fails_startup(self, raw: str) -> None:
        with pytest.raises(ValueError):
            session_auth.cookie_secure_from_env(raw)

    def test_the_console_reads_the_setting(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "dashboard" / "dashboard.py").read_text()
        assert 'session_cookie.secure = cookie_secure_from_env(os.getenv("SESSION_COOKIE_SECURE"))' in source


def _unsafe_routes() -> list[tuple[str, str]]:
    from fastapi.routing import APIRoute

    from dashboard.dashboard import app as production

    return [
        (method, re.sub(r"\{[^}]+\}", "probe", path))
        for path, route in _flat_routes(production.routes)
        if isinstance(route, APIRoute)
        for method in sorted(route.methods & {"POST", "PUT", "PATCH", "DELETE"})
    ]


class TestCrossSiteRequestsAreRefused:
    def test_every_state_changing_route_is_probed(self) -> None:
        probed = {path for _method, path in _unsafe_routes()}
        assert {"/admin/api/login", "/admin/api/logout", "/api/ws-ticket", "/admin/api/dlq/purge/probe", "/admin/api/extractions/trigger"} <= probed

    @pytest.mark.parametrize(
        "headers",
        [
            {},  # a cross-site form: it cannot set a custom header
            {"Sec-Fetch-Site": "cross-site", **CONSOLE},  # and the browser says where it came from
        ],
        ids=["no-console-header", "cross-site"],
    )
    def test_a_cookie_alone_never_authorizes_a_change(self, client: TestClient, proxied_catalog: FakeCatalogApi, headers: dict[str, str]) -> None:
        _sign_in(client)
        calls_before = proxied_catalog.session_calls
        with patch("dashboard.admin_proxy._proxy_request") as proxied:
            for method, path in _unsafe_routes():
                response = client.request(method, path, headers=headers, content=b"source=discogs")
                assert response.status_code == 403, f"{method} {path} answered {response.status_code}"
        proxied.assert_not_called()
        assert proxied_catalog.session_calls == calls_before, "refused before the session gate asks catalog-api"
        assert client.get("/api/session").status_code == 200, "the refusal leaves the session alone"

    def test_a_cross_site_form_post_cannot_purge_a_dlq(self, client: TestClient, proxied_catalog: FakeCatalogApi) -> None:
        _sign_in(client)
        form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"}
        with patch("dashboard.admin_proxy._proxy_request") as proxied:
            response = client.post("/admin/api/dlq/purge/groovemap-discogs-graphinator-artists.dlq", headers=form, content=b"x=1")
        assert response.status_code == 403
        proxied.assert_not_called()

    @pytest.mark.parametrize("site", [None, "same-origin"])
    def test_the_consoles_own_requests_pass(self, client: TestClient, proxied_catalog: FakeCatalogApi, site: str | None) -> None:
        _sign_in(client)
        headers = {**CONSOLE, **({"Sec-Fetch-Site": site} if site else {})}
        assert client.post("/api/ws-ticket", headers=headers).status_code == 200

    def test_a_client_with_its_own_bearer_needs_no_console_header(self, client: TestClient, fake_catalog: FakeCatalogApi) -> None:
        assert client.post("/api/ws-ticket", headers=_bearer(fake_catalog.mint())).status_code == 200


GRAFANA = "http://localhost:3000"  # in development, the Grafana the console links to


class TestOtherOriginsCannotActWithTheSession:
    """Even an origin trusted in CORS_ORIGINS cannot make a signed-in browser change anything."""

    @pytest.fixture
    def cors_client(self, proxied_catalog: FakeCatalogApi) -> Iterator[TestClient]:
        app = create_test_app()
        install_cors(app, [GRAFANA])
        with TestClient(app, base_url="https://console.test") as with_cors:
            yield with_cors

    @pytest.mark.parametrize(
        "browser",
        [
            {"Origin": GRAFANA, "Sec-Fetch-Site": "same-site"},  # current browsers: another port on the same host
            {"Origin": GRAFANA},  # a browser too old to send Sec-Fetch-Site
            {"Origin": "null"},  # a sandboxed frame or a redirect chain
        ],
        ids=["same-site", "origin-only", "opaque-origin"],
    )
    def test_a_credentialed_request_from_an_allowed_cors_origin_is_refused(
        self, cors_client: TestClient, proxied_catalog: FakeCatalogApi, browser: dict[str, str]
    ) -> None:
        _sign_in(cors_client)
        with patch("dashboard.admin_proxy._proxy_request") as proxied:
            for method, path in _unsafe_routes():
                response = cors_client.request(method, path, headers={**CONSOLE, **browser})
                assert response.status_code == 403, f"{method} {path} answered {response.status_code}"
                assert "access-control-allow-credentials" not in response.headers
        proxied.assert_not_called()

    def test_the_preflight_admits_neither_credentials_nor_the_console_header(self, cors_client: TestClient) -> None:
        preflight = cors_client.options(
            "/api/ws-ticket",
            headers={"Origin": GRAFANA, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-groovemap-console"},
        )
        assert preflight.status_code == 400
        assert "access-control-allow-credentials" not in preflight.headers
        assert "x-groovemap-console" not in preflight.headers.get("access-control-allow-headers", "").lower()

    def test_a_cross_origin_read_is_never_credentialed(self, cors_client: TestClient) -> None:
        _sign_in(cors_client)
        response = cors_client.get("/api/session", headers={"Origin": GRAFANA})
        assert response.headers["access-control-allow-origin"] == GRAFANA
        assert "access-control-allow-credentials" not in response.headers, "the browser must not hand the answer to the page"

    def test_without_cors_origins_no_cors_is_served(self) -> None:
        from dashboard.dashboard import app as production

        assert not [entry for entry in production.user_middleware if entry.cls is CORSMiddleware]

    @pytest.mark.parametrize(
        "browser",
        [{"Sec-Fetch-Site": "same-origin", "Origin": "https://console.test"}, {"Origin": "https://console.test"}, {"Origin": "http://console.test"}],
        ids=["same-origin", "origin-only", "origin-scheme-ignored"],
    )
    def test_the_consoles_own_origin_passes(self, client: TestClient, proxied_catalog: FakeCatalogApi, browser: dict[str, str]) -> None:
        _sign_in(client)
        assert client.post("/api/ws-ticket", headers={**CONSOLE, **browser}).status_code == 200
