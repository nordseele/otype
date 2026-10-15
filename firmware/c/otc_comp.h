/* OType compiler on the unit: FRONT END and CHECKER: public interface and the data.
 *
 * The front end mirrors tools/otype.py up to the checker passes of Program.run(): the lexer, the lines `otype` / `const` /
 * `def`, `pass_names` (registration of the functions, the clashes of names) and the BodyParser of every
 * registered function. Proven identical to the Python reference by tools/test_otype_ccomp.py through a
 * canonical text of the state (modules/otype/front_dump.py describes it; `otc_front_dump` writes the same).
 * The checker passes (find_unused .. pass_flags) follow, then the diagnostics of the compiler (otb.compile_program:
 * `not on the unit yet`, stack depth, counters, code length, table size) and the REPORT text (= /SCRIPTS/ERRORS.TXT,
 * modules/otype/oterr.py `reference`), proven identical, character for character, on the whole corpus.
 *
 *   - freestanding C99, <stdint.h> only: no libc, no malloc, no printf, no 64-bit arithmetic, no float;
 *   - NO static or global writable variable: all the state lives in the caller's workspace (arena) of
 *     otc_comp_ws_size() bytes, 8-aligned;
 *   - the sources stay in the caller's memory (not copied); the labels too;
 *   - every record of the workspace is reached through a 32-bit offset from the workspace base (so the layout
 *     does not depend on the size of a pointer); the strings are length-prefixed;
 *   - only bounded recursion: the expression parser (depth <= OTC_EXPR_DEPTH, a deeper expression stops the
 *     run with OTC_CE_DEPTH) and the dump of an expression (same bound); blocks use explicit stacks;
 *   - an arena that is full stops the run with OTC_CE_ARENA: never a silent truncation.
 *
 * Use:  void *ws = (caller memory of otc_comp_ws_size() bytes, 8-aligned) and a stack of otc_comp_stack_bytes();
 *       otc_src_t src[n] = {label, label_len, text, text_len} in card order;
 *       int32_t err = otc_run(ws, src, n, OTC_PROFILE_M1);   (0, or OTC_CE_*: there is then no report)
 *       uint32_t len = otc_report(ws, buf, cap);              (the text of ERRORS.TXT: written if len <= cap)
 *   front end alone: otc_front(ws, src, n); otc_front_dump(ws, buf, cap) (test helper: the canonical text, see below)
 *
 * At most OTC_MAX_DIAGS diagnostic lines (the first ones after sorting by label bytes, line, order of emission),
 * then one `-:0: -: error: too many errors (N more)` line, then the summary which counts everything. The workspace never
 * holds more than OTC_MAX_DIAGS messages: a run that sees more is repeated (see otc_run in otc_comp.c).
 * Recursion (checker, code-size walker): ck_node / cost_expr / fc_expr, bounded by OTC_EXPR_DEPTH + a few levels of
 * statements; blocks (<= 9 levels in an accepted function), modifiers of the code-size walker (<= 21 on a line of <= 128
 * characters: fc_mods) and, for the cost, calls (<= 9 nested functions: a function over 8 calls deep is refused before
 * it is costed). No other recursion; the graph walks are iterative.
 */
#ifndef OTC_COMP_H
#define OTC_COMP_H

#include <stdint.h>
#include "otc_opreg.h"

#ifndef OTC_COMP_ARENA_BYTES      /* (the tests build smaller ones with -D) */
#define OTC_COMP_ARENA_BYTES  (3u * 1024u * 1024u)   /* the whole workspace, header included */
#endif
#define OTC_EXPR_DEPTH   64      /* nesting of expressions: <= 64 on a line of <= 128 characters (2 characters per level) */
#define OTC_BLOCK_STACK  400     /* indentation levels: the stack of the block structure; 1 + 2 + ... + n <= 65536 bytes */
#define OTC_HBUCKETS     1024    /* buckets of the const / function name tables (chained: any number of entries) */
#define OTC_MAX_SRC_FILES 64
#define OTC_MAX_SRC_BYTES 65536
#define OTC_MAX_DIAGS    1000    /* diagnostic lines in a report */
#define OTC_MEMO_BUCKETS 4096    /* cost memo (chained: any number of entries) */

enum { OTC_CE_OK = 0, OTC_CE_ARENA = 1, OTC_CE_DEPTH = 2, OTC_CE_BLOCKS = 3, OTC_CE_ABORT = 4 /* internal: restart of a run */ };
enum { OTC_PROFILE_NONE = 0, OTC_PROFILE_M1 = 1 };

