"""Python side of the C VM (firmware/c/vm.c): a ctypes wrapper and the
decoder of the flat export record.

    cvm = CVM("out/otype/libotc.dylib")       # built by `build_c.py --host`
    cvm.load(table_bytes)                     # also resets the persistent state
    r = cvm.run("walk", seed=3)               # one press; the state persists to the next
    cvm.reset_state()
    cvm.bind(2, block, mtype)                 # the pattern bytes of track 2 (RAM TRAC block)
    cvm.blocks()                              # {1..8: bytearray} read back after a run
    slots = cvm.bind_card(tree_root, {1: "AUDIO/x.wav"}, refuse={...}, busy={"dir"})
                                              # the card workspace + HostCard / HostSlots;
    slots.final()                             # {1..128: path}: the unit's slots after the run

`run` returns a dict with the keys of `sim.result_json` that the VM computes -- `cost`, `draws`
(lower-case stream names), `out_of_range`, `skipped_no_trig`, `skipped_pickup`, `globals`, `state`
(with the pick `bags`), `warnings`, `refused`, plus `touched` (set of the tracks whose bytes the run
changed), `part_touched` (the tracks a part write import was called for), `working_set`, `slots_touched` -- or `("error", code)` (OTC_E_*).  A run stopped by a busy card
(OTC_E_BUSY) is NOT an error tuple: it is the dict of everything done up to the access (the simulator's
CardBusy.result) with `busy` True.
`decode_export(words, table)` does the decoding from a plain list of ints, so the same record read
back from ColdFire memory (big-endian: `words_from_be`) decodes the same way.

The record layout is documented in firmware/c/otc_vm.h.
"""
import collections
import copy
import ctypes
import os
import random
import struct

from modules.otype import ops as O
from modules.otype import otb
from modules.otype import sim

E_COST, E_TABLE, E_UNSUPPORTED, E_ARG, E_CAPS, E_BUSY = 1, 2, 3, 4, 5, 6
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
STREAMS = ("rand", "toss", "prob", "drunk", "file")
FSTATE_KEYS = ("flip", "o", "o.min", "o.max", "o.wrap", "o.inc",
               "drunk", "drunk.min", "drunk.max", "drunk.wrap")
HEAD = 14                    # words before the out-of-range records
EXPORT_WORDS = 32768         # room for the biggest record (255 functions, 1024 counters, 64 oor, 8 full bags)
DIR_NAME_MAX = O.DIR_NAME_MAX
MAX_REFUSED = 64             # OTC_MAX_REFUSED of otc_vm.h: refusals kept in the record (the rest is counted)
MAX_DIRWARN = 16             # OTC_MAX_DIRWARN: distinct `dir` warnings kept


def words_from_be(data):
    """A big-endian dump of the export record (int32 words) -> list of ints."""
    n = len(data) // 4
    return list(struct.unpack(">%di" % n, bytes(data[:4 * n])))


def _bytes_at(words, p, nbytes):
    """`nbytes` bytes packed four per big-endian word from words[p:]."""
    nw = (nbytes + 3) // 4
    return b"".join(struct.pack(">I", w & 0xFFFFFFFF) for w in words[p:p + nw])[:nbytes], p + nw


