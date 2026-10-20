"""Tests of the OType simulator (modules/otype/sim.py, `otype.py run`).  From the repo root:

    python3 -m unittest tools/test_otype_run.py

The tests go through the Python API (`sim.simulate` on a program checked by tools/otype.py) and
through the command line (`sim.main`, a few real subprocesses).  The random vectors below are
HARD-CODED on purpose: they are the reference of docs/syntax.md 5.3.2, and a change
to the generator must break them (the firmware has to reproduce them byte for byte).

Behaviour: tr.* and has.trig act on red + trigless trig + one-shot;
`steps a b` replaces; a multi-track `trk` runs its body once per track (spec 6.2); `1:1` is a load
error (tools/test_otype.py); locks and trig-word writes land only on a step that carries a trig of
any kind (skipped and counted otherwise); `trk.init` resets the track's sound, not its trigs.
Removing a trig wipes the step's locks / sample lock /
condition (unless a trigless lock stays), `tr.add` keeps a one-shot's data, a PICKUP track refuses
every write.
"""
import glob
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import otype  # noqa: E402
from modules.otype import ops as O  # noqa: E402
from modules.otype import otb, sim  # noqa: E402

# ---- the reference vectors (first 5 outputs of each stream) ----------------------------------
VEC = {
    1: {"RAND": [1251154478, 319755877, 3384417796, 3307322393, 2801683528],
        "TOSS": [120309530, 2262804286, 892574306, 2306921213, 3896006994],
        "PROB": [2461223821, 2207276012, 2505993434, 1080367032, 2967386321],
        "DRUNK": [1020288408, 3674964745, 3325305148, 6522517, 1720895341],
        "FILE": [3282185157, 1568923048, 3773765843, 2103073801, 3988229092]},
    0: {"RAND": [770105286, 903020009, 663941918, 1204876070, 1865932924],
        "TOSS": [2285585213, 3956207987, 2314337251, 1731580933, 1796620607],
        "PROB": [959658961, 2540677717, 646075303, 4062592604, 2287546858],
        "DRUNK": [3655920382, 1381915653, 2718477738, 1074493684, 3450339518],
        "FILE": [274827971, 101102015, 3591769415, 3764532632, 2097002736]},
}
SEED_STATE = {1: {"RAND": 0x35e49459, "TOSS": 0x885f19eb, "PROB": 0x6bc9295c,
                  "DRUNK": 0xb33c9ee8, "FILE": 0x61d9e918}}


def below(r, n):
    """The spec's below(n) applied to a raw output r (independent of sim.py)."""
    return ((r >> 16) * n) >> 16


COVERED = set()


def covers(*names):
    def deco(f):
        COVERED.update(n.upper() for n in names)
        return f
    return deco


# ---- helpers ---------------------------------------------------------------------------------
def body_text(body):
    return "".join("  %s\n" % ln if ln.strip() else "\n"
                   for ln in textwrap.dedent(body).strip("\n").split("\n"))


# Test macros: the bulk jobs the removed helper ops did, as the cookbook writes them as loops
FILL = ("l 1 128:\n  if gt i file.len: break\n  a static.firstfree\n  if ez a: break\n"
        "  static.load a file.at i")
CLEAR = "l 1 128:\n  static.clr i"
SHUFFLE = "l 1 128:\n  if static.free i: break\n  static.swap i rrand 1 i"
SCENES = ("scn.clr 1\nl 2 16:\n  l 6 11:\n    scn.set j i rrand 0 127\n  l 18 29:\n    scn.set j i rrand 0 127")


def make_prog(body, extra=""):
    macros = {"@fill": FILL, "@clear": CLEAR, "@shuffle": SHUFFLE, "@scenes": SCENES, "@lfodinv": "l 1 16:\n  lfod.set i sub 0 lfod.get i"}
    lines = []
    for ln in body.split("\n"):
        ind = ln[:len(ln) - len(ln.lstrip())]
        if ln.strip() in macros:
            lines.extend(ind + m for m in macros[ln.strip()].split("\n"))
        else:
            lines.append(ln)
    body = "\n".join(lines)
    text = 'def main\n  menu "T" >> "MAIN"\n' + body_text(body) + textwrap.dedent(extra)
    return otype.check_sources([("T.OTY", text)]), text


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def digest(path):
    return hashlib.sha256(read_bytes(path)).hexdigest()


def make_card(root, files):
    for rel in files:
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")
    return root


class Base(unittest.TestCase):
    def prog(self, body, extra=""):
        p, text = make_prog(body, extra)
        bad = [str(d) for d in p.diags if d.level == "error"]
        self.assertEqual(bad, [], text)
        return p

    def go(self, body, extra="", seed=1, **kw):
        return sim.simulate(self.prog(body, extra), "main", seed=seed, **kw)

    def gl(self, body, extra="", **kw):
        """The globals (x y z t) after a run."""
        return self.go(body, extra, **kw).globals

    def tmp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name

    def card(self, files):
        return make_card(self.tmp(), files)

    @staticmethod
    def trigs(res, t=1):
        return [s for s in range(1, 65) if res.model.tracks[t].has("trig", s)]

    @staticmethod
    def locks(res, t=1):
        return {s: dict(d) for s, d in res.model.tracks[t].locks.items()}


# ================================================================================================
class TestPrng(Base):
    def test_vectors(self):
        for seed, streams in VEC.items():
            for name, want in streams.items():
                s = sim.Stream(name, seed)
                self.assertEqual([s.next() for _ in range(5)], want, "seed %d %s" % (seed, name))

    def test_stream_numbers(self):
        self.assertEqual(sim.STREAM_K, {"RAND": 1, "TOSS": 2, "PROB": 3, "DRUNK": 4, "FILE": 5})
        self.assertEqual(sim.STREAM_NAMES, ("RAND", "TOSS", "PROB", "DRUNK", "FILE"))
        self.assertEqual(O.STREAMS, sim.STREAM_NAMES)      # the registry and the simulator agree

    def test_seed_state(self):
        for name, want in SEED_STATE[1].items():
            self.assertEqual(sim.seed_state(1, sim.STREAM_K[name]), want, name)

    def test_seed_uses_16_bits(self):
        self.assertEqual(sim.seed_state(65537, 1), sim.seed_state(1, 1))
        self.assertEqual(sim.seed_state(-65535, 3), sim.seed_state(1, 3))
        self.assertNotEqual(sim.seed_state(1, 1), sim.seed_state(1, 2))     # one stream each
        self.assertNotEqual(sim.seed_state(1, 1), sim.seed_state(2, 1))

    def test_xorshift_step(self):
        self.assertEqual(sim.xorshift32(1), 270369)        # (13, 17, 5) from the state 1

    def test_below_and_toss_formulas(self):
        s = sim.Stream("RAND", 1)
        self.assertEqual(s.below(100), below(VEC[1]["RAND"][0], 100))
        self.assertEqual(s.below(7), below(VEC[1]["RAND"][1], 7))
        t = sim.Stream("TOSS", 1)
        self.assertEqual([t.toss() for _ in range(2)], [0, 1])
        self.assertEqual(VEC[1]["TOSS"][0] >> 31, 0)
        self.assertEqual(VEC[1]["TOSS"][1] >> 31, 1)

    def test_below_non_positive_is_one_draw(self):
        d = {n: 0 for n in sim.STREAM_NAMES}
        s = sim.Stream("RAND", 1, d)
        self.assertEqual((s.below(0), s.below(-3)), (0, 0))
        self.assertEqual(d["RAND"], 2)
        self.assertEqual(s.next(), VEC[1]["RAND"][2])      # the stream moved exactly twice

    def test_vectors_text_prints_the_same_vectors(self):
        text = sim.vectors_text()
        for seed, streams in VEC.items():
            for name, want in streams.items():
                for v in want:
                    self.assertIn(str(v), text)


# ================================================================================================
VALUE_CASES = [
    ("ADD", "add 1 2", 3), ("ADD", "+ 32767 1", 32767), ("ADD", "add -32767 -100", -32768),
    ("SUB", "sub 1 5", -4), ("SUB", "- 5 1", 4), ("SUB", "sub -32767 100", -32768),
    ("MUL", "mul 3 -4", -12), ("MUL", "* 200 200", 32767), ("MUL", "mul -200 200", -32768),
    ("DIV", "div 7 2", 3), ("DIV", "/ -7 2", -3), ("DIV", "div 7 -2", -3), ("DIV", "div 5 0", 0),
    ("DIV", "div -32768 -1", 32767),
    ("MOD", "mod -1 4", 3), ("MOD", "mod 7 4", 3), ("MOD", "mod 5 0", 0),
    ("MOD", "% -1 4", 3), ("MOD", "% 7 4", 3), ("MOD", "% 10 3", 1),
    ("MIN", "min 3 -2", -2), ("MAX", "max 3 -2", 3),
    ("ABS", "abs -9", 9), ("ABS", "abs -32768", 32767),
    ("NEG", "neg 5", -5), ("NEG", "neg -32768", 32767),
    ("NOT", "not 0", 1), ("NOT", "not 7", 0),
    ("LIM", "lim 200 0 127", 127), ("LIM", "lim -5 0 127", 0), ("LIM", "lim 50 0 127", 50),
    ("WRAP", "wrap 130 0 127", 2), ("WRAP", "wrap -1 0 9", 9), ("WRAP", "wrap 5 0 9", 5),
    ("EQ", "eq 3 3", 1), ("EQ", "== 3 4", 0), ("NE", "ne 3 4", 1), ("NE", "!= 3 3", 0),
    ("LT", "lt 3 4", 1), ("LT", "< 4 4", 0), ("GT", "gt 5 4", 1), ("GT", "> 4 4", 0),
    ("LE", "le 4 4", 1), ("LE", "<= 5 4", 0), ("GE", "ge 4 4", 1), ("GE", ">= 3 4", 0),
    ("AND", "and 1 2", 1), ("AND", "and 1 0", 0), ("OR", "or 0 3", 1), ("OR", "or 0 0", 0),
    ("XOR", "xor 1 0", 1), ("XOR", "xor 2 3", 0), ("XOR", "xor 0 0", 0),
    ("EZ", "ez 0", 1), ("EZ", "ez 2", 0), ("NZ", "nz 2", 1), ("NZ", "nz 0", 0),
    ("?", "? 1 10 20", 10), ("?", "? 0 10 20", 20), ("?", "? 5 10 20", 10),
    ("ON", "on", 1), ("OFF", "off", 0),
    ("AT", "at {10 20 30} 2", 20), ("AT", "at {10 20 30} 0", 10), ("AT", "at {10 20 30} 9", 30),
    ("LEN", "len {4 5 6 7}", 4),
    ("PB", "pb 1", 0), ("PB", "pb 6", 5), ("LFO", "lfo 1", 6), ("LFO", "lfo 3", 8),
    ("AMP", "amp 1", 12), ("AMP", "amp 4", 15), ("FX1", "fx1 1", 18), ("FX1", "fx1 6", 23),
    ("FX2", "fx2 1", 24), ("FX2", "fx2 6", 29),
]


covers(*{c[0] for c in VALUE_CASES})(None)           # the table above covers these operations


class TestLanguageOps(Base):
    def test_value_cases(self):
        for name, expr, want in VALUE_CASES:
            with self.subTest(op=name, expr=expr):
                self.assertEqual(self.gl("x " + expr)["x"], want)

    @covers("AMP.ATK", "AMP.HOLD", "AMP.REL", "AMP.VOL", "AMP.BAL", "AMP.XVOL", "LFO.SPD",
            "LFO.DEP")
    def test_registry_constants(self):
        names = {"AMP.ATK": 12, "AMP.HOLD": 13, "AMP.REL": 14, "AMP.VOL": 15, "AMP.BAL": 16,
                 "AMP.XVOL": 17}
        for n, v in names.items():
            with self.subTest(n):
                self.assertEqual(self.gl("x " + n.lower())["x"], v)
        for i in (1, 2, 3):
            self.assertEqual(self.gl("x lfo.spd %d" % i)["x"], 5 + i)
            self.assertEqual(self.gl("x lfo.dep %d" % i)["x"], 8 + i)

    def test_page_helper_out_of_range_skips_its_user(self):
        r = self.go("a pb 0\ntr.add a")           # pb 0 = -1: tr.add -1 is out of range, skipped
        self.assertEqual(self.trigs(r), [])
        self.assertEqual(len(r.oor), 2)           # pb 0 itself, then tr.add -1

    def test_saturation_both_ends(self):
        g = self.gl("x sub 0 32767\ny sub x 100\nz add y -5\nt neg y")
        self.assertEqual((g["y"], g["z"], g["t"]), (-32768, -32768, 32767))

    def test_and_or_evaluate_both_sides(self):
        r = self.go("x and 0 rnd 10\ny or 1 rnd 10")
        self.assertEqual(r.draws["RAND"], 2)

    def test_lazy_question_mark(self):
        r = self.go("x ? 0 rnd 10 5")
        self.assertEqual((r.globals["x"], r.draws["RAND"]), (5, 0))
        r = self.go("x ? 1 rnd 10 5")
        self.assertEqual((r.globals["x"], r.draws["RAND"]), (below(VEC[1]["RAND"][0], 10), 1))
        r = self.go("x ? 0 5 rnd 10")
        self.assertEqual(r.globals["x"], below(VEC[1]["RAND"][0], 10))
        # the dearer branch pays, a side effect of the unchosen branch never happens
        r = self.go("x ? 0 o 7\ny o")
        self.assertEqual((r.globals["x"], r.globals["y"]), (7, 0))      # o was not read by the ?

    def test_lazy_does_not_use_up_a_file(self):
        card = self.card(["A/a.wav", "A/b.wav", "A/c.wav"])
        r = self.go('dir "A"\nx ? 0 file.pick 9', card=card)
        self.assertEqual(r.draws["FILE"], 0)
        self.assertEqual(r.state["funcs"].get("main", {}).get("pick", {}), {})

    @covers("ER", "ER.R")
    def test_euclid(self):
        r = self.go("l 1 8: if er 3 8 i: tr.add i")
        self.assertEqual(self.trigs(r), [1, 4, 7])
        r = self.go("l 1 8: if er.r 3 8 2 i: tr.add i")
        self.assertEqual(self.trigs(r), [1, 3, 6])
        r = self.go("l 1 8: if er 8 8 i: tr.add i")
        self.assertEqual(self.trigs(r), list(range(1, 9)))
        r = self.go("l 1 8: if er 0 8 i: tr.add i")
        self.assertEqual(self.trigs(r), [])
        r = self.go("l 1 16: if er 2 4 i: tr.add i")         # s wraps modulo n
        self.assertEqual(self.trigs(r), [1, 3, 5, 7, 9, 11, 13, 15])

    @covers("DR.V")
    def test_drv_stays_in_velocity_range(self):
        for c in range(0, 9):
            for s in range(1, 17):
                v = self.gl("x dr.v %d %d" % (c, s))["x"]
                self.assertTrue(0 <= v <= 127, (c, s, v))

    def test_drv_tables(self):
        """The tables written 04/10: 8 curves of 16 steps, step s reads entry (s - 1) mod 16, another curve reads 0."""
        self.assertEqual(sim.DRV_CURVES, 8)
        self.assertTrue(all(len(t) == 16 and all(0 <= v <= 127 for v in t) for t in sim.DRV_TABLE))
        row = lambda c, s0=1: [self.gl("x dr.v %d %d" % (c, s))["x"] for s in range(s0, s0 + 16)]
        self.assertEqual(row(0), [100] * 16)
        self.assertEqual(row(1), [127, 80, 80, 80] * 4)                                   # the beats
        self.assertEqual(row(2), [60, 60, 110, 60] * 4)                                   # off the beats
        self.assertEqual(row(3), [16 + 7 * j for j in range(16)])                         # ramp up
        self.assertEqual(row(4), [127 - 7 * j for j in range(16)])                        # ramp down
        self.assertEqual(row(5), [120, 70] * 8)                                           # eighths / sixteenths
        self.assertEqual([j + 1 for j, v in enumerate(row(6)) if v == 127], [1, 4, 7, 9, 12, 15])   # tresillo 3+3+2
        r7 = row(7)
        self.assertEqual(max(r7), 127)
        self.assertEqual(r7.index(127), 8)                                                # swell: the top on step 9
        self.assertEqual(r7[1:9], sorted(r7[1:9]))
        # 16-step repeat, s <= 0 wraps the same way, a curve outside 0..7 reads curve 0
        self.assertEqual(row(1, 17), row(1))
        self.assertEqual(row(6, -15), row(6))
        self.assertEqual(self.gl("x dr.v 6 0")["x"], sim.DRV_TABLE[6][15])
        self.assertEqual(self.gl("x dr.v 3 -32768")["x"], sim.DRV_TABLE[3][(-32768 - 1) % 16])
        for c in (-1, 8, 32767, -32768):
            self.assertEqual(row(c), [100] * 16, c)


class TestRandomOps(Base):
    @covers("RND")
    def test_rnd(self):
        v = VEC[1]["RAND"]
        r = self.go("x rnd 100\ny rnd 100\nz rnd 6")
        self.assertEqual([r.globals[k] for k in "xyz"],
                         [below(v[0], 100), below(v[1], 100), below(v[2], 6)])
        self.assertEqual(below(v[0], 100), 29)               # the number printed in the spec
        self.assertEqual(r.draws, {"RAND": 3, "TOSS": 0, "PROB": 0, "DRUNK": 0, "FILE": 0})

    def test_rnd_non_positive_is_zero_with_one_draw(self):
        r = self.go("x rnd 0\ny rnd -4\nz rnd 100")
        self.assertEqual((r.globals["x"], r.globals["y"]), (0, 0))
        self.assertEqual(r.draws["RAND"], 3)
        self.assertEqual(r.globals["z"], below(VEC[1]["RAND"][2], 100))

    @covers("RRAND")
    def test_rrand(self):
        v = VEC[1]["RAND"]
        r = self.go("x rrand 5 8\ny rrand 8 5\nz rrand -3 -3")
        self.assertEqual(r.globals["x"], 5 + below(v[0], 4))
        self.assertEqual(r.globals["y"], 5 + below(v[1], 4))      # either order
        self.assertEqual(r.globals["z"], -3)
        self.assertEqual(r.draws["RAND"], 3)

    @covers("TOSS")
    def test_toss(self):
        r = self.go("x toss\ny toss\nz toss")
        self.assertEqual([r.globals[k] for k in "xyz"], [v >> 31 for v in VEC[1]["TOSS"][:3]])
        self.assertEqual(r.draws["TOSS"], 3)
        self.assertEqual(self.gl("x toss", seed=0)["x"], 1)

    @covers("CHANCE")
    def test_chance(self):
        # below(TOSS draw 1, 100) = 2: chance 3 hits, chance 2 does not; the stream is the toss one
        self.assertEqual(below(VEC[1]["TOSS"][0], 100), 2)
        self.assertEqual(self.gl("x chance 3")["x"], 1)
        self.assertEqual(self.gl("x chance 2")["x"], 0)
        r = self.go("a 250\nx chance 0\ny chance 100\nz chance a")
        self.assertEqual((r.globals["x"], r.globals["y"], r.globals["z"]), (0, 1, 1))
        self.assertEqual(r.draws["TOSS"], 3)       # chance 0 and chance 100 still draw

    @covers("PICK")
    def test_pick(self):
        r = self.go("x pick {10 20 30}\ny pick {7}")
        self.assertEqual(r.globals["x"], (10, 20, 30)[below(VEC[1]["RAND"][0], 3)])
        self.assertEqual(r.globals["y"], 7)
        self.assertEqual(r.draws["RAND"], 2)       # a one-element list draws too

    def test_each_random_op_is_one_draw_from_its_own_stream(self):
        r = self.go("x rnd 5\ny toss\nz chance 50\nt pick {1 2}\nx rrand 1 9")
        self.assertEqual(r.draws, {"RAND": 3, "TOSS": 2, "PROB": 0, "DRUNK": 0, "FILE": 0})

    def test_prob_modifier(self):
        self.assertEqual(below(VEC[1]["PROB"][0], 100), 57)
        r = self.go("prob 58: x 1")
        self.assertEqual((r.globals["x"], r.draws["PROB"]), (1, 1))
        r = self.go("prob 57: x 1")
        self.assertEqual((r.globals["x"], r.draws["PROB"]), (0, 1))
        r = self.go("prob 0: x 1\nprob 100: y 1\nprob 400: z 1")      # clamped, always one draw
        self.assertEqual([r.globals[k] for k in "xyz"], [0, 1, 1])
        self.assertEqual(r.draws["PROB"], 3)

    def test_prob_does_not_shift_rand(self):
        a = self.gl("prob 50: x 1\ny rnd 1000")["y"]
        b = self.gl("y rnd 1000")["y"]
        self.assertEqual(a, b)


# ================================================================================================
PROBES = {      # stream -> (a script that stores one value of that stream in x, its expected value)
    "RAND": ("x rnd 1000", lambda v: below(v["RAND"][0], 1000)),
    "TOSS": ("x toss", lambda v: v["TOSS"][0] >> 31),
    "PROB": ("prob 50: x 1", lambda v: int(below(v["PROB"][0], 100) < 50)),
    "DRUNK": ("drunk 50\nx drunk\nx drunk", lambda v: 50 + below(v["DRUNK"][0], 3) - 1),
    "FILE": ('dir "A"\nx file.rnd', lambda v: below(v["FILE"][0], 50) + 1),
}


class TestSeedScoping(Base):
    def card50(self):
        return self.card(["A/f%02d.wav" % i for i in range(50)])

    def probe(self, stream, pre="", seed=1):
        body, _ = PROBES[stream]
        return self.gl(pre + body, card=self.card50(), seed=seed)["x"]

    def test_probes_distinguish_the_two_seeds(self):
        for s, (body, f) in PROBES.items():
            with self.subTest(s):
                self.assertEqual(self.probe(s, seed=1), f(VEC[1]))
                self.assertEqual(self.probe(s, seed=0), f(VEC[0]))
                self.assertNotEqual(f(VEC[0]), f(VEC[1]), "the probe cannot tell the seeds apart")

    @covers("SEED")
    def test_seed_statement_equals_the_cli_seed(self):
        for s in PROBES:
            with self.subTest(s):
                self.assertEqual(self.probe(s, pre="seed 0\n", seed=1), PROBES[s][1](VEC[0]))

    @covers("RAND.SEED", "TOSS.SEED", "PROB.SEED", "DRUNK.SEED", "FILE.SEED")
    def test_per_stream_seed_changes_only_its_stream(self):
        for s in sim.STREAM_NAMES:
            for r in sim.STREAM_NAMES:
                with self.subTest(seeded=s, probed=r):
                    want = PROBES[r][1](VEC[0] if r == s else VEC[1])
                    self.assertEqual(self.probe(r, pre="%s.seed 0\n" % s.lower()), want)

    def test_seed_in_a_block_is_scoped_outer_streams_resume(self):
        r = self.go("""
            z rnd 1000
            a toss
            trk 1:
              seed 0
              x rnd 1000
              t toss
            y rnd 1000
            t add mul 2 a toss
            """)
        v1, v0 = VEC[1]["RAND"], VEC[0]["RAND"]
        self.assertEqual(r.globals["x"], below(v0[0], 1000))      # inside: the new seed
        self.assertEqual(r.globals["z"], below(v1[0], 1000))
        self.assertEqual(r.globals["y"], below(v1[1], 1000))      # outside: draw 2 of seed 1
        self.assertEqual(r.globals["t"], 1)                       # toss: draw 1 (0), then draw 2 (1)

    def test_seed_modifier_is_scoped_to_its_line(self):
        r = self.go("seed 0: x rnd 1000\ny rnd 1000")
        self.assertEqual(r.globals["x"], below(VEC[0]["RAND"][0], 1000))
        self.assertEqual(r.globals["y"], below(VEC[1]["RAND"][0], 1000))

    def test_draw_in_a_block_without_seed_moves_the_shared_stream(self):
        r = self.go("x rnd 1000\ntrk 1:\n  y rnd 1000\nz rnd 1000")
        v = VEC[1]["RAND"]
        self.assertEqual([r.globals[k] for k in "xyz"], [below(v[i], 1000) for i in range(3)])

    def test_seed_in_a_called_function_does_not_leak_to_the_caller(self):
        r = self.go("x rnd 1000\nhelper\ny rnd 1000",
                    extra="def helper\n  seed 0\n  z rnd 1000\n")
        self.assertEqual(r.globals["x"], below(VEC[1]["RAND"][0], 1000))
        self.assertEqual(r.globals["z"], below(VEC[0]["RAND"][0], 1000))
        self.assertEqual(r.globals["y"], below(VEC[1]["RAND"][1], 1000))   # resumes exactly

    def test_per_stream_seed_in_a_called_function_does_not_leak(self):
        r = self.go("helper\nx rnd 1000\ny toss", extra="def helper\n  rand.seed 0\n  z rnd 1000\n")
        self.assertEqual(r.globals["z"], below(VEC[0]["RAND"][0], 1000))
        self.assertEqual(r.globals["x"], below(VEC[1]["RAND"][0], 1000))

    def test_a_called_function_without_seed_draws_from_the_callers_streams(self):
        r = self.go("x rnd 1000\nhelper\ny rnd 1000", extra="def helper\n  z rnd 1000\n")
        v = VEC[1]["RAND"]
        self.assertEqual([r.globals[k] for k in "xzy"], [below(v[i], 1000) for i in range(3)])

    def test_seed_in_a_loop_body_restarts_each_pass(self):
        r = self.go("l 1 3:\n  seed 0\n  x add x rnd 100")
        self.assertEqual(r.globals["x"], 3 * below(VEC[0]["RAND"][0], 100))

    def test_seed_value_is_an_expression_and_flip_alternates_it(self):
        prog = self.prog("seed ? flip 0 1\nx rnd 1000")
        r1 = sim.simulate(prog, "main", seed=5)
        r2 = sim.simulate(prog, "main", seed=5, state=json.loads(json.dumps(r1.state)))
        self.assertEqual(r1.globals["x"], below(VEC[1]["RAND"][0], 1000))    # flip reads 0 -> seed 1?
        self.assertEqual(r2.globals["x"], below(VEC[0]["RAND"][0], 1000))

    def test_every_run_without_seed_prints_a_clock_seed(self):
        r = sim.simulate(self.prog("x rnd 10"), "main")
        self.assertEqual(r.seed_source, "clock")
        again = sim.simulate(self.prog("x rnd 10"), "main", seed=r.seed)
        self.assertEqual(again.globals["x"], r.globals["x"])
        self.assertEqual(again.seed_source, "--seed")


