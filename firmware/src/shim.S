| OType glue between the engine sources (bridge, run, card, script, auto) and the stock OS, defined in the same
| assembly unit (the .L labels are unit-scoped). Nothing here is Elektron's; every address is an equate
| (stock.inc). Assembled BEFORE the other sources.
        .text

| ---- the storage task (entry point at 0x4008485e, README.md) ----------------------------------------------------
| The stock reads a job's type here (`mvz.b (a2),d0 ; moveq #45,d1 ; cmp.l d0,d1`, a2 = the job, a0/a1/d0/d1 free).
| otype_st: our job OT_JOB (ERRORS.TXT) is done here and the task takes the next; any other job gets the stock's three
| instructions again and its jump on the type.
        .globl  otype_st
otype_st:
        mvz.b   %a2@,%d0
        cmpi.l  #OT_JOB,%d0
        beq.s   .Lst_ours
        moveq   #45,%d1
        cmp.l   %d0,%d1
        jmp     ST_SWITCH
.Lst_ours:
        jmp     otype_ot_stjob:l        | writes ERRORS.TXT, then jumps to ST_LOOP

| ---- the SCRIPT row: .Lrow_rec / .Lrelabel ------------------------------------------------------------
| .Lrow_rec -- d0 = row: a0 = its record, a1 = the action it shows. d1 clobbered.
.Lrow_rec:
        move.l  %d0,%d1
        lsl.l   #2,%d1
        movea.l %d1,%a0
        adda.l  %d1,%a0
        adda.l  %d1,%a0                   | row*12
        adda.l  #pop_rows,%a0
        clr.l   %d1
        move.b  %a0@(R_CUR),%d1
        lsl.l   #4,%d1                    | cursor*16
        movea.l %a0@(R_ACTS),%a1
        adda.l  %d1,%a1
        rts

| .Lrelabel -- d0 = row: rewrite its label from its cursor, at the fixed width
|     NAME__ ' ' '<' ACTION____ '>'
| with '<' and '>' only where that arrow leads somewhere, and an action that takes a value showing it as "NNN%" over
| its last four characters.
.Lrelabel:
        lea     %sp@(-12),%sp
        moveml  %d2/%d3/%a2,%sp@
        bsr.w   .Lrow_rec
        movea.l %a0@(R_LABEL),%a2
        clr.l   %d2
        move.b  %a0@(R_CUR),%d2
        moveq   #0x20,%d0
        tst.l   %d2
        beq.s   .Lrl_lt
        moveq   #0x3c,%d0                 | '<'
.Lrl_lt:
        move.b  %d0,%a2@(NAME_W+1)
        clr.l   %d1
        move.b  %a0@(R_N),%d1
        subq.l  #1,%d1
        moveq   #0x20,%d0
        cmp.l   %d1,%d2
        bge.s   .Lrl_gt
        moveq   #0x3e,%d0                 | '>'
.Lrl_gt:
        move.b  %d0,%a2@(NAME_W+2+ACT_W)
        movea.l %a1@(A_NAME),%a0          | NUL-terminated; padded here
        lea     %a2@(NAME_W+2),%a2
        moveq   #ACT_W,%d3
.Lrl_ch:
        moveq   #0x20,%d0
        tst.b   %a0@
        beq.s   .Lrl_put
        move.b  %a0@+,%d0
.Lrl_put:
        move.b  %d0,%a2@+
        subq.l  #1,%d3
        bne.s   .Lrl_ch
        btst    #F_VALUE,%a1@(A_FLAGS)
        beq.s   .Lrl_out
        moveq   #0x25,%d0                 | '%', the last character
        move.b  %d0,%a2@-
        clr.l   %d0
        move.b  %a1@(A_VALUE),%d0
        moveq   #10,%d2
        moveq   #3,%d3
.Lrl_dig:                                  | three digits, right to left
        remu.l  %d2,%d1:%d0
        divu.l  %d2,%d0
        addi.l  #0x30,%d1
        move.b  %d1,%a2@-
        subq.l  #1,%d3
        bne.s   .Lrl_dig
        clr.l   %d0                        | then blank the leading zeros
        move.b  %a1@(A_VALUE),%d0
        moveq   #0x20,%d1
        cmpi.l  #100,%d0
        bge.s   .Lrl_out
        move.b  %d1,%a2@
        cmpi.l  #10,%d0
        bge.s   .Lrl_out
        move.b  %d1,%a2@(1)
.Lrl_out:
        moveml  %sp@,%d2/%d3/%a2
        lea     %sp@(12),%sp
        rts

| ---- the file objects of the card access ------------------------------------------------------------
| card.S / script.S read through rd_fh/rd_io (mode "r") and the storage task writes ERRORS.TXT through wr_fh/wr_io
| (mode "w").
        .balign 4
rd_fh:  .space  0x40                      | the file object (24 B used)
rd_io:  .space  FIO_IOLEN                  | the stream buffer the open wants
rd_mode_r:
        .asciz  "r"
wr_mode_w:
        .asciz  "w"
        .balign 4
wr_fh: .space  0x40                      | the storage task's file object
wr_io: .space  FIO_IOLEN
