# Operations-console architecture

The operations console is the human-facing observability and privileged-administration surface for GrooveMap. It reads service, queue, Neo4j, and PostgreSQL health, presents live and historical metrics, and proxies authenticated operator requests to `catalog-api`. It does not own authentication, extraction, message consumption, or datastore schema.

```mermaid
flowchart LR
    Operator[Operator browser] -->|HTTP and WebSocket :8003| Console[operations-console]
    Console -->|health reads| Services[GrooveMap services]
    Console -->|management metrics| RabbitMQ[(RabbitMQ)]
    Console -->|statistics| Neo4j[(Neo4j)]
    Console -->|statistics| Postgres[(PostgreSQL)]
    Console -->|authenticated admin proxy| Catalog[catalog-api]
    Catalog -->|extraction and audit operations| Platform[Catalog platform]
```

## Repository boundary

`catalog-api` owns login, authorization, audit persistence, and privileged endpoints. `catalog-ingestion` owns extraction and catalog-event exchange naming. The four loader and enricher repositories own their runtime consumers. `database-schema` owns datastore compatibility, and `deployment` owns endpoints, credentials, and runtime composition.

This repository consumes promoted, immutable copies of the catalog API, catalog-event, and persistence contracts under `contracts/`. Each source record identifies the producer commit and content hashes. Contract validation fails when a promoted document and generated binding diverge.

## Browser and API surfaces

The public monitoring page receives live metrics over WebSocket and exposes read-only health endpoints. The administrator page sends bearer-authenticated requests through a constrained proxy whose allowed paths come from the promoted `catalog-api` contract. Queue names are derived from the promoted exchange prefixes; arbitrary queue and URL input is rejected.

```mermaid
sequenceDiagram
    participant Browser
    participant Console as operations-console
    participant API as catalog-api
    Browser->>Console: Sign in or privileged operation
    Console->>API: Forward allowed path, token, and trusted proxy metadata
    API-->>Console: Authorized response and audit result
    Console-->>Browser: Sanitized response
```

## Runtime lifecycle

Startup initializes RabbitMQ, Neo4j, PostgreSQL, and the background metrics collector. A partial dependency failure is reported in health data instead of being presented as healthy. Shutdown snapshots connected WebSockets before awaiting close operations, cancels the collector, and closes each client without holding the connection-set lock across network I/O.

The image is `operations-console`, runs as numeric user `1000:1000`, and embeds its exact forty-character source revision in OCI metadata and the browser-visible corresponding-source link.