# ================================================================================================
class TestState(Base):
    def presses(self, body, n, extra="", **kw):
        prog = self.prog(body, extra)
        out, state = [], None
        for _ in range(n):
            r = sim.simulate(prog, "main", seed=1, state=state, **kw)
            state = json.loads(json.dumps(r.state))       # what --state writes and reads back
            out.append(r)
        return out

    @covers("FLIP")
    def test_flip(self):
        rs = self.presses("x flip\ny flip", 2)
        self.assertEqual([(r.globals["x"], r.globals["y"]) for r in rs], [(0, 1), (0, 1)])
        rs = self.presses("x flip", 4)
        self.assertEqual([r.globals["x"] for r in rs], [0, 1, 0, 1])
        r = self.go("flip 1\nx flip\ny flip")                   # `flip x` sets
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 0))

    @covers("O", "O.MIN", "O.MAX", "O.WRAP", "O.INC")
    def test_o_counter(self):
        rs = self.presses("x o", 3)
        self.assertEqual([r.globals["x"] for r in rs], [0, 1, 2])
        r = self.go("o.min 2\no.max 4\nx o\ny o\nz o\nt o")
        self.assertEqual([r.globals[k] for k in "xyzt"], [2, 3, 4, 2])          # wraps
        r = self.go("o.min 2\no.max 4\no.wrap 0\nx o\ny o\nz o\nt o")
        self.assertEqual([r.globals[k] for k in "xyzt"], [2, 3, 4, 4])          # stops
        r = self.go("o.inc 2\nx o\ny o\nz o")
        self.assertEqual([r.globals[k] for k in "xyz"], [0, 2, 4])
        r = self.go("o 10\nx o\ny o")
        self.assertEqual((r.globals["x"], r.globals["y"]), (10, 11))
        r = self.go("o.max 3\no.inc 3\nx o\ny o\nz o")                           # 0, 3, 6 -> 2
        self.assertEqual([r.globals[k] for k in "xyz"], [0, 3, 2])

    def drunk_walk(self, start, seeds, lo=0, hi=127, wrap=0):
        v, out = start, []
        for r in seeds:
            out.append(v)
            v += below(r, 3) - 1
            if v < lo or v > hi:
                v = lo + (v - lo) % (hi - lo + 1) if wrap else min(max(v, lo), hi)
        return out

    @covers("DRUNK", "DRUNK.MIN", "DRUNK.MAX", "DRUNK.WRAP")
    def test_drunk(self):
        want = self.drunk_walk(50, VEC[1]["DRUNK"][:4])
        r = self.go("drunk 50\nx drunk\ny drunk\nz drunk\nt drunk")
        self.assertEqual([r.globals[k] for k in "xyzt"], want)
        self.assertEqual(r.draws["DRUNK"], 4)
        # the first step of seed 1 is -1: at the low bound it stops, or wraps to the top
        self.assertEqual(below(VEC[1]["DRUNK"][0], 3) - 1, -1)
        r = self.go("drunk.max 2\nx drunk\ny drunk")
        self.assertEqual((r.globals["x"], r.globals["y"]), (0, 0))
        r = self.go("drunk.max 2\ndrunk.wrap 1\nx drunk\ny drunk")
        self.assertEqual((r.globals["x"], r.globals["y"]), (0, 2))
        r = self.go("drunk.min 5\ndrunk.max 5\nx drunk\ny drunk")
        self.assertEqual((r.globals["x"], r.globals["y"]), (5, 5))
        self.assertEqual(self.go("drunk.min 9\nx drunk").globals["x"], 9)       # a set clamps

    def test_drunk_continues_over_presses(self):
        first = self.presses("drunk 50\nx drunk", 1)[0]
        self.assertEqual(first.globals["x"], 50)
        # no `drunk x` in the script: the value walked to by press 1 is where press 2 starts
        a, b = self.presses("drunk.min 40\nx drunk\ny drunk", 2)
        self.assertEqual(b.globals["x"], a.state["funcs"]["main"]["drunk"])
        walk = self.drunk_walk(40, VEC[1]["DRUNK"][:3], lo=40)                 # 40 -> 40 (low bound) -> ...
        self.assertEqual((a.globals["x"], a.globals["y"], a.state["funcs"]["main"]["drunk"]), tuple(walk))

    def test_every_and_skip_over_presses(self):
        rs = self.presses("every 3: x add x 1\nskip 2: y add y 1", 6)
        # x is a global kept over the presses: fires on press 3 and 6; y (skip 2) on 1, 3, 5
        self.assertEqual([r.globals["x"] for r in rs], [0, 0, 1, 1, 1, 2])
        self.assertEqual([r.globals["y"] for r in rs], [1, 1, 2, 2, 3, 3])
        self.assertEqual(set(rs[-1].state["funcs"]["main"]["every"].values()), {0})

    def test_every_counter_per_line_not_per_function(self):
        rs = self.presses("every 2: x add x 1\nevery 3: y add y 1", 6)
        self.assertEqual([r.globals["x"] for r in rs], [0, 1, 1, 2, 2, 3])
        self.assertEqual([r.globals["y"] for r in rs], [0, 0, 1, 1, 1, 2])

    def test_every_counts_each_pass_of_a_line_in_a_loop(self):
        r = self.go("l 1 6: every 3: x add x 1")
        self.assertEqual(r.globals["x"], 2)

    def test_every_one_always_fires_and_zero_never(self):
        r = self.go("l 1 4: every 1: x add x 1\nl 1 4: every 0: y add y 1\nl 1 4: skip 0: z add z 1")
        self.assertEqual((r.globals["x"], r.globals["y"], r.globals["z"]), (4, 0, 4))

    def test_globals_xyzt_persist_and_locals_do_not(self):
        rs = self.presses("x add x 1\ny add y 2\nz add z a\na add a 5\nt a", 3)
        self.assertEqual([(r.globals["x"], r.globals["y"], r.globals["z"], r.globals["t"])
                          for r in rs], [(1, 2, 0, 5), (2, 4, 0, 5), (3, 6, 0, 5)])

    def test_state_of_two_functions_is_separate(self):
        rs = self.presses("x flip\nother", 2, extra="def other\n  y flip\n")
        self.assertEqual([(r.globals["x"], r.globals["y"]) for r in rs], [(0, 0), (1, 1)])
        st = rs[0].state["funcs"]
        self.assertEqual(set(st), {"main", "other"})

    @covers("FILE.PICK")
    def test_file_pick_is_a_bag_over_presses(self):
        card = self.card(["A/a.wav", "A/b.wav", "A/c.wav"])
        rs = self.presses('dir "A"\nx file.pick', 4, card=card)
        # same seed every press: draws below(.,3), below(.,2), below(.,1), then a new bag
        want = []
        bag = [1, 2, 3]
        for _ in range(4):
            if not bag:
                bag = [1, 2, 3]
            want.append(bag.pop(below(VEC[1]["FILE"][0], len(bag))))
        self.assertEqual([r.globals["x"] for r in rs], want)
        self.assertEqual(want, [3, 2, 1, 3])

    def test_file_pick_bag_has_no_repeat_inside_one_press(self):
        card = self.card(["A/a.wav", "A/b.wav", "A/c.wav", "A/d.wav"])
        r = self.go('dir "A"\na file.pick\nx a\na file.pick\ny a\na file.pick\nz a\na file.pick\nt a',
                    card=card)
        self.assertEqual(sorted(r.globals[k] for k in "xyzt"), [1, 2, 3, 4])

    def test_file_pick_bags_are_per_list(self):
        card = self.card(["A/a.wav", "A/b.wav", "B/c.wav", "B/d.wav"])
        r = self.go('dir "A"\nx file.pick\ndir "B"\ny file.pick\ndir "A"\nz file.pick', card=card)
        self.assertEqual(r.globals["x"], below(VEC[1]["FILE"][0], 2) + 1)
        self.assertEqual(r.globals["z"], 3 - r.globals["x"])     # the A bag had one file left

    def test_state_file_survives_json_and_keeps_untouched_functions(self):
        prog = self.prog("x add x 5\ny flip", extra="def other\n  z flip\n")
        r1 = sim.simulate(prog, "main", seed=1)
        r2 = sim.simulate(prog, "other", seed=1, state=json.loads(json.dumps(r1.state)))
        self.assertEqual(set(r2.state["funcs"]), {"main", "other"})
        self.assertEqual(r2.state["funcs"]["main"]["flip"], 1)       # main kept its flip
        self.assertEqual(r2.globals["x"], 5)                         # and the globals are shared

    def test_parameters_are_not_kept_over_presses(self):
        prog = self.prog("param n 5 1 9\nx n")      # (make_prog gives main a menu line)
        r = sim.simulate(prog, "main", seed=1, params={"n": 7})
        self.assertEqual(r.globals["x"], 7)
        r = sim.simulate(prog, "main", seed=1, state=r.state)
        self.assertEqual(r.globals["x"], 5)

    def test_bad_state_values_do_not_crash(self):
        prog = self.prog("x flip\ny o")
        r = sim.simulate(prog, "main", seed=1, state={"globals": {"x": "3", "q": 1},
                                                      "funcs": {"main": {"flip": "1", "zzz": 4}}})
        self.assertEqual(r.globals["x"], 1)          # flip was "1": read 1, then 0
        self.assertIn("main", r.state["funcs"])


# ================================================================================================
class TestControlFlow(Base):
    def test_loops_and_counters(self):
        r = self.go("l 1 3:\n  x add x i")
        self.assertEqual(r.globals["x"], 6)
        r = self.go("l 3 1:\n  x mul x 10\n  x add x i")                  # downwards
        self.assertEqual(r.globals["x"], 321)
        r = self.go("l 1 2:\n  l 1 3:\n    x add x add mul j 10 i")
        self.assertEqual(r.globals["x"], sum(10 * j + i for j in (1, 2) for i in (1, 2, 3)))
        self.assertEqual(self.gl("x add i j")["x"], 0)                      # 0 outside loops

    def test_break_leaves_only_the_innermost_loop(self):
        r = self.go("l 1 3:\n  l 1 5:\n    if eq i 3: break\n    x add x 1")
        self.assertEqual(r.globals["x"], 6)

    def test_stop_and_ret(self):
        r = self.go("x 1\nstop\nx 2")
        self.assertEqual(r.globals["x"], 1)
        r = self.go("x 3\ny helper | 4\nz helper | 0",
                    extra="def helper | n\n  if n: ret add n 100\n  ret 7\n")
        self.assertEqual((r.globals["y"], r.globals["z"]), (104, 7))
        r = self.go("x helper\ny 5", extra="def helper\n  a 3\n")
        self.assertEqual(r.globals["x"], 0)                                 # no ret = 0

    def test_stop_in_a_callee_stops_only_the_callee(self):
        r = self.go("helper\nx 4", extra="def helper\n  y 9\n  stop\n  y 8\n")
        self.assertEqual((r.globals["x"], r.globals["y"]), (4, 9))

    def test_if_elif_else(self):
        src = "if eq x 1: y 10\nelif eq x 2: y 20\nelse: y 30"
        for x, want in ((1, 10), (2, 20), (3, 30)):
            self.assertEqual(self.gl("x %d\n%s" % (x, src))["y"], want)

    def test_locals_are_per_call(self):
        r = self.go("a 5\nhelper\nx a", extra="def helper\n  a 9\n")
        self.assertEqual(r.globals["x"], 5)

    def test_call_arguments_and_params(self):
        r = self.go("x helper | 3 4", extra="def helper | p q\n  ret add mul p 10 q\n")
        self.assertEqual(r.globals["x"], 34)
        r = self.go("param n 5 1 9\nx n")
        self.assertEqual(r.globals["x"], 5)

    def test_each_runs_over_the_context_steps(self):
        r = self.go("steps 3 5\neach: x add x i")
        self.assertEqual(r.globals["x"], 12)
        r = self.go("steps 3 5\neach: y step.cur")
        self.assertEqual(r.globals["y"], 5)
        r = self.go("step 7: x step.cur")
        self.assertEqual(r.globals["x"], 7)

    @covers("STEP.CUR")
    def test_step_cur(self):
        self.assertEqual(self.gl("step 9: x step.cur")["x"], 9)
        self.assertEqual(self.gl("x step.cur")["x"], 0)           # no step selected yet


# ================================================================================================
class TestContext(Base):
    @covers("TRK", "TR.ADD")
    def test_trk_statement_modifier_and_default(self):
        r = self.go("tr.add 1\ntrk 2 3:\n  tr.add 2\ntr.add 3", track=4)
        self.assertEqual({t: self.trigs(r, t) for t in (1, 2, 3, 4)},
                         {1: [], 2: [2], 3: [2], 4: [1, 3]})
        r = self.go("trk 2\ntr.add 1\ntrk 3:\n  tr.add 2\ntr.add 3")
        self.assertEqual({t: self.trigs(r, t) for t in (1, 2, 3)}, {1: [], 2: [1, 3], 3: [2]})

    def test_trk_cur_all_and_expressions(self):
        r = self.go("trk cur:\n  tr.add 1", track=5)           # at the root, cur = the host track
        self.assertEqual(self.trigs(r, 5), [1])
        r = self.go("trk 2\ntrk cur:\n  tr.add 1", track=5)    # cur = the track being processed
        self.assertEqual((self.trigs(r, 2), self.trigs(r, 5)), ([1], []))
        r = self.go("trk all:\n  tr.add 1")
        self.assertEqual([t for t in range(1, 9) if self.trigs(r, t)], list(range(1, 9)))
        r = self.go("a 4\ntrk a add a 1:\n  tr.add 2")
        self.assertEqual([t for t in range(1, 9) if self.trigs(r, t)], [4, 5])
        r = self.go("trk 2 2 2:\n  tr.add 2")                  # duplicates count once
        self.assertEqual([t for t in range(1, 9) if self.trigs(r, t)], [2])

    def test_trk_bad_numbers_are_dropped_and_counted(self):
        r = self.go("a 9\ntrk 2 a 3:\n  tr.add 1")
        self.assertEqual([t for t in range(1, 9) if self.trigs(r, t)], [2, 3])
        self.assertEqual(len(r.oor), 1)

    @covers("TRK.CUR")
    def test_trk_cur_op_is_the_track_being_processed(self):
        self.assertEqual(self.go("x trk.cur", track=6).globals["x"], 6)       # root: the host track
        self.assertEqual(self.go("trk 2:\n  x trk.cur", track=6).globals["x"], 2)
        self.assertEqual(self.go("trk 3\nx trk.cur", track=6).globals["x"], 3)

    def test_called_function_inherits_a_copy_of_the_context(self):
        r = self.go("trk 2:\n  helper\ntr.add 9",
                    extra="def helper\n  tr.add 7\n  trk 4\n  tr.add 1\n")
        self.assertEqual(self.trigs(r, 2), [7])
        self.assertEqual(self.trigs(r, 4), [1])
        self.assertEqual(self.trigs(r, 1), [9])             # the caller's context is unchanged

    @covers("STEPS", "STEP", "TR.CLR")
    def test_steps_and_step_select_what_bulk_ops_act_on(self):
        r = self.go("l 1 16: tr.add i\nsteps 5 8: tr.clr")
        self.assertEqual(self.trigs(r), [1, 2, 3, 4, 9, 10, 11, 12, 13, 14, 15, 16])
        r = self.go("l 1 16: tr.add i\nstep 3: tr.clr")
        self.assertEqual(self.trigs(r), [s for s in range(1, 17) if s != 3])
        r = self.go("l 1 16: tr.add i\nsteps 8 5: tr.clr")       # either order
        self.assertEqual(self.trigs(r), [s for s in range(1, 17) if not 5 <= s <= 8])
        r = self.go("l 1 16: tr.add i\nsteps 5 8\nsteps all: tr.clr")
        self.assertEqual(self.trigs(r), [])

    @covers("PAT.LEN")
    def test_steps_default_is_the_pattern_length(self):
        r = self.go("l 1 16: tr.add i\ntr.clr", length=8)
        self.assertEqual(self.trigs(r), list(range(9, 17)))        # only steps 1..8 were cleared
        r = self.go("x pat.len", length=12)
        self.assertEqual(r.globals["x"], 12)

    def test_steps_predicate_narrows(self):
        r = self.go("tr.add 3\ntr.add 9\nsteps has.trig:\n  each: pl.set i amp.vol 100")
        self.assertEqual(sorted(self.locks(r)), [3, 9])

    def test_steps_out_of_range_is_skipped_and_counted(self):
        r = self.go("a 70\nsteps 2 a: tr.add 1\ntr.add 4")
        self.assertEqual(len(r.oor), 1)
        r = self.go("a 0\nstep a: tr.add 4")
        self.assertEqual(len(r.oor), 1)
        self.assertEqual(self.trigs(r), [4])      # the line still ran with the context unchanged


# ================================================================================================
class TestPerTrack(Base):
    """Spec 6.2: a `trk` naming several tracks runs its body once per track, in `trk` order, each
    pass with the context of that single track (its own step set, trk.cur = that track)."""

    def test_each_track_keeps_its_own_step_set(self):
        r = self.go("""
            trk 1:
              tr.add 3
            trk 2:
              tr.add 7
            trk 1 2:
              steps has.trig:
                each: pl.set i amp.vol 100
            """)
        self.assertEqual(self.locks(r, 1), {3: {15: 100}})
        self.assertEqual(self.locks(r, 2), {7: {15: 100}})

    def test_predicate_before_trk_is_evaluated_on_the_track_of_that_moment(self):
        r = self.go("tr.add 3\ntrk 2:\n  tr.add 3\n  tr.add 5\n"
                    "steps has.trig\ntrk 1 2:\n  each: pl.set i amp.vol 9", track=1)
        self.assertEqual(self.locks(r, 1), {3: {15: 9}})
        self.assertEqual(self.locks(r, 2), {3: {15: 9}})          # the same set for both tracks

    def test_the_body_runs_once_per_track_in_trk_order(self):
        r = self.go("trk 3 1 2:\n  x add mul x 10 trk.cur")
        self.assertEqual(r.globals["x"], 312)
        r = self.go("trk 1 2 3:\n  x add x 1\n  y add y trk.cur")
        self.assertEqual((r.globals["x"], r.globals["y"]), (3, 6))

    def test_statement_form_runs_the_rest_of_the_block_per_track(self):
        r = self.go("x add x 1\ntrk 1 2\nx add x 10\ntr.add 5")
        self.assertEqual(r.globals["x"], 21)                      # once before, twice after
        self.assertEqual((self.trigs(r, 1), self.trigs(r, 2)), ([5], [5]))
        r = self.go("trk 1 2:\n  trk 3 4:\n    x add x 1")      # nested: 2 x 2 passes
        self.assertEqual(r.globals["x"], 4)

    def test_random_draw_order_is_track_by_track(self):
        v = VEC[1]["RAND"]
        r = self.go("trk 1 2:\n  tr.add 1\n  tr.add 2\ntrk 2 1:\n  steps 1 2\n  pl.rnd amp.vol")
        self.assertEqual(self.locks(r, 2), {1: {15: below(v[0], 128)}, 2: {15: below(v[1], 128)}})
        self.assertEqual(self.locks(r, 1), {1: {15: below(v[2], 128)}, 2: {15: below(v[3], 128)}})
        # the whole body of track 1, then the whole body of track 2
        r = self.go("trk 1 2:\n  tr.add 1\ntrk 1 2:\n  step 1\n  pl.rnd amp.vol\n  cond.rnd 50% 99%")
        self.assertEqual(self.locks(r, 1)[1][15], below(v[0], 128))
        self.assertEqual(r.model.tracks[1].cond[1], 19 + below(v[1], 11))
        self.assertEqual(self.locks(r, 2)[1][15], below(v[2], 128))
        self.assertEqual(r.model.tracks[2].cond[1], 19 + below(v[3], 11))

    def test_value_reads_look_at_the_track_being_processed(self):
        r = self.go("trk 1:\n  tr.add 4\ntrk 1 2:\n  x add mul x 10 has.trig 4")
        self.assertEqual(r.globals["x"], 10)                      # track 1: 1, track 2: 10 + 0
        r = self.go("trk 2:\n  tr.add 3\n  smp.set 3 9\ntrk 1 2:\n  x add mul x 100 smp.get 3")
        self.assertEqual(r.globals["x"], 9)
        r = self.go("trk 2:\n  tr.add 3\n  smp.set 3 9\ntrk 2 1:\n  x add mul x 100 smp.get 3")
        self.assertEqual(r.globals["x"], 900)
        r = self.go("trk 2:\n  tr.add 3\n  pl.set 3 amp.vol 55\ntrk 2 1:\n  x add mul x 200 pl.get 3 amp.vol")
        self.assertEqual(r.globals["x"] // 200, 55)      # track 2 first (55), then track 1 on top

    def test_each_iterates_the_tracks_own_steps(self):
        r = self.go("trk 1:\n  tr.add 3\ntrk 2:\n  tr.add 7\n  tr.add 9\n"
                    "trk 1 2:\n  steps has.trig:\n    each: x add x 1")
        self.assertEqual(r.globals["x"], 3)

    def test_a_user_call_runs_once_per_track_with_that_track_current(self):
        r = self.go("trk 2 4:\n  helper", extra="def helper\n  x add x 1\n  tr.add trk.cur\n")
        self.assertEqual(r.globals["x"], 2)
        self.assertEqual((self.trigs(r, 2), self.trigs(r, 4)), ([2], [4]))

    def test_modifiers_inside_the_pass_run_per_track(self):
        r = self.go("trk 1 2:\n  prob 50: x add x 1")
        self.assertEqual(r.draws["PROB"], 2)                      # a draw per track
        r = self.go("trk 1 2: every 2: x add x 1")                # the counter counts passes
        self.assertEqual(r.globals["x"], 1)
        r = self.go("prob 100: trk 1 2: x add x 1")               # modifiers before the trk: once
        self.assertEqual((r.globals["x"], r.draws["PROB"]), (2, 1))

    def test_break_stop_and_ret_end_everything_not_one_pass(self):
        r = self.go("l 1 3:\n  trk 1 2:\n    x add x 1\n    break")
        self.assertEqual(r.globals["x"], 1)
        r = self.go("trk 1 2:\n  x add x 1\n  stop\ny 5")
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 0))
        r = self.go("x helper\ny 7", extra="def helper\n  trk 1 2:\n    ret 3\n")
        self.assertEqual(r.globals["x"], 3)

    def test_no_valid_track_runs_nothing(self):
        r = self.go("a 9\ntrk a:\n  x 1\ntrk a\ny 1")
        self.assertEqual((r.globals["x"], r.globals["y"], len(r.oor)), (0, 0, 2))

    def test_single_track_forms_do_not_repeat_anything(self):
        r = self.go("trk 2:\n  x add x 1\ntrk 3\ny add y 1")
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 1))

    def test_seed_inside_a_pass_is_scoped_to_that_pass(self):
        r = self.go("trk 1 2:\n  seed 0\n  x add x rnd 1000\ny rnd 1000")
        self.assertEqual(r.globals["x"], 2 * below(VEC[0]["RAND"][0], 1000))
        self.assertEqual(r.globals["y"], below(VEC[1]["RAND"][0], 1000))

    def test_cost_stays_within_the_checkers_bound_for_several_tracks(self):
        r = self.go("trk all:\n  tr.clr\n  l 1 4: tr.add i\ntrk 2 3\ntr.clr")
        self.assertLessEqual(r.cost, r.max_cost)
        self.assertGreater(r.cost, 8 * 128)


