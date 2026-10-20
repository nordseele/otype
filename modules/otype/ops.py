"""OType operation registry (spec: docs/syntax.md section 8).

Single source of truth for every operation of the API, including the language
operators (spec 5.2) and the context statements (spec 6.2).  tools/otype.py
knows NO operation by name: it reads OPS.  Adding an operation = adding an
entry below.  An `id` is STABLE and never reused or renumbered.

    OPS       dict, upper-case name (and alias) -> Op (internal key: the language
              is case-insensitive, lookups upper-case what the user wrote)
    ALL_OPS   list of the distinct Op entries, in declaration order
    show()    the ONE helper that renders a name for a human or a tool: lower case
              (the canonical written form).  Every output
              (ops, ops --md, ops --json, messages, -v) goes through it.
"""
from dataclasses import dataclass

# ---- argument kinds (spec 8.1) --------------------------------------------
INT = "INT"        # any expression
STEP = "STEP"      # expression, 1..64
TRACK = "TRACK"    # expression, 1..8
SLOT = "SLOT"      # expression, 1..128
PARAM = "PARAM"    # expression, 0..29
VALUE = "VALUE"    # expression, clamped by the core to the parameter's range
COND = "COND"      # trig-condition constant, or an expression
PCT = "PCT"        # expression, 0..100
FILE = "FILE"      # expression (index in the current DIR list)
STR = "STR"        # constant string
LIST = "LIST"      # constant list
ITEM = "ITEM"      # a string constant or an integer expression (msg)
# Context-only kinds (used by the CTX family, parsed by the generic arg parser)
TRACKS = "TRACKS"    # 1..8 TRACK expressions, or the single keyword CUR / ALL
STEPSEL = "STEPSEL"  # ALL | two STEP-range expressions | a predicate name

FOLDER = "FOLDER"  # folder param or constant string (DIR)
PARAM_INT = "INT"
PARAM_BOOL = "BOOL"
PARAM_OPTION = "OPTION"
PARAM_FOLDER = "FOLDER"
CH = "CH"          # expression, 1..16
VAL7 = "VAL7"      # expression, 0..127
VAL14 = "VAL14"    # expression, 0..16383 (a 14-bit MIDI number: NRPN parameter / value)
DUR = "DUR"        # expression, 1..32767
OUT = "OUT"        # expression, 1..4 (a module output)
NAME = "NAME"      # a user function name, written bare (`cv acid 1`) and <= 8 characters

NUMERIC_KINDS = (INT, STEP, TRACK, SLOT, PARAM, VALUE, PCT, FILE, CH, VAL7, VAL14, DUR, OUT)
# constant ranges checked at load time (spec 8.1)
RANGES = {STEP: (1, 64), TRACK: (1, 8), SLOT: (1, 128), PARAM: (0, 29), PCT: (0, 100),
          CH: (1, 16), VAL7: (0, 127), VAL14: (0, 16383), DUR: (1, 32767), OUT: (1, 4)}


class Param(tuple):
    """Parameter specification: tuple of (default, min, max) + typed attributes."""
    def __new__(cls, d, lo, hi, kind="INT", labels=None, path=None, src=None):
        inst = super().__new__(cls, (d, lo, hi))
        inst.kind = kind
        inst.labels = tuple(labels) if labels else ()
        inst.path = path
        inst.src = src
        return inst

    @property
    def d(self):
        return self[0]

    @property
    def lo(self):
        return self[1]

    @property
    def hi(self):
        return self[2]

    def __repr__(self):
        if self.src:
            return "Param(%d, %d, %d, src=%r)" % (self[0], self[1], self[2], self.src)
        if self.kind == "INT":
            return "Param(%d, %d, %d)" % (self[0], self[1], self[2])
        if self.kind == "BOOL":
            return "Param.bool(%d)" % self[0]
        if self.kind == "OPTION":
            return "Param.option(%d, %s)" % (self[0], self.labels)
        if self.kind == "FOLDER":
            return "Param.folder(%s)" % (self.path,)
        return super().__repr__()




class KW:
    """A keyword out of a fixed list (DEEP, CUR, ALL, WAV ...)."""
    def __init__(self, *names):
        self.names = tuple(n.upper() for n in names)

    def __repr__(self):
        return "KW(%s)" % " ".join(self.names)


class OPT_KW(KW):
    """Optional trailing keyword (statements only)."""
    def __repr__(self):
        return "[%s]" % "|".join(self.names)


class OPT_INT:
    """Optional integer argument; if omitted at parse time, default value is emitted."""
    def __init__(self, default=0):
        self.default = default

    def __repr__(self):
        return "[%s]" % self.default


class REST:
    """1..n arguments of `kind` until end of line or ':' (statements only)."""
    def __init__(self, kind):
        self.kind = kind

    def __repr__(self):
        return "REST(%r)" % (self.kind,)


_LOW = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def show(s):
    """Canonical written form of a name, keyword, kind or condition: lower case (ASCII letters only:
    a byte >= 0x80 is never changed).

    Text between double quotes (a user string) keeps its case."""
    parts = s.split('"')
    for i in range(0, len(parts), 2):
        parts[i] = parts[i].translate(_LOW)
    return '"'.join(parts)


def kind_name(k):
    return show(k if isinstance(k, str) else repr(k))


# ---- families / touches ----------------------------------------------------
LANG, CTX, EDIT, IO_R, IO_W, UI, STATE = "LANG", "CTX", "EDIT", "IO_R", "IO_W", "UI", "STATE"
TRAC, PART, SLOTS, NONE = "TRAC", "PART", "SLOTS", "NONE"

# namespace numbers (ns_number of an id); a namespace belongs to a module
NAMESPACES = {"LANG": 0, "CTX": 1, "READ": 2, "TR": 3, "TL": 4, "PL": 5,
              "SMP": 6, "COND": 7, "MICRO": 8, "PART": 9, "PRM": 10,
              "FILE": 11, "SLOT": 12, "POOL": 13, "UI": 14, "RHYTHM": 15, "STATE": 16,
              "AUDIO": 17, "MODULE": 18}


@dataclass(frozen=True)
class Op:
    name: str
    ns: str
    id: tuple              # (ns_number, op_number) - stable, unique
    args: tuple            # kinds, in order
    ret: object            # None or INT
    cost: int
    family: str
    destructive: bool
    touches: str
    predicate: bool        # usable after STEPS without its argument
    stmt_only: bool        # statement only (never a value)
    core: str              # firmware core symbol ("" for language / context)
    since: int
    level: str             # evidence level of the core (LEVELS)
    aliases: tuple = ()
    value: object = None   # value of a 0-ary constant (ON, AMP.VOL ...)
    lazy: bool = False     # args[1:] are alternatives: only one is evaluated
    doc: str = ""          # one English line for the manual
    category: str = ""     # one of CATEGORIES
    settable: bool = False  # "NAME value" at the start of a statement SETS it
    stream: object = None  # random stream drawn from (STREAMS) or None
    argnames: tuple = ()   # short argument names for the manual
    menu_only: bool = False  # refused in a live script (its confirmation cannot be asked there)

    def signature(self):
        """`pl.set s p v` -- the name and short argument letters (lower case)."""
        return " ".join((show(self.name),) + _arg_letters(self))

    @property
    def shown(self):
        """The name as written for humans and tools (lower case)."""
        return show(self.name)

    @property
    def min_args(self):
        return sum(1 for k in self.args if not isinstance(k, (OPT_KW, OPT_INT)))


