---
type: problem
track: [sde, distinguished, low-latency]
level:
status: solid
last_reviewed:
sources:
  - "Python Software Foundation, Memory Management (the pymalloc allocator, PYTHONMALLOC, default allocators, free-threaded mimalloc), Python 3 C API documentation, 2026, https://docs.python.org/3/c-api/memory.html"
  - "Python Software Foundation, faulthandler: Dump the Python traceback (enable, -X faulthandler, c_stack, dump_c_stack added in 3.14), Python 3 library documentation, 2026, https://docs.python.org/3/library/faulthandler.html"
  - "Python Software Foundation, ctypes: A foreign function library for Python (bytes passed as pointer to their data, CDLL releases the GIL), Python 3 library documentation, 2026, https://docs.python.org/3/library/ctypes.html"
  - "Python Software Foundation, Debugging C API extensions and CPython Internals with GDB (python-gdb.py, py-bt, python3-dbg), Python HOWTOs, 2026, https://docs.python.org/3/howto/gdb_helpers.html"
  - "Michael Kerrisk et al., mallopt(3): M_MMAP_THRESHOLD dynamic adjustment, DEFAULT_MMAP_THRESHOLD_MAX, M_TRIM_THRESHOLD, Linux man-pages project, 2026, https://man7.org/linux/man-pages/man3/mallopt.3.html"
  - "The Rust Project Developers, The Rust Unstable Book: sanitizer (-Zsanitizer=address, supported targets, -Zbuild-std), The Rust Project, 2026, https://doc.rust-lang.org/beta/unstable-book/compiler-flags/sanitizer.html"
  - "The Rust Project Developers, rustc_codegen_ssa back/link.rs (add_sanitizer_libraries, link_sanitizer_runtime), rust-lang/rust master, 2026, https://github.com/rust-lang/rust/blob/master/compiler/rustc_codegen_ssa/src/back/link.rs"
  - "LLVM Project, AddressSanitizer (bug classes, 2x slowdown, LeakSanitizer on by default on Linux, report format), Clang documentation, 2026, https://clang.llvm.org/docs/AddressSanitizer.html"
  - "LLVM Project, compiler-rt asan_linux.cpp (AsanCheckDynamicRTPrereqs: ASan runtime does not come first in initial library list), llvm-project main, 2026, https://github.com/llvm/llvm-project/blob/main/compiler-rt/lib/asan/asan_linux.cpp"
  - "Google Sanitizers Project, AddressSanitizerAsDso (LD_PRELOAD of the shared runtime, version matching, GCC defaults to the DSO), google/sanitizers wiki, 2026, https://github.com/google/sanitizers/wiki/AddressSanitizerAsDso"
  - "Google Sanitizers Project, SanitizerCommonFlags (detect_leaks, symbolize, abort_on_error) and AddressSanitizerFlags (quarantine_size_mb, verify_asan_link_order), google/sanitizers wiki, 2026, https://github.com/google/sanitizers/wiki/SanitizerCommonFlags"
  - "PyO3 Project, PyO3 0.30.1 API documentation: PyBytesMethods::as_bytes and pybacked::PyBackedBytes, docs.rs, 2026, https://docs.rs/pyo3/latest/pyo3/types/trait.PyBytesMethods.html"
  - "PyO3 Project, PyO3 changelog 0.26.0 (Python::with_gil renamed to Python::attach, allow_threads to detach), pyo3.rs, 2025, https://pyo3.rs/latest/changelog"
  - "LLVM Project, compiler-rt asan_flags.inc and asan_flags.cpp (quarantine_size_mb default -1 resolved to 256 MB, 16 MB in low-memory builds), llvm-project main, 2026, https://github.com/llvm/llvm-project/blob/main/compiler-rt/lib/asan/asan_flags.cpp"
  - "Python Software Foundation, CPython Python/traceback.c (Current thread's C stack trace header), python/cpython main, 2026, https://github.com/python/cpython/blob/main/Python/traceback.c"
  - "Python Software Foundation, CPython Objects/bytesobject.c (PyObject_Malloc in _PyBytes_FromSize, bytes_repeat) and Include/cpython/bytesobject.h (ob_shash, ob_sval), python/cpython 3.15 branch, 2026, https://github.com/python/cpython/blob/3.15/Objects/bytesobject.c"
  - "Free Software Foundation, Debugging with GDB: Your Program's Arguments (run with arguments, set args), GNU Project, 2026, https://sourceware.org/gdb/current/onlinedocs/gdb.html/Arguments.html"
  - "PyO3 Project, PyO3 0.30.1 API documentation: pybacked::PyBackedBytes, docs.rs, 2026, https://docs.rs/pyo3/latest/pyo3/pybacked/struct.PyBackedBytes.html"
---

# Drill: Debugging Mixed Python-Rust FFI Segfault

> [!summary] Scope
> This drill hands you a Python service whose Rust extension segfaults on some inputs, on some hosts, and only the first time, and asks you to prove which runtime freed the memory and which one used it.
> It exercises `faulthandler`, GDB with Python-aware scripting, and AddressSanitizer preloaded into an uninstrumented interpreter, across a ctypes-to-Rust `cdylib` boundary.
> The lesson is that a pointer shared between two runtimes has two owners only in the programmer's head: Python's reference count decides when the memory dies, and three different allocator paths decide whether that death is a crash, a wrong answer, or nothing at all.

