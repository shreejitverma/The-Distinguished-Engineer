---
type: problem
track: [sde, distinguished, low-latency]
level:
status: solid
last_reviewed:
sources:
  - "CodeSourcery, Compaq, EDG, HP, IBM, Intel, SGI, Itanium C++ ABI, 2026, https://itanium-cxx-abi.github.io/cxx-abi/abi.html"
  - "CodeSourcery, Compaq, EDG, HP, IBM, Intel, SGI, Itanium C++ ABI: Exception Handling (abi-eh.html), 2026, https://itanium-cxx-abi.github.io/cxx-abi/abi-eh.html"
  - "LLVM Project, libc++abi: private_typeinfo.cpp and cxa_personality.cpp, 2026, https://github.com/llvm/llvm-project/tree/main/libcxxabi"
  - "Free Software Foundation, GCC libstdc++: typeinfo and eh_personality.cc, 2026, https://gcc.gnu.org/onlinedocs/libstdc++/"
  - "Michael Kerrisk et al., dlopen(3): open and close a shared object, Linux man-pages project, 2026, https://man7.org/linux/man-pages/man3/dlopen.3.html"
  - "Free Software Foundation, The GNU Linker: VERSION Command and Linker Scripts, 2026, https://sourceware.org/binutils/docs/ld/VERSION.html"
  - "GCC Project, Visibility of Symbols (GCC Wiki), 2026, https://gcc.gnu.org/wiki/Visibility"
  - "Ulrich Drepper, How To Write Shared Libraries, Red Hat, 2011, https://www.akkadia.org/drepper/dsohowto.pdf"
  - "LLVM Project, libc++abi private_typeinfo.cpp (is_equal, __class_type_info::can_catch, __dynamic_cast), main branch, 2026, https://github.com/llvm/llvm-project/blob/main/libcxxabi/src/private_typeinfo.cpp"
  - "LLVM Project, libc++abi cxa_exception.cpp (__cxa_throw, failed_throw), main branch, 2026, https://github.com/llvm/llvm-project/blob/main/libcxxabi/src/cxa_exception.cpp"
  - "LLVM Project, libc++ <typeinfo> (_LIBCPP_TYPEINFO_COMPARISON_IMPLEMENTATION), main branch, 2026, https://github.com/llvm/llvm-project/blob/main/libcxx/include/typeinfo"
  - "Free Software Foundation, GCC libsupc++ typeinfo (type_info::operator==), master branch, 2026, https://github.com/gcc-mirror/gcc/blob/master/libstdc++-v3/libsupc++/typeinfo"
  - "Free Software Foundation, GCC libsupc++ class_type_info.cc (__class_type_info::__do_catch), master branch, 2026, https://github.com/gcc-mirror/gcc/blob/master/libstdc++-v3/libsupc++/class_type_info.cc"
  - "The SCO Group, System V ABI gABI: Symbol Table (Symbol Visibility), 2013, https://www.sco.com/developers/gabi/latest/ch4.symtab.html"
  - "Free Software Foundation, The GNU Linker: Command-line Options (-Bsymbolic-functions, -z now, -z relro), 2026, https://sourceware.org/binutils/docs/ld/Options.html"
---

# Drill: Cross-DSO Exception and RTTI Failure

> [!summary] Scope
> Blind problem drill presenting a mysterious std::terminate crash when an exception is thrown across a dlopen(..., RTLD_LOCAL) shared library boundary in a libc++ build on Linux, where catch matching compares typeinfo name addresses (libstdc++ compares mangled names for external-linkage types and usually masks the bug).
> Requires the engineer to diagnose typeinfo pointer inequality resulting from hidden RTTI visibility, inspect the Itanium personality routine exception match logic, and rectify symbol export maps.
> Reaches Debug, validating mastery of Cpp-Itanium-ABI-Object-Model-Vtables-and-RTTI.md and Dynamic-Linking-PLT-GOT-and-Loaders.md.

## Related

- Prerequisites: [[Cpp-Itanium-ABI-Object-Model-Vtables-and-RTTI]], [[Stack-Unwinding-CFI-and-Exception-ABI]], [[Dynamic-Linking-PLT-GOT-and-Loaders]]
- See also: [[02-Programming-Languages/C++/CPP_Zero_to_Godhood/Volume_08_Advanced_Systems/Chapter_102_Linking_Loading_ABI/Chapter_102_Linking_Loading_ABI_and_WPO|Chapter_102_Linking_Loading_ABI_and_WPO]]
- Lab: `code/00-Program-Execution/drill_cross_dso_rtti/` (requires: linux)

## Reproduction

In modular C++ architectures, plugin systems frequently load isolated Dynamic Shared Objects (DSOs) at runtime using `dlopen(..., RTLD_NOW | RTLD_LOCAL)`.
To prevent namespace pollution and optimize dynamic relocation overhead, production toolchains routinely compile all shared libraries and host binaries with default hidden visibility (`-fvisibility=hidden`).
Communication across the dynamic boundary relies on a shared header declaring abstract interfaces and common domain exception types.

Consider a minimal reproduction located in `code/00-Program-Execution/drill_cross_dso_rtti/`:

A shared domain exception header `error.hpp` defines a common error type derived from `std::exception`.
The `ERROR_EXPORT` and `USE_KEY_FUNCTION` macros are off in the broken build; the remediations below switch them on:

```cpp
#pragma once
#include <exception>

#ifndef ERROR_EXPORT
#define ERROR_EXPORT
#endif

#ifdef USE_KEY_FUNCTION
struct ERROR_EXPORT PluginError : public std::exception {
    ~PluginError() override;
    const char* what() const noexcept override {
        return "Cross-DSO PluginError: operation failed in worker thread";
    }
};
#else
struct ERROR_EXPORT PluginError : public std::exception {
    const char* what() const noexcept override {
        return "Cross-DSO PluginError: operation failed in worker thread";
    }
};
#endif
```

A dynamic plugin library `plugin.cpp` implements a worker routine that throws `PluginError`:

```cpp
#include "error.hpp"
#include <typeinfo>

extern "C" __attribute__((visibility("default"))) const std::type_info* get_plugin_typeinfo() {
    return &typeid(PluginError);
}

extern "C" __attribute__((visibility("default"))) void trigger_error() {
    throw PluginError();
}
```

A host application `host.cpp` dynamically loads the plugin via `dlopen(..., RTLD_NOW | RTLD_LOCAL)` and executes the function inside a dedicated `try / catch` block.
It prints three equality measurements of the host's and the plugin's `typeid(PluginError)`, then throws.
In strict mode only a `PluginError` handler exists, so a failed match has nowhere to go but `std::terminate`:

