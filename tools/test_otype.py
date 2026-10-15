"""Tests of tools/otype.py.  Run from the repo root:

    python3 -m unittest tools/test_otype.py
"""
import glob
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import otype  # noqa: E402

def read(path):
    return otype.read_text(path)


GOOD = "DEF GOOD\n  TR.ADD 1\n\n"        # lines 1-3: a valid function, a blank line
AFTER = "\nDEF GOOD2\n  TR.ADD 2\n"
FIRST = 4                                 # first line of the text under test


class Base(unittest.TestCase):
    def check(self, *texts, **kw):
        srcs = [("F%d.OTY" % (i + 1), t) for i, t in enumerate(texts)]
        prog = otype.check_sources(srcs)
        self.assertLowerMessages(prog)      # every diagnostic of every test is lower case
        return prog

    def errs(self, prog):
        return [d for d in prog.diags if d.level == "error" and not d.hidden]

    def warns(self, prog):
        return [d for d in prog.diags if d.level == "warning"]

    def ok_names(self, prog):
        return sorted(f.name for f in prog.ok_funcs())

    def assertClean(self, prog):
        self.assertEqual([str(d) for d in prog.diags], [], "unexpected diagnostics")

    # What may stay upper case in a message: a user string (between double quotes), a file
    # reference (F1.OTY:3, any path), the acronyms ASCII and API.
    ALLOWED_UPPER = re.compile(r'"[^"]*"|[\w./-]+\.OTY(?::\d+)?|ASCII|API')

    def assertLowerMessages(self, prog):
        """The canonical written form is lower case: no diagnostic shows a name in capitals."""
        for d in prog.diags:
            rest = self.ALLOWED_UPPER.sub("", d.msg)
            self.assertEqual(rest, rest.lower(), "capitals in a message: %s" % d)
            self.assertEqual(d.func, d.func.lower(), "capitals in a function name: %s" % d)

    def assertErr(self, prog, line, sub, func=None, file="F1.OTY"):
        """An error containing `sub` on line `line` (of the file); `func` in any case."""
        self.assertLowerMessages(prog)
        found = [d for d in self.errs(prog) if d.file == file and d.line == line
                 and sub.lower() in d.msg.lower()
                 and (func is None or d.func == func.lower())]
        self.assertTrue(found, "no error %r on %s:%d (func %s); got:\n%s" % (
            sub, file, line, func, "\n".join(str(d) for d in prog.diags)))

    def bad(self, text, line, sub, func="BAD", refused=("BAD",)):
        """`text` goes after a valid function and before another one.

        The error must be on line FIRST + line - 1; GOOD and GOOD2 must load."""
        prog = self.check(GOOD + text + AFTER)
        self.assertErr(prog, FIRST + line - 1, sub, func)
        ok = self.ok_names(prog)
        self.assertIn("GOOD", ok)
        self.assertIn("GOOD2", ok)
        for r in refused:
            self.assertNotIn(r, ok)
        return prog

    def good(self, text, extra=()):
        prog = self.check(text, *extra)
        self.assertClean(prog)
        return prog


class Examples(Base):
    def test_examples_pass(self):
        files = sorted(glob.glob(os.path.join(ROOT, "modules", "otype", "examples", "*.OTY")))
        self.assertEqual([os.path.basename(f) for f in files],
                         ["BOOMBAP.OTY", "LEGACY.OTY", "LIB.OTY", "M1.OTY", "PAGE.OTY", "RHYTHM.OTY", "TUTORIAL.OTY"])
        prog = otype.check_files(files)
        self.assertEqual(self.errs(prog), [])
        self.assertEqual(self.warns(prog), [])
        self.assertEqual(self.ok_names(prog), sorted(
            ["BOOMBAP", "KICK", "SNARE", "HATS", "PLAY_ONE", "LOCK_SAMPLES",
             "AB_EUCLID", "WALK",
             "SPRINKLE", "EUCLID_HATS", "AB_LOCKS", "TWO_EUCLIDS", "EUCLID_ON",
             "KICKS", "KIT_LOCK", "FOUR", "EUCL", "PDENS",
             # LEGACY.OTY: the functions of an earlier menu
             "POOL_FILL", "POOL_REFILL", "POOL_SHUFFLE", "POOL_CLEAR", "RND_LFO", "RND_FX", "SCENES_GEN", "SCENES_CLR", "PL_FAMILIES",
             "RND_PLOCKS", "RND_SMPLOCKS", "CLR_PLOCKS", "CLR_SMPLOCKS", "TRIGLESS", "INIT_TRACK"]))
        boom = [f for f in prog.funcs if f.name == "BOOMBAP"][0]
        self.assertTrue(prog.destructive[boom])
        self.assertLess(prog.maxcost[boom], otype.MENU_BUDGET)

    def test_cross_file_needed(self):
        # BOOMBAP alone: PLAY_ONE and LOCK_SAMPLES are in the other file
        path = os.path.join(ROOT, "modules", "otype", "examples", "BOOMBAP.OTY")
        prog = otype.check_files([path])
        self.assertTrue(self.errs(prog))

    def test_cli_exit_codes(self):
        import contextlib
        import io
        d = os.path.join(ROOT, "modules", "otype", "examples")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(otype.main(["otype.py", "check", d + "/BOOMBAP.OTY", d + "/LIB.OTY"]), 0)
            self.assertEqual(otype.main(["otype.py", "check", d + "/BOOMBAP.OTY"]), 1)
        # the unknown PLAY_ONE must not drown the report in type errors about it
        self.assertNotIn("expected a number", out.getvalue())


def recase(text, mode):
    """The code of `text` in upper case ("upper") or in alternating case ("mixed"); strings
    (between double quotes) and comments are left exactly as they are."""
    out = []
    n = 0
    for line in text.split("\n"):
        code, comment, ins = line, "", False
        for k, ch in enumerate(line):
            if ch == '"':
                ins = not ins
            elif ch == "#" and not ins:
                code, comment = line[:k], line[k:]
                break
        parts = code.split('"')
        for i in range(0, len(parts), 2):
            if mode == "upper":
                parts[i] = parts[i].upper()
            else:
                chars = []
                for ch in parts[i]:
                    n += 1
                    chars.append(ch.upper() if n % 2 else ch.lower())
                parts[i] = "".join(chars)
        out.append('"'.join(parts) + comment)
    return "\n".join(out)


class CaseInsensitive(Base):
    """The language is case-insensitive; the canonical written form is lower case."""
    EX = os.path.join(ROOT, "modules", "otype", "examples")

    def sources(self):
        return [(os.path.basename(p), read(p)) for p in sorted(glob.glob(os.path.join(self.EX, "*.OTY")))]

    def summary(self, srcs):
        import json
        prog = otype.check_sources(srcs)
        j = otype.check_json(prog)
        return {"diags": [str(d) for d in prog.sorted_diags()],
                "json": json.dumps(j, sort_keys=True),
                "cost": sorted((f.name, prog.maxcost.get(f)) for f in prog.funcs),
                "destructive": sorted((f.name, prog.destructive[f]) for f in prog.funcs),
                "menus": sorted((f.name, otype.menu_path(f)) for f in prog.funcs)}, prog

    def test_examples_are_written_in_lower_case(self):
        for label, text in self.sources():
            for line in text.split("\n"):
                code = line.split("#")[0]
                outside = "".join(code.split('"')[0::2])
                self.assertEqual(outside, outside.lower(), "%s: %s" % (label, line))

    def test_upper_and_mixed_case_examples_give_the_same_result(self):
        srcs = self.sources()
        base, prog = self.summary(srcs)
        self.assertEqual(self.errs(prog), [])
        self.assertTrue(base["json"] and len(base["cost"]) == 33)     # 18 + LEGACY.OTY 15
        for mode in ("upper", "mixed"):
            alt = [(l, recase(t, mode)) for l, t in srcs]
            self.assertNotEqual(alt, srcs)
            got, _p = self.summary(alt)
            self.assertEqual(got, base, mode)

    def test_strings_keep_their_case(self):
        srcs = [(l, recase(t, "mixed")) for l, t in self.sources()]
        prog = otype.check_sources(srcs)
        self.assertEqual(otype.menu_path([f for f in prog.funcs if f.name == "BOOMBAP"][0]),
                         "DRUMS >> BOOM BAP")
        j = otype.check_json(prog)
        menus = {f["name"]: f["menu"] for f in j["functions"]}
        self.assertEqual(menus["ab_locks"], ["LOCKS", "A/B FX"])
        self.assertEqual(menus["boombap"], ["DRUMS", "BOOM BAP"])
        import io
        buf = io.StringIO()
        otype.report(prog, True, buf)
        self.assertIn("DRUMS >> BOOM BAP", buf.getvalue())
        self.assertIn("ok  boombap ", buf.getvalue())

    def test_errors_are_the_same_in_any_case(self):
        text = ("def bad | x\n  tr.add\n  tr.add 1 2\n  prob 50%: tr.add 1\n  frob 1\n"
                "  l 1 a: tr.add i\n  trk 9: tr.add 1\n")
        base = otype.check_sources([("F1.OTY", text)])
        want = [(d.line, d.func, d.level, d.msg) for d in base.sorted_diags()]
        self.assertTrue(want)
        self.assertLowerMessages(base)
        for mode in ("upper", "mixed"):
            prog = otype.check_sources([("F1.OTY", recase(text, mode))])
            self.assertEqual([(d.line, d.func, d.level, d.msg) for d in prog.sorted_diags()], want)

    def test_messages_are_lower_case(self):
        prog = self.check("DEF F | K\n  TR.ADD K\n  TR.ADD\n  TR.ADD ZZZ\n  PROB 50%: TR.ADD 1\n  L 1 A: TR.ADD I\n"
                          "  TRK 9: TR.ADD 1\n")
        msgs = [str(d) for d in prog.sorted_diags()]
        self.assertIn("F1.OTY:3: f: error: tr.add expects 1 argument (step), got 0", msgs)
        self.assertIn("F1.OTY:4: f: error: unknown name zzz", msgs)
        self.assertIn("F1.OTY:5: f: error: 50% is a trig condition; write prob 50:", msgs)
        self.assertTrue(any(m.startswith("F1.OTY:6: f: error: l bound must be constant (a):")
                            for m in msgs), msgs)
        self.assertIn("F1.OTY:7: f: error: 9 is out of range for track (1..8)", msgs)
        self.assertLowerMessages(prog)

    def test_user_names_are_shown_in_lower_case(self):
        prog = self.check("CONST BIG 70\nDEF Hats | Track\n  TR.ADD BIG\n")
        j = otype.check_json(prog)
        self.assertEqual([f["name"] for f in j["functions"]], ["hats"])
        self.assertEqual(j["functions"][0]["args"], ["track"])
        self.assertEqual([c["name"] for c in j["consts"]], ["big"])
        self.assertEqual(j["diagnostics"][0]["func"], "hats")
        self.assertLowerMessages(prog)

    def test_a_string_with_capitals_is_not_lowered_in_a_message(self):
        prog = self.check('DEF F\n  MENU "TOOLONGNAME" >> "X"\n  TR.ADD 1\n')
        self.assertTrue(any('menu category "TOOLONGNAME"' in d.msg for d in prog.diags))
        prog = self.check('DEF F\n  "Hello"\n')
        self.assertTrue(any('"Hello" does nothing' in d.msg for d in prog.diags), [str(d) for d in prog.diags])


