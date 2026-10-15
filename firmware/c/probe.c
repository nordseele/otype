/* OType: the C-to-DRAM-unit proof.
 *
 * This file is compiled for ColdFire V4e by build/cunits.py into the unit
 * otype_c.s, linked next to the unit's assembly part. Policy for C in the
 * unit: freestanding, <stdint.h> only; no libc, no libgcc helpers, no
 * .data/.bss/.comm (the runtime is one flat binary); only constants and
 * code; the only external symbol is what bridge.S defines (otb_host_add).
 * Stack use stays small (-Wstack-usage=256). The PRNG is the reference of
 * modules/otype/sim.py, bit for bit.
 */
#include <stdint.h>

extern uint32_t otb_host_add(uint32_t a, uint32_t b);   /* implemented in asm */

static uint32_t seed_state(uint32_t n, uint32_t k)
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

static uint32_t xorshift32(uint32_t x)
{
#ifdef OTC_SABOTAGE
    x ^= x << 12;                       /* wrong on purpose: must-fail check */
#else
    x ^= x << 13;
#endif
    x ^= x >> 17;
    x ^= x << 5;
    return x;
}

uint32_t otc_probe(uint32_t n, uint32_t k, uint32_t count, uint32_t *out)
{
    uint32_t x, i;
    if (count > 64u)
        count = 64u;
    x = seed_state(n, k);
    for (i = 0; i < count; i++) {
        x = xorshift32(x);
        out[i] = x;
    }
    return otb_host_add(x, count);
}
