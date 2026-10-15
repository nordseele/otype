#!/usr/bin/env python3
"""Link the four OType units into one flat binary, at the addresses your integrating tool chose.

    python3 build/link.py --base 0x40a955e0 --region 0x4127fc30 [--units build/out] [--out build/out]

  --base     where the code will sit in DRAM when it runs (the binary is not position-independent)
  --region   the address of the otype_rgn block (build/layout.py: its size and alignment), reserved by your tool

Writes into --out:
  otype.bin   the code and its tables, one flat image to copy to --base (no .data, no .bss outside it)
  otype.elf   the same, with symbols
  otype.json  what your tool must do with it: the load address and size, the memory block, and the entry point
              (the stock site, the span to overwrite, the six-byte `jmp` to write there, the instructions it displaces
              and where the module resumes the stock code)

Every unit is assembled with -mcpu=54455 (the unit's CPU; -mcpu=5407 encodes a few instructions differently) and
linked with GNU ld (`-Ttext=<base> --defsym otype_rgn=<region>`), in this
order: otype.s, otype_c.s, otype_comp.s, otype_script.s (build/build.py makes them). Nothing here reads an Elektron
image: the bytes written at the entry points are this module's own (`jmp` + `nop`); the stock instructions they replace
are named, not copied. README.md, "Integration contract", is the full description.
"""
import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import layout as L          # noqa: E402

UNITS = ("otype.s", "otype_c.s", "otype_comp.s", "otype_script.s")
DRAM_LO, DRAM_HI = 0x40000000, 0x48000000          # cached DRAM; 0x48000000 up is its uncached alias
STOCK_LO, STOCK_HI = 0x40000400, 0x4010fdf0        # the stock OS image (OS 1.40C, 1,112,560 B at 0x40000400)
JMP_ABS_L, NOP = 0x4EF9, 0x4E71                    # `jmp (xxx).l`, `nop`

# The place where the stock OS must enter the module (OS 1.40C). `span` = the bytes to overwrite: a six-byte
# `jmp (target).l`, then `nop`s up to the span (never executed: the module jumps back past them).
ENTRY_POINTS = (
    {"name": "storage_job", "site": 0x4008485E, "span": 6, "symbol": "otype_st",
     "displaced": "mvz.b (%a2),%d0 ; moveq #45,%d1 ; cmp.l %d0,%d1",
     "resumes": "0x40084864 for every job but 0x45 (the displaced instructions replayed); 0x4008484e (the task's next "
                "job) after job 0x45",
     "what": "the storage task reads a job's type: job 0x45 is the module's (it writes /SCRIPTS/ERRORS.TXT); any other "
             "job goes on to the stock jump table"},
)
# the C entry points the assembly unit calls; `.weak` there, so a missing C unit would link silently to 0
C_SYMBOLS = ("otb_host_commit", "otc_vm_load", "otc_vm_bind", "otc_vm_bind_work", "otc_vm_run", "otc_compile", "ots_load")


def run(cmd, cwd):
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode:
        sys.exit(f"link: {pathlib.Path(str(cmd[0])).name} failed:\n{r.stdout}{r.stderr}")
    return r.stdout


def hexint(s):
    return int(s, 16)


def check_addresses(base, region, size):
    errs = []
    if base & 3:
        errs.append(f"--base 0x{base:08x} is not long-aligned")
    if region % L.REGION_ALIGN:
        errs.append(f"--region 0x{region:08x} is not {L.REGION_ALIGN}-aligned")
    for what, lo, hi in (("the code", base, base + size), ("the block", region, region + L.REGION_BYTES)):
        if not (DRAM_LO <= lo and hi <= DRAM_HI):
            errs.append(f"{what} 0x{lo:08x}..0x{hi:08x} is not in cached DRAM (0x{DRAM_LO:08x}..0x{DRAM_HI:08x})")
        if lo < STOCK_HI and STOCK_LO < hi:
            errs.append(f"{what} 0x{lo:08x}..0x{hi:08x} overlaps the stock OS image (its free-looking tail is live)")
    if base < region + L.REGION_BYTES and region < base + size:
        errs.append("the code and the block overlap")
    if errs:
        sys.exit("link: " + "; ".join(errs))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, type=hexint)
    ap.add_argument("--region", required=True, type=hexint)
    ap.add_argument("--units", type=pathlib.Path, default=HERE / "out")
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "out")
    a = ap.parse_args()
    missing = [u for u in UNITS if not (a.units / u).is_file()]
    if missing:
        sys.exit(f"link: missing {missing} in {a.units}: run build/build.py first")
    a.out.mkdir(parents=True, exist_ok=True)
    elf, binf = a.out / "otype.elf", a.out / "otype.bin"
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        objs = []
        for i, u in enumerate(UNITS):
            o = d / f"{i}_{u[:-2]}.o"
            run(["m68k-elf-as", "-mcpu=54455", "-o", o, (a.units / u).resolve()], d)
            objs.append(o)
        run(["m68k-elf-ld", f"-Ttext=0x{a.base:x}", f"--defsym={L.REGION_SYMBOL}=0x{a.region:x}", "-e", "0",
             "--no-warn-rwx-segments", "-o", elf.resolve(), *objs], d)
    sections = [ln.split() for ln in run(["m68k-elf-objdump", "-h", elf], a.out).splitlines()
                if ln.strip()[:1].isdigit()]
    alloc = [s[1] for s in sections if s[1] not in (".comment",)]
    if alloc != [".text"]:
        sys.exit(f"link: expected one .text section, got {alloc}")
    run(["m68k-elf-objcopy", "-O", "binary", elf, binf], a.out)
    blob = binf.read_bytes()
    check_addresses(a.base, a.region, len(blob))
    syms = {}
    for ln in run(["m68k-elf-nm", elf], a.out).splitlines():
        f = ln.split()
        if len(f) == 3:
            syms[f[2]] = int(f[0], 16)
    absent = [s for s in [e["symbol"] for e in ENTRY_POINTS] + list(C_SYMBOLS)
              if not a.base <= syms.get(s, 0) < a.base + len(blob)]
    if absent:
        sys.exit(f"link: symbols missing from the linked code: {absent}")
    entries = []
    for e in ENTRY_POINTS:
        target = syms[e["symbol"]]
        write = JMP_ABS_L.to_bytes(2, "big") + target.to_bytes(4, "big") + NOP.to_bytes(2, "big") * ((e["span"] - 6) // 2)
        entries.append({"name": e["name"], "site": f"0x{e['site']:08x}", "span": e["span"], "symbol": e["symbol"],
                        "target": f"0x{target:08x}", "write": write.hex(), "displaced": e["displaced"],
                        "resumes": e["resumes"], "what": e["what"]})
    desc = {"os": "Octatrack MKI OS 1.40C", "base": f"0x{a.base:08x}", "size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "region": {"symbol": L.REGION_SYMBOL, "address": f"0x{a.region:08x}", "size": L.REGION_BYTES,
                       "align": L.REGION_ALIGN, "initialised": False},
            "entry_points": entries}
    (a.out / "otype.json").write_text(json.dumps(desc, indent=2) + "\n")
    print(f"link: otype.bin {len(blob):,} B at 0x{a.base:08x} (sha256 {desc['sha256'][:16]}...); "
          f"{L.REGION_SYMBOL} {L.REGION_BYTES:,} B at 0x{a.region:08x}")
    for e in entries:
        print(f"link:   {e['name']:14s} {e['site']} ({e['span']} B) <- {e['write']}  ({e['symbol']} = {e['target']})")


if __name__ == "__main__":
    main()
