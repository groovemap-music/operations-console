import { describe, it, expect, beforeEach } from 'vitest';
import { DATASTORES, discogs, loadTopologyFlow, makeDashboard, queue as queueOf, topologyOf } from './overview-helpers.js';

/**
 * The topology drill-down: one pipeline's flow, opened from its matrix head on desktop widths.
 * Its layout and edge widths are pure; its node states are the matrix cells' own.
 */
const TopologyFlow = loadTopologyFlow();

const twoX = () =>
    topologyOf({
        id: 'discogs',
        label: 'Discogs',
        prefix: 'groovemap-discogs',
        producer: 'extractor-discogs',
        consumers: [
            { name: 'graphinator', datastore: 'neo4j' },
            { name: 'tableinator', datastore: 'postgresql' },
            { name: 'searchinator', datastore: 'postgresql' },
            { name: 'embedinator', datastore: 'neo4j' },
        ],
        entities: ['masters', 'releases', 'artists', 'labels'],
    });

describe('TopologyFlow edge width', () => {
    it('grows with the square root of the rate and caps at RATE_MAX', () => {
        const { edgeWidth, RATE_MAX } = TopologyFlow;
        expect(edgeWidth(RATE_MAX)).toBeCloseTo(11.5);
        expect(edgeWidth(RATE_MAX / 4)).toBeCloseTo(1.5 + 5);
        expect(edgeWidth(RATE_MAX * 10)).toBeCloseTo(11.5);
        // Square root, not linear: a quarter of the rate keeps half the extra width.
        expect(edgeWidth(RATE_MAX / 4) - 1.5).toBeCloseTo((edgeWidth(RATE_MAX) - 1.5) / 2);
        expect(edgeWidth(1)).toBeGreaterThan(1.5);
    });

    it('draws zero and unknown rates as hairlines told apart by style, never an unknown as 0/s', () => {
        const { edgeWidth, edgeStyle } = TopologyFlow;
        expect([edgeWidth(0), edgeWidth(null), edgeWidth(undefined)]).toEqual([1, 1, 1]);
        expect([edgeStyle(0), edgeStyle(null), edgeStyle(undefined), edgeStyle(12)]).toEqual(['idle', 'unknown', 'unknown', 'flowing']);
    });

    it('reads a fanout exchange from any bound queue, and sums what a consumer writes', () => {
        const { exchangeRate, sumRates } = TopologyFlow;
        expect(exchangeRate([400, 405, null])).toBe(405);
        expect(exchangeRate([null, null])).toBeNull();
        expect(exchangeRate([0, 0])).toBe(0);
        expect(sumRates([100, null, 50])).toBe(150);
        expect(sumRates([null])).toBeNull();
    });
});

describe('TopologyFlow layout', () => {
    const byKey = (layout) => Object.fromEntries(layout.nodes.map((n) => [n.key, n]));
    const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

    it('places producer → exchanges → queues → consumers → datastores left to right', () => {
        const layout = TopologyFlow.layout(discogs());
        const nodes = byKey(layout);
        expect(layout.columns.map((c) => c.kind)).toEqual(['producer', 'exchange', 'queue', 'consumer', 'datastore']);
        const kinds = ['producer', 'exchange', 'queue', 'consumer', 'datastore'];
        const right = (kind) => Math.max(...layout.nodes.filter((n) => n.kind === kind).map((n) => n.x + n.w));
        const left = (kind) => Math.min(...layout.nodes.filter((n) => n.kind === kind).map((n) => n.x));
        for (let i = 1; i < kinds.length; i++) expect(left(kinds[i])).toBeGreaterThan(right(kinds[i - 1]));
        expect(Math.max(...layout.nodes.map((n) => n.x + n.w))).toBeLessThanOrEqual(layout.width);
        expect(layout.nodes.filter((n) => n.kind === 'queue')).toHaveLength(8);
        expect(layout.nodes.filter((n) => n.kind === 'exchange').map((n) => n.entity)).toEqual(['masters', 'releases', 'artists', 'labels']);
        expect(layout.nodes.filter((n) => n.kind === 'datastore').map((n) => n.datastore)).toEqual(['neo4j', 'postgresql']);
        expect(nodes['queue:tableinator:releases']).toMatchObject({ consumer: 'tableinator', entity: 'releases' });
    });

    it('fits a 1024 px viewport without scaling the text', () => {
        // 1024 px less the dialog's 16 px margins, 16 px padding, and 1 px border on each side.
        expect(TopologyFlow.layout(discogs()).width).toBeLessThanOrEqual(1024 - 2 * (16 + 16 + 1));
        expect(TopologyFlow.MIN_WIDTH).toBe(1024);
    });

    it('groups each entity’s queues beside its exchange, centred on it, with no node overlapping another', () => {
        for (const pipeline of [discogs(), twoX()]) {
            const layout = TopologyFlow.layout(pipeline);
            const nodes = byKey(layout);
            for (const entity of pipeline.entity_types) {
                const queues = layout.nodes.filter((n) => n.kind === 'queue' && n.entity === entity);
                const top = Math.min(...queues.map((q) => q.y));
                const bottom = Math.max(...queues.map((q) => q.y + q.h));
                const exchange = nodes[`exchange:${entity}`];
                expect(exchange.y + exchange.h / 2).toBeCloseTo((top + bottom) / 2);
            }
            for (const [i, a] of layout.nodes.entries()) {
                for (const b of layout.nodes.slice(i + 1)) expect(overlaps(a, b), `${a.key} / ${b.key}`).toBe(false);
            }
            expect(Math.max(...layout.nodes.map((n) => n.y + n.h))).toBeLessThanOrEqual(layout.height);
        }
    });

    it('connects every hop once: producer to each exchange, exchange to its queues, queue to its consumer, consumer to its store', () => {
        const pipeline = twoX();
        const { edges } = TopologyFlow.layout(pipeline);
        const keys = edges.map((e) => e.key);
        expect(new Set(keys).size).toBe(keys.length);
        // 4 exchanges + 16 queues in + 16 queues out + 4 consumers to their datastores.
        expect(edges).toHaveLength(4 + 16 + 16 + 4);
        expect(keys).toContain('producer>exchange:releases');
        expect(keys).toContain('exchange:releases>queue:searchinator:releases');
        expect(keys).toContain('queue:searchinator:releases>consumer:searchinator');
        expect(keys).toContain('consumer:searchinator>datastore:postgresql');
        expect(keys).toContain('consumer:embedinator>datastore:neo4j');
        for (const edge of edges) expect(edge.d).toMatch(/^M[\d.]+,[\d.]+ C/);
    });
});

