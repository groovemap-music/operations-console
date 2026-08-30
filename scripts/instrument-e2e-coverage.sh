#!/usr/bin/env bash
set -uo pipefail

restore_on_failure() {
  instrument_status=$?
  trap - EXIT INT TERM
  if [[ "${instrument_status}" -ne 0 ]]; then
    node scripts/restore-e2e-sources.mjs || true
  fi
  exit "${instrument_status}"
}

trap restore_on_failure EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
node scripts/instrument-e2e-coverage.mjs || exit $?
trap - EXIT INT TERM
