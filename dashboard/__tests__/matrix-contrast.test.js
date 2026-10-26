import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * WCAG 2.x contrast for every colour pair the Overview matrix, its drawer, the topology drill-down,
 * the status badges, the charts, and the Identity Ledger draw, in both themes: text needs 4.5:1
 * (1.4.3) and state borders, badges, edges, series lines, and focus rings 3:1 (1.4.11). The tokens
 * are read from index.html (and the vendored brand/tokens.css it links), so a changed colour is
 * checked where it is defined. The chart series are also held at least 15 CIEDE2000 from every
 * Strata state and entity colour and from one another.
 */
const STATIC = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const SHELL = readFileSync(resolve(STATIC, 'index.html'), 'utf-8');
const BRAND = readFileSync(resolve(STATIC, 'brand', 'tokens.css'), 'utf-8');

const declarations = (block) => Object.fromEntries([...block.matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]));
const BRAND_TOKENS = declarations(BRAND);

function themeBlock(selector) {
    const start = SHELL.indexOf(`${selector} {`);
    expect(start, `${selector} block`).toBeGreaterThan(-1);
    return declarations(SHELL.slice(start, SHELL.indexOf('}', start)));
}

const DARK_BLOCK = themeBlock(':root, [data-theme="dark"]');
const LIGHT_BLOCK = themeBlock('[data-theme="light"]');

const rgbOf = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);

/** A colour token as [r, g, b] (and alpha for rgba), following var() through the theme and the brand tokens. */
function resolveToken(theme, name, depth = 0) {
    const own = theme === 'light' ? { ...DARK_BLOCK, ...LIGHT_BLOCK } : DARK_BLOCK;
    const value = own[name] ?? BRAND_TOKENS[name];
    expect(value, `--${name}`).toBeDefined();
    expect(depth, `--${name} resolves`).toBeLessThan(8);
    const ref = value.match(/^var\(--([\w-]+)\)$/);
    if (ref) return resolveToken(theme, ref[1], depth + 1);
    if (value.startsWith('#')) return rgbOf(value);
    if (value.startsWith('rgba')) {
        const [r, g, b, a] = value.match(/[\d.]+/g).map(Number);
        return [r / 255, g / 255, b / 255, a];
    }
    throw new Error(`--${name}: unsupported colour ${value}`);
}

function themeTokens(theme) {
    const own = theme === 'light' ? { ...DARK_BLOCK, ...LIGHT_BLOCK } : DARK_BLOCK;
    const tokens = {};
    for (const name of Object.keys(own)) {
        if (name.startsWith('mx-')) tokens[name.slice(3)] = resolveToken(theme, name);
    }
    return tokens;
}

const ui = (theme, name) => resolveToken(theme, name);

const blend = ([r, g, b, a], under) => [r, g, b].map((c, i) => a * c + (1 - a) * under[i]);
const linear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const luminance = (rgb) => {
    const [r, g, b] = rgb.map(linear);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a, b) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
};
const lightness = (rgb) => {
    const y = luminance(rgb);
    return y > 216 / 24389 ? 116 * Math.cbrt(y) - 16 : (24389 / 27) * y;
};

// CIEDE2000 (Sharma, Wu, and Dalal), as the design repository's brand-tokens.mjs computes it.
function toLab(rgb) {
    const [r, g, b] = rgb.map(linear);
    const x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047;
    const y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b;
    const z = (0.0193339 * r + 0.119192 * g + 0.9503041 * b) / 1.08883;
    const f = (t) => (t > 216 / 24389 ? Math.cbrt(t) : ((24389 / 27) * t + 16) / 116);
    const [fx, fy, fz] = [f(x), f(y), f(z)];
    return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}
