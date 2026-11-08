---
type: problem
track: [sde, distinguished, low-latency, quant-dev]
level:
status: solid
last_reviewed:
sources:
  - "The GNU C Library (glibc) 2.39, malloc/malloc.c (tcache_put, _int_free tcache double-free check, PROTECT_PTR and REVEAL_PTR safe-linking, free_perturb), Free Software Foundation, 2024, https://sourceware.org/git/?p=glibc.git;a=blob;f=malloc/malloc.c;hb=refs/heads/release/2.39/master"
  - "Free Software Foundation, Debugging with GDB: Convenience Variables ($_siginfo) and Core File Generation, sourceware.org, 2026, https://sourceware.org/gdb/current/onlinedocs/gdb.html/Convenience-Vars.html"
  - "Michael Kerrisk et al., sigaction(2): si_code values for SIGSEGV and SIGBUS, Linux man-pages project, 2026, https://man7.org/linux/man-pages/man2/sigaction.2.html"
  - "GNU Binutils project, GNU Binary Utilities: objcopy (--only-keep-debug) and strip (--strip-all), sourceware.org, 2026, https://sourceware.org/binutils/docs/binutils/objcopy.html"
  - "LLVM Project, AddressSanitizer: heap-use-after-free detection and report format, LLVM Project, 2026, https://clang.llvm.org/docs/AddressSanitizer.html"
  - "Google sanitizers wiki, AddressSanitizerAlgorithm (shadow memory, quarantine), 2026, https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm"
  - "Free Software Foundation, Debugging with GDB: How to Produce a Core File from Your Program, sourceware.org, 2026, https://sourceware.org/gdb/current/onlinedocs/gdb.html/Core-File-Generation.html"
  - "Free Software Foundation, Debugging with GDB: Debugging Information in Separate Files (.build-id layout), sourceware.org, 2026, https://sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html"
  - "Free Software Foundation, Debugging with GDB: Program Variables (@entry values), sourceware.org, 2026, https://sourceware.org/gdb/current/onlinedocs/gdb.html/Variables.html"
  - "GNU Binutils project, strip (--strip-all), sourceware.org, 2026, https://sourceware.org/binutils/docs/binutils/strip.html"
  - "The GNU C Library 2.33 and 2.31, malloc/malloc.c (tcache key was the per-thread tcache pointer before 2.34; no PROTECT_PTR in 2.31), https://raw.githubusercontent.com/bminor/glibc/release/2.33/master/malloc/malloc.c"
  - "LLVM Project, GWP-ASan, 2026, https://llvm.org/docs/GwpAsan.html"
  - "The Linux kernel documentation, Memory Tagging Extension (MTE) in AArch64 Linux (PR_MTE_TCF_SYNC, SEGV_MTESERR), kernel.org, 2026, https://www.kernel.org/doc/html/latest/arch/arm64/memory-tagging-extension.html"
  - "LLVM Project, Hardware-assisted AddressSanitizer Design Documentation, LLVM Project, 2026, https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html"
---

# Drill: Debugging Use-After-Free From a Core Dump

> [!summary] Scope
> This drill hands you a stripped, optimized Linux core from a C++ service that died on a use-after-free, and asks you to name the free site that is not in the backtrace.
> The crashing backtrace names the victim (an indirect call through a freed object), never the culprit (the free), so the answer lives in the heap bytes the core captured.
> The lesson is that on glibc a freed small chunk is self-describing: tcache overwrites its first 16 bytes with a safe-linked next pointer and a process-wide key (glibc 2.34 and later; `malloc.c` "Process-wide key"), and reading those two words out of the core proves the object was freed and never reallocated.

The capture chain (`core_pattern`, `RLIMIT_CORE`, `coredump_filter`) is owned by [[Core-Dumps-Minidumps-and-Crash-Reporting]].
The matching-and-symbol workflow (build IDs, `debuginfod`, `-iex` settings) is owned by [[Debug-a-Production-Crash-From-a-Core-Dump]], which is the prerequisite for this drill.
The allocator shadow-memory tooling that would have caught this at the write is owned by [[Cpp-AddressSanitizer-and-LeakSanitizer-Internals]].
This note owns only the core-dump reconstruction of a use-after-free.

## Reproduction

