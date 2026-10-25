"""In-memory stand-in for catalog-api's admin auth surface, for console tests.

It answers the promoted operations the console's session gate and login flow call
(``admin_login``, ``admin_session``, ``admin_logout``, ``admin_extractions``) with
catalog-api's documented statuses: 401 for missing, unknown, expired, or revoked tokens and
403 for an authenticated non-admin. It also serves the audit log and the three Identity Ledger
operations (``admin_identity_ledger_works``, ``admin_identity_ledger_work``,
``admin_identity_ledger_density``) from fixtures a test can replace. Unit tests mount it in-process through
``httpx.ASGITransport``; the browser suite runs it as its own server (``create_app``) and drives
it through the ``/__fake__`` control routes, so the console reaches it over a real socket.
"""

from __future__ import annotations

import copy
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


LEDGER_WORK = "00000000-0000-4000-8000-000000000101"
LEDGER_OTHER_WORK = "00000000-0000-4000-8000-000000000102"
LEDGER_EDITION = "00000000-0000-4000-8000-000000000001"
LEDGER_SPLIT_EDITION = "00000000-0000-4000-8000-000000000002"
LEDGER_FORMER_ITEM = "00000000-0000-4000-8000-000000000009"
LEDGER_MBID = "00000000-9000-4000-8000-000000000001"
_RULE = "fake rule"


def _event(at: str, event_type: str, dated_from: str = "groovemap", **fields: Any) -> dict[str, Any]:
    return {"at": at, "type": event_type, **fields, "dated_from": dated_from, "rule": _RULE}


def default_ledger() -> dict[str, Any]:
    """A small Identity Ledger in catalog-api's payload shapes, with both provenances.

    ``density`` is keyed by catalog kind, ``works`` is the whole works list (paged on read), and
    ``work`` maps a work id to its payload read now. A work read ``as_of`` drops the events and
    former items recorded after it.
    """
    link_added = _event(
        "2025-11-14T00:00:00Z",
        "link_added",
        "musicbrainz_fullexport",
        provider="musicbrainz",
        external_id=LEDGER_MBID,
        discogs_id_before=None,
        discogs_id_after="900001",
        source_ref="20261024-001001",
        decision_ref="backfill-1",
    )
    return {
        "density": {
            "release": [
                {"month": "2025-11", "type": "link_added", "dated_from": "musicbrainz_fullexport", "events": 40},
                {"month": "2025-12", "type": "link_added", "dated_from": "musicbrainz_fullexport", "events": 25},
                {"month": "2026-02", "type": "link_changed", "dated_from": "musicbrainz_fullexport", "events": 3},
                {"month": "2026-08", "type": "linked_at_ingest", "dated_from": "groovemap", "events": 60},
                {"month": "2026-08", "type": "minted", "dated_from": "groovemap", "events": 120},
                {"month": "2026-09", "type": "link_changed", "dated_from": "groovemap", "events": 2},
                {"month": "2026-09", "type": "reattached", "dated_from": "groovemap", "events": 4},
            ],
        },
        "works": [
            {
                "gm_id": LEDGER_WORK,
                "providers": [{"provider": "discogs", "external_id": "900101"}],
                "editions": 2,
                "evented_editions": 2,
                "repaired_editions": 1,
                "work_disagreements": 1,
                "first_event": "2025-11-14T00:00:00Z",
                "last_event": "2026-09-03T12:00:00Z",
            },
            {
                "gm_id": LEDGER_OTHER_WORK,
                "providers": [{"provider": "musicbrainz", "external_id": "00000000-9000-4000-8000-000000000102"}],
                "editions": 1,
                "evented_editions": 1,
                "repaired_editions": 0,
                "work_disagreements": 0,
                "first_event": "2026-08-01T02:00:00Z",
                "last_event": "2026-08-01T02:00:00Z",
            },
        ],
        "work": {
            LEDGER_WORK: {
                "work": {
                    "gm_id": LEDGER_WORK,
                    "state": "linked",
                    "providers": [{"provider": "discogs", "external_id": "900101", "valid_from": "2026-08-01T02:00:00Z", "valid_to": None}],
                    "events": [_event("2026-08-01T02:00:00Z", "minted", item=LEDGER_WORK, providers=["discogs"])],
                },
                "as_of": None,
                "editions": [
                    {
                        "gm_id": LEDGER_EDITION,
                        "state": "linked",
                        "primary_work": LEDGER_WORK,
                        "works": [LEDGER_WORK],
                        "work_disagreement": False,
                        "provider_rows": [
                            {"provider": "discogs", "external_id": "900001"},
                            {"provider": "musicbrainz", "external_id": LEDGER_MBID, "names_discogs_release": "900001"},
                        ],
                        "former_items": [
                            {
                                "gm_id": LEDGER_FORMER_ITEM,
                                "superseded_at": "2026-09-03T12:00:00Z",
                                "cause": "catalog_reattachment",
                                "decision_ref": "audit-7",
                            }
                        ],
                        "evidence": [],
                        "events": [
                            link_added,
                            _event("2026-08-01T02:00:00Z", "minted", item=LEDGER_EDITION, providers=["discogs"]),
                            _event(
                                "2026-09-03T12:00:00Z",
                                "reattached",
                                superseded=LEDGER_FORMER_ITEM,
                                survivor=LEDGER_EDITION,
                                decision_ref="audit-7",
                                compressed=False,
                                moves={"collection_items": 2},
                            ),
                        ],
                    },
                    {
                        "gm_id": LEDGER_SPLIT_EDITION,
                        "state": "split",
                        "primary_work": LEDGER_WORK,
                        "works": [LEDGER_WORK, LEDGER_OTHER_WORK],
                        "work_disagreement": True,
                        "provider_rows": [{"provider": "discogs", "external_id": "900002"}],
                        "former_items": [],
                        "evidence": [],
                        "events": [_event("2026-08-01T02:00:00Z", "minted", item=LEDGER_SPLIT_EDITION, providers=["discogs"])],
                    },
                ],
                "edition_count": 2,
                "truncated": False,
            },
        },
    }


