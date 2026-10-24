import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { vi } from 'vitest';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DASHBOARD_JS_PATH = resolve(__dirname, '..', 'static', 'dashboard.js');

export function loadDashboardClass() {
    const code = readFileSync(DASHBOARD_JS_PATH, 'utf-8');
    const withoutAutoInit = code.replace(/document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/, '');
    const wrapped = `(function() {\n${withoutAutoInit}\nglobalThis.__Dashboard = Dashboard;\n})();`;
    vm.runInThisContext(wrapped, { filename: DASHBOARD_JS_PATH });
    return globalThis.__Dashboard;
}

/** The shape /api/topology returns, built the way the server derives it. */
export function topologyOf({ id, label, prefix, producer, consumers, entities }) {
    return {
        id,
        label,
        queue_prefix: prefix,
        entity_types: entities,
        producer: { name: producer, label: 'Extractor', exchanges: entities.map((entity) => ({ entity, name: `${prefix}-${entity}` })) },
        consumers: consumers.map(({ name, datastore }) => ({
            name,
            label: name.charAt(0).toUpperCase() + name.slice(1),
            datastore,
            queues: entities.map((entity) => {
                const queueName = `${prefix}-${name}-${entity}`;
                return { entity, name: queueName, dead_letter_exchange: `${queueName}.dlx`, dead_letter_queue: `${queueName}.dlq` };
            }),
        })),
        dead_letter_queues: [],
    };
}

export const DATASTORES = [
    { id: 'neo4j', label: 'Neo4j' },
    { id: 'postgresql', label: 'PostgreSQL' },
];

export const discogs = () =>
    topologyOf({
        id: 'discogs',
        label: 'Discogs',
        prefix: 'groovemap-discogs',
        producer: 'extractor-discogs',
        consumers: [
            { name: 'graphinator', datastore: 'neo4j' },
            { name: 'tableinator', datastore: 'postgresql' },
        ],
        entities: ['masters', 'releases', 'artists', 'labels'],
    });

/** A dashboard with the live-view side effects stubbed and an empty `#pipelines` container. */
export function makeDashboard() {
    document.body.innerHTML = '<div id="pipelines"></div>';
    const Dashboard = loadDashboardClass();
    const dashboard = Object.create(Dashboard.prototype);
    Object.assign(dashboard, {
        CIRCUMFERENCE: 176,
        gaugeStats: {},
        currentMaps: {},
        queuesUnavailable: {},
        activityLog: [],
        maxLogEntries: 50,
        topology: null,
        topologyRequest: null,
        pendingData: null,
    });
    for (const method of ['updateServices', 'updateDatabases', 'updateLastUpdated', 'updateOverallStatus', 'renderActivityLog']) {
        dashboard[method] = vi.fn();
    }
    return dashboard;
}

export const queue = (name, extra = {}) => ({ name, messages: 12, messages_ready: 12, messages_unacknowledged: 0, consumers: 1, ...extra });
