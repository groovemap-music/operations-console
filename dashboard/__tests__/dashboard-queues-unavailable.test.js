import { describe, it, expect, beforeEach } from 'vitest';
import { DATASTORES, discogs, makeDashboard, queue as queueOf } from './overview-helpers.js';

/**
 * A failed broker scrape (`queues_available: false`) must render an explicit "Queue metrics
 * unavailable" state in the matrix: the banner, and every cell marked unavailable rather than
 * empty or idle. It clears on recovery.
 */
const queue = queueOf('groovemap-discogs-graphinator-artists', { message_rate: 4.5, ack_rate: 3 });

describe('Dashboard queue metrics unavailable state', () => {
    let dashboard;

    beforeEach(() => {
        dashboard = makeDashboard();
        dashboard.renderTopology({ pipelines: [discogs()], datastores: DATASTORES });
    });

    const banners = () => [...document.querySelectorAll('[data-queues-unavailable="discogs"]')];
    const cells = () => [...document.querySelectorAll('[data-matrix="discogs"] .mx-cell')];
    const artists = () => document.getElementById('discogs-graphinator-artists-cell');
    const update = (queues, available) => dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues, queues_available: available } } });

    it('shows the banner and unavailable cells instead of an empty pipeline, then clears on recovery', () => {
        update([queue], true);
        expect(banners().length).toBe(1);
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
        expect(artists().querySelector('.mx-n').textContent).toBe('12');

        update([], false);
        expect(banners().every((el) => !el.classList.contains('hidden'))).toBe(true);
        expect(banners()[0].textContent).toBe('Queue metrics unavailable');
        expect(cells().length).toBe(8);
        for (const cell of cells()) {
            expect(cell.classList.contains('mx-unavailable')).toBe(true);
            expect(cell.dataset).toMatchObject({ state: 'unknown', kind: 'unavailable' });
            expect(cell.querySelector('.mx-meta').textContent).toBe('unavailable');
            expect(cell.getAttribute('aria-label')).toContain('queue metrics unavailable');
        }

        update([queue], true);
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
        expect(artists().classList.contains('mx-unavailable')).toBe(false);
        expect(artists().querySelector('.mx-n').textContent).toBe('12');
    });

    it('starts the history afresh after an outage, so a stall is never assumed across it', () => {
        update([queue], true);
        expect(dashboard.history.has(queue.name)).toBe(true);
        update([], false);
        expect(dashboard.history.has(queue.name)).toBe(false);
    });

    it('treats a payload without queues_available as available', () => {
        dashboard.updateDashboard({ pipelines: { discogs: { services: [], queues: [queue] } } });
        expect(banners().every((el) => el.classList.contains('hidden'))).toBe(true);
        expect(artists().classList.contains('mx-unavailable')).toBe(false);
    });
});
