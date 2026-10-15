/* OType compiler front end, part 2: loading the sources (`otype`, `const`, `def` lines) and
 * pass_names. Included by otc_comp.c. Mirrors tools/otype.py Program.load_text / load_const / load_def /
 * pass_names and check_sources (the caps on files and bytes). */

#define FN(c, o)  PTR(c, otc_func_t, o)
#define CN(c, o)  PTR(c, otc_const_t, o)

/* report(li, func): the lexical errors of a line, to the function (cur) or, without one, as file errors */
static void cc_report(otc_comp_t *c, uint32_t file, const li_t *li, uint32_t no, uint32_t fn)
{
    uint32_t k;
    for (k = 0; k < li->nerr; k++) {
        uint32_t msg = cc_lexerr_msg(c, &li->err[k], li->body);
        if (fn)
            cc_func_err(c, fn, no, msg, 0);
        else
            cc_file_err(c, file, no, msg);
    }
}

/* the first tokens of a line that lexed (at most `max`), and how many it has in all (capped at max + 1) */
static uint32_t cc_first_toks(const li_t *li, tok_t *tk, uint32_t max)
{
    lx_t lx;
    lexerr_t e;
    tok_t t;
    uint32_t n = 0;
    lx.s = li->body;
    lx.n = li->blen;
    lx.i = 0;
    while (n <= max && lex_next(&lx, &t, &e) > 0) {
        if (n < max)
            tk_set(&tk[n], &t);
        n++;
    }
    return n;
}

static uint32_t cc_list_ref(otc_comp_t *c, const uint8_t *body, uint32_t a, uint32_t b)
{
    int32_t v[OTC_L_MAX_LIST];
    uint32_t n = cc_list_vals(body, a, b, v), ref, i;
    uint8_t *d;
    ref = cc_alloc(c, 4u + 2u * n);
    if (!ref)
        return 0;
    *PTR(c, uint32_t, ref) = 2u * n;
    d = PTR(c, uint8_t, ref + 4);
    for (i = 0; i < n; i++) {
        d[2 * i] = (uint8_t)(((uint32_t)v[i] >> 8) & 255u);
        d[2 * i + 1] = (uint8_t)((uint32_t)v[i] & 255u);
    }
    return ref;
}

/* the canonical text of a COND token: n%, a:b or the word (upper case) */
static uint32_t cc_cond_ref(otc_comp_t *c, const uint8_t *body, const tok_t *t)
{
    if (t->sub == CS_WORD)
        return cc_str(c, body + t->a, t->b - t->a, 1);
    mb_begin(c);
    if (t->sub == CS_PROB) {
        mb_dec(c, t->val);
        mb_byte(c, '%');
    } else {
        mb_dec(c, t->val >> 4);
        mb_byte(c, ':');
        mb_dec(c, t->val & 15);
    }
    return mb_end(c);
}

static void cc_add_const_rec(otc_comp_t *c, uint32_t k)
{
    if (c->const_last)
        CN(c, c->const_last)->next = k;
    else
        c->const_first = k;
    c->const_last = k;
    c->nconsts++;
}

static uint32_t cc_new_const(otc_comp_t *c, uint32_t file, uint32_t line, const uint8_t *name, uint32_t nlen)
{
    uint32_t k = cc_alloc(c, (uint32_t)sizeof(otc_const_t));
    if (!k)
        return 0;
    CN(c, k)->file = (uint16_t)file;
    CN(c, k)->line = line;
    CN(c, k)->name = cc_str(c, name, nlen, 1);
    CN(c, k)->hash = cc_hash(name, nlen, 1);
    if (!CN(c, k)->name)
        return 0;
    return k;
}

