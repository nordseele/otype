#!/usr/bin/env python3
"""OType load-time checker (spec: docs/syntax.md).

    python3 tools/otype.py check [-v] FILE.OTY [FILE2.OTY ...]
    python3 tools/otype.py run FUNC FILE.OTY [FILE2.OTY ...] [--seed n] ... [--vm]   (the simulator,
                                                       modules/otype/sim.py; `run -h`)
    python3 tools/otype.py dump FILE.OTY [FILE2.OTY ...] [--func NAME] [-o OUT.OTB] [--hex]
                                         (the compiled table OTB, modules/otype/otb.py; `dump -h`)

All files are loaded together as one program: functions and CONST are global
across files.  One line per diagnostic, sorted by file then line:

    FILE:LINE: FUNC: error: message        (FUNC = function name, or '-')
    FILE:LINE: FUNC: warning: message

then `N functions OK, E refused, W warnings`.  Exit status 0 if there is no
error, 1 otherwise.  The checker knows no operation by name: operations come
from modules/otype/ops.py (the registry).  Only the language keywords and
modifiers are known here.
"""
import json
import os
import re
import sys
from collections import OrderedDict, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from modules.otype import ops as O  # noqa: E402
from modules.otype.menu_limits import MAX_CATEGORIES as MENU_MAX_CATEGORIES  # noqa: E402
from modules.otype.menu_limits import MAX_SCRIPTS_PER_CATEGORY as MENU_MAX_SCRIPTS_PER_CATEGORY  # noqa: E402

# The language is case-insensitive; internally names are kept in upper case (compare
# on upper), but everything the user sees is lower case, through this ONE helper
# Text between double quotes keeps its case.
show = O.show

# ---- language constants ----------------------------------------------------
KEYWORDS = {"OTYPE", "DEF", "CONST", "MENU", "PAGE", "ON", "PARAM", "CONFIRM", "INPUT", "SAMPLE", "IN", "VAR", "IF", "ELIF",
            "LAUNCH",
            "ELSE", "L", "EACH", "PROB", "EVERY", "SKIP", "DEL", "BREAK", "STOP",
            "RET", "ALL", "CUR", "DEEP"}
VARS = set("ABCDXYZTIJ")
ASSIGNABLE = set("ABCDXYZT")
DECLS = {"MENU", "PAGE", "ON", "PARAM", "CONFIRM", "INPUT", "VAR", "LAUNCH", "MODULE", "CLOCK"}
MODIFIERS = {"IF", "ELIF", "ELSE", "L", "EACH", "PROB", "EVERY", "SKIP", "DEL"}
PROBS = (1, 2, 4, 6, 9, 13, 19, 25, 33, 41, 50, 59, 67, 75, 81, 87, 91, 94, 96, 98, 99)
COND_WORDS = {"!FILL", "PRE", "!PRE", "NEI", "!NEI", "1ST", "!1ST"}
FUTURE_HOSTS = {"TRIG", "LOOP", "PAT", "FILL", "LOAD", "MIDI"}
# 1.1 leaves the script trigs out (no poster yet); the genre-2 bytecode stays, nothing compiles it.  Same
# literal in firmware/c/otc_comp_parse.c.
IN_TRIG_SCRIPT_REFUSED = "in trig t script: is not available in 1.1: use in trig t: or in trig t all:"
RENAMED = {
    "RATCH": "ratch.set",
    "FX.SET": "fx.type",
    "FILE.N": "file.len",
    "SLOTS.NONE": "slots.clr",
    "LFO.SPD1": "lfo.spd 1",
    "LFO.SPD2": "lfo.spd 2",
    "LFO.SPD3": "lfo.spd 3",
    "LFO.DEP1": "lfo.dep 1",
    "LFO.DEP2": "lfo.dep 2",
    "LFO.DEP3": "lfo.dep 3",
    "SLOT.LOAD": "static.load",
    "SLOT.FREE": "static.free",
    "SLOT.FIRSTFREE": "static.firstfree",
    "SLOT.CLR": "static.clr",
}
# Removed operators: name -> the cookbook recipe that replaces them with a loop (mirrored in otc_comp_parse.c)
REMOVED = {
    "POOL.FILL": "Fill the Static slots",
    "POOL.RND": "Fill the Static slots at random",
    "POOL.CLR": "Clear all Static slots",
    "POOL.SHUFFLE": "Shuffle the Static slots",
    "SCN.GEN": "Random scenes",
    "LFO.RND": "Random LFOs",
    "FX.RND": "Random effects",
    "LFOD.RND": "Random LFO designer",
    "LFOD.INV": "Invert the LFO designer",
}


def retired(w):
    """The refusal message for a renamed or removed operator word, else None."""
    if w == "MODULE":
        return "module is gone: use in clock n"
    if w == "CLOCK":
        return "clock declaration is gone: use in clock n"
    if w in RENAMED:
        return "%s is now %s" % (show(w), RENAMED[w])
    if w in REMOVED:
        return '%s was removed: write the loop (cookbook "%s")' % (show(w), REMOVED[w])
    return None


MAX_LINE = 128
MAX_STR = 127
MAX_MSG = O.MAX_MSG           # modules/otype/ops.py: the one number behind the checker, the C compiler, the VM buffer
MAX_MSG_ITEMS = 8         # the items of one msg stay on the VM stack together (declared stack depth 16)
MAX_LIST = 16
MAX_ARGS = 4
MAX_PARAMS = 8
MAX_DEPTH = 8
MAX_NEST = 8
MAX_FUNCS = 128
MENU_BUDGET = 4096
MODULE_BUDGET = 512
EACH_ITERS = 64
LEVEL_MAX = 6
ACTION_MAX = 10
PAGE_LABEL_MAX = 8        # `page "LABEL"`: 1..8 printable ASCII characters (0x20..0x7e)
PAGE_MAX_PARAMS = 6       # a page function has at most 6 params (the 6 encoders A..F)
PAGE_FORBIDDEN_NS = ("FILE", "SLOT", "POOL")   # namespaces a page function may not use (card, sample slots, pool)
MODULE_LABEL_MAX = 8      # `module "NAME"`: 1..8 characters

NAME_MAX = 16
COST_SAT = 0xFFFFFFFF     # costs saturate here (the table does, spec 14); the C does not do big integers
MAX_FILES = 64            # source files taken from a card (in the given order)
MAX_SOURCE_BYTES = 65536  # total source bytes
MAX_DIAGS = 1000          # diagnostic lines in a report (the first ones after sorting), then one "too many errors" line

NUM_RE = re.compile(r"-?(0X[0-9A-F]+|[0-9]+)\Z")
NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")


# ---- ASCII semantics -----------------------------------------------------------------
# A source is a sequence of BYTES read as Latin-1.  Nothing below may depend on Unicode: no str.upper /
# lower / split() / strip() / isdigit() / isspace(), no regex class that is Unicode-aware (\s, \w, \d).
# Whitespace is the space and the tab; another control byte is a lexical error (lex_line).
WS = " \t"
_UP = {c: c - 32 for c in range(ord("a"), ord("z") + 1)}


def aupper(s):
    """ASCII-only upper case (the bytes >= 0x80 are untouched, unlike str.upper)."""
    return s.translate(_UP)


def asplit(s, maxsplit=-1):
    """Split on runs of space / tab only; no empty piece (like str.split() on ASCII whitespace)."""
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i] in WS:
            i += 1
        if i >= n:
            break
        if maxsplit >= 0 and len(out) >= maxsplit:
            out.append(s[i:])
            break
        j = i
        while j < n and s[j] not in WS:
            j += 1
        out.append(s[i:j])
        i = j
    return out


def isdig(ch):
    return "0" <= ch <= "9"


def q(s):
    """A text quoted in a message: always '...', no escaping (repr() would pick the quote and escape
    backslashes, which a C port cannot be expected to copy)."""
    return "'" + s + "'"


def digits_value(w):
    """int(w) of a string of ASCII digits, saturated at 10**9 (int() refuses > 4300 digits)."""
    w = w.lstrip("0")
    return 10 ** 9 if len(w) > 9 else (int(w) if w else 0)


PENDING = object()


class Msg(str):
    """A message that may carry the column span (start, end) of the offending token."""
    span = None


def msg_at(text, span):
    m = Msg(text)
    m.span = span
    return m


class LexError(Exception):
    def __init__(self, text, span=None):
        Exception.__init__(self, text)
        self.span = span


class ParseError(Exception):
    def __init__(self, text, tok=None):
        Exception.__init__(self, text)
        self.tok = tok          # Tok (or Node) whose span is the culprit, if known


# ---- diagnostics -----------------------------------------------------------
class Diag:
    __slots__ = ("file", "line", "func", "level", "msg", "hidden", "seq", "span")

    def __init__(self, file, line, func, level, msg, hidden=False, seq=0, span=None):
        self.file, self.line, self.func, self.level = file, line, show(func), level
        self.msg, self.hidden, self.seq, self.span = msg, hidden, seq, span

    def __str__(self):
        return "%s:%d: %s: %s: %s" % (self.file, self.line, self.func, self.level, self.msg)


# ---- lexer -----------------------------------------------------------------
class Tok:
    __slots__ = ("kind", "text", "val", "col", "end")

    def __init__(self, kind, text, val=None, col=0, end=0):
        self.kind, self.text, self.val = kind, text, val
        self.col, self.end = col, end      # 0-based columns in the raw line, end exclusive

    @property
    def span(self):
        return (self.col, self.end)

    def __repr__(self):
        return "Tok(%s,%r)" % (self.kind, self.text)


def split_comment(raw):
    """(code, comment) -- '#' starts a comment outside strings."""
    in_str = False
    for i, ch in enumerate(raw):
        if in_str:
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "#":
            return raw[:i], raw[i:]
    return raw, ""


def parse_int(w):
    written = w                     # the message shows the number with its sign, as written
    neg = w.startswith("-")
    if neg:
        w = w[1:]
    if w.startswith("0X"):
        d = w[2:].lstrip("0")
        v = 10 ** 9 if len(d) > 8 else (int(d, 16) if d else 0)
    else:
        v = digits_value(w)
    v = -v if neg else v
    if not -32768 <= v <= 32767:
        raise LexError("number %s out of 16-bit range (-32768..32767)" % show(written))
    return v


def classify_word(w):
    W = aupper(w)
    if W == ">>":
        return Tok("SYM", ">>")
    if NUM_RE.match(W):
        return Tok("NUM", W, parse_int(W))
    m = re.fullmatch(r"([0-9]+)%", W)
    if m:
        n = digits_value(m.group(1))
        if n not in PROBS:
            raise LexError("%s is not a trig condition (allowed: %s)"
                           % (show(W), " ".join("%d%%" % p for p in PROBS)))
        return Tok("COND", W, "%d%%" % n)       # the value is canonical (050% = 50%), the text is as written
    m = re.fullmatch(r"([0-9]+):([0-9]+)", W)
    if m:
        a, b = digits_value(m.group(1)), digits_value(m.group(2))
        if not 1 <= a <= b <= 8:
            raise LexError("%s is not a trig condition (a:b needs 1 <= a <= b <= 8)" % show(W))
        if a == b == 1:
            raise LexError("1:1 is not a trig condition (the octatrack has no always condition: "
                           "use cond.set s off for none)")
        return Tok("COND", W, "%d:%d" % (a, b))
    if W in COND_WORDS:
        return Tok("COND", W, W)
    return Tok("WORD", W)


def lex_tokens(code, base=0):
    """Tokens of `code`; `base` is the column of code[0] in the raw line."""
    toks = []
    i, n = 0, len(code)

    def add(tok, start, end):
        tok.col, tok.end = base + start, base + end
        toks.append(tok)

    while i < n:
        c = code[i]
        if c == " ":
            i += 1
        elif c == '"':
            j = code.find('"', i + 1)
            if j < 0:
                raise LexError("unterminated string", (base + i, base + n))
            s = code[i + 1:j]
            if len(s) > MAX_STR:
                raise LexError("string too long (%d > %d characters)" % (len(s), MAX_STR),
                               (base + i, base + j + 1))
            add(Tok("STR", s, s), i, j + 1)
            i = j + 1
            if i < n and code[i] not in " :|":
                raise LexError("unexpected text right after a string", (base + i, base + i + 1))
        elif c == "{":
            j = code.find("}", i)
            if j < 0:
                raise LexError("unterminated list (missing })", (base + i, base + n))
            items = asplit(code[i + 1:j])
            vals = []
            for it in items:
                if not NUM_RE.match(aupper(it)):
                    raise LexError("a list holds numbers only (found %s)" % q(show(it)),
                                   (base + i, base + j + 1))
                vals.append(parse_int(aupper(it)))
            if not vals:
                raise LexError("empty list", (base + i, base + j + 1))
            if len(vals) > MAX_LIST:
                raise LexError("list too long (%d > %d elements)" % (len(vals), MAX_LIST),
                               (base + i, base + j + 1))
            add(Tok("LIST", code[i:j + 1], tuple(vals)), i, j + 1)
            i = j + 1
        elif c == "}":
            raise LexError("unexpected }", (base + i, base + i + 1))
        elif c == ":":
            add(Tok("SYM", ":"), i, i + 1)
            i += 1
        elif c == "|":
            add(Tok("SYM", "|"), i, i + 1)
            i += 1
        else:
            j = i
            while j < n:
                ch = code[j]
                if ch in ' "{}|':
                    break
                if ch == ":":
                    # ':' glued between two digits (1:4) forms a condition
                    if (re.fullmatch(r"[0-9]+", code[i:j]) and j + 1 < n
                            and isdig(code[j + 1])):
                        j += 1
                        continue
                    break
                j += 1
            try:
                tok = classify_word(code[i:j])
            except LexError as e:
                raise LexError(str(e), (base + i, base + j))
            add(tok, i, j)
            i = j
    return toks


class LineInfo:
    __slots__ = ("no", "indent", "toks", "errs", "blank", "opens", "first", "code", "lead", "end")


def lex_line(no, raw):
    li = LineInfo()
    li.no, li.errs, li.toks, li.opens = no, [], None, False
    li.indent, li.first, li.code = 0, "", ""
    li.lead, li.end = 0, 0
    if len(raw) > MAX_LINE:
        li.errs.append("line too long (%d > %d characters)" % (len(raw), MAX_LINE))
    code, _comment = split_comment(raw)
    code = code.rstrip(WS)
    if not code.strip(WS):
        li.blank = True
        return li
    li.blank = False
    lead = len(code) - len(code.lstrip(WS))
    if "\t" in code[:lead]:
        li.errs.append(msg_at("tab in indentation (use spaces)", (0, lead)))
    li.indent = lead
    li.lead, li.end = lead, len(code)
    body = code[lead:]
    li.code = body
    li.opens = body.endswith(":")
    li.first = aupper(asplit(body, 1)[0])
    for k, ch in enumerate(body):
        o = ord(ch)
        if o < 0x20 or o > 0x7E:
            li.errs.append(msg_at("character 0x%02x outside a comment (printable ASCII only)" % o,
                                  (lead + k, lead + k + 1)))
            return li
    try:
        li.toks = lex_tokens(body, lead)
    except LexError as e:
        li.errs.append(msg_at(str(e), e.span))
    return li


