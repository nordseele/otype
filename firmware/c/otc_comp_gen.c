/* OType compiler, CODE GENERATOR (the diagnostics, the bytes of the table). Included by otc_comp.c after
 * the checker. Mirrors modules/otype/otb.py compile_program / apply_compile:
 *   - `not on the unit yet: op` (profile), `stack depth N > 16`, `loop/pass depth N > 16`, `context depth N > 16`,
 *     `code too long (N bytes)`, `more than 255 every/skip counters`, `more than 255 menu lines`, `needs f` (a callee
 *     the compiler refused);
 *   - the table-level errors: `more than 255 functions`, `string too long for the pool` (a label over 255 bytes),
 *     `more than 1024 every/skip counters in the table`, `compiled table is N bytes (> 32768)`.
 * fc_* are the methods of otb.FuncCompiler (same names, same order of calls). One walker, three uses:
 *   - the dry run (decides what is refused: counts bytes and stack, builds nothing);
 *   - the pool pass (the same walk, adding the strings / lists to the pool in the order of first use: Python's order);
 *   - the emission (otc_emit: the same walk again, writing the code, the line table and the counters of every function
 *     into the caller's buffer; the pool already holds every offset).
 * Recursion: fc_expr (<= OTC_EXPR_DEPTH + 3), fc_mods (<= 64: only accepted functions are generated, their lines
 * have <= 128 characters), fc_block / fc_chain / fc_children / fc_scoped (blocks nest <= 8 in an accepted function).
 * A run of `trk` lines is a loop with an explicit stack of labels (Python recurses once per line). */

#define LB_NONE 0x7FFFFFFF      /* a label whose stack depth is not known yet */
#define LB_END  0xFFFFu         /* end of the chain of the jump operands waiting for a label (codes are < 65535 bytes) */

#ifdef OTC_SABOTAGE_GEN         /* must-fail builds (tools/test_otype_ccomp.py): 1 = a menu before the file name in the pool, 2 = a line-table offset, 3 = the page label before the menu list,
                                   4 = the confirm text before the page label */
#define GEN_SAB OTC_SABOTAGE_GEN
#else
#define GEN_SAB 0
#endif

enum {                          /* the instruction set (otb.INSTRS) */
    OI_PUSH8 = 0x10, OI_PUSH16 = 0x11, OI_PUSHK = 0x12, OI_PUSHC = 0x13, OI_LD = 0x14, OI_LDA = 0x15, OI_LDP = 0x16,
    OI_ST = 0x17, OI_DROP = 0x18, OI_OP = 0x30, OI_OPV = 0x31, OI_SET = 0x32, OI_JMP = 0x40, OI_JZ = 0x41, OI_SEL = 0x42,
    OI_PROB = 0x43, OI_EVERY = 0x44, OI_SKIP = 0x45, OI_JNEV = 0x46, OI_JNGT = 0x47, OI_LBEG = 0x50, OI_LNEXT = 0x51, OI_EBEG = 0x52, OI_ENEXT = 0x53,
    OI_PASS = 0x54, OI_PNEXT = 0x55, OI_BREAK = 0x56, OI_CALL = 0x60, OI_RET = 0x61, OI_STOP = 0x62, OI_SCOPE = 0x70,
    OI_UNSCOPE = 0x71, OI_DIR = 0x80, OI_EXT = 0x81, OI_MATCH = 0x82, OI_MSG = 0x83
};

typedef struct { int32_t depth, head; } fc_lb_t;                 /* a label: stack depth, chain of the operands to patch */
typedef struct { fc_lb_t lb; uint32_t start; } fc_pass_t;        /* a trk pass of a block: its end label, its first byte */

typedef struct {
    otc_comp_t *c;
    uint32_t fn;
    uint32_t pos, nlines, last_pc, last_line, cur, nctr, maxdepth_line;
    uint32_t ctx, ctl, maxctx, maxctl, maxctx_line, maxctl_line, nlocals;
    int32_t depth, maxdepth;
    uint8_t dead, err, pool;          /* pool: add the constants to the pool (pool pass, emission) */
    uint8_t *code, *lines, *ctr;      /* emission: the bytes of the code of this function, its line table, its counters (NULL: not emitted) */
    uint32_t lcap;                    /* entries of the line table allotted to this function */
} fc_t;

/* ---- the pool (declared here: the walker adds to it) ----------------------------------------------------- */
static uint32_t pool_string(otc_comp_t *c, uint32_t ref);
static uint32_t pool_list(otc_comp_t *c, uint32_t ref);

static void fc_stack(fc_t *x, int32_t d)
{
    x->depth += d;
    if (x->depth > x->maxdepth) {
        x->maxdepth = x->depth;
        x->maxdepth_line = x->cur;
    }
}

static void fc_lb_init(fc_lb_t *lb)
{
    lb->depth = LB_NONE;
    lb->head = -1;
}

static void fc_put(fc_t *x, uint32_t at, uint32_t v)
{
    if (x->code)
        x->code[at] = (uint8_t)v;
}

static void fc_e0(fc_t *x, uint8_t code, int32_t stack)
{
    fc_put(x, x->pos, code);
    x->pos += 1;
    fc_stack(x, stack);
}

static void fc_e1(fc_t *x, uint8_t code, uint32_t a, int32_t stack)
{
    fc_put(x, x->pos, code);
    fc_put(x, x->pos + 1, a);
    x->pos += 2;
    fc_stack(x, stack);
}

static void fc_e2(fc_t *x, uint8_t code, uint32_t a, uint32_t b, int32_t stack)
{
    fc_put(x, x->pos, code);
    fc_put(x, x->pos + 1, a);
    fc_put(x, x->pos + 2, b);
    x->pos += 3;
    fc_stack(x, stack);
}

static void fc_e3(fc_t *x, uint8_t code, uint32_t a, uint32_t b, uint32_t d, int32_t stack)
{
    fc_put(x, x->pos, code);
    fc_put(x, x->pos + 1, a);
    fc_put(x, x->pos + 2, b);
    fc_put(x, x->pos + 3, d);
    x->pos += 4;
    fc_stack(x, stack);
}

static void fc_eh(fc_t *x, uint8_t code, uint32_t h, int32_t stack)      /* a 16-bit big-endian operand */
{
    fc_e2(x, code, (h >> 8) & 0xFFu, h & 0xFFu, stack);
}

/* a forward jump: `front` (0..255, or -1: none) then the 16-bit target, patched when the label is placed */
static void fc_jump(fc_t *x, fc_lb_t *lb, uint8_t code, int32_t front, int32_t stack, int is_jmp)
{
    uint32_t size = front >= 0 ? 4u : 3u, at = x->pos + size - 2u;
    if (x->code) {
        fc_put(x, x->pos, code);
        if (front >= 0)
            fc_put(x, x->pos + 1, (uint32_t)front);
        fc_put(x, at, (lb->head < 0 ? LB_END : (uint32_t)lb->head) >> 8);
        fc_put(x, at + 1, (lb->head < 0 ? LB_END : (uint32_t)lb->head) & 0xFFu);
        lb->head = (int32_t)at;
    }
    x->pos += size;
    fc_stack(x, stack);
    if (lb->depth == LB_NONE)
        lb->depth = x->depth;
    if (is_jmp)
        x->dead = 1;
}

static void fc_place(fc_t *x, fc_lb_t *lb)
{
    if (x->dead) {
        if (lb->depth != LB_NONE)
            x->depth = lb->depth;
        x->dead = 0;
    }
    lb->depth = x->depth;
    if (x->code) {
        int32_t h = lb->head;
        while (h >= 0) {                                    /* patch the chain of operands */
            uint32_t prev = ((uint32_t)x->code[h] << 8) | x->code[h + 1];
            x->code[h] = (uint8_t)(x->pos >> 8);
            x->code[h + 1] = (uint8_t)x->pos;
            h = prev == LB_END ? -1 : (int32_t)prev;
        }
        lb->head = -1;
    }
}

static void fc_wline(fc_t *x, uint32_t idx, uint32_t pc, uint32_t line)
{
    if (x->lines && idx < x->lcap) {
        uint8_t *p = x->lines + 4u * idx;
#ifdef OTC_SABOTAGE_GEN
        if (GEN_SAB == 2 && idx)
            pc++;
#endif
        p[0] = (uint8_t)(pc >> 8);
        p[1] = (uint8_t)pc;
        p[2] = (uint8_t)(line >> 8);
        p[3] = (uint8_t)line;
    }
}

