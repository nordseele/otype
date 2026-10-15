/* OType compiler, CHECKER passes. Included by otc_comp.c after the front end.
 * Mirrors tools/otype.py Program.run() after the body parser: find_unused, infer_kinds, check_all (check_func,
 * check_node, check_l_bound), pass_graph, pass_needs, pass_cost (compute_ranges, cost_*), pass_flags, and the
 * warning of more than MAX_FUNCS functions. Every table of Python keyed by (function, argument NAME) is an
 * otc_argx_t of the function, found through `canon` (the first argument of that name).
 *
 * Costs are 32-bit unsigned and SATURATE at 0xFFFFFFFF: Python's integers are exact, but every operation (sum,
 * product, max) is monotone and the only uses are `> 4096` and the printed min(cost, 0xFFFFFFFF), so a saturating
 * evaluation gives the same text.
 *
 * Recursion (all bounded, see otc_comp.h): ck_node, cost_expr and ck_walk_expr follow the expression trees
 * (<= OTC_EXPR_DEPTH + 3); ck_cost_block the nested blocks (<= 9 levels in an accepted function; the modifiers of a line
 * and a run of `trk` lines are loops, not recursions); cost_func the calls (<= 9 deep: a function more than 8 calls deep
 * is refused before it is costed). The strongly connected components, the heights, the destructive flags, the kinds and
 * the ranges are iterative. Worst-case stack on the ColdFire (frames of -fstack-usage). */

/* Must-fail builds (tools/test_otype_ccomp.py): -DOTC_SABOTAGE_CHECK (= 1) allows a call depth of 9 instead of 8,
 * -DOTC_SABOTAGE_CHECK=2 adds the costs of the branches of an if / elif chain instead of taking the largest (the
 * comparison with Python's report must fail), =3 reverses the order of emission among diagnostics of one line
 * (otc_comp_core.c), =4 names the LAST card / slot / pool op of a page function instead of the first. Never for a real build. */
#ifdef OTC_SABOTAGE_CHECK
#define CK_SAB OTC_SABOTAGE_CHECK
#else
#define CK_SAB 0
#endif

#define FX(c, i)    (PTR(c, otc_fx_t, (c)->fx) + (i))
#define FNI(c, i)   FN(c, FX(c, i)->func)
#define AXA(c, fx)  PTR(c, otc_argx_t, (fx)->argx)

static uint32_t cc_arg_ref(const otc_comp_t *c, const otc_func_t *f, uint32_t i)     /* the name of argument i */
{
    return PTR(c, uint32_t, f->args)[i] & 0x7FFFFFFFu;
}

/* the first argument of the function `f` called as the node text (upper case): the canonical index, or 0xFFFFFFFF */
static uint32_t ck_find_arg(const otc_comp_t *c, const otc_func_t *f, uint32_t text)
{
    uint32_t i, n;
    const uint8_t *p = cc_sp(c, text, &n);
    for (i = 0; i < f->nargs; i++)
        if (cc_ref_eq(c, cc_arg_ref(c, f, i), p, n, 0))
            return i;
    return 0xFFFFFFFFu;
}

static otc_argx_t *ck_ax(otc_comp_t *c, uint32_t fi, uint32_t canon)       /* the table entry of argument `canon` of function fi */
{
    return AXA(c, FX(c, fi)) + canon;
}

/* index of the function of an otc_func_t offset */
#define FIDX(c, f)  (FN(c, f)->idx)

/* ---- setup ----------------------------------------------------------------------------------------------- */
CC_NOINLINE static int ck_setup(otc_comp_t *c)
{
    uint32_t f, i = 0, n = c->nfuncs, a, b;
    c->nf = n;
    if (!n)
        return 1;
    c->fx = cc_alloc(c, n * (uint32_t)sizeof(otc_fx_t));
    if (!c->fx)
        return 0;
    for (f = c->func_first; f; f = FN(c, f)->next, i++) {
        FN(c, f)->idx = i;
        FX(c, i)->func = f;
        FX(c, i)->grank = 0xFFFFFFFFu;
    }
    for (i = 0; i < n && !c->err; i++) {
        otc_func_t *fn = FNI(c, i);
        otc_argx_t *ax;
        if (!(fn->flags & OTC_FF_REG) || !fn->nargs)
            continue;
        FX(c, i)->argx = cc_alloc(c, fn->nargs * (uint32_t)sizeof(otc_argx_t));
        if (!FX(c, i)->argx)
            return 0;
        ax = AXA(c, FX(c, i));
        for (a = 0; a < fn->nargs; a++) {
            uint32_t an;
            const uint8_t *ap = cc_sp(c, cc_arg_ref(c, fn, a), &an);
            ax[a].canon = a;
            for (b = 0; b < a; b++)
                if (ax[b].canon == b && cc_ref_eq(c, cc_arg_ref(c, fn, b), ap, an, 0)) {
                    ax[a].canon = b;
                    break;
                }
            ax[a].isconst = 1;
        }
    }
    return c->err == 0;
}

/* ---- walking the trees ----------------------------------------------------------------------------------- */
typedef void (*ck_visit_fn)(otc_comp_t *c, void *ctx, uint32_t node, uint32_t line);

static void ck_walk_expr(otc_comp_t *c, uint32_t n, uint32_t line, ck_visit_fn fn, void *ctx)
{
    uint32_t a;
    fn(c, ctx, n, line);
    for (a = ND(c, n)->first; a; a = ND(c, a)->next)
        ck_walk_expr(c, a, line, fn, ctx);
}

/* every expression / statement of the body of function `fn` in the order of Python's line_roots: per line (preorder)
 * the arguments and the node of each modifier, then the statement */
static void ck_walk_func(otc_comp_t *c, uint32_t fn, ck_visit_fn visit, void *ctx)
{
    uint32_t l, m, a;
    for (l = FN(c, fn)->root_first; l && !c->err; l = cc_preorder_next(c, l)) {
        const otc_line_t *ln = LN(c, l);
        for (m = ln->mods; m; m = ND(c, m)->next)
            for (a = ND(c, m)->first; a; a = ND(c, a)->next)
                ck_walk_expr(c, a, ln->no, visit, ctx);
        if (ln->body && !(ln->flags & OTC_LF_DECL))
            ck_walk_expr(c, ln->body, ln->no, visit, ctx);
    }
}

static uint32_t ck_first_call_target(otc_comp_t *c, uint32_t node)      /* the function a call node calls (offset) or 0 */
{
    uint32_t n;
    const uint8_t *p = cc_sp(c, ND(c, node)->text, &n);
    return cc_fmap_find(c, p, n, 0);
}

/* ---- find_unused ------------------------------------------------------------------------------------------- */
static void ck_find_unused(otc_comp_t *c)
{
    uint32_t i, a;
    for (i = 0; i < c->nf && !c->err; i++) {
        otc_func_t *f = FNI(c, i);
        otc_argx_t *ax;
        if (!(f->flags & OTC_FF_REG) || !f->nargs || (f->flags & OTC_FF_BLIND))
            continue;
        ax = AXA(c, FX(c, i));
        for (a = 0; a < f->nargs; a++) {
            if (ax[a].canon != a || (PTR(c, uint32_t, f->args)[a] & 0x80000000u))
                continue;
            ax[a].unused = 1;
            mb_begin(c);
            mb_lit(c, "argument ");
            mb_show_ref(c, cc_arg_ref(c, f, a));
            mb_lit(c, " is unused");
            cc_func_warn(c, FX(c, i)->func, f->line, mb_end(c));
        }
    }
}

/* ---- check_func / check_node ------------------------------------------------------------------------------- */
typedef struct {
    otc_comp_t *c;
    uint32_t fn, fi;              /* the function (offset, index) */
    int rep;                      /* report (check_all) or only collect (infer_kinds) */
    uint32_t line;                /* the line being checked */
} ck_t;

static const char *const ck_want[8] = {"?", "a number", "a string", "a list", "a condition", "?", "?", "a string or a number"};

static uint32_t ck_const_class(const otc_const_t *cn)
{
    switch (cn->type) {
    case OTC_CT_INT: return CL_INT;
    case OTC_CT_STR: return CL_STR;
    case OTC_CT_LIST: return CL_LIST;
    case OTC_CT_COND: return CL_COND;
    default: return CL_NONE;
    }
}

static uint32_t ck_ntype(ck_t *k, const otc_node_t *nd)
{
    otc_comp_t *c = k->c;
    switch (nd->kind) {
    case OTC_N_NUM:
    case OTC_N_VAR:
    case OTC_N_OP:
    case OTC_N_CALL:
        return CL_INT;
    case OTC_N_PARAM: {
        uint32_t q, n;
        const uint8_t *p = cc_sp(c, nd->text, &n);
        for (q = FN(c, k->fn)->param_first; q; q = PTR(c, otc_param_t, q)->next) {
            if (cc_ref_eq(c, PTR(c, otc_param_t, q)->name, p, n, 0)) {
                if (PTR(c, otc_param_t, q)->kind == OTC_PK_FOLDER)
                    return CL_FOLDER;
                break;
            }
        }
        return CL_INT;
    }
    case OTC_N_CONST: {
        uint32_t n, kc;
        const uint8_t *p = cc_sp(c, nd->text, &n);
        kc = cc_cmap_find(c, p, n, 0);
        return kc ? ck_const_class(CN(c, kc)) : CL_NONE;
    }
    case OTC_N_STR: return CL_STR;
    case OTC_N_LIST: return CL_LIST;
    case OTC_N_COND: return CL_COND;
    case OTC_N_ARG: {
        uint32_t a = ck_find_arg(c, FN(c, k->fn), nd->text);
        return a == 0xFFFFFFFFu ? CL_NONE : ck_ax(c, k->fi, a)->kind;
    }
    default:
        return CL_NONE;
    }
}

