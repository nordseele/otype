/* OType bytecode VM. Interface and state: otc_vm.h. Operation table (generated
 * from modules/otype/ops.py): otc_ops.h.
 *
 * This file mirrors modules/otype/vm.py (the instructions, class VMRun) and the handlers of
 * modules/otype/sim.py (class Run and the _lang / state handlers); comments name the Python
 * function each piece follows. The card / slot operations (the "card section" below) keep all their
 * state in the caller's workspace (otc_ws_t) and reaches the card and the slots only through the
 * otb_host_* imports. Integer values are int16 semantics computed in int32 (sim.sat).
 * Python's floor modulo is reproduced explicitly (fmod_pos); C's % truncates.
 *
 * Policy (the same C is compiled for the ColdFire in the DRAM unit): freestanding, <stdint.h>
 * only, no writable global or static, no 64-bit arithmetic, no recursion, every function's stack
 * small (-Wstack-usage=256). The only memory is the caller's otc_vm_t (and, for the card operations,
 * the workspace it binds).
 */
#include <stdint.h>
#include "otc_vm.h"
#include "otc_ops.h"

#define TRY(x) do { int e_ = (x); if (e_) return e_; } while (0)

/* opcodes (spec 14.3) */
enum {
    I_PUSH8 = 0x10, I_PUSH16 = 0x11, I_PUSHK = 0x12, I_PUSHC = 0x13, I_LD = 0x14, I_LDA = 0x15,
    I_LDP = 0x16, I_ST = 0x17, I_DROP = 0x18, I_OP = 0x30, I_OPV = 0x31, I_SET = 0x32,
    I_JMP = 0x40, I_JZ = 0x41, I_SEL = 0x42, I_PROB = 0x43, I_EVERY = 0x44, I_SKIP = 0x45,
    I_LBEG = 0x50, I_LNEXT = 0x51, I_EBEG = 0x52, I_ENEXT = 0x53, I_PASS = 0x54, I_PNEXT = 0x55,
    I_BREAK = 0x56, I_CALL = 0x60, I_RET = 0x61, I_STOP = 0x62, I_SCOPE = 0x70, I_UNSCOPE = 0x71,
    I_DIR = 0x80, I_EXT = 0x81, I_MATCH = 0x82, I_MSG = 0x83
};
enum { CK_L = 0, CK_E = 1, CK_T = 2 };           /* control frame kinds */
enum { ST_RAND = 0, ST_TOSS, ST_PROB, ST_DRUNK, ST_FILE };
#define SLOT_NONE 0xFFFFu
#define P_LIST 2                                  /* pool entry type of a list */

/* ---- tiny libc replacements (gcc may turn loops into calls: keep it from doing so here) ------ */
#if defined(__GNUC__) && !defined(__clang__)
#define OTC_NOIDIOM __attribute__((optimize("no-tree-loop-distribute-patterns")))
#else
#define OTC_NOIDIOM
#endif

OTC_NOIDIOM static void otc_memset(void *d, int c, uint32_t n)
{
    uint8_t *p = (uint8_t *)d;
    while (n--)
        *p++ = (uint8_t)c;
}

/* ---- reading the table: big-endian, byte by byte ---------------------------------------------- */
static uint32_t rd16(const uint8_t *p)
{
    return ((uint32_t)p[0] << 8) | p[1];
}

static int32_t rds16(const uint8_t *p)
{
    int32_t v = (int32_t)rd16(p);
    return v >= 32768 ? v - 65536 : v;
}

/* ---- arithmetic helpers (sim.sat, sim.clamp, Python floor modulo) ------------------------------ */
static int32_t sat(int32_t v)
{
    return v < -32768 ? -32768 : v > 32767 ? 32767 : v;
}

static int32_t clampv(int32_t v, int32_t lo, int32_t hi)     /* sim.clamp: lo wins when lo > hi */
{
    return v < lo ? lo : v > hi ? hi : v;
}

static int32_t iabs(int32_t v)
{
    return v < 0 ? -v : v;
}

static int32_t fmod_pos(int32_t a, int32_t m)      /* Python a % m for m > 0: 0 <= result < m */
{
    int32_t r = a % m;
    return r < 0 ? r + m : r;
}

/* ---- the PRNG (sim.otc_seed_state, sim.xorshift32, Stream.below / toss) ---------------------------- */
static uint32_t otc_seed_state(uint32_t n, uint32_t k)
{
    uint32_t x = (n & 0xFFFFu) | (k << 16);
    x *= 0x9E3779B1u;
    x ^= x >> 16;
    x *= 0x85EBCA6Bu;
    x ^= x >> 13;
    x *= 0xC2B2AE35u;
    x ^= x >> 16;
    return x ? x : 0x6D2B79F5u;
}

static uint32_t otc_xorshift(uint32_t x)
{
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return x;
}

static uint32_t draw(otc_vm_t *vm, const otc_ctx_t *c, unsigned k)    /* Stream.next */
{
    uint32_t x = otc_xorshift(vm->slot[c->str[k]]);
    vm->slot[c->str[k]] = x;
    vm->draws[k]++;
    return x;
}

static int32_t below(otc_vm_t *vm, const otc_ctx_t *c, unsigned k, int32_t n)   /* Stream.below */
{
    uint32_t r = draw(vm, c, k);               /* one draw, whatever n is */
    return n >= 1 ? (int32_t)(((r >> 16) * (uint32_t)n) >> 16) : 0;
}

/* Free slot not referenced by any live context. The caller has already cleared the references it
 * is about to replace, so the scan never needs more than OTC_STREAM_SLOTS slots. */
static int alloc_slot(const otc_vm_t *vm, uint32_t *out)
{
    uint32_t used[(OTC_STREAM_SLOTS + 31) / 32];
    uint32_t f, c, k, s;
    for (s = 0; s < (OTC_STREAM_SLOTS + 31) / 32; s++)
        used[s] = 0;
    for (f = 0; f < vm->nframes; f++)
        for (c = 0; c < vm->fr[f].nctx; c++)
            for (k = 0; k < OTC_NSTREAMS; k++) {
                s = vm->fr[f].ctx[c].str[k];
                if (s != SLOT_NONE)
                    used[s >> 5] |= 1u << (s & 31);
            }
    for (s = 0; s < OTC_STREAM_SLOTS; s++)
        if (!(used[s >> 5] & (1u << (s & 31)))) {
            *out = s;
            return 0;
        }
    return OTC_E_TABLE;
}

/* ---- contexts and step sets -------------------------------------------------------------------- */
static void ctx_copy(otc_ctx_t *d, const otc_ctx_t *s)        /* sim.Ctx.copy (streams shared) */
{
    unsigned k;
    d->mlo = s->mlo;
    d->mhi = s->mhi;
    for (k = 0; k < OTC_NSTREAMS; k++)
        d->str[k] = s->str[k];
    d->ntr = s->ntr;
    d->step = s->step;
    for (k = 0; k < 8; k++)
        d->tr[k] = s->tr[k];
}

static void mask_set(uint32_t *lo, uint32_t *hi, int32_t s)    /* s in 1..64 */
{
    if (s <= 32)
        *lo |= 1u << (s - 1);
    else
        *hi |= 1u << (s - 33);
}

static int mask_has(uint32_t lo, uint32_t hi, int32_t s)
{
    return s <= 32 ? (int)((lo >> (s - 1)) & 1u) : (int)((hi >> (s - 33)) & 1u);
}

static void ctx_steps_range(otc_ctx_t *c, int32_t a, int32_t b)      /* steps = a..b ascending */
{
    int32_t s;
    c->mlo = c->mhi = 0;
    for (s = a; s <= b; s++)
        mask_set(&c->mlo, &c->mhi, s);
}

static int32_t next_step(uint32_t lo, uint32_t hi, int32_t after)    /* next set step > after, 0 if none */
{
    int32_t s;
    for (s = after + 1; s <= 64; s++)
        if (mask_has(lo, hi, s))
            return s;
    return 0;
}

static int32_t cur_track(const otc_vm_t *vm, const otc_ctx_t *c)    /* sim.Run.cur_track */
{
    return c->ntr ? c->tr[0] : vm->host_track;
}

static otc_ctx_t *top(otc_frame_t *fr)
{
    return &fr->ctx[fr->nctx - 1];
}

/* ---- the operation table: binary search by (ns, num) ------------------------------------------- */
static const otc_op_t *op_find(uint32_t ns, uint32_t num)
{
    uint32_t lo = 0, hi = OTC_NOPS, key = (ns << 8) | num;
    while (lo < hi) {
        uint32_t mid = (lo + hi) >> 1;
        uint32_t k = ((uint32_t)otc_ops[mid].ns << 8) | otc_ops[mid].num;
        if (k == key)
            return &otc_ops[mid];
        if (k < key)
            lo = mid + 1;
        else
            hi = mid;
    }
    return 0;
}

/* ---- the pool: only lists are read by the supported operations --------------------------------- */
static int pool_list(const otc_vm_t *vm, int32_t off, uint32_t *n, const uint8_t **items)
{
    const uint8_t *p;
    uint32_t cnt;
    if (off < 2 || (uint32_t)off >= vm->pool_len)
        return OTC_E_TABLE;
    p = vm->tab + vm->pool_off + off;
    if (p[0] != P_LIST)
        return OTC_E_TABLE;
    cnt = p[1];
    if ((uint32_t)off + 2 + 2 * cnt > vm->pool_len)
        return OTC_E_TABLE;
    *n = cnt;
    *items = p + 2;
    return 0;
}

#define P_STR 1
static int pool_str(const otc_vm_t *vm, int32_t off, const uint8_t **s, uint32_t *n)   /* a string entry */
{
    const uint8_t *p;
    uint32_t cnt;
    if (off < 2 || (uint32_t)off >= vm->pool_len)
        return OTC_E_TABLE;
    p = vm->tab + vm->pool_off + off;
    if (p[0] != P_STR)
        return OTC_E_TABLE;
    cnt = p[1];
    if ((uint32_t)off + 2 + cnt > vm->pool_len)
        return OTC_E_TABLE;
    *s = p + 2;
    *n = cnt;
    return 0;
}

/* ---- run bookkeeping --------------------------------------------------------------------------- */
static int charge(otc_vm_t *vm, uint32_t units)                 /* sim.Run.charge */
{
    vm->cost += units;
    return vm->cost > OTC_MAX_COST ? OTC_E_COST : 0;
}

static uint32_t line_of(const otc_vm_t *vm, const otc_fn_t *f, uint32_t ipc)    /* otb.line_of */
{
    const uint8_t *p = vm->tab + f->lines;
    uint32_t line = f->def_line, i;
    for (i = 0; i < f->nlines; i++, p += 4) {
        if (rd16(p) > ipc)
            break;
        line = rd16(p + 2);
    }
    return line;
}

static uint32_t cur_line(const otc_vm_t *vm, const otc_frame_t *fr)
{
    return line_of(vm, &vm->fn[fr->fn], fr->ipc);
}

static void oor(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op)   /* sim.Run.oor */
{
    if (vm->noor < OTC_MAX_OOR) {
        vm->oor_fn[vm->noor] = fr->fn;
        vm->oor_op[vm->noor] = (uint16_t)(((uint32_t)op->ns << 8) | op->num);
        vm->oor_line[vm->noor] = (uint16_t)cur_line(vm, fr);
        vm->noor++;
    } else {
        vm->overflow++;
    }
}

static int push(otc_frame_t *fr, int32_t v)
{
    if (fr->sp >= OTC_MAX_STACK + 1)
        return OTC_E_TABLE;
    fr->stack[fr->sp++] = v;
    return 0;
}

static int pop(otc_frame_t *fr, int32_t *v)       /* VMRun.pop: an empty stack is a corrupt table */
{
    if (!fr->sp)
        return OTC_E_TABLE;
    *v = fr->stack[--fr->sp];
    return 0;
}

/* ---- state operators (sim.set_state / read_state / walk / the o.* and drunk.* setters) --------- */
static int32_t walk(int32_t v, int32_t lo, int32_t hi, int32_t wrap)       /* sim.Run.walk */
{
    if (hi < lo)
        hi = lo;
    if (lo <= v && v <= hi)
        return v;
    if (wrap)
        return lo + fmod_pos(v - lo, hi - lo + 1);
    return clampv(v, lo, hi);
}

static void state_set(otc_fstate_t *st, unsigned h, int32_t v)        /* sim.Run.set_state */
{
    if (h == OTC_H_FLIP)
        st->flip = v ? 1 : 0;
    else if (h == OTC_H_O)
        st->o = clampv(v, st->o_min, st->o_max > st->o_min ? st->o_max : st->o_min);
    else if (h == OTC_H_DRUNK)
        st->drunk = clampv(v, st->d_min, st->d_max > st->d_min ? st->d_max : st->d_min);
}

static int32_t state_read(otc_vm_t *vm, otc_frame_t *fr, unsigned h)  /* sim.Run.read_state */
{
    otc_fstate_t *st = &vm->fs[fr->fn];
    int32_t v, step;
    st->touched = 1;
    if (h == OTC_H_FLIP) {
        v = st->flip;
        st->flip = 1 - v;
        return v;
    }
    if (h == OTC_H_O) {
        v = st->o;
        st->o = walk(v + st->o_inc, st->o_min, st->o_max, st->o_wrap);
        return v;
    }
    step = below(vm, top(fr), ST_DRUNK, 3) - 1;
    v = st->drunk;
    st->drunk = walk(v + step, st->d_min, st->d_max, st->d_wrap);
    return v;
}

static void state_setter(otc_fstate_t *st, unsigned h, int32_t v)     /* sim._setter(key) */
{
    switch (h) {
    case OTC_H_O_MIN:  st->o_min = v; break;
    case OTC_H_O_MAX:  st->o_max = v; break;
    case OTC_H_O_WRAP: st->o_wrap = v ? 1 : 0; break;
    case OTC_H_O_INC:  st->o_inc = v; break;
    case OTC_H_DRUNK_MIN:  st->d_min = v; break;
    case OTC_H_DRUNK_MAX:  st->d_max = v; break;
    default:               st->d_wrap = v ? 1 : 0; break;
    }
    if (h == OTC_H_O_MIN || h == OTC_H_O_MAX)
        st->o = clampv(st->o, st->o_min, st->o_max > st->o_min ? st->o_max : st->o_min);
    else if (h == OTC_H_DRUNK_MIN || h == OTC_H_DRUNK_MAX)
        st->drunk = clampv(st->drunk, st->d_min, st->d_max > st->d_min ? st->d_max : st->d_min);
}

/* The tables of dr.v (sim.DRV_TABLE, byte for byte): velocity of curve c at step s = entry (s - 1) mod 16; a curve outside
 * 0..OTC_DRV_CURVES-1 reads curve 0. */
#define OTC_DRV_CURVES 8
static const uint8_t DRV_TAB[OTC_DRV_CURVES][16] = {
    {100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100},   /* 0 flat */
    {127, 80, 80, 80, 127, 80, 80, 80, 127, 80, 80, 80, 127, 80, 80, 80},               /* 1 accents on the beats */
    {60, 60, 110, 60, 60, 60, 110, 60, 60, 60, 110, 60, 60, 60, 110, 60},               /* 2 accents off the beats */
    {16, 23, 30, 37, 44, 51, 58, 65, 72, 79, 86, 93, 100, 107, 114, 121},               /* 3 ramp up */
    {127, 120, 113, 106, 99, 92, 85, 78, 71, 64, 57, 50, 43, 36, 29, 22},               /* 4 ramp down */
    {120, 70, 120, 70, 120, 70, 120, 70, 120, 70, 120, 70, 120, 70, 120, 70},           /* 5 eighths loud, sixteenths soft */
    {127, 64, 64, 127, 64, 64, 127, 64, 127, 64, 64, 127, 64, 64, 127, 64},             /* 6 tresillo 3+3+2 */
    {32, 44, 56, 68, 80, 92, 104, 116, 127, 116, 104, 92, 80, 68, 56, 44},              /* 7 swell */
};

static int32_t dr_v(int32_t c, int32_t s)
{
#ifdef OTC_SABOTAGE_DRV           /* must-fail build (tools/test_otype_cvm.py TestDrv): s mod 16 instead of (s - 1) mod 16 */
    int32_t j = s % 16;
#else
    int32_t j = (s - 1) % 16;
#endif
    if (j < 0)
        j += 16;                  /* Python's modulo: s <= 0 wraps like the steps above */
    if (c < 0 || c >= OTC_DRV_CURVES)
        c = 0;
    return DRV_TAB[c][j];
}

/* ---- the language handlers (sim._lang, euclid_hit) --------------------------------------------- */
static int32_t euclid_hit(int32_t k, int32_t n, int32_t r, int32_t s)  /* sim.euclid_hit */
{
    int32_t i;
    if (n <= 0 || k <= 0)
        return 0;
    if (k >= n)
        return 1;
    i = fmod_pos(s - 1 - r, n);
    return fmod_pos(i * k, n) < k ? 1 : 0;
}

static int32_t op_div(int32_t a, int32_t b)       /* sim._div: truncates toward zero, 0 if b is 0 */
{
    int32_t q;
    if (b == 0)
        return 0;
    q = iabs(a) / iabs(b);
    return sat((a < 0) == (b < 0) ? q : -q);
}

static int32_t op_mod(int32_t a, int32_t b)       /* sim._mod: a % |b|, never negative; 0 if b is 0 */
{
    if (b == 0)
        return 0;
#ifdef OTC_SABOTAGE
    return a % iabs(b);                            /* wrong on purpose: C truncation (must-fail check) */
#else
    return fmod_pos(a, iabs(b));
#endif
}

static int32_t op_wrap(int32_t x, int32_t lo, int32_t hi)     /* sim._wrap */
{
    if (hi < lo) {
        int32_t t = lo;
        lo = hi;
        hi = t;
    }
    return lo + fmod_pos(x - lo, hi - lo + 1);
}