```cpp
#include "error.hpp"
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <typeinfo>

#ifndef DLMODE
#define DLMODE RTLD_LOCAL
#endif

int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s <plugin_path> [--strict]\n", argv[0]);
        return 2;
    }

    void* handle = dlopen(argv[1], RTLD_NOW | DLMODE);
    if (!handle) {
        std::fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return 2;
    }

    auto get_ti = reinterpret_cast<const std::type_info* (*)()>(dlsym(handle, "get_plugin_typeinfo"));
    auto trigger = reinterpret_cast<void (*)()>(dlsym(handle, "trigger_error"));
    if (!get_ti || !trigger) {
        std::fprintf(stderr, "dlsym failed: %s\n", dlerror());
        return 2;
    }

    const std::type_info* host_ti = &typeid(PluginError);
    const std::type_info* plugin_ti = get_ti();
    std::printf("addr_eq=%d op_eq=%d strcmp_eq=%d\n", host_ti == plugin_ti, *host_ti == *plugin_ti,
                std::strcmp(host_ti->name(), plugin_ti->name()) == 0);
    std::fflush(stdout);

    const bool strict = argc > 2 && std::strcmp(argv[2], "--strict") == 0;
    if (strict) {
        // Only a PluginError handler exists: a failed match has nowhere to go but std::terminate.
        try {
            trigger();
        } catch (const PluginError&) {
            std::puts("outcome=caught-PluginError");
        }
    } else {
        // std::exception is a base class resolved through the shared C++ runtime, so it still matches.
        try {
            trigger();
        } catch (const PluginError&) {
            std::puts("outcome=caught-PluginError");
        } catch (const std::exception&) {
            std::puts("outcome=fell-back-to-std-exception");
        }
    }
    return 0;
}
```

Both components are compiled with `-fvisibility=hidden`.
The lab (`run.sh`, `# requires: linux`) builds the same sources twice: with `clang++ -stdlib=libc++` (real libc++ and libc++abi) and with `g++` (libstdc++ and libsupc++).
Nothing is emulated: every catch outcome below is produced by the real personality routine of the named runtime.
The pinned container image carries `clang`, `libc++-dev`, and `libc++abi-dev` for this (clang 18.1.3, libc++ 18, g++ 13.3.0, GNU ld 2.42 on Ubuntu 24.04, as of 2026-11, Linux).

```bash
clang++ -stdlib=libc++ -std=c++17 -Wall -Wextra -fPIC -shared -fvisibility=hidden plugin.cpp -o libplugin_broken.so
clang++ -stdlib=libc++ -std=c++17 -Wall -Wextra -fvisibility=hidden host.cpp -ldl -o host_broken
```

Run it with `make verify LAB=00-Program-Execution/drill_cross_dso_rtti/` from `18-Developer-Toolchain/code/`.
On macOS the same sources were also run outside `make verify` with Apple clang 21.0.0 (macOS 27.2 arm64, as of 2026-11): the fallback build caught `std::exception` and the strict build aborted with exit status 134.
A one-off run of the lab in an `ubuntu:24.04` `linux/amd64` container (as of 2026-11) gave the same outcomes as the aarch64 recording; only the relocation names in section 6 differ.

Execute the compiled host against the plugin under both fallback and strict catch modes:

```bash
./host_broken ./libplugin_broken.so
./host_broken ./libplugin_broken.so --strict
```

## Observed Symptom

When executed, the program exhibits two distinct anomalous behaviors depending on whether catch fallback handlers are present:

Under fallback mode, a real `libc++` build of `host_broken` completely ignores the matching `catch (const PluginError& e)` block.
Instead, execution drops through to the generic `catch (const std::exception& e)` clause.

Under strict mode (where only `catch (const PluginError& e)` is registered), the host crashes fatally with an unhandled exception abort:

```text
libc++abi: terminating due to uncaught exception of type PluginError: Cross-DSO PluginError: operation failed in worker thread
Aborted (core dumped)
```

The first line is the exact `libc++abi` message, observed with `libc++` 18 on Linux aarch64 and Apple clang 21 on macOS arm64 (as of 2026-11).
The second line is printed by the shell, not the program, and varies: bash printed `Aborted` on Linux (with `(core dumped)` only when a core is written) and `Abort trap: 6` on macOS.
The shell reports process termination with exit status 134 (`128 + SIGABRT`).
`std::terminate()` is reached from `__cxa_throw`, not from inside the unwinder: `_Unwind_RaiseException` returns `_URC_END_OF_STACK` after Phase 1 finds no handler, and `libc++abi` `__cxa_throw` then calls `failed_throw`, which calls `std::__terminate` (`libcxxabi/src/cxa_exception.cpp`), even though the thrown object is an instance of `PluginError` and the calling frame contains an active `catch (const PluginError&)` handler.

The golden output below is `code/00-Program-Execution/drill_cross_dso_rtti/expected/linux-aarch64.txt`, recorded in the pinned container (Linux aarch64, as of 2026-11), byte for byte.
Every line is measured from a real libc++ or libstdc++ build.
`run.sh` also exits non-zero when any case ends differently from its expected outcome, so the controls (sections 4 and 5) are checks, not decoration.
The `libc++abi: terminating` line is the first stderr line of the aborted process.

