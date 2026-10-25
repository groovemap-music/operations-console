// Dashboard JavaScript

// The console closes the live WebSocket with 4401 when its ticket was missing, reused, or
// expired, or when the session it was opened with stopped being valid; and with 4503 when
// catalog-api could not answer for the session, which is retried rather than signed out.
const WS_CLOSE_UNAUTHENTICATED = 4401;
const WS_CLOSE_UNAVAILABLE = 4503;

// Statuses with which the session gate refuses a request: no or a dead session (401), or
// catalog-api unable to answer (503).
const SESSION_REFUSALS = new Set([401, 503]);

// Reconnect backoff: 5 s, doubling to a 60 s cap, back to 5 s once a socket delivers data.
// Every reconnect waits, so a refusal the session check cannot explain (catalog-api down while
// the console's cache still vouches for the session) never turns into a tight loop.
const RECONNECT_BASE_MS = 5000;
const RECONNECT_MAX_MS = 60000;

// Operator actions shown in the System Event Log, read from the audit log every minute.
const AUDIT_ACTIONS = ['extraction.trigger', 'dlq.purge'];
const AUDIT_PAGE_SIZE = 20;
const AUDIT_POLL_MS = 60000;

// ─── Queue health: the Overview matrix's rules ────────────────────────────────
//
// From spike gm-operations-console-2rf (docs/spikes/gm-operations-console-2rf-pipeline-visualization.md).
// Every rule reads a short in-browser history of one queue: samples of
// `{ t, messages, messages_ready, messages_unacknowledged, consumers, message_rate, ack_rate }`,
// oldest first, `t` in milliseconds of server time. A null rate is "not computable this interval"
// (first poll, missed poll, counter reset): it is never a zero and never part of a stall.
const QueueHealth = (() => {
    const STALL_SECONDS = 60;       // backlog, consumers, and computed zero acks for this long: stalled
    const TREND_SECONDS = 300;      // the window for DLQ growth, the backlog trend, and the mean ack rate
    const GAP_SECONDS = 30;         // a longer silence between samples starts the history afresh
    const TREND_MIN_CHANGE = 1000;  // backlog changes smaller than a few prefetch windows read as steady
    // Lag = seconds the current backlog needs at the 5-minute mean ack rate, in four bins.
    const LAG_BINS = [[60, '< 1m'], [600, '< 10m'], [3600, '< 1h'], [Infinity, '≥ 1h']];

    // Append a sample and keep exactly the trend window, plus the newest sample at or before its
    // start as the baseline. An older sample than the newest kept is dropped (it arrived late); one
    // with the same time replaces it. Returns false when the sample was dropped.
    function record(samples, sample) {
        const last = samples[samples.length - 1];
        if (last && sample.t < last.t) return false;
        if (last && sample.t === last.t) samples.pop();
        else if (last && sample.t - last.t > GAP_SECONDS * 1000) samples.length = 0;
        samples.push(sample);
        const start = sample.t - TREND_SECONDS * 1000;
        while (samples.length > 1 && samples[1].t <= start) samples.shift();
        return true;
    }

    const latest = samples => samples[samples.length - 1];
    const seconds = (from, to) => (to.t - from.t) / 1000;

    // Seconds of the current run of computed zero acks with a backlog, measured from its first
    // sample: conservative by up to one poll, so a stall is never reported early.
    function zeroAckSeconds(samples) {
        const last = latest(samples);
        let start = null;
        for (let i = samples.length - 1; i >= 0 && samples[i].ack_rate === 0 && samples[i].messages > 0; i--) start = samples[i];
        return start && last ? seconds(start, last) : 0;
    }

    // Mean of the computed ack rates over the trend window; null when none could be computed.
    function meanAckRate(samples) {
        const last = latest(samples);
        if (!last) return null;
        const rates = samples.filter(s => s.ack_rate !== null && s.ack_rate !== undefined && last.t - s.t <= TREND_SECONDS * 1000).map(s => s.ack_rate);
        return rates.length ? rates.reduce((a, b) => a + b, 0) / rates.length : null;
    }

    // 0 with no backlog, Infinity when nothing was acknowledged over the window, null when unknown.
    function lagSeconds(samples) {
        const last = latest(samples);
        if (!last) return null;
        if (last.messages === 0) return 0;
        const ack = meanAckRate(samples);
        if (ack === null) return null;
        return ack > 0 ? last.messages / ack : Infinity;
    }

    function lagBin(lag) {
        if (lag === null || lag === undefined || Number.isNaN(lag)) return null;
        return LAG_BINS.findIndex(([limit]) => lag < limit || limit === Infinity);
    }

    // Depth now minus depth at the start of the window (or the oldest sample, if the history is shorter).
    function depthChange(samples) {
        return samples.length ? latest(samples).messages - samples[0].messages : 0;
    }

    // Seconds the history covers, up to the trend window.
    function coverage(samples) {
        return samples.length ? Math.min(TREND_SECONDS, seconds(samples[0], latest(samples))) : 0;
    }

    function trend(samples) {
        const change = depthChange(samples);
        if (Math.abs(change) < TREND_MIN_CHANGE) return { direction: 'steady', change };
        return { direction: change > 0 ? 'rising' : 'falling', change };
    }

    // A work queue: critical when stalled or without a consumer, otherwise ok and drawn quietly.
    // A filling queue during an extraction is not an alarm.
    function assessQueue(samples) {
        const last = latest(samples);
        if (!last) return { state: 'unknown', kind: 'missing', label: 'not reported' };
        if (last.messages > 0 && last.consumers === 0) return { state: 'crit', kind: 'no-consumer', label: 'NO CONSUMER' };
        const zero = zeroAckSeconds(samples);
        if (last.messages > 0 && last.consumers > 0 && zero >= STALL_SECONDS) {
            const span = zero >= TREND_SECONDS ? `${formatDuration(TREND_SECONDS)}+` : formatDuration(zero);
            return { state: 'crit', kind: 'stalled', label: `STALLED · no acks ${span}` };
        }
        if (last.messages === 0) return { state: 'ok', kind: 'drained', label: 'drained' };
        return { state: 'ok', kind: 'flowing', label: 'flowing' };
    }

    // A dead-letter queue: critical while it grows, a warning while it holds messages.
    function assessDlq(samples) {
        const last = latest(samples);
        if (!last) return { state: 'unknown', kind: 'dlq-missing', label: 'DLQ not reported' };
        const grown = depthChange(samples);
        if (grown > 0) return { state: 'crit', kind: 'dlq-growing', label: `DLQ +${formatCount(grown)} / 5m`, grown };
        if (last.messages > 0) return { state: 'warn', kind: 'dlq-static', label: `DLQ ${formatCount(last.messages)}` };
        return { state: 'ok', kind: 'dlq-empty', label: 'DLQ empty' };
    }

    const RANK = { unknown: 0, ok: 1, warn: 2, crit: 3 };
    const worst = (...states) => states.reduce((a, b) => (RANK[b] > RANK[a] ? b : a), 'unknown');

    function formatCount(n) {
        if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 1 : 2)}M`;
        if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
        if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
        return String(Math.round(n));
    }

    function formatDuration(s) {
        if (!Number.isFinite(s)) return '∞';
        if (s < 60) return `${Math.max(1, Math.round(s))}s`;
        if (s < 3600) return `${Math.round(s / 60)}m`;
        const h = Math.floor(s / 3600);
        const m = Math.round((s - h * 3600) / 60);
        return m ? `${h}h${String(m).padStart(2, '0')}m` : `${h}h`;
    }

    return {
        STALL_SECONDS, TREND_SECONDS, GAP_SECONDS, TREND_MIN_CHANGE, LAG_BINS,
        record, zeroAckSeconds, meanAckRate, lagSeconds, lagBin, depthChange, coverage, trend,
        assessQueue, assessDlq, worst, formatCount, formatDuration,
    };
})();

// ─── Attention: what needs an operator right now ──────────────────────────────
//
// Pure derivation of the attention bar's items from one reading. Queue conditions are not
// re-derived: they are the matrix cells' own assessments (QueueHealth), so the bar and the matrix
// can never disagree. Each item is `{ id, severity: 'crit' | 'warn', kind, text, target }`, where
// `target` says where it leads: a matrix cell (its drawer), a service node, a tab, or a Grafana alert.
const Attention = (() => {
    const ALERT_POLL_MS = 30000;   // how often the browser asks the console for firing Grafana alerts
    // Service statuses that are not a problem: `extracting` and `active` are a working extractor.
    const SERVICE_OK = new Set(['healthy', 'extracting', 'active']);
    const SERVICE_UNHEALTHY = 'unhealthy';
    const DATASTORE_OK = 'healthy';
    const PLATFORM_OK = 'healthy';
    const TAB_OVERVIEW = 'overview';
    const TAB_STORAGE = 'storage';
    const RANK = { crit: 0, warn: 1 };

    // Queue assessments that raise an item; the cell's own label is the item's text.
    const QUEUE_KINDS = new Set(['no-consumer', 'stalled']);
    const DLQ_KINDS = new Set(['dlq-growing', 'dlq-static']);

    // Items for one pipeline: `cells` are `{ consumer, entity, queue, health, dlq }` from the matrix.
    function pipelineItems({ id, label, down, queuesUnavailable, services, cells }) {
        const items = [];
        // No service answered: the pipeline is down, said once, not as one warning per service.
        if (down) {
            return [{
                id: `pipeline-down:${id}`, severity: 'crit', kind: 'pipeline-down',
                text: `${label}: pipeline is down, no service answered`, target: { type: 'tab', tab: TAB_OVERVIEW },
            }];
        }
        for (const service of services || []) {
            const status = String(service.status || 'unknown').toLowerCase();
            if (SERVICE_OK.has(status)) continue;
            items.push({
                id: `service:${id}:${service.name}`,
                severity: status === SERVICE_UNHEALTHY ? 'crit' : 'warn',
                kind: status === SERVICE_UNHEALTHY ? 'service-unhealthy' : 'service-unknown',
                text: `${label}: ${service.name} is ${status === SERVICE_UNHEALTHY ? 'unhealthy' : status}`,
                target: { type: 'service', service: service.name },
            });
        }
        if (queuesUnavailable) {
            items.push({
                id: `queues-unavailable:${id}`, severity: 'warn', kind: 'queues-unavailable',
                text: `${label}: queue metrics unavailable`, target: { type: 'tab', tab: TAB_OVERVIEW },
            });
            return items;
        }
        for (const cell of cells || []) {
            const where = `${cell.entityLabel} → ${cell.consumerLabel}`;
            const target = { type: 'cell', pipeline: id, consumer: cell.consumer, entity: cell.entity };
            if (QUEUE_KINDS.has(cell.health.kind)) {
                items.push({ id: `queue:${cell.queue}`, severity: 'crit', kind: cell.health.kind, text: `${label} ${where}: ${cell.health.label}`, target });
            }
            if (DLQ_KINDS.has(cell.dlq.kind)) {
                items.push({ id: `dlq:${cell.queue}`, severity: cell.dlq.state, kind: cell.dlq.kind, text: `${label} ${where}: ${cell.dlq.label}`, target });
            }
        }
        return items;
    }

    // Platform services outside the pipelines. A datastore row is skipped: `datastoreItems` raises it.
    // Observability being down is a warning (the stack still serves); an application or the broker is critical.
    function platformItems(platform) {
        return (platform || []).filter(service => service.source !== 'datastore' && service.status !== PLATFORM_OK).map(service => ({
            id: `platform:${service.name}`,
            severity: service.group === 'observability' ? 'warn' : 'crit',
            kind: service.status === 'unhealthy' ? 'platform-unhealthy' : 'platform-unreachable',
            text: `${service.name} is ${service.status}`,
            target: { type: 'platform', service: service.name },
        }));
    }

    function datastoreItems(databases) {
        return (databases || []).filter(db => db.status !== DATASTORE_OK).map(db => ({
            id: `datastore:${String(db.name).toLowerCase()}`, severity: 'crit', kind: 'datastore-unavailable',
            text: `${db.name} is unavailable`, target: { type: 'tab', tab: TAB_STORAGE },
        }));
    }

    function alertItems(alerts) {
        return (alerts || []).map((alert, index) => ({
            id: `grafana:${alert.name}:${index}`, severity: alert.severity === 'crit' ? 'crit' : 'warn', kind: 'grafana-alert',
            text: alert.summary ? `${alert.name}: ${alert.summary}` : alert.name, target: { type: 'grafana', url: alert.url },
        }));
    }

    // Critical items first, then warnings, each group in the order the sources listed them.
    function derive({ pipelines, databases, platform, alerts }) {
        const items = [...(pipelines || []).flatMap(pipelineItems), ...platformItems(platform), ...datastoreItems(databases), ...alertItems(alerts)];
        return items.map((item, order) => ({ item, order }))
            .sort((a, b) => RANK[a.item.severity] - RANK[b.item.severity] || a.order - b.order)
            .map(({ item }) => item);
    }

    // The header's operational status. Derived from the items, not from the services alone: a
    // reading with no services listed, or services in a state the header never named, is not
    // "unknown". Unknown means nothing has been reported at all.
    function overall({ reported, items, services }) {
        if (!reported) return { key: 'unknown', label: 'Unknown' };
        if (services.length > 0 && services.every(s => String(s.status).toLowerCase() === SERVICE_UNHEALTHY)) return { key: 'offline', label: 'Offline' };
        if (items.some(item => item.severity === 'crit')) return { key: 'critical', label: 'Critical' };
        if (items.length) return { key: 'warning', label: 'Warning' };
        return { key: 'operational', label: 'Operational' };
    }

    return { ALERT_POLL_MS, SERVICE_OK, DATASTORE_OK, derive, overall };
})();

// ─── Deep links: from the console into Grafana and traces ─────────────────────
//
// Pure link building. The console shows live state and a short window of history; the provisioned
// Grafana dashboards hold everything longer, so each service, queue, and datastore links there
// instead of redrawing their charts. A link carries the dashboard's own variables and the console's
// shared time range. With a base URL unset there is no link at all, never a broken one.
const DeepLinks = (() => {
    const DEFAULT_RANGE = '24h';
    const RANGE_PATTERN = /^\d+[mhd]$/;
    // The provisioned dashboards' uids (deployment config/grafana/dashboards).
    const DASHBOARD = {
        pipeline: 'groovemap-pipeline-overview',   // var: source (discogs | musicbrainz)
        consumers: 'groovemap-consumers',          // var: service
        infrastructure: 'groovemap-infrastructure', // var: queue (RabbitMQ), plus PostgreSQL and Valkey panels
        ingestion: 'groovemap-ingestion',          // vars: service, source
        neo4j: 'groovemap-neo4j',                  // var: service (the writer), All by default
        traces: 'groovemap-traces',                // vars: service, span
    };

    // The console's range ("24h", "7d") as Grafana's relative from/to.
    function timeRange(range) {
        return { from: `now-${RANGE_PATTERN.test(range) ? range : DEFAULT_RANGE}`, to: 'now' };
    }

    // `{base}/d/{uid}?var-x=..&from=..&to=..`, every value URL-encoded; null when the base is unset.
    function dashboardUrl(base, key, vars, range) {
        if (!base || !DASHBOARD[key]) return null;
        const { from, to } = timeRange(range);
        const query = [...Object.entries(vars || {}).map(([name, value]) => [`var-${name}`, value]), ['from', from], ['to', to]]
            .map(([name, value]) => `${encodeURIComponent(name)}=${encodeURIComponent(value)}`).join('&');
        return `${base}/d/${encodeURIComponent(DASHBOARD[key])}?${query}`;
    }

    // Grafana panels vs. the trace view, which may be hosted under its own base URL.
    function resolve(bases, spec, range) {
        const base = spec.dashboard === 'traces' ? bases?.traces : bases?.grafana;
        return dashboardUrl(base, spec.dashboard, spec.vars, range);
    }

    // What each kind of thing links to: `[{ label, dashboard, vars }]`.
    const specs = {
        queue: (pipeline, queue, deadLetterQueue) => [
            { label: 'Queue', dashboard: 'infrastructure', vars: { queue } },
            ...(deadLetterQueue ? [{ label: 'Dead-letter queue', dashboard: 'infrastructure', vars: { queue: deadLetterQueue } }] : []),
            { label: 'Pipeline', dashboard: 'pipeline', vars: { source: pipeline } },
        ],
        producer: (pipeline, service) => [
            { label: 'Grafana', dashboard: 'ingestion', vars: { service, source: pipeline } },
            { label: 'Traces', dashboard: 'traces', vars: { service } },
        ],
        consumer: (service) => [
            { label: 'Grafana', dashboard: 'consumers', vars: { service } },
            { label: 'Traces', dashboard: 'traces', vars: { service } },
        ],
        datastore: (id) => (id === 'neo4j'
            ? [{ label: 'Grafana', dashboard: 'neo4j', vars: {} }]
            // PostgreSQL has no dashboard of its own; its panels sit on the infrastructure one.
            : id === 'postgresql' ? [{ label: 'Grafana', dashboard: 'infrastructure', vars: {} }] : []),
    };

    return { DASHBOARD, timeRange, dashboardUrl, resolve, specs };
})();

// ─── Run progress: when the running extraction finishes, and what limits it ────
//
// Pure derivation for the headline above each pipeline's matrix and for the Run progress view
// (spike gm-operations-console-2rf). Expected records per entity and each queue's loaded count
// since the run started come from /api/progress, which reads them from VictoriaMetrics. The ack
// rate and the queue's state are the matrix cell's own (QueueHealth), so what makes a run unable
// to finish is exactly what the matrix marks critical. A missing total, baseline, or rate is never
// filled in: the run's progress is unavailable, or its finish time unknown, instead.
const RunProgress = (() => {
    const POLL_MS = 30000;   // how often the browser re-reads the runs and their progress
    // Queue assessments that stop a run finishing, and how the headline names them.
    const BLOCKERS = { stalled: 'stalled', 'no-consumer': 'has no consumer', 'dlq-growing': 'DLQ growing' };

    // catalog-api records a run without its source: its trigger reaches only the Discogs extractor.
    const pipelineOf = run => run.source || 'discogs';

    // The newest run that has started, per pipeline (catalog-api lists them newest first).
    function latestRuns(extractions) {
        const runs = {};
        for (const run of extractions || []) {
            if (!run.started_at || Number.isNaN(Date.parse(run.started_at))) continue;
            const id = pipelineOf(run);
            if (!(id in runs)) runs[id] = run;
        }
        return runs;
    }

    const isRunning = run => Boolean(run) && run.status === 'running' && !run.completed_at;

    // A finished run's duration in seconds: catalog-api's own, or completed minus started.
    function duration(run) {
        if (run.duration_seconds !== null && run.duration_seconds !== undefined) return run.duration_seconds;
        const end = Date.parse(run.completed_at);
        return Number.isNaN(end) ? null : (end - Date.parse(run.started_at)) / 1000;
    }

    // Why one cell's progress cannot be derived, or null when it can. `totals` is the entity's
    // /api/progress entry and `acked` the queue's.
    function gap({ entityLabel, consumerLabel, totals, acked }) {
        if (!totals || totals.extracted === null) return 'no history from the run\'s start';
        if (totals.extracted === 0) return `${entityLabel} not extracted yet`;
        // The gauge still holds the previous run's ratio until the new file's first checkpoint.
        if (totals.waiting_for_checkpoint) return `${entityLabel} waiting for the extractor's first progress checkpoint`;
        if (totals.expected === null) return `no file progress for ${entityLabel}`;
        if (!acked || acked.loaded === null) return `no acked baseline for ${consumerLabel} · ${entityLabel}`;
        return null;
    }

    // Seconds until a consumer has loaded every expected record of one entity at its 5-minute mean
    // ack rate: 0 once nothing remains, and null, never Infinity, when the remaining work is
    // unknown or nothing is being acknowledged (a null or zero rate).
    function eta({ expected, loaded, dlqDepth = 0, meanAck }) {
        if (expected === null || expected === undefined || loaded === null || loaded === undefined) return null;
        const remaining = Math.max(0, expected - loaded - (dlqDepth || 0));
        if (remaining < 1) return 0;
        return meanAck > 0 ? remaining / meanAck : null;
    }

    // The slowest of a consumer's cells; null when any of them has no ETA.
    function consumerEta(cells) {
        const etas = cells.map(eta);
        return etas.length && etas.every(e => e !== null) ? Math.max(...etas) : null;
    }

    // One pipeline's headline: `{ kind, state, text, detail? }`, or null when no run is in progress.
    // `cells` are `{ consumerLabel, entityLabel, health, dlq, expected, loaded, dlqDepth, meanAck, gap }`.
    function headline({ running, error, cells }) {
        if (!running) return null;
        const reasons = cells.flatMap(cell => [cell.health, cell.dlq]
            .filter(health => health && health.kind in BLOCKERS)
            .map(health => `${cell.consumerLabel} · ${cell.entityLabel} ${BLOCKERS[health.kind]}`));
        if (reasons.length) return { kind: 'cannot-finish', state: 'crit', text: `Cannot finish: ${reasons.join(', ')}` };
        const missing = error || (cells.length ? cells.map(cell => cell.gap).find(Boolean) : 'no queues');
        if (missing) return { kind: 'unavailable', state: 'unknown', text: `Progress unavailable: ${missing}` };
        const timed = cells.map(cell => ({ cell, eta: eta(cell) }));
        const unknown = timed.find(t => t.eta === null);
        if (unknown) {
            return { kind: 'unknown', state: 'unknown', text: `Finish time unknown: no ack rate for ${unknown.cell.consumerLabel} · ${unknown.cell.entityLabel}` };
        }
        const slowest = timed.reduce((a, b) => (b.eta > a.eta ? b : a));
        if (slowest.eta === 0) return { kind: 'loaded', state: 'ok', text: 'Loaded' };
        return { kind: 'finishes', state: 'ok', text: `Finishes in ${QueueHealth.formatDuration(slowest.eta)}`, detail: `bottleneck ${slowest.cell.consumerLabel}` };
    }

    // Loaded fraction over the run for one consumer: `[[ms, fraction], ...]` at the times every one
    // of its queues reported, over the total expected records of the pipeline.
    function burnupSeries(cells, totalExpected) {
        const sums = new Map();
        for (const cell of cells) {
            for (const [iso, loaded] of cell.points) {
                const at = sums.get(iso) || { n: 0, loaded: 0 };
                sums.set(iso, { n: at.n + 1, loaded: at.loaded + loaded });
            }
        }
        return [...sums].filter(([, at]) => at.n === cells.length)
            .map(([iso, at]) => [Date.parse(iso), at.loaded / totalExpected]).sort((a, b) => a[0] - b[0]);
    }

    // Burn-up geometry: each consumer's line from the run start to now, and a dashed projection
    // from now to 100 % at its ETA (none without one). Times in ms; ETAs in seconds.
    function burnup({ start, now, series, width, height, left = 34, bottom = 14 }) {
        const etas = series.map(s => s.eta).filter(e => e !== null && e !== undefined);
        const horizon = Math.max(now + (etas.length ? Math.max(...etas) * 1000 * 1.05 : 0), start + 1);
        const x = t => left + ((t - start) / (horizon - start)) * (width - left - 4);
        const y = f => (height - bottom) - Math.min(1, Math.max(0, f)) * (height - bottom - 4);
        const fmt = (t, f) => `${x(t).toFixed(1)},${y(f).toFixed(1)}`;
        return {
            nowX: x(now), top: y(1), base: y(0),
            lines: series.map(s => {
                const last = s.points[s.points.length - 1];
                return {
                    consumer: s.consumer,
                    points: s.points.map(([t, f]) => fmt(t, f)).join(' '),
                    projection: last && s.eta !== null && s.eta !== undefined ? `${fmt(now, last[1])} ${fmt(now + s.eta * 1000, 1)}` : null,
                };
            }),
        };
    }

    return { POLL_MS, BLOCKERS, pipelineOf, latestRuns, isRunning, duration, gap, eta, consumerEta, headline, burnupSeries, burnup };
})();

