/* OType /SCRIPTS loader of the unit (the first load and the reload of the scripts).
 *
 * ONE call, `ots_load(ctx)`, made by firmware/src/script.S from the UI task, with the integrator's interface already closed:
 *   1. list /SCRIPTS (the card ROOT): the regular files whose name ends in .OTY (any case), never a name that
 *      starts with '.' (`._*` sidecars, hidden files), never a folder; at most OTS_MAXNAMES names, sorted like
 *      sim._sort_key (ASCII-folded bytes, then the raw bytes);
 *   2. read them, in that order, into one buffer (64 files / 65536 bytes are loaded, the compiler gives the others
 *      their `file not loaded` line, so a file past a cap is only SIZED, never read);
 *   3. otc_compile(... OTC_PROFILE_M1 ...) on a private stack (the asm import does the switch);
 *   4. the report cut at the buffer size (`... report truncated (N bytes)` as the last line);
 *   5. the action records of the script table, one per `menu` line of the table, in table order.
 * The card, the stack switch and the file read are the asm imports below (script.S); everything else is plain C and
 * touches only the buffers of the context. The layout of `ots_ctx_t` is shared with script.S (static asserts below).
 *
 * Same unit policy as build_c.py: freestanding, no libc (no memcpy: the loops are marked OTS_NOIDIOM), no writable
 * statics, imports only the four `otb_host_*` names of build_script.py. Not built for the Mac.
 */
#include <stddef.h>
#include <stdint.h>

#include "otc_comp.h"

#if defined(__GNUC__) && !defined(__clang__)
#define OTS_NOIDIOM __attribute__((optimize("no-tree-loop-distribute-patterns")))
#else
#define OTS_NOIDIOM
#endif

#define OTS_MAXNAMES 128         /* OT_NAMES of layout.py */
#define OTS_LABEL    272         /* OT_LABEL of layout.py */
#define OTS_MAXACT   2048        /* 32 categories x 64 scripts each */
#define OTS_SHORT    10          /* width of an action name (ACT_W of build/units.py) */
#define OTS_LONG     23          /* the YES/NO title: 24 bytes with the NUL */
#define OTS_SLEN     12          /* bytes per short name in the buffer */
#define OTS_LLEN     24
#define OTS_MSGLEN   48
#define OTS_NAMEMAX  259         /* card.S OTK_NAMEMAX */

enum { OTS_ST_TABLE = 0, OTS_ST_NOFILES = 1, OTS_ST_NOTABLE = 2, OTS_ST_CERR = 3, OTS_ST_BUSY = 4 };

typedef struct {
    uint8_t *src_buf;            /*  0 the sources, back to back */
    uint32_t src_cap;            /*  4 65536 */
    uint8_t *arena;              /*  8 the compiler's workspace */
    uint32_t arena_size;         /* 12 */
    uint32_t stack_top;          /* 16 the private stack: the top of the arena region (8-aligned) */
    uint8_t *stage;              /* 20 the table, OTC_OTB_MAX bytes */
    char *report;                /* 24 */
    uint32_t report_cap;         /* 28 the buffer holds 256 bytes more (the cut line) */
    otc_src_t *srcv;             /* 32 OTS_MAXNAMES entries */
    char *labels;                /* 36 OTS_MAXNAMES x OTS_LABEL */
    uint8_t *acts;               /* 40 the script table's action record 1 (16 bytes each; record 0 is LOAD) */
    char *shorts;                /* 44 OTS_MAXACT x OTS_SLEN */
    char *longs;                 /* 48 OTS_MAXACT x OTS_LLEN */
    uint8_t *slotfn;             /* 52 OTS_MAXACT bytes: function index of each script action */
    uint32_t entry;              /* 56 otype_ot_menu (the integrator's menu entry) */
    char *msg;                   /* 60 OTS_MSGLEN bytes */
    char *name;                  /* 64 scratch for a name: 264 bytes */
    uint8_t *order;              /* 68 OTS_MAXNAMES bytes */
    uint32_t status;             /* 72 out */
    uint32_t nfiles;             /* 76 files given to the compiler */
    uint32_t nlisted;            /* 80 names found (> OTS_MAXNAMES: the rest is only counted in nskipped) */
    uint32_t otb_len;            /* 84 the table, 0 = none */
    uint32_t report_len;         /* 88 bytes of the report to write (after the cut) */
    uint32_t full_len;           /* 92 the compiler's report length, before the cut */
    uint32_t nacts;              /* 96 script actions made */
    uint32_t nfuncs;             /* 100 functions in the table */
    uint32_t nrefused;           /* 104 functions refused */
    uint32_t cerr;               /* 108 OTC_CE_* of otc_compile */
    uint32_t ncut;               /* 112 menu lines past OTS_MAXACT */
    uint32_t nskipped;           /* 116 names not taken: too long, or past OTS_MAXNAMES */
    uint32_t nmenus;             /* 120 menu lines in the table */
} ots_ctx_t;

