#!/usr/bin/env python3
"""Build the OType firmware module into build/out/.

    python3 build/build.py                                    # the four assembly units only
    python3 build/build.py --base 0x40a955e0 --region 0x4127fc30   # ... then link them (build/link.py)

  1. build/cunits.py   c/*.c  -> out/otype_c.s, out/otype_comp.s, out/otype_script.s   (m68k-elf-gcc)
  2. build/units.py    src/*  -> out/otype.s                                            (m68k-elf-as, trial link)
  3. build/link.py     the four units -> out/otype.bin, out/otype.elf, out/otype.json   (only with --base and --region)

The units choose no address: the buffers are offsets from the symbol `otype_rgn`, a block of DRAM the integrating tool
reserves (build/layout.py), and the code is linked where that tool will put it. Step 3 resolves both. No Elektron data
is read: everything comes from this tree. Putting the binary in an image and writing the entry point is the
integrating tool's job (README.md, "Integration contract").
"""
import argparse
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "out")
    ap.add_argument("--base", help="link address of the code (hex); with --region, also link")
    ap.add_argument("--region", help="address of the otype_rgn block (hex); with --base, also link")
    a = ap.parse_args()
    if (a.base is None) != (a.region is None):
        sys.exit("build: --base and --region go together")
    cmds = [[sys.executable, HERE / "cunits.py", "--out", a.out],
            [sys.executable, HERE / "units.py", "--out", a.out / "otype.s"]]
    if a.base is not None:
        cmds.append([sys.executable, HERE / "link.py", "--units", a.out, "--out", a.out, "--base", a.base,
                     "--region", a.region])
    for cmd in cmds:
        r = subprocess.run([str(c) for c in cmd])
        if r.returncode:
            sys.exit(r.returncode)
    print(f"build: done -> {a.out}")


if __name__ == "__main__":
    main()