# ================================================================================================
class TestTrigFamily(Base):
    """tr.add/del/clr/flip and has.trig act on the red trig, the trigless trig and the
    one-shot; the trigless locks belong to tl.* alone."""

    def bank(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank01.work"), pattern=1)   # step 3: trigless
        with open(path, "r+b") as fh:
            fh.seek(sim.trac_off(0, 0) + 0x18 + 6)           # one-shot at step 9
            fh.write(bytes([0b00000001]))
            fh.seek(sim.trac_off(0, 0) + 0x10 + 7)           # trigless lock at step 8
            fh.write(bytes([0b10000000]))
        return path

    def test_has_trig_sees_all_three_but_not_the_trigless_lock(self):
        r = self.go("x has.trig 1\ny has.trig 3\nz has.trig 9\nt has.trig 8", bank=self.bank())
        self.assertEqual([r.globals[k] for k in "xyzt"], [1, 1, 1, 0])

    def test_tr_clr_del_flip_remove_the_whole_family_and_leave_trigless_locks(self):
        b = self.bank()
        r = self.go("tr.clr", bank=b)
        td = r.model.tracks[1]
        self.assertEqual([td.masks[m] for m in ("trig", "tless", "oneshot")], [0, 0, 0])
        self.assertTrue(td.has("tlock", 8))
        r = self.go("tr.del 3\ntr.del 9", bank=b)
        self.assertEqual((r.model.tracks[1].masks["tless"], r.model.tracks[1].masks["oneshot"]), (0, 0))
        self.assertTrue(r.model.tracks[1].has("trig", 1))
        r = self.go("tr.flip 3\ntr.flip 9\ntr.flip 10", bank=b)         # present: off; absent: red trig
        td = r.model.tracks[1]
        self.assertEqual((td.has_trig(3), td.has_trig(9), td.has("trig", 10)), (False, False, True))

    def test_tl_ops_write_trigless_locks_only(self):
        r = self.go("tl.add 2\ntl.fill 100")
        td = r.model.tracks[1]
        self.assertTrue(td.has("tlock", 2))
        self.assertEqual((td.masks["trig"], td.masks["tless"], td.masks["oneshot"]), (0, 0, 0))
        r = self.go("tl.add 2\ntr.clr")
        self.assertTrue(r.model.tracks[1].has("tlock", 2))                # tr.clr leaves it


# ================================================================================================
class TestTrigRules(Base):
    """Removing a trig acts as the stock
    release (locks, sample lock and condition go unless a trigless lock keeps the step; micro and
    count stay), tr.add on a one-shot / trigless trig keeps the step's data, tr.add on an empty
    step resets the trig word, and a PICKUP track refuses every write (counted apart)."""

    FILL = ("pl.set 2 amp.vol 9\nsmp.set 2 3\ncond.set 2 50%\nmicro.set 2 5\nratch.set 2 3\n")

    def bank(self):
        return TestTrigFamily.bank(self)         # step 3 trigless trig, 8 trigless lock, 9 one-shot

    @staticmethod
    def word(res, s, t=1):
        td = res.model.tracks[t]
        return (td.cond.get(s), td.micro.get(s), td.count.get(s))

    def test_removal_wipes_locks_smp_and_cond_but_keeps_micro_and_count(self):
        for rm in ("tr.del 2", "tr.flip 2", "tr.clr"):
            with self.subTest(rm):
                r = self.go("tr.add 2\n" + self.FILL + rm)
                td = r.model.tracks[1]
                self.assertFalse(td.has_trig(2))
                self.assertEqual((td.locks, td.smp, td.cond), ({}, {}, {}))
                self.assertEqual((td.micro, td.count), ({2: 5}, {2: 3}))

    def test_removal_with_a_trigless_lock_clears_only_the_trig_bits(self):
        for rm in ("tr.del 2", "tr.flip 2", "tr.clr"):
            with self.subTest(rm):
                r = self.go("tl.add 2\ntr.add 2\n" + self.FILL + rm)
                td = r.model.tracks[1]
                self.assertFalse(td.has_trig(2))
                self.assertTrue(td.has("tlock", 2))
                self.assertEqual(self.locks(r), {2: {15: 9}})
                self.assertEqual((td.smp, td.cond, td.micro, td.count),
                                 ({2: 3}, {2: 19}, {2: 5}, {2: 3}))

    def test_removal_on_a_step_without_a_trig_wipes_nothing(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank01.work"), pattern=1)
        with open(path, "r+b") as fh:
            fh.seek(sim.trac_off(0, 0) + 0x59 + 9 * 0x20 + 15)          # step 10: lock, no trig
            fh.write(bytes([50]))
        r = self.go("tr.del 10\ntr.flip 10\ntr.del 11", bank=path, pattern=1)
        self.assertEqual(self.locks(r)[10], {15: 50})                     # flip added a red trig only
        self.assertTrue(r.model.tracks[1].has("trig", 10))

    def test_tr_add_on_a_one_shot_or_trigless_trig_keeps_the_step(self):
        b = self.bank()
        for s, mask in ((9, "oneshot"), (3, "tless")):
            with self.subTest(mask):
                body = "".join(x.replace(" 2 ", " %d " % s) for x in self.FILL.splitlines(True))
                before = self.go(body, bank=b).model.tracks[1]
                self.assertTrue(before.has(mask, s))
                r = self.go(body + "tr.add %d" % s, bank=b)
                td = r.model.tracks[1]
                self.assertEqual((td.has("trig", s), td.has("oneshot", s), td.has("tless", s)),
                                 (True, False, False))
                self.assertEqual((td.locks[s], td.smp[s], td.cond[s], td.micro[s], td.count[s]),
                                 (before.locks[s], 3, 19, 5, 3))

    def test_tr_add_on_an_empty_step_resets_the_trig_word_and_keeps_the_locks(self):
        r = self.go("tl.add 2\ntr.add 2\n" + self.FILL + "tr.del 2\n")
        self.assertEqual(self.word(r, 2), (19, 5, 3))                     # left by the removal
        r = self.go("tl.add 2\ntr.add 2\n" + self.FILL + "tr.del 2\ntr.add 2")
        td = r.model.tracks[1]
        self.assertTrue(td.has("trig", 2))
        self.assertEqual(self.word(r, 2), (None, None, None))
        self.assertEqual((self.locks(r), td.smp), ({2: {15: 9}}, {2: 3}))
        r = self.go("tr.add 2\nmicro.set 2 4\ntr.del 2\ntr.add 2")
        self.assertEqual(self.word(r, 2), (None, None, None))             # micro stayed, tr.add reset it

    def test_tr_add_on_a_red_trig_changes_nothing(self):
        a = self.go("tr.add 2\n" + self.FILL)
        b = self.go("tr.add 2\n" + self.FILL + "tr.add 2")
        self.assertEqual(a.model.to_json(), b.model.to_json())
        self.assertEqual(self.word(b, 2), (19, 5, 3))


def pickup_sim(prog, pickups, **kw):
    """Run `main` with the tracks in `pickups` set to machine type 4 (PICKUP)."""
    class R(sim.Run):
        def __init__(self, *a):
            super().__init__(*a)
            for t in pickups:
                self.model.part[t].mtype = 4
    return sim.simulate(prog, "main", run_class=R, **kw)


class TestPickupTracks(Base):
    """A PICKUP track (type 4) refuses every write; the skipped op is counted in skipped_pickup,
    not in skipped_no_trig; reads still answer; the skipped track draws nothing."""

    def pk(self, body, pickups=(1,), **kw):
        return pickup_sim(self.prog(body), pickups, seed=kw.pop("seed", 3), **kw)

    def test_writes_are_skipped_and_counted(self):
        body = "tr.add 2\npl.set 2 amp.vol 9\nsmp.rnd 3 5\ntl.fill 100"
        r = self.pk(body)
        td = r.model.tracks[1]
        self.assertEqual((td.masks["trig"], td.masks["tlock"], td.locks, td.smp), (0, 0, {}, {}))
        self.assertEqual(r.draws["RAND"], 0)
        self.assertEqual(r.skipped, [])
        self.assertEqual([(f, o, ln, t) for f, o, ln, t in r.skipped_pickup],
                         [("main", "tr.add", 3, 1), ("main", "pl.set", 4, 1),
                          ("main", "smp.rnd", 5, 1), ("main", "tl.fill", 6, 1)])
        d = sim.result_json(r)
        self.assertEqual(d["skipped_pickup"][0], {"func": "main", "op": "tr.add", "line": 3, "track": 1})
        self.assertEqual(len(d["skipped_pickup"]), 4)
        self.assertEqual(d["skipped_no_trig"], [])

    def test_every_writer_is_skipped(self):
        for body in ("tr.add 2", "tr.del 2", "tr.flip 2", "tr.clr", "tl.add 2", "tl.fill 50",
                     "pl.set 2 amp.vol 9", "pl.del 2 amp.vol", "pl.rnd amp.vol", "pl.vary amp.vol 10",
                     "pl.clr", "smp.set 2 3", "smp.del 2", "smp.rnd 3 5", "smp.clr",
                     "cond.set 2 50%", "cond.rnd 50% 99%", "micro.set 2 5", "micro.rnd 5", "ratch.set 2 3"):
            with self.subTest(body):
                r = self.pk(body)
                self.assertEqual(r.model.tracks[1].to_json(), sim.TrackData().to_json())
                self.assertEqual(len(r.skipped_pickup), 1)
                self.assertEqual(r.draws["RAND"], 0)

    def test_a_pickup_track_leaves_its_data_alone(self):
        b = TestTrigFamily.bank(self)
        r = self.pk("tr.clr\ntl.add 1\npl.set 1 amp.vol 9\nsmp.clr", bank=b)
        base = self.go("x 0", bank=b)
        self.assertEqual(r.model.tracks[1].to_json(), base.model.tracks[1].to_json())
        self.assertEqual(len(r.skipped_pickup), 4)

    def test_reads_still_answer(self):
        b = TestTrigFamily.bank(self)
        r = self.pk("x has.trig 1\ny has.trig 3\nz trk.type\nt is.empty 2\ntr.clr", bank=b)
        self.assertEqual([r.globals[k] for k in "xyzt"], [1, 1, 4, 1])
        r = self.pk("x pl.get 1 amp.vol\ny smp.get 1\nz has.smp 1", bank=b)
        self.assertEqual(r.skipped_pickup, [])

    def test_the_other_track_gets_the_same_result_and_the_same_draws(self):
        inner = ("l 1 6: tr.add i\nsteps 1 8: pl.rnd amp.vol\nsteps 1 8: smp.rnd 3 9\n"
                 "steps 1 8: cond.rnd 50% 99%\nsteps 1 8: micro.rnd 5\nsteps 1 8: pl.vary amp.vol 10\n"
                 "tl.fill 40\nratch.set 2 3")
        indent = lambda hd: hd + "\n" + "".join("  " + x + "\n" for x in inner.split("\n"))
        alone = self.pk(indent("trk 1:"), pickups=(2,))
        both = self.pk(indent("trk 1 2:"), pickups=(2,))
        self.assertEqual(alone.skipped_pickup, [])
        self.assertTrue(both.skipped_pickup)
        self.assertEqual({t for f, o, ln, t in both.skipped_pickup}, {2})
        self.assertEqual(both.model.tracks[1].to_json(), alone.model.tracks[1].to_json())
        self.assertEqual(both.model.tracks[2].to_json(), sim.TrackData().to_json())
        self.assertEqual(both.draws, alone.draws)
        self.assertEqual(both.skipped, alone.skipped)

    def test_text_report_names_the_pickup_skips(self):
        t = sim.render_text(self.pk("tr.add 2\ntr.add 3"))
        self.assertIn("main: 2 skipped, PICKUP track (tr.add line 3 track 1, tr.add line 4 track 1)", t)
        self.assertNotIn("PICKUP", sim.render_text(self.go("tr.add 2")))

    def test_a_static_track_is_not_affected(self):
        r = self.pk("tr.add 2", pickups=(2,))
        self.assertEqual(self.trigs(r), [2])
        self.assertEqual(r.skipped_pickup, [])


# ================================================================================================
class TestLocksSkipEmptySteps(Base):
    """A lock (pl.set pl.rnd pl.vary smp.set smp.rnd) or a trig-word
    write (cond.set cond.rnd micro.set micro.rnd ratch.set) lands only on a step that carries a trig of
    any kind (red, trigless trig, trigless lock, one-shot).  Elsewhere it is skipped and counted
    apart from out-of-range.  Draw rule (5.3.2): the random draw of a per-step op is made on EVERY
    step of the context, then the step is checked: skipped steps still draw."""

    WRITERS = (("pl.set 2 amp.vol 9", "pl.set"), ("smp.set 2 3", "smp.set"),
               ("cond.set 2 50%", "cond.set"), ("micro.set 2 5", "micro.set"),
               ("ratch.set 2 3", "ratch.set"), ("step 2: pl.rnd amp.vol", "pl.rnd"),
               ("step 2: smp.rnd 3 5", "smp.rnd"), ("step 2: cond.rnd 50% 99%", "cond.rnd"),
               ("step 2: micro.rnd 20", "micro.rnd"))

    @staticmethod
    def untouched(res, t=1):
        td = res.model.tracks[t]
        return not (td.locks or td.smp or td.cond or td.micro or td.count)

    def test_every_writer_is_skipped_on_an_empty_step_and_counted(self):
        for body, name in self.WRITERS:
            with self.subTest(name):
                r = self.go(body, seed=3)
                self.assertTrue(self.untouched(r))
                self.assertEqual([(f, o) for f, o, ln in r.skipped], [("main", name)])
                self.assertEqual(r.oor, [])
                r = self.go("tr.add 2\n" + body, seed=3)                    # a red trig: it lands
                self.assertFalse(self.untouched(r))
                self.assertEqual(r.skipped, [])

    def test_pl_vary_is_skipped_where_a_lock_has_no_trig(self):
        # a lock without a trig can only come from a bank file; vary leaves it alone and counts it
        path = synthetic_bank(os.path.join(self.tmp(), "bank01.work"), pattern=1)
        with open(path, "r+b") as fh:
            fh.seek(sim.trac_off(0, 0) + 0x59 + 9 * 0x20 + 15)          # step 10: lock, no trig
            fh.write(bytes([50]))
        r = self.go("steps 1 12: pl.vary amp.vol 10", bank=path, pattern=1)
        self.assertEqual(self.locks(r)[10], {15: 50})                    # step 10 untouched
        self.assertEqual([ln for f, o, ln in r.skipped], [3])             # one skip: step 10
        self.assertEqual(r.draws["RAND"], 2)                             # steps 3 and 10 hold the lock
        self.assertNotEqual(self.locks(r)[3][15], 100)                   # step 3 has a trigless trig

    def test_every_kind_of_trig_accepts_a_lock(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank01.work"), pattern=1)
        with open(path, "r+b") as fh:
            fh.seek(sim.trac_off(0, 0) + 0x18 + 6)
            fh.write(bytes([0b00000001]))                                # one-shot, step 9
            fh.seek(sim.trac_off(0, 0) + 0x10 + 7)
            fh.write(bytes([0b10000000]))                                # trigless lock, step 8
        r = self.go("l 1 10: pl.set i amp.atk 5", bank=path, pattern=1)  # 1 3 5 8 9 carry a trig
        self.assertEqual(sorted(self.locks(r)), [1, 3, 5, 8, 9])
        self.assertEqual(len(r.skipped), 5)                              # steps 2 4 6 7 10

    def test_a_lock_on_an_empty_step_needs_a_trigless_lock_first(self):
        r = self.go("tl.add 4\npl.set 4 amp.vol 9\npl.set 5 amp.vol 9")
        self.assertEqual(self.locks(r), {4: {15: 9}})
        self.assertEqual(len(r.skipped), 1)
        r = self.go("tl.fill 100\nsteps 1 16: pl.rnd amp.vol")
        self.assertEqual((len(self.locks(r)), len(r.skipped), r.draws["RAND"]), (16, 0, 32))

    def test_skipped_is_not_out_of_range(self):
        r = self.go("a 70\npl.set a amp.vol 1\npl.set 2 amp.vol 1")
        self.assertEqual((len(r.oor), len(r.skipped)), (1, 1))
        self.assertEqual(r.skipped, [("main", "pl.set", 5)])

    def test_draws_are_made_on_skipped_steps_too(self):
        v = VEC[1]["RAND"]
        r = self.go("tr.add 2\nsteps 1 4: pl.rnd amp.vol\nx rnd 1000")
        self.assertEqual(self.locks(r), {2: {15: below(v[1], 128)}})      # step 2 = the 2nd draw
        self.assertEqual(r.draws["RAND"], 5)                             # 4 steps + the rnd
        self.assertEqual(r.globals["x"], below(v[4], 1000))
        self.assertEqual(len(r.skipped), 3)
        # the same stream position whatever the trigs are
        r2 = self.go("l 1 4: tr.add i\nsteps 1 4: pl.rnd amp.vol\nx rnd 1000")
        self.assertEqual(r2.globals["x"], r.globals["x"])
        for body in ("steps 1 4: smp.rnd 3 9", "steps 1 4: cond.rnd 50% 99%", "steps 1 4: micro.rnd 5"):
            self.assertEqual(self.go(body).draws["RAND"], 4, body)       # nothing lands, 4 draws

    def test_the_run_line_names_the_ops(self):
        t = sim.render_text(self.go("pl.set 2 amp.vol 1\npl.set 3 amp.vol 1\nsteps 1 3: smp.rnd 3 3"))
        self.assertIn("main: 5 skipped, no trig (pl.set line 3, pl.set line 4, smp.rnd line 5 x3)", t)
        self.assertNotIn("out of range", t)
        self.assertNotIn("skipped", sim.render_text(self.go("tr.add 2\npl.set 2 amp.vol 1")))
        t = sim.render_text(self.go("a 70\ntr.add a\nsmp.set 2 3"))
        self.assertIn("main: 1 out of range (tr.add line 4)\nmain: 1 skipped, no trig (smp.set line 5)", t)

    def test_multi_track_counts_each_track(self):
        r = self.go("trk 1 2:\n  tr.add 3\ntrk 1 2 3:\n  pl.set 3 amp.vol 9")
        self.assertEqual(len(r.skipped), 1)                              # track 3 only
        self.assertEqual((self.locks(r, 1), self.locks(r, 2), self.locks(r, 3)),
                         ({3: {15: 9}}, {3: {15: 9}}, {}))

    def test_json_report_lists_the_skipped_writes(self):
        d = sim.result_json(self.go("ratch.set 2 3"))
        self.assertEqual(d["skipped_no_trig"], [{"func": "main", "op": "ratch.set", "line": 3}])
        self.assertEqual(d["out_of_range"], [])

    def test_the_vm_agrees(self):
        from modules.otype import vm
        body = ("tr.add 2\ntl.add 6\nsteps 1 8: pl.rnd amp.vol\nsteps 1 8: cond.rnd 50% 99%\n"
                "pl.set 4 amp.vol 3\nsmp.set 6 4\nratch.set 2 3\nsteps 1 8: pl.vary amp.vol 10\n"
                "steps 1 8: micro.rnd 4\nsteps 1 8: smp.rnd 3 9")
        prog = self.prog(body)
        a = sim.result_json(sim.simulate(prog, "main", seed=5))
        b = sim.result_json(vm.run_vm(prog, "main", seed=5))
        self.assertEqual(a, b)
        self.assertTrue(a["skipped_no_trig"])


# ================================================================================================
class TestStepsRange(Base):
    def test_steps_a_b_replaces_it_is_absolute(self):
        r = self.go("l 1 16: tr.add i\nsteps 3 8\nsteps 5 12\ntr.clr")
        self.assertEqual(self.trigs(r), [s for s in range(1, 17) if not 5 <= s <= 12])
        r = self.go("steps 20 24\neach: x add x 1")             # beyond the pattern length: allowed
        self.assertEqual(r.globals["x"], 5)

    def test_only_a_predicate_narrows(self):
        r = self.go("tr.add 4\ntr.add 9\nsteps 1 6\nsteps has.trig\neach: x add x i")
        self.assertEqual(r.globals["x"], 4)                       # 1..6 narrowed to the trig at 4


# ================================================================================================
def euclid(k, n, r):
    """k pulses over n steps (step i fires when i*k mod n < k), rotated r: the model of a euclidean generator page's EUCL mode
    (the reference model `euclid`)."""
    return {(i + r) % n for i in range(n) if (i * k) % n < k}


def generator(pl1, pl2, op, r1, r2, tro, n):
    """the reference model `gen`: the steps (0-based) the EUCL mode lights."""
    g1, g2 = euclid(pl1, n, r1), euclid(pl2, n, r2)
    g = {0: g1 | g2, 1: g1 ^ g2, 2: g1 & g2, 3: g1 - g2}[op]
    return {(s + tro) % n for s in g}


class TestPage(Base):
    """Page functions (`page "LABEL"`, `param` without `menu`) run like any root function; `--param` overrides the params."""
    PAGE = 'def pg\n  page "PG"\n  param a1 5 1 9\n  param a2 2 0 3\n  x a1\n  y a2\n'

    def test_run_param_on_a_page_function(self):
        prog = otype.check_sources([("T.OTY", self.PAGE)])
        self.assertEqual([str(d) for d in prog.diags], [])
        self.assertEqual(sim.simulate(prog, "pg", seed=1).globals["x"], 5)
        r = sim.simulate(prog, "pg", seed=1, params={"a1": 8, "a2": 1})
        self.assertEqual((r.globals["x"], r.globals["y"]), (8, 1))
        r = sim.simulate(prog, "pg", seed=1, params={"A1": 99, "a2": -4})          # clamped to min..max, case-free
        self.assertEqual((r.globals["x"], r.globals["y"]), (9, 0))
        self.assertEqual(len(r.warnings), 2)

    def test_cli_param_on_a_page_function(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "p.OTY")
        with open(path, "w") as fh:
            fh.write(self.PAGE)
        out, err = io.StringIO(), io.StringIO()
        code = sim.main(["pg", path, "--param", "a1=7", "--json"], checker=otype, out=out, err=err)
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(json.loads(out.getvalue())["globals"]["x"], 7)
        out, err = io.StringIO(), io.StringIO()
        code = sim.main(["pg", path, "--vm", "--param", "a1=7", "--param", "a2=3", "--json"], checker=otype, out=out, err=err)
        self.assertEqual((code, err.getvalue()), (0, ""))
        j = json.loads(out.getvalue())
        self.assertEqual((j["globals"]["x"], j["globals"]["y"]), (7, 3))

    def test_a_page_function_with_a_card_op_is_refused_by_run(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "p.OTY")
        with open(path, "w") as fh:
            fh.write('def pg\n  page "PG"\n  dir "A"\n  tr.add 1\n')
        out, err = io.StringIO(), io.StringIO()
        code = sim.main(["pg", path], checker=otype, out=out, err=err)
        self.assertEqual(code, 1)
        self.assertIn("op dir not allowed in a page function", out.getvalue())

    def test_example_eucl_is_the_generators_euclidean_mode(self):
        prog = otype.check_files([os.path.join(ROOT, "modules", "otype", "examples", "PAGE.OTY")])
        self.assertEqual([str(d) for d in prog.diags], [])
        cases = [dict(pl1=4), dict(pl1=5, pl2=3), dict(pl1=5, pl2=3, op=1), dict(pl1=7, pl2=4, op=2, r1=2, r2=-3),
                 dict(pl1=9, pl2=5, op=3, r1=1, r2=4, tro=3), dict(pl1=3, pl2=0, tro=-2), dict(pl1=0, pl2=6, op=1, r2=5, tro=63),
                 dict(pl1=16, pl2=1), dict(pl1=64, pl2=64, op=3), dict(pl1=6, pl2=6, op=1, r1=-63, r2=63, tro=-63)]
        for n in (16, 12, 7, 32, 64):
            for kw in cases:
                full = dict(pl1=4, pl2=0, op=0, r1=0, r2=0, tro=0)
                full.update(kw)
                with self.subTest(n=n, **kw):
                    r = sim.simulate(prog, "eucl", seed=1, params=kw, length=n)
                    want = {s + 1 for s in generator(full["pl1"], full["pl2"], full["op"], full["r1"], full["r2"], full["tro"], n)}
                    self.assertEqual(set(self.trigs(r)), want)

    def test_example_eucl_defaults_and_no_pulses(self):
        prog = otype.check_files([os.path.join(ROOT, "modules", "otype", "examples", "PAGE.OTY")])
        r0 = sim.simulate(prog, "eucl", seed=1, params=dict(pl1=0))
        self.assertEqual(self.trigs(r0), [])
        r = sim.simulate(prog, "eucl", seed=1, params=dict(pl1=4), length=16)
        self.assertEqual(self.trigs(r), [1, 5, 9, 13])

    def test_example_density_page(self):
        prog = otype.check_files([os.path.join(ROOT, "modules", "otype", "examples", "PAGE.OTY")])
        none = sim.simulate(prog, "pdens", seed=2, params=dict(pct=0))
        full = sim.simulate(prog, "pdens", seed=2, params=dict(pct=100))
        half = sim.simulate(prog, "pdens", seed=2, params=dict(pct=50))
        self.assertEqual(self.trigs(none), [])
        self.assertEqual(self.trigs(full), list(range(1, 17)))
        self.assertTrue(0 < len(self.trigs(half)) < 16)
        self.assertEqual(self.trigs(half), self.trigs(sim.simulate(prog, "pdens", seed=2, params=dict(pct=50))))


# ================================================================================================
class TestCoverage(Base):
    @covers("TR.ADD", "TR.DEL", "TR.FLIP")
    def test_add_del_flip(self):
        r = self.go("tr.add 3\ntr.add 5\ntr.del 3\ntr.flip 5\ntr.flip 7")
        self.assertEqual(self.trigs(r), [7])
        r = self.go("trk 1 2:\n  tr.add 6")
        self.assertEqual((self.trigs(r, 1), self.trigs(r, 2)), ([6], [6]))

    @covers("TL.ADD")
    def test_tl_add_is_a_trigless_lock_not_a_trig(self):
        r = self.go("tl.add 4")
        self.assertEqual(self.trigs(r), [])
        self.assertTrue(r.model.tracks[1].has(sim.TL_MASK, 4))
        self.assertEqual(r.model.tracks[1].masks["trig"], 0)

    @covers("TL.FILL")
    def test_tl_fill(self):
        r = self.go("tr.add 2\ntl.fill 100")
        td = r.model.tracks[1]
        self.assertEqual([s for s in range(1, 17) if td.has(sim.TL_MASK, s)],
                         [s for s in range(1, 17) if s != 2])        # free steps only
        self.assertEqual(r.draws["RAND"], 16)                        # one draw per step
        r = self.go("tl.fill 0")
        self.assertEqual(r.model.tracks[1].masks[sim.TL_MASK], 0)
        r = self.go("a 400\ntl.fill a")                              # pct clamps to 100
        self.assertEqual(bin(r.model.tracks[1].masks[sim.TL_MASK]).count("1"), 16)

    def test_tl_fill_probability_uses_the_rand_stream(self):
        r = self.go("steps 1 5: tl.fill 50")
        td = r.model.tracks[1]
        self.assertEqual([s for s in range(1, 6) if td.has(sim.TL_MASK, s)],
                         [i + 1 for i in range(5) if below(VEC[1]["RAND"][i], 100) < 50])

    def test_bulk_ops_charge_their_cost(self):
        r = self.go("tr.clr")
        self.assertGreaterEqual(r.cost, O.OPS["TR.CLR"].cost)

    def test_out_of_range_step_is_skipped_and_counted(self):
        r = self.go("l 1 3: tr.add add i 62")
        self.assertEqual(self.trigs(r), [63, 64])
        self.assertEqual(len(r.oor), 1)

    def test_out_of_range_still_evaluated_the_arguments(self):
        r = self.go("a 70\ntr.add add a rnd 10")
        self.assertEqual(r.draws["RAND"], 1)
        self.assertEqual(len(r.oor), 1)
        self.assertEqual(self.trigs(r), [])

    def test_out_of_range_kinds(self):
        r = self.go("a 0\nb 129\nc 30\nd 65\ny 9\ntr.add a\nsmp.set 1 b\npl.set 1 c 5\ncond.set 1 d\n"
                    "static.clr a\nz trk.type y")
        self.assertEqual(len(r.oor), 6)
        self.assertEqual((self.trigs(r), r.model.tracks[1].locks, r.model.tracks[1].smp), ([], {}, {}))

    def test_out_of_range_counts_are_reported_with_function_and_line(self):
        r = self.go("a 70\ntr.add a\ntr.add a")
        self.assertEqual([(f, o) for f, o, ln in r.oor], [("main", "tr.add")] * 2)
        self.assertEqual([ln for f, o, ln in r.oor], [4, 5])           # line 2 is the menu
        text = sim.render_text(r)
        self.assertIn("main: 2 out of range", text)


# ================================================================================================
class TestLockOps(Base):
    @covers("PL.SET", "PL.GET")
    def test_pl_set_get_and_clamp(self):
        r = self.go("tr.add 3\ntr.add 4\ntr.add 5\npl.set 3 amp.vol 100\npl.set 4 amp.vol 300\npl.set 5 amp.vol -9\n"
                    "x pl.get 3 amp.vol\ny pl.get 4 amp.vol\nz pl.get 5 amp.vol")
        self.assertEqual(self.locks(r), {3: {15: 100}, 4: {15: 127}, 5: {15: 0}})
        self.assertEqual([r.globals[k] for k in "xyz"], [100, 127, 0])

    def test_pl_get_without_lock_reads_the_part_parameter(self):
        r = self.go("tr.add 3\npar.set amp.vol 77\nx pl.get 3 amp.vol\npl.set 3 amp.vol 5\ny pl.get 3 amp.vol")
        self.assertEqual((r.globals["x"], r.globals["y"]), (77, 5))

    @covers("PL.DEL")
    def test_pl_del(self):
        r = self.go("tr.add 3\npl.set 3 amp.vol 100\npl.set 3 amp.atk 9\npl.del 3 amp.vol\npl.del 8 amp.vol")
        self.assertEqual(self.locks(r), {3: {12: 9}})
        r = self.go("tr.add 3\npl.set 3 amp.vol 100\npl.del 3 amp.vol")
        self.assertEqual(self.locks(r), {})                          # no empty step record left

    @covers("PL.RND")
    def test_pl_rnd(self):
        r = self.go("tr.add 1\ntr.add 2\ntr.add 3\nsteps 1 3: pl.rnd amp.vol")
        self.assertEqual(self.locks(r), {i + 1: {15: below(VEC[1]["RAND"][i], 128)} for i in range(3)})
        self.assertEqual(r.draws["RAND"], 3)

    @covers("PL.VARY")
    def test_pl_vary(self):
        # x = 10 %: d = (127*10 + 50) // 100 = 13; a draw below(2d+1) - d on locked steps only
        r = self.go("tr.add 2\ntr.add 4\npl.set 2 amp.vol 100\npl.set 4 amp.atk 50\npl.vary amp.vol 10")
        d = 13
        self.assertEqual(self.locks(r), {2: {15: 100 + below(VEC[1]["RAND"][0], 2 * d + 1) - d},
                                         4: {12: 50}})
        self.assertEqual(r.draws["RAND"], 1)                       # step 4 holds no amp.vol lock
        r = self.go("tr.add 2\npl.set 2 amp.vol 100\npl.vary amp.vol 0")
        self.assertEqual(self.locks(r), {2: {15: 100}})
        r = self.go("tr.add 2\npl.set 2 amp.vol 127\nl 1 10: pl.vary amp.vol 100")
        self.assertTrue(0 <= self.locks(r)[2][15] <= 127)

    def test_m3a_pl_set_clamps_into_the_part_descriptor_and_refuses_a_disabled_slot(self):
        """The range and the enabled flag of a parameter come from the part
        (sim.ParamModel: the host twin of otb_host_param_info), the neutral part being 0..127."""
        pm = sim.ParamModel({(1, 15): (10, 50, True), (1, 12): (0, 128, False)})
        r = self.go("tr.add 1\npl.set 1 amp.vol 3\npl.set 1 amp.vol 100\npl.set 1 amp.atk 5", pmodel=pm)
        self.assertEqual(self.locks(r), {1: {15: 59}})                      # 3 -> 10 then 100 -> 59; amp.atk refused
        self.assertEqual([o["op"] for o in sim.result_json(r)["out_of_range"]], ["pl.set"])
        r = self.go("tr.add 1\npl.set 1 amp.vol 3", pmodel=sim.ParamModel())
        self.assertEqual(self.locks(r), {1: {15: 3}})                       # the neutral part
        r = self.go("pl.set 3 amp.atk 5\npl.rnd amp.atk\npl.vary amp.atk 9", pmodel=pm)
        self.assertEqual((r.skipped, r.draws["RAND"]), ([], 0))             # the slot is refused first: no "no trig", no draw
        self.assertEqual(len(sim.result_json(r)["out_of_range"]), 3)

    def test_m3a_pl_get_answers_the_part_where_the_step_holds_no_lock(self):
        pm = sim.ParamModel(values={(1, 15): 99, (1, 16): 70000})
        r = self.go("tr.add 1\npl.set 1 amp.vol 7\nx pl.get 1 amp.vol\ny pl.get 2 amp.vol\nz pl.get 2 amp.bal", pmodel=pm)
        self.assertEqual((r.globals["x"], r.globals["y"], r.globals["z"]), (7, 99, 32767))
        self.assertEqual(self.gl("x pl.get 2 amp.vol")["x"], sim.PARAM_DEFAULT[15])    # the neutral part

    def test_m3a_tl_add_zeroes_the_word_of_a_step_without_a_trig_only(self):
        r = self.go("tr.add 2\nratch.set 2 3\ntr.del 2\ntl.add 2\ntr.add 4\nratch.set 4 3\ntl.add 4")
        td = r.model.tracks[1]
        self.assertEqual((td.has("tlock", 2), td.count.get(2)), (True, None))   # no trig left on 2: the word is zeroed
        self.assertEqual((td.has("tlock", 4), td.count.get(4)), (True, 3))      # step 4 keeps its trig and its word

    @covers("PL.CLR")
    def test_pl_clr(self):
        r = self.go("tr.add 3\ntr.add 9\npl.set 3 amp.vol 100\npl.set 9 amp.vol 100\nsteps 1 5: pl.clr")
        self.assertEqual(self.locks(r), {9: {15: 100}})

    @covers("SMP.SET", "SMP.GET")
    def test_smp_set_get(self):
        r = self.go("tr.add 4\nsmp.set 4 9\nx smp.get 4\ny smp.get 5")
        self.assertEqual(r.model.tracks[1].smp, {4: 9})
        self.assertEqual((r.globals["x"], r.globals["y"]), (9, 0))

    @covers("SMP.DEL")
    def test_smp_del(self):
        r = self.go("tr.add 4\ntr.add 5\nsmp.set 4 9\nsmp.set 5 8\nsmp.del 4")
        self.assertEqual(r.model.tracks[1].smp, {5: 8})

    @covers("SMP.RND")
    def test_smp_rnd(self):
        r = self.go("tr.add 1\ntr.add 2\ntr.add 3\nsteps 1 3: smp.rnd 10 14")
        self.assertEqual(r.model.tracks[1].smp,
                         {i + 1: 10 + below(VEC[1]["RAND"][i], 5) for i in range(3)})
        r = self.go("tr.add 1\ntr.add 2\ntr.add 3\nsteps 1 3: smp.rnd 14 10")                      # either order
        self.assertEqual(r.model.tracks[1].smp,
                         {i + 1: 10 + below(VEC[1]["RAND"][i], 5) for i in range(3)})

    @covers("SMP.CLR")
    def test_smp_clr(self):
        r = self.go("tr.add 3\ntr.add 9\nsmp.set 3 9\nsmp.set 9 9\nsteps 1 5: smp.clr")
        self.assertEqual(r.model.tracks[1].smp, {9: 9})

    @covers("HAS.TRIG", "HAS.LOCK", "HAS.SMP", "IS.EMPTY")
    def test_predicates_as_values(self):
        r = self.go("tr.add 3\ntl.add 2\ntl.add 5\npl.set 2 amp.vol 9\nsmp.set 5 7\n"
                    "x has.trig 3\ny has.trig 4\nz has.lock 2\nt has.smp 5\n"
                    "a is.empty 7\nx add x mul 10 a\na is.empty 2\nx add x mul 100 a\n"
                    "y add y mul 10 has.lock 3\nz add z mul 10 has.smp 6")
        self.assertEqual([r.globals[k] for k in "xyzt"], [11, 0, 1, 1])

    def test_predicates_narrow_steps(self):
        setup = "tr.add 1\ntl.add 2\ntl.add 3\npl.set 2 amp.vol 1\nsmp.set 3 1\n"
        for pred, want in (("has.trig", [1]), ("has.lock", [2]), ("has.smp", [3]),
                           ("is.empty", list(range(4, 17)))):
            with self.subTest(pred):
                r = self.go(setup + "steps %s:\n  each: trk 2: tr.add i" % pred)   # marks track 2
                self.assertEqual(self.trigs(r, 2), want)

    def test_predicate_step_set_is_exactly_the_matching_steps(self):
        r = self.go("tr.add 4\ntr.add 6\npl.set 4 amp.vol 1\npl.set 6 amp.vol 1\nsteps has.lock:\n  smp.rnd 3 3")
        self.assertEqual(sorted(r.model.tracks[1].smp), [4, 6])


# ================================================================================================
class TestTrigWordOps(Base):
    def cond_of(self, r, s, t=1):
        return r.model.tracks[t].to_json()["steps"].get(str(s), {}).get("cond")

    def test_condition_codes_follow_the_documented_table(self):
        names = ["off", "fill", "!fill", "pre", "!pre", "nei", "!nei", "1st", "!1st"]
        names += ["%d%%" % p for p in (1, 2, 4, 6, 9, 13, 19, 25, 33, 41, 50, 59, 67, 75, 81, 87,
                                       91, 94, 96, 98, 99)]
        names += ["%d:%d" % (a, b) for b in range(2, 9) for a in range(1, b + 1)]
        self.assertEqual(len(names), 65)
        self.assertEqual([sim.cond_name(c) for c in range(65)], names)
        self.assertEqual(sim.cond_code("50%"), 19)               # docs/TRIGS.md: 50 % = 19
        self.assertEqual(sim.cond_code("1:2"), 30)
        self.assertEqual(sim.cond_code("8:8"), 64)

    def test_a_const_condition_is_a_condition_code(self):
        prog = otype.check_sources([("T.OTY", 'const half 50%\ndef main\n  tr.add 2\n  cond.set 2 half\n')])
        r = sim.simulate(prog, "main", seed=1)
        self.assertEqual(r.model.tracks[1].cond, {2: 19})

    @covers("COND.SET")
    def test_cond_set(self):
        r = self.go("l 3 7: tr.add i\ncond.set 3 50%\ncond.set 4 fill\ncond.set 5 !1st\ncond.set 6 3:4\ncond.set 7 12")
        self.assertEqual([self.cond_of(r, s) for s in (3, 4, 5, 6, 7)],
                         ["50%", "fill", "!1st", "3:4", "6%"])
        r = self.go("tr.add 3\ncond.set 3 50%\ncond.set 3 off")
        self.assertIsNone(self.cond_of(r, 3))                    # 0 = no condition, nothing stored

    @covers("COND.RND")
    def test_cond_rnd(self):
        r = self.go("l 1 3: tr.add i\nsteps 1 3: cond.rnd 50% 99%")                # codes 19..29
        got = [r.model.tracks[1].cond[i + 1] for i in range(3)]
        self.assertEqual(got, [19 + below(VEC[1]["RAND"][i], 11) for i in range(3)])
        r = self.go("l 1 3: tr.add i\nsteps 1 3: cond.rnd 99% 50%")
        self.assertEqual([r.model.tracks[1].cond[i + 1] for i in range(3)], got)

    @covers("MICRO.SET")
    def test_micro_set(self):
        r = self.go("l 3 7: tr.add i\nmicro.set 3 5\nmicro.set 4 -7\nmicro.set 5 99\nmicro.set 6 -99\n"
                    "micro.set 7 4\nmicro.set 7 0")
        self.assertEqual(r.model.tracks[1].micro, {3: 5, 4: -7, 5: 23, 6: -23})

    @covers("MICRO.RND")
    def test_micro_rnd(self):
        r = self.go("l 1 4: tr.add i\nsteps 1 4: micro.rnd 6")
        want = {i + 1: below(VEC[1]["RAND"][i], 13) - 6 for i in range(4)}
        self.assertEqual(r.model.tracks[1].micro, {s: v for s, v in want.items() if v})
        r = self.go("l 1 4: tr.add i\nsteps 1 4: micro.rnd 0")
        self.assertEqual(r.model.tracks[1].micro, {})
        self.assertEqual(r.draws["RAND"], 4)

    @covers("RATCH.SET")
    def test_ratch(self):
        r = self.go("l 3 6: tr.add i\nratch.set 3 4\nratch.set 4 99\nratch.set 5 0\nratch.set 6 3\nratch.set 6 1")
        self.assertEqual(r.model.tracks[1].count, {3: 4, 4: 8})


# ================================================================================================
class TestPartOps(Base):
    """The part ops follow the proven firmware cores and read the part through the model
    (sim.ParamModel, the twin of the unit's imports): descriptor ranges, disabled slots, chooser lengths, defaults."""

    def pgo(self, body, pm=None, seed=1):
        return self.go(body, seed=seed, pmodel=pm)

    @staticmethod
    def raw(n, seed=1):
        s = sim.Stream("RAND", seed)
        return [s.next() for _ in range(n)]

    @covers("PAR.SET")
    def test_par_set(self):
        r = self.go("par.set amp.vol 100\npar.set amp.atk 400\ntrk 2 3:\n  par.set pb 1 5")
        self.assertEqual(r.model.part[1].params, {15: 100, 12: 127})
        self.assertEqual(r.model.part[2].params, {0: 5})
        self.assertEqual(r.model.part[3].params, {0: 5})

    def test_par_set_clamps_into_the_descriptor_and_refuses_a_disabled_slot(self):
        pm = sim.ParamModel({(1, 15): (10, 50, True), (2, 15): (0, 128, False)})
        r = self.pgo("trk 1 2 3:\n  par.set amp.vol 3\npar.set amp.vol 99\ntrk 2:\n  par.set amp.vol 9", pm)
        self.assertEqual([r.model.part[t].params for t in (1, 2, 3)], [{15: 59}, {}, {15: 3}])
        self.assertEqual([o[1:] for o in r.oor], [("par.set", 4), ("par.set", 7)])     # track 2, twice
        r = self.pgo("par.set lfo.spd 1 64\npar.set lfo.dep 3 300")    # the LFO page through its constants
        self.assertEqual(r.model.part[1].params, {6: 64, 11: 127})

    def test_pl_get_reads_what_par_set_wrote(self):
        r = self.go("tr.add 3\npar.set amp.vol 77\nx pl.get 3 amp.vol\nfx.type 1 2\npar.set fx1 1 40\nfx.type 1 3\n"
                    "y pl.get 3 fx1 1")
        self.assertEqual((r.globals["x"], r.globals["y"]), (77, 0))   # the apply forgot fx1 1: the model's value again

    @covers("LFO.DEST")
    def test_lfo_dest(self):
        r = self.go("lfo.dest 2 amp.vol\ntrk 3:\n  lfo.dest 1 fx1 2")
        self.assertEqual(r.model.part[1].lfo_dest, {2: 15})
        self.assertEqual(r.model.part[3].lfo_dest, {1: 19})
        r = self.go("lfo.dest 4 amp.vol")
        self.assertEqual((r.model.part[1].lfo_dest, len(r.oor)), ({}, 1))

    @covers("FX.TYPE")
    def test_fx_set(self):
        r = self.go("fx.type 1 4\nfx.type 2 0\nfx.type 3 4\nfx.type 1 99\nfx.type 1 11\nfx.type 2 14")
        self.assertEqual(r.model.part[1].fx, {1: 4, 2: 14})
        self.assertEqual([o[2] for o in r.oor], [5, 6, 7])         # slot 3; 99 and 11 beyond the 11 entries of FX1
        r = self.pgo("fx.type 1 20\ntrk 1 2\nfx.type 2 3", sim.ParamModel(fx_count={1: 21, 2: 3}))
        self.assertEqual([r.model.part[t].fx for t in (1, 2)], [{1: 20}, {}])
        self.assertEqual([o[2] for o in r.oor], [5, 5])           # refused on each track's pass of the body

    @covers("SCN.CLR")
    def test_scn_clr(self):
        r = self.go("@scenes\ntrk 2:\n  @scenes\ntrk 1:\n  scn.clr")
        self.assertEqual(r.model.part[1].scenes, {k: {b: 0xFF for b in range(32)} for k in range(1, 17)})
        self.assertEqual(len(r.model.part[2].scenes[2]), 18)               # the random scenes loop
        self.assertEqual(sim.result_json(r)["model"]["part"]["1"]["scenes"], {str(k): "off" for k in range(1, 17)})

    @covers("TRK.INIT")
    def test_trk_init(self):
        # the track's sound is reset, its trigs / locks are not
        r = self.go("tr.add 1\npl.set 1 amp.vol 9\nsmp.set 1 3\ncond.set 1 50%\npar.set amp.vol 9\n"
                    "lfo.dest 1 amp.vol\nfx.type 1 4\n@scenes\ntrk.slot 5\ntrk 2:\n  tr.add 2\n"
                    "  par.set amp.vol 7\ntrk 1:\n  trk.init")
        pt = r.model.part[1]
        self.assertEqual(pt.fx, {1: 1, 2: 1})                     # the neutral factory effects
        self.assertEqual(pt.slot, "factory")
        self.assertEqual(pt.params, {p: sim.PARAM_DEFAULT.get(p, 0) for p in range(30)})
        self.assertEqual((pt.lfo_dest, pt.lfo_wave), ({1: 0, 2: 0, 3: 0}, {1: 0, 2: 0, 3: 0}))
        self.assertEqual(pt.scenes, {k: {b: 0xFF for b in range(32)} for k in range(1, 17)})
        self.assertEqual(self.trigs(r, 1), [1])                      # the trig is still there
        self.assertEqual(self.locks(r, 1), {1: {15: 9}})             # and its lock
        self.assertEqual((r.model.tracks[1].smp, r.model.tracks[1].cond), ({1: 3}, {1: 19}))
        self.assertEqual(r.model.part[2].params, {15: 7})            # another track: untouched
        self.assertEqual(self.trigs(r, 2), [2])
        self.assertEqual(r.draws["RAND"], 15 * 18)                   # (the scenes loop's draws; trk.init draws nothing)

    def test_trk_init_reads_defaults_ranges_and_factory_effects(self):
        pm = sim.ParamModel({(1, 0): (0, 128, False), (1, 15): (10, 20, True)},
                            defaults={(1, 15): 90, (1, 3): -4}, fx_count={2: 2}, fx_default={1: 7, 2: 2})
        r = self.pgo("par.set pb 1 3\ntrk.init", pm)
        pt = r.model.part[1]
        self.assertEqual(pt.fx, {1: 7})                              # FX2's factory index 2 is not in its 2 entries
        self.assertNotIn(0, pt.params)                               # disabled: par.set refused, trk.init skips it
        self.assertEqual((pt.params[15], pt.params[3]), (29, 0))     # the default clamped into the range
        self.assertEqual([o[1] for o in r.oor], ["par.set"])

    def test_trk_init_keeps_the_machine_type(self):
        prog = self.prog("trk.init")
        r = sim.simulate(prog, "main", seed=1)
        self.assertEqual(r.model.part[1].mtype, 0)
        m = sim.Model()
        m.part[1].mtype = 1
        run = sim.Run(prog, prog.fmap["MAIN"], 1, "--seed", m, 1, 16, 1, None, None, {})
        run.run()
        self.assertEqual(m.part[1].mtype, 1)

    @covers("TRK.SLOT", "TRK.TYPE")
    def test_trk_slot_and_type(self):
        r = self.go("trk.slot 5\ntrk 2:\n  trk.slot 6\ntrk 1:\n  x trk.type")
        self.assertEqual((r.model.part[1].slot, r.model.part[2].slot), (5, 6))
        self.assertEqual(r.globals["x"], 0)                       # STATIC (docs/TRACK.md)
        self.assertNotIn("trk.slot", r.not_on_unit)

    @covers("TEMPO")
    def test_tempo(self):
        for v, want in ((120, 120), (5, 30), (999, 300)):
            r = self.go("tempo %d" % v)
            self.assertEqual(r.model.tempo, want)
            self.assertNotIn("tempo", r.not_on_unit)
        self.assertNotIn("tempo", self.go("tr.add 1").not_on_unit)

    @covers("MSG")
    def test_msg(self):
        r = self.go('msg "Hello"\nmsg "AB c"')
        self.assertEqual(r.model.msgs, ["Hello", "AB c"])
        self.assertIn('msg:   "Hello"', sim.render_text(r))


# ================================================================================================
CARD_FILES = ["AUDIO/DRUMS/kick.wav", "AUDIO/DRUMS/Snare.WAV", "AUDIO/DRUMS/B.wav",
              "AUDIO/DRUMS/a.wav", "AUDIO/DRUMS/hat.aif", "AUDIO/DRUMS/readme.txt",
              "AUDIO/DRUMS/._x.wav", "AUDIO/DRUMS/.hidden.wav", "AUDIO/DRUMS/Kick2.wav",
              "AUDIO/DRUMS/SUB/clap.wav", "AUDIO/DRUMS/SUB/deeper/ride.wav",
              "AUDIO/DRUMS/SUB/._junk.wav", "AUDIO/DRUMS/.Trash/t.wav", "AUDIO/other.wav"]


class TestCard(Base):
    def setUp(self):
        self.card_dir = make_card(self.tmp(), CARD_FILES)

    def names(self, body):
        """The files of the list after `body`, as loaded into slots 1.. by the fill loop."""
        r = self.go(body + "\n@fill", card=self.card_dir)
        return [p for s, p in sorted(r.model.slots.items())]

    def test_listing_is_sorted_case_insensitively_and_skips_dotfiles(self):
        self.assertEqual(sim.list_card(self.card_dir, "AUDIO/DRUMS"),
                         ["a.wav", "B.wav", "hat.aif", "kick.wav", "Kick2.wav", "readme.txt",
                          "Snare.WAV"])

    def test_sort_key_breaks_a_case_tie_on_the_exact_bytes(self):
        names = ["b.wav", "B.wav", "a.wav", "C.wav", "c.wav"]
        want = ["a.wav", "B.wav", "b.wav", "C.wav", "c.wav"]
        self.assertEqual(sorted(names, key=sim._sort_key), want)
        self.assertEqual(sorted(reversed(names), key=sim._sort_key), want)

    @covers("DIR")
    def test_dir_lists_the_folder_case_insensitive_and_deep(self):
        self.assertEqual(self.names('dir "AUDIO/DRUMS"')[:2], ["AUDIO/DRUMS/a.wav", "AUDIO/DRUMS/B.wav"])
        self.assertEqual([n.lower() for n in self.names('dir "audio/drums"')],      # as written
                         [n.lower() for n in self.names('dir "AUDIO/DRUMS"')])
        self.assertEqual(self.names('dir "AUDIO/DRUMS/"'), self.names('dir "AUDIO/DRUMS"'))
        self.assertEqual(self.names('dir "AUDIO/DRUMS" deep'),
                         ["AUDIO/DRUMS/" + n for n in
                          ["a.wav", "B.wav", "hat.aif", "kick.wav", "Kick2.wav", "readme.txt",
                           "Snare.WAV", "SUB/clap.wav", "SUB/deeper/ride.wav"]])

    def test_deep_sorts_the_relative_paths_together(self):
        deep = sim.list_card(self.card_dir, "AUDIO/DRUMS", deep=True)
        self.assertEqual(deep, sorted(deep, key=lambda n: (n.upper(), n)))
        self.assertIn("SUB/deeper/ride.wav", deep)
        self.assertNotIn("SUB/._junk.wav", deep)
        self.assertFalse([n for n in deep if ".Trash" in n or n.startswith(".")])

    def test_missing_folder_and_unsafe_paths_give_an_empty_list(self):
        for p in ("NOPE", "AUDIO/NOPE", "../x", "AUDIO/../AUDIO/DRUMS"):
            self.assertEqual(sim.list_card(self.card_dir, p), [], p)
        self.assertEqual(sim.list_card(None, "AUDIO"), [])
        self.assertEqual(sim.list_card(self.card_dir, "AUDIO/DRUMS/kick.wav"), [])   # a file

    @covers("EXT")
    def test_ext(self):
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\next wav'),
                         ["AUDIO/DRUMS/a.wav", "AUDIO/DRUMS/B.wav", "AUDIO/DRUMS/kick.wav",
                          "AUDIO/DRUMS/Kick2.wav", "AUDIO/DRUMS/Snare.WAV"])
        self.assertEqual(len(self.names('dir "AUDIO/DRUMS"\next wav aif')), 6)
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\next aif'), ["AUDIO/DRUMS/hat.aif"])
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\next aiff'), [])
        self.assertEqual(len(self.names('dir "AUDIO/DRUMS"')), 7)     # no ext: every file

    @covers("MATCH")
    def test_match(self):
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\nmatch "K*"'),
                         ["AUDIO/DRUMS/kick.wav", "AUDIO/DRUMS/Kick2.wav"])
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\nmatch "k*" "*.aif"'),
                         ["AUDIO/DRUMS/hat.aif", "AUDIO/DRUMS/kick.wav", "AUDIO/DRUMS/Kick2.wav"])
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\nmatch "*2*"'), ["AUDIO/DRUMS/Kick2.wav"])
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\nmatch "kick.wav"'), ["AUDIO/DRUMS/kick.wav"])
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\nmatch "kick"'), [])
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\nmatch "k"'), [])           # whole name
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\nmatch "a.*"'), ["AUDIO/DRUMS/a.wav"])
        # in a deep listing the pattern tests the base name
        self.assertEqual(self.names('dir "AUDIO/DRUMS" deep\nmatch "r*"'),
                         ["AUDIO/DRUMS/readme.txt", "AUDIO/DRUMS/SUB/deeper/ride.wav"])

    def test_ext_and_match_combine(self):
        self.assertEqual(self.names('dir "AUDIO/DRUMS"\next wav\nmatch "k*"'),
                         ["AUDIO/DRUMS/kick.wav", "AUDIO/DRUMS/Kick2.wav"])

    @covers("FILE.LEN")
    def test_file_n(self):
        self.assertEqual(self.gl('dir "AUDIO/DRUMS"\next wav\nx file.len', card=self.card_dir)["x"], 5)
        self.assertEqual(self.gl('x file.len')["x"], 0)                 # no dir yet
        self.assertEqual(self.gl('dir "AUDIO/DRUMS"\nx file.len')["x"], 0)    # no card given

    @covers("FILE.RND")
    def test_file_rnd(self):
        r = self.go('dir "AUDIO/DRUMS"\next wav\nx file.rnd\ny file.rnd', card=self.card_dir)
        v = VEC[1]["FILE"]
        self.assertEqual((r.globals["x"], r.globals["y"]), (below(v[0], 5) + 1, below(v[1], 5) + 1))
        self.assertEqual(r.draws["FILE"], 2)
        r = self.go('x file.rnd')                                      # empty list: 0, one draw
        self.assertEqual((r.globals["x"], r.draws["FILE"]), (0, 1))

    def test_file_pick_on_an_empty_list_draws_once_and_gives_zero(self):
        r = self.go("x file.pick")
        self.assertEqual((r.globals["x"], r.draws["FILE"]), (0, 1))

    @covers("FILE.AT")
    def test_file_at(self):
        g = self.gl('dir "AUDIO/DRUMS"\next wav\nx file.at 2\ny file.at 0\nz file.at 6\nt file.at 5',
                    card=self.card_dir)
        self.assertEqual([g[k] for k in "xyzt"], [2, 0, 0, 5])

    @covers("STATIC.LOAD", "STATIC.FREE", "STATIC.FIRSTFREE", "STATIC.CLR")
    def test_slot_ops(self):
        r = self.go('dir "AUDIO/DRUMS"\next wav\nstatic.load 5 2\nx static.free 5\ny static.free 6\n'
                    'z static.firstfree\nstatic.load z 1\nt static.firstfree', card=self.card_dir)
        self.assertEqual(r.model.slots, {5: "AUDIO/DRUMS/B.wav", 1: "AUDIO/DRUMS/a.wav"})
        self.assertEqual([r.globals[k] for k in "xyzt"], [0, 1, 1, 2])
        r = self.go('dir "AUDIO/DRUMS"\nstatic.load 5 2\nstatic.clr 5\nx static.free 5', card=self.card_dir)
        self.assertEqual((r.model.slots, r.globals["x"]), ({}, 1))
        r = self.go('dir "AUDIO/DRUMS"\nstatic.load 5 99', card=self.card_dir)        # file out of range
        self.assertEqual((r.model.slots, len(r.oor)), ({}, 1))
        r = self.go('static.load 5 1')                                                 # no list
        self.assertEqual((r.model.slots, len(r.oor)), ({}, 1))

    def test_static_firstfree_when_full_is_zero(self):
        r = self.go('dir "A"\n@fill\nx static.firstfree', card=self.card(["A/%03d.wav" % i for i in range(128)]))
        self.assertEqual(r.globals["x"], 0)

    @covers("SLOTS.CLR", "SLOTS.ADD", "SLOTS.RND")
    def test_working_set(self):
        r = self.go("slots.add 4\nslots.add 9\nslots.add 4\nx slots.rnd\ny slots.rnd")
        self.assertEqual(r.model.working, [4, 9])
        v = VEC[1]["FILE"]
        self.assertEqual((r.globals["x"], r.globals["y"]),
                         ((4, 9)[below(v[0], 2)], (4, 9)[below(v[1], 2)]))
        self.assertEqual(r.draws["FILE"], 2)                       # slots.rnd draws from `file`
        r = self.go("slots.add 4\nslots.clr\nx slots.rnd")
        self.assertEqual((r.model.working, r.globals["x"], r.draws["FILE"]), ([], 0, 1))

    def test_fill_loop_uses_free_slots_in_order(self):
        r = self.go('dir "AUDIO/DRUMS"\next wav\nstatic.load 2 1\n@fill', card=self.card_dir)
        self.assertEqual(r.model.slots[2], "AUDIO/DRUMS/a.wav")       # was loaded before the fill
        self.assertEqual(r.model.slots[1], "AUDIO/DRUMS/a.wav")       # first free slot, first file
        self.assertEqual(r.model.slots[3], "AUDIO/DRUMS/B.wav")
        self.assertEqual(len(r.model.slots), 6)                      # the 5 wavs + slot 2 kept

    def test_clear_loop(self):
        r = self.go('dir "AUDIO/DRUMS"\n@fill\n@clear', card=self.card_dir)
        self.assertEqual(r.model.slots, {})

    def test_no_card_means_no_files_and_skips_are_counted(self):
        r = self.go('dir "AUDIO/DRUMS"\nstatic.load 1 1\nx file.len')
        self.assertEqual((r.globals["x"], r.model.slots, len(r.oor)), (0, {}, 1))

    @covers("SRC.SAVE", "FLEX.LOAD", "FLEX.FREE", "CAT.CLR", "CAT.ADD", "CAT.SAVE", "NORM")
    def test_audio_ops(self):
        r = self.go('input sample\nsrc.save\nflex.load 2 file.at 1\nx flex.free 2\ny flex.free 3',
                    card=self.card_dir)
        self.assertEqual(r.model.flex_slots.get(2), "AUDIO/SOURCE.WAV")
        self.assertEqual(r.globals["x"], 0)
        self.assertEqual(r.globals["y"], 1)
        r = self.go('dir "AUDIO/DRUMS"\ncat.clr\ncat.add file.at 1\ncat.add file.at 2\nnorm file.at 1 -3\ncat.save "CHAIN"',
                    card=self.card_dir)
        self.assertEqual(r.model.cat_list, ["AUDIO/DRUMS/a.wav", "AUDIO/DRUMS/B.wav"])
        self.assertEqual(r.model.normalized.get("AUDIO/DRUMS/a.wav"), -3)

    @covers("FLEX.FIRSTFREE", "FLEX.DEL")
    def test_flex_firstfree_and_del(self):
        m = sim.Model()
        m.flex_slots = {1: "A/a.wav", 2: "A/b.wav", 4: "A/c.wav"}
        r = self.go("x flex.firstfree\nflex.del 1\ny flex.firstfree\nz flex.free 1\nflex.del 5\nflex.del 4", model=m)
        self.assertEqual((r.globals["x"], r.globals["y"], r.globals["z"]), (3, 1, 1))
        self.assertEqual(r.model.flex_slots, {2: "A/b.wav"})
        m = sim.Model()
        m.flex_slots = {k: "A/x.wav" for k in range(1, 129)}
        self.assertEqual(self.go("x flex.firstfree", model=m).globals["x"], 0)

    @covers("STATIC.SWAP")
    def test_static_swap(self):
        held = {3: "A/a.wav", 9: "B/b.wav"}
        r = self.go("static.swap 3 9\nx static.free 3\ny static.free 9", slots=held)
        self.assertEqual((r.model.slots, r.globals["x"], r.globals["y"]), ({3: "B/b.wav", 9: "A/a.wav"}, 0, 0))
        r = self.go("static.swap 3 5\nx static.free 3\ny static.free 5", slots=held)         # an empty slot swaps too
        self.assertEqual((r.model.slots, r.globals["x"], r.globals["y"]), ({5: "A/a.wav", 9: "B/b.wav"}, 1, 0))
        for body in ("static.swap 3 3", "static.swap 6 7"):                                   # nothing moves
            r = self.go(body, slots=held, busy={"static.swap"})                               # and the card is not asked
            self.assertEqual(r.model.slots, held)
        r = self.go("a 0\nb 129\nstatic.swap a 3\nstatic.swap 3 b\nstatic.swap 1 128", slots={1: "A/a.wav", 128: "B/b.wav"})
        self.assertEqual((r.model.slots, len(r.oor)), ({1: "B/b.wav", 128: "A/a.wav"}, 2))
        with self.assertRaises(sim.CardBusy):
            self.go("static.swap 3 9", slots=held, busy={"static.swap"})

    def test_static_swap_refused_reload_frees_the_slot(self):
        held = {3: "A/a.wav", 9: "B/b.wav"}
        r = self.go("static.swap 3 9", slots=held, refuse={"A/a.wav"})                       # a goes to 9, refused
        self.assertEqual((r.model.slots, [(f[1], f[3], f[4]) for f in r.refused]), ({3: "B/b.wav"}, [("static.swap", 9, "A/a.wav")]))
        r = self.go("static.swap 3 5", slots=held, refuse={"A/a.wav"})
        self.assertEqual(r.model.slots, {9: "B/b.wav"})                                       # moved, refused, gone

    def test_shuffle_loop_permutes_the_leading_run_with_n_minus_one_draws_ahead(self):
        held = {k: "A/%d.wav" % k for k in (1, 2, 3, 4, 5)}
        held[7] = "Z/7.wav"                                                                   # 6 is free: 7 is left alone
        a, b = self.go("@shuffle", slots=held), self.go("@shuffle", slots=held)
        self.assertEqual(a.model.slots, b.model.slots)                                        # reproducible per seed
        self.assertEqual(sorted(a.model.slots), [1, 2, 3, 4, 5, 7])
        self.assertEqual(sorted(a.model.slots[k] for k in range(1, 6)), sorted(held[k] for k in range(1, 6)))
        self.assertEqual(a.model.slots[7], "Z/7.wav")
        self.assertEqual(a.draws["RAND"], 5)
        # Fisher-Yates forward: slot i swaps with a draw in 1..i (the first draw, rrand 1 1, takes one value too)
        vals = {k: held[k] for k in range(1, 6)}
        for k, i in enumerate(range(1, 6)):
            j = 1 + below(VEC[1]["RAND"][k], i)
            vals[i], vals[j] = vals[j], vals[i]
        self.assertEqual({k: a.model.slots[k] for k in range(1, 6)}, vals)
        self.assertGreater(len({tuple(self.go("@shuffle", slots=held, seed=k).model.slots[s] for s in range(1, 6)) for k in range(8)}), 3)

    def test_the_dir_list_belongs_to_the_function_frame(self):
        card = self.card(["A/a.wav", "A/b.wav", "B/c.wav"])
        r = self.go('dir "A"\nhelper\nx file.len', card=card, extra='def helper\n  dir "B"\n  y file.len\n')
        self.assertEqual((r.globals["x"], r.globals["y"]), (2, 1))
        r = self.go('dir "A"\nhelper', card=card, extra="def helper\n  x file.len\n")   # a copy goes in
        self.assertEqual(r.globals["x"], 2)

    def test_the_card_is_never_written(self):
        before = sorted(os.listdir(self.card_dir))
        self.go('dir "AUDIO/DRUMS"\n@fill\nstatic.load 9 1', card=self.card_dir)
        self.go('dir "AUDIO/DRUMS"\nstatic.load 1 1\nstatic.load 2 2\n@shuffle', card=self.card_dir)
        self.assertEqual(sorted(os.listdir(self.card_dir)), before)