def _ledger_work_as_of(payload: dict[str, Any], as_of: str | None) -> dict[str, Any]:
    """One work's payload with what was recorded after ``as_of`` dropped, as catalog-api reads it."""
    work = copy.deepcopy(payload)
    work["as_of"] = as_of
    if as_of is None:
        return work
    cutoff = datetime.fromisoformat(as_of)

    def recorded(at: str | None) -> bool:
        return at is not None and datetime.fromisoformat(at) <= cutoff

    work["work"]["events"] = [event for event in work["work"]["events"] if recorded(event["at"])]
    for edition in work["editions"]:
        edition["events"] = [event for event in edition["events"] if recorded(event["at"])]
        edition["former_items"] = [item for item in edition["former_items"] if recorded(item["superseded_at"])]
    return work


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
        self.ledger = default_ledger()
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

        @app.get(catalog_admin_contract.ADMIN_IDENTITY_LEDGER_WORKS_PATH)
        async def ledger_works(request: Request) -> JSONResponse:
            record = self._live(request)
            if isinstance(record, JSONResponse):
                return record
            page = max(int(request.query_params.get("page", "1")), 1)
            page_size = min(max(int(request.query_params.get("page_size", "50")), 1), 100)
            works = self.ledger["works"]
            listed = works[(page - 1) * page_size : page * page_size]
            return JSONResponse({"works": listed, "total": len(works), "page": page, "page_size": page_size})

        @app.get(catalog_admin_contract.ADMIN_IDENTITY_LEDGER_WORK_PATH)
        async def ledger_work(work_id: str, request: Request) -> JSONResponse:
            record = self._live(request)
            if isinstance(record, JSONResponse):
                return record
            payload = self.ledger["work"].get(work_id)
            if payload is None:
                return JSONResponse({"detail": "Work not found"}, status_code=404)
            return JSONResponse(_ledger_work_as_of(payload, request.query_params.get("as_of")))

        @app.get(catalog_admin_contract.ADMIN_IDENTITY_LEDGER_DENSITY_PATH)
        async def ledger_density(request: Request) -> JSONResponse:
            record = self._live(request)
            if isinstance(record, JSONResponse):
                return record
            kind = request.query_params.get("kind", "release")
            return JSONResponse({"kind": kind, "buckets": self.ledger["density"].get(kind, [])})

        @app.post("/__fake__/ledger")
        async def set_ledger(request: Request) -> JSONResponse:
            """Replace the ledger fixtures; an empty body restores the defaults."""
            body: dict[str, Any] = await request.json()
            self.ledger = {**default_ledger(), **body}
            return JSONResponse({"works": len(self.ledger["works"])})

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
