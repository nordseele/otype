"""Memory layout of the OType module: ONE block of DRAM, `otype_rgn`, reserved by the integrating tool.

    python3 build/layout.py            # print the region and the offsets of its buffers

    OT_VM       the VM instance + mailbox (otc_vm_sizeof)
    OT_TAB      the VM's loaded table
    OT_WS       the VM workspace
    OT_STAGE    the compiler's table before it is accepted
    OT_REPORT   ERRORS.TXT text
    OT_SRC      the sources, back to back
    OT_SRCV     otc_src_t[]
    OT_LABELS   "/SCRIPTS/<name>" labels
    OT_ARENA    the compiler arena, its private stack at the top

PLACEMENT. The integrating tool reserves REGION_BYTES of DRAM, aligned to REGION_ALIGN, that nothing else uses (not the
stock OS, not its sample memory, not another module), and passes its address to the link (`--defsym otype_rgn=<address>`,
build/link.py --region). The unit never assumes an address: build/units.py writes every OT_* buffer as
`otype_rgn + <offset>`, which the link resolves.

The block is NOT expected to be initialised and nothing outside the module clears it: the module zeroes the VM, its
table and the workspace itself before the first run (src/script.S) and every other buffer is written before it is read.

A 16-byte GUARD precedes OT_VM, a margin: the stock memset 0x40020984 writes one long past a length that is a multiple
of 16, so a block placed right above a buffer the stock clears keeps its first bytes.
"""

GUARD = 16
REGION_SYMBOL = "otype_rgn"
REGION_ALIGN = 16

OT_VM_BYTES = 38912
OT_TAB_BYTES = 32768
OT_WS_BYTES = 669840
OT_STAGE_BYTES = 32768
OT_REPORT_CAP = 262144
OT_REPORT_BYTES = OT_REPORT_CAP + 256
OT_SRC_CAP = 65536
OT_SRC_BYTES = OT_SRC_CAP + 8
OT_NAMES = 128
OT_LABEL = 272
OT_SRCV_BYTES = OT_NAMES * 16
OT_LABELS_BYTES = OT_NAMES * OT_LABEL
OT_STACK_BYTES = 40 * 1024
OT_ARENA_BYTES = 1024 * 1024
OT_ARENA_SIZE = OT_ARENA_BYTES - OT_STACK_BYTES

OT_BYTES = (OT_VM_BYTES + OT_TAB_BYTES + OT_WS_BYTES + OT_STAGE_BYTES + OT_REPORT_BYTES + OT_SRC_BYTES
            + OT_SRCV_BYTES + OT_LABELS_BYTES + OT_ARENA_BYTES)
REGION_BYTES = GUARD + OT_BYTES

# offsets of the buffers from the start of the region (contiguous, in this order: VM, table and workspace are one run)
OT_AT = {}
_o = GUARD
for _name, _size in (("VM", OT_VM_BYTES), ("TAB", OT_TAB_BYTES), ("WS", OT_WS_BYTES), ("STAGE", OT_STAGE_BYTES),
                     ("REPORT", OT_REPORT_BYTES), ("SRC", OT_SRC_BYTES), ("SRCV", OT_SRCV_BYTES),
                     ("LABELS", OT_LABELS_BYTES), ("ARENA", OT_ARENA_BYTES)):
    OT_AT[_name] = _o
    _o += _size
OT_END = _o
OT_STACK_TOP = OT_AT["ARENA"] + OT_ARENA_BYTES
assert OT_END == REGION_BYTES
assert all(v % 4 == 0 for v in OT_AT.values()), "every buffer must be long-aligned"

if __name__ == "__main__":
    print(f"OType region {REGION_SYMBOL}: {REGION_BYTES:,} B ({REGION_BYTES / 1048576:.2f} MiB), align {REGION_ALIGN}"
          f" (buffers {OT_BYTES:,} B + guard {GUARD})")
    for k, v in OT_AT.items():
        print(f"  OT_{k:7s} {REGION_SYMBOL} + 0x{v:06x}")
    print(f"  OT_STACK_TOP {REGION_SYMBOL} + 0x{OT_STACK_TOP:06x}")
