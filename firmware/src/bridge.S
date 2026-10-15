| OType bridge: the otb_host_* imports of the C VM that touch the part and the project (part parameters, part
| writers, tempo, track slot). The VM and the probe are the unit otype_c (build/cunits.py); linked together.
        .text
        .globl  otype_probe
        .globl  otb_host_add
        .globl  otp_mail
        .globl  otb_host_param_info
        .globl  otb_host_param_value
        .globl  otb_host_par_set, otb_host_param_default, otb_host_lfo_set, otb_host_lfo_waves
        .globl  otb_host_fx_count, otb_host_fx_default, otb_host_fx_set, otb_host_scene_put, otb_host_trk_slot_init
        .globl  otb_host_trk_slot, otb_host_tempo

        | ---- firmware addresses & equates ----------------------------------
        .equ    OTB_BANKPTR,     0x46c82456   | -> the active bank buffer
        .equ    OTB_G_PART,      0x100b14cf   | current part (byte)
        .equ    OTB_G_TRK,       0x100b14cc   | current track mirror (byte)
        .equ    OTB_ENG_TRK,     0x80000000   | active engine track (byte)
        .equ    OTB_PART_STRIDE, 0x18b2       | stride between parts
        .equ    OTB_PART_OFF,    0x8ed80      | bank + part*0x18b2 + this = part
        .equ    OTB_P_MACHINE,   0x22         | part + 0x22 + track = machine type
        .equ    OTB_P_PB1,       0x2a         | part + 0x2a + track*30 + machine*6 + slot = playback param
        .equ    OTB_P_BLOCK,     0x11a        | part + 0x11a + track*24 + (p - 6) = lfo/amp/fx param
        .equ    OTB_P_SLOT,      0x2ca        | part + 0x2ca + track*5 + machine = slot
        .equ    OTB_P_LFO2,      0x2f2        | part + 0x2f2 + track*30 = lfo2 (dest x3 then wave x3)
        .equ    OTB_P_SCENE,     0x662        | part + 0x662 + (scene*8 + track)*32 = scene byte
        .equ    OTB_PARAMSET,    0x40054cd8   | (track, flat, value)
        .equ    OTB_DESC_GET,    0x40031da4   | (track, page_kind) -> descriptor ptr
        .equ    OTB_FX1_CURSOR,  0x460d5c94   | FX1 chooser cursor
        .equ    OTB_FX1_APPLY,   0x400526e4   | FX1 apply
        .equ    OTB_FX2_CURSOR,  0x460d5ca8   | FX2 chooser cursor
        .equ    OTB_FX2_APPLY,   0x40052474   | FX2 apply
        .equ    OTB_FX1_CHOOSER, 0x400d6060   | FX1 chooser list
        .equ    OTB_FX2_CHOOSER, 0x400d6090   | FX2 chooser list
        .equ    OTB_TEMPO_SET,   0x4009c708   | (tempo24 = bpm * 24)

        .align  4
otp_mail:                               | n, k, count, result, out[64]
        .long   0
        .long   0
        .long   0
        .long   0
        .space  256
otype_probe:
        lea     otp_mail,%a0
        pea     16(%a0)                 | out
        move.l  8(%a0),-(%sp)           | count
        move.l  4(%a0),-(%sp)           | k
        move.l  (%a0),-(%sp)            | n
        jsr     otc_probe               | C: d0 = otb_host_add(last, count)
        lea     16(%sp),%sp
        lea     otp_mail,%a0            | a0/a1/d1 died in the call
        move.l  %d0,12(%a0)
        rts
otb_host_add:
        move.l  4(%sp),%d0
        add.l   8(%sp),%d0
        rts

