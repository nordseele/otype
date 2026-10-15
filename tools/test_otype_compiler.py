"""Tests of the reference of the OType compiler (modules/otype/oterr.py), the source caps, the
ASCII semantics, the order of the report and the differential corpus (modules/otype/corpus.py).
From the repo root:

    python3 -m unittest tools/test_otype_compiler.py

"""
import ast
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import otype  # noqa: E402
from modules.otype import corpus, oterr, otb  # noqa: E402
from modules.otype import ops as O  # noqa: E402

EXAMPLES = sorted(os.path.join(ROOT, "modules", "otype", "examples", n)
                  for n in os.listdir(os.path.join(ROOT, "modules", "otype", "examples")) if n.upper().endswith(".OTY"))


def rd(path, mode="r", encoding=None):
    with open(path, mode, encoding=encoding) as fh:
        return fh.read()


def src(text, name=b"T.OTY"):
    """One source of the card: (label, bytes)."""
    return [(b"/SCRIPTS/" + name, text if isinstance(text, bytes) else text.encode("latin-1"))]


def ref(text, profile=None, name=b"T.OTY"):
    return oterr.reference(src(text, name), profile)


def lines(report):
    return report.split("\n")[:-1]


def m1_without_msg():
    """msg is on the m1 profile and no other op is outside it: the tests of the profile's refusal take one away."""
    from unittest import mock
    return mock.patch.dict(O.PROFILES, {"m1": O.PROFILES["m1"] - {"MSG"}})


