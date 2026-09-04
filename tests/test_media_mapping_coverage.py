"""Tests for the media mapping coverage composition and proxy route in admin_proxy.py.

The reading is sourced from catalog-api's `admin_unmapped_media` route
(`GET /api/admin/media/unmapped?provider=…&limit=…`) for both providers; these tests
mock that route rather than the data-quality violations route it replaced.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx as httpx_mod
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.admin_proxy import (
    _MEDIA_MAPPING_PROVIDERS,
    _MEDIA_MAPPING_TOP_N,
    _compose_media_mapping_coverage,
    _media_mapping_unavailable,
    configure,
    router,
)


def _unmapped(kind: str, name: str, releases: int) -> dict[str, Any]:
    return {"kind": kind, "name": name, "releases": releases}


def _coverage_payload(
    provider: str = "discogs",
    *,
    media_tagged_releases: int = 100,
    releases_with_unmapped: int = 25,
    unmapped_rate: float = 0.25,
    limit: int = _MEDIA_MAPPING_TOP_N,
    top_unmapped: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "media_tagged_releases": media_tagged_releases,
        "releases_with_unmapped": releases_with_unmapped,
        "unmapped_rate": unmapped_rate,
        "limit": limit,
        "top_unmapped": [_unmapped("format", "Shellac", 15), _unmapped("description", "Hand-Numbered", 10)] if top_unmapped is None else top_unmapped,
    }


# ---------------------------------------------------------------------------
# _compose_media_mapping_coverage — pure shaping logic
# ---------------------------------------------------------------------------


class TestComposeMediaMappingCoverage:
    def test_maps_upstream_counts_onto_the_rendered_shape(self) -> None:
        summary = {"version": "20240101", "source": "discogs"}

        result = _compose_media_mapping_coverage(summary, _coverage_payload())

        assert result["available"] is True
        assert result["version"] == "20240101"
        assert result["source"] == "discogs"
        assert result["provider"] == "discogs"
        assert result["media_tagged_releases"] == 100
        assert result["releases_with_unmapped_media"] == 25
        assert result["unmapped_rate"] == 0.25
        assert result["limit"] == _MEDIA_MAPPING_TOP_N

    def test_top_unmapped_preserves_upstream_order_and_carries_kind(self) -> None:
        """Upstream already orders by releases DESC, kind ASC, name ASC — do not re-sort."""
        top = [
            _unmapped("format", "Shellac", 9),
            _unmapped("description", "Hand-Numbered", 4),
            _unmapped("format", "Betamax", 4),
        ]
        summary = {"version": "v1", "source": "discogs"}

        result = _compose_media_mapping_coverage(summary, _coverage_payload(top_unmapped=top))

        assert result["top_unmapped_formats"] == [
            {"kind": "format", "name": "Shellac", "count": 9},
            {"kind": "description", "name": "Hand-Numbered", "count": 4},
            {"kind": "format", "name": "Betamax", "count": 4},
        ]

    def test_musicbrainz_reading_carries_names_and_counts(self) -> None:
        """The MusicBrainz reading is a real reading now, not a 'not observable' note."""
        summary = {"version": "20240201", "source": "musicbrainz"}
        payload = _coverage_payload(
            "musicbrainz",
            media_tagged_releases=80,
            releases_with_unmapped=12,
            unmapped_rate=0.15,
            top_unmapped=[_unmapped("format", "DualDisc", 7)],
        )

        result = _compose_media_mapping_coverage(summary, payload)

        assert result["available"] is True
        assert result["provider"] == "musicbrainz"
        assert result["releases_with_unmapped_media"] == 12
        assert result["media_tagged_releases"] == 80
        assert result["top_unmapped_formats"] == [{"kind": "format", "name": "DualDisc", "count": 7}]

    def test_empty_top_unmapped_is_not_truncated(self) -> None:
        summary = {"version": "v1", "source": "discogs"}

        result = _compose_media_mapping_coverage(summary, _coverage_payload(top_unmapped=[]))

        assert result["top_unmapped_formats"] == []
        assert result["truncated"] is False

    def test_truncated_when_the_list_fills_the_requested_limit(self) -> None:
        top = [_unmapped("format", f"format-{index}", 1) for index in range(3)]
        summary = {"version": "v1", "source": "discogs"}

        result = _compose_media_mapping_coverage(summary, _coverage_payload(limit=3, top_unmapped=top))

        assert result["truncated"] is True

    def test_zero_denominator_reading_is_reported_as_zero_not_absent(self) -> None:
        summary = {"version": "v1", "source": "musicbrainz"}
        payload = _coverage_payload(
            "musicbrainz",
            media_tagged_releases=0,
            releases_with_unmapped=0,
            unmapped_rate=0.0,
            top_unmapped=[],
        )

        result = _compose_media_mapping_coverage(summary, payload)

        assert result["available"] is True
        assert result["media_tagged_releases"] == 0
        assert result["releases_with_unmapped_media"] == 0
        assert result["unmapped_rate"] == 0.0

    def test_unavailable_reason_names_the_readable_providers(self) -> None:
        result = _media_mapping_unavailable("v1", "bandcamp")

        assert result["available"] is False
        assert result["source"] == "bandcamp"
        for provider in _MEDIA_MAPPING_PROVIDERS:
            assert provider in result["reason"]
        assert "bandcamp" in result["reason"]


# ---------------------------------------------------------------------------
# GET /admin/api/extraction-analysis/{version}/media-mapping-coverage
# ---------------------------------------------------------------------------


def _mock_response(status: int, payload: dict[str, Any]) -> MagicMock:
    content = json.dumps(payload).encode()
    resp = MagicMock()
    resp.status_code = status
    resp.content = content
    resp.json = MagicMock(return_value=payload)
    return resp


def _mock_httpx_sequence(responses: list[MagicMock]) -> tuple[AsyncMock, AsyncMock]:
    """Return (mock_cls, mock_instance) where .get() yields *responses* in call order."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(side_effect=responses)
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)

    mock_cls = AsyncMock(return_value=mock_instance)
    return mock_cls, mock_instance