# ================================================================================================
def synthetic_bank(path, pattern=3, length=24, mode=0, track_lengths=None):
    """A bank file with ONE decoded pattern, laid out at the documented offsets (docs/TRIGS.md)."""
    size = sim.BANK_HEAD + 16 * sim.PATTERN_STRIDE + 100
    d = bytearray(size)
    d[0:4] = b"FORM"
    d[8:16] = b"DPS1BANK"
    for q in range(16):                                           # every pattern: no lock at all
        for t in range(8):
            tq = sim.BANK_HEAD + q * sim.PATTERN_STRIDE + 8 + t * 0x922 + 9
            for s in range(64):
                o = tq + 0x59 + s * 0x20
                d[o:o + 32] = b"\xff" * 32
    p = pattern - 1
    base = sim.BANK_HEAD + p * sim.PATTERN_STRIDE
    for t in range(8):
        tr = base + 8 + t * 0x922 + 9                           # PTRN header 8 + TRAC header 9
        d[tr + 0x50] = (track_lengths or {}).get(t + 1, 16)
    tr1 = base + 8 + 0 * 0x922 + 9
    d[tr1 + 7] = 0b00010001                                       # trig mask: steps 1 and 5
    d[tr1 + 8 + 7] = 0b00000100                                   # trigless trig: step 3
    d[tr1 + 0x59 + 2 * 0x20 + 15] = 100                           # step 3: parameter 15 = 100
    d[tr1 + 0x59 + 2 * 0x20 + 31] = 4                             # step 3: sample lock slot 5
    w7 = (2 << 13) | (5 << 7) | 19                                # step 7: 3 trigs, micro +5, 50 %
    d[tr1 + 0x899 + 6 * 2:tr1 + 0x899 + 6 * 2 + 2] = w7.to_bytes(2, "big")
    w8 = (61 << 7) | 1                                            # step 8: micro -3, fill
    d[tr1 + 0x899 + 7 * 2:tr1 + 0x899 + 7 * 2 + 2] = w8.to_bytes(2, "big")
    tr2 = base + 8 + 1 * 0x922 + 9
    d[tr2 + 7] = 0b00000010                                       # track 2: step 2
    end = base + sim.PATTERN_STRIDE
    d[end - 9] = length
    d[end - 7] = mode
    with open(path, "wb") as fh:
        fh.write(d)
    return path


