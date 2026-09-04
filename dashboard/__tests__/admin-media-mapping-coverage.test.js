import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * Coverage for AdminDashboard._eaRenderMediaMappingCoverage(), the rendering
 * half of the "Media mapping coverage" panel in the data-quality
 * (extraction-analysis) view. The reading itself comes from catalog-api's
 * /api/admin/media/unmapped route and is composed server-side, covered in
 * Python (tests/test_media_mapping_coverage.py); this suite only exercises how
 * the console renders the JSON that proxy route returns, for both providers.
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
            source: 'bandcamp',
            reason: "This extraction version reports source 'bandcamp'.",
        });

        const unavailableEl = document.getElementById('ea-media-mapping-unavailable');
        const bodyEl = document.getElementById('ea-media-mapping-body');
        expect(bodyEl.style.display).toBe('none');
        expect(unavailableEl.style.display).toBe('');
        expect(unavailableEl.textContent).toBe("This extraction version reports source 'bandcamp'.");
    });

    it('shows a fallback message when the fetch itself failed (null data)', () => {
        app._eaRenderMediaMappingCoverage(null);

        const unavailableEl = document.getElementById('ea-media-mapping-unavailable');
        const bodyEl = document.getElementById('ea-media-mapping-body');
        expect(bodyEl.style.display).toBe('none');
        expect(unavailableEl.style.display).toBe('');
        expect(unavailableEl.textContent).not.toBe('');
    });

    it('renders stat cards and the top-name table for a Discogs reading', () => {
        app._eaRenderMediaMappingCoverage({
            available: true,
            version: '20240101',
            source: 'discogs',
            provider: 'discogs',
            media_tagged_releases: 200,
            releases_with_unmapped_media: 12,
            unmapped_rate: 0.06,
            limit: 10,
            top_unmapped_formats: [
                { kind: 'format', name: 'Shellac', count: 8 },
                { kind: 'description', name: 'Hand-Numbered', count: 4 },
            ],
            truncated: false,
        });

        const bodyEl = document.getElementById('ea-media-mapping-body');
        const unavailableEl = document.getElementById('ea-media-mapping-unavailable');
        expect(bodyEl.style.display).toBe('');
        expect(unavailableEl.style.display).toBe('none');

        const cardText = document.getElementById('ea-media-mapping-cards').textContent;
        expect(cardText).toContain('discogs');
        expect(cardText).toContain('12');
        expect(cardText).toContain('200');
        expect(cardText).toContain('6.00%');

        const rows = document.getElementById('ea-media-mapping-tbody').querySelectorAll('tr');
        expect(rows.length).toBe(2);
        expect(rows[0].textContent).toContain('format');
        expect(rows[0].textContent).toContain('Shellac');
        expect(rows[0].textContent).toContain('8');
        expect(rows[1].textContent).toContain('description');
        expect(rows[1].textContent).toContain('Hand-Numbered');
        expect(rows[1].textContent).toContain('4');
    });

    it('renders a MusicBrainz reading with names and counts rather than a note', () => {
        app._eaRenderMediaMappingCoverage({
            available: true,
            version: '20240201',
            source: 'musicbrainz',
            provider: 'musicbrainz',
            media_tagged_releases: 80,
            releases_with_unmapped_media: 12,
            unmapped_rate: 0.15,
            limit: 10,
            top_unmapped_formats: [{ kind: 'format', name: 'DualDisc', count: 7 }],
            truncated: false,
        });

        const unavailableEl = document.getElementById('ea-media-mapping-unavailable');
        expect(unavailableEl.style.display).toBe('none');

        const cardText = document.getElementById('ea-media-mapping-cards').textContent;
        expect(cardText).toContain('musicbrainz');
        expect(cardText).toContain('15.00%');

        const rows = document.getElementById('ea-media-mapping-tbody').querySelectorAll('tr');
        expect(rows.length).toBe(1);
        expect(rows[0].textContent).toContain('DualDisc');
        expect(rows[0].textContent).toContain('7');
    });

    it('renders an empty-state row when no unmapped names are found', () => {
        app._eaRenderMediaMappingCoverage({
            available: true,
            source: 'discogs',
            provider: 'discogs',
            media_tagged_releases: 0,
            releases_with_unmapped_media: 0,
            unmapped_rate: 0.0,
            limit: 10,
            top_unmapped_formats: [],
            truncated: false,
        });

        const tbody = document.getElementById('ea-media-mapping-tbody');
        expect(tbody.textContent).toContain('No unmapped media names found.');
        expect(tbody.querySelector('td').colSpan).toBe(3);
    });

    it('shows the truncated note only when truncated is true', () => {
        const base = {
            available: true,
            source: 'discogs',
            provider: 'discogs',
            media_tagged_releases: 100000,
            releases_with_unmapped_media: 5000,
            unmapped_rate: 0.05,
            limit: 10,
            top_unmapped_formats: [{ kind: 'format', name: 'Shellac', count: 5000 }],
        };

        app._eaRenderMediaMappingCoverage({ ...base, truncated: true });

        const note = document.getElementById('ea-media-mapping-truncated-note');
        expect(note.style.display).toBe('');
        expect(note.textContent).toContain('capped');

        app._eaRenderMediaMappingCoverage({ ...base, truncated: false });

        expect(note.style.display).toBe('none');
        expect(note.textContent).toBe('');
    });

    it('omits the unmapped-rate card when the API did not provide a rate', () => {
        app._eaRenderMediaMappingCoverage({
            available: true,
            source: 'discogs',
            provider: 'discogs',
            media_tagged_releases: 100,
            releases_with_unmapped_media: 3,
            unmapped_rate: null,
            limit: 10,
            top_unmapped_formats: [{ kind: 'format', name: 'Shellac', count: 3 }],
            truncated: false,
        });

        const cardsContainer = document.getElementById('ea-media-mapping-cards');
        expect(cardsContainer.textContent).not.toContain('%');
        // Four base stat cards remain: provider, unmapped releases, media-tagged, distinct names.
        expect(cardsContainer.children.length).toBe(4);
    });
});
