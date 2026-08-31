"""Focused regressions for public repository and service identity."""

from pathlib import Path

from dashboard.dashboard import SOURCE_REPOSITORY, STARTUP_BANNER


ROOT = Path(__file__).resolve().parents[1]


def test_startup_banner_names_groovemap_operations_console() -> None:
    assert "GrooveMap operations-console" in STARTUP_BANNER
    assert "discogs" + "ography" not in STARTUP_BANNER.lower()


def test_runtime_and_image_use_repository_identity() -> None:
    assert SOURCE_REPOSITORY == "https://github.com/groovemap-music/operations-console"
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'org.opencontainers.image.title="operations-console"' in dockerfile
    assert 'org.opencontainers.image.source="https://github.com/groovemap-music/operations-console"' in dockerfile


def test_browser_pages_use_groovemap_identity() -> None:
    for page in ("index.html", "admin.html"):
        markup = (ROOT / "dashboard/static" / page).read_text()
        assert "GrooveMap" in markup
        assert "discogs" + "ography" not in markup.lower()
