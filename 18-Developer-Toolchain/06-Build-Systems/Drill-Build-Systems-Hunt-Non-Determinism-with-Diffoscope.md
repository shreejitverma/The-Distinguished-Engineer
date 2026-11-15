---
type: problem
track: [sde, distinguished, low-latency, quant-dev]
level:
status: solid
last_reviewed:
sources:
  - "Reproducible Builds project, diffoscope: in-depth comparison of files, archives, and directories (diffoscope 259 --help, comparators/elf.py), Debian package diffoscope-minimal 259 in Ubuntu 24.04, 2026, https://diffoscope.org/"
  - "Reproducible Builds project, diffoscope(1) manual page (diffoscope 161), Debian manpages, 2020, https://manpages.debian.org/testing/diffoscope/diffoscope.1.en.html"
  - "Reproducible Builds project, SOURCE_DATE_EPOCH specification, revision 1.1, 2017, https://reproducible-builds.org/specs/source-date-epoch/"
  - "Reproducible Builds project, Build path (documentation), 2026, https://reproducible-builds.org/docs/build-path/"
  - "Reproducible Builds project, Archive metadata (documentation), 2026, https://reproducible-builds.org/docs/archives/"
  - "Reproducible Builds project, Stable order for outputs (documentation), 2026, https://reproducible-builds.org/docs/stable-outputs/"
  - "Reproducible Builds project, Timestamps (documentation), 2026, https://reproducible-builds.org/docs/timestamps/"
  - "Free Software Foundation, Using the GNU Compiler Collection: Environment Variables Affecting GCC (SOURCE_DATE_EPOCH), gcc.gnu.org, 2026, https://gcc.gnu.org/onlinedocs/gcc/Environment-Variables.html"
  - "Free Software Foundation, Using the GNU Compiler Collection: Options Controlling the Kind of Output (-ffile-prefix-map, -fcanon-prefix-map), gcc.gnu.org, 2026, https://gcc.gnu.org/onlinedocs/gcc/Overall-Options.html"
  - "Free Software Foundation, Using the GNU Compiler Collection: Options for Debugging Your Program (-fdebug-prefix-map, -grecord-gcc-switches), gcc.gnu.org, 2026, https://gcc.gnu.org/onlinedocs/gcc/Debugging-Options.html"
  - "Free Software Foundation, Using the GNU Compiler Collection: Options Controlling the Preprocessor (-fmacro-prefix-map), gcc.gnu.org, 2026, https://gcc.gnu.org/onlinedocs/gcc/Preprocessor-Options.html"
  - "Free Software Foundation, Using the GNU Compiler Collection: GCC Developer Options (-frandom-seed), gcc.gnu.org, 2026, https://gcc.gnu.org/onlinedocs/gcc/Developer-Options.html"
  - "Free Software Foundation, Using the GNU Compiler Collection: Options to Request or Suppress Warnings (-Wdate-time), gcc.gnu.org, 2026, https://gcc.gnu.org/onlinedocs/gcc/Warning-Options.html"
  - "GCC developers, gcc/file-prefix-map.cc (add_prefix_map, remap_filename), GCC 13 release branch, 2023, https://github.com/gcc-mirror/gcc/blob/releases/gcc-13/gcc/file-prefix-map.cc"
  - "GCC developers, gcc/opts.cc (gen_command_line_string, gen_producer_string), GCC 13 release branch, 2023, https://github.com/gcc-mirror/gcc/blob/releases/gcc-13/gcc/opts.cc"
  - "LLVM Project, Clang 16.0.0 Release Notes (SOURCE_DATE_EPOCH), releases.llvm.org, 2023, https://releases.llvm.org/16.0.0/tools/clang/docs/ReleaseNotes.html"
  - "The Rust Project Developers, The rustc book: Command-line Arguments (--remap-path-prefix, --remap-path-scope), doc.rust-lang.org, 2026, https://doc.rust-lang.org/rustc/command-line-arguments.html"
  - "The Rust Project Developers, The rustc book: Remap source paths, doc.rust-lang.org, 2026, https://doc.rust-lang.org/rustc/remap-source-paths.html"
  - "The Rust Project Developers, RELEASES.md (Version 1.95.0, stabilize --remap-path-scope, PR 147611), rust-lang/rust, 2026, https://github.com/rust-lang/rust/blob/master/RELEASES.md"
  - "The Rust Project Developers, library/std/src/hash/random.rs (RandomState, thread-local KEYS, SipHasher13), rust-lang/rust, 2026, https://github.com/rust-lang/rust/blob/master/library/std/src/hash/random.rs"
  - "The Rust Project Developers, std::collections::hash_map::RandomState, doc.rust-lang.org, 2026, https://doc.rust-lang.org/std/collections/hash_map/struct.RandomState.html"
  - "The Cargo Team, The Cargo Book: Unstable Features (profile trim-paths, tracking issue cargo#12137), doc.rust-lang.org, 2026, https://doc.rust-lang.org/cargo/reference/unstable.html"
  - "Python Software Foundation, Command line and environment (PYTHONHASHSEED), docs.python.org, 2026, https://docs.python.org/3/using/cmdline.html"
  - "Python Software Foundation, Built-in Types (dict insertion order, set), docs.python.org, 2026, https://docs.python.org/3/library/stdtypes.html"
  - "Python Software Foundation, py_compile: Compile Python source files (SOURCE_DATE_EPOCH and CHECKED_HASH), docs.python.org, 2026, https://docs.python.org/3/library/py_compile.html"
  - "GNU Binutils project, GNU Binary Utilities: ar command-line options (D and U modifiers), sourceware.org, 2026, https://sourceware.org/binutils/docs/binutils/ar-cmdline.html"
  - "The Bazel Authors, Debugging Remote Cache Hits for Remote Execution (execution logs), bazel.build, 2026, https://bazel.build/remote/cache-remote"
  - "The Bazel Authors, rules_cc cc/private/toolchain/unix_cc_configure.bzl (unfiltered_compile_flags redacting __DATE__, __TIME__, __TIMESTAMP__), GitHub, 2026, https://github.com/bazelbuild/rules_cc/blob/main/cc/private/toolchain/unix_cc_configure.bzl"
  - "The Bazel Authors, rules_cc cc/private/toolchain/unix_cc_toolchain_config.bzl (random_seed, macos_reproducible, ZERO_AR_DATE), GitHub, 2026, https://github.com/bazelbuild/rules_cc/blob/main/cc/private/toolchain/unix_cc_toolchain_config.bzl"
  - "Joel Rosdahl et al., Ccache Manual (base_dir, hash_dir), ccache.dev, 2026, https://ccache.dev/manual/latest.html"
  - "Bazel Remote APIs authors, build/bazel/remote/execution/v2/remote_execution.proto (Action, Command), GitHub, 2026, https://github.com/bazelbuild/remote-apis/blob/main/build/bazel/remote/execution/v2/remote_execution.proto"
  - "GNU Binutils project, GNU Binary Utilities: ranlib (-D), sourceware.org, 2026, https://sourceware.org/binutils/docs/binutils/ranlib.html"
  - "GNU Binutils project, GNU Binary Utilities: objcopy (--enable-deterministic-archives), sourceware.org, 2026, https://sourceware.org/binutils/docs/binutils/objcopy.html"
  - "GDB developers, Debugging with GDB: Specifying Source Directories (set substitute-path), sourceware.org, 2026, https://sourceware.org/gdb/current/onlinedocs/gdb.html/Source-Path.html"
  - "LLVM Project, GDB to LLDB command map (settings set target.source-map), lldb.llvm.org, 2026, https://lldb.llvm.org/use/map.html"
