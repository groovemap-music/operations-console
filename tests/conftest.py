"""Pytest configuration for dashboard tests."""

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from dashboard.config import DashboardConfig


E2E_PROJECTS = {"chromium", "firefox", "webkit", "iphone", "ipad"}
_DEVICE_CONTEXT: dict[str, Any] = {}


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item) -> Iterator[None]:
    """Expose the call outcome to fixture teardown for failure artifacts."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture(scope="session")
def test_server() -> Any:
    """Start the test dashboard server for E2E tests.

    This fixture is only used when running E2E tests.
    """
    # Get project root directory
    project_root = str(Path(__file__).resolve().parents[1])

    # Start server as subprocess
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # Ensure Python can find the project modules
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")

    server_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.dashboard_test_app:create_test_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8003",
            "--log-level",
            "info",  # Changed from warning to info for better debugging
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=project_root,  # Set working directory to project root
    )

    # Wait for server to be ready
    max_retries = 40  # 20 seconds total
    server_started = False
    for retry in range(max_retries):
        try:
            response = httpx.get("http://127.0.0.1:8003/api/metrics", timeout=1.0)
            if response.status_code == 200:
                server_started = True
                break
        except Exception:  # noqa: S110
            pass  # Server might not be ready yet

        # Check if process died
        if server_process.poll() is not None:
            stdout, stderr = server_process.communicate()
            error_msg = f"Server process died after {retry} retries.\n"
            error_msg += f"Working directory: {project_root}\n"
            error_msg += f"Python executable: {sys.executable}\n"
            if stdout:
                error_msg += f"STDOUT:\n{stdout}\n"
            else:
                error_msg += "STDOUT: (empty)\n"
            if stderr:
                error_msg += f"STDERR:\n{stderr}\n"
            else:
                error_msg += "STDERR: (empty)\n"
            raise RuntimeError(error_msg)

        time.sleep(0.5)

    # If we reach here, server didn't start successfully
    if not server_started:
        server_process.terminate()
        try:
            stdout, stderr = server_process.communicate(timeout=2)
            error_msg = "Test server failed to start after 20 seconds.\n"
            error_msg += f"Working directory: {project_root}\n"
            error_msg += f"Python executable: {sys.executable}\n"
            if stdout:
                error_msg += f"STDOUT:\n{stdout}\n"
            else:
                error_msg += "STDOUT: (empty)\n"
            if stderr:
                error_msg += f"STDERR:\n{stderr}\n"
            else:
                error_msg += "STDERR: (empty)\n"
            raise RuntimeError(error_msg)
        except subprocess.TimeoutExpired as e:
            server_process.kill()
            raise RuntimeError("Test server failed to start and didn't terminate cleanly") from e

    yield

    # Cleanup
    server_process.terminate()
    try:
        server_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_process.kill()
        server_process.wait()


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
        amqp_connection="amqp://test:test@localhost:5672/",
        neo4j_host="neo4j://localhost:7687",
        neo4j_username="test",
        neo4j_password="test",
        postgres_host="localhost:5432",
        postgres_username="test",
        postgres_password="test",
        postgres_database="test",
        rabbitmq_username="test",
        rabbitmq_password="test",
    )


@pytest.fixture
def dashboard_mock_amqp_connection() -> AsyncMock:
    """Create a mock AMQP connection for dashboard tests."""
    mock = AsyncMock()
    mock.close = AsyncMock()

    # Mock channel
    mock_channel = AsyncMock()
    mock.channel = AsyncMock(return_value=mock_channel)

    return mock


@pytest.fixture
def dashboard_mock_neo4j_driver() -> MagicMock:
    """Create a mock Neo4j driver for dashboard tests."""
    mock = MagicMock()
    mock.close = AsyncMock()

    # Mock session
    mock_session = AsyncMock()
    mock.session = MagicMock(return_value=mock_session)

    # Mock query results
    mock_result = AsyncMock()
    mock_result.data = AsyncMock(return_value=[{"count": 10}])
    mock_session.run = AsyncMock(return_value=mock_result)

    return mock


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
        elif "api/queues" in url:
            # Mock RabbitMQ management API
            response.status_code = 200
            response.json = lambda: [
                {
                    "name": "groovemap-discogs-graphinator-artists",
                    "messages": 5,
                    "messages_ready": 3,
                    "messages_unacknowledged": 2,
                    "consumers": 1,
                    "message_stats": {"ack_details": {"rate": 1.5}},
                }
            ]
        else:
            response.status_code = 404
            response.json = lambda: {"error": "Not found"}

        return response

    mock.get = AsyncMock(side_effect=mock_get)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)

    return mock


@pytest.fixture
def dashboard_mock_psycopg_connect() -> AsyncMock:
    """Create a mock PostgreSQL connection for dashboard tests."""
    mock_conn = AsyncMock()

    # Mock cursor
    mock_cursor = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=(10,))
    mock_cursor.close = AsyncMock()

    mock_conn.cursor = AsyncMock(return_value=mock_cursor)
    mock_conn.close = AsyncMock()

    return mock_conn
