"""OType reference VM: runs an OTB table (modules/otype/otb.py, spec 14) byte by byte.

It is the model of the firmware's interpreter.  It reads NOTHING but the table's bytes, and it
shares with the tree-walking simulator (modules/otype/sim.py) the data model, the random streams,
the state operators, and the handler of every registry operation: the two engines must give
identical results (tests/otb equivalence: model, draws, cost, out-of-range list, state).

The VM state is explicit and resumable: `VMRun.frames` (one VFrame per call: pc, value stack,
locals a b c d, counters i j, context scopes, control frames, argument values, file list), the
globals x y z t, the random streams (inside the contexts), the cost counter.  `step()` runs ONE
instruction; `run()` is just `while frames: step()`.

    simulate_otb(table, "name", seed=7, ...) -> sim.Result        (table: otb.Table or bytes)
    run_vm(prog, "name", ...)                -> sim.Result        (compiles `prog` first: `run --vm`)
"""
import struct
from types import SimpleNamespace

from modules.otype import ops as O
from modules.otype import otb
from modules.otype import sim

MAX_CALL_DEPTH = 8       # spec 10


class VMError(sim.RunError):
    """The table is inconsistent (a compiler or table bug, never a script's fault)."""


class Site:
    """What a handler reads of its call site (a tree node in the simulator): the op and the line."""
    __slots__ = ("op", "line", "kinds")

    def __init__(self, op, line, kinds):
        self.op, self.line, self.kinds = op, line, kinds


class OtbProgram:
    """The part of a checked Program that `sim.simulate` reads, backed by a parsed table."""

    def __init__(self, table):
        self.table = table
        self.fmap = table.fmap
        self.maxcost = {f: f.max_cost for f in table.funcs}


class Ctl:
    """A control frame: an l / each loop (kind L, E) or a pass over tracks (kind T)."""
    __slots__ = ("kind", "start", "end", "depth", "cur", "last", "step", "items", "idx", "si", "sj")


class VFrame:
    """One call: everything the VM needs to resume it."""

    def __init__(self, fn, args, files, ctxs):
        self.fn, self.args, self.files, self.ctxs = fn, list(args), files, ctxs
        self.own = 0             # name slots of this call's own `dir` base (sim.Frame.own)
        self.pc = self.ipc = 0
        self.stack = []
        self.locals = [0, 0, 0, 0]
        self.i = self.j = 0
        self.ctl = []
        self.plist = []          # the track list made by `opv trk`, consumed by the next `pass`


def _special(op):
    return any(isinstance(k, (O.OPT_KW, O.REST)) or k in (O.TRACKS, O.STEPSEL) for k in op.args)