---

# Drill: Hunt Non-Determinism with Diffoscope

> [!summary] Scope
> Owns a practical debugging scenario where a C++ and Rust compilation unit fails remote cache hits due to hidden non-determinism.
> Reaches the Debug and Explain rungs under timed conditions by tasking the engineer with running diffoscope across successive builds, identifying embedded macros, unsorted dictionary iterations in code generators, and leaked build paths.
> Requires modifying the build configuration to achieve bit-for-bit identity across repeated builds.

The theory of reproducible builds, the `SOURCE_DATE_EPOCH` contract, and the variation taxonomy are owned by [[Hermetic-and-Reproducible-Builds]].
How an action key is computed and stored is owned by [[Remote-Execution-API-and-Remote-Caching]], and the procedure for diffing two Bazel or Buck2 execution logs is owned by [[Diagnose-Bazel-and-Buck2-Cache-Misses]].
This drill owns one skill: given two builds of the same commit that should be identical and are not, find every root cause with diffoscope, prove each one, and change the build until the artifacts are equal byte for byte.

## The Ticket

A trading-infrastructure team builds an order engine from one repository with two languages in it.
The C++ side compiles a version stamp, an opcode dispatcher fed by a header that a Python generator writes, and a `main`, then links `engine` and archives `libengine.a`.
The Rust side runs a generator, in the role of a Cargo build script, that writes a routing table into `OUT_DIR`, and compiles the `routing` crate to one object file.
CI packages everything into `engine.tar.gz`.

The platform team reports that the shared remote cache hit rate for this repository is near zero across CI agents, while every other repository sits above 90 percent, and that two release tarballs built from the same commit an hour apart have different SHA-256 digests.
Your job: make two builds of the same commit on two different agents produce the same `engine.tar.gz`, and explain why each fix moves the cache hit rate.
Target time: 30 minutes, with the hint ladder below as the clock.

## Reproduction

The lab is `code/06-Build-Systems/drill_diffoscope_determinism/`.

```bash
cd code/06-Build-Systems/drill_diffoscope_determinism
bash run.sh
# or, through the harness (the image build needs the network once):
cd code && TOOLCHAIN_LABS_NETWORK=1 make verify LAB=drill_diffoscope_determinism
```

`run.sh` declares `# requires: docker, network`.
It builds a local image from the lab's `Dockerfile` (the same pinned `ubuntu:24.04` digest as the harness image, plus `diffoscope-minimal`, `g++`, `rustc`, and `binutils` from the Ubuntu archive), tagged by a hash of the Dockerfile, and then runs `drill.sh` inside it with `--network none`, `--cap-drop ALL`, and `no-new-privileges`, with the lab mounted read-only.
The network is needed only for the one-time `docker build`; the drill itself runs offline.

What the lab contains:

| File | Role |
| :--- | :--- |
| `src/build.sh` | The build configuration under test; `build.sh ROOT FIXES` builds the checkout at `ROOT`, and `FIXES` is a comma list of `epoch`, `prefix`, `sort`. |
| `src/version.cpp` | `build_stamp()` returns `"engine built " __DATE__ " " __TIME__`; `version_source()` returns `__FILE__`. |
| `src/gen_opcodes.py` | Writes `gen/opcodes.h` by iterating a Python `set` of 12 opcode names; `--sorted` sorts them first. |
| `src/engine.cpp`, `src/main.cpp` | Use the generated enum and its name table; `engine.cpp` prints `__FILE__` in an error path. |
| `src/rust/gen_routes.rs` | Fills a `std::collections::HashMap` with 12 routes and writes `routes.rs`; `--sorted` sorts the entries first. |
| `src/rust/lib.rs` | `include!(concat!(env!("OUT_DIR"), "/routes.rs"))`, the way a crate consumes build-script output. |
| `drill.sh` | Builds the same source twice per stage as agent A and agent B, runs diffoscope, and calls `summarize.py`. |
| `summarize.py` | Turns diffoscope's JSON report into a per-member cause table, compares the generated sources, and computes a toy action cache key per build action. |

The two "agents" differ in exactly the three ways real CI agents do:
- **Checkout path:** agent A builds in `/work/agent-a/repo`, agent B in `/work/agent-b/checkouts/repo`, which is 10 bytes longer.
- **Start time:** agent B starts at least two seconds after agent A finishes, so any read of the wall clock lands on a different second.
- **Hash seed:** CPython seeds `str` hashing randomly per process, and so does Rust's `RandomState`.
  The lab pins `PYTHONHASHSEED=1` for agent A and `PYTHONHASHSEED=2` for agent B so the Python generator always disagrees, instead of disagreeing with high probability; the Rust generator is left truly random.

`build.sh` logs every action it runs to `target/actions.tsv` as a caching executor would see it: name, declared inputs, output, the value of `SOURCE_DATE_EPOCH`, and the argv.
`summarize.py` hashes argv, environment, and the SHA-256 of each declared input into a key, and reports, for each action, whether agent B's key equals agent A's.
"hit" in that table means "agent B would have been served agent A's cached output"; "output bytes equal" says whether agent B's own execution produced the same bytes.
The key is a deliberately simplified stand-in for a Remote Execution API action digest, which also covers the platform, the output paths, and the full input tree (REAPI v2 `Action` carries `command_digest`, `input_root_digest`, and `platform`; `Command` carries `arguments`, `environment_variables`, `output_paths`, and `working_directory`, per `remote_execution.proto`).

## Observed Symptom

Stage 0 runs the build exactly as first written.
This block and every block below are copied from `expected/darwin-arm64.txt`, recorded on 2026-11-14 by `make record` on a macOS arm64 host whose Docker VM runs the lab image as linux/aarch64; a second, independent `make verify` run matched it byte for byte, and so did an independent verification run of `drill.sh` in the same image on 2026-11-15.