static void fc_mark(fc_t *x, uint32_t line)
{
    x->cur = line;
    if (!x->nlines || x->last_line != line) {
        if (x->nlines && x->last_pc == x->pos) {
            x->last_line = line;
            fc_wline(x, x->nlines - 1u, x->pos, line);
        } else {
            fc_wline(x, x->nlines, x->pos, line);
            x->nlines++;
            x->last_pc = x->pos;
            x->last_line = line;
        }
    }
}

static void fc_ctx_up(fc_t *x)
{
    x->ctx++;
    if (x->ctx > x->maxctx) {
        x->maxctx = x->ctx;
        x->maxctx_line = x->cur;
    }
}

static void fc_scope_in(fc_t *x)
{
    fc_e0(x, OI_SCOPE, 0);
    fc_ctx_up(x);
}

static void fc_scope_out(fc_t *x)
{
    fc_e0(x, OI_UNSCOPE, 0);
    x->ctx--;
}

static void fc_frame_in(fc_t *x)                         /* a loop or a pass: a control frame and the scope of an iteration */
{
    x->ctl++;
    if (x->ctl > x->maxctl) {
        x->maxctl = x->ctl;
        x->maxctl_line = x->cur;
    }
    fc_ctx_up(x);
}

static void fc_frame_out(fc_t *x)
{
    x->ctl--;
    x->ctx--;
}

static uint32_t fc_var(fc_t *x, uint32_t text)           /* index of a variable `a b c d x y z t i j` (VARS = "ABCDXYZTIJ") */
{
    static const char vars[] = "ABCDXYZTIJ";
    uint32_t n, k = 0;
    const uint8_t *p = cc_sp(x->c, text, &n);
    while (k < 9u && n && (uint8_t)vars[k] != cc_up(p[0]))
        k++;
    if (k < 4u && k + 1u > x->nlocals)
        x->nlocals = k + 1u;
    return k;
}

static void fc_push_int(fc_t *x, int32_t v)
{
    if (v >= -128 && v <= 127)
        fc_e1(x, OI_PUSH8, (uint32_t)v & 0xFFu, 1);
    else
        fc_eh(x, OI_PUSH16, (uint32_t)v & 0xFFFFu, 1);
}

/* sim.cond_code: 0..64 = OFF FILL !FILL PRE !PRE NEI !NEI 1ST !1ST, the 21 percentages, then a:b (b = 2..8, a = 1..b) */
static uint32_t fc_cond_code(const uint8_t *p, uint32_t n)
{
    static const char *const words[9] = {"OFF", "FILL", "!FILL", "PRE", "!PRE", "NEI", "!NEI", "1ST", "!1ST"};
    uint32_t i, v = 0;
    if (n && p[n - 1u] == '%') {
        for (i = 0; i + 1u < n; i++)
            v = v * 10u + (uint32_t)(p[i] - '0');
        for (i = 0; i < OTC_PROBS_N; i++)
            if (otc_probs[i] == v)
                return 9u + i;
        return 0;
    }
    if (n == 3u && p[1] == ':') {
        uint32_t a = (uint32_t)(p[0] - '0'), b = (uint32_t)(p[2] - '0');
        return 28u + b * (b - 1u) / 2u + a;
    }
    for (i = 0; i < 9u; i++)
        if (cc_ci_eq(p, n, words[i]))
            return i;
    return 0;
}

static void fc_expr(fc_t *x, uint32_t n);
static void fc_mods(fc_t *x, uint32_t ln, uint32_t m);
static void fc_block(fc_t *x, uint32_t first);

static const otc_live_t *gen_live(const otc_comp_t *c, const otc_func_t *f);

/* `jnev end, kind, ch, num` (0x46): the guard of an `in` block, patched to its end label */
static void fc_jnev(fc_t *x, fc_lb_t *lb, uint32_t kind, uint32_t ch, uint32_t num)
{
    uint32_t at = x->pos + 4u;
    if (x->code) {
        fc_put(x, x->pos, OI_JNEV);
        fc_put(x, x->pos + 1u, kind);
        fc_put(x, x->pos + 2u, ch);
        fc_put(x, x->pos + 3u, num);
        fc_put(x, at, (lb->head < 0 ? LB_END : (uint32_t)lb->head) >> 8);
        fc_put(x, at + 1u, (lb->head < 0 ? LB_END : (uint32_t)lb->head) & 0xFFu);
        lb->head = (int32_t)at;
    }
    x->pos += 6u;
    fc_stack(x, 0);
    if (lb->depth == LB_NONE)
        lb->depth = x->depth;
}

/* `jngt end, name, n` (0x47): the guard of an `in gate NAME n:` block, name = pool offset */
static void fc_jngt(fc_t *x, fc_lb_t *lb, uint32_t off, uint32_t out)
{
    uint32_t at = x->pos + 4u;
    if (x->code) {
        fc_put(x, x->pos, OI_JNGT);
        fc_put(x, x->pos + 1u, off >> 8);
        fc_put(x, x->pos + 2u, off & 0xFFu);
        fc_put(x, x->pos + 3u, out);
        fc_put(x, at, (lb->head < 0 ? LB_END : (uint32_t)lb->head) >> 8);
        fc_put(x, at + 1u, (lb->head < 0 ? LB_END : (uint32_t)lb->head) & 0xFFu);
        lb->head = (int32_t)at;
    }
    x->pos += 6u;
    fc_stack(x, 0);
    if (lb->depth == LB_NONE)
        lb->depth = x->depth;
}

static const otc_in_t *fc_ib(const otc_comp_t *c, uint32_t fn, uint32_t ib)
{
    uint32_t q, k = 1;
    const otc_live_t *lv = gen_live(c, FN(c, fn));
    for (q = lv ? lv->ib_first : 0; q; q = PTR(c, otc_in_t, q)->next, k++)
        if (k == ib)
            return PTR(c, otc_in_t, q);
    return 0;
}

static uint32_t fc_str_off(fc_t *x, uint32_t ref)
{
    return x->pool ? pool_string(x->c, ref) : 0;
}

static uint32_t fc_list_off(fc_t *x, uint32_t ref)
{
    return x->pool ? pool_list(x->c, ref) : 0;
}

static void fc_call_node(fc_t *x, uint32_t n)
{
    otc_comp_t *c = x->c;
    uint32_t a, idx = 0;
    for (a = ND(c, n)->first; a; a = ND(c, a)->next)
        fc_expr(x, a);
    if (x->code)
        idx = FX(c, FIDX(c, ck_first_call_target(c, n)))->tabidx;
    fc_e1(x, OI_CALL, idx, 1 - (int32_t)ND(c, n)->val);
}

static void fc_trk_args(fc_t *x, uint32_t n)
{
    otc_comp_t *c = x->c;
    uint32_t a, id = otc_reg[ND(c, n)->op].id;
    const otc_node_t *a0 = ND(c, ND(c, n)->first);
    if (a0->kind == OTC_N_KW) {
        uint32_t tn;
        const uint8_t *tp = cc_sp(c, a0->text, &tn);
        fc_e3(x, OI_OPV, id >> 8, id & 0xFFu, cc_ci_eq(tp, tn, "CUR") ? 0u : 255u, 0);
        return;
    }
    for (a = ND(c, n)->first; a; a = ND(c, a)->next)
        fc_expr(x, a);
    fc_e3(x, OI_OPV, id >> 8, id & 0xFFu, ND(c, n)->val, -(int32_t)ND(c, n)->val);
}

