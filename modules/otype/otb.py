"""OType compiled table (OTB), format 1: compiler, decoder and disassembler.

Spec: docs/syntax.md section 14.  The firmware never parses text twice: at card load it
compiles /SCRIPTS/*.OTY into this table (in DRAM) and a small VM runs it.  This module is the
computer's version of that compiler: the same input must give the SAME BYTES (step 4 compares them
byte for byte in emulation), so every choice below is written in the spec and nothing depends on
dictionary order, hashes or the clock.

    compile_program(prog) -> Compiled      prog: a checked tools/otype.py Program
    Compiled.data                          the OTB bytes
    Compiled.diags                         compile errors (stack > 16 ...), checker format
    parse(data) -> Table                   decode the bytes (what the VM and the dump read)
    disassemble(table, sources, func)      the human listing of `otype.py dump`
    hexdump(data)

All multi-byte fields are BIG-ENDIAN (ColdFire).  16-bit fields are 2-aligned.
"""
import struct
from collections import OrderedDict

from modules.otype import ops as O

FORMAT = 1               # format version byte
LANG_VERSION = 1         # language version (`otype 1`)
MAGIC = b"OTB"
HEADER_LEN = 32
FENT = 54                # bytes per function entry
MAX_TABLE = 32768        # spec 10: total compiled table
MAX_STACK = 16           # spec 10: VM stack
MAX_FUNCS = 255          # a call carries the table index in one byte
MAX_CTX = 16             # the VM's caps at load (c/vm.c OTC_MAX_CTX / OTC_MAX_CTL / OTC_MAX_COUNTERS): context scopes,
MAX_CTL = 16             #   control frames per call, every/skip counters of the whole table; a table above them is
MAX_COUNTERS = 1024      #   refused by the VM, so the compiler never makes one
MAX_MENUS = 255          # `menu` lines of one function (one byte in the pool entry)
NAME_LEN = 16

# ---- constant pool entry types ------------------------------------------------------------
P_STR, P_LIST, P_STRS, P_MENU = 1, 2, 3, 4

# ---- masks stored in the function table ---------------------------------------------------
FAMILY_BITS = OrderedDict((("LANG", 0x01), ("CTX", 0x02), ("EDIT", 0x04), ("IO_R", 0x08),
                           ("IO_W", 0x10), ("UI", 0x20), ("STATE", 0x40),
                           ("PLAY", 0x80), ("ASK", 0x100)))      # PLAY, ASK: reserved
TOUCH_BITS = OrderedDict((("TRAC", 1), ("PART", 2), ("SLOTS", 4)))
HOST_BITS = OrderedDict((("MENU", 0x01), ("TRIG", 0x02), ("LOOP", 0x04), ("PAT", 0x08),
                         ("FILL", 0x10), ("LOAD", 0x20), ("MODULE", 0x40), ("PAGE", 0x80),
                         ("GATE", 0x100), ("CC", 0x200)))
F_DESTRUCTIVE, F_CONFIRM, F_CONFIRM_TEXT, F_TYPED_PARAMS, F_INPUT_SAMPLE, F_VARS, F_PARAM_SOURCES = 1, 2, 4, 8, 16, 32, 64
F_LAUNCH = 128      # `launch N` of a live script: 2 bytes (the ticks) end the aux data

ALWAYS_ALLOWED = ("LANG", "CTX", "STATE")      # families no host forbids (not shown by the dump)

VARS = "ABCDXYZTIJ"          # operand of ld / st: 0..9
KW_CUR, KW_ALL = 0, 255      # form operand of opv trk
STEPS_ALL, STEPS_PRED, STEPS_RANGE = 0, 1, 2   # form operand of opv steps

REG = {o.id: o for o in O.ALL_OPS}      # (namespace, number) -> Op


class OtbError(Exception):
    """A malformed table, or a table that cannot be built."""


class CompileError(Exception):
    def __init__(self, msg, line):
        Exception.__init__(self, msg)
        self.line = line


# ---- the instruction set (spec 14.3) -------------------------------------------------------
class Ins:
    def __init__(self, code, name, fmt, stack, doc):
        self.code, self.name, self.fmt, self.stack, self.doc = code, name, fmt, stack, doc
        self.size = 1 + struct.calcsize(">" + fmt)


INSTRS = [
    Ins(0x10, "push8", "b", "+1", "push the signed byte"),
    Ins(0x11, "push16", "h", "+1", "push the signed 16-bit value"),
    Ins(0x12, "pushk", "H", "+1", "push the pool offset of a string / list constant"),
    Ins(0x13, "pushc", "B", "+1", "push a trig-condition code 0..64"),
    Ins(0x14, "ld", "B", "+1", "push variable v (0..9 = a b c d x y z t i j)"),
    Ins(0x15, "lda", "B", "+1", "push argument n of the running function"),
    Ins(0x16, "ldp", "B", "+1", "push param n of the running function"),
    Ins(0x17, "st", "B", "-1", "pop into variable v (0..7 = a b c d x y z t)"),
    Ins(0x18, "drop", "", "-1", "pop and discard"),
    Ins(0x30, "op", "BB", "-n+r", "registry operation (ns, num): pops its n arguments, pushes r"),
    Ins(0x31, "opv", "BBB", "-f", "registry operation of variable shape (ns, num, form): trk, steps"),
    Ins(0x32, "set", "BB", "-1", "setter form of a state operation: pops the value, cost 1"),
    Ins(0x40, "jmp", "H", "0", "forward jump"),
    Ins(0x41, "jz", "H", "-1", "pop; forward jump if it is 0"),
    Ins(0x42, "sel", "H", "-1", "lazy ?: charge its cost, pop; forward jump if it is 0"),
    Ins(0x43, "prob", "H", "-1", "pop p, one draw of the prob stream; forward jump unless it is < p"),
    Ins(0x44, "every", "BH", "-1", "pop n, step counter k; forward jump unless it fires"),
    Ins(0x45, "skip", "BH", "-1", "pop n, step counter k; forward jump if it fires"),
    Ins(0x46, "jnev", "BBBH", "0", "jump forward unless current event matches (kind, ch, num)"),
    Ins(0x47, "jngt", "HBH", "0", "jump forward unless the current event is a gate on output n of the script whose name is the pool string"),
    Ins(0x50, "lbeg", "H", "-2", "begin l: pops b, a; the loop ends at the target"),
    Ins(0x51, "lnext", "H", "0", "end of an l iteration: next one (jump back) or leave the loop"),
    Ins(0x52, "ebeg", "H", "0", "begin each over the steps of the context; empty: jump to the end"),
    Ins(0x53, "enext", "H", "0", "end of an each iteration: next one (jump back) or leave"),
    Ins(0x54, "pass", "H", "0", "begin the passes over the pending track list; empty: jump to end"),
    Ins(0x55, "pnext", "H", "0", "end of a track pass: next track (jump back) or leave"),
    Ins(0x56, "break", "", "0", "leave the innermost l / each (unwinds its scopes and passes)"),
    Ins(0x60, "call", "B", "-n+1", "call function (table index): pops its n arguments, pushes its value"),
    Ins(0x61, "ret", "", "-1", "pop the value and leave the function"),
    Ins(0x62, "stop", "", "0", "leave the function, value 0"),
    Ins(0x70, "scope", "", "0", "enter a context scope (a copy of the current one)"),
    Ins(0x71, "unscope", "", "0", "leave the context scope"),
    Ins(0x80, "dir", "B", "-1", "dir: pops the folder string; operand 1 = deep"),
    Ins(0x81, "ext", "B", "-n", "ext: pops n extension keyword codes"),
    Ins(0x82, "match", "B", "-n", "match: pops n pattern strings"),
    Ins(0x83, "msg", "BB", "-n", "msg: pops n items (operand 2: bit i set = item i is a pool string, else a number)"),
]
INS = OrderedDict((i.code, i) for i in INSTRS)
BY_NAME = OrderedDict((i.name, i) for i in INSTRS)
BACKWARD = ("lnext", "enext", "pnext")          # the only instructions that jump backward
JUMPS = ("jmp", "jz", "sel", "prob", "every", "skip", "lbeg", "ebeg", "pass", "jnev", "jngt")   # forward


