#!/usr/bin/env bash
set -uo pipefail

projects=(chromium firefox webkit iphone ipad)
status=0
for project in "${projects[@]}"; do
  echo "Running operations-console E2E project: ${project}"
  if ! GROOVEMAP_E2E_PROJECT="${project}" uv run pytest -m e2e; then
    status=1
  fi
done
exit "${status}"
