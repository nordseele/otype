# OType firmware module

The OType engine for the Elektron Octatrack, OS 1.40C, as sources: the on-unit compiler, the VM, the `/SCRIPTS` loader,
card and sample-slot access. It ships **no user interface**: your firmware lists the scripts and decides when a function
runs. It does not depend on any particular patching tool: you build a flat binary with `build/build.py`, and your own
tool puts it in memory and makes the stock OS enter it at one place. This README is the contract between the two. No
firmware image and no Elektron bytes are distributed here, ever.

Developed by Octalab / Nordseele. OType is used extensively in [OT1](https://github.com/nordseele/ot1); we share it with
other custom firmware authors to make it easier to write functions "by hand". Build whatever interface you like around
it ([Your own interface](#your-own-interface)). We cannot offer support, as our time goes to OT1; this contract and the
cookbook are written so that a coding agent can follow them.

**Status:** the language runs on the Octatrack MKI inside OT1. The standalone engine is tested in an emulator only,
after stock OS startup with its code and entry point installed by the emulator fixture. This checks the engine;
the integrating firmware must separately validate its boot loader and memory reservation.

| | |
|---|---|
| `--base 0x40a955e0 --region 0x4127fc30` | example placement; each link records the exact size and SHA256 in `build/out/otype.json` |

## What it does

- LOAD reads `/SCRIPTS/*.OTY` from the card root. The OType compiler runs **on the unit**; the report goes to
  `/SCRIPTS/ERRORS.TXT`. A function with an error is refused, everything else loads (language: `../docs/syntax.md`).
- A script runs on the current pattern and commits the stock way (trigs, locks, parts, scenes, sample slots, files on
  the card). Your interface asks the parameters and the YES/NO of destructive or `confirm` scripts before the run.

## Requirements

- `m68k-elf-gcc`, `m68k-elf-as`, `m68k-elf-ld`, `m68k-elf-objcopy`, `m68k-elf-objdump`, `m68k-elf-nm` on the `PATH`
  (Homebrew: `m68k-elf-gcc`, `m68k-elf-binutils`).
- `python3`, standard library only.
- A tool of your own (or someone else's) that can: unpack and repack the OS image, copy a binary into DRAM before its
  first use, keep a block of DRAM away from the stock OS, and write one short jump into the OS.

## Build

```sh
python3 build/build.py                                        # the four assembly units, into build/out/
python3 build/build.py --base 0x40a955e0 --region 0x4127fc30  # ... and link them there (build/link.py)
python3 build/layout.py                                       # the memory block and its buffers
```

1. `build/cunits.py` compiles the three C units (VM, on-card compiler, `/SCRIPTS` loader; `m68k-elf-gcc -mcfv4e -Os
   -ffreestanding -nostdlib -Werror`, no libgcc, no `.data`/`.bss`) to `otype_c.s`, `otype_comp.s`, `otype_script.s`.
2. `build/units.py` generates `otype.s`, the assembly unit (`src/`), and checks it with a trial link.
3. `build/link.py` (with `--base` and `--region`) assembles the four units with `-mcpu=54455` and links them with GNU
   ld at `--base`, with `otype_rgn` at `--region`. It writes:
   - `otype.bin`: one flat image, code and variables, to copy to `--base`;
   - `otype.elf`: the same with symbols;
   - `otype.json`: the load address, size and sha256, the memory block, and the entry point with the bytes to write
     (computed from the link, see below).

   It refuses a base or a block outside cached DRAM, on the stock OS image, or overlapping each other.

## Integration contract

### 1. The code

- Reserve the entire code area at `--base`, as well as the separate memory block below, against all stock writes.
  Copy `otype.bin` after stock startup initialisation and before your interface first calls it, and make the instruction
  cache coherent with it (push and invalidate the caches after the copy). Appending bytes to an OS image alone does
  not reserve or load that area: startup or project loading can overwrite it.
- The binary is linked for that address: it is **not position-independent**. Link again for another address.
- It contains its own variables (one `.text` section, nothing else), so the copy must be writable RAM.
- Nothing has to be called at boot: the module initialises itself on its first use. It installs no interrupt handler and
  no task: it runs in the stock UI task that your interface calls it from, and in the stock storage task (below).

### 2. The memory block `otype_rgn`

2,187,688 B (2.09 MiB), aligned to 16, **for the module alone**. Nothing else may use it: not the stock OS, not its sample
memory, not another module. Your tool decides where it lies (taking it out of the sample memory is the usual way) and
passes its address to `link.py --region`. It need not be initialised: the module clears what must be zero before its
first run and writes every other buffer before it reads it. Its buffers (`build/layout.py`):

| | |
|---|---|
| guard | 16 B |
| VM instance + mailbox | 38,912 B |
| the VM's loaded table | 32,768 B |
| VM workspace (card lists, pick bags) | 669,840 B |
| compiler staging table | 32,768 B |
| report text (`ERRORS.TXT`) | 262,400 B |
| sources, back to back | 65,544 B |
| source records, labels | 2,048 + 34,816 B |
| compiler arena (1 MiB, its 40 KiB stack at the top) | 1,048,576 B |

The code itself (`--base`) is outside the block; reserve its full linked size from `otype.json` too.

### 3. The entry point

At the site, write a six-byte `jmp (target).l` to the module's symbol, then `nop`s up to the span. `otype.json` gives
the exact bytes (`write`); they are this module's own instructions, not Elektron's. **Check first that your image holds
the displaced instructions at the site** (OS 1.40C): if it does not, the image is not the one this module was written for,
or another patch is already there.

| Entry | Site, span | Jump to | Displaced stock instructions | The module goes back to |
|---|---|---|---|---|
| storage task, job type | `0x4008485e`, 6 B | `otype_st` | `mvz.b (%a2),%d0 ; moveq #45,%d1 ; cmp.l %d0,%d1` | `0x40084864` after replaying them (every job but `0x45`); `0x4008484e`, the next job, after job `0x45` |

- `otype_st` runs in the stock storage task, on every job: job `0x45` is the module's (it writes `/SCRIPTS/ERRORS.TXT`,
  posted by LOAD); any other job goes on to the stock jump table. Job type `0x45` must stay unused by anything else.
- Nothing else in the OS is changed: no table, no menu, no other hook. How your interface is reached (a key, a menu,
  a trig) is your firmware's own hook.

### 4. The audio imports

The audio operators (`src.save`, `flex.*`, `cat.*`, `norm`) call nine C functions that the integrating firmware
provides: the take, the Flex slots, the card's WAV and `.ot` files. Their signatures, errors and rules are in
[`../docs/audio-imports-contract.md`](../docs/audio-imports-contract.md). `build/cunits.py` links weak defaults
(`c/audio_default.c`) so that the engine builds without them: no take is ever available and every audio operator
stops the run with IO ERROR. Your own definitions replace the defaults at link time.

### 5. The sequencer and module imports

The module operators (`play`, `nx.smp`, `nx.pl`, `seq.run`, `seq.step`, `seq.mute`) call four C functions that the integrating firmware
provides:
- `otb_host_module_play(uint32_t track0, const uint8_t *locks32)`: schedules voice playback on track `track0` (0..7) at the module's deadline with the 32 lock bytes `locks32` (`0..29` = parameter locks, `31` = 0-indexed Static sample slot `0..127`, `0xFF` = no lock);
- `otb_host_seq_run()`: returns 1 if the sequencer transport is currently playing, 0 if stopped;
- `otb_host_seq_step(uint32_t track0)`: returns the current playing step (1..64) of track `track0` (0..7);
- `otb_host_seq_mute(uint32_t track0)`: returns 1 if track `track0` (0..7) is muted, 0 if active.

Weak defaults are linked from `c/audio_default.c`: `otb_host_module_play` does nothing, `otb_host_seq_run` returns 0,
`otb_host_seq_step` returns 1, and `otb_host_seq_mute` returns 0. The integrating firmware provides its own definitions
at link time.

### 6. What the module assumes about the OS

- **OS 1.40C, unchanged at the addresses the sources name**: the stock routines and globals listed in `src/stock.inc` and
  in the `.equ` block at the top of each `src/*.S`. They are addresses and record layouts only.
- **The effect choosers.** Effect numbers in scripts (`fx.*`) index the stock FX1 / FX2 lists at `0x400d6060` and
  `0x400d6090`. If your tool rebuilds the choosers, keep the stock effects in stock order in both.
- **Shared code.** OType calls stock writers instead of writing memory itself: the part commit (`0x40029a4c`), the effect
  applies (`0x400526e4`, `0x40052474`), the parameter set (`0x40054cd8`), the sample-slot loads and the file-system calls.
  Another patch that rewrites one of those changes what a script does: test the combination on the unit.
- **Not combinable** with another patch on the entry site.

## Your own interface

The engine has no interface of its own. Yours drives it from the stock UI task:

- **Load**: `otype_ot_load` (UI task) reads `/SCRIPTS`, compiles, stages the table and posts the job that
  writes `ERRORS.TXT`; the loaded functions and their menu paths are in `ot_ctx` (`ots_ctx_t`, `c/otc_script.c`).
- **Prepare**: call `otype_prepare` and require `d0 = 0` after loading, before accessing C metadata readers or setting
  parameters. It indexes a pending table without running a script. First load through `otype_ot_load`: its first-use
  clearing initializes the VM/table/workspace; preparation alone does not replace that clearing. Your interface must
  follow this order, even when the reserved memory initially contains arbitrary bytes.
- **List**: after a load, `ot_nloaded` is the number of loaded actions; the action array of `pop_rows` holds LOAD as
  action 0, then one action per loaded function, filled by `ots_load`; `ot_ctx` gives each function's menu path
  (category, name), for your own sorting.
- **Run**: `otype_ot_menu(slot)` runs a loaded menu action with a fresh seed; lower down, `otype_run` runs function
  `otr_mail+4` with seed `otr_mail+8` on the current pattern and commits it (the mailbox is described at the top of
  `src/run.S`).
- **Ask before running**: after preparation, the C functions of `c/otc_vm.h` read the loaded table: `otc_vm_fn_param` (each parameter: name,
  type, range, default, option labels, folder), `otc_vm_fn_confirm` (the `confirm` text), `otc_vm_fn_page` (the page
  label); `otc_vm_set_params` and `otc_vm_set_param_folder` pass the values chosen in your interface to the next run.
- **Compile only**: `otc_compile` (`c/otc_comp.h`) turns sources into a table and a report, with no card and no UI.

Calling conventions: the C units are cdecl (arguments on the stack, 4 bytes each, result in `d0`, `d2-d7/a2-a6`
preserved); the assembly entry points say what they keep in their header comments.

## Layout

| | |
|---|---|
| `src/` | ColdFire assembly: `stock.inc` (stock addresses, equates only), `shim.S` (storage entry, row helpers, file objects), `bridge.S` `run.S` `card.S` (host imports of the VM, the run commit, card and slot access), `script.S` (LOAD, run, `ERRORS.TXT`), `auto.S` (a quiet first load) |
| `c/` | C sources of the three units: VM (`vm.c`, `otc_vm.h`), compiler (`otc_comp*.c`), `/SCRIPTS` loader (`otc_script.c`); `otc_ops.h` and `otc_opreg.h` are generated from `modules/otype/ops.py` and the language tables |
| `build/` | `build.py`, `cunits.py`, `units.py`, `link.py`, `layout.py` (the block's size and offsets); products go to `build/out/` |

The tests of the computer-side tools (checker, simulator, compiler) are in `../tools/`. The tests of the C against the
Python reference and the emulated walk-through are not published: they need a pinned emulator harness.

## Licence

GPL-3.0, see `../LICENSE`.
