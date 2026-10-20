"""OType simulator: the reference behaviour of `otype.py run` (spec: docs/syntax.md 5.3.2, 9.2).

It walks the tree built by the load-time checker (tools/otype.py) -- it never parses text itself --
and executes a function on a data model of one pattern (trigs, trigless, locks, sample locks,
conditions, micro timing, ratchets, part fields, slots).  What it does to random numbers, the
evaluation order, the scope of seeds and of the context, the state operators, the out-of-range
rule and the cost is THE REFERENCE the firmware must reproduce byte for byte.

    simulate(prog, "ab_euclid", seed=7, bank=None, pattern=1, track=1, length=None, card=None,
             state=None, params=None) -> Result

Nothing here ever writes to a bank file or to a card: they are read only.
"""
import copy
import json
import os
import re
import sys
import time

from modules.otype import ops as O

M32 = 0xFFFFFFFF
S16_MIN, S16_MAX = -32768, 32767

# ---- random streams (spec 5.3.2) -------------------------------------------------------------
STREAM_K = {"RAND": 1, "TOSS": 2, "PROB": 3, "DRUNK": 4, "FILE": 5}
STREAM_NAMES = tuple(sorted(STREAM_K, key=STREAM_K.get))


def seed_state(n, k):
    """Initial xorshift32 state of stream number k seeded with the value n (spec 5.3.2)."""
    x = (n & 0xFFFF) | (k << 16)
    x = (x * 0x9E3779B1) & M32
    x ^= x >> 16
    x = (x * 0x85EBCA6B) & M32
    x ^= x >> 13
    x = (x * 0xC2B2AE35) & M32
    x ^= x >> 16
    return x if x else 0x6D2B79F5


def xorshift32(x):
    """One xorshift32 step (13, 17, 5): the new state, which is also the output."""
    x ^= (x << 13) & M32
    x ^= x >> 17
    x ^= (x << 5) & M32
    return x


class Stream:
    """One random stream.  Every draw (one `next`) is counted in the run's `draws`."""

    def __init__(self, name, n, draws=None):
        self.name = name
        self.x = seed_state(n, STREAM_K[name])
        self.draws = draws

    def next(self):
        self.x = xorshift32(self.x)
        if self.draws is not None:
            self.draws[self.name] += 1
        return self.x

    def below(self, n):
        """0..n-1 for n >= 1, else 0 -- and exactly one draw in both cases."""
        r = self.next()
        return ((r >> 16) * n) >> 16 if n >= 1 else 0

    def toss(self):
        return self.next() >> 31


def vectors_text(seeds=(1, 0), count=5):
    """The test vectors of spec 5.3.2: the first `count` outputs of each stream for each seed.

    Printed by `python3 -c "from modules.otype import sim; print(sim.vectors_text())"`; the same
    numbers are hard-coded in tools/test_otype_run.py."""
    out = []
    for seed in seeds:
        out.append("seed %d" % seed)
        for name in STREAM_NAMES:
            st = Stream(name, seed)
            x0 = st.x
            outs = [st.next() for _ in range(count)]
            out.append("  %-5s start 0x%08x  %s" % (name.lower(), x0, " ".join(str(v) for v in outs)))
    return "\n".join(out)


def sat(v):
    return S16_MIN if v < S16_MIN else S16_MAX if v > S16_MAX else v


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# ---- conditions (docs/TRIGS.md) --------------------------------------------------------------
PROBS = (1, 2, 4, 6, 9, 13, 19, 25, 33, 41, 50, 59, 67, 75, 81, 87, 91, 94, 96, 98, 99)
_COND_WORDS = ("OFF", "FILL", "!FILL", "PRE", "!PRE", "NEI", "!NEI", "1ST", "!1ST")
COND_NAMES = list(_COND_WORDS) + ["%d%%" % p for p in PROBS]
for _b in range(2, 9):
    for _a in range(1, _b + 1):
        COND_NAMES.append("%d:%d" % (_a, _b))
assert len(COND_NAMES) == 65          # codes 0..64


def cond_code(text):
    """Code 0..64 of a condition constant.  `1:1` is not one: the checker refuses it at load."""
    return COND_NAMES.index(str(text).upper())


def cond_name(code):
    return O.show(COND_NAMES[code])


# ---- parameters ------------------------------------------------------------------------------
def param_name(p):
    """Readable name of a flat parameter number 0..29 (PB 0..5, LFO 6..11, AMP 12..17, FX1, FX2)."""
    if p < 6:
        return "pb[%d]" % (p + 1)
    if p < 12:
        return "lfo.%s" % ("spd1 spd2 spd3 dep1 dep2 dep3".split()[p - 6])
    if p < 18:
        return "amp.%s" % ("atk hold rel vol bal xvol".split()[p - 12])
    if p < 24:
        return "fx1[%d]" % (p - 17)
    return "fx2[%d]" % (p - 23)


# defaults of the part's parameters (docs/TRACK.md: LFO at rest, AMP defaults); 0 where unknown
PARAM_DEFAULT = {6: 32, 7: 32, 8: 32, 12: 0, 13: 127, 14: 127, 15: 64, 16: 64, 17: 127}
VAL_MAX = 127          # PROVISIONAL: the NEUTRAL range of a parameter (0..127) when the part says nothing (see ParamModel)
LOCK_MAX = 254         # a lock is a byte of the step record and 0xff means "no lock": no range reaches 255
MICRO_MAX = 23
TEMPO_MIN, TEMPO_MAX = 30, 300
NUM_STEPS = 64

# ---- the part -------------------------------------------------
# What the unit says about the part is read AT RUNTIME through host imports (c/otc_vm.h); the values below are the
# NEUTRAL answers of the simulator's twin (ParamModel) when a test or a user gives nothing else -- none of them is a
# byte of the firmware's tables.
FX_COUNT_NEUTRAL = {1: 11, 2: 15}     # entries of the FX1 / FX2 choosers, NONE (index 0) included: the lengths the stock
                                      # choosers had when the earlier RANDOM FX function was built; the unit reads its own
FX_DEFAULT_NEUTRAL = {1: 1, 2: 1}     # chooser index of the factory effect (FILTER / DELAY on the unit, found by name)
LFO_WAVES_NEUTRAL = 7                 # LFO waveforms of the neutral model (the unit reads the LFO descriptor's count)
SCENES = 16
SCENE_BYTES = 32                      # a scene block of one track: bytes 0..29 locks, 30..31 unused; 0xff = no lock
NOT_LOCKED = 0xFF
CHOOSER_MAX = 255                     # a chooser length / index / waveform count from the unit is cut to a byte

# ---- the data model --------------------------------------------------------------------------
MASKS = ("trig", "tless", "tlock", "oneshot", "rec1", "rec2", "rec3", "m38", "swing", "slide")
# tr.add / tr.del / tr.clr / tr.flip and has.trig work on the red trig,
# the trigless trig and the one-shot; the trigless locks belong to tl.* alone.
TRIG_FAMILY = ("trig", "tless", "oneshot")
TL_MASK = "tlock"                              # what tl.add / tl.fill write
PICKUP = 4                                     # the machine type of a PICKUP track (refuses writes)
SWING_DEFAULT = 0xAAAAAAAAAAAAAAAA


def _bit(s):
    return 1 << (s - 1)


class TrackData:
    """One audio track of the pattern.  Steps are 1..64; sparse dicts hold what is not default."""

    def __init__(self):
        self.masks = {m: 0 for m in MASKS}
        self.masks["swing"] = SWING_DEFAULT
        self.locks = {}     # step -> {param: value}
        self.smp = {}       # step -> slot 1..128 (129..136 = a recorder, from a bank file)
        self.cond = {}      # step -> code 1..64 (0 = none, not stored)
        self.micro = {}     # step -> -23..23 (0 not stored)
        self.count = {}     # step -> trigs per step 2..8 (1 not stored)
        self.swing = 50     # swing amount 50..80 (default 50)
        self.os_armed = 1   # one-shot arm flag (1 = armed)

    def has(self, mask, s):
        return bool(self.masks[mask] & _bit(s))

    def put(self, mask, s, on=True):
        if on:
            self.masks[mask] |= _bit(s)
        else:
            self.masks[mask] &= ~_bit(s)

    def has_trig(self, s):
        return any(self.has(m, s) for m in TRIG_FAMILY)

    def has_any_trig(self, s):
        """A trig of ANY kind: red, trigless trig, trigless lock or one-shot (the four masks).
        Locks and trig-word writes only land on such a step ."""
        return any(self.has(m, s) for m in MASKS[:4])

    def clear_trig(self, s):
        for m in TRIG_FAMILY:
            self.put(m, s, False)

    def remove_trig(self, s):
        """Remove the trig of step s as the stock release does : the
        trig bits go; unless a trigless lock keeps the step, so do its p-locks, sample lock and
        condition.  Micro and count stay (reset by the next tr.add).  A step with no trig of the
        family is left alone."""
        if not self.has_trig(s):
            return
        self.clear_trig(s)
        if not self.has(TL_MASK, s):
            self.locks.pop(s, None)
            self.smp.pop(s, None)
            self.cond.pop(s, None)

    def add_trig(self, s):
        """tr.add on step s : already red: nothing; a one-shot or a
        trigless trig becomes a red trig and keeps everything; a step with no trig of the family
        gets the trig and a zeroed trig word (condition, micro, count)."""
        if self.has("trig", s):
            return
        if self.has("tless", s) or self.has("oneshot", s):
            self.put("tless", s, False)
            self.put("oneshot", s, False)
        else:
            self.cond.pop(s, None)
            self.micro.pop(s, None)
            self.count.pop(s, None)
        self.put("trig", s)

    def add_tlock(self, s):
        """tl.add on step s (provisional): already a trigless lock: nothing; a step with
        no trig of ANY kind gets the bit and a zeroed trig word (as tr.add: what a deleted trig left in the word
        is no longer wanted); a step that already carries a trig keeps its word (it is that trig's).  The record
        is never touched."""
        if self.has(TL_MASK, s):
            return
        if not self.has_any_trig(s):
            self.cond.pop(s, None)
            self.micro.pop(s, None)
            self.count.pop(s, None)
        self.put(TL_MASK, s)

    def has_lock(self, s):
        return bool(self.locks.get(s))

    def is_empty(self, s):
        return not (any(self.has(m, s) for m in MASKS[:4]) or self.has_lock(s) or s in self.smp)

    def lock_set(self, s, p, v):
        self.locks.setdefault(s, {})[p] = v

    def lock_del(self, s, p=None):
        d = self.locks.get(s)
        if d is None:
            return
        if p is None:
            d.clear()
        else:
            d.pop(p, None)
        if not d:
            del self.locks[s]

    def set_opt(self, store, s, v, default):
        if v == default:
            store.pop(s, None)
        else:
            store[s] = v

    def to_json(self):
        steps = {}
        for s in sorted(set(self.locks) | set(self.smp) | set(self.cond) | set(self.micro)
                        | set(self.count)):
            d = {}
            if self.locks.get(s):
                d["locks"] = {param_name(p): v for p, v in sorted(self.locks[s].items())}
            if s in self.smp:
                d["smp"] = self.smp[s]
            if s in self.cond:
                d["cond"] = cond_name(self.cond[s])
            if s in self.micro:
                d["micro"] = self.micro[s]
            if s in self.count:
                d["ratch"] = self.count[s]
            steps[str(s)] = d
        return {"masks": {m: "0x%016x" % self.masks[m] for m in MASKS}, "steps": steps}


def scene_byte_name(k):
    """Name of byte k (0..31) of a track's scene block: the parameter it locks, or byte30 / byte31 (unused)."""
    return param_name(k) if k < 30 else "byte%d" % k


class PartData:
    """What the run WROTE into the part for one track.  The part the run starts from is
    the model's (ParamModel): nothing here is a copy of it, only the writes -- so the C VM's host twin
    (cvm.HostParams) and this record compare one to one."""

    def __init__(self):
        self.mtype = 0                    # 0 = STATIC (docs/TRACK.md); nothing here changes it
        self.slot = None                  # TRK.SLOT n, or "factory" after trk.init
        self.level = 108                  # 0..127
        self.cue = 0                      # 0..127
        self.params = {}                  # flat param -> value written (par.set, trk.init)
        self.pb_setup = {}                # 1..6 -> val
        self.amp_setup = {}               # 1..6 -> val
        self.lfo_dest = {}                # LFO 1..3 -> flat param it is aimed at
        self.lfo_wave = {}                # LFO 1..3 -> waveform
        self.lfo_mult = {}                # LFO 1..3 -> mult
        self.lfo_trig = {}                # LFO 1..3 -> trig mode
        self.lfod = [0] * 16              # steps 1..16 (-64..63)
        self.lfod_interp = [0] * 16       # steps 1..16 (0 or 1)
        self.fx = {}                      # FX slot 1..2 -> chooser index (0 = NONE)
        self.fx1_setup = {}               # 1..6 -> val
        self.fx2_setup = {}               # 1..6 -> val
        self.scenes = {}                  # scene 1..16 -> {byte 0..31: value written} (NOT_LOCKED = 0xff = no lock)
        self.rec_setup = {}               # RECORDING SETUP: byte offset 0, 1, 2, 4 -> val

    def scenes_json(self):
        out = {}
        for sc, d in sorted(self.scenes.items()):
            if len(d) == SCENE_BYTES and all(v == NOT_LOCKED for v in d.values()):
                out[str(sc)] = "off"              # the whole block unlocked (scn.clr, trk.init)
            else:
                out[str(sc)] = {scene_byte_name(k): ("off" if v == NOT_LOCKED else v) for k, v in sorted(d.items())}
        return out

    def to_json(self):
        d = {"type": self.mtype, "slot": self.slot,
             "params": {param_name(p): v for p, v in sorted(self.params.items())},
             "lfo_dest": {str(k): param_name(v) for k, v in sorted(self.lfo_dest.items())},
             "lfo_wave": {str(k): v for k, v in sorted(self.lfo_wave.items())},
             "fx": {str(k): v for k, v in sorted(self.fx.items())},
             "scenes": self.scenes_json()}
        if self.level != 108:
            d["level"] = self.level
        if self.cue != 0:
            d["cue"] = self.cue
        if self.pb_setup:
            d["pb_setup"] = {str(k): v for k, v in sorted(self.pb_setup.items())}
        if self.amp_setup:
            d["amp_setup"] = {str(k): v for k, v in sorted(self.amp_setup.items())}
        if self.lfo_mult:
            d["lfo_mult"] = {str(k): v for k, v in sorted(self.lfo_mult.items())}
        if self.lfo_trig:
            d["lfo_trig"] = {str(k): v for k, v in sorted(self.lfo_trig.items())}
        if any(v != 0 for v in self.lfod):
            d["lfod"] = list(self.lfod)
        if any(v != 0 for v in self.lfod_interp):
            d["lfod_interp"] = list(self.lfod_interp)
        if self.fx1_setup:
            d["fx1_setup"] = {str(k): v for k, v in sorted(self.fx1_setup.items())}
        if self.fx2_setup:
            d["fx2_setup"] = {str(k): v for k, v in sorted(self.fx2_setup.items())}
        if self.rec_setup:
            d["rec_setup"] = {str(k): v for k, v in sorted(self.rec_setup.items())}
        return d


