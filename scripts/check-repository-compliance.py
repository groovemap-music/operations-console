"""Validate repository identity, documentation, automation, and exposure policy."""

import json
import re
from pathlib import Path

from repository_source import RepositorySourceError, tracked_tree_text


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_REVISION = "833cb464507678c38ab78bd4718ce697399463e9"
PYTHON_LIBRARIES_REVISION = "455523ec388fdb9862d7aca65d9434aa7073dcb5"
E2E_PROJECTS = {"chromium", "firefox", "webkit", "iphone", "ipad"}
COVERAGE_FILES = (
    "coverage.xml",
    "coverage/javascript/lcov.info",
    "coverage/e2e/lcov.info",
)
BROWSER_COVERAGE_MAPPING = [
    {
        "project": project,
        "lcov": f"coverage/e2e/{project}/lcov.info",
        "artifacts": [f"test-results/{project}", f"coverage/e2e/raw/{project}"],
    }
    for project in ("chromium", "firefox", "webkit", "iphone", "ipad")
]


def workflow_jobs(text: str) -> set[str]:
    """Return top-level job IDs from a workflow's jobs section."""
    jobs = text.split("\njobs:\n", 1)[1]
    return set(re.findall(r"(?m)^  ([a-zA-Z0-9_-]+):\s*$", jobs))


def workflow_block_input(text: str, name: str) -> tuple[str, ...]:
    """Parse one workflow-call block scalar without accepting implicit path syntax."""
    match = re.search(rf"(?m)^      {re.escape(name)}: [|>][+-]?\n((?:        .*\n)+)", text)
    assert match is not None
    return tuple(line.removeprefix("        ") for line in match.group(1).splitlines())


ci = (ROOT / ".github/workflows/ci.yml").read_text()
assert re.search(r"(?m)^  pull_request:\s*$", ci)
assert 'cron: "0 1 * * 6"' in ci
assert 'cron: "0 4 * * 1"' in ci
assert "github.actor" not in ci
assert "dependabot" not in ci.lower()
assert "fallback-command" not in ci
assert workflow_jobs(ci) == {"required"}
ci_target = re.search(r"groovemap-music/automation/\.github/workflows/reusable-ci\.yml@([^\s]+)", ci)
assert ci_target is not None and ci_target.group(1) == AUTOMATION_REVISION
coverage_files = workflow_block_input(ci, "coverage-files")
assert coverage_files == COVERAGE_FILES
browser_mapping_lines = workflow_block_input(ci, "browser-coverage-mapping")
browser_mapping = json.loads(" ".join(browser_mapping_lines))
assert browser_mapping == BROWSER_COVERAGE_MAPPING
declared_browser_paths = [path for entry in browser_mapping for path in (entry["lcov"], *entry["artifacts"])]
assert not any(character in path for path in (*coverage_files, *declared_browser_paths) for character in "*?[]{}")
for required_input in (
    "language: mixed",
    "coverage-command: just coverage",
    "e2e-setup-command: just e2e-setup",
    "e2e-instrument-command: just e2e-instrument",
    "e2e-command: just e2e-run",
    "e2e-post-command: just e2e-post",
    "upload-codecov: true",
    "image-command: just image",
    "CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}",
):
    assert required_input in ci
assert "secrets: inherit" not in ci
for marker in (
    "requires-private-library",
    "private-library-client-id",
    "private-library-revision",
    "private_library_private_key",
    "groovemap_ci_app_client_id",
    "groovemap_ci_app_private_key",
):
    assert marker not in ci.lower()

release = (ROOT / ".github/workflows/release.yml").read_text()
assert "attestations: write" in release
release_target = re.search(r"groovemap-music/automation/\.github/workflows/reusable-release\.yml@([^\s]+)", release)
assert release_target is not None and release_target.group(1) == AUTOMATION_REVISION
for required_input in (
    "repository-name: operations-console",
    "release-command: just release-dry-run",
    "publish-image: true",
):
    assert required_input in release
assert "secrets: inherit" not in release
for marker in (
    "requires-private-library",
    "private-library-client-id",
    "private-library-revision",
    "private_library_private_key",
    "groovemap_ci_app_client_id",
    "groovemap_ci_app_private_key",
):
    assert marker not in release.lower()

pyproject = (ROOT / "pyproject.toml").read_text()
assert "https://github.com/groovemap-music/python-libraries.git" in pyproject
assert PYTHON_LIBRARIES_REVISION in pyproject

