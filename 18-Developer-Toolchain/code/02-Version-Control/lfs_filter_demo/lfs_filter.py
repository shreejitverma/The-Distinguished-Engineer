#!/usr/bin/env python3
"""Git long-running filter process (protocol v2) implementing Git LFS clean/smudge."""

import hashlib
import os
import sys

PKT_MAX_PAYLOAD = 65516


def read_pkt():
    """Read a single Git packet-line. Returns payload bytes, b'' for flush, or None on EOF."""
    hdr = sys.stdin.buffer.read(4)
    if not hdr or len(hdr) < 4:
        return None
    length = int(hdr.decode("ascii"), 16)
    if length == 0:
        return b""
    return sys.stdin.buffer.read(length - 4)


def write_pkt(payload):
    """Write a single Git packet-line or flush packet (0000)."""
    if payload is None or payload == b"":
        sys.stdout.buffer.write(b"0000")
    else:
        raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        sys.stdout.buffer.write(f"{len(raw) + 4:04x}".encode("ascii") + raw)
    sys.stdout.buffer.flush()


def write_content_pkts(data):
    """Send arbitrary byte content as chunked packet-lines terminated by a flush packet."""
    offset = 0
    total = len(data)
    if total == 0:
        write_pkt(b"")
        return
    while offset < total:
        chunk = data[offset : offset + PKT_MAX_PAYLOAD]
        write_pkt(chunk)
        offset += len(chunk)
    write_pkt(b"")


def main():
    # 1. Capability Handshake
    welcome = read_pkt()
    if welcome is None:
        sys.exit(0)
    version = read_pkt()
    read_pkt()  # flush packet

    write_pkt(b"git-filter-server\n")
    write_pkt(b"version=2\n")
    write_pkt(b"")

    # Read capabilities offered by Git
    while True:
        cap = read_pkt()
        if cap == b"" or cap is None:
            break

    # Respond with capabilities we support
    write_pkt(b"capability=clean\n")
    write_pkt(b"capability=smudge\n")
    write_pkt(b"")

    # 2. Command processing loop
    while True:
        header_lines = []
        while True:
            pkt = read_pkt()
            if pkt is None:
                sys.exit(0)
            if pkt == b"":
                break
            header_lines.append(pkt.decode("utf-8", errors="replace").strip())

        headers = {}
        for line in header_lines:
            if "=" in line:
                k, v = line.split("=", 1)
                headers[k] = v

        cmd = headers.get("command")
        if not cmd:
            sys.exit(0)

        # Read content packets until flush
        content = bytearray()
        while True:
            pkt = read_pkt()
            if pkt is None:
                sys.exit(0)
            if pkt == b"":
                break
            content.extend(pkt)

        if cmd == "clean":
            raw_bytes = bytes(content)
            sha = hashlib.sha256(raw_bytes).hexdigest()
            size = len(raw_bytes)

            # Store in local Content-Addressable Storage (CAS)
            cas_dir = os.path.join(".git", "lfs", "objects", sha[:2], sha[2:4])
            os.makedirs(cas_dir, exist_ok=True)
            obj_path = os.path.join(cas_dir, sha)
            with open(obj_path, "wb") as f:
                f.write(raw_bytes)

            # Format Git LFS v1 pointer file (keys sorted alphabetically: oid, size)
            pointer = (
                f"version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{sha}\n"
                f"size {size}\n"
            ).encode("utf-8")

            write_pkt(b"status=success\n")
            write_pkt(b"")
            write_content_pkts(pointer)
            write_pkt(b"")  # empty list to keep status=success unchanged

        elif cmd == "smudge":
            text = content.decode("utf-8", errors="replace")
            lines = text.splitlines()
            oid = None
            is_lfs_ptr = False

            if lines and lines[0] == "version https://git-lfs.github.com/spec/v1":
                is_lfs_ptr = True
                for line in lines[1:]:
                    if line.startswith("oid sha256:"):
                        oid = line.split(":", 1)[1].strip()

            if is_lfs_ptr and oid:
                obj_path = os.path.join(".git", "lfs", "objects", oid[:2], oid[2:4], oid)
                if os.path.exists(obj_path):
                    with open(obj_path, "rb") as f:
                        smudged_payload = f.read()
                else:
                    # A pointer whose object is not in the local store is an error, never a silent passthrough.
                    write_pkt(b"status=error\n")
                    write_pkt(b"")
                    continue
            else:
                smudged_payload = bytes(content)

            write_pkt(b"status=success\n")
            write_pkt(b"")
            write_content_pkts(smudged_payload)
            write_pkt(b"")


if __name__ == "__main__":
    main()