class Passing(Base):
    def test_ternary(self):
        self.good("DEF TT\n  A ? EQ X 0 5 RND 3\n  TR.ADD ? GT A 2 1 2\n")

    def test_ternary_cost_is_cond_plus_max_branch(self):
        prog = self.good("DEF TT\n  MENU \"A\" >> \"B\"\n  A ? X ADD 1 2 FILE.PICK\n")
        f = prog.funcs[0]
        # ? (1) + X (0) + max(ADD 1 2 = 1, FILE.PICK = 2)
        self.assertEqual(prog.maxcost[f], 1 + 0 + 2)

    def test_elif_else_block_form(self):
        self.good("DEF E\n  IF EQ X 1:\n    TR.ADD 1\n  ELIF EQ X 2:\n    TR.ADD 2\n"
                  "  ELSE:\n    TR.ADD 3\n")

    def test_elif_else_inline_form(self):
        self.good("DEF E\n  IF EQ X 1: TR.ADD 1\n  ELIF EQ X 2: TR.ADD 2\n  ELSE: TR.ADD 3\n")

    def test_elif_else_mixed_forms(self):
        self.good("DEF E\n  IF X: TR.ADD 1\n  ELSE:\n    TR.ADD 3\n"
                  "  IF Y:\n    TR.ADD 4\n  ELSE: TR.ADD 5\n")

    def test_steps_range(self):
        self.good("DEF S\n  STEPS 1 8:\n    TR.ADD 3\n  STEPS ALL: TR.DEL 1\n"
                  "  STEPS HAS.TRIG: TR.DEL 2\n  TRK 1 2 3: TR.CLR\n  TRK ALL: TR.CLR\n"
                  "  TRK CUR: TR.CLR\n  STEP 5: TR.ADD 5\n  SEED 7: TL.FILL 50\n")

    def test_context_statement_and_modifier(self):
        self.good("DEF S\n  TRK 3\n  STEPS 1 8\n  SEED 4\n  TR.ADD 1\n  TRK 2 4:\n    TR.ADD 2\n")

    def test_user_function_as_value(self):
        self.good("DEF PICK_SLOT | N\n  IF GT N 4: RET 4\n  RET N\n\n"
                  "DEF USE\n  A PICK_SLOT | 3\n  TRK.SLOT PICK_SLOT | 9\n"
                  "  SMP.SET 1 ADD 1 PICK_SLOT | 2\n")

    def test_user_function_without_ret_is_value_zero(self):
        self.good("DEF NORET\n  TR.ADD 1\n\nDEF USE\n  A NORET\n  TR.ADD NORET\n")

    def test_cond_tokens(self):
        self.good("DEF CC\n  COND.RND 50% 99%\n  COND.SET 3 1:4\n  COND.SET 5 !FILL\n"
                  "  COND.SET 6 PRE\n  COND.SET 7 !PRE\n  COND.SET 8 NEI\n  COND.SET 9 !NEI\n"
                  "  COND.SET 10 1ST\n  COND.SET 11 !1ST\n  COND.SET 12 FILL\n"
                  "  COND.SET 13 8:8\n  COND.SET 14 1%\n  COND.SET 15 5\n")

    def test_cond_const_and_user_arg(self):
        self.good("CONST C1 25%\nDEF CC\n  SETC | 4 C1\n  SETC | 5 3:4\n\n"
                  "DEF SETC | S K\n  COND.SET S K\n")

    def test_comments_and_odd_indentation(self):
        text = ("# a comment at column 1\n"
                "   # a comment with a tab\there and \xe9 non-ascii\n"
                "OTYPE 1   # version\n"
                "\n"
                "CONST DD \"A#B\"   # a # inside a string is not a comment\n"
                "          # comment indented deeply\n"
                "DEF ODD   # header\n"
                "      # comment at odd indentation\n"
                "    TR.ADD 1     # four spaces for this body\n"
                "\n"
                "                # another one\n"
                "    IF X:     # block\n"
                "          # inside, odd\n"
                "       TR.ADD 2\n"
                "       TR.ADD 3   # seven spaces: a block only needs to be uniform\n"
                "  # dedent comment between 4 and 7\n"
                "    TR.ADD 4\n")
        self.good(text)

    def test_line_endings(self):
        body = ["DEF A1", "  TR.ADD 1", "  TR.ADD 2", "DEF A2", "  TR.ADD 3"]
        for eol in ("\n", "\r\n", "\r"):
            self.good(eol.join(body) + eol)

    def test_case_insensitive(self):
        self.good("def xx\n  tr.add 1\n  a Add 1 mUl 2 3\n")

    def test_hex_and_negative_numbers(self):
        self.good("DEF H\n  A 0x7F\n  B -3\n  C - 3 1\n  MICRO.SET 1 -0x10\n  A + 1 2\n  A <= 1 2\n")

    def test_list_constant_and_ops(self):
        self.good("CONST SC {0 3 5 7 10}\nDEF LL\n  A PICK SC\n  B AT SC 2\n  C LEN SC\n"
                  "  A PICK {1 2 3}\n")

    def test_loops_with_const_param_and_arg(self):
        self.good("CONST N 8\nDEF M\n  MENU \"A\" >> \"B\"\n  PARAM P 4 1 8\n"
                  "  L 1 N: TR.ADD I\n  L 1 P: TR.ADD I\n  L 8 1: TR.ADD I\n"
                  "  LP | 3\n\nDEF LP | K\n  L 1 K:\n    L 1 K: TR.ADD J\n")

    def test_break_and_stop(self):
        self.good("DEF BB\n  L 1 8:\n    IF EQ I 4: BREAK\n    TR.ADD I\n  STOP\n  EACH:\n    BREAK\n")

    def test_decl_warnings_only(self):
        prog = self.check("DEF DD\n  MENU \"A\" >> \"B\" >> \"C\"\n  ON TRIG\n  ON FOO 1\n  TR.ADD 1\n")
        self.assertEqual(self.errs(prog), [])
        msgs = " | ".join(d.msg for d in self.warns(prog))
        self.assertIn("submenus not supported by the menu yet", msgs)
        self.assertIn("host not available yet", msgs)
        self.assertIn("unknown host", msgs)
        self.assertEqual(self.ok_names(prog), ["DD"])

    def test_several_menus_and_confirm(self):
        prog = self.check("DEF DD\n  MENU \"A\" >> \"B\"\n  MENU \"C\" >> \"D\"\n  CONFIRM \"SURE\"\n"
                          "  CONFIRM\n  TR.ADD 1\n")
        self.assertEqual(prog.diags, [])
        self.assertTrue(prog.funcs[0].confirm)

    def test_card_ops(self):
        self.good("DEF K\n  DIR \"A/B\"\n  DIR \"A/B\" DEEP\n  EXT WAV AIF\n  MATCH \"A*\" \"B*\"\n"
                  "  STATIC.LOAD 1 FILE.AT 2\n  MSG \"HI\"\n  STATIC.CLR 1\n  A FILE.LEN\n")

    def test_value_op_as_statement_is_a_warning(self):
        prog = self.check("DEF V\n  FILE.RND\n  TR.ADD 1\n")
        self.assertEqual(self.errs(prog), [])
        self.assertTrue(any("ignored" in d.msg and d.line == 2 for d in self.warns(prog)))

    def test_cross_file_calls_and_const(self):
        self.good("DEF A1\n  B1 | 2 DD\n", extra=["CONST DD \"X\"\nDEF B1 | N S\n  DIR S\n  TR.ADD N\n"])

    def test_destructive_flag_propagates(self):
        prog = self.check("DEF TOP\n  MID\n\nDEF MID\n  TR.CLR\n\nDEF SAFE\n  TR.ADD 1\n")
        d = {f.name: prog.destructive[f] for f in prog.funcs}
        self.assertEqual(d, {"TOP": True, "MID": True, "SAFE": False})


class LexiconErrors(Base):
    def test_tab_in_indentation(self):
        self.bad("DEF BAD\n\tTR.ADD 1\n  TR.ADD 2\n", 2, "tab in indentation")

    def test_tab_in_comment_ok(self):
        self.good("DEF XX\n  TR.ADD 1 # a\ttab\n\t# comment line\n")

    def test_non_ascii_outside_comment(self):
        self.bad("DEF BAD\n  MSG \"caf\xe9\"\n", 2, "outside a comment")

    def test_line_too_long(self):
        self.bad("DEF BAD\n  TR.ADD 1 # " + "x" * 130 + "\n", 2, "line too long")

    def test_line_of_128_is_ok(self):
        line = "  TR.ADD 1 #" + "x" * (128 - len("  TR.ADD 1 #"))
        self.assertEqual(len(line), 128)
        self.good("DEF XX\n" + line + "\n")

    def test_string_too_long(self):
        self.bad("DEF BAD\n  MSG \"" + "A" * 128 + "\"\n", 2, "string too long")

    def test_unterminated_string(self):
        self.bad("DEF BAD\n  MSG \"abc\n", 2, "unterminated string")

    def test_list_too_long(self):
        self.bad("CONST BAD {" + " ".join(map(str, range(17))) + "}\n", 1, "list too long", func="-",
                 refused=())

    def test_list_non_numbers(self):
        self.bad("DEF BAD\n  A PICK {1 2 X}\n", 2, "numbers only")

    def test_number_out_of_range(self):
        self.bad("DEF BAD\n  A 40000\n", 2, "16-bit")

    def test_bad_probability_condition(self):
        self.bad("DEF BAD\n  COND.SET 1 51%\n", 2, "not a trig condition")

    def test_bad_ratio_condition(self):
        self.bad("DEF BAD\n  COND.SET 1 5:3\n", 2, "not a trig condition")
        self.bad("DEF BAD\n  COND.SET 1 1:9\n", 2, "not a trig condition")

    def test_one_to_one_is_not_a_condition(self):
        # the OT has no "always" condition: 1:1 refuses the function
        prog = self.bad("DEF BAD\n  COND.SET 1 1:1\n", 2, "1:1 is not a trig condition")
        self.assertIn("cond.set s off", [d for d in self.errs(prog) if d.func == "bad"][0].msg)
        self.bad("DEF BAD\n  COND.RND 1:1 2:8\n", 2, "1:1 is not a trig condition")
        self.bad("DEF BAD\n  A 1:1\n", 2, "1:1 is not a trig condition")
        self.good("DEF K\n  COND.SET 1 1:2\n  COND.SET 2 8:8\n  COND.SET 3 OFF\n")

    def test_condition_where_number_expected(self):
        self.bad("DEF BAD\n  PROB 50%: TR.ADD 1\n", 2, "50% is a trig condition; write PROB 50:")

    def test_condition_ratio_where_number_expected(self):
        self.bad("DEF BAD\n  TR.ADD 1:4\n", 2, "trig condition")


class StructureErrors(Base):
    def test_stray_text_at_column_1(self):
        self.bad("DEF BAD\n  TR.ADD 1\nTR.ADD 2\n", 3, "column 1")

    def test_indented_def(self):
        prog = self.check("DEF OK1\n  TR.ADD 1\n  DEF XX\n")
        self.assertErr(prog, 3, "must start at column 1", "OK1")

    def test_def_without_name(self):
        prog = self.check(GOOD + "DEF\n  TR.ADD 1\n" + AFTER)
        self.assertErr(prog, 4, "function name", "?")
        self.assertIn("GOOD2", self.ok_names(prog))

    def test_unexpected_indent(self):
        self.bad("DEF BAD\n  TR.ADD 1\n    TR.ADD 2\n", 3, "unexpected indentation")

    def test_inconsistent_block_indent(self):
        self.bad("DEF BAD\n  IF X:\n    TR.ADD 1\n      TR.ADD 2\n", 4, "unexpected indentation")

    def test_dedent_to_nowhere(self):
        self.bad("DEF BAD\n    IF X:\n        TR.ADD 1\n      TR.ADD 2\n", 4, "does not match")

    def test_dedent_below_body(self):
        self.bad("DEF BAD\n    TR.ADD 1\n  TR.ADD 2\n", 3, "does not match")

    def test_empty_block_at_end(self):
        self.bad("DEF BAD\n  TR.ADD 1\n  IF X:\n", 3, "empty block")

    def test_empty_block_followed_by_sibling(self):
        self.bad("DEF BAD\n  IF X:\n  TR.ADD 1\n", 2, "empty block")

    def test_empty_function(self):
        self.bad("DEF BAD\n", 1, "no statement")

    def test_function_of_declarations_only(self):
        self.bad("DEF BAD\n  MENU \"A\" >> \"B\"\n", 1, "no statement")

    def test_nesting_too_deep(self):
        text = "DEF BAD\n"
        ind = 2
        for i in range(9):
            text += " " * ind + "IF X:\n"
            ind += 2
        text += " " * ind + "TR.ADD 1\n"
        # the 9th block opens on the line of the last (deepest) IF's body
        self.bad(text, 11, "nested deeper")

    def test_nesting_8_ok(self):
        text = "DEF XX\n"
        ind = 2
        for i in range(8):
            text += " " * ind + "IF X:\n"
            ind += 2
        text += " " * ind + "TR.ADD 1\n"
        self.good(text)

    def test_otype_unsupported_refuses_whole_file_only(self):
        prog = self.check("OTYPE 2\nDEF F1A\n  TR.ADD 1\nDEF F1B\n  TR.ADD 1\n",
                          "DEF F2\n  TR.ADD 1\n")
        self.assertErr(prog, 1, "not supported", "-")
        self.assertEqual(self.ok_names(prog), ["F2"])

    def test_otype_must_be_first(self):
        prog = self.check("DEF A1\n  TR.ADD 1\nOTYPE 1\n")
        self.assertErr(prog, 3, "first line", "-")
        self.assertEqual(self.ok_names(prog), ["A1"])

    def test_otype_after_comments_ok(self):
        self.good("# hello\n\nOTYPE 1\nDEF XX\n  TR.ADD 1\n")

    def test_indented_line_outside_function(self):
        prog = self.check("CONST A 1\n  TR.ADD 1\nDEF XX\n  TR.ADD 1\n")
        self.assertErr(prog, 2, "outside any function", "-")
        self.assertEqual(self.ok_names(prog), ["XX"])

    def test_function_ends_at_next_column_1_def_or_const(self):
        prog = self.check("DEF A1\n  TR.ADD 1\nCONST K 3\nDEF B1\n  TR.ADD K\n")
        self.assertEqual(prog.diags, [])
        # a statement after a CONST does not belong to A1
        prog = self.check("DEF A1\n  TR.ADD 1\nCONST K 3\n  TR.ADD 2\n")
        self.assertErr(prog, 4, "outside any function", "-")
        self.assertEqual(self.ok_names(prog), ["A1"])


