"""Admin-session gate for the console's own data routes and its live WebSocket.

``catalog-api`` owns authentication; the console holds no JWT secret. A bearer is valid only
when catalog-api's promoted ``admin_session`` operation answers 200 for it. Any other answer
denies access: 401 and 403 (missing, expired, revoked, password-changed, or non-admin tokens)
deny it as an invalid session; a 5xx, a malformed body, or an unreachable catalog-api deny it
as unavailable, because the console cannot tell a live session from a dead one without its
authority. A positive answer is cached for at most 30 seconds, keyed by a SHA-256 of the
token and never past the session's own expiry, which keeps the console's polling inside
catalog-api's per-token rate limit. Failures are never cached.

Browsers cannot set ``Authorization`` on a WebSocket, so an authenticated
``POST /api/ws-ticket`` mints a single-use ticket valid for 30 seconds and the browser opens
``/ws?ticket=...``. ``take_ws_ticket`` moves the ticket out of the ASGI scope's query string
before any access logger or tracer reads it, and ``TicketRedactingFilter`` redacts it from
server log records written before the application sees the request (the websocket library's
own debug line). Only a ticket removed from the query string can be redeemed, so a code path
that bypasses the stripping fails closed instead of authenticating a logged ticket.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable  # noqa: TC003  # dataclass fields resolve annotations at runtime.
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode

import httpx
import structlog
from common import describe_exception
from fastapi import Request  # noqa: TC002  # FastAPI resolves dependency annotations at runtime.
from starlette.responses import JSONResponse

from dashboard import catalog_admin_contract


logger = structlog.get_logger(__name__)

SESSION_CACHE_MAX_SECONDS = 30.0
WS_TICKET_SECONDS = 30.0
WS_CLOSE_UNAUTHENTICATED = 4401
WS_TICKET_SCOPE_KEY = "groovemap.ws_ticket"
_VALIDATION_TIMEOUT_SECONDS = 5.0
_MAX_CACHED_SESSIONS = 1024
_MAX_OUTSTANDING_TICKETS = 4096

# RFC 6750 b64token, bounded. Anything else is rejected without a catalog-api round trip, so
# an anonymous client cannot turn arbitrary header bytes into upstream validation calls.
_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9\-._~+/]{1,4096}=*$")

Reason = Literal["missing", "invalid", "unavailable"]

_REJECTION_STATUS: dict[str, int] = {"missing": 401, "invalid": 401, "unavailable": 503}
_REJECTION_DETAIL: dict[str, str] = {
    "missing": "Sign in to use the operations console",
    "invalid": "Your admin session has expired or was revoked; sign in again",
    "unavailable": "catalog-api could not verify your admin session; sign in again once it is reachable",
}


@dataclass(frozen=True)
class AdminSession:
    """A catalog-api admin session the console has confirmed live."""

    admin_id: str
    email: str
    expires_at: datetime

    def seconds_left(self, now: datetime | None = None) -> float:
        """Seconds until catalog-api stops honouring the session."""
        return (self.expires_at - (now or datetime.now(UTC))).total_seconds()


class SessionRejected(Exception):
    """The request carries no session the console can honour."""

    def __init__(self, reason: Reason) -> None:
        """Record why the session was rejected."""
        super().__init__(reason)
        self.reason: Reason = reason

    @property
    def status_code(self) -> int:
        """HTTP status for the rejection: 503 when catalog-api could not answer, else 401."""
        return _REJECTION_STATUS[self.reason]

    @property
    def detail(self) -> str:
        """Human-readable reason, shown on the login form."""
        return _REJECTION_DETAIL[self.reason]


def token_digest(token: str) -> str:
    """Return the cache key for a bearer token; the token itself is never stored as a key."""
    return hashlib.sha256(token.encode()).hexdigest()


def bearer_token(authorization: str | None) -> str | None:
    """Extract a well-formed bearer token from an ``Authorization`` header value."""
    scheme, _, credentials = (authorization or "").partition(" ")
    credentials = credentials.strip()
    if scheme.lower() != "bearer" or not _BEARER_TOKEN.fullmatch(credentials):
        return None
    return credentials


class SessionCache:
    """Positive-only session cache with a bounded lifetime and size."""

    def __init__(
        self,
        ttl_seconds: float = SESSION_CACHE_MAX_SECONDS,
        *,
        max_entries: int = _MAX_CACHED_SESSIONS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Create a cache whose entries live at most ``ttl_seconds`` (capped at 30)."""
        self.ttl_seconds = min(max(ttl_seconds, 0.0), SESSION_CACHE_MAX_SECONDS)
        self.max_entries = max_entries
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._entries: OrderedDict[str, tuple[float, AdminSession]] = OrderedDict()

    def __len__(self) -> int:
        """Number of entries currently held, including any not yet pruned."""
        return len(self._entries)

    def get(self, key: str) -> AdminSession | None:
        """Return the cached session for ``key`` while its entry is still fresh."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        deadline, session = entry
        if self._monotonic() >= deadline:
            del self._entries[key]
            return None
        return session

    def put(self, key: str, session: AdminSession) -> None:
        """Cache ``session`` for the shorter of the TTL and the session's remaining life."""
        lifetime = min(self.ttl_seconds, session.seconds_left(self._wall_clock()))
        if lifetime <= 0:
            self._entries.pop(key, None)
            return
        self._entries[key] = (self._monotonic() + lifetime, session)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def evict(self, key: str) -> None:
        """Drop ``key`` so the next request re-validates with catalog-api."""
        self._entries.pop(key, None)