typedef struct {
    const uint8_t *label;
    uint32_t label_len;
    const uint8_t *text;
    uint32_t text_len;
} otc_src_t;

uint32_t otc_comp_ws_size(void);
uint32_t otc_comp_stack_bytes(void);               /* worst-case stack of otc_run / otc_report (analytic, ColdFire frames) */
int32_t otc_front(void *ws, const otc_src_t *src, uint32_t nsrc);
uint32_t otc_comp_used(const void *ws);            /* arena bytes in use (the high-water mark: nothing is freed) */
/* front end + checker passes + compiler diagnostics (the text of the report is then `otc_report`). `profile`:
 * OTC_PROFILE_NONE or OTC_PROFILE_M1 (the ops of the unit profile `m1`, otc_reg.flags & OTC_RF_M1). Returns 0 or OTC_CE_*. */
int32_t otc_run(void *ws, const otc_src_t *src, uint32_t nsrc, uint32_t profile);
/* Same, in a workspace of `ws_size` bytes (the caller's arena; at least sizeof(otc_comp_t) + 4 KB, 8-aligned) instead of
 * OTC_COMP_ARENA_BYTES. */
int32_t otc_run_n(void *ws, uint32_t ws_size, const otc_src_t *src, uint32_t nsrc, uint32_t profile);
/* the OTB table of the last otc_run, byte for byte that of modules/otype/otb.py,
 * into `out` (at most `cap` bytes, a table is at most OTC_OTB_MAX). Returns its length, or 0 when there is none (a table-level
 * error: > 255 functions, a label over 255 bytes, > 1024 counters, a table over 32768 bytes; or `cap` too small). */
#define OTC_OTB_MAX 32768u
uint32_t otc_emit(void *ws, uint8_t *out, uint32_t cap);
/* THE entry point of the firmware: sources in card order in, the table and the text of ERRORS.TXT out.
 *   src, nsrc   the .OTY files of /SCRIPTS (label = the name shown in the messages), at most 64 files / 65536 bytes are read
 *   profile     OTC_PROFILE_NONE or OTC_PROFILE_M1
 *   arena       the workspace, 8-aligned, `arena_size` bytes (OTC_COMP_ARENA_BYTES is enough for any input; a typical
 *               script needs about 46 KB + 40 x the bytes of the sources); a stack of otc_comp_stack_bytes()
 *   otb         at least OTC_OTB_MAX bytes: *otb_len is the table's length, 0 = no table (see otc_emit)
 *   report      the text of ERRORS.TXT (LF line ends; NUL terminated only if it fits): at most OTC_MAX_DIAGS (1000) diagnostic lines
 *               + the `too many errors` line + the summary. *report_len is its FULL length; bytes past `report_cap` are not
 *               written, so *report_len > report_cap = truncated. Bound: a line is `LABEL:LINE: FUNC: error: MESSAGE` = label
 *               + 5 + 2 + 16 + 9 + message; measured on the corpus, with labels <= 40 and source lines <= 128 characters, a message
 *               is <= 120 characters and a line <= 148: 1000 x 149 + 150 (cut line, summary) = 149 150 bytes. Three things are not
 *               covered by that figure: a long label, a message that quotes a source line longer than 128 characters (the whole
 *               line, at most the 64 KB of the sources in all), and the paths of `loop a>b>...` / `call depth N > 8 (...)`, which
 *               name every function of the cycle (17 bytes each, up to 2 KB in the corpus; a ring of 1500 functions gives a
 *               25 KB message, 982 363 bytes in the worst report of the corpus). A 256 KiB buffer, cut at the end, is enough in practice.
 * Returns 0, or OTC_CE_* (arena too small, expression nested too deep ...: no table, no report). */
int32_t otc_compile(const otc_src_t *src, uint32_t nsrc, uint32_t profile, void *arena, uint32_t arena_size, uint8_t *otb,
                    uint32_t otb_cap, uint32_t *otb_len, char *report, uint32_t report_cap, uint32_t *report_len);
/* The report text (every line ends with LF), after a successful otc_run. Returns its length; it is written (and NUL
 * terminated) only if it fits `cap`: call again with a bigger buffer when the result is >= cap. */
uint32_t otc_report(void *ws, char *out, uint32_t cap);
/* Test helper: one line per function (state after the checker and the dry run of the code generator, see
 * modules/otype/check_dump.py). Same convention as otc_front_dump. */
