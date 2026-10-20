# OType cookbook

Short recipes, one need each: copy one into a `.OTY` file of `/SCRIPTS` on the card, or start from it.
Every recipe below is checked by the test suite (`tools/test_otype_run.py`, `CookbookRecipes`): it passes the
checker with the unit's profile and runs in the simulator. Firmware authors: the last part, "Integrating the
engine", is for you. The language itself is in `docs/syntax.md`; every
operation, with its arguments, is in the command reference (`docs/cheatsheet.html`).

Try any recipe on the computer before the card:

```sh
python3 tools/otype.py check --profile m1 MY.OTY
python3 tools/otype.py run four_floor MY.OTY --seed 1
python3 tools/otype.py run four_floor MY.OTY --seed 1 --bank PATH/TO/bank01.work --pattern 1   # your own pattern, read only
```

Conventions used here: a function acts on the **current track** and the **current pattern** unless it says
`trk …`; steps are numbered from 1; `i` is the step (or loop index) being visited.

---

## 1. The smallest script: four on the floor

```otype
def four_floor
  menu "BASICS" >> "FOUR FLOOR"
  confirm "REPLACE TRIGS?"
  tr.clr
  l 1 16: if eq mod i 4 1: tr.add i
```

- `menu "CATEGORY" >> "NAME"` puts the function in the script menu of your firmware, under its category.
- `confirm` asks YES/NO first, with your own question; use it on anything that removes something.
- `l 1 16:` loops `i` from 1 to 16; `if … :` runs the rest of the line when the condition is not 0.

## 2. Parameters the player sets before running

```otype
def euclid_p
  menu "BASICS" >> "EUCLID"
  param hits 5 1 16
  param rot 0 0 15
  steps all:
    each:
      if er.r hits 16 rot i: tr.add i
      else: tr.del i
```

`param NAME default min max` declares a value the player sets before the run, between `min` and `max`; the
firmware's menu shows it. `steps all:` + `each:` visits every step; `er.r k n r s` is 1 when step `s` plays in a
euclidean rhythm of `k` hits over `n` steps rotated by `r`.

## 3. Typed parameters: yes/no, a choice, a folder

```otype
def density
  menu "BASICS" >> "DENSITY"
  param amount 30 0 100
  param keep bool 1
  param grid option 0 "16THS" "8THS" "4THS"
  a 1
  if eq grid 1: a 2
  if eq grid 2: a 4
  l 1 16:
    if not keep: tr.del i
    if eq mod sub i 1 a 0: prob amount: tr.add i
```

- `bool` shows YES/NO (1/0); `option` shows labels, the script reads their index from 0.
- `prob p:` runs the rest of the line `p` percent of the time.
- `a`…`z` (except `i`) are variables; `a 1` sets `a`, `sub i 1` is `i - 1` (prefix notation).

## 4. Random, but the same every time: seeds

```otype
def sprinkle_seeded
  menu "BASICS" >> "SPRINKLE 7"
  seed 7
  l 1 16: prob 25: tr.add i
```

Without `seed`, each press draws new numbers. With `seed n`, a press always gives the same result; the
simulator and the unit agree on it, so `run --seed` shows exactly what the machine will do.

## 5. One press a variation, the next press back: state between presses

```otype
def ab_fx
  menu "LOCKS" >> "A/B FX"
  seed ? flip 7 0
  steps has.trig:
    pl.rnd fx1 1
```

`flip` alternates 0, 1, 0… on each read and keeps its value between presses (so do `o` and `drunk`).
`? c x y` is `x` when `c` is not 0, else `y`: odd presses use seed 7, even presses seed 0.
`steps has.trig:` restricts the next lines to the steps that hold a trig.

## 6. Accents with velocity curves and p-locks

```otype
def accents
  menu "LOCKS" >> "ACCENTS"
  param curve option 1 "FLAT" "BEATS" "OFFBEATS" "RAMP UP" "RAMP DN" "8THS" "TRESILLO" "SWELL"
  steps has.trig:
    each: pl.set i amp.vol dr.v curve i
```

