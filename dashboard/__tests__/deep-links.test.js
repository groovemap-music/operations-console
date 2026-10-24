import { describe, it, expect, beforeEach } from 'vitest';
import { DATASTORES, discogs, loadDeepLinks, makeDashboard, queue as queueOf } from './overview-helpers.js';

/**
 * Every service, queue, and datastore links into the provisioned Grafana dashboards with the
 * dashboard's own variables and the console's shared time range. With a base URL unset there is
 * no link, and every value that reaches a URL is escaped.
 */
const GRAFANA = 'https://grafana.test';
const TRACES = 'https://traces.test';
const QUEUE = 'groovemap-discogs-graphinator-artists';

describe('DeepLinks URL building', () => {
    const DeepLinks = loadDeepLinks();

    it('maps the console range to a relative Grafana time range', () => {
        expect(DeepLinks.timeRange('1h')).toEqual({ from: 'now-1h', to: 'now' });
        expect(DeepLinks.timeRange('90d')).toEqual({ from: 'now-90d', to: 'now' });
    });

    it('falls back to 24h for a range it does not recognise, never splicing it into the URL', () => {
        for (const bad of [null, undefined, '', 'forever', '24h&x=1', '../7d']) {
            expect(DeepLinks.timeRange(bad).from).toBe('now-24h');
        }
    });

    it('builds {base}/d/{uid} with var-, from and to', () => {
        expect(DeepLinks.dashboardUrl(GRAFANA, 'infrastructure', { queue: QUEUE }, '6h'))
            .toBe(`${GRAFANA}/d/groovemap-infrastructure?var-queue=${QUEUE}&from=now-6h&to=now`);
        expect(DeepLinks.dashboardUrl(GRAFANA, 'ingestion', { service: 'extractor-discogs', source: 'discogs' }, '24h'))
            .toBe(`${GRAFANA}/d/groovemap-ingestion?var-service=extractor-discogs&var-source=discogs&from=now-24h&to=now`);
        expect(DeepLinks.dashboardUrl(GRAFANA, 'neo4j', {}, '7d')).toBe(`${GRAFANA}/d/groovemap-neo4j?from=now-7d&to=now`);
    });

    it('URL-encodes values so a queue name cannot add or break parameters', () => {
        const url = DeepLinks.dashboardUrl(GRAFANA, 'infrastructure', { queue: 'a&b=c d#e/é?' }, '1h');
        expect(url).toBe(`${GRAFANA}/d/groovemap-infrastructure?var-queue=a%26b%3Dc%20d%23e%2F%C3%A9%3F&from=now-1h&to=now`);
        expect(new URL(url).searchParams.get('var-queue')).toBe('a&b=c d#e/é?');
        expect([...new URL(url).searchParams.keys()]).toEqual(['var-queue', 'from', 'to']);
    });

    it('is absent, not broken, when the base is unset or the dashboard unknown', () => {
        for (const base of [null, undefined, '']) expect(DeepLinks.dashboardUrl(base, 'consumers', { service: 'x' }, '1h')).toBeNull();
        expect(DeepLinks.dashboardUrl(GRAFANA, 'nonexistent', {}, '1h')).toBeNull();
    });

    it('sends trace links to the traces base and everything else to Grafana', () => {
        const bases = { grafana: GRAFANA, traces: TRACES };
        const [, traces] = DeepLinks.specs.consumer('graphinator');
        expect(DeepLinks.resolve(bases, traces, '1h')).toBe(`${TRACES}/d/groovemap-traces?var-service=graphinator&from=now-1h&to=now`);
        const [grafana] = DeepLinks.specs.consumer('graphinator');
        expect(DeepLinks.resolve(bases, grafana, '1h')).toBe(`${GRAFANA}/d/groovemap-consumers?var-service=graphinator&from=now-1h&to=now`);
        expect(DeepLinks.resolve({ grafana: null, traces: TRACES }, grafana, '1h')).toBeNull();
        expect(DeepLinks.resolve({ grafana: GRAFANA, traces: null }, traces, '1h')).toBeNull();
    });

    it('links each kind of thing to its own dashboard', () => {
        const dashboards = (specs) => specs.map(spec => [spec.dashboard, spec.vars]);
        expect(dashboards(DeepLinks.specs.queue('discogs', QUEUE, `${QUEUE}.dlq`))).toEqual([
            ['infrastructure', { queue: QUEUE }], ['infrastructure', { queue: `${QUEUE}.dlq` }], ['pipeline', { source: 'discogs' }],
        ]);
        expect(dashboards(DeepLinks.specs.producer('discogs', 'extractor-discogs'))).toEqual([
            ['ingestion', { service: 'extractor-discogs', source: 'discogs' }], ['traces', { service: 'extractor-discogs' }],
        ]);
        expect(dashboards(DeepLinks.specs.consumer('tableinator'))[0]).toEqual(['consumers', { service: 'tableinator' }]);
        expect(dashboards(DeepLinks.specs.datastore('neo4j'))).toEqual([['neo4j', {}]]);
        expect(dashboards(DeepLinks.specs.datastore('postgresql'))).toEqual([['infrastructure', {}]]);
        expect(DeepLinks.specs.datastore('valkey')).toEqual([]);
    });
});

