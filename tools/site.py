#!/usr/bin/env python3
"""The whole OType site in one command: the stylesheet, the reference, Learn and the cookbook.

    python3 tools/site.py [OUT_DIR] [--watch]   (default out/site; the published site is ../otype-docs)

--watch keeps running and rebuilds on every save: for a live preview while editing.

OUT_DIR/style.css is tools/site.css, the one stylesheet every page links. roadmap.html is not built here: it is
kept by hand in otype-docs and links the same style.css.
"""
import glob
import os
import shutil
import sys
import time

import cheatsheet as cs
import cookbook
import learn


def build(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(cs.CSS_FILE, os.path.join(out_dir, "style.css"))
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(cs.build(False, css_href="style.css"))
    return ([os.path.join(out_dir, "style.css"), os.path.join(out_dir, "index.html")]
            + learn.build(os.path.join(out_dir, "learn")) + [cookbook.build(out_dir)])


def watch(out_dir):
    """Rebuild on every save, for a live preview: site.css alone is just copied, anything else rebuilds all."""
    sources = lambda: {p: os.path.getmtime(p) for p in [cs.CSS_FILE, cookbook.SRC] + glob.glob(os.path.join(learn.SRC, "*"))}
    seen = sources()
    print("watching tools/site.css, docs/learn/, docs/cookbook.md (Ctrl+C to stop)")
    while True:
        time.sleep(0.5)
        now = sources()
        changed = [p for p in now if now[p] != seen.get(p)]
        seen = now
        if changed == [cs.CSS_FILE]:
            shutil.copyfile(cs.CSS_FILE, os.path.join(out_dir, "style.css"))
        elif changed:
            try:
                build(out_dir)
            except SystemExit as e:                    # a listing that fails: say it, keep watching
                print(e)
                continue
        if changed:
            print(time.strftime("%H:%M:%S"), "rebuilt:", ", ".join(os.path.relpath(p, cs.ROOT) for p in changed))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--watch"]
    out = args[0] if args else os.path.join(cs.ROOT, "out", "site")
    for path in build(out):
        print(path)
    if "--watch" in sys.argv:
        watch(out)