The general method for FFI boundary bugs (ABI mismatches, unwinding, alignment, GIL rules) is owned by [[Rust-FFI-Boundary-Debugging-Across-Cpp-and-Python]], the prerequisite for this drill.
Python-side crash dumping is owned by [[Python-faulthandler-and-Crash-Diagnostics]], GDB on the interpreter by [[Python-Debugging-Hung-Processes-and-C-Extensions]], and shadow-memory mechanics by [[Cpp-AddressSanitizer-and-LeakSanitizer-Internals]] and [[Rust-Miri-Sanitizers-Loom-and-Cargo-Fuzz]].
Reconstructing a use-after-free from a core without a sanitizer is [[Drill-Debugging-Use-After-Free-Core-Dump]].
This note owns only the cross-runtime version of the problem: who owns the bytes, why the symptom is intermittent, and how to make each tool see across the boundary.

## Reproduction

The drill lives in `code/03-Debugging/drill_ffi_segfault/`.

- `ticktape/` is a dependency-free Rust crate built as a `cdylib`, so `cargo build --offline` needs no registry.
  `tape_feed(tape, data, len)` stores the raw `data` pointer in a heap `Tape` and returns; `tape_sum(tape)` later reads every byte with `ptr::read_volatile`, one 1-byte load at a time.
  `tape_feed_copy` is the fixed entry point: it copies the bytes into a `Box<[u8]>` the `Tape` owns.
- `driver.py` loads the library with `ctypes.CDLL`, declares `argtypes` as `(c_void_p, c_char_p, c_size_t)`, and passes a Python `bytes` object for `data`.
  ctypes passes a bytes argument "as pointer to the memory block that contains their data", so Rust receives a pointer into the object's inline buffer.
  The driver then does what any Python code may do: it drops its last reference (`del payload`) and calls `tape_sum` later.
- `run.sh` builds the library twice with `cargo +nightly` (plain, then `-Zsanitizer=address`), runs four scenarios, then triages the crashing one with `-X faulthandler` (plus a forced `fault` mode, a read at the unmapped address 16, so faulthandler is exercised on macOS too), with `gdb_triage.py` under `gdb -batch`, and with ASan preloaded into `python3`.
  `digest.py` and `gdb_triage.py` print derived, address-free facts so the output is stable under ASLR.

The lab declares `# requires: nightly`.
`nightly` is needed because `-Zsanitizer=address` is an unstable compiler flag documented in the Rust Unstable Book.
The lab runs natively across platforms: on macOS with rustup nightly and dynamic library insertion, and on Linux with shared runtime preloading and GDB triage.
The harness has no requirement token for the Linux extras, so `run.sh` checks them itself and fails loudly when they are missing: Linux needs `gdb` built with Python and a shared ASan runtime (a `libclang_rt.asan` of rustc's LLVM major version, or GCC's `libasan.so`).
Run verification or record golden output with:

```sh
cd code && python3 -m _harness.verify --record 03-Debugging/drill_ffi_segfault/
```

As of 2026-11, the lab records `expected/darwin-arm64.txt` (macOS, Python 3.15.2, rustc 1.101.0-nightly) and `expected/linux-aarch64.txt` (Ubuntu 24.04 container, Python 3.12.3, gdb 15.1, glibc 2.39, rustc 1.102.0-nightly with LLVM 23.1.5, GCC 13 `libasan.so`).
Every output block below is copied from one of them and names its platform.
On macOS, `make verify` cannot exercise the glibc-specific claims (the cold `large` crash and the `warm` contrast) or GDB, because macOS has no gdb and its allocator does not unmap the chunk; those are proven by the Linux golden file, which needs a native Linux host with nightly because the harness container has no Rust.

## Observed Symptom

The production report reads: "the tick service segfaults a few times a week, never in staging, never on a laptop, and the core shows a Rust frame".
Running the four driver scenarios shows the shape of the problem.
This output is copied from the lab's `expected/darwin-arm64.txt` Stage 1 (as of 2026-11, macOS):

```text
=== Stage 1: the symptom depends on payload size and allocator history ===
small: replacement object reuses the freed block: yes
small: tape_sum returned (no crash)
small: sum equals the freed payload's sum: no
small: sum equals the replacement's sum: yes
small: process exit status 0
warm: reading 1048576 bytes through the stale pointer
warm: tape_sum returned (no crash)
warm: sum equals the freed payload's sum: yes
warm: process exit status 0
large: reading 1048576 bytes through the stale pointer
large: tape_sum returned (no crash)
large: sum equals the freed payload's sum: yes
large: process exit status 0
fixed-small: sum equals the original payload's sum: yes
fixed-large: sum equals the original payload's sum: yes
fixed: process exit status 0
```