workflow_names = {path.name.lower() for path in (ROOT / ".github/workflows").iterdir()}
assert not any("renovate" in name or "claude" in name for name in workflow_names)
assert not any(path.name.lower().startswith("renovate") for path in ROOT.iterdir())

gitleaks_config = (ROOT / ".gitleaks.toml").read_text()
assert 'description = "Deterministic brand asset digests"' in gitleaks_config
assert "^dashboard/static/brand/source\\.json$" in gitleaks_config
assert '"[0-9a-f]{64}"' in gitleaks_config

matrix = (ROOT / "scripts/run-e2e-matrix.sh").read_text()
justfile = (ROOT / "Justfile").read_text()
assert "uv run playwright install --with-deps chromium firefox webkit" in justfile
projects_match = re.search(r"projects=\(([^)]+)\)", matrix)
assert projects_match is not None
assert set(projects_match.group(1).split()) == E2E_PROJECTS
instrument = (ROOT / "scripts/instrument-e2e-coverage.mjs").read_text()
instrument_guard = (ROOT / "scripts/instrument-e2e-coverage.sh").read_text()
restore = (ROOT / "scripts/e2e-coverage-sources.mjs").read_text()
finalize = (ROOT / "scripts/finalize-e2e-coverage.mjs").read_text()
assert "istanbul-lib-instrument" in instrument
assert instrument_guard.index("trap restore_on_failure EXIT") < instrument_guard.index("node scripts/instrument-e2e-coverage.mjs")
assert "restoreSources();" in instrument
assert "e2e-original" in restore
assert "Missing Istanbul coverage" in finalize
assert set(re.findall(r'"(chromium|firefox|webkit|iphone|ipad)"', finalize)) == E2E_PROJECTS
assert "finally" in finalize and "restoreSources();" in finalize

conftest = (ROOT / "tests/conftest.py").read_text()
assert "def _finalize_e2e_page(" in conftest
assert 'instance.screenshot(path=artifact_root / f"{node_digest}.png"' in conftest
assert "context.tracing.stop" in conftest
assert "context.close()" in conftest
assert "finally:" in conftest

package = json.loads((ROOT / "package.json").read_text())
for dependency in ("istanbul-lib-coverage", "istanbul-lib-instrument", "istanbul-lib-report", "istanbul-reports"):
    assert dependency in package["devDependencies"]

private_planning = (
    ROOT / ".planning",
    ROOT / "docs/superpowers/plans",
    ROOT / "docs/superpowers/specs",
)
assert not any(path.exists() for path in private_planning)
rehearsal = (ROOT / "scripts/rehearse-history-sanitization.sh").read_text()
assert "--mirror --no-local" in rehearsal
assert "filter-repo --force --invert-paths" in rehearsal
assert "remote-cutover-approved=false" in rehearsal
assert "public-visibility-approved=false" in rehearsal
assert 'gitleaks git --config "${gitleaks_config}"' in rehearsal
assert 'cd "${sanitized_worktree}"' in rehearsal
assert not re.search(r"\bgit\s+push\b", rehearsal)

readme = (ROOT / "README.md").read_text()
assert "docs/README.md" in readme
assert not readme.startswith("# GrooveMap operations console\n\nPrivate,")
active_paths = [ROOT / "README.md", *(sorted((ROOT / "docs").glob("*.md")))]
active_text = "\n".join(path.read_text() for path in active_paths)
legacy_product_name = "discogs" + "ography"
assert legacy_product_name not in active_text.lower()
assert "```mermaid" in (ROOT / "docs/architecture.md").read_text()

source = (ROOT / "dashboard/dashboard.py").read_text()
assert "GrooveMap operations-console" in source
assert 'SOURCE_REPOSITORY = "https://github.com/groovemap-music/operations-console"' in source
assert 'logger.info("🚀 Starting GrooveMap operations-console...")' in source

dockerfile = (ROOT / "Dockerfile").read_text()
assert 'org.opencontainers.image.title="operations-console"' in dockerfile
assert 'org.opencontainers.image.source="https://github.com/groovemap-music/operations-console"' in dockerfile
assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile

ignored_parts = {
    ".git",
    ".venv",
    ".build",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "coverage",
    "test-results",
}
try:
    current_tree_text = tracked_tree_text(ROOT, excluded_parts=ignored_parts)
except RepositorySourceError as error:
    raise SystemExit(str(error)) from error
assert legacy_product_name not in current_tree_text.lower()