// ─── Topology: one pipeline's flow, an optional desktop drill-down ────────────
//
// Pure geometry for the producer → exchanges → per-entity queues → consumers → datastores
// diagram (spike gm-operations-console-2rf, flow.html). The spike ruled it out as the Overview:
// it needs horizontal panning on a phone and braids its edges with twice the consumers. So it is
// only opened from a pipeline's matrix head on a viewport at least MIN_WIDTH wide, and nothing on
// the Overview depends on it. Node states are the matrix cells' own (QueueHealth); this module
// only places nodes and sizes edges.
const TopologyFlow = (() => {
    const MIN_WIDTH = 1024;      // narrower viewports get no Topology control (index.html holds the same breakpoint)
    const RATE_MAX = 3000;       // messages/s drawn at full edge width; faster edges are capped
    const WIDTH = 950;
    const TOP = 26;              // room for the column headings
    const QUEUE_HEIGHT = 58, QUEUE_GAP = 6, GROUP_GAP = 14;
    const COLUMNS = {
        producer: { x: 0, w: 130, title: 'PRODUCER' },
        exchange: { x: 160, w: 150, title: 'EXCHANGE (FANOUT)' },
        queue: { x: 350, w: 250, title: 'QUEUE' },
        consumer: { x: 660, w: 150, title: 'CONSUMER' },
        datastore: { x: 840, w: 110, title: 'DATASTORE' },
    };

    // Edge width grows with the square root of the rate, so a slow edge stays visible next to a
    // fast one. Zero and unknown rates are hairlines, told apart by their dash pattern.
    function edgeWidth(rate) {
        if (rate === null || rate === undefined || !(rate > 0)) return 1;
        return 1.5 + 10 * Math.sqrt(Math.min(rate, RATE_MAX) / RATE_MAX);
    }

    function edgeStyle(rate) {
        if (rate === null || rate === undefined || Number.isNaN(rate)) return 'unknown';
        return rate > 0 ? 'flowing' : 'idle';
    }

    // A fanout exchange copies every message to each bound queue, so its publish rate is any
    // bound queue's; the highest known one, or null when none is known.
    function exchangeRate(rates) {
        const known = rates.filter(r => r !== null && r !== undefined);
        return known.length ? Math.max(...known) : null;
    }

    // What a consumer writes to its datastore: the sum of its queues' known ack rates.
    function sumRates(rates) {
        const known = rates.filter(r => r !== null && r !== undefined);
        return known.length ? known.reduce((a, b) => a + b, 0) : null;
    }

    function curve(x1, y1, x2, y2) {
        const mx = (x1 + x2) / 2;
        return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    }

    // Nodes and edges for one /api/topology pipeline. Queues are grouped by entity, next to their
    // exchange; consumers and datastores are spread evenly down the full height.
    function layout(pipeline) {
        const { entity_types: entities, consumers } = pipeline;
        const nodes = [];
        const edges = [];
        const node = (n) => { nodes.push(n); return n; };
        const box = (kind, y, h) => ({ x: COLUMNS[kind].x, y, w: COLUMNS[kind].w, h });

        let y = TOP;
        const queueNodes = [];
        const exchanges = entities.map(entity => {
            const top = y;
            consumers.forEach(consumer => {
                queueNodes.push(node({ key: `queue:${consumer.name}:${entity}`, kind: 'queue', consumer: consumer.name, entity, ...box('queue', y, QUEUE_HEIGHT) }));
                y += QUEUE_HEIGHT + QUEUE_GAP;
            });
            const mid = (top + y - QUEUE_GAP) / 2;
            y += GROUP_GAP;
            return node({ key: `exchange:${entity}`, kind: 'exchange', entity, ...box('exchange', mid - 22, 44) });
        });
        const height = Math.max(y - GROUP_GAP, TOP + 80);
        const spread = (count, i) => TOP + ((height - TOP) / count) * (i + 0.5);

        const producer = node({ key: 'producer', kind: 'producer', ...box('producer', spread(1, 0) - 40, 80) });
        const consumerNodes = consumers.map((consumer, i) =>
            node({ key: `consumer:${consumer.name}`, kind: 'consumer', consumer: consumer.name, ...box('consumer', spread(consumers.length, i) - 26, 52) }));
        const stores = [...new Set(consumers.map(c => c.datastore))];
        const storeNodes = stores.map((datastore, i) =>
            node({ key: `datastore:${datastore}`, kind: 'datastore', datastore, ...box('datastore', spread(stores.length, i) - 22, 44) }));

        const right = n => n.x + n.w;
        const middle = n => n.y + n.h / 2;
        const edge = (from, to) => edges.push({ key: `${from.key}>${to.key}`, from: from.key, to: to.key, d: curve(right(from), middle(from), to.x, middle(to)) });
        exchanges.forEach(exchange => edge(producer, exchange));
        queueNodes.forEach(queue => {
            edge(exchanges[entities.indexOf(queue.entity)], queue);
            edge(queue, consumerNodes[consumers.findIndex(c => c.name === queue.consumer)]);
        });
        consumers.forEach((consumer, i) => edge(consumerNodes[i], storeNodes[stores.indexOf(consumer.datastore)]));

        const columns = Object.entries(COLUMNS).map(([kind, { x, title }]) => ({ kind, x, title }));
        return { width: WIDTH, height, columns, nodes, edges };
    }

    return { MIN_WIDTH, RATE_MAX, edgeWidth, edgeStyle, exchangeRate, sumRates, layout };
})();

