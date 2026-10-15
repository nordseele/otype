"""Tests of the OType compiled table (modules/otype/otb.py), the reference VM
(modules/otype/vm.py), `otype.py dump` and `otype.py run --vm`.  From the repo root:

    python3 -m unittest tools/test_otype_otb.py

The main deliverable is the EQUIVALENCE test (`TestEquivalence`): for every function without
argument of the examples (modules/otype/examples/*.OTY), of a kitchen-sink script written here, and
of every script the two other OType test suites feed to the checker (collected while they run), for
seeds 0..20 and 3 presses chained through `--state`, the tree-walking simulator and the VM running
the table's BYTES give the identical JSON result (model, draws, cost, out-of-range list, state).
"""
import atexit
import glob
import hashlib
import io
import json
import os
import re
import shutil
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
from modules.otype import otb, sim, vm  # noqa: E402

EXAMPLES = sorted(glob.glob(os.path.join(ROOT, "modules", "otype", "examples", "*.OTY")))
SEEDS = range(21)
PRESSES = 3

# ---- the kitchen sink: every instruction, every construct --------------------------------------
KITCHEN = '''otype 1
const kdir "AUDIO/DRUMS/KICKS"
const klist {3 1 4 1 5}
const kcond 33%
const kbig 3000
const kneg -200

def ks_inc | n
  ret add n 1

def ks_files | folder lst
  dir folder
  ext wav aif
  match "k*" "s*"
  msg "N" static.firstfree "K" -7
  slots.clr
  l 1 3:
    a static.firstfree
    static.load a file.pick
    slots.add a
  ret add file.len at lst 2

def ks_stop | n
  if gt n 1: stop
  tr.add 7
  ret 9

def ks_early | n
  l 1 5:
    if eq i n: ret add i 100
  ret 0

def ks_hasp
  menu "KS" >> "HASP"
  param n 3 1 4
  l 1 n: tr.add i

def ks_main
  menu "KS" >> "MAIN"
  param p 5 1 9
  param q 100 0 200
  a ks_inc | p
  b ks_files | kdir klist
  c kbig
  d neg kneg
  x add a b
  y ? gt a 3 11 22
  z ? eq b 0 ? lt a 2 1 2 3
  ks_inc | 3
  ks_stop | 2
  c ks_early | 3
  ks_hasp
  cond.set 4 kcond
  cond.set 5 67%
  trk 1 2:
    steps 1 8:
      tr.add 3
    steps has.trig:
      pl.set 3 amp.vol q
  trk 3 4
  steps all
  step 5
  seed 9
  prob.seed 4
  each:
    prob p: pl.set i amp.vol i
    every 2: tr.add i
    skip 3: micro.set i 1
  l 1 8:
    if eq i 5: break
    elif eq i 6: tr.add i
    else: tr.flip i
    l 1 2: tr.add add i j
  l 1 4:
    trk 1 2:
      if eq i 3: break
      tr.add i
  l 4 2: tr.del i
  l 5 5: tr.add i
  flip 1
  o 1
  drunk 80
  a flip
  b o
  c drunk
  d file.rnd
  ret add x y

def ks_edge
  menu "KS" >> "EDGE"
  a 9
  trk a
  tr.add 1

def ks_edge2
  a 70
  b -3
  trk 1 a 1 2:
    steps 1 4:
      pl.rnd 3
  trk 3 3
  tr.add 2
  steps a a
  step a
  steps 1 3
  c 150
  prob c: tr.add 4
  prob b: tr.add 5
  every 0: tr.add 6
  skip 0: tr.add 7
  every b: tr.add 8
  l 1 2: every 2: tr.add 9
  steps has.lock:
    each: tr.add i
  trk 2:
    steps all:
      a rnd 0
      b rrand 5 1
      c pick {7}
      c 150
      d chance c
  seed ? toss 1 2
  l 1 3:
    seed i
    a rnd 100
  x rnd 100
  rand.seed 5
  toss.seed 6
  file.seed 7
  drunk.seed 8

def ks_other_dir
  dir "AUDIO/DRUMS/HIHATS"
  ret file.len

def ks_filter
  match "k1*"
  ret file.len

def ks_scopes
  l 1 3: steps has.trig:
    pl.rnd 3
    tr.add add i 8
  l 1 3:
    tr.add i
    steps has.trig
    pl.rnd 3
  pl.rnd 4
  each:
    seed i
    steps 1 8
    pl.rnd 5
  if 1:
    steps 1 4
    tr.add 9
  pl.vary 3 20
  prob 100:
    steps has.trig
    seed 3
    pl.rnd 6
  a rnd 1000
  trk 1 2:
    seed 5
    steps 2 3
    tl.add 3
  b rnd 1000
  steps has.trig
  trk 3 4
  steps 1 2
  c rnd 1000
  l 1 3:
    l 1 5:
      if eq i 2: break
    y add i j
  z i
  t j
  ret add a add b c

def ks_dirs
  dir "AUDIO/DRUMS/KICKS"
  a ks_other_dir
  x file.len
  b ks_filter
  y file.len
  z a
  t b

def ks_prm
  a pb 7
  b fx2 3
  lfo.dest 4 amp.vol
  fx.type 3 99
  c add on off
  pl.set 2 amp.vol 300

def ks_src_save
  input sample
  src.save
'''

# each registry operation once, from its argument kinds (a statement, or `a <op>` for a value)
_SAMPLE = {O.INT: "3", O.STEP: "3", O.TRACK: "2", O.SLOT: "5", O.PARAM: "2", O.VALUE: "64",
           O.COND: "50%", O.PCT: "50", O.FILE: "1", O.STR: '"abc"', O.LIST: "{1 2 3}"}
_HANDWRITTEN = {"?", "TRK", "STEPS", "STEP", "SEED", "RAND.SEED", "TOSS.SEED", "PROB.SEED",
                "DRUNK.SEED", "FILE.SEED", "DIR", "EXT", "MATCH", "MSG", "SRC.SAVE"}


