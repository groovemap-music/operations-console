'use strict';

// DLQ naming contract MUST mirror the backend's _VALID_DLQ_NAMES construction
// (api/routers/admin.py): f"{source-exchange-prefix}-{consumer}-{data_type}.dlq".
// Keep these prefixes/types in lockstep with common/config.py's
// DISCOGS_EXCHANGE_PREFIX / MUSICBRAINZ_EXCHANGE_PREFIX / DATA_TYPES /
// MUSICBRAINZ_DATA_TYPES defaults — a drift here reproduces the "every Purge
// button 404s" regression.
// DEFAULTS ONLY. The live values are fetched from /api/queue-prefixes (which serves
// the dashboard's env-derived DISCOGS_EXCHANGE_PREFIX / MUSICBRAINZ_EXCHANGE_PREFIX)
// before the DLQ list is rendered. Under a prefix override, compiled-in literals would
// make every Purge button 404 against the backend's env-derived _VALID_DLQ_NAMES —
// the same failure shape as the queue-prefix regression.
const DLQ_SOURCE_PREFIXES = {
    discogs: 'groovemap-discogs',
    musicbrainz: 'groovemap-musicbrainz',
};

const DLQ_CONSUMER_GROUPS = [
    { source: 'discogs', consumer: 'graphinator', types: ['artists', 'labels', 'masters', 'releases'] },
    { source: 'discogs', consumer: 'tableinator', types: ['artists', 'labels', 'masters', 'releases'] },
    { source: 'musicbrainz', consumer: 'brainzgraphinator', types: ['artists', 'labels', 'release-groups', 'releases'] },
    { source: 'musicbrainz', consumer: 'brainztableinator', types: ['artists', 'labels', 'release-groups', 'releases'] },
];

// Structured items (queue/service/type) generated directly — NOT parsed back
// out of the queue name string, which is brittle against multi-segment
// prefixes (e.g. "groovemap-discogs-graphinator-artists.dlq").
function buildDlqItems(prefixes) {
    return DLQ_CONSUMER_GROUPS.flatMap(({ source, consumer, types }) =>
        types.map(type => ({
            queue: `${prefixes[source]}-${consumer}-${type}.dlq`,
            service: consumer,
            type,
        }))
    );
}

let DLQ_ITEMS = buildDlqItems(DLQ_SOURCE_PREFIXES);

let DLQ_NAMES = DLQ_ITEMS.map(item => item.queue);

// The admin bearer lives in an HttpOnly session cookie the console sets at sign-in, so no
// script here can read it; the browser attaches it to every console request by itself. Every
// request also carries this header, without which the console refuses a state-changing one, so
// a cross-site page cannot ride the cookie.
const CONSOLE_REQUEST_HEADERS = { 'X-GrooveMap-Console': '1' };

// Older consoles kept the bearer in localStorage. It is signed out and removed on load.
const LEGACY_TOKEN_KEY = 'admin_token';
const LEGACY_EMAIL_KEY = 'admin_email';
// Set when a sign-out never reached the console, which then could not clear the cookie; the
// next load signs out again before resuming anything.
const SIGNOUT_PENDING_KEY = 'console_signout_pending';

// Replace the compiled-in defaults with the server's env-derived prefixes. Best effort:
// on any failure the defaults stand, which is exactly the pre-existing behavior. The route
// is session-gated like every console data route.
async function loadQueuePrefixes() {
    try {
        const response = await fetch('/api/queue-prefixes', { headers: CONSOLE_REQUEST_HEADERS });
        if (!response.ok) return;
        const prefixes = await response.json();
        if (!prefixes || !prefixes.discogs || !prefixes.musicbrainz) return;
        DLQ_ITEMS = buildDlqItems(prefixes);
        DLQ_NAMES = DLQ_ITEMS.map(item => item.queue);
    } catch {
        // Keep the defaults — a missing endpoint must not break the admin panel.
    }
}

// The console shell re-validates its session this often, and once more just after the
// session's own expiry, so an expired, revoked, or unverifiable session returns to the login
// form instead of leaving a stale console on screen.
const SESSION_CHECK_INTERVAL_MS = 15000;

// Why the login form is showing, keyed by the `reason` the console's session gate returns
// (plus the two outcomes only the browser can observe).
const SESSION_END_MESSAGES = {
    'signed-out': 'You signed out.',
    'signed-out-unconfirmed': 'You are signed out of this browser, but catalog-api did not confirm the sign-out, so the session may still be valid until it expires.',
    missing: 'Your session expired or was revoked. Sign in again.',
    invalid: 'Your session expired or was revoked. Sign in again.',
    unavailable: 'catalog-api is unreachable, so your session could not be verified. Sign in again once it is back.',
    'console-unreachable': 'The operations console could not be reached. Sign in again once it is back.',
};

// Reasons after which the session is over: the console has cleared its cookie (or is retried
// until it does). After an outage the session may still be live, so the cookie is kept and a
// reload resumes the session once the dependency is back.
const SESSION_DISCARD_REASONS = new Set(['signed-out', 'signed-out-unconfirmed', 'missing', 'invalid']);

// One time range, shared by every history view, persisted across reloads.
const RANGE_STORAGE_KEY = 'console_range';
const HISTORY_RANGES = ['1h', '6h', '24h', '7d', '30d', '90d'];
const HISTORY_TABS = ['queue-trends', 'system-health'];

// Chart.js paints to a canvas, which cannot read CSS variables, so the colours are the active
// theme's tokens (index.html) resolved when a chart is built, and again on every theme change.
// Series order: brass (oxblood in the light theme), sage (forest), then the warm series colours,
// each at least 15 CIEDE2000 from every Strata state and entity colour and from the others.
const SERIES_COUNT = 9;

function themeVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function seriesColor(index) {
    return themeVar(`--series-${(index % SERIES_COUNT) + 1}`);
}

// Axis grid lines and tick labels: the border and muted-text tokens.
function chartAxisColors() {
    return { grid: themeVar('--chart-grid'), tick: themeVar('--chart-tick') };
}

// Identity Ledger (design ADR 0016 section 7, concept D), in the order the density strip stacks
// event types. The strip also counts `minted`, which the works list cannot filter on.
const LEDGER_EVENT_LABELS = {
    minted: 'Minted',
    linked_at_ingest: 'Linked at ingest',
    evidence_attached: 'Evidence attached',
    alias_closed: 'Alias closed',
    reattached: 'Re-attached',
    promoted: 'Promoted',
    reverted: 'Reverted',
    link_added: 'Link added',
    link_changed: 'Link changed',
    link_removed: 'Link removed',
};

// The CSS variable each event type is drawn in: `minted` is the neutral muted tone, the rest take
// the series palette, so no event colour is a Strata state colour (see ledgerEventColor).
const LEDGER_EVENT_COLORS = {
    minted: '--text-muted-aa',
    linked_at_ingest: '--series-2',
    evidence_attached: '--series-1',
    alias_closed: '--series-7',
    reattached: '--series-5',
    promoted: '--series-4',
    reverted: '--series-3',
    link_added: '--series-6',
    link_changed: '--series-8',
    link_removed: '--series-9',
};

function ledgerEventColor(type) {
    return themeVar(LEDGER_EVENT_COLORS[type] || '--text-dim');
}

// Identity states drawn by the `.ledger-badge[data-state]` rules in index.html, which take the
// --groovemap-state-identity-* tokens; a state the console does not know is shown as unknown.
const LEDGER_STATES = new Set(['not_recorded', 'discogs_only', 'musicbrainz_only', 'unresolved', 'split', 'linked', 'superseded']);

// `dated_from` of a link change the one-time backfill dated from the MusicBrainz full export.
// About 9% of those dates belong to an older link MusicBrainz replaced, so such an event is
// always labelled as dated, never shown as recorded.
const LEDGER_BACKFILL = 'musicbrainz_fullexport';
const LEDGER_BACKFILL_LABEL = 'dated from the MusicBrainz full export';
const LEDGER_PAGE_SIZE = 50;
// What catalog-api answers (503, with Retry-After) from the works list and the density strip
// until the ledger's first refresh. It is not an error: nothing is wrong, the views are being built.
const LEDGER_NOT_REFRESHED = 'identity ledger not yet refreshed';
const LEDGER_RETRY_DEFAULT_MS = 60000;
const LEDGER_RETRY_MIN_MS = 1000;
const LEDGER_RETRY_MAX_MS = 600000;

// `YYYY-MM-DD HH:MM UTC` for an RFC 3339 instant, independent of the browser's locale and zone.
function _ledgerTime(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    const utc = date.toISOString();
    return `${utc.slice(0, 10)} ${utc.slice(11, 16)} UTC`;
}

// "Ledger as of <refreshed_at>", or that it has never been refreshed: refreshed_at is null on a
// single work read before the first refresh.
function _ledgerFreshness(refreshedAt) {
    return refreshedAt ? `Ledger as of ${_ledgerTime(refreshedAt)}` : 'Ledger not yet refreshed';
}

// How long to wait before asking again: a Retry-After in seconds or as an HTTP date, kept within
// bounds so a missing or absurd header neither hammers catalog-api nor parks the tab for good.
function _ledgerRetryDelay(retryAfter) {
    let delay = LEDGER_RETRY_DEFAULT_MS;
    if (retryAfter) {
        const seconds = Number(retryAfter);
        if (Number.isFinite(seconds)) delay = seconds * 1000;
        else if (!Number.isNaN(Date.parse(retryAfter))) delay = Date.parse(retryAfter) - Date.now();
    }
    return Math.min(Math.max(delay, LEDGER_RETRY_MIN_MS), LEDGER_RETRY_MAX_MS);
}

// The RFC 3339 instant that starts the UTC day a date input names, `days` later.
function _ledgerDayStart(value, days = 0) {
    const [year, month, day] = value.split('-').map(Number);
    return new Date(Date.UTC(year, month - 1, day + days)).toISOString().replace('.000Z', 'Z');
}

// A datetime-local input's value read as UTC, as an RFC 3339 instant; empty means now.
function _ledgerAsOf(value) {
    if (!value) return null;
    return value.length === 16 ? `${value}:00Z` : `${value}Z`;
}

// The strip's series: every UTC month from the first bucket to the last, empty months included
// so the axis is time, and one dataset per event type and provenance.
function _ledgerDensitySeries(buckets) {
    const counted = (buckets || []).filter(bucket => /^\d{4}-\d{2}$/.test(bucket.month));
    if (counted.length === 0) return { months: [], datasets: [], backfilledMonths: [] };
    const sorted = counted.map(bucket => bucket.month).sort();
    const last = sorted[sorted.length - 1];
    let [year, month] = sorted[0].split('-').map(Number);
    const months = [];
    for (;;) {
        const label = `${year}-${String(month).padStart(2, '0')}`;
        months.push(label);
        if (label >= last) break;
        month += 1;
        if (month > 12) { month = 1; year += 1; }
    }
    const index = new Map(months.map((label, i) => [label, i]));
    const series = new Map();
    for (const bucket of counted) {
        const key = `${bucket.type}|${bucket.dated_from}`;
        if (!series.has(key)) series.set(key, { type: bucket.type, datedFrom: bucket.dated_from, counts: months.map(() => 0) });
        series.get(key).counts[index.get(bucket.month)] += Number(bucket.events) || 0;
    }
    const order = Object.keys(LEDGER_EVENT_LABELS);
    const rank = type => (order.includes(type) ? order.indexOf(type) : order.length);
    const datasets = [...series.values()].sort((a, b) =>
        rank(a.type) - rank(b.type)
        || a.type.localeCompare(b.type)
        || Number(a.datedFrom === LEDGER_BACKFILL) - Number(b.datedFrom === LEDGER_BACKFILL));
    const backfilledMonths = months.filter((_, i) => datasets.some(d => d.datedFrom === LEDGER_BACKFILL && d.counts[i] > 0));
    return { months, datasets, backfilledMonths };
}

// A diagonal hatch in `color` for backfilled segments, or a translucent fill without a 2D canvas.
function _ledgerHatch(color) {
    const tile = document.createElement('canvas');
    tile.width = 6;
    tile.height = 6;
    const context = tile.getContext('2d');
    if (!context) return `${color}66`;
    context.fillStyle = `${color}33`;
    context.fillRect(0, 0, 6, 6);
    context.strokeStyle = color;
    context.lineWidth = 1.5;
    context.beginPath();
    context.moveTo(0, 6);
    context.lineTo(6, 0);
    context.moveTo(-1, 1);
    context.lineTo(1, -1);
    context.moveTo(5, 7);
    context.lineTo(7, 5);
    context.stroke();
    return context.createPattern(tile, 'repeat') || `${color}66`;
}

function _ledgerEventLabel(type) {
    return LEDGER_EVENT_LABELS[type] || type;
}

function _el(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
}

function _esc(str) {
    const div = document.createElement('div');
    div.textContent = String(str ?? '');
    return div.innerHTML;
}

function _shortQueueName(name) {
    return name.replace(/^groovemap-/, '');
}

function _emptyRow(colSpan, message) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = colSpan;
    td.className = 'py-3 text-center t-muted';
    td.textContent = message;
    tr.appendChild(td);
    return tr;
}

class AdminDashboardState {
    constructor() {
        // Bumped whenever a session starts or ends, so an answer about an earlier one is ignored.
        this.sessionGeneration = 1;
        this.activeTab = 'overview';
        this.signedIn = false;
        // True while logout awaits the console: the revocation makes catalog-api refuse reads
        // still in flight, and those refusals are not the session ending on its own.
        this.signingOut = false;
        this.sessionWatch = null;
        this.sessionExpiryTimer = null;
        this.queueDepthChart = null;
        this.responseTimeChart = null;
        this.auditLogPage = 1;
        this.ledgerLoaded = false;
        this.ledgerPage = 1;
        this.ledgerChart = null;
        this.ledgerWorkId = null;
        // Per read ('density', 'works'): the retry timer, and whether catalog-api said it is still being built.
        this.ledgerRetryTimers = {};
        this.ledgerBuilding = {};
        this.eaVersions = [];
        this.eaSelectedRecords = new Map();
        this.eaParsingErrors = null;
        this.eaVdRule = '';
        this.eaVdEntityType = '';
        this.eaVdPage = 1;
        this.toastHideTimer = null;
        this.toastRemoveTimer = null;
    }
}

class RefreshSubscription {
    constructor() {
        this.handle = null;
    }

    start(callback) {
        this.stop();
        this.handle = setInterval(callback, 60000);
    }

    stop() {
        if (this.handle) clearInterval(this.handle);
        this.handle = null;
    }
}

class AdminInteractions {
    constructor() {
        this.state = new AdminDashboardState();
        this.refreshSubscription = new RefreshSubscription();
        this.bindEvents();
        this.start();
    }

    get signingOut() { return this.state.signingOut; }
    set signingOut(value) { this.state.signingOut = value; }
    get sessionGeneration() { return this.state.sessionGeneration; }
    set sessionGeneration(value) { this.state.sessionGeneration = value; }
    get refreshInterval() { return this.refreshSubscription.handle; }
    set refreshInterval(value) { this.refreshSubscription.handle = value; }
    get activeTab() { return this.state.activeTab; }
    set activeTab(value) { this.state.activeTab = value; }
    get queueDepthChart() { return this.state.queueDepthChart; }
    set queueDepthChart(value) { this.state.queueDepthChart = value; }
    get responseTimeChart() { return this.state.responseTimeChart; }
    set responseTimeChart(value) { this.state.responseTimeChart = value; }
    get _auditLogPage() { return this.state.auditLogPage; }
    set _auditLogPage(value) { this.state.auditLogPage = value; }
    get _eaVersions() { return this.state.eaVersions; }
    set _eaVersions(value) { this.state.eaVersions = value; }
    get _eaSelectedRecords() { return this.state.eaSelectedRecords; }
    set _eaSelectedRecords(value) { this.state.eaSelectedRecords = value; }
    get _eaParsingErrors() { return this.state.eaParsingErrors; }
    set _eaParsingErrors(value) { this.state.eaParsingErrors = value; }
    get _eaVdRule() { return this.state.eaVdRule; }
    set _eaVdRule(value) { this.state.eaVdRule = value; }
    get _eaVdEntityType() { return this.state.eaVdEntityType; }
    set _eaVdEntityType(value) { this.state.eaVdEntityType = value; }
    get _eaVdPage() { return this.state.eaVdPage; }
    set _eaVdPage(value) { this.state.eaVdPage = value; }
    get _toastHideTimer() { return this.state.toastHideTimer; }
    set _toastHideTimer(value) { this.state.toastHideTimer = value; }
    get _toastRemoveTimer() { return this.state.toastRemoveTimer; }
    set _toastRemoveTimer(value) { this.state.toastRemoveTimer = value; }