```text
=== Drill: Cross-DSO Exception and RTTI Failure ===
--- 1. Reproduction: real libc++ with -fvisibility=hidden ---
[libc++ fallback handler present]
  addr_eq=0 op_eq=0 strcmp_eq=1
  outcome=fell-back-to-std-exception
  exit status 0
[libc++ strict, RTLD_LOCAL]
  addr_eq=0 op_eq=0 strcmp_eq=1
  stderr: libc++abi: terminating due to uncaught exception of type PluginError: Cross-DSO PluginError: operation failed in worker thread
  exit status 134
[libc++ strict, RTLD_GLOBAL]
  addr_eq=0 op_eq=0 strcmp_eq=1
  stderr: libc++abi: terminating due to uncaught exception of type PluginError: Cross-DSO PluginError: operation failed in worker thread
  exit status 134
--- 2. Diagnosis: libstdc++ masks the same bug ---
[libstdc++ fallback handler present]
  addr_eq=0 op_eq=1 strcmp_eq=1
  outcome=caught-PluginError
  exit status 0
[libstdc++ strict]
  addr_eq=0 op_eq=1 strcmp_eq=1
  outcome=caught-PluginError
  exit status 0
--- 3. Diagnosis: ELF symbol inspection ---
dynamic RTTI symbols in libplugin_broken.so: 0
libplugin_broken.so .symtab (bind visibility name):
  LOCAL DEFAULT _ZTI11PluginError
  LOCAL DEFAULT _ZTS11PluginError
plugin.o _ZTI11PluginError, -fvisibility=hidden: WEAK HIDDEN
plugin.o _ZTI11PluginError, default flags:       WEAK DEFAULT
--- 4. Remediation A: visibility annotation plus --export-dynamic ---
[A without --export-dynamic, RTLD_LOCAL (control)]
  addr_eq=0 op_eq=0 strcmp_eq=1
  stderr: libc++abi: terminating due to uncaught exception of type PluginError: Cross-DSO PluginError: operation failed in worker thread
  exit status 134
[A without --export-dynamic, RTLD_GLOBAL (control)]
  addr_eq=0 op_eq=0 strcmp_eq=1
  stderr: libc++abi: terminating due to uncaught exception of type PluginError: Cross-DSO PluginError: operation failed in worker thread
  exit status 134
[A with --export-dynamic]
  addr_eq=1 op_eq=1 strcmp_eq=1
  outcome=caught-PluginError
  exit status 0
--- 5. Remediation B: version script that lists the RTTI symbols ---
[B map lists _ZTI/_ZTS]
  addr_eq=1 op_eq=1 strcmp_eq=1
  outcome=caught-PluginError
  exit status 0
[B map omits _ZTI/_ZTS (control)]
  addr_eq=0 op_eq=0 strcmp_eq=1
  stderr: libc++abi: terminating due to uncaught exception of type PluginError: Cross-DSO PluginError: operation failed in worker thread
  exit status 134
dynamic RTTI symbols, annotated plugin, map lists them: 2
dynamic RTTI symbols, annotated plugin, map omits them: 0
dynamic RTTI symbols, hidden plugin, map lists them:    0
--- 6. Remediation C: key function in a common shared library ---
[C key function in libcommon.so, host not exporting]
  addr_eq=1 op_eq=1 strcmp_eq=1
  outcome=caught-PluginError
  exit status 0
libcommon.so defines _ZTI11PluginError: 1
host_common relocations against _ZTI11PluginError: R_AARCH64_ABS64,R_AARCH64_GLOB_DAT
host_common defines _ZTI11PluginError itself: 0
libplugin_common.so relocations against _ZTI11PluginError: R_AARCH64_GLOB_DAT
libplugin_common.so defines _ZTI11PluginError itself: 0
=== All drill checks passed ===
```

Curiously, recompiling the identical source code using GCC's GNU `libstdc++` on Linux masks the bug entirely:
The GCC build catches `PluginError` successfully, even though `Typeinfo address equality` remains `false`.
Consequently, unit test suites run on Linux with GCC succeed, whereas builds targeting macOS (which uses `libc++` natively) or hermetic Linux Clang/`libc++` containers abort in production (both confirmed for this hidden-visibility build, as of 2026-11).

## Progressive Hint Ladder

> [!tip]- Hint 1: Inspecting Exception Object Types and Catch Clause Matching
> An exception catch clause matches a thrown object by interrogating the runtime type information (RTTI) structure stored within the exception header allocated by `__cxa_allocate_exception`.
> When `PluginError` inherits from `std::exception`, why does `catch (const std::exception&)` match while `catch (const PluginError&)` fails?
> Where does `typeid(std::exception)` reside in memory compared to `typeid(PluginError)`?

> [!tip]- Hint 2: Examining Typeinfo Pointer Addresses
> Notice that `Typeinfo address equality` evaluates to `false` while `Typeinfo strcmp equality` evaluates to `true`.
> The Itanium C++ ABI specification models every type using an instance of `std::type_info` emitted under the mangled symbol `_ZTI<length><name>`.
> In `libc++abi`, how does the personality routine (`__gxx_personality_v0`) evaluate whether the catch clause type matches the thrown exception type?
> Does `libc++abi` compare the `type_info` objects by address, compare the name strings by address, or compare the name strings by content (`strcmp`)?

> [!tip]- Hint 3: Querying the Dynamic Symbol Table with readelf
> Run `readelf --dyn-syms libplugin_broken.so` and `readelf --syms libplugin_broken.so`.
> Notice that `_ZTI11PluginError` and `_ZTS11PluginError` are completely absent from the `.dynsym` dynamic symbol table.
> In `.symtab`, they are marked with `LOCAL` binding (GNU ld 2.42 shows `DEFAULT` in the visibility column after localizing them; the `HIDDEN` marking is visible in `readelf --syms` on the object file, and lab section 3 prints both).
> When `dlopen` loads `libplugin_broken.so` with `RTLD_LOCAL`, how can the dynamic linker (`ld.so`) merge or interpose symbols that do not exist in `.dynsym`?

> [!tip]- Hint 4: Tracking Vague Linkage, Visibility Attributes, and Symbol Demotion
> In C++, inline virtual functions (such as `what() const noexcept override` defined inside the class body) have vague linkage.
> The compiler emits duplicate copies of the vtable (`_ZTV11PluginError`) and RTTI structure (`_ZTI11PluginError`) in every translation unit that references them.
> When compiling with `-fvisibility=hidden`, what visibility does the compiler assign to these vague linkage symbols by default?
> If you apply a linker version script (`--version-script=export.map`) with `local: *;`, what happens to default-visibility RTTI symbols if they are omitted from the `global:` list?

> [!tip]- Hint 5: Reconciling dlopen Scopes and Dynamic Symbol Export
> Even if you annotate `PluginError` with `__attribute__((visibility("default")))` in the shared library, why does a Linux `libc++` host executable still crash if the host binary was linked without `-Wl,--export-dynamic` (`-rdynamic`)?
> How does the dynamic loader's global search list resolve weak symbols between an executable and a library loaded with `RTLD_LOCAL`?
> What happens if the RTTI symbol is emitted with an out-of-line key function inside a dedicated shared library linked by both components?

## Model Solution

