import { describe, it, expect, beforeEach, vi } from 'vitest';
import { DATASTORES, discogs, makeDashboard, queue as queueOf, topologyOf } from './overview-helpers.js';

/**
 * The Overview is drawn from /api/topology and filled from live metrics. A third consumer and a
 * new entity type in the topology render, update, and toggle to dead-letter counts with no change
 * to the markup or the script.
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

describe('Overview rendered from the topology', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
        document.body.insertAdjacentHTML('beforeend', '<input id="dlq-toggle" type="checkbox">');
    });

    it('draws a hidden section per pipeline and reveals only those the metrics report', () => {
        dashboard.renderTopology(topology(discogs(), synthetic()));
        const hidden = (id) => document.getElementById(`pipeline-${id}`).classList.contains('hidden');
        expect([hidden('discogs'), hidden('synthetic')]).toEqual([true, true]);

        dashboard.updateDashboard({ pipelines: { synthetic: { services: [], queues: [], queues_available: true } } });
        expect([hidden('discogs'), hidden('synthetic')]).toEqual([true, false]);
    });

    it('lays out a third consumer and a new entity type with cards, bars, and dials', () => {
        dashboard.renderTopology(topology(synthetic()));
        for (const consumer of ['alpha', 'beta', 'gamma']) {
            expect(document.getElementById(`service-${consumer}`)).not.toBeNull();
            expect(document.getElementById(`synthetic-chart-legend-${consumer}`).textContent).toBe(consumer.charAt(0).toUpperCase() + consumer.slice(1));
            for (const entity of ['artists', 'release-groups', 'tracks']) {
                expect(document.getElementById(`synthetic-${consumer}-${entity}-count`)).not.toBeNull();
                expect(document.getElementById(`synthetic-bar-${entity}-${consumer}`)).not.toBeNull();
                for (const kind of ['publish', 'ack']) {
                    expect(document.getElementById(`synthetic-rate-circle-${consumer}-${entity}-${kind}`)).not.toBeNull();
                    expect(document.getElementById(`synthetic-rate-text-${consumer}-${entity}-${kind}`)).not.toBeNull();
                }
            }
        }
        expect(text('synthetic-extractor-synthetic-status-badge')).toBe('Unknown');
        expect(document.getElementById('service-gamma').querySelector('[data-datastore]').textContent).toBe('→ Valkey');
        expect(text('synthetic-alpha-release-groups-count')).toBe('—');
    });

    it('fills counts, bars, and rates from live queues by exact name, and shows dead letters on toggle', () => {
        dashboard.renderTopology(topology(synthetic()));
        const queues = [
            queueOf('acme-synthetic-gamma-tracks', { messages: 1500, message_rate: 7.5, ack_rate: null }),
            queueOf('acme-synthetic-gamma-tracks.dlq', { messages: 3 }),
            queueOf('acme-synthetic-alpha-artists', { messages: 4 }),
            queueOf('unrelated-queue', { messages: 99 }),
        ];
        dashboard.updateDashboard({ pipelines: { synthetic: { services: [], queues, queues_available: true } } });

        expect(text('synthetic-gamma-tracks-count')).toBe('1,500');
        expect(text('synthetic-alpha-artists-count')).toBe('4');
        expect(text('synthetic-beta-tracks-count')).toBe('—');
        expect(document.getElementById('synthetic-bar-tracks-gamma').style.height).toBe('100%');
        expect(text('synthetic-rate-text-gamma-tracks-publish')).toBe('7.5');
        expect(text('synthetic-rate-text-gamma-tracks-ack')).toBe('—');

        document.getElementById('dlq-toggle').checked = true;
        dashboard._onDlqToggle();
        expect(text('synthetic-gamma-tracks-count')).toBe('3');
        expect(text('synthetic-alpha-artists-count')).toBe('—');
        expect(text('synthetic-chart-legend-gamma')).toBe('Gamma DLQ');
        expect(text('synthetic-rate-text-gamma-tracks-publish')).toBe('N/A');
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
        expect(text('discogs-graphinator-masters-count')).toBe('12');
        expect(document.getElementById('pipeline-discogs').classList.contains('hidden')).toBe(false);
    });
});