# ---- manual metadata -------------------------------------------------------
CATEGORIES = ("LANG", "CONTEXT", "PATTERN", "PARTS", "TRIGS", "TIMING", "TRACK", "PAGES",
              "LOCKS", "SMP", "SCENES", "FILES", "SLOTS", "AUDIO", "UI", "MODULES")
STREAMS = ("RAND", "TOSS", "PROB", "DRUNK", "FILE")
# random streams drawn by language MODIFIERS (they are keywords, not registry entries)
MODIFIER_STREAMS = {"PROB": "PROB"}

_LETTERS = {STEP: "s", TRACK: "t", SLOT: "n", PARAM: "p", VALUE: "v", COND: "c", PCT: "pct",
            FILE: "f", STR: "str", LIST: "list", TRACKS: "t...", STEPSEL: "all|a b|pred",
            OUT: "n", NAME: "name",
            CH: "ch", VAL7: "v", VAL14: "v", DUR: "dur"}


def _arg_letters(o):
    if o.argnames:
        return tuple(o.argnames)
    out, ints, used = [], iter("xyzw"), set()
    for k in o.args:
        if isinstance(k, OPT_KW):
            out.append("[%s]" % "|".join(show(n) for n in k.names))
            continue
        if isinstance(k, OPT_INT):
            out.append("[%s]" % k.default)
            continue
        if isinstance(k, REST):
            sub = k.kind
            out.append(("%s|..." % "|".join(show(n) for n in sub.names)) if isinstance(sub, KW)
                       else _LETTERS.get(sub, "x") + "...")
            continue
        if isinstance(k, KW):
            out.append("|".join(show(n) for n in k.names))
            continue
        if k == INT:
            out.append(next(ints))
            continue
        L = _LETTERS.get(k, "x")
        n = L
        i = 2
        while n in used:
            n = "%s%d" % (L, i)
            i += 1
        used.add(n)
        out.append(n)
    return tuple(out)


# Evidence levels of an op's firmware core:
#   vm               language / context / VM-only: needs no firmware core
#   proven           the core is confirmed on the unit by an existing firmware function
#   to establish     the core is still to be found (small research needed)
#   tables to write  the data tables are not written yet
#   unknown          how the firmware does this is not known yet
LEVELS = ("vm", "proven", "to establish", "tables to write", "unknown")

_PROVEN = frozenset(n.upper() for n in """
    trk.cur pat.len has.trig has.lock has.smp is.empty tr.add tl.add tl.fill
    pl.set pl.get pl.del pl.rnd pl.clr smp.set smp.get smp.del smp.rnd smp.clr micro.set
    micro.rnd par.set lfo.dest fx.type scn.clr trk.init dir
    static.load static.free static.firstfree static.clr
    play
    send.note send.cc send.prog send.nrpn midi.cc midi.note midi.vel
    rt.add rt.del rt.clr rt.src ro.add rec.src rec.len rec.trig rec.loop rec.arm
    pat.copy gate out cv
    """.split())
_TO_ESTABLISH = frozenset(n.upper() for n in """
    trk.type trk.slot tempo
    tr.del tr.flip tr.clr pl.vary cond.set cond.rnd ratch.set
    pat.cur bank.cur pat.go
    part.cur part.go part.save part.reload
    tl.del tt.add tt.del os.add os.del os.arm sw.add sw.del sw.flip sl.add sl.del sl.flip trk.swing
    cond.get cond.del micro.get ratch.get fill fill.once
    trk.level trk.cue trk.mute
    par.get par.min par.max fx.count static.swap pb.setup amp.setup lfo.wave lfo.mult lfo.trig lfod.set lfod.get lfod.interp fx1.setup fx2.setup
    scn.set scn.get scn.del scn.copy msg
    src.save flex.load flex.free flex.firstfree flex.del cat.clr cat.add cat.save norm
    seq.run seq.step seq.mute nx.smp nx.pl ro.del""".split())
_VM_ONLY = frozenset(n.upper() for n in """
    step.cur ext match file.len file.rnd file.pick file.at slots.clr slots.add slots.rnd
    lfo.spd lfo.dep
    note.num note.vel note.ch cc.num cc.val cc.ch trig.step trig.hit trig.val""".split())



def _default_level(name, family):
    if name in _PROVEN:
        return "proven"
    if name in _TO_ESTABLISH:
        return "to establish"
    if family in (LANG, CTX, STATE) or name in _VM_ONLY:
        return "vm"
    return "to establish"        # not VM, in no list: reported by the cheat-sheet check


OPS = {}
ALL_OPS = []
_IDS = {}


def op(name, ns, num, args=(), ret=None, cost=4, family=EDIT, destructive=False,
       touches=NONE, predicate=False, stmt_only=False, core=None, since=1,
       level=None, aliases=(), value=None, lazy=False,
       doc="", category="", settable=False, stream=None, argnames=(), menu_only=False):
    name = name.upper()
    if not doc.strip():
        raise ValueError("op %s: missing doc" % name)
    if category not in CATEGORIES:
        raise ValueError("op %s: missing or unknown category %r" % (name, category))
    if level is None:
        level = _default_level(name, family)
    if level not in LEVELS:
        raise ValueError("op %s: unknown level %r (one of %s)" % (name, level, ", ".join(LEVELS)))
    if stream is not None and stream not in STREAMS:
        raise ValueError("op %s: unknown stream %r" % (name, stream))
    if core is None:
        core = "" if family in (LANG, CTX) else "otc_" + name.lower().replace(".", "_")
    ident = (NAMESPACES[ns], num)
    if ident in _IDS:
        raise ValueError("duplicate op id %r: %s and %s" % (ident, name, _IDS[ident]))
    _IDS[ident] = name
    entry = Op(name, ns, ident, tuple(args), ret, cost, family, destructive, touches,
               predicate, stmt_only, core, since, level,
               tuple(a.upper() for a in aliases), value, lazy, doc.strip(), category,
               settable, stream, tuple(argnames), menu_only)
    for n in (name,) + entry.aliases:
        if n in OPS:
            raise ValueError("duplicate op name %s" % n)
        OPS[n] = entry
    ALL_OPS.append(entry)
    return entry


# ---- LANG / MATH / LOGIC / RANDOM (cost 1) ---------------------------------
# ---- 1. LANG (cost 1 / 2) --------------------------------------------------
def _lang(name, num, args, doc, **kw):
    return op(name, "LANG", num, args, ret=INT, cost=1, family=LANG, category="LANG", doc=doc, **kw)