> [!success]- Solution: Root Cause and Triple Remediation
>
> ### Root Cause Analysis
>
> The crash stems from an ABI mismatch in runtime type identification caused by the confluence of three low-level mechanisms:
>
> 1. **Vague Linkage and Symbol Localization:**
>    Because `PluginError` defines its virtual member function `what()` inline inside `error.hpp`, the class possesses no out-of-line key function.
>    According to the [[Cpp-Itanium-ABI-Object-Model-Vtables-and-RTTI|Itanium C++ ABI (Sections 5.2.3 and 5.2.4)]], classes without a key function emit their virtual table (`_ZTV11PluginError`), typeinfo structure (`_ZTI11PluginError`), and typeinfo name string (`_ZTS11PluginError`) into every object that references them, which GCC 13.3 and clang 18 emit as weak symbols in COMDAT groups.
>    When compiled with `-fvisibility=hidden`, the compiler overrides the default visibility of these weak symbols and marks `_ZTI11PluginError` as `STV_HIDDEN` (`readelf --syms plugin.o` shows `WEAK HIDDEN`, versus `WEAK DEFAULT` without the flag).
>    During static link time, the link editor must remove a hidden symbol or convert it to `STB_LOCAL` (System V gABI, Symbol Visibility), so GNU ld 2.42 makes `_ZTI11PluginError` `LOCAL` in the plugin's `.symtab` and omits it from the dynamic symbol table (`.dynsym`).
>
> 2. **Dynamic Linker Isolation under RTLD_LOCAL:**
>    When the host invokes `dlopen("./libplugin_broken.so", RTLD_NOW | RTLD_LOCAL)`, the dynamic loader (`ld.so`) maps the shared object without adding its symbols to the global lookup scope.
>    Because `_ZTI11PluginError` is not present in `libplugin_broken.so`'s `.dynsym`, and the host's copy is hidden and not exported either, the dynamic linker never sees either symbol and cannot interpose one over the other.
>    The `RTLD_LOCAL` flag is not the cause: the same build loaded with `RTLD_GLOBAL` aborts identically under `libc++` 18 (lab section 1, libc++ 18 on Linux aarch64, as of 2026-11).
>    Consequently, two distinct physical instances of `_ZTI11PluginError` exist in the process memory space:
>    - Instance A resides in the host executable's `.data.rel.ro` segment.
>    - Instance B resides in the plugin DSO's `.data.rel.ro` segment.
>
> 3. **Personality Routine Type Matching Logic:**
>    When the plugin executes `throw PluginError()`, the compiler generates a call to `__cxa_throw(&exception_obj, &_ZTI11PluginError_plugin, &destructor)`.
>    The unwinder runtime initiates two-phase stack unwinding via `_Unwind_RaiseException`, described in [[Stack-Unwinding-CFI-and-Exception-ABI]].
>    In Phase 1 (`_UA_SEARCH_PHASE`), the unwinder invokes the personality routine `__gxx_personality_v0` for each frame.
>    For the host frame, the personality routine reads the Language-Specific Data Area (LSDA) action table.
>    The LSDA type table contains a reference to the host's expected catch type: `&_ZTI11PluginError_host`.
>    The personality routine calls the runtime type matching helper `can_catch`:
>    - **LLVM libc++abi (`private_typeinfo.cpp`):**
>      `__class_type_info::can_catch` calls `is_equal(this, thrown_type, false)`, which with `use_strcmp == false` evaluates `std::type_info::operator==`:
>      ```cpp
>      static inline bool is_equal(const std::type_info* x, const std::type_info* y, bool use_strcmp) {
>        if (!use_strcmp)
>          return *x == *y;
>        return x == y || strcmp(x->name(), y->name()) == 0;
>      }
>
>      bool __class_type_info::can_catch(const __shim_type_info* thrown_type, void*& adjustedPtr) const {
>        if (is_equal(this, thrown_type, false))
>          return true;
>        // ... base class traversal ...
>      }
>      ```
>      In `libc++`'s default ELF configuration (`_LIBCPP_TYPEINFO_COMPARISON_IMPLEMENTATION == 1`, "Unique"), `operator==` compares the addresses of the type name strings, which assumes one RTTI copy per type in the program.
>      Apple arm64 defaults to mode 3 ("NonUniqueARMRTTIBit") and COFF and XCOFF to mode 2 ("NonUnique", address or `strcmp`); see `libcxx/include/typeinfo` (as of 2026-11).
>      No `_LIBCXX_DYNAMIC_FALLBACK` macro exists in current `libc++abi`; its `_LIBCXXABI_FORGIVING_DYNAMIC_CAST` affects only `dynamic_cast`, and the source notes it does not help `can_catch` because `can_catch` passes `use_strcmp=false`.
>      Because the host's and the plugin's `_ZTS11PluginError` are distinct local copies, the comparison fails.
>      When traversing base classes, it checks `std::exception`.
>      Because `std::exception` is defined in `libc++abi.so.1` (not `libc++.so.1`, as of `libc++` 18 on Ubuntu 24.04) with `STV_DEFAULT` visibility, both the host and the plugin resolve `_ZTISt9exception` to the single dynamic instance in `libc++abi.so.1`.
>      Thus, `catch (const std::exception&)` matches, while `catch (const PluginError&)` is rejected.
>      In strict mode, with no matching handler found, `_Unwind_RaiseException` returns `_URC_END_OF_STACK` at the end of Phase 1 without unwinding any frame.
>      `__cxa_throw` then calls `failed_throw`, which calls `std::__terminate()`.
>    - **GNU libstdc++ (`eh_personality.cc` and `typeinfo`):**
>      In GNU `libsupc++`, `__class_type_info::__do_catch` (`class_type_info.cc`) evaluates `*this == *thr_type` first.
>      On Linux (where `__GXX_MERGED_TYPEINFO_NAMES` defaults to `0`, confirmed with GCC 13.3 on aarch64, as of 2026-11), `std::type_info::operator==` performs a fallback string comparison (abridged from `libsupc++/typeinfo`):
>      ```cpp
>      bool type_info::operator==(const type_info& __arg) const noexcept {
>          if (__name == __arg.__name)
>              return true;
>          // A leading '*' marks a name that must be compared by address (internal linkage types).
>          return __name[0] != '*' && __builtin_strcmp(__name, __arg.name()) == 0;
>      }
>      ```
>      Because the mangled string `"11PluginError"` matches via `strcmp`, `libstdc++` accepts the catch clause, masking the underlying type duplication bug.
>
> ```mermaid
> flowchart TD
>     subgraph Host["Host Executable (fvisibility=hidden)"]
>         H_Catch["catch (const PluginError&)"]
>         H_LSDA["LSDA Type Table: &_ZTI_host"]
>         H_TI["_ZTI11PluginError (Host Instance)<br/>Address: 0x5555... (illustrative)<br/>Hidden, not in .dynsym"]
>         H_Catch --> H_LSDA --> H_TI
>     end
> 
>     subgraph Plugin["Plugin DSO (dlopen RTLD_LOCAL)"]
>         P_Throw["throw PluginError()"]
>         P_Alloc["__cxa_throw(..., &_ZTI_plugin)"]
>         P_TI["_ZTI11PluginError (Plugin Instance)<br/>Address: 0x7fff... (illustrative)<br/>Binding: LOCAL"]
>         P_Throw --> P_Alloc --> P_TI
>     end
> 
>     subgraph Unwinder["libc++abi Personality Routine"]
>         Match{"can_catch(H_TI, P_TI)<br/>Name Address Comparison"}
>         H_TI -->|this| Match
>         P_TI -->|thrown_type| Match
>         Match -->|H_TI != P_TI| Reject["Match Failed<br/>(Drop to base class or std::terminate)"]
>         Match -.->|If addresses unified| Accept["Match Succeeded<br/>(Execute Catch Handler)"]
>     end
> ```
>
> ---
>
> ### Remediation Strategies
>
> Depending on build system constraints and packaging models, three standard remediations rectify the defect:
>
> #### Remediation A: Class Visibility Annotation and Dynamic Export
> Annotate the exception class definition with `__attribute__((visibility("default")))`.
> This forces the compiler to emit `_ZTI11PluginError` and `_ZTS11PluginError` with `STV_DEFAULT` visibility into `.dynsym`.
> Crucially, the host binary must also be linked with `-Wl,--export-dynamic` (or `-rdynamic`), enabling the dynamic loader to interpose the host's RTTI symbol over the plugin's:
>
> ```cpp
> struct __attribute__((visibility("default"))) PluginError : public std::exception {
>     const char* what() const noexcept override {
>         return "Cross-DSO PluginError: operation failed in worker thread";
>     }
> };
> ```
>
> Link flags (as the lab runs them, with real `libc++`):
> ```bash
> clang++ -stdlib=libc++ -std=c++17 -Wall -Wextra -fPIC -shared -fvisibility=hidden \
>     -DERROR_EXPORT='__attribute__((visibility("default")))' \
>     plugin.cpp -o libplugin_annotated.so
> clang++ -stdlib=libc++ -std=c++17 -Wall -Wextra -fvisibility=hidden \
>     -DERROR_EXPORT='__attribute__((visibility("default")))' \
>     -Wl,--export-dynamic host.cpp -ldl -o host_annotated
> ```
> Lab section 4 (`libc++` 18 on Linux aarch64, as of 2026-11): the annotated build linked without `--export-dynamic` still aborts under both `RTLD_LOCAL` and `RTLD_GLOBAL`, and adding `--export-dynamic` unifies the addresses and the catch succeeds.
> macOS arm64 differs (Apple clang 21, as of 2026-11): the annotation alone makes the catch succeed, because mode 3 gives default-visibility weak RTTI the non-unique bit and compares names with `strcmp`, and the addresses stay split even with `-Wl,-export_dynamic` (the `ld64` spelling).
>
> #### Remediation B: Linker Version Script Preserving RTTI Symbols
> When shared libraries employ a linker version script (`--version-script=export.map`) with a wildcard `local: *;` rule, the linker strips all unlisted symbols from `.dynsym`, even if marked with default visibility in source code.
> Lab section 5 (GNU ld 2.42 and `libc++` 18, Linux aarch64, as of 2026-11): an annotated plugin linked with a map that omits `_ZTI11PluginError` and `_ZTS11PluginError` aborts in strict mode, and listing them restores the catch.
> A map cannot export a hidden symbol: the same map applied to the unannotated, hidden plugin leaves both symbols out of `.dynsym` (the lab prints 2, 0, and 0 dynamic RTTI symbols for the three plugins), so the annotation is still required.
> The version script must explicitly list the mangled RTTI symbols in the `global:` clause:
>
> ```text
> {
>     global:
>         trigger_error;
>         get_plugin_typeinfo;
>         _ZTI11PluginError;
>         _ZTS11PluginError;
>     local:
>         *;
> };
> ```
>
> Link flags:
> ```bash
> clang++ -stdlib=libc++ -std=c++17 -Wall -Wextra -fPIC -shared -fvisibility=hidden \
>     -DERROR_EXPORT='__attribute__((visibility("default")))' \
>     -Wl,--version-script=plugin_export.map plugin.cpp -o libplugin_map.so
> ```
>
> #### Remediation C: Dedicated Common Interface Shared Library (Architectural Best Practice)
> To completely decouple the host executable from `-rdynamic` and eliminate vague linkage across plugins, isolate common exception and interface definitions into a dedicated shared library `libcommon.so`.
> Anchor the RTTI and virtual tables by declaring an out-of-line virtual destructor (the key function) in `libcommon.so`:
>
> In `error.hpp`:
> ```cpp
> struct __attribute__((visibility("default"))) PluginError : public std::exception {
>     ~PluginError() override;
>     const char* what() const noexcept override {
>         return "Cross-DSO PluginError: operation failed in worker thread";
>     }
> };
> ```
>
> In `common.cpp` (compiled into `libcommon.so`):
> ```cpp
> #include "error.hpp"
> PluginError::~PluginError() = default;
> ```
>
> Both the host executable and all plugin shared libraries link against `libcommon.so`:
>
> ```bash
> clang++ -stdlib=libc++ -std=c++17 -Wall -Wextra -fPIC -shared common.cpp -o libcommon.so
> clang++ -stdlib=libc++ -std=c++17 -Wall -Wextra -fPIC -shared -fvisibility=hidden \
>     -DUSE_KEY_FUNCTION -DERROR_EXPORT='__attribute__((visibility("default")))' \
>     plugin.cpp -L. -lcommon -Wl,-rpath,'$ORIGIN' -o libplugin_common.so
> clang++ -stdlib=libc++ -std=c++17 -Wall -Wextra -fvisibility=hidden \
>     -DUSE_KEY_FUNCTION -DERROR_EXPORT='__attribute__((visibility("default")))' \
>     host.cpp -L. -lcommon -ldl -Wl,-rpath,'$ORIGIN' -o host_common
> ```
>
> Because the key function resides exclusively in `libcommon.so`, neither the host nor the plugin emits local weak copies of `_ZTI11PluginError`.
> Instead, both binaries carry `_ZTI11PluginError` as an undefined (`UND`) symbol with a dynamic relocation (lab section 6 prints `R_AARCH64_GLOB_DAT` for the plugin, plus an `R_AARCH64_ABS64` for the host's exception-table type reference; the one-off `linux/amd64` run printed `R_X86_64_GLOB_DAT` and `R_X86_64_64`, as of 2026-11) resolved by `ld.so` to the single physical instance in `libcommon.so`.
> Address equality is guaranteed regardless of `dlopen` flags or host dynamic export settings: lab section 6 passes with a host that has no `--export-dynamic`, and its relocation lines show that neither binary defines `_ZTI11PluginError` itself.

## Core Takeaway

Never allow polymorphic types or exception objects to cross dynamic library boundaries with hidden visibility.
In the [[Cpp-Itanium-ABI-Object-Model-Vtables-and-RTTI|Itanium C++ ABI]], RTTI identity and exception matching depend on unique, process-wide `type_info` instances merged via COMDAT vague linkage and dynamic symbol interposition.
Compiling with `-fvisibility=hidden` converts vague linkage weak symbols into local hidden symbols.
Local symbols never reach `.dynsym`, so the dynamic loader cannot merge them under any `dlopen` flag (`RTLD_LOCAL` or `RTLD_GLOBAL`), producing split type identities across memory segments.

Furthermore, never rely on GNU `libstdc++` test passes to validate cross-DSO exception safety.
On Linux (as of 2026-11), `libstdc++` sets `__GXX_MERGED_TYPEINFO_NAMES` to `0` and falls back to string comparison via `strcmp`, concealing latent symbol localization bugs.
In contrast, LLVM's `libc++` on ELF platforms defaults to comparing type name addresses (the "Unique" mode, which assumes the Itanium ABI's one-copy rule), instantly converting duplicated RTTI into fatal `std::terminate` crashes (as of 2026-11, Linux; Apple arm64 uses a hybrid mode that still fails for hidden-visibility types).

