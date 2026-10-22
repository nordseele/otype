#!/usr/bin/env python3
"""Learn OType: the studies, one page per chapter, from docs/learn/*.html.

    python3 tools/learn.py [OUT_DIR]      -> OUT_DIR/index.html, OUT_DIR/quickstart.html, ... (default out/site/learn)

Each docs/learn/NN-slug.html is a page body (its <h1> is the title). Listings:
    <pre class="oty" data-caption="..." data-run="FUNC" data-runargs="-v">code</pre>
are checked by the OType checker (a listing that does not start with def/const/otype is wrapped in a function)
and, with data-run, run by the simulator: its output is printed under the listing. The pages refuse to build
when a listing fails. <pre class="sh"> is a shell command, <pre class="plain"> plain text.
"""
import glob
import html
import os
import re
import subprocess
import sys
import tempfile

import cheatsheet as cs   # tools/ is on sys.path when run as a script; cheatsheet puts the repo root there

SRC = os.path.join(cs.ROOT, "docs", "learn")
OUT = os.path.join(cs.ROOT, "out", "site", "learn")
OTYPE = os.path.join(cs.ROOT, "tools", "otype.py")

PRE = re.compile(r'<pre class="(oty|sh|plain)"([^>]*)>(.*?)</pre>', re.S)
ATTR = re.compile(r'data-([a-z]+)="([^"]*)"')


def chapters():
    out = []
    for path in sorted(glob.glob(os.path.join(SRC, "[0-9][0-9]-*.html"))):
        slug = re.sub(r"^\d\d-", "", os.path.basename(path)[:-5])
        body = open(path, encoding="utf-8").read()
        title = re.search(r"<h1>(.*?)</h1>", body).group(1)
        out.append(("index" if slug == "index" else slug, title, body))
    return out


def check_listing(code, where):
    """Check one listing (alone, like a file of its own); return the path of the file, for `run`."""
    first = code.lstrip().split(None, 1)[0].lower()
    text = code if first in ("def", "const", "otype") else "def learn_listing\n" + "".join(
        "  " + line + "\n" for line in code.splitlines())
    fd, path = tempfile.mkstemp(suffix=".OTY")
    with os.fdopen(fd, "w") as f:
        f.write(text + "\n")
    errors = [str(d) for d in otype_check(path) if " error: " in str(d)]
    if errors:
        raise SystemExit(f"{where}: the listing does not pass the checker:\n  " + "\n  ".join(errors))
    return path


def otype_check(path):
    return cs.otype.check_files([path]).sorted_diags()