class NameErrors(Base):
    def test_unknown_name(self):
        self.bad("DEF BAD\n  FROB 1\n", 2, "unknown name FROB")

    def test_unknown_name_in_expression(self):
        self.bad("DEF BAD\n  TR.ADD NOPE\n", 2, "unknown name NOPE")

    def test_name_with_dot(self):
        prog = self.check(GOOD + "DEF MY.FUNC\n  TR.ADD 1\n" + AFTER)
        self.assertErr(prog, 4, "no dot", "MY.FUNC")
        self.assertIn("GOOD2", self.ok_names(prog))

    def test_reserved_names(self):
        for nm in ("IF", "ON", "A", "TOSS", "CUR", "ADD", "TR", "PRE", "STEPS"):
            prog = self.check(GOOD + "DEF %s\n  TR.ADD 1\n" % nm + AFTER)
            if nm == "TR":
                self.assertFalse(self.errs(prog), nm)   # TR alone is not an operation
                continue
            self.assertErr(prog, 4, "reserved", nm)
            self.assertIn("GOOD", self.ok_names(prog))

    def test_name_too_long(self):
        self.bad("DEF " + "N" * 17 + "\n  TR.ADD 1\n", 1, "too long", func="N" * 17)

    def test_name_16_ok(self):
        self.good("DEF " + "N" * 16 + "\n  TR.ADD 1\n")

    def test_bad_name_characters(self):
        self.bad("DEF 9LIVES\n  TR.ADD 1\n", 1, "not a valid name", func="9LIVES")

    def test_duplicate_function_same_file(self):
        prog = self.check("DEF DUP\n  TR.ADD 1\nDEF DUP\n  TR.ADD 2\nDEF OK1\n  TR.ADD 3\n")
        self.assertErr(prog, 1, "also defined at F1.OTY:3", "DUP")
        self.assertErr(prog, 3, "also defined at F1.OTY:1", "DUP")
        self.assertEqual(self.ok_names(prog), ["OK1"])

    def test_duplicate_function_across_files(self):
        prog = self.check("DEF DUP\n  TR.ADD 1\nDEF OK1\n  TR.ADD 1\n", "\n\nDEF DUP\n  TR.ADD 2\n")
        self.assertErr(prog, 1, "also defined at F2.OTY:3", "DUP")
        self.assertErr(prog, 3, "also defined at F1.OTY:1", "DUP", file="F2.OTY")
        self.assertEqual(self.ok_names(prog), ["OK1"])

    def test_caller_of_duplicate_is_refused(self):
        prog = self.check("DEF DUP\n  TR.ADD 1\nDEF DUP\n  TR.ADD 2\nDEF CALLER\n  DUP\n"
                          "DEF OK1\n  TR.ADD 1\n")
        self.assertErr(prog, 6, "NEEDS DUP", "CALLER")
        self.assertEqual(self.ok_names(prog), ["OK1"])

    def test_duplicate_const(self):
        prog = self.check("CONST K 1\nCONST K 2\nDEF OK1\n  TR.ADD 1\n")
        self.assertErr(prog, 1, "duplicate CONST K", "-")
        self.assertErr(prog, 2, "duplicate CONST K", "-")
        self.assertEqual(self.ok_names(prog), ["OK1"])

    def test_duplicate_const_across_files(self):
        prog = self.check("CONST K 1\nDEF OK1\n  TR.ADD 1\n", "CONST K 2\n")
        self.assertErr(prog, 1, "also defined at F2.OTY:1", "-")
        self.assertErr(prog, 1, "also defined at F1.OTY:1", "-", file="F2.OTY")
        self.assertEqual(self.ok_names(prog), ["OK1"])

    def test_use_of_refused_const(self):
        prog = self.check("CONST K 1\nCONST K 2\nDEF USER\n  TR.ADD K\nDEF OK1\n  TR.ADD 1\n")
        self.assertErr(prog, 4, "CONST K is refused", "USER")
        self.assertEqual(self.ok_names(prog), ["OK1"])

    def test_const_value_forms(self):
        prog = self.check("CONST K ADD 1 2\nCONST L2\nCONST M 1 2\nCONST N X\nDEF OK1\n  TR.ADD 1\n")
        for line in (1, 2, 3, 4):
            self.assertEqual(len([d for d in self.errs(prog) if d.line == line]), 1, line)
        self.assertEqual(self.ok_names(prog), ["OK1"])

    def test_const_and_function_same_name(self):
        prog = self.check("CONST SAME 1\nDEF SAME\n  TR.ADD 1\nDEF OK1\n  TR.ADD 1\n")
        self.assertErr(prog, 1, "name of the function", "-")
        self.assertErr(prog, 2, "name of the CONST", "SAME")
        self.assertEqual(self.ok_names(prog), ["OK1"])

    def test_argument_names(self):
        self.bad("DEF BAD | A\n  TR.ADD 1\n", 1, "reserved")
        self.bad("DEF BAD | N N\n  TR.ADD N\n", 1, "duplicate argument")
        self.bad("DEF BAD | TR.ADD\n  TR.ADD 1\n", 1, "dot")
        prog = self.check("CONST N 3\nDEF BAD | N\n  TR.ADD 1\nDEF OK1\n  TR.ADD 1\n")
        self.assertErr(prog, 2, "hides the CONST", "BAD")

    def test_too_many_arguments(self):
        self.bad("DEF BAD | A1 B1 C1 D1 E1\n  TR.ADD A1\n", 1, "5 arguments (maximum 4)")

    def test_missing_bar_in_header(self):
        self.bad("DEF BAD X1\n  TR.ADD 1\n", 1, "expected '|'")

    def test_bar_without_arguments_in_header(self):
        self.bad("DEF BAD |\n  TR.ADD 1\n", 1, "without arguments")


class StatementErrors(Base):
    def test_arity_too_few(self):
        self.bad("DEF BAD\n  TR.ADD\n", 2, "TR.ADD expects 1 argument (STEP), got 0")

    def test_arity_too_few_nested(self):
        self.bad("DEF BAD\n  TR.ADD ADD 1\n", 2, "ADD expects 2 arguments")

    def test_arity_too_many(self):
        self.bad("DEF BAD\n  TR.ADD 1 2\n", 2, "TR.ADD expects 1 argument (STEP), got 2")

    def test_arity_zero_ary(self):
        self.bad("DEF BAD\n  TR.CLR 1\n", 2, "TR.CLR expects no argument, got 1")

    def test_bar_after_api_op(self):
        self.bad("DEF BAD\n  TR.ADD | 1\n", 2, "'|' is for your own functions")

    def test_bar_mandatory_for_user_function_with_args(self):
        prog = self.check(GOOD + "DEF BAD\n  TWO 1 2\n\nDEF TWO | P Q\n  TR.ADD P\n" + AFTER)
        self.assertErr(prog, 5, "write TWO | P Q", "BAD")
        self.assertIn("TWO", self.ok_names(prog))

    def test_bar_forbidden_for_user_function_without_args(self):
        prog = self.check(GOOD + "DEF BAD\n  NONE | 1\n\nDEF NONE\n  TR.ADD 1\n" + AFTER)
        self.assertErr(prog, 5, "takes no argument", "BAD")

    def test_user_call_arg_count(self):
        prog = self.check(GOOD + "DEF BAD\n  TWO | 1\n\nDEF TWO | P Q\n  TR.ADD P\n  TR.ADD Q\n" + AFTER)
        self.assertErr(prog, 5, "TWO expects 2 arguments, got 1", "BAD")
        prog = self.check(GOOD + "DEF BAD\n  TWO | 1 2 3\n\nDEF TWO | P Q\n  TR.ADD P\n  TR.ADD Q\n" + AFTER)
        self.assertErr(prog, 5, "TWO expects 2 arguments, got 3", "BAD")

    def test_bare_value_does_nothing(self):
        self.bad("DEF BAD\n  ADD 1 2\n", 2, "does nothing")
        self.bad("DEF BAD\n  5\n", 2, "does nothing")
        self.bad("DEF BAD\n  A\n", 2, "does nothing")
        self.bad("DEF BAD\n  OFF\n", 2, "does nothing")
        self.bad("DEF BAD\n  AMP.VOL\n", 2, "does nothing")

    def test_bare_arg_const_param_does_nothing(self):
        self.bad("DEF BAD | N\n  N\n", 2, "does nothing")
        self.bad("CONST K 1\nDEF BAD\n  K\n", 3, "CONST K is read-only")

    def test_read_only_assignment(self):
        self.bad("DEF BAD\n  I 3\n", 2, "read-only")
        self.bad("DEF BAD\n  J 3\n", 2, "read-only")
        self.bad("DEF BAD | N\n  N 3\n", 2, "argument (read-only)")
        self.bad("DEF BAD\n  MENU \"A\" >> \"B\"\n  PARAM P 1 0 2\n  P 3\n", 4, "PARAM (read-only)")
        self.bad("CONST K 1\nDEF BAD\n  K 3\n", 3, "read-only")

    def test_assignment_forms(self):
        self.good("DEF A1\n  A 1\n  B 2\n  C 3\n  D 4\n  X 5\n  Y 6\n  Z 7\n  T 8\n  X ADD X 1\n")

    def test_op_returning_no_value_in_expression(self):
        self.bad("DEF BAD\n  A TR.ADD 1\n", 2, "gives no value")
        self.bad("DEF BAD\n  A DIR \"X\"\n", 2, "gives no value")

    def test_break_outside_loop(self):
        self.bad("DEF BAD\n  BREAK\n", 2, "BREAK outside")
        self.bad("DEF BAD\n  IF X: BREAK\n", 2, "BREAK outside")

    def test_del_not_available(self):
        self.bad("DEF BAD\n  DEL 4: TR.ADD 1\n", 2, "not available yet")

    def test_colon_after_non_modifier(self):
        self.bad("DEF BAD\n  TR.ADD 1:\n", 2, "unexpected ':'")

    def test_missing_colon(self):
        self.bad("DEF BAD\n  IF X TR.ADD 1\n", 2, "expected ':'")

    def test_modifier_without_value(self):
        self.bad("DEF BAD\n  PROB : TR.ADD 1\n", 2, "expects a value")

    def test_keyword_where_value_expected(self):
        self.bad("DEF BAD\n  A ALL\n", 2, "keyword")


