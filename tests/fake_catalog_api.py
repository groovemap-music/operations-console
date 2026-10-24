"""In-memory stand-in for catalog-api's admin auth surface, for console tests.

It answers the promoted operations the console's session gate and login flow call
(``admin_login``, ``admin_session``, ``admin_logout``, ``admin_extractions``) with
catalog-api's documented statuses: 401 for missing, unknown, expired, or revoked tokens and
403 for an authenticated non-admin. Unit tests mount it in-process through
``httpx.ASGITransport``; the browser suite runs it as its own server (``create_app``) and drives
it through the ``/__fake__`` control routes, so the console reaches it over a real socket.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dashboard import catalog_admin_contract


ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct-horse-battery"  # Test-only credential for the in-memory fake.
# Logging in as this account issues a session that expires a few seconds later, so the browser
# suite can watch a session expire mid-use without waiting out a real lifetime.
SHORT_LIVED_EMAIL = "short-lived@example.com"
SHORT_LIVED_SECONDS = 6


@dataclass
class FakeToken:
    """One issued bearer and the catalog-api state behind it."""

    admin_id: str
    email: str
    expires_at: datetime
    is_admin: bool = True
    revoked: bool = False


class FakeCatalogApi:
    """catalog-api's admin auth contract, held in memory."""

    def __init__(self) -> None:
        """Start with no sessions and catalog-api reachable."""
        self.tokens: dict[str, FakeToken] = {}
        self.outage = False
        self.session_calls = 0
        self.audit_entries: list[dict[str, Any]] = []
        self.app = self._build_app()

    def mint(self, email: str = ADMIN_EMAIL, *, lifetime_seconds: float = 3600, is_admin: bool = True) -> str:
        """Issue a bearer directly, as a successful login would."""
        token = secrets.token_urlsafe(24)
        self.tokens[token] = FakeToken(
            admin_id=f"admin-{len(self.tokens) + 1}",
            email=email,
            expires_at=datetime.now(UTC) + timedelta(seconds=lifetime_seconds),
            is_admin=is_admin,
        )
        return token

    def revoke(self, token: str) -> None:
        """Revoke a bearer, as a logout or password change elsewhere would."""
        self.tokens[token].revoked = True

    def _live(self, request: Request) -> FakeToken | JSONResponse:
        if self.outage:
            return JSONResponse({"detail": "Admin endpoints not configured"}, status_code=503)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        record = self.tokens.get(token) if scheme.lower() == "bearer" else None
        if record is None or record.revoked or record.expires_at <= datetime.now(UTC):
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
        if not record.is_admin:
            return JSONResponse({"detail": "Admin access required"}, status_code=403)
        return record

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post(catalog_admin_contract.ADMIN_LOGIN_PATH)
        async def login(request: Request) -> JSONResponse:
            if self.outage:
                return JSONResponse({"detail": "Service not ready"}, status_code=503)
            body: dict[str, Any] = await request.json()
            email = str(body.get("email", "")).strip().lower()
            if body.get("password") != ADMIN_PASSWORD or email not in {ADMIN_EMAIL, SHORT_LIVED_EMAIL}:
                return JSONResponse({"detail": "Incorrect email or password"}, status_code=401)
            lifetime = SHORT_LIVED_SECONDS if email == SHORT_LIVED_EMAIL else 3600
            return JSONResponse({"access_token": self.mint(email, lifetime_seconds=lifetime), "token_type": "bearer", "expires_in": lifetime})

        @app.get(catalog_admin_contract.ADMIN_SESSION_PATH)
        async def session(request: Request) -> JSONResponse:
            self.session_calls += 1
            record = self._live(request)
            if isinstance(record, JSONResponse):
                return record
            return JSONResponse({"admin_id": record.admin_id, "email": record.email, "expires_at": record.expires_at.isoformat()})

        @app.post(catalog_admin_contract.ADMIN_LOGOUT_PATH)
        async def logout(request: Request) -> JSONResponse:
            record = self._live(request)
            if isinstance(record, JSONResponse):
                return record
            record.revoked = True
            return JSONResponse({"logged_out": True})

        @app.get(catalog_admin_contract.ADMIN_EXTRACTIONS_PATH)
        async def extractions(request: Request) -> JSONResponse:
            record = self._live(request)
            if isinstance(record, JSONResponse):
                return record
            return JSONResponse({"extractions": []})

        @app.get(catalog_admin_contract.ADMIN_AUDIT_LOG_PATH)
        async def audit_log(request: Request) -> JSONResponse:
            record = self._live(request)
            if isinstance(record, JSONResponse):
                return record
            action = request.query_params.get("action")
            entries = [entry for entry in self.audit_entries if action in (None, entry["action"])]
            return JSONResponse({"entries": entries, "total": len(entries), "page": 1, "page_size": 50})

        @app.post("/__fake__/audit")
        async def set_audit(request: Request) -> JSONResponse:
            self.audit_entries = (await request.json())["entries"]
            return JSONResponse({"entries": len(self.audit_entries)})

        @app.post("/__fake__/outage")
        async def set_outage(request: Request) -> JSONResponse:
            self.outage = bool((await request.json())["enabled"])
            return JSONResponse({"outage": self.outage})

        @app.post("/__fake__/revoke-all")
        async def revoke_all() -> JSONResponse:
            for record in self.tokens.values():
                record.revoked = True
            return JSONResponse({"revoked": len(self.tokens)})

        @app.post("/__fake__/mint")
        async def mint(request: Request) -> JSONResponse:
            body: dict[str, Any] = await request.json()
            token = self.mint(
                body.get("email", ADMIN_EMAIL),
                lifetime_seconds=float(body.get("lifetime_seconds", 3600)),
                is_admin=bool(body.get("is_admin", True)),
            )
            return JSONResponse({"token": token})

        @app.get("/__fake__/health")
        async def health() -> JSONResponse:
            return JSONResponse({"status": "ok"})

        return app


def create_app() -> FastAPI:
    """Uvicorn factory for the browser suite's standalone fake catalog-api."""
    return FakeCatalogApi().app
