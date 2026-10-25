"""Tests for admin proxy router."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx as httpx_mod
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.admin_proxy import _auth_headers, _validate_path_segment, _validated_json_body, configure, public_router, router
from tests.fake_catalog_api import FakeCatalogApi


if TYPE_CHECKING:
    from collections.abc import Iterator


def _mock_httpx(method: str = "post", status: int = 200, content: bytes = b"{}") -> tuple[AsyncMock, AsyncMock]:
    """Return (mock_cls, mock_instance) for patching httpx.AsyncClient."""
    mock_resp = AsyncMock()
    mock_resp.status_code = status
    mock_resp.content = content

    mock_instance = AsyncMock()
    setattr(mock_instance, method, AsyncMock(return_value=mock_resp))
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)

    mock_cls = AsyncMock(return_value=mock_instance)
    return mock_cls, mock_instance


def _mock_httpx_error(method: str = "post") -> tuple[AsyncMock, AsyncMock]:
    """Return mocks that raise ConnectError."""
    mock_instance = AsyncMock()
    setattr(mock_instance, method, AsyncMock(side_effect=httpx_mod.ConnectError("refused")))
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)

    mock_cls = AsyncMock(return_value=mock_instance)
    return mock_cls, mock_instance


@pytest.fixture
def proxy_app() -> FastAPI:
    app = FastAPI()
    app.include_router(public_router)
    app.include_router(router)
    configure("localhost", 8004)
    return app


@pytest.fixture
def proxy_client(proxy_app: FastAPI) -> TestClient:
    return TestClient(proxy_app)


# ---------------------------------------------------------------------------
# configure + helpers
# ---------------------------------------------------------------------------


class TestConfigure:
    def test_sets_base_url(self) -> None:
        configure("myhost", 9999)
        from dashboard.admin_proxy import _api_base_url

        assert _api_base_url == "http://myhost:9999"
        # Reset for other tests
        configure("localhost", 8004)


class TestValidatePathSegment:
    def test_valid_uuid(self) -> None:
        assert _validate_path_segment("550e8400-e29b-41d4-a716-446655440000") is True

    def test_valid_alphanumeric(self) -> None:
        assert _validate_path_segment("graphinator-artists-dlq") is True

    def test_rejects_slashes(self) -> None:
        assert _validate_path_segment("../etc/passwd") is False

    def test_allows_dots(self) -> None:
        # Dots are allowed to support version strings like "20240101.0"
        assert _validate_path_segment("foo.bar") is True

    def test_rejects_empty(self) -> None:
        assert _validate_path_segment("") is False

    def test_rejects_spaces(self) -> None:
        assert _validate_path_segment("foo bar") is False

    def test_rejects_dot_dot_dot_segment(self) -> None:
        """Regression: a pure ``..`` segment matches the
        alphanumeric-plus-dot allowlist regex, but httpx/RFC 3986 URL normalization
        collapses it during base_url merge — silently retargeting the request to a
        sibling endpoint. Must be rejected explicitly."""
        assert _validate_path_segment("..") is False

    def test_rejects_single_dot_segment(self) -> None:
        assert _validate_path_segment(".") is False


class TestAuthHeaders:
    """_auth_headers must set trustworthy X-Forwarded-For/-Proto
    from what this service observed, never copy a client-supplied value — otherwise
    an attacker could spoof their apparent IP to the API and defeat the admin
    login rate limiter (api/routers/admin.py 5/minute)."""

    def test_sets_x_forwarded_for_from_the_real_peer(self) -> None:
        from starlette.requests import Request

        scope = {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"1.2.3.4"), (b"authorization", b"Bearer tok")],
            "client": ("10.0.0.9", 12345),
            "scheme": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
        }
        request = Request(scope)
        headers = _auth_headers(request)

        assert headers["X-Forwarded-For"] == "10.0.0.9"
        assert headers["X-Forwarded-For"] != "1.2.3.4"
        assert headers["Authorization"] == "Bearer tok"

    def test_sets_x_forwarded_proto(self) -> None:
        from starlette.requests import Request

        scope = {
            "type": "http",
            "headers": [],
            "client": ("10.0.0.9", 12345),
            "scheme": "https",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "server": ("testserver", 443),
        }
        request = Request(scope)
        headers = _auth_headers(request)

        assert headers["X-Forwarded-Proto"] == "https"


# ---------------------------------------------------------------------------
# POST /admin/api/login
# ---------------------------------------------------------------------------


class TestLoginProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_login_with_body(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _mock_cls, mock_instance = _mock_httpx("post", 200, b'{"access_token":"tok","token_type":"bearer","expires_in":1800}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/login", json={"email": "a@b.com", "password": "testtest"})
        assert resp.status_code == 200
        assert resp.json() == {"expires_in": 1800}, "the bearer goes into the cookie, never the body"
        assert "__Host-groovemap_console_session=tok;" in resp.headers["set-cookie"]
        mock_instance.post.assert_called_once()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_login_without_body(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _mock_cls, mock_instance = _mock_httpx("post", 422, b'{"detail":"body required"}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/login")
        assert resp.status_code == 422

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_login_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("post")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/login", json={"email": "a@b.com", "password": "x"})
        assert resp.status_code == 502
        assert "unavailable" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /admin/api/logout
# ---------------------------------------------------------------------------


class TestLogoutProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_logout(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 200, b'{"logged_out":true}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/logout", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert resp.json()["logged_out"] is True

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_logout_forwards_auth_header(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 200, b'{"logged_out":true}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.post("/admin/api/logout", headers={"Authorization": "Bearer mytoken"})
        call_kwargs = mock_instance.post.call_args
        assert "Bearer mytoken" in str(call_kwargs)

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_logout_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("post")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/logout")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# GET /admin/api/extractions
# ---------------------------------------------------------------------------


class TestListExtractionsProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_list(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"extractions":[],"total":0}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extractions", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_list_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("get")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extractions")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# GET /admin/api/extractions/{id}
# ---------------------------------------------------------------------------


class TestGetExtractionProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_get(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"id":"abc-123","status":"completed"}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extractions/abc-123", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    def test_rejects_invalid_id(self, proxy_client: TestClient) -> None:
        resp = proxy_client.get("/admin/api/extractions/../../../etc/passwd")
        assert resp.status_code in (400, 404)  # FastAPI may reject before handler

    def test_allows_id_with_dots(self, proxy_client: TestClient) -> None:
        # Dots are now allowed in path segments (needed for version strings)
        # foo.bar passes validation; the upstream API returns the actual status
        # We just verify the proxy doesn't reject it at the validation layer.
        # Without a mock the upstream call fails with a connection error → 502.
        resp = proxy_client.get("/admin/api/extractions/foo.bar")
        assert resp.status_code != 400

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_get_extraction_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("get")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extractions/abc-123")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /admin/api/extractions/trigger
# ---------------------------------------------------------------------------


class TestTriggerProxy:
    def test_source_outside_the_contract_is_refused(self) -> None:
        """A route cannot be wired to a source catalog-api's contract does not list."""
        from dashboard import admin_proxy

        with pytest.raises(ValueError, match="Unsupported extraction source"):
            asyncio.run(admin_proxy._proxy_trigger_for_source(None, "lastfm"))  # type: ignore[arg-type]

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_trigger(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b'{"id":"abc","status":"running"}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/extractions/trigger", headers={"Authorization": "Bearer mytoken"})
        assert resp.status_code == 202
        call_kwargs = mock_instance.post.call_args
        assert "Bearer mytoken" in str(call_kwargs)

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_trigger_with_body(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b'{"id":"abc","status":"running"}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post(
            "/admin/api/extractions/trigger",
            json={"force_reprocess": True},
            headers={"Authorization": "Bearer tok"},
        )
        assert resp.status_code == 202

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_no_body_sends_discogs_source(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        """An older tab posts no body; catalog-api still gets source=discogs."""
        _, mock_instance = _mock_httpx("post", 202, b'{"id":"abc","status":"running"}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.post("/admin/api/extractions/trigger", headers={"Authorization": "Bearer tok"})
        assert json.loads(mock_instance.post.call_args.kwargs["content"]) == {"source": "discogs"}

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_body_source_discogs_is_forwarded(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b'{"id":"abc","status":"running"}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.post("/admin/api/extractions/trigger", json={"source": "discogs"}, headers={"Authorization": "Bearer tok"})
        assert json.loads(mock_instance.post.call_args.kwargs["content"]) == {"source": "discogs"}

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_conflicting_body_source_returns_400(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b"{}")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/extractions/trigger", json={"source": "musicbrainz"}, headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 400
        mock_instance.post.assert_not_called()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_trigger_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("post")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/extractions/trigger")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /admin/api/dlq/purge/{queue}
# ---------------------------------------------------------------------------


class TestDlqPurgeProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_purge(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 200, b'{"queue":"graphinator-artists-dlq","messages_purged":0}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post(
            "/admin/api/dlq/purge/graphinator-artists-dlq",
            headers={"Authorization": "Bearer tok"},
        )
        assert resp.status_code == 200
        assert resp.json()["queue"] == "graphinator-artists-dlq"

    def test_rejects_invalid_queue_name(self, proxy_client: TestClient) -> None:
        resp = proxy_client.post("/admin/api/dlq/purge/../../etc")
        assert resp.status_code in (400, 404)

    def test_allows_queue_with_dots(self, proxy_client: TestClient) -> None:
        # Dots are now allowed in path segments; the upstream will handle validity.
        # Without a mock the upstream call fails with a connection error → 502.
        resp = proxy_client.post("/admin/api/dlq/purge/bad.queue.name")
        assert resp.status_code != 400

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_purge_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("post")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/dlq/purge/graphinator-artists-dlq")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Auth header forwarding
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Additional path-segment validation tests for lines 145 and 258
# ---------------------------------------------------------------------------


class TestExtractionIdValidationRejectsInvalidChars:
    def test_rejects_extraction_id_with_exclamation(self, proxy_client: TestClient) -> None:
        """proxy_get_extraction returns 400 for extraction_id containing '!' (line 145).

        The '!' character fails _SAFE_PATH_SEGMENT and is not normalised away by FastAPI.
        """
        resp = proxy_client.get("/admin/api/extractions/bad!id")
        assert resp.status_code == 400

    def test_rejects_extraction_id_with_space(self, proxy_client: TestClient) -> None:
        """proxy_get_extraction returns 400 for extraction_id containing a space (line 145)."""
        resp = proxy_client.get("/admin/api/extractions/bad%20id")
        assert resp.status_code == 400


class TestDlqPurgeInvalidQueueValidation:
    def test_rejects_queue_name_with_invalid_chars(self, proxy_client: TestClient) -> None:
        """proxy_dlq_purge returns 400 for queue names containing '!' (line 258).

        The '!' character fails _SAFE_PATH_SEGMENT and is not normalised away by FastAPI.
        """
        resp = proxy_client.post("/admin/api/dlq/purge/bad!queue")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Phase 2 — User Activity & Storage proxy routes
# ---------------------------------------------------------------------------


class TestUserStatsProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_user_stats(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"total_users":5,"active_users":3}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/users/stats", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert resp.json()["total_users"] == 5

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_user_stats_forwards_auth_header(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"total_users":1}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.get("/admin/api/users/stats", headers={"Authorization": "Bearer mytoken"})
        call_kwargs = mock_instance.get.call_args
        assert "Bearer mytoken" in str(call_kwargs)

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_user_stats_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("get")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/users/stats")
        assert resp.status_code == 502
        assert "unavailable" in resp.json()["detail"]


class TestSyncActivityProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_sync_activity(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"activity":[]}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/users/sync-activity", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert "activity" in resp.json()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_sync_activity_forwards_auth_header(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"activity":[]}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.get("/admin/api/users/sync-activity", headers={"Authorization": "Bearer mytoken"})
        call_kwargs = mock_instance.get.call_args
        assert "Bearer mytoken" in str(call_kwargs)

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_sync_activity_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("get")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/users/sync-activity")
        assert resp.status_code == 502
        assert "unavailable" in resp.json()["detail"]


class TestStorageProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_storage(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"neo4j":{"size_bytes":1024},"postgres":{"size_bytes":2048}}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/storage", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert "neo4j" in resp.json()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_storage_forwards_auth_header(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"neo4j":{},"postgres":{}}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.get("/admin/api/storage", headers={"Authorization": "Bearer mytoken"})
        call_kwargs = mock_instance.get.call_args
        assert "Bearer mytoken" in str(call_kwargs)

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_storage_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("get")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/storage")
        assert resp.status_code == 502
        assert "unavailable" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Phase 4 — Audit Log proxy route
# ---------------------------------------------------------------------------


class TestAuditLogProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_proxy_audit_log(self, mock_client_cls: AsyncMock, proxy_client: TestClient) -> None:
        mock_resp = MagicMock()
        mock_resp.content = b'{"entries":[],"total":0,"page":1,"page_size":50}'
        mock_resp.status_code = 200
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_resp)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_instance

        resp = proxy_client.get(
            "/admin/api/audit-log",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_proxy_audit_log_with_params(self, mock_client_cls: AsyncMock, proxy_client: TestClient) -> None:
        """Test that query parameters are forwarded."""
        mock_resp = MagicMock()
        mock_resp.content = b'{"entries":[],"total":0,"page":2,"page_size":25}'
        mock_resp.status_code = 200
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_resp)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_instance

        resp = proxy_client.get(
            "/admin/api/audit-log?page=2&page_size=25&action=dlq.purge&admin_id=abc-123",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        # Verify params were forwarded
        call_args = mock_instance.get.call_args
        params = call_args[1].get("params", {})
        assert params.get("page") == "2"
        assert params.get("page_size") == "25"
        assert params.get("action") == "dlq.purge"
        assert params.get("admin_id") == "abc-123"

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_proxy_audit_log_api_unreachable(self, mock_client_cls: AsyncMock, proxy_client: TestClient) -> None:
        """Test error handling when API is unreachable."""
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(side_effect=httpx_mod.ConnectError("Connection refused"))
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_instance

        resp = proxy_client.get(
            "/admin/api/audit-log",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Identity Ledger proxy routes
# ---------------------------------------------------------------------------

LEDGER_WORK = "00000000-0000-4000-8000-000000000101"


class TestIdentityLedgerProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_works_forwards_every_parameter(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"works":[],"total":0,"page":2,"page_size":25}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get(
            "/admin/api/identity/ledger/works",
            params={"page": "2", "page_size": "25", "event": "link_changed", "since": "2025-11-01T00:00:00Z", "until": "2026-01-01T00:00:00+02:00"},
            headers={"Authorization": "Bearer tok"},
        )

        assert resp.status_code == 200
        assert resp.json()["page"] == 2
        call = mock_instance.get.call_args
        assert call.args[0] == "http://localhost:8004/api/admin/identity/ledger/works"
        assert call.kwargs["params"] == {
            "page": "2",
            "page_size": "25",
            "event": "link_changed",
            "since": "2025-11-01T00:00:00Z",
            "until": "2026-01-01T00:00:00+02:00",
        }
        assert call.kwargs["headers"]["Authorization"] == "Bearer tok"

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_works_forwards_no_parameter_it_was_not_given(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"works":[]}')
        mock_cls_patch.return_value = mock_instance

        assert proxy_client.get("/admin/api/identity/ledger/works").status_code == 200
        assert mock_instance.get.call_args.kwargs["params"] == {}

    @pytest.mark.parametrize(
        "query",
        [
            "page=0",
            "page_size=101",
            "event=minted",
            "event=link_added;drop",
            "since=2025-11-01",
            "since=2025-11-01T00:00:00",
            "until=2025-13-01T00:00:00Z",
            "until=yesterday",
        ],
    )
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_works_rejects_a_malformed_parameter_without_calling_upstream(
        self, mock_cls_patch: AsyncMock, proxy_client: TestClient, query: str
    ) -> None:
        assert proxy_client.get(f"/admin/api/identity/ledger/works?{query}").status_code == 422
        mock_cls_patch.assert_not_called()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_work_forwards_as_of_and_edition_limit(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"work":{},"editions":[]}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get(f"/admin/api/identity/ledger/works/{LEDGER_WORK}", params={"as_of": "2026-09-03T00:00:00.5Z", "edition_limit": "500"})

        assert resp.status_code == 200
        call = mock_instance.get.call_args
        assert call.args[0] == f"http://localhost:8004/api/admin/identity/ledger/works/{LEDGER_WORK}"
        assert call.kwargs["params"] == {"as_of": "2026-09-03T00:00:00.5Z", "edition_limit": "500"}

    @pytest.mark.parametrize(
        "as_of", ["2026-09-03", "2026-09-03T00:00:00", "2026-09-03 00:00:00Z", "2026-09-31T25:00:00Z", "now", "2026-09-03T00:00:00Z&x=1"]
    )
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_work_rejects_a_malformed_as_of(self, mock_cls_patch: AsyncMock, proxy_client: TestClient, as_of: str) -> None:
        resp = proxy_client.get(f"/admin/api/identity/ledger/works/{LEDGER_WORK}", params={"as_of": as_of})
        assert resp.status_code == 422
        mock_cls_patch.assert_not_called()

    @pytest.mark.parametrize("edition_limit", ["0", "501", "many"])
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_work_rejects_an_edition_limit_out_of_range(self, mock_cls_patch: AsyncMock, proxy_client: TestClient, edition_limit: str) -> None:
        resp = proxy_client.get(f"/admin/api/identity/ledger/works/{LEDGER_WORK}", params={"edition_limit": edition_limit})
        assert resp.status_code == 422
        mock_cls_patch.assert_not_called()

    @pytest.mark.parametrize("segment", ["%2e%2e", "%2e", "work%20id", "a%3Bb", "%24%7Bid%7D"])
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_work_rejects_an_unsafe_path_segment(self, mock_cls_patch: AsyncMock, proxy_client: TestClient, segment: str) -> None:
        resp = proxy_client.get(f"/admin/api/identity/ledger/works/{segment}")
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Invalid work ID"}
        mock_cls_patch.assert_not_called()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_work_passes_an_upstream_404_through(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 404, b'{"detail":"Work not found"}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get(f"/admin/api/identity/ledger/works/{LEDGER_WORK}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Work not found"}

    @pytest.mark.parametrize("kind", ["release", "master", "artist", "label"])
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_density_forwards_each_kind(self, mock_cls_patch: AsyncMock, proxy_client: TestClient, kind: str) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"kind":"release","buckets":[]}')
        mock_cls_patch.return_value = mock_instance

        assert proxy_client.get("/admin/api/identity/ledger/density", params={"kind": kind}).status_code == 200
        call = mock_instance.get.call_args
        assert call.args[0] == "http://localhost:8004/api/admin/identity/ledger/density"
        assert call.kwargs["params"] == {"kind": kind}

    @pytest.mark.parametrize("kind", ["releases", "work", "", "release&kind=master"])
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_density_rejects_an_unknown_kind(self, mock_cls_patch: AsyncMock, proxy_client: TestClient, kind: str) -> None:
        assert proxy_client.get("/admin/api/identity/ledger/density", params={"kind": kind}).status_code == 422
        mock_cls_patch.assert_not_called()

    @pytest.mark.parametrize(
        "path", ["/admin/api/identity/ledger/works", f"/admin/api/identity/ledger/works/{LEDGER_WORK}", "/admin/api/identity/ledger/density"]
    )
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_an_unreachable_api_answers_502(self, mock_cls_patch: AsyncMock, proxy_client: TestClient, path: str) -> None:
        _, mock_instance = _mock_httpx_error("get")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get(path)
        assert resp.status_code == 502
        assert "unavailable" in resp.json()["detail"]


class TestIdentityLedgerProxyAgainstTheFakeCatalogApi:
    """The proxy and tests/fake_catalog_api.py agree on the three ledger routes, over ASGI."""

    @pytest.fixture
    def fake_client(self, proxy_client: TestClient) -> Iterator[tuple[TestClient, FakeCatalogApi, str]]:
        fake = FakeCatalogApi()
        token = fake.mint()
        # The proxy's `httpx` is this module's, so the patch below replaces the class both see.
        real_client = httpx_mod.AsyncClient

        def client_for(*_args: object, **kwargs: object) -> httpx_mod.AsyncClient:
            return real_client(transport=httpx_mod.ASGITransport(app=fake.app), timeout=kwargs.get("timeout"))

        with patch("dashboard.admin_proxy.httpx.AsyncClient", side_effect=client_for):
            yield proxy_client, fake, token

    def test_the_works_list_pages(self, fake_client: tuple[TestClient, FakeCatalogApi, str]) -> None:
        client, _fake, token = fake_client
        body = client.get("/admin/api/identity/ledger/works?page=2&page_size=1", headers={"Authorization": f"Bearer {token}"}).json()
        assert body["total"] == 2
        assert body["page"] == 2
        assert [work["gm_id"] for work in body["works"]] == ["00000000-0000-4000-8000-000000000102"]

    def test_one_work_read_as_of_drops_later_events(self, fake_client: tuple[TestClient, FakeCatalogApi, str]) -> None:
        client, _fake, token = fake_client
        headers = {"Authorization": f"Bearer {token}"}
        now = client.get(f"/admin/api/identity/ledger/works/{LEDGER_WORK}", headers=headers).json()
        then = client.get(f"/admin/api/identity/ledger/works/{LEDGER_WORK}?as_of=2026-08-15T00:00:00Z", headers=headers).json()

        assert now["as_of"] is None
        assert [event["type"] for event in now["editions"][0]["events"]] == ["link_added", "minted", "reattached"]
        assert then["as_of"] == "2026-08-15T00:00:00Z"
        assert [event["type"] for event in then["editions"][0]["events"]] == ["link_added", "minted"]
        assert then["editions"][0]["former_items"] == []
        assert client.get("/admin/api/identity/ledger/works/00000000-0000-4000-8000-000000000999", headers=headers).status_code == 404

    def test_the_density_strip_is_served_per_kind_and_can_be_replaced(self, fake_client: tuple[TestClient, FakeCatalogApi, str]) -> None:
        client, fake, token = fake_client
        headers = {"Authorization": f"Bearer {token}"}
        release = client.get("/admin/api/identity/ledger/density", headers=headers).json()
        assert release["kind"] == "release"
        assert {bucket["dated_from"] for bucket in release["buckets"]} == {"groovemap", "musicbrainz_fullexport"}
        assert client.get("/admin/api/identity/ledger/density?kind=label", headers=headers).json() == {"kind": "label", "buckets": []}

        bucket = {"month": "2026-01", "type": "promoted", "dated_from": "groovemap", "events": 1}
        replaced = TestClient(fake.app).post("/__fake__/ledger", json={"density": {"master": [bucket]}})
        assert replaced.status_code == 200
        assert client.get("/admin/api/identity/ledger/density?kind=master", headers=headers).json()["buckets"] == [bucket]

    def test_the_fake_refuses_a_caller_without_a_session(self, fake_client: tuple[TestClient, FakeCatalogApi, str]) -> None:
        client, _fake, _token = fake_client
        assert client.get("/admin/api/identity/ledger/density").status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/api/extractions/trigger-musicbrainz
# ---------------------------------------------------------------------------


class TestTriggerMusicBrainzProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_trigger_musicbrainz(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b'{"id":"abc","status":"running"}')
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/extractions/trigger-musicbrainz", headers={"Authorization": "Bearer mytoken"})
        assert resp.status_code == 202
        call_kwargs = mock_instance.post.call_args
        assert "Bearer mytoken" in str(call_kwargs)

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_injects_source_musicbrainz(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b'{"id":"abc","status":"running"}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.post("/admin/api/extractions/trigger-musicbrainz", headers={"Authorization": "Bearer tok"})
        call_kwargs = mock_instance.post.call_args
        assert '"source":"musicbrainz"' in str(call_kwargs) or b'"source":"musicbrainz"' in call_kwargs.kwargs.get("content", b"")

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_merges_body_with_source_injection(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b'{"id":"abc","status":"running"}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.post(
            "/admin/api/extractions/trigger-musicbrainz",
            json={"force_reprocess": True},
            headers={"Authorization": "Bearer tok"},
        )
        call_kwargs = mock_instance.post.call_args
        content = call_kwargs.kwargs.get("content", b"")
        import json as _json

        body = _json.loads(content)
        assert body["source"] == "musicbrainz"
        assert body["force_reprocess"] is True

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_trigger_musicbrainz_targets_trigger_endpoint(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b'{"id":"abc","status":"running"}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.post("/admin/api/extractions/trigger-musicbrainz", headers={"Authorization": "Bearer tok"})
        call_url = mock_instance.post.call_args[0][0]
        assert call_url.endswith("/api/admin/extractions/trigger")

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_conflicting_body_source_returns_400(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("post", 202, b"{}")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/extractions/trigger-musicbrainz", json={"source": "discogs"}, headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 400
        mock_instance.post.assert_not_called()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_trigger_musicbrainz_unreachable(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error("post")
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.post("/admin/api/extractions/trigger-musicbrainz")
        assert resp.status_code == 502


class TestValidatedJsonBody:
    """Direct unit tests for the validated JSON body boundary."""

    @pytest.mark.asyncio
    async def test_non_utf8_body_raises_json_decode_error(self) -> None:
        """A non-UTF-8 body must raise json.JSONDecodeError, not UnicodeDecodeError,
        so every caller's existing `except json.JSONDecodeError` clause catches it."""
        request = MagicMock()
        request.body = AsyncMock(return_value=b"\xff")

        with pytest.raises(json.JSONDecodeError):
            await _validated_json_body(request)

    @pytest.mark.asyncio
    async def test_malformed_json_raises_json_decode_error(self) -> None:
        request = MagicMock()
        request.body = AsyncMock(return_value=b"{not valid json")

        with pytest.raises(json.JSONDecodeError):
            await _validated_json_body(request)

    @pytest.mark.asyncio
    async def test_empty_body_returns_none(self) -> None:
        request = MagicMock()
        request.body = AsyncMock(return_value=b"")

        assert await _validated_json_body(request) is None

    @pytest.mark.asyncio
    async def test_valid_json_body_reserialised(self) -> None:
        request = MagicMock()
        request.body = AsyncMock(return_value=b'{"a": 1}')

        assert await _validated_json_body(request) == b'{"a":1}'


class TestMalformedJsonBody:
    def test_login_malformed_json_returns_400(self, proxy_client: TestClient) -> None:
        """POST /admin/api/login with malformed JSON body returns 400."""
        resp = proxy_client.post(
            "/admin/api/login",
            content=b"{not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["detail"]

    def test_trigger_malformed_json_returns_400(self, proxy_client: TestClient) -> None:
        """POST /admin/api/extractions/trigger with malformed JSON body returns 400."""
        resp = proxy_client.post(
            "/admin/api/extractions/trigger",
            content=b"{{bad json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["detail"]

    def test_trigger_musicbrainz_malformed_json_returns_400(self, proxy_client: TestClient) -> None:
        """POST /admin/api/extractions/trigger-musicbrainz with malformed JSON returns 400."""
        resp = proxy_client.post(
            "/admin/api/extractions/trigger-musicbrainz",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["detail"]

    def test_trigger_musicbrainz_non_dict_body_returns_400(self, proxy_client: TestClient) -> None:
        """POST /admin/api/extractions/trigger-musicbrainz with JSON array body returns 400."""
        resp = proxy_client.post(
            "/admin/api/extractions/trigger-musicbrainz",
            content=b"[1, 2, 3]",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "JSON object" in resp.json()["detail"]

    def test_login_non_utf8_body_returns_400(self, proxy_client: TestClient) -> None:
        """POST /admin/api/login with a non-UTF-8 body returns 400, not an unhandled 500.

        Regression: json.loads(bytes) raises
        UnicodeDecodeError (a ValueError sibling, not a subclass, of
        json.JSONDecodeError) for invalid UTF-8 bytes such as b"\\xff".
        """
        resp = proxy_client.post(
            "/admin/api/login",
            content=b"\xff",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["detail"]

    def test_trigger_non_utf8_body_returns_400(self, proxy_client: TestClient) -> None:
        """POST /admin/api/extractions/trigger with a non-UTF-8 body returns 400."""
        resp = proxy_client.post(
            "/admin/api/extractions/trigger",
            content=b"{\xff}",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["detail"]

    def test_trigger_musicbrainz_non_utf8_body_returns_400(self, proxy_client: TestClient) -> None:
        """POST /admin/api/extractions/trigger-musicbrainz with a non-UTF-8 body returns 400."""
        resp = proxy_client.post(
            "/admin/api/extractions/trigger-musicbrainz",
            content=b"\x80\x81",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["detail"]


class TestAuthHeaderForwarding:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_no_auth_header_sent_when_absent(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx("get", 200, b'{"extractions":[]}')
        mock_cls_patch.return_value = mock_instance

        proxy_client.get("/admin/api/extractions")
        call_kwargs = mock_instance.get.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert "Authorization" not in headers


class TestExtractionAnalysisTraversalRejected:
    """Regression: a ``..`` path segment previously passed
    `_validate_path_segment` (dots are allowlisted for version strings) and was then
    silently collapsed by httpx/RFC 3986 URL normalization, retargeting the proxied
    request to a sibling `/api/admin/*` endpoint. Use a percent-encoded segment so the
    test client doesn't itself normalize the dot-segment away before the request is
    sent — the same technique an adversary would use against the deployed server."""

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_ea_summary_rejects_dot_dot_version(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        resp = proxy_client.get("/admin/api/extraction-analysis/%2e%2e/summary")
        assert resp.status_code == 400
        mock_cls_patch.assert_not_called()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_ea_compare_rejects_dot_dot_in_either_version(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        resp = proxy_client.get("/admin/api/extraction-analysis/%2e%2e/compare/1.0")
        assert resp.status_code == 400
        resp2 = proxy_client.get("/admin/api/extraction-analysis/1.0/compare/%2e%2e")
        assert resp2.status_code == 400
        mock_cls_patch.assert_not_called()

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_ea_violation_detail_rejects_dot_dot_record_id(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        resp = proxy_client.get("/admin/api/extraction-analysis/1.0/violations/%2e%2e")
        assert resp.status_code == 400
        mock_cls_patch.assert_not_called()
