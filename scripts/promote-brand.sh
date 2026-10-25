#!/usr/bin/env bash
set -euo pipefail

design_repo="${GROOVEMAP_DESIGN_REPO:-../design}"
expected="7abe13ad3e22e81b5437eb92acb8db998730e11a"

test -e "${design_repo}/.git"
test "$(git -C "${design_repo}" rev-parse HEAD)" = "${expected}"
test -z "$(git -C "${design_repo}" status --short)"
node "${design_repo}/brand/render.mjs" --check

find dashboard/static/brand -type f ! -name source.json -depth -delete
cp "${design_repo}"/brand/assets/* dashboard/static/brand/
EXPECTED="${expected}" uv run python - <<'PY'
import json, os
from hashlib import sha256
from pathlib import Path

brand = Path("dashboard/static/brand")
source = json.loads((brand / "source.json").read_text())
source["producer_commit"] = os.environ["EXPECTED"]
source["assets"] = {p.name: sha256(p.read_bytes()).hexdigest() for p in sorted(brand.iterdir()) if p.name != "source.json"}
(brand / "source.json").write_text(json.dumps(source, indent=2, sort_keys=True) + "\n")
PY
uv run python scripts/check-brand.py
