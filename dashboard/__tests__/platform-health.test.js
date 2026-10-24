import { describe, it, expect, beforeEach } from 'vitest';
import { DATASTORES, discogs, loadAttention, makeDashboard } from './overview-helpers.js';

/**
 * Platform health: every long-running service outside the pipelines is listed by group with its
 * state, a down one raises an attention item (critical, or a warning for observability), a
 * datastore is raised once by its own rule, and a pipeline none of whose services answered is
 * shown as down rather than missing.
 */
const service = (name, group, status = 'healthy', extra = {}) => ({ name, group, status, last_seen: null, error: null, source: 'probe', ...extra });

describe('Attention.derive for platform services', () => {
    const Attention = loadAttention();
    const derive = (platform) => Attention.derive({ pipelines: [], databases: [], alerts: [], platform });

    it('raises nothing while every service is healthy', () => {
        expect(derive([service('catalog-api', 'applications'), service('grafana', 'observability')])).toEqual([]);
    });

    it('raises a critical item for an application, the broker, or a store, and a warning for observability', () => {
        const items = derive([
            service('grafana', 'observability', 'unreachable'),
            service('catalog-api', 'applications', 'unreachable'),
            service('rabbitmq', 'data', 'unhealthy'),
        ]);
        expect(items.map((i) => [i.severity, i.kind, i.text])).toEqual([
            ['crit', 'platform-unreachable', 'catalog-api is unreachable'],
            ['crit', 'platform-unhealthy', 'rabbitmq is unhealthy'],
            ['warn', 'platform-unreachable', 'grafana is unreachable'],
        ]);
        expect(items[0].target).toEqual({ type: 'platform', service: 'catalog-api' });
    });

    it('leaves a datastore row to the datastore rule, so it is raised once', () => {
        const items = Attention.derive({
            pipelines: [], alerts: [],
            databases: [{ name: 'Neo4j', status: 'unhealthy' }],
            platform: [service('neo4j', 'data', 'unhealthy', { source: 'datastore' })],
        });
        expect(items.map((i) => i.kind)).toEqual(['datastore-unavailable']);
    });

    it('says a down pipeline is down once, not once per service', () => {
        const items = Attention.derive({
            databases: [], alerts: [],
            pipelines: [{ id: 'discogs', label: 'Discogs', down: true, services: [{ name: 'a', status: 'unknown' }, { name: 'b', status: 'unknown' }], cells: [] }],
        });
        expect(items).toHaveLength(1);
        expect(items[0]).toMatchObject({ severity: 'crit', kind: 'pipeline-down', text: 'Discogs: pipeline is down, no service answered' });
    });
});

describe('Dashboard platform section', () => {
    let dashboard;
    beforeEach(() => {
        dashboard = makeDashboard();
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
    });
    const rows = (group) => [...document.querySelectorAll(`[data-platform-group="${group}"] .ph-row`)];
    const reading = (platform, jobs = [], pipeline = { services: [], queues: [], queues_available: true }) =>
        dashboard.updateDashboard({ pipelines: { discogs: pipeline }, databases: [], platform, jobs });

    it('lists each service under its group with its state, and updates a row when the state changes', () => {
        reading([service('catalog-api', 'applications'), service('postgres', 'data'), service('grafana', 'observability')]);
        expect(rows('applications').map((r) => [r.dataset.service, r.dataset.state])).toEqual([['catalog-api', 'healthy']]);
        expect(rows('data').map((r) => r.dataset.service)).toEqual(['postgres']);
        expect(rows('observability').map((r) => r.dataset.service)).toEqual(['grafana']);

        reading([service('catalog-api', 'applications', 'unreachable', { error: 'no answer within 3s' })]);
        const [row] = rows('applications');
        expect(row.dataset.state).toBe('unreachable');
        expect(row.querySelector('.ph-badge').textContent).toBe('Unreachable');
        expect(row.querySelector('.ph-detail').textContent).toBe('no answer within 3s');
        expect(rows('data')).toEqual([]);
    });

    it('lists jobs, with the last run where one is recorded and "Not monitored" otherwise', () => {
        reading([], [
            { name: 'schema-init', monitored: false, detail: 'not monitored here' },
            { name: 'embeddings', monitored: true, outcome: 'loaded', detail: 'last load wrote dump discogs-2026-09 (2026-09-01)', last_run: '2026-09-02T03:00:00Z' },
        ]);
        const jobs = [...document.querySelectorAll('[data-platform-jobs] .ph-row')];
        expect(jobs.map((j) => [j.dataset.service, j.querySelector('.ph-badge').textContent])).toEqual([['schema-init', 'Not monitored'], ['embeddings', 'Last run']]);
        expect(jobs[0].querySelector('.ph-detail').textContent).toBe('not monitored here');
        expect(jobs[1].querySelector('.ph-detail').textContent).toContain('discogs-2026-09');
    });

    it('keeps a pipeline whose services all went unanswered visible and marks it down, then clears on recovery', () => {
        const down = { services: [{ name: 'graphinator', status: 'unknown' }], queues: [], queues_available: true, down: true };
        reading([], [], down);
        const banner = document.querySelector('[data-pipeline-down="discogs"]');
        expect(document.getElementById('pipeline-discogs').classList.contains('hidden')).toBe(false);
        expect(banner.classList.contains('hidden')).toBe(false);
        expect(banner.textContent).toBe('Pipeline down: no service answered');
        expect(dashboard.attentionItems.map((i) => i.kind)).toEqual(['pipeline-down']);

        reading([], [], { ...down, down: false, services: [{ name: 'graphinator', status: 'healthy' }] });
        expect(banner.classList.contains('hidden')).toBe(true);
        expect(dashboard.attentionItems).toEqual([]);
    });

    it('raises an attention item for a platform service going down and clears it when it recovers', () => {
        reading([service('valkey', 'data', 'unreachable')]);
        expect(dashboard.attentionItems.map((i) => i.id)).toEqual(['platform:valkey']);
        reading([service('valkey', 'data')]);
        expect(dashboard.attentionItems).toEqual([]);
    });
});
