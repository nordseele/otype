#!/usr/bin/env python3
"""Generate firmware/c/otc_ops.h from the operation registry (modules/otype/ops.py).

    python3 modules/otype/gen_ops_c.py [--check]

The registry is the single source: the C VM (firmware/c/vm.c) never repeats an op's id,
argument count, cost, constant value, argument range or family. `--check` exits 1 if the committed
header differs from what the registry produces now (tools/test_otype_cvm.py runs the same check).
the same script also writes firmware/c/otc_opreg.h (the registry + language constants for the compiler's front
end, firmware/c/otc_comp.c; `render_reg`), checked by `--check` and by tools/test_otype_ccomp.py.

Per operation (sorted by (namespace, number), so the VM can binary-search it):
  ns num      the id of the operation
  nargs       len(op.args): what `op` pops (an OPT_KW or REST kind counts as one)
  flags       RET       the op pushes a value
              CTX       family CTX (trk steps step seed x.seed)
              VALUE     a constant (op.value): the handler is not run, the value is the result
              SUPPORTED the C VM runs it (see SUPPORTED_RULE)
              CARD      a card / slot operation: it needs the workspace bound with
                        otc_vm_bind_work, else the run stops with OTC_E_UNSUPPORTED
              NOPLAIN   no plain `op` form (lazy ?, or special arguments: trk steps dir ext match)
  handler     OTC_H_<NAME>: what vm.c switches on
  cost        registry cost
  value       op.value (when VALUE)
  ak/lo/hi    per argument: kind code (none, PCT clamp 0..100, RANGE lo..hi from ops.RANGES,
              COND 0..64, LIST pool offset, STR pool offset) and its bounds
"""
import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from modules.otype import ops as O  # noqa: E402
sys.path.insert(0, str(ROOT / "tools"))
import otype  # noqa: E402  (the language constants and describe(): the compiler's single source)

C_DIR = ROOT / "firmware" / "c" if (ROOT / "firmware" / "c").is_dir() else HERE / "c"
HEADER = C_DIR / "otc_ops.h"
REG_HEADER = C_DIR / "otc_opreg.h"       # what the compiler's front end (otc_comp.c) reads

# The VM runs every op of level "vm" except these (they read the card / the slot list), plus two
# pure reads of the run context. Everything else stops a run with OTC_E_UNSUPPORTED.
SUPPORTED_RULE = ("level == 'vm', plus TRK.CUR and PAT.LEN, plus the trig / sample-lock ops, the p-lock ops, "
                  "the part ops and the trig-word ops (E2B_EXTRA, M3A_EXTRA, M3B_EXTRA and M3C_EXTRA of "
                  "modules/otype/gen_ops_c.py), plus the card / slot ops of category CARD and SLOTS "
                  "(they need the workspace)")
E1B_EXTRA = frozenset(("TRK.CUR", "PAT.LEN"))
# the trig / sample-lock operations, run on the pattern's bytes bound with otc_vm_bind
E2B_EXTRA = frozenset(("TR.ADD", "TR.DEL", "TR.FLIP", "TR.CLR", "HAS.TRIG", "HAS.LOCK", "HAS.SMP",
                       "IS.EMPTY", "TRK.TYPE", "SMP.SET", "SMP.GET", "SMP.DEL", "SMP.RND", "SMP.CLR",
                       "RT.ADD", "RT.DEL", "RT.CLR", "RT.SRC", "RO.ADD", "RO.DEL"))

# the p-lock and trigless-lock operations, run on the same bound blocks; the PL.* ops also read the part's
# descriptor and value through the two otb_host_param_* imports
M3A_EXTRA = frozenset(("PL.SET", "PL.GET", "PL.DEL", "PL.RND", "PL.VARY", "PL.CLR", "TL.ADD", "TL.FILL"))

# the part operations, through the part imports (otb_host_par_set ...).
M3B_EXTRA = frozenset(("PAR.SET", "LFO.DEST", "FX.TYPE", "SCN.CLR", "TRK.INIT",
                       "TRK.SLOT", "TEMPO", "REC.SRC", "REC.LEN", "REC.TRIG", "REC.LOOP", "REC.ARM", "PAT.COPY"))

