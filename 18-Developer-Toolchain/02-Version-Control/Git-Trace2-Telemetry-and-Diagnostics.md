---
type: concept
track: [sde, distinguished, low-latency, quant-dev]
level:
status: solid
last_reviewed:
sources:
  - "Jeff Hostetler and Git Contributors, api-trace2 - Trace2 API, Git Reference Manual, Git 2.55.0, 2026, https://git-scm.com/docs/api-trace2"
  - "Git Contributors, trace2.h and trace2.c, git.git source at v2.55.0, 2026, https://github.com/git/git/blob/v2.55.0/trace2.c"
  - "Git Contributors, trace2 target implementations (tr2_tgt_event.c, tr2_tgt_perf.c, tr2_tgt_normal.c, tr2_dst.c, tr2_sid.c), git.git source at v2.55.0, 2026, https://github.com/git/git/tree/v2.55.0/trace2"
  - "Git Contributors, git-config - Get and set repository or global options, Git Reference Manual, Git 2.55.0, 2026, https://git-scm.com/docs/git-config"
  - "Victoria Dye and Git Contributors, git-diagnose - Generate a zip archive of diagnostic information, Git Reference Manual, Git 2.55.0, 2026, https://git-scm.com/docs/git-diagnose"
  - "Johannes Schindelin, trace2: redact passwords from https:// URLs by default (commit b7d49ac1ec, first released in Git 2.43.1 and 2.44.0), git.git, 2023, https://github.com/git/git/commit/b7d49ac1ec"
  - "Victoria Dye, builtin/diagnose.c: create git diagnose builtin, Git 2.38.0 Release, 2022, https://lore.kernel.org/git/"
  - "Git Contributors, trace2-target-values.adoc, git.git source at v2.55.0, 2026, https://github.com/git/git/blob/v2.55.0/Documentation/trace2-target-values.adoc"
  - "Git Contributors, trace2/tr2_tgt.h, tr2_tls.h, tr2_sysenv.c, tr2_cmd_name.c, git.git source at v2.55.0, 2026, https://github.com/git/git/tree/v2.55.0/trace2"
  - "Git Contributors, read_very_early_config() in config.c, git.git source at v2.55.0, 2026, https://github.com/git/git/blob/v2.55.0/config.c"
  - "Git Contributors, diagnose.c and compat/disk.h, git.git source at v2.55.0, 2026, https://github.com/git/git/blob/v2.55.0/diagnose.c"
  - "Git Contributors, git-config gc.auto and gc.autoPackLimit, Git Reference Manual, Git 2.55.0, 2026, https://git-scm.com/docs/git-config#Documentation/git-config.txt-gcauto"
  - "Git Contributors, git-config transfer.advertiseSID, Git Reference Manual, Git 2.55.0, 2026, https://git-scm.com/docs/git-config#Documentation/git-config.txt-transferadvertiseSID"
  - "Josh Steadmon, serve: advertise session ID in v2 capabilities (commit 6b5b6e422e, Git 2.30.0), git.git, 2020, https://github.com/git/git/commit/6b5b6e422e"
  - "Git Contributors, Git 2.38.0 Release Notes, git.git, 2022, https://github.com/git/git/blob/v2.55.0/Documentation/RelNotes/2.38.0.adoc"
---

# Git Trace2 Telemetry and Diagnostics

> [!summary] Claim:
> Git Trace2 replaces ad-hoc printf logging with a multi-target, structured telemetry engine that streams process lifecycles, child executions, stopwatch regions, and configuration parameters across thread-safe VTable sinks.
> By propagating session identifiers and command names across process boundaries via environment variables, Trace2 reconstructs complete distributed invocation trees across hooks, background daemons, and submodules.
> Sinks write JSON records to files, file descriptors, or Unix domain sockets; a missing socket fails fast and silently, but writes are blocking, so a collector that stops reading can stall Git, while Trace2 reads only system and global config and the environment, never repository-local config or `-c`.
> Companion diagnostic utilities package filesystem geometry, packfile distribution, and loose object counts into self-contained diagnostic bundles for fleet triage.

```mermaid
flowchart TD
    subgraph PROCESS["Git Process Execution"]
        MAIN["main() / common-init.c<br/>trace2_initialize()"]
        CMD["Command Execution<br/>trace2_cmd_name()<br/>trace2_cmd_mode()"]
        REG["Region Enter / Leave<br/>trace2_region_enter()<br/>trace2_region_leave()"]
        CHILD["Child Spawn<br/>run-command.c<br/>trace2_child_start()"]
        TERM["Shutdown / atexit<br/>trace2_cmd_exit()<br/>pfn_atexit / pfn_term"]
        MAIN --> CMD --> REG --> CHILD --> TERM
    end

    subgraph TLS["Per-Thread State (tr2_tls.c)"]
        TID["Thread ID & Name<br/>'main' / 'th01:preload'"]
        RSTACK["Region Timer Stack<br/>category, label, enter_time"]
        TMR["Stopwatch Timers<br/>intervals, min, max, total"]
    end

    subgraph CORE["Trace2 Core Engine (trace2.c)"]
        VTABLE{"Target VTable Dispatch<br/>(struct tr2_tgt)"}
        RED["Redaction Filter<br/>GIT_TRACE2_REDACT=1<br/>mask http(s) URL passwords"]
        SID["SID Generator (tr2_sid.c)<br/>Base: time-host-pid<br/>Inherited: parent_sid/child_sid"]
    end

    subgraph TARGETS["Target Formatters"]
        NORM["Normal Target<br/>tr2_tgt_normal.c<br/>human-readable printf"]
        PERF["Perf Target<br/>tr2_tgt_perf.c<br/>columnar d0-dN, t_abs, t_rel"]
        EVENT["Event Target<br/>tr2_tgt_event.c<br/>JSON-lines schema v4"]
    end

    subgraph SINKS["I/O Sinks (tr2_dst.c)"]
        STDERR["File Descriptor<br/>1=stderr, [2-9]=open fd"]
        FILE["Regular File / Dir<br/>append mode / maxFiles check<br/>sentinel: git-trace2-discard"]
        SOCKET["Unix Domain Socket<br/>af_unix:stream / dgram<br/>connect fails fast, target disabled"]
    end

    REG -.-> TLS
    CMD --> CORE
    REG --> CORE
    CHILD --> CORE
    CORE --> RED --> VTABLE
    SID -.-> VTABLE
    VTABLE --> NORM
    VTABLE --> PERF
    VTABLE --> EVENT
    NORM --> SINKS
    PERF --> SINKS
    EVENT --> SINKS
    SOCKET --> DAEMON["Observability Agent<br/>OpenTelemetry / Collector"]
```

## The Mechanism

Tracing in early versions of Git relied on `GIT_TRACE` and `GIT_TRACE_PERFORMANCE`.
These mechanisms wrote unstructured text lines directly to standard error or flat files using variable `printf()` calls.
Unstructured text created three fundamental bottlenecks for enterprise engineering teams.
First, parent and child processes had no shared correlation key, making it impossible to attribute child process latency (such as hooks, credential helpers, or filter drivers) back to the parent command.
Second, parsing arbitrary strings across varying localizations broke automated ingestion pipelines.
Third, concurrent threads in multi-threaded Git operations (such as `index-pack` or index preload) corrupted text output when writing to shared streams.
In Git 2.22.0 (June 2019; `trace2.c` is absent at tag v2.21.0 and present at v2.22.0), Jeff Hostetler of Microsoft introduced the Trace2 telemetry infrastructure to solve these limitations.
Trace2 provides a high-performance, structured, thread-safe telemetry pipeline embedded directly inside the Git runtime.

### 1. Architecture and Target Dispatch