The reproducible drill lives in `code/03-Debugging/drill_uaf_core/`.

- `uaf.cpp` is the buggy service: `make_order` heap-allocates a 48-byte `Order` with a function pointer `on_fill` in its first eight bytes, `retire` frees it, and then `dispatch` calls `execute(o)` on the dangling pointer, whose body is `o->on_fill(o)`.
- `run.sh` compiles it release-style (`-O2 -g -fno-omit-frame-pointer -fno-optimize-sibling-calls -Wl,--build-id`), splits the debug info into a `.build-id` tree with `objcopy --only-keep-debug`, strips the shipped binary with `strip --strip-all`, crashes it to capture a kernel core (falling back to GDB `generate-core-file` when `core_pattern` pipes elsewhere), and runs `triage.py` on the core.
- `triage.py` is a GDB batch script that prints only derived, address-free facts, so the output is identical on every run despite address-space layout randomization.

The lab declares `# requires: linux` because the whole drill rests on the kernel ELF core format and glibc tcache safe-linking.
On a non-Linux host the harness runs it in the pinned Ubuntu 24.04 image (gdb 15.1, elfutils 0.190, gcc 13.3.0, binutils 2.42, glibc 2.39, as of 2026-11; read from `gdb --version`, `c++ --version`, and `ldd --version` in image `toolchain-labs:f39efe0ff57b`).

Run it from `code/`:

```sh
make verify LAB=03-Debugging/drill_uaf_core/
```

## Observed Symptom

The service dies under its orchestrator with exit status 139 (`128 + SIGSEGV`) or, depending on address-space layout, 135 (`128 + SIGBUS`).
You retrieve the core and open it against the stripped release binary, before attaching debug info, and GDB can name almost nothing (captured in the pinned container on linux-aarch64, 2026-11; addresses vary with ASLR, and this run took the SIGBUS path):

```text
Program terminated with signal SIGBUS, Bus error.
#0  0x0000000aaaac4316 in ?? ()
#1  0x0000aaaab7900840 in ?? ()
#2  0x0000aaaab790085c in ?? ()
#3  0x0000aaaab79006e0 in ?? ()
#4  0x0000ffffbd4f84c4 in ?? () from /lib/aarch64-linux-gnu/libc.so.6
#5  0x0000ffffbd4f8598 in __libc_start_main () from /lib/aarch64-linux-gnu/libc.so.6
#6  0x0000aaaab7900730 in ?? ()
```

Two facts are already strange and are the whole puzzle.
The program counter has no symbol and points into memory that holds no code, so execution branched to a wild address rather than faulting on a data access.
The stack is walkable but unlabeled: `strip --strip-all` keeps the allocated `.eh_frame` and `.eh_frame_hdr` sections (checked with `readelf -S` on the stripped lab binary), so GDB unwinds every frame, yet without symbols every application frame is `??`.
Note also that frame 0 (`0xaaaac4316`) is exactly the heap address of the object (`0xaaaac43162a0`) shifted right by 12, which is the first hint that the branch target came out of allocator metadata.

## Progressive Hint Ladder

> [!tip]- Hint 1: Attach the debug info before you theorize
> A backtrace of `??` frames from a stripped binary is shape without names; it is not yet evidence.
> Follow [[Debug-a-Production-Crash-From-a-Core-Dump]]: read the build ID from the core, confirm it equals the shipped artifact, install the split debug file as `debug_root/.build-id/nn/rest.debug`, and reopen with `gdb -iex "set debug-file-directory debug_root"`.
> Only then do the frames carry function names and `uaf.cpp` line numbers.

> [!tip]- Hint 2: A wild pc is a jump, not a bad read
> `SEGV_MAPERR` on a data read leaves `pc` inside your code and `si_addr` at the bad data address.
> Here `pc` itself is the bad address, which means the processor tried to fetch an instruction there.
> On arm64 a wild target in practice comes from a register branch (`br`, `blr`) or a return (`ret` through a corrupted `x30`), and a misaligned target raises SIGBUS with `si_code` `BUS_ADRALN` (observed: `$_siginfo.si_code == 1`) instead of SIGSEGV with `SEGV_MAPERR`, so look for an indirect call or a corrupted return address, and read the register that held the target.

