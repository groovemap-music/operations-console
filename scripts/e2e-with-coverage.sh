#!/usr/bin/env bash
set -uo pipefail

node scripts/instrument-e2e-coverage.mjs || exit $?
status=0
bash scripts/run-e2e-matrix.sh || status=$?
node scripts/finalize-e2e-coverage.mjs || status=$?
exit "${status}"
