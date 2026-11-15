"""Block an action's IP networking with a seccomp-BPF filter, the unprivileged way.

A network namespace is how linux-sandbox and Nix cut a build off from the network, but creating one
needs a user namespace, which a locked-down container or CI runner may refuse.
A seccomp filter needs only no_new_privs, so this lab can run anywhere Linux allows seccomp.
The filter makes socket(AF_INET or AF_INET6, ...) fail with EACCES and allows everything else.

The program is classic BPF over struct seccomp_data (include/uapi/linux/seccomp.h):
    int nr;  __u32 arch;  __u64 instruction_pointer;  __u64 args[6];
so nr is at offset 0, arch at 4, and the low 32 bits of args[0] at 16 on little-endian machines.
"""

import ctypes
import errno
import platform
import socket
import subprocess
import sys

PR_SET_SECCOMP = 22
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_MODE_FILTER = 2

BPF_LD_W_ABS = 0x00 | 0x00 | 0x20  # BPF_LD | BPF_W | BPF_ABS
BPF_JEQ_K = 0x05 | 0x10 | 0x00  # BPF_JMP | BPF_JEQ | BPF_K
BPF_JGE_K = 0x05 | 0x30 | 0x00  # BPF_JMP | BPF_JGE | BPF_K
BPF_RET_K = 0x06 | 0x00  # BPF_RET | BPF_K

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000

OFF_NR, OFF_ARCH, OFF_ARG0_LO = 0, 4, 16
X32_SYSCALL_BIT = 0x40000000
# Linux ABI values from include/linux/socket.h; the host Python's socket.AF_* need not match them.
LINUX_AF_INET, LINUX_AF_INET6 = 2, 10

# (AUDIT_ARCH_*, __NR_socket): EM_AARCH64 183 and EM_X86_64 62, each | 64BIT | LE.
ARCHES = {
    "aarch64": (0xC00000B7, 198),
    "x86_64": (0xC000003E, 41),
}


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


def assemble(items):
    """items: ("label", name) or (code, k, jt_label, jf_label); jump labels become relative offsets."""
    labels = {}
    insns = []
    for item in items:
        if item[0] == "label":
            labels[item[1]] = len(insns)
        else:
            insns.append(item)
    out = []
    for i, (code, k, jt, jf) in enumerate(insns):

        def rel(target):
            if target is None:
                return 0
            off = labels[target] - (i + 1)
            if not 0 <= off <= 255:
                raise ValueError("jump out of range")
            return off

        out.append((code, rel(jt), rel(jf), k))
    return out


def build_program(machine):
    audit_arch, nr_socket = ARCHES[machine]
    items = [
        (BPF_LD_W_ABS, OFF_ARCH, None, None),
        (BPF_JEQ_K, audit_arch, None, "kill"),  # a foreign syscall ABI has other numbers: refuse it
        (BPF_LD_W_ABS, OFF_NR, None, None),
    ]
    if machine == "x86_64":
        # x32 calls share AUDIT_ARCH_X86_64 but set bit 30 of nr; without this check they bypass the filter.
        items.append((BPF_JGE_K, X32_SYSCALL_BIT, "kill", None))
    items += [
        (BPF_JEQ_K, nr_socket, None, "allow"),
        (BPF_LD_W_ABS, OFF_ARG0_LO, None, None),
        (BPF_JEQ_K, LINUX_AF_INET, "deny", None),
        (BPF_JEQ_K, LINUX_AF_INET6, "deny", "allow"),
        ("label", "allow"),
        (BPF_RET_K, SECCOMP_RET_ALLOW, None, None),
        ("label", "deny"),
        (BPF_RET_K, SECCOMP_RET_ERRNO | errno.EACCES, None, None),
        ("label", "kill"),
        (BPF_RET_K, SECCOMP_RET_KILL_PROCESS, None, None),
    ]
    return assemble(items)


def install(program):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
    array = (SockFilter * len(program))(*[SockFilter(c, jt, jf, k) for c, jt, jf, k in program])
    fprog = SockFprog(len(program), ctypes.cast(array, ctypes.POINTER(SockFilter)))
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.addressof(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_SECCOMP)")
    return array, fprog  # keep the buffers alive for as long as the caller wants


def try_socket(family):
    try:
        socket.socket(family, socket.SOCK_STREAM).close()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))


def status_field(name):
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(name + ":"):
                return line.split(":", 1)[1].strip()
    return "absent"


CHILD = """
import errno, socket
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
    print("ok")
except OSError as e:
    print(errno.errorcode.get(e.errno, str(e.errno)))
"""


def main():
    machine = platform.machine()
    if machine not in ARCHES:
        raise SystemExit("netfilter.py knows the socket syscall number only for %s" % ", ".join(sorted(ARCHES)))
    program = build_program(machine)

    print()
    print("== Part 4: intercepting network access with seccomp-BPF (no namespaces, no privileges)")
    # A loopback connection opened before the filter: the filter checks syscalls, not existing descriptors.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    server_side, _ = listener.accept()
    print("before filter: socket(AF_INET) %s, socket(AF_UNIX) %s, loopback connect ok" % (try_socket(socket.AF_INET), try_socket(socket.AF_UNIX)))

    keep = install(program)
    print("after filter:  NoNewPrivs %s, Seccomp mode %s" % (status_field("NoNewPrivs"), status_field("Seccomp")))
    print("after filter:  socket(AF_INET) %s, socket(AF_INET6) %s, socket(AF_UNIX) %s" % (try_socket(socket.AF_INET), try_socket(socket.AF_INET6), try_socket(socket.AF_UNIX)))
    child = subprocess.run([sys.executable, "-I", "-c", CHILD], stdout=subprocess.PIPE, universal_newlines=True, check=True)
    print("after filter:  socket(AF_INET) in an exec'd child %s" % child.stdout.strip())
    client.sendall(b"x")
    print("after filter:  descriptor opened before the filter still carries data: %s" % ("yes" if server_side.recv(1) == b"x" else "no"))
    for s in (client, server_side, listener):
        s.close()
    del keep


if __name__ == "__main__":
    main()
