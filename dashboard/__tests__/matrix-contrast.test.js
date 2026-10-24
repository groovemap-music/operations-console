import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * WCAG 2.x contrast for every colour pair the Overview matrix, its drawer, and the topology
 * drill-down draw, in both themes: text needs 4.5:1 (1.4.3) and state borders, badges, edges,
 * and focus rings 3:1 (1.4.11). The tokens are read from index.html, so a changed colour is
 * checked where it is defined.
 */
const SHELL = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '..', 'static', 'index.html'), 'utf-8');

function themeTokens(selector) {
    const start = SHELL.indexOf(`${selector} {`);
    expect(start, `${selector} block`).toBeGreaterThan(-1);
    const block = SHELL.slice(start, SHELL.indexOf('}', start));
    const tokens = {};
    for (const [, name, value] of block.matchAll(/--mx-([\w-]+):\s*([^;]+);/g)) {
        if (value.startsWith('#')) tokens[name] = [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16) / 255);
        else if (value.startsWith('rgba')) {
            const [r, g, b, a] = value.match(/[\d.]+/g).map(Number);
            tokens[name] = [r / 255, g / 255, b / 255, a];
        }
    }
    return tokens;
}

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
        },
        graphics: {
            'critical cell border vs surface': [t.crit, t.surface],
            'critical DLQ badge vs critical hatch': [t.crit, critBg],
            'warning badge and banner border vs surface': [t.warn, t.surface],
            'focus ring vs surface': [t.accent, t.surface],
            'unavailable cell border and swatch outline vs surface': [t['text-dim'], t.surface],
            'topology node outline vs surface': [t['text-dim'], t.surface],
            'topology edge (solid, dashed 0/s, dotted unknown) vs surface': [t.edge, t.surface],
        },
    };
}

describe.each([
    ['dark', ':root, [data-theme="dark"]'],
    ['light', '[data-theme="light"]'],
])('matrix colours, %s theme', (_theme, selector) => {
    const t = themeTokens(selector);
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