describe('Topology drill-down', () => {
    let dashboard;
    const topology = () => ({ pipelines: [discogs()], datastores: DATASTORES });
    const node = (key) => document.querySelector(`#topology-flow [data-node="${key}"]`);
    const edge = (key) => document.querySelector(`#topology-flow [data-edge="${key}"]`);
    const dialog = () => document.getElementById('topology-dialog');

    // Readings 2 s apart for 70 s: tableinator · releases stops acknowledging while connected, and
    // graphinator · artists holds a flat DLQ. Everything else flows; masters has finished.
    const scenario = (readings = 36) => {
        const start = Date.parse('2026-10-24T10:00:00Z');
        for (let i = 0; i < readings; i++) {
            const queues = [];
            for (const consumer of discogs().consumers) {
                for (const q of consumer.queues) {
                    const stalled = consumer.name === 'tableinator' && q.entity === 'releases';
                    const done = q.entity === 'masters';
                    const ack = stalled || done ? 0 : 400;
                    queues.push(queueOf(q.name, {
                        messages: done ? 0 : 5000 + (stalled ? i * 1200 : 0),
                        message_rate: done ? 0 : 450, ack_rate: i === 0 ? null : ack,
                    }));
                    const dlq = consumer.name === 'graphinator' && q.entity === 'artists' ? 12 : 0;
                    queues.push(queueOf(q.dead_letter_queue, { messages: dlq, consumers: 0, message_rate: 0, ack_rate: 0 }));
                }
            }
            dashboard.updateDashboard({
                timestamp: new Date(start + i * 2000).toISOString(),
                pipelines: { discogs: { services: [], queues, queues_available: true } },
            });
        }
    };

    beforeEach(() => {
        dashboard = makeDashboard();
        dashboard.renderTopology(topology());
    });

    it('puts a Topology control in each pipeline’s matrix head', () => {
        const button = document.querySelector('#pipeline-discogs .mx-pipeline-head [data-topology="discogs"]');
        expect(button.tagName).toBe('BUTTON');
        expect(button.textContent).toBe('Topology');
        expect(button.getAttribute('aria-label')).toBe('Show the Discogs pipeline topology');
    });

    it('opens the pipeline’s flow from the control and closes it again, returning focus', () => {
        const button = document.querySelector('[data-topology="discogs"]');
        button.click();
        expect(dialog().open).toBe(true);
        expect(document.getElementById('topology-title').textContent).toBe('Discogs pipeline topology');
        expect(document.querySelectorAll('#topology-flow [data-kind="queue"], #topology-flow .tp-queue')).toHaveLength(8);
        expect(node('producer').textContent).toContain('extractor-discogs');
        expect(node('datastore:postgresql').textContent).toBe('PostgreSQL');

        dashboard.closeTopology();
        expect(dialog().open).toBe(false);
        expect(dashboard.topologyPipeline).toBeNull();
        expect(document.getElementById('topology-flow').children).toHaveLength(0);
        expect(document.activeElement).toBe(button);
    });

    it('does not open below the desktop breakpoint, and closes when the viewport narrows', () => {
        const phone = makeDashboard({ wide: false });
        phone.renderTopology(topology());
        phone.openTopology('discogs');
        expect(dialog().open).toBe(false);

        dashboard = makeDashboard();
        dashboard.renderTopology(topology());
        dashboard.openTopology('discogs');
        expect(dialog().open).toBe(true);
        dashboard.renderTopology(topology());
        expect(dialog().open).toBe(false);
    });

    it('marks each queue with its matrix cell’s own state, label, and glyph', () => {
        scenario();
        dashboard.openTopology('discogs');
        for (const consumer of ['graphinator', 'tableinator']) {
            for (const entity of discogs().entity_types) {
                const cell = document.getElementById(`discogs-${consumer}-${entity}-cell`);
                const g = node(`queue:${consumer}:${entity}`);
                expect(g.dataset.state, `${consumer} ${entity}`).toBe(cell.dataset.state);
                expect(g.getAttribute('aria-label')).toBe(cell.getAttribute('aria-label'));
            }
        }
        const stalled = node('queue:tableinator:releases');
        expect(stalled.dataset.state).toBe('crit');
        expect(stalled.classList.contains('tp-crit')).toBe(true);
        expect(stalled.querySelector('.tp-sub').textContent).toBe('◆ STALLED · no acks 1m · 47k queued');
        const flat = node('queue:graphinator:artists');
        expect(flat.dataset.state).toBe('warn');
        expect(flat.querySelector('.tp-dlq').dataset.state).toBe('warn');
        expect(flat.querySelector('.tp-pill-text').textContent).toBe('▲ DLQ 12');
        expect(node('queue:graphinator:labels').dataset.state).toBe('ok');
        expect(node('queue:graphinator:labels').querySelector('.tp-sub').textContent).toBe('5.0k queued · lag 13s');

        // A consumer takes the worst of its queues, and says how many are in trouble.
        expect(node('consumer:tableinator').dataset.state).toBe('crit');
        expect(node('consumer:tableinator').querySelector('.tp-sub').textContent).toBe('◆ 1 critical');
        expect(node('consumer:graphinator').dataset.state).toBe('warn');
        expect(node('consumer:graphinator').querySelector('.tp-sub').textContent).toBe('▲ 1 warning');
    });

    it('sizes edges from the live rates: √ width, dashed at 0/s, dotted while unknown', () => {
        scenario(1);
        dashboard.openTopology('discogs');
        // The first reading has publish rates but no computable ack rate yet.
        expect(edge('queue:graphinator:releases>consumer:graphinator').getAttribute('class')).toBe('tp-edge tp-edge-unknown');
        expect(edge('consumer:graphinator>datastore:neo4j').getAttribute('class')).toBe('tp-edge tp-edge-unknown');

        scenario();
        const flowing = edge('queue:graphinator:releases>consumer:graphinator');
        expect(flowing.getAttribute('class')).toBe('tp-edge tp-edge-flowing');
        expect(Number(flowing.getAttribute('stroke-width'))).toBeCloseTo(TopologyFlow.edgeWidth(400), 1);
        expect(flowing.querySelector('title').textContent).toBe('Releases → Graphinator acknowledged: 400.0/s');
        expect(edge('queue:tableinator:releases>consumer:tableinator').getAttribute('class')).toBe('tp-edge tp-edge-idle');
        expect(edge('producer>exchange:masters').getAttribute('class')).toBe('tp-edge tp-edge-idle');
        expect(edge('producer>exchange:releases').dataset.rate).toBe('450');
        // Graphinator acknowledges 400/s on releases, artists, and labels.
        expect(edge('consumer:graphinator>datastore:neo4j').dataset.rate).toBe('1200');
        expect(node('exchange:releases').querySelector('.tp-sub').textContent).toBe('450.0/s in');
        expect(node('producer').querySelectorAll('.tp-sub')[1].textContent).toBe('1.4k/s out');
    });

    it('marks every queue unknown when the pipeline’s queue metrics are unavailable', () => {
        scenario();
        dashboard.openTopology('discogs');
        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [], queues_available: false } } });
        const g = node('queue:tableinator:releases');
        expect(g.dataset.state).toBe('unknown');
        expect(g.querySelector('.tp-sub').textContent).toBe('queue metrics unavailable');
        expect(edge('exchange:releases>queue:tableinator:releases').getAttribute('class')).toBe('tp-edge tp-edge-unknown');
    });

    it('opens the existing queue drawer from a queue node, by click or keyboard', () => {
        scenario();
        dashboard.openTopology('discogs');
        const g = node('queue:tableinator:releases');
        g.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(document.getElementById('queue-drawer').open).toBe(true);
        expect(document.getElementById('queue-drawer-title').textContent).toBe('Releases → Tableinator');
        expect(dashboard.drawerReturnFocus).toBe(g);
        dashboard.closeQueueDrawer();

        node('queue:graphinator:artists').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        expect(document.getElementById('queue-drawer-title').textContent).toBe('Artists → Graphinator');
        expect(dialog().open).toBe(true);
    });
});
