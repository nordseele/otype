"""Differential corpus of the OType compiler.

    python3 -m modules.otype.corpus --out out/otype/comp/corpus          # generate (deterministic)
    python3 -m modules.otype.corpus --out DIR --fast                    # no run of the test suites (fewer seeds)
    python3 -m modules.otype.corpus --read DIR/corpus.bin.gz            # statistics of an existing corpus

Writes DIR/corpus.bin.gz (the entries), DIR/stats.txt and DIR/coverage.txt (which distinct diagnostic
messages of tools/otype.py and modules/otype/otb.py the corpus reaches).  Same command + same sources
= same bytes (the random generator is seeded; nothing depends on dictionary or set order, nor on time).

An entry is what a C harness feeds to the compiler and compares with:

    Entry(id, profile, sources, report, otb)
        id        str   one per input, shared by its two profile variants ("ex/M1.OTY", "mut/0042/del_line", ...)
        profile   None or "m1"
        sources   [(label bytes, content bytes)]  in card order, labels like b"/SCRIPTS/HATS.OTY"
        report    str   what oterr.reference gives (= /SCRIPTS/ERRORS.TXT), Latin-1 text, lines end with "\\n"
        otb       bytes the table, or b"" when there is none

    for e in corpus.iter_entries("out/otype/comp/corpus/corpus.bin.gz"): ...

File format (gzip, mtime 0): b"OTCORP1\\n", then per entry, all integers big-endian:
    u16 len(id) id | u8 len(profile) profile ("" = None) | u16 nsources | per source: u16 len(label) label
    u32 len(content) content | u32 len(report) report (Latin-1) | u32 len(otb) otb ; a record with id
    length 0 ends the file.

Seeds: every .OTY of examples/ and sketches/ (alone and together), every OType source string of
tools/test_otype*.py (found with `ast`) and, unless --fast, every input the three Python suites hand to
`otype.check_sources` while they run (collected by running them); then seeded mutations of all of them and
hand-made families (nesting, call chains, caps, table size, non-ASCII bytes, boundary numbers ...).
Every input is run with profile m1 AND with no profile.
"""
import argparse
import ast
import collections
import glob
import gzip
import hashlib
import os
import random
import re
import struct
import sys
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from modules.otype import oterr  # noqa: E402
import otype  # noqa: E402  (oterr put tools/ on the path)

SEED = 20261003
PROFILES = (None, "m1")
MAGIC = b"OTCORP1\n"
MUTANTS_PER_SEED = 4
SCRIPTS = b"/SCRIPTS/"

Entry = collections.namedtuple("Entry", "id profile sources report otb")


# ============================================================================ the file format
def _w(buf, fmt, *a):
    buf += struct.pack(">" + fmt, *a)


def write_corpus(path, entries):
    """entries: iterable of Entry.  Byte-identical for identical entries (gzip mtime 0)."""
    buf = bytearray(MAGIC)
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
            def flush():
                gz.write(bytes(buf))
                del buf[:]
            for e in entries:
                eid = e.id.encode("ascii")
                prof = (e.profile or "").encode("ascii")
                _w(buf, "H", len(eid))
                buf += eid
                _w(buf, "B", len(prof))
                buf += prof
                _w(buf, "H", len(e.sources))
                for label, data in e.sources:
                    _w(buf, "H", len(label))
                    buf += label
                    _w(buf, "I", len(data))
                    buf += data
                rep = e.report.encode("latin-1")
                _w(buf, "I", len(rep))
                buf += rep
                _w(buf, "I", len(e.otb))
                buf += e.otb
                if len(buf) > 1 << 20:
                    flush()
            _w(buf, "H", 0)
            flush()


def iter_entries(path):
    """Yield the Entry records of a corpus file (the API of the C harness)."""
    with gzip.open(path, "rb") as fh:
        data = fh.read()
    if data[:len(MAGIC)] != MAGIC:
        raise ValueError("not an OType corpus: %s" % path)
    p = len(MAGIC)

    def take(fmt):
        nonlocal p
        v = struct.unpack_from(">" + fmt, data, p)[0]
        p += struct.calcsize(">" + fmt)
        return v

    def blob(n):
        nonlocal p
        b = data[p:p + n]
        p += n
        return b

    while True:
        n = take("H")
        if n == 0:
            return
        eid = blob(n).decode("ascii")
        prof = blob(take("B")).decode("ascii") or None
        srcs = []
        for _ in range(take("H")):
            label = blob(take("H"))
            srcs.append((label, blob(take("I"))))
        report = blob(take("I")).decode("latin-1")
        otb = blob(take("I"))
        yield Entry(eid, prof, srcs, report, otb)


def has_error(report):
    return " error: " in report


# ============================================================================ the seeds
def label_of(name):
    return SCRIPTS + os.path.basename(name).upper().encode("latin-1", "replace")


def seeds_files():
    """Examples and sketches, each alone, then together."""
    out, per = [], {}
    for sub in ("examples", "sketches"):
        files = sorted(glob.glob(os.path.join(ROOT, "modules", "otype", sub, "*")),
                       key=lambda p: os.path.basename(p).upper())
        srcs = []
        for f in files:
            if f.upper().endswith(".OTY"):
                with open(f, "rb") as fh:
                    srcs.append((label_of(f), fh.read()))
        per[sub] = srcs
        for s in srcs:
            out.append(("%s/%s" % (sub[:2], s[0][len(SCRIPTS):].decode()), [s]))
    for sub, srcs in per.items():
        out.append(("%s/ALL" % sub[:2], list(srcs)))
    out.append(("all/ALL", per["examples"] + per["sketches"]))
    return out


def _enc(text):
    try:
        return text.encode("latin-1")
    except UnicodeEncodeError:
        return text.encode("utf-8")


def seeds_ast():
    """Every string constant of tools/test_otype*.py that looks like OType source (found with `ast`)."""
    out = []
    pat = re.compile(r"(?m)^[ \t]*(def|const|otype)[ \t]+\S")
    for path in sorted(glob.glob(os.path.join(ROOT, "tools", "test_otype*.py"))):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        nodes = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)),
                       key=lambda n: (n.lineno, n.col_offset))
        for n in nodes:
            v = n.value
            if len(v) > 20000 or not pat.search(v):
                continue
            lines = [l for l in v.split("\n") if l.strip()]
            if lines and all(l[0] in " \t" for l in lines):
                v = textwrap.dedent(v)
            out.append(("ast/%s:%d" % (os.path.basename(path)[5:-3], n.lineno), [(b"/SCRIPTS/T.OTY", _enc(v))]))
    return out


def seeds_runtime():
    """Run the three Python suites and keep every list of sources given to `otype.check_sources`."""
    calls = []
    orig = otype.check_sources

    def wrap(sources):
        calls.append([(l, t) for l, t in sources])
        return orig(sources)

    sys.path.insert(0, os.path.join(ROOT, "tools"))
    otype.check_sources = wrap
    cwd = os.getcwd()
    try:
        os.chdir(ROOT)
        suite = unittest.TestSuite()
        for m in ("test_otype", "test_otype_run", "test_otype_otb"):
            suite.addTests(unittest.TestLoader().loadTestsFromName(m))
        with open(os.devnull, "w") as dn:
            r = unittest.TextTestRunner(stream=dn).run(suite)
        if r.failures or r.errors:
            raise RuntimeError("the Python suites fail: no corpus from a red tree")
    finally:
        otype.check_sources = orig
        os.chdir(cwd)
    out, seen = [], set()
    for c in calls:
        srcs = [(SCRIPTS + os.path.basename(l).upper().encode("latin-1", "replace"), _enc(t)) for l, t in c]
        key = tuple(srcs)
        if key in seen:
            continue
        seen.add(key)
        out.append(("run/%04d" % len(out), srcs))
    return out


def dedupe(seeds):
    seen, out = set(), []
    for name, srcs in seeds:
        key = tuple(srcs)
        if key not in seen:
            seen.add(key)
            out.append((name, srcs))
    return out


# ============================================================================ mutations (bytes -> bytes)
SPLIT = re.compile(rb"\r\n|\r|\n")
BOUNDS = [b"0", b"-1", b"1", b"32767", b"32768", b"-32768", b"-32769", b"65535", b"65536", b"0x7FFF", b"0X8000",
          b"0xFFFF", b"0x10000", b"-0", b"007", b"99999999999", b"0x0", b"-0x8000", b"-0x8001", b"0x",
          b"1" * 40, b"0" * 40 + b"5", b"1" * 5000]
NONASCII = [0x80, 0x85, 0xA0, 0xAD, 0xB2, 0xB5, 0xC0, 0xDF, 0xE9, 0xFF, 0x0B, 0x0C, 0x1C, 0x1F, 0x00, 0x01, 0x7F, 0x1B]
PUNCT = [b":", b"|", b"{", b"}", b'"', b"#", b">>", b"-", b"%", b"!", b"(", b")", b",", b".", b"\\", b"'", b";", b"~"]
KEYWORDS = [b"def", b"const", b"otype", b"if", b"elif", b"else", b"l", b"each", b"prob", b"every", b"skip", b"del",
            b"break", b"stop", b"ret", b"trk", b"steps", b"step", b"seed", b"menu", b"param", b"confirm", b"on", b"page",
            b"a", b"x", b"i", b"all", b"cur", b"deep", b"flip", b"o", b"drunk"]


def _split(data):
    m = SPLIT.search(data)
    eol = m.group(0) if m else b"\n"
    return SPLIT.split(data), eol


def _join(lines, eol):
    return eol.join(lines)


def _pick(rng, lines, pred=None):
    idx = [i for i, l in enumerate(lines) if (pred(l) if pred else l.strip())]
    return rng.choice(idx) if idx else None


def _code(l):
    """(code part, comment part) of a line, '#' outside strings."""
    inq = False
    for i, ch in enumerate(l):
        if ch == 0x22:
            inq = not inq
        elif ch == 0x23 and not inq:
            return l[:i], l[i:]
    return l, b""


def m_del_line(rng, d):
    L, e = _split(d)
    i = _pick(rng, L)
    if i is None:
        return None
    del L[i]
    return _join(L, e)


def m_dup_line(rng, d):
    L, e = _split(d)
    i = _pick(rng, L)
    if i is None:
        return None
    L.insert(rng.choice((i, i + 1)), L[i])
    return _join(L, e)


def m_swap_lines(rng, d):
    L, e = _split(d)
    if len(L) < 2:
        return None
    i = rng.randrange(len(L) - 1)
    j = i + 1 if rng.random() < 0.7 else rng.randrange(len(L))
    L[i], L[j] = L[j], L[i]
    return _join(L, e)


def _tokens(l):
    code, com = _code(l)
    ind = code[:len(code) - len(code.lstrip(b" \t"))]
    return ind, re.findall(rb'"[^"]*"|\{[^}]*\}|\S+', code), com


def _retok(ind, toks, com):
    return ind + b" ".join(toks) + ((b" " + com) if com else b"")


def _tok_op(rng, d, op):
    L, e = _split(d)
    idx = [i for i, l in enumerate(L) if len(_tokens(l)[1]) >= 2]
    if not idx:
        return None
    i = rng.choice(idx)
    ind, toks, com = _tokens(L[i])
    k = rng.randrange(len(toks))
    if op == "del":
        del toks[k]
    elif op == "dup":
        toks.insert(k, toks[k])
    else:
        j = rng.randrange(len(toks))
        toks[k], toks[j] = toks[j], toks[k]
    L[i] = _retok(ind, toks, com)
    return _join(L, e)


def m_del_token(rng, d):
    return _tok_op(rng, d, "del")


def m_dup_token(rng, d):
    return _tok_op(rng, d, "dup")


def m_swap_tokens(rng, d):
    return _tok_op(rng, d, "swap")


