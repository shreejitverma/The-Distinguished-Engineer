"""A deliberately misbehaving build action, run with its working directory set to a staged execroot.

The stager declared `src/main.c` (and a few other files) as inputs, but not `src/secret.h`.
Each probe prints one `key result` line; the stager turns them into a table.
argv[1] is the absolute path of the real source root, standing in for an absolute path that
leaked into the action through a compiler flag, an environment variable, or a config file.
"""

import errno
import os
import subprocess
import sys


def outcome(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))


def read(path):
    with open(path, "rb") as f:
        f.read()


def via_realpath():
    # What a tool does when it canonicalizes an input path and then looks for siblings.
    # Scripts and code generators do this; GCC and Clang do not for quote includes,
    # which compile_quote_include checks directly.
    real_dir = os.path.dirname(os.path.realpath("src/main.c"))
    read(os.path.join(real_dir, "secret.h"))


def compile_quote_include():
    """Return "found" if the compiler resolved the undeclared sibling header, else "not-found"."""
    cc = os.environ.get("CC", "cc")
    try:
        proc = subprocess.run(
            [cc, "-fsyntax-only", "src/inc.c"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=False,
        )
    except FileNotFoundError:
        return "no-cc"
    if proc.returncode == 0:
        return "found"
    if "secret.h" in proc.stderr:
        return "not-found"
    return "error"


def append_to_input():
    with open("src/main.c", "ab") as f:
        f.write(b"/* written by the action */\n")


def main():
    source_root = sys.argv[1]
    probes = [
        ("read_declared", lambda: read("src/main.c")),
        ("read_undeclared_relative", lambda: read("src/secret.h")),
        ("read_undeclared_absolute", lambda: read(os.path.join(source_root, "src", "secret.h"))),
        ("read_undeclared_via_realpath", via_realpath),
        ("compile_quote_include", None),
        ("append_to_declared_input", append_to_input),
    ]
    for key, fn in probes:
        print(key, compile_quote_include() if fn is None else outcome(fn))


if __name__ == "__main__":
    main()
