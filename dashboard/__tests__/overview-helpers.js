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
    const wrapped = `(function() {\n${withoutAutoInit}\nglobalThis.__Dashboard = Dashboard;\nglobalThis.__QueueHealth = QueueHealth;\nglobalThis.__Attention = Attention;\nglobalThis.__DeepLinks = DeepLinks;\nglobalThis.__RunProgress = RunProgress;\nglobalThis.__matrixLayout = matrixLayout;\nglobalThis.__TopologyFlow = TopologyFlow;\n})();`;
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

/** The attention bar's pure derivation, as dashboard.js defines it. */
export function loadAttention() {
    loadDashboardScript();
    return globalThis.__Attention;
}

/** The deep-link builder, as dashboard.js defines it. */
export function loadDeepLinks() {
    loadDashboardScript();
    return globalThis.__DeepLinks;
}

/** The run-progress derivation (headline, ETA, burn-up), as dashboard.js defines it. */
export function loadRunProgress() {
    loadDashboardScript();
    return { RunProgress: globalThis.__RunProgress, QueueHealth: globalThis.__QueueHealth };
}

/** The topology drill-down's pure layout and edge sizing, as dashboard.js defines it. */
export function loadTopologyFlow() {
    loadDashboardScript();
    return globalThis.__TopologyFlow;
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
/** The shell's attention bar, as index.html declares it. */
export const ATTENTION_MARKUP = `
<section id="attention-bar" hidden>
  <span id="attention-summary"></span><button id="attention-toggle" type="button" hidden>Hide</button>
  <p id="attention-note" hidden></p><ul id="attention-list"></ul>
</section>
<span id="operational-status-dot"></span><span id="operational-status-text"></span>`;

/** The shell's platform health section, as index.html declares it. */
export const PLATFORM_MARKUP = `
<div id="platform-health">
  <ul data-platform-group="applications"></ul><ul data-platform-group="data"></ul>
  <ul data-platform-group="observability"></ul><ul data-platform-jobs></ul>
</div>`;

export const DRAWER_MARKUP = `
<dialog id="queue-drawer">
  <h2 id="queue-drawer-title"></h2><p id="queue-drawer-subtitle"></p>
  <button id="queue-drawer-close" type="button">Close</button>
  <p id="queue-drawer-state"></p><nav id="queue-drawer-links" hidden></nav><dl id="queue-drawer-numbers"></dl>
</dialog>`;

/** The shell's topology drill-down dialog, as index.html declares it. */
export const TOPOLOGY_MARKUP = `
<dialog id="topology-dialog">
  <h2 id="topology-title"></h2><button id="topology-close" type="button">Close</button>
  <div id="topology-flow"></div>
</dialog>`;

/**
 * A dashboard with the live-view side effects stubbed, an empty `#pipelines` container, the drawer,
 * and the topology dialog. `narrow` is a phone (stacked matrix), `wide` a desktop (topology control).
 */
export function makeDashboard({ narrow = false, wide = true } = {}) {
    document.body.innerHTML = `${ATTENTION_MARKUP}<div id="pipelines"></div>${PLATFORM_MARKUP}${DRAWER_MARKUP}${TOPOLOGY_MARKUP}`;
    const Dashboard = loadDashboardClass();
    const dashboard = Object.create(Dashboard.prototype);
    Object.assign(dashboard, {
        narrowViewport: { matches: narrow },
        history: new Map(),
        healthKinds: {},
        drawerQueue: null,
        drawerReturnFocus: null,
        topologyPipeline: null,
        topologyReturnFocus: null,
        wideViewport: { matches: wide },
        queuesUnavailable: {},
        activityLog: [],
        maxLogEntries: 100,
        lastServerEventId: 0,
        auditTimer: null,
        topology: null,
        topologyRequest: null,
        pendingData: null,
        lastReading: null,
        alerts: null,
        attentionSeen: new Set(),
        attentionCollapsed: false,
        linkBases: { grafana: null, traces: null },
        linkSets: new Map(),
        runs: {},
        runProgress: {},
        progressTimer: null,
        overviewView: 'matrix',
    });
    for (const method of ['updateServices', 'updateDatabases', 'updateLastUpdated']) {
        dashboard[method] = vi.fn();
    }
    return dashboard;
}

export const queue = (name, extra = {}) => ({ name, messages: 12, messages_ready: 12, messages_unacknowledged: 0, consumers: 1, ...extra });
