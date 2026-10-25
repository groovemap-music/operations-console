import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

/**
 * The Ledger tab (design ADR 0016 section 7, concept D): a per-month density strip split by
 * event type and provenance, a paged works table with event, since and until filters, and one
 * work's drill-down at a chosen as_of. Backfilled events are dated from the MusicBrainz full
 * export, not recorded, and say so wherever they appear.
 *
 * Loaded with the same vm technique as admin-media-mapping-coverage.test.js, against the real
 * Ledger panel parsed from dashboard/static/index.html, so the ids the script binds to are the
 * shell's own.
 */
const here = dirname(fileURLToPath(import.meta.url));
const ADMIN_JS_PATH = resolve(here, '..', 'static', 'admin.js');
const ADMIN_SOURCE = readFileSync(ADMIN_JS_PATH, 'utf-8');
const SHELL = new DOMParser().parseFromString(readFileSync(resolve(here, '..', 'static', 'index.html'), 'utf-8'), 'text/html');

const WORK = '00000000-0000-4000-8000-000000000101';
const OTHER_WORK = '00000000-0000-4000-8000-000000000102';
const EDITION = '00000000-0000-4000-8000-000000000001';
const SPLIT_EDITION = '00000000-0000-4000-8000-000000000002';
const FORMER = '00000000-0000-4000-8000-000000000009';
const MBID = '00000000-9000-4000-8000-000000000001';

const REFRESHED_AT = '2026-10-25T21:05:00.123456Z';
const NOT_REFRESHED = { detail: 'identity ledger not yet refreshed' };

const DENSITY = {
    kind: 'release',
    refreshed_at: REFRESHED_AT,
    buckets: [
        { month: '2025-11', type: 'link_added', dated_from: 'musicbrainz_fullexport', events: 40 },
        { month: '2026-02', type: 'link_changed', dated_from: 'musicbrainz_fullexport', events: 3 },
        { month: '2026-02', type: 'link_changed', dated_from: 'groovemap', events: 2 },
        { month: '2026-01', type: 'minted', dated_from: 'groovemap', events: 120 },
    ],
};

const WORKS = {
    works: [
        {
            gm_id: WORK,
            providers: [{ provider: 'discogs', external_id: '900101' }, { provider: 'musicbrainz', external_id: MBID }],
            editions: 5,
            evented_editions: 2,
            repaired_editions: 1,
            work_disagreements: 1,
            first_event: '2025-11-14T00:00:00Z',
            last_event: '2026-09-03T12:00:00Z',
        },
    ],
    total: 120,
    page: 1,
    page_size: 50,
    refreshed_at: REFRESHED_AT,
};

const LINK_ADDED = {
    at: '2025-11-14T00:00:00Z', type: 'link_added', provider: 'musicbrainz', external_id: MBID,
    discogs_id_before: null, discogs_id_after: '900001', source_ref: '20261024-001001', decision_ref: 'b-1',
    dated_from: 'musicbrainz_fullexport', rule: 'ADR 0016 2026-10-24 amendment',
};

const WORK_PAYLOAD = {
    work: {
        gm_id: WORK,
        requested_id: FORMER,
        state: 'linked',
        providers: [{ provider: 'discogs', external_id: '900101', valid_from: '2026-08-01T02:00:00Z', valid_to: null }],
        events: [{ at: '2026-08-01T02:00:00Z', type: 'minted', item: WORK, providers: ['discogs'], dated_from: 'groovemap', rule: 'ADR 0009' }],
    },
    as_of: '2026-09-03T00:00:00Z',
    editions: [
        {
            gm_id: EDITION,
            state: 'linked',
            primary_work: WORK,
            works: [WORK],
            work_disagreement: false,
            provider_rows: [
                { provider: 'discogs', external_id: '900001' },
                { provider: 'musicbrainz', external_id: MBID, names_discogs_release: '900001' },
            ],
            former_items: [{ gm_id: FORMER, superseded_at: '2026-09-02T12:00:00Z', cause: 'catalog_reattachment', decision_ref: 'a-7' }],
            evidence: [],
            events: [
                LINK_ADDED,
                { at: '2026-08-01T02:00:00Z', type: 'minted', item: EDITION, providers: ['discogs'], dated_from: 'groovemap', rule: 'ADR 0009' },
                {
                    at: '2026-09-02T12:00:00Z', type: 'reattached', superseded: FORMER, survivor: EDITION, decision_ref: 'a-7',
                    compressed: false, moves: { collection_items: 2 }, dated_from: 'groovemap', rule: 'ADR 0014 section 8',
                },
                { ...LINK_ADDED, at: '2026-02-01T00:00:00Z', type: 'link_changed', discogs_id_before: '900000', discogs_id_after: '900001' },
            ],
        },
        {
            gm_id: SPLIT_EDITION,
            state: 'split',
            primary_work: WORK,
            works: [WORK, OTHER_WORK],
            work_disagreement: true,
            provider_rows: [{ provider: 'discogs', external_id: '900002' }],
            former_items: [],
            evidence: [],
            events: [],
        },
    ],
    edition_count: 5,
    truncated: true,
    refreshed_at: REFRESHED_AT,
};