def per_op_script():
    stmts = []
    for o in O.ALL_OPS:
        if o.name in _HANDWRITTEN:
            continue
        args = ["3" if isinstance(k, O.OPT_INT) else _SAMPLE[k] for k in o.args]
        text = " ".join([o.shown] + args)
        stmts.append(("a " + text) if o.ret is not None and not o.stmt_only else text)
        if o.settable:
            stmts.append(" ".join([o.shown] + ["1"] * (len(o.args) + 1)))
    out = []
    for k in range(0, len(stmts), 8):
        out.append("def ks_g%d\n" % (k // 8) + "".join("  %s\n" % s for s in stmts[k:k + 8]))
    return "\n".join(out)


def kitchen_sources():
    return [("KITCHEN.OTY", KITCHEN), ("KOPS.OTY", per_op_script())]


_CARD = []


def make_card():
    """A small card (one per process, removed at exit; the tests only read it)."""
    if _CARD:
        return _CARD[0]
    root = tempfile.mkdtemp(prefix="otb_card_")
    atexit.register(shutil.rmtree, root, True)
    _CARD.append(root)
    for rel in ("AUDIO/DRUMS/KICKS/k1.wav", "AUDIO/DRUMS/KICKS/k2.wav", "AUDIO/DRUMS/KICKS/k3.aif",
                "AUDIO/DRUMS/KICKS/s1.wav", "AUDIO/DRUMS/KICKS/z9.mp3", "AUDIO/DRUMS/SNARES/s1.wav",
                "AUDIO/DRUMS/SNARES/s2.wav", "AUDIO/DRUMS/HIHATS/h1.wav", "AUDIO/DRUMS/HIHATS/h2.wav",
                "AUDIO/DRUMS/HIHATS/h3.wav", "AUDIO/DRUMS/HIHATS/h4.wav", "abc/x.wav"):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")
    return root


def compile_text(text, label="T.OTY"):
    prog = otype.check_sources([(label, text)])
    return prog, otb.compile_program(prog)


def decode_all(f):
    """[(pc, Ins, operands, size)] of a function's code; the sizes must tile it exactly."""
    out, pc = [], 0
    while pc < len(f.code):
        ins, ops, size = otb.decode(f.code, pc)
        out.append((pc, ins, ops, size))
        pc += size
    assert pc == len(f.code)
    return out


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prog_ex = otype.check_files(EXAMPLES)
        cls.c_ex = otb.compile_program(cls.prog_ex)
        cls.t_ex = otb.parse(cls.c_ex.data)
        cls.prog_k = otype.check_sources(kitchen_sources())
        cls.c_k = otb.compile_program(cls.prog_k)
        cls.t_k = otb.parse(cls.c_k.data)


# ---- format -------------------------------------------------------------------------------------
class TestFormat(Base):
    def test_examples_compile_clean(self):
        self.assertEqual([str(d) for d in self.c_ex.diags], [])
        self.assertEqual(self.c_ex.nrefused, 0)
        self.assertEqual(self.t_ex.nfunc, 33)         # 18 + LEGACY.OTY 15

    def test_kitchen_is_clean(self):
        bad = [str(d) for d in self.prog_k.diags if d.level == "error"]
        self.assertEqual(bad, [])
        self.assertEqual([str(d) for d in self.c_k.diags], [])

    def test_header(self):
        d = self.c_ex.data
        t = self.t_ex
        self.assertEqual(d[:3], b"OTB")
        self.assertEqual(d[3], 1)                    # format version
        self.assertEqual(d[4], 1)                    # language version
        self.assertEqual(t.total, len(d))
        self.assertEqual(t.pool_off, otb.HEADER_LEN)
        # the regions follow each other without gap, in this order
        self.assertEqual(t.ftab_off, t.pool_off + t.pool_len)
        self.assertEqual(t.aux_off, t.ftab_off + t.nfunc * otb.FENT)
        self.assertEqual(t.line_off, t.aux_off + t.aux_len)
        self.assertEqual(t.code_off, t.line_off + t.line_len)
        self.assertEqual(t.total, t.code_off + t.code_len)
        self.assertLessEqual(t.total, otb.MAX_TABLE)
        for off in (t.pool_off, t.pool_len, t.ftab_off, t.aux_off, t.aux_len, t.line_off,
                    t.line_len, t.code_off):
            self.assertEqual(off % 2, 0, "16-bit fields stay 2-aligned")

    def test_big_endian_header(self):
        d = self.c_ex.data
        self.assertEqual(int.from_bytes(d[6:8], "big"), 33)          # function count
        self.assertEqual(int.from_bytes(d[28:30], "big"), len(d))    # total size

    def test_function_order_is_the_checkers(self):
        want = [f.name for f in self.prog_ex.funcs if not f.refused]
        self.assertEqual([f.name for f in self.t_ex.funcs], want)

    def test_function_table_fields(self):
        f = self.t_ex.fmap["WALK"]
        self.assertEqual(f.menus, [["DRUMS", "WALK"]])
        self.assertEqual(f.hosts, otb.HOST_BITS["MENU"])
        self.assertEqual(f.touches, otb.TOUCH_BITS["TRAC"])
        self.assertTrue(f.families & otb.FAMILY_BITS["EDIT"])
        self.assertFalse(f.families & otb.FAMILY_BITS["IO_R"])
        self.assertEqual(f.max_cost, self.prog_ex.maxcost[self.prog_ex.fmap["WALK"]])
        self.assertEqual(f.nlocals, 1)
        self.assertTrue(f.file.endswith("RHYTHM.OTY"))
        self.assertEqual(f.def_line, 20)
        ab = self.t_ex.fmap["AB_EUCLID"]
        self.assertEqual(list(ab.params), ["HITS"])
        self.assertEqual(ab.params["HITS"], (5, 1, 16))
        self.assertTrue(ab.destructive)
        k = self.t_ex.fmap["KICK"]
        self.assertEqual(k.args, ["TRACK", "FOLDER"])
        self.assertEqual(k.menus, [])
        self.assertEqual(k.hosts, 0)
        # a brick that reads the card: IO_R and the slots; the namespaces it uses are listed
        po = self.t_ex.fmap["PLAY_ONE"]
        self.assertTrue(po.families & otb.FAMILY_BITS["IO_R"])
        self.assertTrue(po.touches & otb.TOUCH_BITS["SLOTS"])
        self.assertTrue(po.ns_mask & (1 << O.NAMESPACES["FILE"]))
        # families / touches of a caller include its callees'
        bb = self.t_ex.fmap["BOOMBAP"]
        self.assertTrue(bb.touches & otb.TOUCH_BITS["SLOTS"])
        self.assertTrue(bb.families & otb.FAMILY_BITS["IO_R"])

    def test_names_fit(self):
        for f in self.t_ex.funcs:
            self.assertLessEqual(len(f.name), otb.NAME_LEN)

    def test_everything_is_in_one_file_per_function_table(self):
        # the table of 33 functions (the examples) is small; the budget is 32 KB
        self.assertLess(len(self.c_ex.data), 8192)

    def test_pool_dedups(self):
        pool = otb.Pool()
        a = pool.string("X")
        self.assertEqual(pool.string("X"), a)
        self.assertNotEqual(pool.string("Y"), a)
        self.assertEqual(pool.list([1, 2]), pool.list((1, 2)))
        self.assertGreaterEqual(a, 2)
        self.assertEqual(a % 2, 0)
        lo, mo = pool.list([1, -2, 300]), pool.menus([["A", "B"], ["C", "D", "E"]])
        v = otb.PoolView(bytes(pool.buf))
        self.assertEqual(v.string(a), "X")
        self.assertEqual(v.list(lo), [1, -2, 300])
        self.assertEqual(v.menus(mo), [["A", "B"], ["C", "D", "E"]])

    def test_parse_rejects_garbage(self):
        with self.assertRaises(otb.OtbError):
            otb.parse(b"XYZ" + bytes(40))
        d = bytearray(self.c_ex.data)
        d[3] = 9
        with self.assertRaises(otb.OtbError):
            otb.parse(bytes(d))
        with self.assertRaises(otb.OtbError):
            otb.parse(self.c_ex.data[:-1])

    def test_unknown_opcode_is_refused(self):
        with self.assertRaises(otb.OtbError):
            otb.decode(bytes([0x00]), 0)
        with self.assertRaises(otb.OtbError):
            otb.decode(bytes([0x11, 0x00]), 0)       # push16 cut short


# ---- round trip, structure ------------------------------------------------------------------------
class TestRoundTrip(Base):
    def tables(self):
        return (self.t_ex, self.t_k)

    def test_every_instruction_decodes_and_tiles_the_code(self):
        for t in self.tables():
            for f in t.funcs:
                n = 0
                for pc, ins, ops, size in decode_all(f):
                    n += 1
                    self.assertEqual(bytes(f.code[pc:pc + size])[:1], bytes([ins.code]))
                self.assertGreater(n, 0)
                self.assertEqual(f.code[-1], otb.BY_NAME["stop"].code, "every function ends in stop")

    def test_jump_targets_are_instruction_boundaries(self):
        for t in self.tables():
            for f in t.funcs:
                starts = {pc for pc, _i, _o, _s in decode_all(f)}
                for pc, ins, ops, size in decode_all(f):
                    if ins.name in otb.JUMPS or ins.name in otb.BACKWARD:
                        self.assertIn(ops[-1], starts, "%s %04x" % (f.name, pc))

    def test_forward_jumps_go_forward_and_loops_are_the_only_backward_ones(self):
        for t in self.tables():
            for f in t.funcs:
                dec = decode_all(f)
                begins = []          # open loop constructs: (name, pc after the begin)
                for pc, ins, ops, size in dec:
                    if ins.name in otb.JUMPS:
                        self.assertGreater(ops[-1], pc, "%s %s at %04x jumps backward" % (f.name, ins.name, pc))
                    if ins.name in ("lbeg", "ebeg", "pass"):
                        begins.append((ins.name, pc + size, ops[-1]))
                    if ins.name in otb.BACKWARD:
                        kind, body, end = begins.pop()
                        self.assertEqual(ins.name, {"lbeg": "lnext", "ebeg": "enext", "pass": "pnext"}[kind])
                        self.assertEqual(ops[0], body, "a backward jump targets the start of ITS loop body")
                        self.assertLess(ops[0], pc)
                        self.assertEqual(pc + size, end, "the loop construct ends right after its next")
                self.assertEqual(begins, [])

    def test_scopes_and_calls_balance(self):
        for t in self.tables():
            for f in t.funcs:
                depth = 0
                for pc, ins, ops, size in decode_all(f):
                    if ins.name == "scope":
                        depth += 1
                    elif ins.name == "unscope":
                        depth -= 1
                    self.assertGreaterEqual(depth, 0)
                    if ins.name == "call":
                        self.assertLess(ops[0], len(t.funcs))
                        self.assertEqual(t.funcs[ops[0]].index, ops[0])
                self.assertEqual(depth, 0)

    def test_registry_ids_in_code_exist(self):
        for t in self.tables():
            for f in t.funcs:
                for pc, ins, ops, size in decode_all(f):
                    if ins.name in ("op", "opv", "set"):
                        self.assertIn((ops[0], ops[1]), otb.REG, "%s %04x" % (f.name, pc))

    def test_line_tables(self):
        for t in self.tables():
            for f in t.funcs:
                pcs = [p for p, _l in f.lines]
                self.assertEqual(pcs, sorted(pcs))
                self.assertEqual(len(set(pcs)), len(pcs))
                self.assertTrue(all(p < len(f.code) for p in pcs))
                self.assertEqual(otb.line_of(f, 0), f.lines[0][1])

    def test_counters_match_the_every_skip_instructions(self):
        for t in self.tables():
            for f in t.funcs:
                ks = [ops[0] for _p, ins, ops, _s in decode_all(f) if ins.name in ("every", "skip")]
                self.assertEqual(sorted(ks), list(range(len(f.counters))))

    def test_listing_decodes_line_by_line(self):
        sources = {k: v for k, v in self.prog_ex.raw.items()}
        text = otb.disassemble(self.t_ex, sources)
        for f in self.t_ex.funcs:
            one = otb.disassemble(self.t_ex, sources, f.name)
            rows = [l for l in one.splitlines() if re.match(r"^[0-9a-f]{4}  ", l)]
            dec = decode_all(f)
            self.assertEqual(len(rows), len(dec))
            for row, (pc, ins, ops, size) in zip(rows, dec):
                self.assertEqual(int(row[:4], 16), pc)
                got = row[6:].split("  ")[0].split()
                self.assertEqual(len(got), size, row)
                self.assertEqual(int(got[0], 16), ins.code)
        self.assertIn("RHYTHM.OTY:20", text)
        self.assertIn("o.min 1", text)               # the source line in the comment column

    def test_max_stack_is_exact(self):
        # the compile-time bound is reached by some run (the VM tracks the depth it reaches)
        card = make_card()
        for t, prog in ((self.t_ex, self.prog_ex), (self.t_k, self.prog_k)):
            for f in t.funcs:
                if f.args:
                    continue
                reached = {}
                for seed in (1, 2):
                    run = {}
                    orig = vm.VMRun.run

                    def spy(self_, orig=orig, run=run):
                        orig(self_)
                        run["hw"] = self_.hw

                    vm.VMRun.run = spy
                    try:
                        vm.simulate_otb(t, f.name, seed=seed, card=card)
                    except sim.RunError:
                        pass
                    finally:
                        vm.VMRun.run = orig
                    for name, (st, cx, cl) in run.get("hw", {}).items():
                        g = t.fmap[name]
                        self.assertLessEqual(st, g.max_stack)
                        self.assertLessEqual(cx, g.max_ctx)
                        self.assertLessEqual(cl, g.max_ctl)


# ---- determinism -----------------------------------------------------------------------------------
class TestDeterminism(Base):
    def test_same_files_same_bytes(self):
        again = otb.compile_program(otype.check_files(EXAMPLES))
        self.assertEqual(again.data, self.c_ex.data)
        self.assertEqual(otb.compile_program(otype.check_sources(kitchen_sources())).data, self.c_k.data)

    def test_hash_seed_does_not_matter(self):
        out = []
        for hs in ("1", "2", "random"):
            env = dict(os.environ, PYTHONHASHSEED=hs)
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "otype.py"), "dump", "--hex"]
                               + EXAMPLES, capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertEqual(p.returncode, 0, p.stderr)
            out.append(hashlib.sha256(p.stdout.encode()).hexdigest())
        self.assertEqual(len(set(out)), 1)
        # and it is the bytes of the API
        want = otb.hexdump(self.c_ex.data)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "otype.py"), "dump", "--hex"]
                           + EXAMPLES, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(p.stdout, want)

    def test_file_order_is_the_load_order(self):
        a = otb.parse(otb.compile_program(otype.check_files(EXAMPLES)).data)
        b = otb.parse(otb.compile_program(otype.check_files(list(reversed(EXAMPLES)))).data)
        self.assertEqual(sorted(f.name for f in a.funcs), sorted(f.name for f in b.funcs))
        self.assertNotEqual([f.name for f in a.funcs], [f.name for f in b.funcs])
        self.assertEqual([f.name for f in b.funcs][:2], ["SPRINKLE", "EUCLID_HATS"][:2])