# ---- AST -------------------------------------------------------------------
class Node:
    """Expression or statement node.

    kind: num str list cond var arg param const op call kw pred
          assign opstmt callstmt ctxstmt break stop ret
    """
    def __init__(self, kind, text, line, **kw):
        self.kind, self.text, self.line = kind, text, line
        self.val = None
        self.args = []
        self.kinds = []
        self.op = None
        self.name = None
        self.node = None
        self.span = None
        for k, v in kw.items():
            setattr(self, k, v)


class Mod:
    def __init__(self, kind, line, args=None, node=None):
        self.kind, self.line = kind, line
        self.args = args or []
        self.node = node


class Line:
    def __init__(self, no, indent):
        self.no, self.indent = no, indent
        self.mods = []
        self.stmt = None
        self.decl = None
        self.in_block = None
        self.children = []
        self.error = False
        self.opens = False
        self.first_word = ""

    def first_kind(self):
        return self.mods[0].kind if self.mods else None


class Const:
    def __init__(self, file, line, name, span=None):
        self.file, self.line, self.name, self.span = file, line, name, span
        self.type = None     # INT STR LIST COND
        self.val = None
        self.bad = False


Param = O.Param


class VarInfo:
    def __init__(self, name, init, scope, slot, line):
        self.name = name
        self.init = init
        self.scope = scope  # "FUNC" or InBlock
        self.slot = slot    # 0..15
        self.line = line


class InBlock:
    def __init__(self, kind, line=0, ticks=0, ch=None, num=0, clock=None, name=None):
        self.kind = kind    # "CLOCK", "NOTE", "CC", "TRIG", "GATE"
        self.line = line
        self.ticks = clock if clock is not None else ticks
        self.clock = self.ticks
        self.ch = ch        # None or 1..16 or param name; GATE: the output 1..4
        self.num = num      # if CC: 0..127 or param name
        self.name = name    # GATE: the master's function name (upper case, <= 8 characters)
        self.vars = OrderedDict() # vars local to this in-block

    def key(self):
        return (self.kind, self.ticks, self.ch, self.num, self.name)

    def shown(self):
        if self.kind == "CLOCK":
            return "clock %d" % self.ticks
        if self.kind == "NOTE":
            return ("note %s" % self.ch) if self.ch is not None else "note"
        if self.kind == "CC":
            return "cc %s %s" % (self.ch, self.num)
        if self.kind == "TRIG":
            return "trig %s%s" % (self.ch, {1: " all", 2: " script"}.get(self.num, ""))
        if self.kind == "GATE":
            return "gate %s %s" % (show(self.name), self.ch)
        return show(self.kind)


class Func:
    def __init__(self, prog, file, line, name):
        self.prog, self.file, self.line, self.name = prog, file, line, name
        self.args = []
        self.lines = []          # LineInfo of the body
        self.params = OrderedDict()   # name -> Param (or tuple)
        self.menus = []          # (levels, action, line)
        self.page = None         # (label, line) of the `page "LABEL"` declaration, or None
        self.module = None       # (label, line) of the `module "NAME"` declaration, or None
        self.clock = None        # (ticks, line) of the `clock N` declaration, or None
        self.launch = None       # (ticks, line) of the `launch N` declaration, or None (OTB aux tail)
        self.input_sample = False # input sample declaration
        self.confirm = False
        self.confirm_line = None # line of confirm declaration, or None
        self.confirm_text = None # (text, line): the first `confirm "TEXT"` with a non-empty text (OTB aux tail), or None
        self.body = []           # top-level Line list
        self.vars = OrderedDict() # name -> VarInfo
        self.in_blocks = []      # list of (InBlock, line)
        self.errors = []
        self.has_header_error = False
        self.registered = False
        self.name_span = None
        self.arg_spans = {}      # argument name -> (col, end) on the DEF line
        self.unused = set()      # arguments never used in the body

    @property
    def is_live(self):
        return bool(self.in_blocks or self.module is not None)


    def err(self, line, msg, hidden=False, span=None):
        if span is None and line == self.line and getattr(msg, "span", None) is None:
            span = self.name_span
        d = self.prog.diag(self.file, line, self.name, "error", msg, hidden, span)
        self.errors.append(d)
        return d

    def warn(self, line, msg, span=None):
        if span is None and line == self.line:
            span = self.name_span
        return self.prog.diag(self.file, line, self.name, "warning", msg, span=span)

    @property
    def refused(self):
        return bool(self.errors)


def describe(op):
    n = op.min_args
    if n == 0 and not op.args:
        return "no argument"
    names = " ".join(O.kind_name(k) for k in op.args)   # lower case
    more = " or more" if any(isinstance(k, O.REST) for k in op.args) else ""
    return "%d%s argument%s (%s)" % (n, more, "" if n == 1 and not more else "s", names)


def tokspan(li, k):
    """Column span of token k of a lexed line, or None."""
    if li.toks and 0 <= k < len(li.toks):
        return li.toks[k].span
    return None


def iter_lines(lines):
    for ln in lines:
        yield ln
        for sub in iter_lines(ln.children):
            yield sub


def iter_nodes(n):
    yield n
    for a in n.args:
        for x in iter_nodes(a):
            yield x
    if n.node is not None:
        for x in iter_nodes(n.node):
            yield x


def line_roots(ln):
    """Every expression/statement root of a line."""
    for m in ln.mods:
        for a in m.args:
            yield a
        if m.node is not None:
            yield m.node
    if ln.stmt is not None:
        yield ln.stmt


def func_calls(fn):
    """(call node) for every user-function call in fn, in source order."""
    out = []
    for ln in iter_lines(fn.body):
        for r in line_roots(ln):
            for n in iter_nodes(r):
                if n.kind == "call":
                    out.append(n)
    return out


def reserved_reason(name):
    if name in KEYWORDS:
        return "language keyword"
    if name in VARS:
        return "variable"
    if name in O.OPS:
        return "operation of the API"
    if name in COND_WORDS:
        return "trig condition"
    return None


def name_problem(text, tok_kind="WORD"):
    """Message if `text` is not a valid user name, else None."""
    if tok_kind == "STR" or (tok_kind != "WORD" and not NAME_RE.match(text)):   # the text of a string is not a name
        return "%s is not a valid name" % q(show(text))
    if "." in text:
        return "user names have no dot (dots belong to the API)"
    if not NAME_RE.match(text):
        return "%s is not a valid name (a letter, then letters, digits or _)" % q(show(text))
    if len(text) > NAME_MAX:
        return "name is too long (%d > %d characters)" % (len(text), NAME_MAX)
    r = reserved_reason(text)
    if r:
        return "reserved name (%s)" % r
    return None