def run_listing(path, func, args, where):
    seed = [] if "--seed" in args else ["--seed", "1"]          # the same page at every build
    r = subprocess.run([sys.executable, OTYPE, "run", func, path] + args.split() + seed, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{where}: `run {func}` failed:\n{r.stdout}{r.stderr}")
    lines = r.stdout.rstrip("\n").split("\n")
    keep = []                                                  # -v prints all 8 tracks: drop the empty ones
    for k, line in enumerate(lines):
        empty = re.fullmatch(r"\s+\.+", lines[k + 1]) if k + 1 < len(lines) else None
        if re.match(r"T\d ", line) and empty or re.fullmatch(r"\s+\.+", line) and re.match(r"T\d ", lines[k - 1]):
            continue
        keep.append(line)
    return "\n".join(keep)


def render_body(chapter_no, slug, body):
    n = [0]

    def sub(m):
        kind, attrs, code = m.group(1), dict(ATTR.findall(m.group(2))), m.group(3).strip("\n")
        if kind == "sh":
            return f'<pre class="sh"><code>{html.escape(code)}</code></pre>'
        if kind == "plain":
            return f'<pre class="plain"><code>{html.escape(code)}</code></pre>'
        n[0] += 1
        label = f"Listing {chapter_no}-{n[0]}" if chapter_no else "Example"
        where = f"docs/learn/{slug}: {label}"
        path = check_listing(code, where)
        try:
            out = ""
            if "run" in attrs:
                args = attrs.get("runargs", "")
                text = run_listing(path, attrs["run"], args, where)
                cmd = " ".join(["otype.py run", attrs["run"]] + ([args] if args else []))
                out = (f'<details class="out"><summary><b>Output</b> · what the simulator prints for '
                       f'<code>$ {html.escape(cmd)}</code></summary>'
                       f'<pre><code>{html.escape(text)}</code></pre></details>')
        finally:
            os.unlink(path)
        cap = html.escape(attrs.get("caption", ""))
        return (f'<figure class="listing"><figcaption><b>{label}</b>{" · " + cap if cap else ""}</figcaption>'
                f'<pre><code>{cs.highlight(code)}</code></pre>{out}</figure>')

    return PRE.sub(sub, cs.highlight_inline(body))


LEARN_CSS = """
.listing > pre { background: var(--code-bg); color: var(--code-fg); padding: 12px 16px; }
article p code, article li code, article td code, article dt code { background: var(--code-bg); color: var(--code-fg); padding: 1px 5px; }
.learn { display: grid; grid-template-columns: 15rem minmax(0, 1fr); gap: 32px 64px; margin-top: 36px; align-items: start; }
.toc { position: sticky; top: 16px; font-size: 13px; }
.toc summary { font-size: var(--fs-small); letter-spacing: var(--track); text-transform: uppercase; color: var(--muted); cursor: pointer; margin-bottom: 10px; }
.toc ol { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--rule); }
.toc li { border-bottom: 1px solid var(--rule); }
.toc a { display: grid; grid-template-columns: 2.2em 1fr; padding: 6px 4px; text-decoration: none; }
.toc a span { color: var(--faint); }
.toc a[aria-current] { background: var(--strong); color: var(--bg); font-weight: 600; }
.toc a[aria-current] span { color: var(--bg); }
article { min-width: 0; max-width: 76ch; font-weight: 400; }
article h1 { font-size: 24px; line-height: 1.3; font-weight: 700; color: var(--strong); margin: 0 0 6px; }
article .kicker { font-weight: 500; font-size: var(--fs-small); letter-spacing: var(--track); text-transform: uppercase; color: var(--muted); margin: 0 0 18px; }
article h2 { display: block; margin: 48px 0 14px; padding-bottom: 6px; border-bottom: 1px solid var(--rule);
  font-size: 18px; line-height: 1.35; font-weight: 600; letter-spacing: 0; text-transform: none; color: var(--strong); }
article h2::after { content: none; }
article h2 code { font-weight: 600; background: none; border: 0; padding: 0; color: inherit; }
article p, article li { max-width: 72ch; }
article p { margin: 0 0 14px; }
article ul, article ol { padding-left: 1.3em; margin: 0 0 16px; }
article li { margin-bottom: 6px; }
article code { font-weight: 400; color: var(--code-fg); }
article pre code { font-weight: 400; color: inherit; }
article table { width: 100%; border-collapse: collapse; font-size: var(--fs-code); margin: 6px 0 20px; }
article th, article td { text-align: left; vertical-align: top; padding: 6px 12px 6px 0; border-bottom: 1px solid var(--rule); }
article thead { color: var(--muted); }
article th { font-weight: 400; }
.listing { margin: 20px 0 26px; }
.listing figcaption { font-style: italic; font-size: var(--fs-small); letter-spacing: var(--track); text-transform: uppercase; color: var(--muted); margin-bottom: 10px; }
.listing figcaption b { color: var(--strong); font-weight: 600; font-style: normal; }
.out { border: 1px solid var(--rule); margin-top: 12px; }
.out summary { cursor: pointer; padding: 6px 16px; font-size: var(--fs-small); letter-spacing: var(--track); color: var(--muted); }
.out summary:hover { color: var(--strong); }
.out[open] summary { border-bottom: 1px dashed var(--rule); }
.out pre { padding: 8px 16px 12px; }
.out summary b { color: var(--strong); font-weight: 600; text-transform: uppercase; }
.out pre { color: var(--text); }
pre.sh, pre.plain { margin: 12px 0 18px; padding: 10px 12px; border: 1px solid var(--rule); }
pre.sh code::before { content: "$ "; color: var(--faint); }
.recap, .try { margin: 40px 0 0; }
.recap dl { display: grid; grid-template-columns: minmax(8em, max-content) 1fr; gap: 0; margin: 0; font-size: 13px; }
.recap dt, .recap dd { margin: 0; padding: 6px 12px 6px 0; border-bottom: 1px solid var(--rule); }
.recap dd { color: var(--muted); font-style: italic; font-weight: 300; }
.try li { color: var(--text); }
.pager { display: flex; justify-content: space-between; gap: 16px; margin: 56px 0 0; padding-top: 16px; border-top: 1px solid var(--rule); font-size: 13px; }
.pager a { text-decoration: none; padding: 6px 10px; border: 1px solid var(--rule); }
.pager small { display: block; font-size: var(--fs-small); color: var(--muted); letter-spacing: var(--track); text-transform: uppercase; }
.pager a:hover small { color: var(--bg); }
.pager .next { margin-left: auto; text-align: right; }
.next-step { margin-top: 32px; }
@media (max-width: 860px) {
  .learn { grid-template-columns: minmax(0, 1fr); }
  .toc { position: static; }
}
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="{desc}">
<meta name="theme-color" content="#1a1a1a">
<meta property="og:type" content="article">
<meta property="og:site_name" content="otype">
<meta property="og:title" content="{full_title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="https://nordseele.github.io/otype-docs/learn/{href}">
<meta property="og:image" content="https://nordseele.github.io/otype-docs/og.png">
<meta name="twitter:card" content="summary_large_image">
<title>{full_title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:ital,wght@0,300;0,400;0,500;0,600;0,700;1,300;1,400;1,500&display=swap">
<style>{css}</style>
</head>
<body>
<div class="wrap">
<div class="bar"><span class="cell first">OTYPE</span><span class="cell">learn</span><span class="cell">{version}</span><span class="cell end">octatrack</span><button id="theme" class="cell" type="button">dark</button></div>
<header class="top">
  <h1 aria-label="otype"><a href="../" style="background:none">{wordmark}</a></h1>
  <p class="tag">scripting for the octatrack</p>
  {nav}
  <div class="steps" aria-hidden="true">{steps}</div>
</header>
<div class="learn">
<details class="toc" open><summary>The studies</summary><ol>{toc}</ol></details>
<article>
<p class="kicker">{kicker}</p>
{body}
<nav class="pager" aria-label="Chapters">{pager}</nav>
</article>
</div>
<footer>
  <p>otype — scripting for the octatrack · every listing on this page passes the OType checker, and the outputs are printed by its simulator</p>
  <p>syntax directly inspired by <a href="https://monome.org/docs/teletype/">monome teletype</a> · not affiliated with, endorsed or supported by elektron or monome</p>
  <p>elektron and octatrack are trademarks of elektron music machines; monome and teletype belong to monome. the names are used only to say what this works with.</p>
  <p><a href="../">command reference</a> · <a href="https://github.com/nordseele/otype">otype on github</a> · <a href="https://www.gnu.org/licenses/gpl-3.0.html">gpl-3.0</a> · © nordseele 2026</p>
</footer>
</div>
<script>{js}
if (matchMedia('(max-width: 860px)').matches) document.querySelector('.toc').removeAttribute('open');</script>
</body>
</html>
"""


def build(out_dir):
    cs.OPNAMES = {o["name"].upper() for o in cs.otype.ops_json() if o["category"] not in ("KEYWORD", "VARIABLE")}
    chs = chapters()
    css = open(cs.CSS_FILE, encoding="utf-8").read() + LEARN_CSS
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for k, (slug, title, body) in enumerate(chs):
        href = "" if slug == "index" else slug + ".html"
        toc = "".join(
            f'<li><a href="{"./" if s == "index" else s + ".html"}"{" aria-current=page" if s == slug else ""}>'
            f'<span>{"" if s == "index" else f"{j:02d}"}</span>{t}</a></li>'
            for j, (s, t, _) in enumerate(chs))
        pager = ""
        if k > 0:
            ps, pt, _ = chs[k - 1]
            pager += f'<a class="prev" href="{"./" if ps == "index" else ps + ".html"}"><small>previous</small>← {pt}</a>'
        if k + 1 < len(chs):
            ns, nt, _ = chs[k + 1]
            pager += f'<a class="next" href="{ns}.html"><small>next</small>{nt} →</a>'
        first_p = re.search(r"<p>(.*?)</p>", body, re.S).group(1)
        desc = html.escape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(first_p))).strip()[:200], quote=True)
        page = PAGE.format(
            title=re.sub(r"<[^>]+>", "", title), full_title=title if slug == "index" else title + " · Learn OType", desc=desc, href=href, css=css, version=cs.VERSION,
            wordmark=cs.wordmark_svg(cs.WORDMARK), nav=cs.site_nav("learn", "../"), steps=cs.STEPS, toc=toc,
            kicker="learn OType" if slug == "index" else "appendix" if slug == "scope" else f"study {k:02d} of {len(chs) - 2}",
            body=render_body(k, slug, body), pager=pager, js=cs.SHARED_JS)
        path = os.path.join(out_dir, "index.html" if slug == "index" else slug + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        written.append(path)
    return written


def main(argv):
    for path in build(argv[0] if argv else OUT):
        print(path)


if __name__ == "__main__":
    main(sys.argv[1:])