/* the pure operations: no context, no stream, no state. Returns 1 if h is one of them. */
static int lang_pure(unsigned h, const int32_t *v, int32_t *r)
{
    switch (h) {
    case OTC_H_ADD: *r = sat(v[0] + v[1]); break;
    case OTC_H_SUB: *r = sat(v[0] - v[1]); break;
    case OTC_H_MUL: *r = sat(v[0] * v[1]); break;
    case OTC_H_DIV: *r = op_div(v[0], v[1]); break;
    case OTC_H_MOD: *r = op_mod(v[0], v[1]); break;
    case OTC_H_MIN: *r = v[0] < v[1] ? v[0] : v[1]; break;
    case OTC_H_MAX: *r = v[0] > v[1] ? v[0] : v[1]; break;
    case OTC_H_ABS: *r = sat(iabs(v[0])); break;
    case OTC_H_NEG: *r = sat(-v[0]); break;
    case OTC_H_NOT: *r = v[0] == 0; break;
    case OTC_H_LIM:
        *r = clampv(v[0], v[1] < v[2] ? v[1] : v[2], v[1] > v[2] ? v[1] : v[2]);
        break;
    case OTC_H_WRAP: *r = op_wrap(v[0], v[1], v[2]); break;
    case OTC_H_EQ: *r = v[0] == v[1]; break;
    case OTC_H_NE: *r = v[0] != v[1]; break;
    case OTC_H_LT: *r = v[0] < v[1]; break;
    case OTC_H_GT: *r = v[0] > v[1]; break;
    case OTC_H_LE: *r = v[0] <= v[1]; break;
    case OTC_H_GE: *r = v[0] >= v[1]; break;
    case OTC_H_AND: *r = v[0] != 0 && v[1] != 0; break;
    case OTC_H_OR: *r = v[0] != 0 || v[1] != 0; break;
    case OTC_H_XOR: *r = (v[0] != 0) != (v[1] != 0); break;
    case OTC_H_EZ: *r = v[0] == 0; break;
    case OTC_H_NZ: *r = v[0] != 0; break;
    case OTC_H_ER: *r = euclid_hit(v[0], v[1], 0, v[2]); break;
    case OTC_H_ER_R: *r = euclid_hit(v[0], v[1], v[2], v[3]); break;
    case OTC_H_DR_V: *r = dr_v(v[0], v[1]); break;
    default: return 0;
    }
    return 1;
}

/* rand / toss / list operations (sim._rnd _rrand _toss _chance _pick, AT, LEN) */
static int lang_ctx(otc_vm_t *vm, const otc_ctx_t *ctx, unsigned h, const int32_t *v, int32_t *r)
{
    uint32_t n;
    const uint8_t *items;
    int32_t lo, hi, i;
    switch (h) {
    case OTC_H_RND:
        *r = below(vm, ctx, ST_RAND, v[0]);
        break;
    case OTC_H_RRAND:
        lo = v[0] < v[1] ? v[0] : v[1];
        hi = v[0] > v[1] ? v[0] : v[1];
        *r = lo + below(vm, ctx, ST_RAND, hi - lo + 1);
        break;
    case OTC_H_TOSS:
        *r = (int32_t)(draw(vm, ctx, ST_TOSS) >> 31);
        break;
    case OTC_H_CHANCE:
        *r = below(vm, ctx, ST_TOSS, 100) < v[0];
        break;
    case OTC_H_PICK:
        TRY(pool_list(vm, v[0], &n, &items));
        i = below(vm, ctx, ST_RAND, (int32_t)n);        /* one draw even for a one-element list */
        if (n == 0)
            return OTC_E_TABLE;
        *r = rds16(items + 2 * (uint32_t)i);
        break;
    case OTC_H_AT:
        TRY(pool_list(vm, v[0], &n, &items));
        if (n == 0)
            return OTC_E_TABLE;
        i = clampv(v[1], 1, (int32_t)n) - 1;
        *r = rds16(items + 2 * (uint32_t)i);
        break;
    case OTC_H_LEN:
        TRY(pool_list(vm, v[0], &n, &items));
        *r = (int32_t)n;
        break;
    default:
        return -1;
    }
    return 0;
}

/* ---- the pattern bytes (sim.TrackData on one track's TRAC block; layout in otc_vm.h) ----------- */
enum { M_TRIG = 0x00, M_TLESS = 0x08, M_TLOCK = 0x10, M_ONESHOT = 0x18 };   /* mask offsets */
#define REC_OFF  0x59u          /* step record of step 1; 0x20 bytes per step */
#define WORD_OFF 0x89au         /* trig word of step 1 (RAM; one byte later than in the bank file) */

static uint32_t mask_byte(unsigned m, int32_t s)            /* offset of the byte holding step s */
{
    return m + 7 - (uint32_t)((s - 1) >> 3);
}

static int blk_bit(const uint8_t *b, unsigned m, int32_t s)       /* TrackData.has */
{
    return (b[mask_byte(m, s)] >> ((s - 1) & 7)) & 1;
}

static uint32_t rec_off(int32_t s)                                /* start of the step's record */
{
    return REC_OFF + ((uint32_t)(s - 1) << 5);
}

static uint32_t word_off(int32_t s)                               /* high byte of the step's trig word */
{
    return WORD_OFF + ((uint32_t)(s - 1) << 1);
}

static int blk_has_trig(const uint8_t *b, int32_t s)              /* TrackData.has_trig */
{
    return blk_bit(b, M_TRIG, s) | blk_bit(b, M_TLESS, s) | blk_bit(b, M_ONESHOT, s);
}

static int blk_has_any_trig(const uint8_t *b, int32_t s)          /* TrackData.has_any_trig */
{
    return blk_has_trig(b, s) | blk_bit(b, M_TLOCK, s);
}

static int blk_has_lock(const uint8_t *b, int32_t s)              /* TrackData.has_lock: bytes 0..29 */
{
    const uint8_t *r = b + rec_off(s);
    unsigned k;
    for (k = 0; k < 30; k++)
        if (r[k] != 0xFF)
            return 1;
    return 0;
}

static int blk_has_smp(const uint8_t *b, int32_t s)
{
    return b[rec_off(s) + 31] != 0xFF;
}

static int32_t blk_smp(const uint8_t *b, int32_t s)               /* TrackData.smp.get(s, 0) */
{
    return blk_has_smp(b, s) ? (int32_t)b[rec_off(s) + 31] + 1 : 0;
}

/* sim.Run.read_pred: the predicate `h` (HAS.* / IS.EMPTY) of step s; anything else is is.empty */
static int blk_pred(const uint8_t *b, unsigned h, int32_t s)
{
    if (h == OTC_H_HAS_TRIG)
        return blk_has_trig(b, s);
    if (h == OTC_H_HAS_LOCK)
        return blk_has_lock(b, s);
    if (h == OTC_H_HAS_SMP)
        return blk_has_smp(b, s);
    return !(blk_has_any_trig(b, s) || blk_has_lock(b, s) || blk_has_smp(b, s));   /* is_empty */
}

/* The one place a byte is written: into the bound block and, when there is one, the SRAM copy. */
static void blk_put(otc_vm_t *vm, unsigned t, uint32_t off, uint8_t v)
{
    uint8_t *b = vm->trac[t - 1];
    if (b[off] != v) {
        vm->touched |= 1u << (t - 1);
        vm->pending_trac |= 1u << (t - 1);
    }
    b[off] = v;
    if (vm->copy[t - 1])
        vm->copy[t - 1][off] = v;
}

static void blk_bitput(otc_vm_t *vm, unsigned t, unsigned m, int32_t s, int on)    /* TrackData.put */
{
    uint32_t off = mask_byte(m, s);
    uint8_t bit = (uint8_t)(1u << ((s - 1) & 7)), v = vm->trac[t - 1][off];
    blk_put(vm, t, off, on ? (uint8_t)(v | bit) : (uint8_t)(v & ~bit));
}

/* TrackData.remove_trig: the stock release. */
static void blk_remove_trig(otc_vm_t *vm, unsigned t, int32_t s)
{
    const uint8_t *b = vm->trac[t - 1];
    unsigned k;
    if (!blk_has_trig(b, s))
        return;                                     /* nothing of the family: left alone */
    blk_bitput(vm, t, M_TRIG, s, 0);
    blk_bitput(vm, t, M_TLESS, s, 0);
    blk_bitput(vm, t, M_ONESHOT, s, 0);
    if (blk_bit(b, M_TLOCK, s))
        return;                                     /* a trigless lock keeps the locks */
    for (k = 0; k < 32; k++)
        blk_put(vm, t, rec_off(s) + k, 0xFF);       /* p-locks and sample lock */
#ifndef OTC_SABOTAGE
    blk_put(vm, t, word_off(s) + 1, (uint8_t)(b[word_off(s) + 1] & 0x80));   /* condition gone, micro stays */
#endif
}

/* TrackData.add_trig */
static void blk_add_trig(otc_vm_t *vm, unsigned t, int32_t s)
{
    const uint8_t *b = vm->trac[t - 1];
    if (blk_bit(b, M_TRIG, s))
        return;                                     /* already red */
    if (blk_bit(b, M_TLESS, s) || blk_bit(b, M_ONESHOT, s)) {
        blk_bitput(vm, t, M_TLESS, s, 0);           /* becomes a red trig, record and word kept */
        blk_bitput(vm, t, M_ONESHOT, s, 0);
    } else {
        blk_put(vm, t, word_off(s), 0);             /* condition, micro, count back to nothing */
        blk_put(vm, t, word_off(s) + 1, 0);
    }
    blk_bitput(vm, t, M_TRIG, s, 1);
}

/* TrackData.add_tlock: a step with no trig of any kind also gets a zeroed word; a step that has a trig keeps its own */
static void blk_add_tlock(otc_vm_t *vm, unsigned t, int32_t s)
{
    const uint8_t *b = vm->trac[t - 1];
    if (blk_bit(b, M_TLOCK, s))
        return;
    if (!blk_has_any_trig(b, s)) {
#ifndef OTC_SABOTAGE_TLWORD
        blk_put(vm, t, word_off(s), 0);
        blk_put(vm, t, word_off(s) + 1, 0);
#endif
    }
    blk_bitput(vm, t, M_TLOCK, s, 1);
}

/* the three fields of the trig word (sim.TrackData cond / micro / count). The word is
 * big-endian at word_off(s): the high byte holds count-1 (bits 7-5) and micro bits 5-1, the low byte micro bit 0 (bit 7)
 * and the condition (bits 6-0). Sabotage defines (must-fail builds): OTC_SABOTAGE_CONDMASK (the condition write clears
 * micro's low bit), OTC_SABOTAGE_MICROSIGN (micro written as its magnitude), OTC_SABOTAGE_RATCHCLAMP (ratch clamped to
 * 1..16: the count spills into micro), OTC_SABOTAGE_TWLANDS (cond.set writes on a step without a trig). */
#define MICRO_MAX 23

static uint32_t word_get(const otc_vm_t *vm, unsigned t, int32_t s)
{
    const uint8_t *b = vm->trac[t - 1] + word_off(s);
    return ((uint32_t)b[0] << 8) | b[1];
}

static void word_put(otc_vm_t *vm, unsigned t, int32_t s, uint32_t w)
{
    blk_put(vm, t, word_off(s), (uint8_t)(w >> 8));
    blk_put(vm, t, word_off(s) + 1, (uint8_t)w);
}

static void word_cond(otc_vm_t *vm, unsigned t, int32_t s, int32_t c)          /* c 0..64, 0 = no condition */
{
#ifdef OTC_SABOTAGE_CONDMASK
    word_put(vm, t, s, (word_get(vm, t, s) & 0xFF00u) | (uint32_t)c);
#else
    word_put(vm, t, s, (word_get(vm, t, s) & 0xFF80u) | (uint32_t)c);
#endif
}

static void word_micro(otc_vm_t *vm, unsigned t, int32_t s, int32_t x)         /* x -23..23, 6 bits two's complement */
{
#ifdef OTC_SABOTAGE_MICROSIGN
    x = x < 0 ? -x : x;
#endif
    word_put(vm, t, s, (word_get(vm, t, s) & 0xE07Fu) | (((uint32_t)x & 0x3Fu) << 7));
}

static void word_count(otc_vm_t *vm, unsigned t, int32_t s, int32_t n)         /* n 1..8 trigs on the step */
{
    word_put(vm, t, s, (word_get(vm, t, s) & 0x1FFFu) | (((uint32_t)(n - 1) & 0xFFFFu) << 13));
}

/* sim.Run.lands: a lock / sample-lock write only lands on a step with a trig of any kind */
static void rec_skip(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op)
{
    if (vm->nskip < OTC_MAX_OOR) {
        vm->skip_fn[vm->nskip] = fr->fn;
        vm->skip_op[vm->nskip] = (uint16_t)(((uint32_t)op->ns << 8) | op->num);
        vm->skip_line[vm->nskip] = (uint16_t)cur_line(vm, fr);
        vm->nskip++;
    } else {
        vm->skip_ovf++;
    }
}

static int lands(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op, unsigned t, int32_t s)
{
    if (blk_has_any_trig(vm->trac[t - 1], s))
        return 1;
#ifdef OTC_SABOTAGE_TWLANDS
    if (op->handler == OTC_H_COND_SET)
        return 1;
#endif
    rec_skip(vm, fr, op);
    return 0;
}

/* sim.Run.each_track: a PICKUP track refuses every write, skipped before any step is visited */
static void rec_pickup(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op, unsigned t)
{
    if (vm->npick < OTC_MAX_OOR) {
        vm->pick_fn[vm->npick] = fr->fn;
        vm->pick_op[vm->npick] = (uint16_t)(((uint32_t)op->ns << 8) | op->num);
        vm->pick_line[vm->npick] = (uint16_t)cur_line(vm, fr);
        vm->pick_trk[vm->npick] = (uint8_t)t;
        vm->npick++;
    } else {
        vm->pick_ovf++;
    }
}

/* sim._lock_range: the values a lock of parameter p may take on track t, from the part's descriptor read AT
 * RUNTIME (otb_host_param_info; nothing of it is a constant here). 0 = the part disables the slot: the stock store
 * refuses it, counted as out of range (sim Run.oor), once per track and call, before any step is visited (no draw).
 * Same normalisation as sim.param_norm: a lock is a byte and 0xff means "none", so lo is 0..254, the range ends at
 * 254 at the most and holds at least one value. */
#define LOCK_MAX 254
static int lock_range(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op, unsigned t, int32_t p,
                      int32_t *lo, int32_t *hi)
{
    uint32_t l = 0, c = 0, en = 0;
    otb_host_param_info(t - 1, (uint32_t)p, &l, &c, &en);
#ifndef OTC_SABOTAGE_PLENABLED
    if (!en) {
        oor(vm, fr, op);
        return 0;
    }
#else
    (void)vm; (void)fr; (void)op; (void)en;
#endif
    if (l > LOCK_MAX)
        l = LOCK_MAX;
    if (c < 1)
        c = 1;
    if (c > LOCK_MAX + 1 - l)
        c = LOCK_MAX + 1 - l;
    *lo = (int32_t)l;
    *hi = (int32_t)(l + c - 1);
    return 1;
}

static int need_track(const otc_vm_t *vm, int32_t t)      /* a track the trig / lock ops touch must be bound */
{
    return (t >= 1 && t <= 8 && ((vm->bound >> (t - 1)) & 1)) ? 0 : OTC_E_UNSUPPORTED;
}

/* The write of one step of one track: the handlers of sim.py (_tradd _trdel _trflip _trclr _smpset
 * _smpdel _smprnd _smpclr), `s` the step, v the evaluated arguments. */
