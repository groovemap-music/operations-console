import { describe, it, expect, beforeEach } from 'vitest';
import { DATASTORES, discogs, makeDashboard, queue as queueOf } from './overview-helpers.js';

/**
 * A failed broker scrape (`queues_available: false`) must render an explicit "Queue metrics
 * unavailable" state in the queue chart and the rate dials, and clear it on recovery, never an
 * idle-looking empty pipeline.
 */
const queue = queueOf('groovemap-discogs-graphinator-artists', { message_rate: 4.5, ack_rate: 3 });

describe('Dashboard queue metrics unavailable state', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
    });

    const banners = () => [...document.querySelectorAll('[data-queues-unavailable="discogs"]')];
    const dial = () => document.getElementById('discogs-rate-text-graphinator-artists-publish');

    it('shows the banners and N/A dials instead of an empty pipeline, then clears on recovery', () => {
        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue], queues_available: true } } });
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
        expect(dial().textContent).not.toBe('N/A');

        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [], queues_available: false } } });
        expect(banners().every((el) => !el.classList.contains('hidden'))).toBe(true);
        expect(dial().textContent).toBe('N/A');

        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue], queues_available: true } } });
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
        expect(dial().textContent).toBe('4.5');
    });

    it('treats a payload without queues_available as available', () => {
        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue] } } });
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
    });
});
