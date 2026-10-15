"""The C compiler (firmware/c/otc_comp*.c) against the Python reference.
From the repo root:

    python3 -m unittest tools/test_otype_ccomp.py            # the whole corpus (~40 s)
    QUICK=1 python3 -m unittest tools/test_otype_ccomp.py    # a deterministic subsample (every 8th entry / source set)

Report (class TestReport*, TestSabotageCheck, TestX7, TestLazyPaths): the REPORT text of the C (`otc_run` + `otc_report`)
must equal `oterr.reference(sources, profile)[0]` character for character on every entry of the corpus, both profiles, and
the per-function numbers of `otc_check_dump` (cost, destructive flag, code size, line table, counters, stack) must equal
modules/otype/check_dump.py.  Table (class TestTableCorpus, TestSabotageGen, TestFuzz): the OTB TABLE of the C (`otc_compile`) must equal `oterr.reference(...)[1]`
byte for byte on every entry of the corpus and on the random programs (with the report, through the same call); every non-empty
table the C makes is loaded by the C VM (`otc_vm_load`) and decoded by `otb.parse`; -DOTC_SABOTAGE_GEN[=1|2|3|4] must be caught (3: the page label before the menu list; 4: the confirm text before the
page label).
Must-fail: builds with -DOTC_SABOTAGE_CHECK[=1|2|3|4] (=4: the last op of a page function named; call depth 9 instead of 8; the costs of
an if / elif chain added instead of the largest; the order of emission reversed) have to be caught.  The front end is compared as follows:

What is compared: the canonical dump of modules/otype/front_dump.py (the Python Program at the front-end boundary:
loading of the sources, pass_names, the body parser; functions, consts, params, menus, every statement and
expression tree, the diagnostics of this phase in emission order) with the text `otc_front_dump` writes from the C
state (modules/otype/ccomp.py wraps the Mac library). The corpus is the differential one (out/otype/comp/corpus; regenerated
with --fast if it is missing); every source set is compared once, the two profiles share it (the profile does not
matter before the checker).  Must-fail: a library built with -DOTC_SABOTAGE_FRONT (the 128-character line limit off
by one) has to be caught.
"""
import ctypes
import hashlib
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import otype  # noqa: E402
from modules.otype import build_comp, ccomp, check_dump, corpus, cvm, front_dump, gen_ops_c, oterr, otb  # noqa: E402
from modules.otype.progen import ProgGen  # noqa: E402
from modules.otype import ops as O  # noqa: E402

QUICK = bool(os.environ.get("QUICK"))
CORPUS_DIR = os.path.join(ROOT, "out", "otype", "comp", "corpus")
CORPUS = os.path.join(CORPUS_DIR, "corpus.bin.gz")
LABEL = b"/SCRIPTS/T.OTY"
_CC = {}


def cc(defines=()):
    key = tuple(defines)
    if key not in _CC:
        _CC[key] = ccomp.CComp(ccomp.build(defines))
    return _CC[key]


_VM = []


def vm_lib():
    """The host C VM (otype_unit.c + host_shim.c), for the load check of the tables."""
    if not _VM:
        out = os.path.join(ROOT, "out", "otype", "comp")
        os.makedirs(out, exist_ok=True)
        lib = os.path.join(out, "libotc_vm_load%s" % ccomp.SO)
        csrc = os.path.join(ROOT, "firmware", "c")
        r = subprocess.run(["cc", "-O2", "-shared", "-fPIC", "-I", csrc, "-o", lib, os.path.join(csrc, "otype_unit.c"),
                            os.path.join(csrc, "host_shim.c")], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr)
        _VM.append(lib)
    return _VM[0]


def src(text, label=LABEL):
    return [(label, text if isinstance(text, bytes) else text.encode("latin-1"))]