static void edit_step(otc_vm_t *vm, const otc_frame_t *fr, const otc_ctx_t *ctx, const otc_op_t *op,
                      unsigned t, int32_t s, const int32_t *v, int32_t plo, int32_t phi)
{
    int32_t lo, hi, val, d;
    unsigned k, h = op->handler;
    uint8_t *rec;
    switch (op->handler) {
    case OTC_H_TR_ADD:
        blk_add_trig(vm, t, s);
        break;
    case OTC_H_TR_DEL:
    case OTC_H_TR_CLR:
        blk_remove_trig(vm, t, s);
        break;
    case OTC_H_TR_FLIP:
        if (blk_has_trig(vm->trac[t - 1], s))
            blk_remove_trig(vm, t, s);
        else
            blk_add_trig(vm, t, s);
        break;
    case OTC_H_SMP_SET:
        if (lands(vm, fr, op, t, s))
            blk_put(vm, t, rec_off(s) + 31, (uint8_t)(v[1] - 1));
        break;
    case OTC_H_SMP_RND:
        lo = v[0] < v[1] ? v[0] : v[1];
        hi = v[0] > v[1] ? v[0] : v[1];
        val = lo + below(vm, ctx, ST_RAND, hi - lo + 1);      /* the draw is made on every step, */
        if (lands(vm, fr, op, t, s))                          /* then the step is checked */
            blk_put(vm, t, rec_off(s) + 31, (uint8_t)(val - 1));
        break;
    /* p-locks (record byte p, 0xff = none), range plo..phi = the part's descriptor (lock_range) */
    case OTC_H_PL_SET:
        if (lands(vm, fr, op, t, s))
#ifdef OTC_SABOTAGE_PLCLAMP
            blk_put(vm, t, rec_off(s) + (uint32_t)v[1], (uint8_t)clampv(v[2], 0, 127));   /* the old fixed range */
#else
            blk_put(vm, t, rec_off(s) + (uint32_t)v[1], (uint8_t)clampv(v[2], plo, phi));
#endif
        break;
    case OTC_H_PL_DEL:
        blk_put(vm, t, rec_off(s) + (uint32_t)v[1], 0xFF);
        break;
    case OTC_H_PL_RND:
        val = plo + below(vm, ctx, ST_RAND, phi - plo + 1);   /* the draw is made on every step, */
        if (lands(vm, fr, op, t, s))                          /* then the step is checked */
            blk_put(vm, t, rec_off(s) + (uint32_t)v[0], (uint8_t)val);
        break;
    case OTC_H_PL_VARY:
        rec = vm->trac[t - 1] + rec_off(s) + (uint32_t)v[0];
        if (*rec != 0xFF) {                                   /* a draw only where the lock is held */
            d = ((phi - plo) * clampv(v[1], 0, 100) + 50) / 100;
            val = below(vm, ctx, ST_RAND, 2 * d + 1) - d;
            if (lands(vm, fr, op, t, s))
                blk_put(vm, t, rec_off(s) + (uint32_t)v[0], (uint8_t)clampv(*rec + val, plo, phi));
        }
        break;
    case OTC_H_PL_CLR:
        for (k = 0; k < 30; k++)                              /* the sample lock (byte 31) stays */
            blk_put(vm, t, rec_off(s) + k, 0xFF);
        break;
    case OTC_H_TL_ADD:
        blk_add_tlock(vm, t, s);
        break;
    /* the trig word (bits 15-13 count-1, 12-7 micro signed 6, 6-0 condition), on a step with a trig of any kind */
    case OTC_H_COND_SET:
        if (lands(vm, fr, op, t, s))
            word_cond(vm, t, s, v[1]);
        break;
    case OTC_H_COND_RND:
        lo = v[0] < v[1] ? v[0] : v[1];
        hi = v[0] > v[1] ? v[0] : v[1];
        val = lo + below(vm, ctx, ST_RAND, hi - lo + 1);      /* the draw is made on every step, */
        if (lands(vm, fr, op, t, s))                          /* then the step is checked */
            word_cond(vm, t, s, val);
        break;
    case OTC_H_MICRO_SET:
        if (lands(vm, fr, op, t, s))
            word_micro(vm, t, s, clampv(v[1], -MICRO_MAX, MICRO_MAX));
        break;
    case OTC_H_MICRO_RND:
        d = clampv(v[0], 0, MICRO_MAX);
        val = below(vm, ctx, ST_RAND, 2 * d + 1) - d;
        if (lands(vm, fr, op, t, s))
            word_micro(vm, t, s, val);
        break;
    case OTC_H_RATCH_SET:
        if (lands(vm, fr, op, t, s))
#ifdef OTC_SABOTAGE_RATCHCLAMP
            word_count(vm, t, s, clampv(v[1], 1, 16));
#else
            word_count(vm, t, s, clampv(v[1], 1, 8));
#endif
        break;
    case OTC_H_TL_FILL:
        if (below(vm, ctx, ST_RAND, 100) < v[0] && !blk_has_trig(vm->trac[t - 1], s)
            && !blk_bit(vm->trac[t - 1], M_TLOCK, s))         /* free steps only */
            blk_add_tlock(vm, t, s);
        break;
    case OTC_H_TL_DEL:
        blk_bitput(vm, t, M_TLOCK, s, 0);
        if (!blk_has_trig(vm->trac[t - 1], s))
            for (k = 0; k < 32; k++)
                blk_put(vm, t, rec_off(s) + k, 0xFF);
        break;
    case OTC_H_TT_ADD: case OTC_H_OS_ADD:
        blk_bitput(vm, t, M_TRIG, s, 0);
        blk_bitput(vm, t, h == OTC_H_TT_ADD ? M_ONESHOT : M_TLESS, s, 0);
        blk_bitput(vm, t, h == OTC_H_TT_ADD ? M_TLESS : M_ONESHOT, s, 1);
        break;
    case OTC_H_TT_DEL: case OTC_H_OS_DEL:
        blk_bitput(vm, t, h == OTC_H_TT_DEL ? M_TLESS : M_ONESHOT, s, 0);
        break;
    case OTC_H_SW_ADD: case OTC_H_SW_DEL: case OTC_H_SW_FLIP:
    case OTC_H_SL_ADD: case OTC_H_SL_DEL: case OTC_H_SL_FLIP: {
        unsigned mask = h <= OTC_H_SW_FLIP ? 0x40 : 0x48;
        int on = h == OTC_H_SW_ADD || h == OTC_H_SL_ADD;
        if (h == OTC_H_SW_FLIP || h == OTC_H_SL_FLIP)
            on = !blk_bit(vm->trac[t - 1], mask, s);
        blk_bitput(vm, t, mask, s, on);
        break;
    }
    case OTC_H_COND_DEL:
        if (lands(vm, fr, op, t, s))
            word_cond(vm, t, s, 0);
        break;
    default:                                                  /* SMP.DEL, SMP.CLR */
        blk_put(vm, t, rec_off(s) + 31, 0xFF);
        break;
    }
}

/* The trig / sample-lock operations. Returns -1 when `h` is not one of them. Reads look at the track
 * being processed (cur_track); writes visit every track of the context (sim.Run.each_track /
 * each_step), a PICKUP one is skipped and counted. TR.ADD TR.DEL TR.FLIP SMP.SET SMP.DEL act on the
 * step v[0]; TR.CLR SMP.RND SMP.CLR on every step of the context. */
static int track_op(otc_vm_t *vm, otc_frame_t *fr, const otc_op_t *op, const int32_t *v, int32_t *r)
{
    const otc_ctx_t *ctx = top(fr);
    unsigned h = op->handler, k, t;
    int32_t s, val, plo = 0, phi = 0;
    int per_step;
    switch (h) {
    case OTC_H_HAS_TRIG: case OTC_H_HAS_LOCK: case OTC_H_HAS_SMP: case OTC_H_IS_EMPTY:
        t = (unsigned)cur_track(vm, ctx);
        TRY(need_track(vm, (int32_t)t));
        *r = blk_pred(vm->trac[t - 1], h, v[0]);
        return 0;
    case OTC_H_SMP_GET:                                       /* sim.Run.first_track */
        t = (unsigned)cur_track(vm, ctx);
        TRY(need_track(vm, (int32_t)t));
        *r = blk_smp(vm->trac[t - 1], v[0]);
        return 0;
    case OTC_H_PL_GET:                                        /* the lock, else what the part holds now */
        t = (unsigned)cur_track(vm, ctx);
        TRY(need_track(vm, (int32_t)t));
        val = vm->trac[t - 1][rec_off(v[0]) + (uint32_t)v[1]];
        *r = val != 0xFF ? val : sat(otb_host_param_value(t - 1, (uint32_t)v[1]));
#ifdef OTC_SABOTAGE_PLGET
        *r = val;
#endif
        return 0;
    case OTC_H_COND_GET: case OTC_H_MICRO_GET: case OTC_H_RATCH_GET: case OTC_H_TRK_SWING:
        t = (unsigned)cur_track(vm, ctx);
        TRY(need_track(vm, (int32_t)t));
        if (h == OTC_H_TRK_SWING)
            *r = vm->trac[t - 1][0x52] + 50;
        else {
            uint32_t w = word_get(vm, t, v[0]);
            if (h == OTC_H_COND_GET)
                *r = (int32_t)(w & 0x7F);
            else if (h == OTC_H_RATCH_GET)
                *r = (int32_t)(w >> 13) + 1;
            else {
                *r = (int32_t)((w >> 7) & 63);
                if (*r & 32) *r -= 64;
            }
        }
        return 0;
    case OTC_H_TRK_TYPE:
        t = (unsigned)cur_track(vm, ctx);
        TRY(need_track(vm, (int32_t)t));
        *r = vm->mtype[t - 1];
        return 0;
    case OTC_H_TR_ADD: case OTC_H_TR_DEL: case OTC_H_TR_FLIP: case OTC_H_SMP_SET: case OTC_H_SMP_DEL:
    case OTC_H_PL_SET: case OTC_H_PL_DEL: case OTC_H_TL_ADD:
    case OTC_H_COND_SET: case OTC_H_MICRO_SET: case OTC_H_RATCH_SET:                  /* trig-word ops */
    case OTC_H_TL_DEL: case OTC_H_TT_ADD: case OTC_H_TT_DEL: case OTC_H_OS_ADD: case OTC_H_OS_DEL:
    case OTC_H_SW_ADD: case OTC_H_SW_DEL: case OTC_H_SW_FLIP:
    case OTC_H_SL_ADD: case OTC_H_SL_DEL: case OTC_H_SL_FLIP: case OTC_H_COND_DEL:
        per_step = 0;
        break;
    case OTC_H_TR_CLR: case OTC_H_SMP_RND: case OTC_H_SMP_CLR:
    case OTC_H_PL_RND: case OTC_H_PL_VARY: case OTC_H_PL_CLR: case OTC_H_TL_FILL:
    case OTC_H_COND_RND: case OTC_H_MICRO_RND:                                    /* trig-word ops */
        per_step = 1;
        break;
    default:
        return -1;
    }
    for (k = 0; k < ctx->ntr; k++) {
        t = ctx->tr[k];
        TRY(need_track(vm, (int32_t)t));
        if (vm->mtype[t - 1] == OTC_PICKUP) {
            rec_pickup(vm, fr, op, t);
            continue;
        }
        if (h == OTC_H_PL_SET || h == OTC_H_PL_RND || h == OTC_H_PL_VARY)   /* the part's descriptor */
            if (!lock_range(vm, fr, op, t, h == OTC_H_PL_SET ? v[1] : v[0], &plo, &phi))
                continue;
        if (!per_step)
            edit_step(vm, fr, ctx, op, t, v[0], v, plo, phi);
        else
            for (s = 1; s <= 64; s++)
                if (mask_has(ctx->mlo, ctx->mhi, s))
                    edit_step(vm, fr, ctx, op, t, s, v, plo, phi);
    }
    return 0;
}

/* sim.Run.steps_pred: keep the context's steps for which the predicate holds on the track processed */
static int steps_pred(otc_vm_t *vm, otc_ctx_t *c, unsigned h)
{
    int32_t t = cur_track(vm, c), s;
    uint32_t lo = 0, hi = 0;
    TRY(need_track(vm, t));
    for (s = 1; s <= 64; s++)
        if (mask_has(c->mlo, c->mhi, s) && blk_pred(vm->trac[t - 1], h, s))
            mask_set(&lo, &hi, s);
    c->mlo = lo;
    c->mhi = hi;
    return 0;
}

/* ---- the card / slot operations ---------------------------------------------------------
 * All state is in the workspace (vm->ws, otc_ws_t); every buffer is there too (the stack stays small).
 * Sabotage defines (must-fail builds of tools/test_otype_cvm.py, never in a real build):
 *   OTC_SABOTAGE_SORT (no case fold in the order), OTC_SABOTAGE_DOT (dot names kept), OTC_SABOTAGE_BAG=1
 *   (bags forgotten between presses) / =2 (a bag refilled one pick too early), OTC_SABOTAGE_CAP (4095 names), OTC_SABOTAGE_COPY (no list copy on a call),
 *   OTC_SABOTAGE_SLOTPATH (no "../" in the stored path), OTC_SABOTAGE_REFUSE (a refusal ignored).
 * p-locks: OTC_SABOTAGE_PLCLAMP (pl.set clamps to 0..127, not the descriptor), OTC_SABOTAGE_PLENABLED (a
 *   disabled slot is written), OTC_SABOTAGE_PLGET (pl.get returns the raw byte, 0xff, where no lock is held),
 *   OTC_SABOTAGE_TLWORD (tl.add leaves the word of a step without trig). */
#define FNV_INIT  0x811C9DC5u
#define FNV_PRIME 0x01000193u
#define NS_FILE   11            /* operation ids of the card instructions (ops.py: dir ext match) */
#define NUM_DIR   1
#define NUM_EXT   2
#define NUM_MATCH 3

OTC_NOIDIOM static void otc_copy(uint8_t *d, const uint8_t *s, uint32_t n)
{
    while (n--)
        *d++ = *s++;
}

OTC_NOIDIOM static uint32_t otc_strlen(const uint8_t *s)
{
    uint32_t n = 0;
    while (s[n])
        n++;
    return n;
}

static uint32_t fold(uint32_t c)                         /* sim.fold: ASCII upper case only */
{
    return (c >= 'a' && c <= 'z') ? c - 32 : c;
}

static uint32_t fnv(uint32_t h, uint32_t b)              /* sim.sig_hash, one byte */
{
    return (h ^ b) * FNV_PRIME;
}

static uint32_t fnv_dec(uint32_t h, uint32_t n)          /* the decimal digits of n (sim: "%d") */
{
    uint8_t d[10];
    uint32_t k = 0;
    do {
        d[k++] = (uint8_t)('0' + n % 10);
        n /= 10;
    } while (n);
    while (k)
        h = fnv(h, d[--k]);
    return h;
}

static uint32_t popcount32(uint32_t x)                   /* no libgcc, no multiply */
{
    x = x - ((x >> 1) & 0x55555555u);
    x = (x & 0x33333333u) + ((x >> 2) & 0x33333333u);
    x = (x + (x >> 4)) & 0x0F0F0F0Fu;
    x += x >> 8;
    x += x >> 16;
    return x & 0x3F;
}

/* the order of the names: (ASCII-folded bytes, then the raw bytes), sim._sort_key */
static int name_cmp(const uint8_t *a, const uint8_t *b)
{
    uint32_t i;
#ifndef OTC_SABOTAGE_SORT
    uint32_t x, y;
    for (i = 0;; i++) {
        x = fold(a[i]);
        y = fold(b[i]);
        if (x != y)
            return x < y ? -1 : 1;
        if (!x)
            break;
    }
#endif
    for (i = 0;; i++) {
        if (a[i] != b[i])
            return a[i] < b[i] ? -1 : 1;
        if (!a[i])
            return 0;
    }
}

/* ---- the names arena: a max-heap keeps the K smallest names during the walk, then heapsort ------- */
static int idx_less(const otc_ws_t *ws, uint32_t s0, uint32_t i, uint32_t j)
{
    return name_cmp(ws->names[s0 + ws->idx[s0 + i]], ws->names[s0 + ws->idx[s0 + j]]) < 0;
}

static void idx_swap(otc_ws_t *ws, uint32_t s0, uint32_t i, uint32_t j)
{
    uint16_t t = ws->idx[s0 + i];
    ws->idx[s0 + i] = ws->idx[s0 + j];
    ws->idx[s0 + j] = t;
}

static void sift_down(otc_ws_t *ws, uint32_t s0, uint32_t i, uint32_t n)
{
    for (;;) {
        uint32_t c = 2 * i + 1;
        if (c >= n)
            return;
        if (c + 1 < n && idx_less(ws, s0, c, c + 1))
            c++;
        if (!idx_less(ws, s0, i, c))
            return;
        idx_swap(ws, s0, i, c);
        i = c;
    }
}

static void sift_up(otc_ws_t *ws, uint32_t s0, uint32_t i)
{
    while (i) {
        uint32_t p = (i - 1) >> 1;
        if (!idx_less(ws, s0, p, i))
            return;
        idx_swap(ws, s0, p, i);
        i = p;
    }
}

static void heap_sort(otc_ws_t *ws, uint32_t s0, uint32_t n)     /* a max-heap on idx[s0..s0+n): ascending */
{
    uint32_t e;
    for (e = n; e > 1;) {
        e--;
        idx_swap(ws, s0, 0, e);
        sift_down(ws, s0, 0, e);
    }
}

/* ---- paths (sim.canon_path, slot_path) ------------------------------------------------------------ */
/* Split on '/' and '\\', drop "" and ".", join with '/'; `*dd` = a ".." component is there (the list is
 * then empty: sim._resolve). A leading '/' is ignored (a path stays inside the Set). */
static uint32_t canon_path(const uint8_t *s, uint32_t n, uint8_t *out, int *dd)
{
    uint32_t i = 0, o = 0, start, len;
    *dd = 0;
    while (i < n) {
        while (i < n && (s[i] == '/' || s[i] == '\\'))
            i++;
        start = i;
        while (i < n && s[i] != '/' && s[i] != '\\')
            i++;
        len = i - start;
        if (!len || (len == 1 && s[start] == '.'))
            continue;
        if (len == 2 && s[start] == '.' && s[start + 1] == '.')
            *dd = 1;
        if (o)
            out[o++] = '/';
        otc_copy(out + o, s + start, len);
        o += len;
    }
    out[o] = 0;
    return o;
}

/* a name with a component that starts with '.': never listed (sim.list_card; the walker already
 * drops them, the C does not rely on it) */
static int dotted(const uint8_t *s)
{
#ifdef OTC_SABOTAGE_DOT
    (void)s;
    return 0;
#else
    uint32_t i;
    if (s[0] == '.')
        return 1;
    for (i = 0; s[i]; i++)
        if (s[i] == '/' && s[i + 1] == '.')
            return 1;
    return 0;
#endif
}

/* ---- the lists (sim.FileList) ------------------------------------------------------------------ */
static void flist_sig(otc_flist_t *l)        /* the signature up to the filters: FOLD(path) | deep | */
{
    uint32_t h = FNV_INIT, k;
    for (k = 0; k < l->pathlen; k++)
        h = fnv(h, fold(l->path[k]));
    h = fnv(h, '|');
    if (l->deep) {
        h = fnv(h, 'd');
        h = fnv(h, 'e');
        h = fnv(h, 'e');
        h = fnv(h, 'p');
    }
    l->hash = fnv(h, '|');
    l->nfilt = 0;
}

static void flist_empty(otc_flist_t *l)      /* sim.FileList(): path "", not deep, no name */
{
    l->s0 = l->nbase = l->own = l->nlive = 0;
    l->deep = l->pathlen = 0;
    l->path[0] = 0;
    flist_sig(l);
}

OTC_NOIDIOM static void flist_live(otc_flist_t *l, uint32_t n)    /* the first n names are in the list */
{
    uint32_t w;
    l->nbase = l->nlive = (uint16_t)n;
    for (w = 0; w < (n + 31) >> 5; w++)
        l->live[w] = 0xFFFFFFFFu;
    if (n & 31)
        l->live[n >> 5] = 0xFFFFFFFFu >> (32 - (n & 31));
}

static int live_has(const otc_flist_t *l, uint32_t r)
{
    return (int)((l->live[r >> 5] >> (r & 31)) & 1u);
}

/* the rank of the f-th name of the list (1-based) */
static uint32_t live_rank(const otc_flist_t *l, uint32_t f)
{
    uint32_t w = 0, c, b;
    for (;; w++) {
        c = popcount32(l->live[w]);
        if (f <= c)
            break;
        f -= c;
    }
    for (b = 0;; b++)
        if (((l->live[w] >> b) & 1u) && !--f)
            return (w << 5) + b;
}

static const uint8_t *list_name(const otc_ws_t *ws, const otc_flist_t *l, uint32_t rank)
{
    return ws->names[l->s0 + ws->idx[l->s0 + rank]];
}

static uint32_t own_sum(const otc_ws_t *ws, uint32_t nframes)     /* the arena slots held by the live frames */
{
    uint32_t k, s = 0;
    for (k = 0; k < nframes; k++)
        s += ws->fl[k].own;
    return s;
}