class DeclarationErrors(Base):
    def test_declaration_after_statement(self):
        self.bad("DEF BAD\n  TR.ADD 1\n  MENU \"A\" >> \"B\"\n", 3, "after the first statement")
        self.bad("DEF BAD\n  TR.ADD 1\n  CONFIRM\n", 3, "after the first statement")
        self.bad("DEF BAD\n  TR.ADD 1\n  ON TRIG\n", 3, "after the first statement")

    def test_declaration_in_block_or_with_modifier(self):
        self.bad("DEF BAD\n  IF X:\n    CONFIRM\n", 3, "inside a block")
        self.bad("DEF BAD\n  IF X: CONFIRM\n  TR.ADD 1\n", 2, "declaration")

    def test_menu_widths(self):
        prog = self.check("DEF OK1\n  MENU \"TENCHARS10\" >> \"OK\"\n  TR.ADD 1\n")
        self.assertEqual(len(self.errs(prog)), 0)
        self.assertTrue(any('menu category "TENCHARS10" longer than 9 chars (clipped on screen)' in d.msg for d in prog.diags))
        prog = self.check("DEF OK2\n  MENU \"TWELVECHARS_\" >> \"OK\"\n  TR.ADD 1\n")
        self.assertEqual(len(self.errs(prog)), 0)
        self.assertTrue(any('menu category "TWELVECHARS_" longer than 11 chars (put under "SCRIPTS")' in d.msg for d in prog.diags))
        prog = self.check("DEF OK3\n  MENU \"OK\" >> \"FOURTEENCHARS_\"\n  TR.ADD 1\n")
        self.assertEqual(len(self.errs(prog)), 0)
        self.assertTrue(any('script name "FOURTEENCHARS_" longer than 13 chars (clipped on screen)' in d.msg for d in prog.diags))
        self.good("DEF XX\n  MENU \"NINECHARS\" >> \"THIRTEENCHARS\"\n  TR.ADD 1\n")

    def test_menu_needs_two_strings(self):
        self.bad("DEF BAD\n  MENU \"ONLY\"\n  TR.ADD 1\n", 2, "at least one level")
        self.bad("DEF BAD\n  MENU \"A\" \"B\"\n  TR.ADD 1\n", 2, "separated by >>")

    def test_declaration_error_does_not_cascade(self):
        prog = self.check('DEF BAD\n  PAGE "TOOLONGPAGE"\n  PARAM P 1 0 5\n  TR.ADD P\n')
        self.assertEqual(len(self.errs(prog)), 1)

    def test_param_in_brick(self):
        self.bad("DEF BAD\n  PARAM P 1 0 5\n  TR.ADD P\n", 2, "a brick cannot declare PARAM")

    def test_param_bounds(self):
        self.bad("DEF BAD\n  MENU \"A\" >> \"B\"\n  PARAM P 9 0 5\n  TR.ADD 1\n", 3, "min <= default <= max")
        self.bad("DEF BAD\n  MENU \"A\" >> \"B\"\n  PARAM P 1 3 5\n  TR.ADD 1\n", 3, "min <= default <= max")
        self.bad("DEF BAD\n  MENU \"A\" >> \"B\"\n  PARAM P X 0 5\n  TR.ADD 1\n", 3, "number")

    def test_param_with_const_values(self):
        self.good("CONST HI 9\nDEF XX\n  MENU \"A\" >> \"B\"\n  PARAM P 4 0 HI\n  TR.ADD P\n")

    def test_param_limit_8(self):
        lines = "".join("  PARAM P%d 1 0 5\n" % i for i in range(9))
        self.bad("DEF BAD\n  MENU \"A\" >> \"B\"\n" + lines + "  TR.ADD 1\n", 11, "more than 8 PARAM")
        lines = "".join("  PARAM P%d 1 0 5\n" % i for i in range(8))
        self.good("DEF XX\n  MENU \"A\" >> \"B\"\n" + lines + "  TR.ADD 1\n")

    def test_param_duplicate_and_names(self):
        self.bad("DEF BAD | P\n  MENU \"A\" >> \"B\"\n  PARAM P 1 0 5\n  TR.ADD 1\n", 3, "name of an argument")
        self.bad("DEF BAD\n  MENU \"A\" >> \"B\"\n  PARAM P 1 0 5\n  PARAM P 1 0 5\n  TR.ADD 1\n", 4, "duplicate PARAM")
        self.bad("DEF BAD\n  MENU \"A\" >> \"B\"\n  PARAM TR.X 1 0 5\n  TR.ADD 1\n", 3, "dot")

    def test_on_declaration_needs_host(self):
        self.bad("DEF BAD\n  ON\n  TR.ADD 1\n", 2, "host")


class PageDeclarations(Base):
    """`page "LABEL"` (docs/syntax.md section 4): a function listed on the SCRIPT page of the unit."""

    def test_page_function_loads(self):
        prog = self.good('DEF EUCL\n  PAGE "EUCL"\n  PARAM PL1 4 0 64\n  TR.ADD PL1\n')
        f = prog.fmap["EUCL"]
        self.assertEqual(f.page, ("EUCL", 2))
        self.assertEqual(list(f.params), ["PL1"])

    def test_label_keeps_its_case_and_may_hold_spaces_and_punctuation(self):
        prog = self.good('def aa\n  page "Eucl 2~"\n  tr.add 1\n')
        self.assertEqual(prog.fmap["AA"].page[0], "Eucl 2~")

    def test_label_length(self):
        self.good('DEF XX\n  PAGE "A"\n  TR.ADD 1\n')
        self.good('DEF XX\n  PAGE "12345678"\n  TR.ADD 1\n')
        self.bad('DEF BAD\n  PAGE "123456789"\n  TR.ADD 1\n', 2, 'page label "123456789" is longer than 8')
        self.bad('DEF BAD\n  PAGE ""\n  TR.ADD 1\n', 2, "page label is empty")

    def test_page_needs_a_string(self):
        self.bad("DEF BAD\n  PAGE EUCL\n  TR.ADD 1\n", 2, 'page expects a string')
        self.bad("DEF BAD\n  PAGE\n  TR.ADD 1\n", 2, "unexpected end of line")
        self.bad('DEF BAD\n  PAGE "A" "B"\n  TR.ADD 1\n', 2, "after page declaration")

    def test_only_one_page(self):
        self.bad('DEF BAD\n  PAGE "A"\n  PAGE "B"\n  TR.ADD 1\n', 3, "duplicate page declaration")

    def test_declaration_rules_of_page(self):
        self.bad('DEF BAD\n  TR.ADD 1\n  PAGE "A"\n', 3, "after the first statement")
        self.bad('DEF BAD\n  IF X:\n    PAGE "A"\n', 3, "inside a block")

    def test_page_is_a_keyword(self):
        self.bad("DEF PAGE\n  TR.ADD 1\n", 1, "reserved name", func="PAGE", refused=())
        self.bad("DEF BAD\n  PARAM PAGE 1 0 2\n  TR.ADD 1\n", 2, "reserved name")

    def test_page_function_takes_no_argument(self):
        self.bad('DEF BAD | A\n  PAGE "A"\n  TR.ADD A\n', 2, "a page function takes no arguments")

    def test_params_without_menu(self):
        # a brick cannot declare param (the caller passes the value) ...
        self.bad("DEF BAD\n  PARAM P 1 0 5\n  TR.ADD P\n", 2, "a brick cannot declare PARAM")
        # ... a page function is not a brick
        self.good('DEF XX\n  PAGE "A"\n  PARAM P 1 0 5\n  TR.ADD P\n')
        self.good('DEF XX\n  PARAM P 1 0 5\n  PAGE "A"\n  TR.ADD P\n')      # in any order

    def test_at_most_six_params(self):
        six = "".join("  PARAM P%d 1 0 5\n" % i for i in range(6))
        self.good('DEF XX\n  PAGE "A"\n' + six + "  TR.ADD 1\n")
        seven = six + "  PARAM P6 1 0 5\n"
        self.bad('DEF BAD\n  PAGE "A"\n' + seven + "  TR.ADD 1\n", 2, "page function has more than 6 params")
        self.bad('DEF BAD\n' + seven + '  PAGE "A"\n  TR.ADD 1\n', 9, "page function has more than 6 params")
        eight = seven + "  PARAM P7 1 0 5\n"
        self.bad('DEF BAD\n  PAGE "A"\n' + eight + "  TR.ADD 1\n", 2, "page function has more than 6 params")
        # without `page` a function with a menu keeps its 8
        self.good('DEF XX\n  MENU "A" >> "B"\n' + eight + "  TR.ADD 1\n")

    def test_menu_and_page_together_and_confirm(self):
        prog = self.good('DEF XX\n  MENU "A" >> "B"\n  PAGE "PG"\n  CONFIRM\n  PARAM P 1 0 5\n  TR.ADD P\n')
        f = prog.fmap["XX"]
        self.assertTrue(f.menus and f.page and f.confirm)

    def test_card_slot_and_pool_ops_are_refused_in_a_page_function(self):
        ops = ['DIR "A"', "A FILE.LEN", "A STATIC.FREE 1", "SLOTS.RND", "STATIC.CLR 1", "SLOTS.CLR", "STATIC.LOAD 1 1",
               "A STATIC.FIRSTFREE", "STATIC.SWAP 1 2", 'MATCH "K*"', "EXT WAV"]
        for op in ops:
            with self.subTest(op=op):
                name = op.split()[1 if op.startswith("A ") else 0].lower()
                self.bad('DEF BAD\n  PAGE "A"\n  TR.ADD 1\n  %s\n' % op, 4,
                         "op %s not allowed in a page function" % name)
        # the same function without `page` is fine
        self.good("DEF XX\n  TR.ADD 1\n  STATIC.CLR 1\n")

    def test_first_op_in_source_order_is_named(self):
        self.bad('DEF BAD\n  PAGE "A"\n  STATIC.CLR 1\n  DIR "A"\n', 3, "op static.clr not allowed")
        self.bad('DEF BAD\n  PAGE "A"\n  L 1 4:\n    IF RND 2:\n      STATIC.CLR 1\n  DIR "B"\n', 5, "op static.clr")
        self.bad('DEF BAD\n  PAGE "A"\n  TR.ADD ADD FILE.LEN STATIC.FREE 1\n', 3, "op file.len")      # same line: the first in the line

    def test_pattern_reads_are_allowed_in_a_page_function(self):
        # IO_R also holds the reads of the pattern (has.trig pat.len pl.get trk.cur ...): only the card, the slots and the pool are forbidden
        self.good('DEF XX\n  PAGE "A"\n  L 1 16:\n    IF HAS.TRIG I: TR.DEL I\n  A TRK.CUR\n  B PL.GET 1 AMP.VOL\n  C PAT.LEN\n')

    def test_ops_through_calls(self):
        prog = self.check('DEF CARD\n  DIR "A"\nDEF BAD\n  PAGE "A"\n  TR.ADD 1\n  CARD\n')
        self.assertErr(prog, 6, "op dir not allowed in a page function", "BAD")      # at the call
        self.assertIn("CARD", self.ok_names(prog))                                    # the brick itself is fine
        prog = self.check('DEF C3\n  STATIC.CLR 1\nDEF C2\n  C3\nDEF C1\n  C2\nDEF BAD\n  PAGE "A"\n  C1\n')
        self.assertErr(prog, 9, "op static.clr not allowed in a page function", "BAD")
        # a direct op wins over an earlier call; between calls the first call that leads to one
        prog = self.check('DEF C\n  STATIC.CLR 1\nDEF BAD\n  PAGE "A"\n  C\n  DIR "X"\n')
        self.assertErr(prog, 6, "op dir not allowed")
        prog = self.check('DEF OK\n  TR.ADD 1\nDEF C1\n  STATIC.CLR 1\nDEF C2\n  STATIC.CLR 1\nDEF BAD\n  PAGE "A"\n  OK\n  C2\n  C1\n')
        self.assertErr(prog, 10, "op static.clr not allowed", "BAD")

    def test_callers_of_a_refused_page_function_are_refused_too(self):
        prog = self.check('DEF P\n  PAGE "A"\n  STATIC.CLR 1\nDEF G\n  P\n')
        self.assertErr(prog, 5, "needs p", "G")
        self.assertEqual(self.ok_names(prog), [])

    def test_a_page_function_may_call_other_page_functions_and_bricks(self):
        self.good('DEF BR\n  TR.ADD 1\nDEF P1\n  PAGE "A"\n  TR.ADD 2\nDEF P2\n  PAGE "B"\n  P1\n  BR\n')

    def test_cyclic_callee_is_left_to_the_loop_error(self):
        prog = self.check('DEF A1\n  B1\n  STATIC.CLR 1\nDEF B1\n  A1\nDEF P\n  PAGE "A"\n  A1\n')
        msgs = [d.msg for d in self.errs(prog)]
        self.assertIn("needs a1", msgs)
        self.assertFalse([m for m in msgs if "page function" in m])

    def test_check_listing_and_json_show_the_page(self):
        import io
        prog = otype.check_sources([("F1.OTY", 'def p\n  page "PG"\n  param q 1 0 5\n  tr.add q\n')])
        out = io.StringIO()
        otype.report(prog, True, out)
        self.assertIn('page "PG"', out.getvalue())
        self.assertEqual(otype.check_json(prog)["functions"][0]["page"], "PG")
        self.assertIn("page", [k for k, _sig, _doc in otype.KEYWORD_DOCS])


