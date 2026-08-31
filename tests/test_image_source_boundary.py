from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).parent.parent
CHECK = ROOT / "scripts" / "check-image-source.sh"


def run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Git setup for disposable test repositories
        ["git", "-C", str(root), *arguments],  # noqa: S607 - trusted test PATH
        check=True,
        capture_output=True,
        text=True,
    )


def run_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - repository-owned test helper
        ["bash", str(CHECK), str(root)],  # noqa: S607 - trusted test PATH
        check=False,
        capture_output=True,
        text=True,
    )


def initialize_repository(root: Path) -> Path:
    run_git(root, "init", "--quiet")
    tracked = root / "README.md"
    tracked.write_text("GrooveMap operations console\n", encoding="utf-8")
    run_git(root, "add", "README.md")
    run_git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--quiet", "-m", "test")
    return tracked


def test_untracked_injected_checkout_does_not_taint_image_revision(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    injected = tmp_path / "python-libraries" / "README.md"
    injected.parent.mkdir()
    injected.write_text("Injected dependency checkout\n", encoding="utf-8")

    assert run_check(tmp_path).returncode == 0


def test_unstaged_tracked_change_blocks_image_revision(tmp_path: Path) -> None:
    tracked = initialize_repository(tmp_path)
    tracked.write_text("Modified tracked source\n", encoding="utf-8")

    result = run_check(tmp_path)

    assert result.returncode == 2
    assert "modified tracked source" in result.stderr


def test_staged_tracked_change_blocks_image_revision(tmp_path: Path) -> None:
    tracked = initialize_repository(tmp_path)
    tracked.write_text("Modified tracked source\n", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")

    result = run_check(tmp_path)

    assert result.returncode == 2
    assert "modified tracked source" in result.stderr