CC_NOINLINE static void cc_load_const(otc_comp_t *c, uint32_t file, const li_t *li, uint32_t no)
{
    tok_t tk[3];
    uint32_t n, k, np;
    uint32_t i;
    otc_const_t *cn;
    /* (the errors of the line itself were reported once by cc_load_file) */
    if (!li->ok) {
        /* still register the name when readable, to avoid cascades: the second piece of the body */
        uint32_t a, b;
        i = 0;
        while (i < li->blen && !cc_ws(li->body[i]))
            i++;
        while (i < li->blen && cc_ws(li->body[i]))
            i++;
        a = i;
        while (i < li->blen && !cc_ws(li->body[i]))
            i++;
        b = i;
        if (b > a && cc_is_name(li->body + a, b - a, 1)) {
            k = cc_new_const(c, file, no, li->body + a, b - a);
            if (k) {
                CN(c, k)->bad = 1;
                cc_add_const_rec(c, k);
            }
        }
        return;
    }
    n = cc_first_toks(li, tk, 3);
    if (n < 2 || tk[1].kind != T_WORD) {
        mb_begin(c);
        mb_lit(c, "const expects a name and a value");
        cc_file_err(c, file, no, mb_end(c));
        return;
    }
    k = cc_new_const(c, file, no, li->body + tk[1].a, tk[1].b - tk[1].a);
    if (!k)
        return;
    cn = CN(c, k);
    np = (uint32_t)cc_name_problem(li->body + tk[1].a, tk[1].b - tk[1].a, 1, 1);
    if (np) {
        mb_begin(c);
        mb_lit(c, "const ");
        mb_show_ref(c, cn->name);
        mb_lit(c, ": ");
        cc_np_text(c, (int)np, li->body + tk[1].a, tk[1].b - tk[1].a, 1);
        cc_file_err(c, file, no, mb_end(c));
        CN(c, k)->bad = 1;
    }
    if (n < 3) {
        mb_begin(c);
        mb_lit(c, "const ");
        mb_show_ref(c, CN(c, k)->name);
        mb_lit(c, " has no value");
        cc_file_err(c, file, no, mb_end(c));
        CN(c, k)->bad = 1;
    } else if (n > 3) {
        mb_begin(c);
        mb_lit(c, "const takes a single constant (no expressions)");
        cc_file_err(c, file, no, mb_end(c));
        CN(c, k)->bad = 1;
    } else {
        const tok_t *v = &tk[2];
        if (v->kind == T_NUM) {
            CN(c, k)->type = OTC_CT_INT;
            CN(c, k)->val = (uint32_t)v->val;
        } else if (v->kind == T_STR) {
            CN(c, k)->type = OTC_CT_STR;
            CN(c, k)->val = cc_str(c, li->body + v->a, v->b - v->a, 0);
        } else if (v->kind == T_LIST) {
            CN(c, k)->type = OTC_CT_LIST;
            CN(c, k)->val = cc_list_ref(c, li->body, v->a, v->b);
        } else if (v->kind == T_COND || (v->kind == T_WORD && cc_ci_eq(li->body + v->a, v->b - v->a, "FILL"))) {
            CN(c, k)->type = OTC_CT_COND;
            CN(c, k)->val = v->kind == T_COND ? cc_cond_ref(c, li->body, v) : cc_str(c, (const uint8_t *)"FILL", 4, 1);
        } else {
            mb_begin(c);
            mb_lit(c, "const ");
            mb_show_ref(c, CN(c, k)->name);
            mb_lit(c, ": the value must be a number, a string, a list or a condition (no expressions)");
            cc_file_err(c, file, no, mb_end(c));
            CN(c, k)->bad = 1;
        }
    }
    cc_add_const_rec(c, k);
}

/* the parts of `def` line: asplit(code.replace("|", " | ")) */
typedef struct {
    const uint8_t *p;
    uint32_t n, i;
} pit_t;

static int cc_part_next(pit_t *it, uint32_t *a, uint32_t *b)
{
    uint32_t i = it->i, j;
    while (i < it->n && cc_ws(it->p[i]))
        i++;
    if (i >= it->n)
        return 0;
    if (it->p[i] == '|') {
        *a = i;
        *b = i + 1;
        it->i = i + 1;
        return 1;
    }
    for (j = i; j < it->n && !cc_ws(it->p[j]) && it->p[j] != '|'; j++)
        ;
    *a = i;
    *b = j;
    it->i = j;
    return 1;
}

static void cc_err_lit(otc_comp_t *c, uint32_t fn, uint32_t line, const char *s)
{
    mb_begin(c);
    mb_lit(c, s);
    cc_func_err(c, fn, line, mb_end(c), 0);
}

