"""Verify promoted deterministic brand assets and their source provenance."""

import json
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "dashboard/static/brand"
DESIGN_REPOSITORY = "https://github.com/groovemap-music/design"
DESIGN_COMMIT = "7abe13ad3e22e81b5437eb92acb8db998730e11a"
source = json.loads((BRAND / "source.json").read_text())

assert source["producer_repository"] == DESIGN_REPOSITORY
assert source["producer_commit"] == DESIGN_COMMIT
assert set(source["assets"]) == {path.name for path in BRAND.iterdir() if path.is_file() and path.name != "source.json"}
for name, expected in source["assets"].items():
    assert sha256((BRAND / name).read_bytes()).hexdigest() == expected, name
