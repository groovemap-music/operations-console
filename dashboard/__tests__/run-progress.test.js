import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { DATASTORES, discogs, loadRunProgress, makeDashboard } from './overview-helpers.js';

/**
 * Run progress (spike gm-operations-console-2rf): the headline above each matrix reads "Finishes in
 * <ETA>, bottleneck <consumer>", or "Cannot finish: <reason>" when the matrix marks a queue stalled,
 * without a consumer, or with a growing DLQ, or nothing with no run in progress. The ETA uses the
 * 5-minute mean ack rate; a null or zero rate gives no ETA, never Infinity. Expected totals and loaded
 * counts come from /api/progress, and anything missing reads "progress unavailable".
 */
const { RunProgress, QueueHealth } = loadRunProgress();

const ok = { state: 'ok', kind: 'flowing', label: 'flowing' };
const dlqEmpty = { state: 'ok', kind: 'dlq-empty', label: 'DLQ empty' };
const cell = (extra = {}) => ({
    consumer: 'graphinator', consumerLabel: 'Graphinator', entity: 'releases', entityLabel: 'Releases',
    health: ok, dlq: dlqEmpty, expected: 1_000_000, loaded: 400_000, dlqDepth: 0, meanAck: 100, gap: null, points: [],
    ...extra,
});

describe('RunProgress.eta', () => {
    it('is the remaining records over the 5-minute mean ack rate', () => {
        expect(RunProgress.eta(cell())).toBe(6000);
    });

    it('does not count dead-lettered records as still to load', () => {
        expect(RunProgress.eta(cell({ dlqDepth: 100_000 }))).toBe(5000);
    });

    it('is 0 once every expected record is loaded, whatever the rate', () => {
        expect(RunProgress.eta(cell({ loaded: 1_000_000, meanAck: null }))).toBe(0);
    });

    it('is null, never Infinity, for a null or zero rate', () => {
        expect(RunProgress.eta(cell({ meanAck: null }))).toBeNull();
        expect(RunProgress.eta(cell({ meanAck: 0 }))).toBeNull();
    });

    it('is null without an expected total or a loaded count', () => {
        expect(RunProgress.eta(cell({ expected: null }))).toBeNull();
        expect(RunProgress.eta(cell({ loaded: null }))).toBeNull();
    });
});

describe('RunProgress.headline', () => {
    const headline = (cells, extra = {}) => RunProgress.headline({ running: true, error: null, cells, ...extra });

    it('is nothing when no extraction is running', () => {
        expect(RunProgress.headline({ running: false, error: null, cells: [cell({ health: { state: 'crit', kind: 'stalled' } })] })).toBeNull();
    });

    it('names the finish time and the slowest consumer', () => {
        const result = headline([cell(), cell({ consumer: 'tableinator', consumerLabel: 'Tableinator', meanAck: 50 })]);
        expect(result).toEqual({ kind: 'finishes', state: 'ok', text: 'Finishes in 3h20m', detail: 'bottleneck Tableinator' });
    });

    it('reads Loaded when nothing remains', () => {
        expect(headline([cell({ loaded: 1_000_000 })])).toMatchObject({ kind: 'loaded', text: 'Loaded' });
    });

    it.each([
        [{ health: { state: 'crit', kind: 'stalled', label: 'STALLED · no acks 7m' } }, 'Cannot finish: Graphinator · Releases stalled'],
        [{ health: { state: 'crit', kind: 'no-consumer', label: 'NO CONSUMER' } }, 'Cannot finish: Graphinator · Releases has no consumer'],
        [{ dlq: { state: 'crit', kind: 'dlq-growing', label: 'DLQ +40 / 5m' } }, 'Cannot finish: Graphinator · Releases DLQ growing'],
    ])('cannot finish with a blocked queue (%o)', (extra, text) => {
        expect(headline([cell(extra), cell({ consumerLabel: 'Tableinator' })])).toEqual({ kind: 'cannot-finish', state: 'crit', text });
    });

    it('lists every blocked queue, even when progress is otherwise unavailable', () => {
        const result = headline([
            cell({ health: { state: 'crit', kind: 'stalled' } }),
            cell({ consumerLabel: 'Tableinator', entityLabel: 'Artists', dlq: { state: 'crit', kind: 'dlq-growing' }, gap: 'no file progress for Artists' }),
        ], { error: 'history unavailable' });
        expect(result.text).toBe('Cannot finish: Graphinator · Releases stalled, Tableinator · Artists DLQ growing');
    });

    it('does not treat a flat DLQ as a reason the run cannot finish', () => {
        expect(headline([cell({ dlq: { state: 'warn', kind: 'dlq-static', label: 'DLQ 12' } })]).kind).toBe('finishes');
    });

    it('says progress is unavailable rather than inventing a total', () => {
        expect(headline([cell({ gap: 'no file progress for Releases', expected: null })])).toEqual({
            kind: 'unavailable', state: 'unknown', text: 'Progress unavailable: no file progress for Releases',
        });
        expect(headline([cell()], { error: 'history unavailable' }).text).toBe('Progress unavailable: history unavailable');
    });

    it('gives no finish time while a rate is unknown', () => {
        expect(headline([cell(), cell({ consumerLabel: 'Tableinator', meanAck: null })])).toEqual({
            kind: 'unknown', state: 'unknown', text: 'Finish time unknown: no ack rate for Tableinator · Releases',
        });
    });

    it('reads the matrix rules: a QueueHealth stall is the reason', () => {
        const samples = [];
        for (let i = 0; i < 31; i++) {
            QueueHealth.record(samples, { t: i * 2000, messages: 5000, messages_ready: 4800, messages_unacknowledged: 200, consumers: 1, message_rate: 10, ack_rate: 0 });
        }
        const result = headline([cell({ health: QueueHealth.assessQueue(samples), meanAck: QueueHealth.meanAckRate(samples) })]);
        expect(result.text).toBe('Cannot finish: Graphinator · Releases stalled');
    });
});

