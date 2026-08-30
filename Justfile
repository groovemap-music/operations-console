set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

setup:
    uv sync --dev --frozen
    npm ci --ignore-scripts

source-check:
    uvx --from ruff==0.16.4 ruff format --check .
    uvx --from ruff==0.16.4 ruff check .
    python scripts/check-contracts.py
    python scripts/check-brand.py
    python scripts/check-repository-compliance.py
    npm ci --ignore-scripts
    npm run build:css
    npm test
    test -s dashboard/static/tailwind.css
    gitleaks git --redact --no-banner
    gitleaks dir . --redact --no-banner

check: source-check typecheck test build install-check license-check release-artifacts bump-preview

format:
    uv run ruff format .
    uv run ruff check --fix .

typecheck:
    uv run mypy

test:
    uv run pytest -m 'not e2e' --cov=dashboard --cov-report=term-missing --cov-report=xml

js-test:
    npm test

coverage: test
    npm run test:coverage

e2e-setup:
    uv run playwright install chromium firefox webkit

e2e-instrument:
    bash scripts/instrument-e2e-coverage.sh

e2e-run:
    bash scripts/run-e2e-matrix.sh

e2e-post:
    node scripts/finalize-e2e-coverage.mjs

e2e:
    bash scripts/e2e-with-coverage.sh

web-build:
    npm ci --ignore-scripts
    npm run build:css
    test -s dashboard/static/tailwind.css

build: web-build
    uv build --out-dir dist --clear

install-check: build
    bash scripts/install-check.sh

license-check:
    uv run python scripts/check-license.py
    uv run pip-licenses --format=json | uv run python scripts/check_dependency_licenses.py

audit:
    uv run pip-audit
    npm audit --audit-level=high

prepare-runtime-wheel:
    bash scripts/prepare-runtime-wheel.sh

brand:
    python scripts/check-brand.py

brand-promote:
    bash scripts/promote-brand.sh

image: build prepare-runtime-wheel
    bash scripts/build-image.sh
    docker run --rm --entrypoint /app/.venv/bin/python operations-console:local -c 'import dashboard.dashboard'
    test "$(docker run --rm --entrypoint /usr/bin/id operations-console:local -u):$(docker run --rm --entrypoint /usr/bin/id operations-console:local -g)" = "1000:1000"
    uv run python scripts/check-image-metadata.py operations-console:local

bump-preview:
    uv run cz bump --dry-run --changelog --yes --check-consistency

# Update local version metadata and changelog only; do not commit, tag, push, or publish.
bump:
    uv run cz bump --version-files-only --changelog --yes --check-consistency
    uv lock

release-artifacts: build install-check
    bash scripts/release-dry-run.sh

release-dry-run: check