const rad = (degrees) => (degrees * Math.PI) / 180;
const deg = (radians) => ((radians * 180) / Math.PI + 360) % 360;
function deltaE2000(first, second) {
    const [L1, a1, b1] = toLab(first);
    const [L2, a2, b2] = toLab(second);
    const meanC = (Math.hypot(a1, b1) + Math.hypot(a2, b2)) / 2;
    const g = 0.5 * (1 - Math.sqrt(meanC ** 7 / (meanC ** 7 + 25 ** 7)));
    const [ap1, ap2] = [(1 + g) * a1, (1 + g) * a2];
    const [C1, C2] = [Math.hypot(ap1, b1), Math.hypot(ap2, b2)];
    const [h1, h2] = [C1 === 0 ? 0 : deg(Math.atan2(b1, ap1)), C2 === 0 ? 0 : deg(Math.atan2(b2, ap2))];
    const dL = L2 - L1;
    const dC = C2 - C1;
    let dh = 0;
    if (C1 * C2 !== 0) {
        dh = h2 - h1;
        if (dh > 180) dh -= 360;
        else if (dh < -180) dh += 360;
    }
    const dH = 2 * Math.sqrt(C1 * C2) * Math.sin(rad(dh / 2));
    const meanL = (L1 + L2) / 2;
    const meanCp = (C1 + C2) / 2;
    let meanH = h1 + h2;
    if (C1 * C2 !== 0) {
        meanH = Math.abs(h1 - h2) <= 180 ? (h1 + h2) / 2 : h1 + h2 < 360 ? (h1 + h2 + 360) / 2 : (h1 + h2 - 360) / 2;
    }
    const t = 1 - 0.17 * Math.cos(rad(meanH - 30)) + 0.24 * Math.cos(rad(2 * meanH)) + 0.32 * Math.cos(rad(3 * meanH + 6)) - 0.2 * Math.cos(rad(4 * meanH - 63));
    const sL = 1 + (0.015 * (meanL - 50) ** 2) / Math.sqrt(20 + (meanL - 50) ** 2);
    const sC = 1 + 0.045 * meanCp;
    const sH = 1 + 0.015 * meanCp * t;
    const rT = -2 * Math.sqrt(meanCp ** 7 / (meanCp ** 7 + 25 ** 7)) * Math.sin(rad(60 * Math.exp(-(((meanH - 275) / 25) ** 2))));
    return Math.sqrt((dL / sL) ** 2 + (dC / sC) ** 2 + (dH / sH) ** 2 + rT * (dC / sC) * (dH / sH));
}

function pairs(t) {
    const critBg = blend(t['crit-bg'], t.surface);
    return {
        text: {
            'text-high on surface (labels, numbers, drawer values)': [t['text-high'], t.surface],
            'text-mid on surface (column heads, meta, legend, drawer terms)': [t['text-mid'], t.surface],
            'text-dim on surface (pipeline title, extraction state)': [t['text-dim'], t.surface],
            'accent on surface (active extraction)': [t.accent, t.surface],
            'text-high on lag bin 0': [t['text-high'], t['ramp-0']],
            'text-high on lag bin 1': [t['text-high'], t['ramp-1']],
            'text-high on lag bin 2 (number and meta)': [t['text-high'], t['ramp-2']],
            'text-mid on lag bin 0 (meta)': [t['text-mid'], t['ramp-0']],
            'text-mid on lag bin 1 (meta)': [t['text-mid'], t['ramp-1']],
            'ramp-3-text on lag bin 3': [t['ramp-3-text'], t['ramp-3']],
            'text-high on critical hatch': [t['text-high'], critBg],
            'text-mid on critical hatch (meta)': [t['text-mid'], critBg],
            'crit on critical hatch (flag)': [t.crit, critBg],
            'crit on surface (flag between hatch stripes, legend, drawer)': [t.crit, t.surface],
            'warn on surface (DLQ warning badge, banner, legend, drawer)': [t.warn, t.surface],
            'surface on crit (filled DLQ critical badge)': [t.surface, t.crit],
            'ok on surface (platform health badge)': [t.ok, t.surface],
        },
        graphics: {
            'critical cell border vs surface': [t.crit, t.surface],
            'critical DLQ badge vs critical hatch': [t.crit, critBg],
            'warning badge and banner border vs surface': [t.warn, t.surface],
            'platform health ok badge border vs surface': [t.ok, t.surface],
            'focus ring vs surface': [t.accent, t.surface],
            'unavailable cell border and swatch outline vs surface': [t['text-dim'], t.surface],
            'topology node outline vs surface': [t['text-dim'], t.surface],
            'topology edge (solid, dashed 0/s, dotted unknown) vs surface': [t.edge, t.surface],
        },
    };
}

const THEMES = ['dark', 'light'];

describe.each(THEMES)('matrix colours, %s theme', (theme) => {
    const t = themeTokens(theme);
    const { text, graphics } = pairs(t);

    it.each(Object.entries(text))('text %s meets 4.5:1', (_name, [fg, bg]) => {
        expect(contrast(fg, bg)).toBeGreaterThanOrEqual(4.5);
    });

    it.each(Object.entries(graphics))('graphic %s meets 3:1', (_name, [fg, bg]) => {
        expect(contrast(fg, bg)).toBeGreaterThanOrEqual(3);
    });

    it('steps the lag ramp in lightness, so its bins stay apart without hue', () => {
        const steps = [0, 1, 2].map((i) => Math.abs(lightness(t[`ramp-${i + 1}`]) - lightness(t[`ramp-${i}`])));
        for (const step of steps) expect(step).toBeGreaterThanOrEqual(8);
    });
});