def fam_names(mask, all_=False):
    return [n for n, b in FAMILY_BITS.items() if mask & b and (all_ or n not in ALWAYS_ALLOWED)]


# ---- the constant pool -----------------------------------------------------------------------
class Pool:
    """Strings, lists, keyword lists, menu lists: each once, first use first, 2-aligned.

    Offsets are relative to the start of the pool, which begins with two reserved bytes so that
    no entry has offset 0 (0 = none)."""

    def __init__(self):
        self.buf = bytearray(2)
        self.index = {}

    def _put(self, key, body):
        if key in self.index:
            return min(self.index[key], 0xFFFF)
        off = len(self.buf)
        self.buf += body
        if len(self.buf) % 2:
            self.buf.append(0)
        self.index[key] = off
        # an offset over 16 bits means a table over 65535 bytes, which gets no bytes (only the error):
        # the clamp only keeps struct.pack from failing before that is known
        return min(off, 0xFFFF)

    @staticmethod
    def _s(s):
        b = s.encode("latin-1")
        if len(b) > 255:
            raise OtbError("string too long for the pool")
        return bytes([len(b)]) + b

    def string(self, s):
        return self._put((P_STR, s), bytes([P_STR]) + self._s(s))

    def list(self, vals):
        vals = tuple(vals)
        return self._put((P_LIST, vals), struct.pack(">BB%dh" % len(vals), P_LIST, len(vals), *vals))

    def strs(self, items):
        items = tuple(items)
        return self._put((P_STRS, items), bytes([P_STRS, len(items)]) + b"".join(self._s(s) for s in items))

    def menus(self, menus):
        key = (P_MENU, tuple(tuple(m) for m in menus))
        body = bytes([P_MENU, len(menus)])
        for m in menus:
            body += bytes([len(m)]) + b"".join(self._s(s) for s in m)
        return self._put(key, body)


class PoolView:
    """Read side of the pool (what the VM resolves a pushk value against)."""

    def __init__(self, data):
        self.data = data

    def _entry(self, off, kind):
        if not 2 <= off < len(self.data) or self.data[off] != kind:
            raise OtbError("bad pool reference %d (type %d expected)" % (off, kind))
        return self.data[off + 1], off + 2

    def _str_at(self, p):
        n = self.data[p]
        return self.data[p + 1:p + 1 + n].decode("latin-1"), p + 1 + n

    def string(self, off):
        n, p = self._entry(off, P_STR)
        return self.data[p:p + n].decode("latin-1")

    def list(self, off):
        n, p = self._entry(off, P_LIST)
        return list(struct.unpack_from(">%dh" % n, self.data, p))

    def strs(self, off):
        n, p = self._entry(off, P_STRS)
        out = []
        for _ in range(n):
            s, p = self._str_at(p)
            out.append(s)
        return out

    def menus(self, off):
        if off == 0:
            return []
        n, p = self._entry(off, P_MENU)
        out = []
        for _ in range(n):
            k = self.data[p]
            p += 1
            m = []
            for _ in range(k):
                s, p = self._str_at(p)
                m.append(s)
            out.append(m)
        return out

    def describe(self, off):
        """A readable form of whatever constant sits at `off` (for the dump)."""
        if not 2 <= off < len(self.data):
            return "?"
        k = self.data[off]
        if k == P_STR:
            return '"%s"' % self.string(off)
        if k == P_LIST:
            return "{%s}" % " ".join(str(v) for v in self.list(off))
        if k == P_STRS:
            return " ".join('"%s"' % s for s in self.strs(off))
        return "?"


# ---- the compiler (one function) -----------------------------------------------------------------
class Label:
    __slots__ = ("pos", "depth")

    def __init__(self):
        self.pos, self.depth = None, None


def is_trk_stmt(ln):
    """A `trk` statement line: the rest of its block runs once per track (spec 6.2)."""
    return (not ln.mods and ln.stmt is not None and ln.stmt.kind == "ctxstmt"
            and ln.stmt.node.op.name == "TRK")


def stmt_changes_ctx(stmt):
    """A statement-form context operation other than trk (trk makes passes, with scopes of
    their own): it modifies the context it runs in."""
    return stmt is not None and stmt.kind == "ctxstmt" and stmt.node.op.name != "TRK"


def mods_change_ctx(mods, stmt):
    return any(m.kind == "CTX" and m.node.op.name != "TRK" for m in mods) or stmt_changes_ctx(stmt)


def block_changes_ctx(lines):
    return any(ln.decl is None and not ln.error and not ln.mods and stmt_changes_ctx(ln.stmt)
               for ln in lines)