# ---- program ---------------------------------------------------------------
class Program:
    def __init__(self):
        self.diags = []
        self.funcs = []
        self.consts = {}         # name -> Const (first)
        self.const_list = []
        self.fmap = {}           # name -> first registered Func
        self.files = []
        self.raw = {}            # label -> list of raw lines
        self.call_span = {}      # (caller, callee) -> span of the first call
        self.seq = 0
        self.file_errors = 0

    def diag(self, file, line, func, level, msg, hidden=False, span=None):
        self.seq += 1
        if span is None:
            span = getattr(msg, "span", None)
        d = Diag(file, line, func, level, str(msg), hidden, self.seq, span)
        self.diags.append(d)
        return d

    def file_err(self, file, line, msg, span=None):
        self.file_errors += 1
        return self.diag(file, line, "-", "error", msg, span=span)

    # ------------------------------------------------------------ loading
    def load_text(self, label, text):
        self.files.append(label)
        raw_lines = re.split(r"\r\n|\r|\n", text)
        self.raw[label] = raw_lines
        infos = [lex_line(no, raw) for no, raw in enumerate(raw_lines, 1)]
        cur = None
        first_code = True
        refuse = None
        mine = []

        def report(li, func):
            for m in li.errs:
                if func is None:
                    self.file_err(label, li.no, m)
                else:
                    func.err(li.no, m)

        for li in infos:
            if li.blank:
                if li.errs:
                    report(li, cur)
                continue
            if li.indent == 0:
                w = li.first
                if w == "OTYPE":
                    report(li, None)
                    cur = None
                    if not first_code:
                        self.file_err(label, li.no, "otype must be the first line of the file",
                                      tokspan(li, 0))
                    elif (li.toks and len(li.toks) == 2 and li.toks[1].kind == "NUM"):
                        if li.toks[1].val != 1:
                            refuse = li.toks[1].val
                            self.file_err(label, li.no,
                                          "otype %d is not supported (only otype 1): file refused"
                                          % li.toks[1].val, tokspan(li, 1))
                    else:
                        self.file_err(label, li.no, "otype expects a version number",
                                      tokspan(li, 1) or tokspan(li, 0))
                elif w == "CONST":
                    report(li, None)
                    cur = None
                    self.load_const(label, li)
                elif w == "DEF":
                    cur = self.load_def(label, li)
                    mine.append(cur)
                elif w == "VAR":
                    report(li, cur)
                    sp = tokspan(li, 0)
                    if cur is None:
                        self.file_err(label, li.no, "var at file level is not supported yet", sp)
                    else:
                        cur.err(li.no, "var at file level is not supported yet", span=sp)
                else:
                    report(li, cur)
                    msg = ("%s at column 1: only def, const and otype start in column 1 "
                           "(indent the body of a function)" % show(li.first))
                    sp = tokspan(li, 0)
                    if cur is None:
                        self.file_err(label, li.no, msg, sp)
                    else:
                        cur.err(li.no, msg, span=sp)

                first_code = False
            else:
                first_code = False
                if cur is None:
                    report(li, None)
                    self.file_err(label, li.no, "indented line outside any function")
                else:
                    cur.lines.append(li)
        if refuse is not None:
            for f in mine:
                f.err(f.line, "file refused (otype %d)" % refuse, hidden=True)

    def load_const(self, label, li):
        toks = li.toks
        # the errors of the line itself (lexical, too long) were reported once by load_text
        if toks is None:
            # still register the name when readable, to avoid cascades
            parts = asplit(li.code)
            if len(parts) > 1 and NAME_RE.match(aupper(parts[1])):
                c = Const(label, li.no, aupper(parts[1]))
                c.bad = True
                self.add_const(c)
            return
        if len(toks) < 2 or toks[1].kind != "WORD":
            self.file_err(label, li.no, "const expects a name and a value",
                          tokspan(li, 1) or tokspan(li, 0))
            return
        name = toks[1].text
        c = Const(label, li.no, name, toks[1].span)
        prob = name_problem(name)
        if prob:
            self.file_err(label, li.no, "const %s: %s" % (show(name), prob), c.span)
            c.bad = True
        if len(toks) < 3:
            self.file_err(label, li.no, "const %s has no value" % show(name), c.span)
            c.bad = True
        elif len(toks) > 3:
            self.file_err(label, li.no, "const takes a single constant (no expressions)",
                          (toks[3].col, toks[-1].end))
            c.bad = True
        else:
            v = toks[2]
            if v.kind == "NUM":
                c.type, c.val = "INT", v.val
            elif v.kind == "STR":
                c.type, c.val = "STR", v.val
            elif v.kind == "LIST":
                c.type, c.val = "LIST", v.val
            elif v.kind == "COND":
                c.type, c.val = "COND", v.val
            elif v.kind == "WORD" and v.text == "FILL":
                c.type, c.val = "COND", "FILL"
            else:
                self.file_err(label, li.no,
                              "const %s: the value must be a number, a string, a list or a "
                              "condition (no expressions)" % show(name), v.span)
                c.bad = True
        self.add_const(c)

    def add_const(self, c):
        self.const_list.append(c)
        if c.name not in self.consts:
            self.consts[c.name] = c

    def load_def(self, label, li):
        parts = asplit(li.code.replace("|", " | "))
        # column spans of the same words, found in the raw text of the line
        spans = [(li.lead + m.start(), li.lead + m.end())
                 for m in re.finditer(r"\||[^ \t|]+", li.code)]
        name = aupper(parts[1]) if len(parts) > 1 else "?"
        f = Func(self, label, li.no, name)
        self.funcs.append(f)
        if len(spans) > 1:
            f.name_span = spans[1]
        for m in li.errs:
            f.err(li.no, m)
        # header: DEF NAME [ | ARG ... ]
        if len(parts) < 2:
            f.err(li.no, "def expects a function name", span=spans[0] if spans else None)
            f.has_header_error = True
            return f
        rest = [aupper(p) for p in parts[2:]]
        rspan = spans[2:]
        prob = name_problem(name)
        if prob:
            f.err(li.no, prob, span=f.name_span)
            f.has_header_error = True
        if rest:
            if rest[0] != "|":
                f.err(li.no, "expected '|' before the arguments (def %s | a b)" % show(name),
                      span=rspan[0] if rspan else None)
                f.has_header_error = True
            else:
                args = rest[1:]
                if not args:
                    f.err(li.no, "'|' without arguments: write def %s alone for no argument" % show(name),
                          span=rspan[0] if rspan else None)
                for k, a in enumerate(args):
                    sp = rspan[k + 1] if k + 1 < len(rspan) else None
                    if a == "|" or a == ":":
                        f.err(li.no, "unexpected %s in the argument list" % q(a), span=sp)
                        continue
                    p = name_problem(a)
                    if p:
                        f.err(li.no, "argument %s: %s" % (show(a), p), span=sp)
                    elif a in f.args:
                        f.err(li.no, "duplicate argument %s" % show(a), span=sp)
                    if a not in f.arg_spans and sp is not None:
                        f.arg_spans[a] = sp
                    f.args.append(a)
                if len(f.args) > MAX_ARGS:
                    f.err(li.no, "takes %d arguments (maximum %d)" % (len(f.args), MAX_ARGS),
                          span=(rspan[1 + MAX_ARGS][0], rspan[-1][1])
                          if len(rspan) > 1 + MAX_ARGS else None)
        return f

    # ------------------------------------------------------------ analysis
    def run(self):
        self.pass_names()
        for f in self.funcs:
            if f.registered:
                BodyParser(self, f).parse()
        self.find_unused()
        self.infer_kinds()
        self.check_all()
        self.pass_graph()
        self.pass_page()
        self.pass_module()
        self.pass_live()
        self.pass_events()
        self.pass_input_sample()
        self.pass_needs()
        self.pass_cost()
        self.pass_needs()
        self.pass_flags()
        self.pass_menu()
        if len(self.funcs) > MAX_FUNCS:
            f = self.funcs[MAX_FUNCS]
            f.warn(f.line, "%d functions loaded (maximum %d)" % (len(self.funcs), MAX_FUNCS))

    def pass_menu(self):
        cats = OrderedDict()
        for f in self.funcs:
            if f.refused:
                continue
            for levels, action, line in f.menus:
                cat = levels[0] if levels else "SCRIPTS"
                if len(cat) > 11:
                    cat = "SCRIPTS"
                if cat not in cats:
                    cats[cat] = []
                cats[cat].append((f, line))
        cat_names = list(cats.keys())
        if len(cat_names) > MENU_MAX_CATEGORIES:
            for cat in cat_names[MENU_MAX_CATEGORIES:]:
                for f, line in cats[cat]:
                    f.warn(line, "more than %d menu categories (excess categories ignored)" % MENU_MAX_CATEGORIES)
        for cat in cat_names[:MENU_MAX_CATEGORIES]:
            flist = cats[cat]
            if len(flist) > MENU_MAX_SCRIPTS_PER_CATEGORY:
                for f, line in flist[MENU_MAX_SCRIPTS_PER_CATEGORY:]:
                    f.warn(line, 'category "%s" has more than %d scripts (script not listed)' %
                           (cat, MENU_MAX_SCRIPTS_PER_CATEGORY))

    def pass_names(self):
        by_name = defaultdict(list)
        for f in self.funcs:
            if not f.has_header_error:
                by_name[f.name].append(f)
        for name, fl in by_name.items():
            f0 = fl[0]
            f0.registered = True
            self.fmap[name] = f0
            for f in fl[1:]:
                f.registered = True
        for name, fl in by_name.items():
            if len(fl) > 1:
                for i, f in enumerate(fl):
                    other = fl[1] if i == 0 else fl[0]
                    f.err(f.line, "duplicate function name (also defined at %s:%d)"
                          % (other.file, other.line), span=f.name_span)
        # constants
        by_c = defaultdict(list)
        for c in self.const_list:
            by_c[c.name].append(c)
        for name, cl in by_c.items():
            if len(cl) > 1:
                for i, c in enumerate(cl):
                    other = cl[1] if i == 0 else cl[0]
                    c.bad = True
                    self.file_err(c.file, c.line, "duplicate const %s (also defined at %s:%d)"
                                  % (show(name), other.file, other.line), c.span)
            if name in by_name:
                f = by_name[name][0]
                for c in cl:
                    c.bad = True
                    self.file_err(c.file, c.line, "const %s has the name of the function at %s:%d"
                                  % (show(name), f.file, f.line), c.span)
                for f in by_name[name]:
                    f.err(f.line, "function has the name of the const at %s:%d"
                          % (cl[0].file, cl[0].line), span=f.name_span)
        # arguments must not shadow constants or functions
        for f in self.funcs:
            for a in f.args:
                if a in self.consts:
                    f.err(f.line, "argument %s hides the const of the same name" % show(a),
                          span=f.arg_spans.get(a))
                elif a in self.fmap:
                    f.err(f.line, "argument %s has the name of a function" % show(a),
                          span=f.arg_spans.get(a))

    # ------------------------------------------------- unused arguments
    def find_unused(self):
        """An argument that never appears in the body takes any kind at a call site."""
        for f in self.funcs:
            if not f.registered or not f.args:
                continue
            words = set()
            blind = False
            for li in f.lines:
                if li.blank:
                    continue
                if li.toks is None:
                    blind = True      # a line that did not lex may use anything
                    break
                words.update(t.text for t in li.toks if t.kind == "WORD")
            if blind:
                continue
            for a in dict.fromkeys(f.args):
                if a not in words:
                    f.unused.add(a)
                    f.warn(f.line, "argument %s is unused" % show(a), span=f.arg_spans.get(a))

    # --------------------------------------------------- type inference
    def infer_kinds(self):
        self.uses = defaultdict(list)    # (func, arg) -> [(class, line)]
        self.edges = []                  # (func, arg, callee name, index, line)
        self.argkind = {}
        self.rep = False
        for f in self.funcs:
            if f.registered:
                self.check_func(f, rep=False)
        kinds = {}
        keys = [(f, a) for f in self.funcs for a in f.args]
        by_src = defaultdict(list)
        for e in self.edges:
            by_src[(e[0], e[1])].append(e)
        for _ in range(40):
            new = {}
            for key in keys:
                if key[1] in key[0].unused:
                    new[key] = "ANY"
                    continue
                U = set(c for c, _l in self.uses[key])
                for (_f, _a, g, k, _line) in by_src[key]:
                    callee = self.fmap.get(g)
                    if callee is not None and k < len(callee.args):
                        kk = kinds.get((callee, callee.args[k]))
                        if kk:
                            U.add(kk)
                S = U & {"STR", "LIST"}
                if S:
                    new[key] = "STR" if "STR" in S else "LIST"
                elif "INT" in U:
                    new[key] = "INT"
                elif "COND" in U:
                    new[key] = "COND"
                else:
                    new[key] = "INT"
            if new == kinds:
                break
            kinds = new
        self.argkind = kinds
        # constness of arguments at every call site (greatest fixpoint)
        self.callsites = defaultdict(list)
        for f in self.funcs:
            if f.registered:
                for c in func_calls(f):
                    g = self.fmap.get(c.name)
                    if g is not None:
                        self.callsites[g].append((f, c))
        const = {key: True for key in keys}
        changed = True
        while changed:
            changed = False
            for g, sites in self.callsites.items():
                for f, c in sites:
                    for k, a in enumerate(c.args):
                        if k < len(g.args) and const[(g, g.args[k])] and \
                                not self.is_const(f, a, const):
                            const[(g, g.args[k])] = False
                            changed = True
        self.constarg = const

    def is_const(self, f, n, const=None):
        const = self.constarg if const is None else const
        if n.kind == "num":
            return True
        if n.kind == "param":
            return True
        if n.kind == "const":
            c = self.consts.get(n.name)
            return c is not None and c.type == "INT"
        if n.kind == "arg":
            return const.get((f, n.name), False)
        return False

    # ----------------------------------------------------- checking
    def check_all(self):
        self.rep = True
        self.deferred = []
        for f in self.funcs:
            if f.registered:
                self.check_func(f, rep=True)
        for f, callee, line, msg, span in self.deferred:
            if not callee.errors:
                f.err(line, msg, span=span)

    def check_func(self, f, rep):
        loop_stack = []

        def walk(lines, loop):
            for ln in lines:
                lp = loop
                for m in ln.mods:
                    if m.kind in ("IF", "ELIF", "PROB", "EVERY", "SKIP"):
                        self.check_node(f, m.args[0], O.INT, rep)
                    elif m.kind == "L":
                        for a in m.args:
                            self.check_node(f, a, O.INT, rep)
                            if rep:
                                self.check_l_bound(f, a, m)
                        lp += 1
                    elif m.kind == "EACH":
                        lp += 1
                    elif m.kind == "CTX":
                        self.check_node(f, m.node, None, rep)
                s = ln.stmt
                if s is not None:
                    if s.kind == "assign":
                        self.check_node(f, s.args[0], O.INT, rep)
                    elif s.kind in ("opstmt", "callstmt", "ctxstmt"):
                        self.check_node(f, s.node, None, rep)
                        if rep and s.kind == "opstmt" and s.node.op.ret is not None:
                            f.warn(s.line, "the value of %s is ignored" % s.node.op.shown,
                                   span=s.node.span)
                    elif s.kind == "setstmt":
                        for i, a in enumerate(s.args[:-1]):
                            self.check_node(f, a, s.op.args[i], rep)
                        self.check_node(f, s.args[-1], s.op.ret or O.INT, rep)
                    elif s.kind == "ret":
                        if s.args:
                            self.check_node(f, s.args[0], O.INT, rep)
                    elif s.kind == "break" and rep and lp == 0:
                        f.err(s.line, "break outside l or each", span=s.span)
                walk(ln.children, lp)

        walk(f.body, 0)

    def check_l_bound(self, f, n, m):
        if n.kind == "param":
            p = f.params.get(n.name)
            if getattr(p, "kind", "INT") == "FOLDER":
                f.err(m.line, "folder param %s cannot be used as loop bound" % show(n.name), span=n.span)
                return
            return
        if n.kind == "num" or (n.kind == "const" and
                               self.consts.get(n.name) is not None and
                               self.consts[n.name].type == "INT"):
            return
        if n.kind == "arg":
            if self.constarg.get((f, n.name), True):
                return
            hint = ""
            for (cf, c) in self.callsites.get(f, []):
                k = f.args.index(n.name)
                if k < len(c.args) and not self.is_const(cf, c.args[k]):
                    hint = "; %s passes a non-constant value at %s:%d" % (show(cf.name), cf.file, c.line)
                    break
            f.err(m.line, "l bound must be constant (%s)%s" % (show(n.text), hint), span=n.span)
            return
        if n.kind in ("str", "list", "cond", "kw"):
            return   # type error already reported
        f.err(m.line, "l bound must be constant (%s): use a number, a const, a param or "
                      "an argument that is constant at every call" % show(n.text), span=n.span)

    def ntype(self, f, n):
        k = n.kind
        if k in ("num", "var", "var_named", "op", "call"):
            return "INT"
        if k == "param":
            p = f.params.get(n.name)
            if getattr(p, "kind", "INT") == "FOLDER":
                return "FOLDER"
            return "INT"
        if k == "const":
            return self.consts[n.name].type
        if k == "str":
            return "STR"
        if k == "list":
            return "LIST"
        if k == "cond":
            return "COND"
        if k == "arg":
            return self.argkind.get((f, n.name))
        return None

    WANT = {"INT": "a number", "STR": "a string", "LIST": "a list", "COND": "a condition",
            "ITEM": "a string or a number"}

    def check_node(self, f, n, exp, rep, op_name=None):
        k = n.kind
        if k in ("kw", "pred"):
            return
        if exp == O.NAME:
            if not (k == "str" or (k == "const" and self.consts[n.name].type == "STR")):
                f.err(n.line, "%s expects a function name, got %s" % (op_name or "cv", show(n.text)), span=n.span)
            elif len(n.val if k == "str" else self.consts[n.name].val) > 8:
                f.err(n.line, "%s: name is too long (8 characters at most)" % (op_name or "cv"), span=n.span)
            return
        if k == "op":
            for a, ak in zip(n.args, n.kinds):
                self.check_node(f, a, ak, rep, op_name=n.op.name)
            if rep and n.op.name == "MSG":
                self.check_msg(f, n)
        elif k == "call":
            for i, a in enumerate(n.args):
                self.check_node(f, a, ("USER", n.name, i), rep)
        if k == "arg" and not rep:
            if isinstance(exp, tuple):
                self.edges.append((f, n.name, exp[1], exp[2], n.line))
            elif exp is not None:
                self.uses[(f, n.name)].append((self.exp_class(exp), n.line))
            return
        if not rep or exp is None:
            return
        have = self.ntype(f, n)
        strict = isinstance(exp, tuple)
        if strict:
            callee = self.fmap.get(exp[1])
            if callee is None or exp[2] >= len(callee.args):
                return
            ecls = self.argkind.get((callee, callee.args[exp[2]]), "INT")
        else:
            ecls = self.exp_class(exp)
        if ecls == O.NAME:
            ecls = "STR"
        if have == "FOLDER":
            ok = (op_name == "DIR" and ecls == "STR")
        else:
            ok = ((have == ecls) or (ecls == "COND" and not strict and have == "INT")
                  or ecls == "ANY" or have == "ANY" or (ecls == "ITEM" and have in ("INT", "STR")))
        if not ok:
            msg = self.type_msg(n, have, ecls)
            if strict:
                # a callee refused for another reason has unreliable kinds: decide later
                self.deferred.append((f, callee, n.line, msg, n.span))
            else:
                f.err(n.line, msg, span=n.span)
            return
        # constant ranges
        if not strict and exp in O.RANGES:
            v = None
            if k == "num":
                v = n.val
            elif k == "const" and self.consts[n.name].type == "INT":
                v = self.consts[n.name].val
            if v is not None:
                lo, hi = O.RANGES[exp]
                if not lo <= v <= hi:
                    f.err(n.line, "%d is out of range for %s (%d..%d)" % (v, show(exp), lo, hi),
                          span=n.span)

    def msg_items(self, f, n):
        """[(is_string, text or None)] of the items of a msg: a string constant is text, anything else is a number (worst
        case 6 characters); an argument that carries a string is of unknown length (None)."""
        out = []
        for a in n.args:
            if a.kind == "str":
                out.append((True, a.val))
            elif a.kind == "const" and self.consts[a.name].type == "STR":
                out.append((True, self.consts[a.name].val))
            elif a.kind == "arg" and self.argkind.get((f, a.name)) == "STR":
                out.append((True, None))
            else:
                out.append((False, None))
        return out

    def check_msg(self, f, n):
        if len(n.args) > MAX_MSG_ITEMS:
            f.err(n.line, "msg takes at most %d items (got %d)" % (MAX_MSG_ITEMS, len(n.args)), span=n.span)
            return
        items = self.msg_items(f, n)
        w = len(items) - 1 + sum(len(t) if t is not None else 0 if s else 6 for s, t in items)
        if w > MAX_MSG:
            f.err(n.line, "msg is too long for the screen: %d characters in the worst case (a number counts as 6, "
                  "items are separated by a space), at most %d fit" % (w, MAX_MSG), span=n.span)

    @staticmethod
    def exp_class(exp):
        if exp in O.NUMERIC_KINDS:
            return "INT"
        return exp   # STR LIST COND

    def type_msg(self, n, have, want):
        if have == "FOLDER" and want == "INT":
            return "folder param %s cannot be used in arithmetic" % show(n.name)
        if have == "FOLDER":
            return "folder param %s cannot be used here" % show(n.name)
        if have == "COND" and want == "INT":
            m = re.fullmatch(r"([0-9]+)%", n.text)
            if m:
                return "%s is a trig condition; write prob %s:" % (n.text, m.group(1))
            return "%s is a trig condition, not a number" % show(n.text)
        if n.kind == "arg":
            return ("argument %s is used as %s elsewhere, but %s is expected here"
                    % (show(n.name), self.WANT.get(have, have), self.WANT[want]))
        return "expected %s, got %s (%s)" % (self.WANT[want], self.WANT.get(have, "?"), show(n.text))

    # ------------------------------------------------------ call graph
    def callees(self, f):
        out = OrderedDict()
        for c in func_calls(f):
            g = self.fmap.get(c.name)
            if g is not None and g not in out:
                out[g] = c.line
                self.call_span[(f, g)] = c.span
        return out

    def pass_graph(self):
        funcs = [f for f in self.funcs if f.registered]
        self.cg = {f: self.callees(f) for f in funcs}
        # Tarjan SCC
        index, low, onstack, stack, sccs = {}, {}, set(), [], []
        counter = [0]
        sys.setrecursionlimit(10000)

        def strong(v):
            index[v] = low[v] = counter[0]
            counter[0] += 1
            stack.append(v)
            onstack.add(v)
            for w in self.cg.get(v, ()):
                if w not in index:
                    strong(w)
                    low[v] = min(low[v], low[w])
                elif w in onstack:
                    low[v] = min(low[v], index[w])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    onstack.discard(w)
                    comp.append(w)
                    if w is v:
                        break
                sccs.append(comp)

        for f in funcs:
            if f not in index:
                strong(f)
        self.cyclic = set()
        for comp in sccs:
            if len(comp) > 1 or comp[0] in self.cg.get(comp[0], ()):
                members = set(comp)
                for f in comp:
                    self.cyclic.add(f)
                    path = self.cycle_path(f, members)
                    f.err(f.line, "loop %s" % ">".join(show(g.name) for g in path))
        # depth (functions in a chain); cyclic functions are already refused
        memo = {}

        def height(f, seen=()):
            if f in memo:
                return memo[f]
            best, bp = 0, []
            for g in self.cg.get(f, ()):
                if g in self.cyclic or g in seen:
                    continue
                h, p = height(g, seen + (f,))
                if h > best:
                    best, bp = h, p
            memo[f] = (best + 1, [f] + bp)
            return memo[f]

        for f in funcs:
            if f in self.cyclic:
                continue
            h, p = height(f)
            if h > MAX_DEPTH:
                f.err(f.line, "call depth %d > %d (%s)" % (h, MAX_DEPTH, ">".join(show(g.name) for g in p)))

    def cycle_path(self, start, members):
        # DFS inside the SCC for a path start -> ... -> start
        path = [start]
        seen = set()

        def dfs(v):
            for w in self.cg.get(v, ()):
                if w not in members:
                    continue
                if w is start:
                    path.append(w)
                    return True
                if w in seen:
                    continue
                seen.add(w)
                path.append(w)
                if dfs(w):
                    return True
                path.pop()
            return False

        dfs(start)
        return path

    def pass_page(self):
        """A `page` function must not use an op that reads or writes the card, the sample slots or the pool
        (namespaces PAGE_FORBIDDEN_NS: dir ext match file.*, static.* slots.*; not the whole of the IO_R
        family, which also holds the pattern reads has.trig, pat.len, pl.get ...), directly or through what it
        calls.  Rule (the C checker does the same): first_io(f) = the first such op of f's own text (lowest line;
        the first in the order of the walk on a tie), else, for the callees of f in
        order of first call (cyclic ones skipped: they are refused anyway), the first one that has a first_io,
        found at the line of that first call.  A page function that has one is refused: `op NAME not allowed in
        a page function` on that line, NAME the op of first_io.  The other functions are not concerned."""
        memo = {}

        def first_io(f):
            if f in memo:
                return memo[f]
            memo[f] = None
            best = None
            for ln in iter_lines(f.body):
                for r in line_roots(ln):
                    for n in iter_nodes(r):
                        if n.kind in ("op", "pred", "setstmt") and n.op is not None \
                                and n.op.ns in PAGE_FORBIDDEN_NS and (best is None or ln.no < best[1]):
                            best = (n.op, ln.no)
            if best is None:
                for g, line in self.cg.get(f, {}).items():
                    if g in self.cyclic:
                        continue
                    r = first_io(g)
                    if r is not None:
                        best = (r[0], line)
                        break
            memo[f] = best
            return best

        for f in self.funcs:
            if f.registered and f.page is not None:
                r = first_io(f)
                if r is not None:
                    f.err(r[1], "op %s not allowed in a page function" % r[0].shown)

    def pass_module(self):
        """A `module` function must not use an op that reads or writes the card, the sample slots or the pool
        (namespaces PAGE_FORBIDDEN_NS: dir ext match file.*, static.* slots.*), or confirm, directly or through
        what it calls. Same rule as pass_page."""
        memo = {}

        def first_module_bad(f):
            if f in memo:
                return memo[f]
            memo[f] = None
            best = None
            if f.confirm:
                cline = f.confirm_line if f.confirm_line is not None else f.line
                best = ("confirm not allowed in a live script", cline)
            for ln in iter_lines(f.body):
                for r in line_roots(ln):
                    for n in iter_nodes(r):
                        if n.kind in ("op", "pred", "setstmt") and n.op is not None \
                                and n.op.ns in PAGE_FORBIDDEN_NS and (best is None or ln.no < best[1]):
                            best = ("op %s not allowed in a live script" % n.op.shown, ln.no)
            if best is None:
                for g, line in self.cg.get(f, {}).items():
                    if g in self.cyclic:
                        continue
                    r = first_module_bad(g)
                    if r is not None:
                        best = (r[0], line)
                        break
            memo[f] = best
            return best

        for f in self.funcs:
            if f.registered and f.module is not None:
                r = first_module_bad(f)
                if r is not None:
                    f.err(r[1], r[0])

    def pass_live(self):
        """A live script must not run a menu-only operation (`pat.copy`): it is destructive and its confirmation
        cannot be asked from a live wake. Same rule as pass_module, for the `in` blocks (a live script), directly or
        through what it calls: the first such op of f's own text (lowest line; the first in the walk on a tie), else
        the first callee (in order of first call, cyclic ones skipped) that has one. `op NAME not allowed in a live
        script` on that line."""
        memo = {}

        def first_menu_only(f):
            if f in memo:
                return memo[f]
            memo[f] = None
            best = None
            for ln in iter_lines(f.body):
                for r in line_roots(ln):
                    for n in iter_nodes(r):
                        if n.kind in ("op", "pred", "setstmt") and n.op is not None \
                                and getattr(n.op, "menu_only", False) and (best is None or ln.no < best[1]):
                            best = (n.op, ln.no)
            if best is None:
                for g, line in self.cg.get(f, {}).items():
                    if g in self.cyclic:
                        continue
                    r = first_menu_only(g)
                    if r is not None:
                        best = (r[0], line)
                        break
            memo[f] = best
            return best

        for f in self.funcs:
            if f.registered and f.is_live:
                r = first_menu_only(f)
                if r is not None:
                    f.err(r[1], "op %s not allowed in a live script" % r[0].shown)

        nx_memo = {}

        def first_nx(f):
            if f in nx_memo:
                return nx_memo[f]
            nx_memo[f] = None
            best = None
            for ln in iter_lines(f.body):
                for r in line_roots(ln):
                    for n in iter_nodes(r):
                        if n.kind in ("op", "pred", "setstmt") and n.op is not None \
                                and (n.op.name.startswith("NX.") or n.op.name in ("GATE", "OUT", "CV")) \
                                and (best is None or ln.no < best[1]):
                            best = (n.op, ln.no)
            if best is None:
                for g, line in self.cg.get(f, {}).items():
                    if g in self.cyclic:
                        continue
                    r = first_nx(g)
                    if r is not None:
                        best = (r[0], line)
                        break
            nx_memo[f] = best
            return best

        for f in self.funcs:
            if f.registered and f.module is None:
                r = first_nx(f)
                if r is not None:
                    f.err(r[1], "op %s only allowed in a live script" % r[0].shown)

    def pass_events(self):
        def check(f, sub, n, kind, genre):
            nm = n.op.name
            if nm in ("NOTE.NUM", "NOTE.VEL", "NOTE.CH") and kind != "NOTE":
                f.err(sub.no, "%s only allowed in an in note block" % n.op.shown)
            elif nm in ("CC.NUM", "CC.VAL", "CC.CH") and kind != "CC":
                f.err(sub.no, "%s only allowed in an in cc block" % n.op.shown)
            elif nm == "TRIG.STEP" and kind != "TRIG":
                f.err(sub.no, "%s only allowed in an in trig block" % n.op.shown)
            elif nm == "TRIG.HIT" and (kind != "TRIG" or genre == 2):
                f.err(sub.no, "%s only allowed in an in trig or in trig all block" % n.op.shown)
            elif nm == "TRIG.VAL" and (kind != "TRIG" or genre != 2):
                f.err(sub.no, "%s only allowed in an in trig script block" % n.op.shown)

        for f in self.funcs:
            for ln in f.body:
                kind = ln.in_block.kind if ln.in_block is not None else None
                genre = ln.in_block.num if ln.in_block is not None else None
                for sub in [ln] + list(iter_lines(ln.children)):
                    for r in line_roots(sub):
                        for n in iter_nodes(r):
                            if n.kind == "op":
                                check(f, sub, n, kind, genre)

    def pass_input_sample(self):
        """A function using an op of namespace AUDIO starting with SRC. (src.*)
        directly or through what it calls must be declared with `input sample`.
        Any function without `input sample` that reaches a `src.*` op is refused:
        `op NAME needs input sample` on the op's line (or the line of the call)."""
        memo = {}

        def first_src(f):
            if f in memo:
                return memo[f]
            memo[f] = None
            best = None
            for ln in iter_lines(f.body):
                for r in line_roots(ln):
                    for n in iter_nodes(r):
                        if n.kind in ("op", "pred", "setstmt") and n.op is not None \
                                and n.op.name.startswith("SRC.") and (best is None or ln.no < best[1]):
                            best = (n.op, ln.no)
            if best is None:
                for g, line in self.cg.get(f, {}).items():
                    if g in self.cyclic:
                        continue
                    r = first_src(g)
                    if r is not None:
                        best = (r[0], line)
                        break
            memo[f] = best
            return best

        for f in self.funcs:
            if f.registered and not getattr(f, "input_sample", False):
                r = first_src(f)
                if r is not None:
                    f.err(r[1], "op %s needs input sample" % r[0].shown)

    def pass_needs(self):
        changed = True
        while changed:
            changed = False
            for f in self.funcs:
                if not f.registered or f.refused:
                    continue
                for g, line in self.callees(f).items():
                    if g.refused:
                        f.err(line, "needs %s" % show(g.name), span=self.call_span.get((f, g)))
                        changed = True
                        break

    # ------------------------------------------------------------ cost
    def pass_cost(self):
        self.compute_ranges()
        self.cmemo = {}
        self.maxcost = {}
        for f in self.funcs:
            if not f.registered or f.refused:
                continue
            c = self.cost_func(f, tuple(self.rng.get((f, a)) for a in f.args))
            self.maxcost[f] = c
            if f.menus and c > MENU_BUDGET:
                f.err(f.line, "too long: %d/%d" % (min(c, COST_SAT), MENU_BUDGET))
            if f.module is not None and c > MODULE_BUDGET:
                f.err(f.line, "too long: %d/%d" % (min(c, COST_SAT), MODULE_BUDGET))

    def compute_ranges(self):
        rng = {}
        self.rng = rng
        for _ in range(30):
            changed = False
            for g, sites in self.callsites.items():
                for f, c in sites:
                    for k, a in enumerate(c.args):
                        if k >= len(g.args):
                            continue
                        iv = self.ival(f, a, None)
                        if iv is PENDING:
                            continue
                        key = (g, g.args[k])
                        if key in rng and rng[key] is None:
                            continue
                        if iv is None:
                            new = None
                        elif key in rng:
                            o = rng[key]
                            new = (min(o[0], iv[0]), max(o[1], iv[1]))
                        else:
                            new = iv
                        if key not in rng or rng[key] != new:
                            rng[key] = new
                            changed = True
            if not changed:
                break

    def ival(self, f, n, env):
        if n.kind == "num":
            return (n.val, n.val)
        if n.kind == "const":
            c = self.consts.get(n.name)
            if c is not None and c.type == "INT":
                return (c.val, c.val)
            return None
        if n.kind == "param":
            p = f.params.get(n.name)
            return (p[1], p[2]) if p else None
        if n.kind == "arg":
            if env is None:
                key = (f, n.name)
                if key in self.rng:
                    return self.rng[key]
                return PENDING if self.callsites.get(f) else None
            return env.get(n.name)
        return None

    def cost_func(self, f, key):
        k = (f, key)
        if k in self.cmemo:
            return self.cmemo[k]
        self.cmemo[k] = 0
        env = dict(zip(f.args, key))
        c = self.cost_block(f, f.body, env)
        self.cmemo[k] = c
        return c

    def cost_block(self, f, lines, env):
        total, i = 0, 0
        while i < len(lines):
            ln = lines[i]
            if ln.first_kind() == "IF":
                chain = [ln]
                j = i + 1
                while j < len(lines) and lines[j].first_kind() in ("ELIF", "ELSE"):
                    chain.append(lines[j])
                    j += 1
                acc = 0
                for c in reversed(chain):
                    inner = self.cost_mods(f, c, 1, env)
                    if c.first_kind() == "ELSE":
                        acc = inner
                    else:
                        acc = self.cost_expr(f, c.mods[0].args[0], env) + max(inner, acc)
                total += acc
                i = j
            elif (not ln.mods and ln.stmt is not None and ln.stmt.kind == "ctxstmt"
                  and ln.stmt.node.op.name == "TRK"):
                # a multi-track `trk` runs the rest of the block once per track (spec 6.2)
                total += self.cost_expr(f, ln.stmt.node, env)
                total += self.trk_count(ln.stmt.node) * self.cost_block(f, lines[i + 1:], env)
                break
            else:
                total += self.cost_mods(f, ln, 0, env)
                i += 1
        return total

    @staticmethod
    def trk_count(node):
        """How many times a `trk` runs what it governs, at most: the tracks listed (`all` = 8)."""
        a0 = node.args[0]
        if a0.kind == "kw":
            return 8 if a0.name == "ALL" else 1
        return max(1, len(node.args))

    def cost_mods(self, f, ln, idx, env):
        if idx >= len(ln.mods):
            if ln.stmt is not None:
                return self.cost_stmt(f, ln.stmt, env)
            return self.cost_block(f, ln.children, env)
        m = ln.mods[idx]
        inner = self.cost_mods(f, ln, idx + 1, env)
        if m.kind in ("IF", "ELIF"):
            return self.cost_expr(f, m.args[0], env) + inner
        if m.kind == "ELSE":
            return inner
        if m.kind == "L":
            a, b = (self.ival(f, x, env) for x in m.args)
            if a is None or b is None:
                iters = 1
            else:
                iters = max(abs(b[1] - a[0]), abs(a[1] - b[0])) + 1
            return iters * inner
        if m.kind == "EACH":
            return EACH_ITERS * inner
        if m.kind in ("PROB", "EVERY", "SKIP"):
            return self.cost_expr(f, m.args[0], env) + inner
        if m.kind == "CTX":
            mult = self.trk_count(m.node) if m.node.op.name == "TRK" else 1
            return self.cost_expr(f, m.node, env) + mult * inner
        return inner

    def cost_stmt(self, f, s, env):
        if s.kind in ("assign", "assign_var_named"):
            return self.cost_expr(f, s.args[0], env)
        if s.kind in ("opstmt", "callstmt", "ctxstmt"):
            return self.cost_expr(f, s.node, env)
        if s.kind == "ret" and s.args:
            return self.cost_expr(f, s.args[0], env)
        if s.kind == "setstmt":
            return 1 + sum(self.cost_expr(f, a, env) for a in s.args)
        return 0

    def cost_expr(self, f, n, env):
        if n.kind == "op":
            op = n.op
            if op.lazy and n.args:
                alts = [self.cost_expr(f, a, env) for a in n.args[1:]]
                return op.cost + self.cost_expr(f, n.args[0], env) + (max(alts) if alts else 0)
            return op.cost + sum(self.cost_expr(f, a, env) for a in n.args)
        if n.kind == "call":
            g = self.fmap.get(n.name)
            c = sum(self.cost_expr(f, a, env) for a in n.args)
            if g is not None:
                c += self.cost_func(g, tuple(self.ival(f, a, env) for a in n.args))
            return c
        return 0

    # ----------------------------------------------------------- flags
    def pass_flags(self):
        self.destructive = {}
        direct = {}
        for f in self.funcs:
            d = False
            if f.registered:
                for ln in iter_lines(f.body):
                    for r in line_roots(ln):
                        for n in iter_nodes(r):
                            if n.kind == "op" and n.op.destructive:
                                d = True
            direct[f] = d
        for f in self.funcs:
            seen, todo, dd = set(), [f], False
            while todo:
                x = todo.pop()
                if x in seen:
                    continue
                seen.add(x)
                if direct.get(x):
                    dd = True
                todo.extend(self.cg.get(x, ()))
            self.destructive[f] = dd

    # ---------------------------------------------------------- results
    def sorted_diags(self, include_hidden=False):
        ds = [d for d in self.diags if include_hidden or not d.hidden]
        return sorted(ds, key=lambda d: (d.file, d.line, d.seq))

    def ok_funcs(self):
        return [f for f in self.funcs if not f.refused]

    def summary(self):
        ok = len(self.ok_funcs())
        bad = len(self.funcs) - ok
        w = sum(1 for d in self.diags if d.level == "warning")
        s = "%d functions OK, %d refused, %d warnings" % (ok, bad, w)
        if self.file_errors:
            s += " (%d file-level errors)" % self.file_errors
        return s

    def error_count(self):
        return sum(1 for d in self.diags if d.level == "error")


