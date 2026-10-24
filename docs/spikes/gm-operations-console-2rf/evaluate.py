"""Measure the three spike prototypes against the synthetic fixtures and capture screenshots.

There are no human operators for this spike, so the "time to find" comparison uses a fixation
model computed from the rendered DOM rather than stopwatch trials. See the spike document's Method
for what the model can and cannot show. Everything here is deterministic and re-runnable:

    uv run python docs/spikes/gm-operations-console-2rf/simulate.py
    uv run python docs/spikes/gm-operations-console-2rf/evaluate.py

Writes ``evaluation.json`` and ``screenshots/*.png`` next to this file and prints Markdown tables.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


HERE = Path(__file__).resolve().parent
SHOTS = HERE / "screenshots"
VIEWS = ("flow", "matrix", "progress")
FIXTURES = ("healthy", "stalled-consumer", "growing-dlq", "stalled-consumer-2x")
VIEWPORTS = {
    "desktop": {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1},
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
}
# The unit a view must surface for each injected fault (pipeline/consumer/entity[.dlq]).
TARGETS = {
    "stalled-consumer": "discogs/tableinator/releases",
    "stalled-consumer-2x": "discogs/tableinator/releases",
    "growing-dlq": "musicbrainz/brainzgraphinator/releases.dlq",
}
# Curated screenshots committed as evidence: (view, fixture, theme, viewport, vision deficiency).
CAPTURES = [
    *((view, "stalled-consumer", "dark", "desktop", None) for view in VIEWS),
    *((view, "stalled-consumer", "light", "desktop", None) for view in VIEWS),
    *((view, "growing-dlq", "dark", "desktop", None) for view in VIEWS),
    *((view, "stalled-consumer-2x", "dark", "desktop", None) for view in VIEWS),
    *((view, "stalled-consumer", "dark", "phone", None) for view in VIEWS),
    *((view, "growing-dlq", "light", "phone", None) for view in VIEWS),
    *((view, "stalled-consumer-2x", "light", "phone", None) for view in VIEWS),
    ("matrix", "stalled-consumer-2x&layout=grid", "light", "phone", None),
    *((view, "growing-dlq", "light", "desktop", "deuteranopia") for view in VIEWS),
    *((view, "stalled-consumer", "dark", "desktop", "achromatopsia") for view in VIEWS),
]

MEASURE = """
(target) => {
  const units = [...document.querySelectorAll('[data-unit]')].map(el => {
    const r = el.getBoundingClientRect();
    return { ref: el.dataset.unit, state: el.dataset.state, kind: el.dataset.kind, text: el.textContent.replace(/\\s+/g, ' ').trim(),
             top: r.top + scrollY, left: r.left + scrollX, width: r.width, height: r.height, el };
  });
  // Horizontal scroll containers shift what is visible; record each unit's scrollable ancestor offset.
  const scrollParent = el => { for (let p = el.parentElement; p; p = p.parentElement) { if (p.scrollWidth > p.clientWidth + 1 && getComputedStyle(p).overflowX !== 'visible') return p; } return null; };
  const t = units.find(u => u.ref === target);
  let targetInfo = null;
  if (t) {
    const sp = scrollParent(t.el);
    const spRect = sp ? sp.getBoundingClientRect() : null;
    const visibleRight = spRect ? Math.min(innerWidth, spRect.right) : innerWidth;
    // A DLQ marker drawn inside its queue's node or cell borrows that container's printed names.
    const containers = units.filter(u => u !== t && u.left <= t.left + 1 && u.top <= t.top + 1
      && u.left + u.width >= t.left + t.width - 1 && u.top + u.height >= t.top + t.height - 1);
    targetInfo = { ref: t.ref, state: t.state, text: [t.text, ...containers.map(u => u.text)].join(' '), top: t.top, left: t.left, width: t.width, height: t.height,
                   inFirstScreen: t.top + Math.min(t.height, 30) <= innerHeight && t.left + Math.min(t.width, 60) <= visibleRight,
                   horizontalScrollNeeded: Math.max(0, t.left + Math.min(t.width, 60) - visibleRight) };
  }
  // Text legibility: smallest rendered font size and overlapping text boxes.
  const walker = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  const boxes = []; let minFont = Infinity;
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim()) continue;
    const host = n.parentElement;
    const size = parseFloat(getComputedStyle(host).fontSize);
    const range = document.createRange(); range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      boxes.push({ host, l: r.left + scrollX, t: r.top + scrollY, r: r.right + scrollX, b: r.bottom + scrollY });
      minFont = Math.min(minFont, size);
    }
  }
  // Words the layout split across lines (e.g. "tablei / nator" in a narrow column).
  let brokenWords = 0;
  const walker2 = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  for (let n = walker2.nextNode(); n; n = walker2.nextNode()) {
    for (const m of n.textContent.matchAll(/[A-Za-z][\\w-]{2,}/g)) {
      const range = document.createRange(); range.setStart(n, m.index); range.setEnd(n, m.index + m[0].length);
      const rows = new Set([...range.getClientRects()].filter(r => r.width > 0).map(r => Math.round(r.top)));
      if (rows.size > 1) brokenWords++;
    }
  }
  let collisions = 0;
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    if (a.host === b.host) continue;
    const w = Math.min(a.r, b.r) - Math.max(a.l, b.l), h = Math.min(a.b, b.b) - Math.max(a.t, b.t);
    if (w > 2 && h > 2) collisions++;
  }
  // Text that spills out of its SVG node rectangle (flow view) or its HTML cell.
  let spills = 0;
  document.querySelectorAll('[data-unit]').forEach(u => {
    if (!(u instanceof SVGElement) && u.scrollWidth > u.clientWidth + 1) spills++;
    const shape = u.querySelector('rect') || u;
    const box = shape.getBoundingClientRect();
    u.querySelectorAll('text, .n, .meta, .flag').forEach(tx => {
      const r = tx.getBoundingClientRect();
      if (r.right > box.right + 1 || r.left < box.left - 1) spills++;
    });
  });
  const doc = document.scrollingElement;
  const inner = [...document.querySelectorAll('.scroll-x')].some(e => e.scrollWidth > e.clientWidth + 1);
  return {
    units: units.map(({ el, ...u }) => u), target: targetInfo, minFont, collisions, spills, brokenWords,
    pageHeight: doc.scrollHeight, pageWidth: doc.scrollWidth, viewportWidth: innerWidth, viewportHeight: innerHeight,
    horizontalOverflow: doc.scrollWidth > innerWidth + 1 || inner,
  };
}
"""


def url(view: str, fixture: str, theme: str) -> str:
    return f"{(HERE / f'{view}.html').as_uri()}?fixture={fixture}&theme={theme}"


def fixations(result: dict, fixture: str) -> dict:
    """Fixation-count model for locating and naming the injected fault.

    1 fixation if the target pops out (it is the only critical unit outside its own consumer),
    +1 per additional screen of scrolling before it is visible, +1 per competing critical unit that
    must be inspected and dismissed, +1 per identifying name (consumer, entity) not printed inside
    the target unit or the node/cell that visually contains it (read from a row or column header instead).
    """
    target = TARGETS[fixture.split("&", maxsplit=1)[0]]
    info = result["target"]
    if info is None or info["state"] != "crit":
        return {"found": False, "fixations": None}
    pipeline, consumer, entity = target.removesuffix(".dlq").split("/")
    related = {target, target.removesuffix(".dlq"), f"{pipeline}/{consumer}"}
    competing = [u["ref"] for u in result["units"] if u["state"] == "crit" and u["ref"] not in related]
    screens = 0 if info["inFirstScreen"] else math.ceil(max(info["top"] - result["viewportHeight"], 1) / result["viewportHeight"])
    if info["horizontalScrollNeeded"]:
        screens += 1
    missing_names = sum(1 for name in (consumer, entity) if not re.search(rf"\b{re.escape(name)}\b", info["text"]))
    return {
        "found": True,
        "in_first_screen": info["inFirstScreen"],
        "screens_to_scroll": screens,
        "competing_critical": competing,
        "names_outside_unit": missing_names,
        "fixations": 1 + screens + len(competing) + missing_names,
    }


def measure(page: Page) -> dict:
    results: dict = {}
    combos = [*((view, fixture) for view in VIEWS for fixture in FIXTURES), ("matrix", "stalled-consumer-2x&layout=grid")]
    for view, fixture in combos:
        base = fixture.split("&")[0]
        for vp_name in VIEWPORTS:
            page.set_viewport_size(VIEWPORTS[vp_name]["viewport"])
            page.goto(url(view, fixture, "dark"))
            raw = page.evaluate(MEASURE, TARGETS.get(base, ""))
            entry = {
                "units": len(raw["units"]),
                "critical": sorted(u["ref"] for u in raw["units"] if u["state"] == "crit"),
                "warning": sorted(u["ref"] for u in raw["units"] if u["state"] == "warn"),
                "state_has_glyph": all(
                    ("◆" in u["text"]) if u["state"] == "crit" else ("▲" in u["text"]) for u in raw["units"] if u["state"] != "ok"
                ),
                "min_font_px": raw["minFont"],
                "text_collisions": raw["collisions"],
                "text_spills": raw["spills"],
                "broken_words": raw["brokenWords"],
                "page_height_px": raw["pageHeight"],
                "horizontal_overflow": raw["horizontalOverflow"],
            }
            if base in TARGETS:
                entry["find"] = fixations(raw, fixture)
            results[f"{view}|{fixture}|{vp_name}"] = entry
    return results


# ── Colour: WCAG contrast and colour-vision-deficiency separation ──────────────────────────────

MACHADO = {  # Machado, Oliveira & Fernandes (2009), severity 1.0, applied in linear RGB
    "protanopia": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deuteranopia": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "tritanopia": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900)),
}


def tokens() -> dict[str, dict[str, tuple[float, ...]]]:
    css = (HERE / "shared.css").read_text()
    themes = {}
    for theme, block in (
        ("dark", css.split('[data-theme="dark"]', 1)[1].split("}", 1)[0]),
        ("light", css.split('[data-theme="light"]', 1)[1].split("}", 1)[0]),
    ):
        values = {}
        for name, value in re.findall(r"--([\w-]+):\s*([^;]+);", block):
            if value.startswith("#"):
                values[name] = tuple(int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))
            elif value.startswith("rgba"):
                values[name] = tuple(float(x) for x in re.findall(r"[\d.]+", value))
        themes[theme] = values
    return themes


def blend(over: tuple[float, ...], under: tuple[float, ...]) -> tuple[float, ...]:
    r, g, b, a = over
    return tuple(a * c / 255 + (1 - a) * u for c, u in zip((r, g, b), under, strict=True))


def linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[float, ...]) -> float:
    r, g, b = (linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def simulate(rgb: tuple[float, ...], deficiency: str) -> tuple[float, ...]:
    lin = [linear(c) for c in rgb]
    if deficiency == "achromatopsia":
        y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        out = [y, y, y]
    else:
        out = [max(0.0, min(1.0, sum(m * c for m, c in zip(row, lin, strict=True)))) for row in MACHADO[deficiency]]
    return tuple(12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in out)


def lab(rgb: tuple[float, ...]) -> tuple[float, float, float]:
    r, g, b = (linear(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def ciede2000(c1: tuple[float, ...], c2: tuple[float, ...]) -> float:
    l1, a1, b1 = lab(c1)
    l2, a2, b2 = lab(c2)
    cbar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25**7)))
    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dl, dc = l2 - l1, c2p - c1p
    dh = 0.0 if c1p * c2p == 0 else (h2p - h1p if abs(h2p - h1p) <= 180 else h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360)
    dhh = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    lbar, cbarp = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbar = (h1p + h2p) / 2
    else:
        hbar = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = 1 - 0.17 * math.cos(math.radians(hbar - 30)) + 0.24 * math.cos(math.radians(2 * hbar)) + 0.32 * math.cos(math.radians(3 * hbar + 6))
    t -= 0.20 * math.cos(math.radians(4 * hbar - 63))
    sl = 1 + 0.015 * (lbar - 50) ** 2 / math.sqrt(20 + (lbar - 50) ** 2)
    sc, sh = 1 + 0.045 * cbarp, 1 + 0.015 * cbarp * t
    rt = -2 * math.sqrt(cbarp**7 / (cbarp**7 + 25**7)) * math.sin(math.radians(60 * math.exp(-(((hbar - 275) / 25) ** 2))))
    return math.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dhh / sh) ** 2 + rt * (dc / sc) * (dhh / sh))


def colour_report() -> dict:
    report: dict = {}
    for theme, t in tokens().items():
        surface = t["surface"]
        crit_bg = blend(t["crit-bg"], surface)
        warn_bg = blend(t["warn-bg"], surface)
        text_pairs = {  # (foreground, background) pairs the prototypes actually draw as text; WCAG 1.4.3 needs 4.5
            "text-high on bg": (t["text-high"], t["bg"]),
            "text-high on inner": (t["text-high"], t["inner"]),
            "text-mid on surface": (t["text-mid"], surface),
            "text-dim on surface": (t["text-dim"], surface),
            "text-dim on inner": (t["text-dim"], t["inner"]),
            "crit on surface": (t["crit"], surface),
            "crit on crit-bg": (t["crit"], crit_bg),
            "surface on crit (filled DLQ badge)": (surface, t["crit"]),
            "warn on surface": (t["warn"], surface),
            "warn on warn-bg": (t["warn"], warn_bg),
            "accent on surface": (t["accent"], surface),
            **{f"text-high on ramp-{i}": (t["text-high"], t[f"ramp-{i}"]) for i in range(3)},
            **{f"text-mid on ramp-{i}": (t["text-mid"], t[f"ramp-{i}"]) for i in range(2)},
            "ramp-3-text on ramp-3": (t["ramp-3-text"], t["ramp-3"]),
        }
        graphic_pairs = {  # borders and marks; WCAG 1.4.11 needs 3.0
            "crit border vs surface": (t["crit"], surface),
            "warn border vs surface": (t["warn"], surface),
            "accent bar vs inner": (t["accent"], t["inner"]),
            "edge vs surface": (t["edge"], surface),
        }
        states = {"ok": t["accent"], "warn": t["warn"], "crit": t["crit"]}
        cvd = {}
        for deficiency in ("none", "protanopia", "deuteranopia", "tritanopia", "achromatopsia"):
            sim = {k: (v if deficiency == "none" else simulate(v, deficiency)) for k, v in states.items()}
            ramp = [t[f"ramp-{i}"] if deficiency == "none" else simulate(t[f"ramp-{i}"], deficiency) for i in range(4)]
            cvd[deficiency] = {
                "crit-warn": round(ciede2000(sim["crit"], sim["warn"]), 1),
                "crit-ok": round(ciede2000(sim["crit"], sim["ok"]), 1),
                "warn-ok": round(ciede2000(sim["warn"], sim["ok"]), 1),
                "ramp min adjacent": round(min(ciede2000(ramp[i], ramp[i + 1]) for i in range(3)), 1),
            }
        report[theme] = {
            "text": {k: round(contrast(*v), 2) for k, v in text_pairs.items()},
            "graphics": {k: round(contrast(*v), 2) for k, v in graphic_pairs.items()},
            "cvd_delta_e2000": cvd,
        }
    return report


def capture(browser_type: object) -> list[str]:
    SHOTS.mkdir(exist_ok=True)
    for old in SHOTS.glob("*.png"):
        old.unlink()
    names = []
    for view, fixture, theme, vp_name, deficiency in CAPTURES:
        context = browser_type.new_context(**VIEWPORTS[vp_name])  # type: ignore[attr-defined]
        page = context.new_page()
        if deficiency:
            context.new_cdp_session(page).send("Emulation.setEmulatedVisionDeficiency", {"type": deficiency})
        page.goto(url(view, fixture, theme))
        name = f"{view}-{fixture.replace('&layout=', '-')}-{theme}-{vp_name}{'-' + deficiency if deficiency else ''}.png"
        page.screenshot(path=SHOTS / name, full_page=vp_name == "desktop")
        names.append(name)
        context.close()
    return names


def tables(results: dict, colours: dict) -> str:
    out = [
        "### Find the fault (fixation model, dark theme)",
        "",
        "| View | Fixture | Viewport | Found | First screen | Competing critical | Names outside unit | Fixations |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, r in results.items():
        if "find" not in r:
            continue
        view, fixture, vp = key.split("|")
        f = r["find"]
        if not f["found"]:
            out.append(f"| {view} | {fixture} | {vp} | no | | | | — |")
            continue
        out.append(
            f"| {view} | {fixture} | {vp} | yes | {'yes' if f['in_first_screen'] else f'no (+{f["screens_to_scroll"]})'} | {len(f['competing_critical'])} | {f['names_outside_unit']} | {f['fixations']} |"
        )
    out += [
        "",
        "### Legibility and density (dark theme)",
        "",
        "| View | Fixture | Viewport | Units | Critical | Warning | Glyph on every non-ok state | Min font px | Text collisions | Text spills | Broken words | Page height px | Horizontal overflow |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, r in results.items():
        view, fixture, vp = key.split("|")
        out.append(
            f"| {view} | {fixture} | {vp} | {r['units']} | {len(r['critical'])} | {len(r['warning'])} | {'yes' if r['state_has_glyph'] else 'NO'} | {r['min_font_px']:g} | {r['text_collisions']} | {r['text_spills']} | {r['broken_words']} | {r['page_height_px']} | {'yes' if r['horizontal_overflow'] else 'no'} |"
        )
    for theme, c in colours.items():
        out += ["", f"### Contrast, {theme} theme (WCAG: text >= 4.5, graphics >= 3.0)", "", "| Pair | Ratio |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in {**c["text"], **c["graphics"]}.items()]
        out += [
            "",
            f"### State-colour separation under colour-vision deficiency, {theme} theme (CIEDE2000)",
            "",
            "| Simulation | crit-warn | crit-ok | warn-ok | lag ramp, min adjacent |",
            "|---|---|---|---|---|",
        ]
        out += [f"| {k} | {v['crit-warn']} | {v['crit-ok']} | {v['warn-ok']} | {v['ramp min adjacent']} |" for k, v in c["cvd_delta_e2000"].items()]
    return "\n".join(out) + "\n"


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        results = measure(page)
        shots = capture(browser)
        browser.close()
    colours = colour_report()
    (HERE / "evaluation.json").write_text(json.dumps({"measurements": results, "colour": colours, "screenshots": shots}, indent=1) + "\n")
    sys.stdout.write(tables(results, colours))


if __name__ == "__main__":
    main()
