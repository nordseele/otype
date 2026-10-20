/* OType compiler front end, part 1: arena, strings, messages, lookups, the lexer.
 * Included by otc_comp.c (one translation unit). Mirrors tools/otype.py: lex_line, lex_tokens, classify_word,
 * parse_int, name_problem; see otc_comp.h. */

#define PTR(c, T, off)  ((T *)((uint8_t *)(c) + (off)))
#define CPTR(c, T, off) ((const T *)((const uint8_t *)(c) + (off)))

#if defined(__GNUC__)
#define CC_NOINLINE __attribute__((noinline))
#else
#define CC_NOINLINE
#endif

/* ---- gcc turns copy loops into memcpy calls, which the unit does not have ------------------------- */
#if defined(__GNUC__) && !defined(__clang__)
#define CC_NOIDIOM __attribute__((optimize("no-tree-loop-distribute-patterns")))
#else
#define CC_NOIDIOM
#endif

CC_NOIDIOM static void cc_zero(void *d, uint32_t n)
{
    uint8_t *p = (uint8_t *)d;
    while (n--)
        *p++ = 0;
}

/* ---- bytes: ASCII only ------------------------------------------------------------------- */
static uint8_t cc_up(uint8_t b)
{
    return (b >= 'a' && b <= 'z') ? (uint8_t)(b - 32) : b;
}

static uint8_t cc_low(uint8_t b)
{
    return (b >= 'A' && b <= 'Z') ? (uint8_t)(b + 32) : b;
}

static int cc_ws(uint8_t b)
{
    return b == ' ' || b == '\t';
}

static int cc_dig(uint8_t b)
{
    return b >= '0' && b <= '9';
}

static uint32_t cc_cstrlen(const char *s)
{
    uint32_t n = 0;
    while (s[n])
        n++;
    return n;
}

/* ---- the arena ------------------------------------------------------------------------------------ */
static void cc_fail(otc_comp_t *c, uint32_t code)
{
    if (!c->err)
        c->err = code;
}

/* n zeroed bytes, 4-aligned; the offset, or 0 when the arena is full (the run is then over: c->err) */
static uint32_t cc_alloc(otc_comp_t *c, uint32_t n)
{
    uint32_t a = (n + 3u) & ~3u, off;
    if (c->err)
        return 0;
    if (a < n || a > c->limit - c->top) {
        cc_fail(c, OTC_CE_ARENA);
        return 0;
    }
    off = c->top;
    c->top += a;
    cc_zero((uint8_t *)c + off, a);
    return off;
}

static uint32_t cc_str(otc_comp_t *c, const uint8_t *p, uint32_t n, int up)
{
    uint32_t ref = cc_alloc(c, 4u + n), i;
    uint8_t *d;
    if (!ref)
        return 0;
    *PTR(c, uint32_t, ref) = n;
    d = PTR(c, uint8_t, ref + 4);
    for (i = 0; i < n; i++)
        d[i] = up ? cc_up(p[i]) : p[i];
    return ref;
}

static const uint8_t *cc_sp(const otc_comp_t *c, uint32_t ref, uint32_t *len)
{
    *len = *CPTR(c, uint32_t, ref);
    return CPTR(c, uint8_t, ref + 4);
}

/* ---- messages: built at the top of the arena (nothing else may be allocated meanwhile) ------------ */
static void mb_begin(otc_comp_t *c)
{
    if (c->err)
        return;
    if (c->limit - c->top < 4u) {
        cc_fail(c, OTC_CE_ARENA);
        return;
    }
    c->msg = c->top;
    c->msg_len = 0;
    c->msg_inq = 0;
}

static void mb_byte(otc_comp_t *c, uint8_t b)
{
    uint32_t pos;
    if (c->err)
        return;
    pos = c->msg + 4u + c->msg_len;
    if (pos >= c->limit) {
        cc_fail(c, OTC_CE_ARENA);
        return;
    }
    *PTR(c, uint8_t, pos) = b;
    c->msg_len++;
}

static uint32_t mb_end(otc_comp_t *c)
{
    uint32_t end;
    if (c->err)
        return 0;
    *PTR(c, uint32_t, c->msg) = c->msg_len;
    end = c->msg + 4u + c->msg_len;
    end = (end + 3u) & ~3u;
    if (end > c->limit) {
        cc_fail(c, OTC_CE_ARENA);
        return 0;
    }
    c->top = end;
    return c->msg;
}

static void mb_bytes(otc_comp_t *c, const uint8_t *p, uint32_t n)
{
    while (n--)
        mb_byte(c, *p++);
}

static void mb_lit(otc_comp_t *c, const char *s)
{
    while (*s)
        mb_byte(c, (uint8_t)*s++);
}