/* i_call: the callee gets a COPY of the caller's list; its own base is empty (sim: files.copy(), own 0) */
OTC_NOIDIOM static void flist_copy(otc_ws_t *ws, uint32_t to, uint32_t from)
{
#ifdef OTC_SABOTAGE_COPY
    (void)ws; (void)to; (void)from;                 /* wrong on purpose: the callee works on whatever is there */
#else
    otc_flist_t *d = &ws->fl[to], *s = &ws->fl[from];
    uint32_t w, k;
    d->hash = s->hash;
    d->s0 = s->s0;
    d->nbase = s->nbase;
    d->own = 0;
    d->nlive = s->nlive;
    d->deep = s->deep;
    d->nfilt = s->nfilt;
    d->pathlen = s->pathlen;
    for (k = 0; k <= s->pathlen; k++)
        d->path[k] = s->path[k];
    for (w = 0; w < (uint32_t)(s->nbase + 31) >> 5; w++)
        d->live[w] = s->live[w];
#endif
}

/* ---- the card's busy flag, warnings, refusals --------------------------------------------------- */
static int card_access(const otc_op_t *op)           /* sim.Run.card_access: asked before the card is touched */
{
    return otb_host_card_busy(((uint32_t)op->ns << 8) | op->num) ? OTC_E_BUSY : 0;
}

OTC_NOIDIOM static void warn_once(otc_ws_t *ws, uint32_t kind, uint32_t n, uint32_t cap, const uint8_t *path,
                                  uint32_t pl)       /* sim._warn_once: a text is kept once */
{
    uint32_t k, i;
    otc_warn_t *w;
    for (k = 0; k < ws->nwarn; k++) {
        w = &ws->warn[k];
        if (w->kind != kind || w->n != n || w->cap != cap || w->pathlen != pl)
            continue;
        for (i = 0; i < pl && w->path[i] == path[i]; i++)
            ;
        if (i == pl)
            return;
    }
    if (ws->nwarn >= OTC_MAX_DIRWARN) {
        ws->warn_ovf++;
        return;
    }
    w = &ws->warn[ws->nwarn++];
    w->kind = (uint8_t)kind;
    w->n = (uint16_t)n;
    w->cap = (uint16_t)cap;
    w->pathlen = (uint8_t)pl;
    otc_copy(w->path, path, pl);
}

static void rec_refused(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op, uint32_t slot1,
                        const uint8_t *path)         /* sim.Run.load_slot: the refusal list; path without "../" */
{
    otc_ws_t *ws = vm->ws;
    otc_ref_t *r;
    uint32_t n;
    if (path[0] == '.' && path[1] == '.' && path[2] == '/')
        path += 3;
    if (ws->nref >= OTC_MAX_REFUSED) {
        ws->ref_ovf++;
        return;
    }
    r = &ws->ref[ws->nref++];
    r->fn = fr->fn;
    r->op = (uint16_t)(((uint32_t)op->ns << 8) | op->num);
    r->line = (uint16_t)cur_line(vm, fr);
    r->slot = (uint8_t)slot1;
    n = otc_strlen(path);
    r->len = (uint16_t)n;
    otc_copy(r->path, path, n);
}

/* ---- dir (sim._dir) ------------------------------------------------------------------------------ */
/* One walk: every name <= 127 bytes counts in *total, the K smallest are kept in the arena (a max-heap),
 * the longer ones in *nlong. Returns 0 or OTC_E_BUSY. */
static int dir_walk(otc_ws_t *ws, uint32_t s0, uint32_t K, uint32_t *total, uint32_t *nlong, uint32_t *kept)
{
    uint32_t n = 0, t = 0, nl = 0, len, slot;
    int32_t rc;
    for (;;) {
        rc = otb_host_dir_next(ws->it, (char *)ws->walk);
        if (rc < 0)
            return OTC_E_BUSY;
        if (!rc)
            break;
        if (dotted(ws->walk))
            continue;
        len = otc_strlen(ws->walk);
        if (len > OTC_NAME_MAX) {
            nl++;
            continue;
        }
        t++;
        if (n < K) {
            otc_copy(ws->names[s0 + n], ws->walk, len + 1);
            ws->idx[s0 + n] = (uint16_t)n;
            sift_up(ws, s0, n);
            n++;
        } else if (K && name_cmp(ws->walk, ws->names[s0 + ws->idx[s0]]) < 0) {
            slot = ws->idx[s0];                  /* smaller than the largest kept: it takes its place */
            otc_copy(ws->names[s0 + slot], ws->walk, len + 1);
            sift_down(ws, s0, 0, K);
        }
    }
    *total = t;
    *nlong = nl + otb_host_dir_skipped(ws->it);
    *kept = n;
    return 0;
}

static int do_dir(otc_vm_t *vm, const uint8_t *s, uint32_t slen, uint32_t deep)
{
    otc_ws_t *ws = vm->ws;
    otc_flist_t *l = &ws->fl[vm->nframes - 1];
    uint32_t pl, top, cap, total = 0, nlong = 0, n = 0;
    int dd, rc;
    TRY(card_access(op_find(NS_FILE, NUM_DIR)));
    pl = canon_path(s, slen, ws->canon, &dd);
    l->own = 0;                                      /* this frame's old base goes back to the budget */
    top = own_sum(ws, vm->nframes);
    cap = OTC_NAMES_MAX - top;                       /* sim: min(DIR_LIST_MAX, DIR_LIST_MAX - sum(own)) */
#ifdef OTC_SABOTAGE_CAP
    if (cap)
        cap--;
#endif
    l->deep = (uint8_t)(deep != 0);
    l->pathlen = (uint8_t)pl;
    otc_copy(l->path, ws->canon, pl + 1);
    flist_sig(l);
    l->s0 = (uint16_t)top;
    l->nbase = l->nlive = 0;
    if (!dd) {                                       /* a ".." component: the list is empty, no walk */
        rc = otb_host_dir_open(ws->it, (const char *)ws->canon, l->deep);
        if (rc == -2)
            return OTC_E_BUSY;
        if (rc == 0)
            TRY(dir_walk(ws, top, cap, &total, &nlong, &n));
    }
    if (nlong)
        warn_once(ws, 0, nlong, 0, ws->canon, pl);
    if (total > cap)
        warn_once(ws, 1, total, cap, ws->canon, pl);
    heap_sort(ws, top, n);
    flist_live(l, n);
    l->own = (uint16_t)n;
    return 0;
}

/* ---- ext, match (sim._ext, _match) ---------------------------------------------------------------- */
static int eq_fold(const uint8_t *s, const char *lit)
{
    while (*lit) {
        if (fold(*s) != (uint32_t)(uint8_t)*lit)
            return 0;
        s++;
        lit++;
    }
    return *s == 0;
}

/* sim.ext_key on a relative name: posixpath.splitext, then folded; bit c of `mask` = extension word c */
static int ext_in(const uint8_t *name, uint32_t mask)
{
    uint32_t i, sep = 0, dot = 0, have_sep = 0, have_dot = 0, f;
    static const char words[4][8] = {".WAV", ".AIF", ".AIFF", ".AIFC"};
    for (i = 0; name[i]; i++) {
        if (name[i] == '/') {
            sep = i;
            have_sep = 1;
        } else if (name[i] == '.') {
            dot = i;
            have_dot = 1;
        }
    }
    if (!have_dot || (have_sep && dot < sep))
        return 0;
    for (f = have_sep ? sep + 1 : 0; f < dot; f++)
        if (name[f] != '.')
            break;
    if (f >= dot)
        return 0;                                    /* only dots before the last one: no extension */
    for (i = 0; i < 4; i++)
        if ((mask >> i & 1u) && eq_fold(name + dot, words[i]))
            return 1;
    return 0;
}

/* sim.wildcard_match: `*` is any run, everything else literal; ASCII-case-insensitive; the whole name */
static int wild(const uint8_t *p, uint32_t pn, const uint8_t *s, uint32_t sn)
{
    uint32_t pi = 0, si = 0, after = 0, mark = 0;
    int star = 0;
    while (si < sn) {
        if (pi < pn && p[pi] == '*') {
            star = 1;
            after = ++pi;                            /* where the pattern resumes after the last `*` */
            mark = si;
        } else if (pi < pn && fold(p[pi]) == fold(s[si])) {
            pi++;
            si++;
        } else if (star) {
            pi = after;                              /* the `*` swallows one more character */
            si = ++mark;
        } else {
            return 0;
        }
    }
    while (pi < pn && p[pi] == '*')
        pi++;
    return pi == pn;
}

static void sig_text(otc_flist_t *l, const char *lit)    /* one literal of a filter's text, FNV-ed */
{
    while (*lit)
        l->hash = fnv(l->hash, (uint8_t)*lit++);
}

static int i_ext(otc_vm_t *vm, otc_frame_t *fr, uint32_t n)
{
    const otc_op_t *op = op_find(NS_FILE, NUM_EXT);
    otc_ws_t *ws = vm->ws;
    otc_flist_t *l;
    static const uint8_t rank[4] = {1, 3, 2, 0};     /* the words in sorted order: aif aifc aiff wav */
    static const char low[4][8] = {"wav", "aif", "aiff", "aifc"};
    uint32_t cnt[4], mask = 0, k, c, r;
    if (!ws)
        return OTC_E_UNSUPPORTED;
    if (fr->sp < n)
        return OTC_E_TABLE;
    for (k = 0; k < 4; k++)
        cnt[k] = 0;
    for (k = 0; k < n; k++) {
        int32_t v = fr->stack[fr->sp - n + k];
        if (v < 0 || v > 3)
            return OTC_E_TABLE;
        cnt[v]++;
        mask |= 1u << v;
    }
    fr->sp = (uint8_t)(fr->sp - n);
    TRY(charge(vm, op->cost));
    l = &ws->fl[vm->nframes - 1];
    for (r = 0; r < l->nbase; r++)
        if (live_has(l, r) && !ext_in(list_name(ws, l, r), mask)) {
            l->live[r >> 5] &= ~(1u << (r & 31));
            l->nlive--;
        }
    if (l->nfilt)                                    /* the signature text: ext:aif+wav (sorted, lower case) */
        l->hash = fnv(l->hash, ',');
    l->nfilt = 1;
    sig_text(l, "ext:");
    for (k = 0, c = 0; k < 4; k++)
        for (r = 0; r < cnt[rank[k]]; r++) {
            if (c++)
                l->hash = fnv(l->hash, '+');
            sig_text(l, low[rank[k]]);
        }
    return 0;
}

static int i_match(otc_vm_t *vm, otc_frame_t *fr, uint32_t n)
{
    const otc_op_t *op = op_find(NS_FILE, NUM_MATCH);
    otc_ws_t *ws = vm->ws;
    otc_flist_t *l;
    const uint8_t *p, *nm, *base;
    uint32_t k, r, pn, i, len, hit;
    if (!ws)
        return OTC_E_UNSUPPORTED;
    if (fr->sp < n)
        return OTC_E_TABLE;
    for (k = 0; k < n; k++)
        TRY(pool_str(vm, fr->stack[fr->sp - n + k], &p, &pn));
    TRY(charge(vm, op->cost));
    l = &ws->fl[vm->nframes - 1];
    for (r = 0; r < l->nbase; r++) {
        if (!live_has(l, r))
            continue;
        nm = list_name(ws, l, r);
        len = otc_strlen(nm);
        for (base = nm, i = 0; i < len; i++)         /* the last component */
            if (nm[i] == '/')
                base = nm + i + 1;
        hit = 0;
        for (k = 0; k < n && !hit; k++) {
            pool_str(vm, fr->stack[fr->sp - n + k], &p, &pn);
            hit = (uint32_t)wild(p, pn, base, len - (uint32_t)(base - nm));
        }
        if (!hit) {
            l->live[r >> 5] &= ~(1u << (r & 31));
            l->nlive--;
        }
    }
    if (l->nfilt)
        l->hash = fnv(l->hash, ',');
    l->nfilt = 1;
    sig_text(l, "match:");
    for (k = 0; k < n; k++) {
        pool_str(vm, fr->stack[fr->sp - n + k], &p, &pn);
        if (k)
            l->hash = fnv(l->hash, '+');
        for (i = 0; i < pn; i++)
            l->hash = fnv(l->hash, p[i]);
    }
    fr->sp = (uint8_t)(fr->sp - n);
    return 0;
}

/* ---- msg (sim.msg_text, vm.i_msg): the run keeps the text of its LAST msg in vm->msg, whatever draws it ---------------- */
static void msg_put(otc_vm_t *vm, uint32_t *n, uint8_t ch)
{
    if (*n < OTC_MSG_MAX)                              /* shortcut: cut at OTC_MSG_MAX, the checker refuses longer literals */
        vm->msg[(*n)++] = (char)ch;
}

static int i_msg(otc_vm_t *vm, otc_frame_t *fr, uint32_t n, uint32_t mask)
{
    const otc_op_t *op = op_find(14, 1);
    const uint8_t *p;
    uint32_t k, m = 0, pn, i;
    int32_t v;
    if (n > OTC_MSG_ITEMS || fr->sp < n)
        return OTC_E_TABLE;
    for (k = 0; k < n; k++)
        if (mask >> k & 1u)
            TRY(pool_str(vm, fr->stack[fr->sp - n + k], &p, &pn));
    TRY(charge(vm, op->cost));
    for (k = 0; k < n; k++) {                          /* the items are all evaluated: nothing runs between the writes */
        if (k)
            msg_put(vm, &m, ' ');
        v = fr->stack[fr->sp - n + k];
        if (mask >> k & 1u) {
            pool_str(vm, v, &p, &pn);
            for (i = 0; i < pn; i++)
                msg_put(vm, &m, p[i]);
        } else {
            char d[12];
            uint32_t nd = 0, u = v < 0 ? 0u - (uint32_t)v : (uint32_t)v;
            do {
                d[nd++] = (char)('0' + u % 10u);
                u /= 10u;
            } while (u);
            if (v < 0)
                msg_put(vm, &m, '-');
            while (nd)
                msg_put(vm, &m, (uint8_t)d[--nd]);
        }
    }
    vm->msg[m] = 0;
    fr->sp = (uint8_t)(fr->sp - n);
    return 0;
}

static int i_dir(otc_vm_t *vm, otc_frame_t *fr, uint32_t deep)
{
    const otc_op_t *op = op_find(NS_FILE, NUM_DIR);
    const uint8_t *s;
    uint32_t n;
    int32_t off;
    if (!vm->ws)
        return OTC_E_UNSUPPORTED;
    TRY(pop(fr, &off));
    if (off < 0) {
        uint32_t k = (uint32_t)(-off - 1);
        if (k >= OTC_MAX_PARAMS || !vm->folder_path[k])
            return OTC_E_TABLE;
        s = (const uint8_t *)vm->folder_path[k];
        n = vm->folder_len[k];
    } else {
        TRY(pool_str(vm, off, &s, &n));
    }
    TRY(charge(vm, op->cost));
    return do_dir(vm, s, n, deep);
}

/* ---- file.pick: the bags (sim._filepick) ----------------------------------------------------------- */
static otc_bag_t *bag_find(otc_ws_t *ws, uint32_t fn, uint32_t h)
{
    uint32_t k;
    for (k = 0; k < OTC_BAGS_MAX; k++)
        if (ws->bag[k].used && ws->bag[k].fn == fn && ws->bag[k].hash == h)
            return &ws->bag[k];
    return 0;
}

static otc_bag_t *bag_new(otc_ws_t *ws, uint32_t fn, uint32_t h)    /* a free place, else the OLDEST goes */
{
    uint32_t k, pick = 0;
    for (k = 0; k < OTC_BAGS_MAX; k++) {
        if (!ws->bag[k].used) {
            pick = k;
            break;
        }
        if (ws->bag[k].seq < ws->bag[pick].seq)
            pick = k;
    }
    ws->bag[pick].used = 1;
    ws->bag[pick].fn = (uint8_t)fn;
    ws->bag[pick].hash = h;
    ws->bag[pick].seq = ++ws->bagseq;
    ws->bag[pick].n = ws->bag[pick].nrest = 0;
    return &ws->bag[pick];
}

#if defined(OTC_SABOTAGE_BAG) && OTC_SABOTAGE_BAG == 2
#define BAG_REFILL(b) ((b)->nrest <= 1)             /* wrong on purpose: refilled one pick too early */
#else
#define BAG_REFILL(b) (!(b)->nrest)
#endif

OTC_NOIDIOM static int32_t file_pick(otc_vm_t *vm, otc_frame_t *fr, const otc_flist_t *l)
{
    otc_ws_t *ws = vm->ws;
    otc_bag_t *b;
    uint32_t h, cnt = l->nlive, k, i;
    if (cnt == 0) {
        below(vm, top(fr), ST_FILE, 0);     /* one draw, even for nothing */
        return 0;
    }
    h = fnv(l->hash, '|');                          /* sim FileList.sig: ... | n */
    h = fnv_dec(h, cnt);
    b = bag_find(ws, fr->fn, h);
    if (!b)
        b = bag_new(ws, fr->fn, h);
    if (BAG_REFILL(b)) {                            /* an exhausted bag refills on the next pick */
        b->n = (uint16_t)cnt;
        for (k = 0; k < cnt; k++)
            b->rest[k] = (uint16_t)(k + 1);
        b->nrest = (uint16_t)cnt;
    }
    k = (uint32_t)below(vm, top(fr), ST_FILE, b->nrest);
    i = b->rest[k];
    for (; k + 1 < b->nrest; k++)                   /* bag.pop(k): the others close up, in order */
        b->rest[k] = b->rest[k + 1];
    b->nrest--;
    return (int32_t)i;
}

/* ---- slots ---------------------------------------------------------------------------------------- */
static int slot_is_free(uint32_t slot1)
{
    return otb_host_slot_name(slot1 - 1, 0, 0) == 0;
}

/* the path stored for a name of the list: "../" + folder + "/" + name (sim.slot_path, plus the unit's "../") */
static void spath_build(otc_ws_t *ws, const otc_flist_t *l, const uint8_t *name)
{
    uint32_t o = 0;
#ifndef OTC_SABOTAGE_SLOTPATH
    ws->spath[o++] = '.';
    ws->spath[o++] = '.';
    ws->spath[o++] = '/';
#endif
    if (l->pathlen) {
        otc_copy(ws->spath + o, l->path, l->pathlen);
        o += l->pathlen;
        ws->spath[o++] = '/';
    }
    otc_copy(ws->spath + o, name, otc_strlen(name) + 1);
}