# ---- compile errors ---------------------------------------------------------------------------------
def deep(n):
    return "def dpt\n  menu \"T\" >> \"DEEP\"\n  a " + "add 1 " * n + "1\n"


class TestStackDepth(unittest.TestCase):
    def test_depth_16_is_accepted(self):
        prog, c = compile_text(deep(15))
        self.assertEqual([str(d) for d in c.diags], [])
        self.assertEqual(otb.parse(c.data).fmap["DPT"].max_stack, 16)

    def test_depth_17_is_a_compile_error(self):
        prog, c = compile_text(deep(16))
        self.assertEqual([d for d in prog.diags if d.level == "error"], [], "the checker accepts it")
        self.assertEqual([str(d) for d in c.diags], ["T.OTY:3: dpt: error: stack depth 17 > 16"])
        t = otb.parse(c.data)
        self.assertEqual(t.nfunc, 0)
        self.assertEqual(t.nrefused, 1)

    def test_callers_are_refused_too(self):
        text = deep(16) + 'def top\n  menu "T" >> "TOP"\n  dpt\ndef fine\n  menu "T" >> "FINE"\n  tr.add 1\n'
        prog, c = compile_text(text)
        msgs = [str(d) for d in c.diags]
        self.assertEqual(len(msgs), 2)
        self.assertIn("T.OTY:3: dpt: error: stack depth 17 > 16", msgs)
        self.assertIn("T.OTY:6: top: error: needs dpt", msgs)
        t = otb.parse(c.data)
        self.assertEqual([f.name for f in t.funcs], ["FINE"])
        self.assertEqual(t.nrefused, 2)

    def test_checker_refusals_are_counted_not_silent(self):
        text = 'def good\n  menu "T" >> "GOOD"\n  tr.add 1\ndef bad\n  menu "T" >> "BAD"\n  tr.add 99\n'
        prog, c = compile_text(text)
        t = otb.parse(c.data)
        self.assertEqual([f.name for f in t.funcs], ["GOOD"])
        self.assertEqual(t.nrefused, 1)

    def test_cli_reports_the_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "D.OTY")
            with open(p, "w") as fh:
                fh.write(deep(16))
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "otype.py"), "dump", p],
                               capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(r.returncode, 1)
            self.assertIn("dpt: error: stack depth 17 > 16", r.stderr)
            self.assertIn("1 refused", r.stdout)


# ---- coverage ---------------------------------------------------------------------------------------
class TestCoverage(Base):
    def test_every_opcode_is_exercised(self):
        seen = set()
        for t in (self.t_k, self.t_ex):
            for f in t.funcs:
                seen.update(ins.name for _p, ins, _o, _s in decode_all(f))
        self.assertEqual(sorted(set(otb.BY_NAME) - seen), [])

    def test_every_registry_operation_is_compiled(self):
        allops = {o.name for o in O.ALL_OPS}
        self.assertEqual(sorted(allops - set(self.c_k.used_ops)), [])

    def test_every_registry_id_appears_in_a_table(self):
        ids = set()
        for t in (self.t_k, self.t_ex):
            for f in t.funcs:
                for _p, ins, ops, _s in decode_all(f):
                    if ins.name in ("op", "opv", "set"):
                        ids.add((ops[0], ops[1]))
        # `?`, dir, ext, match, msg are compiled to sel / dir / ext / match / msg (no id in the code)
        native = {O.OPS[n].id for n in ("?", "DIR", "EXT", "MATCH", "MSG")}
        missing = {o.id for o in O.ALL_OPS} - ids - native
        self.assertEqual(sorted(missing), [])

    def test_opcodes_are_unique_one_byte(self):
        self.assertEqual(len(otb.INS), len(otb.INSTRS))
        for ins in otb.INSTRS:
            self.assertTrue(0 < ins.code < 256)

    def test_pool_holds_strings_and_lists(self):
        pool = self.t_k.pool
        self.assertIn('"AUDIO/DRUMS/KICKS"', [pool.describe(o) for o in range(2, self.t_k.pool_len, 2)])


# ---- the spec says what the code does ---------------------------------------------------------------
class TestSpec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # the spec, in either repository layout (docs/syntax.md here)
        spec = os.path.join(ROOT, "research", "otype-syntax.md")
        if not os.path.exists(spec):
            spec = os.path.join(ROOT, "docs", "syntax.md")
        with open(spec) as fh:
            text = fh.read()
        cls.sec = text[text.index("## 14. Compiled table"):]

    def test_instruction_table_lists_every_opcode(self):
        for ins in otb.INSTRS:
            row = [l for l in self.sec.splitlines() if l.startswith("| 0x%02x |" % ins.code)]
            self.assertEqual(len(row), 1, "0x%02x %s" % (ins.code, ins.name))
            self.assertIn("`%s`" % ins.name, row[0])

    def test_layout_constants(self):
        self.assertIn("| 28 | 2 | `total`", self.sec)
        self.assertIn("**Function table entry** (54 bytes)", self.sec)
        self.assertEqual(otb.FENT, 54)
        self.assertEqual(otb.HEADER_LEN, 32)
        self.assertIn("`nfunc` × 54", self.sec)

    def test_worked_example_is_the_real_listing(self):
        prog = otype.check_files(EXAMPLES)
        table = otb.parse(otb.compile_program(prog).data)
        listing = otb.disassemble(table, prog.raw, "WALK").rstrip("\n")
        # the spec shows paths as given on the command line from the repo root
        rel = listing.replace(ROOT + os.sep, "")
        self.assertIn(rel, self.sec)