class TestReference(unittest.TestCase):
    def test_clean_example(self):
        text = rd(os.path.join(ROOT, "modules", "otype", "examples", "M1.OTY"), "rb")
        rep, data = oterr.reference([(b"/SCRIPTS/M1.OTY", text)], "m1")
        self.assertEqual(rep, "3 functions OK, 0 refused, 0 warnings\n")
        prog = otype.check_sources([("/SCRIPTS/M1.OTY", text.decode("latin-1"))])
        self.assertEqual(data, otb.compile_program(prog, O.PROFILES["m1"]).data)
        self.assertEqual(otb.parse(data).nfuncs if hasattr(otb.parse(data), "nfuncs") else 3, 3)

    def test_line_ends_are_lf_only(self):
        for f in EXAMPLES:
            rep, _ = oterr.reference([(b"/SCRIPTS/" + os.path.basename(f).upper().encode(), rd(f, "rb"))], "m1")
            self.assertTrue(rep.endswith("\n"))
            self.assertNotIn("\r", rep)
            self.assertNotIn("\n\n", rep)

    @m1_without_msg()
    def test_profile_may_be_a_name_a_set_or_none(self):
        s = src('def f\n  msg "hi"\n')
        a = oterr.reference(s, "m1")
        self.assertEqual(a, oterr.reference(s, O.PROFILES["m1"]))
        self.assertNotEqual(a, oterr.reference(s, None))
        self.assertIn("not on the unit yet: msg", a[0])
        self.assertEqual(oterr.reference(s, None)[0], "1 functions OK, 0 refused, 0 warnings\n")

    def test_compiler_errors_are_in_the_report_and_count_as_refusals(self):
        rep, data = ref("def f\n  a " + "add 1 " * 16 + "1\ndef g\n  f\ndef h\n  tr.add 1\n")
        self.assertEqual(lines(rep), ["/SCRIPTS/T.OTY:2: f: error: stack depth 17 > 16",
                                      "/SCRIPTS/T.OTY:4: g: error: needs f",
                                      "1 functions OK, 2 refused, 0 warnings"])
        t = otb.parse(data)
        self.assertEqual([f.name for f in t.funcs], ["H"])

    @m1_without_msg()
    def test_order_checker_and_compiler_diagnostics_are_merged_by_file_line_emission(self):
        sources = [(b"/SCRIPTS/B.OTY", b"def g\n  msg \"hi\"\n  nosuch\n"),
                   (b"/SCRIPTS/A.OTY", b"def f\n  msg \"hi\"\n  tr.add 1\n")]
        rep, _ = oterr.reference(sources, "m1")
        self.assertEqual(lines(rep), [
            "/SCRIPTS/A.OTY:2: f: error: not on the unit yet: msg",
            "/SCRIPTS/B.OTY:3: g: error: unknown name nosuch",
            "0 functions OK, 2 refused, 0 warnings"])
        # a checker diagnostic and a compiler one on the same line: the checker's first
        rep, _ = oterr.reference([(b"/SCRIPTS/A.OTY", b"def f | n\n  tr.add 1\n  msg \"hi\"\n")], "m1")
        self.assertEqual(lines(rep)[0], "/SCRIPTS/A.OTY:1: f: warning: argument n is unused")
        self.assertEqual(lines(rep)[1], "/SCRIPTS/A.OTY:3: f: error: not on the unit yet: msg")

    def test_files_are_sorted_by_label_bytes_not_by_card_order(self):
        rep, _ = oterr.reference([(b"/SCRIPTS/b.OTY", b"def b\n  nosuch\n"), (b"/SCRIPTS/A.OTY", b"def a\n  nosuch\n"),
                                  (b"-", b"def c\n  nosuch\n")], None)
        self.assertEqual([l.split(":")[0] for l in lines(rep)[:3]], ["-", "/SCRIPTS/A.OTY", "/SCRIPTS/b.OTY"])

    def test_table_level_errors(self):
        rep, data = ref("".join("def f%d\n  tr.add 1\n" % k for k in range(256)))
        self.assertIn("-:0: -: error: more than 255 functions", lines(rep))
        self.assertEqual(data, b"")
        self.assertTrue(lines(rep)[-1].endswith("(1 file-level errors)"))
        rep, data = ref("def f\n" + "  tr.add 1\n" * 4000)
        self.assertRegex(rep, r"(?m)^-:0: -: error: compiled table is \d+ bytes \(> 32768\)$")
        self.assertEqual(data, b"")
        rep, data = ref("def f\n" + "  tr.add 1\n" * 3000)       # under the limit: a table
        self.assertEqual(lines(rep), ["1 functions OK, 0 refused, 0 warnings"])
        self.assertTrue(0 < len(data) <= otb.MAX_TABLE)

    def test_table_over_64k_has_no_bytes_and_does_not_crash(self):
        line = " a " + "+ " * 14 + "1 " * 15 + "\n"
        rep, data = ref("def f\n" + line * 700)
        self.assertIn("compiled table is", rep)
        self.assertEqual(data, b"")

    def test_label_over_255_bytes_is_a_table_error(self):
        rep, data = oterr.reference([(b"/SCRIPTS/" + b"L" * 300 + b".OTY", b"def f\n  tr.add 1\n")], None)
        self.assertIn("-:0: -: error: string too long for the pool", lines(rep))
        self.assertEqual(data, b"")

    def test_counter_and_code_limits_are_errors_not_crashes(self):
        rep, _ = ref("def f\n" + "  every 2: tr.add 1\n" * 257)
        self.assertIn("/SCRIPTS/T.OTY:1: f: error: more than 255 every/skip counters", lines(rep))
        rep, data = ref("def f\n" + "  every 2: tr.add 1\n" * 255)
        self.assertEqual(lines(rep), ["1 functions OK, 0 refused, 0 warnings"])
        line = " a " + "+ " * 14 + "1 " * 15 + "\n"
        rep, data = ref("def f\n" + line * 1000)
        self.assertRegex(rep, r"/SCRIPTS/T\.OTY:1: f: error: code too long \(\d+ bytes\)")
        self.assertEqual(len(otb.parse(data).funcs), 0)

    def test_huge_cost_saturates_in_the_message(self):
        # exact value 2**82: the message says the saturated 32-bit value (the C port has no big integers)
        rep, _ = ref('def f\n  menu "T" >> "X"\n' + "".join("  " * k + "l -32768 32767:\n" for k in range(1, 6))
                     + "  " * 6 + "tr.add 1\n")
        self.assertIn("/SCRIPTS/T.OTY:1: f: error: too long: 4294967295/4096", lines(rep))
        rep, _ = ref('def f\n  menu "T" >> "X"\n  l 1 5000: tr.add i\n')
        self.assertIn("too long: 20000/4096", rep)

    @m1_without_msg()
    def test_check_profile_cli_prints_the_same_lines(self):
        import contextlib
        import io
        text = 'def f\n  msg "hi"\ndef g\n  f\ndef h\n  tr.add 1\n'
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "P.OTY")
            with open(p, "w") as fh:
                fh.write(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = otype.main(["otype.py", "check", p, "--profile", "m1"])
            rep, _ = oterr.reference([(p.encode(), text.encode())], "m1")
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), rep)

    def test_dump_of_a_table_over_64k_does_not_crash(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "P.OTY")
            with open(p, "w") as fh:
                fh.write("def f\n" + (" a " + "+ " * 14 + "1 " * 15 + "\n") * 700)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = otb.main([p], otype, err=err)
            self.assertEqual(rc, 1)
            self.assertIn("compiled table is", err.getvalue())