static void fc_op_node(fc_t *x, uint32_t n)
{
    otc_comp_t *c = x->c;
    const otc_node_t *nd = ND(c, n);
    const otc_reg_t *r = &otc_reg[nd->op];
    uint32_t a, id = r->id;
    int32_t nargs = (int32_t)nd->val;
    if (r->flags & OTC_RF_LAZY) {                     /* ? : only the chosen branch runs */
        fc_lb_t els, end;
        uint32_t a1 = ND(c, nd->first)->next, a2 = ND(c, a1)->next;
        fc_lb_init(&els);
        fc_lb_init(&end);
        fc_expr(x, nd->first);
        fc_jump(x, &els, OI_SEL, -1, -1, 0);
        fc_expr(x, a1);
        fc_jump(x, &end, OI_JMP, -1, 0, 1);
        fc_place(x, &els);
        fc_expr(x, a2);
        fc_place(x, &end);
    } else if (nd->op == OTC_OP_DIR) {
        fc_expr(x, nd->first);
        fc_e1(x, OI_DIR, nargs > 1 ? 1u : 0u, -1);
    } else if (nd->op == OTC_OP_EXT) {
        uint32_t l = r->akw[0];
        for (a = nd->first; a; a = ND(c, a)->next) {
            uint32_t tn, k, at = 0;
            const uint8_t *tp = cc_sp(c, ND(c, a)->text, &tn);
            for (k = otc_kw_off[l]; k < otc_kw_off[l + 1u]; k++)
                if (cc_ci_eq(tp, tn, &otc_rstr[otc_kw_names[k]])) {
                    at = k - otc_kw_off[l];
                    break;
                }
            fc_e1(x, OI_PUSH8, at, 1);
        }
        fc_e1(x, OI_EXT, (uint32_t)nargs, -nargs);
    } else if (nd->op == OTC_OP_MATCH) {
        for (a = nd->first; a; a = ND(c, a)->next)
            fc_expr(x, a);
        fc_e1(x, OI_MATCH, (uint32_t)nargs, -nargs);
    } else if (nd->op == OTC_OP_MSG) {
        uint32_t mask = 0, i = 0, len;
        for (a = nd->first; a; a = ND(c, a)->next, i++) {
            fc_expr(x, a);
            if (ck_item_str(c, x->fn, FIDX(c, x->fn), ND(c, a), &len))
                mask |= 1u << i;
        }
        fc_e2(x, OI_MSG, (uint32_t)nargs, mask, -nargs);
    } else if (nd->op == OTC_OP_TRK) {
        fc_trk_args(x, n);
    } else if (nd->op == OTC_OP_STEPS) {
        const otc_node_t *a0 = ND(c, nd->first);
        if (a0->kind == OTC_N_KW) {
            fc_e3(x, OI_OPV, id >> 8, id & 0xFFu, 0, 0);                          /* STEPS_ALL */
        } else if (a0->kind == OTC_N_PRED) {
            uint32_t pid = otc_reg[a0->op].id;
            fc_eh(x, OI_PUSH16, pid, 1);
            fc_e3(x, OI_OPV, id >> 8, id & 0xFFu, 1, -1);                         /* STEPS_PRED */
        } else {
            for (a = nd->first; a; a = ND(c, a)->next)
                fc_expr(x, a);
            fc_e3(x, OI_OPV, id >> 8, id & 0xFFu, 2, -2);                         /* STEPS_RANGE */
        }
    } else {
        for (a = nd->first; a; a = ND(c, a)->next)
            fc_expr(x, a);
        fc_e2(x, OI_OP, id >> 8, id & 0xFFu, ((r->flags & OTC_RF_RET) ? 1 : 0) - nargs);
    }
}

static void fc_expr(fc_t *x, uint32_t n)
{
    otc_comp_t *c = x->c;
    const otc_node_t *nd = ND(c, n);
    switch (nd->kind) {
    case OTC_N_NUM:
        fc_push_int(x, (int32_t)nd->val);
        break;
    case OTC_N_STR:
        fc_eh(x, OI_PUSHK, fc_str_off(x, nd->text), 1);
        break;
    case OTC_N_LIST:
        fc_eh(x, OI_PUSHK, fc_list_off(x, nd->val), 1);
        break;
    case OTC_N_COND: {
        uint32_t tn;
        const uint8_t *tp = cc_sp(c, nd->val, &tn);
        fc_e1(x, OI_PUSHC, fc_cond_code(tp, tn), 1);
        break;
    }
    case OTC_N_VAR:
        fc_e1(x, OI_LD, fc_var(x, nd->text), 1);
        break;
    case OTC_N_VAR_NAMED:                                  /* a `var`: the frame's locals 16..31, by slot */
        fc_e1(x, OI_LD, 16u + nd->val, 1);
        break;
    case OTC_N_ARG: {
        uint32_t a = ck_find_arg(c, FN(c, x->fn), nd->text);
        fc_e1(x, OI_LDA, a == 0xFFFFFFFFu ? 0u : a, 1);
        break;
    }
    case OTC_N_PARAM: {
        uint32_t q, k = 0, tn;
        const uint8_t *tp = cc_sp(c, nd->text, &tn);
        for (q = FN(c, x->fn)->param_first; q; q = PTR(c, otc_param_t, q)->next, k++)
            if (cc_ref_eq(c, PTR(c, otc_param_t, q)->name, tp, tn, 0))
                break;
        fc_e1(x, OI_LDP, k, 1);
        break;
    }
    case OTC_N_CONST: {
        uint32_t tn, kc;
        const uint8_t *tp = cc_sp(c, nd->text, &tn);
        kc = cc_cmap_find(c, tp, tn, 0);
        if (kc && CN(c, kc)->type == OTC_CT_INT) {
            fc_push_int(x, (int32_t)CN(c, kc)->val);
        } else if (kc && CN(c, kc)->type == OTC_CT_COND) {
            uint32_t cn;
            const uint8_t *cp = cc_sp(c, CN(c, kc)->val, &cn);
            fc_e1(x, OI_PUSHC, fc_cond_code(cp, cn), 1);
        } else if (kc && CN(c, kc)->type == OTC_CT_LIST) {
            fc_eh(x, OI_PUSHK, fc_list_off(x, CN(c, kc)->val), 1);
        } else {
            fc_eh(x, OI_PUSHK, kc ? fc_str_off(x, CN(c, kc)->val) : 0u, 1);
        }
        break;
    }
    case OTC_N_OP:
        fc_op_node(x, n);
        break;
    case OTC_N_CALL:
        fc_call_node(x, n);
        break;
    default:
        break;
    }
}

/* passes(node, body): the body is what the caller does between fc_passes_begin and fc_passes_end */
static void fc_passes_begin(fc_t *x, uint32_t node, fc_pass_t *ps)
{
    fc_trk_args(x, node);
    fc_lb_init(&ps->lb);
    fc_jump(x, &ps->lb, OI_PASS, -1, 0, 0);
    fc_frame_in(x);
    ps->start = x->pos;
}

static void fc_passes_end(fc_t *x, fc_pass_t *ps)
{
    fc_eh(x, OI_PNEXT, ps->start, 0);
    fc_frame_out(x);
    fc_place(x, &ps->lb);
}

static void fc_stmt(fc_t *x, uint32_t s)
{
    otc_comp_t *c = x->c;
    const otc_node_t *nd = ND(c, s);
    switch (nd->kind) {
    case OTC_N_ASSIGN:
        fc_expr(x, nd->first);
        fc_e1(x, OI_ST, fc_var(x, nd->text), -1);
        break;
    case OTC_N_ASSIGN_VAR_NAMED:
        fc_expr(x, nd->first);
        fc_e1(x, OI_ST, 16u + nd->val, -1);
        break;
    case OTC_N_OPSTMT:
        fc_op_node(x, nd->first);
        if (otc_reg[ND(c, nd->first)->op].flags & OTC_RF_RET)
            fc_e0(x, OI_DROP, -1);
        break;
    case OTC_N_CALLSTMT:
        fc_call_node(x, nd->first);
        fc_e0(x, OI_DROP, -1);
        break;
    case OTC_N_CTXSTMT:
        if (ND(c, nd->first)->op == OTC_OP_TRK) {
            fc_pass_t ps;
            fc_passes_begin(x, nd->first, &ps);
            fc_passes_end(x, &ps);
        } else {
            fc_op_node(x, nd->first);
        }
        break;
    case OTC_N_BREAK:
        fc_e0(x, OI_BREAK, 0);
        break;
    case OTC_N_STOP:
        fc_e0(x, OI_STOP, 0);
        break;
    case OTC_N_RET:
        if (nd->first)
            fc_expr(x, nd->first);
        else
            fc_push_int(x, 0);
        fc_e0(x, OI_RET, -1);
        break;
    case OTC_N_SETSTMT: {
        uint32_t id = otc_reg[nd->op].id, a, cnt = 0;
        for (a = nd->first; a; a = ND(c, a)->next) {
            fc_expr(x, a);
            cnt++;
        }
        fc_e2(x, OI_SET, id >> 8, id & 0xFFu, -(int32_t)cnt);
        break;
    }
    default:
        break;
    }
}

static int fc_stmt_changes_ctx(const otc_comp_t *c, const otc_line_t *l)     /* a ctx statement other than trk */
{
    return l->body && !(l->flags & OTC_LF_DECL) && ND(c, l->body)->kind == OTC_N_CTXSTMT &&
           ND(c, ND(c, l->body)->first)->op != OTC_OP_TRK;
}

