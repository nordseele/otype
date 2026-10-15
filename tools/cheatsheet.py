#!/usr/bin/env python3
"""OType command reference, generated from the operation registry.

    python3 tools/cheatsheet.py                 -> docs/cheatsheet.html (standalone page)
    python3 tools/cheatsheet.py --fragment OUT  -> page body only (no <html>/<head>), for an Artifact

Every entry comes from modules/otype/ops.py (through otype.ops_json()), the
examples from modules/otype/examples/TUTORIAL.OTY, which the checker must
accept: the page refuses to build otherwise.
"""
import html
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import otype  # noqa: E402
from modules.otype import ops as OPREG  # noqa: E402  (evidence levels; the JSON export has none)

# ops whose firmware side is not done yet get a faint marker after their name
MARKS = {
    "to establish": "to establish: machine support still needs hardware confirmation",
    "unknown": "unknown: how the firmware does this is not known yet",
    "tables to write": "tables to write: the data tables are not written yet",
}

EXAMPLE_LINE = """l 1 16: prob 35: tr.add i        # for i = 1..16, 35 % chance: a trig on step i
pl.set i amp.vol rrand 70 127    # = pl.set (i) (amp.vol) (rrand 70 127)
steps has.trig:                  # block: the steps that have a trig
  each: cond.set i 50%"""
TUTORIAL = os.path.join(ROOT, "modules", "otype", "examples", "TUTORIAL.OTY")

# (section title, [(category, sub-heading or None, filter)])
STRUCTURE = ("OTYPE", "DEF", "CONST", "MENU", "PAGE", "ON", "PARAM", "CONFIRM", "INPUT")
MODIFIERS = ("IF", "ELIF", "ELSE", "L", "EACH", "PROB", "EVERY", "SKIP", "DEL")
FLOW = ("BREAK", "STOP", "RET")
KWARGS = ("ALL", "CUR", "DEEP")

SECTIONS = [
    ("Structure", [("KEYWORD", None, STRUCTURE)]),
    ("Modifiers", [("KEYWORD", None, MODIFIERS)]),
    ("Flow", [("KEYWORD", None, FLOW), ("KEYWORD", "Keyword arguments", KWARGS)]),
    ("Variables", [("VARIABLE", None, None)]),
    ("Language", [("LANG", None, None)]),
    ("Context", [("CONTEXT", None, None)]),
    ("Pattern", [("PATTERN", None, None)]),
    ("Parts", [("PARTS", None, None)]),
    ("Trigs", [("TRIGS", None, None)]),
    ("Timing", [("TIMING", None, None)]),
    ("Track", [("TRACK", None, None)]),
    ("Pages", [
        ("PAGES", "Reading / writing parameters", ("PAR.GET", "PAR.SET", "PAR.MIN", "PAR.MAX")),
        ("PAGES", "Playback", ("PB", "PB.SETUP")),
        ("PAGES", "AMP", ("AMP", "AMP.ATK", "AMP.HOLD", "AMP.REL", "AMP.VOL", "AMP.BAL", "AMP.XVOL", "AMP.SETUP")),
        ("PAGES", "LFO", ("LFO", "LFO.SPD", "LFO.DEP", "LFO.DEST", "LFO.WAVE", "LFO.MULT", "LFO.TRIG")),
        ("PAGES", "LFO designer", ("LFOD.SET", "LFOD.GET", "LFOD.INTERP")),
        ("PAGES", "FX1 / FX2", ("FX1", "FX2", "FX.TYPE", "FX.COUNT", "FX1.SETUP", "FX2.SETUP")),
    ]),
    ("Locks", [("LOCKS", None, None)]),
    ("Sample locks", [("SMP", None, None)]),
    ("Scenes", [("SCENES", None, None)]),
    ("Files", [("FILES", None, None)]),
    ("Slots", [("SLOTS", None, None)]),
    ("Audio", [("AUDIO", None, None)]),
    ("Screen", [("UI", None, None)]),
]