uint32_t otc_check_dump(const void *ws, char *out, uint32_t cap);
/* The canonical dump of modules/otype/front_dump.py. Returns the length of the text; it is written (and
 * NUL terminated) only if it fits `cap`: call again with a bigger buffer when the result is >= cap. When the
 * run stopped with an OTC_CE_* error the text is "OVERFLOW <code>\n". */
uint32_t otc_front_dump(const void *ws, char *out, uint32_t cap);

/* ---------------------------------------------------------------------------------------------------- */
/* The records (the checker and the code generator read them). Offsets are from the base of the workspace; 0 = none. A string is at
 * a `ref`: uint32 length, then the bytes (no NUL), padded to 4. */

enum { OTC_LV_ERROR = 0, OTC_LV_WARNING = 1 };

enum { OTC_LK_NONE = 0, OTC_LK_LOOP = 1, OTC_LK_DEPTH = 2 };    /* a message made when the report is written */
#define OTC_FILE_TABLE 0xFFFFu    /* the pseudo file "-" of the table-level errors */

typedef struct {                  /* a diagnostic, in emission order (linked list) */
    uint32_t next;
    uint32_t msg;                 /* string ref (0 when lkind != 0) */
    uint32_t line;
    uint32_t func;                /* otc_func_t offset; 0 = "-" */
    uint32_t seq;                 /* order of emission: Diag.seq */
    uint32_t laux;                /* OTC_LK_DEPTH: the depth */
    uint16_t file;                /* index in the input list; OTC_FILE_TABLE */
    uint8_t level, hidden, lkind;
    uint8_t pad[3];
} otc_diag_t;

enum { OTC_CT_NONE = 0, OTC_CT_INT = 1, OTC_CT_STR = 2, OTC_CT_LIST = 3, OTC_CT_COND = 4 };

typedef struct {                  /* a `const` line (const_list order) */
    uint32_t next;                /* the next const of the list */
    uint32_t hnext;               /* chain of the name table (heads of the groups of a name only) */
    uint32_t gnext, gtail;        /* the consts of the same name, in order (gtail: kept by the first only) */
    uint32_t name;                /* string ref (upper case) */
    uint32_t line;
    uint32_t val;                 /* INT: the value; STR: ref of the text; LIST: ref of 2 bytes (big-endian) per value; COND: ref of the canonical text */
    uint32_t hash;
    uint16_t file;
    uint8_t type, bad;
} otc_const_t;

enum { OTC_PK_INT = 0, OTC_PK_BOOL = 1, OTC_PK_OPTION = 2, OTC_PK_FOLDER = 3 };

typedef struct {                  /* a `param` of a function (declaration order) */
    uint32_t next, name;
    int32_t def, lo, hi;
    uint32_t path;                /* FOLDER: string ref of default path */
    uint32_t labels[16];          /* OPTION: string refs of labels */
    uint8_t kind;                 /* OTC_PK_* */
    uint8_t nlabels;              /* OPTION: number of choices (2..16) */
    uint8_t pad[2];
} otc_param_t;

typedef struct {                  /* a `menu` of a function: the levels (a chain of cells), then the action */
    uint32_t next, line, action, nlevels, lfirst;
} otc_menu_t;

typedef struct {                  /* a level of a menu: string ref */
    uint32_t next, ref;
} otc_cell_t;

enum { OTC_FF_REG = 1, OTC_FF_HERR = 2, OTC_FF_CONFIRM = 4, OTC_FF_BLIND = 8, OTC_FF_INPUT_SAMPLE = 16 };

typedef struct {
    uint32_t next, hnext, gnext, gtail, hash;
    uint32_t name, line;
    uint32_t args;                /* offset of nargs uint32: string ref | (1 << 31) when the argument appears as a word in the body */
    uint32_t nargs;
    uint32_t body_off, body_end, body_line;   /* the lines after the `def` line: byte range in the file and number of the first line */
    uint32_t root_first, root_last;           /* the top-level lines (otc_line_t) */
    uint32_t nerr;                /* len(Func.errors) */
    uint32_t idx;                 /* position in the list of functions (index of the checker state otc_fx_t) */
    uint32_t param_first, param_last, nparams;
    uint32_t menu_first, menu_last, nmenus;
    uint32_t page_line, page_label;   /* line of the `page "LABEL"` declaration (0: none) and the string ref of its label */
    uint32_t confirm_line, confirm_text;   /* the first `confirm "TEXT"` with a non-empty text: its line (0: none), its string ref */
    uint16_t file;
    uint8_t flags;                /* OTC_FF_* */
    uint8_t pad;
} otc_func_t;