/* load_def(): the Func of a `def` line (always created). body_off / body_line: the line after it */
CC_NOINLINE static uint32_t cc_load_def(otc_comp_t *c, uint32_t file, const li_t *li, uint32_t no, uint32_t body_off,
                            uint32_t body_line)
{
    pit_t it;
    uint32_t a, b, np = 0, fn, k, i, rest;
    uint32_t pa[2], pb[2];
    int have_bar;
    it.p = li->body;
    it.n = li->blen;
    it.i = 0;
    while (cc_part_next(&it, &a, &b)) {                /* parts[0] is `def` */
        if (np < 2) {
            pa[np] = a;
            pb[np] = b;
        }
        np++;
    }
    fn = cc_alloc(c, (uint32_t)sizeof(otc_func_t));
    if (!fn)
        return 0;
    FN(c, fn)->file = (uint16_t)file;
    FN(c, fn)->line = no;
    FN(c, fn)->body_off = body_off;
    FN(c, fn)->body_end = body_off;
    FN(c, fn)->body_line = body_line;
    if (np > 1) {
        FN(c, fn)->name = cc_str(c, li->body + pa[1], pb[1] - pa[1], 1);
    } else {
        FN(c, fn)->name = cc_str(c, (const uint8_t *)"?", 1, 0);
    }
    if (!FN(c, fn)->name)
        return 0;
    {
        uint32_t nl;
        const uint8_t *nm = cc_sp(c, FN(c, fn)->name, &nl);
        FN(c, fn)->hash = cc_hash(nm, nl, 0);
    }
    if (c->func_last)
        FN(c, c->func_last)->next = fn;
    else
        c->func_first = fn;
    c->func_last = fn;
    c->nfuncs++;
    cc_report(c, file, li, no, fn);                    /* for m in li.errs: f.err(...) */
    if (np < 2) {
        cc_err_lit(c, fn, no, "def expects a function name");
        FN(c, fn)->flags |= OTC_FF_HERR;
        return fn;
    }
    {
        uint32_t nl;
        const uint8_t *nm = cc_sp(c, FN(c, fn)->name, &nl);
        int prob = cc_name_problem(nm, nl, 0, 1);
        if (prob) {
            mb_begin(c);
            cc_np_text(c, prob, nm, nl, 0);
            cc_func_err(c, fn, no, mb_end(c), 0);
            FN(c, fn)->flags |= OTC_FF_HERR;
        }
    }
    rest = np - 2u;
    if (!rest)
        return fn;
    /* the parts after the name */
    it.i = pb[1];
    cc_part_next(&it, &a, &b);                          /* rest[0] */
    have_bar = (b - a == 1u && li->body[a] == '|');
    if (!have_bar) {
        mb_begin(c);
        mb_lit(c, "expected '|' before the arguments (def ");
        mb_show_ref(c, FN(c, fn)->name);
        mb_lit(c, " | a b)");
        cc_func_err(c, fn, no, mb_end(c), 0);
        FN(c, fn)->flags |= OTC_FF_HERR;
        return fn;
    }
    if (rest == 1u) {
        mb_begin(c);
        mb_lit(c, "'|' without arguments: write def ");
        mb_show_ref(c, FN(c, fn)->name);
        mb_lit(c, " alone for no argument");
        cc_func_err(c, fn, no, mb_end(c), 0);
        return fn;
    }
    k = cc_alloc(c, 4u * (rest - 1u));
    if (!k)
        return fn;
    FN(c, fn)->args = k;
    for (i = 1; i < rest; i++) {
        uint32_t ref;
        const uint8_t *ap;
        uint32_t al, j;
        int prob;
        cc_part_next(&it, &a, &b);
        ap = li->body + a;
        al = b - a;
        if (al == 1u && (ap[0] == '|' || ap[0] == ':')) {
            mb_begin(c);
            mb_lit(c, "unexpected '");
            mb_byte(c, ap[0]);
            mb_lit(c, "' in the argument list");
            cc_func_err(c, fn, no, mb_end(c), 0);
            continue;
        }
        prob = cc_name_problem(ap, al, 1, 1);
        if (prob) {
            mb_begin(c);
            mb_lit(c, "argument ");
            mb_show(c, ap, al, 1);
            mb_lit(c, ": ");
            cc_np_text(c, prob, ap, al, 1);
            cc_func_err(c, fn, no, mb_end(c), 0);
        } else {
            for (j = 0; j < FN(c, fn)->nargs; j++)
                if (cc_ref_eq(c, PTR(c, uint32_t, FN(c, fn)->args)[j], ap, al, 1))
                    break;
            if (j < FN(c, fn)->nargs) {
                mb_begin(c);
                mb_lit(c, "duplicate argument ");
                mb_show(c, ap, al, 1);
                cc_func_err(c, fn, no, mb_end(c), 0);
            }
        }
        ref = cc_str(c, ap, al, 1);
        if (!ref)
            return fn;
        PTR(c, uint32_t, FN(c, fn)->args)[FN(c, fn)->nargs++] = ref;
    }
    if (FN(c, fn)->nargs > OTC_L_MAX_ARGS) {
        mb_begin(c);
        mb_lit(c, "takes ");
        mb_dec(c, (int32_t)FN(c, fn)->nargs);
        mb_lit(c, " arguments (maximum ");
        mb_dec(c, OTC_L_MAX_ARGS);
        mb_byte(c, ')');
        cc_func_err(c, fn, no, mb_end(c), 0);
    }
    return fn;
}