For production plugin systems:
1. Always declare polymorphic exception classes with explicit `__attribute__((visibility("default")))`.
2. Explicitly retain mangled `_ZTI*` and `_ZTS*` symbols in dynamic linker export maps.
3. For enterprise platforms, house cross-boundary exception types in a dedicated shared library anchored by an out-of-line key function, ensuring clean dynamic resolution through [[Dynamic-Linking-PLT-GOT-and-Loaders]].

## Interview Questions

### Question 1: Predict the Output (RTTI Address vs String Equality)
Consider a plugin DSO loaded with `dlopen("plugin.so", RTLD_NOW | RTLD_LOCAL)`.
Both the executable and the DSO include a common header defining `struct CustomError : public std::runtime_error { using std::runtime_error::runtime_error; };`.
Both binaries are compiled with `-fvisibility=hidden`.
The executable executes:
```cpp
const std::type_info* h_ti = &typeid(CustomError);
const std::type_info* p_ti = plugin_get_typeinfo();
std::cout << (h_ti == p_ti) << " " << (*h_ti == *p_ti) << "\n";
```
Predict the stdout output when compiled under:
1. Clang with `libc++` on Linux or macOS.
2. GCC with `libstdc++` on Linux.

> [!success]- Answer
> 1. Under Clang with `libc++` on Linux and macOS, the output is:
>    `0 0`
>    Explanation: `h_ti == p_ti` tests pointer equality of the two `std::type_info` instances, which evaluates to `0` because `-fvisibility=hidden` localized `_ZTI11CustomError` in each binary.
>    `*h_ti == *p_ti` invokes `std::type_info::operator==`.
>    In `libc++` on Linux (the "Unique" mode), `operator==` compares the underlying name string pointers (`__type_name == __arg.__type_name`).
>    On macOS arm64 (mode 3), a type declared with hidden visibility is treated as unique, so its comparison is also by address.
>    Because the mangled name strings (`_ZTS11CustomError`) were also emitted with hidden visibility as separate copies in the two binaries, the name pointers differ, yielding `0`.
>    This exact `std::runtime_error` variant was run (as of 2026-11): Linux aarch64 with libc++ 18 printed `0 0`, Linux aarch64 with g++ 13.3 and libstdc++ printed `0 1`, and macOS arm64 with Apple clang 21 printed `0 0`.
>    The drill's `PluginError` gives the same `0 0` and `0 1` pair in lab sections 1 and 2 (the lab prints `addr_eq` and `op_eq`).
> 2. Under GCC with `libstdc++` on Linux (as of 2026-11), the output is:
>    `0 1`
>    Explanation: `h_ti == p_ti` evaluates to `0` because the physical object addresses are distinct.
>    However, `*h_ti == *p_ti` evaluates to `1` because `libstdc++` defines `__GXX_MERGED_TYPEINFO_NAMES` as `0`, causing `operator==` to fall back to `strcmp` on the mangled name strings.
>    Since both strings contain `"11CustomError"`, `strcmp` returns 0, and the operator returns `true`.