enum {
    OTC_N_NUM = 1, OTC_N_STR, OTC_N_LIST, OTC_N_COND, OTC_N_VAR, OTC_N_ARG, OTC_N_PARAM, OTC_N_CONST,
    OTC_N_OP, OTC_N_CALL, OTC_N_KW, OTC_N_PRED,
    OTC_N_ASSIGN, OTC_N_OPSTMT, OTC_N_CALLSTMT, OTC_N_CTXSTMT, OTC_N_SETSTMT, OTC_N_BREAK, OTC_N_STOP, OTC_N_RET,
    OTC_N_DECL,
    OTC_N_M_IF, OTC_N_M_ELIF, OTC_N_M_ELSE, OTC_N_M_L, OTC_N_M_EACH, OTC_N_M_PROB, OTC_N_M_EVERY, OTC_N_M_SKIP,
    OTC_N_M_CTX
};

typedef struct {                  /* an expression, a statement or a modifier of a line */
    uint8_t kind;                 /* OTC_N_* */
    uint8_t akind;                /* the kind of the argument it is for its parent (OTC_K_*: INT STEP ... KW USER; 0 none) */
    uint8_t op;                   /* index in otc_reg of the op (OP, PRED, SETSTMT) */
    uint8_t flags;
    uint32_t text;                /* ref: the token as the checker keeps it (upper case; list as written; string = its content) */
    uint32_t val;                 /* NUM: the value; LIST: ref of 2 bytes per value; COND: ref of the canonical text; OP / CALL: number of arguments; DECL: ref of the param name or 0 */
    uint32_t first;               /* the first argument (chain through `next`); a statement's / CTX modifier's node; ASSIGN / SETSTMT / RET: the value */
    uint32_t next;                /* the next argument of the parent; for a modifier: the next modifier of the line */
} otc_node_t;

enum { OTC_LF_ERROR = 1, OTC_LF_OPENS = 2, OTC_LF_DECL = 4 };

typedef struct {                  /* a line of a body (Python's Line) */
    uint32_t no, indent, first_word;   /* first_word: string ref (upper case) */
    uint32_t mods;                /* the first modifier node (chain through `next`) */
    uint32_t body;                /* the declaration node (OTC_LF_DECL) or the statement node, or 0 */
    uint32_t child_first, child_last, next, parent;
    uint8_t flags;                /* OTC_LF_* */
    uint8_t pad[3];
} otc_line_t;

/* ---- the checker state ----------------------------------------------------------------------------- */
enum { CL_NONE = 0, CL_INT = 1, CL_STR = 2, CL_LIST = 3, CL_COND = 4, CL_ANY = 5, CL_FOLDER = 6, CL_ITEM = 7 };   /* classes of values (Python: "INT" ...) */
enum { RS_MISSING = 0, RS_NONE = 1, RS_IV = 2 };           /* rng[(f, arg)]: absent / None / (lo, hi) */

typedef struct { uint32_t g, line, pad; } otc_edge_t;      /* callee (function index), line of its first call (12 bytes: -mcpu=5407 has no index scale of 8) */
typedef struct { uint32_t next, g, k, pad; } otc_ed_t;     /* an argument passed on as argument k of the function g */

typedef struct {                  /* an argument of a registered function (keys are by NAME: canon = first of its name) */
    int32_t lo, hi;               /* the range of the values it gets (rstate == RS_IV) */
    uint32_t efirst;              /* chain of otc_ed_t: where it is passed on */
    uint32_t canon;               /* index of the first argument of the same name: the key of every table */
    uint8_t unused, uses, kind, newkind, isconst, rstate, pad[2];   /* uses: bit 1 << CL_* ; kind: CL_* (0 before the inference) */
} otc_argx_t;

typedef struct { uint32_t f, node, line, g; } otc_site_t;  /* a call: caller index, call node, its line, callee index */

