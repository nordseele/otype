/* OType bytecode VM: public interface and the VM state.
 *
 * The VM runs an OTB table and is proven identical to the
 * Python reference VM (modules/otype/vm.py, sim.py) by tools/test_otype_cvm.py. The
 * same C runs on the ColdFire, so:
 *   - no global or static writable variable: ALL state lives in the caller's otc_vm_t;
 *   - only <stdint.h>; no libc, no 64-bit arithmetic, no float;
 *   - the table is read byte by byte, big-endian (host and target agree);
 *   - no recursion: a call is a frame in the struct.
 *
 * Supported: every opcode of 14.3, every registry operation of level "vm", plus trk.cur and pat.len
 * (see otc_ops.h, generated). Anything else stops the run with OTC_E_UNSUPPORTED.
 * The card / slot operations (dir ext match file.* static.* slots.*) need the workspace
 * bound with otc_vm_bind_work; without it they stop the run with OTC_E_UNSUPPORTED, so a table that
 * does not use them runs exactly as before.
 *
 * The trig / sample-lock operations (tr.add tr.del tr.flip tr.clr has.trig has.lock has.smp
 * is.empty trk.type smp.set smp.get smp.del smp.rnd smp.clr and `steps <predicate>`). They read and
 * WRITE the pattern's own bytes, in place: the caller binds the TRAC block (RAM layout, below) of each
 * track with otc_vm_bind, after otc_vm_load and before each run. A track that is not bound makes
 * these operations stop the run with OTC_E_UNSUPPORTED, so a table that does not use them runs
 * exactly as without any binding. Mirrors modules/otype/sim.py (TrackData.remove_trig /
 * add_trig, Run.each_track / lands, the SMP.* / TR.* / HAS.* handlers).
 *
 * TRAC block of one track in RAM (0x91a bytes; sim.read_bank describes the same data in the bank file):
 *   +0x00 +0x08 +0x10 +0x18   masks trig (red), tless (trigless trig), tlock (trigless lock), oneshot:
 *                             8 bytes each, big-endian 64 bit, bit (s-1) = step s
 *   +0x59 + (s-1)*0x20        step record, 32 bytes: 0..29 p-locks (0xff = none), 30 unused,
 *                             31 sample lock (0xff = none, else slot - 1)
 *   +0x89a + (s-1)*2          trig word, big-endian 16 bit: bits 15-13 count-1, 12-7 micro (signed 6),
 *                             6-0 condition. (In the bank FILE the word is one byte earlier.)
 * Writes (the three rules): removing a trig (a trig-family bit was set)
 * clears the three family bits and, unless a trigless lock keeps the step, sets the 32 record bytes
 * to 0xff and does word_low &= 0x80; adding on a one-shot / trigless trig converts it to a red trig
 * (bits cleared / set, record and word kept), adding on a step without a family trig sets the red bit
 * and zeroes the word; a PICKUP track (mtype 4) refuses every write (skipped before its steps are
 * visited, no draw, recorded in the "pickup" list); a lock / sample-lock write on a step without any
 * trig of the four masks is skipped and recorded in the "no trig" list.
 *
 * The p-lock / trigless-lock operations (pl.set pl.get pl.del pl.rnd pl.vary pl.clr tl.add tl.fill) on the same
 * bound blocks: a p-lock is byte p (0..29) of the step's 32-byte record, 0xff = none; a trigless lock is the +0x10 mask.
 * pl.set / pl.rnd / pl.vary clamp / draw into the part's descriptor range and refuse a disabled slot (out of range,
 * counted, before any step is visited), through the two otb_host_param_* imports below; pl.get answers the part's
 * value where the step holds no lock. Mirrors sim.py (_lock_range, _plset ... _tlfill, TrackData.add_tlock).
 *
 * The part operations (par.set lfo.dest fx.type scn.clr trk.init):
 * The part is never bound: every read and write goes through the otb_host_* part imports below (the stock parameter writer,
 * direct part bytes, the FX chooser apply, all on the unit; host_shim.c on the Mac), on the tracks of the context, without
 * the PICKUP gate (a pattern rule). par.set clamps into the descriptor (otb_host_param_info) and refuses a disabled slot
 * (out of range, once per track); fx.type reads the chooser's
 * length at runtime; trk.init reads the descriptor defaults and the factory effects. `part_touched` (bit t-1) says that
 * a part write import was called for track t: the bridge commits the part once (stock part setter + refresh) after the run.
 * Mirrors sim.py (_parset ... _trkinit, ParamModel / PartData).
 *
 * the card / slot operations. Their state (names arena of 4096 x 128 bytes, sorted index, one
 * file list per frame, the pick bags, working set, path buffers, the walker's iterator) lives in the
 * caller's WORKSPACE (otc_ws_size() bytes, 8-aligned), not in otc_vm_t. The card and the unit's slots
 * are reached ONLY through the otb_host_* imports below (asm on the unit, function pointers in
 * host_shim.c on the Mac): the C is the owner of the lists, the host is a generator of names and the
 * truth about the slots (static.free asks the unit, no local copy). Mirrors modules/otype/sim.py
 * (fold, ext_key, canon_path, slot_path, list_card, FileList, Run.load_slot, the DIR ... STATIC.CLR
 * handlers) and modules/otype/vm.py (list copy on call).
 *
 * Use:  otc_vm_t *vm = (caller memory of otc_vm_sizeof() bytes);
 *       otc_vm_load(vm, table, len);              (resets the persistent state AND all bindings)
 *       otc_vm_bind_work(vm, ws);                 (card / slot ops; survives otc_vm_load, kept
 *                                                  by a second bind of the same area)
 *       otc_vm_bind(vm, track, trac, copy, mtype);(per track 1..8, each time before a run)
 *       otc_vm_run(vm, func, seed, track, len);   (one press; the state persists to the next)
 *       otc_vm_export(vm, words, n);              (flat result record, below)
 *       otc_vm_touched(vm);                       (bit t-1: a write of the run changed a byte of track t)
 *       otc_vm_slots_touched(vm);                 (!= 0: the run called a slot_set / slot_load / slot_clr)
 *
 * EXPORT RECORD (int32 words, in this order; `otc_vm_export` returns the number of words the
 * record needs and writes at most max_words of them):
 *   [0]      error: 0 = ran to the end, else OTC_E_*
 *   [1]      cost (registry units, as sim.Run.cost)
 *   [2]      instructions executed
 *   [3..7]   draws of rand, toss, prob, drunk, file
 *   [8..11]  globals x y z t
 *   [12]     n_oor: out-of-range records kept (at most OTC_MAX_OOR)
 *   [13]     overflow: records dropped after the first OTC_MAX_OOR
 *   [14..]   n_oor x (function index, (op ns << 8) | op num, source line)
 *   then     nfunc
 *   then     per function of the table:
 *              touched (1 if the persistent state of the function exists: Python's fstate()),
 *              flip, o, o.min, o.max, o.wrap, o.inc, drunk, drunk.min, drunk.max, drunk.wrap,
 *              nctr (every/skip counters of the function),
 *              nctr x (present, value)   present = the counter was evaluated at least once
 *   then     n_skip_notrig, overflow, n x (function index, (op ns << 8) | op num, source line)
 *              (sim skipped_no_trig; at most OTC_MAX_OOR kept, the rest only counted in overflow)
 *   then     n_skip_pickup, overflow, n x (function index, (op ns << 8) | op num, source line, track)
 *              (sim skipped_pickup; same cap)
 *   then     touched: bit t-1 set = a write of the run changed a byte of track t (a write of the value
 *              already there does not count; add then delete may leave the bytes as they were and
 *              still counts)
 *   then     part_touched: bit t-1 set = the run called a part write import for track t
 *   card / slot tail (always present, after `touched`):
 *   then     bound: 1 if the workspace is bound, else 0 and nothing follows
 *   then     slots_touched (0 / 1)
 *   then     n_warn, overflow, n x (kind, N, cap, path_len, n_words, words): the `dir` warnings of the run,
 *              deduplicated by text (sim.warnings). kind 0: `dir "P": N names longer than 127 characters
 *              skipped`; kind 1: `dir "P": N files, only the first cap kept`. P = the canonical path,
 *              path_len bytes packed 4 per word, big-endian, zero padded (n_words = ceil(path_len/4)).
 *              At most OTC_MAX_DIRWARN kept, the rest counted in overflow.
 *   then     n_work, n_work x slot (1..128): the working set (slots.add), in order (sim Model.working)
 *   then     n_bags, n x (function index, hash, n, n_rest, ceil(n_rest/2) words): the pick bags, OLDEST
 *              FIRST (by insertion; reuse does not refresh the age), at most OTC_BAGS_MAX; `rest` values
 *              1..4096 packed two per word, the first in the high half (sim state "bags": func, hash, n, rest)
 *   then     n_ref, overflow, n x (function index, (op ns << 8) | op num, source line, slot (1..128),
 *              path_len, n_words, words): the loads the unit refused (sim refused), path = the canonical
 *              path (without the unit's "../"), packed like the warnings. At most OTC_MAX_REFUSED kept.
 */