| =============================================================================
| Parameter info & value
| =============================================================================
| void otb_host_param_info(uint32_t track0, uint32_t p, uint32_t *lo, uint32_t *count, uint32_t *enabled)
otb_host_param_info:
        lea     -16(%sp),%sp
        movem.l %d2-%d4/%a2,(%sp)
        move.l  20(%sp),%d2             | track0
        move.l  24(%sp),%d3             | p
        move.l  %d3,%d0
        moveq   #6,%d1
        divs.l  %d1,%d0                 | %d0 = quotient = page_kind (0..4)
        move.l  %d0,%d4
        mulu.l  %d1,%d4                 | %d4 = page_kind * 6
        sub.l   %d4,%d3                 | %d3 = slot (p % 6, 0..5)
        move.l  %d0,-(%sp)
        move.l  %d2,-(%sp)
        jsr     OTB_DESC_GET            | FUN_40031da4(track0, page_kind) -> desc in %d0
        addq.l  #8,%sp
        tst.l   %d0
        beq.s   .Lpi_dis
        movea.l %d0,%a0                 | %a0 = descriptor
        | check enable bit: bit (slot*4) in 0x18e(%a0)
        move.l  %d3,%d1
        lsl.l   #2,%d1                  | slot * 4
        move.l  0x18e(%a0),%d0
        lsr.l   %d1,%d0
        btst    #0,%d0
        beq.s   .Lpi_dis
        movea.l 36(%sp),%a1             | enabled
        move.l  #1,(%a1)
        adda.l  %d1,%a0                 | desc + slot * 4
        movea.l 28(%sp),%a1             | lo
        move.l  0x6a(%a0),(%a1)
        movea.l 32(%sp),%a1             | count
        move.l  0x9a(%a0),(%a1)
        movem.l (%sp),%d2-%d4/%a2
        lea     16(%sp),%sp
        rts
.Lpi_dis:
        movea.l 36(%sp),%a1             | enabled
        clr.l   (%a1)
        movem.l (%sp),%d2-%d4/%a2
        lea     16(%sp),%sp
        rts

| int32_t otb_host_param_value(uint32_t track0, uint32_t p)
otb_host_param_value:
        lea     -8(%sp),%sp
        movem.l %d2-%d3,(%sp)
        move.l  12(%sp),%d0             | track0
        move.l  16(%sp),%d1             | p
        movea.l OTB_BANKPTR,%a0
        clr.l   %d2
        move.b  OTB_G_PART,%d2
        move.l  #OTB_PART_STRIDE,%d3
        mulu.l  %d3,%d2
        adda.l  %d2,%a0
        adda.l  #OTB_PART_OFF,%a0       | %a0 = working part
        cmpi.l  #6,%d1
        bge.s   .Lpv_block
        | p < 6: PLAYBACK
        clr.l   %d2
        move.b  OTB_P_MACHINE(%a0,%d0.l),%d2 | machine type
        move.l  #6,%d3
        mulu.l  %d3,%d2                 | machine * 6
        move.l  #30,%d3
        mulu.l  %d0,%d3                 | track0 * 30
        add.l   %d3,%d2
        add.l   %d1,%d2                 | + p
        adda.l  #OTB_P_PB1,%a0
        clr.l   %d0
        move.b  (%a0,%d2.l),%d0
        movem.l (%sp),%d2-%d3
        lea     8(%sp),%sp
        rts
.Lpv_block:
        | p >= 6: LFO / AMP / FX1 / FX2
        subq.l  #6,%d1
        move.l  #24,%d2
        mulu.l  %d0,%d2                 | track0 * 24
        add.l   %d1,%d2                 | + (p - 6)
        adda.l  #OTB_P_BLOCK,%a0
        clr.l   %d0
        move.b  (%a0,%d2.l),%d0
        movem.l (%sp),%d2-%d3
        lea     8(%sp),%sp
        rts

| =============================================================================
| Part writers & readers
| =============================================================================
| void otb_host_par_set(uint32_t track0, uint32_t p, int32_t v)
otb_host_par_set:
        move.l  12(%sp),-(%sp)          | v
        move.l  12(%sp),-(%sp)          | p
        move.l  12(%sp),-(%sp)          | track0
        jsr     OTB_PARAMSET
        lea     12(%sp),%sp
        rts