typedef struct {                  /* per function */
    uint32_t func;                /* otc_func_t offset */
    uint32_t argx;                /* otc_argx_t[nargs] (registered functions with arguments) */
    uint32_t cg, ncg;             /* otc_edge_t[ncg]: the callees in order of first call */
    uint32_t gstart, gcnt;        /* its call sites as a callee: sorder[gstart .. gstart + gcnt) */
    uint32_t grank;               /* rank of the group in the order of first appearance (0xFFFFFFFF: never called) */
    uint32_t tidx, tlow, scc;     /* Tarjan; scc = id of the strongly connected component (0 = none) */
    uint32_t height, hnext;       /* longest chain of non-cyclic callees; hnext = first callee giving it, plus 1 */
    uint32_t stamp;               /* scratch marks */
    uint32_t cost;                /* maxcost (saturated) */
    uint32_t code_len, nlines, nctr, maxstack, cerr_line;    /* dry run of the code generator */
    uint32_t maxctl, maxctx, nlocals;                         /* idem: control frames, context scopes, locals a..d used */
    uint32_t tabidx, fam, tch, nsm;                           /* index in the table; masks of the families, touches, namespaces */
    uint32_t bline, barg;         /* compile refusal: line, op index (bad == 1) / callee index (bad == 3) */
    uint8_t cyclic, onstack, destr, costed, cerr, direct;   /* cerr: 0 ok, 1 counters, 2 stack, 3 code length, 4 loop/pass depth, 5 context depth, 6 menus (dry run) */
    uint8_t bad, inorder;         /* bad: 0 accepted, 1 not on the unit yet, 2 compile error (cerr), 3 needs a refused function */
    uint8_t masked, iostate, io_op, srcstate;   /* pass_page; input_sample */
    uint8_t src_op, pad2[3];
    uint32_t io_line, src_line;
} otc_fx_t;

typedef struct { uint32_t next, f, callee, node, line; uint8_t have, want, pad[2]; } otc_defer_t;   /* a type error that waits for the end */

typedef struct { uint32_t file, line, seq; } otc_key_t;     /* sort key of a diagnostic (the label is that of `file`) */

typedef struct {                  /* the header of the workspace */
    uint32_t top, limit;          /* bump allocator: first free byte, size of the workspace */
    uint32_t err;                 /* OTC_CE_* of the run (sticky) */
    const otc_src_t *src;         /* the caller's inputs (kept, not copied) */
    uint32_t nsrc, nloaded;       /* the first `nloaded` inputs were loaded (the others are refused) */
    uint32_t file_errors;
    uint32_t nfuncs, nconsts, ndiags;
    uint32_t func_first, func_last;
    uint32_t const_first, const_last;
    uint32_t diag_first, diag_last;
    uint32_t msg;                 /* the message under construction (offset), 0 if none */
    uint32_t msg_len;
    uint8_t msg_inq;              /* show(): inside double quotes */
    uint8_t pad[3];
    uint32_t dmode, dcap;         /* how diagnostics are kept (otc_comp_core.c); dcap: visible diagnostics before a restart */
    uint32_t seq, nvis, nwarn;    /* diagnostics emitted, visible ones, warnings (all of them, kept or not) */
    otc_key_t thr;                /* DM_SELECT: keep the diagnostics whose key is <= thr */
    uint32_t hn;                  /* DM_COUNT: the OTC_MAX_DIAGS smallest keys (a max-heap) */
    otc_key_t hk[OTC_MAX_DIAGS];
    uint32_t profile, checked, gen;               /* gen: generation of the scratch marks otc_fx_t.stamp */
    uint32_t nf, fx;              /* functions, otc_fx_t[nf] */
    uint32_t nsites, sites, sorder, ngroups, gorder;   /* call sites; sorder: site indexes by group; gorder: function index by rank */
    uint32_t defer_first, defer_last;
    uint32_t memo, nmemo;         /* cost memo: bucket heads, entries */
    uint32_t rsort, rpath;        /* the report: kept diagnostics sorted; scratch of the call paths */
    uint32_t nkept;
    uint32_t ntab_funcs, tab_total, tab_err;     /* compiled functions, table size, table-level error (0 none, 1 >255 functions, 2 label, 3 size, 4 counters) */
    uint32_t pool, npool, pool_bytes;            /* constant pool of the table: hash buckets, entries, bytes (with offsets, in the order of first use) */
    uint32_t pool_first, pool_last;              /* the entries in the order of creation (= the order of the pool) */
    uint32_t lps[OTC_BLOCK_STACK + 1];           /* loop depth of the ancestors of a line (checker walk) */
    uint32_t stk_ind[OTC_BLOCK_STACK];     /* the block stack of the body parser: indentation, list owner */
    uint32_t stk_own[OTC_BLOCK_STACK];
    uint32_t fbucket[OTC_HBUCKETS];    /* function name -> first function of the name (registered ones) */
    uint32_t cbucket[OTC_HBUCKETS];    /* const name -> first const of the name */
} otc_comp_t;

#endif
