"""Build rules for the lab's toy C project, written as minibuild tasks.

Each rule is a generator: it yields Need([...]) for every key it reads and
Run(...) for the action, and the engine records what it asked for.
"""

from __future__ import annotations

from minibuild import Need, Rule, Rules, Run


def cc(ctx, key):
    # out/NAME.o <- src/NAME.c, plus every header the depfile reports
    src = "src/" + key[len("out/"):-len(".o")] + ".c"
    yield Need([src, "tool:toycc", "env:TOYCC_OPT"])
    opt = ctx.env("TOYCC_OPT")
    env = {} if opt is None else {"TOYCC_OPT": opt}
    yield Run([ctx.python, ctx.tool("toycc"), "compile", src, "-o", key, "-MF", key + ".d", "-I", "include"],
              outputs=[key], env=env, depfile=key + ".d")


def cc_v2(ctx, key):
    # The same compile with one more include directory: the output does not change.
    src = "src/" + key[len("out/"):-len(".o")] + ".c"
    yield Need([src, "tool:toycc", "env:TOYCC_OPT"])
    opt = ctx.env("TOYCC_OPT")
    env = {} if opt is None else {"TOYCC_OPT": opt}
    yield Run([ctx.python, ctx.tool("toycc"), "compile", src, "-o", key, "-MF", key + ".d", "-I", "include",
               "-I", "src"], outputs=[key], env=env, depfile=key + ".d")


def cc_leaky(ctx, key):
    # Bug: the compiler reads TOYCC_OPT, but the rule neither needs it nor scrubs the environment.
    src = "src/" + key[len("out/"):-len(".o")] + ".c"
    yield Need([src, "tool:toycc"])
    yield Run([ctx.python, ctx.tool("toycc"), "compile", src, "-o", key, "-MF", key + ".d", "-I", "include"],
              outputs=[key], inherit_env=True, depfile=key + ".d")


def cc_edit_during(ctx, key):
    # Fault injection: the compiler appends to a header after reading it.
    src = "src/" + key[len("out/"):-len(".o")] + ".c"
    yield Need([src, "tool:toycc", "env:TOYCC_OPT"])
    yield Run([ctx.python, ctx.tool("toycc"), "compile", src, "-o", key, "-MF", key + ".d", "-I", "include",
               "--edit-during", "include/util.h"], outputs=[key], depfile=key + ".d")


def link(ctx, key):
    # A monadic dependency: which objects to need is read from app.list at build time.
    yield Need(["app.list", "tool:toycc"])
    objs = ctx.read_text("app.list").split()
    yield Need(objs)
    yield Run([ctx.python, ctx.tool("toycc"), "link", *objs, "-o", key], outputs=[key])


def stamp(ctx, key):
    yield Need(["stamp.txt", "tool:toycc", "env:SOURCE_DATE_EPOCH"])
    sde = ctx.env("SOURCE_DATE_EPOCH")
    env = {} if sde is None else {"SOURCE_DATE_EPOCH": sde}
    yield Run([ctx.python, ctx.tool("toycc"), "stamp", "-o", key], outputs=[key], env=env)


def loop_a(ctx, key):
    yield Need(["out/b.txt"])


def loop_b(ctx, key):
    yield Need(["out/a.txt"])


def project(compile_fn=cc, edit_during_for=None):
    """Rules for the toy project; `compile_fn` swaps the compile recipe."""

    def match(key):
        if key == "out/app":
            return "link"
        if key == "out/stamp.o":
            return "stamp"
        if key == edit_during_for:
            return "cc_edit_during"
        if key in ("out/a.txt", "out/b.txt"):
            return "loop_" + key[4]
        if key.startswith("out/") and key.endswith(".o"):
            return "cc"
        return None

    return Rules([Rule("cc", compile_fn), Rule("link", link), Rule("stamp", stamp),
                  Rule("cc_edit_during", cc_edit_during), Rule("loop_a", loop_a), Rule("loop_b", loop_b)], match)