/* type_msg(): the message of a mismatch, into the message buffer (not ended) */
static void ck_type_msg(otc_comp_t *c, const otc_node_t *nd, uint32_t have, uint32_t want)
{
    uint32_t n, i;
    const uint8_t *p = cc_sp(c, nd->text, &n);
    if (have == CL_FOLDER && want == CL_INT) {
        mb_lit(c, "folder param ");
        mb_show(c, p, n, 0);
        mb_lit(c, " cannot be used in arithmetic");
        return;
    }
    if (have == CL_FOLDER) {
        mb_lit(c, "folder param ");
        mb_show(c, p, n, 0);
        mb_lit(c, " cannot be used here");
        return;
    }
    if (have == CL_COND && want == CL_INT) {
        for (i = 0; i + 1u < n && cc_dig(p[i]); i++)
            ;
        if (n >= 2u && i + 1u == n && p[n - 1u] == '%') {          /* ([0-9]+)% */
            mb_bytes(c, p, n);
            mb_lit(c, " is a trig condition; write prob ");
            mb_bytes(c, p, n - 1u);
            mb_byte(c, ':');
        } else {
            mb_show(c, p, n, 0);
            mb_lit(c, " is a trig condition, not a number");
        }
        return;
    }
    if (nd->kind == OTC_N_ARG) {
        mb_lit(c, "argument ");
        mb_show(c, p, n, 0);
        mb_lit(c, " is used as ");
        mb_lit(c, ck_want[have <= CL_ITEM ? have : 0]);
        mb_lit(c, " elsewhere, but ");
        mb_lit(c, ck_want[want <= CL_ITEM ? want : 0]);
        mb_lit(c, " is expected here");
        return;
    }
    mb_lit(c, "expected ");
    mb_lit(c, ck_want[want <= CL_ITEM ? want : 0]);
    mb_lit(c, ", got ");
    mb_lit(c, ck_want[have <= CL_ITEM ? have : 0]);
    mb_lit(c, " (");
    if (nd->kind == OTC_N_STR) {                                    /* Python's text of a string node is "content": show() keeps it */
        mb_byte(c, '"');
        mb_bytes(c, p, n);
        mb_byte(c, '"');
    } else {
        mb_show(c, p, n, 0);
    }
    mb_byte(c, ')');
}

static void ck_emit_type_err(otc_comp_t *c, uint32_t fn, uint32_t line, uint32_t node, uint32_t have, uint32_t want)
{
    mb_begin(c);
    ck_type_msg(c, ND(c, node), have, want);
    cc_func_err(c, fn, line, mb_end(c), 0);
}

static uint32_t ck_nargs_of(const otc_comp_t *c, uint32_t fnoff)
{
    return FN(c, fnoff)->nargs;
}

/* msg items (Python: Checker.msg_items / check_msg): 1 if the item is a string; *len its length (0 for a number or an argument) */
static int ck_item_str(otc_comp_t *c, uint32_t fn, uint32_t fi, const otc_node_t *a, uint32_t *len)
{
    *len = 0;
    if (a->kind == OTC_N_STR) {
        (void)cc_sp(c, a->text, len);
        return 1;
    }
    if (a->kind == OTC_N_CONST) {
        uint32_t tn, kc;
        const uint8_t *tp = cc_sp(c, a->text, &tn);
        kc = cc_cmap_find(c, tp, tn, 0);
        if (kc && CN(c, kc)->type == OTC_CT_STR) {
            (void)cc_sp(c, CN(c, kc)->val, len);
            return 1;
        }
        return 0;
    }
    if (a->kind == OTC_N_ARG) {
        uint32_t pos = ck_find_arg(c, FN(c, fn), a->text);
        return pos != 0xFFFFFFFFu && ck_ax(c, fi, pos)->kind == CL_STR;
    }
    return 0;
}

static void ck_msg(ck_t *k, uint32_t n)
{
    otc_comp_t *c = k->c;
    const otc_node_t *nd = ND(c, n);
    uint32_t a, items = 0, w = 0, len;
    for (a = nd->first; a; a = ND(c, a)->next)
        items++;
    if (items > OTC_L_MAX_MSG_ITEMS) {
        mb_begin(c);
        mb_lit(c, "msg takes at most ");
        mb_dec(c, OTC_L_MAX_MSG_ITEMS);
        mb_lit(c, " items (got ");
        mb_dec(c, (int32_t)items);
        mb_byte(c, ')');
        cc_func_err(c, k->fn, k->line, mb_end(c), 0);
        return;
    }
    w = items - 1u;
    for (a = nd->first; a; a = ND(c, a)->next)
        w += ck_item_str(c, k->fn, k->fi, ND(c, a), &len) ? len : 6u;
    if (w > OTC_L_MAX_MSG) {
        mb_begin(c);
        mb_lit(c, "msg is too long for the screen: ");
        mb_dec(c, (int32_t)w);
        mb_lit(c, " characters in the worst case (a number counts as 6, items are separated by a space), at most ");
        mb_dec(c, OTC_L_MAX_MSG);
        mb_lit(c, " fit");
        cc_func_err(c, k->fn, k->line, mb_end(c), 0);
    }
}

/* exp: OTC_K_* of the expected kind (0 = none; OTC_K_USER: an argument of a call of `callee`, the k-th) */
static void ck_node(ck_t *k, uint32_t n, uint32_t exp, uint32_t callee, uint32_t argi, uint32_t op_idx)
{
    otc_comp_t *c = k->c;
    const otc_node_t *nd = ND(c, n);
    uint32_t a, i, have, ecls;
    int strict, ok;
    if (nd->kind == OTC_N_KW || nd->kind == OTC_N_PRED)
        return;
    if (nd->kind == OTC_N_OP) {
        for (a = nd->first; a; a = ND(c, a)->next)
            ck_node(k, a, ND(c, a)->akind, 0, 0, nd->op);
        if (k->rep && nd->op == OTC_OP_MSG)
            ck_msg(k, n);
    } else if (nd->kind == OTC_N_CALL) {
        uint32_t g = ck_first_call_target(c, n);
        for (a = nd->first, i = 0; a; a = ND(c, a)->next, i++)
            ck_node(k, a, OTC_K_USER, g, i, 0xFFFFFFFFu);
    }
    nd = ND(c, n);
    if (nd->kind == OTC_N_ARG && !k->rep) {
        uint32_t pos = ck_find_arg(c, FN(c, k->fn), nd->text);
        if (pos == 0xFFFFFFFFu)
            return;
        if (exp == OTC_K_USER) {
            uint32_t ed = cc_alloc(c, (uint32_t)sizeof(otc_ed_t));
            if (!ed)
                return;
            PTR(c, otc_ed_t, ed)->g = callee;
            PTR(c, otc_ed_t, ed)->k = argi;
            PTR(c, otc_ed_t, ed)->next = ck_ax(c, k->fi, pos)->efirst;
            ck_ax(c, k->fi, pos)->efirst = ed;
        } else if (exp) {
            ck_ax(c, k->fi, pos)->uses = (uint8_t)(ck_ax(c, k->fi, pos)->uses | (1u << otc_kind[exp].cls));
        }
        return;
    }
    if (!k->rep || !exp)
        return;
    have = ck_ntype(k, nd);
    strict = exp == OTC_K_USER;
    if (strict) {
        if (!callee || argi >= ck_nargs_of(c, callee))
            return;
        {
            otc_argx_t *cax = ck_ax(c, FIDX(c, callee), 0);
            ecls = cax[cax[argi].canon].kind;
            if (!ecls)
                ecls = CL_INT;
        }
    } else {
        ecls = otc_kind[exp].cls;
    }
    if (have == CL_FOLDER) {
        ok = (op_idx == OTC_OP_DIR && ecls == CL_STR);
    } else {
        ok = (have == ecls || (ecls == CL_COND && !strict && have == CL_INT) || ecls == CL_ANY || have == CL_ANY
              || (ecls == CL_ITEM && (have == CL_INT || have == CL_STR)));
    }
    if (!ok) {
        if (strict) {                 /* a callee refused for another reason has unreliable kinds: decide later */
            uint32_t d = cc_alloc(c, (uint32_t)sizeof(otc_defer_t));
            if (!d)
                return;
            PTR(c, otc_defer_t, d)->f = k->fn;
            PTR(c, otc_defer_t, d)->callee = callee;
            PTR(c, otc_defer_t, d)->node = n;
            PTR(c, otc_defer_t, d)->line = k->line;
            PTR(c, otc_defer_t, d)->have = (uint8_t)have;
            PTR(c, otc_defer_t, d)->want = (uint8_t)ecls;
            if (c->defer_last)
                PTR(c, otc_defer_t, c->defer_last)->next = d;
            else
                c->defer_first = d;
            c->defer_last = d;
        } else {
            ck_emit_type_err(c, k->fn, k->line, n, have, ecls);
        }
        return;
    }
    if (!strict && otc_kind[exp].has_range) {
        int have_v = 0;
        int32_t v = 0;
        if (nd->kind == OTC_N_NUM) {
            v = (int32_t)nd->val;
            have_v = 1;
        } else if (nd->kind == OTC_N_CONST) {
            uint32_t tn, kc;
            const uint8_t *tp = cc_sp(c, nd->text, &tn);
            kc = cc_cmap_find(c, tp, tn, 0);
            if (kc && CN(c, kc)->type == OTC_CT_INT) {
                v = (int32_t)CN(c, kc)->val;
                have_v = 1;
            }
        }
        if (have_v && !(otc_kind[exp].lo <= v && v <= otc_kind[exp].hi)) {
            mb_begin(c);
            mb_dec(c, v);
            mb_lit(c, " is out of range for ");
            mb_lit(c, &otc_rstr[otc_kind[exp].name]);
            mb_lit(c, " (");
            mb_dec(c, otc_kind[exp].lo);
            mb_lit(c, "..");
            mb_dec(c, otc_kind[exp].hi);
            mb_byte(c, ')');
            cc_func_err(c, k->fn, k->line, mb_end(c), 0);
        }
    }
}

