import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadDashboardClass, makeDashboard } from './overview-helpers.js';

/**
 * The System Event Log shows what the server detected and what operators did, newest first.
 * The server streams a backlog on every connect and live events after it; operator actions come
 * from the audit log through the console's /admin/api proxy. Connect and disconnect are the
 * connection indicator's business and never become entries.
 */
const T0 = Date.parse('2026-01-01T10:00:00Z');
const event = (id, message, level = 'warning', offset = 0) => ({ id, message, level, timestamp: new Date(T0 + offset * 1000).toISOString() });

function rows() {
    return [...document.querySelectorAll('#activityLog .log-entry')].map((row) => [...row.children].map((c) => c.textContent));
}

describe('System Event Log', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
        document.body.insertAdjacentHTML('beforeend', '<div id="activityLog"></div><span class="status-indicator"></span><span class="status-text"></span>');
    });

    afterEach(() => vi.restoreAllMocks());

    it('renders a backlog newest first with the level and the time the event happened', () => {
        dashboard.applyEventBacklog([event(1, 'Neo4j is down', 'error', 0), event(2, 'Neo4j is up', 'info', 30)]);
        const shown = rows();
        expect(shown.map((r) => r[2])).toEqual(['Neo4j is up', 'Neo4j is down']);
        expect(shown.map((r) => r[1])).toEqual(['[INFO]', '[ERR]']);
    });

    it('replaces the server entries on every backlog, so a reconnect never doubles them', () => {
        dashboard.applyEventBacklog([event(1, 'one'), event(2, 'two', 'warning', 5)]);
        dashboard.applyEventBacklog([event(1, 'one'), event(2, 'two', 'warning', 5)]);
        expect(rows().map((r) => r[2])).toEqual(['two', 'one']);
    });

    it('shows a live event once, and ignores one the backlog already covered', () => {
        dashboard.applyEventBacklog([event(1, 'one'), event(2, 'two', 'warning', 5)]);
        dashboard.applyServerEvent(event(2, 'two', 'warning', 5));
        dashboard.applyServerEvent(event(3, 'three', 'error', 10));
        dashboard.applyServerEvent(event(3, 'three', 'error', 10));
        expect(rows().map((r) => r[2])).toEqual(['three', 'two', 'one']);
    });

    it('shows the server\'s stall, no-consumer and recovery events, from the backlog after a reconnect and live', () => {
        const queue = 'groovemap-discogs-graphinator-artists';
        dashboard.handleMessage({ type: 'event_backlog', data: [
            { ...event(1, `${queue}: NO CONSUMER`, 'error', 0), kind: 'queue-no-consumer' },
            { ...event(2, `${queue}: STALLED · no acks 1m`, 'error', 20), kind: 'queue-stalled' },
        ] });
        dashboard.handleMessage({ type: 'event', data: { ...event(3, `${queue} is flowing`, 'info', 40), kind: 'queue-recovered' } });
        expect(rows().map((r) => [r[1], r[2]])).toEqual([
            ['[INFO]', `${queue} is flowing`],
            ['[ERR]', `${queue}: STALLED · no acks 1m`],
            ['[ERR]', `${queue}: NO CONSUMER`],
        ]);
    });

    it('routes backlog and live events through the message handler, and ignores unknown types', () => {
        dashboard.handleMessage({ type: 'event_backlog', data: [event(1, 'one')] });
        dashboard.handleMessage({ type: 'event', data: event(2, 'two', 'info', 3) });
        dashboard.handleMessage({ type: 'something_else', data: {} });
        expect(rows().map((r) => r[2])).toEqual(['two', 'one']);
    });

    it('keeps no more than the newest maxLogEntries', () => {
        dashboard.maxLogEntries = 3;
        dashboard.applyEventBacklog([1, 2, 3, 4, 5].map((id) => event(id, `e${id}`, 'info', id)));
        expect(rows().map((r) => r[2])).toEqual(['e5', 'e4', 'e3']);
    });

    it('interleaves operator actions from the audit log by time, and replaces them on each poll', async () => {
        dashboard.active = true;
        dashboard.session = 1;
        const audit = {
            'extraction.trigger': [{ action: 'extraction.trigger', admin_email: 'ops@example.com', created_at: new Date(T0 + 20_000).toISOString() }],
            'dlq.purge': [{ action: 'dlq.purge', admin_email: 'sre@example.com', target: 'q.dlq', created_at: new Date(T0 + 5_000).toISOString() }],
        };
        const calls = [];
        globalThis.fetch = vi.fn(async (url, init) => {
            calls.push([String(url), init.headers]);
            const action = new URL(url, 'http://console.test').searchParams.get('action');
            return { ok: true, status: 200, json: async () => ({ entries: audit[action] }) };
        });
        dashboard.applyEventBacklog([event(1, 'Neo4j is down', 'error', 10)]);

        await dashboard.fetchAuditEntries();
        expect(rows().map((r) => r[2])).toEqual(['ops@example.com triggered an extraction', 'Neo4j is down', 'sre@example.com purged DLQ q.dlq']);
        expect(calls.every(([, headers]) => headers['X-GrooveMap-Console'] === '1' && !('Authorization' in headers))).toBe(true);
        expect(calls.map(([url]) => url)).toEqual([
            '/admin/api/audit-log?action=extraction.trigger&page_size=20',
            '/admin/api/audit-log?action=dlq.purge&page_size=20',
        ]);

        audit['dlq.purge'] = [];
        await dashboard.fetchAuditEntries();
        expect(rows().map((r) => r[2])).toEqual(['ops@example.com triggered an extraction', 'Neo4j is down']);
    });

    it('keeps the entries shown when the audit log cannot be read, and asks for a session check on a refusal', async () => {
        dashboard.active = true;
        dashboard.session = 1;
        dashboard.requestSessionCheck = vi.fn();
        dashboard._replaceLogSource('audit', [{ message: 'ops@example.com triggered an extraction', type: 'info', time: T0 }]);

        globalThis.fetch = vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }));
        await dashboard.fetchAuditEntries();
        globalThis.fetch = vi.fn(async () => { throw new Error('offline'); });
        await dashboard.fetchAuditEntries();
        expect(rows().length).toBe(1);
        expect(dashboard.requestSessionCheck).not.toHaveBeenCalled();

        globalThis.fetch = vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) }));
        await dashboard.fetchAuditEntries();
        expect(dashboard.requestSessionCheck).toHaveBeenCalledTimes(1);
        expect(rows().length).toBe(1);
    });

    describe('connection', () => {
        class FakeSocket {
            static OPEN = 1;
            static CONNECTING = 0;
            constructor() { this.readyState = 0; FakeSocket.last = this; }
            close() {}
        }

        it('reaches the connection indicator and the log stays empty through connect, disconnect, and refusals', async () => {
            globalThis.WebSocket = FakeSocket;
            dashboard.active = true;
            dashboard.connecting = false;
            dashboard.ws = null;
            dashboard.reconnectDelay = 5000;
            dashboard._fetchTicket = async () => 'ticket';
            dashboard.reconnect = vi.fn();
            dashboard.requestSessionCheck = vi.fn();
            dashboard._resetHistory = vi.fn();

            await dashboard.initializeWebSocket();
            const socket = FakeSocket.last;
            socket.onopen();
            expect(document.querySelector('.status-text').textContent).toBe('Connected');
            socket.onclose({ code: 1006 });
            expect(document.querySelector('.status-text').textContent).not.toBe('Connected');
            expect(dashboard.reconnect).toHaveBeenCalled();

            dashboard.ws = null;
            await dashboard.initializeWebSocket();
            FakeSocket.last.onclose({ code: 4503 });
            dashboard.ws = null;
            await dashboard.initializeWebSocket();
            FakeSocket.last.onclose({ code: 4401 });
            expect(dashboard.requestSessionCheck).toHaveBeenCalledTimes(1);

            expect(dashboard.activityLog).toEqual([]);
            expect(rows()).toEqual([]);
        });
    });

    it('says the log is empty rather than collapsing to nothing', () => {
        dashboard.applyEventBacklog([]);
        expect(rows()).toEqual([]);
        expect(document.querySelector('#activityLog .log-empty').textContent).toBe('No events yet');
        dashboard.applyEventBacklog([event(1, 'one')]);
        expect(document.querySelector('#activityLog .log-empty')).toBeNull();
    });

    it('is cleared when the session ends', () => {
        const Dashboard = loadDashboardClass();
        dashboard.applyEventBacklog([event(1, 'one')]);
        dashboard.stopAlertPolling = vi.fn();
        dashboard.closeQueueDrawer = vi.fn();
        dashboard._resetHistory = vi.fn();
        dashboard.updateAttention = vi.fn();
        dashboard.updateConnectionStatus = vi.fn();
        Dashboard.prototype.stop.call(dashboard);
        expect(dashboard.activityLog).toEqual([]);
        expect(rows()).toEqual([]);
        expect(dashboard.lastServerEventId).toBe(0);
    });
});