On Linux (Ubuntu 24.04, glibc 2.39, linux-aarch64), `small`, `warm`, and `fixed` behave identically, but the cold `large` read segfaults immediately because glibc unmaps the 1 MiB chunk on `free`.
Every Linux block in this note is copied from the lab's `expected/linux-aarch64.txt`, recorded by running `run.sh` with the real Rust library in an Ubuntu 24.04 container (rustc 1.102.0-nightly with LLVM 23.1.5, Python 3.12.3, gdb 15.1, GCC 13's `libasan.so`); two runs were byte-identical (as of 2026-11, Linux):

```text
=== Stage 1: the symptom depends on payload size and allocator history ===
small: replacement object reuses the freed block: yes
small: tape_sum returned (no crash)
small: sum equals the freed payload's sum: no
small: sum equals the replacement's sum: yes
small: process exit status 0
warm: reading 1048576 bytes through the stale pointer
warm: tape_sum returned (no crash)
warm: sum equals the freed payload's sum: yes
warm: process exit status 0
large: reading 1048576 bytes through the stale pointer
large: process killed by signal 11
fixed-small: sum equals the original payload's sum: yes
fixed-large: sum equals the original payload's sum: yes
fixed: process exit status 0
```

So one bug has three faces: a wrong answer with exit 0, a correct-looking answer with exit 0, and a `SIGSEGV`, and which face you get depends on the payload size, on what the process freed earlier, and on the operating system.

## Progressive Hint Ladder

> [!tip]- Hint 1: Find the Python line before you open a debugger
> Run the crashing scenario with `python3 -X faulthandler` (or `PYTHONFAULTHANDLER=1`).
> On `SIGSEGV`, faulthandler prints `Fatal Python error: Segmentation fault` and the Python traceback of the faulting thread before the process dies.
> A ctypes foreign call adds no Python frame, so the innermost frame is the Python line that called into Rust, not the Rust function.
> Ask what that line's arguments point to, and who keeps them alive.

> [!tip]- Hint 2: Read the fault address, not just the faulting function
> In GDB, `print $_siginfo._sifields._sigfault.si_addr` gives the address the load touched, and `info proc mappings` tells you whether anything is mapped there.
> Have the driver write the buffer pointer Rust stored before the call, and compare.
> If the fault address is byte 0 of that buffer and no mapping contains it, the memory was returned to the kernel between `tape_feed` and `tape_sum`.

> [!tip]- Hint 3: Ask which allocator served the object
> A CPython `bytes` object is allocated with `PyObject_Malloc`.
> In the default build, pymalloc serves requests of at most 512 bytes from 1 MiB arenas obtained with `mmap`, and falls back to `PyMem_RawMalloc`, which is `malloc`, for larger ones.
> glibc then serves a `malloc` request with its own `mmap` when it is above `M_MMAP_THRESHOLD` (initially 128 KiB), and returns it with `munmap` on `free`.
> Map each driver scenario onto that chain and predict which one can fault.

> [!tip]- Hint 4: The threshold moves
> mallopt(3): glibc uses a dynamic mmap threshold, and when a block larger than the current threshold (and at most `DEFAULT_MMAP_THRESHOLD_MAX`, 32 MiB on 64-bit) is freed, the threshold rises to that block's size.
> The `warm` scenario frees one 1 MiB `bytes` object first.
> What does the next 1 MiB request get, and what does `free` do with it?

> [!tip]- Hint 5: Make the sanitizer see Python's frees
> On Linux, rustc links the ASan runtime only into executables, so an instrumented `cdylib` inside an uninstrumented `python3` needs a shared runtime in `LD_PRELOAD`.
> ASan then replaces `malloc` and `free`, but pymalloc's arenas still come from `mmap` and look like valid memory to it.
> Which environment variable makes CPython send small objects to `malloc` too, and which flag silences the interpreter's leak reports?

## Model Solution

