import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * The console shell's session boundary in the browser: admin.js owns the session and shows
 * either the login form or the console; dashboard.js reads nothing until it is told a session
 * is live, trades the bearer for a single-use WebSocket ticket, and hands any refusal back to
 * admin.js instead of retrying blind.
 */
const here = dirname(fileURLToPath(import.meta.url));
const stripAutoInit = (code) => code.replace(/document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/, '');
const adminSource = stripAutoInit(readFileSync(resolve(here, '../static/admin.js'), 'utf8'));
const dashboardSource = stripAutoInit(readFileSync(resolve(here, '../static/dashboard.js'), 'utf8'));
const shellBody = readFileSync(resolve(here, '../static/index.html'), 'utf8').match(/<body[^>]*>([\s\S]*)<\/body>/)[1];

function loadClasses() {
    vm.runInThisContext(`(function() { ${adminSource}; globalThis.__ConsoleAdmin = AdminDashboard; })();`);
    vm.runInThisContext(`(function() { ${dashboardSource}; globalThis.__ConsoleDashboard = Dashboard; })();`);
    return { AdminDashboard: globalThis.__ConsoleAdmin, Dashboard: globalThis.__ConsoleDashboard };
}

function jsonResponse(status, body) {
    return { ok: status >= 200 && status < 300, status, json: async () => body };
}

const LIVE_SESSION = { admin_id: 'admin-1', email: 'ops@example.com', expires_at: new Date(Date.now() + 3600_000).toISOString() };

// Route fetches by URL; the session answer can change mid-test.
function installFetch(routes) {
    const fetchMock = vi.fn(async (url) => {
        const path = new URL(url, 'http://console.test').pathname;
        const handler = routes[path];
        if (!handler) return jsonResponse(404, {});
        return handler();
    });
    globalThis.fetch = fetchMock;
    return fetchMock;
}

const visible = (id) => document.getElementById(id).style.display !== 'none';
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static instances = [];
    constructor(url) {
        this.url = url;
        this.readyState = FakeWebSocket.CONNECTING;
        this.closedWith = null;
        FakeWebSocket.instances.push(this);
    }
    close(code) {
        this.closedWith = code ?? 1005;
        this.readyState = 3;
    }
    serverClose(code) {
        this.readyState = 3;
        this.onclose?.({ code });
    }
}

// Each test builds fresh instances that listen on the one jsdom document; remove every listener
// a test added so an earlier test's instances cannot answer a later test's events.
const addedListeners = [];
const addListener = document.addEventListener.bind(document);
document.addEventListener = (type, listener, options) => {
    addedListeners.push([type, listener, options]);
    addListener(type, listener, options);
};

let classes;
let events;

beforeEach(() => {
    document.body.innerHTML = shellBody;
    localStorage.clear();
    FakeWebSocket.instances = [];
    globalThis.WebSocket = FakeWebSocket;
    events = [];
    for (const name of ['console:authenticated', 'console:signed-out', 'console:session-check']) {
        document.addEventListener(name, (event) => events.push({ name, detail: event.detail }));
    }
    classes = loadClasses();
});

afterEach(() => {
    vi.useRealTimers();
    for (const [type, listener, options] of addedListeners.splice(0)) {
        document.removeEventListener(type, listener, options);
    }
});

