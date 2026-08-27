#!/usr/bin/env bash
set -euo pipefail

infra_repo="${GROOVEMAP_INFRA_REPO:-../infra}"
expected="342ee0d4d8a7290e55dfe1ad0d8fe82425ea2658"

test -d "${infra_repo}/.git"
test "$(git -C "${infra_repo}" rev-parse HEAD)" = "${expected}"
test -z "$(git -C "${infra_repo}" status --short)"
node "${infra_repo}/brand/render.mjs" --check

find dashboard/static/brand -type f ! -name source.json -depth -delete
cp "${infra_repo}"/brand/assets/* dashboard/static/brand/
python scripts/check-brand.py
