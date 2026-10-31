"""Redaction of URLs the console surfaces: in the API, the UI, error text and trace attributes.

An operator's shared target may carry ``user:pass@`` or ``?token=``. Whatever is shown, logged or
exported passes through here; the probes themselves keep using the full URL.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit


# A URL-looking token runs to the next whitespace: stopping early at a quote or bracket is how a
# password containing one leaked its tail. Trailing sentence punctuation is kept out of the token.
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_TRAILING = ".,;:!?)]}'\""
REDACTED_URL = "<redacted url>"
REDACTED = "<redacted>"


def redact_url(target: str) -> str:
    """``target`` as ``scheme://host[:port]/path``: no userinfo, query or fragment.

    The one place every surfaced probe address passes through (API, WebSocket, UI, error text),
    because an operator's shared target may carry ``user:pass@`` or ``?token=``. Probes keep
    using the full URL. A bare ``host:port`` has only its userinfo and query to lose. What cannot
    be parsed is replaced whole, never partly shown.
    """
    if "://" not in target:
        return target.split("?", 1)[0].split("#", 1)[0].rpartition("@")[2]
    try:
        parts = urlsplit(target)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host if parts.port is None else f"{host}:{parts.port}"
        if not host:
            return REDACTED_URL
    except ValueError:
        return REDACTED_URL
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def sensitive_parts(target: str) -> list[str]:
    """Every piece of ``target`` that must not be shown: userinfo, user, password, query, fragment, and each query value.

    Split by hand, not by ``urlsplit``, so a target ``urlsplit`` rejects still yields its secrets.
    Each piece also appears percent-decoded and percent-encoded, longest first.
    """
    rest = target.partition("://")[2] if "://" in target else target
    rest, _, fragment = rest.partition("#")
    rest, _, query = rest.partition("?")
    authority = rest.partition("/")[0]
    userinfo = authority.rpartition("@")[0] if "@" in authority else ""
    user, _, password = userinfo.partition(":")
    pieces = {userinfo, user, password, query, fragment}
    for pair in query.split("&"):
        pieces.add(pair.partition("=")[2])
    variants: set[str] = set()
    for piece in pieces:
        variants.update((piece, unquote(piece), quote(piece, safe=""), quote(unquote(piece), safe="")))
    return sorted((v for v in variants if v), key=len, reverse=True)


def scrub(text: str, target: str) -> str:
    """``text`` with ``target`` (the probe's own address) and every secret part of it removed."""
    if not target:
        return text
    text = text.replace(target, redact_url(target))
    for part in sensitive_parts(target):
        text = text.replace(part, REDACTED)
    return text


def _redact_token(match: re.Match[str]) -> str:
    token = match.group(0)
    body = token.rstrip(_TRAILING)
    tail = token[len(body) :]
    return redact_url(body) + tail


def redact_text(text: str) -> str:
    """``text`` with every whole URL-looking token in it passed through ``redact_url``.

    A secondary net: the primary defence is ``scrub`` with the known target.
    """
    return _URL_IN_TEXT.sub(_redact_token, text)