static void mb_rstr(otc_comp_t *c, uint32_t off)      /* a string of the generated registry */
{
    mb_lit(c, &otc_rstr[off]);
}

static void mb_udec(otc_comp_t *c, uint32_t u)
{
    char tmp[12];
    uint32_t n = 0;
    do {
        tmp[n++] = (char)('0' + u % 10u);
        u /= 10u;
    } while (u);
    while (n)
        mb_byte(c, (uint8_t)tmp[--n]);
}

static void mb_dec(otc_comp_t *c, int32_t v)
{
    char tmp[12];
    uint32_t u = v < 0 ? 0u - (uint32_t)v : (uint32_t)v, n = 0;
    if (v < 0)
        mb_byte(c, '-');
    do {
        tmp[n++] = (char)('0' + u % 10u);
        u /= 10u;
    } while (u);
    while (n)
        mb_byte(c, (uint8_t)tmp[--n]);
}

static void mb_hex2(otc_comp_t *c, uint32_t b)
{
    static const char hx[] = "0123456789abcdef";
    mb_byte(c, (uint8_t)hx[(b >> 4) & 15u]);
    mb_byte(c, (uint8_t)hx[b & 15u]);
}

/* show(): lower case outside double quotes, the text between quotes is kept. `up`: the text is first made
 * upper case (a Python str that went through aupper). The quote state runs on from one part to the next
 * (show(" ".join(...))): mb_begin / mb_show_reset start it again. */
static void mb_show_part(otc_comp_t *c, const uint8_t *p, uint32_t n, int up)
{
    while (n--) {
        uint8_t b = *p++;
        if (up)
            b = cc_up(b);
        if (b == '"') {
            c->msg_inq = (uint8_t)!c->msg_inq;
            mb_byte(c, b);
        } else {
            mb_byte(c, c->msg_inq ? b : cc_low(b));
        }
    }
}

static void mb_show(otc_comp_t *c, const uint8_t *p, uint32_t n, int up)
{
    c->msg_inq = 0;
    mb_show_part(c, p, n, up);
}

static void mb_show_ref(otc_comp_t *c, uint32_t ref)      /* show() of a stored string */
{
    uint32_t n;
    const uint8_t *p = cc_sp(c, ref, &n);
    mb_show(c, p, n, 0);
}

static void mb_label(otc_comp_t *c, uint32_t file)
{
    mb_bytes(c, c->src[file].label, c->src[file].label_len);
}

/* ---- diagnostics ----------------------------------------------------------------------------------- */
/* A diagnostic is KEPT (message in the arena, record in the list of emission order) or only COUNTED, according to
 * c->dmode (a report holds OTC_MAX_DIAGS lines, the first ones after sorting):
 *   DM_KEEP   keep all (the front end alone: otc_front); with a cap `dcap` on the VISIBLE ones: the 1001st stops the
 *             run with OTC_CE_ABORT and otc_run starts again in the next two modes;
 *   DM_COUNT  keep nothing, only the OTC_MAX_DIAGS smallest sort keys (a max-heap): the last of them is the threshold;
 *   DM_SELECT keep the visible diagnostics whose key is <= the threshold: exactly OTC_MAX_DIAGS of them.
 * The key is (label bytes of the file, line, order of emission) as in Program.sorted_diags. A message that is not
 * kept is taken back from the top of the arena (it was the last thing built), so nothing leaks. A hidden
 * diagnostic is never visible. */
enum { DM_KEEP = 0, DM_COUNT = 1, DM_SELECT = 2 };

static const uint8_t *cc_label(const otc_comp_t *c, uint32_t file, uint32_t *len)
{
    static const uint8_t dash[1] = {'-'};
    if (file == OTC_FILE_TABLE) {
        *len = 1;
        return dash;
    }
    *len = c->src[file].label_len;
    return c->src[file].label;
}

static int cc_key_cmp(const otc_comp_t *c, const otc_key_t *a, const otc_key_t *b)
{
    if (a->file != b->file) {
        uint32_t la, lb, i, m;
        const uint8_t *pa = cc_label(c, a->file, &la), *pb = cc_label(c, b->file, &lb);
        m = la < lb ? la : lb;
        for (i = 0; i < m; i++)
            if (pa[i] != pb[i])
                return pa[i] < pb[i] ? -1 : 1;
        if (la != lb)
            return la < lb ? -1 : 1;
    }
    if (a->line != b->line)
        return a->line < b->line ? -1 : 1;
    if (a->seq != b->seq) {
#if defined(OTC_SABOTAGE_CHECK) && OTC_SABOTAGE_CHECK == 3           /* must-fail build: the later diagnostic first */
        return a->seq < b->seq ? 1 : -1;
#else
        return a->seq < b->seq ? -1 : 1;
#endif
    }
    return 0;
}