# ---- page functions -------------------------------------------------------------------------------
PAGE_SRC = textwrap.dedent("""\
    def plain
      menu "A" >> "B"
      tr.add 1
    def both
      menu "DRUMS" >> "BOTH"
      page "EUCL"
      param pl1 4 0 64
      param pl2 2 0 64
      l 1 4:
        every 2: tr.add i
        skip 3: tr.add 1
    def only
      page "PL1"
      param pl1 3 0 9
      tr.add pl1
    def noparam
      page "NONE"
      tr.add 2
    def brick
      tr.add 3
    """)


class TestPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prog = otype.check_sources([("T.OTY", PAGE_SRC)])
        cls.c = otb.compile_program(cls.prog)
        cls.t = otb.parse(cls.c.data)

    def aux_of(self, k):
        """(aux bytes of function k)"""
        d, t = self.c.data, self.t
        p = t.ftab_off + k * otb.FENT
        off = int.from_bytes(d[p + 48:p + 50], "big")
        f = t.funcs[k]
        n = len(f.params) * 8 + f.nargs * 2 + len(f.counters) * 4 + (2 if f.hosts & otb.HOST_BITS["PAGE"] else 0)
        return d[t.aux_off + off:t.aux_off + off + n]

    def test_it_compiles_clean(self):
        self.assertEqual([str(d) for d in self.prog.diags], [])
        self.assertEqual(self.t.nfunc, 5)

    def test_the_host_mask_has_bit_7(self):
        self.assertEqual(otb.HOST_BITS["PAGE"], 0x80)
        h = {f.name: f.hosts for f in self.t.funcs}
        self.assertEqual(h, {"PLAIN": 0x01, "BOTH": 0x81, "ONLY": 0x80, "NOPARAM": 0x80, "BRICK": 0})
        self.assertEqual({f.name: f.page for f in self.t.funcs},
                         {"PLAIN": None, "BOTH": "EUCL", "ONLY": "PL1", "NOPARAM": "NONE", "BRICK": None})

    def test_the_label_offset_ends_the_aux_data_after_the_counters(self):
        f = self.t.fmap["BOTH"]
        self.assertEqual(len(f.params), 2)
        self.assertEqual(len(f.counters), 2)
        aux = self.aux_of(f.index)
        self.assertEqual(len(aux), 2 * 8 + 2 * 4 + 2)
        off = int.from_bytes(aux[-2:], "big")
        self.assertEqual(self.t.pool.string(off), "EUCL")
        self.assertEqual(self.t.pool.data[off], otb.P_STR)
        # the counters are where they were: right after the params and the arguments
        self.assertEqual([tuple(aux[16 + 4 * k:16 + 4 * k + 2]) for k in range(2)],
                         [tuple(int(f.counters[k][0]).to_bytes(2, "big")) for k in range(2)])
        # a page function without params: the aux data IS the label
        aux = self.aux_of(self.t.fmap["NOPARAM"].index)
        self.assertEqual(self.t.pool.string(int.from_bytes(aux, "big")), "NONE")
        # a function that is not a page function has no tail
        self.assertEqual(len(self.aux_of(self.t.fmap["PLAIN"].index)), 0)
        self.assertEqual(len(self.aux_of(self.t.fmap["BRICK"].index)), 0)

    def test_aux_regions_are_contiguous_and_total_length_is_right(self):
        want = 0
        for f in self.t.funcs:
            want += len(f.params) * 8 + f.nargs * 2 + len(f.counters) * 4 + (2 if f.page is not None else 0)
        self.assertEqual(self.t.aux_len, want)
        offs = [int.from_bytes(self.c.data[self.t.ftab_off + k * otb.FENT + 48:][:2], "big") for k in range(self.t.nfunc)]
        self.assertEqual(offs, sorted(offs))
        self.assertEqual(offs[0], 0)

    def test_pool_order_the_label_comes_right_after_the_menu_list(self):
        t = self.t
        f = t.fmap["BOTH"]
        p = t.ftab_off + f.index * otb.FENT
        menu_off = int.from_bytes(t.data[p + 16:p + 18], "big")
        page_off = int.from_bytes(self.aux_of(f.index)[-2:], "big")
        pl1_off = int.from_bytes(self.aux_of(f.index)[:2], "big")
        file_off = int.from_bytes(t.data[p + 40:p + 42], "big")
        self.assertEqual(t.pool.menus(menu_off), [["DRUMS", "BOTH"]])
        self.assertTrue(file_off < menu_off < page_off < pl1_off, (file_off, menu_off, page_off, pl1_off))
        # the entry that follows the menu list is the label: nothing between them
        size = 2 + sum(1 + sum(1 + len(x) for x in m) for m in t.pool.menus(menu_off))
        self.assertEqual(page_off, menu_off + ((size + 1) & ~1))

    def test_a_label_equal_to_a_param_name_shares_the_pool_string(self):
        f = self.t.fmap["ONLY"]
        aux = self.aux_of(f.index)
        self.assertEqual(int.from_bytes(aux[:2], "big"), int.from_bytes(aux[-2:], "big"))      # PL1 / PL1
        self.assertEqual(self.t.pool.string(int.from_bytes(aux[-2:], "big")), "PL1")

    def test_a_page_only_function_has_its_label_after_the_file_name_and_before_its_params(self):
        t = self.t
        f = t.fmap["ONLY"]
        p = t.ftab_off + f.index * otb.FENT
        file_off = int.from_bytes(t.data[p + 40:p + 42], "big")
        self.assertEqual(int.from_bytes(t.data[p + 16:p + 18], "big"), 0)          # no menu list
        self.assertGreater(int.from_bytes(self.aux_of(f.index)[-2:], "big"), file_off)

    def test_old_tables_read_as_before(self):
        # a table without page functions: every host mask is 0 or 1, nothing after the counters
        c = otb.compile_program(otype.check_files(EXAMPLES[:1] + EXAMPLES[1:3]))
        t = otb.parse(c.data)
        self.assertTrue(all(f.hosts in (0, 1) and f.page is None for f in t.funcs))

    def test_dump_shows_hosts_and_label(self):
        listing = otb.func_listing(self.t, self.t.fmap["BOTH"])
        self.assertIn("hosts MENU PAGE", listing)
        self.assertIn('page   "EUCL"', listing)
        listing = otb.func_listing(self.t, self.t.fmap["ONLY"])
        self.assertIn("hosts PAGE ", listing)
        self.assertIn("menu (page only)", listing)
        self.assertIn('page   "PL1"', listing)
        self.assertNotIn("page ", otb.func_listing(self.t, self.t.fmap["PLAIN"]).replace("(page only)", ""))

    def test_the_example_pages_compile(self):
        prog = otype.check_files([os.path.join(ROOT, "modules", "otype", "examples", "PAGE.OTY")])
        c = otb.compile_program(prog, O.PROFILES["m1"])
        self.assertEqual([str(d) for d in c.diags], [])
        t = otb.parse(c.data)
        self.assertEqual({f.name: f.page for f in t.funcs}, {"EUCL": "EUCL", "PDENS": "DENS"})
        self.assertEqual(list(t.fmap["EUCL"].params), ["PL1", "PL2", "OP", "R1", "R2", "TRO"])
        self.assertEqual([t.fmap["EUCL"].params[k] for k in t.fmap["EUCL"].params],
                         [(4, 0, 64), (0, 0, 64), (0, 0, 3), (0, -63, 63), (0, -63, 63), (0, -63, 63)])

    def test_parse_still_walks_every_function_with_pages_between(self):
        for f in self.t.funcs:
            self.assertEqual(f.params if f.name in ("BOTH", "ONLY") else {}, f.params)
        self.assertEqual(self.t.fmap["BOTH"].params, {"PL1": (4, 0, 64), "PL2": (2, 0, 64)})
        self.assertEqual(self.t.fmap["ONLY"].params, {"PL1": (3, 0, 9)})

    def test_sim_and_vm_agree_on_page_functions_with_params(self):
        for name in ("both", "only", "noparam"):
            for params in ({}, {"pl1": 7}, {"pl1": 99}):
                if name == "noparam" and params:
                    continue
                r1 = sim.result_json(sim.simulate(self.prog, name, seed=3, params=params))
                r2 = sim.result_json(vm.simulate_otb(self.t, name, seed=3, params=params))
                self.assertEqual(r1, r2, (name, params))
        # the param really reaches the code: `only` adds a trig at step pl1
        r = sim.result_json(vm.simulate_otb(self.t, "only", seed=3, params={"pl1": 7}))
        self.assertEqual(r["model"]["tracks"]["1"]["masks"]["trig"], "0x0000000000000040")      # step 7
        r = sim.result_json(vm.simulate_otb(self.t, "only", seed=3))
        self.assertEqual(r["model"]["tracks"]["1"]["masks"]["trig"], "0x0000000000000004")      # the default 3