`dr.v c s` is a velocity 0..127 from one of eight 16-step curves. `pl.set s p v` locks parameter `p`
(here `amp.vol`) on step `s`; a step without a trig is skipped and counted, never turned into a trig.
Parameter names: `amp.vol`, `lfo.spd 1`…, or by page and position: `fx1 1` is the first FX1 parameter.

## 7. Humanize: micro timing and gentle volume

```otype
def humanize
  menu "TRIGS" >> "HUMANIZE"
  param amount 4 0 23
  micro.rnd amount
  pl.vary amp.vol 10
```

`micro.rnd x` shifts each trig by up to `x` micro steps either side. `pl.vary p x` moves existing locks of
`p` by up to `x` percent; steps without that lock are left alone. Both already visit every step with a trig:
wrapping them in `steps has.trig: each:` would run them once per trig and cost sixteen times more (the
checker refuses it: over 4096).

## 8. Trig conditions: a fill on the last bar

```otype
def fill_end
  menu "TRIGS" >> "FILL END"
  l 13 16:
    tr.add i
    cond.set i fill
  cond.set 16 1:2
```

`cond.set s c` writes the trig condition: `fill`, `!fill`, `pre`, `nei`, `1st`, a ratio `a:b`, or a
probability among the Octatrack's own (`25%`, `50%`…). `off` removes it.

## 9. Fill the Static slots from a folder the player picks

```otype
def static_from
  menu "STATIC" >> "FILL FROM"
  param src folder "AUDIO"
  dir src deep
  ext wav aif aiff
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.pick
```

`dir path [deep]` lists the files of a folder of the current set (`deep`: with its subfolders); `ext` and
`match` filter that list. The loop gives every free Static slot a random file of the list, never the same
one twice (`file.pick`); it stops when the files or the free slots run out. `static.load n f` loads file `f`
of the list into Static slot `n`; `static.firstfree` is the first free one (0 if none). Lists hold up to 4096 names.
The Static list is the sample list of the project: the Flex list is separate. More ways to fill, clear and
randomize the slots are in recipe 16.

## 10. A kit from a folder, locked at random on the trigs

```otype
def kit_lock
  menu "LOCKS" >> "KIT LOCK"
  param src folder "AUDIO"
  param files 4 1 8
  dir src deep
  match "*hat*" "*hh*"
  slots.clr
  l 1 files:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.pick
    slots.add a
  steps has.trig:
    each: smp.set i slots.rnd
```

`file.pick` never gives the same file twice in a row of picks; `slots.clr` / `slots.add` build a working
set of slots, `slots.rnd` draws one of them. `break` leaves the loop (no free slot, or no file left).

## 11. Several tracks in one press, with your own functions

```otype
def beat
  menu "DRUMS" >> "BEAT"
  confirm "REPLACE T1-T3?"
  hits_on | 1 4 0
  hits_on | 2 2 4
  hits_on | 3 7 0

def hits_on | track hits rot
  trk track:
    tr.clr
    l 1 16: if er.r hits 16 rot i: tr.add i
```

`def name | args` declares a function that takes arguments; call it with `name | values`. A function
without `menu` is not shown in the menu: it is a brick for the others. `trk t:` acts on track `t` for the
rest of its block.

## 12. A walk through samples: drunk

```otype
def smp_walk
  menu "LOCKS" >> "SMP WALK"
  drunk.min 1
  drunk.max 8
  drunk 4
  steps has.trig:
    each: smp.set i drunk
```

`drunk` moves by -1, 0 or +1 on each read, between `drunk.min` and `drunk.max`: neighbouring trigs get
neighbouring sample slots.

## 13. An effect and an LFO, set in the part

```otype
def wobble
  menu "SOUND" >> "WOBBLE"
  param depth 90 0 127
  fx.type 1 1
  lfo.dest 1 fx1 1
  par.set lfo.spd 1 rrand 16 48
  par.set lfo.dep 1 depth
```

`fx.type x y` picks entry `y` of effect slot `x`'s list (0 = none). `lfo.dest x p` aims LFO `x` at a
parameter; `par.set p v` writes a parameter of the current part (not a lock): the value the track plays
wherever no lock says otherwise, clamped to its range. A part holds much more than this (the machines and their
slots, every parameter page, the scenes); the recipe changes one effect and one LFO of the current track only.
These changes survive a power cycle; RELOAD PART on the unit goes back to the saved part.