> [!tip]- Hint 3: The victim is the call, the culprit is the free
> With debug info the frames are `execute` then `dispatch` then `main`, and `execute` is literally `o->on_fill(o)`.
> The function pointer it loaded is garbage, so ask who could have written garbage into `on_fill`.
> Nobody in the backtrace did; the writer was `free`, whose frame returned long before the crash, so it cannot appear here.

> [!tip]- Hint 4: Read the freed object as a tcache entry
> In `execute`, `o` is still in `x0` (the body is `ldr x1, [x0]; blr x1`, confirmed with `disassemble execute` on the gcc 13.3.0 `-O2` build), so `print (unsigned long)o` gives the object and `x/2gx o` gives its first two words.
> On glibc those two words are not your fields any more; they are a `tcache_entry`: a safe-linked `next` at offset 0 and a `key` at offset 8.
> `next` is mangled by `PROTECT_PTR(pos, ptr) = (pos >> 12) ^ ptr`, so reveal it with `((unsigned long)&o->on_fill >> 12) ^ word0`; for the only chunk in its bin it reveals to 0, which payload data does only if it happens to equal the object's own address shifted right by 12.

## Model Solution

> [!success]- Solution
> Reopen the core with the matching debug info and the frames resolve.
> The verified output of the drill, copied from `code/03-Debugging/drill_uaf_core/expected/linux-aarch64.txt` (recorded by `make record` in the pinned Ubuntu 24.04 container on linux-aarch64, 2026-11), is:
>
> ```text
> === Crash site (the victim) ===
> Crashed on a memory-access fault (SIGSEGV or SIGBUS): yes
> Faulting pc has no symbol (wild jump target): yes
> Victim frame: execute(Order*) at uaf.cpp:43
> Jumped to the object's first 8 bytes (pc == *obj): yes
>
> === Backtrace (the free site is absent) ===
> execute(Order*) at uaf.cpp:43
> dispatch(Order*) at uaf.cpp:45
> main() at uaf.cpp:50
> retire present in backtrace: no
>
> === Heap forensics (the core proves the free) ===
> obj.on_fill no longer holds &settle (it was overwritten): yes
> Safe-linked next reveals to NULL (glibc tcache list tail): yes
> Word at offset 8 is non-zero and no longer the original id 0x2222 (tcache key): yes
> Payload qty 100 survives at offset 16 (tcache touched only 16 bytes): yes
>
> === Verdict ===
> Use-after-free: dispatch used `o` after retire() freed it to the tcache;
> the free site retire() is not in the backtrace and was recovered from the heap.
>
> === Contrast: AddressSanitizer on the same source ===
> ASan bug class reported: heap-use-after-free
> ASan names the use site (execute): yes
> ASan names the free site (retire): yes
> ASan names the allocation site (make_order): yes
> ```
>
> Reading it as a chain of proof:
>
> 1. The crash is an instruction fetch at a wild address, and that address equals the freed object's first eight bytes.
>    `execute` is `ldr x1, [x0]; blr x1`, so it loaded `on_fill` from the object and branched to it; `pc == *obj` ties the fault to the object's memory, not to a coincidence, and the lab confirms with GDB (`info symbol`, block and line lookup) that the pc names nothing.
> 2. The backtrace stops at `execute`, `dispatch`, `main`, and `retire` is absent.
>    A free returns before the use, so the core can never show the free in the stack; this is the defining difficulty of use-after-free triage and the reason you must read the heap.
> 3. The object's first word reveals to `NULL` under glibc safe-linking.
>    `PROTECT_PTR(&next, NULL) = (&next >> 12) ^ 0`, and revealing it with the same transform yields 0 because glibc wrote it; arbitrary payload almost never would.
>    This is the signature that the chunk is the tail of a glibc safe-linked free list right now, freed and not reallocated; on its own it does not separate tcache from fastbin, because fastbins are safe-linked the same way (a run with `GLIBC_TUNABLES=glibc.malloc.tcache_count=0` still reveals to 0), so step 4 is what pins it to the tcache.
> 4. The second word is a non-zero `key` that replaced the payload id `0x2222`.
>    `tcache_put` writes `e->key = tcache_key` at offset 8, which is how glibc catches a later double free; a fastbin free leaves offset 8 untouched, so a word that is no longer `0x2222` is what confirms the free went through the tcache path (with `GLIBC_TUNABLES=glibc.malloc.tcache_count=0` the lab's offset-8 line flips to `no`), and `qty` surviving at offset 16 confirms tcache touched only the first 16 bytes.
>    The lab cannot read `tcache_key` itself (libc is stripped), so it proves "written by free" by exclusion of the original payload, not by comparing against the key value.
> 5. The contrast run shows the cost of losing this bug to production.
>    The same source under AddressSanitizer reports `heap-use-after-free` and names all three sites (`execute`, `retire`, `make_order`) directly, because its quarantine and shadow memory catch the access at the instruction that made it.
>    From a core you reconstruct what ASan would have told you for free, which is the skill this drill builds.
>
> The one-sentence answer is the form [[Debug-a-Production-Crash-From-a-Core-Dump]] asks for: thread 1 died fetching an instruction from the bytes of the `Order` passed to `execute` (`uaf.cpp:43`), because that object was freed by `retire` (recovered from its tcache next-and-key words), and `dispatch` then called through its stale `on_fill`.

## Core Takeaway

A use-after-free hides its cause by construction: the free runs and returns, the use crashes later, and the backtrace shows only the use.
On glibc the freed object is not a black box, because the allocator stamps every small chunk freed to the tcache with a safe-linked `next` and the process-wide `tcache_key` in its first 16 bytes.
Reading those two words out of the core, and revealing the pointer with glibc's own `(pos >> 12) ^ ptr` transform, proves the object is freed and not merely garbage, and separates a use-after-free from heap overflow or stack corruption.
When the bug must not reach production in the first place, the owning tool is a sanitizer: see [[Cpp-AddressSanitizer-and-LeakSanitizer-Internals]] for the shadow-memory and quarantine mechanism that turns this reconstruction into an immediate report, and [[Cpp-Playbook-Heap-Corruption]] for the wider family of heap-metadata failures.

## Interview Questions

> [!question] L5: Why does the crash backtrace never show the function that called `free`?
>
> > [!success]- Answer
> > `free` runs, returns, and its stack frame is destroyed well before the dangling pointer is used.
> > The crash happens at the later use, so the unwinder, walking live frames from the faulting instruction outward, can only reach the use site and its callers.
> > The free is in the past, not on the stack, which is exactly why use-after-free is harder to triage than a null dereference: the evidence is in the heap, not the backtrace.
> > You recover the free by recognizing the allocator metadata the free left in the object.

> [!question] L5: The faulting `pc` has no symbol and points where there is no code.
> What single fact does that establish, and what do you read next?
>
> > [!success]- Answer
> > It establishes that control transferred to that address, so the fault is an instruction fetch, not a data access.
> > A bad data read instead leaves `pc` inside real code and puts the bad address in `si_addr`.
> > Next you read the register that supplied the branch target: on arm64 the function pointer was loaded into a register and reached by `blr`, and the object passed as the argument is still in `x0`, so `x/2gx $x0` shows the freed object's first two words.

> [!question] L6 (predict the output): A 48-byte object is `malloc`-ed, freed once as the only chunk of its size, and never reallocated.
> You dump its first two 64-bit words from a core on glibc 2.39.
> Predict both words in terms of the object address `A` and explain each.
>
> > [!success]- Answer
> > Word 0 is `(A >> 12) ^ 0 = A >> 12`.
> > `tcache_put` sets `e->next = PROTECT_PTR(&e->next, entries[idx])`, and because the bin was empty, `entries[idx]` was `NULL`, so the stored value is the object address shifted right by 12 (safe-linking mangles `NULL` into the shifted self-address).
> > Word 1 is the process-wide `tcache_key`, a random value from `getrandom` (falling back to `random_bits`) set by `e->key = tcache_key` (glibc 2.34 and later; through 2.33 the key was the per-thread `tcache` struct pointer).
> > Revealing word 0 with `(A >> 12) ^ word0` gives 0, which is the deterministic signature that the chunk is a freed list tail; the lab asserts the reveal (and that word 1 is no longer the payload), and that is why the recorded output is stable across address-space randomization.
> > Observed in the lab core: `A = 0xaaaac43162a0`, word 0 `0x0000000aaaac4316`, word 1 `0x5bf50079b7a03317`.

> [!question] L6 (debug this): A colleague opens the stripped core, sees `#0 0x... in ?? ()` followed only by more `?? ()` frames, and concludes the stack is corrupted and the core is useless.
> What did they skip, and how do you get three named frames?
>
> > [!success]- Answer
> > They skipped attaching debug info, so GDB has no symbols or line tables for the application and can name none of its frames.
> > The stack is not corrupted; it is unlabeled: `strip --strip-all` keeps `.eh_frame`, so GDB still walks every frame, and the lab's stripped core shows seven `??`-dominated frames down to `__libc_start_main`.
> > Read the build ID from the core with `eu-unstrip -n --core`, confirm it matches the shipped artifact, install the split debug file into a `.build-id` tree, and reopen with `gdb -iex "set debug-file-directory ..."` (details in [[Debug-a-Production-Crash-From-a-Core-Dump]]).
> > The frames then resolve to `execute`, `dispatch`, and `main` with `uaf.cpp` line numbers, and the real work (reading the freed object's tcache words) can begin.

> [!question] L6: Distinguish, from a single core, a use-after-free from a heap-buffer-overflow and from stack corruption.
> What in the object or stack tells them apart?
>
> > [!success]- Answer
> > Use-after-free: the victim object's first 16 bytes match a `tcache_entry` (glibc 2.34 and later), meaning word 0 reveals to a valid chunk or `NULL` under `(pos >> 12) ^ ptr` and word 1 equals the process `tcache_key`; the object is freed and self-describing.
> > Heap-buffer-overflow: the corruption sits in a neighboring chunk's header (size and prev-size fields are implausible, or glibc already aborted with `malloc(): ... corrupted`), and the victim itself is still a live allocation.
> > Stack corruption: the saved return address or frame pointer in the stack does not match any `r-x` mapping, the canary check failed (`__stack_chk_fail` near the top), and the heap metadata is intact.
> > The discriminator for use-after-free specifically is the safe-linking reveal plus the key sentinel, because neither appears in live data or in overflow debris by chance.

> [!question] L7: The register that held the freed object might have been reused by the time of the crash.
> How do you still recover the object address reliably, and why is reading a raw register risky?
>
> > [!success]- Answer
> > Prefer the DWARF description of the parameter over a raw register.
> > At `-O2` a value lives in different locations over different `pc` ranges, and the compiler is free to clobber an argument register after its last use, so reading `x0` is only correct where the location list says the value is there.
> > Selecting the victim frame and evaluating the variable (`print (unsigned long)o`) makes GDB apply the location list, including entry-value recovery (`o@entry`) when the caller's call-site description preserved it.
> > In this drill the body is `ldr x1, [x0]; blr x1`, so `x0` happens to still hold `o` at the fault (in the lab core `p/x $x0` and `print o` in frame 1 agree, and GDB shows `o=o@entry=...`), but the robust habit is to let DWARF resolve the parameter, which the triage script does, so the method survives a hotter function where the register was reused.

> [!question] L7: Safe-linking was added to glibc to harden exactly this metadata.
> Does it help or hinder your core-dump forensics, and what is the operational trade-off?
>
> > [!success]- Answer
> > Safe-linking (glibc 2.32, absent in 2.31; `PROTECT_PTR` occurs 0 times in 2.31 `malloc.c` and 5 times in 2.32) masks each singly-linked `next` pointer with `(pos >> 12) ^ ptr` to make tcache and fastbin poisoning harder for an attacker who controls a single write.
> > For forensics it neither helps nor hinders once you know the transform: you reveal the pointer with the same operation and recover the true link, and the fact that a field reveals cleanly is itself strong evidence the field is genuine allocator metadata rather than attacker or payload data.
> > The operational trade-off is that it assumes a minimum 4096-byte page for its 12-bit shift, so on larger pages it mixes in less entropy (the `malloc.c` Safe-Linking comment says so), and it protects the linked-list pointers only, not the chunk sizes, so it raises the bar on one class of heap exploit without being a general integrity check.
> > A triage tool that forgets to reveal the pointer will read a nonsense address and misdiagnose the chunk as corrupted.

> [!question] L8+: A latency-critical matching engine use-after-frees a pooled order object about once a week, and in every captured core the object has already been reallocated, so its bytes look valid.
> How do you catch the writer without the per-access overhead of AddressSanitizer in production?
>
> > [!success]- Answer
> > Make the fault happen at the stale access instead of at a later, innocent read, so the culprit is on the stack when the core is taken.
> > A sampling allocator such as GWP-ASan gives a small random fraction of allocations (default `SampleRate` 5000, so about 1 in 5000, as of 2026-11, Both) their own guarded slot between two inaccessible guard pages, and maps the slot inaccessible when it is freed, so a use-after-free on a sampled object faults at the using instruction with the using thread on the stack.
> > Two limits matter for a weekly bug: only sampled allocations are covered, so you catch it by running the sampler across a fleet and waiting, and detection is transient, because freed slots are randomly reused for later sampled allocations rather than quarantined for a fixed time.
> > The GWP-ASan docs describe the overhead as very low (about 40 KiB of memory per process with default settings), which is why it is affordable in production, unlike full AddressSanitizer (mechanism owned by [[Cpp-AddressSanitizer-and-LeakSanitizer-Internals]]).
> > On arm64 with the Memory Tagging Extension, the allocator retags a freed granule, and in synchronous mode (`PR_MTE_TCF_SYNC`, opt-in per process; checking is off by default) a stale pointer whose tag no longer matches raises `SIGSEGV` with `SEGV_MTESERR` before the access happens.
> > The tag is 4 bits per 16-byte granule, so detection is probabilistic, and the software HWASan runtime is documented as not meant to be linked into production executables; measure the throughput cost of the hardware mode on the real workload before claiming it is free (see [[Cpp-HWASan-and-Hardware-Memory-Tagging]]).
> > When the trigger can be reproduced in staging, record-and-replay lets you run the freed object's address backward to the exact free (see [[Record-and-Replay-rr-UDB-and-Pernosco]]).
> > The common thread is to convert a silent reuse into a loud, immediate fault whose core names the writer, rather than reconstructing a cold trail every week.

## Further Reading

- The GNU C Library 2.39, `malloc/malloc.c`, https://sourceware.org/git/?p=glibc.git;a=blob;f=malloc/malloc.c;hb=refs/heads/release/2.39/master.
  `tcache_put`, the `_int_free` double-free check (`free(): double free detected in tcache 2`), and the `PROTECT_PTR` / `REVEAL_PTR` safe-linking macros read in this drill.
- Debugging with GDB, Convenience Variables and Core File Generation, https://sourceware.org/gdb/current/onlinedocs/gdb.html/Convenience-Vars.html and https://sourceware.org/gdb/current/onlinedocs/gdb.html/Core-File-Generation.html.
  `$_siginfo` fields and the `generate-core-file` fallback the lab uses when `core_pattern` pipes the core elsewhere.
- sigaction(2), Linux man-pages project, https://man7.org/linux/man-pages/man2/sigaction.2.html.
  The `si_code` values that separate an unmapped fetch (`SEGV_MAPERR`) from an alignment fault (`BUS_ADRALN`), the two outcomes of the wild branch here.
- GNU binutils objcopy, https://sourceware.org/binutils/docs/binutils/objcopy.html.
  `--only-keep-debug` and `strip --strip-all`, the split-debug recipe the lab reproduces.
- Clang AddressSanitizer, https://clang.llvm.org/docs/AddressSanitizer.html, and the AddressSanitizerAlgorithm wiki, https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm.
  The shadow-memory and quarantine design (documented in the wiki, not the Clang page) behind the `heap-use-after-free` report in the contrast run.

- GWP-ASan, https://llvm.org/docs/GwpAsan.html, and Linux kernel arm64 Memory Tagging Extension, https://www.kernel.org/doc/html/latest/arch/arm64/memory-tagging-extension.html.
  The sampling, guard-page, and transient use-after-free semantics of GWP-ASan, and the opt-in synchronous tag-check fault mode (`SEGV_MTESERR`) used in the L8+ answer.
- Clang HWASan design, https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html.
  Why `free` retagging detects use-after-free without a quarantine, and the statement that the runtime is not for production executables.

## Related

- Prerequisites: [[Debug-a-Production-Crash-From-a-Core-Dump]]
- Next: [[Drill-Debugging-Asyncio-and-Tokio-Deadlock]]
- Lab: `code/03-Debugging/drill_uaf_core/` (requires: linux)
