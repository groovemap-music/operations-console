"""Regression coverage for browser instrumentation and teardown failures."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import _finalize_e2e_page


ROOT = Path(__file__).resolve().parents[1]


def test_partial_instrumentation_restores_first_source_when_second_is_unavailable(tmp_path: Path) -> None:
    target_root = tmp_path / "repository"
    admin_source = target_root / "dashboard/static/admin.js"
    admin_source.parent.mkdir(parents=True)
    original = b"function admin() { return 1; }\n"
    admin_source.write_bytes(original)

    result = subprocess.run(
        ["/bin/bash", "scripts/instrument-e2e-coverage.sh"],
        cwd=ROOT,
        env={**os.environ, "GROOVEMAP_E2E_ROOT": str(target_root)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "dashboard/static/dashboard.js" in result.stderr
    assert admin_source.read_bytes() == original
    assert not (target_root / ".build/e2e-original").exists()


class _ClosedPage:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def evaluate(self, expression: str) -> None:
        assert expression == "globalThis.__coverage__ || null"
        self.calls.append("coverage")
        raise RuntimeError("page crashed before coverage collection")

    def screenshot(self, **kwargs: Any) -> None:
        assert kwargs["full_page"] is True
        self.calls.append("screenshot")
        raise RuntimeError("page is closed")


class _Tracing:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail
        self.path: Path | None = None

    def stop(self, *, path: Path | None = None) -> None:
        self.calls.append("trace")
        self.path = path
        if self.fail:
            raise RuntimeError("trace finalization failed")


class _Context:
    def __init__(self, calls: list[str], *, trace_fails: bool = False) -> None:
        self.calls = calls
        self.tracing = _Tracing(calls, fail=trace_fails)

    def close(self) -> None:
        self.calls.append("context/video")


def _failed_request() -> Any:
    return SimpleNamespace(node=SimpleNamespace(rep_call=SimpleNamespace(failed=True)))


def test_closed_page_still_attempts_screenshot_trace_and_video_finalization(tmp_path: Path) -> None:
    calls: list[str] = []
    context = _Context(calls)

    with pytest.raises(ExceptionGroup) as raised:
        _finalize_e2e_page(
            _failed_request(),
            _ClosedPage(calls),  # type: ignore[arg-type]
            context,
            tmp_path,
            "chromium",
            "closed-page",
        )

    assert calls == ["coverage", "screenshot", "trace", "context/video"]
    assert context.tracing.path == tmp_path / "closed-page-trace.zip"
    notes = [note for error in raised.value.exceptions for note in (error.__notes__ or [])]
    assert notes == [
        "operations-console E2E teardown phase: coverage",
        "operations-console E2E teardown phase: screenshot",
    ]


def test_trace_failure_cannot_skip_context_and_video_finalization(tmp_path: Path) -> None:
    calls: list[str] = []
    context = _Context(calls, trace_fails=True)
    page = SimpleNamespace(
        evaluate=lambda expression: None,
        screenshot=lambda **kwargs: calls.append("screenshot"),
    )

    with pytest.raises(ExceptionGroup) as raised:
        _finalize_e2e_page(
            _failed_request(),
            page,  # type: ignore[arg-type]
            context,
            tmp_path,
            "firefox",
            "trace-failure",
        )

    assert calls == ["screenshot", "trace", "context/video"]
    assert raised.value.exceptions[0].__notes__ == ["operations-console E2E teardown phase: trace"]
