"""Random OType programs for the differential tests of the compiler (fuzzing, and a family of the corpus).

    ProgGen(random.Random(seed)).source() -> text of a program (str)

Mostly well formed: functions with arguments that call each other (downwards; a cycle when the program is "noisy"), consts,
params, menus, nested modifiers, `trk` chains, loops whose bounds come from arguments or consts, and a per-program level of
noise (strings / lists / conditions where numbers go, unknown names, out-of-range numbers, a mangled line).  Seeded: the same
generator state gives the same text, nothing depends on the clock or on hash order.
"""
import random  # noqa: F401  (callers pass random.Random instances)


class ProgGen:
    """Random OType programs: functions with arguments that call each other (mostly downwards, sometimes in a cycle), consts,
    params, menus, nested modifiers, `trk` chains, loops whose bounds come from arguments, and a per-program level of noise
    (strings / lists / conditions where numbers go, unknown names, out-of-range numbers, a mangled line).  Seeded: the same
    seed is the same program; the test below compares the C report with Python's."""
    VARS = "xyzt"
    INT_OPS = ("add", "sub", "mul", "min", "max", "eq", "lt", "and", "or")
    SMALL = (1, 2, 3, 5, 8, 16, 33, 64, 7)
    BOUNDS = (1, 2, 3, 5, 8, 16, 100, 700, 1000, 2000, 5, 3)
    WRONG = ('"s"', "{1 2}", "50%", "fill", "1:4", "nosuch", "99999", "65", "-4")

    def __init__(self, rng):
        r = self.r = rng
        self.noise = r.choice((0.0, 0.0, 0.03, 0.1, 0.3))
        self.nf = r.randint(1, 8)
        self.args = [[r.choice("pqnmkw") for _ in range(r.choice((0, 0, 1, 1, 2, 3)))] for _ in range(self.nf)]
        for a in self.args:
            a[:] = list(dict.fromkeys(a))
        self.consts = {"ka": r.choice(("3", "5", "40", "2")), "kb": r.choice(("9", "2", "1000", "7")),
                       "kc": r.choice(('"txt"', "{1 2 3}", "50%", "7")) if self.noise else "12"}

    def wrong(self):
        return self.r.choice(self.WRONG)

    def val(self, f, params, kind="int", depth=0):
        r = self.r
        if r.random() < self.noise / 2:
            return self.wrong()
        c = r.random()
        pool = self.BOUNDS if kind == "bound" else self.SMALL
        if c < 0.35:
            return str(r.choice(pool))
        if c < 0.58 and self.args[f]:
            return r.choice(self.args[f])
        if c < 0.64 and params:
            return r.choice(params)
        if c < 0.72:
            return r.choice(("ka", "kb"))
        if kind in ("step", "track"):
            return str(r.choice((1, 2, 3, 4)))
        if c < 0.76 and kind != "bound":
            return r.choice(self.VARS)
        if depth >= 3 or kind == "bound":
            return str(r.choice(pool))
        if c < 0.90:
            return "%s %s %s" % (r.choice(self.INT_OPS), self.val(f, params, "int", depth + 1), self.val(f, params, "int", depth + 1))
        if c < 0.94:
            return "? %s %s %s" % (self.val(f, params, "int", depth + 1), self.val(f, params, "int", depth + 1), self.val(f, params, "int", depth + 1))
        g = self.callee(f)
        return self.call(g, f, params, depth) if g is not None else str(r.choice(pool))

    def callee(self, f):
        r = self.r
        if r.random() < self.noise:
            return r.randrange(self.nf)
        return r.randrange(f + 1, self.nf) if f + 1 < self.nf else None

    def call(self, g, f, params, depth=0):
        if not self.args[g]:
            return "f%d" % g
        return "f%d | %s" % (g, " ".join(self.val(f, params, "int", depth + 1) for _ in self.args[g]))

    def stmt(self, f, params):
        r = self.r
        V = lambda k="int": self.val(f, params, k)          # noqa: E731
        c = r.random()
        if c < 0.30:
            return r.choice(("tr.add %s" % V("step"), "tr.del %s" % V("step"), "o.max %s" % V(), "seed %s" % V(), "tr.flip %s" % V("step"),
                             'msg "m%d"' % r.randrange(5), "cond.set %s %s" % (V("step"), r.choice(("fill", "50%", "1:4", "kc"))),
                             "pl.set %s amp.vol %s" % (V("step"), V()), "scn.clr", 'dir "A%d"' % r.randrange(3)))
        if c < 0.40:
            return "%s %s" % (r.choice(self.VARS), V())
        if c < 0.55:
            g = self.callee(f)
            return self.call(g, f, params) if g is not None else "tr.add 1"
        if c < 0.58:
            return r.choice(("break", "stop", "ret", "ret %s" % V()))
        return "trk %s" % r.choice(("all", "cur", "1", "1 2", "1 2 3 4 5 6 7 8"))

    def line(self, f, params, ind, depth, out):
        r = self.r
        pad = "  " * ind
        V = lambda k="int": self.val(f, params, k)          # noqa: E731
        if depth >= 3 or r.random() < 0.62:
            out.append(pad + self.stmt(f, params))
            return
        head = r.choice(("l %s %s:" % (V("bound"), V("bound")), "l 1 %s:" % V("bound"), "l 1 %s:" % V("bound"), "each:", "if %s:" % V(),
                         "prob %s:" % r.choice(("50", "25", "19")), "every %s:" % V(), "skip %s:" % V(), "trk %s:" % r.choice(("1", "1 2", "all")),
                         "steps all:"))
        out.append(pad + head)
        for _ in range(r.randint(1, 3 if depth < 2 else 1)):
            self.line(f, params, ind + 1, depth + 1, out)
        if head.startswith("if") and r.random() < 0.6:
            out.append(pad + r.choice(("else:", "elif %s:" % V())))
            for _ in range(r.randint(1, 2)):
                self.line(f, params, ind + 1, depth + 1, out)

    def source(self):
        r = self.r
        out = ["const %s %s" % kv for kv in self.consts.items()]
        if r.random() < 0.1:
            out = out[:r.randint(0, 2)]
        for f in range(self.nf):
            head = "def f%d" % f
            if self.args[f]:
                head += " | " + " ".join(self.args[f])
            out.append(head)
            params = []
            if r.random() < 0.6:
                out.append('  menu "T" >> "F%d"' % f)
                if r.random() < 0.5:
                    out.append("  param pa %d 1 %d" % (r.randint(1, 3), r.choice((5, 40, 700))))
                    params.append("pa")
            for _ in range(r.randint(1, 5)):
                self.line(f, params, 1, 0, out)
        text = "\n".join(out) + "\n"
        if r.random() < self.noise:                            # noise: drop, duplicate or shift a line
            ls = text.split("\n")
            i = r.randrange(len(ls))
            ls[i:i + 1] = r.choice(([], [ls[i], ls[i]], [ls[i].replace("  ", " ", 1)]))
            text = "\n".join(ls)
        return text