static void canon_full_path(const otc_ws_t *ws, const otc_flist_t *l, const uint8_t *name, char *out, uint32_t max)
{
    uint32_t o = 1;
    (void)ws;
    out[0] = '/';
    if (l->pathlen) {
        uint32_t pl = l->pathlen;
        if (pl >= max - 1)
            pl = max - 2;
        otc_copy((uint8_t *)out + o, l->path, pl);
        o += pl;
        out[o++] = '/';
    }
    while (*name && o < max - 1)
        out[o++] = (char)*name++;
    out[o] = 0;
}

static int flist_set_single(otc_vm_t *vm, const char *full_path)
{
    otc_ws_t *ws = vm->ws;
    otc_flist_t *l;
    const char *p = full_path;
    const char *p_orig;
    const char *last_slash = 0;
    const char *s;
    uint32_t folder_len = 0, name_len = 0, top, pl;
    int dd;
    if (!ws || !vm->nframes)
        return OTC_AE_BAD_ARG;
    l = &ws->fl[vm->nframes - 1];
    while (*p == '/' || *p == '\\')
        p++;
    p_orig = p;
    for (s = p; *s; s++) {
        if (*s == '/' || *s == '\\')
            last_slash = s;
    }
    if (last_slash) {
        folder_len = (uint32_t)(last_slash - p_orig);
        p = last_slash + 1;
    }
    name_len = otc_strlen((const uint8_t *)p);
    if (!name_len || name_len > 127 || folder_len > 255)
        return OTC_AE_BAD_ARG;
    pl = canon_path((const uint8_t *)p_orig, folder_len, ws->canon, &dd);
    if (dd)
        return OTC_AE_BAD_ARG;
    l->own = 0;
    top = own_sum(ws, vm->nframes);
    l->deep = 0;
    l->pathlen = (uint8_t)pl;
    otc_copy(l->path, ws->canon, pl + 1);
    flist_sig(l);
    l->s0 = (uint16_t)top;
    if (top < OTC_NAMES_MAX) {
        otc_copy(ws->names[top], (const uint8_t *)p, name_len);
        ws->names[top][name_len] = 0;
        ws->idx[top] = 0;
        flist_live(l, 1);
        l->own = 1;
    } else {
        flist_live(l, 0);
        return OTC_AE_BAD_ARG;
    }
    return 0;
}

/* sim.Run.load_slot: the unit loads the file named `sp` ("../...") into slot 1..128. A refusal CLEARS the
 * slot (the unit calls FUN_40025288) and is recorded. 1 = loaded. */
static int load_slot(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op, uint32_t slot1, const uint8_t *sp)
{
    otc_ws_t *ws = vm->ws;
    int ok;
    ws->touched = 1;
    ok = otb_host_slot_set(slot1 - 1, (const char *)sp) == 0;
    if (ok)
        ok = otb_host_slot_load(slot1 - 1) == 1;
    else
        otb_host_slot_clr(slot1 - 1);               /* a name the unit cannot store counts as a refusal */
    if (ok)
        return 1;
#ifdef OTC_SABOTAGE_REFUSE
    (void)fr; (void)op; (void)rec_refused;
    return 1;                                       /* the refusal is ignored */
#else
    rec_refused(vm, fr, op, slot1, sp);
    return 0;
#endif
}

static int slot_load_op(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op, const otc_flist_t *l,
                        int32_t n, int32_t f)
{
    otc_ws_t *ws = vm->ws;
    if (f < 1 || f > (int32_t)l->nlive) {
        oor(vm, fr, op);
        return 0;
    }
    TRY(card_access(op));
    spath_build(ws, l, list_name(ws, l, live_rank(l, (uint32_t)f)));
    load_slot(vm, fr, op, (uint32_t)n, ws->spath);
    return 0;
}

/* static.swap (sim _staticswap): each slot is reloaded by name from the other's (the names are read first, a, then b); an empty
 * source clears its target. A refused reload frees its slot. a = b, or both empty: nothing moves, no card access. */
static int static_swap(otc_vm_t *vm, const otc_frame_t *fr, const otc_op_t *op, uint32_t a1, uint32_t b1)
{
    otc_ws_t *ws = vm->ws;
    int32_t la, lb;
    if (a1 == b1)
        return 0;
    la = otb_host_slot_name(a1 - 1, (char *)ws->shuf[0], OTC_WALK_BYTES);
    lb = otb_host_slot_name(b1 - 1, (char *)ws->shuf[1], OTC_WALK_BYTES);
    if (la <= 0 && lb <= 0)
        return 0;
    TRY(card_access(op));
    ws->touched = 1;
    if (lb > 0)
        load_slot(vm, fr, op, a1, ws->shuf[1]);
    else
        otb_host_slot_clr(a1 - 1);
#ifndef OTC_SABOTAGE_SWAP
    if (la > 0)
        load_slot(vm, fr, op, b1, ws->shuf[0]);
    else
        otb_host_slot_clr(b1 - 1);
#endif
    return 0;
}

/* The card / slot operations that go through call_op (the arguments are already range checked; dir ext
 * match have their own instructions). Returns -1 when `op` is not one of them. */
static int card_op(otc_vm_t *vm, otc_frame_t *fr, const otc_op_t *op, const int32_t *v, int32_t *r)
{
    otc_ws_t *ws = vm->ws;
    otc_flist_t *l = &ws->fl[vm->nframes - 1];
    uint32_t k, n;
    int32_t x;
    switch (op->handler) {
    case OTC_H_FILE_LEN:
        *r = l->nlive;
        return 0;
    case OTC_H_FILE_RND:
        n = l->nlive;
        x = below(vm, top(fr), ST_FILE, (int32_t)n);        /* one draw even for an empty list */
        *r = n ? x + 1 : 0;
        return 0;
    case OTC_H_FILE_PICK:
        *r = file_pick(vm, fr, l);
        return 0;
    case OTC_H_FILE_AT:
        *r = (v[0] >= 1 && v[0] <= (int32_t)l->nlive) ? v[0] : 0;
        return 0;
    case OTC_H_STATIC_LOAD:
        return slot_load_op(vm, fr, op, l, v[0], v[1]);
    case OTC_H_STATIC_FREE:
        *r = slot_is_free((uint32_t)v[0]);
        return 0;
    case OTC_H_STATIC_FIRSTFREE:
        *r = 0;
        for (k = 1; k <= OTC_UNIT_SLOTS; k++)
            if (slot_is_free(k)) {
                *r = (int32_t)k;
                break;
            }
        return 0;
    case OTC_H_STATIC_CLR:
        if (!slot_is_free((uint32_t)v[0])) {                /* an empty slot is not touched: no card access */
            TRY(card_access(op));
            ws->touched = 1;
            otb_host_slot_clr((uint32_t)v[0] - 1);
        }
        return 0;
    case OTC_H_STATIC_SWAP:
        return static_swap(vm, fr, op, (uint32_t)v[0], (uint32_t)v[1]);
    case OTC_H_SLOTS_CLR:
        ws->nwork = 0;
        return 0;
    case OTC_H_SLOTS_ADD:
        for (k = 0; k < ws->nwork && ws->work[k] != v[0]; k++)
            ;
        if (k == ws->nwork)
            ws->work[ws->nwork++] = (uint8_t)v[0];
        return 0;
    case OTC_H_SLOTS_RND:
        x = below(vm, top(fr), ST_FILE, (int32_t)ws->nwork);
        *r = ws->nwork ? ws->work[x] : 0;
        return 0;
    case OTC_H_SRC_SAVE: {
        int32_t rc;
        if (card_access(op)) return OTC_AE_CARD_BUSY;
        rc = otb_host_src_save((char *)ws->spath, sizeof(ws->spath));
        if (rc < 0)
            return rc;
        return flist_set_single(vm, (const char *)ws->spath);
    }
    case OTC_H_FLEX_LOAD: {
        int32_t rc;
        if (v[1] < 1 || v[1] > (int32_t)l->nlive) {
            oor(vm, fr, op);
            return 0;
        }
        if (card_access(op)) return OTC_AE_CARD_BUSY;
        canon_full_path(ws, l, list_name(ws, l, live_rank(l, (uint32_t)v[1])), (char *)ws->spath, sizeof(ws->spath));
        rc = otb_host_flex_load((uint32_t)(v[0] - 1), (const char *)ws->spath);
        if (rc < 0)
            return rc;
        ws->touched = 1;
        return 0;
    }
    case OTC_H_FLEX_FREE: {
        int32_t rc = otb_host_flex_free((uint32_t)(v[0] - 1));
        if (rc < 0)
            return rc;
        *r = rc;
        return 0;
    }
    case OTC_H_FLEX_FIRSTFREE:
        *r = 0;
        for (k = 1; k <= OTC_UNIT_SLOTS; k++) {
            x = otb_host_flex_free(k - 1);
            if (x < 0)
                return x;
            if (x) {
                *r = (int32_t)k;
                break;
            }
        }
        return 0;
    case OTC_H_FLEX_DEL:
        x = otb_host_flex_free((uint32_t)(v[0] - 1));
        if (x < 0)
            return x;
        if (!x) {                                           /* an empty slot is not touched */
            x = otb_host_flex_del((uint32_t)(v[0] - 1));
            if (x < 0)
                return x;
            ws->touched = 1;
        }
        return 0;
    case OTC_H_CAT_CLR:
        otb_host_cat_clr();
        return 0;
    case OTC_H_CAT_ADD: {
        int32_t rc;
        if (v[0] < 1 || v[0] > (int32_t)l->nlive) {
            oor(vm, fr, op);
            return 0;
        }
        canon_full_path(ws, l, list_name(ws, l, live_rank(l, (uint32_t)v[0])), (char *)ws->spath, sizeof(ws->spath));
        rc = otb_host_cat_add((const char *)ws->spath);
        if (rc < 0)
            return rc;
        return 0;
    }
    case OTC_H_CAT_SAVE: {
        const uint8_t *s;
        uint32_t slen, i;
        char namebuf[64];
        int32_t rc;
        TRY(pool_str(vm, v[0], &s, &slen));
        if (slen < 1 || slen > 63)
            return OTC_AE_BAD_ARG;
        for (i = 0; i < slen; i++) {
            char c = (char)s[i];
            if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == ' ' || c == '_' || c == '-'))
                return OTC_AE_BAD_ARG;
            namebuf[i] = c;
        }
        namebuf[slen] = 0;
        if (card_access(op)) return OTC_AE_CARD_BUSY;
        rc = otb_host_cat_save(namebuf, (char *)ws->spath, sizeof(ws->spath));
        if (rc < 0)
            return rc;
        return flist_set_single(vm, (const char *)ws->spath);
    }
    case OTC_H_NORM: {
        int32_t rc;
        if (v[0] < 1 || v[0] > (int32_t)l->nlive) {
            oor(vm, fr, op);
            return 0;
        }
        if (card_access(op)) return OTC_AE_CARD_BUSY;
        canon_full_path(ws, l, list_name(ws, l, live_rank(l, (uint32_t)v[0])), (char *)ws->spath, sizeof(ws->spath));
        rc = otb_host_norm((const char *)ws->spath, v[1]);
        if (rc < 0)
            return rc;
        return 0;
    }
    default:
        return OTC_E_UNSUPPORTED;
    }
}

/* ---- the part operations (sim.py _parset ... _trkinit) ------------------------------
 * Every write is an import (otc_vm.h): the C decides what (clamped / drawn / in the host's order), the unit performs it.
 * Tracks = the context's, no PICKUP gate. Sabotage defines (must-fail builds, never in a real build):
 *   OTC_SABOTAGE_PARCLAMP (par.set hands the raw value over), OTC_SABOTAGE_PARDISABLED (a disabled slot is written),
 *   OTC_SABOTAGE_INITORDER (trk.init applies the factory effects AFTER the parameters). */
#define N_SCENES      16
#define SCENE_BYTES   32
#define CHOOSER_MAX   255u      /* sim.chooser_norm */

static uint32_t chooser_norm(uint32_t n)
{
    return n > CHOOSER_MAX ? CHOOSER_MAX : n;
}

static void part_mark(otc_vm_t *vm, unsigned t)
{
    vm->part_touched |= 1u << (t - 1);
    vm->pending_part |= 1u << (t - 1);
}

/* sim._part_range: (lo, hi) of parameter p on track t (the shared normalisation of lock_range), 0 = disabled */
static int part_range(unsigned t, int32_t p, int32_t *lo, int32_t *hi)
{
    uint32_t l = 0, c = 0, en = 0;
    otb_host_param_info(t - 1, (uint32_t)p, &l, &c, &en);
#ifndef OTC_SABOTAGE_PARDISABLED
    if (!en)
        return 0;
#else
    (void)en;
#endif
    if (l > LOCK_MAX)
        l = LOCK_MAX;
    if (c < 1)
        c = 1;
    if (c > LOCK_MAX + 1 - l)
        c = LOCK_MAX + 1 - l;
    *lo = (int32_t)l;
    *hi = (int32_t)(l + c - 1);
    return 1;
}

static void fx_apply(otc_vm_t *vm, unsigned t, unsigned slot0, uint32_t index)       /* sim._fx_applied */
{
    otb_host_fx_set(t - 1, slot0, index);
    part_mark(vm, t);
}

static void scene_fill(otc_vm_t *vm, unsigned t, unsigned sc0)                      /* sim._all_off on one scene */
{
    unsigned b;
    for (b = 0; b < SCENE_BYTES; b++)
        otb_host_scene_put(t - 1, sc0, b, 0xFF);
    part_mark(vm, t);
}

static void fx_init(otc_vm_t *vm, unsigned t)                 /* trk.init: the factory effects the chooser holds */
{
    unsigned slot0;
    uint32_t d;
    for (slot0 = 0; slot0 < 2; slot0++) {
        d = otb_host_fx_default(slot0);
        if (d < chooser_norm(otb_host_fx_count(slot0)))
            fx_apply(vm, t, slot0, d);
    }
}

/* One track of a part operation (v = the evaluated arguments, already range-checked by call_op). */
static void part_track(otc_vm_t *vm, otc_frame_t *fr, const otc_op_t *op, const int32_t *v, unsigned t)
{
    int32_t lo = 0, hi = 0, p;
    unsigned x, sc;
    switch (op->handler) {
    case OTC_H_PAR_SET:
        if (!part_range(t, v[0], &lo, &hi)) {
            oor(vm, fr, op);                                  /* refused by the part: out of range, once per track */
            return;
        }
#ifdef OTC_SABOTAGE_PARCLAMP
        otb_host_par_set(t - 1, (uint32_t)v[0], v[1]);
#else
        otb_host_par_set(t - 1, (uint32_t)v[0], clampv(v[1], lo, hi));
#endif
        part_mark(vm, t);
        return;
    case OTC_H_LFO_DEST:                                      /* (x checked by part_op) */
        otb_host_lfo_set(t - 1, (uint32_t)(v[0] - 1), 0, (uint32_t)v[1]);
        part_mark(vm, t);
        return;
    case OTC_H_FX_TYPE:                                       /* (x and y checked by part_op) */
        fx_apply(vm, t, (unsigned)(v[0] - 1), (uint32_t)v[1]);
        return;
    case OTC_H_SCN_CLR:
        if (v[0] >= 1 && v[0] <= N_SCENES) {
            scene_fill(vm, t, (unsigned)(v[0] - 1));
        } else {
            for (sc = 0; sc < N_SCENES; sc++)
                scene_fill(vm, t, sc);
        }
        return;
    default:                                                  /* OTC_H_TRK_INIT, in a fixed order */
#ifndef OTC_SABOTAGE_INITORDER
        fx_init(vm, t);
#endif
        otb_host_trk_slot_init(t - 1);
        part_mark(vm, t);
        for (p = 0; p < 30; p++)
            if (part_range(t, p, &lo, &hi))
                otb_host_par_set(t - 1, (uint32_t)p, clampv(otb_host_param_default(t - 1, (uint32_t)p), lo, hi));
#ifdef OTC_SABOTAGE_INITORDER
        fx_init(vm, t);
#endif
        for (x = 0; x < 3; x++) {
            otb_host_lfo_set(t - 1, x, 0, 0);
            otb_host_lfo_set(t - 1, x, 1, 0);
        }
        for (sc = 0; sc < N_SCENES; sc++)
            scene_fill(vm, t, sc);
        return;
    }
}

/* The part operations; -1 when `op` is not one of them. */
static int part_op(otc_vm_t *vm, otc_frame_t *fr, const otc_op_t *op, const int32_t *v)
{
    const otc_ctx_t *ctx = top(fr);
    unsigned k;
    switch (op->handler) {
    case OTC_H_LFO_DEST:
        if (v[0] < 1 || v[0] > 3) {
            oor(vm, fr, op);
            return 0;
        }
        break;
    case OTC_H_FX_TYPE:
        if (v[0] < 1 || v[0] > 2 || v[1] < 0 || (uint32_t)v[1] >= chooser_norm(otb_host_fx_count((uint32_t)(v[0] - 1)))) {
            oor(vm, fr, op);
            return 0;
        }
        break;
    case OTC_H_SCN_CLR:
        if (v[0] < 0 || v[0] > N_SCENES) {
            oor(vm, fr, op);
            return 0;
        }
        break;
    case OTC_H_PAR_SET: case OTC_H_TRK_INIT:
        break;
    default:
        return -1;
    }
    for (k = 0; k < ctx->ntr; k++)
        part_track(vm, fr, op, v, ctx->tr[k]);
    return 0;
}

static int control_index(unsigned h, int32_t n)
{
    switch (h) {
    case OTC_H_PB_SETUP: case OTC_H_AMP_SETUP: case OTC_H_FX1_SETUP: case OTC_H_FX2_SETUP:
        return n >= 1 && n <= 6;
    case OTC_H_LFO_WAVE: case OTC_H_LFO_MULT: case OTC_H_LFO_TRIG:
        return n >= 1 && n <= 3;
    case OTC_H_LFOD_GET: case OTC_H_LFOD_SET: case OTC_H_LFOD_INTERP:
        return n >= 1 && n <= 16;
    case OTC_H_SCN_GET: case OTC_H_SCN_SET: case OTC_H_SCN_DEL: case OTC_H_SCN_COPY:
        return n >= 1 && n <= 16;
    default:
        return 1;
    }
}