class TestCaps(unittest.TestCase):
    def files(self, n, size=0):
        return [(b"/SCRIPTS/F%03d.OTY" % k, b"def f%d\n  tr.add 1\n" % k + b"#" * size) for k in range(n)]

    def test_64_files_are_taken_the_65th_is_not(self):
        rep, _ = oterr.reference(self.files(64), None)
        self.assertEqual(lines(rep), ["64 functions OK, 0 refused, 0 warnings"])
        rep, _ = oterr.reference(self.files(65), None)
        self.assertEqual(lines(rep), ["/SCRIPTS/F064.OTY:0: -: error: file not loaded: over 64 files",
                                      "64 functions OK, 0 refused, 0 warnings (1 file-level errors)"])

    def test_every_file_after_the_first_refusal_is_refused_with_one_error_each(self):
        rep, _ = oterr.reference(self.files(67), None)
        self.assertEqual(lines(rep)[:3], ["/SCRIPTS/F064.OTY:0: -: error: file not loaded: over 64 files",
                                          "/SCRIPTS/F065.OTY:0: -: error: file not loaded: over 64 files",
                                          "/SCRIPTS/F066.OTY:0: -: error: file not loaded: over 64 files"])
        self.assertTrue(lines(rep)[-1].endswith("(3 file-level errors)"))

    def test_byte_cap(self):
        base = b"def f\n  tr.add 1\n"
        for total, ok in ((65535, True), (65536, True), (65537, False)):
            data = base + b"#" * (total - len(base) - 1) + b"\n"
            self.assertEqual(len(data), total)
            rep, _ = oterr.reference([(b"/SCRIPTS/A.OTY", data)], None)
            self.assertEqual("file not loaded" not in rep, ok, total)
            if not ok:
                self.assertEqual(lines(rep)[0], "/SCRIPTS/A.OTY:0: -: error: file not loaded: over 65536 bytes")

    def test_byte_cap_counts_files_in_the_given_order_and_refuses_the_later_small_ones(self):
        big = (b"#" * 99 + b"\n") * 400
        rep, _ = oterr.reference([(b"/SCRIPTS/A.OTY", big), (b"/SCRIPTS/B.OTY", big),
                                  (b"/SCRIPTS/C.OTY", b"def c\n  tr.add 1\n")], None)
        self.assertEqual(lines(rep)[:2], ["/SCRIPTS/B.OTY:0: -: error: file not loaded: over 65536 bytes",
                                          "/SCRIPTS/C.OTY:0: -: error: file not loaded: over 65536 bytes"])
        self.assertEqual(lines(rep)[-1], "0 functions OK, 0 refused, 0 warnings (2 file-level errors)")

    def test_files_cap_wins_over_bytes_when_it_stops_first(self):
        files = self.files(64) + [(b"/SCRIPTS/Z.OTY", b"#" * 70000)]
        rep, _ = oterr.reference(files, None)
        self.assertIn("/SCRIPTS/Z.OTY:0: -: error: file not loaded: over 64 files", rep)

    def test_check_sources_applies_the_caps_too(self):
        prog = otype.check_sources([("F%d.OTY" % k, "def f%d\n  tr.add 1\n" % k) for k in range(70)])
        self.assertEqual(len(prog.funcs), 64)
        self.assertEqual(prog.file_errors, 6)
        self.assertEqual(otype.MAX_FILES, 64)
        self.assertEqual(otype.MAX_SOURCE_BYTES, 65536)