_Static_assert(offsetof(ots_ctx_t, entry) == 56, "ctx layout");
_Static_assert(offsetof(ots_ctx_t, order) == 68, "ctx layout");
_Static_assert(offsetof(ots_ctx_t, status) == 72, "ctx layout");
_Static_assert(offsetof(ots_ctx_t, report_len) == 88, "ctx layout");
_Static_assert(offsetof(ots_ctx_t, nacts) == 96, "ctx layout");
_Static_assert(sizeof(ots_ctx_t) == 124, "ctx layout");

/* ---- the imports (script.S) --------------------------------------------------------------------------------- */
extern int32_t otb_host_ls_open(void);                /* 0 ok, -1 no /SCRIPTS folder, -2 card busy */
extern int32_t otb_host_ls_next(char *out);           /* 1 file, 2 folder, 3 name too long, 0 end, -2 card busy */
extern int32_t otb_host_file_read(const char *path, uint8_t *buf, uint32_t max);   /* size (not read if > max), -1 absent, -3 failed */
extern int32_t otb_host_compile(const uint32_t *argv);   /* otc_compile(argv[0..10]) on the stack argv[11] */

/* ---- tiny helpers -------------------------------------------------------------------------------------------- */
OTS_NOIDIOM static uint32_t ots_len(const char *s)
{
    uint32_t n = 0;
    while (s[n])
        n++;
    return n;
}

OTS_NOIDIOM static void ots_cat(char *d, uint32_t *at, uint32_t cap, const char *s)
{
    while (*s && *at + 1 < cap)
        d[(*at)++] = *s++;
    d[*at] = 0;
}

OTS_NOIDIOM static void ots_dec(char *d, uint32_t *at, uint32_t cap, uint32_t v)
{
    char t[12];
    uint32_t k = 0;
    do {
        t[k++] = (char)('0' + v % 10);
        v /= 10;
    } while (v && k < 11);
    while (k && *at + 1 < cap)
        d[(*at)++] = t[--k];
    d[*at] = 0;
}

static uint32_t ots_rd16(const uint8_t *p)
{
    return ((uint32_t)p[0] << 8) | p[1];
}

static void ots_wr32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint32_t ots_fold(uint32_t c)                     /* sim.fold: ASCII upper case only */
{
    return (c >= 'a' && c <= 'z') ? c - 32 : c;
}

/* sim._sort_key: the folded bytes, then the raw bytes */
static int ots_cmp(const char *a, const char *b)
{
    uint32_t i, x, y;
    for (i = 0;; i++) {
        x = ots_fold((uint8_t)a[i]);
        y = ots_fold((uint8_t)b[i]);
        if (x != y)
            return x < y ? -1 : 1;
        if (!x)
            break;
    }
    for (i = 0;; i++) {
        if ((uint8_t)a[i] != (uint8_t)b[i])
            return (uint8_t)a[i] < (uint8_t)b[i] ? -1 : 1;
        if (!a[i])
            return 0;
    }
}