# the trig-word operations (condition, micro-timing, count), run on the bound blocks like the trig / sample-lock ops
M3C_EXTRA = frozenset(("COND.SET", "COND.RND", "MICRO.SET", "MICRO.RND", "RATCH.SET"))

V1_EXTRA = O._V1_OPS | frozenset(("MSG",))      # msg: the run keeps the text of its last msg (OTC_MSG_MAX characters)
AUDIO_EXTRA = frozenset(("SRC.SAVE", "FLEX.LOAD", "FLEX.FREE", "FLEX.FIRSTFREE", "FLEX.DEL", "CAT.CLR", "CAT.ADD", "CAT.SAVE", "NORM"))
MODULE_EXTRA = frozenset(("PLAY", "SEQ.RUN", "SEQ.STEP", "SEQ.MUTE", "NX.SMP", "NX.PL",
                          "SEND.NOTE", "SEND.CC", "SEND.PROG", "SEND.NRPN",
                          "MIDI.CC", "MIDI.NOTE", "MIDI.VEL", "GATE", "OUT", "CV"))

# the card / slot operations (dir ext match file.* static.* slots.*), run on the workspace
# bound with otc_vm_bind_work (without it they stop the run with OTC_E_UNSUPPORTED, as without a workspace)

AK = {"none": 0, "pct": 1, "range": 2, "cond": 3, "list": 4, "str": 5}


def supported(op):
    return (op.level == "vm" or op.name in E1B_EXTRA or op.name in E2B_EXTRA or op.name in M3A_EXTRA
            or op.name in M3B_EXTRA or op.name in M3C_EXTRA or op.name in V1_EXTRA or op.name in AUDIO_EXTRA
            or op.name in MODULE_EXTRA or card(op))


def card(op):
    return op.category in ("FILES", "SLOTS", "AUDIO")


def noplain(op):
    special = any(isinstance(k, (O.KW, O.REST)) or k in (O.TRACKS, O.STEPSEL) for k in op.args)
    return special or op.lazy


def ident(name):
    return "OTC_H_" + ("SEL" if name == "?" else name.replace(".", "_"))


def arg_kind(k):
    if k == O.PCT:
        return AK["pct"], 0, 100
    if k == O.COND:
        return AK["cond"], 0, 64
    if k in O.RANGES:
        return AK["range"], O.RANGES[k][0], O.RANGES[k][1]
    if k == O.LIST:
        return AK["list"], 0, 0
    if k == O.STR or k == O.NAME:
        return AK["str"], 0, 0
    return AK["none"], 0, 0