class ModifierErrors(Base):
    def test_else_without_if(self):
        self.bad("DEF BAD\n  ELSE: TR.ADD 1\n", 2, "ELSE without IF")
        self.bad("DEF BAD\n  ELSE:\n    TR.ADD 1\n", 2, "ELSE without IF")

    def test_elif_without_if(self):
        self.bad("DEF BAD\n  TR.ADD 1\n  ELIF X: TR.ADD 1\n", 3, "ELIF without IF")

    def test_else_after_non_if_line(self):
        self.bad("DEF BAD\n  IF X: TR.ADD 1\n  TR.ADD 2\n  ELSE: TR.ADD 3\n", 4, "ELSE without IF")

    def test_else_after_chain_that_does_not_start_with_if(self):
        self.bad("DEF BAD\n  PROB 50: IF X: TR.ADD 1\n  ELSE: TR.ADD 3\n", 3, "ELSE without IF")

    def test_else_after_else(self):
        self.bad("DEF BAD\n  IF X: TR.ADD 1\n  ELSE: TR.ADD 2\n  ELSE: TR.ADD 3\n", 4, "ELSE without IF")

    def test_else_not_first_modifier(self):
        self.bad("DEF BAD\n  IF X: TR.ADD 1\n  PROB 50: ELSE: TR.ADD 2\n", 3, "ELSE without IF")

    def test_else_at_other_indentation(self):
        # the ELSE is in a nested block: the IF is not at the same indentation
        self.bad("DEF BAD\n  IF X: TR.ADD 1\n  IF Y:\n    ELSE: TR.ADD 2\n    TR.ADD 3\n", 4, "ELSE without IF")

    def test_l_bound_variable(self):
        self.bad("DEF BAD\n  A 4\n  L 1 A: TR.ADD I\n", 3, "L bound must be constant (A)")
        self.bad("DEF BAD\n  L X 4: TR.ADD I\n", 2, "L bound must be constant (X)")
        self.bad("DEF BAD\n  L 1 ADD 1 2: TR.ADD I\n", 2, "L bound must be constant")
        self.bad("DEF BAD\n  L 1 I: TR.ADD I\n", 2, "L bound must be constant (I)")

    def test_l_bound_argument_not_constant_at_one_call_site(self):
        text = ("DEF LP | N\n  L 1 N: TR.ADD I\n\n"
                "DEF C1\n  LP | 4\n\nDEF C2\n  A 3\n  LP | A\n")
        prog = self.check(GOOD + text + AFTER)
        self.assertErr(prog, FIRST + 1, "L bound must be constant (N)", "LP")
        ok = self.ok_names(prog)
        self.assertIn("GOOD", ok)
        self.assertNotIn("LP", ok)
        # callers of the refused function are refused too
        self.assertErr(prog, FIRST + 4, "NEEDS LP", "C1")
        self.assertErr(prog, FIRST + 8, "NEEDS LP", "C2")

    def test_l_bound_argument_propagates_through_calls(self):
        ok = ("DEF TOP | N\n  MID | N\n\nDEF MID | M\n  L 1 M: TR.ADD I\n\n"
              "DEF USE\n  TOP | 5\n")
        prog = self.check(ok)
        self.assertClean(prog)
        bad = ok + "DEF USE2\n  X 1\n  TOP | X\n"
        prog = self.check(bad)
        self.assertErr(prog, 5, "L bound must be constant (M)", "MID")

    def test_l_bound_argument_from_caller_param_or_const(self):
        self.good("CONST K 6\nDEF M\n  MENU \"A\" >> \"B\"\n  PARAM P 3 1 8\n  LP | P\n  LP | K\n  LP | 2\n\n"
                  "DEF LP | N\n  L 1 N: TR.ADD I\n")

    def test_l_bound_string_is_type_error(self):
        self.bad("DEF BAD\n  L 1 \"A\": TR.ADD I\n", 2, "expected a number")


class ArgumentKindErrors(Base):
    def test_string_where_number_expected(self):
        self.bad("DEF BAD\n  TR.ADD \"A\"\n", 2, "expected a number")

    def test_number_where_string_expected(self):
        self.bad("DEF BAD\n  DIR 3\n", 2, "expected a string")
        self.bad("DEF BAD\n  MSG {1 2}\n", 2, "expected a string or a number")

    def test_expression_where_constant_expected(self):
        self.bad("DEF BAD\n  A PICK X\n", 2, "expected a list")

    def test_list_where_number_expected(self):
        self.bad("DEF BAD\n  TR.ADD {1 2}\n", 2, "expected a number")

    def test_keyword_arguments(self):
        self.bad("DEF BAD\n  EXT MP3\n", 2, "expected one of")
        self.bad("DEF BAD\n  DIR \"A\" DEEPER\n", 2, "DIR expects")

    def test_constant_out_of_range(self):
        self.bad("DEF BAD\n  TR.ADD 65\n", 2, "65 is out of range for STEP (1..64)")
        self.bad("DEF BAD\n  TR.ADD 0\n", 2, "out of range for STEP")
        self.bad("DEF BAD\n  TRK 9: TR.ADD 1\n", 2, "out of range for TRACK (1..8)")
        self.bad("DEF BAD\n  TRK.SLOT 129\n", 2, "out of range for SLOT (1..128)")
        self.bad("DEF BAD\n  PL.CLR\n  PL.DEL 1 30\n", 3, "out of range for PARAM (0..29)")
        self.bad("DEF BAD\n  TL.FILL 101\n", 2, "out of range for PCT (0..100)")
        self.bad("CONST BIG 70\nDEF BAD\n  TR.ADD BIG\n", 3, "out of range for STEP")

    def test_range_limits_are_inclusive(self):
        self.good("DEF R\n  TR.ADD 1\n  TR.ADD 64\n  TRK 1 8: TR.ADD 1\n  TRK.SLOT 1\n  TRK.SLOT 128\n"
                  "  PL.DEL 1 0\n  PL.DEL 1 29\n  TL.FILL 0\n  TL.FILL 100\n")

    def test_track_list_limit(self):
        self.bad("DEF BAD\n  TRK 1 2 3 4 5 6 7 8 1: TR.ADD 1\n", 2, "at most 8 tracks")

    def test_context_argument_errors(self):
        self.bad("DEF BAD\n  TRK:\n    TR.ADD 1\n", 2, "TRK expects")
        self.bad("DEF BAD\n  STEPS 1:\n    TR.ADD 1\n", 2, "STEPS expects")
        self.bad("DEF BAD\n  STEPS HAS.TRIG 3: TR.ADD 1\n", 2, "STEPS expects")
        self.bad("DEF BAD\n  STEP:\n    TR.ADD 1\n", 2, "STEP expects 1 argument")

    def test_predicate_must_be_registered_predicate(self):
        self.bad("DEF BAD\n  STEPS FILE.LEN:\n    TR.ADD 1\n", 2, "STEPS expects")

    def test_user_arg_kinds_inferred_and_checked(self):
        lib = "DEF S1 | PATH\n  DIR PATH\n\nDEF N1 | K\n  TR.ADD K\n\nDEF L1 | LS\n  A PICK LS\n"
        # the right kinds pass
        ok = lib + "DEF OKC\n  S1 | \"X\"\n  N1 | 3\n  L1 | {1 2}\n"
        self.assertClean(self.check(ok))
        base = FIRST + 9 - 3   # line of the first statement of the function appended below
        for call, sub in (("S1 | 3", "expected a string"),
                          ("N1 | \"X\"", "expected a number"),
                          ("L1 | 3", "expected a list"),
                          ("S1 | X", "expected a string")):
            prog = self.check(GOOD + lib + "DEF BAD\n  " + call + "\n" + AFTER)
            self.assertErr(prog, FIRST + 9, sub, "BAD")
            self.assertIn("GOOD2", self.ok_names(prog))
            self.assertIn("S1", self.ok_names(prog))

    def test_user_arg_kind_through_nested_calls(self):
        text = ("DEF TOP | F\n  MID | F\n\nDEF MID | G\n  DIR G\n\nDEF OKC\n  TOP | \"A\"\n"
                "DEF BADC\n  TOP | 3\n")
        prog = self.check(text)
        self.assertErr(prog, 10, "expected a string", "BADC")
        self.assertEqual(self.ok_names(prog), ["MID", "OKC", "TOP"])

    def test_arg_used_as_two_kinds(self):
        self.bad("DEF BAD | P\n  DIR P\n  TR.ADD P\n", 3, "used as a string elsewhere")
        self.bad("DEF BAD | P\n  A PICK P\n  DIR P\n", 2, "elsewhere")

    def test_cond_argument_needs_constant_condition(self):
        lib = "DEF SETC | S K\n  COND.SET S K\n\n"
        prog = self.check(GOOD + lib + "DEF BAD\n  SETC | 1 5\n" + AFTER)
        self.assertErr(prog, FIRST + 4, "expected a condition", "BAD")
        prog = self.check(GOOD + lib + "DEF OKC\n  SETC | 1 50%\n  SETC | 1 2:3\n" + AFTER)
        self.assertEqual(self.errs(prog), [])

    def test_cond_argument_also_used_as_number_is_a_number(self):
        # COND accepts an expression: so an argument used both ways is just a number
        self.good("DEF F | K\n  COND.SET 1 K\n  TR.ADD K\n\nDEF U\n  F | 3\n")

    def test_const_type_mismatch(self):
        self.bad("CONST S \"A\"\nDEF BAD\n  TR.ADD S\n", 3, "expected a number")
        self.bad("CONST N 3\nDEF BAD\n  DIR N\n", 3, "expected a string")


class GraphErrors(Base):
    def test_direct_recursion(self):
        prog = self.check(GOOD + "DEF SELF\n  SELF\n" + AFTER)
        self.assertErr(prog, FIRST, "LOOP SELF>SELF", "SELF")
        self.assertIn("GOOD2", self.ok_names(prog))

    def test_cycle_reported_on_each_function(self):
        text = "DEF AA\n  BB\n\nDEF BB\n  CC\n\nDEF CC\n  AA\n\nDEF FINE\n  TR.ADD 1\n"
        prog = self.check(text)
        self.assertErr(prog, 1, "LOOP AA>BB>CC>AA", "AA")
        self.assertErr(prog, 4, "LOOP BB>CC>AA>BB", "BB")
        self.assertErr(prog, 7, "LOOP CC>AA>BB>CC", "CC")
        self.assertEqual(self.ok_names(prog), ["FINE"])

    def test_caller_of_cycle_is_refused(self):
        text = "DEF AA\n  BB\n\nDEF BB\n  AA\n\nDEF TOP\n  AA\n"
        prog = self.check(text)
        self.assertErr(prog, 8, "NEEDS AA", "TOP")
        self.assertEqual(self.ok_names(prog), [])

    def test_depth_8_ok_and_9_refused(self):
        def chain(n):
            t = ""
            for i in range(1, n):
                t += "DEF F%d\n  F%d\n\n" % (i, i + 1)
            return t + "DEF F%d\n  TR.ADD 1\n" % n
        self.assertClean(self.check(chain(8)))
        prog = self.check(chain(9))
        self.assertErr(prog, 1, "call depth 9 > 8", "F1")
        # the leaf and the functions below the limit still load
        self.assertIn("F9", self.ok_names(prog))
        self.assertNotIn("F1", self.ok_names(prog))

    def test_needs_refused_function(self):
        text = ("DEF BROKEN\n  TR.ADD\n\nDEF MID\n  BROKEN\n\nDEF TOP\n  MID\n\nDEF FINE\n  TR.ADD 1\n")
        prog = self.check(text)
        self.assertErr(prog, 2, "expects", "BROKEN")
        self.assertErr(prog, 5, "NEEDS BROKEN", "MID")
        self.assertErr(prog, 8, "NEEDS MID", "TOP")
        self.assertEqual(self.ok_names(prog), ["FINE"])

    def test_needs_across_files(self):
        prog = self.check("DEF TOP\n  LIBF\n", "DEF LIBF\n  TR.ADD\n")
        self.assertErr(prog, 2, "NEEDS LIBF", "TOP")
        self.assertErr(prog, 2, "expects", "LIBF", file="F2.OTY")

    def test_unknown_function_call(self):
        self.bad("DEF BAD\n  NOWHERE | 1\n", 2, "unknown name NOWHERE")