CONFIRM_SRC = textwrap.dedent("""\
    def asks
      menu "POOL" >> "CLEAR"
      confirm "CLEAR POOL?"
      tr.clr
    def bare
      menu "POOL" >> "FILL"
      confirm
      tr.add 1
    def implied
      menu "TRIGS" >> "CLEAR"
      tr.clr
    def firsttext
      menu "A" >> "B"
      confirm ""
      confirm "FIRST"
      confirm "SECOND"
      tr.add 2
    def withpage
      menu "A" >> "C"
      page "PG"
      confirm "PAGE?"
      param p 1 0 5
      l 1 4:
        every 2: tr.add i
    def shared
      menu "A" >> "D"
      confirm "CLEAR POOL?"
      tr.add 3
    """)


class TestConfirmText(unittest.TestCase):
    """`confirm "TEXT"` is kept (spec 14.1): flag bit 2, the text's pool offset after the page label in the aux data, the text
    after the page label in the pool; an old reader that ignores the bit reads the same table as before."""

    @classmethod
    def setUpClass(cls):
        cls.prog = otype.check_sources([("C.OTY", CONFIRM_SRC)])
        cls.c = otb.compile_program(cls.prog)
        cls.t = otb.parse(cls.c.data)

    def entry(self, name):
        return self.t.ftab_off + self.t.fmap[name].index * otb.FENT

    def tail(self, name):
        """The aux bytes of a function after its params, arguments and counters (page label, then confirm text)."""
        f, d = self.t.fmap[name], self.c.data
        p = self.entry(name)
        off = self.t.aux_off + int.from_bytes(d[p + 48:p + 50], "big") + len(f.params) * 8 + f.nargs * 2 + len(f.counters) * 4
        n = (2 if f.page is not None else 0) + (2 if f.flags & otb.F_CONFIRM_TEXT else 0)
        return d[off:off + n]

    def test_it_compiles_clean(self):
        self.assertEqual([str(d) for d in self.prog.diags], [])
        self.assertEqual(self.t.nfunc, 6)

    def test_flags_and_texts(self):
        self.assertEqual(otb.F_CONFIRM_TEXT, 4)
        self.assertEqual({f.name: f.flags for f in self.t.funcs},
                         {"ASKS": 7, "BARE": 2, "IMPLIED": 1, "FIRSTTEXT": 6, "WITHPAGE": 6, "SHARED": 6})
        self.assertEqual({f.name: f.confirm_text for f in self.t.funcs},
                         {"ASKS": "CLEAR POOL?", "BARE": None, "IMPLIED": None, "FIRSTTEXT": "FIRST", "WITHPAGE": "PAGE?",
                          "SHARED": "CLEAR POOL?"})

    def test_the_offset_ends_the_aux_data_after_the_page_label(self):
        tl = self.tail("WITHPAGE")
        self.assertEqual(len(tl), 4)
        self.assertEqual(self.t.pool.string(int.from_bytes(tl[:2], "big")), "PG")
        self.assertEqual(self.t.pool.string(int.from_bytes(tl[2:], "big")), "PAGE?")
        self.assertEqual(self.t.fmap["WITHPAGE"].counters, [(24, 0)])          # the counters are where they were
        self.assertEqual(self.t.pool.string(int.from_bytes(self.tail("ASKS"), "big")), "CLEAR POOL?")
        self.assertEqual(self.tail("BARE"), b"")

    def test_aux_length_counts_the_tails(self):
        want = sum(len(f.params) * 8 + f.nargs * 2 + len(f.counters) * 4 + (2 if f.page is not None else 0)
                   + (2 if f.confirm_text is not None else 0) for f in self.t.funcs)
        self.assertEqual(self.t.aux_len, want)

    def test_pool_order_the_text_comes_right_after_the_page_label_and_before_the_params(self):
        tl = self.tail("WITHPAGE")
        page_off, conf_off = int.from_bytes(tl[:2], "big"), int.from_bytes(tl[2:], "big")
        p = self.entry("WITHPAGE")
        aux0 = self.t.aux_off + int.from_bytes(self.c.data[p + 48:p + 50], "big")
        param_off = int.from_bytes(self.c.data[aux0:aux0 + 2], "big")
        self.assertEqual(conf_off, page_off + ((2 + len("PG") + 1) & ~1))
        self.assertLess(conf_off, param_off)
        # the same text in two functions is one pool entry
        self.assertEqual(self.tail("ASKS"), self.tail("SHARED"))

    def test_a_reader_that_ignores_the_bit_reads_the_table_as_before(self):
        bad = bytearray(self.c.data)
        for f in self.t.funcs:
            bad[self.entry(f.name) + 23] &= ~otb.F_CONFIRM_TEXT
        t = otb.parse(bytes(bad))
        for a, b in zip(t.funcs, self.t.funcs):
            self.assertEqual((a.params, a.args, a.counters, a.page, a.menus, a.code), (b.params, b.args, b.counters, b.page, b.menus, b.code))
            self.assertIsNone(a.confirm_text)
        # and the runs are the same: the text changes nothing in the code
        for name in ("asks", "firsttext", "withpage"):
            self.assertEqual(sim.result_json(vm.simulate_otb(t, name, seed=2)),
                             sim.result_json(vm.simulate_otb(self.t, name, seed=2)), name)

    def test_without_any_text_there_is_no_bit_and_no_tail(self):
        # functions that never write `confirm "TEXT"` (a plain `confirm`, an implied one, none) have no bit 2 and no tail:
        # their entries and aux data are those of format 1 as they were
        src = CONFIRM_SRC.replace('confirm "CLEAR POOL?"', "confirm").replace('confirm "FIRST"', "confirm")
        src = src.replace('confirm "SECOND"', "confirm").replace('confirm "PAGE?"', "confirm")
        t = otb.parse(otb.compile_program(otype.check_sources([("C.OTY", src)])).data)
        self.assertTrue(all(not f.flags & otb.F_CONFIRM_TEXT and f.confirm_text is None for f in t.funcs))
        self.assertEqual(t.aux_len, sum(len(f.params) * 8 + len(f.counters) * 4 + (2 if f.page else 0) for f in t.funcs))
        # M1.OTY writes texts: they are in its table now
        c = otb.compile_program(otype.check_files([os.path.join(ROOT, "modules", "otype", "examples", "M1.OTY")]))
        self.assertTrue(any(f.confirm_text for f in otb.parse(c.data).funcs))

    def test_dump_shows_the_text(self):
        self.assertIn('confirm "CLEAR POOL?"', otb.func_listing(self.t, self.t.fmap["ASKS"]))
        self.assertNotIn('confirm "', otb.func_listing(self.t, self.t.fmap["BARE"]))


# ---- the equivalence ---------------------------------------------------------------------------------
def entropy(table, f, memo=None):
    """Does f (or what it calls) draw random numbers, read a stream, or keep a state?"""
    memo = {} if memo is None else memo
    if f.name in memo:
        return memo[f.name]
    memo[f.name] = False
    r = False
    for _p, ins, ops, _s in decode_all(f):
        if ins.name in ("prob", "every", "skip", "set"):
            r = True
        elif ins.name in ("op", "opv"):
            op = otb.REG[(ops[0], ops[1])]
            if op.stream is not None or op.family in (O.STATE, O.CTX):
                r = True
        elif ins.name == "call":
            r = r or entropy(table, table.funcs[ops[0]], memo)
    memo[f.name] = r
    return r


def one(fn):
    """(result json or ("error", text))"""
    try:
        return sim.result_json(fn())
    except sim.RunError as e:
        return ("error", str(e))


def equivalent(tc, prog, table, name, card=None, seeds=SEEDS, **kw):
    """sim and vm, `seeds`, PRESSES presses each (state chained): the JSON must be identical."""
    for seed in seeds:
        s1 = s2 = None
        for press in range(PRESSES):
            r1 = one(lambda: sim.simulate(prog, name, seed=seed, card=card, state=s1, **kw))
            r2 = one(lambda: vm.simulate_otb(table, name, seed=seed, card=card, state=s2, **kw))
            if isinstance(r1, tuple) or isinstance(r2, tuple):
                tc.assertEqual(r1, r2, "%s seed %d press %d" % (name, seed, press))
                break
            diff = [k for k in r1 if r1[k] != r2.get(k)]
            tc.assertEqual(diff, [], "%s seed %d press %d: %s" % (name, seed, press, diff))
            tc.assertEqual(r1, r2)
            s1, s2 = r1["state"], r2["state"]
    return True