## 14. Scenes to play with the crossfader

```otype
def scenes_new
  menu "SCENES" >> "NEW SCENES"
  confirm "REPLACE SCENES?"
  scn.clr
  l 2 16:
    l 6 11:
      scn.set j i rrand par.min i par.max i
    l 18 29:
      scn.set j i rrand par.min i par.max i
```

`scn.clr` unlocks all 16 scenes; the loops put a random lock on every LFO and FX parameter (6..11 and
18..29) of scenes 2..16 (`scn.set scene param value`; `j` is the scene, `i` the parameter). Put scene 1 on A,
any other on B, and move the crossfader. `par.min p` and `par.max p` are the bounds of parameter `p` on the
current track, so the draw stays inside what the parameter accepts; a parameter the part disables is refused
(and counted) by all three operations.

## 15. A page of six encoders

```otype
def dens_page
  page "DENS"
  param pct 25 0 100
  each:
    if not has.trig i:
      prob pct: tr.add i
```

`page "LABEL"` puts the function on a page of encoders instead of the menu (when your firmware has one: the
engine itself only lists `menu` functions); its parameters (at most six) are
the encoders A..F. The page reruns it each time an encoder moves, on the pattern as you left it, and keeps
the result as a layer you print or discard. A page function takes no argument and does not touch the card,
the Static slots.

## 16. Bulk jobs on the Static slots, scenes, LFOs and effects, as loops

OType has no one-shot helper for these: a few lines of loop do each, and you can change them. Each recipe
below stands alone.

### Fill the Static slots

```otype
def static_fill
  menu "STATIC" >> "FILL"
  param src folder "AUDIO"
  dir src deep
  ext wav aif aiff
  l 1 128:
    if gt i file.len: break
    a static.firstfree
    if ez a: break
    static.load a file.at i
```

`file.at i` is the i-th file of the list in its sorted order: the free slots take the files in order.
A file the unit refuses leaves its slot free, and the next file is offered to it.

### Fill the Static slots at random

Recipe 9: `file.pick` instead of `file.at i`.

### Shuffle the Static slots

```otype
def static_shuffle
  menu "STATIC" >> "SHUFFLE"
  l 1 128:
    if static.free i: break
    static.swap i rrand 1 i
```

`static.swap a b` trades the samples of two Static slots: each is reloaded by name from the other, an empty
slot takes the sample and leaves its partner empty, and `a` = `b` does nothing. The loop is a Fisher-Yates
shuffle: slot `i` swaps with a random slot from 1 to `i`. It stops at the first free slot, so it shuffles the run
of used slots from slot 1 (what the fill recipes leave) and leaves the rest alone. A sample the unit refuses to
reload empties its slot, as `static.load` does.

To shuffle every slot, holes included, run the same shuffle over the 128 slots:

```otype
def static_shuf_all
  menu "STATIC" >> "SHUFFLE ALL"
  l 2 128: static.swap i rrand 1 i
```

Every sample can now land anywhere, including in a slot that was empty, and an empty slot can take the place of a
used one. Use the first recipe to keep the holes where they are, this one when the slots are not filled from slot 1.

### Clear all Static slots

```otype
def static_clear
  menu "STATIC" >> "CLEAR"
  confirm "CLEAR ALL STATIC SLOTS?"
  l 1 128:
    static.clr i
```

### Random scenes

Recipe 14.

### Random LFOs

```otype
def lfos_random
  menu "LFO" >> "RANDOM LFOS"
  l 6 11:
    par.set i rrand par.min i par.max i
  l 1 3:
    lfo.dest i rrand 18 29
    lfo.wave i rrand 0 6
```

Parameters 6..11 are the speeds and depths of the three LFOs (`lfo.spd n`, `lfo.dep n`); `lfo.dest x p`
aims LFO `x` at one of the effect parameters (18..29) and `lfo.wave x w` picks its waveform (0..6 on a stock
unit). `par.min p` and `par.max p` give the bounds of parameter `p`, so the draw covers its whole range;
`par.set` clamps the value to it anyway.

