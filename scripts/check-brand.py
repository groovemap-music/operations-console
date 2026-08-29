"""Verify promoted deterministic brand assets and their source provenance."""

import json
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "dashboard/static/brand"
DESIGN_REPOSITORY = "https://github.com/groovemap-music/design"
DESIGN_COMMIT = "59c9fd3c8bbdfa676e0b7bb3d463fc766c1f3c0d"
source = json.loads((BRAND / "source.json").read_text())

assert source["producer_repository"] == DESIGN_REPOSITORY
assert source["producer_commit"] == DESIGN_COMMIT
assert set(source["assets"]) == {path.name for path in BRAND.iterdir() if path.is_file() and path.name != "source.json"}
for name, expected in source["assets"].items():
    assert sha256((BRAND / name).read_bytes()).hexdigest() == expected, name