class TestEquivalence(Base):
    @classmethod
    def setUpClass(cls):
        Base.setUpClass()
        cls.card = make_card()

    def run_all(self, prog, table, seeds=SEEDS, card=True):
        n = 0
        for f in prog.funcs:
            if f.refused or f.args or f.name not in table.fmap:
                continue
            equivalent(self, prog, table, f.name, self.card if card else None, seeds)
            n += 1
        return n

    def test_examples(self):
        n = self.run_all(self.prog_ex, self.t_ex)
        self.assertGreaterEqual(n, 7)             # the menu functions and the argument-less bricks
        menus = [f.name for f in self.prog_ex.funcs if f.menus and not f.args]
        self.assertEqual(menus, ["BOOMBAP", "POOL_FILL", "POOL_REFILL", "POOL_SHUFFLE", "POOL_CLEAR", "RND_LFO", "RND_FX", "SCENES_GEN", "SCENES_CLR",
                                 "RND_PLOCKS", "RND_SMPLOCKS", "CLR_PLOCKS", "CLR_SMPLOCKS", "TRIGLESS", "INIT_TRACK", "KICKS", "KIT_LOCK", "FOUR", "AB_EUCLID", "WALK", "SPRINKLE",
                                 "EUCLID_HATS", "AB_LOCKS", "TWO_EUCLIDS"])

    def test_examples_are_not_vacuous(self):
        # the equivalence compares real work: trigs placed, locks, slots, draws, state
        r = vm.simulate_otb(self.t_ex, "boombap", seed=3, card=self.card)
        j = sim.result_json(r)
        self.assertGreater(j["cost"], 100)
        self.assertTrue(j["model"]["slots"])
        self.assertGreater(sum(j["draws"].values()), 3)
        self.assertNotEqual(sim.result_json(vm.simulate_otb(self.t_ex, "boombap", seed=4, card=self.card)), j)
        w = sim.result_json(vm.simulate_otb(self.t_ex, "walk", seed=1))
        self.assertTrue(w["state"]["funcs"]["walk"])

    def test_kitchen_sink(self):
        n = self.run_all(self.prog_k, self.t_k)
        self.assertGreater(n, 8)

    def test_kitchen_main_does_a_lot(self):
        j = sim.result_json(vm.simulate_otb(self.t_k, "ks_main", seed=5, card=self.card))
        self.assertGreater(j["cost"], 200)
        self.assertGreater(sum(j["draws"].values()), 4)
        e = sim.result_json(vm.simulate_otb(self.t_k, "ks_edge2", seed=5))
        self.assertGreaterEqual(len(e["out_of_range"]), 3, "the out-of-range rule is exercised")
        self.assertTrue(sim.result_json(vm.simulate_otb(self.t_k, "ks_prm", seed=5))["out_of_range"])

    def test_params_and_tracks_and_lengths(self):
        for name, params in (("ks_hasp", {"n": 4}), ("ks_main", {"p": 9, "q": 0}),
                             ("ab_euclid", {"hits": 11})):
            prog, table = ((self.prog_k, self.t_k) if name.startswith("ks") else (self.prog_ex, self.t_ex))
            equivalent(self, prog, table, name, self.card, seeds=(0, 3, 17), params=params)
        for track, length in ((3, 64), (8, 7), (2, 33)):
            equivalent(self, self.prog_k, self.t_k, "ks_main", self.card, seeds=(1, 9), track=track,
                       length=length)
            equivalent(self, self.prog_ex, self.t_ex, "boombap", self.card, seeds=(2,), track=track,
                       length=length)

    def test_no_card(self):
        for name in ("boombap", "ab_euclid"):
            equivalent(self, self.prog_ex, self.t_ex, name, None, seeds=(0, 7))

    def test_the_two_engines_agree_on_runtime_errors(self):
        text = 'def big\n  l 1 3000: l 1 3000: l 1 3000: tr.add 1\n'
        prog = otype.check_sources([("T.OTY", text)])
        t = otb.parse(otb.compile_program(prog).data)
        one_r = one(lambda: sim.simulate(prog, "big", seed=1))
        two_r = one(lambda: vm.simulate_otb(t, "big", seed=1))
        self.assertEqual(one_r[0], "error")
        self.assertEqual(one_r, two_r)

    def test_harvested_scripts(self):
        """Every script the other two OType suites give the checker (their tests are the
        reference scripts): each function without argument runs on both engines."""
        sources = harvest()
        self.assertGreater(len(sources), 300)
        funcs = runs = 0
        for srcs in sources:
            prog = otype.check_sources(list(srcs))
            c = otb.compile_program(prog)
            table = otb.parse(c.data)
            memo = {}
            for f in prog.funcs:
                if f.refused or f.args or f.name not in table.fmap:
                    continue
                funcs += 1
                # a function with no random draw and no state gives the same answer for every seed
                seeds = SEEDS if entropy(table, table.fmap[f.name], memo) else (0, 20)
                equivalent(self, prog, table, f.name, None, seeds)
                runs += len(seeds)
        self.assertGreater(funcs, 800)
        self.assertGreater(runs, 4000)

    def test_declared_depths_are_never_exceeded(self):
        # VMRun.step raises when a call exceeds its function's declared stack / scopes / control
        # frames: the whole equivalence run above is that check; here the declared numbers are
        # sane for the kitchen sink
        m = self.t_k.fmap["KS_MAIN"]
        self.assertGreaterEqual(m.max_ctx, 3)
        self.assertGreaterEqual(m.max_ctl, 3)
        self.assertLessEqual(m.max_stack, otb.MAX_STACK)


_HARVEST = []


def harvest():
    """The (label, text) lists given to `otype.check_sources` while the two other OType suites run."""
    if _HARVEST:
        return _HARVEST
    import test_otype
    import test_otype_run
    seen, got = set(), []
    orig = otype.check_sources

    def rec(sources):
        key = tuple((l, re.sub(r"[ \t]+", " ", t)) for l, t in sources)
        if key not in seen:
            seen.add(key)
            got.append(list(sources))
        return orig(sources)

    otype.check_sources = rec
    try:
        for mod in (test_otype_run, test_otype):
            suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
            unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        otype.check_sources = orig
    _HARVEST.extend(got)
    return _HARVEST


# ---- the resumable state ------------------------------------------------------------------------------
class TestVMState(Base):
    def test_step_is_one_instruction(self):
        seen = {}

        class Spy(vm.VMRun):
            def run(self):
                steps = list(range(1, self.length + 1))
                ctx = sim.Ctx([self.host_track], steps, self.new_streams(self.seed))
                self.frames = [vm.VFrame(self.root, [], sim.FileList(), [ctx.copy()])]
                n = 0
                while self.frames:
                    before = self.executed
                    self.step()
                    assert self.executed == before + 1
                    n += 1
                seen["n"] = n

        res = sim.simulate(vm.OtbProgram(self.t_ex), "sprinkle", seed=1, run_class=Spy)
        ref = vm.simulate_otb(self.t_ex, "sprinkle", seed=1)
        self.assertEqual(sim.result_json(res), sim.result_json(ref))
        self.assertGreater(seen["n"], 50)

    def test_state_is_data(self):
        # everything a suspended call holds is plain data: pc, stack, locals, counters, scopes
        fr = vm.VFrame(self.t_ex.funcs[0], [], sim.FileList(), [])
        for k in ("pc", "stack", "locals", "i", "j", "ctxs", "ctl", "args", "files", "plist"):
            self.assertTrue(hasattr(fr, k), k)

    def test_vm_reads_only_the_bytes(self):
        # a table rebuilt from nothing but its bytes runs identically (no object of the checker)
        raw = bytes(self.c_ex.data)
        t = otb.parse(bytearray(raw))
        a = sim.result_json(vm.simulate_otb(t, "walk", seed=2))
        b = sim.result_json(vm.simulate_otb(raw, "walk", seed=2))
        self.assertEqual(a, b)