/* is_const(f, n): a number, a param, an integer const, or an argument that is constant at every call */
static int ck_is_const(otc_comp_t *c, uint32_t fi, uint32_t n)
{
    const otc_node_t *nd = ND(c, n);
    if (nd->kind == OTC_N_NUM || nd->kind == OTC_N_PARAM)
        return 1;
    if (nd->kind == OTC_N_CONST) {
        uint32_t tn, kc;
        const uint8_t *tp = cc_sp(c, nd->text, &tn);
        kc = cc_cmap_find(c, tp, tn, 0);
        return kc && CN(c, kc)->type == OTC_CT_INT;
    }
    if (nd->kind == OTC_N_ARG) {
        uint32_t a = ck_find_arg(c, FNI(c, fi), nd->text);
        return a != 0xFFFFFFFFu && ck_ax(c, fi, a)->isconst;
    }
    return 0;
}

static void ck_l_bound(ck_t *k, uint32_t n, uint32_t mline)
{
    otc_comp_t *c = k->c;
    const otc_node_t *nd = ND(c, n);
    uint32_t tn, kc;
    const uint8_t *tp = cc_sp(c, nd->text, &tn);
    if (nd->kind == OTC_N_PARAM) {
        uint32_t q;
        for (q = FN(c, k->fn)->param_first; q; q = PTR(c, otc_param_t, q)->next) {
            if (cc_ref_eq(c, PTR(c, otc_param_t, q)->name, tp, tn, 0)) {
                if (PTR(c, otc_param_t, q)->kind == OTC_PK_FOLDER) {
                    mb_begin(c);
                    mb_lit(c, "folder param ");
                    mb_show(c, tp, tn, 0);
                    mb_lit(c, " cannot be used as loop bound");
                    cc_func_err(c, k->fn, mline, mb_end(c), 0);
                }
                break;
            }
        }
        return;
    }
    if (nd->kind == OTC_N_NUM)
        return;
    if (nd->kind == OTC_N_CONST) {
        kc = cc_cmap_find(c, tp, tn, 0);
        if (kc && CN(c, kc)->type == OTC_CT_INT)
            return;
    }
    if (nd->kind == OTC_N_ARG) {
        uint32_t pos = ck_find_arg(c, FN(c, k->fn), nd->text), s, hint = 0;
        otc_fx_t *fx = FX(c, k->fi);
        if (pos == 0xFFFFFFFFu || ck_ax(c, k->fi, pos)->isconst)
            return;
        for (s = 0; s < fx->gcnt; s++) {
            const otc_site_t *st = PTR(c, otc_site_t, c->sites) + PTR(c, uint32_t, c->sorder)[fx->gstart + s];
            uint32_t a, j;
            for (a = ND(c, st->node)->first, j = 0; a && j < pos; a = ND(c, a)->next, j++)
                ;
            if (a && !ck_is_const(c, st->f, a)) {
                hint = s + 1u;
                break;
            }
        }
        mb_begin(c);
        mb_lit(c, "l bound must be constant (");
        mb_show(c, tp, tn, 0);
        mb_byte(c, ')');
        if (hint) {
            const otc_site_t *st = PTR(c, otc_site_t, c->sites) + PTR(c, uint32_t, c->sorder)[fx->gstart + hint - 1u];
            const otc_func_t *cf = FNI(c, st->f);
            uint32_t cn;
            const uint8_t *cp = cc_sp(c, cf->name, &cn);
            mb_lit(c, "; ");
            mb_show(c, cp, cn, 0);
            mb_lit(c, " passes a non-constant value at ");
            mb_label(c, cf->file);
            mb_byte(c, ':');
            mb_dec(c, (int32_t)st->line);
        }
        cc_func_err(c, k->fn, mline, mb_end(c), 0);
        return;
    }
    if (nd->kind == OTC_N_STR || nd->kind == OTC_N_LIST || nd->kind == OTC_N_COND || nd->kind == OTC_N_KW)
        return;                                                    /* type error already reported */
    mb_begin(c);
    mb_lit(c, "l bound must be constant (");
    if (nd->kind == OTC_N_STR) {
        mb_byte(c, '"');
        mb_bytes(c, tp, tn);
        mb_byte(c, '"');
    } else {
        mb_show(c, tp, tn, 0);
    }
    mb_lit(c, "): use a number, a const, a param or an argument that is constant at every call");
    cc_func_err(c, k->fn, mline, mb_end(c), 0);
}

CC_NOINLINE static void ck_check_func(otc_comp_t *c, uint32_t fi, int rep)
{
    ck_t k;
    uint32_t l, depth = 0;
    k.c = c;
    k.fn = FX(c, fi)->func;
    k.fi = fi;
    k.rep = rep;
    k.line = 0;
    l = FN(c, k.fn)->root_first;
    while (l && !c->err) {
        const otc_line_t *ln = LN(c, l);
        uint32_t lp = depth ? c->lps[depth - 1u] : 0, m;
        k.line = ln->no;
        for (m = ln->mods; m; m = ND(c, m)->next) {
            uint8_t mk = ND(c, m)->kind;
            uint32_t a = ND(c, m)->first;
            if (mk == OTC_N_M_IF || mk == OTC_N_M_ELIF || mk == OTC_N_M_PROB || mk == OTC_N_M_EVERY || mk == OTC_N_M_SKIP) {
                ck_node(&k, a, OTC_K_INT, 0, 0, 0xFFFFFFFFu);
            } else if (mk == OTC_N_M_L) {
                for (; a; a = ND(c, a)->next) {
                    ck_node(&k, a, OTC_K_INT, 0, 0, 0xFFFFFFFFu);
                    if (rep)
                        ck_l_bound(&k, a, ln->no);
                }
                lp++;
            } else if (mk == OTC_N_M_EACH) {
                lp++;
            } else if (mk == OTC_N_M_CTX) {
                ck_node(&k, a, 0, 0, 0, 0xFFFFFFFFu);
            }
        }
        if (ln->body && !(ln->flags & OTC_LF_DECL)) {
            const otc_node_t *s = ND(c, ln->body);
            uint8_t sk = s->kind;
            if (sk == OTC_N_ASSIGN) {
                ck_node(&k, s->first, OTC_K_INT, 0, 0, 0xFFFFFFFFu);
            } else if (sk == OTC_N_SETSTMT) {
                const otc_reg_t *r = &otc_reg[s->op];
                uint32_t a = s->first, i;
                for (i = 0; i < r->nargs && a; i++, a = CPTR(c, otc_node_t, a)->next) {
                    ck_node(&k, a, r->ak[i], 0, 0, 0xFFFFFFFFu);
                }
                if (a) {
                    uint8_t ret_k = r->ret ? r->ret : OTC_K_INT;
                    ck_node(&k, a, ret_k, 0, 0, 0xFFFFFFFFu);
                }
            } else if (sk == OTC_N_OPSTMT || sk == OTC_N_CALLSTMT || sk == OTC_N_CTXSTMT) {
                ck_node(&k, s->first, 0, 0, 0, 0xFFFFFFFFu);
                if (rep && sk == OTC_N_OPSTMT && (otc_reg[ND(c, s->first)->op].flags & OTC_RF_RET)) {
                    mb_begin(c);
                    mb_lit(c, "the value of ");
                    mb_rstr(c, otc_reg[ND(c, s->first)->op].shown);
                    mb_lit(c, " is ignored");
                    cc_func_warn(c, k.fn, ln->no, mb_end(c));
                }
            } else if (sk == OTC_N_RET) {
                if (s->first)
                    ck_node(&k, s->first, OTC_K_INT, 0, 0, 0xFFFFFFFFu);
            } else if (sk == OTC_N_BREAK && rep && lp == 0) {
                mb_begin(c);
                mb_lit(c, "break outside l or each");
                cc_func_err(c, k.fn, ln->no, mb_end(c), 0);
            }
        }
        c->lps[depth] = lp;
        if (ln->child_first) {
            l = ln->child_first;
            depth++;
            continue;
        }
        for (;;) {
            if (LN(c, l)->next) {
                l = LN(c, l)->next;
                break;
            }
            l = LN(c, l)->parent;
            if (!l)
                break;
            depth--;
        }
    }
}

/* ---- call sites --------------------------------------------------------------------------------------------- */
typedef struct {
    uint32_t fi, n;
    int fill;
} cs_t;