// Below this viewport width a pipeline with more than two consumers switches to the stacked,
// self-labelled cell layout (the matrix CSS in index.html holds the same breakpoint).
const MATRIX_STACKED_MAX_WIDTH = 479;
const MATRIX_STACKED_MIN_CONSUMERS = 3;

// A 390 px grid of more than two consumer columns breaks names mid-word, so on a narrow viewport
// each entity becomes a two-column block of cells that carry their consumer's name.
function matrixLayout(consumerCount, narrow) {
    return narrow && consumerCount >= MATRIX_STACKED_MIN_CONSUMERS ? 'stacked' : 'grid';
}

// The Overview's live view. It reads nothing until admin.js confirms a session and announces
// it with `console:authenticated`; `console:signed-out` stops it again. Every data request and
// every WebSocket ticket carries the session's bearer.
class Dashboard {
    constructor() {
        this.ws = null;
        this.token = null;
        this.active = false;
        this.connecting = false;
        this.reconnectTimer = null;
        this.reconnectDelay = RECONNECT_BASE_MS;
        // The System Event Log, newest first: events the server detected and streamed (`server`),
        // recent operator actions from the audit log (`audit`), and queue health changes the matrix
        // derives in this browser (`local`). Connect and disconnect are the connection indicator's
        // business, never log entries.
        this.activityLog = [];
        this.maxLogEntries = 100;
        this.lastServerEventId = 0;
        this.auditTimer = null;
        // The first reading over HTTP, which can land after the socket's first frame.
        this.initialLoad = Promise.resolve();

        // The pipeline layout from /api/topology; the Overview draws nothing until it arrives.
        this.topology = null;
        this.topologyRequest = null;
        this.pendingData = null;
        // Pipelines whose last broker scrape failed; their queue data is unavailable, not empty.
        this.queuesUnavailable = {};
        // Short per-queue history (QueueHealth samples) by queue name, the health kind last logged
        // per queue, and the queue the detail drawer shows.
        this.history = new Map();
        this.healthKinds = {};
        this.drawerQueue = null;
        this.drawerReturnFocus = null;
        // The pipeline the topology drill-down shows (null while closed), and what it was opened from.
        this.topologyPipeline = null;
        this.topologyReturnFocus = null;

        // The attention bar: the latest reading, the Grafana alerts the console last served (null
        // until asked), and which items the operator has already seen, so a new one re-opens a
        // list they collapsed.
        this.lastReading = null;
        this.alerts = null;
        this.alertTimer = null;
        this.attentionSeen = new Set();
        this.attentionCollapsed = false;

        // Deep links: the Grafana and traces base URLs the console serves (null until asked, and
        // null for a base that is unset), and the specs behind every link container on the page.
        this.linkBases = { grafana: null, traces: null };
        this.linkSets = new Map();

        // Run progress: the newest started run per pipeline (from the extraction history), what
        // /api/progress last answered for it (`{ key, data, error }` by pipeline), and which of the
        // Overview's two views is shown.
        this.runs = {};
        this.runProgress = {};
        this.progressTimer = null;
        this.overviewView = 'matrix';

        document.addEventListener('console:authenticated', (event) => this.start(event.detail?.token));
        document.addEventListener('console:signed-out', () => this.stop());

        this.narrowViewport = window.matchMedia?.(`(max-width: ${MATRIX_STACKED_MAX_WIDTH}px)`);
        this.narrowViewport?.addEventListener?.('change', () => this._applyMatrixLayout());

        // The topology drill-down exists only on a wide viewport; narrowing the window closes it.
        this.wideViewport = window.matchMedia?.(`(min-width: ${TopologyFlow.MIN_WIDTH}px)`);
        this.wideViewport?.addEventListener?.('change', () => {
            if (!this._isWideViewport()) this.closeTopology();
        });
        document.getElementById('topology-dialog')?.addEventListener('close', () => this._onTopologyClosed());
        document.getElementById('topology-close')?.addEventListener('click', () => this.closeTopology());

        const drawer = document.getElementById('queue-drawer');
        drawer?.addEventListener('close', () => this._onDrawerClosed());
        document.getElementById('queue-drawer-close')?.addEventListener('click', () => this.closeQueueDrawer());
        document.querySelectorAll('[data-datastore-links]').forEach(el => this._linkSet(el, DeepLinks.specs.datastore(el.dataset.datastoreLinks)));
        // The shared range lives in admin.js; a link follows it as soon as it changes.
        document.addEventListener('click', (event) => {
            if (event.target.closest?.('.range-btn')) setTimeout(() => this._refreshLinks(), 0);
        });
        document.getElementById('attention-toggle')?.addEventListener('click', () => {
            this.attentionCollapsed = !this.attentionCollapsed;
            this.updateAttention();
        });
        document.querySelectorAll('[data-overview-view]').forEach(button => {
            button.addEventListener('click', () => this.showOverviewView(button.dataset.overviewView));
        });
    }

    // ─── Session lifecycle ────────────────────────────────────────────────────

    start(token) {
        if (!token) return;
        const resumed = this.active && this.token === token;
        this.token = token;
        this.active = true;
        if (resumed) {
            // admin.js re-confirmed the session after a refusal: reconnect on the backoff.
            this.reconnect();
            return;
        }
        this.reconnectDelay = RECONNECT_BASE_MS;
        this.initialLoad = this.fetchInitialData();
        this.initializeWebSocket();
        this.startAlertPolling();
        this.startAuditPolling();
        this.startProgressPolling();
        this.fetchLinkBases();
    }

    stop() {
        this.active = false;
        this.token = null;
        // Nothing of the signed-in view outlives the session; the next one fetches it afresh.
        this.topology = null;
        this.pendingData = null;
        this.closeQueueDrawer();
        this.closeTopology();
        this._resetHistory();
        this.stopAlertPolling();
        this.stopAuditPolling();
        this.stopProgressPolling();
        this.runs = {};
        this.runProgress = {};
        document.getElementById('run-progress')?.replaceChildren();
        this.showOverviewView('matrix');
        this.activityLog = [];
        this.lastServerEventId = 0;
        this.renderActivityLog();
        this.lastReading = null;
        this.alerts = null;
        this.attentionSeen = new Set();
        this.attentionCollapsed = false;
        this.updateAttention();
        document.getElementById('pipelines')?.replaceChildren();
        this.reconnectDelay = RECONNECT_BASE_MS;
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        const socket = this.ws;
        this.ws = null;
        if (socket) {
            try { socket.close(1000); } catch (_) { /* ignore */ }
        }
        this.updateConnectionStatus('disconnected');
    }

    // Hand the decision back to admin.js, which owns the session: it either confirms the
    // session (and announces it again, which reconnects) or returns to the login form.
    requestSessionCheck() {
        document.dispatchEvent(new CustomEvent('console:session-check'));
    }

    _authHeaders() {
        return { Authorization: `Bearer ${this.token}` };
    }

    // ─── WebSocket ────────────────────────────────────────────────────────────

    // Browsers cannot send Authorization on a WebSocket, so each connection first trades the
    // bearer for a single-use ticket (valid 30 s) and presents it as `/ws?ticket=...`.
    async _fetchTicket() {
        const response = await fetch('/api/ws-ticket', { method: 'POST', headers: this._authHeaders(), cache: 'no-store' });
        if (response.status === 401) {
            this.requestSessionCheck();
            return null;
        }
        if (!response.ok) throw new Error(`ticket request failed: ${response.status}`);
        const body = await response.json();
        return body.ticket || null;
    }