static void cc_key_set(otc_key_t *d, const otc_key_t *s)
{
    d->file = s->file;
    d->line = s->line;
    d->seq = s->seq;
}

static void cc_heap_add(otc_comp_t *c, const otc_key_t *k)
{
    uint32_t i, ch;
    if (c->hn < OTC_MAX_DIAGS) {
        i = c->hn++;
        while (i) {                                         /* sift up */
            uint32_t p = (i - 1u) / 2u;
            if (cc_key_cmp(c, &c->hk[p], k) >= 0)
                break;
            cc_key_set(&c->hk[i], &c->hk[p]);
            i = p;
        }
        cc_key_set(&c->hk[i], k);
        return;
    }
    if (cc_key_cmp(c, k, &c->hk[0]) >= 0)
        return;
    i = 0;                                                  /* replace the root: sift down */
    for (;;) {
        ch = 2u * i + 1u;
        if (ch >= c->hn)
            break;
        if (ch + 1u < c->hn && cc_key_cmp(c, &c->hk[ch + 1u], &c->hk[ch]) > 0)
            ch++;
        if (cc_key_cmp(c, &c->hk[ch], k) <= 0)
            break;
        cc_key_set(&c->hk[i], &c->hk[ch]);
        i = ch;
    }
    cc_key_set(&c->hk[i], k);
}

static void cc_drop_msg(otc_comp_t *c, uint32_t msg)
{
    uint32_t n;
    if (!msg)
        return;
    n = *PTR(c, uint32_t, msg);
    if (msg + ((4u + n + 3u) & ~3u) == c->top)
        c->top = msg;
}

static void cc_diag2(otc_comp_t *c, uint32_t file, uint32_t line, uint32_t func, uint32_t level, uint32_t hidden,
                     uint32_t msg, uint32_t lkind, uint32_t laux)
{
    uint32_t off, vis = !hidden;
    otc_key_t k;
    otc_diag_t *d;
    if (c->err)
        return;
    if (!msg && !lkind)
        return;
    c->seq++;
    if (level == OTC_LV_WARNING)
        c->nwarn++;
    if (vis)
        c->nvis++;
    k.file = file;
    k.line = line;
    k.seq = c->seq;
    if (c->dmode == DM_COUNT) {
        if (vis)
            cc_heap_add(c, &k);
        cc_drop_msg(c, msg);
        return;
    }
    if (c->dmode == DM_SELECT && (!vis || cc_key_cmp(c, &k, &c->thr) > 0)) {
        cc_drop_msg(c, msg);
        return;
    }
    if (c->dmode == DM_KEEP && vis && c->nvis > c->dcap) {
        cc_drop_msg(c, msg);
        cc_fail(c, OTC_CE_ABORT);
        return;
    }
    off = cc_alloc(c, (uint32_t)sizeof(otc_diag_t));
    if (!off)
        return;
    d = PTR(c, otc_diag_t, off);
    d->msg = msg;
    d->line = line;
    d->func = func;
    d->seq = c->seq;
    d->laux = laux;
    d->file = (uint16_t)file;
    d->level = (uint8_t)level;
    d->hidden = (uint8_t)hidden;
    d->lkind = (uint8_t)lkind;
    if (c->diag_last)
        PTR(c, otc_diag_t, c->diag_last)->next = off;
    else
        c->diag_first = off;
    c->diag_last = off;
    c->ndiags++;
}

static void cc_diag(otc_comp_t *c, uint32_t file, uint32_t line, uint32_t func, uint32_t level, uint32_t hidden,
                    uint32_t msg)
{
    cc_diag2(c, file, line, func, level, hidden, msg, OTC_LK_NONE, 0);
}

static void cc_file_err(otc_comp_t *c, uint32_t file, uint32_t line, uint32_t msg)
{
    c->file_errors++;
    cc_diag(c, file, line, 0, OTC_LV_ERROR, 0, msg);
}

static void cc_func_err(otc_comp_t *c, uint32_t fn, uint32_t line, uint32_t msg, uint32_t hidden)
{
    otc_func_t *f = PTR(c, otc_func_t, fn);
    f->nerr++;
    cc_diag(c, f->file, line, fn, OTC_LV_ERROR, hidden, msg);
}

static void cc_func_err_lazy(otc_comp_t *c, uint32_t fn, uint32_t line, uint32_t lkind, uint32_t laux)
{
    otc_func_t *f = PTR(c, otc_func_t, fn);
    f->nerr++;
    cc_diag2(c, f->file, line, fn, OTC_LV_ERROR, 0, 0, lkind, laux);
}

static void cc_func_warn(otc_comp_t *c, uint32_t fn, uint32_t line, uint32_t msg)
{
    cc_diag(c, PTR(c, otc_func_t, fn)->file, line, fn, OTC_LV_WARNING, 0, msg);
}