describe('RunProgress.gap', () => {
    const labels = { entityLabel: 'Releases', consumerLabel: 'Graphinator' };
    const totals = { extracted: 2_000_000, file_progress: 0.4, expected: 5_000_000 };

    it('is null when the totals and the baseline are known', () => {
        expect(RunProgress.gap({ ...labels, totals, acked: { loaded: 10 } })).toBeNull();
    });

    it('names what is missing', () => {
        expect(RunProgress.gap({ ...labels, totals: { extracted: null, expected: null }, acked: {} })).toBe("no history from the run's start");
        expect(RunProgress.gap({ ...labels, totals: { extracted: 0, expected: null }, acked: {} })).toBe('Releases not extracted yet');
        expect(RunProgress.gap({ ...labels, totals: { extracted: 10, file_progress: null, expected: null, waiting_for_checkpoint: true }, acked: {} }))
            .toBe("Releases waiting for the extractor's first progress checkpoint");
        expect(RunProgress.gap({ ...labels, totals: { extracted: 10, file_progress: null, expected: null }, acked: {} })).toBe('no file progress for Releases');
        expect(RunProgress.gap({ ...labels, totals, acked: { loaded: null } })).toBe('no acked baseline for Graphinator · Releases');
    });
});

describe('RunProgress.latestRuns', () => {
    it('takes the newest started run per pipeline; a record without a source is a Discogs run', () => {
        const runs = RunProgress.latestRuns([
            { id: 'p', status: 'pending', started_at: null },
            { id: 'b', status: 'running', started_at: '2026-10-24T08:00:00Z', source: 'musicbrainz' },
            { id: 'a', status: 'running', started_at: '2026-10-24T07:00:00Z' },
            { id: 'old', status: 'completed', started_at: '2026-10-20T07:00:00Z' },
        ]);
        expect(Object.fromEntries(Object.entries(runs).map(([k, v]) => [k, v.id]))).toEqual({ musicbrainz: 'b', discogs: 'a' });
        expect(RunProgress.isRunning(runs.discogs)).toBe(true);
        expect(RunProgress.isRunning({ status: 'completed', completed_at: '2026-10-24T09:00:00Z' })).toBe(false);
    });

    it("uses catalog-api's duration, or completed minus started", () => {
        expect(RunProgress.duration({ duration_seconds: 42 })).toBe(42);
        expect(RunProgress.duration({ started_at: '2026-10-24T07:00:00Z', completed_at: '2026-10-24T08:30:00Z' })).toBe(5400);
    });
});

