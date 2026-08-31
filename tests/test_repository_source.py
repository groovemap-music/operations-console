from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from scripts.repository_source import RepositorySourceError, tracked_files, tracked_tree_text


if TYPE_CHECKING:
    from pathlib import Path


RETIRED_NAME = "discogs" + "ography"


def run_git(root: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603 - fixed Git setup for disposable test repositories
        ["git", "-C", str(root), *arguments],  # noqa: S607 - trusted test PATH
        check=True,
        capture_output=True,
    )


def initialize_repository(root: Path) -> None:
    run_git(root, "init", "--quiet")


def test_tracked_retired_branding_remains_visible_to_policy(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(f"Retired project: {RETIRED_NAME}\n", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")

    assert RETIRED_NAME in tracked_tree_text(tmp_path, excluded_parts=set()).casefold()


def test_untracked_injected_dependency_is_outside_source_boundary(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("GrooveMap operations console\n", encoding="utf-8")
    injected_readme = tmp_path / "python-libraries" / "README.md"
    injected_readme.parent.mkdir()
    injected_readme.write_text(f"Historical dependency content: {RETIRED_NAME}\n", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")

    assert RETIRED_NAME not in tracked_tree_text(tmp_path, excluded_parts=set()).casefold()
    assert tracked_files(tmp_path) == (readme,)


def test_git_inventory_failure_is_reported_and_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RepositorySourceError, match=r"cannot obtain Git-tracked source inventory .*not a git repository"):
        tracked_tree_text(tmp_path, excluded_parts=set())


def test_missing_tracked_source_is_reported_and_fails_closed(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("GrooveMap operations console\n", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")
    readme.unlink()

    with pytest.raises(RepositorySourceError, match=r"tracked source is unavailable: README\.md"):
        tracked_tree_text(tmp_path, excluded_parts=set())
