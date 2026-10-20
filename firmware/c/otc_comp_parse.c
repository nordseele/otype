/* OType compiler front end, part 3: the body parser. Included by otc_comp.c.
 * Mirrors tools/otype.py BodyParser (parse_term .. parse_decl, parse_line, parse) token by token. The
 * tokens of a line are read from the lexer one at a time (a window of two: peek(0), peek(1)); a ParseError is
 * a flag (p->err) and a message built in the arena, never an exception.
 *
 * Recursion: pp_term -> pp_term_of -> pp_word -> pp_op_call -> pp_op_args -> pp_term (and pp_user_call). It is
 * bounded by OTC_EXPR_DEPTH nested terms (a legal line of 128 characters nests fewer than 43); deeper stops the
 * run with OTC_CE_DEPTH. Every other loop is iterative. */

#define ND(c, o)  PTR(c, otc_node_t, o)
#define LN(c, o)  PTR(c, otc_line_t, o)

typedef struct {
    otc_comp_t *c;
    uint32_t fn;
    const uint8_t *body;
    uint32_t blen;
    lx_t lx;
    tok_t tk[2];
    uint32_t nt;                  /* tokens in the window */
    uint32_t line_no;
    uint32_t errmsg;
    uint32_t depth;
    uint8_t err, eof;
} pp_t;

/* ---- tokens ---------------------------------------------------------------------------------------- */
static void pp_fill(pp_t *p, uint32_t k)
{
    while (p->nt <= k && !p->eof) {
        lexerr_t e;
        if (lex_next(&p->lx, &p->tk[p->nt], &e) > 0)
            p->nt++;
        else
            p->eof = 1;               /* (a lexical error cannot happen: the line was checked) */
    }
}

static const tok_t *pp_peek(pp_t *p, uint32_t off)
{
    pp_fill(p, off);
    return off < p->nt ? &p->tk[off] : (const tok_t *)0;
}

static void pp_adv(pp_t *p)
{
    pp_fill(p, 0);
    if (p->nt) {
        tk_set(&p->tk[0], &p->tk[1]);
        p->nt--;
    }
}

static void pp_skip_all(pp_t *p)
{
    p->nt = 0;
    p->eof = 1;
}

static int tk_is_sym(const pp_t *p, const tok_t *t, uint8_t ch)
{
    return t && t->kind == T_SYM && p->body[t->a] == ch && t->b - t->a == 1u;
}

static int pp_at_end(pp_t *p, uint32_t off)
{
    const tok_t *t = pp_peek(p, off);
    return !t || tk_is_sym(p, t, ':');
}

static int tk_up(const tok_t *t)                   /* the text of the token is the upper-cased source (not a string / list) */
{
    return t->kind != T_STR && t->kind != T_LIST;
}

static const uint8_t *tk_ptr(const pp_t *p, const tok_t *t)
{
    return p->body + t->a;
}

static uint32_t tk_len(const tok_t *t)
{
    return t->b - t->a;
}

static int tk_word_eq(const pp_t *p, const tok_t *t, const char *upper)
{
    return t->kind == T_WORD && cc_ci_eq(tk_ptr(p, t), tk_len(t), upper);
}

static int tk_kw_in(const pp_t *p, const tok_t *t, uint32_t list)    /* WORD token in a keyword list of the registry */
{
    uint32_t i;
    if (t->kind != T_WORD)
        return 0;
    for (i = otc_kw_off[list]; i < otc_kw_off[list + 1]; i++)
        if (cc_opcmp(tk_ptr(p, t), tk_len(t), 1, &otc_rstr[otc_kw_names[i]]) == 0)
            return 1;
    return 0;
}

/* tokshow(): a string keeps its case, in quotes; the rest is lower case */
static void mb_tokshow(pp_t *p, const tok_t *t)
{
    otc_comp_t *c = p->c;
    if (t->kind == T_STR) {
        mb_byte(c, '"');
        mb_bytes(c, tk_ptr(p, t), tk_len(t));
        mb_byte(c, '"');
    } else {
        mb_show(c, tk_ptr(p, t), tk_len(t), tk_up(t));
    }
}

/* ---- errors ---------------------------------------------------------------------------------------- */
static uint32_t pp_raise(pp_t *p)                  /* the message of the ParseError is complete */
{
    p->errmsg = mb_end(p->c);
    p->err = 1;
    return 0;
}

static uint32_t pp_raise_lit(pp_t *p, const char *s)
{
    mb_begin(p->c);
    mb_lit(p->c, s);
    return pp_raise(p);
}

/* "<w> <lit>" with w shown from a token */
static uint32_t pp_raise_w(pp_t *p, const tok_t *t, const char *tail)
{
    mb_begin(p->c);
    mb_show(p->c, tk_ptr(p, t), tk_len(t), 1);
    mb_lit(p->c, tail);
    return pp_raise(p);
}

/* ---- nodes ----------------------------------------------------------------------------------------- */
static uint32_t pp_node(pp_t *p, uint8_t kind)
{
    uint32_t n = cc_alloc(p->c, (uint32_t)sizeof(otc_node_t));
    if (!n) {
        p->err = 1;
        return 0;
    }
    ND(p->c, n)->kind = kind;
    return n;
}

static uint32_t pp_text(pp_t *p, const tok_t *t)
{
    uint32_t r = cc_str(p->c, tk_ptr(p, t), tk_len(t), tk_up(t));
    if (!r)
        p->err = 1;
    return r;
}

static void pp_link(pp_t *p, uint32_t parent, uint32_t *last, uint32_t child, uint8_t akind)
{
    ND(p->c, child)->akind = akind;
    if (*last)
        ND(p->c, *last)->next = child;
    else
        ND(p->c, parent)->first = child;
    *last = child;
    ND(p->c, parent)->val++;
}

static uint32_t pp_kw_node(pp_t *p, const tok_t *t, uint8_t kind)
{
    uint32_t n = pp_node(p, kind);
    if (!n)
        return 0;
    ND(p->c, n)->text = pp_text(p, t);
    return n;
}

/* ---- terms ------------------------------------------------------------------------------------------ */
static uint32_t pp_term(pp_t *p);
static uint32_t pp_op_call(pp_t *p, uint32_t opi, const tok_t *t);
static uint32_t pp_user_call(pp_t *p, const tok_t *t, uint32_t g);

static uint32_t pp_find_arg(const pp_t *p, const tok_t *t)       /* 1 + index of the argument of the function called so, or 0 */
{
    const otc_comp_t *c = p->c;
    const otc_func_t *f = FN(c, p->fn);
    uint32_t i;
    for (i = 0; i < f->nargs; i++)
        if (cc_ref_eq(c, PTR(c, uint32_t, f->args)[i] & 0x7FFFFFFFu, tk_ptr(p, t), tk_len(t), tk_up(t)))
            return i + 1u;
    return 0;
}

static int pp_find_param(const pp_t *p, const uint8_t *s, uint32_t n, int up)
{
    const otc_comp_t *c = p->c;
    uint32_t q;
    for (q = FN(c, p->fn)->param_first; q; q = PTR(c, otc_param_t, q)->next)
        if (cc_ref_eq(c, PTR(c, otc_param_t, q)->name, s, n, up))
            return 1;
    return 0;
}

/* the `var` of this function named so (offset), or 0; visibility is checked by the caller */
static uint32_t pp_find_var(const pp_t *p, const uint8_t *s, uint32_t n, int up)
{
    const otc_comp_t *c = p->c;
    const otc_func_t *f = FN(c, p->fn);
    uint32_t q;
    for (q = f->module_label ? PTR(c, otc_live_t, f->module_label)->var_first : 0; q; q = PTR(c, otc_var_t, q)->next)
        if (cc_ref_eq(c, PTR(c, otc_var_t, q)->name, s, n, up))
            return q;
    return 0;
}

/* Python resolve_word: a var of a block is only visible inside that block */
static int pp_var_visible(const otc_comp_t *c, const otc_var_t *v)
{
    return v->scope == 0 || v->scope == c->cur_ib;
}

/* the value of an integer const token, or 0 (the caller emits the message) */
static int pp_int_const(const pp_t *p, const tok_t *t, int32_t *out)
{
    uint32_t k;
    if (t->kind == T_NUM) {
        *out = t->val;
        return 1;
    }
    if (t->kind == T_WORD && (k = cc_cmap_find(p->c, tk_ptr(p, t), tk_len(t), 1)) != 0 &&
        !CN(p->c, k)->bad && CN(p->c, k)->type == OTC_CT_INT) {
        *out = (int32_t)CN(p->c, k)->val;
        return 1;
    }
    return 0;
}

/* the otc_live_t of this function, allocated on first use */
static otc_live_t *pp_live(otc_comp_t *c, otc_func_t *f)
{
    uint32_t off;
    if (f->module_label)
        return PTR(c, otc_live_t, f->module_label);
    off = cc_alloc(c, (uint32_t)sizeof(otc_live_t));
    if (!off)
        return 0;
    cc_zero(PTR(c, otc_live_t, off), (uint32_t)sizeof(otc_live_t));
    f->module_label = off;
    return PTR(c, otc_live_t, off);
}

/* tools/otype.py RENAMED / REMOVED: the word, its replacement (removed = 0) or the cookbook recipe (removed = 1) */
static const struct { const char *word, *text; int removed; } pp_retired[] = {
    {"RATCH", "ratch.set", 0}, {"FX.SET", "fx.type", 0}, {"FILE.N", "file.len", 0},
    {"SLOTS.NONE", "slots.clr", 0},
    {"LFO.SPD1", "lfo.spd 1", 0}, {"LFO.SPD2", "lfo.spd 2", 0}, {"LFO.SPD3", "lfo.spd 3", 0},
    {"LFO.DEP1", "lfo.dep 1", 0}, {"LFO.DEP2", "lfo.dep 2", 0}, {"LFO.DEP3", "lfo.dep 3", 0},
    {"SLOT.LOAD", "static.load", 0}, {"SLOT.FREE", "static.free", 0},
    {"SLOT.FIRSTFREE", "static.firstfree", 0}, {"SLOT.CLR", "static.clr", 0},
    {"POOL.FILL", "Fill the Static slots", 1}, {"POOL.RND", "Fill the Static slots at random", 1},
    {"POOL.CLR", "Clear all Static slots", 1}, {"POOL.SHUFFLE", "Shuffle the Static slots", 1},
    {"SCN.GEN", "Random scenes", 1}, {"LFO.RND", "Random LFOs", 1}, {"FX.RND", "Random effects", 1},
    {"LFOD.RND", "Random LFO designer", 1}, {"LFOD.INV", "Invert the LFO designer", 1},
};

static int pp_check_renamed(pp_t *p, const uint8_t *w, uint32_t wn)
{
    otc_comp_t *c = p->c;
    unsigned k;
    if (cc_ci_eq(w, wn, "MODULE")) {                    /* tools/otype.py retired(): checked before every other name */
        mb_begin(c);
        mb_lit(c, "module is gone: use in clock n");
        pp_raise(p);
        return 1;
    }
    if (cc_ci_eq(w, wn, "CLOCK")) {
        mb_begin(c);
        mb_lit(c, "clock declaration is gone: use in clock n");
        pp_raise(p);
        return 1;
    }
    for (k = 0; k < sizeof(pp_retired) / sizeof(pp_retired[0]); k++) {
        if (!cc_ci_eq(w, wn, pp_retired[k].word))
            continue;
        mb_begin(c);
        mb_show(c, w, wn, 1);
        if (pp_retired[k].removed) {
            mb_lit(c, " was removed: write the loop (cookbook \"");
            mb_lit(c, pp_retired[k].text);
            mb_lit(c, "\")");
        } else {
            mb_lit(c, " is now ");
            mb_lit(c, pp_retired[k].text);
        }
        pp_raise(p);
        return 1;
    }
    return 0;
}