CATEGORY_SUMMARIES = {
    "LANG": "Maths, random values, rhythm and script state between presses.",
    "CONTEXT": "Choose the tracks and steps a script acts on; control random seeds.",
    "PATTERN": "Current pattern/bank, pattern length, transitions and tempo.",
    "PARTS": "Choose, save and reload parts; keep each part's sound distinct.",
    "TRIGS": "Audio trigs, trigless locks/trigs, one-shots, swing and slide trigs.",
    "TIMING": "Conditions, microtiming, retriggers and FILL.",
    "TRACK": "Machine, assigned sample slot, LEVEL, CUE, project mute, swing and track init.",
    "PAGES": "Sound parameters and SETUP: Playback, AMP, LFO, Designer and FX.",
    "LOCKS": "Parameter locks on individual steps.",
    "SMP": "Sample-slot locks on individual steps.",
    "SCENES": "Set, read, copy and clear individual scenes and their locks.",
    "FILES": "List and filter the files of a card folder, then pick from the list.",
    "SLOTS": "Load, test, clear and swap Static and Flex slots; keep a working set of slot numbers. Bulk jobs are loops: see the cookbook.",
    "AUDIO": "Save audio takes, create sliced chains and normalize files.",
    "UI": "The text of the end box: msg replaces DONE with strings and numbers.",
}

LETTERS = [
    ("s", "step, 1..64"), ("t", "track, 1..8"), ("n", "object / parameter index or sample slot; bounds in each entry"),
    ("p", "parameter, 0..29 — written pb 1, lfo 3, amp.vol, fx1 2…"),
    ("v", "value, clamped to the parameter's range"),
    ("c", "trig condition: 50%, 1:4, fill, !pre… or a number"),
    ("k n r", "hits, length, rotation of a rhythm"),
    ("f", "file number in the current dir list"),
    ("x y z", "numbers (16-bit, saturating)"),
]

TOKEN = re.compile(r'(#.*$)|("[^"]*")|(\{[^}]*\})|(\b\d+%|\b[1-8]:[1-8]\b|!?\b(?:FILL|PRE|NEI|1ST)\b)'
                   r'|(-?\b0x[0-9A-Fa-f]+\b|-?\b\d+\b)|(\|)|(>>)|([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)+|\?)'
                   r'|(\b[A-Z][A-Z0-9_]*\b)', re.I)
NAMEWORD = re.compile(r"(?<![\w\"])!?[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*")


def lower_names(text):
    """Render every OType name inside running text in its canonical lowercase."""
    def f(m):
        w = m.group(0)
        u = w.upper().lstrip("!")
        if (u in OPNAMES or u in KEYWORDS or u in ("FILL", "PRE", "NEI", "1ST")) and len(u) > 1:
            return w.lower()
        return w
    return NAMEWORD.sub(f, text)
KEYWORDS = set(STRUCTURE + MODIFIERS + FLOW + KWARGS + ("TRK", "STEPS", "STEP", "SEED"))
VARIABLES = set("ABCDXYZTIJ")


def highlight(src):
    """Very small OType highlighter for the examples (same classes as the editor)."""
    out = []
    for line in src.splitlines():
        pos, buf = 0, []
        for m in TOKEN.finditer(line):
            buf.append(html.escape(line[pos:m.start()]))
            t = m.group(0)
            e = html.escape(t)
            g = m.lastindex
            if g == 1:
                cls = "c-com"
            elif g in (2, 3):
                cls = "c-str"
            elif g == 4:
                cls = "c-cond"
            elif g == 5:
                cls = "c-num"
            elif g in (6, 7):
                cls = "c-op"
            elif g == 8:
                cls = "c-api"
            else:
                u = t.upper()
                if u in KEYWORDS:
                    cls = "c-kw"
                elif u in VARIABLES or u in ("FLIP", "O", "DRUNK"):
                    cls = "c-var"
                elif u in OPNAMES:
                    cls = "c-api"
                else:
                    cls = "c-user"
            buf.append(f'<span class="{cls}">{e}</span>')
            pos = m.end()
        buf.append(html.escape(line[pos:]))
        out.append("".join(buf))
    return "\n".join(out)


def tutorial_blocks():
    prog = otype.check_files([TUTORIAL])
    if any(" error: " in str(d) for d in prog.sorted_diags()):
        raise SystemExit("TUTORIAL.OTY does not pass the checker")
    text = open(TUTORIAL).read()
    parts = re.split(r"^# --- ", text, flags=re.M)
    blocks = []
    for p in parts:
        if not p.strip():
            continue
        title, _, body = p.partition("\n")
        blocks.append((title.strip(), body.strip("\n")))
    return blocks


