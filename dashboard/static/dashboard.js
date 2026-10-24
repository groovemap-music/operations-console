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

// Series colours for a pipeline's consumers, in topology order, and the icon for the datastore a
// consumer writes to. A consumer or datastore beyond these still renders, with the fallback.
const CONSUMER_COLORS = ['var(--purple-accent)', 'var(--blue-accent)', 'var(--accent-green)', 'var(--accent-yellow)'];
const DATASTORE_ICONS = { neo4j: 'hub', postgresql: 'grid_view', default: 'database' };

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
        this.activityLog = [];
        this.maxLogEntries = 50;
        // SVG circle circumference: 2 * PI * r(28) ≈ 176
        this.CIRCUMFERENCE = 176;

        // The pipeline layout from /api/topology; the Overview draws nothing until it arrives.
        this.topology = null;
        this.topologyRequest = null;
        this.pendingData = null;
        this.gaugeStats = {};
        this.currentMaps = {};
        // Pipelines whose last broker scrape failed; their queue data is unavailable, not empty.
        this.queuesUnavailable = {};

        document.addEventListener('console:authenticated', (event) => this.start(event.detail?.token));
        document.addEventListener('console:signed-out', () => this.stop());

        const dlqToggle = document.getElementById('dlq-toggle');
        if (dlqToggle) {
            dlqToggle.addEventListener('change', () => this._onDlqToggle());
        }
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
        this.fetchInitialData();
        this.initializeWebSocket();
    }

    stop() {
        this.active = false;
        this.token = null;
        // Nothing of the signed-in view outlives the session; the next one fetches it afresh.
        this.topology = null;
        this.pendingData = null;
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
            this.addLogEntry('Connected to dashboard', 'info');
        };

        socket.onmessage = (event) => {
            // Only a socket that delivers data resets the backoff: a refused socket opens too,
            // and is closed straight after.
            this.reconnectDelay = RECONNECT_BASE_MS;
            try {
                const message = JSON.parse(event.data);
                if (message.type === 'metrics_update') {
                    this.updateDashboard(message.data);
                }
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
            if (!this.active) return;
            this.ws = null;
            if (event.code === WS_CLOSE_UNAUTHENTICATED) {
                this.addLogEntry('Live feed closed: the session is no longer valid', 'warning');
                this.requestSessionCheck();
                return;
            }
            if (event.code === WS_CLOSE_UNAVAILABLE) {
                this.addLogEntry('Live feed paused: catalog-api could not confirm the session', 'warning');
            } else {
                this.addLogEntry('Disconnected from dashboard', 'warning');
            }
            this.reconnect();
        };
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

    updateOverallStatus(services) {
        const dot = document.getElementById('operational-status-dot');
        const text = document.getElementById('operational-status-text');
        if (!dot || !text) return;

        const allHealthy = services.length > 0 && services.every(s => s.status === 'healthy');
        const anyUnhealthy = services.some(s => s.status === 'unhealthy');

        const allUnhealthy = services.length > 0 && services.every(s => s.status === 'unhealthy');

        if (allHealthy) {
            dot.className = 'w-3 h-3 rounded-full bg-emerald-500 animate-pulse';
            text.className = 'text-[10px] text-emerald-500 font-bold uppercase tracking-widest';
            text.textContent = 'Operational';
        } else if (allUnhealthy) {
            dot.className = 'w-3 h-3 rounded-full bg-red-500';
            text.className = 'text-[10px] text-red-500 font-bold uppercase tracking-widest';
            text.textContent = 'Offline';
        } else if (anyUnhealthy) {
            dot.className = 'w-3 h-3 rounded-full bg-yellow-500';
            text.className = 'text-[10px] text-yellow-500 font-bold uppercase tracking-widest';
            text.textContent = 'Degraded';
        } else {
            dot.className = 'w-3 h-3 rounded-full bg-yellow-500 animate-pulse';
            text.className = 'text-[10px] text-yellow-500 font-bold uppercase tracking-widest';
            text.textContent = 'Unknown';
        }
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
        for (const [pipelineName, pipelineData] of Object.entries(pipelines)) {
            this.updateServices(pipelineName, pipelineData.services || []);
            this.setQueuesUnavailable(pipelineName, pipelineData.queues_available === false);
            this.updateQueues(pipelineName, pipelineData.queues || []);
        }

        this.updateDatabases(data.databases || []);
        this.updateLastUpdated(data.timestamp);

        // Aggregate all services for overall status
        const allServices = Object.values(pipelines).flatMap(p => p.services || []);
        this.updateOverallStatus(allServices);
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

            // Producer card – use extraction_progress from health endpoint
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
                        el.className = 'text-blue-400';
                    } else {
                        el.textContent = 'Idle';
                        el.className = 'text-emerald-400';
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

    _consumerColor(index) {
        return CONSUMER_COLORS[index % CONSUMER_COLORS.length];
    }

    _entityLabel(entity) {
        return entity.split('-').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
    }

    // Build every pipeline section from the topology. Sections start hidden; updateDashboard
    // reveals the ones the live metrics report. Element ids are derived from names, so a new
    // consumer or entity type needs no markup.
    renderTopology(topology) {
        this.topology = topology;
        this.gaugeStats = {};
        this.currentMaps = {};
        const container = document.getElementById('pipelines');
        if (!container) return;
        container.replaceChildren(...topology.pipelines.map(pipeline => this._renderPipeline(pipeline, topology)));
        const dataStores = new Map(topology.datastores.map(store => [store.id, store.label]));
        container.querySelectorAll('[data-datastore]').forEach(el => {
            el.textContent = `→ ${dataStores.get(el.dataset.datastore) || el.dataset.datastore}`;
        });
    }

    _renderPipeline(pipeline) {
        const unavailable = () => this._el('span', {
            cls: 'hidden text-[10px] font-bold uppercase tracking-widest',
            style: 'color:var(--amber-accent,#f59e0b)',
            text: 'Queue metrics unavailable',
            attrs: { 'data-queues-unavailable': pipeline.id, role: 'status' },
        });
        const heading = (text) => this._el('div', { cls: 'flex items-center gap-3' },
            this._el('h2', { cls: 'text-sm font-bold uppercase tracking-[0.15em] t-dim', text }),
            this._el('div', { cls: 'flex-1 border-t b-theme' }));

        const cards = this._el('div', { cls: 'grid gap-6', style: `grid-template-columns:repeat(${1 + pipeline.consumers.length},minmax(0,1fr))` },
            this._renderProducerCard(pipeline),
            ...pipeline.consumers.map((consumer, index) => this._renderConsumerCard(pipeline, consumer, index)));

        return this._el('section', { id: `pipeline-${pipeline.id}`, cls: 'space-y-8 hidden' },
            heading(`${pipeline.label} Pipeline`),
            cards,
            this._renderQueueChart(pipeline, unavailable),
            this._el('div', { cls: 'grid grid-cols-2 gap-6' },
                this._renderRatePanel(pipeline, 'publish', 'Publish Rates (msg/s)', unavailable),
                this._renderRatePanel(pipeline, 'ack', 'Ack Rates (msg/s)', unavailable)));
    }

    _cardHeader(icon, iconStyle, label, badgeId) {
        return this._el('div', { cls: 'flex items-center justify-between mb-6 border-b b-theme pb-4' },
            this._el('h3', { cls: 'text-sm font-semibold flex items-center gap-2' },
                this._el('span', { cls: `material-symbols-outlined text-sm${iconStyle ? '' : ' t-dim'}`, text: icon, style: iconStyle }),
                ` ${label}`),
            this._el('span', {
                id: badgeId,
                cls: 'text-[10px] bg-yellow-500/10 text-yellow-400 px-2 py-0.5 rounded border border-yellow-500/20 uppercase font-bold',
                text: 'Unknown',
            }));
    }

    _entityRow(entity, last, valueId, valueCls) {
        return this._el('div', { cls: `flex justify-between text-xs mono py-1${last ? '' : ' border-b b-row'}` },
            this._el('span', { text: this._entityLabel(entity) }),
            this._el('span', { id: valueId, cls: valueCls, text: '—' }));
    }

    _renderProducerCard(pipeline) {
        const { producer, entity_types: types } = pipeline;
        const prefix = `${pipeline.id}-${producer.name}`;
        return this._el('div', { id: `service-${producer.name}`, cls: 'dashboard-card p-6' },
            this._cardHeader('output', null, producer.label, `${prefix}-status-badge`),
            this._el('div', { cls: 'space-y-3' },
                this._el('div', { cls: 'flex justify-between text-[10px] t-muted uppercase font-bold tracking-wider' },
                    this._el('span', { text: 'Queues' }), this._el('span', { text: 'State' })),
                ...types.map((type, i) => this._entityRow(type, i === types.length - 1, `${prefix}-${type}-state`, 't-muted')),
                this._el('div', { cls: 'flex justify-between text-[10px] t-muted uppercase font-bold tracking-wider pt-3 mt-1 border-t b-theme' },
                    this._el('span', { text: 'Total Records' }),
                    this._el('span', { id: `${prefix}-total-records`, cls: 't-high normal-case font-mono', text: '—' }))));
    }

    _renderConsumerCard(pipeline, consumer, index) {
        const prefix = `${pipeline.id}-${consumer.name}`;
        const types = pipeline.entity_types;
        const icon = DATASTORE_ICONS[consumer.datastore] || DATASTORE_ICONS.default;
        return this._el('div', { id: `service-${consumer.name}`, cls: 'dashboard-card p-6' },
            this._cardHeader(icon, `color:${this._consumerColor(index)}`, consumer.label, `${prefix}-status-badge`),
            this._el('div', { cls: 'space-y-3' },
                this._el('div', { cls: 'flex justify-between text-[10px] t-muted uppercase font-bold tracking-wider' },
                    this._el('span', { text: 'Queues' }), this._el('span', { text: 'Messages' })),
                ...types.map((type, i) => this._entityRow(type, i === types.length - 1, `${prefix}-${type}-count`, 't-high')),
                this._el('div', { cls: 'text-[10px] t-muted uppercase font-bold tracking-wider pt-3 mt-1 border-t b-theme', attrs: { 'data-datastore': consumer.datastore } })));
    }

    _renderQueueChart(pipeline, unavailable) {
        const types = pipeline.entity_types;
        const legend = this._el('div', { cls: 'flex items-center space-x-8' },
            ...pipeline.consumers.map((consumer, index) => this._el('div', { cls: 'flex items-center space-x-2' },
                this._el('div', { cls: 'w-3 h-3 rounded-sm', style: `background:${this._consumerColor(index)}` }),
                this._el('span', { id: `${pipeline.id}-chart-legend-${consumer.name}`, cls: 'text-[10px] font-bold t-dim uppercase tracking-widest', text: consumer.label }))));
        const yAxis = [4, 3, 2, 1].map(step => this._el('div', { id: `${pipeline.id}-bar-yaxis-${step}`, cls: 'border-t b-theme w-full pt-1', text: '—' }));
        const groups = types.map(type => this._el('div', { cls: 'flex flex-col items-center gap-4 h-full' },
            this._el('div', { cls: 'flex items-end gap-2 flex-1' },
                ...pipeline.consumers.map((consumer, index) => this._el('div', {
                    id: `${pipeline.id}-bar-${type}-${consumer.name}`,
                    cls: 'w-10 rounded-t-sm transition-all duration-500',
                    style: `background:${this._consumerColor(index)};height:0%`,
                }))),
            this._el('span', { cls: 'text-[10px] font-bold t-dim uppercase tracking-widest', text: this._entityLabel(type) })));

        return this._el('div', { cls: 'dashboard-card p-8' },
            this._el('div', { cls: 'flex items-center justify-between mb-10' },
                this._el('div', {},
                    this._el('h2', { cls: 'text-lg font-bold', text: 'Queue Size Metrics' }),
                    this._el('p', { cls: 'text-[10px] t-muted uppercase tracking-[0.2em] font-mono mt-1', text: 'Message Count by Queue Type' }),
                    unavailable()),
                legend),
            this._el('div', { cls: 'h-64 flex items-end justify-between px-12 relative' },
                this._el('div', { cls: 'absolute left-0 top-0 bottom-0 w-full flex flex-col justify-between pointer-events-none text-[9px] font-mono t-muted' },
                    ...yAxis, this._el('div', { cls: 'border-t b-theme w-full' })),
                this._el('div', { cls: 'flex-1 flex justify-around items-end z-10 h-full' }, ...groups)));
    }

    _renderGauge(pipelineId, consumer, type, kind, color) {
        const idSuffix = `${consumer}-${type}-${kind}`;
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'w-full h-full -rotate-90');
        const circle = (attrs) => {
            const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            for (const [name, value] of Object.entries({ cx: 32, cy: 32, fill: 'transparent', r: 28, 'stroke-width': 4, ...attrs })) c.setAttribute(name, value);
            return c;
        };
        const progress = circle({
            id: `${pipelineId}-rate-circle-${idSuffix}`, stroke: color, 'stroke-dasharray': 176, 'stroke-dashoffset': 176, 'stroke-linecap': 'round',
        });
        progress.style.transition = 'stroke-dashoffset 0.5s ease';
        svg.append(circle({ stroke: 'var(--gauge-track)' }), progress);
        return this._el('div', { cls: 'flex flex-col items-center space-y-2' },
            this._el('div', { cls: 'relative w-16 h-16' }, svg,
                this._el('div', { cls: 'absolute inset-0 flex items-center justify-center text-[10px] font-bold mono' },
                    this._el('span', { id: `${pipelineId}-rate-text-${idSuffix}`, cls: 't-muted', text: '0' }))),
            this._el('span', { cls: 'text-[9px] t-muted font-bold uppercase', text: this._entityLabel(type) }));
    }

    _renderRatePanel(pipeline, kind, title, unavailable) {
        const types = pipeline.entity_types;
        return this._el('div', { cls: 'dashboard-card p-8' },
            this._el('div', { cls: 'flex items-center justify-between mb-6' },
                this._el('h2', { cls: 'text-xs font-bold t-dim uppercase tracking-[0.2em]', text: title }),
                unavailable(),
                this._el('div', { cls: 'flex items-center space-x-6' },
                    ...pipeline.consumers.map((consumer, index) => this._el('div', { cls: 'flex items-center space-x-2' },
                        this._el('div', { cls: 'w-2.5 h-2.5 rounded-full', style: `background:${this._consumerColor(index)}` }),
                        this._el('span', { cls: 'text-[9px] font-bold t-muted uppercase', text: consumer.label }))))),
            ...pipeline.consumers.map((consumer, index) => this._el('div', { cls: `flex items-center gap-4${index < pipeline.consumers.length - 1 ? ' mb-6' : ''}` },
                this._el('span', {
                    cls: 'text-[9px] font-bold uppercase tracking-wider w-28 shrink-0 truncate',
                    style: `color:${this._consumerColor(index)}`,
                    text: consumer.label,
                    attrs: { title: consumer.label },
                }),
                this._el('div', { cls: 'grid gap-4 flex-1', style: `grid-template-columns:repeat(${types.length},minmax(0,1fr))` },
                    ...types.map(type => this._renderGauge(pipeline.id, consumer.name, type, kind, this._consumerColor(index)))))));
    }

    // ─── Queue metrics ────────────────────────────────────────────────────────

    setQueuesUnavailable(pipelineName, unavailable) {
        this.queuesUnavailable[pipelineName] = unavailable;
        document.querySelectorAll(`[data-queues-unavailable="${pipelineName}"]`).forEach(el => {
            el.classList.toggle('hidden', !unavailable);
        });
    }

    // Sort the live queues into the topology's slots by exact name: `queues[consumer][entity]`
    // for the work queues and `dlqs[consumer][entity]` for their dead-letter queues.
    updateQueues(pipelineName, queues) {
        const pipeline = this._pipeline(pipelineName);
        if (!pipeline) return;

        const slots = new Map();
        const maps = { queues: {}, dlqs: {} };
        for (const consumer of pipeline.consumers) {
            maps.queues[consumer.name] = {};
            maps.dlqs[consumer.name] = {};
            for (const queue of consumer.queues) {
                slots.set(queue.name, [maps.queues[consumer.name], queue.entity]);
                slots.set(queue.dead_letter_queue, [maps.dlqs[consumer.name], queue.entity]);
            }
        }
        queues.forEach(queue => {
            const slot = slots.get(queue.name);
            if (slot) slot[0][slot[1]] = queue;
        });
        this.currentMaps[pipelineName] = maps;

        this._renderQueueValues(pipeline);

        // Log high message counts (regular queues only)
        queues.forEach(queue => {
            if (!queue.name.toLowerCase().endsWith('.dlq') && queue.messages > 1000) {
                this.addLogEntry(
                    `High message count in ${queue.name}: ${queue.messages.toLocaleString()}`,
                    'warning'
                );
            }
        });
    }

    _renderQueueValues(pipeline) {
        const maps = this.currentMaps[pipeline.id];
        if (!maps) return;
        const isDlq = document.getElementById('dlq-toggle')?.checked ?? false;
        const active = isDlq ? maps.dlqs : maps.queues;

        // Consumer cards – messages in the active queue set.
        pipeline.consumers.forEach(consumer => {
            pipeline.entity_types.forEach(type => {
                const el = document.getElementById(`${pipeline.id}-${consumer.name}-${type}-count`);
                if (!el) return;
                const q = active[consumer.name][type];
                el.textContent = q ? q.messages.toLocaleString() : '—';
            });
        });

        this._updateBarChart(pipeline, active, isDlq);
        this._updateRateCircles(pipeline, maps.queues, isDlq);
    }

    _onDlqToggle() {
        if (!this.currentMaps || !this.topology) return;
        for (const pipelineName of Object.keys(this.currentMaps)) {
            const pipeline = this._pipeline(pipelineName);
            if (pipeline) this._renderQueueValues(pipeline);
        }
    }

    // ─── Bar chart (CSS height bars) ─────────────────────────────────────────

    _updateBarChart(pipeline, active, isDlq = false) {
        pipeline.consumers.forEach(consumer => {
            const legend = document.getElementById(`${pipeline.id}-chart-legend-${consumer.name}`);
            if (legend) legend.textContent = isDlq ? `${consumer.label} DLQ` : consumer.label;
        });

        const counts = pipeline.consumers.flatMap(c => pipeline.entity_types.map(t => active[c.name][t]?.messages || 0));
        const maxCount = Math.max(...counts, 1);

        // Update Y-axis labels
        const fmt = this._formatCount.bind(this);
        [[4, 1], [3, 0.75], [2, 0.5], [1, 0.25]].forEach(([step, share]) => {
            const label = document.getElementById(`${pipeline.id}-bar-yaxis-${step}`);
            if (label) label.textContent = fmt(maxCount * share);
        });

        // One bar per consumer and entity type, in the consumer's colour.
        pipeline.consumers.forEach(consumer => {
            pipeline.entity_types.forEach(type => {
                const bar = document.getElementById(`${pipeline.id}-bar-${type}-${consumer.name}`);
                if (!bar) return;
                const count = active[consumer.name][type]?.messages || 0;
                const pct = Math.max((count / maxCount) * 100, count > 0 ? 1 : 0);
                bar.style.height = `${pct}%`;
            });
        });
    }

    // ─── Circular rate gauges ─────────────────────────────────────────────────

    _updateRateCircles(pipeline, queueMaps, isDlq = false) {
        const C = this.CIRCUMFERENCE;
        const pipelineName = pipeline.id;

        const unavailable = this.queuesUnavailable[pipelineName] === true;

        const updateGauge = (circleId, textId, rate, disabled) => {
            const circle = document.getElementById(circleId);
            const text   = document.getElementById(textId);
            if (!circle || !text) return;

            if (disabled) {
                circle.style.strokeDashoffset = C;
                text.textContent = 'N/A';
                text.className = 't-muted';
                text.removeAttribute('aria-label');
                text.removeAttribute('title');
                // Hide min–max stats when disabled
                const container = text.closest('.flex.flex-col');
                const statsEl = container?.querySelector('.gauge-stats');
                if (statsEl) statsEl.textContent = '';
                return;
            }

            // A null rate is "not computable this interval", which is not a zero: show a dash.
            if (rate === null || rate === undefined) {
                circle.style.strokeDashoffset = C;
                text.textContent = '—';
                text.className = 't-muted';
                text.setAttribute('aria-label', 'rate unknown');
                text.setAttribute('title', 'Rate unknown');
                return;
            }
            text.removeAttribute('aria-label');
            text.removeAttribute('title');

            // Track per-gauge min (non-zero) and max
            const s = this.gaugeStats[circleId] ??= { min: Infinity, max: 0 };
            if (rate > 0) s.min = Math.min(s.min, rate);
            s.max = Math.max(s.max, rate);

            // Fill relative to this gauge's own observed max
            const fill = s.max > 0 ? rate / s.max : 0;
            circle.style.strokeDashoffset = fill > 0 ? C - fill * C : C;
            text.textContent = this._formatRate(rate);
            text.className = rate > 0 ? '' : 't-muted';

            // Show min–max below the gauge label
            const container = text.closest('.flex.flex-col');
            if (container) {
                let statsEl = container.querySelector('.gauge-stats');
                if (!statsEl) {
                    statsEl = document.createElement('span');
                    statsEl.className = 'gauge-stats text-[8px] t-muted font-mono';
                    container.appendChild(statsEl);
                }
                const minStr = s.min === Infinity ? '—' : this._formatRate(s.min);
                statsEl.textContent = `${minStr}–${this._formatRate(s.max)}`;
            }
        };

        pipeline.consumers.forEach(consumer => {
            pipeline.entity_types.forEach(type => {
                const q = queueMaps[consumer.name][type];
                const base = `${consumer.name}-${type}`;
                updateGauge(`${pipelineName}-rate-circle-${base}-publish`, `${pipelineName}-rate-text-${base}-publish`, q?.message_rate ?? null, isDlq || unavailable);
                updateGauge(`${pipelineName}-rate-circle-${base}-ack`,     `${pipelineName}-rate-text-${base}-ack`,     q?.ack_rate     ?? null, isDlq || unavailable);
            });
        });
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

    addLogEntry(message, type = 'info') {
        const ts = new Date().toLocaleTimeString('en-US', {
            hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
        this.activityLog.unshift({ timestamp: ts, message, type });
        if (this.activityLog.length > this.maxLogEntries) this.activityLog.pop();
        this.renderActivityLog();
    }

    renderActivityLog() {
        const container = document.getElementById('activityLog');
        if (!container) return;

        const levelCls = { info: 'text-emerald-500', warning: 'text-orange-500', error: 'text-red-500' };
        const levelLbl = { info: '[INFO]',            warning: '[WARN]',          error: '[ERR]'        };

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

    _formatCount(count) {
        if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
        if (count >= 1_000)     return `${(count / 1_000).toFixed(0)}K`;
        return Math.round(count).toString();
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.dashboard = new Dashboard();
});
