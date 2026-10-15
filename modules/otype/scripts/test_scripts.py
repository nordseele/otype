"""The /SCRIPTS set for the unit (modules/otype/scripts/*.OTY): load, profile and behaviour checks.

    python3 -m unittest modules/otype/scripts/test_scripts.py

Runs on the simulator only (no image): what each script writes on a pattern that already holds trigs,
and the loader's limits (category <= 11 characters, name <= 17, <= 64 scripts/category, <= 32 categories)."""
import glob
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import otype as T                      # noqa: E402  tools/otype.py
from modules.otype import sim          # noqa: E402
from modules.otype import menu_limits  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = sorted(glob.glob(os.path.join(HERE, "*.OTY")))
ODD = (1, 3, 5, 7, 9, 11, 13, 15)      # the pattern the behaviour tests start from: trigs on odd steps


def program(profile="m1"):
    srcs = [(os.path.basename(f), open(f, encoding="latin-1").read()) for f in FILES]
    prog = T.check_sources(srcs)
    return prog


def preloaded(steps=ODD, track=1):
    """A Run class whose model starts with red trigs on `steps` of `track` (the simulator has no bank here)."""
    class Pre(sim.Run):
        def __init__(self, prog, root, seed, seed_source, model, *a, **k):
            for s in steps:
                model.tracks[track].put("trig", s)
            super().__init__(prog, root, seed, seed_source, model, *a, **k)
    return Pre


def trigs(r, track=1):
    t = r.model.tracks[track]
    return [s for s in range(1, 17) if t.has_trig(s)]


class TestScripts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prog = program()

    def run_fn(self, name, steps=ODD, seed=3, **params):
        return sim.simulate(self.prog, name, seed=seed, params=params, run_class=preloaded(steps))

    def test_all_load_clean(self):
        diags = [d for d in self.prog.diags]
        self.assertEqual(diags, [], [str(d) for d in diags])
        self.assertGreaterEqual(len([f for f in self.prog.funcs if f.menus]), 20)

    def test_loader_limits(self):
        cats = {}
        for f in self.prog.funcs:
            for levels, act, _line in f.menus:
                cat = levels[0]
                self.assertLessEqual(len(cat), 11, cat)
                self.assertLessEqual(len(act), 17, act)
                cats.setdefault(cat, []).append(act)
        self.assertLessEqual(len(cats), menu_limits.MAX_CATEGORIES)
        for cat, acts in cats.items():
            self.assertLessEqual(len(acts), menu_limits.MAX_SCRIPTS_PER_CATEGORY, cat)

    def test_euclid_replaces(self):
        r = self.run_fn("euclid", hits=4, length=16, rot=0)
        self.assertEqual(trigs(r), [1, 5, 9, 13])

    def test_every_n_offset(self):
        r = self.run_fn("every_n", period=4, offset=2)
        self.assertEqual(trigs(r), [3, 7, 11, 15])

    def test_invert(self):
        r = self.run_fn("invert_trigs")
        self.assertEqual(trigs(r), [2, 4, 6, 8, 10, 12, 14, 16])

    def test_thin_and_add_keep_their_side(self):
        r = self.run_fn("thin_trigs", pct=100)
        self.assertEqual(trigs(r), [])
        r = self.run_fn("add_trigs", dens=100)
        self.assertEqual(trigs(r), list(range(1, 17)))
        r = self.run_fn("add_trigs", dens=0)
        self.assertEqual(trigs(r), list(ODD))

    def test_fill_end(self):
        r = self.run_fn("fill_end", steps=(), last=4, dens=100)
        self.assertEqual(trigs(r), [13, 14, 15, 16])
        self.assertEqual(sorted(r.model.tracks[1].cond), [13, 14, 15, 16])

    def test_smp_walk(self):
        r = self.run_fn("smp_walk", first=3, last=5)
        smp = r.model.tracks[1].smp
        self.assertEqual([smp[s] for s in ODD], [3, 4, 5, 3, 4, 5, 3, 4])

    def test_beat_three_tracks(self):
        r = sim.simulate(self.prog, "beat", seed=1, params={"style": 0, "busy": 0})
        self.assertEqual(trigs(r, 1), [1, 5, 9, 13])
        self.assertEqual(trigs(r, 2), [5, 13])
        self.assertEqual(trigs(r, 3), [1, 3, 5, 7, 9, 11, 13, 15])

    def test_pool_fill_folder_param(self):
        with tempfile.TemporaryDirectory() as card:
            for d, names in (("AUDIO/DRUMS", ("kick1.wav", "snare1.wav")), ("AUDIO/PADS", ("pad.wav",))):
                os.makedirs(os.path.join(card, d))
                for n in names:
                    open(os.path.join(card, d, n), "wb").close()
            r = sim.simulate(self.prog, "slots_fill", seed=1, card=card, params={"src": "AUDIO/PADS"})
            self.assertEqual(sorted(r.model.slots.values()), ["AUDIO/PADS/pad.wav"])
            r = sim.simulate(self.prog, "slots_fill_kind", seed=1, card=card, params={"kind": 0})
            self.assertEqual(sorted(r.model.slots.values()), ["AUDIO/DRUMS/kick1.wav"])


if __name__ == "__main__":
    unittest.main()
