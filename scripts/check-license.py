"""Validate the first-party licensing boundary and synchronized package version."""

import hashlib
import json
import re
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as source:
    project = tomllib.load(source)["project"]

version_match = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "dashboard/__init__.py").read_text(), re.MULTILINE)
assert version_match is not None
assert project["license"] == "AGPL-3.0-only"
assert not any("Proprietary" in classifier for classifier in project["classifiers"])
assert project["version"] == version_match.group(1)

legal_files = {
    "COMMERCIAL-LICENSING.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
}
assert set(project["license-files"]) == legal_files
wheels = list((ROOT / "dist").glob("groovemap_operations_console-*.whl"))
assert len(wheels) == 1
with zipfile.ZipFile(wheels[0]) as wheel:
    metadata_path = next(path for path in wheel.namelist() if path.endswith(".dist-info/METADATA"))
    metadata = Parser().parsestr(wheel.read(metadata_path).decode())
    assert metadata["License-Expression"] == "AGPL-3.0-only"
    assert set(metadata.get_all("License-File", [])) == legal_files
    for filename in legal_files:
        packaged_path = next(path for path in wheel.namelist() if path.endswith(f".dist-info/licenses/{filename}"))
        assert wheel.read(packaged_path) == (ROOT / filename).read_bytes()

package = json.loads((ROOT / "package.json").read_text())
assert package["license"] == "AGPL-3.0-only"
package_lock = json.loads((ROOT / "package-lock.json").read_text())
assert package_lock["packages"][""]["license"] == "AGPL-3.0-only"

license_bytes = (ROOT / "LICENSE").read_bytes()
assert hashlib.sha256(license_bytes).hexdigest() == "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"

dockerfile = (ROOT / "Dockerfile").read_text()
assert 'org.opencontainers.image.licenses="AGPL-3.0-only"' in dockerfile
assert 'org.opencontainers.image.source="https://github.com/groovemap-music/operations-console"' in dockerfile
assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
assert '[ "${#VCS_REF}" -eq 40 ]' in dockerfile

readme = (ROOT / "README.md").read_text()
commercial = (ROOT / "COMMERCIAL-LICENSING.md").read_text()
notice = (ROOT / "NOTICE").read_text()
assert "AGPL permits commercial use" in readme
assert "Outside code contributions are temporarily paused" in readme
assert "does not require separate commercial terms" in commercial
assert "relicensing-capable contributor license agreement" in commercial
assert "MIT License" in notice
assert "PolyForm Noncommercial License 1.0.0" in notice
assert "does not grant trademark rights" in notice

dependency_notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
with (ROOT / "uv.lock").open("rb") as source:
    locked_packages = {package["name"]: package["version"] for package in tomllib.load(source)["package"]}
for dependency in ("certifi", "orjson", "psycopg", "psycopg-binary"):
    assert f"`{dependency}` {locked_packages[dependency]}" in dependency_notices
for obligation in ("LGPL-3.0-only", "LGPL-2.1-or-later", "MPL-2.0", "Apache-2.0", "MIT"):
    assert obligation in dependency_notices
assert "applies only to first-party rights" in dependency_notices
assert "does not change any third-party license" in dependency_notices
assert "does not grant trademark rights" in dependency_notices

js_license_overrides = {
    "istanbul-lib-coverage": "BSD-3-Clause",
    "istanbul-lib-instrument": "BSD-3-Clause",
    "istanbul-lib-report": "BSD-3-Clause",
    "istanbul-reports": "BSD-3-Clause",
}
for dependency, version in package["devDependencies"].items():
    locked = package_lock["packages"][f"node_modules/{dependency}"]
    assert locked["version"] == version
    assert locked["license"] == js_license_overrides.get(dependency, "MIT")
    assert f"`{dependency}` {version}" in dependency_notices

assert "BSD-3-Clause" in dependency_notices

assert "`lightningcss` 1.32.0 and 1.33.0" in dependency_notices

justfile = (ROOT / "Justfile").read_text()
assert "pip-licenses --format=json | uv run python scripts/check_dependency_licenses.py" in justfile