Trace2 is designed around a decoupled producer-consumer architecture.
The public API is defined in `trace2.h` and implemented in `trace2.c`.
Public API names such as `trace2_region_enter()` are macros that only append `__FILE__` and `__LINE__` and call the matching `_fl` function; arguments are always evaluated, and each `_fl` function returns early when Trace2 is not enabled (`trace2.h` at v2.55.0).
When enabled, events pass into private dispatch routines in `trace2/` prefixed with `tr2_`.
Target dispatch relies on a virtual method table defined by `struct tr2_tgt` in `trace2/tr2_tgt.h`.

```c
/* trace2/tr2_tgt.h at v2.55.0; alignment condensed */
struct tr2_tgt {
    struct tr2_dst *pdst;
    tr2_tgt_init_t *pfn_init;
    tr2_tgt_term_t *pfn_term;
    tr2_tgt_evt_version_fl_t *pfn_version_fl;
    tr2_tgt_evt_start_fl_t *pfn_start_fl;
    tr2_tgt_evt_exit_fl_t *pfn_exit_fl;
    tr2_tgt_evt_signal_t *pfn_signal;
    tr2_tgt_evt_atexit_t *pfn_atexit;
    tr2_tgt_evt_error_va_fl_t *pfn_error_va_fl;
    tr2_tgt_evt_command_path_fl_t *pfn_command_path_fl;
    tr2_tgt_evt_command_ancestry_fl_t *pfn_command_ancestry_fl;
    tr2_tgt_evt_command_name_fl_t *pfn_command_name_fl;
    tr2_tgt_evt_command_mode_fl_t *pfn_command_mode_fl;
    tr2_tgt_evt_alias_fl_t *pfn_alias_fl;
    tr2_tgt_evt_child_start_fl_t *pfn_child_start_fl;
    tr2_tgt_evt_child_exit_fl_t *pfn_child_exit_fl;
    tr2_tgt_evt_child_ready_fl_t *pfn_child_ready_fl;
    tr2_tgt_evt_thread_start_fl_t *pfn_thread_start_fl;
    tr2_tgt_evt_thread_exit_fl_t *pfn_thread_exit_fl;
    tr2_tgt_evt_exec_fl_t *pfn_exec_fl;
    tr2_tgt_evt_exec_result_fl_t *pfn_exec_result_fl;
    tr2_tgt_evt_param_fl_t *pfn_param_fl;
    tr2_tgt_evt_repo_fl_t *pfn_repo_fl;
    tr2_tgt_evt_region_enter_printf_va_fl_t *pfn_region_enter_printf_va_fl;
    tr2_tgt_evt_region_leave_printf_va_fl_t *pfn_region_leave_printf_va_fl;
    tr2_tgt_evt_data_fl_t *pfn_data_fl;
    tr2_tgt_evt_data_json_fl_t *pfn_data_json_fl;
    tr2_tgt_evt_printf_va_fl_t *pfn_printf_va_fl;
    tr2_tgt_evt_timer_t *pfn_timer;
    tr2_tgt_evt_counter_t *pfn_counter;
};
```

Git registers three built-in target implementations at compile time:
1. `tr2_tgt_normal.c`: Emits classic human-readable lines modeled after `GIT_TRACE`.
2. `tr2_tgt_perf.c`: Emits column-aligned, pipe-delimited records including process depth (`d0` through `dN`), thread identifiers, absolute timestamps (`t_abs`), and region durations (`t_rel`).
3. `tr2_tgt_event.c`: Emits single-line JSON objects (format version `"evt":"4"`, `TR2_EVENT_VERSION` in `tr2_tgt_event.c`, as of Git 2.55.0) built with `json-writer.c`, suitable for machine ingestion and stream analytics.

An enabled target transforms abstract event calls into byte buffers and writes them to its configured destination sink (`struct tr2_dst`).

### 2. Transport Sinks and Fail-Fast Socket Delivery

Destination sinks manage the operating system handles used for trace output.
A target destination is configured via environment variables (`GIT_TRACE2`, `GIT_TRACE2_PERF`, `GIT_TRACE2_EVENT`) or global configuration keys (`trace2.normalTarget`, `trace2.perfTarget`, `trace2.eventTarget`).
The destination specification supports these sink types (`Documentation/trace2-target-values.adoc` at v2.55.0):
- `0` or `false`: Disables the target sink entirely.
- `1` or `true`: Binds the sink to standard error (`STDERR_FILENO`).
- `[2-9]`: Binds the sink to an already open file descriptor inherited from a parent harness.
- Absolute path: Opens the file in append mode (`O_WRONLY | O_CREAT | O_APPEND`).
  If the path specifies an existing directory, Git generates a dedicated trace file per process underneath that directory, named after the last component of the Session ID (optionally followed by a counter to avoid collisions).
  The value must be absolute: `~` is not expanded, and `~/trace.json` in a config file produces `warning: trace2: unknown value` (tested on Git 2.55.0).
- `af_unix:[<socket-type>:]<absolute-pathname>`: Connects directly to a local Unix domain socket.
  The socket type may be `stream` (`SOCK_STREAM`) or `dgram` (`SOCK_DGRAM`).
  If omitted, Git attempts a stream connection and falls back to datagram.
  The path must fit in `sun_path` (104 bytes on macOS, 108 on Linux), which `tr2_dst.c` checks before connecting.
  The `af_unix:` prefix is compiled in only when `NO_UNIX_SOCKETS` is not defined (`tr2_dst.c`), and the v2.55.0 target-values documentation lists no Windows named-pipe target (as of 2026-11).

The Unix domain socket sink is critical for enterprise telemetry.
Writing high-frequency JSON events to persistent disk creates write amplification, degrades flash memory, and contends with Git index lockfiles.
Streaming directly into a background Unix domain socket eliminates disk I/O from the critical path.
To protect developer workflows from telemetry infrastructure failures, `tr2_dst_try_uds_connect()` in `tr2_dst.c` treats a failed `connect()` as "target unavailable".
The socket is an ordinary blocking socket (there is no `O_NONBLOCK` in `tr2_dst.c` at v2.55.0), but a local connect fails immediately with `ENOENT` when the socket file is missing or `ECONNREFUSED` when a stale socket file has no listener, so Git neither fails nor stalls.
Instead, Git silently disables the target for the duration of the process lifecycle and proceeds with the requested version control command.
Connection warnings are suppressed by default.
Setting `GIT_TRACE2_DST_DEBUG=1` or `trace2.destinationDebug=true` forces Git to print destination connection failures to standard error for troubleshooting, for example `warning: trace2: could not connect to socket '<path>' for 'GIT_TRACE2_EVENT' tracing: No such file or directory` (Git 2.55.0, macOS).
Writes after a successful connect use `write()` on that blocking socket, so a collector that accepts but stops reading eventually blocks Git; see the blocking-listener pitfall.

### 3. Session Identifiers, Process Nesting, and SID Chaining

Correlation across distributed processes requires persistent, collision-resistant identifiers.
Operating system Process IDs (PIDs) are insufficient because kernels rapidly recycle 16-bit or 32-bit integer PIDs.
Trace2 generates a globally unique Session ID (`sid`) for every process invocation in `trace2/tr2_sid.c`.
A root Session ID is computed by `tr2_sid_compute()` in `tr2_sid.c`, reached from `trace2_initialize_fl()` in `trace2.c`, which `init_git()` in `common-init.c` calls at startup, by formatting:

$$\text{SID}_{\text{root}} = \text{YYYYMMDDTHHMMSS.uuuuuuZ} - \text{H}\langle\text{host\_hash}\rangle - \text{P}\langle\text{pid}\rangle$$