static void ck_site_visit(otc_comp_t *c, void *ctx, uint32_t node, uint32_t line)
{
    cs_t *cs = (cs_t *)ctx;
    uint32_t g;
    if (ND(c, node)->kind != OTC_N_CALL)
        return;
    g = ck_first_call_target(c, node);
    if (!g)
        return;
    if (cs->fill) {
        otc_site_t *st = PTR(c, otc_site_t, c->sites) + cs->n;
        otc_fx_t *gx = FX(c, FIDX(c, g));
        st->f = cs->fi;
        st->node = node;
        st->line = line;
        st->g = FIDX(c, g);
        if (gx->grank == 0xFFFFFFFFu) {
            gx->grank = c->ngroups;
            PTR(c, uint32_t, c->gorder)[c->ngroups++] = FIDX(c, g);
        }
    }
    cs->n++;
}

CC_NOINLINE static int ck_build_sites(otc_comp_t *c)
{
    cs_t cs;
    uint32_t i, r, pos = 0;
    cs.n = 0;
    cs.fill = 0;
    for (i = 0; i < c->nf && !c->err; i++)
        if (FNI(c, i)->flags & OTC_FF_REG) {
            cs.fi = i;
            ck_walk_func(c, FX(c, i)->func, ck_site_visit, &cs);
        }
    c->nsites = cs.n;
    if (!cs.n)
        return !c->err;
    c->sites = cc_alloc(c, cs.n * (uint32_t)sizeof(otc_site_t));
    c->sorder = cc_alloc(c, cs.n * 4u);
    c->gorder = cc_alloc(c, cs.n * 4u);
    if (!c->sites || !c->sorder || !c->gorder)
        return 0;
    cs.n = 0;
    cs.fill = 1;
    c->ngroups = 0;
    for (i = 0; i < c->nf && !c->err; i++)
        if (FNI(c, i)->flags & OTC_FF_REG) {
            cs.fi = i;
            ck_walk_func(c, FX(c, i)->func, ck_site_visit, &cs);
        }
    for (i = 0; i < c->nsites; i++)
        FX(c, PTR(c, otc_site_t, c->sites)[i].g)->gcnt++;
    for (r = 0; r < c->ngroups; r++) {                              /* groups in order of first appearance */
        FX(c, PTR(c, uint32_t, c->gorder)[r])->gstart = pos;
        pos += FX(c, PTR(c, uint32_t, c->gorder)[r])->gcnt;
        FX(c, PTR(c, uint32_t, c->gorder)[r])->gcnt = 0;
    }
    for (i = 0; i < c->nsites; i++) {
        otc_fx_t *gx = FX(c, PTR(c, otc_site_t, c->sites)[i].g);
        PTR(c, uint32_t, c->sorder)[gx->gstart + gx->gcnt++] = i;
    }
    return c->err == 0;
}

/* ---- infer_kinds ----------------------------------------------------------------------------------------------- */
CC_NOINLINE static void ck_infer_kinds(otc_comp_t *c)
{
    uint32_t i, a, it, r, s;
    int have_kinds = 0, changed;
    for (i = 0; i < c->nf && !c->err; i++)
        if (FNI(c, i)->flags & OTC_FF_REG)
            ck_check_func(c, i, 0);
    for (it = 0; it < 40u && !c->err; it++) {
        changed = 0;
        for (i = 0; i < c->nf; i++) {
            const otc_func_t *f = FNI(c, i);
            otc_argx_t *ax;
            if (!(f->flags & OTC_FF_REG) || !f->nargs)
                continue;
            ax = AXA(c, FX(c, i));
            for (a = 0; a < f->nargs; a++) {
                uint32_t u, e;
                uint8_t nk;
                if (ax[a].canon != a)
                    continue;
                if (ax[a].unused) {
                    nk = CL_ANY;
                } else {
                    u = ax[a].uses;
                    for (e = ax[a].efirst; e; e = PTR(c, otc_ed_t, e)->next) {
                        uint32_t g = PTR(c, otc_ed_t, e)->g, kk;
                        otc_argx_t *gx;
                        if (!g || PTR(c, otc_ed_t, e)->k >= FN(c, g)->nargs)
                            continue;
                        gx = AXA(c, FX(c, FIDX(c, g)));
                        kk = gx[gx[PTR(c, otc_ed_t, e)->k].canon].kind;
                        if (kk && kk != CL_ANY)
                            u |= 1u << kk;
                    }
                    nk = (u & (1u << CL_STR)) ? CL_STR : (u & (1u << CL_LIST)) ? CL_LIST : (u & (1u << CL_INT)) ? CL_INT
                       : (u & (1u << CL_COND)) ? CL_COND : CL_INT;
                }
                ax[a].newkind = nk;
                if (!have_kinds || ax[a].kind != nk)
                    changed = 1;
            }
        }
        if (!changed)
            break;
        for (i = 0; i < c->nf; i++) {
            const otc_func_t *f = FNI(c, i);
            otc_argx_t *ax;
            if (!(f->flags & OTC_FF_REG) || !f->nargs)
                continue;
            ax = AXA(c, FX(c, i));
            for (a = 0; a < f->nargs; a++)
                if (ax[a].canon == a)
                    ax[a].kind = ax[a].newkind;
        }
        have_kinds = 1;
    }
    /* constness of arguments at every call site (greatest fixpoint) */
    do {
        changed = 0;
        for (r = 0; r < c->ngroups; r++) {
            uint32_t gi = PTR(c, uint32_t, c->gorder)[r];
            const otc_fx_t *gfx = FX(c, gi);
            for (s = 0; s < gfx->gcnt; s++) {
                const otc_site_t *st = PTR(c, otc_site_t, c->sites) + PTR(c, uint32_t, c->sorder)[gfx->gstart + s];
                uint32_t arg, kx = 0;
                otc_argx_t *gx = AXA(c, FX(c, gi));
                for (arg = ND(c, st->node)->first; arg; arg = ND(c, arg)->next, kx++) {
                    if (kx >= FNI(c, gi)->nargs)
                        break;
                    if (gx[gx[kx].canon].isconst && !ck_is_const(c, st->f, arg)) {
                        gx[gx[kx].canon].isconst = 0;
                        changed = 1;
                    }
                }
            }
        }
    } while (changed && !c->err);
}

/* ---- check_all ---------------------------------------------------------------------------------------------- */
CC_NOINLINE static void ck_check_all(otc_comp_t *c)
{
    uint32_t i, d;
    for (i = 0; i < c->nf && !c->err; i++)
        if (FNI(c, i)->flags & OTC_FF_REG)
            ck_check_func(c, i, 1);
    for (d = c->defer_first; d && !c->err; d = PTR(c, otc_defer_t, d)->next) {
        const otc_defer_t *df = PTR(c, otc_defer_t, d);
        if (!FN(c, df->callee)->nerr)
            ck_emit_type_err(c, df->f, df->line, df->node, df->have, df->want);
    }
}

/* ---- pass_graph ------------------------------------------------------------------------------------------------ */
typedef struct {
    uint32_t fi, n, edges;
    int fill;
} cg_t;

static void ck_cg_visit(otc_comp_t *c, void *ctx, uint32_t node, uint32_t line)
{
    cg_t *cg = (cg_t *)ctx;
    uint32_t g;
    otc_fx_t *gx;
    if (ND(c, node)->kind != OTC_N_CALL)
        return;
    g = ck_first_call_target(c, node);
    if (!g)
        return;
    gx = FX(c, FIDX(c, g));
    if (gx->stamp == c->gen)
        return;
    gx->stamp = c->gen;
    if (cg->fill) {
        otc_edge_t *e = PTR(c, otc_edge_t, FX(c, cg->fi)->cg) + cg->n;
        e->g = FIDX(c, g);
        e->line = line;
    }
    cg->n++;
}

/* scratch frames of the iterative walks: function, next edge, best so far */
typedef struct {
    uint32_t v, e, best;
} ck_frame_t;

