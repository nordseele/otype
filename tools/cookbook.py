#!/usr/bin/env python3
"""The cookbook as a page of the site, from docs/cookbook.md (which stays the source: the tests read its recipes).

    python3 tools/cookbook.py [OUT_DIR]      -> OUT_DIR/cookbook.html (default out/site)

Handles the Markdown the cookbook uses: headings, paragraphs, - and 1. lists, ``` fences, `code`, **bold**,
[links](…) and ---. Its "Contents" section becomes the grouped sidebar.
"""
import html
import os
import re
import sys

import cheatsheet as cs
import learn

SRC = os.path.join(cs.ROOT, "docs", "cookbook.md")
OUT = os.path.join(cs.ROOT, "out", "site")
REPO = "https://github.com/nordseele/otype/blob/main/"


def slug(text):
    """GitHub's heading anchors, so the cookbook's own #links keep working."""
    t = re.sub(r"[^\w\- ]", "", text.lower().replace("`", ""))
    return t.replace(" ", "-")


def inline(text):
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append("<code>" + html.escape(p[1:-1]) + "</code>")
            continue
        p = html.escape(p, quote=False)
        p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
        p = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: '<a href="%s">%s</a>' % (
            m.group(2) if m.group(2).startswith(("#", "http")) else REPO + os.path.normpath(
                os.path.join("docs", m.group(2))), m.group(1)), p)
        out.append(p)
    return cs.highlight_inline("".join(out))


def toc(contents):
    """The Contents section: **Group** lines, then numbered or bulleted links."""
    items = []
    for line in contents:
        g = re.fullmatch(r"\*\*(.+)\*\*", line.strip())
        m = re.match(r"\s*(?:(\d+)\.|-)\s+\[(.+)\]\((#[^)]+)\)", line)
        if g:
            items.append(f'<li class="grp">{html.escape(g.group(1))}</li>')
        elif m:
            num, title, href = m.groups()
            if not num:                                   # "- [I1. Build …]"
                num, _, title = title.partition(". ")
            items.append(f'<li><a href="{href}"><span>{html.escape(num)}</span>{inline(title)}</a></li>')
    return "".join(items)


def convert(md):
    lines = md.split("\n")
    body, contents, k = [], [], 0
    para, lst = [], None                                  # lst: ("ul" | "ol", [items])

    def flush():
        nonlocal para, lst
        if para:
            body.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []
        if lst:
            body.append(f"<{lst[0]}>" + "".join(f"<li>{inline(i)}</li>" for i in lst[1]) + f"</{lst[0]}>")
            lst = None

    while k < len(lines):
        line = lines[k]
        if line.startswith("## Contents"):               # becomes the sidebar
            flush()
            k += 1
            while k < len(lines) and lines[k].strip() != "---":
                contents.append(lines[k])
                k += 1
            k += 1
            continue
        fence = re.match(r"```(\w*)\s*$", line)
        if fence:
            flush()
            end = lines.index("```", k + 1)
            code = "\n".join(lines[k + 1:end])
            if fence.group(1) == "otype":
                body.append(f'<figure class="listing"><pre><code>{cs.highlight(code)}</code></pre></figure>')
            else:
                kind = "sh" if fence.group(1) == "sh" else "plain"
                body.append(f'<pre class="{kind}"><code>{html.escape(code)}</code></pre>')
            k = end + 1
            continue
        h = re.match(r"(#{1,3}) (.+)", line)
        if h:
            flush()
            n = len(h.group(1))
            body.append(f'<h{n} id="{slug(h.group(2))}">{inline(h.group(2))}</h{n}>' if n > 1
                        else f"<h1>{inline(h.group(2))}</h1>")
        elif line.strip() == "---":
            flush()
        elif not line.strip():
            flush()
        elif re.match(r"(\d+\.|-) ", line):
            kind = "ol" if line[0].isdigit() else "ul"
            if para or (lst and lst[0] != kind):
                flush()
            if not lst:
                lst = (kind, [])
            lst[1].append(re.sub(r"^(\d+\.|-) ", "", line))
        elif lst and line.startswith("  "):              # a list item's next line
            lst[1][-1] += " " + line.strip()
        else:
            if lst:
                flush()
            para.append(line.strip())
        k += 1
    flush()
    return "\n".join(body), toc(contents)


def build(out_dir):
    cs.OPNAMES = {o["name"].upper() for o in cs.otype.ops_json() if o["category"] not in ("KEYWORD", "VARIABLE")}
    body, side = convert(open(SRC, encoding="utf-8").read())
    first_p = re.search(r"<p>(.*?)</p>", body, re.S).group(1)
    desc = html.escape(re.sub(r"<[^>]+>", "", first_p)[:200], quote=True)
    page = learn.PAGE.format(
        title="OType cookbook", full_title="OType Cookbook", desc=desc, url="cookbook.html", up="",
        section="cookbook", toc_title="Recipes", version=cs.VERSION,
        checked="every recipe on this page is checked by the OType test suite",
        wordmark=cs.wordmark_svg(cs.WORDMARK), nav=cs.site_nav("cookbook"), steps=cs.STEPS, toc=side,
        kicker="cookbook", body=body, pager="", js=cs.SHARED_JS)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "cookbook.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else OUT))