```text
=== Toolchain (Ubuntu 24.04 lab image) ===
binutils 2.42-4ubuntu2.10
diffoscope-minimal 259
g++ 4:13.2.0-7ubuntu1
gzip 1.12-1ubuntu3.2
rustc 1.75.0+dfsg0ubuntu1-0ubuntu7.4
tar 1.35+dfsg-3ubuntu0.4
ar default mode is deterministic (D): yes
gcc -dumpfullversion: 13.3.0
rustc --version: rustc 1.75.0

=== Compiler probes ===
SOURCE_DATE_EPOCH=1767225600 gives __DATE__ __TIME__: "Jan  1 2026" " " "00:00:00"
  and __TIMESTAMP__ (source mtime 2001-02-03 04:05:06 UTC): "Sat Feb  3 04:05:06 2001"
SOURCE_DATE_EPOCH=yesterday: exit 1, error names the variable: yes
two -ffile-prefix-map for the same prefix, __FILE__: "/second/probe.c"
source named by a relative path, __FILE__: "probe.c"
DW_AT_producer records -O2: yes, -ffile-prefix-map: no, -D: no
DW_AT_comp_dir after -ffile-prefix-map=<cwd>=.: .

=== Stage 0: as first written (fixes: none) ===
diffoscope exit status: 1
Differing members: 9
  member                             clock  path 
  (gzip header)                      yes    -    
  (tar listing)                      yes    -    
  pkg/BUILDINFO                      yes    -    
  pkg/bin/engine                     yes    yes  
  pkg/include/opcodes.h              -      -    
  pkg/lib/libengine.a (listing)      -      -    
  pkg/lib/libengine.a > engine.o     -      yes  
  pkg/lib/libengine.a > version.o    yes    yes  
  pkg/lib/routing.o                  -      yes  
Evidence (first changed line per member and cause):
  [clock] (gzip header)
    - gzip compressed data, was "engine.tar", last modified: <time-A>, from Unix
    + gzip compressed data, was "engine.tar", last modified: <time-B>, from Unix
  [clock] (tar listing)
    - drwxr-xr-x 0 lab (1001) lab (1001) 0 <time-A> pkg/
    + drwxr-xr-x 0 lab (1001) lab (1001) 0 <time-B> pkg/
  [clock] pkg/BUILDINFO
    - built_at=<time-A>
    + built_at=<time-B>
  [clock] pkg/bin/engine
    - engine built <time-A>
    + engine built <time-B>
  [path] pkg/bin/engine
    - $ROOT_A/src/version.cpp
    + $ROOT_B/src/version.cpp
  [path] pkg/lib/libengine.a > engine.o
    - $ROOT_A/src/engine.cpp
    + $ROOT_B/src/engine.cpp
  [clock] pkg/lib/libengine.a > version.o
    - engine built <time-A>
    + engine built <time-B>
  [path] pkg/lib/libengine.a > version.o
    - $ROOT_A/src/version.cpp
    + $ROOT_B/src/version.cpp
  [path] pkg/lib/routing.o
    - $ROOT_A/target/out/routes.rs
    + $ROOT_B/target/out/routes.rs
Generated sources:
  gen/opcodes.h          same names, different order
  target/out/routes.rs   same lines, different order
Toy action cache, agent B looking up agent A's results:
  action           key=argv+env+inputs  same key, root-normalized  output bytes equal
  compile-version  miss                 hit                        no
  gen-opcodes      hit                  hit                        no
  compile-engine   miss                 miss                       no
  compile-main     miss                 miss                       no
  link-engine      miss                 miss                       no
  gen-routes       hit                  hit                        no
  compile-routing  miss                 miss                       no
Release tarball sha256 equal: no
```

The `g++ 4:13.2.0-7ubuntu1` line is the Debian metapackage version; the compiler that ran is GCC 13.3.0, as `gcc -dumpfullversion` shows.

How to read the Stage 0 block:
- `summarize.py` replaces wall-clock values with `<time-A>` and `<time-B>` and the checkout roots with `$ROOT_A` and `$ROOT_B`, so the recording is stable; the raw lines held real times such as `Nov 15 2026 02:19:37`.
- A member marked `clock` has at least one changed line whose set of date or time tokens differs between the two sides; `path` means a changed line contains the agent's own checkout root.
  A member with neither mark still differs, so its cause is somewhere the two regexes cannot see.
- `diffoscope exit status: 1` means "different"; 0 means identical and 2 means diffoscope itself failed, which `drill.sh` treats as a lab failure.
- The two generator rows are the strongest clue in the block: their cache keys hit, which means argv, environment, and inputs were identical, and their outputs still differ.
  An action whose declared inputs are equal and whose output is not has an undeclared input.

## How Diffoscope Sees a Difference

Use this section as the reference while you work through the hints.

### The comparison tree

diffoscope identifies each input by content (it uses `file(1)` and its own format detectors), picks a comparator, and recurses through containers until it reaches leaves it can render as text.
For the drill's tarball the JSON report is a tree: the gzip file, then `filetype from file(1)` for the gzip header, then `engine.tar`, then `file list` (the tar listing with modes, owners, sizes, and mtimes), then one node per member.
An ELF member is rendered by running binutils tools on both sides and diffing their text: in the lab's report the leaves for `pkg/bin/engine` include `readelf --wide --file-header`, `--sections`, `--symbols`, `--relocs`, `--notes`, `--debug-dump=rawline`, `--debug-dump=info`, `strings --all --bytes=8`, `objdump --disassemble` per text section, and `readelf --wide --decompress --hex-dump=` or `--string-dump=` per data section.
A static library is an `ar` container, so `libengine.a` gets its own `file list` and then one subtree per object.

Three consequences shape the hunt:
- One root cause produces many leaves.
  A path that is 10 bytes longer moves every later string in `.debug_line_str`, which changes every offset that points into it, which changes relocations and `.debug_info`.
  In the lab's Stage 1 report, `version.o`, whose only remaining cause is the checkout path, still differs in its section headers, relocations, DWARF line program, debug info, and disassembly leaves, not only in its strings.
  Count root causes, not changed lines.
- `strings --all --bytes=8` and the `--string-dump` leaves are the fastest place to read a cause, because they show the leaked value itself rather than its knock-on offsets.
- A build ID is a content hash, so a differing `NT_GNU_BUILD_ID` note is always a symptom.
  diffoscope 259 also adds the comment `File has been modified after NT_GNU_BUILD_ID has been applied.` when the SHA-1 of the file with the 20 build-ID bytes zeroed does not equal the build ID (`_verify_build_id` in `comparators/elf.py`, referencing diffoscope issue 260).
  In the lab's JSON reports (a development run and the 2026-11-15 verification run) the comment is attached to the linked `engine` in Stages 0, 1, and 2, every differing stage, so the build ID that GNU ld 2.42 wrote did not match that recomputation even for an untouched binary; treat the comment as a heuristic, not as evidence of tampering.

### Commands and numbers to know

