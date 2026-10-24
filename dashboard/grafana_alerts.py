"""Firing Grafana alerts for the console's attention bar.

The console reads Grafana's Alertmanager API server-side with a service-account token held in a
file, so the token never reaches the browser. Alerts are configured only when both ``GRAFANA_URL``
and ``GRAFANA_TOKEN_FILE`` are set; otherwise the route answers ``configured: false`` and the
attention bar shows only the problems it derives from the live feed. A Grafana failure answers
``available: false`` with a fixed note instead of an error status, so the bar degrades to the
derived items and says why. Neither the token nor an upstream error message is logged or returned.

The route is mounted behind the console's session gate.
"""

from datetime import datetime
from os import getenv
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse


logger = structlog.get_logger(__name__)

router = APIRouter()

_TIMEOUT_SECONDS = 5.0
ALERTS_PATH = "/api/alertmanager/grafana/api/v2/alerts"
ALERTS_QUERY = {"active": "true", "silenced": "false", "inhibited": "false"}
ALERT_LIMIT = 50  # more firing alerts than this is a flood the bar summarizes, not lists
SEVERITY_CRITICAL = "critical"
UNAVAILABLE_NOTE = "Grafana alerts unavailable: showing the problems derived from the live feed only"

# Test seam: tests install a mock transport here instead of patching httpx globally, which the
# session gate's own client would also see.
_transport: httpx.AsyncBaseTransport | None = None


class GrafanaUnavailableError(Exception):
    """Grafana could not be asked, or did not answer with alerts."""


def grafana_url() -> str | None:
    """Base URL of Grafana, read per request; None when alerts are not configured."""
    return getenv("GRAFANA_URL", "").strip().rstrip("/") or None


def _read_token() -> str | None:
    """The Grafana read token from ``GRAFANA_TOKEN_FILE``; None when unset, unreadable, or empty."""
    path = getenv("GRAFANA_TOKEN_FILE", "").strip()
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _same_origin(url: str, base: str) -> bool:
    left, right = urlsplit(url), urlsplit(base)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)


def shape_alerts(payload: Any, base: str) -> list[dict[str, Any]]:
    """The firing alerts the browser needs: a name, a severity, a summary, a link, and when it began."""
    if not isinstance(payload, list):
        raise GrafanaUnavailableError("unexpected response shape")
    alerts = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        labels = item.get("labels") or {}
        annotations = item.get("annotations") or {}
        link = item.get("generatorURL")
        alerts.append(
            {
                "name": str(labels.get("alertname") or "Grafana alert"),
                "severity": "crit" if str(labels.get("severity", "")).lower() == SEVERITY_CRITICAL else "warn",
                "summary": str(annotations.get("summary") or "")[:200],
                # Only a link into the configured Grafana is offered: the payload is not trusted to point elsewhere.
                "url": link if isinstance(link, str) and _same_origin(link, base) else f"{base}/alerting/list",
                "since": item.get("startsAt") if _parses_as_time(item.get("startsAt")) else None,
            }
        )
    return alerts[:ALERT_LIMIT]


def _parses_as_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


async def fetch_alerts(base: str, token: str) -> list[dict[str, Any]]:
    """Ask Grafana for its firing alerts; raises GrafanaUnavailableError on any failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=_transport) as client:
            response = await client.get(f"{base}{ALERTS_PATH}", params=ALERTS_QUERY, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise GrafanaUnavailableError(type(exc).__name__) from None
    if response.status_code != 200:
        raise GrafanaUnavailableError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise GrafanaUnavailableError("response was not JSON") from None
    return shape_alerts(payload, base)


@router.get("/api/alerts")
async def firing_alerts() -> JSONResponse:
    """Firing Grafana alerts, or why there are none to show."""
    base = grafana_url()
    if base is None or not getenv("GRAFANA_TOKEN_FILE", "").strip():
        return JSONResponse(content={"configured": False, "available": False, "alerts": [], "note": None})
    token = _read_token()
    if token is None:
        logger.warning("grafana_alerts_unavailable", reason="token file unreadable or empty")
        return JSONResponse(content={"configured": True, "available": False, "alerts": [], "note": UNAVAILABLE_NOTE})
    try:
        alerts = await fetch_alerts(base, token)
    except GrafanaUnavailableError as exc:
        logger.warning("grafana_alerts_unavailable", reason=str(exc))
        return JSONResponse(content={"configured": True, "available": False, "alerts": [], "note": UNAVAILABLE_NOTE})
    return JSONResponse(content={"configured": True, "available": True, "alerts": alerts, "note": None})
