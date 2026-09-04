# Operations-console administrator guide

## Creating an Admin Account

Admin accounts are created via the `admin-setup` CLI tool inside the API container:

```bash
docker exec -it groovemap-catalog-api admin-setup --email admin@example.com
```

`admin-setup` never accepts the password as a CLI argument (command-line
arguments are world-readable via `/proc/*/cmdline` and land in shell
history). The command above prompts interactively (input hidden, not
echoed). For scripted/non-interactive use, set `ADMIN_PASSWORD` (or
`ADMIN_PASSWORD_FILE`, pointing at a Docker secret) in the container's
environment instead.

Passwords must be at least 8 characters. If the email already exists, the password is updated.

## Listing Admin Accounts

```bash
docker exec -it groovemap-catalog-api admin-setup --list
```

## Accessing the Admin Panel

Navigate to `http://<host>:8003/admin` and log in with your admin credentials.

The monitoring dashboard at `http://<host>:8003` remains public — no login required.

## Triggering an Extraction

Click **Trigger Extraction** in the admin panel. This forces a full reprocessing of all Discogs data files:

- Downloads the latest monthly data from the Discogs S3 bucket
- Reprocesses all files regardless of existing state markers
- Publishes records to RabbitMQ for the `discogs-graph-enricher` and `discogs-sql-loader` consumers

The admin panel also supports triggering a **MusicBrainz extraction**, which downloads the latest MusicBrainz JSONL dumps and publishes records to the `groovemap-musicbrainz-{artists,labels,release-groups,releases}` exchanges for the `musicbrainz-graph-enricher` and `musicbrainz-sql-loader` consumers.

Use this when:

- A previous extraction failed and you want to retry
- You suspect data corruption and want a clean reprocess
- A new Discogs monthly dump (or MusicBrainz twice-weekly dump) has been published and you don't want to wait for the periodic check

The extraction runs asynchronously. Progress is tracked in the extraction history table.

If an extraction is already running, the trigger returns an error — wait for it to complete first.

## DLQ Management

Dead-letter queues (DLQs) collect messages that consumers failed to process. Each data type has a DLQ per consumer:

| Queue                                                             | Consumer          |
| ----------------------------------------------------------------- | ----------------- |
| `groovemap-discogs-graphinator-artists.dlq`                  | `discogs-graph-enricher`       |
| `groovemap-discogs-graphinator-labels.dlq`                   | `discogs-graph-enricher`       |
| `groovemap-discogs-graphinator-masters.dlq`                  | `discogs-graph-enricher`       |
| `groovemap-discogs-graphinator-releases.dlq`                 | `discogs-graph-enricher`       |
| `groovemap-discogs-tableinator-artists.dlq`                  | `discogs-sql-loader`           |
| `groovemap-discogs-tableinator-labels.dlq`                   | `discogs-sql-loader`           |
| `groovemap-discogs-tableinator-masters.dlq`                  | `discogs-sql-loader`           |
| `groovemap-discogs-tableinator-releases.dlq`                 | `discogs-sql-loader`           |
| `groovemap-musicbrainz-brainzgraphinator-artists.dlq`        | `musicbrainz-graph-enricher`   |
| `groovemap-musicbrainz-brainzgraphinator-labels.dlq`         | `musicbrainz-graph-enricher`   |
| `groovemap-musicbrainz-brainzgraphinator-release-groups.dlq` | `musicbrainz-graph-enricher`   |
| `groovemap-musicbrainz-brainzgraphinator-releases.dlq`       | `musicbrainz-graph-enricher`   |
| `groovemap-musicbrainz-brainztableinator-artists.dlq`        | `musicbrainz-sql-loader`       |
| `groovemap-musicbrainz-brainztableinator-labels.dlq`         | `musicbrainz-sql-loader`       |
| `groovemap-musicbrainz-brainztableinator-release-groups.dlq` | `musicbrainz-sql-loader`       |
| `groovemap-musicbrainz-brainztableinator-releases.dlq`       | `musicbrainz-sql-loader`       |

**Purging** permanently deletes all messages in a DLQ. Do this when:

- Messages are known-bad and will never succeed on retry
- After fixing the root cause and retriggering an extraction

Purging cannot be undone.

DLQ names follow the pattern `{exchange-prefix}-{consumer}-{data-type}.dlq`, using the `DISCOGS_EXCHANGE_PREFIX` and `MUSICBRAINZ_EXCHANGE_PREFIX` env vars as the base.

## Phase 3: Metrics History and Trend Analysis

### Queue and Health History Endpoints

Two new endpoints expose time-series metrics for queue depths and service health:

```http
GET /api/admin/queues/history?range=<range>
GET /api/admin/health/history?range=<range>
```

Both endpoints require admin authentication (Bearer token).

**Valid range values:**

| Range  | Description             | Data Granularity  |
| ------ | ----------------------- | ----------------- |
| `1h`   | Last 1 hour             | 5-minute buckets  |
| `6h`   | Last 6 hours            | 5-minute buckets  |
| `24h`  | Last 24 hours (default) | 15-minute buckets |
| `7d`   | Last 7 days             | 1-hour buckets    |
| `30d`  | Last 30 days            | 6-hour buckets    |
| `90d`  | Last 90 days            | 1-day buckets     |
| `365d` | Last 365 days           | 1-day buckets     |

Granularity is selected automatically based on the requested range. Omitting the `range` parameter defaults to `24h`.

### Background Metrics Collector

A background collector runs inside the API service and periodically samples queue depths and service health. Collected data is stored in PostgreSQL for historical querying.