| Item | Platform | Value (as of 2026-11) | Why it matters |
| :--- | :--- | :--- | :--- |
| Exit status | Both | 0 same, 1 different, 2 trouble | Gate CI on 0; treat 2 as a broken check, never as "different". |
| `--json FILE`, `--text FILE`, `--html FILE`, `--html-dir DIR`, `--markdown FILE` | Both | `-` writes to stdout | JSON is the machine-readable tree the lab parses; HTML is the human report. |
| `--exclude-directory-metadata` | Both | `no` when comparing two directories, else `yes`; archive member metadata stays unless `recursive` | Why the tar listing's mtimes and owners show up even though the tarballs are files. |
| `--exclude GLOB`, `--exclude-command REGEX` | Both | none | Skip known-noisy files or slow tools; the help suggests `'^readelf.*\s--debug-dump=info'` as an expensive, usually secondary leaf. |
| `--diff-mask REGEX` | Both | none | Unifies matching substrings before diffing, for example a checkout root, to see what remains once a known leak is masked. |
| `--max-diff-block-lines` | Both | 1024 | Long diffs are truncated per block; raise it or use `--no-default-limits` when the cause is deep in a block. |
| `--max-report-size` | Both | 41943040 bytes | Whole-report cap. |
| `--max-diff-input-lines` | Both | 4194304 | Cap on lines fed to diff(1). |
| `--max-container-depth` | Both | 50, cannot be disabled | Recursion bound for nested archives. |
| `--diff-context` | Both | 7 lines | Context in each unified diff. |
| `--fuzzy-threshold` | Both | 110 in diffoscope 259; the 161 manual page says 60 | Matching of container members whose names differ; 0 disables. |
| `--timeout SECONDS` | Both | none, marked experimental | Stops recursing into further sub-archives after the budget. |
| `--list-missing-tools` | Both | n/a | Prints which external tools would enable more comparators; `diffoscope-minimal` lacks many by design. |
| `SOURCE_DATE_EPOCH` | Both | Integer seconds since the Unix epoch, as `date +%s` prints | GCC uses it for `__DATE__` and `__TIME__`; Clang 16 and later also for `__TIMESTAMP__`. |
| GCC on a malformed `SOURCE_DATE_EPOCH` | Both | Error, exit 1, when a date macro is expanded | Measured in the lab probe; the specification says builds SHOULD fail on malformed values. |
| `PYTHONHASHSEED` | Both | Unset or `random` randomizes `str` and `bytes` hashing; integer in [0, 4294967295]; 0 disables randomization | Explains set iteration order across processes. |
| GNU `ar` mode | Linux | `D` (deterministic) is the default only when binutils was configured with `--enable-deterministic-archives`; Ubuntu 24.04 `ar --help` marks `[D]` default | Elsewhere, pass `D` or set `ARFLAGS=Dcvr`. |
| `gzip -n` | Both | Do not save the original name and timestamp | Without it the header carries the input file's mtime. |
| GNU tar `--sort=name` / `--clamp-mtime` | Linux | Since tar 1.28 / 1.29 | Fix member order and cap mtimes at `SOURCE_DATE_EPOCH`. |
| rustc `--remap-path-scope` | Both | Stable since Rust 1.95.0 (2026-04-16); the lab's rustc 1.75 predates it | Limits remapping to `object` so diagnostics keep real paths. |
| Cargo `trim-paths` | Both | Unstable profile option, default `"none"`, tracking issue cargo#12137 | The future Cargo-level answer to build-script and registry paths. |

## Progressive Hint Ladder

Open one hint only after the previous one stops being useful.
The times are the drill clock.

> [!tip]- Hint 1 (minute 0 to 5): Classify before you fix
> Ignore the changed-line counts and read the member table.
> Every member marked `clock` contains a time that differs between agents, every member marked `path` contains the agent's own checkout root, and the members marked neither are either knock-on effects or a third cause.
> Write down one sentence per cause before you touch `build.sh`.

> [!tip]- Hint 2 (minute 5 to 10): Who reads the clock?
> There are four clock readers in Stage 0: the gzip header, the tar listing, `BUILDINFO`, and the version stamp compiled into `version.o` and therefore into `engine`.
> They are four different tools, so one knob must reach all of them: an environment variable the compiler reads natively, that the build script reads for `BUILDINFO`, and that the archive steps take as an explicit argument.
> The repository already ships `src/SOURCE_DATE_EPOCH` with the value `git log -1 --format=%ct` would give.

> [!tip]- Hint 3 (minute 10 to 15): Where does the path enter?
> The C++ rules pass absolute source paths, as CMake-generated rules do, so `__FILE__` expands to an absolute path and the debug info records an absolute compilation directory.
> The Rust crate includes a file from `OUT_DIR`, an absolute directory, so the panic location of the bounds check in the generated `route_port` and the debug info both carry it.
> Each compiler has one flag that rewrites a path prefix everywhere it records a path.

> [!tip]- Hint 4 (minute 15 to 20): The generators have equal keys and unequal outputs
> Look at the `gen-opcodes` and `gen-routes` rows: key `hit`, output bytes `no`.
> Compare the two generated files sorted line by line, and the two token multisets; the lab prints the verdict under `Generated sources:`.
> Ask what in each generator iterates a hashed container, and whether its order is defined.

> [!tip]- Hint 5 (minute 20 to 25): Fix order at the source, not with a seed
> Setting `PYTHONHASHSEED` would make the Python side stable, but it hides the bug from the next generator, does nothing for Rust, and changes the build's behavior depending on who runs it.
> Sort the entries, or use an ordered container, and also stop the tarball's member order from depending on `readdir`.

> [!tip]- Hint 6 (minute 25 to 30): Prove it, then read the cache table again
> Rebuild both agents, require `diffoscope` to exit 0 and the SHA-256 digests to match, and then explain why some actions still say `miss` in the raw-key column even though every output is equal.

## Model Solution

