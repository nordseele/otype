#!/usr/bin/env python3
"""Compile the OType compiler (firmware/c/otc_comp.c: front end, checker and report) for the ColdFire and report its size.

    python3 modules/otype/build_comp.py [--out out/otype/comp/otc_comp.s] [--no-dump] [--define NAME[=V]]

Same flags and same unit policy as build_c.py (-mcfv4e -Os -ffreestanding -fno-builtin -nostdlib -Wall -Wextra
-Werror -Wstack-usage=256): `.rodata` goes to `.text`, any `.data` / `.bss` / `.comm` is refused, the object may
not reference an undefined symbol (no memcpy, __mulsi3, ...), and is assembled with -mcpu=5407 like the firmware's
link.  Prints `.text` and the size of the workspace (`otc_comp_ws_size`, the same on the Mac).  No firmware image
is built here: this only proves the C compiles for the unit.  --no-dump leaves the test-only dump out.
"""
import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_c  # noqa: E402

ROOT = HERE.parents[1]
C_DIR = ROOT / "firmware" / "c"
SRC = C_DIR / "otc_comp.c"
INC = C_DIR


def build(out, defines=(), quiet=False):
    cmd = ["m68k-elf-gcc", *build_c.CFLAGS, f"-I{INC}", *[f"-D{d}" for d in defines], "-S", "-o", "-", str(SRC)]
    raw = build_c.run(cmd)
    lines = []
    for line in raw.splitlines():
        if build_c.BAD.match(line):
            sys.exit(f"build_comp: refused: {line.strip()!r} (no .data/.bss/.comm in the unit)")
        m = build_c.SECTION.match(line)
        if m:
            if m.group(1).startswith(".rodata"):
                line = "        .text"
            else:
                sys.exit(f"build_comp: refused: section {m.group(1)} (only .text/.rodata allowed)")
        lines.append(line)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    with tempfile.TemporaryDirectory() as d:
        o = pathlib.Path(d) / "c.o"
        build_c.run(["m68k-elf-as", "-mcpu=5407", "-o", str(o), str(out)])
        nm = build_c.run(["m68k-elf-nm", str(o)]).splitlines()
        size = build_c.run(["m68k-elf-size", "-A", str(o)])
    undef = sorted({l.split()[-1] for l in nm if l.split()[0] == "U"})
    glob = sorted({l.split()[-1] for l in nm if len(l.split()) == 3 and l.split()[1] == "T"})
    sec = {l.split()[0]: int(l.split()[1]) for l in size.splitlines() if l.startswith(".")}
    if undef:
        sys.exit(f"build_comp: refused: undefined symbols {undef}")
    if any(v and k not in (".text", ".comment") for k, v in sec.items()):
        sys.exit(f"build_comp: refused: non-text sections {sec}")
    if not quiet:
        print(f"wrote {out}: .text {sec.get('.text', 0)} B, .data {sec.get('.data', 0)} B, .bss {sec.get('.bss', 0)} B; "
              f"exports {glob}")
    return sec, glob


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "out" / "otype" / "comp" / "otc_comp.s")
    ap.add_argument("--no-dump", action="store_true")
    ap.add_argument("--define", action="append", default=[])
    a = ap.parse_args()
    build(a.out, list(a.define) + (["OTC_NO_DUMP"] if a.no_dump else []))


if __name__ == "__main__":
    main()