# ---- body parser -----------------------------------------------------------
def tokshow(t):
    """A token as shown in a message: a string keeps its case, in quotes; the rest is lower case."""
    return '"%s"' % t.text if t.kind == "STR" else show(t.text)


class BodyParser:
    def __init__(self, prog, fn):
        self.prog, self.fn = prog, fn
        self.toks, self.pos = [], 0
        self.line_no = 0

    # --- token helpers
    def peek(self, off=0):
        i = self.pos + off
        return self.toks[i] if i < len(self.toks) else None

    def last_tok(self):
        return self.toks[self.pos - 1] if 0 < self.pos <= len(self.toks) else None

    def next(self):
        t = self.peek()
        if t is None:
            raise ParseError("unexpected end of line", self.last_tok())
        self.pos += 1
        return t

    def at_end(self, off=0):
        t = self.peek(off)
        return t is None or (t.kind == "SYM" and t.text == ":")

    # --- expressions
    def parse_term(self):
        t = self.next()
        n = self.term(t)
        if n.span is None:
            n.span = t.span
        return n

    def term(self, t):
        if t.kind == "NUM":
            return Node("num", t.text, self.line_no, val=t.val)
        if t.kind == "STR":
            return Node("str", '"%s"' % t.text, self.line_no, val=t.val)
        if t.kind == "LIST":
            return Node("list", t.text, self.line_no, val=t.val)
        if t.kind == "COND":
            return Node("cond", t.text, self.line_no, val=t.val)
        if t.kind == "SYM":
            raise ParseError("unexpected '%s'" % t.text, t)
        return self.resolve_word(t)

    def resolve_word(self, t):
        w, fn, prog = t.text, self.fn, self.prog
        if retired(w):
            raise ParseError(retired(w), t)
        if w in VARS:
            return Node("var", w, self.line_no, name=w)
        if w in fn.vars:
            vinfo = fn.vars[w]
            cur_scope = getattr(self, "current_in_block", None)
            if vinfo.scope != "FUNC" and vinfo.scope is not cur_scope:
                raise ParseError("var %s is not visible in this block" % show(w), t)
            return Node("var_named", w, self.line_no, name=w, var_info=vinfo)
        if w in fn.args:
            return Node("arg", w, self.line_no, name=w)
        if w in fn.params:
            return Node("param", w, self.line_no, name=w)
        if w in prog.consts:
            if prog.consts[w].bad:
                raise ParseError("const %s is refused (see its error)" % show(w), t)
            return Node("const", w, self.line_no, name=w)
        if w in O.OPS:
            op = O.OPS[w]
            if op.stmt_only or op.ret is None:
                raise ParseError("%s gives no value: it is a statement" % op.shown, t)
            return self.op_call(op, t)
        if w in prog.fmap:
            return self.user_call(w, t)
        if w in KEYWORDS:
            raise ParseError("unexpected keyword %s" % show(w), t)
        raise ParseError("unknown name %s" % show(w), t)

    def op_call(self, op, t):
        nxt = self.peek()
        if nxt is not None and nxt.kind == "SYM" and nxt.text == "|":
            raise ParseError("'|' is for your own functions: %s takes its arguments directly "
                             "(%s)" % (op.shown, describe(op)), nxt)
        self.op_tok = t
        args, kinds = self.op_args(op, t)
        return Node("op", t.text, self.line_no, op=op, args=args, kinds=kinds, name=op.name,
                    span=t.span)

    def need(self, op, got, t=None):
        raise ParseError("%s expects %s, got %d" % (op.shown, describe(op), got), t)

    def op_args(self, op, ot=None):
        args, kinds = [], []
        for i, k in enumerate(op.args):
            if isinstance(k, O.OPT_KW):
                t = self.peek()
                if t is not None and t.kind == "WORD" and t.text in k.names:
                    self.pos += 1
                    args.append(Node("kw", t.text, self.line_no, name=t.text))
                    kinds.append("KW")
            elif isinstance(k, O.REST):
                rk = k.kind
                count = 0
                while not self.at_end():
                    if isinstance(rk, O.KW):
                        t = self.next()
                        if t.kind != "WORD" or t.text not in rk.names:
                            raise ParseError("%s: expected one of %s, got %s"
                                             % (op.shown, " ".join(show(x) for x in rk.names),
                                                tokshow(t)), t)
                        args.append(Node("kw", t.text, self.line_no, name=t.text))
                        kinds.append("KW")
                    else:
                        args.append(self.parse_term())
                        kinds.append(rk)
                    count += 1
                if count == 0:
                    self.need(op, 0, ot)
            elif isinstance(k, O.KW):
                t = self.peek()
                if self.at_end() or t.kind != "WORD" or t.text not in k.names:
                    self.need(op, len(args), ot)
                self.pos += 1
                args.append(Node("kw", t.text, self.line_no, name=t.text))
                kinds.append("KW")
            elif k == O.TRACKS:
                t = self.peek()
                if t is not None and t.kind == "WORD" and t.text in ("CUR", "ALL") and self.at_end(1):
                    self.pos += 1
                    args.append(Node("kw", t.text, self.line_no, name=t.text))
                    kinds.append("KW")
                else:
                    count = 0
                    while not self.at_end():
                        args.append(self.parse_term())
                        kinds.append(O.TRACK)
                        count += 1
                    if count == 0:
                        raise ParseError("%s expects 1 to 8 tracks, or cur, or all" % op.shown, ot)
                    if count > 8:
                        raise ParseError("%s takes at most 8 tracks (got %d)" % (op.shown, count), ot)
            elif k == O.STEPSEL:
                t = self.peek()
                if t is not None and t.kind == "WORD" and t.text == "ALL" and self.at_end(1):
                    self.pos += 1
                    args.append(Node("kw", t.text, self.line_no, name=t.text))
                    kinds.append("KW")
                elif (t is not None and t.kind == "WORD" and t.text in O.OPS
                      and O.OPS[t.text].predicate and self.at_end(1)):
                    self.pos += 1
                    args.append(Node("pred", t.text, self.line_no, name=t.text, op=O.OPS[t.text]))
                    kinds.append("KW")
                else:
                    for j in range(2):
                        if self.at_end():
                            raise ParseError("%s expects all, a range (two steps) or a "
                                             "predicate name such as has.trig" % op.shown, ot)
                        args.append(self.parse_term())
                        kinds.append(O.STEP)
            elif isinstance(k, O.OPT_INT):
                if not self.at_end():
                    args.append(self.parse_term())
                    kinds.append(O.INT)
                else:
                    args.append(Node("num", str(k.default), self.line_no, val=k.default))
                    kinds.append(O.INT)
            elif k == O.NAME:
                if self.at_end():
                    self.need(op, len(args), ot)
                t = self.next()
                if t.kind != "WORD":
                    raise ParseError("%s: expected a function name, got %s" % (op.shown, tokshow(t)), t)
                prob = name_problem(t.text)
                if prob:
                    raise ParseError("%s: %s" % (op.shown, prob), t)
                if len(t.text) > 8:
                    raise ParseError("%s: name is too long (%d > 8 characters)"
                                     % (op.shown, len(t.text)), t)
                args.append(Node("str", '"%s"' % t.text, self.line_no, val=t.text))
                kinds.append("STR")
            elif k == O.COND:
                if self.at_end():
                    self.need(op, len(args), ot)
                t = self.peek()
                if t is not None and t.kind == "WORD" and t.text == "FILL":
                    self.pos += 1
                    args.append(Node("cond", t.text, self.line_no, val="FILL"))
                else:
                    args.append(self.parse_term())
                kinds.append(k)
            else:
                if self.at_end():
                    self.need(op, len(args), ot)
                args.append(self.parse_term())
                kinds.append(k)
        return args, kinds

    def user_call(self, name, t=None):
        g = self.prog.fmap[name]
        k = len(g.args)
        nxt = self.peek()
        has_bar = nxt is not None and nxt.kind == "SYM" and nxt.text == "|"
        if k == 0:
            if has_bar:
                raise ParseError("%s takes no argument: write %s without '|'"
                                 % (show(name), show(name)), nxt)
            return Node("call", name, self.line_no, name=name, span=t.span if t else None)
        if not has_bar:
            raise ParseError("%s takes %d argument%s: write %s | %s"
                             % (show(name), k, "" if k == 1 else "s", show(name),
                                show(" ".join(g.args))), t)
        self.pos += 1
        args = []
        for i in range(k):
            if self.at_end():
                raise ParseError("%s expects %d argument%s, got %d"
                                 % (show(name), k, "" if k == 1 else "s", i), t)
            args.append(self.parse_term())
        return Node("call", name, self.line_no, name=name, args=args, kinds=["USER"] * k,
                    span=t.span if t else None)

    # --- statements
    def parse_statement(self):
        t = self.peek()
        try:
            n = self.statement(t)
        except ParseError as e:
            if e.tok is None:
                e.tok = t
            raise
        if n.span is None:
            n.span = t.span
        return n

    def statement(self, t):
        ln = self.line_no
        if t.kind in ("NUM", "STR", "LIST", "COND"):
            raise ParseError("%s does nothing (a value with no destination)" % tokshow(t))
        if t.kind == "SYM":
            raise ParseError("unexpected '%s'" % t.text)
        w = t.text
        if w == "BREAK":
            self.pos += 1
            return Node("break", w, ln)
        if w == "STOP":
            self.pos += 1
            return Node("stop", w, ln)
        if w == "RET":
            self.pos += 1
            s = Node("ret", w, ln)
            if not self.at_end():
                s.args = [self.parse_term()]
            return s
        if w in DECLS:
            raise ParseError("%s is a declaration: it goes at the top of the function body, "
                             "before the first statement, without modifier" % show(w))
        if w in ("DEF", "CONST", "OTYPE"):
            raise ParseError("%s must start at column 1" % show(w))
        if retired(w):
            raise ParseError(retired(w), t)
        if w in MODIFIERS:
            raise ParseError("unexpected %s" % show(w))
        if w in VARS:
            if w not in ASSIGNABLE:
                raise ParseError("%s is read-only (loop counter)" % show(w))
            self.pos += 1
            if self.at_end():
                raise ParseError("%s alone does nothing (write %s <value> to assign)"
                                 % (show(w), show(w)))
            e = self.parse_term()
            return Node("assign", w, ln, name=w, args=[e])
        fn = self.fn
        if w in fn.vars:
            vinfo = fn.vars[w]
            cur_scope = getattr(self, "current_in_block", None)
            if vinfo.scope != "FUNC" and vinfo.scope is not cur_scope:
                raise ParseError("var %s is not visible in this block" % show(w))
            self.pos += 1
            if self.at_end():
                raise ParseError("%s alone does nothing (write %s <value> to assign)"
                                 % (show(w), show(w)))
            e = self.parse_term()
            return Node("assign_var_named", w, ln, name=w, var_info=vinfo, args=[e])
        if w in fn.args:
            raise ParseError("%s is an argument (read-only)" % show(w) if len(self.toks) > self.pos + 1
                             else "%s alone does nothing" % show(w))
        if w in fn.params:
            raise ParseError("%s is a param (read-only)" % show(w) if len(self.toks) > self.pos + 1
                             else "%s alone does nothing" % show(w))
        if w in self.prog.consts:
            raise ParseError("const %s is read-only; a constant alone does nothing" % show(w))
        if w in O.OPS:
            op = O.OPS[w]
            self.pos += 1
            if op.family == O.LANG:
                raise ParseError("%s does nothing: it computes a value that nobody uses "
                                 "(assign it, e.g. a %s ...)" % (show(w), show(w)))
            if op.settable and not self.at_end():
                # getter/setter: "NAME value" or "NAME arg... value" at the start of a statement sets it
                args = []
                for _ in range(len(op.args)):
                    if self.at_end():
                        self.need(op, len(args), t)
                    args.append(self.parse_term())
                if self.at_end():
                    raise ParseError("%s expects a value to set" % op.shown, t)
                args.append(self.parse_term())
                return Node("setstmt", w, ln, op=op, args=args)
            node = self.op_call(op, t)
            kind = "ctxstmt" if op.family == O.CTX else "opstmt"
            return Node(kind, w, ln, node=node)
        if w in self.prog.fmap:
            self.pos += 1
            return Node("callstmt", w, ln, node=self.user_call(w, t))
        if w in KEYWORDS:
            raise ParseError("unexpected keyword %s" % show(w))
        raise ParseError("unknown name %s" % show(w))

    def leftover(self, stmt):
        t = self.peek()
        if t.kind == "SYM":
            if t.text == ":":
                raise ParseError("unexpected ':' (not a modifier: %s)" % show(stmt.text), t)
            raise ParseError("unexpected '%s'" % t.text, t)
        if stmt.kind in ("opstmt", "callstmt") and stmt.node.kind in ("op", "call"):
            nd = stmt.node
            extra = 0
            while not self.at_end():
                extra += 1
                try:
                    self.parse_term()
                except ParseError:
                    break
            if nd.kind == "op":
                raise ParseError("%s expects %s, got %d" % (nd.op.shown, describe(nd.op),
                                                           len(nd.args) + extra), nd)
            k = len(nd.args)
            raise ParseError("%s expects %d argument%s, got %d"
                             % (show(nd.name), k, "" if k == 1 else "s", k + extra), nd)
        raise ParseError("unexpected %s after %s" % (tokshow(t), show(stmt.text)), t)

    def parse_modifier(self):
        t = self.next()
        w, ln = t.text, self.line_no
        if w == "DEL":
            raise ParseError("del is not available yet")
        args = []
        if w in ("IF", "ELIF", "PROB", "EVERY", "SKIP"):
            if self.at_end():
                raise ParseError("%s expects a value before ':'" % show(w))
            args.append(self.parse_term())
        elif w == "L":
            for _ in range(2):
                if self.at_end():
                    raise ParseError("l expects two bounds: l first last:")
                args.append(self.parse_term())
        self.expect_colon(w)
        return Mod(w, ln, args)

    def expect_colon(self, w):
        t = self.peek()
        if t is None or not (t.kind == "SYM" and t.text == ":"):
            raise ParseError("expected ':' after %s" % show(w))
        self.pos += 1

    # --- declarations
    def parse_decl(self, ln_info):
        t = self.next()
        w = t.text
        fn = self.fn
        line = self.line_no
        decl = Node("decl", w, line)
        if w == "MENU":
            strs = []
            while True:
                s = self.next()
                if s.kind != "STR":
                    raise ParseError('menu expects strings: menu "LEVEL" >> "ACTION"')
                strs.append(s.val)
                if self.peek() is None:
                    break
                sep = self.next()
                if not (sep.kind == "SYM" and sep.text == ">>"):
                    raise ParseError("menu levels are separated by >>")
            if len(strs) < 2:
                raise ParseError('menu needs at least one level and the action: menu "LEVEL" >> "ACTION"')
            cat = strs[0]
            if len(cat) > 11:
                fn.warn(line, 'menu category "%s" longer than 11 chars (put under "SCRIPTS")' % cat)
            elif len(cat) > 9:
                fn.warn(line, 'menu category "%s" longer than 9 chars (clipped on screen)' % cat)
            action = strs[-1]
            if len(action) > 13:
                fn.warn(line, 'script name "%s" longer than 13 chars (clipped on screen)' % action)
            if len(strs) > 2:
                fn.warn(line, "submenus not supported by the menu yet")
            fn.menus.append((strs[:-1], strs[-1], line))
        elif w == "PAGE":
            s = self.next()
            if s.kind != "STR":
                raise ParseError('page expects a string: page "LABEL"')
            if fn.page is not None:
                raise ParseError("duplicate page declaration (a function has at most one)")
            lab = s.val        # (the lexer already refuses any character outside 0x20..0x7e: no charset check here)
            if not lab:
                fn.err(line, "page label is empty")
            elif len(lab) > PAGE_LABEL_MAX:
                fn.err(line, 'page label "%s" is longer than %d characters' % (lab, PAGE_LABEL_MAX))
            fn.page = (lab, line)
        elif w == "MODULE":
            raise ParseError("module is gone: use in clock n")
        elif w == "CLOCK":
            raise ParseError("clock declaration is gone: use in clock n")
        elif w == "VAR":
            if self.peek() is None:
                raise ParseError("var expects a name and initial value: var name init")
            nm = self.next()
            prob = name_problem(nm.text, nm.kind)
            if prob:
                raise ParseError("var %s: %s" % (show(nm.text), prob))
            if nm.text in fn.args:
                raise ParseError("var %s has the name of an argument" % show(nm.text))
            if nm.text in self.prog.consts or nm.text in self.prog.fmap:
                raise ParseError("var %s has the name of a const or a function" % show(nm.text))
            if nm.text in fn.params:
                raise ParseError("var %s has the name of a param" % show(nm.text))
            if nm.text in fn.vars:
                raise ParseError("duplicate var declaration %s" % show(nm.text))
            if len(fn.vars) >= 16:
                raise ParseError("too many vars in function %s (at most 16)" % show(fn.name))
            if self.peek() is None:
                raise ParseError("var %s: initial value is missing" % show(nm.text))
            v = self.next()
            if v.kind == "NUM":
                init_val = v.val
            elif v.kind == "WORD" and v.text in self.prog.consts and \
                    not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                init_val = self.prog.consts[v.text].val
            else:
                raise ParseError("var %s: initial value must be a number or an integer const" % show(nm.text))
            if not -32768 <= init_val <= 32767:
                raise ParseError("var %s: initial value must fit in 16 bits (-32768..32767)" % show(nm.text))
            slot = len(fn.vars)
            scope = getattr(self, "current_in_block", None) or "FUNC"
            vinfo = VarInfo(nm.text, init_val, scope, slot, line)
            fn.vars[nm.text] = vinfo
            if scope != "FUNC":
                scope.vars[nm.text] = vinfo
            decl.val = nm.text

        elif w == "ON":
            if self.peek() is None:
                raise ParseError("on expects a host name (on trig, on loop 4 ...)")
            h = self.next()
            if h.kind not in ("WORD", "COND"):
                raise ParseError("on expects a host name (on trig, on loop 4 ...)")
            self.pos = len(self.toks)
            if h.text in FUTURE_HOSTS:
                fn.warn(line, "host not available yet: on %s" % show(h.text))
            else:
                fn.warn(line, "unknown host: on %s" % show(h.text))
        elif w == "PARAM":
            nm = self.next()
            prob = name_problem(nm.text, nm.kind)
            if prob:
                raise ParseError("param %s: %s" % (show(nm.text), prob))
            if nm.text in fn.args:
                raise ParseError("param %s has the name of an argument" % show(nm.text))
            if nm.text in self.prog.consts or nm.text in self.prog.fmap:
                raise ParseError("param %s has the name of a const or a function" % show(nm.text))
            if nm.text in fn.params:
                raise ParseError("duplicate param %s" % show(nm.text))
            if len(fn.params) >= MAX_PARAMS:
                raise ParseError("more than %d param in one function" % MAX_PARAMS)
            if len(nm.text) > 11:
                fn.warn(line, 'param "%s" label longer than 11 chars (clipped on screen)' % nm.text)
            nxt = self.peek()
            if nxt is None:
                raise ParseError("param name default min max: default is missing")
            ptype = "INT"
            if nxt.kind == "WORD" and nxt.text in ("INT", "BOOL", "OPTION", "FOLDER"):
                ptype = nxt.text
                self.next()

            if ptype == "BOOL":
                if self.peek() is None:
                    raise ParseError("param %s: bool default is missing" % show(nm.text))
                v = self.next()
                if v.kind == "NUM":
                    if v.val not in (0, 1):
                        raise ParseError("param %s: bool default must be 0 or 1 (got %d)" % (show(nm.text), v.val))
                    d = v.val
                elif v.kind == "WORD" and v.text in ("YES", "TRUE"):
                    d = 1
                elif v.kind == "WORD" and v.text in ("NO", "FALSE"):
                    d = 0
                elif v.kind == "WORD" and v.text in self.prog.consts and \
                        not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                    val = self.prog.consts[v.text].val
                    if val not in (0, 1):
                        raise ParseError("param %s: bool default must be 0 or 1 (got %d)" % (show(nm.text), val))
                    d = val
                else:
                    raise ParseError("param %s: bool default must be 0, 1, yes or no" % show(nm.text))
                fn.params[nm.text] = Param(d, 0, 1, kind="BOOL")
                decl.val = nm.text
            elif ptype == "OPTION":
                if self.peek() is None:
                    raise ParseError("param %s: option default is missing" % show(nm.text))
                v = self.next()
                if v.kind == "NUM":
                    d = v.val
                elif v.kind == "WORD" and v.text in self.prog.consts and \
                        not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                    d = self.prog.consts[v.text].val
                else:
                    raise ParseError("param %s: option default index must be a number or integer const" % show(nm.text))
                labels = []
                while self.peek() is not None:
                    t = self.next()
                    if t.kind != "STR":
                        raise ParseError("param %s: option labels must be strings" % show(nm.text))
                    if len(t.val) > 12:
                        raise ParseError('option param %s: label "%s" is longer than 12 characters' % (show(nm.text), t.val))
                    labels.append(t.val)
                if len(labels) < 2:
                    raise ParseError("option param %s needs at least 2 choices" % show(nm.text))
                if len(labels) > 16:
                    raise ParseError("option param %s has more than 16 choices" % show(nm.text))
                if not 0 <= d < len(labels):
                    raise ParseError("param %s: need min <= default <= max (got %d 0 %d)" % (show(nm.text), d, len(labels) - 1))
                fn.params[nm.text] = Param(d, 0, len(labels) - 1, kind="OPTION", labels=labels)
                decl.val = nm.text
            elif ptype == "FOLDER":
                if self.peek() is None:
                    raise ParseError("param %s: folder default path is missing" % show(nm.text))
                v = self.next()
                if v.kind != "STR":
                    raise ParseError('param %s: folder default must be a string path' % show(nm.text))
                path = v.val
                if len(path) > 64:
                    raise ParseError("folder param %s: path is longer than 64 characters" % show(nm.text))
                if sum(1 for p in fn.params.values() if getattr(p, "kind", "INT") == "FOLDER") >= 4:
                    raise ParseError("more than 4 folder params in one function")
                fn.params[nm.text] = Param(0, 0, 0, kind="FOLDER", path=path)
                decl.val = nm.text
            else: # INT
                vals = []
                for what in ("default", "min", "max"):
                    if self.peek() is None:
                        raise ParseError("param name default min max: %s is missing" % what)
                    v = self.next()
                    if v.kind == "NUM":
                        vals.append(v.val)
                    elif v.kind == "WORD" and v.text in self.prog.consts and \
                            not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                        vals.append(self.prog.consts[v.text].val)
                    else:
                        raise ParseError("param %s: %s must be a number or an integer const"
                                         % (show(nm.text), what))
                d, lo, hi = vals
                if not lo <= d <= hi:
                    raise ParseError("param %s: need min <= default <= max (got %d %d %d)"
                                     % (show(nm.text), d, lo, hi))
                src = None
                if self.peek() is not None and self.peek().kind == "WORD" and self.peek().text in ("CC", "NOTE", "VEL"):
                    stok = self.next()
                    skind = stok.text
                    if skind == "CC":
                        if self.peek() is None:
                            raise ParseError("param %s cc: channel and cc number required" % show(nm.text))
                        ctok = self.next()
                        if ctok.kind == "NUM":
                            ch = ctok.val
                        elif ctok.kind == "WORD" and ctok.text in fn.params:
                            raise ParseError("param %s cc: channel cannot be a param (dynamic param not supported)" % show(nm.text))
                        elif ctok.kind == "WORD" and ctok.text in self.prog.consts and \
                                not self.prog.consts[ctok.text].bad and self.prog.consts[ctok.text].type == "INT":
                            ch = self.prog.consts[ctok.text].val
                        else:
                            raise ParseError("param %s cc: channel must be 1..16" % show(nm.text))
                        if isinstance(ch, int) and not 1 <= ch <= 16:
                            raise ParseError("param %s cc: channel must be 1..16 (got %d)" % (show(nm.text), ch))

                        if self.peek() is None:
                            raise ParseError("param %s cc: cc number is missing" % show(nm.text))
                        ntok = self.next()
                        if ntok.kind == "NUM":
                            num = ntok.val
                        elif ntok.kind == "WORD" and ntok.text in fn.params:
                            raise ParseError("param %s cc: cc number cannot be a param (dynamic param not supported)" % show(nm.text))
                        elif ntok.kind == "WORD" and ntok.text in self.prog.consts and \
                                not self.prog.consts[ntok.text].bad and self.prog.consts[ntok.text].type == "INT":
                            num = self.prog.consts[ntok.text].val
                        else:
                            raise ParseError("param %s cc: cc number must be 0..127" % show(nm.text))
                        if isinstance(num, int) and not 0 <= num <= 127:
                            raise ParseError("param %s cc: cc number must be 0..127 (got %d)" % (show(nm.text), num))
                        src = ("cc", ch, num)
                    elif skind in ("NOTE", "VEL"):
                        if self.peek() is None:
                            raise ParseError("param %s %s: channel required" % (show(nm.text), show(skind)))
                        ctok = self.next()
                        if ctok.kind == "NUM":
                            ch = ctok.val
                        elif ctok.kind == "WORD" and ctok.text in fn.params:
                            raise ParseError("param %s %s: channel cannot be a param (dynamic param not supported)" % (show(nm.text), show(skind)))
                        elif ctok.kind == "WORD" and ctok.text in self.prog.consts and \
                                not self.prog.consts[ctok.text].bad and self.prog.consts[ctok.text].type == "INT":
                            ch = self.prog.consts[ctok.text].val
                        else:
                            raise ParseError("param %s %s: channel must be 1..16" % (show(nm.text), show(skind)))
                        if isinstance(ch, int) and not 1 <= ch <= 16:
                            raise ParseError("param %s %s: channel must be 1..16 (got %d)" % (show(nm.text), show(skind), ch))
                        src = ({"NOTE": "note", "VEL": "vel"}[skind], ch)

                fn.params[nm.text] = Param(d, lo, hi, kind="INT", src=src)
                decl.val = nm.text

        elif w == "CONFIRM":
            if self.peek() is not None:
                s = self.next()
                if s.kind != "STR":
                    raise ParseError('confirm takes an optional string: confirm "TEXT"')
                if s.val and fn.confirm_text is None:     # the first non-empty text is the box's question (spec 14.1)
                    fn.confirm_text = (s.val, line)
                if s.val and len(s.val) > 28:
                    fn.warn(line, 'confirm text "%s" longer than 28 chars (clipped in dialog)' % s.val)
            fn.confirm = True
            fn.confirm_line = line
        elif w == "LAUNCH":
            if fn.launch is not None:
                raise ParseError("duplicate launch declaration (a function has at most one)")
            if self.peek() is None:
                raise ParseError("launch expects a number of ticks: launch n")
            v = self.next()
            if v.kind == "NUM":
                ticks = v.val
            elif v.kind == "WORD" and v.text in self.prog.consts and \
                    not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                ticks = self.prog.consts[v.text].val
            else:
                raise ParseError("launch n: n must be a number or an integer const")
            if ticks < 1:
                raise ParseError("launch n: n must be >= 1 (got %d)" % ticks)
            if ticks > 32767:
                raise ParseError("launch n: n must be <= 32767 (got %d)" % ticks)
            fn.launch = (ticks, line)
        elif w == "INPUT":
            s = self.next()
            if s is None or s.kind != "WORD" or s.text != "SAMPLE":
                raise ParseError("input expects sample")
            if fn.input_sample:
                raise ParseError("duplicate input declaration")
            fn.input_sample = True
        if self.peek() is not None:
            raise ParseError("unexpected %s after %s declaration" % (tokshow(self.peek()), show(w)))
        return decl

    # --- lines
    def parse_line(self, li):
        ln = Line(li.no, li.indent)
        ln.opens = li.opens
        ln.first_word = li.first
        self.line_no = li.no
        if li.toks is None:
            ln.error = True
            ln.opens = False        # a line with an error never opens a block
            return ln
        self.toks, self.pos = li.toks, 0
        try:
            first = self.peek()
            if first.kind == "WORD" and first.text in DECLS:
                ln.decl = self.parse_decl(li)
                return ln
            if first.kind == "WORD" and first.text == "IN":
                ln.in_block = self.parse_in_block(li)
                ln.opens = True
                return ln
            while True:
                t = self.peek()
                if t.kind == "WORD" and t.text in MODIFIERS:
                    if ln.mods and t.text in ("ELIF", "ELSE"):
                        raise ParseError("%s without if (elif and else must start the line)"
                                         % show(t.text))
                    ln.mods.append(self.parse_modifier())
                    if self.peek() is None:
                        ln.opens = True
                        return ln
                    continue
                s = self.parse_statement()
                if s.kind == "ctxstmt" and self.peek() is not None and self.peek().text == ":" \
                        and self.peek().kind == "SYM":
                    self.pos += 1
                    ln.mods.append(Mod("CTX", li.no, node=s.node))
                    if self.peek() is None:
                        ln.opens = True
                        return ln
                    continue
                if self.peek() is not None:
                    self.leftover(s)
                ln.stmt = s
                ln.opens = False
                return ln
        except ParseError as e:
            tk = e.tok if e.tok is not None else self.last_tok()
            self.fn.err(li.no, str(e), span=tk.span if tk is not None else None)
            ln.error = True
            ln.opens = False
            ln.mods, ln.stmt, ln.decl, ln.in_block = [], None, None, None
            return ln

    def parse_in_block(self, li):
        self.next()
        if self.peek() is None:
            raise ParseError("in expects an input specifier (in clock N:, in note [ch]:, in cc ch n:, in trig t [all]:)")
        ktok = self.next()
        k = ktok.text
        line = li.no
        if k == "CLOCK":
            if self.peek() is None or self.peek().text == ":":
                raise ParseError("in clock expects a number of ticks: in clock N:")
            v = self.next()
            if v.kind == "NUM":
                ticks = v.val
            elif v.kind == "WORD" and v.text in self.prog.consts and \
                    not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                ticks = self.prog.consts[v.text].val
            else:
                raise ParseError("in clock N: N must be a number or an integer const")
            if ticks < 1:
                raise ParseError("in clock N: N must be >= 1 (got %d)" % ticks)
            block = InBlock("CLOCK", line, clock=ticks)
        elif k == "NOTE":
            ch = None
            if self.peek() is not None and self.peek().text != ":":
                v = self.next()
                if v.kind == "NUM":
                    ch = v.val
                elif v.kind == "WORD" and v.text in self.prog.consts and \
                        not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                    ch = self.prog.consts[v.text].val
                else:
                    raise ParseError("in note: channel must be 1..16 or an integer const")
                if not 1 <= ch <= 16:
                    raise ParseError("in note: channel must be 1..16 (got %d)" % ch)
            block = InBlock("NOTE", line, ch=ch)
        elif k == "CC":
            if self.peek() is None or self.peek().text == ":":
                raise ParseError("in cc expects channel and cc number: in cc ch n:")
            v = self.next()
            if v.kind == "NUM":
                ch = v.val
            elif v.kind == "WORD" and v.text in self.prog.consts and \
                    not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                ch = self.prog.consts[v.text].val
            else:
                raise ParseError("in cc: channel must be 1..16 or an integer const")
            if not 1 <= ch <= 16:
                raise ParseError("in cc: channel must be 1..16 (got %d)" % ch)
            if self.peek() is None or self.peek().text == ":":
                raise ParseError("in cc ch n: cc number is missing")
            v2 = self.next()
            if v2.kind == "NUM":
                num = v2.val
            elif v2.kind == "WORD" and v2.text in self.prog.consts and \
                    not self.prog.consts[v2.text].bad and self.prog.consts[v2.text].type == "INT":
                num = self.prog.consts[v2.text].val
            else:
                raise ParseError("in cc: cc number must be 0..127 or an integer const")
            if not 0 <= num <= 127:
                raise ParseError("in cc: cc number must be 0..127 (got %d)" % num)
            block = InBlock("CC", line, ch=ch, num=num)
        elif k == "TRIG":
            if self.peek() is None or self.peek().text == ":":
                raise ParseError("in trig expects a track: in trig t [all]:")
            v = self.next()
            if v.kind == "NUM":
                t = v.val
            elif v.kind == "WORD" and v.text in self.prog.consts and \
                    not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                t = self.prog.consts[v.text].val
            else:
                raise ParseError("in trig: track must be 1..8 or an integer const")
            if not 1 <= t <= 8:
                raise ParseError("in trig: track must be 1..8 (got %d)" % t)
            every = 0
            if self.peek() is not None and self.peek().kind == "WORD" and self.peek().text == "ALL":
                self.next()
                every = 1
            elif self.peek() is not None and self.peek().kind == "WORD" and self.peek().text == "SCRIPT":
                raise ParseError(IN_TRIG_SCRIPT_REFUSED)
            block = InBlock("TRIG", line, ch=t, num=every)
        elif k == "GATE":
            if self.peek() is None or self.peek().text == ":":
                raise ParseError("in gate expects a script name and an output: in gate NAME n:")
            nm = self.next()
            if nm.kind != "WORD":
                raise ParseError("in gate: expected a function name, got %s" % tokshow(nm))
            prob = name_problem(nm.text)
            if prob:
                raise ParseError("in gate: " + prob)
            if len(nm.text) > 8:
                raise ParseError("in gate: name is too long (%d > 8 characters)" % len(nm.text))
            v = self.next()
            if v.kind == "NUM":
                n = v.val
            elif v.kind == "WORD" and v.text in self.prog.consts and \
                    not self.prog.consts[v.text].bad and self.prog.consts[v.text].type == "INT":
                n = self.prog.consts[v.text].val
            else:
                raise ParseError("in gate: output must be 1..4 or an integer const")
            if not 1 <= n <= 4:
                raise ParseError("in gate: output must be 1..4 (got %d)" % n)
            block = InBlock("GATE", line, ch=n, name=nm.text)
        else:
            raise ParseError("unknown input kind %s: use in clock n, in note [ch], in cc ch n, in trig t [all]"
                             % show(k))

        if self.peek() is not None and self.peek().kind == "SYM" and self.peek().text == ":":
            self.next()
        if self.peek() is not None:
            raise ParseError("unexpected %s after in %s declaration" % (tokshow(self.peek()), show(k)))
        if not li.opens:
            raise ParseError("in %s: line must end with ':'" % show(k))

        # Check duplicate
        for b in self.fn.in_blocks:
            if b.key() == block.key():
                if block.kind == "CLOCK":
                    raise ParseError("duplicate in clock %d" % block.clock)
                elif block.kind == "NOTE":
                    if block.ch is None:
                        raise ParseError("duplicate in note")
                    else:
                        raise ParseError("duplicate in note %d" % block.ch)
                elif block.kind == "CC":
                    raise ParseError("duplicate in cc %d %d" % (block.ch, block.num))
                elif block.kind == "TRIG":
                    raise ParseError("duplicate in trig %d%s" % (block.ch, " all" if block.num else ""))
                elif block.kind == "GATE":
                    raise ParseError("duplicate in gate %s %d" % (show(block.name), block.ch))

        self.fn.in_blocks.append(block)
        if block.kind == "CLOCK" and self.fn.clock is None:
            self.fn.clock = (block.clock, block.line)
        self.fn.module = (self.fn.name, block.line)
        return block

    def parse(self):
        fn = self.fn
        root = []
        stack = [(None, root, None)]     # (indent, lines, in_block)
        pending = None
        self.current_in_block = None
        for li in fn.lines:
            for m in li.errs:
                fn.err(li.no, m)
            if li.blank:
                continue
            if pending is not None:
                if li.indent > pending.indent:
                    cur_in = pending.in_block if pending.in_block is not None else stack[-1][2]
                    stack.append((li.indent, pending.children, cur_in))
                    if len(stack) - 1 > MAX_NEST:
                        fn.err(li.no, "blocks nested deeper than %d" % MAX_NEST)
                else:
                    fn.err(pending.no, "empty block: a line ending with ':' needs more-indented "
                                       "lines below it")
                pending = None
            if stack[0][0] is None:
                stack[0] = (li.indent, root, None)
            # indentation against the enclosing blocks
            popped = False
            while li.indent < stack[-1][0] and len(stack) > 1:
                stack.pop()
                popped = True
            self.current_in_block = stack[-1][2]
            if li.indent > stack[-1][0] and popped:
                fn.err(li.no, "indentation does not match any enclosing block")
            elif li.indent > stack[-1][0]:
                fn.err(li.no, "unexpected indentation (no block was opened above)")
            elif li.indent < stack[-1][0]:
                fn.err(li.no, "indentation does not match the function body")
            line = self.parse_line(li)
            if line.in_block is not None and len(stack) > 1:
                fn.err(li.no, "in block must be at the root of the function")
            lines = stack[-1][1]
            prev = lines[-1] if lines else None
            fk = line.first_kind()
            if fk in ("ELIF", "ELSE") and not line.error:
                if prev is None or (not prev.error and prev.first_kind() not in ("IF", "ELIF")):
                    fn.err(li.no, "%s without if (it must directly follow a line that starts "
                                  "with if or elif, at the same indentation)" % show(fk))
            lines.append(line)
            if line.opens:
                pending = line
        if pending is not None:
            fn.err(pending.no, "empty block: a line ending with ':' needs more-indented lines "
                               "below it")
        fn.body = root
        # declarations before the first statement
        seen_stmt = False
        for ln in root:
            if ln.decl is not None or (ln.error and ln.first_word in DECLS):
                if seen_stmt:
                    fn.err(ln.no, "declaration %s after the first statement" % show(ln.first_word))
            elif ln.in_block is not None:
                seen_stmt = True
                seen_sub_stmt = False
                for sub in ln.children:
                    if sub.decl is not None:
                        if sub.decl.text != "VAR":
                            fn.err(sub.no, "declaration %s inside a block" % show(sub.first_word))
                        elif seen_sub_stmt:
                            fn.err(sub.no, "declaration %s after the first statement" % show(sub.first_word))
                    elif sub.stmt is not None or sub.mods:
                        seen_sub_stmt = True
            else:
                seen_stmt = True
                if fn.in_blocks:
                    fn.err(ln.no, "statement outside any in block (a live script organizes its code in 'in' blocks)")

        for ln in root:
            if ln.in_block is None:
                for sub in iter_lines(ln.children):
                    if sub.decl is not None:
                        fn.err(sub.no, "declaration %s inside a block" % show(sub.first_word))
        if fn.params and not fn.menus and fn.page is None and not fn.is_live:
            for ln in iter_lines(root):
                if ln.decl is not None and ln.decl.text == "PARAM":
                    fn.err(ln.no, "a brick cannot declare param; pass the value as an argument")
        if fn.page is not None:
            if fn.args:
                fn.err(fn.page[1], "a page function takes no arguments")
            if len(fn.params) > PAGE_MAX_PARAMS:
                fn.err(fn.page[1], "page function has more than %d params" % PAGE_MAX_PARAMS)
            for p in fn.params.values():
                if getattr(p, "kind", "INT") == "FOLDER":
                    fn.err(fn.page[1], "page function cannot declare a folder param")
                    break
        if fn.launch is not None and not fn.is_live:
            fn.err(fn.launch[1], "launch is only allowed in a live script")
        if fn.is_live:
            if len(fn.name) > MODULE_LABEL_MAX:
                fn.err(fn.line, "live script name %s is longer than %d characters" % (show(fn.name), MODULE_LABEL_MAX))
            if fn.page is not None:
                fn.err(fn.page[1], "a live script cannot declare a page")
            if fn.args:
                fn.err(fn.line, "a live script takes no arguments")
            if fn.confirm:
                cline = fn.confirm_line if fn.confirm_line is not None else fn.line
                fn.err(cline, "confirm not allowed in a live script")
        if not any(ln.decl is None and ln.in_block is None and ln.first_word not in DECLS for ln in iter_lines(root)):
            fn.err(fn.line, "no statement in the body")