> [!success]- Solution
> ### Stage 1: pin every clock read to SOURCE_DATE_EPOCH
>
> The fix in `src/build.sh` when `FIXES` contains `epoch`:
>
> ```bash
> SOURCE_DATE_EPOCH="$(cat src/SOURCE_DATE_EPOCH)"
> export SOURCE_DATE_EPOCH
> BUILT_AT="$(date -u -d "@${SOURCE_DATE_EPOCH}" +%Y-%m-%dT%H:%M:%SZ)"
> TAR_FLAGS+=(--mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner)
> GZIP_FLAGS+=(-n)
> ```
>
> - GCC replaces the current time with `SOURCE_DATE_EPOCH` in `__DATE__` and `__TIME__`; the probe shows `"Jan  1 2026" " " "00:00:00"` for `1767225600`, which is 2026-01-01 00:00:00 UTC.
> - `BUILDINFO` is written by the build script, so the script must read the variable itself; no compiler flag reaches a `date` call.
> - `tar --mtime` sets every member's mtime and `--owner=0 --group=0 --numeric-owner` removes the builder's uid and gid (`lab (1001)` in the Stage 0 listing).
> - `gzip -n` stops gzip from storing the input file name and mtime in its header.
> - Exporting matters: the specification requires the variable to reach child processes through the environment and forbids unsetting it for children once present.
>
> ```text
> === Stage 1: pin clocks to SOURCE_DATE_EPOCH (fixes: epoch) ===
> diffoscope exit status: 1
> Differing members: 7
>   member                             clock  path 
>   (tar listing)                      -      -    
>   pkg/bin/engine                     -      yes  
>   pkg/include/opcodes.h              -      -    
>   pkg/lib/libengine.a (listing)      -      -    
>   pkg/lib/libengine.a > engine.o     -      yes  
>   pkg/lib/libengine.a > version.o    -      yes  
>   pkg/lib/routing.o                  -      yes  
> Evidence (first changed line per member and cause):
>   [path] pkg/bin/engine
>     - $ROOT_A/src/version.cpp
>     + $ROOT_B/src/version.cpp
>   [path] pkg/lib/libengine.a > engine.o
>     - $ROOT_A/src/engine.cpp
>     + $ROOT_B/src/engine.cpp
>   [path] pkg/lib/libengine.a > version.o
>     - $ROOT_A/src/version.cpp
>     + $ROOT_B/src/version.cpp
>   [path] pkg/lib/routing.o
>     - $ROOT_A/target/out/routes.rs
>     + $ROOT_B/target/out/routes.rs
> Generated sources:
>   gen/opcodes.h          same names, different order
>   target/out/routes.rs   same lines, different order
> Toy action cache, agent B looking up agent A's results:
>   action           key=argv+env+inputs  same key, root-normalized  output bytes equal
>   compile-version  miss                 hit                        no
>   gen-opcodes      hit                  hit                        no
>   compile-engine   miss                 miss                       no
>   compile-main     miss                 miss                       no
>   link-engine      miss                 miss                       no
>   gen-routes       hit                  hit                        no
>   compile-routing  miss                 miss                       no
> Release tarball sha256 equal: no
> ```
>
> The gzip header and `BUILDINFO` are gone from the table and no member has a `clock` mark.
> The tar listing still differs, but only in member sizes, because the members themselves still differ.
> `version.o` still differs although its source and its clock are now identical: the path is the remaining cause.
>
> ### Stage 2: map the checkout root out of every output
>
> The fix when `FIXES` contains `prefix`:
>
> ```bash
> CXXFLAGS+=(-ffile-prefix-map="${ROOT}"=.)
> RUSTFLAGS+=(--remap-path-prefix="${ROOT}"=.)
> ```
>
> - GCC's `-ffile-prefix-map=OLD=NEW` is "equivalent to specifying all the individual -f*-prefix-map options": `-fmacro-prefix-map` rewrites `__FILE__`, `__BASE_FILE__`, and `__builtin_FILE()`, `-fdebug-prefix-map` rewrites debug info, and `-fprofile-prefix-map` rewrites profile paths (`add_file_prefix_map` in GCC 13 `file-prefix-map.cc` registers all three).
> - The probe shows the debug info's compilation directory becoming `.` (`DW_AT_comp_dir after -ffile-prefix-map=<cwd>=.: .`).
> - The flag does not leak into the object through the producer string: GCC records code-generation switches in `DW_AT_producer` by default (`-grecord-gcc-switches`), but `gen_command_line_string` in GCC 13 `opts.cc` skips `-fdebug-prefix-map`, `-fmacro-prefix-map`, `-ffile-prefix-map`, `-fprofile-prefix-map`, `-fcanon-prefix-map`, `-D`, `-I`, and others, which the probe confirms (`-O2: yes, -ffile-prefix-map: no, -D: no`).
> - rustc's `--remap-path-prefix=FROM=TO` rewrites "all output, including compiler diagnostics, debug information, macro expansions"; it covers the `OUT_DIR` path inside the panic location of the generated bounds check and the debug info.
>
> ```text
> === Stage 2: map build paths out (fixes: epoch,prefix) ===
> diffoscope exit status: 1
> Differing members: 6
>   member                             clock  path 
>   (tar listing)                      -      -    
>   pkg/bin/engine                     -      -    
>   pkg/include/opcodes.h              -      -    
>   pkg/lib/libengine.a (listing)      -      -    
>   pkg/lib/libengine.a > engine.o     -      -    
>   pkg/lib/routing.o                  -      -    
> Generated sources:
>   gen/opcodes.h          same names, different order
>   target/out/routes.rs   same lines, different order
> Toy action cache, agent B looking up agent A's results:
>   action           key=argv+env+inputs  same key, root-normalized  output bytes equal
>   compile-version  miss                 hit                        yes
>   gen-opcodes      hit                  hit                        no
>   compile-engine   miss                 miss                       no
>   compile-main     miss                 miss                       no
>   link-engine      miss                 miss                       no
>   gen-routes       hit                  hit                        no
>   compile-routing  miss                 miss                       no
> Release tarball sha256 equal: no
> ```
>
> `version.o` has left the table: `compile-version` is now a deterministic action, and its `output bytes equal` flipped to `yes`.
> Six members still differ with no clock and no path in any changed line, which is the third cause.
>
> ### Stage 3: make generator and archive order independent of hashing
>
> The Python generator iterated `OPCODES`, a `set`.
> A set is "an unordered collection", and its iteration order follows the hashes of its elements, which CPython randomizes for `str` per process unless `PYTHONHASHSEED` is set.
> A `dict` would not have had the bug: dictionary order "is guaranteed to be insertion order" since Python 3.7.
> The order matters twice in `opcodes.h`: it permutes the `kOpNames` table, and because the enum values are assigned by position, `ADD` has a different numeric value in each build, which changes the dispatch code in `engine.o` and the enum's debug info in both `engine.o` and `main.o`, and would be an ABI break between two builds of one commit.
>
> The Rust generator iterated a `HashMap`, whose default `RandomState` seeds SipHash-1-3 with keys drawn once per thread from the OS (`hashmap_random_keys`) and incremented for every new `RandomState` (`library/std/src/hash/random.rs`), so its order differs in every process and no environment variable controls it.
>
> The fix when `FIXES` contains `sort`:
>
> ```bash
> GEN_ORDER+=(--sorted)      # sorted(OPCODES) in Python, entries.sort() in Rust
> TAR_FLAGS+=(--sort=name)   # tar member order no longer follows readdir
> ```
>
> ```text
> === Stage 3: sort generator and archive order (fixes: epoch,prefix,sort) ===
> diffoscope exit status: 0
> Differing members: 0
> Generated sources:
>   gen/opcodes.h          identical
>   target/out/routes.rs   identical
> Toy action cache, agent B looking up agent A's results:
>   action           key=argv+env+inputs  same key, root-normalized  output bytes equal
>   compile-version  miss                 hit                        yes
>   gen-opcodes      hit                  hit                        yes
>   compile-engine   miss                 hit                        yes
>   compile-main     miss                 hit                        yes
>   link-engine      hit                  hit                        yes
>   gen-routes       hit                  hit                        yes
>   compile-routing  miss                 hit                        yes
> Release tarball sha256 equal: yes
> ```
>
> diffoscope exits 0 and the SHA-256 digests match; `summarize.py` fails the lab if the two verdicts ever disagree.
>
> ### Why the cache hit rate moves, and why the raw keys still miss
>
> Read the `same key, root-normalized` column across the stages.
> `compile-version` hits from Stage 0 on, because its argv, environment, and single input were always equal after normalizing the root; it was never the action that missed.
> It was the action that poisoned others: whenever it re-executed (a cold cache, an eviction, a local build that does not upload), it produced new bytes, and every action that consumed `version.o` got a new input digest and missed.
> `compile-engine`, `compile-main`, and `link-engine` hit only in Stage 3, because their inputs include generated or compiled outputs that were non-deterministic until then.
> The cache was never broken; the build was feeding it a different key for the same work.
>
> The `key=argv+env+inputs` column is the second lesson.
> `link-engine` hits there in Stage 3 because its argv is relative, but `compile-engine`, `compile-main`, `compile-version`, and `compile-routing` still miss, because their argv contains the checkout root: absolute source paths and `-I` directories for the C++ compiles, `env OUT_DIR=<root>/target/out` for `compile-routing`, and now also the prefix-map flags themselves, `-ffile-prefix-map=/work/agent-a/repo=.` and `--remap-path-prefix=/work/agent-a/repo=.`.
> Making the output path-independent did not make the key path-independent.
> Three real-world answers, each owned elsewhere:
> - Run every action from a fixed, relative execution root, as Bazel does with its execroot; see [[Bazel-Skyframe-Bzlmod-and-Execution-Model]] and [[Sandboxing-and-Hermetic-Execution-Internals]].
> - Let the cache rewrite absolute paths before hashing: ccache's `base_dir` "converts absolute paths to relative paths before sending them to the compiler (or including them in the input hash)" for paths under the listed base directories; see [[Compiler-Caches-ccache-and-sccache]].
> - Build in one canonical path on every agent, which the Reproducible Builds documentation calls a workaround, because reprotest varies the build path and a fixed path makes reproducibility harder to assess.
>
> ```mermaid
> flowchart LR
>     Clock["wall clock<br/>__DATE__ __TIME__"] --> V["compile-version<br/>key equal, output differs"]
>     Path["checkout root<br/>__FILE__, comp_dir"] --> V
>     Seed["hash seed<br/>set and HashMap order"] --> G["gen-opcodes, gen-routes<br/>key equal, output differs"]
>     G --> E["compile-engine, compile-main<br/>input digest differs: miss"]
>     G --> R["compile-routing<br/>input digest differs: miss"]
>     V --> L["link-engine<br/>input digests differ: miss"]
>     E --> L
>     L --> T["engine.tar.gz<br/>sha256 differs"]
>     R --> T
> ```