static int control_set(otc_vm_t *vm, otc_frame_t *fr, const otc_op_t *op, const int32_t *v)
{
    const otc_ctx_t *ctx = top(fr);
    unsigned h = op->handler, k, t;
    int32_t n = op->nargs ? v[0] : 0, val = v[op->nargs];
    if (!control_index(h, n)) {
        oor(vm, fr, op);
        return 0;
    }
    if (h == OTC_H_TEMPO) {
        otb_host_tempo(clampv(val, 30, 300));
        return 0;
    }
    if (h == OTC_H_FILL) {
        return otb_host_control_action(vm, h, val != 0, 0);
    }
    for (k = 0; k < ctx->ntr; k++) {
        t = ctx->tr[k];
        if (h == OTC_H_TRK_SWING) {
            TRY(need_track(vm, (int32_t)t));
            blk_put(vm, t, 0x52, (uint8_t)(clampv(val, 50, 80) - 50));
        } else if (otb_host_control_write(h, t - 1, (uint32_t)n, val)) {
            if (h == OTC_H_TRK_TYPE)
                vm->mtype[t - 1] = (uint8_t)val;
            if (h != OTC_H_TRK_MUTE)
                part_mark(vm, t);
        } else {
            oor(vm, fr, op);
        }
    }
    return 0;
}

/* Machine reads never go through a setter. The host resolves current part and descriptor. */
static int control_op(otc_vm_t *vm, otc_frame_t *fr, const otc_op_t *op, const int32_t *v, int32_t *r)
{
    const otc_ctx_t *ctx = top(fr);
    unsigned h = op->handler, k, t = (unsigned)cur_track(vm, ctx), sc, b;
    int32_t lo = 0, hi = 0, n = op->nargs ? v[0] : 0, val;
    switch (h) {
    case OTC_H_PAT_CUR: case OTC_H_BANK_CUR: case OTC_H_PART_CUR: case OTC_H_TEMPO: case OTC_H_FILL:
    case OTC_H_TRK_SLOT: case OTC_H_TRK_LEVEL: case OTC_H_TRK_CUE: case OTC_H_TRK_MUTE:
    case OTC_H_PB_SETUP: case OTC_H_AMP_SETUP: case OTC_H_LFO_WAVE: case OTC_H_LFO_MULT: case OTC_H_LFO_TRIG:
    case OTC_H_LFOD_GET: case OTC_H_SCN_GET: case OTC_H_FX1_SETUP: case OTC_H_FX2_SETUP:
        if (!control_index(h, n)) { oor(vm, fr, op); return 0; }
        if (h == OTC_H_SCN_GET) {
            *r = otb_host_control_read(h, t - 1, (uint32_t)((n - 1) * 32 + v[1]));
            if (*r == 0xFF) *r = sat(otb_host_param_value(t - 1, (uint32_t)v[1]));
        } else *r = otb_host_control_read(h, t - 1, (uint32_t)n);
        return 0;
    case OTC_H_PAR_GET:
        if (!part_range(t, v[0], &lo, &hi)) { oor(vm, fr, op); return 0; }
        *r = sat(otb_host_param_value(t - 1, (uint32_t)v[0]));
        return 0;
    case OTC_H_PAR_MIN: case OTC_H_PAR_MAX:
        if (!part_range(t, v[0], &lo, &hi)) { oor(vm, fr, op); return 0; }
        *r = h == OTC_H_PAR_MIN ? lo : hi;
        return 0;
    case OTC_H_FX_COUNT:
        if (v[0] < 1 || v[0] > 2) { oor(vm, fr, op); return 0; }
        *r = (int32_t)chooser_norm(otb_host_fx_count((uint32_t)(v[0] - 1)));
        return 0;
    case OTC_H_PAT_GO:
        val = v[1] ? v[1] : otb_host_control_read(OTC_H_BANK_CUR, 0, 0);
        if (v[0] < 1 || v[0] > 16 || val < 1 || val > 16) { oor(vm, fr, op); return 0; }
        return otb_host_control_action(vm, h, v[0], val);
    case OTC_H_PART_GO: case OTC_H_PART_SAVE: case OTC_H_PART_RELOAD:
        val = v[0] ? v[0] : otb_host_control_read(OTC_H_PART_CUR, 0, 0);
        if (val < 1 || val > 4) { oor(vm, fr, op); return 0; }
        return otb_host_control_action(vm, h, val, 0);
    case OTC_H_FILL_ONCE:
        return otb_host_control_action(vm, h, 0, 0);
    case OTC_H_OS_ARM:
        for (k = 0; k < ctx->ntr; k++)
            TRY(otb_host_control_action(vm, h, ctx->tr[k] - 1, 0));
        return 0;
    case OTC_H_LFOD_SET: case OTC_H_LFOD_INTERP:
        if (!control_index(h, n)) { oor(vm, fr, op); return 0; }
        for (k = 0; k < ctx->ntr; k++) {
            t = ctx->tr[k];
            val = h == OTC_H_LFOD_SET ? clampv(v[1], -64, 63) : v[1] != 0;
            otb_host_control_write(h, t - 1, (unsigned)n, val);
            part_mark(vm, t);
        }
        return 0;
    case OTC_H_SCN_SET: case OTC_H_SCN_DEL: case OTC_H_SCN_COPY:
        if (!control_index(h, n) || (h == OTC_H_SCN_COPY && !control_index(h, v[1]))) {
            oor(vm, fr, op); return 0;
        }
        for (k = 0; k < ctx->ntr; k++) {
            t = ctx->tr[k];
            sc = (unsigned)(n - 1);
            if (h == OTC_H_SCN_COPY) {
                for (b = 0; b < SCENE_BYTES; b++) {
                    val = otb_host_control_read(OTC_H_SCN_GET, t - 1, sc * SCENE_BYTES + b);
                    otb_host_scene_put(t - 1, (uint32_t)(v[1] - 1), b, (uint32_t)val);
                }
            } else {
                val = 0xFF;
                if (h == OTC_H_SCN_SET) {
                    if (!part_range(t, v[1], &lo, &hi)) { oor(vm, fr, op); continue; }
                    val = clampv(v[2], lo, hi);
                }
                otb_host_scene_put(t - 1, sc, (uint32_t)v[1], (uint32_t)val);
            }
            part_mark(vm, t);
        }
        return 0;
    default:
        return -1;
    }
}

/* ---- sim.Run.call_op: one registry operation on evaluated arguments ---------------------------- */
static int call_op(otc_vm_t *vm, otc_frame_t *fr, const otc_op_t *op, int32_t *v, int32_t *r)
{
    unsigned i, h = op->handler;
    int bad = 0, e;
    otc_ctx_t *ctx = top(fr);
    *r = 0;
    if (op->flags & OTC_OF_VALUE) {                 /* a constant: no range check, no handler */
        *r = op->value;
        return 0;
    }
    for (i = 0; i < op->nargs; i++) {               /* the range of every argument (spec 8.1) */
        if (op->ak[i] == OTC_AK_PCT)
            v[i] = clampv(v[i], 0, 100);
        else if ((op->ak[i] == OTC_AK_RANGE || op->ak[i] == OTC_AK_COND)
                 && (v[i] < op->lo[i] || v[i] > op->hi[i]))
            bad = 1;
    }
    if (bad) {
        oor(vm, fr, op);
        return 0;
    }
    e = control_op(vm, fr, op, v, r);
    if (e >= 0)
        return e;
    if (lang_pure(h, v, r))
        return 0;
    e = lang_ctx(vm, ctx, h, v, r);
    if (e >= 0)
        return e;
    switch (h) {
    case OTC_H_PB: case OTC_H_LFO: case OTC_H_AMP: case OTC_H_FX1: case OTC_H_FX2:
        /* sim._pagebase: page n of PB / LFO / AMP / FX1 / FX2, an invalid one is -1 and counted */
        if (v[0] < 1 || v[0] > 6) {
            oor(vm, fr, op);
            *r = -1;
        } else {
            *r = (h == OTC_H_PB ? 0 : h == OTC_H_LFO ? 6 : h == OTC_H_AMP ? 12 : h == OTC_H_FX1 ? 18 : 24)
                 + v[0] - 1;
        }
        return 0;
    case OTC_H_LFO_SPD: case OTC_H_LFO_DEP:
        if (v[0] < 1 || v[0] > 3) {
            oor(vm, fr, op);
            *r = -1;
        } else {
            *r = (h == OTC_H_LFO_SPD ? 5 : 8) + v[0];
        }
        return 0;
    case OTC_H_FLIP: case OTC_H_O: case OTC_H_DRUNK:
        *r = state_read(vm, fr, h);
        return 0;
    case OTC_H_O_MIN: case OTC_H_O_MAX: case OTC_H_O_WRAP: case OTC_H_O_INC:
    case OTC_H_DRUNK_MIN: case OTC_H_DRUNK_MAX: case OTC_H_DRUNK_WRAP:
        vm->fs[fr->fn].touched = 1;
        state_setter(&vm->fs[fr->fn], h, v[0]);
        return 0;
    case OTC_H_TRK_CUR:
        *r = cur_track(vm, ctx);
        return 0;
    case OTC_H_PAT_LEN:
        *r = vm->length;
        return 0;
    case OTC_H_STEP_CUR:
        *r = ctx->step;
        return 0;
    default:
        if (op->flags & OTC_OF_CARD) {              /* the workspace is bound (i_op checked) */
            return card_op(vm, fr, op, v, r);
        }
        e = part_op(vm, fr, op, v);
        if (e >= 0)
            return e;
        e = track_op(vm, fr, op, v, r);
        return e >= 0 ? e : OTC_E_UNSUPPORTED;
    }
}

/* ---- context operations (sim.step_set, seed_all, seed_one) ------------------------------------- */
static void new_ref(otc_vm_t *vm, otc_ctx_t *c, unsigned k, int32_t n, uint32_t slot)
{
    vm->slot[slot] = otc_seed_state((uint32_t)n, k + 1);
    c->str[k] = (uint16_t)slot;
}

static int seed_all(otc_vm_t *vm, otc_frame_t *fr, int32_t n)       /* ctx.streams = new_streams(n) */
{
    otc_ctx_t *c = top(fr);
    uint32_t slot;
    unsigned k;
    for (k = 0; k < OTC_NSTREAMS; k++)
        c->str[k] = SLOT_NONE;                       /* drop the old references first */
    for (k = 0; k < OTC_NSTREAMS; k++) {
        TRY(alloc_slot(vm, &slot));
        new_ref(vm, c, k, n, slot);
    }
    return 0;
}

static int seed_one(otc_vm_t *vm, otc_frame_t *fr, unsigned k, int32_t n)   /* ctx.streams[k] = Stream */
{
    otc_ctx_t *c = top(fr);
    uint32_t slot;
    c->str[k] = SLOT_NONE;
    TRY(alloc_slot(vm, &slot));
    new_ref(vm, c, k, n, slot);
    return 0;
}

static int ctx_fixed(otc_vm_t *vm, otc_frame_t *fr, const otc_op_t *op, int32_t v)   /* VMRun.ctx_fixed */
{
    otc_ctx_t *c = top(fr);
    switch (op->handler) {
    case OTC_H_STEP:                                  /* sim.step_set */
        if (v < 1 || v > 64) {
            oor(vm, fr, op);
        } else {
            c->mlo = c->mhi = 0;
            mask_set(&c->mlo, &c->mhi, v);
            c->step = (uint8_t)v;
        }
        return 0;
    case OTC_H_SEED:       return seed_all(vm, fr, v);
    case OTC_H_RAND_SEED:  return seed_one(vm, fr, ST_RAND, v);
    case OTC_H_TOSS_SEED:  return seed_one(vm, fr, ST_TOSS, v);
    case OTC_H_PROB_SEED:  return seed_one(vm, fr, ST_PROB, v);
    case OTC_H_DRUNK_SEED: return seed_one(vm, fr, ST_DRUNK, v);
    case OTC_H_FILE_SEED:  return seed_one(vm, fr, ST_FILE, v);
    default:               return OTC_E_TABLE;
    }
}

/* ---- instructions: constants and variables (VMRun.i_push8 ... i_drop) -------------------------- */
static int i_ld(otc_vm_t *vm, otc_frame_t *fr, uint32_t v)
{
    int32_t x;
    if (v < 4)
        x = fr->loc[v];
    else if (v < 8)
        x = vm->g[v - 4];
    else
        x = v == 8 ? fr->i : fr->j;                 /* as vm.py: any v >= 9 reads j */
    return push(fr, x);
}

static int i_st(otc_vm_t *vm, otc_frame_t *fr, uint32_t v)
{
    int32_t x;
    if (v >= 8)
        return OTC_E_TABLE;
    TRY(pop(fr, &x));
    if (v < 4)
        fr->loc[v] = x;
    else
        vm->g[v - 4] = x;
    return 0;
}

static int i_lda(otc_vm_t *vm, otc_frame_t *fr, uint32_t n)
{
    if (n >= vm->fn[fr->fn].nargs)
        return OTC_E_TABLE;
    return push(fr, fr->args[n]);
}

static int i_ldp(otc_vm_t *vm, otc_frame_t *fr, uint32_t n)       /* the param's default (aux table) */
{
    const otc_fn_t *f = &vm->fn[fr->fn];
    const uint8_t *p = vm->tab + f->aux;
    uint32_t k;
    if (n >= f->nparams)
        return OTC_E_TABLE;
    /* 8 bytes per param. Not `8 * n`: gcc turns that into an index scale of 8, which the
     * assembler (-mcpu=5407) refuses; the empty asm keeps the loop from being folded back. */
    for (k = 0; k < n; k++) {
        p += 8;
        __asm__ __volatile__("" : "+r"(p));
    }
    if (fr == &vm->fr[0]) {
        if (n < OTC_MAX_PARAMS && vm->folder_path[n])
            return push(fr, -(int32_t)(n + 1));
        if (n < vm->npset)                         /* the page's value (otc_vm_set_params), clamped like --param */
            return push(fr, clampv(vm->pset[n], rds16(p + 4), rds16(p + 6)));
    }
    return push(fr, rds16(p + 2));
}

/* ---- operations (VMRun.i_op, i_opv, i_set) ----------------------------------------------------- */
static int i_op(otc_vm_t *vm, otc_frame_t *fr, uint32_t ns, uint32_t num)
{
    const otc_op_t *op = op_find(ns, num);
    int32_t v[4], r = 0;
    unsigned n, i;
    if (!op || (op->flags & OTC_OF_NOPLAIN))
        return OTC_E_TABLE;                         /* unknown, or "has no plain form" */
    if (!(op->flags & OTC_OF_SUPPORTED))
        return OTC_E_UNSUPPORTED;
    if ((op->flags & OTC_OF_CARD) && !vm->ws)
        return OTC_E_UNSUPPORTED;                   /* the card operations need the workspace */
    n = op->nargs;
    if (fr->sp < n)
        return OTC_E_TABLE;
    for (i = 0; i < n; i++) {
        v[i] = fr->stack[fr->sp - n + i];
        if (op->ak[i] == OTC_AK_LIST) {             /* VMRun.convert: a list must be a pool entry */
            uint32_t cnt;
            const uint8_t *items;
            TRY(pool_list(vm, v[i], &cnt, &items));
        }
    }
    fr->sp = (uint8_t)(fr->sp - n);
    TRY(charge(vm, op->cost));
    if (op->flags & OTC_OF_CTX)
        TRY(ctx_fixed(vm, fr, op, v[0]));
    else
        TRY(call_op(vm, fr, op, v, &r));
    if (op->flags & OTC_OF_RET)
        TRY(push(fr, r));
    return 0;
}

static int i_opv(otc_vm_t *vm, otc_frame_t *fr, uint32_t ns, uint32_t num, uint32_t form)
{
    const otc_op_t *op = op_find(ns, num);
    otc_ctx_t *c = top(fr);
    int32_t a, b, t;
    uint32_t k, m;
    if (!op)
        return OTC_E_TABLE;
    TRY(charge(vm, op->cost));
    if (op->handler == OTC_H_TRK) {
        if (form == 0) {                            /* cur */
            fr->plist[0] = (uint8_t)cur_track(vm, c);
            fr->nplist = 1;
        } else if (form == 255) {                   /* all */
            for (k = 0; k < 8; k++)
                fr->plist[k] = (uint8_t)(k + 1);
            fr->nplist = 8;
        } else {                                    /* sim.tracks_from: valid ones, no duplicate */
            if (fr->sp < form)
                return OTC_E_TABLE;
            fr->nplist = 0;
            for (k = 0; k < form; k++) {
                t = fr->stack[fr->sp - form + k];
                if (t < 1 || t > 8) {
                    oor(vm, fr, op);
                    continue;
                }
                for (m = 0; m < fr->nplist && fr->plist[m] != t; m++)
                    ;
                if (m == fr->nplist)
                    fr->plist[fr->nplist++] = (uint8_t)t;
            }
            fr->sp = (uint8_t)(fr->sp - form);
        }
        return 0;
    }
    if (op->handler == OTC_H_STEPS) {
        if (form == 0) {                            /* sim.steps_all */
            ctx_steps_range(c, 1, vm->length);
        } else if (form == 1) {                     /* sim.steps_pred: the predicate op is on the stack */
            const otc_op_t *p;
            if (fr->sp < 1)
                return OTC_E_TABLE;
            a = fr->stack[--fr->sp];
            p = (a >= 0 && a <= 0xFFFF) ? op_find((uint32_t)a >> 8, (uint32_t)a & 255) : 0;
            if (!p)
                return OTC_E_TABLE;
            TRY(steps_pred(vm, c, p->handler));
        } else if (form == 2) {                     /* sim.steps_range: absolute, replaces */
            if (fr->sp < 2)
                return OTC_E_TABLE;
            a = fr->stack[fr->sp - 2];
            b = fr->stack[fr->sp - 1];
            fr->sp -= 2;
            if (a < 1 || a > 64 || b < 1 || b > 64)
                oor(vm, fr, op);
            else
                ctx_steps_range(c, a < b ? a : b, a > b ? a : b);
        } else {
            return OTC_E_TABLE;
        }
        return 0;
    }
    return OTC_E_TABLE;                             /* "has no variable form" */
}