| int32_t otb_host_param_default(uint32_t track0, uint32_t p)
otb_host_param_default:
        lea     -8(%sp),%sp
        movem.l %d2-%d3,(%sp)
        move.l  12(%sp),%d2             | track0
        move.l  16(%sp),%d3             | p
        move.l  %d3,%d0
        moveq   #6,%d1
        divs.l  %d1,%d0                 | %d0 = page_kind
        move.l  %d0,%d1
        moveq   #6,%d3
        mulu.l  %d3,%d1                 | page_kind * 6
        move.l  16(%sp),%d3             | p: return + two args + saved d2/d3
        sub.l   %d1,%d3                 | %d3 = slot (p % 6)
        move.l  %d0,-(%sp)
        move.l  %d2,-(%sp)
        jsr     OTB_DESC_GET            | FUN_40031da4
        addq.l  #8,%sp
        tst.l   %d0
        beq.s   .Lpd_zero
        movea.l %d0,%a0
        clr.l   %d0
        move.b  0x5e(%a0,%d3.l),%d0     | default byte at +0x5e
        movem.l (%sp),%d2-%d3
        lea     8(%sp),%sp
        rts
.Lpd_zero:
        clr.l   %d0
        movem.l (%sp),%d2-%d3
        lea     8(%sp),%sp
        rts

| void otb_host_lfo_set(uint32_t track0, uint32_t lfo0, uint32_t what, uint32_t v)
otb_host_lfo_set:
        lea     -12(%sp),%sp
        movem.l %d2-%d4,(%sp)
        move.l  16(%sp),%d0             | track0
        move.l  20(%sp),%d1             | lfo0
        move.l  24(%sp),%d2             | what
        move.l  28(%sp),%d3             | v
        movea.l OTB_BANKPTR,%a0
        clr.l   %d4
        move.b  OTB_G_PART,%d4
        move.l  #OTB_PART_STRIDE,%d1
        mulu.l  %d1,%d4
        adda.l  %d4,%a0
        adda.l  #OTB_PART_OFF + OTB_P_LFO2,%a0
        move.l  #30,%d1
        mulu.l  %d0,%d1                 | track0 * 30
        adda.l  %d1,%a0
        tst.l   %d2
        beq.s   .Lls_dest
        adda.l  #3,%a0                  | waveform is +3
.Lls_dest:
        move.l  20(%sp),%d1             | lfo0
        move.b  %d3,(%a0,%d1.l)
        movem.l (%sp),%d2-%d4
        lea     12(%sp),%sp
        rts

| uint32_t otb_host_lfo_waves(void)
otb_host_lfo_waves:
        moveq   #19,%d0
        rts

| uint32_t otb_host_fx_count(uint32_t slot0)
otb_host_fx_count:
        move.l  4(%sp),%d0              | slot0
        movea.l #OTB_FX1_CHOOSER,%a0
        tst.l   %d0
        beq.s   .Lfc_scan
        movea.l #OTB_FX2_CHOOSER,%a0
.Lfc_scan:
        clr.l   %d0                     | count
.Lfc_loop:
        move.l  (%a0)+,%d1
        cmpi.l  #0x400d4000,%d1
        blo.s   .Lfc_done
        cmpi.l  #0x400d6000,%d1
        bhs.s   .Lfc_done
        addq.l  #1,%d0
        cmpi.l  #32,%d0
        blo.s   .Lfc_loop
.Lfc_done:
        rts

| uint32_t otb_host_fx_default(uint32_t slot0)
otb_host_fx_default:
        move.l  4(%sp),%d0
        tst.l   %d0
        bne.s   .Lfd_fx2
        moveq   #1,%d0                  | FX1 FILTER (index 1)
        rts
.Lfd_fx2:
        moveq   #11,%d0                 | FX2 DELAY (index 11)
        rts

| void otb_host_fx_set(uint32_t track0, uint32_t slot0, uint32_t index)
otb_host_fx_set:
        lea     -12(%sp),%sp
        movem.l %d2-%d4,(%sp)
        move.l  16(%sp),%d0             | track0
        move.l  20(%sp),%d1             | slot0
        move.l  24(%sp),%d2             | index
        clr.l   %d3
        clr.l   %d4
        move.b  OTB_ENG_TRK,%d3         | save engine track
        move.b  OTB_G_TRK,%d4           | save UI track mirror
        move.b  %d0,OTB_ENG_TRK         | switch to track0
        move.b  %d0,OTB_G_TRK
        tst.l   %d1
        bne.s   .Lfs_fx2
        move.l  %d2,OTB_FX1_CURSOR
        jsr     OTB_FX1_APPLY
        bra.s   .Lfs_rest
.Lfs_fx2:
        move.l  %d2,OTB_FX2_CURSOR
        jsr     OTB_FX2_APPLY