## Core Takeaway

Non-determinism does not break the action that has it; it breaks every action downstream of it, because a cache key covers input digests and a non-deterministic output is a fresh input digest.
The hunt is a classification problem: find each value that differs between builds (a clock, a path, an iteration order), trace it to the one tool that reads it, and fix it at that tool with the narrowest knob, then require diffoscope to exit 0.
An action whose key matches and whose output differs has an undeclared input, and that row in a two-build comparison is the single most useful signal in the whole report.
The mechanisms and the policy side are owned by [[Hermetic-and-Reproducible-Builds]]; reading cache misses in a real Bazel or Buck2 build is owned by [[Diagnose-Bazel-and-Buck2-Cache-Misses]].

## Beyond the Lab: Leaks the Drill Does Not Plant

The three planted causes are the most common, not the only ones.
For each of the following the fix follows the same pattern: find the reader, give it a fixed value or remove the read.

| Source | Mechanism | Fix |
| :--- | :--- | :--- |
| `__TIMESTAMP__` | Expands to the source file's mtime, which is checkout time; the lab probe shows GCC 13.3 ignoring `SOURCE_DATE_EPOCH` for it. | Clang 16 and later replace it with `SOURCE_DATE_EPOCH`; for GCC, remove it, or redact it as Bazel's auto-configured toolchain does. |
| Redacted macros | Bazel's `unix_cc_configure.bzl` puts `-Wno-builtin-macro-redefined`, `-D__DATE__="redacted"`, `-D__TIMESTAMP__="redacted"`, and `-D__TIME__="redacted"` in `unfiltered_compile_flags`, with the comment "Use linkstamping instead of these compiler symbols". | Inject build metadata at link time from a stamped file, so only the final link is non-cacheable. |
| Detecting date macros | GCC `-Wdate-time` warns "when macros `__TIME__`, `__DATE__` or `__TIMESTAMP__` are encountered". | Turn it on with `-Werror=date-time` in CI. |
| GCC random seed | `-frandom-seed=STRING` replaces the random numbers GCC uses "in generating certain symbol names that have to be different in every compiled file" and in coverage stamps. | Bazel's `random_seed` feature passes `-frandom-seed=%{output_file}`; the seed must differ per file. |
| Archives | `ar` without `D` stores uids, gids, mtimes, and modes; zip stores extra time fields; tar stores owners, mtimes, and readdir order. | `ar D` (or `ARFLAGS=Dcvr` and `ranlib -D`), `zip -X`, `tar --sort=name --mtime --owner=0 --group=0 --numeric-owner`, and for pax format `--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime`. |
| Darwin `ld64` | rules_cc sets `ZERO_AR_DATE=1` in the link environment and a `macos_reproducible` feature passing `-ffile-compilation-dir=.`. | Use the toolchain feature rather than per-target flags. |
| Python bytecode | `.pyc` files default to `TIMESTAMP` invalidation, which embeds the source mtime; `py_compile` defaults to `CHECKED_HASH` when `SOURCE_DATE_EPOCH` is set (since 3.7.2 it sets the default rather than forcing it). | Export `SOURCE_DATE_EPOCH` before byte-compiling. |
| Cargo paths | Registry sources live under `~/.cargo/registry`, and the rustc book warns that "generated code introduced by Cargo's build script may still contain un-remapped paths". | Remap both the workspace and `CARGO_HOME`; Cargo's unstable `trim-paths` maps workspace packages to `.`, registry sources to `/cargo/registry/<id>`, the sysroot to `/rustc/<commit>`, and build-directory outputs to `/cargo/build-dir`. |
| Locale and timezone | `sort` and formatted dates depend on `LC_ALL` and `TZ`; the Reproducible Builds documentation warns that locale affects sorting functions. | Pin `LC_ALL=C` and `TZ=UTC` in generator steps, and defer user-visible formatting to runtime. |

## Pitfalls

> [!warning] Fixing the seed instead of the order
> `PYTHONHASHSEED=0` makes one generator stable and leaves the bug in the code, where the next caller without the variable reintroduces it.
> It does nothing for Rust, whose `RandomState` has no environment override, or for Perl or Ruby hash iteration, which have their own seeds.
> Sort at the point of emission, or use an ordered container (`BTreeMap`, `sorted()`, an insertion-ordered `dict`).

> [!warning] Reading changed-line counts as severity
> The longest diffs in the lab's report are relocations and DWARF line programs, and they are all knock-on effects of a 10-byte path difference or a permuted string table.
> Fix the cause whose evidence is the leaked value itself, then rerun; most of the noise disappears with it.

> [!warning] Assuming the prefix-map flag is free
> The flag fixes the artifact and pollutes the action key, because it contains the absolute root.
> The Stage 3 raw-key column shows every compile still missing for that reason.
> Decide how keys become path-independent (relative execroot, cache-side normalization, canonical build path) at the same time as you add the flag.

> [!warning] Mapping only one prefix
> GCC's maps are a list searched from the most recently added entry and stop at the first match (`add_prefix_map` prepends; `remap_filename` breaks on the first match), and rustc applies "the last matching one".
> The probe shows the effect: with two maps for the same prefix, `__FILE__` became `/second/probe.c`.
> Order your maps from general to specific, and remember that rustc's documentation recommends remapping both relative and absolute forms.

> [!warning] Silencing the symptom in diffoscope
> `--exclude`, `--exclude-command`, and `--diff-mask` are for investigation, for example masking a known path to see what else remains.
> A CI gate that excludes the differing leaf passes on artifacts that are not identical, which defeats the gate.

> [!warning] Trusting one rebuild on one machine
> Two builds on the same agent share the path, the user, the locale, and often the second; the lab's agents differ in path, start time, and hash seed on purpose.
> A same-machine double build would have found only the clock and the seed.

> [!warning] Breaking debuggability with `=.`
> After `-ffile-prefix-map=ROOT=.` the debug info names sources relative to `.`, so a debugger on another machine needs a source map (`set substitute-path` in GDB, `settings set target.source-map` in LLDB) or a fixed install prefix such as `/usr/src/engine` as the map target.

## At Scale

From one developer to 1,000 to 50,000 engineers the drill turns into a standing control, because every new code generator, archive step, or toolchain upgrade can reintroduce a leak and the first symptom is a slow decline in cache hit rate.

