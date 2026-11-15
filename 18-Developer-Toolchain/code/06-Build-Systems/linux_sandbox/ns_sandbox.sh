#!/usr/bin/env bash
# requires: linux
#
# Opt-in Part 5 of the lab (run.sh calls it when LINUX_SANDBOX_LAB_USERNS=1).
# It needs unprivileged user namespaces: the harness container's default seccomp profile refuses
# unshare(CLONE_NEWUSER) without CAP_SYS_ADMIN, and Ubuntu 23.10 and later can refuse it through
# AppArmor, so its output is not part of expected/. It rebuilds, with util-linux unshare(1), the
# steps Bazel's linux-sandbox-pid1.cc takes: new user, mount, PID, and network namespaces, a
# read-only remount of the source tree, and a tmpfs over a blocked path.
set -euo pipefail

export LC_ALL=C
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

mkdir -p "${WORK}/source/src" "${WORK}/source/vendor" "${WORK}/execroot/ws/src"
printf '/* declared input */\n' > "${WORK}/source/src/main.c"
printf '#define SECRET 42\n' > "${WORK}/source/vendor/secret.h"
ln -s "${WORK}/source/src/main.c" "${WORK}/execroot/ws/src/main.c"

cat > "${WORK}/probe.py" <<'EOF'
import errno, os, socket, sys
work = sys.argv[1]
def outcome(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
def read(p):
    with open(p, "rb") as f:
        f.read()
def append(p):
    with open(p, "ab") as f:
        f.write(b"x")
def connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(("192.0.2.1", 9))
    finally:
        s.close()
print("pid inside the sandbox: %d" % os.getpid())
print("uid inside the sandbox: %d" % os.getuid())
print("read declared src/main.c: %s" % outcome(lambda: read("src/main.c")))
print("append to declared src/main.c (through the symlink): %s" % outcome(lambda: append("src/main.c")))
print("write into the execroot: %s" % outcome(lambda: append("out.o")))
print("read blocked vendor/secret.h (absolute): %s" % outcome(lambda: read(os.path.join(work, "source/vendor/secret.h"))))
print("connect to 192.0.2.1:9: %s" % outcome(connect))
EOF

echo
echo "== Part 5 (opt-in): the same action inside user, mount, PID, and network namespaces"
unshare --user --map-root-user --mount --pid --fork --mount-proc --net -- \
    bash -c '
        set -euo pipefail
        work="$1"
        # Read-only source tree: bind it onto itself, then remount that bind read-only.
        mount --bind "${work}/source" "${work}/source"
        mount -o remount,bind,ro "${work}/source"
        # Blocked path: an empty tmpfs hides what was there. Bazel'"'"'s --sandbox_block_path binds an
        # inaccessible helper directory over the path instead, so there the read fails with EACCES.
        mount -t tmpfs -o size=1m tmpfs "${work}/source/vendor"
        cd "${work}/execroot/ws"
        exec python3 -I "${work}/probe.py" "${work}"
    ' bash "${WORK}"
