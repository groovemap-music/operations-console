# GrooveMap operations console

Private, privileged administration and monitoring console for GrooveMap. It presents service, queue, Neo4j, PostgreSQL, extraction, audit, and data-quality views, and proxies authenticated operator actions to `catalog-api`.

This source is available under the [PolyForm Noncommercial License 1.0.0](LICENSE). Commercial use requires a separate license.

## Development

Prerequisites are pinned in `.mise.toml`. Python dependencies are locked in `uv.lock`; the Tailwind build is pinned in `package-lock.json`.

```bash
mise install
just setup
just check
```

The stable repository interface is:

- `just setup` — install the locked Python and Node environments.
- `just check` — run the authoritative pre-merge gate.
- `just test` — run non-browser tests with coverage.
- `just build` — build deterministic CSS, wheel, and source distribution.
- `just e2e-setup` / `just e2e` — install Chromium, then run the browser gate.
- `just image` — build and inspect the non-root production image.
- `just release-dry-run` — build checksums, SBOM, notices, and provenance without publishing.
- `just bump-preview` — preview the Conventional Commits version and changelog without changing files.

## Runtime

```bash
uv run operations-console
```

The service listens on port 8003. Configure the RabbitMQ, Neo4j, PostgreSQL, Redis, and catalog API connections through the deployment repository. Supply credentials through its secret mechanism; never commit them here.

## Repository boundary

`catalog-api` owns authentication and operator endpoints. `catalog-ingestion` owns catalog-event exchange and queue naming. `database-schema` owns datastore compatibility. This repository consumes immutable promoted copies of those contracts; it does not import producer source or require sibling checkouts at runtime.

Canonical editable branding belongs to `infra/brand`. `dashboard/static/brand/` contains promoted deterministic render outputs with recorded hashes and source commit. Run `scripts/promote-brand.sh` against the expected clean `infra` checkout to update them.

## Releases

The console is independently versioned from PEP 621 metadata with Commitizen and `v$version` annotated tags. Migration verification is deliberately non-publishing. A hosted release workflow remains disabled until a short-lived GitHub App installation token can read the private runtime repository and an approved image publishing identity exists.

See [docs/extraction.md](docs/extraction.md) for source-history provenance.