def entry_html(o):
    signature = o["signature"]
    if o.get("settable"):
        signature += " / " + o["signature"] + " x"
    sig = html.escape(signature.lower())
    name = html.escape(o["name"].lower())
    reg = OPREG.OPS.get(o["name"].upper())
    mark = ""
    if reg is not None and o["category"] not in ("KEYWORD", "VARIABLE") and reg.level in MARKS:
        mark = f'<span class="todo" title="{html.escape(MARKS[reg.level])}">\u25cc</span>'
    sig = sig.replace(name, f'<b>{name}</b>{mark}', 1)
    alias = " ".join(html.escape(a.lower()) for a in o.get("alias") or [])
    chips = []
    if o.get("destructive"):
        chips.append('<span class="chip warn" title="asks YES/NO before running">destructive</span>')
    if o.get("stmt_only") and o["category"] not in ("KEYWORD",):
        chips.append('<span class="chip" title="a statement of its own, not a value">statement</span>')
    if o.get("predicate"):
        chips.append('<span class="chip" title="usable after steps">predicate</span>')
    search = html.escape(f'{o["name"]} {" ".join(o.get("alias") or [])} {o["doc"]}'.lower())
    return (f'<div class="op" data-s="{search}"><div class="sig"><code>{sig}</code>'
            f'{f"<span class=alias>{alias}</span>" if alias else ""}</div>'
            f'<div class="doc">{html.escape(lower_names(o["doc"]))}{" " + " ".join(chips) if chips else ""}</div></div>')


def build(fragment):
    ops = otype.ops_json()
    global OPNAMES
    OPNAMES = {o["name"].upper() for o in ops if o["category"] not in ("KEYWORD", "VARIABLE")}
    by_cat = {}
    seen_kw = set()
    for o in ops:
        if o["category"] == "KEYWORD":
            if o["name"] in seen_kw or o["name"].upper() in OPNAMES:
                continue
            seen_kw.add(o["name"])
        by_cat.setdefault(o["category"], []).append(o)
    n_ops = len(OPNAMES)

    sections = []
    used = set()
    for title, groups in SECTIONS:
        inner = []
        for cat, sub, names in groups:
            items = by_cat.get(cat, [])
            if names is not None:
                order = {n: i for i, n in enumerate(names)}
                items = sorted((o for o in items if o["name"].upper() in order), key=lambda o: order[o["name"].upper()])
            for o in items:
                used.add((o["category"], o["name"]))
            if not items:
                continue
            if sub:
                inner.append(f'<h3>{html.escape(sub)}</h3>')
            inner.extend(entry_html(o) for o in items)
        if inner:
            sid = re.sub(r"[^a-z]+", "-", title.lower()).strip("-")
            sections.append(f'<section class="cat" id="{sid}"><h2><span class="hb">{len(sections) + 1:02d} {html.escape(title)}</span></h2>{"".join(inner)}</section>')
    missing = [f'{o["category"]}:{o["name"]}' for o in ops
               if (o["category"], o["name"]) not in used
               and not (o["category"] == "KEYWORD" and o["name"].upper() in OPNAMES)]
    if missing:
        raise SystemExit("not placed in any section: " + ", ".join(missing))

    letters = "".join(f'<div><code>{html.escape(a)}</code><span>{html.escape(b)}</span></div>' for a, b in LETTERS)
    examples = "".join(
        f'<figure class="ex"><figcaption>{html.escape(t)}</figcaption><pre><code>{highlight(b)}</code></pre></figure>'
        for t, b in tutorial_blocks())
    nav = "".join(f'<a href="#{re.sub(r"[^a-z]+", "-", t.lower()).strip("-")}">{html.escape(t)}</a>'
                  for t, g in SECTIONS)
    overview = []
    for title, groups in SECTIONS:
        cat = groups[0][0]
        if cat not in CATEGORY_SUMMARIES:
            continue
        sid = re.sub(r"[^a-z]+", "-", title.lower()).strip("-")
        overview.append(f'<tr><th scope="row"><a href="#{sid}">{html.escape(title)}</a></th>'
                        f'<td>{len(by_cat[cat])}</td><td>{html.escape(CATEGORY_SUMMARIES[cat])}</td></tr>')

    body = PAGE.format(css=open(CSS_FILE, encoding="utf-8").read(), js=JS, wordmark=wordmark_svg(WORDMARK), steps=STEPS, n_ops=n_ops, example_line=highlight(EXAMPLE_LINE), letters=letters, examples=examples,
                       sections="".join(sections), nav=nav, overview="".join(overview), n_cats=len(overview))
    if fragment:
        return body
    head, body = body.split('<div class="wrap">', 1)          # title, fonts and style go in <head>
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            + OG_META + head +
            '</head>\n<body>\n<div class="wrap">' + body + '\n</body>\n</html>\n')