static void fc_children(fc_t *x, uint32_t ln)
{
    otc_comp_t *c = x->c;
    const otc_line_t *l = LN(c, ln);
    uint32_t k;
    int need = 0;
    for (k = l->child_first; k && !need; k = LN(c, k)->next) {
        const otc_line_t *cl = LN(c, k);
        if (!(cl->flags & (OTC_LF_DECL | OTC_LF_ERROR)) && !cl->mods && fc_stmt_changes_ctx(c, cl))
            need = 1;
    }
    if (need)
        fc_scope_in(x);
    fc_block(x, l->child_first);
    if (need)
        fc_scope_out(x);
    fc_mark(x, l->no);
}

static void fc_mods(fc_t *x, uint32_t ln, uint32_t m)
{
    otc_comp_t *c = x->c;
    const otc_line_t *l = LN(c, ln);
    uint8_t kind;
    if (x->err)
        return;
    if (!m) {
        if (l->body && !(l->flags & OTC_LF_DECL))
            fc_stmt(x, l->body);
        else
            fc_children(x, ln);
        return;
    }
    kind = ND(c, m)->kind;
    if (kind == OTC_N_M_IF || kind == OTC_N_M_ELIF || kind == OTC_N_M_PROB) {
        fc_lb_t skip;
        fc_lb_init(&skip);
        fc_expr(x, ND(c, m)->first);
        fc_jump(x, &skip, kind == OTC_N_M_PROB ? OI_PROB : OI_JZ, -1, -1, 0);
        fc_mods(x, ln, ND(c, m)->next);
        fc_place(x, &skip);
    } else if (kind == OTC_N_M_EVERY || kind == OTC_N_M_SKIP) {
        fc_lb_t skip;
        fc_lb_init(&skip);
        fc_expr(x, ND(c, m)->first);
        x->nctr++;
        if (x->nctr > 255u) {                           /* the counter index is one byte */
            x->err = 1;
            return;
        }
        if (x->ctr) {                                   /* the counter's record: its line and the index of the modifier */
            uint32_t midx = 0, q;
            uint8_t *p = x->ctr + 4u * (x->nctr - 1u);
            for (q = l->mods; q != m; q = ND(c, q)->next)
                midx++;
            p[0] = (uint8_t)(l->no >> 8);
            p[1] = (uint8_t)l->no;
            p[2] = (uint8_t)midx;
            p[3] = 0;
        }
        fc_jump(x, &skip, kind == OTC_N_M_EVERY ? OI_EVERY : OI_SKIP, (int32_t)(x->nctr - 1u), -1, 0);
        fc_mods(x, ln, ND(c, m)->next);
        fc_place(x, &skip);
    } else if (kind == OTC_N_M_L) {
        fc_lb_t end;
        uint32_t a0 = ND(c, m)->first, start;
        fc_lb_init(&end);
        fc_expr(x, a0);
        fc_expr(x, ND(c, a0)->next);
        fc_jump(x, &end, OI_LBEG, -1, -2, 0);
        fc_frame_in(x);
        start = x->pos;
        fc_mods(x, ln, ND(c, m)->next);
        fc_mark(x, l->no);
        fc_eh(x, OI_LNEXT, start, 0);
        fc_frame_out(x);
        fc_place(x, &end);
    } else if (kind == OTC_N_M_EACH) {
        fc_lb_t end;
        uint32_t start;
        fc_lb_init(&end);
        fc_jump(x, &end, OI_EBEG, -1, 0, 0);
        fc_frame_in(x);
        start = x->pos;
        fc_mods(x, ln, ND(c, m)->next);
        fc_mark(x, l->no);
        fc_eh(x, OI_ENEXT, start, 0);
        fc_frame_out(x);
        fc_place(x, &end);
    } else if (kind == OTC_N_M_CTX) {
        uint32_t node = ND(c, m)->first;
        if (ND(c, node)->op == OTC_OP_TRK) {
            fc_pass_t ps;
            fc_passes_begin(x, node, &ps);
            fc_mods(x, ln, ND(c, m)->next);
            fc_passes_end(x, &ps);
        } else {
            fc_op_node(x, node);
            fc_mods(x, ln, ND(c, m)->next);
        }
    }
}

static void fc_scoped(fc_t *x, uint32_t ln, uint32_t m)
{
    otc_comp_t *c = x->c;
    const otc_line_t *l = LN(c, ln);
    uint32_t k;
    int need = fc_stmt_changes_ctx(c, l);
    for (k = m; k && !need; k = ND(c, k)->next)
        if (ND(c, k)->kind == OTC_N_M_CTX && ND(c, ND(c, k)->first)->op != OTC_OP_TRK)
            need = 1;
    if (need)
        fc_scope_in(x);
    fc_mods(x, ln, m);
    if (need)
        fc_scope_out(x);
}

static int fc_is_trk_line(const otc_comp_t *c, const otc_line_t *l)
{
    return !l->mods && l->body && !(l->flags & OTC_LF_DECL) && ND(c, l->body)->kind == OTC_N_CTXSTMT &&
           ND(c, ND(c, l->body)->first)->op == OTC_OP_TRK;
}

static int fc_is_chain_kind(int k)
{
    return k == OTC_N_M_ELIF || k == OTC_N_M_ELSE;
}

/* chain(): the lines starting at `first` (an `if` line, then elif / else lines, ending with the first else); returns
 * the line after the chain */
static uint32_t fc_chain(fc_t *x, uint32_t first)
{
    otc_comp_t *c = x->c;
    fc_lb_t end;
    uint32_t cur = first, nxt;
    fc_lb_init(&end);
    for (;;) {
        const otc_line_t *cl = LN(c, cur);
        int ck = cc_first_kind(c, cl), last;
        fc_lb_t nl;
        fc_lb_init(&nl);
        nxt = cl->next;
        last = ck == OTC_N_M_ELSE || !nxt || !fc_is_chain_kind(cc_first_kind(c, LN(c, nxt)));
        fc_mark(x, cl->no);
        if (ck != OTC_N_M_ELSE) {
            fc_expr(x, ND(c, cl->mods)->first);
            fc_jump(x, last ? &end : &nl, OI_JZ, -1, -1, 0);
        }
        fc_scoped(x, cur, ND(c, cl->mods)->next);
        if (x->err)
            return nxt;
        if (!last) {
            fc_jump(x, &end, OI_JMP, -1, 0, 1);
            fc_place(x, &nl);
        }
        if (last)
            break;
        cur = nxt;
    }
    fc_place(x, &end);
    return nxt;
}

static void fc_block(fc_t *x, uint32_t first)
{
    otc_comp_t *c = x->c;
    uint32_t ln = first, mark = c->top, ntrk = 0, np = 0, k, pend = 0;
    for (k = first; k; k = LN(c, k)->next)
        if (fc_is_trk_line(c, LN(c, k)))
            ntrk++;
    if (ntrk) {
        pend = cc_alloc(c, (uint32_t)sizeof(fc_pass_t) * ntrk);
        if (!pend)
            return;
    }
    while (ln && !x->err) {
        const otc_line_t *l = LN(c, ln);
        int fk;
        if (l->flags & (OTC_LF_DECL | OTC_LF_ERROR)) {
            ln = l->next;
            continue;
        }
        fk = cc_first_kind(c, l);
        if (fk == OTC_N_M_IF) {
            ln = fc_chain(x, ln);
            continue;
        }
        if (fk == OTC_N_M_ELIF || fk == OTC_N_M_ELSE) {      /* (an else without if: the checker refused the function) */
            ln = l->next;
            continue;
        }
        if (l->ib) {                                          /* an `in` block: a guard, then its body (no mark) */
            const otc_in_t *b = fc_ib(c, x->fn, l->ib);
            fc_lb_t end;
            end.depth = LB_NONE;
            end.head = -1;
            if (!b) {
                x->err = 1;
                return;
            }
            if (b->kind == OTC_IK_GATE) {
                uint32_t noff = x->pool ? pool_string(c, b->name) : 0;   /* no pool before the pool pass */
                fc_jngt(x, &end, noff, b->ch);
            }
            else
                fc_jnev(x, &end, (uint32_t)b->kind, b->ch, b->num);
            fc_block(x, l->child_first);
            fc_place(x, &end);
            ln = l->next;
            continue;
        }
        fc_mark(x, l->no);
        if (fc_is_trk_line(c, l)) {                           /* the rest of the block runs once per track */
            fc_passes_begin(x, ND(c, l->body)->first, PTR(c, fc_pass_t, pend) + np);
            np++;
            ln = l->next;
            continue;
        }
        if (l->mods)
            fc_scoped(x, ln, l->mods);
        else
            fc_mods(x, ln, 0);
        ln = l->next;
    }
    while (np) {
        np--;
        fc_passes_end(x, PTR(c, fc_pass_t, pend) + np);
    }
    if (ntrk && !x->pool)
        c->top = mark;                                        /* nothing else was kept since (the pool keeps its entries) */
}