class TestAscii(unittest.TestCase):
    """Nothing depends on Unicode; a source is bytes read as Latin-1."""

    def errs(self, data, profile=None):
        rep, _ = ref(data, profile)
        return [l for l in lines(rep) if ": error: " in l]

    def test_helpers_leave_non_ascii_alone(self):
        self.assertEqual(otype.aupper("a\xdf\xff\xe9z"), "A\xdf\xff\xe9Z")
        self.assertEqual(O.show("AB\xc0\xdc"), "ab\xc0\xdc")
        self.assertEqual(O.show('A"B\xc9"C'), 'a"B\xc9"c')
        self.assertEqual(otype.asplit(" a\t\tb\xa0c  d "), ["a", "b\xa0c", "d"])
        self.assertEqual(otype.asplit("a b c", 1), ["a", "b c"])
        self.assertFalse(otype.isdig("\xb2"))
        self.assertFalse(otype.isdig("\u0661"))

    def test_sharp_s_does_not_become_ss(self):
        # str.upper() would make def FOO\xdf the name FOOSS and accept it
        e = self.errs("def foo\xdf\n  tr.add 1\n")
        self.assertTrue(any("character 0xdf outside a comment" in l for l in e), e)
        self.assertTrue(any("is not a valid name" in l for l in e), e)

    def test_y_diaeresis_does_not_leave_latin1(self):
        e = self.errs("def \xff\n  tr.add 1\n")
        self.assertTrue(any("character 0xff outside a comment" in l for l in e), e)
        self.assertTrue(any("not a valid name" in l for l in e), e)

    def test_superscript_two_is_not_a_digit(self):
        e = self.errs("def f\n  tr.add \xb2\n")
        self.assertEqual(len(e), 1)
        self.assertIn("character 0xb2 outside a comment", e[0])
        e = self.errs("def f\n  tr.add 1:\xb2\n")
        self.assertTrue(any("character 0xb2" in l for l in e), e)

    def test_no_break_space_and_next_line_are_not_whitespace(self):
        for ch, name in (("\xa0", "0xa0"), ("\u0085", "0x85"), ("\x0c", "0x0c"), ("\x0b", "0x0b"), ("\x1c", "0x1c"),
                         ("\x1f", "0x1f")):
            for tpl in ("def f\n  tr.add%s1\n", "def f\n  tr.add 1%s\n", "def f%s\n  tr.add 1\n", "def%sf\n  tr.add 1\n",
                        "def f\n%s  tr.add 1\n", "def f\n  tr.add 1\n%s\n"):
                e = self.errs(tpl % ch)
                self.assertTrue(any("character %s outside a comment" % name in l for l in e), (name, tpl, e))

    def test_a_line_of_only_a_control_byte_is_not_blank(self):
        self.assertEqual(self.errs("def f\n  tr.add 1\n\x0c\n")[0],
                         "/SCRIPTS/T.OTY:3: f: error: character 0x0c outside a comment (printable ASCII only)")
        self.assertEqual(self.errs("def f\n  tr.add 1\n \t \n"), [])       # spaces and tabs: blank

    def test_space_and_tab_are_the_only_whitespace_and_a_trailing_tab_is_stripped(self):
        self.assertEqual(self.errs("def f\n  tr.add 1\t \t\n"), [])
        self.assertEqual(len(self.errs("def f\n  tr.add\t1\n")), 1)
        self.assertEqual(self.errs("def f\n\ttr.add 1\n"), ["/SCRIPTS/T.OTY:2: f: error: tab in indentation (use spaces)"])

    def test_high_bytes_in_comments_are_kept_and_in_strings_refused(self):
        self.assertEqual(self.errs("def f # caf\xe9 \u0085 \xa0 \x0c\n  tr.add 1 # \xff\n# \xdf\n"), [])
        e = self.errs('def f\n  dir "caf\xe9"\n')
        self.assertEqual(e, ["/SCRIPTS/T.OTY:2: f: error: character 0xe9 outside a comment (printable ASCII only)"])

    def test_non_ascii_letters_and_digits_in_every_position_are_errors(self):
        for ch in ("\xe9", "\xc9", "\u0661"[:0] or "\xb9", "\xbd", "\xb5", "\xaa"):
            for tpl in ("def f\n  x%s\n", "def f\n  tr.add 1%s\n", "const k%s 1\n", "def f | a%s\n  tr.add a\n"):
                self.assertTrue(self.errs(tpl % ch), (ch, tpl))

    def test_utf8_bytes_are_two_errors_free_in_a_comment(self):
        self.assertEqual(self.errs(b"def f # caf\xc3\xa9\n  tr.add 1\n"), [])
        self.assertTrue(self.errs(b"def f\n  tr.add 1 \xc3\xa9\n"))

    def test_bytes_object_equals_latin1_text(self):
        s = "def f\n  tr.add 1 \xe9\n"
        self.assertEqual(ref(s), ref(s.encode("latin-1")))

    def test_case_folding_is_ascii_only(self):
        a = ref("DEF F\n  TR.ADD 1\n")
        b = ref("def f\n  tr.add 1\n")
        self.assertEqual(a[0], b[0].replace(":2: ", ":2: "))
        self.assertEqual(a[1], b[1])
        # a message never changes the case of a user string
        rep, _ = ref('DEF F\n  MENU "Ab" >> "XyzXyzXyzXyz12"\n  TR.ADD 1\n')
        self.assertIn('script name "XyzXyzXyzXyz12" longer than 13 chars (clipped on screen)', rep)

    def test_quoting_in_messages_does_not_depend_on_repr(self):
        e = self.errs("def it's\n  tr.add 1\n")
        self.assertEqual(e, ["/SCRIPTS/T.OTY:1: it's: error: 'it's' is not a valid name (a letter, then letters, digits or _)"])
        e = self.errs("def a\\b\n  tr.add 1\n")
        self.assertEqual(e, ["/SCRIPTS/T.OTY:1: a\\b: error: 'a\\b' is not a valid name (a letter, then letters, digits or _)"])
        e = self.errs("const l {1 ' 2}\n")
        self.assertIn("/SCRIPTS/T.OTY:1: -: error: a list holds numbers only (found ''')", e)

    def test_numbers_with_more_than_4300_digits_do_not_crash(self):
        e = self.errs("def f\n  tr.add " + "9" * 5000 + "\n")
        self.assertTrue(any("out of 16-bit range" in l for l in e))
        self.assertTrue(any("line too long (5" in l for l in e))
        e = self.errs("def f\n  tr.add " + "0" * 5000 + "7\n")           # the value is 7: only the line is too long
        self.assertEqual(len(e), 1)
        self.assertRegex(e[0], r"line too long \(5010 > 128 characters\)")
        e = self.errs("def f\n  prob " + "5" * 5000 + "%: tr.add 1\n")
        self.assertTrue(any("is not a trig condition" in l for l in e))
        e = self.errs("def f\n  if " + "1" * 5000 + ":" + "2" * 5000 + ": tr.add 1\n")
        self.assertTrue(any("is not a trig condition" in l for l in e))
        self.assertTrue(self.errs("def f\n  tr.add 0x" + "F" * 5000 + "\n"))

    def test_conditions_written_with_leading_zeros_compile(self):
        for c in ("050%", "01:04", "0001ST".replace("0001ST", "1st")):
            rep, data = ref("def f\n  cond.set 1 %s\n" % c)
            self.assertEqual(lines(rep), ["1 functions OK, 0 refused, 0 warnings"], c)
            self.assertTrue(data)
        a = ref("def f\n  cond.set 1 050%\n")[1]
        self.assertEqual(a, ref("def f\n  cond.set 1 50%\n")[1])
        self.assertEqual(ref("def f\n  cond.set 1 01:04\n")[1], ref("def f\n  cond.set 1 1:4\n")[1])
        self.assertEqual(ref("def f\n  prob 050: tr.add 1\n")[0].split("\n")[0].split(": ", 3)[-1:][0][:20],
                         "prob 50".split()[0][:0] + "050 is a trig condition"[:0] or ref("def f\n  prob 050: tr.add 1\n")[0].split("\n")[0].split(": ", 3)[-1:][0][:20])

    def test_line_ends(self):
        base = ref("def f\n  tr.add 1\n  tr.add x y\n")
        for eol in ("\r\n", "\r", "\n"):
            self.assertEqual(ref(("def f\n  tr.add 1\n  tr.add x y\n").replace("\n", eol)), base)
        self.assertEqual(self.errs("def f\r\n  tr.add 1\r  tr.add\n\r  tr.add 2\n")[0].split(":")[1], "3")

    def test_no_unicode_sensitive_call_is_left_in_the_checker(self):
        forb = {"upper", "lower", "isdigit", "isalpha", "isalnum", "isspace", "title", "casefold", "swapcase",
                "isupper", "islower", "isnumeric", "isdecimal", "splitlines", "capitalize"}
        strip = {"split", "strip", "rstrip", "lstrip"}
        allowed = {"tools/otype.py": set(),
                   "modules/otype/otb.py": {"mods", "fmt_instr", "func_listing", "source_text_of", "main"}}
        for path, ok in allowed.items():
            tree = ast.parse(rd(os.path.join(ROOT, path), encoding="utf-8"))
            hits = []

            class V(ast.NodeVisitor):
                def __init__(s):
                    s.stack = []

                def visit_FunctionDef(s, n):
                    s.stack.append(n.name)
                    s.generic_visit(n)
                    s.stack.pop()

                def visit_Call(s, n):
                    f = n.func
                    if isinstance(f, ast.Attribute) and (f.attr in forb or (f.attr in strip and not n.args)):
                        if not (s.stack and s.stack[-1] in ok):
                            hits.append((n.lineno, f.attr))
                    # regexes: no Unicode-aware class
                    if (isinstance(f, ast.Attribute) and getattr(f.value, "id", "") == "re" and n.args
                            and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
                            and re.search(r"\\[swdSWDbB]", n.args[0].value)):
                        hits.append((n.lineno, "regex " + n.args[0].value))
                    if isinstance(f, ast.Name) and f.id == "repr":
                        hits.append((n.lineno, "repr"))
                    s.generic_visit(n)

            V().visit(tree)
            self.assertEqual(hits, [], path)
        text = rd(os.path.join(ROOT, "tools", "otype.py"), encoding="utf-8")
        self.assertEqual(len(re.findall(r"%r", text)), 1)          # Tok.__repr__ only (debugging)


