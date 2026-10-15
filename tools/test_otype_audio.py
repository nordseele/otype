#!/usr/bin/env python3
"""Tests for the OType audio operations (audio core, plus flex.firstfree / flex.del):
- AUDIO category and opcodes (src.save, flex.load, flex.free, cat.clr, cat.add, cat.save, norm)
- input sample placement declaration
- load-time refusal: src.* without input sample (directly or via call)
- simulator mock host and refusal conditions
"""
import unittest
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_TOOLS = os.path.join(ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import otype
from modules.otype import ops as O
from modules.otype import otb
from modules.otype import sim


class TestAudioOpsRegistry(unittest.TestCase):
    def test_audio_category_present(self):
        self.assertIn("AUDIO", O.CATEGORIES)

    def test_audio_namespace_present(self):
        self.assertIn("AUDIO", O.NAMESPACES)
        self.assertEqual(O.NAMESPACES["AUDIO"], 17)

    def test_audio_ops_declared(self):
        for name in ("SRC.SAVE", "FLEX.LOAD", "FLEX.FREE", "CAT.CLR", "CAT.ADD", "CAT.SAVE", "NORM"):
            self.assertIn(name, O.OPS, f"Op {name} must be declared in ops.py")
            op = O.OPS[name]
            self.assertEqual(op.ns, "AUDIO")  # bytecode namespace; flex.* are listed under SLOTS
            self.assertEqual(op.category, "SLOTS" if name.startswith("FLEX.") else "AUDIO")

    def test_src_save_attributes(self):
        op = O.OPS["SRC.SAVE"]
        self.assertEqual(op.args, ())
        self.assertIsNone(op.ret)
        self.assertTrue(op.stmt_only)
        self.assertEqual(op.family, O.IO_W)

    def test_flex_load_and_free_attributes(self):
        op_load = O.OPS["FLEX.LOAD"]
        self.assertEqual(op_load.args, (O.SLOT, O.FILE))
        self.assertIsNone(op_load.ret)

        op_free = O.OPS["FLEX.FREE"]
        self.assertEqual(op_free.args, (O.SLOT,))
        self.assertEqual(op_free.ret, O.INT)

    def test_cat_ops_attributes(self):
        op_clr = O.OPS["CAT.CLR"]
        self.assertEqual(op_clr.args, ())
        self.assertIsNone(op_clr.ret)

        op_add = O.OPS["CAT.ADD"]
        self.assertEqual(op_add.args, (O.FILE,))
        self.assertIsNone(op_add.ret)

        op_save = O.OPS["CAT.SAVE"]
        self.assertEqual(op_save.args, (O.STR,))
        self.assertIsNone(op_save.ret)
        self.assertTrue(op_save.stmt_only)

    def test_norm_attributes(self):
        op = O.OPS["NORM"]
        self.assertEqual(op.args, (O.FILE, O.INT))
        self.assertIsNone(op.ret)


class TestInputSamplePlacement(unittest.TestCase):
    def test_input_sample_declaration(self):
        src = """def record_take
  menu "AUDIO" >> "SAVE TAKE"
  input sample
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertEqual(p.error_count(), 0, "Valid input sample function must be accepted")
        f = p.fmap["RECORD_TAKE"]
        self.assertTrue(getattr(f, "input_sample", False))

    def test_duplicate_input_sample_refused(self):
        src = """def record_take
  input sample
  input sample
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertGreater(p.error_count(), 0)
        diags = [str(d) for d in p.diags if d.level == "error"]
        self.assertTrue(any("duplicate input declaration" in d for d in diags))

    def test_unknown_input_target_refused(self):
        src = """def record_take
  input folder
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertGreater(p.error_count(), 0)
        diags = [str(d) for d in p.diags if d.level == "error"]
        self.assertTrue(any("input expects sample" in d for d in diags))

    def test_src_save_without_input_sample_refused(self):
        src = """def bad_fn
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertGreater(p.error_count(), 0)
        diags = [str(d) for d in p.diags if d.level == "error"]
        self.assertTrue(any("op src.save needs input sample" in d for d in diags))

    def test_src_save_via_call_without_input_sample_refused(self):
        src = """def helper
  input sample
  src.save

def caller
  helper
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertGreater(p.error_count(), 0)
        diags = [str(d) for d in p.diags if d.level == "error"]
        self.assertTrue(any("op src.save needs input sample" in d for d in diags))

    def test_audio_ops_without_input_sample_allowed(self):
        src = """def process_dir
  dir "AUDIO"
  cat.clr
  cat.add file.at 1
  cat.save "CHAIN"
  flex.load 1 file.at 1
  norm file.at 1 -3
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertEqual(p.error_count(), 0, "AUDIO ops other than src.* must be allowed without input sample")


class TestHostHeaderBytecode(unittest.TestCase):
    def test_ccompiler_audio_contract(self):
        from modules.otype import ccomp, oterr
        cc = ccomp.CComp(ccomp.build())
        cases = (
            "def source\n  input sample\n  src.save\n  flex.load 128 file.at 1\n  a flex.free 128\n  cat.clr\n  cat.add file.at 1\n  cat.save \"CHAIN\"\n  norm file.at 1 -12\n",
            "def source\n  src.save\n",
            "def source\n  input sample\n  src.save\n\ndef relay\n  source\n\ndef root\n  relay\n",
            "def source | x\n  src.save\n\ndef root\n  source | 1\n",
            "def source\n  input sample\n  input sample\n  src.save\n",
        )
        for source in cases:
            with self.subTest(source=source):
                sources = [(b"/SCRIPTS/TEST.OTY", source.encode())]
                report, table = oterr.reference(sources, "m1")
                actual_table, actual_report = cc.compile(sources, "m1")
                self.assertEqual(cc.err, 0)
                self.assertEqual(actual_report, report)
                self.assertEqual(actual_table, table)

    def test_bytecode_host_bit_sample(self):
        src = """def sample_action
  input sample
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertEqual(p.error_count(), 0)
        c = otb.compile_program(p)
        t = otb.parse(c.data)
        f = t.funcs[0]
        self.assertTrue(hasattr(otb, "F_INPUT_SAMPLE"))
        self.assertEqual(f.flags & otb.F_INPUT_SAMPLE, otb.F_INPUT_SAMPLE)
        self.assertTrue(getattr(f, "input_sample", False))


class TestAudioSimulatorErrors(unittest.TestCase):
    """Test error codes -1..-13 in simulator."""

    def test_sim_error_empty_cat_save(self):
        src = """def run_cat
  cat.clr
  cat.save "TAKE"
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertEqual(p.error_count(), 0)
        with self.assertRaises(sim.AudioError) as ctx:
            sim.simulate(p, "run_cat")
        self.assertEqual(ctx.exception.code, -4)
        self.assertEqual(ctx.exception.text, "EMPTY")

    def test_sim_error_no_source(self):
        src = """def run_src
  input sample
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertEqual(p.error_count(), 0)
        m = sim.Model()
        m.audio_source = None
        with self.assertRaises(sim.AudioError) as ctx:
            sim.simulate(p, "run_src", model=m)
        self.assertEqual(ctx.exception.code, -3)
        self.assertEqual(ctx.exception.text, "NO SOURCE")

    def test_sim_error_stop_first_recording(self):
        src = """def run_src
  input sample
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        m = sim.Model()
        m.audio_source = {"path": "A.WAV", "empty": False, "recording": True}
        with self.assertRaises(sim.AudioError) as ctx:
            sim.simulate(p, "run_src", model=m)
        self.assertEqual(ctx.exception.code, -6)
        self.assertEqual(ctx.exception.text, "STOP FIRST")

    def test_sim_error_cat_full_64(self):
        src = """def fill_cat
  cat.clr
  cat.add file.at 1
"""
        p = otype.check_sources([("test.oty", src)])
        m = sim.Model()
        m.cat_list = [f"F_{i}.WAV" for i in range(64)]
        fr_files = sim.FileList("AUDIO", False, ["NEW.WAV"])
        # Manually run on full cat_list
        with self.assertRaises(sim.AudioError) as ctx:
            run = sim.Run(p, p.fmap["FILL_CAT"], 1, "m", m, 1, 16, 1, None, {}, {})
            fr = sim.Frame(p.fmap["FILL_CAT"], {}, fr_files)
            run.live.append(fr)
            sim.HANDLERS["CAT.ADD"](run, None, fr, p.fmap["FILL_CAT"].body[1], [1])
        self.assertEqual(ctx.exception.code, -12)
        self.assertEqual(ctx.exception.text, "CAT FULL")

    def test_sim_refusals_all_codes(self):
        src = """def run_src
  input sample
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        for code, expected_name in sim.AUDIO_ERRORS.items():
            m = sim.Model()
            m.audio_refuse = {"SRC.SAVE": code}
            with self.assertRaises(sim.AudioError) as ctx:
                sim.simulate(p, "run_src", model=m)
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(ctx.exception.text, expected_name)


class TestAudioCVM(unittest.TestCase):
    """Test CVM audio execution, query, and error codes."""

    @classmethod
    def setUpClass(cls):
        from tools.test_otype_cvm import build_lib
        from modules.otype import cvm
        cls.cv = cvm.CVM(build_lib())

    def test_cvm_fn_input_sample_query(self):
        src = """def sample_fn
  input sample
  src.save

def normal_fn
  a 42
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertEqual(p.error_count(), 0)
        c = otb.compile_program(p)
        self.assertEqual(self.cv.load(c.data), 0)
        self.assertTrue(self.cv.fn_input_sample("sample_fn"))
        self.assertTrue(self.cv.fn_input_sample(0))
        self.assertFalse(self.cv.fn_input_sample("normal_fn"))
        self.assertFalse(self.cv.fn_input_sample(1))

    def test_cvm_successful_src_save(self):
        src = """def record
  input sample
  src.save
  flex.load 1 file.at 1
"""
        p = otype.check_sources([("test.oty", src)])
        self.assertEqual(p.error_count(), 0)
        c = otb.compile_program(p)
        self.assertEqual(self.cv.load(c.data), 0)
        self.cv.bind_card()
        ha = self.cv.bind_audio({"source": "REC/TAKE.WAV"})
        res = self.cv.run("record", 1, 1, 16)
        self.assertIsInstance(res, dict)
        self.assertEqual(ha.flex_slots.get(0), "/REC/TAKE.WAV")

    def test_flex_free_only_queries(self):
        ha = self.cv.bind_audio({})
        ha.flex_slots[0] = "/AUDIO/KEEP.WAV"
        ha.flex_playing.add(0)
        self.assertEqual(ha.flex_free(0), 0)
        self.assertEqual(ha.flex_slots[0], "/AUDIO/KEEP.WAV")
        self.assertEqual(ha.mutations, 0)

    def test_paths_and_nested_cat_list(self):
        src = '''def root
  input sample
  src.save
  cat.add file.at 1
  helper
  cat.save "CHAIN"
  norm file.at 1 -3

def helper
  a flex.free 1
  cat.add file.at 1
'''
        p = otype.check_sources([("test.oty", src)])
        self.assertEqual(p.error_count(), 0)
        self.assertEqual(self.cv.load(otb.compile_program(p).data), 0)
        self.cv.bind_card()
        ha = self.cv.bind_audio({"source": "/AUDIO/TAKE.WAV"})
        self.cv.run("root", 1, 1, 16)
        self.assertEqual(ha.cat_list, ["/AUDIO/TAKE.WAV"] * 2)
        self.assertEqual(ha.normalized, {"/AUDIO/CHAIN.WAV": -3})
        self.cv.run("root", 1, 1, 16)
        self.assertEqual(len(ha.cat_list), 2)

    def test_busy_code_and_norm_argument(self):
        for body, busy, code in (("src.save", True, -2), ("src.save\n  norm file.at 1 1", False, -1)):
            p = otype.check_sources([("test.oty", "def root\n  input sample\n  " + body + "\n")])
            self.assertEqual(p.error_count(), 0)
            self.assertEqual(self.cv.load(otb.compile_program(p).data), 0)
            self.cv.bind_card(busy=lambda op: busy)
            self.cv.bind_audio({"source": "/AUDIO/TAKE.WAV"})
            self.assertEqual(self.cv.run("root", 1, 1, 16), ("error", code))

    def test_source_path_is_never_truncated(self):
        p = otype.check_sources([("test.oty", "def root\n  input sample\n  src.save\n")])
        for path in ("/" + "x" * 128, "/" + "x" * 256 + "/A.WAV"):
            self.assertEqual(self.cv.load(otb.compile_program(p).data), 0)
            self.cv.bind_card()
            self.cv.bind_audio({"source": path})
            self.assertEqual(self.cv.run("root", 1, 1, 16), ("error", -1))

    def test_cvm_error_codes(self):
        from modules.otype import cvm
        src = """def record
  input sample
  src.save
"""
        p = otype.check_sources([("test.oty", src)])
        c = otb.compile_program(p)
        self.assertEqual(self.cv.load(c.data), 0)
        self.cv.bind_card()

        for code in cvm.AUDIO_ERRORS:
            self.cv.bind_audio({"refuse": {"SRC.SAVE": code}})
            res = self.cv.run("record", 1, 1, 16)
            self.assertEqual(res, ("error", code), f"Expected error code {code}")



FLEX_PARITY = (            # (name, script, flex slots 1..128 held at the start, expected globals)
    ("firstfree-gap", "def f\n  x flex.firstfree\n  y flex.free 3\n", {1, 2, 4}, {"x": 3, "y": 1}),
    ("firstfree-empty", "def f\n  x flex.firstfree\n", set(), {"x": 1}),
    ("firstfree-full", "def f\n  x flex.firstfree\n", set(range(1, 129)), {"x": 0}),
    ("firstfree-last", "def f\n  x flex.firstfree\n", set(range(1, 128)), {"x": 128}),
    ("del-reopens", "def f\n  flex.del 1\n  x flex.firstfree\n  y flex.free 1\n  z flex.free 2\n",
     {1, 2}, {"x": 1, "y": 1, "z": 0}),
    ("del-empty", "def f\n  flex.del 5\n  x flex.firstfree\n", {2}, {"x": 1}),
    ("del-bounds", "def f\n  flex.del 128\n  x flex.free 128\n  y flex.free 127\n", {127, 128}, {"x": 1, "y": 0}),
    ("del-loop", "def f\n  l 1 128:\n    flex.del i\n  x flex.firstfree\n", {3, 77, 128}, {"x": 1}),
)


class TestFlexMirrorsStatic(unittest.TestCase):
    """flex.firstfree and flex.del: the Flex twins of static.firstfree and static.clr."""

    def check(self, source):
        p = otype.check_sources([("test.oty", source)])
        self.assertEqual(p.error_count(), 0, [str(d) for d in p.diags])
        return p

    def test_declared(self):
        for name, num in (("FLEX.FIRSTFREE", 8), ("FLEX.DEL", 9)):
            op = O.OPS[name]
            self.assertEqual((op.ns, op.num if hasattr(op, "num") else op.id[1], op.category), ("AUDIO", num, "SLOTS"))
        self.assertEqual((O.OPS["FLEX.FIRSTFREE"].args, O.OPS["FLEX.FIRSTFREE"].ret), ((), O.INT))
        self.assertEqual((O.OPS["FLEX.DEL"].args, O.OPS["FLEX.DEL"].ret), ((O.SLOT,), None))
        self.assertTrue(O.OPS["FLEX.DEL"].destructive)
        self.assertEqual(O.OPS["FLEX.DEL"].touches, O.OPS["STATIC.CLR"].touches)
        self.assertEqual(O.OPS["FLEX.FIRSTFREE"].family, O.OPS["STATIC.FIRSTFREE"].family)
        self.assertEqual(len({o.id for o in O.ALL_OPS}), len(O.ALL_OPS))

    def test_flex_del_is_destructive_and_asks_confirmation(self):
        p = self.check("def f\n  flex.del 1\ndef g\n  x flex.firstfree\n")
        self.assertTrue(p.destructive[p.fmap["F"]])
        self.assertFalse(p.destructive[p.fmap["G"]])

    def test_slot_range_checked(self):
        source = "def f\n  x 129\n  flex.del x\n  x 0\n  flex.del x\n"
        m = sim.Model()
        m.flex_slots = {1: "A"}
        self.assertEqual(len(sim.simulate(self.check(source), "f", model=m).oor), 2)
        self.assertEqual(m.flex_slots, {1: "A"})
        p = otype.check_sources([("test.oty", "def f\n  flex.del 129\n")])
        self.assertGreater(p.error_count(), 0)

    def test_simulator(self):
        for name, source, held, want in FLEX_PARITY:
            with self.subTest(name):
                m = sim.Model()
                m.flex_slots = {s: "/AUDIO/%d.WAV" % s for s in held}
                r = sim.simulate(self.check(source), "f", model=m)
                self.assertEqual({k: r.globals[k] for k in want}, want)

    def test_simulator_empty_slot_is_not_touched_and_stop_first(self):
        p = self.check("def f\n  flex.del 5\n  flex.del 2\n")
        m = sim.Model()
        m.audio_refuse = {"FLEX.DEL": -2}
        m.flex_slots = {1: "A"}
        sim.simulate(p, "f", model=m)                                  # both empty: the refusal is never asked
        m.flex_slots = {2: "B"}
        with self.assertRaises(sim.AudioError) as ctx:
            sim.simulate(p, "f", model=m)
        self.assertEqual(ctx.exception.code, -2)
        m = sim.Model()
        m.flex_slots, m.flex_playing = {2: "B", 3: "C"}, {2}
        with self.assertRaises(sim.AudioError) as ctx:
            sim.simulate(p, "f", model=m)
        self.assertEqual((ctx.exception.code, ctx.exception.text), (-6, "STOP FIRST"))
        self.assertEqual(sorted(m.flex_slots), [2, 3])
        m.flex_playing = {3}                                           # another slot plays: this one goes
        sim.simulate(p, "f", model=m)
        self.assertEqual(sorted(m.flex_slots), [3])

    def test_c_compiler_accepts_both(self):
        from modules.otype import ccomp, oterr
        cc = ccomp.CComp(ccomp.build())
        sources = [(b"/SCRIPTS/TEST.OTY", b"def f\n  x flex.firstfree\n  flex.del x\n  flex.del 128\n")]
        report, table = oterr.reference(sources, "m1")
        actual_table, actual_report = cc.compile(sources, "m1")
        self.assertEqual(cc.err, 0)
        self.assertIn("1 functions OK, 0 refused", actual_report)
        self.assertEqual(actual_report, report)
        self.assertEqual(actual_table, table)

    def test_no_other_spelling(self):
        # the Flex twin of static.clr is flex.del; "flex.clr" and "flex.first" do not exist
        p = otype.check_sources([("test.oty", "def f\n  flex.clr 1\n")])
        self.assertGreater(p.error_count(), 0)


class TestFlexMirrorsStaticCVM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools.test_otype_cvm import build_lib
        from modules.otype import cvm
        cls.cv = cvm.CVM(build_lib())

    def start(self, source, held, **audio):
        p = otype.check_sources([("test.oty", source)])
        self.assertEqual(p.error_count(), 0, [str(d) for d in p.diags])
        self.assertEqual(self.cv.load(otb.compile_program(p).data), 0)
        self.cv.bind_card()
        ha = self.cv.bind_audio(audio)
        ha.flex_slots.update({s - 1: "/AUDIO/%d.WAV" % s for s in held})
        return ha

    def test_c_vm_matches_the_simulator(self):
        for name, source, held, want in FLEX_PARITY:
            with self.subTest(name):
                ha = self.start(source, held)
                res = self.cv.run("f", 1, 1, 16)
                self.assertEqual({k: res["globals"][k] for k in want}, want)
                m = sim.Model()
                m.flex_slots = {s: "/AUDIO/%d.WAV" % s for s in held}
                r = sim.simulate(otype.check_sources([("test.oty", source)]), "f", model=m)
                self.assertEqual(set(ha.flex_slots), {s - 1 for s in m.flex_slots})
                self.assertEqual(bool(res["slots_touched"]), bool(held - set(m.flex_slots)))

    def test_empty_slot_never_reaches_the_host(self):
        ha = self.start("def f\n  flex.del 5\n  flex.del 2\n", {1}, refuse={"FLEX.DEL": -2})
        res = self.cv.run("f", 1, 1, 16)
        self.assertIsInstance(res, dict)
        self.assertEqual((ha.mutations, res["slots_touched"]), (0, 0))
        ha = self.start("def f\n  flex.del 5\n  flex.del 2\n", {2}, refuse={"FLEX.DEL": -2})
        self.assertEqual(self.cv.run("f", 1, 1, 16), ("error", -2))

    def test_stop_first_keeps_the_slot(self):
        ha = self.start("def f\n  flex.del 2\n", {2, 3})
        ha.flex_playing.add(1)
        self.assertEqual(self.cv.run("f", 1, 1, 16), ("error", -6))
        self.assertEqual(sorted(ha.flex_slots), [1, 2])
        ha.flex_playing.clear()
        self.assertIsInstance(self.cv.run("f", 1, 1, 16), dict)
        self.assertEqual(sorted(ha.flex_slots), [2])

    def test_out_of_range_slot_is_skipped(self):
        ha = self.start("def f\n  x 129\n  flex.del x\n  x 0\n  flex.del x\n", {1})
        res = self.cv.run("f", 1, 1, 16)
        self.assertEqual((len(res["out_of_range"]), sorted(ha.flex_slots)), (2, [0]))

    def test_host_error_stops_firstfree(self):
        self.start("def f\n  x flex.firstfree\n", set(), refuse={"FLEX.FREE": -2})
        self.assertEqual(self.cv.run("f", 1, 1, 16), ("error", -2))


if __name__ == "__main__":
    unittest.main()