# ================================================================================================
LEGACY = os.path.join(ROOT, "modules", "otype", "examples", "LEGACY.OTY")


class TestLegacy(Base):
    """modules/otype/examples/LEGACY.OTY: the functions of an earlier menu as scripts, run on the
    synthetic bank (track 1: red trigs 1 and 5, a trigless trig on 3 with AMP VOL 100 and sample lock 5; track 2: step 2)
    and on a small card, compared with what the assembler did (the earlier assembler
    headers).  The differences that cannot be avoided are asserted too, so a change of either side is noticed."""

    @classmethod
    def setUpClass(cls):
        cls.p = otype.check_files([LEGACY])
        bad = [str(d) for d in cls.p.diags]
        assert not bad, bad

    def run_(self, func, seed=1, bank=True, params=None, **kw):
        b = synthetic_bank(os.path.join(self.tmp(), "bank01.work"), pattern=1, length=16) if bank else None
        return sim.simulate(self.p, func, seed=seed, bank=b, pattern=1, params=params, **kw)

    def test_menu_rows_confirm_and_profile(self):
        rows = {f.name.lower(): (tuple(f.menus[0][0]) + (f.menus[0][1],)) for f in self.p.funcs if f.menus}
        self.assertEqual(rows, {
            "pool_fill": ("POOL", "FILL"), "pool_refill": ("POOL", "REFILL"), "pool_shuffle": ("POOL", "SHUFFLE"),
            "pool_clear": ("POOL", "CLEAR"),
            "rnd_lfo": ("LFO", "RANDOM"), "rnd_fx": ("FX", "RANDOM"), "scenes_gen": ("SCENES", "GENERATE"),
            "scenes_clr": ("SCENES", "CLEAR"), "rnd_plocks": ("TRIGS", "RANDOM P"), "rnd_smplocks": ("TRIGS", "RANDOM SMP"),
            "clr_plocks": ("TRIGS", "CLEAR P"), "clr_smplocks": ("TRIGS", "CLEAR SMP"), "trigless": ("TRIGS", "TRIGLESS"),
            "init_track": ("TRACK", "INIT")})
        # the destructive ones ask, with a text (the earlier menu asked YES/NO on exactly these)
        texts = {f.name.lower(): f.confirm_text[0] for f in self.p.funcs if f.confirm_text}
        self.assertEqual(texts, {"pool_refill": "REFILL?", "pool_clear": "CLR POOL?", "scenes_clr": "CLR SCNS?", "clr_plocks": "CLEAR P?",
                                 "clr_smplocks": "CLR SMP?", "init_track": "INIT TRK?"})
        self.assertTrue(all(self.p.destructive.get(self.p.fmap[n.upper()]) for n in texts))
        self.assertTrue(all(len(t) <= 9 for t in texts.values()))            # what the confirmation box shows
        c = otb.compile_program(self.p, O.PROFILES["m1"])
        self.assertEqual([str(d) for d in c.diags], [])                      # every function is on the unit profile

    def test_params_are_the_old_page_options(self):
        prm = {f.name.lower(): dict(f.params) for f in self.p.funcs if f.params}
        self.assertNotIn("pool_fill", prm)                                    # FILL OVERWR is POOL > REFILL now
        self.assertEqual(prm["scenes_gen"], {"SC_LFO_DEST": (1, 0, 1)})
        pl = {"PL_PITCH": (0, 0, 1), "PL_START": (0, 0, 1), "PL_LENGTH": (0, 0, 1), "PL_RATE": (0, 0, 1),
              "PL_RETRIG": (0, 0, 1), "PL_LFO": (0, 0, 1), "PL_AMP": (0, 0, 1), "PL_FX": (3, 0, 3)}
        self.assertEqual(prm["rnd_plocks"], pl)                              # defaults: PL FX1 and PL FX2, as before
        self.assertEqual(prm["trigless"], pl)

    # ---- POOL --------------------------------------------------------------------------------------------------
    def card_files(self, n, extra=()):
        return self.card(["AUDIO/K/k%03d.wav" % k for k in range(n)] + ["AUDIO/S/s.aif", "AUDIO/S/notes.txt",
                                                                         "AUDIO/a/b/c/d/e/deep.wav"] + list(extra))

    def test_fill_takes_random_distinct_audio_files_into_every_free_slot_in_one_press(self):
        card = self.card_files(200)
        r = self.run_("pool_fill", bank=False, card=card, slots={2: "AUDIO/OLD.WAV", 7: "AUDIO/OLD2.WAV"})
        sl = r.model.slots
        self.assertEqual((sl[2], sl[7]), ("AUDIO/OLD.WAV", "AUDIO/OLD2.WAV"))   # occupied slots kept
        new = {s: v for s, v in sl.items() if s not in (2, 7)}
        self.assertEqual(sorted(new), [s for s in range(1, 129) if s not in (2, 7)])   # the whole pool, one press
        self.assertEqual(len(set(new.values())), 126)                         # never the same file twice
        self.assertTrue(all(v.lower().endswith((".wav", ".aif")) for v in new.values()))
        self.assertNotEqual([new[s] for s in sorted(new)], sorted(new.values()))   # random order, not the list order
        other = self.run_("pool_fill", seed=2, bank=False, card=card, slots={2: "AUDIO/OLD.WAV", 7: "AUDIO/OLD2.WAV"})
        self.assertNotEqual(other.model.slots, sl)

    def test_fill_with_fewer_files_than_slots_loads_every_file_once(self):
        card = self.card_files(5)
        r = self.run_("pool_fill", bank=False, card=card)
        self.assertEqual(len(r.model.slots), 7)                              # 5 + s.aif + deep.wav, not notes.txt
        self.assertEqual(len(set(r.model.slots.values())), 7)
        self.assertIn("AUDIO/a/b/c/d/e/deep.wav", r.model.slots.values())    # deeper than the old MAXDEPTH 4: listed now
        self.assertFalse(any(v.endswith(".txt") for v in r.model.slots.values()))

    def test_refill_replaces_every_slot_in_one_press(self):
        card = self.card_files(300)
        old = {s: "AUDIO/OLD%d.WAV" % s for s in range(1, 129)}
        r = self.run_("pool_refill", bank=False, card=card, slots=dict(old))
        sl = r.model.slots
        self.assertEqual(sorted(sl), list(range(1, 129)))
        self.assertTrue(all(sl[s] != old[s] for s in sl))
        self.assertEqual(len(set(sl.values())), 128)

    def test_clear(self):
        slots = {1: "AUDIO/A.WAV", 4: "AUDIO/B.WAV", 9: "AUDIO/C.WAV"}
        card = self.card(["AUDIO/A.WAV", "AUDIO/B.WAV", "AUDIO/C.WAV"])
        r = self.run_("pool_clear", bank=False, card=card, slots=slots)
        self.assertEqual(r.model.slots, {})

    def test_shuffle(self):
        slots = {1: "AUDIO/A.WAV", 2: "AUDIO/B.WAV", 3: "AUDIO/C.WAV", 9: "AUDIO/D.WAV"}      # 4 is free: 9 stays put
        card = self.card(["AUDIO/A.WAV", "AUDIO/B.WAV", "AUDIO/C.WAV", "AUDIO/D.WAV"])
        r = self.run_("pool_shuffle", bank=False, card=card, slots=slots)
        self.assertEqual(sorted(r.model.slots), [1, 2, 3, 9])
        self.assertEqual(sorted(r.model.slots[s] for s in (1, 2, 3)), ["AUDIO/A.WAV", "AUDIO/B.WAV", "AUDIO/C.WAV"])
        self.assertEqual(r.model.slots[9], "AUDIO/D.WAV")

    # ---- LFO, FX, SCENES, TRACK ------------------------------------------------------------------------------
    def test_random_lfo_and_fx_on_the_current_track(self):
        r = self.run_("rnd_lfo", track=3)
        pt = r.model.part[3]
        self.assertEqual(sorted(pt.params), list(range(6, 12)))             # page 1: speeds and depths
        self.assertTrue(all(18 <= v <= 29 for v in pt.lfo_dest.values()) and len(pt.lfo_dest) == 3)   # aimed at FX
        self.assertEqual(len(pt.lfo_wave), 3)
        self.assertTrue(all(not r.model.part[t].params for t in range(1, 9) if t != 3))
        r = self.run_("rnd_fx", track=2)
        fx = r.model.part[2].fx
        self.assertEqual(sorted(fx), [1, 2])
        self.assertTrue(fx[1] >= 1 and fx[2] >= 1)                          # NONE never drawn

    def test_generate_scenes_on_the_current_track(self):
        r = self.run_("scenes_gen", track=3)
        pt = r.model.part[3]
        self.assertEqual(len(pt.lfo_dest), 3)                                # SC LFO DEST (default on)
        self.assertTrue(all(18 <= v <= 29 for v in pt.lfo_dest.values()))
        self.assertTrue(all(v == sim.NOT_LOCKED for v in pt.scenes.get(1, {}).values()))   # scene 1 blank
        for sc in range(2, 17):
            self.assertEqual(sorted(pt.scenes[sc]), list(range(6, 12)) + list(range(18, 30)), sc)  # SC LFO + SC FX
        self.assertTrue(all(not r.model.part[t].scenes for t in range(1, 9) if t != 3))
        r = self.run_("scenes_gen", track=3, params={"sc_lfo_dest": 0})
        self.assertFalse(r.model.part[3].lfo_dest)
        r = self.run_("scenes_clr")
        for t in range(1, 9):
            self.assertEqual(sorted(r.model.part[t].scenes), list(range(1, 17)), t)

    def test_init_track(self):
        r = self.run_("init_track", track=4)
        pt = r.model.part[4]
        self.assertEqual(pt.slot, "factory")
        self.assertEqual(sorted(pt.params), list(range(30)))
        self.assertEqual(sorted(pt.scenes), list(range(1, 17)))
        self.assertEqual(self.trigs(r, 1), [1, 5])                           # the trigs stay

    # ---- TRIGS -------------------------------------------------------------------------------------------------
    def test_random_plocks_default_families(self):
        r = self.run_("rnd_plocks")
        lk = r.model.tracks[1].locks
        fx = set(range(18, 30))
        self.assertEqual(set(lk[1]), fx)
        self.assertEqual(set(lk[5]), fx)
        # inevitable difference: the old routine locked RED trigs only; pl.rnd locks every step with a trig, here the
        # trigless trig of step 3 (its AMP VOL lock stays)
        self.assertEqual(set(lk[3]), fx | {15})
        self.assertEqual(lk[3][15], 100)
        self.assertEqual(sorted(lk), [1, 3, 5])
        self.assertEqual(self.trigs(r, 2), [2])
        self.assertNotIn(2, r.model.tracks[2].locks)                         # the current track only

    def test_random_plocks_each_family(self):
        fams = {"pl_pitch": {0}, "pl_start": {1}, "pl_length": {2}, "pl_rate": {3}, "pl_retrig": {4, 5},
                "pl_lfo": set(range(6, 12)), "pl_amp": set(range(12, 18))}
        for name, want in fams.items():
            r = self.run_("rnd_plocks", params={name: 1, "pl_fx": 0})
            self.assertEqual(set(r.model.tracks[1].locks[1]), want, name)
        for v, want in ((1, set(range(18, 24))), (2, set(range(24, 30)))):
            r = self.run_("rnd_plocks", params={"pl_fx": v})
            self.assertEqual(set(r.model.tracks[1].locks[1]), want, v)
        r = self.run_("rnd_plocks", params={"pl_fx": 0})
        self.assertNotIn(1, r.model.tracks[1].locks)
        every = {k: 1 for k in fams}
        r = self.run_("rnd_plocks", params=every)
        self.assertEqual(set(r.model.tracks[1].locks[5]), set(range(30)))

    def test_random_sample_locks_from_the_loaded_slots(self):
        r = self.run_("rnd_smplocks", slots={2: "AUDIO/A.WAV", 9: "AUDIO/B.WAV"})
        sm = r.model.tracks[1].smp
        self.assertEqual(sorted(sm), [1, 3, 5])                              # every trig (step 3: trigless, an écart)
        self.assertTrue(set(sm.values()) <= {2, 9})
        seen = set()
        for seed in range(12):
            seen |= set(self.run_("rnd_smplocks", seed=seed, slots={2: "AUDIO/A.WAV", 9: "AUDIO/B.WAV"}).model.tracks[1].smp.values())
        self.assertEqual(seen, {2, 9})
        r = self.run_("rnd_smplocks")                                       # nothing loaded: nothing locked
        self.assertEqual(r.model.tracks[1].smp, {3: 5})

    def test_clear_plocks_and_sample_locks(self):
        r = self.run_("clr_plocks")
        self.assertEqual(r.model.tracks[1].locks, {})
        self.assertEqual(r.model.tracks[1].smp, {3: 5})                      # the sample lock stays
        self.assertEqual(self.trigs(r), [1, 5])
        r = self.run_("clr_smplocks")
        self.assertEqual(r.model.tracks[1].smp, {})
        self.assertEqual(r.model.tracks[1].locks, {3: {15: 100}})            # the p-lock stays

    def test_trigless_locks_on_free_steps_only(self):
        n_new, steps_seen = 0, set()
        for seed in range(30):
            r = self.run_("trigless", seed=seed)
            td = r.model.tracks[1]
            new = [s for s in range(1, 65) if td.has("tlock", s)]
            self.assertTrue(all(1 <= s <= 16 and s not in (1, 3, 5) for s in new), new)   # inside the length, free steps
            self.assertEqual(self.trigs(r), [1, 5])
            for s in new:
                self.assertEqual(set(td.locks[s]), set(range(18, 30)), (seed, s))   # PL FX1 + FX2 on the new ones
            self.assertEqual(set(td.locks) - set(new), {3})                  # nothing else locked
            self.assertEqual(td.locks[3], {15: 100})
            n_new += len(new)
            steps_seen |= set(new)
        self.assertTrue(0.15 < n_new / (30 * 13.0) < 0.35, n_new)              # ~25 % of the 13 free steps
        self.assertGreater(len(steps_seen), 8)


# ================================================================================================
class TestDirLimits(Base):
    """`dir` keeps at most 4096 names of at most 127 bytes."""
    def test_limit_constants(self):
        self.assertEqual((O.DIR_LIST_MAX, O.DIR_NAME_MAX), (4096, 127))

    def test_more_than_4096_files_keep_the_first_4096_and_warn_once(self):
        card = self.card(["AUDIO/X/f%04d.wav" % i for i in range(4100)])
        r = self.go('dir "AUDIO/X"\nx file.len\nl 1 3: dir "AUDIO/X"\ny file.len', card=card)
        self.assertEqual(r.globals["x"], 4096)
        self.assertEqual(r.globals["y"], 4096)
        self.assertEqual(r.warnings, ['dir "AUDIO/X": 4100 files, only the first 4096 kept'])
        r = self.go('dir "AUDIO/X"\nstatic.load 1 file.at 4096\nstatic.load 2 file.at 4097', card=card)
        self.assertEqual(r.model.slots[1], "AUDIO/X/f4095.wav")
        self.assertNotIn(2, r.model.slots)

    def test_exactly_4096_files_do_not_warn(self):
        card = self.card(["AUDIO/X/f%04d.wav" % i for i in range(4096)])
        r = self.go('dir "AUDIO/X"\nx file.len', card=card)
        self.assertEqual((r.globals["x"], r.warnings), (4096, []))

    def test_long_names_are_skipped_before_the_cut(self):
        long_ = "L" * 124 + ".wav"                    # 128 bytes
        ok = "L" * 123 + ".wav"                       # 127 bytes
        card = self.card(["AUDIO/X/a.wav", "AUDIO/X/" + long_, "AUDIO/X/" + ok,
                          "AUDIO/X/\u00e9" + "e" * 122 + ".wav"])      # 128 bytes in UTF-8
        r = self.go('dir "AUDIO/X"\nx file.len\nl 1 2: dir "AUDIO/X"', card=card)
        self.assertEqual(r.globals["x"], 2)
        self.assertEqual(r.warnings,
                         ['dir "AUDIO/X": 2 names longer than 127 characters skipped'])

    def test_the_relative_path_of_a_deep_listing_counts(self):
        card = self.card(["AUDIO/X/" + "d" * 120 + "/abcdefgh.wav", "AUDIO/X/ok.wav"])
        r = self.go('dir "AUDIO/X" deep\nx file.len', card=card)
        self.assertEqual(r.globals["x"], 1)
        self.assertEqual(len(r.warnings), 1)

    def test_deep_descends_at_most_15_folders_and_is_silent_about_the_rest(self):
        # the unit's walker enters 15 folders below the listed one: a file in `a/b/.../` with n folders above it
        # is listed iff n <= 15 (relative name with n '/'); a deeper file is not visited, not counted, no warning
        self.assertEqual(O.DIR_DEPTH_MAX, 15)
        rels = ["/".join(chr(97 + k) for k in range(n)) + ("/" if n else "") + "f%d.wav" % n for n in (0, 1, 14, 15, 16, 17)]
        card = self.card(["AUDIO/X/" + r for r in rels])
        got = sim.list_card(card, "AUDIO/X", deep=True)
        self.assertEqual(sorted(got), sorted(r for r in rels if r.count("/") <= 15))
        self.assertIn(rels[3], got)                    # 15 folders: the last level listed
        self.assertNotIn(rels[4], got)                 # 16 folders: not entered
        self.assertNotIn(rels[5], got)
        self.assertEqual(sim.list_card(card, "AUDIO/X"), ["f0.wav"])      # not deep: direct files only
        r = self.go('dir "AUDIO/X" deep\nx file.len', card=card)
        self.assertEqual((r.globals["x"], r.warnings), (4, []))
        # the limit counts below the LISTED folder: from AUDIO/X/a, "b/../p/f16.wav" is 15 folders down, f17.wav is 16
        sub = sim.list_card(card, "AUDIO/X/a", deep=True)
        self.assertIn("/".join(chr(98 + k) for k in range(15)) + "/f16.wav", sub)
        self.assertFalse([n for n in sub if n.endswith("f17.wav")])

    def test_skip_happens_before_ext_and_match_and_the_warning_is_in_json(self):
        card = self.card(["AUDIO/X/k.wav", "AUDIO/X/" + "k" * 130 + ".wav"])
        r = self.go('dir "AUDIO/X"\next wav\nmatch "k*"\nx file.len', card=card)
        self.assertEqual(r.globals["x"], 1)
        self.assertEqual(sim.result_json(r)["warnings"], r.warnings)
        self.assertEqual(len(r.warnings), 1)


# ================================================================================================
def fnv1a(text):
    """FNV-1a 32 over UTF-8, written out independently of sim.py."""
    h = 2166136261
    for b in text.encode("utf-8"):
        h = ((h ^ b) * 16777619) % (1 << 32)
    return h


