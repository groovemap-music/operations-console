"""Run one e2e test server that is provably this run's and dies with it.

Every response carries the run's ``X-E2E-Run`` token (``E2E_RUN_TOKEN``), so the fixture can tell
its own server from any other process answering on the port. The server also exits when the pytest
process that started it is gone: pytest-timeout's ``thread`` method ends the session with
``os._exit``, which skips fixture teardown, and a server started the plain way would then keep its
port for the next run.
"""

import importlib
import os
import sys
import threading
import time
from typing import Any

import uvicorn


TOKEN_HEADER = b"x-e2e-run"


def _exit_with_parent(parent_pid: int) -> None:
    while os.getppid() == parent_pid:
        time.sleep(0.5)
    os._exit(0)


def _stamped(app: Any, token: str) -> Any:
    """The ASGI app with the run token added to every HTTP response."""

    async def stamped(scope: Any, receive: Any, send: Any) -> None:
        async def stamp(message: Any) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (TOKEN_HEADER, token.encode())]}
            await send(message)

        await app(scope, receive, stamp)

    return stamped


if __name__ == "__main__":
    module, _, factory = sys.argv[1].partition(":")
    port = int(sys.argv[2])
    threading.Thread(target=_exit_with_parent, args=(os.getppid(),), daemon=True).start()
    app = _stamped(getattr(importlib.import_module(module), factory)(), os.environ["E2E_RUN_TOKEN"])
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
