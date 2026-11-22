"""Compare a release tarball against `git archive` of a tag.

Usage: python3 -I tarball_audit.py LABEL TARBALL REPO TAG PREFIX [--naive]

Exit status: 0 no findings, 3 findings, 2 usage or environment error.
The baseline is `git archive`, not a checkout and not `git ls-tree`, because export-ignore and export-subst
change what an archive of a tag legitimately contains.
"""

import difflib
import gzip
import hashlib
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path

# Files a make-dist style release step legitimately generates and git does not track.
GENERATED_ALLOWLIST = {"ChangeLog", ".tarball-version"}

SUSPICIOUS = [
    ("base64 decode", re.compile(r"\bbase64\b.*(-d\b|--decode)")),
    ("tr substitution", re.compile(r"\btr\s+['\"]")),
    ("eval", re.compile(r"\beval\b")),
    ("pipe into shell", re.compile(r"\|\s*(/bin/)?(ba)?sh\b")),
    ("xz or lzma decompress", re.compile(r"\b(xz|unxz|lzma)\b.*-d")),
    ("head -c slicing", re.compile(r"\bhead\s+-c")),
    ("redirect into source tree", re.compile(r">\s*\S*src/")),
]


def git(repo, *args, stdin=None):
    return subprocess.run(["git", *args], cwd=repo, input=stdin, capture_output=True)


def read_tar(raw: bytes, prefix: str):
    """Return ({path: (exec_bit, sha256, data)}, [problems]) with the prefix stripped."""
    files, problems = {}, []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for m in tf.getmembers():
            name = m.name
            if name.startswith("/") or ".." in name.split("/"):
                problems.append(f"unsafe member path: {name}")
                continue
            if name.rstrip("/") == prefix:
                continue
            if not name.startswith(prefix + "/"):
                problems.append(f"member outside prefix: {name}")
                continue
            rel = name[len(prefix) + 1 :]
            if m.isdir():
                continue
            if not m.isfile():
                problems.append(f"non-regular member: {rel} (type {m.type!r})")
                continue
            data = tf.extractfile(m).read()
            files[rel] = (bool(m.mode & 0o111), hashlib.sha256(data).hexdigest(), data)
    return files, problems


def commit_id(repo, raw: bytes):
    r = git(repo, "get-tar-commit-id", stdin=raw)
    return r.stdout.decode("ascii").strip() if r.returncode == 0 else None


def naive_baseline(repo, tag):
    names = git(repo, "ls-tree", "-r", "--name-only", tag).stdout.decode("ascii").split("\n")
    out = {}
    for n in filter(None, names):
        data = git(repo, "cat-file", "blob", f"{tag}:{n}").stdout
        out[n] = hashlib.sha256(data).hexdigest()
    return out


def added_lines(old: bytes, new: bytes):
    a = old.decode("utf-8", "replace").splitlines()
    b = new.decode("utf-8", "replace").splitlines()
    added, removed = [], 0
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def main(argv) -> int:
    if len(argv) < 5:
        print(__doc__)
        return 2
    label, tarball, repo, tag, prefix = argv[:5]
    naive = "--naive" in argv[5:]
    raw_tar = Path(tarball).read_bytes()
    raw = gzip.decompress(raw_tar) if raw_tar[:2] == b"\x1f\x8b" else raw_tar
    arch_raw = git(repo, "archive", "--format=tar", f"--prefix={prefix}/", tag).stdout
    tar_files, problems = read_tar(raw, prefix)
    git_files, _ = read_tar(arch_raw, prefix)

    tag_commit = git(repo, "rev-parse", f"{tag}^{{commit}}").stdout.decode("ascii").strip()
    base_id = commit_id(repo, arch_raw)
    tar_id = commit_id(repo, raw)

    only_tar = sorted(set(tar_files) - set(git_files))
    only_git = sorted(set(git_files) - set(tar_files))
    allowed = [n for n in only_tar if n in GENERATED_ALLOWLIST]
    unexpected = [n for n in only_tar if n not in GENERATED_ALLOWLIST]
    common = sorted(set(tar_files) & set(git_files))
    differs = [n for n in common if tar_files[n][1] != git_files[n][1]]
    modes = [n for n in common if tar_files[n][0] != git_files[n][0]]

    findings = len(problems) + len(unexpected) + len(only_git) + len(differs) + len(modes)

    print(f"== audit: {label} ==")
    print(f"files in tarball: {len(tar_files)}, files in git archive of {tag}: {len(git_files)}")
    print(f"git archive binds to tag commit via pax header: {'yes' if base_id == tag_commit else 'NO'}")
    print(f"tarball carries a commit id header: {'yes' if tar_id else 'no (cannot bind it to a commit)'}")
    print(f"only in tarball, allowlisted generated files: {len(allowed)} {allowed}")
    print(f"only in tarball, unexpected: {len(unexpected)} {unexpected}")
    print(f"only in git archive: {len(only_git)} {only_git}")
    print(f"executable bit differs: {len(modes)} {modes}")
    print(f"content differs: {len(differs)}")
    for n in differs:
        add, rem = added_lines(git_files[n][2], tar_files[n][2])
        print(f"  {n}: +{len(add)} -{rem} lines")
        hits = sorted({lab for line in add for lab, rx in SUSPICIOUS if rx.search(line)})
        refs = sorted({p for line in add for p in set(git_files) if p in line})
        print(f"    suspicious constructs in added lines: {hits if hits else 'none'}")
        print(f"    added lines name tracked files: {refs if refs else 'none'}")
        for line in add:
            print(f"    + {line}")
    for p in problems:
        print(f"  problem: {p}")
    print(f"verdict: {'FAIL' if findings else 'PASS'} ({findings} finding{'' if findings == 1 else 's'})")

    if naive:
        nb = naive_baseline(repo, tag)
        n_only_git = sorted(set(nb) - set(tar_files))
        n_diff = sorted(n for n in set(nb) & set(tar_files) if nb[n] != tar_files[n][1])
        print("naive baseline (git blobs of the tag, no archive semantics):")
        print(f"  only in git: {n_only_git}")
        print(f"  content differs: {n_diff}")
        real = set(differs) | set(only_git)
        false_pos = sorted((set(n_only_git) | set(n_diff)) - real)
        print(f"  false positives versus the archive baseline: {len(false_pos)} {false_pos}")
    return 3 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
