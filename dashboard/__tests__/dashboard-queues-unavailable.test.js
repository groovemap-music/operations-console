import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * A failed broker scrape (`queues_available: false`) must render an explicit "Queue metrics
 * unavailable" state in the queue chart and the rate dials, and clear it on recovery, never an
 * idle-looking empty pipeline.
 */
const __dirname = dirname(fileURLToPath(import.meta.url));
const DASHBOARD_JS_PATH = resolve(__dirname, '..', 'static', 'dashboard.js');

function loadDashboardClass() {
    const code = readFileSync(DASHBOARD_JS_PATH, 'utf-8');
    const withoutAutoInit = code.replace(/document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/, '');
    const wrapped = `(function() {\n${withoutAutoInit}\nglobalThis.__Dashboard = Dashboard;\n})();`;
    vm.runInThisContext(wrapped, { filename: DASHBOARD_JS_PATH });
    return globalThis.__Dashboard;
}

function setupDOM() {
    document.body.innerHTML = `
        <span data-queues-unavailable="discogs" class="hidden">Queue metrics unavailable</span>
        <span data-queues-unavailable="discogs" class="hidden">Queue metrics unavailable</span>
        <div class="flex flex-col">
            <svg><circle id="discogs-rate-circle-graphinator-artists-publish"></circle></svg>
            <span id="discogs-rate-text-graphinator-artists-publish">0</span>
        </div>
        <span id="discogs-graphinator-artists-count">—</span>`;
}

const queue = { name: 'groovemap-discogs-graphinator-artists', messages: 12, messages_ready: 12, messages_unacknowledged: 0, consumers: 1, message_rate: 4.5, ack_rate: 3 };

describe('Dashboard queue metrics unavailable state', () => {
    let dashboard;

    beforeEach(() => {
        setupDOM();
        const Dashboard = loadDashboardClass();
        dashboard = Object.create(Dashboard.prototype);
        Object.assign(dashboard, { CIRCUMFERENCE: 176, gaugeStats: {}, currentMaps: {}, queuesUnavailable: {}, activityLog: [], maxLogEntries: 50 });
        for (const method of ['updateServices', 'updateDatabases', 'updateLastUpdated', 'updateOverallStatus', 'renderActivityLog']) {
            dashboard[method] = vi.fn();
        }
    });

    const banners = () => [...document.querySelectorAll('[data-queues-unavailable="discogs"]')];
    const dial = () => document.getElementById('discogs-rate-text-graphinator-artists-publish');

    it('shows the banners and N/A dials instead of an empty pipeline, then clears on recovery', () => {
        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue], queues_available: true } } });
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
        expect(dial().textContent).not.toBe('N/A');

        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [], queues_available: false } } });
        expect(banners().every((el) => !el.classList.contains('hidden'))).toBe(true);
        expect(dial().textContent).toBe('N/A');

        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue], queues_available: true } } });
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
        expect(dial().textContent).toBe('4.5');
    });

    it('treats a payload without queues_available as available', () => {
        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue] } } });
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
    });
});