    async initializeWebSocket() {
        if (!this.active || this.connecting) return;
        // Skip if already connecting or connected — prevents reconnect churn
        if (this.ws && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
            return;
        }
        this.connecting = true;
        let ticket;
        try {
            ticket = await this._fetchTicket();
        } catch {
            ticket = undefined;
        } finally {
            this.connecting = false;
        }
        if (!this.active) return;
        if (ticket === null) return;  // admin.js is re-checking the session
        if (ticket === undefined) {
            this.updateConnectionStatus('disconnected');
            this.reconnect();
            return;
        }

        // Close any existing WebSocket to prevent connection leaks on reconnect
        if (this.ws) {
            try { this.ws.close(); } catch (_) { /* ignore */ }
        }
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`;
        const socket = new WebSocket(wsUrl);
        this.ws = socket;

        socket.onopen = () => {
            this.updateConnectionStatus('connected');
        };

        socket.onmessage = (event) => {
            // Only a socket that delivers data resets the backoff: a refused socket opens too,
            // and is closed straight after.
            this.reconnectDelay = RECONNECT_BASE_MS;
            try {
                this.handleMessage(JSON.parse(event.data));
            } catch {
                // Ignore malformed WebSocket messages (e.g., partial frames during reconnect)
            }
        };

        socket.onerror = () => {
            if (this.ws === socket) this.updateConnectionStatus('disconnected');
        };

        socket.onclose = (event) => {
            // A socket that was replaced or stopped must not trigger a reconnect.
            if (this.ws !== socket) return;
            this.updateConnectionStatus('disconnected');
            // The feed has a gap now: the stall and trend windows start again with the next one.
            this._resetHistory();
            if (!this.active) return;
            this.ws = null;
            if (event.code === WS_CLOSE_UNAUTHENTICATED) {
                this.requestSessionCheck();
                return;
            }
            this.reconnect();
        };
    }

    handleMessage(message) {
        if (message.type === 'metrics_update') this.updateDashboard(message.data);
        else if (message.type === 'event_backlog') this.applyEventBacklog(message.data);
        else if (message.type === 'event') this.applyServerEvent(message.data);
    }

    // Schedule one reconnect after the current backoff delay, then double the delay.
    reconnect() {
        if (!this.active || this.reconnectTimer) return;
        const delay = this.reconnectDelay;
        this.reconnectDelay = Math.min(delay * 2, RECONNECT_MAX_MS);
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.initializeWebSocket();
        }, delay);
    }

    // ─── Status indicators ────────────────────────────────────────────────────

    updateConnectionStatus(status) {
        const indicator = document.querySelector('.status-indicator');
        const text = document.querySelector('.status-text');

        if (status === 'connected') {
            // NOTE: 'connected' class is kept for Playwright test detection
            if (indicator) indicator.className = 'status-indicator connected w-2 h-2 rounded-full bg-emerald-500 animate-pulse';
            if (text) {
                text.textContent = 'Connected';
                text.className = 'status-text text-[10px] text-emerald-400 font-bold uppercase tracking-widest';
            }
        } else {
            // NOTE: 'disconnected' class is kept for Playwright test detection
            if (indicator) indicator.className = 'status-indicator disconnected w-2 h-2 rounded-full bg-red-500';
            if (text) {
                text.textContent = 'Disconnected';
                text.className = 'status-text text-[10px] text-red-400 font-bold uppercase tracking-widest';
            }
        }
    }

    // The header's operational status, from the attention items and the services reported.
    updateOverallStatus(status) {
        const dot = document.getElementById('operational-status-dot');
        const text = document.getElementById('operational-status-text');
        if (!dot || !text) return;
        const looks = {
            operational: ['bg-emerald-500 animate-pulse', 'text-emerald-500'],
            warning: ['bg-yellow-500', 'text-yellow-500'],
            critical: ['bg-red-500', 'text-red-500'],
            offline: ['bg-red-500', 'text-red-500'],
            unknown: ['bg-yellow-500 animate-pulse', 'text-yellow-500'],
            connecting: ['bg-yellow-500 animate-pulse', 'text-yellow-500'],
        };
        const [dotClasses, textClass] = looks[status.key];
        dot.className = `w-3 h-3 rounded-full ${dotClasses}`;
        text.className = `text-[10px] ${textClass} font-bold uppercase tracking-widest`;
        text.textContent = status.label;
        text.dataset.status = status.key;
    }

    // ─── Attention bar ────────────────────────────────────────────────────────

    // Everything the bar reads, for the latest reading: each reported pipeline's services and
    // matrix cells (assessed by the matrix's own rules), the datastores, and the Grafana alerts.
    attentionInputs() {
        const reading = this.lastReading;
        const reported = reading?.pipelines || {};
        const pipelines = (this.topology?.pipelines || []).filter(p => p.id in reported).map(pipeline => ({
            id: pipeline.id,
            label: pipeline.label,
            down: reported[pipeline.id].down === true,
            queuesUnavailable: this.queuesUnavailable[pipeline.id] === true,
            services: reported[pipeline.id].services || [],
            cells: pipeline.consumers.flatMap(consumer => pipeline.entity_types.flatMap(entity => {
                const model = this._cellModel(pipeline, consumer, entity);
                if (!model.queue) return [];
                return [{
                    consumer: consumer.name, consumerLabel: consumer.label, entity, entityLabel: this._entityLabel(entity),
                    queue: model.queue.name, health: model.health, dlq: model.dlq,
                }];
            })),
        }));
        return { pipelines, databases: reading?.databases || [], platform: reading?.platform || [], alerts: this.alerts?.alerts || [] };
    }

    // Recompute the items, redraw the bar, and set the header from the same reading.
    updateAttention() {
        const inputs = this.attentionInputs();
        const items = Attention.derive(inputs);
        const services = inputs.pipelines.flatMap(p => p.services);
        const reported = inputs.pipelines.length > 0 || inputs.databases.length > 0;
        this.updateOverallStatus(this.lastReading === null ? { key: 'connecting', label: 'Connecting' } : Attention.overall({ reported, items, services }));

        if (items.some(item => !this.attentionSeen.has(item.id))) this.attentionCollapsed = false;
        this.attentionSeen = new Set(items.map(item => item.id));
        this.attentionItems = items;
        this._renderAttention(items);
    }

    _renderAttention(items) {
        const bar = document.getElementById('attention-bar');
        if (!bar) return;
        const note = this.alerts?.configured && !this.alerts.available ? (this.alerts.note || 'Grafana alerts unavailable') : '';
        bar.hidden = items.length === 0 && !note;
        bar.dataset.count = String(items.length);
        const crit = items.filter(item => item.severity === 'crit').length;
        const warn = items.length - crit;

        const summary = document.getElementById('attention-summary');
        if (summary) {
            summary.textContent = items.length
                ? `${items.length} ${items.length === 1 ? 'item needs' : 'items need'} attention · ${crit} critical · ${warn} warning`
                : 'Nothing needs attention';
        }
        const toggle = document.getElementById('attention-toggle');
        if (toggle) {
            toggle.hidden = items.length === 0;
            toggle.setAttribute('aria-expanded', String(!this.attentionCollapsed));
            toggle.textContent = this.attentionCollapsed ? 'Show' : 'Hide';
        }
        const noteEl = document.getElementById('attention-note');
        if (noteEl) {
            noteEl.hidden = !note;
            noteEl.textContent = note;
        }
        const list = document.getElementById('attention-list');
        if (!list) return;
        list.hidden = this.attentionCollapsed || items.length === 0;
        list.replaceChildren(...items.map(item => this._renderAttentionItem(item)));
    }

    _renderAttentionItem(item) {
        const glyph = item.severity === 'crit' ? '◆' : '▲';
        const mark = this._el('b', { cls: item.severity === 'crit' ? 'mx-crit-text' : 'mx-warn-text', text: glyph, attrs: { 'aria-hidden': 'true' } });
        const label = `${item.severity === 'crit' ? 'Critical' : 'Warning'}: ${item.text}`;
        const attrs = { 'aria-label': label, 'data-attention-kind': item.kind };
        let control;
        if (item.target.type === 'grafana') {
            control = this._el('a', { cls: 'attention-link', attrs: { ...attrs, href: item.target.url, target: '_blank', rel: 'noopener noreferrer' } }, mark, ` ${item.text}`);
        } else {
            control = this._el('button', { cls: 'attention-link', attrs: { ...attrs, type: 'button' } }, mark, ` ${item.text}`);
            control.addEventListener('click', () => this.goToAttention(item.target));
        }
        const links = this._el('span', { cls: 'mx-links attention-links', attrs: { 'aria-label': 'Grafana links' } });
        this._linkSet(links, this._attentionLinkSpecs(item.target));
        return this._el('li', { cls: 'attention-item', attrs: { 'data-severity': item.severity, 'data-attention-id': item.id } }, control, links);
    }

    // Show the Overview (or the item's tab) and take the operator to the thing itself.
    goToAttention(target) {
        const tab = target.type === 'tab' ? target.tab : 'overview';
        window.adminDashboard?.switchTab(tab);
        if (target.type === 'cell') {
            this.showOverviewView('matrix');
            const cell = document.getElementById(`${target.pipeline}-${target.consumer}-${target.entity}-cell`);
            if (cell) this.openQueueDrawer(cell);
        } else if (target.type === 'service') {
            document.getElementById(`service-${target.service}`)?.scrollIntoView?.({ block: 'center' });
        } else if (target.type === 'platform') {
            document.getElementById(`platform-${target.service}`)?.scrollIntoView?.({ block: 'center' });
        }
    }

    // ─── Grafana alerts, served by the console so the token stays on the server ─────

    startAlertPolling() {
        this.stopAlertPolling();
        this.fetchAlerts();
        this.alertTimer = setInterval(() => this.fetchAlerts(), Attention.ALERT_POLL_MS);
    }

    stopAlertPolling() {
        if (this.alertTimer) clearInterval(this.alertTimer);
        this.alertTimer = null;
    }

    async fetchAlerts() {
        if (!this.active) return;
        const token = this.token;
        let alerts;
        try {
            const response = await fetch('/api/alerts', { headers: this._authHeaders(), cache: 'no-store' });
            if (SESSION_REFUSALS.has(response.status)) {
                this.requestSessionCheck();
                return;
            }
            if (!response.ok) throw new Error(`alerts request failed: ${response.status}`);
            alerts = await response.json();
        } catch {
            alerts = { configured: true, available: false, alerts: [], note: 'Grafana alerts unavailable: showing the problems derived from the live feed only' };
        }
        if (!this.active || this.token !== token) return;
        this.alerts = alerts.configured ? alerts : null;
        this.updateAttention();
    }

    // ─── Deep links ───────────────────────────────────────────────────────────

    // The base URLs come from the console (never the page), behind the session like every data route.
    async fetchLinkBases() {
        if (!this.active) return;
        const token = this.token;
        let bases = { grafana: null, traces: null };
        try {
            const response = await fetch('/api/links', { headers: this._authHeaders(), cache: 'no-store' });
            if (SESSION_REFUSALS.has(response.status)) {
                this.requestSessionCheck();
                return;
            }
            if (response.ok) {
                const body = await response.json();
                bases = { grafana: body.grafana || null, traces: body.traces || null };
            }
        } catch {
            // No links is the safe answer when the bases cannot be read.
        }
        if (!this.active || this.token !== token) return;
        this.linkBases = bases;
        this._refreshLinks();
    }

    _linkRange() {
        return localStorage.getItem('console_range') || '24h';
    }

    // Register `container` as showing `specs` and draw it. An empty container stays hidden, so a
    // base URL that is unset leaves no stray label behind.
    _linkSet(container, specs, labelPrefix = '') {
        if (!container) return;
        this.linkSets.set(container, { specs, labelPrefix });
        this._renderLinkSet(container);
    }

    _renderLinkSet(container) {
        const { specs, labelPrefix } = this.linkSets.get(container);
        const range = this._linkRange();
        const links = specs.flatMap(spec => {
            const href = DeepLinks.resolve(this.linkBases, spec, range);
            if (!href) return [];
            const label = labelPrefix ? `${labelPrefix} ${spec.label.toLowerCase()}` : spec.label;
            const subject = spec.vars.service || spec.vars.queue || spec.vars.source;
            const name = `${label}${subject ? ` for ${subject}` : ''}${spec.dashboard === 'traces' ? '' : ' in Grafana'}`;
            return [this._el('a', {
                cls: 'mx-link',
                text: label,
                attrs: { href, target: '_blank', rel: 'noopener noreferrer', 'data-link': spec.dashboard, 'aria-label': `${name}, opens in a new tab` },
            })];
        });
        // Live updates redraw the drawer; keep the anchors (and a focused one) when nothing changed.
        const key = links.map(a => `${a.textContent}|${a.href}`).join('\n');
        if (container.dataset.linkKey === key) return;
        container.dataset.linkKey = key;
        container.replaceChildren(...links);
        container.hidden = links.length === 0;
    }

    _refreshLinks() {
        for (const container of [...this.linkSets.keys()]) {
            if (container.isConnected) this._renderLinkSet(container);
            else this.linkSets.delete(container);
        }
    }

    _producerOrConsumerSpecs(serviceName) {
        for (const pipeline of this.topology?.pipelines || []) {
            if (pipeline.producer.name === serviceName) return DeepLinks.specs.producer(pipeline.id, serviceName);
            if (pipeline.consumers.some(consumer => consumer.name === serviceName)) return DeepLinks.specs.consumer(serviceName);
        }
        return [];
    }

    // What an attention item links to besides taking the operator to the thing on this page.
    _attentionLinkSpecs(target) {
        if (target.type === 'service') return this._producerOrConsumerSpecs(target.service);
        if (target.type === 'cell') {
            const pipeline = this._pipeline(target.pipeline);
            const queue = pipeline?.consumers.find(c => c.name === target.consumer)?.queues.find(q => q.entity === target.entity);
            return queue ? DeepLinks.specs.queue(pipeline.id, queue.name, queue.dead_letter_queue).slice(0, 2) : [];
        }
        return [];
    }

    // ─── Initial data fetch ───────────────────────────────────────────────────

    async fetchInitialData() {
        try {
            await this.ensureTopology();
            const response = await fetch('/api/metrics', { headers: this._authHeaders() });
            if (SESSION_REFUSALS.has(response.status)) {
                this.requestSessionCheck();
            } else if (response.ok) {
                const data = await response.json();
                this.updateDashboard(data);
            }
        } catch (error) {
            this.addLogEntry('Failed to fetch initial data', 'error');
        }
    }

    // ─── Main update dispatcher ───────────────────────────────────────────────

    updateDashboard(data) {
        if (!this.topology) {
            // The layout is not drawn yet: keep the latest reading and apply it once it is.
            this.pendingData = data;
            this.ensureTopology().then(ready => {
                if (ready && this.pendingData) {
                    const pending = this.pendingData;
                    this.pendingData = null;
                    this.updateDashboard(pending);
                }
            });
            return;
        }
        const pipelines = data.pipelines || {};

        // Show/hide pipeline sections based on presence in data
        for (const { id } of this.topology.pipelines) {
            const section = document.getElementById(`pipeline-${id}`);
            if (section) {
                section.classList.toggle('hidden', !(id in pipelines));
            }
        }

        // Update each pipeline's services and queues
        const at = this._sampleTime(data.timestamp);
        for (const [pipelineName, pipelineData] of Object.entries(pipelines)) {
            this.updateServices(pipelineName, pipelineData.services || []);
            this.setPipelineDown(pipelineName, pipelineData.down === true);
            this.setQueuesUnavailable(pipelineName, pipelineData.queues_available === false);
            this.updateQueues(pipelineName, pipelineData.queues || [], at);
        }

        this.updatePlatform(data.platform || [], data.jobs || []);
        this.updateDatabases(data.databases || []);
        this.updateLastUpdated(data.timestamp);

        this.lastReading = data;
        this.updateAttention();
    }

    // ─── Service cards ────────────────────────────────────────────────────────

    _serviceBadgeClasses(status) {
        switch (status.toLowerCase()) {
            case 'healthy':
                return 'text-[10px] px-2 py-0.5 rounded uppercase font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
            case 'unhealthy':
                return 'text-[10px] px-2 py-0.5 rounded uppercase font-bold bg-red-500/10 text-red-400 border border-red-500/20';
            case 'extracting':
            case 'active':
                return 'text-[10px] px-2 py-0.5 rounded uppercase font-bold badge-active';
            default:
                return 'text-[10px] px-2 py-0.5 rounded uppercase font-bold bg-yellow-500/10 text-yellow-400 border border-yellow-500/20';
        }
    }

    _statusLabel(status) {
        switch (status.toLowerCase()) {
            case 'healthy':    return 'Healthy';
            case 'unhealthy':  return 'Unhealthy';
            case 'extracting': return 'Active';
            case 'unknown':    return 'Unknown';
            default:           return status;
        }
    }

    updateServices(pipelineName, services) {
        const pipeline = this._pipeline(pipelineName);
        const types = pipeline?.entity_types || [];

        services.forEach(service => {
            const prefix = `${pipelineName}-${service.name}`;
            const badge = document.getElementById(`${prefix}-status-badge`);
            if (badge) {
                badge.className = this._serviceBadgeClasses(service.status);
                badge.textContent = this._statusLabel(service.status);
            }

            // Producer: per-entity extraction state in the matrix row heads, from the health endpoint
            if (service.name === pipeline?.producer.name && service.extraction_progress) {
                const progress = service.extraction_progress;
                const elapsed = service.last_extraction_time || {};

                types.forEach(type => {
                    const el = document.getElementById(`${prefix}-${type}-state`);
                    if (!el) return;
                    const count = progress[type] || 0;
                    // Active if last extraction was within 30 seconds
                    const active = elapsed[type] != null && elapsed[type] < 30;
                    if (active) {
                        el.textContent = `Processing (${count.toLocaleString()})`;
                        el.className = 'mx-extract mx-extract-active';
                    } else {
                        el.textContent = 'Idle';
                        el.className = 'mx-extract';
                    }
                });

                // Update total records
                const totalEl = document.getElementById(`${prefix}-total-records`);
                if (totalEl) {
                    const total = progress.total || 0;
                    totalEl.textContent = total > 0 ? total.toLocaleString() : '—';
                }
            }
        });
    }

    // ─── Topology: the pipeline layout, drawn from /api/topology ──────────────

    _pipeline(pipelineName) {
        return this.topology?.pipelines.find(p => p.id === pipelineName);
    }

    // The topology is fetched with the session's bearer; a refusal hands the decision to admin.js.
    // Concurrent callers share one request, and a failure leaves `topology` unset so the next
    // metrics update tries again.
    ensureTopology() {
        if (this.topology) return Promise.resolve(true);
        this.topologyRequest ??= this._loadTopology().finally(() => { this.topologyRequest = null; });
        return this.topologyRequest;
    }

    async _loadTopology() {
        try {
            const response = await fetch('/api/topology', { headers: this._authHeaders() });
            if (SESSION_REFUSALS.has(response.status)) {
                this.requestSessionCheck();
                return false;
            }
            if (!response.ok) throw new Error(`topology request failed: ${response.status}`);
            this.renderTopology(await response.json());
            return true;
        } catch {
            this.addLogEntry('Failed to fetch the pipeline topology', 'error');
            return false;
        }
    }

    _el(tag, { cls, id, text, style, attrs } = {}, ...children) {
        const el = document.createElement(tag);
        if (cls) el.className = cls;
        if (id) el.id = id;
        if (text !== undefined) el.textContent = text;
        if (style) el.style.cssText = style;
        for (const [name, value] of Object.entries(attrs || {})) el.setAttribute(name, value);
        el.append(...children);
        return el;
    }

    _linkContainer(specs) {
        const container = this._el('span', { cls: 'mx-links', attrs: { 'aria-label': 'Grafana links' } });
        this._linkSet(container, specs);
        return container;
    }

    _entityLabel(entity) {
        return entity.split('-').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
    }

    _datastoreLabel(datastore) {
        return this.topology?.datastores.find(store => store.id === datastore)?.label || datastore;
    }

    // Build every pipeline's entity x consumer matrix from the topology. Sections start hidden;
    // updateDashboard reveals the ones the live metrics report. Element ids are derived from names,
    // so a new consumer or entity type needs no markup.
    renderTopology(topology) {
        this.topology = topology;
        this.closeQueueDrawer();
        this.closeTopology();
        this._resetHistory();
        const container = document.getElementById('pipelines');
        if (!container) return;
        container.replaceChildren(this._renderLegend(), ...topology.pipelines.map(pipeline => this._renderPipeline(pipeline)));
        this._applyMatrixLayout();
    }

    _renderLegend() {
        const swatch = (bin, label) => this._el('span', { cls: 'mx-legend-item' },
            this._el('i', { cls: `mx-swatch mx-lag-${bin}`, attrs: { 'aria-hidden': 'true' } }), label);
        return this._el('div', { cls: 'mx-legend', attrs: { 'data-matrix-legend': '' } },
            this._el('span', { text: 'Cell colour = lag:' }),
            ...QueueHealth.LAG_BINS.map(([, label], bin) => swatch(bin, label)),
            this._el('span', { cls: 'mx-legend-item' }, this._el('b', { cls: 'mx-crit-text', text: '◆' }), ' critical (hatched)'),
            this._el('span', { cls: 'mx-legend-item' }, this._el('b', { cls: 'mx-warn-text', text: '▲' }), ' warning'),
            this._el('span', { text: 'Number = messages queued; ↑/↓ = depth over 5 min' }));
    }

    _renderPipeline(pipeline) {
        const { producer, consumers, entity_types: types } = pipeline;
        const producerPrefix = `${pipeline.id}-${producer.name}`;
        const badge = (id) => this._el('span', {
            id,
            cls: 'text-[10px] bg-yellow-500/10 text-yellow-400 px-2 py-0.5 rounded border border-yellow-500/20 uppercase font-bold',
            text: 'Unknown',
        });

        const head = this._el('div', { cls: 'mx-pipeline-head' },
            this._el('h2', { text: `${pipeline.label} Pipeline` }),
            this._el('div', { id: `service-${producer.name}`, cls: 'mx-producer' },
                this._el('span', { cls: 'mx-producer-label', text: producer.label }),
                badge(`${producerPrefix}-status-badge`),
                this._el('span', { cls: 'mx-dim', text: 'Total records' }),
                this._el('span', { id: `${producerPrefix}-total-records`, cls: 'mx-high mono', text: '—' }),
                this._linkContainer(DeepLinks.specs.producer(pipeline.id, producer.name))),
            this._el('span', {
                cls: 'mx-unavailable-banner mx-down-banner hidden',
                text: 'Pipeline down: no service answered',
                attrs: { 'data-pipeline-down': pipeline.id, role: 'status' },
            }),
            this._el('span', {
                cls: 'mx-unavailable-banner hidden',
                text: 'Queue metrics unavailable',
                attrs: { 'data-queues-unavailable': pipeline.id, role: 'status' },
            }),
            this._topologyButton(pipeline));

        const cells = [this._el('div', { cls: 'mx-corner', attrs: { 'aria-hidden': 'true' } })];
        consumers.forEach(consumer => cells.push(this._el('div', { id: `service-${consumer.name}`, cls: 'mx-colhead' },
            this._el('b', { cls: 'mx-consumer', text: consumer.label }),
            this._el('span', { cls: 'mx-datastore', text: this._datastoreLabel(consumer.datastore), attrs: { 'data-datastore': consumer.datastore } }),
            badge(`${pipeline.id}-${consumer.name}-status-badge`),
            this._linkContainer(DeepLinks.specs.consumer(consumer.name)))));
        types.forEach(entity => {
            cells.push(this._el('div', { cls: 'mx-rowhead' },
                this._el('span', { cls: 'mx-entity', text: this._entityLabel(entity) }),
                this._el('span', { id: `${producerPrefix}-${entity}-state`, cls: 'mx-extract', text: '—' })));
            consumers.forEach(consumer => {
                const queue = consumer.queues.find(q => q.entity === entity);
                const cell = this._el('button', {
                    id: `${pipeline.id}-${consumer.name}-${entity}-cell`,
                    cls: 'mx-cell',
                    attrs: {
                        type: 'button',
                        'data-pipeline': pipeline.id,
                        'data-consumer': consumer.name,
                        'data-entity': entity,
                        'data-queue': queue?.name || '',
                        'data-state': 'unknown',
                        'aria-haspopup': 'dialog',
                    },
                },
                this._el('span', { cls: 'mx-cname', text: consumer.label }),
                this._el('span', { cls: 'mx-flag' }),
                this._el('span', { cls: 'mx-n mono', text: '—' }),
                this._el('span', { cls: 'mx-meta', text: 'not reported' }),
                this._el('span', { cls: 'mx-dlq' }));
                cell.addEventListener('click', () => this.openQueueDrawer(cell));
                cells.push(cell);
            });
        });
        const grid = this._el('div', {
            cls: 'mx-grid',
            style: `--mx-cols:${consumers.length}`,
            attrs: { 'data-matrix': pipeline.id, 'data-consumers': String(consumers.length) },
        }, ...cells);

        // The run's ETA headline sits between the head and the matrix; empty with no run in progress.
        const headline = this._el('p', { cls: 'mx-headline', attrs: { 'data-run-headline': pipeline.id, hidden: '' } });
        return this._el('section', { id: `pipeline-${pipeline.id}`, cls: 'mx-pipeline hidden' }, head, headline, grid);
    }

    // Stacked below the phone breakpoint when a pipeline has more than two consumers.
    _applyMatrixLayout() {
        const narrow = this.narrowViewport ? this.narrowViewport.matches : window.innerWidth <= MATRIX_STACKED_MAX_WIDTH;
        document.querySelectorAll('[data-matrix]').forEach(grid => {
            grid.classList.toggle('mx-stacked', matrixLayout(Number(grid.dataset.consumers), narrow) === 'stacked');
        });
    }

    // ─── Queue metrics ────────────────────────────────────────────────────────

    setQueuesUnavailable(pipelineName, unavailable) {
        this.queuesUnavailable[pipelineName] = unavailable;
        document.querySelectorAll(`[data-queues-unavailable="${pipelineName}"]`).forEach(el => {
            el.classList.toggle('hidden', !unavailable);
        });
    }

    setPipelineDown(pipelineName, down) {
        document.querySelectorAll(`[data-pipeline-down="${pipelineName}"]`).forEach(el => {
            el.classList.toggle('hidden', !down);
        });
        document.getElementById(`pipeline-${pipelineName}`)?.toggleAttribute('data-down', down);
    }

    // ─── Platform health: every long-running service outside the pipelines, and the jobs ─────

    _platformBadge(status) {
        return {
            healthy: ['ph-ok', 'Healthy'],
            unhealthy: ['ph-bad', 'Unhealthy'],
            unreachable: ['ph-bad', 'Unreachable'],
        }[status] || ['ph-unknown', status || 'Unknown'];
    }

    // One list per group, one row per service; a row's id is stable so an attention item can lead to it.
    updatePlatform(platform, jobs) {
        const section = document.getElementById('platform-health');
        if (!section) return;
        for (const list of section.querySelectorAll('[data-platform-group]')) {
            const group = list.dataset.platformGroup;
            const rows = platform.filter(service => service.group === group).map(service => {
                const [tone, label] = this._platformBadge(service.status);
                return this._el('li', { id: `platform-${service.name}`, cls: 'ph-row', attrs: { 'data-state': service.status, 'data-service': service.name } },
                    this._el('span', { cls: 'ph-name', text: service.name }),
                    this._el('span', { cls: `ph-badge ${tone}`, text: label }),
                    this._el('span', { cls: 'ph-detail', text: service.error || '' }));
            });
            list.replaceChildren(...rows);
        }
        const jobList = section.querySelector('[data-platform-jobs]');
        jobList?.replaceChildren(...jobs.map(job => {
            const when = job.last_run ? ` · ${new Date(job.last_run).toLocaleString()}` : '';
            return this._el('li', { id: `job-${job.name}`, cls: 'ph-row', attrs: { 'data-monitored': String(job.monitored), 'data-service': job.name } },
                this._el('span', { cls: 'ph-name', text: job.name }),
                this._el('span', { cls: `ph-badge ${job.monitored ? 'ph-ok' : 'ph-unknown'}`, text: job.monitored ? (job.outcome ? 'Last run' : 'No run') : 'Not monitored' }),
                this._el('span', { cls: 'ph-detail', text: job.monitored ? `${job.detail}${when}` : job.detail }));
        }));
    }

    // Live feed lost or session ended: the history no longer describes a continuous interval.
    _resetHistory() {
        this.history.clear();
        this.healthKinds = {};
    }

    // Server time of a reading in milliseconds, or now when the reading carries none.
    _sampleTime(timestamp) {
        const t = timestamp ? Date.parse(timestamp) : NaN;
        return Number.isNaN(t) ? Date.now() : t;
    }

    // Record the live queues into the per-queue history by exact name, for every work queue and
    // dead-letter queue the topology names. A queue the reading lacks has no history; a pipeline
    // whose scrape failed loses its history, since the gap breaks the stall and trend windows.
    updateQueues(pipelineName, queues, at = Date.now()) {
        const pipeline = this._pipeline(pipelineName);
        if (!pipeline) return;

        const live = new Map(queues.map(queue => [queue.name, queue]));
        const unavailable = this.queuesUnavailable[pipelineName] === true;
        for (const consumer of pipeline.consumers) {
            for (const queue of consumer.queues) {
                for (const name of [queue.name, queue.dead_letter_queue]) {
                    const reading = live.get(name);
                    if (unavailable || !reading) {
                        this.history.delete(name);
                        continue;
                    }
                    const samples = this.history.get(name) || [];
                    QueueHealth.record(samples, {
                        t: at,
                        messages: reading.messages,
                        messages_ready: reading.messages_ready,
                        messages_unacknowledged: reading.messages_unacknowledged,
                        consumers: reading.consumers,
                        message_rate: reading.message_rate ?? null,
                        ack_rate: reading.ack_rate ?? null,
                    });
                    this.history.set(name, samples);
                }
            }
        }
        this._renderQueueValues(pipeline);
    }

    // The assessed state of one cell: its work queue, its dead-letter queue, and the lag.
    _cellModel(pipeline, consumer, entity) {
        const queue = consumer.queues.find(q => q.entity === entity);
        const samples = (queue && this.history.get(queue.name)) || [];
        const dlqSamples = (queue && this.history.get(queue.dead_letter_queue)) || [];
        const unavailable = this.queuesUnavailable[pipeline.id] === true;
        const health = unavailable ? { state: 'unknown', kind: 'unavailable', label: 'metrics unavailable' } : QueueHealth.assessQueue(samples);
        const dlq = unavailable ? { state: 'unknown', kind: 'unavailable', label: 'DLQ unavailable' } : QueueHealth.assessDlq(dlqSamples);
        const lag = QueueHealth.lagSeconds(samples);
        return {
            pipeline, consumer, entity, queue, samples, dlqSamples, unavailable, health, dlq, lag,
            last: samples[samples.length - 1] || null,
            dlqLast: dlqSamples[dlqSamples.length - 1] || null,
            state: QueueHealth.worst(health.state, dlq.state),
            trend: QueueHealth.trend(samples),
        };
    }

    _lagText(lag) {
        if (lag === null) return 'lag unknown';
        if (lag === 0) return 'no lag';
        return `lag ${QueueHealth.formatDuration(lag)}`;
    }

    _lagValue(lag) {
        if (lag === null) return 'unknown';
        return lag === 0 ? 'none' : QueueHealth.formatDuration(lag);
    }

    _renderQueueValues(pipeline) {
        for (const consumer of pipeline.consumers) {
            for (const entity of pipeline.entity_types) {
                const cell = document.getElementById(`${pipeline.id}-${consumer.name}-${entity}-cell`);
                const model = this._cellModel(pipeline, consumer, entity);
                if (cell) this._renderCell(cell, model);
                this._logHealthChange(model);
            }
        }
        if (this.drawerQueue?.pipeline === pipeline.id) this._renderDrawer();
        if (this.topologyPipeline === pipeline.id) this._fillTopologyFlow();
        this._renderHeadline(pipeline);
        if (this.overviewView === 'progress') this._renderRunPipeline(pipeline);
    }

    _renderCell(cell, model) {
        const { health, dlq, lag, last, trend, unavailable } = model;
        const bin = QueueHealth.lagBin(lag);
        const critical = model.state === 'crit';
        let look = 'mx-lag-unknown';
        if (unavailable) look = 'mx-unavailable';
        else if (critical) look = 'mx-crit';
        else if (last && bin !== null) look = `mx-lag-${bin}`;
        cell.className = `mx-cell ${look}`;
        cell.dataset.state = model.state;
        cell.dataset.kind = health.kind;
        cell.dataset.lagBin = bin === null ? '' : String(bin);

        const [, flag, count, meta, dlqBadge] = cell.children;
        flag.textContent = health.state === 'crit' ? `◆ ${health.label}` : '';
        const arrow = { rising: ' ↑', falling: ' ↓', steady: '' }[trend.direction];
        count.textContent = last ? `${QueueHealth.formatCount(last.messages)}${arrow}` : '—';
        if (unavailable) meta.textContent = 'unavailable';
        else meta.textContent = last ? this._lagText(lag) : 'not reported';

        dlqBadge.className = 'mx-dlq';
        dlqBadge.dataset.state = dlq.state;
        dlqBadge.dataset.kind = dlq.kind;
        if (dlq.state === 'crit' || dlq.state === 'warn') {
            dlqBadge.classList.add(dlq.state === 'crit' ? 'mx-dlq-crit' : 'mx-dlq-warn');
            dlqBadge.textContent = `${dlq.state === 'crit' ? '◆' : '▲'} ${dlq.label}`;
        } else {
            dlqBadge.textContent = '';
        }

        cell.setAttribute('aria-label', this._cellLabel(model));
    }

    // What a screen reader hears for one queue, in the matrix and in the topology.
    _cellLabel({ entity, consumer, health, dlq, lag, last, trend, unavailable }) {
        const label = [`${this._entityLabel(entity)} to ${consumer.label}`];
        if (unavailable) label.push('queue metrics unavailable');
        else if (!last) label.push('not reported');
        else {
            label.push(`${last.messages.toLocaleString()} messages queued${trend.direction === 'steady' ? '' : `, ${trend.direction}`}`);
            label.push(this._lagText(lag));
        }
        if (health.state === 'crit') label.push(`critical: ${health.label}`);
        if (dlq.state === 'crit') label.push(`critical: ${dlq.label}`);
        if (dlq.state === 'warn') label.push(`warning: ${dlq.label}`);
        return label.join('; ');
    }

    // Log a queue entering a critical or warning state, and leaving one, once per change.
    _logHealthChange(model) {
        const where = `${this._entityLabel(model.entity)} → ${model.consumer.label}`;
        for (const [name, health] of [[model.queue?.name, model.health], [model.queue?.dead_letter_queue, model.dlq]]) {
            if (!name || health.state === 'unknown') continue;
            const previous = this.healthKinds[name];
            this.healthKinds[name] = health.kind;
            if (previous === health.kind) continue;
            if (health.state === 'crit' || health.state === 'warn') {
                this.addLogEntry(`${where}: ${health.label}`, health.state === 'crit' ? 'error' : 'warning');
            } else if (previous && !['drained', 'flowing', 'dlq-empty'].includes(previous)) {
                this.addLogEntry(`${where}: ${health.label}`, 'info');
            }
        }
    }

    // ─── Queue detail drawer ──────────────────────────────────────────────────

    openQueueDrawer(cell) {
        const drawer = document.getElementById('queue-drawer');
        if (!drawer) return;
        this.drawerQueue = { pipeline: cell.dataset.pipeline, consumer: cell.dataset.consumer, entity: cell.dataset.entity };
        this.drawerReturnFocus = cell;
        this._renderDrawer();
        if (!drawer.open) {
            if (typeof drawer.showModal === 'function') drawer.showModal();
            else drawer.setAttribute('open', '');
        }
    }

    closeQueueDrawer() {
        const drawer = document.getElementById('queue-drawer');
        if (drawer?.open) {
            if (typeof drawer.close === 'function') drawer.close();
            else drawer.removeAttribute('open');
        }
        this._onDrawerClosed();
    }

    _onDrawerClosed() {
        const target = this.drawerReturnFocus;
        this.drawerQueue = null;
        this.drawerReturnFocus = null;
        if (target?.isConnected) target.focus();
    }

    _renderDrawer() {
        const ref = this.drawerQueue;
        const pipeline = ref && this._pipeline(ref.pipeline);
        const consumer = pipeline?.consumers.find(c => c.name === ref.consumer);
        const title = document.getElementById('queue-drawer-title');
        const subtitle = document.getElementById('queue-drawer-subtitle');
        const state = document.getElementById('queue-drawer-state');
        const numbers = document.getElementById('queue-drawer-numbers');
        if (!consumer || !title || !numbers) return;
        const model = this._cellModel(pipeline, consumer, ref.entity);
        const { health, dlq, last, dlqLast, samples, dlqSamples, unavailable } = model;

        const links = document.getElementById('queue-drawer-links');
        if (links) {
            const queue = model.queue;
            this._linkSet(links, [
                ...(queue ? DeepLinks.specs.queue(pipeline.id, queue.name, queue.dead_letter_queue) : []),
                ...DeepLinks.specs.consumer(consumer.name).map(spec => ({ ...spec, label: `Consumer ${spec.label === 'Traces' ? 'traces' : ''}`.trim() })),
            ]);
        }

        title.textContent = `${this._entityLabel(ref.entity)} → ${consumer.label}`;
        if (subtitle) subtitle.textContent = `${pipeline.label} pipeline · ${this._datastoreLabel(consumer.datastore)}`;

        if (state) {
            const glyph = { crit: '◆ ', warn: '▲ ' };
            const lines = [health, dlq].filter(h => h.state === 'crit' || h.state === 'warn');
            if (unavailable) state.textContent = 'Queue metrics unavailable';
            else if (lines.length) state.textContent = lines.map(h => `${glyph[h.state]}${h.label}`).join(' · ');
            else state.textContent = health.state === 'ok' ? `OK · ${health.label}` : health.label;
            state.dataset.state = model.state;
        }

        const dash = '—';
        const num = (n) => (n === null || n === undefined ? dash : n.toLocaleString());
        const rate = (r) => (r === null || r === undefined ? 'unknown' : `${this._formatRate(r)}/s`);
        const span = (s) => QueueHealth.formatDuration(QueueHealth.coverage(s));
        const change = (s) => {
            if (s.length < 2) return dash;
            const d = QueueHealth.depthChange(s);
            return `${d > 0 ? '+' : ''}${d.toLocaleString()} over ${span(s)}`;
        };
        const meanAck = QueueHealth.meanAckRate(samples);
        const zero = QueueHealth.zeroAckSeconds(samples);
        const rows = [
            ['Queue', model.queue?.name || dash],
            ['Messages', num(last?.messages)],
            ['Ready', num(last?.messages_ready)],
            ['Unacknowledged', num(last?.messages_unacknowledged)],
            ['Consumers', num(last?.consumers)],
            ['Publish rate', last ? rate(last.message_rate) : dash],
            ['Ack rate', last ? rate(last.ack_rate) : dash],
            ['Mean ack rate (5 min)', last ? rate(meanAck) : dash],
            ['Lag', last ? this._lagValue(model.lag) : dash],
            ['Backlog change', change(samples)],
            ['No acks for', zero > 0 ? QueueHealth.formatDuration(zero) : dash],
            ['Dead-letter queue', model.queue?.dead_letter_queue || dash],
            ['DLQ messages', num(dlqLast?.messages)],
            ['DLQ change', change(dlqSamples)],
            ['History', samples.length > 1 ? `${samples.length} samples over ${span(samples)}` : `${samples.length} sample${samples.length === 1 ? '' : 's'}`],
        ];
        numbers.replaceChildren(...rows.flatMap(([term, value]) => [
            this._el('dt', { text: term }),
            this._el('dd', { cls: 'mono', text: unavailable && term !== 'Queue' && term !== 'Dead-letter queue' ? dash : value }),
        ]));
    }

    // ─── Topology drill-down: one pipeline's flow, on desktop widths only ─────

    _isWideViewport() {
        return this.wideViewport ? this.wideViewport.matches : window.innerWidth >= TopologyFlow.MIN_WIDTH;
    }

    // The control in a pipeline's matrix head; index.html hides it below TopologyFlow.MIN_WIDTH.
    _topologyButton(pipeline) {
        const button = this._el('button', {
            cls: 'mx-topology-btn',
            text: 'Topology',
            attrs: {
                type: 'button',
                'data-topology': pipeline.id,
                'aria-haspopup': 'dialog',
                'aria-label': `Show the ${pipeline.label} pipeline topology`,
            },
        });
        button.addEventListener('click', () => this.openTopology(pipeline.id, button));
        return button;
    }

    openTopology(pipelineId, trigger = null) {
        const dialog = document.getElementById('topology-dialog');
        const pipeline = this._pipeline(pipelineId);
        if (!dialog || !pipeline || !this._isWideViewport()) return;
        this.topologyPipeline = pipeline.id;
        this.topologyReturnFocus = trigger;
        const title = document.getElementById('topology-title');
        if (title) title.textContent = `${pipeline.label} pipeline topology`;
        this._buildTopologyFlow(pipeline);
        this._fillTopologyFlow();
        if (!dialog.open) {
            if (typeof dialog.showModal === 'function') dialog.showModal();
            else dialog.setAttribute('open', '');
        }
    }

    closeTopology() {
        const dialog = document.getElementById('topology-dialog');
        if (dialog?.open) {
            if (typeof dialog.close === 'function') dialog.close();
            else dialog.removeAttribute('open');
        }
        this._onTopologyClosed();
    }

    _onTopologyClosed() {
        const target = this.topologyReturnFocus;
        this.topologyPipeline = null;
        this.topologyReturnFocus = null;
        document.getElementById('topology-flow')?.replaceChildren();
        if (target?.isConnected) target.focus();
    }

    _svg(tag, attrs = {}, ...children) {
        const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
        for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, String(value));
        el.append(...children);
        return el;
    }

    _svgText(x, y, cls, text = '') {
        const el = this._svg('text', { x, y, class: cls });
        el.textContent = text;
        return el;
    }

    // Draw the pipeline's nodes and edges once; _fillTopologyFlow writes the live values into them.
    // Each node and edge carries `data-node` / `data-edge` with its layout key.
    _buildTopologyFlow(pipeline) {
        const container = document.getElementById('topology-flow');
        if (!container) return;
        const { width, height, columns, nodes, edges } = TopologyFlow.layout(pipeline);
        const svg = this._svg('svg', {
            class: 'tp-svg', width, height, viewBox: `0 0 ${width} ${height}`,
            role: 'group', 'aria-label': `${pipeline.label} pipeline flow`, 'data-flow': pipeline.id,
        });
        columns.forEach(({ x, title }) => svg.append(this._svgText(x, 12, 'tp-col', title)));
        // Edges first, so nodes sit on top of them.
        edges.forEach(({ key, d }) => svg.append(this._svg('path', { class: 'tp-edge', d, 'data-edge': key }, this._svg('title'))));
        nodes.forEach(n => svg.append(this._topologyNode(pipeline, n)));
        container.replaceChildren(svg);
    }

    _topologyNode(pipeline, n) {
        const { x, y, w, h } = n;
        const g = this._svg('g', { class: `tp-node tp-${n.kind}`, 'data-node': n.key, 'data-kind': n.kind, 'data-state': 'unknown' },
            this._svg('rect', { class: 'tp-box', x, y, width: w, height: h, rx: 8 }));
        const label = (text) => this._svgText(x + 10, y + (n.kind === 'queue' ? 17 : 18), 'tp-name', text);
        const sub = (dy, text = '') => this._svgText(x + 10, y + dy, 'tp-sub', text);
        const consumer = pipeline.consumers.find(c => c.name === n.consumer);
        if (n.kind === 'producer') g.append(label(pipeline.producer.label), sub(36, pipeline.producer.name), sub(60));
        if (n.kind === 'exchange') g.append(label(this._entityLabel(n.entity)), sub(34));
        if (n.kind === 'datastore') g.append(this._svgText(x + 10, y + 27, 'tp-name', this._datastoreLabel(n.datastore)));
        if (n.kind === 'consumer') g.append(label(consumer.label), sub(36));
        if (n.kind === 'queue') {
            const name = this._svgText(x + 10, y + 17, 'tp-name');
            name.append(this._svg('tspan', { 'font-weight': 600 }), ` → ${consumer.label}`);
            name.firstChild.textContent = this._entityLabel(n.entity);
            g.append(name, sub(33),
                this._svg('g', { class: 'tp-dlq', 'data-state': 'ok' },
                    this._svg('rect', { class: 'tp-pill', x: x + 8, y: y + 39, width: 0, height: 14, rx: 7 }),
                    this._svgText(x + 16, y + 50, 'tp-pill-text')));
            // A queue opens the matrix's own detail drawer, and focus comes back here.
            Object.assign(g.dataset, { pipeline: pipeline.id, consumer: n.consumer, entity: n.entity });
            g.setAttribute('tabindex', '0');
            g.setAttribute('role', 'button');
            g.setAttribute('aria-haspopup', 'dialog');
            g.addEventListener('click', () => this.openQueueDrawer(g));
            g.addEventListener('keydown', (event) => {
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault();
                this.openQueueDrawer(g);
            });
        }
        return g;
    }

    _rateText(rate, suffix) {
        return rate === null || rate === undefined ? 'rate unknown' : `${this._formatRate(rate)}/s ${suffix}`;
    }

    // Write the live values into the open flow: every queue's state is its matrix cell's model, a
    // consumer takes the worst state of its queues, and edges are sized from the latest rates.
    _fillTopologyFlow() {
        const pipeline = this._pipeline(this.topologyPipeline);
        const svg = document.querySelector(`#topology-flow [data-flow="${this.topologyPipeline}"]`);
        if (!pipeline || !svg) return;
        const nodeEl = key => svg.querySelector(`[data-node="${key}"]`);
        const setEdge = (from, to, rate, what) => {
            const path = svg.querySelector(`[data-edge="${from}>${to}"]`);
            if (!path) return;
            const style = TopologyFlow.edgeStyle(rate);
            path.setAttribute('class', `tp-edge tp-edge-${style}`);
            path.setAttribute('stroke-width', TopologyFlow.edgeWidth(rate).toFixed(1));
            path.dataset.rate = rate === null ? '' : String(rate);
            path.firstChild.textContent = `${what}: ${this._rateText(rate, '').trim()}`;
        };
        const setState = (el, state) => {
            el.dataset.state = state;
            el.classList.remove('tp-ok', 'tp-warn', 'tp-crit', 'tp-unknown');
            el.classList.add(`tp-${state}`);
        };
        const glyph = { crit: '◆ ', warn: '▲ ' };
        const rateOf = (model, field) => (model.unavailable || !model.last ? null : model.last[field] ?? null);

        const models = pipeline.consumers.flatMap(consumer => pipeline.entity_types.map(entity => this._cellModel(pipeline, consumer, entity)));
        for (const model of models) {
            const key = `queue:${model.consumer.name}:${model.entity}`;
            const g = nodeEl(key);
            if (!g) continue;
            const { health, dlq, last, lag, unavailable } = model;
            setState(g, model.state);
            g.dataset.kind = health.kind;
            g.setAttribute('aria-label', this._cellLabel(model));
            const status = g.querySelector('.tp-sub');
            const depth = last ? `${QueueHealth.formatCount(last.messages)} queued` : '';
            status.setAttribute('class', health.state === 'crit' ? 'tp-sub tp-crit-text' : 'tp-sub');
            if (unavailable) status.textContent = 'queue metrics unavailable';
            else if (!last) status.textContent = 'not reported';
            else if (health.state === 'crit') status.textContent = `${glyph.crit}${health.label} · ${depth}`;
            else status.textContent = `${depth} · ${this._lagText(lag)}`;
            const pill = g.querySelector('.tp-dlq');
            pill.dataset.state = dlq.state;
            pill.dataset.kind = dlq.kind;
            const pillText = dlq.state === 'crit' || dlq.state === 'warn' ? `${glyph[dlq.state]}${dlq.label}` : '';
            pill.querySelector('.tp-pill-text').textContent = pillText;
            // Sized to its text at 11 px bold, so the badge reads as a badge rather than a bar.
            pill.querySelector('.tp-pill').setAttribute('width', String(Math.round(16 + 6.6 * pillText.length)));
            setEdge(`exchange:${model.entity}`, key, rateOf(model, 'message_rate'), `${this._entityLabel(model.entity)} → ${model.consumer.label} published`);
            setEdge(key, `consumer:${model.consumer.name}`, rateOf(model, 'ack_rate'), `${this._entityLabel(model.entity)} → ${model.consumer.label} acknowledged`);
        }

        let out = null;
        for (const entity of pipeline.entity_types) {
            const rate = TopologyFlow.exchangeRate(models.filter(m => m.entity === entity).map(m => rateOf(m, 'message_rate')));
            if (rate !== null) out = (out ?? 0) + rate;
            const exchange = nodeEl(`exchange:${entity}`);
            if (exchange) exchange.querySelector('.tp-sub').textContent = this._rateText(rate, 'in');
            setEdge('producer', `exchange:${entity}`, rate, `${this._entityLabel(entity)} published`);
        }
        const producer = nodeEl('producer');
        if (producer) producer.querySelectorAll('.tp-sub')[1].textContent = this._rateText(out, 'out');

        for (const consumer of pipeline.consumers) {
            const own = models.filter(m => m.consumer === consumer);
            const state = QueueHealth.worst(...own.map(m => m.state));
            const acked = TopologyFlow.sumRates(own.map(m => rateOf(m, 'ack_rate')));
            const g = nodeEl(`consumer:${consumer.name}`);
            if (g) {
                setState(g, state);
                const counts = ['crit', 'warn'].map(s => [s, own.filter(m => m.state === s).length]).filter(([, n]) => n);
                const words = { crit: 'critical', warn: 'warning' };
                const status = g.querySelector('.tp-sub');
                status.setAttribute('class', `tp-sub${counts.length ? ` tp-${counts[0][0]}-text` : ''}`);
                status.textContent = counts.length
                    ? counts.map(([s, n]) => `${glyph[s]}${n} ${words[s]}`).join(' · ')
                    : this._rateText(acked, 'acked');
            }
            setEdge(`consumer:${consumer.name}`, `datastore:${consumer.datastore}`, acked, `${consumer.label} → ${this._datastoreLabel(consumer.datastore)} written`);
        }
    }