_lang("ADD", 0x01, (INT, INT), "add x and y", aliases=("+",), argnames=("x", "y"))
_lang("SUB", 0x02, (INT, INT), "subtract y from x", aliases=("-",), argnames=("x", "y"))
_lang("MUL", 0x03, (INT, INT), "multiply x by y", aliases=("*",), argnames=("x", "y"))
_lang("DIV", 0x04, (INT, INT), "divide x by y (0 if y is 0)", aliases=("/",), argnames=("x", "y"))
_lang("MOD", 0x05, (INT, INT), "remainder of x / y, never negative (0 if y is 0)", aliases=("%",), argnames=("x", "y"))
_lang("MIN", 0x06, (INT, INT), "the smaller of x and y", argnames=("x", "y"))
_lang("MAX", 0x07, (INT, INT), "the larger of x and y", argnames=("x", "y"))
_lang("ABS", 0x08, (INT,), "absolute value of x", argnames=("x",))
_lang("NEG", 0x09, (INT,), "x with its sign flipped", argnames=("x",))
_lang("NOT", 0x0A, (INT,), "1 if x is 0, else 0", argnames=("x",))
_lang("LIM", 0x0B, (INT, INT, INT), "clamp x into lo..hi", argnames=("x", "lo", "hi"))
_lang("WRAP", 0x0C, (INT, INT, INT), "wrap x around into lo..hi", argnames=("x", "lo", "hi"))
_lang("EQ", 0x0D, (INT, INT), "1 if x equals y", aliases=("==",), argnames=("x", "y"))
_lang("NE", 0x0E, (INT, INT), "1 if x differs from y", aliases=("!=",), argnames=("x", "y"))
_lang("LT", 0x0F, (INT, INT), "1 if x is less than y", aliases=("<",), argnames=("x", "y"))
_lang("GT", 0x10, (INT, INT), "1 if x is greater than y", aliases=(">",), argnames=("x", "y"))
_lang("LE", 0x11, (INT, INT), "1 if x is less than or equal to y", aliases=("<=",), argnames=("x", "y"))
_lang("GE", 0x12, (INT, INT), "1 if x is greater than or equal to y", aliases=(">=",), argnames=("x", "y"))
_lang("AND", 0x13, (INT, INT), "1 if x and y are both true (both are evaluated)", argnames=("x", "y"))
_lang("OR", 0x14, (INT, INT), "1 if x or y is true (both are evaluated)", argnames=("x", "y"))
_lang("XOR", 0x15, (INT, INT), "1 if exactly one of x and y is true", argnames=("x", "y"))
_lang("EZ", 0x16, (INT,), "1 if x is 0", argnames=("x",))
_lang("NZ", 0x17, (INT,), "1 if x is not 0", argnames=("x",))
_lang("?", 0x18, (INT, INT, INT), "a if c is true, else b (only the chosen one is evaluated)",
      lazy=True, argnames=("c", "a", "b"))
_lang("RND", 0x19, (INT,), "random number 0..n-1", stream="RAND", argnames=("n",))
_lang("RRAND", 0x1A, (INT, INT), "random number a..b inclusive", stream="RAND", argnames=("a", "b"))
_lang("TOSS", 0x1B, (), "random 0 or 1", stream="TOSS")
_lang("CHANCE", 0x1C, (PCT,), "1 with p percent probability, else 0", stream="TOSS", argnames=("p",))
_lang("PICK", 0x1D, (LIST,), "random element of a list", stream="RAND", argnames=("list",))
_lang("AT", 0x1E, (LIST, INT), "i-th element of a list (1..n, clamped)", argnames=("list", "i"))
_lang("LEN", 0x1F, (LIST,), "number of elements of a list", argnames=("list",))
_lang("ON", 0x20, (), "the value 1", value=1)
_lang("OFF", 0x21, (), "the value 0", value=0)

op("ER", "RHYTHM", 0x01, (INT, INT, INT), ret=INT, cost=2, family=LANG, category="LANG",
   doc="1 if step s plays in a Euclidean rhythm of k hits over n steps",
   argnames=("k", "n", "s"))
op("ER.R", "RHYTHM", 0x02, (INT, INT, INT, INT), ret=INT, cost=2, family=LANG, category="LANG",
   doc="like er, with the rhythm rotated by r steps", argnames=("k", "n", "r", "s"))
op("DR.V", "RHYTHM", 0x03, (INT, INT), ret=INT, cost=2, family=LANG, category="LANG",
   doc="velocity 0..127 of curve c at step s (16 steps, repeats): 0 flat, 1 beats, 2 off-beats, 3 ramp up, "
       "4 ramp down, 5 eighths, 6 tresillo, 7 swell; another c reads 0",
   argnames=("c", "s"))

op("FLIP", "STATE", 0x01, (), ret=INT, cost=2, family=STATE, category="LANG", settable=True,
   doc="alternates 0, 1, 0... on each read; flip x sets it")
op("O", "STATE", 0x02, (), ret=INT, cost=2, family=STATE, category="LANG", settable=True,
   doc="counter, moves by o.inc after each read, between o.min and o.max; o x sets it")
op("O.MIN", "STATE", 0x03, (INT,), cost=1, family=STATE, category="LANG", stmt_only=True,
   doc="lowest value of o (default 0)", argnames=("x",))
op("O.MAX", "STATE", 0x04, (INT,), cost=1, family=STATE, category="LANG", stmt_only=True,
   doc="highest value of o (default 63)", argnames=("x",))
op("O.WRAP", "STATE", 0x05, (INT,), cost=1, family=STATE, category="LANG", stmt_only=True,
   doc="1 = o wraps around at its limits (default), 0 = it stops", argnames=("x",))
op("O.INC", "STATE", 0x06, (INT,), cost=1, family=STATE, category="LANG", stmt_only=True,
   doc="step of o after each read (default 1)", argnames=("x",))
op("DRUNK", "STATE", 0x07, (), ret=INT, cost=2, family=STATE, category="LANG", settable=True,
   stream="DRUNK", doc="random walk: moves by -1, 0 or +1 on each read; drunk x sets it")
op("DRUNK.MIN", "STATE", 0x08, (INT,), cost=1, family=STATE, category="LANG", stmt_only=True,
   doc="lowest value of drunk (default 0)", argnames=("x",))
op("DRUNK.MAX", "STATE", 0x09, (INT,), cost=1, family=STATE, category="LANG", stmt_only=True,
   doc="highest value of drunk (default 127)", argnames=("x",))
op("DRUNK.WRAP", "STATE", 0x0A, (INT,), cost=1, family=STATE, category="LANG", stmt_only=True,
   doc="1 = drunk wraps around at its limits, 0 = it stops (default)", argnames=("x",))

# ---- 2. CONTEXT ------------------------------------------------------------
op("TRK", "CTX", 0x01, (TRACKS,), cost=1, family=CTX, category="CONTEXT",
   doc="act on tracks t... (or cur, or all)")