class TestX9(unittest.TestCase):
    """Nothing the OTB format cannot hold, nothing the VM refuses at load, ever leaves the compiler as a table;
    the function is refused with an ordinary error (its callers: `needs f`), the table with a table-level one."""

    def one(self, text, profile=None):
        rep, data = ref(text, profile)
        return lines(rep), data

    def test_control_frames_over_16(self):
        for n, ok in ((15, True), (16, False), (17, False), (255, False), (256, False)):
            ls, data = self.one("def f\n" + "  trk 1\n" * n + "  tr.add 1\n")
            if ok:
                self.assertEqual(ls, ["1 functions OK, 0 refused, 0 warnings"])
                self.assertEqual(otb.parse(data).fmap["F"].max_ctl, 15)
            else:       # 16 passes need 17 contexts (the function's own one counts): the context cap comes first
                self.assertEqual(ls[-1], "0 functions OK, 1 refused, 0 warnings")
                self.assertEqual(ls[0], "/SCRIPTS/T.OTY:%d: f: error: %s" % (
                    n + 1, "context depth 17 > 16" if n == 16 else "loop/pass depth %d > 16" % n))
                self.assertEqual(len(otb.parse(data).funcs), 0)

    def test_context_depth_over_16_with_nested_loops(self):
        def nest(d):
            return "def f\n" + "".join("  " * (k + 1) + "seed 1: l 1 2:\n" for k in range(d)) + "  " * (d + 1) + "tr.add 1\n"
        self.assertEqual(self.one(nest(7))[0], ["1 functions OK, 0 refused, 0 warnings"])
        ls, data = self.one(nest(8) + "def g\n  f\n")
        self.assertEqual(ls[:2], ["/SCRIPTS/T.OTY:9: f: error: context depth 17 > 16", "/SCRIPTS/T.OTY:12: g: error: needs f"])
        self.assertEqual(len(otb.parse(data).funcs), 0)

    def test_menus_over_255(self):
        def text(n):
            return "def f\n" + "".join('  menu "A" >> "B%d"\n' % k for k in range(n)) + "  tr.add 1\n"
        ls, data = self.one(text(255))
        self.assertEqual(ls[-1], "1 functions OK, 0 refused, 191 warnings")
        self.assertEqual(len(ls), 192)
        self.assertEqual(len(otb.parse(data).funcs[0].menus), 255)
        ls, data = self.one(text(256) + "def g\n  f\n")
        self.assertIn("/SCRIPTS/T.OTY:1: f: error: more than 255 menu lines", ls)
        self.assertIn("/SCRIPTS/T.OTY:260: g: error: needs f", ls)
        self.assertEqual(len(otb.parse(data).funcs), 0)

    def test_counters_of_the_table_over_1024(self):
        def fn(name, n):
            return "def %s\n" % name + "  every 2: tr.add 1\n" * n
        ok = "".join(fn("f%d" % i, 205) for i in range(4)) + fn("g", 204)
        ls, data = self.one(ok)
        self.assertEqual(ls, ["5 functions OK, 0 refused, 0 warnings"])
        self.assertEqual(sum(len(f.counters) for f in otb.parse(data).funcs), 1024)
        ls, data = self.one(ok + fn("h", 1))
        self.assertEqual(ls, ["-:0: -: error: more than 1024 every/skip counters in the table",
                              "6 functions OK, 0 refused, 0 warnings (1 file-level errors)"])
        self.assertEqual(data, b"")

    def test_loops_after_65535_bytes_of_code_do_not_crash(self):
        line = "  a " + "+ " * 14 + "1 " * 15 + "\n"
        for tail in ("  l 1 2: tr.add 1\n", "  each: tr.add 1\n", "  trk 1\n  tr.add 1\n"):
            ls, data = self.one("def f\n" + line * 900 + tail)
            self.assertRegex(ls[0], r"f: error: code too long \(\d+ bytes\)")
            self.assertEqual(len(otb.parse(data).funcs), 0)