function loadAdminDashboardClass() {
    const withoutAutoInit = ADMIN_SOURCE.replace(/document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/, '');
    vm.runInThisContext(`(function() {\n${withoutAutoInit}\nglobalThis.__AdminDashboard = AdminDashboard;\n})();`, { filename: ADMIN_JS_PATH });
    return globalThis.__AdminDashboard;
}

// The Ledger panel and the elements the constructor and the tab switch touch.
function setupLedgerDOM() {
    document.body.replaceChildren();
    for (const id of ['login-view', 'console-view', 'toast']) {
        const element = document.createElement('div');
        element.id = id;
        document.body.appendChild(element);
    }
    document.body.appendChild(document.importNode(SHELL.getElementById('tab-btn-ledger'), true));
    document.body.appendChild(document.importNode(SHELL.getElementById('tab-ledger'), true));
}

function jsonResponse(status, body, headers = {}) {
    const lowered = Object.fromEntries(Object.entries(headers).map(([name, value]) => [name.toLowerCase(), value]));
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
        headers: { get: (name) => lowered[name.toLowerCase()] ?? null },
    };
}

// catalog-api's answer from the works list and the density strip before the ledger's first refresh.
const notRefreshed = (retryAfter = '60') => jsonResponse(503, NOT_REFRESHED, retryAfter === null ? {} : { 'Retry-After': retryAfter });

// Answer ledger reads by path; record every URL asked for.
function installFetch({ density = DENSITY, works = WORKS, work = WORK_PAYLOAD } = {}) {
    const fetchMock = vi.fn(async (url) => {
        const path = new URL(url, 'http://console.test').pathname;
        if (path === '/admin/api/identity/ledger/density') return jsonResponse(200, density);
        if (path === '/admin/api/identity/ledger/works') return jsonResponse(200, works);
        if (path.startsWith('/admin/api/identity/ledger/works/')) return typeof work === 'function' ? work(url) : jsonResponse(200, work);
        return jsonResponse(404, {});
    });
    globalThis.fetch = fetchMock;
    return fetchMock;
}

const requested = (fetchMock) => fetchMock.mock.calls.map(([url]) => new URL(url, 'http://console.test'));
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
const text = (id) => document.getElementById(id).textContent;

class FakeChart {
    static instances = [];
    constructor(canvas, config) {
        this.canvas = canvas;
        this.config = config;
        this.destroyed = false;
        this.resize = vi.fn();
        FakeChart.instances.push(this);
    }
    destroy() { this.destroyed = true; }
}