class TestCardAlignment(Base):
    """The simulator does what the unit can do: ASCII folding, canonical paths, a shared name budget,
    initial slots and refusals, bounded hashed pick bags, and CARD BUSY."""

    DOTLESS = "ı"          # i without a dot: str.upper() gives "I", the firmware folds ASCII only

    def abc(self, folder="A", names=("a.wav", "b.wav", "c.wav")):
        return self.card(["%s/%s" % (folder, n) for n in names])

    # ---- ASCII case folding ------------------------------------------------------------
    def test_fold_is_ascii_only(self):
        self.assertEqual(sim.fold("abcXYZ-_éıK"), "ABCXYZ-_éıK")
        self.assertEqual(sim.fold_lower("ABCÉ"), "abcÉ")

    def test_sort_does_not_fold_unicode(self):
        card = self.card(["A/" + n for n in ("Ia.wav", self.DOTLESS + ".wav", "b.wav")])
        # fold("dotless.wav") keeps its own bytes (0xC4 0xB1 ...): it sorts after every ASCII name
        self.assertEqual(sim.list_card(card, "A"),
                         ["b.wav", "Ia.wav", self.DOTLESS + ".wav"])
        self.assertEqual(sorted(["é.wav", "Éa.wav"], key=sim._sort_key),
                         ["Éa.wav", "é.wav"])       # Unicode upper() would put e-acute first

    def test_match_does_not_fold_unicode(self):
        self.assertFalse(sim.wildcard_match("i*", self.DOTLESS + ".wav"))
        self.assertFalse(sim.wildcard_match("É*", "é.wav"))
        self.assertFalse(sim.wildcard_match("é*", "É.wav"))
        self.assertTrue(sim.wildcard_match("K*", "kick.wav"))
        self.assertTrue(sim.wildcard_match("é*", "é.wav"))
        card = self.card(["A/" + self.DOTLESS + ".wav", "A/i.wav", "A/I2.wav"])
        r = self.go('dir "A"\nmatch "i*"\nx file.len', card=card)
        self.assertEqual(r.globals["x"], 2)                  # i.wav and I2.wav, not the dotless one

    def test_first_folder_lookup_does_not_fold_unicode(self):
        card = self.card([self.DOTLESS + "x/a.wav", "IX/b.wav"])
        self.assertEqual(sim.list_card(card, "ix"), ["b.wav"])          # IX, not the dotless folder
        self.assertEqual(sim.list_card(card, self.DOTLESS + "x"), ["a.wav"])
        self.assertEqual(sim.list_card(self.card([self.DOTLESS + "x/a.wav"]), "ix"), [])

    def test_ext_folds_ascii_only(self):
        self.assertEqual(sim.ext_key("a/x.WaV"), ".WAV")
        self.assertEqual(sim.ext_key("a.ıf"), ".ıF")      # str.upper() would give ".IF"
        r = self.go('dir "A"\next wav aif\nx file.len', card=self.card(["A/a.WaV", "A/b.AiF", "A/c.ıf"]))
        self.assertEqual(r.globals["x"], 2)

    # ---- canonical paths ---------------------------------------------------------------
    def test_canon_path(self):
        for raw, want in (("AUDIO/DRUMS", "AUDIO/DRUMS"), ("/AUDIO//DRUMS/", "AUDIO/DRUMS"),
                          ("AUDIO\\DRUMS\\", "AUDIO/DRUMS"), ("./A/./B/.", "A/B"), ("", ""),
                          ("/", ""), (" a ", " a "), ("a\\/b", "a/b"), ("audio/Drums", "audio/Drums")):
            self.assertEqual(sim.canon_path(raw), want, raw)
        self.assertEqual(sim.canon_path("../x"), "../x")        # kept: the list is empty anyway
        self.assertEqual(sim.list_card(self.abc(), "../A"), [])

    def test_slots_store_the_canonical_path(self):
        card = self.abc("AUDIO/DRUMS")
        for spelling in ("/AUDIO//DRUMS/", "AUDIO\\\\DRUMS", "./AUDIO/DRUMS/."):
            r = self.go('dir "%s"\nstatic.load 4 2\n@fill' % spelling, card=card)
            self.assertEqual(r.model.slots[4], "AUDIO/DRUMS/b.wav", spelling)
            self.assertEqual(r.model.slots[1], "AUDIO/DRUMS/a.wav", spelling)
        r = self.go('dir "audio/drums"\nstatic.load 1 1', card=card)      # the case the script wrote
        self.assertEqual(r.model.slots, {1: "audio/drums/a.wav"})
        r = self.go('dir ""\nstatic.load 1 1', card=self.card(["x.wav"]))          # the Set root
        self.assertEqual(r.model.slots, {1: "x.wav"})

    def test_warnings_quote_the_canonical_path(self):
        card = self.card(["AUDIO/X/k%04d.wav" % i for i in range(4100)])
        r = self.go('dir "/AUDIO//X/"', card=card)
        self.assertEqual(r.warnings, ['dir "AUDIO/X": 4100 files, only the first 4096 kept'])

    def test_spellings_of_one_folder_share_one_bag(self):
        card = self.abc()
        r = self.go('dir "A"\nx file.pick\ndir "/a//"\ny file.pick\ndir ".\\\\A"\nz file.pick', card=card)
        self.assertEqual(sorted(r.globals[k] for k in "xyz"), [1, 2, 3])
        self.assertEqual(len(r.state["bags"]), 1)

    # ---- the shared 4096-name budget ---------------------------------------------------
    def big(self):
        return self.card(["A/a%04d.wav" % i for i in range(3000)] +
                         ["B/b%04d.wav" % i for i in range(2000)])

    def test_a_callee_dir_gets_what_the_callers_list_leaves(self):
        card = self.big()
        helper = 'def helper\n  dir "B"\n  y file.len\n  static.load 1 file.at 1096\n  static.load 2 file.at 1097\n'
        r = self.go('dir "A"\nhelper\nx file.len', extra=helper, card=card)
        self.assertEqual((r.globals["x"], r.globals["y"]), (3000, 1096))
        self.assertEqual(r.warnings, ['dir "B": 2000 files, only the first 1096 kept'])
        self.assertEqual(r.model.slots, {1: "B/b1095.wav"})              # the FIRST names are kept
        self.assertEqual(len(r.oor), 1)                                  # file 1097 does not exist

    def test_a_copy_costs_nothing_until_the_callee_runs_dir(self):
        card = self.big()
        extra = ("def h1\n  h2\n  y file.len\ndef h2\n  h3\n  z file.len\n"
                 "def h3\n  t file.len\n  dir \"B\"\n  static.load 1 file.at 1096\n"
                 "  static.load 2 file.at 1097\n")
        r = self.go('dir "A"\nh1', extra=extra, card=card)
        self.assertEqual((r.globals["y"], r.globals["z"], r.globals["t"]), (3000, 3000, 3000))
        self.assertEqual(r.warnings, ['dir "B": 2000 files, only the first 1096 kept'])
        self.assertEqual(r.model.slots, {1: "B/b1095.wav"})             # h3's own list holds 1096

    def test_slots_come_back_when_the_frame_returns(self):
        card = self.big()
        r = self.go('dir "A"\nhelper\nhelper2', card=card,
                    extra='def helper\n  dir "B"\n  x file.len\ndef helper2\n  dir "B"\n  y file.len\n')
        self.assertEqual((r.globals["x"], r.globals["y"]), (1096, 1096))

    def test_slots_come_back_when_the_own_base_is_replaced(self):
        card = self.big()
        r = self.go('dir "A"\ndir "B"\nx file.len\nhelper', card=card,
                    extra='def helper\n  dir "A"\n  y file.len\n')
        # the caller's own base (3000) was replaced by B's 2000: the callee's A gets 4096 - 2000
        self.assertEqual((r.globals["x"], r.globals["y"]), (2000, 2096))
        self.assertEqual(r.warnings, ['dir "A": 3000 files, only the first 2096 kept'])

    def test_an_exhausted_budget_keeps_nothing_and_the_warning_says_so(self):
        card = self.card(["A/a%04d.wav" % i for i in range(4096)] + ["B/b.wav"])
        r = self.go('dir "A"\nhelper', card=card, extra='def helper\n  dir "B"\n  x file.len\n')
        self.assertEqual(r.globals["x"], 0)
        self.assertEqual(r.warnings, ['dir "B": 1 files, only the first 0 kept'])

    def test_long_names_are_removed_before_the_budget_cut(self):
        card = self.card(["A/a%04d.wav" % i for i in range(3000)] +
                         ["B/b%04d.wav" % i for i in range(1100)] + ["B/" + "z" * 130 + ".wav"])
        r = self.go('dir "A"\nhelper', card=card, extra='def helper\n  dir "B"\n  x file.len\n')
        self.assertEqual(r.warnings, ['dir "B": 1 names longer than 127 characters skipped',
                                      'dir "B": 1100 files, only the first 1096 kept'])

    # ---- initial slots, refusals ---------------------------------------------------------
    def test_initial_slots(self):
        card = self.abc()
        r = self.go('x static.free 5\ny static.firstfree\ndir "A"\n@fill', card=card,
                    slots={5: "AUDIO/x.wav", 1: "AUDIO/y.wav", "2": "AUDIO/z.wav"})
        self.assertEqual((r.globals["x"], r.globals["y"]), (0, 3))
        self.assertEqual(r.model.slots, {1: "AUDIO/y.wav", 2: "AUDIO/z.wav", 3: "A/a.wav",
                                         4: "A/b.wav", 5: "AUDIO/x.wav", 6: "A/c.wav"})
        self.assertEqual(r.before.slots, {1: "AUDIO/y.wav", 2: "AUDIO/z.wav", 5: "AUDIO/x.wav"})
        self.assertIn("3 <- A/a.wav", sim.render_text(r))
        self.assertNotIn("5 <- AUDIO/x.wav", sim.render_text(r))          # it was there before
        r = self.go("x static.firstfree")                                    # default: all free
        self.assertEqual((r.globals["x"], r.model.slots), (1, {}))
        r = self.go("@clear", slots={"7": "a\\b/c.wav"})                 # canonicalised
        self.assertEqual(r.before.slots, {7: "a/b/c.wav"})

    def test_bad_initial_slots_are_refused(self):
        prog = self.prog("x 1")
        for bad in ({0: "a.wav"}, {129: "a.wav"}, {"x": "a.wav"}, {1: ""}, {1: 5}, {1: "../a.wav"}):
            with self.assertRaises(sim.SimError, msg=repr(bad)):
                sim.simulate(prog, "main", seed=1, slots=bad)

    def test_a_refused_load_leaves_the_slot_free_and_the_script_goes_on(self):
        r = self.go('dir "A"\nstatic.load 3 2\nx static.free 3\nstatic.load 4 1\ny static.firstfree',
                    card=self.abc(), refuse={"A/b.wav"})
        self.assertEqual(r.model.slots, {4: "A/a.wav"})
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 1))
        self.assertEqual(r.refused, [("main", "static.load", 4, 3, "A/b.wav")])   # line 4: after `menu`

    def test_a_refused_load_on_an_occupied_slot_frees_it(self):
        """The unit clears the slot on a refusal (a stock routine), so the old file goes too."""
        r = self.go('dir "A"\nstatic.load 3 2\nx static.free 3', card=self.abc(),
                    slots={3: "AUDIO/old.wav", 9: "AUDIO/keep.wav"}, refuse={"A/b.wav"})
        self.assertEqual((r.model.slots, r.globals["x"]), ({9: "AUDIO/keep.wav"}, 1))
        self.assertEqual(len(r.refused), 1)

    def test_refuse_as_a_set_folds_ascii_case_and_as_a_predicate_sees_the_stored_path(self):
        card = self.abc()
        r = self.go('dir "a"\nstatic.load 1 2', card=card, refuse={"A/B.WAV"})
        self.assertEqual((r.model.slots, len(r.refused)), ({}, 1))
        seen = []

        def pred(path):
            seen.append(path)
            return path.endswith("c.wav")
        r = self.go('dir "/a/"\nstatic.load 1 3\nstatic.load 2 1', card=card, refuse=pred)
        self.assertEqual(seen, ["a/c.wav", "a/a.wav"])
        self.assertEqual(r.model.slots, {2: "a/a.wav"})

    def test_nothing_is_refused_by_default(self):
        r = self.go('dir "A"\n@fill', card=self.abc())
        self.assertEqual(r.refused, [])
        self.assertEqual(sim.result_json(r)["refused"], [])
        self.assertNotIn("refused", sim.render_text(r))

    def test_fill_loop_offers_a_refused_slot_to_the_next_file(self):
        card = self.card(["A/%s.wav" % c for c in "abcde"])
        r = self.go('dir "A"\n@fill', card=card, slots={1: "Z/z.wav"},
                    refuse={"A/b.wav", "A/c.wav"})
        # free slots 2, 3, 4, ...; a -> 2, b refused, c refused, d -> 3, e -> 4
        self.assertEqual(r.model.slots, {1: "Z/z.wav", 2: "A/a.wav", 3: "A/d.wav", 4: "A/e.wav"})
        self.assertEqual([(s, p) for _, _, _, s, p in r.refused], [(3, "A/b.wav"), (3, "A/c.wav")])

    def test_fill_loop_when_every_file_is_refused(self):
        r = self.go('dir "A"\n@fill', card=self.abc(), refuse=lambda p: True)
        self.assertEqual(r.model.slots, {})
        self.assertEqual([(s, p) for _, _, _, s, p in r.refused],
                         [(1, "A/a.wav"), (1, "A/b.wav"), (1, "A/c.wav")])

    def test_fill_loop_stops_when_the_slots_run_out(self):
        card = self.card(["A/%03d.wav" % i for i in range(130)])
        r = self.go('dir "A"\n@fill', card=card, slots={5: "Z/z.wav"}, refuse={"A/001.wav"})
        self.assertEqual(sorted(r.model.slots), list(range(1, 129)))
        self.assertEqual(len(r.refused), 1)
        self.assertEqual(r.model.slots[1], "A/000.wav")
        self.assertEqual(r.model.slots[2], "A/002.wav")             # the slot of the refused 001

    def test_refusal_in_json_and_in_the_text_report(self):
        r = self.go('dir "A"\nstatic.load 3 2', card=self.abc(), refuse={"A/b.wav"})
        j = sim.result_json(r)
        self.assertEqual(j["refused"], [{"func": "main", "op": "static.load", "line": 4, "slot": 3,
                                         "path": "A/b.wav"}])
        self.assertIn("main: 1 refused by the unit, slot left free (static.load line 4 slot 3 A/b.wav)",
                      sim.render_text(r))
        r = self.go('dir "A"\nl 1 12: static.load 3 2', card=self.abc(), refuse={"A/b.wav"})
        self.assertIn("; +4 more)", sim.render_text(r))
        self.assertEqual(len(sim.result_json(r)["refused"]), 12)

    def test_the_vm_shares_the_card_semantics(self):
        from modules.otype import vm
        prog = self.prog('dir "A"\nhelper\ndir "B"\n@fill\nstatic.load 1 2',
                         'def helper\n  dir "B"\n  x file.len\n')
        kw = dict(seed=1, card=self.big(), slots={9: "Z/z.wav"}, refuse={"B/b0000.wav", "B/b0003.wav"})
        a = sim.simulate(prog, "main", **kw)
        b = vm.run_vm(prog, "main", **kw)
        self.assertEqual(sim.result_json(a), sim.result_json(b))
        self.assertTrue(a.refused)
        self.assertEqual(a.warnings, ['dir "B": 2000 files, only the first 1096 kept'])

    def test_cli_slots_and_refuse(self):
        card = self.abc()
        d = self.tmp()
        slots = os.path.join(d, "slots.json")
        with open(slots, "w") as fh:
            json.dump({"2": "A/old.wav"}, fh)
        path = os.path.join(d, "a.OTY")
        with open(path, "w") as fh:
            fh.write('def main\n  dir "A"\n  l 1 128:\n    if gt i file.len: break\n    a static.firstfree\n    if ez a: break\n    static.load a file.at i\n')
        out, err = io.StringIO(), io.StringIO()
        code = sim.main(["main", path, "--card", card, "--slots", slots, "--refuse", "B*", "--json",
                         "--seed", "1"], checker=otype, out=out, err=err)
        self.assertEqual((code, err.getvalue()), (0, ""))
        j = json.loads(out.getvalue())
        self.assertEqual(j["model"]["slots"], {"1": "A/a.wav", "2": "A/old.wav", "3": "A/c.wav"})
        self.assertEqual([(r["slot"], r["path"]) for r in j["refused"]], [(3, "A/b.wav")])
        out, err = io.StringIO(), io.StringIO()
        code = sim.main(["main", path, "--card", card, "--refuse", "b.WAV", "--refuse", "x*"],
                        checker=otype, out=out, err=err)
        self.assertEqual(code, 0)
        self.assertIn("1 refused by the unit", out.getvalue())
        for bad in ('{"0": "a.wav"}', "[1]", "not json"):
            with open(slots, "w") as fh:
                fh.write(bad)
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(sim.main(["main", path, "--slots", slots], checker=otype, out=out, err=err),
                             2, bad)
            self.assertIn("--slots", err.getvalue())

    # ---- bounded, hashed pick bags -----------------------------------------------------
    def test_sig_hash_is_fnv1a_32(self):
        self.assertEqual(sim.sig_hash(""), 0x811C9DC5)
        self.assertEqual(sim.sig_hash("a"), 0xE40C292C)
        self.assertEqual(sim.sig_hash("foobar"), 0xBF9CF968)
        for s in ("A|||3", "AUDIO/é|deep|ext:wav,match:K*|17"):
            self.assertEqual(sim.sig_hash(s), fnv1a(s))

    def test_the_signature_folds_the_path_ascii_only(self):
        fl = sim.FileList("audio/Drıms", True, ["a", "b"])
        fl.filters = ["ext:aif+wav", "match:K*+B*"]
        self.assertEqual(fl.sig, "AUDIO/DRıMS|deep|ext:aif+wav,match:K*+B*|2")
        self.assertEqual(sim.FileList("A", False, ["a"]).sig, "A|||1")

    def test_bags_are_listed_in_the_state_in_age_order(self):
        card = self.card(["A/a.wav", "A/b.wav", "A/c.wav", "B/d.wav", "B/e.wav"])
        r = self.go('dir "a"\nx file.pick\ndir "B" deep\next wav\ny file.pick\ny file.pick\n', card=card)
        bags = r.state["bags"]
        self.assertEqual([(b["func"], b["hash"], b["n"]) for b in bags],
                         [("main", fnv1a("A|||3"), 3), ("main", fnv1a("B|deep|ext:wav|2"), 2)])
        self.assertEqual(sorted(bags[0]["rest"] + [r.globals["x"]]), [1, 2, 3])
        self.assertEqual(bags[1]["rest"], [])
        self.assertNotIn("pick", r.state["funcs"].get("main", {}))
        self.assertEqual(json.loads(json.dumps(r.state)), r.state)

    def lists9(self):
        return self.card(["F%d/%s.wav" % (i, c) for i in range(9) for c in "ab"])

    def test_at_most_eight_bags_and_the_oldest_is_dropped(self):
        r = self.go("".join('dir "F%d"\nx file.pick\n' % i for i in range(9)), card=self.lists9())
        self.assertEqual([b["hash"] for b in r.state["bags"]],
                         [fnv1a("F%d|||2" % i) for i in range(1, 9)])
        self.assertEqual(O.PICK_BAGS_MAX, 8)

    def test_reusing_a_bag_does_not_refresh_its_age(self):
        body = "".join('dir "F%d"\nx file.pick\n' % i for i in range(8))
        body += 'dir "F0"\nx file.pick\ndir "F8"\nx file.pick\n'        # F0 reused, then a 9th list
        r = self.go(body, card=self.lists9())
        self.assertEqual([b["hash"] for b in r.state["bags"]],
                         [fnv1a("F%d|||2" % i) for i in range(1, 9)])      # F0 is gone, though reused last

    def test_bags_are_kept_per_function_and_the_cap_counts_them_all(self):
        r = self.go('dir "A"\nx file.pick\nhelper\ny file.pick', card=self.abc(),
                    extra='def helper\n  z file.pick\n')
        self.assertEqual([(b["func"], b["hash"]) for b in r.state["bags"]],
                         [("main", fnv1a("A|||3")), ("helper", fnv1a("A|||3"))])
        r = self.go("".join('dir "F%d"\nx file.pick\n' % i for i in range(8)) + "helper",
                    card=self.lists9(), extra='def helper\n  dir "F8"\n  z file.pick\n')
        self.assertEqual(len(r.state["bags"]), 8)
        self.assertEqual(r.state["bags"][-1]["func"], "helper")
        self.assertEqual(r.state["bags"][0]["hash"], fnv1a("F1|||2"))

    def test_bags_survive_a_press_and_an_old_state_is_read(self):
        card = self.abc()
        prog = self.prog('dir "A"\nx file.pick')
        r1 = sim.simulate(prog, "main", seed=1, card=card)
        r2 = sim.simulate(prog, "main", seed=1, card=card, state=json.loads(json.dumps(r1.state)))
        r3 = sim.simulate(prog, "main", seed=1, card=card, state=r2.state)
        self.assertEqual(sorted([r1.globals["x"], r2.globals["x"], r3.globals["x"]]), [1, 2, 3])
        self.assertEqual(r3.state["bags"][0]["rest"], [])
        # an older state (per-function `pick` by signature) and a malformed bag: no crash
        old = {"funcs": {"main": {"flip": 1, "pick": {"A|||3": [1]}}}, "bags": [{"func": "main"}, 7]}
        r = sim.simulate(prog, "main", seed=1, card=card, state=old)
        self.assertEqual(len(r.state["bags"]), 1)
        self.assertEqual(r.state["funcs"]["main"]["flip"], 1)

    def test_a_state_with_too_many_bags_keeps_the_newest_eight(self):
        bags = [{"func": "main", "hash": i, "n": 2, "rest": [1]} for i in range(12)]
        r = sim.simulate(self.prog("x 1"), "main", seed=1, state={"bags": bags})
        self.assertEqual([b["hash"] for b in r.state["bags"]], list(range(4, 12)))

    # ---- CARD BUSY -------------------------------------------------------------------------
    def test_card_busy_stops_the_run_with_exactly_that_message(self):
        with self.assertRaises(sim.RunError) as cm:
            self.go('x 5\ndir "A"\ny 7', card=self.abc(), busy={"dir"})
        self.assertEqual(str(cm.exception), "CARD BUSY")
        self.assertIsInstance(cm.exception, sim.CardBusy)
        self.assertEqual(cm.exception.result.globals["x"], 5)        # what ran before stays done
        self.assertEqual(cm.exception.result.globals["y"], 0)        # nothing after the access

    def test_card_busy_does_not_undo_what_already_happened(self):
        with self.assertRaises(sim.CardBusy) as cm:
            self.go('dir "A"\nstatic.load 1 1\nstatic.load 2 2\nstatic.clr 1\nx 9', card=self.abc(),
                    busy=lambda op: op == "STATIC.CLR")
        res = cm.exception.result
        self.assertEqual((res.model.slots, res.globals["x"]), ({1: "A/a.wav", 2: "A/b.wav"}, 0))

    def test_every_card_access_asks_the_hook_and_other_ops_do_not(self):
        card = self.abc()
        asked = []
        self.go('dir "A"\nstatic.load 1 1\nx static.free 1\ny static.firstfree\nslots.add 1\nstatic.clr 1\n'
                'z file.len', card=card,
                busy=lambda op: asked.append(op) or False)
        self.assertEqual(asked, ["DIR", "STATIC.LOAD", "STATIC.CLR"])
        for op, body in (("static.load", 'dir "A"\nstatic.load 1 1'), ("static.clr", "static.clr 1"),
                         ("dir", 'dir "A"')):
            slots = {1: "A/a.wav", 2: "A/b.wav"}
            with self.assertRaises(sim.CardBusy, msg=op):
                self.go(body, card=card, slots=slots, busy={op})
            self.go(body, card=card, slots=slots, busy={"tr.add"})      # another op: not busy

    def test_no_work_means_no_card_access(self):
        busy = {"static.load", "static.clr"}
        r = self.go('static.load 1 1\nstatic.clr 4\ndir "A"\nstatic.clr 4',
                    card=self.abc(), busy=busy)
        self.assertEqual(len(r.oor), 1)                              # static.load without a list: oor
        self.go('static.load 1 9', card=self.abc(), busy={"static.load"})        # file out of range: oor

    def test_busy_set_is_case_insensitive_and_the_vm_stops_too(self):
        from modules.otype import vm
        prog = self.prog('dir "A"\nx 3')
        for busy in ({"DIR"}, {"dir"}):
            with self.assertRaises(sim.CardBusy) as cm:
                vm.run_vm(prog, "main", seed=1, card=self.abc(), busy=busy)
            self.assertEqual(str(cm.exception), "CARD BUSY")

    def test_card_busy_is_an_error_exit_on_the_command_line(self):
        d = self.tmp()
        path = os.path.join(d, "a.OTY")
        with open(path, "w") as fh:
            fh.write('def main\n  dir "A"\n')
        out, err = io.StringIO(), io.StringIO()
        real = sim.simulate

        def busy_simulate(*a, **kw):
            return real(*a, busy={"dir"}, **kw)
        sim.simulate = busy_simulate
        try:
            code = sim.main(["main", path, "--card", self.abc()], checker=otype, out=out, err=err)
        finally:
            sim.simulate = real
        self.assertEqual((code, err.getvalue()), (1, "main: CARD BUSY\n"))


# ================================================================================================
M1_FILE = os.path.join(ROOT, "modules", "otype", "examples", "M1.OTY")
ELKA = ["kick_2.wav", "kick_3.wav", "kick_4.wav", "kick_5.wav", "kick_6.wav", "kick_clave.wav",
        "kick_hat_4.wav", "kick_snare_hat.wav", "hat_5.wav", "long_hat.wav", "cowbell_hat.wav",
        "hat_clave_2.wav"]


class TestM1Scripts(Base):
    """The three scripts of M1.OTY on a card shaped like the author's (the Set folder is the root)."""
    def setUp(self):
        files = ["AUDIO/BREAKS/break_%03d.wav" % i for i in range(40)]
        files += ["AUDIO/DRUMS/ELKA/" + n for n in ELKA]
        files += ["AUDIO/DRUMS/ND2/nd2_snare.wav", "AUDIO/DRUMS/ND2/nd2_tom.wav",
                  "AUDIO/DRUMS/ND2/nd2_clap.wav", "AUDIO/DRUMS/._kick_9.wav",
                  "AUDIO/DRUMS/ELKA/._kick_9.wav"]
        self.card_dir = make_card(self.tmp(), files)
        self.prog_m1 = otype.check_files([M1_FILE])
        self.assertEqual([str(d) for d in self.prog_m1.diags if d.level == "error"], [])

    def run_m1(self, name, seed=1):
        return sim.simulate(self.prog_m1, name, seed=seed, card=self.card_dir)

    def test_kicks_loads_eight_kick_files_into_slots_1_to_8(self):
        r = self.run_m1("kicks")
        want = ["AUDIO/DRUMS/ELKA/" + n for n in
                ["kick_2.wav", "kick_3.wav", "kick_4.wav", "kick_5.wav", "kick_6.wav",
                 "kick_clave.wav", "kick_hat_4.wav", "kick_snare_hat.wav"]]
        self.assertEqual(sorted(r.model.slots), list(range(1, 9)))
        self.assertEqual(sorted(r.model.slots.values()), want)
        self.assertEqual(r.warnings, [])

    def test_kit_lock_loads_four_hat_files(self):
        for seed in (1, 2, 3):
            r = self.run_m1("kit_lock", seed)
            got = [p for _s, p in sorted(r.model.slots.items())]
            self.assertEqual(sorted(r.model.slots), [1, 2, 3, 4], seed)
            self.assertEqual(len(set(got)), 4, got)
            for p in got:
                self.assertTrue(p.startswith("AUDIO/DRUMS/ELKA/") and "hat" in p, p)
            self.assertEqual(r.warnings, [])

    def test_four_puts_trigs_on_1_5_9_13(self):
        for seed in (1, 2, 3, 4):
            r = self.run_m1("four", seed)
            t = set(self.trigs(r))
            self.assertTrue({1, 5, 9, 13} <= t, (seed, t))
            self.assertTrue(t <= {1, 5, 9, 13, 15}, (seed, t))

    def test_run_card_cli_takes_the_set_folder(self):
        out, err = io.StringIO(), io.StringIO()
        rc = sim.main(["kicks", M1_FILE, "--card", self.card_dir, "--seed", "1"],
                      checker=otype, out=out, err=err)
        out, err = out.getvalue(), err.getvalue()
        self.assertEqual(rc, 0, err)
        self.assertIn("ELKA/kick_2.wav", out)