class FuncCompiler:
    """Emits the code of one function.  Everything the sim does with a tree, as a list of
    instructions; see spec 14.4 for the correspondence."""

    def __init__(self, prog, f, pool, used):
        self.prog, self.f, self.pool, self.used = prog, f, pool, used
        self.code = bytearray()
        self.fix = []             # (position of a u16 operand, Label)
        self.calls = []           # (position of the call operand, callee Func)
        self.lines = []           # (pc, source line)
        self.ctrs = []            # (line, modifier index)
        self.depth = self.maxdepth = 0
        self.maxdepth_line = f.line
        self.ctx = self.maxctx = 1          # the function's own context counts
        self.ctl = self.maxctl = 0
        self.maxctx_line = self.maxctl_line = f.line
        self.nlocals = 0
        self.dead = False
        self.cur = f.line
        self.args = list(f.args)
        self.params = list(f.params)

    # ---- low level ---------------------------------------------------------------------------
    @property
    def pos(self):
        return len(self.code)

    def stack(self, delta):
        self.depth += delta
        if self.depth > self.maxdepth:
            self.maxdepth, self.maxdepth_line = self.depth, self.cur

    def emit(self, name, *operands, stack=0):
        ins = BY_NAME[name]
        self.code += struct.pack(">B" + ins.fmt, ins.code, *operands)
        self.stack(stack)

    def jump(self, name, label, *front, stack=0):
        """A forward jump: `front` operands, then the u16 target (patched at the end)."""
        ins = BY_NAME[name]
        self.code += struct.pack(">B" + ins.fmt[:-1], ins.code, *front)
        self.fix.append((self.pos, label))
        self.code += b"\0\0"
        self.stack(stack)
        if label.depth is None:
            label.depth = self.depth
        elif label.depth != self.depth:
            raise OtbError("internal: stack depth differs at a jump (%d/%d)" % (label.depth, self.depth))
        if name == "jmp":
            self.dead = True

    def place(self, label):
        if self.dead:
            if label.depth is not None:
                self.depth = label.depth
            self.dead = False
        elif label.depth is not None and label.depth != self.depth:
            raise OtbError("internal: stack depth differs at a label (%d/%d)" % (label.depth, self.depth))
        label.depth = self.depth
        label.pos = self.pos

    def back(self, name, target, stack=0):
        """A backward jump (lnext / enext / pnext): the target is the start of the loop body."""
        self.emit(name, target & 0xFFFF, stack=stack)     # (a target over 16 bits: the code is too long anyway)

    def mark(self, line):
        self.cur = line
        if not self.lines or self.lines[-1][1] != line:
            if self.lines and self.lines[-1][0] == self.pos:
                self.lines[-1] = (self.pos, line)
            else:
                self.lines.append((self.pos, line))

    def scope_in(self):
        self.emit("scope")
        self.ctx += 1
        if self.ctx > self.maxctx:
            self.maxctx, self.maxctx_line = self.ctx, self.cur

    def scope_out(self):
        self.emit("unscope")
        self.ctx -= 1

    def frame_in(self):
        """A loop or pass: a control frame and the scope of an iteration."""
        self.ctl += 1
        if self.ctl > self.maxctl:
            self.maxctl, self.maxctl_line = self.ctl, self.cur
        self.ctx += 1
        if self.ctx > self.maxctx:
            self.maxctx, self.maxctx_line = self.ctx, self.cur

    def frame_out(self):
        self.ctl -= 1
        self.ctx -= 1

    def var(self, w):
        k = VARS.index(w)
        if k < 4:
            self.nlocals = max(self.nlocals, k + 1)
        return k

    # ---- constants and expressions -----------------------------------------------------------
    def push_int(self, v):
        if -128 <= v <= 127:
            self.emit("push8", v, stack=1)
        else:
            self.emit("push16", v, stack=1)

    def expr(self, n):
        k = n.kind
        if k == "num":
            self.push_int(n.val)
        elif k == "str":
            self.emit("pushk", self.pool.string(n.val), stack=1)
        elif k == "list":
            self.emit("pushk", self.pool.list(n.val), stack=1)
        elif k == "cond":
            self.emit("pushc", self.cond(n.val), stack=1)
        elif k == "var":
            self.emit("ld", self.var(n.name), stack=1)
        elif k == "var_named":
            self.emit("ld", 16 + n.var_info.slot, stack=1)
        elif k == "arg":
            self.emit("lda", self.args.index(n.name), stack=1)
        elif k == "param":
            self.emit("ldp", self.params.index(n.name), stack=1)
        elif k == "const":
            c = self.prog.consts[n.name]
            if c.type == "INT":
                self.push_int(c.val)
            elif c.type == "STR":
                self.emit("pushk", self.pool.string(c.val), stack=1)
            elif c.type == "LIST":
                self.emit("pushk", self.pool.list(c.val), stack=1)
            else:
                self.emit("pushc", self.cond(c.val), stack=1)
        elif k == "op":
            self.op_node(n)
        elif k == "call":
            self.call_node(n)
        else:
            raise OtbError("internal: expression kind %s" % k)

    @staticmethod
    def cond(text):
        from modules.otype import sim
        return sim.cond_code(text)

    def is_str(self, a):
        """The value an item of msg pushes is a pool string (a string, a string constant or an argument of string kind)."""
        return (a.kind == "str" or (a.kind == "const" and self.prog.consts[a.name].type == "STR")
                or (a.kind == "arg" and self.prog.argkind.get((self.f, a.name)) == "STR"))

    def call_node(self, n):
        g = self.prog.fmap[n.name]
        for a in n.args:
            self.expr(a)
        self.code += bytes([BY_NAME["call"].code])
        self.calls.append((self.pos, g))
        self.code += b"\0"
        self.stack(1 - len(n.args))

    def op_node(self, n):
        """An operation used as a value or as a statement.  Leaves its value on the stack when
        the registry says it has one."""
        op = n.op
        self.used.add(op.name)
        ns, num = op.id
        nm = op.name
        if op.lazy:                      # ? : only the chosen branch runs
            els, end = Label(), Label()
            self.expr(n.args[0])
            self.jump("sel", els, stack=-1)
            self.expr(n.args[1])
            self.jump("jmp", end)
            self.place(els)
            self.expr(n.args[2])
            self.place(end)
        elif nm == "DIR":
            self.expr(n.args[0])
            self.emit("dir", 1 if len(n.args) > 1 else 0, stack=-1)
        elif nm == "EXT":
            names = op.args[0].kind.names
            for a in n.args:
                self.emit("push8", names.index(a.name), stack=1)
            self.emit("ext", len(n.args), stack=-len(n.args))
        elif nm == "MATCH":
            for a in n.args:
                self.expr(a)
            self.emit("match", len(n.args), stack=-len(n.args))
        elif nm == "MSG":
            mask = 0
            for i, a in enumerate(n.args):
                self.expr(a)
                if self.is_str(a):
                    mask |= 1 << i
            self.emit("msg", len(n.args), mask, stack=-len(n.args))
        elif nm == "TRK":
            self.trk_args(n)
        elif nm == "STEPS":
            a0 = n.args[0]
            if a0.kind == "kw":
                self.emit("opv", ns, num, STEPS_ALL)
            elif a0.kind == "pred":
                self.used.add(a0.op.name)
                self.emit("push16", (a0.op.id[0] << 8) | a0.op.id[1], stack=1)
                self.emit("opv", ns, num, STEPS_PRED, stack=-1)
            else:
                for a in n.args:
                    self.expr(a)
                self.emit("opv", ns, num, STEPS_RANGE, stack=-2)
        else:
            for a in n.args:
                self.expr(a)
            self.emit("op", ns, num, stack=(1 if op.ret is not None else 0) - len(n.args))

    def trk_args(self, n):
        ns, num = n.op.id
        self.used.add("TRK")
        a0 = n.args[0]
        if a0.kind == "kw":
            self.emit("opv", ns, num, KW_CUR if a0.name == "CUR" else KW_ALL)
        else:
            for a in n.args:
                self.expr(a)
            self.emit("opv", ns, num, len(n.args), stack=-len(n.args))

    # ---- statements --------------------------------------------------------------------------
    def stmt(self, s):
        k = s.kind
        if k == "assign":
            self.expr(s.args[0])
            self.emit("st", self.var(s.name), stack=-1)
        elif k == "assign_var_named":
            self.expr(s.args[0])
            self.emit("st", 16 + s.var_info.slot, stack=-1)
        elif k == "opstmt":
            self.op_node(s.node)
            if s.node.op.ret is not None:
                self.emit("drop", stack=-1)
        elif k == "callstmt":
            self.call_node(s.node)
            self.emit("drop", stack=-1)
        elif k == "ctxstmt":
            self.ctx_stmt(s.node)
        elif k == "break":
            self.emit("break")
        elif k == "stop":
            self.emit("stop")
        elif k == "ret":
            if s.args:
                self.expr(s.args[0])
            else:
                self.push_int(0)
            self.emit("ret", stack=-1)
        elif k == "setstmt":
            self.used.add(s.op.name)
            for a in s.args:
                self.expr(a)
            self.emit("set", s.op.id[0], s.op.id[1], stack=-len(s.args))
        else:
            raise OtbError("internal: statement kind %s" % k)

    def ctx_stmt(self, node):
        """A context operation as a statement (it modifies the scope it runs in), except trk,
        which here (a line with modifiers, nothing after it) is a pass over an empty body."""
        if node.op.name == "TRK":
            self.passes(node, lambda: None)
        else:
            self.op_node(node)

    def passes(self, node, body):
        self.trk_args(node)
        end = Label()
        self.jump("pass", end)
        self.frame_in()
        start = self.pos
        body()
        self.back("pnext", start)
        self.frame_out()
        self.place(end)

    # ---- modifiers, lines, blocks ------------------------------------------------------------
    def mods(self, ln, idx):
        if idx >= len(ln.mods):
            if ln.stmt is not None:
                self.stmt(ln.stmt)
            else:
                self.children(ln)
            return
        m = ln.mods[idx]
        k = m.kind
        if k in ("IF", "ELIF", "PROB"):
            skip = Label()
            self.expr(m.args[0])
            self.jump("jz" if k != "PROB" else "prob", skip, stack=-1)
            self.mods(ln, idx + 1)
            self.place(skip)
        elif k in ("EVERY", "SKIP"):
            skip = Label()
            self.expr(m.args[0])
            self.ctrs.append((ln.no, idx))
            if len(self.ctrs) > 255:      # the counter index is one byte (checked here: it is packed below)
                raise CompileError("more than 255 every/skip counters", self.f.line)
            self.jump(k.lower(), skip, len(self.ctrs) - 1, stack=-1)
            self.mods(ln, idx + 1)
            self.place(skip)
        elif k == "L":
            end = Label()
            self.expr(m.args[0])
            self.expr(m.args[1])
            self.jump("lbeg", end, stack=-2)
            self.frame_in()
            start = self.pos
            self.mods(ln, idx + 1)
            self.mark(ln.no)
            self.back("lnext", start)
            self.frame_out()
            self.place(end)
        elif k == "EACH":
            end = Label()
            self.jump("ebeg", end)
            self.frame_in()
            start = self.pos
            self.mods(ln, idx + 1)
            self.mark(ln.no)
            self.back("enext", start)
            self.frame_out()
            self.place(end)
        elif k == "CTX":
            if m.node.op.name == "TRK":
                self.passes(m.node, lambda: self.mods(ln, idx + 1))
            else:
                self.op_node(m.node)
                self.mods(ln, idx + 1)
        else:
            raise OtbError("internal: modifier %s" % k)

    def children(self, ln):
        need = block_changes_ctx(ln.children)
        if need:
            self.scope_in()
        self.block(ln.children)
        if need:
            self.scope_out()
        self.mark(ln.no)

    def scoped(self, ln, idx):
        need = mods_change_ctx(ln.mods[idx:], ln.stmt)
        if need:
            self.scope_in()
        self.mods(ln, idx)
        if need:
            self.scope_out()

    def block(self, lines):
        i, n = 0, len(lines)
        while i < n:
            ln = lines[i]
            if ln.decl is not None or ln.error:
                i += 1
                continue
            if getattr(ln, "in_block", None) is not None:
                ib = ln.in_block
                end_lbl = Label()
                if ib.kind == "GATE":
                    self.jump("jngt", end_lbl, self.pool.string(ib.name), ib.ch)
                    self.block(ln.children)
                    self.place(end_lbl)
                    i += 1
                    continue
                kind_code = {"CLOCK": 1, "NOTE": 2, "CC": 3, "TRIG": 4}.get(ib.kind, 0)
                ch_code = ib.ch if ib.ch is not None else 0
                num_code = ib.num if ib.num is not None else 255
                self.jump("jnev", end_lbl, kind_code, ch_code, num_code)
                self.block(ln.children)
                self.place(end_lbl)
                i += 1
                continue
            fk = ln.first_kind()
            if fk == "IF":
                chain = [ln]
                j = i + 1
                while j < n and lines[j].first_kind() in ("ELIF", "ELSE"):
                    chain.append(lines[j])
                    j += 1
                    if chain[-1].first_kind() == "ELSE":
                        break
                self.chain(chain)
                i = j
                continue
            if fk in ("ELIF", "ELSE"):
                raise OtbError("internal: %s outside a chain" % fk)
            self.mark(ln.no)
            if is_trk_stmt(ln):
                self.passes(ln.stmt.node, lambda: self.block(lines[i + 1:]))
                return
            if ln.mods:
                self.scoped(ln, 0)
            else:
                self.mods(ln, 0)
            i += 1

    def chain(self, chain):
        end = Label()
        last = len(chain) - 1
        for k, c in enumerate(chain):
            self.mark(c.no)
            nxt = Label()
            if c.first_kind() != "ELSE":
                self.expr(c.mods[0].args[0])
                self.jump("jz", end if k == last else nxt, stack=-1)
            self.scoped(c, 1)
            if k != last:
                self.jump("jmp", end)
                self.place(nxt)
        self.place(end)

    # ---- the function ------------------------------------------------------------------------
    def compile(self):
        self.mark(self.f.line)
        self.block(self.f.body)
        self.emit("stop")
        if self.maxdepth > MAX_STACK:
            raise CompileError("stack depth %d > %d" % (self.maxdepth, MAX_STACK), self.maxdepth_line)
        if self.maxctl > MAX_CTL:            # what the VM would refuse at load
            raise CompileError("loop/pass depth %d > %d" % (self.maxctl, MAX_CTL), self.maxctl_line)
        if self.maxctx > MAX_CTX:
            raise CompileError("context depth %d > %d" % (self.maxctx, MAX_CTX), self.maxctx_line)
        if len(self.code) > 0xFFFF:
            raise CompileError("code too long (%d bytes)" % len(self.code), self.f.line)
        if len(self.ctrs) > 255:
            raise CompileError("more than 255 every/skip counters", self.f.line)
        if len(self.f.menus) > MAX_MENUS:    # the count is one byte of the pool entry
            raise CompileError("more than %d menu lines" % MAX_MENUS, self.f.line)
        for pos, label in self.fix:
            self.code[pos:pos + 2] = struct.pack(">H", label.pos)
        # a line entry at the very end of the code would map nothing: drop it
        self.lines = [e for e in self.lines if e[0] < len(self.code)]