class CostTests(Base):
    MENU = "  MENU \"A\" >> \"B\"\n"

    def cost(self, body, extra=""):
        prog = self.check("DEF M\n" + self.MENU + body + extra)
        self.assertEqual(self.errs(prog), [], [str(d) for d in prog.diags])
        return prog.maxcost[prog.funcs[0]]

    def test_basic_costs(self):
        self.assertEqual(self.cost("  TR.ADD 1\n"), 4)
        self.assertEqual(self.cost("  A ADD 1 MUL 2 3\n"), 2)
        self.assertEqual(self.cost("  A FILE.RND\n"), 2)
        self.assertEqual(self.cost("  TR.CLR\n"), 128)
        self.assertEqual(self.cost("  DIR \"X\"\n"), 32)
        self.assertEqual(self.cost("  STATIC.LOAD 1 FILE.RND\n"), 10)

    def test_loop_cost(self):
        self.assertEqual(self.cost("  L 1 16: TR.ADD I\n"), 64)
        self.assertEqual(self.cost("  L 16 1: TR.ADD I\n"), 64)
        self.assertEqual(self.cost("  L 3 3: TR.ADD I\n"), 4)
        self.assertEqual(self.cost("  L 1 4:\n    L 1 4: TR.ADD I\n"), 64)

    def test_each_cost(self):
        self.assertEqual(self.cost("  EACH: TR.ADD I\n"), 64 * 4)

    def test_if_cost_is_cond_plus_max_branch(self):
        self.assertEqual(self.cost("  IF X: TR.ADD 1\n"), 4)
        self.assertEqual(self.cost("  IF ADD X 1: TR.CLR\n  ELSE: TR.ADD 1\n"), 1 + 128)
        # chain: c1 + max(b1, c2 + max(b2, b3))
        self.assertEqual(self.cost("  IF ADD X 1: TR.ADD 1\n  ELIF EQ X 2: TR.CLR\n  ELSE: SMP.CLR\n"),
                         1 + max(4, 1 + max(128, 128)))

    def test_modifier_cost_is_body(self):
        self.assertEqual(self.cost("  PROB 50: TR.ADD 1\n"), 4)
        self.assertEqual(self.cost("  EVERY 2: TR.ADD 1\n"), 4)
        self.assertEqual(self.cost("  SKIP 2: TR.ADD 1\n"), 4)

    def test_trk_multiplies_what_it_governs_by_its_tracks(self):
        # a multi-track trk runs its body (modifier) or the rest of its block (statement) per track
        self.assertEqual(self.cost("  TRK 1: TR.ADD 1\n"), 1 + 4)
        self.assertEqual(self.cost("  TRK 1 2: TR.ADD 1\n"), 1 + 2 * 4)
        self.assertEqual(self.cost("  TRK ALL: TR.CLR\n"), 1 + 8 * 128)
        self.assertEqual(self.cost("  TRK CUR: TR.CLR\n"), 1 + 128)
        self.assertEqual(self.cost("  TRK 1 2 3:\n    TR.ADD 1\n    TR.ADD 2\n"), 1 + 3 * 8)
        self.assertEqual(self.cost("  TRK 1 2\n  TR.ADD 1\n  TR.ADD 2\n"), 1 + 2 * 8)
        self.assertEqual(self.cost("  TR.ADD 9\n  TRK 1 2\n  TR.ADD 1\n"), 4 + 1 + 2 * 4)
        self.assertEqual(self.cost("  TRK 1 2:\n    TRK 3 4: TR.ADD 1\n"), 1 + 2 * (1 + 2 * 4))

    def test_param_bound_uses_worst_case(self):
        c = self.cost("  PARAM P 2 1 10\n  L 1 P: TR.ADD I\n")
        self.assertEqual(c, 40)

    def test_arg_bound_costed_per_call_site(self):
        lib = "\nDEF LP | N\n  L 1 N: TR.ADD I\n"
        self.assertEqual(self.cost("  LP | 3\n  LP | 5\n", lib), 4 * 3 + 4 * 5)

    def test_user_call_cost_with_const_path(self):
        lib = ("\nDEF OUT | N\n  L 1 N: IN | N\n\nDEF IN | K\n  L 1 K: TR.ADD I\n")
        # per path: OUT|4 -> 4 x IN|4 -> 4 x (4 x 4) = 64
        self.assertEqual(self.cost("  OUT | 4\n", lib), 4 * 4 * 4)

    def test_too_long_menu_function_refused(self):
        text = "DEF BIG\n" + self.MENU + "  L 1 40: TR.CLR\n"
        prog = self.check(GOOD + text + AFTER)
        self.assertErr(prog, FIRST, "too long: 5120/4096", "BIG")
        self.assertIn("GOOD", self.ok_names(prog))

    def test_exactly_at_budget_is_ok(self):
        prog = self.check("DEF BIG\n" + self.MENU + "  L 1 32: TR.CLR\n")
        self.assertEqual(self.errs(prog), [])
        self.assertEqual(prog.maxcost[prog.funcs[0]], 4096)

    def test_brick_is_not_checked_against_budget(self):
        prog = self.check("DEF BIG\n  L 1 40: TR.CLR\n")
        self.assertEqual(self.errs(prog), [])
        self.assertEqual(prog.maxcost[prog.funcs[0]], 5120)

    def test_menu_function_too_long_through_calls(self):
        text = ("DEF BIG\n" + self.MENU + "  HEAVY\n  HEAVY\n\nDEF HEAVY\n  L 1 20: TR.CLR\n"
                "\nDEF FINE\n  TR.ADD 1\n")
        prog = self.check(text)
        self.assertErr(prog, 1, "too long: 5120/4096", "BIG")
        self.assertIn("HEAVY", self.ok_names(prog))
        self.assertIn("FINE", self.ok_names(prog))

    def test_caller_of_too_long_menu_function_is_refused(self):
        text = ("DEF BIG\n" + self.MENU + "  L 1 40: TR.CLR\n\nDEF CALLER\n  BIG\n")
        prog = self.check(text)
        self.assertErr(prog, 1, "too long", "BIG")
        self.assertErr(prog, 6, "NEEDS BIG", "CALLER")


class FunctionLimit(Base):
    def test_more_than_128_functions_warns(self):
        text = "".join("DEF F%d\n  TR.ADD 1\n" % i for i in range(129))
        prog = self.check(text)
        self.assertEqual(self.errs(prog), [])
        self.assertTrue(any("129 functions" in d.msg for d in self.warns(prog)))
        prog = self.check("".join("DEF F%d\n  TR.ADD 1\n" % i for i in range(128)))
        self.assertEqual(prog.diags, [])


class Output(Base):
    def test_summary_and_sorting(self):
        import io
        prog = self.check("DEF B1\n  TR.ADD\nDEF A1\n  TR.ADD 1\n", "DEF C1\n  FROB\n")
        buf = io.StringIO()
        otype.report(prog, False, buf)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "F1.OTY:2: b1: error: tr.add expects 1 argument (step), got 0")
        self.assertEqual(lines[1], "F2.OTY:2: c1: error: unknown name frob")
        self.assertEqual(lines[-1], "1 functions OK, 2 refused, 0 warnings")

    def test_verbose_lists_menu_flags_cost(self):
        import io
        prog = self.check("DEF M\n  MENU \"DRUMS\" >> \"BOOM\"\n  TR.CLR\n")
        buf = io.StringIO()
        otype.report(prog, True, buf)
        out = buf.getvalue()
        self.assertIn("ok  m ", out)                    # the function name, lower case
        self.assertIn("DRUMS >> BOOM", out)               # the menu strings keep their case
        self.assertIn("destructive (confirm)", out)
        self.assertIn("max cost 128/4096", out)

    def test_registry_is_consistent(self):
        from modules.otype import ops
        ids = [o.id for o in ops.ALL_OPS]
        self.assertEqual(len(ids), len(set(ids)))
        for n in ("+", "-", "*", "/", "==", "!=", "<", ">", "<=", ">="):
            self.assertIn(n, ops.OPS)
        self.assertIs(ops.OPS["+"], ops.OPS["ADD"])
        self.assertEqual(ops.OPS["AMP.VOL"].value, 15)
        self.assertTrue(ops.OPS["TR.CLR"].destructive)
        self.assertEqual(ops.OPS["TEMPO"].level, "to establish")