# ================================================================================================
class TestBank(Base):
    def test_decode_at_the_documented_offsets(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank03.work"), pattern=3)
        tracks, info = sim.read_bank(path, 3)
        t1 = tracks[1]
        self.assertEqual([s for s in range(1, 65) if t1.has("trig", s)], [1, 5])
        self.assertEqual([s for s in range(1, 65) if t1.has("tless", s)], [3])
        self.assertEqual(t1.locks, {3: {15: 100}})
        self.assertEqual(t1.smp, {3: 5})                         # byte b = slot b + 1
        self.assertEqual(t1.count, {7: 3})
        self.assertEqual(t1.micro, {7: 5, 8: -3})
        self.assertEqual(t1.cond, {7: 19, 8: 1})
        self.assertEqual([s for s in range(1, 65) if tracks[2].has("trig", s)], [2])
        self.assertEqual(tracks[3].masks["trig"], 0)
        self.assertEqual(info["pattern_length"], 24)
        self.assertFalse(info["per_track"])

    def test_the_card_diff_of_2026_10_01_decodes(self):
        """The stock writer's bytes of a bank
        (bank 01 pattern A01 track 1), reproduced: byte 7 of a mask is steps 1..8 with bit 0 = step 1;
        a one-shot is in mask 3 only; the AMP VOL lock is byte 15 of the step record; a trig word
        is 16 bits big-endian (condition 19 = 50 %, bits 15-13 = 2 = 3 trigs)."""
        path = synthetic_bank(os.path.join(self.tmp(), "bank01.work"), pattern=1)
        with open(path, "r+b") as fh:
            tr = sim.trac_off(0, 0)
            for off, val in ((0x00 + 7, 0x19), (0x08 + 7, 0x02), (0x18 + 7, 0x44), (0x10 + 7, 0x00)):
                fh.seek(tr + off)
                fh.write(bytes([val]))
            for o in range(0x899, 0x899 + 16 * 2):                       # clear the synthetic words
                fh.seek(tr + o)
                fh.write(b"\x00")
            fh.seek(tr + 0x59 + 2 * 0x20)                                # no lock on step 3
            fh.write(b"\xff" * 32)
            fh.seek(tr + 0x59 + 6 * 0x20 + 15)                           # step 7: AMP VOL = 127
            fh.write(bytes([0x7f]))
            fh.seek(tr + 0x899 + 3 * 2)
            fh.write((0x0013).to_bytes(2, "big"))                        # step 4: condition 19
            fh.seek(tr + 0x899 + 4 * 2)
            fh.write((0x4000).to_bytes(2, "big"))                        # step 5: 3 trigs
        t1 = sim.read_bank(path, 1)[0][1]
        kinds = {m: [s for s in range(1, 65) if t1.has(m, s)] for m in ("trig", "tless", "tlock", "oneshot")}
        self.assertEqual(kinds, {"trig": [1, 4, 5], "tless": [2], "tlock": [], "oneshot": [3, 7]})
        self.assertEqual(t1.locks, {7: {15: 127}})
        self.assertEqual((t1.cond, t1.count, t1.micro), ({4: 19}, {5: 3}, {}))
        self.assertEqual(sim.cond_name(19), "50%")
        for s in (1, 2, 3, 4, 5, 7):
            self.assertTrue(t1.has_any_trig(s))
        self.assertFalse(t1.has_trig(6))

    def test_other_patterns_are_empty(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank03.work"), pattern=3)
        tracks, _ = sim.read_bank(path, 4)
        self.assertEqual([t.masks["trig"] for t in tracks.values()], [0] * 8)
        self.assertEqual([t.locks for t in tracks.values()], [{}] * 8)

    def test_length_comes_from_the_bank_and_len_overrides_it(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank03.work"), length=24)
        prog = self.prog("tr.add 1")
        r = sim.simulate(prog, "main", seed=1, bank=path, pattern=3)
        self.assertEqual((r.length, r.length_source), (24, "pattern, bank"))
        r = sim.simulate(prog, "main", seed=1, bank=path, pattern=3, length=40)
        self.assertEqual((r.length, r.length_source), (40, "--len"))

    def test_per_track_length_mode(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank03.work"), mode=1, length=24,
                              track_lengths={1: 12, 2: 20})
        prog = self.prog("x pat.len")
        r = sim.simulate(prog, "main", seed=1, bank=path, pattern=3, track=1)
        self.assertEqual((r.globals["x"], r.length_source), (12, "per-track length, bank"))
        r = sim.simulate(prog, "main", seed=1, bank=path, pattern=3, track=2)
        self.assertEqual(r.globals["x"], 20)

    def test_a_script_acts_on_the_decoded_pattern(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank03.work"))
        r = self.go("steps has.trig:\n  tr.clr\ntrk 2:\n  tr.add 9", bank=path, pattern=3)
        self.assertEqual(self.trigs(r, 1), [])
        self.assertEqual(self.trigs(r, 2), [2, 9])
        self.assertEqual(r.model.tracks[1].locks, {})                 # removal wipes the step's locks (10.14)

    def test_the_bank_file_is_never_written(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank03.work"))
        h = digest(path)
        mt = os.stat(path).st_mtime_ns
        self.go("tr.clr\ntr.add 4\npl.rnd amp.vol\ntrk all:\n  trk.init", bank=path, pattern=3)
        self.assertEqual(digest(path), h)
        self.assertEqual(os.stat(path).st_mtime_ns, mt)

    def test_errors(self):
        d = self.tmp()
        path = synthetic_bank(os.path.join(d, "bank01.work"))
        for p in (0, 17):
            with self.assertRaises(sim.BankError):
                sim.read_bank(path, p)
        junk = os.path.join(d, "junk.work")
        with open(junk, "wb") as fh:
            fh.write(b"not a bank file at all" * 100)
        with self.assertRaises(sim.BankError):
            sim.read_bank(junk, 1)
        short = os.path.join(d, "short.work")
        with open(short, "wb") as fh:
            fh.write(read_bytes(path)[:5000])
        with self.assertRaises(sim.BankError):
            sim.read_bank(short, 1)

    def test_bank_letter(self):
        self.assertEqual([sim.bank_letter(p) for p in ("bank01.work", "x/bank16.work", "bank03.work",
                                                       "weird.work", None)],
                         ["A", "P", "C", "A", "A"])

    WORKDATA = os.environ.get("OTYPE_BANKS", "")      # optional: a folder of real bank*.work files

    def real_banks(self):
        return sorted(glob.glob(os.path.join(self.WORKDATA, "**", "bank*.work"), recursive=True))

    def test_real_bank_smoke_pattern_1(self):
        banks = self.real_banks()
        if not banks:
            self.skipTest("set OTYPE_BANKS to a folder of bank*.work files")
        prog = self.prog("steps has.trig:\n  each: pl.set i amp.vol 100\nl 1 16: prob 25: tr.add i")
        for path in banks[:3] + banks[-1:]:
            with self.subTest(path=path):
                r = sim.simulate(prog, "main", seed=1, bank=path, pattern=1)
                self.assertTrue(1 <= r.length <= 64)
                self.assertEqual(r.length_source, "pattern, bank")
                self.assertIn("A01" if path.endswith("bank01.work") else "", sim.render_text(r))

    def test_real_banks_decode_every_pattern(self):
        banks = self.real_banks()
        if not banks:
            self.skipTest("set OTYPE_BANKS to a folder of bank*.work files")
        for p in range(1, 17):
            tracks, info = sim.read_bank(banks[0], p)
            self.assertEqual(sorted(tracks), list(range(1, 9)))
            for td in tracks.values():
                for s, cond in td.cond.items():
                    self.assertTrue(1 <= cond <= 64 and 1 <= s <= 64)

    def test_real_bank_with_a_known_length(self):
        # the capstress6 bank has a pattern of length 32 (tail bytes 0x20, scale 2)
        hit = [b for b in self.real_banks() if "capstress6_hand" in b and b.endswith("bank01.work")]
        if not hit:
            self.skipTest("capstress6_hand/bank01.work not present")
        r = sim.simulate(self.prog("x pat.len"), "main", seed=1, bank=hit[0], pattern=1)
        self.assertEqual(r.globals["x"], 32)


# ================================================================================================
EXAMPLES = sorted(glob.glob(os.path.join(ROOT, "modules", "otype", "examples", "*.OTY")))


class TestExamples(Base):
    def test_examples_exist(self):
        self.assertGreaterEqual(len(EXAMPLES), 4)

    def test_every_menu_function_runs_with_seed_1(self):
        prog = otype.check_files(EXAMPLES)
        ran = []
        for f in prog.funcs:
            if f.refused or not f.menus or f.args:
                continue
            with self.subTest(func=O.show(f.name)):
                r = sim.simulate(prog, f.name, seed=1)
                text = sim.render_text(r)
                self.assertIn("seed 1", text)
                self.assertLessEqual(r.cost, r.max_cost)
                json.dumps(sim.result_json(r))
                ran.append(O.show(f.name))
        self.assertGreaterEqual(len(ran), 6, ran)

    def test_every_menu_function_runs_with_a_card_and_seed_1(self):
        card = self.card(["AUDIO/DRUMS/KICKS/k%02d.wav" % i for i in range(1, 9)] +
                         ["AUDIO/DRUMS/SNARES/s%02d.wav" % i for i in range(1, 5)] +
                         ["AUDIO/DRUMS/HIHATS/h%02d.wav" % i for i in range(1, 9)])
        prog = otype.check_files(EXAMPLES)
        r = sim.simulate(prog, "boombap", seed=1, card=card)
        self.assertEqual(r.oor, [])
        self.assertEqual(len(r.model.slots), 3 + 4)       # three play_one + four lock_samples slots
        self.assertEqual(r.model.part[1].slot, 1)

    def test_same_seed_same_result_and_different_seeds_differ(self):
        prog = otype.check_files(EXAMPLES)
        a = sim.result_json(sim.simulate(prog, "sprinkle", seed=1))
        b = sim.result_json(sim.simulate(prog, "sprinkle", seed=1))
        c = sim.result_json(sim.simulate(prog, "sprinkle", seed=2))
        self.assertEqual(a, b)
        self.assertNotEqual(a["model"], c["model"])

    def test_walk_example_over_four_presses(self):
        """RHYTHM.OTY walk (corrected 04/10): o is read once per step and goes on from press to press (1..8, 9..16,
        1..8 ...: it used to be reset by `o 1` at the end, every press the same); the drunk walk starts mid-range and goes on
        too; every lock lands on a trig."""
        prog = otype.check_files(EXAMPLES)
        state = None
        counters, trigs, vols = [], [], []
        for k in range(4):
            r = sim.simulate(prog, "walk", seed=k + 1, state=state)
            state = json.loads(json.dumps(r.state))
            counters.append(state["funcs"]["walk"]["o"])
            d = sim.result_json(r)
            self.assertEqual(d["skipped_no_trig"], [])
            t = d["model"]["tracks"]["1"]
            trigs.append(t["masks"]["trig"])
            vols.append([t["steps"][s]["locks"]["amp.vol"] for s in sorted(t["steps"], key=int)])
        self.assertEqual(counters, [9, 1, 9, 1])
        self.assertEqual(trigs, ["0x00000000000000ff", "0x000000000000ff00"] * 2)
        self.assertTrue(all(len(v) == 8 and all(60 <= x <= 100 for x in v) for v in vols), vols)   # around 80, not at 40
        self.assertNotEqual(vols[0], vols[2])                                                  # the walk goes on

    def test_ab_euclid_example_locks_every_trig(self):
        """RHYTHM.OTY ab_euclid (corrected 04/10): A = er hits 16 with dr.v 1, B = er.r 3 8 2 in A's gaps with dr.v 2;
        every lock lands on a trig (3 of the 6 B accents used to fall on empty steps), every trig but the step-16 pickup has a
        lock, and the pickup really adds a step half the time."""
        prog = otype.check_files(EXAMPLES)
        picked = 0
        for hits in (1, 3, 5, 8, 16):
            for seed in range(8):
                d = sim.result_json(sim.simulate(prog, "ab_euclid", seed=seed, params={"hits": hits}))
                self.assertEqual(d["skipped_no_trig"], [], (hits, seed))
                t = d["model"]["tracks"]["1"]
                mask = int(t["masks"]["trig"], 16)
                on = [s for s in range(1, 17) if mask >> (s - 1) & 1]
                a = [s for s in range(1, 17) if sim.euclid_hit(hits, 16, 0, s)]
                b = [s for s in range(1, 17) if s not in a and sim.euclid_hit(3, 8, 2, s)]
                self.assertEqual(sorted(set(on) - {16}), sorted(set(a + b) - {16}), (hits, seed))
                locks = {int(s): v["locks"]["amp.vol"] for s, v in t["steps"].items()}
                self.assertEqual(locks, {**{s: sim.drv(1, s) for s in a}, **{s: sim.drv(2, s) for s in b}}, (hits, seed))
                picked += 16 in on and 16 not in a + b
        self.assertTrue(0 < picked < 40, picked)


# ================================================================================================
OTY = "def main\n  menu \"T\" >> \"MAIN\"\n  l 1 4: tr.add i\n  x add x 1\n"


class TestCli(Base):
    def write(self, text=OTY, name="a.OTY"):
        p = os.path.join(self.tmp(), name)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = sim.main(list(argv), checker=otype, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_plain_run(self):
        code, out, err = self.cli("main", self.write(), "--seed", "3")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("main  seed 3 (--seed)  pattern A01 len 16 (default)  track 1", out)
        self.assertIn("XXXX", out)
        self.assertIn("cost:", out)

    def test_help_and_usage(self):
        code, out, err = self.cli("-h")
        self.assertEqual(code, 0)
        self.assertIn("--seed N", out)
        code, out, err = self.cli()
        self.assertEqual(code, 2)
        code, out, err = self.cli("main")                         # no file
        self.assertEqual(code, 2)
        code, out, err = self.cli("main", self.write(), "--bogus")
        self.assertEqual(code, 2)

    def test_json(self):
        code, out, _ = self.cli("main", self.write(), "--seed", "3", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual((d["func"], d["seed"], d["length"], d["bank"]), ("main", 3, 16, None))
        self.assertEqual(d["globals"]["x"], 1)
        self.assertEqual(d["model"]["tracks"]["1"]["masks"]["trig"], "0x000000000000000f")
        self.assertEqual(set(d["draws"]), {"rand", "toss", "prob", "drunk", "file"})

    def test_verbose_shows_all_eight_tracks(self):
        code, plain, _ = self.cli("main", self.write(), "--seed", "1")
        code, verb, _ = self.cli("main", self.write(), "--seed", "1", "-v")
        self.assertTrue(plain.startswith("main") and "T1" in plain)
        self.assertNotIn("T2", plain)                              # only the tracks that hold something
        self.assertTrue(all(("T%d" % t) in verb for t in range(1, 9)))

    def test_state_file_over_three_presses(self):
        path = self.write("def main\n  x add x 1\n  trk 1:\n    pl.set x amp.vol y\n  y flip\n"
                          "  every 2: tr.add x\n")
        st = os.path.join(self.tmp(), "state.json")
        xs = []
        for _ in range(3):
            code, out, err = self.cli("main", path, "--seed", "1", "--state", st, "--json")
            self.assertEqual((code, err), (0, ""))
            xs.append(json.loads(out)["globals"]["x"])
        self.assertEqual(xs, [1, 2, 3])
        with open(st) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["globals"]["x"], 3)
        self.assertIn("main", saved["funcs"])

    def test_missing_state_file_starts_fresh_and_a_broken_one_is_refused(self):
        st = os.path.join(self.tmp(), "nope.json")
        code, out, err = self.cli("main", self.write(), "--state", st)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(st))
        with open(st, "w") as fh:
            fh.write("{ not json")
        code, out, err = self.cli("main", self.write(), "--state", st)
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_param_override_and_clamp(self):
        path = self.write('def main\n  menu "T" >> "M"\n  param n 5 1 9\n  x n\n')
        code, out, _ = self.cli("main", path, "--param", "n=7", "--json")
        self.assertEqual(json.loads(out)["globals"]["x"], 7)
        code, out, _ = self.cli("main", path, "--param", "N=99", "--json")
        d = json.loads(out)
        self.assertEqual(d["globals"]["x"], 9)
        self.assertTrue(d["warnings"] and "clamped" in d["warnings"][0])
        code, out, err = self.cli("main", path, "--param", "zz=1")
        self.assertEqual(code, 1)
        self.assertIn("no param zz", err)
        code, out, err = self.cli("main", path, "--param", "n")
        self.assertEqual(code, 2)
        code, out, err = self.cli("main", path, "--param", "n=abc")
        self.assertEqual(code, 2)

    def test_refused_function_prints_its_diagnostics_and_exits_1(self):
        path = self.write("def main\n  tr.add 99\n\ndef other\n  tr.add 1\n")
        code, out, err = self.cli("main", path)
        self.assertEqual(code, 1)
        self.assertIn("main: error:", out)
        self.assertIn("refused by the checker, not run", out)
        self.assertNotIn("other", out)

    def test_a_function_with_arguments_exits_1(self):
        path = self.write("def brick | n\n  tr.add n\n")
        code, out, err = self.cli("brick", path)
        self.assertEqual(code, 1)
        self.assertIn("takes arguments (n)", err)

    def test_unknown_function_and_bad_options_exit_codes(self):
        path = self.write()
        code, out, err = self.cli("nope", path)
        self.assertEqual((code, "no function named nope" in err), (1, True))
        self.assertEqual(self.cli("main", path, "--card", "/no/such/dir")[0], 2)
        self.assertEqual(self.cli("main", path, "--bank", "/no/such/file")[0], 2)
        self.assertEqual(self.cli("main", path, "--track", "9")[0], 2)
        self.assertEqual(self.cli("main", path, "--pattern", "17")[0], 2)
        self.assertEqual(self.cli("main", path, "--pattern", "x")[0], 2)

    def test_missing_files_are_a_usage_error_not_a_traceback(self):
        code, out, err = self.cli("main", "/no/such/file.OTY")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("is not a file", err)

    def test_function_names_are_case_insensitive(self):
        self.assertEqual(self.cli("MAIN", self.write(), "--seed", "1")[0], 0)

    def test_bank_run_shows_pattern_name_and_never_writes(self):
        d = self.tmp()
        bank = synthetic_bank(os.path.join(d, "bank03.work"), pattern=5, length=32)
        h = digest(bank)
        code, out, err = self.cli("main", self.write(), "--bank", bank, "--pattern", "5", "--seed", "1")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("pattern C05 len 32 (pattern, bank)", out)
        self.assertEqual(digest(bank), h)
        code, out, err = self.cli("main", self.write(), "--bank", bank, "--pattern", "17")
        self.assertEqual(code, 2)
        self.assertIn("--pattern must be 1..16", err)
        junk = os.path.join(d, "junk.work")                           # a file that is no bank
        with open(junk, "wb") as fh:
            fh.write(b"x" * 1000)
        code, out, err = self.cli("main", self.write(), "--bank", junk)
        self.assertEqual(code, 1)
        self.assertIn("is not an Octatrack bank file", err)

    def test_card_option(self):
        card = self.card(["A/k1.wav", "A/k2.wav"])
        path = self.write('def main\n  dir "A"\n  l 1 128:\n    if gt i file.len: break\n    a static.firstfree\n    if ez a: break\n    static.load a file.at i\n')
        code, out, err = self.cli("main", path, "--card", card)
        self.assertEqual(code, 0)
        self.assertIn("slots: 1 <- A/k1.wav", out)
        self.assertIn("2 <- A/k2.wav", out)

    def test_real_subprocess(self):
        path = self.write()
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "otype.py"), "run",
                               "main", path, "--seed", "1"], capture_output=True, text=True,
                              cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("seed 1", proc.stdout)
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "otype.py"), "run",
                               "nope", path], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 1)
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "otype.py"), "run"],
                              capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 2)

    def test_check_still_works_next_to_run(self):
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "otype.py"), "check",
                               self.write()], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


# ================================================================================================
class TestRender(Base):
    def text(self, body, verbose=False, **kw):
        return sim.render_text(self.go(body, **kw), verbose)

    def test_a_64_step_pattern_is_four_rows_of_sixteen(self):
        t = self.text("trk 1:\n  l 1 64: if eq mod i 4 1: tr.add i", length=64)
        lines = t.split("\n")
        self.assertIn("len 64 (--len)", lines[0])
        i = lines.index("T1  1   5   9   13")
        rows = [lines[i + 1], lines[i + 3], lines[i + 5], lines[i + 7]]
        self.assertEqual(rows, ["    " + "X..." * 4] * 4)
        self.assertEqual(lines[i + 2].strip(), "17  21  25  29")
        self.assertEqual(lines[i + 4].strip(), "33  37  41  45")
        self.assertEqual(lines[i + 6].strip(), "49  53  57  61")

    def test_a_16_step_pattern_is_one_row_and_only_busy_tracks_show(self):
        t = self.text("tr.add 1")
        self.assertEqual(t.count("\nT"), 1)
        self.assertIn("T1  1   5   9   13\n    X...............\n", t)
        self.assertIn("cost:", t)

    def test_steps_beyond_the_pattern_length_are_shown_and_flagged(self):
        t = self.text("tr.add 20", length=16)
        self.assertIn("    17\n    ...X\n", t)
        self.assertIn("(steps after 16 are beyond the pattern length)", t)

    def test_nothing_to_show(self):
        self.assertIn("(no track holds anything)", self.text("x 1"))

    def test_default_lists_details_of_playing_steps_only_and_v_lists_all(self):
        few = "tr.add 2\ntl.add 5\ntl.add 6\npl.set 2 amp.vol 9\npl.set 5 amp.vol 7\nmicro.set 6 3"
        plain = self.text(few)
        self.assertIn("step 5: amp.vol 7", plain)               # few details: all are listed
        self.assertNotIn("more steps", plain)
        many = "tr.add 2\nl 3 20: tl.add i\npl.set 2 amp.vol 9\nl 3 20: pl.set i amp.vol 7"       # 19 steps with details
        plain = self.text(many)
        self.assertIn("step 2: amp.vol 9", plain)
        self.assertNotIn("step 5:", plain)
        self.assertIn("(+18 more steps", plain)
        verb = self.text(many, verbose=True)
        self.assertIn("step 5: amp.vol 7", verb)
        self.assertNotIn("more steps", verb)

    def test_more_than_three_locks_are_folded_unless_verbose(self):
        body = "tr.add 1\nl 0 5: pl.set 1 add i 1 5"
        self.assertIn("+3 locks", self.text(body))
        self.assertNotIn("+3 locks", self.text(body, verbose=True))

    def test_verbose_shows_all_eight_tracks(self):
        t = self.text("tr.add 1", verbose=True)
        self.assertEqual([("T%d" % k) in t for k in range(1, 9)], [True] * 8)

    def test_not_on_the_unit_yet_marks(self):
        t = self.text("tempo 140\ntrk.slot 3\n@scenes\npar.set amp.vol 9")
        self.assertIn("part:  tempo 140", t)
        self.assertIn("slot 3", t)
        self.assertNotIn("(not on the unit yet", t)
        self.assertIn("scenes: 270 locks, 1 unlocked", t)
        self.assertNotIn("locks, 1 unlocked (not on the unit yet)", t)      # only what is not there
        self.assertNotIn("not on the unit", self.text("tr.add 1\npar.set amp.vol 9"))

    def test_legend_and_glyphs(self):
        t = self.text("tr.add 1\ntl.add 2\ntl.add 3\npl.set 3 amp.vol 1\nsmp.set 4 1")
        self.assertIn("    X--.............\n", t)       # step 4 has no trig: the lock is skipped
        self.assertIn("X trig  ! one-shot  o trigless trig  - trigless lock  l locks only  . none", t)

    def test_locks_only_glyph_comes_from_a_bank_file_not_from_a_script(self):
        path = synthetic_bank(os.path.join(self.tmp(), "bank01.work"), pattern=1)
        with open(path, "r+b") as fh:                     # a lock and a sample lock, no trig, steps 10 11
            for s, off in ((10, 15), (11, 31)):
                fh.seek(sim.trac_off(0, 0) + 0x59 + (s - 1) * 0x20 + off)
                fh.write(bytes([77]))
        t = sim.render_text(sim.simulate(self.prog("x 1"), "main", seed=1, bank=path, pattern=1))
        self.assertIn("    X.o.X....ll.....\n", t)

    def test_part_line_lists_what_changed(self):
        t = self.text("par.set amp.vol 90\nlfo.dest 1 amp.vol\nfx.type 1 4\ntrk 2:\n  trk.init")
        self.assertIn("part:  t1 fx1 #4  amp.vol 90  lfo1->amp.vol\n", t)
        self.assertIn("part:  t2 factory slot  fx1 #1 fx2 #1  pb[1] 0 ", t)
        self.assertIn("  lfo1 wave 0 lfo2 wave 0 lfo3 wave 0  scenes: 0 locks, 16 unlocked\n", t)

    def test_bank_glyphs_for_trigless_and_one_shot(self):
        d = self.tmp()
        path = synthetic_bank(os.path.join(d, "bank01.work"), pattern=1)
        # one-shot mask (offset +0x18) of track 1: step 9
        with open(path, "r+b") as fh:
            fh.seek(sim.trac_off(0, 0) + 0x18 + 6)                  # step 9 = bit 8 = byte 6, bit 0
            fh.write(bytes([0b00000001]))
            fh.seek(sim.trac_off(0, 0) + 0x10 + 7)
            fh.write(bytes([0b10000000]))          # trigless lock: step 8
        d_ = sim.render_text(sim.simulate(self.prog("x 1"), "main", seed=1, bank=path, pattern=1))
        self.assertIn("    X.o.X..-!.......\n", d_)

    def test_slot_lines(self):
        card = self.card(["A/a.wav", "A/b.wav"])
        t = self.text('dir "A"\nslots.add 2\nstatic.load 1 1\nstatic.load 2 2', card=card)
        self.assertIn("slots: 1 <- A/a.wav\n       2 <- A/b.wav", t)
        self.assertIn("slots set: 2", t)

    def test_json_report_has_the_whole_model(self):
        d = sim.result_json(self.go("tr.add 1\ntempo 100"))
        self.assertEqual(d["model"]["tempo"], 100)
        self.assertEqual(d["not_on_unit"], [])
        self.assertEqual(set(d["model"]["tracks"]), {str(t) for t in range(1, 9)})
        json.dumps(d)