static uint32_t pp_word(pp_t *p, const tok_t *t)       /* resolve_word */
{
    otc_comp_t *c = p->c;
    const uint8_t *w = tk_ptr(p, t);
    uint32_t n = tk_len(t), k;
    int opi;
    if (pp_check_renamed(p, w, n))                      /* retired(): before the variables, like resolve_word */
        return 0;
    if (cc_in_set(OTC_SET_VARS, w, n, 1))
        return pp_kw_node(p, t, OTC_N_VAR);
    k = pp_find_var(p, w, n, 1);
    if (k) {
        const otc_var_t *v = PTR(c, otc_var_t, k);
        uint32_t node;
        if (!pp_var_visible(c, v)) {
            mb_begin(c);
            mb_lit(c, "var ");
            mb_show(c, w, n, 1);
            mb_lit(c, " is not visible in this block");
            return pp_raise(p);
        }
        node = pp_kw_node(p, t, OTC_N_VAR_NAMED);
        if (node)
            ND(c, node)->val = v->slot;
        return node;
    }
    if (pp_find_arg(p, t))
        return pp_kw_node(p, t, OTC_N_ARG);
    if (pp_find_param(p, w, n, 1))
        return pp_kw_node(p, t, OTC_N_PARAM);
    k = cc_cmap_find(c, w, n, 1);
    if (k) {
        if (CN(c, k)->bad) {
            mb_begin(c);
            mb_lit(c, "const ");
            mb_show(c, w, n, 1);
            mb_lit(c, " is refused (see its error)");
            return pp_raise(p);
        }
        return pp_kw_node(p, t, OTC_N_CONST);
    }
    opi = cc_op_find(w, n, 1);
    if (opi >= 0) {
        const otc_reg_t *r = &otc_reg[opi];
        if ((r->flags & OTC_RF_STMT_ONLY) || !(r->flags & OTC_RF_RET)) {
            mb_begin(c);
            mb_rstr(c, r->shown);
            mb_lit(c, " gives no value: it is a statement");
            return pp_raise(p);
        }
        return pp_op_call(p, (uint32_t)opi, t);
    }
    k = cc_fmap_find(c, w, n, 1);
    if (k)
        return pp_user_call(p, t, k);
    if (cc_in_set(OTC_SET_KEYWORDS, w, n, 1))
    {
        mb_begin(c);
        mb_lit(c, "unexpected keyword ");
        mb_show(c, w, n, 1);
        return pp_raise(p);
    }
    mb_begin(c);
    mb_lit(c, "unknown name ");
    mb_show(c, w, n, 1);
    return pp_raise(p);
}

static uint32_t pp_term_of(pp_t *p, const tok_t *t)    /* term */
{
    otc_comp_t *c = p->c;
    uint32_t n;
    if (t->kind == T_NUM) {
        n = pp_node(p, OTC_N_NUM);
        if (!n)
            return 0;
        ND(c, n)->text = pp_text(p, t);
        ND(c, n)->val = (uint32_t)t->val;
        return n;
    }
    if (t->kind == T_STR) {
        n = pp_node(p, OTC_N_STR);
        if (!n)
            return 0;
        ND(c, n)->text = pp_text(p, t);
        return n;
    }
    if (t->kind == T_LIST) {
        n = pp_node(p, OTC_N_LIST);
        if (!n)
            return 0;
        ND(c, n)->text = pp_text(p, t);
        ND(c, n)->val = cc_list_ref(c, p->body, t->a, t->b);
        return n;
    }
    if (t->kind == T_COND) {
        n = pp_node(p, OTC_N_COND);
        if (!n)
            return 0;
        ND(c, n)->text = pp_text(p, t);
        ND(c, n)->val = cc_cond_ref(c, p->body, t);
        return n;
    }
    if (t->kind == T_SYM) {
        mb_begin(c);
        mb_lit(c, "unexpected '");
        mb_bytes(c, tk_ptr(p, t), tk_len(t));
        mb_byte(c, '\'');
        return pp_raise(p);
    }
    return pp_word(p, t);
}

static uint32_t pp_term(pp_t *p)                   /* parse_term */
{
    const tok_t *pt;
    tok_t t;
    uint32_t n;
    pt = pp_peek(p, 0);
    if (!pt)
        return pp_raise_lit(p, "unexpected end of line");
    tk_set(&t, pt);
    pp_adv(p);
    if (++p->depth > OTC_EXPR_DEPTH) {
        cc_fail(p->c, OTC_CE_DEPTH);
        p->err = 1;
        return 0;
    }
    n = pp_term_of(p, &t);
    p->depth--;
    return n;
}

/* ---- operations ------------------------------------------------------------------------------------- */
static void mb_need(pp_t *p, const otc_reg_t *r, uint32_t got)       /* need(): "%s expects %s, got %d" */
{
    otc_comp_t *c = p->c;
    mb_begin(c);
    mb_rstr(c, r->shown);
    mb_lit(c, " expects ");
    mb_rstr(c, r->desc);
    mb_lit(c, ", got ");
    mb_dec(c, (int32_t)got);
}

static uint32_t pp_need(pp_t *p, const otc_reg_t *r, uint32_t got)
{
    mb_need(p, r, got);
    return pp_raise(p);
}

static int pp_op_args(pp_t *p, uint32_t node, const otc_reg_t *r)
{
    otc_comp_t *c = p->c;
    uint32_t last = 0, i;
    for (i = 0; i < r->nargs; i++) {
        uint8_t k = r->ak[i];
        const tok_t *t;
        uint32_t n;
        if (k == OTC_K_OPTKW) {
            t = pp_peek(p, 0);
            if (t && tk_kw_in(p, t, r->akw[i])) {
                tok_t kt;
    tk_set(&kt, t);
                pp_adv(p);
                n = pp_kw_node(p, &kt, OTC_N_KW);
                if (!n)
                    return 0;
                pp_link(p, node, &last, n, OTC_K_KW);
            }
        } else if (k == OTC_K_REST) {
            uint32_t count = 0;
            while (!pp_at_end(p, 0)) {
                if (r->ak2[i] == OTC_K_KW) {
                    tok_t kt;
    tk_set(&kt, pp_peek(p, 0));
                    pp_adv(p);
                    if (!tk_kw_in(p, &kt, r->akw[i])) {
                        uint32_t q;
                        mb_begin(c);
                        mb_rstr(c, r->shown);
                        mb_lit(c, ": expected one of");
                        for (q = otc_kw_off[r->akw[i]]; q < otc_kw_off[r->akw[i] + 1]; q++) {
                            mb_byte(c, ' ');
                            mb_show(c, (const uint8_t *)&otc_rstr[otc_kw_names[q]],
                                    cc_cstrlen(&otc_rstr[otc_kw_names[q]]), 0);
                        }
                        mb_lit(c, ", got ");
                        mb_tokshow(p, &kt);
                        return (int)pp_raise(p);
                    }
                    n = pp_kw_node(p, &kt, OTC_N_KW);
                    if (!n)
                        return 0;
                    pp_link(p, node, &last, n, OTC_K_KW);
                } else {
                    n = pp_term(p);
                    if (!n)
                        return 0;
                    pp_link(p, node, &last, n, r->ak2[i]);
                }
                count++;
            }
            if (!count)
                return (int)pp_need(p, r, 0);
        } else if (k == OTC_K_KW) {
            t = pp_peek(p, 0);
            if (pp_at_end(p, 0) || !tk_kw_in(p, t, r->akw[i]))
                return (int)pp_need(p, r, ND(c, node)->val);
            {
                tok_t kt;
    tk_set(&kt, t);
                pp_adv(p);
                n = pp_kw_node(p, &kt, OTC_N_KW);
                if (!n)
                    return 0;
                pp_link(p, node, &last, n, OTC_K_KW);
            }
        } else if (k == OTC_K_TRACKS) {
            t = pp_peek(p, 0);
            if (t && (tk_word_eq(p, t, "CUR") || tk_word_eq(p, t, "ALL")) && pp_at_end(p, 1)) {
                tok_t kt;
    tk_set(&kt, t);
                pp_adv(p);
                n = pp_kw_node(p, &kt, OTC_N_KW);
                if (!n)
                    return 0;
                pp_link(p, node, &last, n, OTC_K_KW);
            } else {
                uint32_t count = 0;
                while (!pp_at_end(p, 0)) {
                    n = pp_term(p);
                    if (!n)
                        return 0;
                    pp_link(p, node, &last, n, OTC_K_TRACK);
                    count++;
                }
                if (!count) {
                    mb_begin(c);
                    mb_rstr(c, r->shown);
                    mb_lit(c, " expects 1 to 8 tracks, or cur, or all");
                    return (int)pp_raise(p);
                }
                if (count > 8u) {
                    mb_begin(c);
                    mb_rstr(c, r->shown);
                    mb_lit(c, " takes at most 8 tracks (got ");
                    mb_dec(c, (int32_t)count);
                    mb_byte(c, ')');
                    return (int)pp_raise(p);
                }
            }
        } else if (k == OTC_K_STEPSEL) {
            int popi;
            t = pp_peek(p, 0);
            popi = (t && t->kind == T_WORD) ? cc_op_find(tk_ptr(p, t), tk_len(t), 1) : -1;
            if (t && tk_word_eq(p, t, "ALL") && pp_at_end(p, 1)) {
                tok_t kt;
    tk_set(&kt, t);
                pp_adv(p);
                n = pp_kw_node(p, &kt, OTC_N_KW);
                if (!n)
                    return 0;
                pp_link(p, node, &last, n, OTC_K_KW);
            } else if (popi >= 0 && (otc_reg[popi].flags & OTC_RF_PRED) && pp_at_end(p, 1)) {
                tok_t kt;
    tk_set(&kt, t);
                pp_adv(p);
                n = pp_kw_node(p, &kt, OTC_N_PRED);
                if (!n)
                    return 0;
                ND(c, n)->op = (uint8_t)popi;
                pp_link(p, node, &last, n, OTC_K_KW);
            } else {
                uint32_t j;
                for (j = 0; j < 2; j++) {
                    if (pp_at_end(p, 0)) {
                        mb_begin(c);
                        mb_rstr(c, r->shown);
                        mb_lit(c, " expects all, a range (two steps) or a predicate name such as has.trig");
                        return (int)pp_raise(p);
                    }
                    n = pp_term(p);
                    if (!n)
                        return 0;
                    pp_link(p, node, &last, n, OTC_K_STEP);
                }
            }
        } else if (k == OTC_K_NAME) {
            int np;
            t = pp_peek(p, 0);
            if (pp_at_end(p, 0) || !t) {
                if (i >= r->minargs)
                    break;
                return (int)pp_need(p, r, ND(c, node)->val);
            }
            if (t->kind != T_WORD) {
                tok_t kt;
                tk_set(&kt, t);
                mb_begin(c);
                mb_rstr(c, r->shown);
                mb_lit(c, ": expected a function name, got ");
                mb_tokshow(p, &kt);
                return (int)pp_raise(p);
            }
            {
                tok_t kt;
                tk_set(&kt, t);
                np = cc_name_problem(tk_ptr(p, &kt), tk_len(&kt), 1, 1);
                if (np) {
                    mb_begin(c);
                    mb_rstr(c, r->shown);
                    mb_lit(c, ": ");
                    cc_np_text(c, np, tk_ptr(p, &kt), tk_len(&kt), 1);
                    return (int)pp_raise(p);
                }
                if (tk_len(&kt) > 8u) {
                    mb_begin(c);
                    mb_rstr(c, r->shown);
                    mb_lit(c, ": name is too long (");
                    mb_dec(c, (int32_t)tk_len(&kt));
                    mb_lit(c, " > 8 characters)");
                    return (int)pp_raise(p);
                }
                pp_adv(p);
                n = pp_node(p, OTC_N_STR);
                if (!n)
                    return 0;
                ND(c, n)->text = pp_text(p, &kt);
                if (p->err)
                    return 0;
                pp_link(p, node, &last, n, OTC_K_STR);
            }
        } else if (k == OTC_K_COND) {
            t = pp_peek(p, 0);
            if (t && tk_word_eq(p, t, "FILL")) {
                tok_t kt;
                tk_set(&kt, t);
                pp_adv(p);
                n = pp_node(p, OTC_N_COND);
                if (!n)
                    return 0;
                ND(c, n)->text = pp_text(p, &kt);
                ND(c, n)->val = pp_text(p, &kt);
                pp_link(p, node, &last, n, OTC_K_COND);
            } else {
                if (pp_at_end(p, 0)) {
                    if (i >= r->minargs)
                        break;
                    return (int)pp_need(p, r, ND(c, node)->val);
                }
                n = pp_term(p);
                if (!n)
                    return 0;
                pp_link(p, node, &last, n, k);
            }
        } else {
            if (pp_at_end(p, 0)) {
                if (i >= r->minargs) {                     /* an optional argument: its OPT_INT default */
                    int32_t dv = 0;
                    uint32_t q, opi = (uint32_t)(r - otc_reg);
                    char buf[8];
                    uint32_t bn = 0;
                    for (q = 0; q < OTC_NARGDEF; q++)
                        if (otc_argdef[q].op == opi && otc_argdef[q].arg == i)
                            dv = otc_argdef[q].value;
                    n = pp_node(p, OTC_N_NUM);
                    if (!n)
                        return 0;
                    ND(c, n)->val = (uint32_t)dv;
                    if (dv < 0) {
                        buf[bn++] = '-';
                        dv = -dv;
                    }
                    if (dv >= 10)
                        buf[bn++] = (char)('0' + (dv / 10) % 10);
                    buf[bn++] = (char)('0' + dv % 10);
                    ND(c, n)->text = cc_str(c, (const uint8_t *)buf, bn, 0);
                    if (!ND(c, n)->text) {
                        p->err = 1;
                        return 0;
                    }
                    pp_link(p, node, &last, n, k);
                    continue;
                }
                return (int)pp_need(p, r, ND(c, node)->val);
            }
            n = pp_term(p);
            if (!n)
                return 0;
            pp_link(p, node, &last, n, k);
        }
    }
    return 1;
}