@dataclass(frozen=True)
class TicketGrant:
    """What a redeemed WebSocket ticket proves: a live session and the bearer behind it.

    The bearer is held in memory only, so the socket can re-validate its session while open.
    """

    session: AdminSession
    token: str = field(repr=False)


class TicketStore:
    """Single-use WebSocket tickets, stored by digest and valid for at most 30 seconds."""

    def __init__(
        self,
        ttl_seconds: float = WS_TICKET_SECONDS,
        *,
        max_outstanding: int = _MAX_OUTSTANDING_TICKETS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Create an empty store."""
        self.ttl_seconds = ttl_seconds
        self.max_outstanding = max_outstanding
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._tickets: OrderedDict[str, tuple[float, TicketGrant]] = OrderedDict()

    def __len__(self) -> int:
        """Number of tickets issued and not yet redeemed or pruned."""
        return len(self._tickets)

    def issue(self, session: AdminSession, token: str) -> tuple[str, float]:
        """Mint a ticket for ``session``; return it with its lifetime in seconds."""
        self._prune()
        lifetime = max(min(self.ttl_seconds, session.seconds_left(self._wall_clock())), 0.0)
        ticket = secrets.token_urlsafe(32)
        self._tickets[token_digest(ticket)] = (self._monotonic() + lifetime, TicketGrant(session=session, token=token))
        while len(self._tickets) > self.max_outstanding:
            self._tickets.popitem(last=False)
        return ticket, lifetime

    def redeem(self, ticket: str | None) -> TicketGrant | None:
        """Consume ``ticket``: a grant the first time within its lifetime, else None."""
        if not ticket:
            return None
        entry = self._tickets.pop(token_digest(ticket), None)
        if entry is None:
            return None
        deadline, grant = entry
        if self._monotonic() >= deadline:
            return None
        return grant

    def _prune(self) -> None:
        now = self._monotonic()
        for key in [key for key, (deadline, _) in self._tickets.items() if now >= deadline]:
            del self._tickets[key]


class SessionGate:
    """Validates bearers against catalog-api and issues WebSocket tickets."""

    def __init__(self) -> None:
        """Start with the compose-default catalog-api address and empty stores."""
        self.api_base_url = "http://api:8004"
        self.cache = SessionCache()
        self.tickets = TicketStore()
        self.transport: httpx.AsyncBaseTransport | None = None

    def configure(
        self,
        api_host: str,
        api_port: int,
        *,
        cache_seconds: float = SESSION_CACHE_MAX_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Point the gate at catalog-api and reset its cache and tickets (called at startup)."""
        self.api_base_url = f"http://{api_host}:{api_port}"
        self.cache = SessionCache(cache_seconds)
        self.tickets = TicketStore()
        self.transport = transport

    async def validate(self, authorization: str | None) -> tuple[AdminSession, str]:
        """Return the live session behind an ``Authorization`` header value, and its bearer."""
        if not authorization:
            raise SessionRejected("missing")
        token = bearer_token(authorization)
        if token is None:
            raise SessionRejected("invalid")
        return await self.validate_token(token), token

    async def validate_token(self, token: str) -> AdminSession:
        """Return the live session for ``token`` from the cache or from catalog-api."""
        key = token_digest(token)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        session = await self._fetch_session(token)
        self.cache.put(key, session)
        return session

    def forget(self, authorization: str | None) -> None:
        """Evict the cached session behind a header value, e.g. after a successful logout."""
        token = bearer_token(authorization)
        if token is not None:
            self.cache.evict(token_digest(token))

    async def _fetch_session(self, token: str) -> AdminSession:
        url = f"{self.api_base_url}{catalog_admin_contract.ADMIN_SESSION_PATH}"
        try:
            async with httpx.AsyncClient(timeout=_VALIDATION_TIMEOUT_SECONDS, transport=self.transport) as client:
                response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            logger.warning("⚠️ catalog-api unreachable for admin-session validation", url=url, error=describe_exception(exc))
            raise SessionRejected("unavailable") from exc

        if response.status_code in {401, 403}:
            raise SessionRejected("invalid")
        if response.status_code != 200:
            logger.warning("⚠️ catalog-api could not validate an admin session", status=response.status_code)
            raise SessionRejected("unavailable")

        session = _parse_session(response)
        if session is None:
            logger.warning("⚠️ catalog-api returned a malformed admin-session body")
            raise SessionRejected("unavailable")
        if session.seconds_left() <= 0:
            raise SessionRejected("invalid")
        return session


def _parse_session(response: httpx.Response) -> AdminSession | None:
    try:
        body: Any = response.json()
        admin_id, email, expires_raw = body["admin_id"], body["email"], body["expires_at"]
        if not isinstance(admin_id, str) or not isinstance(email, str) or not isinstance(expires_raw, str):
            return None
        expires_at = datetime.fromisoformat(expires_raw)
    except ValueError, KeyError, TypeError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return AdminSession(admin_id=admin_id, email=email, expires_at=expires_at)


gate = SessionGate()


async def require_session(request: Request) -> AdminSession:
    """FastAPI dependency: the request's live admin session, or a SessionRejected response."""
    session, _ = await gate.validate(request.headers.get("authorization"))
    return session


async def session_rejected_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render a SessionRejected as ``{"detail", "reason"}`` with the matching status."""
    if not isinstance(exc, SessionRejected):  # pragma: no cover - registered for SessionRejected only
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "reason": exc.reason},
        headers={"WWW-Authenticate": "Bearer"},
    )


def take_ws_ticket(scope: dict[str, Any]) -> None:
    """Move a WebSocket's ``ticket`` query parameter into the scope, out of the query string.

    Called on the raw ASGI scope before any middleware, so access logs and request spans see
    the query string without it. The scope dict is the server's own, so uvicorn's
    "WebSocket ... [accepted]" line is rendered from the stripped query too.
    """
    if scope.get("type") != "websocket":
        return
    raw: bytes = scope.get("query_string", b"")
    if b"ticket" not in raw:
        return
    ticket: str | None = None
    kept: list[tuple[str, str]] = []
    for name, value in parse_qsl(raw.decode("latin-1"), keep_blank_values=True):
        if name == "ticket":
            ticket = value if ticket is None else ticket
        else:
            kept.append((name, value))
    scope["query_string"] = urlencode(kept).encode("latin-1")
    if ticket is not None:
        scope[WS_TICKET_SCOPE_KEY] = ticket


_TICKET_IN_TEXT = re.compile(r"(ticket=)[^&\s\"']+")


def redact_tickets(text: str) -> str:
    """Replace any ``ticket=<value>`` in ``text`` with a redaction marker."""
    return _TICKET_IN_TEXT.sub(r"\1[redacted]", text)


class TicketRedactingFilter(logging.Filter):
    """Redact WebSocket tickets from a log record's rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record in place when its message carries a ticket; never drop it."""
        try:
            message = record.getMessage()
        except Exception:  # A malformed record is the logger's problem, not this filter's.
            return True
        if "ticket=" in message:
            record.msg = redact_tickets(message)
            record.args = None
        return True


# Loggers that render a request target. uvicorn's error logger also receives the websocket
# library's handshake debug line ("< GET /ws?ticket=... HTTP/1.1"), and at trace level
# uvicorn.asgi dumps the raw scope; both are written before the app can strip the ticket.
_REQUEST_TARGET_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi", "websockets", "websockets.server")


def install_ticket_redaction() -> None:
    """Attach one TicketRedactingFilter to every logger that renders a request target."""
    for name in _REQUEST_TARGET_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(existing, TicketRedactingFilter) for existing in target.filters):
            target.addFilter(TicketRedactingFilter())