def _decode_card(words, p, table):
    """The card / slot tail of the record (firmware/c/otc_vm.h): warnings, working set, bags, refusals.
    `p` = the `bound` word.  Unbound: the same keys, empty (what the simulator gives without a card)."""
    out = {"warnings": [], "warn_overflow": 0, "working_set": [], "bags": [], "refused": [],
           "refused_overflow": 0, "slots_touched": 0, "bound": words[p]}
    if not words[p]:
        return out
    out["slots_touched"] = words[p + 1]
    nwarn, out["warn_overflow"] = words[p + 2], words[p + 3]
    p += 4
    for _ in range(nwarn):
        kind, n, cap, plen, _nw = words[p:p + 5]
        raw, p = _bytes_at(words, p + 5, plen)
        path = raw.decode("utf-8", "surrogateescape")
        out["warnings"].append(
            'dir "%s": %d names longer than %d characters skipped' % (path, n, DIR_NAME_MAX) if kind == 0
            else 'dir "%s": %d files, only the first %d kept' % (path, n, cap))
    nwork = words[p]
    out["working_set"] = list(words[p + 1:p + 1 + nwork])
    p += 1 + nwork
    nbags = words[p]
    p += 1
    for _ in range(nbags):
        fi, h, n, nrest = words[p:p + 4]
        p += 4
        rest = []
        for k in range(0, nrest, 2):
            w = words[p + k // 2] & 0xFFFFFFFF
            rest.append(w >> 16)
            if k + 1 < nrest:
                rest.append(w & 0xFFFF)
        p += (nrest + 1) // 2
        out["bags"].append({"func": table.funcs[fi].show(), "hash": h & 0xFFFFFFFF, "n": n, "rest": rest})
    nref, out["refused_overflow"] = words[p], words[p + 1]
    p += 2
    for _ in range(nref):
        fi, opid, line, slot, plen, _nw = words[p:p + 6]
        raw, p = _bytes_at(words, p + 6, plen)
        out["refused"].append({"func": table.funcs[fi].show(),
                               "op": O.show(otb.REG[(opid >> 8, opid & 255)].name), "line": line,
                               "slot": slot, "path": raw.decode("utf-8", "surrogateescape")})
    return out


def decode_export(words, table):
    """The export record (list of ints) -> result dict, or ("error", code).

    `table`: the parsed otb.Table the VM ran (names of functions and operations, the (line, modifier)
    key of every every/skip counter)."""
    if words[0] and words[0] != E_BUSY:
        return ("error", words[0])
    n_oor, overflow = words[12], words[13]
    p = HEAD
    oor = []
    for _ in range(n_oor):
        fi, opid, line = words[p:p + 3]
        p += 3
        oor.append({"func": table.funcs[fi].show(), "op": O.show(otb.REG[(opid >> 8, opid & 255)].name),
                    "line": line})
    nfunc = words[p]
    p += 1
    funcs = {}
    for k in range(nfunc):
        touched = words[p]
        st = dict(zip(FSTATE_KEYS, words[p + 1:p + 11]))
        nctr = words[p + 11]
        p += 12
        every = {}
        for m in range(nctr):
            present, val = words[p:p + 2]
            p += 2
            if present:
                line, midx = table.funcs[k].counters[m]
                every[str(line) if midx == 0 else "%d.%d" % (line, midx)] = val
        if touched:
            st["every"] = every
            funcs[table.funcs[k].show()] = st
    skip, skip_overflow = [], words[p + 1]
    n_skip = words[p]
    p += 2
    for _ in range(n_skip):
        fi, opid, line = words[p:p + 3]
        p += 3
        skip.append({"func": table.funcs[fi].show(), "op": O.show(otb.REG[(opid >> 8, opid & 255)].name),
                     "line": line})
    pick, pick_overflow = [], words[p + 1]
    n_pick = words[p]
    p += 2
    for _ in range(n_pick):
        fi, opid, line, track = words[p:p + 4]
        p += 4
        pick.append({"func": table.funcs[fi].show(), "op": O.show(otb.REG[(opid >> 8, opid & 255)].name),
                     "line": line, "track": track})
    touched = {t for t in range(1, 9) if words[p] >> (t - 1) & 1}
    part_touched = {t for t in range(1, 9) if words[p + 1] >> (t - 1) & 1}
    p += 2
    ext = _decode_card(words, p, table)
    g = dict(zip("xyzt", words[8:12]))
    return {"cost": words[1], "executed": words[2], "draws": dict(zip(STREAMS, words[3:8])),
            "out_of_range": oor, "oor_overflow": overflow, "globals": g,
            "skipped_no_trig": skip, "skip_overflow": skip_overflow,
            "skipped_pickup": pick, "pickup_overflow": pick_overflow, "touched": touched,
            "part_touched": part_touched, "state": {"globals": dict(g), "funcs": funcs, "bags": ext.pop("bags")},
            "busy": words[0] == E_BUSY, **ext}


def _utf8(b):
    return b.decode("utf-8", "surrogateescape")


def _bytes(s):
    return s.encode("utf-8", "surrogateescape")


class HostCard:
    """The card as the walker imports see it (otb_host_dir_*): a real directory tree walked in a SHUFFLED
    order (seeded, different at every `dir_open`), so that the C sort is really exercised.  `root` = the
    Set folder (None: no card).  The rules of the walker contract: no name with a component that starts with
    '.', nothing longer than 259 bytes of UTF-8 (counted in `skipped`), at most 15 folders below the opened one
    (sim O.DIR_DEPTH_MAX; deeper files are silently not visited), a folder with a character above U+00FF is absent.  `leak_dots` breaks the first rule on purpose
    (must-fail tests); `busy_after` = n makes dir_next answer -2 (busy) once n names were given."""

    def __init__(self, root=None, seed=0, leak_dots=False, busy_after=None):
        self.root, self.seed, self.leak_dots, self.busy_after = root, seed, leak_dots, busy_after
        self.opened = 0
        self.items = []
        self.skipped = 0
        self.given = 0
        self.log = []

    def listing(self, canon, deep):
        """[relative name] of root/canon (sim.list_card without the sort), or None when absent."""
        if self.root is None:
            return None
        base = sim._resolve(self.root, canon)
        if base is None:
            return None
        out = []

        def walk(d, rel, level):                 # level 0 = the opened folder; the unit's walker goes 15 folders down
            try:
                names = os.listdir(d)
            except OSError:
                return
            for n in names:
                if n.startswith(".") and not self.leak_dots:
                    continue
                full = os.path.join(d, n)
                if os.path.isdir(full):
                    if deep and level < O.DIR_DEPTH_MAX:
                        walk(full, rel + n + "/", level + 1)
                elif os.path.isfile(full):
                    out.append(rel + n)
        walk(base, "", 0)
        return out

    def dir_open(self, canon, deep):
        self.log.append(("open", canon, deep))
        self.opened += 1
        self.items, self.skipped, self.given = [], 0, 0
        if any(ord(c) > 0xFF for c in canon):    # the on-unit card implementation recodes the folder to Latin-1: a character above U+00FF is not a folder it can ask for
            return -1
        names = self.listing(canon, deep)
        if names is None:
            return -1
        random.Random("%s/%d" % (self.seed, self.opened)).shuffle(names)
        for n in names:
            b = _bytes(n)
            if len(b) > 259:
                self.skipped += 1
            else:
                self.items.append(b)
        self.items.reverse()
        return 0

    def dir_next(self):
        if self.busy_after is not None and self.given >= self.busy_after:
            return -2, b""
        if not self.items:
            return 0, b""
        self.given += 1
        return 1, self.items.pop()


class HostSlots:
    """The unit's slots as the slot imports see them: slot0 -> the stored path ("../" + canonical path).
    `slots` = the sim's initial slots ({1..128: path}); `refuse` / `busy` = the sim's arguments of the same
    names (a predicate, or a set; `busy` is asked once per card access, with the op name).  `final()` =
    {1..128: canonical path} like the sim's `model.slots`; it insists on the "../"."""

    def __init__(self, slots=None, refuse=None, busy=None, store_limit=None):
        self.d = {n - 1: "../" + p for n, p in sim._initial_slots(slots).items()}
        self.refuse = sim._refuse_predicate(refuse)
        self.busy = sim._busy_predicate(busy)
        self.store_limit = store_limit
        self.calls = collections.Counter()
        self.asked = []

    def name(self, slot0):
        return len(_bytes(self.d.get(slot0, "")))

    def set(self, slot0, path):
        self.calls["set"] += 1
        if self.store_limit is not None and len(_bytes(path)) > self.store_limit:
            return -1                           # the on-unit card implementation: more than the limit in bytes of UTF-8
        if any(ord(c) > 0xFF for c in path):    # the on-unit card implementation stores Latin-1: a character above U+00FF cannot be stored (a refusal)
            return -1
        self.d[slot0] = path
        return 0

    def load(self, slot0):
        self.calls["load"] += 1
        path = self.d.get(slot0, "")
        if self.refuse is not None and self.refuse(path[3:] if path.startswith("../") else path):
            self.d.pop(slot0, None)                     # the unit clears the slot (a stock routine)
            return 0
        return 1

    def clr(self, slot0):
        self.calls["clr"] += 1
        self.d.pop(slot0, None)

    def card_busy(self, op):
        name = otb.REG[(op >> 8, op & 255)].name
        self.asked.append(name)
        return 1 if self.busy is not None and self.busy(name) else 0

    def final(self):
        out = {}
        for s, p in self.d.items():
            if not p.startswith("../"):
                raise AssertionError("slot %d holds %r: no `../` in front of the stored path" % (s + 1, p))
            out[s + 1] = p[3:]
        return dict(sorted(out.items()))

    def mutated(self):
        return sum(self.calls[k] for k in ("set", "load", "clr")) > 0


class HostParams:
    """The part as the imports see it: the host twin of what the unit's asm reads from the descriptor and the part
    (otb_host_param_info / otb_host_param_value) and of what the part writes do (otb_host_par_set ...
    otb_host_trk_slot_init).  It is built on the simulator's own model (sim.ParamModel) and answers RAW, un-normalised
    words (the C normalises, sim.param_norm / chooser_norm do the same on the other side); what the model does not list
    is neutral.  The writes land in `parts` {track 1..8: sim.PartData}, kept as the simulator keeps its own (an effect
    apply forgets what was written on its page; `value` answers what was written, else the model), so `part_json()` is
    compared with the simulator's `model.part`.  This twin is INDEPENDENT of sim.py's handlers: it only records.
    `log` = every call, in order (("info" | "value" | "par_set" | ..., args...)): the ColdFire run must make the same."""

    def __init__(self, pmodel=None, pattern=1):
        self.pm = pmodel if pmodel is not None else sim.ParamModel()
        self.log = []
        self.model = sim.Model()
        self.model.go_pattern(1, pattern)
        self.model.pmodel = self.pm
        self.parts = self.model.part
        self.track_blocks = {}

    def info(self, track0, p):
        self.log.append(("info", track0, p))
        lo, count, en = self.pm.info.get((track0 + 1, p), (0, sim.VAL_MAX + 1, True))
        return lo & 0xFFFFFFFF, count & 0xFFFFFFFF, 1 if en else 0

    def value(self, track0, p):
        self.log.append(("value", track0, p))
        pr = self.parts[track0 + 1].params
        return pr[p] if p in pr else self.pm.param_value(track0 + 1, p)

    # ---- the part ----------------------------------------------------------------------------------------------------
    def par_set(self, track0, p, v):
        self.log.append(("par_set", track0, p, v))
        self.parts[track0 + 1].params[p] = v

    def param_default(self, track0, p):
        self.log.append(("default", track0, p))
        return self.pm.raw_default(track0 + 1, p)

    def lfo_set(self, track0, lfo0, what, v):
        self.log.append(("lfo", track0, lfo0, what, v))
        pt = self.parts[track0 + 1]
        (pt.lfo_dest if what == 0 else pt.lfo_wave)[lfo0 + 1] = v

    def lfo_waves(self):
        self.log.append(("waves",))
        return self.pm.raw_lfo_waves()

    def fx_count(self, slot0):
        self.log.append(("fx_count", slot0))
        return self.pm.raw_fx_count(slot0 + 1)

    def fx_default(self, slot0):
        self.log.append(("fx_default", slot0))
        return self.pm.raw_fx_default(slot0 + 1)

    def fx_set(self, track0, slot0, index):
        """The chooser apply: the effect, and its own defaults on its page (what was written there is forgotten)."""
        self.log.append(("fx_set", track0, slot0, index))
        pt = self.parts[track0 + 1]
        pt.fx[slot0 + 1] = index
        for k in range(6):
            pt.params.pop(18 + 6 * slot0 + k, None)

    def scene_put(self, track0, scene0, byte, v):
        self.log.append(("scene", track0, scene0, byte, v))
        self.parts[track0 + 1].scenes.setdefault(scene0 + 1, {})[byte] = v

    def trk_slot_init(self, track0):
        self.log.append(("slot_init", track0))
        self.parts[track0 + 1].slot = "factory"

    def trk_slot(self, track0, slot0):
        self.log.append(("trk_slot", track0, slot0))
        self.parts[track0 + 1].slot = slot0 + 1

    def tempo(self, bpm):
        self.log.append(("tempo", bpm))
        self.model.tempo = bpm

    @property
    def tempo_val(self):
        return self.model.tempo

    def control_read(self, name, track0, n):
        t = track0 + 1
        if name == "PAT.CUR": return self.model.pattern
        if name == "BANK.CUR": return self.model.bank
        if name == "PART.CUR": return self.model.part_cur
        if name == "PAT.LEN": return self.model.pattern_lengths.get((self.model.bank, self.model.pattern), 16)
        if name == "TEMPO": return self.tempo_val
        if name == "FILL": return self.model.fill
        if name == "TRK.MUTE": return self.model.mutes[t]
        pt = self.parts[t]
        if name == "TRK.SLOT":
            if pt.slot == "factory": return t if pt.mtype <= 1 else 128 + t if pt.mtype == sim.PICKUP else 1
            return pt.slot if isinstance(pt.slot, int) else 1
        if name == "LFOD.GET": return pt.lfod[n - 1]
        if name == "SCN.GET": return pt.scenes.get(n // 32 + 1, {}).get(n % 32, sim.NOT_LOCKED)
        field = name.lower().replace(".", "_")
        if name.startswith("TRK."):
            return getattr(pt, name.split(".")[1].lower())
        return getattr(pt, field).get(n, self.pm.setup_values.get((t, name, n), 0))

    def control_write(self, name, track0, n, v):
        t, pt = track0 + 1, self.parts[track0 + 1]
        if name == "TRK.TYPE":
            if not 0 <= v <= 4 or (v == 3 and t in (1, 5)): return 0
            pt.mtype = v
        elif name == "TRK.SLOT":
            if not 1 <= v <= 128 or pt.mtype > 1 or (pt.mtype == 1 and v not in self.pm.flex_slots): return 0
            self.trk_slot(track0, v - 1)
        elif name == "TRK.MUTE": self.model.mutes[t] = int(bool(v))
        elif name in ("TRK.LEVEL", "TRK.CUE"):
            setattr(pt, name.split(".")[1].lower(), sim.clamp(v, 0, 127))
        elif name == "LFOD.SET": pt.lfod[n - 1] = sim.clamp(v, -64, 63)
        elif name == "LFOD.INTERP": pt.lfod_interp[n - 1] = int(bool(v))
        else:
            if name == "LFO.WAVE":
                rg = (0, self.pm.waves() - 1) if self.pm.waves() else None
            elif name == "LFO.MULT": rg = (0, 6)
            elif name == "LFO.TRIG": rg = (0, 7)
            else: rg = self.pm.setup_range(t, name, n)
            if rg is None: return 0
            getattr(pt, name.lower().replace(".", "_"))[n] = sim.clamp(v, *rg)
        if name not in ("TRK.MUTE", "TRK.SLOT"):
            self.log.append(("control", track0, name, n, v))
        return 1

    def control_action(self, cv, name, a, b):
        if name == "FILL":
            self.model.fill = int(bool(a))
            if not a: self.model.fill_once = 0
        elif name == "FILL.ONCE": self.model.fill_once = 1
        elif name == "OS.ARM": self.model.tracks[a + 1].os_armed = 1
        else:
            self.track_blocks[self.model.bank, self.model.pattern] = cv.blocks()
            if name == "PAT.GO": self.model.go_pattern(b, a)
            elif name == "PART.GO": self.model.go_part(a)
            elif name == "PART.SAVE":
                target = self.parts if a == self.model.part_cur else self.model.parts.setdefault(
                    (self.model.bank, a), {t: sim.PartData() for t in range(1, 9)})
                self.model.saved_parts[self.model.bank, a] = copy.deepcopy(target)
            elif name == "PART.RELOAD":
                if (self.model.bank, a) not in self.model.saved_parts: return 4
                self.model.parts[self.model.bank, a] = copy.deepcopy(self.model.saved_parts[self.model.bank, a])
                if a == self.model.part_cur: self.model.part = self.model.parts[self.model.bank, a]
            else: return 3
            self.parts = self.model.part
            key = self.model.bank, self.model.pattern
            if key not in self.track_blocks:
                blocks = {}
                for t in range(1, 9):
                    blk = bytearray(sim.RAM_TRAC_SIZE)
                    blk[0x40:0x48] = sim.SWING_DEFAULT.to_bytes(8, "big")
                    blk[0x50] = 16
                    blk[0x59:0x859] = bytes([255]) * 0x800
                    blocks[t] = blk
                self.track_blocks[key] = blocks
            for t, blk in self.track_blocks[key].items():
                cv.bind(t, blk, self.parts[t].mtype, copy=True)
        return 0

    def part_json(self):
        """{"1".."8": the PartData JSON without the machine type}: what the simulator's model.part says."""
        out = {}
        for t, pt in self.parts.items():
            j = pt.to_json()
            j.pop("type")
            out[str(t)] = j
        return out


class HostAudio:
    """Mock / host implementation of OType Audio imports for CVM testing."""

    def __init__(self, source="AUDIO/SOURCE.WAV", empty=False, recording=False, exists=None, refuse=None):
        self.source = source
        self.source_empty = empty
        self.recording = recording
        self.flex_slots = {}
        self.flex_playing = set()
        self.cat_list = []
        self.normalized = {}
        self.exists = set(exists or [])
        self.refuse = dict(refuse or {})
        self.input_mode = 1 if source else 0
        self.mutations = 0

    def mutated(self):
        return self.mutations > 0

    def audio_input(self):
        return self.input_mode

    def src_save(self, out_ptr, cap):
        if "SRC.SAVE" in self.refuse:
            return self.refuse["SRC.SAVE"]
        if not self.source:
            return -3
        if self.recording:
            return -6
        if self.source_empty:
            return -4
        if self.source in self.exists:
            return -7
        b = self.source.encode("utf-8") + b"\0"
        if len(b) > cap:
            return -1
        ctypes.memmove(out_ptr, b, len(b))
        return 0

    def flex_load(self, slot0, path):
        if "FLEX.LOAD" in self.refuse:
            return self.refuse["FLEX.LOAD"]
        if slot0 in self.flex_playing:
            return -6
        self.flex_slots[slot0] = path
        self.mutations += 1
        return 0

    def flex_free(self, slot0):
        if "FLEX.FREE" in self.refuse:
            return self.refuse["FLEX.FREE"]
        return int(slot0 not in self.flex_slots)

    def flex_del(self, slot0):
        if "FLEX.DEL" in self.refuse:
            return self.refuse["FLEX.DEL"]
        if slot0 not in self.flex_slots:
            return 0
        if slot0 in self.flex_playing:
            return -6
        del self.flex_slots[slot0]
        self.mutations += 1
        return 0

    def cat_clr(self):
        self.cat_list.clear()

    def cat_add(self, path):
        if "CAT.ADD" in self.refuse:
            return self.refuse["CAT.ADD"]
        if len(self.cat_list) >= 64:
            return -12
        self.cat_list.append(path)
        return 0

    def cat_save(self, name, out_ptr, cap):
        if "CAT.SAVE" in self.refuse:
            return self.refuse["CAT.SAVE"]
        if not self.cat_list:
            return -4
        dest_dir = "AUDIO"
        if self.source:
            sp = self.source.lstrip("/")
            if "/" in sp:
                dest_dir = sp.rsplit("/", 1)[0]
        out_name = f"{dest_dir}/{name}.WAV" if dest_dir else f"{name}.WAV"
        if out_name in self.exists:
            return -7
        b = out_name.encode("utf-8") + b"\0"
        if len(b) > cap:
            return -1
        ctypes.memmove(out_ptr, b, len(b))
        return 0

    def norm(self, path, db):
        if not -12 <= db <= 0:
            return -1
        if "NORM" in self.refuse:
            return self.refuse["NORM"]
        self.normalized[path] = db
        return 0


class CVM:
    """One otc_vm_t in Python-owned memory plus the table it runs."""

    def __init__(self, lib_path):
        L = self.lib = ctypes.CDLL(str(lib_path))
        L.otc_vm_sizeof.restype = ctypes.c_uint32
        L.otc_vm_load.restype = ctypes.c_int
        L.otc_vm_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        L.otc_vm_reset_state.argtypes = [ctypes.c_void_p]
        L.otc_vm_run.restype = ctypes.c_int
        L.otc_vm_run.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint8,
                                 ctypes.c_uint8]
        L.otc_vm_export.restype = ctypes.c_uint32
        L.otc_vm_export.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32), ctypes.c_uint32]
        L.otc_vm_bind.restype = None
        L.otc_vm_bind.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_uint]
        L.otc_vm_msg.restype = ctypes.c_char_p
        L.otc_vm_msg.argtypes = [ctypes.c_void_p]
        L.otc_vm_touched.restype = ctypes.c_uint32
        L.otc_vm_touched.argtypes = [ctypes.c_void_p]
        L.otc_ws_size.restype = ctypes.c_uint32
        L.otc_vm_bind_work.restype = None
        L.otc_vm_bind_work.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.otc_vm_slots_touched.restype = ctypes.c_uint32
        L.otc_vm_slots_touched.argtypes = [ctypes.c_void_p]
        L.otc_vm_fn_page.restype = ctypes.c_uint32            # page functions
        L.otc_vm_fn_page.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_char_p)]
        L.otc_vm_fn_confirm.restype = ctypes.c_uint32         # the text of `confirm "TEXT"`
        L.otc_vm_fn_confirm.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_char_p)]
        L.otc_vm_fn_input_sample.restype = ctypes.c_uint32    # input sample
        L.otc_vm_fn_input_sample.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        L.otc_vm_fn_param.restype = ctypes.c_int
        L.otc_vm_fn_param.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        L.otc_vm_set_params.restype = None
        L.otc_vm_set_params.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_uint32]
        L.otc_vm_set_param_folder.restype = None
        L.otc_vm_set_param_folder.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
        L.otb_shim_set.restype = None
        L.otb_shim_set.argtypes = [ctypes.c_void_p] * 8
        L.otb_shim_set_audio.restype = None                   # audio imports
        L.otb_shim_set_audio.argtypes = [ctypes.c_void_p] * 9
        L.otb_shim_set_params.restype = None                  # the part's descriptor / value
        L.otb_shim_set_params.argtypes = [ctypes.c_void_p] * 2
        L.otb_shim_set_part.restype = None                    # the part writes
        L.otb_shim_set_part.argtypes = [ctypes.c_void_p] * 11
        L.otb_shim_set_control.argtypes = [ctypes.c_void_p] * 4
        L.otc_vm_part_touched.restype = ctypes.c_uint32
        L.otc_vm_part_touched.argtypes = [ctypes.c_void_p]
        self.size = L.otc_vm_sizeof()
        self.ws_size = L.otc_ws_size()
        self.ws = None                       # the card workspace, allocated by the first bind_card
        self.card = self.slots = None        # the HostCard / HostSlots bound now
        self.errors = []                     # exceptions raised inside a callback (the test asserts none)
        self._cbs = ()
        self.bufs, self.copies = {}, {}      # track -> the ctypes buffer bound to it (the VM points at it)
        self.mtypes = {}
        self.mem = (ctypes.c_uint64 * ((self.size + 7) // 8))()      # 8-aligned, zeroed
        self.out = (ctypes.c_int32 * EXPORT_WORDS)()
        self.table = self.data = None
        self.params = None                   # the HostParams bound now
        self.audio = None                    # the HostAudio bound now (audio)
        self._pcbs = self._mcbs = self._acbs = ()
        self._folder_bufs = {}
        self.bind_params(HostParams())       # the neutral part until a test binds another
        self.bind_audio(HostAudio())         # the neutral audio host

    @property
    def ptr(self):
        return ctypes.addressof(self.mem)

    def load(self, table):
        """Load OTB bytes (or an otb.Table's data). Returns 0 or the OTC_E_* code of the refusal."""
        data = bytes(table.data if hasattr(table, "data") else table)
        self.data = ctypes.create_string_buffer(data, len(data))      # kept alive: the VM points at it
        self.bufs, self.copies = {}, {}                               # (otc_vm_load clears the bindings)
        self.unbind_card()                                            # ... and so does this wrapper, for the card
        self.table = otb.parse(data)
        return self.lib.otc_vm_load(self.ptr, self.data, len(data))

    def msg(self):
        """The text of the last msg of the run ("" = none: the end box says DONE)."""
        return self.lib.otc_vm_msg(self.ptr).decode("latin-1")

    def bind(self, track, block, mtype=0, copy=False):
        """Bind track 1..8 to a COPY of `block` (bytes / bytearray of 0x91a: the RAM TRAC block).
        `copy`: also give the VM a second buffer, the "SRAM copy", that starts equal (the Mac tests
        otherwise pass copy = NULL).  `block` None unbinds.  Bind again after every `load`; bind fresh
        blocks before every press when each press must start from the same pattern."""
        self.mtypes[track] = mtype
        if self.params is not None:
            self.params.parts[track].mtype = mtype
        if block is None:
            self.bufs.pop(track, None)
            self.copies.pop(track, None)
            self.lib.otc_vm_bind(self.ptr, track, None, None, mtype)
            return
        buf = (ctypes.c_uint8 * len(block)).from_buffer_copy(bytes(block))
        self.bufs[track] = buf
        cbuf = None
        if copy:
            cbuf = self.copies[track] = (ctypes.c_uint8 * len(block)).from_buffer_copy(bytes(block))
        else:
            self.copies.pop(track, None)
        self.lib.otc_vm_bind(self.ptr, track, ctypes.addressof(buf),
                             ctypes.addressof(cbuf) if cbuf is not None else None, mtype)

    def blocks(self):
        """{track: bytearray}: the bytes of the bound blocks as they are now."""
        return {t: bytearray(b) for t, b in self.bufs.items()}

    def copy_blocks(self):
        """{track: bytearray}: the SRAM copies (only the tracks bound with copy=True)."""
        return {t: bytearray(b) for t, b in self.copies.items()}

    def touched(self):
        return {t for t in range(1, 9) if self.lib.otc_vm_touched(self.ptr) >> (t - 1) & 1}

    def part_touched(self):
        return {t for t in range(1, 9) if self.lib.otc_vm_part_touched(self.ptr) >> (t - 1) & 1}

    # ---- the part's parameters ----------------------------------------------------------
    def bind_params(self, params=None):
        """Bind the host side of otb_host_param_info / otb_host_param_value: a HostParams, a sim.ParamModel
        (wrapped), or None (the shim's own neutral answers: 0..127, enabled, value 0, no log).  Survives `load`;
        bind a fresh one before each press that must start from the same part.  Returns the HostParams."""
        T = ctypes
        if params is None:
            self.params, self._pcbs, self._mcbs = None, (), ()
            self.lib.otb_shim_set_params(None, None)
            self.lib.otb_shim_set_part(*([None] * 11))
            self.lib.otb_shim_set_control(*([None] * 4))
            return None
        hp = params if isinstance(params, HostParams) else HostParams(params)
        self.params = hp
        for t, ty in self.mtypes.items():
            hp.parts[t].mtype = ty

        def guard(f, bad):
            def g(*a):
                try:
                    return f(*a)
                except BaseException as e:           # a callback must never raise into C
                    self.errors.append(e)
                    return bad
            return g

        def p_info(t0, p, lo, count, en):
            a, b, c = hp.info(t0, p)
            lo[0], count[0], en[0] = a, b, c

        U, I, F = T.c_uint32, T.c_int32, T.CFUNCTYPE
        self._pcbs = (
            F(None, U, U, T.POINTER(U), T.POINTER(U), T.POINTER(U))(guard(p_info, None)),
            F(I, U, U)(guard(hp.value, 0)))
        self._mcbs = (                                             # in otb_shim_set_part's order
            F(None, U, U, I)(guard(hp.par_set, None)),
            F(I, U, U)(guard(hp.param_default, 0)),
            F(None, U, U, U, U)(guard(hp.lfo_set, None)),
            F(U)(guard(hp.lfo_waves, 0)),
            F(U, U)(guard(hp.fx_count, 0)),
            F(U, U)(guard(hp.fx_default, 0)),
            F(None, U, U, U)(guard(hp.fx_set, None)),
            F(None, U, U, U, U)(guard(hp.scene_put, None)),
            F(None, U)(guard(hp.trk_slot_init, None)),
            F(None, U, U)(guard(hp.trk_slot, None)),
            F(None, I)(guard(hp.tempo, None)))
        self.lib.otb_shim_set_params(*[T.cast(f, T.c_void_p) for f in self._pcbs])
        self.lib.otb_shim_set_part(*[T.cast(f, T.c_void_p) for f in self._mcbs])
        names = {k + 1: op.name for k, op in enumerate(sorted(O.ALL_OPS, key=lambda o: o.id))}
        self._vcbs = (
            F(I, U, U, U)(guard(lambda h, t, n: hp.control_read(names[h], t, n), 0)),
            F(I, U, U, U, I)(guard(lambda h, t, n, v: hp.control_write(names[h], t, n, v), 0)),
            F(I, T.c_void_p, U, I, I)(guard(lambda ptr, h, a, b: hp.control_action(self, names[h], a, b), 3)))
        self.lib.otb_shim_set_control(*[T.cast(f, T.c_void_p) for f in self._vcbs], None)
        return hp

    # ---- audio imports ----------------------------------------------------------------
    def bind_audio(self, audio=None):
        """Bind the host side of the audio imports (HostAudio). Returns the HostAudio."""
        T = ctypes
        if audio is None:
            self.audio, self._acbs = None, ()
            self.lib.otb_shim_set_audio(*([None] * 9))
            return None
        ha = audio if isinstance(audio, HostAudio) else HostAudio(**audio)
        self.audio = ha

        def guard(f, bad):
            def g(*a):
                try:
                    return f(*a)
                except Exception as ex:
                    self.errors.append(ex)
                    return bad
            return g

        def src_save(out_ptr, cap):
            return ha.src_save(out_ptr, cap)

        def cat_save(name, out_ptr, cap):
            return ha.cat_save(name.decode("utf-8", "surrogateescape"), out_ptr, cap)

        U, I, F, C_STR = T.c_uint32, T.c_int32, T.CFUNCTYPE, T.c_char_p
        self._acbs = (
            F(U)(guard(ha.audio_input, 0)),
            F(I, T.c_void_p, U)(guard(src_save, -3)),
            F(I, U, C_STR)(guard(lambda s, p: ha.flex_load(s, p.decode("utf-8", "surrogateescape")), 0)),
            F(I, U)(guard(ha.flex_free, 1)),
            F(I, U)(guard(ha.flex_del, 0)),
            F(None)(guard(ha.cat_clr, None)),
            F(I, C_STR)(guard(lambda p: ha.cat_add(p.decode("utf-8", "surrogateescape")), 0)),
            F(I, C_STR, T.c_void_p, U)(guard(cat_save, -4)),
            F(I, C_STR, I)(guard(lambda p, db: ha.norm(p.decode("utf-8", "surrogateescape"), db), 0)),
        )
        self.lib.otb_shim_set_audio(*[T.cast(f, T.c_void_p) for f in self._acbs])
        return ha

    # ---- the card and the slots -------------------------------------------------------
    def bind_card(self, card=None, slots=None, refuse=None, busy=None, seed=0):
        """Bind the card workspace and the host side of the imports.  `card` = a directory (the Set folder),
        a HostCard, or None (no card: every `dir` finds nothing); `slots` = {1..128: path} the unit holds at
        the start, or a HostSlots; `refuse`, `busy` as the simulator's arguments.  Bind again before every
        press (fresh slots); the pick bags persist in the workspace between presses (load / reset_state
        empty them).  Returns the HostSlots: `.final()` is the unit's slot table after the run."""
        if self.ws is None:
            self.ws = (ctypes.c_uint64 * ((self.ws_size + 7) // 8))()
        hc = card if isinstance(card, HostCard) else HostCard(card, seed)
        hs = slots if isinstance(slots, HostSlots) else HostSlots(slots, refuse, busy)
        self.card, self.slots = hc, hs
        T = ctypes
        C = T.CFUNCTYPE

        def guard(f, bad):
            def g(*a):
                try:
                    return f(*a)
                except BaseException as e:           # a callback must never raise into C
                    self.errors.append(e)
                    return bad
            return g

        def d_open(it, canon, deep):
            return hc.dir_open(_utf8(canon), bool(deep))

        def d_next(it, out):
            rc, name = hc.dir_next()
            if rc == 1:
                T.memmove(out, name + b"\0", len(name) + 1)
            return rc

        def s_name(slot0, out, mx):
            b = _bytes(hs.d.get(slot0, ""))
            if out and mx:
                k = min(len(b), mx - 1)
                T.memmove(out, b[:k] + b"\0", k + 1)
            return len(b)

        self._cbs = (
            C(T.c_int32, T.c_void_p, T.c_char_p, T.c_uint32)(guard(d_open, -1)),
            C(T.c_int32, T.c_void_p, T.c_void_p)(guard(d_next, 0)),
            C(T.c_uint32, T.c_void_p)(guard(lambda it: hc.skipped, 0)),
            C(T.c_int32, T.c_uint32, T.c_void_p, T.c_uint32)(guard(s_name, 0)),
            C(T.c_int32, T.c_uint32, T.c_char_p)(guard(lambda s, p: hs.set(s, _utf8(p)), -1)),
            C(T.c_int32, T.c_uint32)(guard(hs.load, 0)),
            C(None, T.c_uint32)(guard(hs.clr, None)),
            C(T.c_int32, T.c_uint32)(guard(hs.card_busy, 0)))
        self.lib.otb_shim_set(*[T.cast(f, T.c_void_p) for f in self._cbs])
        self.lib.otc_vm_bind_work(self.ptr, T.addressof(self.ws))
        return hs

    def unbind_card(self):
        """Back to the behaviour without a workspace: the card / slot operations stop the run with OTC_E_UNSUPPORTED."""
        self.lib.otc_vm_bind_work(self.ptr, None)
        self.lib.otb_shim_set(*([None] * 8))
        self.card = self.slots = None
        self._cbs = ()

    def slots_touched(self):
        return self.lib.otc_vm_slots_touched(self.ptr)

    def reset_state(self):
        self.lib.otc_vm_reset_state(self.ptr)

    # ---- page functions ------------------------------------------------------------------
    def set_params(self, vals):
        """Impose the root function's params for the next runs (otc_vm_set_params): a list of ints, one per param in
        declaration order (cut at the first missing one); [] clears."""
        vals = list(vals)
        arr = (ctypes.c_int16 * max(1, len(vals)))(*vals)
        self.lib.otc_vm_set_params(self.ptr, arr, len(vals))

    def set_param_folder(self, k, path):
        """Impose a folder path for param k of the root function (otc_vm_set_param_folder); None clears."""
        if path is None:
            self._folder_bufs.pop(k, None)
            self.lib.otc_vm_set_param_folder(self.ptr, k, None, 0)
        else:
            b = path.encode("latin-1")
            buf = ctypes.create_string_buffer(b, len(b))
            self._folder_bufs[k] = buf
            self.lib.otc_vm_set_param_folder(self.ptr, k, buf, len(b))

    def fn_page(self, func):
        """The page label of function `func` (a name or an index) as text, or None (otc_vm_fn_page)."""
        idx = func if isinstance(func, int) else self.table.fmap[func.upper()].index
        lab = ctypes.c_char_p()
        n = self.lib.otc_vm_fn_page(self.ptr, idx, ctypes.byref(lab))
        if not n:
            return None
        return ctypes.string_at(ctypes.cast(lab, ctypes.c_void_p).value, n).decode("latin-1")

    def fn_confirm(self, func):
        """The text of `confirm "TEXT"` of function `func` (a name or an index), or None (otc_vm_fn_confirm)."""
        idx = func if isinstance(func, int) else self.table.fmap[func.upper()].index
        txt = ctypes.c_char_p()
        n = self.lib.otc_vm_fn_confirm(self.ptr, idx, ctypes.byref(txt))
        if not n:
            return None
        return ctypes.string_at(ctypes.cast(txt, ctypes.c_void_p).value, n).decode("latin-1")

    def fn_input_sample(self, func):
        """True if function `func` (a name or an index) declares `input sample`, else False."""
        idx = func if isinstance(func, int) else self.table.fmap[func.upper()].index
        return bool(self.lib.otc_vm_fn_input_sample(self.ptr, idx))

    def fn_params(self, func):
        """[(name, default, min, max)] of function `func` as otc_vm_fn_param reads them from the table."""
        idx = func if isinstance(func, int) else self.table.fmap[func.upper()].index

        class P(ctypes.Structure):
            _fields_ = [
                ("name", ctypes.c_void_p), ("name_len", ctypes.c_uint32),
                ("d", ctypes.c_int16), ("lo", ctypes.c_int16), ("hi", ctypes.c_int16),
                ("kind", ctypes.c_uint8), ("nlabels", ctypes.c_uint8),
                ("labels", ctypes.c_void_p * 16), ("label_lens", ctypes.c_uint8 * 16),
                ("folder_path", ctypes.c_void_p), ("folder_len", ctypes.c_uint32),
            ]
        out, k = [], 0
        while True:
            p = P()
            if not self.lib.otc_vm_fn_param(self.ptr, idx, k, ctypes.byref(p)):
                return out
            out.append((ctypes.string_at(p.name, p.name_len).decode("latin-1"), p.d, p.lo, p.hi))
            k += 1

    def fn_param_info(self, func, k):
        """Detailed dict for param k of function `func`: name, def, lo, hi, kind, labels, folder_path."""
        idx = func if isinstance(func, int) else self.table.fmap[func.upper()].index

        class P(ctypes.Structure):
            _fields_ = [
                ("name", ctypes.c_void_p), ("name_len", ctypes.c_uint32),
                ("d", ctypes.c_int16), ("lo", ctypes.c_int16), ("hi", ctypes.c_int16),
                ("kind", ctypes.c_uint8), ("nlabels", ctypes.c_uint8),
                ("labels", ctypes.c_void_p * 16), ("label_lens", ctypes.c_uint8 * 16),
                ("folder_path", ctypes.c_void_p), ("folder_len", ctypes.c_uint32),
            ]
        p = P()
        if not self.lib.otc_vm_fn_param(self.ptr, idx, k, ctypes.byref(p)):
            return None
        name = ctypes.string_at(p.name, p.name_len).decode("latin-1")
        kind_names = {0: "INT", 1: "BOOL", 2: "OPTION", 3: "FOLDER"}
        kind = kind_names.get(p.kind, "INT")
        labels = [ctypes.string_at(p.labels[i], p.label_lens[i]).decode("latin-1") for i in range(p.nlabels)]
        folder = ctypes.string_at(p.folder_path, p.folder_len).decode("latin-1") if p.folder_path else None
        return {
            "name": name,
            "def": p.d,
            "lo": p.lo,
            "hi": p.hi,
            "kind": kind,
            "labels": labels,
            "folder_path": folder,
        }

    def run(self, func, seed, track=1, length=16):
        f = self.table.fmap[func.upper()]
        if self.params is not None:
            self.params.model.pattern_lengths[self.params.model.bank, self.params.model.pattern] = length
        self.lib.otc_vm_run(self.ptr, f.index, seed & 0xFFFF, track, length)
        n = self.lib.otc_vm_export(self.ptr, self.out, EXPORT_WORDS)
        assert n <= EXPORT_WORDS, "export record needs %d words" % n
        return decode_export(list(self.out[:n]), self.table)