describe('admin.js owns the session', () => {
    it('shows only the login form and reads nothing without a stored bearer', () => {
        const fetchMock = installFetch({});
        new classes.AdminDashboard();
        expect(visible('login-view')).toBe(true);
        expect(visible('console-view')).toBe(false);
        expect(document.getElementById('login-reason').style.display).toBe('none');
        expect(fetchMock).not.toHaveBeenCalled();
    });

    it('opens the console on the Overview tab once catalog-api confirms the session', async () => {
        localStorage.setItem('admin_token', 'live-token');
        const fetchMock = installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/api/queue-prefixes': () => jsonResponse(200, { discogs: 'groovemap-discogs', musicbrainz: 'groovemap-musicbrainz' }),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
        });
        new classes.AdminDashboard();
        await flush();

        expect(fetchMock.mock.calls[0][0]).toBe('/api/session');
        expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer live-token');
        expect(visible('console-view')).toBe(true);
        expect(visible('login-view')).toBe(false);
        expect(visible('tab-overview')).toBe(true);
        expect(visible('tab-extractions')).toBe(false);
        const tabs = [...document.querySelectorAll('.tab-btn')].map((btn) => btn.dataset.tab);
        expect(tabs[0]).toBe('overview');
        expect(tabs.slice(1)).toEqual(['extractions', 'dlq', 'users', 'storage', 'queue-trends', 'system-health', 'audit-log', 'extraction-analysis']);
        expect(document.querySelector('.tab-btn.active').dataset.tab).toBe('overview');
        expect(document.getElementById('admin-email').textContent).toBe('ops@example.com');
        expect(events).toContainEqual({ name: 'console:authenticated', detail: { token: 'live-token' } });
    });

    it.each([
        [401, { reason: 'invalid' }, 'Your session expired or was revoked. Sign in again.', null],
        [503, { reason: 'unavailable' }, 'catalog-api is unreachable, so your session could not be verified. Sign in again once it is back.', 'stale-token'],
    ])('returns a stored session answered %i to the login form with its reason', async (status, body, message, kept) => {
        localStorage.setItem('admin_token', 'stale-token');
        installFetch({ '/api/session': () => jsonResponse(status, body) });
        new classes.AdminDashboard();
        await flush();

        expect(visible('login-view')).toBe(true);
        expect(visible('console-view')).toBe(false);
        expect(document.getElementById('login-reason').textContent).toBe(message);
        expect(document.getElementById('login-reason').style.display).toBe('block');
        expect(localStorage.getItem('admin_token')).toBe(kept);
    });

    it('reports an unreachable console as its own reason', async () => {
        localStorage.setItem('admin_token', 'token');
        globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
        new classes.AdminDashboard();
        await flush();
        expect(document.getElementById('login-reason').dataset.reason).toBe('console-unreachable');
    });

    it('returns to the login form with a reason when the session expires mid-use', async () => {
        vi.useFakeTimers();
        localStorage.setItem('admin_token', 'live-token');
        let sessionStatus = 200;
        installFetch({
            '/api/session': () => (sessionStatus === 200 ? jsonResponse(200, LIVE_SESSION) : jsonResponse(401, { reason: 'invalid' })),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
        });
        new classes.AdminDashboard();
        await flush();
        expect(visible('console-view')).toBe(true);

        sessionStatus = 401;
        await vi.advanceTimersByTimeAsync(15000);

        expect(visible('login-view')).toBe(true);
        expect(document.getElementById('login-reason').dataset.reason).toBe('invalid');
        expect(events.map((event) => event.name)).toContain('console:signed-out');
    });

    it('re-checks the session just after its own expiry', async () => {
        vi.useFakeTimers();
        localStorage.setItem('admin_token', 'short-token');
        const expiresAt = new Date(Date.now() + 5000).toISOString();
        let expired = false;
        installFetch({
            '/api/session': () => (expired ? jsonResponse(401, { reason: 'invalid' }) : jsonResponse(200, { ...LIVE_SESSION, expires_at: expiresAt })),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
        });
        new classes.AdminDashboard();
        await flush();
        expired = true;

        await vi.advanceTimersByTimeAsync(6100);

        expect(visible('login-view')).toBe(true);
        expect(document.getElementById('login-reason').dataset.reason).toBe('invalid');
    });

    it('signs out with a visible reason and forgets the bearer', async () => {
        localStorage.setItem('admin_token', 'live-token');
        const fetchMock = installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
            '/admin/api/logout': () => jsonResponse(200, { logged_out: true }),
        });
        const app = new classes.AdminDashboard();
        await flush();

        await app.logout();

        expect(fetchMock.mock.calls.some(([url]) => url === '/admin/api/logout')).toBe(true);
        expect(visible('login-view')).toBe(true);
        expect(document.getElementById('login-reason').textContent).toBe('You signed out.');
        expect(localStorage.getItem('admin_token')).toBeNull();
        expect(events.map((event) => event.name)).toContain('console:signed-out');
    });

    it('treats a 401 from any admin call as an ended session', async () => {
        localStorage.setItem('admin_token', 'live-token');
        installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
            '/admin/api/storage': () => jsonResponse(401, { detail: 'Invalid or expired token' }),
        });
        const app = new classes.AdminDashboard();
        await flush();

        await expect(app.authFetch('/admin/api/storage')).rejects.toThrow('Session expired');
        expect(visible('login-view')).toBe(true);
        expect(document.getElementById('login-reason').dataset.reason).toBe('invalid');
    });

    it('answers a session check from the live view by re-announcing a live session', async () => {
        localStorage.setItem('admin_token', 'live-token');
        installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
        });
        new classes.AdminDashboard();
        await flush();
        events.length = 0;

        document.dispatchEvent(new CustomEvent('console:session-check'));
        await flush();

        expect(events.map((event) => event.name)).toEqual(['console:session-check', 'console:authenticated']);
    });
});

