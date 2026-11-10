import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/** AdminDashboard.replayDlq(): confirm step, result messages per status, and the queue-count refresh. */
const ADMIN_JS_PATH = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'static', 'admin.js');

function loadAdminDashboardClass() {
    const code = readFileSync(ADMIN_JS_PATH, 'utf-8').replace(/document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/, '');
    vm.runInThisContext(`(function() {\n${code}\nglobalThis.__AdminDashboard = AdminDashboard;\n})();`, { filename: ADMIN_JS_PATH });
    return globalThis.__AdminDashboard;
}

const reply = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

describe('AdminDashboard.replayDlq', () => {
    let app;
    let refresh;

    beforeEach(() => {
        document.body.textContent = '';
        for (const id of ['login-view', 'console-view', 'toast']) {
            const el = document.createElement('div');
            el.id = id;
            document.body.appendChild(el);
        }
        localStorage.clear();
        app = new (loadAdminDashboardClass())();
        app.showToast = vi.fn();
        refresh = vi.fn();
        globalThis.window.dashboard = { fetchInitialData: refresh };
        globalThis.confirm = vi.fn(() => true);
    });

    afterEach(() => {
        delete globalThis.window.dashboard;
        vi.restoreAllMocks();
    });

    it('does nothing when the operator declines the confirm', async () => {
        globalThis.confirm = vi.fn(() => false);
        app.authFetch = vi.fn();
        await app.replayDlq('q.dlq');
        expect(app.authFetch).not.toHaveBeenCalled();
        expect(refresh).not.toHaveBeenCalled();
    });

    it('posts to the replay proxy and shows replayed and remaining, then refreshes counts', async () => {
        app.authFetch = vi.fn(async () => reply(200, { queue: 'q.dlq', replayed: 97, remaining: 3 }));
        await app.replayDlq('q.dlq');
        expect(app.authFetch).toHaveBeenCalledWith('/admin/api/dlq/replay/q.dlq', { method: 'POST' });
        expect(app.showToast).toHaveBeenCalledWith('Replayed 97 message(s) from q.dlq, 3 remaining', 'success');
        expect(refresh).toHaveBeenCalledOnce();
    });

    it('409 says the work queue is missing and nothing moved', async () => {
        app.authFetch = vi.fn(async () => reply(409, { detail: 'Work queue does not exist: w' }));
        await app.replayDlq('q.dlq');
        const [message, type] = app.showToast.mock.calls[0];
        expect(message).toContain('missing');
        expect(message).toContain('nothing was moved');
        expect(type).toBe('error');
        expect(refresh).toHaveBeenCalledOnce();
    });

    it('502 says how many moved and that the rest stay parked', async () => {
        app.authFetch = vi.fn(async () => reply(502, { detail: 'RabbitMQ unreachable or replay failed after 41 replayed; unreplayed messages remain in the DLQ' }));
        await app.replayDlq('q.dlq');
        const [message, type] = app.showToast.mock.calls[0];
        expect(message).toContain('41 message(s) moved');
        expect(message).toContain('stay parked');
        expect(type).toBe('error');
        expect(refresh).toHaveBeenCalledOnce();
    });

    it('502 without a parsable count still says the rest stay parked', async () => {
        app.authFetch = vi.fn(async () => reply(502, {}));
        await app.replayDlq('q.dlq');
        expect(app.showToast.mock.calls[0][0]).toContain('stay parked');
    });

    it('502 with structured counts shows the exact moved and remaining numbers', async () => {
        // The structured fields win over the prose, so a detail that disagrees is ignored.
        app.authFetch = vi.fn(async () => reply(502, {
            detail: 'RabbitMQ unreachable or replay failed after 40 replayed; unreplayed messages remain in the DLQ',
            replayed: 41,
            remaining: 9,
        }));
        await app.replayDlq('q.dlq');
        expect(app.showToast).toHaveBeenCalledWith('Partial replay of q.dlq: 41 moved, 9 still parked', 'error');
        expect(refresh).toHaveBeenCalledOnce();
    });

    it('502 with remaining null says the remaining depth is unknown and suggests a refresh', async () => {
        app.authFetch = vi.fn(async () => reply(502, {
            detail: 'RabbitMQ unreachable or replay failed after 3 replayed; unreplayed messages remain in the DLQ',
            replayed: 3,
            remaining: null,
        }));
        await app.replayDlq('q.dlq');
        const [message, type] = app.showToast.mock.calls[0];
        expect(message).toContain('Partial replay of q.dlq: 3 moved');
        expect(message).toContain('remaining depth is unknown');
        expect(message).toContain('refresh');
        expect(message).not.toContain('null');
        expect(type).toBe('error');
        expect(refresh).toHaveBeenCalledOnce();
    });

    it('502 with only detail text (older catalog-api) falls back to the prose count', async () => {
        app.authFetch = vi.fn(async () => reply(502, { detail: 'RabbitMQ unreachable or replay failed after 7 replayed; unreplayed messages remain in the DLQ' }));
        await app.replayDlq('q.dlq');
        expect(app.showToast).toHaveBeenCalledWith(
            'Partial replay of q.dlq: 7 message(s) moved; the rest stay parked in the dead letter queue',
            'error',
        );
    });

    it('502 with a garbage body never throws and falls back to the generic line', async () => {
        for (const body of [
            { replayed: 'lots', remaining: 'some', detail: 42 },
            { replayed: 1.5, remaining: {}, detail: null },
            null,
            'not an object',
        ]) {
            app.showToast.mockClear();
            app.authFetch = vi.fn(async () => reply(502, body));
            await app.replayDlq('q.dlq');
            const [message, type] = app.showToast.mock.calls[0];
            expect(message).toBe('Partial replay of q.dlq: some messages may have moved; the rest stay parked in the dead letter queue');
            expect(type).toBe('error');
        }
    });

    it('404 names the queue as unknown', async () => {
        app.authFetch = vi.fn(async () => reply(404, { detail: 'Unknown DLQ: q.dlq' }));
        await app.replayDlq('q.dlq');
        expect(app.showToast.mock.calls[0][0]).toContain('not a known');
    });

    it('reports a connection error', async () => {
        app.authFetch = vi.fn(async () => { throw new Error('down'); });
        await app.replayDlq('q.dlq');
        expect(app.showToast).toHaveBeenCalledWith('Connection error', 'error');
    });
});
