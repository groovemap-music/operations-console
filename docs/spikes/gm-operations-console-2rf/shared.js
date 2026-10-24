// Shared model for the three spike prototypes: fixture selection, the health rules every view
// applies identically, ETA arithmetic, formatting, and the page chrome. Spike code, not product code.
'use strict';

const SPIKE = (() => {
    const params = new URLSearchParams(location.search);
    const data = window.SPIKE_FIXTURES;
    // Only known fixture and theme names reach the markup below.
    const fixtureKey = Object.hasOwn(data.scenarios, params.get('fixture')) ? params.get('fixture') : 'stalled-consumer';
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    const theme = ['dark', 'light'].includes(params.get('theme')) ? params.get('theme') : (prefersDark ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;

    const scenario = data.scenarios[fixtureKey];
    const STEP = data.step_s;
    const SHORT = 6;          // 60 s: a queue with backlog and no ack in this span is stalled
    const TREND = 30;         // 5 min: DLQ growth window, and the ack-rate average used for ETAs

    // ── Formatting ───────────────────────────────────────────────────────────
    function count(n) {
        if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 1 : 2)}M`;
        if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
        if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
        return String(Math.round(n));
    }
    function rate(r) { return `${count(r)}/s`; }
    function duration(s) {
        if (!Number.isFinite(s)) return '∞';
        if (s < 60) return `${Math.max(1, Math.round(s))}s`;
        if (s < 3600) return `${Math.round(s / 60)}m`;
        const h = Math.floor(s / 3600);
        const m = Math.round((s - h * 3600) / 60);
        return m ? `${h}h${String(m).padStart(2, '0')}m` : `${h}h`;
    }
    function pct(f) { return `${(100 * f).toFixed(f >= 0.995 && f < 1 ? 1 : f < 0.1 ? 1 : 0)}%`; }

    // ── Health rules (identical for every view) ──────────────────────────────
    const mean = xs => xs.reduce((a, b) => a + b, 0) / Math.max(1, xs.length);
    function trailingZeroAckSeconds(series) {
        let n = 0;
        for (let i = series.ack_rate.length - 1; i >= 0 && series.ack_rate[i] === 0 && series.messages[i] > 0; i--) n++;
        return n * STEP;
    }
    function assessQueue(q) {
        const last = q.last;
        if (q.dlq) {
            const s = q.series.messages;
            const grown = s[s.length - 1] - s[Math.max(0, s.length - 1 - TREND)];
            if (grown > 0) return { state: 'crit', kind: 'dlq-growing', label: `DLQ ▲ +${count(grown)} / 5m`, grown };
            if (last.messages > 0) return { state: 'warn', kind: 'dlq-static', label: `DLQ ${count(last.messages)} (flat)` };
            return { state: 'ok', kind: 'dlq-empty', label: 'DLQ empty' };
        }
        if (last.messages > 0 && last.consumers === 0) return { state: 'crit', kind: 'no-consumer', label: 'NO CONSUMER' };
        const zero = trailingZeroAckSeconds(q.series);
        if (last.consumers > 0 && zero >= SHORT * STEP) {
            const span = zero >= data.window_s ? `>${duration(data.window_s)}` : duration(zero);
            return { state: 'crit', kind: 'stalled', label: `STALLED · no acks ${span}` };
        }
        if (last.messages === 0 && last.message_rate === 0) return { state: 'ok', kind: 'idle', label: 'drained' };
        return { state: 'ok', kind: 'flowing', label: last.message_rate > last.ack_rate * 1.02 ? 'filling' : 'keeping up' };
    }
    const rank = { ok: 0, warn: 1, crit: 2 };
    const worst = states => states.reduce((a, b) => (rank[b] > rank[a] ? b : a), 'ok');

    // ── Run arithmetic ───────────────────────────────────────────────────────
    // Expected records per entity come from the extractor's groovemap.extraction.file.progress
    // gauge (bytes read / file size): extracted / progress. /health carries counts only.
    function expected(pipeline, entity) {
        const x = pipeline.extractor[entity];
        return x.done || x.file_progress === 0 ? x.extracted : x.extracted / x.file_progress;
    }
    function queueFor(pipeline, consumer, entity, dlq = false) {
        return pipeline.queues.find(q => q.consumer === consumer && q.entity === entity && q.dlq === dlq);
    }
    // Seconds until `consumer` has loaded every expected `entity` record, at its 5-minute mean ack rate.
    function loadEta(pipeline, consumer, entity) {
        const q = queueFor(pipeline, consumer, entity);
        const total = expected(pipeline, entity);
        const remaining = Math.max(0, total - q.acked_since_run_start - queueFor(pipeline, consumer, entity, true).last.messages);
        if (remaining < 1) return 0;
        const ack = mean(q.series.ack_rate.slice(-TREND));
        return ack > 0 ? remaining / ack : Infinity;
    }
    // Seconds behind: how long the current backlog takes at the 5-minute mean ack rate.
    function lag(q) {
        if (q.last.messages === 0) return 0;
        const ack = mean(q.series.ack_rate.slice(-TREND));
        return ack > 0 ? q.last.messages / ack : Infinity;
    }

    // ── SVG helpers ──────────────────────────────────────────────────────────
    function sparkline(values, w, h, cls = '') {
        const max = Math.max(1000, ...values);  // floor: a prefetch window of in-flight messages reads as flat
        const pts = values.map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${(h - (v / max) * h).toFixed(1)}`);
        return `<polyline class="spark ${cls}" fill="none" stroke-width="1.5" points="${pts.join(' ')}"/>`;
    }

    // Marks a fault-bearing element so evaluate.py can find it without reading pixels.
    function unitAttrs(ref, assessment) {
        return `data-unit="${ref}" data-state="${assessment.state}" data-kind="${assessment.kind}"`;
    }

    // ── Chrome ───────────────────────────────────────────────────────────────
    function chrome(view, title) {
        const link = (over, text, current) => {
            const p = new URLSearchParams({ fixture: fixtureKey, theme, ...over });
            const file = over.view ? `${over.view}.html` : `${view}.html`;
            p.delete('view');
            return `<a href="${file}?${p}" aria-current="${current}">${text}</a>`;
        };
        const views = ['flow', 'matrix', 'progress']
            .map(v => link({ view: v }, v === 'flow' ? 'Flow' : v === 'matrix' ? 'Matrix' : 'Run progress', v === view));
        const fixtures = Object.entries(data.scenarios).map(([k, s]) => link({ fixture: k }, s.title, k === fixtureKey));
        const themeLink = link({ theme: theme === 'dark' ? 'light' : 'dark' }, theme === 'dark' ? 'Light' : 'Dark', false);
        document.body.insertAdjacentHTML('afterbegin', `
<header class="chrome">
  <img src="../../../dashboard/static/brand/icon-${theme === 'dark' ? 'dark' : 'light'}.svg" alt="GrooveMap">
  <span class="brand">GrooveMap · ${title}</span>
  <span class="synthetic" title="${data.note}">Synthetic fixture</span>
  <nav aria-label="View">${views.join('')}</nav>
  <nav aria-label="Fixture">${fixtures.join('')}</nav>
  <nav aria-label="Theme">${themeLink}</nav>
</header>`);
    }

    return {
        data, scenario, fixtureKey, theme, STEP, count, rate, duration, pct,
        assessQueue, worst, expected, queueFor, loadEta, lag, sparkline, unitAttrs, chrome, mean,
    };
})();