static uint32_t pp_op_call(pp_t *p, uint32_t opi, const tok_t *t)
{
    otc_comp_t *c = p->c;
    const otc_reg_t *r = &otc_reg[opi];
    const tok_t *nxt = pp_peek(p, 0);
    uint32_t node;
    if (tk_is_sym(p, nxt, '|')) {
        mb_begin(c);
        mb_lit(c, "'|' is for your own functions: ");
        mb_rstr(c, r->shown);
        mb_lit(c, " takes its arguments directly (");
        mb_rstr(c, r->desc);
        mb_byte(c, ')');
        return pp_raise(p);
    }
    node = pp_node(p, OTC_N_OP);
    if (!node)
        return 0;
    ND(c, node)->op = (uint8_t)opi;
    ND(c, node)->text = pp_text(p, t);
    if (!pp_op_args(p, node, r))
        return 0;
    return node;
}

static void mb_plural(otc_comp_t *c, uint32_t k)
{
    if (k != 1u)
        mb_byte(c, 's');
}

static uint32_t pp_user_call(pp_t *p, const tok_t *t, uint32_t g)
{
    otc_comp_t *c = p->c;
    const otc_func_t *gf = FN(c, g);
    uint32_t k = gf->nargs, i, node, last = 0;
    const tok_t *nxt = pp_peek(p, 0);
    int has_bar = tk_is_sym(p, nxt, '|');
    const uint8_t *w = tk_ptr(p, t);
    uint32_t wn = tk_len(t);
    if (k == 0) {
        if (has_bar) {
            mb_begin(c);
            mb_show(c, w, wn, 1);
            mb_lit(c, " takes no argument: write ");
            mb_show(c, w, wn, 1);
            mb_lit(c, " without '|'");
            return pp_raise(p);
        }
        node = pp_node(p, OTC_N_CALL);
        if (!node)
            return 0;
        ND(c, node)->text = pp_text(p, t);
        return node;
    }
    if (!has_bar) {
        mb_begin(c);
        mb_show(c, w, wn, 1);
        mb_lit(c, " takes ");
        mb_dec(c, (int32_t)k);
        mb_lit(c, " argument");
        mb_plural(c, k);
        mb_lit(c, ": write ");
        mb_show(c, w, wn, 1);
        mb_lit(c, " | ");
        c->msg_inq = 0;                                     /* show(" ".join(g.args)): one string */
        for (i = 0; i < k; i++) {
            uint32_t al;
            const uint8_t *ap = cc_sp(c, PTR(c, uint32_t, gf->args)[i] & 0x7FFFFFFFu, &al);
            if (i)
                mb_byte(c, ' ');
            mb_show_part(c, ap, al, 0);
        }
        return pp_raise(p);
    }
    pp_adv(p);
    node = pp_node(p, OTC_N_CALL);
    if (!node)
        return 0;
    ND(c, node)->text = pp_text(p, t);
    for (i = 0; i < k; i++) {
        uint32_t n;
        if (pp_at_end(p, 0)) {
            mb_begin(c);
            mb_show(c, w, wn, 1);
            mb_lit(c, " expects ");
            mb_dec(c, (int32_t)k);
            mb_lit(c, " argument");
            mb_plural(c, k);
            mb_lit(c, ", got ");
            mb_dec(c, (int32_t)i);
            return pp_raise(p);
        }
        n = pp_term(p);
        if (!n)
            return 0;
        pp_link(p, node, &last, n, OTC_K_USER);
    }
    return node;
}

/* ---- statements ---------------------------------------------------------------------------------------- */
static uint32_t pp_stmt_node(pp_t *p, uint8_t kind, const tok_t *t)
{
    uint32_t n = pp_node(p, kind);
    if (!n)
        return 0;
    ND(p->c, n)->text = pp_text(p, t);
    return n;
}

static uint32_t pp_statement(pp_t *p)
{
    otc_comp_t *c = p->c;
    tok_t t;
    tk_set(&t, pp_peek(p, 0));
    const uint8_t *w = tk_ptr(p, &t);
    uint32_t wn = tk_len(&t), n, e, k;
    int opi;
    if (t.kind == T_NUM || t.kind == T_STR || t.kind == T_LIST || t.kind == T_COND) {
        mb_begin(c);
        mb_tokshow(p, &t);
        mb_lit(c, " does nothing (a value with no destination)");
        return pp_raise(p);
    }
    if (t.kind == T_SYM) {
        mb_begin(c);
        mb_lit(c, "unexpected '");
        mb_bytes(c, w, wn);
        mb_byte(c, '\'');
        return pp_raise(p);
    }
    if (tk_word_eq(p, &t, "BREAK")) {
        pp_adv(p);
        return pp_stmt_node(p, OTC_N_BREAK, &t);
    }
    if (tk_word_eq(p, &t, "STOP")) {
        pp_adv(p);
        return pp_stmt_node(p, OTC_N_STOP, &t);
    }
    if (tk_word_eq(p, &t, "RET")) {
        pp_adv(p);
        n = pp_stmt_node(p, OTC_N_RET, &t);
        if (!n)
            return 0;
        if (!pp_at_end(p, 0)) {
            e = pp_term(p);
            if (!e)
                return 0;
            ND(c, n)->first = e;
        }
        return n;
    }
    if (cc_in_set(OTC_SET_DECLS, w, wn, 1))
        return pp_raise_w(p, &t, " is a declaration: it goes at the top of the function body, before the first "
                                 "statement, without modifier");
    if (tk_word_eq(p, &t, "DEF") || tk_word_eq(p, &t, "CONST") || tk_word_eq(p, &t, "OTYPE"))
        return pp_raise_w(p, &t, " must start at column 1");
    if (cc_in_set(OTC_SET_MODIFIERS, w, wn, 1)) {
        mb_begin(c);
        mb_lit(c, "unexpected ");
        mb_show(c, w, wn, 1);
        return pp_raise(p);
    }
    if (cc_in_set(OTC_SET_VARS, w, wn, 1)) {
        if (!cc_in_set(OTC_SET_ASSIGNABLE, w, wn, 1))
            return pp_raise_w(p, &t, " is read-only (loop counter)");
        pp_adv(p);
        if (pp_at_end(p, 0)) {
            mb_begin(c);
            mb_show(c, w, wn, 1);
            mb_lit(c, " alone does nothing (write ");
            mb_show(c, w, wn, 1);
            mb_lit(c, " <value> to assign)");
            return pp_raise(p);
        }
        e = pp_term(p);
        if (!e)
            return 0;
        n = pp_stmt_node(p, OTC_N_ASSIGN, &t);
        if (!n)
            return 0;
        ND(c, n)->first = e;
        return n;
    }
    k = pp_find_var(p, w, wn, 1);
    if (k) {
        const otc_var_t *v = PTR(c, otc_var_t, k);
        if (!pp_var_visible(c, v)) {
            mb_begin(c);
            mb_lit(c, "var ");
            mb_show(c, w, wn, 1);
            mb_lit(c, " is not visible in this block");
            return pp_raise(p);
        }
        pp_adv(p);
        if (pp_at_end(p, 0)) {
            mb_begin(c);
            mb_show(c, w, wn, 1);
            mb_lit(c, " alone does nothing (write ");
            mb_show(c, w, wn, 1);
            mb_lit(c, " <value> to assign)");
            return pp_raise(p);
        }
        e = pp_term(p);
        if (!e)
            return 0;
        n = pp_stmt_node(p, OTC_N_ASSIGN_VAR_NAMED, &t);
        if (!n)
            return 0;
        ND(c, n)->first = e;
        ND(c, n)->val = v->slot;
        return n;
    }
    if (pp_find_arg(p, &t)) {
        mb_begin(c);
        mb_show(c, w, wn, 1);
        mb_lit(c, pp_peek(p, 1) ? " is an argument (read-only)" : " alone does nothing");
        return pp_raise(p);
    }
    if (pp_find_param(p, w, wn, 1)) {
        mb_begin(c);
        mb_show(c, w, wn, 1);
        mb_lit(c, pp_peek(p, 1) ? " is a param (read-only)" : " alone does nothing");
        return pp_raise(p);
    }
    if (cc_cmap_find(c, w, wn, 1)) {
        mb_begin(c);
        mb_lit(c, "const ");
        mb_show(c, w, wn, 1);
        mb_lit(c, " is read-only; a constant alone does nothing");
        return pp_raise(p);
    }
    opi = cc_op_find(w, wn, 1);
    if (opi >= 0) {
        const otc_reg_t *r = &otc_reg[opi];
        pp_adv(p);
        if (r->family == OTC_FAM_LANG) {
            mb_begin(c);
            mb_show(c, w, wn, 1);
            mb_lit(c, " does nothing: it computes a value that nobody uses (assign it, e.g. a ");
            mb_show(c, w, wn, 1);
            mb_lit(c, " ...)");
            return pp_raise(p);
        }
        if ((r->flags & OTC_RF_SETTABLE) && !pp_at_end(p, 0)) {
            uint32_t last = 0, a;
            n = pp_stmt_node(p, OTC_N_SETSTMT, &t);
            if (!n)
                return 0;
            ND(c, n)->op = (uint8_t)opi;
            for (a = 0; a < r->nargs; a++) {
                if (pp_at_end(p, 0))
                    return (int)pp_need(p, r, a);
                e = pp_term(p);
                if (!e)
                    return 0;
                pp_link(p, n, &last, e, r->ak[a]);
            }
            if (pp_at_end(p, 0)) {
                mb_begin(c);
                mb_rstr(c, r->shown);
                mb_lit(c, " expects a value to set");
                return (int)pp_raise(p);
            }
            e = pp_term(p);
            if (!e)
                return 0;
            pp_link(p, n, &last, e, r->ret ? r->ret : OTC_K_INT);
            return n;
        }
        e = pp_op_call(p, (uint32_t)opi, &t);
        if (!e)
            return 0;
        n = pp_stmt_node(p, r->family == OTC_FAM_CTX ? OTC_N_CTXSTMT : OTC_N_OPSTMT, &t);
        if (!n)
            return 0;
        ND(c, n)->first = e;
        return n;
    }
    k = cc_fmap_find(c, w, wn, 1);
    if (k) {
        pp_adv(p);
        e = pp_user_call(p, &t, k);
        if (!e)
            return 0;
        n = pp_stmt_node(p, OTC_N_CALLSTMT, &t);
        if (!n)
            return 0;
        ND(c, n)->first = e;
        return n;
    }
    if (pp_check_renamed(p, w, wn))
        return 0;
    mb_begin(c);
    mb_lit(c, cc_in_set(OTC_SET_KEYWORDS, w, wn, 1) ? "unexpected keyword " : "unknown name ");
    mb_show(c, w, wn, 1);
    return pp_raise(p);
}

