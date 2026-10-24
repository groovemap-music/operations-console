import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { vi } from 'vitest';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DASHBOARD_JS_PATH = resolve(__dirname, '..', 'static', 'dashboard.js');

function loadDashboardScript() {
    const code = readFileSync(DASHBOARD_JS_PATH, 'utf-8');
    const withoutAutoInit = code.replace(/document\.addEventListener\(\s*['"]DOMContentLoaded['"][\s\S]*$/, '');
    const wrapped = `(function() {\n${withoutAutoInit}\nglobalThis.__Dashboard = Dashboard;\nglobalThis.__QueueHealth = QueueHealth;\nglobalThis.__matrixLayout = matrixLayout;\n})();`;
    vm.runInThisContext(wrapped, { filename: DASHBOARD_JS_PATH });
}

export function loadDashboardClass() {
    loadDashboardScript();
    return globalThis.__Dashboard;
}

/** The matrix's pure health rules and its layout choice, as dashboard.js defines them. */
export function loadQueueHealth() {
    loadDashboardScript();
    return { QueueHealth: globalThis.__QueueHealth, matrixLayout: globalThis.__matrixLayout };
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

/** The shell's queue detail drawer, as index.html declares it. */
export const DRAWER_MARKUP = `
<dialog id="queue-drawer">
  <h2 id="queue-drawer-title"></h2><p id="queue-drawer-subtitle"></p>
  <button id="queue-drawer-close" type="button">Close</button>
  <p id="queue-drawer-state"></p><dl id="queue-drawer-numbers"></dl>
</dialog>`;

/** A dashboard with the live-view side effects stubbed, an empty `#pipelines` container, and the drawer. */
export function makeDashboard({ narrow = false } = {}) {
    document.body.innerHTML = `<div id="pipelines"></div>${DRAWER_MARKUP}`;
    const Dashboard = loadDashboardClass();
    const dashboard = Object.create(Dashboard.prototype);
    Object.assign(dashboard, {
        narrowViewport: { matches: narrow },
        history: new Map(),
        healthKinds: {},
        drawerQueue: null,
        drawerReturnFocus: null,
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