describe.each(THEMES)('console surfaces and text, %s theme', (theme) => {
    const surfaces = ['bg-void', 'bg-deep', 'card-bg', 'inner-bg'];
    // Badges sit on cards, inner panels and the ledger's deep panel; the page canvas (bg-void) carries only text.
    const badgeSurfaces = surfaces.slice(1);
    const fg = (name) => ui(theme, name);

    it.each(surfaces)('text-high, text-mid, text-dim and the muted text meet 4.5:1 on %s', (surface) => {
        for (const name of ['text-high', 'text-mid', 'text-dim', 'text-muted-aa']) {
            expect(contrast(fg(name), fg(surface)), `${name} on ${surface}`).toBeGreaterThanOrEqual(4.5);
        }
    });

    it.each(surfaces)('the accent and secondary data colour meet 4.5:1 on %s', (surface) => {
        expect(contrast(fg('accent'), fg(surface))).toBeGreaterThanOrEqual(4.5);
        expect(contrast(fg('secondary'), fg(surface))).toBeGreaterThanOrEqual(4.5);
    });

    it('puts readable text on the accent fill and on the status fills (buttons, tabs, toasts)', () => {
        expect(contrast(fg('on-accent'), fg('accent'))).toBeGreaterThanOrEqual(4.5);
        for (const status of ['status-ok', 'status-err']) expect(contrast(fg('on-status'), fg(status))).toBeGreaterThanOrEqual(4.5);
    });

    it.each(['ok', 'warn', 'err'])('status %s text meets 4.5:1 on every surface and on its own badge tint', (name) => {
        const colour = fg(`status-${name}`);
        for (const surface of surfaces) {
            expect(contrast(colour, fg(surface)), surface).toBeGreaterThanOrEqual(4.5);
        }
        for (const surface of badgeSurfaces) {
            expect(contrast(colour, blend([...colour, 0.08], fg(surface))), `${surface} tint`).toBeGreaterThanOrEqual(4.5);
        }
    });

    it('keeps dark surfaces that carry Strata state colours no lighter than #141310', () => {
        if (theme !== 'dark') return;
        for (const surface of surfaces) {
            expect(luminance(fg(surface))).toBeLessThanOrEqual(luminance(rgbOf('#141310')));
        }
    });

    it('draws every identity state at 4.5:1 or better on every surface, and tells the states apart', () => {
        const states = ['not-recorded', 'discogs-only', 'musicbrainz-only', 'unresolved', 'split', 'linked', 'superseded'];
        for (const state of states) {
            const colour = fg(`id-${state}`);
            expect(colour, state).toEqual(resolveToken(theme, `groovemap-state-identity-${state}-${theme}`));
            for (const surface of surfaces) {
                expect(contrast(colour, fg(surface)), `${state} on ${surface}`).toBeGreaterThanOrEqual(4.5);
            }
        }
        for (const [i, first] of states.entries()) {
            for (const second of states.slice(i + 1)) {
                expect(deltaE2000(fg(`id-${first}`), fg(`id-${second}`)), `${first} vs ${second}`).toBeGreaterThanOrEqual(15);
            }
        }
    });

    it('keeps a CSS rule in the shell for every identity state', () => {
        for (const state of ['not_recorded', 'discogs_only', 'musicbrainz_only', 'unresolved', 'split', 'linked', 'superseded']) {
            expect(SHELL).toContain(`.ledger-badge[data-state="${state}"]`);
        }
    });
});