# ---- the command line -------------------------------------------------------------------------------------
class TestCLI(Base):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = otype.main(["otype.py"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def sub(self, *args):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "otype.py")] + list(args),
                           capture_output=True, text=True, cwd=ROOT)
        return p.returncode, p.stdout, p.stderr

    def test_dump_listing(self):
        rc, out, err = self.cli("dump", *EXAMPLES)
        self.assertEqual(rc, 0, err)
        self.assertIn("OTB format 1  language 1  33 functions, 0 refused", out)
        for f in self.t_ex.funcs:
            self.assertIn(f.show() + "  (", out)

    def test_dump_func_walk(self):
        rc, out, err = self.cli("dump", *EXAMPLES, "--func", "walk")
        self.assertEqual(rc, 0, err)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("walk  ("), first)
        self.assertIn("menu DRUMS > WALK", first)
        self.assertIn("families EDIT", first)
        self.assertIn("touches TRAC", first)
        self.assertIn("locals a", first)
        self.assertIn("0000  10 01        push8 1", out)
        self.assertIn("op o.min", out)
        self.assertNotIn("sprinkle", out)
        rc, out2, err = self.cli("dump", *EXAMPLES, "--func", "WALK")
        self.assertEqual(out, out2)

    def test_dump_unknown_func(self):
        rc, out, err = self.cli("dump", *EXAMPLES, "--func", "nosuch")
        self.assertEqual(rc, 1)
        self.assertIn("no function named nosuch", err)

    def test_dump_writes_the_binary(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "OUT.OTB")
            rc, out, err = self.cli("dump", *EXAMPLES, "-o", p)
            self.assertEqual(rc, 0, err)
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), self.c_ex.data)

    def test_dump_hex(self):
        rc, out, err = self.cli("dump", *EXAMPLES, "--hex")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, otb.hexdump(self.c_ex.data))
        self.assertTrue(out.startswith("0000  4f 54 42 01 01 00"))

    def test_dump_usage(self):
        self.assertEqual(self.cli("dump")[0], 2)
        self.assertEqual(self.cli("dump", "/no/such/file.OTY")[0], 2)
        self.assertEqual(self.cli("dump", *EXAMPLES, "--bogus")[0], 2)
        self.assertEqual(self.cli("dump", "-h")[0], 0)

    def test_dump_refused_function_is_counted(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "R.OTY")
            with open(p, "w") as fh:
                fh.write('def good\n  menu "T" >> "GOOD"\n  tr.add 1\ndef bad\n  tr.add 99\n')
            rc, out, err = self.cli("dump", p)
            self.assertEqual(rc, 1)
            self.assertIn("1 functions, 1 refused", out.replace("function,", "functions,"))
            self.assertIn("bad: error", err)

    def test_run_vm_matches_run(self):
        card = make_card()
        for name in ("boombap", "ab_euclid", "walk", "euclid_hats", "two_euclids"):
            a = self.cli("run", name, *EXAMPLES, "--seed", "4", "--card", card)
            b = self.cli("run", name, *EXAMPLES, "--seed", "4", "--card", card, "--vm")
            self.assertEqual(a[0], 0)
            self.assertEqual(a, b, name)
            a = self.cli("run", name, *EXAMPLES, "--seed", "4", "--card", card, "--json", "-v")
            b = self.cli("run", name, *EXAMPLES, "--seed", "4", "--card", card, "--json", "-v", "--vm")
            self.assertEqual(a, b, name)

    def test_run_vm_state_over_presses(self):
        with tempfile.TemporaryDirectory() as d:
            s1, s2 = os.path.join(d, "a.json"), os.path.join(d, "b.json")
            for press in range(3):
                a = self.cli("run", "walk", *EXAMPLES, "--seed", "6", "--state", s1)
                b = self.cli("run", "walk", *EXAMPLES, "--seed", "6", "--state", s2, "--vm")
                self.assertEqual(a, b)
                with open(s1) as f1, open(s2) as f2:
                    self.assertEqual(f1.read(), f2.read())

    def test_run_vm_subprocess(self):
        a = self.sub("run", "walk", *EXAMPLES, "--seed", "1")
        b = self.sub("run", "walk", *EXAMPLES, "--seed", "1", "--vm")
        self.assertEqual(a[0], 0)
        self.assertEqual(a, b)

    def test_run_vm_errors_like_run(self):
        for args in (("nosuch",), ("kick",)):
            a = self.cli("run", *args, *EXAMPLES)
            b = self.cli("run", *args, *EXAMPLES, "--vm")
            self.assertEqual(a, b, args)
            self.assertEqual(a[0], 1)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "R.OTY")
            with open(p, "w") as fh:
                fh.write('def bad\n  menu "T" >> "BAD"\n  tr.add 99\n')
            a = self.cli("run", "bad", p)
            b = self.cli("run", "bad", p, "--vm")
            self.assertEqual(a, b)
            self.assertEqual(a[0], 1)

    def test_run_vm_of_a_function_the_compiler_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "D.OTY")
            with open(p, "w") as fh:
                fh.write(deep(16))
            rc, out, err = self.cli("run", "dpt", p, "--vm")
            self.assertEqual(rc, 1)
            self.assertIn("not in the compiled table", err)
            self.assertIn("stack depth 17 > 16", err)
            self.assertEqual(self.cli("run", "dpt", p)[0], 0)      # the tree walker has no such limit

    def test_usage_mentions_dump_and_vm(self):
        rc, out, err = self.cli()
        self.assertIn("dump", err)
        self.assertIn("--vm", err)
        rc, out, err = self.cli("run", "-h")
        self.assertIn("--vm", out)


