/* OType compiler front end, part 4: the canonical dump (test helper; format in
 * modules/otype/front_dump.py). Included by otc_comp.c; leave it out with -DOTC_NO_DUMP.
 * The recursion of the dump of an expression is bounded by the depth the parser accepts (OTC_EXPR_DEPTH + 2). */

typedef struct {
    char *out;
    uint32_t cap, len;
} dw_t;

static void dw_byte(dw_t *w, uint8_t b)
{
    if (w->len < w->cap)
        w->out[w->len] = (char)b;
    w->len++;
}

static void dw_bytes(dw_t *w, const uint8_t *p, uint32_t n)
{
    while (n--)
        dw_byte(w, *p++);
}

static void dw_lit(dw_t *w, const char *s)
{
    while (*s)
        dw_byte(w, (uint8_t)*s++);
}

static void dw_dec(dw_t *w, int32_t v)
{
    char tmp[12];
    uint32_t u = v < 0 ? 0u - (uint32_t)v : (uint32_t)v, n = 0;
    if (v < 0)
        dw_byte(w, '-');
    do {
        tmp[n++] = (char)('0' + u % 10u);
        u /= 10u;
    } while (u);
    while (n)
        dw_byte(w, (uint8_t)tmp[--n]);
}

static void dw_udec(dw_t *w, uint32_t u)
{
    char tmp[12];
    uint32_t n = 0;
    do {
        tmp[n++] = (char)('0' + u % 10u);
        u /= 10u;
    } while (u);
    while (n)
        dw_byte(w, (uint8_t)tmp[--n]);
}

static void dw_ref(dw_t *w, const otc_comp_t *c, uint32_t ref)
{
    uint32_t n;
    const uint8_t *p = cc_sp(c, ref, &n);
    dw_bytes(w, p, n);
}

/* show() of a stored string */
static void dw_show(dw_t *w, const otc_comp_t *c, uint32_t ref)
{
    uint32_t n, i;
    int inq = 0;
    const uint8_t *p = cc_sp(c, ref, &n);
    for (i = 0; i < n; i++) {
        if (p[i] == '"') {
            inq = !inq;
            dw_byte(w, p[i]);
        } else {
            dw_byte(w, inq ? p[i] : cc_low(p[i]));
        }
    }
}

static void dw_label(dw_t *w, const otc_comp_t *c, uint32_t file)
{
    dw_bytes(w, c->src[file].label, c->src[file].label_len);
}

static void dw_vals(dw_t *w, const otc_comp_t *c, uint32_t ref)       /* a list value: v,v,v */
{
    uint32_t n, i;
    const uint8_t *p = cc_sp(c, ref, &n);
    for (i = 0; i + 1u < n; i += 2) {
        int32_t v = (int32_t)(((uint32_t)p[i] << 8) | p[i + 1]);
        if (i)
            dw_byte(w, ',');
        dw_dec(w, v >= 32768 ? v - 65536 : v);
    }
}

static const char *const dw_kinds[] = {"", "INT", "STEP", "TRACK", "SLOT", "PARAM", "VALUE", "COND", "PCT", "FILE",
                                       "STR", "LIST", "TRACKS", "STEPSEL", "KW", "OPTKW", "REST", "USER", "ITEM",
                                       "CH", "VAL7", "DUR", "VAL14", "NAME", "OUT"};

