import { describe, it, expect } from 'vitest';
import { loadQueueHealth } from './overview-helpers.js';

/**
 * The matrix's health rules (spike gm-operations-console-2rf): stalled and no consumer are the
 * server's verdicts, rendered as given; a DLQ growing over 5 minutes is critical and a non-empty DLQ a warning; lag is
 * backlog over the 5-minute mean ack rate, in four bins.
 */
const { QueueHealth, matrixLayout } = loadQueueHealth();

/** A history of `count` samples `step` seconds apart; `at(i)` overrides fields of sample i. */
function history(count, { step = 2, at = () => ({}), base = {} } = {}) {
    const samples = [];
    for (let i = 0; i < count; i++) {
        QueueHealth.record(samples, {
            t: 1_000_000 + i * step * 1000,
            messages: 500,
            messages_ready: 300,
            messages_unacknowledged: 200,
            consumers: 1,
            message_rate: 10,
            ack_rate: 10,
            ...base,
            ...at(i),
        });
    }
    return samples;
}

describe('stalled and no consumer', () => {
    // The verdicts are the server's (tests/test_events.py); the browser renders them as given.
    it('renders the condition the server sent as critical, with its label', () => {
        const stalled = history(3, { at: (i) => (i === 2 ? { condition: { kind: 'stalled', label: 'STALLED · no acks 5m+' } } : {}) });
        expect(QueueHealth.assessQueue(stalled)).toMatchObject({ state: 'crit', kind: 'stalled', label: 'STALLED · no acks 5m+' });
        const none = history(1, { base: { consumers: 0, condition: { kind: 'no-consumer', label: 'NO CONSUMER' } } });
        expect(QueueHealth.assessQueue(none)).toMatchObject({ state: 'crit', kind: 'no-consumer', label: 'NO CONSUMER' });
    });

    it('clears as soon as the server sends no condition', () => {
        const samples = history(3, { base: { condition: { kind: 'stalled', label: 'STALLED · no acks 1m' } }, at: (i) => (i === 2 ? { condition: null } : {}) });
        expect(QueueHealth.assessQueue(samples)).toMatchObject({ state: 'ok', kind: 'flowing' });
    });

    it('decides neither verdict itself from zero acks or a missing consumer', () => {
        expect(QueueHealth.assessQueue(history(200, { base: { ack_rate: 0 } }))).toMatchObject({ state: 'ok', kind: 'flowing' });
        expect(QueueHealth.assessQueue(history(1, { base: { consumers: 0 } }))).toMatchObject({ state: 'ok', kind: 'flowing' });
        expect(QueueHealth.assessQueue(history(1, { base: { consumers: 0, messages: 0 } }))).toMatchObject({ state: 'ok', kind: 'drained' });
    });

    it('measures the drawer\'s "No acks for" from computed zeros only, never from a null rate', () => {
        expect(QueueHealth.zeroAckSeconds(history(60, { base: { ack_rate: null } }))).toBe(0);
        const samples = history(40, { base: { ack_rate: 0 }, at: (i) => (i === 20 ? { ack_rate: null } : {}) });
        // Zero from sample 21 to 39: 18 intervals of 2 s.
        expect(QueueHealth.zeroAckSeconds(samples)).toBe(36);
    });

    it('reports an unreported queue as unknown, not ok', () => {
        expect(QueueHealth.assessQueue([])).toMatchObject({ state: 'unknown', kind: 'missing' });
    });
});

describe('dead-letter queue', () => {
    it('is critical while it grows within 5 minutes', () => {
        const samples = history(10, { step: 10, at: (i) => ({ messages: 12 + i * 3 }) });
        expect(QueueHealth.assessDlq(samples)).toMatchObject({ state: 'crit', kind: 'dlq-growing', label: 'DLQ +27 / 5m', grown: 27 });
    });

    it('measures growth against the depth 5 minutes ago, not the oldest sample ever seen', () => {
        // Grew from 0 to 12 six minutes ago, flat since.
        const samples = history(50, { step: 10, at: (i) => ({ messages: i < 10 ? i : 12 }) });
        expect(QueueHealth.assessDlq(samples)).toMatchObject({ state: 'warn', kind: 'dlq-static', label: 'DLQ 12' });
    });

    it('is a warning while flat and non-empty, and ok when empty', () => {
        expect(QueueHealth.assessDlq(history(5, { base: { messages: 12 } }))).toMatchObject({ state: 'warn', kind: 'dlq-static' });
        expect(QueueHealth.assessDlq(history(5, { base: { messages: 0 } }))).toMatchObject({ state: 'ok', kind: 'dlq-empty' });
        expect(QueueHealth.assessDlq([])).toMatchObject({ state: 'unknown' });
    });

    it('does not call a shrinking DLQ growing', () => {
        expect(QueueHealth.assessDlq(history(5, { at: (i) => ({ messages: 50 - i }) })).state).toBe('warn');
    });
});