    // ─── Event binding ───────────────────────────────────────────────────────

    bindEvents() {
        // The live Overview asks for a session check when its ticket or socket is refused.
        document.addEventListener('console:session-check', () => this.onSessionCheckRequested());

        const loginBtn = document.getElementById('login-btn');
        if (loginBtn) {
            loginBtn.addEventListener('click', () => this.login());
        }

        const passwordInput = document.getElementById('login-password');
        if (passwordInput) {
            passwordInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') this.login();
            });
        }

        const emailInput = document.getElementById('login-email');
        if (emailInput) {
            emailInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') this.login();
            });
        }

        const logoutBtn = document.getElementById('logout-btn');
        if (logoutBtn) {
            logoutBtn.addEventListener('click', () => this.logout());
        }

        const triggerDiscogsBtn = document.getElementById('trigger-discogs-btn');
        if (triggerDiscogsBtn) {
            triggerDiscogsBtn.addEventListener('click', () => this.triggerExtraction('discogs'));
        }

        const triggerMusicbrainzBtn = document.getElementById('trigger-musicbrainz-btn');
        if (triggerMusicbrainzBtn) {
            triggerMusicbrainzBtn.addEventListener('click', () => this.triggerExtraction('musicbrainz'));
        }

        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => this.switchTab(btn.dataset.tab));
        });

        document.querySelectorAll('.collapsible-header').forEach(header => {
            header.addEventListener('click', () => {
                const targetId = header.dataset.target;
                const body = document.getElementById(targetId);
                const chevron = header.querySelector('.collapsible-chevron');
                if (!body) return;
                body.classList.toggle('collapsed');
                if (chevron) {
                    chevron.textContent = body.classList.contains('collapsed') ? 'chevron_right' : 'expand_more';
                }
            });
        });

        const usersRefreshBtn = document.getElementById('users-refresh-btn');
        if (usersRefreshBtn) {
            usersRefreshBtn.addEventListener('click', () => {
                this.fetchUserStats();
                this.fetchSyncActivity();
            });
        }

        const storageRefreshBtn = document.getElementById('storage-refresh-btn');
        if (storageRefreshBtn) {
            storageRefreshBtn.addEventListener('click', () => this.fetchStorage());
        }

        const qtRefreshBtn = document.getElementById('qt-refresh-btn');
        if (qtRefreshBtn) {
            qtRefreshBtn.addEventListener('click', () => this.fetchQueueHistory(this._getRange()));
        }

        const shRefreshBtn = document.getElementById('sh-refresh-btn');
        if (shRefreshBtn) {
            shRefreshBtn.addEventListener('click', () => this.fetchHealthHistory(this._getRange()));
        }

        const alRefreshBtn = document.getElementById('al-refresh-btn');
        if (alRefreshBtn) alRefreshBtn.addEventListener('click', () => this.fetchAuditLog());

        const alActionFilter = document.getElementById('al-action-filter');
        if (alActionFilter) alActionFilter.addEventListener('change', () => { this._auditLogPage = 1; this.fetchAuditLog(); });

        const alPrevBtn = document.getElementById('al-prev-btn');
        if (alPrevBtn) alPrevBtn.addEventListener('click', () => { if (this._auditLogPage > 1) { this._auditLogPage--; this.fetchAuditLog(); } });

        const alNextBtn = document.getElementById('al-next-btn');
        if (alNextBtn) alNextBtn.addEventListener('click', () => { this._auditLogPage++; this.fetchAuditLog(); });

        const ledgerKind = document.getElementById('ledger-kind');
        if (ledgerKind) ledgerKind.addEventListener('change', () => this.fetchLedgerDensity());

        ['ledger-event-filter', 'ledger-since', 'ledger-until'].forEach(id => {
            const filter = document.getElementById(id);
            if (filter) filter.addEventListener('change', () => { this.state.ledgerPage = 1; this.fetchLedgerWorks(); });
        });

        const ledgerRefreshBtn = document.getElementById('ledger-refresh-btn');
        if (ledgerRefreshBtn) ledgerRefreshBtn.addEventListener('click', () => this.loadLedger());

        const ledgerPrevBtn = document.getElementById('ledger-prev-btn');
        if (ledgerPrevBtn) ledgerPrevBtn.addEventListener('click', () => { if (this.state.ledgerPage > 1) { this.state.ledgerPage--; this.fetchLedgerWorks(); } });

        const ledgerNextBtn = document.getElementById('ledger-next-btn');
        if (ledgerNextBtn) ledgerNextBtn.addEventListener('click', () => { this.state.ledgerPage++; this.fetchLedgerWorks(); });

        const ledgerAsOfBtn = document.getElementById('ledger-as-of-btn');
        if (ledgerAsOfBtn) ledgerAsOfBtn.addEventListener('click', () => this.fetchLedgerWork());

        const ledgerNowBtn = document.getElementById('ledger-now-btn');
        if (ledgerNowBtn) ledgerNowBtn.addEventListener('click', () => {
            const asOf = document.getElementById('ledger-as-of');
            if (asOf) asOf.value = '';
            this.fetchLedgerWork();
        });

        const ledgerWorkClose = document.getElementById('ledger-work-close');
        if (ledgerWorkClose) ledgerWorkClose.addEventListener('click', () => {
            this.state.ledgerWorkId = null;
            const section = document.getElementById('ledger-work');
            if (section) section.style.display = 'none';
        });

        // One time range for every history view; the buttons live in the shared range bar.
        this._syncRangeButtons();
        document.querySelectorAll('.range-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                localStorage.setItem(RANGE_STORAGE_KEY, btn.dataset.range);
                this._syncRangeButtons();
                this._refreshHistory();
            });
        });

        document.querySelectorAll('.ea-view-btn').forEach(btn => {
            btn.addEventListener('click', () => this._eaSwitchView(btn.dataset.eaView));
        });

        const eaVersionSelect = document.getElementById('ea-version-select');
        if (eaVersionSelect) {
            eaVersionSelect.addEventListener('change', () => this._eaLoadReport());
        }

        const eaRefreshBtn = document.getElementById('ea-refresh-btn');
        if (eaRefreshBtn) eaRefreshBtn.addEventListener('click', () => this._eaLoadReport());

        const eaSelectAll = document.getElementById('ea-select-all');
        if (eaSelectAll) eaSelectAll.addEventListener('change', (e) => this._eaToggleSelectAll(e.target.checked));

        const eaCompareBtn = document.getElementById('ea-compare-btn');
        if (eaCompareBtn) eaCompareBtn.addEventListener('click', () => this._eaCompare());

        const eaGenerateBtn = document.getElementById('ea-generate-btn');
        if (eaGenerateBtn) eaGenerateBtn.addEventListener('click', () => this._eaGeneratePrompt());

        const eaAiGenerateBtn = document.getElementById('ea-ai-generate-btn');
        if (eaAiGenerateBtn) eaAiGenerateBtn.addEventListener('click', () => this._eaGenerateAiPrompt());

        const eaCopyBtn = document.getElementById('ea-copy-btn');
        if (eaCopyBtn) eaCopyBtn.addEventListener('click', () => this._eaCopyPrompt());

        const eaModalClose = document.getElementById('ea-modal-close');
        if (eaModalClose) eaModalClose.addEventListener('click', () => {
            const modal = document.getElementById('ea-modal');
            if (modal) modal.style.display = 'none';
        });

        const eaVdClose = document.getElementById('ea-vd-close');
        if (eaVdClose) eaVdClose.addEventListener('click', () => {
            const section = document.getElementById('ea-violations-detail');
            if (section) section.style.display = 'none';
        });
        const eaVdPrev = document.getElementById('ea-vd-prev');
        if (eaVdPrev) eaVdPrev.addEventListener('click', () => {
            if (this._eaVdPage > 1) { this._eaVdPage--; this._eaLoadViolationsPage(); }
        });
        const eaVdNext = document.getElementById('ea-vd-next');
        if (eaVdNext) eaVdNext.addEventListener('click', () => {
            this._eaVdPage++; this._eaLoadViolationsPage();
        });
        const eaVdPageSize = document.getElementById('ea-vd-page-size');
        if (eaVdPageSize) eaVdPageSize.addEventListener('change', () => {
            this._eaVdPage = 1; this._eaLoadViolationsPage();
        });
    }

    // ─── Tab switching ───────────────────────────────────────────────────────

    switchTab(tabName) {
        this.activeTab = tabName;

        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });

        const panels = ['overview', 'extractions', 'dlq', 'users', 'storage', 'queue-trends', 'system-health', 'audit-log', 'extraction-analysis', 'ledger'];
        panels.forEach(name => {
            const el = document.getElementById(`tab-${name}`);
            if (el) el.style.display = name === tabName ? 'block' : 'none';
        });

        const rangeBar = document.getElementById('console-range-bar');
        if (rangeBar) rangeBar.style.display = HISTORY_TABS.includes(tabName) ? 'flex' : 'none';

        if (tabName === 'users') {
            this.fetchUserStats();
            this.fetchSyncActivity();
        } else if (tabName === 'storage') {
            this.fetchStorage();
        } else if (HISTORY_TABS.includes(tabName)) {
            this._refreshHistory();
        } else if (tabName === 'audit-log') {
            this.fetchAuditLog();
        } else if (tabName === 'extraction-analysis') {
            this.fetchExtractionAnalysisVersions();
        } else if (tabName === 'ledger') {
            this.showLedger();
        }
    }

    // ─── View switching ──────────────────────────────────────────────────────

    showLogin(reason = null) {
        const wasSignedIn = this.signedIn;
        this.signedIn = false;
        this.stopSessionWatch();
        this.stopAutoRefresh();
        this.stopLedgerRetries();
        const consoleView = document.getElementById('console-view');
        const loginView = document.getElementById('login-view');
        if (consoleView) consoleView.style.display = 'none';
        if (loginView) loginView.style.display = 'flex';

        const reasonEl = document.getElementById('login-reason');
        if (reasonEl) {
            const message = reason ? (SESSION_END_MESSAGES[reason] || SESSION_END_MESSAGES.invalid) : '';
            reasonEl.textContent = message;
            reasonEl.dataset.reason = reason || '';
            reasonEl.style.display = message ? 'block' : 'none';
        }
        if (wasSignedIn) {
            document.dispatchEvent(new CustomEvent('console:signed-out'));
        }
    }

    showPanel(session) {
        this.signedIn = true;
        this.state.ledgerLoaded = false;
        document.getElementById('login-view').style.display = 'none';
        document.getElementById('console-view').style.display = 'block';
        this.switchTab('overview');
        // Refresh the prefixes first so the rendered DLQ names match the backend's
        // env-derived _VALID_DLQ_NAMES; re-render once they land.
        loadQueuePrefixes().then(() => this.renderDlqList());
        this.renderDlqList();
        this.loadExtractions();
        this.startAutoRefresh();
        this.startSessionWatch(session);

        const email = session?.email;
        const emailEl = document.getElementById('admin-email');
        if (emailEl && email) {
            emailEl.textContent = email;
        }
        document.dispatchEvent(new CustomEvent('console:authenticated', { detail: { session: this.sessionGeneration } }));
    }

    // ─── Session ─────────────────────────────────────────────────────────────

    // The page cannot see whether it holds a session cookie, so every load asks the console.
    // A bearer an older console left in localStorage is retired first, and a sign-out that
    // never reached the console is finished before anything is resumed.
    async start() {
        await this.retireLegacyBearer();
        if (localStorage.getItem(SIGNOUT_PENDING_KEY)) {
            await this.logout();
            return;
        }
        await this.resumeSession(true);
    }

    // Remove the bearer an older console kept in script-readable storage, and sign it out at
    // catalog-api so a copy taken from there is dead too. Best effort: it is gone from this
    // browser either way. The explicit header leaves any cookie session alone.
    async retireLegacyBearer() {
        const legacy = localStorage.getItem(LEGACY_TOKEN_KEY);
        localStorage.removeItem(LEGACY_TOKEN_KEY);
        localStorage.removeItem(LEGACY_EMAIL_KEY);
        if (!legacy) return;
        try {
            await fetch('/admin/api/logout', { method: 'POST', headers: { Authorization: `Bearer ${legacy}` } });
        } catch {
            // Unreachable now; catalog-api still ends the session at its own expiry.
        }
    }

    // Ask the console whether the session cookie still carries a live admin session. The
    // console answers from catalog-api (cached for at most 30 seconds); a 401 means none,
    // expired, or revoked (and clears the cookie), a 503 means catalog-api could not answer.
    async checkSession() {
        try {
            const response = await fetch('/api/session', { headers: CONSOLE_REQUEST_HEADERS, cache: 'no-store' });
            if (response.ok) return { ok: true, session: await response.json() };
            const body = await response.json().catch(() => ({}));
            return { ok: false, reason: body.reason || (response.status === 503 ? 'unavailable' : 'invalid') };
        } catch {
            return { ok: false, reason: 'console-unreachable' };
        }
    }

    // On first load no session at all is not an ending: the login form shows without a reason.
    async resumeSession(initial = false) {
        const generation = this.sessionGeneration;
        const result = await this.checkSession();
        if (generation !== this.sessionGeneration) return;
        if (result.ok) {
            this.showPanel(result.session);
        } else if (initial && result.reason === 'missing') {
            this.showLogin();
        } else {
            this.endSession(result.reason);
        }
    }

    // An answer may end only the session it was asked about, and never one that is already
    // ending or being signed out by the user.
    ownsSession(generation) {
        return generation === this.sessionGeneration && this.signedIn && !this.signingOut;
    }

    // Mid-session, no cookie at all means the session ended: the browser dropped it at its
    // expiry, or the console cleared it when catalog-api called it invalid.
    async verifySession() {
        const generation = this.sessionGeneration;
        const result = await this.checkSession();
        if (!this.ownsSession(generation)) return result;
        if (!result.ok) this.endSession(result.reason === 'missing' ? 'invalid' : result.reason);
        return result;
    }

    async onSessionCheckRequested() {
        if (!this.signedIn) return;
        const result = await this.verifySession();
        if (result.ok && this.signedIn) {
            document.dispatchEvent(new CustomEvent('console:authenticated', { detail: { session: this.sessionGeneration } }));
        }
    }

    endSession(reason) {
        if (SESSION_DISCARD_REASONS.has(reason)) {
            this.sessionGeneration += 1;
        }
        this.showLogin(reason);
    }

    startSessionWatch(session) {
        this.stopSessionWatch();
        this.sessionWatch = setInterval(() => this.verifySession(), SESSION_CHECK_INTERVAL_MS);
        const expiresAt = Date.parse(session?.expires_at ?? '');
        if (Number.isFinite(expiresAt)) {
            // One second past expiry, so the check lands after catalog-api stops honouring it.
            const delay = Math.max(expiresAt - Date.now(), 0) + 1000;
            if (delay <= 2147483647) {
                this.sessionExpiryTimer = setTimeout(() => this.verifySession(), delay);
            }
        }
    }

    stopSessionWatch() {
        if (this.sessionWatch) clearInterval(this.sessionWatch);
        if (this.sessionExpiryTimer) clearTimeout(this.sessionExpiryTimer);
        this.sessionWatch = null;
        this.sessionExpiryTimer = null;
    }

    // ─── Auth ────────────────────────────────────────────────────────────────

    async login() {
        const email = document.getElementById('login-email').value.trim();
        const password = document.getElementById('login-password').value;
        const errorEl = document.getElementById('login-error');
        const loginBtn = document.getElementById('login-btn');

        if (!email || !password) {
            errorEl.textContent = 'Email and password are required';
            errorEl.style.display = 'block';
            return;
        }

        loginBtn.disabled = true;
        errorEl.style.display = 'none';

        try {
            const response = await fetch('/admin/api/login', {
                method: 'POST',
                headers: { ...CONSOLE_REQUEST_HEADERS, 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password }),
            });

            if (response.ok) {
                // The console set the session cookie; the body carries no bearer.
                this.sessionGeneration += 1;
                localStorage.removeItem(SIGNOUT_PENDING_KEY);
                document.getElementById('login-password').value = '';
                await this.resumeSession();
            } else {
                const err = await response.json().catch(() => ({}));
                errorEl.textContent = err.detail || 'Invalid credentials';
                errorEl.style.display = 'block';
            }
        } catch {
            errorEl.textContent = 'Connection error. Please try again.';
            errorEl.style.display = 'block';
        } finally {
            loginBtn.disabled = false;
        }
    }

    // Only a 2xx (revoked) or a 401 (already dead) proves the session can no longer be used;
    // anything else, including no answer, is reported as unconfirmed. Any answer from the
    // console clears the cookie; with none, the sign-out is retried on the next load.
    async logout() {
        let status = null;
        this.signingOut = true;
        try {
            const response = await fetch('/admin/api/logout', { method: 'POST', headers: CONSOLE_REQUEST_HEADERS });
            status = response.status;
        } catch {
            status = null;
        } finally {
            this.signingOut = false;
        }
        if (status === null) {
            localStorage.setItem(SIGNOUT_PENDING_KEY, '1');
        } else {
            localStorage.removeItem(SIGNOUT_PENDING_KEY);
        }
        const confirmed = status !== null && ((status >= 200 && status < 300) || status === 401);
        this.endSession(confirmed ? 'signed-out' : 'signed-out-unconfirmed');
    }

    // A 401 ends the session it was asked for, never a later one, and never overrides a
    // sign-out that already ended it.
    async authFetch(url, options = {}) {
        const generation = this.sessionGeneration;
        const headers = { ...CONSOLE_REQUEST_HEADERS, ...options.headers };
        const response = await fetch(url, { ...options, headers });
        if (response.status === 401) {
            if (this.ownsSession(generation)) this.endSession('invalid');
            throw new Error('Session expired');
        }
        return response;
    }
}

