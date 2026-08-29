"""Compliance metadata exposed through the console and API documentation."""

from pathlib import Path

from fastapi.testclient import TestClient

from dashboard.dashboard import SOURCE_REPOSITORY, app, source_url


ROOT = Path(__file__).resolve().parents[1]


def test_source_url_identifies_exact_revision() -> None:
    revision = "a" * 40

    assert source_url(revision) == f"{SOURCE_REPOSITORY}/tree/{revision}"


def test_source_url_rejects_non_revision_values() -> None:
    assert source_url(None) == SOURCE_REPOSITORY
    assert source_url("main") == SOURCE_REPOSITORY
    assert source_url("A" * 40) == SOURCE_REPOSITORY


def test_openapi_advertises_license_and_corresponding_source() -> None:
    schema = app.openapi()

    assert schema["info"]["license"]["identifier"] == "AGPL-3.0-only"
    assert schema["externalDocs"]["description"] == "Corresponding source for this console revision"
    assert schema["externalDocs"]["url"].startswith(SOURCE_REPOSITORY)


def test_visible_pages_link_to_source_and_legal_notices() -> None:
    for filename in ("index.html", "admin.html"):
        markup = (ROOT / "dashboard" / "static" / filename).read_text()
        assert 'id="source-legal-link"' in markup
        assert 'href="__GROOVEMAP_SOURCE_URL__"' in markup
        assert "Source and legal notices (AGPL-3.0-only)" in markup

    client = TestClient(app)
    for path in ("/", "/index.html", "/admin", "/admin.html"):
        response = client.get(path)
        assert response.status_code == 200
        assert "__GROOVEMAP_SOURCE_URL__" not in response.text
        assert SOURCE_REPOSITORY in response.text