class TestProfile(unittest.TestCase):
    """`compile_program(prog, profile)` and `dump --profile m1` (the ops on the unit)."""
    M1 = os.path.join(ROOT, "modules", "otype", "examples", "M1.OTY")
    SRC = ('def uses_msg\n  menu "T" >> "MSG"\n  tr.add 1\n  msg "hi"\n'
           'def calls_msg\n  menu "T" >> "CALLS"\n  uses_msg\n'
           'def m1_only\n  menu "T" >> "OK"\n  tr.add 1\n  smp.set 1 1\n')

    # msg is on the m1 profile now, and no other op is outside it: these tests need one, so they run on m1 without msg.
    REAL_M1 = O.PROFILES["m1"]

    def setUp(self):
        from unittest import mock
        p = mock.patch.dict(O.PROFILES, {"m1": self.REAL_M1 - {"MSG"}})
        p.start()
        self.addCleanup(p.stop)

    def comp(self, profile):
        prog = otype.check_sources([("P.OTY", self.SRC)])
        self.assertEqual([str(d) for d in prog.diags if d.level == "error"], [])
        return otb.compile_program(prog, profile)

    def cli(self, *args):
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = otype.main(["otype.py"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_m1_includes_v1_machine_ops_and_msg(self):
        """The v1 machine profile includes every implemented operation, msg (the end box text) among them."""
        m1 = self.REAL_M1
        self.assertEqual(len(m1), 177)
        self.assertEqual(sorted(o.name for o in O.ALL_OPS if o.name not in m1), sorted(O._M1_NO_OPS))
        self.assertIn("DR.V", m1)
        self.assertIn("TRK.SLOT", m1)
        self.assertIn("TEMPO", m1)
        self.assertIn("MSG", m1)
        for n in ("TR.ADD", "SMP.SET", "HAS.SMP", "STATIC.LOAD", "DIR", "ER", "PL.SET", "PL.GET", "PL.DEL", "PL.RND",
                  "PL.VARY", "PL.CLR", "TL.ADD", "TL.FILL", "HAS.LOCK", "PB", "AMP", "AMP.VOL", "LFO.DEP", "FX2",
                  "PAR.SET", "LFO.DEST", "FX.TYPE", "SCN.CLR", "TRK.INIT", "TRK.TYPE",
                  "COND.SET", "COND.RND", "MICRO.SET", "MICRO.RND", "RATCH.SET", "TRK.SLOT", "TEMPO"):
            self.assertIn(n, m1)
        self.assertTrue(O.in_profile(O.OPS["TEMPO"], None))
        self.assertTrue(O.in_profile(O.OPS["TEMPO"], m1))
        self.assertTrue(O.in_profile(O.OPS["MSG"], m1))

    def test_tempo_is_refused_and_its_caller_needs_it(self):
        c = self.comp(O.PROFILES["m1"])
        msgs = {d.func: (d.line, d.msg) for d in c.diags}
        self.assertEqual(msgs["uses_msg"], (4, "not on the unit yet: msg"))
        self.assertEqual(msgs["calls_msg"], (7, "needs uses_msg"))
        self.assertNotIn("m1_only", msgs)
        self.assertEqual(c.nrefused, 2)
        self.assertEqual([f.name for f in c.funcs], ["M1_ONLY"])

    def test_without_a_profile_nothing_changes(self):
        c = self.comp(None)
        self.assertEqual([str(d) for d in c.diags], [])
        self.assertEqual(c.nrefused, 0)
        prog = otype.check_sources([("P.OTY", self.SRC)])
        self.assertEqual(otb.compile_program(prog).data, c.data)

    def test_op_used_as_a_value_counts_too(self):
        # (dr.v was the value op outside m1 until its tables were added: a profile without it keeps the check honest)
        prog = otype.check_sources([("P.OTY", 'def v\n  menu "T" >> "V"\n  x dr.v 1 2\n')])
        c = otb.compile_program(prog, O.PROFILES["m1"] - {"DR.V"})
        self.assertEqual([str(d) for d in c.diags],
                         ["P.OTY:3: v: error: not on the unit yet: dr.v"])
        self.assertEqual(otb.compile_program(prog, O.PROFILES["m1"]).diags, [])

    def test_m3c_ops_compile_under_the_profile(self):
        """The trig-word ops are on the unit profile (they were refused before)."""
        prog = otype.check_sources([("P.OTY", 'def v\n  menu "T" >> "V"\n  tr.add 1\n  cond.set 1 50%\n'
                                             '  cond.rnd fill 3:4\n  micro.set 1 -5\n  micro.rnd 4\n  ratch.set 1 3\n')])
        self.assertEqual([str(d) for d in otb.compile_program(prog, O.PROFILES["m1"]).diags], [])

    def test_m3b_ops_compile_under_the_profile(self):
        """The part ops (and trk.type, trk.slot, tempo) are on the unit profile."""
        prog = otype.check_sources([("P.OTY", 'def v\n  menu "T" >> "V"\n  par.set amp.vol 9\n'
                                             '  lfo.dest 1 fx1 1\n  fx.type 1 2\n  scn.clr\n'
                                             '  trk.init\n  x trk.type\n  trk.slot 3\n  tempo 120\n')])
        c = otb.compile_program(prog, O.PROFILES["m1"])
        self.assertEqual([str(d) for d in c.diags], [])

    def test_m3a_ops_compile_under_the_profile(self):
        """The p-lock and trigless-lock ops are on the unit profile (they were refused before)."""
        prog = otype.check_sources([("P.OTY", 'def v\n  menu "T" >> "V"\n  tl.fill 30\n  pl.rnd amp.vol\n'
                                              '  pl.vary pb 1 10\n  pl.set 1 amp.vol 64\n  x pl.get 1 amp.vol\n  pl.del 1 amp.vol\n'
                                              '  tl.add 5\n  pl.clr\n')])
        c = otb.compile_program(prog, O.PROFILES["m1"])
        self.assertEqual(([str(d) for d in c.diags], c.nrefused), ([], 0))

    def test_unknown_profile_is_usage_error(self):
        rc, out, err = self.cli("dump", self.M1, "--profile", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("unknown profile nope (one of m1)", err)
        self.assertEqual(self.cli("dump", self.M1, "--profile")[0], 2)

    def test_m1_script_dumps_clean_under_the_profile(self):
        rc, out, err = self.cli("dump", self.M1, "--profile", "m1")
        self.assertEqual(rc, 0, err)
        self.assertIn("3 functions, 0 refused", out)

    def test_dump_profile_refuses_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "P.OTY")
            with open(p, "w") as fh:
                fh.write(self.SRC)
            rc, out, err = self.cli("dump", p, "--profile", "m1")
            self.assertEqual(rc, 1)
            self.assertIn("P.OTY:4: uses_msg: error: not on the unit yet: msg", err)
            self.assertIn("1 function, 2 refused", out)
            self.assertEqual(self.cli("dump", p)[0], 0)

    def test_check_profile(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "P.OTY")
            with open(p, "w") as fh:
                fh.write(self.SRC)
            rc, out, err = self.cli("check", p, "--profile", "m1")
            self.assertEqual(rc, 1)
            self.assertIn("P.OTY:4: uses_msg: error: not on the unit yet: msg", out)
            self.assertIn("P.OTY:7: calls_msg: error: needs uses_msg", out)
            self.assertEqual(self.cli("check", p)[0], 0)
        self.assertEqual(self.cli("check", self.M1, "--profile", "m1")[0], 0)
        self.assertEqual(self.cli("check", self.M1, "--profile", "nope")[0], 2)



class TestTypedParamsOTB(unittest.TestCase):
    def test_otb_encoding_and_parsing(self):
        src = textwrap.dedent("""\
            def tf
              menu "CAT" >> "TF"
              param flag bool yes
              param opt option 1 "A" "B" "C"
              param fld folder "AUDIO/DRUMS/KICKS"
              dir fld
        """)
        prog, c = compile_text(src)
        self.assertEqual(c.diags, [])
        t = otb.parse(c.data)
        f = t.funcs[0]
        self.assertTrue(f.flags & otb.F_TYPED_PARAMS)
        self.assertEqual(len(f.params), 3)

        pb = f.params["FLAG"]
        self.assertEqual(pb.kind, "BOOL")
        self.assertEqual(pb.d, 1)
        self.assertEqual(pb.lo, 0)
        self.assertEqual(pb.hi, 1)

        popt = f.params["OPT"]
        self.assertEqual(popt.kind, "OPTION")
        self.assertEqual(popt.d, 1)
        self.assertEqual(popt.lo, 0)
        self.assertEqual(popt.hi, 2)
        self.assertEqual(popt.labels, ("A", "B", "C"))

        pfld = f.params["FLD"]
        self.assertEqual(pfld.kind, "FOLDER")
        self.assertEqual(pfld.lo, 0)
        self.assertEqual(pfld.hi, 0)
        self.assertEqual(pfld.path, "AUDIO/DRUMS/KICKS")

    def test_int_only_params_do_not_set_flag(self):
        src = textwrap.dedent("""\
            def plain
              menu "CAT" >> "PLAIN"
              param p 5 0 10
              tr.add p
        """)
        prog, c = compile_text(src)
        self.assertEqual(c.diags, [])
        t = otb.parse(c.data)
        f = t.funcs[0]
        self.assertFalse(f.flags & otb.F_TYPED_PARAMS)
        p = f.params["P"]
        self.assertEqual(p.kind, "INT")
        self.assertEqual(p.d, 5)

    def test_func_listing_disassembly(self):
        src = textwrap.dedent("""\
            def tf
              menu "CAT" >> "TF"
              param flag bool no
              param opt option 0 "FIRST" "SECOND"
              param fld folder "AUDIO/DRUMS"
              dir fld
        """)
        prog, c = compile_text(src)
        t = otb.parse(c.data)
        listing = otb.func_listing(t, t.fmap["TF"])
        self.assertIn("flag bool default NO", listing)
        self.assertIn('opt option default 0 ["FIRST" "SECOND"]', listing)
        self.assertIn('fld folder default "AUDIO/DRUMS"', listing)

    def test_sim_and_vm_execution_with_folder_param(self):
        card = make_card()
        src = textwrap.dedent("""\
            def fld_test
              menu "CAT" >> "FLD_TEST"
              param src folder "AUDIO/DRUMS/KICKS"
              dir src
              static.load 1 1
        """)
        prog, c = compile_text(src)
        t = otb.parse(c.data)

        # 1. Default folder path
        r_sim = sim.simulate(prog, "fld_test", seed=1, card=card)
        r_vm = vm.simulate_otb(t, "fld_test", seed=1, card=card)
        self.assertEqual(sim.result_json(r_sim), sim.result_json(r_vm))
        self.assertEqual(r_sim.model.slots[1], "AUDIO/DRUMS/KICKS/k1.wav")

        # 2. Overridden folder path
        overrides = {"SRC": "AUDIO/DRUMS/SNARES"}
        r_sim_ov = sim.simulate(prog, "fld_test", seed=1, card=card, params=overrides)
        r_vm_ov = vm.simulate_otb(t, "fld_test", seed=1, card=card, params=overrides)
        self.assertEqual(sim.result_json(r_sim_ov), sim.result_json(r_vm_ov))
        self.assertEqual(r_sim_ov.model.slots[1], "AUDIO/DRUMS/SNARES/s1.wav")

    def test_sim_and_vm_bool_and_option_params(self):
        src = textwrap.dedent("""\
            def logic_test
              menu "CAT" >> "LOGIC"
              param active bool yes
              param mult option 0 "X1" "X2"
              if active:
                if mult:
                  tr.add 2
                else:
                  tr.add 1
        """)
        prog, c = compile_text(src)
        self.assertEqual(c.diags, [])
        t = otb.parse(c.data)

        # Default
        r1 = sim.result_json(sim.simulate(prog, "logic_test", seed=1))
        r2 = sim.result_json(vm.simulate_otb(t, "logic_test", seed=1))
        self.assertEqual(r1, r2)
        self.assertEqual(r1["model"]["tracks"]["1"]["masks"]["trig"], "0x0000000000000001")

        # Override mult=1
        r1_m = sim.result_json(sim.simulate(prog, "logic_test", seed=1, params={"mult": 1}))
        r2_m = sim.result_json(vm.simulate_otb(t, "logic_test", seed=1, params={"mult": 1}))
        self.assertEqual(r1_m, r2_m)
        self.assertEqual(r1_m["model"]["tracks"]["1"]["masks"]["trig"], "0x0000000000000002")

        # Override active=0
        r1_off = sim.result_json(sim.simulate(prog, "logic_test", seed=1, params={"active": 0}))
        r2_off = sim.result_json(vm.simulate_otb(t, "logic_test", seed=1, params={"active": 0}))
        self.assertEqual(r1_off, r2_off)
        self.assertEqual(r1_off["model"]["tracks"]["1"]["masks"]["trig"], "0x0000000000000000")
        self.assertEqual(r1_off["model"]["tracks"]["1"]["masks"]["trig"], "0x0000000000000000")


if __name__ == "__main__":
    unittest.main()
