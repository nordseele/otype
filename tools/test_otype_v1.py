"""Focused regressions from the 2026-10-11 OType v1 review (no emulator/Unicorn)."""
import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import test_otype_cvm as C  # noqa: E402
import test_otype_run as R  # noqa: E402
from modules.otype import cvm, ops, sim, vm  # noqa: E402


V1_OPS = frozenset("""
PAT.CUR BANK.CUR PAT.GO PART.CUR PART.GO PART.SAVE PART.RELOAD TL.DEL TT.ADD TT.DEL OS.ADD OS.DEL OS.ARM
SW.ADD SW.DEL SW.FLIP SL.ADD SL.DEL SL.FLIP TRK.SWING COND.GET COND.DEL MICRO.GET RATCH.GET FILL FILL.ONCE
TRK.LEVEL TRK.CUE TRK.MUTE PAR.GET PB.SETUP AMP.SETUP LFO.WAVE LFO.MULT LFO.TRIG LFOD.SET LFOD.GET
LFOD.INTERP FX1.SETUP FX2.SETUP SCN.SET SCN.GET SCN.DEL SCN.COPY MSG
""".split())


def run_python(body, **kwargs):
    prog, _ = R.make_prog(body)
    return sim.simulate(prog, "main", seed=1, **kwargs), vm.run_vm(prog, "main", seed=1, **kwargs)


