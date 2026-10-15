/* OType compiler, REPORT: the text of /SCRIPTS/ERRORS.TXT. Included by otc_comp.c.
 * Mirrors tools/otype.py report() with the cap on the number of lines: the kept diagnostics sorted by (label bytes, line, order of
 * emission), then `-:0: -: error: too many errors (N more)` if there were more than OTC_MAX_DIAGS visible ones, then
 * the summary `n functions OK, e refused, w warnings[ (k file-level errors)]`.
 *
 * The messages that can be very long are NOT built in the workspace: the call paths of `loop a>b>a` and of
 * `call depth N > 8 (a>b>...)` (a ring or a chain of thousands of functions would make a message per function of
 * thousands of names) are written here, from the graph the checker left (otc_fx_t: scc, hnext, the callee lists). */

typedef struct {
    char *out;
    uint32_t cap, len;
    uint8_t inq;
} rp_t;

static void rp_byte(rp_t *r, uint8_t b)
{
    if (r->len < r->cap)
        r->out[r->len] = (char)b;
    r->len++;
}

static void rp_bytes(rp_t *r, const uint8_t *p, uint32_t n)
{
    while (n--)
        rp_byte(r, *p++);
}

static void rp_lit(rp_t *r, const char *s)
{
    while (*s)
        rp_byte(r, (uint8_t)*s++);
}