/* ---- names ----------------------------------------------------------------------------------------- */
static uint32_t cc_hash(const uint8_t *p, uint32_t n, int up)
{
    uint32_t h = 2166136261u;
    while (n--) {
        h ^= up ? cc_up(*p++) : *p++;
        h *= 16777619u;
    }
    return h;
}

/* a stored string (already upper case) against a probe (made upper case when `up`) */
static int cc_ref_eq(const otc_comp_t *c, uint32_t ref, const uint8_t *p, uint32_t n, int up)
{
    uint32_t m, i;
    const uint8_t *s = cc_sp(c, ref, &m);
    if (m != n)
        return 0;
    for (i = 0; i < n; i++)
        if (s[i] != (up ? cc_up(p[i]) : p[i]))
            return 0;
    return 1;
}

static uint32_t cc_fmap_find(const otc_comp_t *c, const uint8_t *p, uint32_t n, int up)
{
    uint32_t f = c->fbucket[cc_hash(p, n, up) & (OTC_HBUCKETS - 1u)];
    uint32_t h = cc_hash(p, n, up);
    while (f) {
        const otc_func_t *fn = CPTR(c, otc_func_t, f);
        if (fn->hash == h && cc_ref_eq(c, fn->name, p, n, up))
            return f;
        f = fn->hnext;
    }
    return 0;
}

static uint32_t cc_cmap_find(const otc_comp_t *c, const uint8_t *p, uint32_t n, int up)
{
    uint32_t k = c->cbucket[cc_hash(p, n, up) & (OTC_HBUCKETS - 1u)];
    uint32_t h = cc_hash(p, n, up);
    while (k) {
        const otc_const_t *cn = CPTR(c, otc_const_t, k);
        if (cn->hash == h && cc_ref_eq(c, cn->name, p, n, up))
            return k;
        k = cn->hnext;
    }
    return 0;
}

/* name of the registry (upper case) of record `i` of otc_opname against the probe: <0, 0, >0 */
static int cc_opcmp(const uint8_t *p, uint32_t n, int up, const char *name)
{
    uint32_t i = 0;
    for (;;) {
        uint8_t a, b = (uint8_t)name[i];
        if (i == n)
            return b ? -1 : 0;
        a = up ? cc_up(p[i]) : p[i];
        if (!b)
            return 1;
        if (a != b)
            return a < b ? -1 : 1;
        i++;
    }
}

/* index in otc_reg of the op (or alias) called so, or -1 */
static int cc_op_find(const uint8_t *p, uint32_t n, int up)
{
    int lo = 0, hi = OTC_NNAMES - 1;
    if (n > 16u)
        return -1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2, r = cc_opcmp(p, n, up, &otc_rstr[otc_opname[mid].off]);
        if (r == 0)
            return otc_opname[mid].op;
        if (r < 0)
            hi = mid - 1;
        else
            lo = mid + 1;
    }
    return -1;
}

/* is the text one of the NUL separated strings (ended by an empty one) at otc_rstr[set]? */
static int cc_in_set(uint32_t set, const uint8_t *p, uint32_t n, int up)
{
    const char *s = &otc_rstr[set];
    while (*s) {
        if (cc_opcmp(p, n, up, s) == 0)
            return 1;
        s += cc_cstrlen(s) + 1u;
    }
    return 0;
}

static int cc_ci_eq(const uint8_t *p, uint32_t n, const char *upper)    /* p (any case) == upper literal */
{
    return cc_opcmp(p, n, 1, upper) == 0;
}

/* NAME_RE.match: [A-Z][A-Z0-9_]* on the text (made upper case when `up`) */
static int cc_is_name(const uint8_t *p, uint32_t n, int up)
{
    uint32_t i;
    if (!n)
        return 0;
    for (i = 0; i < n; i++) {
        uint8_t b = up ? cc_up(p[i]) : p[i];
        if (!((b >= 'A' && b <= 'Z') || (i && (cc_dig(b) || b == '_'))))
            return 0;
    }
    return 1;
}

/* name_problem(): 0 = none, else the reason; cc_np_text appends the message */
enum { NP_NONE = 0, NP_QUOTED, NP_DOT, NP_BAD, NP_LONG, NP_KEYWORD, NP_VAR, NP_OP, NP_COND };

int cc_name_problem(const uint8_t *p, uint32_t n, int up, int is_word)
{
    uint32_t i;
    if (!is_word && !cc_is_name(p, n, up))
        return NP_QUOTED;
    for (i = 0; i < n; i++)
        if (p[i] == '.')
            return NP_DOT;
    if (!cc_is_name(p, n, up))
        return NP_BAD;
    if (n > OTC_L_NAME_MAX)
        return NP_LONG;
    if (cc_in_set(OTC_SET_KEYWORDS, p, n, up))
        return NP_KEYWORD;
    if (cc_in_set(OTC_SET_VARS, p, n, up))
        return NP_VAR;
    if (cc_op_find(p, n, up) >= 0)
        return NP_OP;
    if (cc_in_set(OTC_SET_COND_WORDS, p, n, up))
        return NP_COND;
    return NP_NONE;
}

