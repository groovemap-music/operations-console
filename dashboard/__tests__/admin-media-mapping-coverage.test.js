import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * Coverage for AdminDashboard._eaRenderMediaMappingCoverage(), the rendering
 * half of the "Media mapping coverage" panel in the data-quality
 * (extraction-analysis) view. The aggregation itself is a server-side
 * concern covered in Python (tests/test_media_mapping_coverage.py); this
 * suite only exercises how the console renders the JSON that route returns.
 *
 * Loaded via the same vm-based technique as admin-toast.test.js so the
 * real dashboard/static/admin.js class is exercised rather than a copy.
 */
const __dirname = dirname(fileURLToPath(import.meta.url));
const ADMIN_JS_PATH = resolve(__dirname, '..', 'static', 'admin.js');

function loadAdminDashboardClass() {
    const code = readFileSync(ADMIN_JS_PATH, 'utf-8');
    const withoutAutoInit = code.replace(
        /document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/,
        ''
    );
    const wrapped = `(function() {\n${withoutAutoInit}\nglobalThis.__AdminDashboard = AdminDashboard;\n})();`;
    vm.runInThisContext(wrapped, { filename: ADMIN_JS_PATH });
    return globalThis.__AdminDashboard;
}

function setupAdminDOM() {
    document.body.textContent = '';

    const ids = [
        'login-view',
        'admin-view',
        'toast',
        'ea-media-mapping-unavailable',
        'ea-media-mapping-cards',
        'ea-media-mapping-truncated-note',
    ];
    ids.forEach((id) => {
        const el = document.createElement('div');
        el.id = id;
        document.body.appendChild(el);
    });

    const body = document.createElement('div');
    body.id = 'ea-media-mapping-body';
    document.body.appendChild(body);

    const table = document.createElement('table');
    const tbody = document.createElement('tbody');
    tbody.id = 'ea-media-mapping-tbody';
    table.appendChild(tbody);
    document.body.appendChild(table);
}

describe('AdminDashboard._eaRenderMediaMappingCoverage', () => {
    let AdminDashboard;
    let app;

    beforeEach(() => {
        setupAdminDOM();
        localStorage.clear();
        AdminDashboard = loadAdminDashboardClass();
        app = new AdminDashboard();
    });

    it('shows the unavailable note and hides the body when available is false', () => {
        app._eaRenderMediaMappingCoverage({
            available: false,
            source: 'musicbrainz',
            reason: 'MusicBrainz ingestion has no data-quality rules engine.',
        });

        const unavailableEl = document.getElementById('ea-media-mapping-unavailable');
        const bodyEl = document.getElementById('ea-media-mapping-body');
        expect(bodyEl.style.display).toBe('none');
        expect(unavailableEl.style.display).toBe('');
        expect(unavailableEl.textContent).toBe('MusicBrainz ingestion has no data-quality rules engine.');
    });

    it('shows a fallback message when the fetch itself failed (null data)', () => {
        app._eaRenderMediaMappingCoverage(null);

        const unavailableEl = document.getElementById('ea-media-mapping-unavailable');
        const bodyEl = document.getElementById('ea-media-mapping-body');
        expect(bodyEl.style.display).toBe('none');
        expect(unavailableEl.style.display).toBe('');
        expect(unavailableEl.textContent).not.toBe('');
    });

    it('renders stat cards and the top-format table when available', () => {
        app._eaRenderMediaMappingCoverage({
            available: true,
            version: '20240101',
            source: 'discogs',
            releases_with_unmapped_media: 12,
            unmapped_violation_count: 14,
            total_flagged_releases: 200,
            unmapped_share_of_flagged_releases_percent: 6.0,
            top_unmapped_formats: [
                { name: 'Shellac', count: 8 },
                { name: 'Wax Cylinder', count: 4 },
            ],
            truncated: false,
        });

        const bodyEl = document.getElementById('ea-media-mapping-body');
        const unavailableEl = document.getElementById('ea-media-mapping-unavailable');
        expect(bodyEl.style.display).toBe('');
        expect(unavailableEl.style.display).toBe('none');

        const cardsContainer = document.getElementById('ea-media-mapping-cards');
        const cardText = cardsContainer.textContent;
        expect(cardText).toContain('12');
        expect(cardText).toContain('14');
        expect(cardText).toContain('200');
        expect(cardText).toContain('6%');

        const tbody = document.getElementById('ea-media-mapping-tbody');
        const rows = tbody.querySelectorAll('tr');
        expect(rows.length).toBe(2);
        expect(rows[0].textContent).toContain('Shellac');
        expect(rows[0].textContent).toContain('8');
        expect(rows[1].textContent).toContain('Wax Cylinder');
        expect(rows[1].textContent).toContain('4');
    });

    it('renders an empty-state row when no unmapped formats are found', () => {
        app._eaRenderMediaMappingCoverage({
            available: true,
            source: 'discogs',
            releases_with_unmapped_media: 0,
            unmapped_violation_count: 0,
            total_flagged_releases: 0,
            unmapped_share_of_flagged_releases_percent: null,
            top_unmapped_formats: [],
            truncated: false,
        });

        const tbody = document.getElementById('ea-media-mapping-tbody');
        expect(tbody.textContent).toContain('No unmapped format names found.');
    });

    it('shows the truncated note only when truncated is true', () => {
        app._eaRenderMediaMappingCoverage({
            available: true,
            source: 'discogs',
            releases_with_unmapped_media: 5000,
            unmapped_violation_count: 5000,
            total_flagged_releases: null,
            unmapped_share_of_flagged_releases_percent: null,
            top_unmapped_formats: [{ name: 'Shellac', count: 5000 }],
            truncated: true,
        });

        const note = document.getElementById('ea-media-mapping-truncated-note');
        expect(note.style.display).toBe('');
        expect(note.textContent.length).toBeGreaterThan(0);

        app._eaRenderMediaMappingCoverage({
            available: true,
            source: 'discogs',
            releases_with_unmapped_media: 1,
            unmapped_violation_count: 1,
            total_flagged_releases: null,
            unmapped_share_of_flagged_releases_percent: null,
            top_unmapped_formats: [{ name: 'Shellac', count: 1 }],
            truncated: false,
        });

        expect(note.style.display).toBe('none');
    });

    it('omits the "total flagged" and "share" stat cards when the API did not provide them', () => {
        app._eaRenderMediaMappingCoverage({
            available: true,
            source: 'discogs',
            releases_with_unmapped_media: 3,
            unmapped_violation_count: 3,
            total_flagged_releases: null,
            unmapped_share_of_flagged_releases_percent: null,
            top_unmapped_formats: [{ name: 'Shellac', count: 3 }],
            truncated: false,
        });

        const cardsContainer = document.getElementById('ea-media-mapping-cards');
        expect(cardsContainer.textContent).not.toContain('%');
        // Three base stat cards remain (unmapped releases, violations, distinct formats).
        expect(cardsContainer.children.length).toBe(3);
    });
});