describe('lag', () => {
    it('is the backlog over the 5-minute mean of the computed ack rates', () => {
        const samples = history(10, { base: { messages: 6000 }, at: (i) => ({ ack_rate: i % 2 ? null : 20 + i }) });
        // Computed rates 20, 22, 24, 26, 28: mean 24.
        expect(QueueHealth.meanAckRate(samples)).toBe(24);
        expect(QueueHealth.lagSeconds(samples)).toBe(250);
    });

    it('is 0 with no backlog, Infinity with nothing acked, and null with no computed rate', () => {
        expect(QueueHealth.lagSeconds(history(3, { base: { messages: 0 } }))).toBe(0);
        expect(QueueHealth.lagSeconds(history(3, { base: { ack_rate: 0 } }))).toBe(Infinity);
        expect(QueueHealth.lagSeconds(history(3, { base: { ack_rate: null } }))).toBeNull();
        expect(QueueHealth.lagSeconds([])).toBeNull();
    });

    it('bins into < 1m, < 10m, < 1h and ≥ 1h, with unknown lag in no bin', () => {
        expect([0, 59.9, 60, 599, 600, 3599, 3600, Infinity].map(QueueHealth.lagBin)).toEqual([0, 0, 1, 1, 2, 2, 3, 3]);
        expect(QueueHealth.lagBin(null)).toBeNull();
        expect(QueueHealth.LAG_BINS.map(([, label]) => label)).toEqual(['< 1m', '< 10m', '< 1h', '≥ 1h']);
    });
});

describe('history', () => {
    it('keeps the 5-minute window plus one baseline sample before it', () => {
        const samples = history(100, { step: 10 });
        expect(samples.length).toBe(31);
        expect((samples[samples.length - 1].t - samples[0].t) / 1000).toBe(300);
    });

    it('drops a late sample, replaces one with the same time, and restarts after a gap', () => {
        const samples = history(3);
        expect(QueueHealth.record(samples, { ...samples[2], t: samples[1].t })).toBe(false);
        QueueHealth.record(samples, { ...samples[2], messages: 7 });
        expect(samples.length).toBe(3);
        expect(samples[2].messages).toBe(7);
        QueueHealth.record(samples, { ...samples[2], t: samples[2].t + (QueueHealth.GAP_SECONDS + 1) * 1000 });
        expect(samples.length).toBe(1);
    });

    it('reports the backlog trend over the window, ignoring small changes', () => {
        expect(QueueHealth.trend(history(10, { at: (i) => ({ messages: 1000 + i * 200 }) }))).toEqual({ direction: 'rising', change: 1800 });
        expect(QueueHealth.trend(history(10, { at: (i) => ({ messages: 5000 - i * 200 }) })).direction).toBe('falling');
        expect(QueueHealth.trend(history(10, { at: (i) => ({ messages: 500 + i * 10 }) })).direction).toBe('steady');
    });
});

describe('formatting', () => {
    it('shortens counts and durations the way the spike drew them', () => {
        expect([12, 3000, 44_200, 1_440_000, 17_000_000].map(QueueHealth.formatCount)).toEqual(['12', '3.0k', '44k', '1.44M', '17.0M']);
        expect([0.2, 59, 420, 3600, 9300, Infinity].map(QueueHealth.formatDuration)).toEqual(['1s', '59s', '7m', '1h', '2h35m', '∞']);
    });

    it('ranks states worst first', () => {
        expect(QueueHealth.worst('ok', 'warn')).toBe('warn');
        expect(QueueHealth.worst('crit', 'warn', 'ok')).toBe('crit');
        expect(QueueHealth.worst('unknown', 'ok')).toBe('ok');
    });
});

describe('stacked fallback', () => {
    it('stacks only on a narrow viewport with more than two consumers', () => {
        expect(matrixLayout(4, true)).toBe('stacked');
        expect(matrixLayout(3, true)).toBe('stacked');
        expect(matrixLayout(2, true)).toBe('grid');
        expect(matrixLayout(4, false)).toBe('grid');
    });
});