op("STEPS", "CTX", 0x02, (STEPSEL,), cost=1, family=CTX, category="CONTEXT",
   doc="act on all steps, steps a..b, or the steps matching a predicate (has.trig)")
op("STEP", "CTX", 0x03, (STEP,), cost=1, family=CTX, category="CONTEXT",
   doc="act on step s only")
op("TRK.CUR", "READ", 0x01, (), ret=INT, cost=2, family=IO_R, category="CONTEXT",
   doc="the current track number")
op("STEP.CUR", "READ", 0x03, (), ret=INT, cost=2, family=IO_R, category="CONTEXT",
   doc="the current step number")
op("SEED", "CTX", 0x04, (INT,), cost=1, family=CTX, category="CONTEXT",
   doc="reproducible randomness from here on: seeds all five streams", argnames=("n",))
for _i, (_n, _doc) in enumerate((("RAND", "rnd, rrand and pick"), ("TOSS", "toss and chance"),
                                 ("PROB", "the prob modifier"), ("DRUNK", "drunk"),
                                 ("FILE", "file.rnd, file.pick and slots.rnd"))):
    op(_n + ".SEED", "CTX", 0x05 + _i, (INT,), cost=1, family=CTX, category="CONTEXT",
       doc="seed the random stream of %s only" % _doc, argnames=("n",))

# ---- 3. PATTERN ------------------------------------------------------------
op("PAT.LEN", "READ", 0x02, (), ret=INT, cost=2, family=IO_R, category="PATTERN",
   doc="length of the current pattern")
op("PAT.CUR", "READ", 0x05, (), ret=INT, cost=2, family=IO_R, category="PATTERN",
   doc="active pattern number (1..16)")
op("BANK.CUR", "READ", 0x06, (), ret=INT, cost=2, family=IO_R, category="PATTERN",
   doc="active bank number (1..16)")
op("PAT.GO", "PART", 0x0B, (INT, OPT_INT(0)), cost=32, family=EDIT, touches=NONE, category="PATTERN",
   doc="change to pattern p (1..16) and optional bank b (1..16)", argnames=("p", "[b]"))
op("PAT.COPY", "PART", 0x25, (INT, INT), cost=128, family=EDIT, destructive=True, touches=TRAC,
   category="PATTERN", menu_only=True,
   doc="copy pattern a (1..16) onto pattern b (1..16) of the current bank, with its undo; the pattern clipboard is untouched",
   argnames=("a", "b"))
op("TEMPO", "PART", 0x0A, (), ret=INT, cost=2, family=IO_R, touches=PART, category="PATTERN", settable=True,
   doc="active tempo in BPM; tempo x sets it")

# ---- 4. PARTS --------------------------------------------------------------
op("PART.CUR", "READ", 0x07, (), ret=INT, cost=2, family=IO_R, category="PARTS",
   doc="part linked to the current pattern (1..4)")
op("PART.GO", "PART", 0x0C, (INT,), cost=32, family=EDIT, touches=PART, category="PARTS",
   doc="link part n (1..4) to the current pattern", argnames=("n",))
op("PART.SAVE", "PART", 0x0D, (OPT_INT(0),), cost=64, family=EDIT, touches=PART, category="PARTS",
   doc="save part n (1..4, or current part) to its buffer", argnames=("[n]",))
op("PART.RELOAD", "PART", 0x0E, (OPT_INT(0),), cost=64, family=EDIT, touches=PART, category="PARTS",
   doc="reload part n (1..4, or current part) from its buffer (refused if never saved)", argnames=("[n]",))

# ---- 5. TRIGS --------------------------------------------------------------
op("HAS.TRIG", "READ", 0x10, (STEP,), ret=INT, cost=2, family=IO_R, predicate=True, category="TRIGS",
   doc="1 if step s has a trig")
op("IS.EMPTY", "READ", 0x13, (STEP,), ret=INT, cost=2, family=IO_R, predicate=True, category="TRIGS",
   doc="1 if step s has no trig and no lock")
op("TR.ADD", "TR", 0x01, (STEP,), touches=TRAC, category="TRIGS",
   doc="add a trig on step s (a one-shot or trigless trig becomes red; keeps its locks)")
op("TR.DEL", "TR", 0x02, (STEP,), touches=TRAC, category="TRIGS",
   doc="remove the trig on step s (and its locks, sample lock, condition)")
op("TR.FLIP", "TR", 0x03, (STEP,), touches=TRAC, category="TRIGS",
   doc="toggle the trig on step s (removal as tr.del)")
op("TR.CLR", "TR", 0x04, (), cost=128, destructive=True, touches=TRAC, category="TRIGS",
   doc="remove every trig of the current steps (as tr.del)")
op("TL.ADD", "TL", 0x01, (STEP,), touches=TRAC, category="TRIGS",
   doc="add a trigless lock trig on step s")
op("TL.FILL", "TL", 0x02, (PCT,), cost=128, touches=TRAC, category="TRIGS", stream="RAND",
   doc="fill the current steps with trigless locks, p percent of them", argnames=("p",))
op("TL.DEL", "TL", 0x03, (STEP,), touches=TRAC, category="TRIGS",
   doc="remove the trigless lock on step s (mask and locks)")
op("TT.ADD", "TR", 0x05, (STEP,), touches=TRAC, category="TRIGS",
   doc="add a trigless trig on step s")
op("TT.DEL", "TR", 0x06, (STEP,), touches=TRAC, category="TRIGS",
   doc="remove the trigless trig on step s")
op("OS.ADD", "TR", 0x07, (STEP,), touches=TRAC, category="TRIGS",
   doc="add a one-shot trig on step s")
op("OS.DEL", "TR", 0x08, (STEP,), touches=TRAC, category="TRIGS",
   doc="remove the one-shot trig on step s")
op("OS.ARM", "TR", 0x09, (), touches=TRAC, category="TRIGS",
   doc="rearm one-shot trigs on context tracks")
op("SW.ADD", "TR", 0x0A, (STEP,), touches=TRAC, category="TRIGS",
   doc="add a swing trig on step s")
op("SW.DEL", "TR", 0x0B, (STEP,), touches=TRAC, category="TRIGS",
   doc="remove the swing trig on step s")
op("SW.FLIP", "TR", 0x0C, (STEP,), touches=TRAC, category="TRIGS",
   doc="toggle the swing trig on step s")
op("SL.ADD", "TR", 0x0D, (STEP,), touches=TRAC, category="TRIGS",
   doc="add a slide trig on step s")
op("SL.DEL", "TR", 0x0E, (STEP,), touches=TRAC, category="TRIGS",
   doc="remove the slide trig on step s")
op("SL.FLIP", "TR", 0x0F, (STEP,), touches=TRAC, category="TRIGS",
   doc="toggle the slide trig on step s")