class AdminExtractions extends AdminInteractions {

    async loadExtractions() {
        try {
            const response = await this.authFetch('/admin/api/extractions');
            if (!response.ok) return;

            const data = await response.json();
            const extractions = data.extractions || [];
            const historyBody = document.getElementById('history-body');
            const emptyMsg = document.getElementById('history-empty');

            if (extractions.length === 0) {
                historyBody.replaceChildren();
                emptyMsg.style.display = 'block';
                return;
            }

            emptyMsg.style.display = 'none';

            const rows = extractions.map(e => {
                const tr = document.createElement('tr');
                tr.className = 'border-b b-row';
                // The Overview's Run progress view links here, to the run's own row.
                tr.id = `extraction-run-${e.id}`;
                tr.tabIndex = -1;

                const tdDate = document.createElement('td');
                tdDate.className = 'py-2.5 pr-4 mono t-mid';
                tdDate.textContent = e.started_at ? new Date(e.started_at).toLocaleString() : '\u2014';

                const tdStatus = document.createElement('td');
                tdStatus.className = 'py-2.5 pr-4';
                const badge = document.createElement('span');
                badge.className = `text-[10px] px-2 py-0.5 rounded uppercase font-bold ${this._statusBadgeClass(e.status)}`;
                badge.textContent = e.status;
                tdStatus.appendChild(badge);

                // A run's source is shown as recorded; null (older runs) is never guessed.
                const tdSource = document.createElement('td');
                tdSource.className = 'py-2.5 pr-4 mono t-mid';
                tdSource.textContent = e.source || 'not recorded';

                const tdDuration = document.createElement('td');
                tdDuration.className = 'py-2.5 pr-4 mono t-mid';
                tdDuration.textContent = this._formatRunDuration(e.duration_seconds);

                const tdRecords = document.createElement('td');
                tdRecords.className = 'py-2.5 pr-4 mono t-mid text-right';
                const records = this._runRecordTotal(e.record_counts);
                tdRecords.textContent = records !== null ? records.toLocaleString() : '\u2014';
                if (e.record_counts) {
                    tdRecords.title = Object.entries(e.record_counts).filter(([k]) => k !== 'total')
                        .map(([k, v]) => `${k}: ${Number(v).toLocaleString()}`).join(', ');
                }

                const tdError = document.createElement('td');
                tdError.className = 'py-2.5 mono t-muted text-xs truncate max-w-[200px]';
                tdError.textContent = e.error_message || '\u2014';
                if (e.error_message) tdError.title = e.error_message;

                tr.append(tdDate, tdSource, tdStatus, tdDuration, tdRecords, tdError);
                return tr;
            });

            historyBody.replaceChildren(...rows);

            const latest = extractions[0];
            if (latest) {
                const statusEl = document.getElementById('extraction-status');
                if (statusEl) {
                    statusEl.className = `text-[10px] px-2 py-0.5 rounded uppercase font-bold ${this._statusBadgeClass(latest.status)}`;
                    statusEl.textContent = latest.status;
                }
            }
        } catch {
            // Silently fail — will retry on next auto-refresh
        }
    }

    // catalog-api's duration_seconds as "3h12m" (null while the run has not finished).
    _formatRunDuration(seconds) {
        if (seconds === null || seconds === undefined) return '\u2014';
        if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
        if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
        const h = Math.floor(seconds / 3600);
        const m = Math.round((seconds - h * 3600) / 60);
        return m ? `${h}h${String(m).padStart(2, '0')}m` : `${h}h`;
    }

    // The extractor's final per-entity counts carry their own total; older records may not.
    _runRecordTotal(counts) {
        if (!counts || typeof counts !== 'object') return null;
        if (counts.total != null) return Number(counts.total);
        return Object.values(counts).reduce((sum, n) => sum + (Number(n) || 0), 0);
    }

    async triggerExtraction(source = 'discogs') {
        const btnId = source === 'musicbrainz' ? 'trigger-musicbrainz-btn' : 'trigger-discogs-btn';
        const triggerBtn = document.getElementById(btnId);
        const spinner = document.getElementById('trigger-spinner');

        triggerBtn.disabled = true;
        spinner.style.display = 'inline-block';

        const endpoint = source === 'musicbrainz'
            ? '/admin/api/extractions/trigger-musicbrainz'
            : '/admin/api/extractions/trigger';

        try {
            const response = await this.authFetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source }),
            });

            if (response.ok) {
                const label = source === 'musicbrainz' ? 'MusicBrainz' : 'Discogs';
                this.showToast(`${label} extraction triggered successfully`, 'success');
                await this.loadExtractions();
            } else {
                const err = await response.json().catch(() => ({}));
                this.showToast(err.detail || `Failed to trigger ${source} extraction`, 'error');
            }
        } catch {
            this.showToast('Connection error', 'error');
        } finally {
            triggerBtn.disabled = false;
            spinner.style.display = 'none';
        }
    }
}

class AdminQueues extends AdminExtractions {

    renderDlqList() {
        const container = document.getElementById('dlq-list');
        if (!container) return;

        const items = DLQ_ITEMS.map(({ queue, service, type }) => {
            const row = document.createElement('div');
            row.className = 'flex items-center justify-between py-3.5 border-b b-row';

            const left = document.createElement('div');
            left.className = 'flex items-center gap-3';

            const icon = document.createElement('span');
            icon.className = 'material-symbols-outlined text-sm t-muted';
            icon.textContent = 'mail';

            const name = document.createElement('span');
            name.className = 'text-xs mono t-mid';
            name.textContent = queue;

            const serviceBadge = document.createElement('span');
            serviceBadge.className = 'text-[10px] px-1.5 py-0.5 rounded t-muted bg-inner border b-theme uppercase font-bold';
            serviceBadge.textContent = service;

            const typeBadge = document.createElement('span');
            typeBadge.className = 'text-[10px] px-1.5 py-0.5 rounded t-muted bg-inner border b-theme uppercase font-bold';
            typeBadge.textContent = type;

            left.append(icon, name, serviceBadge, typeBadge);

            const purgeBtn = document.createElement('button');
            purgeBtn.className = 'text-[10px] px-3 py-1 rounded font-bold uppercase tracking-wider badge-error hover:opacity-80 transition-opacity flex items-center gap-1';
            purgeBtn.addEventListener('click', () => this.purgeDlq(queue));

            const deleteIcon = document.createElement('span');
            deleteIcon.className = 'material-symbols-outlined';
            deleteIcon.style.fontSize = '12px';
            deleteIcon.textContent = 'delete';

            purgeBtn.append(deleteIcon, document.createTextNode(' Purge'));

            row.append(left, purgeBtn);
            return row;
        });

        container.replaceChildren(...items);
    }

    async purgeDlq(queue) {
        if (!confirm(`Are you sure you want to purge ${queue}?\n\nThis will permanently delete all messages in this dead letter queue.`)) {
            return;
        }

        try {
            const response = await this.authFetch(`/admin/api/dlq/purge/${queue}`, {
                method: 'POST',
            });

            if (response.ok) {
                const data = await response.json().catch(() => ({}));
                const count = data.purged != null ? data.purged : 0;
                this.showToast(`Purged ${count} message(s) from ${queue}`, 'success');
            } else {
                const err = await response.json().catch(() => ({}));
                this.showToast(err.detail || `Failed to purge ${queue}`, 'error');
            }
        } catch {
            this.showToast('Connection error', 'error');
        }
    }
}

class AdminServices extends AdminQueues {

    async fetchUserStats() {
        const loadingEl = document.getElementById('users-loading');
        const errorEl = document.getElementById('users-error');
        const errorMsgEl = document.getElementById('users-error-msg');
        if (loadingEl) loadingEl.style.display = 'inline';
        if (errorEl) errorEl.style.display = 'none';

        try {
            const response = await this.authFetch('/admin/api/users/stats');
            if (!response.ok) {
                const err = await response.json().catch(() => ({}));
                this._showInlineError(errorEl, errorMsgEl, err.detail || `Error ${response.status}`);
                return;
            }

            const data = await response.json();

            this._setText('stat-total-users', data.total_users != null ? Number(data.total_users).toLocaleString() : '—');
            this._setText('stat-active-7d', data.active_7d != null ? Number(data.active_7d).toLocaleString() : '—');
            this._setText('stat-active-30d', data.active_30d != null ? Number(data.active_30d).toLocaleString() : '—');
            this._setText('stat-oauth-rate', data.oauth_rate != null ? `${data.oauth_rate.toFixed(1)}%` : '—');

            const tbody = document.getElementById('registrations-body');
            if (tbody) {
                const rows = data.daily_registrations || [];
                if (rows.length === 0) {
                    tbody.replaceChildren(_emptyRow(3, 'No registration data available'));
                } else {
                    tbody.replaceChildren(...rows.map(row => {
                        const tr = document.createElement('tr');
                        tr.className = 'border-b b-row';

                        const tdDate = document.createElement('td');
                        tdDate.className = 'py-2 px-4 mono t-mid';
                        tdDate.textContent = row.date || '—';

                        const tdNew = document.createElement('td');
                        tdNew.className = 'py-2 px-4 mono t-mid text-right';
                        tdNew.textContent = row.new_users != null ? Number(row.new_users).toLocaleString() : '—';

                        const tdOAuth = document.createElement('td');
                        tdOAuth.className = 'py-2 px-4 mono t-mid text-right';
                        tdOAuth.textContent = row.oauth_users != null ? Number(row.oauth_users).toLocaleString() : '—';

                        tr.append(tdDate, tdNew, tdOAuth);
                        return tr;
                    }));
                }
            }
        } catch {
            this._showInlineError(errorEl, errorMsgEl, 'Failed to load user stats');
        } finally {
            if (loadingEl) loadingEl.style.display = 'none';
        }
    }

    async fetchSyncActivity() {
        try {
            const response = await this.authFetch('/admin/api/users/sync-activity');
            if (!response.ok) return;

            const data = await response.json();
            const w7 = data['7d'] || {};
            const w30 = data['30d'] || {};

            this._setText('stat-syncs-per-day-7d', w7.syncs_per_day != null ? w7.syncs_per_day.toFixed(1) : '—');
            this._setText('stat-syncs-per-day-30d-label', w30.syncs_per_day != null ? `30d: ${w30.syncs_per_day.toFixed(1)}` : '');

            this._setText('stat-avg-items-7d', w7.avg_items_synced != null ? Number(w7.avg_items_synced).toLocaleString() : '—');
            this._setText('stat-avg-items-30d-label', w30.avg_items_synced != null ? `30d: ${Number(w30.avg_items_synced).toLocaleString()}` : '');

            this._setText('stat-failure-rate-7d', w7.failure_rate != null ? `${w7.failure_rate.toFixed(1)}%` : '—');
            this._setText('stat-failure-rate-30d-label', w30.failure_rate != null ? `30d: ${w30.failure_rate.toFixed(1)}%` : '');
        } catch {
            // Silently fail — non-critical secondary fetch
        }
    }

    // ─── Storage Utilization ──────────────────────────────────────────────────

    async fetchStorage() {
        const loadingEl = document.getElementById('storage-loading');
        const errorEl = document.getElementById('storage-error');
        const errorMsgEl = document.getElementById('storage-error-msg');
        if (loadingEl) loadingEl.style.display = 'inline';
        if (errorEl) errorEl.style.display = 'none';

        try {
            const response = await this.authFetch('/admin/api/storage');
            if (!response.ok) {
                const err = await response.json().catch(() => ({}));
                this._showInlineError(errorEl, errorMsgEl, err.detail || `Error ${response.status}`);
                return;
            }

            const data = await response.json();
            this._renderNeo4j(data.neo4j || {});
            this._renderPostgres(data.postgresql || {});
            this._renderValkey(data.valkey || {});
        } catch {
            this._showInlineError(errorEl, errorMsgEl, 'Failed to load storage data');
        } finally {
            if (loadingEl) loadingEl.style.display = 'none';
        }
    }

    _renderNeo4j(neo4j) {
        const badge = document.getElementById('storage-neo4j-status-badge');
        if (badge) {
            const ok = neo4j.status === 'ok';
            badge.className = `text-[10px] px-2 py-0.5 rounded uppercase font-bold ${ok ? 'badge-ok' : 'badge-error'}`;
            badge.textContent = neo4j.status || '—';
        }

        if (neo4j.status !== 'ok') {
            const nodesTbody = document.getElementById('neo4j-nodes-body');
            if (nodesTbody) nodesTbody.replaceChildren(_emptyRow(2, neo4j.status === 'error' ? 'Connection error' : 'No data'));
            const relsTbody = document.getElementById('neo4j-rels-body');
            if (relsTbody) relsTbody.replaceChildren(_emptyRow(2, neo4j.status === 'error' ? 'Connection error' : 'No data'));
            return;
        }

        // Node counts
        const nodesTbody = document.getElementById('neo4j-nodes-body');
        if (nodesTbody) {
            const nodes = neo4j.nodes || neo4j.node_counts || [];
            if (nodes.length === 0) {
                nodesTbody.replaceChildren(_emptyRow(2, 'No data'));
            } else {
                nodesTbody.replaceChildren(...nodes.map(item => {
                    const tr = document.createElement('tr');
                    tr.className = 'border-b b-row';
                    const tdLabel = document.createElement('td');
                    tdLabel.className = 'py-1.5 pr-4 mono t-mid';
                    tdLabel.textContent = item.label || '—';
                    const tdCount = document.createElement('td');
                    tdCount.className = 'py-1.5 mono t-mid text-right';
                    tdCount.textContent = item.count != null ? Number(item.count).toLocaleString() : '—';
                    tr.append(tdLabel, tdCount);
                    return tr;
                }));
            }
        }

        // Relationship counts
        const relsTbody = document.getElementById('neo4j-rels-body');
        if (relsTbody) {
            const rels = neo4j.relationships || neo4j.relationship_counts || [];
            if (rels.length === 0) {
                relsTbody.replaceChildren(_emptyRow(2, 'No data'));
            } else {
                relsTbody.replaceChildren(...rels.map(item => {
                    const tr = document.createElement('tr');
                    tr.className = 'border-b b-row';
                    const tdType = document.createElement('td');
                    tdType.className = 'py-1.5 pr-4 mono t-mid';
                    tdType.textContent = item.type || '—';
                    const tdCount = document.createElement('td');
                    tdCount.className = 'py-1.5 mono t-mid text-right';
                    tdCount.textContent = item.count != null ? Number(item.count).toLocaleString() : '—';
                    tr.append(tdType, tdCount);
                    return tr;
                }));
            }
        }

        // Store sizes summary
        const storeSizesEl = document.getElementById('neo4j-store-sizes');
        if (storeSizesEl) {
            storeSizesEl.replaceChildren();
            const sizes = neo4j.store_sizes || {};
            const entries = Object.entries(sizes);
            if (entries.length > 0) {
                const label = document.createElement('p');
                label.className = 'text-[10px] font-bold uppercase tracking-wider t-muted mb-2';
                label.textContent = 'Store Sizes';
                storeSizesEl.appendChild(label);

                const grid = document.createElement('div');
                grid.className = 'flex flex-wrap gap-3';
                entries.forEach(([key, value]) => {
                    const card = document.createElement('div');
                    card.className = 'stat-card';
                    card.style.flex = '0 1 auto';
                    card.style.minWidth = '140px';
                    const cardLabel = document.createElement('p');
                    cardLabel.className = 'text-[10px] font-bold uppercase tracking-wider t-muted mb-1';
                    cardLabel.textContent = key.replace(/_/g, ' ');
                    const cardValue = document.createElement('p');
                    cardValue.className = 'text-base font-semibold mono t-high';
                    cardValue.textContent = value || '—';
                    card.append(cardLabel, cardValue);
                    grid.appendChild(card);
                });
                storeSizesEl.appendChild(grid);
            }
        }
    }

