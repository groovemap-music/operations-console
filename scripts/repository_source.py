"""First-party source inventory helpers for repository policy checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class RepositorySourceError(RuntimeError):
    """Raised when the repository source boundary cannot be trusted."""


def tracked_files(root: Path) -> tuple[Path, ...]:
    """Return the deterministic Git-tracked file inventory below ``root``."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed Git query with no user-controlled arguments
            ["git", "-C", str(root), "ls-files", "--cached", "-z"],  # noqa: S607 - trusted CI PATH
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise RepositorySourceError(f"cannot obtain Git-tracked source inventory: {error}") from error

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise RepositorySourceError(f"cannot obtain Git-tracked source inventory (git exited {result.returncode}){suffix}")

    if result.stdout and not result.stdout.endswith(b"\0"):
        raise RepositorySourceError("cannot trust Git-tracked source inventory: output is not NUL-terminated")

    files: list[Path] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise RepositorySourceError(f"cannot trust Git-tracked source inventory: unsafe path {relative!s}")
        files.append(root / relative)
    return tuple(sorted(files, key=lambda path: os.fsencode(path.relative_to(root))))


def tracked_tree_text(root: Path, *, excluded_parts: set[str]) -> str:
    """Read policy-relevant text from tracked first-party files only."""
    contents: list[str] = []
    for path in tracked_files(root):
        relative = path.relative_to(root)
        if any(part in excluded_parts for part in relative.parts):
            continue
        if not path.is_file():
            raise RepositorySourceError(f"tracked source is unavailable: {relative}")
        try:
            contents.append(path.read_text(errors="ignore"))
        except OSError as error:
            raise RepositorySourceError(f"cannot inspect tracked source {relative}: {error}") from error
    return "\n".join(contents)