describe('RunProgress burn-up', () => {
    const at = (s) => new Date(Date.UTC(2026, 9, 24, 7, 0, s)).toISOString();

    it('sums a consumer\'s queues at the times every one reported, over the total expected', () => {
        const cells = [cell({ points: [[at(0), 100], [at(30), 300]] }), cell({ entity: 'artists', points: [[at(30), 200], [at(60), 400]] })];
        expect(RunProgress.burnupSeries(cells, 1000)).toEqual([[Date.parse(at(30)), 0.5]]);
    });

    it('projects each consumer to 100 % at its ETA, and draws no projection without one', () => {
        const start = 0, now = 1000_000;
        const geometry = RunProgress.burnup({
            start, now, width: 434, height: 90,
            series: [{ consumer: 'graphinator', points: [[0, 0], [now, 0.5]], eta: 1000 }, { consumer: 'tableinator', points: [[now, 0.2]], eta: null }],
        });
        // Horizon: now + 1000 s x 1.05, so x spans 34..430 over 2,050 s.
        expect(geometry.nowX).toBeCloseTo(34 + (1000 / 2050) * 396, 3);
        expect(geometry.lines[0].projection).toBe(`${geometry.nowX.toFixed(1)},40.0 ${(34 + (2000 / 2050) * 396).toFixed(1)},4.0`);
        expect(geometry.lines[1].projection).toBeNull();
        expect(geometry.lines[0].points).toBe(`34.0,76.0 ${geometry.nowX.toFixed(1)},40.0`);
    });

    it('takes a consumer\'s slowest queue as its ETA, and none if any is unknown', () => {
        expect(RunProgress.consumerEta([cell(), cell({ loaded: 0 })])).toBe(10000);
        expect(RunProgress.consumerEta([cell(), cell({ meanAck: null })])).toBeNull();
    });
});

