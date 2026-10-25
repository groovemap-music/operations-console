"""Proxy router for admin API calls to the API service.

All proxied requests target a fixed internal API base URL configured via
server-side environment variables only.  Path parameters are validated
with strict alphanumeric patterns to prevent path-traversal.  Request
bodies are validated as JSON and re-serialised before forwarding.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import structlog
from common import describe_exception
from fastapi import APIRouter, Query, Request, Response
from starlette.responses import JSONResponse

from dashboard import catalog_admin_contract
from dashboard.session_auth import bearer_token, request_authorization, session_cookie
from dashboard.session_auth import gate as session_gate


logger = structlog.get_logger(__name__)

router = APIRouter()
# Login and logout are mounted without the console's session gate (catalog-api validates both);
# every route on `router` is mounted behind it.
public_router = APIRouter()

# Fixed internal API base URL — set once at startup from env vars, never
# from user input.  This is NOT an SSRF vector because callers cannot
# influence the destination host or port.
_api_base_url: str = "http://api:8004"

# Strict pattern for path parameters forwarded to the API.
# Dots are allowed to support version strings like "20240101.0".
_SAFE_PATH_SEGMENT = re.compile(r"^[a-zA-Z0-9._-]+$")


def configure(api_host: str, api_port: int) -> None:
    """Set API service connection details (called once at startup)."""
    global _api_base_url
    _api_base_url = f"http://{api_host}:{api_port}"


def _validate_path_segment(value: str) -> bool:
    """Return True when *value* is safe to embed in a URL path.

    The alphanumeric-plus-dot allowlist intentionally permits dots for
    version strings like "20240101.0", but a segment of pure dots (``.``
    or ``..``) also matches that pattern and is a reserved dot-segment:
    httpx/RFC 3986 URL normalization collapses it during `base_url` merge,
    silently retargeting the request to a sibling endpoint. Reject those
    explicitly so the allowlist actually prevents path-traversal, as the
    module docstring claims.
    """
    if value in {".", ".."}:
        return False
    return bool(_SAFE_PATH_SEGMENT.match(value))


def _auth_headers(request: Request, *, authenticate: bool = True) -> dict[str, str]:
    """Build headers for the proxied request: Authorization plus trustworthy forwarding info.

    The Authorization header is the request's own, or else the bearer from the session cookie,
    so catalog-api sees the same credential whichever way the browser presented it. Login
    passes ``authenticate=False``: it starts a session and must not present an old one.

    X-Forwarded-For/-Proto are always set from what THIS service observed as the
    TCP peer and request scheme — never copied from the inbound request, which
    would let a client spoof its apparent IP and defeat the API's per-IP rate
    limits (e.g. the admin login limiter, api/routers/admin.py). api/api.py only
    trusts these headers when they arrive from the internal docker network. See
    the trusted-forwarding regression.
    """
    headers: dict[str, str] = {}
    auth = request_authorization(request) if authenticate else None
    if auth:
        headers["Authorization"] = auth
    if request.client and request.client.host:
        headers["X-Forwarded-For"] = request.client.host
    headers["X-Forwarded-Proto"] = request.url.scheme
    return headers


def _unavailable_response() -> Response:
    return Response(content=b'{"detail":"API service unavailable"}', status_code=502, media_type="application/json")


def _ok_response(resp: httpx.Response) -> Response:
    """Relay catalog-api's answer, with the one header its callers act on.

    Retry-After says when to ask again: the Identity Ledger answers 503 with it until its
    first refresh, and the Ledger tab waits that long.
    """
    headers: dict[str, str] = {}
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        headers["Retry-After"] = retry_after
    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json", headers=headers)


def _build_url(api_path: str) -> str:
    """Build a full URL from the fixed base and a hardcoded API path.

    This is only called with string literals defined in this module —
    never with user-supplied data.
    """
    return f"{_api_base_url}{api_path}"


async def _proxy_request(
    request: Request,
    method: str,
    api_path: str,
    *,
    params: dict[str, str] | None = None,
    content: bytes | None = None,
    timeout: float = 30.0,
    client: httpx.AsyncClient | None = None,
    authenticate: bool = True,
) -> Response:
    """Forward one request while keeping endpoint policy in the route handler.

    A 401 from catalog-api for a cookie-authenticated request means the session behind the
    cookie is gone: the console forgets it and the response clears the cookie.
    """
    url = _build_url(api_path)
    headers = _auth_headers(request, authenticate=authenticate)
    if content:
        headers["Content-Type"] = "application/json"

    async def send(active_client: httpx.AsyncClient) -> httpx.Response:
        kwargs: dict[str, Any] = {"headers": headers}
        if params is not None:
            kwargs["params"] = params
        if content:
            kwargs["content"] = content
        if method == "GET":
            return await active_client.get(url, **kwargs)
        return await active_client.post(url, **kwargs)

    try:
        if client is not None:
            response = await send(client)
        else:
            async with httpx.AsyncClient(timeout=timeout) as owned_client:
                response = await send(owned_client)
    except (httpx.ConnectError, httpx.RequestError) as exc:
        logger.error("❌ API service unreachable", url=url, error=describe_exception(exc))
        return _unavailable_response()
    result = _ok_response(response)
    if authenticate and response.status_code == 401 and session_cookie.carried_by(request):
        session_gate.forget(request_authorization(request))
        session_cookie.clear(result)
    return result


async def _validated_json_body(request: Request) -> bytes | None:
    """Read the request body, validate it as JSON, and re-serialise.

    Re-serialising through ``json.loads`` / ``json.dumps`` sanitises the
    payload so that no raw user bytes are forwarded verbatim.  Returns
    ``None`` when the body is empty.

    Raises ``json.JSONDecodeError`` for both malformed JSON and non-UTF-8
    bodies, so every caller's single ``except json.JSONDecodeError`` clause
    handles both cases uniformly instead of only the former (a raw
    ``UnicodeDecodeError`` is a ``ValueError`` sibling, not a subclass, of
    ``json.JSONDecodeError``, and would otherwise escape as an unhandled 500).
    """
    raw = await request.body()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except UnicodeDecodeError as exc:
        msg = f"Request body is not valid UTF-8: {exc}"
        raise json.JSONDecodeError(msg, "", 0) from exc
    return json.dumps(parsed, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# Routes — each maps a dashboard path to a fixed API path.
# Every handler builds its own URL from a hardcoded literal path.
# ---------------------------------------------------------------------------


def _signed_in_response(body: bytes) -> Response:
    """Keep the bearer catalog-api issued in the session cookie; tell the browser only its lifetime."""
    try:
        issued: Any = json.loads(body)
        raw_token, expires_in = issued["access_token"], issued.get("expires_in")
    except ValueError, KeyError, TypeError:
        raw_token, expires_in = None, None
    token = bearer_token(f"Bearer {raw_token}") if isinstance(raw_token, str) else None
    if token is None:
        logger.warning("⚠️ catalog-api answered a login without a usable bearer")
        return JSONResponse(content={"detail": "catalog-api returned no usable session"}, status_code=502)
    max_age = expires_in if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0 else None
    response = JSONResponse(content={"expires_in": max_age}, headers={"Cache-Control": "no-store"})
    session_cookie.set(response, token, max_age)
    return response


@public_router.post("/admin/api/login")
async def proxy_login(request: Request) -> Response:
    """Proxy login requests to the API service and start a cookie session on success.

    The bearer never reaches the browser's script: it goes into the HttpOnly session cookie,
    which lives no longer than the session, and the body carries only ``expires_in``. Any
    session cookie the browser already holds is not presented to catalog-api.
    """
    try:
        sanitised_body = await _validated_json_body(request)
    except json.JSONDecodeError:
        return JSONResponse(content={"detail": "Malformed JSON in request body"}, status_code=400)
    response = await _proxy_request(request, "POST", catalog_admin_contract.ADMIN_LOGIN_PATH, content=sanitised_body, authenticate=False)
    if response.status_code != 200:
        return response
    return _signed_in_response(bytes(response.body))


@public_router.post("/admin/api/logout")
async def proxy_logout(request: Request) -> Response:
    """Proxy logout requests to the API service.

    The console's cached session for this bearer is dropped whatever catalog-api answers, so the
    next gated request re-validates instead of riding the cache for up to 30 seconds after a
    revocation, and a session cookie is cleared with it, so the browser is signed out even
    when catalog-api did not confirm.
    """
    authorization = request_authorization(request)
    from_cookie = session_cookie.carried_by(request)
    try:
        response = await _proxy_request(request, "POST", catalog_admin_contract.ADMIN_LOGOUT_PATH)
    finally:
        session_gate.forget(authorization)
    if from_cookie and response.status_code != 401:  # _proxy_request already cleared it on a 401.
        session_cookie.clear(response)
    return response


@router.get("/admin/api/extractions")
async def proxy_list_extractions(request: Request) -> Response:
    """Proxy extraction list requests to the API service."""
    return await _proxy_request(request, "GET", catalog_admin_contract.ADMIN_EXTRACTIONS_PATH)


@router.get("/admin/api/extractions/{extraction_id}")
async def proxy_get_extraction(extraction_id: str, request: Request) -> Response:
    """Proxy extraction detail requests to the API service."""
    if not _validate_path_segment(extraction_id):
        return Response(content=b'{"detail":"Invalid extraction ID"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_PATH.format(extraction_id=extraction_id)
    return await _proxy_request(request, "GET", path)


async def _proxy_trigger_for_source(request: Request, source: str) -> Response:
    """Forward a trigger to catalog-api with ``source`` set by the route, never by the caller.

    An empty body (an older browser tab) is accepted and gets the route's source. A body that
    names a different source is rejected with 400 rather than silently overridden.
    """
    if source not in catalog_admin_contract.ADMIN_EXTRACTION_TRIGGER_SOURCES:
        msg = f"Unsupported extraction source: {source}"
        raise ValueError(msg)
    try:
        sanitised_body = await _validated_json_body(request)
    except json.JSONDecodeError:
        return JSONResponse(content={"detail": "Malformed JSON in request body"}, status_code=400)
    parsed = json.loads(sanitised_body) if sanitised_body else {}
    if not isinstance(parsed, dict):
        return JSONResponse(content={"detail": "Request body must be a JSON object"}, status_code=400)
    requested = parsed.get("source", source)
    if requested != source:
        return JSONResponse(content={"detail": f"Body source {requested!r} conflicts with this route's source {source!r}"}, status_code=400)
    parsed["source"] = source
    payload = json.dumps(parsed, separators=(",", ":")).encode()
    return await _proxy_request(request, "POST", catalog_admin_contract.ADMIN_EXTRACTION_TRIGGER_PATH, content=payload)


@router.post("/admin/api/extractions/trigger")
async def proxy_trigger(request: Request) -> Response:
    """Proxy Discogs extraction trigger requests to the API service."""
    return await _proxy_trigger_for_source(request, "discogs")


@router.post("/admin/api/extractions/trigger-musicbrainz")
async def proxy_trigger_musicbrainz(request: Request) -> Response:
    """Proxy MusicBrainz extraction trigger requests to the API service."""
    return await _proxy_trigger_for_source(request, "musicbrainz")


# ---------------------------------------------------------------------------
# Phase 2 — User Activity & Storage proxy routes
# ---------------------------------------------------------------------------


@router.get("/admin/api/users/stats")
async def proxy_user_stats(request: Request) -> Response:
    """Proxy user stats requests to the API service."""
    return await _proxy_request(request, "GET", catalog_admin_contract.ADMIN_USER_STATS_PATH)


@router.get("/admin/api/users/sync-activity")
async def proxy_sync_activity(request: Request) -> Response:
    """Proxy sync activity requests to the API service."""
    return await _proxy_request(request, "GET", catalog_admin_contract.ADMIN_SYNC_ACTIVITY_PATH)


@router.get("/admin/api/storage")
async def proxy_storage(request: Request) -> Response:
    """Proxy storage utilization requests to the API service."""
    return await _proxy_request(request, "GET", catalog_admin_contract.ADMIN_STORAGE_PATH)


@router.post("/admin/api/dlq/purge/{queue}")
async def proxy_dlq_purge(queue: str, request: Request) -> Response:
    """Proxy DLQ purge requests to the API service."""
    if not _validate_path_segment(queue):
        return Response(content=b'{"detail":"Invalid queue name"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_DLQ_PURGE_PATH.format(queue=queue)
    return await _proxy_request(request, "POST", path)


# ---------------------------------------------------------------------------
# Phase 4 — Audit Log proxy route
# ---------------------------------------------------------------------------


@router.get("/admin/api/audit-log")
async def proxy_audit_log(
    request: Request,
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    action: str | None = Query(default=None, pattern=r"^[a-z][a-z0-9_.]+$"),
    admin_id: str | None = Query(default=None, pattern=r"^[a-f0-9-]+$"),
) -> Response:
    """Proxy audit log requests to the API service."""
    params: dict[str, str] = {}
    if page is not None:
        params["page"] = str(page)
    if page_size is not None:
        params["page_size"] = str(page_size)
    if action is not None:
        params["action"] = action
    if admin_id is not None:
        params["admin_id"] = admin_id
    return await _proxy_request(request, "GET", catalog_admin_contract.ADMIN_AUDIT_LOG_PATH, params=params)


# ---------------------------------------------------------------------------
# Identity Ledger proxy routes (design ADR 0016 section 7, concept D)
# ---------------------------------------------------------------------------

# The promoted contract's `events` and `kinds` for the ledger operations. catalog-api rejects
# anything else with 422; matching them here keeps a malformed value from leaving the console.
LEDGER_EVENTS = (
    "linked_at_ingest",
    "reattached",
    "promoted",
    "reverted",
    "evidence_attached",
    "alias_closed",
    "link_added",
    "link_changed",
    "link_removed",
)
LEDGER_KINDS = ("release", "master", "artist", "label")
# catalog-api's MAX_EDITION_LIMIT for one work.
LEDGER_MAX_EDITION_LIMIT = 500

# An RFC 3339 instant with an explicit offset, as catalog-api's AwareDatetime requires.
_RFC3339 = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?(Z|[+-]([01]\d|2[0-3]):[0-5]\d)$"


@router.get("/admin/api/identity/ledger/works")
async def proxy_identity_ledger_works(
    request: Request,
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    event: str | None = Query(default=None, pattern=f"^({'|'.join(LEDGER_EVENTS)})$"),
    since: str | None = Query(default=None, pattern=_RFC3339),
    until: str | None = Query(default=None, pattern=_RFC3339),
) -> Response:
    """Proxy the Identity Ledger works list to the API service."""
    params: dict[str, str] = {}
    if page is not None:
        params["page"] = str(page)
    if page_size is not None:
        params["page_size"] = str(page_size)
    if event is not None:
        params["event"] = event
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    return await _proxy_request(request, "GET", catalog_admin_contract.ADMIN_IDENTITY_LEDGER_WORKS_PATH, params=params)


@router.get("/admin/api/identity/ledger/works/{work_id}")
async def proxy_identity_ledger_work(
    work_id: str,
    request: Request,
    as_of: str | None = Query(default=None, pattern=_RFC3339),
    edition_limit: int | None = Query(default=None, ge=1, le=LEDGER_MAX_EDITION_LIMIT),
) -> Response:
    """Proxy one work's Identity Ledger, read at ``as_of``, to the API service."""
    if not _validate_path_segment(work_id):
        return Response(content=b'{"detail":"Invalid work ID"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_IDENTITY_LEDGER_WORK_PATH.format(work_id=work_id)
    params: dict[str, str] = {}
    if as_of is not None:
        params["as_of"] = as_of
    if edition_limit is not None:
        params["edition_limit"] = str(edition_limit)
    return await _proxy_request(request, "GET", path, params=params)


@router.get("/admin/api/identity/ledger/density")
async def proxy_identity_ledger_density(
    request: Request,
    kind: str | None = Query(default=None, pattern=f"^({'|'.join(LEDGER_KINDS)})$"),
) -> Response:
    """Proxy the Identity Ledger's per-month event density to the API service."""
    params: dict[str, str] = {}
    if kind is not None:
        params["kind"] = kind
    return await _proxy_request(request, "GET", catalog_admin_contract.ADMIN_IDENTITY_LEDGER_DENSITY_PATH, params=params)


# ---------------------------------------------------------------------------
# Phase 5 — Extraction Analysis proxy routes
# ---------------------------------------------------------------------------


@router.get("/admin/api/extraction-analysis/versions")
async def proxy_ea_versions(request: Request) -> Response:
    """Proxy extraction analysis versions list to the API service."""
    return await _proxy_request(request, "GET", catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_VERSIONS_PATH)


@router.get("/admin/api/extraction-analysis/{version}/summary")
async def proxy_ea_summary(version: str, request: Request) -> Response:
    """Proxy extraction analysis summary for a single version."""
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_SUMMARY_PATH.format(version=version)
    return await _proxy_request(request, "GET", path)


@router.get("/admin/api/extraction-analysis/{version}/violations/{record_id}")
async def proxy_ea_violation_detail(version: str, record_id: str, request: Request) -> Response:
    """Proxy extraction analysis violation record detail — must be registered before the bare violations route."""
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")
    if not _validate_path_segment(record_id):
        return Response(content=b'{"detail":"Invalid record ID"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_VIOLATION_PATH.format(version=version, record_id=record_id)
    return await _proxy_request(request, "GET", path)


@router.get("/admin/api/extraction-analysis/{version}/skipped")
async def proxy_ea_skipped(
    version: str,
    request: Request,
    entity_type: str | None = Query(default=None, pattern=r"^[a-z-]+$"),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=200),
) -> Response:
    """Proxy extraction analysis skipped records list with optional query param filtering."""
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_SKIPPED_PATH.format(version=version)
    params: dict[str, str] = {}
    if entity_type is not None:
        params["entity_type"] = entity_type
    if page is not None:
        params["page"] = str(page)
    if page_size is not None:
        params["page_size"] = str(page_size)
    return await _proxy_request(request, "GET", path, params=params)


@router.get("/admin/api/extraction-analysis/{version}/violations")
async def proxy_ea_violations(
    version: str,
    request: Request,
    entity_type: str | None = Query(default=None, pattern=r"^[a-z-]+$"),
    severity: str | None = Query(default=None, pattern=r"^(error|warning|info)$"),
    rule: str | None = Query(default=None, pattern=r"^[a-zA-Z0-9_-]+$"),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=200),
) -> Response:
    """Proxy extraction analysis violations list with optional query param filtering."""
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_VIOLATIONS_PATH.format(version=version)
    params: dict[str, str] = {}
    if entity_type is not None:
        params["entity_type"] = entity_type
    if severity is not None:
        params["severity"] = severity
    if rule is not None:
        params["rule"] = rule
    if page is not None:
        params["page"] = str(page)
    if page_size is not None:
        params["page_size"] = str(page_size)
    return await _proxy_request(request, "GET", path, params=params)


