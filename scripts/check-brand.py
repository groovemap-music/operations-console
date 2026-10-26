"""Verify promoted deterministic brand assets and their source provenance."""

import json
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "dashboard/static/brand"
DESIGN_REPOSITORY = "https://github.com/groovemap-music/design"
DESIGN_COMMIT = "41be7cbe738f40df0c75ad7065475e91d6cc2aa4"
source = json.loads((BRAND / "source.json").read_text())

assert source["producer_repository"] == DESIGN_REPOSITORY
assert source["producer_commit"] == DESIGN_COMMIT
assert set(source["assets"]) == {path.name for path in BRAND.iterdir() if path.is_file() and path.name != "source.json"}
for name, expected in source["assets"].items():
    assert sha256((BRAND / name).read_bytes()).hexdigest() == expected, name

FONTS = ROOT / "dashboard/static/fonts"
font_source = json.loads((FONTS / "source.json").read_text())
assert font_source["font"] == "Jura"
assert font_source["license"] == "OFL-1.1"
assert font_source["package"] == "@fontsource/jura"
assert set(font_source["files"]) == {path.name for path in FONTS.iterdir() if path.is_file() and path.name != "source.json"}
assert font_source["license_file"] in font_source["files"]
for name, expected in font_source["files"].items():
    assert sha256((FONTS / name).read_bytes()).hexdigest() == expected, name
package_lock = json.loads((ROOT / "package-lock.json").read_text())
assert package_lock["packages"]["node_modules/@fontsource/jura"]["version"] == font_source["package_version"]