# ---- API -------------------------------------------------------------------
def check_sources(sources):
    """sources: list of (label, text), in the order the card gives them.  Returns the checked Program.

    Caps: at most MAX_FILES files and MAX_SOURCE_BYTES characters (= bytes, text is Latin-1) in
    all.  The first file that would go over a cap, and every file after it, is not loaded: one file-level
    error each (`LABEL:0: -: error: file not loaded: over 64 files` / `... over 65536 bytes`, the cap
    that stopped the first one)."""
    prog = Program()
    nbytes, nfiles, stop = 0, 0, None
    for label, text in sources:
        if stop is None:
            if nfiles >= MAX_FILES:
                stop = "file not loaded: over %d files" % MAX_FILES
            elif nbytes + len(text) > MAX_SOURCE_BYTES:
                stop = "file not loaded: over %d bytes" % MAX_SOURCE_BYTES
        if stop is not None:
            prog.file_err(label, 0, stop)
            continue
        nfiles += 1
        nbytes += len(text)
        prog.load_text(label, text)
    prog.run()
    return prog


def read_text(path):
    with open(path, "rb") as fh:
        return fh.read().decode("latin-1")


def check_files(paths):
    return check_sources([(p, read_text(p)) for p in paths])


def menu_path(f):
    return "; ".join(" >> ".join(levels + [action]) for levels, action, _l in f.menus)


