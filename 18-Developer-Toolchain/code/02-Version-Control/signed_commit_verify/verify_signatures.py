#!/usr/bin/env python3
"""
Verification engine for Git hooks and cryptographic commit signing.
Demonstrates:
  1. Config-defined hooks via the `git hook` builtin (Git 2.55).
  2. Cryptographic commit signing using OpenSSH Ed25519 keys.
  3. Raw git commit object parsing, SSHSIG binary armor decoding, and
     canonical payload reconstruction.
  4. Low-level cryptographic verification with `ssh-keygen -Y`.
  5. Server-side `pre-receive` hook policy enforcement on push batches.
"""

import base64
import os
import re
import struct
import subprocess
import sys
import tempfile

ALICE_PRIV = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACAYJQqg11nMv2+PSyorf3YfsuJFUAGyDqLru5MTIrCyHwAAAJgFI5vbBSOb
2wAAAAtzc2gtZWQyNTUxOQAAACAYJQqg11nMv2+PSyorf3YfsuJFUAGyDqLru5MTIrCyHw
AAAECY/1WSsYcAOzgiDGt0Qkt1JLRi3O2QkpXy5ZgSKANzNBglCqDXWcy/b49LKit/dh+y
4kVQAbIOouu7kxMisLIfAAAAEWFsaWNlQGV4YW1wbGUuY29tAQIDBA==
-----END OPENSSH PRIVATE KEY-----
""".strip() + "\n"

ALICE_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBglCqDXWcy/b49LKit/dh+y4kVQAbIOouu7kxMisLIf alice@example.com"

EVE_PRIV = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACCt/mIUFVqtq1QFdugjCMeNwHj6sf7r+NdsTnmf+PaFlQAAAJjS2dzg0tnc
4AAAAAtzc2gtZWQyNTUxOQAAACCt/mIUFVqtq1QFdugjCMeNwHj6sf7r+NdsTnmf+PaFlQ
AAAECYbUPLvg/52bTiNLCPxaKCUFFfPTRyD/9br28h/VERH63+YhQVWq2rVAV26CMIx43A
ePqx/uv412xOeZ/49oWVAAAAD2V2ZUBleGFtcGxlLmNvbQECAwQFBg==
-----END OPENSSH PRIVATE KEY-----
""".strip() + "\n"

EVE_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK3+YhQVWq2rVAV26CMIx43AePqx/uv412xOeZ/49oWV eve@example.com"


def parse_commit_object(raw_bytes: bytes):
    lines = raw_bytes.split(b"\n")
    headers = []
    payload_lines = []
    sig_lines = []
    in_sig = False
    in_header = True

    for line in lines:
        if in_header:
            if line == b"":
                in_header = False
                payload_lines.append(line)
            elif line.startswith(b"gpgsig "):
                in_sig = True
                headers.append("gpgsig")
                sig_lines.append(line[len(b"gpgsig "):])
            elif in_sig:
                if line.startswith(b" "):
                    sig_lines.append(line[1:])
                else:
                    in_sig = False
                    key = line.split(b" ", 1)[0].decode("ascii", errors="replace")
                    headers.append(key)
                    payload_lines.append(line)
            else:
                key = line.split(b" ", 1)[0].decode("ascii", errors="replace")
                headers.append(key)
                payload_lines.append(line)
        else:
            payload_lines.append(line)

    payload = b"\n".join(payload_lines)
    sig_pem = b"\n".join(sig_lines).decode("ascii") + "\n"
    return headers, payload, sig_pem