CC_NOINLINE static int ck_graph(otc_comp_t *c)
{
    uint32_t i, counter = 0, sccn = 0, sp = 0, fp = 0, f;
    uint32_t *stk;
    ck_frame_t *fr;
    cg_t cg;
    if (!c->nf)
        return 1;
    for (i = 0; i < c->nf && !c->err; i++) {            /* callees(f): unique functions in order of first call */
        if (!(FNI(c, i)->flags & OTC_FF_REG))
            continue;
        cg.fi = i;
        cg.n = 0;
        cg.fill = 0;
        c->gen++;
        ck_walk_func(c, FX(c, i)->func, ck_cg_visit, &cg);
        FX(c, i)->ncg = cg.n;
        if (cg.n) {
            FX(c, i)->cg = cc_alloc(c, cg.n * (uint32_t)sizeof(otc_edge_t));
            if (!FX(c, i)->cg)
                return 0;
            cg.n = 0;
            cg.fill = 1;
            c->gen++;
            ck_walk_func(c, FX(c, i)->func, ck_cg_visit, &cg);
        }
    }
    f = cc_alloc(c, c->nf * (uint32_t)sizeof(uint32_t));
    {
        uint32_t fo = cc_alloc(c, c->nf * (uint32_t)sizeof(ck_frame_t));
        if (!f || !fo)
            return 0;
        stk = PTR(c, uint32_t, f);
        fr = PTR(c, ck_frame_t, fo);
    }
    /* Tarjan (iterative); a component is complete when its root returns: the loop errors come in that order */
    for (i = 0; i < c->nf && !c->err; i++) {
        if (!(FNI(c, i)->flags & OTC_FF_REG) || FX(c, i)->tidx)
            continue;
        FX(c, i)->tidx = FX(c, i)->tlow = ++counter;
        stk[sp++] = i;
        FX(c, i)->onstack = 1;
        fr[0].v = i;
        fr[0].e = 0;
        fp = 1;
        while (fp) {
            ck_frame_t *t = &fr[fp - 1u];
            otc_fx_t *vx = FX(c, t->v);
            if (t->e < vx->ncg) {
                uint32_t w = PTR(c, otc_edge_t, vx->cg)[t->e++].g;
                otc_fx_t *wx = FX(c, w);
                if (!wx->tidx) {
                    wx->tidx = wx->tlow = ++counter;
                    stk[sp++] = w;
                    wx->onstack = 1;
                    fr[fp].v = w;
                    fr[fp].e = 0;
                    fp++;
                } else if (wx->onstack && wx->tidx < vx->tlow) {
                    vx->tlow = wx->tidx;
                }
                continue;
            }
            if (vx->tlow == vx->tidx) {                           /* the root of a component: pop it */
                uint32_t first = sp, m, self = 0;
                sccn++;
                while (first && stk[first - 1u] != t->v)
                    first--;
                first--;                                          /* stk[first .. sp) = the members, the root first */
                if (sp - first == 1u) {
                    uint32_t e;
                    for (e = 0; e < vx->ncg; e++)
                        if (PTR(c, otc_edge_t, vx->cg)[e].g == t->v)
                            self = 1;
                }
                for (m = sp; m > first; m--) {                    /* pop order: the last pushed first */
                    otc_fx_t *mx = FX(c, stk[m - 1u]);
                    mx->onstack = 0;
                    mx->scc = sccn;
                }
                if (sp - first > 1u || self)
                    for (m = sp; m > first; m--) {
                        otc_fx_t *mx = FX(c, stk[m - 1u]);
                        mx->cyclic = 1;
                        cc_func_err_lazy(c, mx->func, FN(c, mx->func)->line, OTC_LK_LOOP, 0);
                    }
                sp = first;
            }
            fp--;
            if (fp) {
                otc_fx_t *px = FX(c, fr[fp - 1u].v);
                if (vx->tlow < px->tlow)
                    px->tlow = vx->tlow;
            }
        }
    }
    /* heights: the longest chain of non-cyclic functions (the first callee that gives the maximum) */
    for (i = 0; i < c->nf && !c->err; i++) {
        if (!(FNI(c, i)->flags & OTC_FF_REG) || FX(c, i)->cyclic || FX(c, i)->height)
            continue;
        fr[0].v = i;
        fr[0].e = 0;
        fr[0].best = 0;
        fp = 1;
        while (fp) {
            ck_frame_t *t = &fr[fp - 1u];
            otc_fx_t *vx = FX(c, t->v);
            int pushed = 0;
            while (t->e < vx->ncg) {
                uint32_t g = PTR(c, otc_edge_t, vx->cg)[t->e].g;
                otc_fx_t *gx = FX(c, g);
                if (gx->cyclic) {
                    t->e++;
                    continue;
                }
                if (!gx->height) {
                    fr[fp].v = g;
                    fr[fp].e = 0;
                    fr[fp].best = 0;
                    fp++;
                    pushed = 1;
                    break;
                }
                if (gx->height > t->best) {
                    t->best = gx->height;
                    vx->hnext = g + 1u;
                }
                t->e++;
            }
            if (pushed)
                continue;
            vx->height = t->best + 1u;
            fp--;
        }
    }
    for (i = 0; i < c->nf && !c->err; i++)
        if ((FNI(c, i)->flags & OTC_FF_REG) && !FX(c, i)->cyclic && FX(c, i)->height > OTC_L_MAX_DEPTH + (CK_SAB == 1 ? 1u : 0u))
            cc_func_err_lazy(c, FX(c, i)->func, FNI(c, i)->line, OTC_LK_DEPTH, FX(c, i)->height);
    return c->err == 0;
}

/* ---- pass_page ------------------------------------------------------------------------------------------
 * A `page` function may not use an op of the namespaces FILE, SLOT, POOL (OTC_PAGE_NS_FIRST..LAST: the card, the sample
 * slots, the pool), directly or through what it calls. first_io(f): the first such op of f's own text (lowest line, the
 * first in the walk on a tie), else, over the callees of f in order of first call (cyclic ones skipped), the first that
 * has a first_io, at the line of that call. Memoised per function (iostate), iterative (a call chain can be long before
 * `call depth` refuses it). tools/otype.py Program.pass_page is the twin. */
typedef struct { uint32_t op, line; int found; } io_t;

static void ck_io_visit(otc_comp_t *c, void *ctx, uint32_t node, uint32_t line)
{
    io_t *io = (io_t *)ctx;
    const otc_node_t *nd = ND(c, node);
    if (nd->kind == OTC_N_OP || nd->kind == OTC_N_PRED || nd->kind == OTC_N_SETSTMT) {
        uint32_t ns = (uint32_t)(otc_reg[nd->op].id >> 8);
        if (ns >= OTC_PAGE_NS_FIRST && ns <= OTC_PAGE_NS_LAST && (!io->found || (CK_SAB == 4 ? line > io->line : line < io->line))) {
            io->found = 1;
            io->line = line;
            io->op = nd->op;
        }
    }
}

static void ck_io_direct(otc_comp_t *c, uint32_t i)       /* the function's own text: sets iostate 2 when it has one */
{
    io_t io;
    otc_fx_t *fx = FX(c, i);
    io.found = 0;
    io.op = io.line = 0;
    ck_walk_func(c, fx->func, ck_io_visit, &io);
    fx->iostate = 1;
    if (io.found) {
        fx->io_op = (uint8_t)(io.op + 1u);
        fx->io_line = io.line;
        fx->iostate = 2;
    }
}

CC_NOINLINE static int ck_page(otc_comp_t *c)
{
    uint32_t i, fo = 0, fp;
    ck_frame_t *fr = 0;
    for (i = 0; i < c->nf; i++)
        if ((FNI(c, i)->flags & OTC_FF_REG) && FNI(c, i)->page_line)
            break;
    if (i == c->nf)
        return 1;                                         /* no page function: nothing to do (and nothing allocated) */
    fo = cc_alloc(c, c->nf * (uint32_t)sizeof(ck_frame_t));
    if (!fo)
        return 0;
    fr = PTR(c, ck_frame_t, fo);
    for (i = 0; i < c->nf && !c->err; i++) {
        otc_fx_t *rx = FX(c, i);
        if (!(FNI(c, i)->flags & OTC_FF_REG) || !FNI(c, i)->page_line)
            continue;
        if (!rx->iostate) {
            ck_io_direct(c, i);
            fr[0].v = i;
            fr[0].e = 0;
            fp = 1;
            while (fp && !c->err) {
                ck_frame_t *t = &fr[fp - 1u];
                otc_fx_t *vx = FX(c, t->v);
                int pushed = 0;
                if (vx->iostate == 2) {                  /* a direct op: done */
                    fp--;
                    continue;
                }
                while (t->e < vx->ncg) {
                    const otc_edge_t *ed = PTR(c, otc_edge_t, vx->cg) + t->e;
                    otc_fx_t *gx = FX(c, ed->g);
                    if (gx->cyclic || gx->iostate == 1) {
                        t->e++;
                        continue;
                    }
                    if (!gx->iostate) {
                        ck_io_direct(c, ed->g);
                        fr[fp].v = ed->g;
                        fr[fp].e = 0;
                        fp++;
                        pushed = 1;
                        break;
                    }
                    if (gx->io_op) {                      /* the callee has one: its op, at the line of this call */
                        vx->io_op = gx->io_op;
                        vx->io_line = ed->line;
                        break;
                    }
                    t->e++;
                }
                if (pushed)
                    continue;
                vx->iostate = 2;                          /* done (with or without an op) */
                fp--;
            }
        }
    }
    for (i = 0; i < c->nf && !c->err; i++) {
        const otc_fx_t *fx = FX(c, i);
        if ((FNI(c, i)->flags & OTC_FF_REG) && FNI(c, i)->page_line && fx->io_op) {
            mb_begin(c);
            mb_lit(c, "op ");
            mb_rstr(c, otc_reg[fx->io_op - 1u].shown);
            mb_lit(c, " not allowed in a page function");
            cc_func_err(c, fx->func, fx->io_line, mb_end(c), 0);
        }
    }
    return c->err == 0;
}

/* ---- pass_input_sample -------------------------------------------------------------------------------
 * A function using a src.* op directly or through what it calls must declare `input sample`. Any registered function
 * without `input sample` that reaches a `src.*` op is refused: `op <shown> needs input sample`.
 * Twin of pass_page and tools/otype.py Program.pass_input_sample. */
static int op_needs_input_sample(uint32_t op)
{
    const char *nm = (const char *)&otc_rstr[otc_reg[op].name];
    return nm[0] == 'S' && nm[1] == 'R' && nm[2] == 'C' && nm[3] == '.';
}

static void ck_src_visit(otc_comp_t *c, void *ctx, uint32_t node, uint32_t line)
{
    io_t *src = (io_t *)ctx;
    const otc_node_t *nd = ND(c, node);
    if (nd->kind == OTC_N_OP || nd->kind == OTC_N_PRED || nd->kind == OTC_N_SETSTMT) {
        if (op_needs_input_sample(nd->op) && (!src->found || line < src->line)) {
            src->found = 1;
            src->line = line;
            src->op = nd->op;
        }
    }
}

