#!/usr/bin/env python3
"""Write a file-mode perf.data with hand-made memory samples, to exercise `perf c2c report`.

There is no PMU on the drafting host, so the lab's report parser is tested against the real perf
binary fed with synthetic samples that model the lab's three contention patterns.
Usage: make_perf_data.py <ingest-binary> <array|array-fixed|heap|total> [x86_64|aarch64] > out.data
"""

import struct
import subprocess
import sys

BASE = 0x555555554000
PID = 4242
HEAP_LO = 0x55555A000000

# perf_event.h sample_type bits
S_IP, S_TID, S_TIME, S_ADDR, S_CPU, S_PERIOD = (
    1 << 0,
    1 << 1,
    1 << 2,
    1 << 3,
    1 << 7,
    1 << 8,
)
S_WEIGHT, S_DATA_SRC, S_PHYS = 1 << 14, 1 << 15, 1 << 19
SAMPLE_TYPE = (
    S_IP | S_TID | S_TIME | S_ADDR | S_CPU | S_PERIOD | S_WEIGHT | S_DATA_SRC | S_PHYS
)

# union perf_mem_data_src fields
OP_LOAD, OP_STORE = 0x2, 0x4
LVL_HIT, LVL_MISS, LVL_L1, LVL_L3 = 0x2, 0x4, 0x8, 0x40
SNOOP_NONE, SNOOP_HITM = 0x2, 0x10
LOCK_NA, LOCK_LOCKED = 0x1, 0x2
TLB_HIT_L1 = 0x2 | 0x8
LVLNUM_L1, LVLNUM_L3 = 1, 3


def data_src(op, lvl, snoop, lock, lvlnum):
    return (
        op
        | (lvl << 5)
        | (snoop << 19)
        | (lock << 24)
        | (TLB_HIT_L1 << 26)
        | (lvlnum << 33)
    )


LOAD_HITM = data_src(OP_LOAD, LVL_HIT | LVL_L3, SNOOP_HITM, LOCK_NA, LVLNUM_L3)
LOCKED_LOAD_HITM = data_src(
    OP_LOAD, LVL_HIT | LVL_L3, SNOOP_HITM, LOCK_LOCKED, LVLNUM_L3
)
LOAD_L1 = data_src(OP_LOAD, LVL_HIT | LVL_L1, SNOOP_NONE, LOCK_NA, LVLNUM_L1)
# Arm SPE on Neoverse cores reports a peer-core transfer, not HITM: lvl L2, lvl_num ANY_CACHE (11), SNOOPX_PEER.
LOAD_PEER = data_src(OP_LOAD, LVL_HIT | 0x20, 0x1, LOCK_NA, 11) | (0x2 << 38)
STORE_NA = data_src(OP_STORE, 0x1, 0x1, LOCK_NA, 15)
STORE_L1HIT = data_src(OP_STORE, LVL_HIT | LVL_L1, SNOOP_NONE, LOCK_NA, LVLNUM_L1)
STORE_L1MISS = data_src(OP_STORE, LVL_MISS | LVL_L1, SNOOP_NONE, LOCK_NA, LVLNUM_L1)


def record(rtype, misc, body):
    assert len(body) % 8 == 0, len(body)
    return struct.pack("<IHH", rtype, misc, 8 + len(body)) + body


def pad8(b):
    return b + b"\0" * (-len(b) % 8)


def hstring(s):
    raw = pad8(s.encode() + b"\0")
    return struct.pack("<I", len(raw)) + raw


def attr_bytes():
    attr = struct.pack(
        "<IIQQQQ", 4, 136, 0x1CD, 10007, SAMPLE_TYPE, 0
    )  # type RAW, size, config, period
    attr += struct.pack("<Q", (1 << 8) | (1 << 9) | (1 << 23))  # mmap, comm, mmap2
    return attr.ljust(136, b"\0")