    _renderPostgres(postgres) {
        const badge = document.getElementById('postgres-status-badge');
        if (badge) {
            const ok = postgres.status === 'ok';
            badge.className = `text-[10px] px-2 py-0.5 rounded uppercase font-bold ${ok ? 'badge-ok' : 'badge-error'}`;
            badge.textContent = postgres.status || '—';
        }

        if (postgres.status !== 'ok') {
            // Clear the "Loading..." placeholder when status is not ok
            const tbody = document.getElementById('postgres-tables-body');
            if (tbody) tbody.replaceChildren(_emptyRow(4, postgres.status === 'error' ? 'Connection error' : 'No data'));
            return;
        }

        const dbSizeEl = document.getElementById('postgres-db-size');
        if (dbSizeEl && postgres.total_size) {
            dbSizeEl.textContent = `Total database size: ${postgres.total_size}`;
        }

        const tbody = document.getElementById('postgres-tables-body');
        if (tbody) {
            const tables = postgres.tables || [];
            if (tables.length === 0) {
                tbody.replaceChildren(_emptyRow(4, 'No data'));
            } else {
                tbody.replaceChildren(...tables.map(t => {
                    const tr = document.createElement('tr');
                    tr.className = 'border-b b-row';

                    const tdName = document.createElement('td');
                    tdName.className = 'py-2 pr-4 mono t-mid';
                    tdName.textContent = t.name || t.table_name || '—';

                    const tdRows = document.createElement('td');
                    tdRows.className = 'py-2 pr-4 mono t-mid text-right';
                    tdRows.textContent = t.row_count != null ? Number(t.row_count).toLocaleString() : '—';

                    const tdData = document.createElement('td');
                    tdData.className = 'py-2 pr-4 mono t-mid text-right';
                    tdData.textContent = t.size || t.data_size || '—';

                    const tdIndex = document.createElement('td');
                    tdIndex.className = 'py-2 mono t-mid text-right';
                    tdIndex.textContent = t.index_size || '—';

                    tr.append(tdName, tdRows, tdData, tdIndex);
                    return tr;
                }));
            }
        }
    }

    _renderValkey(valkey) {
        const badge = document.getElementById('valkey-status-badge');
        if (badge) {
            const ok = valkey.status === 'ok';
            badge.className = `text-[10px] px-2 py-0.5 rounded uppercase font-bold ${ok ? 'badge-ok' : 'badge-error'}`;
            badge.textContent = valkey.status || '—';
        }

        if (valkey.status !== 'ok') return;

        this._setText('valkey-mem-used', valkey.memory_used || '—');
        this._setText('valkey-mem-peak', valkey.memory_peak || '—');
        this._setText('valkey-total-keys', valkey.total_keys != null ? Number(valkey.total_keys).toLocaleString() : '—');

        const tbody = document.getElementById('valkey-keys-body');
        if (tbody) {
            const prefixes = valkey.keys_by_prefix || {};
            const entries = Object.entries(prefixes);
            if (entries.length === 0) {
                tbody.replaceChildren(_emptyRow(2, 'No key data'));
            } else {
                tbody.replaceChildren(...entries.map(([prefix, count]) => {
                    const tr = document.createElement('tr');
                    tr.className = 'border-b b-row';
                    const tdPrefix = document.createElement('td');
                    tdPrefix.className = 'py-1.5 pr-4 mono t-mid';
                    tdPrefix.textContent = prefix;
                    const tdCount = document.createElement('td');
                    tdCount.className = 'py-1.5 mono t-mid text-right';
                    tdCount.textContent = Number(count).toLocaleString();
                    tr.append(tdPrefix, tdCount);
                    return tr;
                }));
            }
        }
    }
}

class AdminFeedback extends AdminServices {

    showToast(message, type = 'success') {
        const toast = document.getElementById('toast');
        if (!toast) return;

        // Clear any pending hide/remove timers from a still-visible prior toast
        // so overlapping calls don't hide this one early (each new toast resets
        // its own 3s + 300ms window).
        if (this._toastHideTimer) {
            clearTimeout(this._toastHideTimer);
            this._toastHideTimer = null;
        }
        if (this._toastRemoveTimer) {
            clearTimeout(this._toastRemoveTimer);
            this._toastRemoveTimer = null;
        }

        toast.textContent = message;
        toast.style.display = 'block';

        if (type === 'success') {
            toast.style.backgroundColor = 'var(--status-ok)';
        } else {
            toast.style.backgroundColor = 'var(--status-err)';
        }
        toast.style.color = 'var(--on-status)';

        requestAnimationFrame(() => {
            toast.style.opacity = '1';
            toast.style.transform = 'translateY(0)';
        });

        this._toastHideTimer = setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(0.5rem)';
            this._toastRemoveTimer = setTimeout(() => {
                toast.style.display = 'none';
                this._toastRemoveTimer = null;
            }, 300);
            this._toastHideTimer = null;
        }, 3000);
    }

    // ─── Auto-refresh ────────────────────────────────────────────────────────

    startAutoRefresh() {
        this.refreshSubscription.start(() => {
            this.loadExtractions();
            if (this.activeTab === 'users') {
                this.fetchUserStats();
                this.fetchSyncActivity();
            } else if (this.activeTab === 'storage') {
                this.fetchStorage();
            } else if (HISTORY_TABS.includes(this.activeTab)) {
                this._refreshHistory();
            } else if (this.activeTab === 'audit-log') {
                this.fetchAuditLog();
            }
        });
    }

    stopAutoRefresh() {
        this.refreshSubscription.stop();
    }
}

class AdminQueueHistory extends AdminFeedback {

    async fetchQueueHistory(range) {
        const loadingEl = document.getElementById('qt-loading');
        const errorEl = document.getElementById('qt-error');
        const errorMsgEl = document.getElementById('qt-error-msg');
        if (loadingEl) loadingEl.style.display = 'inline';
        if (errorEl) errorEl.style.display = 'none';

        try {
            const response = await this.authFetch(`/api/history/queues?range=${encodeURIComponent(range)}`);
            if (!response.ok) {
                const err = await response.json().catch(() => ({}));
                if (response.status === 503) {
                    this._showHistoryUnavailable('queue', errorEl, errorMsgEl);
                    return;
                }
                this._showInlineError(errorEl, errorMsgEl, err.detail || `Error ${response.status}`);
                return;
            }

            const normalized = await response.json();
            this.renderQueueSummaryTiles(normalized);
            this.renderQueueDepthChart(normalized);
            this.renderDlqGrid(normalized);
        } catch (err) {
            this._showInlineError(errorEl, errorMsgEl, `Failed to load queue history: ${err.message || err}`);
        } finally {
            if (loadingEl) loadingEl.style.display = 'none';
        }
    }

    renderQueueSummaryTiles(data) {
        const container = document.getElementById('queue-summary-tiles');
        if (!container) return;

        const queues = data.queues || [];
        const dlqs = data.dlq_summary || [];

        const totalDepth = queues.reduce((sum, q) => {
            const pts = q.data_points || [];
            const latest = pts.length > 0 ? (pts[pts.length - 1].messages_ready || 0) : 0;
            return sum + latest;
        }, 0);
        const depthSparkPoints = this._aggregateSparkline(queues, 'messages_ready');

        const dlqTotal = dlqs.reduce((sum, q) => {
            const pts = q.data_points || [];
            const latest = pts.length > 0 ? (pts[pts.length - 1].messages_ready || 0) : 0;
            return sum + latest;
        }, 0);
        const dlqSparkPoints = this._aggregateSparkline(dlqs, 'messages_ready');

        const avgPublish = this._avgLatest(queues, 'publish_rate');
        const avgAck = this._avgLatest(queues, 'ack_rate');
        const totalConsumers = queues.reduce((sum, q) => {
            const pts = q.data_points || [];
            const latest = pts.length > 0 ? (pts[pts.length - 1].consumers || 0) : 0;
            return sum + latest;
        }, 0);

        const tiles = [
            { label: 'Total Queue Depth', value: totalDepth.toLocaleString(), spark: depthSparkPoints, color: 'var(--series-1)' },
            { label: 'DLQ Messages', value: dlqTotal.toLocaleString(), spark: dlqSparkPoints, color: 'var(--status-err)' },
            { label: 'Avg Publish Rate', value: avgPublish == null ? 'No rate data' : `${avgPublish.toFixed(1)}/s`, spark: null, color: null },
            { label: 'Avg Ack Rate', value: avgAck == null ? 'No rate data' : `${avgAck.toFixed(1)}/s`, spark: null, color: null },
            { label: 'Active Consumers', value: totalConsumers.toLocaleString(), spark: null, color: null },
        ];

        container.replaceChildren(...tiles.map(t => {
            const card = document.createElement('div');
            card.className = 'stat-card';

            const labelEl = document.createElement('p');
            labelEl.className = 'text-[10px] font-bold uppercase tracking-wider t-muted mb-1';
            labelEl.textContent = t.label;

            const row = document.createElement('div');
            row.className = 'flex items-center justify-between';

            const valEl = document.createElement('p');
            valEl.className = 'text-2xl font-semibold mono t-high';
            valEl.textContent = t.value;

            row.appendChild(valEl);

            if (t.spark && t.spark.length >= 2) {
                const sparkDiv = document.createElement('div');
                sparkDiv.appendChild(this._createSparklineSVGElement(t.spark, t.color));
                row.appendChild(sparkDiv);
            }

            card.append(labelEl, row);
            return card;
        }));
    }

    renderQueueDepthChart(data) {
        const canvas = document.getElementById('queue-depth-chart');
        if (!canvas) return;

        if (this.queueDepthChart) {
            this.queueDepthChart.destroy();
            this.queueDepthChart = null;
        }

        const queues = data.queues || [];
        if (queues.length === 0) return;

        const datasets = queues.map((q, i) => {
            const pts = q.data_points || [];
            return {
                label: _shortQueueName(q.queue_name || `Queue ${i + 1}`),
                data: pts.map(p => ({ x: p.timestamp, y: p.messages_ready || 0 })),
                borderColor: seriesColor(i),
                backgroundColor: seriesColor(i) + '20',
                borderWidth: 2,
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            };
        });

        // Update legend area using safe DOM methods
        const legendEl = document.getElementById('queue-chart-legend');
        if (legendEl) {
            legendEl.replaceChildren(...datasets.map(ds => {
                const span = document.createElement('span');
                span.className = 'flex items-center gap-1';
                const swatch = document.createElement('span');
                swatch.style.cssText = `display:inline-block;width:12px;height:3px;background:${ds.borderColor};border-radius:2px`;
                const label = document.createElement('span');
                label.className = 't-muted';
                label.textContent = ds.label;
                span.append(swatch, label);
                return span;
            }));
        }

        const axes = chartAxisColors();
        this._lastQueueHistory = data;
        this.queueDepthChart = new Chart(canvas, {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { display: false },
                    tooltip: { enabled: true },
                },
                scales: {
                    x: {
                        type: 'time',
                        time: { tooltipFormat: 'MMM d, HH:mm' },
                        grid: { color: axes.grid },
                        ticks: { color: axes.tick, maxTicksLimit: 8 },
                    },
                    y: {
                        beginAtZero: true,
                        grid: { color: axes.grid },
                        ticks: { color: axes.tick },
                    },
                },
            },
        });
    }

    renderDlqGrid(data) {
        const container = document.getElementById('dlq-grid');
        if (!container) return;

        const dlqs = data.dlq_summary || [];
        if (dlqs.length === 0) {
            container.replaceChildren();
            const empty = document.createElement('p');
            empty.className = 'text-xs t-muted col-span-2 text-center py-4';
            empty.textContent = 'No DLQ data available';
            container.appendChild(empty);
            return;
        }

        container.replaceChildren(...dlqs.map(q => {
            const card = document.createElement('div');
            card.className = 'stat-card';

            const name = document.createElement('p');
            name.className = 'text-[10px] font-bold uppercase tracking-wider t-muted mb-1 truncate';
            name.textContent = _shortQueueName(q.queue_name || '\u2014');
            name.title = q.queue_name || '';

            const row = document.createElement('div');
            row.className = 'flex items-center justify-between';

            const pts = q.data_points || [];
            const latest = pts.length > 0 ? (pts[pts.length - 1].messages_ready || 0) : 0;

            const val = document.createElement('p');
            val.className = 'text-xl font-semibold mono t-high';
            val.textContent = latest.toLocaleString();

            row.appendChild(val);

            const sparkPts = pts.map(p => p.messages_ready || 0);
            if (sparkPts.length >= 2) {
                const sparkDiv = document.createElement('div');
                sparkDiv.appendChild(this._createSparklineSVGElement(sparkPts, 'var(--status-err)'));
                row.appendChild(sparkDiv);
            }

            card.append(name, row);
            return card;
        }));
    }
}

class AdminServiceHealth extends AdminQueueHistory {

    async fetchHealthHistory(range) {
        const loadingEl = document.getElementById('sh-loading');
        const errorEl = document.getElementById('sh-error');
        const errorMsgEl = document.getElementById('sh-error-msg');
        if (loadingEl) loadingEl.style.display = 'inline';
        if (errorEl) errorEl.style.display = 'none';

        try {
            const response = await this.authFetch(`/api/history/health?range=${encodeURIComponent(range)}`);
            if (!response.ok) {
                const err = await response.json().catch(() => ({}));
                if (response.status === 503) {
                    this._showHistoryUnavailable('health', errorEl, errorMsgEl);
                    return;
                }
                this._showInlineError(errorEl, errorMsgEl, err.detail || `Error ${response.status}`);
                return;
            }

            const normalized = await response.json();
            this.renderServiceCards(normalized);
            this.renderResponseTimeChart(normalized);
            this.renderEndpointsTable(normalized);
        } catch (err) {
            this._showInlineError(errorEl, errorMsgEl, `Failed to load health history: ${err.message || err}`);
        } finally {
            if (loadingEl) loadingEl.style.display = 'none';
        }
    }

    renderServiceCards(data) {
        const container = document.getElementById('service-status-cards');
        if (!container) return;

        const services = data.services || [];
        if (services.length === 0) {
            container.replaceChildren();
            const empty = document.createElement('p');
            empty.className = 'text-xs t-muted sh-cards-empty text-center py-4';
            empty.textContent = 'No service data available';
            container.appendChild(empty);
            return;
        }

        container.replaceChildren(...services.map(svc => {
            const card = document.createElement('div');
            card.className = 'stat-card';

            const status = (svc.status || 'unknown').toLowerCase();
            let borderColor = 'var(--status-err)';
            if (status === 'healthy') borderColor = 'var(--status-ok)';
            else if (status === 'degraded' || status === 'starting') borderColor = 'var(--status-warn)';
            card.style.borderLeft = `3px solid ${borderColor}`;

            const nameEl = document.createElement('p');
            nameEl.className = 'text-xs font-semibold t-high mb-2';
            nameEl.textContent = svc.service_name || '\u2014';

            const statusEl = document.createElement('p');
            statusEl.className = 'text-[10px] font-bold uppercase tracking-wider mb-2';
            statusEl.style.color = borderColor;
            statusEl.textContent = svc.status || 'unknown';

            const uptimeLabel = document.createElement('p');
            uptimeLabel.className = 'text-[10px] font-bold uppercase tracking-wider t-muted mb-0.5';
            uptimeLabel.textContent = 'Uptime';
            const uptimeVal = document.createElement('p');
            uptimeVal.className = 'text-sm font-semibold mono t-high';
            uptimeVal.textContent = svc.uptime_pct != null ? `${svc.uptime_pct.toFixed(1)}%` : '\u2014';

            const rtLabel = document.createElement('p');
            rtLabel.className = 'text-[10px] font-bold uppercase tracking-wider t-muted mb-0.5 mt-2';
            rtLabel.textContent = 'Response Time';
            const rtVal = document.createElement('p');
            rtVal.className = 'text-sm font-semibold mono t-high';
            rtVal.textContent = svc.response_time_ms != null ? `${svc.response_time_ms.toFixed(0)}ms` : '\u2014';

            card.append(nameEl, statusEl, uptimeLabel, uptimeVal, rtLabel, rtVal);
            return card;
        }));
    }