class ParamModel:
    """What the PART says about a parameter, for the p-lock ops.

    On the unit the C VM reads it AT RUNTIME through two host imports (nothing of the firmware's descriptor is
    copied into the sources): `otb_host_param_info(track0, p, &lo, &count, &enabled)` (the descriptor's range and
    whether the slot is enabled) and `otb_host_param_value(track0, p)` (the part's current value).  This is the
    simulator's twin of that model: `info` {(track 1..8, p): (lo, count, enabled)} and `values` {(track, p): v}.
    What it does not list is NEUTRAL: range 0..127 (lo 0, count 128), enabled, value = PARAM_DEFAULT.get(p, 0).
    The twin is a model, not the unit's table: a host that needs the real values supplies them.

    The part ops also read, each behind an import of its own on the unit:
    `defaults` {(track, p): v} (otb_host_param_default: the descriptor's default, trk.init; neutral PARAM_DEFAULT),
    `fx_count` {slot 1..2: n} (otb_host_fx_count: the chooser's length, NONE included; neutral FX_COUNT_NEUTRAL),
    `fx_default` {slot: index} (otb_host_fx_default: the factory effect's chooser index; neutral FX_DEFAULT_NEUTRAL),
    `lfo_waves` (otb_host_lfo_waves: the number of LFO waveforms; neutral LFO_WAVES_NEUTRAL).  Raw words, normalised by
    the same functions as the C (chooser_norm)."""

    def __init__(self, info=None, values=None, defaults=None, fx_count=None, fx_default=None, lfo_waves=None,
                 setup_info=None, setup_values=None, flex_slots=None, tempo_mode=False):
        self.info = dict(info or {})
        self.values = dict(values or {})
        self.defaults = dict(defaults or {})
        self.fx_count = dict(fx_count or {})
        self.fx_default = dict(fx_default or {})
        self.lfo_waves = lfo_waves
        self.setup_info = dict(setup_info or {})
        self.setup_values = dict(setup_values or {})
        self.flex_slots = set(flex_slots or ())
        self.tempo_mode = tempo_mode

    def setup_range(self, t, name, n):
        lo, count, enabled = self.setup_info.get((t, name, n), (0, 128, True))
        return (lo, lo + count - 1) if enabled and count > 0 else None

    def param_info(self, t, p):
        """(lo, count, enabled) as the C reads it (normalised: see param_norm)."""
        return param_norm(*self.info.get((t, p), (0, VAL_MAX + 1, True)))

    def param_value(self, t, p):
        return self.values.get((t, p), PARAM_DEFAULT.get(p, 0))

    # ---- the RAW words of the imports (the twin answers them; the C and the simulator normalise) ---------------
    def raw_default(self, t, p):
        return self.defaults.get((t, p), PARAM_DEFAULT.get(p, 0))

    def raw_fx_count(self, slot):
        return self.fx_count.get(slot, FX_COUNT_NEUTRAL[slot])

    def raw_fx_default(self, slot):
        return self.fx_default.get(slot, FX_DEFAULT_NEUTRAL[slot])

    def raw_lfo_waves(self):
        return LFO_WAVES_NEUTRAL if self.lfo_waves is None else self.lfo_waves

    # ---- normalised, as vm.c reads them -----------------------------------------------------------------------------
    def param_default(self, t, p):
        return sint32(self.raw_default(t, p))

    def fx_len(self, slot):
        return chooser_norm(self.raw_fx_count(slot))

    def fx_factory(self, slot):
        return int(self.raw_fx_default(slot)) & 0xFFFFFFFF          # an index >= fx_len = no factory effect: skipped

    def waves(self):
        return chooser_norm(self.raw_lfo_waves())


def sint32(v):
    """An import's int32 word as the C sees it."""
    v = int(v) & 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def chooser_norm(n):
    """A chooser length or a waveform count from the unit (unsigned 32-bit word) as vm.c uses it: cut to 0..255."""
    return min(int(n) & 0xFFFFFFFF, CHOOSER_MAX)


def param_norm(lo, count, enabled):
    """The one normalisation of a descriptor range, shared with vm.c (`pinfo`): a lock is a byte and 0xff means
    "none", so lo is 0..254 and the range ends at 254 at the most; at least one value."""
    lo = min(int(lo) & 0xFFFFFFFF, LOCK_MAX)               # (the imports answer unsigned 32-bit words)
    count = clamp(int(count) & 0xFFFFFFFF, 1, LOCK_MAX + 1 - lo)
    return lo, count, bool(enabled)


class Model:
    def __init__(self):
        self.pmodel = ParamModel()        # the part's descriptors and values (not in to_json: a model input)
        self.tracks = {t: TrackData() for t in range(1, 9)}
        self.part = {t: PartData() for t in range(1, 9)}
        self.saved_parts = {}             # (bank, part) -> snapshot
        self.slots = {}                   # slot 1..128 -> path of the file loaded there
        self.working = []                 # the working set of slots (slots.add / slots.rnd)
        self.project_tempo = 120
        self.pattern_tempos = {}
        self.pattern = 1                  # 1..16
        self.bank = 1                     # 1..16
        self.part_cur = 1                 # 1..4
        self.patterns = {(1, 1): self.tracks}
        self.parts = {(1, 1): self.part}
        self.part_links = {}
        self.pattern_lengths = {}
        self.mutes = {t: 0 for t in range(1, 9)}
        self.fill = 0                     # 0 or 1
        self.fill_once = 0                # 0 or 1
        self.msgs = []
        self.flex_slots = {}
        self.flex_playing = set()
        self.cat_list = []
        self.audio_source = {"path": "AUDIO/SOURCE.WAV", "empty": False, "recording": False}
        self.audio_refuse = {}
        self.normalized = {}
        self.exists_files = set()
        # what the MIDI IN hook has stored: the last CC per (channel, controller), the last note and its
        # velocity per channel (the unit's otype_cc_table / otype_last_note / otype_last_vel)
        self.midi_cc = {}
        self.midi_note = {}
        self.midi_vel = {}
        self.rec_arms = []                # (track, source mask) of every rec.arm of the run

    @property
    def tempo(self):
        return self.pattern_tempos.get((self.bank, self.pattern), 120) if self.pmodel.tempo_mode else self.project_tempo

    @tempo.setter
    def tempo(self, value):
        if self.pmodel.tempo_mode:
            self.pattern_tempos[self.bank, self.pattern] = value
        else:
            self.project_tempo = value

    def go_pattern(self, bank, pattern):
        self.patterns[self.bank, self.pattern] = self.tracks
        self.parts[self.bank, self.part_cur] = self.part
        self.bank, self.pattern = bank, pattern
        self.tracks = self.patterns.setdefault((bank, pattern), {t: TrackData() for t in range(1, 9)})
        self.part_cur = self.part_links.get((bank, pattern), 1)
        self.part = self.parts.setdefault((bank, self.part_cur), {t: PartData() for t in range(1, 9)})

    def go_part(self, part):
        self.parts[self.bank, self.part_cur] = self.part
        self.part_cur = part
        self.part_links[self.bank, self.pattern] = part
        self.part = self.parts.setdefault((self.bank, part), {t: PartData() for t in range(1, 9)})

    def snapshot(self):
        return copy.deepcopy(self)

    def param_value(self, t, p):
        """The part's value of parameter p on track t: what the run wrote (par.set ...), else the model's."""
        pr = self.part[t].params
        return pr[p] if p in pr else self.pmodel.param_value(t, p)

    def to_json(self):
        return {"tracks": {str(t): self.tracks[t].to_json() for t in range(1, 9)},
                "part": {str(t): self.part[t].to_json() for t in range(1, 9)},
                "slots": {str(k): v for k, v in sorted(self.slots.items())},
                "working_set": list(self.working), "tempo": self.tempo, "messages": list(self.msgs)}


# ---- bank files (read only) ------------------------------------------------------------------
BANK_HEAD = 0x16
PATTERN_STRIDE = 0x8EEC
TRACK_STRIDE = 0x922


def trac_off(p, t):
    """File offset of track t's TRAC data in pattern p (both IFF headers: PTRN 8 + TRAC 9)."""
    return BANK_HEAD + p * PATTERN_STRIDE + 8 + t * TRACK_STRIDE + 9


class BankError(Exception):
    pass


def decode_track(d, tr, word_off):
    """The TrackData of the TRAC data at `d[tr:]`: masks (+0x00), step records (+0x59), trig words at
    +word_off (0x899 in a bank file, 0x89a in RAM).  The one decoding of read_bank and of the RAM
    blocks the C VM writes (ram_blocks, decode_ram_block)."""
    td = TrackData()
    td.swing = d[tr + 0x52] + 50
    for k, m in enumerate(MASKS):
        td.masks[m] = int.from_bytes(d[tr + 8 * k:tr + 8 * k + 8], "big")
    for s in range(1, 65):
        rec = d[tr + 0x59 + (s - 1) * 0x20:tr + 0x59 + s * 0x20]
        locks = {k: rec[k] for k in range(30) if rec[k] != 0xFF}
        if locks:
            td.locks[s] = locks
        if rec[31] != 0xFF:
            td.smp[s] = rec[31] + 1
        o = tr + word_off + (s - 1) * 2
        w = int.from_bytes(d[o:o + 2], "big")
        cnt = ((w >> 13) & 7) + 1
        mic = (w >> 7) & 0x3F
        if mic >= 32:
            mic -= 64                       # signed 6 bits (negatives: HYPOTHESIS)
        cond = w & 0x7F
        if cnt > 1:
            td.count[s] = cnt
        if mic:
            td.micro[s] = mic
        if 0 < cond <= 64:
            td.cond[s] = cond
    return td


RAM_TRAC_SIZE = 0x91A          # one track's TRAC block in RAM (the C VM binds these)


def decode_ram_block(blk):
    """TrackData of a RAM TRAC block (0x91a bytes; the trig words at +0x89a)."""
    return decode_track(blk, 0, 0x89A)


def ram_blocks(path, pattern):
    """{1..8: bytearray(0x91a)}: the RAM TRAC blocks of the 8 audio tracks of pattern `pattern` of a
    bank file: everything copied as it is, the trig words moved one byte later (file +0x899 -> RAM
    +0x89a).  Reads the file only."""
    if not 1 <= pattern <= 16:
        raise BankError("pattern must be 1..16 (got %d)" % pattern)
    with open(path, "rb") as fh:
        d = fh.read()
    if d[:4] != b"FORM" or d[8:16] != b"DPS1BANK":
        raise BankError("%s is not an Octatrack bank file (no FORM/DPS1BANK header)" % path)
    if len(d) < BANK_HEAD + pattern * PATTERN_STRIDE:
        raise BankError("%s is too short for pattern %d" % (path, pattern))
    out = {}
    for t in range(8):
        tr = trac_off(pattern - 1, t)
        blk = bytearray(d[tr:tr + RAM_TRAC_SIZE])
        blk[0x89A:0x91A] = d[tr + 0x899:tr + 0x919]
        blk[0x899] = d[tr + 0x898]
        out[t + 1] = blk
    return out


def read_bank(path, pattern):
    """The 8 audio tracks of pattern `pattern` (1..16) of a bank file, plus where the length is.

    Returns (tracks: {1..8: TrackData}, info) with info = {length, length_source, per_track,
    track_lengths}.  The file is opened for reading only."""
    if not 1 <= pattern <= 16:
        raise BankError("pattern must be 1..16 (got %d)" % pattern)
    with open(path, "rb") as fh:
        d = fh.read()
    if d[:4] != b"FORM" or d[8:16] != b"DPS1BANK":
        raise BankError("%s is not an Octatrack bank file (no FORM/DPS1BANK header)" % path)
    p = pattern - 1
    base = BANK_HEAD + p * PATTERN_STRIDE
    if len(d) < base + PATTERN_STRIDE:
        raise BankError("%s is too short for pattern %d" % (path, pattern))
    tracks = {}
    track_len = {}
    for t in range(8):
        tr = trac_off(p, t)
        td = decode_track(d, tr, 0x899)         # in the FILE the trig words sit one byte before RAM's
        track_len[t + 1] = d[tr + 0x50]
        tracks[t + 1] = td
    end = base + PATTERN_STRIDE              # the pattern's tail: RAM +0x8e50.. = end-12..
    mode = d[end - 7]
    info = {"per_track": mode == 1, "track_lengths": track_len, "pattern_length": d[end - 9],
            "length_source": None, "length": None}
    return tracks, info


def bank_letter(path):
    m = re.search(r"bank0*(\d+)", os.path.basename(path or ""), re.I)
    n = int(m.group(1)) if m else 1
    return chr(64 + n) if 1 <= n <= 16 else "A"


# ---- the card (read only) --------------------------------------------------------------------
# Case folding is ASCII only (a-z <-> A-Z), never Unicode: the firmware compares names that way
# ONE helper is used by the sort, `match`, `ext`, the first-folder lookup of
# `_resolve` and the pick-bag signature.
_FOLD = {c: c - 32 for c in range(97, 123)}


def fold(s):
    """ASCII upper case: a-z become A-Z, every other character (e-acute included) is unchanged."""
    return s.translate(_FOLD)


_LOWER = {c: c + 32 for c in range(65, 91)}


def fold_lower(s):
    """ASCII lower case (the spelling of `ext` words in a pick-bag signature)."""
    return s.translate(_LOWER)


def _sort_key(name):
    return (fold(name).encode("utf-8", "surrogateescape"), name.encode("utf-8", "surrogateescape"))


def canon_path(path):
    """The canonical form of a `dir` path: split on `/` and `\\`, `""` and `.` dropped,
    joined with `/`; a leading `/` is ignored (a path always stays inside the Set).  The case is the
    one the script wrote (the card is case-insensitive, the pick-bag signature folds it); a `..`
    component stays in the string, `_resolve` then gives an empty list.  This string is what
    `FileList.path` holds and what `static.load` stores in front of the file name."""
    return "/".join(p for p in re.split(r"[\\/]+", path) if p and p != ".")


def slot_path(folder, name):
    """The path stored in a slot: the list's canonical folder + `/` + the relative file name."""
    return folder + "/" + name if folder else name


def ext_key(name):
    """The extension of a (relative) file name, folded: `.WAV` -> `.WAV` whatever its case."""
    return fold(os.path.splitext(name)[1])


def _resolve(card, path):
    """card/path with an ASCII-case-insensitive match of every component (the first folder in sort
    order when several fold alike); None if absent or unsafe."""
    cur = card
    for part in canon_path(path).split("/"):
        if part == "":
            continue
        if part == "..":
            return None
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        hit = [n for n in names if fold(n) == fold(part) and os.path.isdir(os.path.join(cur, n))]
        if not hit:
            return None
        cur = os.path.join(cur, sorted(hit, key=_sort_key)[0])
    return cur if os.path.isdir(cur) else None


def list_card(card, path, deep=False):
    """Files of card/path: names relative to `path`, sorted case-insensitively (spec 2.1, 5.3.2).

    Names starting with '.' (including ._*) are ignored, directories are never listed, `deep`
    recurses (paths use '/') at most O.DIR_DEPTH_MAX folders below `path`: a file whose relative name has more
    '/' than that is not listed, nor counted anywhere (the unit's walker does not enter that folder)."""
    if card is None:
        return []
    root = _resolve(card, path)
    if root is None:
        return []
    out = []

    def walk(d, rel, level):                  # level 0 = the listed folder itself
        try:
            names = os.listdir(d)
        except OSError:
            return
        for n in names:
            if n.startswith("."):
                continue
            full = os.path.join(d, n)
            if os.path.isdir(full):
                if deep and level < O.DIR_DEPTH_MAX:
                    walk(full, rel + n + "/", level + 1)
            elif os.path.isfile(full):
                out.append(rel + n)

    walk(root, "", 0)
    return sorted(out, key=_sort_key)


def wildcard_match(pattern, name):
    """`*` matches any run of characters; everything else is literal; ASCII-case-insensitive."""
    rx = "".join(".*" if c == "*" else re.escape(fold(c)) for c in pattern)
    return re.fullmatch(rx, fold(name), re.S) is not None