void cc_np_text(otc_comp_t *c, int np, const uint8_t *p, uint32_t n, int up)
{
    switch (np) {
    case NP_QUOTED:
        mb_byte(c, '\'');
        mb_show(c, p, n, up);
        mb_lit(c, "' is not a valid name");
        break;
    case NP_DOT:
        mb_lit(c, "user names have no dot (dots belong to the API)");
        break;
    case NP_BAD:
        mb_byte(c, '\'');
        mb_show(c, p, n, up);
        mb_lit(c, "' is not a valid name (a letter, then letters, digits or _)");
        break;
    case NP_LONG:
        mb_lit(c, "name is too long (");
        mb_dec(c, (int32_t)n);
        mb_lit(c, " > ");
        mb_dec(c, OTC_L_NAME_MAX);
        mb_lit(c, " characters)");
        break;
    case NP_KEYWORD:
        mb_lit(c, "reserved name (language keyword)");
        break;
    case NP_VAR:
        mb_lit(c, "reserved name (variable)");
        break;
    case NP_OP:
        mb_lit(c, "reserved name (operation of the API)");
        break;
    default:
        mb_lit(c, "reserved name (trig condition)");
        break;
    }
}

/* ---- the lexer: tools/otype.py lex_line / lex_tokens, on bytes, token by token ---------------------- */
enum { T_NONE = 0, T_SYM, T_NUM, T_COND, T_WORD, T_STR, T_LIST };
enum { CS_WORD = 0, CS_PROB = 1, CS_AB = 2 };         /* a COND token: named word / n% / a:b (val = a * 16 + b) */

typedef struct {
    uint8_t kind, sub;
    uint32_t a, b;                /* [a, b) in the body: the text of the token (a string: its content, without quotes) */
    int32_t val;                  /* NUM: the value; COND: n or a * 16 + b */
} tok_t;

typedef struct {
    const uint8_t *s;
    uint32_t n, i;
} lx_t;

static void tk_set(tok_t *d, const tok_t *s)         /* (a struct copy would call memcpy, which the unit does not have) */
{
    d->kind = s->kind;
    d->sub = s->sub;
    d->a = s->a;
    d->b = s->b;
    d->val = s->val;
}

enum {
    E_LINELONG = 1, E_TAB, E_BADCHAR, E_NUMRANGE, E_NOTPROB, E_NOTAB, E_COND11, E_UNTERM_STR, E_STR_LONG, E_AFTER_STR,
    E_UNTERM_LIST, E_LIST_NONNUM, E_LIST_EMPTY, E_LIST_LONG, E_RBRACE
};

typedef struct {
    uint8_t kind;
    uint32_t n1, a, b;            /* a number; a range of the body */
} lexerr_t;

static uint32_t cc_dvalue(const uint8_t *p, uint32_t n)       /* digits_value, saturated at 1000 (nothing needs more) */
{
    uint32_t v = 0, i;
    for (i = 0; i < n; i++) {
        v = v * 10u + (uint32_t)(p[i] - '0');
        if (v > 1000u)
            v = 1000u;
    }
    return v;
}

/* parse_int() of an upper-case NUM word: 0 = ok (value in *out), 1 = out of the 16-bit range */
static int cc_parse_int(const uint8_t *w, uint32_t n, int32_t *out)
{
    uint32_t i = 0, mag = 0;
    int neg = 0;
    if (w[0] == '-') {
        neg = 1;
        i = 1;
    }
    if (n - i >= 3u && w[i] == '0' && cc_up(w[i + 1]) == 'X') {
        /* hex: more than 8 significant digits are out of range whatever they are */
        uint32_t j, sig = 0;
        for (j = i + 2; j < n; j++) {
            uint8_t b = cc_up(w[j]);
            uint32_t d = b <= '9' ? (uint32_t)(b - '0') : (uint32_t)(b - 'A' + 10);
            if (!sig && !d)
                continue;
            sig++;
            if (sig > 8u)
                return 1;
            mag = (mag << 4) | d;
        }
    } else {
        uint32_t sig = 0;
        for (; i < n; i++) {
            if (!sig && w[i] == '0')
                continue;
            sig++;
            if (sig > 9u)
                return 1;
            mag = mag * 10u + (uint32_t)(w[i] - '0');
        }
    }
    if (neg ? mag > 32768u : mag > 32767u)
        return 1;
    *out = neg ? -(int32_t)mag : (int32_t)mag;
    return 0;
}