def report(prog, verbose, out=None):
    out = out or sys.stdout
    ds = prog.sorted_diags()
    for d in ds[:MAX_DIAGS]:
        print(d, file=out)
    if len(ds) > MAX_DIAGS:          # the report is cut after the MAX_DIAGS first lines (sorted); the summary counts everything
        print("-:0: -: error: too many errors (%d more)" % (len(ds) - MAX_DIAGS), file=out)
    if verbose:
        for f in prog.ok_funcs():
            flags = []
            if prog.destructive.get(f):
                flags.append("destructive (confirm)")
            elif f.confirm:
                flags.append("confirm")
            cost = prog.maxcost.get(f, 0)
            where = menu_path(f) if f.menus else "(brick, no menu)"
            if f.page is not None:
                where = (where + "; " if f.menus else "") + 'page "%s"' % f.page[0]
            if f.module is not None:
                where = (where + "; " if f.menus or f.page is not None else "") + 'module "%s" (clock %d)' % (f.module[0], f.clock[0])
            if f.launch is not None:
                where += "; launch %d" % f.launch[0]
            budget = "/%d" % MENU_BUDGET if f.menus else ("/%d" % MODULE_BUDGET if f.module is not None else "")
            print("  ok  %-16s %s  [%s]  max cost %d%s  (%s:%d)"
                  % (show(f.name), where, ", ".join(flags) or "-", cost, budget, f.file, f.line),
                  file=out)
    print(prog.summary(), file=out)