### Random effects

```otype
def fx_random
  menu "FX" >> "RANDOM FX"
  fx.type 1 rrand 1 sub fx.count 1 1
  fx.type 2 rrand 1 sub fx.count 2 1
```

`fx.type x y` picks entry `y` of effect slot `x`'s list; 0 is NONE, so the draw starts at 1. `fx.count x` is the
number of entries of that list, NONE included: the last entry is `fx.count x` minus 1, whatever the firmware (the
FX2 list changes with a remixed firmware). A list that holds NONE alone makes the draw 0 or 1; 1 is out of range
and counted, 0 leaves the effect off.

### Random LFO designer

```otype
def lfod_random
  menu "LFO" >> "RND DESIGNER"
  l 1 16:
    lfod.set i rrand -64 63
```

### Invert the LFO designer

```otype
def lfod_invert
  menu "LFO" >> "INV DESIGNER"
  l 1 16:
    lfod.set i sub 0 lfod.get i
```

`lfod.set` clamps to -64..63, so an inverted -64 becomes 63.

---

## 17. Show where `pat.go` landed

After a run the menu says `DONE`. `msg` replaces it with your own text, so you can see which pattern or bank a
script chose:

```otype
def go_rnd_pat
  menu "BASICS" >> "RND PATTERN"
  a add rnd 16 1
  b bank.cur
  pat.go a b
  msg "BANK" b "PAT" a
```

The end message reads `BANK 2 PAT 7` (a number, a space, the next item; the last `msg` of the run wins). **Show the
variables you gave to `pat.go`, not `pat.cur` / `bank.cur` read afterwards:** when the sequencer is playing, the
Octatrack queues a pattern change until the end of the current pattern (the stock call `pat.go` uses does that
itself), so `pat.cur` right after `pat.go` can still say the old pattern, while the simulator already answers the
target. The variables are right in both cases. A text is at most 29 characters in the worst case, a number counting
as 6 (`-32768`): `msg "TEMPO" tempo` is fine, a long sentence is refused when the script loads.

## 18. Save an audio take and load it into a Flex slot

```otype
def bounce_take
  menu "AUDIO" >> "BOUNCE"
  input sample
  src.save
  flex.load 1 file.at 1
  msg "SAVED TAKE"
```

`input sample` declares that the function operates on the active audio sample context (recording buffer or audio editor).
`src.save` commits the take to durable storage on the card; upon return, `file.at 1` holds the newly written WAV file path.
`flex.load 1 file.at 1` assigns the new recording to Flex slot 1.

To keep every take, use the first free Flex slot:

```otype
def bounce_free
  menu "AUDIO" >> "BOUNCE FREE"
  input sample
  src.save
  a flex.firstfree
  if nz a:
    flex.load a file.at 1
    msg "SLOT" a
```

Clearing all Flex slots is the same loop as for Static:

```otype
def flex_clear
  menu "AUDIO" >> "FLEX CLEAR"
  confirm "CLEAR ALL FLEX SLOTS?"
  l 1 128:
    flex.del i
```

`flex.firstfree` is the number of the first free Flex slot (0 if all 128 are used), the Flex twin of `static.firstfree`.
`flex.del n` empties Flex slot `n` and gives its memory back, the Flex twin of `static.clr`; an empty slot is left alone.
Like `flex.load` over a used slot, it is refused with STOP FIRST while the sequencer plays.
The eight recorder buffers are not Flex slots: `flex.firstfree` never counts them and `flex.del` cannot empty them
(both experimental).

## 19. Build a sliced sample chain from a folder

```otype
def chain_folder
  menu "AUDIO" >> "CHAIN"
  param src folder "AUDIO"
  dir src
  ext wav
  cat.clr
  l 1 16:
    if gt i file.len: break
    cat.add file.at i
  if nz file.len:
    cat.save "CHAIN"
    flex.load 1 file.at 1
```

`cat.clr` clears the host's concatenation list (up to 64 files). `cat.add` appends files from the current directory list.
`cat.save "NAME"` stitches the concatenated files into a single WAV with automatic `.ot` slice markers per source file,
saved in the context directory or `/AUDIO`. Upon success, `file.at 1` points to the generated chain.
(Experimental. A very large folder is slow to read on the unit itself, before the first file is processed: allow time.)