/* NUM_RE.match(upper(w)): -?(0X[0-9A-F]+|[0-9]+) */
static int cc_is_num(const uint8_t *w, uint32_t n)
{
    uint32_t i = 0, j;
    if (n && w[0] == '-')
        i = 1;
    if (n - i >= 3u && w[i] == '0' && cc_up(w[i + 1]) == 'X') {
        for (j = i + 2; j < n; j++) {
            uint8_t b = cc_up(w[j]);
            if (!(cc_dig(b) || (b >= 'A' && b <= 'F')))
                break;
        }
        if (j == n)
            return 1;
    }
    if (n - i < 1u)
        return 0;
    for (j = i; j < n; j++)
        if (!cc_dig(w[j]))
            return 0;
    return 1;
}

/* classify_word(): 0 = a token in *t, -1 = an error in *e */
static int cc_classify(const uint8_t *body, uint32_t a, uint32_t b, tok_t *t, lexerr_t *e)
{
    const uint8_t *w = body + a;
    uint32_t n = b - a, k;
    int32_t v;
    t->a = a;
    t->b = b;
    t->sub = 0;
    t->val = 0;
    if (n == 2 && w[0] == '>' && w[1] == '>') {
        t->kind = T_SYM;
        return 0;
    }
    if (cc_is_num(w, n)) {
        if (cc_parse_int(w, n, &v)) {
            e->kind = E_NUMRANGE;
            e->a = a;
            e->b = b;
            return -1;
        }
        t->kind = T_NUM;
        t->val = v;
        return 0;
    }
    /* [0-9]+% */
    if (n >= 2 && w[n - 1] == '%') {
        for (k = 0; k + 1 < n && cc_dig(w[k]); k++)
            ;
        if (k + 1 == n) {
            uint32_t pv = cc_dvalue(w, n - 1), i;
            for (i = 0; i < OTC_PROBS_N; i++)
                if (otc_probs[i] == pv)
                    break;
            if (i == OTC_PROBS_N) {
                e->kind = E_NOTPROB;
                e->a = a;
                e->b = b;
                return -1;
            }
            t->kind = T_COND;
            t->sub = CS_PROB;
            t->val = (int32_t)pv;
            return 0;
        }
    }
    /* [0-9]+:[0-9]+ */
    for (k = 0; k < n && cc_dig(w[k]); k++)
        ;
    if (k > 0 && k < n && w[k] == ':' && k + 1 < n) {
        uint32_t m;
        for (m = k + 1; m < n && cc_dig(w[m]); m++)
            ;
        if (m == n) {
            uint32_t x = cc_dvalue(w, k), y = cc_dvalue(w + k + 1, n - k - 1);
            if (!(x >= 1u && x <= y && y <= 8u)) {
                e->kind = E_NOTAB;
                e->a = a;
                e->b = b;
                return -1;
            }
            if (x == 1u && y == 1u) {
                e->kind = E_COND11;
                return -1;
            }
            t->kind = T_COND;
            t->sub = CS_AB;
            t->val = (int32_t)(x * 16u + y);
            return 0;
        }
    }
    if (cc_in_set(OTC_SET_COND_WORDS, w, n, 1)) {
        t->kind = T_COND;
        t->sub = CS_WORD;
        return 0;
    }
    t->kind = T_WORD;
    return 0;
}

/* The values of the list token [a, b) (braces included): at most OTC_L_MAX_LIST, already checked */
static uint32_t cc_list_vals(const uint8_t *body, uint32_t a, uint32_t b, int32_t *out)
{
    uint32_t i = a + 1, cnt = 0;
    while (i < b - 1u) {
        uint32_t j;
        while (i < b - 1u && cc_ws(body[i]))
            i++;
        if (i >= b - 1u)
            break;
        j = i;
        while (j < b - 1u && !cc_ws(body[j]))
            j++;
        if (cnt < OTC_L_MAX_LIST && cc_parse_int(body + i, j - i, &out[cnt]) == 0)
            cnt++;
        i = j;
    }
    return cnt;
}