- **Detect continuously.**
  Run a nightly double build of the release targets with varied checkout paths, start times, users, and locales, compare outputs by digest, and run diffoscope only on artifacts whose digests differ, because diffoscope's cost is proportional to the size of what differs.
- **Diff execution logs before artifacts.**
  In Bazel, record `--execution_log_compact_file` on two machines after `bazel clean` and compare the parsed logs; an action with equal inputs and different output digests is the same signal as the lab's generator rows, found without unpacking anything.
  The procedure belongs to [[Diagnose-Bazel-and-Buck2-Cache-Misses]].
- **Own the toolchain defaults.**
  Put `-ffile-prefix-map`, `-frandom-seed`, `-Werror=date-time`, `ARFLAGS=Dcvr`, and the tar and gzip flags in the paved-road toolchain, not in each project, so one platform team fixes a leak once.
- **Treat generators as compilers.**
  Code generators (IDL, protocol tables, build scripts) are where hash-order bugs live; require sorted output and a determinism test for every generator, the cheapest of which is running it twice with different `PYTHONHASHSEED` values and comparing.
- **Budget the diff.**
  Large artifacts can make diffoscope slow; bound it with `--timeout` and `--exclude-command '^readelf.*\s--debug-dump=info'` in triage, and run the full comparison only when the bounded one is inconclusive.

Decision rules:
- If an action's key matches and its output differs, it is non-deterministic: fix it before tuning anything else, because it lowers the hit rate of everything downstream.
- If an action's key differs and its output matches, the key is too wide (paths, timestamps, or environment in argv): normalize the key, not the output.
- If both match and the hit rate is still low, the problem is cache capacity, eviction, or upload policy, which [[Remote-Execution-API-and-Remote-Caching]] covers.
- If a release must be independently verifiable, require bit-for-bit equality from two builders and publish the digest; this is the policy side of [[Hermetic-and-Reproducible-Builds]].

## Practice Drill

> [!tip] Practice Drill: Break it again, one leak at a time
> Copy `code/06-Build-Systems/drill_diffoscope_determinism/` to a scratch directory, run `bash run.sh` once, and confirm it matches `expected/darwin-arm64.txt`.
> Then make one change at a time in the copy, predict the member table and the cache table first, and rerun.
> 1. Replace `ar rcs` with `ar rcsU` in `build.sh`: predict which member gains a `clock` mark in Stage 0 and which stage first shows `libengine.a (listing)` as identical.
> 2. Add `const char* t = __TIMESTAMP__;` to `version.cpp`: predict that Stage 3 is no longer identical, and explain why `SOURCE_DATE_EPOCH` does not help with GCC 13 when `drill.sh` copies the source with `cp -R`, which gives each agent's copy a new mtime, just as a fresh `git checkout` does.
> 3. Change `--remap-path-prefix="${ROOT}"=.` to `--remap-path-prefix=/work=.`: predict the remaining path in `routing.o` for each agent.
> 4. Replace the Rust `HashMap` with a `BTreeMap` and drop `entries.sort()`: predict the `gen-routes` row.
> 5. Drop `--sort=name` from the tar flags only: predict whether Stage 3 still reaches `diffoscope exit status: 0` in this container, and explain why the answer is not a proof that the flag is unnecessary.
> Target time: 20 minutes for steps 1 to 4.

## Interview Questions

