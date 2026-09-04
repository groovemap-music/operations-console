"""Tests for the media mapping coverage aggregation and proxy route in admin_proxy.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx as httpx_mod
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.admin_proxy import (
    _MEDIA_MAPPING_MAX_PAGES,
    _aggregate_media_mapping_coverage,
    configure,
    router,
)


def _violation(record_id: str, field_value: str) -> dict[str, str]:
    return {
        "record_id": record_id,
        "rule": "format-not-recognized",
        "severity": "warning",
        "field": "formats.format.@name",
        "field_value": field_value,
        "entity_type": "releases",
    }


# ---------------------------------------------------------------------------
# _aggregate_media_mapping_coverage — pure aggregation logic
# ---------------------------------------------------------------------------


class TestAggregateMediaMappingCoverage:
    def test_counts_distinct_releases_not_raw_violations(self) -> None:
        """A release with two unrecognized format values counts once, not twice."""
        violations = [
            _violation("r1", "Shellac"),
            _violation("r1", "Wax Cylinder"),
            _violation("r2", "Shellac"),
        ]
        summary = {"version": "20240101", "source": "discogs", "by_entity": {"releases": {"total": 10}}}

        result = _aggregate_media_mapping_coverage(summary, violations, truncated=False)

        assert result["available"] is True
        assert result["releases_with_unmapped_media"] == 2
        assert result["unmapped_violation_count"] == 3

    def test_top_unmapped_formats_sorted_by_count_desc_then_name(self) -> None:
        violations = [
            _violation("r1", "Shellac"),
            _violation("r2", "Shellac"),
            _violation("r3", "Wax Cylinder"),
            _violation("r4", "Betamax"),
            _violation("r5", "Betamax"),
        ]
        summary = {"version": "v1", "source": "discogs", "by_entity": {"releases": {"total": 5}}}

        result = _aggregate_media_mapping_coverage(summary, violations, truncated=False)

        names_in_order = [entry["name"] for entry in result["top_unmapped_formats"]]
        # Betamax and Shellac tie at count=2; alphabetical tiebreak puts Betamax first.
        assert names_in_order == ["Betamax", "Shellac", "Wax Cylinder"]
        assert result["top_unmapped_formats"][0] == {"name": "Betamax", "count": 2}

    def test_top_unmapped_formats_capped_at_ten(self) -> None:
        violations = [_violation(f"r{i}", f"format-{i}") for i in range(15)]
        summary = {"version": "v1", "source": "discogs", "by_entity": {}}

        result = _aggregate_media_mapping_coverage(summary, violations, truncated=False)

        assert len(result["top_unmapped_formats"]) == 10

    def test_missing_field_value_falls_back_to_placeholder(self) -> None:
        violations = [{"record_id": "r1", "field_value": ""}, {"record_id": "r2"}]
        summary = {"version": "v1", "source": "discogs", "by_entity": {}}

        result = _aggregate_media_mapping_coverage(summary, violations, truncated=False)

        assert result["top_unmapped_formats"] == [{"name": "(empty)", "count": 2}]

    def test_empty_violations_yields_zero_counts_and_no_percent(self) -> None:
        summary = {"version": "v1", "source": "discogs", "by_entity": {"releases": {"total": 42}}}

        result = _aggregate_media_mapping_coverage(summary, [], truncated=False)

        assert result["releases_with_unmapped_media"] == 0
        assert result["unmapped_violation_count"] == 0
        assert result["top_unmapped_formats"] == []
        assert result["unmapped_share_of_flagged_releases_percent"] == 0.0

    def test_percent_computed_against_total_flagged_releases(self) -> None:
        violations = [_violation("r1", "Shellac"), _violation("r2", "Shellac")]
        summary = {"version": "v1", "source": "discogs", "by_entity": {"releases": {"total": 8}}}

        result = _aggregate_media_mapping_coverage(summary, violations, truncated=False)

        assert result["total_flagged_releases"] == 8
        assert result["unmapped_share_of_flagged_releases_percent"] == 25.0

    def test_percent_is_none_when_total_flagged_releases_missing(self) -> None:
        violations = [_violation("r1", "Shellac")]
        summary = {"version": "v1", "source": "discogs", "by_entity": {}}

        result = _aggregate_media_mapping_coverage(summary, violations, truncated=False)

        assert result["total_flagged_releases"] is None
        assert result["unmapped_share_of_flagged_releases_percent"] is None

    def test_truncated_flag_passed_through(self) -> None:
        summary = {"version": "v1", "source": "discogs", "by_entity": {}}

        result = _aggregate_media_mapping_coverage(summary, [], truncated=True)

        assert result["truncated"] is True


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


def _mock_httpx_error() -> tuple[AsyncMock, AsyncMock]:
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(side_effect=httpx_mod.ConnectError("refused"))
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
    def test_discogs_single_page_aggregates(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs", "by_entity": {"releases": {"total": 4}}}
        violations_payload = {
            "violations": [_violation("r1", "Shellac"), _violation("r2", "Shellac")],
            "pagination": {"page": 1, "page_size": 200, "total_items": 2, "total_pages": 1},
        }
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload), _mock_response(200, violations_payload)])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get(
            "/admin/api/extraction-analysis/20240101/media-mapping-coverage",
            headers={"Authorization": "Bearer tok"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["source"] == "discogs"
        assert data["releases_with_unmapped_media"] == 2
        assert data["unmapped_violation_count"] == 2
        assert data["total_flagged_releases"] == 4
        assert data["truncated"] is False
        assert mock_instance.get.call_count == 2

        # Second call is the violations list, filtered to the media-mapping rule/entity.
        violations_call = mock_instance.get.call_args_list[1]
        assert "/api/admin/extraction-analysis/20240101/violations" in violations_call[0][0]
        params = violations_call[1]["params"]
        assert params["rule"] == "format-not-recognized"
        assert params["entity_type"] == "releases"
        assert params["page"] == "1"
        assert params["page_size"] == "200"

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_musicbrainz_source_returns_unavailable_without_fetching_violations(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240201", "source": "musicbrainz", "by_entity": {}}
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload)])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240201/media-mapping-coverage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["source"] == "musicbrainz"
        assert "MusicBrainz" in data["reason"]
        assert "rules engine" in data["reason"]
        # Only the summary was fetched — no violations pagination for an unavailable source.
        assert mock_instance.get.call_count == 1

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_paginates_through_multiple_violation_pages(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs", "by_entity": {"releases": {"total": 3}}}
        page1 = {
            "violations": [_violation("r1", "Shellac")],
            "pagination": {"page": 1, "page_size": 1, "total_items": 2, "total_pages": 2},
        }
        page2 = {
            "violations": [_violation("r2", "Wax Cylinder")],
            "pagination": {"page": 2, "page_size": 1, "total_items": 2, "total_pages": 2},
        }
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload), _mock_response(200, page1), _mock_response(200, page2)])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240101/media-mapping-coverage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["releases_with_unmapped_media"] == 2
        assert data["unmapped_violation_count"] == 2
        assert data["truncated"] is False
        assert mock_instance.get.call_count == 3
        second_page_call = mock_instance.get.call_args_list[2]
        assert second_page_call[1]["params"]["page"] == "2"

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_truncates_after_max_pages(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs", "by_entity": {"releases": {"total": 1000}}}
        # Every page reports far more pages remaining than the cap allows.
        page_payload = {
            "violations": [_violation("r-x", "Some Format")],
            "pagination": {"page": 1, "page_size": 200, "total_items": 100_000, "total_pages": 999},
        }
        responses = [_mock_response(200, summary_payload)] + [_mock_response(200, page_payload) for _ in range(_MEDIA_MAPPING_MAX_PAGES)]
        _, mock_instance = _mock_httpx_sequence(responses)
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240101/media-mapping-coverage")

        assert resp.status_code == 200
        data = resp.json()
        assert data["truncated"] is True
        # 1 summary call + exactly _MEDIA_MAPPING_MAX_PAGES violation-page calls — the loop
        # stops at the cap rather than continuing to page 999.
        assert mock_instance.get.call_count == 1 + _MEDIA_MAPPING_MAX_PAGES

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_summary_non_200_passed_through(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_sequence([_mock_response(404, {"detail": "Version not found"})])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/missing/media-mapping-coverage")

        assert resp.status_code == 404
        assert mock_instance.get.call_count == 1

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_violations_non_200_passed_through(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "discogs", "by_entity": {"releases": {"total": 1}}}
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload), _mock_response(500, {"detail": "boom"})])
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240101/media-mapping-coverage")

        assert resp.status_code == 500

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_returns_502_on_connect_error(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        _, mock_instance = _mock_httpx_error()
        mock_cls_patch.return_value = mock_instance

        resp = proxy_client.get("/admin/api/extraction-analysis/20240101/media-mapping-coverage")

        assert resp.status_code == 502
        assert "unavailable" in resp.json()["detail"]

    def test_rejects_invalid_version(self, proxy_client: TestClient) -> None:
        resp = proxy_client.get("/admin/api/extraction-analysis/bad!version/media-mapping-coverage")
        assert resp.status_code == 400

    @patch("dashboard.admin_proxy.httpx.AsyncClient")
    def test_forwards_auth_header(self, mock_cls_patch: AsyncMock, proxy_client: TestClient) -> None:
        summary_payload = {"version": "20240101", "source": "musicbrainz", "by_entity": {}}
        _, mock_instance = _mock_httpx_sequence([_mock_response(200, summary_payload)])
        mock_cls_patch.return_value = mock_instance

        proxy_client.get(
            "/admin/api/extraction-analysis/20240101/media-mapping-coverage",
            headers={"Authorization": "Bearer mytoken"},
        )

        call_kwargs = mock_instance.get.call_args
        assert "Bearer mytoken" in str(call_kwargs)