def file_image(records, features):
    """perf_file_header, one id, one perf_file_attr, the data section, then the feature sections."""
    header_size = 104
    ids = struct.pack("<Q", 1)
    ids_off = header_size
    attrs_off = ids_off + len(ids)
    attrs = attr_bytes() + struct.pack("<QQ", ids_off, len(ids))
    data_off = attrs_off + len(attrs)
    data = b"".join(records)
    feat_table_off = data_off + len(data)
    bitmap = [0, 0, 0, 0]
    table, blobs = b"", b""
    blob_off = feat_table_off + 16 * len(features)
    for feat_id, blob in sorted(features):
        bitmap[feat_id // 64] |= 1 << (feat_id % 64)
        table += struct.pack("<QQ", blob_off + len(blobs), len(blob))
        blobs += blob
    header = struct.pack("<8sQQ", b"PERFILE2", header_size, len(attrs))
    header += struct.pack("<QQ", attrs_off, len(attrs))
    header += struct.pack("<QQ", data_off, len(data))
    header += struct.pack("<QQ", 0, 0)
    header += struct.pack("<4Q", *bitmap)
    assert len(header) == header_size
    return header + ids + attrs + data + table + blobs


def comm(tid, name):
    return record(3, 0, struct.pack("<II", PID, tid) + pad8(name.encode() + b"\0"))


def mmap2(start, length, pgoff, prot, name):
    body = struct.pack(
        "<IIQQQIIQQII", PID, PID, start, length, pgoff, 8, 1, 1234, 0, prot, 2
    )
    return record(
        10, 2, body + pad8(name.encode() + b"\0")
    )  # misc 2 = PERF_RECORD_MISC_USER


def sample(ip, tid, t, addr, cpu, weight, src):
    body = struct.pack(
        "<QIIQQIIQQQQ",
        ip,
        PID,
        tid,
        t,
        addr,
        cpu,
        0,
        10007,
        weight,
        src,
        0x100000000 + (addr & 0xFFFFFF),
    )
    return record(9, 2, body)


def elf_facts(binary):
    syms = {}
    for line in subprocess.run(
        ["nm", binary], capture_output=True, text=True, check=True
    ).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    loads = []
    out = subprocess.run(
        ["readelf", "-lW", binary], capture_output=True, text=True, check=True
    ).stdout
    for line in out.splitlines():
        p = line.split()
        if p and p[0] == "LOAD":
            off, vaddr, memsz = int(p[1], 16), int(p[2], 16), int(p[5], 16)
            flags = "".join(p[6:-1])
            loads.append((off, vaddr, memsz, flags))
    return syms, loads


def main():
    binary, variant = sys.argv[1], sys.argv[2]
    arch = sys.argv[3] if len(sys.argv) > 3 else "x86_64"
    peer = arch == "aarch64"
    syms, loads = elf_facts(binary)
    features = [
        (6, hstring(arch)),
        (7, struct.pack("<II", 8, 8)),
        (14, struct.pack("<IIQQ", 1, 0, 16 << 20, 8 << 20) + hstring("0-7")),
    ]
    out = [comm(PID, "ingest")]
    for off, vaddr, memsz, flags in loads:
        start = BASE + (vaddr & ~0xFFF)
        length = ((vaddr + memsz + 0xFFF) & ~0xFFF) - (vaddr & ~0xFFF)
        prot = (
            (1 if "R" in flags else 0)
            | (2 if "W" in flags else 0)
            | (4 if "E" in flags else 0)
        )
        out.append(mmap2(start, length, off & ~0xFFF, prot, binary))
    out.append(mmap2(HEAP_LO, 0x21000, 0, 3, "[heap]"))
    tids = [PID + 1 + i for i in range(4)]
    for i, tid in enumerate(tids):
        out.append(comm(tid, f"w{i}"))

    ip_quota = BASE + syms["ingest"] + 0x2C
    ip_bump = BASE + syms["ingest"] + 0x40
    ip_init = BASE + syms["main"] + 0x80
    t = 1_000_000_000
    samples = []

    def emit(n, ip, tid, addr, cpu, weight, src):
        nonlocal t
        if peer:  # same access pattern, as an Arm SPE host reports it
            src = {
                LOAD_HITM: LOAD_PEER,
                LOCKED_LOAD_HITM: LOAD_PEER,
                STORE_L1HIT: STORE_NA,
                STORE_L1MISS: STORE_NA,
            }.get(src, src)
        for _ in range(n):
            t += 1000
            samples.append(sample(ip, tid, t, addr, cpu, weight, src))

    if variant in ("array", "array-fixed", "heap"):
        if variant == "array":
            addrs = [BASE + syms["shards"] + 16 * i for i in range(4)]
        elif variant == "array-fixed":
            addrs = [BASE + syms["shards_fixed"] + 64 * i for i in range(4)]
        else:
            addrs = [HEAP_LO + 0x2A0 + 32 * i for i in range(4)]
        hot = variant != "array-fixed"
        for i, tid in enumerate(tids):
            emit(
                1, ip_init, PID, addrs[i], 7, 0, STORE_L1HIT
            )  # main writes each quota once
            if hot:
                emit(40 + 5 * i, ip_quota, tid, addrs[i], i, 190 + i, LOAD_HITM)
                emit(
                    30 + 3 * i, ip_bump, tid, addrs[i] + 8, i, 240 + i, LOCKED_LOAD_HITM
                )
                emit(25, ip_bump, tid, addrs[i] + 8, i, 0, STORE_L1HIT)
                emit(9, ip_bump, tid, addrs[i] + 8, i, 0, STORE_L1MISS)
            else:
                emit(2, ip_quota, tid, addrs[i], i, 33, LOAD_L1)
                emit(40, ip_bump, tid, addrs[i] + 8, i, 0, STORE_L1HIT)
    elif variant == "total":
        total = BASE + syms["total"]
        for i, tid in enumerate(tids):
            quota = BASE + syms["private_quota"] + 64 * i
            emit(1, ip_init, PID, quota, 7, 0, STORE_L1HIT)
            emit(2, ip_quota, tid, quota, i, 31, LOAD_L1)
            emit(60 + 4 * i, ip_bump, tid, total, i, 260 + i, LOCKED_LOAD_HITM)
            emit(30, ip_bump, tid, total, i, 0, STORE_L1HIT)
            emit(12, ip_bump, tid, total, i, 0, STORE_L1MISS)
    else:
        sys.exit("unknown variant")
    out.extend(samples)
    sys.stdout.buffer.write(file_image(out, features))


if __name__ == "__main__":
    main()
