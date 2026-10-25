"""The e2e server fixtures only trust, and always clean up, the servers they started."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.conftest import _server_log, _start_server, _stop_server, _wait_until_ready
from tests.e2e_console import free_port


ROOT = str(Path(__file__).resolve().parents[1])
CATALOG = "tests.fake_catalog_api:create_app"


def _env(token: str) -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": ROOT, "E2E_RUN_TOKEN": token}


def test_a_listener_already_on_the_port_fails_the_fixture_loudly() -> None:
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        with pytest.raises(RuntimeError, match="already has a listener"):
            _start_server(CATALOG, port, _env("t"), ROOT)


def test_a_server_that_does_not_carry_the_run_token_is_not_trusted() -> None:
    port = free_port()
    process = _start_server(CATALOG, port, _env("theirs"), ROOT)
    try:
        with pytest.raises(RuntimeError, match="failed to start"):
            _wait_until_ready(process, f"http://127.0.0.1:{port}/__fake__/health", ROOT, _server_log(ROOT, port), "ours")
    finally:
        _stop_server(process)


def test_a_server_with_the_run_token_is_ready() -> None:
    port = free_port()
    process = _start_server(CATALOG, port, _env("ours"), ROOT)
    try:
        _wait_until_ready(process, f"http://127.0.0.1:{port}/__fake__/health", ROOT, _server_log(ROOT, port), "ours")
    finally:
        _stop_server(process)


def test_a_session_killed_without_teardown_leaves_no_server_behind() -> None:
    """pytest-timeout's thread method ends pytest with os._exit: model that with SIGKILL on the parent."""
    port = free_port()
    code = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from tests.conftest import _start_server\n"
        "import os, time\n"
        f"p = _start_server({CATALOG!r}, {port}, dict(os.environ, E2E_RUN_TOKEN='t'), sys.argv[1])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code, ROOT], stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": ROOT})  # noqa: S603
    assert parent.stdout is not None
    server_pid = int(parent.stdout.readline())
    for _ in range(40):
        try:
            httpx.get(f"http://127.0.0.1:{port}/__fake__/health", timeout=1.0)
            break
        except httpx.HTTPError:
            time.sleep(0.25)
    parent.kill()
    parent.wait()
    for _ in range(20):
        try:
            os.kill(server_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)
    os.kill(server_pid, 9)
    pytest.fail("the test server outlived its pytest process")