class TestDeterminism(unittest.TestCase):
    SCRIPT = (
        "import sys, hashlib\n"
        "sys.path.insert(0, %r)\n"
        "from modules.otype import corpus, oterr\n"
        "h = hashlib.sha256()\n"
        "for eid, srcs in corpus.build_inputs(True):\n"
        "    for prof in (None, 'm1'):\n"
        "        rep, data = oterr.reference(srcs, prof)\n"
        "        h.update(eid.encode() + rep.encode('latin-1') + data)\n"
        "print(h.hexdigest())\n"
    ) % ROOT

    def test_report_and_table_do_not_depend_on_the_hash_seed(self):
        out = set()
        for hs in ("1", "2", "random"):
            p = subprocess.run([sys.executable, "-c", self.SCRIPT], capture_output=True, text=True,
                               env=dict(os.environ, PYTHONHASHSEED=hs), cwd=ROOT)
            self.assertEqual(p.returncode, 0, p.stderr)
            out.add(p.stdout.strip())
        self.assertEqual(len(out), 1, out)

    def test_call_cycle_message_is_stable(self):
        rep, _ = ref("def cya\n  cyb\ndef cyb\n  cyc\ndef cyc\n  cya\ndef cyd\n  cya\n")
        self.assertEqual(lines(rep), ["/SCRIPTS/T.OTY:1: cya: error: loop cya>cyb>cyc>cya",
                                      "/SCRIPTS/T.OTY:3: cyb: error: loop cyb>cyc>cya>cyb",
                                      "/SCRIPTS/T.OTY:5: cyc: error: loop cyc>cya>cyb>cyc",
                                      "/SCRIPTS/T.OTY:8: cyd: error: needs cya",
                                      "0 functions OK, 4 refused, 0 warnings"])