    // ─── Run progress: the ETA headline and the Run progress view ──────────────

    // Runs come from the extraction history through the console's /admin/api proxy with this
    // session's bearer, like the audit log; their progress from /api/progress.
    startProgressPolling() {
        this.stopProgressPolling();
        this.fetchRunProgress();
        this.progressTimer = setInterval(() => this.fetchRunProgress(), RunProgress.POLL_MS);
    }

    stopProgressPolling() {
        if (this.progressTimer) clearInterval(this.progressTimer);
        this.progressTimer = null;
    }

    async fetchRunProgress() {
        if (!this.active) return;
        const token = this.token;
        try {
            const response = await fetch('/admin/api/extractions', { headers: this._authHeaders(), cache: 'no-store' });
            if (SESSION_REFUSALS.has(response.status)) {
                this.requestSessionCheck();
                return;
            }
            // A failed read keeps the runs already known; their progress still refreshes below.
            if (response.ok) {
                const body = await response.json();
                if (!this.active || this.token !== token) return;
                this.runs = RunProgress.latestRuns(body.extractions);
            }
        } catch {
            // Offline or malformed: keep what is known.
        }
        if (!this.active || this.token !== token || !(await this.ensureTopology())) return;
        await Promise.all(this.topology.pipelines.map(pipeline => this._fetchPipelineProgress(pipeline, token)));
        if (!this.active || this.token !== token) return;
        for (const pipeline of this.topology.pipelines) this._renderHeadline(pipeline);
        this.renderRunProgress();
    }

