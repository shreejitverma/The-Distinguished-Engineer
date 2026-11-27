#!/usr/bin/env python3
"""Drill 3: Compromised Transitive Dependency Exfiltration Probe.

Simulates a malicious build script (build.rs / setup.py) executing inside CI.
Demonstrates credential harvesting from process environment and egress connection
attempts, followed by network isolation and secret-scoping remediation.
"""

import os
import sys

def simulate_unhardened_build_runner():
    print("[DRILL 3] --- SIMULATING UNHARDENED CI BUILD RUNNER ---")
    # Tainted environment containing sensitive credentials
    fake_ci_env = {
        "PATH": "/usr/local/bin:/usr/bin",
        "CI": "true",
        "BUILD_ID": "ci-run-84920",
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "GITHUB_TOKEN": "ghp_EXAMPLE_TOKEN_NEVER_VALID_FAKE_CRED_001",
    }
    
    # Malicious transitive dependency payload
    harvested = {}
    for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"]:
        if key in fake_ci_env:
            harvested[key] = fake_ci_env[key]
    
    print(f"[DRILL 3] MALICIOUS_PAYLOAD: Harvested {len(harvested)} secrets from environment:")
    for k, v in harvested.items():
        print(f"  - {k}: {v[:8]}...")

    # Simulated network egress attempt
    target_ip = "198.51.100.42"
    target_port = 443
    print(f"[DRILL 3] MALICIOUS_PAYLOAD: Attempting outbound exfiltration to https://{target_ip}:{target_port}/collect...")
    print("[DRILL 3] VULNERABILITY_CONFIRMED: Runner permitted outbound network socket during build step!")

def simulate_hardened_sandboxed_runner():
    print("\n[DRILL 3] --- SIMULATING HARDENED SANDBOXED RUNNER (REMEDIATION) ---")
    # Remediation 1: Credential scoping (Build steps run without production credentials)
    hardened_build_env = {
        "PATH": "/usr/local/bin:/usr/bin",
        "CI": "true",
        "BUILD_ID": "ci-run-84920",
    }
    
    # Build step attempts harvesting
    harvested = {k: v for k, v in hardened_build_env.items() if "KEY" in k or "TOKEN" in k}
    print(f"[DRILL 3] HARVEST_CHECK: Found {len(harvested)} credentials in build environment (Hermetic Secret Scoping).")

    # Remediation 2: Network namespace isolation (unshare -n / Docker internal: true)
    network_sandbox_active = True
    target_ip = "198.51.100.42"
    target_port = 443
    
    if network_sandbox_active:
        print(f"[DRILL 3] EGRESS_FILTER: Connection to {target_ip}:{target_port} BLOCKED by network sandbox (ENETUNREACH / EPERM).")
        print("[DRILL 3] AUDIT_EVENT: Alert dispatched to SOC: unauthorized egress attempt trapped in build sandbox.")

    # Remediation 3: Lockfile integrity & vendor verification
    print("[DRILL 3] SUPPLY_CHAIN_POLICY: Enforcing cargo --locked and pip --require-hashes.")
    print("[DRILL 3] SUCCESS: Drill 3 exfiltration containment verified.")

def main():
    simulate_unhardened_build_runner()
    simulate_hardened_sandboxed_runner()

if __name__ == "__main__":
    main()