def decode_sshsig_armor(sig_pem: str):
    b64_lines = [l.strip() for l in sig_pem.splitlines() if not l.startswith("-----")]
    raw_sig = base64.b64decode("".join(b64_lines))

    magic = raw_sig[:6].decode("ascii")
    version = struct.unpack(">I", raw_sig[6:10])[0]

    idx = 10
    pk_len = struct.unpack(">I", raw_sig[idx:idx + 4])[0]
    idx += 4
    pk_blob = raw_sig[idx:idx + pk_len]
    idx += pk_len

    key_type_len = struct.unpack(">I", pk_blob[:4])[0]
    key_type = pk_blob[4:4 + key_type_len].decode("ascii")

    ns_len = struct.unpack(">I", raw_sig[idx:idx + 4])[0]
    idx += 4
    namespace = raw_sig[idx:idx + ns_len].decode("ascii")
    idx += ns_len

    res_len = struct.unpack(">I", raw_sig[idx:idx + 4])[0]
    idx += 4
    idx += res_len

    hash_len = struct.unpack(">I", raw_sig[idx:idx + 4])[0]
    idx += 4
    hash_alg = raw_sig[idx:idx + hash_len].decode("ascii")

    return {
        "magic": magic,
        "version": version,
        "namespace": namespace,
        "hash_algorithm": hash_alg,
        "key_type": key_type,
    }


FAILURES = []
CHECKS = []


def check(label, ok):
    """Record one machine-checked invariant; any failure makes the lab exit 1."""
    CHECKS.append(label)
    if not ok:
        FAILURES.append(label)
    return ok


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def keygen_verify(allowed, principal, sig_file, data):
    return run(
        ["ssh-keygen", "-Y", "verify", "-n", "git", "-f", allowed, "-I", principal, "-s", sig_file],
        input=data,
    )


def line_with(proc, needle):
    """First output line of proc containing needle, else a visible marker."""
    for line in (proc.stderr + proc.stdout).decode("utf-8", errors="replace").splitlines():
        if needle in line:
            return line.strip()
    return f"(no line containing {needle!r})"


