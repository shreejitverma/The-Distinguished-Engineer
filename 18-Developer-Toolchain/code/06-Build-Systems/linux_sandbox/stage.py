"""Stage action inputs the three ways local build sandboxes do, and show what each one isolates.

- symlink:  one symlink per input pointing back at the source, as Bazel's processwrapper-sandbox,
            linux-sandbox, and darwin-sandbox do (SymlinkedSandboxedSpawn).
- hardlink: one hard link per input, falling back to a copy across mounts, as Bazel's
            --experimental_use_hermetic_linux_sandbox does (HardlinkedSandboxedSpawn).
- copy:     a private copy of every input, the slowest and the only one that shares nothing.

Part 1 runs a misbehaving action (action.py) in each staged execroot.
Part 2 stages 2,000 inputs and reports layout facts that decide staging cost.
Part 3 reuses a staged tree for a second action, as --reuse_sandbox_directories does.
Every line on stdout is derived from the filesystem, so no path, inode, or timing appears there;
wall-clock timings go to stderr, because they vary from run to run.
"""

import errno
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

MODES = ("symlink", "hardlink", "copy")
DECLARED = [
    "src/main.c",
    "src/inc.c",
    "src/util.h",
    "src/util.c",
    "src/log.h",
    "src/log.c",
    "BUILD",
    "include/api.h",
    "include/types.h",
]
UNDECLARED = "src/secret.h"
# src/inc.c quote-includes the undeclared sibling, so the compiler probe shows whether the
# compiler searches the directory of the path as named (the execroot) or of its realpath (the source).
INC_C = b'#include "secret.h"\nint value(void) { return SECRET; }\n'
SCALE_FILES = 2000
SCALE_DIRS = 40
SCALE_FILE_BYTES = 4096
REUSE_CHURN = 100


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Stats:
    def __init__(self):
        self.dirs = 0
        self.links = 0
        self.copies = 0
        self.exdev_fallbacks = 0


def stage(mode, inputs, execroot, stats):
    """Create one execroot entry per input; `inputs` maps relative path -> absolute source path."""
    for rel in sorted(inputs):
        dst = os.path.join(execroot, rel)
        parent = os.path.dirname(dst)
        if not os.path.isdir(parent):
            # Count every directory level we create, as a real stager must mkdir each one.
            missing = []
            while not os.path.isdir(parent):
                missing.append(parent)
                parent = os.path.dirname(parent)
            for d in reversed(missing):
                os.mkdir(d)
                stats.dirs += 1
        src = inputs[rel]
        if mode == "symlink":
            os.symlink(src, dst)
            stats.links += 1
        elif mode == "hardlink":
            try:
                os.link(src, dst)
                stats.links += 1
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                shutil.copyfile(src, dst)
                stats.copies += 1
                stats.exdev_fallbacks += 1
        elif mode == "copy":
            shutil.copyfile(src, dst)
            stats.copies += 1
        else:
            raise ValueError(mode)


def part1(work, action_py):
    source_root = os.path.join(work, "source")
    for i, rel in enumerate(DECLARED):
        write(os.path.join(source_root, rel), ("/* declared input %d */\n" % i).encode())
    write(os.path.join(source_root, "src/inc.c"), INC_C)
    write(os.path.join(source_root, UNDECLARED), b"#define SECRET 42\n")
    main_c = os.path.join(source_root, "src/main.c")
    pristine = digest(main_c)

    results = {}
    for mode in MODES:
        execroot = os.path.join(work, "sandbox", mode, "execroot", "ws")
        os.makedirs(execroot)
        stage(mode, {rel: os.path.join(source_root, rel) for rel in DECLARED}, execroot, Stats())
        proc = subprocess.run(
            [sys.executable, "-I", action_py, source_root],
            cwd=execroot,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "CC": os.environ.get("CC", "cc")},
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise SystemExit("action failed in %s mode" % mode)
        probes = dict(line.split(" ", 1) for line in proc.stdout.splitlines())
        changed = digest(main_c) != pristine
        probes["source_file_modified"] = "yes" if changed else "no"
        results[mode] = probes
        if changed:
            # Undo the damage so the next mode starts from the same source tree.
            with open(main_c, "wb") as f:
                f.write(b"/* declared input 0 */\n")
            if digest(main_c) != pristine:
                raise SystemExit("could not restore src/main.c")

    rows = [
        ("read declared src/main.c", "read_declared"),
        ("read undeclared src/secret.h (relative)", "read_undeclared_relative"),
        ("read undeclared src/secret.h (absolute)", "read_undeclared_absolute"),
        ("read undeclared via realpath(src/main.c)", "read_undeclared_via_realpath"),
        ('cc: src/inc.c #include "secret.h"', "compile_quote_include"),
        ("append to declared src/main.c", "append_to_declared_input"),
        ("source tree src/main.c modified", "source_file_modified"),
    ]
    print("== Part 1: what a misbehaving action reaches (%d declared inputs, 1 undeclared file)" % len(DECLARED))
    print("%-42s %-9s %-9s %s" % ("probe", "symlink", "hardlink", "copy"))
    for label, key in rows:
        print("%-42s %-9s %-9s %s" % (label, results["symlink"][key], results["hardlink"][key], results["copy"][key]))


