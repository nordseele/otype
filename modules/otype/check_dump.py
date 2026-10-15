"""Per-function state of the checker and of the code generator, as the C compiler must reproduce it.

    check_dump(sources, profile) -> text

Twin of `otc_check_dump` (firmware/c/otc_comp_dump.c), compared by tools/test_otype_ccomp.py on the whole corpus:
the report only shows the costs and the sizes through thresholds (`too long`, `code too long`, the size of the table), this
shows the numbers themselves, for every function.  One line per function, in the order of the files:

    chk N
    fn I NAME refused=R cost=C destr=D comp=X

R   the function is refused in the end (after the checker AND the compiler's diagnostics: `otb.apply_compile`)
C   `prog.maxcost` saturated at 0xFFFFFFFF, or `-` when the function was not costed (unregistered, or refused before)
D   `prog.destructive` (the function or something it calls changes the pattern: the table's flag)
X   the dry run of the code generator, for the functions the checker accepted: `-` when it did not run,
    `ok CODE LINES CTRS STACK CTX CTL LOCALS` (bytes of code, entries of the line table, every/skip counters, deepest stack,
    context scopes, control frames, locals a..d used) or `err LINE counters|stack|loop|context|code|menus` (the compiler
    refuses it)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_TOOLS = os.path.join(ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import otype  # noqa: E402
from modules.otype import ops as O  # noqa: E402
from modules.otype import otb  # noqa: E402


def _label(x):
    return x.decode("latin-1") if isinstance(x, (bytes, bytearray)) else x


def check_dump(sources, profile=None):
    if isinstance(profile, str):
        profile = O.PROFILES[profile]
    prog = otype.check_sources([(_label(lbl), bytes(data).decode("latin-1")) for lbl, data in sources])
    comp = {}
    for f in prog.funcs:
        if not f.registered or f.refused:
            continue
        try:
            fc = otb.FuncCompiler(prog, f, otb.Pool(), set())
            fc.compile()
            comp[f] = "ok %d %d %d %d %d %d %d" % (len(fc.code), len(fc.lines), len(fc.ctrs), fc.maxdepth, fc.maxctx, fc.maxctl,
                                                   fc.nlocals)
        except otb.CompileError as e:
            m = str(e)
            comp[f] = "err %d %s" % (e.line, "counters" if "counters" in m else "stack" if m.startswith("stack") else
                                    "loop" if m.startswith("loop") else "context" if m.startswith("context") else
                                    "menus" if "menu" in m else "code")
    otb.apply_compile(prog, profile)
    out = ["chk %d" % len(prog.funcs)]
    for i, f in enumerate(prog.funcs):
        c = prog.maxcost.get(f)
        out.append("fn %d %s refused=%d cost=%s destr=%d comp=%s" % (
            i, O.show(f.name), 1 if f.refused else 0, "-" if c is None else min(c, otype.COST_SAT),
            1 if prog.destructive.get(f) else 0, comp.get(f, "-")))
    return "\n".join(out) + "\n"