describe('dashboard.js reads only inside a session', () => {
    function startedDashboard(routes) {
        const fetchMock = installFetch(routes);
        const dashboard = new classes.Dashboard();
        return { dashboard, fetchMock };
    }

    it('reads nothing until a session is announced', () => {
        const { fetchMock } = startedDashboard({});
        expect(fetchMock).not.toHaveBeenCalled();
        expect(FakeWebSocket.instances).toHaveLength(0);
    });

    it('trades the bearer for a ticket and opens the socket with it', async () => {
        const { fetchMock } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [], timestamp: new Date().toISOString() }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: 'single-use', expires_in: 30 }),
        });
        document.dispatchEvent(new CustomEvent('console:authenticated', { detail: { token: 'live-token' } }));
        await flush();

        const ticketCall = fetchMock.mock.calls.find(([url]) => url === '/api/ws-ticket');
        expect(ticketCall[1]).toMatchObject({ method: 'POST', headers: { Authorization: 'Bearer live-token' } });
        const metricsCall = fetchMock.mock.calls.find(([url]) => url === '/api/metrics');
        expect(metricsCall[1].headers.Authorization).toBe('Bearer live-token');
        expect(FakeWebSocket.instances).toHaveLength(1);
        expect(FakeWebSocket.instances[0].url).toMatch(/\/ws\?ticket=single-use$/);
    });

    it('hands a 4401 close back to admin.js instead of reconnecting', async () => {
        vi.useFakeTimers();
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: 't1' }),
        });
        dashboard.start('live-token');
        await vi.advanceTimersByTimeAsync(0);

        FakeWebSocket.instances[0].serverClose(4401);
        await vi.advanceTimersByTimeAsync(10000);

        expect(events.map((event) => event.name)).toContain('console:session-check');
        expect(FakeWebSocket.instances).toHaveLength(1);
        expect(dashboard.reconnectInterval).toBeNull();
    });

    it('reconnects with a fresh ticket after an ordinary drop', async () => {
        vi.useFakeTimers();
        let issued = 0;
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: `t${++issued}` }),
        });
        dashboard.start('live-token');
        await vi.advanceTimersByTimeAsync(0);

        FakeWebSocket.instances[0].serverClose(1006);
        await vi.advanceTimersByTimeAsync(5000);

        expect(FakeWebSocket.instances).toHaveLength(2);
        expect(FakeWebSocket.instances[1].url).toMatch(/ticket=t2$/);
        expect(events.map((event) => event.name)).not.toContain('console:session-check');
    });

    it.each([401, 503])('asks for a session check when the ticket request is refused with %i', async (status) => {
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(status, { reason: 'invalid' }),
        });
        dashboard.start('stale-token');
        await flush();

        expect(FakeWebSocket.instances).toHaveLength(0);
        expect(events.map((event) => event.name)).toContain('console:session-check');
    });

    it('closes the socket and stays quiet once signed out', async () => {
        vi.useFakeTimers();
        const { dashboard, fetchMock } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: 't1' }),
        });
        dashboard.start('live-token');
        await vi.advanceTimersByTimeAsync(0);
        const socket = FakeWebSocket.instances[0];
        const callsBefore = fetchMock.mock.calls.length;

        document.dispatchEvent(new CustomEvent('console:signed-out'));
        socket.serverClose(1000);
        await vi.advanceTimersByTimeAsync(10000);

        expect(socket.closedWith).toBe(1000);
        expect(FakeWebSocket.instances).toHaveLength(1);
        expect(fetchMock.mock.calls.length).toBe(callsBefore);
        expect(document.querySelector('.status-text').textContent).toBe('Disconnected');
    });
});