@router.get("/admin/api/extraction-analysis/{version}/parsing-errors")
async def proxy_ea_parsing_errors(version: str, request: Request) -> Response:
    """Proxy extraction analysis parsing errors — uses longer timeout as parsing can be slow."""
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_PARSING_ERRORS_PATH.format(version=version)
    return await _proxy_request(request, "GET", path, timeout=60.0)


@router.get("/admin/api/extraction-analysis/{version}/compare/{other_version}")
async def proxy_ea_compare(version: str, other_version: str, request: Request) -> Response:
    """Proxy extraction analysis version comparison."""
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")
    if not _validate_path_segment(other_version):
        return Response(content=b'{"detail":"Invalid other_version"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_COMPARE_PATH.format(version=version, other_version=other_version)
    return await _proxy_request(request, "GET", path)


@router.post("/admin/api/extraction-analysis/{version}/prompt-context")
async def proxy_ea_prompt_context(version: str, request: Request) -> Response:
    """Proxy extraction analysis prompt context generation."""
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_PROMPT_CONTEXT_PATH.format(version=version)
    try:
        sanitised_body = await _validated_json_body(request)
    except json.JSONDecodeError:
        return JSONResponse(content={"detail": "Malformed JSON in request body"}, status_code=400)
    return await _proxy_request(request, "POST", path, content=sanitised_body)


@router.post("/admin/api/extraction-analysis/{version}/generate-ai-prompt")
async def proxy_ea_generate_ai_prompt(version: str, request: Request) -> Response:
    """Proxy AI-powered prompt generation — may take longer due to Claude API call."""
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")
    path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_GENERATE_AI_PROMPT_PATH.format(version=version)
    try:
        sanitised_body = await _validated_json_body(request)
    except json.JSONDecodeError:
        return JSONResponse(content={"detail": "Malformed JSON in request body"}, status_code=400)
    return await _proxy_request(request, "POST", path, content=sanitised_body, timeout=120.0)


# ---------------------------------------------------------------------------
# Media mapping coverage — a console-side composition over the extraction
# analysis summary route (for the version's provider) and catalog-api's
# `admin_unmapped_media` route (for the reading itself), not a 1:1 proxy of a
# single catalog-api endpoint.
#
# catalog-api now exposes GET /api/admin/media/unmapped?provider=…&limit=…
# (contract operation `admin_unmapped_media`), which ranks the raw media names
# the ADR 0007 canonical taxonomy did not recognise. Loaders keep those names
# in each release's `media` block under `unmapped`, split into `formats` (the
# provider's own format names) and `descriptions` (their qualifiers), so the
# reading comes from stored data and exists for BOTH providers — MusicBrainz
# included, which is why this view no longer reports it as unobservable.
#
# The summary route is still called first, but only to learn which provider
# the requested extraction version belongs to; the coverage numbers themselves
# come entirely from the new route.
# ---------------------------------------------------------------------------

# Sources this view can read, mapped 1:1 onto the upstream `provider` query
# parameter. A summary reporting anything else is reported as unavailable
# rather than guessed at.
_MEDIA_MAPPING_PROVIDERS = frozenset({"discogs", "musicbrainz"})
# How many top names to request upstream. Well inside the route's 1-200 range,
# and the number of rows the admin UI's table is sized for.
_MEDIA_MAPPING_TOP_N = 10


def _compose_media_mapping_coverage(summary: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    """Shape one `admin_unmapped_media` payload into this view's coverage response.

    *coverage* is the upstream body verbatim: `provider`, `media_tagged_releases`,
    `releases_with_unmapped`, `unmapped_rate`, `limit`, and a `top_unmapped` list of
    `{kind, name, releases}` ordered by `releases` descending. *summary* supplies only
    the version/source context — no number in the result is derived from it.

    **This deliberately does NOT agree with the violations-based reading it replaces,
    and the numbers will move for Discogs on the same fixtures.** The old reading
    counted discogs-ingestion `format-not-recognized` data-quality violations for one
    extraction version: its release count was the distinct `record_id` set across those
    violations, its ranking was by how many violations carried each `field_value`, and
    its denominator was the total release-entity violations of *any* rule for that
    version. The new reading counts stored releases instead — `releases_with_unmapped`
    is how many rows in the provider's release table carry at least one unmapped name
    in `releases.media`, ranked by release count, against a denominator of every
    media-tagged release. Three consequences worth stating plainly:

    1. It is a release count, not an occurrence count. The taxonomy de-duplicates each
       release's `unmapped` list, so a release naming one unrecognised format twice
       contributes 1 here where the rules engine could emit two violations.
    2. It covers description qualifiers as well as format names (`kind` distinguishes
       them); the old rule only ever fired on `formats.format.@name`.
    3. It is table-wide, not scoped to the `{version}` in this route's path. The version
       selects the provider and nothing more, so the reading reflects everything loaded
       for that provider rather than one extraction run.

    `truncated` means the ranked list hit the requested limit and more distinct unmapped
    names exist upstream. The counts themselves are always exact — unlike the old
    pagination cap, which truncated the aggregation input as well.
    """
    top_unmapped = coverage.get("top_unmapped") or []
    limit = coverage.get("limit", _MEDIA_MAPPING_TOP_N)
    return {
        "available": True,
        "version": summary.get("version"),
        "source": summary.get("source"),
        "provider": coverage.get("provider"),
        "media_tagged_releases": coverage.get("media_tagged_releases", 0),
        "releases_with_unmapped_media": coverage.get("releases_with_unmapped", 0),
        "unmapped_rate": coverage.get("unmapped_rate", 0.0),
        "limit": limit,
        "top_unmapped_formats": [
            {
                "kind": entry.get("kind"),
                "name": entry.get("name"),
                "count": entry.get("releases", 0),
            }
            for entry in top_unmapped
        ],
        "truncated": isinstance(limit, int) and len(top_unmapped) >= limit,
    }


def _media_mapping_unavailable(version: str, source: str | None) -> dict[str, Any]:
    """Coverage response for a source the upstream media-coverage route cannot read.

    Both shipped providers are readable now; this is the fallback for a summary whose
    `source` is missing or is something neither loader produces, where naming a
    `provider` upstream would be a guess.
    """
    return {
        "available": False,
        "version": version,
        "source": source,
        "reason": (
            f"Media mapping coverage is read per provider from catalog-api, which recognises "
            f"{' and '.join(sorted(_MEDIA_MAPPING_PROVIDERS))}. This extraction version reports "
            f"source {source!r}, so there is no provider to read coverage for."
        ),
    }


@router.get("/admin/api/extraction-analysis/{version}/media-mapping-coverage")
async def proxy_ea_media_mapping_coverage(version: str, request: Request) -> Response:
    """Media/format mapping coverage for the provider behind one extraction version.

    Calls the existing summary route to learn the version's `source`, then reads the
    coverage numbers for that provider from catalog-api's `admin_unmapped_media` route.
    Both Discogs and MusicBrainz return a real reading; see
    `_compose_media_mapping_coverage` for why the Discogs numbers differ from the
    violations-based reading this replaced. An unreadable source returns
    `available: false` with a reason instead of a misleading zero; an upstream non-200
    (including a 5xx) is passed through unchanged and an unreachable API yields 502.
    """
    if not _validate_path_segment(version):
        return Response(content=b'{"detail":"Invalid version"}', status_code=400, media_type="application/json")

    summary_path = catalog_admin_contract.ADMIN_EXTRACTION_ANALYSIS_SUMMARY_PATH.format(version=version)
    async with httpx.AsyncClient(timeout=30.0) as client:
        summary_response = await _proxy_request(request, "GET", summary_path, client=client)
        if summary_response.status_code != 200:
            return summary_response
        summary = json.loads(bytes(summary_response.body))

        source = summary.get("source")
        if source not in _MEDIA_MAPPING_PROVIDERS:
            return JSONResponse(content=_media_mapping_unavailable(version, source))

        coverage_response = await _proxy_request(
            request,
            "GET",
            catalog_admin_contract.ADMIN_UNMAPPED_MEDIA_PATH,
            params={"provider": source, "limit": str(_MEDIA_MAPPING_TOP_N)},
            client=client,
        )
        if coverage_response.status_code != 200:
            return coverage_response
        coverage = json.loads(bytes(coverage_response.body))

    return JSONResponse(content=_compose_media_mapping_coverage(summary, coverage))