enum { CE_NONE = 0, CE_COUNTERS = 1, CE_STACK = 2, CE_CODE = 3, CE_CTL = 4, CE_CTX = 5, CE_MENUS = 6 };   /* otc_fx_t.cerr */

/* the code of a function. Dry run (all the targets NULL): fills the results of its otc_fx_t. `pool`: add the constants
 * to the pool. `code` / `lines` / `ctr`: write the bytes (the sizes of the dry run must have been taken first). */
CC_NOINLINE static void fc_compile(otc_comp_t *c, uint32_t fi, int pool, uint8_t *code, uint8_t *lines, uint32_t lcap,
                                   uint8_t *ctr)
{
    fc_t x;
    otc_fx_t *fx = FX(c, fi);
    const otc_func_t *f = FN(c, fx->func);
    cc_zero(&x, (uint32_t)sizeof x);
    x.c = c;
    x.fn = fx->func;
    x.cur = x.maxdepth_line = x.maxctx_line = x.maxctl_line = f->line;
    x.ctx = x.maxctx = 1;                                     /* the function's own context counts */
    x.pool = (uint8_t)pool;
    x.code = code;
    x.lines = lines;
    x.lcap = lcap;
    x.ctr = ctr;
    fc_mark(&x, f->line);
    fc_block(&x, f->root_first);
    fc_e0(&x, OI_STOP, 0);
    if (pool || code)
        return;
    fx->cerr = CE_NONE;
    fx->cerr_line = 0;
    if (x.err) {
        fx->cerr = CE_COUNTERS;
        fx->cerr_line = f->line;
    } else if (x.maxdepth > 16) {
        fx->cerr = CE_STACK;
        fx->cerr_line = x.maxdepth_line;
    } else if (x.maxctl > 16u) {                              /* what the VM refuses at load (OTC_MAX_CTL / OTC_MAX_CTX) */
        fx->cerr = CE_CTL;
        fx->cerr_line = x.maxctl_line;
    } else if (x.maxctx > 16u) {
        fx->cerr = CE_CTX;
        fx->cerr_line = x.maxctx_line;
    } else if (x.pos > 0xFFFFu) {
        fx->cerr = CE_CODE;
        fx->cerr_line = f->line;
    } else if (f->nmenus > 255u) {                            /* the count is one byte of the pool entry */
        fx->cerr = CE_MENUS;
        fx->cerr_line = f->line;
    }
    fx->code_len = x.pos;
    fx->nlines = x.nlines - ((x.nlines && x.last_pc == x.pos) ? 1u : 0u);
    fx->nctr = x.nctr;
    fx->maxstack = (uint32_t)x.maxdepth;
    fx->maxctl = x.maxctl;
    fx->maxctx = x.maxctx;
    fx->nlocals = x.nlocals;
}

/* ---- the pool ---------------------------------------------------------------------------------------------- */
typedef struct { uint32_t next, hash, kind, ref, size, off, onext; } pe_t;
#define POOL_BUCKETS 1024u
enum { P_STR = 1, P_LIST = 2, P_MENU = 4 };

/* Add a pool entry (or find it) and return its offset in the pool (2 = the first: the pool starts with two reserved bytes,
 * so that no entry has offset 0). The key is the kind and the bytes of the string `ref` (fresh: the ref was just built at
 * the top of the arena, take it back when the entry exists); bodysize = bytes of the entry before the padding to 2.
 * Strings, lists and menu lists: each once, in the order of first use, 2-aligned (otb.Pool). 0: the arena is full. */
static uint32_t pool_add(otc_comp_t *c, uint32_t kind, uint32_t ref, uint32_t bodysize, int fresh)
{
    uint32_t n, h, e, i;
    const uint8_t *p = cc_sp(c, ref, &n);
    h = kind * 2654435761u;
    for (i = 0; i < n; i++)
        h = (h ^ p[i]) * 16777619u;
    for (e = PTR(c, uint32_t, c->pool)[h % POOL_BUCKETS]; e; e = PTR(c, pe_t, e)->next) {
        const pe_t *pe = PTR(c, pe_t, e);
        uint32_t m, off;
        const uint8_t *q;
        if (pe->hash != h || pe->kind != kind)
            continue;
        q = cc_sp(c, pe->ref, &m);
        if (m != n)
            continue;
        for (i = 0; i < n && p[i] == q[i]; i++)
            ;
        if (i == n) {
            off = pe->off;
            if (fresh)
                cc_drop_msg(c, ref);
            return off;
        }
    }
    e = cc_alloc(c, (uint32_t)sizeof(pe_t));
    if (!e)
        return 0;
    PTR(c, pe_t, e)->hash = h;
    PTR(c, pe_t, e)->kind = kind;
    PTR(c, pe_t, e)->ref = ref;
    PTR(c, pe_t, e)->size = bodysize;
    PTR(c, pe_t, e)->off = c->pool_bytes;
    PTR(c, pe_t, e)->next = PTR(c, uint32_t, c->pool)[h % POOL_BUCKETS];
    PTR(c, uint32_t, c->pool)[h % POOL_BUCKETS] = e;
    if (c->pool_last)
        PTR(c, pe_t, c->pool_last)->onext = e;
    else
        c->pool_first = e;
    c->pool_last = e;
    c->npool++;
    c->pool_bytes += (bodysize + 1u) & ~1u;
    return PTR(c, pe_t, e)->off;
}

static uint32_t pool_string(otc_comp_t *c, uint32_t ref)
{
    uint32_t n;
    (void)cc_sp(c, ref, &n);
    return pool_add(c, P_STR, ref, n + 2u, 0);
}

static uint32_t pool_list(otc_comp_t *c, uint32_t ref)       /* the ref holds two bytes per value */
{
    uint32_t n;
    (void)cc_sp(c, ref, &n);
    return pool_add(c, P_LIST, ref, n + 2u, 0);
}

/* the entries of a function that come before its code: its file name, its menus, the names of its params and arguments;
 * then its code (the pool pass: em == 0) or the table's data of the function (emission) */
typedef struct {
    uint8_t *aux, *lines, *code;     /* the regions of the table */
    uint32_t auxr, liner, coder;     /* offsets of this function in them */
    uint32_t file_off, menu_off;
} em_t;

static uint32_t gen_menus(otc_comp_t *c, const otc_func_t *f)    /* pool.menus: ONE entry for all the menus of the function */
{
    uint32_t m, ref, bn;
    mb_begin(c);
    mb_byte(c, P_MENU);
    mb_byte(c, (uint8_t)f->nmenus);
    for (m = f->menu_first; m; m = PTR(c, otc_menu_t, m)->next) {
        const otc_menu_t *mn = PTR(c, otc_menu_t, m);
        uint32_t cell, sn;
        const uint8_t *sp;
        mb_byte(c, (uint8_t)(mn->nlevels + 1u));
        for (cell = mn->lfirst; cell; cell = PTR(c, otc_cell_t, cell)->next) {
            sp = cc_sp(c, PTR(c, otc_cell_t, cell)->ref, &sn);
            mb_byte(c, (uint8_t)sn);
            mb_bytes(c, sp, sn);
        }
    }
    ref = mb_end(c);
    if (!ref)
        return 0;
    (void)cc_sp(c, ref, &bn);
    return pool_add(c, P_MENU, ref, bn, 1);
}