static int ots_is_oty(const char *s, uint32_t n)         /* ends in ".OTY", any case */
{
    return n >= 5 && s[n - 4] == '.' && (s[n - 3] | 0x20) == 'o' && (s[n - 2] | 0x20) == 't' && (s[n - 1] | 0x20) == 'y';
}

OTS_NOIDIOM static void ots_msg(ots_ctx_t *c, const char *s)
{
    uint32_t at = 0;
    ots_cat(c->msg, &at, OTS_MSGLEN, s);
}

/* the menu lines of the table -> the action table's records */
OTS_NOIDIOM static void ots_actions(ots_ctx_t *c)
{
    const uint8_t *t = c->stage;
    uint32_t len = c->otb_len, nfunc = ots_rd16(t + 6), pool = ots_rd16(t + 10), ftab = ots_rd16(t + 14);
    uint32_t k, m, nm;
    c->nacts = c->ncut = c->nmenus = 0;
    c->nfuncs = nfunc;
    c->nrefused = ots_rd16(t + 8);
    if (ftab + nfunc * 54u > len)
        return;
    for (k = 0; k < nfunc; k++) {
        const uint8_t *e = t + ftab + k * 54u;
        uint32_t moff = ots_rd16(e + 16);
        const uint8_t *p;
        if (!moff) {
            if (ots_rd16(e + 18) & 0x40u) { /* MODULE */
                uint32_t aux_off = ots_rd16(t + 16);
                uint32_t faux = ots_rd16(e + 48);
                uint32_t nparams = e[33];
                uint32_t nargs = e[32];
                uint32_t nctr = e[38];
                uint32_t mo = ots_rd16(t + aux_off + faux + nparams * 8u + nargs * 2u + nctr * 4u);
                if (mo && pool + mo + 2 <= len) {
                    uint32_t pn = t[pool + mo + 1], q;
                    uint32_t sat = 0, lat = 0;
                    char *sh, *lg;
                    uint8_t *r;
                    c->nmenus++;
                    if (c->nacts >= OTS_MAXACT) {
                        c->ncut++;
                        continue;
                    }
                    sh = c->shorts + c->nacts * OTS_SLEN;
                    lg = c->longs + c->nacts * OTS_LLEN;
                    ots_cat(lg, &lat, OTS_LLEN, "MODULE>");
                    for (q = 0; q < pn && lat < OTS_LONG; q++)
                        lg[lat++] = (char)t[pool + mo + 2 + q];
                    lg[lat] = 0;
                    for (q = 0; q < pn && sat < OTS_SHORT; q++)
                        sh[sat++] = (char)t[pool + mo + 2 + q];
                    sh[sat] = 0;
                    r = c->acts + c->nacts * 16u;
                    ots_wr32(r, c->entry);
                    ots_wr32(r + 4, (uint32_t)(uintptr_t)sh);
                    ots_wr32(r + 8, (uint32_t)(uintptr_t)lg);
                    r[12] = 0;
                    r[13] = (uint8_t)c->nacts;
                    r[14] = (uint8_t)(c->nacts >> 8);
                    r[15] = (uint8_t)c->nacts;
                    c->slotfn[c->nacts] = (uint8_t)k;
                    c->nacts++;
                }
            }
            continue;
        }
        if (pool + moff + 2 > len)
            continue;
        p = t + pool + moff;
        if (p[0] != 4)
            continue;
        nm = p[1];
        p += 2;
        for (m = 0; m < nm; m++) {
            uint32_t ns = *p++, s;
            uint32_t sat = 0, lat = 0;
            char *sh, *lg;
            uint8_t *r;
            const uint8_t *last = p;
            c->nmenus++;
            if (c->nacts >= OTS_MAXACT) {                /* past the capacity: skipped, counted */
                c->ncut++;
                for (s = 0; s < ns; s++)
                    p += 1u + *p;
                continue;
            }
            sh = c->shorts + c->nacts * OTS_SLEN;
            lg = c->longs + c->nacts * OTS_LLEN;
            lg[0] = 0;
            for (s = 0; s < ns; s++) {
                uint32_t n = *p, q;
                last = p;
                if (s && lat < OTS_LONG)
                    lg[lat++] = '>';
                for (q = 0; q < n && lat < OTS_LONG; q++)
                    lg[lat++] = (char)p[1 + q];
                p += 1u + n;
            }
            lg[lat] = 0;
            sh[0] = 0;
            if (ns) {
                uint32_t n = *last, q;
                for (q = 0; q < n && sat < OTS_SHORT; q++)
                    sh[sat++] = (char)last[1 + q];
            }
            sh[sat] = 0;
            r = c->acts + c->nacts * 16u;
            ots_wr32(r, c->entry);
            ots_wr32(r + 4, (uint32_t)(uintptr_t)sh);
            ots_wr32(r + 8, (uint32_t)(uintptr_t)lg);
            r[12] = (e[23] & 3u) ? 1u : 0u;               /* F_DESTRUCTIVE | F_CONFIRM: the stock YES/NO */
            r[13] = (uint8_t)c->nacts;                    /* A_VALUE: low byte of the action index */
            r[14] = (uint8_t)(c->nacts >> 8);             /* the full 16-bit action index */
            r[15] = (uint8_t)c->nacts;
            c->slotfn[c->nacts] = (uint8_t)k;
            c->nacts++;
        }
    }
}

