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
    npm ci --ignore-scripts
    npm run build:css
    npm test
    test -s dashboard/static/tailwind.css
    gitleaks git --redact --no-banner
    gitleaks dir . --redact --no-banner

check: source-check typecheck test build install-check license-check bump-preview

format:
    uv run ruff format .
    uv run ruff check --fix .

typecheck:
    uv run mypy

test:
    uv run pytest -m 'not e2e' --cov=dashboard --cov-report=term-missing

js-test:
    npm test

e2e-setup:
    uv run playwright install chromium

e2e:
    uv run pytest -m e2e --browser chromium

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
    uv run pip-licenses --fail-on "GPL-2.0-only;GPL-3.0-only;AGPL-3.0-only"

audit:
    uv run pip-audit
    npm audit --audit-level=high

prepare-runtime-wheel:
    bash scripts/prepare-runtime-wheel.sh

brand:
    python scripts/check-brand.py

brand-promote:
    bash scripts/promote-brand.sh

image: prepare-runtime-wheel
    bash scripts/build-image.sh
    docker run --rm --entrypoint /app/.venv/bin/python operations-console:local -c 'import dashboard.dashboard'
    test "$(docker run --rm --entrypoint /usr/bin/id operations-console:local -u):$(docker run --rm --entrypoint /usr/bin/id operations-console:local -g)" = "1000:1000"

bump-preview:
    uv run cz bump --dry-run --changelog --yes --check-consistency

# Update local version metadata and changelog only; do not commit, tag, push, or publish.
bump:
    uv run cz bump --version-files-only --changelog --yes --check-consistency
    uv lock

release-dry-run: check
    bash scripts/release-dry-run.sh
