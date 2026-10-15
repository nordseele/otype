| Shared first-visit LOAD for the script table: the scripts are loaded automatically the first time.
        .text
        .globl  otype_ot_auto, otype_ot_quietload, otype_ot_autodone
        .globl  ot_auto_loaded, ot_auto_owner
        .balign 4
ot_auto_loaded: .long 0
ot_auto_owner:  .space 160

| d0 = 1 when a load completed, 0 when already loaded or refused (retry).
otype_ot_auto:
        tst.l   ot_auto_loaded:l
        beq.s   otype_ot_quietload
        jsr     PRJ_OPEN
        tst.l   %d0
        beq.s   .Lota_same
        pea     PRJ_NAME
        pea     ot_auto_owner:l
        jsr     STR_CMP
        addq.l  #8,%sp
        tst.l   %d0
        bne.s   otype_ot_quietload
.Lota_same:
        clr.l   %d0
        rts

otype_ot_quietload:
        moveq   #4,%d0
        move.l  %d0,ot_ctx+OTS_STATUS:l
        moveq   #1,%d0
        move.l  %d0,ot_quiet:l
        clr.l   %sp@-
        jsr     otype_ot_load:l
        addq.l  #4,%sp
        clr.l   ot_quiet:l
        move.l  ot_ctx+OTS_STATUS:l,%d0
        cmpi.l  #4,%d0
        beq.s   .Lota_same
        bsr.s   otype_ot_autodone
        moveq   #1,%d0
        rts

| Also called by the LOAD action. Idempotent, no I/O of its own.
otype_ot_autodone:
        moveq   #1,%d0
        move.l  %d0,ot_auto_loaded:l
        jsr     PRJ_OPEN
        tst.l   %d0
        beq.s   .Lotad_out
        pea     PRJ_NAME
        pea     ot_auto_owner:l
        jsr     STR_CPY
        addq.l  #8,%sp
.Lotad_out:
        rts