def unique_sets():
    """[(id, sources)]: every distinct source set of the corpus once, in corpus order."""
    if not os.path.exists(CORPUS):
        subprocess.run([sys.executable, "-m", "modules.otype.corpus", "--out", CORPUS_DIR, "--fast"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL)
    seen, out = set(), []
    for e in corpus.iter_entries(CORPUS):
        h = hashlib.sha1(repr(e.sources).encode()).digest()
        if h not in seen:
            seen.add(h)
            out.append((e.id, e.sources))
    return out


def first_diff(a, b):
    la, lb = a.split("\n"), b.split("\n")
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<end>"
        y = lb[i] if i < len(lb) else "<end>"
        if x != y:
            return "line %d\n  python: %s\n  c:      %s" % (i + 1, x[:300], y[:300])
    return "(equal)"


class TestHeader(unittest.TestCase):
    def test_generated_headers_are_up_to_date(self):
        with open(gen_ops_c.REG_HEADER) as fh:
            self.assertEqual(fh.read(), gen_ops_c.render_reg(), "otc_opreg.h is stale: python3 modules/otype/gen_ops_c.py")
        with open(gen_ops_c.HEADER) as fh:
            self.assertEqual(fh.read(), gen_ops_c.render(), "otc_ops.h is stale: python3 modules/otype/gen_ops_c.py")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "modules", "otype", "gen_ops_c.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_registry_names_are_all_there(self):
        text = open(gen_ops_c.REG_HEADER).read()
        self.assertIn("#define OTC_NREG %d" % len(O.ALL_OPS), text)
        self.assertIn("#define OTC_NNAMES %d" % len(O.OPS), text)


# Small sources: each one exercises a corner of the front end; the C dump must equal the Python one, and the
# `expect` strings (when given) must be in it, so that the comparison cannot be vacuous.
CASES = [
    ("clean", "otype 1\ndef main\n  menu \"A\" >> \"B\"\n  tr.add 1\n", ["TR.ADD", "diags 0"]),
    ("empty file", "", ["files 1"]),
    ("renamed and removed operators",
     "def f\n  slot.load 1 1\n  a slot.free 1\n  a slot.firstfree\n  slot.clr 1\n  pool.fill\n  pool.rnd\n  pool.clr\n"
     "  pool.shuffle\n  scn.gen\n  lfo.rnd\n  fx.rnd\n  lfod.rnd\n  lfod.inv\n",
     ["slot.load is now static.load", "slot.clr is now static.clr", "pool.fill was removed: write the loop (cookbook",
      "Shuffle the Static slots", "Invert the LFO designer"]),
    ("only comments", "# x\n\n# y\n", []),
    ("tabs and cr", "def f\r\n\tif 1:\r  tr.add 1\r\n", ["tab in indentation"]),
    ("line too long", "def f\n  tr.add 1 # " + "x" * 130 + "\n", ["line too long (", "> 128"]),
    ("line of 128", "def f\n  tr.add 1" + " " * 118 + "\n", ["diags 0"]),
    ("line of 129", "def f\n  tr.add 1" + " " * 119 + "\n", ["line too long (129 > 128"]),
    ("control char", "def f\n  tr.add 1\x01\n", ["0x01"]),
    ("high byte", "def f\n  tr.add 1\xe9\n", ["0xe9"]),
    ("high byte in comment", "def f\n  tr.add 1 # \xe9\n", ["diags 0"]),
    ("number range", "def f\n  a 32768\n  a -32769\n  a -32768\n  a 0x7FFF\n  a 0x8000\n  a 99999999999\n",
     ["out of 16-bit range"]),
    ("hex and signs", "def f\n  a -0x10\n  a 0X0000001\n  a 000000000000000000000005\n", ["diags 0"]),
    ("lists", "const l {1 2 0x3 -4}\ndef f\n  a at {1 2} 2\n  a at {} 1\n  a at {1 x} 1\n  a at {1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17} 1\n",
     ["empty list", "numbers only", "too long"]),
    ("conditions", "def f\n  tr.add 1\n  cond.set 1 50%\n  cond.set 1 050%\n  cond.set 1 1:4\n  cond.set 1 01:04\n  cond.set 1 fill\n  cond.set 1 !1st\n"
     "  cond.set 1 7%\n  cond.set 1 1:1\n  cond.set 1 5:3\n  cond.set 1 1:9\n", ["not a trig condition"]),
    ("strings", "def f\n  msg \"hi\"\n  msg \"unterminated\n  msg \"a\"b\n  msg \"" + "x" * 128 + "\"\n", ["unterminated string", "string too long"]),
    ("braces", "def f\n  a at } 1\n  a at {1\n", ["unexpected }", "unterminated list"]),
    ("otype lines", "otype 2\ndef f\n  tr.add 1\notype 1\notype\notype x\n", ["otype 2 is not supported", "must be the first line", "hidden" if False else "diag!"]),
    ("otype 0 and negative", "otype -3\ndef f\n  tr.add 1\ndef g\n  tr.add 1\n", ["diag!"]),
    ("const forms", "const a 1\nconst\nconst x\nconst x 1 2\nconst x y\nconst \"s\" 1\nconst ok \"str\"\nconst ok2 {1 2}\nconst ok3 50%\nconst mixed 07\nconst bad.name 1\nconst x\x01y 2\n", ["const takes a single", "has no value"]),
    ("const lex error twice", "const x 99999\ndef f\n  tr.add 1\n", ["out of 16-bit range"]),
    ("const dup and clash", "const k 1\nconst K 2\nconst g 3\ndef g\n  tr.add 1\ndef k\n  tr.add 1\n", ["duplicate const", "has the name of the function"]),
    ("def forms", "def\ndef f |\ndef g | p q p\ndef h x\ndef i | a | b\ndef j | a :\ndef k | 1x\ndef l | tr.add\ndef m | a b c d e\n  tr.add 1\n", ["expected '|'", "duplicate argument", "takes 5 arguments"]),
    ("def odd names", "def a\"b\ndef \"Q\"R | x\"Y z\n  tr.add 1\ndef | a\n", ["not a valid name"]),
    ("duplicate functions", "def f\n  tr.add 1\ndef F\n  tr.add 2\ndef f\n  tr.add 3\n", ["duplicate function name"]),
    ("args hide", "const c 1\ndef g\n  tr.add 1\ndef f | c g\n  tr.add c\n", ["hides the const", "has the name of a function"]),
    ("unused word and blind", "def f | x y\n  tr.add x\ndef g | x\n  tr.add 1\n  \"bad\n", ["used="]),
    ("col 1 junk", "tr.add 1\n  tr.add 1\nx = 1\ndef f\nfoo bar\n  tr.add 1\n", ["only def, const and otype start in column 1", "indented line outside"]),
    ("declarations", "def f | a\n  menu \"A\" >> \"B\"\n  menu \"LONGLEVEL\" >> \"LONG ACTION!\"\n  menu \"A\" >> \"B\" >> \"C\"\n  menu\n  menu \"A\"\n  menu 1\n  menu \"A\" \"B\"\n"
     "  confirm\n  confirm \"x\"\n  confirm 1\n  on trig\n  on foo\n  on\n  on 5\n  param p 5 0 10\n  param p 1 1 1\n  param q 5 6 10\n  param \"R\" 1 0 2\n  param \"r\" 1 0 2\n  param a 1 0 2\n  param tr.add 1 0 2\n  param z\n"
     "  tr.add 1\n  menu \"X\" >> \"Y\"\n  if 1:\n    param w 1 0 2\n", ["submenus", "host not available", "unknown host", "declaration"]),
    ("brick param", "def f\n  param p 1 0 2\n  tr.add p\n", ["a brick cannot declare param"]),
    ("param consts", "const lo 0\nconst hi 9\nconst s \"x\"\nconst bad 1 2\ndef f\n  menu \"A\" >> \"B\"\n  param p lo lo hi\n  param q 1 lo s\n  param r 1 lo bad\n  tr.add p\n", []),
    ("modifiers", "def f | n\n  if n: tr.add 1\n  elif n: tr.add 2\n  else: tr.add 3\n  prob 50%: tr.add 1\n  prob 50: tr.add 1\n  l 1 4: tr.add i\n  l 1: tr.add 1\n  each: tr.add i\n  every 2: tr.add 1\n  skip 3: tr.add 1\n  del 2: tr.add 1\n  if: tr.add 1\n  if 1 tr.add 1\n  if 1 else: tr.add 1\n  else: tr.add 1\n", ["expects a value", "del is not available"]),
    ("blocks", "def f\n  if 1:\n    tr.add 1\n   tr.add 2\n      tr.add 3\n  tr.add 4\n tr.add 5\n  if 1:\ndef g\n    tr.add 1\n  tr.add 2\n", ["empty block", "indentation does not match", "unexpected indentation"]),
    ("deep blocks", "def f\n" + "".join("%sif 1:\n" % (" " * (i + 1)) for i in range(11)) + " " * 12 + "tr.add 1\n", ["blocks nested deeper than 8"]),
    ("else chain", "def f\n  if 1:\n    tr.add 1\n  elif 1:\n    tr.add 1\n  else:\n    tr.add 1\n  else:\n    tr.add 1\n  tr.add 1\n  elif 1:\n    tr.add 1\n", ["without if"]),
    ("statements", "def f | x\n  a 1\n  i 2\n  x 1\n  x\n  c 1\n  k\n  tr.add\n  tr.add 1 2\n  tr.add 1 2 3 x y\n  add 1 2\n  nosuch 1\n  def\n  const\n  1\n  \"s\"\n  {1}\n  50%\n  :\n  if\n  break\n  stop\n  ret\n  ret 1\n  ret 1 2\n  trk 1 2 3\n  trk 1 2 3 4 5 6 7 8 9\n  trk cur\n  trk all\n  trk:\n  trk 1: tr.add 1\n  trk 1: steps all: tr.add 1\n  steps has.trig:\n  steps 1 4:\n  steps 1:\n  step 1: tr.add 1\n  seed 5\n  flip\n  flip 1\n  o 3\n  o.min 2\n  drunk\n  tr.add | 1\n  f | 1\n  f\n", ["expects", "does nothing"]),
    ("terms", "const c 5\nconst s \"x\"\ndef g | p q\n  tr.add p\n  tr.add q\ndef f | x\n  param pp 1 0 2\n  a x\n  a pp\n  a c\n  a s\n  a b\n  a j\n  a nosuch\n  a tr.add\n  a add 1\n  a + 1 2\n  a ? 1 2 3\n  a g | 1 2\n  a g | 1\n  a g 1 2\n  a pick {1 2}\n  a add | 1 2\n  a add 1\n  a add 1 2 3\n  a if\n  a on\n  a off\n  a 1 2\n  a :\n  a >>\n  a |\n  a (\n", []),
    ("user calls", "def zz\n  tr.add 1\ndef g | p q\n  tr.add p\ndef f\n  zz | 1\n  g\n  g | 1\n  g | 1 2\n  g | 1 2 3\n  tr.add zz\n  a zz\n  a g | 1 2\n  tr.add g | 1 2\n  tr.add g | 1 2 3\n", ["takes no argument", "takes 2 arguments", "expects 2 arguments"]),
    ("ext dir match", "def f\n  dir \"A\"\n  dir \"A\" deep\n  dir \"A\" nodeep\n  ext wav aif\n  ext wav mp3\n  ext\n  match \"a*\" \"b\"\n  match\n  match 1\n  dir\n  static.load 1 2\n", ["expected one of", "deep"]),
    ("kw and long names", "def " + "n" * 17 + "\n  tr.add 1\ndef " + "m" * 100 + " | " + "a" * 40 + "\n  tr.add 1\nconst " + "c" * 30 + " 5\n", ["name is too long"]),
    ("case insensitive", "DEF F | X\n  IF X: TR.ADD 1\n  Menu \"a\" >> \"b\"\nConst K 1\nOtype 1\n", []),
    ("steps predicates", "def f\n  steps has.trig: tr.add 1\n  steps has.lock\n  steps all\n  steps 1 2: tr.add 1\n  steps tr.add: tr.add 1\n  steps 1\n  steps\n", []),
    ("many args hidden", "def f | a1 a2 a3 a4 a5\n  tr.add 1\n", ["takes 5 arguments"]),
    ("long ids", "def f\n  a " + "q" * 300 + "\n  " + "q" * 300 + " 1\n  tr.add " + "z" * 200 + "\n", ["unknown name"]),
    ("colons", "def f\n  trk 1:\n    tr.add 1\n  trk 1: tr.add 1 :\n  tr.add 1:\n  tr.add 1 2:\n  a 1 :\n  a 1 : 2\n", []),
]


class TestUnit(unittest.TestCase):
    def check(self, text, expect=(), label=LABEL):
        s = src(text, label)
        a, b = front_dump.front_dump(s), cc().dump(s)
        self.assertEqual(cc().err, 0)
        self.assertEqual(a, b, first_diff(a, b))
        for e in expect:
            self.assertIn(e, a)
        return a

    def test_cases(self):
        for name, text, expect in CASES:
            with self.subTest(name):
                self.check(text, expect)

    def test_x2_caps(self):
        many = [(b"/SCRIPTS/F%02d.OTY" % i, b"def f%d\n  tr.add 1\n" % i) for i in range(70)]
        a, b = front_dump.front_dump(many), cc().dump(many)
        self.assertEqual(a, b, first_diff(a, b))
        self.assertIn("over 64 files", a)
        big = [(b"/SCRIPTS/A.OTY", b"def a1\n  tr.add 1\n" + b"#" * 60000 + b"\n"), (b"/SCRIPTS/B.OTY", b"x" * 6000),
               (b"/SCRIPTS/C.OTY", b"def c\n  tr.add 1\n")]
        a, b = front_dump.front_dump(big), cc().dump(big)
        self.assertEqual(a, b, first_diff(a, b))
        self.assertIn("over 65536 bytes", a)
        exact = [(b"/SCRIPTS/A.OTY", b"#" * 65536), (b"/SCRIPTS/B.OTY", b"")]
        a, b = front_dump.front_dump(exact), cc().dump(exact)
        self.assertEqual(a, b, first_diff(a, b))
        self.assertNotIn("not loaded", a)

    def test_several_files_and_labels(self):
        s = [(b"/SCRIPTS/B.OTY", b"const k 1\ndef f\n  tr.add 1\n"), (b"x\x01y\"z", b"const K 2\ndef F\n  tr.add 2\ndef k\n  tr.add 1\n"),
             (b"", b"def q | k\n  tr.add k\n")]
        a, b = front_dump.front_dump(s), cc().dump(s)
        self.assertEqual(a, b, first_diff(a, b))
        self.assertIn("duplicate", a)

    def test_examples(self):
        d = os.path.join(ROOT, "modules", "otype", "examples")
        files = sorted(f for f in os.listdir(d) if f.upper().endswith(".OTY"))
        every = []
        for f in files:
            with open(os.path.join(d, f), "rb") as fh:
                every.append((("/SCRIPTS/" + f.upper()).encode(), fh.read()))
            a, b = front_dump.front_dump(every[-1:]), cc().dump(every[-1:])
            self.assertEqual(a, b, f + "\n" + first_diff(a, b))
        a, b = front_dump.front_dump(every), cc().dump(every)
        self.assertEqual(a, b, first_diff(a, b))

    def test_front_is_a_prefix_of_the_checker(self):
        """The boundary is honest: the diagnostics of `front()` are the first ones, in order, of the complete check."""
        for name, text, _e in CASES:
            s = src(text)
            prog = front_dump.front(s)
            full = otype.check_sources([(LABEL.decode(), text)])
            n = len(prog.diags)
            self.assertEqual([str(d) for d in prog.diags], [str(d) for d in full.diags[:n]], name)


class TestLimits(unittest.TestCase):
    def test_workspace_size(self):
        self.assertEqual(cc().ws_size, 3 * 1024 * 1024)
        self.assertEqual(cc().ws_size % 8, 0)

    def test_expression_depth(self):
        ok = "def f\n  a " + "+ " * 40 + "1 " * 42 + "\n"          # nests 40: far over what 128 characters allow
        s = src(ok)
        a, b = front_dump.front_dump(s), cc().dump(s)
        self.assertEqual(a, b, first_diff(a, b))
        deep = src("def f\n  a " + "+ " * 70 + "1 " * 72 + "\n")     # 70 > OTC_EXPR_DEPTH: a deterministic stop
        self.assertEqual(cc().dump(deep), "OVERFLOW 2\n")
        self.assertEqual(cc().err, 2)
        # and the state is clean again afterwards
        self.assertEqual(cc().dump(s), a)

    def test_arena_overflow_is_an_error_not_a_truncation(self):
        small = cc(("OTC_COMP_ARENA_BYTES=65536",))
        self.assertEqual(small.ws_size, 65536)
        big = src("def f\n" + "  tr.add 1\n" * 5000)
        self.assertEqual(small.dump(big), "OVERFLOW 1\n")
        self.assertEqual(small.err, 1)
        fine = src("def f\n  tr.add 1\n")
        self.assertEqual(small.dump(fine), front_dump.front_dump(fine))     # the same library still works
        # the same source in the real arena
        a, b = front_dump.front_dump(big), cc().dump(big)
        self.assertEqual(a, b, first_diff(a, b))

    def test_worst_cases_of_the_caps(self):
        """Dense 64 KB sources that fit the arena (the corpus peak is far lower); peaks are printed by the corpus test."""
        cases = {"op lines": "def fn\n" + " o\n" * 21800, "dup defs": "def fn\n" * 10900,
                 "unknown names": "def fn\n" + " x\n" * 21800, "one long line": "def fn\n  a " + "1 " * 30000 + "\n"}
        for name, text in cases.items():
            with self.subTest(name):
                s = src(text.encode()[:65536])
                a, b = front_dump.front_dump(s), cc().dump(s)
                self.assertEqual(cc().err, 0)
                self.assertEqual(a, b, first_diff(a, b))


class TestColdFire(unittest.TestCase):
    def test_compiles_for_the_unit(self):
        from shutil import which
        if which("m68k-elf-gcc") is None:
            self.skipTest("no m68k-elf-gcc")
        import pathlib
        out = pathlib.Path(ROOT) / "out" / "otype" / "comp" / "otc_comp.s"
        sec, glob = build_comp.build(out, quiet=True)
        self.assertGreater(sec[".text"], 10000)
        self.assertEqual(sec.get(".data", 0) + sec.get(".bss", 0), 0)
        for sym in ("otc_front", "otc_run", "otc_report"):
            self.assertIn(sym, glob)
        nod, _g = build_comp.build(out.with_name("otc_comp_nodump.s"), ["OTC_NO_DUMP"], quiet=True)
        self.assertEqual(nod.get(".data", 0) + nod.get(".bss", 0), 0)
        print("[ccomp] ColdFire .text: %d bytes with the test dump, %d without" % (sec[".text"], nod[".text"]))


class TestCorpus(unittest.TestCase):
    def test_every_source_set_of_the_corpus(self):
        sets = unique_sets()
        step = 8 if QUICK else 1
        n = bad = overflow = peak = 0
        first = None
        c = cc()
        for i, (eid, sources) in enumerate(sets):
            if i % step:
                continue
            n += 1
            a, b = front_dump.front_dump(sources), c.dump(sources)
            peak = max(peak, c.used)
            if c.err:
                overflow += 1
            if a != b:
                bad += 1
                first = first or (eid, first_diff(a, b))
        print("[ccomp] source sets compared %d of %d (QUICK=%s): mismatches %d, arena overflows %d, peak arena use %d of %d bytes"
              % (n, len(sets), int(QUICK), bad, overflow, peak, c.ws_size))
        self.assertEqual(overflow, 0)
        self.assertIsNone(first, "%d mismatches; first: %s" % (bad, first))
        self.assertGreaterEqual(n, 800 if QUICK else 6000)


class TestSabotage(unittest.TestCase):
    def test_a_sabotaged_front_end_is_caught(self):
        sab = cc(("OTC_SABOTAGE_FRONT",))
        good = cc()
        # the sets of the corpus that hold a line of exactly 128 characters (and a few without: they must stay equal)
        chosen = []
        for eid, sources in unique_sets():
            if any(len(l) == 128 for _lb, data in sources for l in data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")):
                chosen.append((eid, sources))
            if len(chosen) >= 40:
                break
        self.assertGreater(len(chosen), 0)
        caught = 0
        for eid, sources in chosen:
            ref = front_dump.front_dump(sources)
            self.assertEqual(good.dump(sources), ref, eid)
            if sab.dump(sources) != ref:
                caught += 1
        print("[ccomp] sabotage -DOTC_SABOTAGE_FRONT: caught on %d of %d sets with a 128-character line" % (caught, len(chosen)))
        self.assertGreater(caught, 0)
        # and on the small cases too
        s = src("def f\n  tr.add 1" + " " * 118 + "\n")
        self.assertNotEqual(sab.dump(s), front_dump.front_dump(s))


# ============================================================================================ the report
def rep(sources, profile=None, lib=None):
    """The C report of a source set (profile None or "m1")."""
    c = lib or cc()
    r = c.report(sources, profile)
    return r


def py_rep(sources, profile=None):
    return oterr.reference(sources, profile)[0]


# Source sets that exercise a corner of the checker (kinds, ranges, constness, cost, graph, compiler diagnostics); the C report
# is compared with Python's, and every `expect` fragment must be in it so that the comparison cannot be vacuous.
REPORT_CASES = [
    ("clean", "otype 1\ndef main\n  menu \"A\" >> \"B\"\n  tr.add 1\n", ["1 functions OK, 0 refused, 0 warnings"]),
    ("unused arg", "def f | p q\n  tr.add q\ndef g\n  f | 1 2\n", ["argument p is unused"]),
    ("kinds", "def f | p\n  tr.add p\n  dir p\ndef g\n  f | 1\n", ["used as a string elsewhere, but a number is expected"]),
    ("call kinds", "def f | p\n  dir p\ndef g\n  f | 1\n  f | \"s\"\n", ["expected a string, got a number"]),
    ("deferred", "def bad\n  nosuch\ndef f | p\n  tr.add p\ndef g\n  f | \"s\"\n", ["expected a number, got a string"]),
    ("cond", "def f\n  tr.add 50%\n  cond.set 1 5\n  cond.set 1 fill\n", ["is a trig condition; write prob 50"]),
    ("ranges", "def f\n  tr.add 99\n  pl.set 70 amp.vol 1\n  trk 9\n  cond.set 0 fill\n", ["out of range for"]),
    ("const range", "const k 200\ndef f\n  pl.set k amp.vol 1\n", ["200 is out of range for step (1..64)"]),
    ("l bound", "def f | n\n  l 1 n: tr.add i\ndef g\n  x 3\n  f | x\n  l 1 x: tr.add 1\n  l 1 rnd 4: tr.add 1\n",
     ["l bound must be constant", "g passes a non-constant value at /SCRIPTS/T.OTY:5"]),
    ("break", "def f\n  break\n  l 1 2:\n    break\n  each: break\n", ["break outside l or each"]),
    ("ignored value", "def f\n  flip\n  file.rnd\n  tr.add 1\n", ["the value of flip is ignored", "the value of file.rnd is ignored"]),
    ("loop", "def fa\n  fb\ndef fb\n  fc\ndef fc\n  fa\ndef fd\n  fa\n", ["loop fa>fb>fc>fa", "needs fa"]),
    ("loop self", "def fa\n  fa\n", ["loop fa>fa"]),
    ("depth", "".join("def f%d\n  f%d\n" % (k, k + 1) for k in range(9)) + "def f9\n  tr.add 1\n", ["call depth 10 > 8 (f0>f1>f2>f3>f4>f5>f6>f7>f8>f9)"]),
    ("needs", "def fa\n  nosuch\ndef fb\n  fa\ndef fc\n  fb\n", ["needs fa", "needs fb"]),
    ("too long", "def f\n  menu \"T\" >> \"X\"\n  l 1 5000: tr.add i\n", ["too long: 20000/4096"]),
    ("too long saturated", "def f\n  menu \"T\" >> \"X\"\n" + "".join("%sl 0 32767:\n" % ("  " * (k + 1)) for k in range(5)) + "           tr.add 1\n",
     ["too long: 4294967295/4096"]),
    ("stack", "def f\n  a " + "add 1 " * 16 + "1\n", ["stack depth 17 > 16"]),
    ("counters", "def f\n" + "  every 2: tr.add 1\n" * 256, ["more than 255 every/skip counters"]),
    ("code length", "def f\n" + (" a " + "+ " * 14 + "1 " * 15 + "\n") * 800, ["compiled table is"]),
    ("code too long", "def f\n" + (" a " + "+ " * 14 + "1 " * 15 + "\n") * 900, ["code too long (66601 bytes)"]),
    ("table", "".join("def f%d\n  tr.add 1\n" % k for k in range(256)), ["more than 255 functions"]),
    ("table size", "def f\n" + "  tr.add 1\n" * 3000 + "def g\n" + "  tr.add 1\n" * 2000, ["compiled table is"]),
    ("trk", "def f\n  menu \"T\" >> \"X\"\n  trk all\n  trk 1 2\n  l 1 100: tr.add i\n", ["too long"]),
    ("if chain", "def f | n\n  menu \"T\" >> \"X\"\n  if n:\n    l 1 1030: tr.add i\n  elif n:\n    l 1 1001: tr.add i\n  else:\n    tr.add 1\n", ["too long: 4120"]),
    ("if chain fits", "def f | n\n  menu \"T\" >> \"X\"\n  if n:\n    l 1 700: tr.add i\n  elif n:\n    l 1 700: tr.add i\n  else:\n    l 1 700: tr.add i\n", ["1 functions OK"]),
    ("functions over 128", "".join("def f%d\n  tr.add 1\n" % k for k in range(130)), ["130 functions loaded (maximum 128)"]),
    ("destructive and calls", "def fa | n\n  l 1 n: tr.add i\ndef fb\n  fa | 3\n  fa | 4000\n", []),
]


MSG_SOURCES = [
    "def f\n  msg \"BANK\" bank.cur \"PAT\" pat.cur\n  msg \"T\" tempo -5\n  msg 1 2 3\n  msg \"\" \"\"\n",
    "const k \"KK\"\nconst n 7\ndef f\n  msg k n k\n  g | \"S\" 5\ndef g | p q\n  msg q q\n  dir p\n",
    "def f\n  msg \"" + "A" * O.MAX_MSG + "\"\n  msg \"" + "A" * (O.MAX_MSG - 6) + "\" x\n",
    "def fa\n  msg \"" + "A" * (O.MAX_MSG + 1) + "\"\ndef fb\n  msg \"" + "A" * (O.MAX_MSG - 5) + "\" x\n"
    "def fc\n  msg \"A\" \"A\" \"A\" \"A\" \"A\" \"A\" \"A\" \"A\" \"A\"\ndef fd\n  msg {1 2}\ndef fe\n  msg 50%\ndef fg\n  msg\n",
]


class TestMsgComp(unittest.TestCase):
    """msg: the C compiler's table and report are Python's (items of both kinds, the worst-case length, the item count)."""

    def test_table_and_report_equal_python(self):
        c = cc()
        for i, text in enumerate(MSG_SOURCES):
            for prof in (None, "m1"):
                s = [(LABEL, text.encode("latin-1"))]
                got = c.compile(s, prof)
                self.assertEqual(got, oterr.reference(s, prof)[::-1], (i, prof))

    def test_profile_refusal_path(self):
        """No op is outside m1 now: a test build of the C compiler (-DOTC_TEST_NOT_M1) puts msg outside, Python's profile loses it too."""
        from unittest import mock
        s = [(LABEL, b"def f\n  msg \"hi\"\ndef g\n  f\ndef h\n  tr.add 1\n")]
        c = cc(("OTC_TEST_NOT_M1=OTC_OP_MSG",))
        with mock.patch.dict(O.PROFILES, {"m1": O.PROFILES["m1"] - {"MSG"}}):
            want = oterr.reference(s, "m1")[::-1]
        got = c.compile(s, "m1")
        self.assertEqual(got, want)
        self.assertIn("not on the unit yet: msg", got[1])
        self.assertIn("needs f", got[1])
        self.assertIn("3 functions OK, 0 refused", cc().compile(s, "m1")[1])

    def test_refusals_are_the_checkers(self):
        n = O.MAX_MSG
        rep = cc().compile([(LABEL, MSG_SOURCES[3].encode())], None)[1]
        for want in ("msg is too long for the screen: %d characters" % (n + 1), "msg is too long for the screen: %d characters" % (n + 1),
                     "msg takes at most 8 items (got 9)", "expected a string or a number, got a list", "msg expects 1 or more arguments"):
            self.assertIn(want, rep)


class TestReportUnit(unittest.TestCase):
    def check(self, text, expect=(), label=LABEL, profile="m1"):
        s = src(text, label)
        for prof in (None, profile):
            a, b = py_rep(s, prof), rep(s, prof)
            self.assertEqual(cc().err, 0)
            self.assertEqual(a, b, first_diff(a, b))
            if prof == profile:
                for e in expect:
                    self.assertIn(e, a)
        return a

    def test_cases(self):
        for name, text, expect in REPORT_CASES:
            with self.subTest(name):
                self.check(text, expect)

    def test_front_cases_too(self):
        for name, text, _e in CASES:
            with self.subTest(name):
                self.check(text)

    def test_examples(self):
        d = os.path.join(ROOT, "modules", "otype", "examples")
        every = []
        for f in sorted(x for x in os.listdir(d) if x.upper().endswith(".OTY")):
            with open(os.path.join(d, f), "rb") as fh:
                every.append((("/SCRIPTS/" + f.upper()).encode(), fh.read()))
            for prof in (None, "m1"):
                self.assertEqual(py_rep(every[-1:], prof), rep(every[-1:], prof), f)
        for prof in (None, "m1"):
            self.assertEqual(py_rep(every, prof), rep(every, prof))

    def test_labels_and_order_of_files(self):
        s = [(b"/SCRIPTS/B.OTY", b"def b\n  nosuch\n"), (b"/SCRIPTS/A.OTY", b"def a\n  nosuch\n"), (b"-", b"def d\n  nosuch\n"),
             (b"", b"def e\n  nosuch\n"), (b"x\"y\x01", b"const k 1\ndef f\n  tr.add 1\n"), (b"/SCRIPTS/A.OTY", b"def a\n  nosuch\n")]
        self.assertEqual(py_rep(s), rep(s))

    def test_loader_category_and_script_limits_match_python(self):
        cats = "".join('def f%d\n  menu "C%d" >> "A"\n  tr.add 1\n' % (i, i) for i in range(34))
        self.check(cats, ("more than 32 menu categories (excess categories ignored)",))
        scripts = "".join('def f%d\n  menu "CAT" >> "A%d"\n  tr.add 1\n' % (i, i) for i in range(66))
        self.check(scripts, ('category "CAT" has more than 64 scripts (script not listed)',))

    def test_label_over_255_bytes_is_a_table_error(self):
        s = [(b"/SCRIPTS/" + b"L" * 300 + b".OTY", b"def f\n  tr.add 1\n"), (b"/SCRIPTS/A.OTY", b"def g\n  nosuch\n")]
        a = py_rep(s)
        self.assertIn("string too long for the pool", a)
        self.assertEqual(a, rep(s))

    def test_empty_inputs(self):
        for s in ([], [(b"/SCRIPTS/A.OTY", b"")], [(b"/SCRIPTS/A.OTY", b"# only a comment\n")]):
            self.assertEqual(py_rep(s), rep(s))

    def test_report_is_stable_and_the_workspace_reusable(self):
        s = src("def f | a\n  tr.add a\n  nosuch\n")
        first = rep(s)
        for _ in range(3):
            rep(src("def zzz\n" + "  nosuch\n" * 1500))     # (a run that uses the three diagnostic modes in between)
            self.assertEqual(rep(s), first)


class TestX7(unittest.TestCase):
    def test_the_cap_and_its_three_passes(self):
        for n in (999, 1000, 1001, 1002, 1500, 4000):
            s = src("def f\n" + "  nosuch\n" * n)
            a, b = py_rep(s), rep(s)
            self.assertEqual(a, b, "%d: %s" % (n, first_diff(a, b)))
            self.assertEqual(a.count("\n"), min(n, 1000) + 1 + (1 if n > 1000 else 0))
        s = src("def f\n" + "  nosuch\n" * 1001)
        self.assertIn("-:0: -: error: too many errors (1 more)\n", rep(s))

    def test_the_cut_is_in_sorted_order_not_in_emission_order(self):
        # type errors come from the checker, after every parse error: they sort before the parse errors of later lines
        text = "def f | p\n  tr.add p\n  dir p\n" + "  tr.add 1\n" * 3 + "  nosuch\n" * 1100 + "def g\n  f | \"s\"\n"
        a, b = py_rep(src(text)), rep(src(text))
        self.assertEqual(a, b, first_diff(a, b))
        self.assertIn("used as", a)
        # the files sort by label bytes, not by card order
        s = [(b"Z.OTY", b"def zz\n" + b"  nosuch\n" * 700), (b"A.OTY", b"def aa\n" + b"  nosuch\n" * 700),
             (b"M.OTY", b"def mm\n" + b"  nosuch\n" * 700), (b"-", b"def dd\n  nosuch\n")]
        a, b = py_rep(s), rep(s)
        self.assertEqual(a, b, first_diff(a, b))
        self.assertTrue(a.startswith("-:2: dd: error"))
        self.assertIn("A.OTY:2: aa: error", a)
        self.assertNotIn("Z.OTY", a)

    def test_the_workspace_holds_few_messages(self):
        """The 2-byte-line gabarit made 3 MiB overflow before the cap on the report; now the front end keeps 1000 messages whatever the input."""
        c = cc()
        s = src("def f\n" + " x\n" * 21800)
        self.assertEqual(py_rep(s), rep(s))
        self.assertEqual(c.err, 0)
        self.assertLess(c.used, 1_500_000)
        small = cc(("OTC_COMP_ARENA_BYTES=1572864",))
        self.assertEqual(py_rep(s), rep(s, lib=small))      # 1.5 MiB are enough for it now
        self.assertEqual(small.err, 0)

    def test_arena_overflow_is_an_error_not_a_report(self):
        small = cc(("OTC_COMP_ARENA_BYTES=65536",))
        self.assertEqual(rep(src("def f\n" + "  tr.add 1\n" * 5000), lib=small), "")
        self.assertEqual(small.err, 1)
        self.assertEqual(rep(src("def f\n  tr.add 1\n"), lib=small), "1 functions OK, 0 refused, 0 warnings\n")


class TestLazyPaths(unittest.TestCase):
    """The call paths of `loop` and `call depth` are written when the report is, not stored (a ring of n functions has n messages
    of n names): thousands of functions fit the workspace."""

    def test_rings_and_chains(self):
        for n in (2, 7, 450, 1500):
            for name, text in (("ring", "".join("def r%d\n  r%d\n" % (k, (k + 1) % n) for k in range(n))),
                               ("ring-rev", "".join("def r%d\n  r%d\n" % (k, (k + 1) % n) for k in reversed(range(n)))),
                               ("chain", "".join("def f%d\n  f%d\n" % (k, k + 1) for k in range(n)) + "def f%d\n  tr.add 1\n" % n)):
                with self.subTest("%s-%d" % (name, n)):
                    a, b = py_rep(src(text)), rep(src(text))
                    self.assertEqual(cc().err, 0)
                    self.assertEqual(a, b, first_diff(a, b))

    def test_thousands_of_functions_in_a_ring_or_a_chain(self):
        """Python needs a minute for a ring of 4000 and the report is 20 MB: only the C side is checked, against a property."""
        n = 3000
        text = "".join("def r%d\n  r%d\n" % (k, (k + 1) % n) for k in range(n))
        s = src(text[:65536])
        r = rep(s)
        self.assertEqual(cc().err, 0)
        lines = r.split("\n")
        self.assertTrue(lines[0].startswith("/SCRIPTS/T.OTY:1: r0: error: loop r0>r1>r2>"))
        self.assertTrue(lines[0].endswith(">r%d>r0" % (n - 1)))
        self.assertEqual(lines[1000], "-:0: -: error: too many errors (%d more)" % (n - 1000 + 1))     # (+ the warning of more than 128 functions)

    def test_the_rule_of_the_deepest_first_callee(self):
        # f calls g1 and g2, both 3 deep: the path goes through the FIRST callee (strictly larger only)
        text = ("def d\n  tr.add 1\ndef c\n  d\ndef b\n  c\ndef g1\n  b\ndef g2\n  b\ndef a1\n  g2\n  g1\n"
                "def a2\n  a1\ndef a3\n  a2\ndef a4\n  a3\ndef a5\n  a4\ndef a6\n  a5\ndef a7\n  a6\ndef a8\n  a7\n")
        a, b = py_rep(src(text)), rep(src(text))
        self.assertEqual(a, b, first_diff(a, b))
        self.assertIn("call depth 9", a)


class TestStress(unittest.TestCase):
    """Dense gabarits of 64 KB (what the arena must hold) against Python where Python survives (it recurses once per `trk` line)."""

    def test_gabarits(self):
        cases = {
            "ring": "".join("def r%d\\n  r%d\\n" % (k, (k + 1) % 1500) for k in range(1500)),
            "needs": "def f0\n  nosuch\n" + "".join("def f%d\n  f%d\n" % (k, k - 1) for k in range(1, 3000)),
            "dup defs": "def fn\n" * 10900,
            "2-byte lines": "def f\n" + "x\n" * 32000,
            "op lines": "def fn\n" + " o\n" * 21800,
            "args chain": "".join("def f%d | a\n  f%d | a\n" % (k, k + 1) for k in range(2000)) + "def f2000 | a\n  dir a\n",
            "args chain reversed": "def f2000 | a\n  dir a\n" + "".join("def f%d | a\n  f%d | a\n" % (k, k + 1) for k in reversed(range(2000))),
            "calls": "def g | a b c d\n  l 1 a: tr.add 1\ndef h | w\n" + "  g | 1 2 3 w\n" * 5000,
            "one long token": "def f\n  " + "q" * 60000 + "\n",
            "types": "def f\n" + "  tr.add \"s\"\n" * 5000,
            "const dups": "const a 1\n" * 6000,
        }
        peak = 0
        for name, text in cases.items():
            with self.subTest(name):
                s = src(text.replace("\\n", "\n").encode()[:65536])
                a, ta = oterr.reference(s, "m1")
                tb, b = cc().compile(s, "m1")
                self.assertEqual(cc().err, 0)
                self.assertEqual(a, b, first_diff(a, b))
                self.assertEqual(ta, tb, "the tables differ")
                peak = max(peak, cc().used)
        print("[ccomp] dense 64 KB gabarits: reports and tables equal to Python, peak arena %d of %d bytes" % (peak, cc().ws_size))

    def test_a_long_run_of_trk_lines_is_a_loop_not_a_recursion(self):
        """Thousands of `trk` lines: Python recurses once per line and dies (RecursionError) from ~2000; the C is iterative and
        answers.  A function with more than 15 chained passes is refused (16 control frames, 16 contexts at most: the
        VM refuses such a table at load); the cost of a long run is the product of the track counts, saturated."""
        s = src("def f\n" + "  trk 1\n" * 1500 + "  tr.add 1\n")
        self.assertEqual(rep(s), "/SCRIPTS/T.OTY:1501: f: error: loop/pass depth 1500 > 16\n0 functions OK, 1 refused, 0 warnings\n")
        self.assertEqual(rep(s), py_rep(s))
        self.assertEqual(cc().err, 0)
        s = src("def f\n" + "  trk 1\n" * 8000 + "  tr.add 1\n")
        self.assertEqual(rep(s), "/SCRIPTS/T.OTY:8001: f: error: loop/pass depth 8000 > 16\n0 functions OK, 1 refused, 0 warnings\n")
        s = src('def f\n  menu "T" >> "X"\n' + "  trk all\n" * 6000 + "  tr.add 1\n")
        self.assertEqual(rep(s), "/SCRIPTS/T.OTY:1: f: error: too long: 4294967295/4096\n0 functions OK, 1 refused, 0 warnings\n")
        # the same as Python on the sizes it survives
        for n in (1, 10, 15, 16, 17, 100, 250):
            t = src('def f\n  menu "T" >> "X"\n' + "  trk all\n" * n + "  tr.add 1\n")
            self.assertEqual(py_rep(t), rep(t))


class TestSabotageCheck(unittest.TestCase):
    def test_a_sabotaged_checker_is_caught(self):
        entries = list(corpus.iter_entries(CORPUS))
        good = cc()
        for variant, what in ((("OTC_SABOTAGE_CHECK",), "call depth 9 instead of 8"),
                              (("OTC_SABOTAGE_CHECK=2",), "if / elif chain costs added instead of the largest"),
                              (("OTC_SABOTAGE_CHECK=3",), "order of emission reversed"),
                              (("OTC_SABOTAGE_CHECK=4",), "the last card op of a page function named, not the first")):
            sab = cc(variant)
            caught = 0
            for e in entries:
                if sab.report(e.sources, e.profile) != e.report:
                    caught += 1
            print("[ccomp] sabotage -D%s (%s): caught on %d of %d entries" % (variant[0], what, caught, len(entries)))
            self.assertGreater(caught, 0, what)
        # and the unsabotaged build is exact on a depth-9 chain and a cost chain
        s = src("".join("def f%d\n  f%d\n" % (k, k + 1) for k in range(8)) + "def f8\n  tr.add 1\n")
        self.assertEqual(py_rep(s), rep(s, lib=good))
        self.assertNotEqual(py_rep(s), cc(("OTC_SABOTAGE_CHECK",)).report(s, None))


class TestReportCorpus(unittest.TestCase):
    def test_every_entry_of_the_corpus(self):
        """Both profiles of every source set: the C report is the Python report (stored in the corpus), byte for byte."""
        step = 8 if QUICK else 1
        n = bad = overflow = peak = 0
        first = None
        c = cc()
        for i, e in enumerate(corpus.iter_entries(CORPUS)):
            if i % step:
                continue
            n += 1
            r = c.report(e.sources, e.profile)
            peak = max(peak, c.used)
            if c.err:
                overflow += 1
            if r != e.report:
                bad += 1
                first = first or (e.id, e.profile, first_diff(e.report, r))
        print("[ccomp] report: entries compared %d (QUICK=%s): mismatches %d, arena overflows %d, peak arena use %d of %d bytes"
              % (n, int(QUICK), bad, overflow, peak, c.ws_size))
        self.assertEqual(overflow, 0)
        self.assertIsNone(first, "%d mismatches; first: %s" % (bad, first))
        self.assertGreaterEqual(n, 1800 if QUICK else 14000)

    def test_per_function_numbers(self):
        """Costs, destructive flags, code sizes, line tables, counters and stack depths, function by function."""
        step = 8 if QUICK else 1
        n = bad = 0
        first = None
        c = cc()
        for i, e in enumerate(corpus.iter_entries(CORPUS)):
            if i % step:
                continue
            n += 1
            a, b = check_dump.check_dump(e.sources, e.profile), c.check_dump(e.sources, e.profile)
            if a != b:
                bad += 1
                first = first or (e.id, e.profile, first_diff(a, b))
        print("[ccomp] per-function numbers: entries compared %d: mismatches %d" % (n, bad))
        self.assertIsNone(first, "%d mismatches; first: %s" % (bad, first))


class TestTableCorpus(unittest.TestCase):
    def test_every_entry_of_the_corpus(self):
        """Both profiles of every source set: the table and the report of `otc_compile` are Python's (stored in the corpus)."""
        step = 8 if QUICK else 1
        n = bad = overflow = peak = nonempty = spill = 0
        first = None
        c = cc()
        for i, e in enumerate(corpus.iter_entries(CORPUS)):
            if i % step:
                continue
            n += 1
            ctypes.memset(c.otb_buf, 0xA5, len(c.otb_buf))              # the table must not write past its own length
            o, r = c.compile(e.sources, e.profile)
            peak = max(peak, c.used)
            if c.err:
                overflow += 1
                continue
            nonempty += 1 if o else 0
            if c.otb_buf.raw[len(o):] != b"\xa5" * (len(c.otb_buf) - len(o)):
                spill += 1
            if o != e.otb or r != e.report:
                bad += 1
                first = first or (e.id, e.profile, "table equal: %s, report equal: %s, %d / %d bytes"
                                  % (o == e.otb, r == e.report, len(o), len(e.otb)))
        print("[ccomp] otb: entries compared %d (QUICK=%s), %d with a table: mismatches %d, arena overflows %d, writes past the table %d, "
              "peak arena use %d" % (n, int(QUICK), nonempty, bad, overflow, spill, peak))
        self.assertEqual(overflow, 0)
        self.assertEqual(spill, 0)
        self.assertIsNone(first, "%d mismatches; first: %s" % (bad, first))
        self.assertGreater(nonempty, n // 2)

    def test_the_vm_loads_every_table(self):
        """The C VM (otc_vm_load: header, regions, caps) and otb.parse accept every table the C compiler makes."""
        step = 4 if QUICK else 1
        n = 0
        c = cc()
        vm = cvm.CVM(vm_lib())
        for i, e in enumerate(corpus.iter_entries(CORPUS)):
            if i % step:
                continue
            o, _ = c.compile(e.sources, e.profile)
            if not o:
                continue
            n += 1
            self.assertEqual(vm.load(o), 0, "%s %s: the C VM refuses the table" % (e.id, e.profile))
            t = otb.parse(o)
            self.assertEqual(t.nfunc, len(t.funcs))
        print("[ccomp] vm load: %d tables loaded by the C VM and decoded by otb.parse" % n)
        self.assertGreater(n, 1000)

    def test_the_compilers_caps_are_the_vms_caps(self):
        """The compiler's caps match the VM exactly: a table whose max_ctx / max_ctl is 16 loads, 17 is refused (OTC_E_CAPS), like the compiler."""
        s = src("def f\n  tr.add 1\n")
        o, _ = cc().compile(s, None)
        vm = cvm.CVM(vm_lib())
        ftab = (o[14] << 8) | o[15]
        for field, name in ((36, "max_ctx"), (37, "max_ctl"), (35, "max_stack")):
            for v, want in ((16, 0), (17, cvm.E_CAPS)):
                t = bytearray(o)
                t[ftab + field] = v
                self.assertEqual(vm.load(bytes(t)), want, "%s = %d" % (name, v))
        for n, want in ((15, "1 functions OK"), (16, "context depth 17 > 16"), (17, "loop/pass depth 17 > 16")):
            self.assertIn(want, rep(src("def f\n" + "  trk 1\n" * n + "  tr.add 1\n")))

    def test_small_arena_and_buffers(self):
        """otc_compile in an arena sized for the job (not the 3 MiB one), a table that does not fit, a report cut short."""
        text = open(os.path.join(ROOT, "modules", "otype", "examples", "M1.OTY"), "rb").read()
        s = [(b"/SCRIPTS/M1.OTY", text)]
        ref_rep, ref_otb = oterr.reference(s, "m1")
        c = cc()
        o, r = c.compile(s, "m1", arena=300 * 1024)
        self.assertEqual((o, r), (ref_otb, ref_rep))
        self.assertLess(c.used, 300 * 1024)
        self.assertEqual(c.compile(s, "m1", arena=1024), (None, None))                 # too small: OTC_CE_ARENA, no output
        self.assertEqual(c.err, 1)
        arr, n = c._pack(src("def f\n  nosuch\n"))                                        # a report longer than its buffer: cut, full length told
        ol, rl, small = ctypes.c_uint32(0), ctypes.c_uint32(0), ctypes.create_string_buffer(b"#" * 20)
        self.assertEqual(c.lib.otc_compile(arr, n, 0, c.ws, c.ws_size, c.otb_buf, len(c.otb_buf), ctypes.byref(ol), small, 10,
                                           ctypes.byref(rl)), 0)
        full = oterr.reference(src("def f\n  nosuch\n"), None)[0]
        self.assertEqual((rl.value, small.raw[:10], small.raw[10:20]), (len(full), full.encode()[:10], b"#" * 10))

    def test_a_table_that_does_not_fit_the_buffer_is_none(self):
        s = src(open(os.path.join(ROOT, "modules", "otype", "examples", "M1.OTY"), "rb").read())
        c = cc()
        self.assertTrue(len(oterr.reference(s, "m1")[1]) > 100)
        self.assertEqual(c.compile(s, "m1")[0], oterr.reference(s, "m1")[1])
        self.assertEqual(c.lib.otc_emit(c.ws, c.otb_buf, 100), 0)


class TestSabotageGen(unittest.TestCase):
    def test_a_sabotaged_generator_is_caught(self):
        entries = list(corpus.iter_entries(CORPUS))[::2 if QUICK else 1]
        for variant, what in ((("OTC_SABOTAGE_GEN",), "a menu before the file name in the pool"),
                              (("OTC_SABOTAGE_GEN=2",), "the pc of the line table off by one"),
                              (("OTC_SABOTAGE_GEN=3",), "the page label before the menu list in the pool"),
                              (("OTC_SABOTAGE_GEN=4",), "the confirm text before the page label in the pool")):
            sab = cc(variant)
            caught = 0
            for e in entries:
                o, r = sab.compile(e.sources, e.profile)
                if o != e.otb or r != e.report:
                    caught += 1
            print("[ccomp] sabotage -D%s (%s): caught on %d of %d entries" % (variant[0], what, caught, len(entries)))
            self.assertGreater(caught, 0, what)


# ============================================================================================ page functions
class TestPageCorpus(unittest.TestCase):
    """The `page "LABEL"` family of the corpus (modules/otype/corpus.py fam_page, plus examples/PAGE.OTY) is really in it, reaches
    every new message and builds tables with the PAGE bit; the C compiler gives the same report and bytes on each of them (the
    whole corpus is compared by the other tests: this one fails if the family is lost or becomes vacuous)."""

    def test_page_entries_are_equal_and_not_vacuous(self):
        entries = [e for e in corpus.iter_entries(CORPUS) if e.id.startswith("page/") or e.id.endswith("PAGE.OTY")]
        self.assertGreater(len(entries), 250)
        c = cc()
        msgs, with_page, labels = set(), 0, set()
        for e in entries:
            o, r = c.compile(e.sources, e.profile)
            self.assertEqual((o, r), (e.otb, e.report), e.id)
            for line in e.report.splitlines():
                for key in ("not allowed in a page function", "has more than 6 params", "takes no arguments", "duplicate page",
                            "page label", "page expects a string"):
                    if key in line:
                        msgs.add(key)
            if e.otb:
                t = otb.parse(e.otb)
                for f in t.funcs:
                    if f.page is not None:
                        with_page += 1
                        labels.add(f.page)
                        self.assertTrue(f.hosts & 0x80)
        self.assertEqual(len(msgs), 6, msgs)
        self.assertGreater(with_page, 300)
        self.assertGreater(len(labels), 100)


class TestConfirmCorpus(unittest.TestCase):
    """The `confirm "TEXT"` family of the corpus (modules/otype/corpus.py fam_confirm) is really in it and builds tables with the
    flag bit 2 and the text; the C compiler gives the same report and bytes on each, and its parse state keeps the same text."""

    def test_confirm_entries_are_equal_and_not_vacuous(self):
        entries = [e for e in corpus.iter_entries(CORPUS) if e.id.startswith("confirm/")]
        self.assertGreater(len(entries), 50)
        c = cc()
        texts, with_text, with_both = set(), 0, 0
        for e in entries:
            o, r = c.compile(e.sources, e.profile)
            self.assertEqual((o, r), (e.otb, e.report), e.id)
            if e.otb:
                for f in otb.parse(e.otb).funcs:
                    if f.confirm_text is not None:
                        with_text += 1
                        texts.add(f.confirm_text)
                        self.assertTrue(f.flags & otb.F_CONFIRM_TEXT and f.flags & otb.F_CONFIRM)
                        with_both += f.page is not None
                    else:
                        self.assertFalse(f.flags & otb.F_CONFIRM_TEXT)
        self.assertGreater(with_text, 200)
        self.assertGreater(len(texts), 120)
        self.assertGreater(with_both, 0)
        self.assertIn("FIRST", texts)          # confirm "" then confirm "LATE": the first non-empty text
        self.assertIn("LATE", texts)
        self.assertNotIn("TWO", texts)         # confirm "ONE" then confirm "TWO": the first


# ============================================================================================ fuzzing the checker
class TestFuzz(unittest.TestCase):
    def test_random_programs(self):
        import random
        n = 400 if QUICK else 2500
        bad = errs = clean = costed = tables = vm_bad = 0
        first = None
        c = cc()
        vm = cvm.CVM(vm_lib())
        for seed in range(n):
            text = ProgGen(random.Random("e3xc/%d" % seed)).source()
            for prof in (None, "m1"):
                s = src(text)
                (a, ta) = oterr.reference(s, prof)
                tb, b = c.compile(s, prof)
                if c.err:
                    errs += 1
                    continue
                if a != b:
                    bad += 1
                    first = first or (seed, prof, first_diff(a, b), text)
                if ta != tb:
                    bad += 1
                    first = first or (seed, prof, "table: %d / %d bytes" % (len(ta), len(tb)), text)
                tables += 1 if tb else 0
                if tb:
                    vm_bad += vm.load(tb) != 0
                if " 0 refused" in a.split("\n")[-2]:
                    clean += 1
                if seed % 4 == 0:
                    x, y = check_dump.check_dump(s, prof), c.check_dump(s, prof)
                    if x != y:
                        bad += 1
                        first = first or (seed, prof, "check_dump " + first_diff(x, y), text)
                    costed += x.count("cost=") - x.count("cost=-")
        print("[ccomp] fuzz: %d random programs x 2 profiles: mismatches %d (reports and tables); %d fully accepted, %d tables "
              "(all loaded by the C VM), %d functions costed" % (n, bad, clean, tables, costed))
        self.assertEqual(errs, 0)
        self.assertEqual(vm_bad, 0)
        self.assertIsNone(first, "%d mismatches; first: %s" % (bad, first))


if __name__ == "__main__":
    unittest.main()