    renderResponseTimeChart(data) {
        const canvas = document.getElementById('response-time-chart');
        if (!canvas) return;

        if (this.responseTimeChart) {
            this.responseTimeChart.destroy();
            this.responseTimeChart = null;
        }

        const timeSeries = data.response_time_series || [];
        if (timeSeries.length === 0) return;

        const status = { ok: themeVar('--status-ok'), warn: themeVar('--status-warn'), err: themeVar('--status-err') };
        const axes = chartAxisColors();
        this._lastResponseTimeData = data;
        const datasets = [
            {
                label: 'p50',
                data: timeSeries.map(p => ({ x: p.timestamp, y: p.p50 || 0 })),
                borderColor: status.ok,
                borderWidth: 2,
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            },
            {
                label: 'p95',
                data: timeSeries.map(p => ({ x: p.timestamp, y: p.p95 || 0 })),
                borderColor: status.warn,
                borderWidth: 2,
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            },
            {
                label: 'p99',
                data: timeSeries.map(p => ({ x: p.timestamp, y: p.p99 || 0 })),
                borderColor: status.err,
                borderWidth: 2,
                borderDash: [5, 3],
                pointRadius: 0,
                tension: 0.3,
                fill: false,
            },
        ];

        this.responseTimeChart = new Chart(canvas, {
            type: 'line',
            data: { datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { display: false },
                    tooltip: { enabled: true },
                },
                scales: {
                    x: {
                        type: 'time',
                        time: { tooltipFormat: 'MMM d, HH:mm' },
                        grid: { color: axes.grid },
                        ticks: { color: axes.tick, maxTicksLimit: 8 },
                    },
                    y: {
                        beginAtZero: true,
                        grid: { color: axes.grid },
                        ticks: {
                            color: axes.tick,
                            callback: (v) => `${v}ms`,
                        },
                    },
                },
            },
        });
    }

    renderEndpointsTable(data) {
        const tbody = document.getElementById('endpoints-tbody');
        if (!tbody) return;

        const endpoints = data.endpoints || [];
        if (endpoints.length === 0) {
            tbody.replaceChildren(_emptyRow(6, 'No endpoint data available'));
            return;
        }

        const sorted = [...endpoints].sort((a, b) => (b.request_count || 0) - (a.request_count || 0));

        tbody.replaceChildren(...sorted.map(ep => {
            const tr = document.createElement('tr');
            tr.className = 'border-b b-row';

            const tdPath = document.createElement('td');
            tdPath.className = 'py-2 px-4 mono text-xs';
            tdPath.style.color = 'var(--accent)';
            tdPath.textContent = ep.endpoint ? (ep.service_name ? `${ep.service_name} ${ep.endpoint}` : ep.endpoint) : '\u2014';

            const tdReqs = document.createElement('td');
            tdReqs.className = 'py-2 px-4 mono t-mid text-right';
            tdReqs.textContent = ep.request_count != null ? Number(ep.request_count).toLocaleString() : '\u2014';

            const tdP50 = document.createElement('td');
            tdP50.className = 'py-2 px-4 mono t-mid text-right';
            tdP50.textContent = ep.p50 != null ? `${ep.p50.toFixed(0)}ms` : '\u2014';

            const tdP95 = document.createElement('td');
            tdP95.className = 'py-2 px-4 mono t-mid text-right';
            tdP95.textContent = ep.p95 != null ? `${ep.p95.toFixed(0)}ms` : '\u2014';

            const tdP99 = document.createElement('td');
            tdP99.className = 'py-2 px-4 mono t-mid text-right';
            tdP99.textContent = ep.p99 != null ? `${ep.p99.toFixed(0)}ms` : '\u2014';

            const tdErr = document.createElement('td');
            tdErr.className = 'py-2 px-4 mono text-right font-semibold';
            const errPct = ep.error_pct != null ? ep.error_pct : 0;
            tdErr.textContent = `${errPct.toFixed(1)}%`;
            if (errPct < 1) tdErr.style.color = 'var(--status-ok)';
            else if (errPct <= 5) tdErr.style.color = 'var(--status-warn)';
            else tdErr.style.color = 'var(--status-err)';

            tr.append(tdPath, tdReqs, tdP50, tdP95, tdP99, tdErr);
            return tr;
        }));
    }

    // ─── Helpers ─────────────────────────────────────────────────────────────

    _statusBadgeClass(status) {
        switch ((status || '').toLowerCase()) {
            case 'completed': return 'badge-completed';
            case 'waiting':   return 'badge-waiting';
            case 'failed':    return 'badge-failed';
            case 'running':
            case 'pending':   return 'badge-running';
            default:          return 'badge-idle';
        }
    }

    _setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    _showInlineError(errorEl, errorMsgEl, message) {
        if (errorMsgEl) errorMsgEl.textContent = message;
        if (errorEl) errorEl.style.display = 'flex';
    }

    _getRange() {
        const saved = localStorage.getItem(RANGE_STORAGE_KEY);
        return HISTORY_RANGES.includes(saved) ? saved : '24h';
    }

    _syncRangeButtons() {
        const range = this._getRange();
        document.querySelectorAll('.range-btn').forEach(b => b.classList.toggle('active', b.dataset.range === range));
    }

    _refreshHistory() {
        if (this.activeTab === 'queue-trends') this.fetchQueueHistory(this._getRange());
        else if (this.activeTab === 'system-health') this.fetchHealthHistory(this._getRange());
    }

    // VictoriaMetrics is down: say so explicitly and drop stale panels. The live Overview does not depend on it.
    _showHistoryUnavailable(kind, errorEl, errorMsgEl) {
        this._showInlineError(errorEl, errorMsgEl, 'History unavailable: the metrics store is not answering. Live data on Overview is unaffected.');
        if (kind === 'queue') {
            this.renderQueueSummaryTiles({});
            this.renderQueueDepthChart({});
            this.renderDlqGrid({});
        } else {
            this.renderServiceCards({});
            this.renderResponseTimeChart({});
            this.renderEndpointsTable({});
        }
    }

    _aggregateSparkline(queues, field) {
        if (!queues || queues.length === 0) return [];
        // Find the max number of data points across queues
        const maxLen = Math.max(...queues.map(q => (q.data_points || []).length));
        if (maxLen === 0) return [];
        const result = [];
        for (let i = 0; i < maxLen; i++) {
            let sum = 0;
            for (const q of queues) {
                const pts = q.data_points || [];
                if (i < pts.length) {
                    sum += pts[i][field] || 0;
                }
            }
            result.push(sum);
        }
        return result;
    }

    _avgLatest(queues, field) {
        // null when no queue has a reading, so an unscraped rate shows as "no data" rather than 0/s
        let sum = 0;
        let count = 0;
        for (const q of queues || []) {
            const pts = q.data_points || [];
            const latest = pts.length > 0 ? pts[pts.length - 1][field] : null;
            if (latest != null) {
                sum += latest;
                count++;
            }
        }
        return count > 0 ? sum / count : null;
    }

    _createSparklineSVGElement(points, color, width = 55, height = 20) {
        const ns = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('width', String(width));
        svg.setAttribute('height', String(height));

        if (!points || points.length < 2) return svg;

        const max = Math.max(...points);
        const min = Math.min(...points);
        const range = max - min || 1;
        const pad = 2;
        const usableH = height - pad * 2;
        const step = (width - pad * 2) / (points.length - 1);
        const coords = points.map((v, i) =>
            `${(pad + i * step).toFixed(1)},${(pad + usableH - ((v - min) / range) * usableH).toFixed(1)}`
        ).join(' ');

        const polyline = document.createElementNS(ns, 'polyline');
        polyline.setAttribute('points', coords);
        polyline.setAttribute('fill', 'none');
        polyline.style.stroke = color;
        polyline.setAttribute('stroke-width', '1.5');
        polyline.setAttribute('stroke-linecap', 'round');
        polyline.setAttribute('stroke-linejoin', 'round');
        svg.appendChild(polyline);

        return svg;
    }

    static _prettyPrintXml(xml) {
        let formatted = '';
        let indent = 0;
        const parts = xml.replace(/>\s*</g, '><').split(/(<[^>]+>)/);
        for (const part of parts) {
            if (!part.trim()) continue;
            if (part.startsWith('</')) {
                indent = Math.max(0, indent - 1);
                formatted += '  '.repeat(indent) + part + '\n';
            } else if (part.startsWith('<') && !part.startsWith('<?') && !part.endsWith('/>') && !part.startsWith('<!')) {
                formatted += '  '.repeat(indent) + part + '\n';
                indent++;
            } else if (part.endsWith('/>')) {
                formatted += '  '.repeat(indent) + part + '\n';
            } else {
                formatted += '  '.repeat(indent) + part + '\n';
            }
        }
        return formatted.trim();
    }
}

class AdminContracts extends AdminServiceHealth {

    async fetchAuditLog() {
        const loading = document.getElementById('al-loading');
        const error = document.getElementById('al-error');
        const errorMsg = document.getElementById('al-error-msg');

        if (loading) loading.style.display = '';
        if (error) error.style.display = 'none';

        const actionFilter = document.getElementById('al-action-filter')?.value || '';
        let url = `/admin/api/audit-log?page=${this._auditLogPage}&page_size=50`;
        if (actionFilter) url += `&action=${encodeURIComponent(actionFilter)}`;

        try {
            const resp = await this.authFetch(url);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            this.renderAuditLog(data);
        } catch (err) {
            if (errorMsg) errorMsg.textContent = err.message;
            if (error) error.style.display = '';
        } finally {
            if (loading) loading.style.display = 'none';
        }
    }

    renderAuditLog(data) {
        const tbody = document.getElementById('al-table-body');
        if (!tbody) return;

        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

        if (!data.entries || data.entries.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.setAttribute('colspan', '5');
            td.className = 'text-center py-8 t-muted';
            td.textContent = 'No audit log entries';
            tr.appendChild(td);
            tbody.appendChild(tr);
        } else {
            for (const entry of data.entries) {
                const tr = document.createElement('tr');
                tr.className = 'border-b b-theme hover:bg-hover transition-colors';

                const tdTs = document.createElement('td');
                tdTs.className = 'py-2 px-3 t-muted whitespace-nowrap';
                tdTs.textContent = new Date(entry.created_at).toLocaleString();
                tr.appendChild(tdTs);

                const tdAdmin = document.createElement('td');
                tdAdmin.className = 'py-2 px-3';
                tdAdmin.textContent = entry.admin_email;
                tr.appendChild(tdAdmin);

                const tdAction = document.createElement('td');
                tdAction.className = 'py-2 px-3';
                const badge = document.createElement('span');
                badge.className = 'inline-block px-2 py-0.5 rounded text-[10px] font-medium badge-secondary';
                badge.textContent = entry.action;
                tdAction.appendChild(badge);
                tr.appendChild(tdAction);

                const tdTarget = document.createElement('td');
                tdTarget.className = 'py-2 px-3 t-muted';
                tdTarget.textContent = entry.target || '\u2014';
                tr.appendChild(tdTarget);

                const tdDetails = document.createElement('td');
                tdDetails.className = 'py-2 px-3 t-muted text-[10px] font-mono max-w-[200px] truncate';
                const detailsText = entry.details ? JSON.stringify(entry.details) : '\u2014';
                tdDetails.textContent = detailsText;
                tdDetails.title = detailsText;
                tr.appendChild(tdDetails);

                tbody.appendChild(tr);
            }
        }

        const pagination = document.getElementById('al-pagination');
        const pageInfo = document.getElementById('al-page-info');
        const prevBtn = document.getElementById('al-prev-btn');
        const nextBtn = document.getElementById('al-next-btn');

        if (pagination && data.total > 0) {
            pagination.style.display = '';
            const totalPages = Math.ceil(data.total / data.page_size);
            if (pageInfo) pageInfo.textContent = `Page ${data.page} of ${totalPages} (${data.total} entries)`;
            if (prevBtn) prevBtn.disabled = data.page <= 1;
            if (nextBtn) nextBtn.disabled = data.page >= totalPages;
        } else if (pagination) {
            pagination.style.display = 'none';
        }
    }

    // ─── Extraction Analysis ─────────────────────────────────────────────────

    _eaSwitchView(viewName) {
        // Toggle sub-view buttons
        document.querySelectorAll('.ea-view-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.eaView === viewName);
        });
        // Toggle sub-view panels
        ['report', 'compare', 'prompt'].forEach(name => {
            const panel = document.getElementById(`ea-view-${name}`);
            if (panel) panel.style.display = name === viewName ? '' : 'none';
        });
        // Show/hide report controls in the header
        const reportControls = document.getElementById('ea-report-controls');
        if (reportControls) reportControls.style.display = viewName === 'report' ? '' : 'none';

