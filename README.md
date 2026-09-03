# GrooveMap operations console

Privileged administration and monitoring console for GrooveMap. It presents service, queue, Neo4j, PostgreSQL, extraction, audit, and data-quality views, and proxies authenticated operator actions to `catalog-api`.

This source is licensed under the [GNU Affero General Public License v3.0 only](LICENSE). The AGPL permits commercial use subject to its terms, including its source-availability requirements. Optional, separately negotiated commercial terms are available as an alternative for users who do not want to use the software under the AGPL; see [Commercial licensing](COMMERCIAL-LICENSING.md).

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
- `just e2e-setup` / `just e2e` — install Chromium, Firefox, and WebKit, then run the desktop and emulated mobile browser matrix.
- `just image` — build and inspect the non-root production image.
- `just release-dry-run` — build checksums, SBOM, notices, and provenance without publishing.
- `just bump-preview` — preview the Conventional Commits version and changelog without changing files.

## Runtime

```bash
uv run operations-console
```

The service listens on port 8003. Configure the RabbitMQ, Neo4j, PostgreSQL, Redis, and catalog API connections through the deployment repository. Supply credentials through its secret mechanism; never commit them here.

The container build injects its full Git revision into both the OCI metadata and the console's visible source/legal link. A released console therefore links to the corresponding source tree for the exact running revision. Dependency and first-party trademark notices are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [NOTICE](NOTICE).

### Metrics

The console exports OpenTelemetry metrics through `groovemap-runtime`'s `common.telemetry` bootstrap; there is no Prometheus `/metrics` scrape endpoint. Metrics push over OTLP/HTTP-protobuf and are configured entirely through the standard OpenTelemetry environment variables:

- `OTEL_EXPORTER_OTLP_ENDPOINT` — collector base URL, for example `http://otel-collector:4318`. When unset, telemetry stays a no-op and the service behaves exactly as it does without it.
- `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` — metrics-only endpoint override.
- `OTEL_METRICS_EXPORTER` — `otlp` (default) or `none` to force export off.
- `OTEL_METRIC_EXPORT_INTERVAL` — push interval in milliseconds.
- `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` — override the resource's `service.name` and add attributes such as `service.namespace` and `deployment.environment.name`.

HTTP server and client metrics (`http.server.request.duration`, `http.client.request.duration`) are emitted automatically once the `otel-http` extra is installed. The console additionally records `groovemap.console.websocket.connections` (an up-down counter of active WebSocket clients) and `groovemap.console.poll.duration` (a histogram, keyed by `target` — a polled service key, `rabbitmq`, `neo4j`, `postgres`, or `loop` for the 2-second collection loop itself — and `outcome`).

## Repository boundary

`catalog-api` owns authentication and operator endpoints. `catalog-ingestion` owns catalog-event exchange and queue naming. `database-schema` owns datastore compatibility. This repository consumes immutable promoted copies of those contracts; it does not import producer source or require sibling checkouts at runtime.

Canonical editable branding belongs to the public [`groovemap-music/design`](https://github.com/groovemap-music/design) repository. `dashboard/static/brand/` contains promoted deterministic render outputs with recorded hashes and the full design source commit. Run `scripts/promote-brand.sh` against the expected clean design checkout to update them; set `GROOVEMAP_DESIGN_REPO` when that checkout is not at `../design`. Use of the GrooveMap name and logos is governed separately by the design repository's [trademark-use policy](https://github.com/groovemap-music/design/blob/59c9fd3c8bbdfa676e0b7bb3d463fc766c1f3c0d/TRADEMARKS.md).

## Releases

The console is independently versioned from PEP 621 metadata with Commitizen and approved `v$version` annotated tags. Migration and release-readiness verification are deliberately non-publishing; the hosted workflow only responds to an explicitly created version tag.

See the [documentation index](docs/README.md) for operator guidance, dashboard design
records, and source-history provenance.

## Contributions and licensing

Outside code contributions are temporarily paused. Do not submit code or documentation changes until a relicensing-capable contributor license agreement has been approved and published. Bug reports and other feedback that do not include proposed code remain welcome through the issue tracker.

The current license, optional alternative-licensing boundary, and prior-license history are recorded in [LICENSE](LICENSE), [COMMERCIAL-LICENSING.md](COMMERCIAL-LICENSING.md), and [NOTICE](NOTICE).