static void ck_src_direct(otc_comp_t *c, uint32_t i)
{
    io_t src;
    otc_fx_t *fx = FX(c, i);
    src.found = 0;
    src.op = src.line = 0;
    ck_walk_func(c, fx->func, ck_src_visit, &src);
    fx->srcstate = 1;
    if (src.found) {
        fx->src_op = (uint8_t)(src.op + 1u);
        fx->src_line = src.line;
        fx->srcstate = 2;
    }
}

CC_NOINLINE static int ck_input_sample(otc_comp_t *c)
{
    uint32_t i, fo = 0, fp;
    ck_frame_t *fr = 0;
    fo = cc_alloc(c, c->nf * (uint32_t)sizeof(ck_frame_t));
    if (!fo)
        return 0;
    fr = PTR(c, ck_frame_t, fo);
    for (i = 0; i < c->nf && !c->err; i++) {
        otc_fx_t *rx = FX(c, i);
        if (!(FNI(c, i)->flags & OTC_FF_REG) || (FNI(c, i)->flags & OTC_FF_INPUT_SAMPLE))
            continue;
        if (!rx->srcstate) {
            ck_src_direct(c, i);
            fr[0].v = i;
            fr[0].e = 0;
            fp = 1;
            while (fp && !c->err) {
                ck_frame_t *t = &fr[fp - 1u];
                otc_fx_t *vx = FX(c, t->v);
                int pushed = 0;
                if (vx->srcstate == 2) {                  /* a direct op: done */
                    fp--;
                    continue;
                }
                while (t->e < vx->ncg) {
                    const otc_edge_t *ed = PTR(c, otc_edge_t, vx->cg) + t->e;
                    otc_fx_t *gx = FX(c, ed->g);
                    if (gx->cyclic || gx->srcstate == 1) {
                        t->e++;
                        continue;
                    }
                    if (!gx->srcstate) {
                        ck_src_direct(c, ed->g);
                        fr[fp].v = ed->g;
                        fr[fp].e = 0;
                        fp++;
                        pushed = 1;
                        break;
                    }
                    if (gx->src_op) {                      /* the callee has one: its op, at the line of this call */
                        vx->src_op = gx->src_op;
                        vx->src_line = ed->line;
                        break;
                    }
                    t->e++;
                }
                if (pushed)
                    continue;
                vx->srcstate = 2;                          /* done (with or without an op) */
                fp--;
            }
        }
    }
    for (i = 0; i < c->nf && !c->err; i++) {
        const otc_fx_t *fx = FX(c, i);
        if ((FNI(c, i)->flags & OTC_FF_REG) && !(FNI(c, i)->flags & OTC_FF_INPUT_SAMPLE) && fx->src_op) {
            mb_begin(c);
            mb_lit(c, "op ");
            mb_rstr(c, otc_reg[fx->src_op - 1u].shown);
            mb_lit(c, " needs input sample");
            cc_func_err(c, fx->func, fx->src_line, mb_end(c), 0);
        }
    }
    return c->err == 0;
}

/* pass_needs */
static void ck_needs(otc_comp_t *c)
{
    int changed;
    uint32_t i, e;
    do {
        changed = 0;
        for (i = 0; i < c->nf && !c->err; i++) {
            const otc_fx_t *fx = FX(c, i);
            if (!(FNI(c, i)->flags & OTC_FF_REG) || FNI(c, i)->nerr)
                continue;
            for (e = 0; e < fx->ncg; e++) {
                const otc_edge_t *ed = PTR(c, otc_edge_t, fx->cg) + e;
                if (FNI(c, ed->g)->nerr) {
                    mb_begin(c);
                    mb_lit(c, "needs ");
                    mb_show_ref(c, FNI(c, ed->g)->name);
                    cc_func_err(c, fx->func, ed->line, mb_end(c), 0);
                    changed = 1;
                    break;
                }
            }
        }
    } while (changed && !c->err);
}

/* ---- cost ------------------------------------------------------------------------------------------------------ */
typedef struct { uint32_t st; int32_t lo, hi; } iv_t;                    /* st: RS_NONE (unknown), RS_IV, or IV_PEND */
#define IV_PEND 3u

static uint32_t sat_add(uint32_t a, uint32_t b)
{
    uint32_t s = a + b;
    return s < a ? 0xFFFFFFFFu : s;
}

static uint32_t sat_mul(uint32_t a, uint32_t b)
{
    if (!a || !b)
        return 0;
    return a > 0xFFFFFFFFu / b ? 0xFFFFFFFFu : a * b;
}

static void ck_param_range(const otc_comp_t *c, const otc_func_t *f, uint32_t text, iv_t *out)
{
    uint32_t q, n;
    const uint8_t *p = cc_sp(c, text, &n);
    out->st = RS_NONE;
    out->lo = out->hi = 0;
    for (q = f->param_first; q; q = CPTR(c, otc_param_t, q)->next)
        if (cc_ref_eq(c, CPTR(c, otc_param_t, q)->name, p, n, 0)) {
            out->st = RS_IV;
            out->lo = CPTR(c, otc_param_t, q)->lo;
            out->hi = CPTR(c, otc_param_t, q)->hi;
            return;
        }
}

/* ival(f, n, env): env == 0: the ranges of the arguments (compute_ranges), else the values of the call being costed */
static void ck_ival(otc_comp_t *c, uint32_t fi, uint32_t n, const iv_t *env, iv_t *out)
{
    const otc_node_t *nd = ND(c, n);
    out->st = RS_NONE;
    out->lo = out->hi = 0;
    if (nd->kind == OTC_N_NUM) {
        out->st = RS_IV;
        out->lo = out->hi = (int32_t)nd->val;
    } else if (nd->kind == OTC_N_CONST) {
        uint32_t tn, kc;
        const uint8_t *tp = cc_sp(c, nd->text, &tn);
        kc = cc_cmap_find(c, tp, tn, 0);
        if (kc && CN(c, kc)->type == OTC_CT_INT) {
            out->st = RS_IV;
            out->lo = out->hi = (int32_t)CN(c, kc)->val;
        }
    } else if (nd->kind == OTC_N_PARAM) {
        ck_param_range(c, FNI(c, fi), nd->text, out);
    } else if (nd->kind == OTC_N_ARG) {
        uint32_t pos = ck_find_arg(c, FNI(c, fi), nd->text);
        if (pos == 0xFFFFFFFFu)
            return;
        if (!env) {
            const otc_argx_t *ax = ck_ax(c, fi, pos);
            if (ax->rstate == RS_IV) {
                out->st = RS_IV;
                out->lo = ax->lo;
                out->hi = ax->hi;
            } else if (ax->rstate == RS_MISSING && FX(c, fi)->gcnt) {
                out->st = IV_PEND;
            }
        } else {
            out->st = env[pos].st;
            out->lo = env[pos].lo;
            out->hi = env[pos].hi;
        }
    }
}

CC_NOINLINE static void ck_ranges(otc_comp_t *c)
{
    uint32_t pass, r, s;
    for (pass = 0; pass < 30u && !c->err; pass++) {
        int changed = 0;
        for (r = 0; r < c->ngroups; r++) {
            uint32_t gi = PTR(c, uint32_t, c->gorder)[r];
            const otc_fx_t *gfx = FX(c, gi);
            for (s = 0; s < gfx->gcnt; s++) {
                const otc_site_t *st = PTR(c, otc_site_t, c->sites) + PTR(c, uint32_t, c->sorder)[gfx->gstart + s];
                uint32_t a, kx = 0;
                for (a = ND(c, st->node)->first; a; a = ND(c, a)->next, kx++) {
                    iv_t iv;
                    otc_argx_t *key;
                    if (kx >= FNI(c, gi)->nargs)
                        break;
                    ck_ival(c, st->f, a, 0, &iv);
                    if (iv.st == IV_PEND)
                        continue;
                    key = AXA(c, FX(c, gi)) + AXA(c, FX(c, gi))[kx].canon;
                    if (key->rstate == RS_NONE)
                        continue;
                    if (iv.st == RS_NONE) {
                        key->rstate = RS_NONE;
                        key->lo = key->hi = 0;
                        changed = 1;
                    } else if (key->rstate == RS_IV) {
                        int32_t lo = key->lo < iv.lo ? key->lo : iv.lo, hi = key->hi > iv.hi ? key->hi : iv.hi;
                        if (lo != key->lo || hi != key->hi) {
                            key->lo = lo;
                            key->hi = hi;
                            changed = 1;
                        }
                    } else {
                        key->rstate = RS_IV;
                        key->lo = iv.lo;
                        key->hi = iv.hi;
                        changed = 1;
                    }
                }
            }
        }
        if (!changed)
            break;
    }
}

typedef struct {
    uint32_t next, f, cost;
    iv_t iv[4];
} memo_t;

static uint32_t cost_func(otc_comp_t *c, uint32_t fi, const iv_t *key);
static uint32_t cost_expr(otc_comp_t *c, uint32_t fi, uint32_t n, const iv_t *env);
static uint32_t ck_cost_block(otc_comp_t *c, uint32_t fi, uint32_t first, const iv_t *env);

/* the cost of a call: its arguments, and the callee costed for the values it gets (its own frame, so that the long
 * recursion of expressions in cost_expr stays small) */
CC_NOINLINE static uint32_t cost_call(otc_comp_t *c, uint32_t fi, uint32_t n, const iv_t *env)
{
    const otc_node_t *nd = ND(c, n);
    uint32_t g = ck_first_call_target(c, n), i = 0, a, total = 0;
    iv_t key[4];
    for (a = nd->first; a; a = ND(c, a)->next, i++) {
        total = sat_add(total, cost_expr(c, fi, a, env));
        if (g && i < 4u)
            ck_ival(c, fi, a, env, &key[i]);
    }
    if (g)
        total = sat_add(total, cost_func(c, FIDX(c, g), key));
    return total;
}

