# Changelog

All notable changes to this repository will be recorded here by Commitizen from
Conventional Commits.

## v0.2.0 (2026-10-31)

### Feat

- **ui**: adopt the Label web brand with warm surfaces, wordmark, light/dark toggle and brand charts
- **brand**: self-host Jura from @fontsource/jura with its OFL license recorded
- **dashboard**: move the console from Lagoon to brass and oxblood
- **dashboard**: show the ledger's refreshed_at and its being-built state
- **contracts**: promote catalog-api c31703b with refreshed_at on the ledger payloads
- **dashboard**: add the Ledger tab for the Identity Ledger
- **proxy**: proxy the three Identity Ledger routes
- **contracts**: promote catalog-api e4423d4 with the Identity Ledger routes
- **dashboard**: keep the admin session in an HttpOnly cookie
- **dashboard**: detect stalled and no-consumer queues on the server
- **brand**: promote Design 7abe13ad (Degrees mark, Lagoon palette)
- **dashboard**: poll and show health for every long-running service
- **console**: send source on both trigger paths, promote catalog-api 9066cb7, show run source
- **console**: add a desktop-only pipeline topology drill-down
- **console**: add a run ETA headline and a Run progress view
- **console**: serve run progress from VictoriaMetrics
- **console**: stream a server-side event feed to the System Event Log
- **console**: deep-link services, queues, and datastores to Grafana and traces
- **console**: add an attention bar and derive the header status from it
- **console**: serve firing Grafana alerts through a session-gated route
- **dashboard**: make the entity x consumer matrix the Overview's primary view
- **dashboard**: draw the Overview pipelines from /api/topology
- **console**: serve queue and system history from VictoriaMetrics with one range control
- **dashboard**: report uncomputable queue rates as null, not zero
- **console**: serve one authenticated shell at / and gate every data route and the WebSocket
- **dashboard**: read live queue metrics from the RabbitMQ Prometheus endpoint
- **telemetry**: emit the Neo4j store, transaction, and count gauges
- **telemetry**: adopt the wave-2 runtime and trace each console poll
- **dashboard**: read media mapping coverage for both providers from catalog-api
- **dashboard**: show media mapping coverage in the data-quality view
- **telemetry**: adopt common.telemetry and remove prometheus_client

### Fix

- **logging**: rely on the runtime's console fallback when /logs is unwritable
- **deps**: upgrade msgpack past CVE-2026-114613
- **ui**: give the ledger minted event its own colour, clear of every Strata state
- **console**: keep the sign-out reason when an in-flight read is refused
- **tests**: write e2e server output to a log file, not an unread pipe
- **test**: expect the Degrees text tokens in the topology theme test
- **dashboard**: derive pipeline health ports from service role
- **dashboard**: treat a Valkey NOAUTH reply as alive and wrap the System Health cards
- **dashboard**: report a broken Grafana token file as unavailable, not unconfigured
- **dashboard**: add GRAFANA_API_URL as the server-side Grafana base
- **console**: wait for the run's own file-progress checkpoint
- **console**: say the System Event Log is empty instead of collapsing it
- **e2e**: freeze the live feed only once its socket is open
- **console**: label unscraped publish and ack rates as no rate data
- **console**: drop the address throttle, keep logout ungated, back off on 4503
- **console**: close the session gaps found in security review
- **dashboard**: show unavailable queue metrics, serve /api/queues from the live snapshot, drop AMQP deps
- **deps**: upgrade filelock to 4.2.1 past CVE-2026-112940
- **deps**: upgrade certifi and opentelemetry-api past PYSEC-2026-4320 and CVE-2026-114603
- **admin**: drop legacy data.redis storage payload fallback
- **deps**: synchronize npm and Python dependency notices
- **deps**: resolve audit findings that fail CI on main and every Dependabot PR (#10)
- **admin**: label storage as Valkey and drop unused Redis config
- **justfile**: run brand recipe with uv and guard against bare python
- **validation**: use pinned Python for scripts
- **telemetry**: keep the Neo4j count queries on the count store
- **ci**: accept commitizen's no-eligible-commits bump-preview state
- **ci**: use public python libraries

### Refactor

- **contracts**: promote catalog-api without the metrics-history routes
- **ci**: normalize console validation recipes
- **admin**: split dashboard responsibilities
- **admin**: consolidate proxy transport handling
- **dashboard**: clarify runtime lifecycle ownership

## v0.1.1 (2026-08-31)

### Fix

- **ci**: harden release-boundary validation and synchronize dashboard package versions

## v0.1.0 (2026-08-31)

The v0.1.0 workflow failed before publishing artifacts or images. The tag is
retained as an immutable record of that release attempt.
