"""Prove the dashboard's file log sink rotates instead of growing unbounded.

groovemap-runtime bounds every consumer's ``setup_logging(..., log_file=...)`` file sink to
a size-capped ``RotatingFileHandler`` (see python-libraries commit 11cf764, "fix(logging):
bound application log files"). dashboard.dashboard imports and calls that same function
directly (``from common import setup_logging``; see dashboard/dashboard.py's lifespan and
main()), so exercising it here through the console's own import proves the exact handler
this service gets for `/logs/dashboard.log`, not just the library's internal call path.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

from dashboard.dashboard import setup_logging


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_dashboard_log_file_handler_is_a_size_capped_rotating_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler built for the console's log file rotates at a configured size cap."""
    monkeypatch.setenv("LOG_FILE_MAX_BYTES", "65536")
    monkeypatch.setenv("LOG_FILE_BACKUP_COUNT", "3")
    log_file = tmp_path / "dashboard.log"

    root_logger = logging.getLogger()
    previous_handlers = list(root_logger.handlers)
    previous_level = root_logger.level
    try:
        setup_logging("dashboard", log_file=log_file)

        rotating_handlers = [handler for handler in root_logger.handlers if isinstance(handler, RotatingFileHandler)]
        assert len(rotating_handlers) == 1, root_logger.handlers
        handler = rotating_handlers[0]
        assert handler.maxBytes == 65536
        assert handler.backupCount == 3
        assert handler.baseFilename == str(log_file)
    finally:
        for handler in root_logger.handlers:
            if handler not in previous_handlers:
                handler.close()
        root_logger.handlers = previous_handlers
        root_logger.setLevel(previous_level)


def test_dashboard_log_file_handler_defaults_are_bounded_not_unbounded(tmp_path: Path) -> None:
    """Without deployment overrides, the console still gets a bounded handler, never a plain FileHandler."""
    log_file = tmp_path / "dashboard.log"

    root_logger = logging.getLogger()
    previous_handlers = list(root_logger.handlers)
    previous_level = root_logger.level
    try:
        setup_logging("dashboard", log_file=log_file)

        rotating_handlers = [handler for handler in root_logger.handlers if isinstance(handler, RotatingFileHandler)]
        assert len(rotating_handlers) == 1, root_logger.handlers
        handler = rotating_handlers[0]
        assert handler.maxBytes == 100 * 1024 * 1024
        assert handler.backupCount == 5
    finally:
        for handler in root_logger.handlers:
            if handler not in previous_handlers:
                handler.close()
        root_logger.handlers = previous_handlers
        root_logger.setLevel(previous_level)