Here `host_hash` is the first 8 hexadecimal digits of the SHA-1 of the hostname (the literal `Localhost` replaces `H<hash>` if `gethostname()` fails), and `pid` is the 8-character zero-padded hexadecimal OS process ID (`tr2_sid_append_my_sid_component()` in `tr2_sid.c`).
Git does not inject these into each child explicitly; each Git process calls `setenv()` on its own environment at startup, and every child, including non-Git intermediates such as a `/bin/sh` hook, inherits them:
- `GIT_TRACE2_PARENT_SID`: Set by `tr2_sid_compute()` to the process's full Session ID string.
- `GIT_TRACE2_PARENT_NAME`: Set in `tr2_cmd_name.c` to the process's full command hierarchy (for example `commit`, or `commit/status` in a grandchild's parent).

When the child process initializes Trace2, `tr2_sid_compute()` reads `GIT_TRACE2_PARENT_SID`.
If present, the child computes its local SID fragment and concatenates it to the parent SID using a forward slash separator:

$$\text{SID}_{\text{child}} = \text{SID}_{\text{parent}} \,/\, \text{SID}_{\text{local}}$$

The child's `cmd_name` event similarly concatenates the command hierarchy:

$$\text{hierarchy}_{\text{child}} = \text{hierarchy}_{\text{parent}} \,/\, \text{name}_{\text{child}}$$

```text
[Root Process: git commit]
  SID:       20261108T120000.100000Z-H08bce2d9-P00001a2b
  Command:   commit
  Hierarchy: commit
      |
      +--> spawns child hook: .git/hooks/pre-commit
      |      |
      |      +--> hook invokes: git status --porcelain
      |             SID:       20261108T120000.100000Z-H08bce2d9-P00001a2b/20261108T120000.150000Z-H08bce2d9-P00001a2e
      |             Command:   status
      |             Hierarchy: commit/status
      |
      +--> spawns auto-maintenance: git maintenance run --auto
             SID:       20261108T120000.100000Z-H08bce2d9-P00001a2b/20261108T120000.220000Z-H08bce2d9-P00001a30
             Command:   maintenance
             Hierarchy: commit/maintenance
```

This slash-delimited chain allows telemetry collectors to reconstruct the entire parent-child execution tree without needing global timestamp synchronization or distributed trace context injection.
Process depth is tracked as an integer depth counter: `d0` represents top-level user invocations, `d1` represents direct children, and `d2` represents grandchildren.

### 4. Stopwatch Timers, Counters, and Thread-Local Storage

Fine-grained performance diagnostics require profiling internal phases without modifying function signatures across hundreds of files.
Trace2 implements Thread-Local Storage (TLS) in `trace2/tr2_tls.c`.
Every thread that interacts with Git registers via `trace2_thread_start()`.
The main execution thread is named `"main"`, while background worker threads are named `th%02d:<base>`, such as `"th01:preload_thread"` (`api-trace2` examples) or `"th10:fsm-listen"` (`tr2_tls.h` comment).
Each thread allocates a `struct tr2tls_thread_ctx` (`tr2_tls.h`) containing:
- An active region start-time stack (`array_us_start`, `nr_open_regions`).
- Thread-specific stopwatch timers (`struct tr2_timer_block`).
- Thread-specific counters (`struct tr2_counter_block`).

Regions measure nested execution blocks.
When Git enters an instrumented code block, it invokes `trace2_region_enter(category, label, repository)`.
The TLS engine captures a microsecond timestamp (`getnanotime() / 1000` in `tr2_tls.c`) and pushes the region record onto the thread's region stack.
When exiting the block, `trace2_region_leave()` pops the record and computes `t_rel`:

$$t_{\text{rel}} = t_{\text{leave}} - t_{\text{enter}}$$

$$t_{\text{abs}} = t_{\text{leave}} - t_{\text{process\_start}}$$

The configuration setting `trace2.eventNesting` (default `2`, `tr2env_event_max_nesting_levels` in `tr2_tgt_event.c`) caps the region nesting depth emitted to the JSON event stream.
Regions exceeding the nesting limit execute normally but suppress JSON event emission to prevent telemetry storms.
Stopwatch timers (`tr2_tmr.c`) measure cumulative execution time across non-contiguous function calls.
For example, reading and parsing loose objects may happen thousands of times during a graph walk.
Instead of emitting thousands of individual events, a stopwatch timer accumulates interval counts, total elapsed time, minimum interval duration, and maximum interval duration.
When the thread exits or the process invokes `atexit`, aggregated `timer` and `counter` events are flushed once.

### 5. Configuration Isolation and Automated Redaction

Trace2 possesses extraordinary observability into command line arguments, environment variables, and filesystem paths.
This power presents a significant attack surface if improperly bounded.
Git implements two mandatory security controls in the Trace2 subsystem:

#### Security Scope Isolation
Trace2 explicitly refuses to read configuration directives from repository-local configuration files (`.git/config`), worktree configuration files (`.git/config.worktree`), or command-line overrides (`-c key=value`).
Trace2 settings are parsed exclusively from system configuration (`/etc/gitconfig`), global user configuration (`~/.gitconfig`), or explicit process environment variables.
The mechanism is `tr2_sysenv_load()`, which reads config through `read_very_early_config()`; that function sets `ignore_repo`, `ignore_worktree`, and `ignore_cmdline` (`config.c` at v2.55.0), and `api-trace2` states the rule explicitly.
Tested on Git 2.55.0: both `git -c trace2.eventTarget=<file> status` and `git config trace2.eventTarget <file>` in the repository produced no trace file.
The upstream documentation does not give a security rationale; the practical consequence is still that a repository cannot redirect a developer's telemetry to a socket or file of its choosing.

#### Automated Credential Redaction
Developers frequently execute commands containing sensitive authentication tokens embedded directly within repository URLs, such as `git clone https://x-access-token:ghp_secret@github.com/org/repo.git`.
If written to trace files, these credentials would leak into corporate log aggregators and developer workstations.
Johannes Schindelin added URL password redaction controlled by `GIT_TRACE2_REDACT` in commit b7d49ac1ec ("trace2: redact passwords from https:// URLs by default"), first released in Git 2.43.1 and 2.44.0 (2024).
By default (`GIT_TRACE2_REDACT` unset, `1`, or `true`), `redact_arg()` in `trace2.c` rewrites `http://` and `https://` arguments of the form `user:password@host` in the `start` and `child_start` argv, and Git 2.55.0 also redacts `def_param` values such as `remote.origin.url` (tested).
It replaces only the password component with `<REDACTED>` prior to writing to any target stream:

