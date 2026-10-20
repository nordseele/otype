/* Stock 1.40C imports for the v1 controls. Descriptors stay in the installed OS. */
#define M8(a) (*(volatile uint8_t *)(uintptr_t)(a))
#define M16(a) (*(volatile uint16_t *)(uintptr_t)(a))
#define M32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define FN0(a) ((void (*)(void))(uintptr_t)(a))
#define FN1(a) ((void (*)(uint32_t))(uintptr_t)(a))
#define FN2(a) ((void (*)(uint32_t, uint32_t))(uintptr_t)(a))
#define FN3(a) ((void (*)(uint32_t, uint32_t, uint32_t))(uintptr_t)(a))

static uint8_t *machine_bank(void)
{
    return (uint8_t *)(uintptr_t)M32(0x46c82456);
}

static uint8_t *machine_part(void)
{
    return machine_bank() + 0x8ed80 + M8(0x100b14cf) * 0x18b2;
}

void otb_host_refresh(otc_vm_t *vm)
{
    uint8_t *bank = machine_bank(), *pt = machine_part();
    uint32_t off = M8(0x100b14d0) * 0x8ed8, t, len;
    for (t = 0; t < 8; t++)
        otc_vm_bind(vm, (uint8_t)(t + 1), bank + off + t * 0x91a,
                    (uint8_t *)(uintptr_t)(0x1001614e + off + t * 0x91a), pt[0x22 + t]);
    len = bank[off + 0x8e55] ? bank[off + (vm->host_track - 1) * 0x91a + 0x50] : bank[off + 0x8e53];
    vm->length = (uint8_t)(len >= 1 && len <= 64 ? len : 64);
}

void otb_host_commit(otc_vm_t *vm)
{
    if (vm->pending_trac) {
        M32((uintptr_t)machine_bank() + 0x9b332) = 1;
        M32(0x100f8598) = 1;
        FN0(0x40027e00)();
        FN0(0x400339d8)();
    }
    if (vm->pending_part) {
        FN2(0x40029a4c)((uint32_t)(uintptr_t)machine_part(), M8(0x100b14cf));
        FN0(0x40027e00)();
        FN1(0x40093468)(0xffffffffu);
    }
    if (vm->pending_trac || vm->pending_part)
        FN1(0x4009da20)(0xffffffffu);
    vm->pending_trac = vm->pending_part = 0;
}

static uint32_t machine_offset(uint32_t h, uint32_t t, uint32_t n)
{
    switch (h) {
    case OTC_H_TRK_TYPE: return 0x22 + t;
    case OTC_H_TRK_SLOT: return 0x2ca + t * 5 + machine_part()[0x22 + t];
    case OTC_H_TRK_LEVEL: return 0x12 + t * 2;
    case OTC_H_TRK_CUE: return 0x13 + t * 2;
    case OTC_H_PB_SETUP: return 0x1da + t * 30 + machine_part()[0x22 + t] * 6 + n - 1;
    case OTC_H_AMP_SETUP: return 0x2f8 + t * 30 + n - 1;
    case OTC_H_FX1_SETUP: return 0x2fe + t * 30 + n - 1;
    case OTC_H_FX2_SETUP: return 0x304 + t * 30 + n - 1;
    case OTC_H_LFO_WAVE: return 0x2f5 + t * 30 + n - 1;
    case OTC_H_LFO_MULT: return 0x30a + t * 30 + n - 1;
    case OTC_H_LFO_TRIG: return 0x30d + t * 30 + n - 1;
    case OTC_H_LFOD_GET: case OTC_H_LFOD_SET: return 0x1702 + t * 16 + n - 1;
    case OTC_H_REC_SRC: return 0x602 + t * 12 + (n == 1 ? 0 : n == 2 ? 1 : 4);
    case OTC_H_REC_LEN: return 0x602 + t * 12 + 2;
    case OTC_H_REC_TRIG: return 0x602 + t * 12 + 3;      /* RECORDING SETUP 1, in the page's own order */
    case OTC_H_REC_LOOP: return 0x602 + t * 12 + 5;
    case OTC_H_SCN_GET: {
        volatile uint32_t scene_off = (n / 32) * 256; /* keep GCC from emitting a ColdFire-invalid index scale of 8 */
        return 0x662 + scene_off + t * 32 + n % 32;
    }
    default: return 0;
    }
}

