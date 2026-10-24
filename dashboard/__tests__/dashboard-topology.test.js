import { describe, it, expect, beforeEach, vi } from 'vitest';
import { DATASTORES, discogs, makeDashboard, queue as queueOf, topologyOf } from './overview-helpers.js';

/**
 * Each pipeline is an entity x consumer matrix drawn from /api/topology and filled from live
 * metrics. A third consumer and a new entity type render, update, and stack on a phone with no
 * change to the markup or the script.
 */
const synthetic = () =>
    topologyOf({
        id: 'synthetic',
        label: 'Synthetic',
        prefix: 'acme-synthetic',
        producer: 'extractor-synthetic',
        consumers: [
            { name: 'alpha', datastore: 'neo4j' },
            { name: 'beta', datastore: 'postgresql' },
            { name: 'gamma', datastore: 'valkey' },
        ],
        entities: ['artists', 'release-groups', 'tracks'],
    });

const topology = (...pipelines) => ({ pipelines, datastores: [...DATASTORES, { id: 'valkey', label: 'Valkey' }] });
const text = (id) => document.getElementById(id)?.textContent;
const cell = (pipeline, consumer, entity) => document.getElementById(`${pipeline}-${consumer}-${entity}-cell`);
const parts = (el) => {
    const [cname, flag, count, meta, dlq] = el.children;
    return { cname: cname.textContent, flag: flag.textContent, count: count.textContent, meta: meta.textContent, dlq: dlq.textContent };
};
const push = (dashboard, pipeline, queues, timestamp) =>
    dashboard.updateDashboard({ timestamp, pipelines: { [pipeline]: { services: [], queues, queues_available: true } } });