def m_indent(rng, d):
    L, e = _split(d)
    i = _pick(rng, L)
    if i is None:
        return None
    l = L[i]
    body = l.lstrip(b" \t")
    n = len(l) - len(body)
    c = rng.randrange(8)
    if c == 0:
        new = b"\t" + l[1:] if n else b"\t" + l
    elif c == 1:
        new = b" " + l
    elif c == 2:
        new = l[1:] if n else l
    elif c == 3:
        new = b"   " + body
    elif c == 4:
        new = body
    elif c == 5:
        new = l[:n // 2] + b"\t" + l[n // 2:]
    elif c == 6:
        new = b"  " + l
    else:
        new = b" \t" + body
    L[i] = new
    return _join(L, e)


def m_number(rng, d):
    L, e = _split(d)
    pat = re.compile(rb"(?<![\w.])-?(?:0[xX][0-9a-fA-F]+|\d+)(?![\w.])")
    idx = [i for i, l in enumerate(L) if pat.search(_code(l)[0])]
    if not idx:
        return None
    i = rng.choice(idx)
    code, com = _code(L[i])
    ms = list(pat.finditer(code))
    m = rng.choice(ms)
    L[i] = code[:m.start()] + rng.choice(BOUNDS) + code[m.end():] + com
    return _join(L, e)


def m_nonascii(rng, d):
    L, e = _split(d)
    i = _pick(rng, L)
    if i is None:
        return None
    b = bytes([rng.choice(NONASCII)])
    if rng.random() < 0.3:
        b = rng.choice((b, "\u00e9".encode("utf-8"), b + b, b"\xc3\x9f"))
    l = L[i]
    where = rng.randrange(4)
    code, com = _code(l)
    if where == 0:                           # in the code, anywhere
        p = rng.randrange(len(code) + 1)
        L[i] = code[:p] + b + code[p:] + com
    elif where == 1:                         # in a comment
        L[i] = l + (b" " if com else b" # ") + b
    elif where == 2:                         # inside a string if there is one
        q = [k for k, ch in enumerate(l) if ch == 0x22]
        if len(q) >= 2:
            p = rng.randrange(q[0] + 1, q[1] + 1)
            L[i] = l[:p] + b + l[p:]
        else:
            L[i] = l + b
    else:                                    # trailing
        L[i] = l + b
    return _join(L, e)


def m_eol(rng, d):
    L, _e = _split(d)
    c = rng.randrange(6)
    if c == 0:
        return b"\r\n".join(L)
    if c == 1:
        return b"\r".join(L)
    if c == 2:
        out = L[0] if L else b""
        for l in L[1:]:
            out += rng.choice((b"\n", b"\r\n", b"\r")) + l
        return out
    if c == 3:
        return b"\n".join(L).rstrip(b"\n")                 # no final newline
    if c == 4:
        return b"\r\n".join(L) + b"\r"
    return b"\n\n".join(L)


def m_long_line(rng, d):
    L, e = _split(d)
    i = _pick(rng, L)
    if i is None:
        return None
    target = rng.choice((126, 127, 128, 129, 130, 255, 300))
    l = L[i]
    if len(l) >= target:
        return None
    pad = target - len(l)
    kind = rng.randrange(3)
    if kind == 0 and pad >= 3:
        L[i] = l + b" #" + b"x" * (pad - 2)
    elif kind == 1:
        L[i] = l + b" " * pad
    else:
        L.insert(i + 1, b"#" + b"y" * (target - 1))
    return _join(L, e)


def m_truncate(rng, d):
    if len(d) < 2:
        return None
    return d[:rng.randrange(1, len(d))]


def m_punct(rng, d):
    L, e = _split(d)
    i = _pick(rng, L, lambda l: _code(l)[0].strip())
    if i is None:
        return None
    code, com = _code(L[i])
    p = rng.randrange(len(code) + 1)
    p2 = min(len(code), p + rng.choice((0, 0, 1)))
    L[i] = code[:p] + rng.choice(PUNCT) + code[p2:] + com
    return _join(L, e)


def m_keyword(rng, d):
    L, e = _split(d)
    i = _pick(rng, L, lambda l: _code(l)[0].strip())
    if i is None:
        return None
    ind, toks, com = _tokens(L[i])
    if not toks:
        return None
    toks[0] = rng.choice(KEYWORDS)
    L[i] = _retok(ind, toks, com)
    return _join(L, e)


def m_case(rng, d):
    L, e = _split(d)
    i = _pick(rng, L)
    if i is None:
        return None
    code, com = _code(L[i])
    f = rng.choice((bytes.upper, bytes.lower, bytes.title, bytes.swapcase))
    L[i] = f(code) + com
    return _join(L, e)


def m_blank(rng, d):
    L, e = _split(d)
    L.insert(rng.randrange(len(L) + 1), rng.choice((b"", b"   ", b"\t", b"# note", b"    # indented note")))
    return _join(L, e)


MUTATIONS = [m_del_line, m_dup_line, m_swap_lines, m_del_token, m_dup_token, m_swap_tokens, m_indent, m_number,
             m_nonascii, m_eol, m_long_line, m_truncate, m_punct, m_keyword, m_case, m_blank]
WEIGHTS = [4, 2, 3, 4, 2, 3, 4, 4, 4, 1, 2, 2, 5, 3, 1, 1]


# ---- multi-file mutations (list of (label, bytes) -> same)
def s_split_funcs(rng, srcs):
    """Cut the functions of the whole set into 2..3 files (some defs duplicated across files)."""
    text = b"\n".join(d for _l, d in srcs)
    parts = re.split(rb"(?m)^(?=def[ \t])", text)
    parts = [p for p in parts if p.strip()]
    if len(parts) < 2:
        return None
    n = rng.choice((2, 3))
    files = [[] for _ in range(n)]
    for p in parts:
        files[rng.randrange(n)].append(p)
        if rng.random() < 0.15:
            files[rng.randrange(n)].append(p)               # name clash
    names = [b"A.OTY", b"B.OTY", b"C.OTY"]
    return [(SCRIPTS + names[i], b"".join(f)) for i, f in enumerate(files)]


def s_dup_file(rng, srcs):
    i = rng.randrange(len(srcs))
    lab, d = srcs[i]
    new = (lab if rng.random() < 0.5 else SCRIPTS + b"COPY.OTY", d)
    out = list(srcs)
    out.insert(rng.randrange(len(out) + 1), new)
    return out


def s_reorder(rng, srcs):
    if len(srcs) < 2:
        return None
    out = list(srcs)
    rng.shuffle(out)
    return out


def s_relabel(rng, srcs):
    out = list(srcs)
    i = rng.randrange(len(out))
    out[i] = (rng.choice((b"/SCRIPTS/Z.OTY", b"-", b"a", b"/SCRIPTS/" + b"N" * 40 + b".OTY", b"/SCRIPTS/B.OTY")),
              out[i][1])
    return out


SET_MUTATIONS = [s_split_funcs, s_dup_file, s_reorder, s_relabel]


def mutate(rng, srcs):
    """One to three seeded mutations of a source set -> (kind, new set)."""
    for _ in range(20):
        kinds, cur = [], list(srcs)
        for _k in range(rng.choice((1, 1, 1, 2, 3))):
            if rng.random() < 0.08:
                f = rng.choice(SET_MUTATIONS)
                new = f(rng, cur)
                if new is not None:
                    kinds.append(f.__name__[2:])
                    cur = new
                continue
            i = rng.randrange(len(cur))
            f = rng.choices(MUTATIONS, WEIGHTS)[0]
            new = f(rng, cur[i][1])
            if new is not None:
                cur[i] = (cur[i][0], new)
                kinds.append(f.__name__[2:])
        if kinds and cur != list(srcs):
            return "+".join(kinds), cur
    return None


# ============================================================================ hand-made families
def S(*files):
    """files: (name, text) -> sources."""
    return [(SCRIPTS + n.encode(), t if isinstance(t, bytes) else _enc(t)) for n, t in files]


def one(text, name="T.OTY"):
    return S((name, text))


def fam_nesting():
    out = []
    rng = random.Random("%d/nest" % SEED)
    for depth in range(1, 12):
        for variant in range(3):
            lines, ind = ["def f%d" % depth], 1
            for k in range(depth):
                head = rng.choice(("if 1:", "l 1 2:", "each:", "if a:", "prob 50:", "every 2:", "trk 1:")) \
                    if variant else "if 1:"
                lines.append("  " * ind + head)
                ind += 1
            lines.append("  " * ind + "tr.add 1")
            if variant == 2:
                lines.append("  " * (ind - 1) + "else:")
                lines.append("  " * ind + "stop")
            out.append(("nest/%d.%d" % (depth, variant), one("\n".join(lines) + "\n")))
    out.append(("nest/modifier-chain", one("def f\n  " + "if 1: " * 30 + "tr.add 1\n")))
    out.append(("nest/modifier-chain-long", one("def f\n  " + "if 1: " * 22 + "tr.add 1\n")))
    out.append(("nest/odd-indent", one("def f\n if 1:\n   if 1:\n     if 1:\n        tr.add 1\n")))
    out.append(("nest/dedent-mismatch", one("def f\n  if 1:\n      tr.add 1\n    tr.add 2\n")))
    out.append(("nest/else-chain", one("def f\n  if a:\n    tr.add 1\n  elif b:\n    tr.add 2\n  else:\n    tr.add 3\n"
                                      "  else:\n    tr.add 4\n")))
    return out


def fam_chains():
    out = []

    def chain(n, args=False, root_first=True):
        defs = []
        for k in range(1, n + 1):
            a = " | a" if args and k > 1 else ""
            body = ("  f%d%s\n" % (k + 1, " | 1" if args else "")) if k < n else "  tr.add 1\n"
            defs.append("def f%d%s\n%s" % (k, a, body))
        return "".join(defs if root_first else reversed(defs))

    for n in range(6, 12):
        out.append(("chain/%d" % n, one(chain(n))))
        out.append(("chain/%d-rev" % n, one(chain(n, root_first=False))))
    out.append(("chain/9-split", S(("A.OTY", chain(9)[:len(chain(9)) // 2]), ("B.OTY", chain(9)[len(chain(9)) // 2:]))))
    for name, src in (
        ("self", "def a\n  a\n"),
        ("two", "def a\n  b\ndef b\n  a\n"),
        ("three", "def a\n  b\ndef b\n  c\ndef c\n  a\n"),
        ("tail", "def a\n  b\ndef b\n  c\ndef c\n  b\ndef d\n  a\n"),
        ("two-files", None),
        ("arg-cycle", "def a | n\n  b | n\ndef b | n\n  a | n\n"),
        ("needs-chain", "def bad\n  nosuchop 1\ndef a\n  bad\ndef b\n  a\ndef c\n  b\n"),
        ("diamond", "def a\n  b\n  c\ndef b\n  d\ndef c\n  d\ndef d\n  tr.add 1\n"),
        ("call-in-expr", "def a\n  x add b 1\ndef b\n  ret 5\n"),
        ("call-wrong-args", "def a\n  b | 1 2\ndef b | n\n  tr.add n\n"),
        ("call-no-bar", "def a\n  b 1\ndef b | n\n  tr.add n\n"),
        ("call-bar-noargs", "def a\n  b | 1\ndef b\n  tr.add 1\n"),
    ):
        if src is None:
            out.append(("chain/" + name, S(("A.OTY", "def a\n  b\n"), ("B.OTY", "def b\n  a\n"))))
        else:
            out.append(("chain/" + name, one(src)))
    return out


def fam_nfuncs():
    out = []
    for n in (127, 128, 129, 130, 254, 255, 256, 257):
        out.append(("nfuncs/%d" % n, one("".join("def f%d\n  tr.add 1\n" % k for k in range(n)))))
    out.append(("nfuncs/256-menus", one("".join('def f%d\n  menu "T" >> "F%d"\n  tr.add 1\n' % (k, k)
                                                 for k in range(256)))))
    out.append(("nfuncs/300-over-3-files", S(*[("F%d.OTY" % i, "".join("def g%d_%d\n  tr.add 1\n" % (i, k)
                                                                         for k in range(100))) for i in range(3)])))
    out.append(("nfuncs/255-one-refused", one("".join("def f%d\n  tr.add 1\n" % k for k in range(255))
                                              + "def bad\n  nosuchop\n")))
    return out


def table_size(srcs, profile=None):
    return len(oterr.reference(srcs, profile)[1])


def fam_table():
    """Tables around the 32 KB limit: lines of `tr.add 1` in one function, tuned by bisection."""
    out = []

    def make(n, extra=0):
        body = "def big\n" + "  tr.add 1\n" * n + "  tr.add 1000\n" * extra
        return one(body)

    lo, hi = 1, 6000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if table_size(make(mid)) <= 32768:
            lo = mid
        else:
            hi = mid - 1
    for k in (-300, -2, -1, 0, 1, 2, 300):
        out.append(("table/n%+d" % k, make(lo + k)))
    for e in range(0, 4):
        out.append(("table/edge+extra%d" % e, make(lo - 3, e)))
    # exactly 32768 and 32769 when reachable by the last 1000-lines
    for e in range(0, 8):
        s = make(lo - 4, e)
        out.append(("table/e%d" % e, s))
    out.append(("table/many-funcs", one("".join("def f%d\n  tr.add 1\n" % k for k in range(200))
                                          + "def big\n" + "  tr.add 1\n" * 1900)))
    out.append(("table/long-strings", one("".join('def f%d\n  menu "T" >> "F"\n  msg "%s%d"\n' % (k, "z" * 100, k)
                                                   for k in range(160)))))
    out.append(("table/ok-3500", one("def f\n" + "  tr.add 1\n" * 3500)))
    return out


def fam_caps():
    out = []
    for n in (62, 63, 64, 65, 66, 100):
        out.append(("caps/files%d" % n, S(*[("F%03d.OTY" % k, "def f%d\n  tr.add 1\n" % k) for k in range(n)])))
    out.append(("caps/files65-bad-first", S(*[("F%03d.OTY" % k, "def f%d\n  nosuch\n" % k) for k in range(65)])))
    # total bytes: one big file + a second one
    unit = "def f%d\n  tr.add 1\n"
    for total in (65534, 65535, 65536, 65537, 65538):
        for split in (0, 1):
            base = "".join(unit % k for k in range(1000))
            pad = total - len(base.encode())
            comment = "#" + "c" * 100 + "\n"
            filler = (comment * (pad // len(comment) + 1)).encode()[:pad]
            if split:
                a = base.encode() + filler[:pad // 2]
                b = filler[pad // 2:]
                out.append(("caps/bytes%d-split" % total, S(("A.OTY", a), ("B.OTY", b))))
            else:
                out.append(("caps/bytes%d" % total, S(("A.OTY", base.encode() + filler))))
    # later small files are refused too after the first refusal
    big = ("#" + "x" * 99 + "\n") * 650
    out.append(("caps/after-refusal", S(("A.OTY", big), ("B.OTY", big), ("C.OTY", "def c\n  tr.add 1\n"),
                                        ("D.OTY", "def d\n  tr.add 1\n"))))
    out.append(("caps/empty-files", S(*[("E%03d.OTY" % k, "") for k in range(66)])))
    out.append(("caps/no-files", []))
    out.append(("caps/files-then-bytes", S(*([("F%03d.OTY" % k, "def f%d\n  tr.add 1\n" % k) for k in range(63)]
                                              + [("Z.OTY", "#" + "q" * 65000 + "\n")]
                                              + [("Y.OTY", "def y\n  tr.add 1\n")]))))
    return out


def fam_lines():
    out = []
    for n in range(124, 133):
        out.append(("line/comment%d" % n, one("def f\n  tr.add 1\n#" + "c" * (n - 1) + "\n")))
        out.append(("line/code%d" % n, one("def f\n  tr.add 1 #" + "x" * (n - len("  tr.add 1 #")) + "\n")))
        out.append(("line/spaces%d" % n, one("def f\n  tr.add 1\n" + " " * n + "\n")))
        out.append(("line/def%d" % n, one("def f" + " " * (n - 5 - 1) + "|" + "\n  tr.add 1\n")))
    for n in range(124, 131):
        out.append(("str/const%d" % n, one('const k "%s"\ndef f\n  tr.add 1\n' % ("s" * n))))
        out.append(("str/dir%d" % n, one('def f\n  dir "%s"\n' % ("a" * n))))
        out.append(("str/menu%d" % n, one('def f\n  menu "%s" >> "X"\n  tr.add 1\n' % ("L" * min(n, 40)))))
    for n in (14, 15, 16, 17, 18):
        out.append(("list/%d" % n, one("const l {%s}\ndef f\n  tr.add 1\n" % " ".join(str(k) for k in range(n)))))
    for n in (15, 16, 17, 40):
        out.append(("name/%d" % n, one("def %s\n  tr.add 1\n" % ("n" * n))))
        out.append(("const-name/%d" % n, one("const %s 1\ndef f\n  tr.add 1\n" % ("n" * n))))
        out.append(("arg-name/%d" % n, one("def f | %s\n  tr.add 1\n" % ("n" * n))))
    for n in (3, 4, 5, 6, 7, 8):
        out.append(("args/%d" % n, one("def f | %s\n  tr.add 1\n" % " ".join("a%d" % k for k in range(n)))))
    for n in (6, 7, 8, 9, 10):
        out.append(("params/%d" % n, one("def f\n  menu \"T\" >> \"X\"\n" + "".join(
            "  param p%d 1 0 5\n" % k for k in range(n)) + "  tr.add 1\n")))
    for n in (5, 6, 7, 9, 10, 11):
        out.append(("menu-len/%d" % n, one('def f\n  menu "%s" >> "%s"\n  tr.add 1\n' % ("L" * n, "A" * n))))
    out.append(("line/long-number", one("def f\n  tr.add " + "9" * 5000 + "\n")))
    out.append(("line/long-hex", one("def f\n  tr.add 0x" + "F" * 5000 + "\n")))
    out.append(("line/long-cond", one("def f\n  prob " + "5" * 5000 + "%:\n    tr.add 1\n")))
    out.append(("line/long-ratio", one("def f\n  if " + "1" * 5000 + ":" + "2" * 5000 + ": tr.add 1\n")))
    return out


def fam_numbers():
    out = []
    nums = ["0", "-1", "1", "127", "128", "255", "256", "32767", "32768", "-32768", "-32769", "65535", "65536",
            "0x7FFF", "0X8000", "0xFFFF", "0x0", "007", "-0", "0x", "-", "+1", "1_000", "1e3", "0b1", "\u0661\u0662"]
    for n in nums:
        for name, tpl in (("stmt", "def f\n  tr.add %s\n"), ("var", "def f\n  x %s\n"), ("const", "const c %s\ndef f\n  tr.add c\n"),
                          ("param", 'def f\n  menu "T" >> "X"\n  param p %s 0 99\n  tr.add p\n'),
                          ("loop", "def f\n  l %s 3: tr.add i\n"), ("every", "def f\n  every %s: tr.add 1\n"),
                          ("list", "const l {%s 2}\ndef f\n  tr.add 1\n"), ("otype", "otype %s\ndef f\n  tr.add 1\n"),
                          ("range", "def f\n  tr.add 1\n  pl.set 1 amp.vol %s\n")):
            out.append(("num/%s/%s" % (name, n if n.isascii() else "arabic-indic"), one(tpl % n)))
    conds = ["50%", "0%", "100%", "1%", "99%", "5%", "050%", "1:4", "1:1", "0:1", "8:8", "9:9", "4:2", "8:1", "2:8", "01:04",
             "fill", "!fill", "pre", "nei", "1st", "!1st", "FILL", "!Fill"]
    for c in conds:
        for name, tpl in (("cond", "def f\n  cond.set 1 %s\n"), ("prob", "def f\n  prob %s: tr.add 1\n"),
                          ("if", "def f\n  if %s: tr.add 1\n"), ("const", "const c %s\ndef f\n  tr.add 1\n"),
                          ("add", "def f\n  x add %s 1\n"), ("stmt", "def f\n  %s\n")):
            out.append(("cond/%s/%s" % (name, c), one(tpl % c)))
    return out


def fam_stack():
    out = []
    for k in range(11, 20):
        out.append(("stack/%d" % k, one("def f\n  a " + "add 1 " * k + "1\n")))
        out.append(("stack/loop%d" % k, one("def f\n  l 1 2:\n    if 1:\n      a " + "add 1 " * k + "1\n")))
        out.append(("stack/call%d" % k, one("def g | n\n  tr.add n\ndef f\n  g | " + "add 1 " * k + "1\n")))
        out.append(("stack/caller-of-bad%d" % k, one("def g\n  a " + "add 1 " * k + "1\ndef f\n  g\ndef h\n  f\n")))
        out.append(("stack/ternary%d" % k, one("def f\n  a " + "? 1 1 " * k + "1\n")))
    return out


def fam_counters():
    out = []
    for n in (254, 255, 256, 257):
        for w in ("every", "skip"):
            out.append(("ctr/%s%d" % (w, n), one("def f\n" + ("  %s 2: tr.add 1\n" % w) * n)))
    out.append(("ctr/mixed256", one("def f\n" + "  every 2: tr.add 1\n  skip 3: tr.add 2\n" * 128)))
    return out


def fam_x9():
    """What does not fit the OTB format or the VM's caps at load is a compile error of the function (or of
    the table): control frames and context scopes over 16, 256 `menu` lines, more than 1024 counters in the table,
    a loop after more than 65535 bytes of code."""
    out = []
    for n in (14, 15, 16, 17, 18, 254, 255, 256, 257):
        out.append(("x9/trk%d" % n, one("def f\n" + "  trk 1\n" * n + "  tr.add 1\n")))
    out.append(("x9/trk-caller", one("def g\n" + "  trk 1\n" * 17 + "  tr.add 1\ndef f\n  g\ndef h\n  tr.add 2\n")))
    out.append(("x9/trk-mods", one("def f\n  trk 1: trk 2: trk 3: l 1 2: tr.add 1\n")))
    for d in (6, 7, 8, 9):                  # nested lines `seed 1: l 1 2:` (a scope and a loop frame each): context depth 1 + 2 d
        body = "def f\n"
        for k in range(d):
            body += "  " * (k + 1) + "seed 1: l 1 2:\n"
        body += "  " * (d + 1) + "tr.add 1\n"
        out.append(("x9/nest%d" % d, one(body)))
        out.append(("x9/nest%d-caller" % d, one(body + "def g\n  f\n")))
    for n in (254, 255, 256, 257):
        menus = "".join('  menu "A" >> "B%d"\n' % k for k in range(n))
        out.append(("x9/menu%d" % n, one("def f\n" + menus + "  tr.add 1\n")))
    out.append(("x9/menu-caller", one("def f\n" + "".join('  menu "A" >> "B%d"\n' % k for k in range(256)) + "  tr.add 1\ndef g\n  f\n")))

    def ctrs(n, name="f"):
        return "def %s\n" % name + "  every 2: tr.add 1\n" * n

    for tot in (1023, 1024, 1025, 1026):        # counters of the whole table: the VM takes at most 1024
        k, r = divmod(tot, 205)
        out.append(("x9/counters%d" % tot, one("".join(ctrs(205, "f%d" % i) for i in range(k)) + (ctrs(r, "g") if r else ""))))
    out.append(("x9/counters-files", S(("A.OTY", ctrs(255, "fa")), ("B.OTY", ctrs(255, "fb")), ("C.OTY", ctrs(255, "fc")),
                                      ("D.OTY", ctrs(255, "fd")), ("E.OTY", ctrs(5, "fe")))))
    out.append(("x9/counters-refused", one("".join(ctrs(205, "f%d" % i) for i in range(5)) + "def h\n  nosuchop 1\n")))
    line = "  a " + "+ " * 14 + "1 " * 15 + "\n"
    for tail in ("  l 1 2: tr.add 1\n", "  each: tr.add 1\n", "  trk 1\n  tr.add 1\n", "  trk 1: tr.add 1\n"):
        out.append(("x9/backjump%d" % len(tail), one("def f\n" + line * 900 + tail)))
    return out


def fam_codelen():
    """Code of one function around 65535 bytes: dense lines `a + + ... 1 1 ...` (5 code bytes per 4 source ones)."""
    line = " a " + "+ " * 14 + "1 " * 15 + "\n"
    out = []

    def too_long(n):
        return "code too long" in oterr.reference(one("def f\n" + line * n), None)[0]

    lo, hi = 600, 1000                      # first n whose code is over 65535 bytes
    while lo < hi:
        mid = (lo + hi) // 2
        if too_long(mid):
            hi = mid
        else:
            lo = mid + 1
    for n in (700, lo - 2, lo - 1, lo, lo + 1, 1000):
        out.append(("codelen/%d" % n, one("def f\n" + line * n)))
    return out


def fam_cost():
    out = []
    for n in (4094, 4095, 4096, 4097, 4098):
        out.append(("cost/loop%d" % n, one('def f\n  menu "T" >> "X"\n  l 1 %d: tr.add i\n' % n)))
        out.append(("cost/brick%d" % n, one("def f\n  l 1 %d: tr.add i\n" % n)))
    for a, b in ((64, 64), (64, 65), (63, 65), (2, 2049), (2, 2048)):
        out.append(("cost/nest%dx%d" % (a, b), one('def f\n  menu "T" >> "X"\n  l 1 %d: l 1 %d: tr.add i\n' % (a, b))))
    out.append(("cost/huge", one("def f\n  l 0 32767:\n    l 0 32767:\n      l 0 32767:\n        l 0 32767:\n"
                                  "          l 0 32767:\n            l 0 32767:\n              l 0 32767:\n"
                                  "                l 0 32767:\n                  tr.add 1\n")))
    out.append(("cost/huge-menu", one('def f\n  menu "T" >> "X"\n  l -32768 32767:\n    l -32768 32767:\n'
                                      '      l -32768 32767:\n        l -32768 32767:\n          l -32768 32767:\n'
                                      '            tr.add 1\n')))
    out.append(("cost/each", one('def f\n  menu "T" >> "X"\n  each: each: each: tr.add i\n')))
    out.append(("cost/trk", one('def f\n  menu "T" >> "X"\n  trk all:\n    l 1 500: tr.add i\n')))
    return out


def fam_nonascii():
    """Every byte in two contexts, selected bytes in many contexts: the traps of ASCII-only case folding."""
    out = []
    ctx = (
        ("code", "def f\n  tr.add %s1\n"),
        ("string", 'def f\n  dir "A%sB"\n'),
    )
    for b in range(256):
        if b in (0x0A, 0x0D):
            continue
        ch = bytes([b])
        for name, tpl in ctx:
            pre, post = tpl.split("%s")
            out.append(("byte/%s/%02x" % (name, b), one(pre.encode() + ch + post.encode())))
    sel = [0x00, 0x09, 0x0B, 0x0C, 0x1C, 0x1F, 0x20, 0x7F, 0x80, 0x85, 0xA0, 0xAD, 0xB2, 0xDF, 0xE9, 0xFF]
    ctxs = (
        ("indent", "def f\n%str.add 1\n"),
        ("trail", "def f\n  tr.add 1%s\n"),
        ("only", "def f\n  tr.add 1\n%s\n"),
        ("defname", "def f%sg\n  tr.add 1\n"),
        ("defargs", "def f | a%sb\n  tr.add a\n"),
        ("const", "const k%s1\ndef f\n  tr.add k\n"),
        ("conststr", 'const k "x%sy"\ndef f\n  dir k\n'),
        ("list", "const l {1%s2}\ndef f\n  tr.add 1\n"),
        ("comment", "def f\n  tr.add 1 # c%sc\n"),
        ("commentline", "# c%sc\ndef f\n  tr.add 1\n"),
        ("first", "%sdef f\n  tr.add 1\n"),
        ("word", "def f\n  tr%sadd 1\n"),
        ("menu", 'def f\n  menu "A%sB" >> "C"\n  tr.add 1\n'),
        ("otype", "otype%s1\ndef f\n  tr.add 1\n"),
        ("upper-ss", "def f\n  tr.add 1\n  \u00df\n"),
    )
    for b in sel:
        ch = bytes([b])
        for name, tpl in ctxs:
            if "%s" in tpl:
                pre, post = tpl.split("%s")
                out.append(("ctx/%s/%02x" % (name, b), one(pre.encode("latin-1") + ch + post.encode("latin-1"))))
    out.append(("ctx/upper-ss", one("def f\n  tr.add 1\n  \u00df\n")))
    out.append(("ctx/ydiaeresis-name", one("def \u00ff\n  tr.add 1\n")))
    out.append(("ctx/superscript-digit", one("def f\n  tr.add \u00b2\n")))
    out.append(("ctx/nbsp-sep", one("def f\n  tr.add\u00a01\n")))
    out.append(("ctx/nel-sep", one("def f\n  tr.add\u00851\n")))
    out.append(("ctx/ff-sep", one("def f\n  tr.add\x0c1\n")))
    out.append(("ctx/vt-trailing", one("def f\n  tr.add 1\x0b\n")))
    out.append(("ctx/nbsp-def", one("def\u00a0f\n  tr.add 1\n")))
    out.append(("ctx/utf8-comment", one("def f # caf\u00c3\u00a9\n  tr.add 1\n")))
    out.append(("ctx/tab-trailing", one("def f\n  tr.add 1\t\t\n")))
    out.append(("ctx/tab-mid", one("def f\n  tr.add\t1\n")))
    out.append(("ctx/tab-indent", one("def f\n\ttr.add 1\n")))
    out.append(("ctx/tab-space-indent", one("def f\n \t tr.add 1\n")))
    out.append(("ctx/cr-only", one("def f\r  tr.add 1\r  tr.add 2\r")))
    out.append(("ctx/crlf", one("def f\r\n  tr.add 1\r\n\r\n  tr.add 2\r\n")))
    out.append(("ctx/lfcr", one("def f\n\r  tr.add 1\n\r")))
    out.append(("ctx/quote-in-list", one("const l {1 ' 2}\ndef f\n  tr.add 1\n")))
    out.append(("ctx/quote-name", one("def f\n  tr.add 1\ndef it's\n  tr.add 1\n")))
    out.append(("ctx/backslash-name", one("def a\\b\n  tr.add 1\n")))
    out.append(("ctx/param-str-name", one('def f\n  menu "T" >> "X"\n  param "it\'s" 1 0 2\n  tr.add 1\n')))
    out.append(("ctx/dquote-in-list", one('const l {1 "a" 2}\ndef f\n  tr.add 1\n')))
    return out


def fam_labels():
    out = []
    for n in (200, 254, 255, 256, 300):
        out.append(("label/%d" % n, [(b"/SCRIPTS/" + b"L" * (n - 9 - 4) + b".OTY", b"def f\n  tr.add 1\n")]))
    out.append(("label/sort", [(b"/SCRIPTS/b.OTY", b"def b\n  nosuch\n"), (b"/SCRIPTS/A.OTY", b"def a\n  nosuch\n"),
                               (b"/SCRIPTS/B.OTY", b"def c\n  nosuch\n"), (b"-", b"def d\n  nosuch\n")]))
    out.append(("label/same", [(b"/SCRIPTS/A.OTY", b"def a\n  nosuch\n"), (b"/SCRIPTS/A.OTY", b"def b\n  nosuch\ndef a\n  tr.add 1\n")]))
    out.append(("label/empty", [(b"", b"def a\n  nosuch\n")]))
    out.append(("label/high", [(b"/SCRIPTS/\xe9.OTY", b"def a\n  nosuch\n")]))
    return out


def fam_misc():
    """Targeted seeds for messages the other families rarely hit."""
    out = []
    for i, s in enumerate((
        "otype 1\notype 1\ndef f\n  tr.add 1\n", "def f\n  tr.add 1\notype 1\n", "otype\ndef f\n  tr.add 1\n",
        "otype 2\ndef f\n  tr.add 1\ndef g\n  tr.add 2\n", "otype x\n", "otype 1 2\n", "  tr.add 1\n", "tr.add 1\n",
        "const\n", "const a\n", "const a 1 2\n", "const 1 2\n", "const a tr.add\n", "const a b\n", "const a add 1 2\n",
        "const a 1\nconst a 2\ndef f\n  tr.add a\n", "const f 1\ndef f\n  tr.add 1\n", "const x 1\n", "const tr.add 1\n",
        "def\n", "def f g\n  tr.add 1\n", "def f |\n  tr.add 1\n", "def f | a a\n  tr.add a\n", "def f | tr.add\n  tr.add 1\n",
        "def f | a | b\n  tr.add 1\n", "def f | a:\n  tr.add 1\n", "def f | a b c d e\n  tr.add 1\n",
        "const k 1\ndef f | k\n  tr.add k\n", "def g\n  tr.add 1\ndef f | g\n  tr.add 1\n", "def f | a\n  tr.add 1\n",
        "def f\n  tr.add 1\ndef f\n  tr.add 2\n", "def tr.add\n  tr.add 1\n", "def 9a\n  tr.add 1\n", "def if\n  tr.add 1\n",
        "def x\n  tr.add 1\n", "def fill\n  tr.add 1\n", "def a.b\n  tr.add 1\n", "def f\n", "def f\n  # nothing\n",
        "def f\n  tr.add 1\n    tr.add 2\n", "def f\n  if 1:\n  tr.add 1\n", "def f\n  if 1:\n", "def f\n   tr.add 1\n  tr.add 2\n",
        "def f\n  tr.add 1\n tr.add 2\n", "def f\n  if 1:\n    tr.add 1\n   tr.add 2\n", "def f\n  else:\n    tr.add 1\n",
        "def f\n  tr.add 1\n  elif 1:\n    tr.add 1\n", "def f\n  if 1:\n    tr.add 1\n  if 1: else: tr.add 1\n",
        "def f\n  break\n", "def f\n  l 1 3:\n    break\n  break\n", "def f\n  each: break\n",
        "def f\n  menu \"A\" >> \"B\"\n  tr.add 1\n  menu \"C\" >> \"D\"\n", "def f\n  tr.add 1\n  param p 1 0 2\n",
        "def f\n  if 1:\n    param p 1 0 2\n  tr.add 1\n", "def f\n  param p 1 0 2\n  tr.add p\n",
        "def f\n  menu \"A\" >> \"B\"\n  param p 3 0 2\n  tr.add p\n", "def f\n  menu \"A\" >> \"B\"\n  param p 1\n  tr.add 1\n",
        "def f\n  menu \"A\" >> \"B\"\n  param p a 0 2\n  tr.add 1\n", "def f\n  menu \"A\" >> \"B\"\n  param tr.add 1 0 2\n  tr.add 1\n",
        "def f | p\n  menu \"A\" >> \"B\"\n  param p 1 0 2\n  tr.add 1\n", "const k 1\ndef f\n  menu \"A\" >> \"B\"\n  param k 1 0 2\n",
        "def f\n  menu \"A\" >> \"B\"\n  param p 1 0 2\n  param p 1 0 2\n  tr.add 1\n",
        "const k 5\ndef f\n  menu \"A\" >> \"B\"\n  param p k 0 k\n  tr.add p\n",
        "def f\n  menu \"A\" >> \"B\"\n  menu\n", "def f\n  menu \"A\"\n  tr.add 1\n", "def f\n  menu \"A\" \"B\"\n  tr.add 1\n",
        "def f\n  menu \"A\" >> \"B\" >> \"C\"\n  tr.add 1\n", "def f\n  menu A >> B\n  tr.add 1\n",
        "def f\n  on\n  tr.add 1\n", "def f\n  on trig\n  tr.add 1\n", "def f\n  on foo 1 2\n  tr.add 1\n", "def f\n  on 5\n  tr.add 1\n",
        "def f\n  on 1:4\n  tr.add 1\n", "def f\n  confirm\n  tr.add 1\n", "def f\n  confirm \"sure\"\n  tr.add 1\n",
        "def f\n  confirm 5\n  tr.add 1\n", "def f\n  confirm \"a\" 1\n  tr.add 1\n", "def f\n  if 1: menu \"A\" >> \"B\"\n  tr.add 1\n",
        "def f\n  del 1: tr.add 1\n", "def f\n  l 1: tr.add 1\n", "def f\n  l: tr.add 1\n", "def f\n  if: tr.add 1\n",
        "def f\n  if 1 tr.add 1\n", "def f\n  if 1:\n", "def f\n  prob 50%: tr.add 1\n", "def f\n  prob 50: tr.add 1\n",
        "def f\n  prob: tr.add 1\n", "def f\n  every 0: tr.add 1\n", "def f\n  tr.add 1: tr.add 2\n", "def f\n  tr.add 1 :\n",
        "def f\n  a\n", "def f\n  i 1\n", "def f\n  a 1 2\n", "def f\n  a b c\n", "def f\n  1\n", "def f\n  \"x\"\n", "def f\n  {1 2}\n",
        "def f\n  50%\n", "def f\n  :\n", "def f\n  |\n", "def f\n  >>\n", "def f\n  add 1 2\n", "def f\n  er 1 2 3\n", "def f\n  flip\n",
        "def f\n  flip 1\n", "def f\n  o\n  o.max 5\n", "def f\n  file.rnd\n", "def f\n  x file.rnd\n", "def f\n  tr.add\n",
        "def f\n  tr.add 1 2\n", "def f\n  tr.add 1 2 3 4\n", "def f\n  trk\n", "def f\n  trk 1 2 3 4 5 6 7 8 9\n  tr.add 1\n",
        "def f\n  trk 1 2 3 4 5 6 7 8\n  tr.add 1\n", "def f\n  trk all: tr.add 1\n", "def f\n  trk cur 1: tr.add 1\n",
        "def f\n  steps\n", "def f\n  steps 1\n", "def f\n  steps all: tr.add 1\n", "def f\n  steps has.trig: tr.add 1\n",
        "def f\n  steps 1 4: tr.add 1\n", "def f\n  step 3: tr.add 1\n", "def f\n  seed 3\n  tr.add 1\n", "def f\n  seed\n",
        "def f\n  x tr.add 1\n", "def f\n  x\n", "def f\n  x x\n  y x\n", "def f\n  x \"s\"\n", "def f\n  tr.add \"s\"\n",
        "def f\n  tr.add {1 2}\n", "def f\n  tr.add 50%\n", "def f\n  tr.add 5:2\n", "def f\n  cond.set 1 2\n",
        "def f\n  cond.set 1 fill\n", "def f\n  ret 1\n  tr.add 1\n", "def f\n  ret\n", "def f\n  stop 1\n", "def f\n  break 1\n",
        "def f\n  ret ret\n", "def f\n  x ret\n", "def f\n  x if\n", "def f\n  x deep\n", "def f\n  x all\n", "def f\n  tr.add all\n",
        "def f\n  tr.add cur\n", "def f\n  dir \"A\" deep\n", "def f\n  dir \"A\" deeper\n", "def f\n  ext wav aif\n", "def f\n  ext foo\n",
        "def f\n  match \"a*\"\n", "def f\n  match\n", "def f | a\n  tr.add a\ndef g\n  f | \"s\"\n",
        "def f | a\n  x a\n  tr.add a\n  dir a\ndef g\n  f | 1\n", "def f | a\n  l 1 a: tr.add i\ndef g | n\n  f | n\n",
        "def f | a\n  l 1 a: tr.add i\ndef g\n  x 3\n  f | x\n", "def f | a\n  l 1 a: tr.add i\ndef g\n  f | 3\n",
        "def f\n  l 1 x: tr.add i\n", "def f\n  l 1 add 1 2: tr.add i\n", "def f\n  l 1 \"s\": tr.add i\n", "def f\n  l 1 50%: tr.add i\n",
        "const k 3\ndef f\n  l 1 k: tr.add i\n", "const k \"s\"\ndef f\n  l 1 k: tr.add i\n", "def f\n  menu \"A\" >> \"B\"\n  param p 3 1 5\n  l 1 p: tr.add i\n",
        "def f | a\n  tr.add a\ndef g | b\n  f | b\ndef h\n  g | 7\n", "def f | a\n  tr.add a\n  pl.set 1 amp.vol a\ndef g\n  f | 1000\n",
        "def f | a\n  dir a\ndef g\n  f | 1\n", "def f | a\n  tr.add a\n  dir a\n", "def f | a\n  cond.set 1 a\ndef g\n  f | 50%\n",
        "def f | a\n  cond.set 1 a\ndef g\n  f | 5\n", "def f | a\n  cond.set 1 a\n  tr.add a\n", "def f | a b\n  tr.add a\n",
        "def f | a\n  tr.add 1\ndef g\n  f | 1 2\n", "def f | a\n  tr.add 1\ndef g\n  f\n", "def f\n  tr.add 1\ndef g\n  f | 1\n",
        "def f\n  ret 3\ndef g\n  tr.add f\n", "def f\n  tr.add 1\ndef g\n  x f\n", "def f | a\n  ret a\ndef g\n  x f | 3\n  y f | f | 3\n",
        "const c 1\ndef f\n  c\n", "const c 1\ndef f\n  c 2\n", "const c 1\ndef f\n  x c\n", "const c \"a\"\ndef f\n  x c\n",
        "const c 1 \ndef f | q\n  q 1\n", "def f | q\n  q\n", "def f\n  menu \"A\" >> \"B\"\n  param q 1 0 2\n  q\n",
        "def f\n  menu \"A\" >> \"B\"\n  param q 1 0 2\n  q 1\n", "def f\n  fill 1\n", "def f\n  if fill: tr.add 1\n",
        "def f\n  x fill\n", "def f\n  cond.set 1 !1st\n",
        "def f\n  tr.add 1 # c\ndef g\n  tr.add 2 # \xe9\n",
        "def f\n  dir \"a\"b\n", "def f\n  dir \"a\n", "def f\n  dir \"a\"\"b\"\n", "def f\n  dir \"a\":\n", "def f\n  dir \"a\"|\n",
        "def f\n  tr.add {1 2\n", "def f\n  tr.add {}\n", "def f\n  tr.add }\n", "def f\n  tr.add {a}\n", "def f\n  tr.add {1 b 3}\n",
        "def f\n  tr.add {1 2}x\n", "def f\n  x pick {1 2 3}\n", "def f\n  x pick {1,2}\n", "def f\n  x -3\n  y - 3 1\n", "def f\n  x --3\n",
        "def f\n  x 1:4\n", "def f\n  x 1:\n", "def f\n  x :4\n", "def f\n  x 1::4\n",
        "def f\n  x ?\n", "def f\n  x ? 1\n", "def f\n  x ? 1 2\n", "def f\n  x ? 1 2 3\n", "def f\n  x ? a file.pick 1\n",
        "def f\n  seed ? flip 7 0\n  tr.add 1\n", "def f\n  tr.add rnd 3\n  tr.add file.rnd\n", "def f\n  x lim 1 2\n",
        "def f\n  x lim 1 2 3 4\n", "def f\n  x + 1 2\n  y - 1 2\n", "def f\n  x == 1 2\n", "def f\n  x <= 1 2\n",
        "def f\n  er.r 1 2 3 4\n", "def f\n  pl.set 1 amp.vol dr.v 0 1\n", "def f\n  tr.add 1\n  tr.del\n  tr.flip\n", "def f\n  msg \"hello\"\n",
    )):
        out.append(("misc/%03d" % i, one(s)))
    return out


def fam_fixes():
    """Seeds for the checker's rules (once-only const errors, a string is not a param name, signed numbers out
    of range, an erroneous line opens no block) and for the cap of 1000 report lines."""
    out = []
    for i, t in enumerate(("const x 99999\ndef f\n  tr.add 1\n", "const y -99999\n", "const w \"a\ndef f\n  tr.add 1\n",
                           "const v 1\x01\n", "const u {1 2 99999}\n", "const t 1 # " + "z" * 130 + "\nconst s 1 2 # " + "z" * 130 + "\n",
                           "const 5% 1\nconst a%\n", "const zz 7%\nconst yy 0x10000\ndef f\n  tr.add zz\n")):
        out.append(("fix/const-once-%d" % i, one(t)))
    for i, t in enumerate(('def f\n  menu "A" >> "B"\n  param "ABC" 1 0 2\n  tr.add 1\n',
                           'def f\n  menu "A" >> "B"\n  param "abc" 1 0 2\n  param "R2" 1 0 2\n  param "" 1 0 2\n  tr.add 1\n',
                           'def f\n  menu "A" >> "B"\n  param ABC 1 0 2\n  param fill 1 0 2\n  param 5 1 0 2\n  param 50% 1 0 2\n  param {1} 1 0 2\n'
                           '  tr.add 1\n',
                           'def f\n  menu "A" >> "B"\n  param "IT\'S" 1 0 2\n  param "A.B" 1 0 2\n  param "' + "X" * 40 + '" 1 0 2\n  tr.add 1\n')):
        out.append(("fix/param-str-%d" % i, one(t)))
    for i, t in enumerate(("def f\n  a -32769\n  a 32768\n  a -0X8001\n  a 0x8000\n  a -99999999999\n  a -32768\n  a 32767\n",
                           "def f\n  b {1 -40000}\n  b {-0x10000 0 1}\n  b {-32768 32767}\n",
                           "const k -40000\nconst j 40000\nconst i -0x9000\n",
                           "def f\n  cond.set 1 -050%\n  x -1:4\n  x - 5 -99999\n")):
        out.append(("fix/sign-%d" % i, one(t)))
    for i, t in enumerate(("def f\n  if nosuch:\n    tr.add 1\n  tr.add 2\n", "def f\n  tr.add 1\n  if nosuch:\n",
                           "def f\n  tr.add 1\n  l 1 \x01:\n    tr.add 2\n", "def f\n  tr.add 1\n  if 1 2:\n    tr.add 2\n  else:\n    tr.add 3\n",
                           "def f\n  tr.add 1\n  else nosuch:\n    tr.add 1\n", "def f\n  each nosuch:\n    tr.add 1\n      tr.add 2\n",
                           "def f\n  if 1:\n    if bad:\n      tr.add 1\n    tr.add 2\n  tr.add 3\n",
                           "def f\n  trk 1 2 3 4 5 6 7 8 9:\n    tr.add 1\n  tr.add 2\n", "def f\n  trk:\n    tr.add 1\n",
                           "def f\n  prob 33%:\n    tr.add 1\n  prob 34%:\n    tr.add 1\n", "def f\n  if 1:\n  tr.add 1\n",
                           "def f\n  on trig:\n    tr.add 1\n", "def f\n  menu \"A\" >> \"B\":\n    tr.add 1\n",
                           "def f\n  confirm 5:\n    tr.add 1\n  tr.add 2\n", "def f\n  tr.add 1:\n    tr.add 1\n",
                           "def f\n  trk 1: x\n    tr.add 1\n", "def f\n  trk 1:\n    if bad:\n  tr.add 3\n")):
        out.append(("fix/opens-%d" % i, one(t)))
    # the cap of 1000 report lines (the first 1000 after sorting), around the limit and with several sources
    for n in (999, 1000, 1001, 1002, 1500, 3000):
        out.append(("cap/stmt-%d" % n, one("def f\n" + "  nosuch\n" * n)))
    out.append(("cap/two-funcs", one("def f\n" + "  nosuch\n" * 600 + "def g\n" + "  nosuch\n" * 600)))
    out.append(("cap/warnings", one("def f | p q r\n  tr.add 1\n" + "def g%d | p\n  tr.add 1\n" * 0 + "".join(
        "def g%d | p\n  tr.add 1\n" % k for k in range(1100)))))
    out.append(("cap/type-errors", one("".join("def f%d\n  tr.add \"s\"\n" % k for k in range(1100)))))
    out.append(("cap/needs", one("def bad\n  nosuch\n" + "".join("def f%d\n  bad\n" % k for k in range(1200)))))
    out.append(("cap/loops", one("".join("def f%d\n  f%d\n" % (k, (k + 1) % 300) for k in range(300)))))
    out.append(("cap/loops-many", one("".join("def a%d\n  a%d\n" % (k, k) for k in range(1100)))))
    out.append(("cap/file-errors", one("const\n" * 1100)))
    out.append(("cap/mixed-passes", one("def f | p\n  tr.add 1\n" + "def g%d\n  nosuch\n  tr.add \"s\"\n" % 0
                                           + "".join("def h%d | p\n  tr.add p\n  nosuch\n" % k for k in range(500)))))
    # the sort is by label bytes, not by card order: the cut falls in the file that sorts first
    out.append(("cap/label-order", S(("Z.OTY", "def z\n" + "  nosuch\n" * 700), ("A.OTY", "def a\n" + "  nosuch\n" * 700),
                                     ("M.OTY", "def m\n" + "  nosuch\n" * 700))))
    out.append(("cap/label-dash", [(b"/SCRIPTS/A.OTY", b"def a\n" + b"  nosuch\n" * 1100), (b"-", b"def b\n  nosuch\n"),
                                   (b"!x", b"def c\n  nosuch\n")]))
    out.append(("cap/table-error-sorts-first", S(("A.OTY", "".join("def f%d\n  tr.add 1\n" % k for k in range(256)) + "def g\n" + "  nosuch\n" * 1100))))
    out.append(("cap/2-byte-lines", one("def f\n" + " x\n" * 21800)))
    out.append(("cap/hidden", one("otype 2\ndef f\n" + "  nosuch\n" * 1100)))
    return out


def fam_checker():
    """The checker passes at their limits: kinds and ranges passed along long chains (the fixpoints stop after 40 / 30
    rounds, in a given order), constants through calls, trk multipliers, deep call graphs and their paths."""
    out = []
    # kinds travel along a chain f0 | a -> f1 | a -> ... -> fn (uses the argument as a string / int / condition / list)
    for n in (3, 8, 30, 38, 39, 40, 41, 42, 45):
        for use, extra in (("dir a", ""), ("tr.add a", ""), ("cond.set 1 a", ""), ("a pick a", ""), ("dir a", "tr.add a")):
            for order in ("fwd", "rev"):
                fs = []
                for k in range(n):
                    body = "  f%d | a\n" % (k + 1) if k < n - 1 else "  %s\n" % use + ("  %s\n" % extra if extra else "")
                    fs.append("def f%d | a\n%s" % (k, body))
                if order == "rev":
                    fs.reverse()
                out.append(("chk/kinds-%d-%s-%s" % (n, use.split()[0], order + extra[:3].strip()),
                            one("".join(fs) + "def top\n  f0 | \"s\"\n  f0 | 3\n")))
    # ranges: a loop bound passed down a chain, the callers give different constants
    for n in (2, 5, 28, 29, 30, 31, 33):
        for order in ("fwd", "rev"):
            fs = ["def c%d | a\n  %s\n" % (k, "c%d | a" % (k + 1) if k < n - 1 else "l 1 a: tr.add i") for k in range(n)]
            if order == "rev":
                fs.reverse()
            out.append(("chk/range-chain-%d-%s" % (n, order),
                        one("".join(fs) + 'def top\n  menu "T" >> "X"\n  c0 | 10\n  c0 | 3000\n  c0 | -5\n')))
            out.append(("chk/range-chain-%d-%s-brick" % (n, order), one("".join(fs) + "def top\n  c0 | 700\n")))
    # constness: an argument is constant only if every call passes a constant
    out.append(("chk/const-ok", one("def f | a\n  l 1 a: tr.add i\ndef g | b\n  f | b\ndef h\n  g | 3\n  g | 4\n")))
    out.append(("chk/const-hint", one("def f | a\n  l 1 a: tr.add i\ndef g | b\n  f | b\ndef h\n  g | 3\n  x rnd 4\n  g | x\n")))
    out.append(("chk/const-hint2", one("def f | a b\n  l a b: tr.add i\ndef g | p\n  f | 1 p\ndef h\n  f | 2 3\n  x 4\n  f | x 3\n")))
    out.append(("chk/const-param", one('def f | a\n  l 1 a: tr.add i\ndef g\n  menu "T" >> "X"\n  param p 3 1 5\n  f | p\n')))
    out.append(("chk/const-const", one("const k 4\nconst s \"x\"\ndef f | a\n  l 1 a: tr.add i\ndef g\n  f | k\n  f | s\n  l 1 k: tr.add 1\n  l 1 s: tr.add 1\n")))
    # trk multipliers: a chain of consecutive trk lines multiplies, saturating at 2**32-1
    for n in (1, 2, 5, 10, 11, 12, 20, 40, 300):
        out.append(("chk/trk-chain-%d" % n, one('def f\n  menu "T" >> "X"\n' + "  trk all\n" * n + "  l 1 50: tr.add i\n")))
        out.append(("chk/trk-nested-%d" % n, one('def f\n  menu "T" >> "X"\n' + "  trk all: trk all\n" * 0 + "  trk 1 2 3\n" * n + "  tr.add 1\n")))
    out.append(("chk/trk-mods", one('def f\n  menu "T" >> "X"\n  trk all: l 1 100: trk 1 2: tr.add i\n  trk cur: tr.add 1\n')))
    out.append(("chk/trk-block", one('def f\n  menu "T" >> "X"\n  trk all:\n    tr.add 1\n    trk 1 2:\n      tr.add 2\n  tr.add 3\n')))
    out.append(("chk/trk-250", one("def f\n" + "  trk 1\n" * 250 + "  tr.add 1\n")))    # (255 chained passes crash otb.compile_program: max_ctl packs in one byte)
    # saturation: the cost passes 2**32 at an inner step and the menu check prints the saturated number
    out.append(("chk/saturate", one('def f\n  menu "T" >> "X"\n' + "  l 0 32767:\n".replace("\n", "\n") * 0
                                    + "".join("%sl 0 32767:\n" % ("  " * (k + 1)) for k in range(3)) + "      each: tr.add 1\n")))
    out.append(("chk/saturate2", one('def f\n  menu "T" >> "X"\n  l 0 32767:\n    l 0 32767:\n      l 0 32767:\n        l 0 32767:\n          tr.add 1\n')))
    out.append(("chk/saturate-calls", one("def a\n  l 0 32767:\n    l 0 32767:\n      l 0 32767:\n        tr.add 1\n"
                                          'def b\n  a\n  a\n  a\ndef c\n  menu "T" >> "X"\n  b\n  b\n  b\n  b\n  b\n')))
    out.append(("chk/lazy", one('def f\n  menu "T" >> "X"\n  x ? 1 rnd 4 rrand 1 2\n  y ? x l 0 1 2 3\n  tr.add ? 1 2 3\n')))
    out.append(("chk/if-chain", one('def f | a\n  menu "T" >> "X"\n  if a:\n    l 1 100: tr.add i\n  elif a:\n    l 1 200: tr.add i\n  else:\n    tr.add 1\n  if 1: tr.add 1\n  else: l 1 5: tr.add 1\n')))
    for k in (500, 700, 1000, 1023, 1024, 1025, 1030):        # the branches of a chain fit the budget, their sum does not
        out.append(("chk/if-chain-fit-%d" % k, one('def f | n\n  menu "T" >> "X"\n  if n:\n    l 1 %d: tr.add i\n  elif n:\n    l 1 %d: tr.add i\n  else:\n    l 1 %d: tr.add i\n' % (k, k, k))))
        out.append(("chk/if-chain-else-%d" % k, one('def f | n\n  menu "T" >> "X"\n  if n:\n    tr.add 1\n  else:\n    l 1 %d: tr.add i\n  else:\n    l 1 %d: tr.add i\n  elif n:\n    tr.add 2\n' % (k, 2 * k))))
    out.append(("chk/else-else", one('def f\n  menu "T" >> "X"\n  if 1:\n    tr.add 1\n  else:\n    l 1 400: tr.add 1\n  else:\n    tr.add 1\n')))
    out.append(("chk/params-range", one('def f\n  menu "T" >> "X"\n  param p 3 1 500\n  param q 2 -9 9\n  l p q: tr.add i\n  l q 1: tr.add i\n  l 1 p: tr.add i\n')))
    out.append(("chk/args-none", one("def f | a b\n  l a b: tr.add i\ndef g\n  f | 3 5\ndef h | z\n  f | z 1\n  f | 7 z\n")))
    # call depth and cycles
    for n in (7, 8, 9, 10, 12, 20, 60, 400):
        out.append(("chk/depth-%d" % n, one("".join("def f%d\n  f%d\n" % (k, k + 1) for k in range(n)) + "def f%d\n  tr.add 1\n" % n)))
        out.append(("chk/depth-rev-%d" % n, one("def f%d\n  tr.add 1\n" % n + "".join("def f%d\n  f%d\n" % (k, k + 1) for k in reversed(range(n))))))
    out.append(("chk/depth-fork", one("def l1\n  tr.add 1\ndef l2\n  l1\ndef l3\n  l2\n  l1\ndef l4\n  l1\n  l3\n  l2\ndef l5\n  l2\n  l4\n"
                                       "def l6\n  l3\n  l5\ndef l7\n  l5\n  l6\ndef l8\n  l6\n  l7\ndef l9\n  l7\n  l8\n  l6\n")))
    for n in (2, 3, 7, 30, 300, 450):
        out.append(("chk/ring-%d" % n, one("".join("def r%d\n  r%d\n" % (k, (k + 1) % n) for k in range(n)))))
        out.append(("chk/ring-rev-%d" % n, one("".join("def r%d\n  r%d\n" % (k, (k + 1) % n) for k in reversed(range(n))))))
        out.append(("chk/ring-tail-%d" % n, one("".join("def r%d\n  r%d\n  tr.add 1\n" % (k, (k + 1) % n) for k in range(n)) + "def top\n  r0\n")))
    out.append(("chk/scc-chords", one("def a\n  b\n  c\ndef b\n  c\n  d\ndef c\n  a\n  d\ndef d\n  b\n  e\ndef e\n  f\ndef f\n  e\n  a\n")))
    out.append(("chk/scc-order", one("def a\n  c\n  b\ndef b\n  a\ndef c\n  d\n  a\ndef d\n  c\n  b\ndef z\n  z\n  a\n")))
    out.append(("chk/self", one("def a\n  a\ndef b\n  a\n")))
    out.append(("chk/needs-chain", one("def f0\n  nosuch\n" + "".join("def f%d\n  f%d\n" % (k, k - 1) for k in range(1, 60)))))
    out.append(("chk/needs-order", one("".join("def f%d\n  f%d\n" % (k, k - 1) for k in reversed(range(1, 40))) + "def f0\n  nosuch\n")))
    out.append(("chk/needs-first", one("def a\n  nosuch\ndef b\n  nosuch\ndef c\n  b\n  a\ndef d\n  a\n  b\n")))
    # the cost of a callee depends on the arguments: memo and the product of the call sites
    out.append(("chk/memo-fanout", one('def leaf | n\n  l 1 n: tr.add i\n' + "".join(
        "def m%d | n\n  leaf | n\n  leaf | 3\n  leaf | n\n  m%d | 5\n" % (k, k + 1) if k < 12 else "def m%d | n\n  leaf | n\n" % k for k in range(13))
        + 'def top\n  menu "T" >> "X"\n  m0 | 1\n  m0 | 2\n')))
    return out


def fam_page():
    """`page "LABEL"` functions: the label (length, characters), the duplicate and the misplaced declaration, params without
    a menu and the cap of 6, no argument, the card / slot / pool ops refused directly and through the calls (first op rule of
    tools/otype.py Program.pass_page), the table (host bit PAGE, the label after the menu list in the pool, the aux tail after
    the every/skip counters)."""
    out = []
    P6 = "".join("  param p%d %d 0 9\n" % (k, k) for k in range(6))
    for i, t in enumerate((
            'def f\n  page "EUCL"\n  tr.add 1\n',
            'def f\n  page "A"\n  tr.add 1\n',
            'def f\n  page "12345678"\n  tr.add 1\n',
            'def f\n  page "123456789"\n  tr.add 1\n',
            'def f\n  page ""\n  tr.add 1\n',
            'def f\n  page "A B ~!"\n  tr.add 1\n',
            'def f\n  page "' + "caf\xe9" + '"\n  tr.add 1\n',
            'def f\n  page "\x80\x81"\n  tr.add 1\n',
            'def f\n  page "a\tb"\n  tr.add 1\n',
            'def f\n  page "' + "\xe9" * 12 + '"\n  tr.add 1\n',
            'def f\n  page\n  tr.add 1\n',
            'def f\n  page 5\n  tr.add 1\n',
            'def f\n  page EUCL\n  tr.add 1\n',
            'def f\n  page "A" "B"\n  tr.add 1\n',
            'def f\n  page "A" >> "B"\n  tr.add 1\n',
            'def f\n  page "A"\n  page "B"\n  tr.add 1\n',
            'def f\n  page "A"\n  page ""\n  tr.add 1\n',
            'def f\n  page "A"\n  tr.add 1\n  page "B"\n',
            'def f\n  tr.add 1\n  page "A"\n',
            'def f\n  if 1:\n    page "A"\n  tr.add 1\n',
            'def f\n  page "A":\n    tr.add 1\n',
            'def f\n  page "A"\n',
            'def f | a\n  page "A"\n  tr.add a\n',
            'def f | a b\n  page "A"\n  tr.add a\n  tr.add b\n',
            'def f | a\n  page ""\n  page "B"\n  tr.add a\n',
            'def page\n  tr.add 1\n',
            'const page 3\ndef f\n  tr.add page\n',
            'def f | page\n  tr.add page\n',
            'def f\n  param page 1 0 2\n  tr.add 1\n',
            'def f\n  page "A"\n  PAGE "B"\n  tr.add 1\n',
            'def f\n  PAGE "AbC"\n  tr.add 1\n')):
        out.append(("page/label-%d" % i, one(t)))
    for n in range(0, 10):
        out.append(("page/params-%d" % n, one('def f\n  page "P"\n' + "".join("  param q%d 1 0 5\n" % k for k in range(n)) + "  tr.add 1\n")))
        out.append(("page/params-late-%d" % n, one('def f\n' + "".join("  param q%d 1 0 5\n" % k for k in range(n)) + '  page "P"\n  tr.add 1\n')))
        out.append(("page/params-menu-%d" % n, one('def f\n  menu "A" >> "B"\n  page "P"\n' + "".join("  param q%d 1 0 5\n" % k for k in range(n)) + "  tr.add 1\n")))
        out.append(("page/params-nopage-%d" % n, one('def f\n  menu "A" >> "B"\n' + "".join("  param q%d 1 0 5\n" % k for k in range(n)) + "  tr.add 1\n")))
        out.append(("page/params-brick-%d" % n, one('def f\n' + "".join("  param q%d 1 0 5\n" % k for k in range(n)) + "  tr.add 1\n")))
    out.append(("page/six-args", one('def f | a\n  page "P"\n' + P6 + "  tr.add a\n")))
    out.append(("page/dup-param", one('def f\n  page "P"\n  param a 1 0 5\n  param a 2 0 5\n  tr.add 1\n')))
    out.append(("page/with-confirm", one('def f\n  page "P"\n  confirm\n  confirm "SURE?"\n  param p 1 0 5\n  tr.add 1\n')))
    out.append(("page/destructive", one('def f\n  page "P"\n  tr.clr\n  param p 1 0 5\n  tr.add 1\n')))
    out.append(("page/menu-and-page", one('def f\n  menu "A" >> "B"\n  menu "C" >> "D"\n  page "PG"\n  param p 1 0 5\n  tr.add p\n')))
    out.append(("page/same-as-param", one('def f\n  page "PL1"\n  param pl1 1 0 5\n  param pl2 1 0 5\n  tr.add pl1\n')))
    out.append(("page/same-as-menu", one('def f\n  menu "A" >> "B"\n  page "A"\n  tr.add 1\n')))
    out.append(("page/counters", one('def f\n  page "CTR"\n  param p 1 0 5\n  l 1 4:\n    every 2: tr.add i\n    skip 3: tr.add 1\n')))
    out.append(("page/shared-label", one('def f\n  page "SAME"\n  tr.add 1\ndef g\n  page "SAME"\n  tr.add 2\n')))
    out.append(("page/two-files", S(("A.OTY", 'def f\n  page "ONE"\n  param p 1 0 5\n  tr.add p\n'),
                                    ("B.OTY", 'def g\n  page "TWO"\n  tr.add 2\ndef h\n  menu "A" >> "B"\n  tr.add 3\n'))))
    out.append(("page/many", one("".join('def f%d\n  page "PG%d"\n  param p 1 0 5\n  tr.add p\n' % (k, k) for k in range(130)))))
    out.append(("page/many-plain", one("".join('def f%d\n  page "PG%d"\n  tr.add 1\n' % (k, k) for k in range(300)))))
    # the card / slot / pool ops, directly and through calls: the first one (lowest line; direct before called) is named
    IO = ('dir "AUDIO"', "dir \"AUDIO\" deep", 'ext wav', 'match "K*"', "a file.len", "a file.rnd", "a file.pick", "a file.at 1",
          "a static.free 1", "a static.firstfree", "slots.rnd 3", 'static.load 1 "AUDIO/X.WAV"', "static.clr 1", "slots.clr", "slots.add 1")
    for k, op in enumerate(IO):
        out.append(("page/io-%d" % k, one('def f\n  page "P"\n  tr.add 1\n  %s\n  tr.add 2\n' % op)))
    out.append(("page/io-two", one('def f\n  page "P"\n  static.clr 1\n  dir "A"\n  a file.len\n  tr.add 2\n')))
    out.append(("page/io-same-line", one('def f\n  page "P"\n  tr.add file.len static.free 1\n')))
    out.append(("page/io-in-block", one('def f\n  page "P"\n  l 1 4:\n    if rnd 2:\n      static.clr 1\n  tr.add 1\n')))
    out.append(("page/io-in-modifier", one('def f\n  page "P"\n  prob file.len: tr.add 1\n')))
    out.append(("page/io-setter", one('def f\n  page "P"\n  l 1 file.len: tr.add i\n')))
    out.append(("page/ok-reads", one('def f\n  page "P"\n  l 1 pat.len:\n    if has.trig i: tr.del i\n  a trk.cur\n  b has.lock 1\n  tr.add 1\n')))
    out.append(("page/ok-ui", one('def f\n  page "P"\n  msg "HI"\n  trk 2:\n    tr.add 1\n')))
    out.append(("page/via-1", one('def cardx\n  dir "AUDIO"\ndef f\n  page "P"\n  tr.add 1\n  cardx\n')))
    out.append(("page/via-before", one('def cardx\n  dir "AUDIO"\ndef f\n  page "P"\n  cardx\n  tr.add 1\n')))
    out.append(("page/via-3", one('def fc\n  static.clr 1\ndef fb\n  fc\ndef fa\n  fb\ndef f\n  page "P"\n  tr.add 1\n  fa\n')))
    out.append(("page/via-direct-first", one('def cio\n  static.clr 1\ndef f\n  page "P"\n  cio\n  dir "X"\n  tr.add 1\n')))
    out.append(("page/via-order", one('def ok\n  tr.add 1\ndef c1\n  static.clr 1\ndef c2\n  static.clr 1\n'
                                      'def f\n  page "P"\n  ok\n  c2\n  c1\n')))
    out.append(("page/via-twice", one('def cio\n  static.clr 1\ndef f\n  page "P"\n  cio\n  tr.add 1\n  cio\n')))
    out.append(("page/via-diamond", one('def dio\n  slots.add 1\ndef bio\n  dio\ndef cio\n  dio\ndef f\n  page "P"\n  bio\n  cio\n')))
    out.append(("page/via-args", one('def cio | n\n  static.clr n\ndef f\n  page "P"\n  cio | 1\n')))
    out.append(("page/via-value", one('def v\n  ret file.len\ndef f\n  page "P"\n  tr.add v\n')))
    out.append(("page/via-page", one('def cio\n  static.clr 1\ndef g\n  page "G"\n  cio\ndef f\n  page "P"\n  g\n')))
    out.append(("page/via-page-ok", one('def g\n  page "G"\n  tr.add 1\ndef f\n  page "P"\n  g\n  tr.add 2\n')))
    out.append(("page/via-menu", one('def cio\n  menu "A" >> "B"\n  static.clr 1\ndef f\n  page "P"\n  cio\n')))
    out.append(("page/via-cycle", one('def aio\n  bio\n  static.clr 1\ndef bio\n  aio\ndef f\n  page "P"\n  aio\n  tr.add 1\n')))
    out.append(("page/via-cycle-ok", one('def aio\n  bio\ndef bio\n  aio\ndef f\n  page "P"\n  aio\n  tr.add 1\n')))
    out.append(("page/self-cycle", one('def f\n  page "P"\n  f\n  static.clr 1\n')))
    out.append(("page/via-cycle-callee", one('def xio\n  dir "A"\ndef aio\n  xio\n  bio\ndef bio\n  aio\ndef f\n  page "P"\n  aio\n')))
    out.append(("page/via-refused", one('def bad\n  nosuch\n  static.clr 1\ndef f\n  page "P"\n  bad\n')))
    out.append(("page/via-refused-deep", one('def bad\n  static.clr 1\n  tr.add "s"\ndef m\n  bad\ndef f\n  page "P"\n  m\n')))
    out.append(("page/via-args-bad", one('def cio | n\n  static.clr 1\ndef f\n  page "P"\n  cio | 1 2\n')))
    out.append(("page/via-unknown", one('def f\n  page "P"\n  nosuch\n  static.clr 1\n')))
    out.append(("page/via-long-chain", one("".join("def c%d\n  c%d\n" % (k, k + 1) for k in range(30)) + "def c30\n  static.clr 1\n"
                                            'def f\n  page "P"\n  c0\n')))
    out.append(("page/via-long-chain-rev", one("def c30\n  static.clr 1\n" + "".join("def c%d\n  c%d\n" % (k, k + 1) for k in reversed(range(30)))
                                               + 'def f\n  page "P"\n  c0\n')))
    out.append(("page/via-many", one("def cio\n  dir \"X\"\n" + "".join('def f%d\n  page "P%d"\n  cio\n' % (k, k) for k in range(40)))))
    out.append(("page/via-in-brick-only", one('def cio\n  dir "X"\ndef g\n  cio\ndef f\n  page "P"\n  tr.add 1\n')))
    out.append(("page/needs", one('def bad\n  nosuch\ndef f\n  page "P"\n  bad\n')))
    out.append(("page/called-by-brick", one('def p\n  page "P"\n  tr.add 1\ndef g\n  p\n  tr.add 2\ndef h\n  menu "A" >> "B"\n  g\n')))
    out.append(("page/refused-callers", one('def p\n  page "P"\n  static.clr 1\ndef g\n  p\ndef h\n  menu "A" >> "B"\n  g\n')))
    out.append(("page/not-in-profile", one('def f\n  page "P"\n  trk.init 1\n  tr.add 1\n')))
    out.append(("page/menu-budget", one('def f\n  page "P"\n  menu "A" >> "B"\n' + "  l 1 64:\n" * 0 + "  l 1 3000:\n    l 1 3000: tr.add i\n")))
    out.append(("page/long-body", one('def f\n  page "P"\n  param p 1 0 5\n' + "  tr.add 1\n" * 200)))
    return out


def fam_confirm():
    """`confirm "TEXT"` in the table (spec 14.1): flag bit 2, the text's pool offset after the page label in the aux data, the
    text after the page label in the pool; the first non-empty text wins; texts shared with names and between functions."""
    out = []
    for i, t in enumerate((
            'def f\n  menu "A" >> "B"\n  confirm "SURE?"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm ""\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm ""\n  confirm "LATE"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm "ONE"\n  confirm "TWO"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm\n  confirm "AFTER"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm "' + "X" * 116 + '"\n  tr.add 1\n',      # the longest a 128-character line holds
            'def f\n  menu "A" >> "B"\n  confirm "' + "X" * 117 + '"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm "caf\xe9"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm "a\tb"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm 5\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm "A" "B"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm "A" 5\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  tr.add 1\n  confirm "LATE?"\n',
            'def f\n  menu "A" >> "B"\n  CONFIRM "Case Kept ~!"\n  tr.add 1\n',
            'def f\n  menu "CLEAR" >> "ALL"\n  confirm "ALL"\n  tr.clr\n',
            'def f\n  menu "A" >> "B"\n  tr.clr\n',
            'def f\n  confirm "BRICK?"\n  tr.add 1\n',
            'def f\n  page "P"\n  confirm "IGNORED"\n  param p 1 0 5\n  tr.add p\n',
            'def f\n  menu "A" >> "B"\n  page "PG"\n  confirm "BOTH?"\n  param p 1 0 5\n  l 1 4:\n    every 2: tr.add i\n',
            'def f | a\n  confirm "ARG?"\n  tr.add a\ndef g\n  menu "A" >> "B"\n  f | 2\n',
            'def f\n  menu "A" >> "B"\n  confirm "P"\n  param p 1 0 5\n  tr.add p\n',
            'def f\n  menu "A" >> "B"\n  confirm "A.OTY"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm "S"\n  msg "S"\n',
            'def f\n  menu "A" >> "B"\n  confirm "SAME"\n  tr.add 1\ndef g\n  menu "A" >> "C"\n  confirm "SAME"\n  tr.add 2\n',
            'def f\n  menu "A" >> "B"\n  confirm "OK"\n  nosuch\n',
            'def f\n  menu "A" >> "B"\n  if 1:\n    confirm "X"\n  tr.add 1\n',
            'def f\n  menu "A" >> "B"\n  confirm "X":\n    tr.add 1\n')):
        out.append(("confirm/text-%d" % i, one(t)))
    out.append(("confirm/many", one("".join('def f%d\n  menu "M" >> "A%d"\n  confirm "SURE %d?"\n  tr.add 1\n' % (k, k, k)
                                            for k in range(120)))))
    out.append(("confirm/two-files", S(("A.OTY", 'def f\n  menu "A" >> "B"\n  confirm "FIRST"\n  tr.add 1\n'),
                                       ("B.OTY", 'def g\n  menu "A" >> "C"\n  confirm "FIRST"\n  tr.clr\n'))))
    return out


def fam_random(n=420):
    """Random programs (modules/otype/progen.py): the checker's paths and the code generator's on programs nobody wrote."""
    from modules.otype.progen import ProgGen
    return [("rand/%03d" % k, one(ProgGen(random.Random("%d/rand/%d" % (SEED, k))).source())) for k in range(n)]


def families():
    out = []
    for f in (fam_nesting, fam_chains, fam_nfuncs, fam_table, fam_caps, fam_lines, fam_numbers, fam_stack,
              fam_counters, fam_x9, fam_codelen, fam_cost, fam_nonascii, fam_labels, fam_misc, fam_fixes, fam_checker, fam_page, fam_confirm, fam_random):
        out.extend(f())
    return out


# ============================================================================ coverage of the messages
TEMPLATE_CALLS = {"err", "warn", "file_err", "ParseError", "LexError", "CompileError", "OtbError", "OtbDiag", "msg_at",
                  "diag"}
TEMPLATE_FUNCS = {"name_problem", "type_msg"}
# messages that no source can produce (or no compile of sources), with the reason
NOT_REACHABLE = (
    ("file refused (otype %d)", "a hidden diagnostic: it marks the functions of an `otype 2` file refused and is never printed"),
    ("internal:", "internal consistency check of the compiler: a bug, never a user error"),
    ("bad pool reference", "decoder of a table (otb.parse): not in the compile path"),
    ("not an OTB table", "decoder of a table (otb.parse): not in the compile path"),
    ("OTB format", "decoder of a table (otb.parse): not in the compile path"),
    ("OTB size field", "decoder of a table (otb.parse): not in the compile path"),
    ("code ends inside", "decoder of a table (otb.parse): not in the compile path"),
    ("unknown opcode", "decoder of a table (otb.parse): not in the compile path"),
    ("runs past the end of the code", "decoder of a table (otb.parse): not in the compile path"),
    ("call of function %d, the table has", "decoder of a table (otb.parse): not in the compile path"),
    ("function name longer than", "a function name is limited to 16 characters by the checker, so the compiler never sees longer"),
)


def extract_templates():
    """{template: [(file, line)]} -- every message template (format string) of tools/otype.py and otb.py."""
    out = collections.OrderedDict()
    for path in (os.path.join(ROOT, "tools", "otype.py"), os.path.join(ROOT, "modules", "otype", "otb.py")):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        roots = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if nm in TEMPLATE_CALLS:
                    roots.extend(node.args)
                elif (nm == "append" and isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Attribute)
                      and fn.value.attr == "errs"):          # li.errs.append("line too long ...")
                    roots.extend(node.args)
            elif isinstance(node, ast.Assign):
                tg = node.targets[0]
                if isinstance(tg, ast.Name) and tg.id in ("msg", "stop"):           # msg = ("... at column 1 ...")
                    roots.append(node.value)
                elif isinstance(tg, ast.Subscript) and getattr(tg.value, "id", "") == "bad":   # bad[f] = (line, msg)
                    roots.append(node.value)
            elif isinstance(node, ast.FunctionDef) and node.name in TEMPLATE_FUNCS:
                for r in ast.walk(node):
                    if isinstance(r, ast.Return) and r.value is not None:
                        roots.append(r.value)
        for r in roots:
            for c in ast.walk(r):
                if (isinstance(c, ast.Constant) and isinstance(c.value, str)
                        and re.search(r"[a-z]{3}", c.value) and c.value not in ("-", "error", "warning")):
                    if re.fullmatch(r"[A-Z_]+", c.value) or c.value in ("INT", "STR", "LIST", "COND"):
                        continue
                    out.setdefault(c.value, []).append((os.path.basename(path), c.lineno))
    # fragments of a message built in pieces: drop the pieces that are only part of a longer template
    return out


def template_regex(t):
    parts = re.split(r"(%[0-9]*[sdrx])", t)
    rx = ""
    for p in parts:
        if re.fullmatch(r"%[0-9]*[sr]", p):
            rx += r".*?"
        elif re.fullmatch(r"%[0-9]*[dx]", p):
            rx += r"-?[0-9a-f]+"
        else:
            rx += re.escape(p)
    return re.compile(rx + r"\Z", re.S)


MSG_LINE = re.compile(r"^(.*?):(\d+): ([^ ]*): (error|warning): (.*)$")


def coverage_text(msgs, total_entries):
    """msgs: {message text: first entry id}.  The coverage report (text)."""
    templates = extract_templates()
    rxs = [(t, template_regex(t)) for t in templates]
    lit = lambda t: len(re.sub(r"%[0-9]*[sdrx]", "", t))
    hit = collections.OrderedDict((t, []) for t in templates)
    unmatched = []
    for m, eid in msgs.items():
        cands = [t for t, rx in rxs if rx.match(m)]
        if not cands:
            unmatched.append((m, eid))
            continue
        best = max(cands, key=lit)
        hit[best].append((m, eid))
    reached = [t for t in hit if hit[t]]
    # a template that is only a fragment of a longer message is not a message of its own
    def fragment(t):
        return any(t != o and t in o for o in templates)
    miss = [t for t in templates if not hit[t]]
    lines = ["OType diagnostic messages reached by the corpus (%d entries)" % total_entries, ""]
    real = [t for t in templates if not (fragment(t) and not hit[t])]
    lines.append("distinct message formats: %d, reached: %d, not reached: %d" % (
        len(real), len([t for t in real if hit[t]]), len([t for t in real if not hit[t]])))
    lines.append("")
    lines.append("== reached (format, source line, occurrences, first example: entry id, message)")
    for t in real:
        if hit[t]:
            where = ",".join("%s:%d" % w for w in templates[t][:2])
            m, eid = hit[t][0]
            lines.append("%5d  %s  [%s]\n         e.g. %s: %s" % (len(hit[t]), t, where, eid, m[:150]))
    lines.append("")
    lines.append("== not reached")
    for t in real:
        if not hit[t]:
            reason = next((r for k, r in NOT_REACHABLE if k in t), "no source found yet")
            where = ",".join("%s:%d" % w for w in templates[t][:2])
            lines.append("  %s  [%s]\n         reason: %s" % (t, where, reason))
    if unmatched:
        lines.append("")
        lines.append("== messages that match no extracted format (%d distinct): check extract_templates" % len(unmatched))
        for m, eid in unmatched[:60]:
            lines.append("  %s: %s" % (eid, m[:150]))
    return "\n".join(lines) + "\n", len(real), len([t for t in real if hit[t]])


# ============================================================================ generation
def build_inputs(fast=False):
    seeds = seeds_files() + seeds_ast()
    if not fast:
        seeds += seeds_runtime()
    seeds = dedupe(seeds)
    inputs = [(n, s) for n, s in seeds]
    fam = dedupe(families() if not fast else fam_misc() + fam_nonascii()[:40] + fam_caps()[:6])
    seen = set(tuple(s) for _n, s in inputs)
    for n, s in fam:
        if tuple(s) not in seen:
            seen.add(tuple(s))
            inputs.append((n, s))
    muts = []
    per = MUTANTS_PER_SEED if not fast else 1
    for name, srcs in seeds:
        rng = random.Random("%d/%s" % (SEED, name))
        for k in range(per):
            r = mutate(rng, srcs)
            if r is None:
                continue
            kind, new = r
            key = tuple(new)
            if key in seen:
                continue
            seen.add(key)
            muts.append(("mut/%s/%d/%s" % (name, k, kind), new))
    out, ids = [], set()
    for eid, srcs in inputs + muts:           # ids are unique (a suffix ~N when a name repeats)
        k, base = 1, eid
        while eid in ids:
            k += 1
            eid = "%s~%d" % (base, k)
        ids.add(eid)
        out.append((eid, srcs))
    return out


def generate(out_dir, fast=False, log=print):
    t0 = time.time()
    os.makedirs(out_dir, exist_ok=True)
    inputs = build_inputs(fast)
    log("%d inputs (%.0fs)" % (len(inputs), time.time() - t0))
    entries, crashes = [], []
    msgs = collections.OrderedDict()
    for n, (eid, srcs) in enumerate(inputs):
        for prof in PROFILES:
            try:
                rep, otb = oterr.reference(srcs, prof)
            except Exception as e:           # a crash of the Python reference is a bug to fix, not to hide
                crashes.append((eid, prof, repr(e)))
                continue
            entries.append(Entry(eid, prof, srcs, rep, otb))
            for line in rep.split("\n"):
                m = MSG_LINE.match(line)
                if m:
                    msgs.setdefault(m.group(5), "%s|%s" % (eid, prof or "none"))
        if n % 500 == 499:
            log("  %d/%d (%.0fs)" % (n + 1, len(inputs), time.time() - t0))
    path = os.path.join(out_dir, "corpus.bin.gz")
    write_corpus(path, entries)
    cov, ntot, nhit = coverage_text(msgs, len(entries))
    with open(os.path.join(out_dir, "coverage.txt"), "w") as fh:
        fh.write(cov)
    stats = stats_text(entries, crashes)
    with open(os.path.join(out_dir, "stats.txt"), "w") as fh:
        fh.write(stats)
    log(stats)
    log("coverage: %d/%d message formats reached" % (nhit, ntot))
    log("wrote %s (%d bytes), %.0fs" % (path, os.path.getsize(path), time.time() - t0))
    return entries, crashes


def stats_text(entries, crashes=()):
    n = len(entries)
    err = sum(1 for e in entries if has_error(e.report))
    lines = ["entries: %d  with at least one error: %d (%.1f%%)" % (n, err, 100.0 * err / max(1, n))]
    for prof in PROFILES:
        es = [e for e in entries if e.profile == prof]
        er = sum(1 for e in es if has_error(e.report))
        lines.append("  profile %-4s %d entries, %d with errors (%.1f%%), %d with a table, %d without" % (
            prof or "none", len(es), er, 100.0 * er / max(1, len(es)), sum(1 for e in es if e.otb),
            sum(1 for e in es if not e.otb)))
    kinds = collections.Counter(e.id.split("/")[0] for e in entries)
    lines.append("by origin: " + ", ".join("%s=%d" % kv for kv in sorted(kinds.items())))
    lines.append("sources: %d bytes in total, biggest set %d bytes" % (
        sum(len(d) for e in entries for _l, d in e.sources), max((sum(len(d) for _l, d in e.sources) for e in entries), default=0)))
    if crashes:
        lines.append("CRASHES of the reference: %d" % len(crashes))
        for c in crashes[:20]:
            lines.append("  %s %s %s" % c)
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "otype", "comp", "corpus"))
    ap.add_argument("--fast", action="store_true", help="skip the run of the test suites (fewer seeds)")
    ap.add_argument("--read", help="print the statistics of an existing corpus file")
    a = ap.parse_args(argv)
    if a.read:
        print(stats_text(list(iter_entries(a.read))), end="")
        return 0
    _entries, crashes = generate(a.out, a.fast)
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main())
