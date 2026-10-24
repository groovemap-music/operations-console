import { describe, it, expect, beforeEach } from 'vitest';
import { DATASTORES, discogs, makeDashboard, queue as queueOf } from './overview-helpers.js';

/**
 * A null rate means "not computable this interval". The rate dials render it as an em dash with
 * an accessible "rate unknown" label, never as 0, and a computed 0 still renders as 0.0.
 */
const queue = (rates) => queueOf('groovemap-discogs-graphinator-artists', rates);

describe('Dashboard unknown rates', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
    });

    const publish = () => document.getElementById('discogs-rate-text-graphinator-artists-publish');
    const ack = () => document.getElementById('discogs-rate-text-graphinator-artists-ack');
    const update = (rates) => dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue(rates)], queues_available: true } } });

    it('renders null rates as an em dash labelled rate unknown, never 0', () => {
        update({ message_rate: null, ack_rate: null });
        for (const dial of [publish(), ack()]) {
            expect(dial.textContent).toBe('—');
            expect(dial.getAttribute('aria-label')).toBe('rate unknown');
        }
    });

    it('keeps a computed zero as 0.0 and drops the unknown label once a rate returns', () => {
        update({ message_rate: null, ack_rate: null });
        update({ message_rate: 0, ack_rate: 4.5 });
        expect(publish().textContent).toBe('0.0');
        expect(publish().hasAttribute('aria-label')).toBe(false);
        expect(ack().textContent).toBe('4.5');
        expect(ack().hasAttribute('aria-label')).toBe(false);
    });
});
