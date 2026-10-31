"""Redaction of URLs the console surfaces: in the API, the UI, error text and trace attributes.

An operator's shared target may carry ``user:pass@`` or ``?token=``. Whatever is shown, logged or
exported passes through here; the probes themselves keep using the full URL.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>)]+")


def redact_url(target: str) -> str:
    """``target`` as ``scheme://host[:port]/path``: no userinfo, query or fragment.

    The one place every surfaced probe address passes through (API, WebSocket, UI, error text),
    because an operator's shared target may carry ``user:pass@`` or ``?token=``. Probes keep
    using the full URL. A bare ``host:port`` has only its userinfo and query to lose.
    """
    if "://" not in target:
        return target.split("?", 1)[0].split("#", 1)[0].rpartition("@")[2]
    try:
        parts = urlsplit(target)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host if parts.port is None else f"{host}:{parts.port}"
    except ValueError:
        return "<invalid url>"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def redact_text(text: str) -> str:
    """``text`` with every URL in it passed through ``redact_url``."""
    return _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), text)
