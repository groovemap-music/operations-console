import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * A null rate means "not computable this interval". The rate dials render it as an em dash with
 * an accessible "rate unknown" label, never as 0, and a computed 0 still renders as 0.0.
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
        <div class="flex flex-col">
            <svg><circle id="discogs-rate-circle-graphinator-artists-publish"></circle></svg>
            <span id="discogs-rate-text-graphinator-artists-publish">0</span>
        </div>
        <div class="flex flex-col">
            <svg><circle id="discogs-rate-circle-graphinator-artists-ack"></circle></svg>
            <span id="discogs-rate-text-graphinator-artists-ack">0</span>
        </div>
        <span id="discogs-graphinator-artists-count">—</span>`;
}

const queue = (rates) => ({ name: 'groovemap-discogs-graphinator-artists', messages: 12, messages_ready: 12, messages_unacknowledged: 0, consumers: 1, ...rates });

describe('Dashboard unknown rates', () => {
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

    const publish = () => document.getElementById('discogs-rate-text-graphinator-artists-publish');
    const ack = () => document.getElementById('discogs-rate-text-graphinator-artists-ack');
    const update = (rates) => dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue(rates)], queues_available: true } } });

    it('renders null rates as an em dash labelled rate unknown, never 0', () => {
        update({ message_rate: null, ack_rate: null });
        for (const dial of [publish(), ack()]) {
            expect(dial.textContent).toBe('—');
            expect(dial.getAttribute('aria-label')).toBe('rate unknown');
        }
    });

    it('keeps a computed zero as 0.0 and drops the unknown label once a rate returns', () => {
        update({ message_rate: null, ack_rate: null });
        update({ message_rate: 0, ack_rate: 4.5 });
        expect(publish().textContent).toBe('0.0');
        expect(publish().hasAttribute('aria-label')).toBe(false);
        expect(ack().textContent).toBe('4.5');
        expect(ack().hasAttribute('aria-label')).toBe(false);
    });
});