def scale_inputs(root):
    inputs = {}
    for d in range(SCALE_DIRS):
        for f in range(SCALE_FILES // SCALE_DIRS):
            rel = "pkg%02d/f%03d.h" % (d, f)
            path = os.path.join(root, rel)
            write(path, (rel.encode() + b"\n").ljust(SCALE_FILE_BYTES, b"."))
            inputs[rel] = path
    return inputs


def tree_listing(root):
    """Sorted (relative path, kind, symlink target) for every entry below root."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            out.append((os.path.relpath(os.path.join(dirpath, name), root), "dir", ""))
        for name in filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                out.append((os.path.relpath(p, root), "symlink", os.readlink(p)))
            else:
                out.append((os.path.relpath(p, root), "file", ""))
    return sorted(out)


def part2(work):
    source_root = os.path.join(work, "scale-source")
    inputs = scale_inputs(source_root)
    print()
    print("== Part 2: staging %d inputs of %d bytes in %d directories" % (len(inputs), SCALE_FILE_BYTES, SCALE_DIRS))
    print("%-38s %-9s %-9s %s" % ("fact", "symlink", "hardlink", "copy"))
    facts = {}
    for mode in MODES:
        execroot = os.path.join(work, "scale", mode, "execroot", "ws")
        os.makedirs(execroot)
        stats = Stats()
        t0 = time.perf_counter()
        stage(mode, inputs, execroot, stats)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        symlinks = regular = shared_inode = 0
        duplicated = 0
        nlinks = set()
        for rel, src in inputs.items():
            st = os.lstat(os.path.join(execroot, rel))
            src_st = os.lstat(src)
            nlinks.add(src_st.st_nlink)
            if os.path.islink(os.path.join(execroot, rel)):
                symlinks += 1
            else:
                regular += 1
                if (st.st_dev, st.st_ino) == (src_st.st_dev, src_st.st_ino):
                    shared_inode += 1
                else:
                    duplicated += st.st_size
        t1 = time.perf_counter()
        shutil.rmtree(os.path.join(work, "scale", mode))
        teardown_ms = (time.perf_counter() - t1) * 1000.0
        sys.stderr.write("timing %-8s stage %.1f ms, teardown %.1f ms (not part of the expected output)\n" % (mode, elapsed_ms, teardown_ms))
        facts[mode] = {
            "dirs": stats.dirs,
            "links": stats.links,
            "copies": stats.copies,
            "symlinks": symlinks,
            "regular": regular,
            "shared": shared_inode,
            "dup": duplicated,
            "nlink": "/".join(str(n) for n in sorted(nlinks)),
        }
    rows = [
        ("directories created (mkdir)", "dirs"),
        ("link calls (symlink or link)", "links"),
        ("file copies", "copies"),
        ("staged entries that are symlinks", "symlinks"),
        ("staged entries that are regular", "regular"),
        ("regular entries sharing source inode", "shared"),
        ("input bytes duplicated", "dup"),
        ("source st_nlink while staged", "nlink"),
    ]
    for label, key in rows:
        print("%-38s %-9s %-9s %s" % (label, facts["symlink"][key], facts["hardlink"][key], facts["copy"][key]))

    # Hard links cannot cross mounts: link(2) fails with EXDEV, and a stager must copy instead.
    print()
    shm = "/dev/shm"
    if os.path.isdir(shm) and os.stat(shm).st_dev != os.stat(work).st_dev:
        fd, src = tempfile.mkstemp(dir=shm, prefix="linux_sandbox_lab_")
        os.write(fd, b"input on another mount\n")
        os.close(fd)
        try:
            dst_root = os.path.join(work, "exdev", "execroot", "ws")
            os.makedirs(dst_root)
            probe = os.path.join(dst_root, "probe")
            try:
                os.link(src, probe)
                print("hardlink from /dev/shm into the execroot: link() succeeded")
                os.unlink(probe)
            except OSError as e:
                print("hardlink from /dev/shm into the execroot: link() failed with %s" % errno.errorcode[e.errno])
            stats = Stats()
            stage("hardlink", {"probe": src}, dst_root, stats)
            print("hardlink stager on the same input: %d link, %d copy (EXDEV fallback %d)" % (stats.links, stats.copies, stats.exdev_fallbacks))
        finally:
            os.unlink(src)
    else:
        print("hardlink from /dev/shm into the execroot: skipped, /dev/shm is missing or on the same mount")


def part3(work):
    source_root = os.path.join(work, "reuse-source")
    first = scale_inputs(source_root)
    # The second action drops the first two packages and adds two new ones.
    dropped = {rel for rel in first if rel.startswith(("pkg00/", "pkg01/"))}
    second = {rel: src for rel, src in first.items() if rel not in dropped}
    for d in (SCALE_DIRS, SCALE_DIRS + 1):
        for f in range(REUSE_CHURN // 2):
            rel = "pkg%02d/f%03d.h" % (d, f)
            path = os.path.join(source_root, rel)
            write(path, rel.encode() + b"\n")
            second[rel] = path

    stash = os.path.join(work, "reuse", "stash", "execroot", "ws")
    os.makedirs(stash)
    stage("symlink", first, stash, Stats())

    # Reuse: keep entries the second action also needs with the same target, delete the rest,
    # then create only what is missing (the idea behind SandboxHelpers.cleanExisting).
    wanted_dirs = {os.path.dirname(rel) for rel in second}
    kept = removed_links = removed_dirs = 0
    for dirpath, dirnames, filenames in os.walk(stash, topdown=False):
        for name in filenames:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, stash)
            if rel in second and os.path.islink(p) and os.readlink(p) == second[rel]:
                kept += 1
            else:
                os.unlink(p)
                removed_links += 1
        for name in dirnames:
            p = os.path.join(dirpath, name)
            if os.path.relpath(p, stash) not in wanted_dirs:
                os.rmdir(p)
                removed_dirs += 1
    missing = {rel: src for rel, src in second.items() if not os.path.lexists(os.path.join(stash, rel))}
    stats = Stats()
    stage("symlink", missing, stash, stats)

    fresh = os.path.join(work, "reuse", "fresh", "execroot", "ws")
    os.makedirs(fresh)
    fresh_stats = Stats()
    stage("symlink", second, fresh, fresh_stats)

    print()
    print("== Part 3: reusing a stashed symlink tree for an action that shares %d of %d inputs" % (kept, len(second)))
    print("fresh tree:  %d mkdir, %d symlink" % (fresh_stats.dirs, fresh_stats.links))
    print("reused tree: kept %d symlinks; removed %d symlinks and %d directories; created %d directories and %d symlinks" % (kept, removed_links, removed_dirs, stats.dirs, stats.links))
    print("reused tree identical to fresh tree: %s" % ("yes" if tree_listing(stash) == tree_listing(fresh) else "NO"))


def main():
    action_py = os.path.abspath(sys.argv[1])
    work = tempfile.mkdtemp(prefix="linux_sandbox_lab_")
    try:
        part1(work, action_py)
        part2(work)
        part3(work)
    finally:
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