int32_t otb_host_control_read(uint32_t h, uint32_t t, uint32_t n)
{
    switch (h) {
    case OTC_H_PAT_CUR: return M8(0x100b14d0) + 1;
    case OTC_H_BANK_CUR: return M8(0x100b14ce) + 1;
    case OTC_H_PART_CUR: return M8(0x100b14cf) + 1;
    case OTC_H_TEMPO: return ((int32_t (*)(void))(uintptr_t)0x4009c5b8)() / 24;
    case OTC_H_FILL: return M8(0x46107958) != 0;
    case OTC_H_TRK_MUTE: return (int32_t)((M32(0x80000008) >> (8 + t)) & 1);
    case OTC_H_LFOD_GET: return (int8_t)machine_part()[machine_offset(h, t, n)];
    case OTC_H_TRK_SLOT: return machine_part()[machine_offset(h, t, n)] + 1;
    default: return machine_part()[machine_offset(h, t, n)];
    }
}

static void machine_store(uint32_t off, int32_t v)
{
    machine_part()[off] = (uint8_t)v;
    M8(0x100a4ece + M8(0x100b14cf) * 0x18b2 + off) = (uint8_t)v;
}

int32_t otb_host_control_write(uint32_t h, uint32_t t, uint32_t n, int32_t v)
{
    uint8_t *pt = machine_part(), *desc;
    uint32_t off = machine_offset(h, t, n), old, page, slot;
    int32_t lo, count;
    if (t >= 8) return 0;
    switch (h) {
    case OTC_H_TRK_TYPE:
        if (v < 0 || v > 4 || (v == 3 && (t == 0 || t == 4)) || (t == 7 && M8(0x80000034))) return 0;
        old = pt[off];
        machine_store(off, v);
        FN3(0x400972fc)(M8(0x100b14cf), t, old);
        if (v == 0) FN1(0x40093468)(t);
        FN1(0x4009da20)(t);
        return 1;
    case OTC_H_TRK_SLOT:
        if (v < 1 || v > 128 || pt[0x22 + t] > 1) return 0;
        /* A Flex assignment consumes only a slot already loaded into RAM. */
        if (pt[0x22 + t] == 1 && (!M32(0x46105408) || !M8(0x100b14f0 + (v - 1) * 0x448) || M32(0x46c922c4 + (v - 1) * 0x2c + 8))) return 0;
        machine_store(off, v - 1);
        M8(0x8000082f + t * 72) = (uint8_t)(v - 1);
        M32(0x80001338 + t * 4) &= 0x7fffffffu;
        return 1;
    case OTC_H_TRK_MUTE:
        if (otb_host_control_read(h, t, 0) != (v != 0)) FN2(0x4007d7b0)(t, 0);
        return 1;
    case OTC_H_TRK_LEVEL: case OTC_H_TRK_CUE:
        v = clampv(v, 0, 127);
        machine_store(off, v);
        M8(0x80000c50 + t * 2 + (h == OTC_H_TRK_CUE)) = (uint8_t)v;
        if (h == OTC_H_TRK_LEVEL) FN3(0x40033e3c)(t, 0x2e, (uint32_t)v);
        else ((void (*)(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t))(uintptr_t)0x400053d8)(0x43, t, 1, (uint32_t)v, 0, 0);
        return 1;
    case OTC_H_LFOD_SET:
        machine_store(off, clampv(v, -64, 63));
        return 1;
    case OTC_H_LFOD_INTERP:
        off = 0x1782 + t * 2;
        old = M16((uintptr_t)pt + off);
        old = v ? old | (1u << (n - 1)) : old & ~(1u << (n - 1));
        M16((uintptr_t)pt + off) = (uint16_t)old;
        M16(0x100a4ece + M8(0x100b14cf) * 0x18b2 + off) = (uint16_t)old;
        return 1;
    case OTC_H_REC_SRC:
        if (n < 1 || n > 3) return 0;
        if (n == 1 && (v < 0 || v > 4)) return 0;
        if (n == 2 && (v < 0 || v > 4)) return 0;
        if (n == 3 && (v < 0 || v > 10)) return 0;
        machine_store(off, v);
        return 1;
    case OTC_H_REC_LEN:
        /* v = RLEN as the page shows it: 1..63 steps, 64 = MAX. The part holds it 0-based
         * (0..63 = 1..64 steps, 64 = MAX: the descriptor has 65 values). */
        if (v < 1 || v > 64) return 0;
        machine_store(off, v >= 64 ? 64 : (uint32_t)v - 1);
        return 1;
    case OTC_H_REC_TRIG:
        if (v < 0 || v > 2) return 0;                  /* ONE, ONE-2, HOLD */
        machine_store(off, v);
        return 1;
    case OTC_H_REC_LOOP:
        if (v < 0 || v > 1) return 0;
        machine_store(off, v);
        return 1;
    default:
        page = h == OTC_H_PB_SETUP ? 0 : h == OTC_H_AMP_SETUP ? 2 : h == OTC_H_FX1_SETUP ? 3 : h == OTC_H_FX2_SETUP ? 4 : 1;
        desc = ((uint8_t *(*)(uint32_t, uint32_t))(uintptr_t)0x40031da4)(t, page);
        if (!desc) return 0;
        slot = h == OTC_H_LFO_WAVE ? 7 : h == OTC_H_LFO_MULT ? 8 : h == OTC_H_LFO_TRIG ? 9 : n + 5;
        if (slot >= 6 && slot <= 11 && desc[0x14 + slot * 6] == '-') return 0;
        lo = (int32_t)M32((uintptr_t)desc + 0x6a + slot * 4);
        count = (int32_t)M32((uintptr_t)desc + 0x9a + slot * 4);
        if (count < 1) return 0;
        machine_store(off, clampv(v, lo, lo + count - 1));
        return 1;
    }
}