# ---- the compiler (program) ----------------------------------------------------------------------
class OtbDiag:
    """A compile error in the checker's message format (FILE:LINE: FUNC: error: message)."""

    def __init__(self, file, line, func, msg, fobj=None):
        self.file, self.line, self.func, self.level, self.msg = file, line, O.show(func), "error", msg
        self.fobj = fobj          # the checker's Func (None: a table-level error)

    def __str__(self):
        return "%s:%d: %s: %s: %s" % (self.file, self.line, self.func, self.level, self.msg)


class Compiled:
    pass


def func_ops(f):
    """Every registry operation a function's own text uses (statements, modifiers, predicates)."""
    return [op for op, _line in func_ops_at(f)]


def func_ops_at(f):
    """func_ops with the source line of each use: [(op, line)]."""
    seen = []

    def walk(n):
        if n.kind in ("op", "pred", "setstmt") and n.op is not None:
            seen.append((n.op, n.line))
        for a in n.args:
            walk(a)
        if n.node is not None:
            walk(n.node)

    def lines(ls):
        for ln in ls:
            for m in ln.mods:
                for a in m.args:
                    walk(a)
                if m.node is not None:
                    walk(m.node)
            if ln.stmt is not None:
                walk(ln.stmt)
            lines(ln.children)

    lines(f.body)
    return seen


def static_masks(prog, f, memo):
    """Union over f and everything it calls (transitively): families, touches, namespaces."""
    if f in memo:
        return memo[f]
    memo[f] = (0, 0, 0)            # the call graph of an accepted function has no cycle
    fam = tch = ns = 0
    for op in func_ops(f):
        fam |= FAMILY_BITS[op.family]
        tch |= TOUCH_BITS.get(op.touches, 0)
        ns |= 1 << O.NAMESPACES[op.ns]
    for g in prog.cg.get(f, ()):
        a, b, c = static_masks(prog, g, memo)
        fam, tch, ns = fam | a, tch | b, ns | c
    memo[f] = (fam, tch, ns)
    return memo[f]


