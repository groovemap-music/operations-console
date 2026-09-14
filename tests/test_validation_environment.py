"""Contracts for hermetic repository validation commands."""

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_default_validation_routes_python_scripts_through_uv() -> None:
    """The expanded default gate must not depend on an activated or global Python."""
    just = shutil.which("just")
    assert just is not None
    completed = subprocess.run(  # noqa: S603
        [just, "--dry-run", "check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    dry_run = f"{completed.stdout}\n{completed.stderr}"
    python_script_commands = [line for line in dry_run.splitlines() if re.search(r"(?:^|\| )(?:uv run )?python scripts/[^ ]+\.py(?: |$)", line)]

    assert python_script_commands
    assert all(re.search(r"(?:^|\| )uv run python scripts/", line) for line in python_script_commands)