/* load_text() of one file */
CC_NOINLINE static void cc_load_file(otc_comp_t *c, uint32_t file)
{
    const uint8_t *text = c->src[file].text;
    uint32_t len = c->src[file].text_len;
    lines_t ls;
    li_t li;
    uint32_t cur = 0, before = c->func_last, no, f;
    int first_code = 1, refuse_set = 0;
    int32_t refuse = 0;
    cc_lines_init(&ls, text, len, 0, 1);
    while (cc_lines_next(&ls)) {
        if (c->err)
            return;
        no = ls.no;
        cc_lex_line(&li, text + ls.start, ls.end - ls.start);
        if (li.blank) {
            if (li.nerr)
                cc_report(c, file, &li, no, cur);
            continue;
        }
        if (li.indent == 0) {
            const uint8_t *w = li.body;
            uint32_t wn = li.first_len;
            if (cc_ci_eq(w, wn, "OTYPE")) {
                cc_report(c, file, &li, no, 0);
                if (cur)
                    FN(c, cur)->body_end = ls.start;
                cur = 0;
                if (!first_code) {
                    mb_begin(c);
                    mb_lit(c, "otype must be the first line of the file");
                    cc_file_err(c, file, no, mb_end(c));
                } else {
                    tok_t tk[2];
                    if (li.ok && cc_first_toks(&li, tk, 2) == 2u && tk[1].kind == T_NUM) {
                        if (tk[1].val != 1) {
                            refuse_set = 1;
                            refuse = tk[1].val;
                            mb_begin(c);
                            mb_lit(c, "otype ");
                            mb_dec(c, tk[1].val);
                            mb_lit(c, " is not supported (only otype 1): file refused");
                            cc_file_err(c, file, no, mb_end(c));
                        }
                    } else {
                        mb_begin(c);
                        mb_lit(c, "otype expects a version number");
                        cc_file_err(c, file, no, mb_end(c));
                    }
                }
            } else if (cc_ci_eq(w, wn, "CONST")) {
                cc_report(c, file, &li, no, 0);
                if (cur)
                    FN(c, cur)->body_end = ls.start;
                cur = 0;
                cc_load_const(c, file, &li, no);
            } else if (cc_ci_eq(w, wn, "DEF")) {
                if (cur)
                    FN(c, cur)->body_end = ls.start;
                cur = cc_load_def(c, file, &li, no, ls.pos, no + 1u);
                if (!cur)
                    return;
            } else {
                cc_report(c, file, &li, no, cur);
                mb_begin(c);
                mb_show(c, w, wn, 1);
                mb_lit(c, " at column 1: only def, const and otype start in column 1 (indent the body of a function)");
                if (cur)
                    cc_func_err(c, cur, no, mb_end(c), 0);
                else
                    cc_file_err(c, file, no, mb_end(c));
            }
            first_code = 0;
        } else {
            first_code = 0;
            if (!cur) {
                cc_report(c, file, &li, no, 0);
                mb_begin(c);
                mb_lit(c, "indented line outside any function");
                cc_file_err(c, file, no, mb_end(c));
            }
        }
    }
    if (cur)
        FN(c, cur)->body_end = len;
    if (refuse_set) {
        for (f = before ? FN(c, before)->next : c->func_first; f; f = FN(c, f)->next) {
            mb_begin(c);
            mb_lit(c, "file refused (otype ");
            mb_dec(c, refuse);
            mb_byte(c, ')');
            cc_func_err(c, f, FN(c, f)->line, mb_end(c), 1);
        }
    }
}

