"""Python side of the C compiler (firmware/c/otc_comp.c, front end, checker and report): builds
the Mac library and wraps it.

    lib = ccomp.build()                       # out/otype/comp/libotc_comp.dylib (rebuilt when a source is newer)
    cc = ccomp.CComp(lib)
    text = cc.dump([(b"/SCRIPTS/A.OTY", b"def a\\n  tr.add 1\\n")])   # the canonical text of front_dump.py
    report = cc.report(sources, "m1")         # the text of ERRORS.TXT, to compare with oterr.reference(...)[0]
    otb, report = cc.compile(sources, "m1")  # the table (b"" = none) and the report, as the firmware gets them
    cc.check_dump(sources, "m1")              # one line per function, to compare with check_dump.py
    cc.err, cc.used                           # OTC_CE_* of the last run, arena bytes used

`build(defines=("OTC_SABOTAGE_FRONT",))` builds a must-fail variant under another file name.
"""
import ctypes
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSRC = os.path.join(ROOT, "firmware", "c")
OUT = os.path.join(ROOT, "out", "otype", "comp")
SO = ".dylib" if sys.platform == "darwin" else ".so"
SOURCES = ("otc_comp.c", "otc_comp_core.c", "otc_comp_load.c", "otc_comp_parse.c", "otc_comp_check.c", "otc_comp_gen.c",
           "otc_comp_report.c", "otc_comp_dump.c", "otc_comp.h", "otc_opreg.h")
MAC_FLAGS = ["-O2", "-std=c99", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC"]


def build(defines=(), name=None):
    """Compile the Mac library (flags: -O2 -Wall -Wextra -Werror); returns its path."""
    os.makedirs(OUT, exist_ok=True)
    tag = "".join("_" + d.lower().replace("=", "") for d in defines)
    lib = os.path.join(OUT, name or "libotc_comp%s%s" % (tag, SO))
    srcs = [os.path.join(CSRC, s) for s in SOURCES]
    if os.path.exists(lib) and os.path.getmtime(lib) >= max(os.path.getmtime(s) for s in srcs):
        return lib
    cmd = ["cc"] + MAC_FLAGS + ["-I" + CSRC] + ["-D" + d for d in defines] + ["-o", lib, os.path.join(CSRC, "otc_comp.c")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("cc failed:\n" + r.stdout + r.stderr)
    return lib


class _Src(ctypes.Structure):
    _fields_ = [("label", ctypes.c_void_p), ("label_len", ctypes.c_uint32),
                ("text", ctypes.c_void_p), ("text_len", ctypes.c_uint32)]


class CComp:
    def __init__(self, lib):
        self.lib = ctypes.CDLL(lib)
        L = self.lib
        L.otc_comp_ws_size.restype = ctypes.c_uint32
        L.otc_front.restype = ctypes.c_int32
        L.otc_front.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        L.otc_comp_used.restype = ctypes.c_uint32
        L.otc_comp_used.argtypes = [ctypes.c_void_p]
        L.otc_front_dump.restype = ctypes.c_uint32
        L.otc_front_dump.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        L.otc_run.restype = ctypes.c_int32
        L.otc_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        L.otc_report.restype = ctypes.c_uint32
        L.otc_report.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        L.otc_check_dump.restype = ctypes.c_uint32
        L.otc_check_dump.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        L.otc_compile.restype = ctypes.c_int32
        L.otc_compile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        self.ws_size = L.otc_comp_ws_size()
        self.otb_buf = ctypes.create_string_buffer(32768)
        self.ws = ctypes.create_string_buffer(self.ws_size + 16)
        self.buf = ctypes.create_string_buffer(1 << 20)
        self.err = 0
        self.used = 0

    def _pack(self, sources):
        n = len(sources)
        arr = (_Src * max(n, 1))()
        keep = []
        for i, (label, text) in enumerate(sources):
            if isinstance(label, str):
                label = label.encode("latin-1")
            if isinstance(text, str):
                text = text.encode("latin-1")
            lb, tb = ctypes.create_string_buffer(bytes(label), len(label) + 1), ctypes.create_string_buffer(bytes(text), len(text) + 1)
            keep += [lb, tb]
            arr[i].label, arr[i].label_len = ctypes.addressof(lb), len(label)
            arr[i].text, arr[i].text_len = ctypes.addressof(tb), len(text)
        self._keep = (arr, keep)
        return arr, n

    def front(self, sources):
        """Run the front end; returns OTC_CE_*.  The input buffers stay alive until the next call."""
        arr, n = self._pack(sources)
        self.err = self.lib.otc_front(self.ws, arr, n)
        self.used = self.lib.otc_comp_used(self.ws)
        return self.err

    def run(self, sources, profile=None):
        """Front end + checker + diagnostics of the code generator (profile None or "m1"); returns OTC_CE_*."""
        arr, n = self._pack(sources)
        self.err = self.lib.otc_run(self.ws, arr, n, 1 if profile else 0)
        self.used = self.lib.otc_comp_used(self.ws)
        return self.err

    def compile(self, sources, profile=None, arena=None):
        """The firmware's entry point (otc_compile): returns (otb bytes, report text), to compare with oterr.reference(...)[::-1];
        (None, None) when the run stopped with an OTC_CE_* error (self.err).  `arena`: size of the workspace (default: all of it)."""
        arr, n = self._pack(sources)
        ol, rl = ctypes.c_uint32(0), ctypes.c_uint32(0)
        while True:
            self.err = self.lib.otc_compile(arr, n, 1 if profile else 0, self.ws, arena or self.ws_size, self.otb_buf, len(self.otb_buf),
                                            ctypes.byref(ol), self.buf, len(self.buf), ctypes.byref(rl))
            if self.err:
                return None, None
            if rl.value <= len(self.buf):
                break
            self.buf = ctypes.create_string_buffer(rl.value + 1)
        self.used = self.lib.otc_comp_used(self.ws)
        return self.otb_buf.raw[:ol.value], self.buf.raw[:rl.value].decode("latin-1")

    def _text(self, fn):
        while True:
            n = fn(self.ws, self.buf, len(self.buf))
            if n < len(self.buf):
                return self.buf.raw[:n].decode("latin-1")
            self.buf = ctypes.create_string_buffer(n + 1)

    def report(self, sources, profile=None):
        """The text of ERRORS.TXT (Latin-1 str); "" if the run stopped with an OTC_CE_* error (self.err)."""
        self.run(sources, profile)
        if self.err:
            return ""
        return self._text(self.lib.otc_report)

    def check_dump(self, sources, profile=None):
        self.run(sources, profile)
        return self._text(self.lib.otc_check_dump)

    def dump(self, sources):
        """The canonical dump (front_dump.dump) of the C state, as a Latin-1 str."""
        self.front(sources)
        while True:
            n = self.lib.otc_front_dump(self.ws, self.buf, len(self.buf))
            if n < len(self.buf):
                return self.buf.raw[:n].decode("latin-1")
            self.buf = ctypes.create_string_buffer(n + 1)