static int i_set(otc_vm_t *vm, otc_frame_t *fr, uint32_t ns, uint32_t num)
{
    const otc_op_t *op = op_find(ns, num);
    int32_t v[4];
    unsigned k, n;
    if (!op || !(op->flags & OTC_OF_SETTABLE))
        return OTC_E_TABLE;
    if (!(op->flags & OTC_OF_SUPPORTED))
        return OTC_E_UNSUPPORTED;
    TRY(charge(vm, 1));
    n = op->nargs + 1;
    if (n > 4 || fr->sp < n)
        return OTC_E_TABLE;
    for (k = 0; k < n; k++)
        v[k] = fr->stack[fr->sp - n + k];
    fr->sp = (uint8_t)(fr->sp - n);
    if (op->handler == OTC_H_FLIP || op->handler == OTC_H_O || op->handler == OTC_H_DRUNK) {
        vm->fs[fr->fn].touched = 1;                     /* fstate() creates the entry */
        state_set(&vm->fs[fr->fn], op->handler, v[0]);
        return 0;
    }
    if (op->handler >= OTC_H_O_MIN && op->handler <= OTC_H_DRUNK_WRAP) {
        vm->fs[fr->fn].touched = 1;
        state_setter(&vm->fs[fr->fn], op->handler, v[0]);
        return 0;
    }
    return control_set(vm, fr, op, v);
}

/* ---- forward jumps (VMRun.i_jmp ... i_skip) ---------------------------------------------------- */
static int i_every(otc_vm_t *vm, otc_frame_t *fr, uint32_t k, uint32_t off, int is_skip)
{
    const otc_fn_t *f = &vm->fn[fr->fn];
    int32_t n, c;
    uint32_t idx;
    int fires;
    TRY(pop(fr, &n));
    if (k >= f->nctr)
        return OTC_E_TABLE;
    vm->fs[fr->fn].touched = 1;
    idx = (uint32_t)f->cbase + k;
    /* sim.Run.every_fires: increment; fires when it reaches n, then restarts at 0; n < 1 never fires */
    c = (vm->ctr_set[idx] ? vm->ctr_val[idx] : 0) + 1;
    fires = 0;
    if (n >= 1 && c >= n) {
        vm->ctr_val[idx] = 0;
        fires = 1;
    } else {
        vm->ctr_val[idx] = n >= 1 ? c : 0;
    }
    vm->ctr_set[idx] = 1;
    if (fires == is_skip)                           /* every: jump unless it fires; skip: jump if it fires */
        fr->pc = (uint16_t)off;
    return 0;
}

static int i_prob(otc_vm_t *vm, otc_frame_t *fr, uint32_t off)
{
    int32_t p;
    TRY(pop(fr, &p));
    p = clampv(p, 0, 100);
    if (!(below(vm, top(fr), ST_PROB, 100) < p))    /* exactly one draw */
        fr->pc = (uint16_t)off;
    return 0;
}

/* ---- loops: the only backward jumps (VMRun.i_lbeg ... i_break) --------------------------------- */
static int ctl_new(otc_frame_t *fr, unsigned kind, uint32_t end, otc_ctl_t **out)    /* VMRun._frame */
{
    otc_ctl_t *c;
    if (fr->nctl >= OTC_MAX_CTL + 1)
        return OTC_E_TABLE;
    c = &fr->ctl[fr->nctl++];
    c->kind = (uint8_t)kind;
    c->end = (uint16_t)end;
    c->depth = fr->nctx;
    c->si = fr->i;
    c->sj = fr->j;
    c->cur = c->last = 0;
    c->step = 0;
    c->idx = c->n = 0;
    c->mlo = c->mhi = 0;
    *out = c;
    return 0;
}

static int ctl_top(otc_frame_t *fr, unsigned kind, otc_ctl_t **out)       /* VMRun._top */
{
    if (!fr->nctl || fr->ctl[fr->nctl - 1].kind != kind)
        return OTC_E_TABLE;
    *out = &fr->ctl[fr->nctl - 1];
    return 0;
}

static int scope_push(otc_frame_t *fr)             /* push a copy of the top scope */
{
    if (fr->nctx >= OTC_MAX_CTX + 1)
        return OTC_E_TABLE;
    ctx_copy(&fr->ctx[fr->nctx], &fr->ctx[fr->nctx - 1]);
    fr->nctx++;
    return 0;
}

static void scopes_to(otc_frame_t *fr, unsigned depth)       /* del fr.ctxs[depth:] */
{
    if (fr->nctx > depth)
        fr->nctx = (uint8_t)depth;
}

static int i_lbeg(otc_frame_t *fr, uint32_t end)
{
    int32_t a, b;
    otc_ctl_t *c;
    if (fr->sp < 2)
        return OTC_E_TABLE;
    a = fr->stack[fr->sp - 2];
    b = fr->stack[fr->sp - 1];
    fr->sp -= 2;
    TRY(ctl_new(fr, CK_L, end, &c));
    c->cur = a;
    c->last = b;
    c->step = a <= b;                               /* 1 upwards, 0 downwards */
    fr->j = fr->i;
    fr->i = a;
    return scope_push(fr);
}

static int i_lnext(otc_frame_t *fr, uint32_t back)
{
    otc_ctl_t *c;
    TRY(ctl_top(fr, CK_L, &c));
    scopes_to(fr, c->depth);
    if (c->cur == c->last) {
        fr->nctl--;
        fr->i = c->si;
        fr->j = c->sj;
        return 0;
    }
    c->cur += c->step ? 1 : -1;
    fr->i = c->cur;
    TRY(scope_push(fr));
    fr->pc = (uint16_t)back;
    return 0;
}

static int each_scope(otc_frame_t *fr, int32_t s)           /* VMRun._each_scope */
{
    otc_ctx_t *c;
    TRY(scope_push(fr));
    c = top(fr);
    c->mlo = c->mhi = 0;
    mask_set(&c->mlo, &c->mhi, s);
    c->step = (uint8_t)s;
    fr->i = s;
    return 0;
}

static int i_ebeg(otc_frame_t *fr, uint32_t end)
{
    otc_ctx_t *t = top(fr);
    otc_ctl_t *c;
    int32_t first = next_step(t->mlo, t->mhi, 0);
    if (!first) {
        fr->pc = (uint16_t)end;                     /* no steps: skip the body */
        return 0;
    }
    TRY(ctl_new(fr, CK_E, end, &c));
    c->mlo = t->mlo;
    c->mhi = t->mhi;
    c->cur = first;
    fr->j = fr->i;
    return each_scope(fr, first);
}

static int i_enext(otc_frame_t *fr, uint32_t back)
{
    otc_ctl_t *c;
    int32_t s;
    TRY(ctl_top(fr, CK_E, &c));
    scopes_to(fr, c->depth);
    s = next_step(c->mlo, c->mhi, c->cur);
    if (!s) {
        fr->nctl--;
        fr->i = c->si;
        fr->j = c->sj;
        return 0;
    }
    c->cur = s;
    TRY(each_scope(fr, s));
    fr->pc = (uint16_t)back;
    return 0;
}

static int pass_scope(otc_frame_t *fr, uint32_t track)      /* VMRun._pass_scope */
{
    otc_ctx_t *c;
    TRY(scope_push(fr));
    c = top(fr);
    c->ntr = 1;
    c->tr[0] = (uint8_t)track;
    return 0;
}

static int i_pass(otc_frame_t *fr, uint32_t end)
{
    otc_ctl_t *c;
    unsigned k;
    if (!fr->nplist) {
        fr->pc = (uint16_t)end;                     /* empty track list: skip the body */
        return 0;
    }
    TRY(ctl_new(fr, CK_T, end, &c));
    for (k = 0; k < fr->nplist; k++)
        c->items[k] = fr->plist[k];
    c->n = fr->nplist;
    fr->nplist = 0;                                 /* the list is consumed */
    return pass_scope(fr, c->items[0]);
}

static int i_pnext(otc_frame_t *fr, uint32_t back)
{
    otc_ctl_t *c;
    TRY(ctl_top(fr, CK_T, &c));
    scopes_to(fr, c->depth);
    c->idx++;
    if (c->idx >= c->n) {
        fr->nctl--;
        return 0;
    }
    TRY(pass_scope(fr, c->items[c->idx]));
    fr->pc = (uint16_t)back;
    return 0;
}

static int i_break(otc_frame_t *fr)
{
    while (fr->nctl) {
        otc_ctl_t *c = &fr->ctl[--fr->nctl];
        if (c->kind == CK_L || c->kind == CK_E) {   /* the innermost l / each: leave it */
            scopes_to(fr, c->depth);
            fr->i = c->si;
            fr->j = c->sj;
            fr->pc = c->end;
            return 0;
        }
    }
    return OTC_E_TABLE;                             /* "break outside a loop" */
}

/* ---- calls (VMRun.i_call, i_ret, i_stop) ------------------------------------------------------- */
static void frame_init(otc_frame_t *fr, unsigned fn)
{
    unsigned k;
    fr->fn = (uint8_t)fn;
    fr->pc = fr->ipc = 0;
    fr->sp = fr->nctl = fr->nplist = 0;
    fr->nctx = 1;
    fr->i = fr->j = 0;
    for (k = 0; k < OTC_MAX_LOCALS; k++)
        fr->loc[k] = 0;
    for (k = 0; k < OTC_MAX_ARGS; k++)
        fr->args[k] = 0;
}

static int i_call(otc_vm_t *vm, otc_frame_t *fr, uint32_t idx)
{
    otc_frame_t *nf;
    unsigned n, k;
    if (idx >= vm->nfunc)
        return OTC_E_TABLE;
    if (vm->nframes >= OTC_MAX_FRAMES)
        return OTC_E_TABLE;                         /* call depth beyond 8 */
    n = vm->fn[idx].nargs;
    if (fr->sp < n)
        return OTC_E_TABLE;
    nf = &vm->fr[vm->nframes];
    frame_init(nf, idx);
    for (k = 0; k < n; k++)
        nf->args[k] = fr->stack[fr->sp - n + k];
    fr->sp = (uint8_t)(fr->sp - n);
    ctx_copy(&nf->ctx[0], top(fr));                 /* a copy of the caller's top scope */
    if (vm->ws)
        flist_copy(vm->ws, vm->nframes, vm->nframes - 1);     /* ... and of the caller's file list */
    vm->nframes++;
    return 0;
}

static int leave(otc_vm_t *vm, int32_t val)         /* VMRun._leave */
{
    vm->nframes--;
    if (vm->nframes)
        return push(&vm->fr[vm->nframes - 1], val);
    return 0;
}

/* ---- one instruction (VMRun.step) -------------------------------------------------------------- */
static uint32_t ins_size(uint32_t opc)                /* 0 = unknown opcode */
{
    switch (opc) {
    case I_DROP: case I_BREAK: case I_RET: case I_STOP: case I_SCOPE: case I_UNSCOPE:
        return 1;
    case I_PUSH8: case I_PUSHC: case I_LD: case I_LDA: case I_LDP: case I_ST: case I_CALL:
    case I_DIR: case I_EXT: case I_MATCH:
        return 2;
    case I_MSG:
        return 3;
    case I_PUSH16: case I_PUSHK: case I_OP: case I_SET: case I_JMP: case I_JZ: case I_SEL:
    case I_PROB: case I_LBEG: case I_LNEXT: case I_EBEG: case I_ENEXT: case I_PASS: case I_PNEXT:
        return 3;
    case I_OPV: case I_EVERY: case I_SKIP:
        return 4;
    default:
        return 0;
    }
}

static int exec(otc_vm_t *vm, otc_frame_t *fr, uint32_t opc, const uint8_t *q)   /* q: the operands */
{
    int32_t v;
    switch (opc) {
    case I_PUSH8:  return push(fr, (int32_t)(int8_t)q[0]);
    case I_PUSH16: return push(fr, rds16(q));
    case I_PUSHK:  return push(fr, (int32_t)rd16(q));
    case I_PUSHC:  return push(fr, q[0]);
    case I_LD:     return i_ld(vm, fr, q[0]);
    case I_LDA:    return i_lda(vm, fr, q[0]);
    case I_LDP:    return i_ldp(vm, fr, q[0]);
    case I_ST:     return i_st(vm, fr, q[0]);
    case I_DROP:   return pop(fr, &v);
    case I_OP:     return i_op(vm, fr, q[0], q[1]);
    case I_OPV:    return i_opv(vm, fr, q[0], q[1], q[2]);
    case I_SET:    return i_set(vm, fr, q[0], q[1]);
    case I_JMP:    fr->pc = (uint16_t)rd16(q); return 0;
    case I_JZ:
        TRY(pop(fr, &v));
        if (v == 0)
            fr->pc = (uint16_t)rd16(q);
        return 0;
    case I_SEL:                                     /* lazy ?: its cost, jump if the condition is 0 */
        TRY(charge(vm, op_find(0, 0x18)->cost));
        TRY(pop(fr, &v));
        if (v == 0)
            fr->pc = (uint16_t)rd16(q);
        return 0;
    case I_PROB:   return i_prob(vm, fr, rd16(q));
    case I_EVERY:  return i_every(vm, fr, q[0], rd16(q + 1), 0);
    case I_SKIP:   return i_every(vm, fr, q[0], rd16(q + 1), 1);
    case I_LBEG:   return i_lbeg(fr, rd16(q));
    case I_LNEXT:  return i_lnext(fr, rd16(q));
    case I_EBEG:   return i_ebeg(fr, rd16(q));
    case I_ENEXT:  return i_enext(fr, rd16(q));
    case I_PASS:   return i_pass(fr, rd16(q));
    case I_PNEXT:  return i_pnext(fr, rd16(q));
    case I_BREAK:  return i_break(fr);
    case I_CALL:   return i_call(vm, fr, q[0]);
    case I_RET:
        TRY(pop(fr, &v));
        return leave(vm, v);
    case I_STOP:   return leave(vm, 0);
    case I_SCOPE:  return scope_push(fr);
    case I_UNSCOPE:
        if (fr->nctx < 2)
            return OTC_E_TABLE;                     /* unscope of the function's own context */
        fr->nctx--;
        return 0;
    case I_DIR:    return i_dir(vm, fr, q[0]);
    case I_EXT:    return i_ext(vm, fr, q[0]);
    case I_MATCH:  return i_match(vm, fr, q[0]);
    case I_MSG:    return i_msg(vm, fr, q[0], q[1]);
    default:       return OTC_E_UNSUPPORTED;
    }
}

static int step(otc_vm_t *vm)
{
    otc_frame_t *fr = &vm->fr[vm->nframes - 1];
    const otc_fn_t *f = &vm->fn[fr->fn];
    uint32_t pc = fr->pc, opc, size;
    int e;
    if (pc >= f->code_len)
        return OTC_E_TABLE;                         /* code ends inside / after the function */
    opc = vm->tab[f->code + pc];
    size = ins_size(opc);
    if (!size || pc + size > f->code_len)
        return OTC_E_TABLE;
    fr->ipc = (uint16_t)pc;
    fr->pc = (uint16_t)(pc + size);
    e = exec(vm, fr, opc, vm->tab + f->code + pc + 1);
    if (e)
        return e;
    vm->executed++;
    if (vm->executed > OTC_MAX_INSNS)
        return OTC_E_COST;
    if (vm->nframes) {                              /* declared depths (the check of VMRun.step) */
        otc_frame_t *t = &vm->fr[vm->nframes - 1];
        const otc_fn_t *g = &vm->fn[t->fn];
        if (t->sp > g->maxs || t->nctx > g->maxc || t->nctl > g->maxl)
            return OTC_E_TABLE;
    }
    return 0;
}

/* ---- public interface -------------------------------------------------------------------------- */
uint32_t otc_vm_sizeof(void)
{
    return (uint32_t)sizeof(otc_vm_t);
}

void otc_vm_bind(otc_vm_t *vm, unsigned track, uint8_t *trac, uint8_t *copy, unsigned mtype)
{
    if (track < 1 || track > 8)
        return;
    vm->trac[track - 1] = trac;
    vm->copy[track - 1] = copy;
    vm->mtype[track - 1] = (uint8_t)mtype;
    if (trac)
        vm->bound |= (uint8_t)(1u << (track - 1));
    else
        vm->bound &= (uint8_t)~(1u << (track - 1));
}

const char *otc_vm_msg(const otc_vm_t *vm)         /* the text of the last msg of the run ("" = none): drawn by the host */
{
    return vm->msg;
}

uint32_t otc_vm_touched(const otc_vm_t *vm)
{
    return vm->touched;
}

uint32_t otc_vm_part_touched(const otc_vm_t *vm)
{
    return vm->part_touched;
}

uint32_t otc_ws_size(void)
{
    return (uint32_t)sizeof(otc_ws_t);
}

OTC_NOIDIOM static void bags_reset(otc_ws_t *ws)
{
    unsigned k;
    for (k = 0; k < OTC_BAGS_MAX; k++)
        ws->bag[k].used = 0;
    ws->bagseq = 0;
}

void otc_vm_bind_work(otc_vm_t *vm, void *ws)
{
    if (ws && ws != (void *)vm->ws)
        bags_reset((otc_ws_t *)ws);                 /* a new area: its bags are not ours */
    vm->ws = (otc_ws_t *)ws;
}

uint32_t otc_vm_slots_touched(const otc_vm_t *vm)
{
    return vm->ws ? vm->ws->touched : 0;
}

void otc_vm_reset_state(otc_vm_t *vm)
{
    unsigned k;
    for (k = 0; k < 4; k++)
        vm->g[k] = 0;
    for (k = 0; k < OTC_MAX_PARAMS; k++) {
        vm->folder_path[k] = 0;
        vm->folder_len[k] = 0;
    }
    for (k = 0; k < OTC_MAX_FUNCS; k++) {
        otc_fstate_t *s = &vm->fs[k];
        s->flip = 0;
        s->o = 0;
        s->o_min = 0;
        s->o_max = 63;
        s->o_wrap = 1;
        s->o_inc = 1;
        s->drunk = 0;
        s->d_min = 0;
        s->d_max = 127;
        s->d_wrap = 0;
        s->touched = 0;
    }
    for (k = 0; k < OTC_MAX_COUNTERS; k++) {
        vm->ctr_val[k] = 0;
        vm->ctr_set[k] = 0;
    }
    if (vm->ws)
        bags_reset(vm->ws);                         /* the pick bags are persistent state too */
}

