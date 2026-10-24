"""Base URLs for the console's deep links into Grafana and traces.

The browser builds each link (it alone knows the shared time range and the selected queue), but
the base URLs are server configuration, so the console serves them from a session-gated route.
A base URL that is unset, or is not an ``http(s)`` URL, is reported as ``None`` and the browser
renders no link at all: an absent link is better than a broken one, and a ``javascript:`` value
in the environment must never become an ``href``.

Traces are read through Grafana (its Tempo datasource points at VictoriaTraces, which serves only
the Tempo query API), so ``TRACES_URL`` is optional: when set it is the base of the Grafana that
hosts the ``groovemap-traces`` dashboard, otherwise ``GRAFANA_URL`` is used.
"""

from os import getenv
from urllib.parse import urlsplit

from fastapi import APIRouter
from fastapi.responses import JSONResponse


router = APIRouter()


def base_url(name: str) -> str | None:
    """The ``http(s)`` base URL in environment variable ``name`` without a trailing slash, or None."""
    value = getenv(name, "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return value


@router.get("/api/links")
async def deep_link_bases() -> JSONResponse:
    """The Grafana and traces base URLs the browser links to; either is None when unset."""
    grafana = base_url("GRAFANA_URL")
    return JSONResponse(content={"grafana": grafana, "traces": base_url("TRACES_URL") or grafana})