        if (viewName === 'prompt') this._eaUpdatePromptCount();
    }

    async fetchExtractionAnalysisVersions() {
        try {
            const resp = await this.authFetch('/admin/api/extraction-analysis/versions');
            if (!resp.ok) return;
            const data = await resp.json();
            this._eaVersions = data.versions || [];
            this._eaPopulateVersionSelects();
            // Auto-load the first version
            if (this._eaVersions.length > 0) {
                const sel = document.getElementById('ea-version-select');
                if (sel && sel.value === '') {
                    sel.value = this._eaVersionString(this._eaVersions[0]);
                    this._eaLoadReport();
                }
            }
        } catch {
            // Silently fail — non-critical
        }
    }

    _eaVersionString(v) {
        return typeof v === 'string' ? v : v.version;
    }

    _eaVersionLabel(v) {
        if (typeof v === 'string') return v;
        return v.source ? `${v.version} (${v.source})` : v.version;
    }

    _eaPopulateVersionSelects() {
        const ids = ['ea-version-select', 'ea-cmp-version-a', 'ea-cmp-version-b'];
        ids.forEach(id => {
            const sel = document.getElementById(id);
            if (!sel) return;
            const current = sel.value;
            // Rebuild options
            while (sel.firstChild) sel.removeChild(sel.firstChild);
            const placeholder = document.createElement('option');
            placeholder.value = '';
            placeholder.textContent = '— select —';
            sel.appendChild(placeholder);
            this._eaVersions.forEach(v => {
                const opt = document.createElement('option');
                opt.value = this._eaVersionString(v);
                opt.textContent = this._eaVersionLabel(v);
                sel.appendChild(opt);
            });
            // Restore previous selection if still valid
            const versionStrings = this._eaVersions.map(v => this._eaVersionString(v));
            if (current && versionStrings.includes(current)) sel.value = current;
        });
    }

    async _eaLoadReport() {
        const sel = document.getElementById('ea-version-select');
        const version = sel ? sel.value : '';
        if (!version) return;

        // Reset parsing errors cache for new version
        this._eaParsingErrors = null;

        const spinner = document.getElementById('ea-loading-spinner');
        if (spinner) spinner.style.display = '';

        // Fetch summary, parsing errors, and media mapping coverage in parallel
        try {
            const [summaryResp, errorsResp, mappingResp] = await Promise.all([
                this.authFetch(`/admin/api/extraction-analysis/${encodeURIComponent(version)}/summary`),
                this.authFetch(`/admin/api/extraction-analysis/${encodeURIComponent(version)}/parsing-errors`),
                this.authFetch(`/admin/api/extraction-analysis/${encodeURIComponent(version)}/media-mapping-coverage`),
            ]);

            if (summaryResp.ok) {
                const summary = await summaryResp.json();
                // Update source badge
                const badge = document.getElementById('ea-source-badge');
                if (badge) {
                    badge.textContent = summary.source || 'unknown';
                    badge.style.display = '';
                    badge.className = `text-[10px] px-2 py-0.5 rounded uppercase font-bold ${summary.source === 'discogs' ? 'badge-completed' : 'badge-running'}`;
                }
                this._eaRenderPipelineStatus(summary.pipeline_status || {});
                this._eaRenderEntityCards(summary);
                this._eaRenderSkippedCards(summary.skipped || {});
                // Store parsing errors for use in rule breakdown
                if (errorsResp.ok) {
                    const errData = await errorsResp.json();
                    this._eaParsingErrors = errData;
                }
                this._eaRenderRuleBreakdown(summary.by_rule || []);
            }

            if (mappingResp.ok) {
                const mapping = await mappingResp.json();
                this._eaRenderMediaMappingCoverage(mapping);
            } else {
                this._eaRenderMediaMappingCoverage(null);
            }
        } catch {
            // Coverage is optional; leave the existing view unchanged if it cannot load.
        } finally {
            if (spinner) spinner.style.display = 'none';
        }
    }

    _eaRenderPipelineStatus(pipelineStatus) {
        const container = document.getElementById('ea-pipeline-cards');
        if (!container) return;
        const phases = Object.entries(pipelineStatus);
        if (phases.length === 0) {
            container.replaceChildren();
            const p = document.createElement('p');
            p.className = 'text-xs t-muted';
            p.textContent = 'No pipeline status available.';
            container.appendChild(p);
            return;
        }
        const cards = phases.map(([phase, status]) => {
            const card = document.createElement('div');
            card.className = 'stat-card flex flex-col gap-1';
            const label = document.createElement('p');
            label.className = 'text-[10px] font-bold uppercase tracking-wider t-muted';
            label.textContent = phase;
            const badgeEl = document.createElement('span');
            const badgeClass = status === 'completed' ? 'badge-completed' : status === 'running' ? 'badge-running' : status === 'failed' ? 'badge-failed' : 'badge-idle';
            badgeEl.className = `text-[10px] px-2 py-0.5 rounded uppercase font-bold ${badgeClass}`;
            badgeEl.textContent = String(status);
            card.append(label, badgeEl);
            return card;
        });
        container.replaceChildren(...cards);
    }

    _eaRenderEntityCards(summary) {
        const container = document.getElementById('ea-entity-cards');
        if (!container) return;
        const byEntity = summary.by_entity || {};
        const entries = Object.entries(byEntity);
        if (entries.length === 0) {
            container.replaceChildren();
            const p = document.createElement('p');
            p.className = 'text-xs t-muted';
            p.textContent = 'No violation data.';
            container.appendChild(p);
            return;
        }
        const cards = entries.map(([entityType, counts]) => {
            const card = document.createElement('div');
            card.className = 'stat-card flex flex-col gap-1';
            const label = document.createElement('p');
            label.className = 'text-[10px] font-bold uppercase tracking-wider t-muted';
            label.textContent = entityType;
            const total = document.createElement('p');
            total.className = 'text-xl font-bold t-high';
            total.textContent = (counts.total ?? 0).toLocaleString();
            const sub = document.createElement('p');
            sub.className = 'text-[10px] t-muted';
            sub.textContent = `${(counts.errors ?? 0)} errors · ${(counts.warnings ?? 0)} warnings`;
            card.append(label, total, sub);
            return card;
        });
        container.replaceChildren(...cards);
    }

    // Media mapping coverage: releases carrying a raw provider media name the canonical
    // taxonomy did not recognise, kept in each release's `media.unmapped` block and ranked
    // by catalog-api's `/api/admin/media/unmapped` route, which the console's
    // `/media-mapping-coverage` proxy route reads per provider. Both Discogs and
    // MusicBrainz return a real reading. `data` is null on a fetch failure and
    // `{available: false, reason}` when the version's source names no readable provider.
    // Counts are release counts against every media-tagged release, not rule-violation
    // counts against one extraction version as in the earlier violations-based reading.
    _eaRenderMediaMappingCoverage(data) {
        const unavailableEl = document.getElementById('ea-media-mapping-unavailable');
        const bodyEl = document.getElementById('ea-media-mapping-body');
        if (!unavailableEl || !bodyEl) return;

        if (!data || !data.available) {
            bodyEl.style.display = 'none';
            unavailableEl.style.display = '';
            unavailableEl.textContent = (data && data.reason) || 'Media mapping coverage could not be loaded for this version.';
            return;
        }

        unavailableEl.style.display = 'none';
        bodyEl.style.display = '';

        const cardsContainer = document.getElementById('ea-media-mapping-cards');
        if (cardsContainer) {
            const stats = [
                { label: 'Provider', value: data.provider ?? data.source ?? '—' },
                { label: 'Releases with unmapped media', value: (data.releases_with_unmapped_media ?? 0).toLocaleString() },
                { label: 'Media-tagged releases', value: (data.media_tagged_releases ?? 0).toLocaleString() },
                { label: 'Distinct unmapped names', value: (data.top_unmapped_formats || []).length.toLocaleString() },
            ];
            if (data.unmapped_rate !== null && data.unmapped_rate !== undefined) {
                stats.push({ label: 'Unmapped rate', value: `${(data.unmapped_rate * 100).toFixed(2)}%` });
            }
            const cards = stats.map(({ label, value }) => {
                const card = document.createElement('div');
                card.className = 'stat-card flex flex-col gap-1';
                const labelEl = document.createElement('p');
                labelEl.className = 'text-[10px] font-bold uppercase tracking-wider t-muted';
                labelEl.textContent = label;
                const valueEl = document.createElement('p');
                valueEl.className = 'text-xl font-bold t-high';
                valueEl.textContent = value;
                card.append(labelEl, valueEl);
                return card;
            });
            cardsContainer.replaceChildren(...cards);
        }

        const tbody = document.getElementById('ea-media-mapping-tbody');
        if (tbody) {
            const formats = data.top_unmapped_formats || [];
            if (formats.length === 0) {
                const row = document.createElement('tr');
                const cell = document.createElement('td');
                cell.colSpan = 3;
                cell.className = 'py-6 text-center t-muted';
                cell.textContent = 'No unmapped media names found.';
                row.appendChild(cell);
                tbody.replaceChildren(row);
            } else {
                const rows = formats.map(({ kind, name, count }) => {
                    const row = document.createElement('tr');
                    row.className = 'border-b b-theme';
                    const kindCell = document.createElement('td');
                    kindCell.className = 'py-2 px-2 t-muted';
                    kindCell.textContent = kind ?? '—';
                    const nameCell = document.createElement('td');
                    nameCell.className = 'py-2 px-2 mono';
                    nameCell.textContent = name;
                    const countCell = document.createElement('td');
                    countCell.className = 'py-2 px-2 text-right';
                    countCell.textContent = (count ?? 0).toLocaleString();
                    row.append(kindCell, nameCell, countCell);
                    return row;
                });
                tbody.replaceChildren(...rows);
            }
        }

        const truncatedNote = document.getElementById('ea-media-mapping-truncated-note');
        if (truncatedNote) {
            truncatedNote.style.display = data.truncated ? '' : 'none';
            truncatedNote.textContent = data.truncated
                ? 'The ranked list is capped — more distinct unmapped media names exist upstream than are shown. The counts above are exact.'
                : '';
        }
    }

    _eaRenderSkippedCards(skippedSummary) {
        const section = document.getElementById('ea-skipped-section');
        const container = document.getElementById('ea-skipped-cards');
        const detail = document.getElementById('ea-skipped-detail');
        if (!section || !container) return;

        const entries = Object.entries(skippedSummary || {});
        if (entries.length === 0) {
            section.style.display = 'none';
            return;
        }

        section.style.display = '';
        const cards = entries.map(([entityType, info]) => {
            const card = document.createElement('div');
            card.className = 'stat-card flex flex-col gap-1 cursor-pointer hover:ring-1 ring-current rounded';
            card.title = 'Click to view skipped records';
            const label = document.createElement('p');
            label.className = 'text-[10px] font-bold uppercase tracking-wider t-muted';
            label.textContent = entityType;
            const count = document.createElement('p');
            count.className = 'text-xl font-bold t-high';
            count.textContent = (info.count ?? 0).toLocaleString();
            const reason = document.createElement('p');
            reason.className = 'text-[10px] t-muted truncate';
            reason.style.maxWidth = '200px';
            reason.textContent = (info.reasons || []).join(', ') || 'Unknown reason';
            card.append(label, count, reason);

            card.addEventListener('click', () => this._eaLoadSkippedDetail(entityType));
            return card;
        });
        container.replaceChildren(...cards);
        if (detail) detail.style.display = 'none';
    }

    async _eaLoadSkippedDetail(entityType) {
        const sel = document.getElementById('ea-version-select');
        const version = sel ? sel.value : '';
        if (!version) return;
        const detail = document.getElementById('ea-skipped-detail');
        if (!detail) return;

        try {
            const resp = await this.authFetch(
                `/admin/api/extraction-analysis/${encodeURIComponent(version)}/skipped?entity_type=${encodeURIComponent(entityType)}`
            );
            if (!resp.ok) return;
            const data = await resp.json();
            const items = data.skipped || [];

            if (items.length === 0) {
                detail.style.display = 'none';
                return;
            }

            const heading = document.createElement('p');
            heading.className = 'text-xs font-bold t-dim uppercase tracking-wider';
            heading.textContent = `Skipped ${entityType} (${items.length})`;

            const list = document.createElement('div');
            list.className = 'flex flex-wrap gap-2';

            for (const item of items) {
                const chip = document.createElement('button');
                chip.className = 'text-xs px-2 py-1 rounded border b-theme t-mid hover:t-high mono';
                chip.textContent = item.record_id;
                chip.title = item.reason || '';
                chip.addEventListener('click', () => this._eaShowRecordDetail(version, item.record_id, entityType));
                list.appendChild(chip);
            }

            detail.replaceChildren(heading, list);
            detail.style.display = '';
        } catch {
            // Silently fail
        }
    }

    _eaRenderRuleBreakdown(byRule) {
        const tbody = document.getElementById('ea-rule-tbody');
        if (!tbody) return;
        if (!byRule || byRule.length === 0) {
            tbody.replaceChildren(_emptyRow(7, 'No violations found.'));
            return;
        }
        // Count parsing errors per entity type for the badge column
        const parseErrByEntity = {};
        if (this._eaParsingErrors && this._eaParsingErrors.by_entity) {
            Object.entries(this._eaParsingErrors.by_entity).forEach(([et, c]) => {
                parseErrByEntity[et] = c;
            });
        }
        const rows = byRule.map(row => {
            const tr = document.createElement('tr');
            tr.className = 'border-b b-row hover:bg-inner cursor-pointer';

            // Checkbox
            const tdChk = document.createElement('td');
            tdChk.className = 'py-2 px-2';
            const chk = document.createElement('input');
            chk.type = 'checkbox';
            chk.className = 'rounded ea-rule-chk';
            chk.dataset.rule = row.rule;
            chk.dataset.entityType = row.entity_type;
            chk.addEventListener('change', () => {
                const key = `${row.rule}::${row.entity_type}`;
                if (chk.checked) {
                    this._eaSelectedRecords.set(key, { rule: row.rule, entity_type: row.entity_type });
                } else {
                    this._eaSelectedRecords.delete(key);
                }
                this._eaUpdatePromptCount();
            });
            tdChk.appendChild(chk);

            // Rule name
            const tdRule = document.createElement('td');
            tdRule.className = 'py-2 px-2 mono t-mid';
            tdRule.textContent = row.rule;

            // Entity type
            const tdEntity = document.createElement('td');
            tdEntity.className = 'py-2 px-2 t-dim';
            tdEntity.textContent = row.entity_type || '—';

            // Severity
            const tdSev = document.createElement('td');
            tdSev.className = 'py-2 px-2';
            const sevBadge = document.createElement('span');
            const sevClass = row.severity === 'error' ? 'badge-error' : row.severity === 'warning' ? 'badge-running' : 'badge-idle';
            sevBadge.className = `text-[10px] px-1.5 py-0.5 rounded uppercase font-bold ${sevClass}`;
            sevBadge.textContent = row.severity || 'info';
            tdSev.appendChild(sevBadge);

            // Violation count
            const tdCount = document.createElement('td');
            tdCount.className = 'py-2 px-2 text-right mono t-mid';
            tdCount.textContent = (row.count ?? 0).toLocaleString();

            // Parsing errors badge
            const tdParseErr = document.createElement('td');
            tdParseErr.className = 'py-2 px-2 text-right';
            const parseCount = parseErrByEntity[row.entity_type] ?? 0;
            if (parseCount > 0) {
                const badge = document.createElement('span');
                badge.className = 'text-[10px] px-1.5 py-0.5 rounded font-bold badge-error';
                badge.textContent = parseCount.toLocaleString();
                tdParseErr.appendChild(badge);
            } else {
                tdParseErr.textContent = '—';
                tdParseErr.className += ' t-muted';
            }

            // Sample button
            const tdSample = document.createElement('td');
            tdSample.className = 'py-2 px-2 text-right';
            const sampleBtn = document.createElement('button');
            sampleBtn.className = 'text-[10px] px-2 py-0.5 rounded border b-theme t-dim hover:t-high';
            sampleBtn.textContent = 'View';
            sampleBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._eaShowViolationsForRule(row.rule, row.entity_type);
            });
            tdSample.appendChild(sampleBtn);

            tr.append(tdChk, tdRule, tdEntity, tdSev, tdCount, tdParseErr, tdSample);
            return tr;
        });
        tbody.replaceChildren(...rows);
    }

    async _eaShowViolationsForRule(rule, entityType) {
        this._eaVdRule = rule;
        this._eaVdEntityType = entityType;
        this._eaVdPage = 1;
        await this._eaLoadViolationsPage();
    }

    async _eaLoadViolationsPage() {
        const sel = document.getElementById('ea-version-select');
        const version = sel ? sel.value : '';
        if (!version) return;

        const section = document.getElementById('ea-violations-detail');
        if (!section) return;
        section.style.display = '';
        section.scrollIntoView({ behavior: 'smooth', block: 'start' });

        const ruleEl = document.getElementById('ea-vd-rule');
        if (ruleEl) ruleEl.textContent = this._eaVdRule;
        const entityEl = document.getElementById('ea-vd-entity');
        if (entityEl) entityEl.textContent = this._eaVdEntityType ? `(${this._eaVdEntityType})` : '';

        const pageSizeEl = document.getElementById('ea-vd-page-size');
        const pageSize = pageSizeEl ? parseInt(pageSizeEl.value, 10) : 50;

        const params = new URLSearchParams({
            rule: this._eaVdRule,
            page: String(this._eaVdPage),
            page_size: String(pageSize),
        });
        if (this._eaVdEntityType) params.set('entity_type', this._eaVdEntityType);

        const tbody = document.getElementById('ea-vd-tbody');
        if (tbody) tbody.replaceChildren(_emptyRow(6, 'Loading...'));

        try {
            const resp = await this.authFetch(
                `/admin/api/extraction-analysis/${encodeURIComponent(version)}/violations?${params}`,
            );
            if (!resp.ok) {
                if (tbody) tbody.replaceChildren(_emptyRow(6, 'Failed to load violations.'));
                return;
            }
            const data = await resp.json();
            const violations = data.violations || [];
            const pagination = data.pagination || {};

            if (violations.length === 0) {
                if (tbody) tbody.replaceChildren(_emptyRow(6, 'No violations found.'));
            } else if (tbody) {
                const rows = violations.map(v => {
                    const tr = document.createElement('tr');
                    tr.className = 'border-b b-row';

                    const tdId = document.createElement('td');
                    tdId.className = 'py-2 px-2 mono t-mid';
                    tdId.textContent = v.record_id || '—';

                    const tdRule = document.createElement('td');
                    tdRule.className = 'py-2 px-2 mono t-dim';
                    tdRule.textContent = v.rule || '—';

                    const tdSev = document.createElement('td');
                    tdSev.className = 'py-2 px-2';
                    const sevBadge = document.createElement('span');
                    const sevClass = v.severity === 'error' ? 'badge-error' : v.severity === 'warning' ? 'badge-running' : 'badge-idle';
                    sevBadge.className = `text-[10px] px-1.5 py-0.5 rounded uppercase font-bold ${sevClass}`;
                    sevBadge.textContent = v.severity || 'info';
                    tdSev.appendChild(sevBadge);

                    const tdField = document.createElement('td');
                    tdField.className = 'py-2 px-2 mono t-dim';
                    tdField.textContent = v.field || '—';

                    const tdValue = document.createElement('td');
                    tdValue.className = 'py-2 px-2 mono t-dim';
                    tdValue.textContent = v.field_value || '—';

                    const tdDetail = document.createElement('td');
                    tdDetail.className = 'py-2 px-2 text-right';
                    const detailBtn = document.createElement('button');
                    detailBtn.className = 'text-[10px] px-2 py-0.5 rounded border b-theme t-dim hover:t-high';
                    detailBtn.textContent = 'View';
                    detailBtn.addEventListener('click', () => this._eaShowRecordDetail(version, v.record_id, v.entity_type));
                    tdDetail.appendChild(detailBtn);

                    tr.append(tdId, tdRule, tdSev, tdField, tdValue, tdDetail);
                    return tr;
                });
                tbody.replaceChildren(...rows);
            }

            // Update pagination controls
            const infoEl = document.getElementById('ea-vd-info');
            if (infoEl) {
                const start = ((pagination.page || 1) - 1) * (pagination.page_size || pageSize) + 1;
                const end = Math.min(start + violations.length - 1, pagination.total_items || 0);
                infoEl.textContent = `${start}–${end} of ${(pagination.total_items || 0).toLocaleString()}`;
            }

            const labelEl = document.getElementById('ea-vd-page-label');
            if (labelEl) labelEl.textContent = `${pagination.page || 1} / ${pagination.total_pages || 1}`;

            const prevBtn = document.getElementById('ea-vd-prev');
            const nextBtn = document.getElementById('ea-vd-next');
            if (prevBtn) prevBtn.disabled = (pagination.page || 1) <= 1;
            if (nextBtn) nextBtn.disabled = (pagination.page || 1) >= (pagination.total_pages || 1);
        } catch {
            if (tbody) tbody.replaceChildren(_emptyRow(6, 'Error loading violations.'));
        }
    }

    async _eaShowRecordDetail(version, recordId, entityType) {
        const modal = document.getElementById('ea-modal');
        if (!modal) return;
        try {
            const params = entityType ? `?entity_type=${encodeURIComponent(entityType)}` : '';
            const resp = await this.authFetch(
                `/admin/api/extraction-analysis/${encodeURIComponent(version)}/violations/${encodeURIComponent(recordId)}${params}`,
            );
            if (!resp.ok) return;
            const data = await resp.json();

            const recordIdEl = document.getElementById('ea-modal-record-id');
            if (recordIdEl) recordIdEl.textContent = recordId;

            const xmlEl = document.getElementById('ea-modal-xml');
            if (xmlEl) xmlEl.textContent = data.raw_xml ? AdminDashboard._prettyPrintXml(data.raw_xml) : '(none)';

            const jsonEl = document.getElementById('ea-modal-json');
            if (jsonEl) {
                if (data.parsed_json && data.parsed_json._truncated) {
                    jsonEl.textContent = `${data.parsed_json._message}\n\nPreview:\n${data.parsed_json._preview || ''}`;
                } else {
                    jsonEl.textContent = data.parsed_json ? JSON.stringify(data.parsed_json, null, 2) : '(none)';
                }
            }

            const violList = document.getElementById('ea-modal-violations');
            if (violList) {
                const items = (data.violations || []).map(v => {
                    const li = document.createElement('li');
                    li.className = 'flex items-start gap-2';
                    const dot = document.createElement('span');
                    dot.className = `material-symbols-outlined text-xs ${v.severity === 'error' ? 'st-err' : 'st-warn'}`;
                    dot.textContent = 'circle';
                    const text = document.createElement('span');
                    text.className = 'text-xs t-mid';
                    text.textContent = `[${v.rule}] ${v.message || ''}`;
                    li.append(dot, text);
                    return li;
                });
                if (items.length === 0) {
                    const li = document.createElement('li');
                    li.className = 'text-xs t-muted';
                    li.textContent = 'No violations.';
                    violList.replaceChildren(li);
                } else {
                    violList.replaceChildren(...items);
                }
            }

            modal.style.display = 'flex';
        } catch {
            // Silently fail
        }
    }

    async _eaCompare() {
        const vA = document.getElementById('ea-cmp-version-a')?.value;
        const vB = document.getElementById('ea-cmp-version-b')?.value;
        if (!vA || !vB) {
            this.showToast('Select both versions to compare', 'error');
            return;
        }
        try {
            const resp = await this.authFetch(
                `/admin/api/extraction-analysis/${encodeURIComponent(vA)}/compare/${encodeURIComponent(vB)}`,
            );
            if (!resp.ok) {
                this.showToast('Comparison failed', 'error');
                return;
            }
            const data = await resp.json();
            const summary = document.getElementById('ea-cmp-summary');
            const tableWrap = document.getElementById('ea-cmp-table-wrap');
            const tbody = document.getElementById('ea-cmp-tbody');
            if (!summary || !tableWrap || !tbody) return;

            // The API (extraction_analysis.compare_versions) returns per-rule
            // deltas under `details` — not `delta` — and does not send
            // total_a/total_b directly; derive them by summing count_a/count_b
            // across all rules (retained regression coverage).
            const deltas = data.details || [];
            const totalA = deltas.reduce((sum, d) => sum + (d.count_a ?? 0), 0);
            const totalB = deltas.reduce((sum, d) => sum + (d.count_b ?? 0), 0);
            const delta = totalB - totalA;
            const sign = delta > 0 ? '+' : '';
            summary.textContent = `${vA}: ${totalA.toLocaleString()} violations → ${vB}: ${totalB.toLocaleString()} violations (${sign}${delta.toLocaleString()})`;
            summary.style.display = '';

            if (deltas.length === 0) {
                tbody.replaceChildren(_emptyRow(5, 'No differences found.'));
            } else {
                const rows = deltas.map(d => {
                    const tr = document.createElement('tr');
                    tr.className = 'border-b b-row';
                    const tdRule = document.createElement('td');
                    tdRule.className = 'py-2 px-2 mono t-mid';
                    tdRule.textContent = d.rule;
                    const tdEt = document.createElement('td');
                    tdEt.className = 'py-2 px-2 t-dim';
                    tdEt.textContent = d.entity_type || '—';
                    const tdA = document.createElement('td');
                    tdA.className = 'py-2 px-2 text-right mono t-mid';
                    tdA.textContent = (d.count_a ?? 0).toLocaleString();
                    const tdB = document.createElement('td');
                    tdB.className = 'py-2 px-2 text-right mono t-mid';
                    tdB.textContent = (d.count_b ?? 0).toLocaleString();
                    const tdDelta = document.createElement('td');
                    tdDelta.className = 'py-2 px-2 text-right mono font-bold';
                    const diff = (d.count_b ?? 0) - (d.count_a ?? 0);
                    tdDelta.textContent = (diff > 0 ? '+' : '') + diff.toLocaleString();
                    tdDelta.style.color = diff > 0 ? 'var(--text-high)' : diff < 0 ? 'var(--status-ok)' : 'var(--text-dim)';
                    tr.append(tdRule, tdEt, tdA, tdB, tdDelta);
                    return tr;
                });
                tbody.replaceChildren(...rows);
            }

            // Add skipped records delta below the table
            const skippedData = data.skipped || {};
            const skA = skippedData.version_a || {};
            const skB = skippedData.version_b || {};
            const allSkipEntities = new Set([...Object.keys(skA), ...Object.keys(skB)]);
            if (allSkipEntities.size > 0) {
                const skipRow = document.createElement('tr');
                skipRow.className = 'border-b b-row bg-inner';
                const tdLabel = document.createElement('td');
                tdLabel.className = 'py-2 px-2 t-dim italic';
                tdLabel.colSpan = 2;
                tdLabel.textContent = '⏭️ Skipped records';
                const totalSkA = Object.values(skA).reduce((a, b) => a + b, 0);
                const totalSkB = Object.values(skB).reduce((a, b) => a + b, 0);
                const tdA = document.createElement('td');
                tdA.className = 'py-2 px-2 text-right mono t-mid';
                tdA.textContent = totalSkA.toLocaleString();
                const tdB = document.createElement('td');
                tdB.className = 'py-2 px-2 text-right mono t-mid';
                tdB.textContent = totalSkB.toLocaleString();
                const tdD = document.createElement('td');
                tdD.className = 'py-2 px-2 text-right mono font-bold';
                const skipDiff = totalSkB - totalSkA;
                tdD.textContent = (skipDiff > 0 ? '+' : '') + skipDiff.toLocaleString();
                tdD.style.color = skipDiff > 0 ? 'var(--text-high)' : skipDiff < 0 ? 'var(--status-ok)' : 'var(--text-dim)';
                skipRow.append(tdLabel, tdA, tdB, tdD);
                tbody.appendChild(skipRow);
            }

            tableWrap.style.display = '';
        } catch {
            this.showToast('Comparison error', 'error');
        }
    }

    _eaToggleSelectAll(checked) {
        document.querySelectorAll('.ea-rule-chk').forEach(chk => {
            chk.checked = checked;
            const key = `${chk.dataset.rule}::${chk.dataset.entityType}`;
            if (checked) {
                this._eaSelectedRecords.set(key, { rule: chk.dataset.rule, entity_type: chk.dataset.entityType });
            } else {
                this._eaSelectedRecords.delete(key);
            }
        });
        this._eaUpdatePromptCount();
    }

    _eaUpdatePromptCount() {
        const el = document.getElementById('ea-prompt-count');
        if (el) el.textContent = `${this._eaSelectedRecords.size} rule${this._eaSelectedRecords.size === 1 ? '' : 's'} selected`;
    }

    async _eaGeneratePrompt() {
        const sel = document.getElementById('ea-version-select');
        const version = sel ? sel.value : '';
        if (!version) { this.showToast('Select a version first', 'error'); return; }
        if (this._eaSelectedRecords.size === 0) { this.showToast('Select at least one rule', 'error'); return; }

        const textarea = document.getElementById('ea-prompt-textarea');
        if (textarea) textarea.value = 'Generating…';

        try {
            const rules = Array.from(this._eaSelectedRecords.values());
            const resp = await this.authFetch(
                `/admin/api/extraction-analysis/${encodeURIComponent(version)}/prompt-context`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ rules }),
                },
            );
            if (!resp.ok) {
                if (textarea) textarea.value = 'Error generating prompt.';
                return;
            }
            const data = await resp.json();
            const lines = [
                `# Extraction Analysis — Version ${version}`,
                '',
                'You are debugging data quality violations from the GrooveMap extraction pipeline.',
                'For each rule below, analyze the sample records to determine:',
                '1. Root cause — why did the validation rule fire?',
                '2. Whether this is a data issue (upstream Discogs data) or a parser bug',
                '3. Suggested fix or workaround',
                '',
                '---',
                '',
            ];
            (data.contexts || []).forEach(ctx => {
                lines.push(`## Rule: ${ctx.rule} (${ctx.entity_type})`);
                lines.push(`- **Severity:** ${ctx.severity || 'unknown'}`);
                lines.push(`- **Total violations:** ${(ctx.total_violations || 0).toLocaleString()}`);
                if (ctx.sample_records && ctx.sample_records.length > 0) {
                    lines.push('');
                    ctx.sample_records.forEach((rec, i) => {
                        lines.push(`### Sample ${i + 1} — Record ID: ${rec.record_id || '?'}`);
                        lines.push('');
                        if (rec.violations && rec.violations.length > 0) {
                            lines.push('**Violations:**');
                            rec.violations.forEach(v => {
                                lines.push(`- \`${v.rule}\` on field \`${v.field || '(unknown)'}\` — value: \`${v.field_value || '(not captured)'}\``);
                            });
                            lines.push('');
                        }
                        if (rec.parsed_json) {
                            const jsonStr = typeof rec.parsed_json === 'string'
                                ? rec.parsed_json
                                : JSON.stringify(rec.parsed_json, null, 2);
                            // Limit JSON to ~2000 chars to keep prompt manageable
                            const trimmed = jsonStr.length > 2000
                                ? jsonStr.slice(0, 2000) + '\n... (truncated)'
                                : jsonStr;
                            lines.push('**Parsed JSON:**');
                            lines.push('```json');
                            lines.push(trimmed);
                            lines.push('```');
                            lines.push('');
                        }
                        if (rec.raw_xml) {
                            // Limit XML to ~2000 chars
                            const trimmed = rec.raw_xml.length > 2000
                                ? rec.raw_xml.slice(0, 2000) + '\n... (truncated)'
                                : rec.raw_xml;
                            lines.push('**Raw XML:**');
                            lines.push('```xml');
                            lines.push(trimmed);
                            lines.push('```');
                            lines.push('');
                        }
                    });
                }
                lines.push('---');
                lines.push('');
            });
            if (textarea) textarea.value = lines.join('\n');
        } catch {
            if (textarea) textarea.value = 'Error generating prompt.';
        }
    }

    async _eaGenerateAiPrompt() {
        const sel = document.getElementById('ea-version-select');
        const version = sel ? sel.value : '';
        if (!version) { this.showToast('Select a version first', 'error'); return; }
        if (this._eaSelectedRecords.size === 0) { this.showToast('Select at least one rule', 'error'); return; }

        const textarea = document.getElementById('ea-prompt-textarea');
        const aiBtn = document.getElementById('ea-ai-generate-btn');
        const spinner = document.getElementById('ea-ai-spinner');

        if (textarea) textarea.value = 'Analyzing violations with AI — this may take a moment...';
        if (aiBtn) aiBtn.disabled = true;
        if (spinner) spinner.style.display = '';

        try {
            const rules = Array.from(this._eaSelectedRecords.values());
            const resp = await this.authFetch(
                `/admin/api/extraction-analysis/${encodeURIComponent(version)}/generate-ai-prompt`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ rules }),
                },
            );
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                const detail = err.detail || 'AI prompt generation failed';
                if (textarea) textarea.value = `Error: ${detail}`;
                this.showToast(detail, 'error');
                return;
            }
            const data = await resp.json();
            if (textarea) textarea.value = data.prompt || 'No prompt generated.';
            this.showToast('AI analysis complete', 'success');
        } catch {
            if (textarea) textarea.value = 'Error: connection failed.';
            this.showToast('Connection error', 'error');
        } finally {
            if (aiBtn) aiBtn.disabled = false;
            if (spinner) spinner.style.display = 'none';
        }
    }

    async _eaCopyPrompt() {
        const textarea = document.getElementById('ea-prompt-textarea');
        if (!textarea || !textarea.value) { this.showToast('Nothing to copy', 'error'); return; }
        try {
            await navigator.clipboard.writeText(textarea.value);
            this.showToast('Prompt copied to clipboard', 'success');
        } catch {
            this.showToast('Copy failed — select all text manually', 'error');
        }
    }
}

