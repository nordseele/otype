| OType script loader (the SCRIPT row of the action table): LOAD, the loaded functions, and ERRORS.TXT.
| Assembled in the same unit as shim.S, bridge.S, run.S and card.S, so every label and equate of shim.S (rd_fh, rd_io,
| wr_fh, wr_io, wr_mode_w, ST_MBOX, MB_POST, FS_REMOVE, FIO_*, .Lrelabel, TICKS, SPRINTF, pop_rows) and of card.S
| (otk_lock, OTK_*) is in scope. The C half is the unit otype_script (otc_script.c: ots_load, cdecl), the compiler is
| otype_comp (otc_compile), the VM otype_c; the unit's header defines OT_VM OT_TAB OT_WS OT_STAGE OT_REPORT OT_SRC
| OT_SRCV OT_LABELS OT_ARENA OT_ARENA_SIZE OT_STACK_TOP OT_REPORT_CAP OT_SRC_CAP OT_ROW OT_JOB (the memory region
| otype_rgn of layout.py: none of those buffers is in the runtime).
|
| LOAD (otype_ot_load, the row's action 0, UI task, the integrator's interface already closed):
|   zero the VM / table / card workspace once (the reserve is not zero at boot) -> ots_load(ot_ctx) -> on a table:
|   copy it to the VM's (otr_mail +0 length, +12 load request), make the row's actions (R_N = 1 + count, cursor on LOAD,
|   relabelled), -> post OT_JOB (the storage task writes /SCRIPTS/ERRORS.TXT) when a compile ran -> MSG_POP(message, 48
|   ticks: the stock's own passing message, it closes itself).
| RUN (otype_ot_menu(slot), A_VALUE of the action): slot -> function index -> otr_mail +4, seed +8 -> otype_run (it
|   commits). The seed: a press counter * 40503 + TICKS (the display-loop tick, "a little entropy"), low 16 bits; the
|   stock has no better documented counter.
|
| ot_ctx is ots_ctx_t of otc_script.c (static asserts there): the offsets below are its.
        .text
        .globl  otype_ot_load
        .globl  otype_ot_menu
        .globl  otype_ot_stjob
        .globl  otype_ot_job
        .globl  otb_host_ls_open
        .globl  otb_host_ls_next
        .globl  otb_host_file_read
        .globl  otb_host_compile
        .globl  ot_ctx
        .globl  ot_slotfn
        .globl  ot_lastrc
        .globl  ot_menuquiet
        .globl  ot_state
        .globl  ot_msgbuf
        .globl  ot_rec
        .equ    OTS_ACTS,       40
        .equ    OTS_STATUS,     72
        .equ    OTS_OTBLEN,     84
        .equ    OTS_REPLEN,     88
        .equ    OTS_NACTS,      96
        .equ    OTS_NREFUSED,   104
        .equ    OTS_NCUT,       112
        .equ    OTS_MSG,        60
        .equ    OT_MSG_POP,     0x4005a2b8      | (text, ticks): the stock's passing message; ticks > 0 closes it by itself
        .equ    OT_MSG_TICKS,   48
        .equ    OT_PRESS_MUL,   40503
        .equ    OT_CHUNK,       0x4000          | one FS write: 16 KiB (FS_MUTEX is given back between two)
        .equ    OT_NACT,        2048               | 32 categories x 64 scripts each

        .balign 4
ot_ctx:
        .long   OT_SRC, OT_SRC_CAP, OT_ARENA, OT_ARENA_SIZE, OT_STACK_TOP, OT_STAGE
        .long   OT_REPORT, OT_REPORT_CAP, OT_SRCV, OT_LABELS
        .long   0                               | +40 acts: the row's action record 1 (set at LOAD)
        .long   ot_shorts, ot_longs, ot_slotfn, otype_ot_menu, ot_msgbuf, ot_name, ot_order
        .long   0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0   | +72.. the outputs
        .balign 4
ot_state:                                       | {zeroed, busy, loaded, press} as longs, for the verifier
ot_zeroed:      .long   0
ot_busy:        .long   0                       | the storage task still writes ERRORS.TXT from OT_REPORT
ot_nloaded:     .long   0                       | script actions live in the row (the bound of otype_ot_menu)
ot_press:       .long   0
ot_lastrc:      .long   0                       | the last menu run's result (0 = done), read by the integrator's interface
ot_menuquiet:   .long   0                       | 1: the error text is left in ot_msgbuf, not shown (the integrator's interface shows it)
ot_wres:        .long   0                       | the last ERRORS.TXT write: its length, or < 0
ot_njobs:       .long   0
ot_pendval:     .long   0                       | A_VALUE of the destructive action waiting for YES / NO
ot_quiet:       .long   0                       | 1 = a quiet LOAD (auto.S): the passing message only for an error / no script
        .globl  ot_quiet
        .globl  ot_zeroed, ot_busy, ot_nloaded, ot_press, ot_wres, ot_njobs
ot_shorts:      .space  OT_NACT * 12
ot_longs:       .space  OT_NACT * 24
ot_slotfn:      .space  OT_NACT
ot_msgbuf:      .space  48
ot_name:        .space  264
ot_order:       .space  128
        .balign 4
ot_it:          .space  16                      | ITERINIT's ctx (3 longs)
ot_rec:         .space  0x140                   | the directory record
ot_msgjob:      .byte   OT_JOB, 0               | our storage job
                .space  14
ot_dir:         .asciz  "/SCRIPTS"
ot_path:        .asciz  "/SCRIPTS/ERRORS.TXT"
ot_fmt_err:     .asciz  "SCRIPT ERROR %d"
ot_busymsg:     .asciz  "SCRIPTS: WRITING"
        .balign 4

| ============================================================================================================
| otb_host_ls_open() -> 0 ok (ot_it is the iterator), -1 no /SCRIPTS folder, -2 card busy. Imports of the C half.
| The FS mutex policy of card.S: taken per access with otk_lock (non-blocking try, sleeps), never held between two.
| ============================================================================================================
otb_host_ls_open:
        lea     -8(%sp),%sp
        movem.l %d2/%a2,(%sp)
        jsr     otk_lock:l
        tst.l   %d0
        beq.s   .Lots_lo_busy
        pea     ot_rec:l
        pea     ot_dir:l
        jsr     OTK_RESOLVE
        addq.l  #8,%sp
        move.l  %d0,%d2
        pea     OTK_FS_MUTEX
        jsr     OTK_MUTEX_GIVE
        addq.l  #4,%sp
        tst.l   %d2
        bne.s   .Lots_lo_none                   | nothing at that path
        lea     ot_rec:l,%a0
        move.b  OTK_R_ATTR(%a0),%d0
        andi.l  #0x10,%d0
        beq.s   .Lots_lo_none                   | a file called SCRIPTS
        move.l  ot_rec+OTK_R_CLU:l,%d0            | the whole cluster: a FAT32 folder goes past 65535
        pea     ot_it:l
        move.l  %d0,-(%sp)
        jsr     OTK_ITERINIT
        addq.l  #8,%sp
        clr.l   %d0
        bra.s   .Lots_lo_out
.Lots_lo_none:
        moveq   #-1,%d0
        bra.s   .Lots_lo_out
.Lots_lo_busy:
        moveq   #-2,%d0
.Lots_lo_out:
        movem.l (%sp),%d2/%a2
        lea     8(%sp),%sp
        rts

| otb_host_ls_next(out) -> 1 a file, 2 a folder, 3 a name over 259 bytes, 0 the end, -2 card busy; `out` = the name
| (Latin-1, as the card stores it), NUL terminated. Volume labels are skipped here.
otb_host_ls_next:
        lea     -12(%sp),%sp
        movem.l %d2-%d3/%a2,(%sp)
        move.l  16(%sp),%a2                     | out
.Lots_ln_again:
        jsr     otk_lock:l
        tst.l   %d0
        beq.w   .Lots_ln_busy
        pea     ot_rec:l
        pea     ot_it:l
        jsr     OTK_NEXTIN                      | the body of NEXTENT, under our own try
        addq.l  #8,%sp
        move.l  %d0,%d2
        pea     OTK_FS_MUTEX
        jsr     OTK_MUTEX_GIVE
        addq.l  #4,%sp
        tst.l   %d2
        bne.s   .Lots_ln_end
        lea     ot_rec:l,%a0
        move.b  OTK_R_ATTR(%a0),%d0
        btst    #3,%d0                          | a volume label
        bne.s   .Lots_ln_again
        moveq   #1,%d3
        btst    #4,%d0
        beq.s   .Lots_ln_cp
        moveq   #2,%d3
.Lots_ln_cp:
        move.l  #OTK_NAMEMAX,%d1                | at most 259 bytes + NUL
        move.l  %a2,%a1
.Lots_ln_c:
        move.b  (%a0)+,%d0
        beq.s   .Lots_ln_done
        move.b  %d0,(%a1)+
        subq.l  #1,%d1
        bne.s   .Lots_ln_c
        clr.b   (%a1)                           | 259 bytes read and no NUL yet: too long
        moveq   #3,%d3
        bra.s   .Lots_ln_ret
.Lots_ln_done:
        clr.b   (%a1)
.Lots_ln_ret:
        move.l  %d3,%d0
        bra.s   .Lots_ln_out
.Lots_ln_end:
        clr.b   (%a2)
        clr.l   %d0
        bra.s   .Lots_ln_out
.Lots_ln_busy:
        moveq   #-2,%d0
.Lots_ln_out:
        movem.l (%sp),%d2-%d3/%a2
        lea     12(%sp),%sp
        rts

| otb_host_file_read(path, buf, max) -> the size (the file is read into buf only when size <= max), 0 for an empty file,
| -1 absent, -3 the read failed. Reads only, through the stock buffered calls (rd_fh, rd_io).
otb_host_file_read:
        link.w  %fp,#0
        lea     -8(%sp),%sp
        movem.l %d2/%a2,(%sp)
        move.l  #FIO_IOLEN,-(%sp)
        pea     rd_io:l
        pea     rd_mode_r:l
        move.l  8(%fp),-(%sp)                   | path
        pea     rd_fh:l
        jsr     FIO_OPEN
        lea     20(%sp),%sp
        tst.l   %d0
        bmi.s   .Lots_fr_absent
        move.l  rd_fh:l,-(%sp)                    | the handle's first word, as the size slot takes it
        move.l  FIO_SIZE,%a0
        jsr     (%a0)
        addq.l  #4,%sp
        move.l  %d0,%d2                         | size
        bmi.s   .Lots_fr_fail
        beq.s   .Lots_fr_close                  | empty: nothing to read
        cmp.l   16(%fp),%d2
        bhi.s   .Lots_fr_close                  | larger than the caller's room: sized only
        move.l  %d2,-(%sp)
        move.l  12(%fp),-(%sp)                  | dst
        pea     rd_fh:l
        jsr     FIO_READ
        lea     12(%sp),%sp
        moveq   #1,%d1
        cmp.l   %d1,%d0
        beq.s   .Lots_fr_close
.Lots_fr_fail:
        moveq   #-3,%d2
.Lots_fr_close:
        pea     rd_fh:l
        jsr     FIO_CLOSE
        addq.l  #4,%sp
        move.l  %d2,%d0
        bra.s   .Lots_fr_out
.Lots_fr_absent:
        moveq   #-1,%d0
.Lots_fr_out:
        movem.l (%sp),%d2/%a2
        lea     8(%sp),%sp
        unlk    %fp
        rts

| otb_host_compile(argv): otc_compile(argv[0..10]) (cdecl, 11 arguments) on the private stack argv[11] (8-aligned top).
| otc_compile keeps d2-d7 / a2-a6, so a2 carries the old SP across it.
otb_host_compile:
        lea     -8(%sp),%sp
        movem.l %d2/%a2,(%sp)
        move.l  12(%sp),%a0                     | argv
        move.l  %sp,%a2                         | the old SP (restored below)
        move.l  44(%a0),%sp                     | the private stack
        lea     -44(%sp),%sp
        move.l  %sp,%a1
        moveq   #11,%d0
.Lots_cc_cp:
        move.l  (%a0)+,(%a1)+
        subq.l  #1,%d0
        bne.s   .Lots_cc_cp
        jsr     otc_compile:l
        move.l  %a2,%sp
        movem.l (%sp),%d2/%a2
        lea     8(%sp),%sp
        rts

| ============================================================================================================
| otype_ot_load(arg) — the SCRIPT row's action 0.
| ============================================================================================================
otype_ot_load:
        lea     -44(%sp),%sp
        movem.l %d2-%d7/%a2-%a6,(%sp)
        lea     ot_ctx:l,%a2
        tst.l   ot_busy:l                         | the last report is still being written from OT_REPORT
        bne.w   .Lotl_wait
        tst.l   ot_zeroed:l
        bne.s   .Lotl_go
        lea     OT_VM,%a0                       | VM, table and workspace are one run: the reserve is not zero at boot
        move.l  #(OT_WS_OFF + OTR_WS_SIZE - OT_VM_OFF) / 4,%d0
.Lotl_z:
        clr.l   (%a0)+
        subq.l  #1,%d0
        bne.s   .Lotl_z
        moveq   #1,%d0
        move.l  %d0,ot_zeroed:l
.Lotl_go:
        move.l  pop_rows+OT_ROW*12+R_ACTS:l,%d0
        addi.l  #16,%d0                         | the record after LOAD
        move.l  %d0,OTS_ACTS(%a2)
        move.l  %a2,-(%sp)
        jsr     ots_load:l
        addq.l  #4,%sp
        move.l  OTS_STATUS(%a2),%d2
        moveq   #4,%d0
        cmp.l   %d0,%d2
        beq.w   .Lotl_msg                       | the card was busy: nothing changed
        clr.l   %d3                             | d3 = script actions the row holds now
        tst.l   %d2
        bne.s   .Lotl_rep                       | no table
        move.l  OTS_OTBLEN(%a2),%d0
        lea     otr_mail:l,%a0
        move.l  %d0,(%a0)                       | +0 length
        moveq   #1,%d1
        move.l  %d1,12(%a0)                     | +12 load request
        addq.l  #3,%d0
        lsr.l   #2,%d0
        lea     OT_STAGE,%a0
        lea     OT_TAB,%a1
.Lotl_cp:
        move.l  (%a0)+,(%a1)+
        subq.l  #1,%d0
        bne.s   .Lotl_cp
        move.l  OTS_NACTS(%a2),%d3
.Lotl_rep:
        tst.l   %d2
        beq.s   .Lotl_latched
        clr.l   otr_tab:l                       | completed load without a table: no stale PAGE functions
        clr.l   otr_mail:l
        clr.l   otr_mail+12:l
        clr.l   %sp@-
        pea     otr_tab:l
        pea     otr_vm:l
        jsr     otc_vm_load:l
        lea     %sp@(12),%sp
.Lotl_latched:
        jsr     otype_ot_autodone:l            | shared first-visit latch, including a completed empty load
        move.l  %d3,ot_nloaded:l
        lea     pop_rows+OT_ROW*12:l,%a0      | the row: R_N = 1 + count, the cursor on LOAD, relabelled
        move.l  %d3,%d0
        addq.l  #1,%d0
        cmpi.l  #255,%d0                       | R_N is one byte; an interface reads ot_nloaded
        bls.s   .Lotl_row_count
        move.l  #255,%d0
.Lotl_row_count:
        move.b  %d0,R_N(%a0)
        clr.b   R_CUR(%a0)
        moveq   #OT_ROW,%d0
        jsr     .Lrelabel:l
        moveq   #1,%d0                          | the report goes to the card when a compile ran (status 0 or 2)
        cmp.l   %d2,%d0
        beq.s   .Lotl_msg                       | 1: no file, no folder
        moveq   #3,%d0
        cmp.l   %d2,%d0
        beq.s   .Lotl_msg                       | 3: the compiler refused to run: no report
        moveq   #1,%d0
        move.l  %d0,ot_busy:l
        addq.l  #1,ot_njobs:l
        pea     ot_msgjob:l
        pea     ST_MBOX
        jsr     MB_POST
        addq.l  #8,%sp
.Lotl_msg:
        tst.l   ot_quiet:l                      | the LOAD action says nothing when all went well
        beq.s   .Lotl_post
        tst.l   %d2                             | status 0: a table ...
        bne.s   .Lotl_post
        tst.l   OTS_NREFUSED(%a2)               | ... without refused functions
        bne.s   .Lotl_post
        tst.l   OTS_NCUT(%a2)                   | ... or menu lines cut
        bne.s   .Lotl_post
        tst.l   OTS_NACTS(%a2)                  | ... and with at least one action
        bne.s   .Lotl_out
.Lotl_post:
        move.l  #OT_MSG_TICKS,-(%sp)
        move.l  OTS_MSG(%a2),-(%sp)
        jsr     OT_MSG_POP
        addq.l  #8,%sp
        bra.s   .Lotl_out
.Lotl_wait:
        move.l  #OT_MSG_TICKS,-(%sp)
        pea     ot_busymsg:l
        jsr     OT_MSG_POP
        addq.l  #8,%sp
.Lotl_out:
        movem.l (%sp),%d2-%d7/%a2-%a6
        lea     44(%sp),%sp
        clr.l   %d0
        rts

| ============================================================================================================
| otype_ot_menu(slot) — a script action: its menu slot runs its function.
| ============================================================================================================
otype_ot_menu:
        lea     -44(%sp),%sp
        movem.l %d2-%d7/%a2-%a6,(%sp)
        move.l  48(%sp),%d0                     | the slot
        cmp.l   ot_nloaded:l,%d0
        bcc.w   .Lotm_out                       | unsigned: not a live action
        lea     ot_slotfn:l,%a0
        moveq   #0,%d1
        move.b  %a0@(0,%d0:l),%d1
        lea     otr_mail:l,%a2
        move.l  %d1,4(%a2)                      | the function's index in the table
        addq.l  #1,ot_press:l
        move.l  ot_press:l,%d0
        move.l  #OT_PRESS_MUL,%d1
        mulu.l  %d1,%d0
        add.l   TICKS:l,%d0
        andi.l  #0xffff,%d0
        move.l  %d0,8(%a2)                      | the seed
        jsr     otype_run:l
        move.l  %d0,ot_lastrc:l
        tst.l   %d0
        beq.s   .Lotm_out
        move.l  %d0,-(%sp)
        pea     ot_fmt_err:l
        pea     ot_msgbuf:l
        jsr     SPRINTF
        lea     12(%sp),%sp
        tst.l   ot_menuquiet:l
        bne.s   .Lotm_out
        move.l  #OT_MSG_TICKS,-(%sp)
        pea     ot_msgbuf:l
        jsr     OT_MSG_POP
        addq.l  #8,%sp
.Lotm_out:
        movem.l (%sp),%d2-%d7/%a2-%a6
        lea     44(%sp),%sp
        clr.l   %d0
        rts

| ============================================================================================================
| The storage task: our job OT_JOB (the hook in otype_st): /SCRIPTS/ERRORS.TXT = OT_REPORT, ot_ctx.report_len bytes,
| the old file removed first (the stock's write order), written in chunks of 16 KiB, closed; ot_busy released.
| ============================================================================================================
otype_ot_stjob:
        jsr     otype_ot_job:l
        jmp     ST_LOOP

otype_ot_job:
        lea     -40(%sp),%sp
        movem.l %d2-%d7/%a2-%a5,(%sp)
        pea     ot_path:l
        move.l  FS_REMOVE,%a0
        jsr     (%a0)
        addq.l  #4,%sp
        move.l  #FIO_IOLEN,-(%sp)
        pea     wr_io:l
        pea     wr_mode_w:l
        pea     ot_path:l
        pea     wr_fh:l
        jsr     FIO_OPEN
        lea     20(%sp),%sp
        tst.l   %d0
        bmi.s   .Lotj_res
        move.l  ot_ctx+OTS_REPLEN:l,%d3       | bytes left
        lea     OT_REPORT,%a2
        clr.l   %d2                             | written
.Lotj_chunk:
        tst.l   %d3
        beq.s   .Lotj_close
        move.l  %d3,%d4
        cmpi.l  #OT_CHUNK,%d4
        ble.s   .Lotj_w
        move.l  #OT_CHUNK,%d4
.Lotj_w:
        move.l  %d4,-(%sp)
        move.l  %a2,-(%sp)
        pea     wr_fh:l
        jsr     FIO_WRITE
        lea     12(%sp),%sp
        tst.l   %d0
        bmi.s   .Lotj_werr
        add.l   %d4,%d2
        adda.l  %d4,%a2
        sub.l   %d4,%d3
        bra.s   .Lotj_chunk
.Lotj_werr:
        move.l  %d0,%d2                         | the error
        pea     wr_fh:l
        jsr     FIO_CLOSE
        addq.l  #4,%sp
        move.l  %d2,%d0
        bra.s   .Lotj_res
.Lotj_close:
        pea     wr_fh:l
        jsr     FIO_CLOSE
        addq.l  #4,%sp
        tst.l   %d0
        bmi.s   .Lotj_res
        move.l  %d2,%d0
.Lotj_res:
        move.l  %d0,ot_wres:l
        clr.l   ot_busy:l
        movem.l (%sp),%d2-%d7/%a2-%a5
        lea     40(%sp),%sp
        rts
