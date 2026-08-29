"""Validate the first-party licensing boundary and synchronized package version."""

import hashlib
import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as source:
    project = tomllib.load(source)["project"]

version_match = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "dashboard/__init__.py").read_text(), re.MULTILINE)
assert version_match is not None
assert project["license"] == "AGPL-3.0-only"
assert project["license-files"] == ["LICENSE", "NOTICE"]
assert not any("Proprietary" in classifier for classifier in project["classifiers"])
assert project["version"] == version_match.group(1)

package = json.loads((ROOT / "package.json").read_text())
assert package["license"] == "AGPL-3.0-only"
package_lock = json.loads((ROOT / "package-lock.json").read_text())
assert package_lock["packages"][""]["license"] == "AGPL-3.0-only"

license_bytes = (ROOT / "LICENSE").read_bytes()
assert hashlib.sha256(license_bytes).hexdigest() == "8486a10c4393cee1c25392769ddd3b2d6c242d6ec7928e1414efff7dfb2f07ef"

dockerfile = (ROOT / "Dockerfile").read_text()
assert 'org.opencontainers.image.licenses="AGPL-3.0-only"' in dockerfile

readme = (ROOT / "README.md").read_text()
commercial = (ROOT / "COMMERCIAL-LICENSING.md").read_text()
notice = (ROOT / "NOTICE").read_text()
assert "AGPL permits commercial use" in readme
assert "Outside code contributions are temporarily paused" in readme
assert "does not require separate commercial terms" in commercial
assert "relicensing-capable contributor license agreement" in commercial
assert "MIT License" in notice
assert "PolyForm Noncommercial License 1.0.0" in notice
