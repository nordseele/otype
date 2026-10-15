"""The C VM (firmware/c/vm.c) against the Python reference VM (modules/otype/vm.py).
From the repo root:

    python3 -m unittest tools/test_otype_cvm.py -v

The test builds the C unit as a shared library (`build_c.py --host`, into out/otype/) and, for every
function without argument of the corpus whose operations are all supported by the C VM, runs it on the
reference VM and on the C VM -- seeds 0..20 (0 and 20 when the function neither draws nor keeps a
state), three presses chained, the C struct persisting between presses -- and requires identical
cost, draws, out-of-range list, globals and state, and the same error class.

Corpus: the examples, the kitchen sink and the one-op-per-statement script of test_otype_otb.py,
every script the two other OType suites feed to the checker (`harvest()`), and EXTRA below: a
generated script that forces the edges (saturation, floor modulo, wrap, state limits, counters with
n < 1, break inside nested loops inside a pass, call depth 8, seeds in loops and calls...).
A must-fail test builds the library with -DOTC_SABOTAGE (MOD truncating like C; tr.del forgetting the
word) and requires the comparison to find the difference.

the card / slot operations (dir ext match file.* static.* slots.*). Every press of a function that
uses them binds a FRESH fake card (HostCard: a real directory tree walked in a SHUFFLED, seeded order, so the
C sort is really exercised) and the slots the unit holds (HostSlots: slot -> "../" + path, with the
simulator's `refuse` / `busy` arguments) on BOTH engines. Compared whole: error, cost, draws, out-of-range,
warnings, working set, bags (function, FNV hash, n, rest), refusals, the unit's slot table (modulo the
"../"), CARD BUSY (the simulator's CardBusy.result). TestCardTrees runs a template over the trees of
(empty, 1 file, 130 / 4,097 names, long names, mixed case, 6 levels, dot files,
extensions, a non-ASCII name, missing folder, `dir ""`); TestCardM1 the M1 scripts; TestCardRefusals,
TestCardBusy, TestCardBudget the rest; TestCardMustFail builds the library with each card / slot sabotage
(OTC_SABOTAGE_SORT / DOT / BAG=1 / BAG=2 / FILL / CAP / COPY / SLOTPATH / REFUSE) and requires the tests
to catch it. QUICK=1 in the environment shrinks the seed lists of the card tests (the full run is the
default).

the trig / sample-lock operations (tr.* smp.* has.* is.empty trk.type, `steps <predicate>`) write
the pattern's own bytes. Every press starts both engines from the same pattern: the simulator reads a
bank file (+ machine types), the C VM gets fresh RAM TRAC blocks built from the same file
(sim.ram_blocks), bound with otc_vm_bind. After the press the 8 decoded blocks must equal the
simulator's model, the skipped lists must be equal, and no byte outside the masks / records / words
may have changed. The default start is an EMPTY pattern; TestRealPatterns starts from the user's real
bank01.work (patterns 1 and 2) when the file is there.
"""
import atexit
import collections
import copy
import ctypes
import os
import random
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
import test_otype_otb as T  # noqa: E402
from modules.otype import cvm, gen_ops_c, ops as O, otb, sim, vm  # noqa: E402

OUT = os.path.join(ROOT, "out", "otype")
SO = ".dylib" if sys.platform == "darwin" else ".so"
SEEDS, PRESSES = T.SEEDS, T.PRESSES
CSRC = os.path.join(ROOT, "firmware", "c")


# ---- building the library ---------------------------------------------------------------------
_LIBS = {}


def build_lib(tag="", sabotage=False, defines=()):
    """modules/otype/build_c.py --host: the whole C unit (also the ColdFire asm, checked) + the dylib.
    `defines`: -D of the must-fail builds (OTC_SABOTAGE_SORT ...)."""
    if tag in _LIBS:
        return _LIBS[tag]
    os.makedirs(OUT, exist_ok=True)
    lib = os.path.join(OUT, "libotc_cvm%s%s" % (tag, SO))
    cmd = [sys.executable, os.path.join(ROOT, "modules", "otype", "build_c.py"),
           "--out", os.path.join(OUT, "cvm%s.s" % tag), "--host", lib] + (["--sabotage"] if sabotage else []) \
        + [a for d in defines for a in ("--define", d)]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if r.returncode:
        raise RuntimeError("build_c.py failed:\n" + r.stdout + r.stderr)
    _LIBS[tag] = lib
    return lib