### Question 2: Vague Linkage and COMDAT Demotion
What is vague linkage in the Itanium C++ ABI, and how does `-fvisibility=hidden` alter the behavior of the static linker when assembling shared libraries?

> [!success]- Answer
> Vague linkage refers to language constructs whose definitions must be emitted in every translation unit that references them, yet must resolve to a single canonical definition at runtime.
> Examples include inline functions, template instantiations, virtual tables (`_ZTV`), and RTTI descriptors (`_ZTI` and `_ZTS`) for classes lacking an out-of-line key function.
> In ELF object files, the compiler places vague linkage symbols into COMDAT section groups with weak linkage (`STB_WEAK`) and default visibility (`STV_DEFAULT`).
> The static linker unifies duplicate COMDAT sections across object files during static linking and places the surviving symbol in `.dynsym` so the dynamic linker can interpose and deduplicate it across DSOs.
> When `-fvisibility=hidden` is specified, the compiler overrides the default visibility of these weak symbols, setting their visibility to `STV_HIDDEN`.
> The static linker consequently treats them as private to the DSO, demotes their binding to `STB_LOCAL`, and strips them from the dynamic symbol table (`.dynsym`), making cross-DSO deduplication impossible.

### Question 3: Debug This (dynamic_cast Returns nullptr)
You are maintaining a high-performance trading gateway that loads risk plugins dynamically.
A developer reports that after migrating from GCC to Clang `libc++`, the following code fails:
```cpp
BaseRiskEngine* raw = plugin_factory();
AdvancedRiskEngine* adv = dynamic_cast<AdvancedRiskEngine*>(raw);
assert(adv != nullptr); // Crashes here!
```
Inspection in GDB reveals that `raw` points to an instance of `ConcreteAdvancedRiskEngine` which publicly inherits from `AdvancedRiskEngine`.
GDB also shows that `typeid(*raw).name()` prints `30ConcreteAdvancedRiskEngine`.
Diagnose the failure mechanism and explain two ways to resolve it without modifying the inheritance hierarchy.

