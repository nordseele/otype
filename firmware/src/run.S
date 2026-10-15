| OType run bridge, appended to the DRAM unit after bridge.S; the C VM is the unit otype_c (otc_vm_*, cdecl,
| args 4 bytes each, result d0, d2-d7/a2-a6 kept).
|
| otype_run runs one function of the loaded table on the unit's CURRENT
| pattern, the way a press will, and commits it the stock way:
|   - the 8 audio tracks are bound at EVERY call (nothing cached: the bank
|     pointer, pattern, part and track are read now): TRAC block in the bank,
|     the SRAM copy at 0x1001614e, the machine type from the part;
|   - the host track is the current track (0x100b14cc, 0..7) + 1; its length
|     is the pattern's, or the track's own in PER TRACK mode (0..64, else 64);
|   - if the VM changed any track (touched != 0): the two dirty flags,
|     FUN_40027e00 (project dirty), FUN_400339d8 (lock bitmaps from the
|     records), FUN_4009da20(-1) (UI refresh) — the order of the stock sample-
|     lock store.
|   - the card workspace otr_ws (otc_ws_size() bytes) is bound at every call (otc_vm_bind_work: the
|     same area again keeps the pick bags); if the VM changed a slot (otc_vm_slots_touched != 0):
|     TRACKSYNC(-1) once; the UI refresh (-1) is made ONCE per press, after both commits, when either
|     the tracks or the slots changed (the imports of card.S do neither per slot).
| d0 = the VM's error (0 = ok), or the load's refusal. A refused load leaves
| the request set (the next call retries); nothing is run.
| otype_prepare is the non-executing lazy preparation entry point used
| by UI metadata readers as well as run. It preserves d2-d7/a2-a6 and returns
| the load result in d0; +16 records that result.
|
| otr_mail (the host reads/writes it; the table goes into otr_tab):
|    +0 table length        +4 function index       +8 seed (low 16 bits used)
|   +12 load request (!= 0: otc_vm_load first; cleared when it succeeds)
|   +16 error (out)         +20 touched mask (out)  +24 host track 1..8 (out)
|   +28 length used (out)   +32 reserve size of otr_vm (constant, 38912)
|   +36 reserve size of otr_ws (constant)   +40 slots touched (out: otc_vm_slots_touched)
        .text
        .globl  otype_run
        .globl  otype_prepare
        .globl  otr_mail
        .globl  otr_vm
        .globl  otr_tab
        .globl  otr_ws
        .equ    OTR_WS_SIZE,  669840      | >= otc_ws_size() of the ColdFire C unit (669838), a multiple of 8
        .equ    OTR_TRACKSYNC, 0x40093468 | (mask) re-arm the 8 tracks' voices
        .equ    OTR_BANKPTR,  0x46c82456  | -> the active bank buffer
        .equ    OTR_G_TRK,    0x100b14cc  | current track 0..7 (byte)
        .equ    OTR_G_PART,   0x100b14cf  | current part (byte)
        .equ    OTR_G_PAT,    0x100b14d0  | current pattern (byte)
        .equ    OTR_COPY,     0x1001614e  | the second copy of the pattern records
        .equ    OTR_PTRN,     0x8ed8      | pattern stride
        .equ    OTR_TRAC,     0x91a       | track record stride
        .equ    OTR_PART,     0x18b2      | part stride
        .equ    OTR_PART_OFF, 0x8ed80     | part offset within bank
        .equ    OTR_PART_COMMIT, 0x40029a4c | (src, part) stock part setter
        .equ    OTR_MTYPE,    0x8ed80 + 0x22 | bank + part*PART + this + (track-1) = machine type
        .equ    OTR_P_LEN,    0x8e53      | pattern + this: length; +2 the scale mode
        .equ    OTR_DIRTY_ONE, 0x9b332    | bank + this = 1
        .equ    OTR_DIRTY_PRJ, 0x100f8598 | = 1
        .equ    OTR_PROJ_DIRTY, 0x40027e00 | project dirty
        .equ    OTR_LOCKS,    0x400339d8  | lock bitmaps rebuilt from the records
        .equ    OTR_UIREFRESH, 0x4009da20 | (mask) refresh what shows the tracks
        .align  4
otr_mail:
        .long   0, 0, 0, 0, 0, 0, 0, 0
        .long   38912
        .long   OTR_WS_SIZE
        .long   0
        .ifdef  OT_VM
        | the three big buffers are in the module's memory region (layout.py, OT_* equates), not in the runtime; contiguous
        | in this order, otr_tab is 32768 bytes (OTC_OTB_MAX). The runtime is one flat binary that the integrator copies (and may
        | hash) at boot: megabytes of `.space` would be megabytes of zeros to carry.
        .set    otr_vm,  OT_VM
        .set    otr_tab, OT_TAB
        .set    otr_ws,  OT_WS
        .else
otr_vm:
        .space  38912
otr_tab:
        .space  16384
        .balign 8
otr_ws:
        .space  OTR_WS_SIZE
        .endif
otype_run:
        lea     -44(%sp),%sp
        movem.l %d2-%d7/%a2-%a6,(%sp)
        lea     otr_mail,%a2            | a2 survives the C calls
        jsr     otype_prepare:l
        tst.l   %d0
        bne.w   .Lotr_out
        bra.s   .Lotr_bind