    // What /api/progress says for the pipeline's run. A finished run's numbers do not change, so
    // they are read once; a 503 from VictoriaMetrics is "history unavailable", not a session refusal.
    async _fetchPipelineProgress(pipeline, token) {
        const run = this.runs[pipeline.id];
        if (!run) {
            delete this.runProgress[pipeline.id];
            return;
        }
        const end = RunProgress.isRunning(run) ? null : run.completed_at || null;
        const key = `${run.id}|${run.started_at}|${end}`;
        if (end && this.runProgress[pipeline.id]?.key === key && this.runProgress[pipeline.id].data) return;
        const params = new URLSearchParams({ start: run.started_at, ...(end ? { end } : {}) });
        let entry;
        try {
            const response = await fetch(`/api/progress/${encodeURIComponent(pipeline.id)}?${params}`, { headers: this._authHeaders(), cache: 'no-store' });
            const body = await response.json().catch(() => ({}));
            if (response.status === 401 || (response.status === 503 && body.detail !== 'history unavailable')) {
                this.requestSessionCheck();
                return;
            }
            entry = response.ok ? { key, data: body, error: null } : { key, data: null, error: body.detail || `progress request failed: ${response.status}` };
        } catch {
            entry = { key, data: null, error: 'progress request failed' };
        }
        if (this.active && this.token === token) this.runProgress[pipeline.id] = entry;
    }

