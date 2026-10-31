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


def is_ambiguous(target: str) -> bool:
    """Whether parsers could disagree about where ``target``'s credentials end and its host begins.

    The same string can read as ``user:p`` / path ``/ss@h:1`` to ``urlsplit`` and as password
    ``p/ss`` / host ``h:1`` to another client, so redacting by one reading leaks the other's
    secret. A target is ambiguous when its hand split (last ``@`` before the first ``/``, ``?`` or
    ``#``) disagrees with ``urlsplit``'s netloc, when it has more than one ``@`` in the authority,
    when ``@`` appears in the path, query or fragment, or when it holds whitespace, control
    characters or a backslash in the authority. Percent-encoded userinfo is never ambiguous.
    """
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in target):
        return True
    if "://" not in target:
        rest = target
        tail_start = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=len(rest))
        authority, tail = rest[:tail_start], rest[tail_start:]
        return authority.count("@") > 1 or "@" in tail or "\\" in authority
    rest = target.partition("://")[2]
    end = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=len(rest))
    try:
        parts = urlsplit(target)
        _ = parts.port
    except ValueError:
        return True
    return rest[:end] != parts.netloc or parts.netloc.count("@") > 1 or "\\" in parts.netloc or "@" in parts.path + parts.query + parts.fragment


def redact_url(target: str) -> str:
    """``target`` as ``scheme://host[:port]/path``: no userinfo, query or fragment.

    The one place every surfaced probe address passes through (API, WebSocket, UI, error text),
    because an operator's shared target may carry ``user:pass@`` or ``?token=``. Probes keep
    using the full URL. A bare ``host:port`` has only its userinfo and query to lose. What cannot
    be parsed, or could be read two ways (`is_ambiguous`), is replaced whole, never partly shown.
    """
    if is_ambiguous(target):
        return REDACTED_URL
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
    if is_ambiguous(target):
        return REDACTED_URL
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