class TestV1NewOps(Base):
    @covers("PAT.CUR", "BANK.CUR", "PAT.GO")
    def test_pattern_ops(self):
        r = self.go("x pat.cur\ny bank.cur\npat.go 3\nz pat.cur\npat.go 2 4\nt pat.cur\nz bank.cur")
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 1))
        self.assertEqual((r.globals["z"], r.globals["t"]), (4, 2))
        self.assertEqual((r.model.pattern, r.model.bank), (2, 4))
        r2 = self.go("pat.go 0\npat.go 17\npat.go 1 -1\npat.go 1 17")
        self.assertEqual(len(r2.oor), 4)

    @covers("PAT.COPY")
    def test_pat_copy(self):
        # the whole pattern record travels: trigs, trigless locks, parameter locks
        r = self.go("tr.add 1\ntr.add 5\npl.set 5 amp.vol 9\npat.copy 1 2\npat.go 2\n"
                    "x has.trig 1\ny has.trig 5\nz pl.get 5 amp.vol")
        self.assertEqual((r.globals["x"], r.globals["y"], r.globals["z"]), (1, 1, 9))
        # the source keeps its content, and the part assigned to the pattern travels
        self.assertEqual(r.model.pattern_lengths.get((1, 1)), r.model.pattern_lengths.get((1, 2)))
        r = self.go("part.go 2\npat.copy 1 2\npat.go 2\nx part.cur")
        self.assertEqual(r.globals["x"], 2)
        # pasted onto the playing pattern: what the run reads next is the copy
        r = self.go("tr.add 1\npat.go 2\ntr.add 3\npat.go 1\npat.copy 2 1\nx has.trig 3\ny has.trig 1")
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 0))

    @covers("PAT.COPY")
    def test_pat_copy_refusals(self):
        with self.assertRaises(sim.RunError):
            self.go("pat.copy 2 2")                     # same pattern: refused, the run stops
        r = self.go("pat.copy 0 2\npat.copy 1 17\npat.copy 17 1")
        self.assertEqual(len(r.oor), 3)                 # outside 1..16: out of range, counted

    @covers("PART.CUR", "PART.GO", "PART.SAVE", "PART.RELOAD")
    def test_part_ops(self):
        r = self.go("x part.cur\npart.go 2\ny part.cur")
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 2))
        self.assertEqual(r.model.part_cur, 2)
        r = self.go("par.set amp.vol 80\npart.save 1\npar.set amp.vol 100\nx par.get amp.vol\npart.reload 1\ny par.get amp.vol")
        self.assertEqual((r.globals["x"], r.globals["y"]), (100, 80))
        with self.assertRaises(sim.RunError):
            self.go("part.reload 3")

    @covers("TL.DEL")
    def test_tl_del(self):
        r = self.go("tl.add 1\ntl.del 1")
        self.assertFalse(r.model.tracks[1].has("tlock", 1))

    @covers("TT.ADD", "TT.DEL")
    def test_tt_ops(self):
        r = self.go("tt.add 3\nx has.trig 3\ntt.del 3\ny has.trig 3")
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 0))

    @covers("OS.ADD", "OS.DEL", "OS.ARM")
    def test_os_ops(self):
        r = self.go("os.add 5\nx has.trig 5\nos.arm\nos.del 5\ny has.trig 5")
        self.assertEqual((r.globals["x"], r.globals["y"]), (1, 0))
        self.assertEqual(r.model.tracks[1].os_armed, 1)

    @covers("SW.ADD", "SW.DEL", "SW.FLIP")
    def test_sw_ops(self):
        r = self.go("sw.add 2\nx has.trig 2\nsw.flip 2\nsw.flip 4\nsw.del 4")
        td = r.model.tracks[1]
        self.assertFalse(td.has("swing", 2))
        self.assertFalse(td.has("swing", 4))

    @covers("SL.ADD", "SL.DEL", "SL.FLIP")
    def test_sl_ops(self):
        r = self.go("sl.add 3\nsl.flip 3\nsl.flip 5\nsl.del 5")
        td = r.model.tracks[1]
        self.assertFalse(td.has("slide", 3))
        self.assertFalse(td.has("slide", 5))

    @covers("TRK.SWING")
    def test_trk_swing(self):
        r = self.go("x trk.swing\ntrk.swing 60\ny trk.swing")
        self.assertEqual((r.globals["x"], r.globals["y"]), (50, 60))

    @covers("COND.GET", "COND.DEL")
    def test_cond_ops(self):
        r = self.go("tr.add 1\ncond.set 1 50%\nx cond.get 1\ncond.del 1\ny cond.get 1")
        self.assertEqual(r.globals["x"], 19)
        self.assertEqual(r.globals["y"], 0)

    @covers("MICRO.GET")
    def test_micro_get(self):
        r = self.go("tr.add 1\nmicro.set 1 12\nx micro.get 1")
        self.assertEqual(r.globals["x"], 12)

    @covers("RATCH.GET")
    def test_ratch_get(self):
        r = self.go("tr.add 1\nratch.set 1 4\nx ratch.get 1")
        self.assertEqual(r.globals["x"], 4)

    @covers("FILL", "FILL.ONCE")
    def test_fill_ops(self):
        r = self.go("x fill\nfill 1\ny fill\nfill.once")
        self.assertEqual((r.globals["x"], r.globals["y"]), (0, 1))
        self.assertEqual(r.model.fill_once, 1)

    @covers("TRK.LEVEL", "TRK.CUE", "TRK.MUTE")
    def test_trk_level_cue_mute(self):
        r = self.go("trk.level 108\ntrk.cue 1\ntrk.mute 1\nx trk.level\ny trk.cue\nz trk.mute")
        self.assertEqual((r.globals["x"], r.globals["y"], r.globals["z"]), (108, 1, 1))

    @covers("PAR.GET")
    def test_par_get(self):
        r = self.go("par.set amp.vol 88\nx par.get amp.vol")
        self.assertEqual(r.globals["x"], 88)

    @covers("PAR.MIN", "PAR.MAX")
    def test_par_min_max(self):
        pm = sim.ParamModel({(1, 15): (10, 50, True), (1, 12): (0, 128, False), (2, 15): (0, 128, True)})
        r = self.go("x par.min amp.vol\ny par.max amp.vol\nz par.min amp.atk\nt par.max amp.atk", pmodel=pm)
        self.assertEqual([r.globals[k] for k in "xyzt"], [10, 59, 0, 0])                 # the disabled slot: par.get's refusal
        self.assertEqual([o[1:] for o in r.oor], [("par.min", 5), ("par.max", 6)])
        r = self.go("trk 2\nx par.min amp.vol\ny par.max amp.vol", pmodel=pm)            # the context track's descriptor
        self.assertEqual([r.globals[k] for k in "xy"], [0, 127])
        r = self.go("l 0 29:\n  x add x par.min i\n  y add y par.max i")                 # the neutral part: 0..127 everywhere
        self.assertEqual((r.globals["x"], r.globals["y"], r.oor), (0, 127 * 30, []))
        # pure getters: nothing of the part, the pattern or the slots changes
        base, after = self.go("x 1", pmodel=pm), self.go("x par.min amp.vol\ny par.max amp.vol\nz fx.count 1", pmodel=pm)
        self.assertEqual({t: vars(pt) for t, pt in after.model.part.items()}, {t: vars(pt) for t, pt in base.model.part.items()})
        self.assertEqual((after.model.slots, after.model.tracks[1].__dict__ == base.model.tracks[1].__dict__), (base.model.slots, True))

    @covers("FX.COUNT")
    def test_fx_count(self):
        pm = sim.ParamModel(fx_count={1: 11, 2: 300})
        r = self.go("x fx.count 1\ny fx.count 2\na 0\nz fx.count a\na 3\nt fx.count a", pmodel=pm)
        self.assertEqual([r.globals[k] for k in "xyzt"], [11, 255, 0, 0])                # 300 -> 255; 0 and 3 are not choosers
        self.assertEqual([o[1:] for o in r.oor], [("fx.count", 6), ("fx.count", 8)])
        r = self.go("x fx.count 1\ny fx.count 2")                                         # the neutral part
        self.assertEqual([r.globals[k] for k in "xy"], [sim.FX_COUNT_NEUTRAL[1], sim.FX_COUNT_NEUTRAL[2]])

    @covers("PB.SETUP", "AMP.SETUP")
    def test_page_setup_ops(self):
        r = self.go("pb.setup 1 42\nx pb.setup 1\namp.setup 2 15\ny amp.setup 2")
        self.assertEqual((r.globals["x"], r.globals["y"]), (42, 15))

    @covers("LFO.WAVE", "LFO.MULT", "LFO.TRIG")
    def test_lfo_setup_ops(self):
        r = self.go("lfo.wave 1 3\nx lfo.wave 1\nlfo.mult 2 5\ny lfo.mult 2\nlfo.trig 3 2\nz lfo.trig 3")
        self.assertEqual((r.globals["x"], r.globals["y"], r.globals["z"]), (3, 5, 2))

    @covers("LFOD.SET", "LFOD.GET", "LFOD.INTERP")
    def test_lfod_ops(self):
        r = self.go("lfod.set 1 30\nx lfod.get 1\nlfod.interp 1 1")
        self.assertEqual(r.globals["x"], 30)
        self.assertEqual(r.model.part[1].lfod_interp[0], 1)

    @covers("FX1.SETUP", "FX2.SETUP")
    def test_fx_setup_ops(self):
        r = self.go("fx1.setup 1 10\nx fx1.setup 1\nfx2.setup 2 20\ny fx2.setup 2")
        self.assertEqual((r.globals["x"], r.globals["y"]), (10, 20))

    @covers("SCN.SET", "SCN.GET", "SCN.DEL", "SCN.COPY")
    def test_scn_ops(self):
        r = self.go("scn.set 1 amp.vol 99\nx scn.get 1 amp.vol\nscn.copy 1 2\ny scn.get 2 amp.vol\nscn.del 1 amp.vol\nz scn.get 1 amp.vol")
        self.assertEqual((r.globals["x"], r.globals["y"]), (99, 99))
        self.assertEqual(r.globals["z"], 64)

    def test_lfod_invert_loop(self):
        r = self.go("lfod.set 1 20\n@lfodinv\nx lfod.get 1")
        self.assertEqual(r.globals["x"], -20)

    def test_trk_all_context(self):
        r = self.go("trk all:\n  trk.level 77\n  trk.swing 66")
        for t in range(1, 9):
            self.assertEqual(r.model.part[t].level, 77)
            self.assertEqual(r.model.tracks[t].swing, 66)

    def test_loop_lfo_spd(self):
        r = self.go("l 1 3:\n  x lfo.spd i\n  y lfo.dep i")
        self.assertEqual(r.globals["x"], 8)
        self.assertEqual(r.globals["y"], 11)

    def test_scn_clr_alone_vs_arg(self):
        r = self.go("scn.set 1 amp.vol 99\nscn.set 2 amp.vol 88\nscn.clr 1\nx scn.get 1 amp.vol\ny scn.get 2 amp.vol\nscn.clr\nz scn.get 2 amp.vol")
        self.assertEqual(r.globals["x"], 64)
        self.assertEqual(r.globals["y"], 88)
        self.assertEqual(r.globals["z"], 64)

    def test_pickup_track_refuses_writes(self):
        prog = self.prog("trk 2:\n  tr.add 1\n  tt.add 2\n  os.add 3\n  sw.add 4\n  sl.add 5")
        r = pickup_sim(prog, (2,), seed=1)
        self.assertEqual(len(r.skipped_pickup), 5)

    @covers("PLAY")
    def test_play_op(self):
        r = self.go("play 1\nplay 3")
        self.assertEqual(r.plays, [(0, 1), (0, 3)])

    @covers("SEQ.RUN")
    def test_seq_run_op(self):
        r = self.go("x seq.run")
        self.assertEqual(r.globals["x"], 0)

    @covers("SEQ.STEP")
    def test_seq_step_op(self):
        r = self.go("x seq.step 1\ny seq.step 2")
        self.assertEqual(r.globals["x"], 1)
        self.assertEqual(r.globals["y"], 1)

    @covers("SEQ.MUTE")
    def test_seq_mute_op(self):
        r = self.go("x seq.mute 1\ny seq.mute 2")
        self.assertEqual(r.globals["x"], 0)
        self.assertEqual(r.globals["y"], 0)

    def test_simulate_module_ticks(self):
        code = textwrap.dedent("""
        def seq_mod
          in clock 24:
            play 1
            x add x 1
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        res, plays = sim.simulate_module(prog, "seq_mod", total_ticks=72)
        self.assertEqual(plays, [(0, 1), (24, 1), (48, 1)])
        self.assertEqual(res.globals["x"], 3)

    @covers("GATE")
    def test_gate_op_emits_at_the_block_tick(self):
        code = textwrap.dedent("""
        def acid
          in clock 24:
            gate 1
            prob 100: gate 3
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        self.assertEqual([d for d in prog.diags if d.level == "error"], [])
        res, _plays = sim.simulate_module(prog, "acid", total_ticks=24)
        self.assertEqual(res.gates, [(0, 1), (0, 3)])

    @covers("OUT")
    def test_out_op_publishes_and_holds(self):
        code = textwrap.dedent("""
        def acid
          in clock 24:
            out 1 42
            out 4 7
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        self.assertEqual([d for d in prog.diags if d.level == "error"], [])
        res, _plays = sim.simulate_module(prog, "acid", total_ticks=24)
        self.assertEqual(res.outs, [42, 0, 0, 7])

    @covers("CV")
    def test_cv_op_reads_the_running_script_by_name(self):
        code = textwrap.dedent("""
        def follow
          var v 5
          in gate acid 1:
            x cv acid 1
            y cv acid 2
            z cv nothere 1
            t cv acid v
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        self.assertEqual([d for d in prog.diags if d.level == "error"], [])

        def _make_gate_run(*args, **kwargs):
            r = sim.Run(*args, **kwargs)
            r.current_event = {"type": "GATE", "gate.name": "ACID", "gate.out": 1}
            r.module_outs = {"ACID": [42, 0, 0, 0]}
            return r
        r = sim.simulate(prog, "follow", run_class=_make_gate_run)
        self.assertEqual(r.globals["x"], 42)
        self.assertEqual(r.globals["y"], 0)
        self.assertEqual(r.globals["z"], 0)
        self.assertEqual(r.globals["t"], 0)          # out of range
        # a gate on another output does not wake the block
        def _make_gate2_run(*args, **kwargs):
            r = sim.Run(*args, **kwargs)
            r.current_event = {"type": "GATE", "gate.name": "ACID", "gate.out": 2}
            r.module_outs = {"ACID": [42, 0, 0, 0]}
            return r
        r2 = sim.simulate(prog, "follow", run_class=_make_gate2_run)
        self.assertEqual(r2.globals["x"], 0)

    @covers("NX.SMP", "NX.PL")
    def test_nx_smp_and_nx_pl(self):
        code = textwrap.dedent("""
        def test_mod
          in clock 24:
            nx.smp 3
            nx.pl fx1 1 45
            play 1
            play 2
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        self.assertEqual([d for d in prog.diags if d.level == "error"], [])
        res, plays = sim.simulate_module(prog, "test_mod", total_ticks=24)
        self.assertEqual(plays, [(0, 1, 3, {18: 45}), (0, 2)])

    @covers("NOTE.NUM", "NOTE.VEL", "NOTE.CH", "CC.NUM", "CC.VAL", "CC.CH")
    def test_note_and_cc_events(self):
        code = textwrap.dedent("""
        def midi_mod
          var note_n 0
          var note_v 0
          var note_c 0
          in note:
            note_n note.num
            note_v note.vel
            note_c note.ch
          in cc 1 7:
            x cc.num
            y cc.val
            z cc.ch
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        self.assertEqual([d for d in prog.diags if d.level == "error"], [])
        def _make_note_run(*args, **kwargs):
            r = sim.Run(*args, **kwargs)
            r.current_event = {"type": "NOTE", "note.num": 60, "note.vel": 100, "note.ch": 2, "ch": 2}
            return r
        r1 = sim.simulate(prog, "midi_mod", run_class=_make_note_run)
        self.assertEqual(r1.fn_vars["MIDI_MOD"]["NOTE_N"], 60)
        self.assertEqual(r1.fn_vars["MIDI_MOD"]["NOTE_V"], 100)
        self.assertEqual(r1.fn_vars["MIDI_MOD"]["NOTE_C"], 2)
        self.assertEqual(r1.globals["x"], 0)

        def _make_cc_run(*args, **kwargs):
            r = sim.Run(*args, **kwargs)
            r.current_event = {"type": "CC", "cc.num": 7, "cc.val": 127, "cc.ch": 1, "ch": 1, "num": 7}
            return r
        r2 = sim.simulate(prog, "midi_mod", run_class=_make_cc_run)
        self.assertEqual(r2.globals["x"], 7)
        self.assertEqual(r2.globals["y"], 127)
        self.assertEqual(r2.globals["z"], 1)
        self.assertNotIn("NOTE_N", r2.fn_vars.get("MIDI_MOD", {}))

    @covers("TRIG.STEP", "TRIG.HIT", "TRIG.VAL")
    def test_trig_events(self):
        """`in trig t:` and `in trig t all:`.  The script genre is refused in 1.1, so no source reaches
        `trig.val` any more: its handler stays for the bytecode's genre 2 (1.2 brings the poster back)."""
        code = textwrap.dedent("""
        def trig_mod
          var st 0
          var hi 0
          in trig 3:
            st trig.step
            hi trig.hit
          in trig 4 all:
            x trig.hit
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        self.assertEqual([d for d in prog.diags if d.level == "error"], [])

        def run(ev):
            def _make(*args, **kwargs):
                r = sim.Run(*args, **kwargs)
                r.current_event = ev
                return r
            return sim.simulate(prog, "trig_mod", run_class=_make)

        r1 = run({"type": "TRIG", "track": 3, "trig.step": 11, "trig.hit": 1, "trig.val": 0,
                  "trig.genre": 0})
        self.assertEqual(r1.fn_vars["TRIG_MOD"]["ST"], 11)
        self.assertEqual(r1.fn_vars["TRIG_MOD"]["HI"], 1)
        r2 = run({"type": "TRIG", "track": 3, "trig.step": 11, "trig.hit": 0, "trig.val": 0,
                  "trig.genre": 0})
        self.assertNotIn("ST", r2.fn_vars.get("TRIG_MOD", {}))      # `in trig 3:`: not played, no wake
        r3 = run({"type": "TRIG", "track": 4, "trig.step": 12, "trig.hit": 0, "trig.val": 0,
                  "trig.genre": 0})
        self.assertEqual(r3.globals["x"], 0)                        # `all`: wakes, trig.hit = 0
        self.assertNotIn("ST", r3.fn_vars.get("TRIG_MOD", {}))
        r6 = run({"type": "TRIG", "track": 3, "trig.step": 1, "trig.hit": 1, "trig.val": 7,
                  "trig.genre": 0})
        self.assertEqual(r6.fn_vars["TRIG_MOD"]["ST"], 1)            # a trig on track 3 does not wake track 4's `all`
        self.assertEqual(r6.globals["x"], 0)
        bad = otype.check_sources([("S.OTY", "def s\n  in trig 5 script:\n    z trig.val\n")])
        self.assertIn("in trig t script: is not available in 1.1: use in trig t: or in trig t all:",
                      [d.msg for d in bad.diags])

    @covers("SEND.NOTE", "SEND.CC", "SEND.PROG")
    def test_send_midi_ops(self):
        code = textwrap.dedent("""
        def test_send
          send.note 1 60 100 24
          send.cc 2 74 120
          send.prog 3 10
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        self.assertEqual([d for d in prog.diags if d.level == "error"], [])
        res = sim.simulate(prog, "test_send")
        self.assertEqual(res.midi_out, [
            (0, "note", 1, 60, 100, 24),
            (0, "cc", 2, 74, 120),
            (0, "prog", 3, 10),
        ])

        # Test in a live module
        code_mod = textwrap.dedent("""
        def mod_send
          in clock 24:
            send.note 1 60 100 24
        """).strip()
        prog_mod = otype.check_sources([("T.OTY", code_mod)])
        res_m, plays = sim.simulate_module(prog_mod, "mod_send", total_ticks=48)
        self.assertEqual(res_m.midi_out, [(24, "note", 1, 60, 100, 24)])

        # Test dynamic runtime out-of-range checking via expressions
        code_bad1 = "send.note (sub 1 1) 60 100 24"
        res_bad1 = self.go("a 0\nsend.note a 60 100 24")
        self.assertEqual(res_bad1.midi_out, [])
        self.assertEqual(len(res_bad1.oor), 1)

        res_bad2 = self.go("a 128\nsend.note 1 a 100 24")
        self.assertEqual(res_bad2.midi_out, [])
        self.assertEqual(len(res_bad2.oor), 1)

        res_bad3 = self.go("a 0\nsend.note 1 60 100 a")
        self.assertEqual(res_bad3.midi_out, [])
        self.assertEqual(len(res_bad3.oor), 1)

    def test_nx_outside_module_refused(self):
        code = textwrap.dedent("""
        def menu_fn
          menu "M" >> "ACT"
          nx.smp 1
        """).strip()
        prog = otype.check_sources([("T.OTY", code)])
        self.assertTrue(any("only allowed in a live script" in str(d) for d in prog.diags))

    @covers("RT.ADD", "RT.DEL", "RT.CLR", "RT.SRC", "REC.SRC", "REC.LEN")
    def test_recorder_trig_and_setup_ops(self):
        # 1. rt.add with default src=7 and explicit src
        res = self.go("rt.add 1\nrt.add 5 2\nrt.src 1 1")
        m = res.model.tracks[1].masks
        # step 1: was 7, then rt.src 1 1 -> AB only (bit 0 set, bit 1 and 2 clear)
        self.assertEqual(m["rec1"] & 1, 1)
        self.assertEqual(m["rec2"] & 1, 0)
        self.assertEqual(m["rec3"] & 1, 0)
        # step 5: was 2 (CD only -> bit 1 set, bit 0 and 2 clear)
        self.assertEqual(m["rec1"] & (1 << 4), 0)
        self.assertEqual(m["rec2"] & (1 << 4), 1 << 4)
        self.assertEqual(m["rec3"] & (1 << 4), 0)

        # 2. rt.del: the four recorder masks of the step go, the PLAYBACK trig word of the step stays
        #    (the recorder micro-timing byte is a pattern byte no operation of OType models)
        res_del = self.go("tr.add 1\nmicro.set 1 5\ntt.add 2\nrt.add 1\nrt.del 1")
        td = res_del.model.tracks[1]
        self.assertEqual([td.has(k, 1) for k in ("rec1", "rec2", "rec3", "m38")], [False] * 4)
        self.assertTrue(td.has("trig", 1))
        self.assertEqual(td.micro.get(1), 5)      # a playback micro-timing is not a recorder setting
        self.assertTrue(td.has("tless", 2))       # nor is a trigless trig a recorder trig

        # 3. rt.clr: every step of the context, the playback trigs untouched
        res_clr = self.go("tr.add 3\nrt.add 1\nrt.add 2\nrt.clr")
        m_clr = res_clr.model.tracks[1].masks
        self.assertEqual((m_clr["rec1"], m_clr["rec2"], m_clr["rec3"], m_clr["m38"]), (0, 0, 0, 0))
        self.assertTrue(res_clr.model.tracks[1].has("trig", 3))

        # 4. rec.src and rec.len
        res_rec = self.go("rec.src 1 1 2\nrec.src 1 2 3\nrec.src 1 3 1\nrec.len 1 4")
        p = res_rec.model.part[1].rec_setup
        self.assertEqual(p[0], 2)  # INAB = 2 (A)
        self.assertEqual(p[1], 3)  # INCD = 3 (D)
        self.assertEqual(p[4], 1)  # SRC3 = 1 (MAIN)
        self.assertEqual(p[2], 3)  # RLEN 4 pas : la part le stocke 0-based

        # 5. Out of range checking: track, source number, stock value, length, and a source MASK
        #    outside 1..7 (refused, not truncated: 8 is not "SRC3 only")
        bad = self.go("a 9\nrec.src a 1 1")
        self.assertEqual(len(bad.oor), 1)
        bad_src = self.go("a 4\nrec.src 1 a 1")
        self.assertEqual(len(bad_src.oor), 1)
        bad_val = self.go("a 5\nrec.src 1 1 a")
        self.assertEqual(len(bad_val.oor), 1)
        bad_len = self.go("a 65\nb 0\nrec.len 1 a\nrec.len 1 b")
        self.assertEqual(len(bad_len.oor), 2)
        self.assertEqual(self.go("rec.len 1 64").model.part[1].rec_setup[2], 64)   # 64 = MAX, stock byte 64
        bad_mask = self.go("a 0\nrt.add 1 a\nb 8\nrt.src 2 b")
        self.assertEqual(len(bad_mask.oor), 2)
        self.assertEqual(bad_mask.model.tracks[1].masks["rec1"] | bad_mask.model.tracks[1].masks["rec2"]
                         | bad_mask.model.tracks[1].masks["rec3"], 0)

    @covers("RO.ADD", "RO.DEL")
    def test_one_shot_recorder_trigs(self):
        """ro.add is rt.add plus the one-shot state (TRAC +0x38): that bit is the ONLY difference between the two
        families, and ro.del removes a recorder trig of either kind."""
        res = self.go("ro.add 1\nro.add 3 2")
        td = res.model.tracks[1]
        self.assertEqual([td.has(k, 1) for k in ("rec1", "rec2", "rec3", "m38")], [True] * 4)
        self.assertEqual([td.has(k, 3) for k in ("rec1", "rec2", "rec3", "m38")], [False, True, False, True])
        self.assertFalse(self.go("rt.add 1").model.tracks[1].has("m38", 1))    # a plain recorder trig is not one-shot
        res_del = self.go("ro.add 1\nrt.add 2\nro.del 1\nro.del 2")
        self.assertEqual([[res_del.model.tracks[1].has(k, s) for k in ("rec1", "rec2", "rec3", "m38")]
                          for s in (1, 2)], [[False] * 4, [False] * 4])
        bad = self.go("a 0\nb 8\nro.add 1 a\nro.add 2 b")
        self.assertEqual(len(bad.oor), 2)
        self.assertFalse(any(bad.model.tracks[1].has(k, s) for s in (1, 2)
                             for k in ("rec1", "rec2", "rec3", "m38")))

    @covers("REC.TRIG", "REC.LOOP")
    def test_recorder_trig_mode_and_loop(self):
        res = self.go("rec.trig 1 1\nrec.loop 1 1\nrec.trig 2 2")
        p = res.model.part[1].rec_setup
        self.assertEqual((p[3], p[5]), (1, 1))         # the part's own bytes: TRIG and LOOP, next to the sources
        self.assertEqual(res.model.part[2].rec_setup[3], 2)
        self.assertEqual(self.go("rec.trig 3 0\nrec.loop 3 0").model.part[3].rec_setup,
                         {3: 0, 5: 0})
        bad = self.go("a 3\nb 2\nrec.trig 1 a\nrec.loop 1 b")
        self.assertEqual(len(bad.oor), 2)

    @covers("REC.ARM")
    def test_recorder_arm(self):
        res = self.go("rec.arm 2\nrec.arm 1 3\ntrk 3\nrec.arm 3")
        self.assertEqual(res.model.rec_arms, [(2, 7), (1, 3), (3, 7)])
        bad = self.go("a 0\nb 9\nrec.arm 1 a\nrec.arm b")
        self.assertEqual(len(bad.oor), 2)
        self.assertEqual(bad.model.rec_arms, [])

    @covers("MIDI.CC", "MIDI.NOTE", "MIDI.VEL")
    def test_the_midi_in_tables(self):
        """The three reads answer what the MIDI IN hook stored (the unit's tables), 0 before anything came in."""
        m = sim.Model()
        m.midi_cc[(1, 74)] = 64
        m.midi_note[2] = 60
        m.midi_vel[2] = 100
        r = self.go("x midi.cc 1 74\ny midi.note 2\nz midi.vel 2\nt midi.cc 2 74", model=m)
        self.assertEqual([r.globals[k] for k in "xyzt"], [64, 60, 100, 0])
        bad = self.go("a 0\nb 17\nc 128\nx midi.cc a 0\ny midi.cc b 0\nz midi.cc 1 c", model=m)
        self.assertEqual(len(bad.oor), 3)

    @covers("SEND.NRPN")
    def test_send_nrpn_is_four_control_changes(self):
        r = self.go("send.nrpn 1 300 16383")
        self.assertEqual(r.midi_out, [(0, "cc", 1, 99, 2), (0, "cc", 1, 98, 44),
                                      (0, "cc", 1, 6, 127), (0, "cc", 1, 38, 127)])   # 300 = 2<<7 | 44
        bad = self.go("a 16384\nb 0\nsend.nrpn 1 a 0\nsend.nrpn b 1 1")
        self.assertEqual(bad.midi_out, [])
        self.assertEqual(len(bad.oor), 2)



# ================================================================================================
class TestCoverage(Base):
    def test_every_registry_op_has_a_handler(self):
        have = sim.declared_names()
        missing = sorted(o.name for o in O.ALL_OPS if o.name not in have)
        self.assertEqual(missing, [], "operations with no simulation")

    def test_every_registry_op_has_an_effect_test(self):
        want = {o.name for o in O.ALL_OPS}
        missing = sorted(want - COVERED)
        self.assertEqual(missing, [], "operations with no effect test in this file")

    def test_the_coverage_list_only_names_real_ops(self):
        real = {o.name for o in O.ALL_OPS}
        self.assertEqual(sorted(COVERED - real), [])


class CookbookRecipes(unittest.TestCase):
    """Every ```otype block of the cookbook passes the checker with the unit's profile, with no warning,
    and each of its functions that takes no argument runs in the simulator (seed 1, a synthetic pattern)."""
    PATH = os.path.join(ROOT, "docs", "cookbook.md")

    def test_recipes(self):
        import re
        with open(self.PATH) as f:
            blocks = re.findall(r"```otype\n(.*?)```", f.read(), re.S)
        self.assertGreaterEqual(len(blocks), 15)
        for k, text in enumerate(blocks, 1):
            with self.subTest(recipe=k):
                prog = otype.check_sources([("R%02d.OTY" % k, text)])
                otb.apply_compile(prog, O.PROFILES["m1"])
                self.assertEqual([str(d) for d in prog.diags], [])
                bank = synthetic_bank(os.path.join(self.tmp, "bank01.work"), pattern=1, length=16)
                for fn in prog.funcs:
                    if not fn.args:
                        sim.simulate(prog, fn.name, seed=1, bank=bank, pattern=1)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()


if __name__ == "__main__":
    unittest.main()