static void rp_dec(rp_t *r, uint32_t v)
{
    char tmp[12];
    uint32_t n = 0;
    do {
        tmp[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n)
        rp_byte(r, (uint8_t)tmp[--n]);
}

static void rp_show(rp_t *r, const uint8_t *p, uint32_t n)       /* show(): lower case outside double quotes */
{
    r->inq = 0;
    while (n--) {
        uint8_t b = *p++;
        if (b == '"') {
            r->inq = (uint8_t)!r->inq;
            rp_byte(r, b);
        } else {
            rp_byte(r, r->inq ? b : cc_low(b));
        }
    }
}

static void rp_show_name(rp_t *r, const otc_comp_t *c, uint32_t fn)
{
    uint32_t n;
    const uint8_t *p = cc_sp(c, FN(c, fn)->name, &n);
    rp_show(r, p, n);
}

/* `loop a>b>a`: the DFS of Program.cycle_path inside the component of f, in the order of the calls */
static void rp_loop_path(rp_t *r, otc_comp_t *c, uint32_t start_fn)
{
    uint32_t start = FN(c, start_fn)->idx, d = 0, i, gen;
    uint32_t *path = PTR(c, uint32_t, c->rpath), *eidx = PTR(c, uint32_t, c->rpath) + (c->nf + 2u);
    c->gen++;
    gen = c->gen;
    path[0] = start;
    eidx[0] = 0;
    for (;;) {
        const otc_fx_t *vx = FX(c, path[d]);
        if (eidx[d] < vx->ncg) {
            uint32_t w = PTR(c, otc_edge_t, vx->cg)[eidx[d]++].g;
            if (FX(c, w)->scc != FX(c, start)->scc)
                continue;
            if (w == start) {
                path[++d] = w;
                break;
            }
            if (FX(c, w)->stamp == gen)
                continue;
            FX(c, w)->stamp = gen;
            path[++d] = w;
            eidx[d] = 0;
        } else {
            if (!d)
                break;
            d--;
        }
    }
    for (i = 0; i <= d; i++) {
        if (i)
            rp_byte(r, '>');
        rp_show_name(r, c, FX(c, path[i])->func);
    }
}

static void rp_depth_path(rp_t *r, const otc_comp_t *c, uint32_t fn)
{
    uint32_t i = FN(c, fn)->idx, first = 1;
    for (;;) {
        if (!first)
            rp_byte(r, '>');
        first = 0;
        rp_show_name(r, c, FX(c, i)->func);
        if (!FX(c, i)->hnext)
            break;
        i = FX(c, i)->hnext - 1u;
    }
}

static void rp_diag(rp_t *r, otc_comp_t *c, const otc_diag_t *d)
{
    uint32_t n;
    const uint8_t *p = cc_label(c, d->file, &n);
    rp_bytes(r, p, n);
    rp_byte(r, ':');
    rp_dec(r, d->line);
    rp_lit(r, ": ");
    if (d->func)
        rp_show_name(r, c, d->func);
    else
        rp_byte(r, '-');
    rp_lit(r, d->level == OTC_LV_WARNING ? ": warning: " : ": error: ");
    if (d->lkind == OTC_LK_LOOP) {
        rp_lit(r, "loop ");
        rp_loop_path(r, c, d->func);
    } else if (d->lkind == OTC_LK_DEPTH) {
        rp_lit(r, "call depth ");
        rp_dec(r, d->laux);
        rp_lit(r, " > ");
        rp_dec(r, OTC_L_MAX_DEPTH);
        rp_lit(r, " (");
        rp_depth_path(r, c, d->func);
        rp_byte(r, ')');
    } else {
        p = cc_sp(c, d->msg, &n);
        rp_bytes(r, p, n);
    }
    rp_byte(r, '\n');
}

static int rp_key_of(const otc_comp_t *c, uint32_t doff, otc_key_t *k)
{
    const otc_diag_t *d = CPTR(c, otc_diag_t, doff);
    (void)c;
    k->file = d->file;
    k->line = d->line;
    k->seq = d->seq;
    return 0;
}

/* the visible kept diagnostics, sorted (insertion sort: at most OTC_MAX_DIAGS of them) */
CC_NOINLINE static int rp_prepare(otc_comp_t *c)
{
    uint32_t d, n = 0, i, j;
    uint32_t *a;
    if (c->rsort || !c->checked)
        return c->rsort != 0;
    for (d = c->diag_first; d; d = CPTR(c, otc_diag_t, d)->next)
        if (!CPTR(c, otc_diag_t, d)->hidden)
            n++;
    c->nkept = n;
    c->rsort = cc_alloc(c, 4u * (n ? n : 1u));
    if (!c->rsort)
        return 0;
    a = PTR(c, uint32_t, c->rsort);
    i = 0;
    for (d = c->diag_first; d; d = CPTR(c, otc_diag_t, d)->next) {
        otc_key_t kd;
        if (CPTR(c, otc_diag_t, d)->hidden)
            continue;
        rp_key_of(c, d, &kd);
        for (j = i; j > 0; j--) {
            otc_key_t kp;
            rp_key_of(c, a[j - 1u], &kp);
            if (cc_key_cmp(c, &kp, &kd) <= 0)
                break;
            a[j] = a[j - 1u];
        }
        a[j] = d;
        i++;
    }
    c->rpath = cc_alloc(c, 8u * (c->nf + 2u));
    return c->rpath != 0 || c->nf == 0;
}

uint32_t otc_report(void *ws, char *out, uint32_t cap)
{
    otc_comp_t *c = (otc_comp_t *)ws;
    rp_t r;
    uint32_t i, ok = 0, kept;
    r.out = out;
    r.cap = cap;
    r.len = 0;
    r.inq = 0;
    if (c->err || !c->checked || !rp_prepare(c))
        return 0;
    kept = c->nkept;
    for (i = 0; i < kept && i < OTC_MAX_DIAGS; i++)
        rp_diag(&r, c, CPTR(c, otc_diag_t, PTR(c, uint32_t, c->rsort)[i]));
    if (c->nvis > OTC_MAX_DIAGS) {                               /* the cap on the report */
        rp_lit(&r, "-:0: -: error: too many errors (");
        rp_dec(&r, c->nvis - OTC_MAX_DIAGS);
        rp_lit(&r, " more)\n");
    }
    for (i = 0; i < c->nfuncs; i++)
        if (!FN(c, FX(c, i)->func)->nerr)
            ok++;
    rp_dec(&r, ok);
    rp_lit(&r, " functions OK, ");
    rp_dec(&r, c->nfuncs - ok);
    rp_lit(&r, " refused, ");
    rp_dec(&r, c->nwarn);
    rp_lit(&r, " warnings");
    if (c->file_errors) {
        rp_lit(&r, " (");
        rp_dec(&r, c->file_errors);
        rp_lit(&r, " file-level errors)");
    }
    rp_byte(&r, '\n');
    if (r.len < r.cap)
        r.out[r.len] = 0;
    return r.len;
}