> [!success]- Solution
> **Step 1: faulthandler points at the boundary.**
> Stage 2 of `run.sh` reruns the `large` scenario with `-X faulthandler`, then a forced `fault` scenario that reads the unmapped address 16 and so crashes on every platform.
> On macOS, where the large allocation is not unmapped, `large` does not fault and faulthandler produces no report; the forced fault shows what faulthandler prints there.
> Copied from `expected/darwin-arm64.txt` (as of 2026-11, macOS, Python 3.15.2):
>
> ```text
> === Stage 2: faulthandler names the Python caller, not the Rust callee ===
> exit status: 0
> faulthandler banner: none
> innermost Python frame: none
> C stack section present (Python 3.14+): no
> --- forced fault: a read at the unmapped address 16, on any platform ---
> exit status: 139 (128 + signal 11)
> faulthandler banner: Fatal Python error: Segmentation fault
> innermost Python frame: driver.py:78 in stale_read
> Python call chain: stale_read <- run_fault <- main <- <module>
> C stack section present (Python 3.14+): yes
> C stack names tape_sum: no
> ```
>
> On Linux (glibc 2.39, Python 3.12.3), where the cold `large` memory is unmapped, faulthandler catches the `SIGSEGV` in the real scenario and names the Python call.
> Copied from `expected/linux-aarch64.txt` (as of 2026-11, Linux):
>
> ```text
> === Stage 2: faulthandler names the Python caller, not the Rust callee ===
> exit status: 139 (128 + signal 11)
> faulthandler banner: Fatal Python error: Segmentation fault
> innermost Python frame: driver.py:78 in stale_read
> Python call chain: stale_read <- run_large <- main <- <module>
> C stack section present (Python 3.14+): no
> --- forced fault: a read at the unmapped address 16, on any platform ---
> exit status: 139 (128 + signal 11)
> faulthandler banner: Fatal Python error: Segmentation fault
> innermost Python frame: driver.py:78 in stale_read
> Python call chain: stale_read <- run_fault <- main <- <module>
> C stack section present (Python 3.14+): no
> ```
>
> `driver.py:78` is `total = lib.tape_sum(tape)`.
> Python 3.12 prints no C stack; since Python 3.14, `faulthandler.enable` has a `c_stack` parameter that defaults to `True`, so `-X faulthandler` alone appends `Current thread's C stack trace (most recent call first):` after the Python traceback, as the macOS Python 3.15.2 forced fault shows.
> That C stack walked `_sigtramp`, `ffi_call_SYSV`, `ffi_call_int`, `_ctypes_callproc`, and into the interpreter, and the lab's last line proves it names no `tape_sum` frame: the faulting function's PC lives in the signal context rather than in a frame record, so a frame walk from the handler starts at its caller (as of 2026-11, macOS; the cause is an inference, the absence is measured).
> faulthandler tells you which Python call crossed the boundary; it does not tell you why the memory was bad.
>
> **Step 2: GDB proves the address belonged to a freed Python object.**
> Stage 3 runs `gdb -nx -batch -iex "set debuginfod enabled off" -x gdb_triage.py --args python3 driver.py LIB large ADDR_FILE`.
> On macOS, GDB is not available, copied from `expected/darwin-arm64.txt` (as of 2026-11, macOS):
>
> ```text
> === Stage 3: gdb stops at the faulting load and checks the address ===
> gdb: not available on Darwin (gdb triage is Linux-only)
> ```
>
> On Linux, the script runs to the signal, reads `$_siginfo`, compares `si_addr` with the pointer the driver wrote just before the call, checks `info proc mappings`, and walks the frames with `gdb.solib_name` (as of 2026-11, Linux):
>
> ```text
> === Stage 3: gdb stops at the faulting load and checks the address ===
> signal: SIGSEGV (SEGV_MAPERR)
> fault address inside the buffer Python freed: yes
> fault address offset into that buffer: 0
> fault address inside any current mapping: no
> frame 0 is in libticktape: yes
> innermost ticktape source frame: ticktape::tape_sum at lib.rs:75
> libffi frame directly below the Rust frames: yes
> interpreter frames below libffi: yes
> py-bt: unavailable (no python-gdb.py auto-loaded for this interpreter)
> ```
>
> GDB names the source frame by its Rust path, `ticktape::tape_sum`, at `lib.rs:75`, the `read_volatile` line.
> `SEGV_MAPERR` at offset 0 with no mapping means the very first byte of the buffer is gone from the address space: the 1 MiB `bytes` object came from a glibc `mmap` chunk, and `del payload` dropped the refcount to zero, which freed the object, which `munmap`ped the chunk.
> The stack reads bottom-up as interpreter, then libffi (`ffi_call`), then Rust, which is exactly the path `ctypes.CDLL` takes.
> `py-bt` needs the interpreter's GDB extension, which the lab image does not have, hence the `unavailable` line in the golden file.
> On Ubuntu the HOWTO recipe is `sudo apt install gdb python3-dbg`; after installing `python3-dbg` in the same container, the same script printed `py-bt innermost Python frame: driver.py:78 in stale_read` in place of that line, and every other line was unchanged (a hand run, not recorded in a golden file; as of 2026-11, Linux).
>
> **Step 3: explain all three faces.**
>
> | Scenario | Allocator path | What `free` did | What the stale read saw |
> | :--- | :--- | :--- | :--- |
> | `small` (256-byte payload) | pymalloc block in an `mmap` arena | Pushed the block on its pool's free list | The next same-size `bytes` object reused the block, so Rust summed the replacement's bytes: a wrong answer, exit 0 |
> | `large` (1 MiB, first big free) | `malloc`, above the 128 KiB initial threshold, so a private `mmap` | `munmap` | `SEGV_MAPERR` on byte 0 |
> | `warm` (1 MiB, after one earlier 1 MiB free) | `malloc` from the heap, because the earlier free raised the threshold | Kept the chunk mapped in the arena | The old bytes, so the sum was correct and nothing failed |
> | macOS `large` | libmalloc's large-allocation path | Did not unmap the range before the read, in the run observed | The old bytes, exit 0 |
>
> That table is the intermittency.
> Production crashes only for large messages, only before anything else in the process has freed a comparably large block, and only on Linux; staging with warmed-up traffic and laptops on macOS never crash, and the small-message corruption never crashes anywhere.
>
> **Step 4: AddressSanitizer names both sides.**
> On Linux, `add_sanitizer_libraries` in rustc's `back/link.rs` links sanitizer runtimes into `Dylib` and `Cdylib` crates only for Apple and MSVC targets, where the runtime ships as a dynamic library; elsewhere (Android excepted, where it returns early because the runtime is provided dynamically) the runtime is a static archive (`librustc-nightly_rt.asan.a`) linked into executables only.
> `python3` is not instrumented, so on Linux Stage 4 preloads a shared runtime via `LD_PRELOAD`: `libclang_rt.asan` from a clang whose major version equals rustc's LLVM, or GCC's `libasan.so`.
> The Linux run used GCC 13's `libasan.so` under LLVM 23 instrumentation, despite the wiki's version-match warning, and printed a Stage 4 byte-identical to the macOS Stage 4 (as of 2026-11, Linux).
> On macOS, rustc links `@rpath/librustc-nightly_rt.asan.dylib` into the `cdylib`, and `run.sh` injects the runtime with `DYLD_INSERT_LIBRARIES` into the uninstrumented Python process.
> It runs each scenario with `ASAN_OPTIONS=detect_leaks=0` (LeakSanitizer reports the interpreter's own internal allocations) and `PYTHONMALLOC` set to `pymalloc` and then `malloc`.
> This complete output is copied from the lab's `expected/darwin-arm64.txt` Stage 4 (as of 2026-11, macOS):
>
> ```text
> === Stage 4: AddressSanitizer, preloaded into an uninstrumented python3 ===
> --- PYTHONMALLOC=pymalloc, small ---
> ASan report: none (exit status 0)
> small: replacement object reuses the freed block: yes
> small: sum equals the freed payload's sum: no
> small: sum equals the replacement's sum: yes
> --- PYTHONMALLOC=pymalloc, large ---
> ASan report: heap-use-after-free
> address: 32 bytes inside a 1048609-byte heap block
> --- PYTHONMALLOC=pymalloc, fixed ---
> ASan report: none (exit status 0)
> fixed-small: sum equals the original payload's sum: yes
> fixed-large: sum equals the original payload's sum: yes
> --- PYTHONMALLOC=malloc, small ---
> ASan report: heap-use-after-free
> access: READ of size 1
> address: 32 bytes inside a 289-byte heap block
> use stack reaches tape_sum: yes (lib.rs:75)
> use stack crosses libffi: yes
> freed by: free(), Rust frames in that stack: 0
> allocated by: malloc(), Rust frames in that stack: 0
> --- PYTHONMALLOC=malloc, large ---
> ASan report: heap-use-after-free
> address: 32 bytes inside a 1048609-byte heap block
> --- PYTHONMALLOC=malloc, fixed ---
> ASan report: none (exit status 0)
> fixed-small: sum equals the original payload's sum: yes
> fixed-large: sum equals the original payload's sum: yes
> ```
>
> With the default allocator, ASan misses the small case completely, prints nothing, and the program returns the wrong sum: the pymalloc arena is memory ASan considers valid.
> `PYTHONMALLOC=malloc` routes every Python allocation through the intercepted `malloc`, and the small case becomes a precise report.
> The large case is caught either way, and it is no longer a `SIGSEGV`: ASan's allocator holds freed blocks in quarantine (`quarantine_size_mb`, 256 by default) instead of unmapping them, so the access is classified at the instruction instead of faulting by luck.
> The block sizes decode the object: 289 is a 256-byte payload plus 33 bytes of `bytes` header and terminator, and 32 bytes inside is the offset of the inline data, so the address Rust read is the first payload byte of a dead `bytes` object.
>
> The same experiment with the real Rust `cdylib` on darwin-arm64, where rustc links `@rpath/librustc-nightly_rt.asan.dylib` into the `cdylib` itself, needed the runtime inserted at launch: loading it through `dlopen` printed `ERROR: Interceptors are not working. This may be because AddressSanitizer is loaded too late (e.g. via dlopen).`, and `DYLD_INSERT_LIBRARIES` pointing at the nightly's `librustc-nightly_rt.asan.dylib` worked when applied to the framework's `Python.app/Contents/MacOS/Python` binary (as of 2026-11, macOS).
> Read the `PYTHONMALLOC=malloc, small` report as a two-runtime story, using only what the lab prints.
> The use is Rust: the use stack reaches `tape_sum` at `lib.rs:75` and crosses libffi.
> The free is Python: the "freed by" and "previously allocated by" stacks start in `free()` and `malloc()` and the digest counts 0 Rust frames in either.
> `del payload` compiles to `DELETE_FAST` (check with `python3 -m dis`), which is where the refcount hits zero, and the allocation is `bytes([fill]) * n` in the driver's `make`.
> Nothing in Rust's ownership system was told that Python owned these bytes, which is the defect.
>
> **Step 5: fix the ownership, not the symptom.**
> The lab's fix is `tape_feed_copy`: Rust copies the bytes into a `Box<[u8]>` it owns, and both fixed scenarios stay correct under ASan with `PYTHONMALLOC=malloc`.
> The alternatives keep zero copy and move the guarantee into a type:
> - In ctypes, keep the `bytes` object referenced for as long as the tape uses it, for example as an attribute of the Python wrapper that owns the tape handle, and document it as the contract of `tape_feed`.
> - In PyO3, `PyBytesMethods::as_bytes(&self) -> &[u8]` ties the slice to the `Bound<'py, PyBytes>` borrow, so safe code cannot store it; this bug needs `unsafe` (a raw pointer or a transmute to `'static`).
>   Store a `Py<PyBytes>` (a strong reference) next to the offsets, or a `PyBackedBytes`, whose storage is either owned by a Python `bytes` object or a Rust `Box<[u8]>`, and borrow the slice on each use.
> - Reject any review of an `extern "C"` or PyO3 function that saves a pointer argument past return without saying who keeps the pointee alive.
>
> The one-sentence answer: `tape_sum` (Rust, `lib.rs:75`) read the first byte of a `bytes` object that CPython freed at `del payload` (`DELETE_FAST`), because `tape_feed` kept the pointer ctypes lent it for one call, and whether that read crashed was decided by pymalloc for small payloads, by glibc's dynamic mmap threshold for large ones, and by the operating system.

## Core Takeaway

A pointer that crosses from a garbage-collected or reference-counted runtime into native code is a loan for the duration of the call unless the native side takes a reference.
Python and Rust each have a sound ownership model, and the bug lives in the gap between them, where neither model applies.
The symptom is intermittent because the free goes through one of several allocators: pymalloc reuses blocks silently, glibc unmaps large chunks only until its dynamic threshold rises, and macOS's allocator behaves differently again, so "does it crash" is a property of allocator history, not of the code.
Make the tools cross the gap deliberately: faulthandler to find the Python call that crossed, GDB to prove the address belonged to a dead object, and ASan preloaded into the interpreter with `PYTHONMALLOC=malloc` so it sees Python's frees and names both runtimes in one report.
The boundary method in general, including unwinding and the GIL, is in [[Rust-FFI-Boundary-Debugging-Across-Cpp-and-Python]]; the shadow-memory and quarantine mechanism is in [[Cpp-AddressSanitizer-and-LeakSanitizer-Internals]].

## Pitfalls

> [!warning] Isolated mode drops PYTHONMALLOC
> `python3 -I` implies `-E`, which ignores every `PYTHON*` environment variable.
> On darwin-arm64 with Python 3.15.2, `PYTHONMALLOC=malloc python3 -I driver.py ... small` under ASan printed no report and `replacement object reuses the freed block: yes`, exactly like the default allocator (as of 2026-11, macOS).
> A sanitizer run that silently stops seeing small objects looks like a clean run.

> [!warning] `run ARGS` in GDB replaces `--args`
> The first draft of `gdb_triage.py` ran `run > /dev/null 2>&1` to silence the inferior.
> In GDB, `run` with arguments replaces the argument list given by `--args`, so `python3` started with no script, read an empty standard input, and GDB printed `[Inferior 1 (process N) exited normally]`; the script then reported that the stale read did not fault.
> The fixed script reads `gdb.parameter("args")` and runs `run ARGS > /dev/null 2>&1`.

> [!warning] Reproducing on the wrong platform or with warm state
> Every scenario except a cold `large` read on Linux returned exit 0.
> A reproduction that runs the crashing call in a loop warms the glibc threshold after the first iteration, and a macOS laptop did not crash at all in the run recorded here; reproduce with one cold process per attempt on the production OS.

> [!warning] Free-threaded CPython changes the allocator
> The C API documentation specifies that mimalloc is the mandatory allocator for the `PYMEM_DOMAIN_MEM` and `PYMEM_DOMAIN_OBJ` domains in free-threaded builds and cannot be disabled, and `bytes` objects come from `PyObject_Malloc`, which is the `OBJ` domain.
> A hand run of the lab's driver on the free-threaded build CPython 3.14.6 (python-build-standalone via uv, macOS arm64, ASan inserted with `DYLD_INSERT_LIBRARIES`, interceptors confirmed initialized) showed what that means (as of 2026-11, macOS; not part of the golden files):
> - `PYTHONMALLOC=malloc` and `PYTHONMALLOC=pymalloc` abort at startup with `Fatal Python error: preconfig_init_allocator: PYTHONMALLOC: unknown allocator`, so the lab's recipe cannot be applied as is; `mimalloc`, `debug`, and `default` start.
> - With the default allocator, neither the small nor the large scenario produced an ASan report; the small case returned the replacement's sum exactly as in the default build, and the large case, which the default build catches because it goes through `malloc`, was silent too.
> A free-threaded interpreter therefore hides this bug from a preloaded ASan runtime entirely, unless CPython itself is built with AddressSanitizer.
> Test the sanitizer setup directly on the exact interpreter build deployed to production.

> [!warning] Uninstrumented standard library
> `run.sh` builds without `-Zbuild-std`, because rebuilding `std` needs its dependencies from the registry.
> ASan still sees this bug because the bad load is in `ticktape`'s own code, but the Unstable Book recommends `-Zbuild-std` for complete coverage: an out-of-bounds access inside an uninstrumented `std` function can go unreported.

## Interview Questions

> [!question] L5: The small-payload scenario returns a wrong sum and exits 0.
> Why does it not crash?
>
> > [!success]- Answer
> > The 256-byte payload makes a 289-byte `bytes` object, which pymalloc serves from an arena it obtained with `mmap`, because the request is at most 512 bytes.
> > Freeing it puts the block on its pool's free list; the memory stays mapped and owned by the process, so no fault is possible.
> > The next `bytes` object of the same size class reused the block (the driver checks `id(replacement) == old_id`), so Rust summed the replacement's bytes.
> > A silent wrong answer is the worst face of this bug, because nothing in monitoring sees it.

> [!question] L5: faulthandler's traceback shows `stale_read` in `driver.py`, not `tape_sum`.
> Is faulthandler broken?
>
> > [!success]- Answer
> > No; faulthandler prints Python frames, and a ctypes foreign call does not create one, so the innermost Python frame is the line that made the call.
> > That is the right starting point: it names the boundary crossing and its arguments.
> > Since Python 3.14 it also prints the C stack (`c_stack`, `True` by default), but on the darwin-arm64 run that stack started at the caller of the faulting function, most likely because the faulting PC is in the signal context rather than in a frame record; use GDB or a core for frame 0.

> [!question] L5: Predict the output.
> Under the lab's ASan setup, the small scenario is run once with the default allocator and once with `PYTHONMALLOC=malloc`.
> What does each print?
>
> > [!success]- Answer
> > Default allocator: no ASan report, exit status 0, and the sum equals the replacement's sum, because pymalloc's arena memory came from `mmap` and ASan treats it as ordinary valid memory.
> > `PYTHONMALLOC=malloc`: `heap-use-after-free`, `READ of size 1`, `32 bytes inside a 289-byte heap block`, the use stack in `tape_sum`, and free and allocation stacks with no Rust frame.
> > The allocator, not the bug, decides whether the sanitizer can see it; the large case is caught in both runs because it already goes through `malloc`.

> [!question] L6: Why does the lab need `LD_PRELOAD` on Linux, while on macOS the instrumented `cdylib` already depends on the ASan runtime?
>
> > [!success]- Answer
> > In rustc's `add_sanitizer_libraries`, `Dylib` and `Cdylib` crates get the runtime only on Apple and MSVC targets, where it is a dynamic library; on Linux it is a static archive linked into executables only.
> > The executable here is `python3`, which nobody instrumented, so the runtime must come from a preloaded shared object; the runtime also has to initialize before other libraries resolve `malloc`, and its link-order check prints `ASan runtime does not come first in initial library list` when it is loaded later.
> > On macOS the `cdylib` names `@rpath/librustc-nightly_rt.asan.dylib`, but loading it via `dlopen` is too late for the interceptors, so it still has to be inserted with `DYLD_INSERT_LIBRARIES` at launch.
> > Preload a runtime from the same LLVM major version as rustc when one exists, because the DSO must match the instrumentation.

> [!question] L6: Debug this.
> A GDB batch script runs `gdb.execute("run > /dev/null 2>&1")` on `gdb --args python3 driver.py LIB large FILE`, and reports that the program exited normally, yet the same command line segfaults every time outside GDB.
> What is wrong?
>
> > [!success]- Answer
> > `run` with any arguments, and a redirection counts, replaces the argument list that `--args` set.
> > GDB therefore started `python3` with no arguments; it read an empty standard input and exited 0, so the bug never ran.
> > Read the list with `gdb.parameter("args")` and pass it again: `run ARGS > /dev/null 2>&1`, or use `set args` and `tty`/`set inferior-tty` for redirection.
> > The general lesson: when a debugger run "cures" a crash, first prove the debugged process did the same work.

> [!question] L6: The `warm` scenario frees one 1 MiB object before running the bug, and then does not crash.
> Explain with the exact glibc rule.
>
> > [!success]- Answer
> > mallopt(3): the mmap threshold starts at 128 KiB and is dynamic; when a block larger than the threshold and at most `DEFAULT_MMAP_THRESHOLD_MAX` (`4*1024*1024*sizeof(long)`, 32 MiB on 64-bit) is freed, the threshold rises to that block's size, and the trim threshold becomes twice the mmap threshold.
> > The warm-up free raised the threshold to the size of a 1 MiB chunk, so the next 1 MiB request came from the heap; freeing it left the memory mapped, and the stale read saw the old bytes.
> > Setting `M_MMAP_THRESHOLD` with `mallopt` disables the dynamic adjustment, and the `MALLOC_MMAP_THRESHOLD_` environment variable controls the same parameter, which is one way to pin the behavior while reproducing.

> [!question] L6: Why is the large case an ASan report rather than a `SIGSEGV` once ASan is preloaded?
>
> > [!success]- Answer
> > ASan replaces `malloc` and `free`; freed blocks are poisoned in shadow memory and held in a quarantine (256 MB by default) instead of being returned to glibc or unmapped.
> > The read hits poisoned shadow and ASan reports at the instruction, with the free and allocation stacks it recorded, before the hardware ever faults.
> > That is why sanitizer runs turn intermittent crashes into deterministic reports, and also why they use more memory.

> [!question] L7: How would this bug appear in a PyO3 extension, and which types prevent it?
>
> > [!success]- Answer
> > Safe PyO3 cannot express it: `as_bytes` on a `Bound<'py, PyBytes>` returns a slice borrowed from that handle, so storing it in a `#[pyclass]` field fails to compile.
> > The bug appears when someone keeps a raw pointer or transmutes the slice to `'static` "for zero copy".
> > The sound designs keep the owner with the view: a `Py<PyBytes>` field (a strong reference, so CPython cannot free the object) plus offsets, or `PyBackedBytes`, or a copy into `Vec<u8>` or `Box<[u8]>` when the payload is small.
> > Since PyO3 0.26 the attach and detach calls are `Python::attach` and `Python::detach` (formerly `with_gil` and `allow_threads`); detaching while holding a borrowed slice is a second way to outlive a guarantee, so review those sites too.

> [!question] L7: Design the CI gate that would have caught this before production for a fleet of Rust and C extensions used from Python.
>
> > [!success]- Answer
> > Build every extension twice in CI: release, and nightly with `-Zsanitizer=address` (plus `-Zbuild-std` where the registry is reachable).
> > Run the Python test suite against the sanitized build with a preloaded runtime from the same LLVM, `PYTHONMALLOC=malloc`, `ASAN_OPTIONS=detect_leaks=0` or a curated LSan suppression list, and fail on any ASan report.
> > Assert the setup works with a canary test that deliberately reads a freed `bytes` buffer and must produce a report; otherwise a runtime that silently failed to load looks like a pass.
> > Add Miri for the pure-Rust layers and a lint or review rule for any FFI function that stores a pointer argument; cross-runtime ownership is not visible to either compiler.

> [!question] L8+: Your organization is moving services to free-threaded CPython and wants zero-copy buffers between Python and Rust everywhere.
> What policy do you set?
>
> > [!success]- Answer
> > Ownership must be in types at the boundary: zero copy only through owning handles (`Py<T>`, buffer-protocol views held for the duration of use, `PyBackedBytes`), never through stored raw pointers, and every `unsafe` pointer escape needs a named owner in its safety comment.
> > Free-threaded builds remove the GIL as an accidental serializer and, per the C API documentation, make mimalloc mandatory for object memory, so allocator-based detection changes: in the 3.14.6 run above a preloaded ASan saw neither the small nor the large stale read, and `PYTHONMALLOC=malloc` was rejected at startup, so re-validate that the sanitizer job still sees object frees and prefer a CPython built with ASan for the deepest tier.
> > Treat allocator behavior as part of the reproduction contract: record the interpreter build, allocator, and platform with every crash, because the same defect is a crash, a wrong answer, or nothing depending on them.
> > Budget the cost: sanitized nightly CI is cheap compared with one silent wrong answer in a pricing or market-data path.

## Further Reading

- Python C API, Memory Management, https://docs.python.org/3/c-api/memory.html.
  The pymalloc 512-byte cutoff, `mmap` arenas, `PYTHONMALLOC=malloc`, and the free-threaded mimalloc rule.
- Python faulthandler, https://docs.python.org/3/library/faulthandler.html.
  The fatal signals it handles, `-X faulthandler`, and the C stack added in 3.14.
- Python ctypes, https://docs.python.org/3/library/ctypes.html.
  How `bytes` arguments are passed and that `CDLL` releases the GIL around foreign calls.
- Python HOWTO, Debugging C API extensions and CPython Internals with GDB, https://docs.python.org/3/howto/gdb_helpers.html.
  `python-gdb.py`, `py-bt`, and the packages that ship them.
- mallopt(3), https://man7.org/linux/man-pages/man3/mallopt.3.html.
  The dynamic mmap threshold rule that makes this crash depend on history.
- The Rust Unstable Book, sanitizer, https://doc.rust-lang.org/beta/unstable-book/compiler-flags/sanitizer.html.
  `-Zsanitizer=address`, its supported targets, and the `-Zbuild-std` recommendation.
- rustc `back/link.rs`, `add_sanitizer_libraries`, https://github.com/rust-lang/rust/blob/master/compiler/rustc_codegen_ssa/src/back/link.rs.
  The source of truth for which crate types get a sanitizer runtime on which targets.
- google/sanitizers wiki, AddressSanitizerAsDso, https://github.com/google/sanitizers/wiki/AddressSanitizerAsDso.
  Why the preloaded runtime must match the instrumentation.
- Clang AddressSanitizer documentation, https://clang.llvm.org/docs/AddressSanitizer.html.
  Bug classes, report anatomy, and LeakSanitizer defaults.
- PyO3 API documentation, https://docs.rs/pyo3/latest/pyo3/.
  `Bound`, `Py`, `PyBytesMethods::as_bytes`, and `PyBackedBytes`, the types that make the lifetime explicit.

## Related

- Prerequisites: [[Rust-FFI-Boundary-Debugging-Across-Cpp-and-Python]]
- See also: [[Drill-Debugging-Use-After-Free-Core-Dump]], [[Python-faulthandler-and-Crash-Diagnostics]], [[Python-Debugging-Hung-Processes-and-C-Extensions]], [[Cpp-AddressSanitizer-and-LeakSanitizer-Internals]], [[Rust-Miri-Sanitizers-Loom-and-Cargo-Fuzz]]
- Lab: `code/03-Debugging/drill_ffi_segfault/` (requires: nightly)