static void wr16(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

static void wr32(uint8_t *p, uint32_t v)
{
    wr16(p, v >> 16);
    wr16(p + 2, v & 0xFFFFu);
}

static int gen_has_typed(otc_comp_t *c, const otc_func_t *f)
{
    uint32_t q;
    for (q = f->param_first; q; q = PTR(c, otc_param_t, q)->next) {
        if (PTR(c, otc_param_t, q)->kind != OTC_PK_INT)
            return 1;
    }
    return 0;
}

static uint32_t gen_typed_aux_len(otc_comp_t *c, const otc_func_t *f)
{
    uint32_t q, len = 0;
    if (!gen_has_typed(c, f))
        return 0;
    len += 2u;
    for (q = f->param_first; q; q = PTR(c, otc_param_t, q)->next) {
        const otc_param_t *pa = PTR(c, otc_param_t, q);
        if (pa->kind == OTC_PK_FOLDER)
            len += 2u;
        else if (pa->kind == OTC_PK_OPTION)
            len += 2u + 2u * pa->nlabels;
    }
    return len;
}

static const otc_live_t *gen_live(const otc_comp_t *c, const otc_func_t *f)
{
    return f->module_label ? PTR(c, otc_live_t, f->module_label) : 0;
}

static int gen_has_param_src(const otc_comp_t *c, const otc_func_t *f)
{
    uint32_t q;
    for (q = f->param_first; q; q = PTR(c, otc_param_t, q)->next)
        if (PTR(c, otc_param_t, q)->src_kind)
            return 1;
    return 0;
}

/* the aux bytes of a live script that come after `confirm`: its vars, its param sources, its `launch` ticks */
static uint32_t gen_live_aux_len(const otc_comp_t *c, const otc_func_t *f)
{
    uint32_t len = 0;
    const otc_live_t *lv = gen_live(c, f);
    if (lv && lv->nvars)
        len += 2u + 2u * lv->nvars;
    if (gen_has_param_src(c, f))
        len += 4u * f->nparams;
    if (lv && lv->launch_line)
        len += 2u;
    return len;
}

CC_NOINLINE static void gen_func_pool(otc_comp_t *c, uint32_t i, em_t *em)
{
    const otc_fx_t *fx = FX(c, i);
    const otc_func_t *f = FNI(c, i);
    uint32_t q, a, ref, k, l, file_off = 0, menu_off = 0, page_off = 0, module_off = 0, conf_off = 0;
    uint8_t *ap = em ? em->aux + em->auxr : 0;
    ref = cc_str(c, c->src[f->file].label, c->src[f->file].label_len, 0);
    if (!ref)
        return;
#ifdef OTC_SABOTAGE_GEN
    if (GEN_SAB == 1 && f->nmenus)
        menu_off = gen_menus(c, f);
#endif
    file_off = pool_add(c, P_STR, ref, c->src[f->file].label_len + 2u, 1);
#ifdef OTC_SABOTAGE_GEN
    if (GEN_SAB == 3 && f->page_line)                        /* wrong on purpose: the page label before the menu list */
        page_off = pool_string(c, f->page_label);
#endif
    if (f->nmenus && !(GEN_SAB == 1))
        menu_off = gen_menus(c, f);
#ifdef OTC_SABOTAGE_GEN
    if (GEN_SAB == 4 && f->confirm_line)                     /* wrong on purpose: the confirm text before the page label */
        conf_off = pool_string(c, f->confirm_text);
#endif
    if (f->page_line && !(GEN_SAB == 3))                     /* the page label follows the menu list in the pool */
        page_off = pool_string(c, f->page_label);
    if (f->module_line)
        module_off = pool_string(c, f->name);
    if (f->confirm_line && !(GEN_SAB == 4))                  /* then the confirm text (spec 14.1) */
        conf_off = pool_string(c, f->confirm_text);
    for (q = f->param_first, k = 0; q; q = PTR(c, otc_param_t, q)->next, k++) {
        const otc_param_t *pa = PTR(c, otc_param_t, q);
        uint32_t off = pool_string(c, pa->name);
        uint32_t pdef = pa->def;
        uint32_t plo = pa->lo;
        uint32_t phi = pa->hi;
        if (pa->kind == OTC_PK_FOLDER) {
            pdef = pool_string(c, pa->path);
            plo = 0;
            phi = 0;
        }
        if (ap) {
            wr16(ap + 8u * k, off);
            wr16(ap + 8u * k + 2u, pdef & 0xFFFFu);
            wr16(ap + 8u * k + 4u, plo & 0xFFFFu);
            wr16(ap + 8u * k + 6u, phi & 0xFFFFu);
        }
    }
    for (a = 0; a < f->nargs; a++) {
        uint32_t off = pool_string(c, cc_arg_ref(c, f, a));
        if (ap)
            wr16(ap + 8u * f->nparams + 2u * a, off);
    }
    if (em) {
        uint32_t aux_tail = 8u * f->nparams + 2u * f->nargs + 4u * fx->nctr;
        em->file_off = file_off;
        em->menu_off = menu_off;
        fc_compile(c, i, 1, em->code + em->coder, em->lines + 4u * em->liner, fx->nlines,
                   ap + 8u * f->nparams + 2u * f->nargs);
        if (f->page_line) {                                  /* the label's pool offset */
            wr16(ap + aux_tail, page_off);
            aux_tail += 2u;
        }
        if (f->module_line) {                                /* pool offset (2B) + clock ticks (2B) */
            wr16(ap + aux_tail, module_off);
            wr16(ap + aux_tail + 2u, f->clock_ticks & 0xFFFFu);
            aux_tail += 4u;
        }
        if (f->confirm_line) {                               /* then the confirm text's */
            wr16(ap + aux_tail, conf_off);
            aux_tail += 2u;
        }
        if (gen_live(c, f) && gen_live(c, f)->nvars) {        /* a live script's vars: count, then their init values */
            uint32_t v;
            const otc_live_t *lv = gen_live(c, f);
            wr16(ap + aux_tail, lv->nvars);
            aux_tail += 2u;
            for (v = lv->var_first; v; v = PTR(c, otc_var_t, v)->next) {
                wr16(ap + aux_tail, (uint32_t)PTR(c, otc_var_t, v)->init & 0xFFFFu);
                aux_tail += 2u;
            }
        }
        if (gen_has_param_src(c, f)) {                       /* 4 bytes per param, in declaration order */
            uint32_t q;
            for (q = f->param_first; q; q = PTR(c, otc_param_t, q)->next) {
                const otc_param_t *pa = PTR(c, otc_param_t, q);
                ap[aux_tail] = pa->src_kind;
                ap[aux_tail + 1u] = pa->src_ch;
                ap[aux_tail + 2u] = pa->src_num;
                ap[aux_tail + 3u] = 0;
                aux_tail += 4u;
            }
        }
        if (gen_live(c, f) && gen_live(c, f)->launch_line) {  /* `launch N`: its ticks end the aux data */
            wr16(ap + aux_tail, gen_live(c, f)->launch_ticks & 0xFFFFu);
            aux_tail += 2u;
        }
    } else {
        fc_compile(c, i, 1, 0, 0, 0, 0);
    }
    if (gen_has_typed(c, f)) {
        uint32_t aux_base = 8u * f->nparams + 2u * f->nargs + 4u * fx->nctr
                          + (f->page_line ? 2u : 0u) + (f->module_line ? 4u : 0u) + (f->confirm_line ? 2u : 0u)
                          + gen_live_aux_len(c, f);
        uint16_t types_mask = 0;
        for (q = f->param_first, k = 0; q; q = PTR(c, otc_param_t, q)->next, k++) {
            const otc_param_t *pa = PTR(c, otc_param_t, q);
            uint32_t code = 0;
            if (pa->kind == OTC_PK_BOOL)
                code = 1;
            else if (pa->kind == OTC_PK_OPTION)
                code = 2;
            else if (pa->kind == OTC_PK_FOLDER)
                code = 3;
            types_mask |= (uint16_t)(code << (2u * k));
        }
        if (ap)
            wr16(ap + aux_base, types_mask);
        aux_base += 2u;
        for (q = f->param_first; q; q = PTR(c, otc_param_t, q)->next) {
            const otc_param_t *pa = PTR(c, otc_param_t, q);
            if (pa->kind == OTC_PK_FOLDER) {
                uint32_t poff = pool_string(c, pa->path);
                if (ap)
                    wr16(ap + aux_base, poff);
                aux_base += 2u;
            }
        }
        for (q = f->param_first; q; q = PTR(c, otc_param_t, q)->next) {
            const otc_param_t *pa = PTR(c, otc_param_t, q);
            if (pa->kind == OTC_PK_OPTION) {
                if (ap)
                    wr16(ap + aux_base, pa->nlabels);
                aux_base += 2u;
                for (l = 0; l < pa->nlabels; l++) {
                    uint32_t loff = pool_string(c, pa->labels[l]);
                    if (ap)
                        wr16(ap + aux_base, loff);
                    aux_base += 2u;
                }
            }
        }
    }
}

/* ---- compile_program: what is refused, and the size of the table ---------------------------------------- */
typedef struct { uint32_t line, op; int found, profile_m1; } pr_t;

#ifdef OTC_TEST_NOT_M1            /* a test build: this registry index counts as outside the profile (no op is, otherwise) */
#define GEN_NOT_M1(i) ((i) == (OTC_TEST_NOT_M1))
#else
#define GEN_NOT_M1(i) 0
#endif

static void gen_profile_visit(otc_comp_t *c, void *ctx, uint32_t node, uint32_t line)
{
    pr_t *pr = (pr_t *)ctx;
    const otc_node_t *nd = ND(c, node);
    if (pr->found || !pr->profile_m1)
        return;
    if ((nd->kind == OTC_N_OP || nd->kind == OTC_N_PRED || nd->kind == OTC_N_SETSTMT) && (!(otc_reg[nd->op].flags & OTC_RF_M1) || GEN_NOT_M1(nd->op))) {
        pr->found = 1;
        pr->line = line;
        pr->op = nd->op;
    }
}

static void gen_table_err(otc_comp_t *c, const char *text, uint32_t n)
{
    mb_begin(c);
    mb_lit(c, text);
    if (n) {
        mb_dec(c, (int32_t)n);
        mb_lit(c, " bytes (> 32768)");
    }
    cc_file_err(c, OTC_FILE_TABLE, 0, mb_end(c));
}

CC_NOINLINE static void gen_diags(otc_comp_t *c)
{
    uint32_t i, e, nok = 0, aux = 0, lines = 0, code = 0, nctr = 0;
    int changed;
    for (i = 0; i < c->nf && !c->err; i++) {                 /* 1. what cannot be compiled */
        otc_fx_t *fx = FX(c, i);
        const otc_func_t *f = FNI(c, i);
        pr_t pr;
        if (!(f->flags & OTC_FF_REG) || f->nerr)
            continue;
        fx->inorder = 1;
        fc_compile(c, i, 0, 0, 0, 0, 0);
        pr.found = 0;
        pr.line = pr.op = 0;
        pr.profile_m1 = c->profile == OTC_PROFILE_M1;
        ck_walk_func(c, fx->func, gen_profile_visit, &pr);
        if (pr.found) {
            fx->bad = 1;
            fx->bline = pr.line;
            fx->barg = pr.op;
        } else if (fx->cerr) {
            fx->bad = 2;
            fx->bline = fx->cerr_line;
        }
    }
    do {                                                     /* a function that calls a refused one is refused */
        changed = 0;
        for (i = 0; i < c->nf; i++) {
            otc_fx_t *fx = FX(c, i);
            if (!fx->inorder || fx->bad)
                continue;
            for (e = 0; e < fx->ncg; e++) {
                const otc_edge_t *ed = PTR(c, otc_edge_t, fx->cg) + e;
                if (FX(c, ed->g)->bad) {
                    fx->bad = 3;
                    fx->bline = ed->line;
                    fx->barg = ed->g;
                    changed = 1;
                    break;
                }
            }
        }
    } while (changed);
    for (i = 0; i < c->nf; i++)
        if (FX(c, i)->inorder && !FX(c, i)->bad) {
            FX(c, i)->tabidx = nok++;                        /* the index of a call: the rank among the compiled functions */
            nctr += FX(c, i)->nctr;
        }
    c->ntab_funcs = nok;
    if (nok > 255u) {
        c->tab_err = 1;
        gen_table_err(c, "more than 255 functions", 0);
        return;
    }
    for (i = 0; i < c->nf; i++)                              /* a file label is a string of the pool: at most 255 bytes */
        if (FX(c, i)->inorder && !FX(c, i)->bad && c->src[FNI(c, i)->file].label_len > 255u) {
            c->tab_err = 2;
            gen_table_err(c, "string too long for the pool", 0);
            return;
        }
    if (nctr > 1024u) {                                      /* the VM takes at most 1024 counters (OTC_MAX_COUNTERS) */
        c->tab_err = 4;
        gen_table_err(c, "more than 1024 every/skip counters in the table", 0);
        return;
    }
    for (i = 0; i < c->nf && !c->err; i++) {                 /* the diagnostics, in the order of the functions */
        otc_fx_t *fx = FX(c, i);
        if (!fx->bad)
            continue;
        mb_begin(c);
        if (fx->bad == 1) {
            mb_lit(c, "not on the unit yet: ");
            mb_rstr(c, otc_reg[fx->barg].shown);
        } else if (fx->bad == 2) {
            if (fx->cerr == CE_COUNTERS) {
                mb_lit(c, "more than 255 every/skip counters");
            } else if (fx->cerr == CE_STACK) {
                mb_lit(c, "stack depth ");
                mb_dec(c, (int32_t)fx->maxstack);
                mb_lit(c, " > 16");
            } else if (fx->cerr == CE_CTL) {
                mb_lit(c, "loop/pass depth ");
                mb_dec(c, (int32_t)fx->maxctl);
                mb_lit(c, " > 16");
            } else if (fx->cerr == CE_CTX) {
                mb_lit(c, "context depth ");
                mb_dec(c, (int32_t)fx->maxctx);
                mb_lit(c, " > 16");
            } else if (fx->cerr == CE_MENUS) {
                mb_lit(c, "more than 255 menu lines");
            } else {
                mb_lit(c, "code too long (");
                mb_dec(c, (int32_t)fx->code_len);
                mb_lit(c, " bytes)");
            }
        } else {
            mb_lit(c, "needs ");
            mb_show_ref(c, FNI(c, fx->barg)->name);
        }
        cc_func_err(c, fx->func, fx->bline, mb_end(c), 0);
    }
    /* the pool and the size of the table (the real thing, in the order of the functions) */
    c->pool = cc_alloc(c, POOL_BUCKETS * 4u);
    if (!c->pool)
        return;
    c->pool_bytes = 2;
    for (i = 0; i < c->nf && !c->err; i++) {
        const otc_fx_t *fx = FX(c, i);
        const otc_func_t *f = FNI(c, i);
        if (!fx->inorder || fx->bad)
            continue;
        gen_func_pool(c, i, 0);
        aux += 8u * f->nparams + 2u * f->nargs + 4u * fx->nctr + (f->page_line ? 2u : 0u) + (f->module_line ? 4u : 0u) + (f->confirm_line ? 2u : 0u) + gen_live_aux_len(c, f) + gen_typed_aux_len(c, f);
        lines += fx->nlines;
        code += fx->code_len;
    }
    if (c->err)
        return;
    c->tab_total = 32u + c->pool_bytes + 54u * nok + aux + 4u * lines + code;
    if (c->tab_total > 32768u) {
        c->tab_err = 3;
        gen_table_err(c, "compiled table is ", c->tab_total);
    }
}

/* ---- the table --------------------------------------------------------------------------------------- */
typedef struct { uint32_t fam, tch, nsm; } mk_t;

static void gen_mask_visit(otc_comp_t *c, void *ctx, uint32_t node, uint32_t line)
{
    mk_t *mk = (mk_t *)ctx;
    const otc_node_t *nd = ND(c, node);
    (void)line;
    if (nd->kind == OTC_N_OP || nd->kind == OTC_N_PRED || nd->kind == OTC_N_SETSTMT) {
        const otc_reg_t *r = &otc_reg[nd->op];
        mk->fam |= 1u << r->family;
        mk->tch |= r->touches < 3u ? 1u << r->touches : 0u;
        mk->nsm |= 1u << (r->id >> 8);
    }
}

/* static_masks: the union over the function and everything it calls (transitively) of the families, the touches and the
 * namespaces of its ops. Depth <= 9 (an accepted function calls at most 8 deep; `masked` stops any cycle). */
static void gen_masks(otc_comp_t *c, uint32_t i)
{
    otc_fx_t *fx = FX(c, i);
    mk_t mk;
    uint32_t e;
    if (fx->masked)
        return;
    fx->masked = 1;
    mk.fam = mk.tch = mk.nsm = 0;
    ck_walk_func(c, fx->func, gen_mask_visit, &mk);
    fx->fam = mk.fam;
    fx->tch = mk.tch;
    fx->nsm = mk.nsm;
    for (e = 0; e < fx->ncg; e++) {
        const otc_edge_t *ed = PTR(c, otc_edge_t, fx->cg) + e;
        const otc_fx_t *gx;
        gen_masks(c, ed->g);
        gx = FX(c, ed->g);
        fx->fam |= gx->fam;
        fx->tch |= gx->tch;
        fx->nsm |= gx->nsm;
    }
}

/* hosts bit GATE: the live script has an `in gate` block (the cascade wakes it) */
static int gen_gate_host(otc_comp_t *c, const otc_func_t *f)
{
    const otc_live_t *lv = f->module_label ? PTR(c, otc_live_t, f->module_label) : 0;
    uint32_t q;
    for (q = lv ? lv->ib_first : 0; q; q = PTR(c, otc_in_t, q)->next)
        if (PTR(c, otc_in_t, q)->kind == OTC_IK_GATE)
            return 1;
    return 0;
}

/* hosts bit CC: the live script has an `in cc ch n` block (the MIDI IN drain wakes it) */
static int gen_cc_host(otc_comp_t *c, const otc_func_t *f)
{
    const otc_live_t *lv = f->module_label ? PTR(c, otc_live_t, f->module_label) : 0;
    uint32_t q;
    for (q = lv ? lv->ib_first : 0; q; q = PTR(c, otc_in_t, q)->next)
        if (PTR(c, otc_in_t, q)->kind == OTC_IK_CC)
            return 1;
    return 0;
}

/* hosts bit TRIG: the live script has an `in trig` block (the UI wakes it on trig events) */
static uint32_t gen_trig_host(otc_comp_t *c, const otc_func_t *f)
{
    const otc_live_t *lv = f->module_label ? PTR(c, otc_live_t, f->module_label) : 0;
    uint32_t q;
    for (q = lv ? lv->ib_first : 0; q; q = PTR(c, otc_in_t, q)->next)
        if (PTR(c, otc_in_t, q)->kind == OTC_IK_TRIG)
            return 1;
    return 0;
}

/* The OTB table (spec 14) of the last run into `out` (at most `cap` bytes; a table is at most 32768 bytes). Returns its
 * length, or 0 when there is none: a failed run, a table-level error (more than 255 functions, a label over 255 bytes,
 * more than 1024 counters, a table over 32768 bytes) or a buffer that is too small. Byte for byte the output of
 * modules/otype/otb.py compile_program. */
uint32_t otc_emit(void *ws, uint8_t *out, uint32_t cap)
{
    otc_comp_t *c = (otc_comp_t *)ws;
    uint32_t i, total, nok = 0, pool_len, ftab_off, aux_off, aux_len = 0, line_off, line_len = 0, code_off, code_len = 0;
    uint32_t e, auxr = 0, liner = 0, coder = 0;
    if (!out || c->err || !c->checked || c->tab_err || !c->pool)
        return 0;
    total = c->tab_total;
    if (total > cap || total > 32768u)
        return 0;
    cc_zero(out, total);
    for (i = 0; i < c->nf; i++) {
        const otc_fx_t *fx = FX(c, i);
        const otc_func_t *f = FNI(c, i);
        if (!fx->inorder || fx->bad)
            continue;
        nok++;
        aux_len += 8u * f->nparams + 2u * f->nargs + 4u * fx->nctr + (f->page_line ? 2u : 0u) + (f->module_line ? 4u : 0u) + (f->confirm_line ? 2u : 0u) + gen_live_aux_len(c, f) + gen_typed_aux_len(c, f);
        line_len += 4u * fx->nlines;
        code_len += fx->code_len;
    }
    pool_len = c->pool_bytes;
    ftab_off = 32u + pool_len;
    aux_off = ftab_off + 54u * nok;
    line_off = aux_off + aux_len;
    code_off = line_off + line_len;
    if (code_off + code_len != total)
        return 0;                                            /* (cannot happen: the sizes of the dry run) */
    for (e = c->pool_first; e; e = PTR(c, pe_t, e)->onext) {  /* the pool, in the order of first use */
        const pe_t *pe = PTR(c, pe_t, e);
        uint32_t n, k;
        const uint8_t *p = cc_sp(c, pe->ref, &n);
        uint8_t *d = out + 32u + pe->off;
        if (pe->kind == P_STR) {
            d[0] = P_STR;
            d[1] = (uint8_t)n;
            d += 2;
        } else if (pe->kind == P_LIST) {
            d[0] = P_LIST;
            d[1] = (uint8_t)(n / 2u);
            d += 2;
        }
        for (k = 0; k < n; k++)
            d[k] = p[k];
    }
    for (i = 0; i < c->nf; i++) {                            /* the functions: entry, aux, lines, code */
        const otc_fx_t *fx = FX(c, i);
        const otc_func_t *f = FNI(c, i);
        uint8_t *en;
        em_t em;
        uint32_t nm, k, fl;
        const uint8_t *np;
        if (!fx->inorder || fx->bad)
            continue;
        em.aux = out + aux_off;
        em.lines = out + line_off;
        em.code = out + code_off;
        em.auxr = auxr;
        em.liner = liner;
        em.coder = coder;
        em.file_off = em.menu_off = 0;
        gen_func_pool(c, i, &em);
        gen_masks(c, i);
        en = out + ftab_off + 54u * fx->tabidx;
        np = cc_sp(c, f->name, &nm);
        for (k = 0; k < nm && k < 16u; k++)
            en[k] = np[k];
        fl = (fx->destr ? 1u : 0u) | ((f->flags & OTC_FF_CONFIRM) ? 2u : 0u) | (f->confirm_line ? 4u : 0u) | (gen_has_typed(c, f) ? 8u : 0u) | ((f->flags & OTC_FF_INPUT_SAMPLE) ? 16u : 0u) | (gen_live(c, f) && gen_live(c, f)->nvars ? 32u : 0u) | (gen_has_param_src(c, f) ? 64u : 0u) | (gen_live(c, f) && gen_live(c, f)->launch_line ? 128u : 0u);
        wr16(en + 16, em.menu_off);
        wr16(en + 18, (f->nmenus ? 1u : 0u) | (gen_trig_host(c, f) ? 2u : 0u) | (gen_gate_host(c, f) ? 0x100u : 0u) | (gen_cc_host(c, f) ? 0x200u : 0u) | (f->page_line ? 0x80u : 0u) | (f->module_line ? 0x40u : 0u));   /* hosts: MENU, TRIG, GATE, CC, PAGE, MODULE */
        wr16(en + 20, fx->fam);
        en[22] = (uint8_t)fx->tch;
        en[23] = (uint8_t)fl;
        wr32(en + 24, fx->cost);
        wr32(en + 28, fx->nsm);
        en[32] = (uint8_t)f->nargs;
        en[33] = (uint8_t)f->nparams;
        en[34] = (uint8_t)fx->nlocals;
        en[35] = (uint8_t)fx->maxstack;
        en[36] = (uint8_t)fx->maxctx;
        en[37] = (uint8_t)fx->maxctl;
        en[38] = (uint8_t)fx->nctr;
        {
            uint32_t b, nch = 0, nseen = 0;
            const otc_live_t *lv = gen_live(c, f);
            for (b = lv ? lv->ib_first : 0; b; b = PTR(c, otc_in_t, b)->next) {
                int cch;
                if (PTR(c, otc_in_t, b)->kind != OTC_IK_NOTE)
                    continue;
                cch = PTR(c, otc_in_t, b)->ch ? (int)PTR(c, otc_in_t, b)->ch : 255;
                if (!nseen) {
                    nch = (uint32_t)cch;
                    nseen = 1;
                } else if ((int)nch != cch) {
                    nch = 255;
                }
            }
            en[39] = (uint8_t)nch;                           /* the note channel a live script answers (255: any, 0: none) */
        }
        wr16(en + 40, em.file_off);
        wr16(en + 42, f->line);
        wr16(en + 44, coder);
        wr16(en + 46, fx->code_len);
        wr16(en + 48, auxr);
        wr16(en + 50, 4u * liner);
        wr16(en + 52, fx->nlines);
        auxr += 8u * f->nparams + 2u * f->nargs + 4u * fx->nctr + (f->page_line ? 2u : 0u) + (f->module_line ? 4u : 0u) + (f->confirm_line ? 2u : 0u) + gen_live_aux_len(c, f) + gen_typed_aux_len(c, f);
        liner += fx->nlines;
        coder += fx->code_len;
    }
    out[0] = 'O';
    out[1] = 'T';
    out[2] = 'B';
    out[3] = 1;                                              /* format */
    out[4] = 1;                                              /* language version */
    wr16(out + 6, nok);
    wr16(out + 8, c->nfuncs - nok);
    wr16(out + 10, 32);
    wr16(out + 12, pool_len);
    wr16(out + 14, ftab_off);
    wr16(out + 16, aux_off);
    wr16(out + 18, aux_len);
    wr16(out + 20, line_off);
    wr16(out + 22, line_len);
    wr16(out + 24, code_off);
    wr16(out + 26, code_len);
    wr16(out + 28, total);
    return c->err ? 0u : total;
}
