#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:-$(git rev-parse --show-toplevel)}"
if ! git -C "${repo_root}" diff --quiet --ignore-submodules -- ||
  ! git -C "${repo_root}" diff --cached --quiet --ignore-submodules --; then
  echo "Refusing to label an image from modified tracked source." >&2
  exit 2
fi