describe('Overview matrix rendered from the topology', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
    });

    it('draws a hidden section per pipeline and reveals only those the metrics report', () => {
        dashboard.renderTopology(topology(discogs(), synthetic()));
        const hidden = (id) => document.getElementById(`pipeline-${id}`).classList.contains('hidden');
        expect([hidden('discogs'), hidden('synthetic')]).toEqual([true, true]);

        dashboard.updateDashboard({ pipelines: { synthetic: { services: [], queues: [], queues_available: true } } });
        expect([hidden('discogs'), hidden('synthetic')]).toEqual([true, false]);
    });

    it('lays out entity rows and consumer columns with their datastore, for any consumer and entity', () => {
        dashboard.renderTopology(topology(synthetic()));
        const heads = [...document.querySelectorAll('[data-matrix="synthetic"] .mx-colhead')];
        expect(heads.map((h) => h.id)).toEqual(['service-alpha', 'service-beta', 'service-gamma']);
        expect(heads.map((h) => h.querySelector('[data-datastore]').textContent)).toEqual(['Neo4j', 'PostgreSQL', 'Valkey']);
        expect([...document.querySelectorAll('[data-matrix="synthetic"] .mx-entity')].map((e) => e.textContent)).toEqual(['Artists', 'Release Groups', 'Tracks']);
        for (const consumer of ['alpha', 'beta', 'gamma']) {
            expect(text(`synthetic-${consumer}-status-badge`)).toBe('Unknown');
            for (const entity of ['artists', 'release-groups', 'tracks']) {
                const c = cell('synthetic', consumer, entity);
                expect(c.tagName).toBe('BUTTON');
                expect(c.dataset.queue).toBe(`acme-synthetic-${consumer}-${entity}`);
                expect(parts(c)).toMatchObject({ count: '—', meta: 'not reported' });
            }
        }
        expect(document.getElementById('service-extractor-synthetic')).not.toBeNull();
        expect(text('synthetic-extractor-synthetic-release-groups-state')).toBe('—');
        expect(document.querySelector('[data-matrix="synthetic"]').style.getPropertyValue('--mx-cols')).toBe('3');
    });

    it('draws one legend for the lag ramp and the state glyphs', () => {
        dashboard.renderTopology(topology(discogs(), synthetic()));
        const legends = document.querySelectorAll('[data-matrix-legend]');
        expect(legends.length).toBe(1);
        expect(legends[0].textContent).toContain('< 1m');
        expect(legends[0].textContent).toContain('≥ 1h');
        expect(legends[0].textContent).toContain('◆ critical');
        expect(legends[0].textContent).toContain('▲ warning');
        expect(legends[0].querySelectorAll('.mx-swatch').length).toBe(4);
    });

    it('fills cells from live queues by exact name: backlog, lag bin, and DLQ badge', () => {
        dashboard.renderTopology(topology(synthetic()));
        push(dashboard, 'synthetic', [
            queueOf('acme-synthetic-gamma-tracks', { messages: 1500, message_rate: 7.5, ack_rate: 10 }),
            queueOf('acme-synthetic-gamma-tracks.dlq', { messages: 3, consumers: 0 }),
            queueOf('acme-synthetic-alpha-artists', { messages: 0, ack_rate: 0 }),
            queueOf('unrelated-queue', { messages: 99 }),
        ]);

        const gamma = cell('synthetic', 'gamma', 'tracks');
        // 1,500 messages at 10/s is 150 s: the "< 10m" bin.
        expect(parts(gamma)).toMatchObject({ flag: '', count: '1.5k', meta: 'lag 3m', dlq: '▲ DLQ 3' });
        expect(gamma.className).toBe('mx-cell mx-lag-1');
        expect(gamma.dataset.state).toBe('warn');
        expect(gamma.querySelector('.mx-dlq').classList.contains('mx-dlq-warn')).toBe(true);
        expect(gamma.getAttribute('aria-label')).toBe('Tracks to Gamma; 1,500 messages queued; lag 3m; warning: DLQ 3');

        expect(parts(cell('synthetic', 'alpha', 'artists'))).toMatchObject({ count: '0', meta: 'no lag', dlq: '' });
        expect(cell('synthetic', 'alpha', 'artists').className).toBe('mx-cell mx-lag-0');
        expect(parts(cell('synthetic', 'beta', 'tracks'))).toMatchObject({ count: '—', meta: 'not reported' });
        expect(cell('synthetic', 'beta', 'tracks').dataset.state).toBe('unknown');
    });

    it('marks a stalled queue critical with glyph, label, and hatch, and a growing DLQ critical', () => {
        dashboard.renderTopology(topology(synthetic()));
        const start = Date.parse('2026-10-24T00:00:00Z');
        for (let i = 0; i <= 31; i++) {
            push(dashboard, 'synthetic', [
                queueOf('acme-synthetic-beta-artists', { messages: 4000 + i * 100, messages_unacknowledged: 200, ack_rate: 0, message_rate: 50 }),
                queueOf('acme-synthetic-beta-artists.dlq', { messages: 0, consumers: 0 }),
                queueOf('acme-synthetic-alpha-tracks', { messages: 20, ack_rate: 5 }),
                queueOf('acme-synthetic-alpha-tracks.dlq', { messages: 10 + i, consumers: 0 }),
            ], new Date(start + i * 2000).toISOString());
        }
        const stalled = cell('synthetic', 'beta', 'artists');
        expect(stalled.className).toBe('mx-cell mx-crit');
        expect(stalled.dataset).toMatchObject({ state: 'crit', kind: 'stalled' });
        expect(parts(stalled)).toMatchObject({ flag: '◆ STALLED · no acks 1m', count: '7.1k ↑', meta: 'lag ∞', dlq: '' });

        const dlq = cell('synthetic', 'alpha', 'tracks');
        expect(dlq.className).toBe('mx-cell mx-crit');
        expect(dlq.dataset).toMatchObject({ state: 'crit', kind: 'flowing' });
        expect(parts(dlq)).toMatchObject({ flag: '', dlq: '◆ DLQ +31 / 5m' });
        expect(dlq.querySelector('.mx-dlq').classList.contains('mx-dlq-crit')).toBe(true);
    });

    it('logs a queue entering a critical or warning state once, not on every update', () => {
        dashboard.renderTopology(topology(synthetic()));
        const log = vi.spyOn(dashboard, 'addLogEntry');
        for (let i = 0; i < 3; i++) {
            push(dashboard, 'synthetic', [queueOf('acme-synthetic-gamma-tracks', { messages: 50_000, consumers: 0 })], new Date(1e12 + i * 2000).toISOString());
        }
        expect(log).toHaveBeenCalledTimes(1);
        expect(log).toHaveBeenCalledWith('Tracks → Gamma: NO CONSUMER', 'error');
        push(dashboard, 'synthetic', [queueOf('acme-synthetic-gamma-tracks', { messages: 50_000, consumers: 1, ack_rate: 900 })], new Date(1e12 + 8000).toISOString());
        expect(log).toHaveBeenLastCalledWith('Tracks → Gamma: flowing', 'info');
    });

    it('holds a reading that arrives before the topology and applies it once the layout is drawn', async () => {
        const data = { pipelines: { discogs: { services: [], queues: [queueOf('groovemap-discogs-graphinator-masters')], queues_available: true } } };
        let release;
        dashboard._loadTopology = vi.fn(() => new Promise((resolve) => { release = resolve; }));
        dashboard.updateDashboard(data);
        expect(document.getElementById('pipeline-discogs')).toBeNull();

        dashboard.renderTopology(topology(discogs()));
        release(true);
        await dashboard.topologyRequest;
        await Promise.resolve();
        expect(parts(cell('discogs', 'graphinator', 'masters')).count).toBe('12');
        expect(document.getElementById('pipeline-discogs').classList.contains('hidden')).toBe(false);
    });

    it('drops the queue history when the live feed is lost, so a stall is not carried across the gap', () => {
        dashboard.renderTopology(topology(synthetic()));
        push(dashboard, 'synthetic', [queueOf('acme-synthetic-gamma-tracks')]);
        expect(dashboard.history.size).toBe(1);
        dashboard._resetHistory();
        expect(dashboard.history.size).toBe(0);
    });
});