OG_META = '<meta name="description" content="Every command of OType, simple yet powerful scripting for the Octatrack: what it does, its arguments, examples.">\n<meta name="theme-color" content="#1a1a1a">\n<meta property="og:type" content="website">\n<meta property="og:site_name" content="otype">\n<meta property="og:title" content="OType Command Reference">\n<meta property="og:description" content="Every command of OType, simple yet powerful scripting for the Octatrack: what it does, its arguments, examples.">\n<meta property="og:url" content="https://nordseele.github.io/otype-docs/">\n<meta property="og:image" content="https://nordseele.github.io/otype-docs/og.png">\n<meta property="og:image:width" content="1200">\n<meta property="og:image:height" content="630">\n<meta property="og:image:alt" content="otype: simple yet powerful scripting for the Octatrack">\n<meta name="twitter:card" content="summary_large_image">\n'   # link preview (tools/og_image.py -> otype-docs/og.png)

CSS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cheatsheet.css")

WORDMARK = """       █
▄▀▀▀▄ ▀█▀▀  █   █ █▀▀▀▄ ▄▀▀▀▄
█   █  █    █   █ █   █ █▀▀▀▀
▀▄▄▄▀  ▀▄▄▀ ▀▄▄▄█ █▄▄▄▀ ▀▄▄▄▀
             ▄▄▄▀ █"""


def wordmark_svg(text):
    """The half-block wordmark as an SVG of square pixels: one text column = one pixel wide,
    one text row = two pixels tall.  Fonts render block characters differently (Safari
    shifted them), an SVG does not."""
    rows = text.split("\n")
    w = max(len(r) for r in rows)
    px = []                                    # pixel rows, top to bottom
    for r in rows:
        r = r.ljust(w)
        px.append([c in "█▀" for c in r])
        px.append([c in "█▄" for c in r])
    rects = []
    for y, line in enumerate(px):              # one rect per horizontal run
        x = 0
        while x < w:
            if line[x]:
                x0 = x
                while x < w and line[x]:
                    x += 1
                rects.append('<rect x="%d" y="%d" width="%d" height="1"/>' % (x0, y, x - x0))
            else:
                x += 1
    return ('<svg class="mark" viewBox="0 0 %d %d" aria-hidden="true" shape-rendering="crispEdges" '
            'fill="currentColor">%s</svg>' % (w, len(px), "".join(rects)))


STEPS = "".join('<i class="%s"></i>' % ("f on" if i == 0 else "f" if i % 4 == 0 else "") .strip() for i in range(16))


JS = r"""
(function () {
  var root = document.documentElement, btn = document.getElementById('theme');
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  function current() { return root.getAttribute('data-theme') || (mq.matches ? 'dark' : 'light'); }
  function label() { var t = current(); btn.textContent = t === 'dark' ? 'light' : 'dark';
    btn.setAttribute('aria-label', t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'); }
  var saved = null; try { saved = localStorage.getItem('otype-theme'); } catch (e) {}
  if (saved === 'dark' || saved === 'light') root.setAttribute('data-theme', saved);
  label();
  if (mq.addEventListener) mq.addEventListener('change', label);
  btn.addEventListener('click', function () {
    var t = current() === 'dark' ? 'light' : 'dark'; root.setAttribute('data-theme', t); label();
    try { localStorage.setItem('otype-theme', t); } catch (e) {}
  });
  var steps = Array.prototype.slice.call(document.querySelectorAll('.steps i')), at = 0;
  var tempo = parseFloat(getComputedStyle(root).getPropertyValue('--tempo')) * 1000 || 500;
  if (steps.length && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    setInterval(function () { steps[at].classList.remove('on'); at = (at + 1) % steps.length; steps[at].classList.add('on'); }, tempo);
  }
  var links = Array.prototype.slice.call(document.querySelectorAll('nav.jump a'));
  function here() { links.forEach(function (a) { if (a.getAttribute('href') === location.hash) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current'); }); }
  window.addEventListener('hashchange', here); here();
  var tools = document.querySelector('.tools');
  function pad() { root.style.setProperty('--tools-h', (getComputedStyle(tools).position === 'sticky' ? tools.offsetHeight + 16 : 16) + 'px'); }
  window.addEventListener('resize', pad); pad();
  var q = document.getElementById('q');
  var ops = Array.prototype.slice.call(document.querySelectorAll('.op'));
  var cats = Array.prototype.slice.call(document.querySelectorAll('.cat'));
  var none = document.getElementById('none');
  function run() {
    var t = q.value.trim().toLowerCase(), shown = 0;
    ops.forEach(function (el) {
      var ok = !t || el.getAttribute('data-s').indexOf(t) >= 0;
      el.hidden = !ok; el.classList.toggle('hit', !!t && ok); if (ok) shown++;
    });
    cats.forEach(function (c) { c.hidden = !c.querySelector('.op:not([hidden])'); });
    none.hidden = shown > 0;
    var sheet = document.querySelector('.sheet');  // results sit below the intro: bring them into view
    if (t && sheet.getBoundingClientRect().top > innerHeight / 2) none.hidden ? sheet.scrollIntoView() : none.scrollIntoView();
  }
  q.addEventListener('input', run);
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement !== q) { e.preventDefault(); q.focus(); }
    if (e.key === 'Escape' && document.activeElement === q) { q.value = ''; run(); }
  });
})();
"""