class StateAndRhythm(Base):
    def test_rhythm_ops(self):
        self.good("DEF RR\n  L 1 16:\n    IF ER 5 16 I: TR.ADD I\n"
                  "    IF ER.R 3 8 2 I: PL.SET I AMP.VOL DR.V 2 I\n")

    def test_rhythm_arity(self):
        self.bad("DEF BAD\n  A ER 5 16\n", 2, "ER expects 3 arguments (INT INT INT)")
        self.bad("DEF BAD\n  A ER.R 1 2 3\n", 2, "ER.R expects 4 arguments")
        self.bad("DEF BAD\n  A DR.V 1\n", 2, "DR.V expects 2 arguments")

    def test_rhythm_value_needs_destination(self):
        self.bad("DEF BAD\n  ER 1 2 3\n", 2, "does nothing")

    def test_flip_seed_example(self):
        self.good("DEF AB\n  MENU \"A\" >> \"B\"\n  SEED ? FLIP 7 0\n  TR.ADD 1\n")

    def test_getter_in_expression_and_setter_statement(self):
        self.good("DEF GG\n  A FLIP\n  B O\n  C DRUNK\n  FLIP 1\n  O 0\n  DRUNK 64\n"
                  "  O ADD A 1\n  TR.ADD ADD O 1\n")

    def test_set_only_names(self):
        self.good("DEF SS\n  O.MIN 1\n  O.MAX 16\n  O.WRAP 0\n  O.INC 2\n"
                  "  DRUNK.MIN 10\n  DRUNK.MAX 100\n  DRUNK.WRAP 1\n  O.MAX ADD 2 3\n")

    def test_set_only_names_are_not_values(self):
        self.bad("DEF BAD\n  A O.MAX\n", 2, "gives no value")
        self.bad("DEF BAD\n  O.MAX\n", 2, "O.MAX expects 1 argument (INT), got 0")
        self.bad("DEF BAD\n  O.MIN 1 2\n", 2, "O.MIN expects 1 argument (INT), got 2")
        self.bad("DEF BAD\n  TR.ADD O.INC 1\n", 2, "gives no value")

    def test_setter_value_must_be_a_number(self):
        self.bad("DEF BAD\n  O \"A\"\n", 2, "expected a number")
        self.bad("DEF BAD\n  FLIP 1 2\n", 2, "unexpected")

    def test_bare_getter_is_a_warning(self):
        prog = self.check("DEF BB\n  FLIP\n  TR.ADD 1\n")
        self.assertEqual(self.errs(prog), [])
        self.assertTrue(any("ignored" in d.msg and d.line == 2 for d in self.warns(prog)))

    def test_getter_with_modifier_colon_is_a_read(self):
        self.good("DEF BB\n  IF FLIP: TR.ADD 1\n  IF O: TR.ADD 2\n  PROB DRUNK: TR.ADD 3\n")

    def test_state_names_are_reserved(self):
        for nm in ("FLIP", "O", "DRUNK", "O.MAX"):
            prog = self.check(GOOD + "DEF %s\n  TR.ADD 1\n" % nm + AFTER)
            self.assertTrue(self.errs(prog), nm)
            self.assertIn("GOOD", self.ok_names(prog))
        self.bad("DEF BAD | O\n  TR.ADD 1\n", 1, "reserved")
        self.bad("CONST FLIP 1\nDEF BAD\n  TR.ADD 1\n", 1, "reserved", func="-", refused=())

    def test_state_cost(self):
        prog = self.check("DEF M\n  MENU \"A\" >> \"B\"\n  O 3\n  A FLIP\n  B DRUNK\n")
        self.assertEqual(prog.maxcost[prog.funcs[0]], 1 + 2 + 2)

    def test_stream_seeds_as_statement_and_modifier(self):
        self.good("DEF SD\n  RAND.SEED 1\n  TOSS.SEED 2\n  PROB.SEED 3\n  DRUNK.SEED 4\n"
                  "  FILE.SEED 5\n  SEED 6\n  TR.ADD 1\n"
                  "  RAND.SEED 9: TR.ADD A\n  PROB.SEED 3:\n    PROB 50: TR.ADD 2\n"
                  "  L 1 2: DRUNK.SEED I: TR.ADD I\n")

    def test_stream_seed_errors(self):
        self.bad("DEF BAD\n  RAND.SEED\n", 2, "RAND.SEED expects 1 argument")
        self.bad("DEF BAD\n  A TOSS.SEED 1\n", 2, "gives no value")
        self.bad("DEF BAD\n  FILE.SEED 1:\n", 2, "empty block")

    def test_streams_in_registry(self):
        from modules.otype import ops
        expect = {"RND": "RAND", "RRAND": "RAND", "PICK": "RAND", "TOSS": "TOSS", "CHANCE": "TOSS",
                  "DRUNK": "DRUNK", "FILE.RND": "FILE", "FILE.PICK": "FILE", "SLOTS.RND": "FILE"}
        for n in ("PL.RND", "SMP.RND", "COND.RND", "MICRO.RND", "TL.FILL"):
            expect[n] = "RAND"
        for n, st in expect.items():
            self.assertEqual(ops.OPS[n].stream, st, n)
        self.assertEqual(ops.MODIFIER_STREAMS, {"PROB": "PROB"})
        drawing = {o.name for o in ops.ALL_OPS if o.stream}
        self.assertTrue(set(expect) <= drawing)
        for o in ops.ALL_OPS:
            self.assertIn(o.stream, (None,) + ops.STREAMS)

    def test_registry_metadata_complete(self):
        from modules.otype import ops
        for o in ops.ALL_OPS:
            self.assertTrue(o.doc.strip(), o.name)
            self.assertIn(o.category, ops.CATEGORIES, o.name)
        cats = {o.category for o in ops.ALL_OPS}
        self.assertEqual(cats, set(ops.CATEGORIES))
        with self.assertRaises(ValueError):
            ops.op("ZZ.NODOC", "UI", 0x7F, category="UI")
        with self.assertRaises(ValueError):
            ops.op("ZZ.NOCAT", "UI", 0x7F, doc="x")
        self.assertNotIn("ZZ.NODOC", ops.OPS)

    def test_registry_costs(self):
        from modules.otype import ops
        self.assertEqual(ops.OPS["STATIC.LOAD"].cost, 8)
        for n in ("SCN.CLR", "TRK.INIT"):
            self.assertEqual(ops.OPS[n].cost, 128, n)
        self.assertEqual(ops.OPS["ER"].cost, 2)
        self.assertEqual(ops.OPS["DR.V"].level, "vm")             # its tables are written (04/10)
        self.assertTrue(ops.OPS["FLIP"].settable and ops.OPS["O"].settable and ops.OPS["DRUNK"].settable)

    def test_signatures(self):
        from modules.otype import ops
        self.assertEqual(ops.OPS["PL.SET"].signature(), "pl.set s p v")
        self.assertEqual(ops.OPS["ER.R"].signature(), "er.r k n r s")
        self.assertEqual(ops.OPS["DIR"].signature(), "dir str [deep]")
        self.assertEqual(ops.OPS["EXT"].signature(), "ext wav|aif|aiff|aifc|...")
        self.assertEqual(ops.OPS["STEPS"].signature(), "steps all|a b|pred")

    def test_ops_listing(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(otype.main(["otype.py", "ops"]), 0)
            self.assertEqual(otype.main(["otype.py", "ops", "--md"]), 0)
        text = out.getvalue()
        self.assertIn("  er k n s", text)
        self.assertIn("| `er k n s` |", text)
        self.assertIn("### PATTERN", text)
        for o in __import__("modules.otype.ops", fromlist=["x"]).ALL_OPS:
            self.assertIn(o.name.lower(), text)
        # the signature column of the listing (text and markdown) is lower case
        for line in text.splitlines():
            if line.startswith("  "):
                sig = line[2:32].strip()
            elif line.startswith("| `"):
                sig = line[3:line.index("`", 3)]
            else:
                continue                                  # a category heading, a table header
            self.assertEqual(sig, sig.lower(), line)
        self.assertIn("flip  /  flip x", text)
        self.assertIn("lfo.wave n  /  lfo.wave n x", text)
        self.assertIn("`lfo.wave n x`", text)
        self.assertIn("steps all|a b|pred", text)

    def test_messages_do_not_repeat_the_function_name(self):
        cases = ["DEF T\n  TR.ADD 1\n", "DEF MY.F\n  TR.ADD 1\n", "DEF " + "N" * 17 + "\n  TR.ADD 1\n",
                 "DEF DUP\n  TR.ADD 1\nDEF DUP\n  TR.ADD 1\n", "DEF EMPTYF\n",
                 "DEF F | A\n  TR.ADD 1\n", "DEF F | A1 B1 C1 D1 E1\n  TR.ADD A1\n",
                 "DEF F\n  PARAM P 1 0 2\n  TR.ADD 1\n", "DEF F | N\n  PARAM N 1 0 2\n  TR.ADD 1\n",
                 "CONST F 1\nDEF F\n  TR.ADD 1\n"]
        for c in cases:
            prog = self.check(c)
            self.assertTrue(self.errs(prog), c)
            for d in self.errs(prog):
                self.assertFalse(d.func != "-" and d.msg.lower().startswith(d.func + ":"), str(d))
                self.assertFalse(d.func != "-" and d.msg.lower().startswith(d.func + " "), str(d))


# ---- editor integration: --json, --stdin-file, ops --json, unused arguments --------
class EditorSupport(Base):
    TOOL = os.path.join(ROOT, "tools", "otype.py")

    def run_tool(self, args, stdin=None):
        import subprocess
        r = subprocess.run([sys.executable, self.TOOL] + args, input=stdin, capture_output=True)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def tmp(self, name, text):
        import tempfile
        d = getattr(self, "_dir", None)
        if d is None:
            d = self._dir = tempfile.mkdtemp()
            self.addCleanup(__import__("shutil").rmtree, d, True)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def diag_text(self, j, d):
        """The text a diagnostic's span covers."""
        line = j["_lines"][d["line"] - 1]
        return line[d["col"] - 1:d["endCol"] - 1]

    def check_json(self, text):
        prog = self.check(text)
        j = otype.check_json(prog)
        j["_lines"] = text.split("\n")
        return j

    def test_json_diagnostic_columns_of_a_token(self):
        j = self.check_json("DEF F\n  L 1 4: TR.ADD ZZZ\n  TR.ADD 99999\n  TR.ADD \"x\"\n")
        by_line = {d["line"]: d for d in j["diagnostics"]}
        self.assertEqual(self.diag_text(j, by_line[2]), "ZZZ")
        self.assertEqual(by_line[2]["col"], 17)
        self.assertEqual(by_line[2]["endCol"], 20)
        self.assertEqual(self.diag_text(j, by_line[3]), "99999")
        self.assertEqual(self.diag_text(j, by_line[4]), '"x"')
        for d in j["diagnostics"]:
            self.assertEqual(sorted(d), ["col", "endCol", "file", "func", "line", "message",
                                         "severity"])
            self.assertEqual(d["severity"], "error")
            self.assertEqual(d["func"], "f")

    def test_json_columns_follow_the_indentation(self):
        j = self.check_json("DEF F\n      TR.ADD NOPE\n")
        d = j["diagnostics"][0]
        self.assertEqual((d["col"], d["endCol"]), (14, 18))

    def test_json_names_and_arguments(self):
        j = self.check_json("DEF F | W K\n  TR.ADD K\nDEF F2 | W K\n  TR.ADD W\n"
                            "DEF G\n  PL.SET 1 2\n  50% 3\n")
        texts = {d["line"]: self.diag_text(j, d) for d in j["diagnostics"]}
        self.assertEqual(texts[1], "W")                  # unused argument (the span is in the source)
        self.assertEqual(texts[3], "K")
        self.assertEqual(texts[6], "PL.SET")
        self.assertEqual(texts[7], "50%")
        dup = otype.check_json(self.check("DEF F\n  TR.ADD 1\nDEF F\n  TR.ADD 1\n"))
        self.assertEqual([self.diag_text({"_lines": ["DEF F", "", "DEF F"]}, d)
                          for d in dup["diagnostics"]], ["F", "F"])

    def test_json_default_span_is_the_line(self):
        j = self.check_json("DEF F\n  TR.ADD 1\nDEF G\n  TR.ADD 1\n    TR.ADD 2   # note\n")
        errs = [d for d in j["diagnostics"] if d["line"] == 5]
        self.assertTrue(errs)
        for d in errs:
            self.assertEqual(self.diag_text(j, d), "TR.ADD 2")   # first char .. end of code
            self.assertEqual(d["col"], 5)

    def test_json_lexer_errors_have_a_span(self):
        j = self.check_json('DEF F\n  TR.ADD 1\n  MSG "abc\n  TR.ADD 99999999\n')
        by_line = {d["line"]: d for d in j["diagnostics"]}
        self.assertEqual(self.diag_text(j, by_line[3]), '"abc')
        self.assertEqual(self.diag_text(j, by_line[4]), "99999999")

    def test_json_functions_and_consts(self):
        text = ('CONST N 4\nCONST NAME "x"\nCONST L {1 2}\nCONST C 1:4\n'
                'DEF F | W\n  MENU "DRUMS" >> "BOOM"\n  PARAM P 5 0 9\n  TR.CLR\n  TR.ADD W\n'
                'DEF V\n  RET 3\nDEF BAD\n  NOPE\n')
        prog = self.check(text)
        j = otype.check_json(prog)
        f = {x["name"]: x for x in j["functions"]}
        self.assertEqual(sorted(f), ["bad", "f", "v"])      # names are lower case
        self.assertEqual(f["f"]["args"], ["w"])
        self.assertEqual(f["f"]["menu"], ["DRUMS", "BOOM"])  # the menu strings keep their case
        self.assertEqual(f["f"]["params"], [{"name": "p", "default": 5, "min": 0, "max": 9}])
        self.assertTrue(f["f"]["destructive"])
        self.assertTrue(f["f"]["ok"])
        self.assertFalse(f["f"]["returns"])
        self.assertIsInstance(f["f"]["cost"], int)
        self.assertEqual(f["f"]["line"], 5)
        self.assertEqual(f["f"]["file"], "F1.OTY")
        self.assertTrue(f["v"]["returns"])
        self.assertIsNone(f["v"]["menu"])
        self.assertFalse(f["v"]["destructive"])
        self.assertFalse(f["bad"]["ok"])
        self.assertIsNone(f["bad"]["cost"])
        c = {x["name"]: x for x in j["consts"]}
        self.assertEqual(sorted(c), ["c", "l", "n", "name"])
        self.assertEqual(c["n"]["value"], 4)
        self.assertEqual(c["name"]["value"], "x")
        self.assertEqual(c["l"]["value"], [1, 2])
        self.assertEqual(c["c"]["value"], "1:4")
        self.assertEqual(c["n"]["line"], 1)
        import json
        json.dumps(j)

    def test_cli_json(self):
        import json
        good = self.tmp("A.OTY", "DEF A1\n  TR.ADD 1\n")
        bad = self.tmp("B.OTY", "DEF B1\n  TR.ADD NOPE\n")
        code, out, _err = self.run_tool(["check", "--json", good, bad])
        self.assertEqual(code, 1)
        j = json.loads(out)
        self.assertEqual(sorted(j), ["consts", "diagnostics", "functions"])
        self.assertEqual([d["file"] for d in j["diagnostics"]], [bad])
        code, out, _err = self.run_tool(["check", "--json", good])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["diagnostics"], [])

    def test_cli_stdin_file(self):
        import json
        disk = self.tmp("A.OTY", "DEF A1\n  NOPE 1\n")        # the disk copy is broken
        other = self.tmp("B.OTY", "DEF B1\n  A1\n")
        # the buffer fixes A.OTY and adds a function B.OTY calls
        code, out, _err = self.run_tool(["check", "--json", "--stdin-file", disk, other, disk],
                                        stdin=b"DEF A1\n  TR.ADD 1\n")
        j = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(j["diagnostics"], [])
        self.assertEqual(sorted(f["name"] for f in j["functions"]), ["a1", "b1"])
        # without the buffer the disk text is checked
        code, out, _err = self.run_tool(["check", "--json", disk, other])
        self.assertEqual(code, 1)
        # the stdin file need not be in the list; text mode works too
        code, out, _err = self.run_tool(["check", "--stdin-file", disk, other],
                                        stdin=b"DEF A1\n  TR.ADD 1\n")
        self.assertEqual(code, 0)
        self.assertIn("2 functions OK", out)
        # the buffer wins and its errors carry the path of the file
        code, out, _err = self.run_tool(["check", "--json", "--stdin-file", other, disk],
                                        stdin=b"DEF B1\n  TR.ADD ZZ\n")
        j = json.loads(out)
        self.assertTrue(any(d["file"] == other and d["line"] == 2 for d in j["diagnostics"]))

    def test_ops_json(self):
        import json
        from modules.otype import ops
        code, out, _err = self.run_tool(["ops", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        keys = {"name", "signature", "args", "argnames", "ret", "doc", "category", "alias",
                "settable", "stmt_only", "family", "destructive", "predicate"}
        for e in data:
            self.assertTrue(keys <= set(e), e["name"])
            self.assertTrue(e["doc"].strip(), e["name"])
        by = {}
        for e in data:
            self.assertEqual(e["name"], e["name"].lower(), e["name"])
            self.assertEqual(e["signature"], ops.show(e["signature"]), e["signature"])  # strings keep case
            for a in e["args"] + e["alias"]:
                self.assertEqual(a, a.lower(), a)
            by.setdefault(e["name"], []).append(e)
        for o in ops.ALL_OPS:
            self.assertIn(o.name.lower(), by)
        e = by["pl.set"][0]
        self.assertEqual(e["signature"], "pl.set s p v")
        self.assertEqual(e["args"], ["step", "param", "value"])
        self.assertEqual(e["argnames"], ["s", "p", "v"])
        self.assertFalse(e["ret"])
        self.assertEqual(by["add"][0]["alias"], ["+"])
        self.assertTrue(by["tr.clr"][0]["destructive"])
        self.assertTrue(by["flip"][0]["settable"])
        self.assertTrue(by["has.trig"][0]["predicate"])
        self.assertTrue(by["dir"][0]["stmt_only"])
        self.assertEqual(by["dir"][0]["args"], ["str", "[deep]"])
        self.assertEqual(by["rrand"][0]["category"], "LANG")
        self.assertEqual(by["rrand"][0]["minArgs"], 2)
        self.assertEqual(by["trk"][0]["maxArgs"], 8)
        self.assertIsNone(by["match"][0]["maxArgs"])
        kw = {n for n, es in by.items() if any(x["category"] == "KEYWORD" for x in es)}
        for n in ("if elif else l each prob every skip del break stop ret def const menu on "
                  "param confirm otype trk steps step seed").split():
            self.assertIn(n, kw)
        for n in "abcdxyztij":
            self.assertEqual([x["category"] for x in by[n]], ["VARIABLE"])
        for e in data:
            if e["category"] in ("KEYWORD", "VARIABLE"):
                for k in ("name", "signature"):
                    self.assertEqual(e[k], ops.show(e[k]), e[k])
        for e in data:
            if e["category"] in ("KEYWORD", "VARIABLE"):
                self.assertTrue(e["doc"].strip())

    def test_unused_argument_is_a_warning(self):
        prog = self.check("DEF F | W K\n  TR.ADD K\n\nDEF G\n  F | \"s\" 3\n  F | {1 2} 4\n"
                          "  F | 50% 5\n  F | 7 6\n")
        self.assertEqual(self.errs(prog), [])
        w = [d for d in self.warns(prog) if "unused" in d.msg]
        self.assertEqual([(d.func, d.line, d.msg) for d in w],
                         [("f", 1, "argument w is unused")])
        self.assertEqual(self.ok_names(prog), ["F", "G"])

    def test_used_argument_is_still_typed(self):
        prog = self.check("DEF F | W K\n  TR.ADD K\n  TR.ADD W\n\nDEF G\n  F | \"s\" 3\n")
        self.assertEqual([d for d in self.warns(prog) if "unused" in d.msg], [])
        self.assertTrue(self.errs(prog))

    def test_argument_only_passed_on_is_used(self):
        prog = self.check("DEF F | W\n  H | W\n\nDEF H | K\n  TR.ADD K\n")
        self.assertEqual([d for d in self.warns(prog) if "unused" in d.msg], [])

    def test_unused_argument_in_a_broken_function(self):
        # a line that does not parse may use the argument: no unused warning
        prog = self.check("DEF F | W\n  TR.ADD NOPE W\n")
        self.assertEqual([d for d in self.warns(prog) if "unused" in d.msg], [])



class TestTypedParamsAndWarnings(Base):
    def test_bool_param(self):
        self.good('DEF F\n  MENU "A" >> "B"\n  PARAM P BOOL 1\n  PARAM Q BOOL 0\n  PARAM R BOOL YES\n  PARAM S BOOL NO\n  TR.ADD P\n')
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM P BOOL 2\n  TR.ADD 1\n', 3, "bool default must be 0 or 1")
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM P BOOL FOO\n  TR.ADD 1\n', 3, "bool default must be 0, 1, yes or no")

    def test_option_param(self):
        self.good('DEF F\n  MENU "A" >> "B"\n  PARAM OPT OPTION 1 "1/4" "1/2" "1"\n  TR.ADD OPT\n')
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM OPT OPTION 3 "1/4" "1/2" "1"\n  TR.ADD 1\n', 3, "min <= default <= max")
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM OPT OPTION 0 "1/4"\n  TR.ADD 1\n', 3, "at least 2 choices")
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM OPT OPTION 0 "1234567890123" "OK"\n  TR.ADD 1\n', 3, "longer than 12 characters")
        opts = " ".join('"%d"' % i for i in range(17))
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM OPT OPTION 0 %s\n  TR.ADD 1\n' % opts, 3, "more than 16 choices")

    def test_folder_param(self):
        self.good('DEF F\n  MENU "A" >> "B"\n  PARAM SRC FOLDER "AUDIO/DRUMS"\n  DIR SRC DEEP\n')
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM SRC FOLDER "%s"\n  DIR SRC\n' % ("X" * 65), 3, "longer than 64 characters")
        srcs = "".join('  PARAM S%d FOLDER "A"\n' % i for i in range(5))
        self.bad('DEF BAD\n  MENU "A" >> "B"\n%s  DIR S0\n' % srcs, 7, "more than 4 folder params")

    def test_folder_param_type_checking(self):
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM SRC FOLDER "A"\n  A SRC\n', 4, "folder param src cannot be used in arithmetic")
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM SRC FOLDER "A"\n  TR.ADD SRC\n', 4, "folder param src cannot be used in arithmetic")
        self.bad('DEF BAD\n  MENU "A" >> "B"\n  PARAM SRC FOLDER "A"\n  L 1 SRC:\n    TR.ADD 1\n', 4, "folder param src cannot be used as loop bound")
        self.bad('DEF BAD\n  PAGE "PG"\n  PARAM SRC FOLDER "A"\n  TR.ADD 1\n', 2, "page function cannot declare a folder param")

    def test_warnings_menu_and_params(self):
        prog = self.check('DEF F\n  MENU "TENCHARS10" >> "FOURTEENCHARS_"\n  CONFIRM "12345678901234567890123456789"\n  PARAM TOOLONGLABEL1 40 0 100\n  TR.ADD 1\n')
        self.assertEqual(len(self.errs(prog)), 0)
        diags = [d.msg for d in prog.diags]
        self.assertTrue(any('menu category "TENCHARS10" longer than 9 chars (clipped on screen)' in m for m in diags))
        self.assertTrue(any('script name "FOURTEENCHARS_" longer than 13 chars (clipped on screen)' in m for m in diags))
        self.assertTrue(any('confirm text "12345678901234567890123456789" longer than 28 chars (clipped in dialog)' in m for m in diags))
        self.assertTrue(any('param "TOOLONGLABEL1" label longer than 11 chars (clipped on screen)' in m for m in diags))

    def test_warning_more_than_32_categories(self):
        fns = "".join('DEF F%d\n  MENU "CAT%d" >> "ACT"\n  TR.ADD 1\n' % (i, i) for i in range(34))
        prog = self.check(fns)
        self.assertEqual(len(self.errs(prog)), 0)
        self.assertTrue(any('more than 32 menu categories (excess categories ignored)' in d.msg for d in prog.diags))

    def test_warning_more_than_64_scripts_in_category(self):
        fns = "".join('DEF F%d\n  MENU "MYCAT" >> "ACT%d"\n  TR.ADD 1\n' % (i, i) for i in range(66))
        prog = self.check(fns)
        self.assertEqual(len(self.errs(prog)), 0)
        self.assertTrue(any('category "MYCAT" has more than 64 scripts (script not listed)' in d.msg for d in prog.diags))


class TestDocsAndEditor(unittest.TestCase):
    """The generated cheat sheet and the VS Code extension follow the language (04/10)."""

    def test_cheatsheet_places_every_keyword_and_operation(self):
        import cheatsheet                          # tools/cheatsheet.py: refuses to build when an entry has no section
        page = cheatsheet.build(False)
        self.assertIn("<b>page</b>", page)
        self.assertEqual(set(cheatsheet.CATEGORY_SUMMARIES), set(otype.O.CATEGORIES))
        self.assertEqual(page.count('<th scope="row">'), len(otype.O.CATEGORIES))
        self.assertIn("<b>lfo.wave</b>", page)
        self.assertIn(" n / lfo.wave n x</code>", page)
        self.assertNotIn("nothing runs on the unit yet", page)
        for o in otype.ops_json():
            self.assertIn("<b>%s</b>" % o["name"].lower(), page.replace("&gt;", ">").replace("&lt;", "<"),
                          o["name"])

    def test_the_extension_bundles_the_current_checker(self):
        here = os.path.join(ROOT, "editors", "vscode", "checker")
        pairs = [("tools/otype.py", "tools/otype.py"), ("modules/otype/ops.py", "modules/otype/ops.py"),
                 ("modules/otype/menu_limits.py", "modules/otype/menu_limits.py"),
                 ("modules/otype/__init__.py", "modules/otype/__init__.py")]
        for a, b in pairs:
            with open(os.path.join(ROOT, a), "rb") as f1, open(os.path.join(here, b), "rb") as f2:
                self.assertEqual(f1.read(), f2.read(), "%s is stale: bash editors/vscode/sync_checker.sh" % b)

    def test_the_bundled_checker_runs_outside_the_repository(self):
        import subprocess
        here = os.path.join(ROOT, "editors", "vscode", "checker")
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "CHECK.OTY")
            with open(source, "w") as out:
                out.write("def check\n  lfo.wave 1 3\n")
            result = subprocess.run([sys.executable, os.path.join(here, "tools", "otype.py"), "check", source],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_grammar_colours_every_declaration(self):
        import json
        import re
        with open(os.path.join(ROOT, "editors", "vscode", "syntaxes", "otype.tmLanguage.json")) as fh:
            g = json.load(fh)
        rx = re.compile(g["repository"]["declaration"]["match"])
        for w in sorted(otype.DECLS | {"DEF", "CONST", "OTYPE"}):
            self.assertTrue(rx.search(" %s " % w.lower()), w)

class TestRenamedOpsRejection(Base):
    """Old names are strictly rejected without aliases, suggesting the new name."""

    def test_rejection_of_renamed_operators(self):
        cases = [
            ("DEF BAD\n  RATCH 1 4\n", "ratch is now ratch.set"),
            ("DEF BAD\n  FX.SET 1 2\n", "fx.set is now fx.type"),
            ("DEF BAD\n  A FILE.N\n", "file.n is now file.len"),
            ("DEF BAD\n  SLOTS.NONE\n", "slots.none is now slots.clr"),
            ("DEF BAD\n  SLOT.LOAD 1 1\n", "slot.load is now static.load"),
            ("DEF BAD\n  A SLOT.FREE 1\n", "slot.free is now static.free"),
            ("DEF BAD\n  A SLOT.FIRSTFREE\n", "slot.firstfree is now static.firstfree"),
            ("DEF BAD\n  SLOT.CLR 1\n", "slot.clr is now static.clr"),
        ]
        for src, expected_msg in cases:
            with self.subTest(src=src):
                self.bad(src, 2, expected_msg)

    def test_rejection_of_removed_operators(self):
        for old, recipe in (("POOL.FILL", "Fill the Static slots"), ("POOL.RND", "Fill the Static slots at random"),
                            ("POOL.CLR", "Clear all Static slots"), ("POOL.SHUFFLE", "Shuffle the Static slots"),
                            ("SCN.GEN", "Random scenes"), ("LFO.RND", "Random LFOs"), ("FX.RND", "Random effects"),
                            ("LFOD.RND", "Random LFO designer"), ("LFOD.INV", "Invert the LFO designer")):
            with self.subTest(op=old):
                self.bad("DEF BAD\n  %s\n" % old, 2, '%s was removed: write the loop (cookbook "%s")' % (old.lower(), recipe))

    def test_lfo_spd_and_dep_require_argument(self):
        # Old lfo.spd and lfo.dep took 0 args; now they take 1 argument (1..3)
        self.bad("DEF BAD\n  A LFO.SPD\n", 2, "lfo.spd expects 1 argument")
        self.bad("DEF BAD\n  A LFO.DEP\n", 2, "lfo.dep expects 1 argument")


if __name__ == "__main__":
    unittest.main()