static void dw_expr(dw_t *w, const otc_comp_t *c, uint32_t n)
{
    const otc_node_t *nd = CPTR(c, otc_node_t, n);
    uint32_t a;
    dw_byte(w, '(');
    switch (nd->kind) {
    case OTC_N_NUM:
        dw_lit(w, "num ");
        dw_ref(w, c, nd->text);
        dw_byte(w, ' ');
        dw_dec(w, (int32_t)nd->val);
        break;
    case OTC_N_STR:
        dw_lit(w, "str \"");
        dw_ref(w, c, nd->text);
        dw_byte(w, '"');
        break;
    case OTC_N_LIST:
        dw_lit(w, "list ");
        dw_ref(w, c, nd->text);
        dw_byte(w, ' ');
        dw_vals(w, c, nd->val);
        break;
    case OTC_N_COND:
        dw_lit(w, "cond ");
        dw_ref(w, c, nd->text);
        dw_byte(w, ' ');
        dw_ref(w, c, nd->val);
        break;
    case OTC_N_VAR:
    case OTC_N_VAR_NAMED:
    case OTC_N_ARG:
    case OTC_N_PARAM:
    case OTC_N_CONST:
    case OTC_N_KW:
        dw_lit(w, nd->kind == OTC_N_VAR ? "var " : nd->kind == OTC_N_VAR_NAMED ? "var_named " :
                  nd->kind == OTC_N_ARG ? "arg " :
                  nd->kind == OTC_N_PARAM ? "param " : nd->kind == OTC_N_CONST ? "const " : "kw ");
        dw_ref(w, c, nd->text);
        break;
    case OTC_N_OP:
    case OTC_N_CALL:
        if (nd->kind == OTC_N_OP) {
            dw_lit(w, "op ");
            dw_ref(w, c, nd->text);
            dw_byte(w, ' ');
            dw_lit(w, &otc_rstr[otc_reg[nd->op].name]);
        } else {
            dw_lit(w, "call ");
            dw_ref(w, c, nd->text);
        }
        for (a = nd->first; a; a = CPTR(c, otc_node_t, a)->next) {
            dw_byte(w, ' ');
            dw_lit(w, dw_kinds[CPTR(c, otc_node_t, a)->akind]);
            dw_byte(w, '=');
            dw_expr(w, c, a);
        }
        break;
    case OTC_N_PRED:
        dw_lit(w, "pred ");
        dw_ref(w, c, nd->text);
        dw_byte(w, ' ');
        dw_lit(w, &otc_rstr[otc_reg[nd->op].name]);
        break;
    case OTC_N_ASSIGN:
    case OTC_N_ASSIGN_VAR_NAMED:
        dw_lit(w, nd->kind == OTC_N_ASSIGN ? "assign " : "assign_var_named ");
        dw_ref(w, c, nd->text);
        dw_byte(w, ' ');
        dw_expr(w, c, nd->first);
        break;
    case OTC_N_OPSTMT:
    case OTC_N_CALLSTMT:
    case OTC_N_CTXSTMT:
        dw_lit(w, nd->kind == OTC_N_OPSTMT ? "opstmt " : nd->kind == OTC_N_CALLSTMT ? "callstmt " : "ctxstmt ");
        dw_ref(w, c, nd->text);
        dw_byte(w, ' ');
        dw_expr(w, c, nd->first);
        break;
    case OTC_N_SETSTMT:
        dw_lit(w, "setstmt ");
        dw_ref(w, c, nd->text);
        dw_byte(w, ' ');
        dw_lit(w, &otc_rstr[otc_reg[nd->op].name]);
        for (a = nd->first; a; a = CPTR(c, otc_node_t, a)->next) {
            dw_byte(w, ' ');
            dw_expr(w, c, a);
        }
        break;
    case OTC_N_BREAK:
    case OTC_N_STOP:
        dw_lit(w, nd->kind == OTC_N_BREAK ? "break " : "stop ");
        dw_ref(w, c, nd->text);
        break;
    case OTC_N_RET:
        dw_lit(w, "ret ");
        dw_ref(w, c, nd->text);
        if (nd->first) {
            dw_byte(w, ' ');
            dw_expr(w, c, nd->first);
        }
        break;
    case OTC_N_DECL:
        dw_lit(w, "decl ");
        dw_ref(w, c, nd->text);
        dw_byte(w, ' ');
        if (nd->val)
            dw_ref(w, c, nd->val);
        else
            dw_byte(w, '-');
        break;
    default:
        dw_lit(w, "?");
        break;
    }
    dw_byte(w, ')');
}

static void dw_mod(dw_t *w, const otc_comp_t *c, uint32_t m)
{
    static const char *const names[] = {"IF", "ELIF", "ELSE", "L", "EACH", "PROB", "EVERY", "SKIP", "CTX"};
    const otc_node_t *nd = CPTR(c, otc_node_t, m);
    uint32_t a;
    dw_byte(w, '(');
    dw_lit(w, names[nd->kind - OTC_N_M_IF]);
    if (nd->kind == OTC_N_M_CTX) {
        dw_byte(w, ' ');
        dw_expr(w, c, nd->first);
    } else {
        for (a = nd->first; a; a = CPTR(c, otc_node_t, a)->next) {
            dw_byte(w, ' ');
            dw_expr(w, c, a);
        }
    }
    dw_byte(w, ')');
}