describe('the Overview with a run', () => {
    const START = Date.parse('2026-10-24T06:00:00Z');
    const NOW = Date.parse('2026-10-24T08:00:00Z');
    const RUN = { id: 'run-1', status: 'running', started_at: '2026-10-24T06:00:00Z', completed_at: null, record_counts: null };
    let dashboard;

    // Every queue of the pipeline at `messages` and `ack` /s, recorded for `count` polls 2 s apart.
    function feed({ count = 3, ack = 100, stalled = null } = {}) {
        const pipeline = dashboard.topology.pipelines[0];
        for (let i = 0; i < count; i++) {
            const queues = pipeline.consumers.flatMap(c => c.queues.flatMap(q => [
                { name: q.name, messages: 5000, messages_ready: 4800, messages_unacknowledged: 200, consumers: 1, message_rate: 10, ack_rate: q.name === stalled ? 0 : ack },
                { name: q.dead_letter_queue, messages: 0, messages_ready: 0, messages_unacknowledged: 0, consumers: 0, message_rate: 0, ack_rate: 0 },
            ]));
            dashboard.updateQueues('discogs', queues, NOW - (count - 1 - i) * 2000);
        }
    }

    // /api/progress for an hour-long Discogs run: every entity expects 1M, every queue loaded 400k.
    function progress({ releasesRatio = 0.4 } = {}) {
        const pipeline = dashboard.topology.pipelines[0];
        return {
            pipeline: 'discogs', start: new Date(START).toISOString(), end: new Date(NOW).toISOString(),
            entities: pipeline.entity_types.map(entity => (entity === 'releases' && releasesRatio === null
                ? { entity, extracted: 400_000, file_progress: null, expected: null }
                : { entity, extracted: 400_000, file_progress: 0.4, expected: 1_000_000 })),
            queues: pipeline.consumers.flatMap(c => c.queues.map(q => ({
                queue: q.name, consumer: c.name, entity: q.entity, baseline: 10, loaded: 400_000,
                points: [[new Date(START + 1800_000).toISOString(), 100_000], [new Date(NOW).toISOString(), 400_000]],
            }))),
        };
    }

    beforeEach(() => {
        vi.useFakeTimers({ now: NOW, toFake: ['Date'] });
        dashboard = makeDashboard();
        document.body.insertAdjacentHTML('beforeend', `
            <button data-overview-view="matrix" aria-pressed="true"></button><button data-overview-view="progress" aria-pressed="false"></button>
            <div id="run-progress" hidden></div>
            <table><tbody><tr id="extraction-run-run-1" tabindex="-1"><td>run</td></tr></tbody></table>`);
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
        dashboard.runs = { discogs: RUN };
        dashboard.runProgress = { discogs: { key: 'k', data: progress(), error: null } };
    });

    afterEach(() => {
        vi.useRealTimers();
        delete window.adminDashboard;
    });

    const headlineEl = () => document.querySelector('[data-run-headline="discogs"]');

    it('heads the matrix with the finish time and bottleneck', () => {
        feed({ ack: 100 });
        expect(headlineEl().hidden).toBe(false);
        expect(headlineEl().dataset.kind).toBe('finishes');
        // 600k remaining at 100/s on every queue.
        expect(headlineEl().textContent).toBe('Finishes in 1h40m, bottleneck Graphinator');
        const grid = document.querySelector('[data-matrix="discogs"]');
        expect(headlineEl().nextElementSibling).toBe(grid);
    });

    it('heads the matrix with the reason a stalled run cannot finish', () => {
        feed({ count: 31, stalled: 'groovemap-discogs-tableinator-releases' });
        expect(headlineEl().dataset.state).toBe('crit');
        expect(headlineEl().textContent).toBe('◆ Cannot finish: Tableinator · Releases stalled');
    });

    it('shows nothing above the matrix when no run is in progress', () => {
        dashboard.runs = { discogs: { ...RUN, status: 'completed', completed_at: '2026-10-24T07:00:00Z' } };
        feed();
        expect(headlineEl().hidden).toBe(true);
        expect(headlineEl().textContent).toBe('');
    });

    it('draws the Run progress view: a burn-up with projections, and per entity x consumer bars', () => {
        feed({ ack: 100 });
        dashboard.showOverviewView('progress');
        expect(document.getElementById('pipelines').hidden).toBe(true);
        expect(document.getElementById('run-progress').hidden).toBe(false);
        const section = document.getElementById('run-progress-discogs');
        expect(section.dataset.runState).toBe('running');
        expect(section.querySelector('[data-run-summary]').textContent).toBe('Finishes in 1h40m, bottleneck Graphinator');
        expect(section.querySelectorAll('.rp-burnup polyline.rp-line')).toHaveLength(2);
        expect(section.querySelectorAll('.rp-burnup polyline.rp-projection')).toHaveLength(2);
        const releases = section.querySelector('[data-run-entity="releases"]');
        expect(releases.querySelector('.rp-entity-head').textContent).toBe('Releases · 1.00M expected · 40% read');
        const row = releases.querySelector('[data-run-cell="graphinator-releases"]');
        expect(row.querySelector('.rp-pct').textContent).toBe('40%');
        expect(row.querySelector('.rp-eta').textContent).toBe('ETA 1h40m');
        expect(row.querySelector('.rp-bar').getAttribute('aria-label')).toBe('40% loaded, 40% extracted');
    });

    it('says progress unavailable for an entity without a file-progress gauge, and draws no burn-up', () => {
        dashboard.runProgress.discogs.data = progress({ releasesRatio: null });
        feed();
        expect(headlineEl().textContent).toBe('Progress unavailable: no file progress for Releases');
        dashboard.showOverviewView('progress');
        const section = document.getElementById('run-progress-discogs');
        expect(section.querySelector('.rp-burnup')).toBeNull();
        expect(section.querySelector('.rp-unavailable').textContent).toBe('Progress unavailable: no file progress for Releases');
        const releases = section.querySelector('[data-run-entity="releases"]');
        expect(releases.querySelector('.rp-entity-head').textContent).toBe('Releases · progress unavailable: no file progress for Releases');
        expect(releases.querySelector('[data-run-cell="graphinator-releases"]').textContent).toBe('Graphinatorprogress unavailable');
    });

    it("waits for the extractor's first checkpoint instead of reading the previous run's ratio", () => {
        const data = progress();
        data.entities = data.entities.map(e => (e.entity === 'releases'
            ? { entity: 'releases', extracted: 50_000, file_progress: null, expected: null, waiting_for_checkpoint: true } : e));
        dashboard.runProgress.discogs.data = data;
        feed();
        expect(headlineEl().textContent).toBe("Progress unavailable: Releases waiting for the extractor's first progress checkpoint");
        dashboard.showOverviewView('progress');
        const releases = document.querySelector('#run-progress-discogs [data-run-entity="releases"]');
        expect(releases.querySelector('.rp-entity-head').textContent).toBe("Releases · progress unavailable: Releases waiting for the extractor's first progress checkpoint");
        expect(releases.querySelector('.rp-pct')).toBeNull();
    });

    it('shows a completed run with its duration and final counts', () => {
        dashboard.runs = {
            discogs: { ...RUN, status: 'completed', completed_at: '2026-10-24T09:12:00Z', duration_seconds: 11520, record_counts: { releases: 1_000_000, masters: 900_000, artists: 800_000, labels: 300_000, total: 3_000_000 } },
        };
        feed();
        dashboard.showOverviewView('progress');
        const section = document.getElementById('run-progress-discogs');
        expect(section.dataset.runState).toBe('completed');
        expect(section.querySelector('[data-run-summary]').textContent).toBe('Completed in 3h12m · 3,000,000 records');
        expect(section.querySelector('[data-run-entity="releases"] .rp-entity-head').textContent).toBe('Releases · 1,000,000 extracted');
        expect(section.querySelector('[data-run-cell="tableinator-releases"] .rp-eta').textContent).toBe('400,000 loaded');
        expect(section.querySelectorAll('.rp-burnup polyline.rp-projection')).toHaveLength(0);
    });

    it('links the run to its row in the Extractions tab', async () => {
        window.adminDashboard = { switchTab: vi.fn() };
        dashboard.showOverviewView('progress');
        document.querySelector('[data-run-link="run-1"]').click();
        await Promise.resolve();
        expect(window.adminDashboard.switchTab).toHaveBeenCalledWith('extractions');
        const row = document.getElementById('extraction-run-run-1');
        expect(row.dataset.highlight).toBe('true');
        expect(document.activeElement).toBe(row);
    });

    it('returns to the matrix when the session ends', () => {
        dashboard.showOverviewView('progress');
        dashboard.showOverviewView('matrix');
        expect(document.querySelector('[data-overview-view="matrix"]').getAttribute('aria-pressed')).toBe('true');
        expect(document.getElementById('run-progress').hidden).toBe(true);
    });
});