def compile_program(prog, profile=None):
    """The OTB of a checked program.  Functions the checker refused are absent, and so are
    functions the compiler refuses (stack depth > 16 ...) and those that call them: the header
    counts them all.  PROFILE (a set of op names, O.PROFILES) also refuses the functions that
    use an op outside it directly ("not on the unit yet: op"), and their callers."""
    order = [f for f in prog.funcs if f.registered and not f.refused]
    diags, bad = [], {}
    # 1. find what cannot be compiled (a scratch pool: nothing is kept)
    for f in order:
        out = [(ln, op) for op, ln in func_ops_at(f) if not O.in_profile(op, profile)]
        if out:
            ln, op = min(out, key=lambda t: t[0])
            bad[f] = (ln, "not on the unit yet: %s" % op.shown)
            continue
        try:
            FuncCompiler(prog, f, Pool(), set()).compile()
        except CompileError as e:
            bad[f] = (e.line, str(e))
    changed = True
    while changed:
        changed = False
        for f in order:
            if f in bad:
                continue
            for g, line in prog.callees(f).items():
                if g in bad:
                    bad[f] = (line, "needs %s" % O.show(g.name))
                    changed = True
                    break
    for f in order:
        if f in bad:
            line, msg = bad[f]
            diags.append(OtbDiag(f.file, line, f.name, msg, f))
    funcs = [f for f in order if f not in bad]
    if len(funcs) > MAX_FUNCS:
        raise OtbError("more than %d functions" % MAX_FUNCS)
    index = {f: i for i, f in enumerate(funcs)}
    nrefused = len(prog.funcs) - len(funcs)

    # 2. the real thing: pool, per-function data, code
    pool = Pool()
    used = set()
    memo = {}
    entries = []
    aux, lines_r, code_r = bytearray(), bytearray(), bytearray()
    for f in funcs:
        file_off = pool.string(f.file)
        menu_off = pool.menus([list(lv) + [act] for lv, act, _l in f.menus]) if f.menus else 0
        page_off = pool.string(f.page[0]) if f.page is not None else 0      # the page label follows the menu list
        module_off = pool.string(f.module[0]) if f.module is not None else 0
        conf_off = pool.string(f.confirm_text[0]) if f.confirm_text is not None else 0   # then the confirm text
        has_typed = any(getattr(v, "kind", "INT") != "INT" for v in f.params.values())
        pnames = []
        for nm, v in f.params.items():
            off = pool.string(nm)
            kind = getattr(v, "kind", "INT")
            if kind == "FOLDER":
                path_off = pool.string(v.path)
                pnames.append((off, path_off, 0, 0))
            else:
                pnames.append((off, v[0], v[1], v[2]))
        anames = [pool.string(a) for a in f.args]
        fc = FuncCompiler(prog, f, pool, used)
        fc.compile()
        for pos, g in fc.calls:
            fc.code[pos] = index[g]
        fam, tch, ns = static_masks(prog, f, memo)
        e = OrderedDict()
        e["name"] = f.name
        e["menu_off"] = menu_off
        e["hosts"] = ((HOST_BITS["MENU"] if f.menus else 0)
                      | (HOST_BITS["PAGE"] if f.page is not None else 0)
                      | (HOST_BITS["MODULE"] if f.module is not None else 0)
                      | (HOST_BITS["TRIG"] if any(b.kind == "TRIG" for b in getattr(f, "in_blocks", [])) else 0)
                      | (HOST_BITS["GATE"] if any(b.kind == "GATE" for b in getattr(f, "in_blocks", [])) else 0)
                      | (HOST_BITS["CC"] if any(b.kind == "CC" for b in getattr(f, "in_blocks", [])) else 0))
        e["families"], e["touches"] = fam, tch
        has_vars = bool(getattr(f, "vars", None))
        has_param_sources = any(getattr(v, "src", None) is not None for v in f.params.values())
        e["flags"] = ((F_DESTRUCTIVE if prog.destructive.get(f) else 0) | (F_CONFIRM if f.confirm else 0)
                      | (F_CONFIRM_TEXT if f.confirm_text is not None else 0)
                      | (F_TYPED_PARAMS if has_typed else 0)
                      | (F_INPUT_SAMPLE if getattr(f, "input_sample", False) else 0)
                      | (F_VARS if has_vars else 0)
                      | (F_PARAM_SOURCES if has_param_sources else 0)
                      | (F_LAUNCH if getattr(f, "launch", None) is not None else 0))
        note_chs = set()
        for b in getattr(f, "in_blocks", []):
            if b.kind == "NOTE":
                note_chs.add(b.ch if b.ch is not None else 255)
        if len(note_chs) == 1:
            note_ch = next(iter(note_chs))
        elif len(note_chs) > 1:
            note_ch = 255
        else:
            note_ch = 0
        e["note_ch"] = note_ch
        e["max_cost"], e["ns_mask"] = min(prog.maxcost.get(f, 0), 0xFFFFFFFF), ns   # saturated
        e["nargs"], e["nparams"], e["nlocals"] = len(f.args), len(f.params), fc.nlocals
        e["max_stack"], e["max_ctx"], e["max_ctl"], e["nctr"] = fc.maxdepth, fc.maxctx, fc.maxctl, len(fc.ctrs)
        e["file_off"], e["def_line"] = file_off, f.line
        e["code_off"], e["code_len"] = len(code_r), len(fc.code)
        e["aux_off"] = len(aux)
        for off, d, lo, hi in pnames:
            aux += struct.pack(">Hhhh", off, d, lo, hi)
        for off in anames:
            aux += struct.pack(">H", off)
        for line, midx in fc.ctrs:
            aux += struct.pack(">HBB", line, midx, 0)
        if f.page is not None:
            aux += struct.pack(">H", page_off)                  # the label's pool offset ends the aux data
        if f.module is not None:
            clk = f.clock[0] if f.clock is not None else 0
            aux += struct.pack(">HH", module_off, clk)   # pool offset du nom (2B) + clock ticks (2B)
        if f.confirm_text is not None:
            aux += struct.pack(">H", conf_off)                  # the confirm text's pool offset, after the page label
        if has_vars:
            aux += struct.pack(">H", len(f.vars))
            for vinfo in f.vars.values():
                aux += struct.pack(">h", vinfo.init)
        if has_typed:
            types_mask = 0
            for k, (nm, v) in enumerate(f.params.items()):
                kind = getattr(v, "kind", "INT")
                code = 0 if kind == "INT" else (1 if kind == "BOOL" else (2 if kind == "OPTION" else 3))
                types_mask |= (code << (2 * k))
            aux += struct.pack(">H", types_mask)
            for nm, v in f.params.items():
                if getattr(v, "kind", "INT") == "FOLDER":
                    aux += struct.pack(">H", pool.string(v.path))
            for nm, v in f.params.items():
                if getattr(v, "kind", "INT") == "OPTION":
                    aux += struct.pack(">H", len(v.labels))
                    for lbl in v.labels:
                        aux += struct.pack(">H", pool.string(lbl))
        if has_param_sources:
            for nm, v in f.params.items():
                src = getattr(v, "src", None)
                if src is None:
                    aux += struct.pack(">BBBB", 0, 0, 0, 0)
                elif src[0] == "cc":
                    aux += struct.pack(">BBBB", 1, src[1], src[2], 0)
                elif src[0] == "note":
                    aux += struct.pack(">BBBB", 2, src[1], 0, 0)
                elif src[0] == "vel":
                    aux += struct.pack(">BBBB", 3, src[1], 0, 0)
        if getattr(f, "launch", None) is not None:
            aux += struct.pack(">H", f.launch[0])                 # `launch N`: its ticks end the aux data
        e["line_off"], e["nlines"] = len(lines_r), len(fc.lines)
        for pc, line in fc.lines:
            lines_r += struct.pack(">HH", pc, line)
        code_r += fc.code
        entries.append(e)

    if sum(e["nctr"] for e in entries) > MAX_COUNTERS:     # the VM refuses such a table at load
        raise OtbError("more than %d every/skip counters in the table" % MAX_COUNTERS)
    total = HEADER_LEN + len(pool.buf) + FENT * len(entries) + len(aux) + len(lines_r) + len(code_r)
    if total > 0xFFFF:
        # the 16-bit offsets of the header and of the function entries cannot hold such a table:
        # no bytes, only the error (a table over MAX_TABLE is refused anyway)
        r = Compiled()
        r.data, r.diags, r.funcs, r.used_ops, r.nrefused = b"", diags, funcs, sorted(used), nrefused
        r.diags.append(OtbDiag("-", 0, "-", "compiled table is %d bytes (> %d)" % (total, MAX_TABLE)))
        return r
    ftab = bytearray()
    for e in entries:
        nm = e["name"].encode("latin-1")
        if len(nm) > NAME_LEN:
            raise OtbError("function name longer than %d" % NAME_LEN)
        ftab += nm.ljust(NAME_LEN, b"\0")
        ftab += struct.pack(">HHHBBIIBBBBBBBBHHHHHHH", e["menu_off"], e["hosts"], e["families"],
                            e["touches"], e["flags"], e["max_cost"], e["ns_mask"],
                            e["nargs"], e["nparams"], e["nlocals"], e["max_stack"],
                            e["max_ctx"], e["max_ctl"], e["nctr"], e.get("note_ch", 0),
                            e["file_off"], e["def_line"], e["code_off"], e["code_len"],
                            e["aux_off"], e["line_off"], e["nlines"])
    assert len(ftab) == FENT * len(entries), len(ftab)

    pool_off = HEADER_LEN
    ftab_off = pool_off + len(pool.buf)
    aux_off = ftab_off + len(ftab)
    line_off = aux_off + len(aux)
    code_off = line_off + len(lines_r)
    total = code_off + len(code_r)
    header = MAGIC + struct.pack(">BBBHHHHHHHHHHHH", FORMAT, LANG_VERSION, 0, len(funcs), nrefused,
                                 pool_off, len(pool.buf), ftab_off, aux_off, len(aux),
                                 line_off, len(lines_r), code_off, len(code_r), total)
    header += b"\0" * (HEADER_LEN - len(header))
    data = bytes(header + pool.buf + ftab + aux + lines_r + code_r)
    assert len(data) == total
    r = Compiled()
    r.data, r.diags, r.funcs, r.used_ops = data, diags, funcs, sorted(used)
    r.nrefused = nrefused
    if total > MAX_TABLE:
        r.diags.append(OtbDiag("-", 0, "-", "compiled table is %d bytes (> %d)" % (total, MAX_TABLE)))
    return r