/* next token: 1 = a token in *t, 0 = the end of the line, -1 = a lexical error in *e */
static int lex_next(lx_t *lx, tok_t *t, lexerr_t *e)
{
    const uint8_t *s = lx->s;
    uint32_t n = lx->n, i = lx->i, j;
    uint8_t ch;
    while (i < n && s[i] == ' ')
        i++;
    if (i >= n) {
        lx->i = i;
        return 0;
    }
    ch = s[i];
    t->sub = 0;
    t->val = 0;
    if (ch == '"') {
        for (j = i + 1; j < n && s[j] != '"'; j++)
            ;
        if (j >= n) {
            e->kind = E_UNTERM_STR;
            return -1;
        }
        if (j - i - 1u > OTC_L_MAX_STR) {
            e->kind = E_STR_LONG;
            e->n1 = j - i - 1u;
            return -1;
        }
        t->kind = T_STR;
        t->a = i + 1;
        t->b = j;
        i = j + 1;
        if (i < n && s[i] != ' ' && s[i] != ':' && s[i] != '|') {
            e->kind = E_AFTER_STR;
            return -1;
        }
        lx->i = i;
        return 1;
    }
    if (ch == '{') {
        uint32_t k, cnt = 0;
        for (j = i; j < n && s[j] != '}'; j++)
            ;
        if (j >= n) {
            e->kind = E_UNTERM_LIST;
            return -1;
        }
        k = i + 1;
        while (k < j) {
            uint32_t m;
            int32_t v;
            while (k < j && cc_ws(s[k]))
                k++;
            if (k >= j)
                break;
            m = k;
            while (m < j && !cc_ws(s[m]))
                m++;
            if (!cc_is_num(s + k, m - k)) {
                e->kind = E_LIST_NONNUM;
                e->a = k;
                e->b = m;
                return -1;
            }
            if (cc_parse_int(s + k, m - k, &v)) {
                e->kind = E_NUMRANGE;
                e->a = k;
                e->b = m;
                return -1;
            }
            cnt++;
            k = m;
        }
        if (!cnt) {
            e->kind = E_LIST_EMPTY;
            return -1;
        }
        if (cnt > OTC_L_MAX_LIST) {
            e->kind = E_LIST_LONG;
            e->n1 = cnt;
            return -1;
        }
        t->kind = T_LIST;
        t->a = i;
        t->b = j + 1;
        lx->i = j + 1;
        return 1;
    }
    if (ch == '}') {
        e->kind = E_RBRACE;
        return -1;
    }
    if (ch == ':' || ch == '|') {
        t->kind = T_SYM;
        t->a = i;
        t->b = i + 1;
        lx->i = i + 1;
        return 1;
    }
    j = i;
    while (j < n) {
        uint8_t x = s[j];
        if (x == ' ' || x == '"' || x == '{' || x == '}' || x == '|')
            break;
        if (x == ':') {
            uint32_t m;
            for (m = i; m < j && cc_dig(s[m]); m++)
                ;
            if (j > i && m == j && j + 1 < n && cc_dig(s[j + 1])) {      /* ':' glued between two digits (1:4) */
                j++;
                continue;
            }
            break;
        }
        j++;
    }
    if (cc_classify(s, i, j, t, e) < 0)
        return -1;
    lx->i = j;
    return 1;
}

/* ---- lex_line ---------------------------------------------------------------------------------------- */
typedef struct {
    uint8_t blank, opens, ok;     /* ok: the line lexed (Python: toks is not None) */
    uint32_t indent;
    const uint8_t *body;          /* the code of the line, indentation and trailing white space removed */
    uint32_t blen;
    uint32_t first_len;           /* the first piece of the body (up to a space or tab) */
    uint32_t nerr;
    lexerr_t err[3];
} li_t;