class TestOTypeV1Review(unittest.TestCase):
    def test_pattern_and_part_state_are_distinct_in_simulator_and_vm(self):
        part_body = ("par.set amp.vol 80\npart.save 1\npart.go 2\nx par.get amp.vol\n"
                     "par.set amp.vol 90\npart.go 1\ny par.get amp.vol")
        for result in run_python(part_body):
            self.assertEqual((result.globals["x"], result.globals["y"]), (64, 80))

        pat_body = "tr.add 1\npat.go 2\nx has.trig 1\ntr.add 2\npat.go 1\ny has.trig 2"
        for result in run_python(pat_body):
            self.assertEqual((result.globals["x"], result.globals["y"]), (0, 0))
        for result in run_python("x pat.cur", pattern=3):
            self.assertEqual(result.globals["x"], 3)

    def test_part_save_reload_targets_inactive_part_and_bank_boundary(self):
        # Save and reload part 4 while part 1 is active in bank/pattern 16.
        body = ("pat.go 16 16\npar.set amp.vol 80\npart.save 1\npart.go 4\npar.set amp.vol 100\n"
                "part.go 1\npart.save 4\npart.go 4\npar.set amp.vol 110\npart.go 1\npart.reload 4\n"
                "part.go 4\ny par.get amp.vol\npart.go 1\nx par.get amp.vol")
        for result in run_python(body, pattern=16):
            self.assertEqual((result.globals["x"], result.globals["y"]), (80, 100))

    def test_reload_part_does_not_restore_project_mute(self):
        for result in run_python("trk.mute 1\npart.save\ntrk.mute 0\npart.reload\nx trk.mute"):
            self.assertEqual(result.globals["x"], 0)

    def test_fill_off_disarms_fill_once(self):
        for result in run_python("fill.once\nfill 0"):
            self.assertEqual(result.model.fill_once, 0)

    def test_lfo_wave_uses_non_neutral_and_empty_descriptors(self):
        pm = sim.ParamModel(lfo_waves=50)
        for result in run_python("lfo.wave 1 40\nx lfo.wave 1", pmodel=pm):
            self.assertEqual(result.globals["x"], 40)
        s, _v, _c, _hp = self.parity("lfo.wave 1 40\nx lfo.wave 1", pmodel=pm,
                                     name="lfo-wave-descriptor-50")
        self.assertEqual(s.globals["x"], 40)
        pm = sim.ParamModel(lfo_waves=0)
        for result in run_python("lfo.wave 1 40\nx lfo.wave 1", pmodel=pm):
            self.assertEqual(result.globals["x"], 0)
        s, _v, _c, _hp = self.parity("lfo.wave 1 40\nx lfo.wave 1", pmodel=pm,
                                     name="lfo-wave-empty-descriptor")
        self.assertEqual(s.globals["x"], 0)

    def c_run(self, body):
        source = "def main\n  menu \"T\" >> \"MAIN\"\n" + R.body_text(body)
        _prog, compiled, _table = C.compile_sources([("T.OTY", source)])
        cv = cvm.CVM(C.build_lib("_otype_v1"))
        self.assertEqual(cv.load(compiled.data), 0)
        for track, block in C.blank().items():
            cv.bind(track, block, 0)
        hp = cv.bind_params(cvm.HostParams(copy.deepcopy(sim.ParamModel())))
        result = cv.run("main", 1, 1, 16)
        self.assertFalse(isinstance(result, tuple), result)
        return result, hp

    def parity(self, body, pmodel=None, track=1, name="case", check_blocks=True, length=16):
        pmodel = pmodel or sim.ParamModel()
        prog, _ = R.make_prog(body)
        s = sim.simulate(prog, "main", seed=11, track=track, length=length, pmodel=copy.deepcopy(pmodel))
        v = vm.run_vm(prog, "main", seed=11, track=track, length=length, pmodel=copy.deepcopy(pmodel))
        _checked, compiled, _table = C.compile_sources([("T.OTY", "def main\n  menu \"T\" >> \"MAIN\"\n" + R.body_text(body))])
        cv = cvm.CVM(C.build_lib("_otype_v1"))
        self.assertEqual(cv.load(compiled.data), 0, name)
        start = C.Start()
        for t, block in start.blocks.items():
            cv.bind(t, block, 0)
        hp = cv.bind_params(cvm.HostParams(copy.deepcopy(pmodel)))
        c = cv.run("main", 11, track, length)
        self.assertFalse(isinstance(c, tuple), (name, c))
        self.assertEqual(s.globals, v.globals, name + " simulator / Python VM globals")
        self.assertEqual(s.globals, c["globals"], name + " simulator / C globals")
        self.assertEqual(s.draws, v.draws, name + " simulator / Python VM draws")
        self.assertEqual(s.draws, {k.upper(): v for k, v in c["draws"].items()}, name + " simulator / C draws")
        py_oor = [o["op"] for o in R.sim.result_json(s)["out_of_range"]]
        vm_oor = [o["op"] for o in R.sim.result_json(v)["out_of_range"]]
        c_oor = [o["op"] for o in c["out_of_range"]]
        self.assertEqual(py_oor, vm_oor, name + " simulator / Python VM range refusals")
        self.assertEqual(py_oor, c_oor, name + " simulator / C range refusals")
        self.assertEqual(s.cost, c["cost"], name + " cost")
        if check_blocks:
            before = start.blocks
            for t, after in cv.blocks().items():
                changed = {i for i, (a, b) in enumerate(zip(before[t], after)) if a != b}
                allowed = {i for i in changed if i < 0x20 or 0x40 <= i < 0x48
                           or 0x50 <= i < 0x58 or 0x59 <= i < 0x59 + 0x800 or 0x89A <= i < 0x91A}
                self.assertEqual(changed, allowed,
                                 "%s: track %d wrote outside trig/lock/swing-word bytes" % (name, t))
                self.assertEqual(sim.decode_ram_block(after).to_json(),
                                 R.sim.result_json(s)["model"]["tracks"][str(t)], "%s track %d" % (name, t))
                if changed:
                    self.assertIn(t, c["touched"], name + " changed track not reported touched")
        return s, v, c, hp

    def test_v1_ops_are_in_m1_with_the_ui_message(self):
        self.assertEqual(len(V1_OPS), 45)
        self.assertEqual(V1_OPS, V1_OPS & ops.PROFILES["m1"])
        self.assertIn("MSG", ops.PROFILES["m1"])

    def test_v1_pattern_and_part_selectors_match_all_vms(self):
        s, _v, _c, _hp = self.parity("x pat.cur\ny bank.cur\npat.go 16 16\nz pat.cur\nt bank.cur",
                                     name="pattern-bank-selectors", check_blocks=False)
        self.assertEqual((s.globals["x"], s.globals["y"], s.globals["z"], s.globals["t"]), (1, 1, 16, 16))
        s, _v, _c, _hp = self.parity("part.go 4\nx part.cur", name="part-selector")
        self.assertEqual(s.globals["x"], 4)

    def test_pattern_tempo_and_length_state_survive_switching(self):
        s, _v, _c, _hp = self.parity("tempo 140\nx tempo\npat.go 2\ny tempo\ntempo 150\nz tempo\n"
                                     "pat.go 1\nt tempo", pmodel=sim.ParamModel(tempo_mode=True),
                                     name="pattern-tempo-state")
        self.assertEqual((s.globals["x"], s.globals["y"], s.globals["z"], s.globals["t"]),
                         (140, 120, 150, 140))
        s, _v, _c, _hp = self.parity("x pat.len\npat.go 2\ny pat.len\npat.go 1\nz pat.len",
                                     name="pattern-length-state", check_blocks=False, length=64)
        self.assertEqual((s.globals["x"], s.globals["y"], s.globals["z"]), (64, 16, 64))

    def test_v1_pattern_part_trig_timing_and_project_controls_match_all_vms(self):
        body = """x pat.cur
y bank.cur
part.go 2
x part.cur
part.save
part.go 3
part.save 2
part.reload 2
part.go 2
tr.add 1
tl.add 2
tt.add 3
os.add 4
sw.add 5
sw.del 5
sw.add 5
sl.add 6
sl.del 6
sl.add 6
tl.del 2
tt.del 3
os.del 4
os.arm
sw.flip 5
sl.flip 6
trk.swing 61
x trk.swing
cond.set 1 50%
y cond.get 1
cond.del 1
micro.set 1 -7
z micro.get 1
ratch.set 1 4
t ratch.get 1
fill.once
y fill
fill 0
x trk.swing
trk all:
  trk.level 95
  trk.cue 22
  trk.mute 1
trk 1:
  x trk.level
  y trk.cue
  z trk.mute
"""
        s, v, c, _hp = self.parity(body, name="patterns-trigs-timing-context-all")
        self.assertEqual((s.globals["x"], s.globals["y"], s.globals["z"], s.globals["t"]), (95, 22, 1, 4))
        self.assertEqual(s.model.tracks[1].swing, 61)
        self.assertEqual(s.model.fill_once, 0)
        self.assertEqual(c["part_touched"], set(range(1, 9)))
        for t in range(1, 9):
            self.assertEqual((_hp.parts[t].level, _hp.parts[t].cue, _hp.model.mutes[t]), (95, 22, 1))

    def test_v1_param_and_page_setup_descriptors_match_all_vms(self):
        pm = sim.ParamModel(info={(1, C.AMP_VOL): (10, 4, True)}, values={(1, C.AMP_VOL): 11},
                            setup_info={(1, "PB.SETUP", 1): (20, 8, True),
                                        (1, "AMP.SETUP", 2): (3, 5, True)},
                            setup_values={(1, "PB.SETUP", 1): 21, (1, "AMP.SETUP", 2): 4})
        s, _v, _c, hp = self.parity("x par.get amp.vol\npar.set amp.vol 99\ny par.get amp.vol\n"
                                    "z pb.setup 1\npb.setup 1 99\nt pb.setup 1", pmodel=pm,
                                    name="param-and-setup-descriptors")
        self.assertEqual((s.globals["x"], s.globals["y"], s.globals["z"], s.globals["t"]), (11, 13, 21, 27))
        self.assertEqual((hp.parts[1].params[C.AMP_VOL], hp.parts[1].pb_setup[1]), (13, 27))
        s, _v, _c, hp = self.parity("x amp.setup 2\namp.setup 2 1\ny amp.setup 2", pmodel=pm,
                                    name="amp-setup-seed-and-clamp")
        self.assertEqual((s.globals["x"], s.globals["y"], hp.parts[1].amp_setup[2]), (4, 3, 3))

    def test_v1_lfo_setup_getters_match_all_vms(self):
        pm = sim.ParamModel(lfo_waves=50)
        s, _v, _c, hp = self.parity("lfo.wave 1 40\nx lfo.wave 1\nlfo.mult 2 6\ny lfo.mult 2\n"
                                    "lfo.trig 3 7\nz lfo.trig 3", pmodel=pm, name="lfo-setup-getters")
        self.assertEqual((s.globals["x"], s.globals["y"], s.globals["z"]), (40, 6, 7))
        self.assertEqual((hp.parts[1].lfo_wave[1], hp.parts[1].lfo_mult[2], hp.parts[1].lfo_trig[3]), (40, 6, 7))

    def test_v1_scenes_fx_and_lfod_ops_match_all_vms(self):
        s, _v, _c, hp = self.parity("scn.set 1 amp.vol 44\nscn.copy 1 2\nscn.del 1 amp.vol\n"
                                    "x scn.get 2 amp.vol", name="scene-set-copy-delete")
        self.assertEqual(s.globals["x"], 44)
        self.assertEqual(hp.parts[1].scenes[2][C.AMP_VOL], 44)
        pm = sim.ParamModel(setup_info={(1, "FX1.SETUP", 3): (40, 4, True),
                                        (1, "FX2.SETUP", 4): (8, 3, True)},
                            setup_values={(1, "FX1.SETUP", 3): 41, (1, "FX2.SETUP", 4): 9})
        s, _v, _c, hp = self.parity("x fx1.setup 3\nfx1.setup 3 99\ny fx1.setup 3\n"
                                    "z fx2.setup 4\nfx2.setup 4 1\nt fx2.setup 4",
                                    pmodel=pm, name="fx-setup-seed-and-clamp")
        self.assertEqual((s.globals["x"], s.globals["y"], s.globals["z"], s.globals["t"]), (41, 43, 9, 8))
        self.assertEqual((hp.parts[1].fx1_setup[3], hp.parts[1].fx2_setup[4]), (43, 8))
        s, _v, _c, hp = self.parity("lfod.set 1 63\nx lfod.get 1\nlfod.interp 1 1", name="lfod-io")
        self.assertEqual(s.globals["x"], 63)
        self.assertEqual((hp.parts[1].lfod[0], hp.parts[1].lfod_interp[0]), (63, 1))
        s, _v, _c, hp = self.parity("l 1 16:\n  lfod.set i rrand -64 63\nl 1 16:\n  lfod.set i sub 0 lfod.get i",
                                    name="lfod-loops")
        self.assertEqual(s.model.part[1].lfod, hp.parts[1].lfod)

    def test_v1_flex_slot_and_neighbor_machine_refusals_match_all_vms(self):
        pm = sim.ParamModel(flex_slots={7})
        s, _v, c, hp = self.parity("trk.type 1\ntrk.slot 7\nx trk.slot\ny trk.type", pmodel=pm,
                                   name="flex-slot-permitted")
        self.assertEqual((s.globals["x"], s.globals["y"], hp.parts[1].mtype), (7, 1, 1))
        for track in (1, 5):
            s, _v, c, _hp = self.parity("trk.type 3\ntrk.type 5\nx trk.type", track=track,
                                        name="neighbor-type-refused-T%d" % track)
            self.assertEqual(len(c["out_of_range"]), 2)

    def test_v1_helpers_and_message_compile_and_agree(self):
        s, _v, _c, hp = self.parity("l 1 16:\n  lfod.set i rrand -64 63\nl 1 16:\n  lfod.set i sub 0 lfod.get i",
                                    name="lfod-loops")
        self.assertEqual(s.model.part[1].lfod, hp.parts[1].lfod)
        prog, _ = R.make_prog('msg "done"')
        s = sim.simulate(prog, "main", seed=1)
        v = vm.run_vm(prog, "main", seed=1)
        self.assertEqual(s.model.msgs, v.model.msgs)
        self.assertEqual(s.model.msgs, ["done"])

    def test_c_tempo_and_slot_getters_do_not_call_setters(self):
        result, hp = self.c_run("tempo 120\nx tempo\ntrk.slot 5\ny trk.slot")
        self.assertEqual((result["globals"]["x"], result["globals"]["y"]), (120, 5))
        self.assertEqual([entry[0] for entry in hp.log if entry[0] in ("tempo", "trk_slot")],
                         ["tempo", "trk_slot"])

    def test_c_trk_type_setter_changes_machine_type(self):
        result, hp = self.c_run("trk.type 1\nx trk.type")
        self.assertEqual(result["globals"]["x"], 1)
        self.assertEqual(hp.parts[1].mtype, 1)

    def test_factory_slot_getter_after_track_init(self):
        s, _v, _c, hp = self.parity("trk 3\ntrk.init\nx trk.slot", name="factory-slot-by-track")
        self.assertEqual(s.globals["x"], 3)
        self.assertEqual(hp.parts[3].slot, "factory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
