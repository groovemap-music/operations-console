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
    const TAB_OVERVIEW = 'overview';
    const TAB_STORAGE = 'storage';
    const RANK = { crit: 0, warn: 1 };

    // Queue assessments that raise an item; the cell's own label is the item's text.
    const QUEUE_KINDS = new Set(['no-consumer', 'stalled']);
    const DLQ_KINDS = new Set(['dlq-growing', 'dlq-static']);

    // Items for one pipeline: `cells` are `{ consumer, entity, queue, health, dlq }` from the matrix.
    function pipelineItems({ id, label, queuesUnavailable, services, cells }) {
        const items = [];
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
    function derive({ pipelines, databases, alerts }) {
        const items = [...(pipelines || []).flatMap(pipelineItems), ...datastoreItems(databases), ...alertItems(alerts)];
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

        document.addEventListener('console:authenticated', (event) => this.start(event.detail?.token));
        document.addEventListener('console:signed-out', () => this.stop());

        this.narrowViewport = window.matchMedia?.(`(max-width: ${MATRIX_STACKED_MAX_WIDTH}px)`);
        this.narrowViewport?.addEventListener?.('change', () => this._applyMatrixLayout());

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
        this.fetchLinkBases();
    }

    stop() {
        this.active = false;
        this.token = null;
        // Nothing of the signed-in view outlives the session; the next one fetches it afresh.
        this.topology = null;
        this.pendingData = null;
        this.closeQueueDrawer();
        this._resetHistory();
        this.stopAlertPolling();
        this.stopAuditPolling();
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
        return { pipelines, databases: reading?.databases || [], alerts: this.alerts?.alerts || [] };
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
            const cell = document.getElementById(`${target.pipeline}-${target.consumer}-${target.entity}-cell`);
            if (cell) this.openQueueDrawer(cell);
        } else if (target.type === 'service') {
            document.getElementById(`service-${target.service}`)?.scrollIntoView?.({ block: 'center' });
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
            this.setQueuesUnavailable(pipelineName, pipelineData.queues_available === false);
            this.updateQueues(pipelineName, pipelineData.queues || [], at);
        }

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
                return 'text-[10px] px-2 py-0.5 rounded uppercase font-bold bg-purple-500/10 text-purple-400 border border-purple-500/20';
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
                cls: 'mx-unavailable-banner hidden',
                text: 'Queue metrics unavailable',
                attrs: { 'data-queues-unavailable': pipeline.id, role: 'status' },
            }));

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

        return this._el('section', { id: `pipeline-${pipeline.id}`, cls: 'mx-pipeline hidden' }, head, grid);
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

        const label = [`${this._entityLabel(model.entity)} to ${model.consumer.label}`];
        if (unavailable) label.push('queue metrics unavailable');
        else if (!last) label.push('not reported');
        else {
            label.push(`${last.messages.toLocaleString()} messages queued${trend.direction === 'steady' ? '' : `, ${trend.direction}`}`);
            label.push(this._lagText(lag));
        }
        if (health.state === 'crit') label.push(`critical: ${health.label}`);
        if (dlq.state === 'crit') label.push(`critical: ${dlq.label}`);
        if (dlq.state === 'warn') label.push(`warning: ${dlq.label}`);
        cell.setAttribute('aria-label', label.join('; '));
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