def _mock_httpx_raising(error: Exception) -> tuple[AsyncMock, AsyncMock]:
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(side_effect=error)
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)

    mock_cls = AsyncMock(return_value=mock_instance)
    return mock_cls, mock_instance


@pytest.fixture
def proxy_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    configure("localhost", 8004)
    return app


@pytest.fixture
def proxy_client(proxy_app: FastAPI) -> TestClient:
    return TestClient(proxy_app)


class TestMediaMappingCoverageProxy:
    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_discogs_reads_the_unmapped_media_route(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs"}
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload), _mock_response(200, _coverage_payload("discogs"))])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get(
            "/admin/api/extraction-analysis/20240101/media-mapping-coverage",
            headers={"Authorization": "Bearer tok"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["source"] == "discogs"
        assert data["provider"] == "discogs"
        assert data["releases_with_unmapped_media"] == 25
        assert data["media_tagged_releases"] == 100
        assert data["unmapped_rate"] == 0.25
        assert mock_instance.get.call_count == 2

        coverage_call = mock_instance.get.call_args_list[1]
        assert coverage_call[0][0].endswith("/api/admin/media/unmapped")
        assert coverage_call[1]["params"] == {"provider": "discogs", "limit": str(_MEDIA_MAPPING_TOP_N)}

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_musicbrainz_reads_the_same_route_with_its_own_provider(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240201", "source": "musicbrainz"}
        coverage = _coverage_payload(
            "musicbrainz",
            media_tagged_releases=80,
            releases_with_unmapped=12,
            unmapped_rate=0.15,
            top_unmapped=[_unmapped("format", "DualDisc", 7), _unmapped("description", "Copy Control", 3)],
        )
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload), _mock_response(200, coverage)])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240201/media-mapping-coverage")

        assert resp.status_code == 200
        data = resp.json()
        # The old reading returned available=false with a "not observable" note here.
        assert data["available"] is True
        assert data["provider"] == "musicbrainz"
        assert data["releases_with_unmapped_media"] == 12
        assert [entry["name"] for entry in data["top_unmapped_formats"]] == ["DualDisc", "Copy Control"]
        assert mock_instance.get.call_count == 2
        assert mock_instance.get.call_args_list[1][1]["params"]["provider"] == "musicbrainz"

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_unknown_source_returns_unavailable_without_calling_upstream(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240301", "source": "bandcamp"}
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload)])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240301/media-mapping-coverage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["source"] == "bandcamp"
        # Only the summary was fetched — no provider to name upstream.
        assert mock_instance.get.call_count == 1

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_summary_non_200_passed_through(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_sequence([_mock_response(404, {"detail": "Version not found"})])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/missing/media-mapping-coverage")

        assert resp.status_code == 404
        assert mock_instance.get.call_count == 1

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_upstream_5xx_from_the_coverage_route_passed_through(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs"}
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload), _mock_response(500, {"detail": "boom"})])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240101/media-mapping-coverage")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "boom"

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_upstream_422_from_the_coverage_route_passed_through(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs"}
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload), _mock_response(422, {"detail": "Invalid provider"})])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240101/media-mapping-coverage")

        assert resp.status_code == 422

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_returns_502_on_connect_error(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_raising(httpx_mod.ConnectError("refused"))
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240101/media-mapping-coverage")

        assert resp.status_code == 502
        assert "unavailable" in resp.json()["detail"]

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_returns_502_when_the_coverage_route_times_out(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs"}
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(side_effect=[_mock_response(200, summary_payload), httpx_mod.ReadTimeout("timed out")])
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240101/media-mapping-coverage")

        assert resp.status_code == 502
        assert "unavailable" in resp.json()["detail"]

    def test_rejects_invalid_version(self, proxy_client: TestClient) -> None:
        resp = proxy_client.get("/admin/api/extraction-analysis/bad!version/media-mapping-coverage")
        assert resp.status_code == 400

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_auth_header_to_the_coverage_route(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs"}
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload), _mock_response(200, _coverage_payload("discogs"))])
        mock_cls_patch.return_value = mock_instance

        proxy_client.get(
            "/admin/api/extraction-analysis/20240101/media-mapping-coverage",
            headers={"Authorization": "Bearer mytoken"},
        )

        coverage_call = mock_instance.get.call_args_list[1]
        assert coverage_call[1]["headers"]["Authorization"] == "Bearer mytoken"