class AdminLedger extends AdminContracts {

    // ─── Identity Ledger ─────────────────────────────────────────────────────

    // Read the first time the tab is shown, not at load: the strip and the list each cost a scan
    // at catalog scale, and a canvas drawn inside a hidden tab measures 0 x 0. Refresh re-reads.
    showLedger() {
        if (!this.state.ledgerLoaded) return this.loadLedger();
        if (this.state.ledgerChart) this.state.ledgerChart.resize();
        return Promise.resolve();
    }

    loadLedger() {
        this.state.ledgerLoaded = true;
        const reads = [this.fetchLedgerDensity(), this.fetchLedgerWorks()];
        if (this.state.ledgerWorkId) reads.push(this.fetchLedgerWork());
        return Promise.all(reads);
    }

    // Runs one ledger read, showing its spinner and error. A read superseded by a newer one of
    // the same kind (a quick filter change) is dropped rather than drawn over it. Until the
    // ledger's first refresh the list and the strip answer 503 "not yet refreshed": that is shown
    // as the ledger being built, and asked again after Retry-After, not as an error.
    async _ledgerRead(name, url, render) {
        const generation = (this.state[`${name}Generation`] || 0) + 1;
        this.state[`${name}Generation`] = generation;
        this._ledgerStopRetry(name);
        const loading = document.getElementById(`ledger-${name}-loading`);
        const error = document.getElementById(`ledger-${name}-error`);
        const errorMsg = document.getElementById(`ledger-${name}-error-msg`);
        if (loading) loading.style.display = '';
        if (error) error.style.display = 'none';
        try {
            const resp = await this.authFetch(url);
            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                if (name !== 'work' && resp.status === 503 && body.detail === LEDGER_NOT_REFRESHED) {
                    if (this.state[`${name}Generation`] === generation) this._ledgerBuilding(name, resp.headers?.get('Retry-After'));
                    return;
                }
                throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${resp.status}`);
            }
            const data = await resp.json();
            if (this.state[`${name}Generation`] === generation) {
                this._ledgerSetBuilding(name, false);
                render(data);
            }
        } catch (err) {
            if (this.state[`${name}Generation`] !== generation) return;
            this._ledgerSetBuilding(name, false);
            if (errorMsg) errorMsg.textContent = err.message;
            if (error) error.style.display = '';
        } finally {
            if (loading && this.state[`${name}Generation`] === generation) loading.style.display = 'none';
        }
    }

    // The ledger has no views to read yet: say so in place of the strip and the list, and read
    // again once catalog-api says to.
    _ledgerBuilding(name, retryAfter) {
        this._ledgerSetBuilding(name, true);
        if (name === 'density') {
            this.renderLedgerDensity({ buckets: [], refreshed_at: null }, { building: true });
        } else {
            this.renderLedgerWorks({ works: [], total: 0 }, { building: true });
        }
        const retry = name === 'density' ? () => this.fetchLedgerDensity() : () => this.fetchLedgerWorks();
        this.state.ledgerRetryTimers[name] = setTimeout(retry, _ledgerRetryDelay(retryAfter));
    }

    _ledgerSetBuilding(name, building) {
        this.state.ledgerBuilding[name] = building;
        const notice = document.getElementById('ledger-building');
        if (notice) notice.style.display = Object.values(this.state.ledgerBuilding).some(Boolean) ? '' : 'none';
    }

    _ledgerStopRetry(name) {
        clearTimeout(this.state.ledgerRetryTimers[name]);
        delete this.state.ledgerRetryTimers[name];
    }

    stopLedgerRetries() {
        Object.keys(this.state.ledgerRetryTimers).forEach(name => this._ledgerStopRetry(name));
        this.state.ledgerBuilding = {};
        this._ledgerSetBuilding('density', false);
        this._ledgerSetBuilding('works', false);
    }

    _ledgerShowFreshness(id, refreshedAt) {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = _ledgerFreshness(refreshedAt);
        el.title = refreshedAt || '';
    }

    fetchLedgerDensity() {
        const kind = document.getElementById('ledger-kind')?.value || 'release';
        return this._ledgerRead('density', `/admin/api/identity/ledger/density?kind=${encodeURIComponent(kind)}`, data => this.renderLedgerDensity(data));
    }

    _ledgerWorksUrl() {
        const params = new URLSearchParams({ page: String(this.state.ledgerPage), page_size: String(LEDGER_PAGE_SIZE) });
        const event = document.getElementById('ledger-event-filter')?.value;
        const since = document.getElementById('ledger-since')?.value;
        const until = document.getElementById('ledger-until')?.value;
        if (event) params.set('event', event);
        if (since) params.set('since', _ledgerDayStart(since));
        // Until names the last day included; catalog-api's `until` is exclusive.
        if (until) params.set('until', _ledgerDayStart(until, 1));
        return `/admin/api/identity/ledger/works?${params}`;
    }

    fetchLedgerWorks() {
        return this._ledgerRead('works', this._ledgerWorksUrl(), data => this.renderLedgerWorks(data));
    }

    openLedgerWork(workId) {
        this.state.ledgerWorkId = workId;
        const asOf = document.getElementById('ledger-as-of');
        if (asOf) asOf.value = '';
        const section = document.getElementById('ledger-work');
        if (section) {
            section.style.display = '';
            if (typeof section.scrollIntoView === 'function') section.scrollIntoView({ block: 'start' });
        }
        return this.fetchLedgerWork();
    }

    fetchLedgerWork() {
        const workId = this.state.ledgerWorkId;
        if (!workId) return Promise.resolve();
        const asOf = _ledgerAsOf(document.getElementById('ledger-as-of')?.value);
        const query = asOf ? `?${new URLSearchParams({ as_of: asOf })}` : '';
        return this._ledgerRead('work', `/admin/api/identity/ledger/works/${encodeURIComponent(workId)}${query}`, data => this.renderLedgerWork(data));
    }

    renderLedgerDensity(data, { building = false } = {}) {
        this._ledgerShowFreshness('ledger-density-asof', data?.refreshed_at);
        const series = _ledgerDensitySeries(data?.buckets);
        if (this.state.ledgerChart) {
            this.state.ledgerChart.destroy();
            this.state.ledgerChart = null;
        }

        const empty = document.getElementById('ledger-density-empty');
        if (empty) empty.style.display = series.months.length || building ? 'none' : '';

        const legend = document.getElementById('ledger-density-legend');
        if (legend) legend.replaceChildren(...series.datasets.map(dataset => this._ledgerLegendItem(dataset)));

        const note = document.getElementById('ledger-density-backfill');
        if (note) {
            const months = series.backfilledMonths;
            note.textContent = months.length
                ? `Hatched segments and highlighted months hold link changes ${LEDGER_BACKFILL_LABEL}, not recorded by GrooveMap: ${months.join(', ')}.`
                : '';
            note.style.display = months.length ? '' : 'none';
        }

        const canvas = document.getElementById('ledger-density-chart');
        if (!canvas || series.months.length === 0 || typeof Chart === 'undefined') return;
        const backfilled = new Set(series.backfilledMonths);
        const axes = chartAxisColors();
        const warn = themeVar('--status-warn');
        this._lastLedgerDensity = { data, building };
        this.state.ledgerChart = new Chart(canvas, {
            type: 'bar',
            data: {
                labels: series.months,
                datasets: series.datasets.map(dataset => {
                    const color = ledgerEventColor(dataset.type);
                    const isBackfill = dataset.datedFrom === LEDGER_BACKFILL;
                    return {
                        label: isBackfill ? `${_ledgerEventLabel(dataset.type)}, ${LEDGER_BACKFILL_LABEL}` : _ledgerEventLabel(dataset.type),
                        data: dataset.counts,
                        backgroundColor: isBackfill ? _ledgerHatch(color) : color,
                        borderColor: color,
                        borderWidth: isBackfill ? 1 : 0,
                        stack: 'events',
                        datedFrom: dataset.datedFrom,
                    };
                }),
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { enabled: true },
                },
                scales: {
                    x: {
                        stacked: true,
                        grid: { display: false },
                        ticks: {
                            color: context => (backfilled.has(series.months[context.index]) ? warn : axes.tick),
                            maxRotation: 0,
                            autoSkip: true,
                        },
                    },
                    y: {
                        stacked: true,
                        beginAtZero: true,
                        grid: { color: axes.grid },
                        ticks: { color: axes.tick, precision: 0 },
                    },
                },
            },
        });
    }

    _ledgerLegendItem(dataset) {
        const color = ledgerEventColor(dataset.type);
        const isBackfill = dataset.datedFrom === LEDGER_BACKFILL;
        const item = _el('span');
        item.dataset.datedFrom = dataset.datedFrom;
        const swatch = _el('span', isBackfill ? 'ledger-swatch ledger-swatch-backfill' : 'ledger-swatch');
        swatch.style.backgroundColor = isBackfill ? `${color}55` : color;
        swatch.style.borderColor = color;
        item.append(swatch, _el('span', null, isBackfill ? `${_ledgerEventLabel(dataset.type)}, ${LEDGER_BACKFILL_LABEL}` : _ledgerEventLabel(dataset.type)));
        return item;
    }

    renderLedgerWorks(data, { building = false } = {}) {
        this._ledgerShowFreshness('ledger-works-asof', data.refreshed_at);
        const tbody = document.getElementById('ledger-works-body');
        if (!tbody) return;
        const works = data.works || [];
        if (building) {
            tbody.replaceChildren(_emptyRow(8, 'No works to list until the ledger is built'));
        } else if (works.length === 0) {
            tbody.replaceChildren(_emptyRow(8, 'No works carry identity events for these filters'));
        } else {
            tbody.replaceChildren(...works.map(work => this._ledgerWorkRow(work)));
        }

        const pagination = document.getElementById('ledger-pagination');
        const pageInfo = document.getElementById('ledger-page-info');
        const prevBtn = document.getElementById('ledger-prev-btn');
        const nextBtn = document.getElementById('ledger-next-btn');
        if (pagination && data.total > 0) {
            pagination.style.display = '';
            const totalPages = Math.ceil(data.total / data.page_size);
            if (pageInfo) pageInfo.textContent = `Page ${data.page} of ${totalPages} (${data.total} works)`;
            if (prevBtn) prevBtn.disabled = data.page <= 1;
            if (nextBtn) nextBtn.disabled = data.page >= totalPages;
        } else if (pagination) {
            pagination.style.display = 'none';
        }
    }

    _ledgerWorkRow(work) {
        const tr = _el('tr', 'border-b b-theme hover:bg-hover transition-colors');
        tr.dataset.work = work.gm_id;

        const tdWork = _el('td', 'py-2 px-3');
        const link = _el('button', 'ledger-work-link ledger-id', work.gm_id);
        link.type = 'button';
        link.addEventListener('click', () => this.openLedgerWork(work.gm_id));
        tdWork.appendChild(link);

        const tdProviders = _el('td', 'py-2 px-3 t-muted');
        const providers = work.providers || [];
        if (providers.length === 0) tdProviders.textContent = '—';
        providers.forEach(provider => tdProviders.appendChild(_el('div', 'whitespace-nowrap', `${provider.provider} ${provider.external_id}`)));

        const counts = [work.editions, work.evented_editions, work.repaired_editions, work.work_disagreements]
            .map(count => _el('td', 'py-2 px-3 text-right mono', count ?? 0));

        tr.append(
            tdWork,
            tdProviders,
            ...counts,
            _el('td', 'py-2 px-3 t-muted whitespace-nowrap', _ledgerTime(work.first_event)),
            _el('td', 'py-2 px-3 t-muted whitespace-nowrap', _ledgerTime(work.last_event)),
        );
        return tr;
    }

    renderLedgerWork(data) {
        const work = data.work || {};
        const editions = data.editions || [];
        const idEl = document.getElementById('ledger-work-id');
        if (idEl) idEl.textContent = work.gm_id || '';

        this._ledgerShowFreshness('ledger-work-asof', data.refreshed_at);
        const status = document.getElementById('ledger-work-status');
        if (status) {
            const parts = [`Read at ${data.as_of ? _ledgerTime(data.as_of) : 'now'}`];
            parts.push(`${editions.length} of ${data.edition_count ?? editions.length} editions${data.truncated ? ' (truncated)' : ''}`);
            if (work.requested_id) parts.push(`${work.requested_id} was superseded into this work`);
            status.textContent = parts.join(' · ');
        }

        const summary = document.getElementById('ledger-work-summary');
        if (summary) {
            const providers = (work.providers || []).map(provider =>
                `${provider.provider} ${provider.external_id} from ${_ledgerTime(provider.valid_from)}${provider.valid_to ? ` to ${_ledgerTime(provider.valid_to)}` : ''}`);
            const fields = this._ledgerFields([
                ['State', this._ledgerStateBadge(work.state)],
                ['Providers', providers.length ? providers.join('; ') : '—'],
            ]);
            summary.replaceChildren(fields, this._ledgerEvents(work.events, 'Work events'));
        }

        const container = document.getElementById('ledger-editions');
        if (container) {
            if (editions.length === 0) {
                container.replaceChildren(_el('p', 'text-xs t-muted', 'No editions are recorded for this work at this time.'));
            } else {
                container.replaceChildren(...editions.map(edition => this._ledgerEdition(edition, work.gm_id)));
            }
        }
    }

    _ledgerEdition(edition, workId) {
        const article = _el('article', 'ledger-edition');
        article.dataset.edition = edition.gm_id;

        const header = _el('div', 'flex flex-wrap items-center gap-2');
        header.append(_el('span', 'ledger-id text-xs font-semibold', edition.gm_id), this._ledgerStateBadge(edition.state));
        if (edition.work_disagreement) {
            const badge = _el('span', 'ledger-badge badge-running ledger-disagreement', 'Work disagreement');
            badge.title = 'Its Discogs master and MusicBrainz release group resolve to different works; the Discogs side is primary.';
            header.appendChild(badge);
        }

        const primary = edition.primary_work
            ? this._ledgerWorkRef(edition.primary_work, workId)
            : '—';
        const others = (edition.works || []).filter(work => work !== edition.primary_work);
        const otherWorks = others.length ? _el('span') : '—';
        if (others.length) others.forEach((work, i) => otherWorks.append(...(i ? [', '] : []), this._ledgerWorkRef(work, workId)));

        const rows = (edition.provider_rows || []).map(row =>
            `${row.provider} ${row.external_id}${row.names_discogs_release ? ` (names Discogs ${row.names_discogs_release})` : ''}`);

        const formerItems = edition.former_items || [];
        let former = '—';
        if (formerItems.length) {
            former = _el('ul', 'ledger-former');
            formerItems.forEach(item => {
                const li = _el('li');
                li.append(_el('span', 'ledger-id', item.gm_id), ` superseded ${_ledgerTime(item.superseded_at)} (${item.cause})`);
                former.appendChild(li);
            });
        }

        article.append(
            header,
            this._ledgerFields([
                ['Primary work', primary],
                ['Other works', otherWorks],
                ['Provider rows', rows.length ? rows.join('; ') : '—'],
                ['Former items', former],
            ]),
            this._ledgerEvents(edition.events, 'Events'),
        );
        return article;
    }

    // A work id; one other than the open work opens it.
    _ledgerWorkRef(gmId, workId) {
        if (gmId === workId) return _el('span', 'ledger-id', `${gmId} (this work)`);
        const link = _el('button', 'ledger-work-link ledger-id', gmId);
        link.type = 'button';
        link.addEventListener('click', () => this.openLedgerWork(gmId));
        return link;
    }

    _ledgerStateBadge(state) {
        const badge = _el('span', 'ledger-badge', state || 'unknown');
        badge.dataset.state = LEDGER_STATES.has(state) ? state : 'not_recorded';
        return badge;
    }

    _ledgerFields(entries) {
        const dl = _el('dl', 'ledger-fields');
        entries.forEach(([term, value]) => {
            const dd = _el('dd');
            dd.append(value);
            dl.append(_el('dt', null, term), dd);
        });
        return dl;
    }

    _ledgerEvents(events, title) {
        const wrap = _el('div');
        wrap.appendChild(_el('h4', 'text-[10px] font-bold uppercase tracking-wider t-muted mt-2', title));
        if (!events || events.length === 0) {
            wrap.appendChild(_el('p', 'text-xs t-muted', 'No events at this time.'));
            return wrap;
        }
        const list = _el('ol', 'ledger-events');
        events.forEach(event => list.appendChild(this._ledgerEventItem(event)));
        wrap.appendChild(list);
        return wrap;
    }

    _ledgerEventItem(event) {
        const li = _el('li');
        li.dataset.type = event.type;
        li.dataset.datedFrom = event.dated_from;
        const type = _el('span', 'ledger-badge ledger-chip', _ledgerEventLabel(event.type));
        if (event.rule) type.title = event.rule;
        const provenance = event.dated_from === LEDGER_BACKFILL
            ? _el('span', 'ledger-backfill', LEDGER_BACKFILL_LABEL)
            : _el('span', 'ledger-recorded', event.dated_from === 'groovemap' ? 'recorded by GrooveMap' : event.dated_from);
        li.append(
            _el('span', 'mono t-dim', _ledgerTime(event.at)),
            type,
            _el('span', 'ledger-detail', this._ledgerEventDetail(event)),
            provenance,
        );
        return li;
    }

    _ledgerEventDetail(event) {
        switch (event.type) {
        case 'minted':
            return `${event.item}${event.providers?.length ? ` from ${event.providers.join(' and ')}` : ''}`;
        case 'linked_at_ingest':
            return event.item;
        case 'evidence_attached':
            return `${event.provider} alias from ${event.source}`;
        case 'alias_closed':
            return `${event.provider} alias closed`;
        case 'reattached':
        case 'promoted': {
            const moves = Object.entries(event.moves || {}).map(([table, count]) => `${table} ${count}`);
            return `${event.superseded} into ${event.survivor}${moves.length ? `; moved ${moves.join(', ')}` : ''}${event.compressed ? '; compressed' : ''}`;
        }
        case 'reverted':
            return `${event.superseded} split back out of ${event.survivor}`;
        case 'link_added':
        case 'link_changed':
        case 'link_removed':
            return `${event.provider} ${event.external_id}: Discogs ${event.discogs_id_before ?? 'none'} to ${event.discogs_id_after ?? 'none'}`;
        default:
            return '';
        }
    }
}

class AdminDashboard extends AdminLedger {}

document.addEventListener('DOMContentLoaded', () => {
    window.adminDashboard = new AdminDashboard();
});

// Canvas charts cannot follow CSS variables, so a theme change redraws each chart that is showing
// from the data it was built with.
document.addEventListener('groovemap:themechange', () => {
    const app = window.adminDashboard;
    if (!app) return;
    if (app.queueDepthChart && app._lastQueueHistory) app.renderQueueDepthChart(app._lastQueueHistory);
    if (app.responseTimeChart && app._lastResponseTimeData) app.renderResponseTimeChart(app._lastResponseTimeData);
    if (app.state.ledgerChart && app._lastLedgerDensity) app.renderLedgerDensity(app._lastLedgerDensity.data, { building: app._lastLedgerDensity.building });
});