/* ---- pass_names ------------------------------------------------------------------------------------ */
CC_NOINLINE static void cc_pass_names(otc_comp_t *c)
{
    uint32_t f, k, i;
    /* the functions: the groups of a name among those without a header error */
    for (f = c->func_first; f; f = FN(c, f)->next) {
        otc_func_t *fn = FN(c, f);
        uint32_t nl, head;
        const uint8_t *nm;
        if (fn->flags & OTC_FF_HERR)
            continue;
        fn->flags |= OTC_FF_REG;
        nm = cc_sp(c, fn->name, &nl);
        head = cc_fmap_find(c, nm, nl, 0);
        if (!head) {
            uint32_t *bk = &c->fbucket[fn->hash & (OTC_HBUCKETS - 1u)];
            fn->hnext = *bk;
            *bk = f;
            fn->gtail = f;
        } else {
            FN(c, FN(c, head)->gtail)->gnext = f;
            FN(c, head)->gtail = f;
        }
    }
    for (f = c->func_first; f; f = FN(c, f)->next) {            /* duplicate function names */
        otc_func_t *fn = FN(c, f);
        uint32_t g;
        if (!fn->gtail || !fn->gnext)
            continue;                                           /* not the head of a group, or alone */
        i = 0;
        for (g = f; g; g = FN(c, g)->gnext, i++) {
            uint32_t other = i == 0 ? fn->gnext : f;
            mb_begin(c);
            mb_lit(c, "duplicate function name (also defined at ");
            mb_label(c, FN(c, other)->file);
            mb_byte(c, ':');
            mb_dec(c, (int32_t)FN(c, other)->line);
            mb_byte(c, ')');
            cc_func_err(c, g, FN(c, g)->line, mb_end(c), 0);
        }
    }
    /* the consts: the groups of a name (all of them, bad or not) */
    for (k = c->const_first; k; k = CN(c, k)->next) {
        otc_const_t *cn = CN(c, k);
        uint32_t nl, head;
        const uint8_t *nm = cc_sp(c, cn->name, &nl);
        head = cc_cmap_find(c, nm, nl, 0);
        if (!head) {
            uint32_t *bk = &c->cbucket[cn->hash & (OTC_HBUCKETS - 1u)];
            cn->hnext = *bk;
            *bk = k;
            cn->gtail = k;
        } else {
            CN(c, CN(c, head)->gtail)->gnext = k;
            CN(c, head)->gtail = k;
        }
    }
    for (k = c->const_first; k; k = CN(c, k)->next) {
        otc_const_t *cn = CN(c, k);
        uint32_t nl, g, fh;
        const uint8_t *nm;
        if (!cn->gtail)
            continue;
        if (cn->gnext) {
            i = 0;
            for (g = k; g; g = CN(c, g)->gnext, i++) {
                uint32_t other = i == 0 ? cn->gnext : k;
                CN(c, g)->bad = 1;
                mb_begin(c);
                mb_lit(c, "duplicate const ");
                mb_show_ref(c, cn->name);
                mb_lit(c, " (also defined at ");
                mb_label(c, CN(c, other)->file);
                mb_byte(c, ':');
                mb_dec(c, (int32_t)CN(c, other)->line);
                mb_byte(c, ')');
                cc_file_err(c, CN(c, g)->file, CN(c, g)->line, mb_end(c));
            }
        }
        nm = cc_sp(c, cn->name, &nl);
        fh = cc_fmap_find(c, nm, nl, 0);
        if (fh) {
            for (g = k; g; g = CN(c, g)->gnext) {
                CN(c, g)->bad = 1;
                mb_begin(c);
                mb_lit(c, "const ");
                mb_show_ref(c, cn->name);
                mb_lit(c, " has the name of the function at ");
                mb_label(c, FN(c, fh)->file);
                mb_byte(c, ':');
                mb_dec(c, (int32_t)FN(c, fh)->line);
                cc_file_err(c, CN(c, g)->file, CN(c, g)->line, mb_end(c));
            }
            for (g = fh; g; g = FN(c, g)->gnext) {
                mb_begin(c);
                mb_lit(c, "function has the name of the const at ");
                mb_label(c, cn->file);
                mb_byte(c, ':');
                mb_dec(c, (int32_t)cn->line);
                cc_func_err(c, g, FN(c, g)->line, mb_end(c), 0);
            }
        }
    }
    /* arguments must not shadow constants or functions */
    for (f = c->func_first; f; f = FN(c, f)->next) {
        otc_func_t *fn = FN(c, f);
        uint32_t a;
        for (a = 0; a < fn->nargs; a++) {
            uint32_t al, ref = PTR(c, uint32_t, fn->args)[a] & 0x7FFFFFFFu;
            const uint8_t *ap = cc_sp(c, ref, &al);
            if (cc_cmap_find(c, ap, al, 0)) {
                mb_begin(c);
                mb_lit(c, "argument ");
                mb_show(c, ap, al, 0);
                mb_lit(c, " hides the const of the same name");
                cc_func_err(c, f, fn->line, mb_end(c), 0);
            } else if (cc_fmap_find(c, ap, al, 0)) {
                mb_begin(c);
                mb_lit(c, "argument ");
                mb_show(c, ap, al, 0);
                mb_lit(c, " has the name of a function");
                cc_func_err(c, f, fn->line, mb_end(c), 0);
            }
        }
    }
}