OTS_NOIDIOM uint32_t ots_load(ots_ctx_t *c)
{
    uint32_t n = 0, i, j, used = 0, stop = 0;
    int32_t r;
    char *lab;
    c->status = OTS_ST_NOFILES;
    c->nfiles = c->nlisted = c->otb_len = c->report_len = c->full_len = 0;
    c->nacts = c->nfuncs = c->nrefused = c->cerr = c->ncut = c->nskipped = c->nmenus = 0;
    c->msg[0] = 0;
    r = otb_host_ls_open();
    if (r != 0) {
        if (r == -2) {
            c->status = OTS_ST_BUSY;
            ots_msg(c, "CARD BUSY");
        } else {
            ots_msg(c, "NO /SCRIPTS");
        }
        return c->status;
    }
    for (;;) {
        int32_t t = otb_host_ls_next(c->name);
        uint32_t nl;
        if (t == -2) {
            c->status = OTS_ST_BUSY;
            ots_msg(c, "CARD BUSY");
            return c->status;
        }
        if (t <= 0)
            break;
        if (t == 3) {
            c->nskipped++;
            continue;
        }
        if (t != 1 || c->name[0] == '.')
            continue;
        nl = ots_len(c->name);
        if (!ots_is_oty(c->name, nl))
            continue;
        c->nlisted++;
        if (n >= OTS_MAXNAMES) {
            c->nskipped++;
            continue;
        }
        lab = c->labels + n * OTS_LABEL;
        j = 0;
        ots_cat(lab, &j, OTS_LABEL, "/SCRIPTS/");
        ots_cat(lab, &j, OTS_LABEL, c->name);
        c->order[n] = (uint8_t)n;
        n++;
    }
    if (n == 0) {
        ots_msg(c, "NO SCRIPTS FOUND");
        return c->status;
    }
    for (i = 1; i < n; i++) {                           /* insertion sort of the indices */
        uint8_t x = c->order[i];
        j = i;
        while (j > 0 && ots_cmp(c->labels + c->order[j - 1] * OTS_LABEL, c->labels + x * OTS_LABEL) > 0) {
            c->order[j] = c->order[j - 1];
            j--;
        }
        c->order[j] = x;
    }
    for (i = 0; i < n; i++) {
        otc_src_t *s = &c->srcv[i];
        lab = c->labels + c->order[i] * OTS_LABEL;
        s->label = (const uint8_t *)lab;
        s->label_len = ots_len(lab);
        s->text = c->src_buf + used;
        s->text_len = 0;
        if (stop || i >= OTC_MAX_SRC_FILES)             /* the compiler gives it its error line: no read */
            continue;
        r = otb_host_file_read(lab, c->src_buf + used, c->src_cap - used);
        if (r < 0)
            r = 0;                                      /* absent or unreadable now: an empty file */
        if ((uint32_t)r > c->src_cap - used) {          /* over the 64 KB: sized only, the compiler stops here */
            stop = 1;
            s->text_len = c->src_cap - used + 1u;
        } else {
            s->text_len = (uint32_t)r;
            used += (uint32_t)r;
        }
    }
    c->nfiles = n;
    {
        uint32_t args[12];
        args[0] = (uint32_t)(uintptr_t)c->srcv;
        args[1] = n;
        args[2] = OTC_PROFILE_M1;
        args[3] = (uint32_t)(uintptr_t)c->arena;
        args[4] = c->arena_size;
        args[5] = (uint32_t)(uintptr_t)c->stage;
        args[6] = OTC_OTB_MAX;
        args[7] = (uint32_t)(uintptr_t)&c->otb_len;
        args[8] = (uint32_t)(uintptr_t)c->report;
        args[9] = c->report_cap;
        args[10] = (uint32_t)(uintptr_t)&c->full_len;
        args[11] = c->stack_top;
        r = otb_host_compile(args);
    }
    if (r != 0) {
        c->status = OTS_ST_CERR;
        c->cerr = (uint32_t)r;
        c->otb_len = c->report_len = 0;
        if (r == OTC_CE_ARENA) {
            ots_msg(c, "SCRIPTS TOO BIG");
        } else {
            uint32_t at = 0;
            ots_cat(c->msg, &at, OTS_MSGLEN, "SCRIPTS: ERROR ");
            ots_dec(c->msg, &at, OTS_MSGLEN, (uint32_t)r);
        }
        return c->status;
    }
    c->report_len = c->full_len;
    if (c->full_len > c->report_cap) {                  /* the beginning as is, then the last line */
        uint32_t at = c->report_cap;
        if (c->report[at - 1] != '\n')
            c->report[at++] = '\n';
        ots_cat(c->report, &at, c->report_cap + 250u, "... report truncated (");
        ots_dec(c->report, &at, c->report_cap + 250u, c->full_len);
        ots_cat(c->report, &at, c->report_cap + 250u, " bytes)\n");
        c->report_len = at;
    }
    if (c->otb_len == 0) {
        c->status = OTS_ST_NOTABLE;
        ots_msg(c, "SCRIPTS: NO TABLE");
        return c->status;
    }
    c->status = OTS_ST_TABLE;
    ots_actions(c);
    {
        uint32_t at = 0;
        ots_cat(c->msg, &at, OTS_MSGLEN, "SCRIPTS: ");
        ots_dec(c->msg, &at, OTS_MSGLEN, c->nfuncs);
        ots_cat(c->msg, &at, OTS_MSGLEN, " FUNCS");
        if (c->nrefused) {
            ots_cat(c->msg, &at, OTS_MSGLEN, ", ");
            ots_dec(c->msg, &at, OTS_MSGLEN, c->nrefused);
            ots_cat(c->msg, &at, OTS_MSGLEN, " ERR");
        }
        if (c->ncut) {
            ots_cat(c->msg, &at, OTS_MSGLEN, ", ");
            ots_dec(c->msg, &at, OTS_MSGLEN, c->ncut);
            ots_cat(c->msg, &at, OTS_MSGLEN, " CUT");
        }
    }
    return c->status;
}