> [!question] L5
> A C++ library builds fine, but no CI agent ever gets a remote cache hit on anything that links it.
> Its `version.cpp` contains `__DATE__ " " __TIME__`.
> Explain the mechanism and give two fixes.
>
> > [!success]- Answer
> > The compile of `version.cpp` has the same key on every agent, so it can hit; but whenever it executes it embeds the current time, so its output digest is new.
> > Every action that consumes that object (the archive, every link) has a new input digest and misses.
> > Fix one: export `SOURCE_DATE_EPOCH` from the commit time; GCC substitutes it in `__DATE__` and `__TIME__`, and Clang 16 and later also in `__TIMESTAMP__`.
> > Fix two: stop embedding build time in compiled code, and inject it at link time from a stamped file (Bazel's linkstamping), so only the final link step varies.
> > Add `-Werror=date-time` so the macros cannot come back.

> [!question] L5: Predict the output
> In the lab image (GCC 13.3), `probe.c` contains `const char* f = __FILE__;` and lives in `/tmp/x`.
> What does `gcc -E -P -ffile-prefix-map=/tmp/x=/first -ffile-prefix-map=/tmp/x=/second /tmp/x/probe.c` print for `f`, and what does it print if you compile with `probe.c` as a relative path and no map?
>
> > [!success]- Answer
> > `"/second/probe.c"`, then `"probe.c"`; both are lines of the lab's compiler probes.
> > GCC prepends each new map to a list and `remap_filename` stops at the first match, so the last map given for a prefix wins.
> > `__FILE__` is the path as the compiler was given it, so a relative invocation needs no macro map at all; debug info still records the absolute compilation directory unless a debug or file prefix map rewrites it.

> [!question] L5
> Why does GNU `ar rcs` produce identical archives on Ubuntu but not necessarily on another distribution or a self-built binutils?
>
> > [!success]- Answer
> > Deterministic mode (`D`) zeroes uids, gids, and timestamps and uses consistent modes, and it is the default only when binutils was configured with `--enable-deterministic-archives`.
> > Ubuntu 24.04's `ar --help` marks `[D]` as the default, which the lab prints; a binutils built without that option defaults to `U` and stores real metadata.
> > Pass `D` explicitly (`ARFLAGS=Dcvr`, `ranlib -D`), or post-process with `objcopy --enable-deterministic-archives`; none of these fix member order, which comes from the command line.

> [!question] L6: Debug this
> A two-build comparison of a generator action shows identical argv, identical environment, identical input digests, and different output digests.
> The generator is a Python script that writes a C header from a table of names.
> What is wrong, how do you confirm it in one minute, and what do you change?
>
> > [!success]- Answer
> > An action whose declared inputs are equal and whose output differs has an undeclared input; in a Python generator the usual one is the per-process `str` hash seed, which orders `set` iteration.
> > Confirm by running the generator twice with `PYTHONHASHSEED=1` and `PYTHONHASHSEED=2` and diffing; if the outputs differ and are equal when sorted (the lab's `same names, different order`), the order is the cause.
> > Fix it in the code: iterate `sorted(names)` or keep the table in a `dict` or list, whose order is insertion order.
> > Also check what the order feeds: if enum values are assigned by position, the two builds also disagree on numeric values, which is an ABI break, not only a cache problem.

> [!question] L6
> diffoscope reports hundreds of changed lines in `readelf --debug-dump=rawline`, `--relocs`, and `objdump --disassemble` for an object, and one changed line in `strings`, which shows a different checkout path.
> Where do you start, and why?
>
> > [!success]- Answer
> > Start with the `strings` line: it shows a leaked value, while the other leaves show offsets and addresses that moved because that value has a different length.
> > A longer path shifts every later string in `.debug_line_str` and `.debug_str`, which changes the offsets that point into them, which changes relocations and DWARF line programs.
> > Fix the path with `-ffile-prefix-map` (C and C++) or `--remap-path-prefix` (Rust), rebuild, and rerun diffoscope; most or all of the large leaves should disappear with it.
> > If they do not, `--diff-mask` with the path pattern shows what remains independently of the path.

> [!question] L6
> After adding `-ffile-prefix-map=$PWD=.`, the objects from two agents are byte-identical, yet the compile actions still miss in a cache keyed on argv.
> Why, and what are your options?
>
> > [!success]- Answer
> > The flag contains the absolute checkout path, as do the absolute source paths, so the argv, and therefore the key, still differ by agent; the lab's Stage 3 shows every compile with `miss` in the raw column and `hit` in the root-normalized column.
> > Options: run actions from a fixed relative execution root (Bazel's execroot), so neither the sources nor the map need absolute paths; normalize the key in the cache (ccache `base_dir` rewrites absolute paths under the base directory before hashing); or build in one canonical path on all agents.
> > The trade-off of the canonical path is that it hides path leaks, so a double build with varied paths stops testing them.

> [!question] L6
> A Rust crate's build script iterates a `HashMap` to write generated code.
> Why does `cargo build` give different object files on every run, and why is setting an environment variable not a fix?
>
> > [!success]- Answer
> > `HashMap` uses `RandomState`, which seeds SipHash-1-3 with keys drawn per thread from the OS random source and incremented per instance, so iteration order differs in every process.
> > There is no environment override for `RandomState`, unlike `PYTHONHASHSEED`; the order must be fixed in the code with `BTreeMap`, an insertion-ordered map, or a sort before emitting.
> > Separately, code included from `OUT_DIR` embeds absolute build-directory paths in panic locations and debug info, so remap with `--remap-path-prefix` (and, on Rust 1.95.0 and later, limit it with `--remap-path-scope=object` if diagnostics should keep real paths).

> [!question] L7
> Design the reproducibility control for a 2,000-engineer monorepo whose cache hit rate dropped from 92 percent to 70 percent over a quarter.
>
> > [!success]- Answer
> > First separate the three failure classes with data: key equal and output different (non-deterministic actions), key different and output equal (keys too wide), and both equal (cache capacity or upload policy); execution logs from two clean builds on two machines give all three.
> > Rank non-deterministic actions by fan-out, because one stamped object feeding every link costs more than a leaf action.
> > Make the fixes toolchain defaults owned by the platform team: `SOURCE_DATE_EPOCH` exported from the commit time, prefix maps, `-frandom-seed`, `-Werror=date-time`, deterministic archive flags, and link-time stamping.
> > Add a nightly double build with varied paths, users, locales, times, and hash seeds; compare digests first and run diffoscope with a timeout only on mismatches, and open the ticket against the action, not the artifact.
> > Require a determinism test for every code generator, and alert on hit-rate decline per action mnemonic so a regression is attributed within a day.

> [!question] L7
> A regulator asks you to prove that the shipped `engine` binary was built from the published source.
> What does that require from the build, and what does diffoscope contribute?
>
> > [!success]- Answer
> > It requires bit-for-bit reproducibility across independent builders: the same source, the same declared toolchain, and an environment description, so a third party can rebuild and compare digests.
> > Every input that is not source must be pinned and recorded: compiler versions (the lab prints them), `SOURCE_DATE_EPOCH`, build path policy, and flags.
> > diffoscope is the tool for the failure case: when digests differ, it shows which member and which section differ, so the difference can be attributed to an environment leak rather than to a modified source.
> > A matching digest proves equality; diffoscope output is evidence for explaining a mismatch, not a substitute for it.

> [!question] L8+
> Your organization wants every build action to be reproducible by default across C++, Rust, Python, and Go, with no per-team work.
> What architecture gets you there, and what do you accept as residual risk?
>
> > [!success]- Answer
> > Enforce at the execution layer: sandboxed actions with declared inputs, a fixed execution root, a scrubbed environment with `SOURCE_DATE_EPOCH`, `TZ=UTC`, and `LC_ALL=C` injected, and toolchains whose wrappers add prefix maps and deterministic archive flags.
> > Detect at the cache layer: sample a fraction of cache hits, re-execute them on a different worker, and compare output digests, which turns non-determinism into a metric per action type instead of a quarterly surprise.
> > Accept residual risk where tools are inherently non-deterministic (some LTO or PGO pipelines, signing with timestamps, third-party binaries), mark those actions non-cacheable or post-process them, and keep that list short and owned.
> > The cost is execution-time overhead for sampling and some loss of debuggability from path mapping, which source maps and a canonical install prefix recover.

## Further Reading

- [diffoscope](https://diffoscope.org/): the tool and its supported formats; run `diffoscope --help` for the defaults of the installed version, which differ from older manual pages.
- [SOURCE_DATE_EPOCH specification, revision 1.1](https://reproducible-builds.org/specs/source-date-epoch/): the exact contract for producers and consumers, including clamping and malformed values.
- [Reproducible Builds: build path](https://reproducible-builds.org/docs/build-path/): prefix maps per compiler version and why a fixed path is only a workaround.
- [Reproducible Builds: archive metadata](https://reproducible-builds.org/docs/archives/): the tar, ar, and zip recipes used in Stage 1 and Stage 3.
- [Reproducible Builds: stable order for outputs](https://reproducible-builds.org/docs/stable-outputs/): hash-order iteration across languages.
- [GCC prefix map options](https://gcc.gnu.org/onlinedocs/gcc/Overall-Options.html) and [`file-prefix-map.cc`](https://github.com/gcc-mirror/gcc/blob/releases/gcc-13/gcc/file-prefix-map.cc): what the flags promise and how matching works.
- [GCC `opts.cc`](https://github.com/gcc-mirror/gcc/blob/releases/gcc-13/gcc/opts.cc): `gen_command_line_string`, the list of options kept out of `DW_AT_producer`.
- [The rustc book: Remap source paths](https://doc.rust-lang.org/rustc/remap-source-paths.html): scopes, last-match rule, and the build-script caveat.
- [Bazel: debugging remote cache hits](https://bazel.build/remote/cache-remote): the execution-log procedure for the at-scale version of this drill.
- [ccache manual](https://ccache.dev/manual/latest.html): `base_dir` and `hash_dir`, the cache-side answer to path-dependent keys.

## See Also

- [[Hermetic-and-Reproducible-Builds]] for the variation taxonomy and policy.
- [[Diagnose-Bazel-and-Buck2-Cache-Misses]] for execution-log diffing in Bazel and Buck2.
- [[Remote-Execution-API-and-Remote-Caching]] for how action digests are formed.
- [[Compiler-Caches-ccache-and-sccache]] for `base_dir`, direct mode, and cache-side normalization.
- [[Cargo-Build-Graph-and-Rustc-Pipelining]] for build scripts, `OUT_DIR`, and crate metadata hashes.
- [[Sandboxing-and-Hermetic-Execution-Internals]] for fixed execution roots and undeclared inputs.
- [[Drill-Build-Systems-Fix-Broken-Incremental-Depfile]] for the next drill.

## Related

- Prerequisites: [[Hermetic-and-Reproducible-Builds]], [[Diagnose-Bazel-and-Buck2-Cache-Misses]]
- Next: [[Drill-Build-Systems-Fix-Broken-Incremental-Depfile]]
- Lab: `code/06-Build-Systems/drill_diffoscope_determinism/` (requires: docker, network)