PAGE = """<title>OType Command Reference</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:ital,wght@0,300;0,400;0,600;1,400&display=swap">
<style>{css}</style>
<div class="wrap">
<div class="bar"><span class="cell first">OTYPE</span><span class="cell">v1</span><span class="cell">{n_ops} ops</span><span class="cell end">octatrack</span><button id="theme" class="cell" type="button">dark</button></div>
<header class="top">
  <h1 aria-label="otype">{wordmark}</h1>
  <p class="tag">simple yet powerful scripting for the octatrack · <a href="roadmap.html">roadmap</a> · <a href="https://github.com/nordseele/otype">github</a></p>
  <div class="steps" aria-hidden="true">{steps}</div>
</header>
<div class="overview">
  <h2><span class="hb">v1 · {n_cats} operator categories</span></h2>
  <p>{n_ops} operators, grouped by what you want to change. Syntax keywords and variables are listed separately. The machine profile includes every operator.</p>
  <table><thead><tr><th scope="col">Category</th><th scope="col">Ops</th><th scope="col">Use</th></tr></thead><tbody>{overview}</tbody></table>
</div>
<div class="tools">
  <input id="q" type="search" placeholder="search: tr. random slot…  ( / )" aria-label="Search commands">
  <nav class="jump" aria-label="Sections">{nav}</nav>
</div>
<div class="intro">
  <div class="howto">
    <h2><span class="hb">Reading a line</span></h2>
    <p>One command per line, in prefix notation: an operation takes the values that follow it, which may be operations themselves. Modifiers before <code>:</code> repeat or condition the rest of the line; a line ending with <code>:</code> opens an indented block.</p>
<pre><code>{example_line}</code></pre>
    <p>Dotted names (<code>tr.add</code>) are the OT's API; your own functions use underscores and take their arguments after <code>|</code>. OType ignores case; this reference writes everything in lowercase. Text between quotes keeps its case.</p>
    <p>Read a setting with <code>x tempo</code>; write it with <code>tempo 137</code>. Numbered settings keep their number: <code>x lfo.wave 1</code> / <code>lfo.wave 1 18</code>. Renamed operators have no compatibility aliases.</p>
    <p>Names such as <code>amp.vol</code> and <code>lfo.spd 1</code> identify a parameter. Read/write its value with <code>par.get</code>/<code>par.set</code>, or use <code>pl.*</code> for a step lock. SETUP operations such as <code>lfo.mult 1 3</code> write the setting directly.</p>
  </div>
  <div>
    <h2><span class="hb">Argument letters</span></h2>
    <div class="letters">{letters}</div>
  </div>
</div>
<div class="examples">{examples}</div>
<p id="none" class="empty" hidden>No command matches this search.</p>
<main class="sheet">{sections}</main>
<footer>
  <p>otype — simple yet powerful scripting for the octatrack</p>
  <p><span class="todo" aria-hidden="true">\u25cc</span> see the operation's status tooltip · v1 runs on the Octatrack MKI; the audio operators are experimental · generated from the operation registry; the examples pass the OType checker</p>
  <p>syntax directly inspired by <a href="https://monome.org/docs/teletype/">monome teletype</a> · not affiliated with, endorsed or supported by elektron or monome</p>
  <p>elektron and octatrack are trademarks of elektron music machines; monome and teletype belong to monome. the names are used only to say what this works with.</p>
  <p>no elektron firmware, os image, disassembly or code is distributed here.</p>
  <p>provided as is, without any warranty (gpl-3.0 §15–16). changing your instrument's firmware is at your own risk; keep the official os at hand to restore it.</p>
  <p><a href="https://github.com/nordseele/ot1">ot1</a> · <a href="https://www.gnu.org/licenses/gpl-3.0.html">gpl-3.0</a> · © nordseele 2026</p>
</footer>
</div>
<script>{js}</script>"""


def main(argv):
    frag = "--fragment" in argv
    args = [a for a in argv if not a.startswith("--")]
    out = args[0] if args else os.path.join(ROOT, "docs", "cheatsheet.html")
    page = build(frag)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(out)


if __name__ == "__main__":
    main(sys.argv[1:])
