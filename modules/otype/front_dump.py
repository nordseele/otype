"""Canonical dump of the OType compiler's FRONT END state.

    front(sources)  ->  tools/otype.Program at the front-end boundary
    dump(prog)      ->  the canonical text the C front end (firmware/c/otc_comp.c) must reproduce
    front_dump(sources) = dump(front(sources))

BOUNDARY.  Everything `Program.run()` does before its checker passes: the loading of every source
(`load_text`, `load_const`, `load_def`; the caps on files and bytes), then `pass_names()` (registration of the
functions and the name clashes: it must run first, the parser needs `registered`, `fmap`, `consts`) and
the `BodyParser` of every registered function.  `find_unused`, `infer_kinds`, `check_all` and the rest are
the checker's.  `front()` copies the first lines of `otype.check_sources` / `Program.run` and does not touch
otype.py; tools/test_otype_ccomp.py checks that the diagnostics of `front()` are a prefix, in the same
order, of the ones of the complete `check_sources`.

FORMAT (one item per line, "\\n" after the last; every text is the one the checker holds, bytes as Latin-1):

    files N                                  the sources that were loaded (refused ones are not here)
    file LABEL
    consts N
    const FILE LINE NAME TYPE VALUE bad=B    every `const` seen, in order; TYPE INT|STR|LIST|COND|-
                                             VALUE 12 | "text" | {1,2,3} | 50% | -
    funcs N
    func FILE LINE NAME reg=R herr=H confirm=C nerr=K blind=B args=A,B,C used=A,B
        reg = registered, herr = has_header_error, nerr = len(errors), args = the argument names in order
        ("-" if none), blind/used = what Program.find_unused needs: for a registered function whose body has
        a line that did not lex, blind=1 used=-; else used = the arguments (same order, duplicates kept)
        that appear as a word in the body ("-" when unregistered or blind)
    param NAME DEFAULT MIN MAX               the params of the function above, in declaration order
    menu LINE levels="a","b" action="x"      the menus of the function above, in order
    page LINE "label"                        the `page` declaration of the function above, if any
    confirm LINE "text"                      its first `confirm "TEXT"` with a non-empty text, if any
    ++L<no> i<indent> e<error> o<opens> f=<first word> m=[MOD,...] d=<decl|-> s=<stmt|->
                                             one line of the body; one '+' per depth (1 = top level)
    diags N
    diag FILE:LINE: FUNC: LEVEL: MESSAGE     (diag! when hidden), in emission order, FUNC as printed
    file_errors N

Expressions are in prefix form, parenthesised:
    (num TEXT VALUE) (str "S") (list TEXT v,v,v) (cond TEXT VALUE) (var N) (arg N) (param N) (const N)
    (op TEXT OPNAME KIND=expr ...) (call NAME USER=expr ...) (kw NAME) (pred TEXT OPNAME)
    (assign N expr) (opstmt TEXT (op ...)) (callstmt TEXT (call ...)) (ctxstmt TEXT (op ...))
    (setstmt TEXT OPNAME expr) (break TEXT) (stop TEXT) (ret TEXT [expr]) (decl TEXT VALUE|-)
    modifiers: (IF e) (ELIF e) (ELSE) (L e e) (EACH) (PROB e) (EVERY e) (SKIP e) (CTX (op ...))
TEXT is the text of the token as the checker keeps it (upper case, except the text of a list, as written).
The spans (columns) are not dumped.  Nothing the checker needs of the tree is left out: the kinds of the
arguments (KIND=), the op behind an alias (OPNAME), the text of every node, the lines.
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


def _label(x):
    return x.decode("latin-1") if isinstance(x, (bytes, bytearray)) else x


def front(sources):
    """sources: list of (label, bytes or str) in card order.  The Program after loading, `pass_names` and the
    body parser (the beginning of `check_sources` + `Program.run`)."""
    prog = otype.Program()
    nbytes, nfiles, stop = 0, 0, None
    for label, data in sources:
        label = _label(label)
        text = data.decode("latin-1") if isinstance(data, (bytes, bytearray)) else data
        if stop is None:
            if nfiles >= otype.MAX_FILES:
                stop = "file not loaded: over %d files" % otype.MAX_FILES
            elif nbytes + len(text) > otype.MAX_SOURCE_BYTES:
                stop = "file not loaded: over %d bytes" % otype.MAX_SOURCE_BYTES
        if stop is not None:
            prog.file_err(label, 0, stop)
            continue
        nfiles += 1
        nbytes += len(text)
        prog.load_text(label, text)
    prog.pass_names()
    for f in prog.funcs:
        if f.registered:
            otype.BodyParser(prog, f).parse()
    return prog


def _expr(n):
    k = n.kind
    if k == "num":
        return "(num %s %d)" % (n.text, n.val)
    if k == "str":
        return '(str "%s")' % n.val
    if k == "list":
        return "(list %s %s)" % (n.text, ",".join(str(v) for v in n.val))
    if k == "cond":
        return "(cond %s %s)" % (n.text, n.val)
    if k == "var_named":
        return "(var_named %s)" % n.name
    if k in ("var", "arg", "param", "const"):
        return "(%s %s)" % (k, n.name)
    if k == "op":
        return "(op %s %s%s)" % (n.text, n.name, "".join(" %s=%s" % (kd, _expr(a))
                                                         for kd, a in zip(n.kinds, n.args)))
    if k == "call":
        return "(call %s%s)" % (n.name, "".join(" %s=%s" % (kd, _expr(a)) for kd, a in zip(n.kinds, n.args)))
    if k == "kw":
        return "(kw %s)" % n.name
    if k == "pred":
        return "(pred %s %s)" % (n.text, n.op.name)
    if k in ("assign", "assign_var_named"):
        return "(%s %s %s)" % (k, n.name, _expr(n.args[0]))
    if k in ("opstmt", "callstmt", "ctxstmt"):
        return "(%s %s %s)" % (k, n.text, _expr(n.node))
    if k == "setstmt":
        return "(setstmt %s %s %s)" % (n.text, n.op.name, " ".join(_expr(a) for a in n.args))
    if k in ("break", "stop"):
        return "(%s %s)" % (k, n.text)
    if k == "ret":
        return "(ret %s%s)" % (n.text, " " + _expr(n.args[0]) if n.args else "")
    if k == "decl":
        return "(decl %s %s)" % (n.text, "-" if n.val is None else n.val)
    raise ValueError("front_dump: node kind %r" % k)


def _mod(m):
    if m.kind == "CTX":
        return "(CTX %s)" % _expr(m.node)
    return "(%s%s)" % (m.kind, "".join(" " + _expr(a) for a in m.args))


def _lines(out, lines, depth):
    for ln in lines:
        out.append("%sL%d i%d e%d o%d f=%s m=[%s] d=%s s=%s" % (
            "+" * depth, ln.no, ln.indent, int(ln.error), int(ln.opens), ln.first_word,
            ",".join(_mod(m) for m in ln.mods),
            "-" if ln.decl is None else _expr(ln.decl),
            "-" if ln.stmt is None else _expr(ln.stmt)))
        _lines(out, ln.children, depth + 1)


def _cval(c):
    if c.type is None:
        return "-"
    if c.type == "INT":
        return "%d" % c.val
    if c.type == "STR":
        return '"%s"' % c.val
    if c.type == "LIST":
        return "{%s}" % ",".join(str(v) for v in c.val)
    return c.val


def word_info(f):
    """(blind, used): what Program.find_unused reads of the body of a registered function."""
    words = set()
    for li in f.lines:
        if li.blank:
            continue
        if li.toks is None:
            return True, None
        words.update(t.text for t in li.toks if t.kind == "WORD")
    return False, [a for a in f.args if a in words]


def dump(prog):
    out = ["files %d" % len(prog.files)]
    out += ["file %s" % lb for lb in prog.files]
    out.append("consts %d" % len(prog.const_list))
    for c in prog.const_list:
        out.append("const %s %d %s %s %s bad=%d" % (c.file, c.line, c.name, c.type or "-", _cval(c), int(c.bad)))
    out.append("funcs %d" % len(prog.funcs))
    for f in prog.funcs:
        blind, used = (False, None)
        if f.registered:
            blind, used = word_info(f)
        out.append("func %s %d %s reg=%d herr=%d confirm=%d nerr=%d blind=%d args=%s used=%s" % (
            f.file, f.line, f.name, int(f.registered), int(f.has_header_error), int(f.confirm), len(f.errors),
            int(blind), ",".join(f.args) or "-", ",".join(used) if used else "-"))
        for name, (d, lo, hi) in f.params.items():
            out.append("param %s %d %d %d" % (name, d, lo, hi))
        for levels, action, line in f.menus:
            out.append('menu %d levels=%s action="%s"' % (line, ",".join('"%s"' % s for s in levels) or "-", action))
        if f.page is not None:
            out.append('page %d "%s"' % (f.page[1], f.page[0]))
        if f.module is not None:
            out.append('module %d "%s"' % (f.module[1], f.module[0]))
        if f.clock is not None:
            out.append('clock %d %d' % (f.clock[1], f.clock[0]))
        if f.confirm_text is not None:
            out.append('confirm %d "%s"' % (f.confirm_text[1], f.confirm_text[0]))
        _lines(out, f.body, 1)
    out.append("diags %d" % len(prog.diags))
    for d in prog.diags:
        out.append("%s %s" % ("diag!" if d.hidden else "diag", d))
    out.append("file_errors %d" % prog.file_errors)
    return "\n".join(out) + "\n"


def front_dump(sources):
    return dump(front(sources))
