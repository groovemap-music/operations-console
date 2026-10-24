import { describe, it, expect, beforeEach, vi } from 'vitest';
import { DATASTORES, discogs, loadAttention, makeDashboard, queue as queueOf } from './overview-helpers.js';

/**
 * The attention bar lists what needs an operator: a non-empty DLQ, a backlog with no consumer, a
 * queue stalled for a minute, an unhealthy or unknown service, an unavailable datastore, a
 * pipeline whose queue metrics are unavailable, and the Grafana alerts the console serves. Queue
 * conditions are the matrix cells' own assessments, so the bar cannot disagree with the matrix.
 */
const WORK = 'groovemap-discogs-graphinator-artists';
const DLQ = `${WORK}.dlq`;
const SERVICE = (name, status) => ({ name, status });

describe('Attention.derive and Attention.overall', () => {
    const Attention = loadAttention();
    const cell = (health, dlq) => ({
        consumer: 'graphinator', consumerLabel: 'Graphinator', entity: 'artists', entityLabel: 'Artists', queue: WORK,
        health: { state: 'ok', kind: 'flowing', label: 'flowing', ...health },
        dlq: { state: 'ok', kind: 'dlq-empty', label: 'DLQ empty', ...dlq },
    });
    const pipeline = (extra = {}) => ({ id: 'discogs', label: 'Discogs', queuesUnavailable: false, services: [], cells: [], ...extra });
    const derive = (inputs) => Attention.derive({ pipelines: [], databases: [], alerts: [], ...inputs });

    it('is empty when nothing is wrong', () => {
        const items = derive({
            pipelines: [pipeline({ services: [SERVICE('graphinator', 'healthy'), SERVICE('extractor-discogs', 'extracting')], cells: [cell({}, {})] })],
            databases: [{ name: 'Neo4j', status: 'healthy' }],
        });
        expect(items).toEqual([]);
    });

    it('flags a backlog with no consumer as critical, linking to its cell', () => {
        const [item] = derive({ pipelines: [pipeline({ cells: [cell({ state: 'crit', kind: 'no-consumer', label: 'NO CONSUMER' })] })] });
        expect(item).toMatchObject({ severity: 'crit', kind: 'no-consumer', text: 'Discogs Artists → Graphinator: NO CONSUMER' });
        expect(item.target).toEqual({ type: 'cell', pipeline: 'discogs', consumer: 'graphinator', entity: 'artists' });
    });

    it('flags a stalled queue as critical', () => {
        const [item] = derive({ pipelines: [pipeline({ cells: [cell({ state: 'crit', kind: 'stalled', label: 'STALLED · no acks 1m' })] })] });
        expect(item).toMatchObject({ severity: 'crit', kind: 'stalled' });
    });

    it('flags a growing DLQ as critical and a static non-empty DLQ as a warning', () => {
        const growing = derive({ pipelines: [pipeline({ cells: [cell({}, { state: 'crit', kind: 'dlq-growing', label: 'DLQ +40 / 5m' })] })] });
        const flat = derive({ pipelines: [pipeline({ cells: [cell({}, { state: 'warn', kind: 'dlq-static', label: 'DLQ 12' })] })] });
        expect(growing[0]).toMatchObject({ severity: 'crit', kind: 'dlq-growing', target: { type: 'cell' } });
        expect(flat[0]).toMatchObject({ severity: 'warn', kind: 'dlq-static' });
    });

    it('ignores queues that are drained, flowing, or not yet reported', () => {
        const items = derive({ pipelines: [pipeline({ cells: [cell({ kind: 'drained' }), cell({ state: 'unknown', kind: 'missing' }, { state: 'unknown', kind: 'dlq-missing' })] })] });
        expect(items).toEqual([]);
    });

    it('flags an unhealthy service as critical and an unknown or starting one as a warning', () => {
        const items = derive({ pipelines: [pipeline({ services: [SERVICE('graphinator', 'unhealthy'), SERVICE('tableinator', 'unknown'), SERVICE('x', 'starting')] })] });
        expect(items.map(i => [i.severity, i.kind, i.text])).toEqual([
            ['crit', 'service-unhealthy', 'Discogs: graphinator is unhealthy'],
            ['warn', 'service-unknown', 'Discogs: tableinator is unknown'],
            ['warn', 'service-unknown', 'Discogs: x is starting'],
        ]);
        expect(items[0].target).toEqual({ type: 'service', service: 'graphinator' });
    });

    it('flags an unavailable datastore as critical, linking to the Storage tab', () => {
        const [item] = derive({ databases: [{ name: 'PostgreSQL', status: 'unhealthy' }, { name: 'Neo4j', status: 'healthy' }] });
        expect(item).toMatchObject({ severity: 'crit', kind: 'datastore-unavailable', text: 'PostgreSQL is unavailable', target: { type: 'tab', tab: 'storage' } });
    });

    it('flags unavailable queue metrics once per pipeline and skips that pipeline\'s cells', () => {
        const items = derive({ pipelines: [pipeline({ queuesUnavailable: true, cells: [cell({ state: 'crit', kind: 'stalled', label: 'x' })] })] });
        expect(items.map(i => [i.severity, i.kind])).toEqual([['warn', 'queues-unavailable']]);
    });

    it('lists Grafana alerts with their severity and link, and sorts critical items first', () => {
        const items = derive({
            pipelines: [pipeline({ cells: [cell({}, { state: 'warn', kind: 'dlq-static', label: 'DLQ 3' })] })],
            alerts: [{ name: 'HighLag', severity: 'crit', summary: 'lag > 10m', url: 'https://grafana.test/a' }, { name: 'Disk', severity: 'warn', summary: '', url: 'https://grafana.test/b' }],
        });
        expect(items.map(i => [i.severity, i.kind])).toEqual([['crit', 'grafana-alert'], ['warn', 'dlq-static'], ['warn', 'grafana-alert']]);
        expect(items[0]).toMatchObject({ text: 'HighLag: lag > 10m', target: { type: 'grafana', url: 'https://grafana.test/a' } });
        expect(items[2].text).toBe('Disk');
    });

    describe('overall', () => {
        const crit = { severity: 'crit' };
        const warn = { severity: 'warn' };
        it('is unknown only when nothing was reported', () => {
            expect(Attention.overall({ reported: false, items: [], services: [] }).key).toBe('unknown');
        });
        it('is operational when something was reported and nothing is wrong, even with no services listed', () => {
            expect(Attention.overall({ reported: true, items: [], services: [] })).toEqual({ key: 'operational', label: 'Operational' });
        });
        it('is critical, warning, or offline from the items and services', () => {
            expect(Attention.overall({ reported: true, items: [warn, crit], services: [SERVICE('a', 'healthy')] }).key).toBe('critical');
            expect(Attention.overall({ reported: true, items: [warn], services: [] }).key).toBe('warning');
            expect(Attention.overall({ reported: true, items: [crit], services: [SERVICE('a', 'unhealthy'), SERVICE('b', 'unhealthy')] }).key).toBe('offline');
        });
    });
});