def apply_compile(prog, profile=None):
    """Run the compiler on a checked program and fold ITS errors into the program's diagnostics, so
    that the checker's report, `check --profile` and the unit's ERRORS.TXT are one text (spec
    14.6): a function the compiler refuses (`stack depth 17 > 16`, `not on the unit yet: op`, `needs x`)
    gets an ordinary error on its function (it is then counted as refused in the summary); a table-level
    error (`compiled table is N bytes (> 32768)`, `more than 255 functions`) is a file-level error of the
    pseudo-file `-` at line 0.  Returns (data, error): `data` the OTB bytes, or None when a table-level
    error means that there is no table."""
    try:
        c = compile_program(prog, profile)
    except OtbError as e:
        prog.file_err("-", 0, str(e))
        return None
    table_err = False
    for d in c.diags:
        if d.fobj is not None:
            d.fobj.err(d.line, d.msg)
        else:
            prog.file_err(d.file, d.line, d.msg)
            table_err = True
    return None if table_err else c.data


# ---- the decoder ---------------------------------------------------------------------------------
class FuncInfo:
    """One function of a parsed table.  `.name` is upper case (the checker's internal form)."""
    refused = False

    def show(self):
        return O.show(self.name)


class Table:
    pass


def parse(data):
    """Decode OTB bytes into a Table: header fields, pool view, FuncInfo per function."""
    data = bytes(data)
    if len(data) < HEADER_LEN or data[:3] != MAGIC:
        raise OtbError("not an OTB table (no 'OTB' magic)")
    t = Table()
    (t.format, t.lang, t.flags, nfunc, t.nrefused, t.pool_off, t.pool_len, t.ftab_off, t.aux_off,
     t.aux_len, t.line_off, t.line_len, t.code_off, t.code_len, t.total) = struct.unpack_from(
        ">BBBHHHHHHHHHHHH", data, 3)
    if t.format != FORMAT:
        raise OtbError("OTB format %d is not supported (only %d)" % (t.format, FORMAT))
    if t.total != len(data):
        raise OtbError("OTB size field %d differs from the %d bytes given" % (t.total, len(data)))
    t.data = data
    t.nfunc = nfunc
    t.pool = PoolView(data[t.pool_off:t.pool_off + t.pool_len])
    t.funcs = []
    t.fmap = OrderedDict()
    for k in range(nfunc):
        p = t.ftab_off + k * FENT
        f = FuncInfo()
        f.index = k
        f.name = data[p:p + NAME_LEN].rstrip(b"\0").decode("latin-1")
        (menu_off, f.hosts, f.families, f.touches, f.flags, f.max_cost, f.ns_mask, nargs, nparams,
         f.nlocals, f.max_stack, f.max_ctx, f.max_ctl, nctr, _r, file_off, f.def_line, code_off,
         code_len, aux_off, line_off, nlines) = struct.unpack_from(">HHHBBIIBBBBBBBBHHHHHHH", data, p + NAME_LEN)
        f.menus = t.pool.menus(menu_off)
        f.file = t.pool.string(file_off)
        ap = t.aux_off + aux_off
        f.params = OrderedDict()
        for _ in range(nparams):
            off, d, lo, hi = struct.unpack_from(">Hhhh", data, ap)
            f.params[t.pool.string(off)] = (d, lo, hi)
            ap += 8
        f.args = []
        for _ in range(nargs):
            f.args.append(t.pool.string(struct.unpack_from(">H", data, ap)[0]))
            ap += 2
        f.counters = []
        for _ in range(nctr):
            line, midx, _z = struct.unpack_from(">HBB", data, ap)
            f.counters.append((line, midx))
            ap += 4
        f.page = None
        if f.hosts & HOST_BITS["PAGE"]:
            f.page = t.pool.string(struct.unpack_from(">H", data, ap)[0])
            ap += 2
        f.module = None
        f.clock = None
        if f.hosts & HOST_BITS["MODULE"]:
            f.module = t.pool.string(struct.unpack_from(">H", data, ap)[0])
            f.clock = struct.unpack_from(">H", data, ap + 2)[0]
            ap += 4
        f.confirm_text = None
        if f.flags & F_CONFIRM_TEXT:
            f.confirm_text = t.pool.string(struct.unpack_from(">H", data, ap)[0])
            ap += 2
        f.vars = []
        if f.flags & F_VARS:
            nvars, = struct.unpack_from(">H", data, ap)
            ap += 2
            f.vars = list(struct.unpack_from(">%dh" % nvars, data, ap))
            ap += 2 * nvars
        if f.flags & F_TYPED_PARAMS:
            types_mask, = struct.unpack_from(">H", data, ap)
            ap += 2
            kinds = [(types_mask >> (2 * k)) & 3 for k in range(nparams)]
            folder_paths = []
            for k in range(nparams):
                if kinds[k] == 3:
                    path_off, = struct.unpack_from(">H", data, ap)
                    ap += 2
                    folder_paths.append(t.pool.string(path_off))
            option_labels = []
            for k in range(nparams):
                if kinds[k] == 2:
                    cnt, = struct.unpack_from(">H", data, ap)
                    ap += 2
                    lbls = []
                    for _ in range(cnt):
                        lbl_off, = struct.unpack_from(">H", data, ap)
                        ap += 2
                        lbls.append(t.pool.string(lbl_off))
                    option_labels.append(tuple(lbls))
            f_idx, o_idx = 0, 0
            new_params = OrderedDict()
            for k, (pname, (d, lo, hi)) in enumerate(f.params.items()):
                kd = kinds[k]
                if kd == 1:
                    new_params[pname] = O.Param(d, lo, hi, kind="BOOL")
                elif kd == 2:
                    new_params[pname] = O.Param(d, lo, hi, kind="OPTION", labels=option_labels[o_idx])
                    o_idx += 1
                elif kd == 3:
                    new_params[pname] = O.Param(d, lo, hi, kind="FOLDER", path=folder_paths[f_idx])
                    f_idx += 1
                else:
                    new_params[pname] = O.Param(d, lo, hi, kind="INT")
            f.params = new_params
        else:
            new_params = OrderedDict()
            for pname, (d, lo, hi) in f.params.items():
                new_params[pname] = O.Param(d, lo, hi, kind="INT")
            f.params = new_params
        if f.flags & F_PARAM_SOURCES:
            for nm, v in f.params.items():
                kind, ch, num, _z = struct.unpack_from(">BBBB", data, ap)
                ap += 4
                if kind == 1:
                    v.src = ("cc", ch, num)
                elif kind == 2:
                    v.src = ("note", ch)
                elif kind == 3:
                    v.src = ("vel", ch)
        f.launch = None
        if f.flags & F_LAUNCH:
            f.launch, = struct.unpack_from(">H", data, ap)
            ap += 2
        lp = t.line_off + line_off
        f.lines = [struct.unpack_from(">HH", data, lp + 4 * i) for i in range(nlines)]
        f.code = data[t.code_off + code_off:t.code_off + code_off + code_len]
        f.code_off = code_off
        f.nargs = nargs
        f.destructive = bool(f.flags & F_DESTRUCTIVE)
        f.confirm = bool(f.flags & F_CONFIRM)
        f.input_sample = bool(f.flags & F_INPUT_SAMPLE)
        t.funcs.append(f)
        t.fmap.setdefault(f.name, f)
    return t