otype_prepare:
        lea     -44(%sp),%sp
        movem.l %d2-%d7/%a2-%a6,(%sp)
        lea     otr_mail,%a2
        tst.l   12(%a2)
        beq.s   .Lotr_prepared
        move.l  (%a2),-(%sp)            | len
        pea     otr_tab
        pea     otr_vm
        jsr     otc_vm_load
        lea     12(%sp),%sp
        move.l  %d0,16(%a2)
        bne.s   .Lotr_prepare_out       | refused: d0 = its code, request kept
        clr.l   12(%a2)
.Lotr_prepared:
        clr.l   %d0
        move.l  %d0,16(%a2)
.Lotr_prepare_out:
        movem.l (%sp),%d2-%d7/%a2-%a6
        lea     44(%sp),%sp
        rts
.Lotr_bind:
        | ---- the live globals, read now ------------------------------------
        move.l  OTR_BANKPTR,%d2         | d2 bank
        clr.l   %d0
        move.b  OTR_G_PAT,%d0
        move.l  #OTR_PTRN,%d3
        mulu.l  %d0,%d3                 | d3 pattern offset
        clr.l   %d4
        move.b  OTR_G_PART,%d4
        move.l  #OTR_PART,%d0
        mulu.l  %d0,%d4                 | d4 part offset
        clr.l   %d5
        move.b  OTR_G_TRK,%d5
        addq.l  #1,%d5                  | d5 host track 1..8
        movea.l %d2,%a3
        adda.l  %d3,%a3                 | a3 TRAC of track 1 in the bank
        movea.l #OTR_COPY,%a4
        adda.l  %d3,%a4                 | a4 the same in the copy
        movea.l %d2,%a5
        adda.l  %d4,%a5
        adda.l  #OTR_MTYPE,%a5          | a5 machine types, track 1
        moveq   #1,%d6                  | d6 track
.Lotr_t:
        clr.l   %d0
        move.b  (%a5)+,%d0
        move.l  %d0,-(%sp)              | mtype
        move.l  %a4,-(%sp)              | copy
        move.l  %a3,-(%sp)              | trac
        move.l  %d6,-(%sp)              | track
        pea     otr_vm
        jsr     otc_vm_bind
        lea     20(%sp),%sp
        lea     OTR_TRAC(%a3),%a3
        lea     OTR_TRAC(%a4),%a4
        addq.l  #1,%d6
        cmpi.l  #9,%d6
        bne.s   .Lotr_t
        | ---- the length: the pattern's, or the host track's in PER TRACK ----
        movea.l %d2,%a0
        adda.l  %d3,%a0
        adda.l  #OTR_P_LEN,%a0          | a0 pattern + P_LEN
        move.b  (%a0),%d7
        tst.b   2(%a0)                  | 0 normal, 1 per track
        beq.s   .Lotr_len
        move.l  #OTR_TRAC,%d0
        move.l  %d5,%d1
        subq.l  #1,%d1
        mulu.l  %d1,%d0
        add.l   %d3,%d0
        add.l   %d2,%d0
        movea.l %d0,%a1                 | a1 the host track's TRAC
        move.b  0x50(%a1),%d7
.Lotr_len:
        extb.l  %d7
        ble.s   .Lotr_64                | never seen; the whole track then
        cmpi.l  #64,%d7
        ble.s   .Lotr_run
.Lotr_64:
        moveq   #64,%d7
.Lotr_run:
        pea     otr_ws                  | the card workspace, again at every call (the same area is a no-op)
        pea     otr_vm
        jsr     otc_vm_bind_work
        addq.l  #8,%sp
        move.l  %d5,24(%a2)
        move.l  %d7,28(%a2)
        move.l  %d7,-(%sp)              | length
        move.l  %d5,-(%sp)              | track
        move.l  8(%a2),%d0
        andi.l  #0xffff,%d0
        move.l  %d0,-(%sp)              | seed
        move.l  4(%a2),-(%sp)           | function index
        pea     otr_vm
        jsr     otc_vm_run
        lea     20(%sp),%sp
        move.l  %d0,%d4                 | d4 error
        move.l  %d0,16(%a2)
        pea     otr_vm
        jsr     otc_vm_touched
        addq.l  #4,%sp
        move.l  %d0,20(%a2)
        clr.l   %d6                     | d6: a UI refresh is owed (the tracks or the slots changed)
        pea     otr_vm
        jsr     otb_host_commit
        addq.l  #4,%sp
.Lotr_slots:
        pea     otr_vm                  | a slot import was called
        jsr     otc_vm_slots_touched
        addq.l  #4,%sp
        move.l  %d0,40(%a2)
        beq.s   .Lotr_refresh
        moveq   #-1,%d0                 | re-arm the voices once, then the refresh
        move.l  %d0,-(%sp)
        jsr     OTR_TRACKSYNC
        addq.l  #4,%sp
        moveq   #1,%d6
.Lotr_refresh:
        tst.l   %d6
        beq.s   .Lotr_done
        moveq   #-1,%d0
        move.l  %d0,-(%sp)              | every track
        jsr     OTR_UIREFRESH
        addq.l  #4,%sp
.Lotr_done:
        move.l  %d4,%d0
.Lotr_out:
        movem.l (%sp),%d2-%d7/%a2-%a6
        lea     44(%sp),%sp
        rts