describe('the attention bar and the header status', () => {
    let dashboard;
    const bar = () => document.getElementById('attention-bar');
    const items = () => [...document.querySelectorAll('#attention-list li')];
    const header = () => document.getElementById('operational-status-text');
    // 40 readings 2 s apart (78 s) of the artists queue and its DLQ.
    const run = (work, dlq, { services = [], databases = [] } = {}) => {
        for (let i = 0; i < 40; i++) {
            const queues = [queueOf(WORK, { messages: 5000, consumers: 1, message_rate: 3, ack_rate: 3, ...work }), queueOf(DLQ, { messages: 0, consumers: 0, ...dlq(i) })];
            dashboard.updateDashboard({ timestamp: new Date(1e12 + i * 2000).toISOString(), pipelines: { discogs: { services, queues, queues_available: true } }, databases });
        }
    };

    beforeEach(() => {
        dashboard = makeDashboard();
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
    });

    it('stays hidden and says Operational for a healthy reading with no services listed', () => {
        run({}, () => ({}));
        expect(bar().hidden).toBe(true);
        expect(items()).toHaveLength(0);
        expect(header().textContent).toBe('Operational');
    });

    it('says Operational for healthy services and datastores', () => {
        run({}, () => ({}), { services: [SERVICE('graphinator', 'healthy'), SERVICE('extractor-discogs', 'extracting')], databases: [{ name: 'Neo4j', status: 'healthy' }] });
        expect(header().textContent).toBe('Operational');
        expect(bar().hidden).toBe(true);
    });

    it('lists a stalled queue from the matrix\'s rules, with a count, and Critical in the header', () => {
        run({ ack_rate: 0, message_rate: 3 }, () => ({}));
        expect(bar().hidden).toBe(false);
        expect(document.getElementById('attention-summary').textContent).toBe('1 item needs attention · 1 critical · 0 warning');
        expect(items()[0].textContent).toContain('Discogs Artists → Graphinator: STALLED · no acks 1m');
        expect(header().textContent).toBe('Critical');
    });

    it('never lists a stall for null ack rates', () => {
        run({ ack_rate: null, message_rate: null }, () => ({}));
        expect(bar().hidden).toBe(true);
    });

    it('lists a growing DLQ as critical and a static one as a warning', () => {
        run({}, (i) => ({ messages: 100 + i * 10 }));
        expect(items()[0].dataset).toMatchObject({ severity: 'crit' });
        expect(items()[0].textContent).toContain('DLQ +390 / 5m');
        run({}, () => ({ messages: 100 }));
        expect(items()[0].dataset).toMatchObject({ severity: 'warn' });
        expect(header().textContent).toBe('Warning');
    });

    it('lists a backlog with no consumer', () => {
        run({ consumers: 0 }, () => ({}));
        expect(items()[0].textContent).toContain('NO CONSUMER');
    });

    it('lists unhealthy services and unavailable datastores, and clears when they recover', () => {
        run({}, () => ({}), { services: [SERVICE('graphinator', 'unhealthy'), SERVICE('tableinator', 'healthy')], databases: [{ name: 'Neo4j', status: 'unhealthy' }] });
        expect(items().map(li => li.dataset.attentionId)).toEqual(['service:discogs:graphinator', 'datastore:neo4j']);
        expect(header().textContent).toBe('Critical');
        run({}, () => ({}), { services: [SERVICE('graphinator', 'healthy')], databases: [{ name: 'Neo4j', status: 'healthy' }] });
        expect(bar().hidden).toBe(true);
        expect(header().textContent).toBe('Operational');
    });

    it('lists unavailable queue metrics', () => {
        dashboard.updateDashboard({ timestamp: new Date(1e12).toISOString(), pipelines: { discogs: { services: [], queues: [], queues_available: false } }, databases: [] });
        expect(items()[0].querySelector('button').dataset.attentionKind).toBe('queues-unavailable');
    });

    it('opens a cell\'s drawer from its item, and switches to the Overview first', () => {
        run({ ack_rate: 0, message_rate: 3 }, () => ({}));
        const switchTab = vi.fn();
        window.adminDashboard = { switchTab };
        items()[0].querySelector('button').click();
        expect(switchTab).toHaveBeenCalledWith('overview');
        expect(dashboard.drawerQueue).toEqual({ pipeline: 'discogs', consumer: 'graphinator', entity: 'artists' });
        delete window.adminDashboard;
    });

    it('switches to the Storage tab from a datastore item', () => {
        run({}, () => ({}), { databases: [{ name: 'Neo4j', status: 'unhealthy' }] });
        const switchTab = vi.fn();
        window.adminDashboard = { switchTab };
        items()[0].querySelector('button').click();
        expect(switchTab).toHaveBeenCalledWith('storage');
        delete window.adminDashboard;
    });

    it('collapses on request and re-opens when a new item appears', () => {
        run({ consumers: 0 }, () => ({}));
        // makeDashboard skips the constructor, which wires the toggle.
        document.getElementById('attention-toggle').addEventListener('click', () => {
            dashboard.attentionCollapsed = !dashboard.attentionCollapsed;
            dashboard.updateAttention();
        });
        document.getElementById('attention-toggle').click();
        expect(document.getElementById('attention-list').hidden).toBe(true);
        expect(document.getElementById('attention-toggle').getAttribute('aria-expanded')).toBe('false');
        run({ consumers: 0 }, () => ({ messages: 5 }));
        expect(document.getElementById('attention-list').hidden).toBe(false);
    });

    describe('Grafana alerts', () => {
        const respond = (status, body) => vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: status === 200, status, json: async () => body }));
        beforeEach(() => Object.assign(dashboard, { active: true, token: 't', _authHeaders: () => ({ Authorization: 'Bearer t' }), requestSessionCheck: vi.fn() }));

        it('adds firing alerts as links to Grafana', async () => {
            respond(200, { configured: true, available: true, note: null, alerts: [{ name: 'HighLag', severity: 'crit', summary: 'lag', url: 'https://grafana.test/a' }] });
            await dashboard.fetchAlerts();
            const link = items()[0].querySelector('a');
            expect(link.getAttribute('href')).toBe('https://grafana.test/a');
            expect(link.rel).toContain('noopener');
        });

        it('is silent when Grafana is not configured', async () => {
            respond(200, { configured: false, available: false, alerts: [], note: null });
            await dashboard.fetchAlerts();
            expect(bar().hidden).toBe(true);
        });

        it('keeps the derived items and shows a note when Grafana is unavailable', async () => {
            run({ consumers: 0 }, () => ({}));
            respond(200, { configured: true, available: false, alerts: [], note: 'Grafana alerts unavailable: showing the problems derived from the live feed only' });
            await dashboard.fetchAlerts();
            expect(items()).toHaveLength(1);
            expect(document.getElementById('attention-note').hidden).toBe(false);
            expect(document.getElementById('attention-note').textContent).toContain('Grafana alerts unavailable');
        });

        it('treats a failed request like an unavailable Grafana', async () => {
            vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
            await dashboard.fetchAlerts();
            expect(bar().hidden).toBe(false);
            expect(document.getElementById('attention-note').textContent).toContain('Grafana alerts unavailable');
        });

        it('hands a refused session back to admin.js', async () => {
            respond(401, {});
            await dashboard.fetchAlerts();
            expect(dashboard.requestSessionCheck).toHaveBeenCalled();
        });
    });
});