static void cc_lex_line(li_t *li, const uint8_t *raw, uint32_t rn)
{
    uint32_t i, end = rn, lead, k;
    int in_str = 0;
    lx_t lx;
    tok_t tk;
    cc_zero(li, (uint32_t)sizeof *li);
    li->body = raw;
#ifdef OTC_SABOTAGE_FRONT                                /* must-fail build (tools/test_otype_ccomp.py): off by one */
    if (rn >= OTC_L_MAX_LINE) {
#else
    if (rn > OTC_L_MAX_LINE) {
#endif
        li->err[li->nerr].kind = E_LINELONG;
        li->err[li->nerr++].n1 = rn;
    }
    for (i = 0; i < rn; i++) {                        /* split_comment: '#' outside strings */
        if (in_str) {
            if (raw[i] == '"')
                in_str = 0;
        } else if (raw[i] == '"') {
            in_str = 1;
        } else if (raw[i] == '#') {
            end = i;
            break;
        }
    }
    while (end > 0 && cc_ws(raw[end - 1]))
        end--;
    for (lead = 0; lead < end && cc_ws(raw[lead]); lead++)
        ;
    if (lead >= end) {
        li->blank = 1;
        return;
    }
    for (k = 0; k < lead; k++)
        if (raw[k] == '\t') {
            li->err[li->nerr++].kind = E_TAB;
            break;
        }
    li->indent = lead;
    li->body = raw + lead;
    li->blen = end - lead;
    li->opens = li->body[li->blen - 1] == ':';
    for (k = 0; k < li->blen && !cc_ws(li->body[k]); k++)
        ;
    li->first_len = k;
    for (k = 0; k < li->blen; k++)
        if (li->body[k] < 0x20 || li->body[k] > 0x7E) {
            li->err[li->nerr].kind = E_BADCHAR;
            li->err[li->nerr++].n1 = li->body[k];
            return;
        }
    lx.s = li->body;
    lx.n = li->blen;
    lx.i = 0;
    for (;;) {
        int r = lex_next(&lx, &tk, &li->err[li->nerr]);
        if (r < 0) {
            li->nerr++;
            return;
        }
        if (!r)
            break;
    }
    li->ok = 1;
}

/* the message of a lexical error of a line (body = the text it is about) */
static uint32_t cc_lexerr_msg(otc_comp_t *c, const lexerr_t *e, const uint8_t *body)
{
    uint32_t k;
    mb_begin(c);
    switch (e->kind) {
    case E_LINELONG:
        mb_lit(c, "line too long (");
        mb_dec(c, (int32_t)e->n1);
        mb_lit(c, " > ");
        mb_dec(c, OTC_L_MAX_LINE);
        mb_lit(c, " characters)");
        break;
    case E_TAB:
        mb_lit(c, "tab in indentation (use spaces)");
        break;
    case E_BADCHAR:
        mb_lit(c, "character 0x");
        mb_hex2(c, e->n1);
        mb_lit(c, " outside a comment (printable ASCII only)");
        break;
    case E_NUMRANGE:
        mb_lit(c, "number ");           /* with its sign, as written */
        mb_show(c, body + e->a, e->b - e->a, 1);
        mb_lit(c, " out of 16-bit range (-32768..32767)");
        break;
    case E_NOTPROB:
        mb_show(c, body + e->a, e->b - e->a, 1);
        mb_lit(c, " is not a trig condition (allowed:");
        for (k = 0; k < OTC_PROBS_N; k++) {
            mb_byte(c, ' ');
            mb_dec(c, otc_probs[k]);
            mb_byte(c, '%');
        }
        mb_byte(c, ')');
        break;
    case E_NOTAB:
        mb_show(c, body + e->a, e->b - e->a, 1);
        mb_lit(c, " is not a trig condition (a:b needs 1 <= a <= b <= 8)");
        break;
    case E_COND11:
        mb_lit(c, "1:1 is not a trig condition (the octatrack has no always condition: use cond.set s off for none)");
        break;
    case E_UNTERM_STR:
        mb_lit(c, "unterminated string");
        break;
    case E_STR_LONG:
        mb_lit(c, "string too long (");
        mb_dec(c, (int32_t)e->n1);
        mb_lit(c, " > ");
        mb_dec(c, OTC_L_MAX_STR);
        mb_lit(c, " characters)");
        break;
    case E_AFTER_STR:
        mb_lit(c, "unexpected text right after a string");
        break;
    case E_UNTERM_LIST:
        mb_lit(c, "unterminated list (missing })");
        break;
    case E_LIST_NONNUM:
        mb_lit(c, "a list holds numbers only (found '");
        mb_show(c, body + e->a, e->b - e->a, 0);
        mb_lit(c, "')");
        break;
    case E_LIST_EMPTY:
        mb_lit(c, "empty list");
        break;
    case E_LIST_LONG:
        mb_lit(c, "list too long (");
        mb_dec(c, (int32_t)e->n1);
        mb_lit(c, " > ");
        mb_dec(c, OTC_L_MAX_LIST);
        mb_lit(c, " elements)");
        break;
    default:
        mb_lit(c, "unexpected }");
        break;
    }
    return mb_end(c);
}

/* ---- lines of a text: re.split(r"\r\n|\r|\n") ---------------------------------------------------------- */
typedef struct {
    const uint8_t *text;
    uint32_t len, pos, no;        /* next line starts at pos; `no` = its number - 1 */
    uint32_t start, end;          /* the line returned: [start, end) */
    uint8_t done;
} lines_t;

static void cc_lines_init(lines_t *ls, const uint8_t *text, uint32_t len, uint32_t pos, uint32_t first_no)
{
    ls->text = text;
    ls->len = len;
    ls->pos = pos;
    ls->no = first_no - 1u;
    ls->done = 0;
    ls->start = ls->end = pos;
}

/* the next line (its number is ls->no); 0 at the end. The empty piece after a last terminator is a line too. */
static int cc_lines_next(lines_t *ls)
{
    uint32_t i;
    if (ls->done)
        return 0;
    ls->start = ls->pos;
    for (i = ls->pos; i < ls->len && ls->text[i] != '\n' && ls->text[i] != '\r'; i++)
        ;
    ls->end = i;
    ls->no++;
    if (i >= ls->len) {
        ls->done = 1;
        ls->pos = ls->len;
    } else {
        if (ls->text[i] == '\r' && i + 1u < ls->len && ls->text[i + 1] == '\n')
            i++;
        ls->pos = i + 1u;
    }
    return 1;
}
