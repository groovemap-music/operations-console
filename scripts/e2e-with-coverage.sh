#!/usr/bin/env bash
set -uo pipefail

restore_sources() {
  node scripts/restore-e2e-sources.mjs
}

cleanup_on_exit() {
  e2e_status=$?
  trap - EXIT INT TERM
  restore_status=0
  restore_sources || restore_status=$?
  if [[ "${e2e_status}" -ne 0 ]]; then
    exit "${e2e_status}"
  fi
  exit "${restore_status}"
}

trap cleanup_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
bash scripts/instrument-e2e-coverage.sh || exit $?
status=0
bash scripts/run-e2e-matrix.sh || status=$?
node scripts/finalize-e2e-coverage.mjs || status=$?
exit "${status}"