def build_cost_lib(limit):
    """The host library with a small OTC_MAX_COST, to test the 'run too long' boundary exactly."""
    key = "_cost%d" % limit
    if key in _LIBS:
        return _LIBS[key]
    lib = os.path.join(OUT, "libotc_cvm%s%s" % (key, SO))
    r = subprocess.run(["cc", "-O2", "-shared", "-fPIC", "-I", CSRC, "-DOTC_MAX_COST=%du" % limit, "-o", lib,
                        os.path.join(CSRC, "otype_unit.c"), os.path.join(CSRC, "host_shim.c")],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    _LIBS[key] = lib
    return lib


# ---- what is supported ------------------------------------------------------------------------
def unsupported(table, f, memo=None):
    """Counter of the unsupported things f (and what it calls) uses; empty = the C VM can run it."""
    memo = {} if memo is None else memo
    if f.name in memo:
        return memo[f.name]
    memo[f.name] = collections.Counter()
    bad = collections.Counter()
    for _p, ins, ops, _s in T.decode_all(f):
        if ins.name in ("op", "set"):
            op = otb.REG[(ops[0], ops[1])]
            if not gen_ops_c.supported(op):
                bad[op.shown] += 1
        elif ins.name == "call":
            bad.update(unsupported(table, table.funcs[ops[0]], memo))
    memo[f.name] = bad
    return bad


# ---- the comparison ---------------------------------------------------------------------------
COV = {"ins": set(), "op": set(), "set": set(), "opv": set(), "oor": set()}


CARD_INS = {"dir": 1, "ext": 2, "match": 3}      # the operation number (namespace 11) of the three instructions


class Spy(vm.VMRun):
    """The reference VM, recording which instructions and operations a run executes."""

    def step(self):
        fr = self.frames[-1]
        ins, ops, _ = otb.decode(fr.fn.code, fr.pc)
        COV["ins"].add(ins.name)
        if ins.name in COV:
            COV[ins.name].add(tuple(ops))
        elif ins.name in CARD_INS:                  # dir ext match are instructions, not `op`s: count their op
            COV["op"].add((11, CARD_INS[ins.name]))
        elif ins.name == "msg":                     # msg is an instruction too: count its op (14, 1)
            COV["op"].add((14, 1))
        vm.VMRun.step(self)


class Stats:
    def __init__(self):
        self.funcs = self.runs = self.skipped = self.errors = 0
        self.pattern_funcs = self.skips = self.pickups = self.writing = 0
        self.card_funcs = self.card_runs = self.warned = self.refused = self.busy = self.loaded = 0
        self.skip_ops = collections.Counter()


def uses_card(table, f, memo=None):
    """Does f (or what it calls) use a card / slot operation (dir ext match file.* static.* slots.*)?"""
    memo = {} if memo is None else memo
    if f.name in memo:
        return memo[f.name]
    memo[f.name] = False
    r = False
    for _p, ins, ops, _s in T.decode_all(f):
        if ins.name in ("dir", "ext", "match"):
            r = True
        elif ins.name in ("op", "set"):
            r = r or gen_ops_c.card(otb.REG[(ops[0], ops[1])])
        elif ins.name == "call":
            r = r or uses_card(table, table.funcs[ops[0]], memo)
    memo[f.name] = r
    return r


def uses_pattern(table, f, memo=None):
    """Does f (or what it calls) use an operation on the pattern's bytes or `steps <predicate>`?"""
    memo = {} if memo is None else memo
    if f.name in memo:
        return memo[f.name]
    memo[f.name] = False
    r = False
    for _p, ins, ops, _s in T.decode_all(f):
        if ins.name in ("op", "set"):
            r = r or otb.REG[(ops[0], ops[1])].name in (gen_ops_c.E2B_EXTRA | gen_ops_c.M3A_EXTRA | gen_ops_c.M3C_EXTRA)
        elif ins.name == "opv":
            r = r or (otb.REG[(ops[0], ops[1])].name == "STEPS" and ops[2] == otb.STEPS_PRED)
        elif ins.name == "call":
            r = r or uses_pattern(table, table.funcs[ops[0]], memo)
    memo[f.name] = r
    return r


# ---- the pattern both engines start from ------------------------------------------------------
_EMPTY = os.path.join(OUT, "e2b_empty.bank")
REAL_DIR = os.environ.get("OTYPE_REAL_DIR", "")      # optional: the OTYPE folder of a real card
PICKUP = sim.PICKUP


def read_file(path):
    with open(path, "rb") as fh:
        return fh.read()


def empty_bank():
    """A bank file whose 16 patterns are EMPTY like bank02.work's: masks 0 (swing 0xaa..), records 0xff,
    words 0, length 16.  Written once into out/otype/."""
    if not os.path.exists(_EMPTY):
        os.makedirs(OUT, exist_ok=True)
        d = bytearray(sim.BANK_HEAD + 16 * sim.PATTERN_STRIDE + 100)
        d[0:4], d[8:16] = b"FORM", b"DPS1BANK"
        for q in range(16):
            base = sim.BANK_HEAD + q * sim.PATTERN_STRIDE
            for t in range(8):
                tr = sim.trac_off(q, t)
                d[tr + 0x40:tr + 0x48] = bytes([0xAA]) * 8              # the swing mask
                d[tr + 0x50] = 16
                d[tr + 0x59:tr + 0x59 + 64 * 0x20] = b"\xff" * (64 * 0x20)
            d[base + sim.PATTERN_STRIDE - 9] = 16
        with open(_EMPTY, "wb") as fh:
            fh.write(d)
    return _EMPTY


class Start:
    """Where a press starts: a bank file + pattern (the simulator reads it, the C VM gets the RAM
    blocks built from it) and the machine type of each track."""

    def __init__(self, path=None, pattern=1, label=None, pmodel=None):
        self.path = path or empty_bank()
        self.pattern = pattern
        self.pmodel = pmodel if pmodel is not None else sim.ParamModel()    # the part's descriptors / values
        self.label = label or "empty"
        self.blocks = sim.ram_blocks(self.path, pattern)
        self.json = {t: sim.decode_ram_block(b).to_json() for t, b in self.blocks.items()}

    def bind(self, cv, mtypes):
        for t in range(1, 9):
            cv.bind(t, self.blocks[t], mtypes.get(t, 0))
        if hasattr(cv, "bind_params"):          # a fresh host twin of the part for each press (clean call log)
            cv.bind_params(cvm.HostParams(copy.deepcopy(self.pmodel), pattern=self.pattern))


def writable(i):
    """Offsets of a TRAC block the trig / sample-lock operations may write: masks, records, words."""
    return i < 0x20 or 0x40 <= i < 0x50 or i == 0x52 or 0x59 <= i < 0x59 + 0x800 or 0x89A <= i < 0x91A


_SPY = {}


def spy_class(mtypes):
    """The Spy engine with the machine types `mtypes` {track: type} set on the model's parts."""
    key = tuple(sorted(mtypes.items()))
    if key not in _SPY:
        class S(Spy):
            def __init__(self, *a):
                Spy.__init__(self, *a)
                for t, ty in key:
                    self.model.part[t].mtype = ty
        _SPY[key] = S
    return _SPY[key]


# ---- the card both engines start from -------------------------------------------------
QUICK = bool(os.environ.get("QUICK"))


class CardSetup:
    """What a press starts with on both engines: the Set folder (a directory, or None), the slots the unit
    holds ({1..128: path}), the refusals and the busy card, as the simulator takes them.  The C VM gets a
    fresh HostCard (seeded, another walk order at every press) and HostSlots made from the same values.
    `leak_dots`: the fake walker breaks its contract and returns the dot names (must-fail test of the C
    filter)."""

    def __init__(self, root, slots=None, refuse=None, busy=None, label="card", seed=1, leak_dots=False):
        self.root, self.slots, self.refuse, self.busy = root, slots, refuse, busy
        self.label, self.seed, self.leak_dots = label, seed, leak_dots

    def sim_kw(self):
        return dict(card=self.root, slots=self.slots, refuse=self.refuse, busy=self.busy)

    def bind(self, cv, press=0):
        hc = cvm.HostCard(self.root, seed=self.seed * 100 + press, leak_dots=self.leak_dots)
        return cv.bind_card(hc, self.slots, self.refuse, self.busy)


_DEFAULT_SETUP = []


def default_setups():
    """What the corpus functions that use the card run on: the small card of test_otype_otb.py, and the
    "corpus" tree (the folders the harvested scripts list: A B F0..F8 AUDIO/...; B holds 140 files) with two
    slots already loaded and a deterministic fifth of the paths refused by the unit."""
    if not _DEFAULT_SETUP:
        _DEFAULT_SETUP.append(CardSetup(T.make_card(), label="small card"))
        _DEFAULT_SETUP.append(CardSetup(
            tree_root("corpus"), slots={2: "AUDIO/old2.wav", 5: "A/x.wav"},
            refuse=lambda p: sum(p.encode()) % 5 == 0, label="corpus tree, slots, refusals"))
    return _DEFAULT_SETUP


def py_run(prog, name, seed, state, track, length, start, mtypes, setup=None, params=None):
    """The simulator's result JSON ("busy" added), or ("error", text).  A CARD BUSY run is its Result."""
    kw = setup.sim_kw() if setup is not None else {}
    try:
        r = sim.result_json(sim.simulate(prog, name, seed=seed, state=state, track=track, length=length,
                                         bank=start.path, pattern=start.pattern, run_class=spy_class(mtypes),
                                         params=params, pmodel=copy.deepcopy(start.pmodel), **kw))
        r["busy"] = False
        return r
    except sim.CardBusy as e:
        r = sim.result_json(e.result)
        r["busy"] = True
        return r
    except sim.RunError as e:
        return ("error", str(e))


def same_list(tc, l1, l2, overflow, what):
    """The simulator's list l1 against the C VM's capped list l2 (+ the number of dropped records)."""
    if overflow:
        tc.assertEqual(l1[:len(l2)], l2, what)
        tc.assertEqual(len(l1), len(l2) + overflow, what)
    else:
        tc.assertEqual(l1, l2, what)


def check_press(tc, cv, start, r1, r2, what):
    """The decoded RAM blocks against the simulator's model, byte-level guard, touched (a superset of
    the tracks whose bytes differ at the end: add then del leaves them as they were).  Returns the
    set of the changed tracks."""
    changed = set()
    after = cv.blocks()
    for t in range(1, 9):
        a, b = start.blocks[t], after[t]
        if a == b:
            got = start.json[t]
        else:
            changed.add(t)
            bad = [i for i in range(len(a)) if a[i] != b[i] and not writable(i)]
            tc.assertEqual(bad, [], "%s: track %d: bytes outside the masks / records / words changed" % (what, t))
            got = sim.decode_ram_block(b).to_json()
        tc.assertEqual(r1["model"]["tracks"][str(t)], got, "%s: track %d" % (what, t))
    tc.assertEqual(changed - r2["touched"], set(), what + ": a changed track is not in touched")
    return changed


def check_card(tc, cv, setup, hs, r1, r2, what, stats=None):
    """Card / slot: warnings, refusals, working set, the unit's slot table, bags (inside `state`), slots_touched."""
    tc.assertEqual(r1["warnings"], r2["warnings"], what + " warnings")
    tc.assertEqual(r2["warn_overflow"], 0, what)
    same_list(tc, r1["refused"], r2["refused"], r2["refused_overflow"], what + " refused")
    tc.assertEqual(r1["model"]["working_set"], r2["working_set"], what + " working_set")
    tc.assertEqual(r1["model"]["slots"], {str(k): v for k, v in hs.final().items()}, what + " slots")
    audio_mut = cv.audio.mutated() if hasattr(cv, "audio") and cv.audio else False
    tc.assertEqual(bool(r2["slots_touched"]), hs.mutated() or audio_mut, what + " slots_touched")
    tc.assertEqual(cv.errors, [], what + " callback errors")
    if stats is not None:
        stats.card_runs += 1
        stats.warned += len(r1["warnings"])
        stats.refused += len(r1["refused"])
        stats.busy += r1["busy"]
        stats.loaded += bool(r1["model"]["slots"])


PART_WRITES = ("par_set", "lfo", "fx_set", "scene", "slot_init", "trk_slot", "control")


def check_part(tc, cv, r1, r2, what):
    """What the C VM wrote into the part (its host twin, cvm.HostParams) = the simulator's model.part (machine type
    aside), and part_touched = the tracks a write import was called for."""
    hp = getattr(cv, "params", None)
    if hp is None or not hasattr(hp, "part_json"):
        return
    want = {t: {k: v for k, v in j.items() if k != "type"} for t, j in r1["model"]["part"].items()}
    tc.assertEqual(want, hp.part_json(), what + " part")
    tc.assertEqual(r2["part_touched"], {e[1] + 1 for e in hp.log if e[0] in PART_WRITES}, what + " part_touched")


def param_list(prog, name, params):
    """The values `otc_vm_set_params` takes for the Python `--param` dict `params` (name -> value, any case): one per
    param of the function in declaration order, up to the last one given, the default for those in between."""
    fparams = prog.fmap[name.upper()].params
    given = {k.upper(): v for k, v in params.items()}
    last = max((i for i, n in enumerate(fparams) if n in given), default=-1)
    return [given.get(n, fparams[n][0]) for i, n in enumerate(fparams) if i <= last]


def compare_press(tc, cv, prog, name, seed, state, track, length, start, mtypes, setup, press, what, stats, params=None):
    """One press on both engines.  Returns the simulator's result JSON, or None after an error run.
    `params`: a `--param` dict, imposed on the Python VM and, through otc_vm_set_params, on the C VM."""
    pattern = hasattr(cv, "bind")      # an engine without the pattern binding (modules/otype/verify_vm.py)
    r1 = py_run(prog, name, seed, state, track, length, start, mtypes, setup, params)
    if params is not None:
        cv.set_params(param_list(prog, name, params))
    if pattern:
        start.bind(cv, mtypes)              # fresh blocks: the simulator re-reads its bank
    if setup is not None:
        hs = setup.bind(cv, press)                  # a fresh card and fresh slots
        if hasattr(cv, "bind_audio"):
            cv.bind_audio(cvm.HostAudio())
    else:
        hs = None
        if hasattr(cv, "unbind_card"):
            cv.unbind_card()                        # a function without card operations runs without a workspace
    r2 = cv.run(name, seed, track, length)
    stats.runs += 1
    if isinstance(r1, tuple):
        stats.errors += 1
        inv_audio = {v: k for k, v in sim.AUDIO_ERRORS.items()}
        if r1[1] in inv_audio:
            want = inv_audio[r1[1]]
        else:
            want = cvm.E_COST if "run too long" in r1[1] else cvm.E_ARG if "has never been saved" in r1[1] else cvm.E_TABLE
        tc.assertEqual(r2, ("error", want), what + " " + r1[1])
        return None
    tc.assertFalse(isinstance(r2, tuple), what + " C error %r" % (r2,))
    tc.assertEqual(r1["busy"], r2["busy"], what + " CARD BUSY")
    tc.assertEqual(r1["not_on_unit"], [], what)
    if hasattr(cv, "msg"):                          # the end box: the last msg of the run, "" = none (DONE)
        tc.assertEqual((r1["model"]["messages"] or [""])[-1], cv.msg(), what + " msg")
    if not pattern:
        tc.assertEqual(r1["model"]["tracks"], {str(t): j for t, j in start.json.items()}, what)
        tc.assertEqual((r1["skipped_no_trig"], r1["skipped_pickup"]), ([], []), what)
    same_list(tc, r1["out_of_range"], r2["out_of_range"], r2["oor_overflow"], what)
    COV["oor"].update(o["op"] for o in r1["out_of_range"])
    for k in ("cost", "draws", "globals", "state"):
        tc.assertEqual(r1[k], r2[k], "%s: %s" % (what, k))
    if setup is not None:
        check_card(tc, cv, setup, hs, r1, r2, what, stats)
    else:
        # (a `--param` clamp is the only warning the Python side may add: the C VM clamps silently)
        warns = [w for w in r1["warnings"] if not (params is not None and w.startswith("param ") and "clamped to" in w)]
        tc.assertEqual((warns, r1["refused"], r2["bound"]), ([], [], 0), what)
    check_part(tc, cv, r1, r2, what)
    if pattern:
        same_list(tc, r1["skipped_no_trig"], r2["skipped_no_trig"], r2["skip_overflow"], what + " skipped_no_trig")
        same_list(tc, r1["skipped_pickup"], r2["skipped_pickup"], r2["pickup_overflow"], what + " skipped_pickup")
        stats.skips += len(r1["skipped_no_trig"])
        stats.pickups += len(r1["skipped_pickup"])
        changed = check_press(tc, cv, start, r1, r2, what)
        stats.writing += bool(changed)
    return r1


def compare_function(tc, cv, prog, name, seeds, stats, variants=((1, 16),), start=None, setup=None, params=None):
    """`variants`: (track, length) or (track, length, mtypes {track: type}).  `setup`: a CardSetup for a
    function that uses the card (each press binds a fresh card and slots on the C VM)."""
    start = start or Start()
    if setup is not None and not hasattr(cv, "bind_card"):
        return                         # an engine without the card binding (verify_vm.py): no card
    for var in variants:               # runs the functions that do not use the pattern, without a binding
        track, length = var[:2]
        mtypes = var[2] if len(var) > 2 else {}
        for seed in seeds:
            cv.reset_state()
            state = None
            for press in range(PRESSES):
                what = "%s [%s%s] seed %d press %d track %d len %d%s" % (
                    name, start.label, "; " + setup.label if setup else "", seed, press, track, length,
                    " types %r" % (mtypes,) if mtypes else "")
                r1 = compare_press(tc, cv, prog, name, seed, state, track, length, start, mtypes, setup,
                                   press, what + ("" if params is None else " params %r" % (params,)), stats, params)
                if r1 is None:
                    break
                state = r1["state"]


# the machine types the pattern-using functions are also run with (a PICKUP host track, PICKUP among
# the tracks of a `trk` list, another type): fewer seeds, they only exercise the gate
TYPE_VARIANTS = ((1, 16, {1: PICKUP, 3: PICKUP}), (5, 64, {2: PICKUP, 5: PICKUP, 7: 1}))


def compare_table(tc, cv, table, data, stats, variants=((1, 16),), only=None, start=None, pattern_only=False,
                  setup=None, seeds=None):
    """Every runnable function of `table` (OTB parsed from `data`) on both VMs.  `pattern_only`: only
    those that use the pattern's bytes.  `setup`: the CardSetup of the functions that use the card (default:
    the small card); `seeds`: instead of the default lists."""
    tc.assertEqual(cv.load(data), 0, "the C VM refuses the table")
    vprog = vm.OtbProgram(table)
    emem, umem, pmem, cmem = {}, {}, {}, {}
    for f in table.funcs:
        if f.nargs or (only and f.name not in only):
            continue
        bad = unsupported(table, f, umem)
        if bad:
            stats.skipped += 1
            stats.skip_ops.update(bad.keys())
            continue
        uses = uses_pattern(table, f, pmem)
        if pattern_only and not uses:
            continue
        if uses and not hasattr(cv, "bind"):                 # (an engine without the pattern binding)
            stats.skipped += 1
            stats.skip_ops["(pattern ops)"] += 1
            continue
        card = uses_card(table, f, cmem)
        if card and not hasattr(cv, "bind_card"):            # (an engine without the card binding)
            stats.skipped += 1
            stats.skip_ops["(card ops)"] += 1
            continue
        sus = ([setup] if setup else default_setups()) if card else [None]
        stats.funcs += 1
        stats.pattern_funcs += uses
        stats.card_funcs += card
        sd = seeds or (SEEDS if T.entropy(table, f, emem) else (0, 20))
        for su in sus:
            compare_function(tc, cv, vprog, f.name, sd, stats, variants, start, su)
            if uses:
                few = (1, 7, 13) if len(sd) > 2 else sd
                compare_function(tc, cv, vprog, f.name, few, stats, TYPE_VARIANTS, start, su)


def compile_sources(sources):
    prog = otype.check_sources(list(sources))
    c = otb.compile_program(prog)
    return prog, c, otb.parse(c.data)


# ---- EXTRA: the generated edge-case script ----------------------------------------------------
INTS = (-32768, -32767, -300, -7, -1, 0, 1, 7, 300, 32767)
SMALL = (-32768, -7, -1, 0, 1, 7, 32767)


def grid_exprs():
    """(prefix expression) for every pure operation at the edges."""
    ex = []
    for op in ("add", "sub", "mul", "div", "mod", "min", "max", "eq", "ne", "lt", "gt", "le", "ge",
               "and", "or", "xor"):
        for a in INTS:
            for b in SMALL:
                ex.append("%s %d %d" % (op, a, b))
    for op in ("abs", "neg", "not", "ez", "nz"):
        for a in INTS + (2, 3, 32766):
            ex.append("%s %d" % (op, a))
    for x in (-32768, -9, -1, 0, 4, 5, 6, 10, 11, 32767):
        for lo, hi in ((0, 0), (1, 5), (5, 1), (-3, 3), (-32768, 32767), (7, 7), (-5, -2)):
            ex.append("lim %d %d %d" % (x, lo, hi))
            ex.append("wrap %d %d %d" % (x, lo, hi))
    for i in (-1, 0, 1, 2, 5, 6, 7, 100):
        ex.append("at elist %d" % i)
        ex.append("at one %d" % i)
    ex.append("len elist")
    ex.append("len one")
    ex.append("add len sixteen at sixteen 16")
    for k in (-1, 0, 1, 3, 5, 8, 9, 40):
        for n in (-1, 0, 1, 5, 8, 16, 32767):
            for s in (1, 4, 8, -2, 17):
                ex.append("er %d %d %d" % (k, n, s))
    for k in (0, 3, 5, 8):
        for n in (0, 5, 8, 16):
            for r in (-3, 0, 2, 9, 40):
                for s in (1, 4, 8, -2, 17):
                    ex.append("er.r %d %d %d %d" % (k, n, r, s))
    for c in (-32768, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 32767):          # dr.v (tables written 04/10): every curve, the wraps
        for st in (-32768, -17, -1, 0, 1, 2, 3, 9, 15, 16, 17, 32767):
            ex.append("dr.v %d %d" % (c, st))
    ex.extend(["on", "off", "amp.vol", "amp.atk", "amp.hold", "amp.rel", "amp.bal", "amp.xvol",
               "lfo.spd 1", "lfo.spd 2", "lfo.spd 3", "lfo.dep 1", "lfo.dep 2", "lfo.dep 3",
               "pb 1", "pb 6", "lfo 3", "amp 6", "fx1 1", "fx2 5", "pb 0", "lfo 7", "amp -1", "fx1 100",
               "fx2 6", "fx2 0", "pat.len", "trk.cur", "step.cur"])
    return ex


EXTRA_HAND = '''
def ex_o1
  o.min -5
  o.max 5
  o.inc 3
  l 1 14: x add x o
  o 100
  y o
  o.wrap 0
  l 1 6: z add z o
  o.min 10
  t o
  o -50
  y o

def ex_o2
  o.max 2
  o.min 8
  l 1 5: x add x o
  o.inc -2
  o.wrap 1
  l 1 9: y add y o
  o.min -32768
  o.max 32767
  o.inc 32767
  l 1 4: z add z o
  o.inc 0
  o.wrap 0
  t o
  o.wrap 5
  o 3
  x add x o

def ex_o3
  a 3
  o a
  o.inc -1
  o.wrap 1
  o.max 0
  o.min 0
  l 1 3: x add x o
  o 9
  y o

def ex_drunk1
  l 1 40: x add x drunk
  drunk.wrap 1
  drunk.min 2
  drunk.max 4
  l 1 40: y add y drunk
  drunk 100
  z drunk
  drunk.max 1
  l 1 20: t add t drunk
  drunk 0
  drunk.wrap 0
  drunk.min 0
  drunk.max 0
  x drunk
  drunk.max 127
  drunk -5
  y drunk
  drunk.seed 77
  l 1 5: z add z drunk

def ex_flip
  x flip
  y flip
  flip 7
  z flip
  flip 0
  t flip
  flip 1
  x add x flip
  flip -3
  y flip

def ex_every
  l 1 10:
    every 3: x add x 1
    skip 4: y add y 1
    every 0: z add z 1
    every -2: t add t 1
    skip 0: z add z 10
    skip -1: t add t 10
    every 1: x add x 100
    skip 1: y add y 100
  l 1 8: every 2: skip 3: x add x 1
  l 1 6: every 3: every 2: y add y 1
  every 2: z add z 1
  skip 2: t add t 1

def ex_prob
  l 1 30:
    prob -5: x add x 1
    prob 0: y add y 1
    prob 100: z add z 1
    prob 150: t add t 1
    prob 50: x add x 100
    prob rnd 100: y add y 100

def ex_rand
  x rnd 0
  y rnd -4
  z rnd 32767
  t rrand -32768 32767
  a rrand 5 1
  x add x a
  b pick one
  c pick sixteen
  y add y add b c
  a 150
  d chance a
  z add z d
  a -3
  d chance a
  t add t d
  d chance 0
  x add x d
  d chance 100
  y add y d
  l 1 30: z add z toss
  l 1 30: t add t chance 33
  x rrand 3 3
  y rnd 1
  z pick elist
  t ? gt rnd 10 5 rnd 10 rnd 20
  x ? 0 rnd 10 7
  y add rnd 5 rnd 5
  z and rnd 2 rnd 2
  t or toss toss
  x xor toss toss

def ex_seed
  l 1 3:
    seed i
    x add x rnd 100
  y rnd 100
  rand.seed 5
  z rnd 100
  toss.seed -6
  t add t toss
  prob.seed 7
  prob 50: x add x 1
  drunk.seed 8
  y add y drunk
  file.seed 9
  seed 32767
  z add z rnd 1000
  seed 1
  t add t rnd 1000
  seed -1
  x add x rnd 1000
  seed 0
  y add y rnd 1000

def ex_seedloop
  l 1 2000:
    seed i
    x mod add x rnd 100 30000
  l 1 700:
    rand.seed i
    y mod add y rnd 100 30000
    toss.seed i
    prob.seed i
    drunk.seed i
    file.seed i
  l 1 50:
    each:
      seed i
      z mod add z rnd 100 30000
    seed j

def ex_seedblock
  seed 4
  if 1:
    seed 3
    x add x rnd 1000
  y add y rnd 1000
  trk 1 2:
    seed 5
    z add z rnd 100
  t add t rnd 100
  steps 1 4
  each:
    rand.seed i
    x add x rnd 100
  y add y rnd 100
  l 1 3:
    toss.seed i
    z add z toss
  t add t toss
  if 1:
    drunk.seed 5
    if 1:
      prob.seed 6
      prob 50: x add x 1
    prob 50: y add y 1
    z add z drunk
  t add t drunk

def ex_deep
  seed 1
  if 1:
    seed 2
    if 1:
      seed 3
      if 1:
        seed 4
        if 1:
          seed 5
          if 1:
            seed 6
            if 1:
              seed 7
              if 1:
                seed 8
                x add x rnd 1000
              y add y rnd 1000
            z add z rnd 1000
          t add t rnd 1000
        x add x rnd 1000
      y add y rnd 1000
    z add z rnd 1000
  t add t rnd 1000

def ex_cs | n
  seed n
  ret rnd 1000

def ex_callseed
  seed 5
  x ex_cs | 1
  y rnd 1000
  z ex_cs | 2
  t rnd 1000
  ex_cs | 3
  x add x rnd 1000

def ex_early | n
  l 1 5:
    if eq i n: ret add i 100
  ret 0

def ex_args | p q
  a add p q
  b sub p q
  ret add a b

def ex_calls
  x ex_early | 3
  y ex_early | 9
  z ex_args | 7 -7
  t ex_args | 32767 32767
  x add x ex_early | ex_args | 1 1
  ex_args | 1 2

def ex_stop | n
  if gt n 1: stop
  x add x 1
  ret 9

def ex_stops
  ex_stop | 2
  ex_stop | 0
  y ex_stop | 1
  z ex_stop | 5

def ex_loops
  trk 1 2 3:
    l 1 4:
      l 1 4:
        if eq j 3: break
        x add x add i j
      if eq i 2: break
      y add y 1
    z add z 1
    each:
      if eq i 3: break
      t add t i
  l 5 1: x add x i
  l 3 3: y add y i
  l -2 2: z add z i
  l 2 -2: t add t i
  steps 3 9:
    each:
      l 1 3:
        if eq i 2: break
        x add x i
      y add y i
  x add x add i j
  l 1 3:
    l 1 3:
      l 1 3:
        a add i add j a
        if eq a 4: break
        y add y a
    z add z i
  t add t j

def ex_passes
  trk 1 2:
    x add x trk.cur
    trk 3 4:
      y add y trk.cur
      steps 2 6
      each: z add z i
    t add t trk.cur
  trk 1 1 2 2 8:
    x add x trk.cur
  trk cur:
    y add y trk.cur
  trk all:
    z add z trk.cur
  trk 5
  t add t trk.cur
  x add x step.cur
  step 7
  y add y step.cur
  steps all
  each:
    z add z step.cur
  steps 3 3
  each: t add t step.cur

def ex_oor
  a 9
  b 0
  trk a:
    x add x trk.cur
  trk 1 a 2 b 3:
    y add y trk.cur
  trk b a:
    z add z 1
  t add t 1

def ex_oor_steps
  a 70
  b 0
  steps a 3
  x add x 1
  steps 3 a
  steps b 3
  steps 5 2
  y step.cur
  step a
  step b
  step 64
  z step.cur
  step 1
  steps 64 64
  each: t add t step.cur
  a 7
  x pb a
  y lfo b
  z fx1 a
  t amp -1

def ex_oor_many
  l 1 100:
    a pb 9
    x add x i

def ex_pages
  x pb 1
  y lfo 6
  z amp 3
  t fx1 4
  a fx2 5
  x add x a
  y add y add amp.vol lfo.dep 3
  z add z add pb 2 fx2 6

def ex_len
  x pat.len
  y trk.cur
  steps all
  each: z add z i
  t step.cur

def ex_param
  menu "EX" >> "PARAM"
  param p 5 1 9
  param q -3 -10 10
  x p
  l 1 p: y add y i
  z add q p

def ex_c1
  x ex_c2 | 1
def ex_c2 | n
  ret add n ex_c3 | add n 1
def ex_c3 | n
  ret add n ex_c4 | add n 1
def ex_c4 | n
  ret add n ex_c5 | add n 1
def ex_c5 | n
  ret add n ex_c6 | add n 1
def ex_c6 | n
  ret add n ex_c7 | add n 1
def ex_c7 | n
  ret add n ex_c8 | add n 1
def ex_c8 | n
  seed n
  ret add n rnd 100

def ex_locals
  a 1
  b 2
  c add a b
  d mul c c
  x add d a
  y sub b d
  z add c b
  t neg x
  a add a 5
  x add x a

def ex_globals
  x 32767
  y -32768
  z add x 1
  t sub y 1
  x add x t
  y add y z
  z mul x y
  t div x y

def ex_if
  a 0
  if a: x 1
  elif 1: x 2
  else: x 3
  if 1: y 1
  else: y 2
  if 0: z 1
  elif 0: z 2
  elif a: z 3
  else: t 4
  a rnd 3
  if eq a 0: x add x 10
  elif eq a 1: x add x 20
  elif rnd 2: y add y 5
  else: z add z 5
'''


EXTRA_HEAD = ["otype 1",
              "const elist {-5 7 100 -32768 32767 0}",
              "const one {42}",
              "const sixteen {1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16}"]


def slot_chain(nfunc=8, depth=7):
    """8 nested calls, each 7 scopes deep (the checker's limit is 8 blocks) and each scope seeded:
    ~290 stream slots live at once (ex_slot1 .. ex_slot8); then the innermost level reseeds in a loop,
    which must reuse slots. (The full 640 are reached by hand-written bytecode: TestHandWritten.)"""
    out = []
    for k in range(1, nfunc + 1):
        lines = ["def ex_slot%d" % k]
        pad = "  "
        for lvl in range(depth):
            lines.append(pad + "if 1:")
            pad += "  "
            lines.append(pad + "seed %d" % (k * 100 + lvl))
            lines.append(pad + "x mod add x rnd 1000 30011")
        if k < nfunc:
            lines.append(pad + "ex_slot%d" % (k + 1))
        lines.append(pad + "l 1 40:")
        lines.append(pad + "  seed add i %d" % (k * 7))
        lines.append(pad + "  y mod add y rnd 1000 30011")
        lines.append(pad + "z add z drunk")
        out.append("\n".join(lines) + "\n")
    return "\n" + "\n".join(out)


def extra_sources():
    """The EXTRA scripts as lists of (label, text): a table holds at most 255 functions, so the
    generated grid is cut into several programs, and the hand-written functions form the last."""
    ex = grid_exprs()
    funcs = []
    for k in range(0, len(ex), 4):
        grp = ex[k:k + 4]
        funcs.append("def ex_g%d\n" % (k // 4) + "".join("  %s %s\n" % ("xyzt"[i], e) for i, e in enumerate(grp)))
    out = []
    for k in range(0, len(funcs), 200):
        out.append([("EXTRA%d.OTY" % (k // 200), "\n".join(EXTRA_HEAD + funcs[k:k + 200]))])
    out.append([("EXTRAH.OTY", "\n".join(EXTRA_HEAD) + "\n" + EXTRA_HAND + slot_chain())])
    return out


# ---- patching tables --------------------------------------------------------------------------
def enc(name, *ops):
    ins = otb.BY_NAME[name]
    import struct
    return struct.pack(">B" + ins.fmt, ins.code, *ops)


def patched(data, table, fname, code, declared=None):
    """The table with the first bytes of fname's code replaced by `code` (hand-written bytecode);
    `declared` = (max_stack, max_ctx, max_ctl) rewrites the function's declared depths."""
    f = table.fmap[fname]
    assert len(code) <= len(f.code), (len(code), len(f.code))
    d = bytearray(data)
    if declared:
        e = table.ftab_off + 54 * f.index
        d[e + 35:e + 38] = bytes(declared)
    start = table.code_off + f.code_off
    d[start:start + len(code)] = code
    return bytes(d)


# ---- the tests --------------------------------------------------------------------------------
_PARTS = {}
STATS = {}


class TestWritableOffsets(unittest.TestCase):
    def test_swing_amount_is_the_only_new_metadata_byte(self):
        self.assertTrue(writable(0x52))
        self.assertFalse(any(writable(i) for i in (0x50, 0x51, 0x53, 0x58)))


class Base(unittest.TestCase):
    cv = None

    @classmethod
    def setUpClass(cls):
        Base.cv = cvm.CVM(build_lib())

    def part(self, name, fn):
        """Run one part of the corpus once per process (the coverage test needs them all)."""
        if name not in _PARTS:
            st = Stats()
            fn(self, st)
            _PARTS[name] = st
        return _PARTS[name]

    def run_sources(self, sources, st, variants=((1, 16),), all_present=False):
        _prog, c, table = compile_sources(sources)
        if all_present:                         # a refused function would silently not be tested
            import re
            defs = {m.upper() for _l, text in sources for m in re.findall(r"^def (\w+)", text, re.M)}
            self.assertEqual(sorted(defs - set(table.fmap)), [], "refused by the checker / compiler")
        compare_table(self, self.cv, table, c.data, st, variants)


class TestMsg(Base):
    """msg: the sim, the Python VM and the C VM agree on the text of the last msg of a run (strings, signed 16-bit
    values, several msg calls where the last wins, none = ""), and the checker / compiler refuse a worst case that does
    not fit OTC_MSG_MAX."""

    SRC = """\
def one
  msg "BANK" x "PAT" y
def negs
  x -32768
  y 32767
  msg "N" x y
def last
  msg "FIRST" 1
  msg "SECOND" 2
def none
  tr.add 1
def mixed | q
  msg q "A" q
def callmsg
  mixed | 7
def loop
  l 1 3:
    msg "I" i
def early
  msg "KEEP"
  stop
  msg "NOT"
def skipped
  msg "A"
  if 0:
    msg "B"
def empty
  msg "" ""
def maxlen
  msg "ABCDEFGHIJKLMNOPQRSTUVWXYZ123"
def num6
  x -32768
  msg "ABCDEFGHIJKLMNOPQRSTU" x
"""

    def test_agreement_and_exact_text(self):
        prog, c, table = compile_sources([("M.OTY", self.SRC)])
        self.assertEqual(self.cv.load(c.data), 0)
        want = {"ONE": "BANK 0 PAT 0", "NEGS": "N -32768 32767", "LAST": "SECOND 2", "NONE": "", "CALLMSG": "7 A 7",
                "LOOP": "I 3", "EARLY": "KEEP", "SKIPPED": "A", "EMPTY": " ", "MAXLEN": "ABCDEFGHIJKLMNOPQRSTUVWXYZ123",
                "NUM6": "ABCDEFGHIJKLMNOPQRSTU -32768"}
        for name, text in want.items():
            self.assertEqual(self.cv.load(c.data), 0)
            Start().bind(self.cv, {})
            r1 = sim.simulate(prog, name, seed=1)
            self.assertEqual((r1.model.msgs or [""])[-1], text, "sim " + name)
            self.assertEqual(vm.simulate_otb(table, name, seed=1).model.msgs[-1:] or [""], [text], "vm " + name)
            self.assertEqual(self.cv.run(name, 1, 1, 16)["globals"] is not None, True)
            self.assertEqual(self.cv.msg(), text, "C " + name)

    def test_text_is_per_run(self):
        _prog, c, _table = compile_sources([("M.OTY", self.SRC)])
        self.assertEqual(self.cv.load(c.data), 0)
        self.cv.run("one", 1, 1, 16)
        self.assertEqual(self.cv.msg(), "BANK 0 PAT 0")
        self.cv.run("none", 1, 1, 16)
        self.assertEqual(self.cv.msg(), "")

    def test_the_corpus_functions_agree(self):
        self.run_sources([("M.OTY", self.SRC)], Stats(), all_present=True)

    def test_limit_is_one_constant(self):
        self.assertEqual(otype.MAX_MSG, O.MAX_MSG)
        hdr = open(os.path.join(CSRC, "otc_opreg.h")).read()
        self.assertIn("#define OTC_L_MAX_MSG %d\n" % O.MAX_MSG, hdr)
        self.assertIn("#define OTC_MSG_MAX %d " % O.MAX_MSG, open(os.path.join(CSRC, "otc_ops.h")).read())

    def test_checker_refuses_a_worst_case_over_the_limit(self):
        n = O.MAX_MSG
        for ok, line in ((True, 'msg "%s"' % ("A" * n)), (False, 'msg "%s"' % ("A" * (n + 1))),
                         (True, 'msg "%s" x' % ("A" * (n - 7))), (False, 'msg "%s" x' % ("A" * (n - 6))),
                         (False, 'msg "A" "A" "A" "A" "A" "A" "A" "A" "A"')):
            diags = otype.check_sources([("L.OTY", "def f\n  %s\n" % line)]).diags
            errs = [d.msg for d in diags if d.level == "error"]
            self.assertEqual(errs == [], ok, (line, errs))
        errs = [d.msg for d in otype.check_sources([("L.OTY", 'def f\n  msg "%s" x\n' % ("A" * n))]).diags]
        self.assertIn("msg is too long for the screen: %d characters in the worst case (a number counts as 6, items are "
                      "separated by a space), at most %d fit" % (n + 7, n), errs)


class TestHeader(unittest.TestCase):
    def test_ops_header_is_up_to_date(self):
        with open(gen_ops_c.HEADER) as fh:
            self.assertEqual(fh.read(), gen_ops_c.render(),
                             "otc_ops.h is stale: python3 modules/otype/gen_ops_c.py")

    def test_unit_includes_probe_and_vm(self):
        with open(os.path.join(CSRC, "otype_unit.c")) as fh:
            text = fh.read()
        self.assertIn('#include "probe.c"', text)
        self.assertIn('#include "vm.c"', text)

    def test_supported_set_is_what_the_task_says(self):
        sup = {o.name for o in O.ALL_OPS if gen_ops_c.supported(o)}
        for n in ("ADD", "RND", "O", "DRUNK.WRAP", "SEED", "RAND.SEED", "ER.R", "PB", "AMP.VOL", "TRK",
                  "STEPS", "STEP", "STEP.CUR", "TRK.CUR", "PAT.LEN", "?",
                  "TR.ADD", "TR.DEL", "TR.FLIP", "TR.CLR", "HAS.TRIG", "HAS.LOCK", "HAS.SMP", "IS.EMPTY",
                  "TRK.TYPE", "SMP.SET", "SMP.GET", "SMP.DEL", "SMP.RND", "SMP.CLR",
                  # the p-locks and the trigless locks
                  "PL.SET", "PL.GET", "PL.DEL", "PL.RND", "PL.VARY", "PL.CLR", "TL.ADD", "TL.FILL",
                  # the part
                  "PAR.SET", "LFO.DEST", "FX.TYPE", "SCN.CLR", "TRK.INIT",
                  # the trig word
                  "COND.SET", "COND.RND", "MICRO.SET", "MICRO.RND", "RATCH.SET",
                  # the card / slot operations (they need the workspace: TestCardBinding)
                  "DIR", "EXT", "MATCH", "FILE.LEN", "FILE.RND", "FILE.PICK", "FILE.AT", "STATIC.LOAD", "STATIC.FREE",
                  "STATIC.FIRSTFREE", "STATIC.CLR", "SLOTS.CLR", "SLOTS.ADD", "SLOTS.RND"):
            self.assertIn(n, sup)
        self.assertIn("DR.V", sup)                     # its tables are written (04/10)
        self.assertIn("MSG", sup)                      # the end-box text (the msg instruction), 2026-10-12
        for n in ("TRK.SLOT", "TEMPO"):
            self.assertIn(n, sup)
        self.assertEqual({o.name for o in O.ALL_OPS if o.level == "vm"} - sup, set())
        self.assertEqual(sup - {o.name for o in O.ALL_OPS if o.level == "vm"},
                         {"TRK.CUR", "PAT.LEN", "TR.ADD", "TR.DEL", "TR.FLIP", "TR.CLR", "HAS.TRIG",
                          "HAS.LOCK", "HAS.SMP", "IS.EMPTY", "TRK.TYPE", "SMP.SET", "SMP.GET", "SMP.DEL",
                          "SMP.RND", "SMP.CLR", "PL.SET", "PL.GET", "PL.DEL", "PL.RND", "PL.VARY", "PL.CLR",
                          "TL.ADD", "TL.FILL", "PAR.SET", "LFO.DEST", "FX.TYPE",
                          "SCN.CLR", "TRK.INIT", "TRK.SLOT", "TEMPO", "COND.SET", "COND.RND", "MICRO.SET", "MICRO.RND", "RATCH.SET",
                          "DIR", "STATIC.LOAD", "STATIC.FREE", "STATIC.FIRSTFREE",
                          "STATIC.CLR"}
                         | set(gen_ops_c.V1_EXTRA)
                         | set(gen_ops_c.AUDIO_EXTRA))


class TestCorpus(Base):
    def test_examples(self):
        def go(tc, st):
            prog = otype.check_files(T.EXAMPLES)
            c = otb.compile_program(prog)
            compare_table(tc, tc.cv, otb.parse(c.data), c.data, st)
        self.part("examples", go)

    def test_kitchen_and_per_op_scripts(self):
        def go(tc, st):
            tc.run_sources(T.kitchen_sources(), st)
        st = self.part("kitchen", go)
        self.assertGreater(st.funcs, 0)

    def test_harvested_scripts(self):
        def go(tc, st):
            sources = T.harvest()
            tc.assertGreater(len(sources), 300)
            for srcs in sources:
                tc.run_sources(srcs, st)
        st = self.part("harvest", go)
        self.assertGreater(st.funcs, 200)
        self.assertGreater(st.runs, 1500)

    def test_extra_edges(self):
        def go(tc, st):
            for srcs in extra_sources():
                tc.run_sources(srcs, st, variants=((1, 16), (5, 64), (8, 7), (3, 1)), all_present=True)
        st = self.part("extra", go)
        self.assertGreater(st.funcs, 300)
        self.assertEqual(st.skipped, 0, "every EXTRA function must be runnable by the C VM")

    def test_zz_coverage(self):
        self.test_examples()
        self.test_kitchen_and_per_op_scripts()
        self.test_harvested_scripts()
        self.test_extra_edges()
        funcs = sum(s.funcs for s in _PARTS.values())
        runs = sum(s.runs for s in _PARTS.values())
        skipped = sum(s.skipped for s in _PARTS.values())
        errors = sum(s.errors for s in _PARTS.values())
        skip_ops = sum((s.skip_ops for s in _PARTS.values()), collections.Counter())
        print("\n[cvm] functions compared %d, runs %d (error runs %d), skipped %d (unsupported)"
              % (funcs, runs, errors, skipped))
        print("[cvm] of them %d use the pattern (tr.* smp.* has.* ...); presses that wrote a track %d, "
              "no-trig skips %d, pickup skips %d"
              % (sum(s.pattern_funcs for s in _PARTS.values()), sum(s.writing for s in _PARTS.values()),
                 sum(s.skips for s in _PARTS.values()), sum(s.pickups for s in _PARTS.values())))
        print("[cvm] of them %d use the card / slots: runs %d, with dir warnings %d, refusals %d, "
              "CARD BUSY %d, presses that left a slot loaded %d"
              % (sum(s.card_funcs for s in _PARTS.values()), sum(s.card_runs for s in _PARTS.values()),
                 sum(s.warned for s in _PARTS.values()), sum(s.refused for s in _PARTS.values()),
                 sum(s.busy for s in _PARTS.values()), sum(s.loaded for s in _PARTS.values())))
        print("[cvm] top unsupported ops among the skipped: %s"
              % ", ".join("%s %d" % kv for kv in skip_ops.most_common(12)))
        for k, s in _PARTS.items():
            print("[cvm]   %-9s functions %4d runs %6d skipped %4d" % (k, s.funcs, s.runs, s.skipped))
        # supported operations never exercised by any run
        seen = {(a, b) for a, b in {(o[0], o[1]) for o in COV["op"]} | {(o[0], o[1]) for o in COV["opv"]}
                | {(o[0], o[1]) for o in COV["set"]}}
        sup = [o for o in O.ALL_OPS if gen_ops_c.supported(o)]
        missing = sorted(o.shown for o in sup if o.id not in seen and o.name != "?")
        print("[cvm] supported ops never exercised: %s" % (", ".join(missing) or "none"))
        ins_all = {i.name for i in otb.INSTRS}
        print("[cvm] instructions never executed: %s"
              % (", ".join(sorted(ins_all - COV["ins"])) or "none"))
        settable = {o.name for o in sup if o.settable and o.name != "TRK.TYPE"}
        set_seen = {otb.REG[(a, b)].name for a, b, *_ in COV["set"]}
        self.assertEqual(settable - set_seen, set(), "state setters (`set`) not exercised")
        self.assertEqual(missing, [], "supported operations never executed by the corpus")
        forms = {(otb.REG[(a, b)].name, f) for a, b, f in COV["opv"]}
        for want in (("TRK", otb.KW_CUR), ("TRK", otb.KW_ALL), ("TRK", 1), ("TRK", 2), ("STEPS", otb.STEPS_ALL),
                     ("STEPS", otb.STEPS_RANGE), ("STEPS", otb.STEPS_PRED)):
            self.assertIn(want, forms)
        for opn in ("pb", "lfo", "amp", "fx1", "fx2", "step", "trk", "steps"):
            self.assertIn(opn, COV["oor"], "the out-of-range path of %s is not exercised" % opn)
        self.assertGreater(funcs, 700)
        self.assertGreater(runs, 20000)
        self.assertGreater(sum(s.skips for s in _PARTS.values()), 0, "no run skipped a write for lack of a trig")
        self.assertGreater(sum(s.pickups for s in _PARTS.values()), 0, "no run met a PICKUP track")


class TestHandWritten(Base):
    """Patched tables: raw bytecode that the compiler never emits."""

    SRC = [("H.OTY", "def hp\n" + "  x add x 1\n" * 12 + "def hq\n  x 5\n")]

    @classmethod
    def setUpClass(cls):
        Base.setUpClass()
        cls.prog, cls.c, cls.table = compile_sources(cls.SRC)

    def both(self, code, fname="HP"):
        """(Python verdict, C result) of a patched table: "ok" / "table" / "cost"."""
        data = patched(self.c.data, self.table, fname, code)
        try:
            t = otb.parse(data)
            vm.simulate_otb(t, fname, seed=1)
            py = "ok"
        except sim.RunError as e:
            py = "cost" if "run too long" in str(e) else "table"
        except (otb.OtbError, IndexError):       # (the reference raises a bare IndexError for lda / ldp / every)
            py = "table"
        self.assertEqual(self.cv.load(data), 0)
        return py, self.cv.run(fname, 1)

    def table_error(self, code, fname="HP"):
        py, c = self.both(code, fname)
        self.assertEqual(py, "table", "the reference VM must refuse it too")
        self.assertEqual(c, ("error", cvm.E_TABLE))

    def test_corrupt_tables_are_refused_like_the_reference(self):
        stop = enc("stop")
        cases = {
            "unknown opcode": b"\x99" + stop,
            "pop of an empty stack": enc("drop") + stop,
            "st of an empty stack": enc("st", 4) + stop,
            "break outside a loop": enc("break") + stop,
            "unscope of the own context": enc("unscope") + stop,
            "lnext without its frame": enc("lnext", 0) + stop,
            "enext without its frame": enc("enext", 0) + stop,
            "pnext without its frame": enc("pnext", 0) + stop,
            "jump out of the code": enc("jmp", 0xF000) + stop,
            "unknown operation": enc("push8", 1) + enc("op", 99, 1) + stop,
            "lazy ? as a plain op": enc("push8", 1) * 3 + enc("op", 0, 0x18) + stop,
            "trk as a plain op": enc("push8", 1) + enc("op", 1, 1) + stop,
            "bad pool offset": enc("push16", 3) + enc("push8", 1) + enc("op", 0, 0x1E) + stop,
            "stack beyond the declared": enc("push8", 1) * 4 + stop,
            "scopes beyond the declared": enc("scope") * 3 + stop,
            "ctl beyond the declared": enc("push8", 1) + enc("push8", 2) + enc("lbeg", 0) + stop,
            "call of a missing function": enc("call", 200) + stop,
            "call depth beyond 8": enc("call", 0) + stop,
            "lda without an argument": enc("lda", 0) + stop,
            "ldp without a param": enc("ldp", 0) + stop,
            "every without a counter": enc("push8", 1) + enc("every", 0, 0) + stop,
            "opv with an unknown form": enc("opv", 1, 2, 5) + stop,
        }
        for what, code in cases.items():
            with self.subTest(what):
                py, c = self.both(code)
                self.assertEqual(py, "table", what + ": the reference VM must refuse it")
                self.assertEqual(c, ("error", cvm.E_TABLE), what)
        # hq's code is 5 bytes: push8 5 / st x / stop
        for what, code in (("falls off the end of the code", enc("push8", 1) + enc("drop") + enc("push8", 0)),
                           ("code ends inside an instruction", enc("push8", 1) + enc("drop") + b"\x11\x00")):
            with self.subTest(what):
                py, c = self.both(code, "HQ")
                self.assertEqual((py, c), ("table", ("error", cvm.E_TABLE)), what)

    def test_pushc_pushk_and_the_other_constants(self):
        # pushc is only emitted for trig conditions (unsupported ops): run it by hand-patching
        code = (enc("pushc", 33) + enc("st", 4) + enc("push16", -300) + enc("st", 5) + enc("pushk", 0)
                + enc("drop") + enc("push8", -128) + enc("st", 6) + enc("stop"))
        data = patched(self.c.data, self.table, "HP", code)
        r1 = sim.result_json(vm.simulate_otb(otb.parse(data), "HP", seed=1))
        self.assertEqual(self.cv.load(data), 0)
        r2 = self.cv.run("HP", 1)
        self.assertEqual((r1["globals"], r1["cost"]), (r2["globals"], r2["cost"]))
        self.assertEqual(r2["globals"], {"x": 33, "y": -300, "z": -128, "t": 0})

    def test_all_stream_slots_live_at_once(self):
        """8 frames x 16 scopes, 15 scoped seeds per frame (605 of the 640 slots live) and a reseed at
        the deepest point: hand-written bytecode, since the checker stops at 8 nested blocks."""
        prog, c, table = compile_sources([("S.OTY", "def hs\n" + "  x add x 1\n" * 60)])
        body = b""
        for n in range(15):
            body += (enc("scope") + enc("push8", n + 1) + enc("op", 1, 4)            # scope; seed n+1
                     + enc("ld", 5) + enc("push8", 100) + enc("op", 0, 0x19) + enc("op", 0, 1) + enc("st", 5))
        body += (enc("ld", 4) + enc("push8", 1) + enc("op", 0, 1) + enc("st", 4)        # x = x + 1
                 + enc("ld", 4) + enc("push8", 8) + enc("op", 0, 0x0F))                 # x < 8 ?
        jz_at = len(body)
        end = jz_at + 3 + 2                                                            # after jz, call
        body += enc("jz", end) + enc("call", 0)
        body += (enc("push8", 77) + enc("op", 1, 4)                                      # reseed at the end
                 + enc("ld", 6) + enc("push16", 1000) + enc("op", 0, 0x19) + enc("op", 0, 1) + enc("st", 6))
        body += enc("stop")
        data = patched(c.data, table, "HS", body, declared=(4, 16, 0))
        t = otb.parse(data)
        r1 = sim.result_json(vm.simulate_otb(t, "HS", seed=3))
        self.assertEqual(self.cv.load(data), 0)
        r2 = self.cv.run("hs", 3)
        self.assertNotEqual(r2[0] if isinstance(r2, tuple) else None, "error", r2)
        for k in ("cost", "draws", "globals"):
            self.assertEqual(r1[k], r2[k], k)
        self.assertEqual(r2["draws"]["rand"], 8 * 15 + 8)
        self.assertGreater(r2["executed"], 8 * 15 * 8)

    def test_unsupported_is_reported_not_run(self):
        stop = enc("stop")
        for what, code in (("tr.add", enc("push8", 1) + enc("op", 3, 1) + stop),
                           ("dir", enc("push8", 1) + enc("dir", 0) + stop),
                           ("ext", enc("ext", 0) + stop),
                           ("match", enc("match", 0) + stop),
                           ("steps <pred>", enc("push16", 0x0210) + enc("opv", 1, 2, 1) + stop),
                           ("file.len", enc("op", 11, 4) + stop)):
            with self.subTest(what):
                data = patched(self.c.data, self.table, "HP", code)
                self.assertEqual(self.cv.load(data), 0)
                self.assertEqual(self.cv.run("HP", 1), ("error", cvm.E_UNSUPPORTED))

    def test_instruction_limit_stops_a_looping_table(self):
        # a backward jmp is never emitted; C stops it (the Python VM would run for ever)
        data = patched(self.c.data, self.table, "HP", enc("jmp", 0))
        self.assertEqual(self.cv.load(data), 0)
        self.assertEqual(self.cv.run("HP", 1), ("error", cvm.E_COST))

    def test_the_real_cost_limit(self):
        src = "def big\n  l 1 3000: l 1 3000: l 1 3000: a pb 1\n"
        _prog, c, _t = compile_sources([("B.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        self.assertEqual(self.cv.run("big", 1), ("error", cvm.E_COST))

    def test_load_refusals(self):
        d = bytearray(self.c.data)
        for what, bad in (("magic", b"XTB" + bytes(d[3:])), ("format", bytes(d[:3]) + b"\x02" + bytes(d[4:])),
                          ("truncated", bytes(d[:-4])), ("short", bytes(d[:20])), ("empty", b"")):
            with self.subTest(what):
                self.assertEqual(self.cv.lib.otc_vm_load(self.cv.ptr, bad, len(bad)), cvm.E_TABLE)
        # a declared depth above a cap: refused (E_CAPS), not run
        f = self.table.fmap["HP"]
        e = self.table.ftab_off + 54 * f.index
        for off, what in ((35, "max_stack"), (36, "max_ctx"), (37, "max_ctl")):
            d2 = bytearray(d)
            d2[e + off] = 17
            self.assertEqual(self.cv.load(bytes(d2)), cvm.E_CAPS, what)
        self.assertEqual(self.cv.load(bytes(d)), 0)

    def test_run_argument_errors(self):
        self.assertEqual(self.cv.load(self.c.data), 0)
        L, p = self.cv.lib, self.cv.ptr
        self.assertEqual(L.otc_vm_run(p, 9, 1, 1, 16), cvm.E_ARG)        # no such function
        self.assertEqual(L.otc_vm_run(p, 0, 1, 0, 16), cvm.E_ARG)        # track 0
        self.assertEqual(L.otc_vm_run(p, 0, 1, 9, 16), cvm.E_ARG)        # track 9
        self.assertEqual(L.otc_vm_run(p, 0, 1, 1, 16), 0)


class TestEdgesExact(Base):
    """Exact values (not only agreement with Python) for rules the spec states."""

    def run1(self, src, name, **kw):
        _prog, c, table = compile_sources([("E.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        return self.cv.run(name, kw.get("seed", 0), kw.get("track", 1), kw.get("length", 16))

    def test_floor_modulo_and_truncating_division(self):
        r = self.run1("def f\n  x mod -7 3\n  y div -7 2\n  z mod 7 -3\n  t div 5 0\n", "f")
        self.assertEqual(r["globals"], {"x": 2, "y": -3, "z": 1, "t": 0})

    def test_saturation(self):
        r = self.run1("def f\n  x add 32767 1\n  y sub -32768 1\n  z mul 300 300\n  t div -32768 -1\n", "f")
        self.assertEqual(r["globals"], {"x": 32767, "y": -32768, "z": 32767, "t": 32767})

    def test_prng_vectors(self):
        # spec 5.3.2: rnd 65536 would be the raw top 16 bits; check the first two draws of stream rand
        st = sim.Stream("RAND", 1)
        want = [((st.next() >> 16) * 1000) >> 16 for _ in range(2)]
        r = self.run1("def f\n  x rnd 1000\n  y rnd 1000\n", "f", seed=1)
        self.assertEqual([r["globals"]["x"], r["globals"]["y"]], want)
        self.assertEqual(r["draws"]["rand"], 2)

    def test_state_persists_between_presses_and_resets(self):
        _prog, c, table = compile_sources([("E.OTY", "def f\n  x add x 1\n  a o\n  y o\n")])
        self.assertEqual(self.cv.load(c.data), 0)
        r1 = self.cv.run("f", 0)
        r2 = self.cv.run("f", 0)
        self.assertEqual((r1["globals"]["x"], r2["globals"]["x"], r2["globals"]["y"]), (1, 2, 3))
        self.cv.reset_state()
        self.assertEqual(self.cv.run("f", 0)["globals"]["x"], 1)
        self.assertEqual(self.cv.run("f", 0)["state"]["funcs"]["f"]["o"], 4)

    def test_untouched_function_has_no_state(self):
        r = self.run1("def f\n  x 1\ndef g\n  o.max 5\n", "f")
        self.assertEqual(r["state"]["funcs"], {})
        r = self.run1("def f\n  x 1\ndef g\n  o.max 5\n", "g")
        self.assertEqual(list(r["state"]["funcs"]), ["g"])

    def test_cost_limit_boundary_matches(self):
        # the C limit built small (OTC_MAX_COST) against sim.MAX_COST patched to the same number
        limit = 2000
        cv = cvm.CVM(build_cost_lib(limit))
        old = sim.MAX_COST
        sim.MAX_COST = limit
        try:
            src = "def okf\n  l 1 %d: a pb 1\ndef bad\n  l 1 %d: a pb 1\n" % (limit, limit + 1)
            prog, c, table = compile_sources([("C.OTY", src)])
            self.assertEqual(cv.load(c.data), 0)
            r1 = sim.result_json(vm.simulate_otb(table, "okf", seed=1))
            r2 = cv.run("okf", 1)
            self.assertEqual((r1["cost"], r2["cost"]), (limit, limit))
            with self.assertRaises(sim.RunError):
                vm.simulate_otb(table, "bad", seed=1)
            self.assertEqual(cv.run("bad", 1), ("error", cvm.E_COST))
        finally:
            sim.MAX_COST = old


class TestDecodeBigEndian(Base):
    def test_record_decodes_from_a_big_endian_dump(self):
        import struct
        _prog, c, table = compile_sources([("E.OTY", "def f\n  x rnd 9\n  o.max 7\n  l 1 3: every 2: o\n")])
        self.assertEqual(self.cv.load(c.data), 0)
        r = self.cv.run("f", 4)
        n = self.cv.lib.otc_vm_export(self.cv.ptr, self.cv.out, len(self.cv.out))
        be = struct.pack(">%di" % n, *self.cv.out[:n])
        self.assertEqual(cvm.decode_export(cvm.words_from_be(be), table), r)


# ---- the pattern's bytes ----------------------------------------------------------------
PATTERN_SRC = """def clr
  tr.clr
def clr_all
  trk all
  tr.clr
def del_some
  l 1 16: if mod i 3: tr.del i
def flip_some
  trk 1 2
  l 1 12: tr.flip i
def add_some
  l 1 16: if mod i 2: tr.add i
def set_smp
  l 1 16: smp.set i add i 3
def set_smp_trk
  trk 1 3
  l 1 20: smp.set i 7
def rnd_smp
  smp.rnd 5 20
def rnd_all
  trk all
  steps 3 11
  smp.rnd 1 128
def del_smp
  l 1 8: smp.del i
def clr_smp
  trk all
  smp.clr
def lock_trigged
  steps has.trig:
    each: smp.set i 5
def lock_empty
  steps is.empty:
    each: smp.set i 9
def lock_lock
  steps has.lock
  smp.rnd 2 4
def has_smp_steps
  steps has.smp
  tr.clr
def reads
  trk 2
  x has.trig 3
  y has.lock 2
  z smp.get 3
  t trk.type
def reads_trk
  trk 3
  x has.trig 2
  y is.empty 6
  z smp.get 5
  t has.smp 9
def combo
  trk 2 3
  steps has.trig
  tr.del 1
  l 1 16: tr.add i
  smp.set 4 12
  tr.flip 8
def many_skips
  l 1 8: l 1 8: l 1 8: smp.set 3 5
def many_pickups
  l 1 90: tr.add 1
"""


def blank():
    """{1..8: bytearray}: the RAM blocks of the empty pattern."""
    return {t: bytearray(b) for t, b in Start().blocks.items()}


def set_bit(b, mask, s):
    b[mask + 7 - ((s - 1) >> 3)] |= 1 << ((s - 1) & 7)


def get_bit(b, mask, s):
    return (b[mask + 7 - ((s - 1) >> 3)] >> ((s - 1) & 7)) & 1


TRIG, TLESS, TLOCK, ONESHOT = 0x00, 0x08, 0x10, 0x18


def rec_at(b, s):
    return bytes(b[0x59 + (s - 1) * 0x20:0x59 + s * 0x20])


def put_rec(b, s, rec):
    b[0x59 + (s - 1) * 0x20:0x59 + s * 0x20] = rec


def word_at(b, s):
    return (b[0x89A + (s - 1) * 2] << 8) | b[0x89B + (s - 1) * 2]


def put_word(b, s, w):
    b[0x89A + (s - 1) * 2], b[0x89B + (s - 1) * 2] = w >> 8, w & 255


def lock_rec(p, v, smp=None, spare=0xFF):
    """A step record with one p-lock and an optional sample lock (slot - 1)."""
    r = bytearray(b"\xff" * 32)
    r[p], r[30] = v, spare
    if smp is not None:
        r[31] = smp
    return bytes(r)


def bank_from_blocks(name, blocks):
    """out/otype/e2b_<name>.bank: the empty bank with pattern 1 made of the RAM blocks (words moved)."""
    d = bytearray(read_file(empty_bank()))
    for t, blk in blocks.items():
        tr = sim.trac_off(0, t - 1)
        d[tr:tr + 0x919] = blk[:0x919]
        d[tr + 0x899:tr + 0x919] = blk[0x89A:0x91A]
    path = os.path.join(OUT, "e2b_%s.bank" % name)
    with open(path, "wb") as fh:
        fh.write(d)
    return path


def random_bank(seed):
    """A pattern 1 full of everything: masks, p-locks, sample locks, trig words, bytes the operations
    must leave alone.  Deterministic."""
    import random
    rng = random.Random(seed)
    d = bytearray(read_file(empty_bank()))
    for t in range(8):
        tr = sim.trac_off(0, t)
        for m, dens in ((TRIG, .3), (TLESS, .08), (TLOCK, .1), (ONESHOT, .08)):
            v = sum(1 << b for b in range(64) if rng.random() < dens)
            d[tr + m:tr + m + 8] = v.to_bytes(8, "big")
        for m in (0x20, 0x28, 0x30, 0x38, 0x48):
            d[tr + m:tr + m + 8] = bytes(rng.randrange(256) for _ in range(8))
        d[tr + 0x51:tr + 0x59] = bytes(rng.randrange(256) for _ in range(8))
        for s in range(1, 65):
            if rng.random() < .4:
                r = bytearray(b"\xff" * 32)
                for _ in range(rng.randrange(4)):
                    r[rng.randrange(30)] = rng.randrange(128)
                if rng.random() < .3:
                    r[30] = rng.randrange(256)
                if rng.random() < .5:
                    r[31] = rng.randrange(136)
                d[tr + 0x59 + (s - 1) * 0x20:tr + 0x59 + s * 0x20] = r
            w = ((rng.randrange(8) << 13) | ((rng.randrange(-23, 24) & 0x3F) << 7)
                 | (rng.randrange(65) if rng.random() < .6 else 0))
            d[tr + 0x899 + (s - 1) * 2:tr + 0x899 + s * 2] = w.to_bytes(2, "big")
    path = os.path.join(OUT, "e2b_rand%d.bank" % seed)
    with open(path, "wb") as fh:
        fh.write(d)
    return path


REAL_BANK = os.path.join(REAL_DIR, "bank01.work")


class TestRandomPatterns(Base):
    """The hand-written pattern script over random starts (and a few with machine types)."""

    def test_pattern_script_on_random_patterns(self):
        prog, c, table = compile_sources([("P.OTY", PATTERN_SRC)])
        st = Stats()
        for seed in (1, 2, 3):
            start = Start(random_bank(seed), 1, "random%d" % seed)
            compare_table(self, self.cv, table, c.data, st, variants=((1, 16), (4, 64), (6, 7)), start=start)
        self.assertEqual(st.skipped, 0)
        self.assertEqual(st.funcs, 3 * 20)
        self.assertGreater(st.skips, 100)
        self.assertGreater(st.pickups, 50)
        self.assertGreater(st.writing, 500)


@unittest.skipUnless(os.path.exists(REAL_BANK),
                     "bank01.work not found (%s): user data, never copied into the repo" % REAL_BANK)
class TestRealPatterns(Base):
    """Both engines start from the user's own patterns (bank01.work, patterns 1 and 2): the corpus
    functions that use tr.* smp.* has.* / `steps <predicate>`, seeds 0..20."""

    def start(self, pattern):
        return Start(REAL_BANK, pattern, "bank01.p%d" % pattern)

    def test_decoding_agrees_with_read_bank(self):
        for pattern in (1, 2):
            st = self.start(pattern)
            tracks, _info = sim.read_bank(REAL_BANK, pattern)
            self.assertEqual({t: tracks[t].to_json() for t in tracks}, st.json)

    def test_pattern_script(self):
        prog, c, table = compile_sources([("P.OTY", PATTERN_SRC)])
        stats = Stats()
        for pattern in (1, 2):
            compare_table(self, self.cv, table, c.data, stats, variants=((1, 16), (2, 16), (5, 64)),
                          start=self.start(pattern))
        self.assertGreater(stats.writing, 300)

    def test_corpus_functions_that_use_the_pattern(self):
        def go(tc, st):
            for pattern in (1, 2):
                start = tc.start(pattern)
                prog = otype.check_files(T.EXAMPLES)
                c = otb.compile_program(prog)
                compare_table(tc, tc.cv, otb.parse(c.data), c.data, st, start=start, pattern_only=True)
                tc.run_sources_at(T.kitchen_sources(), st, start)
                for srcs in extra_sources():
                    tc.run_sources_at(srcs, st, start, variants=((1, 16), (5, 64), (8, 7)))
                for srcs in T.harvest():
                    tc.run_sources_at(srcs, st, start)
        st = self.part("real", go)
        print("\n[cvm] real bank01.work patterns 1 and 2: functions %d, runs %d, presses that wrote a track %d, "
              "no-trig skips %d, pickup skips %d" % (st.funcs, st.runs, st.writing, st.skips, st.pickups))
        self.assertGreater(st.funcs, 300)
        self.assertGreater(st.writing, 1000)

    def run_sources_at(self, sources, st, start, variants=((1, 16),)):
        _prog, c, table = compile_sources(sources)
        compare_table(self, self.cv, table, c.data, st, variants, start=start, pattern_only=True)


class TestPatternBytes(Base):
    """Exact bytes for the three write rules, and the sim agrees."""

    def run_blocks(self, src, blocks, mtypes=None, track=1, seed=0, name="f", copy=False):
        _prog, c, table = compile_sources([("E.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        for t, b in blocks.items():
            self.cv.bind(t, b, (mtypes or {}).get(t, 0), copy=copy)
        r = self.cv.run(name, seed, track, 16)
        self.assertFalse(isinstance(r, tuple), r)
        return r, self.cv.blocks()

    def agrees(self, src, blocks, mtypes=None, name="f", track=1):
        """The simulator on a bank file made of `blocks` gives the same model and lists."""
        path = bank_from_blocks("explicit", blocks)
        prog, c, table = compile_sources([("E.OTY", src)])
        compare_table(self, self.cv, table, c.data, Stats(), variants=((track, 16, mtypes or {}),),
                      only={name.upper()}, start=Start(path, 1, "explicit"))

    def removal_block(self, tlock=False):
        b = blank()
        set_bit(b[1], TRIG, 2)
        put_rec(b[1], 2, lock_rec(15, 9, smp=2, spare=0x55))
        put_word(b[1], 2, (2 << 13) | (5 << 7) | 19)         # 3 trigs, micro +5, condition 19
        if tlock:
            set_bit(b[1], TLOCK, 2)
        return b

    def test_remove_wipes_record_and_condition_keeps_micro_and_count(self):
        for rm in ("tr.del 2", "tr.flip 2", "tr.clr"):
            with self.subTest(rm):
                b = self.removal_block()
                r, after = self.run_blocks("def f\n  %s\n" % rm, b)
                a = after[1]
                self.assertEqual([get_bit(a, m, 2) for m in (TRIG, TLESS, TLOCK, ONESHOT)], [0, 0, 0, 0])
                self.assertEqual(rec_at(a, 2), b"\xff" * 32)              # p-locks, spare byte, sample lock
                self.assertEqual(word_at(a, 2), (2 << 13) | (5 << 7))     # condition gone, micro + count kept
                self.assertEqual(r["touched"], {1})
                self.assertEqual(r["skipped_no_trig"], [])
                for t in range(2, 9):
                    self.assertEqual(after[t], b[t])
                self.agrees("def f\n  %s\n" % rm, self.removal_block())

    def test_remove_with_a_trigless_lock_clears_only_the_trig_bits(self):
        for rm in ("tr.del 2", "tr.flip 2", "tr.clr"):
            with self.subTest(rm):
                b = self.removal_block(tlock=True)
                _r, after = self.run_blocks("def f\n  %s\n" % rm, b)
                a = after[1]
                self.assertEqual([get_bit(a, m, 2) for m in (TRIG, TLESS, TLOCK, ONESHOT)], [0, 0, 1, 0])
                self.assertEqual(rec_at(a, 2), rec_at(b[1], 2))
                self.assertEqual(word_at(a, 2), word_at(b[1], 2))
                self.agrees("def f\n  %s\n" % rm, self.removal_block(tlock=True))

    def test_remove_where_there_is_no_family_trig_changes_nothing(self):
        for tlock in (False, True):
            b = blank()
            if tlock:
                set_bit(b[1], TLOCK, 2)
            put_rec(b[1], 2, lock_rec(15, 9, smp=2))
            put_word(b[1], 2, 0x1234 & 0x7FFF)
            src = "def f\n  tr.del 2\n  tr.clr\n  tr.flip 3\n  tr.del 3\n"    # (3: added, removed again)
            r, after = self.run_blocks(src, b)
            self.assertEqual(after[1], b[1])
            self.agrees(src, b)

    def test_add_on_a_oneshot_or_trigless_trig_converts_and_keeps_everything(self):
        b = blank()
        for s, m in ((3, ONESHOT), (4, TLESS)):
            set_bit(b[1], m, s)
            put_rec(b[1], s, lock_rec(3, 77, smp=s))
            put_word(b[1], s, (1 << 13) | (61 << 7) | 33)
        r, after = self.run_blocks("def f\n  tr.add 3\n  tr.add 4\n", b)
        a = after[1]
        for s in (3, 4):
            self.assertEqual([get_bit(a, m, s) for m in (TRIG, TLESS, TLOCK, ONESHOT)], [1, 0, 0, 0])
            self.assertEqual(rec_at(a, s), rec_at(b[1], s))
            self.assertEqual(word_at(a, s), word_at(b[1], s))
        self.assertEqual(r["touched"], {1})
        self.agrees("def f\n  tr.add 3\n  tr.add 4\n", b)

    def test_add_on_a_step_without_a_family_trig_zeroes_the_word_and_keeps_the_locks(self):
        b = blank()
        set_bit(b[1], TLOCK, 5)                                   # a trigless lock keeps its locks
        put_rec(b[1], 5, lock_rec(2, 40, smp=9))
        put_word(b[1], 5, (7 << 13) | (3 << 7) | 50)
        put_word(b[1], 6, (4 << 13) | 10)                         # a leftover word on an empty step
        r, after = self.run_blocks("def f\n  tr.add 5\n  tr.add 6\n", b)
        a = after[1]
        self.assertEqual([get_bit(a, m, 5) for m in (TRIG, TLESS, TLOCK, ONESHOT)], [1, 0, 1, 0])
        self.assertEqual(rec_at(a, 5), rec_at(b[1], 5))
        self.assertEqual((word_at(a, 5), word_at(a, 6)), (0, 0))
        self.assertEqual(get_bit(a, TRIG, 6), 1)
        self.agrees("def f\n  tr.add 5\n  tr.add 6\n", b)

    def test_add_on_a_red_trig_changes_nothing(self):
        b = self.removal_block()
        r, after = self.run_blocks("def f\n  tr.add 2\n", b)
        self.assertEqual(after[1], b[1])
        self.assertEqual(r["touched"], set())

    def test_pickup_track_is_skipped_before_its_steps_and_counted(self):
        b = blank()
        for t in (1, 2):
            set_bit(b[t], TRIG, 1)
            set_bit(b[t], TRIG, 4)
        r, after = self.run_blocks("def f\n  trk 1 2\n  tr.clr\n", b, mtypes={2: 4})
        self.assertEqual(after[2], b[2])
        self.assertEqual(after[1][:8], bytes(8))
        self.assertEqual(r["skipped_pickup"], [{"func": "f", "op": "tr.clr", "line": 3, "track": 2}])
        self.assertEqual(r["touched"], {1})
        r, after = self.run_blocks("def f\n  trk 1 2\n  smp.rnd 1 9\n", b, mtypes={2: 4})
        self.assertEqual(r["draws"]["rand"], 16)                  # track 1's 16 steps only
        self.assertEqual(len(r["skipped_pickup"]), 1)
        self.assertEqual(r["skipped_no_trig"][0]["op"], "smp.rnd")
        self.assertEqual(len(r["skipped_no_trig"]), 14)           # 2 of the 16 steps have a trig
        self.agrees("def f\n  trk 1 2\n  smp.rnd 1 9\n", b, mtypes={2: 4})
        self.agrees("def f\n  trk 1 2\n  tr.clr\n", b, mtypes={2: 4})

    def test_reads_still_answer_on_a_pickup_track(self):
        b = blank()
        set_bit(b[2], TRIG, 1)
        put_rec(b[2], 1, lock_rec(0, 3, smp=6))
        r, _after = self.run_blocks("def f\n  trk 2\n  x has.trig 1\n  y trk.type\n  z smp.get 1\n"
                                    "  t has.lock 1\n", b, mtypes={2: 4}, track=2)
        self.assertEqual(r["globals"], {"x": 1, "y": 4, "z": 7, "t": 1})
        self.assertEqual((r["skipped_pickup"], r["touched"]), ([], set()))

    def test_kit_lock_shape(self):
        b = blank()
        for s in (2, 9):
            set_bit(b[1], TRIG, s)
        set_bit(b[1], TLOCK, 4)                                   # a trigless lock is not has.trig
        src = "def f\n  steps has.trig:\n    each: smp.set i 5\n"
        r, after = self.run_blocks(src, b)
        a = after[1]
        self.assertEqual({s: a[0x59 + (s - 1) * 0x20 + 31] for s in range(1, 17)},
                         {s: (4 if s in (2, 9) else 0xFF) for s in range(1, 17)})
        self.assertEqual((r["skipped_no_trig"], r["touched"]), ([], {1}))
        self.agrees(src, b)

    def test_a_lock_on_a_step_without_a_trig_is_skipped_and_counted(self):
        b = blank()
        r, after = self.run_blocks("def f\n  smp.set 3 5\n  smp.del 3\n", b)
        self.assertEqual(after[1], b[1])
        self.assertEqual(r["skipped_no_trig"], [{"func": "f", "op": "smp.set", "line": 2}])
        self.assertEqual(r["touched"], set())

    def test_sample_lock_bytes(self):
        b = blank()
        for s in (1, 2):
            set_bit(b[1], TRIG, s)
        r, after = self.run_blocks("def f\n  smp.set 1 128\n  smp.set 2 1\n  x smp.get 1\n  y smp.get 2\n"
                                   "  z smp.get 3\n", b)
        self.assertEqual((after[1][0x59 + 31], after[1][0x79 + 31]), (127, 0))
        self.assertEqual(r["globals"], {"x": 128, "y": 1, "z": 0, "t": 0})

    def test_every_write_also_goes_to_the_copy(self):
        b = self.removal_block()
        r, after = self.run_blocks("def f\n  tr.del 2\n  tr.add 7\n  smp.set 7 9\n", b, copy=True)
        self.assertEqual(self.cv.copy_blocks()[1], after[1])
        self.assertNotEqual(after[1], b[1])

    def test_the_copy_gets_only_the_bytes_that_are_written(self):
        _prog, c, _t = compile_sources([("E.OTY", "def f\n  tr.add 1\n")])
        self.assertEqual(self.cv.load(c.data), 0)
        b = blank()
        self.cv.bind(1, b[1], 0, copy=True)
        self.cv.copies[1][0] = 0xAA                               # marks a byte nothing writes
        self.cv.run("f", 0)
        c1 = self.cv.copy_blocks()[1]
        self.assertEqual((c1[0], c1[7]), (0xAA, 1))               # step 1 of the trig mask is written

    def test_skip_lists_are_capped_like_the_oor_list(self):
        prog, c, table = compile_sources([("P.OTY", PATTERN_SRC)])
        st = Stats()
        compare_table(self, self.cv, table, c.data, st, only={"MANY_SKIPS", "MANY_PICKUPS"},
                      variants=((1, 16), (1, 16, {1: PICKUP})))
        self.assertEqual(st.funcs, 2)
        r = self.run_blocks("def f\n  l 1 8: l 1 8: l 1 8: smp.set 3 5\n", blank())[0]
        self.assertEqual((len(r["skipped_no_trig"]), r["skip_overflow"]), (64, 512 - 64))
        r = self.run_blocks("def f\n  l 1 90: tr.add 1\n", blank(), mtypes={1: 4})[0]
        self.assertEqual((len(r["skipped_pickup"]), r["pickup_overflow"]), (64, 26))

    def test_the_record_decodes_from_a_big_endian_dump(self):
        import struct
        _prog, c, table = compile_sources([("E.OTY", "def f\n  trk 1 2\n  smp.set 3 4\n  tr.add 5\n")])
        self.assertEqual(self.cv.load(c.data), 0)
        b = blank()
        for t in (1, 2):
            self.cv.bind(t, b[t], 4 if t == 2 else 0)
        r = self.cv.run("f", 0)
        # smp.set 3 has no trig on track 1; on the PICKUP track 2 both ops are refused
        self.assertEqual((len(r["skipped_no_trig"]), len(r["skipped_pickup"])), (1, 2))
        n = self.cv.lib.otc_vm_export(self.cv.ptr, self.cv.out, len(self.cv.out))
        be = struct.pack(">%di" % n, *self.cv.out[:n])
        self.assertEqual(cvm.decode_export(cvm.words_from_be(be), table), r)

    def test_touched_is_reset_by_each_run(self):
        b = blank()
        _prog, c, _t = compile_sources([("E.OTY", "def f\n  tr.add 1\n")])
        self.assertEqual(self.cv.load(c.data), 0)
        self.cv.bind(3, b[3])
        self.assertEqual(self.cv.run("f", 0, track=3)["touched"], {3})
        self.assertEqual(self.cv.run("f", 0, track=3)["touched"], set())
        self.assertEqual(self.cv.touched(), set())


class TestBinding(Base):
    """An operation that touches a track which is not bound stops the run: OTC_E_UNSUPPORTED."""

    SRC = ("def rd\n  x has.trig 1\ndef rds\n  x smp.get 1\ndef rdt\n  trk 2\n  x trk.type\ndef pr\n  steps has.trig\n"
           "def wr\n  tr.add 1\ndef w2\n  trk 2\n  tr.add 1\ndef w3\n  trk 1 2\n  smp.rnd 1 3\n"
           "def free\n  x add x 1\n")

    @classmethod
    def setUpClass(cls):
        Base.setUpClass()
        cls.prog, cls.c, cls.table = compile_sources([("B.OTY", cls.SRC)])

    def test_unbound_tracks(self):
        self.assertEqual(self.cv.load(self.c.data), 0)
        for fn in ("rd", "rds", "rdt", "pr", "wr", "w2", "w3"):
            with self.subTest(fn):
                self.assertEqual(self.cv.run(fn, 0), ("error", cvm.E_UNSUPPORTED))
        self.assertEqual(self.cv.run("free", 0)["globals"]["x"], 1)      # no binding needed

    def test_only_the_bound_tracks_are_usable(self):
        self.assertEqual(self.cv.load(self.c.data), 0)
        self.cv.bind(2, blank()[2])
        self.assertEqual(self.cv.run("rd", 0), ("error", cvm.E_UNSUPPORTED))      # the host track 1
        self.assertEqual(self.cv.run("rdt", 0)["globals"]["x"], 0)                # track 2: its type
        self.assertEqual(self.cv.run("w2", 0)["touched"], {2})
        self.assertEqual(self.cv.run("w3", 0), ("error", cvm.E_UNSUPPORTED))      # 1 is not bound
        self.assertEqual(self.cv.run("rd", 0, track=2)["globals"]["x"], 1)      # the host is track 2: w2 gave it a trig
        self.cv.bind(2, None)
        self.assertEqual(self.cv.run("w2", 0), ("error", cvm.E_UNSUPPORTED))

    def test_load_clears_the_bindings(self):
        self.assertEqual(self.cv.load(self.c.data), 0)
        for t in range(1, 9):
            self.cv.bind(t, blank()[t])
        self.assertEqual(self.cv.run("wr", 0)["touched"], {1})
        self.assertEqual(self.cv.load(self.c.data), 0)
        self.assertEqual(self.cv.run("wr", 0), ("error", cvm.E_UNSUPPORTED))


# ==================================================================================================
# the card / slot operations
# ==================================================================================================
CARD_SEEDS = (1, 2) if QUICK else (1, 2, 5, 9, 14)
BIG_SEEDS = (1,) if QUICK else (1, 2)
TREES = {}                         # kind -> ([relative files; "x/" = an empty folder], the folder a template lists)
_TREE_DIR = []
_TREE_MADE = {}


def tree(kind, files, folder="AUDIO"):
    TREES[kind] = (list(files), folder)


def tree_root(kind):
    """The Set folder of the tree `kind` on disk (made once per process, removed at exit)."""
    if kind in _TREE_MADE:
        return _TREE_MADE[kind]
    if not _TREE_DIR:
        d = tempfile.mkdtemp(prefix="otc_trees_")
        atexit.register(shutil.rmtree, d, True)
        _TREE_DIR.append(d)
    root = os.path.join(_TREE_DIR[0], kind)
    os.makedirs(root)
    for rel in TREES[kind][0]:
        path = os.path.join(root, *rel.split("/"))
        if rel.endswith("/"):
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")
    _TREE_MADE[kind] = root
    return root


def tree_setup(kind, **kw):
    return CardSetup(tree_root(kind), label=kw.pop("label", kind), **kw)


F60 = "d" * 60
tree("none", [])                                           # the Set exists, AUDIO does not
tree("empty", ["AUDIO/"])
tree("one", ["AUDIO/only.wav"])
tree("five", ["AUDIO/A/%s.wav" % c for c in "abcde"], "AUDIO/A")
tree("bags", ["AUDIO/A/%s.wav" % c for c in "abcde"] + ["AUDIO/F%d/%s.wav" % (i, c) for i in range(9) for c in "ab"]
     + ["AUDIO/G/x.wav"], "AUDIO/F0")
tree("bulk130", ["AUDIO/BULK/f%03d.wav" % i for i in range(130)]
     + ["AUDIO/BULK/sub/g%d.aif" % i for i in range(3)], "AUDIO/BULK")                 # > 128 slots
tree("big4097", ["AUDIO/BIG/b%04d.wav" % i for i in range(4097)], "AUDIO/BIG")         # the cut at 4096
tree("long", ["AUDIO/LONG/ok.wav",
              "AUDIO/LONG/" + "a" * 123 + ".wav",                      # 127 bytes: kept
              "AUDIO/LONG/" + "b" * 124 + ".wav",                      # 128: skipped
              "AUDIO/LONG/" + "c" * 200 + ".wav",                      # 204: skipped, handed over by the walker
              "AUDIO/LONG/" + "e" * 60 + "/" + "f" * 60 + "/x.wav",    # 127 bytes: kept
              "AUDIO/LONG/" + "e" * 60 + "/" + "f" * 61 + "/x.wav",    # 128: skipped
              "AUDIO/LONG/" + "g" * 60 + "/" + "h" * 60 + "/" + "i" * 60 + "/x.wav",     # 188: skipped
              "AUDIO/LONG/DEEP/" + "/".join([F60] * 5) + "/x.wav",     # 315 bytes: only counted by the walker
              "AUDIO/LONG/DEEP/short.wav"], "AUDIO/LONG")
tree("mixed", ["AUDIO/MIX/" + n for n in
               ("Kick.wav", "kick2.WAV", "KICK3.wav", "Snare.Aif", "snare2.aiff", "hat.AIFC", "Zed.wav",
                "apple.wav", "Banana.wav", "_x.wav", "-y.wav", "a b.wav", "a/x.wav", "A2/y.wav", "b/z.wav",
                "B2/w.wav", "~tilde.wav", "0num.wav", "Mix.Wav")], "AUDIO/MIX")
tree("levels6", ["AUDIO/l1.wav"] + ["AUDIO/" + "/".join("L%d" % k for k in range(1, n + 1)) + "/l%d.wav" % n
                                    for n in range(2, 7)], "AUDIO")
tree("dots", ["AUDIO/D/._kick.wav", "AUDIO/D/.DS_Store", "AUDIO/D/.hidden/x.wav", "AUDIO/D/real1.wav",
              "AUDIO/D/real2.wav", "AUDIO/D/sub/._junk.wav", "AUDIO/D/sub/ok.wav", "AUDIO/D/.wav",
              "AUDIO/D/sub/.deep/y.wav"], "AUDIO/D")
tree("exts", ["AUDIO/E/" + n for n in
              ("a.wav", "b.WAV", "c.Aif", "d.AIFF", "e.aifc", "f.mp3", "g.txt", "noext", "h.", "i.wav.bak",
               "j.tar.wav", "sub.dir/k.wav", "sub.dir/l", "x..wav", "m.aiff.AIF")], "AUDIO/E")
tree("nonascii", ["AUDIO/N/é.wav", "AUDIO/N/Ea.wav", "AUDIO/N/ea2.wav", "AUDIO/N/zz.wav"], "AUDIO/N")
tree("depth17", ["AUDIO/DP/f0.wav"] + ["AUDIO/DP/" + "/".join(chr(97 + k) for k in range(n)) + "/f%d.wav" % n
                                       for n in (1, 14, 15, 16, 17)], "AUDIO/DP")       # the unit goes 15 folders down
tree("accents", ["AUDIO/AC/\u00e9.wav", "AUDIO/AC/\u00c9a.wav", "AUDIO/AC/Zed.wav", "AUDIO/AC/\u00fc b.WAV", "AUDIO/AC/\u00ff.aif",
                 "AUDIO/AC/Caf\u00e9/Cr\u00e8me.wav", "AUDIO/AC/Caf\u00e9/a.wav", "AUDIO/AC/\u00c4/\u00d6/\u00dc.wav"], "AUDIO/AC")
tree("budget", ["AUDIO/A/a%04d.wav" % i for i in range(3000)] + ["AUDIO/B/b%04d.wav" % i for i in range(2000)]
     + ["AUDIO/C/c%04d.wav" % i for i in range(4096)] + ["AUDIO/D/d.wav"]
     + ["AUDIO/E/e%04d.wav" % i for i in range(1100)] + ["AUDIO/E/" + "z" * 130 + ".wav"], "AUDIO/A")
tree("corpus", ["A/%s" % n for n in ("a.wav", "b.wav", "c.wav", "d.WAV", "e.aif", "Kick.wav", "sub/x.wav", "z.mp3")]
     + ["B/b%03d.wav" % i for i in range(140)] + ["F%d/%s.wav" % (i, c) for i in range(9) for c in "ab"]
     + ["AUDIO/DRUMS/KICKS/" + n for n in ("k1.wav", "k2.wav", "k3.aif", "s1.wav", "z9.mp3")]
     + ["AUDIO/DRUMS/SNARES/s1.wav", "AUDIO/DRUMS/SNARES/s2.wav", "AUDIO/DRUMS/HIHATS/h%d.wav" % 1,
        "AUDIO/DRUMS/ELKA/hat1.wav", "AUDIO/DRUMS/ELKA/closedhat.wav", "AUDIO/DRUMS/ELKA/rim.wav",
        "AUDIO/X/x1.wav", "AUDIO/X/x2.aif", "AUDIO/X/x3.wav", "AUDIO/top.wav"])
tree("kicks", ["AUDIO/DRUMS/KICKS/" + n for n in
               ("bd01.wav", "BD02.WAV", "kick_a.wav", "KickHard.wav", "kick_b.aif", "snare1.wav", "notes.txt",
                "._bd99.wav")]
     + ["AUDIO/DRUMS/ELKA/" + n for n in
        ("hat1.wav", "closedhat.wav", "OPHAT.WAV", "rim.wav", "hat9.aif", "thing.wav")]
     + ["AUDIO/bd_root.wav", "AUDIO/misc/kick_x.wav", "AUDIO/misc/sub/BD_deep.wav", "AUDIO/misc/x.mp3"])

# the template: every card operation on the list of the tree's folder ({P}); one function per behaviour
CARD_TEMPLATE = """
def c_basic
  dir "{P}"
  x file.len
  y file.at 1
  z file.at file.len
  t file.at 0
  dir "{P}" deep
  static.load 1 file.len
def c_filters
  dir "{P}" deep
  ext wav
  x file.len
  match "k*" "b*" "*a*"
  y file.len
  match "*"
  z file.len
  ext aif aiff aifc
  t file.len
def c_pick
  dir "{P}" deep
  l 1 7:
    x add x file.pick
  y file.rnd
  z file.pick
  ext wav
  t file.pick
def c_load
  dir "{P}" deep
  static.load 2 1
  static.load 4 file.len
  static.load 6 file.rnd
  a add 100 29
  static.load a 1
  static.load 3 0
  static.load 3 add file.len 1
  x static.firstfree
  y static.free 4
  static.clr 4
  static.clr 5
  z static.firstfree
  t static.free 4
def c_fill
  dir "{P}" deep
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.at i
  x static.firstfree
  l 1 128:
    if static.free i: break
    static.swap i rrand 1 i
  y static.free 128
  static.clr 1
  static.load 1 file.len
  l 1 128:
    static.clr i
  t static.firstfree
def c_fillonly
  dir "{P}" deep
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.at i
def c_rnd
  dir "{P}" deep
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.pick
  x static.firstfree
  static.clr 3
  ext wav
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.pick
  y static.free 3
def c_fillext
  dir "{P}" deep
  ext wav
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.at i
  l 1 128:
    if static.free i: break
    static.swap i rrand 1 i
  static.clr 2
  static.load 2 1
def c_wset
  dir "{P}" deep
  slots.clr
  l 1 6:
    a static.firstfree
    static.load a file.pick
    slots.add a
  slots.add 3
  slots.add 3
  x slots.rnd
  y slots.rnd
  slots.clr
  z slots.rnd
def c_call
  dir "{P}" deep
  c_callee
  x file.len
  y file.pick
def c_callee
  x file.len
  ext wav
  y file.len
  dir "{P}"
  z file.len
  c_callee2
  t file.pick
def c_callee2
  match "a*"
  static.load 7 file.at 1
def c_bags
  dir "{P}" deep
""" + "".join('  x file.pick\n  match "*"\n' for _ in range(10)) + """  dir "{P}" deep
  y file.pick
def c_root
  dir ""
  x file.len
  dir "" deep
  y file.len
  dir "NOPE"
  z file.len
  t file.pick
  static.load 1 1
  dir "/AUDIO\\\\" deep
  static.load 5 1
  dir "AUDIO/../.."
  x file.len
"""


def template(kind):
    return CARD_TEMPLATE.replace("{P}", TREES[kind][1])


def press_both(tc, cv, src, name, setup, seed=1, presses=1, label=None, start=None, track=1, length=16):
    """Compile `src` and run `name` for `presses` chained presses on BOTH engines (compare_press: every
    comparison of the corpus).  Returns the simulator's result of each press."""
    _p, c, table = compile_sources([("T.OTY", src)] if isinstance(src, str) else src)
    tc.assertEqual(cv.load(c.data), 0)
    prog = vm.OtbProgram(table)
    start = start or Start()
    cv.reset_state()
    out, state, stats = [], None, Stats()
    for press in range(presses):
        what = "%s [%s] seed %d press %d" % (label or name, setup.label, seed, press)
        r1 = compare_press(tc, cv, prog, name.upper(), seed, state, track, length, start, {}, setup, press,
                           what, stats)
        out.append(r1)
        state = r1["state"] if r1 else None
    return out


class CardBase(Base):
    def tree_table(self, kind, template_text=None):
        _p, c, table = compile_sources([("C.OTY", template_text or template(kind))])
        return c, table


class TestCardTrees(CardBase):
    """The trees: the template on each of them, every function,
    seeds, three chained presses; a shuffled walk order at every press."""

    def run_kind(self, kind, seeds=None, only=None):
        c, table = self.tree_table(kind)
        st = Stats()
        compare_table(self, self.cv, table, c.data, st, setup=tree_setup(kind), seeds=seeds or CARD_SEEDS,
                      only=only)
        self.assertEqual((st.skipped, st.funcs, st.card_funcs), (0, 14, 14), kind)     # 13 + c_rnd (the random fill loop)
        return st

    def test_empty_and_missing(self):
        for kind in ("none", "empty", "one"):
            self.run_kind(kind)

    def test_more_files_than_slots(self):
        st = self.run_kind("bulk130")
        self.assertGreater(st.loaded, 10)
        r = press_both(self, self.cv, template("bulk130"), "c_fillonly", tree_setup("bulk130"))[0]
        self.assertEqual(sorted(int(k) for k in r["model"]["slots"]), list(range(1, 129)))   # 133 names, 128 slots
        self.assertEqual(r["model"]["slots"]["1"], "AUDIO/BULK/f000.wav")
        self.assertEqual(r["model"]["slots"]["128"], "AUDIO/BULK/f127.wav")

    def test_four_thousand_and_ninety_seven_names_are_cut(self):
        self.run_kind("big4097", BIG_SEEDS)
        r = press_both(self, self.cv, template("big4097"), "c_basic", tree_setup("big4097"))[0]
        self.assertEqual(r["warnings"], ['dir "AUDIO/BIG": 4097 files, only the first 4096 kept'])
        self.assertEqual(r["globals"]["x"], 4096)
        self.assertEqual(r["model"]["slots"], {"1": "AUDIO/BIG/b4095.wav"})       # static.load 1 file.len: the last kept

    def test_long_names(self):
        self.run_kind("long")
        r = press_both(self, self.cv, template("long"), "c_basic", tree_setup("long"))[0]
        # the folder holds 4 direct files (b and c are too long) and 9 names deep: the 127-byte ones stay;
        # 128, 204 and 188 bytes go, and the 315-byte one never reaches the C (the walker only counts it)
        self.assertEqual(r["warnings"], ['dir "AUDIO/LONG": 2 names longer than 127 characters skipped',
                                         'dir "AUDIO/LONG": 5 names longer than 127 characters skipped'])
        self.assertEqual((r["globals"]["x"], r["globals"]["z"]), (2, 2))
        self.assertEqual(r["model"]["slots"], {"1": "AUDIO/LONG/ok.wav"})        # the 4th of 4 deep names
        self.assertEqual(r["model"]["slots"]["1"], "AUDIO/LONG/ok.wav")

    def test_mixed_case_sorts_on_folded_bytes(self):
        self.run_kind("mixed")
        r = press_both(self, self.cv, template("mixed"), "c_fillonly", tree_setup("mixed"))[0]
        got = [r["model"]["slots"][str(k)].rsplit("MIX/", 1)[1] for k in range(1, 20)]
        self.assertEqual(got, sorted(got, key=sim._sort_key))
        self.assertLess(got.index("a b.wav"), got.index("a/x.wav"))      # 0x20 < 0x2f: folders are not grouped
        self.assertLess(got.index("0num.wav"), got.index("Banana.wav"))

    def test_six_levels(self):
        self.run_kind("levels6")
        r = press_both(self, self.cv, template("levels6"), "c_fillonly", tree_setup("levels6"))[0]
        self.assertEqual(len(r["model"]["slots"]), 6)
        self.assertIn("AUDIO/L1/L2/L3/L4/L5/L6/l6.wav", r["model"]["slots"].values())

    def test_dot_names_are_never_listed(self):
        self.run_kind("dots")
        r = press_both(self, self.cv, template("dots"), "c_fillonly", tree_setup("dots"))[0]
        self.assertEqual(sorted(r["model"]["slots"].values()),
                         ["AUDIO/D/real1.wav", "AUDIO/D/real2.wav", "AUDIO/D/sub/ok.wav"])
        # a walker that breaks its contract (hands the dot names over): the C drops them itself
        leaky = tree_setup("dots", leak_dots=True)
        r = press_both(self, self.cv, template("dots"), "c_fillonly", leaky)[0]
        self.assertEqual(len(r["model"]["slots"]), 3)

    def test_extensions(self):
        self.run_kind("exts")
        r = press_both(self, self.cv, template("exts"), "c_filters", tree_setup("exts"))[0]
        # wav: a.wav b.WAV j.tar.wav x..wav sub.dir/k.wav (not i.wav.bak, not m.aiff.AIF); then no aif is left
        self.assertEqual((r["globals"]["x"], r["globals"]["t"]), (5, 0))
        self.assertEqual(r["state"]["bags"], [])

    def test_non_ascii_name(self):
        self.run_kind("nonascii")
        r = press_both(self, self.cv, template("nonascii"), "c_fillonly", tree_setup("nonascii"))[0]
        got = [r["model"]["slots"][str(k)].rsplit("N/", 1)[1] for k in range(1, 5)]
        self.assertEqual(got, ["Ea.wav", "ea2.wav", "zz.wav", "é.wav"])      # the e-acute sorts last: no folding

    def test_depth_is_15_folders_below_the_listed_one(self):
        self.run_kind("depth17")
        r = press_both(self, self.cv, template("depth17"), "c_filters", tree_setup("depth17"))[0]
        self.assertEqual(r["globals"]["x"], 4)                  # f0, f1, f14, f15: the files under 16 and 17 folders are not visited
        self.assertEqual(r["warnings"], [])                     # and nothing is said about them
        self.assertEqual(r["globals"]["x"], len(sim.list_card(tree_root("depth17"), "AUDIO/DP", deep=True)))
        hc = cvm.HostCard(tree_root("depth17"))
        self.assertEqual(sorted(hc.listing("AUDIO/DP", True), key=len)[-1].count("/"), 15)
        self.assertEqual(len(hc.listing("AUDIO/DP", True)), 4)
        sub = hc.listing("AUDIO/DP/a", True)                       # one folder lower: f16 is 15 folders down (listed), f17 16 (not)
        self.assertEqual((len(sub), any(n.endswith("f16.wav") for n in sub), any(n.endswith("f17.wav") for n in sub)), (4, True, False))

    def test_accents_sort_on_utf8_and_load(self):
        self.run_kind("accents")
        r = press_both(self, self.cv, template("accents"), "c_fillonly", tree_setup("accents"))[0]
        got = [r["model"]["slots"][str(k)].split("AC/", 1)[1] for k in range(1, 9)]
        self.assertEqual(got, sorted(got, key=sim._sort_key))          # ASCII fold only: the accents sort by their UTF-8 bytes
        self.assertEqual(got, ["Caf\u00e9/a.wav", "Caf\u00e9/Cr\u00e8me.wav", "Zed.wav", "\u00c4/\u00d6/\u00dc.wav", "\u00c9a.wav",
                               "\u00e9.wav", "\u00fc b.WAV", "\u00ff.aif"])       # accents after every ASCII letter, E-acute before e-acute: no folding

    def test_set_root_and_missing_folder(self):
        for kind in ("kicks", "mixed", "none"):
            c, table = self.tree_table(kind)
            st = Stats()
            compare_table(self, self.cv, table, c.data, st, setup=tree_setup(kind), seeds=CARD_SEEDS,
                          only={"C_ROOT"})
            self.assertEqual(st.funcs, 1)
        r = press_both(self, self.cv, template("kicks"), "c_root", tree_setup("kicks"))[0]
        # no file at the Set root, 17 below it (the dot name is never listed), a missing folder and a `..` give
        # nothing; `static.load 1 1` on the empty list is out of range, the second one (AUDIO deep) loads
        self.assertEqual((r["globals"]["x"], r["globals"]["y"], r["globals"]["z"], r["globals"]["t"]), (0, 17, 0, 0))
        self.assertEqual([o["op"] for o in r["out_of_range"]], ["static.load"])
        self.assertEqual(r["model"]["slots"], {"5": "AUDIO/bd_root.wav"})

    def test_template_covers_the_card_operations(self):
        _c, table = self.tree_table("kicks")
        used = set()
        for f in table.funcs:
            for _p, ins, ops, _s in T.decode_all(f):
                if ins.name in ("op", "set"):
                    used.add(otb.REG[(ops[0], ops[1])].name)
                elif ins.name in CARD_INS:
                    used.add(("DIR", "EXT", "MATCH")[CARD_INS[ins.name] - 1])
        want = {o.name for o in O.ALL_OPS if gen_ops_c.card(o) and o.name not in gen_ops_c.AUDIO_EXTRA}
        self.assertEqual(want - used, set())


class TestCardM1(CardBase):
    """The scripts of M1 (modules/otype/examples/M1.OTY): `kicks` and `kit_lock` on a card made for them,
    from a pattern full of trigs (random_bank), three chained presses."""

    M1 = os.path.join(ROOT, "modules", "otype", "examples", "M1.OTY")

    def test_kicks_and_kit_lock(self):
        with open(self.M1) as fh:
            _p, c, table = compile_sources([("M1.OTY", fh.read())])
        st = Stats()
        for bank in (1, 2):
            start = Start(random_bank(bank), 1, "random%d" % bank)
            compare_table(self, self.cv, table, c.data, st, start=start, only={"KICKS", "KIT_LOCK"},
                          setup=tree_setup("kicks"), seeds=CARD_SEEDS)
        self.assertEqual((st.funcs, st.card_funcs, st.skipped), (4, 4, 0))
        self.assertGreater(st.loaded, 10)
        self.assertGreater(st.writing, 5)            # kit_lock wrote sample locks (smp.set)

    def test_kicks_fills_slots_from_the_bag(self):
        with open(self.M1) as fh:
            src = fh.read()
        rs = press_both(self, self.cv, [("M1.OTY", src)], "kicks", tree_setup("kicks"), seed=3, presses=3)
        # `dir "AUDIO" deep`, ext wav, match bd* kick*: 7 names; 8 picks per press from a bag of 7
        self.assertEqual(rs[0]["state"]["bags"][0]["n"], 7)
        for r in rs:
            self.assertEqual(len(r["model"]["slots"]), 8)
            self.assertEqual(r["draws"]["file"], 8)
        self.assertEqual([len(r["state"]["bags"][0]["rest"]) for r in rs], [6, 5, 4])    # refilled once per 7 picks

    def test_kit_lock_with_a_pool_already_loaded(self):
        with open(self.M1) as fh:
            src = fh.read()
        slots = {1: "AUDIO/old1.wav", 3: "AUDIO/old3.wav"}
        setup = tree_setup("kicks", slots=slots, refuse={"AUDIO/DRUMS/ELKA/hat1.wav"}, label="kicks+slots")
        start = Start(random_bank(1), 1, "random1")
        rs = press_both(self, self.cv, [("M1.OTY", src)], "kit_lock", setup, seed=2, presses=3, start=start)
        for r in rs:
            self.assertIn("1", r["model"]["slots"])      # the old slots stay: only free ones are taken
            self.assertNotIn("AUDIO/DRUMS/ELKA/hat1.wav", r["model"]["slots"].values())


BAG_SRC = """
def pick5
  dir "AUDIO/A"
  x file.pick
def pickn
  dir "AUDIO/F0"
  x file.pick
  dir "AUDIO/F1" deep
  y file.pick
def nine
""" + "".join('  dir "AUDIO/F%d"\n  x file.pick\n' % i for i in range(9)) + """def nine_reuse
""" + "".join('  dir "AUDIO/F%d"\n  x file.pick\n' % i for i in range(8)) + """  dir "AUDIO/F0"
  x file.pick
  dir "AUDIO/F8"
  x file.pick
def per_fn
  dir "AUDIO/F0"
  x file.pick
  per_fn2
  y file.pick
def per_fn2
  z file.pick
def per_fn3
""" + "".join('  dir "AUDIO/F%d"\n  x file.pick\n' % i for i in range(8)) + """  per_fn4
def per_fn4
  dir "AUDIO/F8"
  z file.pick
"""


class TestCardBags(CardBase):
    """The pick bags: persistent between presses, 8 at most, oldest dropped, reuse does not refresh the age,
    an exhausted bag refills on the next pick, reset by load / reset_state; the export carries them."""

    def go(self, name, presses=1, seed=1):
        return press_both(self, self.cv, BAG_SRC, name, tree_setup("bags"), seed=seed, presses=presses)

    def test_a_bag_runs_down_over_the_presses_and_refills(self):
        rs = self.go("pick5", presses=7, seed=4)
        self.assertEqual([len(r["state"]["bags"][0]["rest"]) for r in rs], [4, 3, 2, 1, 0, 4, 3])   # an empty bag stays
        self.assertEqual(sorted(r["globals"]["x"] for r in rs[:5]), [1, 2, 3, 4, 5])      # never twice until all are used

    def test_bags_state_is_complete(self):
        rs = self.go("pickn", presses=2)
        bags = rs[1]["state"]["bags"]
        self.assertEqual([(b["func"], b["n"]) for b in bags], [("pickn", 2), ("pickn", 2)])

    def test_at_most_eight_and_the_oldest_goes(self):
        r = self.go("nine")[0]
        self.assertEqual(len(r["state"]["bags"]), 8)
        self.assertEqual(sim.sig_hash("AUDIO/F1|||2"), r["state"]["bags"][0]["hash"])

    def test_reuse_does_not_refresh_the_age(self):
        r = self.go("nine_reuse")[0]
        self.assertEqual([b["hash"] for b in r["state"]["bags"]],
                         [sim.sig_hash("AUDIO/F%d|||2" % i) for i in range(1, 9)])

    def test_bags_are_per_function(self):
        r = self.go("per_fn", presses=2)[1]
        self.assertEqual([b["func"] for b in r["state"]["bags"]], ["per_fn", "per_fn2"])
        r = self.go("per_fn3")[0]
        self.assertEqual(len(r["state"]["bags"]), 8)            # 8 of main, then one more of per_fn4: the oldest goes
        self.assertEqual(r["state"]["bags"][-1]["func"], "per_fn4")

    def test_load_and_reset_state_empty_the_bags(self):
        self.go("pick5", presses=2)                                        # the bag holds 3 values now
        cv = self.cv
        _p, c, _t = compile_sources([("T.OTY", BAG_SRC)])
        self.assertEqual(len(cv.run("pick5", 1)["state"]["bags"][0]["rest"]), 2)       # state persists
        cv.reset_state()
        self.assertEqual(len(cv.run("pick5", 1)["state"]["bags"][0]["rest"]), 4)       # reset_state: a new bag
        self.assertEqual(cv.load(c.data), 0)                               # load: the wrapper unbinds the card ...
        self.assertEqual(cv.run("pick5", 1), ("error", cvm.E_UNSUPPORTED))
        cv.bind_card(tree_root("bags"))                                    # ... and a new binding starts clean
        self.assertEqual(len(cv.run("pick5", 1)["state"]["bags"][0]["rest"]), 4)


class TestCardRefusals(CardBase):
    """A load the unit refuses: the slot is left free, the script goes on, the refusal is recorded;
    the fill loop offers the slot to the next file."""

    SRC = """
def r_load
  dir "AUDIO/A"
  static.load 3 2
  x static.free 3
  static.load 4 1
  y static.firstfree
def r_occupied
  dir "AUDIO/A"
  static.load 3 2
  x static.free 3
def r_fill
  dir "AUDIO/A"
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.at i
def r_fill128
  dir "AUDIO/BULK"
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.at i
def r_shuffle
  l 1 128:
    if static.free i: break
    static.swap i rrand 1 i
  x static.firstfree
def r_clear
  dir "AUDIO/A"
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.at i
  l 1 128:
    if static.free i: break
    static.swap i rrand 1 i
  l 1 128:
    static.clr i
  static.clr 2
"""

    def one(self, name, kind="five", **kw):
        return press_both(self, self.cv, self.SRC, name, tree_setup(kind, **kw))[0]

    def test_a_refused_load_leaves_the_slot_free_and_the_script_goes_on(self):
        r = self.one("r_load", refuse={"AUDIO/A/b.wav"})
        self.assertEqual(r["model"]["slots"], {"4": "AUDIO/A/a.wav"})
        self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (1, 1))
        self.assertEqual([(f["slot"], f["path"], f["op"]) for f in r["refused"]], [(3, "AUDIO/A/b.wav", "static.load")])

    def test_a_refused_load_on_an_occupied_slot_frees_it(self):
        r = self.one("r_occupied", refuse={"AUDIO/A/b.wav"}, slots={3: "AUDIO/old.wav", 9: "AUDIO/keep.wav"})
        self.assertEqual((r["model"]["slots"], r["globals"]["x"]), ({"9": "AUDIO/keep.wav"}, 1))

    def test_an_occupied_slot_is_replaced(self):
        r = self.one("r_occupied", slots={3: "AUDIO/old.wav"})
        self.assertEqual((r["model"]["slots"], r["globals"]["x"]), ({"3": "AUDIO/A/b.wav"}, 0))

    def test_refuse_as_a_set_folds_ascii_case_and_as_a_predicate_sees_the_stored_path(self):
        r = self.one("r_load", refuse={"audio/A/B.WAV"})
        self.assertEqual(len(r["refused"]), 1)
        seen = []

        def pred(path):
            seen.append(path)
            return path.endswith("c.wav")
        self.one("r_fill", refuse=pred)
        self.assertEqual(seen[:3], ["AUDIO/A/a.wav", "AUDIO/A/b.wav", "AUDIO/A/c.wav"])

    def test_fill_loop_offers_a_refused_slot_to_the_next_file(self):
        r = self.one("r_fill", slots={1: "Z/z.wav"}, refuse={"AUDIO/A/b.wav", "AUDIO/A/c.wav"})
        # free slots 2, 3, 4: a -> 2, b and c refused, d -> 3, e -> 4
        self.assertEqual(r["model"]["slots"], {"1": "Z/z.wav", "2": "AUDIO/A/a.wav", "3": "AUDIO/A/d.wav",
                                               "4": "AUDIO/A/e.wav"})
        self.assertEqual([(f["slot"], f["path"]) for f in r["refused"]], [(3, "AUDIO/A/b.wav"), (3, "AUDIO/A/c.wav")])

    def test_fill_loop_when_every_file_is_refused(self):
        r = self.one("r_fill", refuse=lambda p: True)
        self.assertEqual((r["model"]["slots"], len(r["refused"])), ({}, 5))
        self.assertEqual({f["slot"] for f in r["refused"]}, {1})

    def test_fill_loop_stops_when_the_slots_run_out(self):
        tree("bulk128", ["AUDIO/BULK/f%03d.wav" % i for i in range(130)], "AUDIO/BULK")
        r = self.one("r_fill128", "bulk128", slots={5: "Z/z.wav"}, refuse={"AUDIO/BULK/f001.wav"})
        self.assertEqual(sorted(int(k) for k in r["model"]["slots"]), list(range(1, 129)))
        self.assertEqual(len(r["refused"]), 1)
        self.assertEqual((r["model"]["slots"]["1"], r["model"]["slots"]["2"]), ("AUDIO/BULK/f000.wav", "AUDIO/BULK/f002.wav"))

    def test_shuffle_loop_frees_a_slot_whose_reload_is_refused(self):
        slots = {1: "A/a.wav", 2: "A/b.wav", 3: "A/c.wav"}
        hit = set()
        for seed in range(1, 9):              # C = simulator on each; b is lost exactly when a swap reloaded it
            r = press_both(self, self.cv, self.SRC, "r_shuffle", tree_setup("five", slots=slots, refuse={"A/b.wav"}), seed=seed)[0]
            lost = "A/b.wav" not in r["model"]["slots"].values()
            self.assertEqual(lost, bool(r["refused"]), seed)
            self.assertEqual({(f["op"], f["path"]) for f in r["refused"]}, {("static.swap", "A/b.wav")} if lost else set())
            self.assertEqual({"A/a.wav", "A/c.wav"} - set(r["model"]["slots"].values()), set())
            hit.add(lost)
        self.assertEqual(hit, {True, False})

    def test_fill_shuffle_clear_with_refusals(self):
        r = self.one("r_clear", refuse={"AUDIO/A/d.wav"})
        self.assertEqual(r["model"]["slots"], {})
        self.assertEqual([f["op"] for f in r["refused"]], ["static.load"])    # d: the shuffle only sees a b c e

    def test_refusal_cap_counts_the_rest(self):
        # 100 refusals of one fill loop: the C keeps 64 and counts the others (the simulator lists all)
        tree("bulk100", ["AUDIO/B100/f%03d.wav" % i for i in range(100)], "AUDIO/B100")
        src = 'def f\n  dir "AUDIO/B100"\n' + '  l 1 128:\n    if gt i file.len: break\n    a static.firstfree\n    if ez a: break\n    static.load a file.at i\n'
        setup = tree_setup("bulk100", refuse=lambda p: True)
        r1 = py_run(vm.OtbProgram(compile_sources([("T.OTY", src)])[2]), "F", 1, None, 1, 16, Start(), {}, setup)
        _p, c, table = compile_sources([("T.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        setup.bind(self.cv)
        r2 = self.cv.run("f", 1)
        self.assertEqual((len(r1["refused"]), len(r2["refused"]), r2["refused_overflow"]), (100, cvm.MAX_REFUSED, 36))
        self.assertEqual(r1["refused"][:cvm.MAX_REFUSED], r2["refused"])

    def test_a_name_the_unit_cannot_store_is_a_refusal(self):
        # (no simulator counterpart: its slots hold any path) the C asks slot_set, a non-zero answer frees the slot
        src = 'def f\n  dir "AUDIO/A"\n  static.load 3 2\n  static.load 4 1\n'
        _p, c, table = compile_sources([("T.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        hs = cvm.HostSlots({3: "AUDIO/old.wav"}, store_limit=len("../AUDIO/A/a.wav") - 1)
        self.cv.bind_card(tree_root("five"), hs)
        r = self.cv.run("f", 1)
        self.assertEqual(hs.final(), {})                       # the old name of slot 3 is gone too
        self.assertEqual([(f["slot"], f["path"]) for f in r["refused"]],
                         [(3, "AUDIO/A/b.wav"), (4, "AUDIO/A/a.wav")])
        self.assertEqual(r["slots_touched"], 1)


class TestStaticSwap(CardBase):
    """static.swap a b (phase 2 of the API cleanup): the C = the simulator on the card twins. Each slot is reloaded by name
    from the other's; an empty source clears its target; a = b or two empty slots do nothing (no card access); an argument out
    of 1..128 skips the call and counts it; a refused reload frees its slot; a busy card stops the run."""

    SRC = """
def w_swap
  static.swap 3 9
  x static.free 3
  y static.free 9
def w_same
  static.swap 3 3
def w_move
  static.swap 3 5
  x static.free 3
  y static.free 5
def w_empty2
  static.swap 6 7
def w_oor
  a 0
  b 129
  static.swap a 3
  static.swap 3 b
  static.swap sub a 1 b
  static.swap 1 128
def w_shuffle
  l 1 128:
    if static.free i: break
    static.swap i rrand 1 i
"""
    HELD = {3: "A/a.wav", 9: "B/b.wav"}

    def one(self, name, slots=None, kind="five", seed=1, **kw):
        return press_both(self, self.cv, self.SRC, name, tree_setup(kind, slots=slots, **kw), seed=seed)[0]

    def test_two_used_slots_trade_their_samples(self):
        r = self.one("w_swap", self.HELD)
        self.assertEqual(r["model"]["slots"], {"3": "B/b.wav", "9": "A/a.wav"})
        self.assertEqual((r["globals"]["x"], r["globals"]["y"], r["out_of_range"], r["refused"]), (0, 0, [], []))

    def test_the_same_slot_twice_is_a_no_op_without_card_access(self):
        r = self.one("w_same", self.HELD, busy={"static.swap"})
        self.assertEqual((r["model"]["slots"], r["busy"]), ({"3": "A/a.wav", "9": "B/b.wav"}, False))

    def test_an_empty_slot_takes_the_sample_and_frees_the_source(self):
        r = self.one("w_move", self.HELD)
        self.assertEqual(r["model"]["slots"], {"5": "A/a.wav", "9": "B/b.wav"})
        self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (1, 0))

    def test_two_empty_slots_do_nothing_and_touch_no_card(self):
        r = self.one("w_empty2", self.HELD, busy={"static.swap"})
        self.assertEqual((r["model"]["slots"], r["busy"]), ({"3": "A/a.wav", "9": "B/b.wav"}, False))

    def test_out_of_range_is_skipped_and_counted_the_edges_work(self):
        r = self.one("w_oor", {1: "A/a.wav", 128: "B/b.wav"})
        self.assertEqual([o["op"] for o in r["out_of_range"]], ["static.swap"] * 3)
        self.assertEqual(r["model"]["slots"], {"1": "B/b.wav", "128": "A/a.wav"})

    def test_a_refused_reload_frees_its_slot_the_other_one_is_loaded(self):
        r = self.one("w_swap", self.HELD, refuse={"A/a.wav"})              # a goes to 9 and is refused
        self.assertEqual(r["model"]["slots"], {"3": "B/b.wav"})
        self.assertEqual([(f["slot"], f["path"], f["op"]) for f in r["refused"]], [(9, "A/a.wav", "static.swap")])
        r = self.one("w_move", self.HELD, refuse={"A/a.wav"})              # a moves to 5, is refused: it is lost
        self.assertEqual(r["model"]["slots"], {"9": "B/b.wav"})

    def test_a_busy_card_stops_the_run_before_any_change(self):
        r = self.one("w_swap", self.HELD, busy={"static.swap"})
        self.assertEqual((r["busy"], r["model"]["slots"]), (True, {"3": "A/a.wav", "9": "B/b.wav"}))

    def test_the_shuffle_loop_permutes_the_leading_run_only(self):
        held = {1: "A/1.wav", 2: "A/2.wav", 3: "A/3.wav", 4: "A/4.wav", 5: "A/5.wav", 7: "Z/7.wav"}   # 6 is free
        seen = set()
        for seed in range(1, 13):
            r = self.one("w_shuffle", held, seed=seed)
            sl = r["model"]["slots"]
            self.assertEqual(sorted(sl), ["1", "2", "3", "4", "5", "7"], seed)         # the same slots
            self.assertEqual(sorted(sl[str(k)] for k in range(1, 6)), sorted(held[k] for k in range(1, 6)), seed)
            self.assertEqual(sl["7"], "Z/7.wav")                                       # past the first free slot: untouched
            seen.add(tuple(sl[str(k)] for k in range(1, 6)))
        self.assertGreater(len(seen), 3)                                               # and it really permutes

    def test_a_swap_without_its_second_load_is_caught(self):
        sab = cvm.CVM(build_lib("_otc_sabotage_swap", defines=("OTC_SABOTAGE_SWAP",)))
        self.one("w_swap", self.HELD)
        with self.assertRaises(AssertionError):
            press_both(self, sab, self.SRC, "w_swap", tree_setup("five", slots=self.HELD))


class TestCardText(unittest.TestCase):
    """The fake card and slots speak what the on-unit card implementation speaks: UTF-8 for the C, Latin-1 behind it."""

    def test_host_card_goes_15_folders_down_and_not_further(self):
        hc = cvm.HostCard(tree_root("depth17"))
        deep = hc.listing("AUDIO/DP", True)
        self.assertEqual(max(n.count("/") for n in deep), 15)
        self.assertEqual(len(deep), 4)
        self.assertEqual(hc.listing("AUDIO/DP", False), ["f0.wav"])

    def test_host_card_folder_above_latin1_is_absent(self):
        hc = cvm.HostCard(tree_root("accents"))
        self.assertEqual(hc.dir_open("AUDIO/AC/Caf\u00e9", 1), 0)
        self.assertEqual(hc.dir_open("AUDIO/AC/\u65e5\u672c", 1), -1)

    def test_host_slots_store_latin1_only(self):
        hs = cvm.HostSlots()
        self.assertEqual(hs.set(0, "../AUDIO/\u00e9clat.wav"), 0)
        self.assertEqual(hs.set(1, "../AUDIO/\u65e5\u672c.wav"), -1)           # above U+00FF: refused, nothing stored
        self.assertEqual(hs.set(2, "../AUDIO/\u00ff.wav"), 0)
        self.assertEqual(sorted(hs.final()), [1, 3])
        self.assertEqual(hs.name(0), len("../AUDIO/\u00e9clat.wav".encode("utf-8")))     # slot_name = the UTF-8 length
        hs2 = cvm.HostSlots(store_limit=5)
        self.assertEqual((hs2.set(0, "abcde"), hs2.set(1, "abcd\u00e9"), hs2.set(2, "abc\u00e9")), (0, -1, 0))   # 259 bytes of UTF-8 in the on-unit card implementation


class TestCardBusy(CardBase):
    """CARD BUSY: the card is asked at every access that has work to do; a busy answer stops the run with
    the simulator's CardBusy.result (everything done before stays done)."""

    BODY = {"dir": 'dir "AUDIO/A"', "static.load": 'dir "AUDIO/A"\nstatic.load 1 1', "static.clr": "static.clr 1",
            "static.swap": "static.swap 1 2"}

    def run_busy(self, body, busy, slots=None, card="bags"):
        src = "def f\n  x 5\n" + "".join("  %s\n" % l for l in body.split("\n")) + "  y 7\n"
        return press_both(self, self.cv, src, "f", tree_setup(card, slots=slots, busy=busy), label="busy " + str(busy))[0]

    def test_every_card_op_stops_at_its_access(self):
        for op, body in self.BODY.items():
            slots = {1: "A/a.wav", 2: "A/b.wav"}
            r = self.run_busy(body, {op}, slots)
            self.assertTrue(r["busy"], op)
            self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (5, 0), op)     # what ran before stays done
            r = self.run_busy(body, {"tr.add"}, slots)                                # another op: not busy
            self.assertFalse(r["busy"], op)
            self.assertEqual(r["globals"]["y"], 7, op)

    def test_nothing_is_undone(self):
        r = self.run_busy('dir "AUDIO/A"\nstatic.load 1 1\nstatic.load 2 2\nstatic.clr 1\nx 9',
                          lambda op: op == "STATIC.CLR")
        self.assertTrue(r["busy"])
        self.assertEqual((r["model"]["slots"], r["globals"]["x"]), ({"1": "AUDIO/A/a.wav", "2": "AUDIO/A/b.wav"}, 5))

    def test_the_hook_is_asked_for_every_access_and_only_for_them(self):
        body = ('dir "AUDIO/A"\nstatic.load 1 1\nx static.free 1\ny static.firstfree\nslots.add 1\nstatic.swap 1 2\nstatic.swap 1 1\n'
                'static.swap 3 4\nstatic.clr 2\nstatic.clr 1\ndir "AUDIO/A"\nz file.len')
        src = "def f\n" + "".join("  %s\n" % l for l in body.split("\n"))
        _p, c, table = compile_sources([("T.OTY", src)])
        asked = []
        sim.simulate(vm.OtbProgram(table), "f", seed=1, card=tree_root("five"), run_class=vm.VMRun,
                     busy=lambda op: asked.append(op) or False)
        self.assertEqual(asked, ["DIR", "STATIC.LOAD", "STATIC.SWAP", "STATIC.CLR", "DIR"])
        self.assertEqual(self.cv.load(c.data), 0)
        hs = self.cv.bind_card(tree_root("five"))
        self.cv.run("f", 1)
        self.assertEqual(hs.asked, asked)

    def test_no_work_means_no_card_access(self):
        busy = {"static.load", "static.clr"}
        r = self.run_busy('static.load 1 1\nstatic.clr 4\ndir "AUDIO/A"\nstatic.clr 4', busy)
        self.assertFalse(r["busy"])
        self.assertEqual(len(r["out_of_range"]), 1)                    # static.load without a list
        r = self.run_busy('dir "AUDIO/A"\nstatic.load 1 9', {"static.load"})        # file out of range: oor
        self.assertFalse(r["busy"])
        r = self.run_busy("static.clr 1", {"static.clr"})                           # empty slot: no access
        self.assertFalse(r["busy"])

    def test_busy_set_is_case_insensitive(self):
        for busy in ({"DIR"}, {"dir"}):
            self.assertTrue(self.run_busy('dir "AUDIO/A"', busy)["busy"])

    def test_the_busy_run_still_exports_the_bags_and_warnings(self):
        r = self.run_busy('dir "AUDIO/A"\nz file.pick\ndir "AUDIO/F0"\ny file.pick\nstatic.clr 1', {"static.clr"}, {1: "A/a.wav"})
        self.assertTrue(r["busy"])
        self.assertEqual(len(r["state"]["bags"]), 2)
        self.assertNotEqual(r["globals"]["y"], 7)            # `y 7` after the busy access never ran

    def test_a_walker_that_reports_busy_in_the_middle_stops_the_run(self):
        # (no simulator counterpart: its accesses are asked up front) the walker may answer -2 at any step
        src = 'def f\n  x 5\n  dir "AUDIO/A"\n  y 7\n'
        _p, c, table = compile_sources([("T.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        hs = self.cv.bind_card(cvm.HostCard(tree_root("five"), busy_after=2))
        r = self.cv.run("f", 1)
        self.assertTrue(r["busy"])
        self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (5, 0))
        self.assertEqual(hs.final(), {})
        self.cv.bind_card(cvm.HostCard(tree_root("five"), busy_after=None))
        self.assertEqual(self.cv.run("f", 1)["globals"]["y"], 7)           # and the same card, not busy
        hc = cvm.HostCard(tree_root("five"))                                # the end of the walk is not busy
        hc.busy_after = 5
        self.cv.bind_card(hc)
        self.assertEqual(self.cv.run("f", 1)["globals"]["y"], 7)


class TestCardBudget(CardBase):
    """The live lists share 4096 name slots (one own base per frame at most, a callee's copy costs
    nothing until it runs its own `dir`); slots come back on replacement and on return."""

    SRC = """
def b_callee
  dir "AUDIO/B"
  y file.len
  static.load 1 file.at 1096
  static.load 2 file.at 1097
def b_main
  dir "AUDIO/A"
  b_callee
  x file.len
def b_h1
  b_h2
  y file.len
def b_h2
  b_h3
  z file.len
def b_h3
  t file.len
  dir "AUDIO/B"
  static.load 1 file.at 1096
  static.load 2 file.at 1097
def b_copy
  dir "AUDIO/A"
  b_h1
def b_return
  dir "AUDIO/A"
  b_ret
  b_ret2
def b_ret
  dir "AUDIO/B"
  x file.len
def b_ret2
  dir "AUDIO/B"
  y file.len
def b_replace
  dir "AUDIO/A"
  dir "AUDIO/B"
  x file.len
  b_rep
def b_rep
  dir "AUDIO/A"
  y file.len
def b_exhausted
  dir "AUDIO/C"
  b_ex
def b_ex
  dir "AUDIO/D"
  x file.len
def b_long
  dir "AUDIO/A"
  b_long2
def b_long2
  dir "AUDIO/E"
  x file.len
def b_deep3
  dir "AUDIO/A"
  b_deep3b
  dir "AUDIO/D"
  y file.len
def b_deep3b
  dir "AUDIO/B"
  x file.len
  b_deep3c
def b_deep3c
  dir "AUDIO/E"
  z file.len
def b_filters
  dir "AUDIO/A"
  ext wav
  b_fl
  x file.len
def b_fl
  match "a00*"
  y file.len
  dir "AUDIO/B"
  match "b0001*"
  z file.len
  t file.pick
"""

    def one(self, name, presses=1):
        return press_both(self, self.cv, self.SRC, name, tree_setup("budget"), presses=presses)

    def test_a_callee_dir_gets_what_the_callers_list_leaves(self):
        r = self.one("b_main")[0]
        self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (3000, 1096))
        self.assertEqual(r["warnings"], ['dir "AUDIO/B": 2000 files, only the first 1096 kept'])
        self.assertEqual(r["model"]["slots"], {"1": "AUDIO/B/b1095.wav"})        # the FIRST names are kept
        self.assertEqual(len(r["out_of_range"]), 1)

    def test_a_copy_costs_nothing_until_the_callee_runs_dir(self):
        r = self.one("b_copy")[0]
        self.assertEqual((r["globals"]["y"], r["globals"]["z"], r["globals"]["t"]), (3000, 3000, 3000))
        self.assertEqual(r["warnings"], ['dir "AUDIO/B": 2000 files, only the first 1096 kept'])
        self.assertEqual(r["model"]["slots"], {"1": "AUDIO/B/b1095.wav"})

    def test_slots_come_back_when_the_frame_returns(self):
        r = self.one("b_return")[0]
        self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (1096, 1096))

    def test_slots_come_back_when_the_own_base_is_replaced(self):
        r = self.one("b_replace")[0]
        self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (2000, 2096))
        self.assertEqual(r["warnings"], ['dir "AUDIO/A": 3000 files, only the first 2096 kept'])

    def test_an_exhausted_budget_keeps_nothing_and_the_warning_says_so(self):
        r = self.one("b_exhausted")[0]
        self.assertEqual(r["globals"]["x"], 0)           # 4096 held by the caller: AUDIO/D has 1 file, 0 kept
        self.assertEqual(r["warnings"], ['dir "AUDIO/D": 1 files, only the first 0 kept'])

    def test_long_names_are_removed_before_the_budget_cut(self):
        r = self.one("b_long")[0]
        self.assertEqual(r["warnings"], ['dir "AUDIO/E": 1 names longer than 127 characters skipped',
                                         'dir "AUDIO/E": 1100 files, only the first 1096 kept'])

    def test_three_levels(self):
        r = self.one("b_deep3")[0]
        # A (3000) in the caller, B cut to 1096 in the callee, E gets nothing left; the caller's own D then
        # replaces A and fits
        self.assertEqual((r["globals"]["x"], r["globals"]["y"], r["globals"]["z"]), (1096, 1, 0))
        self.one("b_deep3", presses=3)

    def test_filters_do_not_free_slots(self):
        for r in self.one("b_filters", presses=3):
            self.assertEqual(r["globals"]["x"], 3000)       # the callee's filters do not reach the caller

class TestCardBinding(CardBase):
    """The workspace binding: without it the card operations stop the run with OTC_E_UNSUPPORTED (so every
    table without them runs exactly as without a binding); what a press starts with; slots_touched; sizes."""

    OPS = {"dir": 'dir "A"', "ext": "ext wav", "match": 'match "a*"', "file.len": "x file.len", "file.rnd": "x file.rnd",
           "file.pick": "x file.pick", "file.at": "x file.at 1", "static.load": "static.load 1 1",
           "static.free": "x static.free 1", "static.first": "x static.firstfree", "static.clr": "static.clr 1",
           "slots.clr": "slots.clr", "slots.add": "slots.add 1", "slots.rnd": "x slots.rnd"}

    def test_unbound_card_operations_are_unsupported(self):
        src = "".join("def u_%s\n  %s\n" % (n.replace(".", "_"), b) for n, b in self.OPS.items()) + "def free\n  x 3\n"
        _p, c, _t = compile_sources([("U.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        for n in self.OPS:
            with self.subTest(n):
                self.assertEqual(self.cv.run("u_" + n.replace(".", "_"), 1), ("error", cvm.E_UNSUPPORTED))
        self.assertEqual(self.cv.run("free", 1)["globals"]["x"], 3)
        self.cv.bind_card(tree_root("one"))
        for n in self.OPS:
            with self.subTest("bound " + n):
                r = self.cv.run("u_" + n.replace(".", "_"), 1)
                self.assertNotIsInstance(r, tuple, n)
        self.cv.unbind_card()
        self.assertEqual(self.cv.run("u_dir", 1), ("error", cvm.E_UNSUPPORTED))

    def test_the_binding_survives_the_c_load_and_a_second_bind_keeps_the_bags(self):
        src = 'def f\n  dir "AUDIO/A"\n  x file.pick\n'
        _p, c, _t = compile_sources([("U.OTY", src)])
        cv = self.cv
        self.assertEqual(cv.load(c.data), 0)
        cv.bind_card(tree_root("five"))
        self.assertEqual(len(cv.run("f", 1)["state"]["bags"][0]["rest"]), 4)
        cv.bind_card(tree_root("five"))                                     # the same area: the bag stays
        self.assertEqual(len(cv.run("f", 1)["state"]["bags"][0]["rest"]), 3)
        L = cv.lib
        self.assertEqual(L.otc_vm_load(cv.ptr, cv.data, len(c.data)), 0)    # the C load keeps the binding, empties the bags
        self.assertEqual(len(cv.run("f", 1)["state"]["bags"][0]["rest"]), 4)
        other = (ctypes.c_uint64 * ((cv.ws_size + 7) // 8))()
        L.otc_vm_bind_work(cv.ptr, ctypes.addressof(other))                  # another area: its own (empty) bags
        self.assertEqual(len(cv.run("f", 1)["state"]["bags"][0]["rest"]), 4)
        L.otc_vm_bind_work(cv.ptr, None)
        self.assertEqual(cv.run("f", 1), ("error", cvm.E_UNSUPPORTED))
        cv.bind_card(tree_root("five"))

    def test_a_press_starts_with_an_empty_list_and_working_set(self):
        src = ('def f\n  x file.len\n  dir "AUDIO/A"\n  y file.len\n  slots.add 5\n  z slots.rnd\n'
               'def g\n  x file.len\n  y slots.rnd\n')
        rs = press_both(self, self.cv, src, "f", tree_setup("five"), presses=2)
        self.assertEqual((rs[1]["globals"]["x"], rs[1]["globals"]["y"], rs[1]["globals"]["z"]), (0, 5, 5))
        self.assertEqual(rs[1]["model"]["working_set"], [5])
        c = compile_sources([("U.OTY", src)])[1]
        self.assertEqual(self.cv.load(c.data), 0)
        self.cv.bind_card(tree_root("five"))
        self.cv.run("f", 1)
        r = self.cv.run("g", 1)                                             # the next press: nothing left over
        self.assertEqual((r["globals"]["x"], r["globals"]["y"], r["working_set"]), (0, 0, []))

    def test_slots_touched(self):
        src = ('def t_a\n  x static.free 1\n  y static.firstfree\n  static.clr 4\n  slots.add 2\n'
               'def t_b\n  dir "AUDIO/A"\n  static.load 1 1\n'
               'def t_c\n  static.clr 1\n'
               'def t_d\n  dir "AUDIO/A"\n  static.load 9 1\n')
        _p, c, _t = compile_sources([("U.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        for fn, slots, refuse, want in (("t_a", None, None, 0), ("t_b", None, None, 1), ("t_c", None, None, 0),
                                        ("t_c", {1: "x/y.wav"}, None, 1), ("t_d", None, {"AUDIO/A/a.wav"}, 1),
                                        ):
            hs = self.cv.bind_card(tree_root("five"), slots, refuse)
            r = self.cv.run(fn, 1)
            self.assertEqual((r["slots_touched"], self.cv.slots_touched()), (want, want), (fn, slots))
            self.assertEqual(hs.mutated(), bool(want))

    def test_a_chain_of_eight_calls_each_with_its_own_list(self):
        src = ('def top\n  dir "AUDIO/F0"\n  l1\n  x file.len\n'
               'def l1\n  ext wav\n  l2\n  y file.len\n'
               'def l2\n  match "a*"\n  l3\n  z file.len\n'
               'def l3\n  dir "AUDIO/F1"\n  l4\n  t file.len\n'
               'def l4\n  match "b*"\n  l5\n  a file.len\n'
               'def l5\n  dir "AUDIO/A"\n  l6\n  b file.len\n'
               'def l6\n  ext aif\n  l7\n  c file.len\n'
               'def l7\n' + '  l 1 128:\n    if gt i file.len: break\n    a static.firstfree\n    if ez a: break\n    static.load a file.at i\n' + '  d file.len\n')
        r = press_both(self, self.cv, src, "top", tree_setup("bags"), presses=2)[1]
        self.assertEqual([r["globals"][k] for k in "xyzt"], [2, 2, 1, 2])
        self.assertEqual(len(r["model"]["slots"]), 0)             # l7 filled from an `ext aif` list: nothing there

    def test_sizes_and_constants(self):
        cv = self.cv
        self.assertGreater(cv.ws_size, 4096 * 128)                # the names arena alone is 512 KiB
        self.assertLess(cv.ws_size, 700 * 1024)
        self.assertLess(cv.size, 40000)                           # otc_vm_t grew by one pointer
        with open(os.path.join(CSRC, "otc_vm.h")) as fh:
            h = fh.read()
        import re
        for name, val in (("OTC_MAX_REFUSED", cvm.MAX_REFUSED), ("OTC_MAX_DIRWARN", cvm.MAX_DIRWARN),
                          ("OTC_BAGS_MAX", O.PICK_BAGS_MAX), ("OTC_NAMES_MAX", O.DIR_LIST_MAX),
                          ("OTC_NAME_MAX", O.DIR_NAME_MAX), ("OTC_E_BUSY", cvm.E_BUSY)):
            self.assertEqual(int(re.search(r"#define %s\s+(\d+)" % name, h).group(1)), val, name)
        self.assertEqual(int(re.search(r"#define OTC_NAME_BYTES\s+(\d+)", h).group(1)), O.DIR_NAME_MAX + 1)


class TestCardMustFail(CardBase):
    """The card tests have teeth: the library built with one wrong rule each (OTC_SABOTAGE_*) is caught.
    The sabotages: the sort without the case fold; a dot name kept;
    bags that do not persist / refill too early; a 4095-name cap; no list copy on
    a call; static.load without the `../`; a refusal ignored."""

    def must_fail(self, define, scenario):
        scenario(self, self.cv)                                   # the good library passes it
        sab = cvm.CVM(build_lib("_" + define.lower(), defines=(define,)))
        with self.assertRaises(AssertionError, msg=define):
            scenario(self, sab)

    @staticmethod
    def tree_run(kind, only, seeds=(1, 2), setup=None, src=None):
        def run(tc, cv):
            _p, c, table = compile_sources([("C.OTY", src or template(kind))])
            compare_table(tc, cv, table, c.data, Stats(), setup=setup or tree_setup(kind), seeds=seeds, only=only)
        return run

    def test_sort_without_the_case_fold(self):
        self.must_fail("OTC_SABOTAGE_SORT", self.tree_run("mixed", {"C_FILLONLY", "C_FILL"}))

    def test_a_dot_name_kept(self):
        self.must_fail("OTC_SABOTAGE_DOT", self.tree_run("dots", {"C_BASIC", "C_FILLONLY"},
                                                         setup=tree_setup("dots", leak_dots=True)))

    def test_bags_that_do_not_persist(self):
        self.must_fail("OTC_SABOTAGE_BAG=1", self.tree_run("bags", {"PICKN"}, src=BAG_SRC))

    def test_a_bag_refilled_too_early(self):
        self.must_fail("OTC_SABOTAGE_BAG=2", self.tree_run("bags", {"PICKN", "PICK5"}, src=BAG_SRC))

    def test_a_cap_of_4095(self):
        self.must_fail("OTC_SABOTAGE_CAP", self.tree_run("big4097", {"C_BASIC"}, seeds=(1,)))

    def test_no_list_copy_on_a_call(self):
        self.must_fail("OTC_SABOTAGE_COPY", self.tree_run("mixed", {"C_CALL"}))

    def test_slot_load_without_the_parent_prefix(self):
        self.must_fail("OTC_SABOTAGE_SLOTPATH", self.tree_run("five", {"C_LOAD", "C_FILLONLY"}))

    def test_a_refusal_ignored(self):
        def run(tc, cv):
            press_both(tc, cv, TestCardRefusals.SRC, "r_fill", tree_setup("five", refuse={"AUDIO/A/b.wav"}))
        self.must_fail("OTC_SABOTAGE_REFUSE", run)


# ---- page functions -----------------------------------------------------------------------------------
PAGE_PARAMS_SRC = textwrap.dedent("""\
    def pga
      page "PA"
      param p1 4 0 16
      param p2 2 -3 9
      param p3 0 0 3
      l 1 p1:
        if mod i add p2 4: tr.add i
        every 2: x add x 1
      y p2
      z p3
      t p1
    def pgb
      page "CALLER"
      param q 3 0 9
      pgc
      x q
    def pgc
      page "CALLEE"
      param w 5 0 9
      y w
      each:
        prob 30: tr.add i
    def pgd
      page "NOPAR"
      tr.add 5
    """)


class TestPage(Base):
    """`otc_vm_set_params` / `otc_vm_fn_page` / `otc_vm_fn_param` against the Python VM (`--param`), and the loader's
    checks of the PAGE bit and of the aux tail."""

    @classmethod
    def setUpClass(cls):
        Base.setUpClass()
        _p, cls.c_ex, cls.t_ex = compile_sources([("PAGE.OTY", otype.read_text(os.path.join(ROOT, "modules", "otype", "examples", "PAGE.OTY")))])
        _p, cls.c_pa, cls.t_pa = compile_sources([("P.OTY", PAGE_PARAMS_SRC)])
        assert not cls.c_pa.diags and cls.t_pa.nfunc == 4
        print("[cvm] sizeof(otc_vm_t) on the host: %d bytes (the firmware reserves 38912)" % cls.cv.size)

    def bound(self, table_bytes):
        """Load a table and bind the empty pattern (tr.add needs the tracks); returns 0 or the loader's error."""
        r = self.cv.load(table_bytes)
        if r == 0:
            Start().bind(self.cv, {})
        return r

    def raw_load(self, data):
        """otc_vm_load on bytes that otb.parse (the wrapper's load) may refuse: the C loader's verdict alone."""
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        self._keep = buf
        return self.cv.lib.otc_vm_load(self.cv.ptr, buf, len(data))

    def test_queries_read_the_table(self):
        self.assertEqual(self.cv.load(self.c_pa.data), 0)
        self.assertEqual([self.cv.fn_page(i) for i in range(4)], ["PA", "CALLER", "CALLEE", "NOPAR"])
        self.assertEqual(self.cv.fn_page("pga"), "PA")
        self.assertIsNone(self.cv.fn_page(9))
        for f in self.t_pa.funcs:
            want = [(n, d, lo, hi) for n, (d, lo, hi) in f.params.items()]
            self.assertEqual(self.cv.fn_params(f.index), want, f.name)
        self.assertEqual(self.cv.fn_params(77), [])
        # a table without any page function: nothing is a page
        _p, c, t = compile_sources([("M.OTY", 'def m\n  menu "A" >> "B"\n  param p 1 0 5\n  tr.add p\n')])
        self.assertEqual(self.cv.load(c.data), 0)
        self.assertEqual((self.cv.fn_page(0), self.cv.fn_params(0)), (None, [("P", 1, 0, 5)]))

    def test_example_pages_have_the_params_the_spec_names(self):
        self.assertEqual(self.cv.load(self.c_ex.data), 0)
        self.assertEqual(self.cv.fn_page("eucl"), "EUCL")
        self.assertEqual(self.cv.fn_page("pdens"), "DENS")
        self.assertEqual(self.cv.fn_params("eucl"), [("PL1", 4, 0, 64), ("PL2", 0, 0, 64), ("OP", 0, 0, 3),
                                                     ("R1", 0, -63, 63), ("R2", 0, -63, 63), ("TRO", 0, -63, 63)])

    def test_set_params_equals_the_python_vm_param(self):
        vprog = vm.OtbProgram(self.t_pa)
        self.assertEqual(self.cv.load(self.c_pa.data), 0)
        st = Stats()
        cases = [{}, {"p1": 7}, {"p1": 99}, {"P2": -9}, {"p1": 0, "p2": 9, "p3": 3}, {"p2": 5}, {"p3": 2}, {"p1": 16, "p2": -3},
                 {"p1": 5, "p2": 1, "p3": -4}]
        for params in cases:
            compare_function(self, self.cv, vprog, "pga", (0, 1, 2, 3), st, ((1, 16), (3, 12)), params=params)
        self.cv.set_params([])
        self.assertGreater(st.runs, 50)

    def test_only_the_root_function_sees_the_params(self):
        vprog = vm.OtbProgram(self.t_pa)
        self.assertEqual(self.cv.load(self.c_pa.data), 0)
        st = Stats()
        for params in ({"q": 8}, {"q": 1}, {}):
            compare_function(self, self.cv, vprog, "pgb", (0, 1, 2), st, ((1, 16),), params=params)
        # the callee ran with ITS default (5), whatever the caller was given
        self.cv.set_params([8])
        g = self.cv.run("pgb", 1)["globals"]
        self.assertEqual((g["y"], g["x"]), (5, 8))
        self.cv.set_params([])

    def test_set_params_clamps_clears_and_persists(self):
        self.assertEqual(self.bound(self.c_pa.data), 0)
        self.cv.set_params([100, -100, 50])
        g = self.cv.run("pga", 1)["globals"]
        self.assertEqual((g["y"], g["z"], g["t"]), (-3, 3, 16))                # min..max of each
        self.assertEqual(self.cv.run("pga", 1)["globals"]["t"], 16)             # still imposed on the next run
        self.cv.set_params([7])
        g = self.cv.run("pga", 1)["globals"]
        self.assertEqual((g["t"], g["y"], g["z"]), (7, 2, 0))                   # the others keep their default
        self.cv.set_params([])
        g = self.cv.run("pga", 1)["globals"]
        self.assertEqual((g["t"], g["y"], g["z"]), (4, 2, 0))
        # more values than params, or than the cap: ignored
        self.cv.set_params(list(range(1, 12)))
        self.assertEqual(self.cv.run("pga", 1)["globals"]["t"], 1)
        self.cv.set_params([])
        # a load clears them
        self.cv.set_params([9])
        self.assertEqual(self.bound(self.c_pa.data), 0)
        self.assertEqual(self.cv.run("pga", 1)["globals"]["t"], 4)

    def test_example_eucl_with_params_equals_the_python_vm(self):
        vprog = vm.OtbProgram(self.t_ex)
        self.assertEqual(self.cv.load(self.c_ex.data), 0)
        st = Stats()
        for params in ({"pl1": 5, "pl2": 3, "op": 1}, {"pl1": 9, "pl2": 5, "op": 3, "r1": 1, "r2": 4, "tro": 3},
                       {"pl1": 7, "op": 2, "r1": -63, "tro": -63}, {"pl2": 6, "op": 0, "r2": 63}, {"pl1": 80, "op": 9}):
            compare_function(self, self.cv, vprog, "eucl", (1,), st, ((1, 16), (2, 12), (1, 64)), params=params)
        for params in ({"pct": 0}, {"pct": 100}, {"pct": 40}):
            compare_function(self, self.cv, vprog, "pdens", (1, 2, 3), st, ((1, 16),), params=params)
        self.cv.set_params([])

    # ---- the loader refuses a corrupt page table ---------------------------------------------------------
    def table(self, extra=""):
        _p, c, t = compile_sources([("P.OTY", 'def f\n  page "PAGE"\n  param p 1 0 5\n  tr.add p\n' + extra)])
        return bytearray(c.data), t

    def test_loader_rejects_corrupt_page_tables(self):
        d, t = self.table()
        self.assertEqual(self.raw_load(d), 0)
        ent = t.ftab_off                               # the one function's entry
        aux = t.aux_off
        tail = aux + 8                                 # the label offset (one param first)
        self.assertEqual(int.from_bytes(d[ent + 18:ent + 20], "big"), 0x80)
        lab = int.from_bytes(d[tail:tail + 2], "big")
        for what, patch in (
                ("label offset 0", lambda b: b.__setitem__(slice(tail, tail + 2), b"\x00\x00")),
                ("label offset past the pool", lambda b: b.__setitem__(slice(tail, tail + 2), (t.pool_len + 8).to_bytes(2, "big"))),
                ("label offset on a name that is not a string",
                 lambda b: b.__setitem__(slice(tail, tail + 2), (lab + 2).to_bytes(2, "big"))),
                ("label of 9 characters", lambda b: b.__setitem__(t.pool_off + lab + 1, 9)),
                ("empty label", lambda b: b.__setitem__(t.pool_off + lab + 1, 0)),
                ("label with a control character", lambda b: b.__setitem__(t.pool_off + lab + 2, 0x07)),
                ("label with a non-ASCII byte", lambda b: b.__setitem__(t.pool_off + lab + 3, 0xE9)),
                ("aux region without room for the tail", lambda b: b.__setitem__(slice(18, 20), (8).to_bytes(2, "big"))),
                ("a page function with an argument", lambda b: b.__setitem__(ent + 32, 1)),
                ("seven params", lambda b: b.__setitem__(ent + 33, 7))):
            bad = bytearray(d)
            patch(bad)
            self.assertEqual(self.raw_load(bad), cvm.E_TABLE, what)
        # the same table without the bit is a table the VM (and an old VM) loads as before, tail or not
        ok = bytearray(d)
        ok[ent + 18:ent + 20] = b"\x00\x00"
        self.assertEqual(self.raw_load(ok), 0)
        self.assertEqual(self.cv.lib.otc_vm_fn_page(self.cv.ptr, 0, None), 0)
        self.assertEqual(self.raw_load(d), 0)
        self.assertEqual(self.cv.lib.otc_vm_fn_page(self.cv.ptr, 0, None), 4)


class TestConfirm(Base):
    """`confirm "TEXT"` (spec 14.1): `otc_vm_fn_confirm` reads what `otb.parse` reads, the loader checks the flag bit 2 and the
    text's offset after the page label."""

    def raw_load(self, data):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        self._keep = buf
        return self.cv.lib.otc_vm_load(self.cv.ptr, buf, len(data))

    def test_queries_read_the_table(self):
        src = ('def fa\n  menu "P" >> "CLEAR"\n  confirm "CLEAR POOL?"\n  tr.clr\n'
               'def fb\n  menu "P" >> "FILL"\n  confirm\n  tr.add 1\n'
               'def fc\n  menu "T" >> "CLEAR"\n  tr.clr\n'
               'def fd\n  menu "A" >> "B"\n  page "PG"\n  confirm ""\n  confirm "PAGE?"\n  param p 1 0 5\n'
               '  l 1 4:\n    every 2: tr.add i\n'
               'def fe | n\n  confirm "' + "~" * 116 + '"\n  tr.add n\n')
        _p, c, t = compile_sources([("C.OTY", src)])
        self.assertEqual((c.diags, t.nfunc), ([], 5))
        self.assertEqual(self.cv.load(c.data), 0)
        got = [self.cv.fn_confirm(i) for i in range(t.nfunc)]
        self.assertEqual(got, [f.confirm_text for f in t.funcs])
        self.assertEqual(got, ["CLEAR POOL?", None, None, "PAGE?", "~" * 116])
        self.assertEqual(self.cv.fn_page("fd"), "PG")                  # the page label is still read before it
        self.assertIsNone(self.cv.fn_confirm(t.nfunc))
        self.assertEqual(self.cv.lib.otc_vm_fn_confirm(self.cv.ptr, 0, None), 11)
        # the corpus family: every function of every table, the C query = the Python decoder
        for k in range(0, 40):
            src = "".join('def f%d\n  menu "M" >> "A%d"\n%s  tr.add 1\n' % (j, j, '  confirm "Q%d?"\n' % j if (j * k) % 3 else "")
                          for j in range(k % 7 + 1))
            _p, c, t = compile_sources([("C.OTY", src)])
            self.assertEqual(self.cv.load(c.data), 0)
            self.assertEqual([self.cv.fn_confirm(i) for i in range(t.nfunc)], [f.confirm_text for f in t.funcs], k)

    def table(self):
        _p, c, t = compile_sources([("C.OTY", 'def f\n  menu "A" >> "B"\n  page "PG"\n  confirm "SURE?"\n  param p 1 0 5\n  tr.add p\n')])
        return bytearray(c.data), t

    def test_loader_rejects_corrupt_texts(self):
        d, t = self.table()
        self.assertEqual(self.raw_load(d), 0)
        ent = t.ftab_off
        tail = t.aux_off + 8 + 2                       # one param, then the page label, then the text
        self.assertEqual(d[ent + 23] & 4, 4)
        txt = int.from_bytes(d[tail:tail + 2], "big")
        self.assertEqual(t.pool.string(txt), "SURE?")
        lab = int.from_bytes(d[tail - 2:tail], "big")
        for what, patch in (
                ("text offset 0", lambda b: b.__setitem__(slice(tail, tail + 2), b"\x00\x00")),
                ("text offset past the pool", lambda b: b.__setitem__(slice(tail, tail + 2), (t.pool_len + 8).to_bytes(2, "big"))),
                ("text offset on the menu list", lambda b: b.__setitem__(slice(tail, tail + 2), d[ent + 16:ent + 18])),
                ("empty text", lambda b: b.__setitem__(t.pool_off + txt + 1, 0)),
                ("text running past the pool", lambda b: b.__setitem__(t.pool_off + txt + 1, 127)),
                ("text with a control character", lambda b: b.__setitem__(t.pool_off + txt + 2, 0x07)),
                ("text with a non-ASCII byte", lambda b: b.__setitem__(t.pool_off + txt + 3, 0xE9)),
                ("aux region without room for the text", lambda b: b.__setitem__(slice(18, 20), (10).to_bytes(2, "big")))):
            bad = bytearray(d)
            patch(bad)
            self.assertEqual(self.raw_load(bad), cvm.E_TABLE, what)
        # the label is read before the text: swapping them makes the loader read the text as the label (5 > 2, still valid)
        sw = bytearray(d)
        sw[tail - 2:tail], sw[tail:tail + 2] = d[tail:tail + 2], d[tail - 2:tail]
        self.assertEqual(self.raw_load(sw), 0)
        self.assertEqual((self.cv.fn_page(0), self.cv.fn_confirm(0)), ("SURE?", "PG"))
        # without the bit: the table an old VM reads, no text
        ok = bytearray(d)
        ok[ent + 23] &= ~4
        self.assertEqual(self.raw_load(ok), 0)
        self.assertEqual(self.cv.lib.otc_vm_fn_confirm(self.cv.ptr, 0, None), 0)
        self.assertEqual(self.cv.fn_page(0), "PG")
        self.assertEqual(self.raw_load(d), 0)
        self.assertEqual(self.cv.fn_confirm(0), "SURE?")
        del lab


class TestMustFail(Base):
    """The comparison has teeth: a C VM with one wrong rule (MOD truncating like C; tr.del forgetting to
    clear the condition of the word) is caught."""

    def test_sabotaged_remove_is_detected(self):
        sab = cvm.CVM(build_lib("_sab", sabotage=True))
        prog, c, table = compile_sources([("P.OTY", PATTERN_SRC)])
        start = Start(random_bank(1), 1, "random1")
        with self.assertRaises(AssertionError) as cm:
            compare_table(self, sab, table, c.data, Stats(), start=start, only={"CLR", "DEL_SOME"})
        self.assertIn("track", str(cm.exception))        # the bytes of a track differ, nothing else first
        self.assertNotIn("globals", str(cm.exception))
        compare_table(self, self.cv, table, c.data, Stats(), start=start, only={"CLR", "DEL_SOME"})
        # the three explicit rules catch it too: removing a trig must clear the condition
        b = blank()
        set_bit(b[1], TRIG, 2)
        put_word(b[1], 2, (2 << 13) | (5 << 7) | 19)
        _p, c2, _t = compile_sources([("E.OTY", "def f\n  tr.del 2\n")])
        self.assertEqual(sab.load(c2.data), 0)
        for t in range(1, 9):
            sab.bind(t, b[t] if t == 1 else blank()[t])
        sab.run("f", 0)
        self.assertEqual(word_at(sab.blocks()[1], 2), (2 << 13) | (5 << 7) | 19)   # the wrong result

    def test_sabotaged_mod_is_detected(self):
        sab = cvm.CVM(build_lib("_sab", sabotage=True))
        src = "def f\n  x mod -7 3\n  y mod 8 3\n  z mod -1 -5\n  t mod 7 -3\n"
        prog, c, table = compile_sources([("S.OTY", src)])
        st = Stats()
        with self.assertRaises(AssertionError) as cm:
            compare_table(self, sab, table, c.data, st)
        self.assertIn("globals", str(cm.exception))      # it is the MOD results that differ, nothing else
        # the same script on the good library agrees
        st = Stats()
        compare_table(self, self.cv, table, c.data, st)
        self.assertEqual(st.funcs, 1)
        # and the generated EXTRA grid finds it too (any function using a negative dividend)
        _prog, c, table = compile_sources(extra_sources()[0])
        with self.assertRaises(AssertionError):
            compare_table(self, sab, table, c.data, Stats())


# ==================================================================================================
# the p-locks and the trigless locks (pl.set pl.get pl.del pl.rnd pl.vary pl.clr tl.add tl.fill)
# ==================================================================================================
# Both engines start from the same pattern AND the same part: the simulator's ParamModel (descriptor range /
# enabled / value per (track, parameter)) and, on the C side, its host twin cvm.HostParams answering the two
# otb_host_param_* imports.  Nothing here is a value of the firmware's descriptor table: the ranges are the tests' own.
AMP_ATK, AMP_HOLD, AMP_REL, AMP_VOL, AMP_BAL = 12, 13, 14, 15, 16        # flat parameters (sim.param_name)

M3A_SRC = """def pset
  l 1 16: pl.set i amp.vol mul i 9
def pset_trk
  trk 1 3
  l 1 20: pl.set i amp.atk add i 100
def pset_slots
  l 1 16: pl.set i add 6 mod i 3 sub 0 i
def prnd
  pl.rnd amp.vol
def prnd_all
  trk all
  steps 3 11
  pl.rnd amp.hold
def prnd_pb
  trk 2 5
  pl.rnd pb 1
def pvary
  pl.vary amp.vol 25
def pvary_big
  trk 2 3
  pl.vary amp.atk 100
def pvary_zero
  pl.vary amp.hold 0
def pdel
  l 1 16: pl.del i amp.vol
def pclr
  pl.clr
def pclr_all
  trk all
  pl.clr
def tadd
  l 1 16: if mod i 3: tl.add i
def tfill
  tl.fill 40
def tfill_all
  trk all
  tl.fill 100
def pget
  x pl.get 3 amp.vol
  y pl.get 2 amp.atk
  z pl.get 4 pb 2
  t pl.get 9 amp.bal
def chain
  tl.fill 50
  pl.rnd amp.vol
  pl.vary amp.vol 10
  l 1 16: pl.set i amp.rel 33
  pl.clr
"""


def pm_of(info=None, values=None):
    return sim.ParamModel(info, values)


def random_pmodel(seed):
    """A part whose descriptors differ everywhere: disabled slots, ranges that start high, count 0 or huge (normalised
    by the C and by sim.param_norm), values up to 16 bits and negative."""
    r = random.Random(seed)
    info, values = {}, {}
    for t in range(1, 9):
        for p in range(30):
            k = r.random()
            if k < 0.15:
                info[(t, p)] = (0, 128, False)
            elif k < 0.6:
                info[(t, p)] = (r.choice((0, 0, 5, 20, 100, 200, 254, 300)),
                                r.choice((0, 1, 2, 16, 64, 128, 255, 300, 0xFFFFFFFF)), True)
            if r.random() < 0.4:
                values[(t, p)] = r.choice((0, 1, 64, 127, 200, 40000, -5, -70000))
    return sim.ParamModel(info, values)


def agree(tc, cv, src, blocks, pmodel=None, mtypes=None, track=1, seeds=(0, 1, 2, 5, 9), name="F", length=16):
    """The simulator and `cv` on the pattern of `blocks` ({track: RAM block}) and the part `pmodel`: the whole
    comparison of compare_table (model, bytes, lists, draws, cost, touched)."""
    path = bank_from_blocks("m3a", blocks)
    _p, c, table = compile_sources([("M.OTY", src)])
    st = Stats()
    compare_table(tc, cv, table, c.data, st, variants=((track, length, mtypes or {}),), only={name.upper()},
                  start=Start(path, 1, "m3a", pmodel), seeds=seeds)
    tc.assertEqual(st.funcs, 1, "the function was not run")


class TestM3a(Base):
    """Exact bytes, lists and import calls of the p-lock operations, and the simulator agrees."""

    def go(self, src, blocks=None, pmodel=None, mtypes=None, track=1, seed=0, with_copy=False, name="f"):
        blocks = blocks if blocks is not None else blank()
        _p, c, _t = compile_sources([("M.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        for t, b in blocks.items():
            self.cv.bind(t, b, (mtypes or {}).get(t, 0), copy=with_copy)
        self.cv.bind_params(cvm.HostParams(copy.deepcopy(pmodel if pmodel is not None else sim.ParamModel())))
        r = self.cv.run(name, seed, track, 16)
        self.assertFalse(isinstance(r, tuple), r)
        return r, self.cv.blocks()

    def agrees(self, src, blocks, pmodel=None, **kw):
        agree(self, self.cv, src, blocks, pmodel, **kw)

    @staticmethod
    def trigged(*steps, kind=TRIG, track=1, b=None):
        b = b or blank()
        for s in steps:
            set_bit(b[track], kind, s)
        return b

    # ---- pl.set ---------------------------------------------------------------------------------------------
    def test_pl_set_writes_byte_p_clamped_into_the_descriptor_range(self):
        b = self.trigged(1, 2, 3)
        set_bit(b[1], TLOCK, 4)                                  # a trigless lock carries locks too
        pm = pm_of({(1, AMP_VOL): (10, 50, True)})               # amp.vol on track 1: 10..59; amp.atk: neutral 0..127
        src = ("def f\n  pl.set 1 amp.vol 3\n  pl.set 2 amp.vol 100\n  pl.set 3 amp.vol 30\n  pl.set 4 amp.vol 59\n"
               "  pl.set 5 amp.vol 30\n  pl.set 1 amp.atk 200\n  pl.set 3 amp.atk -4\n")
        r, after = self.run_with(src, b, pm)
        a = after[1]
        self.assertEqual([rec_at(a, s)[AMP_VOL] for s in range(1, 6)], [10, 59, 30, 59, 0xFF])
        self.assertEqual((rec_at(a, 1)[AMP_ATK], rec_at(a, 3)[AMP_ATK]), (127, 0))
        for s in range(1, 6):                                    # nothing else of any record
            self.assertEqual([x for k, x in enumerate(rec_at(a, s)) if k not in (AMP_VOL, AMP_ATK)], [0xFF] * 30)
        self.assertEqual(r["skipped_no_trig"], [{"func": "f", "op": "pl.set", "line": 6}])
        self.assertEqual((r["out_of_range"], r["touched"]), ([], {1}))
        self.assertEqual(self.cv.params.log, [("info", 0, AMP_VOL)] * 5 + [("info", 0, AMP_ATK)] * 2)   # once per call and track
        self.agrees(src, self.trigged(1, 2, 3, b=self.with_tlock(4)), pm)

    @staticmethod
    def with_tlock(s):
        b = blank()
        set_bit(b[1], TLOCK, s)
        return b

    def run_with(self, src, b, pm, **kw):
        return self.go(src, b, pm, **kw)

    def test_a_slot_the_part_disables_is_refused_and_counted_before_the_step_is_looked_at(self):
        pm = pm_of({(1, AMP_VOL): (0, 128, False), (2, AMP_VOL): (0, 128, False)})
        b = self.trigged(1)
        src = ("def f\n  pl.set 1 amp.vol 5\n  pl.set 3 amp.vol 5\n  pl.rnd amp.vol\n  pl.vary amp.vol 20\n"
               "  pl.set 1 amp.atk 5\n  pl.del 1 amp.vol\n")
        r, after = self.run_with(src, b, pm)
        a = after[1]
        self.assertEqual(rec_at(a, 1)[AMP_VOL], 0xFF)
        self.assertEqual(rec_at(a, 1)[AMP_ATK], 5)                               # another slot works
        self.assertEqual([(o["op"], o["line"]) for o in r["out_of_range"]],
                         [("pl.set", 2), ("pl.set", 3), ("pl.rnd", 4), ("pl.vary", 5)])   # counted as out of range
        self.assertEqual(r["skipped_no_trig"], [])                                # step 3 has no trig: the slot came first
        self.assertEqual(r["draws"]["rand"], 0)                                   # refused before any step: no draw
        self.agrees(src, self.trigged(1), pm)

    def test_a_disabled_slot_of_one_track_only(self):
        pm = pm_of({(2, AMP_VOL): (0, 128, False)})
        b = self.trigged(1)
        self.trigged(1, track=2, b=b)
        src = "def f\n  trk 1 2\n  pl.set 1 amp.vol 5\n  pl.rnd amp.vol\n"
        r, after = self.run_with(src, b, pm)
        self.assertTrue(0 <= rec_at(after[1], 1)[AMP_VOL] <= 127)                 # track 1: pl.set 5, then pl.rnd on its trig
        self.assertEqual(rec_at(after[2], 1)[AMP_VOL], 0xFF)                      # track 2: both refused
        self.assertEqual([(o["op"], o["line"]) for o in r["out_of_range"]], [("pl.set", 3), ("pl.rnd", 4)])
        self.assertEqual(r["draws"]["rand"], 16)                                  # track 1 draws on all its steps, track 2 none
        b2 = self.trigged(1)
        self.trigged(1, track=2, b=b2)
        self.agrees(src, b2, pm)

    def test_the_normalisation_of_a_descriptor_range(self):
        b = self.trigged(1)
        pm = pm_of({(1, AMP_VOL): (300, 0, True),                 # lo above 254 -> 254, count 0 -> 1
                    (1, AMP_ATK): (250, 100, True),               # the range stops at 254 (255 is "no lock")
                    (1, AMP_HOLD): (0, 0xFFFFFFFF, True),         # a huge count -> 0..254
                    (1, AMP_REL): (10, 0, True)})                 # count 0 -> a single value
        src = "def f\n  pl.set 1 amp.vol 5\n  pl.set 1 amp.atk 255\n  pl.set 1 amp.hold 255\n  pl.set 1 amp.rel 99\n"
        _r, after = self.run_with(src, b, pm)
        self.assertEqual([rec_at(after[1], 1)[p] for p in (AMP_VOL, AMP_ATK, AMP_HOLD, AMP_REL)], [254, 254, 254, 10])
        self.agrees(src, self.trigged(1), pm)

    # ---- pl.get ---------------------------------------------------------------------------------------------
    def test_pl_get_answers_the_lock_else_the_part(self):
        b = self.trigged(1)
        put_rec(b[1], 1, lock_rec(AMP_VOL, 77))
        pm = pm_of(values={(1, AMP_VOL): 99, (1, AMP_HOLD): 12, (1, AMP_BAL): 70000, (1, AMP_ATK): -3})
        src = ("def f\n  x pl.get 1 amp.vol\n  y pl.get 2 amp.vol\n  z pl.get 2 amp.hold\n  t pl.get 2 amp.bal\n")
        r, _after = self.run_with(src, b, pm)
        self.assertEqual(r["globals"], {"x": 77, "y": 99, "z": 12, "t": 32767})     # the lock; the part; the part; saturated
        self.assertEqual(self.cv.params.log, [("value", 0, AMP_VOL), ("value", 0, AMP_HOLD), ("value", 0, AMP_BAL)])
        self.agrees(src, b, pm)
        # the neutral part: the simulator's PARAM_DEFAULT; a PICKUP track still reads; a bad step or parameter is out of range
        src2 = "def f\n  x pl.get 2 amp.vol\n  y pl.get 2 amp.rel\n  z pl.get add 35 35 amp.vol\n  t pl.get 2 amp.atk\n"
        r, _a = self.run_with(src2, blank(), None, mtypes={1: PICKUP})
        self.assertEqual(r["globals"], {"x": sim.PARAM_DEFAULT[AMP_VOL], "y": sim.PARAM_DEFAULT[AMP_REL], "z": 0, "t": 0})
        self.assertEqual([o["op"] for o in r["out_of_range"]], ["pl.get"])
        self.agrees(src2, blank(), None, mtypes={1: PICKUP})
        self.agrees("def f\n  trk 3\n  x pl.get 2 amp.vol\n  y pl.get 1 amp.atk\n", b, pm)

    # ---- pl.del / pl.clr ------------------------------------------------------------------------------------
    def test_pl_del_and_pl_clr_touch_the_p_lock_bytes_only(self):
        b = self.trigged(1, 2)
        for s in (1, 2, 3):
            r_ = bytearray(b"\xff" * 32)
            r_[AMP_VOL], r_[AMP_ATK], r_[30], r_[31] = 40 + s, 50 + s, 0x55, s
            put_rec(b[1], s, bytes(r_))
        src = "def f\n  pl.del 1 amp.vol\n  pl.del 3 amp.atk\n  steps 2 3\n  pl.clr\n"
        r, after = self.run_with(src, b, None)
        a = after[1]
        want1 = bytearray(b"\xff" * 32)
        want1[AMP_ATK], want1[30], want1[31] = 51, 0x55, 1
        self.assertEqual(rec_at(a, 1), bytes(want1))                         # one byte gone, the rest kept
        for s in (2, 3):
            self.assertEqual(rec_at(a, s), b"\xff" * 30 + bytes([0x55, s]))  # 30 p-lock bytes gone; spare + sample lock kept
        self.assertEqual((r["skipped_no_trig"], r["out_of_range"], r["touched"]), ([], [], {1}))
        self.assertEqual(self.cv.params.log, [])                             # neither op asks the part
        self.agrees(src, b)
        self.agrees("def f\n  trk all\n  pl.clr\n  pl.del 2 amp.vol\n", b, mtypes={1: PICKUP, 4: 1})

    # ---- pl.rnd / pl.vary -----------------------------------------------------------------------------------
    def test_pl_rnd_draws_on_every_step_then_checks_the_step(self):
        b = self.trigged(2, 5, 9)
        pm = pm_of({(1, AMP_VOL): (20, 30, True)})
        src = "def f\n  pl.rnd amp.vol\n"
        for seed in (0, 1, 2, 3, 7):
            r, after = self.run_with(src, self.trigged(2, 5, 9), pm, seed=seed)
            a = after[1]
            vals = {s: rec_at(a, s)[AMP_VOL] for s in range(1, 17)}
            for s in range(1, 17):
                if s in (2, 5, 9):
                    self.assertTrue(20 <= vals[s] <= 49, (seed, s, vals[s]))
                else:
                    self.assertEqual(vals[s], 0xFF)
            self.assertEqual(r["draws"]["rand"], 16)                          # not 3: the position of the flow does not depend on the trigs
            self.assertEqual(len(r["skipped_no_trig"]), 13)
            self.assertEqual(self.cv.params.log, [("info", 0, AMP_VOL)])      # the descriptor once, not per step
        self.agrees(src, b, pm, seeds=(0, 1, 2, 3, 7, 11))
        self.agrees("def f\n  trk all\n  steps 3 11\n  pl.rnd amp.hold\n", b, pm_of({(5, AMP_HOLD): (0, 2, True)}),
                    mtypes={3: PICKUP})

    def test_pl_vary_draws_where_the_lock_is_held_and_keeps_inside_the_range(self):
        b = self.trigged(1, 2, 3)
        put_rec(b[1], 1, lock_rec(AMP_VOL, 100))
        put_rec(b[1], 2, lock_rec(AMP_VOL, 5))
        put_rec(b[1], 6, lock_rec(AMP_VOL, 60))                              # a lock on a step with no trig
        src = "def f\n  pl.vary amp.vol 20\n"
        pm = pm_of({(1, AMP_VOL): (50, 40, True)})                          # 50..89: 100 is pulled in, d = (39*20+50)//100 = 8
        for seed in (0, 1, 2, 3, 4, 5):
            r, after = self.run_with(src, b, pm, seed=seed)
            a = after[1]
            self.assertEqual(r["draws"]["rand"], 3)                          # steps 1, 2 and 6 hold the lock; step 3 holds none
            self.assertEqual(rec_at(a, 3)[AMP_VOL], 0xFF)                    # an absent lock stays absent: no draw, no count
            self.assertEqual(rec_at(a, 6)[AMP_VOL], 60)                      # no trig: skipped and counted
            self.assertEqual(r["skipped_no_trig"], [{"func": "f", "op": "pl.vary", "line": 2}])
            self.assertTrue(50 <= rec_at(a, 1)[AMP_VOL] <= 89 and 50 <= rec_at(a, 2)[AMP_VOL] <= 89)
        r, after = self.run_with("def f\n  pl.vary amp.vol 0\n", b, pm)
        self.assertEqual((rec_at(after[1], 1)[AMP_VOL], rec_at(after[1], 2)[AMP_VOL]), (89, 50))   # x = 0: only the range acts
        self.agrees(src, b, pm, seeds=range(8))
        self.agrees("def f\n  pl.vary amp.vol 100\n", b, pm_of({(1, AMP_VOL): (0, 128, True)}), seeds=range(8))

    # ---- tl.add / tl.fill -----------------------------------------------------------------------------------
    def test_tl_add_sets_the_trigless_lock_bit_and_zeroes_the_word_of_a_step_without_trig(self):
        b = self.trigged(2)
        self.trigged(4, kind=TLOCK, b=b)
        self.trigged(5, kind=ONESHOT, b=b)
        w = (1 << 13) | (5 << 7) | 19
        for s in (2, 3, 4, 5, 6):
            put_word(b[1], s, w)
        put_rec(b[1], 3, lock_rec(AMP_VOL, 9))
        src = "def f\n  tl.add 2\n  tl.add 3\n  tl.add 4\n  tl.add 5\n  tl.add 6\n  tl.add 3\n"
        r, after = self.run_with(src, b, None)
        a = after[1]
        self.assertEqual([get_bit(a, TLOCK, s) for s in range(1, 8)], [0, 1, 1, 1, 1, 1, 0])
        self.assertEqual([get_bit(a, TRIG, s) for s in range(1, 8)], [0, 1, 0, 0, 0, 0, 0])      # no other mask changes
        self.assertEqual([get_bit(a, ONESHOT, s) for s in range(1, 8)], [0, 0, 0, 0, 1, 0, 0])
        self.assertEqual([word_at(a, s) for s in (2, 3, 4, 5, 6)], [w, 0, w, w, 0])               # only a step with no trig is zeroed
        self.assertEqual(rec_at(a, 3), lock_rec(AMP_VOL, 9))                                    # the record is never touched
        self.assertEqual((r["skipped_no_trig"], r["out_of_range"], r["touched"]), ([], [], {1}))
        self.assertEqual(self.cv.params.log, [])
        # a lock now lands on step 3 (it carries a trigless lock) ...
        r, after = self.run_with("def f\n  tl.add 3\n  pl.set 3 amp.vol 5\n", blank(), None)
        self.assertEqual((rec_at(after[1], 3)[AMP_VOL], r["skipped_no_trig"]), (5, []))
        # ... and a PICKUP track refuses it
        b = blank()
        r, after = self.run_with("def f\n  tl.add 3\n", b, None, mtypes={1: PICKUP})
        self.assertEqual((after[1], len(r["skipped_pickup"])), (b[1], 1))
        self.agrees(src, self.rebuilt(b_steps=(2,), tl=(4,), one=(5,), w=w, locks3=True), None)

    def rebuilt(self, b_steps, tl, one, w, locks3=False):
        b = self.trigged(*b_steps)
        self.trigged(*tl, kind=TLOCK, b=b)
        self.trigged(*one, kind=ONESHOT, b=b)
        for s in (2, 3, 4, 5, 6):
            put_word(b[1], s, w)
        if locks3:
            put_rec(b[1], 3, lock_rec(AMP_VOL, 9))
        return b

    def test_tl_fill_fills_free_steps_only_and_draws_on_every_step(self):
        b = self.trigged(1)
        self.trigged(2, kind=TLOCK, b=b)
        self.trigged(3, kind=ONESHOT, b=b)
        self.trigged(4, kind=TLESS, b=b)
        put_word(b[1], 6, 0x1234 & 0x7FFF)
        r, after = self.run_with("def f\n  tl.fill 100\n", b, None)
        a = after[1]
        self.assertEqual([get_bit(a, TLOCK, s) for s in range(1, 17)], [0, 1, 0, 0] + [1] * 12)   # 1, 3, 4 carry a trig: not free
        self.assertEqual([get_bit(a, TRIG, s) for s in range(1, 5)], [1, 0, 0, 0])
        self.assertEqual(word_at(a, 6), 0)                                   # the stale word of a free step is cleared
        self.assertEqual(r["draws"]["rand"], 16)
        r, after = self.run_with("def f\n  tl.fill 0\n", b, None)
        self.assertEqual((after[1], r["draws"]["rand"], r["touched"]), (b[1], 16, set()))
        r, after = self.run_with("def f\n  trk 1 2\n  tl.fill 100\n", b, None, mtypes={2: PICKUP})
        self.assertEqual((r["draws"]["rand"], len(r["skipped_pickup"])), (16, 1))   # the PICKUP track draws nothing
        self.assertEqual(after[2], blank()[2])
        for seed in (0, 1, 2, 3):
            r, after = self.run_with("def f\n  tl.fill 40\n", b, None, seed=seed)
        self.agrees("def f\n  tl.fill 40\n", b, None, seeds=range(8))
        self.agrees("def f\n  trk all\n  tl.fill 100\n", b, None, mtypes={2: PICKUP, 7: PICKUP})

    # ---- the copy, the binding, the list caps, a random start -------------------------------------------------
    def test_every_write_also_goes_to_the_copy(self):
        b = self.trigged(1, 3)
        src = "def f\n  pl.set 1 amp.vol 9\n  pl.rnd amp.atk\n  tl.add 8\n  pl.del 3 amp.vol\n  steps 1 1\n  pl.clr\n"
        r, after = self.run_with(src, b, None, with_copy=True)
        self.assertEqual(self.cv.copy_blocks()[1], after[1])
        self.assertNotEqual(after[1], b[1])

    def test_unbound_tracks_stop_the_run(self):
        _p, c, _t = compile_sources([("M.OTY", "def f\n  pl.set 1 amp.vol 5\ndef g\n  x pl.get 1 amp.vol\ndef h\n  tl.add 1\n")])
        self.assertEqual(self.cv.load(c.data), 0)
        for fn in ("f", "g", "h"):
            self.assertEqual(self.cv.run(fn, 0), ("error", cvm.E_UNSUPPORTED))

    def test_pattern_script_on_random_patterns_and_random_parts(self):
        prog, c, table = compile_sources([("M.OTY", M3A_SRC)])
        st = Stats()
        for seed in (1, 2, 3):
            start = Start(random_bank(seed), 1, "random%d" % seed, random_pmodel(seed))
            compare_table(self, self.cv, table, c.data, st, variants=((1, 16), (4, 64), (6, 7)), start=start)
        self.assertEqual(st.skipped, 0)
        self.assertEqual(st.funcs, 3 * 17)
        self.assertGreater(st.skips, 100)
        self.assertGreater(st.pickups, 50)
        self.assertGreater(st.writing, 300)

    def test_a_real_looking_start_with_neutral_parts(self):
        _p, c, table = compile_sources([("M.OTY", M3A_SRC)])
        st = Stats()
        compare_table(self, self.cv, table, c.data, st, variants=((1, 16), (2, 64, {2: PICKUP})))
        self.assertEqual((st.skipped, st.funcs), (0, 17))


class TestM3aMustFail(Base):
    """The p-lock tests have teeth: the library built with one wrong rule each (OTC_SABOTAGE_PL*, TLWORD) is caught
    by the comparison with the simulator."""

    def must_fail(self, define, scenario):
        scenario(self, self.cv)                                   # the good library passes it
        sab = cvm.CVM(build_lib("_" + define.lower(), defines=(define,)))
        with self.assertRaises(AssertionError, msg=define):
            scenario(self, sab)

    def test_pl_set_clamping_to_a_fixed_range(self):
        def run(tc, cv):
            b = blank()
            set_bit(b[1], TRIG, 1)
            agree(tc, cv, "def f\n  pl.set 1 amp.vol 150\n  pl.set 1 amp.atk 3\n", b, pm_of({(1, AMP_VOL): (10, 200, True)}))
        self.must_fail("OTC_SABOTAGE_PLCLAMP", run)

    def test_a_disabled_slot_written_anyway(self):
        def run(tc, cv):
            b = blank()
            set_bit(b[1], TRIG, 1)
            agree(tc, cv, "def f\n  pl.set 1 amp.vol 9\n  pl.rnd amp.vol\n  pl.vary amp.vol 5\n", b,
                  pm_of({(1, AMP_VOL): (0, 128, False)}))
        self.must_fail("OTC_SABOTAGE_PLENABLED", run)

    def test_pl_get_without_the_part_value(self):
        def run(tc, cv):
            agree(tc, cv, "def f\n  x pl.get 3 amp.vol\n", blank(), pm_of(values={(1, AMP_VOL): 99}))
        self.must_fail("OTC_SABOTAGE_PLGET", run)

    def test_tl_add_leaving_the_stale_word(self):
        def run(tc, cv):
            b = blank()
            put_word(b[1], 3, (1 << 13) | (5 << 7) | 19)
            agree(tc, cv, "def f\n  tl.add 3\n", b)
        self.must_fail("OTC_SABOTAGE_TLWORD", run)



# ---- the part operations ---------------------------------------------------------------
M3B_SRC = """def pset
  par.set amp.vol 100
  par.set amp.atk 400
  par.set lfo.spd 1 -9
def pset_trk
  trk 1 3 6
  par.set pb 2 add trk.cur 60
def lrnd
  l 6 11: par.set i rrand 0 127
def lrnd_all
  trk all
  l 6 11: par.set i rrand 0 127
def ldest
  lfo.dest 1 amp.vol
  lfo.dest 3 fx2 6
  lfo.dest 4 amp.vol
  lfo.dest 0 pb 1
def frnd
  par.set fx1 2 50
  fx.type 1 rrand 1 10
  x pl.get 1 fx1 2
def fset
  fx.type 1 4
  fx.type 2 0
  fx.type 3 1
  fx.type 1 99
  fx.type 2 14
def fset_trk
  trk 2 5
  fx.type 1 rnd 12
def sgen
  scn.clr 1
  l 2 16:
    l 6 11:
      scn.set j i rrand 0 127
    l 18 29:
      scn.set j i rrand 0 127
def sgen_all
  trk all
  scn.clr 1
  l 2 3: scn.set j 6 rrand 0 127
def sclr
  scn.set 2 6 5
  scn.clr
def tinit
  par.set amp.vol 9
  lfo.dest 2 fx1 3
  scn.set 2 6 5
  trk.init
  x pl.get 1 amp.vol
def tinit_all
  trk all
  trk.init
def pmix
  l 1 4: par.set rnd 30 rnd 200
  lfo.dest rrand 0 4 rnd 30
  fx.type rrand 0 3 rnd 16
  y pl.get 2 lfo.dep 2
"""


def random_pmodel_m3b(seed):
    """random_pmodel + what the part ops read: defaults (any int32), chooser lengths (0, 1, small, huge), factory
    effects (inside and outside the chooser), a waveform count (0, small, huge)."""
    pm = random_pmodel(seed)
    r = random.Random(seed * 7 + 1)
    for t in range(1, 9):
        for p in range(30):
            if r.random() < 0.3:
                pm.defaults[(t, p)] = r.choice((0, 5, 64, 127, 300, -2, 0x7FFFFFFF, -0x80000000))
    for slot in (1, 2):
        if r.random() < 0.7:
            pm.fx_count[slot] = r.choice((0, 1, 2, 5, 11, 15, 300, 0xFFFFFFFF))
        if r.random() < 0.7:
            pm.fx_default[slot] = r.choice((0, 1, 3, 14, 0xFFFFFFFF))
    pm.lfo_waves = r.choice((None, 0, 1, 7, 19, 1000))
    return pm


class TestM3b(Base):
    """Exact import calls of the part operations, and the simulator agrees (part, part_touched, oor, draws)."""

    def go(self, src, pmodel=None, name="f", track=1, seed=0):
        _p, c, _t = compile_sources([("M.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        for t in range(1, 9):
            self.cv.bind(t, blank()[t], 0)
        hp = self.cv.bind_params(cvm.HostParams(copy.deepcopy(pmodel if pmodel is not None else sim.ParamModel())))
        r = self.cv.run(name, seed, track, 16)
        self.assertFalse(isinstance(r, tuple), r)
        return r, hp

    def agrees(self, src, pmodel=None, **kw):
        agree(self, self.cv, src, blank(), pmodel, **kw)

    def writes(self, hp):
        return [e for e in hp.log if e[0] in PART_WRITES]

    def test_par_set_hands_the_clamped_value_to_the_stock_writer(self):
        pm = pm_of({(1, AMP_VOL): (10, 50, True), (1, AMP_ATK): (0, 128, False)})
        src = "def f\n  par.set amp.vol 3\n  par.set amp.vol 99\n  par.set amp.atk 5\n  par.set lfo.dep 1 200\n"
        r, hp = self.go(src, pm)
        self.assertEqual(self.writes(hp), [("par_set", 0, AMP_VOL, 10), ("par_set", 0, AMP_VOL, 59),
                                           ("par_set", 0, 9, 127)])
        self.assertEqual([(o["op"], o["line"]) for o in r["out_of_range"]], [("par.set", 4)])   # the disabled slot
        self.assertEqual((r["part_touched"], r["touched"]), ({1}, set()))
        self.agrees(src, pm)

    def test_fx_ops_read_the_chooser_at_runtime(self):
        pm = sim.ParamModel(fx_count={1: 1, 2: 300})
        r, hp = self.go("def f\n  fx.type 2 254\n  fx.type 2 255\n  fx.type 1 0\n  fx.type 1 1\n", pm)
        w = self.writes(hp)
        self.assertEqual(w, [("fx_set", 0, 1, 254), ("fx_set", 0, 0, 0)])    # 300 -> 255 entries; FX1 holds one
        self.assertEqual([o["line"] for o in r["out_of_range"]], [3, 5])
        self.agrees("def f\n  fx.type 2 254\n  fx.type 2 255\n  fx.type 1 0\n  fx.type 1 1\n", pm)

    def test_par_min_max_fx_count_are_pure_getters_of_the_descriptor(self):
        pm = pm_of({(1, AMP_VOL): (10, 50, True), (1, AMP_ATK): (0, 128, False), (1, AMP_REL): (0, 128, True)})
        pm.fx_count = {1: 1, 2: 300}
        src = ("def f\n  x par.min amp.vol\n  y par.max amp.vol\n  z par.min amp.rel\n  t par.max amp.rel\n"
               "def g\n  x par.min amp.atk\n  y par.max amp.atk\n  z fx.count 1\n  t fx.count 2\n"
               "def h\n  a 3\n  x fx.count a\n  a 0\n  y fx.count a\n")
        r, hp = self.go(src, pm, "f")
        self.assertEqual([r["globals"][k] for k in "xyzt"], [10, 59, 0, 127])
        r, hp = self.go(src, pm, "g")
        self.assertEqual([r["globals"][k] for k in "xyzt"], [0, 0, 1, 255])   # a disabled slot: par.get's refusal, 0; 300 -> 255
        self.assertEqual([(o["op"], o["line"]) for o in r["out_of_range"]], [("par.min", 7), ("par.max", 8)])
        self.assertEqual((self.writes(hp), r["part_touched"], r["touched"], r["draws"]["rand"]), ([], set(), set(), 0))
        r, hp = self.go(src, pm, "h")
        self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (0, 0))      # 3 and 0 are not choosers
        self.assertEqual([(o["op"], o["line"]) for o in r["out_of_range"]], [("fx.count", 13), ("fx.count", 15)])
        for fn in "fgh":
            self.agrees(src, pm, name=fn)

    def test_par_min_max_on_the_context_track_and_random_parts(self):
        src = ("def f\n  trk 3\n  l 0 29:\n    a par.min i\n    b par.max i\n    x add x a\n    y add y b\n"
               "  z fx.count 1\n  t fx.count 2\n")
        for seed in (1, 2, 3, 4, 5):
            agree(self, self.cv, src, blank(), random_pmodel(seed), track=2)
            agree(self, self.cv, src, blank(), random_pmodel(seed), track=3)

    def test_scn_clr_bytes(self):
        r, hp = self.go("def f\n  trk 4\n  scn.clr\n")
        self.assertEqual([e for e in hp.log if e[0] == "scene"],
                         [("scene", 3, s, b, 0xFF) for s in range(16) for b in range(32)])
        self.assertEqual(r["part_touched"], {4})

    def test_trk_init_resets_order(self):
        pm = sim.ParamModel({(1, 0): (0, 128, False), (1, AMP_VOL): (10, 20, True)},
                            defaults={(1, AMP_VOL): 90, (1, 3): -4}, fx_count={2: 2}, fx_default={1: 7, 2: 2})
        r, hp = self.go("def f\n  trk.init\n", pm)
        w = self.writes(hp)
        self.assertEqual(w[:2], [("fx_set", 0, 0, 7), ("slot_init", 0)])     # FX1 factory; FX2's 2 is not in its 2 entries
        ps = [e for e in w if e[0] == "par_set"]
        self.assertEqual([e[2] for e in ps], list(range(1, 30)))              # every enabled slot, in order
        self.assertEqual((ps[2][3], ps[AMP_VOL - 1][3]), (0, 29))             # defaults clamped into the range
        self.assertEqual(w[2 + 29:2 + 29 + 6], [("lfo", 0, x, k, 0) for x in range(3) for k in (0, 1)])
        self.assertEqual(len(w), 2 + 29 + 6 + 16 * 32)
        self.assertEqual((r["out_of_range"], r["draws"]["rand"]), ([], 0))
        self.agrees("def f\n  par.set amp.vol 15\n  fx.type 1 2\n  trk.init\n  x pl.get 1 amp.vol\n", pm)

    def test_pl_get_reads_the_part_the_run_wrote(self):
        src = "def f\n  par.set amp.vol 77\n  x pl.get 3 amp.vol\n  par.set fx2 1 9\n  fx.type 2 3\n  y pl.get 3 fx2 1\n"
        r, _hp = self.go(src, pm_of(values={(1, 24): 55}))
        self.assertEqual((r["globals"]["x"], r["globals"]["y"]), (77, 55))   # the apply forgot fx2 1: the model's again
        self.agrees(src, pm_of(values={(1, 24): 55}))

    def test_no_pickup_gate_and_no_pattern_binding_needed(self):
        _p, c, _t = compile_sources([("M.OTY", "def f\n  par.set amp.vol 5\n  scn.clr\n")])
        self.assertEqual(self.cv.load(c.data), 0)                            # nothing bound: the part ops still run
        hp = self.cv.bind_params(cvm.HostParams())
        r = self.cv.run("f", 0)
        self.assertEqual(r["part_touched"], {1})
        self.assertEqual(hp.parts[1].params, {AMP_VOL: 5})
        self.agrees("def f\n  par.set amp.vol 5\n  lfo.dest 1 amp.vol\n  trk.init\n", None, mtypes={1: PICKUP})

    def test_part_script_on_random_parts(self):
        _prog, c, table = compile_sources([("M.OTY", M3B_SRC)])
        st = Stats()
        for seed in (1, 2, 3, 4):
            start = Start(random_bank(seed), 1, "random%d" % seed, random_pmodel_m3b(seed))
            compare_table(self, self.cv, table, c.data, st, variants=((1, 16), (4, 64), (8, 7)), start=start)
        self.assertEqual((st.skipped, st.funcs), (0, 4 * 14))

    def test_part_script_with_the_neutral_part(self):
        _prog, c, table = compile_sources([("M.OTY", M3B_SRC)])
        st = Stats()
        compare_table(self, self.cv, table, c.data, st, variants=((1, 16), (2, 64, {2: PICKUP})))
        self.assertEqual((st.skipped, st.funcs), (0, 14))


class TestM3bMustFail(Base):
    """The part tests have teeth: one wrong rule each (OTC_SABOTAGE_PARCLAMP ... INITORDER) is caught by the comparison."""

    def must_fail(self, define, src, pm=None):
        def run(tc, cv):
            agree(tc, cv, src, blank(), pm, seeds=(0, 1, 2, 3))
        run(self, self.cv)                                                   # the good library passes it
        sab = cvm.CVM(build_lib("_" + define.lower(), defines=(define,)))
        with self.assertRaises(AssertionError, msg=define):
            run(self, sab)

    def test_par_set_without_the_clamp(self):
        self.must_fail("OTC_SABOTAGE_PARCLAMP", "def f\n  par.set amp.vol 150\n", pm_of({(1, AMP_VOL): (10, 20, True)}))

    def test_a_disabled_slot_written_anyway(self):
        self.must_fail("OTC_SABOTAGE_PARDISABLED", "def f\n  par.set amp.vol 9\n  par.set lfo.spd 1 5\n",
                       pm_of({(1, AMP_VOL): (0, 128, False), (1, 6): (0, 128, False)}))

    def test_trk_init_applying_the_effects_last(self):
        self.must_fail("OTC_SABOTAGE_INITORDER", "def f\n  trk.init\n")


# ---- the trig-word operations ------------------------------------------------------------
M3C_SRC = """def cset
  l 1 16: cond.set i rnd 65
def cset_off
  cond.set 1 off
  cond.set 2 50%
  cond.set 3 3:4
def crnd
  cond.rnd 50% 99%
def crnd_all
  trk all
  steps 3 11
  cond.rnd fill 8:8
def mset
  l 1 16: micro.set i sub mul i 4 32
def mrnd
  micro.rnd 23
def mrnd0
  trk 2 5
  micro.rnd 0
def rset
  l 1 16: ratch.set i sub i 4
def rall
  trk all
  l 1 16: ratch.set i rnd 10
def tw_chain
  tl.fill 30
  cond.rnd 1st !nei
  micro.rnd 9
  l 1 16: ratch.set i 3
  steps has.trig: micro.set step.cur -5
  tr.clr
def tw_del
  l 1 16: tr.flip i
  cond.rnd 0 64
  tr.del 3
  tr.add 3
  cond.set 4 99%
"""


class TestM3c(Base):
    """Exact bytes of the trig-word writes, and the simulator agrees."""

    def go(self, src, blocks=None, mtypes=None, track=1, seed=0, with_copy=False, name="f"):
        blocks = blocks if blocks is not None else blank()
        _p, c, _t = compile_sources([("M.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        for t, b in blocks.items():
            self.cv.bind(t, b, (mtypes or {}).get(t, 0), copy=with_copy)
        self.cv.bind_params(cvm.HostParams())
        r = self.cv.run(name, seed, track, 16)
        self.assertFalse(isinstance(r, tuple), r)
        return r, self.cv.blocks()

    def agrees(self, src, blocks, **kw):
        agree(self, self.cv, src, blocks, None, **kw)

    @staticmethod
    def trigged(*steps, kind=TRIG, track=1, b=None):
        b = b or blank()
        for s in steps:
            set_bit(b[track], kind, s)
        return b

    def test_cond_set_writes_the_low_seven_bits_and_keeps_micro_and_count(self):
        b = self.trigged(1, 2, 3)
        put_word(b[1], 1, (2 << 13) | (0x3F << 7) | 5)           # count 3, micro -1 (bit 7 of the low byte), cond 5
        put_word(b[1], 2, (7 << 13) | (0x21 << 7))
        src = "def f\n  cond.set 1 50%\n  cond.set 2 8:8\n  cond.set 3 off\n  cond.set 1 off\n  cond.set 1 9\n"
        r, after = self.go(src, b)
        self.assertEqual(word_at(after[1], 1), (2 << 13) | (0x3F << 7) | 9)
        self.assertEqual(word_at(after[1], 2), (7 << 13) | (0x21 << 7) | 64)
        self.assertEqual(word_at(after[1], 3), 0)
        self.assertEqual((r["touched"], r["skipped_no_trig"]), ({1}, []))
        b2 = self.trigged(1, 2, 3)
        put_word(b2[1], 1, (2 << 13) | (0x3F << 7) | 5)
        put_word(b2[1], 2, (7 << 13) | (0x21 << 7))
        self.agrees(src, b2)

    def test_a_trig_of_any_kind_carries_the_word_and_a_step_without_one_is_skipped(self):
        b = self.trigged(1)
        set_bit(b[1], TLESS, 2)
        set_bit(b[1], TLOCK, 3)
        set_bit(b[1], ONESHOT, 4)
        src = "def f\n  l 1 5: cond.set i 19\n  l 1 5: micro.set i 3\n  l 1 5: ratch.set i 2\n"
        r, after = self.go(src, b)
        want = (1 << 13) | (3 << 7) | 19
        self.assertEqual([word_at(after[1], s) for s in range(1, 6)], [want] * 4 + [0])
        self.assertEqual([(o["op"], o["line"]) for o in r["skipped_no_trig"]],
                         [("cond.set", 2), ("micro.set", 3), ("ratch.set", 4)])     # step 5, once per op
        b2 = self.trigged(1)
        for k, s in ((TLESS, 2), (TLOCK, 3), (ONESHOT, 4)):
            set_bit(b2[1], k, s)
        self.agrees(src, b2)

    def test_micro_is_six_bits_two_complement_clamped_to_23(self):
        b = self.trigged(1, 2, 3, 4, 5)
        put_word(b[1], 1, (5 << 13) | 0x80 | 19)
        src = "def f\n  micro.set 1 -7\n  micro.set 2 99\n  micro.set 3 -99\n  micro.set 4 23\n  micro.set 5 1\n  micro.set 5 0\n"
        r, after = self.go(src, b)
        self.assertEqual([word_at(after[1], s) for s in range(1, 6)],
                         [(5 << 13) | (57 << 7) | 19, 23 << 7, 41 << 7, 23 << 7, 0])    # -7 = 57, -23 = 41
        b2 = self.trigged(1, 2, 3, 4, 5)
        put_word(b2[1], 1, (5 << 13) | 0x80 | 19)
        self.agrees(src, b2)

    def test_ratch_is_the_count_clamped_to_1_8(self):
        b = self.trigged(1, 2, 3, 4)
        put_word(b[1], 1, (0x2A << 7) | 30)
        src = "def f\n  ratch.set 1 4\n  ratch.set 2 99\n  ratch.set 3 0\n  ratch.set 4 8\n  ratch.set 4 1\n"
        r, after = self.go(src, b)
        self.assertEqual([word_at(after[1], s) for s in range(1, 5)], [(3 << 13) | (0x2A << 7) | 30, 7 << 13, 0, 0])
        b2 = self.trigged(1, 2, 3, 4)
        put_word(b2[1], 1, (0x2A << 7) | 30)
        self.agrees(src, b2)

    def test_the_random_ops_draw_on_every_step_then_check_it(self):
        b = self.trigged(2, 5, 9)
        r, after = self.go("def f\n  cond.rnd 50% 99%\n  micro.rnd 0\n", b)
        self.assertEqual(r["draws"]["rand"], 32)                                 # 16 + 16, trig or not
        self.assertTrue(all(19 <= word_at(after[1], s) & 0x7F <= 29 for s in (2, 5, 9)))
        self.assertEqual(len(r["skipped_no_trig"]), 26)
        self.agrees("def f\n  cond.rnd 99% 50%\n  micro.rnd 6\n", self.trigged(2, 5, 9))

    def test_pickup_tracks_are_skipped_and_counted(self):
        b = self.trigged(1)
        self.trigged(1, track=2, b=b)
        src = "def f\n  trk 1 2\n  cond.set 1 19\n  micro.rnd 5\n  ratch.set 1 3\n"
        r, after = self.go(src, b, mtypes={2: PICKUP})
        self.assertEqual(word_at(after[2], 1), 0)
        self.assertEqual([(o["op"], o["track"]) for o in r["skipped_pickup"]],
                         [("cond.set", 2), ("micro.rnd", 2), ("ratch.set", 2)])
        b2 = self.trigged(1)
        self.trigged(1, track=2, b=b2)
        self.agrees(src, b2, mtypes={2: PICKUP})

    def test_every_write_also_goes_to_the_copy(self):
        b = self.trigged(1, 3)
        r, after = self.go("def f\n  cond.set 1 19\n  micro.set 3 -4\n  ratch.set 3 5\n  cond.rnd 1 64\n", b, with_copy=True)
        self.assertEqual(self.cv.copy_blocks()[1], after[1])
        self.assertNotEqual(after[1], b[1])

    def test_unbound_tracks_stop_the_run(self):
        _p, c, _t = compile_sources([("M.OTY", "def f\n  cond.set 1 19\ndef g\n  micro.rnd 3\ndef h\n  ratch.set 1 2\n")])
        self.assertEqual(self.cv.load(c.data), 0)
        for fn in ("f", "g", "h"):
            self.assertEqual(self.cv.run(fn, 0), ("error", cvm.E_UNSUPPORTED))

    def test_trig_word_script_on_random_patterns(self):
        _prog, c, table = compile_sources([("M.OTY", M3C_SRC)])
        st = Stats()
        for seed in (1, 2, 3):
            start = Start(random_bank(seed), 1, "random%d" % seed)
            compare_table(self, self.cv, table, c.data, st, variants=((1, 16), (4, 64), (6, 7)), start=start)
        self.assertEqual((st.skipped, st.funcs), (0, 3 * 11))
        self.assertGreater(st.skips, 100)
        self.assertGreater(st.pickups, 20)
        self.assertGreater(st.writing, 200)

    def test_trig_word_script_from_an_empty_pattern(self):
        _prog, c, table = compile_sources([("M.OTY", M3C_SRC)])
        st = Stats()
        compare_table(self, self.cv, table, c.data, st, variants=((1, 16), (2, 64, {2: PICKUP})))
        self.assertEqual((st.skipped, st.funcs), (0, 11))


class TestM3cMustFail(Base):
    """The trig-word tests have teeth: one wrong rule each (OTC_SABOTAGE_CONDMASK ... TWLANDS) is caught by the comparison."""

    def must_fail(self, define, src, blocks):
        def run(tc, cv):
            agree(tc, cv, src, copy.deepcopy(blocks), None, seeds=(0, 1, 2))
        run(self, self.cv)                                                   # the good library passes it
        sab = cvm.CVM(build_lib("_" + define.lower(), defines=(define,)))
        with self.assertRaises(AssertionError, msg=define):
            run(self, sab)

    @staticmethod
    def pattern():
        b = blank()
        for s in (1, 2, 3):
            set_bit(b[1], TRIG, s)
        put_word(b[1], 1, (2 << 13) | (0x3F << 7) | 5)
        return b

    def test_a_condition_write_that_loses_micro(self):
        self.must_fail("OTC_SABOTAGE_CONDMASK", "def f\n  cond.set 1 19\n", self.pattern())

    def test_micro_written_without_its_sign(self):
        self.must_fail("OTC_SABOTAGE_MICROSIGN", "def f\n  micro.set 2 -5\n", self.pattern())

    def test_ratch_beyond_eight(self):
        self.must_fail("OTC_SABOTAGE_RATCHCLAMP", "def f\n  ratch.set 2 12\n", self.pattern())

    def test_a_condition_on_a_step_without_trig(self):
        self.must_fail("OTC_SABOTAGE_TWLANDS", "def f\n  cond.set 9 19\n", self.pattern())

class TestDrv(Base):
    """dr.v (tables written 04/10): the C VM = the Python VM on every curve -2..9 and steps -40..40 plus the extremes,
    as globals and as p-lock values; a C table read at s mod 16 instead of (s - 1) mod 16 (OTC_SABOTAGE_DRV) is caught."""

    SRC = ("def g\n  x dr.v y z\n"
           "def f\n" + "".join("  y %d\n  z %d\n  g\n  t mod add mul t 5 x 6000\n" % (c, s)      # a running hash: no saturation
                                 for c in range(-2, 10) for s in list(range(-40, 41)) + [-32768, 32767, 100, 1000])
           + "def p\n  l 1 16:\n    tr.add i\n    pl.set i amp.vol dr.v 6 i\n    pl.set i amp.bal dr.v 7 add i 3\n"
           + "def h\n  x dr.v 3 -32768\n  y dr.v 7 32767\n  z dr.v -32768 5\n  t dr.v 32767 0\n")

    def runs(self, cv):
        prog, c, table = compile_sources([("D.OTY", self.SRC)])
        self.assertEqual([str(d) for d in prog.diags], [])
        st = Stats()
        compare_table(self, cv, table, c.data, st)
        return st

    def test_c_equals_python(self):
        st = self.runs(self.cv)
        self.assertEqual(st.funcs, 4)                     # g (no argument: it runs too, on y = z = 0), f, p, h

class TestTypedParamsCVM(CardBase):
    """CVM test for typed params: bool, option, folder, and dir with folder param override."""

    TYPED_SRC = (
        'def f\n'
        '  menu "SCRIPTS" >> "RUN"\n'
        '  param flag bool 1\n'
        '  param opt option 1 "1/4" "1/2" "1"\n'
        '  param src folder "AUDIO/DRUMS"\n'
        '  param n 40 0 100\n'
        '  x flag\n'
        '  y opt\n'
        '  z n\n'
        '  dir src\n'
        '  t file.len\n'
        '  static.load 1 1\n'
    )

    def test_typed_params_inspection(self):
        _p, c, table = compile_sources([("T.OTY", self.TYPED_SRC)])
        self.assertEqual(self.cv.load(c.data), 0)
        p0 = self.cv.fn_param_info("f", 0)
        self.assertEqual(p0["name"], "FLAG")
        self.assertEqual(p0["kind"], "BOOL")
        self.assertEqual(p0["def"], 1)
        self.assertEqual(p0["lo"], 0)
        self.assertEqual(p0["hi"], 1)

        p1 = self.cv.fn_param_info("f", 1)
        self.assertEqual(p1["name"], "OPT")
        self.assertEqual(p1["kind"], "OPTION")
        self.assertEqual(p1["def"], 1)
        self.assertEqual(p1["lo"], 0)
        self.assertEqual(p1["hi"], 2)
        self.assertEqual(p1["labels"], ["1/4", "1/2", "1"])

        p2 = self.cv.fn_param_info("f", 2)
        self.assertEqual(p2["name"], "SRC")
        self.assertEqual(p2["kind"], "FOLDER")
        self.assertEqual(p2["folder_path"], "AUDIO/DRUMS")

        p3 = self.cv.fn_param_info("f", 3)
        self.assertEqual(p3["name"], "N")
        self.assertEqual(p3["kind"], "INT")
        self.assertEqual(p3["def"], 40)
        self.assertEqual(p3["lo"], 0)
        self.assertEqual(p3["hi"], 100)

    def test_folder_param_dir_default_and_override(self):
        tree = {
            "AUDIO/DRUMS": ["bd.wav", "sd.wav"],
            "AUDIO/SYNTH": ["bass.wav", "lead.wav"],
        }
        with tempfile.TemporaryDirectory() as td:
            for d, files in tree.items():
                p = os.path.join(td, d)
                os.makedirs(p, exist_ok=True)
                for f in files:
                    open(os.path.join(p, f), "w").close()

            _p, c, table = compile_sources([("T.OTY", self.TYPED_SRC)])
            self.assertEqual(self.cv.load(c.data), 0)

            # 1. Default folder: AUDIO/DRUMS
            slots = self.cv.bind_card(td)
            res = self.cv.run("f", seed=0)
            self.assertNotEqual(res[0] if isinstance(res, tuple) else None, "error")
            self.assertEqual(res["globals"]["x"], 1)
            self.assertEqual(res["globals"]["y"], 1)
            self.assertEqual(res["globals"]["z"], 40)
            self.assertEqual(res["globals"]["t"], 2)
            self.assertEqual(slots.final().get(1), "AUDIO/DRUMS/bd.wav")

            # 2. Override folder: AUDIO/SYNTH and params [0, 2, 0, 75]
            self.cv.set_param_folder(2, "AUDIO/SYNTH")
            self.cv.set_params([0, 2, 0, 75])
            slots = self.cv.bind_card(td)
            res = self.cv.run("f", seed=0)
            self.assertNotEqual(res[0] if isinstance(res, tuple) else None, "error")
            self.assertEqual(res["globals"]["x"], 0)
            self.assertEqual(res["globals"]["y"], 2)
            self.assertEqual(res["globals"]["z"], 75)
            self.assertEqual(res["globals"]["t"], 2)
            self.assertEqual(slots.final().get(1), "AUDIO/SYNTH/bass.wav")

            # 3. Clear folder override: back to AUDIO/DRUMS
            self.cv.set_param_folder(2, None)
            self.cv.set_params([])
            slots = self.cv.bind_card(td)
            res = self.cv.run("f", seed=0)
            self.assertNotEqual(res[0] if isinstance(res, tuple) else None, "error")
            self.assertEqual(res["globals"]["x"], 1)
            self.assertEqual(res["globals"]["y"], 1)
            self.assertEqual(res["globals"]["z"], 40)
            self.assertEqual(res["globals"]["t"], 2)
            self.assertEqual(slots.final().get(1), "AUDIO/DRUMS/bd.wav")

    def test_typed_params_execution_matches_python(self):
        src = (
            'def f\n'
            '  menu "SCRIPTS" >> "RUN"\n'
            '  param flag bool 1\n'
            '  param opt option 2 "A" "B" "C"\n'
            '  param n 50 0 100\n'
            '  x flag\n'
            '  y opt\n'
            '  z n\n'
        )
        prog, c, table = compile_sources([("T.OTY", src)])
        self.assertEqual(self.cv.load(c.data), 0)
        vprog = vm.OtbProgram(table)
        st = Stats()
        for params in [{}, {"flag": 0}, {"flag": 1, "opt": 0, "n": 25}, {"opt": 1}, {"n": 99}]:
            compare_function(self, self.cv, vprog, "f", (0, 1), st, ((1, 16),), params=params)


if __name__ == "__main__":
    unittest.main()