def op_sig(o):
    sig = o.signature()
    if o.settable:
        sig += "  /  %s x" % o.signature()
    return sig


def ops_by_category():
    groups = OrderedDict((c, []) for c in O.CATEGORIES)
    for o in O.ALL_OPS:
        groups[o.category].append(o)
    return groups


def ops_report(md=False, out=None):
    """The registry grouped by category (feeds the generated manual)."""
    out = out or sys.stdout
    for cat, ops in ops_by_category().items():
        if not ops:
            continue
        if md:
            print("### %s\n" % cat, file=out)
            print("| Operation | Alias | Description |", file=out)
            print("|---|---|---|", file=out)
            for o in ops:
                sig = "`%s`" % o.signature().replace("|", "\\|")
                if o.settable:
                    sig += " \u00b7 `%s x`" % o.signature()
                notes = []
                if o.destructive:
                    notes.append("destructive")
                if o.stmt_only:
                    notes.append("statement only")
                if o.menu_only:
                    notes.append("menu scripts only")
                doc = o.doc + (" (%s)" % ", ".join(notes) if notes else "")
                alias = " ".join("`%s`" % a for a in o.aliases)
                print("| %s | %s | %s |" % (sig, alias, doc), file=out)
            print(file=out)
        else:
            print(cat, file=out)
            for o in ops:
                alias = ("  (alias %s)" % " ".join(o.aliases)) if o.aliases else ""
                notes = []
                if o.destructive:
                    notes.append("destructive")
                if o.stmt_only:
                    notes.append("statement only")
                if o.menu_only:
                    notes.append("menu scripts only")
                note = ("  [%s]" % ", ".join(notes)) if notes else ""
                print("  %-30s %s%s%s" % (op_sig(o), o.doc, alias, note), file=out)
            print(file=out)