def render():
    ops = sorted(O.ALL_OPS, key=lambda o: o.id)
    L = []
    L.append("/* GENERATED by modules/otype/gen_ops_c.py from modules/otype/ops.py: do not edit.")
    L.append(" * Regenerate with: python3 modules/otype/gen_ops_c.py")
    L.append(" * Supported in " + SUPPORTED_RULE + ". */")
    L.append("#ifndef OTC_OPS_H")
    L.append("#define OTC_OPS_H")
    L.append("#include <stdint.h>")
    L.append("")
    L.append("enum {")
    L.append("    OTC_H_NONE = 0,")
    for o in ops:
        L.append("    %s,   /* %s (%d, %d) */" % (ident(o.name), o.shown, o.id[0], o.id[1]))
    L.append("    OTC_H_COUNT")
    L.append("};")
    L.append("")
    L.append("enum { OTC_AK_NONE = 0, OTC_AK_PCT = 1, OTC_AK_RANGE = 2, OTC_AK_COND = 3,")
    L.append("       OTC_AK_LIST = 4, OTC_AK_STR = 5 };")
    L.append("#define OTC_OF_RET       1")
    L.append("#define OTC_OF_CTX       2")
    L.append("#define OTC_OF_VALUE     4")
    L.append("#define OTC_OF_SUPPORTED 8")
    L.append("#define OTC_OF_NOPLAIN   16")
    L.append("#define OTC_OF_CARD      32")
    L.append("#define OTC_OF_SETTABLE  64")
    L.append("")
    L.append("typedef struct {")
    L.append("    uint8_t ns, num, nargs, flags;")
    L.append("    uint8_t handler;            /* OTC_H_* */")
    L.append("    uint8_t ak[4];              /* OTC_AK_* per argument */")
    L.append("    uint16_t cost;")
    L.append("    int16_t value;              /* the result of a constant op (OTC_OF_VALUE) */")
    L.append("    int16_t lo[4], hi[4];       /* bounds of OTC_AK_RANGE / OTC_AK_COND / OTC_AK_PCT */")
    L.append("} otc_op_t;")
    L.append("")
    L.append("#define OTC_NOPS %d" % len(ops))
    L.append("#define OTC_MSG_MAX %d            /* tools/otype.py MAX_MSG: the most characters of a msg text */" % otype.MAX_MSG)
    L.append("#define OTC_MSG_ITEMS %d          /* tools/otype.py MAX_MSG_ITEMS */" % otype.MAX_MSG_ITEMS)
    L.append("#if defined(OTC_MSG_BUF) && OTC_MSG_MAX >= OTC_MSG_BUF")
    L.append('#error "ops.MAX_MSG does not fit the msg buffer of otc_vm.h (OTC_MSG_BUF): enlarge the buffer"')
    L.append("#endif")
    L.append("")
    L.append("/* sorted by (ns, num) */")
    L.append("static const otc_op_t otc_ops[OTC_NOPS] = {")
    for o in ops:
        if len(o.args) > 4:
            raise ValueError("op %s has more than 4 arguments" % o.name)
        flags = []
        if o.ret is not None:
            flags.append("OTC_OF_RET")
        if o.family == O.CTX:
            flags.append("OTC_OF_CTX")
        if o.value is not None:
            flags.append("OTC_OF_VALUE")
        if supported(o):
            flags.append("OTC_OF_SUPPORTED")
        if noplain(o):
            flags.append("OTC_OF_NOPLAIN")
        if card(o):
            flags.append("OTC_OF_CARD")
        if o.settable:
            flags.append("OTC_OF_SETTABLE")
        ks = [arg_kind(k) for k in o.args] + [(0, 0, 0)] * (4 - len(o.args))
        L.append("    {%d, %d, %d, %s, %s, {%s}, %d, %d, {%s}, {%s}},   /* %s */" % (
            o.id[0], o.id[1], len(o.args), " | ".join(flags) or "0", ident(o.name),
            ", ".join(str(k[0]) for k in ks), o.cost, o.value if o.value is not None else 0,
            ", ".join(str(k[1]) for k in ks), ", ".join(str(k[2]) for k in ks), o.shown))
    L.append("};")
    L.append("")
    L.append("#endif")
    return "\n".join(L) + "\n"


# ---- the registry as the compiler's front end needs it (c/otc_opreg.h) -------------------------------
# Argument kind codes.  The same codes name the kind of an argument in the parse tree (node.akind): `otc_kindname`
# gives the text the Python tree holds in Node.kinds ("INT" ... "KW", "USER").
KIND_CODES = ("INT", "STEP", "TRACK", "SLOT", "PARAM", "VALUE", "COND", "PCT", "FILE", "STR", "LIST",
              "TRACKS", "STEPSEL", "KW", "OPTKW", "REST", "USER", "ITEM",
              "CH", "VAL7", "DUR", "VAL14", "NAME", "OUT")      # codes 1..24 (0 = none)
FAMILIES = (O.LANG, O.CTX, O.EDIT, O.IO_R, O.IO_W, O.UI, O.STATE)
TOUCHES = (O.TRAC, O.PART, O.SLOTS, O.NONE)
FLAGS = (("RET", lambda o: o.ret is not None), ("CTX", lambda o: o.family == O.CTX),
         ("STMT_ONLY", lambda o: o.stmt_only), ("PRED", lambda o: o.predicate),
         ("SETTABLE", lambda o: o.settable), ("LAZY", lambda o: o.lazy),
         ("DESTRUCTIVE", lambda o: o.destructive), ("M1", lambda o: o.name in O.PROFILES["m1"]))
LANG_SETS = (("KEYWORDS", "keywords"), ("VARS", "variables"), ("ASSIGNABLE", "assignable variables"),
             ("DECLS", "declarations"), ("MODIFIERS", "modifiers"), ("COND_WORDS", "named trig conditions"),
             ("FUTURE_HOSTS", "hosts of `on` that do not exist yet"))