def decode(code, pc):
    """(Ins, operands, size) of the instruction at `pc`."""
    if pc >= len(code):
        raise OtbError("code ends inside an instruction at %04x" % pc)
    ins = INS.get(code[pc])
    if ins is None:
        raise OtbError("unknown opcode 0x%02x at %04x" % (code[pc], pc))
    if pc + ins.size > len(code):
        raise OtbError("%s at %04x runs past the end of the code" % (ins.name, pc))
    return ins, struct.unpack_from(">" + ins.fmt, code, pc + 1), ins.size


def line_of(f, pc):
    """Source line of the instruction at `pc` (the last line entry at or before it)."""
    line = f.def_line
    for p, ln in f.lines:
        if p > pc:
            break
        line = ln
    return line


def func_at(t, idx):
    if not 0 <= idx < len(t.funcs):
        raise OtbError("call of function %d, the table has %d" % (idx, len(t.funcs)))
    return t.funcs[idx]


# ---- the disassembler ----------------------------------------------------------------------------
def op_name(ns, num):
    o = REG.get((ns, num))
    return o.shown if o else "?%d.%d" % (ns, num)


def fmt_instr(t, f, ins, ops):
    """Mnemonic and decoded operands of one instruction."""
    n = ins.name
    if n in ("push8", "push16"):
        return "%s %d" % (n, ops[0])
    if n == "pushk":
        return "pushk %s" % t.pool.describe(ops[0])
    if n == "pushc":
        from modules.otype import sim
        return "pushc %s" % (sim.cond_name(ops[0]) if ops[0] < len(sim.COND_NAMES) else "?%d" % ops[0])
    if n in ("ld", "st"):
        return "%s %s" % (n, VARS[ops[0]].lower() if ops[0] < len(VARS) else ("var%d" % (ops[0] - 16) if ops[0] >= 16 else "?"))
    if n == "lda":
        return "lda %s" % (O.show(f.args[ops[0]]) if ops[0] < len(f.args) else "?%d" % ops[0])
    if n == "ldp":
        names = list(f.params)
        return "ldp %s" % (O.show(names[ops[0]]) if ops[0] < len(names) else "?%d" % ops[0])
    if n in ("op", "set"):
        return "%s %s" % (n, op_name(*ops))
    if n == "opv":
        ns, num, form = ops
        nm = op_name(ns, num)
        if nm == "trk":
            word = {KW_CUR: "cur", KW_ALL: "all"}.get(form, "%d track%s" % (form, "" if form == 1 else "s"))
        else:
            word = {STEPS_ALL: "all", STEPS_PRED: "pred", STEPS_RANGE: "a b"}.get(form, "form %d" % form)
        return "opv %s %s" % (nm, word)
    if n in JUMPS or n in BACKWARD:
        if n in ("every", "skip"):
            return "%s #%d @%04x" % (n, ops[0], ops[1])
        if n == "jnev":
            kind_name = {1: "clock", 2: "note", 3: "cc"}.get(ops[0], "?%d" % ops[0])
            return "jnev %s %d %d @%04x" % (kind_name, ops[1], ops[2], ops[3])
        if n == "jngt":
            return "jngt %s %d @%04x" % (t.pool.describe(ops[0]), ops[1], ops[2])
        return "%s @%04x" % (n, ops[-1])
    if n == "call":
        g = t.funcs[ops[0]] if ops[0] < len(t.funcs) else None
        return "call %s" % (g.show() if g else "?%d" % ops[0])
    if n == "dir":
        return "dir deep" if ops[0] else "dir"
    if n in ("ext", "match"):
        return "%s %d" % (n, ops[0])
    return n