def main():
    print("=== Git Hooks and Cryptographic Signing Verification ===")
    print()

    with tempfile.TemporaryDirectory() as td:
        alice_key = os.path.join(td, "id_alice")
        with open(alice_key, "w") as f:
            f.write(ALICE_PRIV)
        os.chmod(alice_key, 0o600)
        with open(alice_key + ".pub", "w") as f:
            f.write(ALICE_PUB + "\n")

        eve_key = os.path.join(td, "id_eve")
        with open(eve_key, "w") as f:
            f.write(EVE_PRIV)
        os.chmod(eve_key, 0o600)
        with open(eve_key + ".pub", "w") as f:
            f.write(EVE_PUB + "\n")

        # Alice only: the trust store used by cases 3.1-3.3 and pushes 1-3.
        allowed_signers = os.path.join(td, "allowed_signers")
        with open(allowed_signers, "w") as f:
            f.write(f"alice@example.com {ALICE_PUB}\n")
        # Alice and Eve: Eve's key is trusted, but only for her own principal.
        allowed_both = os.path.join(td, "allowed_signers_both")
        with open(allowed_both, "w") as f:
            f.write(f"alice@example.com {ALICE_PUB}\neve@example.com {EVE_PUB}\n")

        env_alice = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "Alice Engineer",
            "GIT_AUTHOR_EMAIL": "alice@example.com",
            "GIT_COMMITTER_NAME": "Alice Engineer",
            "GIT_COMMITTER_EMAIL": "alice@example.com",
            "GIT_AUTHOR_DATE": "2026-11-08T12:00:00Z",
            "GIT_COMMITTER_DATE": "2026-11-08T12:00:00Z",
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

        def git(*args, env=env_alice, repo=None, check_rc=True):
            cmd = ["git"] + (["-C", repo] if repo else []) + list(args)
            p = run(cmd, env=env)
            if check_rc and p.returncode != 0:
                raise RuntimeError(f"{' '.join(cmd)} failed: {p.stderr.decode()}")
            return p

        # [1] Config-Defined Hook Architecture
        client_repo = os.path.join(td, "client")
        os.makedirs(client_repo)
        git("init", "-b", "main", "-q", repo=client_repo)

        marker = os.path.join(td, "hook-ran")
        hook_bin = os.path.join(td, "check_whitespace.sh")
        with open(hook_bin, "w") as f:
            f.write(f"#!/bin/sh\necho ran > '{marker}'\nexit 0\n")
        os.chmod(hook_bin, 0o755)
        deny_bin = os.path.join(td, "deny.sh")
        with open(deny_bin, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(deny_bin, 0o755)

        git("config", "hook.check-whitespace.command", hook_bin, repo=client_repo)
        git("config", "--add", "hook.check-whitespace.event", "pre-commit", repo=client_repo)

        events = git("config", "--get-all", "hook.check-whitespace.event", repo=client_repo).stdout.decode().split()
        hook_list = git("hook", "list", "pre-commit", repo=client_repo).stdout.decode().strip()
        hook_run_rc = git("hook", "run", "pre-commit", repo=client_repo, check_rc=False).returncode
        check("hook list names check-whitespace", hook_list == "check-whitespace")
        check("hook run exits 0", hook_run_rc == 0)
        check("hook body actually ran", os.path.exists(marker))

        # A second config-defined hook that fails must make `git hook run` fail.
        git("config", "hook.deny.command", deny_bin, repo=client_repo)
        git("config", "hook.deny.event", "pre-commit", repo=client_repo)
        deny_rc = git("hook", "run", "pre-commit", repo=client_repo, check_rc=False).returncode
        check("failing hook makes hook run non-zero", deny_rc != 0)
        git("config", "--unset", "hook.deny.event", repo=client_repo)
        after_unset = git("hook", "list", "pre-commit", repo=client_repo).stdout.decode().split()
        check("unsetting the event unregisters the hook", after_unset == ["check-whitespace"])

        print("[1] Config-Defined Hook Architecture (git hook)")
        print(f"  Registered hook: check-whitespace (event: {', '.join(events)})")
        print(f"  git hook list pre-commit: {hook_list}")
        print(f"  git hook run pre-commit: exit {hook_run_rc}")
        print(f"  hook body ran: {'yes' if os.path.exists(marker) else 'no'}")
        print(f"  second hook that exits 1: git hook run pre-commit: exit {deny_rc}")
        print()

        # [2] Commit Object & SSHSIG Armor Decoding
        git("config", "user.signingKey", alice_key, repo=client_repo)
        git("config", "gpg.format", "ssh", repo=client_repo)
        git("config", "gpg.ssh.allowedSignersFile", allowed_signers, repo=client_repo)

        with open(os.path.join(client_repo, "main.c"), "w") as f:
            f.write("int main(void) { return 0; }\n")
        git("add", "main.c", repo=client_repo)
        git("commit", "-S", "-m", "feat: core runtime initial commit", "-q", repo=client_repo)

        alice_commit_oid = git("rev-parse", "HEAD", repo=client_repo).stdout.decode().strip()
        raw_commit = git("cat-file", "commit", "HEAD", repo=client_repo).stdout

        headers, payload, sig_pem = parse_commit_object(raw_commit)
        sshsig_info = decode_sshsig_armor(sig_pem)
        # The payload is the object with the gpgsig header removed: its length must be
        # the raw object length minus the bytes of the gpgsig header and continuations.
        gpgsig_block = b"gpgsig " + b"\n ".join(sig_pem.rstrip("\n").encode().split(b"\n")) + b"\n"
        check("payload == raw object minus gpgsig block", payload == raw_commit.replace(gpgsig_block, b"", 1))
        check("SSHSIG magic", sshsig_info["magic"] == "SSHSIG")
        check("SSHSIG namespace is git", sshsig_info["namespace"] == "git")
        check("commit headers", headers == ["tree", "author", "committer", "gpgsig"])

        print("[2] Raw Commit Object & SSHSIG Armor Decoding")
        print(f"  Commit OID: {alice_commit_oid}")
        print(f"  Commit header fields: {', '.join(headers)}")
        print("  SSHSIG binary structure:")
        print(f"    magic: {sshsig_info['magic']}")
        print(f"    version: {sshsig_info['version']}")
        print(f"    namespace: {sshsig_info['namespace']}")
        print(f"    hash_algorithm: {sshsig_info['hash_algorithm']}")
        print(f"    key_type: {sshsig_info['key_type']}")
        print(f"  Canonical payload size: {len(payload)} bytes")
        print()

        # [3] Low-Level Cryptographic Verification Engine
        sig_file = os.path.join(td, "alice_commit.sig")
        with open(sig_file, "w") as f:
            f.write(sig_pem)

        alice_fp = run(["ssh-keygen", "-lf", alice_key + ".pub"]).stdout.decode().split()[1]

        # 3.1 Valid signature
        p_valid = keygen_verify(allowed_signers, "alice@example.com", sig_file, payload)
        good = re.match(r'Good "git" signature for (\S+) with ED25519 key (SHA256:\S+)$', p_valid.stdout.decode().strip())
        check("3.1 verify exits 0", p_valid.returncode == 0)
        check("3.1 Good line parsed", good is not None)
        matched_principal, matched_fp = good.groups() if good else ("(none)", "(none)")
        check("3.1 principal is alice", matched_principal == "alice@example.com")
        check("3.1 fingerprint equals ssh-keygen -lf of Alice's public key", matched_fp == alice_fp)

        # 3.2 Tampered payload
        tampered_payload = payload.replace(b"feat: core runtime", b"feat: malicious inject")
        check("3.2 tamper changed the payload", tampered_payload != payload)
        p_tamper = keygen_verify(allowed_signers, "alice@example.com", sig_file, tampered_payload)
        check("3.2 tampered verify fails", p_tamper.returncode != 0)
        check("3.2 reason is incorrect signature", b"incorrect signature" in p_tamper.stderr + p_tamper.stdout)

        # 3.3 Untrusted signer: a real signature made by Eve's key, not in allowed_signers
        git("checkout", "-b", "eve-branch", "-q", repo=client_repo)
        env_eve = dict(env_alice, GIT_AUTHOR_NAME="Eve Hacker", GIT_AUTHOR_EMAIL="eve@example.com",
                       GIT_COMMITTER_NAME="Eve Hacker", GIT_COMMITTER_EMAIL="eve@example.com")
        git("config", "user.signingKey", eve_key, repo=client_repo)
        with open(os.path.join(client_repo, "eve.txt"), "w") as f:
            f.write("eve code\n")
        git("add", "eve.txt", repo=client_repo)
        git("commit", "-S", "-m", "eve commit", "-q", repo=client_repo, env=env_eve)
        eve_raw = git("cat-file", "commit", "HEAD", repo=client_repo).stdout
        _, eve_payload, eve_sig_pem = parse_commit_object(eve_raw)
        eve_sig_file = os.path.join(td, "eve.sig")
        with open(eve_sig_file, "w") as f:
            f.write(eve_sig_pem)
        p_eve = keygen_verify(allowed_signers, "eve@example.com", eve_sig_file, eve_payload)
        check("3.3 untrusted Eve verify fails", p_eve.returncode != 0)
        fp_alice_sig = run(["ssh-keygen", "-Y", "find-principals", "-f", allowed_signers, "-s", sig_file])
        fp_eve_sig = run(["ssh-keygen", "-Y", "find-principals", "-f", allowed_signers, "-s", eve_sig_file])
        check("3.3 find-principals resolves Alice's signature", fp_alice_sig.stdout.decode().split() == ["alice@example.com"])
        check("3.3 find-principals finds nobody for Eve's signature", fp_eve_sig.returncode != 0 and fp_eve_sig.stdout.strip() == b"")

        # 3.4 Spoofing with a TRUSTED key: Eve is in the trust store, committer claims Alice
        git("checkout", "-b", "spoof-branch", alice_commit_oid, "-q", repo=client_repo)
        env_spoof = dict(env_alice)  # author and committer are Alice
        with open(os.path.join(client_repo, "spoof.txt"), "w") as f:
            f.write("spoof\n")
        git("add", "spoof.txt", repo=client_repo)
        git("commit", "-S", "-m", "spoof commit", "-q", repo=client_repo, env=env_spoof)
        spoof_raw = git("cat-file", "commit", "HEAD", repo=client_repo).stdout
        _, spoof_payload, spoof_sig_pem = parse_commit_object(spoof_raw)
        spoof_sig_file = os.path.join(td, "spoof.sig")
        with open(spoof_sig_file, "w") as f:
            f.write(spoof_sig_pem)
        p_spoof_alice = keygen_verify(allowed_both, "alice@example.com", spoof_sig_file, spoof_payload)
        p_spoof_eve = keygen_verify(allowed_both, "eve@example.com", spoof_sig_file, spoof_payload)
        check("3.4 verify as alice fails", p_spoof_alice.returncode != 0)
        check("3.4 verify as eve succeeds (key is trusted)", p_spoof_eve.returncode == 0)
        git("config", "gpg.ssh.allowedSignersFile", allowed_both, repo=client_repo)
        fields = git("log", "-1", "--format=%G?|%GS|%ce", "HEAD", repo=client_repo).stdout.decode().strip().split("|")
        spoof_status, spoof_signer, spoof_committer = fields
        check("3.4 git reports G", spoof_status == "G")
        check("3.4 git reports signer eve", spoof_signer == "eve@example.com")
        check("3.4 git reports committer alice", spoof_committer == "alice@example.com")
        git("config", "gpg.ssh.allowedSignersFile", allowed_signers, repo=client_repo)

        print("[3] Low-Level Cryptographic Verification Engine (ssh-keygen -Y)")
        print(f"  Case 3.1: Valid Alice signature against allowed_signers: {'PASS' if p_valid.returncode == 0 else 'FAIL'}")
        print(f"    matched principal: {matched_principal}")
        print(f"    key fingerprint: {matched_fp}")
        print(f"  Case 3.2: Tampered payload verification: {'PASS' if p_tamper.returncode == 0 else 'FAIL'}")
        print(f"    ssh-keygen: {line_with(p_tamper, "incorrect signature")}")
        print(f"  Case 3.3: Untrusted Eve signature verified as eve@example.com: {'PASS' if p_eve.returncode == 0 else 'FAIL'}")
        print(f"    find-principals for Alice's signature: {fp_alice_sig.stdout.decode().strip() or '(none)'}")
        print(f"    find-principals for Eve's signature: {fp_eve_sig.stdout.decode().strip() or '(none)'} (exit {fp_eve_sig.returncode})")
        print(f"  Case 3.4: Trusted Eve key, committer claims alice, verified as alice@example.com: {'PASS' if p_spoof_alice.returncode == 0 else 'FAIL'}")
        print(f"    verified as eve@example.com: {'PASS' if p_spoof_eve.returncode == 0 else 'FAIL'}")
        print(f"    git log %G? %GS %ce: {spoof_status} {spoof_signer} {spoof_committer}")
        print(f"    signer == committer: {'yes' if spoof_signer == spoof_committer else 'no'}")
        print()

        # [4] Server-Side Hook Policy Enforcement
        bare_repo = os.path.join(td, "bare.git")
        git("init", "--bare", "-b", "main", "-q", bare_repo)
        git("config", "gpg.ssh.allowedSignersFile", allowed_signers, repo=bare_repo)

        hook_sh = os.path.join(bare_repo, "hooks", "pre-receive")
        with open(hook_sh, "w") as f:
            f.write("""#!/bin/sh
set -e
while read -r old_oid new_oid ref; do
    case "$new_oid" in *[!0]*) ;; *) continue ;; esac   # all zeroes: ref deletion
    case "$old_oid" in
        *[!0]*) revs=$(git rev-list "$old_oid..$new_oid") ;;
        *) revs=$(git rev-list "$new_oid") ;;
    esac
    for rev in $revs; do
        status=$(git log -1 --format='%G?' "$rev")
        signer=$(git log -1 --format='%GS' "$rev")
        committer=$(git log -1 --format='%ce' "$rev")
        if [ "$status" != "G" ]; then
            echo "POLICY REJECT: status '$status' is not valid (G)" >&2
            exit 1
        fi
        if [ "$signer" != "$committer" ]; then
            echo "POLICY REJECT: signer '$signer' != committer '$committer'" >&2
            exit 1
        fi
    done
done
echo "POLICY OK: all commits verified" >&2
""")
        os.chmod(hook_sh, 0o755)

        git("remote", "add", "origin", bare_repo, repo=client_repo)

        def bare_ref(name):
            p = git("rev-parse", "--verify", "-q", name, repo=bare_repo, check_rc=False)
            return p.stdout.decode().strip() if p.returncode == 0 else "(missing)"

        def push(branch, env=env_alice):
            p = git("push", "origin", branch, repo=client_repo, env=env, check_rc=False)
            reject = re.search(r"POLICY REJECT: (.*)", p.stderr.decode())
            return p.returncode, (reject.group(1).strip() if reject else None), "POLICY OK" in p.stderr.decode()

        # Push 1: Alice's valid signed commit
        git("checkout", "main", "-q", repo=client_repo)
        git("config", "user.signingKey", alice_key, repo=client_repo)
        rc1, why1, ok1 = push("main")
        check("push 1 accepted with POLICY OK", rc1 == 0 and ok1)
        check("push 1 updated the bare ref", bare_ref("main") == alice_commit_oid)

        # Push 2: unsigned commit
        with open(os.path.join(client_repo, "unsigned.txt"), "w") as f:
            f.write("unsigned\n")
        git("add", "unsigned.txt", repo=client_repo)
        git("commit", "--no-gpg-sign", "-m", "unsigned commit", "-q", repo=client_repo)
        rc2, why2, _ = push("main")
        check("push 2 rejected with status N", rc2 != 0 and why2 == "status 'N' is not valid (G)")
        check("push 2 left the bare ref unchanged", bare_ref("main") == alice_commit_oid)

        # Push 3: Eve's commit, key absent from the server trust store
        rc3, why3, _ = push("eve-branch")
        check("push 3 rejected with status U", rc3 != 0 and why3 == "status 'U' is not valid (G)")
        check("push 3 created no ref", bare_ref("eve-branch") == "(missing)")

        # Push 4: the server now trusts Eve's key, but the commit claims Alice as committer
        git("config", "gpg.ssh.allowedSignersFile", allowed_both, repo=bare_repo)
        rc4, why4, _ = push("spoof-branch")
        check("push 4 rejected on signer != committer", rc4 != 0 and why4 == "signer 'eve@example.com' != committer 'alice@example.com'")
        # Control: the same trusted Eve commit with Eve as committer is accepted.
        rc5, why5, ok5 = push("eve-branch")
        check("push 5 (trusted Eve, honest committer) accepted", rc5 == 0 and ok5)

        def verdict(rc, why):
            return f"REJECTED ({why})" if rc != 0 else "ACCEPTED"

        print("[4] Server-Side Hook Policy Enforcement (pre-receive)")
        print(f"  Push 1 (Alice valid signed commit): {'ACCEPTED (exit 0)' if rc1 == 0 else verdict(rc1, why1)}")
        print(f"  Push 2 (Unsigned commit): {verdict(rc2, why2)}")
        print(f"  Push 3 (Eve commit, key not in server allowed_signers): {verdict(rc3, why3)}")
        print(f"  Push 4 (Eve key now trusted, committer claims Alice): {verdict(rc4, why4)}")
        print(f"  Push 5 (Eve key trusted, honest committer): {'ACCEPTED (exit 0)' if rc5 == 0 else verdict(rc5, why5)}")
        print()

    if FAILURES:
        print(f"Result: FAIL ({len(FAILURES)} of {len(CHECKS)} checks failed)")
        for label in FAILURES:
            print(f"  failed: {label}")
        sys.exit(1)
    print(f"Result: PASS ({len(CHECKS)} checks)")


if __name__ == "__main__":
    main()
