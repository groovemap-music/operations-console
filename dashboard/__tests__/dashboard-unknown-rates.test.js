import { describe, it, expect, beforeEach } from 'vitest';
import { DATASTORES, discogs, makeDashboard, queue as queueOf } from './overview-helpers.js';

/**
 * A null rate means "not computable this interval" (first poll, missed poll, counter reset). The
 * matrix never treats it as a zero: a backlog with only unknown ack rates is not stalled and its
 * lag is unknown, while a stall is the server's verdict, shown as it arrives.
 */
const NAME = 'groovemap-discogs-graphinator-artists';

describe('Dashboard unknown rates', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
    });

    const cell = () => document.getElementById('discogs-graphinator-artists-cell');
    // 40 readings 2 s apart (78 s), all with the given rates.
    const run = (rates) => {
        for (let i = 0; i < 40; i++) {
            const queues = [queueOf(NAME, { messages: 5000, messages_unacknowledged: 200, ...rates })];
            dashboard.updateDashboard({ timestamp: new Date(1e12 + i * 2000).toISOString(), pipelines: { discogs: { services: [], queues, queues_available: true } } });
        }
    };

    it('never reports a stall from null ack rates, and shows the lag as unknown', () => {
        run({ message_rate: null, ack_rate: null });
        expect(cell().dataset.state).toBe('ok');
        expect(cell().querySelector('.mx-flag').textContent).toBe('');
        expect(cell().querySelector('.mx-meta').textContent).toBe('lag unknown');
        expect(cell().className).toBe('mx-cell mx-lag-unknown');
    });

    it('shows the stall the server reports for a computed zero ack rate', () => {
        run({ message_rate: 3, ack_rate: 0, condition: { kind: 'stalled', label: 'STALLED · no acks 1m' } });
        expect(cell().dataset).toMatchObject({ state: 'crit', kind: 'stalled' });
        expect(cell().querySelector('.mx-flag').textContent).toBe('◆ STALLED · no acks 1m');
    });

    it('labels unknown rates as unknown in the drawer, never 0', () => {
        run({ message_rate: null, ack_rate: null });
        dashboard.openQueueDrawer(cell());
        const values = Object.fromEntries([...document.querySelectorAll('#queue-drawer-numbers dt')].map((dt) => [dt.textContent, dt.nextElementSibling.textContent]));
        expect(values).toMatchObject({ 'Publish rate': 'unknown', 'Ack rate': 'unknown', 'Mean ack rate (5 min)': 'unknown', 'No acks for': '—' });
    });
});