# ---- JSON output (editor integration) --------------------------------------
KEYWORD_DOCS = (
    # (name, signature, doc) -- lower case, the canonical written form
    ("otype", "otype 1", "language version, first line of a file (optional)"),
    ("def", "def name [| arg ...]", "define a function (a brick when it has no menu); body indented below"),
    ("const", "const name value", "file-level constant (number, string, list or condition), global to all files"),
    ("menu", 'menu "LEVEL" >> "ACTION"', "place of the function in the menu (levels up to 6 chars, action up to 10)"),
    ("page", 'page "LABEL"', "list the function on the SCRIPT page of the unit (label 1..8 characters; no argument, at most 6 params, no card/slot/pool op)"),
    ("var", "var name init", "persistent variable initialized to init (integer constant) on script load"),
    ("in", "in clock n: | in note [ch]: | in cc ch n: | in trig t [all]:",
     "event block for periodic clock, incoming MIDI note / CC events, or the sequencer's trigs"),
    ("launch", "launch n", "live script: quantize the first wake to the next multiple of n ticks of the grid (96 = a beat)"),
    ("on", "on host [args]", "run on another trigger (on trig, on loop 4); warning until the host exists"),
    ("param", 'param name [int] d min max | bool d | option d "a"... | folder "path"',
     "a setting editable on the unit: number in range, bool (yes/no), option (labels) or folder browser"),
    ("confirm", 'confirm ["TEXT"]', "ask YES/NO before running (implied by destructive operations)"),
    ("input", "input sample", "placement header: function acts on the host audio sample context"),
    ("if", "if c: ...", "run the line or block when c is true"),
    ("elif", "elif c: ...", "after an if/elif at the same indentation: another condition"),
    ("else", "else: ...", "after an if/elif at the same indentation: the remaining case"),
    ("l", "l a b: ...", "loop i from a to b inclusive; bounds are constant (never a variable)"),
    ("each", "each: ...", "for each step of the context, in order (i = the step)"),
    ("prob", "prob p: ...", "run with p percent probability"),
    ("every", "every n: ...", "run once every n passes over this line"),
    ("skip", "skip n: ...", "run every time except once every n passes"),
    ("del", "del n: ...", "reserved: run n steps later (not available yet)"),
    ("break", "break", "leave the innermost loop (l or each)"),
    ("stop", "stop", "leave the function"),
    ("ret", "ret [v]", "leave the function, giving the value v (the function then works as a value)"),
    ("all", "all", "keyword argument: every track or every step (trk all, steps all)"),
    ("cur", "cur", "keyword argument: the current track (trk cur)"),
    ("deep", "deep", "keyword argument of dir: include subfolders"),
    ("trk", "trk t... | cur | all", "context: act on these tracks (statement: to the end of the block; modifier: this line/block)"),
    ("steps", "steps all | a b | pred", "context: act on all steps, a range, or the steps matching a predicate"),
    ("step", "step s", "context: act on one step"),
    ("seed", "seed n", "context: reproducible randomness from here on"),
)
VARIABLE_DOCS = (
    ("a", "local to one run of the function, 0 at start; assignable"),
    ("b", "local to one run of the function, 0 at start; assignable"),
    ("c", "local to one run of the function, 0 at start; assignable"),
    ("d", "local to one run of the function, 0 at start; assignable"),
    ("x", "global, shared, kept from one press to the next, 0 at boot; assignable"),
    ("y", "global, shared, kept from one press to the next, 0 at boot; assignable"),
    ("z", "global, shared, kept from one press to the next, 0 at boot; assignable"),
    ("t", "global, shared, kept from one press to the next, 0 at boot; assignable"),
    ("i", "counter of the innermost loop (l, each); read-only, 0 outside loops"),
    ("j", "counter of the loop above the innermost one; read-only, 0 outside loops"),
)


def default_span(prog, file, line):
    """First non-space char to end of code (comment excluded), 0-based, end exclusive."""
    lines = prog.raw.get(file) or []
    raw = lines[line - 1] if 0 < line <= len(lines) else ""
    code, _c = split_comment(raw)
    code = code.rstrip(WS)
    if not code.strip(WS):
        code = raw.rstrip(WS)
    start = len(code) - len(code.lstrip(WS))
    return (start, max(len(code), start + 1))


def diag_json(prog, d):
    sp = d.span
    if sp is None or sp[1] <= sp[0]:
        sp = default_span(prog, d.file, d.line)
    return {"file": d.file, "line": d.line, "col": sp[0] + 1, "endCol": sp[1] + 1,
            "severity": d.level, "func": d.func, "message": d.msg}


def check_json(prog):
    """Plain dict for `check --json`.  col/endCol are 1-based, endCol is exclusive
    (the column just after the token)."""
    funcs = []
    for f in prog.funcs:
        menu = None
        if f.menus:
            levels, action, _l = f.menus[0]
            menu = list(levels) + [action]
        ret = any(ln.stmt is not None and ln.stmt.kind == "ret" and ln.stmt.args
                  for ln in iter_lines(f.body))
        cost = None if f.refused else prog.maxcost.get(f)
        funcs.append({
            "name": show(f.name), "file": f.file, "line": f.line,
            "args": [show(a) for a in f.args], "menu": menu,
            "page": f.page[0] if f.page is not None else None,
            "module": f.module[0] if f.module is not None else None,
            "clock": f.clock[0] if f.clock is not None else None,
            "launch": f.launch[0] if f.launch is not None else None,
            "params": [{"name": show(n), "default": v[0], "min": v[1], "max": v[2]}
                       for n, v in f.params.items()],
            "returns": ret, "destructive": bool(prog.destructive.get(f)),
            "cost": cost, "ok": not f.refused})
    consts = []
    for c in prog.const_list:
        val = None if c.bad and c.type is None else (list(c.val) if c.type == "LIST" else c.val)
        if c.type == "COND" and val is not None:
            val = show(val)
        consts.append({"name": show(c.name), "file": c.file, "line": c.line, "value": val,
                       "type": c.type})
    diags, seen = [], set()
    for d in prog.sorted_diags():
        j = diag_json(prog, d)
        key = (j["file"], j["line"], j["col"], j["severity"], j["message"])
        if key not in seen:       # a CONST line can report one lexer error twice
            seen.add(key)
            diags.append(j)
    return {"diagnostics": diags, "functions": funcs, "consts": consts}


def op_json(o):
    maxn, minn = 0, 0
    for k in o.args:
        if isinstance(k, O.OPT_KW):
            maxn += 1
        elif isinstance(k, O.REST):
            minn += 1
            maxn = None
        elif k == O.TRACKS:
            minn += 1
            maxn = None if maxn is None else maxn + 8
        elif k == O.STEPSEL:
            minn += 1
            maxn = None if maxn is None else maxn + 2
        else:
            minn += 1
            maxn = None if maxn is None else maxn + 1
    names = list(o.argnames) if o.argnames else list(O._arg_letters(o))
    return {"name": o.shown, "signature": o.signature(), "args": [O.kind_name(k) for k in o.args],
            "argnames": names, "ret": o.ret is not None, "doc": o.doc, "category": o.category,
            "alias": [show(a) for a in o.aliases], "settable": o.settable, "stmt_only": o.stmt_only,
            "family": o.family, "destructive": o.destructive, "menu_only": o.menu_only,
            "predicate": o.predicate,
            "minArgs": minn, "maxArgs": maxn}


def keyword_json(name, sig, doc, category, family):
    return {"name": name, "signature": sig, "args": [], "argnames": [], "ret": False, "doc": doc,
            "category": category, "alias": [], "settable": False, "stmt_only": False,
            "family": family, "destructive": False, "predicate": False,
            "minArgs": 0, "maxArgs": 0}


def ops_json():
    out = [op_json(o) for o in O.ALL_OPS]
    for name, sig, doc in KEYWORD_DOCS:
        out.append(keyword_json(name, sig, doc, "KEYWORD", "KEYWORD"))
    for name, doc in VARIABLE_DOCS:
        out.append(keyword_json(name, name, doc, "VARIABLE", "VARIABLE"))
    return out


USAGE = ("usage: otype.py check [-v] [--json] [--profile NAME] [--stdin-file PATH] FILE.OTY [FILE.OTY ...]\n"
         "       otype.py ops [--md | --json]\n"
         "       otype.py run FUNC FILE.OTY [FILE.OTY ...] [--seed n] [--bank FILE --pattern p] "
         "[--track t] [--len n]\n"
         "                [--card DIR] [--state FILE] [--param NAME=V ...] [--json] [--vm] [-v]   "
         "(run -h for details)\n"
         "       otype.py dump FILE.OTY [FILE.OTY ...] [--func NAME] [-o OUT.OTB] [--hex] [--profile NAME]   "
         "(the compiled table, spec 14)")


def main(argv):
    args = argv[1:]
    if args and args[0] == "run":
        from modules.otype import sim
        return sim.main(args[1:], checker=sys.modules[__name__])
    if args and args[0] == "dump":
        from modules.otype import otb
        return otb.main(args[1:], checker=sys.modules[__name__])
    if args and args[0] == "ops":
        if "--json" in args[1:]:
            json.dump(ops_json(), sys.stdout, ensure_ascii=True)
            print()
            return 0
        ops_report(md="--md" in args[1:])
        return 0
    verbose = False
    as_json = False
    stdin_file = None
    files = []
    cmd = None
    profile = None
    it = iter(args)
    for a in it:
        if a == "--profile":
            name = next(it, None)
            if name not in O.PROFILES:
                print("unknown profile %s (one of %s)" % (name, ", ".join(sorted(O.PROFILES))),
                      file=sys.stderr)
                return 2
            profile = O.PROFILES[name]
        elif a == "-v":
            verbose = True
        elif a == "--json":
            as_json = True
        elif a == "--stdin-file":
            stdin_file = next(it, None)
            if stdin_file is None:
                print(USAGE, file=sys.stderr)
                return 2
        elif cmd is None and a == "check":
            cmd = a
        else:
            files.append(a)
    if stdin_file is not None and stdin_file not in files:
        files.append(stdin_file)
    if cmd != "check" or not files:
        print(USAGE, file=sys.stderr)
        return 2
    sources = []
    want = os.path.abspath(stdin_file) if stdin_file is not None else None
    for p in files:
        if want is not None and os.path.abspath(p) == want:
            sources.append((p, sys.stdin.buffer.read().decode("latin-1")))
            want = None       # only the first spelling is the buffer
        else:
            sources.append((p, read_text(p)))
    prog = check_sources(sources)
    if profile is not None:       # same refusals as `dump --profile`, as error diagnostics
        from modules.otype import otb
        otb.apply_compile(prog, profile)
    if as_json:
        json.dump(check_json(prog), sys.stdout, ensure_ascii=True)
        print()
    else:
        report(prog, verbose)
    return 1 if prog.error_count() else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