class VMRun(sim.Run):
    def __init__(self, prog, root, *args):
        sim.Run.__init__(self, prog, root, *args)
        self.table = prog.table
        self.pool = prog.table.pool
        self.frames = []
        self.hw = {}                       # function -> [stack, contexts, control frames] reached
        self.executed = 0
        self.lazy_cost = O.OPS["?"].cost
        self.ops = {"DIR": O.OPS["DIR"], "EXT": O.OPS["EXT"], "MATCH": O.OPS["MATCH"], "MSG": O.OPS["MSG"],
                    "TRK": O.OPS["TRK"], "STEPS": O.OPS["STEPS"]}

    def live_frames(self):
        return self.frames

    # ---- running -------------------------------------------------------------------------
    def run(self):
        steps = list(range(1, self.length + 1))
        ctx = sim.Ctx([self.host_track], steps, self.new_streams(self.seed))
        self.frames = [VFrame(self.root, [], sim.FileList(), [ctx.copy()])]
        while self.frames:
            self.step()

    def step(self):
        """Execute one instruction of the running call."""
        fr = self.frames[-1]
        code = fr.fn.code
        pc = fr.pc
        ins, ops, size = otb.decode(code, pc)
        fr.ipc, fr.pc = pc, pc + size
        getattr(self, "i_" + ins.name)(fr, *ops)
        self.executed += 1
        if self.frames:
            f = self.frames[-1]
            h = self.hw.setdefault(f.fn.name, [0, 0, 0])
            h[0] = max(h[0], len(f.stack))
            h[1] = max(h[1], len(f.ctxs))
            h[2] = max(h[2], len(f.ctl))
            if len(f.stack) > f.fn.max_stack or len(f.ctxs) > f.fn.max_ctx or len(f.ctl) > f.fn.max_ctl:
                raise VMError("%s: stack / scope / control depth beyond what the table declares"
                              % f.fn.show())

    # ---- helpers -------------------------------------------------------------------------
    def line(self, fr):
        return otb.line_of(fr.fn, fr.ipc)

    @staticmethod
    def push(fr, v):
        fr.stack.append(v)

    @staticmethod
    def pop(fr):
        if not fr.stack:
            raise VMError("pop from an empty stack")
        return fr.stack.pop()

    def popn(self, fr, n):
        if n > len(fr.stack):
            raise VMError("pop of %d values from a stack of %d" % (n, len(fr.stack)))
        vals = fr.stack[len(fr.stack) - n:]
        del fr.stack[len(fr.stack) - n:]
        return vals

    def operation(self, ns, num):
        op = otb.REG.get((ns, num))
        if op is None:
            raise VMError("unknown operation (%d, %d)" % (ns, num))
        return op

    def convert(self, kinds, vals):
        """STR and LIST arguments reach the stack as pool offsets: bring them back."""
        out = []
        for k, v in zip(kinds, vals):
            if k in (O.STR, O.NAME):
                out.append(self.pool.string(v))
            elif k == O.LIST:
                out.append(tuple(self.pool.list(v)))
            else:
                out.append(v)
        return out

    # ---- constants and variables ---------------------------------------------------------
    def i_push8(self, fr, v):
        self.push(fr, v)

    i_push16 = i_pushk = i_pushc = i_push8

    def i_ld(self, fr, v):
        if v < 4:
            x = fr.locals[v]
        elif v < 8:
            x = self.globals["XYZT"[v - 4]]
        elif v >= 16:
            slot = v - 16
            st = self.fn_vars.setdefault(fr.fn, {})
            if slot not in st:
                fn_key = O.show(getattr(fr.fn, "name", fr.fn))
                saved = getattr(self, "saved_vars", {}).get(fn_key)
                if saved is None:
                    saved = getattr(self, "saved_vars", {}).get(str(getattr(fr.fn, "name", fr.fn)).upper(), {})
                if slot in saved:
                    st[slot] = saved[slot]
                elif str(slot) in saved:
                    st[slot] = saved[str(slot)]
                else:
                    init_val = fr.fn.vars[slot] if hasattr(fr.fn, "vars") and slot < len(fr.fn.vars) else 0
                    st[slot] = init_val
            x = st[slot]
        else:
            x = fr.i if v == 8 else fr.j
        self.push(fr, x)

    def i_st(self, fr, v):
        x = self.pop(fr)
        if v < 4:
            fr.locals[v] = x
        elif v >= 16:
            slot = v - 16
            st = self.fn_vars.setdefault(fr.fn, {})
            if slot not in st:
                fn_key = O.show(getattr(fr.fn, "name", fr.fn))
                saved = getattr(self, "saved_vars", {}).get(fn_key)
                if saved is None:
                    saved = getattr(self, "saved_vars", {}).get(str(getattr(fr.fn, "name", fr.fn)).upper(), {})
                if slot in saved:
                    st[slot] = saved[slot]
                elif str(slot) in saved:
                    st[slot] = saved[str(slot)]
            st[slot] = max(-32768, min(32767, x))
        else:
            self.globals["XYZT"[v - 4]] = x

    def i_lda(self, fr, n):
        self.push(fr, fr.args[n])

    def i_ldp(self, fr, n):
        name = list(fr.fn.params)[n]
        d = fr.fn.params[name][0]
        if fr.fn is self.root and name in self.params:
            d = self.params[name]
        self.push(fr, d)

    def i_drop(self, fr):
        self.pop(fr)

    # ---- operations ----------------------------------------------------------------------
    def i_op(self, fr, ns, num):
        op = self.operation(ns, num)
        if _special(op) or op.lazy:
            raise VMError("%s has no plain form" % op.shown)
        vals = self.convert(op.args, self.popn(fr, len(op.args)))
        ctx = fr.ctxs[-1]
        site = Site(op, self.line(fr), op.args)
        self.charge(op.cost)
        if op.family == O.CTX:
            self.ctx_fixed(op, ctx, fr, site.line, vals)
            r = 0
        else:
            r = self.call_op(op, vals, op.args, ctx, fr, site)
        if op.ret is not None:
            self.push(fr, r)

    def ctx_fixed(self, op, ctx, fr, line, vals):
        name = op.name
        if name == "STEP":
            self.step_set(op, ctx, fr, line, vals[0])
        elif name == "SEED":
            self.seed_all(ctx, vals[0])
        elif name.endswith(".SEED"):
            self.seed_one(ctx, name[:-5], vals[0])
        else:
            raise VMError("context operation %s has no plain form" % op.shown)

    def i_opv(self, fr, ns, num, form):
        op = self.operation(ns, num)
        ctx = fr.ctxs[-1]
        line = self.line(fr)
        self.charge(op.cost)
        if op.name == "TRK":
            if form == otb.KW_CUR:
                fr.plist = [self.cur_track(ctx)]
            elif form == otb.KW_ALL:
                fr.plist = list(range(1, 9))
            else:
                fr.plist = self.tracks_from(self.popn(fr, form), op, line, fr)
        elif op.name == "STEPS":
            if form == otb.STEPS_ALL:
                self.steps_all(ctx)
            elif form == otb.STEPS_PRED:
                v = self.pop(fr)
                self.steps_pred(ctx, self.operation(v >> 8, v & 255))
            elif form == otb.STEPS_RANGE:
                a, b = self.popn(fr, 2)
                self.steps_range(op, ctx, fr, line, a, b)
            else:
                raise VMError("steps: form %d" % form)
        else:
            raise VMError("%s has no variable form" % op.shown)

    def i_set(self, fr, ns, num):
        op = self.operation(ns, num)
        self.charge(1)
        k = len(op.args) + 1
        vals = self.popn(fr, k)
        self.set_state(op, vals, fr.ctxs[-1], fr, line=self.line(fr))

    def i_dir(self, fr, deep):
        op = self.ops["DIR"]
        arg = self.pop(fr)
        path = arg if isinstance(arg, str) else self.pool.string(arg)
        vals = [path] + (["DEEP"] if deep else [])
        self.charge(op.cost)
        self.call_op(op, vals, op.args, fr.ctxs[-1], fr, Site(op, self.line(fr), op.args))

    def i_ext(self, fr, n):
        op = self.ops["EXT"]
        names = op.args[0].kind.names
        vals = [names[c] for c in self.popn(fr, n)]
        self.charge(op.cost)
        self.call_op(op, vals, op.args, fr.ctxs[-1], fr, Site(op, self.line(fr), op.args))

    def i_match(self, fr, n):
        op = self.ops["MATCH"]
        vals = [self.pool.string(v) for v in self.popn(fr, n)]
        self.charge(op.cost)
        self.call_op(op, vals, op.args, fr.ctxs[-1], fr, Site(op, self.line(fr), op.args))

    def i_msg(self, fr, n, mask):
        op = self.ops["MSG"]
        vals = [self.pool.string(v) if mask >> i & 1 else v for i, v in enumerate(self.popn(fr, n))]
        self.charge(op.cost)
        self.call_op(op, vals, op.args, fr.ctxs[-1], fr, Site(op, self.line(fr), op.args))

    # ---- forward jumps -------------------------------------------------------------------
    def i_jmp(self, fr, off):
        fr.pc = off

    def i_jz(self, fr, off):
        if self.pop(fr) == 0:
            fr.pc = off

    def i_sel(self, fr, off):
        self.charge(self.lazy_cost)
        if self.pop(fr) == 0:
            fr.pc = off

    def i_prob(self, fr, off):
        p = sim.clamp(self.pop(fr), 0, 100)
        if not self.below(fr.ctxs[-1], "PROB", 100) < p:
            fr.pc = off

    def _counter(self, fr, k, n):
        line, midx = fr.fn.counters[k]
        return self.every_fires(fr, SimpleNamespace(no=line), midx, n)

    def i_every(self, fr, k, off):
        if not self._counter(fr, k, self.pop(fr)):
            fr.pc = off

    def i_skip(self, fr, k, off):
        if self._counter(fr, k, self.pop(fr)):
            fr.pc = off

    def i_jnev(self, fr, kind, ch, num, off):
        ev = getattr(self, "current_event", None)
        match = False
        if ev is None:
            match = True
        else:
            etype = ev.get("type") if isinstance(ev, dict) else None
            if kind == 1 and etype == "CLOCK":
                match = True
            elif kind == 2 and etype == "NOTE":
                if ch == 0 or ch == 255 or ev.get("ch") == ch:
                    match = True
            elif kind == 3 and etype == "CC":
                ch_ok = (ch == 0 or ch == 255 or ev.get("ch") == ch)
                num_ok = (num == 255 or ev.get("num") == num)
                if ch_ok and num_ok:
                    match = True
            elif kind == 4 and etype == "TRIG":
                # num is the block's genre: 0 = plays only, 1 = every trig placed, 2 = a script trig
                g = ev.get("trig.genre", 0)
                if ev.get("track") == ch:
                    if num == 2:
                        match = g == 2
                    elif num == 1:
                        match = g == 0
                    else:
                        match = g == 0 and ev.get("trig.hit") == 1
        if not match:
            fr.pc = off

    def i_jngt(self, fr, off, out, target):
        """The guard of an `in gate NAME n:` block: a GATE event from NAME on output n.
        With no event the block does not run (the C VM's OTC_EV_GATE check); unlike jnev."""
        ev = getattr(self, "current_event", None)
        name = self.pool.string(off)
        if isinstance(ev, dict) and ev.get("type") == "GATE" \
                and ev.get("gate.name") == name and ev.get("gate.out") == out:
            return
        fr.pc = target

    # ---- the loops: the only backward jumps ----------------------------------------------
    def _frame(self, fr, kind, end):
        c = Ctl()
        c.kind, c.start, c.end, c.depth = kind, fr.pc, end, len(fr.ctxs)
        c.si, c.sj = fr.i, fr.j
        c.cur = c.last = c.step = c.items = c.idx = None
        fr.ctl.append(c)
        return c

    @staticmethod
    def _top(fr, kinds):
        if not fr.ctl or fr.ctl[-1].kind not in kinds:
            raise VMError("loop instruction without its control frame")
        return fr.ctl[-1]

    def i_lbeg(self, fr, end):
        a, b = self.popn(fr, 2)
        c = self._frame(fr, "L", end)
        c.cur, c.last, c.step = a, b, (1 if a <= b else -1)
        fr.j, fr.i = fr.i, a
        fr.ctxs.append(fr.ctxs[-1].copy())

    def i_lnext(self, fr, back):
        c = self._top(fr, "L")
        del fr.ctxs[c.depth:]
        if c.cur == c.last:
            fr.ctl.pop()
            fr.i, fr.j = c.si, c.sj
            return
        c.cur += c.step
        fr.i = c.cur
        fr.ctxs.append(fr.ctxs[-1].copy())
        fr.pc = back

    def _each_scope(self, fr, c):
        ctx = fr.ctxs[-1].copy()
        ctx.steps, ctx.step = [c.items[c.idx]], c.items[c.idx]
        fr.ctxs.append(ctx)
        fr.i = c.items[c.idx]

    def i_ebeg(self, fr, end):
        items = list(fr.ctxs[-1].steps)
        if not items:
            fr.pc = end
            return
        c = self._frame(fr, "E", end)
        c.items, c.idx = items, 0
        fr.j = fr.i
        self._each_scope(fr, c)

    def i_enext(self, fr, back):
        c = self._top(fr, "E")
        del fr.ctxs[c.depth:]
        c.idx += 1
        if c.idx >= len(c.items):
            fr.ctl.pop()
            fr.i, fr.j = c.si, c.sj
            return
        self._each_scope(fr, c)
        fr.pc = back

    def _pass_scope(self, fr, c):
        ctx = fr.ctxs[-1].copy()
        ctx.tracks = [c.items[c.idx]]
        fr.ctxs.append(ctx)

    def i_pass(self, fr, end):
        tracks, fr.plist = fr.plist, []
        if not tracks:
            fr.pc = end
            return
        c = self._frame(fr, "T", end)
        c.items, c.idx = tracks, 0
        self._pass_scope(fr, c)

    def i_pnext(self, fr, back):
        c = self._top(fr, "T")
        del fr.ctxs[c.depth:]
        c.idx += 1
        if c.idx >= len(c.items):
            fr.ctl.pop()
            return
        self._pass_scope(fr, c)
        fr.pc = back

    def i_break(self, fr):
        while fr.ctl:
            c = fr.ctl.pop()
            if c.kind in ("L", "E"):
                del fr.ctxs[c.depth:]
                fr.i, fr.j = c.si, c.sj
                fr.pc = c.end
                return
        raise VMError("break outside a loop")

    # ---- scopes, calls -------------------------------------------------------------------
    def i_scope(self, fr):
        fr.ctxs.append(fr.ctxs[-1].copy())

    def i_unscope(self, fr):
        if len(fr.ctxs) < 2:
            raise VMError("unscope of the function's own context")
        fr.ctxs.pop()

    def i_call(self, fr, idx):
        g = otb.func_at(self.table, idx)
        if len(self.frames) >= MAX_CALL_DEPTH:
            raise VMError("call depth beyond %d" % MAX_CALL_DEPTH)
        args = self.popn(fr, len(g.args))
        self.frames.append(VFrame(g, args, fr.files.copy(), [fr.ctxs[-1].copy()]))

    def _leave(self, val):
        self.frames.pop()
        if self.frames:
            self.push(self.frames[-1], val)

    def i_ret(self, fr):
        self._leave(self.pop(fr))

    def i_stop(self, fr):
        self._leave(0)


def simulate_otb(table, func, **kw):
    """`sim.simulate` on the table: the same Result as the simulator's for the same input."""
    if isinstance(table, (bytes, bytearray)):
        table = otb.parse(table)
    return sim.simulate(OtbProgram(table), func, run_class=VMRun, **kw)


def run_vm(prog, func, **kw):
    """`otype.py run --vm`: compile a checked program, run FUNC on the table."""
    name = func.upper()
    fn = prog.fmap.get(name)
    if fn is None:
        raise sim.SimError("no function named %s" % O.show(name))
    if fn.refused:
        raise sim.SimError("refused")
    c = otb.compile_program(prog)
    table = otb.parse(c.data)
    if name not in table.fmap:
        why = "; ".join(str(d) for d in c.diags if d.func == O.show(name))
        raise sim.SimError("%s is not in the compiled table (%s)" % (O.show(name), why))
    return simulate_otb(table, func, **kw)


def simulate_otb_module(table, func, total_ticks, **kw):
    """`sim.simulate_module` on the table via VMRun."""
    if isinstance(table, (bytes, bytearray)):
        table = otb.parse(table)
    return sim.simulate_module(OtbProgram(table), func, total_ticks, run_class=VMRun, **kw)