static void dw_lines(dw_t *w, const otc_comp_t *c, uint32_t first)
{
    uint32_t l = first, depth = 1, m;
    while (l) {
        const otc_line_t *ln = CPTR(c, otc_line_t, l);
        uint32_t i;
        for (i = 0; i < depth; i++)
            dw_byte(w, '+');
        dw_byte(w, 'L');
        dw_dec(w, (int32_t)ln->no);
        dw_lit(w, " i");
        dw_dec(w, (int32_t)ln->indent);
        dw_lit(w, (ln->flags & OTC_LF_ERROR) ? " e1" : " e0");
        dw_lit(w, (ln->flags & OTC_LF_OPENS) ? " o1 f=" : " o0 f=");
        dw_ref(w, c, ln->first_word);
        dw_lit(w, " m=[");
        for (m = ln->mods; m; m = CPTR(c, otc_node_t, m)->next) {
            if (m != ln->mods)
                dw_byte(w, ',');
            dw_mod(w, c, m);
        }
        dw_lit(w, "] d=");
        if (ln->body && (ln->flags & OTC_LF_DECL))
            dw_expr(w, c, ln->body);
        else
            dw_byte(w, '-');
        dw_lit(w, " s=");
        if (ln->body && !(ln->flags & OTC_LF_DECL))
            dw_expr(w, c, ln->body);
        else
            dw_byte(w, '-');
        dw_byte(w, '\n');
        /* preorder, iteratively */
        if (ln->child_first) {
            l = ln->child_first;
            depth++;
        } else {
            for (;;) {
                ln = CPTR(c, otc_line_t, l);
                if (ln->next) {
                    l = ln->next;
                    break;
                }
                l = ln->parent;
                depth--;
                if (!l || depth == 0)
                    break;
            }
            if (depth == 0)
                l = 0;
        }
    }
}

