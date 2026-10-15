/* OType compiler on the unit, front end + checker and report: the ONE translation unit (see otc_comp.h).
 * A host build (shared library for the tests) is `cc -shared otc_comp.c`; the ColdFire object comes from
 * modules/otype/build_comp.py with the flags of build_c.py. */
#include "otc_comp.h"

#include "otc_comp_core.c"
#include "otc_comp_load.c"
#include "otc_comp_parse.c"
#include "otc_comp_check.c"
#include "otc_comp_gen.c"
#include "otc_comp_report.c"
#ifndef OTC_NO_DUMP
#include "otc_comp_dump.c"
#endif

uint32_t otc_comp_ws_size(void)
{
    return OTC_COMP_ARENA_BYTES;
}

/* Worst case, from the frames of `m68k-elf-gcc -mcfv4e -Os -fstack-usage` and the recursion bounds:
 * parse 12.8 KB (64 nested terms), checker 6 KB, cost 32 KB (8 calls deep, each with 9 nested blocks and a 43-deep
 * expression), code walk (dry run, pool pass and the emission: fc_expr 52 + fc_op_node 92 per nested term, fc_mods 80 per
 * modifier, fc_block 100) 25 KB (8 ancestor lines of 21 modifiers + a 64-deep expression + the base frames of otc_compile /
 * otc_emit / fc_compile, 0.6 KB); 36 KB with the margin of 10 % over the largest (the phases never nest). A typical script uses less than
 * 2 KB. (A gabarit of that kind, 2 KB of source, runs in a 32 KB thread stack on the Mac with -O2.) */
uint32_t otc_comp_stack_bytes(void)
{
    return 36u * 1024u;
}

uint32_t otc_comp_used(const void *ws)
{
    return ((const otc_comp_t *)ws)->top;
}

/* One run: the loading of the sources, pass_names, the body parsers and, with `check`, the checker and the diagnostics of
 * the code generator. `dmode`, `dcap`, `thr`: how diagnostics are kept (otc_comp_core.c). */
static int32_t cc_pipeline(otc_comp_t *c, uint32_t limit, const otc_src_t *src, uint32_t nsrc, int check, uint32_t profile,
                           uint32_t dmode, uint32_t dcap, const otc_key_t *thr)
{
    uint32_t i, nbytes = 0, nfiles = 0, f;
    int stop = 0;
    cc_zero(c, (uint32_t)sizeof *c);
    c->limit = limit;
    c->top = ((uint32_t)sizeof *c + 7u) & ~7u;
    c->src = src;
    c->nsrc = nsrc;
    c->dmode = dmode;
    c->dcap = dcap;
    c->profile = profile;
    if (thr)
        cc_key_set(&c->thr, thr);
    if (nsrc > 65534u) {
        cc_fail(c, OTC_CE_ARENA);
        return (int32_t)c->err;
    }
    for (i = 0; i < nsrc && !c->err; i++) {
        if (!stop) {
            if (nfiles >= OTC_MAX_SRC_FILES)
                stop = 1;
            else if (src[i].text_len > OTC_MAX_SRC_BYTES - nbytes)
                stop = 2;
        }
        if (stop) {
            mb_begin(c);
            mb_lit(c, "file not loaded: over ");
            if (stop == 1) {
                mb_dec(c, OTC_MAX_SRC_FILES);
                mb_lit(c, " files");
            } else {
                mb_dec(c, OTC_MAX_SRC_BYTES);
                mb_lit(c, " bytes");
            }
            cc_file_err(c, i, 0, mb_end(c));
            continue;
        }
        nfiles++;
        nbytes += src[i].text_len;
        c->nloaded = i + 1u;
        cc_load_file(c, i);
    }
    if (!c->err)
        cc_pass_names(c);
    for (f = c->func_first; f && !c->err; f = FN(c, f)->next)
        if (FN(c, f)->flags & OTC_FF_REG)
            cc_parse_func(c, f);
    if (check && !c->err) {
        ck_run(c);
        if (!c->err)
            gen_diags(c);
        if (!c->err)
            c->checked = 1;
    }
    return (int32_t)c->err;
}

int32_t otc_front(void *ws, const otc_src_t *src, uint32_t nsrc)
{
    return cc_pipeline((otc_comp_t *)ws, OTC_COMP_ARENA_BYTES, src, nsrc, 0, 0, DM_KEEP, 0xFFFFFFFFu, (const otc_key_t *)0);
}

/* The report keeps OTC_MAX_DIAGS lines, the first ones after sorting, but the diagnostics come in the order of
 * the passes, not in the order of the sort. A run that finds no more than OTC_MAX_DIAGS visible diagnostics keeps them
 * all (the usual case: one run). Otherwise the 1001st one stops it (OTC_CE_ABORT) and the run is made twice more, with
 * the same result each time (nothing depends on anything but the sources): the first one only counts, keeping the
 * OTC_MAX_DIAGS smallest keys, the second one keeps the messages of the diagnostics up to the largest of them. The
 * workspace therefore never holds more than OTC_MAX_DIAGS messages, and no message is built twice in it. */
int32_t otc_run_n(void *ws, uint32_t ws_size, const otc_src_t *src, uint32_t nsrc, uint32_t profile)
{
    otc_comp_t *c = (otc_comp_t *)ws;
    otc_key_t thr;
    int32_t r;
    if (ws_size < (uint32_t)sizeof(otc_comp_t) + 4096u)
        return OTC_CE_ARENA;
    r = cc_pipeline(c, ws_size, src, nsrc, 1, profile, DM_KEEP, OTC_MAX_DIAGS, (const otc_key_t *)0);
    if (r != OTC_CE_ABORT)
        return r;
    r = cc_pipeline(c, ws_size, src, nsrc, 1, profile, DM_COUNT, 0, (const otc_key_t *)0);
    if (r)
        return r;
    cc_key_set(&thr, &c->hk[0]);
    return cc_pipeline(c, ws_size, src, nsrc, 1, profile, DM_SELECT, 0, &thr);
}

int32_t otc_run(void *ws, const otc_src_t *src, uint32_t nsrc, uint32_t profile)
{
    return otc_run_n(ws, OTC_COMP_ARENA_BYTES, src, nsrc, profile);
}

/* The whole job of the unit at card load: the sources of /SCRIPTS (in card order) in, the OTB table and the text of
 * ERRORS.TXT out. See otc_comp.h. */
int32_t otc_compile(const otc_src_t *src, uint32_t nsrc, uint32_t profile, void *arena, uint32_t arena_size, uint8_t *otb,
                    uint32_t otb_cap, uint32_t *otb_len, char *report, uint32_t report_cap, uint32_t *report_len)
{
    int32_t r;
    *otb_len = 0;
    *report_len = 0;
    if (!arena || ((uint32_t)(uintptr_t)arena & 7u))
        return OTC_CE_ARENA;
    r = otc_run_n(arena, arena_size, src, nsrc, profile);
    if (r)
        return r;
    *report_len = otc_report(arena, report, report_cap);
    *otb_len = otc_emit(arena, otb, otb_cap);
    return 0;
}
