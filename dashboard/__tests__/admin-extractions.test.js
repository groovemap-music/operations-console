import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * The Extractions tab's history rows read catalog-api's ExtractionHistoryResponse (duration_seconds,
 * record_counts, error_message), and each row carries the run's id, so the Overview's Run progress
 * view can open a run at its own row.
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

describe('extraction history rows', () => {
    let app;

    beforeEach(() => {
        document.body.textContent = '';
        for (const [tag, id] of [['div', 'login-view'], ['div', 'console-view'], ['span', 'extraction-status'], ['p', 'history-empty']]) {
            const el = document.createElement(tag);
            el.id = id;
            document.body.appendChild(el);
        }
        const table = document.createElement('table');
        const body = document.createElement('tbody');
        body.id = 'history-body';
        table.appendChild(body);
        document.body.appendChild(table);
        const AdminDashboard = loadAdminDashboardClass();
        app = new AdminDashboard();
    });

    it('renders each run with its id, duration, record total, and error', async () => {
        app.authFetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({
                extractions: [
                    { id: 'run-2', status: 'running', started_at: '2026-10-24T08:00:00Z', completed_at: null, duration_seconds: null, record_counts: null, error_message: null },
                    {
                        id: 'run-1', status: 'completed', started_at: '2026-10-23T08:00:00Z', completed_at: '2026-10-23T11:12:00Z', duration_seconds: 11520,
                        record_counts: { releases: 2_000_000, artists: 1_000_000, total: 3_000_000 }, error_message: null,
                    },
                    { id: 'run-0', status: 'failed', started_at: '2026-10-22T08:00:00Z', duration_seconds: 42, record_counts: { releases: 5, artists: 7 }, error_message: 'Extractor unreachable' },
                ],
            }),
        }));
        await app.loadExtractions();
        const rows = [...document.querySelectorAll('#history-body tr')];
        expect(rows.map(r => r.id)).toEqual(['extraction-run-run-2', 'extraction-run-run-1', 'extraction-run-run-0']);
        expect(rows.every(r => r.tabIndex === -1)).toBe(true);
        const cells = rows.map(r => [...r.children].slice(1).map(c => c.textContent));
        expect(cells).toEqual([
            ['running', '—', '—', '—'],
            ['completed', '3h12m', '3,000,000', '—'],
            ['failed', '42s', '12', 'Extractor unreachable'],
        ]);
        expect(rows[1].children[3].title).toBe('releases: 2,000,000, artists: 1,000,000');
    });
});