uint32_t otc_front_dump(const void *ws, char *out, uint32_t cap)
{
    const otc_comp_t *c = (const otc_comp_t *)ws;
    dw_t w;
    uint32_t i, k, f, d, q;
    w.out = out;
    w.cap = cap;
    w.len = 0;
    if (c->err) {
        dw_lit(&w, "OVERFLOW ");
        dw_dec(&w, (int32_t)c->err);
        dw_byte(&w, '\n');
        goto done;
    }
    dw_lit(&w, "files ");
    dw_dec(&w, (int32_t)c->nloaded);
    dw_byte(&w, '\n');
    for (i = 0; i < c->nloaded; i++) {
        dw_lit(&w, "file ");
        dw_label(&w, c, i);
        dw_byte(&w, '\n');
    }
    dw_lit(&w, "consts ");
    dw_dec(&w, (int32_t)c->nconsts);
    dw_byte(&w, '\n');
    for (k = c->const_first; k; k = CPTR(c, otc_const_t, k)->next) {
        const otc_const_t *cn = CPTR(c, otc_const_t, k);
        static const char *const tn[] = {"-", "INT", "STR", "LIST", "COND"};
        dw_lit(&w, "const ");
        dw_label(&w, c, cn->file);
        dw_byte(&w, ' ');
        dw_dec(&w, (int32_t)cn->line);
        dw_byte(&w, ' ');
        dw_ref(&w, c, cn->name);
        dw_byte(&w, ' ');
        dw_lit(&w, tn[cn->type]);
        dw_byte(&w, ' ');
        if (cn->type == OTC_CT_INT) {
            dw_dec(&w, (int32_t)cn->val);
        } else if (cn->type == OTC_CT_STR) {
            dw_byte(&w, '"');
            dw_ref(&w, c, cn->val);
            dw_byte(&w, '"');
        } else if (cn->type == OTC_CT_LIST) {
            dw_byte(&w, '{');
            dw_vals(&w, c, cn->val);
            dw_byte(&w, '}');
        } else if (cn->type == OTC_CT_COND) {
            dw_ref(&w, c, cn->val);
        } else {
            dw_byte(&w, '-');
        }
        dw_lit(&w, cn->bad ? " bad=1\n" : " bad=0\n");
    }
    dw_lit(&w, "funcs ");
    dw_dec(&w, (int32_t)c->nfuncs);
    dw_byte(&w, '\n');
    for (f = c->func_first; f; f = CPTR(c, otc_func_t, f)->next) {
        const otc_func_t *fn = CPTR(c, otc_func_t, f);
        uint32_t a, nused = 0;
        dw_lit(&w, "func ");
        dw_label(&w, c, fn->file);
        dw_byte(&w, ' ');
        dw_dec(&w, (int32_t)fn->line);
        dw_byte(&w, ' ');
        dw_ref(&w, c, fn->name);
        dw_lit(&w, (fn->flags & OTC_FF_REG) ? " reg=1" : " reg=0");
        dw_lit(&w, (fn->flags & OTC_FF_HERR) ? " herr=1" : " herr=0");
        dw_lit(&w, (fn->flags & OTC_FF_CONFIRM) ? " confirm=1" : " confirm=0");
        dw_lit(&w, " nerr=");
        dw_dec(&w, (int32_t)fn->nerr);
        dw_lit(&w, (fn->flags & OTC_FF_BLIND) ? " blind=1" : " blind=0");
        dw_lit(&w, " args=");
        for (a = 0; a < fn->nargs; a++) {
            if (a)
                dw_byte(&w, ',');
            dw_ref(&w, c, CPTR(c, uint32_t, fn->args)[a] & 0x7FFFFFFFu);
        }
        if (!fn->nargs)
            dw_byte(&w, '-');
        dw_lit(&w, " used=");
        if ((fn->flags & OTC_FF_REG) && !(fn->flags & OTC_FF_BLIND)) {
            for (a = 0; a < fn->nargs; a++) {
                uint32_t v = CPTR(c, uint32_t, fn->args)[a];
                if (v & 0x80000000u) {
                    if (nused++)
                        dw_byte(&w, ',');
                    dw_ref(&w, c, v & 0x7FFFFFFFu);
                }
            }
        }
        if (!nused)
            dw_byte(&w, '-');
        dw_byte(&w, '\n');
        for (q = fn->param_first; q; q = CPTR(c, otc_param_t, q)->next) {
            const otc_param_t *pr = CPTR(c, otc_param_t, q);
            dw_lit(&w, "param ");
            dw_ref(&w, c, pr->name);
            dw_byte(&w, ' ');
            dw_dec(&w, pr->def);
            dw_byte(&w, ' ');
            dw_dec(&w, pr->lo);
            dw_byte(&w, ' ');
            dw_dec(&w, pr->hi);
            dw_byte(&w, '\n');
        }
        for (q = fn->menu_first; q; q = CPTR(c, otc_menu_t, q)->next) {
            const otc_menu_t *mn = CPTR(c, otc_menu_t, q);
            uint32_t cell;
            dw_lit(&w, "menu ");
            dw_dec(&w, (int32_t)mn->line);
            dw_lit(&w, " levels=");
            for (cell = mn->lfirst, i = 0; i < mn->nlevels; i++, cell = CPTR(c, otc_cell_t, cell)->next) {
                if (i)
                    dw_byte(&w, ',');
                dw_byte(&w, '"');
                dw_ref(&w, c, CPTR(c, otc_cell_t, cell)->ref);
                dw_byte(&w, '"');
            }
            if (!mn->nlevels)
                dw_byte(&w, '-');
            dw_lit(&w, " action=\"");
            dw_ref(&w, c, mn->action);
            dw_lit(&w, "\"\n");
        }
        if (fn->page_line) {
            dw_lit(&w, "page ");
            dw_dec(&w, (int32_t)fn->page_line);
            dw_lit(&w, " \"");
            dw_ref(&w, c, fn->page_label);
            dw_lit(&w, "\"\n");
        }
        if (fn->module_line) {
            dw_lit(&w, "module ");
            dw_dec(&w, (int32_t)fn->module_line);
            dw_lit(&w, " \"");
            dw_ref(&w, c, fn->name);
            dw_lit(&w, "\"\n");
        }
        if (fn->clock_line) {
            dw_lit(&w, "clock ");
            dw_dec(&w, (int32_t)fn->clock_line);
            dw_byte(&w, ' ');
            dw_dec(&w, (int32_t)fn->clock_ticks);
            dw_byte(&w, '\n');
        }
        if (fn->confirm_text) {
            dw_lit(&w, "confirm ");
            dw_dec(&w, (int32_t)fn->confirm_line);
            dw_lit(&w, " \"");
            dw_ref(&w, c, fn->confirm_text);
            dw_lit(&w, "\"\n");
        }
        dw_lines(&w, c, fn->root_first);
    }
    dw_lit(&w, "diags ");
    dw_dec(&w, (int32_t)c->ndiags);
    dw_byte(&w, '\n');
    for (d = c->diag_first; d; d = CPTR(c, otc_diag_t, d)->next) {
        const otc_diag_t *dg = CPTR(c, otc_diag_t, d);
        dw_lit(&w, dg->hidden ? "diag! " : "diag ");
        dw_label(&w, c, dg->file);
        dw_byte(&w, ':');
        dw_dec(&w, (int32_t)dg->line);
        dw_lit(&w, ": ");
        if (dg->func)
            dw_show(&w, c, CPTR(c, otc_func_t, dg->func)->name);
        else
            dw_byte(&w, '-');
        dw_lit(&w, dg->level == OTC_LV_ERROR ? ": error: " : ": warning: ");
        dw_ref(&w, c, dg->msg);
        dw_byte(&w, '\n');
    }
    dw_lit(&w, "file_errors ");
    dw_dec(&w, (int32_t)c->file_errors);
    dw_byte(&w, '\n');
done:
    if (w.len < w.cap)
        w.out[w.len] = 0;
    return w.len;
}