/* Validate the table (header, regions, per-function entries, caps) and index its functions. The
 * state is reset: counters are indexed by the table. 0, OTC_E_TABLE or OTC_E_CAPS. */
int otc_vm_load(otc_vm_t *vm, const uint8_t *otb, uint32_t len)
{
    uint32_t pool_off, pool_len, ftab_off, aux_off, aux_len, line_off, line_len, code_off, code_len;
    uint32_t k, nfunc, nctr_total = 0;
    otc_ws_t *ws = vm->ws;                          /* the work binding survives a load (bags are reset) */
    otc_memset(vm, 0, (uint32_t)sizeof *vm);
    vm->ws = ws;
    if (len < 32 || otb[0] != 'O' || otb[1] != 'T' || otb[2] != 'B' || otb[3] != 1)
        return OTC_E_TABLE;
    nfunc = rd16(otb + 6);
    pool_off = rd16(otb + 10);  pool_len = rd16(otb + 12);
    ftab_off = rd16(otb + 14);
    aux_off = rd16(otb + 16);   aux_len = rd16(otb + 18);
    line_off = rd16(otb + 20);  line_len = rd16(otb + 22);
    code_off = rd16(otb + 24);  code_len = rd16(otb + 26);
    if (rd16(otb + 28) != len)
        return OTC_E_TABLE;
    if (pool_off + pool_len > len || aux_off + aux_len > len || line_off + line_len > len
        || code_off + code_len > len || ftab_off + nfunc * 54 > len)
        return OTC_E_TABLE;
    if (nfunc > OTC_MAX_FUNCS)
        return OTC_E_CAPS;
    for (k = 0; k < nfunc; k++) {
        const uint8_t *e = otb + ftab_off + k * 54;
        otc_fn_t *f = &vm->fn[k];
        uint32_t fcode_off = rd16(e + 44), faux = rd16(e + 48), fline = rd16(e + 50);
        f->nargs = e[32];
        f->nparams = e[33];
        f->maxs = e[35];
        f->maxc = e[36];
        f->maxl = e[37];
        f->nctr = e[38];
        f->code_len = (uint16_t)rd16(e + 46);
        f->nlines = (uint16_t)rd16(e + 52);
        f->def_line = (uint16_t)rd16(e + 42);
        if (fcode_off + f->code_len > code_len
            || faux + (uint32_t)f->nparams * 8 + (uint32_t)f->nargs * 2 + (uint32_t)f->nctr * 4
                   + ((rd16(e + 18) & 0x80u) ? 2u : 0u) + ((e[23] & OTC_FL_CONFIRM_TEXT) ? 2u : 0u) > aux_len
            || fline + (uint32_t)f->nlines * 4 > line_len)
            return OTC_E_TABLE;
        if (f->nargs > OTC_MAX_ARGS || e[34] > OTC_MAX_LOCALS || f->maxs > OTC_MAX_STACK
            || f->maxc > OTC_MAX_CTX || f->maxl > OTC_MAX_CTL)
            return OTC_E_CAPS;
        if (f->maxc < 1)
            return OTC_E_TABLE;
        if (rd16(e + 18) & 0x80u) {                 /* PAGE: the label's pool offset ends the aux data */
            uint32_t po = rd16(otb + aux_off + faux + (uint32_t)f->nparams * 8 + (uint32_t)f->nargs * 2
                               + (uint32_t)f->nctr * 4), pn, pk;
            if (f->nargs || f->nparams > OTC_PAGE_PARAMS || po < 2 || po + 2 > pool_len)
                return OTC_E_TABLE;
            pn = otb[pool_off + po + 1];
            if (otb[pool_off + po] != P_STR || pn < 1 || pn > OTC_PAGE_LABEL || po + 2 + pn > pool_len)
                return OTC_E_TABLE;
            for (pk = 0; pk < pn; pk++)
                if (otb[pool_off + po + 2 + pk] < 0x20 || otb[pool_off + po + 2 + pk] > 0x7E)
                    return OTC_E_TABLE;
            f->page = (uint16_t)po;
        }
        if (e[23] & OTC_FL_CONFIRM_TEXT) {          /* the confirm text's pool offset, after the page label */
            uint32_t po = rd16(otb + aux_off + faux + (uint32_t)f->nparams * 8 + (uint32_t)f->nargs * 2
                               + (uint32_t)f->nctr * 4 + (f->page ? 2u : 0u)), pn, pk;
            if (po < 2 || po + 2 > pool_len || otb[pool_off + po] != P_STR)
                return OTC_E_TABLE;
            pn = otb[pool_off + po + 1];
            if (pn < 1 || pn > OTC_CONFIRM_TEXT || po + 2 + pn > pool_len)
                return OTC_E_TABLE;
            for (pk = 0; pk < pn; pk++)
                if (otb[pool_off + po + 2 + pk] < 0x20 || otb[pool_off + po + 2 + pk] > 0x7E)
                    return OTC_E_TABLE;
        }
        f->code = code_off + fcode_off;
        f->aux = aux_off + faux;
        f->ctr = f->aux + (uint32_t)f->nparams * 8 + (uint32_t)f->nargs * 2;
        f->lines = line_off + fline;
        f->cbase = (uint16_t)nctr_total;
        nctr_total += f->nctr;
        if (nctr_total > OTC_MAX_COUNTERS)
            return OTC_E_CAPS;
    }
    vm->tab = otb;
    vm->tablen = len;
    vm->pool_off = pool_off;
    vm->pool_len = pool_len;
    vm->nfunc = (uint16_t)nfunc;
    vm->nctr_total = (uint16_t)nctr_total;
    otc_vm_reset_state(vm);
    vm->loaded = 1;
    return 0;
}

uint32_t otc_vm_fn_confirm(const otc_vm_t *vm, uint32_t fn, const char **text)
{
    const otc_fn_t *f;
    uint32_t po;
    if (!vm->loaded || fn >= vm->nfunc)
        return 0;
    f = &vm->fn[fn];
    if (!(vm->tab[rd16(vm->tab + 14) + fn * 54 + 23] & OTC_FL_CONFIRM_TEXT))   /* the entry's flags (validated at load) */
        return 0;
    po = rd16(vm->tab + f->ctr + (uint32_t)f->nctr * 4 + (f->page ? 2u : 0u));
    if (text)
        *text = (const char *)(vm->tab + vm->pool_off + po + 2);
    return vm->tab[vm->pool_off + po + 1];
}

uint32_t otc_vm_fn_input_sample(const otc_vm_t *vm, uint32_t fn)
{
    if (!vm->loaded || fn >= vm->nfunc)
        return 0;
    return (vm->tab[rd16(vm->tab + 14) + fn * 54 + 23] & OTC_FL_INPUT_SAMPLE) ? 1u : 0u;
}

uint32_t otc_vm_fn_page(const otc_vm_t *vm, uint32_t fn, const char **label)
{
    if (!vm->loaded || fn >= vm->nfunc || !vm->fn[fn].page)
        return 0;
    if (label)
        *label = (const char *)(vm->tab + vm->pool_off + vm->fn[fn].page + 2);
    return vm->tab[vm->pool_off + vm->fn[fn].page + 1];
}

int otc_vm_fn_param(const otc_vm_t *vm, uint32_t fn, uint32_t k, otc_vm_param_t *out)
{
    const uint8_t *p;
    uint32_t j;
    int32_t off;
    const uint8_t *s;
    uint32_t n;
    const otc_fn_t *f;
    uint8_t flags;
    if (!vm->loaded || fn >= vm->nfunc || k >= vm->fn[fn].nparams)
        return 0;
    f = &vm->fn[fn];
    p = vm->tab + f->aux;
    for (j = 0; j < k; j++) {                       /* 8 bytes per param, no index scale of 8 (see i_ldp) */
        p += 8;
        __asm__ __volatile__("" : "+r"(p));
    }
    off = (int32_t)rd16(p);
    if (pool_str(vm, off, &s, &n))
        return 0;
    out->name = (const char *)s;
    out->name_len = n;
    out->def = (int16_t)rds16(p + 2);
    out->lo = (int16_t)rds16(p + 4);
    out->hi = (int16_t)rds16(p + 6);
    out->kind = OTC_PK_INT;
    out->nlabels = 0;
    out->folder_path = 0;
    out->folder_len = 0;
    for (j = 0; j < 16; j++) {
        out->labels[j] = 0;
        out->label_lens[j] = 0;
    }
    flags = vm->tab[rd16(vm->tab + 14) + fn * 54 + 23];
    if (flags & OTC_FL_TYPED_PARAMS) {
        uint32_t aux_base = (uint32_t)f->nparams * 8 + (uint32_t)f->nargs * 2 + (uint32_t)f->nctr * 4
                          + (f->page ? 2u : 0u) + ((flags & OTC_FL_CONFIRM_TEXT) ? 2u : 0u);
        const uint8_t *tp = vm->tab + f->aux + aux_base;
        uint16_t mask = rd16(tp);
        tp += 2;
        out->kind = (uint8_t)((mask >> (2u * k)) & 3u);
        for (j = 0; j < f->nparams; j++) {
            uint8_t jk = (uint8_t)((mask >> (2u * j)) & 3u);
            if (jk == OTC_PK_FOLDER) {
                uint32_t poff = rd16(tp);
                tp += 2;
                if (j == k) {
                    if (!pool_str(vm, poff, &s, &n)) {
                        out->folder_path = (const char *)s;
                        out->folder_len = n;
                    }
                }
            }
        }
        for (j = 0; j < f->nparams; j++) {
            uint8_t jk = (uint8_t)((mask >> (2u * j)) & 3u);
            if (jk == OTC_PK_OPTION) {
                uint16_t cnt = rd16(tp);
                uint16_t l;
                tp += 2;
                if (j == k)
                    out->nlabels = (uint8_t)cnt;
                for (l = 0; l < cnt; l++) {
                    uint32_t loff = rd16(tp);
                    tp += 2;
                    if (j == k && l < 16) {
                        if (!pool_str(vm, loff, &s, &n)) {
                            out->labels[l] = (const char *)s;
                            out->label_lens[l] = (uint8_t)n;
                        }
                    }
                }
            }
        }
    }
    return 1;
}

void otc_vm_set_params(otc_vm_t *vm, const int16_t *vals, uint32_t n)
{
    uint32_t k;
    if (!vals || n > OTC_MAX_PARAMS)
        n = vals ? OTC_MAX_PARAMS : 0;
    for (k = 0; k < n; k++)
        vm->pset[k] = vals[k];
    vm->npset = (uint8_t)n;
}

void otc_vm_set_param_folder(otc_vm_t *vm, uint32_t k, const char *path, uint32_t len)
{
    if (!vm || k >= OTC_MAX_PARAMS)
        return;
    vm->folder_path[k] = path;
    vm->folder_len[k] = (uint8_t)(path ? (len > 255u ? 255u : len) : 0u);
}

/* a press starts with the root frame's empty list, no warning, no refusal, an empty working set; the
 * bags stay (they are persistent state) */
OTC_NOIDIOM static void ws_press(otc_ws_t *ws)
{
    flist_empty(&ws->fl[0]);
    ws->touched = 0;
    ws->nwarn = ws->warn_ovf = ws->nref = ws->ref_ovf = ws->nwork = 0;
    otb_host_cat_clr();
#if defined(OTC_SABOTAGE_BAG) && OTC_SABOTAGE_BAG == 1
    bags_reset(ws);                                 /* wrong on purpose: the bags do not persist */
#endif
}

/* sim.simulate + VMRun.run: one press of function `func_index` (a function without argument). */
int otc_vm_run(otc_vm_t *vm, uint16_t func_index, uint16_t seed, uint8_t host_track, uint8_t length)
{
    otc_frame_t *fr;
    otc_ctx_t *c;
    unsigned k;
    int e = 0;
    vm->error = 0;
    vm->cost = vm->executed = 0;
    vm->noor = vm->overflow = 0;
    vm->nskip = vm->skip_ovf = vm->npick = vm->pick_ovf = 0;
    vm->touched = 0;
    vm->msg[0] = 0;                                 /* no msg in this run: the end box says DONE */
    vm->part_touched = 0;
    vm->pending_trac = vm->pending_part = 0;
    vm->nframes = 0;
    for (k = 0; k < OTC_NSTREAMS; k++)
        vm->draws[k] = 0;
    if (!vm->loaded || func_index >= vm->nfunc || vm->fn[func_index].nargs != 0
        || host_track < 1 || host_track > 8) {
        vm->error = OTC_E_ARG;
        return OTC_E_ARG;
    }
    vm->host_track = host_track;
    vm->length = (uint8_t)clampv(length, 1, 64);
    fr = &vm->fr[0];
    frame_init(fr, func_index);
    c = &fr->ctx[0];                                /* the root context: host track, steps 1..length, */
    c->ntr = 1;                                     /* streams seeded by the press (spec 5.3.2) */
    c->tr[0] = host_track;
    c->step = 0;
    ctx_steps_range(c, 1, vm->length);
    for (k = 0; k < OTC_NSTREAMS; k++) {
        c->str[k] = (uint16_t)k;
        vm->slot[k] = otc_seed_state(seed, k + 1);
    }
    vm->nframes = 1;
    if (vm->ws)
        ws_press(vm->ws);
    while (vm->nframes && !e) {
        otb_host_refresh(vm);
        e = step(vm);
    }
    vm->error = e;
    return e;
}

/* card / slot tail of the export record (layout: otc_vm.h) */
static uint32_t pack_bytes(int32_t *out, uint32_t max_words, uint32_t n, const uint8_t *p, uint32_t len)
{
    uint32_t k, w, j;
    for (k = 0; k < len; k += 4) {                  /* four bytes per word, big-endian, zero padded */
        w = 0;
        for (j = 0; j < 4; j++)
            w = (w << 8) | (k + j < len ? p[k + j] : 0u);
        if (n < max_words)
            out[n] = (int32_t)w;
        n++;
    }
    return n;
}

static uint32_t ws_export(const otc_vm_t *vm, int32_t *out, uint32_t max_words, uint32_t n)
{
    const otc_ws_t *ws = vm->ws;
    const otc_bag_t *b;
    uint32_t k, m, cnt = 0, last = 0, pick;
#define W(val) do { if (n < max_words) out[n] = (int32_t)(val); n++; } while (0)
    W(ws ? 1 : 0);
    if (!ws)
        return n;
    W(ws->touched);
    W(ws->nwarn);
    W(ws->warn_ovf);
    for (k = 0; k < ws->nwarn; k++) {
        W(ws->warn[k].kind);
        W(ws->warn[k].n);
        W(ws->warn[k].cap);
        W(ws->warn[k].pathlen);
        W(((uint32_t)ws->warn[k].pathlen + 3) >> 2);
        n = pack_bytes(out, max_words, n, ws->warn[k].path, ws->warn[k].pathlen);
    }
    W(ws->nwork);
    for (k = 0; k < ws->nwork; k++)
        W(ws->work[k]);
    for (k = 0; k < OTC_BAGS_MAX; k++)
        cnt += ws->bag[k].used;
    W(cnt);
    for (m = 0; m < cnt; m++) {                     /* oldest first: by insertion */
        pick = OTC_BAGS_MAX;
        for (k = 0; k < OTC_BAGS_MAX; k++)
            if (ws->bag[k].used && ws->bag[k].seq > last
                && (pick == OTC_BAGS_MAX || ws->bag[k].seq < ws->bag[pick].seq))
                pick = k;
        b = &ws->bag[pick];
        last = b->seq;
        W(b->fn);
        W(b->hash);
        W(b->n);
        W(b->nrest);
        for (k = 0; k < b->nrest; k += 2)
            W(((uint32_t)b->rest[k] << 16) | (k + 1 < b->nrest ? b->rest[k + 1] : 0u));
    }
    W(ws->nref);
    W(ws->ref_ovf);
    for (k = 0; k < ws->nref; k++) {
        W(ws->ref[k].fn);
        W(ws->ref[k].op);
        W(ws->ref[k].line);
        W(ws->ref[k].slot);
        W(ws->ref[k].len);
        W(((uint32_t)ws->ref[k].len + 3) >> 2);
        n = pack_bytes(out, max_words, n, ws->ref[k].path, ws->ref[k].len);
    }
#undef W
    return n;
}

uint32_t otc_vm_export(const otc_vm_t *vm, int32_t *out, uint32_t max_words)
{
    uint32_t n = 0, k, m;
#define W(val) do { if (n < max_words) out[n] = (int32_t)(val); n++; } while (0)
    W(vm->error);
    W(vm->cost);
    W(vm->executed);
    for (k = 0; k < OTC_NSTREAMS; k++)
        W(vm->draws[k]);
    for (k = 0; k < 4; k++)
        W(vm->g[k]);
    W(vm->noor);
    W(vm->overflow);
    for (k = 0; k < vm->noor; k++) {
        W(vm->oor_fn[k]);
        W(vm->oor_op[k]);
        W(vm->oor_line[k]);
    }
    W(vm->nfunc);
    for (k = 0; k < vm->nfunc; k++) {
        const otc_fstate_t *s = &vm->fs[k];
        W(s->touched);
        W(s->flip);
        W(s->o);
        W(s->o_min);
        W(s->o_max);
        W(s->o_wrap);
        W(s->o_inc);
        W(s->drunk);
        W(s->d_min);
        W(s->d_max);
        W(s->d_wrap);
        W(vm->fn[k].nctr);
        for (m = 0; m < vm->fn[k].nctr; m++) {
            W(vm->ctr_set[vm->fn[k].cbase + m]);
            W(vm->ctr_val[vm->fn[k].cbase + m]);
        }
    }
    W(vm->nskip);
    W(vm->skip_ovf);
    for (k = 0; k < vm->nskip; k++) {
        W(vm->skip_fn[k]);
        W(vm->skip_op[k]);
        W(vm->skip_line[k]);
    }
    W(vm->npick);
    W(vm->pick_ovf);
    for (k = 0; k < vm->npick; k++) {
        W(vm->pick_fn[k]);
        W(vm->pick_op[k]);
        W(vm->pick_line[k]);
        W(vm->pick_trk[k]);
    }
    W(vm->touched);
    W(vm->part_touched);
#undef W
    return ws_export(vm, out, max_words, n);
}