op("RT.ADD", "TR", 0x11, (STEP, OPT_INT(7)), touches=TRAC, category="TRIGS",
   doc="add a recorder trig on step s with source mask src (1 AB, 2 CD, 4 internal, 1..7; default 7)", argnames=("s", "[src]"))
op("RT.DEL", "TR", 0x12, (STEP,), touches=TRAC, category="TRIGS",
   doc="remove the recorder trig on step s (sources, one-shot flag and micro-timing)", argnames=("s",))
op("RT.CLR", "TR", 0x13, (), cost=128, destructive=True, touches=TRAC, category="TRIGS",
   doc="remove every recorder trig of the current steps (as rt.del)")
op("RT.SRC", "TR", 0x14, (STEP, INT), touches=TRAC, category="TRIGS",
   doc="set the source mask v (1 AB, 2 CD, 4 internal, 1..7) of the recorder trig on step s", argnames=("s", "v"))
op("RO.ADD", "TR", 0x15, (STEP, OPT_INT(7)), touches=TRAC, category="TRIGS",
   doc="add a ONE SHOT recorder trig on step s with source mask src (1 AB, 2 CD, 4 internal, 1..7; default 7)",
   argnames=("s", "[src]"))
op("RO.DEL", "TR", 0x16, (STEP,), touches=TRAC, category="TRIGS",
   doc="remove the recorder trig on step s, one shot or not (sources, one-shot flag and micro-timing)", argnames=("s",))
op("TRK.SWING", "TR", 0x10, (), ret=INT, cost=2, family=IO_R, touches=TRAC, category="TRACK", settable=True,
   doc="swing amount 50..80 percent on context tracks; trk.swing x sets it")

# ---- 6. TIMING (micro-timing, conditions, repetitions, FILL) ---------------
op("COND.SET", "COND", 0x01, (STEP, COND), touches=TRAC, category="TIMING",
   doc="set the trig condition of step s (skipped if the step has no trig)")
op("COND.RND", "COND", 0x02, (COND, COND), cost=128, touches=TRAC, category="TIMING", stream="RAND",
   doc="random trig conditions between c and d on the current steps (those with a trig)", argnames=("c", "d"))
op("COND.GET", "READ", 0x15, (STEP,), ret=INT, cost=2, family=IO_R, category="TIMING",
   doc="the trig condition on step s (0 if none)")
op("COND.DEL", "COND", 0x03, (STEP,), touches=TRAC, category="TIMING",
   doc="remove the trig condition on step s")
op("MICRO.SET", "MICRO", 0x01, (STEP, INT), touches=TRAC, category="TIMING",
   doc="set the micro-timing offset x of step s (skipped if the step has no trig)", argnames=("s", "x"))
op("MICRO.RND", "MICRO", 0x02, (INT,), cost=128, touches=TRAC, category="TIMING", stream="RAND",
   doc="random micro-timing up to x on the current steps (those with a trig)", argnames=("x",))
op("MICRO.GET", "READ", 0x16, (STEP,), ret=INT, cost=2, family=IO_R, category="TIMING",
   doc="the micro-timing offset on step s (-23..+23)")
op("RATCH.SET", "MICRO", 0x03, (STEP, INT), touches=TRAC, category="TIMING",
   doc="set x retriggers on step s (skipped if the step has no trig)", argnames=("s", "x"))
op("RATCH.GET", "READ", 0x17, (STEP,), ret=INT, cost=2, family=IO_R, category="TIMING",
   doc="the number of retriggers (TRIG COUNT) on step s (1..8)")
op("FILL", "COND", 0x04, (), ret=INT, cost=2, family=IO_R, category="TIMING", settable=True,
   doc="FILL mode status: 1 = active, 0 = off; fill x sets it")
op("FILL.ONCE", "COND", 0x05, (), cost=2, family=EDIT, category="TIMING",
   doc="arm FILL mode for one pattern cycle")

# ---- 7. TRACK --------------------------------------------------------------
op("TRK.TYPE", "READ", 0x04, (), ret=INT, cost=2, family=IO_R, touches=PART, category="TRACK", settable=True,
   doc="machine type of context track (0 static, 1 flex, 2 thru, 3 neighbor, 4 pickup); trk.type x sets it")
op("TRK.SLOT", "PART", 0x09, (), ret=SLOT, cost=2, family=IO_R, touches=PART, category="TRACK", settable=True,
   doc="assigned sample slot of context track (1..128); trk.slot n sets it")
op("TRK.LEVEL", "PART", 0x10, (), ret=INT, cost=2, family=IO_R, touches=PART, category="TRACK", settable=True,
   doc="track LEVEL (0..127); trk.level x sets it")
op("TRK.CUE", "PART", 0x11, (), ret=INT, cost=2, family=IO_R, touches=PART, category="TRACK", settable=True,
   doc="track CUE level (0..127); trk.cue x sets it")
op("TRK.MUTE", "PART", 0x12, (), ret=INT, cost=2, family=IO_R, touches=PART, category="TRACK", settable=True,
   doc="track mute status (1 = muted, 0 = active); trk.mute x sets it")
op("REC.SRC", "PART", 0x20, (TRACK, INT, INT), touches=PART, category="TRACK",
   doc="set recorder input source n (1 INAB, 2 INCD, 3 SRC3) to stock value v on track t "
       "(INAB/INCD 0 - 1 A B/C D 2 A/C 3 B/D 4 A+B/C+D; SRC3 0 - 1..8 T1..T8 9 MAIN 10 CUE)",
   argnames=("t", "n", "v"))
op("REC.LEN", "PART", 0x21, (TRACK, INT), touches=PART, category="TRACK",
   doc="set recorder length RLEN to v steps (1..63) or MAX (64) on track t", argnames=("t", "v"))
op("REC.TRIG", "PART", 0x22, (TRACK, INT), touches=PART, category="TRACK",
   doc="set the recorder TRIG mode of track t: 0 ONE, 1 ONE-2, 2 HOLD", argnames=("t", "v"))
op("REC.LOOP", "PART", 0x23, (TRACK, INT), touches=PART, category="TRACK",
   doc="set recorder LOOP of track t: 0 off, 1 on (the captured sample loops when played back)", argnames=("t", "v"))
op("REC.ARM", "PART", 0x24, (TRACK, OPT_INT(7)), family=EDIT, stmt_only=True, touches=PART, category="TRACK",
   doc="start recording on track t now, sources src (1 AB, 2 CD, 4 SRC3, 1..7; default 7); QREC is honoured",
   argnames=("t", "[src]"))

# ---- 8. PAGES --------------------------------------------------------------
op("PAR.SET", "PART", 0x01, (PARAM, VALUE), touches=PART, category="PAGES",
   doc="set parameter p of the part to v (clamped to its range; refused if the part disables it)", argnames=("p", "v"))
op("PAR.GET", "READ", 0x14, (PARAM,), ret=INT, cost=2, family=IO_R, category="PAGES",
   doc="the base value of parameter p in the part", argnames=("p",))