    // Every cell of one pipeline as the run sees it: the matrix's assessment, the live 5-minute
    // mean ack rate and DLQ depth, and the run's expected and loaded counts.
    _progressCells(pipeline) {
        const data = this.runProgress[pipeline.id]?.data;
        const entities = new Map((data?.entities || []).map(e => [e.entity, e]));
        const queues = new Map((data?.queues || []).map(q => [q.queue, q]));
        return pipeline.consumers.flatMap(consumer => pipeline.entity_types.flatMap(entity => {
            const model = this._cellModel(pipeline, consumer, entity);
            if (!model.queue) return [];
            const totals = entities.get(entity);
            const acked = queues.get(model.queue.name);
            const entityLabel = this._entityLabel(entity);
            return [{
                consumer: consumer.name, consumerLabel: consumer.label, entity, entityLabel, queue: model.queue.name,
                health: model.health, dlq: model.dlq,
                expected: totals?.expected ?? null, extracted: totals?.extracted ?? null, fileProgress: totals?.file_progress ?? null,
                loaded: acked?.loaded ?? null, points: acked?.points || [],
                dlqDepth: model.dlqLast?.messages ?? 0, meanAck: QueueHealth.meanAckRate(model.samples),
                gap: RunProgress.gap({ entityLabel, consumerLabel: consumer.label, totals, acked }),
            }];
        }));
    }

    _runHeadline(pipeline) {
        const run = this.runs[pipeline.id];
        return RunProgress.headline({ running: RunProgress.isRunning(run), error: this.runProgress[pipeline.id]?.error, cells: this._progressCells(pipeline) });
    }

    // The headline's parts: a critical one carries ◆, like every critical mark on the Overview.
    _headlineParts(headline) {
        const parts = [];
        if (headline.state === 'crit') parts.push(this._el('b', { cls: 'mx-crit-text', text: '◆ ', attrs: { 'aria-hidden': 'true' } }));
        parts.push(this._el('b', { cls: headline.state === 'crit' ? 'mx-crit-text' : 'mx-headline-text', text: headline.text }));
        if (headline.detail) parts.push(this._el('span', { cls: 'mx-dim', text: `, ${headline.detail}` }));
        return parts;
    }

    _renderHeadline(pipeline) {
        const el = document.querySelector(`[data-run-headline="${pipeline.id}"]`);
        if (!el) return;
        const headline = this._runHeadline(pipeline);
        el.hidden = !headline;
        el.dataset.kind = headline?.kind || '';
        el.dataset.state = headline?.state || '';
        const text = headline ? `${headline.text}${headline.detail ? `, ${headline.detail}` : ''}` : '';
        if (el.dataset.text === text) return;
        el.dataset.text = text;
        el.replaceChildren(...(headline ? this._headlineParts(headline) : []));
    }

    // Matrix (primary) or Run progress (secondary); the attention bar and headlines stay either way.
    showOverviewView(view) {
        this.overviewView = view === 'progress' ? 'progress' : 'matrix';
        const matrix = document.getElementById('pipelines');
        const progress = document.getElementById('run-progress');
        if (matrix) matrix.hidden = this.overviewView !== 'matrix';
        if (progress) progress.hidden = this.overviewView !== 'progress';
        document.querySelectorAll('[data-overview-view]').forEach(button => {
            button.setAttribute('aria-pressed', String(button.dataset.overviewView === this.overviewView));
        });
        if (this.overviewView === 'progress') this.renderRunProgress();
    }

    renderRunProgress() {
        const container = document.getElementById('run-progress');
        if (!container || !this.topology) return;
        for (const pipeline of this.topology.pipelines) {
            if (!document.getElementById(`run-progress-${pipeline.id}`)) {
                container.append(this._el('section', { id: `run-progress-${pipeline.id}`, cls: 'rp-pipeline' }));
            }
            this._renderRunPipeline(pipeline);
        }
    }

    // One pipeline's run: summary, burn-up, and per entity x consumer bars. Redrawn on every live
    // reading while shown, so a focused run link is restored.
    _renderRunPipeline(pipeline) {
        const section = document.getElementById(`run-progress-${pipeline.id}`);
        if (!section) return;
        const run = this.runs[pipeline.id];
        const entry = this.runProgress[pipeline.id];
        const running = RunProgress.isRunning(run);
        const focused = section.contains(document.activeElement) && document.activeElement.dataset.runLink;
        section.dataset.runState = run ? (running ? 'running' : String(run.status || 'finished')) : 'none';

        const title = this._el('h2', { text: `${pipeline.label} run` });
        if (!run) {
            section.replaceChildren(this._el('div', { cls: 'rp-head' }, title), this._el('p', { cls: 'rp-summary mx-dim', text: 'No extraction run recorded' }));
            return;
        }
        const started = Date.parse(run.started_at);
        const link = this._el('button', {
            cls: 'rp-run-link', text: 'Run details',
            attrs: { type: 'button', 'data-run-link': String(run.id), 'aria-label': `${pipeline.label} run details in the Extractions tab` },
        });
        link.addEventListener('click', () => this.goToRun(run.id));
        const when = running
            ? `started ${QueueHealth.formatDuration((Date.now() - started) / 1000)} ago`
            : `started ${new Date(started).toLocaleString()}`;
        const head = this._el('div', { cls: 'rp-head' }, title, this._el('span', { cls: 'mx-dim', text: when }), link);

        const cells = this._progressCells(pipeline);
        const summary = this._el('p', { cls: 'rp-summary', attrs: { 'data-run-summary': pipeline.id } });
        if (running) {
            const headline = this._runHeadline(pipeline);
            summary.dataset.kind = headline.kind;
            summary.append(...this._headlineParts(headline));
        } else {
            summary.dataset.kind = String(run.status || 'finished');
            summary.textContent = this._finishedSummary(run);
        }
        const parts = [head, summary];
        if (entry?.error) {
            parts.push(this._el('p', { cls: 'rp-unavailable', text: `Progress unavailable: ${entry.error}` }));
        } else if (entry?.data) {
            parts.push(...this._renderBurnup(pipeline, cells, entry.data, running));
            parts.push(...pipeline.entity_types.map(entity => this._renderRunEntity(entity, run, running, cells.filter(c => c.entity === entity))));
        }
        section.replaceChildren(...parts);
        if (focused) section.querySelector(`[data-run-link="${focused}"]`)?.focus();
    }

    // "Completed in 3h12m · 12,345,678 records", or how a failed run ended.
    _finishedSummary(run) {
        const seconds = RunProgress.duration(run);
        const took = seconds === null ? '' : ` in ${QueueHealth.formatDuration(seconds)}`;
        const counts = run.record_counts || {};
        const total = counts.total ?? Object.values(counts).reduce((a, b) => a + (Number(b) || 0), 0);
        const records = Object.keys(counts).length ? ` · ${Number(total).toLocaleString()} records` : '';
        if (run.status === 'failed') return `Failed${seconds === null ? '' : ` after ${QueueHealth.formatDuration(seconds)}`}${run.error_message ? `: ${run.error_message}` : ''}${records}`;
        return `${run.status === 'completed' ? 'Completed' : this._statusText(run.status)}${took}${records}`;
    }

    _statusText(status) {
        const text = String(status || 'finished');
        return text.charAt(0).toUpperCase() + text.slice(1);
    }

