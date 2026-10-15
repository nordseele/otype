| OType card / slot imports: the otb_host_* functions the C VM (c/vm.c, contracts in c/otc_vm.h) calls. Appended to the
| DRAM unit next to bridge.S / run.S; assembled with -mcpu=5407 like the C units.
| cdecl: args on the stack at 4(sp).., result d0, d2-d7 / a2-a6 kept.
|
| The firmware cores are the stock's own file-system and sample-slot routines:
|   PROJDIR 0x40025230(0,0)    -> the current project's path          (fill_all)
|   RESOLVE 0x4001aa70(path, rec) -> 0 found, rec filled (0x140 B)    (no mutex of its own)
|   ITERINIT 0x40016f5c(cluster, ctx) -> -1 (not a failure), ctx = 3 longs
|   0x40019d30(ctx, rec) -> 0 an entry, else the end: the body of NEXTENT 0x4001aa34, which is
|       MUTEX_TAKE(FS_MUTEX); 0x40019d30(a, b); MUTEX_GIVE(FS_MUTEX)  (read in the image)
|   rec: name at +0 (NUL), attribute byte +0x10d (0x10 folder, 0x08 volume label), first cluster LONG +0x11e
|
| The mutex policy: FS_MUTEX (0x461079c0) is taken PER ACCESS, with the non-blocking
| try (0x40000a94), tried again for about 2 s, and never held between two accesses. The stock NEXTENT would
| BLOCK on it (MUTEX_TAKE parks the UI task) and would deadlock if we held it ourselves, so the walker calls
| NEXTENT's inner function under its own try. SLOTLOAD / FUN_40025288 take the mutex inside the stock open /
| close: they are never called under ours (card_busy only probes it, once, before the C starts the operation).
| The wait: try; while busy, SLEEP 10 ms with the stock task delay 0x40020c7c and try again,
| `otk_sleeps` (200) times at most, or until DTCN3 (0xfc07c00c, 132 MHz) says 2 s went by: the task is parked, not
| spinning, so the lower-priority tasks (the engine!) keep running. 0x40020c7c(us, flag) reads (static disassembly,
| CONFIRMED): lock/event/PIT1 set-up, PIT1 one-shot of `us` microseconds (an interrupt handler 0x40020d38 signals the
| event), the caller parked on that event (0x40000818), the lock given back; flag 0 = "try the lock, -1 if busy",
| flag 1 = "take it, wait". The stock boot task uses it as a 10 ms sleep inside a polling loop (0x4008046a: sleep
| (10000, 0), poll 0x400009dc, repeat), the ATA power-up as 20 ms / 25 us delays (0x40015fd8). That it is safe from
| the UI task here is a STRONG HYPOTHESIS: the UI task already parks on the same kind of event (the ATA read inside
| SLOTLOAD), the helper only touches its own lock/event/PIT1; the one hazard read in it is that every call RE-INITIALISES
| its lock and event (0x40020c3c), so two tasks inside it at once would lose one wake-up -- only the card power-up
| (0x40015fd8, boot / card insertion) is the other user, and it cannot overlap a script press. Never measured on the unit.
| The emulator (cold detours) has no scheduler: the helper is replaced by a hook there.
|
| Text: names are Latin-1 for the firmware (e-acute = 0xE9; a code point above U+00FF is returned as a short 8.3 name,
| a documented gap) and UTF-8 for the C: dir_next recodes Latin-1 -> UTF-8 (a name longer
| than 259 bytes AFTER the recoding is only counted), dir_open recodes the canonical folder UTF-8 -> Latin-1 (a code
| point above U+00FF or an invalid sequence: -1, absent), slot_set stores the path as Latin-1 (-1 for the same
| reasons, or for more than 259 bytes of UTF-8: the record is then left untouched), slot_name gives the stored
| name back in UTF-8 (its UTF-8 length).
|
| The walker's iterator is the caller's 1024-byte `it` (OTC_IT_BYTES):
|   +0 depth (0 = the opened folder, at most 15)   +4 prefix length   +8 names skipped (> 259 B)
|   +12 deep   +16 done   +20 counting level (99 = none)   +32 ctx[16] x 12 B   +224 saved prefix lengths x 4 B
|   +288 the record (0x140 B)   +608 the path buffer (416 B): the relative prefix "a/b/" while walking
| A folder whose prefix would leave no room for a 1-byte file name inside 259 B is walked in COUNTING mode
| (every file under it is a skipped name, nothing is built). Levels: the opened folder is depth 0; a folder met at
| depth d < 15 is entered (depth d + 1), one met at depth 15 is NOT: a file is listed iff its relative name has at
| most 15 '/' (sim O.DIR_DEPTH_MAX); the files of the folder not entered are neither listed nor counted. The prefix and
| every name are UTF-8 here (lengths in UTF-8 bytes).
        .text
        .globl  otb_host_dir_open
        .globl  otb_host_dir_next
        .globl  otb_host_dir_skipped
        .globl  otb_host_slot_name
        .globl  otb_host_slot_set
        .globl  otb_host_slot_load
        .globl  otb_host_slot_clr
        .globl  otb_host_card_busy
        .globl  otk_sleeps
        .equ    OTK_FS_MUTEX,   0x461079c0
        .equ    OTK_MUTEX_TRY,  0x40000a94      | (mutex) -> 1 taken (ours), 0 held by another task
        .equ    OTK_MUTEX_GIVE, 0x40000ab4
        .equ    OTK_SLEEP,      0x40020c7c      | (us, flag) task delay: parks the caller until PIT1 fires; flag 0 = try its lock
        .equ    OTK_SLEEP_US,   10000           | one sleep: 10 ms (the stock boot task's own poll interval)
        .equ    OTK_CLK,        0xfc07c00c      | DTCN3
        .equ    OTK_2S,         264000000       | 2 s of DTCN3 ticks
        .equ    OTK_PROJDIR,    0x40025230
        .equ    OTK_RESOLVE,    0x4001aa70
        .equ    OTK_ITERINIT,   0x40016f5c
        .equ    OTK_NEXTIN,     0x40019d30
        .equ    OTK_SLOTLOAD,   0x40093980
        .equ    OTK_SLOTPOST,   0x40099148
        .equ    OTK_SLOTFREE,   0x40025288
        .equ    OTK_SLOTREC,   0x100d5b30
        .equ    OTK_STRIDE,     0x448
        .equ    OTK_R_ATTR,     0x10d
        .equ    OTK_R_CLU,      0x11e           | the LONG; +0x120 is only its low half
        .equ    OTK_DEPTH,      0
        .equ    OTK_PLEN,       4
        .equ    OTK_SKIP,       8
        .equ    OTK_DEEP,       12
        .equ    OTK_DONE,       16
        .equ    OTK_CNT,        20
        .equ    OTK_CTX,        32
        .equ    OTK_PLENS,      224
        .equ    OTK_REC,        288
        .equ    OTK_PATH,       608
        .equ    OTK_PBUF,       416
        .equ    OTK_LEVELS,     16
        .equ    OTK_NAMEMAX,    259              | PATHMAX 0x104 - 1: what the loader copies
        .equ    OTK_NONE,       99
        .align  4
otk_sleeps:     .long   200                     | sleeps of 10 ms (2 s); a variable so a test can shorten it

| otk_lock -- d0 = 1: FS_MUTEX is ours now (the caller gives it back); 0: still held elsewhere after ~2 s.
| Try, then (busy) sleep 10 ms and try again, at most otk_sleeps times / 2 s of DTCN3. Clobbers d0, d1, a0, a1.
otk_lock:
        pea     OTK_FS_MUTEX
        jsr     OTK_MUTEX_TRY
        addq.l  #4,%sp
        tst.l   %d0
        bne.s   .Lotkc_otk_l_ret
        lea     -12(%sp),%sp
        movem.l %d2-%d4,(%sp)
        move.l  OTK_CLK,%d3                     | the start
        move.l  otk_sleeps,%d4
.Lotkc_otk_round:
        clr.l   -(%sp)                          | flag 0: the delay's own lock is tried, not waited for
        pea     OTK_SLEEP_US
        jsr     OTK_SLEEP                       | the task is parked here for ~10 ms
        addq.l  #8,%sp
        pea     OTK_FS_MUTEX
        jsr     OTK_MUTEX_TRY
        addq.l  #4,%sp
        tst.l   %d0
        bne.s   .Lotkc_otk_got
        move.l  OTK_CLK,%d0
        sub.l   %d3,%d0                         | unsigned: DTCN3 wraps every 32 s
        cmpi.l  #OTK_2S,%d0
        bcc.s   .Lotkc_otk_none
        subq.l  #1,%d4
        bne.s   .Lotkc_otk_round
.Lotkc_otk_none:
        clr.l   %d0
.Lotkc_otk_got:
        movem.l (%sp),%d2-%d4
        lea     12(%sp),%sp
.Lotkc_otk_l_ret:
        rts

| otk_l2u -- copy the Latin-1 string at a0 to a1 as UTF-8, NUL included (a byte >= 0x80 becomes two bytes). a0 ends
| after the source NUL, a1 after the destination NUL. Clobbers d0, d1.
otk_l2u:
.Lotkc_l2u_l:
        moveq   #0,%d1
        move.b  (%a0)+,%d1
        bmi.s   .Lotkc_l2u_hi
        move.b  %d1,(%a1)+
        bne.s   .Lotkc_l2u_l
        rts
.Lotkc_l2u_hi:
        move.l  %d1,%d0
        lsr.l   #6,%d0                          | 2 or 3
        ori.l   #0xC0,%d0                       | 0xC2 / 0xC3
        move.b  %d0,(%a1)+
        andi.l  #0x3F,%d1
        ori.l   #0x80,%d1
        move.b  %d1,(%a1)+
        bra.s   .Lotkc_l2u_l

| otk_u2l_len -- the Latin-1 length of the UTF-8 string at a0 (not written anywhere), or -1 if some character is not
| representable in Latin-1 (a sequence other than 0xC2/0xC3 + a continuation byte, which is above U+00FF or invalid).
| Clobbers d0, d1, a0.
otk_u2l_len:
        moveq   #0,%d0
.Lotkc_ul_l:
        moveq   #0,%d1
        move.b  (%a0)+,%d1
        beq.s   .Lotkc_ul_end
        bmi.s   .Lotkc_ul_hi
        addq.l  #1,%d0
        bra.s   .Lotkc_ul_l
.Lotkc_ul_hi:
        andi.l  #0xFE,%d1
        cmpi.l  #0xC2,%d1
        bne.s   .Lotkc_ul_bad
        moveq   #0,%d1
        move.b  (%a0)+,%d1
        subi.l  #0x80,%d1
        cmpi.l  #0x3F,%d1                       | unsigned: a continuation byte 0x80..0xBF only (a NUL fails too)
        bhi.s   .Lotkc_ul_bad
        addq.l  #1,%d0
        bra.s   .Lotkc_ul_l
.Lotkc_ul_bad:
        moveq   #-1,%d0
.Lotkc_ul_end:
        rts

| otk_u2l -- copy the UTF-8 string at a0 to a1 as Latin-1, NUL included. Only for a string otk_u2l_len accepted.
| Clobbers d0, d1, a0, a1.
otk_u2l:
.Lotkc_u2l_l:
        moveq   #0,%d1
        move.b  (%a0)+,%d1
        bmi.s   .Lotkc_u2l_hi
        move.b  %d1,(%a1)+
        bne.s   .Lotkc_u2l_l
        rts
.Lotkc_u2l_hi:
        move.l  %d1,%d0
        andi.l  #3,%d0
        lsl.l   #6,%d0                          | 0x80 / 0xC0
        moveq   #0,%d1
        move.b  (%a0)+,%d1
        andi.l  #0x3F,%d1
        or.l    %d1,%d0
        move.b  %d0,(%a1)+
        bra.s   .Lotkc_u2l_l

| otk_give -- FS_MUTEX back. Keeps d2-d7 / a2-a6.
otk_give:
        pea     OTK_FS_MUTEX
        jsr     OTK_MUTEX_GIVE
        addq.l  #4,%sp
        rts

| int32_t otb_host_card_busy(uint32_t op) -- the C asks before every card operation: 0 = free (tried and given
| back this instant), 1 = still held after ~2 s of tries.
otb_host_card_busy:
        bsr.w   otk_lock
        tst.l   %d0
        beq.s   .Lotkc_cb_busy
        bsr.w   otk_give
        clr.l   %d0
        rts
.Lotkc_cb_busy:
        moveq   #1,%d0
        rts

| int32_t otb_host_dir_open(void *it, const char *canon, uint32_t deep)
|   0 ok, -1 absent, -2 busy. Resolves PROJDIR + "/.." [+ "/" + canon]: the Set folder is the project's parent.
otb_host_dir_open:
        lea     -24(%sp),%sp
        movem.l %d2-%d4/%a2-%a4,(%sp)
        movea.l 28(%sp),%a2                     | it
        movea.l 32(%sp),%a3                     | canon
        move.l  36(%sp),%d2                     | deep
        clr.l   OTK_DEPTH(%a2)
        clr.l   OTK_PLEN(%a2)
        clr.l   OTK_SKIP(%a2)
        clr.l   OTK_DONE(%a2)
        moveq   #OTK_NONE,%d0
        move.l  %d0,OTK_CNT(%a2)
        tst.l   %d2
        sne     %d0
        moveq   #1,%d1
        and.l   %d1,%d0
        move.l  %d0,OTK_DEEP(%a2)
        clr.l   -(%sp)                          | PROJDIR(0, 0)
        clr.l   -(%sp)
        jsr     OTK_PROJDIR
        addq.l  #8,%sp
        movea.l %d0,%a4                         | the project's path
        moveq   #0,%d3                          | length of the project's path
.Lotkc_od_pl:
        tst.b   (%a4,%d3.l)
        beq.s   .Lotkc_od_pl_end
        addq.l  #1,%d3
        bra.s   .Lotkc_od_pl
.Lotkc_od_pl_end:
        moveq   #0,%d4                          | length of canon
.Lotkc_od_cl:
        tst.b   (%a3,%d4.l)
        beq.s   .Lotkc_od_cl_end
        addq.l  #1,%d4
        bra.s   .Lotkc_od_cl
.Lotkc_od_cl_end:
        move.l  %d3,%d0
        add.l   %d4,%d0
        addi.l  #5,%d0                          | "/.." + "/" + NUL, at most
        cmpi.l  #OTK_PBUF,%d0
        bgt.w   .Lotkc_od_absent                     | does not fit: not a path we can ask for
        lea     OTK_PATH(%a2),%a0
.Lotkc_od_cp1:
        move.b  (%a4)+,%d0
        move.b  %d0,(%a0)+
        bne.s   .Lotkc_od_cp1
        subq.l  #1,%a0                          | on the NUL
        move.b  #'/',(%a0)+
        move.b  #'.',(%a0)+
        move.b  #'.',(%a0)+
        tst.l   %d4
        beq.s   .Lotkc_od_term
        move.b  #'/',(%a0)+
        movea.l %a0,%a1                         | the folder as the firmware knows it: Latin-1
        movea.l %a3,%a0
        bsr.w   otk_u2l_len
        tst.l   %d0
        bmi.w   .Lotkc_od_absent                | a code point above U+00FF (or invalid UTF-8): not a folder we can ask for
        movea.l %a3,%a0
        bsr.w   otk_u2l
        bra.s   .Lotkc_od_resolve
.Lotkc_od_term:
        clr.b   (%a0)
.Lotkc_od_resolve:
        bsr.w   otk_lock
        tst.l   %d0
        beq.w   .Lotkc_od_busy
        pea     OTK_REC(%a2)
        pea     OTK_PATH(%a2)
        jsr     OTK_RESOLVE
        addq.l  #8,%sp
        move.l  %d0,%d3
        bsr.w   otk_give
        tst.l   %d3
        bne.s   .Lotkc_od_absent                     | no such folder: an empty list
        move.b  OTK_REC+OTK_R_ATTR(%a2),%d0
        btst    #4,%d0
        beq.s   .Lotkc_od_absent                     | a file of that name
        pea     OTK_CTX(%a2)
        move.l  OTK_REC+OTK_R_CLU(%a2),-(%sp)   | the whole cluster: past 65535 on a FAT32 card
        jsr     OTK_ITERINIT                    | returns -1; that is not a failure
        addq.l  #8,%sp
        clr.l   %d0
        bra.s   .Lotkc_od_out
.Lotkc_od_absent:
        moveq   #-1,%d0
        bra.s   .Lotkc_od_out
.Lotkc_od_busy:
        moveq   #-2,%d0
.Lotkc_od_out:
        movem.l (%sp),%d2-%d4/%a2-%a4
        lea     24(%sp),%sp
        rts

| int32_t otb_host_dir_next(void *it, char *out260) -- 1 a relative file name in out260, 0 the end, -2 busy.
otb_host_dir_next:
        lea     -28(%sp),%sp
        movem.l %d2-%d6/%a2-%a3,(%sp)
        movea.l 32(%sp),%a2                     | it
        movea.l 36(%sp),%a3                     | out260
.Lotkc_dn_loop:
        tst.l   OTK_DONE(%a2)
        bne.w   .Lotkc_dn_end
        move.l  OTK_DEPTH(%a2),%d2
        move.l  %d2,%d0                         | the level's ctx: it + OTK_CTX + depth * 12
        add.l   %d2,%d0
        add.l   %d2,%d0
        lsl.l   #2,%d0
        lea     OTK_CTX(%a2),%a0
        adda.l  %d0,%a0
        move.l  %a0,%d5                         | d5 the ctx
        bsr.w   otk_lock
        tst.l   %d0
        beq.w   .Lotkc_dn_busy
        pea     OTK_REC(%a2)
        move.l  %d5,-(%sp)
        jsr     OTK_NEXTIN
        addq.l  #8,%sp
        move.l  %d0,%d6
        bsr.w   otk_give
        tst.l   %d6
        bne.w   .Lotkc_dn_pop                        | the end of this folder
        move.b  OTK_REC(%a2),%d0                | "." ".." "._x" ".hidden": never listed, never entered
        cmpi.b  #'.',%d0
        beq.s   .Lotkc_dn_loop
        move.b  OTK_REC+OTK_R_ATTR(%a2),%d1
        btst    #3,%d1                          | a volume label
        bne.s   .Lotkc_dn_loop
        lea     OTK_REC(%a2),%a0
        moveq   #0,%d3                          | d3 = the name's length AS UTF-8 (a byte >= 0x80 counts twice)
.Lotkc_dn_len:
        move.b  (%a0)+,%d0
        beq.s   .Lotkc_dn_len_end
        addq.l  #1,%d3
        tst.b   %d0
        bpl.s   .Lotkc_dn_len
        addq.l  #1,%d3
        bra.s   .Lotkc_dn_len
.Lotkc_dn_len_end:
        btst    #4,%d1
        bne.s   .Lotkc_dn_dir
        | ---- a file --------------------------------------------------------
        move.l  OTK_CNT(%a2),%d0                | (no cmpi #imm,(d16,An) on ColdFire)
        cmpi.l  #OTK_NONE,%d0
        bne.s   .Lotkc_dn_skip                       | under a prefix with no room: only counted
        move.l  OTK_PLEN(%a2),%d4
        move.l  %d4,%d0
        add.l   %d3,%d0
        cmpi.l  #OTK_NAMEMAX,%d0
        bgt.s   .Lotkc_dn_skip                       | more than 259 bytes: counted, not given
        lea     OTK_PATH(%a2),%a0
        movea.l %a3,%a1
        move.l  %d4,%d1                         | the prefix
        beq.s   .Lotkc_dn_pfx_done
.Lotkc_dn_pfx:
        move.b  (%a0)+,(%a1)+
        subq.l  #1,%d1
        bne.s   .Lotkc_dn_pfx
.Lotkc_dn_pfx_done:
        lea     OTK_REC(%a2),%a0
        bsr.w   otk_l2u                         | the name, recoded to UTF-8 (<= 259 bytes: checked above)
        moveq   #1,%d0
        bra.w   .Lotkc_dn_out
.Lotkc_dn_skip:
        addq.l  #1,OTK_SKIP(%a2)
        bra.w   .Lotkc_dn_loop
        | ---- a folder -------------------------------------------------------
.Lotkc_dn_dir:
        tst.l   OTK_DEEP(%a2)
        beq.w   .Lotkc_dn_loop                       | not deep: folders are not listed
        cmpi.l  #OTK_LEVELS-1,%d2
        bge.w   .Lotkc_dn_loop                       | in a folder 15 levels below the listed one: no deeper (O.DIR_DEPTH_MAX)
        move.l  OTK_PLEN(%a2),%d4
        move.l  %d2,%d0                         | the prefix length to come back to
        lsl.l   #2,%d0
        lea     OTK_PLENS(%a2),%a0
        adda.l  %d0,%a0
        move.l  %d4,(%a0)
        move.l  OTK_CNT(%a2),%d0                | (no cmpi #imm,(d16,An) on ColdFire)
        cmpi.l  #OTK_NONE,%d0
        bne.s   .Lotkc_dn_down                       | already counting
        move.l  %d4,%d0
        add.l   %d3,%d0
        addq.l  #1,%d0                          | the new prefix, "name/"
        cmpi.l  #OTK_NAMEMAX-1,%d0
        bgt.s   .Lotkc_dn_count                      | no 1-byte name fits under it
        lea     OTK_PATH(%a2),%a1
        adda.l  %d4,%a1
        lea     OTK_REC(%a2),%a0
        bsr.w   otk_l2u                         | the folder's name, UTF-8, into the prefix
        subq.l  #1,%a1
        move.b  #'/',(%a1)+
        clr.b   (%a1)
        move.l  %d4,%d0                         | otk_l2u clobbered d0: the new prefix length again
        add.l   %d3,%d0
        addq.l  #1,%d0
        move.l  %d0,OTK_PLEN(%a2)
        bra.s   .Lotkc_dn_down
.Lotkc_dn_count:
        move.l  %d2,%d0
        addq.l  #1,%d0
        move.l  %d0,OTK_CNT(%a2)                | counting from the level we are about to enter
.Lotkc_dn_down:
        move.l  OTK_REC+OTK_R_CLU(%a2),%d4
        addq.l  #1,%d2
        move.l  %d2,OTK_DEPTH(%a2)
        move.l  %d2,%d0
        add.l   %d2,%d0
        add.l   %d2,%d0
        lsl.l   #2,%d0
        lea     OTK_CTX(%a2),%a0
        adda.l  %d0,%a0
        move.l  %a0,-(%sp)
        move.l  %d4,-(%sp)
        jsr     OTK_ITERINIT
        addq.l  #8,%sp
        bra.w   .Lotkc_dn_loop
        | ---- the end of a folder ---------------------------------------------
.Lotkc_dn_pop:
        tst.l   %d2
        bne.s   .Lotkc_dn_up
        moveq   #1,%d0
        move.l  %d0,OTK_DONE(%a2)
        bra.s   .Lotkc_dn_end
.Lotkc_dn_up:
        subq.l  #1,%d2
        move.l  %d2,OTK_DEPTH(%a2)
        move.l  %d2,%d0
        lsl.l   #2,%d0
        lea     OTK_PLENS(%a2),%a0
        adda.l  %d0,%a0
        move.l  (%a0),%d4
        move.l  %d4,OTK_PLEN(%a2)
        lea     OTK_PATH(%a2),%a0
        clr.b   (%a0,%d4.l)
        cmp.l   OTK_CNT(%a2),%d2                | back above the level the counting began at
        bge.w   .Lotkc_dn_loop
        moveq   #OTK_NONE,%d0
        move.l  %d0,OTK_CNT(%a2)
        bra.w   .Lotkc_dn_loop
.Lotkc_dn_end:
        clr.l   %d0
        bra.s   .Lotkc_dn_out
.Lotkc_dn_busy:
        moveq   #-2,%d0
.Lotkc_dn_out:
        movem.l (%sp),%d2-%d6/%a2-%a3
        lea     28(%sp),%sp
        rts

| uint32_t otb_host_dir_skipped(void *it)
otb_host_dir_skipped:
        movea.l 4(%sp),%a0
        move.l  OTK_SKIP(%a0),%d0
        rts

| otk_slotp -- d0 = slot0: a0 = its slot record (the stored name is at +0), d0 clobbered; d1 = 1 if the
| slot is outside 0..127 (a0 then undefined). Clobbers d0, d1, a0.
otk_slotp:
        cmpi.l  #128,%d0
        bcc.s   .Lotkc_sp_bad
        move.l  #OTK_STRIDE,%d1                 | no mulu.l immediate on ColdFire
        mulu.l  %d1,%d0
        movea.l #OTK_SLOTREC,%a0
        adda.l  %d0,%a0
        clr.l   %d1
        rts
.Lotkc_sp_bad:
        moveq   #1,%d1
        rts

| int32_t otb_host_slot_name(uint32_t slot0, char *out, uint32_t max)
|   the UTF-8 length of the stored path (stored as Latin-1), 0 = free; when out != 0 and max != 0 the path is copied
|   as UTF-8, at most max-1 bytes + NUL, never half of a two-byte character.
otb_host_slot_name:
        lea     -8(%sp),%sp
        movem.l %d2-%d3,(%sp)
        move.l  12(%sp),%d0
        bsr.w   otk_slotp
        tst.l   %d1
        bne.s   .Lotkc_sn_zero
        moveq   #0,%d0                          | d0 = the UTF-8 length, d2 = the index (at most 260 bytes read)
        moveq   #0,%d2
.Lotkc_sn_len:
        move.b  (%a0,%d2.l),%d1
        beq.s   .Lotkc_sn_len_end
        addq.l  #1,%d0
        tst.b   %d1
        bpl.s   .Lotkc_sn_len_next
        addq.l  #1,%d0
.Lotkc_sn_len_next:
        addq.l  #1,%d2
        cmpi.l  #OTK_NAMEMAX+1,%d2              | a name field without a NUL: stop (never meant to happen)
        blt.s   .Lotkc_sn_len
.Lotkc_sn_len_end:
        movea.l 16(%sp),%a1
        move.l  %a1,%d1
        beq.s   .Lotkc_sn_ret
        move.l  20(%sp),%d1
        beq.s   .Lotkc_sn_ret
        subq.l  #1,%d1                          | d1 = the room left, without the NUL
.Lotkc_sn_cp:
        moveq   #0,%d2
        move.b  (%a0)+,%d2
        beq.s   .Lotkc_sn_term
        tst.b   %d2
        bmi.s   .Lotkc_sn_cp2
        tst.l   %d1
        beq.s   .Lotkc_sn_term
        move.b  %d2,(%a1)+
        subq.l  #1,%d1
        bra.s   .Lotkc_sn_cp
.Lotkc_sn_cp2:
        cmpi.l  #2,%d1
        blt.s   .Lotkc_sn_term                  | no room for both bytes
        move.l  %d2,%d3
        lsr.l   #6,%d3
        ori.l   #0xC0,%d3
        move.b  %d3,(%a1)+
        andi.l  #0x3F,%d2
        ori.l   #0x80,%d2
        move.b  %d2,(%a1)+
        subq.l  #2,%d1
        bra.s   .Lotkc_sn_cp
.Lotkc_sn_term:
        clr.b   (%a1)
.Lotkc_sn_ret:
        movem.l (%sp),%d2-%d3
        lea     8(%sp),%sp
        rts
.Lotkc_sn_zero:
        clr.l   %d0
        bra.s   .Lotkc_sn_ret

| int32_t otb_host_slot_set(uint32_t slot0, const char *path) -- 0 stored, -1 the unit cannot store it: outside
| 0..127, longer than 259 bytes (PATHMAX 0x104 is what the loader copies), or holding a character that is not Latin-1
| (above U+00FF, or invalid UTF-8). The path is stored as Latin-1, what the firmware opens; on -1 the record is untouched.
otb_host_slot_set:
        move.l  %a2,-(%sp)
        move.l  8(%sp),%d0
        bsr.w   otk_slotp
        tst.l   %d1
        bne.s   .Lotkc_ss_bad
        movea.l %a0,%a2                         | the record
        movea.l 12(%sp),%a1
        moveq   #0,%d0
.Lotkc_ss_len:
        tst.b   (%a1,%d0.l)
        beq.s   .Lotkc_ss_len_end
        addq.l  #1,%d0
        cmpi.l  #OTK_NAMEMAX,%d0
        ble.s   .Lotkc_ss_len
        bra.s   .Lotkc_ss_bad                   | 260 bytes or more
.Lotkc_ss_len_end:
        movea.l 12(%sp),%a0
        bsr.w   otk_u2l_len
        tst.l   %d0
        bmi.s   .Lotkc_ss_bad                   | not Latin-1
        movea.l 12(%sp),%a0
        movea.l %a2,%a1
        bsr.w   otk_u2l
        clr.l   %d0
        bra.s   .Lotkc_ss_out
.Lotkc_ss_bad:
        moveq   #-1,%d0
.Lotkc_ss_out:
        movea.l (%sp)+,%a2
        rts

| int32_t otb_host_slot_load(uint32_t slot0) -- SLOTLOAD(slot, 0); == 1: SLOTPOST(0, slot) and
| 1; else FUN_40025288(0, slot) (the loader's handle and the name go) and 0. TRACKSYNC / UIREFRESH are NOT here:
| run.S does them once per press.
otb_host_slot_load:
        move.l  %d2,-(%sp)
        move.l  8(%sp),%d2
        cmpi.l  #128,%d2
        bcc.s   .Lotkc_sl_refused_nofree
        clr.l   -(%sp)                          | keep_trim = 0: derive the window
        move.l  %d2,-(%sp)
        jsr     OTK_SLOTLOAD
        addq.l  #8,%sp
        moveq   #1,%d1
        cmp.l   %d0,%d1
        bne.s   .Lotkc_sl_refused
        move.l  %d2,-(%sp)                      | SLOTPOST(0, slot): the half of a load the storage job does after it
        clr.l   -(%sp)
        jsr     OTK_SLOTPOST
        addq.l  #8,%sp
        moveq   #1,%d0
        bra.s   .Lotkc_sl_out
.Lotkc_sl_refused:
        move.l  %d2,-(%sp)                      | leave no trace behind (.Lslot_clear)
        clr.l   -(%sp)
        jsr     OTK_SLOTFREE
        addq.l  #8,%sp
.Lotkc_sl_refused_nofree:
        clr.l   %d0
.Lotkc_sl_out:
        move.l  (%sp)+,%d2
        rts

| void otb_host_slot_clr(uint32_t slot0) -- FUN_40025288(0, slot) when the slot holds a name; an empty slot is
| not touched.
otb_host_slot_clr:
        move.l  4(%sp),%d0
        movea.l %d0,%a1
        bsr.w   otk_slotp
        tst.l   %d1
        bne.s   .Lotkc_sc_out
        tst.b   (%a0)
        beq.s   .Lotkc_sc_out
        move.l  %a1,-(%sp)
        clr.l   -(%sp)
        jsr     OTK_SLOTFREE
        addq.l  #8,%sp
.Lotkc_sc_out:
        rts

| int32_t otb_host_scan_dirs(const char *path, char *out_names, uint32_t max_entries)
|   scans the directory PROJDIR/../path (e.g. AUDIO or AUDIO/DRUMS) on CF card,
|   and writes subdirectories into out_names (max_entries * 16 bytes).
|   Returns number of subdirectories found (0..max_entries).
        .globl  otb_host_scan_dirs
otb_host_scan_dirs:
        lea     -600(%sp),%sp
        movem.l %d2-%d7/%a2-%a5,(%sp)
        movea.l 604(%sp),%a3                    | path
        movea.l 608(%sp),%a4                    | out_names
        move.l  612(%sp),%d7                    | max_entries
        clr.l   %d2                             | count = 0
        tst.l   %d7
        ble.w   .Lotkc_sd_done
        | Resolve path: PROJDIR(0, 0) + "/.." + "/" + path
        clr.l   -(%sp)
        clr.l   -(%sp)
        jsr     OTK_PROJDIR
        addq.l  #8,%sp
        movea.l %d0,%a5                         | projdir
        lea     52(%sp),%a0                     | a0 = path buffer (228 B at sp+52)
.Lotkc_sd_cp1:
        move.b  (%a5)+,%d0
        move.b  %d0,(%a0)+
        bne.s   .Lotkc_sd_cp1
        subq.l  #1,%a0
        move.b  #'/',(%a0)+
        move.b  #'.',(%a0)+
        move.b  #'.',(%a0)+
        move.b  #'/',(%a0)+
.Lotkc_sd_cp2:
        move.b  (%a3)+,%d0
        move.b  %d0,(%a0)+
        bne.s   .Lotkc_sd_cp2
        | Resolve directory
        bsr.w   otk_lock
        tst.l   %d0
        beq.w   .Lotkc_sd_done
        lea     280(%sp),%a2                    | a2 = otk_rec (0x140 = 320 B at sp+280)
        pea     (%a2)
        lea     56(%sp),%a0                     | sp+52 adjusted by pea = sp+56
        pea     (%a0)
        jsr     OTK_RESOLVE
        addq.l  #8,%sp
        move.l  %d0,%d3
        bsr.w   otk_give
        tst.l   %d3
        bne.w   .Lotkc_sd_done                  | resolve failed
        move.b  OTK_R_ATTR(%a2),%d0
        btst    #4,%d0
        beq.w   .Lotkc_sd_done                  | not a directory
        | Init iterator: ctx at sp+40 (12 B)
        lea     40(%sp),%a5                     | ctx
        pea     (%a5)
        move.l  OTK_R_CLU(%a2),-(%sp)           | first cluster (long at +0x11e)
        jsr     OTK_ITERINIT
        addq.l  #8,%sp
.Lotkc_sd_loop:
        cmp.l   %d7,%d2
        bge.s   .Lotkc_sd_done                  | reached max_entries
        bsr.w   otk_lock
        tst.l   %d0
        beq.s   .Lotkc_sd_done
        pea     (%a2)
        pea     (%a5)
        jsr     OTK_NEXTIN
        addq.l  #8,%sp
        move.l  %d0,%d3
        bsr.w   otk_give
        tst.l   %d3
        bne.s   .Lotkc_sd_done                  | end of dir
        | Check entry
        move.b  (%a2),%d0
        cmpi.b  #'.',%d0                        | skip '.', '..', hidden
        beq.s   .Lotkc_sd_loop
        move.b  OTK_R_ATTR(%a2),%d0
        btst    #4,%d0                          | directory?
        beq.s   .Lotkc_sd_loop                  | skip non-directory files
        btst    #3,%d0                          | volume label?
        bne.s   .Lotkc_sd_loop
        | Copy name into out_names (16 bytes per entry: max 15 chars + NUL)
        move.l  %d2,%d0
        lsl.l   #4,%d0                          | d0 = count * 16
        lea     (%a4,%d0.l),%a1                 | dest
        movea.l %a2,%a0                         | src
        moveq   #14,%d4                         | up to 15 chars
.Lotkc_sd_name:
        move.b  (%a0)+,%d0
        beq.s   .Lotkc_sd_nul
        move.b  %d0,(%a1)+
        subq.l  #1,%d4
        bpl.s   .Lotkc_sd_name
        clr.b   (%a1)
        bra.s   .Lotkc_sd_inc
.Lotkc_sd_nul:
        clr.b   (%a1)
.Lotkc_sd_inc:
        addq.l  #1,%d2
        bra.s   .Lotkc_sd_loop
.Lotkc_sd_done:
        move.l  %d2,%d0                         | return count
        movem.l (%sp),%d2-%d7/%a2-%a5
        lea     600(%sp),%sp
        rts