def func_listing(t, f, sources=None):
    """The listing of one function: a header line, params/args, then one instruction per line."""
    lines = []
    menu = "; ".join(" > ".join(m) for m in f.menus) or ("(page only)" if f.page is not None else ("(module only)" if f.module is not None else "(brick, no menu)"))
    fam = " ".join(fam_names(f.families)) or "-"
    tch = " ".join(n for n, b in TOUCH_BITS.items() if f.touches & b) or "-"
    loc = " ".join("abcd"[:f.nlocals]) or "-"
    flags = []
    if f.flags & F_DESTRUCTIVE:
        flags.append("destructive")
    if f.flags & F_CONFIRM:
        flags.append("confirm")
    if f.flags & F_INPUT_SAMPLE:
        flags.append("input_sample")
    lines.append("%s  (%s:%d)  menu %s  families %s  touches %s  cost %d  stack %d  scopes %d  "
                 "ctl %d  locals %s%s" % (f.show(), f.file, f.def_line, menu, fam, tch, f.max_cost,
                                          f.max_stack, f.max_ctx, f.max_ctl, loc,
                                          "  " + " ".join(flags) if flags else ""))
    hosts = " ".join(n for n, b in HOST_BITS.items() if f.hosts & b) or "-"
    nss = " ".join(n.lower() for n, k in O.NAMESPACES.items() if f.ns_mask & (1 << k)) or "-"
    lines.append("  hosts %s  namespaces %s  code %d bytes @%d  lines %d  counters %d"
                 % (hosts, nss, len(f.code), f.code_off, len(f.lines), len(f.counters)))
    if f.page is not None:
        lines.append('  page   "%s"' % f.page)
    if f.module is not None:
        lines.append('  module "%s" (clock %d)' % (f.module, f.clock))
    if f.confirm_text is not None:
        lines.append('  confirm "%s"' % f.confirm_text)
    if getattr(f, "launch", None) is not None:
        lines.append("  launch %d" % f.launch)
    if f.args:
        lines.append("  args   %s" % " ".join(O.show(a) for a in f.args))
    for k, (nm, p) in enumerate(f.params.items()):
        d, lo, hi = p[:3]
        kind = getattr(p, "kind", "INT")
        if kind == "BOOL":
            lines.append("  param  %d: %s bool default %s" % (k, O.show(nm), "YES" if d else "NO"))
        elif kind == "OPTION":
            opts = " ".join('"%s"' % lbl for lbl in getattr(p, "labels", ()))
            lines.append("  param  %d: %s option default %d [%s]" % (k, O.show(nm), d, opts))
        elif kind == "FOLDER":
            lines.append('  param  %d: %s folder default "%s"' % (k, O.show(nm), getattr(p, "path", "")))
        else:
            lines.append("  param  %d: %s default %d min %d max %d" % (k, O.show(nm), d, lo, hi))
    for k, (ln, midx) in enumerate(f.counters):
        lines.append("  every/skip counter %d: line %d%s" % (k, ln, "" if midx == 0 else ", modifier %d" % midx))
    starts = {p: ln for p, ln in f.lines}
    pc = 0
    while pc < len(f.code):
        ins, ops, size = decode(f.code, pc)
        hexs = " ".join("%02x" % b for b in f.code[pc:pc + size])
        text = fmt_instr(t, f, ins, ops)
        comment = ""
        if pc in starts:
            comment = "%d" % starts[pc]
            src = source_text_of(sources, f, starts[pc])
            if src:
                comment += "  " + src
        lines.append("%04x  %-11s  %-26s ; %s" % (pc, hexs, text, comment) if comment else
                     "%04x  %-11s  %-26s ;" % (pc, hexs, text))
        pc += size
    return "\n".join(l.rstrip() for l in lines)


def source_text_of(sources, f, line):
    """The source line (code only, no comment, trimmed) for the comment column."""
    raw = (sources or {}).get(f.file)
    if not raw or not 0 < line <= len(raw):
        return ""
    s = raw[line - 1]
    in_str = False
    for i, ch in enumerate(s):
        if ch == '"':
            in_str = not in_str
        elif ch == "#" and not in_str:
            s = s[:i]
            break
    return s.strip()


def disassemble(t, sources=None, func=None):
    """The whole listing, or the one function `func` (upper case name)."""
    if isinstance(t, (bytes, bytearray)):
        t = parse(t)
    out = []
    if func is None:
        out.append("OTB format %d  language %d  %d function%s, %d refused  %d bytes  "
                   "(pool %d, functions %d, aux %d, lines %d, code %d)"
                   % (t.format, t.lang, t.nfunc, "" if t.nfunc == 1 else "s", t.nrefused, t.total,
                      t.pool_len, t.nfunc * FENT, t.aux_len, t.line_len, t.code_len))
        out.append("")
    for f in t.funcs:
        if func is None or f.name == func:
            out.append(func_listing(t, f, sources))
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def hexdump(data):
    out = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        out.append("%04x  %-47s  %s" % (i, " ".join("%02x" % b for b in chunk),
                                         "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)))
    return "\n".join(out) + "\n"


# ---- command line: otype.py dump -----------------------------------------------------------------
DUMP_HELP = """usage: otype.py dump FILE.OTY [FILE2.OTY ...] [--func NAME] [-o OUT.OTB] [--hex] [--profile NAME]

Compile the checked functions into the OTB table (spec 14) and show it: per function a header
line, then one instruction per line (offset, bytes, mnemonic, operands, source line).  All files
are loaded together, like `check`.  Refused functions are absent from the table (the header
counts them); the diagnostics go to stderr.

options:
  --func NAME    list only this function (the table is still built whole)
  -o OUT.OTB     write the binary table
  --hex          print a hex dump of the table instead of the listing
  --profile NAME refuse the functions using an op the unit does not have yet ("not on the unit
                 yet: pl.set") and their callers ("needs NAME"); NAME is m1 (the profile of the unit)

exit status: 0 = table built, no error; 1 = an error (a refused function, a compile error);
             2 = bad usage
"""


def main(argv, checker, out=None, err=None):
    import os
    import sys
    out = out or sys.stdout
    err = err or sys.stderr
    files, func, outpath, hexm, profile, i = [], None, None, False, None, 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(DUMP_HELP, file=out)
            return 0
        if a == "--func":
            i += 1
            if i >= len(argv):
                print("usage: --func needs a name", file=err)
                return 2
            func = argv[i].upper()
        elif a == "-o":
            i += 1
            if i >= len(argv):
                print("usage: -o needs a file", file=err)
                return 2
            outpath = argv[i]
        elif a == "--hex":
            hexm = True
        elif a == "--profile":
            i += 1
            if i >= len(argv):
                print("usage: --profile needs a name", file=err)
                return 2
            if argv[i] not in O.PROFILES:
                print("unknown profile %s (one of %s)" % (argv[i], ", ".join(sorted(O.PROFILES))),
                      file=err)
                return 2
            profile = O.PROFILES[argv[i]]
        elif a.startswith("-") and a != "-":
            print("usage: unknown option %s" % a, file=err)
            return 2
        else:
            files.append(a)
        i += 1
    if not files:
        print(DUMP_HELP, file=err)
        return 2
    for f in files:
        if not os.path.isfile(f):
            print("%s is not a file" % f, file=err)
            return 2
    try:
        prog = checker.check_files(files)
    except OSError as e:
        print("cannot read %s: %s" % (e.filename, e.strerror), file=err)
        return 2
    for d in prog.sorted_diags():
        print(d, file=err)
    try:
        c = compile_program(prog, profile)
    except OtbError as e:
        print("dump: %s" % e, file=err)
        return 1
    for d in c.diags:
        print(d, file=err)
    if not c.data:
        return 1                      # too big for a table: only the error above
    t = parse(c.data)
    if func is not None and func not in t.fmap:
        print("no function named %s in the table" % O.show(func), file=err)
        return 1
    if outpath and not any(d.func == "-" for d in c.diags):
        with open(outpath, "wb") as fh:
            fh.write(c.data)
    if hexm:
        out.write(hexdump(c.data))
    else:
        out.write(disassemble(t, prog.raw, func))
    return 1 if (prog.error_count() or c.diags) else 0
