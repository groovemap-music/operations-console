"""Pytest configuration for dashboard tests."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, create_autospec

import httpx
import pytest
from common import AsyncResilientNeo4jDriver, AsyncResilientPostgreSQL
from neo4j import AsyncResult, AsyncSession
from playwright.sync_api import Browser, Page, sync_playwright
from psycopg import AsyncConnection, AsyncCursor

from dashboard.config import DashboardConfig
from tests.e2e_console import CATALOG_PORT, CATALOG_URL, CONSOLE_PORT, SECURE_COOKIE
from tests.fake_catalog_api import FakeCatalogApi


if TYPE_CHECKING:
    from collections.abc import Iterator


E2E_PROJECTS = {"chromium", "firefox", "webkit", "iphone", "ipad"}
_DEVICE_CONTEXT: dict[str, Any] = {}

# Every standard OpenTelemetry variable that changes what either signal records or exports —
# tracing's sampler, exporter, and propagator selection included. Mirrors
# groovemap-runtime's own test isolation: the telemetry suites assert on what an in-memory
# provider recorded, so they must not inherit the ambient OpenTelemetry configuration that a
# developer's shell or CI runner may already have set.
_OTEL_ENVIRONMENT = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_METRICS_EXEMPLAR_FILTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_PROPAGATORS",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_SDK_DISABLED",
    "OTEL_SERVICE_NAME",
    "OTEL_TRACES_EXPORTER",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
)


@pytest.fixture(autouse=True)
def isolated_otel_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every test against a known-empty OpenTelemetry configuration."""
    for name in _OTEL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item) -> Iterator[None]:
    """Expose the call outcome to fixture teardown for failure artifacts."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


E2E_CONSOLE_URL = f"http://127.0.0.1:{CONSOLE_PORT}"
E2E_CATALOG_PORT = CATALOG_PORT
E2E_CATALOG_URL = CATALOG_URL


def _server_log(cwd: str, port: int) -> Path:
    """The file one test server writes its output to.

    Never an unread pipe: a pipe holds 64 KiB, and once a whole run's access log fills it the
    server blocks on its next log line, so the last tests of the run hang or time out.
    """
    directory = Path(cwd) / "test-results" / os.environ.get("GROOVEMAP_E2E_PROJECT", "chromium")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"server-{port}.log"


def _start_server(module: str, port: int, env: dict[str, str], cwd: str) -> subprocess.Popen[str]:
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Port {port} already has a listener; refusing to start {module} on it or to trust whatever answers there.")
    with _server_log(cwd, port).open("w") as log:
        return subprocess.Popen(  # noqa: S603  # Fixed test-server modules, never external input.
            [sys.executable, "-m", "tests.serve_test_app", module, str(port)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            start_new_session=True,  # Its own process group, so teardown reaps anything it spawned.
        )


def _wait_until_ready(process: subprocess.Popen[str], health_url: str, project_root: str, log: Path, token: str) -> None:
    """Poll a public health route for up to 20 seconds, failing with the server's output."""
    for retry in range(40):
        if process.poll() is not None:
            raise RuntimeError(
                f"Server for {health_url} died after {retry} retries.\n"
                f"Working directory: {project_root}\nPython executable: {sys.executable}\n"
                f"OUTPUT ({log}):\n{log.read_text() or '(empty)'}\n"
            )
        # A healthy answer only counts when it carries this run's token: a server left behind on
        # the port by another run would answer too.
        try:
            response = httpx.get(health_url, timeout=1.0)
            if response.status_code == 200 and response.headers.get("x-e2e-run") == token and process.poll() is None:
                return
        except Exception:  # noqa: S110
            pass  # Server might not be ready yet
        time.sleep(0.5)
    _stop_server(process)
    raise RuntimeError(f"Server for {health_url} failed to start after 20 seconds.\nOUTPUT ({log}):\n{log.read_text() or '(empty)'}\n")