> [!success]- Answer
> Mechanism:
> `dynamic_cast` across polymorphic classes uses the Itanium RTTI engine (`__dynamic_cast` in `libcxxabi`).
> To verify that `raw` can be cast to `AdvancedRiskEngine`, `__dynamic_cast` compares the target `type_info` descriptor (`&_ZTI18AdvancedRiskEngine`) against the subobject base class descriptors in the object's complete vtable hierarchy.
> In `libc++abi`, this comparison is `is_equal(..., false)`, which is `std::type_info::operator==`, so on Linux it checks name address equality (`private_typeinfo.cpp`).
> This was run (as of 2026-11) with a hidden-visibility `Base`/`Adv` pair loaded from a plugin: `dynamic_cast<Adv*>` returned null with libc++ 18 on Linux aarch64 and non-null with g++ 13.3, so the symptom is specific to a libc++ build on Linux.
> macOS arm64 with Apple clang 21 returned non-null for the same pair; that platform difference is a measurement here, not an explained mechanism.
> The `_LIBCXXABI_FORGIVING_DYNAMIC_CAST` option (`LIBCXXABI_ENABLE_FORGIVING_DYNAMIC_CAST`, default OFF in upstream `libcxxabi`) is what makes `dynamic_cast` fall back to `strcmp`, and it never applies to `catch`.
> Because `AdvancedRiskEngine` was compiled with `-fvisibility=hidden` and lacked an out-of-line key function or explicit default visibility attribute, the host executable and the plugin DSO each generated private copies of `_ZTI18AdvancedRiskEngine`.
> The plugin's object references the plugin's `_ZTI` descriptor, while the host's `dynamic_cast` call site passes the host's `_ZTI` descriptor.
> The addresses do not match, base class traversal fails, and `dynamic_cast` returns `nullptr`.
>
> Remedies:
> 1. Export the interface class: Annotate `struct __attribute__((visibility("default"))) AdvancedRiskEngine` in its header, and link the host binary with `-Wl,--export-dynamic` (`-rdynamic`).
> 2. Anchor with a key function in a common library: Move `virtual ~AdvancedRiskEngine();` out of line, define it in a separate `libinterfaces.so` linked by both the host and the plugin, ensuring a single dynamic RTTI instance across the entire address space.

### Question 4: dlopen Flags and Scope Isolation
Explain the operational difference between `RTLD_LOCAL` and `RTLD_GLOBAL` in `dlopen(3)`.
If an engineer attempts to solve a cross-DSO RTTI failure simply by switching from `RTLD_LOCAL` to `RTLD_GLOBAL`, why will the catch block still fail if the host executable was linked without `--export-dynamic`?

> [!success]- Answer
> In `dlopen(3)`:
> - `RTLD_LOCAL` (the default) adds the loaded DSO's symbols only to a local search scope associated with that library handle.
>   These symbols are not made available to resolve references in subsequently loaded shared objects (`dlopen(3)`).
> - `RTLD_GLOBAL` adds the loaded DSO's exported symbols to the global search scope, making them available for symbol resolution of subsequently loaded shared objects.
>
> Why switching to `RTLD_GLOBAL` fails without `--export-dynamic`:
> In ELF linking, the main executable is loaded first.
> By default, the static linker omits non-exported symbols from the executable's dynamic symbol table (`.dynsym`).
> If the host executable was compiled with `-fvisibility=hidden` and linked without `-Wl,--export-dynamic` (`-rdynamic`), the host's `_ZTI11PluginError` symbol is strictly local to the executable.
> At link time, the host's LSDA catch table relocations resolve directly to this internal, non-exported static address.
> Even if the plugin is subsequently loaded with `RTLD_GLOBAL` and exports its own `_ZTI11PluginError` into the global scope, the host's catch site does not re-bind its static address.
> The plugin throws with the global DSO instance, while the host catches expecting its internal private instance.
> Because the two pointers remain unequal, the catch block still fails.
> Lab sections 1 and 4 (`libc++` 18 on Linux aarch64, as of 2026-11): with `RTLD_GLOBAL` and no `--export-dynamic`, both the hidden build and the annotated build abort with exit status 134.

### Question 5: Key Functions and RTTI Placement
What is a "key function" according to Section 5.2.3 of the Itanium C++ ABI specification, and how does it optimize binary size and guarantee type uniqueness across shared libraries?

> [!success]- Answer
> In the Itanium C++ ABI (Section 5.2.3), the "key function" for a class is the first non-pure virtual function that is not inline at the point of class definition; Section 5.2.4 places the `type_info` object with the virtual tables when those are emitted uniquely.
> 
> Optimization and placement mechanics:
> - If a class has a key function, the compiler emits the class's virtual table (`_ZTV`), typeinfo object (`_ZTI`), and typeinfo name string (`_ZTS`) **only** in the translation unit where that key function is defined.
> - In all other translation units that include the header, the compiler emits only external declarations (`extern`) and relocations referencing those symbols, omitting the bodies.
> - If a class has no key function (e.g., all virtual methods are inline or pure virtual), the compiler is forced to emit the vtable and RTTI in every translation unit as weak symbols in COMDAT groups.
>
> Impact across shared libraries:
> Defining an out-of-line key function (commonly the virtual destructor `virtual ~MyClass();`) in a single shared library guarantees that the vtable and RTTI symbols are emitted exactly once in that library's text and data sections.
> All consumer DSOs emit dynamic relocations to that single location, completely preventing vague linkage code bloat and eliminating duplicate RTTI instances across DSOs.

### Question 6: Two-Phase Exception Unwinding Mechanics
Walk through the two-phase exception handling process defined by the Itanium C++ ABI (`abi-eh.html`).
During which phase does the personality routine check type equivalence, and what data structures are evaluated?

