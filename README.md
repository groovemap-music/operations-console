# GrooveMap operations console

A web console for GrooveMap operators. It shows live service, queue, Neo4j, PostgreSQL, extraction, audit, and data-quality views, and forwards authenticated operator actions (such as triggering an extraction or purging a dead-letter queue) to `catalog-api`.

It is for two audiences: operators who run a GrooveMap stack (start with the [administrator guide](docs/admin-guide.md)) and developers who work on the console itself (this page).

## Quick start

You need the tools pinned in `.mise.toml`. The console starts even when its backing services are down and reports them as unavailable, but the views are empty without Neo4j, PostgreSQL, RabbitMQ, and `catalog-api` to read from.

```bash
mise install
just setup

export NEO4J_HOST=localhost NEO4J_USERNAME=neo4j NEO4J_PASSWORD=<password>
export POSTGRES_HOST=localhost POSTGRES_USERNAME=<user> POSTGRES_PASSWORD=<password> POSTGRES_DATABASE=<database>
export SESSION_COOKIE_SECURE=false   # only for plain-HTTP access from a host other than localhost
uv run operations-console
```

Open `http://localhost:8003`. Signing in needs a running `catalog-api` and an admin account; see the [administrator guide](docs/admin-guide.md#creating-an-admin-account). `GET /health` answers without a session.

This source is licensed under the [GNU Affero General Public License v3.0 only](LICENSE). The AGPL permits commercial use subject to its terms, including its source-availability requirements. Optional, separately negotiated commercial terms are available as an alternative for users who do not want to use the software under the AGPL; see [Commercial licensing](COMMERCIAL-LICENSING.md).

## Development

Prerequisites are pinned in `.mise.toml`. Python dependencies are locked in `uv.lock`; the Tailwind build is pinned in `package-lock.json`. After the quick start above, run `just check` before proposing a change.

The stable repository interface is:

- `just setup` — install the locked Python and Node environments.
- `just check` — run the authoritative pre-merge gate.
- `just test` — run non-browser tests with coverage.
- `just js-test` — run the browser JavaScript unit tests.
- `just coverage` — produce Python and JavaScript coverage reports.
- `just secret-scan` — scan Git history and the working tree without running unrelated checks.
- `just build` — build deterministic CSS, wheel, and source distribution.
- `just e2e-setup` / `just e2e` — install Chromium, Firefox, and WebKit, then run the desktop and emulated mobile browser matrix.
- `just image` — build and inspect the non-root production image.
- `just release-dry-run` — build checksums, SBOM, notices, and provenance without publishing.
- `just bump-preview` — preview the Conventional Commits version and changelog without changing files.

## Runtime

The service listens on port 8003. In production the deployment repository supplies its RabbitMQ, Neo4j, PostgreSQL, and catalog API connections; credentials come from its secret mechanism and are never committed here. The container runs the same `operations-console` entry point and writes logs to `/logs/dashboard.log`; when that path is not writable (a local run without `/logs`), logging stays on the console with one warning. Set `LOG_FILE` to choose another path, or empty to disable the file. The exact environment and `_FILE` interfaces are listed in the [configuration guide](docs/configuration.md).

The container build injects its full Git revision into both the OCI metadata and the console's visible source/legal link. A released console therefore links to the corresponding source tree for the exact running revision. Dependency and first-party trademark notices are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [NOTICE](NOTICE).

### Telemetry

The console pushes OpenTelemetry metrics and traces over OTLP and serves no Prometheus `/metrics` endpoint. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to turn export on. Settings, metric names, the Neo4j gauges, and spans are in [telemetry](docs/telemetry.md).

## Repository boundary

`catalog-api` owns authentication and operator endpoints. `discogs-ingestion` and `musicbrainz-ingestion` independently own their source-specific catalog-event exchanges and queue contracts. `database-schema` owns datastore compatibility. This repository consumes immutable promoted copies of those contracts; it does not import producer source or require sibling checkouts at runtime.

Canonical editable branding belongs to the public [`groovemap-music/design`](https://github.com/groovemap-music/design) repository. `dashboard/static/brand/` contains promoted deterministic render outputs with recorded hashes and the full design source commit. Run `scripts/promote-brand.sh` against the expected clean design checkout to update them; set `GROOVEMAP_DESIGN_REPO` when that checkout is not at `../design`. Use of the GrooveMap name and logos is governed separately by the design repository's [trademark-use policy](https://github.com/groovemap-music/design/blob/41be7cbe738f40df0c75ad7065475e91d6cc2aa4/TRADEMARKS.md).

## Releases

The console is independently versioned from PEP 621 metadata with Commitizen and approved `v$version` annotated tags. Migration and release-readiness verification are deliberately non-publishing; the hosted workflow only responds to an explicitly created version tag.

See the [documentation index](docs/README.md) for operator guidance, architecture, configuration, telemetry, and source-history provenance.

## Contributions and licensing

Outside code contributions are temporarily paused. Do not submit code or documentation changes until a relicensing-capable contributor license agreement has been approved and published. Bug reports and other feedback that do not include proposed code remain welcome through the issue tracker.

The current license, optional alternative-licensing boundary, and prior-license history are recorded in [LICENSE](LICENSE), [COMMERCIAL-LICENSING.md](COMMERCIAL-LICENSING.md), and [NOTICE](NOTICE).