def _stop_server(process: subprocess.Popen[str]) -> None:
    """End the server's whole process group."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


@pytest.fixture(scope="session")
def test_server() -> Any:
    """Start a fake catalog-api and the console test server for E2E tests.

    The console reaches the fake over a real socket, exactly as it reaches catalog-api, and
    validates every session against it with the positive cache disabled
    (``SESSION_CACHE_SECONDS=0``), so a revocation or outage the suite triggers takes effect on
    the next request instead of up to 30 seconds later. The session cookie stays Secure, its
    default, except in the WebKit projects (see ``tests.e2e_console.SECURE_COOKIE``).
    """
    project_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # Ensure Python can find the project modules
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = str(E2E_CATALOG_PORT)
    env["SESSION_CACHE_SECONDS"] = "0"
    token = uuid.uuid4().hex
    env["E2E_RUN_TOKEN"] = token
    env["SESSION_COOKIE_SECURE"] = "true" if SECURE_COOKIE else "false"

    catalog = _start_server("tests.fake_catalog_api:create_app", E2E_CATALOG_PORT, env, project_root)
    try:
        _wait_until_ready(catalog, f"{E2E_CATALOG_URL}/__fake__/health", project_root, _server_log(project_root, E2E_CATALOG_PORT), token)
        console = _start_server("tests.dashboard_test_app:create_test_app", CONSOLE_PORT, env, project_root)
        try:
            _wait_until_ready(console, f"{E2E_CONSOLE_URL}/health", project_root, _server_log(project_root, CONSOLE_PORT), token)
            yield
        finally:
            _stop_server(console)
    finally:
        _stop_server(catalog)


@pytest.fixture
def fake_catalog() -> Iterator[FakeCatalogApi]:
    """Point the console's session gate at an in-process fake catalog-api for one test."""
    from dashboard.session_auth import gate

    saved = dict(vars(gate))
    catalog = FakeCatalogApi()
    gate.configure("catalog-api.test", 8004, transport=httpx.ASGITransport(app=catalog.app))
    yield catalog
    vars(gate).clear()
    vars(gate).update(saved)


@pytest.fixture(scope="session")
def browser_context_args(browser: Browser) -> dict[str, Any]:
    """Configure browser context for testing."""
    del browser
    defaults: dict[str, Any] = {
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
        "locale": "en-US",
        "timezone_id": "UTC",
        "record_video_dir": f"test-results/{os.environ.get('GROOVEMAP_E2E_PROJECT', 'chromium')}/videos",
        "record_video_size": {"width": 1280, "height": 720},
    }
    defaults.update(_DEVICE_CONTEXT)
    return defaults


@pytest.fixture(scope="session")
def browser_type_launch_args() -> dict[str, Any]:
    """Configure browser launch arguments for headless mode."""
    return {
        "headless": True,  # Always run headless
        "timeout": 30000,  # 30 second timeout for browser launch
    }


@pytest.fixture(scope="session")
def browser(browser_type_launch_args: dict[str, Any]) -> Iterator[Browser]:
    """Launch the selected desktop browser or standard emulated WebKit device."""
    project = os.environ.get("GROOVEMAP_E2E_PROJECT", "chromium")
    if project not in E2E_PROJECTS:
        raise ValueError(f"Unknown GROOVEMAP_E2E_PROJECT: {project}")
    with sync_playwright() as playwright:
        engine_name = "webkit" if project in {"iphone", "ipad"} else project
        engine = getattr(playwright, engine_name)
        launch_args = dict(browser_type_launch_args)
        if engine_name == "chromium":
            launch_args["args"] = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        _DEVICE_CONTEXT.clear()
        if project in {"iphone", "ipad"}:
            device_name = "iPhone 15" if project == "iphone" else "iPad (gen 11)"
            _DEVICE_CONTEXT.update(playwright.devices[device_name])
            _DEVICE_CONTEXT.pop("default_browser_type", None)
        instance = engine.launch(**launch_args)
        yield instance
        instance.close()


def _note_e2e_cleanup_error(errors: list[Exception], phase: str, error: Exception) -> None:
    """Retain every independent teardown failure with its diagnostic phase."""
    error.add_note(f"operations-console E2E teardown phase: {phase}")
    errors.append(error)


def _finalize_e2e_page(
    request: pytest.FixtureRequest,
    instance: Page,
    context: Any,
    artifact_root: Path,
    project: str,
    node_digest: str,
) -> None:
    """Collect evidence and close the context even when a page has crashed."""
    errors: list[Exception] = []
    failed = bool(getattr(request.node, "rep_call", None) and request.node.rep_call.failed)
    retain_diagnostics = failed

    try:
        try:
            coverage = instance.evaluate("globalThis.__coverage__ || null")
            if coverage is not None:
                raw_root = Path("coverage/e2e/raw") / project
                raw_root.mkdir(parents=True, exist_ok=True)
                (raw_root / f"{node_digest}.json").write_text(json.dumps(coverage, sort_keys=True) + "\n")
        except Exception as error:
            retain_diagnostics = True
            _note_e2e_cleanup_error(errors, "coverage", error)

        if retain_diagnostics:
            try:
                instance.screenshot(path=artifact_root / f"{node_digest}.png", full_page=True)
            except Exception as error:
                _note_e2e_cleanup_error(errors, "screenshot", error)
    finally:
        try:
            try:
                trace_path = artifact_root / f"{node_digest}-trace.zip" if retain_diagnostics else None
                if trace_path:
                    context.tracing.stop(path=trace_path)
                else:
                    context.tracing.stop()
            except Exception as error:
                _note_e2e_cleanup_error(errors, "trace", error)
        finally:
            try:
                # Closing the context finalizes Playwright's recorded video.
                context.close()
            except Exception as error:
                _note_e2e_cleanup_error(errors, "context/video", error)

    if errors:
        raise ExceptionGroup("operations-console E2E teardown failed", errors)


