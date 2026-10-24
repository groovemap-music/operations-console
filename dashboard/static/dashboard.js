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
        const pipelines = data.pipelines || {};

        // Show/hide pipeline sections based on presence in data
        for (const pipelineId of ['discogs', 'musicbrainz']) {
            const section = document.getElementById(`pipeline-${pipelineId}`);
            if (section) {
                section.classList.toggle('hidden', !(pipelineId in pipelines));
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
        const PIPELINE_ENTITY_TYPES = {
            discogs: ['masters', 'releases', 'artists', 'labels'],
            musicbrainz: ['release-groups', 'releases', 'artists', 'labels'],
        };
        const types = PIPELINE_ENTITY_TYPES[pipelineName] || ['artists', 'labels', 'releases'];

        services.forEach(service => {
            const prefix = `${pipelineName}-${service.name}`;
            const badge = document.getElementById(`${prefix}-status-badge`);
            if (badge) {
                badge.className = this._serviceBadgeClasses(service.status);
                badge.textContent = this._statusLabel(service.status);
            }

            // Extractor card – use extraction_progress from health endpoint
            if (service.name.startsWith('extractor') && service.extraction_progress) {
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

    // ─── Queue metrics ────────────────────────────────────────────────────────

    setQueuesUnavailable(pipelineName, unavailable) {
        this.queuesUnavailable[pipelineName] = unavailable;
        document.querySelectorAll(`[data-queues-unavailable="${pipelineName}"]`).forEach(el => {
            el.classList.toggle('hidden', !unavailable);
        });
    }

    updateQueues(pipelineName, queues) {
        const PIPELINE_ENTITY_TYPES = {
            discogs: ['masters', 'releases', 'artists', 'labels'],
            musicbrainz: ['release-groups', 'releases', 'artists', 'labels'],
        };
        const PIPELINE_CONSUMERS = {
            discogs: { graph: 'graphinator', table: 'tableinator' },
            musicbrainz: { graph: 'brainzgraphinator', table: 'brainztableinator' },
        };

        const TYPES = PIPELINE_ENTITY_TYPES[pipelineName] || ['artists', 'labels', 'releases'];
        const CONSUMERS = PIPELINE_CONSUMERS[pipelineName] || PIPELINE_CONSUMERS.discogs;

        // Build per-service maps for both regular and dead-letter queues.
        const graphMap    = {};
        const tableMap    = {};
        const graphDlqMap = {};
        const tableDlqMap = {};

        queues.forEach(queue => {
            const name  = queue.name.toLowerCase();
            const isDlq = name.endsWith('.dlq');

            for (const type of TYPES) {
                if (!name.includes(type)) continue;
                if (name.includes(CONSUMERS.graph)) {
                    if (isDlq) graphDlqMap[type] = queue;
                    else       graphMap[type]    = queue;
                } else if (name.includes(CONSUMERS.table)) {
                    if (isDlq) tableDlqMap[type] = queue;
                    else       tableMap[type]    = queue;
                }
                break;
            }
        });

        this.currentMaps[pipelineName] = { graphMap, tableMap, graphDlqMap, tableDlqMap, CONSUMERS, TYPES };

        const isDlq         = document.getElementById('dlq-toggle')?.checked ?? false;
        const activeGraphMap = isDlq ? graphDlqMap : graphMap;
        const activeTableMap = isDlq ? tableDlqMap : tableMap;

        // Graph consumer card – total messages in active queue set.
        TYPES.forEach(type => {
            const el = document.getElementById(`${pipelineName}-${CONSUMERS.graph}-${type}-count`);
            if (!el) return;
            const q = activeGraphMap[type];
            el.textContent = q ? q.messages.toLocaleString() : '—';
        });

        // Table consumer card – messages in active queue set.
        TYPES.forEach(type => {
            const el = document.getElementById(`${pipelineName}-${CONSUMERS.table}-${type}-count`);
            if (!el) return;
            const q = activeTableMap[type];
            el.textContent = q ? q.messages.toLocaleString() : '—';
        });

        this._updateBarChart(pipelineName, activeGraphMap, activeTableMap, TYPES, isDlq, CONSUMERS);
        this._updateRateCircles(pipelineName, graphMap, tableMap, TYPES, isDlq, CONSUMERS);

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

    _onDlqToggle() {
        if (!this.currentMaps) return;
        const isDlq = document.getElementById('dlq-toggle')?.checked ?? false;

        for (const [pipelineName, maps] of Object.entries(this.currentMaps)) {
            const { graphMap, tableMap, graphDlqMap, tableDlqMap, CONSUMERS, TYPES } = maps;
            const activeGraphMap = isDlq ? graphDlqMap : graphMap;
            const activeTableMap = isDlq ? tableDlqMap : tableMap;

            TYPES.forEach(type => {
                const gEl = document.getElementById(`${pipelineName}-${CONSUMERS.graph}-${type}-count`);
                if (gEl) {
                    const q = activeGraphMap[type];
                    gEl.textContent = q ? q.messages.toLocaleString() : '—';
                }
                const tEl = document.getElementById(`${pipelineName}-${CONSUMERS.table}-${type}-count`);
                if (tEl) {
                    const q = activeTableMap[type];
                    tEl.textContent = q ? q.messages.toLocaleString() : '—';
                }
            });

            this._updateBarChart(pipelineName, activeGraphMap, activeTableMap, TYPES, isDlq, CONSUMERS);
            this._updateRateCircles(pipelineName, graphMap, tableMap, TYPES, isDlq, CONSUMERS);
        }
    }

    // ─── Bar chart (CSS height bars) ─────────────────────────────────────────

    _updateBarChart(pipelineName, graphMap, tableMap, types, isDlq = false, consumers = null) {
        const graphLegend = document.getElementById(`${pipelineName}-chart-legend-${consumers?.graph || 'graphinator'}`);
        const tableLegend = document.getElementById(`${pipelineName}-chart-legend-${consumers?.table || 'tableinator'}`);
        const graphName = consumers?.graph || 'graphinator';
        const tableName = consumers?.table || 'tableinator';
        const graphLabel = graphName.charAt(0).toUpperCase() + graphName.slice(1);
        const tableLabel = tableName.charAt(0).toUpperCase() + tableName.slice(1);
        if (graphLegend) graphLegend.textContent = isDlq ? `${graphLabel} DLQ` : graphLabel;
        if (tableLegend) tableLegend.textContent = isDlq ? `${tableLabel} DLQ` : tableLabel;

        const allGraph = types.map(t => graphMap[t]?.messages || 0);
        const allTable = types.map(t => tableMap[t]?.messages || 0);
        const maxCount = Math.max(...allGraph, ...allTable, 1);

        // Update Y-axis labels
        const fmt = this._formatCount.bind(this);
        const y4 = document.getElementById(`${pipelineName}-bar-yaxis-4`);
        const y3 = document.getElementById(`${pipelineName}-bar-yaxis-3`);
        const y2 = document.getElementById(`${pipelineName}-bar-yaxis-2`);
        const y1 = document.getElementById(`${pipelineName}-bar-yaxis-1`);
        if (y4) y4.textContent = fmt(maxCount);
        if (y3) y3.textContent = fmt(maxCount * 0.75);
        if (y2) y2.textContent = fmt(maxCount * 0.5);
        if (y1) y1.textContent = fmt(maxCount * 0.25);

        // Update bar heights: purple = graph consumer messages, blue = table consumer messages
        types.forEach(type => {
            const gq = graphMap[type];
            const tq = tableMap[type];
            const msgBar   = document.getElementById(`${pipelineName}-bar-${type}-messages`);
            const readyBar = document.getElementById(`${pipelineName}-bar-${type}-ready`);
            if (msgBar) {
                const count = gq?.messages || 0;
                const pct = Math.max((count / maxCount) * 100, count > 0 ? 1 : 0);
                msgBar.style.height = `${pct}%`;
            }
            if (readyBar) {
                const count = tq?.messages || 0;
                const pct = Math.max((count / maxCount) * 100, count > 0 ? 1 : 0);
                readyBar.style.height = `${pct}%`;
            }
        });
    }

    // ─── Circular rate gauges ─────────────────────────────────────────────────

    _updateRateCircles(pipelineName, graphMap, tableMap, types, isDlq = false, consumers = null) {
        const C = this.CIRCUMFERENCE;
        const graphName = consumers?.graph || 'graphinator';
        const tableName = consumers?.table || 'tableinator';

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

        types.forEach(type => {
            const gq = graphMap[type];
            const tq = tableMap[type];
            updateGauge(`${pipelineName}-rate-circle-${graphName}-${type}-publish`, `${pipelineName}-rate-text-${graphName}-${type}-publish`, gq?.message_rate ?? null, isDlq || unavailable);
            updateGauge(`${pipelineName}-rate-circle-${tableName}-${type}-publish`, `${pipelineName}-rate-text-${tableName}-${type}-publish`, tq?.message_rate ?? null, isDlq || unavailable);
            updateGauge(`${pipelineName}-rate-circle-${graphName}-${type}-ack`,     `${pipelineName}-rate-text-${graphName}-${type}-ack`,     gq?.ack_rate     ?? null, isDlq || unavailable);
            updateGauge(`${pipelineName}-rate-circle-${tableName}-${type}-ack`,     `${pipelineName}-rate-text-${tableName}-${type}-ack`,     tq?.ack_rate     ?? null, isDlq || unavailable);
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