static uint32_t cost_expr(otc_comp_t *c, uint32_t fi, uint32_t n, const iv_t *env)
{
    const otc_node_t *nd = ND(c, n);
    uint32_t a, total;
    if (nd->kind == OTC_N_OP) {
        const otc_reg_t *r = &otc_reg[nd->op];
        if ((r->flags & OTC_RF_LAZY) && nd->first) {
            uint32_t mx = 0;
            total = sat_add(r->cost, cost_expr(c, fi, nd->first, env));
            for (a = ND(c, nd->first)->next; a; a = ND(c, a)->next) {
                uint32_t v = cost_expr(c, fi, a, env);
                if (v > mx)
                    mx = v;
            }
            return sat_add(total, mx);
        }
        total = r->cost;
        for (a = nd->first; a; a = ND(c, a)->next)
            total = sat_add(total, cost_expr(c, fi, a, env));
        return total;
    }
    if (nd->kind == OTC_N_CALL)
        return cost_call(c, fi, n, env);
    return 0;
}

static uint32_t cost_stmt(otc_comp_t *c, uint32_t fi, uint32_t s, const iv_t *env)
{
    const otc_node_t *nd = ND(c, s);
    switch (nd->kind) {
    case OTC_N_ASSIGN: return cost_expr(c, fi, nd->first, env);
    case OTC_N_OPSTMT:
    case OTC_N_CALLSTMT:
    case OTC_N_CTXSTMT: return cost_expr(c, fi, nd->first, env);
    case OTC_N_RET: return nd->first ? cost_expr(c, fi, nd->first, env) : 0;
    case OTC_N_SETSTMT: {
        uint32_t tot = 1u, a;
        for (a = nd->first; a; a = CPTR(c, otc_node_t, a)->next)
            tot = sat_add(tot, cost_expr(c, fi, a, env));
        return tot;
    }
    default: return 0;
    }
}

static uint32_t ck_trk_count(otc_comp_t *c, uint32_t node)      /* how many times a `trk` runs what it governs, at most */
{
    const otc_node_t *nd = ND(c, node);
    uint32_t a0 = nd->first;
    if (ND(c, a0)->kind == OTC_N_KW)
        return cc_ref_ci_eq(c, ND(c, a0)->text, "ALL") ? 8u : 1u;
    return nd->val > 1u ? nd->val : 1u;
}

/* cost_mods(f, ln, idx, env): the modifiers from `m` on, then the statement or the block under the line. Python folds them
 * from the inside, cost = a1 + k1 * (a2 + k2 * (... inner)): a1 + k1 * a2 + k1 * k2 * a3 + ... + (k1 * k2 * ...) * inner,
 * a loop with a running product (a: what the modifier costs, k: how many times it runs what it governs). */
static uint32_t ck_cost_mods(otc_comp_t *c, uint32_t fi, uint32_t ln, uint32_t m, const iv_t *env)
{
    const otc_line_t *l = LN(c, ln);
    uint32_t add = 0, mul = 1, inner;
    for (; m; m = ND(c, m)->next) {
        uint32_t kind = ND(c, m)->kind, a = 0, k = 1;
        if (kind == OTC_N_M_IF || kind == OTC_N_M_ELIF || kind == OTC_N_M_PROB || kind == OTC_N_M_EVERY || kind == OTC_N_M_SKIP) {
            a = cost_expr(c, fi, ND(c, m)->first, env);
        } else if (kind == OTC_N_M_L) {
            iv_t lo, hi;
            uint32_t a0 = ND(c, m)->first, b0 = ND(c, a0)->next;
            ck_ival(c, fi, a0, env, &lo);
            ck_ival(c, fi, b0, env, &hi);
            if (lo.st != RS_IV || hi.st != RS_IV) {
                k = 1;
            } else {
                int32_t d1 = hi.hi - lo.lo, d2 = lo.hi - hi.lo;
                if (d1 < 0)
                    d1 = -d1;
                if (d2 < 0)
                    d2 = -d2;
                k = (uint32_t)(d1 > d2 ? d1 : d2) + 1u;
            }
        } else if (kind == OTC_N_M_EACH) {
            k = OTC_L_EACH_ITERS;
        } else if (kind == OTC_N_M_CTX) {
            uint32_t node = ND(c, m)->first;
            a = cost_expr(c, fi, node, env);
            if (ND(c, node)->op == OTC_OP_TRK)
                k = ck_trk_count(c, node);
        }
        add = sat_add(add, sat_mul(mul, a));
        mul = sat_mul(mul, k);
    }
    if (l->body && !(l->flags & OTC_LF_DECL))
        inner = cost_stmt(c, fi, l->body, env);
    else
        inner = ck_cost_block(c, fi, l->child_first, env);
    return sat_add(add, sat_mul(mul, inner));
}

static int ck_is_trk_line(const otc_comp_t *c, const otc_line_t *l)    /* a `trk` statement line */
{
    return !l->mods && l->body && !(l->flags & OTC_LF_DECL) && ND(c, l->body)->kind == OTC_N_CTXSTMT &&
           ND(c, ND(c, l->body)->first)->op == OTC_OP_TRK;
}

static uint32_t ck_cost_block(otc_comp_t *c, uint32_t fi, uint32_t first, const iv_t *env)
{
    uint32_t total = 0, mult = 1, ln = first;
    while (ln) {
        const otc_line_t *l = LN(c, ln);
        int fk = cc_first_kind(c, l);
        if (fk == OTC_N_M_IF) {
            /* the chain: this line and the lines after it that start with elif / else. Python folds it from the last
             * line: acc = else: inner; if/elif: cost(cond) + max(inner, acc). Unrolled, that is the maximum over the
             * lines of (conditions up to that line + its own cost), the lines after the first else being ignored. */
            uint32_t pre = 0, best = 0, cur = ln, stop = 0;
            while (cur) {
                const otc_line_t *cl = LN(c, cur);
                int ck = cc_first_kind(c, cl);
                uint32_t inner;
                if (cur != ln && ck != OTC_N_M_ELIF && ck != OTC_N_M_ELSE)
                    break;
                if (!stop) {
                    inner = ck_cost_mods(c, fi, cur, ND(c, cl->mods)->next, env);
                    if (ck == OTC_N_M_ELSE) {
                        uint32_t v = sat_add(pre, inner);
                        if (v > best || CK_SAB == 2)
                            best = CK_SAB == 2 ? sat_add(best, v) : v;
                        stop = 1;
                    } else {
                        uint32_t v;
                        pre = sat_add(pre, cost_expr(c, fi, ND(c, cl->mods)->first, env));
                        v = sat_add(pre, inner);
                        if (v > best || CK_SAB == 2)
                            best = CK_SAB == 2 ? sat_add(best, v) : v;
                    }
                }
                cur = cl->next;
            }
            total = sat_add(total, sat_mul(mult, best));
            ln = cur;
        } else if (ck_is_trk_line(c, l)) {
            /* a multi-track `trk` runs the rest of the block once per track: a product, not a recursion */
            total = sat_add(total, sat_mul(mult, cost_expr(c, fi, ND(c, l->body)->first, env)));
            mult = sat_mul(mult, ck_trk_count(c, ND(c, l->body)->first));
            ln = l->next;
        } else {
            total = sat_add(total, sat_mul(mult, ck_cost_mods(c, fi, ln, l->mods, env)));
            ln = l->next;
        }
    }
    return total;
}

static uint32_t cost_func(otc_comp_t *c, uint32_t fi, const iv_t *key)
{
    otc_func_t *f = FNI(c, fi);
    uint32_t h = fi * 2654435761u, a, e, nk = f->nargs < 4u ? f->nargs : 4u, cost;
    memo_t *me;
    iv_t norm[4];
    for (a = 0; a < nk; a++) {
        norm[a].st = key[a].st == RS_IV ? RS_IV : RS_NONE;
        norm[a].lo = norm[a].st == RS_IV ? key[a].lo : 0;
        norm[a].hi = norm[a].st == RS_IV ? key[a].hi : 0;
        h = (h ^ norm[a].st) * 16777619u;
        h = (h ^ (uint32_t)norm[a].lo) * 16777619u;
        h = (h ^ (uint32_t)norm[a].hi) * 16777619u;
    }
    for (e = PTR(c, uint32_t, c->memo)[h % OTC_MEMO_BUCKETS]; e; e = PTR(c, memo_t, e)->next) {
        me = PTR(c, memo_t, e);
        if (me->f != fi)
            continue;
        for (a = 0; a < nk; a++)
            if (me->iv[a].st != norm[a].st || me->iv[a].lo != norm[a].lo || me->iv[a].hi != norm[a].hi)
                break;
        if (a == nk)
            return me->cost;
    }
    e = cc_alloc(c, (uint32_t)sizeof(memo_t));
    if (!e)
        return 0;
    me = PTR(c, memo_t, e);                         /* the entry first (cost 0: the recursion guard of Python), then the value */
    me->f = fi;
    for (a = 0; a < nk; a++) {
        me->iv[a].st = norm[a].st;
        me->iv[a].lo = norm[a].lo;
        me->iv[a].hi = norm[a].hi;
    }
    me->next = PTR(c, uint32_t, c->memo)[h % OTC_MEMO_BUCKETS];
    PTR(c, uint32_t, c->memo)[h % OTC_MEMO_BUCKETS] = e;
    c->nmemo++;
    cost = ck_cost_block(c, fi, f->root_first, norm);
    PTR(c, memo_t, e)->cost = cost;
    return cost;
}

