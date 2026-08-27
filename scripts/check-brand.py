"""Verify promoted deterministic brand assets and their source provenance."""

import json
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "dashboard/static/brand"
source = json.loads((BRAND / "source.json").read_text())

assert source["producer_repository"] == "https://github.com/groovemap-music/infra"
assert len(source["producer_commit"]) == 40
assert set(source["assets"]) == {path.name for path in BRAND.iterdir() if path.is_file() and path.name != "source.json"}
for name, expected in source["assets"].items():
    assert sha256((BRAND / name).read_bytes()).hexdigest() == expected, name