describe('Overview matrix stacked fallback', () => {
    it('stacks a pipeline with more than two consumers on a narrow viewport, and only that one', () => {
        const dashboard = makeDashboard({ narrow: true });
        dashboard.renderTopology(topology(discogs(), synthetic()));
        expect(document.querySelector('[data-matrix="synthetic"]').classList.contains('mx-stacked')).toBe(true);
        expect(document.querySelector('[data-matrix="discogs"]').classList.contains('mx-stacked')).toBe(false);
        // Every stacked cell carries its consumer's name.
        expect(parts(cell('synthetic', 'gamma', 'tracks')).cname).toBe('Gamma');
    });

    it('returns to the grid when the viewport widens', () => {
        const dashboard = makeDashboard({ narrow: true });
        dashboard.renderTopology(topology(synthetic()));
        dashboard.narrowViewport.matches = false;
        dashboard._applyMatrixLayout();
        expect(document.querySelector('[data-matrix="synthetic"]').classList.contains('mx-stacked')).toBe(false);
    });
});

describe('Queue detail drawer', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
        dashboard.renderTopology(topology(synthetic()));
    });

    const numbers = () => {
        const dl = document.getElementById('queue-drawer-numbers');
        const terms = [...dl.querySelectorAll('dt')].map((dt) => dt.textContent);
        const values = [...dl.querySelectorAll('dd')].map((dd) => dd.textContent);
        return Object.fromEntries(terms.map((term, i) => [term, values[i]]));
    };

    it('opens from a cell with the queue numbers, follows live updates, and returns focus on close', () => {
        push(dashboard, 'synthetic', [
            queueOf('acme-synthetic-beta-tracks', { messages: 1200, messages_ready: 1000, messages_unacknowledged: 200, message_rate: 12.5, ack_rate: null }),
            queueOf('acme-synthetic-beta-tracks.dlq', { messages: 4, consumers: 0 }),
        ], '2026-10-24T00:00:00Z');
        const target = cell('synthetic', 'beta', 'tracks');
        target.focus();
        target.click();

        const drawer = document.getElementById('queue-drawer');
        expect(drawer.open).toBe(true);
        expect(text('queue-drawer-title')).toBe('Tracks → Beta');
        expect(text('queue-drawer-subtitle')).toBe('Synthetic pipeline · PostgreSQL');
        expect(text('queue-drawer-state')).toBe('▲ DLQ 4');
        expect(numbers()).toMatchObject({
            Queue: 'acme-synthetic-beta-tracks',
            Messages: '1,200',
            Ready: '1,000',
            Unacknowledged: '200',
            Consumers: '1',
            'Publish rate': '12.5/s',
            'Ack rate': 'unknown',
            'Mean ack rate (5 min)': 'unknown',
            Lag: 'unknown',
            'Dead-letter queue': 'acme-synthetic-beta-tracks.dlq',
            'DLQ messages': '4',
            'Backlog change': '—',
            History: '1 sample',
        });

        push(dashboard, 'synthetic', [
            queueOf('acme-synthetic-beta-tracks', { messages: 1300, ack_rate: 10 }),
            queueOf('acme-synthetic-beta-tracks.dlq', { messages: 4, consumers: 0 }),
        ], '2026-10-24T00:00:10Z');
        expect(numbers()).toMatchObject({ Messages: '1,300', 'Ack rate': '10.0/s', 'Backlog change': '+100 over 10s', History: '2 samples over 10s' });

        document.body.focus();
        dashboard.closeQueueDrawer();
        expect(drawer.open).toBe(false);
        expect(document.activeElement).toBe(target);
        expect(dashboard.drawerQueue).toBeNull();
    });
});