$$\text{https://user:password@host/path.git} \longrightarrow \text{https://user:\langle REDACTED\rangle@host/path.git}$$

A URL with no colon in the userinfo, such as `https://ghp_token@github.com/org/repo.git`, is not redacted, because `redact_arg()` returns the argument unchanged when there is no `:` before the `@`.
Redaction is controlled only by the environment variable (read with `git_env_bool()`; there is no `trace2.*` config key for it), so it is disabled by exporting `GIT_TRACE2_REDACT=0` in the calling environment.

### 6. Diagnostic Health Bundles: `git diagnose`

Structured event logs track runtime execution, but diagnosing repository corruption, file system performance degradation, or severe monorepo bloat requires a snapshot of static repository health.
In Git 2.38.0 (October 2022), Victoria Dye upstreamed `git diagnose` into core Git from the Microsoft Scalar monorepo management tool (`builtin/diagnose.c` is absent at v2.37.0 and present at v2.38.0; the 2.38.0 release notes say the feature was "lifted from scalar").
The `git-diagnose` command automates the collection of repository diagnostics, packaging the telemetry into a single compressed `.zip` archive.
The command operates in two modes:
- `--mode=stats` (Default): Gathers non-sensitive repository statistics and system environment data.
  The resulting archive contains:
  1. `diagnostics.log`: The `git version --build-options` output (CPU architecture, `sizeof-long`, `sizeof-size_t`, shell path, features such as `fsmonitor--daemon`, and library lines such as OpenSSL, zlib-ng, `SHA-1: SHA1_DC`, `SHA-256: SHA256_BLK`, default hash), the repository root, and available disk space computed via `statvfs()` (`GetDiskFreeSpaceExA()` on Windows, `compat/disk.h`).
  2. `packs-local.txt`: Every file in `.git/objects/pack/` (`.pack`, `.idx`, and companions) with its size, plus the same for each alternate object directory.
  3. `objects-local.txt`: Loose object counts for each existing two-character hexadecimal fanout subdirectory (`.git/objects/??/`) and a `Total: N loose objects` line.
- `--mode=all`: Collects everything in `--mode=stats`, and additionally includes copies of the top-level `.git` files, `.git/hooks`, `.git/info`, `.git/logs` (recursively, the complete reflog history), and `.git/objects/info` (`archive_dirs[]` in `diagnose.c`).

Because reflogs and commit hooks can expose commit messages, branch naming conventions, internal author email addresses, and embedded script credentials, `--mode=all` carries security implications.
Engineers must inspect archive contents before transmitting diagnostics outside secure internal infrastructure boundaries.

## Commands and Numbers to Know

| Item | Value | Platform and date | What it controls |
| :--- | :--- | :--- | :--- |
| `trace2.normalTarget` | unset / `false` | Both (as of 2026-11) | Destination for human-readable normal format; overridden by `GIT_TRACE2` |
| `trace2.perfTarget` | unset / `false` | Both (as of 2026-11) | Destination for columnar performance format; overridden by `GIT_TRACE2_PERF` |
| `trace2.eventTarget` | unset / `false` | Both (as of 2026-11) | Destination for structured JSON event format; overridden by `GIT_TRACE2_EVENT` |
| `trace2.normalBrief` | `false` | Both (as of 2026-11) | Omits time, filename, and line number from normal format when `true` |
| `trace2.perfBrief` | `false` | Both (as of 2026-11) | Omits time, filename, and line number from perf format when `true` |
| `trace2.eventBrief` | `false` | Both (as of 2026-11) | Omits `time`, `file`, and `line` fields from JSON events when `true` |
| `trace2.eventNesting` | `2` | Both (as of 2026-11) | Maximum region nesting depth emitted to event stream; deeper regions suppressed; `GIT_TRACE2_EVENT_NESTING` overrides |
| `trace2.configParams` | unset | Both (as of 2026-11) | Comma-separated glob patterns (e.g. `core.*,user.*`) emitted via `def_param` |
| `trace2.envVars` | unset | Both (as of 2026-11) | Comma-separated environment variable names emitted via `def_param` |
| `trace2.destinationDebug` | `false` | Both (as of 2026-11) | Surfaces destination connection warnings on stderr; overridden by `GIT_TRACE2_DST_DEBUG` |
| `trace2.maxFiles` | `0` (unlimited) | Both (as of 2026-11) | Maximum trace files allowed in directory target before writing sentinel discard file; `GIT_TRACE2_MAX_FILES` overrides |
| `GIT_TRACE2_REDACT` | `1` / `true` | Both (as of 2026-11) | Masks the password in `http(s)://user:password@host` URLs to `<REDACTED>`; env only, Git 2.43.1+ |
| `GIT_TRACE2_PARENT_SID` | environment | Both (as of 2026-11) | Propagates parent Session ID to child processes to build slash-chained SIDs |
| `GIT_TRACE2_PARENT_NAME` | environment | Both (as of 2026-11) | Propagates parent command hierarchy to build slash-chained command hierarchies |
| Event format version (`evt`) | `"4"` | Both (as of 2026-11) | Schema version identifier emitted in the initial `version` event |
| Discard sentinel filename | `git-trace2-discard` | Both (as of 2026-11) | Sentinel file written with `too_many_files` event when `maxFiles` is exceeded |
| `git diagnose --mode` | `stats` | Both (as of 2026-11) | Diagnostic archive collection scope; options are `stats` and `all` |
| Unix domain socket syntax | `af_unix:[type:]<path>` | Both (as of 2026-11) | Socket specification where type is `stream` or `dgram`; if omitted, tries stream then dgram |

## Multi-Language Comparison: C / Python / Rust

Telemetry producers in version control are typically embedded in C binaries, while ingestion collectors, parsing daemons, and fleet aggregation agents span multiple languages.

| Concern | C (Git Core v2.55.0) | Python (Collector Daemon / Lab) | Rust (High-Throughput Agent / `gix`) |
| :--- | :--- | :--- | :--- |
| Role in Pipeline | Telemetry Producer / In-Process Emitter | Local Verification / Prototyping Collector | Production Fleet Ingestion Daemon / Producer |
| Target Dispatch | VTable table of function pointers (`struct tr2_tgt`) | Python dynamic dispatch / event dictionaries | Trait objects (`Box<dyn Target>`) or enum static dispatch |
| IPC Transport | Native `af_unix` sockets (`socket`, `connect`, `write`) | `socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)` | `tokio::net::UnixListener` / `std::os::unix::net` |
| Thread Context | Thread-Local Storage via `tr2_tls.c` and pthread keys | `threading.local()` or thread-safe queue | Thread-local cells or Tokio task-local tracing spans |
| JSON Serialization | `json-writer.c` (`jw_object_*`) building into a `strbuf` | `json.loads()` / `json.dumps()` | High-speed zero-copy `serde_json` |
| Error Handling on Sink Loss | Blocking `connect()` that fails fast on a missing or dead socket and silently disables the sink | Handled via connection retry and exception suppression | `tokio::select!` with bounded MPSC channel drop counters |
| Credential Redaction | `redact_arg()` / `redact_argv()` prefix scan in `trace2.c` | Regular expression string replacement | Fast zero-allocation byte scanning with `regex` or `nom` |

## Worked Lab Example

The companion lab is located at `code/02-Version-Control/trace2_collector/`.
It contains an end-to-end verification harness implementing a multi-threaded Unix domain stream socket collector and telemetry test runner in Python 3.

`run.sh` initialises a clean test environment with sanitized configuration (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`, `LC_ALL=C`).
`collector.py` then executes eight distinct validation phases:
1. `test_socket_ingestion_and_schema()`: Starts a Unix domain stream socket server (`af_unix:stream`), configures `GIT_TRACE2_EVENT` to target the socket, executes Git operations, and validates the incoming JSON line stream against the Trace2 event schema v4.
2. `test_process_hierarchy_and_sid_chaining()`: Configures a `.git/hooks/pre-commit` script that invokes a child `git status` command, executes `git commit`, and verifies that the child process inherits the parent's Session ID via `GIT_TRACE2_PARENT_SID` to construct `<parent_sid>/<child_sid>` with hierarchy `commit/status`.
3. `test_telemetry_parameters()`: Exports `GIT_TRACE2_CONFIG_PARAMS="core.*,user.*"` and `GIT_TRACE2_ENV_VARS="TRACE_TEST_ENV"`, verifying that Git emits `def_param` events recording repository configuration (`core.bare = false`) and process environment telemetry; the lab asserts that `core.bare` arrives with `scope` `local` and `TRACE_TEST_ENV` with `scope` `command`.
4. `test_credential_redaction()`: Adds a remote URL containing plaintext credentials (`https://telemetry_user:super_secret_token@example.com/repo.git`) under `GIT_TRACE2_REDACT=1` and `GIT_TRACE2_REDACT=0`, proving that passwords are replaced with `<REDACTED>` unless redaction is explicitly overridden; it also asserts that a token-only URL (`https://<token>@host/`, no colon) is not masked even with redaction on.
5. `test_socket_resilience_and_debug()`: Points `GIT_TRACE2_EVENT` at a non-existent Unix domain socket, verifying that Git fails silently within 5 seconds without corrupting standard error or aborting command execution, and verifies that `GIT_TRACE2_DST_DEBUG=1` surfaces diagnostic warnings.
6. `test_target_directory_sentinel()`: Configures a directory target with `GIT_TRACE2_MAX_FILES=2`, runs multiple Git commands, and verifies that Git halts further trace file creation by generating the sentinel `git-trace2-discard` containing the `too_many_files` event.
7. `test_config_isolation()`: Sets `trace2.eventTarget` in the repository's local config and via `git -c`, verifies that neither produces a trace file, and verifies as a control that the same key in system config does.
8. `test_git_diagnose_archive()`: Invokes `git diagnose -o <dir> -s telemetry --mode=stats` and verifies that the resulting `.zip` bundle contains `diagnostics.log`, `packs-local.txt`, and `objects-local.txt` with verified hardware architecture and filesystem telemetry.

```bash
cd code/02-Version-Control/trace2_collector
bash run.sh
```

### Verified output

Recorded on darwin-arm64 (macOS, Git 2.55.0) on 2026-11-08 and verified mechanically by `python3 -m _harness.verify`:

```text
=== Git Trace2 Telemetry Collector Verification ===

[Test 1: Unix Domain Socket Ingestion & Event Schema]
Socket type: af_unix (SOCK_STREAM)
Events received: OK (all lines valid JSON)
Base event types verified: ['atexit', 'cmd_ancestry', 'cmd_name', 'data', 'def_repo', 'exit', 'region_enter', 'region_leave', 'start', 'version']
Session ID format verified: ISO8601-UTC-H<hash>-P<pid>
Trace2 Event format version: 4

[Test 2: Process Hierarchy & SID Chaining]
Parent command: commit (hierarchy: commit)
Child execution logged: child_class=hook, hook_name=pre-commit
Child Git command: status (hierarchy: commit/status)
Parent-child SID inheritance verified: <parent_sid>/<child_sid>

[Test 3: Telemetry Parameter Capture]
Config telemetry captured: core.bare = false (scope: local)
Environment telemetry captured: TRACE_TEST_ENV = active_fleet_telemetry (scope: command)

[Test 4: Credential Redaction]
Redaction enabled (GIT_TRACE2_REDACT=1): https://telemetry_user:<REDACTED>@example.com/repo.git
Redaction disabled (GIT_TRACE2_REDACT=0): credential preserved in raw trace
Security assertion: secret token absent when redaction active
Token-only URL (no colon): not masked even with redaction active

[Test 5: Socket Resilience & Destination Debugging]
Non-existent socket without debug: silent fallback, exit code 0, empty stderr, no stall
Non-existent socket with GIT_TRACE2_DST_DEBUG=1: diagnostic warning captured

[Test 6: Directory Target & Sentinel File Discard]
Trace directory file limit enforced: maxFiles = 2
Sentinel file created: git-trace2-discard
Sentinel event verified: too_many_files
Subsequent trace generation halted: directory files bounded

[Test 7: Config Isolation]
Repository-local trace2.eventTarget: ignored
Command-line -c trace2.eventTarget: ignored
System-config trace2.eventTarget: honored

[Test 8: Git Diagnostics Archive Generation]
Archive generated: git-diagnostics-telemetry.zip
Archive entries verified: ['diagnostics.log', 'objects-local.txt', 'packs-local.txt']
System diagnostics extracted: Git version, CPU arch, disk space telemetry present

All 8 telemetry and diagnostic mechanisms verified successfully.
```

### Reading the result

- In Test 1, the Unix stream socket received structured events whose first event is `version` carrying format version `"evt":"4"`, and the set includes `exit` and `atexit`.
  The collector counts lines that fail to parse and the lab asserts there are none, so "all lines valid JSON" is proven.
  Every event's Session ID, not only the first, matched the regular expression `^\d{8}T\d{6}\.\d{6}Z-H[0-9a-f]{8}-P[0-9a-f]{8}`.
- In Test 2, the parent `git commit` command logged `child_start` with `child_class=hook` and `hook_name=pre-commit`.
  The hook's child process (`git status`) inherited the parent's Session ID and appended its own sub-SID, while its `cmd_name` event reported `hierarchy: commit/status`.
- In Test 3, `trace2.configParams` and `trace2.envVars` extracted internal repository parameters without requiring custom scripting inside Git hooks.
- In Test 4, the password in a `user:password@` remote URL was replaced with `<REDACTED>`, while a token-only `https://<token>@host/` URL was left intact, so default redaction does not protect tokens passed as bare userinfo.
- In Test 5, Git demonstrated fail-safe resilience: when the target Unix domain socket did not exist, Git executed with exit code 0 and an empty stderr, only surfacing connection errors when `GIT_TRACE2_DST_DEBUG=1` was explicitly requested.
- In Test 6, when file counts exceeded `maxFiles=2`, Git generated `git-trace2-discard` with event `too_many_files` and ceased creating new files, preventing directory inode bloat.
- In Test 7, a `trace2.eventTarget` in repository-local config or after `git -c` produced no trace file, while the same key in system config did, so the absence is isolation and not a broken setup.
- In Test 8, `git diagnose` generated a ZIP bundle whose `diagnostics.log` contains the `git version`, `cpu:`, and `Available space on` lines, alongside the pack and loose object listings.

## Pitfalls

> [!warning] Pitfall: Configuring `trace2.*` in repository-local configuration
> Setting `trace2.eventTarget` or `trace2.configParams` inside `.git/config` or via `git -c trace2.eventTarget=...` has no effect.
> Trace2 reads its settings with `read_very_early_config()`, which ignores repository, worktree, and `-c` config (tested on Git 2.55.0), so an untrusted repository also cannot redirect telemetry or exfiltrate command arguments.
> You must configure Trace2 exclusively in global configuration (`~/.gitconfig`), system configuration (`/etc/gitconfig`), or via environment variables (`GIT_TRACE2*`).

> [!warning] Pitfall: Unbounded directory logging without `trace2.maxFiles`
> Pointing `GIT_TRACE2_EVENT` to a local directory writes one trace file per Git invocation.
> On developer workstations running active IDEs, background file watchers, and status polling, Git commands can execute tens of thousands of times per day.
> Without setting `trace2.maxFiles`, directory targets grow without bound and can exhaust disk space or inodes, after which unrelated writes fail.
> Always configure a finite limit (such as `git config --global trace2.maxFiles 5000`) or stream directly to a Unix domain socket.

> [!warning] Pitfall: Transmitting `git diagnose --mode=all` bundles across untrusted networks
> Running `git diagnose --mode=all` copies `.git/logs/`, `.git/hooks/`, and `.git/info/` into the output archive.
> The reflog contains every historical commit message, branch name, stash message, and orphaned commit reference created in the repository.
> If developers stash files containing credentials or commit sensitive API tokens, those secrets are packaged into the ZIP bundle.
> Restrict automated incident response tooling to `--mode=stats`, which records file sizes and fanout counts without reading file payloads or reflog contents.

> [!warning] Pitfall: Blocking socket listeners stalling interactive Git operations
> If a telemetry collection daemon listening on an `af_unix:stream` socket stops reading a connection, for example because it handles connections one at a time or blocks on disk or network I/O, the Unix domain socket buffer fills (`net.local.stream.sendspace` is 8192 bytes on macOS).
> Once socket buffers fill, the Git client process blocks in `write()`; Git does not use `O_NONBLOCK` on trace2 sockets.
> Observed on Git 2.55.0 on macOS: a listener that drained one connection at a time deadlocked `git commit`, because the parent's connection stayed open while the pre-commit hook's `git status` blocked writing to its own unread connection.
> Telemetry collection daemons must read socket streams asynchronously using non-blocking I/O and decouple ingestion from remote publishing using bounded in-memory ring buffers.

> [!warning] Pitfall: Assuming integer PIDs identify unique processes across telemetry fleets
> In large engineering organizations processing millions of Git commands daily, operating systems recycle process IDs thousands of times.
> Grouping events by `pid` or matching `child_start` to `child_exit` via PID across multiple developer machines creates false concurrency and corrupted metrics.
> Telemetry pipelines must use the composite `sid` string as the unique primary key, using slash-splitting to reconstruct parent-child relationships.

> [!warning] Pitfall: Disabling credential redaction in shared CI pipelines
> Setting `GIT_TRACE2_REDACT=0` disables the masking of `user:password` credentials in `http(s)` URLs in argument vectors and parameters; token-only URLs such as `https://<token>@host/` are never masked.
> If set in continuous integration or shared build runners, any git command that clones or pushes using a token-embedded URL writes the raw secret to standard error or the event stream.
> Never disable `GIT_TRACE2_REDACT` in shared, multi-tenant, or production environments.

## At Scale

At small scale (1 to 50 developers), Git diagnostics are primarily reactive: individual engineers enable `GIT_TRACE2_PERF=1` on an ad-hoc basis to inspect why a local checkout or rebase feels slow.
At enterprise scale (1,000 to 50,000+ developers working in multi-gigabyte repositories), uninstrumented Git operations represent a major developer productivity blindspot.
If average `git status` latency regresses by 800 milliseconds across 20,000 engineers running 50 commands per day, the organization loses over 220 engineer-hours daily in idle waiting time.
Enterprise developer infrastructure teams deploy managed Trace2 fleet telemetry architectures to catch regressions before they impact the organization.

```mermaid
flowchart TD
    subgraph CLIENT["Developer Workstations (1,000 - 50,000 Nodes)"]
        GIT1["git status / checkout / commit"]
        GIT2["IDE Background Watchers"]
        HOOK["Internal Security Hooks"]
        GIT1 -->|"af_unix:stream<br/>fail-fast connect"| SOCK["/var/run/git-trace2.sock"]
        GIT2 --> SOCK
        HOOK --> SOCK
        SOCK --> AGENT["Local Telemetry Agent<br/>(Lightweight Daemon / Go or Rust)"]
    end

    subgraph LOCAL_FILTER["Local Agent Processing"]
        AGENT --> RING["Bounded Ring Buffer<br/>(Drop telemetry if overloaded)"]
        RING --> BATCH["Micro-Batcher<br/>(Compress & redact local PII)"]
    end

    subgraph PIPELINE["Enterprise Telemetry Pipeline"]
        BATCH -->|"gRPC / HTTPS (mTLS)<br/>Sampled Stream"| GATEWAY["Observability Gateway<br/>(OpenTelemetry / Vector / Kafka)"]
        GATEWAY --> CLICKHOUSE["Real-Time Analytics<br/>(ClickHouse / BigQuery)"]
        GATEWAY --> METRICS["Time-Series Metrics<br/>(Prometheus / Grafana)"]
    end

    subgraph ACTIONS["Automated Decision Rules"]
        CLICKHOUSE --> ALERT["Alert on p95 Status > 2.0s"]
        CLICKHOUSE --> BOTTLENECK["Isolate Hook vs Core Latency"]
        CLICKHOUSE --> FSMON["Detect Corrupted Fsmonitor Daemons"]
    end
```

### Operational Decision Rules for Large Fleets

1. **Transport Selection**:
   Never configure file-based directory logging across an enterprise fleet.
   Deploy a local background agent listening on an `af_unix:stream` socket at a standard absolute path (such as `/var/run/git-trace2.sock` on Linux or `/Library/Caches/git-trace2.sock` on macOS; `~` is not expanded in trace2 targets), registered via system configuration (`/etc/gitconfig`).
   If the local agent is terminated, Git's `connect()` fails immediately and the target is silently disabled; an agent that is alive but not reading is the case that stalls Git.

2. **Telemetry Sampling and Tiering**:
   Measured on Git 2.55.0 (macOS, 200-file repository, default nesting): `git log --oneline` emitted 1.5 KB and `git status` 11 KB of event JSON; per-command size depends on the command and on `trace2.eventNesting`, so measure your own fleet before sizing a collector.
   Across 30,000 developers executing 100 commands daily (3 million commands), an average of 10 KB per command is about 30 GB of raw JSON per day, and every 10 KB of average growth adds another 30 GB.
   Implement sampling at the local agent:
   - High-frequency read queries (`git status`, `git diff`, `git rev-parse`): Sample at 1% to 5% across the fleet.
   - Long-running network or disk operations (`git clone`, `git fetch`, `git checkout`, `git rebase`): Sample at 100%.
   - Process failures (`code != 0`, `event == "error"`): Ingest at 100% to track build failures and corrupted repositories.

3. **Latency Attribution via Region Nesting**:
   When diagnosing slow developer commands, query the relative duration (`t_rel`) of nested regions:
   - If `category: "index", label: "do_read_index"` dominates, the index file is bloated; trigger sparse-index or split-index optimizations ([[Git-Index-Stat-Cache-and-Fsmonitor]]).
   - If `category: "status", label: "untracked"` dominates, filesystem crawling is slow; deploy the built-in fsmonitor daemon.
   - If `child_class: "hook"` accounts for more than 50% of the total elapsed time (`t_abs`), the bottleneck is caused by custom internal pre-commit scripts, not Git core.

4. **Incident Response with Diagnostic Health Bundles**:
   When a developer reports catastrophic repository degradation, trigger an automated script that runs `git diagnose --mode=stats`.
   Have the triage agent compare the resulting `objects-local.txt` and `packs-local.txt` against fleet baselines:
   - A loose object count far above `gc.auto` (default 6700) indicates that automatic gc is disabled or failing.
   - More than `gc.autoPackLimit` (default 50) packfiles indicates an urgent need for geometric repacking (`git repack --geometric=2`).
   - Disk space telemetry in `diagnostics.log` flags low-space conditions where atomic rename operations fail during ref updates.

[[Git-Index-Stat-Cache-and-Fsmonitor]] owns the index stat cache and fsmonitor daemon mechanics, [[Git-GC-Maintenance-MIDX-and-Commit-Graph]] owns background maintenance scheduling, and [[Git-At-Scale-VFS-Scalar-and-Monorepos]] covers client-side virtualization for multi-billion line codebases.

## Practice Drill

> [!tip] Practice Drill:
> Execute the following sequence in a scratch directory to capture and analyze live Trace2 JSON telemetry over a local Unix domain stream socket.
> Start a background Python socket listener, execute a compound Git operation with an instrumented pre-commit hook, inspect the resulting process hierarchy, and verify that credential redaction masks secrets in the event stream.

```bash
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export LC_ALL=C
WORKDIR=$(mktemp -d)
cd "$WORKDIR"

# 1. Start a lightweight background socket listener collecting Trace2 JSON events.
SOCK="$WORKDIR/trace2.sock"
python3 -c "
import socket, threading

# Drain every connection concurrently: a parent git keeps its connection open
# while its hook child writes, so a one-at-a-time reader deadlocks once the
# child fills the socket buffer (8 KB on macOS, net.local.stream.sendspace).
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind('$SOCK')
s.listen(16)
s.settimeout(3.0)  # stop after 3 s with no new connection
bufs, threads = [], []
def drain(conn):
    conn.settimeout(None)
    buf = b''
    with conn:
        while data := conn.recv(65536):
            buf += data
    bufs.append(buf)  # one append per connection keeps lines whole
while True:
    try:
        conn, _ = s.accept()
    except socket.timeout:
        break
    t = threading.Thread(target=drain, args=(conn,))
    t.start()
    threads.append(t)
for t in threads:
    t.join()
s.close()
with open('$WORKDIR/events.jsonl', 'wb') as out:
    out.write(b''.join(bufs))
" &
LISTENER_PID=$!
sleep 0.2

# 2. Initialize a repository with an active hook.
git init -b main -q repo
cd repo
mkdir -p .git/hooks
cat << 'EOF' > .git/hooks/pre-commit
#!/bin/sh
git status --porcelain
exit 0
EOF
chmod +x .git/hooks/pre-commit

# 3. Commit with Trace2 telemetry enabled over the Unix domain socket.
echo "telemetry payload" > file.txt
git add file.txt
export GIT_AUTHOR_NAME="Drill Engineer"
export GIT_AUTHOR_EMAIL="drill@example.com"
export GIT_COMMITTER_NAME="Drill Engineer"
export GIT_COMMITTER_EMAIL="drill@example.com"
export GIT_TRACE2_EVENT="af_unix:stream:$SOCK"
export GIT_TRACE2_CONFIG_PARAMS="core.*"
export GIT_TRACE2_REDACT=1

git commit -m "drill: trace2 verification" -q

# 4. Trigger remote credential redaction.
git remote add origin "https://token_user:super_secret_pat@github.com/org/repo.git"

wait $LISTENER_PID 2>/dev/null || true

# 5. Analyze the captured telemetry.
python3 -c "
import json

with open('$WORKDIR/events.jsonl') as f:
    events = [json.loads(line) for line in f if line.strip()]

print('Total Trace2 events captured:', len(events))
for e in events:
    evt = e.get('event')
    if evt == 'cmd_name':
        print(f'Command: {e.get(\"name\"):<12} Hierarchy: {e.get(\"hierarchy\"):<18} SID: {e.get(\"sid\")}')
    elif evt == 'child_start' and e.get('child_class') == 'hook':
        print(f'Parent spawned hook: {e.get(\"hook_name\")} (argv: {e.get(\"argv\")})')
    elif evt == 'def_param' and e.get('param') == 'core.bare':
        print(f'Telemetry param: core.bare = {e.get(\"value\")} (scope: {e.get(\"scope\")})')
    elif evt == 'start' and 'remote' in e.get('argv', []):
        print(f'Redacted remote argv: {e.get(\"argv\")}')
"

cd / && rm -rf "$WORKDIR"
```

## Interview Questions

> [!question] L5: What are the three built-in Trace2 target formats, and what distinct operational problem was each designed to solve?
>
> > [!success]- Answer
> > The three built-in targets are Normal (`GIT_TRACE2`), Performance (`GIT_TRACE2_PERF`), and Event (`GIT_TRACE2_EVENT`).
> > The Normal target produces traditional, human-readable `printf()` lines; it was designed as an immediate drop-in replacement for legacy `GIT_TRACE` debugging without breaking basic developer mental models.
> > The Performance target outputs column-aligned, pipe-delimited records showing process depth (`d0` through `dN`), thread names, relative region durations (`t_rel`), and absolute timestamps (`t_abs`); it was designed for interactive developer profiling to pinpoint execution bottlenecks across nested regions.
> > The Event target outputs structured, single-line JSON objects adhering to a versioned schema; it was designed for automated machine ingestion, local daemon collection over Unix domain sockets, and fleet-wide telemetry aggregation.

> [!question] L5: Predict the output when a developer configures `git -c trace2.eventTarget=/tmp/trace.json status` to trace a single command: what is written to `/tmp/trace.json`, and why?
>
> > [!success]- Answer
> > Nothing is written to `/tmp/trace.json`, and the file is not created.
> > Trace2 explicitly ignores configuration passed via the `-c` command-line switch, repository-local `.git/config`, or worktree configuration files.
> > The mechanism is that `tr2_sysenv_load()` reads config with `read_very_early_config()`, which ignores repository, worktree, and command-line config; `api-trace2` documents the rule without a rationale, but it also acts as a security boundary, because repository-controlled configuration cannot redirect arguments, command paths, or tokens to a socket or file of its choosing.
> > Tested on Git 2.55.0: no file is created.
> > Trace2 only respects settings declared in system configuration (`/etc/gitconfig`), global configuration (`~/.gitconfig`), or environment variables (`GIT_TRACE2_EVENT`).

> [!question] L6: Walk through how Git maintains distributed parent-child process relationships across nested hooks and submodules without a shared database.
>
> > [!success]- Answer
> > Git maintains parent-child process trees using environment variable inheritance managed by `run-command.c` and `tr2_sid.c`.
> > When a parent Git process initializes, it generates a globally unique Session ID (`sid`) consisting of a UTC timestamp, a hostname hash, and its hexadecimal PID.
> > At startup each Git process calls `setenv()` on two variables in its own environment, which every child it spawns inherits: `GIT_TRACE2_PARENT_SID` containing its own Session ID, and `GIT_TRACE2_PARENT_NAME` containing its command hierarchy.
> > If the child process is another Git binary (such as `git status` executed inside a hook, or recursive submodule commands), the child inspects `GIT_TRACE2_PARENT_SID` during startup.
> > The child constructs its own SID by appending a forward slash and its local SID fragment: `${GIT_TRACE2_PARENT_SID}/${CHILD_SID}`.
> > It similarly appends its command name to form the hierarchy: `${GIT_TRACE2_PARENT_NAME}/${CHILD_NAME}`.
> > Telemetry collectors ingest these events independently and reconstruct the complete multi-tier execution tree simply by splitting SIDs on slashes.

> [!question] L6: Debug this scenario where a team deploys an internal Unix domain socket daemon for fleet-wide telemetry, but developers report that whenever the daemon restarts or crashes, their `git` commands hang for 30 seconds: what is the root cause and how do you fix it?
>
> > [!success]- Answer
> > A dead daemon is not the cause: with no listener, `connect()` in `tr2_dst.c` fails immediately with `ENOENT` (socket file gone) or `ECONNREFUSED` (stale socket file), and Git silently disables the target.
> > The hang comes from a daemon that is alive but not reading: while it restarts, replays a backlog, or blocks on its own I/O, connections are accepted or queued but not drained, the socket send buffer fills (8 KB on macOS), and Git blocks in `write()` because trace2 sockets are blocking; a hang of roughly 30 seconds suggests the daemon resumes reading after a startup or flush timeout.
> > A second cause is a plain-path target (`trace2.eventTarget=/path`) that points at a FIFO: `open(O_WRONLY | O_APPEND | O_CREAT)` blocks until a reader opens the FIFO.
> > To fix this:
> > 1. Ensure the collector daemon creates a genuine Unix domain stream socket (`AF_UNIX`, `SOCK_STREAM`), unlinks stale socket files on startup, and starts listening only when it is ready to read.
> > 2. In the collector daemon, drain every accepted connection concurrently (a thread, task, or event loop per connection) into an in-memory queue, because a parent Git keeps its connection open while its hook children write.
> > 3. Never point a path target at a FIFO; use `af_unix:` for streaming.
> > 4. Verify with `GIT_TRACE2_DST_DEBUG=1` that a stopped daemon produces a fast connection warning rather than a delay.

> [!question] L6: How does `trace2.maxFiles` prevent disk exhaustion when writing trace logs to a directory target, and what is the role of the `git-trace2-discard` sentinel?
>
> > [!success]- Answer
> > When `trace2.eventTarget` or `trace2.normalTarget` points to a directory path, Git generates a dedicated trace file per process named after the last component of the process Session ID.
> > In high-churn environments or repositories with frequent polling, this creates hundreds of thousands of small files, exhausting directory inodes.
> > The `trace2.maxFiles` configuration option (default `0`, meaning unlimited) defines an upper bound on the number of trace files allowed in that directory.
> > Before creating a new trace file, Git checks the directory's file count.
> > If the count reaches `trace2.maxFiles` (`file_count >= tr2env_max_files` in `tr2_dst_too_many_files()`), Git skips writing the process trace and instead creates a single sentinel file named `git-trace2-discard`.
> > The event target writes a `too_many_files` event into the sentinel file.
> > Subsequent Git invocations first check for `git-trace2-discard`; if present, they skip trace generation without scanning directory entries, protecting the workstation filesystem until a cleanup job purges the directory and removes the sentinel.

> [!question] L7: Why was `GIT_TRACE2_REDACT` introduced, what specific data patterns does it mask, and what are the security failure modes of disabling it in a CI/CD build train?
>
> > [!success]- Answer
> > `GIT_TRACE2_REDACT` was introduced by Johannes Schindelin in commit b7d49ac1ec, first released in Git 2.43.1 and 2.44.0, to prevent credentials from leaking into telemetry streams and system logs.
> > By default (`GIT_TRACE2_REDACT` unset or `1`), `redact_arg()` checks command-line arguments in `start` and `child_start` events, and Git 2.55.0 also checks `def_param` values, for `http://` or `https://` URLs of the form `<user>:<password>@<host>`.
> > It masks the password, replacing it with `<REDACTED>`, and keeps the user name; a token passed as the user name with no colon (`https://<token>@host/`) is not masked.
> > In a CI/CD build train or automated merge queue, build scripts frequently run commands like `git clone https://x-access-token:$GITHUB_TOKEN@github.com/org/repo.git` or set credentials in temporary remote URLs.
> > If an engineer disables redaction (`GIT_TRACE2_REDACT=0`) to debug a pipeline failure, or the script uses a token-only URL, the raw authentication token is written in plaintext to the Trace2 event stream.
> > If Trace2 logs are forwarded to centralized aggregators (such as Elasticsearch, Datadog, or S3 buckets), any user or automated system with log read permissions gains full access to write-privileged repository tokens, compromising repository integrity and enabling supply-chain attacks.

> [!question] L7: Walk through the internal architecture of `git diagnose`, comparing `--mode=stats` against `--mode=all`, and explain how an infrastructure engineer uses the resulting diagnostic bundle to identify repository bloat.
>
> > [!success]- Answer
> > `git diagnose` collects system, filesystem, and repository health metrics into a compressed `.zip` archive.
> > In `--mode=stats` (the default), it runs without reading commit contents or object payloads:
> > 1. `diagnostics.log`: Records Git version, build flags, compiler info, CPU architecture, pointer sizes, enabled features (`fsmonitor`), cryptographic libraries, repository worktree root, and available filesystem disk space via `statvfs()`.
> > 2. `packs-local.txt`: Lists every file in the pack directory (`.pack`, `.idx`, and companions) with byte sizes, for the local and alternate object stores.
> > 3. `objects-local.txt`: Counts loose objects in each existing two-character hexadecimal fanout subdirectory (`.git/objects/??/`) plus a total.
> > In `--mode=all`, it additionally packages the top-level `.git` files, `.git/hooks`, `.git/info`, `.git/logs` (reflog history), and `.git/objects/info`.
> > To identify repository bloat:
> > - An engineer inspects `objects-local.txt`: a total far above `gc.auto` (default 6700) means automatic garbage collection is disabled or failing, which slows object lookups.
> > - An engineer inspects `packs-local.txt`: a pack count above `gc.autoPackLimit` (default 50), or hundreds of small packs, indicates fragmented packs requiring geometric repacking (`git repack --geometric=2`).
> > - Available disk space in `diagnostics.log` identifies whether the filesystem is dangerously close to full, which causes silent failures during atomic rename operations on packed-refs or index files.

> [!question] L8+: Design a fleet-wide Git performance telemetry architecture for 30,000 engineers working in a multi-gigabyte monorepo, detailing the client agent transport, sampling strategies, correlation with server-side tracing, and privacy/PII guardrails.
>
> > [!success]- Answer
> > An enterprise Git telemetry architecture for 30,000 engineers requires a three-tier design:
> > 1. Client Transport & Daemon:
> >    Configure `/etc/gitconfig` with `trace2.eventTarget = af_unix:stream:/var/run/git-trace2.sock` (or user-isolated paths in `$XDG_RUNTIME_DIR`).
> >    Deploy a local background agent (written in Rust or Go) listening on this domain socket.
> >    If the agent is offline, Git's `connect()` fails immediately and Git continues untraced; the agent must drain every connection concurrently, because trace2 writes are blocking.
> >    The local agent uses an internal bounded MPSC ring buffer to decouple socket ingestion from network egress.
> > 2. Sampling & Tiering Strategy:
> >    To prevent ingesting hundreds of gigabytes of JSON daily:
> >    - Sample high-frequency commands (`git status`, `git diff`, `git rev-parse`) at 2% across general workstations.
> >    - Ingest 100% of mutating commands (`git commit`, `git merge`, `git rebase`, `git checkout`).
> >    - Ingest 100% of commands returning non-zero exit codes (`code != 0`) or emitting `error` events to capture developer failures in real time.
> >    - For developers opted into performance benchmarking groups, dynamically increase sampling to 100%.
> > 3. Distributed Context & Server Correlation:
> >    Set `transfer.advertiseSID = true` (Git 2.30.0+, default false) on clients and servers: each side then advertises its Trace2 session ID to the other through the `session-id` capability (protocol v2 and v0), so the server's Trace2 stream records the client SID.
> >    Server-side smart HTTP / protocol v2 handlers log the same Session ID into backend distributed traces (OpenTelemetry / Jaeger), allowing end-to-end trace joining from client-side `fetch` through CDN bundle-URI servers to backend pack generation.
> >    Note that `trace2.configParams` only logs config values; it cannot make a custom `http.extraHeader` match the SID.
> > 4. Privacy & Compliance:
> >    Keep `GIT_TRACE2_REDACT` at its default (on): it is environment-only, so enforce it by ensuring no managed shell profile or CI template exports `GIT_TRACE2_REDACT=0`, and ban token-only URLs, which it does not mask.
> >    The local agent sanitizes argument vectors before network transmission: file paths outside the repository root are stripped, commit messages are excluded, and author email usernames are pseudonymized with salted cryptographic hashes to comply with GDPR/CCPA employee monitoring regulations while preserving cohort analysis.

## Further Reading

- `api-trace2` (Git 2.55.0 Technical Documentation): Normative documentation of the Trace2 API, event schemas, VTable dispatch, and socket target values.
- `git-config` (Git 2.55.0 Reference Manual): Specifications for `trace2.eventTarget`, `trace2.configParams`, `trace2.envVars`, `trace2.eventNesting`, and `trace2.maxFiles`.
- `git-diagnose` (Git 2.55.0 Reference Manual): Operational manual for repository diagnostic bundle generation and mode selection.
- Jeff Hostetler, "Trace2 API Design and Implementation" (Git Mailing List, 2019): The founding patch series explaining why structured telemetry was necessary for enterprise-scale Git.
- Johannes Schindelin, "trace2: redact passwords from https:// URLs by default" (git.git commit b7d49ac1ec, 2023, released in Git 2.43.1 and 2.44.0): Technical rationale and implementation details for automated credential masking.
- Victoria Dye, "Scalar Diagnose to Git Diagnose" (Git Mailing List, 2022): The architectural refactoring moving scalar diagnostics into Git core builtin tooling.

## Related

- Prerequisites: [[Git-Index-Stat-Cache-and-Fsmonitor]]
- Next: [[Git-Expert-Config]]
- See also: [[19-Shell-and-Unix-Engineering/20-Observability-and-Incident-Response/Log-Triage-Pipelines|Log Triage Pipelines]]
- Lab: `code/02-Version-Control/trace2_collector/`
