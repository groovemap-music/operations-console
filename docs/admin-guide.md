# Operations-console administrator guide

## Creating an admin account

Admin accounts are created via the `catalog-admin-setup` CLI tool inside the API container:

```bash
docker exec -it groovemap-api catalog-admin-setup --email admin@example.com
```

`catalog-admin-setup` never accepts the password as a CLI argument (command-line
arguments are world-readable via `/proc/*/cmdline` and land in shell
history). The command above prompts interactively (input hidden, not
echoed). For scripted/non-interactive use, set `ADMIN_PASSWORD` (or
`ADMIN_PASSWORD_FILE`, pointing at a Docker secret) in the container's
environment instead.

Passwords must be at least 8 characters. If the email already exists, the password is updated.

## Listing admin accounts

```bash
docker exec -it groovemap-api catalog-admin-setup --list
```

## Signing in to the console

Navigate to `http://<host>:8003` and sign in with your admin credentials. The console is one
page: until `catalog-api` confirms an admin session it shows only the login form, and after
sign-in the live **Overview** tab comes first, followed by the admin tabs. The old
`http://<host>:8003/admin` and `/admin.html` addresses redirect to `/`.

The session cookie is `Secure` by default, so the browser keeps it only over HTTPS or on
`http://localhost` (Chromium and Firefox; Safari refuses it there too). To sign in over plain
HTTP from another host, or from Safari on `localhost`, the deployment must set
`SESSION_COOKIE_SECURE=false`; otherwise sign-in returns straight to the login form. See
[configuration](configuration.md#admin-session-cookie-and-reverse-proxies).

Every data route (`/api/metrics`, `/api/services`, `/api/queues`, `/api/databases`,
`/api/queue-prefixes`, `/api/session`, `POST /api/ws-ticket`), every admin proxy route except
login and logout, and the live WebSocket require a live admin session. Only `/health`, the
login page and its static assets, the `/admin` redirects, and the login and logout proxy routes
(which `catalog-api` validates itself) are public; the console serves no `/docs`, `/redoc`, or
`/openapi.json`. Signing in sets the bearer `catalog-api` issues as an HttpOnly,
`SameSite=Strict` session cookie, so no script in the page can read it. The browser sends the
cookie with every console request, and state-changing requests also carry the
`X-GrooveMap-Console` header, without which the console refuses them as cross-site. The cookie
and what a reverse proxy in front of the console must do are described in
[configuration](configuration.md#admin-session-cookie-and-reverse-proxies). The console holds
no JWT secret:
it asks `catalog-api` (`GET /api/admin/auth/session`) whether the bearer is live and caches a
yes for at most 30 seconds. A 401 or 403 from `catalog-api` (missing, expired, revoked,
password-changed, or non-admin token) is answered `401` with `"reason": "invalid"`; a
`catalog-api` error or an unreachable `catalog-api` is answered `503` with
`"reason": "unavailable"`.

An `invalid` answer for a bearer is reused for 10 seconds, so the same dead bearer is not
re-checked on every request; an `unavailable` answer is never reused. Every check goes through
one shared HTTP client whose pool is capped at 16 connections, so concurrent checks queue for a
connection (within the 5-second timeout) instead of opening unbounded sockets to `catalog-api`.
No client is ever locked out by address.

The console returns to the login form, with the reason shown above it, when you sign out, when
the session expires or is revoked while the console is open, and when `catalog-api` cannot be
reached to confirm it. The open console re-checks its session every 15 seconds and just after
the session's expiry. Signing out always reaches `catalog-api`, even when the console cannot
confirm the session itself. Whatever `catalog-api` answers, it clears the session cookie and
drops the console's cached copy of the session and every outstanding WebSocket ticket for it.
If the console itself cannot be reached, the next load of the page signs out again before it
resumes anything. The login form says
"You signed out." only when `catalog-api` answered 2xx (revoked) or 401 (already dead); any
other answer, or none, is reported as an unconfirmed sign-out, because the session may still be
valid until it expires. The browser loses the session either way. A session that expires, or
that `catalog-api` calls invalid, clears the cookie too, but a `catalog-api` outage leaves the
cookie in place, so a reload resumes the session once it is back. A session revoked elsewhere
stops working for HTTP requests within the 30-second cache window. Older consoles kept the
bearer in the browser's `localStorage`. The first load of this console signs that bearer out at
`catalog-api` and removes it, so you sign in again once.

The live WebSocket never accepts the session cookie. The Overview first calls
`POST /api/ws-ticket` with its session for a single-use ticket valid for 30 seconds and opens
`/ws?ticket=<ticket>`. Opening the socket re-validates the ticket's bearer with `catalog-api`,
bypassing the cache. A missing, reused, expired, or logged-out ticket is closed with code
`4401`, as is an open socket whose session stops being valid: the socket asks `catalog-api`
again, bypassing the cache, on a fixed 30-second schedule and at the session's expiry, so a
revocation closes it within 30 seconds. When `catalog-api` cannot answer for the socket's
session, it is closed with `4503` instead; the browser keeps the session and retries the socket
after 5 seconds, doubling the wait up to 60 seconds and resetting it once a socket delivers data.
Every reconnect waits on that backoff, so a refused socket never turns into a reconnect loop.
The console never expects frames from the browser; any it receives are discarded and cannot
delay a re-check. The
console removes the ticket from the request's query string before its access log or request
spans read it and redacts it from server log lines, but a reverse proxy in front of the console
keeps its own access log: configure it not to log query strings for `/ws`.

## Reading the Overview

The Overview draws one matrix per pipeline from `/api/topology`: rows are entity types, columns
are consumers with the datastore they write to, and each cell is one work queue. A cell shows the
messages queued, an ↑ or ↓ when the depth changed by 1,000 or more over the last 5 minutes, the
lag, and a badge for its dead-letter queue. Lag is the backlog divided by the queue's mean ack
rate over the last 5 minutes, and it colours the cell in four bins (under 1 minute, under 10
minutes, under 1 hour, 1 hour or more); the legend above the matrices names them.

Every cell is judged by the same rules, from the pipeline-visualization spike
([gm-operations-console-2rf](spikes/gm-operations-console-2rf-pipeline-visualization.md)):

| State | Rule | Shown as |
|---|---|---|
| Stalled (critical) | Messages queued, at least one consumer, and a computed ack rate of 0 for at least 60 seconds | ◆ `STALLED`, thick border, hatched cell |
| No consumer (critical) | Messages queued and no consumer | ◆ `NO CONSUMER`, thick border, hatched cell |
| DLQ growing (critical) | The dead-letter queue is deeper than 5 minutes ago | Filled ◆ DLQ badge with the growth; the cell is hatched |
| DLQ not empty (warning) | The dead-letter queue holds messages and is not growing | ▲ DLQ badge with the depth |

A queue filling during an extraction is not an alarm. A rate the console cannot compute (the
first reading, a missed poll, a broker counter reset) is shown as unknown and never counts toward
a stall, so a stall needs 60 seconds of real zero-ack readings. The browser keeps the last 5
minutes of readings for these rules; it starts them again when the live feed reconnects or a
pipeline's queue metrics become unavailable, which a pipeline then shows with a banner and every
cell marked unavailable. Each change into or out of a critical or warning state is written to the
System Event Log of the browser that saw it.

The System Event Log also lists what the console server observed, the same for every operator:
a service going healthy or unhealthy, a datastore going up or down, queue metrics becoming
unavailable or returning, and a dead-letter queue growing or emptying. The server keeps the most
recent 200 of these, and a browser that connects or reconnects receives them at once. Recent
extraction triggers and DLQ purges, with the operator who did them, are merged in from the audit
log. Connecting and disconnecting are shown by the connection indicator only.

Select a cell (or focus it and press Enter) to open the queue's details: its depth, ready and
unacknowledged messages, consumers, publish and ack rates, the 5-minute mean ack rate, lag,
backlog change, how long acks have been at zero, and its dead-letter queue. Escape closes it.
Below 480 px wide, a pipeline with more than two consumers lists each entity's cells in two
columns, and every cell names its consumer.

## Pipeline topology

On a viewport at least 1,024 px wide, **Topology** in a pipeline's matrix head opens that
pipeline's flow: the extractor, one fanout exchange per entity type, each consumer's queue per
entity, the consumers, and the datastores they write to, all drawn from `/api/topology`. Edge
width is the live rate on the square root of messages per second (3,000/s and faster at full
width). A dashed hairline is 0/s and a dotted one a rate the console cannot compute yet. An
exchange's rate is its bound queues' publish rate (a fanout exchange copies every message to
each), and a consumer's edge to its datastore is the sum of its queues' ack rates.

Each queue node is its matrix cell under the same rules: ◆ and a thick border for critical, ▲ for
a warning, the DLQ badge, and the same backlog and lag. A consumer takes the worst state of its
queues and counts them. Select a queue (or focus it and press Enter) to open its detail drawer
over the flow; Escape or **Close** closes the flow. The flow is optional: below 1,024 px there is
no **Topology** control, an open flow closes when the window narrows, and nothing on the
Overview depends on it. The matrix stays the Overview because the flow needs horizontal panning
on a phone and its edges braid with more consumers
([spike evidence](spikes/gm-operations-console-2rf-pipeline-visualization.md#evidence)).

## Run progress

While an extraction runs, a headline above its pipeline's matrix says when it will finish:
"Finishes in 25m, bottleneck Graphinator" names the time until the slowest consumer has loaded
every expected record, and that consumer. "◆ Cannot finish: Tableinator · Releases stalled" lists
every queue the matrix marks stalled, without a consumer, or with a growing DLQ. With no
extraction running there is no headline.

**Run progress**, next to **Matrix** above the pipelines, is the secondary view of the same run:
per pipeline, a burn-up of the loaded fraction per consumer since the run started, dashed to 100 %
at each consumer's ETA, and per entity a bar per consumer of the records loaded (solid) against
those extracted but still queued (light). **Run details** opens the run's row in the Extractions
tab. A finished run shows its duration, final counts, and what each consumer loaded.

The numbers come from VictoriaMetrics through `/api/progress/{pipeline}`, for the run's start and
end from the extraction history:

| Number | Source |
|---|---|
| Expected records per entity | Records extracted in the run (`groovemap_extraction_records_total`) divided by the fraction of the file read (`groovemap_extraction_file_progress_ratio`) |
| Loaded per queue | `rabbitmq_detailed_queue_messages_acked_total` minus its value at the run's start, corrected for counter resets |
| ETA | Records still to load (expected, less loaded and dead-lettered) over the queue's 5-minute mean ack rate |

Nothing is kept in the console: the baseline is read at the run's start time on every request,
so a console restart mid-run changes nothing. When a total or a baseline cannot be derived (no
file-progress sample in the run, an entity not extracted yet, no data from the run's start) the
view says "progress unavailable" and why, and the headline says so too; it never shows a guessed
total. The file-progress gauge keeps the previous run's ratio until the extractor reaches its first
8 MiB checkpoint in the new file, so an entity whose gauge has not moved since the run started is
shown as waiting for that checkpoint rather than as fully read. An unknown or zero ack rate gives no ETA ("Finish time unknown"). Extraction records do not
name their source, and catalog-api's trigger reaches the Discogs extractor, so runs are shown
against the Discogs pipeline.

## Platform health

Below the pipelines, the Overview's **Platform Health** section shows every long-running service
the stack runs besides the ingest pipelines, in three groups. Each row is **Healthy**, **Unhealthy**
(it answered, but not with a healthy answer, and the row says what it answered) or **Unreachable**
(no answer within 3 seconds). All checks run at once, every two seconds, over the `groovemap` network.

| Group | Service | Checked by |
|---|---|---|
| Applications | catalog-api | `GET http://api:8005/health` (the health port; the API itself is 8004) |
| | explore | `GET http://explore:8007/health` |
| | insights | `GET http://insights:8009/health` |
| Data | postgres, neo4j | the console's own connections (the same reading as the Storage tab) |
| | valkey | a `PING` on `valkey:6379`: `+PONG` or `-NOAUTH` (a password-protected server is up; the console is never given the password) is healthy, `-LOADING` or any other error is unhealthy |
| | rabbitmq | `GET {RABBITMQ_PROMETHEUS_URL}/metrics/detailed?family=node_coarse_metrics` |
| Observability | victoria-metrics | `GET /health` on `VICTORIA_METRICS_URL` |
| | victoria-traces | `GET http://victoria-traces:10428/health` |
| | otel-collector | `GET http://otel-collector:13133/` (its `health_check` extension) |
| | grafana | `GET /api/health` on `GRAFANA_API_URL` (default `http://grafana:3000`) |
| | postgres-exporter | `GET http://postgres-exporter:9187/` |
| | valkey-exporter | `GET http://valkey-exporter:9121/metrics`, healthy only while `redis_up` is `1` |
| | cadvisor | `GET http://cadvisor:8080/healthz` |
| | node-exporter | `GET http://node-exporter:9100/` |

A platform service that is down raises an attention item (critical for the applications, the data
services and the broker; a warning for observability, which the stack serves without) and its
changes appear in the System Event Log as `service-down` and `service-recovered`. A datastore is
raised once, by the datastore rule, not twice.

A pipeline none of whose services answered stays on the Overview, marked **Pipeline down**, and
raises one critical item; it is not hidden.

**One-shot jobs** are listed under their own heading. `schema-init` and `rabbitmq-dlq-policy-init`
leave no record the console can read (their only trace is a container exit state, behind the Docker
socket, which the console never has), so they read **Not monitored**. `embeddings` shows its last
completed load (dump and time) from `public.artist_embeddings`, when the console's PostgreSQL role can
read that table; otherwise it reads Not monitored too. The record is written by the load, so it
shows the last load that finished, not a run that failed or is still going.

## The attention bar and the header status

Under the header, on every tab, the attention bar lists everything that needs an operator and is
hidden while nothing does. It shows a count and every critical (◆) and warning (▲) item, critical
first, and **Hide** collapses the list until a new item appears. Each item opens what it is about:
a queue item opens that matrix cell's detail drawer, a service item scrolls to its node, a
datastore item opens the Storage tab, and a Grafana alert opens in Grafana.

| Item | Severity |
|---|---|
| A queue stalled, or with a backlog and no consumer; a dead-letter queue that is growing | Critical |
| A dead-letter queue that holds messages and is not growing | Warning |
| An unhealthy service, or an unavailable datastore | Critical |
| A pipeline none of whose services answered | Critical |
| An application, the broker or Valkey that is unhealthy or unreachable | Critical |
| A service whose status is unknown or starting; a pipeline whose queue metrics are unavailable; an observability service that is unhealthy or unreachable | Warning |
| A firing Grafana alert | Critical when labelled `severity=critical`, otherwise warning |

Queue items use the matrix's own rules above, so the bar and the matrix cannot disagree. Grafana
alerts appear only when a Grafana URL (`GRAFANA_API_URL`, else `GRAFANA_URL`) and a readable `GRAFANA_TOKEN_FILE` are configured
([configuration](configuration.md#grafana-alerts)); if Grafana does not answer, the bar keeps the
items derived from the live feed and says "Grafana alerts unavailable".

The header status (Operational, Warning, Critical, Offline) is computed from the same items. It
reads Unknown only before the console has reported any pipeline or datastore, and Connecting until
the first reading.

## Triggering an extraction

Use **Trigger Discogs Extraction** or **Trigger MusicBrainz Extraction** in the admin panel.
The console sends these requests through fixed proxy routes; `catalog-api` owns authorization
and dispatches the trigger to the selected source-owned producer.

| Source | Console route | Producer | Published exchanges |
| --- | --- | --- | --- |
| Discogs | `POST /admin/api/extractions/trigger` | `discogs-ingestion` | `groovemap-discogs-{artists,labels,masters,releases}` |
| MusicBrainz | `POST /admin/api/extractions/trigger-musicbrainz` | `musicbrainz-ingestion` | `groovemap-musicbrainz-{artists,labels,release-groups,releases}` |

Both console routes map to the promoted `catalog-api` operation
`POST /api/admin/extractions/trigger`, which requires `{"source":"discogs"|"musicbrainz"}`.
Each route sets its own source on the validated JSON object (the browser sends it too). A body
naming a different source gets a 400, and an empty body to the Discogs route (an older browser
tab) is sent as `source=discogs`. Each ingestion service owns its own
download, state, normalization, and publication lifecycle. There is no combined ingestion
runtime or shared producer lock.

Use this when:

- A previous extraction failed and you want to retry
- You suspect data corruption and want a clean reprocess
- A new source dump has been published and you do not want to wait for its producer's periodic check

The extraction runs asynchronously. Progress is tracked in the extraction history table.

If an extraction is already running, the trigger returns an error — wait for it to complete first.

The Extractions history table shows each run's source. Runs recorded without a source show
"not recorded"; the console never guesses one.

**Release order:** this console change ships before `catalog-api` 9066cb7. Once catalog-api
requires `source`, a console that sends none gets 422 on the Discogs trigger. The new console
works against both the old and the new catalog-api.

## DLQ management

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

DLQ names follow the promoted producer-contract pattern
`{exchange-prefix}-{consumer}-{entity}.dlq`. The consumer keys in the queue names
(`graphinator`, `tableinator`, `brainzgraphinator`, and `brainztableinator`) are stable wire
identifiers even though the owning repositories have descriptive names. The optional
`DISCOGS_EXCHANGE_PREFIX` and `MUSICBRAINZ_EXCHANGE_PREFIX` overrides are documented in
[configuration](configuration.md).

## Metrics history and trend analysis

### Queue and health history endpoints

The console reads history from VictoriaMetrics itself; `catalog-api` no longer serves metrics
history. The browser calls the console routes below, which are behind the admin session.

| Data | Console route |
| --- | --- |
| Queue history | `GET /api/history/queues?range=<range>` |
| Service health | `GET /api/history/health?range=<range>` |

Valid `range` values are `1h`, `6h`, `24h` (default), `7d`, `30d` and `90d`. See
[configuration](configuration.md) for `VICTORIA_METRICS_URL` and the series the tabs depend on.

### Queue Trends and System Health tabs

The console (`http://<host>:8003`) exposes two tabs backed by the history endpoints:

- **Queue Trends** — Line charts showing message depth over time for each RabbitMQ queue. Use the range selector to zoom in or out.
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

## Identity Ledger

The **Ledger** tab is the console's first view of the Identity Ledger: concept D of
[design ADR 0016](https://github.com/groovemap-music/design/blob/main/docs/adr/0016-strata-graph-view.md)
section 7. It shows how catalog identity changed over time as a table, with no graph view. It
reads three read-only `catalog-api` routes through the console's admin proxy:

| Console route | `catalog-api` operation | Reads |
| --- | --- | --- |
| `GET /admin/api/identity/ledger/density?kind=` | `admin_identity_ledger_density` | Identity events per UTC month, by type and provenance |
| `GET /admin/api/identity/ledger/works?page=&page_size=&event=&since=&until=` | `admin_identity_ledger_works` | Works whose editions carry identity events, latest event first |
| `GET /admin/api/identity/ledger/works/{work_id}?as_of=&edition_limit=` | `admin_identity_ledger_work` | One work's editions, states, and events at `as_of` |

The proxy rejects a malformed request before it reaches `catalog-api`. That covers an unknown
event or kind, a `page_size` above 100, an `edition_limit` outside 1 to 500, an `as_of`, `since`
or `until` that is not an RFC 3339 instant with an offset, and an unsafe `work_id` path segment.

The tab reads nothing until you open it. After that it re-reads only when you press **Refresh**
or change a filter. It does not auto-refresh, except to ask again while the ledger is being built
(see below).

- **How fresh it is.** The works list and the strip read views `catalog-api` refreshes after
  loads and identity runs, not live. Under the strip, above the works table and under the
  drill-down, the tab says **Ledger as of** the UTC time the last refresh started. Hover over it
  for the full instant. The drill-down reads one work live, so its time names the list it was
  reached from. It reads **Ledger not yet refreshed** when no refresh has run. Writes after that
  time appear at the next refresh.
- **Identity events per month.** A stacked bar per UTC month for the chosen kind (releases,
  masters, artists, or labels), split by event type. Empty months stay on the axis, so it reads
  as time.
- **Works with identity events.** Paged 50 at a time, like the audit log. **Event** keeps only
  that event type. **Since** and **Until** are UTC days, and both are included; the console
  sends `until` as the start of the next day, because `catalog-api` treats it as exclusive.
  Counts and the first and last event cover the matching events only. **Editions** and
  **Disagreements** describe the whole work.
- **Work drill-down.** Choose a work id to open it. **As of (UTC)** reads the ledger at that
  transaction time, and **Now** returns to the present. Each edition shows its state
  (`not_recorded`, `discogs_only`, `musicbrainz_only`, `unresolved`, `split`, or `linked`),
  its primary work and any other work parent, its provider rows, the items superseded into it,
  and its events. Hover over an event type to see the rule that produces it. **Work
  disagreement** marks an edition whose Discogs master and MusicBrainz release group resolve to
  different works; the Discogs side is listed first as the primary work. Choosing the other
  work opens it.

### A ledger that is still being built

On a new deployment nothing has refreshed the ledger yet. Until the first refresh, the works
list and the strip answer `503` with `{"detail": "identity ledger not yet refreshed"}` and a
`Retry-After` header, never an empty list, and the console's proxy passes both on. The tab
shows that as a state, not an error: **The ledger is being built; it refreshes after loads and
identity runs.** The strip and the table stay empty, and the tab reads them again after the
`Retry-After` time (one minute from `catalog-api`, never less than one second and never more
than ten minutes here). The notice goes away by itself once the ledger is built. You can still
open a work meanwhile, because it reads live; its time reads **Ledger not yet refreshed**. Any
other `503`, or a `503` with a different message, is still shown as an error and is not retried.

### The two provenances

Every event carries `dated_from`, and the tab shows both values differently:

- **Recorded by GrooveMap** (`groovemap`). GrooveMap's own tables, plus the link changes
  `musicbrainz-sql-loader` records when it loads a dump. These times are when the change was
  recorded.
- **Dated from the MusicBrainz full export** (`musicbrainz_fullexport`). The one-time
  `catalog-identity-ledger-backfill` dated each MusicBrainz release's Discogs link from the full
  export. About 9% of those dates belong to an older link that MusicBrainz later replaced, so
  read them as estimates, not as recorded times. In the drill-down every such event carries
  the label "dated from the MusicBrainz full export". In the strip their segments are hatched
  and their months are highlighted on the axis, and the note under the legend lists those
  months.

A MusicBrainz row keeps no history of its Discogs link. A past `unresolved` or `split` state is
therefore computed from today's link.
