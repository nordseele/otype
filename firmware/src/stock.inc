| Stock (OS 1.40C) addresses and record layouts the OType sources use and do not define.
| Addresses only -- no Elektron bytes.

| ---- stock entry points and globals ------------------------------------------------------------------------
        .equ    TXT,          0x40012bd8  | (font, surface, x, y, n, text)
        .equ    SPRINTF,      0x40013a08  | sprintf(dst, fmt, ...)
        .equ    TICKS,        0x460d5de0  | display-loop tick
        .equ    FIO_OPEN,      0x40016864  | (fh, path, mode, buf, buflen) -> < 0 error
        .equ    FIO_READ,      0x40016564  | (fh, dst, len) -> 1 all read, 0 short, < 0 error
        .equ    FIO_WRITE,     0x400166b8  | (fh, src, len) -> < 0 error
        .equ    FIO_CLOSE,     0x4001677c  | (fh)
        .equ    FIO_SIZE,     0x46c8241e  | the FS table's size slot: (fh word 0) -> size
        .equ    FIO_IOLEN,     0x200
        .equ    FS_REMOVE,    0x46c82432  | the FS table's slot: (path) a file removed
        .equ    MB_POST,      0x40000c3c  | (mailbox, message): queued, the task woken
        .equ    ST_MBOX,      0x460d17ce  | the storage task's mailbox
        .equ    ST_LOOP,      0x4008484e  | the storage task: the next job
        .equ    ST_SWITCH,    0x40084864  | ... the jump on its type, after `cmp.l d0,d1`
        .equ    PRJ_NAME,     0x100f8378  | the project's name
        .equ    PRJ_OPEN,     0x400255ec  | () -> != 0: a project is open
        .equ    STR_CMP,      0x40013e14  | (a, b) -> 0 equal
        .equ    STR_CPY,      0x40013f40  | (dst, src)

| ---- the row and action records of the script table (the layout shim.S and script.S use) ----------------------
        .equ    R_LABEL,      0
        .equ    R_ACTS,       4
        .equ    R_N,          8
        .equ    R_CUR,        9
        .equ    A_NAME,       4
        .equ    A_FLAGS,      12
        .equ    A_VALUE,      13
        .equ    F_VALUE,      1          | bit number in A_FLAGS