describe.each(THEMES)('chart series, %s theme', (theme) => {
    const series = Array.from({ length: 9 }, (_, i) => [`series-${i + 1}`, ui(theme, `series-${i + 1}`)]);
    const named = (prefix, suffix = '') => Object.keys(BRAND_TOKENS)
        .filter((name) => name.startsWith(prefix) && name.endsWith(suffix))
        .map((name) => [name, resolveToken(theme, name)]);
    const states = named('groovemap-state-', `-${theme}`);
    const entities = named('groovemap-entity-');
    const brand = [
        ['groovemap-brass', resolveToken(theme, 'groovemap-brass')],
        ['groovemap-oxblood', resolveToken(theme, 'groovemap-oxblood')],
    ];

    it('has the Strata state and entity colours to compare against', () => {
        expect(states).toHaveLength(21);
        expect(entities).toHaveLength(5);
    });

    it('starts with the accent and the secondary data colour', () => {
        expect(ui(theme, 'series-1')).toEqual(ui(theme, 'accent'));
        expect(ui(theme, 'series-2')).toEqual(ui(theme, 'secondary'));
    });

    it.each(series)('%s draws at 3:1 against the card and the canvas, as a line or bar', (_name, colour) => {
        expect(contrast(colour, ui(theme, 'card-bg'))).toBeGreaterThanOrEqual(3);
        expect(contrast(colour, ui(theme, 'bg-void'))).toBeGreaterThanOrEqual(3);
    });

    it.each(series)('%s is at least 15 CIEDE2000 from every state, entity and brand colour', (name, colour) => {
        const accent = theme === 'dark' ? 'groovemap-brass' : 'groovemap-oxblood';
        for (const [other, value] of [...states, ...entities, ...brand]) {
            if (name === 'series-1' && other === accent) continue; // series 1 is the accent itself
            expect(deltaE2000(colour, value), `${name} vs ${other}`).toBeGreaterThanOrEqual(15);
        }
    });

    it('keeps every pair of series at least 15 CIEDE2000 apart', () => {
        for (const [i, [firstName, first]] of series.entries()) {
            for (const [secondName, second] of series.slice(i + 1)) {
                expect(deltaE2000(first, second), `${firstName} vs ${secondName}`).toBeGreaterThanOrEqual(15);
            }
        }
    });

    it('draws every ledger event in a colour at least 15 CIEDE2000 from every state, entity, brand and other event colour, readable at 4.5:1', () => {
        const ledger = readFileSync(resolve(STATIC, 'admin.js'), 'utf-8');
        const block = ledger.slice(ledger.indexOf('const LEDGER_EVENT_COLORS = {'));
        const events = [...block.slice(0, block.indexOf('};')).matchAll(/(\w+): '--([\w-]+)'/g)].map(([, type, name]) => [type, ui(theme, name)]);
        expect(events.map(([type]) => type)).toHaveLength(10);
        for (const [i, [type, colour]] of events.entries()) {
            for (const [other, value] of [...states, ...entities, ...brand]) {
                if (other === (theme === 'dark' ? 'groovemap-brass' : 'groovemap-oxblood') && colour.join() === ui(theme, 'accent').join()) continue;
                expect(deltaE2000(colour, value), `${type} vs ${other}`).toBeGreaterThanOrEqual(15);
            }
            for (const [otherType, other] of events.slice(i + 1)) {
                expect(deltaE2000(colour, other), `${type} vs ${otherType}`).toBeGreaterThanOrEqual(15);
            }
            for (const surface of ['bg-void', 'bg-deep', 'card-bg', 'inner-bg']) {
                expect(contrast(colour, ui(theme, surface)), `${type} on ${surface}`).toBeGreaterThanOrEqual(type === 'minted' ? 4.5 : 3);
            }
        }
    });

    it('draws the axis grid and ticks from the border and muted-text tokens, ticks at 4.5:1', () => {
        expect(ui(theme, 'chart-grid')).toEqual(ui(theme, 'border-color'));
        expect(ui(theme, 'chart-tick')).toEqual(ui(theme, 'text-dim'));
        expect(contrast(ui(theme, 'chart-tick'), ui(theme, 'card-bg'))).toBeGreaterThanOrEqual(4.5);
    });
});

describe('the shell carries no pre-brand colour', () => {
    it('has no Deep Space, cool blue-gray or Tailwind blue-family literal or utility', () => {
        const legacy = /#0f1a2e|#162540|#2a3d58|#b0bec5|#7a8ba3|#8a9bb3|#c3d0de|#2c3e55|#0d1b2a|#334155|#94a3b8|#818cf8|#3B82F6|#6B7280|\b(?:text|bg|border|ring|from|to|via|fill|stroke)-(?:slate|gray|zinc|blue|indigo|violet|sky|fuchsia|purple|cyan)\b|Space Grotesk/i;
        for (const file of ['index.html', 'admin.js', 'dashboard.js']) {
            expect(readFileSync(resolve(STATIC, file), 'utf-8').match(legacy)?.[0], file).toBeUndefined();
        }
    });
});
