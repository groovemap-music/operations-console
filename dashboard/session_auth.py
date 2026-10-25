"""Admin-session gate for the console's own data routes and its live WebSocket.

``catalog-api`` owns authentication; the console holds no JWT secret. A bearer is valid only
when catalog-api's promoted ``admin_session`` operation answers 200 for it. Any other answer
denies access: 401 and 403 (missing, expired, revoked, password-changed, or non-admin tokens)
deny it as an invalid session; a 5xx, a malformed body, or an unreachable catalog-api deny it
as unavailable, because the console cannot tell a live session from a dead one without its
authority. A positive answer is cached for at most 30 seconds, keyed by a SHA-256 of the
token and never past the session's own expiry, which keeps the console's polling inside
catalog-api's per-token rate limit. Only an "invalid" answer is remembered, for 10 seconds,
so the same dead bearer is not re-checked on every request; "unavailable" is never cached.
One shared HTTP client with a bounded connection pool, opened and closed by the app lifespan,
carries every check, so concurrent validations queue for a connection instead of opening
unbounded sockets to catalog-api.

The browser never holds the bearer where page script can read it. Signing in sets it as an
HttpOnly, SameSite=Strict, Path=/ cookie (``SessionCookie``), Secure and ``__Host-``-prefixed
unless the deployment explicitly turns that off for plain-HTTP access, and the console reads
it back from that cookie. An explicit ``Authorization`` header still wins over the cookie, for
clients that are not a browser. Because a browser attaches the cookie by itself, every
state-changing request that relies on it must come from the console's own origin and carry
the ``X-GrooveMap-Console`` header (``require_console_request``): a cross-site form cannot set
a custom header, and a page on any other origin, even one in ``CORS_ORIGINS``, fails the
origin check. A session catalog-api calls invalid clears the cookie in the same response.

Browsers cannot set ``Authorization`` on a WebSocket, so an authenticated
``POST /api/ws-ticket`` mints a single-use ticket valid for 30 seconds and the browser opens
``/ws?ticket=...``. Logging out purges the bearer's outstanding tickets, and the socket
re-validates the bearer with catalog-api, bypassing the cache, when it opens. ``take_ws_ticket`` moves the ticket out of the ASGI scope's query string
before any access logger or tracer reads it, and ``TicketRedactingFilter`` redacts it from
server log records written before the application sees the request (the websocket library's
own debug line). Only a ticket removed from the query string can be redeemed, so a code path
that bypasses the stripping fails closed instead of authenticating a logged ticket.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable  # noqa: TC003  # dataclass fields resolve annotations at runtime.
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import structlog
from common import describe_exception
from fastapi import HTTPException, Request  # FastAPI resolves dependency annotations at runtime.
from httpx import AsyncClient
from starlette.responses import JSONResponse

from dashboard import catalog_admin_contract


if TYPE_CHECKING:
    from starlette.responses import Response


logger = structlog.get_logger(__name__)

SESSION_CACHE_MAX_SECONDS = 30.0
WS_TICKET_SECONDS = 30.0
WS_CLOSE_UNAUTHENTICATED = 4401
# catalog-api could not answer for the socket's session; the browser backs off and retries.
WS_CLOSE_UNAVAILABLE = 4503
WS_TICKET_SCOPE_KEY = "groovemap.ws_ticket"
_VALIDATION_TIMEOUT_SECONDS = 5.0
_MAX_CACHED_SESSIONS = 1024
_MAX_OUTSTANDING_TICKETS = 4096
_MAX_INVALID_ANSWERS = 4096
# How long catalog-api's "invalid" answer for one bearer is reused without asking again.
INVALID_ANSWER_SECONDS = 10.0
# The console polls one catalog-api route; a small pool is plenty and bounds concurrent checks.
# Waiting for a free connection counts against the 5-second timeout, after which the check is
# answered "unavailable".
_VALIDATION_POOL = httpx.Limits(max_connections=16, max_keepalive_connections=8)

# RFC 6750 b64token, bounded. Anything else is rejected without a catalog-api round trip, so
# an anonymous client cannot turn arbitrary header bytes into upstream validation calls.
_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9\-._~+/]{1,4096}=*$")

# The session cookie. Secure, it takes the `__Host-` prefix, which the browser accepts only when
# Secure, host-only, and on Path=/, so a sibling subdomain or a plain-HTTP response cannot plant
# a session of its own under that name. The plain name exists only for an explicit opt-out.
SESSION_COOKIE = "groovemap_console_session"
SECURE_SESSION_COOKIE = f"__Host-{SESSION_COOKIE}"
# Every state-changing request the console's own pages make carries this header with value "1".
CONSOLE_REQUEST_HEADER = "X-GrooveMap-Console"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

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


def cache_seconds_from_env(raw: str | None) -> float:
    """Parse ``SESSION_CACHE_SECONDS``; anything but a finite number of seconds fails startup.

    ``nan`` passes both ``min`` and ``max`` unchanged and would make every cache entry immortal.
    """
    value = SESSION_CACHE_MAX_SECONDS if raw is None or not raw.strip() else float(raw)
    if not math.isfinite(value):
        raise ValueError(f"SESSION_CACHE_SECONDS must be a finite number of seconds, got {raw!r}")
    return value


def cookie_secure_from_env(raw: str | None) -> bool:
    """Parse ``SESSION_COOKIE_SECURE``: Secure unless it is explicitly ``false``.

    The default fails closed: the console cannot tell a TLS-terminating proxy in front of it
    from plain HTTP, so it never guesses. Anything but true or false fails startup rather than
    silently dropping the Secure attribute.
    """
    value = (raw or "").strip().lower()
    if value in {"", "true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise ValueError(f"SESSION_COOKIE_SECURE must be true or false, got {raw!r}")


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


class SessionCookie:
    """The HttpOnly cookie that carries the admin bearer between the browser and the console.

    Secure (and ``__Host-``-prefixed) unless ``secure`` is False, which only an explicit
    ``SESSION_COOKIE_SECURE=false`` sets, for a console browsers reach over plain HTTP on a
    host other than localhost.
    """

    def __init__(self, secure: bool = True) -> None:
        """Start Secure unless told otherwise."""
        self.secure = secure

    @property
    def name(self) -> str:
        """The cookie name the console sets and reads."""
        return SECURE_SESSION_COOKIE if self.secure else SESSION_COOKIE

    def read(self, request: Request) -> str | None:
        """The bearer the request's session cookie carries, if any."""
        return request.cookies.get(self.name) or None

    def carried_by(self, request: Request) -> bool:
        """True when the request authenticates with the cookie rather than its own header."""
        return not request.headers.get("authorization") and self.read(request) is not None

    def set(self, response: Response, token: str, max_age: int | None) -> None:
        """Hand ``token`` to the browser for at most ``max_age`` seconds (the session's own life)."""
        response.set_cookie(self.name, token, max_age=max_age, path="/", secure=self.secure, httponly=True, samesite="strict")

    def clear(self, response: Response) -> None:
        """Tell the browser to drop the session cookie."""
        response.delete_cookie(self.name, path="/", secure=self.secure, httponly=True, samesite="strict")