describe('the Ledger tab', () => {
    let app;

    beforeEach(() => {
        setupLedgerDOM();
        localStorage.clear();
        FakeChart.instances = [];
        globalThis.Chart = FakeChart;
        // jsdom has no 2D canvas; the hatch falls back to a translucent fill.
        vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
        const AdminDashboard = loadAdminDashboardClass();
        installFetch();
        // The session itself rides an HttpOnly cookie the browser sends; these tests start signed in.
        AdminDashboard.prototype.start = vi.fn();
        app = new AdminDashboard();
        app.signedIn = true;
    });

    afterEach(() => {
        vi.restoreAllMocks();
        delete globalThis.Chart;
    });

    describe('mounting', () => {
        it('reads nothing until the tab is shown, then the strip and the first page', async () => {
            const fetchMock = installFetch();
            expect(fetchMock).not.toHaveBeenCalled();
            expect(document.getElementById('tab-ledger').style.display).toBe('none');

            app.switchTab('ledger');
            await flush();

            expect(document.getElementById('tab-ledger').style.display).toBe('block');
            const urls = requested(fetchMock);
            expect(urls.map((url) => url.pathname)).toEqual(['/admin/api/identity/ledger/density', '/admin/api/identity/ledger/works']);
            expect(urls[0].searchParams.get('kind')).toBe('release');
            expect(Object.fromEntries(urls[1].searchParams)).toEqual({ page: '1', page_size: '50' });
            expect(fetchMock.mock.calls[0][1].headers['X-GrooveMap-Console']).toBe('1');
            expect(fetchMock.mock.calls[0][1].headers.Authorization).toBeUndefined();
        });

        it('draws the chart only once its tab is visible, and resizes it instead of re-reading on return', async () => {
            const fetchMock = installFetch();
            app.switchTab('ledger');
            await flush();
            expect(FakeChart.instances).toHaveLength(1);
            expect(document.getElementById('tab-ledger').style.display).toBe('block');

            app.switchTab('overview');
            app.switchTab('ledger');
            await flush();

            expect(fetchMock).toHaveBeenCalledTimes(2);
            expect(FakeChart.instances[0].resize).toHaveBeenCalledTimes(1);
        });

        it('re-reads everything, including an open work, on Refresh', async () => {
            const fetchMock = installFetch();
            app.switchTab('ledger');
            await flush();
            await app.openLedgerWork(WORK);
            fetchMock.mockClear();

            document.getElementById('ledger-refresh-btn').click();
            await flush();

            expect(requested(fetchMock).map((url) => url.pathname).sort()).toEqual([
                '/admin/api/identity/ledger/density',
                '/admin/api/identity/ledger/works',
                `/admin/api/identity/ledger/works/${WORK}`,
            ]);
            expect(FakeChart.instances[0].destroyed).toBe(true);
            expect(FakeChart.instances).toHaveLength(2);
        });
    });

    describe('density strip', () => {
        beforeEach(async () => {
            app.switchTab('ledger');
            await flush();
        });

        it('fills every month between the first and last bucket, so the axis is time', () => {
            const { labels } = FakeChart.instances[0].config.data;
            expect(labels).toEqual(['2025-11', '2025-12', '2026-01', '2026-02']);
        });

        it('splits each event type by provenance and stacks the counts by month', () => {
            const { datasets } = FakeChart.instances[0].config.data;
            expect(datasets.map((dataset) => [dataset.label, dataset.datedFrom, dataset.data])).toEqual([
                ['Minted', 'groovemap', [0, 0, 120, 0]],
                ['Link added, dated from the MusicBrainz full export', 'musicbrainz_fullexport', [40, 0, 0, 0]],
                ['Link changed', 'groovemap', [0, 0, 0, 2]],
                ['Link changed, dated from the MusicBrainz full export', 'musicbrainz_fullexport', [0, 0, 0, 3]],
            ]);
            expect(datasets.every((dataset) => dataset.stack === 'events')).toBe(true);
            const { scales } = FakeChart.instances[0].config.options;
            expect(scales.x.stacked && scales.y.stacked).toBe(true);
        });

        it('marks backfilled segments and months visibly', () => {
            const { config } = FakeChart.instances[0];
            const backfilled = config.data.datasets.filter((dataset) => dataset.datedFrom === 'musicbrainz_fullexport');
            const recorded = config.data.datasets.filter((dataset) => dataset.datedFrom === 'groovemap');
            expect(backfilled.every((dataset) => dataset.borderWidth === 1 && dataset.backgroundColor !== dataset.borderColor)).toBe(true);
            expect(recorded.every((dataset) => dataset.borderWidth === 0 && dataset.backgroundColor === dataset.borderColor)).toBe(true);

            const tickColor = config.options.scales.x.ticks.color;
            const colors = config.data.labels.map((_, index) => tickColor({ index }));
            expect(colors[0]).toBe(colors[3]);
            expect(colors[1]).not.toBe(colors[0]);
            expect(colors[2]).toBe(colors[1]);

            expect(text('ledger-density-backfill')).toContain('dated from the MusicBrainz full export');
            expect(text('ledger-density-backfill')).toContain('2025-11, 2026-02.');
            expect(document.getElementById('ledger-density-backfill').style.display).toBe('');

            const legend = [...document.querySelectorAll('#ledger-density-legend > span')];
            expect(legend.map((item) => item.textContent)).toEqual([
                'Minted',
                'Link added, dated from the MusicBrainz full export',
                'Link changed',
                'Link changed, dated from the MusicBrainz full export',
            ]);
            expect(legend.filter((item) => item.querySelector('.ledger-swatch-backfill')).map((item) => item.dataset.datedFrom))
                .toEqual(['musicbrainz_fullexport', 'musicbrainz_fullexport']);
        });

        it('re-reads the chosen kind and says so when it has no events', async () => {
            const fetchMock = installFetch({ density: { kind: 'label', buckets: [] } });
            const kind = document.getElementById('ledger-kind');
            kind.value = 'label';
            kind.dispatchEvent(new Event('change'));
            await flush();

            expect(requested(fetchMock)[0].searchParams.get('kind')).toBe('label');
            expect(FakeChart.instances[0].destroyed).toBe(true);
            expect(FakeChart.instances).toHaveLength(1);
            expect(document.getElementById('ledger-density-empty').style.display).toBe('');
            expect(document.getElementById('ledger-density-backfill').style.display).toBe('none');
            expect(document.getElementById('ledger-density-legend').children).toHaveLength(0);
        });

        it('shows catalog-api refusing the read', async () => {
            globalThis.fetch = vi.fn(async () => jsonResponse(503, { detail: 'Service not ready' }));
            await app.fetchLedgerDensity();
            expect(document.getElementById('ledger-density-error').style.display).toBe('');
            expect(text('ledger-density-error-msg')).toBe('Service not ready');
            expect(document.getElementById('ledger-density-loading').style.display).toBe('none');
        });
    });

    describe('works table', () => {
        beforeEach(async () => {
            app.switchTab('ledger');
            await flush();
        });

        it('renders one row per work with its counts, providers, and UTC times', () => {
            const rows = document.querySelectorAll('#ledger-works-body tr');
            expect(rows).toHaveLength(1);
            const cells = [...rows[0].children].map((cell) => cell.textContent);
            expect(cells).toEqual([WORK, `discogs 900101musicbrainz ${MBID}`, '5', '2', '1', '1', '2025-11-14 00:00 UTC', '2026-09-03 12:00 UTC']);
            expect(rows[0].querySelector('button.ledger-work-link').textContent).toBe(WORK);
        });

        it('pages like the audit log', async () => {
            expect(text('ledger-page-info')).toBe('Page 1 of 3 (120 works)');
            expect(document.getElementById('ledger-prev-btn').disabled).toBe(true);
            expect(document.getElementById('ledger-next-btn').disabled).toBe(false);

            const fetchMock = installFetch({ works: { ...WORKS, page: 3 } });
            document.getElementById('ledger-next-btn').click();
            await flush();

            expect(requested(fetchMock)[0].searchParams.get('page')).toBe('2');
            expect(text('ledger-page-info')).toBe('Page 3 of 3 (120 works)');
            expect(document.getElementById('ledger-next-btn').disabled).toBe(true);
            expect(document.getElementById('ledger-prev-btn').disabled).toBe(false);
        });

        it('forwards the event, since and until filters and returns to the first page', async () => {
            app.state.ledgerPage = 3;
            const fetchMock = installFetch();
            document.getElementById('ledger-event-filter').value = 'link_changed';
            document.getElementById('ledger-since').value = '2025-11-01';
            const until = document.getElementById('ledger-until');
            until.value = '2026-02-28';
            until.dispatchEvent(new Event('change'));
            await flush();

            expect(Object.fromEntries(requested(fetchMock)[0].searchParams)).toEqual({
                page: '1',
                page_size: '50',
                event: 'link_changed',
                since: '2025-11-01T00:00:00Z',
                // Until names the last day included; catalog-api's until is exclusive.
                until: '2026-03-01T00:00:00Z',
            });
        });

        it('offers every event the works list can filter on, and not minted', () => {
            const options = [...document.querySelectorAll('#ledger-event-filter option')].map((option) => option.value);
            expect(options).toEqual(['', 'linked_at_ingest', 'reattached', 'promoted', 'reverted', 'evidence_attached', 'alias_closed', 'link_added', 'link_changed', 'link_removed']);
        });

        it('says when no work matches and hides the pager', () => {
            app.renderLedgerWorks({ works: [], total: 0, page: 1, page_size: 50 });
            expect(text('ledger-works-body')).toBe('No works carry identity events for these filters');
            expect(document.getElementById('ledger-pagination').style.display).toBe('none');
        });

        it('drops a superseded read instead of drawing it over a newer one', async () => {
            let release;
            globalThis.fetch = vi.fn()
                .mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve(jsonResponse(200, { ...WORKS, works: [] })); }))
                .mockImplementationOnce(async () => jsonResponse(200, WORKS));
            const slow = app.fetchLedgerWorks();
            await app.fetchLedgerWorks();
            release();
            await slow;
            expect(document.querySelectorAll('#ledger-works-body tr[data-work]')).toHaveLength(1);
        });
    });

    describe('drill-down', () => {
        beforeEach(async () => {
            app.switchTab('ledger');
            await flush();
        });

        it('opens a work from its row and reads it now', async () => {
            const fetchMock = installFetch();
            document.querySelector('#ledger-works-body button.ledger-work-link').click();
            await flush();

            const url = requested(fetchMock)[0];
            expect(url.pathname).toBe(`/admin/api/identity/ledger/works/${WORK}`);
            expect(url.search).toBe('');
            expect(document.getElementById('ledger-work').style.display).toBe('');
            expect(text('ledger-work-id')).toBe(WORK);
        });

        it('reads the work at a chosen as_of, read as UTC, and back at now', async () => {
            await app.openLedgerWork(WORK);
            const fetchMock = installFetch();
            document.getElementById('ledger-as-of').value = '2026-09-03T00:00';
            document.getElementById('ledger-as-of-btn').click();
            await flush();
            expect(requested(fetchMock)[0].searchParams.get('as_of')).toBe('2026-09-03T00:00:00Z');

            document.getElementById('ledger-as-of').value = '2026-09-03T00:00:05';
            document.getElementById('ledger-as-of-btn').click();
            await flush();
            expect(requested(fetchMock)[1].searchParams.get('as_of')).toBe('2026-09-03T00:00:05Z');

            document.getElementById('ledger-now-btn').click();
            await flush();
            expect(requested(fetchMock)[2].search).toBe('');
            expect(document.getElementById('ledger-as-of').value).toBe('');
        });

        it('shows the reading time, the edition count, a truncation, and a superseded request', async () => {
            await app.openLedgerWork(WORK);
            expect(text('ledger-work-status')).toBe(
                `Read at 2026-09-03 00:00 UTC · 2 of 5 editions (truncated) · ${FORMER} was superseded into this work`,
            );
        });

        it('shows each edition with its state, work parents, provider rows, former items and events', async () => {
            await app.openLedgerWork(WORK);
            const editions = [...document.querySelectorAll('#ledger-editions article.ledger-edition')];
            expect(editions.map((edition) => edition.dataset.edition)).toEqual([EDITION, SPLIT_EDITION]);

            const [linked, split] = editions;
            expect(linked.querySelector('.ledger-badge').textContent).toBe('linked');
            expect(linked.querySelector('.ledger-disagreement')).toBeNull();
            const fields = Object.fromEntries([...linked.querySelectorAll('dt')].map((dt) => [dt.textContent, dt.nextElementSibling.textContent]));
            expect(fields).toEqual({
                'Primary work': `${WORK} (this work)`,
                'Other works': '—',
                'Provider rows': `discogs 900001; musicbrainz ${MBID} (names Discogs 900001)`,
                'Former items': `${FORMER} superseded 2026-09-02 12:00 UTC (catalog_reattachment)`,
            });
            const events = [...linked.querySelectorAll('.ledger-events li')];
            expect(events.map((event) => event.dataset.type)).toEqual(['link_added', 'minted', 'reattached', 'link_changed']);
            expect(events[0].querySelector('.ledger-detail').textContent).toBe(`musicbrainz ${MBID}: Discogs none to 900001`);
            expect(events[2].querySelector('.ledger-detail').textContent).toBe(`${FORMER} into ${EDITION}; moved collection_items 2`);
            expect(events[2].querySelector('.ledger-badge').title).toBe('ADR 0014 section 8');

            expect(split.querySelector('.ledger-badge').textContent).toBe('split');
            expect(split.querySelector('.ledger-disagreement').textContent).toBe('Work disagreement');
            const other = split.querySelector('dd button.ledger-work-link');
            expect(other.textContent).toBe(OTHER_WORK);
            expect(split.textContent).toContain('No events at this time.');
        });

        it('opens the other work of a disagreement from the edition', async () => {
            await app.openLedgerWork(WORK);
            const fetchMock = installFetch();
            document.querySelector('[data-edition="' + SPLIT_EDITION + '"] dd button.ledger-work-link').click();
            await flush();
            expect(requested(fetchMock)[0].pathname).toBe(`/admin/api/identity/ledger/works/${OTHER_WORK}`);
        });

        it('labels every backfilled event as dated from the MusicBrainz full export, and no recorded one', async () => {
            await app.openLedgerWork(WORK);
            const events = [...document.querySelectorAll('#tab-ledger .ledger-events li')];
            const backfilled = events.filter((event) => event.dataset.datedFrom === 'musicbrainz_fullexport');
            const recorded = events.filter((event) => event.dataset.datedFrom === 'groovemap');
            expect(backfilled).toHaveLength(2);
            expect(recorded).toHaveLength(3);
            for (const event of backfilled) {
                expect(event.querySelector('.ledger-backfill').textContent).toBe('dated from the MusicBrainz full export');
            }
            for (const event of recorded) {
                expect(event.querySelector('.ledger-backfill')).toBeNull();
                expect(event.textContent).toContain('recorded by GrooveMap');
            }
        });

        it('treats event text as text', async () => {
            const hostile = '<img src=x onerror="globalThis.pwned=1">';
            installFetch({
                work: {
                    ...WORK_PAYLOAD,
                    work: { ...WORK_PAYLOAD.work, gm_id: hostile },
                    editions: [{ ...WORK_PAYLOAD.editions[0], gm_id: hostile, events: [{ ...LINK_ADDED, external_id: hostile }] }],
                },
            });
            await app.openLedgerWork(WORK);
            expect(document.querySelector('#tab-ledger img')).toBeNull();
            expect(text('ledger-work-id')).toBe(hostile);
        });

        it('shows a missing work and closes', async () => {
            installFetch({ work: () => jsonResponse(404, { detail: 'Work not found' }) });
            await app.openLedgerWork(WORK);
            expect(text('ledger-work-error-msg')).toBe('Work not found');
            expect(document.getElementById('ledger-work-error').style.display).toBe('');

            document.getElementById('ledger-work-close').click();
            expect(document.getElementById('ledger-work').style.display).toBe('none');
            expect(app.state.ledgerWorkId).toBeNull();
        });

        it('says when no edition is recorded at the chosen time', async () => {
            installFetch({ work: { ...WORK_PAYLOAD, editions: [], edition_count: 0, truncated: false } });
            await app.openLedgerWork(WORK);
            expect(text('ledger-editions')).toBe('No editions are recorded for this work at this time.');
        });
    });

    describe('freshness', () => {
        it('says when the strip, the works table and the drill-down were last refreshed', async () => {
            app.switchTab('ledger');
            await flush();
            await app.openLedgerWork(WORK);

            for (const id of ['ledger-density-asof', 'ledger-works-asof', 'ledger-work-asof']) {
                expect(text(id)).toBe('Ledger as of 2026-10-25 21:05 UTC');
                expect(document.getElementById(id).title).toBe(REFRESHED_AT);
            }
            expect(document.getElementById('ledger-building').style.display).toBe('none');
        });

        it('reads "not yet refreshed" for a null refreshed_at, which one work has before the first refresh', async () => {
            installFetch({ work: { ...WORK_PAYLOAD, refreshed_at: null } });
            app.switchTab('ledger');
            await flush();
            await app.openLedgerWork(WORK);

            expect(text('ledger-work-asof')).toBe('Ledger not yet refreshed');
            expect(document.getElementById('ledger-work-asof').title).toBe('');
            expect(text('ledger-work-id')).toBe(WORK);
            expect(text('ledger-density-asof')).toBe('Ledger as of 2026-10-25 21:05 UTC');
        });
    });

    describe('the ledger being built', () => {
        const SEC = 1000;

        beforeEach(() => {
            vi.useFakeTimers();
        });

        afterEach(() => {
            app.stopLedgerRetries();
            vi.useRealTimers();
        });

        // The strip and the list answer from `reads`, one answer per read in turn, then the last.
        function answerInTurn(reads) {
            const calls = { density: 0, works: 0 };
            const fetchMock = vi.fn(async (url) => {
                const path = new URL(url, 'http://console.test').pathname;
                const name = path.endsWith('/density') ? 'density' : 'works';
                const list = reads[name];
                return list[Math.min(calls[name]++, list.length - 1)]();
            });
            globalThis.fetch = fetchMock;
            return fetchMock;
        }

        const countOf = (fetchMock, name) => requested(fetchMock).filter((url) => url.pathname.endsWith(name === 'density' ? '/density' : '/works')).length;

        it('shows a state, not an error, for a 503 from the works list and the strip', async () => {
            answerInTurn({ density: [() => notRefreshed()], works: [() => notRefreshed()] });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);

            const notice = document.getElementById('ledger-building');
            expect(notice.style.display).toBe('');
            expect(notice.textContent).toContain('The ledger is being built; it refreshes after loads and identity runs');
            expect(notice.getAttribute('role')).toBe('status');
            expect(document.getElementById('ledger-density-error').style.display).toBe('none');
            expect(document.getElementById('ledger-works-error').style.display).toBe('none');
            expect(document.getElementById('ledger-density-loading').style.display).toBe('none');
            expect(document.getElementById('ledger-works-loading').style.display).toBe('none');
            expect(text('ledger-density-asof')).toBe('Ledger not yet refreshed');
            expect(text('ledger-works-asof')).toBe('Ledger not yet refreshed');
            expect(text('ledger-works-body')).toBe('No works to list until the ledger is built');
            expect(document.getElementById('ledger-density-empty').style.display).toBe('none');
            expect(FakeChart.instances).toHaveLength(0);
        });

        it('asks again after Retry-After, and shows the ledger once it is built', async () => {
            const fetchMock = answerInTurn({
                density: [() => notRefreshed('7'), () => jsonResponse(200, DENSITY)],
                works: [() => notRefreshed('7'), () => jsonResponse(200, WORKS)],
            });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);
            expect(fetchMock).toHaveBeenCalledTimes(2);

            await vi.advanceTimersByTimeAsync(6 * SEC);
            expect(fetchMock).toHaveBeenCalledTimes(2);
            expect(document.getElementById('ledger-building').style.display).toBe('');

            await vi.advanceTimersByTimeAsync(1 * SEC);
            expect(fetchMock).toHaveBeenCalledTimes(4);
            expect(document.getElementById('ledger-building').style.display).toBe('none');
            expect(document.querySelectorAll('#ledger-works-body tr[data-work]')).toHaveLength(1);
            expect(FakeChart.instances).toHaveLength(1);
            expect(text('ledger-works-asof')).toBe('Ledger as of 2026-10-25 21:05 UTC');
            expect(text('ledger-density-asof')).toBe('Ledger as of 2026-10-25 21:05 UTC');

            // Built: nothing is left waiting to ask again.
            await vi.advanceTimersByTimeAsync(10 * 60 * SEC);
            expect(fetchMock).toHaveBeenCalledTimes(4);
        });

        it('keeps asking while the ledger is still being built', async () => {
            const fetchMock = answerInTurn({ density: [() => notRefreshed('2')], works: [() => notRefreshed('2')] });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);
            await vi.advanceTimersByTimeAsync(4 * SEC);

            expect(countOf(fetchMock, 'works')).toBe(3);
            expect(countOf(fetchMock, 'density')).toBe(3);
            expect(document.getElementById('ledger-building').style.display).toBe('');
        });

        it('waits a minute when Retry-After is missing, and keeps a silly one within bounds', async () => {
            const fetchMock = answerInTurn({ density: [() => notRefreshed(null)], works: [() => notRefreshed('0')] });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);

            await vi.advanceTimersByTimeAsync(1 * SEC);
            expect(countOf(fetchMock, 'works')).toBe(2);
            expect(countOf(fetchMock, 'density')).toBe(1);
            await vi.advanceTimersByTimeAsync(59 * SEC);
            expect(countOf(fetchMock, 'density')).toBe(2);
        });

        it('reads Retry-After given as an HTTP date', async () => {
            vi.setSystemTime(new Date('2026-10-25T21:00:00Z'));
            const fetchMock = answerInTurn({
                density: [() => notRefreshed('Sun, 25 Oct 2026 21:00:05 GMT'), () => jsonResponse(200, DENSITY)],
                works: [() => jsonResponse(200, WORKS)],
            });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);
            await vi.advanceTimersByTimeAsync(4 * SEC);
            expect(countOf(fetchMock, 'density')).toBe(1);
            await vi.advanceTimersByTimeAsync(1 * SEC);
            expect(countOf(fetchMock, 'density')).toBe(2);
        });

        it('hides the notice once the only read that was waiting is answered, and leaves the other alone', async () => {
            answerInTurn({ density: [() => notRefreshed('5'), () => jsonResponse(200, DENSITY)], works: [() => jsonResponse(200, WORKS)] });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);
            expect(document.getElementById('ledger-building').style.display).toBe('');
            expect(document.querySelectorAll('#ledger-works-body tr[data-work]')).toHaveLength(1);
            expect(text('ledger-works-asof')).toBe('Ledger as of 2026-10-25 21:05 UTC');

            await vi.advanceTimersByTimeAsync(5 * SEC);
            expect(document.getElementById('ledger-building').style.display).toBe('none');
        });

        it('replaces a waiting retry with a newer read instead of reading twice', async () => {
            const fetchMock = answerInTurn({ density: [() => jsonResponse(200, DENSITY)], works: [() => notRefreshed('30'), () => jsonResponse(200, WORKS)] });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);
            expect(countOf(fetchMock, 'works')).toBe(1);

            document.getElementById('ledger-refresh-btn').click();
            await vi.advanceTimersByTimeAsync(0);
            expect(countOf(fetchMock, 'works')).toBe(2);
            expect(document.getElementById('ledger-building').style.display).toBe('none');

            await vi.advanceTimersByTimeAsync(60 * SEC);
            expect(countOf(fetchMock, 'works')).toBe(2);
        });

        it('stops asking when the session ends', async () => {
            const fetchMock = answerInTurn({ density: [() => notRefreshed('5')], works: [() => notRefreshed('5')] });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);

            app.showLogin('signed-out');
            await vi.advanceTimersByTimeAsync(60 * SEC);
            expect(fetchMock).toHaveBeenCalledTimes(2);
            expect(document.getElementById('ledger-building').style.display).toBe('none');
        });

        it('still shows any other 503 as an error, and does not retry it', async () => {
            const fetchMock = answerInTurn({
                density: [() => jsonResponse(503, { detail: 'Service not ready' }, { 'Retry-After': '1' })],
                works: [() => jsonResponse(200, WORKS)],
            });
            app.switchTab('ledger');
            await vi.advanceTimersByTimeAsync(0);
            await vi.advanceTimersByTimeAsync(60 * SEC);

            expect(text('ledger-density-error-msg')).toBe('Service not ready');
            expect(document.getElementById('ledger-density-error').style.display).toBe('');
            expect(document.getElementById('ledger-building').style.display).toBe('none');
            expect(countOf(fetchMock, 'density')).toBe(1);
        });

        it('does not treat a work that answers 503 as the ledger being built', async () => {
            installFetch({ work: () => jsonResponse(503, NOT_REFRESHED, { 'Retry-After': '1' }) });
            await app.openLedgerWork(WORK);
            await vi.advanceTimersByTimeAsync(60 * SEC);

            expect(text('ledger-work-error-msg')).toBe(NOT_REFRESHED.detail);
            expect(document.getElementById('ledger-building').style.display).toBe('none');
        });
    });

    it('builds the tab with DOM methods only', () => {
        const start = ADMIN_SOURCE.indexOf('class AdminLedger');
        const end = ADMIN_SOURCE.indexOf('class AdminDashboard extends AdminLedger');
        const ledger = ADMIN_SOURCE.slice(start, end);
        expect(start).toBeGreaterThan(0);
        expect(ledger).not.toMatch(/innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
        const helpers = ADMIN_SOURCE.slice(ADMIN_SOURCE.indexOf('const LEDGER_EVENT_LABELS'), ADMIN_SOURCE.indexOf('function _esc('));
        expect(helpers).not.toMatch(/innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
    });
});