describe('deep links on the page', () => {
    let dashboard;
    const hrefs = (selector) => [...document.querySelectorAll(`${selector} a`)].map(a => a.getAttribute('href'));

    beforeEach(() => {
        localStorage.clear();
        document.body.innerHTML = '';
        dashboard = makeDashboard();
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
    });

    const openDrawer = () => {
        dashboard.updateDashboard({
            timestamp: 1,
            pipelines: { discogs: { services: [], queues: [queueOf(QUEUE), queueOf(`${QUEUE}.dlq`, { messages: 0 })], queues_available: true } },
        });
        dashboard.openQueueDrawer(document.getElementById('discogs-graphinator-artists-cell'));
    };

    it('shows no link anywhere while the base URLs are unset', () => {
        openDrawer();
        expect(document.querySelectorAll('a.mx-link')).toHaveLength(0);
        expect(document.getElementById('queue-drawer-links').hidden).toBe(true);
        expect(document.querySelector('#service-graphinator .mx-links').hidden).toBe(true);
    });

    it('links the drawer to the queue, its DLQ, the pipeline, and the consumer, with the current range', () => {
        localStorage.setItem('console_range', '7d');
        dashboard.linkBases = { grafana: GRAFANA, traces: TRACES };
        openDrawer();
        const range = 'from=now-7d&to=now';
        expect(hrefs('#queue-drawer-links')).toEqual([
            `${GRAFANA}/d/groovemap-infrastructure?var-queue=${QUEUE}&${range}`,
            `${GRAFANA}/d/groovemap-infrastructure?var-queue=${QUEUE}.dlq&${range}`,
            `${GRAFANA}/d/groovemap-pipeline-overview?var-source=discogs&${range}`,
            `${GRAFANA}/d/groovemap-consumers?var-service=graphinator&${range}`,
            `${TRACES}/d/groovemap-traces?var-service=graphinator&${range}`,
        ]);
    });

    it('opens every link in a new tab without opener or referrer', () => {
        dashboard.linkBases = { grafana: GRAFANA, traces: TRACES };
        openDrawer();
        const links = [...document.querySelectorAll('a.mx-link')];
        expect(links.length).toBeGreaterThan(0);
        for (const link of links) {
            expect(link.getAttribute('target')).toBe('_blank');
            expect(link.getAttribute('rel')).toBe('noopener noreferrer');
        }
    });

    it('links service headings: the extractor to ingestion, consumers to consumers', () => {
        dashboard.linkBases = { grafana: GRAFANA, traces: GRAFANA };
        dashboard._refreshLinks();
        expect(hrefs('#service-extractor-discogs')[0]).toBe(`${GRAFANA}/d/groovemap-ingestion?var-service=extractor-discogs&var-source=discogs&from=now-24h&to=now`);
        expect(hrefs('#service-tableinator')[0]).toBe(`${GRAFANA}/d/groovemap-consumers?var-service=tableinator&from=now-24h&to=now`);
        expect(hrefs('#service-tableinator')[1]).toContain('/d/groovemap-traces?var-service=tableinator');
    });

    it('follows the range when it changes', () => {
        dashboard.linkBases = { grafana: GRAFANA, traces: GRAFANA };
        dashboard._refreshLinks();
        localStorage.setItem('console_range', '1h');
        dashboard._refreshLinks();
        expect(hrefs('#service-graphinator')[0]).toContain('from=now-1h&to=now');
    });

    it('removes the links again when the bases go away', () => {
        dashboard.linkBases = { grafana: GRAFANA, traces: GRAFANA };
        dashboard._refreshLinks();
        expect(document.querySelectorAll('a.mx-link').length).toBeGreaterThan(0);
        dashboard.linkBases = { grafana: null, traces: null };
        dashboard._refreshLinks();
        expect(document.querySelectorAll('a.mx-link')).toHaveLength(0);
    });

    it('links an attention item for a queue to that queue, and one for a service to its dashboard', () => {
        dashboard.linkBases = { grafana: GRAFANA, traces: GRAFANA };
        dashboard.updateDashboard({
            timestamp: 1,
            pipelines: { discogs: { services: [{ name: 'tableinator', status: 'unhealthy' }], queues: [queueOf(QUEUE, { consumers: 0 })], queues_available: true } },
        });
        const byKind = (kind) => document.querySelector(`[data-attention-kind="${kind}"]`).closest('li');
        expect([...byKind('no-consumer').querySelectorAll('a')].map(a => a.getAttribute('href'))[0])
            .toBe(`${GRAFANA}/d/groovemap-infrastructure?var-queue=${QUEUE}&from=now-24h&to=now`);
        expect(byKind('service-unhealthy').querySelector('a').getAttribute('href')).toContain('/d/groovemap-consumers?var-service=tableinator');
    });
});
