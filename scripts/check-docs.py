"""Validate local Markdown links and Mermaid fence structure."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FILES = (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")))
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def heading_anchors(text: str) -> set[str]:
    """Return GitHub-style anchors for the headings used in these documents."""
    anchors: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if match is None:
            continue
        slug = match.group(1).strip().lower()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"\s+", "-", slug)
        anchors.add(slug)
    return anchors


def validate_links(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    for raw_target in LINK.findall(text):
        target = unquote(raw_target.strip("<>"))
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        relative, _, fragment = target.partition("#")
        resolved = path if not relative else (path.parent / relative).resolve()
        if not resolved.exists():
            errors.append(f"{path.relative_to(ROOT)}: missing link target {target}")
            continue
        if fragment and resolved.suffix == ".md":
            anchors = heading_anchors(resolved.read_text())
            if fragment.lower() not in anchors:
                errors.append(f"{path.relative_to(ROOT)}: missing heading #{fragment} in {resolved.relative_to(ROOT)}")
    return errors


def validate_mermaid(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    blocks = re.findall(r"```mermaid\s*\n(.*?)```", text, flags=re.DOTALL)
    if text.count("```mermaid") != len(blocks):
        errors.append(f"{path.relative_to(ROOT)}: unclosed Mermaid fence")
    for index, block in enumerate(blocks, start=1):
        first_line = next((line.strip() for line in block.splitlines() if line.strip()), "")
        if not first_line.startswith(("flowchart ", "sequenceDiagram")):
            errors.append(f"{path.relative_to(ROOT)}: Mermaid block {index} has no supported diagram declaration")
    return errors


def main() -> int:
    errors: list[str] = []
    for path in MARKDOWN_FILES:
        text = path.read_text()
        errors.extend(validate_links(path, text))
        errors.extend(validate_mermaid(path, text))
    if errors:
        raise SystemExit("\n".join(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