op("PAR.MIN", "READ", 0x1A, (PARAM,), ret=INT, cost=2, family=IO_R, category="PAGES",
   doc="the lowest value parameter p accepts on the context track (refused if the part disables it)", argnames=("p",))
op("PAR.MAX", "READ", 0x1B, (PARAM,), ret=INT, cost=2, family=IO_R, category="PAGES",
   doc="the highest value parameter p accepts on the context track (refused if the part disables it)", argnames=("p",))

for _i, _n in enumerate(("PB", "LFO", "AMP", "FX1", "FX2")):
    op(_n, "PRM", 0x01 + _i, (INT,), ret=INT, cost=1, family=LANG, category="PAGES",
       doc="flat parameter number of the n-th parameter of the %s page" % _n, argnames=("n",))
for _i, _n in enumerate(("ATK", "HOLD", "REL", "VOL", "BAL", "XVOL")):
    op("AMP." + _n, "PRM", 0x10 + _i, (), ret=INT, cost=1, family=LANG, category="PAGES",
       doc="parameter number of AMP %s" % _n, value=12 + _i)

op("LFO.SPD", "PRM", 0x20, (INT,), ret=INT, cost=1, family=LANG, category="PAGES",
   doc="flat parameter number of LFO n speed (n=1..3)", argnames=("n",))
op("LFO.DEP", "PRM", 0x21, (INT,), ret=INT, cost=1, family=LANG, category="PAGES",
   doc="flat parameter number of LFO n depth (n=1..3)", argnames=("n",))

op("PB.SETUP", "PART", 0x13, (INT,), ret=INT, cost=2, family=IO_R, touches=PART, category="PAGES", settable=True,
   doc="setting n (1..6) of the PLAYBACK SETUP page; pb.setup n v sets it", argnames=("n",))
op("AMP.SETUP", "PART", 0x14, (INT,), ret=INT, cost=2, family=IO_R, touches=PART, category="PAGES", settable=True,
   doc="setting n (1..6) of the AMP SETUP page; amp.setup n v sets it", argnames=("n",))
op("LFO.DEST", "PART", 0x03, (INT, PARAM), touches=PART, category="PAGES",
   doc="aim LFO x (1..3) at parameter p", argnames=("x", "p"))
op("LFO.WAVE", "PART", 0x15, (INT,), ret=INT, cost=2, family=IO_R, touches=PART, category="PAGES", settable=True,
   doc="waveform of LFO n (1..3); lfo.wave n w sets it", argnames=("n",))
op("LFO.MULT", "PART", 0x16, (INT,), ret=INT, cost=2, family=IO_R, touches=PART, category="PAGES", settable=True,
   doc="multiplier of LFO n (1..3, 0..6 for 1x..64x); lfo.mult n v sets it", argnames=("n",))
op("LFO.TRIG", "PART", 0x17, (INT,), ret=INT, cost=2, family=IO_R, touches=PART, category="PAGES", settable=True,
   doc="trig mode of LFO n (1..3, 0..7); lfo.trig n m sets it", argnames=("n",))
op("LFOD.SET", "PART", 0x18, (STEP, INT), cost=2, touches=PART, category="PAGES",
   doc="set value (-64..63) of step s (1..16) in LFO designer", argnames=("s", "v"))
op("LFOD.GET", "READ", 0x19, (STEP,), ret=INT, cost=2, family=IO_R, category="PAGES",
   doc="value of step s (1..16) in LFO designer (-64..63)", argnames=("s",))
op("LFOD.INTERP", "PART", 0x1A, (STEP, INT), cost=2, touches=PART, category="PAGES",
   doc="set interpolation of step s (1..16) in LFO designer (0 off, 1 on)", argnames=("s", "x"))
op("FX.TYPE", "PART", 0x05, (INT, INT), touches=PART, category="PAGES",
   doc="choose effect type y of FX block x (1 or 2)", argnames=("x", "y"))
op("FX.COUNT", "READ", 0x1C, (INT,), ret=INT, cost=2, family=IO_R, category="PAGES",
   doc="number of entries in the effect chooser of FX block x (1 or 2), NONE included", argnames=("x",))
op("FX1.SETUP", "PART", 0x1B, (INT,), ret=INT, cost=2, family=IO_R, touches=PART, category="PAGES", settable=True,
   doc="setting n (1..6) of the FX1 SETUP page; fx1.setup n v sets it", argnames=("n",))
op("FX2.SETUP", "PART", 0x1C, (INT,), ret=INT, cost=2, family=IO_R, touches=PART, category="PAGES", settable=True,
   doc="setting n (1..6) of the FX2 SETUP page; fx2.setup n v sets it", argnames=("n",))

# ---- 9. LOCKS (parameter locks) --------------------------------------------
op("HAS.LOCK", "READ", 0x11, (STEP,), ret=INT, cost=2, family=IO_R, predicate=True, category="LOCKS",
   doc="1 if step s has a parameter lock")
op("PL.SET", "PL", 0x01, (STEP, PARAM, VALUE), touches=TRAC, category="LOCKS",
   doc="lock parameter p to value v on step s (skipped if the step has no trig)")
op("PL.GET", "PL", 0x02, (STEP, PARAM), ret=INT, cost=2, family=IO_R, category="LOCKS",
   doc="the lock of parameter p on step s")
op("PL.DEL", "PL", 0x03, (STEP, PARAM), touches=TRAC, category="LOCKS",
   doc="remove the lock of parameter p on step s")
op("PL.RND", "PL", 0x04, (PARAM,), cost=128, touches=TRAC, category="LOCKS", stream="RAND",
   doc="randomise parameter p on the current steps (those with a trig)")
op("PL.VARY", "PL", 0x05, (PARAM, INT), cost=128, touches=TRAC, category="LOCKS", stream="RAND",
   doc="vary the locks of parameter p by up to x percent (steps with a trig)", argnames=("p", "x"))
op("PL.CLR", "PL", 0x06, (), cost=128, destructive=True, touches=TRAC, category="LOCKS",
   doc="remove every parameter lock of the current steps")

# ---- 10. SMP (sample locks) ------------------------------------------------
op("HAS.SMP", "READ", 0x12, (STEP,), ret=INT, cost=2, family=IO_R, predicate=True, category="SMP",
   doc="1 if step s has a sample lock")
op("SMP.SET", "SMP", 0x01, (STEP, SLOT), touches=TRAC, category="SMP",
   doc="lock slot n as the sample of step s (skipped if the step has no trig)")
op("SMP.GET", "SMP", 0x02, (STEP,), ret=INT, cost=2, family=IO_R, category="SMP",
   doc="the sample slot locked on step s (0 if none)")
op("SMP.DEL", "SMP", 0x03, (STEP,), touches=TRAC, category="SMP",
   doc="remove the sample lock of step s")
op("SMP.RND", "SMP", 0x04, (SLOT, SLOT), cost=128, touches=TRAC, category="SMP", stream="RAND",
   doc="random sample locks from slots n..m on the current steps (those with a trig)", argnames=("n", "m"))
