import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * The console shell's session boundary in the browser: admin.js owns the session and shows
 * either the login form or the console; dashboard.js reads nothing until it is told a session
 * is live, trades the cookie session for a single-use WebSocket ticket, and hands any refusal
 * back to admin.js instead of retrying blind. The bearer itself is an HttpOnly cookie neither
 * script can see, so nothing here ever stores or sends it.
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
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const CONSOLE_HEADER = { 'X-GrooveMap-Console': '1' };
const NO_SESSION = () => jsonResponse(401, { reason: 'missing' });

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
    serverOpen() {
        this.readyState = FakeWebSocket.OPEN;
        this.onopen?.();
    }
    serverMessage(data) {
        this.onmessage?.({ data: JSON.stringify(data) });
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
    it('asks the console once and shows only the login form, without a reason, when there is no session', async () => {
        const fetchMock = installFetch({ '/api/session': NO_SESSION });
        new classes.AdminDashboard();
        await flush();
        expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/session']);
        expect(visible('login-view')).toBe(true);
        expect(visible('console-view')).toBe(false);
        expect(document.getElementById('login-reason').style.display).toBe('none');
    });

    it('opens the console on the Overview tab once catalog-api confirms the session', async () => {
        const fetchMock = installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/api/queue-prefixes': () => jsonResponse(200, { discogs: 'groovemap-discogs', musicbrainz: 'groovemap-musicbrainz' }),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
        });
        new classes.AdminDashboard();
        await flush();

        expect(fetchMock.mock.calls[0][0]).toBe('/api/session');
        expect(fetchMock.mock.calls[0][1].headers).toEqual(CONSOLE_HEADER);
        expect(visible('console-view')).toBe(true);
        expect(visible('login-view')).toBe(false);
        expect(visible('tab-overview')).toBe(true);
        expect(visible('tab-extractions')).toBe(false);
        const tabs = [...document.querySelectorAll('.tab-btn')].map((btn) => btn.dataset.tab);
        expect(tabs[0]).toBe('overview');
        expect(tabs.slice(1)).toEqual(['extractions', 'dlq', 'users', 'storage', 'queue-trends', 'system-health', 'audit-log', 'extraction-analysis', 'ledger']);
        expect(document.querySelector('.tab-btn.active').dataset.tab).toBe('overview');
        expect(document.getElementById('admin-email').textContent).toBe('ops@example.com');
        expect(events).toContainEqual({ name: 'console:authenticated', detail: { session: 1 } });
        expect(localStorage.length).toBe(0);
    });

    it.each([
        [401, { reason: 'invalid' }, 'Your session expired or was revoked. Sign in again.'],
        [503, { reason: 'unavailable' }, 'catalog-api is unreachable, so your session could not be verified. Sign in again once it is back.'],
    ])('returns a cookie session answered %i to the login form with its reason', async (status, body, message) => {
        installFetch({ '/api/session': () => jsonResponse(status, body) });
        new classes.AdminDashboard();
        await flush();

        expect(visible('login-view')).toBe(true);
        expect(visible('console-view')).toBe(false);
        expect(document.getElementById('login-reason').textContent).toBe(message);
        expect(document.getElementById('login-reason').style.display).toBe('block');
    });

    it('reports an unreachable console as its own reason', async () => {
        globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
        new classes.AdminDashboard();
        await flush();
        expect(document.getElementById('login-reason').dataset.reason).toBe('console-unreachable');
    });

    it('returns to the login form with a reason when the session expires mid-use', async () => {
        vi.useFakeTimers();
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

    it.each([
        [200, 'You signed out.'],
        [204, 'You signed out.'],
        [401, 'You signed out.'],
        [502, 'You are signed out of this browser, but catalog-api did not confirm the sign-out, so the session may still be valid until it expires.'],
        [503, 'You are signed out of this browser, but catalog-api did not confirm the sign-out, so the session may still be valid until it expires.'],
        [null, 'You are signed out of this browser, but catalog-api did not confirm the sign-out, so the session may still be valid until it expires.'],
    ])('reports a logout answered %s honestly and ends the session either way', async (status, message) => {
        const fetchMock = installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
            '/admin/api/logout': () => {
                if (status === null) throw new TypeError('Failed to fetch');
                return jsonResponse(status, {});
            },
        });
        const app = new classes.AdminDashboard();
        await flush();

        await app.logout();

        const logoutCall = fetchMock.mock.calls.find(([url]) => url === '/admin/api/logout');
        expect(logoutCall[1]).toEqual({ method: 'POST', headers: CONSOLE_HEADER });
        expect(visible('login-view')).toBe(true);
        expect(document.getElementById('login-reason').textContent).toBe(message);
        expect(events.map((event) => event.name)).toContain('console:signed-out');
        // Any answer means the console cleared the cookie; with none, the next load signs out again.
        expect(localStorage.getItem('console_signout_pending')).toBe(status === null ? '1' : null);
    });

    it('finishes a sign-out that never reached the console before resuming anything', async () => {
        localStorage.setItem('console_signout_pending', '1');
        const fetchMock = installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/admin/api/logout': () => jsonResponse(200, {}),
        });
        new classes.AdminDashboard();
        await flush();

        expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/admin/api/logout']);
        expect(visible('login-view')).toBe(true);
        expect(document.getElementById('login-reason').textContent).toBe('You signed out.');
        expect(localStorage.getItem('console_signout_pending')).toBeNull();
    });

    it('signs out and removes a bearer an older console left in localStorage', async () => {
        localStorage.setItem('admin_token', 'legacy-token');
        localStorage.setItem('admin_email', 'ops@example.com');
        const fetchMock = installFetch({ '/api/session': NO_SESSION, '/admin/api/logout': () => jsonResponse(200, {}) });
        new classes.AdminDashboard();
        expect(localStorage.getItem('admin_token')).toBeNull();
        await flush();

        expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/admin/api/logout', '/api/session']);
        expect(fetchMock.mock.calls[0][1].headers).toEqual({ Authorization: 'Bearer legacy-token' });
        expect(localStorage.length).toBe(0);
        expect(visible('login-view')).toBe(true);
        expect(document.getElementById('login-reason').style.display).toBe('none');
    });

    it('signs in without keeping anything in script-readable storage', async () => {
        let signedIn = false;
        const fetchMock = installFetch({
            '/api/session': () => (signedIn ? jsonResponse(200, LIVE_SESSION) : NO_SESSION()),
            '/admin/api/login': () => { signedIn = true; return jsonResponse(200, { expires_in: 3600 }); },
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
        });
        const app = new classes.AdminDashboard();
        await flush();
        document.getElementById('login-email').value = 'ops@example.com';
        document.getElementById('login-password').value = 'secret';

        await app.login();
        await flush();

        const loginCall = fetchMock.mock.calls.find(([url]) => url === '/admin/api/login');
        expect(loginCall[1].headers).toEqual({ ...CONSOLE_HEADER, 'Content-Type': 'application/json' });
        expect(visible('console-view')).toBe(true);
        expect(localStorage.length).toBe(0);
        expect(events).toContainEqual({ name: 'console:authenticated', detail: { session: 2 } });
    });

    it('reports a mid-use check that finds no cookie at all as an ended session', async () => {
        vi.useFakeTimers();
        let sessionStatus = 200;
        installFetch({
            '/api/session': () => (sessionStatus === 200 ? jsonResponse(200, LIVE_SESSION) : NO_SESSION()),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
        });
        new classes.AdminDashboard();
        await flush();

        sessionStatus = 401;  // the browser dropped the cookie at its Max-Age, or the console cleared it
        await vi.advanceTimersByTimeAsync(15000);

        expect(visible('login-view')).toBe(true);
        expect(document.getElementById('login-reason').dataset.reason).toBe('invalid');
    });

    it('keeps the sign-out reason when an admin call answers 401 after it', async () => {
        let answerStorage;
        installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
            '/admin/api/logout': () => jsonResponse(200, {}),
            '/admin/api/storage': () => new Promise((resolve) => { answerStorage = resolve; }),
        });
        const app = new classes.AdminDashboard();
        await flush();

        const inFlight = app.authFetch('/admin/api/storage');
        await app.logout();
        answerStorage(jsonResponse(401, { detail: 'Invalid or expired token' }));

        await expect(inFlight).rejects.toThrow('Session expired');
        expect(document.getElementById('login-reason').textContent).toBe('You signed out.');
    });

    // The revocation makes catalog-api refuse a read still in flight; whichever answer lands
    // first, the user asked to sign out and must never be told the session expired.
    it.each([
        ['before', 'authFetch'],
        ['before', 'verifySession'],
        ['after', 'authFetch'],
        ['after', 'verifySession'],
    ])('reports a sign-out as "You signed out." when the refused read lands %s the logout answer (%s)', async (order, read) => {
        let answerRead;
        let answerLogout;
        let sessionCalls = 0;
        installFetch({
            '/api/session': () => {
                sessionCalls += 1;
                if (read === 'verifySession' && sessionCalls > 1) return new Promise((resolve) => { answerRead = resolve; });
                return jsonResponse(200, LIVE_SESSION);
            },
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
            '/admin/api/logout': () => new Promise((resolve) => { answerLogout = resolve; }),
            '/admin/api/storage': () => new Promise((resolve) => { answerRead = resolve; }),
        });
        const app = new classes.AdminDashboard();
        await flush();
        const reasons = [];
        const showLogin = app.showLogin.bind(app);
        app.showLogin = (reason) => { reasons.push(reason); showLogin(reason); };
        events.length = 0;

        const inFlight = read === 'authFetch' ? app.authFetch('/admin/api/storage') : app.verifySession();
        const settled = inFlight.then(() => null, (error) => error);
        const loggingOut = app.logout();
        await flush();
        const refuse = () => answerRead(read === 'authFetch'
            ? jsonResponse(401, { detail: 'Invalid or expired token' })
            : NO_SESSION());
        const confirm = () => answerLogout(jsonResponse(200, {}));
        if (order === 'before') { refuse(); await flush(); confirm(); } else { confirm(); await flush(); refuse(); }
        await loggingOut;
        await settled;

        expect(reasons).toEqual(['signed-out']);
        expect(document.getElementById('login-reason').textContent).toBe('You signed out.');
        expect(document.getElementById('login-reason').dataset.reason).toBe('signed-out');
        expect(events.filter((event) => event.name === 'console:signed-out')).toHaveLength(1);
    });

    it('still reports an unconfirmed sign-out when a read is refused during it', async () => {
        let answerStorage;
        installFetch({
            '/api/session': () => jsonResponse(200, LIVE_SESSION),
            '/admin/api/extractions': () => jsonResponse(200, { extractions: [] }),
            '/admin/api/logout': () => { throw new TypeError('Failed to fetch'); },
            '/admin/api/storage': () => new Promise((resolve) => { answerStorage = resolve; }),
        });
        const app = new classes.AdminDashboard();
        await flush();

        const settled = app.authFetch('/admin/api/storage').catch((error) => error);
        const loggingOut = app.logout();
        answerStorage(jsonResponse(401, { detail: 'Invalid or expired token' }));
        await loggingOut;
        await settled;

        expect(document.getElementById('login-reason').dataset.reason).toBe('signed-out-unconfirmed');
    });

    it('treats a 401 from any admin call as an ended session', async () => {
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

    it('trades the cookie session for a ticket and opens the socket with it', async () => {
        const { fetchMock } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [], timestamp: new Date().toISOString() }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: 'single-use', expires_in: 30 }),
        });
        document.dispatchEvent(new CustomEvent('console:authenticated', { detail: { session: 1 } }));
        await flush();

        const ticketCall = fetchMock.mock.calls.find(([url]) => url === '/api/ws-ticket');
        expect(ticketCall[1]).toMatchObject({ method: 'POST', headers: CONSOLE_HEADER });
        const metricsCall = fetchMock.mock.calls.find(([url]) => url === '/api/metrics');
        expect(metricsCall[1].headers).toEqual(CONSOLE_HEADER);
        expect(FakeWebSocket.instances).toHaveLength(1);
        expect(FakeWebSocket.instances[0].url).toMatch(/\/ws\?ticket=single-use$/);
    });

    it('hands a 4401 close back to admin.js instead of reconnecting', async () => {
        vi.useFakeTimers();
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: 't1' }),
        });
        dashboard.start(1);
        await vi.advanceTimersByTimeAsync(0);

        FakeWebSocket.instances[0].serverClose(4401);
        await vi.advanceTimersByTimeAsync(10000);

        expect(events.map((event) => event.name)).toContain('console:session-check');
        expect(FakeWebSocket.instances).toHaveLength(1);
        expect(dashboard.reconnectTimer).toBeNull();
    });

    it('reconnects with a fresh ticket after an ordinary drop', async () => {
        vi.useFakeTimers();
        let issued = 0;
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: `t${++issued}` }),
        });
        dashboard.start(1);
        await vi.advanceTimersByTimeAsync(0);

        FakeWebSocket.instances[0].serverClose(1006);
        await vi.advanceTimersByTimeAsync(5000);

        expect(FakeWebSocket.instances).toHaveLength(2);
        expect(FakeWebSocket.instances[1].url).toMatch(/ticket=t2$/);
        expect(events.map((event) => event.name)).not.toContain('console:session-check');
    });

    it('asks for a session check when the ticket request is refused with 401', async () => {
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(401, { reason: 'invalid' }),
        });
        dashboard.start(1);
        await flush();

        expect(FakeWebSocket.instances).toHaveLength(0);
        expect(events.map((event) => event.name)).toContain('console:session-check');
    });

    it('backs off instead of asking for a session check when the ticket request gets 503', async () => {
        vi.useFakeTimers();
        let ticketStatus = 503;
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => (ticketStatus === 503 ? jsonResponse(503, { reason: 'unavailable' }) : jsonResponse(200, { ticket: 't1' })),
        });
        dashboard.start(1);
        await vi.advanceTimersByTimeAsync(0);
        expect(events.map((event) => event.name)).not.toContain('console:session-check');

        ticketStatus = 200;
        await vi.advanceTimersByTimeAsync(4999);
        expect(FakeWebSocket.instances).toHaveLength(0);
        await vi.advanceTimersByTimeAsync(1);
        expect(FakeWebSocket.instances).toHaveLength(1);
    });

    it('retries a 4503 close on an exponential backoff capped at 60 s, without a session check', async () => {
        vi.useFakeTimers();
        let issued = 0;
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: `t${++issued}` }),
        });
        dashboard.start(1);
        await vi.advanceTimersByTimeAsync(0);

        const waits = [];
        for (let attempt = 0; attempt < 6; attempt++) {
            const socket = FakeWebSocket.instances.at(-1);
            socket.serverOpen();
            socket.serverClose(4503);
            const before = FakeWebSocket.instances.length;
            let waited = 0;
            while (FakeWebSocket.instances.length === before) {
                await vi.advanceTimersByTimeAsync(1000);
                waited += 1000;
            }
            waits.push(waited);
        }

        expect(waits).toEqual([5000, 10000, 20000, 40000, 60000, 60000]);
        expect(events.map((event) => event.name)).not.toContain('console:session-check');
    });

    it('resets the backoff once a socket delivers data', async () => {
        vi.useFakeTimers();
        let issued = 0;
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: `t${++issued}` }),
        });
        dashboard.start(1);
        await vi.advanceTimersByTimeAsync(0);
        FakeWebSocket.instances[0].serverClose(4503);
        await vi.advanceTimersByTimeAsync(5000);
        FakeWebSocket.instances[1].serverClose(4503);
        await vi.advanceTimersByTimeAsync(10000);
        expect(FakeWebSocket.instances).toHaveLength(3);

        FakeWebSocket.instances[2].serverMessage({ type: 'metrics_update', data: { pipelines: {}, databases: [] } });
        FakeWebSocket.instances[2].serverClose(1006);
        await vi.advanceTimersByTimeAsync(5000);
        expect(FakeWebSocket.instances).toHaveLength(4);
    });

    it('reconnects on the backoff, not at once, when a 4401 is followed by a confirmed session', async () => {
        vi.useFakeTimers();
        let issued = 0;
        const { dashboard } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: `t${++issued}` }),
        });
        dashboard.start(1);
        await vi.advanceTimersByTimeAsync(0);

        // The cache still vouches for the session, so admin.js re-announces it straight away.
        for (let round = 0; round < 3; round++) {
            FakeWebSocket.instances.at(-1).serverClose(4401);
            document.dispatchEvent(new CustomEvent('console:authenticated', { detail: { session: 1 } }));
            await vi.advanceTimersByTimeAsync(0);
        }
        expect(FakeWebSocket.instances).toHaveLength(1);
        await vi.advanceTimersByTimeAsync(5000);
        expect(FakeWebSocket.instances).toHaveLength(2);
    });

    it('closes the socket and stays quiet once signed out', async () => {
        vi.useFakeTimers();
        const { dashboard, fetchMock } = startedDashboard({
            '/api/metrics': () => jsonResponse(200, { pipelines: {}, databases: [] }),
            '/api/ws-ticket': () => jsonResponse(200, { ticket: 't1' }),
        });
        dashboard.start(1);
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