> [!success]- Answer
> When `throw` executes, `__cxa_throw` delegates to `_Unwind_RaiseException`, which executes a two-phase unwind:
>
> 1. **Phase 1: Search Phase (`_UA_SEARCH_PHASE`):**
>    The unwinder walks the call stack frame by frame from the throw site upward to the top of the stack.
>    For each frame, it locates the Call Frame Information (CFI) in `.eh_frame` to identify the instruction pointer and registers.
>    It identifies the frame's personality routine (`__gxx_personality_v0`) and calls it with the `_UA_SEARCH_PHASE` flag.
>    The personality routine reads the function's Language-Specific Data Area (LSDA):
>    - It parses the Call Site Table to check if the frame's current IP (the call's return address) falls within a protected `try` range.
>    - If protected, it traverses the Action Table and Type Table (`TType`).
>    - For each catch filter, it calls `can_catch(catch_type, thrown_type)`.
>    - If `can_catch` returns true, Phase 1 terminates, returning `_URC_HANDLER_FOUND` along with the stack pointer of the handling frame.
>    - If the unwinder reaches the top of the stack without finding a handler, `_Unwind_RaiseException` returns `_URC_END_OF_STACK`, and `__cxa_throw` calls `std::terminate()` (in `libc++abi`, through `failed_throw`).
> 2. **Phase 2: Cleanup Phase (`_UA_CLEANUP_PHASE`):**
>    The unwinder walks the stack a second time from the throw site up to the handling frame.
>    For each intermediate frame, it calls the personality routine with `_UA_CLEANUP_PHASE`.
>    The personality routine installs the context for local destructors (landing pads) and executes cleanups.
>    Finally, the unwinder restores registers to the handling frame and transfers control to the matching catch block's landing pad.
>
> Type equivalence is strictly evaluated during **Phase 1 (Search Phase)**.
> If type equivalence fails during Phase 1, Phase 2 never executes, meaning destructors in intermediate frames are not called before `std::terminate()` aborts the process.

### Question 7: Linker Symbol Visibility Promotion Rules
Why can an ELF static linker script or `--dynamic-list` not promote a symbol marked `STV_HIDDEN` in an object file to `STV_DEFAULT` in the final binary?
Contrast this with symbol demotion.

> [!success]- Answer
> Under the ELF specification and System V ABI rules, symbol visibility settings in relocatable object files (`.o`) represent non-negotiable compiler contracts regarding symbol scope and optimization assumptions:
> 
> 1. **Compiler Optimization Assumptions:**
>    When the compiler marks a symbol `STV_HIDDEN` (via `-fvisibility=hidden` or `__attribute__((visibility("hidden")))`), it generates code assuming the symbol will never be interposed or accessed outside the current link unit.
>    On x86-64 and AArch64, this allows the compiler to generate direct PC-relative addressing (`RIP`-relative `mov` or `ADRP`/`ADD`) bypassing the Global Offset Table (GOT) and Procedure Linkage Table (PLT).
> 2. **Linker Promotion Prohibition:**
>    If the static linker were permitted to promote an `STV_HIDDEN` symbol to `STV_DEFAULT`, external DSOs attempting to call or relocate against that symbol would expect standard GOT/PLT indirection and interposition semantics.
>    Because the internal code was compiled without dynamic relocation support, promoting the symbol would cause relocation truncation, text relocations, or silent runtime interposition bugs.
>    Therefore, GNU `ld` and `lld` treat hidden visibility in input objects as an upper bound: a linker script can demote a `DEFAULT` symbol to `HIDDEN` or `LOCAL`, but can never promote a `HIDDEN` symbol to `DEFAULT`.
>    The gABI states the rule directly: when symbol references and definitions are combined, the most constraining visibility must be propagated, and a hidden symbol must be removed or made `STB_LOCAL` in the output.
>    With GNU ld 2.42 (as of 2026-11), a version script listing `_ZTI11PluginError` under `global:` left the hidden symbol out of `.dynsym`; GNU ld 2.42 `--dynamic-list` and lld 18.1.3 (`--version-script` and `--dynamic-list`) also left the hidden symbols out of `.dynsym` (run on Linux aarch64, as of 2026-11).
>    The symbol must be declared with default visibility at compile time in source code.

### Question 8: Architecture at Scale (Hot-Reloading Plugin Gateway)
You are the principal architect for an ultra-low-latency electronic trading gateway that hot-reloads proprietary pricing models as dynamic shared libraries via `dlopen(..., RTLD_LOCAL)`.
Both the execution core and the plugins are written in modern C++ and compiled with Clang `libc++`.
How do you architect the boundary between the engine harness and dynamic plugins to guarantee:
1. Zero cross-DSO RTTI/exception personality matching failures.
2. Complete absence of heap allocator corruption across DSO lifecycles.
3. Zero latency jitter from dynamic symbol interposition.

> [!success]- Answer
> 1. **Cross-DSO Exception and RTTI Isolation:**
>    - Prohibit C++ exceptions from crossing the DSO boundary entirely.
>      Enforce a strict C-ABI or `noexcept` C++ interface returning explicit status codes or error structures: `Result<T, ErrorCode>`.
>    - Any exception thrown within a plugin must be caught at the plugin's outer API boundary (`catch (...)`) and serialized into a structured error record before returning control to the host engine.
>    - If polymorphic interfaces must cross the boundary, house the interface class definitions and RTTI descriptors in an independent, immutable shared library (`libengine_api.so`) loaded once at startup.
>      Every interface class must define an out-of-line virtual destructor inside `libengine_api.so` to serve as the key function, guaranteeing unified typeinfo pointers.
> 2. **Heap Allocator Decoupling:**
>    - Cross-DSO memory allocation is a primary source of memory corruption if the host and plugin link separate runtime allocators or mismatched compiler runtimes.
>    - Enforce caller-allocates / callee-fills semantics, or provide an explicit memory arena / allocator pointer passed into the plugin initialization routine (`IAllocator*`).
>    - All allocations returned to the engine must be freed by the same allocator instance that allocated them.
>    - Use opaque handles or plain-old-data (POD) buffers across the boundary rather than standard library containers (`std::string`, `std::vector`), which have ABI-sensitive layouts.
> 3. **Elimination of Latency Jitter from Dynamic Interposition:**
>    - Compile all plugins with `-fvisibility=hidden` and link with `-Wl,-Bsymbolic-functions` and `-Wl,-z,relro,-z,now` (Full RELRO needs both `relro` and `now`).
>    - `-Bsymbolic-functions` binds references to global function symbols to their definitions inside the shared library at link time, so calls to the plugin's own exported functions skip the PLT; hidden functions already bind locally.
>    - Linking with `-z now` tells the dynamic linker to resolve all symbols when the library is loaded by `dlopen` instead of at first call, preventing lazy-binding latency spikes on hot pricing paths during execution.