/* ---- one line per function after the checker and the dry run of the code generator (test helper) -------------
 *   chk N
 *   fn I NAME refused=R cost=C destr=D comp=X
 * C = max cost (saturated) or - when the function was not costed; comp = - (not compiled: refused by the checker),
 * `ok CODE LINES CTRS STACK` or `err LINE MESSAGE-KEY`. Python's twin is modules/otype/check_dump.py. */
uint32_t otc_check_dump(const void *ws, char *out, uint32_t cap)
{
    const otc_comp_t *c = (const otc_comp_t *)ws;
    dw_t w;
    uint32_t i;
    w.out = out;
    w.cap = cap;
    w.len = 0;
    if (c->err || !c->checked) {
        dw_lit(&w, "OVERFLOW ");
        dw_dec(&w, (int32_t)c->err);
        dw_byte(&w, '\n');
        goto done;
    }
    dw_lit(&w, "chk ");
    dw_dec(&w, (int32_t)c->nfuncs);
    dw_byte(&w, '\n');
    for (i = 0; i < c->nfuncs; i++) {
        const otc_fx_t *fx = FX(c, i);
        const otc_func_t *f = FNI(c, i);
        dw_lit(&w, "fn ");
        dw_dec(&w, (int32_t)i);
        dw_byte(&w, ' ');
        dw_show(&w, c, f->name);
        dw_lit(&w, f->nerr ? " refused=1 cost=" : " refused=0 cost=");
        if (fx->costed)
            dw_udec(&w, fx->cost);
        else
            dw_byte(&w, '-');
        dw_lit(&w, fx->destr ? " destr=1 comp=" : " destr=0 comp=");
        if (!fx->inorder) {
            dw_byte(&w, '-');
        } else if (!fx->cerr) {
            dw_lit(&w, "ok ");
            dw_dec(&w, (int32_t)fx->code_len);
            dw_byte(&w, ' ');
            dw_dec(&w, (int32_t)fx->nlines);
            dw_byte(&w, ' ');
            dw_dec(&w, (int32_t)fx->nctr);
            dw_byte(&w, ' ');
            dw_dec(&w, (int32_t)fx->maxstack);
            dw_byte(&w, ' ');
            dw_dec(&w, (int32_t)fx->maxctx);
            dw_byte(&w, ' ');
            dw_dec(&w, (int32_t)fx->maxctl);
            dw_byte(&w, ' ');
            dw_dec(&w, (int32_t)fx->nlocals);
        } else {
            dw_lit(&w, "err ");
            dw_dec(&w, (int32_t)fx->cerr_line);
            dw_lit(&w, fx->cerr == 1 ? " counters" : fx->cerr == 2 ? " stack" : fx->cerr == 4 ? " loop" : fx->cerr == 5 ? " context" :
                   fx->cerr == 6 ? " menus" : " code");
        }
        dw_byte(&w, '\n');
    }
done:
    if (w.len < w.cap)
        w.out[w.len] = 0;
    return w.len;
}