session_cookie = SessionCookie()


def request_authorization(request: Request) -> str | None:
    """The request's credential as an ``Authorization`` value: its own header, else the cookie.

    The header wins, so a request that sends one is never also authenticated by the cookie.
    """
    header = request.headers.get("authorization")
    if header:
        return header
    token = session_cookie.read(request)
    return f"Bearer {token}" if token else None


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
        if not math.isfinite(ttl_seconds):
            raise ValueError(f"session cache TTL must be finite, got {ttl_seconds!r}")
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

    def purge(self, key: str) -> None:
        """Drop every outstanding ticket issued for the bearer whose digest is ``key``."""
        for ticket_key in [ticket_key for ticket_key, (_, grant) in self._tickets.items() if token_digest(grant.token) == key]:
            del self._tickets[ticket_key]

    def _prune(self) -> None:
        now = self._monotonic()
        for key in [key for key, (deadline, _) in self._tickets.items() if now >= deadline]:
            del self._tickets[key]


class InvalidAnswers:
    """Bearer digests catalog-api recently answered "invalid" for, each reused for a few seconds."""

    def __init__(
        self,
        ttl_seconds: float = INVALID_ANSWER_SECONDS,
        *,
        max_entries: int = _MAX_INVALID_ANSWERS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an empty record."""
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._monotonic = monotonic
        self._entries: OrderedDict[str, float] = OrderedDict()

    def __len__(self) -> int:
        """Number of answers held, including any not yet pruned."""
        return len(self._entries)

    def holds(self, key: str) -> bool:
        """True while the recorded answer for ``key`` still stands."""
        deadline = self._entries.get(key)
        if deadline is None:
            return False
        if self._monotonic() >= deadline:
            del self._entries[key]
            return False
        return True

    def add(self, key: str) -> None:
        """Record an "invalid" answer for ``key``."""
        self._entries[key] = self._monotonic() + self.ttl_seconds
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


class SessionGate:
    """Validates bearers against catalog-api and issues WebSocket tickets."""

    def __init__(self) -> None:
        """Start with the compose-default catalog-api address and empty stores."""
        self.api_base_url = "http://api:8004"
        self.cache = SessionCache()
        self.tickets = TicketStore()
        self.invalid = InvalidAnswers()
        self.transport: httpx.AsyncBaseTransport | None = None
        self._client: AsyncClient | None = None

    def configure(
        self,
        api_host: str,
        api_port: int,
        *,
        cache_seconds: float = SESSION_CACHE_MAX_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Point the gate at catalog-api and reset its stores (called at import and in tests)."""
        self.api_base_url = f"http://{api_host}:{api_port}"
        self.cache = SessionCache(cache_seconds)
        self.tickets = TicketStore()
        self.invalid = InvalidAnswers()
        self.use_transport(transport)

    def use_transport(self, transport: httpx.AsyncBaseTransport | None) -> None:
        """Send future checks through ``transport``; the shared client is rebuilt on next start."""
        self.transport = transport
        self._client = None

    async def start(self) -> None:
        """Open the shared catalog-api client (called from the app lifespan)."""
        if self._client is None:
            self._client = AsyncClient(timeout=_VALIDATION_TIMEOUT_SECONDS, limits=_VALIDATION_POOL, transport=self.transport)

    async def aclose(self) -> None:
        """Close the shared catalog-api client (called from the app lifespan)."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def validate(self, authorization: str | None) -> tuple[AdminSession, str]:
        """Return the live session behind an ``Authorization`` header value, and its bearer."""
        if not authorization:
            raise SessionRejected("missing")
        token = bearer_token(authorization)
        if token is None:
            raise SessionRejected("invalid")
        return await self.validate_token(token), token

    async def validate_token(self, token: str, *, fresh: bool = False) -> AdminSession:
        """Return the live session for ``token``.

        ``fresh`` skips the positive cache and asks catalog-api, so a caller that must notice a
        revocation promptly (an open WebSocket) never rides a cached answer.
        """
        key = token_digest(token)
        if not fresh:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        if self.invalid.holds(key):
            raise SessionRejected("invalid")
        try:
            session = await self._fetch_session(token)
        except SessionRejected as rejected:
            if rejected.reason == "invalid":
                self.cache.evict(key)
                self.invalid.add(key)
            raise
        self.cache.put(key, session)
        return session

    def forget(self, authorization: str | None) -> None:
        """Drop everything held for a bearer after logout: cached session and open tickets."""
        token = bearer_token(authorization)
        if token is not None:
            key = token_digest(token)
            self.cache.evict(key)
            self.tickets.purge(key)

    async def _get(self, url: str, token: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"}
        if self._client is not None:
            return await self._client.get(url, headers=headers)
        # Outside the app lifespan (a bare TestClient), fall back to a one-off client.
        async with AsyncClient(timeout=_VALIDATION_TIMEOUT_SECONDS, transport=self.transport) as client:
            return await client.get(url, headers=headers)

    async def _fetch_session(self, token: str) -> AdminSession:
        url = f"{self.api_base_url}{catalog_admin_contract.ADMIN_SESSION_PATH}"
        try:
            response = await self._get(url, token)
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
    session, _ = await gate.validate(request_authorization(request))
    return session


def _same_origin(request: Request) -> bool:
    """Whether a browser sent ``request`` from the console's own origin, as far as it says.

    ``Sec-Fetch-Site``, which every current browser sends and no page can set, must be
    ``same-origin``: ``same-site`` would admit another port on the same host, such as Grafana
    on localhost:3000. A browser too old to send it still sends ``Origin`` on a POST, which must
    name the host the request was sent to. A client that sends neither is not a browser page.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site == "same-origin"
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host")
    return host is not None and urlsplit(origin).netloc.lower() == host.lower()


async def require_console_request(request: Request) -> None:
    """FastAPI dependency: refuse a state-changing request another origin could have forged.

    Safe methods pass. A request with its own ``Authorization`` header passes too: the header
    wins over the cookie, and no other page can attach one without the CORS preflight the
    console refuses. Any other unsafe request is refused with 403 unless it comes from the
    console's own origin and carries ``X-GrooveMap-Console: 1``, so the cookie alone never
    authorizes a change, not even from an origin in ``CORS_ORIGINS``.
    """
    if request.method not in _UNSAFE_METHODS or request.headers.get("authorization"):
        return
    if not _same_origin(request) or request.headers.get(CONSOLE_REQUEST_HEADER) != "1":
        raise HTTPException(status_code=403, detail="Cross-site request refused: the console's own pages send this request")


async def session_rejected_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a SessionRejected as ``{"detail", "reason"}`` with the matching status.

    A session catalog-api called invalid also clears the cookie that carried it; one it could
    not answer for keeps it, so the browser resumes once catalog-api is back.
    """
    if not isinstance(exc, SessionRejected):  # pragma: no cover - registered for SessionRejected only
        raise exc
    response = JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "reason": exc.reason},
        headers={"WWW-Authenticate": "Bearer"},
    )
    if exc.reason == "invalid" and session_cookie.carried_by(request):
        session_cookie.clear(response)
    return response


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