describe('Dashboard.fetchRunProgress', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
        document.body.insertAdjacentHTML('beforeend', '<div id="run-progress" hidden></div>');
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
        Object.assign(dashboard, { active: true, token: 'tok', requestSessionCheck: vi.fn() });
    });

    afterEach(() => vi.unstubAllGlobals());

    const json = (status, body) => ({ status, ok: status < 400, json: async () => body });

    it('reads the runs through the admin proxy and a running run up to now', async () => {
        const fetch = vi.fn(async (url) => (String(url).startsWith('/admin/api/extractions')
            ? json(200, { extractions: [{ id: 'r', status: 'running', started_at: '2026-10-24T06:00:00Z' }] })
            : json(200, { pipeline: 'discogs', entities: [], queues: [] })));
        vi.stubGlobal('fetch', fetch);
        await dashboard.fetchRunProgress();
        expect(fetch.mock.calls.map(([url]) => url)).toEqual(['/admin/api/extractions', '/api/progress/discogs?start=2026-10-24T06%3A00%3A00Z']);
        expect(fetch.mock.calls[1][1].headers).toEqual({ Authorization: 'Bearer tok' });
        expect(dashboard.runProgress.discogs.data.pipeline).toBe('discogs');
    });

    it('reads a finished run up to its end, and only once', async () => {
        const fetch = vi.fn(async (url) => (String(url).startsWith('/admin/api/extractions')
            ? json(200, { extractions: [{ id: 'r', status: 'completed', started_at: '2026-10-24T06:00:00Z', completed_at: '2026-10-24T07:00:00Z' }] })
            : json(200, { pipeline: 'discogs', entities: [], queues: [] })));
        vi.stubGlobal('fetch', fetch);
        await dashboard.fetchRunProgress();
        await dashboard.fetchRunProgress();
        const progressCalls = fetch.mock.calls.map(([url]) => url).filter(url => url.startsWith('/api/progress'));
        expect(progressCalls).toEqual(['/api/progress/discogs?start=2026-10-24T06%3A00%3A00Z&end=2026-10-24T07%3A00%3A00Z']);
    });

    it('keeps "history unavailable" as the run\'s state, and hands a session refusal back to admin.js', async () => {
        let progressStatus = 503;
        let progressBody = { detail: 'history unavailable', reason: 'ConnectError' };
        vi.stubGlobal('fetch', vi.fn(async (url) => (String(url).startsWith('/admin/api/extractions')
            ? json(200, { extractions: [{ id: 'r', status: 'running', started_at: '2026-10-24T06:00:00Z' }] })
            : json(progressStatus, progressBody))));
        await dashboard.fetchRunProgress();
        expect(dashboard.runProgress.discogs).toMatchObject({ data: null, error: 'history unavailable' });
        expect(dashboard.requestSessionCheck).not.toHaveBeenCalled();

        progressStatus = 503;
        progressBody = { detail: 'catalog-api unavailable', reason: 'unavailable' };
        await dashboard.fetchRunProgress();
        expect(dashboard.requestSessionCheck).toHaveBeenCalledTimes(1);
    });

    it('forgets the progress of a pipeline with no run', async () => {
        dashboard.runProgress = { discogs: { key: 'old', data: {}, error: null } };
        vi.stubGlobal('fetch', vi.fn(async () => json(200, { extractions: [] })));
        await dashboard.fetchRunProgress();
        expect(dashboard.runProgress).toEqual({});
    });
});
