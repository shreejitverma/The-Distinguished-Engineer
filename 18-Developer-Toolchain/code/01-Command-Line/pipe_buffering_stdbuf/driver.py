import fcntl
import os
import select
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def run_probe_buffers():
    probe_bin = os.path.join(SCRIPT_DIR, "probe_buffers")
    res = subprocess.run([probe_bin], capture_output=True, text=True, check=True)
    print(res.stdout.strip())

def run_flush_threshold_test(stdbuf_args=(), phase1_lines=30):
    writer_bin = os.path.join(SCRIPT_DIR, "writer")
    cmd = list(("stdbuf",) + tuple(stdbuf_args) + (writer_bin, str(phase1_lines))) if stdbuf_args else [writer_bin, str(phase1_lines)]

    p_in_r, p_in_w = os.pipe()
    p_out_r, p_out_w = os.pipe()
    p_err_r, p_err_w = os.pipe()

    fcntl.fcntl(p_out_r, fcntl.F_SETFL, os.O_NONBLOCK)

    proc = subprocess.Popen(cmd, stdin=p_in_r, stdout=p_out_w, stderr=p_err_w)
    os.close(p_in_r)
    os.close(p_out_w)
    os.close(p_err_w)

    err_out = b""
    while b"PHASE1_WRITTEN\n" not in err_out:
        chunk = os.read(p_err_r, 1024)
        if not chunk:
            break
        err_out += chunk

    phase1_bytes = 0
    try:
        data = os.read(p_out_r, 65536)
        phase1_bytes = len(data)
    except BlockingIOError:
        phase1_bytes = 0

    os.write(p_in_w, b"A")
    os.close(p_in_w)

    fcntl.fcntl(p_out_r, fcntl.F_SETFL, 0)
    total_bytes = phase1_bytes
    while True:
        chunk = os.read(p_out_r, 4096)
        if not chunk:
            break
        total_bytes += len(chunk)

    proc.wait()
    os.close(p_out_r)
    os.close(p_err_r)
    return phase1_bytes, total_bytes

def proc_state(pid):
    """Return the scheduler state letter from /proc/<pid>/stat ('S' = sleeping in an interruptible wait)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return "?"

def both_sleeping(pids, wait_s):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if all(proc_state(pid) == "S" for pid in pids):
            return True
        time.sleep(0.05)
    return all(proc_state(pid) == "S" for pid in pids)

def run_deadlock_test(client_cmd, env=None, expect_success=False):
    """Run a request/reply exchange between peer_server and a client.

    A reply is awaited for 10 s when the exchange is expected to work, so a slow host cannot turn
    SUCCESS into a false DEADLOCK.
    When no reply is expected, DEADLOCK is reported only after a 1 s window AND when both peers are
    observed asleep (state S) in the kernel, so the verdict rests on observed blocking, not on a timeout alone.
    Anything else is INDETERMINATE.
    """
    server_bin = os.path.join(SCRIPT_DIR, "peer_server")
    c2s_r, c2s_w = os.pipe()
    s2c_r, s2c_w = os.pipe()

    server = subprocess.Popen([server_bin], stdin=c2s_r, stdout=s2c_w)
    client = subprocess.Popen(client_cmd, stdin=s2c_r, stdout=c2s_w, stderr=subprocess.PIPE, env=env)

    os.close(c2s_r)
    os.close(c2s_w)
    os.close(s2c_r)
    os.close(s2c_w)

    rlist, _, _ = select.select([client.stderr], [], [], 10.0 if expect_success else 1.0)
    if rlist:
        msg = client.stderr.read().decode().strip()
        client.wait()
        server.wait()
        return "SUCCESS", msg
    blocked = both_sleeping([client.pid, server.pid], 5.0)
    client.kill()
    server.kill()
    client.wait()
    server.wait()
    if blocked:
        return "DEADLOCK", "blocked waiting for mutual stdio flush"
    return "INDETERMINATE", "peers were not both asleep"

def main():
    run_probe_buffers()

    print("\n2. Stdio flush threshold on pipes (30 lines = 1770 B, total 70 lines = 4130 B):")
    p1_def, tot_def = run_flush_threshold_test()
    print(f"   default block buffering: phase 1 received {p1_def:4d} B / total {tot_def} B (held in stdio)")
    p1_std, tot_std = run_flush_threshold_test(("-oL",))
    print(f"   stdbuf -oL line mode:   phase 1 received {p1_std:4d} B / total {tot_std} B (immediate flush)")

    print("\n   Buffer-size boundary (all 70 lines = 4130 B written before the pause):")
    p1_b, tot_b = run_flush_threshold_test(phase1_lines=70)
    print(f"   default (4096 B buffer): phase 1 received {p1_b:4d} B / total {tot_b} B (full buffer flushed)")
    p1_8k, tot_8k = run_flush_threshold_test(("-o8192",), phase1_lines=70)
    print(f"   stdbuf -o8192 buffer:    phase 1 received {p1_8k:4d} B / total {tot_8k} B (4130 B fits, nothing flushed)")

    print("\n3. Inter-process bidirectional deadlock resolution:")
    client_dyn = os.path.join(SCRIPT_DIR, "peer_client")
    s_def, m_def = run_deadlock_test([client_dyn])
    print(f"   unassisted client:      status={s_def} ({m_def})")
    s_std, m_std = run_deadlock_test(["stdbuf", "-oL", client_dyn], expect_success=True)
    print(f"   stdbuf -oL client:      status={s_std} ({m_std})")

    print("\n4. Interposition boundaries (LD_PRELOAD vs runtime runtimes):")
    client_static = os.path.join(SCRIPT_DIR, "peer_client_static")
    s_stat, m_stat = run_deadlock_test(["stdbuf", "-oL", client_static])
    print(f"   static ELF under stdbuf:      status={s_stat} (kernel bypasses dynamic linker)")

    client_py = os.path.join(SCRIPT_DIR, "peer_client.py")
    s_py_std, m_py_std = run_deadlock_test(["stdbuf", "-oL", "python3", client_py])
    print(f"   Python under stdbuf -oL:      status={s_py_std} (Python runtime bypasses libc FILE*)")

    py_env = dict(os.environ)
    py_env["PYTHONUNBUFFERED"] = "1"
    s_py_unb, m_py_unb = run_deadlock_test(["python3", client_py], env=py_env, expect_success=True)
    print(f"   Python with PYTHONUNBUFFERED: status={s_py_unb} ({m_py_unb})")

if __name__ == "__main__":
    main()
