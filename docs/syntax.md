# OType — syntax, version 1 (2026-10-11)

**Status: OType v1 is implemented** (checker, simulator, Python VM, C virtual machine and compiler, `/SCRIPTS` loader).
The registry's evidence level (`level`, §8) says which operations are confirmed on the unit and which still wait
for a hardware check (NOT TESTED on an MKI).
This document is the grammar and the design; the complete, current list of operations is the command reference
(`docs/cheatsheet.html`, generated from `modules/otype/ops.py`).
This document fixes the **language**: what can be written, how it is read, what is an
error. It is the base of the future manual and the single reference for
`tools/otype.py` and for the firmware's parser: **both must accept and
reject exactly the same texts.**

Design choices: indentation = scope; `|` between a
function's name and its arguments; dot = the OT's API, underscore = the
user's names; predicates `has.*` / `is.*`; function name separate from its
menu label; `>>` alone in menu paths; ternary `?`; scripts tested on the
computer before the card; documentation in English; the language stays
case-insensitive but its canonical written form is lowercase (§2.1).

---

## 1. A complete, annotated example

```
otype 1                                   # language version (optional)

const hatdir "AUDIO/DRUMS/HIHATS"         # file-level constant

def boombap                               # a function with no argument
  menu "DRUMS" >> "BOOM BAP"              # its place in the menu
  param dens 40 0 100                     # a setting editable on the unit
  kick  | 1 "AUDIO/DRUMS/KICKS"           # calling your own function: name | arguments
  hats  | 3 hatdir dens

def hats | track folder dens              # a brick: no menu, 3 named arguments
  trk track:                              # block: everything indented below
    tr.clr
    l 1 16: if mod i 2: tr.add i          # one-line form, chained modifiers
    prob dens: tr.add 12
    steps has.trig:
      lock_samples | folder 4
      each:
        pl.set i amp.vol rrand 70 127
```

(`kick`, `lock_samples`: `modules/otype/examples/`.)

## 2. Lexicon

### 2.1 Files and lines

- Files `/SCRIPTS/*.OTY`, **all loaded together**: a function in one file
  may call a function in another (library files).
- Printable ASCII. A byte ≥ 0x80 is allowed only inside a comment.
- Line ends LF, CRLF or CR. A line is ≤ 128 characters, comment included.
- **Case.** The language is case-insensitive: `tr.add`, `TR.ADD`, `Tr.Add`
  are the same. The canonical written form, used by this documentation, the
  messages and the editor, is lowercase. Strings
  keep their case.
- `#` starts a comment up to the end of the line (outside a string).
- A blank or comment-only line is ignored, whatever its indentation.

**Folder and file names on the card.** A path in
a string is passed to the card as written. The OT's FAT layer is expected to
match names case-insensitively (HYPOTHESIS, to verify on the unit). File
lists are sorted case-insensitively (casefold, then by raw bytes to break
ties) before any random draw, so `seed` gives the same file whatever the
casing on the card. `match` patterns are case-insensitive.

### 2.2 Indentation

- **Spaces only.** A tab in the indentation is an error (allowed in a
  comment).
- `def`, `const`, `otype` start in column 1. A function's body is indented,
  by any width, but **every line of one block has the same indentation**.
- A line ending with `:` opens a block: the following, more indented lines.
  The block ends at the first less indented line, which must fall exactly
  on the indentation of an enclosing block.
- An opened block cannot be empty.

### 2.3 Tokens