## 20. Batch normalize samples

```otype
def norm_folder
  menu "AUDIO" >> "NORM -3DB"
  param src folder "AUDIO"
  dir src
  ext wav
  l 1 16:
    if gt i file.len: break
    norm file.at i -3
```

`norm path db` scans all frames of a WAV file on the card and calculates the gain needed to reach the target peak
level (-12..0 dB), updating the `.ot` metadata in place without destructive audio rewrites.
(Experimental. A very large folder is slow to read on the unit itself, before the first file is processed: allow time.)

## 21. Live sequencing module

```otype
def pulse_m
  in clock 24:
    if eq seq.mute 1 0:
      nx.smp rrand 1 4
      nx.pl fx1 1 rrand 40 110
      play 1
```

`in clock N:` registers a live sequencer module executed every N ticks (24 ticks = 1 quarter note). On each clock tick, the module can query sequencer status (`seq.run`, `seq.step t`, `seq.mute t`), prepare per-hit locks for the next hit with `nx.smp n` (Static sample slot 1..128) and `nx.pl p v` (parameter `p` in 0..29, clamped by the part's descriptor like `pl.set`), and schedule track playback with `play t` (which consumes and resets the `nx.*` locks). In v1, the first trigger right after PLAY may be delayed by up to one timer period (~10 ms); all subsequent triggers fall on the step grid. While a menu script is running or awaiting user input (`ask`, `confirm`), modules are silent; their deadlines are skipped and counted, and they resume on the grid afterwards.

## 22. Record a loop on the trigs

```otype
def rec_bar
  menu "REC" >> "LOOP 4"
  rec.src 1 1 2
  rec.len 1 4
  rt.add 1
```

Places a recorder trig on step 1 of the track in context, listening to input A (INAB = 2 on track 1) for four
steps (RLEN 4): at 120 BPM the recorder writes exactly 22 050 samples and stops. `rt.add s src` with the
mask 1 (AB), 2 (CD), 4 (internal) and 7 for all three, as the stock trig does; `rt.del s` removes the recorder
trig of a step (its sources, one-shot flag and micro-timing) and `rt.clr` removes them all. `rec.src t n v`
and `rec.len t v` are the RECORDING SETUP values of a track, stored in the part like the page's own knobs.

## 23. One take, then the next script arms it again

```otype
def one_take
  ro.add 1
  rec.trig 1 0
  rec.loop 1 1
```

`ro.add s [src]` is a recorder trig with the one-shot state: it records once, and the next time the pattern
reaches the step it stays silent, as a one-shot sample trig does. `rec.trig t v` picks ONE (0), ONE-2 (1) or
HOLD (2) and `rec.loop t v` whether the take loops when played back. To take another one, re-arm with
`os.arm` (all one-shot trigs of the track) — that is what [STOP]+[STOP] does for the whole pattern.
`rec.arm t [src]` starts a recording without waiting for a trig: it is the panel's [TRACK]+[AUDIO REC], QREC
included.

## 24. Mirror an external controller

```otype
def cut
  pl.set 1 10 midi.cc 1 74
def n2trk
  in note 1:
    play sub midi.note 1 59
```

`midi.cc ch n`, `midi.note ch` and `midi.vel ch` read what the last MIDI message received on a channel left
behind, so a knob on another box can drive a p-lock. `send.nrpn ch p v` is the output side: one NRPN as its
four Control Changes, all four or none if the transmit buffer is short.

## 25. Copy a pattern from a script

```otype
def keep_it
  pat.copy 1 2
```

`pat.copy a b` is [FUNCTION]+[STOP] without the clipboard: pattern a lands on pattern b of the current bank,
trigs, locks, length and the part assigned together, in the bank and in the pattern's SRAM copy. The stock's
undo snapshot is taken first, so `UNDO PATTERN` can bring the old b back; a copy waiting in the pattern
clipboard is left alone. It is a menu script: a live script is refused (`op pat.copy not allowed in a live
script`), and the run stops if `a` is `b`.

## Limits worth knowing

- 4096 operations per press (the checker prints each function's worst case); 4096 file names per list.
- Up to 8 parameters per function in a menu, 6 on a page.
- A function that fails to load is refused alone: the rest of the file loads, and `/SCRIPTS/ERRORS.TXT`
  says why, with the line.

---

## Integrating the engine (for firmware authors)

OType is shared with other custom firmware authors as sources: build it, link it where your firmware wants it,
make the stock OS enter it at one place, and give it your own interface. The full contract is
`firmware/README.md`; these are the steps in practice.

### I1. Build and link at your addresses

```sh
cd firmware
python3 build/layout.py                                        # the block you must reserve: 2,187,688 B, align 16
python3 build/build.py --base CODE_ADDRESS --region REGION_ADDRESS  # replace with your addresses
```

`build/out/otype.bin` is the code (not position-independent: link again for another address).
Its exact size and SHA256 are recorded in `build/out/otype.json` after each link.
`build/out/otype.json` lists what to write into the OS.

### I2. Write the entry point

From your unpacked system image, check the site and write the bytes
`otype.json` gives. The bytes are the module's own `jmp`, not Elektron's:

```python
import json
d = json.load(open("build/out/otype.json"))
img = bytearray(open("system-image.bin", "rb").read())
for e in d["entry_points"]:
    off = int(e["site"], 16) - image_base
    w = bytes.fromhex(e["write"])
    # check first: the site must hold e["displaced"] (disassemble it), or the image is not the expected one
    img[off:off + len(w)] = w
open("system-image-otype.bin", "wb").write(img)
```

Before patching, disassemble the site with the load address and target CPU for your OS build. The instructions must
match `displaced` in `otype.json`.

### I3. Put the code in memory

Your boot loader copies `otype.bin` to the `--base` address after stock startup initialisation and before your interface
first calls it, then pushes and invalidates the caches. Reserve this code area against stock writes throughout
startup, project loading and use. Appending the binary to an image alone is insufficient. The module sets itself up
on first use.

### I4. Reserve the block

The block is the module's alone and may start uninitialised. The usual place is the top of the sample memory,
made smaller by the same amount. To check that nothing of the stock writes there, fill the block with a pattern
in an emulator, boot, load a project, play and record, and compare.

### I5. Check it on the unit

1. The unit behaves as stock until your interface calls the engine (menus, key combinations, projects).
2. Put recipes 1 and 9 in `/SCRIPTS/` on the card. LOAD through your interface: `ot_nloaded` counts the
   functions, and `/SCRIPTS/ERRORS.TXT` appears.
3. Add a broken function to the file and LOAD again: only it is refused, `ERRORS.TXT` names its line.
4. Run FOUR FLOOR, then power-cycle without saving: the trigs are still there, like a stock edit.

### I6. Your own interface

The engine ships no interface: a key, a menu, a page or a trig of your firmware calls it. List the scripts, ask
what they need, run them (assembly, UI task):

```asm
        clr.l   -(%sp)                      | cdecl, one argument (unused here)
        jsr     otype_ot_load:l             | once: read /SCRIPTS, compile, load (writes ERRORS.TXT)
        addq.l  #4,%sp
        jsr     otype_prepare:l             | index the staged table before C metadata/parameter calls
        ...                                | require d0 = 0; otherwise handle the load refusal
        ...
        move.l  #3,otr_mail+4:l             | function index in the loaded table
        move.l  #1234,otr_mail+8:l          | seed (low 16 bits)
        jsr     otype_run:l                 | runs on the current pattern and commits; d0 = 0: ok
```

After `otype_prepare` succeeds, the parameters, the `confirm` text and the menu paths of each function are read from the loaded table with
`otc_vm_fn_param`, `otc_vm_fn_confirm` and `ot_ctx` (`firmware/README.md`, "Your own interface").

To build a menu, read each function's category and name from `ot_ctx`, group by category and sort as you like;
`input sample` is a flag of the function's entry (`docs/syntax.md`), so a recorder view can list only the
functions that act on an audio take. Ask the
parameters and the `confirm` YES/NO before the run, then show `DONE` or the run's `msg`.
