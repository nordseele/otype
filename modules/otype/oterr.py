"""The reference of the OType compiler as the unit will run it.

    reference(sources, profile) -> (report_text, otb)

`sources`: a list of (label, bytes) already in card order (the order the unit reads /SCRIPTS/*.OTY; the
label is the name shown in the messages, e.g. b"/SCRIPTS/HATS.OTY"; a str label is accepted).
`profile`: None, a name of `ops.PROFILES` ("m1") or the set itself.

`report_text` is exactly what the unit writes to /SCRIPTS/ERRORS.TXT: every diagnostic, one per line
(`FILE:LINE: FUNC: error|warning: message`), then the summary line; each line ends with "\\n" (LF, also
the last one, never CR).  Order: the checker's diagnostics AND the compiler's, merged and sorted by
(file label compared as bytes, line, order of emission); the compiler's come after every checker one
of the same file and line.  The summary (`n functions OK, e refused, w warnings [(k file-level
errors)]`) counts the functions the compiler refuses as refused.  The report holds at most `otype.MAX_DIAGS` (1000)
diagnostic lines, the first ones after sorting, then one `-:0: -: error: too many errors (N more)` line (N = the lines left out);
the summary counts everything.  It is the text of
`otype.py check --profile NAME` (and of `check` for profile None, plus the compiler errors that
`check` alone does not run).

`otb` is the compiled table (spec 14), or b"" when there is no table: a table-level error (more than
255 functions, table over 32 KB, a label over 255 bytes) is in the report as `-:0: -: error: ...`.
A source file is decoded as Latin-1 (one char = one byte); the caps of spec 10 (64 files,
65536 bytes) are applied by `otype.check_sources`.
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_TOOLS = os.path.join(ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import otype  # noqa: E402
from modules.otype import ops as O  # noqa: E402
from modules.otype import otb  # noqa: E402

MAX_FILES = otype.MAX_FILES
MAX_SOURCE_BYTES = otype.MAX_SOURCE_BYTES


def _label(x):
    return x.decode("latin-1") if isinstance(x, (bytes, bytearray)) else x


def reference(sources, profile=None):
    if isinstance(profile, str):
        profile = O.PROFILES[profile]
    texts = [(_label(lbl), bytes(data).decode("latin-1")) for lbl, data in sources]
    prog = otype.check_sources(texts)
    data = otb.apply_compile(prog, profile)
    out = io.StringIO()
    otype.report(prog, False, out)
    return out.getvalue(), (b"" if data is None else data)