| token | form | examples |
|---|---|---|
| number | signed decimal, or `0x` hexadecimal | `12` `-3` `0x7f` |
| string | between `"`, no escapes, ≤ 127 characters | `"AUDIO/DRUMS/KICKS"` |
| list | `{` numbers `}`, ≤ 16 elements, constant | `{0 3 5 7 10}` |
| condition | `nn%` (the OT's 21 probabilities), `a:b` (a ≤ b ≤ 8, **not** `1:1`: see below), `fill` `!fill` `pre` `!pre` `nei` `!nei` `1st` `!1st` | `50%` `1:4` `!fill` |
| API name | letters/digits with at least one dot, or a reserved word | `tr.add` `has.trig` `rrand` |
| user name | a letter, then letters, digits, `_`; ≤ 16; **no dot** | `hats` `lock_samples` |
| variable | one reserved letter | `a b c d x y z t i j` |
| separators | `:` (modifier) · `\|` (arguments) · `>>` (menu) | |

- `-3` glued = a negative number; `- 3 1` (spaced) = a subtraction.
- `:` glued between two digits (`1:4`) makes a **condition**; anywhere else
  it closes a modifier.
- The probabilities are 1 2 4 6 9 13 19 25 33 41 50 59 67 75 81 87 91 94 96
  98 99 %; any other `nn%` is an error.
- `1:1` is **not** a condition: the OT has no "always" condition (the ratios
  are 1:2 … 8:8). It is a load error, `1:1 is not a trig condition (the
  octatrack has no always condition: use cond.set s off for none)`.
- A condition is not a number: `prob 50%:` is an error ("50% is a trig
  condition; write prob 50:").

### 2.4 Reserved words

Language: `otype def const menu page on param confirm in var if elif else l each prob
every skip seed del break stop ret off all cur deep flip o drunk`, the variables, the
operators of §5, plus every API name (registry, §8). A user name that
reuses one is an error.

## 3. File structure

```
file        := [ "otype" number ] { const | def }
const       := "const" name constant_value
def         := "def" name [ "|" name { name } ] nl body
body        := { declaration } statement { statement }
```

- `otype n`: major language version. Absent = 1. Unknown version: the whole
  file is refused, other files load.
- `const name value`: a number, string, list or condition known at load
  time, **global to all files**. It cannot depend on a variable. A duplicate
  name (even in two files) is an error on both.
- `def name`: a function, up to the next `def`/`const` in column 1. A
  duplicate name refuses both definitions; the message cites both files.
- **Arguments**: 0 to 4, after `|`, user names, read-only. Their kind
  (number, string, list, condition) is inferred from their use and checked
  at every call.

## 4. Declarations (at the top of the body, before the first statement)

| declaration | meaning |
|---|---|
| `menu "LEVEL" >> … >> "ACTION"` | place in the menu: at least one level + the action. Several `menu` allowed. **Without `menu` the function does not appear**: it is a brick. Widths checked (today 6 / 10 characters). More than one level: warning, as menus have no submenus yet. |
| `page "LABEL"` | lists the function on a **page of encoders**, when the firmware has one. LABEL = 1..8 printable ASCII characters (0x20..0x7e), kept as written (the page shows it in capitals); empty or longer = error. **At most one** per function (`duplicate page declaration`). A page function **takes no argument** (`a page function takes no arguments`: the page calls it with none), has **at most 6 `param`s** — they are the encoders A..F in declaration order (`page function has more than 6 params`) — and **may not use the card, the sample slots or the pool**, directly or through what it calls: the namespaces FILE, SLOT and POOL (`dir ext match file.* slot.* slots.* pool.*`) are refused with `op NAME not allowed in a page function`, NAME being the **first** such op: the function's own ops in source order (lowest line; the first in the line on a tie), else the first callee (in order of first call) that has one, the error then standing on that call's line; callers of the refused function get `needs …`. (The pattern reads `has.trig pat.len pl.get trk.cur …` are allowed: the page works on a snapshot of the pattern.) A function may have both `menu` and `page`. `confirm` is accepted and ignored by the page. `page` is a reserved word. |
| `var name init` | declares a persistent variable (0..15 per function), initialized to `init` (constant integer) on load. Accessible across invocations of the live script or function. Can also be declared at the top of an `in` block. |
| `in clock N:` | declares a periodic block executed every N clock ticks (N ≥ 1). Organizes a live script without `module` or isolated `clock`. |
| `launch N` | **live script only** (`launch is only allowed in a live script`): the launch quantum of the first wake, in clock ticks, independent of `in clock` (96 ticks = a quarter note, so `launch 384` waits for the next bar in 4/4). The first wake of the script falls on the next multiple of N **from the grid origin**: the transport's start while playing, else the tick the first live script of the run was started at (that origin is forgotten when every live script is stopped, and at PLAY). A script that fixes the grid itself and declares `launch` starts on the grid's own next multiple (`ceil(now / N) * N`), so its first wake waits for the next bar. At most one per function. |
| `in note [ch]:` | declares a block executed on MIDI note-on events, optionally filtered by MIDI channel (1..16). Within this block, `note.num`, `note.vel`, and `note.ch` are available. |
| `in cc ch n:` | declares a block executed on MIDI CC events matching channel `ch` (1..16) and CC number `n` (0..127). Within this block, `cc.num`, `cc.val`, and `cc.ch` are available. |
| `in trig t:` | declares a block executed on the trigs of audio track `t` (1..8) that really play: trig placed, condition met, track not muted. Within this block, `trig.step` (1..64) and `trig.hit` (always 1) are available. The wake is the step's tick, micro-timing excluded. |
| `in trig t all:` | same, but wakes on **every trig placed** on track `t` as the sequencer passes over it, even when its condition fails or the track is muted (`trig.hit` = 1 played, 0 not played). |
| `in gate NAME n:` | wakes when the running live script named `NAME` (1..8 characters) emits `gate n` (n = 1..4), **at the same tick and in the same pass**: its `play` is dated like the master's. Wakes once per tick and per script (a feedback link is deferred by one tick); a cascade runs at most four scripts deep. |
| `on host [args]` | another trigger (`on trig`, `on loop 4`, `on load`). In v1: accepted with a **warning** "host not available yet", no effect. Unknown host: warning too. |
| `param name default min max [source]` | a setting editable on the unit or bound to a MIDI source (e.g. `cc ch n`, `note ch`, `vel ch`), read like a read-only variable. Constants, min ≤ default ≤ max, ≤ 8 per function. |
| `param name bool d` · `param name option d "A" "B" …` · `param name folder "PATH"` | **typed parameters**. `bool`: default `0`/`1` (or `yes`/`no`), shown YES/NO, read as 0 or 1. `option`: 2..16 labels of ≤ 12 characters, `d` the index of the default (0-based), shown as its label, read as the index. `folder`: a path of ≤ 64 characters under the set (`"AUDIO"`), picked on the unit with a folder browser; it is not a number: only `dir name [deep]` reads it (`folder param NAME cannot be used in arithmetic`). At most 4 `folder` per function; a page function may not declare one. A function whose params are all plain numbers compiles to the same bytes as before (§14.1). |
| `confirm ["TEXT"]` | ask YES/NO before running. Implied when the function (or a brick it calls) contains a destructive operation. TEXT (the first non-empty one; ≤ 116 characters on a 128-character line) is the question the confirmation box shows (kept in the table, §14.1); without it the box shows `NAME?`. |
| `input sample` | declares that the function acts on the host audio sample context. **Mandatory** for any function using `src.save` (or calling a function that uses it). Bit 0x10 in entry flags (§14.1). Filtered by the host menu. |

## 5. Expressions

**Prefix notation, fixed arity**: each operation knows how many values it
takes; it takes the following ones, which may be expressions themselves. No
parentheses, no precedence.

```
add 1 mul 2 3           = 1 + (2 × 3) = 7
lim add x 10 0 127      = x + 10 clamped to 0..127
```

### 5.1 Values

- **Signed 16-bit integers**, −32768..32767, **saturating** (no wrap).
- True = any value ≠ 0; comparisons return 1 or 0.
- Strings, lists, conditions: **constants** in v1 (a literal, a `const`, or
  an argument receiving a constant). Computed strings (`fmt`) come later without changing the above.

### 5.2 Language operators

| | arity | note |
|---|---|---|
| `add sub mul div mod` (aliases `+ - * / %`) | 2 | `div x 0` = 0; `mod x 0` = 0; `mod -1 4` = 3 (remainder always ≥ 0) |
| `min max` | 2 | |
| `abs neg not` | 1 | |
| `lim x lo hi` · `wrap x lo hi` | 3 | clamp / wrap into lo..hi |
| `eq ne lt gt le ge` (aliases `== != < > <= >=`) | 2 | |
| `and or xor` | 2 | logical; **both sides are always evaluated** |
| `ez nz` | 1 | = 0? ≠ 0? |
| `? c a b` | 3 | ternary: a if c is true, else b. **Only the chosen branch is evaluated** (otherwise `? x file.pick 0` would use up a file even when x = 0); cost = the dearer branch |
| `rnd n` | 1 | 0..n−1 (n ≤ 0 → 0) |
| `rrand a b` | 2 | a..b inclusive, in either order |
| `toss` | 0 | 0 or 1 |
| `chance p` | 1 | 1 with p % probability |
| `pick list` · `at list i` · `len list` | 1 · 2 · 1 | random element · i-th (1..n, clamped) · size |
| `on` `off` | 0 | 1 and 0 |
| `er k n s` · `er.r k n r s` | 3 · 4 | **rhythm**: 1 if step s plays in a Euclidean rhythm of k hits over n steps (rotated by r), else 0; s wraps modulo n (the unit's euclidean model) |
| `dr.v c s` | 2 | **rhythm**: velocity 0..127 of curve c at step s — our own tables, not Teletype's: 16 values per curve, step s reads entry (s − 1) mod 16 (repeats every 16 steps, s ≤ 0 wraps the same way); c = 0 flat 100 · 1 accents on the beats (127 / 80) · 2 accents off the beats (110 on 3 7 11 15 / 60) · 3 ramp up 16..121 · 4 ramp down 127..22 · 5 eighths 120 / sixteenths 70 · 6 tresillo 3+3+2 (127 on 1 4 7 9 12 15 / 64) · 7 swell 32 → 127 (step 9) → 44; another c reads curve 0 (no out-of-range report). Data: `sim.DRV_TABLE` = `vm.c` `DRV_TAB` |

#### Operator aliases (Word ↔ Symbol)

| Word | Symbol | Arity | Description |
|---|---|---|---|
| `add` | `+` | 2 | addition |
| `sub` | `-` | 2 | subtraction |
| `mul` | `*` | 2 | multiplication |
| `div` | `/` | 2 | division (0 if y = 0) |
| `mod` | `%` | 2 | remainder (always ≥ 0, 0 if y = 0) |
| `eq` | `==` | 2 | 1 if x == y, else 0 |
| `ne` | `!=` | 2 | 1 if x != y, else 0 |
| `lt` | `<` | 2 | 1 if x < y, else 0 |
| `gt` | `>` | 2 | 1 if x > y, else 0 |
| `le` | `<=` | 2 | 1 if x <= y, else 0 |
| `ge` | `>=` | 2 | 1 if x >= y, else 0 |

### 5.2.1 State operators

Each **function has its own** state for these (two functions never disturb
each other), kept between presses, reset at boot — like `every` counters.
The same name **reads** in an expression and **sets** when it starts a
statement followed by a value (Teletype's getter/setter, our variables' rule).

| read | set | meaning |
|---|---|---|
| `flip` | `flip x` | alternates 0, 1, 0… on each read |
| `o` | `o x` · `o.min x` · `o.max x` · `o.wrap x` · `o.inc x` | counter that moves by `o.inc` (default 1) after each read, between `o.min` (0) and `o.max` (63); wraps if `o.wrap` (default 1), else stops |
| `drunk` | `drunk x` · `drunk.min x` · `drunk.max x` · `drunk.wrap x` | random walk: −1, 0 or +1 on each read, within `drunk.min` (0)..`drunk.max` (127) |

```
def ab_hats
  menu "DRUMS" >> "AB HATS"
  seed ? flip 7 0                 # one press variation 7, the next the reference
  hats | 3 "AUDIO/DRUMS/HIHATS" 40
```

### 5.3 Evaluation order (it matters for `seed`)

Left to right, always every argument — the one exception is `?`, which
evaluates only the chosen branch. The sequence of random draws therefore
depends only on the text, the data and the seed, and the computer tool
reproduces the OT exactly for a given seed.

### 5.3.1 Separate random streams

Randomness comes from **one stream per family**, as Teletype does, so that
adding a `prob` line does not shift every `rrand` that follows:

| stream | used by | own seed |
|---|---|---|
| rand | `rnd rrand pick` | `rand.seed n` |
| toss | `toss chance` | `toss.seed n` |
| prob | `prob` modifier | `prob.seed n` |
| drunk | `drunk` | `drunk.seed n` |
| file | `file.rnd file.pick slots.rnd` | `file.seed n` |

`seed n` seeds all five (each stream derives its own seed from n and its
number); a per-stream seed changes only its stream. Without any `seed`, each
press seeds the streams from the system ticks. Inserting a draw of the
**same** family still shifts the draws after it in that stream.


### 5.3.2 Random generator (reference)

The sequence of random numbers is part of the language: the firmware must
draw exactly the same numbers as the computer for the same seed. The
reference implementation is `modules/otype/sim.py`; its tests
(`tools/test_otype_run.py`) hold the vectors below.

**Generator.** xorshift32 with the shifts (13, 17, 5), in 32-bit unsigned
arithmetic. A draw replaces the state `x` by `x ^= x << 13; x ^= x >> 17;
x ^= x << 5` and its value is the **new state**.

**Streams** (§5.3.1), numbered: `rand` 1, `toss` 2, `prob` 3, `drunk` 4,
`file` 5. `seed n` seeds the five; `rand.seed n` … `file.seed n` one only.
The start state of stream *k* seeded with *n* is (all products and shifts
modulo 2^32; only the low 16 bits of *n* count, so `seed 65537` = `seed 1`):

```
x = (n & 0xFFFF) | (k << 16)
x = x * 0x9E3779B1            x ^= x >> 16
x = x * 0x85EBCA6B            x ^= x >> 13
x = x * 0xC2B2AE35            x ^= x >> 16
start state = x               (0x6D2B79F5 if x = 0)
```

**One evaluated random operation = exactly one draw** from its stream, even
if its argument makes the result trivial (`rnd 0`, `pick` of a one-element
list, `chance 0`, `file.rnd` on an empty list): the number of draws depends
on the text and the data only. An operation skipped for an out-of-range
argument (§8.1) has already evaluated its arguments, so their draws happened.
Two derived values:

```
below(n) = ((draw >> 16) * n) >> 16        0..n-1 for n >= 1; 0 for n <= 0 (still one draw)
bit      = draw >> 31
```

| operation | stream | value |
|---|---|---|
| `rnd n` | rand | `below(n)` |
| `rrand a b` | rand | `lo + below(hi - lo + 1)`, `lo = min(a,b)`, `hi = max(a,b)` |
| `pick list` | rand | `list[below(len)]` (0-based position) |
| `toss` | toss | `bit` |
| `chance p` | toss | 1 if `below(100) < p` (p clamped 0..100) |
| `prob p:` | prob | the line runs if `below(100) < p` (p clamped 0..100); one draw per time the modifier is evaluated, whatever p is |
| `drunk` (read) | drunk | returns the current value, then adds `below(3) - 1` and brings the result into `drunk.min..drunk.max` (wrap or stop) |
| `file.rnd` | file | `below(n) + 1` for n files (0 if none) |
| `file.pick` | file | the list's *bag* holds the numbers not yet picked (1..n at the start of a round, in order): the value is `bag[below(len(bag))]`, which leaves the bag; an empty bag is refilled with 1..n. One bag per (function, list: folder, filters, size), at most 8 bags in all (§9.2), kept between presses. 0 if no file |
| `slots.rnd` | file | `set[below(len(set))]` (0 if the set is empty) |

Operations that act on the context draw **track by track, in `trk` order
(§6.2: a multi-track `trk` runs its whole body once per track), then step by
step in ascending order within the track, one value each** (`pl.vary`: only
on the steps that hold the lock):

| operation | stream | draws |
|---|---|---|
| `pl.rnd p` | rand | `below(128)` per step |
| `pl.vary p x` | rand | `below(2d + 1) - d` added to the lock (then 0..127), `d = (127x + 50) // 100` |
| `smp.rnd n m` · `cond.rnd c d` | rand | `lo + below(hi - lo + 1)` per step |
| `micro.rnd x` | rand | `below(2x + 1) - x` per step (x clamped 0..23) |
| `tl.fill p` | rand | one `below(100)` per step; a free step gets the lock if it is `< p` |

**Skipped steps still draw**: a per-step
operation that writes a lock or a trig word (`pl.rnd`, `smp.rnd`, `cond.rnd`,
`micro.rnd`) draws its number **on every step of the context, then checks the
step**: on a step without a trig the number is thrown away and the write is
skipped and counted. The number of draws, and so the position of every later
random operation, does not depend on which steps hold a trig. The firmware loop
is `draw; if the step has a trig: write; else count`. `pl.vary` keeps its own
rule (a draw only on the steps that hold the lock, §5.3.2 table), then the same
check. `tl.fill` is not concerned (it only writes trigless locks).

The value ranges 0..127 and the effect count 14 are provisional until the
firmware cores exist.
**Parameter ranges:** for the p-lock operations the range 0..127 is only the *neutral*
range: `pl.set` `pl.rnd` `pl.vary` use the range of the part's parameter descriptor (read on the unit through
`otb_host_param_info`, `lo` 0..254, at most 254), a slot the part disables is refused and counted as out of
range (no draw), and `pl.vary`'s `d` is `((hi - lo) * x + 50) // 100`.

**Scope of a seed.** The five streams belong to the **context** (§6.2).
A block, a modifier line and a called function receive their parent's
streams **by reference**: what they draw moves the parent's streams. `seed n`
(or `x.seed n`) *replaces* the streams (or one of them) **for the rest of
the block it is in**; when that block ends, the outer streams resume exactly
where they stopped. The root block starts with the seed of the press (the
`seed` of `run`, else the system ticks). Streams are not kept between presses:
every press seeds them again.

**Test vectors**: the first five draws of each stream (`python3 -c "from
modules.otype import sim; print(sim.vectors_text())"` prints them):

```
seed 1
  rand  start 0x35e49459  1251154478 319755877 3384417796 3307322393 2801683528
  toss  start 0x885f19eb  120309530 2262804286 892574306 2306921213 3896006994
  prob  start 0x6bc9295c  2461223821 2207276012 2505993434 1080367032 2967386321
  drunk start 0xb33c9ee8  1020288408 3674964745 3325305148 6522517 1720895341
  file  start 0x61d9e918  3282185157 1568923048 3773765843 2103073801 3988229092
seed 0
  rand  start 0x237db5e6  770105286 903020009 663941918 1204876070 1865932924
  toss  start 0xb8a39595  2285585213 3956207987 2314337251 1731580933 1796620607
  prob  start 0x20e98d50  959658961 2540677717 646075303 4062592604 2287546858
  drunk start 0xbe7afdde  3655920382 1381915653 2718477738 1074493684 3450339518
  file  start 0x2dacc436  274827971 101102015 3591769415 3764532632 2097002736
```

Check: seed 1, `rnd 100` as the first draw of `rand` gives
`below(1251154478, 100) = 29`.

### 5.4 User functions inside an expression

A function that ends with `ret value` can be used as a value:
`a pick_slot | 3`. Reaching its end without `ret`, it is worth 0. Used as a
statement, its value is ignored.

## 6. Statements

One line = one statement, possibly preceded by modifiers.

| statement | example |
|---|---|
| assignment: variable + value | `a 5` · `x add x 1` |
| API operation | `tr.add 5` · `static.load a file.rnd` |
| call of your own function | `hats \| 3 hatdir 4` · `reset_all` |
| context | `trk 3 4` · `steps has.trig` · `step 5` · `seed 7` |
| exit | `break` (leaves the loop) · `stop` (leaves the function) · `ret v` |

- **`|` is mandatory** as soon as your own function takes arguments, and
  forbidden after an API operation: you see at a glance what is yours.
- A bare variable, or a value with no destination (`add 1 2`): error "does
  nothing".
- An API operation that returns a value, used as a statement: allowed, the
  value is ignored.

### 6.4 `msg`: the text of the end box

`msg item…` takes 1 to 8 items, each a string constant or an integer expression, and joins them with one space:
`msg "BANK" bank.cur "PAT" pat.cur` gives `BANK 2 PAT 7`. A number is written in decimal (signed 16-bit, `-32768`
at the longest). The text is stored in the run (one fixed buffer, no heap); each `msg` replaces the previous one, so
**the last `msg` executed** is the text the end box shows **instead of `DONE`**, in the same box for the same 8 ticks.
A run without `msg` ends with `DONE` as before; a failing run keeps `SCRIPT ERROR n`. `otype.py run` prints each `msg:`
line and, last, `end box: "…"`.

- **Length.** The checker refuses a `msg` whose worst case exceeds `MAX_MSG` characters (`modules/otype/ops.py`: today
  **29**, the width of the one-line box: 4 px per glyph in the 115 px free for box and shadow), counting a number as 6
  characters and one space between items. Error: `msg is too long for the screen: 33 characters in the worst case (a number
  counts as 6, items are separated by a space), at most 29 fit`; more than 8 items: `msg takes at most 8 items (got 9)`.
  The VM never writes beyond `MAX_MSG`; a wide glyph (`@`, `#`, `$`) that would overflow the box is cut by the interface.
  `MAX_MSG` is the ONE number to change when the end box grows (the checker, the C compiler, the C VM and the tests read
  it: `gen_ops_c.py` regenerates `OTC_L_MAX_MSG` / `OTC_MSG_MAX`).
- **An argument of your own function** used in `msg` is a number, unless it is also used as a string (`dir`, `match`).
- **What to show after `pat.go`.** See cookbook recipe 17: on the unit a pattern change while playing is queued, so
  `pat.cur` / `bank.cur` keep answering the pattern now playing until the switch; the simulator answers the target at once.
  Show the variable you passed to `pat.go`.

### 6.4 bis Script gates and outputs (`gate`, `out`, `cv`)

A live script can be wired to another by name, like a small modular patch:

```
def acid
  in clock 24:
    prob 50: gate 1
    out 1 rnd 128

def follow
  in gate acid 1:
    x cv acid 1
    play 2
```

- `gate n` (n = 1..4): emits a gate on output `n`, dated at the **current block's tick**. The live scripts that declare
  `in gate NAME n:` (NAME = this function's name) run just after, in the same pass, with the same tick.
- `out n v` (n = 1..4): publishes the integer `v` on output `n`, held until it changes (0 at launch). Every running script
  has four outputs.
- `cv NAME n`: reads output `n` of the running script named `NAME`; **0 if that script is not running** or `n` is out of
  range. The name is written bare (`cv acid 1`, not `cv "acid" 1`), resolved **by name**, not by launch order; a name
  longer than 8 characters or that is not a valid function name is an error ("cv: …").
- The cascade order is the patch's: a master's followers run in the same pass, in slot order; a script runs at most once
  per tick in a cascade, so a feedback link (A follows B, B follows A) is broken by **one tick of delay** on the return
  link; the depth is capped at **4 scripts** (the fifth wake is counted as skipped). A gate ring that is full drops the
  event and counts it; nothing blocks.
- `gate`, `out` and `cv` are only allowed in a live script (`op NAME only allowed in a live script`), and are reserved names.
- The same values are readable from another runtime through the public `otype_read_out(name8, out)`, which `cv` uses.

### 6.5 Direct MIDI output (`send.note`, `send.cc`, `send.prog`, `send.nrpn`) and MIDI input (`midi.*`)

- `send.note ch n vel dur`: sends a MIDI Note-On on channel `ch` (1..16), note `n` (0..127), velocity `vel` (0..127), and automatically schedules a Note-Off after `dur` ticks (dur ≥ 1 in units of `in clock`, 96 per quarter note). If a note is already playing on the same channel and note number, its Note-Off is sent immediately before retriggering.
- `send.cc ch n v`: sends a MIDI Control Change on channel `ch` (1..16), controller `n` (0..127), value `v` (0..127).
- `send.prog ch p`: sends a MIDI Program Change on channel `ch` (1..16), program `p` (0..127).
- `send.nrpn ch p v`: sends an NRPN as the four Control Changes 99, 98, 6, 38 on channel `ch` (1..16): parameter `p` (0..16383) and value `v` (0..16383), MSB first. **All four go out or none does**: the free space of the transmit buffer is tested for the twelve bytes before the first one is written, so a script never sends half an NRPN.

Permitted in menu scripts and live scripts. All messages test for free transmit buffer space before sending to avoid blocking.

**The MIDI input tables** — what the last message received on each channel left behind (since power on, 0 before):
- `midi.cc ch n`: the last value (0..127) of controller `n` (0..127) received on channel `ch` (1..16);
- `midi.note ch`: the last note number (0..127) received on channel `ch`;
- `midi.vel ch`: the velocity of that last note.

A script can therefore mirror an external controller: `midi.cc 1 74`, or play the track the incoming note names, `play midi.note 1 - 59`.

### 6.6 Recorder trigs and track recorder settings (`rt.*`, `rec.*`)

- **Recorder trigs** (on the context tracks, like `tr.*`; a recorder trig and an audio trig may share a step):
  - `rt.add s [src]`: add a recorder trig on step `s` (1..64) with source mask `src` (1 AB, 2 CD, 4 internal, additive; default = 7 for all three, the stock default). `src` must be 1..7: 0 or 8 is counted out of range and nothing is written.
  - `rt.del s`: remove the recorder trig on step `s`: its three source masks, its one-shot flag and its micro-timing. The audio trig of the step, its word and its locks are not touched.
  - `rt.clr`: remove every recorder trig of the context steps (as `rt.del`).
  - `rt.src s v`: set the source mask of step `s` to `v` (1..7).
- **Track recorder settings** (RECORDING SETUP, manual §9.1.2, stored in the part):
  - `rec.src t n v`: set recorder input source `n` (1 INAB, 2 INCD, 3 SRC3) to stock value `v` on track `t` (1..8). For INAB/INCD: 0 (`-`), 1 (`A B` / `C D`), 2 (`A` / `C`), 3 (`B` / `D`), 4 (`A+B` / `C+D`), values above 4 clamp to `A+B`/`C+D`. For SRC3: 0 (`-`), 1..8 (`T1`..`T8`), 9 (`MAIN`), 10 (`CUE`), values above 10 clamp to `CUE`.
  - `rec.len t v`: set recorder length RLEN to `v` steps on track `t` (1..8), or to MAX with `v` = 64. It is the length the recorder writes on its next arm: `rec.len 1 4` records 4 steps. (The part stores it 0-based, 0..63 = 1..64 steps and 64 = MAX.)

- **One shot recorder trigs** (manual 12.4.5): `ro.add s [src]` adds a recorder trig that records **once** (the
  sources and the one-shot state at `TRAC +0x38`), `ro.del s` removes a recorder trig of either kind (as `rt.del`).
  The one-shot state is per track: once one of its one-shot trigs has fired, no other one-shot trig of that track
  fires — sample or recorder — until they are re-armed by `os.arm`, [STOP]+[STOP] or a project change.
- **`rec.trig t v`**: TRIG mode of the track recorder: 0 ONE, 1 ONE-2, 2 HOLD.
- **`rec.loop t v`**: LOOP, 0 off / 1 on: whether the captured sample loops when played back.
- **`rec.arm t [src]`**: starts a recording on track `t` right now, from the sources `src` (1 AB, 2 CD, 4 SRC3,
  1..7, default 7), exactly as the panel's [TRACK]+[AUDIO REC] does — QREC (RECORDING SETUP 2) still delays the start
  to its quantisation point when it is not OFF.

`rt.micro` is not part of v1.

### 6.7 Copying a pattern (`pat.copy a b`)

`pat.copy a b` copies **pattern a** (1..16) onto **pattern b** (1..16) of the **current bank**, through the
same stock writer as the [FUNCTION]+[STOP] paste: the whole pattern record of 0x8ed8 bytes — trigs of the eight
audio and eight MIDI tracks, every lock, the pattern length and the **part assigned** to the pattern — lands in
the bank **and** in the pattern's SRAM copy, the stock's dirty flags are set and the playing pattern's part and
tempo are re-applied as the gesture does. The **pattern clipboard is not touched**: a copy made with
[FUNCTION]+[REC] survives, and neither is any part's data or the sample pool (only the *assignment* byte
travels). The stock's undo snapshot is taken first (`UNDO PATTERN` works afterwards, as after the gesture), and
the operator is **destructive**: the box asks before it runs.

`a = b` is a **refused argument**: nothing is written and the run stops (`pat.copy: source and destination are
the same pattern`). A number outside 1..16 is out of range (`pat.copy` in the run's list, the run goes on).
The source is read in the bank, so the destination never overlaps the source (`a = b` is the only overlap).

**Menu scripts only**: `pat.copy` is refused in a live script (`op pat.copy not allowed in a live script`,
directly or through a call) — a live wake cannot ask the confirmation.

### 6.1 Variables

| | scope |
|---|---|
| `a b c d` | local to one run of the function, 0 at start; every call has its own |
| `x y z t` | global, shared, **kept from one press to the next**, 0 at boot |
| `i j` | counters: `i` = innermost loop (`l`, `each`), `j` = the one above; 0 outside loops; read-only |
| arguments, `param`, `const` | read-only |

### 6.2 Context: `trk`, `steps`, `step`, `seed`

The context (tracks, steps, randomness) says what the operations act on. It is set in two ways:

- **as a statement** (`trk 3 4`): until the end of the current block;
- **as a modifier** (`trk 3 4: …` or `trk 3 4:` + block): for that line or
  that block only.

A block inherits its parent's context and gives it back unchanged. A called
function receives a copy of the caller's context and cannot change it for
the caller.

| | arguments |
|---|---|
| `trk` | 1 to 8 tracks (numbers or expressions), or `cur`, or `all` |
| `steps` | `all` · `a b` (range, **absolute**: it replaces the current steps, it does not intersect them) · a predicate without its step (`has.trig`, `is.empty`) — **only a predicate narrows** the current steps |
| `step s` | one step |
| `seed n` | reproducible randomness from here on; does not leak to the caller |

**Several tracks: one pass per track**. A `trk`
that names several tracks runs what it governs **once per track, in the order
written** (`trk 3 1 2` = 3, then 1, then 2; a number given twice counts once;
`all` = 1..8): the line or block for the modifier form, **the rest of the
block** for the statement form. Each pass has the context of **that one
track**:

- it has **its own step set**: `trk 1 2: steps has.trig: pl.set …` with a trig
  on T1 step 3 and on T2 step 7 locks T1 step 3 and T2 step 7 only. A
  predicate is evaluated on the track of the pass at the moment it runs: write
  `steps has.trig` *before* the `trk` and the one set computed on the track of
  that moment is shared by all the passes;
- `trk.cur` is that track, and so is `trk cur`; value reads (`has.trig s`,
  `pl.get`, `smp.get`) read it. At the root the current track is the host's
  (`run --track`);
- everything that runs in the pass runs once per track, whatever it is: an
  assignment (`a add a 1`), a user call (the callee gets the single-track
  context), a loop, `each` (it iterates that track's own steps), a modifier
  (`prob`, `every`: one draw or one counter step per pass), `seed` (scoped to
  the pass, so each pass restarts from it);
- bulk operations act on the steps of the pass: the random draws therefore
  go **track by track in `trk` order, steps ascending within a track**
  (§5.3.2), and the whole body of one track runs before the next track's;
- `break`, `stop` and `ret` end the loop / function altogether, not just the
  current pass;
- a track number out of range is dropped and counted (§8.1); if no track is
  left, the body does not run at all;
- cost (§9.1): the body is counted once per track named (`all` = 8).

### 6.3 Locks and trig words land on steps that carry a trig

As in the Octatrack manual, locks and conditions apply to "sample trigs, trigless trigs, trigless locks and one shot
trigs". These operations
write only on a step that holds **a trig of any kind** — red, trigless trig,
trigless lock or one-shot:

- locks: `pl.set` `pl.rnd` `pl.vary` `smp.set` `smp.rnd`;
- trig word: `cond.set` `cond.rnd` `micro.set` `micro.rnd` `ratch.set`.
  (`ratch.set s x` sets the OT's TRIG COUNT, x = 1..8 hits, `ratch.get s` reads it; it is not the RTRG/RTIM
  retrig of the playback page.)

On a step without one the operation is **skipped and counted** (a count of
its own, not the out-of-range count of §8.1; the run says so at the end,
§9.2) and nothing is written. To lock empty steps a script makes them trigless
locks first: `tl.add s` or `tl.fill 100`. Removing is not concerned (`pl.del`,
`smp.del`, `pl.clr`, `smp.clr` act on what exists). Random draws on skipped
steps: §5.3.2. `has.trig` and `tr.*` keep their meaning (red, trigless trig,
one-shot; §9.2); the check above is wider because it includes the trigless lock.

Three more rules, as the stock keys behave:
- **Removing a trig** (`tr.del`, `tr.flip` that removes, `tr.clr`) also drops the step's p-locks,
  sample lock and condition, unless a trigless lock keeps the step (then only the trig goes). The
  step's micro timing and retrigger count stay in its trig word; they have no effect without a trig
  and are reset by the next `tr.add`.
- **`tr.add`** on a step already red does nothing; on a one-shot or a trigless trig it makes the step
  a plain red trig and keeps its locks, sample lock and trig word; on a step with no trig it sets
  the trig and resets the trig word (condition, micro, count), keeping the locks.
- **A PICKUP track** (machine type 4) refuses every write (`tr.*`, `tl.*`, `pl.*`, `smp.*`, `cond.*`,
  `micro.*`, `ratch.*`): it is skipped and counted apart (`skipped_pickup` in `--json`, one entry per
  operation and track) and draws no random number. Reads (`has.*`, `is.empty`, `pl.get`, ...) work.

`trk.init` resets the track's **sound** — effects back to FX1 filter / FX2
delay, the factory slot, the parameters, the LFO aims and the scenes — and
leaves its trigs, locks, sample locks, trig words and its machine type alone.

## 7. Modifiers

A modifier is followed by `:`, then either a statement on the same line or
nothing (an indented block follows). They chain on one line; the last one
may open a block.

```
l 1 16: prob 35: tr.add i        # inline
l 1 16: prob 35:                 # a chain that opens a block
  tr.add i
  pl.set i amp.vol 100
```

| modifier | meaning |
|---|---|
| `if c:` | if c is true |
| `elif c:` · `else:` | only on the line right after the `if` or `elif`, **at the same indentation**, and only if that line **starts** with `if`/`elif` |
| `l a b:` | i from a to b inclusive (downwards if a > b). a and b: numbers, `const`, `param`, or arguments receiving a constant at every call — **never a variable** (termination) |
| `each:` | for each step of the context (of the current track's step set, §6.2), in order, i = the step |
| `prob p:` | with p % probability (p an expression, clamped 0..100) |
| `every n:` | once every n passes on this line (a counter of its own, kept between presses, 0 at boot) |
| `skip n:` | every time except once every n (same counter) |
| `trk …:` · `steps …:` · `step s:` · `seed n:` | context of this line / block |
| `del n:` | **reserved** (run n steps later): error "not available yet" in v1 |

Blocks nest ≤ 8 levels.

## 8. The operation registry (declaration format)

Every API operation — the language operators of §5.2 and the context of
§6.2 included — is an **entry of one registry**, `modules/otype/ops.py`,
from which both the firmware's table and `tools/otype.py`'s are produced.
The parser knows **no operation by name**: it reads the registry. Adding an
operation = adding an entry.

```python
op("TR.ADD",                 # name (case-insensitive), unique
   id=(NS_TR, 0x01),         # (namespace, number) — STABLE, never reused
   args=(STEP,),             # argument kinds, in order
   ret=None,                 # None, INT
   cost=4,                   # budget units per run
   family=EDIT,              # LANG | CTX | EDIT | IO_R | IO_W | UI | ASK | PLAY
   destructive=False,
   touches=TRAC,             # what UNDO copies: TRAC | PART | SLOTS | NONE
   hosts=ALL_HOSTS,          # hosts where it is allowed
   core="otc_tr_add",        # firmware core symbol
   since=1,                  # API version it appeared in
   level="to establish")     # evidence level of its core
```

The registry source writes names and kinds in capitals (an internal key:
lookups upper-case what the user wrote); the tool, the messages and the
editor show them in lowercase through one helper (§2.1).

### 8.1 Argument kinds

| kind | accepts | out of range at run time |
|---|---|---|
| `int` | any expression | — |
| `step` | expression, 1..64 | operation skipped, counted |
| `track` | expression, 1..8 | operation skipped, counted |
| `slot` | expression, 1..128 | operation skipped, counted. The Octatrack keeps two separate lists of 128 slots, Static and Flex: `static.*` act on the Static list; `trk.slot` and `smp.*` use the list of the track's machine |
| `param` | expression, 0..29 (`fx1 3`, `amp.vol`) | operation skipped, counted |
| `value` | expression; clamped by the core to the parameter's range | clamped |
| `cond` | a condition constant, or an expression (number 0..64) | operation skipped, counted |
| `pct` | expression, clamped 0..100 | clamped |
| `file` | expression (index in the current `dir` list) | operation skipped, counted |
| `str` · `list` | constant | — (checked at load) |
| `item` | a string constant or any integer expression — only in `rest(item)`, i.e. `msg` | — (checked at load: the worst-case length, §6.4) |
| `pred` | a predicate name without its argument (`steps has.trig`) | — |
| `kw(…)` | a keyword from a list (`deep`, `cur`, `all`) | — |
| `opt_kw(…)` | an optional trailing keyword — **statements only** (`dir "AUDIO" deep`) | — |
| `rest(kind)` | 1..n arguments up to the end of the line or `:` — **statements only** (`trk 3 4`, `ext wav aif`, `match "A*" "B*"`) | — |

A constant out of range (`tr.add 70`) is a load error. A run that skipped
operations says so at the end (`hats: 3 out of range`); it is not a load
error. A lock or trig-word write skipped because the step has no trig (§6.3)
is counted apart (`hats: 5 skipped, no trig`).

### 8.2 Stability rules

- A published `id` is **never** reused or renumbered (a settings-format rule).
- Changing an argument's kind or an operation's meaning = a new operation,
  a new `id`; the old one may stay, marked `deprecated` (warning at load).
- A namespace belongs to a module (`tr`, `pl`… to the OType core;
  other namespaces to optional or host modules). A module
  absent from the image: its operations are unknown, a function using them
  is disabled with `no module <namespace>`.
- A predicate (`has.trig s`) is an operation with `ret=INT`, `args=(STEP,)`,
  `predicate=True`: that is what allows it after `steps`.

## 9. What is checked at load time

| | outcome |
|---|---|
| syntax, indentation, arity, argument kinds | **error**: function refused |
| unknown name, duplicate name, reserved name | error |
| `l` with a variable bound, call cycle (`a > b > a`), depth > 8 | error (`loop a>b>a`) |
| maximum cost > the host's budget | error (`too long: 5200/4096`) |
| operation not allowed by one of the declared hosts | error |
| a menu-only operation (`pat.copy`) in a live script, or in what a live script calls | error (`op pat.copy not allowed in a live script`) |
| `param` in a brick, orphan `else`, empty block | error |
| function calling a refused function | refused too (`needs lock_samples`) |
| `on` not available yet or unknown, `deprecated` operation, submenu | **warning**: the function loads |

An error refuses **only its function** (and those that depend on it); the
rest of the file and the other files load.

**Message format** (computer tool and `/SCRIPTS/ERRORS.TXT`, identical):

```
HATS.OTY:12: hats: error: tr.add takes 1 argument (step), got 0
HATS.OTY:15: hats: error: 50% is a trig condition; write prob 50:
LIB.OTY:3: lock_samples: error: l bound must be constant (n)
```

On the OT's screen: a summary (`2 err, see ERRORS.TXT`).

### 9.1 Precisions fixed by the checker (`tools/otype.py`)

- Summary line: `n functions ok, e refused, w warnings` (+ file-level
  errors for `const`/`otype`). A refused file (`otype 2`) gets one error;
  its callers get `needs x`.
- Call depth: a chain of at most 8 functions including the root. Block
  nesting: blocks opened by `:` count, the body does not.
- Costs: context statements 1 (a `trk` multiplies what it governs by the number of
  tracks it names, `all` = 8, §6.2); assignments and reads 0; an
  `if/elif/else` chain costs `c1 + max(b1, c2 + max(b2, b3))`; bulk ops over
  the context 128; `param` bounds use their min..max worst case; `too long`
  is reported on the `def` line.
- An API value used as a statement (`file.rnd` alone) is a **warning**
  (value ignored); bare variables, constants, arguments, literals and
  language-only expressions are the **error** "does nothing".
- Argument kinds: used as cond and as a number → int; cond only → callers
  must pass a condition constant; str/list mixed with another kind → error.
- `1:1` is a load error (§2.3): the lexer accepts `a:b` with 1 ≤ a ≤ b ≤ 8
  except that pair.
- Condition words (`fill`, `pre`, `nei`, `1st` and `!` forms) are reserved.
  A name shared by a function, `const`, `param` or argument is an error. A
  column-1 line other than `def/const/otype` is an error.
- A misplaced `otype` is an error on its line only. `elif/else` must be the
  first modifier of their line. Numbers are checked against 16 bits when
  read. `on <host>` ignores its arguments in v1.
- `er`, `er.r`, `dr.v` are pure values: alone on a line they "do nothing".
  A state getter alone on a line (`flip`) is a warning (value ignored).
  `name value` at the start of a statement sets a settable state.


### 9.2 Running a function on the computer (`run`)

The simulator runs a function on a pattern and shows what it did, before
anything goes on the card. It walks the tree that the checker (§9) built,
so a function the checker refuses is not run. It is the **reference of the
run-time behaviour** (random draws §5.3.2, evaluation order §5.3, contexts
§6.2, state §5.2.1, out of range §8.1): the firmware must match it.

```
python3 tools/otype.py run FUNC FILE.OTY [FILE2.OTY ...] [options]
```

All files are loaded together, like `check`. `FUNC` is a function **without
arguments** (a menu function, or any brick that takes none). Nothing is ever
written to a bank file or to a card.

| option | meaning |
|---|---|
| `--seed N` | seed as if `seed N` ran first (§5.3.2); without it the clock, printed so the run can be replayed |
| `--bank FILE` `--pattern P` | start from pattern P (1..16, default 1) of a bank file (`bankNN.work`), read only; the pattern length is the bank's (per-track scale: the length of `--track`), else 16 |
| `--track T` | the host's current track (default 1; `trk cur`, `trk.cur`) |
| `--len N` | pattern length 1..64 |
| `--card DIR` | the card's root: `dir "AUDIO/DRUMS"` lists `DIR/AUDIO/DRUMS` (names matched without case, sorted without case, `._*` and dotfiles ignored); without it every list is empty. Give the **Set** folder: on the card `AUDIO/` lives inside the Set (`/Set general/AUDIO`). `dir` applies the device limits below |
| `--slots FILE` | JSON `{"1": "AUDIO/x.wav", ...}`: the slots the unit already holds (default: all free); paths are canonicalised |
| `--refuse PATTERN` | the unit refuses to load a file whose last path component matches PATTERN (`*` joker, ASCII case-insensitive; repeatable) |
| `--state FILE` | JSON of what survives between presses (`x y z t`, `flip`, `o`, `drunk`, `every`/`skip` counters, `file.pick` bags): read if it exists, written after the run. Run the same command again to play the next press |
| `--param NAME=V` | value of a `param` of FUNC (clamped to its min..max, with a warning) |
| `--json` | the whole resulting model and the report as JSON |
| `-v` | all eight tracks, every lock, every step with a condition or micro timing |

Exit status: 0 = ran; 1 = FUNC refused by the checker (its diagnostics are
printed), unknown, takes arguments, or the bank cannot be read; 2 = bad usage.

```
$ python3 tools/otype.py run ab_euclid modules/otype/examples/*.OTY --seed 1 --card CARD
ab_euclid  seed 1 (--seed)  pattern A01 len 16 (default)  track 1
T1  1   5   9   13
    X...X..X..X..X..
    step 1: amp.vol 127   step 11: amp.vol 80   step 14: amp.vol 80
      X trig  ! one-shot  o trigless trig  - trigless lock  l locks only  . none
slots: 1 <- AUDIO/DRUMS/KICKS/k1.wav
part:  t1 slot 1
cost: 368/482   random draws: rand 0  toss 0  prob 1  drunk 0  file 1
ab_euclid: 3 skipped, no trig (pl.set line 13 x3)
```

(`pl.set` on three steps that hold no trig: skipped, §6.3.)

A pattern is drawn 16 steps to a row (four rows for 64 steps, with the step
numbers above); only tracks that hold something or changed are shown. Below
each grid, the locks, condition, micro timing and retrigger of the steps (at
most 3 locks per step); when more than 12 steps have details, only the steps
with a trig are listed and the rest is counted (`-v` lists them all). Below
the tracks: slots loaded, the working set, the part (parameters, LFO targets,
effects, scenes), `msg` texts, the **cost** (spent / the checker's worst
case), the **random draws** per stream, then up to two lines of skipped
operations, each listing the operation, its line and a count per call site:
`ab_euclid: 1 out of range (static.load line 4 in play_one)` for an
out-of-range argument (§8.1), and `ab_euclid: 3 skipped, no trig (pl.set line 13
x3)` for a lock or trig-word write on a step without a trig (§6.3; `--json`:
`out_of_range` and `skipped_no_trig`); a write refused by a PICKUP track is listed the same
way (`skipped, PICKUP track`; `--json`: `skipped_pickup`). The legend's `l locks only` (a step with a
lock or sample lock and no trig of any kind) can no longer come from a script,
since §6.3 refuses the write; it still appears for a pattern read from a bank
file (`--bank`), so the legend stays. (Removing a trig also clears the step's locks:
§6.3.)

**Simulated** (everything in the registry, §8): the language, contexts,
loops, calls, state operators, `every`/`skip`, trigs, trigless trigs and
locks, parameter locks, sample locks, conditions, micro timing, retriggers,
part parameters, LFO targets, effects, scenes, the card listing and slots
(only the file *names* are loaded into slots; no audio is read). A "trig" for
`tr.add`, `tr.del`, `tr.clr`, `tr.flip` and `has.trig` is the red trig, the
trigless trig or the one-shot; trigless locks belong to `tl.*` alone. Locks and trig words land only on steps with a trig of any
kind, trigless locks included (§6.3); `trk.init` resets the sound, not the trigs.

**Files and slots as the unit can do them:**
- **Case**: sorting, `match`, `ext`, the folder lookup and the bag signature fold **ASCII letters only** (a-z = A-Z), never Unicode (é is not É).
- **Paths**: a `dir` path is canonical once: split on `/` and `\`, empty and `.` parts dropped, a leading `/` ignored, `..` gives an empty list. `static.load` stores `canonical folder + "/" + name` (the folder's case as written, the name's as on the card); warnings quote it.
- **Budget**: all live lists share **4096 names**. A frame has at most one own list; a callee's copy costs nothing until it runs its own `dir`, which gets `min(4096, free)` (the first names in sorted order; `dir "P": N files, only the first K kept`, K = the real cap). Replacing a list or returning gives the names back.
- **Start and refusals**: `simulate(slots={n: path}, refuse=set | predicate)`. A refused `static.load` leaves the slot **free** (an occupied one too: the unit clears it), is recorded (`refused`: func, op, line, slot, path; JSON `refused`, a text line) and the script goes on. A fill loop (cookbook, "Fill the Static slots") offers a refused slot to the next file; `static.swap a b` reloads each slot by name from the other's, a refused reload frees its slot, `a` = `b` or two empty slots move nothing. A `refuse` set compares ASCII-folded.
- **Pick bags**: key `(function, FNV-1a 32 of the signature)`, signature `PATH.UPPER|deep|f1,f2|n`; at most 8 bags in all, the oldest by insertion is dropped (reuse does not refresh the age). `state["bags"]`: `[{func, hash, n, rest}]`, oldest first (replaces the per-function `pick`).
- **CARD BUSY**: `simulate(busy=set of op names | callable(op))`; a card access (`dir`, `static.load`, `static.clr`, `static.swap`, only when there is work to do) that finds the card busy stops the run with `CardBusy` (a `RunError`), message exactly `CARD BUSY`; nothing is undone (`e.result` = the run so far).

**Not on the unit yet** (marked in the output): any operation whose registry `level` is `unknown`. The simulator runs
it (the model records its effect) so that scripts can be written
now. Also provisional: the 0..127 range of
parameter values, `micro` ±23, `ratch.set` 1..8 and `tempo` 30..300.

## 10. v1 limits (to be confirmed by measurement)

| | |
|---|---|
| functions loaded | 128 (visible in the menu: up to the host) |
| arguments / `param` per function | 4 / 8 |
| call depth / block nesting | 8 / 8 |
| VM stack | 16 values |
| line / string / list | 128 chars / 127 chars / 16 elements |
| total compiled table | 32 KB (DRAM reserve, to be confirmed) |
| `menu` host budget | ~4096 units (to be measured on the MKI) |

## 11. Asking the user (reserved, after v1)

Could a script ask for input in a dialog, e.g.
choose a folder? **Yes, planned as the `ask` family**, allowed only under
the `menu` host (a trig cannot wait for the user):

```
def new_kick
  menu "DRUMS" >> "NEW KICK"
  dir ask.dir "AUDIO/DRUMS"           # folder browser starting there
  a ask.num "VARIANTS" 4 1 8          # a number chosen with an encoder
  b ask.pick "STYLE" {"BOOM" "TRAP" "HOUSE"}   # a choice in a list → 1..3
  if ask.yes "REPLACE TRIGS?": tr.clr
```

- The VM is already designed to suspend and resume: an
  `ask` suspends the run, the dialog takes the keys, the run resumes with the
  answer. `EXIT` cancels the run; with UNDO, what was already done is undone.
- Precedent on the unit: a folder browser that scans a folder asynchronously,
  lists it, UP/DOWN or A/LEVEL to choose, ENTER to load, EXIT to cancel —
  the same shape as `ask.dir` / `ask.pick`.
- `ask.pick` needs constant lists of strings (v1 lists hold numbers only):
  a small extension of the `list` token.
- `ask.dir` returns a folder, so it needs either runtime strings (`fmt`)
  or the form `dir ask.dir "…"` only (the folder becomes
  the current `dir` directly). The second needs nothing new; proposed first.
- An `ask` makes the run's cost unknown in time but not in work: the budget
  still bounds what runs between two questions.
- Load-time checks: `ask` outside a `menu` function (or in a brick called
  from a non-`menu` host) → error.

## 12. Differences from Teletype (for those who know it)

- No 6-line limit; blocks by indentation; named functions with arguments
  instead of scripts 1–8 and `$ n`.
- No `w` (while): every loop is bounded at load time.
- Operations act on a **context** (tracks, steps) given by the triggering
  event.
- `a b c d` are local; `x y z t` global and persistent.
- No `;` (several commands on a line): an indented block replaces it.

## 13. Open

- `?` kept. Hexadecimal and the `== != …` aliases: keep
  them? (cheap, but more to learn).
- `param`: a step and a unit, like a numeric setting with a step?
- On-screen messages: short English (OT habit) — proposed.
- Variable lists (Teletype's `p`, 64 values per track) and pattern
  transforms (`tr.rot`, `tr.rev`, `tr.shuf`, `pl.rot`): **v2**. Also later: `dr.p` / `nr` drum tables (Teletype's data, GPL:
  licence to check), pitch (`n.s`, `n.c`, `qt.s`), `chaos`, `scale`, `inr`.
- Registry fields for the manual: `doc` (one line), `category` (RHYTHM,
  RANDOM, TRIGS…), `alias`, and `settable` (getter/setter names).
- A readable manual later, in the style of the Teletype command cheat sheet: a GitHub Pages site generated from the registry
  (`ops.py` → one entry per operation, grouped by namespace).

## 14. Compiled table (OTB), format 1

The firmware never parses text twice: at card load it compiles `/SCRIPTS/*.OTY`
into one **OTB table** in DRAM, and a small stack VM runs it. The computer tool produces the
**same bytes** (`python3 tools/otype.py dump`, `modules/otype/otb.py`) and has a reference VM
(`modules/otype/vm.py`, `run --vm`) that runs those bytes on the same data model and the same
operation handlers as the tree walker of §9.2 — the tests (`tools/test_otype_otb.py`) require the
two to give **identical results** (model, draws, cost, out-of-range list, state) for every
function without argument of the examples and of the other test scripts, seeds 0..20, three
presses chained through `--state`. Step 4 (the firmware) compares its bytes with these.

**Conventions.** Target: ColdFire, 32-bit, **big-endian**: every multi-byte field is big-endian.
Every 16-bit field of the header, the function table, the aux and line tables and the pool is at
an **even** offset from the start of the file (the 32-bit fields — cost, namespace mask — are 2-aligned, not
4-aligned; the code, byte-coded, is not aligned at all). Offsets are
relative to the region named in the table. A table is at most 32 KB (§10); nothing is truncated
silently: a function the checker or the compiler refuses is **absent**, and the header counts it.
`cost` values are saturated at 0xFFFFFFFF (= "no host can afford it").

### 14.1 File layout

| region | at | size | content |
|---|---|---|---|
| header | 0 | 32 | below |
| pool | `pool_off` (= 32) | `pool_len` | constants: strings, lists, keyword lists, menu lists |
| function table | `ftab_off` | `nfunc` × 54 | one entry per function, in table order |
| aux | `aux_off` | `aux_len` | per function: params, argument names, every/skip counters |
| lines | `line_off` | `line_len` | per function: code offset → source line |
| code | `code_off` | `code_len` | the instructions of all functions, back to back |

The regions follow each other without gap in that order (`ftab_off = pool_off + pool_len`, …,
`total = code_off + code_len`).

**Header** (32 bytes)

| off | size | field |
|---|---|---|
| 0 | 3 | magic `'O' 'T' 'B'` |
| 3 | 1 | format version = 1 |
| 4 | 1 | language version (`otype 1`) = 1 |
| 5 | 1 | flags = 0 (reserved) |
| 6 | 2 | `nfunc`: functions in the table |
| 8 | 2 | `nrefused`: functions of the sources that are NOT in the table (checker errors, `needs`, compile errors) |
| 10 | 2 | `pool_off` |
| 12 | 2 | `pool_len` |
| 14 | 2 | `ftab_off` |
| 16 | 2 | `aux_off` |
| 18 | 2 | `aux_len` |
| 20 | 2 | `line_off` |
| 22 | 2 | `line_len` |
| 24 | 2 | `code_off` |
| 26 | 2 | `code_len` |
| 28 | 2 | `total`: size of the file |
| 30 | 2 | reserved = 0 |

**Function table entry** (54 bytes). **Function order = the checker's order**: the files in
the order they are loaded (the tool: the command line; the firmware: to be fixed in step 4, **proposed:
sorted without case** like file lists, §2.1), the functions in the order of their `def` lines; refused ones skipped. A call carries the index in this table.

| off | size | field |
|---|---|---|
| 0 | 16 | name, upper case ASCII, NUL padded (≤ 16 characters) |
| 16 | 2 | `menu_off`: pool offset of the menu list (0 = no `menu`: a brick, or a page-only function) |
| 18 | 2 | host mask: bit 0 MENU, **bit 6 LIVE** (a live script: the function's own name is its label), **bit 7 PAGE** (`page "LABEL"`), **bit 8 GATE** (`in gate`), **bit 9 CC** (`in cc`); 1 TRIG, 2 LOOP, 3 PAT, 4 FILL, 5 LOAD reserved |
| 20 | 2 | families mask (union over the function **and what it calls**): 0x01 LANG, 0x02 CTX, 0x04 EDIT, 0x08 IO_R, 0x10 IO_W, 0x20 UI, 0x40 STATE; 0x80 PLAY, 0x100 ASK reserved |
| 22 | 1 | touches mask (same union): 1 TRAC, 2 PART, 4 SLOTS |
| 23 | 1 | flags: bit 0 destructive (§9: confirm asked), bit 1 `confirm` declared, **bit 2 confirm text** (`confirm "TEXT"`: its pool offset ends the aux data), **bit 3 typed params** (a type block follows, see the aux data), **bit 4 input sample** (`input sample`: bit 0x10, `OTC_FL_INPUT_SAMPLE`), **bits 5-6** the `var` values and the param sources (`OTC_FL_VARS`, `OTC_FL_PARAM_SOURCES`), **bit 7 `launch N`** (2 bytes end the aux data, `OTC_FL_LAUNCH`). A VM that ignores bits 2-7 reads the table as before (a `bool` / `option` reads as a number in its range) |
| 24 | 4 | static maximum cost (the checker's bound, §9.1) |
| 28 | 4 | namespace mask (union as above): bit *n* = a namespace number *n* of the registry is used (`no module …` check) |
| 32 | 1 | `nargs` (0..4) |
| 33 | 1 | `nparams` (0..8) |
| 34 | 1 | `nlocals`: locals to reserve (0..4: highest of a b c d used, plus one) |
| 35 | 1 | `max_stack`: deepest value stack, computed at compile time, **≤ 16** |
| 36 | 1 | `max_ctx`: context scopes needed, the function's own one included |
| 37 | 1 | `max_ctl`: control frames needed (loops and track passes nested) |
| 38 | 1 | `nctr`: every/skip counters |
| 39 | 1 | reserved = 0 |
| 40 | 2 | `file_off`: pool offset of the source file name |
| 42 | 2 | `def_line`: line of the `def` |
| 44 | 2 | `code_off`: in the code region |
| 46 | 2 | `code_len` |
| 48 | 2 | `aux_off`: in the aux region |
| 50 | 2 | `line_off`: in the line region |
| 52 | 2 | `nlines`: entries of the line table |

**Aux data of a function** (at `aux_off`, in this order): `nparams` × 8 bytes
`{name_off u16, default i16, min i16, max i16}` (name = pool string); `nargs` × 2 bytes
`{name_off u16}`; `nctr` × 4 bytes `{line u16, modifier index u8, 0}`; **only when the PAGE bit of the host mask is set**
2 bytes `{label_off u16}`, the pool offset of the page label (a type 1 string of 1..8 printable characters);
**only when the LIVE bit of the host mask is set** 4 bytes `{name_off u16, clock_ticks u16}` (the function's own name, and the period of its first `in clock` block, 0 when it has none); so
it sits at `aux_off + nparams*8 + nargs*2 + nctr*4` (a PAGE function has `nargs` = 0 and `nparams` ≤ 6; a loader rejects a
table where it does not, or where the label is not a 1..8 printable-character string); then, **only when flag bit 2 is set**:
2 bytes `{confirm_off u16}`, the pool offset of the text of `confirm "TEXT"` (a type 1 string of 1..127 printable characters,
kept as written), at `aux_off + nparams*8 + nargs*2 + nctr*4` (+ 2 after a page label). The text is the **first non-empty** one
the function declares (`confirm ""` and a plain `confirm` give none; a second text is ignored); a function with no text has
neither the bit nor the 2 bytes, so its bytes are those of before. A loader rejects a text that is not a 1..127
printable-character string. Then, **only when flag bit 3 is set** (typed parameters): `types u16`, 2 bits per
parameter k at bits 2k..2k+1 (0 int, 1 bool, 2 option, 3 folder); for each `folder` parameter in order, `path_off u16` (pool
string of its default path); for each `option` parameter in order, `count u16` then `count` × `label_off u16` (pool
strings). In the 8-byte parameter record a `bool` has min 0, max 1; an `option` min 0, max count − 1; a `folder` default
= `path_off`, min = max = 0. Then **only when flag bit 7 is set** (`launch N` of a live script): 2 bytes
`{launch_ticks u16}` (1..32767) that **end the aux data**, after the `var` values and the param sources; a function without
`launch` has neither the bit nor the 2 bytes, so its bytes are those of before. The counter an
`every`/`skip` instruction names is an index into this list; `(line, index)` is the key the state
file of `run --state` uses (`"12"` = modifier 0 of line 12, `"12.1"` = modifier 1).

**Line table** (at `line_off` of the function): `nlines` × `{code offset u16, source line u16}`,
ascending. The line of an instruction is that of the last entry at or before it. The VM reports an
out-of-range operation with it (§8.1: `hats: 3 out of range`), the dump shows it.

**Pool** (starts with 2 reserved bytes: no entry has offset 0). Entries are padded to an even
length; each exists once, in order of first use (functions in table order: file name, menu list, **page label**, **confirm text**,
param names, argument names, then the constants of the code). Offsets are relative to `pool_off`.

| type | layout | used for |
|---|---|---|
| 1 string | `01 len bytes` | string constants (≤ 127 characters), names, file names, page labels |
| 2 list | `02 count (i16 × count)` | list constants (≤ 16 elements) |
| 3 string list | `03 count (len bytes) × count` | reserved |
| 4 menu list | `04 nmenus` then per menu `nstr (len bytes) × nstr` | the `menu` lines: levels, then the action |

A string or list constant reaches the stack as **its pool offset** (`pushk`), a plain even number
below 32768; an operation whose argument kind is `str` or `list` (§8.1) gets the constant back
from the pool. A function argument of kind str or list is therefore an ordinary 16-bit value.
Trig-condition constants are not in the pool: `pushc` carries the code 0..64 (`sim.COND_NAMES`).

### 14.2 The machine

**Values** are 16-bit signed; the operations saturate (§5.2). **State of a run** (everything is
explicit, so a run can be suspended and resumed): per call a **frame** — `pc`, the value stack
(≤ `max_stack`), locals `a b c d` (0 at start), counters `i j` (0), the **context scopes**
(≤ `max_ctx`), the **control frames** (≤ `max_ctl`), the argument values, the current `dir`
list, the pending track list; for the run the globals `x y z t`, the five random streams (inside the
contexts, §5.3.2), the cost spent, the call frames (≤ 8). A **context scope** is what §6.2 calls the
context: tracks, steps, current step, five stream references (shared by reference, replaced by
a `seed`). `scope` pushes a copy of the top scope; `unscope` pops it. A call starts with
a copy of the caller's top scope; the root starts with the host track, steps 1..length and the streams
seeded by the press (§5.3.2).

**Control frames**: `lbeg`, `ebeg`, `pass` push one, `lnext`, `enext`, `pnext` advance or pop it.
A frame holds the body's first address, the address after the construct, the scope depth to
come back to, the counter / list, and the saved `i j`. Each iteration of a loop or pass runs in
a scope of its own (a copy of the line's scope; `each` narrows it to the one step, a pass to the one track).

### 14.3 Instruction set

One byte opcode, then fixed operands; sizes below. A jump target is an offset from the start of the
function's code (u16). "Stack" shows what the instruction pops/pushes. **Cost**: `op`, `opv` and the
native forms of registry operations (`sel` = `?`, `dir`, `ext`, `match`) add **the registry cost of
their operation** (`cost` field of `ops.py`), `set` adds 1, every other instruction adds nothing
— the checker's bound (§9.1) therefore holds for the run.

| code | mnemonic | operands | stack | meaning |
|---|---|---|---|---|
| 0x10 | `push8` | i8 | +1 | push a constant |
| 0x11 | `push16` | i16 | +1 | push a constant |
| 0x12 | `pushk` | u16 pool offset | +1 | push the offset of a string / list constant |
| 0x13 | `pushc` | u8 code | +1 | push a trig-condition constant (0..64) |
| 0x14 | `ld` | u8 v | +1 | push variable v: 0..7 = `a b c d x y z t`, 8 = `i`, 9 = `j` |
| 0x15 | `lda` | u8 n | +1 | push argument n of the running function |
| 0x16 | `ldp` | u8 n | +1 | push `param` n (its default; `--param` overrides it for the root function only) |
| 0x17 | `st` | u8 v | −1 | pop into variable v (0..7) |
| 0x18 | `drop` | — | −1 | discard (value of a statement) |
| 0x30 | `op` | u8 ns, u8 num | −n+r | **the** registry operation `(ns, num)`: pops its *n* arguments (`len(args)` of the registry), pushes its value if it has one (*r* = 1). Range checks (§8.1), the handler: the simulator's `call_op`. Language operators and `step`, `seed`, `x.seed` go through it |
| 0x31 | `opv` | u8 ns, u8 num, u8 form | −f | registry operation of variable shape. `trk`: form 0 = `cur`, 255 = `all`, 1..8 = pops that many tracks, builds the **pending track list** (valid ones, in order, no duplicate, the others counted out of range). `steps`: form 0 = `all`; 1 = pops a predicate id `(ns << 8 \| num)` (`push16`) and narrows to its steps; 2 = pops `b a` and replaces the steps (absolute) |
| 0x32 | `set` | u8 ns, u8 num | −1 | setter form of a state operation (`flip 1`, `o 5`, `drunk 80`): pops the value, cost 1 |
| 0x40 | `jmp` | u16 | 0 | forward jump |
| 0x41 | `jz` | u16 | −1 | pop; jump forward if 0 (`if`, `elif`) |
| 0x42 | `sel` | u16 | −1 | lazy `?`: cost of `?`; pop c; jump forward if c = 0 (to the *b* branch) |
| 0x43 | `prob` | u16 | −1 | pop p (clamped 0..100); **one** draw of the `prob` stream; jump forward unless `below(100) < p` |
| 0x44 | `every` | u8 k, u16 | −1 | pop n; step counter k as §7; jump forward unless it fires |
| 0x45 | `skip` | u8 k, u16 | −1 | same counter; jump forward if it fires |
| 0x46 | `jnev` | u8 kind, u8 ch, u8 num, u16 | 0 | jump forward unless current event matches kind (1 clock, 2 note, 3 cc, 4 trig), channel ch (0/255=any), and cc number num (255=any). For kind 4, ch is the track (1..8) and num the block's genre: 0 = a trig that plays (`trig.hit` 1), 1 = every trig placed; 2..255 are reserved |
| 0x47 | `jngt` | u16 name pool offset, u8 out, u16 | 0 | jump forward unless the current event is a gate (event type 5) whose source script name is the pool string (` `) and whose output is `out` (the guard of `in gate NAME n:`) |
| 0x50 | `lbeg` | u16 end | −2 | `l a b`: pops `b a`; pushes a control frame; first scope; i = a. The body is the next instruction; `end` is the address after the matching `lnext`. The body always runs at least once (a → b upwards or downwards, inclusive) |
| 0x51 | `lnext` | u16 body | 0 | end of an iteration: leave its scope; if the counter reached b, pop the frame and fall through, else step it (i), open the scope, **jump back to `body`** |
| 0x52 | `ebeg` | u16 end | 0 | `each`: the steps of the top scope (ascending); none: jump to `end`. Else frame, scope narrowed to the first step (`i`, `step.cur`) |
| 0x53 | `enext` | u16 body | 0 | next step (jump back) or pop the frame |
| 0x54 | `pass` | u16 end | 0 | begin the passes over the pending track list (§6.2: one pass per track, in order, each in a scope of that single track); empty list: jump to `end` |
| 0x55 | `pnext` | u16 body | 0 | next track (jump back) or pop the frame |
| 0x56 | `break` | — | 0 | pop control frames up to and including the innermost `l`/`each` (its scopes, its `i j`), jump to its `end` |
| 0x60 | `call` | u8 index | −n+1 | call function `index` of the table: pops its *n* arguments; new frame with a copy of the top scope and of the `dir` list; pushes its value (0 unless `ret v`) when it returns |
| 0x61 | `ret` | — | −1 | pop the value, leave the function |
| 0x62 | `stop` | — | 0 | leave the function (value 0); the last instruction of every function |
| 0x70 | `scope` | — | 0 | push a copy of the top scope |
| 0x71 | `unscope` | — | 0 | pop it |
| 0x80 | `dir` | u8 deep | −1 | `dir`: pops the folder (a pool string); operand 1 = `deep`. **Limits**: names whose relative path is longer than 127 bytes (UTF-8) are skipped, then only the first 4096 names of the sorted list are kept; `ext`/`match` filter afterwards. `deep` descends at most 15 folders below the listed one (`DIR_DEPTH_MAX`: a file is listed only if its relative name has at most 15 `/`; a deeper file is silently not listed and not counted, as on the unit). The simulator warns once per message (`dir "AUDIO/X": N names longer than 127 characters skipped`, `dir "AUDIO/X": N files, only the first 4096 kept`); constants `DIR_NAME_MAX`, `DIR_LIST_MAX`, `DIR_DEPTH_MAX` in `ops.py` |
| 0x81 | `ext` | u8 n | −n | `ext`: pops *n* keyword codes (index in `wav aif aiff aifc`) |
| 0x82 | `match` | u8 n | −n | `match`: pops *n* pool strings |
| 0x83 | `msg` | u8 n, u8 mask | −n | `msg`: pops *n* items (1..8); bit *i* of `mask` set = item *i* is a pool string, else a signed 16-bit number; the text (items in decimal, one space between) replaces the run's end-box text |

Unknown opcode, a pop from an empty stack, a pool offset that is not an entry, an unknown
`(ns, num)`: the table is corrupt — the VM stops the function with an error (never a script's fault).

### 14.4 How the constructs compile

| source | code |
|---|---|
| number, `const`, argument, `param`, variable | `push8`/`push16`, `pushk`/`pushc`, `lda`, `ldp`, `ld` |
| `add x y` (any registry operation) | the arguments left to right, then `op ns num`; the value is on the stack |
| `? c a b` | `c`, `sel Lb`, `a`, `jmp Lend`, `Lb:` `b`, `Lend:` — only one branch runs |
| `a expr` | `expr`, `st a` · `flip expr` → `expr`, `set` |
| operation / call used as a statement | …, `drop` when it has a value; a call always has one |
| `if c:` / `elif` / `else` chain | `c`, `jz Lnext`, body, `jmp Lend`, `Lnext:` …; an `elif` is only reached when no earlier branch ran, and a test is not evaluated otherwise |
| `prob p:` · `every n:` · `skip n:` | `p`, `prob Lskip`, rest of the line, `Lskip:` (same for `every k` / `skip k`; the counter k is one per line and modifier) |
| `l a b:` | `a`, `b`, `lbeg Lend`, body, `lnext Lbody`, `Lend:` |
| `each:` | `ebeg Lend`, body, `enext Lbody`, `Lend:` |
| `trk …:` (modifier) | arguments, `opv trk`, `pass Lend`, body, `pnext Lbody`, `Lend:` |
| `trk …` (statement) | the same, the body being **the rest of the block**; one track or many: the same code (§6.2) |
| `steps …`, `step s`, `seed n`, `x.seed n` | arguments, `opv steps form` / `op` |
| `dir f [deep]` · `ext k…` · `match s…` | `f` (+ `dir 0/1`) · `push8 code…`, `ext n` · `s…`, `match n` |
| `f \| a b` | `a`, `b`, `call index`, `drop` (statement) |
| `break` · `stop` · `ret v` | `break` · `stop` · `v` (else `push8 0`), `ret` |
| the end of the function | `stop` |

**Scopes** are emitted only where something is modified: a line whose modifiers or statement are
a context operation other than `trk` (`steps`, `step`, `seed`, `x.seed`) gets `scope` … `unscope` around
it, and so does a block that holds such a statement directly. Everything else — loop iterations
(`lbeg`/`lnext`/`enext`), track passes — manages its own scope. The result is the context rules of
§6.2 (a block gives its context back, a statement-form change lasts to the end of its block) with
no scope where nothing could tell.

### 14.5 Why a run always ends

1. Every jump except three is **forward** (target > the instruction's own address): `jmp jz sel prob
   every skip lbeg ebeg pass`. Straight-line code and forward jumps cannot loop.
2. The only **backward** jumps are `lnext`, `enext`, `pnext`. Each is the last instruction of its construct,
   its target is the address right after its own `lbeg`/`ebeg`/`pass`, and it jumps back only while its
   control frame has iterations left:
   - `lnext`: at most |b − a| + 1 iterations, a and b being **fixed when `lbeg` ran** (they are
     constants, `param`s or constant arguments, §7 — never a variable; the body cannot change the frame's counter);
   - `enext`: at most 64 (the steps of the context, fixed when `ebeg` ran);
   - `pnext`: at most 8 (the track list, fixed when `pass` ran).
   Nothing else writes a frame's counter, and `break` only pops frames.
3. Loops and passes nest at most as deep as the blocks do (`max_ctl`), so the instructions one call
   can execute are bounded by the code length × the product of these counts.
4. **Calls** form a DAG (§9: `loop a>b>a` is an error) of depth ≤ 8; the table has no recursion.
5. Hence a run executes at most `code_len × Π(iterations)` instructions per call, and its cost
   never exceeds the static maximum stored in the function entry (the checker's bound). A loader can
   re-verify 1–2 from the bytes alone: forward jumps go forward, every backward jump is one of the
   three and targets the address after its matching begin, begins and nexts nest properly. The reference VM also
   stops a run at 2 000 000 cost units. `tools/test_otype_otb.py` checks these rules on every table
   it builds.

### 14.6 Determinism, limits, errors

- The same sources give the same bytes: nothing depends on dictionary order, hashes or time (checked in
  another process with another `PYTHONHASHSEED`).
- A function whose deepest value stack is **> 16** is a compile error, reported like a checker error
  (`FILE:LINE: func: error: stack depth 17 > 16`, line of the expression that went over); the function
  and those that call it (`needs …`) are absent from the table and counted in `nrefused`. More than 255
  functions, or a table over 32 KB, is an error of the whole table.
- The compiled table keeps the text of `confirm "…"` (flag bit 2, §14.1), not
  `on …` hosts (only MENU and PAGE exist).

### 14.7 `otype.py dump` and `run --vm`

```
otype.py dump FILE.OTY ... [--func NAME] [-o OUT.OTB] [--hex] [--profile NAME]
otype.py run FUNC FILE.OTY ... --vm                     # same output as without --vm
```

`dump`: per function a header line (name, source file:line, menu (`(page only)` for a page function without `menu`), families that a host may forbid,
touches, static cost, stack / scopes / control frames, locals), the params, then **one instruction
per line**: offset, bytes, mnemonic with decoded operands (registry names for ids, pool constants,
variable and argument names, jump targets as `@offset`), and the source line number and text in the
comment column at the first instruction of each line. A page function shows `hosts PAGE` (`hosts MENU PAGE` with a menu) and a `page   "LABEL"` line before the params. `-o` writes the binary, `--hex` prints a hex dump
instead of the listing. Diagnostics (checker and compiler) go to stderr; exit 1 if any function was refused.

`--profile NAME` restricts the table to the ops a given firmware has (`ops.PROFILES`; today only `m1`, which now
holds every op). A function that uses another op directly is
refused with `not on the unit yet: <op>` (at its first such line), its callers with `needs <name>`;
an unknown name is a usage error (exit 2). `check --profile NAME` reports the same refusals as errors.

### 14.8 Worked example: `walk` (`modules/otype/examples/RHYTHM.OTY`)

```
def walk
  menu "DRUMS" >> "WALK"
  o.min 1
  o.max 16
  o.wrap 1                        # 1..16, then 1 again: each press goes on where the last one stopped
  drunk.min 40
  drunk.max 120
  if lt drunk 41: drunk 80        # first press: drunk starts at 0 (held at 40): start the walk mid-range
  trk cur:
    l 1 8:
      a o                         # read o once: each read moves it
      tr.add a
      pl.set a amp.vol drunk
```

`python3 tools/otype.py dump modules/otype/examples/*.OTY --func walk`:

```
walk  (modules/otype/examples/RHYTHM.OTY:20)  menu DRUMS > WALK  families EDIT  touches TRAC  cost 114  stack 3  scopes 3  ctl 2  locals a
  hosts MENU  namespaces lang ctx tr pl prm state  code 83 bytes @1629  lines 13  counters 0
0000  10 01        push8 1                    ; 22  o.min 1
0002  30 10 03     op o.min                   ;
0005  10 10        push8 16                   ; 23  o.max 16
0007  30 10 04     op o.max                   ;
000a  10 01        push8 1                    ; 24  o.wrap 1
000c  30 10 05     op o.wrap                  ;
000f  10 28        push8 40                   ; 25  drunk.min 40
0011  30 10 08     op drunk.min               ;
0014  10 78        push8 120                  ; 26  drunk.max 120
0016  30 10 09     op drunk.max               ;
0019  30 10 07     op drunk                   ; 27  if lt drunk 41: drunk 80
001c  10 29        push8 41                   ;
001e  30 00 0f     op lt                      ;
0021  41 00 29     jz @0029                   ;
0024  10 50        push8 80                   ;
0026  32 10 07     set drunk                  ;
0029  31 01 01 00  opv trk cur                ; 28  trk cur:
002d  54 00 52     pass @0052                 ;
0030  10 01        push8 1                    ; 29  l 1 8:
0032  10 08        push8 8                    ;
0034  50 00 4f     lbeg @004f                 ;
0037  30 10 02     op o                       ; 30  a o
003a  17 00        st a                       ;
003c  14 00        ld a                       ; 31  tr.add a
003e  30 03 01     op tr.add                  ;
0041  14 00        ld a                       ; 32  pl.set a amp.vol drunk
0043  30 0a 13     op amp.vol                 ;
0046  30 10 07     op drunk                   ;
0049  30 05 01     op pl.set                  ;
004c  51 00 37     lnext @0037                ; 29  l 1 8:
004f  55 00 30     pnext @0030                ; 28  trk cur:
0052  62           stop                       ;
```

Its function entry (54 bytes, at `ftab_off` + 27 × 54 in the table of the seven example files):

```
57 41 4c 4b 00 00 00 00 00 00 00 00 00 00 00 00   name "WALK"
04 a4            menu list at pool offset 0x4a4 ("DRUMS" > "WALK")
00 01            hosts: MENU
00 47            families: LANG CTX EDIT STATE (the dump hides the three every host allows)
01               touches: TRAC
00               flags
00 00 00 72      static cost 114
00 01 04 2b      namespaces LANG CTX TR PL PRM STATE (bits 0 1 3 5 10 16)
00 00 01 03      nargs 0, nparams 0, nlocals 1 (a), max_stack 3
03 02 00 00      max_ctx 3, max_ctl 2, nctr 0, reserved
04 3a  00 14     file name at pool offset 0x43a, def line 20
06 5d  00 53     code at 0x65d of the code region, 83 bytes
00 fe  02 e0     aux offset 0xfe, line table offset 0x2e0
00 0d            13 line entries
```

Reading the code: `0000`–`0016` the five statements `o.min` … `drunk.max` (`o.min 1` is `push8 1`, then `op o.min`:
the argument, then the registry id `(16, 3)`); `0019`–`0026` `if lt drunk 41: drunk 80`: `op drunk` (the *getter*, one
read: it moves), `push8 41`, `op lt`, `jz @0029` over the setter `set drunk` (cost 1, setter form); `0029` `opv trk cur`
builds the track list `[current track]` and `pass @0052` opens the pass — **the body of `trk cur:` is the code from `0030`
to `004f`**, closed by `pnext @0030` (the only way back); `0034` `lbeg @004f` opens the `l 1 8` loop, `lnext @0037`
closes it; `a o` is `op o` (one read: `o` moves) then `st a`; `0052` `stop` ends the function. Stack: the deepest point is
`pl.set a amp.vol drunk` with `a`, `amp.vol`, `drunk` on the
stack: 3. Scopes: the function's own + the pass + the loop iteration = 3; control frames: the pass and the loop = 2.