int32_t otb_host_control_action(otc_vm_t *vm, uint32_t h, int32_t a, int32_t b)
{
    uint32_t p, t, off;
    uint8_t old[8];
    switch (h) {
    case OTC_H_PAT_GO:
        otb_host_commit(vm);
        FN2(0x400a1030)((uint32_t)(b - 1), (uint32_t)(a - 1));
        break;
    case OTC_H_PART_GO:
        otb_host_commit(vm);
        p = (uint32_t)(a - 1);
        for (t = 0; t < 8; t++) old[t] = machine_part()[0x22 + t];
        off = M8(0x100b14d0) * 0x8ed8 + 0x8e57;
        machine_bank()[off] = (uint8_t)p;
        M8(0x1001614e + off) = (uint8_t)p;
        M8(0x80000003) = M8(0x100b14cf) = (uint8_t)p;
        FN2(0x40009848)(M8(0x80000002), p);
        for (t = 0; t < 8; t++) FN3(0x400972fc)(p, t, old[t]);
        M32((uintptr_t)machine_bank() + 0x9b332) = M32(0x100f8598) = 1;
        FN0(0x40027e00)();
        FN1(0x40093468)(0xffffffffu);
        FN1(0x4009da20)(0xffffffffu);
        break;
    case OTC_H_PAT_COPY:
        /* [FUNCTION]+[STOP] without the clipboard: the undo snapshot first (0x460bf218, nature 14), then the stock
         * writer 0x4002b9b0(src, dst), which copies the 0x8ed8 bytes into the bank AND its SRAM copy, sets the
         * dirty flags, re-applies the selected pattern's part and fixes the tempo. The source is read in the bank. */
        otb_host_commit(vm);
        FN2(0x40026ef0)(0x11u, (uint32_t)(b - 1));
        FN2(0x4002b9b0)((uint32_t)(uintptr_t)(machine_bank() + (uint32_t)(a - 1) * 0x8ed8), (uint32_t)(b - 1));
        break;
    case OTC_H_PART_SAVE: case OTC_H_PART_RELOAD:
        otb_host_commit(vm);
        if (h == OTC_H_PART_SAVE) FN1(0x4004a908)((uint32_t)(a - 1));
        else if (!((uint32_t (*)(uint32_t))(uintptr_t)0x4004aab4)((uint32_t)(a - 1))) return OTC_E_ARG;
        break;
    case OTC_H_FILL:
        FN0(a ? 0x400a53fc : 0x400a5408)();
        break;
    case OTC_H_FILL_ONCE:
        FN0(0x400a5460)();
        break;
    case OTC_H_OS_ARM:
        /* 0x46c7ff64 bit t = the sample one-shots of track t, bit t+8 = its recorder one-shots (the
         * checks at 0x4000b934 / 0x40030632); [STOP]+[STOP] re-arms both (manual 12.4.5). */
        M16(0x46c7ff64) &= (uint16_t)~((1u << a) | (1u << (a + 8)));
        FN1(0x4004d948)(0xffffffffu);
        break;
    case OTC_H_REC_ARM: {
        /* the panel's own word: 0x8010 | REC1 (0x1000), REC2 (0x2000), REC3 (0x4000) -- 0x9010 for AB.
         * The stock routine schedules the start itself, so QREC still applies. */
        uint32_t word = 0x8010u | ((uint32_t)(b & 1) ? 0x1000u : 0u)
                      | ((uint32_t)(b & 2) ? 0x2000u : 0u) | ((uint32_t)(b & 4) ? 0x4000u : 0u);
        FN3(0x40005178)((uint32_t)a, word, 1);
        break;
    }
    default: return OTC_E_UNSUPPORTED;
    }
    otb_host_refresh(vm);
    return 0;
}
