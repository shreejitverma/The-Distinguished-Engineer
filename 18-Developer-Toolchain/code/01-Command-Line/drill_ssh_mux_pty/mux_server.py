#!/usr/bin/env python3
"""OpenSSH connection multiplexing (PROTOCOL.mux) mock master.

Speaks enough of the wire format for the real `ssh -O check`, `-O exit`, and `-O stop` clients:
a hello exchange, then one request, answered with MUX_S_ALIVE or MUX_S_OK.

Modes:
  live     answer requests like a healthy master.
  stopped  bind and listen, then stop the whole process with SIGSTOP (state T), the way a
           suspended or frozen master looks: connect() succeeds into the backlog, nothing is read.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import struct
import time

# PROTOCOL.mux constants (client requests 0x1000000N, server replies 0x8000000N).
MUX_MSG_HELLO = 0x00000001
MUX_C_ALIVE_CHECK = 0x10000004
MUX_C_TERMINATE = 0x10000005
MUX_C_STOP_LISTENING = 0x10000009

MUX_S_OK = 0x80000001
MUX_S_FAILURE = 0x80000003
MUX_S_ALIVE = 0x80000005

MUX_PROTO_VERSION = 4

ACCEPT_POLL_S = 0.2


def recv_exact(conn: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or return what arrived before EOF."""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def read_packet(conn: socket.socket) -> tuple[int, bytes] | None:
    """Return (message type, body after the type) or None on a short read."""
    head = recv_exact(conn, 4)
    if len(head) < 4:
        return None
    (length,) = struct.unpack(">I", head)
    body = recv_exact(conn, length)
    if len(body) < length or length < 4:
        return None
    (msg_type,) = struct.unpack(">I", body[:4])
    return msg_type, body[4:]


def handle_client(conn: socket.socket) -> str:
    """Serve one client. Return "keep", "stop" (stop listening), or "exit" (terminate)."""
    pkt = read_packet(conn)
    if pkt is None or pkt[0] != MUX_MSG_HELLO:
        return "keep"
    conn.sendall(struct.pack(">III", 8, MUX_MSG_HELLO, MUX_PROTO_VERSION))

    pkt = read_packet(conn)
    if pkt is None:
        return "keep"
    req_type, req_body = pkt
    req_id = struct.unpack(">I", req_body[:4])[0] if len(req_body) >= 4 else 0

    if req_type == MUX_C_ALIVE_CHECK:
        conn.sendall(struct.pack(">IIII", 12, MUX_S_ALIVE, req_id, os.getpid()))
        return "keep"
    if req_type in (MUX_C_TERMINATE, MUX_C_STOP_LISTENING):
        conn.sendall(struct.pack(">III", 8, MUX_S_OK, req_id))
        return "exit" if req_type == MUX_C_TERMINATE else "stop"
    msg = b"Unsupported request"
    conn.sendall(struct.pack(">IIII", 12 + len(msg), MUX_S_FAILURE, req_id, len(msg)) + msg)
    return "keep"


terminated = False


def on_term(_signum: int, _frame: object) -> None:
    global terminated
    terminated = True


def serve(srv: socket.socket, sock_path: str) -> None:
    """Accept until terminated; after a stop request keep the process alive without a listener."""
    listening = True
    while not terminated:
        if not listening:
            time.sleep(ACCEPT_POLL_S)
            continue
        ready, _, _ = select.select([srv], [], [], ACCEPT_POLL_S)
        if not ready:
            continue
        conn, _ = srv.accept()
        try:
            action = handle_client(conn)
        finally:
            conn.close()
        if action == "exit":
            return
        if action == "stop":
            # Like client_stop_mux on the master side: drop the listener and unlink the path,
            # while the process (and its sessions) keeps running.
            srv.close()
            if os.path.exists(sock_path):
                os.unlink(sock_path)
            listening = False


def main() -> None:
    parser = argparse.ArgumentParser(description="PROTOCOL.mux mock master")
    parser.add_argument("--socket", required=True, help="UNIX domain socket path")
    parser.add_argument("--mode", choices=["live", "stopped"], default="live")
    parser.add_argument("--pid-file", required=True, help="written after listen() succeeds")
    args = parser.parse_args()

    # The real master catches SIGTERM, so a signal sent while it is stopped stays pending.
    signal.signal(signal.SIGTERM, on_term)
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.socket)
    srv.listen(5)
    with open(args.pid_file, "w") as f:
        f.write(str(os.getpid()))

    if args.mode == "stopped":
        os.kill(os.getpid(), signal.SIGSTOP)
        # Only reached after SIGCONT; behave like a recovered master.

    try:
        serve(srv, args.socket)
    finally:
        srv.close()
        if os.path.exists(args.socket):
            os.unlink(args.socket)


if __name__ == "__main__":
    main()
