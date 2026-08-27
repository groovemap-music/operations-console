"""Regression test for discogsography-cu2.35.

Pins dashboard/static/admin.js's DLQ name generation to the promoted catalog
event contract so the console and backend cannot silently drift apart.
"""

import re
from pathlib import Path

from dashboard.catalog_contract import (
    CONSUMER_SOURCES,
    DISCOGS_EXCHANGE_PREFIX,
    MUSICBRAINZ_EXCHANGE_PREFIX,
    dead_letter_queue_name,
    entity_types,
)


ADMIN_JS_PATH = Path(__file__).parent.parent / "dashboard" / "static" / "admin.js"

_SOURCE_PREFIXES = {
    "discogs": DISCOGS_EXCHANGE_PREFIX,
    "musicbrainz": MUSICBRAINZ_EXCHANGE_PREFIX,
}

_GROUP_RE = re.compile(r"\{\s*source:\s*'(?P<source>\w+)',\s*consumer:\s*'(?P<consumer>[\w-]+)',\s*types:\s*\[(?P<types>[^\]]+)\]\s*\}")
_TYPE_RE = re.compile(r"'([\w-]+)'")


def _valid_dlq_names_from_catalog_contract() -> set[str]:
    return {
        dead_letter_queue_name(consumer, entity)
        for consumer, consumer_config in CONSUMER_SOURCES.items()
        for entity in entity_types(consumer_config["source"])
    }


def _parse_dlq_names_from_admin_js() -> set[str]:
    """Extract the DLQ_CONSUMER_GROUPS definition from admin.js and reconstruct queue names."""
    source = ADMIN_JS_PATH.read_text(encoding="utf-8")
    matches = _GROUP_RE.findall(source)
    assert matches, "DLQ_CONSUMER_GROUPS not found (or shape changed) in dashboard/static/admin.js"

    names: set[str] = set()
    for group_source, consumer, types_blob in matches:
        prefix = _SOURCE_PREFIXES[group_source]
        for data_type in _TYPE_RE.findall(types_blob):
            names.add(f"{prefix}-{consumer}-{data_type}.dlq")
    return names


def test_admin_js_dlq_names_match_backend_valid_dlq_names() -> None:
    """The JavaScript DLQ names must exactly equal the producer contract.

    Regression for discogsography-cu2.35: the frontend previously hardcoded
    bare `{consumer}-{type}-dlq` names while the backend required
    `{source-exchange-prefix}-{consumer}-{type}.dlq`, so no Purge button could
    ever succeed.
    """
    frontend_names = _parse_dlq_names_from_admin_js()

    assert frontend_names, "Failed to reconstruct any DLQ names from admin.js"
    expected_names = _valid_dlq_names_from_catalog_contract()
    assert frontend_names == expected_names, (
        f"admin.js DLQ names drifted from backend _VALID_DLQ_NAMES.\n"
        f"Only in admin.js: {sorted(frontend_names - expected_names)}\n"
        f"Only in contract: {sorted(expected_names - frontend_names)}"
    )


def test_admin_js_dlq_names_all_valid_path_segments() -> None:
    """Every generated DLQ name must satisfy the proxy's safe-path-segment regex."""
    safe_segment = re.compile(r"^[a-zA-Z0-9._-]+$")
    for name in _parse_dlq_names_from_admin_js():
        assert safe_segment.match(name), f"DLQ name {name!r} would be rejected by admin_proxy._validate_path_segment"
