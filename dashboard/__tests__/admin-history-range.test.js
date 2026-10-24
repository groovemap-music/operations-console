import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * Queue Trends and System Health read VictoriaMetrics-backed console routes through one shared
 * time-range control. They never call the catalog-api history proxy, and a metrics store that
 * is down shows an explicit "History unavailable" state.
 */
const __dirname = dirname(fileURLToPath(import.meta.url));
const ADMIN_JS_PATH = resolve(__dirname, '..', 'static', 'admin.js');

function loadAdminDashboardClass() {
    const code = readFileSync(ADMIN_JS_PATH, 'utf-8');
    const withoutAutoInit = code.replace(/document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/, '');
    const wrapped = `(function() {\n${withoutAutoInit}\nglobalThis.__AdminDashboard = AdminDashboard;\n})();`;
    vm.runInThisContext(wrapped, { filename: ADMIN_JS_PATH });
    return globalThis.__AdminDashboard;
}

function setupDOM() {
    document.body.textContent = '';
    const ids = ['login-view', 'console-view', 'qt-loading', 'qt-error', 'qt-error-msg', 'sh-loading', 'sh-error', 'sh-error-msg', 'queue-summary-tiles', 'dlq-grid'];
    ids.forEach((id) => {
        const el = document.createElement('div');
        el.id = id;
        document.body.appendChild(el);
    });
    const bar = document.createElement('div');
    bar.id = 'console-range-bar';
    ['1h', '6h', '24h', '7d', '30d', '90d'].forEach((range) => {
        const btn = document.createElement('button');
        btn.className = 'range-btn';
        btn.dataset.range = range;
        bar.appendChild(btn);
    });
    document.body.appendChild(bar);
}

describe('shared history time range', () => {
    let app;

    beforeEach(() => {
        setupDOM();
        localStorage.clear();
        const AdminDashboard = loadAdminDashboardClass();
        app = new AdminDashboard();
        app.renderQueueDepthChart = vi.fn();
        app.renderResponseTimeChart = vi.fn();
        app.renderEndpointsTable = vi.fn();
        app.renderServiceCards = vi.fn();
    });

    it('defaults to 24h and ignores a stored value that is not an offered range', () => {
        expect(app._getRange()).toBe('24h');
        localStorage.setItem('console_range', '2h');
        expect(app._getRange()).toBe('24h');
    });

    it('one button click sets the range for both history views', () => {
        app.activeTab = 'queue-trends';
        app.fetchQueueHistory = vi.fn();
        app.fetchHealthHistory = vi.fn();
        document.querySelector('.range-btn[data-range="7d"]').click();
        expect(localStorage.getItem('console_range')).toBe('7d');
        expect(app.fetchQueueHistory).toHaveBeenCalledWith('7d');
        expect(document.querySelector('.range-btn.active').dataset.range).toBe('7d');

        app.activeTab = 'system-health';
        document.querySelector('.range-btn[data-range="90d"]').click();
        expect(app.fetchHealthHistory).toHaveBeenCalledWith('90d');
    });

    it('the range bar shows only on the history tabs', () => {
        const bar = document.getElementById('console-range-bar');
        app.fetchQueueHistory = vi.fn();
        app.switchTab('queue-trends');
        expect(bar.style.display).toBe('flex');
        app.switchTab('overview');
        expect(bar.style.display).toBe('none');
    });

    it('fetches the console history routes, never the catalog-api history proxy', async () => {
        app.authFetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ queues: [], dlq_summary: [] }) });
        await app.fetchQueueHistory('6h');
        app.authFetch.mockResolvedValue({ ok: true, json: async () => ({ services: [], endpoints: [] }) });
        await app.fetchHealthHistory('30d');
        const urls = app.authFetch.mock.calls.map((c) => c[0]);
        expect(urls).toEqual(['/api/history/queues?range=6h', '/api/history/health?range=30d']);
    });

    it('shows an explicit history-unavailable state on a 503 and clears the panels', async () => {
        app.authFetch = vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({ detail: 'history unavailable' }) });
        await app.fetchQueueHistory('24h');
        expect(document.getElementById('qt-error').style.display).toBe('flex');
        expect(document.getElementById('qt-error-msg').textContent).toContain('History unavailable');
        expect(document.getElementById('qt-error-msg').textContent).toContain('Overview is unaffected');
        expect(app.renderQueueDepthChart).toHaveBeenCalledWith({});

        await app.fetchHealthHistory('24h');
        expect(document.getElementById('sh-error-msg').textContent).toContain('History unavailable');
    });

    it('shows a missing publish or ack rate as no data, not 0/s', () => {
        const queues = [{ queue_name: 'q', data_points: [{ publish_rate: null, ack_rate: 2 }] }];
        expect(app._avgLatest(queues, 'publish_rate')).toBeNull();
        expect(app._avgLatest(queues, 'ack_rate')).toBe(2);
    });
});