#ifndef OTC_VM_H
#define OTC_VM_H

#include <stdint.h>

/* ---- caps (the table's declared max_stack / max_ctx / max_ctl must fit; otc_vm_load checks) --- */
#define OTC_MAX_FRAMES   8       /* call depth, the root included (spec 10) */
#define OTC_MAX_STACK    16
#define OTC_MAX_LOCALS   4
#define OTC_MAX_ARGS     4
#define OTC_MAX_PARAMS   8       /* params of one function (spec 4); a page function has at most OTC_PAGE_PARAMS */
#define OTC_PAGE_PARAMS  6
#define OTC_PAGE_LABEL   8       /* longest page label */
#define OTC_CONFIRM_TEXT 127     /* longest `confirm "TEXT"` (a string constant, spec 2) */
#define OTC_FL_CONFIRM_TEXT 0x04 /* function entry flags (byte 23): the confirm text's pool offset ends the aux data */
#define OTC_FL_TYPED_PARAMS 0x08 /* function entry flags: typed parameters auxiliary tail present */
#define OTC_FL_INPUT_SAMPLE 0x10 /* function entry flags: input sample declared */
#define OTC_PK_INT        0
#define OTC_PK_BOOL       1
#define OTC_PK_OPTION     2
#define OTC_PK_FOLDER     3
#define OTC_MAX_CTX      16      /* context scopes per frame */
#define OTC_MAX_CTL      16      /* control frames per frame */
#define OTC_MAX_PLIST    8
#define OTC_MAX_FUNCS    255
#define OTC_MAX_COUNTERS 1024    /* every/skip counters over all functions */
#define OTC_MSG_BUF      64      /* bytes of the msg text buffer; ops.MAX_MSG (OTC_MSG_MAX of otc_ops.h) must stay below it: a bigger end box
                                 * changes ONLY ops.MAX_MSG, until it reaches 63 (otc_ops.h #error beyond) */
#define OTC_MAX_OOR      64      /* also the cap of the two skip lists */
#define OTC_PICKUP       4       /* sim.PICKUP: the machine type of a track that refuses writes */
#define OTC_NSTREAMS     5       /* rand toss prob drunk file */
/* One slot per (live context x stream): at most 8*16 contexts are alive at once, and a slot that
 * no live context references is free again (alloc_slot scans the contexts), so they cannot run out. */
#define OTC_STREAM_SLOTS (OTC_MAX_FRAMES * OTC_MAX_CTX * OTC_NSTREAMS)
#ifndef OTC_MAX_COST                  /* (the test builds a small one to check the boundary) */
#define OTC_MAX_COST     2000000u    /* sim.MAX_COST: "run too long" */
#endif
#define OTC_MAX_INSNS    50000000u   /* safety net against a corrupt table that loops (uncharged) */

/* ---- error codes ----------------------------------------------------------------------------- */
#define OTC_E_COST        1   /* cost > OTC_MAX_COST (Python RunError "run too long"), or the insn net */
#define OTC_E_TABLE       2   /* corrupt table / VMError: bad opcode, empty-stack pop, bad pool offset,
                               * depth beyond the declared numbers, call depth > 8, break outside a loop */
#define OTC_E_UNSUPPORTED 3   /* a valid construct that the VM does not run */
#define OTC_E_ARG         4   /* bad argument of otc_vm_run (function, track) */
#define OTC_E_CAPS        5   /* otc_vm_load: the table is fine but exceeds a cap */
#define OTC_E_BUSY        6   /* a card access found the card busy (otb_host_card_busy, or the walker):
                               * the sim's CARD BUSY; nothing already done is undone, the export record
                               * is complete (it is the sim's CardBusy.result) */

/* ---- audio error codes (-1..-13) ------------------------------------------------ */
#define OTC_AE_BAD_ARG     -1
#define OTC_AE_CARD_BUSY   -2
#define OTC_AE_NO_SOURCE   -3
#define OTC_AE_EMPTY       -4
#define OTC_AE_FLEX_FULL   -5
#define OTC_AE_STOP_FIRST  -6
#define OTC_AE_EXISTS      -7
#define OTC_AE_FORMAT      -8
#define OTC_AE_CARD_FULL   -9
#define OTC_AE_IO_ERROR    -10
#define OTC_AE_TIMEOUT     -11
#define OTC_AE_CAT_FULL    -12
#define OTC_AE_TOO_LONG    -13

/* ---- the card workspace -------------------------------------------------------------- */
#define OTC_NAMES_MAX    4096    /* sim O.DIR_LIST_MAX: name slots shared by all the live lists */
#define OTC_NAME_BYTES   128     /* a stored name: at most 127 bytes + NUL (sim O.DIR_NAME_MAX) */
#define OTC_NAME_MAX     127
#define OTC_WALK_BYTES   260     /* a name from the walker / a name of the unit: at most 259 bytes + NUL */
#define OTC_SPATH_BYTES  392     /* "../" + folder (255) + "/" + name (127) + NUL */
#define OTC_CANON_BYTES  256     /* a canonical folder: at most 255 bytes (a pool string) + NUL */
#define OTC_UNIT_SLOTS   128
#define OTC_BAGS_MAX     8       /* sim O.PICK_BAGS_MAX */
#define OTC_MAX_DIRWARN  16
#define OTC_MAX_REFUSED  64
#define OTC_IT_BYTES     1024    /* the walker's iterator: the host / asm owns the layout */

typedef struct {            /* the file list of one frame (sim.FileList + Frame.own) */
    uint32_t hash;          /* FNV-1a state of the signature up to the last filter (sim FileList.sig) */
    uint16_t s0, nbase;     /* the base it reads (own or an ancestor's): first arena slot, names kept */
    uint16_t own, nlive;    /* arena slots this frame owns (sim Frame.own); names left by the filters */
    uint8_t deep, nfilt, pathlen, pad;
    uint8_t path[OTC_CANON_BYTES];                /* the canonical folder (sim FileList.path) */
    uint32_t live[OTC_NAMES_MAX / 32];            /* bit r: the name of rank r is still in the list */
} otc_flist_t;

typedef struct {            /* one pick bag (sim bags: func, hash, n, rest) */
    uint32_t hash, seq;     /* seq: insertion order (the oldest has the smallest) */
    uint16_t n, nrest;
    uint8_t fn, used;
    uint16_t rest[OTC_NAMES_MAX];
} otc_bag_t;

typedef struct {            /* a `dir` warning (kind 0: long names skipped, 1: cut) */
    uint32_t n;
    uint16_t cap;
    uint8_t kind, pathlen;
    uint8_t path[OTC_CANON_BYTES];
} otc_warn_t;

typedef struct {            /* a load the unit refused */
    uint16_t op, line, len;
    uint8_t fn, slot;
    uint8_t path[OTC_SPATH_BYTES];
} otc_ref_t;

typedef struct otc_ws {
    uint8_t names[OTC_NAMES_MAX][OTC_NAME_BYTES];  /* the names arena: a stack of bases, one per frame at most */
    uint16_t idx[OTC_NAMES_MAX];                   /* parallel to the arena: the sorted order of each base */
    otc_bag_t bag[OTC_BAGS_MAX];                   /* persistent between presses */
    uint32_t bagseq;
    otc_flist_t fl[OTC_MAX_FRAMES];                /* the list of each frame */
    /* per press */
    uint32_t touched;                              /* a slot import was called */
    uint16_t nwarn, warn_ovf, nref, ref_ovf, nwork;
    uint8_t work[OTC_UNIT_SLOTS];                  /* the working set */
    uint8_t used[OTC_UNIT_SLOTS], perm[OTC_UNIT_SLOTS];     /* unused (workspace layout kept) */
    otc_warn_t warn[OTC_MAX_DIRWARN];
    otc_ref_t ref[OTC_MAX_REFUSED];
    /* buffers (nothing of this lives on the C stack) */
    uint8_t canon[OTC_CANON_BYTES], walk[OTC_WALK_BYTES], spath[OTC_SPATH_BYTES];
    uint8_t shuf[OTC_UNIT_SLOTS][OTC_WALK_BYTES];  /* static.swap: the two names (shuf[0..1]); the rest unused */
    uint32_t it[OTC_IT_BYTES / 4];                 /* the walker's iterator (otb_host_dir_*) */
} otc_ws_t;

/* ---- imports: the card and the unit's slots (asm modules/otype/card.S; host_shim.c on the Mac) ---- */
/* The walker never returns a directory or a name with a component that starts with '.', and keeps
 * paths of at most 259 bytes (longer ones are only counted: dir_skipped). The C does not trust the
 * first rule: it drops such names itself. Names are the card's bytes, relative to the opened folder,
 * '/'-separated, NUL-terminated, case of the card, in ANY order (the C sorts). */
int32_t otb_host_dir_open(void *it, const char *canon, uint32_t deep);   /* 0 ok, -1 absent (empty list), -2 busy */
int32_t otb_host_dir_next(void *it, char *out260);   /* 1 a name in out260, 0 the end, -2 busy */
uint32_t otb_host_dir_skipped(void *it);             /* names longer than 259 bytes seen (not returned) */
int32_t otb_host_slot_name(uint32_t slot0, char *out260, uint32_t max);   /* length of the stored path, 0 = free
                                                      * (out may be NULL when max is 0) */
int32_t otb_host_slot_set(uint32_t slot0, const char *path);   /* write the stored name ("../AUDIO/x.wav"),
                                                      * 0 ok, else the unit cannot store it (a refusal) */
int32_t otb_host_slot_load(uint32_t slot0);          /* load the file named in the slot: 1 loaded, 0 refused
                                                      * (the slot is already cleared, FUN_40025288) */
void otb_host_slot_clr(uint32_t slot0);              /* empty the slot (FUN_40025288) */
int32_t otb_host_card_busy(uint32_t op);             /* op = (ns << 8) | num of the card operation about to
                                                      * touch the card: != 0 = busy (~2 s of retries made) */

/* ---- imports: the part's parameters (p-locks; asm on the unit, host_shim.c on the Mac) ---------------------
 * What the part says about a parameter is read AT RUNTIME from the unit (nothing of the firmware's descriptor table is
 * copied into this source or into a test): the stock p-lock store clamps a value into the descriptor's range and
 * refuses a slot the descriptor disables. `track0` = 0..7 ([0x100b14cc] numbering),
 * `p` = the flat parameter 0..29 (sim.param_name; the PB / LFO / AMP / FX1 / FX2 pages of 6). */
void otb_host_param_info(uint32_t track0, uint32_t p, uint32_t *lo, uint32_t *count, uint32_t *enabled);
                                                     /* the descriptor: the values lo .. lo+count-1 (min, number),
                                                      * *enabled != 0 if the slot is enabled (else lo / count are
                                                      * not read). The C normalises it: lo 0..254, the range ends at
                                                      * 254 at the most (a lock is a byte, 0xff = none), count >= 1 */
int32_t otb_host_param_value(uint32_t track0, uint32_t p);    /* the part's current value of the parameter (what
                                                      * pl.get answers on a step that holds no lock) */

/* ---- imports: the part (asm on the unit, host_shim.c on the Mac) --------------------------------------------
 * The writes, each the unit's own mechanism; the C decides WHAT to write
 * (clamped, drawn, ordered) and the host only performs it. Nothing of the firmware's tables is a constant here: lengths,
 * defaults and counts are read through the imports at runtime. `track0` = 0..7, `p` = flat parameter 0..29. */
void otb_host_par_set(uint32_t track0, uint32_t p, int32_t v);  /* the stock parameter writer FUN_40054cd8(t, flat, v):
                                                      * v is already inside the descriptor range of an enabled slot */
int32_t otb_host_param_default(uint32_t track0, uint32_t p);    /* the descriptor's default value of the parameter
                                                      * (trk.init; the C clamps it into the range) */
void otb_host_lfo_set(uint32_t track0, uint32_t lfo0, uint32_t what, uint32_t v);   /* LFO page 2, a direct part byte:
                                                      * what 0 = destination (flat 0..29), 1 = waveform */
uint32_t otb_host_lfo_waves(void);                   /* number of LFO waveforms (the C cuts it to 0..255; 0 = none drawn) */
uint32_t otb_host_fx_count(uint32_t slot0);          /* entries of the FX1 / FX2 chooser, NONE (index 0) included
                                                      * (a patched image may rebuild the FX2 list: read it, never assume it) */
uint32_t otb_host_fx_default(uint32_t slot0);        /* chooser index of the factory effect (FILTER / DELAY), or an index
                                                      * >= the count when the chooser does not hold it (then skipped) */
void otb_host_fx_set(uint32_t track0, uint32_t slot0, uint32_t index);   /* the chooser apply of entry `index` on the
                                                      * track: cursor + FX1/FX2 apply (installs the effect's defaults) */
void otb_host_scene_put(uint32_t track0, uint32_t scene0, uint32_t byte, uint32_t v);   /* byte 0..31 of the track's
                                                      * block in scene 0..15, a direct part byte (0xff = no lock) */
void otb_host_trk_slot_init(uint32_t track0);        /* the track's factory slot bytes, a direct part write
                                                      * the bridge follows with the voice re-arm */
void otb_host_trk_slot(uint32_t track0, uint32_t slot0);   /* assign slot0 (0..127) to current machine of track0 (0..7) */
void otb_host_tempo(int32_t bpm);                          /* set tempo in BPM (30..300) via FUN_4009c708(bpm * 24) */

/* ---- imports: audio operations (asm on the unit, host_shim.c on the Mac) --------- */
uint32_t otb_host_audio_input(void);                       /* 0 = none, 1 = sample */
int32_t otb_host_src_save(char *out, uint32_t cap);
int32_t otb_host_flex_load(uint32_t slot0, const char *path);
int32_t otb_host_flex_free(uint32_t slot0);
int32_t otb_host_flex_del(uint32_t slot0);                 /* empty the Flex slot, its RAM back; 0, or
                                                            * OTC_AE_STOP_FIRST while the sequencer plays; a free slot is not touched */
void    otb_host_cat_clr(void);
int32_t otb_host_cat_add(const char *path);
int32_t otb_host_cat_save(const char *name, char *out, uint32_t cap);
int32_t otb_host_norm(const char *path, int32_t db);

/* ---- state ----------------------------------------------------------------------------------- */
typedef struct {            /* context scope (spec 6.2 / 14.2) */
    uint32_t mlo, mhi;      /* steps 1..32, 33..64 (bit s-1) */
    uint16_t str[OTC_NSTREAMS];   /* stream slot of rand toss prob drunk file; shared by reference */
    uint8_t ntr, step;      /* tracks (ordered, no duplicate), current step */
    uint8_t tr[8];
} otc_ctx_t;

typedef struct {            /* control frame: l (kind 0), each (1), track pass (2) */
    int32_t cur, last, si, sj;
    uint32_t mlo, mhi;      /* each: the steps fixed when ebeg ran */
    uint16_t end;
    uint8_t kind, depth, idx, n, step;
    uint8_t items[8];       /* pass: the track list fixed when pass ran */
} otc_ctl_t;

typedef struct {            /* one call */
    int32_t stack[OTC_MAX_STACK + 1];     /* +1: the declared depth is checked after each instruction */
    int32_t loc[OTC_MAX_LOCALS], args[OTC_MAX_ARGS];
    int32_t i, j;
    otc_ctx_t ctx[OTC_MAX_CTX + 1];
    otc_ctl_t ctl[OTC_MAX_CTL + 1];
    uint16_t pc, ipc;
    uint8_t fn, sp, nctx, nctl, nplist;
    uint8_t plist[OTC_MAX_PLIST];         /* the track list made by `opv trk`, consumed by `pass` */
} otc_frame_t;

typedef struct {            /* a function of the table, validated by otc_vm_load */
    uint32_t code, aux, ctr, lines;       /* byte offsets in the table (aux: params, args; ctr: counters) */
    uint16_t code_len, nlines, def_line, cbase;
    uint8_t nargs, nparams, maxs, maxc, maxl, nctr;
    uint16_t page;                        /* pool offset of the page label (host bit PAGE), 0 = not a page function */
} otc_fn_t;

typedef struct {            /* persistent state of a function (sim.FSTATE_DEFAULT) */
    int32_t flip, o, o_min, o_max, o_wrap, o_inc, drunk, d_min, d_max, d_wrap;
    uint8_t touched;
} otc_fstate_t;

typedef struct otc_vm {
    const uint8_t *tab;     /* the table (the caller keeps it alive) */
    uint32_t tablen, pool_off, pool_len;
    uint16_t nfunc, nctr_total;
    uint8_t loaded;
    /* the run */
    uint8_t host_track, length, nframes;
    uint8_t npset;                        /* params imposed on the root function (otc_vm_set_params), 0 = none */
    int16_t pset[OTC_MAX_PARAMS];
    int32_t error;
    uint32_t cost, executed;
    uint32_t draws[OTC_NSTREAMS];
    uint16_t noor, overflow;
    uint8_t oor_fn[OTC_MAX_OOR];
    uint16_t oor_op[OTC_MAX_OOR], oor_line[OTC_MAX_OOR];
    /* the bound pattern bytes (otc_vm_bind) and what the run did to them */
    uint8_t *trac[8], *copy[8];           /* the TRAC block of track t+1 and its optional SRAM copy */
    uint8_t mtype[8];
    uint8_t bound;                        /* bit t-1: track t is bound */
    uint32_t touched;                     /* bit t-1: a byte of track t changed in this run */
    char msg[OTC_MSG_BUF];                /* the text of the last msg of the run, "" = none (sim.msgs[-1]) */
    uint32_t part_touched;                /* bit t-1: a part write import was called for track t in this run */
    uint32_t pending_trac, pending_part;  /* commits belong to the target before a pattern/part switch */
    otc_ws_t *ws;                         /* the card workspace (otc_vm_bind_work), NULL = unbound */
    uint16_t nskip, skip_ovf, npick, pick_ovf;
    uint8_t skip_fn[OTC_MAX_OOR], pick_fn[OTC_MAX_OOR], pick_trk[OTC_MAX_OOR];
    uint16_t skip_op[OTC_MAX_OOR], skip_line[OTC_MAX_OOR], pick_op[OTC_MAX_OOR], pick_line[OTC_MAX_OOR];
    otc_frame_t fr[OTC_MAX_FRAMES];
    uint32_t slot[OTC_STREAM_SLOTS];      /* xorshift32 states */
    /* persistent between runs (otc_vm_reset_state) */
    int32_t g[4];                         /* x y z t */
    const char *folder_path[OTC_MAX_PARAMS]; /* folder param paths passed by host */
    uint8_t folder_len[OTC_MAX_PARAMS];
    otc_fn_t fn[OTC_MAX_FUNCS];
    otc_fstate_t fs[OTC_MAX_FUNCS];
    int32_t ctr_val[OTC_MAX_COUNTERS];
    uint8_t ctr_set[OTC_MAX_COUNTERS];
} otc_vm_t;

int32_t otb_host_control_read(uint32_t handler, uint32_t track0, uint32_t n);
int32_t otb_host_control_write(uint32_t handler, uint32_t track0, uint32_t n, int32_t value);
int32_t otb_host_control_action(otc_vm_t *vm, uint32_t handler, int32_t a, int32_t b);
void otb_host_refresh(otc_vm_t *vm);
void otb_host_commit(otc_vm_t *vm);

uint32_t otc_vm_sizeof(void);
/* page functions (`page "LABEL"`: host bit PAGE 0x80 in the function entry, the label's pool offset ends the aux data).
 * otc_vm_load rejects a PAGE function whose label is not a 1..8 printable-ASCII pool string, that has arguments or more than
 * OTC_PAGE_PARAMS params (OTC_E_TABLE). The queries below read the loaded table; the pointers point INTO it (no NUL). */
/* Length (1..8) of the page label of function `fn` and its first byte in *label (may be NULL), or 0: `fn` is not a page
 * function (or out of range, or no table loaded). */
uint32_t otc_vm_fn_page(const otc_vm_t *vm, uint32_t fn, const char **label);
/* Length (1..127) of the text of `confirm "TEXT"` of function `fn` (the first non-empty one the source declares; flag bit 2 of
 * the entry, the pool offset after the page label in the aux data) and its first byte in *text (may be NULL; INTO the table,
 * no NUL), or 0: no text (a plain `confirm`, an implied confirmation, none at all; or `fn` out of range / no table). The
 * confirmation box shows this text when it exists, else NAME?. otc_vm_load rejects a text
 * that is not a 1..127 printable-ASCII pool string (OTC_E_TABLE). */
uint32_t otc_vm_fn_confirm(const otc_vm_t *vm, uint32_t fn, const char **text);
/* Returns 1 if function `fn` declares `input sample` (flag bit 0x10 at byte +23 of entry), else 0. */
uint32_t otc_vm_fn_input_sample(const otc_vm_t *vm, uint32_t fn);
typedef struct {
    const char *name;                     /* the param's name as the script wrote it (UPPER case; no NUL), in the table */
    uint32_t name_len;
    int16_t def, lo, hi;                  /* default, minimum, maximum */
    uint8_t kind;                         /* OTC_PK_INT, OTC_PK_BOOL, OTC_PK_OPTION, OTC_PK_FOLDER */
    uint8_t nlabels;                      /* for OTC_PK_OPTION: 2..16, else 0 */
    const char *labels[16];               /* for OTC_PK_OPTION: pointers to labels in pool */
    uint8_t label_lens[16];               /* for OTC_PK_OPTION: label lengths */
    const char *folder_path;              /* for OTC_PK_FOLDER: default path in pool */
    uint32_t folder_len;                  /* for OTC_PK_FOLDER: default path length */
} otc_vm_param_t;
/* Param `k` (declaration order: A, B, C ... on the page) of function `fn` into *out: 1, or 0 when `fn` or `k` does not exist
 * (so `for (k = 0; otc_vm_fn_param(vm, fn, k, &p); k++)` walks them). */
int otc_vm_fn_param(const otc_vm_t *vm, uint32_t fn, uint32_t k, otc_vm_param_t *out);
/* Impose the params of the ROOT function of the next runs (what `otype.py run --param` does in the Python VM): param k of
 * the root function reads vals[k], clamped to its min..max, for k < n; the others keep their default. Nested calls never see
 * it. n = 0 clears; n is cut to OTC_MAX_PARAMS. Persistent until cleared; otc_vm_load clears it. */
void otc_vm_set_params(otc_vm_t *vm, const int16_t *vals, uint32_t n);
/* Impose the folder path of a folder param of the ROOT function for the next runs. Persistent until cleared;
 * otc_vm_load / otc_vm_reset_state clears it. path = NULL clears param k. */
void otc_vm_set_param_folder(otc_vm_t *vm, uint32_t k, const char *path, uint32_t len);
int otc_vm_load(otc_vm_t *vm, const uint8_t *otb, uint32_t len);
void otc_vm_reset_state(otc_vm_t *vm);
/* Bind track 1..8: `trac` = its 0x91a-byte TRAC block (every write goes there), `copy` = NULL or the
 * firmware's SRAM copy (every write goes to the same offset there too), `mtype` = the track's machine
 * type (4 = PICKUP). A NULL `trac` unbinds. otc_vm_load clears all bindings. */
void otc_vm_bind(otc_vm_t *vm, unsigned track, uint8_t *trac, uint8_t *copy, unsigned mtype);
uint32_t otc_vm_touched(const otc_vm_t *vm);
const char *otc_vm_msg(const otc_vm_t *vm);      /* the text of the last msg of the run, "" = none (the host draws it) */
uint32_t otc_vm_part_touched(const otc_vm_t *vm);   /* bit t-1 = a part write import was called for track t */
/* bind the card workspace (otc_ws_size() bytes, 8-aligned, any content; NULL unbinds). Binding an
 * area that is not the current one empties the pick bags; so does otc_vm_load / otc_vm_reset_state (the
 * bags are persistent state). otc_vm_load keeps the binding. */
uint32_t otc_ws_size(void);
void otc_vm_bind_work(otc_vm_t *vm, void *ws);
uint32_t otc_vm_slots_touched(const otc_vm_t *vm);
int otc_vm_run(otc_vm_t *vm, uint16_t func_index, uint16_t seed, uint8_t host_track, uint8_t length);
uint32_t otc_vm_export(const otc_vm_t *vm, int32_t *out, uint32_t max_words);

#endif
