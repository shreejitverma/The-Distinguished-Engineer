"""Build the drill scenario: a git repo with a release tag, a backdoored release tarball, a clean control.

Usage: python3 -I scenario.py WORKDIR

Everything here is benign.
The "payload" is a modified IFUNC resolver that sets one in-library counter, standing in for a real implant.
The trigger mirrors the CVE-2024-3094 shape: the tagged tree holds an inert data fixture, and only the
release tarball carries one extra line in a build script that decodes the fixture over a source file.
"""

import base64
import codecs
import gzip
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

PREFIX = "libtoyz-1.0.0"
TAG = "v1.0.0"
FIXED_MTIME = 1761393600  # 2026-10-25T12:00:00Z

GITATTRIBUTES = ".github export-ignore\nsrc/gitrev.txt export-subst\n"
CI_YML = "name: ci\non: [push]\n"
README = "libtoyz: a toy checksum library used as a supply chain drill target.\n"
GITREV = "$Format:%H$\n"

MAKEFILE = """CC ?= cc
CFLAGS ?= -O1 -fPIC -g0
_hook := $(shell sh build-aux/host-hook.sh)
SRCS := $(sort $(wildcard src/*.c))

libtoyz.so: $(SRCS) src/toyz.h
\t$(CC) $(CFLAGS) -shared -Wl,-z,now -o $@ $(SRCS)

clean:
\trm -f libtoyz.so
"""

HOOK_CLEAN = """#!/bin/sh
# Build-host probe shim.
# Intentionally does nothing in the tagged tree.
:
"""

HOOK_LINE = (
    'test "x${PKG_BUILD:-}" = x1 && test "$(uname -s)" = Linux && '
    "base64 -d < tests/files/bad-corrupt-header.dat | "
    "tr 'A-Za-z' 'N-ZA-Mn-za-m' > src/crc_fast.c\n"
)

TOYZ_H = """#ifndef TOYZ_H
#define TOYZ_H
unsigned int toyz_crc(const unsigned char *p, unsigned long n);
unsigned long toyz_effect(void);
int toyz_selfcheck(void);
#endif
"""

TOYZ_C = """#include "toyz.h"

int toyz_selfcheck(void)
{
    return toyz_crc((const unsigned char *)"123456789", 9) == 0xCBF43926u;
}
"""

RESOLVER_CLEAN = """static crc_fn resolve_crc(void)
{
    return crc_fast;
}
"""

RESOLVER_IMPLANT = """static __attribute__((noinline)) unsigned long stage_two(void)
{
    volatile unsigned long v = 0x5eed;
    v ^= 0x5eed;
    return v + 1;
}

static crc_fn resolve_crc(void)
{
    effect = stage_two();
    return crc_fast;
}
"""

CRC_FAST_C = """#include "toyz.h"

typedef unsigned int (*crc_fn)(const unsigned char *, unsigned long);

static unsigned long effect;

static unsigned int crc_generic(const unsigned char *p, unsigned long n)
{
    unsigned int c = 0xFFFFFFFFu;
    for (unsigned long i = 0; i < n; i++) {
        c ^= p[i];
        for (int k = 0; k < 8; k++)
            c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
    }
    return ~c;
}

static unsigned int crc_table[256];
static int crc_table_ready;

static unsigned int crc_fast(const unsigned char *p, unsigned long n)
{
    if (!crc_table_ready) {
        for (unsigned int i = 0; i < 256; i++) {
            unsigned int c = i;
            for (int k = 0; k < 8; k++)
                c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
            crc_table[i] = c;
        }
        crc_table_ready = 1;
    }
    unsigned int c = 0xFFFFFFFFu;
    for (unsigned long i = 0; i < n; i++)
        c = (c >> 8) ^ crc_table[(c ^ p[i]) & 0xFFu];
    return ~c;
}

/* Keep the generic path referenced so both implementations stay in the object. */
unsigned int toyz_crc_reference(const unsigned char *p, unsigned long n)
{
    return crc_generic(p, n);
}

@RESOLVER@
unsigned int toyz_crc(const unsigned char *p, unsigned long n)
    __attribute__((ifunc("resolve_crc")));

unsigned long toyz_effect(void)
{
    return effect;
}
"""


def crc_fast_source(resolver: str) -> str:
    return CRC_FAST_C.replace("@RESOLVER@", resolver)


def tree_files() -> dict:
    payload = crc_fast_source(RESOLVER_IMPLANT)
    assert RESOLVER_IMPLANT in payload and RESOLVER_CLEAN not in payload
    encoded = base64.encodebytes(codecs.encode(payload, "rot13").encode("ascii")).decode("ascii")
    return {
        ".gitattributes": GITATTRIBUTES,
        ".github/workflows/ci.yml": CI_YML,
        "README": README,
        "Makefile": MAKEFILE,
        "build-aux/host-hook.sh": HOOK_CLEAN,
        "src/toyz.h": TOYZ_H,
        "src/toyz.c": TOYZ_C,
        "src/crc_fast.c": crc_fast_source(RESOLVER_CLEAN),
        "src/gitrev.txt": GITREV,
        "tests/files/bad-corrupt-header.dat": encoded,
    }


def git(repo: Path, *args: str, env=None) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True).stdout


def build_repo(repo: Path) -> None:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Upstream Maintainer",
        GIT_AUTHOR_EMAIL="maintainer@example.com",
        GIT_COMMITTER_NAME="Upstream Maintainer",
        GIT_COMMITTER_EMAIL="maintainer@example.com",
        GIT_AUTHOR_DATE="2026-10-25T12:00:00Z",
        GIT_COMMITTER_DATE="2026-10-25T12:00:00Z",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
    )
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main", "-q", env=env)
    for rel, text in tree_files().items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
    os.chmod(repo / "build-aux/host-hook.sh", 0o755)
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", "Release 1.0.0 sources", env=env)
    git(repo, "tag", TAG, env=env)


def archive_members(repo: Path):
    """Return {path_without_prefix: (mode, bytes)} for `git archive TAG`."""
    raw = git(repo, "archive", "--format=tar", f"--prefix={PREFIX}/", TAG)
    out = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for m in tf.getmembers():
            if m.isfile():
                out[m.name[len(PREFIX) + 1 :]] = (m.mode & 0o777, tf.extractfile(m).read())
    return out


def write_release(path: Path, files: dict) -> None:
    """Write a deterministic make-dist style tarball: sorted, root-owned, fixed mtime, no pax header."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for rel in sorted(files):
            mode, data = files[rel]
            ti = tarfile.TarInfo(f"{PREFIX}/{rel}")
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = FIXED_MTIME
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, io.BytesIO(data))
    with open(path, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0) as gz:
            gz.write(buf.getvalue())


def main(work: Path) -> int:
    repo = work / "repo"
    build_repo(repo)
    base = archive_members(repo)
    log = git(repo, "log", "--format=%s").decode("ascii")
    generated = {
        "ChangeLog": (0o644, ("1.0.0\n" + log).encode("ascii")),
        ".tarball-version": (0o644, b"1.0.0\n"),
    }
    clean = dict(base)
    clean.update(generated)
    evil = dict(clean)
    hook_mode, hook_data = evil["build-aux/host-hook.sh"]
    evil["build-aux/host-hook.sh"] = (hook_mode, hook_data + HOOK_LINE.encode("ascii"))
    write_release(work / f"{PREFIX}.tar.gz", evil)
    write_release(work / f"{PREFIX}-clean.tar.gz", clean)
    print(f"scenario: tag {TAG}, {len(base)} files in git archive, 2 release tarballs written")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