    // The burn-up: loaded fraction per consumer since the run started, dashed to 100 % at its ETA.
    // Drawn only when every entity's expected total and every queue's baseline are known.
    _renderBurnup(pipeline, cells, data, running) {
        const missing = cells.map(cell => cell.gap).find(Boolean);
        if (missing) return [this._el('p', { cls: 'rp-unavailable', text: `Progress unavailable: ${missing}` })];
        const totalExpected = (data.entities || []).reduce((a, e) => a + (e.expected || 0), 0);
        if (!totalExpected) return [];
        const start = Date.parse(data.start);
        const now = Date.parse(data.end);
        const series = pipeline.consumers.map(consumer => {
            const own = cells.filter(cell => cell.consumer === consumer.name);
            // Nothing is loaded at the start: the baseline is the acked count then.
            return { consumer: consumer.name, points: [[start, 0], ...RunProgress.burnupSeries(own, totalExpected)], eta: running ? RunProgress.consumerEta(own) : null };
        });
        // Drawn at the section's own width, so the axis labels are not stretched.
        const section = document.getElementById(`run-progress-${pipeline.id}`);
        const width = Math.max(300, (section?.clientWidth || 664) - 24), height = 90;
        const geometry = RunProgress.burnup({ start, now, series, width, height });
        const svg = this._svg('svg', { class: 'rp-burnup', width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `${pipeline.label}: loaded fraction per consumer over the run` },
            this._svg('line', { class: 'rp-axis', x1: 34, y1: geometry.base, x2: width, y2: geometry.base }),
            this._svg('text', { x: 0, y: geometry.top + 4 }, '100%'),
            this._svg('text', { x: 0, y: geometry.base }, '0%'),
            this._svg('line', { class: 'rp-axis rp-now', x1: geometry.nowX, y1: 0, x2: geometry.nowX, y2: geometry.base }),
            ...geometry.lines.flatMap((line, i) => [
                this._svg('polyline', { class: `rp-line rp-series-${i % 4}`, points: line.points, 'data-consumer': line.consumer }),
                ...(line.projection ? [this._svg('polyline', { class: `rp-projection rp-series-${i % 4}`, points: line.projection, 'data-projection': line.consumer })] : []),
            ]));
        const legend = this._el('div', { cls: 'mx-legend rp-legend' },
            ...pipeline.consumers.map((consumer, i) => this._el('span', { cls: 'mx-legend-item' },
                this._el('i', { cls: `rp-swatch rp-series-${i % 4}`, attrs: { 'aria-hidden': 'true' } }), consumer.label)),
            this._el('span', { cls: 'mx-dim', text: running ? 'dashed = projection at the 5-minute mean ack rate' : 'loaded over the run' }));
        return [svg, legend];
    }

    _svg(tag, attrs = {}, ...children) {
        const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
        for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, String(value));
        el.append(...children);
        return el;
    }

    _percent(fraction) {
        return `${Math.round(Math.min(1, Math.max(0, fraction)) * 100)}%`;
    }

    // One entity: its expected total and how much is read, then a bar per consumer of loaded vs
    // extracted-but-queued, with the consumer's ETA or state.
    _renderRunEntity(entity, run, running, cells) {
        // Every cell of an entity shares its totals; the extractor's final counts key release-groups as release_groups.
        const totals = cells[0];
        const final = run.record_counts?.[entity] ?? run.record_counts?.[entity.replace(/-/g, '_')];
        let detail;
        if (!running && final !== undefined && final !== null) detail = `${Number(final).toLocaleString()} extracted`;
        else if (!totals || totals.expected === null) detail = `progress unavailable: ${totals?.gap || 'no queues'}`;
        else detail = `${QueueHealth.formatCount(totals.expected)} expected · ${totals.fileProgress >= 1 ? 'extracted' : `${this._percent(totals.fileProgress)} read`}`;
        const head = this._el('div', { cls: 'rp-entity-head' }, this._el('b', { text: this._entityLabel(entity) }), this._el('span', { cls: 'mx-dim', text: ` · ${detail}` }));
        return this._el('div', { cls: 'rp-entity', attrs: { 'data-run-entity': entity } }, head, ...cells.map(cell => this._renderRunRow(cell, running)));
    }

    _renderRunRow(cell, running) {
        const critical = cell.health.state === 'crit';
        const row = this._el('div', { cls: `rp-row${critical ? ' rp-crit' : ''}`, attrs: { 'data-run-cell': `${cell.consumer}-${cell.entity}`, 'data-state': critical ? 'crit' : (cell.gap ? 'unknown' : 'ok') } });
        const name = this._el('span', { cls: 'rp-name', text: cell.consumerLabel });
        if (cell.gap) {
            row.append(name, this._el('span', { cls: 'rp-gap mx-dim', text: 'progress unavailable' }));
            row.setAttribute('aria-label', `${cell.entityLabel} to ${cell.consumerLabel}: progress unavailable, ${cell.gap}`);
            return row;
        }
        const loaded = cell.loaded / cell.expected;
        const read = cell.fileProgress ?? 0;
        const bar = this._el('span', { cls: 'rp-bar', attrs: { role: 'img', 'aria-label': `${this._percent(loaded)} loaded, ${this._percent(read)} extracted` } },
            this._el('span', { cls: 'rp-read', style: `width:${(100 * Math.min(1, read)).toFixed(1)}%` }),
            this._el('span', { cls: 'rp-loaded', style: `width:${(100 * Math.min(1, loaded)).toFixed(1)}%` }));
        const state = this._el('span', { cls: 'rp-eta' });
        if (critical) state.append(this._el('b', { cls: 'mx-crit-text', text: `◆ ${cell.health.label}` }));
        else if (!running) state.textContent = `${cell.loaded.toLocaleString()} loaded`;
        else {
            const eta = RunProgress.eta(cell);
            state.textContent = eta === null ? 'ETA unknown' : (eta === 0 ? 'done' : `ETA ${QueueHealth.formatDuration(eta)}`);
        }
        if (cell.dlq.state === 'crit' || cell.dlq.state === 'warn') {
            state.append(this._el('span', { cls: `mx-dlq ${cell.dlq.state === 'crit' ? 'mx-dlq-crit' : 'mx-dlq-warn'}`, text: `${cell.dlq.state === 'crit' ? '◆' : '▲'} ${cell.dlq.label}` }));
        }
        row.append(name, bar, this._el('span', { cls: 'rp-pct mono', text: this._percent(loaded) }), state);
        return row;
    }

    // Open the run's row in the Extractions tab.
    async goToRun(runId) {
        const admin = window.adminDashboard;
        admin?.switchTab('extractions');
        let row = document.getElementById(`extraction-run-${runId}`);
        if (!row && admin?.loadExtractions) {
            await admin.loadExtractions();
            row = document.getElementById(`extraction-run-${runId}`);
        }
        if (!row) return;
        document.querySelectorAll('[data-highlight]').forEach(el => el.removeAttribute('data-highlight'));
        row.dataset.highlight = 'true';
        row.scrollIntoView?.({ block: 'center' });
        row.focus();
    }

    // ─── Database cards ───────────────────────────────────────────────────────

    updateDatabases(databases) {
        databases.forEach(db => {
            const key = db.name.toLowerCase();

            if (key === 'neo4j') {
                const badge = document.getElementById('neo4j-status-badge');
                const nodesEl = document.getElementById('neo4j-nodes');
                const relsEl  = document.getElementById('neo4j-relationships');

                if (badge) {
                    badge.textContent = db.status === 'healthy' ? 'Healthy' : 'Unavailable';
                    badge.className = `text-[10px] font-bold uppercase ${db.status === 'healthy' ? 'text-emerald-400' : 'text-red-400'}`;
                }

                if (nodesEl && relsEl) {
                    if (db.size && db.status === 'healthy') {
                        // Backend formats as "X nodes, Y relationships"
                        const m = db.size.match(/^([\d,]+)\s+nodes?,\s+([\d,]+)\s+relationships?/i);
                        if (m) {
                            nodesEl.textContent = m[1];
                            relsEl.textContent  = m[2];
                        } else {
                            nodesEl.textContent = db.size;
                            relsEl.textContent  = '—';
                        }
                    } else {
                        nodesEl.textContent = db.error ? 'Error' : '—';
                        relsEl.textContent  = '—';
                    }
                }

            } else if (key === 'postgresql') {
                const badge  = document.getElementById('postgresql-status-badge');
                const connEl = document.getElementById('postgresql-connections');
                const sizeEl = document.getElementById('postgresql-size');

                if (badge) {
                    badge.textContent = db.status === 'healthy' ? 'Healthy' : 'Unavailable';
                    badge.className = `text-[10px] font-bold uppercase ${db.status === 'healthy' ? 'text-emerald-400' : 'text-red-400'}`;
                }
                if (connEl) connEl.textContent = db.connection_count != null ? db.connection_count.toString() : '—';
                if (sizeEl) sizeEl.textContent = db.size || '—';
            }
        });
    }

    // ─── Activity log ─────────────────────────────────────────────────────────

    // Add one entry, keeping the log newest first by the time the event happened.
    addLogEntry(message, type = 'info', { time = Date.now(), source = 'local' } = {}) {
        const timestamp = new Date(time).toLocaleTimeString('en-US', {
            hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
        this.activityLog.unshift({ time, timestamp, message, type, source });
        this._trimActivityLog();
        this.renderActivityLog();
    }

    _trimActivityLog() {
        this.activityLog.sort((a, b) => b.time - a.time);
        if (this.activityLog.length > this.maxLogEntries) this.activityLog.length = this.maxLogEntries;
    }

    // Replace every entry of one source, then render once.
    _replaceLogSource(source, entries) {
        this.activityLog = this.activityLog.filter(e => e.source !== source);
        for (const { message, type, time } of entries) {
            const timestamp = new Date(time).toLocaleTimeString('en-US', {
                hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
            });
            this.activityLog.push({ time, timestamp, message, type, source });
        }
        this._trimActivityLog();
        this.renderActivityLog();
    }

    // ─── Server events and operator actions ───────────────────────────────────

    static serverEntry(event) {
        const time = Date.parse(event.timestamp);
        return { message: event.message, type: event.level, time: Number.isNaN(time) ? Date.now() : time };
    }

    // The recent history the server kept, sent on every connect: it replaces what this client had.
    applyEventBacklog(events) {
        if (!Array.isArray(events)) return;
        this.lastServerEventId = events.reduce((max, e) => Math.max(max, e.id || 0), 0);
        this._replaceLogSource('server', events.map(Dashboard.serverEntry));
    }

    // One live event; an id the backlog already covered is not shown twice.
    applyServerEvent(event) {
        if (!event || (event.id && event.id <= this.lastServerEventId)) return;
        if (event.id) this.lastServerEventId = event.id;
        const { message, type, time } = Dashboard.serverEntry(event);
        this.addLogEntry(message, type, { time, source: 'server' });
    }

    // Operator actions come from the audit log through the console's own /admin/api proxy with
    // this session's bearer, in the browser: the console holds no service credential for
    // catalog-api, so nothing about another operator's audit view is kept on the server.
    startAuditPolling() {
        this.stopAuditPolling();
        this.fetchAuditEntries();
        this.auditTimer = setInterval(() => this.fetchAuditEntries(), AUDIT_POLL_MS);
    }

    stopAuditPolling() {
        if (this.auditTimer) clearInterval(this.auditTimer);
        this.auditTimer = null;
    }

    static auditMessage(entry) {
        const who = entry.admin_email || 'an operator';
        const target = entry.target ? ` ${entry.target}` : '';
        if (entry.action === 'extraction.trigger') return `${who} triggered an extraction`;
        if (entry.action === 'dlq.purge') return `${who} purged DLQ${target}`;
        return `${who}: ${entry.action}${target}`;
    }

    async fetchAuditEntries() {
        if (!this.active) return;
        const token = this.token;
        try {
            const responses = await Promise.all(AUDIT_ACTIONS.map(action =>
                fetch(`/admin/api/audit-log?action=${encodeURIComponent(action)}&page_size=${AUDIT_PAGE_SIZE}`, { headers: this._authHeaders(), cache: 'no-store' })));
            if (!this.active || this.token !== token) return;
            if (responses.some(r => SESSION_REFUSALS.has(r.status))) {
                this.requestSessionCheck();
                return;
            }
            // A failed fetch leaves the entries already shown; the next poll tries again.
            if (!responses.every(r => r.ok)) return;
            const bodies = await Promise.all(responses.map(r => r.json()));
            if (!this.active || this.token !== token) return;
            const entries = bodies.flatMap(body => body.entries || []).map(entry => ({
                message: Dashboard.auditMessage(entry), type: 'info', time: Date.parse(entry.created_at),
            })).filter(entry => !Number.isNaN(entry.time));
            this._replaceLogSource('audit', entries);
        } catch {
            // Offline or malformed: keep what is shown.
        }
    }

    renderActivityLog() {
        const container = document.getElementById('activityLog');
        if (!container) return;

        const levelCls = { info: 'text-emerald-500', warning: 'text-orange-500', error: 'text-red-500' };
        const levelLbl = { info: '[INFO]',            warning: '[WARN]',          error: '[ERR]'        };

        // A quiet system is news too: say so, rather than collapse the log to nothing.
        if (this.activityLog.length === 0) {
            container.replaceChildren(this._el('p', { cls: 't-dim log-empty', text: 'No events yet' }));
            return;
        }
        container.replaceChildren(
            ...this.activityLog.map(e => {
                const row = document.createElement('div');
                row.className = 'flex items-start space-x-4 log-entry';

                const ts = document.createElement('span');
                ts.className = 't-muted shrink-0';
                ts.textContent = e.timestamp;

                const lv = document.createElement('span');
                lv.className = `${levelCls[e.type] || 'text-emerald-500'} font-bold shrink-0`;
                lv.textContent = levelLbl[e.type] || '[INFO]';

                const msg = document.createElement('span');
                msg.style.color = 'var(--log-msg)';
                msg.textContent = e.message;

                row.append(ts, lv, msg);
                return row;
            })
        );
    }

    // ─── Helpers ──────────────────────────────────────────────────────────────

    updateLastUpdated(timestamp) {
        const el = document.getElementById('lastUpdated');
        if (el) el.textContent = new Date(timestamp).toLocaleString();
    }

    // Kept for backward-compatibility with any external callers
    formatTime(timestamp) {
        return new Date(timestamp).toLocaleString();
    }

    _formatRate(rate) {
        if (rate >= 1000) return `${(rate / 1000).toFixed(1)}k`;
        return rate.toFixed(1);
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.dashboard = new Dashboard();
});