SPECIAL_OPS = ("TRK", "STEPS", "DIR", "EXT", "MATCH", "MSG")      # ops with a code shape of their own
LANG_NUMS = ("MAX_LINE", "MAX_STR", "MAX_LIST", "MAX_ARGS", "MAX_PARAMS", "MAX_DEPTH", "MAX_NEST", "MAX_FUNCS",
             "LEVEL_MAX", "ACTION_MAX", "NAME_MAX", "MAX_FILES", "MAX_SOURCE_BYTES", "MENU_BUDGET", "MODULE_BUDGET", "EACH_ITERS", "PAGE_LABEL_MAX", "PAGE_MAX_PARAMS", "MODULE_LABEL_MAX", "MAX_MSG", "MAX_MSG_ITEMS")


class _Blob:
    """NUL-terminated strings, deduplicated; offsets fit 16 bits."""
    def __init__(self):
        self.data = bytearray()
        self.at = {}

    def add(self, s):
        b = s.encode("latin-1")
        if b not in self.at:
            self.at[b] = len(self.data)
            self.data += b + b"\0"
        return self.at[b]


def _c_bytes(data):
    out, line = [], []
    for ch in data:
        line.append("%d" % ch)
        if len(line) == 24:
            out.append("    " + ",".join(line) + ",")
            line = []
    if line:
        out.append("    " + ",".join(line) + ",")
    return "\n".join(out)