The collector interval is controlled by the `METRICS_COLLECTION_INTERVAL` environment variable (default: 300 seconds / 5 minutes).

### New Environment Variables

| Variable                      | Default | Description                                                                              |
| ----------------------------- | ------- | ---------------------------------------------------------------------------------------- |
| `METRICS_RETENTION_DAYS`      | `366`   | How many days of metrics to retain in the database. Older rows are pruned automatically. |
| `METRICS_COLLECTION_INTERVAL` | `300`   | Seconds between each metrics collection cycle in the background collector.               |

Set these in your `docker-compose.yml` or environment file:

```dotenv
METRICS_RETENTION_DAYS=366
METRICS_COLLECTION_INTERVAL=300
```

### New Database Tables

Metrics are stored in two PostgreSQL tables:

**`queue_metrics`** — RabbitMQ queue depth snapshots:

| Column                    | Type         | Description                                      |
| ------------------------- | ------------ | ------------------------------------------------ |
| `id`                      | bigint       | Primary key (generated always as identity)       |
| `recorded_at`             | timestamptz  | When the sample was taken                        |
| `queue_name`              | varchar(100) | Name of the RabbitMQ queue                       |
| `messages_ready`          | integer      | Number of ready messages at sample time          |
| `messages_unacknowledged` | integer      | Number of unacknowledged messages at sample time |
| `consumers`               | integer      | Number of active consumers at sample time        |
| `publish_rate`            | real         | Message publish rate                             |
| `ack_rate`                | real         | Message acknowledgement rate                     |

**`service_health_metrics`** — Per-service health check results:

| Column             | Type        | Description                                             |
| ------------------ | ----------- | ------------------------------------------------------- |
| `id`               | bigint      | Primary key (generated always as identity)              |
| `recorded_at`      | timestamptz | When the sample was taken                               |
| `service_name`     | varchar(50) | Runtime service identity (for example, the aliases for `discogs-graph-enricher` and `discogs-sql-loader`) |
| `status`           | varchar(20) | Health status (`healthy`, `unhealthy`, `unknown`)       |
| `response_time_ms` | real        | Health check response time in milliseconds              |
| `endpoint_stats`   | jsonb       | Per-endpoint latency statistics (API service only)      |

Both tables are indexed on `recorded_at` for efficient range queries. Rows older than `METRICS_RETENTION_DAYS` are pruned automatically.

### Dashboard: Queue Trends and System Health Tabs

The admin panel (`http://<host>:8003/admin`) exposes two new tabs backed by the history endpoints:

- **Queue Trends** — Line charts showing message depth over time for each RabbitMQ queue. Use the range selector (1h / 6h / 24h / 7d / 30d / 90d / 365d) to zoom in or out.
- **System Health** — Status timeline showing per-service health over the selected range. Unhealthy periods are highlighted in red; response time is shown as a secondary series.

## Extraction Analysis: Media Mapping Coverage

The **Extraction Analysis** tab's single-version report includes a **Media Mapping Coverage**
panel, alongside the existing pipeline status, skipped records, and violations-by-entity
cards. It shows how many of a provider's releases carry a media name the canonical taxonomy
doesn't recognize — the signal that the taxonomy needs a new entry. Both providers are
covered: selecting a MusicBrainz version gives a real reading, not a "not observable" note.

For the provider behind the selected extraction version, the panel shows:

- **Provider** — `discogs` or `musicbrainz`, resolved from the selected version's `source`.
- **Releases with unmapped media** — releases whose stored `media` block carries at least
  one unmapped name, either a provider format name or a description qualifier.
- **Media-tagged releases** — the denominator: every release whose `media` column is
  populated.
- **Distinct unmapped names** and a table of the **top unmapped names** with a **kind**
  (`format` or `description`) and a per-name release count, so an operator can see at a
  glance which raw strings are missing from the taxonomy.
- **Unmapped rate** — unmapped releases as a share of media-tagged releases.

**Where the data comes from.** catalog-api exposes
`GET /api/admin/media/unmapped?provider=…&limit=…` (contract operation
`admin_unmapped_media`), which ranks the raw names each loader kept under
`releases.media -> 'unmapped'` when the [ADR 0007](https://github.com/groovemap-music/design/blob/main/docs/adr/0007-canonical-media-taxonomy.md)
canonical taxonomy did not recognize them. The console's
`/admin/api/extraction-analysis/{version}/media-mapping-coverage` route calls the existing
`/summary` route only to learn which provider the selected version belongs to, then reads
every number from that upstream route. The ranked list is capped at 10 names; when more
distinct names exist upstream the panel shows a **truncated** notice under the table. The
counts themselves are always exact.

**What changed from the earlier reading.** This panel previously counted discogs-ingestion
`format-not-recognized` data-quality violations for one extraction version. The Discogs
numbers moved when it switched to the route above, and the two readings are not expected to
agree:

- It is a **release** count, not an occurrence count. The taxonomy de-duplicates each
  release's `unmapped` list, so a release naming one unrecognized format twice contributes
  1 where the rules engine could emit two violations.
- It covers **description qualifiers** as well as format names; the old rule only ever fired
  on `formats.format.@name`.
- It is **table-wide, not per-version**. The selected version chooses the provider and
  nothing more, so the reading reflects everything loaded for that provider rather than one
  extraction run.
- The denominator is now **media-tagged releases** rather than the count of release-entity
  violations of any rule, so the rate is a share of real releases rather than a share of
  flagged ones.

Both tabs auto-refresh every 60 seconds and respect the currently selected time range.