/* leftover(): something is still there after a complete statement */
static uint32_t pp_leftover(pp_t *p, uint32_t stmt)
{
    otc_comp_t *c = p->c;
    tok_t t;
    tk_set(&t, pp_peek(p, 0));
    otc_node_t *s = ND(c, stmt);
    uint32_t stext = s->text;
    if (t.kind == T_SYM) {
        mb_begin(c);
        if (tk_is_sym(p, &t, ':')) {
            mb_lit(c, "unexpected ':' (not a modifier: ");
            mb_show_ref(c, stext);
            mb_byte(c, ')');
        } else {
            mb_lit(c, "unexpected '");
            mb_bytes(c, tk_ptr(p, &t), tk_len(&t));
            mb_byte(c, '\'');
        }
        return pp_raise(p);
    }
    if (s->kind == OTC_N_OPSTMT || s->kind == OTC_N_CALLSTMT) {
        uint32_t nd = s->first, nargs = ND(c, nd)->val, extra = 0, mark = c->top;
        while (!pp_at_end(p, 0)) {
            extra++;
            if (!pp_term(p)) {
                if (c->err)
                    return 0;
                p->err = 0;                                /* the ParseError is caught: break */
                break;
            }
        }
        c->top = mark;                                     /* what the loop built is garbage */
        mb_begin(c);
        if (ND(c, nd)->kind == OTC_N_OP) {
            const otc_reg_t *r = &otc_reg[ND(c, nd)->op];
            mb_rstr(c, r->shown);
            mb_lit(c, " expects ");
            mb_rstr(c, r->desc);
            mb_lit(c, ", got ");
            mb_dec(c, (int32_t)(nargs + extra));
        } else {
            mb_show_ref(c, ND(c, nd)->text);
            mb_lit(c, " expects ");
            mb_dec(c, (int32_t)nargs);
            mb_lit(c, " argument");
            mb_plural(c, nargs);
            mb_lit(c, ", got ");
            mb_dec(c, (int32_t)(nargs + extra));
        }
        return pp_raise(p);
    }
    mb_begin(c);
    mb_lit(c, "unexpected ");
    mb_tokshow(p, &t);
    mb_lit(c, " after ");
    mb_show_ref(c, stext);
    return pp_raise(p);
}

/* parse_modifier(): the node of the modifier (0 on a ParseError) */
static uint32_t pp_modifier(pp_t *p)
{
    otc_comp_t *c = p->c;
    tok_t t;
    tk_set(&t, pp_peek(p, 0));
    uint32_t n, e, last = 0, j;
    uint8_t kind;
    pp_adv(p);
    if (tk_word_eq(p, &t, "DEL"))
        return pp_raise_lit(p, "del is not available yet");
    if (tk_word_eq(p, &t, "IF"))
        kind = OTC_N_M_IF;
    else if (tk_word_eq(p, &t, "ELIF"))
        kind = OTC_N_M_ELIF;
    else if (tk_word_eq(p, &t, "ELSE"))
        kind = OTC_N_M_ELSE;
    else if (tk_word_eq(p, &t, "L"))
        kind = OTC_N_M_L;
    else if (tk_word_eq(p, &t, "EACH"))
        kind = OTC_N_M_EACH;
    else if (tk_word_eq(p, &t, "PROB"))
        kind = OTC_N_M_PROB;
    else if (tk_word_eq(p, &t, "EVERY"))
        kind = OTC_N_M_EVERY;
    else
        kind = OTC_N_M_SKIP;
    n = pp_stmt_node(p, kind, &t);
    if (!n)
        return 0;
    if (kind == OTC_N_M_L) {
        for (j = 0; j < 2; j++) {
            if (pp_at_end(p, 0))
                return pp_raise_lit(p, "l expects two bounds: l first last:");
            e = pp_term(p);
            if (!e)
                return 0;
            pp_link(p, n, &last, e, 0);
        }
    } else if (kind != OTC_N_M_ELSE && kind != OTC_N_M_EACH) {
        if (pp_at_end(p, 0)) {
            mb_begin(c);
            mb_lit(c, "");
            mb_show(c, tk_ptr(p, &t), tk_len(&t), 1);
            mb_lit(c, " expects a value before ':'");
            return pp_raise(p);
        }
        e = pp_term(p);
        if (!e)
            return 0;
        pp_link(p, n, &last, e, 0);
    }
    {                                                       /* expect_colon */
        const tok_t *q = pp_peek(p, 0);
        if (!tk_is_sym(p, q, ':')) {
            mb_begin(c);
            mb_lit(c, "expected ':' after ");
            mb_show(c, tk_ptr(p, &t), tk_len(&t), 1);
            return pp_raise(p);
        }
        pp_adv(p);
    }
    return n;
}

/* ---- declarations -------------------------------------------------------------------------------------- */
static int pp_next(pp_t *p, tok_t *t)
{
    const tok_t *pt = pp_peek(p, 0);
    if (!pt) {
        pp_raise_lit(p, "unexpected end of line");
        return 0;
    }
    tk_set(t, pt);
    pp_adv(p);
    return 1;
}