.Lfs_rest:
        move.b  %d3,OTB_ENG_TRK         | restore engine track
        move.b  %d4,OTB_G_TRK           | restore UI track mirror
        movem.l (%sp),%d2-%d4
        lea     12(%sp),%sp
        rts

| void otb_host_scene_put(uint32_t track0, uint32_t scene0, uint32_t byte, uint32_t v)
otb_host_scene_put:
        lea     -16(%sp),%sp
        movem.l %d2-%d5,(%sp)
        move.l  20(%sp),%d0             | track0
        move.l  24(%sp),%d1             | scene0
        move.l  28(%sp),%d2             | byte
        move.l  32(%sp),%d3             | v
        movea.l OTB_BANKPTR,%a0
        clr.l   %d4
        move.b  OTB_G_PART,%d4
        move.l  #OTB_PART_STRIDE,%d5
        mulu.l  %d5,%d4
        adda.l  %d4,%a0
        adda.l  #OTB_PART_OFF + OTB_P_SCENE,%a0
        lsl.l   #3,%d1                  | scene0 * 8
        add.l   %d0,%d1                 | scene0 * 8 + track0
        lsl.l   #5,%d1                  | (scene0 * 8 + track0) * 32
        add.l   %d2,%d1                 | + byte
        move.b  %d3,(%a0,%d1.l)
        movem.l (%sp),%d2-%d5
        lea     16(%sp),%sp
        rts

| void otb_host_trk_slot_init(uint32_t track0)
otb_host_trk_slot_init:
        lea     -8(%sp),%sp
        movem.l %d2-%d3,(%sp)
        move.l  12(%sp),%d0             | track0
        movea.l OTB_BANKPTR,%a0
        clr.l   %d1
        move.b  OTB_G_PART,%d1
        move.l  #OTB_PART_STRIDE,%d2
        mulu.l  %d2,%d1
        adda.l  %d1,%a0
        adda.l  #OTB_PART_OFF + OTB_P_SLOT,%a0
        moveq   #5,%d1
        mulu.l  %d0,%d1                 | track0 * 5
        adda.l  %d1,%a0
        move.b  %d0,(%a0)+              | machine 0 (STATIC) -> track0
        move.b  %d0,(%a0)+              | machine 1 (FLEX)   -> track0
        clr.b   (%a0)+                  | machine 2 (THRU)   -> 0
        clr.b   (%a0)+                  | machine 3 (NEIGHBOR)-> 0
        move.l  %d0,%d2
        addi.l  #0x80,%d2
        move.b  %d2,(%a0)+              | machine 4 (PICKUP) -> 128 + track0
        movem.l (%sp),%d2-%d3
        lea     8(%sp),%sp
        rts

| void otb_host_trk_slot(uint32_t track0, uint32_t slot0)
otb_host_trk_slot:
        lea     -12(%sp),%sp
        movem.l %d2-%d4,(%sp)
        move.l  16(%sp),%d0             | track0
        move.l  20(%sp),%d1             | slot0
        movea.l OTB_BANKPTR,%a0
        clr.l   %d2
        move.b  OTB_G_PART,%d2
        move.l  #OTB_PART_STRIDE,%d3
        mulu.l  %d3,%d2
        adda.l  %d2,%a0
        adda.l  #OTB_PART_OFF,%a0
        clr.l   %d2
        move.b  OTB_P_MACHINE(%a0,%d0.l),%d2 | machine type (0..4)
        cmpi.l  #4,%d2
        bhi.s   .Lts_done
        moveq   #5,%d3
        mulu.l  %d0,%d3                 | track0 * 5
        add.l   %d2,%d3                 | + machine
        adda.l  #OTB_P_SLOT,%a0
        move.b  %d1,(%a0,%d3.l)
.Lts_done:
        movem.l (%sp),%d2-%d4
        lea     12(%sp),%sp
        rts

| void otb_host_tempo(int32_t bpm)
otb_host_tempo:
        move.l  4(%sp),%d0              | bpm (30..300)
        moveq   #24,%d1
        mulu.l  %d1,%d0                 | tempo24 = bpm * 24
        move.l  %d0,-(%sp)
        jsr     OTB_TEMPO_SET
        addq.l  #4,%sp
        rts