@pytest.fixture
def page(request: pytest.FixtureRequest, browser: Browser, browser_context_args: dict[str, Any]) -> Iterator[Page]:
    """Create an isolated page and retain coverage plus failure diagnostics."""
    project = os.environ.get("GROOVEMAP_E2E_PROJECT", "chromium")
    artifact_root = Path("test-results") / project
    artifact_root.mkdir(parents=True, exist_ok=True)
    node_digest = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    context = browser.new_context(**browser_context_args)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    instance = context.new_page()
    yield instance
    _finalize_e2e_page(request, instance, context, artifact_root, project, node_digest)


@pytest.fixture
def mock_dashboard_config() -> DashboardConfig:
    """Create a mock dashboard configuration for testing."""
    return DashboardConfig(
        neo4j_host="neo4j://localhost:7687",
        neo4j_username="test",
        neo4j_password="test",
        postgres_host="localhost:5432",
        postgres_username="test",
        postgres_password="test",
        postgres_database="test",
    )


@pytest.fixture
def dashboard_mock_neo4j_result() -> MagicMock:
    """Create an await-faithful Neo4j result restricted to the driver API."""
    result = create_autospec(AsyncResult, instance=True, spec_set=True)
    result.data.return_value = [{"count": 10}]
    result.single.return_value = None
    return result


@pytest.fixture
def dashboard_mock_neo4j_session(dashboard_mock_neo4j_result: MagicMock) -> MagicMock:
    """Create an async-context-manager Neo4j session restricted to the driver API."""
    session = create_autospec(AsyncSession, instance=True, spec_set=True)
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.run.return_value = dashboard_mock_neo4j_result
    return session


@pytest.fixture
def dashboard_mock_neo4j_driver(dashboard_mock_neo4j_session: MagicMock) -> MagicMock:
    """Create a resilient Neo4j driver restricted to its runtime interface."""
    driver = create_autospec(AsyncResilientNeo4jDriver, instance=True, spec_set=True)
    driver.session.return_value = dashboard_mock_neo4j_session
    return driver


@pytest.fixture
def dashboard_mock_httpx_client() -> MagicMock:
    """Create a mock httpx client."""
    mock = MagicMock()

    # Create different responses based on URL
    async def mock_get(url: str, **kwargs: Any) -> AsyncMock:
        response = AsyncMock()
        response.raise_for_status = AsyncMock()

        if "/health" in url:
            # Mock health endpoint responses
            response.status_code = 200
            response.json = lambda: {
                "status": "healthy",
                "current_task": "Processing",
                "progress": 0.5,
                "timestamp": "2024-01-01T00:00:00+00:00",
            }
        elif "metrics/detailed" in url:
            response.status_code = 200
            response.text = (
                'rabbitmq_detailed_queue_messages{vhost="/",queue="groovemap-discogs-graphinator-artists"} 5\n'
                'rabbitmq_detailed_queue_messages_ready{vhost="/",queue="groovemap-discogs-graphinator-artists"} 3\n'
                'rabbitmq_detailed_queue_messages_unacked{vhost="/",queue="groovemap-discogs-graphinator-artists"} 2\n'
                'rabbitmq_detailed_queue_consumers{vhost="/",queue="groovemap-discogs-graphinator-artists"} 1\n'
            )
        else:
            response.status_code = 404
            response.json = lambda: {"error": "Not found"}

        return response

    mock.get = AsyncMock(side_effect=mock_get)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)

    return mock


@pytest.fixture
def dashboard_mock_psycopg_cursor() -> MagicMock:
    """Create an await-faithful psycopg cursor restricted to its runtime API."""
    cursor = create_autospec(AsyncCursor, instance=True, spec_set=True)
    cursor.__aenter__.return_value = cursor
    cursor.__aexit__.return_value = None
    cursor.fetchone.return_value = (10,)
    return cursor


@pytest.fixture
def dashboard_mock_psycopg_connection(dashboard_mock_psycopg_cursor: MagicMock) -> MagicMock:
    """Create a psycopg connection whose cursor is an async context manager."""
    connection = create_autospec(AsyncConnection, instance=True, spec_set=True)
    connection.cursor.return_value = dashboard_mock_psycopg_cursor
    return connection


@pytest.fixture
def dashboard_mock_psycopg_connect(dashboard_mock_psycopg_connection: MagicMock) -> MagicMock:
    """Create the resilient PostgreSQL wrapper used by the dashboard."""
    resilient = create_autospec(AsyncResilientPostgreSQL, instance=True, spec_set=True)
    resilient.get_connection.return_value = dashboard_mock_psycopg_connection
    return resilient