def render_reg():
    blob = _Blob()
    blob.add("")
    ops = list(O.ALL_OPS)                      # declaration order = the index the C uses
    if len(ops) > 255:
        raise ValueError("more than 255 ops")
    kwlists = []                               # tuples of upper-case names, deduplicated

    def kwidx(names):
        names = tuple(names)
        if names not in kwlists:
            kwlists.append(names)
        return kwlists.index(names)

    rows = []
    for o in ops:
        ak, ak2, akw = [0] * 4, [0] * 4, [0xFF] * 4
        for i, k in enumerate(o.args):
            if isinstance(k, O.OPT_KW):
                ak[i], akw[i] = KIND_CODES.index("OPTKW") + 1, kwidx(k.names)
            elif isinstance(k, O.REST):
                ak[i] = KIND_CODES.index("REST") + 1
                if isinstance(k.kind, O.KW):
                    ak2[i], akw[i] = KIND_CODES.index("KW") + 1, kwidx(k.kind.names)
                else:
                    ak2[i] = KIND_CODES.index(k.kind) + 1
            elif isinstance(k, O.KW):
                ak[i], akw[i] = KIND_CODES.index("KW") + 1, kwidx(k.names)
            elif isinstance(k, O.OPT_INT):
                ak[i] = KIND_CODES.index("INT") + 1
            else:
                ak[i] = KIND_CODES.index(k) + 1
        flags = " | ".join("OTC_RF_" + n for n, f in FLAGS if f(o)) or "0"
        ret = KIND_CODES.index(o.ret) + 1 if o.ret is not None else 0
        rows.append("    {0x%02x%02x, %d, %d, %d, %d, %d, %d, %d, %d, %d, %s, %d, {%s}, {%s}, {%s}},   /* %s */" % (
            o.id[0], o.id[1], blob.add(o.name), blob.add(o.shown), blob.add(otype.describe(o)),
            o.cost, o.value if o.value is not None else 0, len(o.args), o.min_args,
            FAMILIES.index(o.family), TOUCHES.index(o.touches), flags, ret,
            ", ".join(map(str, ak)), ", ".join(map(str, ak2)), ", ".join(map(str, akw)), o.shown))
    kindname_off = {k: blob.add(O.kind_name(k)) for k in KIND_CODES if k not in ("KW", "OPTKW", "REST", "USER")}
    kindname_off.update({k: 0 for k in ("KW", "OPTKW", "REST", "USER")})
    names = sorted(((n, i) for i, o in enumerate(ops) for n in (o.name,) + o.aliases),
                   key=lambda t: t[0].encode("latin-1"))
    name_rows = ["    {%d, %d, 0}," % (blob.add(n), i) for n, i in names]
    kw_off, kw_names = [0], []
    for lst in kwlists:
        kw_names += [blob.add(n) for n in lst]
        kw_off.append(len(kw_names))
    sets = []
    for attr, doc in LANG_SETS:
        vals = getattr(otype, attr)
        sets.append("/* %s: %s (NUL separated, ended by an empty string) */\n#define OTC_SET_%s %d\n" % (attr, doc, attr, blob.add("\0".join(sorted(vals)) + "\0")))
    nums = "".join("#define OTC_L_%s %d\n" % (n, getattr(otype, n)) for n in LANG_NUMS)
    if len(blob.data) > 65535:
        raise ValueError("string blob over 64 KB")
    L = []
    L.append("/* GENERATED by modules/otype/gen_ops_c.py from modules/otype/ops.py and tools/otype.py: do not edit.")
    L.append(" * Regenerate with: python3 modules/otype/gen_ops_c.py")
    L.append(" * What the compiler's front end (otc_comp.c) needs of the operation registry and of the language: the op")
    L.append(" * records (declaration order = the index the C uses), the names and aliases (sorted by bytes, for a binary")
    L.append(" * search), the keyword lists of the KW arguments, `describe(op)` of the checker's messages, the sets of the")
    L.append(" * language (keywords, variables, ...) and its numeric limits. No pointer in a table: strings are offsets")
    L.append(" * in otc_rstr (NUL terminated). */")
    L.append("#ifndef OTC_OPREG_H")
    L.append("#define OTC_OPREG_H")
    L.append("#include <stdint.h>")
    L.append("")
    L.append("enum {   /* argument kinds: the codes of otc_reg_t.ak and of the akind of a tree node */")
    L.append("    OTC_K_NONE = 0,")
    for i, k in enumerate(KIND_CODES):
        L.append("    OTC_K_%s = %d," % (k, i + 1))
    L.append("    OTC_K_COUNT")
    L.append("};")
    L.append("enum { %s };" % ", ".join("OTC_FAM_%s = %d" % (f, i) for i, f in enumerate(FAMILIES)))
    L.append("enum { %s };" % ", ".join("OTC_TOUCH_%s = %d" % (f, i) for i, f in enumerate(TOUCHES)))
    for i, (n, _f) in enumerate(FLAGS):
        L.append("#define OTC_RF_%-12s %d" % (n, 1 << i))
    L.append("")
    L.append(nums.rstrip("\n"))
    L.append("#define OTC_PROBS_N %d" % len(otype.PROBS))
    pns = sorted(O.NAMESPACES[n] for n in otype.PAGE_FORBIDDEN_NS)      # the namespaces a page function may not use
    if pns != list(range(pns[0], pns[-1] + 1)):
        raise ValueError("PAGE_FORBIDDEN_NS must be contiguous namespaces")
    L.append("#define OTC_PAGE_NS_FIRST %d    /* page functions: no op of the namespaces FILE .. POOL (id >> 8 in this range) */" % pns[0])
    L.append("#define OTC_PAGE_NS_LAST %d" % pns[-1])
    L.append("")
    L.append("typedef struct {")
    L.append("    uint16_t id;                /* (ns << 8) | num, the stable id of the op */")
    L.append("    uint16_t name, shown, desc; /* offsets in otc_rstr: UPPER name, lower-case name, describe(op) */")
    L.append("    uint16_t cost;")
    L.append("    int16_t value;              /* op.value of a constant op (0 if none) */")
    L.append("    uint8_t nargs, minargs;     /* len(op.args), op.min_args */")
    L.append("    uint8_t family, touches, flags, ret;")
    L.append("    uint8_t ak[4];              /* OTC_K_* of each argument (OPTKW, KW, REST, TRACKS, STEPSEL included) */")
    L.append("    uint8_t ak2[4];             /* REST: OTC_K_* of the repeated item */")
    L.append("    uint8_t akw[4];             /* KW, OPTKW, REST of KW: index of the keyword list (0xFF none) */")
    L.append("} otc_reg_t;")
    L.append("")
    L.append("#define OTC_NREG %d" % len(ops))
    L.append("#define OTC_NNAMES %d" % len(names))
    for nm in SPECIAL_OPS:                     # the ops the compiler treats by name (index in otc_reg)
        L.append("#define OTC_OP_%s %d" % (nm, [o.name for o in ops].index(nm)))
    L.append("")
    L.append("static const char otc_rstr[%d] = {" % len(blob.data))
    L.append(_c_bytes(blob.data))
    L.append("};")
    L.append("")
    L.extend(s.rstrip("\n") for s in sets)
    L.append("")
    L.append("static const otc_reg_t otc_reg[OTC_NREG] = {")
    L.extend(rows)
    L.append("};")
    L.append("")
    menu_only = [i for i, o in enumerate(ops) if getattr(o, "menu_only", False)]
    L.append("/* the optional integer arguments whose default is not 0 (Op.OPT_INT): op index, argument index, value */")
    adefs = ["{%d, %d, %d}" % (i, j, k.default)
             for i, o in enumerate(ops) for j, k in enumerate(o.args)
             if isinstance(k, O.OPT_INT) and k.default]
    L.append("#define OTC_NARGDEF %d" % len(adefs))
    L.append("static const struct { uint8_t op, arg; int16_t value; } otc_argdef[%d] = {%s};" % (max(len(adefs), 1), ", ".join(adefs) or "{0, 0, 0}"))
    L.append("")
    L.append("/* ops a live script may not run (Op.menu_only), indexes in otc_reg */")
    L.append("#define OTC_NMENU_ONLY %d" % len(menu_only))
    L.append("static const uint16_t otc_menu_only[%d] = {%s};" % (max(len(menu_only), 1), ", ".join(map(str, menu_only)) or "0"))
    L.append("")
    L.append("typedef struct { uint16_t off; uint8_t op; uint8_t pad; } otc_opname_t;")
    L.append("/* every name and alias, sorted by its bytes */")
    L.append("static const otc_opname_t otc_opname[OTC_NNAMES] = {")
    L.extend(name_rows)
    L.append("};")
    L.append("")
    L.append("#define OTC_NKWL %d" % len(kwlists))
    L.append("static const uint8_t otc_kw_off[OTC_NKWL + 1] = {%s};" % ", ".join(map(str, kw_off)))
    L.append("static const uint16_t otc_kw_names[%d] = {%s};" % (len(kw_names), ", ".join(map(str, kw_names))))
    L.append("static const uint8_t otc_probs[OTC_PROBS_N] = {%s};" % ", ".join(map(str, otype.PROBS)))
    L.append("")
    L.append("/* what the checker knows of an argument kind (index = OTC_K_*): cls = class of a value of that kind")
    L.append(" * (1 number, 2 string, 3 list, 4 condition, 7 string-or-number, 0 none: O.NUMERIC_KINDS / exp_class), the range of constants of")
    L.append(" * the kind (O.RANGES) and the offset in otc_rstr of its name as shown (lower case). */")
    L.append("typedef struct { uint8_t cls, has_range; int16_t lo, hi; uint16_t name; uint16_t pad[2]; } otc_kind_t;   /* 12 bytes: -mcpu=5407 has no index scale of 8 */")
    L.append("static const otc_kind_t otc_kind[OTC_K_COUNT] = {")
    L.append("    {0, 0, 0, 0, 0, {0, 0}},")
    for k in KIND_CODES:
        cls = 1 if k in O.NUMERIC_KINDS else {"STR": 2, "LIST": 3, "COND": 4, "ITEM": 7}.get(k, 0)
        lo, hi = O.RANGES.get(k, (0, 0))
        L.append("    {%d, %d, %d, %d, %d, {0, 0}},   /* %s */" % (cls, 1 if k in O.RANGES else 0, lo, hi, kindname_off[k], k))
    L.append("};")
    L.append("")
    L.append("#endif")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    outs = ((HEADER, render()), (REG_HEADER, render_reg()))
    if a.check:
        for path, text in outs:
            if not path.exists() or path.read_text() != text:
                sys.exit("%s is out of date: run python3 modules/otype/gen_ops_c.py" % path.name)
        return
    for path, text in outs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print("wrote %s: %d operations; wrote %s" % (HEADER, len(O.ALL_OPS), REG_HEADER))


if __name__ == "__main__":
    main()