CC_NOINLINE static void ck_cost(otc_comp_t *c)
{
    uint32_t i, a;
    ck_ranges(c);
    c->memo = cc_alloc(c, OTC_MEMO_BUCKETS * 4u);
    if (!c->memo)
        return;
    for (i = 0; i < c->nf && !c->err; i++) {
        otc_func_t *f = FNI(c, i);
        iv_t key[4];
        uint32_t cost;
        if (!(f->flags & OTC_FF_REG) || f->nerr)
            continue;
        for (a = 0; a < 4u; a++) {
            key[a].st = RS_NONE;
            key[a].lo = key[a].hi = 0;
        }
        for (a = 0; a < f->nargs && a < 4u; a++) {
            const otc_argx_t *ax = AXA(c, FX(c, i));
            const otc_argx_t *kx = &ax[ax[a].canon];
            if (kx->rstate == RS_IV) {
                key[a].st = RS_IV;
                key[a].lo = kx->lo;
                key[a].hi = kx->hi;
            }
        }
        cost = cost_func(c, i, key);
        FX(c, i)->cost = cost;
        FX(c, i)->costed = 1;
        if (f->nmenus && cost > OTC_L_MENU_BUDGET) {
            mb_begin(c);
            mb_lit(c, "too long: ");
            mb_udec(c, cost);                       /* (min(cost, 0xFFFFFFFF): the cost is saturated) */
            mb_lit(c, "/");
            mb_dec(c, OTC_L_MENU_BUDGET);
            cc_func_err(c, FX(c, i)->func, f->line, mb_end(c), 0);
        }
    }
}

/* ---- pass_flags ---------------------------------------------------------------------------------------------- */
static void ck_flag_visit(otc_comp_t *c, void *ctx, uint32_t node, uint32_t line)
{
    (void)line;
    if (ND(c, node)->kind == OTC_N_OP && (otc_reg[ND(c, node)->op].flags & OTC_RF_DESTRUCTIVE))
        *(uint32_t *)ctx = 1;
}

CC_NOINLINE static void ck_flags(otc_comp_t *c)
{
    uint32_t i, sp, e, f;
    uint32_t *todo;
    if (!c->nf)
        return;
    for (i = 0; i < c->nf && !c->err; i++) {
        uint32_t d = 0;
        if (FNI(c, i)->flags & OTC_FF_REG)
            ck_walk_func(c, FX(c, i)->func, ck_flag_visit, &d);
        FX(c, i)->direct = (uint8_t)d;
    }
    f = cc_alloc(c, c->nf * 4u);
    if (!f)
        return;
    todo = PTR(c, uint32_t, f);
    for (i = 0; i < c->nf && !c->err; i++) {
        c->gen++;
        sp = 0;
        todo[sp++] = i;
        FX(c, i)->stamp = c->gen;
        while (sp) {
            uint32_t x = todo[--sp];
            const otc_fx_t *xx = FX(c, x);
            if (xx->direct)
                FX(c, i)->destr = 1;
            for (e = 0; e < xx->ncg; e++) {
                uint32_t g = PTR(c, otc_edge_t, xx->cg)[e].g;
                if (FX(c, g)->stamp != c->gen) {
                    FX(c, g)->stamp = c->gen;
                    todo[sp++] = g;
                }
            }
        }
    }
}

typedef struct {
    uint32_t next;           /* next cat in order of appearance */
    uint32_t name_ref;       /* string ref, or 0 if "SCRIPTS" */
    uint32_t head;           /* first item of linked list */
    uint32_t tail;           /* last item of linked list */
    uint32_t count;
} ck_cat_t;

typedef struct {
    uint32_t next;
    uint32_t fn;             /* func offset */
    uint32_t line;           /* menu line */
} ck_menu_item_t;

static void ck_menu(otc_comp_t *c)
{
    uint32_t cat_head = 0, cat_tail = 0;
    uint32_t i, cur;

    for (i = 0; i < c->nf; i++) {
        const otc_fx_t *fx = FX(c, i);
        const otc_func_t *f = FNI(c, i);
        uint32_t m;
        if (fx->bad || f->nerr)
            continue;
        for (m = f->menu_first; m; m = PTR(c, otc_menu_t, m)->next) {
            const otc_menu_t *mn = PTR(c, otc_menu_t, m);
            uint32_t cat_ref = 0; /* 0 represents "SCRIPTS" */
            uint32_t found_cat = 0;
            uint32_t item;
            if (mn->nlevels && mn->lfirst) {
                uint32_t ln;
                (void)cc_sp(c, PTR(c, otc_cell_t, mn->lfirst)->ref, &ln);
                if (ln <= 11u)
                    cat_ref = PTR(c, otc_cell_t, mn->lfirst)->ref;
            }
            cur = cat_head;
            while (cur) {
                const ck_cat_t *cat = PTR(c, ck_cat_t, cur);
                if (cat_ref == 0 && cat->name_ref == 0) {
                    found_cat = cur;
                    break;
                }
                if (cat_ref != 0 && cat->name_ref != 0) {
                    uint32_t l1;
                    const uint8_t *s1 = cc_sp(c, cat_ref, &l1);
                    if (cc_ref_eq(c, cat->name_ref, s1, l1, 0)) {
                        found_cat = cur;
                        break;
                    }
                }
                cur = cat->next;
            }
            if (!found_cat) {
                found_cat = cc_alloc(c, (uint32_t)sizeof(ck_cat_t));
                if (!found_cat)
                    return;
                PTR(c, ck_cat_t, found_cat)->next = 0;
                PTR(c, ck_cat_t, found_cat)->name_ref = cat_ref;
                PTR(c, ck_cat_t, found_cat)->head = 0;
                PTR(c, ck_cat_t, found_cat)->tail = 0;
                PTR(c, ck_cat_t, found_cat)->count = 0;
                if (cat_tail)
                    PTR(c, ck_cat_t, cat_tail)->next = found_cat;
                else
                    cat_head = found_cat;
                cat_tail = found_cat;
            }
            item = cc_alloc(c, (uint32_t)sizeof(ck_menu_item_t));
            if (!item)
                return;
            PTR(c, ck_menu_item_t, item)->next = 0;
            PTR(c, ck_menu_item_t, item)->fn = fx->func;
            PTR(c, ck_menu_item_t, item)->line = mn->line;
            if (PTR(c, ck_cat_t, found_cat)->tail)
                PTR(c, ck_menu_item_t, PTR(c, ck_cat_t, found_cat)->tail)->next = item;
            else
                PTR(c, ck_cat_t, found_cat)->head = item;
            PTR(c, ck_cat_t, found_cat)->tail = item;
            PTR(c, ck_cat_t, found_cat)->count++;
        }
    }

    {
        uint32_t cat_idx = 0;
        cur = cat_head;
        while (cur) {
            const ck_cat_t *cat = PTR(c, ck_cat_t, cur);
            if (cat_idx >= 32u) {
                /* 1. Excess categories (> 32) */
                uint32_t it = cat->head;
                while (it) {
                    const ck_menu_item_t *mi = PTR(c, ck_menu_item_t, it);
                    mb_begin(c);
                    mb_lit(c, "more than 32 menu categories (excess categories ignored)");
                    cc_func_warn(c, mi->fn, mi->line, mb_end(c));
                    it = mi->next;
                }
            } else if (cat->count > 64u) {
                /* 2. Excess scripts in category (> 64) for the first 32 categories */
                uint32_t it = cat->head;
                uint32_t idx = 0;
                while (it) {
                    const ck_menu_item_t *mi = PTR(c, ck_menu_item_t, it);
                    if (idx >= 64u) {
                        mb_begin(c);
                        mb_lit(c, "category \"");
                        if (cat->name_ref) {
                            uint32_t ln;
                            const uint8_t *sp = cc_sp(c, cat->name_ref, &ln);
                            mb_bytes(c, sp, ln);
                        } else {
                            mb_lit(c, "SCRIPTS");
                        }
                        mb_lit(c, "\" has more than 64 scripts (script not listed)");
                        cc_func_warn(c, mi->fn, mi->line, mb_end(c));
                    }
                    idx++;
                    it = mi->next;
                }
            }
            cat_idx++;
            cur = cat->next;
        }
    }
}

/* ---- the whole checker -------------------------------------------------------------------------------------- */
CC_NOINLINE static void ck_run(otc_comp_t *c)
{
    if (!ck_setup(c))
        return;
    ck_find_unused(c);
    if (c->err || !ck_build_sites(c))
        return;
    ck_infer_kinds(c);
    ck_check_all(c);
    if (c->err || !ck_graph(c))
        return;
    if (!ck_page(c))
        return;
    if (!ck_input_sample(c))
        return;
    ck_needs(c);
    ck_cost(c);
    ck_needs(c);
    ck_flags(c);
    ck_menu(c);
    if (!c->err && c->nfuncs > OTC_L_MAX_FUNCS) {
        uint32_t f = FX(c, OTC_L_MAX_FUNCS)->func;
        mb_begin(c);
        mb_dec(c, (int32_t)c->nfuncs);
        mb_lit(c, " functions loaded (maximum ");
        mb_dec(c, OTC_L_MAX_FUNCS);
        mb_byte(c, ')');
        cc_func_warn(c, f, FN(c, f)->line, mb_end(c));
    }
}