CC_NOINLINE static uint32_t pp_decl(pp_t *p)
{
    otc_comp_t *c = p->c;
    otc_func_t *f = FN(c, p->fn);
    tok_t t, s, nm, v;
    uint32_t decl;
    if (!pp_next(p, &t))
        return 0;
    decl = pp_stmt_node(p, OTC_N_DECL, &t);
    if (!decl)
        return 0;
    if (tk_word_eq(p, &t, "MENU")) {
        uint32_t nstr = 0, lfirst = 0, llast = 0, action = 0, prevref = 0, cell, m;
        for (;;) {
            if (!pp_next(p, &s))
                return 0;
            if (s.kind != T_STR)
                return pp_raise_lit(p, "menu expects strings: menu \"LEVEL\" >> \"ACTION\"");
            cell = cc_alloc(c, (uint32_t)sizeof(otc_cell_t));
            if (!cell) {
                p->err = 1;
                return 0;
            }
            PTR(c, otc_cell_t, cell)->ref = cc_str(c, tk_ptr(p, &s), tk_len(&s), 0);
            if (!PTR(c, otc_cell_t, cell)->ref) {
                p->err = 1;
                return 0;
            }
            if (llast)
                PTR(c, otc_cell_t, llast)->next = cell;
            else
                lfirst = cell;
            llast = cell;
            nstr++;
            if (!pp_peek(p, 0))
                break;
            if (!pp_next(p, &s))
                return 0;
            if (!(s.kind == T_SYM && tk_len(&s) == 2u))
                return pp_raise_lit(p, "menu levels are separated by >>");
        }
        (void)prevref;
        if (nstr < 2u)
            return pp_raise_lit(p, "menu needs at least one level and the action: menu \"LEVEL\" >> \"ACTION\"");
        {
            uint32_t q, ln, i = 0;
            for (q = lfirst; q; q = PTR(c, otc_cell_t, q)->next, i++) {
                const uint8_t *sp = cc_sp(c, PTR(c, otc_cell_t, q)->ref, &ln);
                if (i + 1u == nstr) {
                    action = PTR(c, otc_cell_t, q)->ref;
                    if (ln > 13u) {
                        mb_begin(c);
                        mb_lit(c, "script name \"");
                        mb_bytes(c, sp, ln);
                        mb_lit(c, "\" longer than 13 chars (clipped on screen)");
                        cc_func_warn(c, p->fn, p->line_no, mb_end(c));
                    }
                } else if (ln > 11u) {
                    mb_begin(c);
                    mb_lit(c, "menu category \"");
                    mb_bytes(c, sp, ln);
                    mb_lit(c, "\" longer than 11 chars (put under \"SCRIPTS\")");
                    cc_func_warn(c, p->fn, p->line_no, mb_end(c));
                } else if (ln > 9u) {
                    mb_begin(c);
                    mb_lit(c, "menu category \"");
                    mb_bytes(c, sp, ln);
                    mb_lit(c, "\" longer than 9 chars (clipped on screen)");
                    cc_func_warn(c, p->fn, p->line_no, mb_end(c));
                }
            }
        }
        /* Python reports every level before the action (strs[:-1] first): the loop above visits them in order,
         * the action is last, so the order is the same */
        if (nstr > 2u) {
            mb_begin(c);
            mb_lit(c, "submenus not supported by the menu yet");
            cc_func_warn(c, p->fn, p->line_no, mb_end(c));
        }
        m = cc_alloc(c, (uint32_t)sizeof(otc_menu_t));
        if (!m) {
            p->err = 1;
            return 0;
        }
        PTR(c, otc_menu_t, m)->line = p->line_no;
        PTR(c, otc_menu_t, m)->action = action;
        PTR(c, otc_menu_t, m)->nlevels = nstr - 1u;
        PTR(c, otc_menu_t, m)->lfirst = lfirst;
        f = FN(c, p->fn);
        if (f->menu_last)
            PTR(c, otc_menu_t, f->menu_last)->next = m;
        else
            f->menu_first = m;
        f->menu_last = m;
        f->nmenus++;
    } else if (tk_word_eq(p, &t, "PAGE")) {                 /* page "LABEL" */
        const uint8_t *lp;
        uint32_t ln, ref;
        if (!pp_next(p, &s))
            return 0;
        if (s.kind != T_STR)
            return pp_raise_lit(p, "page expects a string: page \"LABEL\"");
        if (f->page_line)
            return pp_raise_lit(p, "duplicate page declaration (a function has at most one)");
        lp = tk_ptr(p, &s);                                 /* (the lexer already refuses a byte outside 0x20..0x7e) */
        ln = tk_len(&s);
        ref = cc_str(c, lp, ln, 0);
        if (!ref) {
            p->err = 1;
            return 0;
        }
        if (!ln) {
            mb_begin(c);
            mb_lit(c, "page label is empty");
            cc_func_err(c, p->fn, p->line_no, mb_end(c), 0);
        } else if (ln > OTC_L_PAGE_LABEL_MAX) {
            mb_begin(c);
            mb_lit(c, "page label \"");
            mb_bytes(c, lp, ln);
            mb_lit(c, "\" is longer than ");
            mb_dec(c, OTC_L_PAGE_LABEL_MAX);
            mb_lit(c, " characters");
            cc_func_err(c, p->fn, p->line_no, mb_end(c), 0);
        }
        f = FN(c, p->fn);
        f->page_line = p->line_no;
        f->page_label = ref;
    } else if (tk_word_eq(p, &t, "MODULE")) {               /* gone: a live script uses `in` blocks */
        return pp_raise_lit(p, "module is gone: use in clock n");
    } else if (tk_word_eq(p, &t, "CLOCK")) {                /* gone: a live script uses `in` blocks */
        return pp_raise_lit(p, "clock declaration is gone: use in clock n");
    } else if (tk_word_eq(p, &t, "VAR")) {                  /* var NAME INIT */
        otc_live_t *lv;
        const uint8_t *np_p;
        uint32_t np_n, vref, vr;
        int32_t init;
        int np;
        if (!pp_next(p, &nm))
            return 0;
        np_p = tk_ptr(p, &nm);
        np_n = tk_len(&nm);
        np = nm.kind == T_STR ? NP_QUOTED : cc_name_problem(np_p, np_n, tk_up(&nm), nm.kind == T_WORD);
        if (np) {
            mb_begin(c);
            mb_lit(c, "var ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            mb_lit(c, ": ");
            cc_np_text(c, np, np_p, np_n, tk_up(&nm));
            return pp_raise(p);
        }
        {
            uint32_t a;
            for (a = 0; a < f->nargs; a++)
                if (cc_ref_eq(c, PTR(c, uint32_t, f->args)[a] & 0x7FFFFFFFu, np_p, np_n, tk_up(&nm)))
                    break;
            if (a < f->nargs) {
                mb_begin(c);
                mb_lit(c, "var ");
                mb_show(c, np_p, np_n, tk_up(&nm));
                mb_lit(c, " has the name of an argument");
                return pp_raise(p);
            }
        }
        if (cc_cmap_find(c, np_p, np_n, tk_up(&nm)) || cc_fmap_find(c, np_p, np_n, tk_up(&nm))) {
            mb_begin(c);
            mb_lit(c, "var ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            mb_lit(c, " has the name of a const or a function");
            return pp_raise(p);
        }
        if (pp_find_param(p, np_p, np_n, tk_up(&nm))) {
            mb_begin(c);
            mb_lit(c, "var ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            mb_lit(c, " has the name of a param");
            return pp_raise(p);
        }
        if (pp_find_var(p, np_p, np_n, tk_up(&nm))) {
            mb_begin(c);
            mb_lit(c, "duplicate var declaration ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            return pp_raise(p);
        }
        lv = pp_live(c, f);
        if (!lv) {
            p->err = 1;
            return 0;
        }
        if (lv->nvars >= 16u) {
            mb_begin(c);
            mb_lit(c, "too many vars in function ");
            mb_show_ref(c, f->name);
            mb_lit(c, " (at most 16)");
            return pp_raise(p);
        }
        if (!pp_peek(p, 0)) {
            mb_begin(c);
            mb_lit(c, "var ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            mb_lit(c, ": initial value is missing");
            return pp_raise(p);
        }
        pp_next(p, &v);
        if (!pp_int_const(p, &v, &init)) {
            mb_begin(c);
            mb_lit(c, "var ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            mb_lit(c, ": initial value must be a number or an integer const");
            return pp_raise(p);
        }
        vref = cc_str(c, np_p, np_n, tk_up(&nm));
        vr = cc_alloc(c, (uint32_t)sizeof(otc_var_t));
        if (!vref || !vr) {
            p->err = 1;
            return 0;
        }
        f = FN(c, p->fn);
        lv = pp_live(c, f);
        if (!lv) {
            p->err = 1;
            return 0;
        }
        PTR(c, otc_var_t, vr)->next = 0;
        PTR(c, otc_var_t, vr)->name = vref;
        PTR(c, otc_var_t, vr)->init = init;
        PTR(c, otc_var_t, vr)->slot = lv->nvars;
        PTR(c, otc_var_t, vr)->line = p->line_no;
        PTR(c, otc_var_t, vr)->scope = c->cur_ib;
        if (lv->var_last)
            PTR(c, otc_var_t, lv->var_last)->next = vr;
        else
            lv->var_first = vr;
        lv->var_last = vr;
        lv->nvars++;
        ND(c, decl)->val = vref;
    } else if (tk_word_eq(p, &t, "LAUNCH")) {               /* launch N: the live script's first-wake quantum */
        otc_live_t *lv = pp_live(c, f);
        int32_t ticks;
        if (!lv) {
            p->err = 1;
            return 0;
        }
        if (lv->launch_line)
            return pp_raise_lit(p, "duplicate launch declaration (a function has at most one)");
        if (!pp_peek(p, 0))
            return pp_raise_lit(p, "launch expects a number of ticks: launch n");
        pp_next(p, &v);
        if (!pp_int_const(p, &v, &ticks))
            return pp_raise_lit(p, "launch n: n must be a number or an integer const");
        if (ticks < 1) {
            mb_begin(c);
            mb_lit(c, "launch n: n must be >= 1 (got ");
            mb_dec(c, ticks);
            mb_lit(c, ")");
            return pp_raise(p);
        }
        if (ticks > 32767) {
            mb_begin(c);
            mb_lit(c, "launch n: n must be <= 32767 (got ");
            mb_dec(c, ticks);
            mb_lit(c, ")");
            return pp_raise(p);
        }
        lv->launch_line = p->line_no;
        lv->launch_ticks = (uint32_t)ticks;
    } else if (tk_word_eq(p, &t, "INPUT")) {
        if (!pp_next(p, &s))
            return 0;
        if (s.kind != T_WORD || !tk_word_eq(p, &s, "SAMPLE"))
            return pp_raise_lit(p, "input expects sample");
        if (f->flags & OTC_FF_INPUT_SAMPLE)
            return pp_raise_lit(p, "duplicate input declaration");
        f = FN(c, p->fn);
        f->flags |= OTC_FF_INPUT_SAMPLE;
    } else if (tk_word_eq(p, &t, "ON")) {
        const tok_t *ph = pp_peek(p, 0);
        if (!ph)
            return pp_raise_lit(p, "on expects a host name (on trig, on loop 4 ...)");
        pp_next(p, &s);
        if (s.kind != T_WORD && s.kind != T_COND)
            return pp_raise_lit(p, "on expects a host name (on trig, on loop 4 ...)");
        pp_skip_all(p);
        mb_begin(c);
        mb_lit(c, cc_in_set(OTC_SET_FUTURE_HOSTS, tk_ptr(p, &s), tk_len(&s), 1) ? "host not available yet: on "
                                                                                 : "unknown host: on ");
        mb_show(c, tk_ptr(p, &s), tk_len(&s), 1);
        cc_func_warn(c, p->fn, p->line_no, mb_end(c));
    } else if (tk_word_eq(p, &t, "PARAM")) {
        int32_t vals[3];
        uint32_t i, prm, nref;
        int np;
        uint8_t src_kind = 0, src_ch = 0, src_num = 0;
        static const char *const what[3] = {"default", "min", "max"};
        const uint8_t *np_p;
        uint32_t np_n;
        if (!pp_next(p, &nm))
            return 0;
        np_p = tk_ptr(p, &nm);
        np_n = tk_len(&nm);
        np = nm.kind == T_STR ? NP_QUOTED                /* the text of a string is not a name */
                              : cc_name_problem(np_p, np_n, tk_up(&nm), nm.kind == T_WORD);
        if (np) {
            mb_begin(c);
            mb_lit(c, "param ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            mb_lit(c, ": ");
            cc_np_text(c, np, np_p, np_n, tk_up(&nm));
            return pp_raise(p);
        }
        {
            uint32_t a;
            for (a = 0; a < f->nargs; a++)
                if (cc_ref_eq(c, PTR(c, uint32_t, f->args)[a] & 0x7FFFFFFFu, np_p, np_n, tk_up(&nm)))
                    break;
            if (a < f->nargs) {
                mb_begin(c);
                mb_lit(c, "param ");
                mb_show(c, np_p, np_n, tk_up(&nm));
                mb_lit(c, " has the name of an argument");
                return pp_raise(p);
            }
        }
        if (cc_cmap_find(c, np_p, np_n, tk_up(&nm)) || cc_fmap_find(c, np_p, np_n, tk_up(&nm))) {
            mb_begin(c);
            mb_lit(c, "param ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            mb_lit(c, " has the name of a const or a function");
            return pp_raise(p);
        }
        if (pp_find_param(p, np_p, np_n, tk_up(&nm))) {
            mb_begin(c);
            mb_lit(c, "duplicate param ");
            mb_show(c, np_p, np_n, tk_up(&nm));
            return pp_raise(p);
        }
        if (np_n > 11u) {
            mb_begin(c);
            mb_lit(c, "param \"");
            mb_bytes(c, np_p, np_n);
            mb_lit(c, "\" label longer than 11 chars (clipped on screen)");
            cc_func_warn(c, p->fn, p->line_no, mb_end(c));
        }
        {
            uint8_t pkind = OTC_PK_INT;
            uint32_t folder_path = 0;
            uint32_t nlabels = 0;
            uint32_t olabels[16];
            const tok_t *peek_tp = pp_peek(p, 0);
            if (peek_tp && peek_tp->kind == T_WORD) {
                if (tk_word_eq(p, peek_tp, "BOOL")) {
                    pkind = OTC_PK_BOOL;
                    pp_next(p, &v);
                } else if (tk_word_eq(p, peek_tp, "OPTION")) {
                    pkind = OTC_PK_OPTION;
                    pp_next(p, &v);
                } else if (tk_word_eq(p, peek_tp, "FOLDER")) {
                    pkind = OTC_PK_FOLDER;
                    pp_next(p, &v);
                } else if (tk_word_eq(p, peek_tp, "INT")) {
                    pkind = OTC_PK_INT;
                    pp_next(p, &v);
                }
            }

            if (pkind == OTC_PK_BOOL) {
                int32_t bdef = 0;
                uint32_t k;
                if (!pp_peek(p, 0)) {
                    mb_begin(c);
                    mb_lit(c, "param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": bool default is missing");
                    return pp_raise(p);
                }
                pp_next(p, &v);
                if (v.kind == T_NUM) {
                    if (v.val != 0 && v.val != 1) {
                        mb_begin(c);
                        mb_lit(c, "param ");
                        mb_show(c, np_p, np_n, tk_up(&nm));
                        mb_lit(c, ": bool default must be 0 or 1 (got ");
                        mb_dec(c, v.val);
                        mb_byte(c, ')');
                        return pp_raise(p);
                    }
                    bdef = v.val;
                } else if (v.kind == T_WORD && (tk_word_eq(p, &v, "YES") || tk_word_eq(p, &v, "TRUE"))) {
                    bdef = 1;
                } else if (v.kind == T_WORD && (tk_word_eq(p, &v, "NO") || tk_word_eq(p, &v, "FALSE"))) {
                    bdef = 0;
                } else if (v.kind == T_WORD && (k = cc_cmap_find(c, tk_ptr(p, &v), tk_len(&v), 1)) != 0 &&
                           !CN(c, k)->bad && CN(c, k)->type == OTC_CT_INT) {
                    int32_t val = (int32_t)CN(c, k)->val;
                    if (val != 0 && val != 1) {
                        mb_begin(c);
                        mb_lit(c, "param ");
                        mb_show(c, np_p, np_n, tk_up(&nm));
                        mb_lit(c, ": bool default must be 0 or 1 (got ");
                        mb_dec(c, val);
                        mb_byte(c, ')');
                        return pp_raise(p);
                    }
                    bdef = val;
                } else {
                    mb_begin(c);
                    mb_lit(c, "param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": bool default must be 0, 1, yes or no");
                    return pp_raise(p);
                }
                vals[0] = bdef;
                vals[1] = 0;
                vals[2] = 1;
            } else if (pkind == OTC_PK_OPTION) {
                int32_t odef = 0;
                uint32_t k;
                if (!pp_peek(p, 0)) {
                    mb_begin(c);
                    mb_lit(c, "param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": option default is missing");
                    return pp_raise(p);
                }
                pp_next(p, &v);
                if (v.kind == T_NUM) {
                    odef = v.val;
                } else if (v.kind == T_WORD && (k = cc_cmap_find(c, tk_ptr(p, &v), tk_len(&v), 1)) != 0 &&
                           !CN(c, k)->bad && CN(c, k)->type == OTC_CT_INT) {
                    odef = (int32_t)CN(c, k)->val;
                } else {
                    mb_begin(c);
                    mb_lit(c, "param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": option default index must be a number or integer const");
                    return pp_raise(p);
                }
                while (pp_peek(p, 0)) {
                    tok_t lt;
                    pp_next(p, &lt);
                    if (lt.kind != T_STR) {
                        mb_begin(c);
                        mb_lit(c, "param ");
                        mb_show(c, np_p, np_n, tk_up(&nm));
                        mb_lit(c, ": option labels must be strings");
                        return pp_raise(p);
                    }
                    if (tk_len(&lt) > 12u) {
                        mb_begin(c);
                        mb_lit(c, "option param ");
                        mb_show(c, np_p, np_n, tk_up(&nm));
                        mb_lit(c, ": label \"");
                        mb_bytes(c, tk_ptr(p, &lt), tk_len(&lt));
                        mb_lit(c, "\" is longer than 12 characters");
                        return pp_raise(p);
                    }
                    if (nlabels < 16u) {
                        uint32_t lref = cc_str(c, tk_ptr(p, &lt), tk_len(&lt), 0);
                        if (!lref) { p->err = 1; return 0; }
                        olabels[nlabels] = lref;
                    }
                    nlabels++;
                }
                if (nlabels < 2u) {
                    mb_begin(c);
                    mb_lit(c, "option param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, " needs at least 2 choices");
                    return pp_raise(p);
                }
                if (nlabels > 16u) {
                    mb_begin(c);
                    mb_lit(c, "option param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, " has more than 16 choices");
                    return pp_raise(p);
                }
                if (!(0 <= odef && odef < (int32_t)nlabels)) {
                    mb_begin(c);
                    mb_lit(c, "param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": need min <= default <= max (got ");
                    mb_dec(c, odef);
                    mb_lit(c, " 0 ");
                    mb_dec(c, (int32_t)nlabels - 1);
                    mb_byte(c, ')');
                    return pp_raise(p);
                }
                vals[0] = odef;
                vals[1] = 0;
                vals[2] = (int32_t)nlabels - 1;
            } else if (pkind == OTC_PK_FOLDER) {
                uint32_t nfolder = 0, q;
                if (!pp_peek(p, 0)) {
                    mb_begin(c);
                    mb_lit(c, "param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": folder default path is missing");
                    return pp_raise(p);
                }
                pp_next(p, &v);
                if (v.kind != T_STR) {
                    mb_begin(c);
                    mb_lit(c, "param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": folder default must be a string path");
                    return pp_raise(p);
                }
                if (tk_len(&v) > 64u) {
                    mb_begin(c);
                    mb_lit(c, "folder param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": path is longer than 64 characters");
                    return pp_raise(p);
                }
                for (q = f->param_first; q; q = PTR(c, otc_param_t, q)->next) {
                    if (PTR(c, otc_param_t, q)->kind == OTC_PK_FOLDER)
                        nfolder++;
                }
                if (nfolder >= 4u) {
                    mb_begin(c);
                    mb_lit(c, "more than 4 folder params in one function");
                    return pp_raise(p);
                }
                folder_path = cc_str(c, tk_ptr(p, &v), tk_len(&v), 0);
                if (!folder_path) { p->err = 1; return 0; }
                vals[0] = 0;
                vals[1] = 0;
                vals[2] = 0;
            } else {
                for (i = 0; i < 3; i++) {
                    uint32_t k;
                    if (!pp_peek(p, 0)) {
                        mb_begin(c);
                        mb_lit(c, "param name default min max: ");
                        mb_lit(c, what[i]);
                        mb_lit(c, " is missing");
                        return pp_raise(p);
                    }
                    pp_next(p, &v);
                    if (v.kind == T_NUM) {
                        vals[i] = v.val;
                    } else if (v.kind == T_WORD && (k = cc_cmap_find(c, tk_ptr(p, &v), tk_len(&v), 1)) != 0 &&
                               !CN(c, k)->bad && CN(c, k)->type == OTC_CT_INT) {
                        vals[i] = (int32_t)CN(c, k)->val;
                    } else {
                        mb_begin(c);
                        mb_lit(c, "param ");
                        mb_show(c, np_p, np_n, tk_up(&nm));
                        mb_lit(c, ": ");
                        mb_lit(c, what[i]);
                        mb_lit(c, " must be a number or an integer const");
                        return pp_raise(p);
                    }
                }
                if (!(vals[1] <= vals[0] && vals[0] <= vals[2])) {
                    mb_begin(c);
                    mb_lit(c, "param ");
                    mb_show(c, np_p, np_n, tk_up(&nm));
                    mb_lit(c, ": need min <= default <= max (got ");
                    mb_dec(c, vals[0]);
                    mb_byte(c, ' ');
                    mb_dec(c, vals[1]);
                    mb_byte(c, ' ');
                    mb_dec(c, vals[2]);
                    mb_byte(c, ')');
                    return pp_raise(p);
                }
                {                                  /* an int param may be bound to a MIDI CC, note or velocity */
                    const tok_t *st = pp_peek(p, 0);
                    if (st && st->kind == T_WORD &&
                        (tk_word_eq(p, st, "CC") || tk_word_eq(p, st, "NOTE") || tk_word_eq(p, st, "VEL"))) {
                        tok_t sk, ct;
                        int32_t ch = 0, num = 0;
                        const char *kw;
                        pp_next(p, &sk);
                        if (tk_word_eq(p, &sk, "CC")) {
                            src_kind = 1;
                            kw = "cc";
                        } else if (tk_word_eq(p, &sk, "NOTE")) {
                            src_kind = 2;
                            kw = "note";
                        } else {
                            src_kind = 3;
                            kw = "vel";
                        }
                        if (!pp_peek(p, 0)) {
                            mb_begin(c);
                            mb_lit(c, "param ");
                            mb_show(c, np_p, np_n, tk_up(&nm));
                            mb_lit(c, " ");
                            mb_lit(c, kw);
                            mb_lit(c, src_kind == 1 ? ": channel and cc number required" : ": channel required");
                            return pp_raise(p);
                        }
                        pp_next(p, &ct);
                        if (pp_find_param(p, tk_ptr(p, &ct), tk_len(&ct), tk_up(&ct))) {
                            mb_begin(c);
                            mb_lit(c, "param ");
                            mb_show(c, np_p, np_n, tk_up(&nm));
                            mb_lit(c, " ");
                            mb_lit(c, kw);
                            mb_lit(c, ": channel cannot be a param (dynamic param not supported)");
                            return pp_raise(p);
                        }
                        if (!pp_int_const(p, &ct, &ch)) {
                            mb_begin(c);
                            mb_lit(c, "param ");
                            mb_show(c, np_p, np_n, tk_up(&nm));
                            mb_lit(c, " ");
                            mb_lit(c, kw);
                            mb_lit(c, ": channel must be 1..16");
                            return pp_raise(p);
                        }
                        if (!(1 <= ch && ch <= 16)) {
                            mb_begin(c);
                            mb_lit(c, "param ");
                            mb_show(c, np_p, np_n, tk_up(&nm));
                            mb_lit(c, " ");
                            mb_lit(c, kw);
                            mb_lit(c, ": channel must be 1..16 (got ");
                            mb_dec(c, ch);
                            mb_lit(c, ")");
                            return pp_raise(p);
                        }
                        src_ch = (uint8_t)ch;
                        if (src_kind == 1) {
                            if (!pp_peek(p, 0)) {
                                mb_begin(c);
                                mb_lit(c, "param ");
                                mb_show(c, np_p, np_n, tk_up(&nm));
                                mb_lit(c, " cc: cc number is missing");
                                return pp_raise(p);
                            }
                            pp_next(p, &ct);
                            if (pp_find_param(p, tk_ptr(p, &ct), tk_len(&ct), tk_up(&ct))) {
                                mb_begin(c);
                                mb_lit(c, "param ");
                                mb_show(c, np_p, np_n, tk_up(&nm));
                                mb_lit(c, " cc: cc number cannot be a param (dynamic param not supported)");
                                return pp_raise(p);
                            }
                            if (!pp_int_const(p, &ct, &num)) {
                                mb_begin(c);
                                mb_lit(c, "param ");
                                mb_show(c, np_p, np_n, tk_up(&nm));
                                mb_lit(c, " cc: cc number must be 0..127");
                                return pp_raise(p);
                            }
                            if (!(0 <= num && num <= 127)) {
                                mb_begin(c);
                                mb_lit(c, "param ");
                                mb_show(c, np_p, np_n, tk_up(&nm));
                                mb_lit(c, " cc: cc number must be 0..127 (got ");
                                mb_dec(c, num);
                                mb_lit(c, ")");
                                return pp_raise(p);
                            }
                            src_num = (uint8_t)num;
                        }
                    }
                }
            }

            if (f->nparams >= OTC_L_MAX_PARAMS) {
                mb_begin(c);
                mb_lit(c, "more than ");
                mb_dec(c, OTC_L_MAX_PARAMS);
                mb_lit(c, " param in one function");
                return pp_raise(p);
            }
            prm = cc_alloc(c, (uint32_t)sizeof(otc_param_t));
            nref = cc_str(c, np_p, np_n, tk_up(&nm));
            if (!prm || !nref) {
                p->err = 1;
                return 0;
            }
            PTR(c, otc_param_t, prm)->name = nref;
            PTR(c, otc_param_t, prm)->def = vals[0];
            PTR(c, otc_param_t, prm)->lo = vals[1];
            PTR(c, otc_param_t, prm)->hi = vals[2];
            PTR(c, otc_param_t, prm)->kind = pkind;
            PTR(c, otc_param_t, prm)->src_kind = src_kind;
            PTR(c, otc_param_t, prm)->src_ch = src_ch;
            PTR(c, otc_param_t, prm)->src_num = src_num;
            PTR(c, otc_param_t, prm)->path = (pkind == OTC_PK_FOLDER) ? folder_path : 0;
            PTR(c, otc_param_t, prm)->nlabels = (pkind == OTC_PK_OPTION) ? (uint8_t)nlabels : 0;
            if (pkind == OTC_PK_OPTION) {
                uint32_t l;
                for (l = 0; l < nlabels; l++)
                    PTR(c, otc_param_t, prm)->labels[l] = olabels[l];
            }
            f = FN(c, p->fn);
            if (f->param_last)
                PTR(c, otc_param_t, f->param_last)->next = prm;
            else
                f->param_first = prm;
            f->param_last = prm;
            f->nparams++;
            ND(c, decl)->val = nref;
        }
    } else if (tk_word_eq(p, &t, "CONFIRM")) {
        otc_live_t *lv = pp_live(c, f);                /* (the live record also keeps the declaration's line) */
        if (!lv) {
            p->err = 1;
            return 0;
        }
        lv->confirm_decl_line = p->line_no;
        if (pp_peek(p, 0)) {
            pp_next(p, &s);
            if (s.kind != T_STR)
                return pp_raise_lit(p, "confirm takes an optional string: confirm \"TEXT\"");
            if (tk_len(&s) && !f->confirm_text) {           /* the first non-empty text is the box's question (spec 14.1) */
                uint32_t ref = cc_str(c, tk_ptr(p, &s), tk_len(&s), 0);
                if (!ref) {
                    p->err = 1;
                    return 0;
                }
                f = FN(c, p->fn);
                f->confirm_line = p->line_no;          /* the text's line (the dump shows it) */
                f->confirm_text = ref;
            }
            if (tk_len(&s) > 28u) {
                mb_begin(c);
                mb_lit(c, "confirm text \"");
                mb_bytes(c, tk_ptr(p, &s), tk_len(&s));
                mb_lit(c, "\" longer than 28 chars (clipped in dialog)");
                cc_func_warn(c, p->fn, p->line_no, mb_end(c));
            }
        }
        f->flags |= OTC_FF_CONFIRM;
    }
    {
        const tok_t *left = pp_peek(p, 0);
        if (left) {
            tok_t lt;
    tk_set(&lt, left);
            mb_begin(c);
            mb_lit(c, "unexpected ");
            mb_tokshow(p, &lt);
            mb_lit(c, " after ");
            mb_show(c, tk_ptr(p, &t), tk_len(&t), 1);
            mb_lit(c, " declaration");
            return pp_raise(p);
        }
    }
    return decl;
}

/* parse_in_block(): `in clock N:`, `in note [ch]:`, `in cc ch n:`.  Returns 1 + the index of the block
 * (the value of otc_line_t.ib), or 0 (the ParseError is in p->errmsg). */
CC_NOINLINE static uint32_t pp_in_block(pp_t *p, int opens)
{
    otc_comp_t *c = p->c;
    otc_func_t *f = FN(c, p->fn);
    otc_live_t *lv;
    tok_t t, k, v;
    uint32_t q, ib, line = p->line_no;
    int32_t ticks = 0, ch = -1, num = 0;
    uint8_t kind = 0;
    uint32_t gate_name = 0, gate_namelen = 0;
    if (!pp_next(p, &t))
        return 0;
    if (!pp_peek(p, 0))
        return pp_raise_lit(p, "in expects an input specifier (in clock N:, in note [ch]:, in cc ch n:, in trig t [all]:)");
    pp_next(p, &k);
    if (tk_word_eq(p, &k, "CLOCK")) {
        if (!pp_peek(p, 0) || tk_is_sym(p, pp_peek(p, 0), ':'))
            return pp_raise_lit(p, "in clock expects a number of ticks: in clock N:");
        pp_next(p, &v);
        if (!pp_int_const(p, &v, &ticks))
            return pp_raise_lit(p, "in clock N: N must be a number or an integer const");
        if (ticks < 1) {
            mb_begin(c);
            mb_lit(c, "in clock N: N must be >= 1 (got ");
            mb_dec(c, ticks);
            mb_lit(c, ")");
            return pp_raise(p);
        }
        kind = OTC_IK_CLOCK;
    } else if (tk_word_eq(p, &k, "NOTE")) {
        if (pp_peek(p, 0) && !tk_is_sym(p, pp_peek(p, 0), ':')) {
            pp_next(p, &v);
            if (!pp_int_const(p, &v, &ch))
                return pp_raise_lit(p, "in note: channel must be 1..16 or an integer const");
            if (!(1 <= ch && ch <= 16)) {
                mb_begin(c);
                mb_lit(c, "in note: channel must be 1..16 (got ");
                mb_dec(c, ch);
                mb_lit(c, ")");
                return pp_raise(p);
            }
        }
        kind = OTC_IK_NOTE;
    } else if (tk_word_eq(p, &k, "CC")) {
        if (!pp_peek(p, 0) || tk_is_sym(p, pp_peek(p, 0), ':'))
            return pp_raise_lit(p, "in cc expects channel and cc number: in cc ch n:");
        pp_next(p, &v);
        if (!pp_int_const(p, &v, &ch))
            return pp_raise_lit(p, "in cc: channel must be 1..16 or an integer const");
        if (!(1 <= ch && ch <= 16)) {
            mb_begin(c);
            mb_lit(c, "in cc: channel must be 1..16 (got ");
            mb_dec(c, ch);
            mb_lit(c, ")");
            return pp_raise(p);
        }
        if (!pp_peek(p, 0) || tk_is_sym(p, pp_peek(p, 0), ':'))
            return pp_raise_lit(p, "in cc ch n: cc number is missing");
        pp_next(p, &v);
        if (!pp_int_const(p, &v, &num))
            return pp_raise_lit(p, "in cc: cc number must be 0..127 or an integer const");
        if (!(0 <= num && num <= 127)) {
            mb_begin(c);
            mb_lit(c, "in cc: cc number must be 0..127 (got ");
            mb_dec(c, num);
            mb_lit(c, ")");
            return pp_raise(p);
        }
        kind = OTC_IK_CC;
    } else if (tk_word_eq(p, &k, "TRIG")) {
        int32_t t;
        uint32_t genre = 0;
        if (!pp_peek(p, 0) || tk_is_sym(p, pp_peek(p, 0), ':'))
            return pp_raise_lit(p, "in trig expects a track: in trig t [all]:");
        pp_next(p, &v);
        if (!pp_int_const(p, &v, &t))
            return pp_raise_lit(p, "in trig: track must be 1..8 or an integer const");
        if (!(1 <= t && t <= 8)) {
            mb_begin(c);
            mb_lit(c, "in trig: track must be 1..8 (got ");
            mb_dec(c, t);
            mb_lit(c, ")");
            return pp_raise(p);
        }
        if (pp_peek(p, 0) && tk_word_eq(p, pp_peek(p, 0), "ALL")) {
            pp_adv(p);
            genre = 1;
        } else if (pp_peek(p, 0) && tk_word_eq(p, pp_peek(p, 0), "SCRIPT")) {
            /* 1.1 leaves the script trigs out; the same literal as tools/otype.py's IN_TRIG_SCRIPT_REFUSED */
            return pp_raise_lit(p, "in trig t script: is not available in 1.1: use in trig t: or in trig t all:");
        }
        ch = t;
        num = (int32_t)genre;
        kind = OTC_IK_TRIG;
    } else if (tk_word_eq(p, &k, "GATE")) {
        tok_t nm;
        int32_t n;
        int np;
        if (!pp_peek(p, 0) || tk_is_sym(p, pp_peek(p, 0), ':'))
            return pp_raise_lit(p, "in gate expects a script name and an output: in gate NAME n:");
        pp_next(p, &v);
        if (v.kind != T_WORD) {
            mb_begin(c);
            mb_lit(c, "in gate: expected a function name, got ");
            mb_tokshow(p, &v);
            return pp_raise(p);
        }
        tk_set(&nm, &v);
        np = cc_name_problem(tk_ptr(p, &nm), tk_len(&nm), 1, 1);
        if (np) {
            mb_begin(c);
            mb_lit(c, "in gate: ");
            cc_np_text(c, np, tk_ptr(p, &nm), tk_len(&nm), 1);
            return pp_raise(p);
        }
        if (tk_len(&nm) > 8u) {
            mb_begin(c);
            mb_lit(c, "in gate: name is too long (");
            mb_dec(c, (int32_t)tk_len(&nm));
            mb_lit(c, " > 8 characters)");
            return pp_raise(p);
        }
        gate_name = pp_text(p, &nm);
        if (p->err)
            return 0;
        gate_namelen = tk_len(&nm);
        if (!pp_peek(p, 0) || tk_is_sym(p, pp_peek(p, 0), ':'))
            return pp_raise_lit(p, "in gate: output must be 1..4 or an integer const");
        pp_next(p, &v);
        if (!pp_int_const(p, &v, &n))
            return pp_raise_lit(p, "in gate: output must be 1..4 or an integer const");
        if (!(1 <= n && n <= 4)) {
            mb_begin(c);
            mb_lit(c, "in gate: output must be 1..4 (got ");
            mb_dec(c, n);
            mb_lit(c, ")");
            return pp_raise(p);
        }
        kind = OTC_IK_GATE;
        ch = n;
    } else {
        mb_begin(c);
        mb_lit(c, "unknown input kind ");
        mb_show(c, tk_ptr(p, &k), tk_len(&k), 1);
        mb_lit(c, ": use in clock n, in note [ch], in cc ch n, in trig t [all]");
        return pp_raise(p);
    }
    if (pp_peek(p, 0) && tk_is_sym(p, pp_peek(p, 0), ':'))
        pp_adv(p);
    if (pp_peek(p, 0)) {
        tok_t lt;
        tk_set(&lt, pp_peek(p, 0));
        mb_begin(c);
        mb_lit(c, "unexpected ");
        mb_tokshow(p, &lt);
        mb_lit(c, " after in ");
        mb_show(c, tk_ptr(p, &k), tk_len(&k), 1);
        mb_lit(c, " declaration");
        return pp_raise(p);
    }
    if (!opens) {
        mb_begin(c);
        mb_lit(c, "in ");
        mb_show(c, tk_ptr(p, &k), tk_len(&k), 1);
        mb_lit(c, ": line must end with ':'");
        return pp_raise(p);
    }
    lv = pp_live(c, f);
    if (!lv) {
        p->err = 1;
        return 0;
    }
    for (q = lv->ib_first; q; q = PTR(c, otc_in_t, q)->next) {
        const otc_in_t *b = PTR(c, otc_in_t, q);
        if (b->kind != kind)
            continue;
        if (kind == OTC_IK_CLOCK && b->ticks == (uint32_t)ticks) {
            mb_begin(c);
            mb_lit(c, "duplicate in clock ");
            mb_dec(c, ticks);
            return pp_raise(p);
        }
        if (kind == OTC_IK_NOTE && (int32_t)b->ch == (ch < 0 ? 0 : ch)) {
            if (ch < 0)
                return pp_raise_lit(p, "duplicate in note");
            mb_begin(c);
            mb_lit(c, "duplicate in note ");
            mb_dec(c, ch);
            return pp_raise(p);
        }
        if (kind == OTC_IK_CC && (int32_t)b->ch == ch && (int32_t)b->num == num) {
            mb_begin(c);
            mb_lit(c, "duplicate in cc ");
            mb_dec(c, ch);
            mb_byte(c, ' ');
            mb_dec(c, num);
            return pp_raise(p);
        }
        if (kind == OTC_IK_TRIG && (int32_t)b->ch == ch && (int32_t)b->num == num) {
            mb_begin(c);
            mb_lit(c, "duplicate in trig ");
            mb_dec(c, ch);
            if (num == 1)
                mb_lit(c, " all");
            return pp_raise(p);
        }
        if (kind == OTC_IK_GATE && b->name == gate_name && (int32_t)b->ch == ch) {
            mb_begin(c);
            mb_lit(c, "duplicate in gate ");
            mb_show_ref(c, gate_name);
            mb_byte(c, ' ');
            mb_dec(c, ch);
            return pp_raise(p);
        }
    }
    ib = cc_alloc(c, (uint32_t)sizeof(otc_in_t));
    if (!ib) {
        p->err = 1;
        return 0;
    }
    f = FN(c, p->fn);
    PTR(c, otc_in_t, ib)->next = 0;
    PTR(c, otc_in_t, ib)->line = line;
    PTR(c, otc_in_t, ib)->kind = kind;
    PTR(c, otc_in_t, ib)->ticks = (uint32_t)ticks;
    PTR(c, otc_in_t, ib)->ch = (uint32_t)(ch < 0 ? 0 : ch);
    PTR(c, otc_in_t, ib)->num = (uint32_t)num;
    PTR(c, otc_in_t, ib)->name = gate_name;
    PTR(c, otc_in_t, ib)->namelen = gate_namelen;
    if (lv->ib_last)
        PTR(c, otc_in_t, lv->ib_last)->next = ib;
    else
        lv->ib_first = ib;
    lv->ib_last = ib;
    lv->nib++;
    if (kind == OTC_IK_CLOCK && !f->clock_line) {
        f->clock_line = line;
        f->clock_ticks = (uint32_t)ticks;
    }
    f->module_line = line;                 /* a live script: hosts & MODULE, the name is the function's */
    return lv->nib;
}

/* ---- lines ---------------------------------------------------------------------------------------------- */
static void pp_init(pp_t *p, otc_comp_t *c, uint32_t fn, const li_t *li, uint32_t no)
{
    cc_zero(p, (uint32_t)sizeof *p);
    p->c = c;
    p->fn = fn;
    p->body = li->body;
    p->blen = li->blen;
    p->lx.s = li->body;
    p->lx.n = li->blen;
    p->line_no = no;
}

/* parse_line(): the Line (always one) */
CC_NOINLINE static uint32_t pp_line(otc_comp_t *c, uint32_t fn, const li_t *li, uint32_t no)
{
    pp_t p;
    uint32_t ln = cc_alloc(c, (uint32_t)sizeof(otc_line_t)), mods_last = 0;
    int first;
    otc_line_t *l;
    if (!ln)
        return 0;
    l = LN(c, ln);
    l->no = no;
    l->indent = li->indent;
    l->first_word = cc_str(c, li->body, li->first_len, 1);
    if (li->opens)
        l->flags |= OTC_LF_OPENS;
    if (!l->first_word)
        return 0;
    if (!li->ok) {
        l->flags |= OTC_LF_ERROR;
        l->flags &= (uint8_t)~OTC_LF_OPENS;                 /* a line with an error never opens a block */
        return ln;
    }
    pp_init(&p, c, fn, li, no);
    for (first = 1;; first = 0) {
        const tok_t *t = pp_peek(&p, 0);
        uint32_t s, m;
        if (first && t->kind == T_WORD && tk_word_eq(&p, t, "IN")) {
            s = pp_in_block(&p, li->opens);
            if (s) {
                l = LN(c, ln);
                l->ib = (uint16_t)s;
                l->flags |= OTC_LF_OPENS;
                return ln;
            }
            break;
        }
        if (first && t->kind == T_WORD && cc_in_set(OTC_SET_DECLS, p.body + t->a, t->b - t->a, 1)) {
            s = pp_decl(&p);
            if (s) {
                l = LN(c, ln);
                l->body = s;
                l->flags |= OTC_LF_DECL;
                return ln;
            }
            break;
        }
        if (t->kind == T_WORD && cc_in_set(OTC_SET_MODIFIERS, p.body + t->a, t->b - t->a, 1)) {
            if (LN(c, ln)->mods && (tk_word_eq(&p, t, "ELIF") || tk_word_eq(&p, t, "ELSE"))) {
                pp_raise_w(&p, t, " without if (elif and else must start the line)");
                break;
            }
            m = pp_modifier(&p);
            if (!m)
                break;
            if (mods_last)
                ND(c, mods_last)->next = m;
            else
                LN(c, ln)->mods = m;
            mods_last = m;
            if (!pp_peek(&p, 0)) {
                LN(c, ln)->flags |= OTC_LF_OPENS;
                return ln;
            }
            continue;
        }
        s = pp_statement(&p);
        if (!s)
            break;
        if (ND(c, s)->kind == OTC_N_CTXSTMT && tk_is_sym(&p, pp_peek(&p, 0), ':')) {
            pp_adv(&p);
            m = pp_node(&p, OTC_N_M_CTX);
            if (!m)
                break;
            ND(c, m)->first = ND(c, s)->first;
            ND(c, m)->text = ND(c, s)->text;
            if (mods_last)
                ND(c, mods_last)->next = m;
            else
                LN(c, ln)->mods = m;
            mods_last = m;
            if (!pp_peek(&p, 0)) {
                LN(c, ln)->flags |= OTC_LF_OPENS;
                return ln;
            }
            continue;
        }
        if (pp_peek(&p, 0)) {
            if (!pp_leftover(&p, s))
                break;
        }
        l = LN(c, ln);
        l->body = s;
        l->flags &= (uint8_t)~OTC_LF_OPENS;
        return ln;
    }
    if (c->err)
        return 0;
    /* a ParseError: the line is an error, what was built is dropped */
    cc_func_err(c, fn, no, p.errmsg, 0);
    l = LN(c, ln);
    l->flags |= OTC_LF_ERROR;
    l->mods = 0;
    l->body = 0;
    l->flags &= (uint8_t)~(OTC_LF_DECL | OTC_LF_OPENS);       /* (and it opens no block) */
    return ln;
}

/* ---- a function body ----------------------------------------------------------------------------------- */
static void pp_mark_words(otc_comp_t *c, uint32_t fn, const li_t *li)    /* the arguments that appear as a word */
{
    otc_func_t *f = FN(c, fn);
    lx_t lx;
    tok_t t;
    lexerr_t e;
    uint32_t i;
    lx.s = li->body;
    lx.n = li->blen;
    lx.i = 0;
    while (lex_next(&lx, &t, &e) > 0) {
        if (t.kind != T_WORD)
            continue;
        for (i = 0; i < f->nargs; i++)
            if (cc_ref_eq(c, PTR(c, uint32_t, f->args)[i] & 0x7FFFFFFFu, li->body + t.a, t.b - t.a, 1))
                PTR(c, uint32_t, f->args)[i] |= 0x80000000u;
    }
}

static uint32_t cc_preorder_next(const otc_comp_t *c, uint32_t l)
{
    const otc_line_t *ln = CPTR(c, otc_line_t, l);
    if (ln->child_first)
        return ln->child_first;
    for (;;) {
        ln = CPTR(c, otc_line_t, l);
        if (ln->next)
            return ln->next;
        l = ln->parent;
        if (!l)
            return 0;
    }
}

/* the next line in the preorder of the subtree of `root` (0 when the walk leaves it) */
static uint32_t cc_sub_next(const otc_comp_t *c, uint32_t l, uint32_t root)
{
    if (CPTR(c, otc_line_t, l)->child_first)
        return CPTR(c, otc_line_t, l)->child_first;
    while (l != root) {
        if (CPTR(c, otc_line_t, l)->next)
            return CPTR(c, otc_line_t, l)->next;
        l = CPTR(c, otc_line_t, l)->parent;
    }
    return 0;
}

static int cc_ref_in_set(const otc_comp_t *c, uint32_t set, uint32_t ref)
{
    uint32_t n;
    const uint8_t *p = cc_sp(c, ref, &n);
    return cc_in_set(set, p, n, 0);
}

static int cc_ref_ci_eq(const otc_comp_t *c, uint32_t ref, const char *upper)
{
    uint32_t n;
    const uint8_t *p = cc_sp(c, ref, &n);
    return cc_ci_eq(p, n, upper);
}

static int cc_first_kind(const otc_comp_t *c, const otc_line_t *l)    /* the kind of the first modifier, 0 if none */
{
    return l->mods ? CPTR(c, otc_node_t, l->mods)->kind : 0;
}

static void cc_err_decl(otc_comp_t *c, uint32_t fn, const otc_line_t *l, const char *pre, const char *post)
{
    mb_begin(c);
    mb_lit(c, pre);
    mb_show_ref(c, l->first_word);
    mb_lit(c, post);
    cc_func_err(c, fn, l->no, mb_end(c), 0);
}

static void cc_empty_block(otc_comp_t *c, uint32_t fn, uint32_t no)
{
    mb_begin(c);
    mb_lit(c, "empty block: a line ending with ':' needs more-indented lines below it");
    cc_func_err(c, fn, no, mb_end(c), 0);
}

/* BodyParser.parse() of a registered function */
CC_NOINLINE static void cc_parse_func(otc_comp_t *c, uint32_t fn)
{
    const uint8_t *text = c->src[FN(c, fn)->file].text;
    lines_t ls;
    li_t li;
    uint32_t sp = 1, pending = 0, ln, k, no;
    int seen_stmt = 0, blind = 0;
    c->stk_ind[0] = 0xFFFFFFFFu;
    c->stk_own[0] = 0;                                      /* owner 0 = the root of the function */
    c->stk_ib[0] = 0;
    c->cur_ib = 0;
    cc_lines_init(&ls, text, FN(c, fn)->body_end, FN(c, fn)->body_off, FN(c, fn)->body_line);
    while (cc_lines_next(&ls)) {
        uint32_t own, prev, first_kind, lastp;
        if (c->err)
            return;
        no = ls.no;
        cc_lex_line(&li, text + ls.start, ls.end - ls.start);
        if (li.blank || li.indent == 0)
            continue;                                       /* not a line of fn.lines */
        for (k = 0; k < li.nerr; k++)
            cc_func_err(c, fn, no, cc_lexerr_msg(c, &li.err[k], li.body), 0);
        if (!li.ok)
            blind = 1;
        else if (FN(c, fn)->nargs)
            pp_mark_words(c, fn, &li);
        /* Python checks the pending block and the indentation BEFORE parse_line: the diagnostics of this
         * line come after the empty-block / indentation ones (emission order is compared byte for byte). */
        if (pending) {
            if (li.indent > LN(c, pending)->indent) {
                if (sp >= OTC_BLOCK_STACK) {
                    cc_fail(c, OTC_CE_BLOCKS);
                    return;
                }
                c->stk_ind[sp] = li.indent;
                c->stk_own[sp] = pending;
                c->stk_ib[sp] = LN(c, pending)->ib ? LN(c, pending)->ib : c->stk_ib[sp - 1];
                sp++;
                if (sp - 1u > OTC_L_MAX_NEST) {
                    mb_begin(c);
                    mb_lit(c, "blocks nested deeper than ");
                    mb_dec(c, OTC_L_MAX_NEST);
                    cc_func_err(c, fn, no, mb_end(c), 0);
                }
            } else {
                cc_empty_block(c, fn, LN(c, pending)->no);
            }
            pending = 0;
        }
        if (c->stk_ind[0] == 0xFFFFFFFFu)
            c->stk_ind[0] = li.indent;
        {
            int popped = 0;
            while (li.indent < c->stk_ind[sp - 1] && sp > 1u) {
                sp--;
                popped = 1;
            }
            if (li.indent > c->stk_ind[sp - 1] && popped)
                cc_err_lit(c, fn, no, "indentation does not match any enclosing block");
            else if (li.indent > c->stk_ind[sp - 1])
                cc_err_lit(c, fn, no, "unexpected indentation (no block was opened above)");
            else if (li.indent < c->stk_ind[sp - 1])
                cc_err_lit(c, fn, no, "indentation does not match the function body");
        }
        own = c->stk_own[sp - 1];
        c->cur_ib = c->stk_ib[sp - 1];
        ln = pp_line(c, fn, &li, no);
        if (!ln)
            return;
        if (LN(c, ln)->ib && sp > 1u) {
            mb_begin(c);
            mb_lit(c, "in block must be at the root of the function");
            cc_func_err(c, fn, no, mb_end(c), 0);
        }
        lastp = own ? LN(c, own)->child_last : FN(c, fn)->root_last;
        prev = lastp;
        first_kind = (uint32_t)cc_first_kind(c, LN(c, ln));
        if ((first_kind == OTC_N_M_ELIF || first_kind == OTC_N_M_ELSE) && !(LN(c, ln)->flags & OTC_LF_ERROR)) {
            int ok = 0;
            if (prev) {
                const otc_line_t *pl = LN(c, prev);
                int pk = cc_first_kind(c, pl);
                ok = (pl->flags & OTC_LF_ERROR) || pk == OTC_N_M_IF || pk == OTC_N_M_ELIF;
            }
            if (!ok) {
                mb_begin(c);
                mb_lit(c, first_kind == OTC_N_M_ELIF ? "elif" : "else");
                mb_lit(c, " without if (it must directly follow a line that starts with if or elif, at the same "
                          "indentation)");
                cc_func_err(c, fn, no, mb_end(c), 0);
            }
        }
        if (prev)
            LN(c, prev)->next = ln;
        else if (own)
            LN(c, own)->child_first = ln;
        else
            FN(c, fn)->root_first = ln;
        if (own) {
            LN(c, own)->child_last = ln;
            LN(c, ln)->parent = own;
        } else {
            FN(c, fn)->root_last = ln;
        }
        if (LN(c, ln)->flags & OTC_LF_OPENS)
            pending = ln;
    }
    if (pending)
        cc_empty_block(c, fn, LN(c, pending)->no);
    if (blind)
        FN(c, fn)->flags |= OTC_FF_BLIND;
    /* declarations before the first statement */
    for (ln = FN(c, fn)->root_first; ln; ln = LN(c, ln)->next) {   /* declarations before the first statement */
        const otc_line_t *l = LN(c, ln);
        if ((l->flags & OTC_LF_DECL) ||
            ((l->flags & OTC_LF_ERROR) && cc_ref_in_set(c, OTC_SET_DECLS, l->first_word))) {
            if (seen_stmt)
                cc_err_decl(c, fn, l, "declaration ", " after the first statement");
        } else if (l->ib) {
            int seen_sub_stmt = 0;
            uint32_t sub;
            seen_stmt = 1;
            for (sub = l->child_first; sub; sub = LN(c, sub)->next) {
                const otc_line_t *sl = LN(c, sub);
                if (sl->flags & OTC_LF_DECL) {
                    if (!cc_ref_ci_eq(c, ND(c, sl->body)->text, "VAR"))
                        cc_err_decl(c, fn, sl, "declaration ", " inside a block");
                    else if (seen_sub_stmt)
                        cc_err_decl(c, fn, sl, "declaration ", " after the first statement");
                } else if (sl->body || sl->mods) {
                    seen_sub_stmt = 1;
                }
            }
        } else {
            seen_stmt = 1;
            if (FN(c, fn)->module_label && PTR(c, otc_live_t, FN(c, fn)->module_label)->nib)
                cc_err_lit(c, fn, l->no, "statement outside any in block (a live script organizes its code in 'in' blocks)");
        }
    }
    for (ln = FN(c, fn)->root_first; ln; ln = LN(c, ln)->next) {   /* declarations inside a block */
        uint32_t sub;
        if (LN(c, ln)->ib)
            continue;
        sub = cc_sub_next(c, ln, ln);
        while (sub) {
            if (LN(c, sub)->flags & OTC_LF_DECL)
                cc_err_decl(c, fn, LN(c, sub), "declaration ", " inside a block");
            sub = cc_sub_next(c, sub, ln);
        }
    }
    if (FN(c, fn)->nparams && !FN(c, fn)->nmenus && !FN(c, fn)->page_line && !FN(c, fn)->module_line) {
        for (ln = FN(c, fn)->root_first; ln; ln = cc_preorder_next(c, ln)) {
            const otc_line_t *l = LN(c, ln);
            if ((l->flags & OTC_LF_DECL) && cc_ref_ci_eq(c, ND(c, l->body)->text, "PARAM"))
                cc_err_lit(c, fn, l->no, "a brick cannot declare param; pass the value as an argument");
        }
    }
    if (FN(c, fn)->page_line) {                                  /* a page function has no argument, at most 6 params */
        if (FN(c, fn)->nargs)
            cc_err_lit(c, fn, FN(c, fn)->page_line, "a page function takes no arguments");
        if (FN(c, fn)->nparams > OTC_L_PAGE_MAX_PARAMS) {
            mb_begin(c);
            mb_lit(c, "page function has more than ");
            mb_dec(c, OTC_L_PAGE_MAX_PARAMS);
            mb_lit(c, " params");
            cc_func_err(c, fn, FN(c, fn)->page_line, mb_end(c), 0);
        }
        {
            uint32_t q;
            for (q = FN(c, fn)->param_first; q; q = PTR(c, otc_param_t, q)->next) {
                if (PTR(c, otc_param_t, q)->kind == OTC_PK_FOLDER) {
                    cc_err_lit(c, fn, FN(c, fn)->page_line, "page function cannot declare a folder param");
                    break;
                }
            }
        }
    }
    if (FN(c, fn)->module_label && PTR(c, otc_live_t, FN(c, fn)->module_label)->launch_line &&
        !PTR(c, otc_live_t, FN(c, fn)->module_label)->nib)
        cc_err_lit(c, fn, PTR(c, otc_live_t, FN(c, fn)->module_label)->launch_line, "launch is only allowed in a live script");
    if (FN(c, fn)->module_label && PTR(c, otc_live_t, FN(c, fn)->module_label)->nib) {   /* the rules of a live script */
        uint32_t nlen;
        (void)cc_sp(c, FN(c, fn)->name, &nlen);
        if (nlen > OTC_L_MODULE_LABEL_MAX) {
            mb_begin(c);
            mb_lit(c, "live script name ");
            mb_show_ref(c, FN(c, fn)->name);
            mb_lit(c, " is longer than ");
            mb_dec(c, OTC_L_MODULE_LABEL_MAX);
            mb_lit(c, " characters");
            cc_func_err(c, fn, FN(c, fn)->line, mb_end(c), 0);
        }
        if (FN(c, fn)->page_line)
            cc_err_lit(c, fn, FN(c, fn)->page_line, "a live script cannot declare a page");
        if (FN(c, fn)->nargs)
            cc_err_lit(c, fn, FN(c, fn)->line, "a live script takes no arguments");
        if (FN(c, fn)->flags & OTC_FF_CONFIRM) {
            uint32_t cline = (FN(c, fn)->module_label && PTR(c, otc_live_t, FN(c, fn)->module_label)->confirm_decl_line)
                              ? PTR(c, otc_live_t, FN(c, fn)->module_label)->confirm_decl_line : FN(c, fn)->line;
            cc_err_lit(c, fn, cline, "confirm not allowed in a live script");
        }
    }
    {
        int any = 0;
        for (ln = FN(c, fn)->root_first; ln && !any; ln = cc_preorder_next(c, ln)) {
            const otc_line_t *l = LN(c, ln);
            if (!(l->flags & OTC_LF_DECL) && !l->ib && !cc_ref_in_set(c, OTC_SET_DECLS, l->first_word))
                any = 1;
        }
        if (!any)
            cc_err_lit(c, fn, FN(c, fn)->line, "no statement in the body");
    }
}