def sig_hash(sig):
    """FNV-1a, 32 bits, over the UTF-8 bytes of a pick-bag signature."""
    h = 0x811C9DC5
    for b in sig.encode("utf-8", "surrogateescape"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


class FileList:
    """The current `dir` list of a function's frame."""

    def __init__(self, path="", deep=False, items=()):
        self.path, self.deep, self.items = path, deep, list(items)
        self.filters = []

    def copy(self):
        f = FileList(self.path, self.deep, self.items)
        f.filters = list(self.filters)
        return f

    @property
    def sig(self):
        """Identity of the list for the file.pick bag: folder (canonical, ASCII-folded), filters and
        size.  The bag is keyed by (function, sig_hash(sig))."""
        return "%s|%s|%s|%d" % (fold(self.path), "deep" if self.deep else "",
                                ",".join(self.filters), len(self.items))


# ---- rhythm helpers --------------------------------------------------------------------------
def euclid_hit(k, n, r, s):
    """1 if step s (1-based, wraps modulo n) plays in k hits over n steps rotated by r.

    Step i (0-based) fires when (i*k) mod n < k, then the pattern is rotated by r (the model of
    the reference model)."""
    if n <= 0 or k <= 0:
        return 0
    if k >= n:
        return 1
    i = (s - 1 - r) % n
    return 1 if (i * k) % n < k else 0


# Tables of dr.v (ours, not Teletype's; written 2026-10-04): velocity 0..127 of curve c at step s, 16 values per curve,
# step s reads entry (s - 1) mod 16 (so the curve repeats every 16 steps; s <= 0 wraps the same way).  A curve c outside
# 0..DRV_CURVES-1 reads curve 0 (no out-of-range report: dr.v is a pure value).  Curves 0..4 are the provisional
# formulas of before, unchanged; 5..7 are new.  firmware/c/vm.c DRV_TAB is the same table, byte for byte.
DRV_TABLE = (
    (100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100),   # 0 flat
    (127, 80, 80, 80, 127, 80, 80, 80, 127, 80, 80, 80, 127, 80, 80, 80),               # 1 accents on the beats
    (60, 60, 110, 60, 60, 60, 110, 60, 60, 60, 110, 60, 60, 60, 110, 60),               # 2 accents off the beats
    (16, 23, 30, 37, 44, 51, 58, 65, 72, 79, 86, 93, 100, 107, 114, 121),               # 3 ramp up
    (127, 120, 113, 106, 99, 92, 85, 78, 71, 64, 57, 50, 43, 36, 29, 22),               # 4 ramp down
    (120, 70, 120, 70, 120, 70, 120, 70, 120, 70, 120, 70, 120, 70, 120, 70),           # 5 eighths loud, sixteenths soft
    (127, 64, 64, 127, 64, 64, 127, 64, 127, 64, 64, 127, 64, 64, 127, 64),             # 6 tresillo 3+3+2 accents
    (32, 44, 56, 68, 80, 92, 104, 116, 127, 116, 104, 92, 80, 68, 56, 44),              # 7 swell: up then down
)
DRV_CURVES = len(DRV_TABLE)


def drv(c, s):
    if not 0 <= c < DRV_CURVES:
        c = 0
    return DRV_TABLE[c][(s - 1) % 16]


# ---- control flow ----------------------------------------------------------------------------
class Break(Exception):
    pass


class Stop(Exception):
    pass


class Ret(Exception):
    def __init__(self, val):
        Exception.__init__(self)
        self.val = val


class RunError(Exception):
    pass


class CardBusy(RunError):
    """A card access found the card busy (the storage task holds the FS mutex for ~2 s): the run
    stops, message exactly `CARD BUSY`.  Nothing already done is undone:
    `.result` is the Result of the run up to the access (model, draws, lists...)."""
    result = None


AUDIO_ERRORS = {
    -1: "BAD ARG",
    -2: "CARD BUSY",
    -3: "NO SOURCE",
    -4: "EMPTY",
    -5: "FLEX FULL",
    -6: "STOP FIRST",
    -7: "EXISTS",
    -8: "FORMAT",
    -9: "CARD FULL",
    -10: "IO ERROR",
    -11: "TIMEOUT",
    -12: "CAT FULL",
    -13: "TOO LONG",
}


class AudioError(RunError):
    def __init__(self, code, msg=None):
        self.code = code
        self.text = msg or AUDIO_ERRORS.get(code, "AUDIO ERROR")
        super().__init__(self.text)


class Ctx:
    """Tracks, steps, current step and the five streams of a block (spec 6.2 and 5.3.2).

    `copy` gives a block its own context: the lists are copied, the Stream objects are shared by
    reference (until a seed replaces them in the copy)."""

    def __init__(self, tracks, steps, streams):
        self.tracks, self.steps, self.step, self.streams = tracks, steps, 0, streams

    def copy(self):
        c = Ctx(list(self.tracks), list(self.steps), dict(self.streams))
        c.step = self.step
        return c


class Frame:
    def __init__(self, fn, args, files):
        self.fn, self.args, self.files = fn, args, files
        self.locals = {"A": 0, "B": 0, "C": 0, "D": 0}
        self.iters = []
        self.own = 0            # name slots of this frame's OWN `dir` base; a copy costs 0


FSTATE_DEFAULT = {"flip": 0, "o": 0, "o.min": 0, "o.max": 63, "o.wrap": 1, "o.inc": 1,
                  "drunk": 0, "drunk.min": 0, "drunk.max": 127, "drunk.wrap": 0,
                  "every": {}}

# ops whose core cannot exist yet / is unknown: the output marks them (not on the unit yet)
NOT_ON_UNIT = set()


def not_on_unit(op):
    return op.name in NOT_ON_UNIT or op.level == "unknown"


class Result:
    pass


HANDLERS = {}


def handler(*names):
    def deco(f):
        for n in names:
            HANDLERS[n] = f
        return f
    return deco


def load_bags(saved):
    """The `bags` of a state file: [{func, hash, n, rest}] oldest first, the malformed dropped,
    at most PICK_BAGS_MAX kept (the newest)."""
    out = []
    for b in saved if isinstance(saved, list) else []:
        try:
            out.append({"func": str(b["func"]), "hash": int(b["hash"]) & 0xFFFFFFFF, "n": int(b["n"]),
                        "rest": [int(x) for x in b["rest"]]})
        except (KeyError, TypeError, ValueError):
            continue
    return out[-O.PICK_BAGS_MAX:]


MAX_COST = 2000000        # a hard stop for a runaway run (the checker bounds real ones)


class Run:
    def __init__(self, prog, root, seed, seed_source, model, host_track, length, pattern, card,
                 state, params):
        self.prog, self.root = prog, root
        self.model = model
        self.host_track, self.length, self.pattern = host_track, length, pattern
        self.card = card
        self.draws = {n: 0 for n in STREAM_NAMES}
        self.cost = 0
        self.oor_list = []
        self.skip_list = []             # (function, op, line): a write skipped, no trig on the step
        self.pickup_list = []           # (function, op, line, track): a write refused by a PICKUP track
        self.refused_list = []          # (function, op, line, slot, path): a load the unit refused
        self.refuse = None              # predicate(canonical path) -> True: the unit refuses the load
        self.busy = None                # callable(op name) -> True: the card is busy for that access
        self.live = []                  # the frames of the tree walker (VMRun: its `frames`)
        self.warnings = []
        self.used_unit = set()
        self.params = params            # name -> value for the root function
        self.state = state if state is not None else {}
        self.globals = {k: 0 for k in "XYZT"}
        for k, v in self.state.get("globals", {}).items():
            if k.upper() in self.globals:
                self.globals[k.upper()] = sat(int(v))
        self.fstates = {}
        self.bags = load_bags(self.state.get("bags"))      # file.pick bags, oldest first
        self.seed, self.seed_source = seed, seed_source
        self.plays = []
        self.nx_smp = None
        self.nx_pl = {}
        self.tick = 0
        self.seq_running = False
        self.seq_steps = {}
        self.seq_mutes = {}
        self.fn_vars = {}
        self.saved_vars = copy.deepcopy(self.state.get("vars", {}))
        self.current_event = None
        self.midi_out = []
        self.outs = [0, 0, 0, 0]        # the four outputs `out n v` publishes
        self.gates = []                 # (tick, output) of each `gate n` of this run
        self.module_outs = None         # name -> [4 values] of the running scripts (cv), when known

    def record_gate(self, out):
        if 1 <= out <= 4:
            self.gates.append((self.tick, out))

    def set_out(self, out, v):
        if 1 <= out <= 4:
            self.outs[out - 1] = sat(v)
            if self.module_outs is not None:
                self.module_outs[self.root.name] = list(self.outs)

    def read_cv(self, name, out):
        """The value `out` of the running script `name` (0 if it does not run / out of range)."""
        if not (1 <= out <= 4) or not isinstance(name, str) or self.module_outs is None:
            return 0
        vals = self.module_outs.get(name.upper())
        return vals[out - 1] if vals else 0

    def record_play(self, t):
        if self.nx_smp is None and not self.nx_pl:
            self.plays.append((self.tick, t))
        else:
            self.plays.append((self.tick, t, self.nx_smp, dict(self.nx_pl)))
        self.nx_smp = None
        self.nx_pl = {}

    # ---- helpers -------------------------------------------------------------------------
    def fstate(self, fn):
        key = fn.name
        if key not in self.fstates:
            st = copy.deepcopy(FSTATE_DEFAULT)
            saved = self.state.get("funcs", {}).get(O.show(fn.name), {})
            for k, v in saved.items():
                if k == "every":
                    st[k] = {str(a): (list(b) if isinstance(b, list) else b) for a, b in v.items()}
                elif k in st:
                    st[k] = int(v)
            self.fstates[key] = st
        return self.fstates[key]

    # ---- the card --------------------------------------------------------------
    def live_frames(self):
        return self.live

    def card_access(self, n):
        """Every access to the card (`dir`, `static.load`, `static.clr`)
        first asks the busy hook; a busy card stops the run: `CARD BUSY`, nothing is undone."""
        if self.busy is not None and self.busy(n.op.name):
            if n.op.category == "AUDIO":
                raise AudioError(-2, "CARD BUSY")
            raise CardBusy("CARD BUSY")

    def load_slot(self, fr, n, slot, path):
        """The unit loads `path` into `slot`: True, or refuses: False.  A refusal CLEARS the slot,
        occupied or not (the unit calls a stock routine on the way out), and is recorded."""
        if self.refuse is not None and self.refuse(path):
            self.model.slots.pop(slot, None)
            self.refused_list.append((O.show(fr.fn.name), n.op.shown, n.line, slot, path))
            return False
        self.model.slots[slot] = path
        return True

    def new_streams(self, seed):
        return {n: Stream(n, seed, self.draws) for n in STREAM_NAMES}

    def oor(self, op, line, fr, what=None):
        self.oor_list.append((O.show(fr.fn.name), what or op.shown, line))

    def lands(self, td, s, n, fr):
        """May a lock / trig-word write land on step s of td?  Only on a step that carries a trig
        of any kind; otherwise the write is skipped and counted (apart from out-of-range)."""
        if td.has_any_trig(s):
            return True
        self.skip_list.append((O.show(fr.fn.name), n.op.shown, n.line))
        return False

    def charge(self, units):
        self.cost += units
        if self.cost > MAX_COST:
            raise RunError("run too long (more than %d units)" % MAX_COST)

    def below(self, ctx, stream, n):
        return ctx.streams[stream].below(n)

    # ---- running -------------------------------------------------------------------------
    def run(self):
        self.model.cat_list.clear()
        fn = self.root
        steps = list(range(1, self.length + 1))
        ctx = Ctx([self.host_track], steps, self.new_streams(self.seed))
        self.run_function(fn, [], ctx, None)

    def run_function(self, fn, args, ctx, caller):
        """A called function gets a COPY of the caller's context (its streams are shared by
        reference), fresh locals and a copy of the caller's file list.  Value: `ret v`, else 0."""
        fr = Frame(fn, dict(zip(fn.args, args)), caller.files.copy() if caller else FileList())
        self.live.append(fr)
        try:
            self.exec_block(fn.body, ctx.copy(), fr)
        except Stop:
            pass
        except Ret as r:
            return r.val
        finally:
            self.live.pop()             # its own `dir` base goes back to the shared budget
        return 0

    def call(self, n, ctx, fr):
        fn = self.prog.fmap[n.name]
        args = [self.eval(a, ctx, fr) for a in n.args]
        return self.run_function(fn, args, ctx, fr)

    # ---- blocks, lines, modifiers --------------------------------------------------------
    def exec_block(self, lines, ctx, fr):
        """`ctx` belongs to this block: a statement form (trk 3) changes it until the end."""
        taken = None
        for idx, ln in enumerate(lines):
            if ln.decl is not None or ln.error:
                continue
            if ln.in_block is not None:
                ib = ln.in_block
                ev = getattr(self, "current_event", None)
                if ib.kind == "GATE":           # a gate block needs a gate event, event or not
                    if not (isinstance(ev, dict) and ev.get("type") == "GATE") \
                            or ev.get("gate.name") != ib.name or ev.get("gate.out") != ib.ch:
                        continue
                elif ev is not None:
                    if ib.kind == "CLOCK":
                        if ev.get("type") != "CLOCK":
                            continue
                    elif ib.kind == "NOTE":
                        if not (isinstance(ev, dict) and ev.get("type") == "NOTE"):
                            continue
                        if ib.ch is not None and ev.get("ch") != ib.ch:
                            continue
                    elif ib.kind == "CC":
                        if not (isinstance(ev, dict) and ev.get("type") == "CC"):
                            continue
                        if ib.ch is not None and ev.get("ch") != ib.ch:
                            continue
                        if ib.num is not None and ev.get("num") != ib.num:
                            continue
                    elif ib.kind == "TRIG":
                        if not (isinstance(ev, dict) and ev.get("type") == "TRIG"):
                            continue
                        if ev.get("track") != ib.ch:
                            continue
                        g = ev.get("trig.genre", 0)
                        if ib.num == 2:
                            if g != 2:
                                continue
                        elif ib.num == 1:
                            if g != 0:
                                continue
                        elif ev.get("trig.hit") != 1:
                            continue
                self.exec_block(ln.children, ctx.copy(), fr)
                continue
            fk = ln.first_kind()
            if fk in ("IF", "ELIF", "ELSE"):
                lctx = ctx.copy()
                if fk == "ELSE":
                    go = not taken
                    taken = True
                elif fk == "ELIF" and taken:
                    go = False
                else:
                    go = self.eval(ln.mods[0].args[0], lctx, fr) != 0
                    taken = go if fk == "IF" else (taken or go)
                if go:
                    self.exec_mods(ln, 1, lctx, fr)
            else:
                taken = None
                if self.is_trk_stmt(ln):
                    self.charge(O.OPS["TRK"].cost)
                    ts = self.trk_list(ln.stmt.node, ctx, fr)
                    if len(ts) == 1:
                        ctx.tracks = ts
                        continue
                    rest = lines[idx + 1:]       # the rest of the block runs once per track
                    self.per_track(ts, ctx, lambda c: self.exec_block(rest, c, fr))
                    return
                self.exec_mods(ln, 0, ctx if not ln.mods else ctx.copy(), fr)

    @staticmethod
    def is_trk_stmt(ln):
        return (not ln.mods and ln.stmt is not None and ln.stmt.kind == "ctxstmt"
                and ln.stmt.node.op.name == "TRK")

    def exec_mods(self, ln, idx, ctx, fr):
        if idx >= len(ln.mods):
            if ln.stmt is not None:
                self.exec_stmt(ln.stmt, ctx, fr)
            else:
                self.exec_block(ln.children, ctx.copy(), fr)
            return
        m = ln.mods[idx]
        k = m.kind
        if k in ("IF", "ELIF"):
            if self.eval(m.args[0], ctx, fr):
                self.exec_mods(ln, idx + 1, ctx, fr)
        elif k == "PROB":
            p = clamp(self.eval(m.args[0], ctx, fr), 0, 100)
            if self.below(ctx, "PROB", 100) < p:
                self.exec_mods(ln, idx + 1, ctx, fr)
        elif k in ("EVERY", "SKIP"):
            n = self.eval(m.args[0], ctx, fr)
            fires = self.every_fires(fr, ln, idx, n)
            if fires == (k == "EVERY"):
                self.exec_mods(ln, idx + 1, ctx, fr)
        elif k == "L":
            a = self.eval(m.args[0], ctx, fr)
            b = self.eval(m.args[1], ctx, fr)
            seq = range(a, b + 1) if a <= b else range(a, b - 1, -1)
            self.loop(ln, idx, ctx, fr, seq)
        elif k == "EACH":
            self.loop(ln, idx, ctx, fr, list(ctx.steps), each=True)
        elif k == "CTX" and m.node.op.name == "TRK":
            self.charge(m.node.op.cost)
            self.per_track(self.trk_list(m.node, ctx, fr), ctx,
                           lambda c: self.exec_mods(ln, idx + 1, c, fr))
        elif k == "CTX":
            self.exec_ctx(m.node, ctx, fr)
            self.exec_mods(ln, idx + 1, ctx, fr)
        else:
            raise RunError("modifier %s is not available" % k.lower())

    def loop(self, ln, idx, ctx, fr, seq, each=False):
        for i in seq:
            fr.iters.append(i)
            c = ctx.copy()
            if each:
                c.steps, c.step = [i], i
            try:
                self.exec_mods(ln, idx + 1, c, fr)
            except Break:
                break
            finally:
                fr.iters.pop()

    def every_fires(self, fr, ln, idx, n):
        """The counter of this line (and modifier) is incremented on every pass; the line fires
        when it reaches n, and the counter restarts at 0: `every n` fires on the n-th pass.
        n < 1: `every` never fires (and `skip` always does)."""
        key = str(ln.no) if idx == 0 else "%d.%d" % (ln.no, idx)
        ev = self.fstate(fr.fn)["every"]
        c = ev.get(key, 0) + 1
        if n >= 1 and c >= n:
            ev[key] = 0
            return True
        ev[key] = c if n >= 1 else 0
        return False

    # ---- statements ----------------------------------------------------------------------
    def exec_stmt(self, s, ctx, fr):
        k = s.kind
        if k == "assign":
            v = self.eval(s.args[0], ctx, fr)
            if s.name in "ABCD":
                fr.locals[s.name] = v
            else:
                self.globals[s.name] = v
        elif k == "assign_var_named":
            v = self.eval(s.args[0], ctx, fr)
            vars_dict = self.fn_vars.setdefault(fr.fn, {})
            slot = getattr(s.var_info, "slot", None)
            val = max(-32768, min(32767, v))
            vars_dict[s.name] = val
            if slot is not None:
                vars_dict[slot] = val
        elif k == "opstmt":
            self.eval_op(s.node, ctx, fr)
        elif k == "callstmt":
            self.call(s.node, ctx, fr)
        elif k == "ctxstmt":
            self.exec_ctx(s.node, ctx, fr)
        elif k == "break":
            raise Break()
        elif k == "stop":
            raise Stop()
        elif k == "ret":
            raise Ret(self.eval(s.args[0], ctx, fr) if s.args else 0)
        elif k == "setstmt":
            self.charge(1)
            vals = [self.eval(a, ctx, fr) for a in s.args]
            self.set_state(s.op, vals, ctx, fr, line=s.line)
        else:
            raise RunError("statement %s is not supported" % k)

    # ---- expressions ---------------------------------------------------------------------
    def eval(self, n, ctx, fr):
        k = n.kind
        if k == "num":
            return n.val
        if k == "str":
            return n.val
        if k == "list":
            return tuple(n.val)
        if k == "cond":
            return cond_code(n.val)
        if k == "var":
            w = n.name
            if w in "ABCD":
                return fr.locals[w]
            if w in "XYZT":
                return self.globals[w]
            it = fr.iters
            if w == "I":
                return it[-1] if it else 0
            return it[-2] if len(it) > 1 else 0
        if k == "var_named":
            vars_dict = self.fn_vars.setdefault(fr.fn, {})
            slot = getattr(n.var_info, "slot", None)
            if slot is not None and slot in vars_dict:
                return vars_dict[slot]
            if n.name in vars_dict:
                return vars_dict[n.name]
            fn_key = O.show(fr.fn.name)
            saved = getattr(self, "saved_vars", {}).get(fn_key)
            if saved is None:
                saved = getattr(self, "saved_vars", {}).get(str(fr.fn.name).upper(), {})
            if slot is not None and slot in saved:
                val = saved[slot]
            elif slot is not None and str(slot) in saved:
                val = saved[str(slot)]
            elif n.name in saved:
                val = saved[n.name]
            elif n.name.lower() in saved:
                val = saved[n.name.lower()]
            elif n.name.upper() in saved:
                val = saved[n.name.upper()]
            else:
                val = n.var_info.init
            vars_dict[n.name] = val
            if slot is not None:
                vars_dict[slot] = val
            return val
        if k == "arg":
            return fr.args[n.name]
        if k == "param":
            return self.param_value(fr.fn, n.name)
        if k == "const":
            c = self.prog.consts[n.name]
            if c.type == "COND":
                return cond_code(c.val)
            return tuple(c.val) if c.type == "LIST" else c.val
        if k in ("kw", "pred"):
            return n.name
        if k == "op":
            return self.eval_op(n, ctx, fr)
        if k == "call":
            return self.call(n, ctx, fr)
        raise RunError("expression %s is not supported" % k)

    def param_value(self, fn, name):
        p = fn.params[name]
        d = p[0]
        if fn is self.root and name in self.params:
            return self.params[name]
        if getattr(p, "kind", "INT") == "FOLDER":
            return p.path
        return d

    def eval_op(self, n, ctx, fr):
        op = n.op
        self.charge(op.cost)
        if op.family == O.CTX:
            return self.exec_ctx(n, ctx, fr, charged=True)
        if op.lazy:
            c = self.eval(n.args[0], ctx, fr)
            return self.eval(n.args[1] if c else n.args[2], ctx, fr)
        vals = [self.eval(a, ctx, fr) for a in n.args]
        return self.call_op(op, vals, n.kinds, ctx, fr, n)

    def call_op(self, op, vals, kinds, ctx, fr, n):
        """Run one registry operation on already evaluated arguments (shared with the VM,
        modules/otype/vm.py): a constant, the range of every argument, the handler.  `n` is what
        a handler reads of its call site: `.op` and `.line` (a tree node, or vm.Site)."""
        if op.value is not None:
            return op.value
        # the range of every argument (spec 8.1): out of range = skipped and counted
        bad = False
        for i, v in enumerate(vals):
            kind = kinds[i] if i < len(kinds) else None
            if kind == O.PCT:
                vals[i] = clamp(v, 0, 100)
            elif kind in O.RANGES and not O.RANGES[kind][0] <= v <= O.RANGES[kind][1]:
                bad = True
            elif kind == O.COND and not 0 <= v <= 64:
                bad = True
        if bad:
            self.oor(op, n.line, fr)
            return 0
        if not_on_unit(op):
            self.used_unit.add(op.shown)
        h = HANDLERS.get(op.name)
        if h is None:
            raise RunError("operation %s has no simulation" % op.shown)
        r = h(self, ctx, fr, n, vals)
        return 0 if r is None else r

    # ---- context ops (trk, steps, step, seed, x.seed) ------------------------------------
    def trk_list(self, n, ctx, fr):
        """The tracks named by a `trk` node, in order, de-duplicated; a bad number is dropped and
        counted.  `cur` = the track being processed, `all` = 1..8."""
        a0 = n.args[0]
        if a0.kind == "kw":
            return [self.cur_track(ctx)] if a0.name == "CUR" else list(range(1, 9))
        return self.tracks_from([self.eval(a, ctx, fr) for a in n.args], n.op, n.line, fr)

    def tracks_from(self, vals, op, line, fr):
        """The valid, de-duplicated track numbers among `vals` (the others are counted)."""
        ts = []
        for v in vals:
            if not 1 <= v <= 8:
                self.oor(op, line, fr)
            elif v not in ts:
                ts.append(v)
        return ts

    def per_track(self, tracks, ctx, body):
        """THE multi-track rule (spec 6.2): `body(c)` runs once per track, in `trk` order, with a
        context `c` of that single track (its own step set).  break / stop / ret end everything."""
        for t in tracks:
            c = ctx.copy()
            c.tracks = [t]
            body(c)

    def exec_ctx(self, n, ctx, fr, charged=False):
        op = n.op
        if not charged:
            self.charge(op.cost)
        name = op.name
        if name == "TRK":
            ctx.tracks = self.trk_list(n, ctx, fr)
        elif name == "STEPS":
            a0 = n.args[0]
            if a0.kind == "kw":
                self.steps_all(ctx)
            elif a0.kind == "pred":
                self.steps_pred(ctx, a0.op)
            else:
                a, b = (self.eval(x, ctx, fr) for x in n.args)
                self.steps_range(op, ctx, fr, n.line, a, b)
        elif name == "STEP":
            self.step_set(op, ctx, fr, n.line, self.eval(n.args[0], ctx, fr))
        elif name == "SEED":
            self.seed_all(ctx, self.eval(n.args[0], ctx, fr))
        elif name.endswith(".SEED"):
            self.seed_one(ctx, name[:-5], self.eval(n.args[0], ctx, fr))
        else:
            raise RunError("context operation %s has no simulation" % op.shown)
        return 0

    # the effect of each context operation on a context (shared with the VM)
    def steps_all(self, ctx):
        ctx.steps = list(range(1, self.length + 1))

    def steps_pred(self, ctx, pop):
        t = self.cur_track(ctx)           # the predicate looks at the track being processed
        ctx.steps = [s for s in ctx.steps if self.read_pred(pop, t, s)]

    def steps_range(self, op, ctx, fr, line, a, b):
        if not (1 <= a <= 64 and 1 <= b <= 64):
            self.oor(op, line, fr)
        else:
            ctx.steps = list(range(min(a, b), max(a, b) + 1))      # absolute, replaces

    def step_set(self, op, ctx, fr, line, s):
        if not 1 <= s <= 64:
            self.oor(op, line, fr)
        else:
            ctx.steps, ctx.step = [s], s

    def seed_all(self, ctx, v):
        ctx.streams = self.new_streams(v)

    def seed_one(self, ctx, stream, v):
        ctx.streams[stream] = Stream(stream, v, self.draws)

    # ---- state operators (spec 5.2.1) ----------------------------------------------------
    def set_state(self, op, v, ctx=None, fr=None, line=0):
        if fr is None and isinstance(ctx, Frame):
            fr = ctx
            ctx = getattr(fr, "ctx", None)
        if isinstance(v, (int, bool)):
            v = [int(v)]
        tracks = ctx.tracks if ctx else [self.host_track]
        nm = op.name
        if nm == "FLIP":
            st = self.fstate(fr.fn) if fr else {}
            st["flip"] = 1 if v[0] else 0
        elif nm == "O":
            st = self.fstate(fr.fn) if fr else {}
            st["o"] = clamp(v[0], st.get("o.min", 0), max(st.get("o.min", 0), st.get("o.max", 127)))
        elif nm == "DRUNK":
            st = self.fstate(fr.fn) if fr else {}
            st["drunk"] = clamp(v[0], st.get("drunk.min", 0), max(st.get("drunk.min", 0), st.get("drunk.max", 127)))
        elif nm == "TEMPO":
            self.model.tempo = clamp(v[0], TEMPO_MIN, TEMPO_MAX)
        elif nm == "TRK.TYPE":
            for t in tracks:
                if not 0 <= v[0] <= 4 or (v[0] == 3 and t in (1, 5)):
                    self.oor(op, line, fr)
                else:
                    self.model.part[t].mtype = v[0]
        elif nm == "TRK.SLOT":
            for t in tracks:
                mt = self.model.part[t].mtype
                if not 1 <= v[0] <= 128 or mt > 1 or (mt == 1 and v[0] not in self.model.pmodel.flex_slots):
                    self.oor(op, line, fr)
                else:
                    self.model.part[t].slot = v[0]
        elif nm == "TRK.LEVEL":
            for t in tracks:
                self.model.part[t].level = clamp(v[0], 0, 127)
        elif nm == "TRK.CUE":
            for t in tracks:
                self.model.part[t].cue = clamp(v[0], 0, 127)
        elif nm == "TRK.MUTE":
            for t in tracks:
                self.model.mutes[t] = 1 if v[0] else 0
        elif nm == "TRK.SWING":
            for t in tracks:
                self.model.tracks[t].swing = clamp(v[0], 50, 80)
        elif nm == "FILL":
            self.model.fill = 1 if v[0] else 0
            if not v[0]:
                self.model.fill_once = 0
        elif nm in ("PB.SETUP", "AMP.SETUP", "FX1.SETUP", "FX2.SETUP"):
            if not (1 <= v[0] <= 6):
                self.oor(op, line, fr)
            else:
                for t in tracks:
                    rg = self.model.pmodel.setup_range(t, nm, v[0])
                    if rg is None:
                        self.oor(op, line, fr)
                    else:
                        getattr(self.model.part[t], nm.lower().replace('.', '_'))[v[0]] = clamp(v[1], *rg)
        elif nm == "LFO.WAVE":
            waves = self.model.pmodel.waves()
            if not (1 <= v[0] <= 3) or not waves:
                self.oor(op, line, fr)
            else:
                for t in tracks:
                    self.model.part[t].lfo_wave[v[0]] = clamp(v[1], 0, waves - 1)
        elif nm == "LFO.MULT":
            if not (1 <= v[0] <= 3):
                self.oor(op, line, fr)
            else:
                for t in tracks:
                    self.model.part[t].lfo_mult[v[0]] = clamp(v[1], 0, 6)
        elif nm == "LFO.TRIG":
            if not (1 <= v[0] <= 3):
                self.oor(op, line, fr)
            else:
                for t in tracks:
                    self.model.part[t].lfo_trig[v[0]] = clamp(v[1], 0, 7)

    @staticmethod
    def walk(v, lo, hi, wrap):
        """v brought into lo..hi: wrapped around or stopped at the bound."""
        if hi < lo:
            hi = lo
        if lo <= v <= hi:
            return v
        if wrap:
            return lo + (v - lo) % (hi - lo + 1)
        return clamp(v, lo, hi)

    def read_state(self, op, ctx, fr):
        st = self.fstate(fr.fn)
        if op.name == "FLIP":
            v = st["flip"]
            st["flip"] = 1 - v
            return v
        if op.name == "O":
            v = st["o"]
            st["o"] = self.walk(v + st["o.inc"], st["o.min"], st["o.max"], st["o.wrap"])
            return v
        step = self.below(ctx, "DRUNK", 3) - 1
        v = st["drunk"]
        st["drunk"] = self.walk(v + step, st["drunk.min"], st["drunk.max"], st["drunk.wrap"])
        return v

    # ---- reads on the model --------------------------------------------------------------
    def cur_track(self, ctx):
        """The track being processed (a multi-track `trk` runs its body once per track)."""
        return ctx.tracks[0] if ctx.tracks else self.host_track

    def first_track(self, ctx):
        return self.model.tracks[self.cur_track(ctx)]

    def read_pred(self, op, t, s):
        td = self.model.tracks[t]
        nm = op.name
        if nm == "HAS.TRIG":
            return td.has_trig(s)
        if nm == "HAS.LOCK":
            return td.has_lock(s)
        if nm == "HAS.SMP":
            return s in td.smp
        return td.is_empty(s)

    # ---- per-step helpers ----------------------------------------------------------------
    def each_track(self, ctx, n, fr):
        """The tracks a WRITE handler visits.  A PICKUP track refuses every write: it is skipped
        before any of its steps is visited (so it draws nothing) and counted once per execution."""
        for t in ctx.tracks:
            if self.model.part[t].mtype == PICKUP:
                self.pickup_list.append((O.show(fr.fn.name), n.op.shown, n.line, t))
                continue
            yield t, self.model.tracks[t]

    def each_step(self, ctx, n, fr):
        for t, td in self.each_track(ctx, n, fr):
            for s in ctx.steps:
                yield t, td, s


# ---- the operation handlers ------------------------------------------------------------------
def _lang(name, fn):
    HANDLERS[name] = lambda run, ctx, fr, n, v, _f=fn: _f(*v)


def _div(a, b):
    if b == 0:
        return 0
    q = abs(a) // abs(b)
    return sat(q if (a < 0) == (b < 0) else -q)


def _mod(a, b):
    return 0 if b == 0 else a % abs(b)


def _wrap(x, lo, hi):
    if hi < lo:
        lo, hi = hi, lo
    return lo + (x - lo) % (hi - lo + 1)


_lang("ADD", lambda a, b: sat(a + b))
_lang("SUB", lambda a, b: sat(a - b))
_lang("MUL", lambda a, b: sat(a * b))
_lang("DIV", _div)
_lang("MOD", _mod)
_lang("MIN", min)
_lang("MAX", max)
_lang("ABS", lambda a: sat(abs(a)))
_lang("NEG", lambda a: sat(-a))
_lang("NOT", lambda a: int(a == 0))
_lang("LIM", lambda x, lo, hi: clamp(x, min(lo, hi), max(lo, hi)))
_lang("WRAP", _wrap)
_lang("EQ", lambda a, b: int(a == b))
_lang("NE", lambda a, b: int(a != b))
_lang("LT", lambda a, b: int(a < b))
_lang("GT", lambda a, b: int(a > b))
_lang("LE", lambda a, b: int(a <= b))
_lang("GE", lambda a, b: int(a >= b))
_lang("AND", lambda a, b: int(a != 0 and b != 0))
_lang("OR", lambda a, b: int(a != 0 or b != 0))
_lang("XOR", lambda a, b: int((a != 0) != (b != 0)))
_lang("EZ", lambda a: int(a == 0))
_lang("NZ", lambda a: int(a != 0))
_lang("AT", lambda lst, i: lst[clamp(i, 1, len(lst)) - 1])
_lang("LEN", len)
_lang("ER", lambda k, n, s: euclid_hit(k, n, 0, s))
_lang("ER.R", lambda k, n, r, s: euclid_hit(k, n, r, s))
_lang("DR.V", drv)


@handler("RND")
def _rnd(run, ctx, fr, n, v):
    return run.below(ctx, "RAND", v[0])


@handler("RRAND")
def _rrand(run, ctx, fr, n, v):
    lo, hi = min(v), max(v)
    return lo + run.below(ctx, "RAND", hi - lo + 1)


@handler("TOSS")
def _toss(run, ctx, fr, n, v):
    return ctx.streams["TOSS"].toss()


@handler("CHANCE")
def _chance(run, ctx, fr, n, v):
    return int(run.below(ctx, "TOSS", 100) < v[0])


@handler("PICK")
def _pick(run, ctx, fr, n, v):
    return v[0][run.below(ctx, "RAND", len(v[0]))]


@handler("PB", "LFO", "AMP", "FX1", "FX2")
def _pagebase(run, ctx, fr, n, v):
    base = {"PB": 0, "LFO": 6, "AMP": 12, "FX1": 18, "FX2": 24}[n.op.name]
    if not 1 <= v[0] <= 6:
        run.oor(n.op, n.line, fr)
        return -1                 # an invalid parameter: its user is skipped and counted
    return base + v[0] - 1


@handler("LFO.SPD")
def _lfospd(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 3):
        run.oor(n.op, n.line, fr)
        return -1
    return 5 + v[0]


@handler("LFO.DEP")
def _lfodep(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 3):
        run.oor(n.op, n.line, fr)
        return -1
    return 8 + v[0]


@handler("FLIP", "O", "DRUNK")
def _state_read(run, ctx, fr, n, v):
    return run.read_state(n.op, ctx, fr)


def _setter(key):
    def h(run, ctx, fr, n, v):
        st = run.fstate(fr.fn)
        st[key] = v[0]
        if key in ("o.wrap", "drunk.wrap"):
            st[key] = 1 if v[0] else 0
        elif key.startswith("o.") and key != "o.inc":
            st["o"] = clamp(st["o"], st["o.min"], max(st["o.min"], st["o.max"]))
        elif key.startswith("drunk.") and key != "drunk.wrap":
            st["drunk"] = clamp(st["drunk"], st["drunk.min"], max(st["drunk.min"], st["drunk.max"]))
    return h


for _k in ("o.min", "o.max", "o.wrap", "o.inc", "drunk.min", "drunk.max", "drunk.wrap"):
    HANDLERS[_k.upper()] = _setter(_k)


@handler("TRK.CUR")
def _trkcur(run, ctx, fr, n, v):
    return run.cur_track(ctx)


@handler("PAT.LEN")
def _patlen(run, ctx, fr, n, v):
    return run.length


@handler("PAT.CUR")
def _patcur(run, ctx, fr, n, v):
    return run.model.pattern


@handler("BANK.CUR")
def _bankcur(run, ctx, fr, n, v):
    return run.model.bank


@handler("PAT.GO")
def _patgo(run, ctx, fr, n, v):
    p = v[0]
    b = v[1] if len(v) > 1 and v[1] != 0 else run.model.bank
    if not (1 <= p <= 16 and 1 <= b <= 16):
        run.oor(n.op, n.line, fr)
    else:
        run.model.pattern_lengths[run.model.bank, run.model.pattern] = run.length
        run.model.go_pattern(b, p)
        run.length = run.model.pattern_lengths.get((b, p), 16)


@handler("PAT.COPY")
def _patcopy(run, ctx, fr, n, v):
    """The stock paste: the whole pattern record of `a` replaces `b`'s (trigs, locks, length, part assigned);
    the clipboard and the parts' data are untouched. a == b is a refused argument (the stock would write the
    pattern onto itself), not an out-of-range one."""
    a, b = v[0], v[1]
    m = run.model
    if not (1 <= a <= 16 and 1 <= b <= 16):
        run.oor(n.op, n.line, fr)
    elif a == b:
        raise RunError("pat.copy: source and destination are the same pattern (%d)" % a)
    else:
        m.pattern_lengths[m.bank, m.pattern] = run.length       # the playing pattern keeps the length it has now
        m.patterns[m.bank, m.pattern] = m.tracks
        m.parts[m.bank, m.part_cur] = m.part
        m.patterns[m.bank, b] = copy.deepcopy(m.patterns.setdefault((m.bank, a), {t: TrackData() for t in range(1, 9)}))
        if (m.bank, a) in m.pattern_lengths:
            m.pattern_lengths[m.bank, b] = m.pattern_lengths[m.bank, a]
        else:
            m.pattern_lengths.pop((m.bank, b), None)
        if (m.bank, a) in m.part_links:                        # the part assigned to the pattern travels
            m.part_links[m.bank, b] = m.part_links[m.bank, a]
        if m.pattern == b:                                     # pasted onto the playing pattern: it is read now
            m.tracks = m.patterns[m.bank, b]
            run.length = m.pattern_lengths.get((m.bank, b), 16)
            m.part_cur = m.part_links.get((m.bank, b), 1)
            m.part = m.parts.setdefault((m.bank, m.part_cur), {t: PartData() for t in range(1, 9)})


@handler("PART.CUR")
def _partcur(run, ctx, fr, n, v):
    return run.model.part_cur


@handler("PART.GO")
def _partgo(run, ctx, fr, n, v):
    p = v[0]
    if not (1 <= p <= 4):
        run.oor(n.op, n.line, fr)
    else:
        run.model.go_part(p)


@handler("PART.SAVE")
def _partsave(run, ctx, fr, n, v):
    p = v[0] if v and v[0] != 0 else run.model.part_cur
    if not (1 <= p <= 4):
        run.oor(n.op, n.line, fr)
    else:
        target = run.model.part if p == run.model.part_cur else run.model.parts.setdefault(
            (run.model.bank, p), {t: PartData() for t in range(1, 9)})
        run.model.saved_parts[run.model.bank, p] = copy.deepcopy(target)


@handler("PART.RELOAD")
def _partreload(run, ctx, fr, n, v):
    p = v[0] if v and v[0] != 0 else run.model.part_cur
    if not (1 <= p <= 4):
        run.oor(n.op, n.line, fr)
    elif (run.model.bank, p) not in run.model.saved_parts:
        raise RunError("part %d has never been saved" % p)
    else:
        target = copy.deepcopy(run.model.saved_parts[run.model.bank, p])
        run.model.parts[run.model.bank, p] = target
        if p == run.model.part_cur:
            run.model.part = target


@handler("STEP.CUR")
def _stepcur(run, ctx, fr, n, v):
    return ctx.step


@handler("TRK.TYPE")
def _trktype(run, ctx, fr, n, v):
    return run.model.part[run.cur_track(ctx)].mtype


@handler("HAS.TRIG", "HAS.LOCK", "HAS.SMP", "IS.EMPTY")
def _preds(run, ctx, fr, n, v):
    return int(bool(run.read_pred(n.op, run.cur_track(ctx), v[0])))


# TR / TL --------------------------------------------------------------------------------------
@handler("TR.ADD")
def _tradd(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.add_trig(v[0])


@handler("TR.DEL")
def _trdel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.remove_trig(v[0])


@handler("TR.FLIP")
def _trflip(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        if td.has_trig(v[0]):
            td.remove_trig(v[0])
        else:
            td.add_trig(v[0])


@handler("TR.CLR")
def _trclr(run, ctx, fr, n, v):
    for t, td, s in run.each_step(ctx, n, fr):
        td.remove_trig(s)


@handler("TL.ADD")
def _tladd(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.add_tlock(v[0])


@handler("TL.FILL")
def _tlfill(run, ctx, fr, n, v):
    for t, td, s in run.each_step(ctx, n, fr):
        hit = run.below(ctx, n.op.stream, 100) < v[0]
        if hit and not td.has_trig(s) and not td.has(TL_MASK, s):    # free steps only
            td.add_tlock(s)


@handler("TL.DEL")
def _tldel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        s = v[0]
        td.put(TL_MASK, s, False)
        if not td.has_trig(s):
            td.locks.pop(s, None)
            td.smp.pop(s, None)


@handler("TT.ADD")
def _ttadd(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        s = v[0]
        td.put("trig", s, False)
        td.put("oneshot", s, False)
        td.put("tless", s, True)


@handler("TT.DEL")
def _ttdel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.put("tless", v[0], False)


@handler("OS.ADD")
def _osadd(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        s = v[0]
        td.put("trig", s, False)
        td.put("tless", s, False)
        td.put("oneshot", s, True)


@handler("OS.DEL")
def _osdel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.put("oneshot", v[0], False)


@handler("OS.ARM")
def _osarm(run, ctx, fr, n, v):
    for t in ctx.tracks:
        run.model.tracks[t].os_armed = 1


@handler("SW.ADD")
def _swadd(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.put("swing", v[0], True)


@handler("SW.DEL")
def _swdel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.put("swing", v[0], False)


@handler("SW.FLIP")
def _swflip(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.put("swing", v[0], not td.has("swing", v[0]))


@handler("SL.ADD")
def _sladd(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.put("slide", v[0], True)


@handler("SL.DEL")
def _sldel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.put("slide", v[0], False)


@handler("SL.FLIP")
def _slflip(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.put("slide", v[0], not td.has("slide", v[0]))


@handler("TRK.SWING")
def _trkswing(run, ctx, fr, n, v):
    return run.model.tracks[run.cur_track(ctx)].swing


_REC_SRC_MASKS = ("rec1", "rec2", "rec3")        # TRAC +0x20 (AB), +0x28 (CD), +0x30 (SRC3)


def _rec_src(td, s, src):
    td.put("rec1", s, bool(src & 1))
    td.put("rec2", s, bool(src & 2))
    td.put("rec3", s, bool(src & 4))


def _rec_del(td, s):
    """The stock grid writer's release on one step: the three source masks, the one-shot flag (+0x38) and
    the recorder micro-timing byte (+0x859, not modelled here: no operation of OType sets it) go.  The
    playback trig word of the step, its micro-timing included, is NOT touched."""
    for m in _REC_SRC_MASKS + ("m38",):
        td.put(m, s, False)


@handler("RT.ADD")
def _rtadd(run, ctx, fr, n, v):
    if not 1 <= v[1] <= 7:                      # a recorder trig always listens to something
        run.oor(n.op, n.line, fr)
        return
    for t, td in run.each_track(ctx, n, fr):
        _rec_src(td, v[0], v[1])


@handler("RT.DEL")
def _rtdel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        _rec_del(td, v[0])


@handler("RT.CLR")
def _rtclr(run, ctx, fr, n, v):
    for t, td, s in run.each_step(ctx, n, fr):
        _rec_del(td, s)


@handler("RT.SRC")
def _rtsrc(run, ctx, fr, n, v):
    if not 1 <= v[1] <= 7:
        run.oor(n.op, n.line, fr)
        return
    for t, td in run.each_track(ctx, n, fr):
        _rec_src(td, v[0], v[1])


@handler("RO.ADD")
def _roadd(run, ctx, fr, n, v):
    if not 1 <= v[1] <= 7:                      # as rt.add: a recorder trig always listens to something
        run.oor(n.op, n.line, fr)
        return
    for t, td in run.each_track(ctx, n, fr):
        _rec_src(td, v[0], v[1])
        td.put("m38", v[0], True)               # TRAC +0x38: the one shot state of the recorder trig


@handler("RO.DEL")
def _rodel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        _rec_del(td, v[0])



# PL -------------------------------------------------------------------------------------------
def _lock_range(run, t, p, n, fr):
    """(lo, hi) of the values a lock of parameter p may take on track t, from the part's descriptor
    (`otb_host_param_info` on the unit), or None: the part DISABLES that slot, the stock store refuses it;
    the refusal is counted as out of range (once per track and call, before any step is visited: no draw)."""
    lo, count, enabled = run.model.pmodel.param_info(t, p)
    if not enabled:
        run.oor(n.op, n.line, fr)
        return None
    return lo, lo + count - 1


@handler("PL.SET")
def _plset(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        rg = _lock_range(run, t, v[1], n, fr)
        if rg is not None and run.lands(td, v[0], n, fr):
            td.lock_set(v[0], v[1], clamp(v[2], rg[0], rg[1]))


@handler("PL.GET")
def _plget(run, ctx, fr, n, v):
    t = run.cur_track(ctx)
    td = run.model.tracks[t]
    s, p = v
    if p in td.locks.get(s, {}):
        return td.locks[s][p]
    return sat(run.model.param_value(t, p))     # no lock on the step: what the part holds now


@handler("PL.DEL")
def _pldel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.lock_del(v[0], v[1])


@handler("PL.RND")
def _plrnd(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        rg = _lock_range(run, t, v[0], n, fr)
        if rg is None:
            continue
        for s in ctx.steps:
            val = rg[0] + run.below(ctx, n.op.stream, rg[1] - rg[0] + 1)   # the draw is made on every step,
            if run.lands(td, s, n, fr):                                    # then the step is checked
                td.lock_set(s, v[0], val)


@handler("PL.VARY")
def _plvary(run, ctx, fr, n, v):
    p, x = v[0], clamp(v[1], 0, 100)
    for t, td in run.each_track(ctx, n, fr):
        rg = _lock_range(run, t, p, n, fr)
        if rg is None:
            continue
        d = ((rg[1] - rg[0]) * x + 50) // 100
        for s in ctx.steps:
            if p in td.locks.get(s, {}):
                delta = run.below(ctx, n.op.stream, 2 * d + 1) - d      # drawn where the lock is held
                if run.lands(td, s, n, fr):
                    td.locks[s][p] = clamp(td.locks[s][p] + delta, rg[0], rg[1])


@handler("PL.CLR")
def _plclr(run, ctx, fr, n, v):
    for t, td, s in run.each_step(ctx, n, fr):
        td.lock_del(s)


# SMP ------------------------------------------------------------------------------------------
@handler("SMP.SET")
def _smpset(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        if run.lands(td, v[0], n, fr):
            td.smp[v[0]] = v[1]


@handler("SMP.GET")
def _smpget(run, ctx, fr, n, v):
    return run.first_track(ctx).smp.get(v[0], 0)


@handler("SMP.DEL")
def _smpdel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        td.smp.pop(v[0], None)


@handler("SMP.RND")
def _smprnd(run, ctx, fr, n, v):
    lo, hi = min(v), max(v)
    for t, td, s in run.each_step(ctx, n, fr):
        val = lo + run.below(ctx, n.op.stream, hi - lo + 1)
        if run.lands(td, s, n, fr):
            td.smp[s] = val


@handler("SMP.CLR")
def _smpclr(run, ctx, fr, n, v):
    for t, td, s in run.each_step(ctx, n, fr):
        td.smp.pop(s, None)


# COND / MICRO / RATCH -------------------------------------------------------------------------
@handler("COND.SET")
def _condset(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        if run.lands(td, v[0], n, fr):
            td.set_opt(td.cond, v[0], v[1], 0)


@handler("COND.RND")
def _condrnd(run, ctx, fr, n, v):
    lo, hi = min(v), max(v)
    for t, td, s in run.each_step(ctx, n, fr):
        val = lo + run.below(ctx, n.op.stream, hi - lo + 1)
        if run.lands(td, s, n, fr):
            td.set_opt(td.cond, s, val, 0)


@handler("MICRO.SET")
def _microset(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        if run.lands(td, v[0], n, fr):
            td.set_opt(td.micro, v[0], clamp(v[1], -MICRO_MAX, MICRO_MAX), 0)


@handler("MICRO.RND")
def _micrornd(run, ctx, fr, n, v):
    x = clamp(v[0], 0, MICRO_MAX)
    for t, td, s in run.each_step(ctx, n, fr):
        val = run.below(ctx, n.op.stream, 2 * x + 1) - x
        if run.lands(td, s, n, fr):
            td.set_opt(td.micro, s, val, 0)


@handler("RATCH.SET")
def _ratch(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        if run.lands(td, v[0], n, fr):
            td.set_opt(td.count, v[0], clamp(v[1], 1, 8), 1)


@handler("COND.GET")
def _condget(run, ctx, fr, n, v):
    td = run.first_track(ctx)
    return td.cond.get(v[0], 0)


@handler("COND.DEL")
def _conddel(run, ctx, fr, n, v):
    for t, td in run.each_track(ctx, n, fr):
        if run.lands(td, v[0], n, fr):
            td.cond.pop(v[0], None)


@handler("MICRO.GET")
def _microget(run, ctx, fr, n, v):
    td = run.first_track(ctx)
    return td.micro.get(v[0], 0)


@handler("RATCH.GET")
def _ratchget(run, ctx, fr, n, v):
    td = run.first_track(ctx)
    return td.count.get(v[0], 1)


@handler("FILL")
def _fill(run, ctx, fr, n, v):
    return run.model.fill


@handler("FILL.ONCE")
def _fillonce(run, ctx, fr, n, v):
    run.model.fill_once = 1


# PART -------------------------------------------------------------
# Every write goes through an import on the unit (c/otc_vm.h): par.set -> the stock parameter writer (it
# clamps into the descriptor and refuses a disabled slot itself; the C clamps first so what it hands over is known),
# LFO page 2 / scene bytes / the factory slot -> direct part bytes committed once by the bridge, an effect -> the
# chooser's cursor + apply.  No writes are gated on PICKUP (that rule is the pattern's, 10.14).
def _part_range(run, t, p):
    """(lo, hi) of parameter p on track t from the part's descriptor (otb_host_param_info), None if the slot is disabled."""
    lo, count, enabled = run.model.pmodel.param_info(t, p)
    return (lo, lo + count - 1) if enabled else None


def _fx_applied(pt, slot, index):
    """The chooser apply of effect `index` in FX slot 1..2: the part now holds that effect and the effect's own defaults
    on its page (flat 18..23 / 24..29), so what the run wrote on that page is gone (the part's value is the model's)."""
    pt.fx[slot] = index
    for k in range(6):
        pt.params.pop(12 + 6 * slot + k, None)


def _all_off():
    return {k: NOT_LOCKED for k in range(SCENE_BYTES)}


@handler("PAR.SET")
def _parset(run, ctx, fr, n, v):
    # the stock writer clamps into the descriptor and refuses a disabled slot: refused = out of range, once per track
    for t in ctx.tracks:
        rg = _part_range(run, t, v[0])
        if rg is None:
            run.oor(n.op, n.line, fr)
            continue
        run.model.part[t].params[v[0]] = clamp(v[1], rg[0], rg[1])


@handler("LFO.DEST")
def _lfodest(run, ctx, fr, n, v):
    if not 1 <= v[0] <= 3:
        run.oor(n.op, n.line, fr)
        return
    for t in ctx.tracks:
        run.model.part[t].lfo_dest[v[0]] = v[1]


@handler("FX.TYPE")
def _fxtype(run, ctx, fr, n, v):
    # y = the position in the unit's chooser of slot x (0 = NONE), checked against the chooser read at runtime
    if not 1 <= v[0] <= 2 or not 0 <= v[1] < run.model.pmodel.fx_len(v[0]):
        run.oor(n.op, n.line, fr)
        return
    for t in ctx.tracks:
        _fx_applied(run.model.part[t], v[0], v[1])


@handler("SCN.CLR")
def _scnclr(run, ctx, fr, n, v):
    sc = v[0] if v else 0
    if not 0 <= sc <= SCENES:
        run.oor(n.op, n.line, fr)
        return
    for t in ctx.tracks:
        if sc == 0:
            for s in range(1, SCENES + 1):
                run.model.part[t].scenes[s] = _all_off()
        else:
            run.model.part[t].scenes[sc] = _all_off()


@handler("SCN.SET")
def _scnset(run, ctx, fr, n, v):
    sc, p, val = v
    if not (1 <= sc <= SCENES):
        run.oor(n.op, n.line, fr)
        return
    for t in ctx.tracks:
        rg = _part_range(run, t, p)
        if rg is None:
            run.oor(n.op, n.line, fr)
            continue
        d = run.model.part[t].scenes.setdefault(sc, {})
        d[p] = clamp(val, rg[0], rg[1])


@handler("SCN.GET")
def _scnget(run, ctx, fr, n, v):
    sc, p = v
    if not (1 <= sc <= SCENES):
        run.oor(n.op, n.line, fr)
        return 0
    t = run.cur_track(ctx)
    d = run.model.part[t].scenes.get(sc, {})
    val = d.get(p, NOT_LOCKED)
    return val if val != NOT_LOCKED else sat(run.model.param_value(t, p))


@handler("SCN.DEL")
def _scndel(run, ctx, fr, n, v):
    sc, p = v
    if not (1 <= sc <= SCENES):
        run.oor(n.op, n.line, fr)
        return
    for t in ctx.tracks:
        d = run.model.part[t].scenes.get(sc)
        if d is not None:
            d[p] = NOT_LOCKED


@handler("SCN.COPY")
def _scncopy(run, ctx, fr, n, v):
    src, dst = v
    if not (1 <= src <= SCENES and 1 <= dst <= SCENES):
        run.oor(n.op, n.line, fr)
        return
    for t in ctx.tracks:
        pt = run.model.part[t]
        src_d = pt.scenes.get(src, _all_off())
        pt.scenes[dst] = {k: src_d.get(k, NOT_LOCKED) for k in range(SCENE_BYTES)}


@handler("TRK.INIT")
def _trkinit(run, ctx, fr, n, v):
    # the track's SOUND only; its trigs, locks, sample locks, trig words and machine
    # type are not touched.  the earlier INIT TRACK function (MKI), in its order: the factory effects (an index the chooser does not
    # hold is skipped), the factory slot, every enabled parameter to its descriptor's default, the three LFOs aimed at
    # 0 with waveform 0, the 16 scene blocks of the track unlocked.  No draw.
    pm = run.model.pmodel
    for t in ctx.tracks:
        pt = run.model.part[t]
        for slot in (1, 2):
            d = pm.fx_factory(slot)
            if d < pm.fx_len(slot):
                _fx_applied(pt, slot, d)
        pt.slot = "factory"
        for p in range(30):
            rg = _part_range(run, t, p)
            if rg is not None:
                pt.params[p] = clamp(pm.param_default(t, p), rg[0], rg[1])
        for x in (1, 2, 3):
            pt.lfo_dest[x] = 0
            pt.lfo_wave[x] = 0
        for sc in range(1, SCENES + 1):
            pt.scenes[sc] = _all_off()


@handler("REC.SRC")
def _recsrc(run, ctx, fr, n, v):
    t, src_idx, val = v[0], v[1], v[2]
    if not (1 <= t <= 8) or not (1 <= src_idx <= 3):
        run.oor(n.op, n.line, fr)
        return
    if src_idx in (1, 2) and not (0 <= val <= 4):
        run.oor(n.op, n.line, fr)
        return
    if src_idx == 3 and not (0 <= val <= 10):
        run.oor(n.op, n.line, fr)
        return
    off = 0 if src_idx == 1 else 1 if src_idx == 2 else 4
    run.model.part[t].rec_setup[off] = val


@handler("REC.LEN")
def _reclen(run, ctx, fr, n, v):
    t, val = v[0], v[1]
    if not (1 <= t <= 8) or not (1 <= val <= 64):
        run.oor(n.op, n.line, fr)
        return
    run.model.part[t].rec_setup[2] = 64 if val == 64 else val - 1   # the part holds RLEN 0-based, 64 = MAX


@handler("REC.TRIG")
def _rectrig(run, ctx, fr, n, v):
    t, val = v[0], v[1]
    if not (1 <= t <= 8) or not (0 <= val <= 2):
        run.oor(n.op, n.line, fr)
        return
    run.model.part[t].rec_setup[3] = val            # TRAC +0x603: ONE, ONE-2, HOLD


@handler("REC.LOOP")
def _recloop(run, ctx, fr, n, v):
    t, val = v[0], v[1]
    if not (1 <= t <= 8) or not (0 <= val <= 1):
        run.oor(n.op, n.line, fr)
        return
    run.model.part[t].rec_setup[5] = val            # TRAC +0x605: LOOP off / on


@handler("REC.ARM")
def _recarm(run, ctx, fr, n, v):
    t, src = v[0], v[1]
    if not (1 <= t <= 8) or not (1 <= src <= 7):
        run.oor(n.op, n.line, fr)
        return
    run.model.rec_arms.append((t, src))


@handler("TRK.SLOT")
def _trkslot(run, ctx, fr, n, v):
    t = run.cur_track(ctx)
    pt = run.model.part[t]
    s = pt.slot
    if s == "factory":
        return t if pt.mtype <= 1 else 128 + t if pt.mtype == PICKUP else 1
    return s if isinstance(s, int) else 1


@handler("TEMPO")
def _tempo(run, ctx, fr, n, v):
    return run.model.tempo


@handler("TRK.LEVEL")
def _trklevel(run, ctx, fr, n, v):
    return run.model.part[run.cur_track(ctx)].level


@handler("TRK.CUE")
def _trkcue(run, ctx, fr, n, v):
    return run.model.part[run.cur_track(ctx)].cue


@handler("TRK.MUTE")
def _trkmute(run, ctx, fr, n, v):
    return run.model.mutes[run.cur_track(ctx)]


@handler("PAR.GET")
def _parget(run, ctx, fr, n, v):
    p = v[0]
    t = run.cur_track(ctx)
    rg = _part_range(run, t, p)
    if rg is None:
        run.oor(n.op, n.line, fr)
        return 0
    return sat(run.model.param_value(t, p))


def _parbound(run, ctx, fr, n, p, i):
    """par.min / par.max: the descriptor bound (lo, hi)[i] of parameter p on the context track; a disabled slot is par.get's refusal."""
    rg = _part_range(run, run.cur_track(ctx), p)
    if rg is None:
        run.oor(n.op, n.line, fr)
        return 0
    return rg[i]


@handler("PAR.MIN")
def _parmin(run, ctx, fr, n, v):
    return _parbound(run, ctx, fr, n, v[0], 0)


@handler("PAR.MAX")
def _parmax(run, ctx, fr, n, v):
    return _parbound(run, ctx, fr, n, v[0], 1)


@handler("FX.COUNT")
def _fxcount(run, ctx, fr, n, v):
    if not 1 <= v[0] <= 2:
        run.oor(n.op, n.line, fr)
        return 0
    return run.model.pmodel.fx_len(v[0])


@handler("PB.SETUP")
def _pbsetup(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 6):
        run.oor(n.op, n.line, fr)
        return 0
    t = run.cur_track(ctx)
    return run.model.part[t].pb_setup.get(v[0], run.model.pmodel.setup_values.get((t, n.op.name, v[0]), 0))


@handler("AMP.SETUP")
def _ampsetup(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 6):
        run.oor(n.op, n.line, fr)
        return 0
    t = run.cur_track(ctx)
    return run.model.part[t].amp_setup.get(v[0], run.model.pmodel.setup_values.get((t, n.op.name, v[0]), 0))


@handler("LFO.WAVE")
def _lfowave(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 3):
        run.oor(n.op, n.line, fr)
        return 0
    return run.model.part[run.cur_track(ctx)].lfo_wave.get(v[0], 0)


@handler("LFO.MULT")
def _lfomult(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 3):
        run.oor(n.op, n.line, fr)
        return 0
    return run.model.part[run.cur_track(ctx)].lfo_mult.get(v[0], 0)


@handler("LFO.TRIG")
def _lfotrig(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 3):
        run.oor(n.op, n.line, fr)
        return 0
    return run.model.part[run.cur_track(ctx)].lfo_trig.get(v[0], 0)


@handler("LFOD.SET")
def _lfodset(run, ctx, fr, n, v):
    s, val = v[0], v[1]
    if not (1 <= s <= 16):
        run.oor(n.op, n.line, fr)
        return
    val = clamp(val, -64, 63)
    for t in ctx.tracks:
        run.model.part[t].lfod[s - 1] = val


@handler("LFOD.GET")
def _lfodget(run, ctx, fr, n, v):
    s = v[0]
    if not (1 <= s <= 16):
        run.oor(n.op, n.line, fr)
        return 0
    return run.model.part[run.cur_track(ctx)].lfod[s - 1]


@handler("LFOD.INTERP")
def _lfodinterp(run, ctx, fr, n, v):
    s, x = v[0], v[1]
    if not (1 <= s <= 16):
        run.oor(n.op, n.line, fr)
        return
    flg = 1 if x else 0
    for t in ctx.tracks:
        run.model.part[t].lfod_interp[s - 1] = flg


@handler("FX1.SETUP")
def _fx1setup(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 6):
        run.oor(n.op, n.line, fr)
        return 0
    t = run.cur_track(ctx)
    return run.model.part[t].fx1_setup.get(v[0], run.model.pmodel.setup_values.get((t, n.op.name, v[0]), 0))


@handler("FX2.SETUP")
def _fx2setup(run, ctx, fr, n, v):
    if not (1 <= v[0] <= 6):
        run.oor(n.op, n.line, fr)
        return 0
    t = run.cur_track(ctx)
    return run.model.part[t].fx2_setup.get(v[0], run.model.pmodel.setup_values.get((t, n.op.name, v[0]), 0))


# card -----------------------------------------------------------------------------------------
@handler("DIR")
def _dir(run, ctx, fr, n, v):
    run.card_access(n)
    deep = len(v) > 1 and v[1] == "DEEP"
    path = canon_path(v[0])
    items = list_card(run.card, path, deep)
    # device limits (spec): long names are skipped first, then the list is cut
    keep = [f for f in items if len(f.encode("utf-8", "surrogateescape")) <= O.DIR_NAME_MAX]
    if len(keep) < len(items):
        _warn_once(run, 'dir "%s": %d names longer than %d characters skipped'
                   % (path, len(items) - len(keep), O.DIR_NAME_MAX))
    # the live lists share DIR_LIST_MAX name slots (one own base per frame at most; a
    # callee's copy costs nothing until it runs its own `dir`).  This frame's old base goes back first.
    fr.own = 0
    cap = min(O.DIR_LIST_MAX, O.DIR_LIST_MAX - sum(f.own for f in run.live_frames()))
    if len(keep) > cap:
        _warn_once(run, 'dir "%s": %d files, only the first %d kept' % (path, len(keep), cap))
        keep = keep[:cap]
    fr.files = FileList(path, deep, keep)
    fr.own = len(keep)


def _warn_once(run, msg):
    if msg not in run.warnings:
        run.warnings.append(msg)


@handler("EXT")
def _ext(run, ctx, fr, n, v):
    exts = {fold("." + e) for e in v}
    fr.files.items = [f for f in fr.files.items if ext_key(f) in exts]
    fr.files.filters.append("ext:" + "+".join(sorted(fold_lower(e) for e in v)))


@handler("MATCH")
def _match(run, ctx, fr, n, v):
    fr.files.items = [f for f in fr.files.items
                      if any(wildcard_match(p, f.rsplit("/", 1)[-1]) for p in v)]
    fr.files.filters.append("match:" + "+".join(v))


@handler("FILE.LEN")
def _filelen(run, ctx, fr, n, v):
    return len(fr.files.items)


@handler("FILE.RND")
def _filernd(run, ctx, fr, n, v):
    cnt = len(fr.files.items)
    r = run.below(ctx, n.op.stream, cnt)
    return r + 1 if cnt else 0


@handler("FILE.PICK")
def _filepick(run, ctx, fr, n, v):
    cnt = len(fr.files.items)
    if cnt == 0:
        run.below(ctx, n.op.stream, 0)
        return 0
    # bags are keyed by (function, FNV-1a of the signature), at most PICK_BAGS_MAX in
    # all, oldest first by INSERTION: using a bag again does not refresh its age.
    fn_name, h = O.show(fr.fn.name), sig_hash(fr.files.sig)
    bag = next((b for b in run.bags if b["func"] == fn_name and b["hash"] == h), None)
    if bag is None:
        if len(run.bags) >= O.PICK_BAGS_MAX:
            del run.bags[0]
        bag = {"func": fn_name, "hash": h, "n": cnt, "rest": []}
        run.bags.append(bag)
    if not bag["rest"]:
        bag["n"], bag["rest"] = cnt, list(range(1, cnt + 1))
    return bag["rest"].pop(run.below(ctx, n.op.stream, len(bag["rest"])))


@handler("FILE.AT")
def _fileat(run, ctx, fr, n, v):
    return v[0] if 1 <= v[0] <= len(fr.files.items) else 0


@handler("STATIC.LOAD")
def _slotload(run, ctx, fr, n, v):
    n_, f = v
    if not 1 <= f <= len(fr.files.items):
        run.oor(n.op, n.line, fr)
        return
    run.card_access(n)
    run.load_slot(fr, n, n_, slot_path(fr.files.path, fr.files.items[f - 1]))


@handler("STATIC.FREE")
def _slotfree(run, ctx, fr, n, v):
    return int(v[0] not in run.model.slots)


@handler("STATIC.FIRSTFREE")
def _slotfirst(run, ctx, fr, n, v):
    for s in range(1, 129):
        if s not in run.model.slots:
            return s
    return 0


@handler("STATIC.CLR")
def _slotclr(run, ctx, fr, n, v):
    if v[0] in run.model.slots:          # an empty slot is not touched: no card access
        run.card_access(n)
        del run.model.slots[v[0]]


@handler("STATIC.SWAP")
def _staticswap(run, ctx, fr, n, v):
    """Each slot is reloaded by name from the other's (the native shuffle's per-slot reload): a refused reload frees its slot,
    an empty source clears its target (the sample moves).  a = b, or both empty: nothing moves, no card access."""
    a, b = v
    sl = run.model.slots
    if a == b or (a not in sl and b not in sl):
        return
    run.card_access(n)
    pa, pb = sl.get(a), sl.get(b)
    for s, p in ((a, pb), (b, pa)):
        if p is None:
            del sl[s]
        else:
            run.load_slot(fr, n, s, p)


@handler("SLOTS.CLR")
def _slotsclr(run, ctx, fr, n, v):
    del run.model.working[:]


@handler("SLOTS.ADD")
def _slotsadd(run, ctx, fr, n, v):
    if v[0] not in run.model.working:
        run.model.working.append(v[0])


@handler("SLOTS.RND")
def _slotsrnd(run, ctx, fr, n, v):
    w = run.model.working
    r = run.below(ctx, n.op.stream, len(w))
    return w[r] if w else 0


# ---- AUDIO operations ----------------------------------------
@handler("SRC.SAVE")
def _srcsave(run, ctx, fr, n, v):
    run.card_access(n)
    if "SRC.SAVE" in run.model.audio_refuse:
        code = run.model.audio_refuse["SRC.SAVE"]
        raise AudioError(code)
    src = run.model.audio_source
    if src is None or not src.get("path"):
        raise AudioError(-3, "NO SOURCE")
    if src.get("recording"):
        raise AudioError(-6, "STOP FIRST")
    if src.get("empty"):
        raise AudioError(-4, "EMPTY")
    if src.get("card_full"):
        raise AudioError(-9, "CARD FULL")
    if src.get("io_error"):
        raise AudioError(-10, "IO ERROR")
    if src.get("timeout"):
        raise AudioError(-11, "TIMEOUT")
    path = src["path"]
    if src.get("exists_fail") or (src.get("exists") and (path in getattr(run.model, "exists_files", set()))):
        raise AudioError(-7, "EXISTS")
    p = path.lstrip("/")
    if "/" in p:
        dir_name, base_name = p.rsplit("/", 1)
    else:
        dir_name, base_name = "", p
    fr.files = FileList(dir_name, False, [base_name])
    fr.own = 1


@handler("FLEX.LOAD")
def _flexload(run, ctx, fr, n, v):
    slot, f = v
    if not (1 <= slot <= 128 and 1 <= f <= len(fr.files.items)):
        run.oor(n.op, n.line, fr)
        return
    run.card_access(n)
    if "FLEX.LOAD" in run.model.audio_refuse:
        code = run.model.audio_refuse["FLEX.LOAD"]
        raise AudioError(code)
    path = slot_path(fr.files.path, fr.files.items[f - 1])
    if slot in run.model.flex_slots and slot in run.model.flex_playing:
        raise AudioError(-6, "STOP FIRST")
    if getattr(run.model, "flex_full", False):
        raise AudioError(-5, "FLEX FULL")
    run.model.flex_slots[slot] = path


@handler("FLEX.FREE")
def _flexfree(run, ctx, fr, n, v):
    slot = v[0]
    if not (1 <= slot <= 128):
        run.oor(n.op, n.line, fr)
        return 0
    if "FLEX.FREE" in run.model.audio_refuse:
        code = run.model.audio_refuse["FLEX.FREE"]
        raise AudioError(code)
    return int(slot not in run.model.flex_slots)


@handler("FLEX.FIRSTFREE")
def _flexfirst(run, ctx, fr, n, v):
    for s in range(1, 129):
        if s not in run.model.flex_slots:
            return s
    return 0


@handler("FLEX.DEL")
def _flexdel(run, ctx, fr, n, v):
    slot = v[0]
    if slot not in run.model.flex_slots:     # an empty slot is not touched
        return
    if "FLEX.DEL" in run.model.audio_refuse:
        raise AudioError(run.model.audio_refuse["FLEX.DEL"])
    if slot in run.model.flex_playing:
        raise AudioError(-6, "STOP FIRST")
    del run.model.flex_slots[slot]


@handler("CAT.CLR")
def _catclr(run, ctx, fr, n, v):
    run.model.cat_list.clear()


@handler("CAT.ADD")
def _catadd(run, ctx, fr, n, v):
    f = v[0]
    if not (1 <= f <= len(fr.files.items)):
        run.oor(n.op, n.line, fr)
        return
    if "CAT.ADD" in run.model.audio_refuse:
        code = run.model.audio_refuse["CAT.ADD"]
        raise AudioError(code)
    if len(run.model.cat_list) >= 64:
        raise AudioError(-12, "CAT FULL")
    path = slot_path(fr.files.path, fr.files.items[f - 1])
    run.model.cat_list.append(path)


@handler("CAT.SAVE")
def _catsave(run, ctx, fr, n, v):
    name = v[0]
    if isinstance(name, int):
        name = getattr(run, "pool", None).string(name) if hasattr(run, "pool") else str(name)
    import re
    if not name or len(name) > 63 or not re.fullmatch(r"[A-Za-z0-9 _-]+", name):
        raise AudioError(-1, "BAD ARG")
    if len(run.model.cat_list) == 0:
        raise AudioError(-4, "EMPTY")
    run.card_access(n)
    if "CAT.SAVE" in run.model.audio_refuse:
        code = run.model.audio_refuse["CAT.SAVE"]
        raise AudioError(code)
    src = run.model.audio_source
    dest_dir = "AUDIO"
    if src and src.get("path"):
        sp = src["path"].lstrip("/")
        if "/" in sp:
            dest_dir = sp.rsplit("/", 1)[0]
    out_name = f"{name}.WAV"
    full_out = f"{dest_dir}/{out_name}" if dest_dir else out_name
    if full_out in getattr(run.model, "exists_files", set()):
        raise AudioError(-7, "EXISTS")
    fr.files = FileList(dest_dir, False, [out_name])
    fr.own = 1


@handler("NORM")
def _norm(run, ctx, fr, n, v):
    f, db = v
    if not (1 <= f <= len(fr.files.items)):
        run.oor(n.op, n.line, fr)
        return
    if not -12 <= db <= 0:
        raise AudioError(-1, "BAD ARG")
    run.card_access(n)
    if "NORM" in run.model.audio_refuse:
        code = run.model.audio_refuse["NORM"]
        raise AudioError(code)
    path = slot_path(fr.files.path, fr.files.items[f - 1])
    run.model.normalized[path] = db


@handler("PLAY")
def _play(run, ctx, fr, n, v):
    run.record_play(v[0])


@handler("GATE")
def _gate(run, ctx, fr, n, v):
    run.record_gate(v[0])


@handler("OUT")
def _out(run, ctx, fr, n, v):
    run.set_out(v[0], v[1])


@handler("CV")
def _cv(run, ctx, fr, n, v):
    return run.read_cv(v[0], v[1])


@handler("NX.SMP")
def _nx_smp(run, ctx, fr, n, v):
    run.nx_smp = v[0]


@handler("NX.PL")
def _nx_pl(run, ctx, fr, n, v):
    rg = _lock_range(run, run.cur_track(ctx), v[0], n, fr)
    if rg is not None:
        run.nx_pl[v[0]] = clamp(v[1], rg[0], rg[1])


@handler("SEQ.RUN")
def _seq_run(run, ctx, fr, n, v):
    sr = getattr(run, "seq_running", False)
    if callable(sr):
        return 1 if sr(getattr(run, "tick", 0)) else 0
    return 1 if sr else 0


@handler("SEQ.STEP")
def _seq_step(run, ctx, fr, n, v):
    t = v[0]
    steps = getattr(run, "seq_steps", None)
    tick = getattr(run, "tick", 0)
    if callable(steps):
        return steps(tick, t)
    if isinstance(steps, dict):
        return steps.get(t, 1)
    if isinstance(steps, (list, tuple)) and 1 <= t <= len(steps):
        return steps[t - 1]
    return 1


@handler("SEQ.MUTE")
def _seq_mute(run, ctx, fr, n, v):
    t = v[0]
    mutes = getattr(run, "seq_mutes", None)
    tick = getattr(run, "tick", 0)
    if callable(mutes):
        return 1 if mutes(tick, t) else 0
    if isinstance(mutes, dict):
        return 1 if mutes.get(t, 0) else 0
    if isinstance(mutes, (list, tuple)) and 1 <= t <= len(mutes):
        return 1 if mutes[t - 1] else 0
    return 0


@handler("NOTE.NUM")
def _note_num(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("note.num", 0)


@handler("NOTE.VEL")
def _note_vel(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("note.vel", 0)


@handler("NOTE.CH")
def _note_ch(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("note.ch", 1)


@handler("CC.NUM")
def _cc_num(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("cc.num", 0)


@handler("CC.VAL")
def _cc_val(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("cc.val", 0)


@handler("CC.CH")
def _cc_ch(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("cc.ch", 1)


@handler("TRIG.STEP")
def _trig_step(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("trig.step", 0)


@handler("TRIG.HIT")
def _trig_hit(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("trig.hit", 0)


@handler("TRIG.VAL")
def _trig_val(run, ctx, fr, n, v):
    ev = getattr(run, "current_event", None) or {}
    return ev.get("trig.val", 0)


@handler("SEND.NOTE")
def _send_note(run, ctx, fr, n, v):
    run.midi_out.append((run.tick, "note", v[0], v[1], v[2], v[3]))


@handler("SEND.CC")
def _send_cc(run, ctx, fr, n, v):
    run.midi_out.append((run.tick, "cc", v[0], v[1], v[2]))


@handler("SEND.PROG")
def _send_prog(run, ctx, fr, n, v):
    run.midi_out.append((run.tick, "prog", v[0], v[1]))


@handler("SEND.NRPN")
def _send_nrpn(run, ctx, fr, n, v):
    ch, p, val = v[0], v[1], v[2]
    for num, x in ((99, p >> 7), (98, p & 0x7F), (6, val >> 7), (38, val & 0x7F)):
        run.midi_out.append((run.tick, "cc", ch, num, x))


@handler("MIDI.CC")
def _midi_cc(run, ctx, fr, n, v):
    return run.model.midi_cc.get((v[0], v[1]), 0)


@handler("MIDI.NOTE")
def _midi_note(run, ctx, fr, n, v):
    return run.model.midi_note.get(v[0], 0)


@handler("MIDI.VEL")
def _midi_vel(run, ctx, fr, n, v):
    return run.model.midi_vel.get(v[0], 0)


def msg_text(items):
    """The text of a msg: its items (strings, numbers) in decimal, one space between them; at most MAX_MSG characters
    (the checker refuses a worst case above; an argument that carries a long string is cut here, as the C VM's buffer does)."""
    return " ".join(i if isinstance(i, str) else str(i) for i in items)[:O.MAX_MSG]


@handler("MSG")
def _msg(run, ctx, fr, n, v):
    run.model.msgs.append(msg_text(v))


# ---- simulation entry point -----------------------------------------------------------------
class SimError(Exception):
    """A refusal before the run starts (unknown function, arguments, bad option)."""


def declared_names():
    """Registry names that the simulator implements, plus the context ops handled in exec_ctx."""
    names = set(HANDLERS) | {"TRK", "STEPS", "STEP", "SEED"} | {k + ".SEED" for k in STREAM_NAMES}
    names |= {o.name for o in O.ALL_OPS if o.value is not None or o.lazy}
    return names


def _refuse_predicate(refuse):
    """`refuse`: None, a predicate(canonical path) -> bool, or a set / list of canonical paths
    (compared ASCII-folded: the same file is reached through any spelling of its folder)."""
    if refuse is None:
        return None
    if callable(refuse):
        return refuse
    paths = {fold(canon_path(str(p))) for p in refuse}
    return lambda path: fold(path) in paths


def _busy_predicate(busy):
    """`busy`: None, a callable(op name) -> bool (called at every card access), or a set of op
    names (`dir`, `static.load`, `static.clr`)."""
    if busy is None:
        return None
    if callable(busy):
        return busy
    names = {str(b).upper() for b in busy}
    return lambda op: op in names


def _initial_slots(slots):
    out = {}
    for k, v in (slots or {}).items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            raise SimError("slots: %r is not a slot number" % (k,))
        if not 1 <= n <= 128:
            raise SimError("slots: slot %d is outside 1..128" % n)
        if not isinstance(v, str) or not canon_path(v) or ".." in canon_path(v).split("/"):
            raise SimError("slots: slot %d: %r is not a path inside the Set" % (n, v))
        out[n] = canon_path(v)
    return out


def simulate(prog, func, seed=None, bank=None, pattern=1, track=1, length=None, card=None,
             state=None, params=None, run_class=None, slots=None, refuse=None, busy=None,
             pmodel=None, model=None):
    """Run FUNC (a name) on the program `prog` (a checked tools/otype.py Program).

    `slots` = {1..128: canonical path} the slots hold at the start (default: all free);
    `refuse` = a predicate or a set of canonical paths whose load the unit refuses; `busy` = a set of
    op names or a callable(op name) for which the card is busy (the run raises CardBusy).

    `run_class` (default Run) is the engine: modules/otype/vm.py passes its own, with an
    OTB-backed `prog` (fmap, maxcost), so that both engines build the same Result."""
    name = func.upper()
    fn = prog.fmap.get(name)
    if fn is None:
        raise SimError("no function named %s" % O.show(name))
    if fn.refused:
        raise SimError("refused")
    if fn.args:
        raise SimError("%s takes arguments (%s): only a function with no argument can be run"
                       % (O.show(name), O.show(" ".join(fn.args))))
    if not 1 <= track <= 8:
        raise SimError("--track must be 1..8")
    warnings = []
    pvals = {}
    for k, v in (params or {}).items():
        K = k.upper()
        if K not in fn.params:
            raise SimError("%s has no param %s" % (O.show(name), O.show(k)))
        p = fn.params[K]
        d, lo, hi = p[:3]
        kind = getattr(p, "kind", "INT")
        if kind == "FOLDER":
            pvals[K] = str(v)
        elif kind == "BOOL":
            iv = 1 if (v is True or v == 1 or str(v).lower() in ("yes", "true", "1")) else 0
            pvals[K] = iv
        else:
            c = clamp(int(v), lo, hi)
            if c != int(v):
                warnings.append("param %s = %d clamped to %d (range %d..%d)" % (O.show(k), int(v), c, lo, hi))
            pvals[K] = c
    model = Model() if model is None else model
    if not 1 <= pattern <= 16:
        raise SimError("--pattern must be 1..16")
    model.go_pattern(1, pattern)
    if pmodel is not None:
        model.pmodel = pmodel                     # the part's descriptors / values (ParamModel)
    model.slots = _initial_slots(slots)
    length_src, length_val, bank_info = "default", 16, None
    if bank:
        tracks, bank_info = read_bank(bank, pattern)
        model.tracks = tracks
        if bank_info["per_track"]:
            tl = bank_info["track_lengths"][track]
            length_val, length_src = (tl, "per-track length, bank") if 1 <= tl <= 64 else (16, "default")
        else:
            pl = bank_info["pattern_length"]
            length_val, length_src = (pl, "pattern, bank") if 1 <= pl <= 64 else (16, "default")
    if length is not None:
        length_val, length_src = clamp(length, 1, 64), "--len"
    if seed is None:
        seed, seed_src = int(time.time()) & 0xFFFF, "clock"
    else:
        seed_src = "--seed"
    before = model.snapshot()
    model.pattern_lengths[model.bank, model.pattern] = length_val
    run = (run_class or Run)(prog, fn, seed, seed_src, model, track, length_val, pattern, card,
                             state, pvals)
    run.warnings = warnings
    run.refuse, run.busy = _refuse_predicate(refuse), _busy_predicate(busy)
    try:
        run.run()
    except CardBusy as e:
        e.result = _result(run, name, fn, model, before, seed, seed_src, pattern, track, bank,
                           length_val, length_src, prog)
        raise
    return _result(run, name, fn, model, before, seed, seed_src, pattern, track, bank,
                   length_val, length_src, prog)


def _result(run, name, fn, model, before, seed, seed_src, pattern, track, bank, length_val,
            length_src, prog):
    r = Result()
    r.func, r.fn, r.model, r.before = O.show(name), fn, model, before
    r.seed, r.seed_source = seed, seed_src
    r.pattern, r.track, r.bank = pattern, track, bank
    r.length, r.length_source = length_val, length_src
    r.cost, r.max_cost = run.cost, prog.maxcost.get(fn, 0)
    r.draws = dict(run.draws)
    r.oor = list(run.oor_list)
    r.skipped = list(run.skip_list)
    r.skipped_pickup = list(run.pickup_list)
    r.refused = list(run.refused_list)
    r.not_on_unit = sorted(run.used_unit)
    r.warnings = run.warnings
    r.globals = {k.lower(): v for k, v in run.globals.items()}
    r.fn_vars = {}
    if getattr(run, "saved_vars", None):
        for fn_k, v in run.saved_vars.items():
            r.fn_vars[str(fn_k).upper()] = dict(v)
    for fn, v in getattr(run, "fn_vars", {}).items():
        fn_name = getattr(fn, "name", str(fn))
        fn_key = fn_name.upper()
        d = dict(v)
        for k, val in list(d.items()):
            if isinstance(k, str):
                d[k.upper()] = val
        if fn_key not in r.fn_vars:
            r.fn_vars[fn_key] = {}
        r.fn_vars[fn_key].update(d)
    r.state = state_json(run)
    r.plays = list(run.plays)
    r.midi_out = list(run.midi_out)
    r.outs = list(getattr(run, "outs", []))
    r.gates = list(getattr(run, "gates", []))
    return r


def simulate_module(prog, name, total_ticks, seed=None, state=None, params=None,
                    model=None, track=1, pattern=1, bank=None,
                    seq_running=False, seq_steps=None, seq_mutes=None,
                    run_class=None):
    """Simulate a module function over `total_ticks` ticks (0 .. total_ticks - 1).
    Invokes the module at tick 0 and every `clock_ticks` thereafter (tick < total_ticks).
    Preserves state across invocations.
    Returns (last_result, all_plays) where all_plays is a list of (tick, track_number)."""
    fn = prog.fmap.get(name.upper())
    if fn is None:
        raise SimError("unknown function %r" % name)
    clock_ticks = None
    if getattr(fn, "clock", None) is not None:
        clock_ticks = fn.clock[0] if isinstance(fn.clock, tuple) else int(fn.clock)
    elif hasattr(fn, "clock_ticks"):
        clock_ticks = int(fn.clock_ticks)
    if clock_ticks is None or clock_ticks < 1:
        raise SimError("function %r is not a module or has invalid clock" % name)

    all_plays = []
    curr_state = state
    curr_fn_vars = {}
    last_res = None
    curr_model = model

    for t in range(0, total_ticks, clock_ticks):
        sr = seq_running(t) if callable(seq_running) else seq_running
        sst = seq_steps(t) if callable(seq_steps) else seq_steps
        sm = seq_mutes(t) if callable(seq_mutes) else seq_mutes

        def _make_run(*args, **kwargs):
            cls = run_class or Run
            r = cls(*args, **kwargs)
            r.tick = t
            r.current_event = {"type": "CLOCK"}
            r.seq_running = sr
            r.seq_steps = sst
            r.seq_mutes = sm
            if curr_fn_vars:
                r.saved_vars = copy.deepcopy(curr_fn_vars)
            return r

        last_res = simulate(prog, name, seed=seed, model=curr_model, track=track,
                            pattern=pattern, bank=bank, state=curr_state,
                            params=params, run_class=_make_run)
        all_plays.extend(last_res.plays)
        curr_state = last_res.state
        curr_fn_vars = getattr(last_res, "fn_vars", {})
        curr_model = last_res.model
        if seed is not None:
            seed = (seed + 1) & 0xFFFF

    return last_res, all_plays


def state_json(run):
    funcs = {}
    for name, st in run.fstates.items():
        funcs[O.show(name)] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in st.items()}
    # keep the functions of a previous state that this run did not touch
    for name, st in run.state.get("funcs", {}).items():
        funcs.setdefault(name, st)
    bags = [{"func": b["func"], "hash": b["hash"], "n": b["n"], "rest": list(b["rest"])}
            for b in run.bags]
    return {"globals": {k.lower(): v for k, v in run.globals.items()}, "funcs": funcs, "bags": bags}


# ---- rendering ------------------------------------------------------------------------------
def step_char(td, s):
    if td.has("trig", s):
        return "X"
    if td.has("oneshot", s):
        return "!"
    if td.has("tless", s):
        return "o"
    if td.has("tlock", s):
        return "-"
    if td.has_lock(s) or s in td.smp:
        return "l"
    return "."


def track_summary(td):
    return td.to_json()


def last_used(td):
    last = 0
    for s in range(1, 65):
        if (any(td.has(m, s) for m in MASKS[:4]) or td.has_lock(s) or s in td.smp
                or s in td.cond or s in td.micro or s in td.count):
            last = s
    return last


def step_details(td, s, verbose):
    items = []
    for p, v in sorted(td.locks.get(s, {}).items()):
        items.append("%s %d" % (param_name(p), v))
    if not verbose and len(items) > 3:
        items = items[:3] + ["+%d locks" % (len(items) - 3)]
    if s in td.smp:
        n = td.smp[s]
        items.append("smp %s" % (n if n <= 128 else "rec%d" % (n - 128)))
    if s in td.cond:
        items.append("cond %s" % cond_name(td.cond[s]))
    if s in td.micro:
        items.append("micro %+d" % td.micro[s])
    if s in td.count:
        items.append("ratch %d" % td.count[s])
    return items


DETAIL_WIDTH = 100
DETAIL_ALL_MAX = 12       # up to this many steps with details: all are listed


def wrap_items(items, indent, sep, width):
    """Lines of `items` joined by `sep`, each at most `width` characters wide (one item at least)."""
    lines, cur = [], ""
    for it in items:
        if cur and len(indent + cur + sep + it) > width:
            lines.append(indent + cur)
            cur = ""
        cur = cur + sep + it if cur else it
    if cur:
        lines.append(indent + cur)
    return lines


def render_track(t, td, length, verbose):
    last = max(length, last_used(td))
    out = []
    for g in range(0, last, 16):
        n = min(16, last - g)
        head = "".join(str(g + k + 1).ljust(4) for k in range(0, n, 4)).rstrip()
        row = "".join(step_char(td, g + k + 1) for k in range(n))
        lab = ("T%d" % t).ljust(4) if g == 0 else "    "
        out.append(lab + head)
        out.append("    " + row)
    if last > length:
        out.append("    (steps after %d are beyond the pattern length)" % length)
    items_of = {s: step_details(td, s, verbose) for s in range(1, 65)}
    items_of = {s: it for s, it in items_of.items() if it}
    # many steps with details: only the steps that play something (-v: all)
    everything = verbose or len(items_of) <= DETAIL_ALL_MAX
    det, hidden = [], 0
    for s, items in items_of.items():
        if everything or td.has_trig(s):
            det.append("step %d: %s" % (s, "  ".join(items)))
        else:
            hidden += 1
    out.extend(wrap_items(det, "    ", "   ", DETAIL_WIDTH))
    if hidden:
        out.append("    (+%d more steps with locks, a condition or micro timing and no trig: -v)" % hidden)
    return out


def part_lines(run_res, verbose):
    m, b = run_res.model, run_res.before
    out = []
    changed = {}
    for t in range(1, 9):
        pj, bj = m.part[t].to_json(), b.part[t].to_json()
        if pj != bj or verbose and pj != PartData().to_json():
            changed[t] = pj
    for t, pj in changed.items():
        bits = []
        if pj["slot"] == "factory":
            bits.append("factory slot")
        elif pj["slot"] is not None:
            bits.append("slot %d" % pj["slot"])
        if pj["fx"]:
            bits.append(" ".join("fx%s #%d" % kv for kv in pj["fx"].items()))
        if pj["params"]:
            bits.append(" ".join("%s %d" % kv for kv in pj["params"].items()))
        if pj["lfo_dest"]:
            bits.append(" ".join("lfo%s->%s" % kv for kv in pj["lfo_dest"].items()))
        if pj["lfo_wave"]:
            bits.append(" ".join("lfo%s wave %d" % kv for kv in pj["lfo_wave"].items()))
        if pj["scenes"]:
            off = [k for k, d in pj["scenes"].items() if d == "off"]
            locks = sum(1 for d in pj["scenes"].values() if d != "off" for x in d.values() if x != "off")
            bits.append("scenes: %d locks%s" % (locks, ", %d unlocked" % len(off) if off else ""))
        out.append("part:  t%d %s" % (t, "  ".join(bits) if bits else "(reset)"))
    if m.tempo is not None:
        out.append("part:  tempo %d" % m.tempo)
    return out


def _where(func, items, tracks=False):
    """`op line n x2, ...`: what an out-of-range or skipped list holds, counted per call site
    (and per track for the PICKUP list, whose items carry a fourth field)."""
    cnt = {}
    for it in items:
        cnt[it] = cnt.get(it, 0) + 1
    desc = []
    for it, c in cnt.items():
        fn, op, line = it[:3]
        w = "%s line %d" % (op, line)
        if tracks:
            w += " track %d" % it[3]
        if fn != func:
            w += " in %s" % fn
        desc.append(w + (" x%d" % c if c > 1 else ""))
    return ", ".join(desc)


REFUSED_SHOWN = 8       # refusals listed in the text report (the JSON lists all of them)


def render_text(res, verbose=False):
    out = []
    pat = "%s%02d" % (bank_letter(res.bank), res.pattern)
    src = res.length_source
    out.append("%s  seed %d (%s)  pattern %s len %d (%s)  track %d"
               % (res.func, res.seed, res.seed_source, pat, res.length, src, res.track))
    for w in res.warnings:
        out.append("warning: " + w)
    shown = 0
    for t in range(1, 9):
        td, tb = res.model.tracks[t], res.before.tracks[t]
        changed = td.to_json() != tb.to_json()
        nonempty = last_used(td) > 0
        if verbose or changed or nonempty:
            out.extend(render_track(t, td, res.length, verbose))
            shown += 1
    if not shown:
        out.append("(no track holds anything)")
    out.append("      X trig  ! one-shot  o trigless trig  - trigless lock  l locks only  . none")
    m = res.model
    sl = ["%d <- %s" % (s, p) for s, p in sorted(m.slots.items())
          if res.before.slots.get(s) != p]
    if sl:
        out.append("slots: " + sl[0])
        out.extend("       " + x for x in sl[1:])
    if m.working:
        out.append("slots set: " + " ".join(str(s) for s in m.working))
    out.extend(part_lines(res, verbose))
    if res.not_on_unit:
        out.append("(not on the unit yet: %s)" % " ".join(res.not_on_unit))
    for x in m.msgs:
        out.append('msg:   "%s"' % x)
    if m.msgs:
        out.append('end box: "%s" (the last msg, instead of DONE)' % m.msgs[-1])
    d = res.draws
    out.append("cost: %d/%d   random draws: %s"
               % (res.cost, res.max_cost, "  ".join("%s %d" % (k.lower(), d[k]) for k in STREAM_NAMES)))
    if res.oor:
        out.append("%s: %d out of range (%s)" % (res.func, len(res.oor), _where(res.func, res.oor)))
    if res.skipped:
        out.append("%s: %d skipped, no trig (%s)"
                   % (res.func, len(res.skipped), _where(res.func, res.skipped)))
    if res.skipped_pickup:
        out.append("%s: %d skipped, PICKUP track (%s)"
                   % (res.func, len(res.skipped_pickup), _where(res.func, res.skipped_pickup, True)))
    if res.refused:
        shown = ["%s line %d slot %d %s" % (op, line, slot, path)
                 for _, op, line, slot, path in res.refused[:REFUSED_SHOWN]]
        more = len(res.refused) - REFUSED_SHOWN
        out.append("%s: %d refused by the unit, slot left free (%s%s)"
                   % (res.func, len(res.refused), "; ".join(shown),
                      "; +%d more" % more if more > 0 else ""))
    return "\n".join(out)


def result_json(res):
    d = {"func": res.func, "seed": res.seed, "seed_source": res.seed_source,
         "pattern": res.pattern, "track": res.track, "bank": res.bank,
         "length": res.length, "length_source": res.length_source,
         "model": res.model.to_json(), "cost": res.cost, "max_cost": res.max_cost,
         "draws": {k.lower(): v for k, v in res.draws.items()},
         "out_of_range": [{"func": f, "op": o, "line": ln} for f, o, ln in res.oor],
         "skipped_no_trig": [{"func": f, "op": o, "line": ln} for f, o, ln in res.skipped],
         "skipped_pickup": [{"func": f, "op": o, "line": ln, "track": t}
                            for f, o, ln, t in res.skipped_pickup],
         "refused": [{"func": f, "op": o, "line": ln, "slot": sl, "path": p}
                     for f, o, ln, sl, p in res.refused],
         "not_on_unit": res.not_on_unit, "warnings": res.warnings,
         "globals": res.globals, "state": res.state}
    return d


# ---- command line ----------------------------------------------------------------------------
RUN_HELP = """usage: otype.py run FUNC FILE.OTY [FILE2.OTY ...] [options]

Run FUNC (a function with no argument) on a pattern and show what it did.  All files are
loaded together, like `check`.  Nothing is ever written to a bank file or to a card.

options:
  --seed N         the system ticks: all five random streams are seeded as if `seed N` ran at
                   the start (without it: the clock, printed so the run can be replayed)
  --bank FILE      start from pattern --pattern (1..16, default 1) of a bank file (read only)
  --pattern P      pattern of the bank file (1..16)
  --track T        the current track of the host (default 1)
  --len N          pattern length 1..64 (default: the bank's, else 16)
  --card DIR       the CF card root: `dir "AUDIO/DRUMS"` lists DIR/AUDIO/DRUMS (default: no files)
  --slots FILE     JSON {"1": "AUDIO/x.wav", ...}: the slots the unit already holds (default: all free)
  --refuse PATTERN the unit refuses to load a file whose LAST path component matches PATTERN
                   (`*` joker, ASCII case-insensitive; may be repeated): the slot is left free
  --state FILE     JSON of what survives between presses (x y z t, flip, o, drunk, every/skip
                   counters, file.pick bags): read if it exists, written back after the run
  --param NAME=V   override a `param` of FUNC (clamped to its min..max); may be repeated
  --json           print the whole resulting model and report as JSON
  --vm             run the compiled table (OTB, spec 14) on the reference VM instead of walking
                   the tree: same output (modules/otype/vm.py)
  -v               show all 8 tracks and every lock

exit status: 0 = ran, 1 = FUNC refused by the checker / unknown / takes arguments / bank error,
             2 = bad usage (option, missing file, unreadable --state)
"""


def parse_args(argv):
    import argparse

    class P(argparse.ArgumentParser):
        def error(self, message):
            raise SimError("usage: " + message)

    ap = P(prog="otype.py run", add_help=False)
    ap.add_argument("func", nargs="?")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--bank")
    ap.add_argument("--pattern", type=int, default=1)
    ap.add_argument("--track", type=int, default=1)
    ap.add_argument("--len", type=int, dest="length")
    ap.add_argument("--card")
    ap.add_argument("--state")
    ap.add_argument("--slots")
    ap.add_argument("--refuse", action="append", default=[])
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--vm", action="store_true")
    ap.add_argument("-v", action="store_true", dest="verbose")
    ap.add_argument("-h", "--help", action="store_true", dest="help")
    return ap.parse_args(argv)


def main(argv, checker=None, out=None, err=None):
    """`otype.py run ...`; `checker` is the tools/otype.py module (its check_files and show)."""
    out = out or sys.stdout
    err = err or sys.stderr
    if checker is None:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "tools"))
        import otype as checker
    try:
        a = parse_args(argv)
        if a.help or not a.func or not a.files:
            print(RUN_HELP, file=out if a.help else err)
            return 0 if a.help else 2
        params = {}
        for p in a.param:
            if "=" not in p:
                raise SimError("usage: --param NAME=V (got %r)" % p)
            k, v = p.split("=", 1)
            try:
                params[k] = int(v, 0)
            except ValueError:
                raise SimError("usage: --param %s: %r is not a number" % (k, v))
        if not 1 <= a.track <= 8:
            raise SimError("usage: --track must be 1..8 (got %d)" % a.track)
        if not 1 <= a.pattern <= 16:
            raise SimError("usage: --pattern must be 1..16 (got %d)" % a.pattern)
        if a.card is not None and not os.path.isdir(a.card):
            raise SimError("--card %s is not a directory" % a.card)
        if a.bank is not None and not os.path.isfile(a.bank):
            raise SimError("--bank %s is not a file" % a.bank)
        slots = None
        if a.slots:
            try:
                with open(a.slots) as fh:
                    slots = json.load(fh)
                if not isinstance(slots, dict):
                    raise ValueError("not a JSON object")
                slots = _initial_slots(slots)
            except (OSError, ValueError, SimError) as e:
                raise SimError("--slots %s: cannot read it (%s)" % (a.slots, e))
        refuse = None
        if a.refuse:
            pats = list(a.refuse)
            refuse = lambda path: any(wildcard_match(p, path.rsplit("/", 1)[-1]) for p in pats)
        state = None
        if a.state and os.path.exists(a.state):
            try:
                with open(a.state) as fh:
                    state = json.load(fh)
            except (OSError, ValueError) as e:
                raise SimError("--state %s: cannot read it (%s)" % (a.state, e))
    except SimError as e:
        print(str(e), file=err)
        return 2
    for f in a.files:
        if not os.path.isfile(f):
            print("%s is not a file" % f, file=err)
            return 2
    try:
        prog = checker.check_files(a.files)
    except OSError as e:
        print("cannot read %s: %s" % (e.filename, e.strerror), file=err)
        return 2
    name = a.func.upper()
    runner = simulate
    if a.vm:
        from modules.otype import vm
        runner = vm.run_vm
    try:
        res = runner(prog, a.func, seed=a.seed, bank=a.bank, pattern=a.pattern, track=a.track,
                     length=a.length, card=a.card, state=state, params=params,
                     slots=slots, refuse=refuse)
    except SimError as e:
        if str(e) == "refused":
            for d in prog.sorted_diags(include_hidden=True):
                if d.func == O.show(name):
                    print(d, file=out)
            print("%s: refused by the checker, not run" % O.show(name), file=out)
        else:
            print(str(e), file=err)
        return 1
    except (BankError, RunError) as e:
        print("%s: %s" % (O.show(name), e), file=err)
        return 1
    if a.state:
        with open(a.state, "w") as fh:
            json.dump(res.state, fh, indent=1, sort_keys=True)
            fh.write("\n")
    if a.json:
        json.dump(result_json(res), out, indent=1, sort_keys=True)
        print(file=out)
    else:
        print(render_text(res, a.verbose), file=out)
    return 0