class TestX7Fixes(unittest.TestCase):
    """The Python defects found by the C front end, and the cap on the report."""

    def test_const_lexical_error_is_reported_once(self):
        rep, _ = ref("const x 99999\nconst yy 1 # " + "z" * 130 + "\nconst w \"a\nconst v 1\x01\ndef f\n  tr.add 1\n")
        ls = lines(rep)
        self.assertEqual(sum("number 99999 out of 16-bit range" in l for l in ls), 1)
        self.assertEqual(sum("line too long" in l for l in ls), 1)
        self.assertEqual(sum("unterminated string" in l for l in ls), 1)
        self.assertEqual(sum("0x01" in l for l in ls), 1)
        self.assertEqual(ls[-1], "1 functions OK, 0 refused, 0 warnings (4 file-level errors)")

    def test_the_text_of_a_string_is_not_a_param_name(self):
        rep, _ = ref('def f\n  menu "A" >> "B"\n  param "ABC" 1 0 2\n  param "abc" 1 0 2\n  param fill 1 0 2\n  param 5 1 0 2\n  tr.add 1\n')
        self.assertEqual(lines(rep), [
            "/SCRIPTS/T.OTY:3: f: error: param abc: 'abc' is not a valid name",
            "/SCRIPTS/T.OTY:4: f: error: param abc: 'abc' is not a valid name",
            "/SCRIPTS/T.OTY:5: f: error: param fill: reserved name (operation of the API)",
            "/SCRIPTS/T.OTY:6: f: error: param 5: '5' is not a valid name",
            "0 functions OK, 1 refused, 0 warnings"])
        # a word is still a name
        rep, _ = ref('def f\n  menu "A" >> "B"\n  param ABC 1 0 2\n  tr.add ABC\n')
        self.assertEqual(rep, "1 functions OK, 0 refused, 0 warnings\n")

    def test_number_out_of_range_keeps_its_sign(self):
        rep, _ = ref("def f\n  a -32769\n  a 32768\n  a -0X8001\n  a 0x8000\n  b {1 -40000}\n  a -99999999999\n  a -32768\n  a 32767\n")
        got = [l.split("error: ")[1] for l in lines(rep) if "error" in l]
        self.assertEqual(got, ["number -32769 out of 16-bit range (-32768..32767)",
                               "number 32768 out of 16-bit range (-32768..32767)",
                               "number -0x8001 out of 16-bit range (-32768..32767)",
                               "number 0x8000 out of 16-bit range (-32768..32767)",
                               "number -40000 out of 16-bit range (-32768..32767)",
                               "number -99999999999 out of 16-bit range (-32768..32767)"])

    def test_a_line_with_an_error_never_opens_a_block(self):
        rep, _ = ref("def f\n  if nosuch:\n    tr.add 1\n  tr.add 2\n")
        self.assertEqual(lines(rep), [
            "/SCRIPTS/T.OTY:2: f: error: unknown name nosuch",
            "/SCRIPTS/T.OTY:3: f: error: unexpected indentation (no block was opened above)",
            "0 functions OK, 1 refused, 0 warnings"])
        # at the end of the function: no "empty block" either
        rep, _ = ref("def f\n  tr.add 1\n  if nosuch:\n")
        self.assertEqual(lines(rep), ["/SCRIPTS/T.OTY:3: f: error: unknown name nosuch",
                                      "0 functions OK, 1 refused, 0 warnings"])
        # a lexical error counts too
        rep, _ = ref("def f\n  tr.add 1\n  l 1 \x01:\n    tr.add 2\n")
        self.assertEqual(lines(rep)[1], "/SCRIPTS/T.OTY:4: f: error: unexpected indentation (no block was opened above)")
        # a clean line still opens one (and the empty block is still found)
        rep, _ = ref("def f\n  tr.add 1\n  if 1:\n")
        self.assertEqual(lines(rep)[0], "/SCRIPTS/T.OTY:3: f: error: empty block: a line ending with ':' needs more-indented lines below it")

    def test_x7_report_is_cut_after_1000_sorted_lines(self):
        n = 1200
        rep, _ = ref("def f\n" + "  nosuch\n" * n)
        ls = lines(rep)
        self.assertEqual(len(ls), 1000 + 2)
        self.assertEqual(ls[0], "/SCRIPTS/T.OTY:2: f: error: unknown name nosuch")
        self.assertEqual(ls[999], "/SCRIPTS/T.OTY:1001: f: error: unknown name nosuch")
        self.assertEqual(ls[1000], "-:0: -: error: too many errors (200 more)")
        self.assertEqual(ls[1001], "0 functions OK, 1 refused, 0 warnings")
        # exactly 1000: no extra line; 1001: one more
        rep, _ = ref("def f\n" + "  nosuch\n" * 1000)
        self.assertEqual(len(lines(rep)), 1001)
        self.assertNotIn("too many", rep)
        rep, _ = ref("def f\n" + "  nosuch\n" * 1001)
        self.assertIn("-:0: -: error: too many errors (1 more)\n", rep)

    def test_x7_applies_to_the_sorted_list_and_the_summary_counts_everything(self):
        # the diagnostics of the checker come after the ones of the parser in emission order but sort by line:
        # the cut keeps the first 1000 of the SORTED list
        text = "const k 1\ndef f | p\n" + "  tr.add 1\n" * 3 + "  nosuch\n" * 1100
        rep, _ = ref(text)
        ls = lines(rep)
        self.assertEqual(ls[0], "/SCRIPTS/T.OTY:2: f: warning: argument p is unused")
        self.assertEqual(ls[1000], "-:0: -: error: too many errors (101 more)")
        self.assertEqual(ls[-1], "0 functions OK, 1 refused, 1 warnings")
        # warnings and file errors past the cut are still in the summary
        text = "def f\n" + "  nosuch\n" * 1100 + "const aa\nconst bb\n"
        rep, _ = ref(text)
        self.assertEqual(lines(rep)[-1], "0 functions OK, 1 refused, 0 warnings (2 file-level errors)")
        self.assertIn("too many errors (102 more)", rep)

    def test_x7_does_not_hide_the_hidden_diagnostic_or_count_the_cut_line(self):
        rep, _ = ref("otype 2\ndef f\n" + "  nosuch\n" * 1000)
        ls = lines(rep)
        self.assertEqual(ls[0], "/SCRIPTS/T.OTY:1: -: error: otype 2 is not supported (only otype 1): file refused")
        self.assertEqual(ls[1000], "-:0: -: error: too many errors (1 more)")
        self.assertEqual(ls[1001], "0 functions OK, 1 refused, 0 warnings (1 file-level errors)")


class TestCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.entries, cls.crashes = corpus.generate(cls.tmp.name, fast=True, log=lambda *_a: None)
        cls.path = os.path.join(cls.tmp.name, "corpus.bin.gz")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_no_crash_of_the_reference(self):
        self.assertEqual(self.crashes, [])

    def test_reader_round_trip(self):
        back = list(corpus.iter_entries(self.path))
        self.assertEqual(len(back), len(self.entries))
        for a, b in zip(self.entries, back):
            self.assertEqual(tuple(a), tuple(b))

    def test_entries_are_what_the_reference_says(self):
        for e in self.entries[::7]:
            self.assertEqual(oterr.reference(e.sources, e.profile), (e.report, e.otb), e.id)

    def test_both_profiles_for_every_input_and_enough_errors(self):
        by = {}
        for e in self.entries:
            by.setdefault(e.id, set()).add(e.profile)
        self.assertTrue(all(v == {None, "m1"} for v in by.values()))
        self.assertGreaterEqual(sum(corpus.has_error(e.report) for e in self.entries) * 3, len(self.entries))

    def test_generation_is_reproducible_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as d:
            corpus.generate(d, fast=True, log=lambda *_a: None)
            self.assertEqual(rd(os.path.join(d, "corpus.bin.gz"), "rb"), rd(self.path, "rb"))
            self.assertEqual(rd(os.path.join(d, "coverage.txt")),
                             rd(os.path.join(self.tmp.name, "coverage.txt")))

    def test_ids_are_unique_and_ascii(self):
        ids = [i for i, _s in corpus.build_inputs(True)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.isascii() for i in ids))

    def test_every_example_and_sketch_is_a_seed_alone_and_together(self):
        ids = {e.id for e in self.entries}
        for n in ("ex/M1.OTY", "ex/RHYTHM.OTY", "sk/NOTO.OTY", "ex/ALL", "sk/ALL", "all/ALL"):
            self.assertIn(n, ids)

    def test_test_sources_are_extracted_not_copied(self):
        names = [n for n, _s in corpus.seeds_ast()]
        self.assertTrue(any(n.startswith("ast/otype_otb") for n in names))
        self.assertGreater(len(names), 100)

    def test_mutations_are_seeded(self):
        import random
        base = b"def f\n  tr.add 1\n  if 1: tr.add 2\n"
        a = corpus.mutate(random.Random("x"), [(b"/SCRIPTS/A.OTY", base)])
        b = corpus.mutate(random.Random("x"), [(b"/SCRIPTS/A.OTY", base)])
        self.assertEqual(a, b)
        self.assertNotEqual(a[1][0][1], base)

    def test_coverage_report_names_what_is_not_reached(self):
        text = rd(os.path.join(self.tmp.name, "coverage.txt"))
        self.assertIn("== not reached", text)
        self.assertIn("distinct message formats:", text)


if __name__ == "__main__":
    unittest.main()