op("SMP.CLR", "SMP", 0x05, (), cost=128, destructive=True, touches=TRAC, category="SMP",
   doc="remove every sample lock of the current steps")

# ---- 11. SCENES ------------------------------------------------------------
op("SCN.SET", "PART", 0x1D, (INT, PARAM, VALUE), touches=PART, category="SCENES",
   doc="lock parameter p to value v on scene n (1..16)", argnames=("n", "p", "v"))
op("SCN.GET", "READ", 0x18, (INT, PARAM), ret=INT, cost=2, family=IO_R, category="SCENES",
   doc="the lock of parameter p on scene n (1..16)", argnames=("n", "p"))
op("SCN.DEL", "PART", 0x1E, (INT, PARAM), touches=PART, category="SCENES",
   doc="remove the lock of parameter p on scene n (1..16)", argnames=("n", "p"))
op("SCN.CLR", "PART", 0x07, (OPT_INT(0),), cost=128, destructive=True, touches=PART, category="SCENES",
   doc="unlock all scenes or scene n (1..16) of the track", argnames=("[n]",))
op("SCN.COPY", "PART", 0x1F, (INT, INT), cost=32, touches=PART, category="SCENES",
   doc="copy locks from scene a to scene b (1..16)", argnames=("a", "b"))

# ---- 12. SLOTS (slots and files) -------------------------------------------
op("DIR", "FILE", 0x01, (STR, OPT_KW("DEEP")), cost=32, family=IO_R, stmt_only=True, category="FILES",
   doc="list the files of a card folder (deep: with subfolders)")
op("EXT", "FILE", 0x02, (REST(KW("WAV", "AIF", "AIFF", "AIFC")),), family=IO_R, stmt_only=True,
   category="FILES", doc="keep only files with these extensions")
op("MATCH", "FILE", 0x03, (REST(STR),), family=IO_R, stmt_only=True, category="FILES",
   doc="keep only files whose name matches a pattern (* wildcard)")
op("FILE.LEN", "FILE", 0x04, (), ret=INT, cost=2, family=IO_R, category="FILES",
   doc="number of files in the list")
op("FILE.RND", "FILE", 0x05, (), ret=INT, cost=2, family=IO_R, category="FILES", stream="FILE",
   doc="index of a random file of the list")
op("FILE.PICK", "FILE", 0x06, (), ret=INT, cost=2, family=IO_R, category="FILES", stream="FILE",
   doc="index of a random file, never the same twice until all are used")
op("FILE.AT", "FILE", 0x07, (INT,), ret=INT, cost=2, family=IO_R, category="FILES",
   doc="index of the i-th file of the list", argnames=("i",))
op("STATIC.LOAD", "SLOT", 0x01, (SLOT, FILE), cost=8, touches=SLOTS, category="SLOTS",
   doc="load file f of the list into Static slot n")
op("STATIC.FREE", "SLOT", 0x02, (SLOT,), ret=INT, cost=2, family=IO_R, category="SLOTS",
   doc="1 if Static slot n is free")
op("STATIC.FIRSTFREE", "SLOT", 0x03, (), ret=INT, cost=2, family=IO_R, category="SLOTS",
   doc="number of the first free Static slot (0 if none)")
op("STATIC.CLR", "SLOT", 0x04, (SLOT,), destructive=True, touches=SLOTS, category="SLOTS",
   doc="empty Static slot n")
op("STATIC.SWAP", "SLOT", 0x08, (SLOT, SLOT), cost=16, touches=SLOTS, category="SLOTS",
   doc="swap the samples of Static slots a and b (an empty slot swaps too; a = b does nothing)", argnames=("a", "b"))
op("SLOTS.CLR", "SLOT", 0x05, (), category="SLOTS", doc="empty the working set of slots")
op("SLOTS.ADD", "SLOT", 0x06, (SLOT,), category="SLOTS", doc="add slot n to the working set")
op("SLOTS.RND", "SLOT", 0x07, (), ret=INT, cost=2, family=IO_R, category="SLOTS", stream="FILE",
   doc="a random slot of the working set")

# ---- AUDIO -----------------------------------------------------------------
op("SRC.SAVE", "AUDIO", 0x01, (), cost=64, destructive=True, family=IO_W, stmt_only=True, category="AUDIO",
   doc="save the active source sample to disk; the file list then holds only this file (experimental)")
op("FLEX.LOAD", "AUDIO", 0x02, (SLOT, FILE), cost=8, touches=SLOTS, family=IO_W, category="SLOTS",
   doc="load file f of the list into Flex slot n", argnames=("n", "f"))
op("FLEX.FREE", "AUDIO", 0x03, (SLOT,), ret=INT, cost=2, family=IO_R, category="SLOTS",
   doc="1 if Flex slot n is free, 0 if occupied", argnames=("n",))
op("FLEX.FIRSTFREE", "AUDIO", 0x08, (), ret=INT, cost=2, family=IO_R, category="SLOTS",
   doc="number of the first free Flex slot (0 if none); recorder buffers are not counted (experimental)")
op("FLEX.DEL", "AUDIO", 0x09, (SLOT,), destructive=True, touches=SLOTS, category="SLOTS",
   doc="empty Flex slot n and give its RAM back (STOP FIRST while the sequencer plays, if the slot is occupied; experimental)", argnames=("n",))
op("CAT.CLR", "AUDIO", 0x04, (), cost=1, family=STATE, category="AUDIO",
   doc="clear the concatenation file list")
op("CAT.ADD", "AUDIO", 0x05, (FILE,), cost=1, family=STATE, category="AUDIO",
   doc="add file f to the concatenation list", argnames=("f",))
op("CAT.SAVE", "AUDIO", 0x06, (STR,), cost=128, destructive=True, family=IO_W, stmt_only=True, category="AUDIO",
   doc="write concatenated audio files and .ot slices as name.wav; the file list then holds only this file (experimental; a very large folder is slow to read on the unit itself, allow time)", argnames=("name",))
op("NORM", "AUDIO", 0x07, (FILE, INT), cost=64, family=IO_W, category="AUDIO",
   doc="normalize file f to target db (-12..0) in its .ot attributes (experimental; a very large folder is slow to read on the unit itself, allow time)", argnames=("f", "db"))

# ---- 13. TRACK INIT (category TRACK) -----------------------------------------
op("TRK.INIT", "PART", 0x08, (), cost=128, destructive=True, touches=PART, category="TRACK",
   doc="reset the track's sound: fx, slot, parameters; not its trigs, locks or machine type")

# ---- 14. UI ----------------------------------------------------------------
op("MSG", "UI", 0x01, (REST(ITEM),), family=UI, stmt_only=True, category="UI",
   doc="end box text instead of DONE: strings and numbers (decimal), space-separated; the last msg of the run is shown",
   argnames=("item...",))

# ---- 15. MODULES -----------------------------------------------------------
op("PLAY", "MODULE", 0x01, (TRACK,), stmt_only=True, category="MODULES",
   doc="schedule track t to play at the current block's tick of the live script", argnames=("t",))
op("SEQ.RUN", "MODULE", 0x02, (), ret=INT, cost=2, family=IO_R, category="MODULES",
   doc="1 if sequencer is running, 0 if stopped")
op("SEQ.STEP", "MODULE", 0x03, (TRACK,), ret=INT, cost=2, family=IO_R, category="MODULES",
   doc="current playing step of track t (1..64)", argnames=("t",))
op("SEQ.MUTE", "MODULE", 0x04, (TRACK,), ret=INT, cost=2, family=IO_R, category="MODULES",
   doc="1 if track t is muted, 0 if active", argnames=("t",))
op("NX.SMP", "MODULE", 0x05, (SLOT,), stmt_only=True, category="MODULES",
   doc="lock Static sample slot n (1..128) on the next play hit of this live script", argnames=("n",))
op("NX.PL", "MODULE", 0x06, (PARAM, VALUE), stmt_only=True, category="MODULES",
   doc="lock parameter p (0..29) to v (clamped to p's range) on the next play hit of this live script", argnames=("p", "v"))
op("NOTE.NUM", "MODULE", 0x10, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="note number of the current note event (0..127)")
op("NOTE.VEL", "MODULE", 0x11, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="velocity of the current note event (0..127)")
op("NOTE.CH", "MODULE", 0x12, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="MIDI channel of the current note event (1..16)")
op("CC.NUM", "MODULE", 0x13, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="controller number of the current CC event (0..127)")
op("CC.VAL", "MODULE", 0x14, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="value of the current CC event (0..127)")
op("CC.CH", "MODULE", 0x15, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="MIDI channel of the current CC event (1..16)")
op("TRIG.STEP", "MODULE", 0x19, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="step (1..64) of the current trig event")
op("TRIG.HIT", "MODULE", 0x1A, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="1 if the current trig plays, 0 if its condition failed or the track is muted")
op("TRIG.VAL", "MODULE", 0x1B, (), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="value (0..255) carried by a script trig (coming in 1.2)")
op("MIDI.CC", "MODULE", 0x16, (CH, VAL7), ret=INT, cost=2, family=IO_R, category="MODULES",
   doc="last value (0..127) of controller n received on MIDI channel ch (0 if none since power on)",
   argnames=("ch", "n"))
op("MIDI.NOTE", "MODULE", 0x17, (CH,), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="last note number (0..127) received on MIDI channel ch (0 if none since power on)", argnames=("ch",))
op("MIDI.VEL", "MODULE", 0x18, (CH,), ret=INT, cost=1, family=IO_R, category="MODULES",
   doc="velocity of the last note received on MIDI channel ch (0 if none since power on)", argnames=("ch",))
op("SEND.NOTE", "MODULE", 0x20, (CH, VAL7, VAL7, DUR), stmt_only=True, family=IO_W, category="MODULES",
   doc="send MIDI note on channel ch (1..16), note n (0..127), velocity vel (0..127), duration dur (>= 1 clock ticks)",
   argnames=("ch", "n", "vel", "dur"))
op("SEND.CC", "MODULE", 0x21, (CH, VAL7, VAL7), stmt_only=True, family=IO_W, category="MODULES",
   doc="send MIDI Control Change on channel ch (1..16), cc n (0..127), value v (0..127)",
   argnames=("ch", "n", "v"))
op("SEND.PROG", "MODULE", 0x22, (CH, VAL7), stmt_only=True, family=IO_W, category="MODULES",
   doc="send MIDI Program Change on channel ch (1..16), program p (0..127)",
   argnames=("ch", "p"))
op("SEND.NRPN", "MODULE", 0x23, (CH, VAL14, VAL14), stmt_only=True, family=IO_W, category="MODULES",
   doc="send NRPN p (0..16383) = v (0..16383) on channel ch (1..16) as CC 99, 98, 6, 38; either all four go out or none",
   argnames=("ch", "p", "v"))
op("GATE", "MODULE", 0x24, (OUT,), stmt_only=True, family=IO_W, cost=2, category="MODULES",
   doc="emit a gate on output n (1..4) at the current block's tick; the live scripts whose `in gate NAME n` names this one run in the same pass",
   argnames=("n",))
op("OUT", "MODULE", 0x25, (OUT, INT), stmt_only=True, family=IO_W, cost=1, category="MODULES",
   doc="publish value v on output n (1..4), held until it changes; `cv NAME n` reads it",
   argnames=("n", "v"))
op("CV", "MODULE", 0x26, (NAME, OUT), ret=INT, cost=2, family=IO_R, category="MODULES",
   doc="the value published on output n (1..4) by the running script named NAME (0 if it is not running)",
   argnames=("name", "n"))


_unknown = (_PROVEN | _TO_ESTABLISH | _VM_ONLY) - set(OPS)
if _unknown:
    raise ValueError("level tables name unknown ops: " + ", ".join(sorted(_unknown)))


# ---- device limits and profiles ----------------------------------------------------------
DIR_LIST_MAX = 4096    # `dir` keeps the first 4096 names of the sorted list
DIR_NAME_MAX = 127     # `dir` skips names whose relative path exceeds 127 bytes of UTF-8
DIR_DEPTH_MAX = 15     # `dir ... deep` descends at most 15 folders below the listed one (the unit's folder walker):
                       # a file is listed only if its relative name has at most 15 '/'; a deeper one is silently not visited
# shortcut: the end box shows one line of about 29 glyph columns (4 px each in the 115 px free for box and shadow); a number counts
# as 6 characters ("-32768"), items are joined by one space. Raise it when the box grows (bigger box, lines, scroll): this is the ONLY
# number the checker, the C compiler (OTC_L_MAX_MSG), the VM buffer (OTC_MSG_MAX, both generated) and the tests read.
MAX_MSG = 29
PICK_BAGS_MAX = 8      # `file.pick` bags kept in all: inserting a 9th drops the oldest

_M1_NO_CAT = ()
_V1_OPS = frozenset(n.upper() for n in """
    pat.cur bank.cur pat.go
    part.cur part.go part.save part.reload
    tl.del tt.add tt.del os.add os.del os.arm sw.add sw.del sw.flip sl.add sl.del sl.flip trk.swing
    cond.get cond.del micro.get ratch.get fill fill.once
    trk.level trk.cue trk.mute
    par.get par.min par.max fx.count static.swap pb.setup amp.setup lfo.wave lfo.mult lfo.trig lfod.set lfod.get lfod.interp fx1.setup fx2.setup
    scn.set scn.get scn.del scn.copy""".split())
_M1_NO_OPS = frozenset()
PROFILES = {"m1": frozenset(
    o.name for o in ALL_OPS if o.category not in _M1_NO_CAT and o.name not in _M1_NO_OPS)}


def in_profile(op, profile):
    """True if the op may be used under PROFILE (a set of names; None = everything)."""
    return profile is None or op.name in profile
