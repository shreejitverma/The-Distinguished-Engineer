---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "Gang Ren, Eric Tune, Tipp Moseley, Yixin Shi, Silvius Rus, and Robert Hundt, Google-Wide Profiling: A Continuous Profiling Infrastructure for Data Centers, IEEE Micro 30(4), pp. 65-79, 2010, https://research.google.com/pubs/archive/36575.pdf"
  - "OpenTelemetry Authors, profiles.proto (package opentelemetry.proto.profiles.v1development), opentelemetry-proto main branch, latest release v1.11.1, 2026, https://github.com/open-telemetry/opentelemetry-proto/blob/main/opentelemetry/proto/profiles/v1development/profiles.proto"
  - "OpenTelemetry Authors, OpenTelemetry Profiles Specification (Status: Alpha), opentelemetry-specification main branch, 2026, https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/profiles/README.md"
  - "OpenTelemetry Authors, OpenTelemetry Profiles Enters Public Alpha, OpenTelemetry Blog, 2026-03-26, https://opentelemetry.io/blog/2026/profiles-alpha/"
  - "OpenTelemetry Authors, Semantic Conventions for Profiles (Status: Development), OpenTelemetry Semantic Conventions, 2026, https://github.com/open-telemetry/semantic-conventions/blob/main/docs/general/profiles.md"
  - "OpenTelemetry Authors, OpenTelemetry eBPF Profiler README, opentelemetry-ebpf-profiler main branch (latest tag v0.0.202645), 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/README.md"
  - "OpenTelemetry Authors, Profiling internals (doc/internals.md), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/doc/internals.md"
  - "OpenTelemetry Authors, support/ebpf/types.h (UnwindInfo, StackDelta, StackDeltaPageInfo, ThreadContextBuf, GolangLabel), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/support/ebpf/types.h"
  - "OpenTelemetry Authors, support/ebpf/native_stack_trace.h (bsearch_step, get_stack_delta, unwind_one_frame), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/support/ebpf/native_stack_trace.h"
  - "OpenTelemetry Authors, support/ebpf/native_stack_trace.ebpf.c (stack delta maps, perf_event entry point), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/support/ebpf/native_stack_trace.ebpf.c"
  - "OpenTelemetry Authors, support/ebpf/tracemgmt.h (prepare_trace, unwind_trace, tail_call wrapper), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/support/ebpf/tracemgmt.h"
  - "OpenTelemetry Authors, support/ebpf/interpreter_dispatcher.ebpf.c (unwind_stop, maybe_add_thread_context_info, traces_ctx_v1), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/support/ebpf/interpreter_dispatcher.ebpf.c"
  - "OpenTelemetry Authors, support/ebpf/off_cpu.ebpf.c and probes/offcpu/offcpu.go (off-CPU probe and its Collector extension), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/probes/offcpu/offcpu.go"
  - "OpenTelemetry Authors, support/ebpf/heap_usdt.ebpf.c and probes/heap/heap.go (USDT heap allocation probe), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/probes/heap/heap.go"
  - "OpenTelemetry Authors, tracer/systemconfig.go (setOriginIDs) and tracer/tracer.go (perf event setup), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/tracer/systemconfig.go"
  - "OpenTelemetry Authors, interpreter/threadcontext/threadcontext.go (otel_thread_ctx_v1 discovery), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/interpreter/threadcontext/threadcontext.go"
  - "OpenTelemetry Authors, nativeunwind/elfunwindinfo/elfehframe.go (CFI parsing and recognized DWARF expressions), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/nativeunwind/elfunwindinfo/elfehframe.go"
  - "OpenTelemetry Authors, collector/factory_linux.go and collector/config/config_linux.go (receiver defaults and validation), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/collector/factory_linux.go"
  - "OpenTelemetry Authors, reporter/internal/pdata/generate.go and reporter/samples/samples.go (link_table, SampleKey, TypeMetadata), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/reporter/internal/pdata/generate.go"
  - "OpenTelemetry Authors, OTEP 4947 Thread Context: Sharing Thread-Level Information with External Readers, opentelemetry-specification main branch, 2026, https://github.com/open-telemetry/opentelemetry-specification/blob/main/oteps/profiles/4947-thread-ctx.md"
  - "OpenTelemetry Authors, Correlating Profiles to OBI Traces (devdocs/trace-profile-correlation.md), opentelemetry-ebpf-instrumentation main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-instrumentation/blob/main/devdocs/trace-profile-correlation.md"
  - "Parca Developers, Parca Agent README and flag reference, parca-agent main branch (v0.51.5), 2026, https://github.com/parca-dev/parca-agent/blob/main/README.md"
  - "Parca Developers, Parca Agent v0.32.0 release notes, GitHub, 2024-08-13, https://github.com/parca-dev/parca-agent/releases/tag/v0.32.0"
  - "Parca Developers, Parca Agent Design, Parca Documentation, 2026, https://www.parca.dev/docs/parca-agent-design"
  - "Parca Developers, Parca Storage and Symbolization, Parca Documentation, 2026, https://www.parca.dev/docs/storage"
  - "Polar Signals, DWARF-based Stack Walking Using eBPF, Polar Signals Blog, 2022-11-29, https://www.polarsignals.com/blog/posts/2022/11/29/dwarf-based-stack-walking-using-ebpf"
  - "Grafana Labs, About the Pyroscope v2 architecture, Grafana Pyroscope Documentation, 2026, https://grafana.com/docs/pyroscope/latest/reference-pyroscope-v2-architecture/about-pyroscope-v2-architecture/"
  - "Grafana Labs, About the Pyroscope v1 architecture, Grafana Pyroscope Documentation, 2026, https://grafana.com/docs/pyroscope/latest/reference-pyroscope-architecture/about-grafana-pyroscope-architecture/"
  - "Grafana Labs, Version 2.0 release notes, Grafana Pyroscope Documentation, 2026, https://grafana.com/docs/pyroscope/latest/release-notes/v2-0/"
  - "Grafana Labs, Link tracing and profiling with span profiles, Grafana Pyroscope Documentation, 2026, https://grafana.com/docs/pyroscope/latest/configure-client/trace-span-profiles/"
  - "Grafana Labs, Span profiles with Traces to profiles for Go and for Python, Grafana Pyroscope Documentation, 2026, https://grafana.com/docs/pyroscope/latest/configure-client/trace-span-profiles/go-span-profiles/"
  - "Grafana Labs, OpenTelemetry eBPF profiler with Pyroscope, Grafana Pyroscope Documentation, 2026, https://grafana.com/docs/pyroscope/latest/configure-client/opentelemetry/ebpf-profiler/"
  - "Grafana Labs, pyroscope.ebpf component reference, Grafana Alloy Documentation, 2026, https://grafana.com/docs/alloy/latest/reference/components/pyroscope/pyroscope.ebpf/"
  - "Grafana Labs, otel-profiling-go README and otelpyroscope.go, grafana/otel-profiling-go main branch, 2026, https://github.com/grafana/otel-profiling-go"
  - "The Go Authors, Package runtime/pprof, Go Standard Library Documentation, 2026, https://pkg.go.dev/runtime/pprof"
  - "Google, profile.proto and proto/README.md, google/pprof main branch, 2026, https://github.com/google/pprof/blob/main/proto/profile.proto"
  - "Linux Kernel Developers, include/linux/bpf.h, include/uapi/linux/bpf.h, include/uapi/linux/bpf_common.h, and include/uapi/linux/perf_event.h, Linux v6.12, 2024, https://github.com/torvalds/linux/blob/v6.12/include/linux/bpf.h"
  - "Linux Kernel Developers, kernel/unwind/deferred.c and kernel/unwind/user.c, Linux v6.17 through v7.4-rc2, 2026, https://github.com/torvalds/linux/tree/master/kernel/unwind"
  - "Jens Remus, [PATCH v15 00/20] unwind_deferred: Implement sframe handling, Linux Kernel Mailing List, 2026-05-20, https://lkml.iu.edu/2605.2/08780.html"
  - "Linux man-pages project, perf_event_open(2), man-pages 6.19, 2026, https://man7.org/linux/man-pages/man2/perf_event_open.2.html"
  - "DWARF Debugging Information Format Committee, DWARF Debugging Information Format Version 5, Section 6.4 Call Frame Information, 2017, https://dwarfstd.org/doc/DWARF5.pdf"
  - "Fedora Project, Changes/fno-omit-frame-pointer, Fedora Project Wiki, 2022, https://fedoraproject.org/wiki/Changes/fno-omit-frame-pointer"
  - "Canonical, Performance engineering on Ubuntu leaps forward with frame pointers by default in Ubuntu 24.04 LTS, Ubuntu Blog, 2023, https://ubuntu.com/blog/ubuntu-performance-engineering-with-frame-pointers-by-default"
  - "The Rust Project Developers, Codegen Options: force-frame-pointers, The rustc Book, 2026, https://doc.rust-lang.org/rustc/codegen-options/index.html"
  - "OpenTelemetry Authors, cmd/otelcol-ebpf-profiler/local.example.yaml (privileges and hostPID comments), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/cmd/otelcol-ebpf-profiler/local.example.yaml"
  - "OpenTelemetry Authors, probes/offcpu/metadata.yaml and probes/heap/metadata.yaml (stability: development), opentelemetry-ebpf-profiler main branch, 2026, https://github.com/open-telemetry/opentelemetry-ebpf-profiler/blob/main/probes/offcpu/metadata.yaml"
  - "OpenTelemetry Authors, opentelemetry-ebpf-profiler tags (v0.0.202645), GitHub API, 2026-11, https://api.github.com/repos/open-telemetry/opentelemetry-ebpf-profiler/tags"
  - "GitHub releases API for parca-dev/parca (v0.30.0), parca-dev/parca-agent (v0.51.5, v0.32.0), grafana/pyroscope (v2.4.3, v2.0.0), and open-telemetry/opentelemetry-proto (v1.11.1), 2026-11, https://api.github.com/repos/grafana/pyroscope/releases/latest"
  - "Linux Kernel Developers, torvalds/linux tags (v7.4-rc2) and kernel/unwind contents, GitHub API, 2026-11, https://api.github.com/repos/torvalds/linux/contents/kernel/unwind"
  - "The Go Authors, src/runtime/pprof/label.go (labels used only by CPU and goroutine profiles), golang/go master branch, 2026, https://github.com/golang/go/blob/master/src/runtime/pprof/label.go"
---

# Continuous Profiling: GWP, Parca, Pyroscope, and OTel Profiles

> [!summary] Claim:
> A continuous profiler samples every process on every host at a low fixed rate (19 or 20 Hz per logical CPU by default in Parca Agent and the OpenTelemetry eBPF profiler), ships raw stacks off the host keyed by a binary identity, and symbolizes and aggregates them centrally so that any time range, service, or version can be queried after the fact.
> Google-Wide Profiling (GWP) set the template in 2010: sample in two dimensions (a subset of machines, and events within each machine), keep collection separate from symbolization, and load the samples into a dimensional database.
> Current eBPF agents walk stacks inside the kernel: user space flattens each binary's `.eh_frame` call frame information into small sorted tables in BPF maps, and a BPF program attached to a perf event binary-searches them once per frame, so the host needs no frame pointers, no debug symbols, and no stack copies.
> pprof is the common sample format, and OTLP Profiles (Alpha since 2026-03) extends it with shared dictionaries, resource context, and a per-sample `Link` holding a `trace_id` and `span_id`, which is the field that joins one profile sample, on-CPU or off-CPU, to one distributed trace span.

```mermaid
flowchart LR
    subgraph HOST["Every host (agent as a privileged daemon)"]
        PE["perf event per CPU<br/>cpu-clock at 19 or 20 Hz"]
        OC["sched_switch tracepoint and<br/>finish_task_switch kprobe<br/>(offcpu probe, sampled)"]
        HP["USDT otel_memory:alloc<br/>(heap probe, opt-in)"]
        BPF["BPF unwinder programs<br/>native, JVM, Python, V8, ...<br/>chained with tail calls<br/>ending in unwind_stop"]
        MAPS["BPF maps<br/>stack deltas per executable<br/>unwind info array"]
        UL["Agent user space<br/>parse .eh_frame and .gopclntab<br/>dedupe traces, attach metadata"]
        OBI["Pinned map traces_ctx_v1<br/>written by OBI"]
        PE --> BPF
        OC --> BPF
        HP --> BPF
        MAPS -.->|"lookup per frame"| BPF
        UL -->|"load tables"| MAPS
        OBI -.->|"pid_tgid lookup"| BPF
        BPF -->|"raw traces"| UL
    end

    subgraph APP["Instrumented process (optional)"]
        CTX["Go pprof labels or<br/>otel_thread_ctx_v1 TLS record<br/>trace_id and span_id"]
    end
    CTX -.->|"read in unwind_stop"| BPF

    subgraph BACKEND["Profiling backend"]
        ING["Ingest<br/>OTLP Profiles or pprof"]
        OBJ["Object storage<br/>columnar blocks"]
        SYM["Symbolizer<br/>debug info by build ID"]
        Q["Query engine<br/>label selectors, merge, diff"]
        ING --> OBJ --> Q
        SYM --> Q
    end

    UL -->|"samples: file ID + offset,<br/>Link(trace_id, span_id)"| ING
    UL -->|"debug info upload"| SYM
    CI["CI build<br/>unstripped binary"] -->|"debug info by build ID"| SYM
    Q --> UI["Flame graph UI"]
    TR["Trace backend"] -->|"span_id"| UI
```

## The Mechanism

A one-off profile answers a question about one process on one host during one window that somebody chose in advance.
A continuous profiler removes the choosing: it records a thin, unbiased sample of everything, always, and moves the choice of process, host, version, and window to query time.
That only works when the per-host cost is small enough to leave on, which drives every design decision in this note.
[[Linux-perf-in-Depth-and-perf-c2c]] owns `perf_event_open`, sampling skid, and the `perf` tool itself, and [[eBPF-bpftrace-ftrace-uprobes-and-USDT]] owns the BPF verifier, maps, and probe types; this note uses both and re-derives neither.
Unless a section says otherwise, source references are to the `main` branch of `open-telemetry/opentelemetry-ebpf-profiler` read on 2026-11-15, whose latest tag was `v0.0.202645`.

### 1. The GWP template

Ren et al. describe GWP in IEEE Micro (July/August 2010) as a continuous profiling infrastructure that "samples across machines in multiple data centers and collects various events - such as stack traces, hardware events, lock contention profiles, heap profiles, and kernel events".
The paper presents it as an extension of the Digital Continuous Profiling Infrastructure (DCPI) to data centers.
Four ideas from that paper are still the architecture of every system in this note.

- **Two-dimensional sampling.**
  The paper states "GWP samples in two dimensions": at any moment only a small subset of machines is being profiled, and on each of those machines event-based sampling is used.
  Sampling in one dimension alone fails either way: profiling every machine at a normal event rate costs too much fleet-wide, and a very low event rate makes per-machine data too sparse to drill into.
- **A central collector that pulls.**
  The collector periodically reads the list of all machines from a machine database, selects a random sample, remotely activates profiling on each, and retrieves the results, taking "a few minutes to gather profiles for a specific machine".
  It monitors error conditions and stops profiling when the failure rate reaches a threshold.
- **Collection separated from symbolization.**
  Binaries are deployed stripped to save network bandwidth and disk, so each profile carries "a unique identifier for each running binary", and GWP "stores unstripped binaries in a global repository".
  The Google File System (GFS) archives the profiles and the binaries.
  Symbolizing one day of profiles sequentially "would take weeks", so it is distributed "across a few hundred machines using MapReduce".
- **A dimensional store.**
  GWP loads the samples into a read-only dimensional database distributed across hundreds of machines, holding several terabytes of history at the time of the paper.
  A record is the tuple `<event, sample counter, m-dimension vector>`, where the vector holds application name, function name, platform, compiler version, image name, data center, kernel information, build revision, and builder's name.
  A query picks `k` keys to group by and filters on the other `m - k`.

GWP collected two categories of profile.
Whole-machine profiles came from OProfile driving hardware performance monitoring (HPM) events, without full call stacks, to avoid unwinding cost.
Per-process profiles came from the Google Performance Tools library linked into most applications, which exposes heap, lock contention, wall time, and CPU profilers behind a small HTTP server that the collector calls.

The overhead control is explicit in the paper: measure event-profiling overhead on benchmark applications, then "conservatively set the maximum rates to ensure the overhead is always less than a few percent" on a profiled machine.
Because only a subset of machines is profiled at any time, "the aggregated profiling overhead is negligible - less than 0.01 percent".

The paper also shows that the sampled data is trustworthy.
It measures a profile's stability with entropy and compares two profiles with a Manhattan distance over their top `k` entries:

$$H(W) = -\sum_{i=1}^{n} p(x_i)\,\log p(x_i) \qquad M(X, Y) = \sum_{i=1}^{k} \left| p_x(x_i) - p_y(x_i) \right|$$

Comparing a random subset of machines against the whole set, the distance falls with the number of samples roughly as

$$M(X) = \frac{C}{\sqrt{N(X)}}$$

where `N(X)` is the sample count and `C` depends on the profile type.
That square-root law is what lets a fleet operator trade sampling rate against precision with arithmetic instead of guesswork.

#### What GWP's profiles were used for

The paper's section on uses is the best argument for why fleet-wide data is worth its cost, and each use maps to a capability a current system must keep.

- **Hottest shared code.**
  A routine that is cold in every single application can still dominate the fleet.
  GWP found that zlib "accounted for nearly 5 percent of all CPU cycles consumed", which motivated optimizing its routines and evaluating compression alternatives.
  The paper reports that users turned such shares into estimated savings, and "dollar amount per performance change" became a popular informal metric.
- **Comparing two queries.**
  GWP diffs the profiles of two queries that share keys and events but differ in other dimensions, usually at function level.
  A large change in hot functions between daily profiles triggers a finer comparison that can point at a source revision, a compiler, or a data center.
  This is the ancestor of the diff view in Parca and Pyroscope.
- **Auditing the fleet.**
  Grouping by compiler, platform, or builder shows how fast a new compiler or hardware platform actually reaches production and how fast old platforms retire.
- **Evaluating hardware.**
  Because GWP records what fraction of cycles a kind of work consumes, it was used to judge whether a floating-point coprocessor would pay off.
- **Application-platform affinity.**
  Whole-machine HPM profiles give cycles and retired instructions per job and platform, hence CPI (cycles per instruction).
  The paper's Table 1 has two applications and two platforms: NumCrunch has CPI 1 on both, and MemBench has CPI 1 on Platform 1 but 2 on Platform 2 because of its smaller L2 cache.
  Splitting 200 instructions of each evenly costs 100 + 100 cycles on Platform 1 and 100 + 200 on Platform 2, which is 500 cycles in total.
  Sending all MemBench work to Platform 1 and all NumCrunch work to Platform 2 costs 200 cycles on each platform, which is 400.
  The paper formulates the general case as a linear program over per-application, per-platform load, solved with simulated annealing, and reports 10 to 15 percent potential improvement in most cases where many jobs run on multiple platforms.
  The same CPI data tells operators where a microarchitectural peculiarity (such as a smaller L2 cache) hurts one program, which is the fleet-scale version of the single-host triage in [[Intel-VTune-AMD-uProf-and-Hardware-Tracing-PT-SPE]].
- **Feedback-directed optimization.**
  GWP can extract a profile for one binary in a format the compiler understands.
  Such a profile comes from live data, and an aged profile still helps a fresh release when the critical sections did not change; [[Cpp-PGO-AutoFDO-and-BOLT-Workflow]] owns the loop.
- **Application-specific profiling.**
  The default machine sampling did not collect enough samples for applications that use few cycles fleet-wide, so GWP added application-specific profiling that targets the machines running one application.

Hardware-event sampling (cycles, instructions, cache misses) is what the 2026 eBPF agents give up by default.
They sample the software `cpu-clock` event (`perf.CPUClock.Configure(perfAttribute)` in `tracer/tracer.go`), so they answer "where is time spent" and not "why is this code slow per instruction".
Fleet CPI studies in the GWP style need PMU access, which virtual machines often lack; [[Intel-VTune-AMD-uProf-and-Hardware-Tracing-PT-SPE]] covers the counters and their limits.

### 2. Sampling on a host: a perf event with a BPF program attached

The open-source agents replace OProfile and in-process HTTP handlers with one mechanism.
The Parca Agent design document says the agent attaches "an eBPF program to a `perf_event`, specifically `PERF_COUNT_SW_CPU_CLOCK` event", and samples stack traces "19 times per second per logical CPU".
The `perf_event_open(2)` manual page (man-pages 6.19) describes `PERF_COUNT_SW_CPU_CLOCK` as "the CPU clock, a high-resolution per-CPU timer", and describes the `freq` bit as selecting `sample_freq` instead of `sample_period`.
The design document gives the reason for 19: "it is a prime number, and primes are good to avoid collisions with other things that may be happening periodically on a machine", and it singles out 100 Hz, every 10 ms, as a period other activity is likely to share.

The OpenTelemetry eBPF profiler does the same thing.
Its entry program is declared `SEC("perf_event/native_tracer_entry")` in `native_stack_trace.ebpf.c`, and `tracer/tracer.go` builds the event with `perfAttribute.SetSampleFreq(uint64(t.samplesPerSecond))` and opens one per CPU with `perf.Open(perfAttribute, perf.AllThreads, id, nil)`.
The receiver default is `SamplesPerSecond: 20` (`collector/factory_linux.go`).
The tracer registers this "origin" in `setOriginIDs()` (`tracer/systemconfig.go`) with sample type `samples`/`count` and a period of type `cpu`, unit `nanoseconds`, value `1e9 / SamplesPerSecond`, which is 50,000,000 ns at the default 20 Hz.
Each CPU sample is one count: the exported value is the number of timestamps, and CPU time is that count times the period.

A low fixed rate bounds the worst case.
A host with 64 logical CPUs sampled at 19 Hz produces at most 64 x 19 = 1,216 samples per second, whatever the workload does.

Privileges are part of the mechanism, not an afterthought.
The example Collector configuration lists "CAP_SYS_ADMIN, CAP_PERFMON, and/or CAP_BPF, as well as access to /proc", and `hostPID: true` on Kubernetes.
Grafana Alloy's `pyroscope.ebpf` page spells out a least-privilege set for running without `privileged: true`: `BPF`, `PERFMON`, `SYS_PTRACE` (read `/proc/<pid>/`), `CHECKPOINT_RESTORE` (follow `/proc/<pid>/map_files/*` magic links for ELF reading, kernel 5.9+, else `SYS_ADMIN`), `SYS_RESOURCE` (raise `RLIMIT_MEMLOCK`), `DAC_READ_SEARCH`, and `SYSLOG` (verifier diagnostics), plus a read-only mount of `/sys/kernel/tracing`.

### 3. Unwinding without frame pointers: `.eh_frame` flattened into BPF maps

A sample is only useful with a call stack, and the kernel's own user-stack walker follows the frame-pointer chain.
`bpf_get_stackid()` with `BPF_F_USER_STACK` collects up to `PERF_MAX_STACK_DEPTH` frames (127 in Linux v6.12) that way.
Most distribution binaries were historically compiled without frame pointers, so that walk stops after one or two frames.

`perf record --call-graph dwarf` solves this by copying a slice of the user stack and the registers into every sample (`PERF_SAMPLE_STACK_USER` and `PERF_SAMPLE_REGS_USER`, since Linux 3.7) and unwinding later in user space.
That is too expensive to leave on, and it moves other processes' stack contents off the host; [[Linux-perf-in-Depth-and-perf-c2c]] covers that mode.

#### What `.eh_frame` contains

DWARF 5 section 6.4 defines Call Frame Information (CFI) as an abstract table with one row per code location and the columns `LOC CFA R0 R1 ... RN`.
The Canonical Frame Address (CFA) is, in the standard's words, typically "the value of the stack pointer at the call site in the previous frame".
The CFA column holds a rule that is "either a register and a signed offset that are added together, or a DWARF expression that is evaluated", and each register column says how to recover that register's value in the caller.
The table is stored compressed as a byte-coded state machine, and `.eh_frame` is the copy of it that stays in the loaded image because C++ exception unwinding needs it at run time.
The profiler's internals document notes that C, Zig, and Rust binaries typically carry `.eh_frame` too.

#### The idea: evaluate the state machine in user space, look up rows in the kernel

A BPF program cannot run an unbounded state machine or a general expression evaluator.
So the agent evaluates the CFI once per executable in user space and loads the resulting table into BPF maps, leaving the kernel side a bounded lookup.

Polar Signals described the first Parca Agent version of this in 2022.
User space produced rows of `pc` (u64), a reserved u16, `cfa_type` (u8), `rbp_type` (u8), `cfa_offset` (s16), and `rbp_offset` (s16), 16 bytes per row, sorted by `pc` so the BPF program could binary search.
The BPF side read the interrupted `rip`, `rsp`, and `rbp`, found the row covering the program counter, computed the CFA from `rsp` or `rbp`, and read the return address 8 bytes below the CFA on x86-64.
That version handled only `.eh_frame`, hard-coded two DWARF expressions, and covered x86-64.

Since Parca Agent v0.32.0 (2024-08-13) the agent is, in the release notes' words, "based on the opentelemetry-ebpf-profiler"; its `go.mod` replaces `go.opentelemetry.io/ebpf-profiler` with `github.com/parca-dev/opentelemetry-ebpf-profiler` (as of 2026-11, v0.51.5).
Grafana Alloy's `pyroscope.ebpf` component "embeds the `grafana/opentelemetry-ebpf-profiler` which is a fork of `open-telemetry/opentelemetry-ebpf-profiler`".
So Parca, Pyroscope's eBPF collection, and the OpenTelemetry Collector distribution all run forks of one unwinder; the forks were not diffed against upstream for this note.

#### The data structures (`support/ebpf/types.h`)

The profiler calls a flattened CFI row a stack delta.

```c
typedef struct UnwindInfo {
  u8 flags;       // flags: UNWIND_FLAG_*
  u8 baseReg;     // base register to calculate CFA from
  u8 auxBaseReg;  // base register to calculate FP (x86-64) or RA[+FP] (aarch64)
  u8 mergeOpcode; // opcode for generating next stack delta
  s32 param;      // parameter for the CFA expression
  s32 auxParam;   // parameter for the FP expression
} UnwindInfo;

typedef struct StackDelta {
  u16 addrLow;    // the low 16-bits of the ELF virtual address to which this stack delta applies
  u16 unwindInfo; // index of UnwindInfo, or UNWIND_COMMAND_* if STACK_DELTA_COMMAND_FLAG is set
} StackDelta;
```

- A `StackDelta` is 4 bytes: the low 16 bits of the address where a rule starts, and a 16-bit index.
- An `UnwindInfo` is 12 bytes (four `u8` and two `s32` fields, summed from the declaration) and says "CFA = `baseReg` + `param`" and "saved frame pointer is at `auxBaseReg` + `auxParam`".
  On aarch64 the auxiliary rule recovers the return address (and optionally the frame pointer), because the return address lives in a register at call time instead of on the stack.
- `mergeOpcode` packs a second, adjacent rule into the same entry: per the header comment, 1 bit says whether the next rule's `param` is 8 lower or 8 higher, and 7 bits give the distance to the next `addrLow`.
  That covers the common push-then-call or call-then-pop pattern where the CFA offset moves by one 8-byte slot.
- Unwind rules repeat heavily across functions, so `UnwindInfo` values are deduplicated into one global array, `unwind_info_array`, with `UNWIND_INFO_MAX_ENTRIES` set to 16384; the source comment says a normal desktop's `/usr/bin` and `/usr/lib` hold "about 9700 unique deltas", and that the limit "can be increased up to 2^15".
- The deltas of one executable live in a `BPF_MAP_TYPE_ARRAY` that is the inner map of a `BPF_MAP_TYPE_HASH_OF_MAPS` keyed by the executable's file ID.
  `STACK_DELTA_BUCKET(8)` through `STACK_DELTA_BUCKET(23)` in `native_stack_trace.ebpf.c` declare sixteen such outer maps, `exe_id_to_8_stack_deltas` through `exe_id_to_23_stack_deltas`, whose inner arrays hold 2^8 through 2^23 entries, so a small library does not reserve a large array.
- `stack_delta_page_to_info` is a hash map from `StackDeltaPageKey {u64 fileID; u64 page}` to `StackDeltaPageInfo {u32 firstDelta; u16 numDeltas; u16 mapID}`, where a page is 64 kB of address space (`STACK_DELTA_PAGE_BITS` is 16).

The page table is what makes the lookup verifiable.
Splitting the address space into 64 kB pages lets `addrLow` be 16 bits and caps the entries in one page at 65,535 (`numDeltas` is a `u16`), so a binary search inside one page needs at most 16 steps.

#### The per-frame algorithm (`native_stack_trace.h`)

`get_stack_delta()` and `unwind_one_frame()` do this for each native frame on x86-64:

1. Compute the offset of the program counter inside its executable, subtracting 1 when the PC is a return address so the lookup lands inside the call instruction's rule.
2. Look up `(fileID, offset & ~0xFFFF)` in `stack_delta_page_to_info`.
3. Pick the outer map named by `mapID`, and get the executable's inner array from it.
4. Binary search `[firstDelta, firstDelta + numDeltas)` for the first entry whose `addrLow` is greater than `offset & 0xFFFF`, in a `for (i = 0; i < 16; i++)` loop of `bsearch_step()` calls, then step back one entry.
   Stepping back from the first entry of a page lands on the last entry of the previous page, which is the rule still in force at the page boundary.
   A page with no deltas skips the search and steps back from `firstDelta`, so it also resolves to the previous page's last rule.
   If the loop has not converged after 16 steps, the real code does not guess: it returns `ERR_NATIVE_EXCEEDED_DELTA_LOOKUP_ITERATIONS`.
5. If the entry has `STACK_DELTA_COMMAND_FLAG` (0x8000) set, run the command: `UNWIND_COMMAND_STOP` (1) ends the trace at an entry point, `UNWIND_COMMAND_PLT` (2) applies the fixed procedure-linkage-table rule, `UNWIND_COMMAND_SIGNAL` (3) reads the saved registers out of the signal frame, `UNWIND_COMMAND_FRAME_POINTER` (4) walks one frame-pointer link, and two native-only Go commands (`UNWIND_COMMAND_GO_ASMCGOCALL`, `UNWIND_COMMAND_GO_MORESTACK`, tagged with `STACK_DELTA_NATIVE_COMMAND_BIT`) cross Go runtime stack switches.
   An entry equal to `STACK_DELTA_INVALID` (the flag with `UNWIND_COMMAND_INVALID`, 0) ends the walk with `ERR_NATIVE_STACK_DELTA_INVALID`.
6. Otherwise fetch `unwind_info_array[unwindInfo]`, compute `cfa = regs[baseReg] + param`, and read the saved frame pointer from `regs[auxBaseReg] + auxParam` when that rule is present.
7. Read the caller's PC with `bpf_probe_read_user(&state->pc, sizeof(state->pc), (void *)(cfa - 8))`, set `sp = cfa`, and mark the new frame as a return address.

Every step is a map lookup, an add, or one user-memory read, which is why the internals document can say the unwinder uses "O(log n) algorithms at worst".

The PLT command shows how one awkward DWARF expression is handled.
Toolchains describe a whole PLT with a single expression, and the profiler hard-codes its meaning:

```c
cfa = state->sp + 8UL + ((((state->pc & 15) >= 11) ? 1UL : 0UL) << 3);
```

`elfehframe.go` recognizes that expression by its opcode sequence, plus four register-dereference patterns the comments attribute to SSE-vectorized code and OpenSSL's libcrypto.
Any other expression is rejected by the parser with the error `DWARF expression unmatched`.

#### Program size, tail calls, and stack depth

The profiler supports kernels back to 5.10 and so codes against old verifier limits.
The classic per-program limit is `BPF_MAXINSNS` (4096); the modern verifier budget is `BPF_COMPLEXITY_LIMIT_INSNS` (1,000,000) in Linux v6.12.
The native unwinder therefore unwinds `NATIVE_FRAMES_PER_PROGRAM` (5) frames per program and then tail-calls itself, or the unwinder for whichever runtime owns the next PC.

Tail calls are also bounded: `MAX_TAIL_CALL_CNT` is 33 in Linux v6.12, and the profiler's internals document plans for "a maximum of 32 tail calls".
The profiler's `tail_call()` wrapper in `tracemgmt.h` checks `record->tailCalls >= 29`, records `ERR_MAX_TAIL_CALLS`, and jumps to `PROG_UNWIND_STOP`, so that a deep stack is reported truncated instead of being lost.
A pure native stack is therefore cut at roughly 5 x 29 = 145 frames (an estimate from those two constants), and sooner when interpreter unwinders use some of the tail calls.
The frame buffer is a separate, larger bound: `MAX_FRAME_DATA_SZ` is `sizeof(u64[3072])`, which the header says allows "about 1024 frames in a trace".
State that must survive a tail call lives in a per-CPU record (`PerCPURecord`), because BPF stack frames do not.

Every origin converges on the same exit.
`prepare_trace()` fills the trace header (origin ID, PID, TID, timestamp, `comm`), `unwind_trace()` optionally pushes kernel frames and tail-calls the first user-space unwinder, and every unwinder chain ends in `unwind_stop()` in `interpreter_dispatcher.ebpf.c`, which attaches trace context and labels and sends the trace to user space.
That shared exit is why CPU samples, off-CPU events, and heap events all carry the same kind of trace context.

#### Runtimes that are not described by `.eh_frame`

- **Go.**
  The internals document says Go executables "do not come with `.eh_frame` sections unless they are built with CGo enabled", so stack deltas are extracted from the Go-specific `.gopclntab` section instead.
- **Interpreters and JITs.**
  Each supported runtime has its own BPF unwinder program (`PROG_UNWIND_HOTSPOT`, `PROG_UNWIND_PERL`, `PROG_UNWIND_PYTHON`, `PROG_UNWIND_PHP`, `PROG_UNWIND_RUBY`, `PROG_UNWIND_V8`, `PROG_UNWIND_DOTNET`, `PROG_UNWIND_DOTNET10`, `PROG_UNWIND_BEAM`, `PROG_UNWIND_LUAJIT`) that reads the runtime's own frame structures out of process memory.
  User space detects the runtime version and writes structure offsets into maps, and the entry program dispatches on the memory region that contains the PC.
  One trace can therefore cross from kernel frames through native libraries into Python or JVM frames.

#### The alternatives: frame pointers and SFrame

The simpler fix is to keep frame pointers.
Fedora 38 added `-fno-omit-frame-pointer` and `-mno-omit-leaf-frame-pointer` to its default C and C++ flags, and its change page reports a kernel build 2.4% slower with GCC, a Blender render 2% slower, and CPython benchmarks 1-10% slower depending on the benchmark; packages opt out with `%undefine _include_frame_pointers`.
Ubuntu 24.04 LTS enables frame pointers in its GCC package by default for 64-bit platforms, with Canonical estimating the penalty at "between 1-2% in most cases".
A frame-pointer walk costs two reads per frame and no tables, but it only covers code that was built that way, which is why the agents keep the `.eh_frame` path.

The kernel is growing a third option.
Deferred user-space unwinding (`kernel/unwind/deferred.c`) is in mainline from Linux v6.17 (the file returns 404 at v6.16), and `PERF_RECORD_CALLCHAIN_DEFERRED` appears in the perf UAPI header from v6.19.
An SFrame-based unwinder is not in mainline as of v7.4-rc2 (2026-11): `kernel/unwind/` holds only `Makefile`, `deferred.c`, and `user.c`.
The v15 patch series (Jens Remus, 2026-05-20) parses SFrame V3, enables it on x86-64 only, rejects old SFrame V2 sections, and requires binaries "generated by binutils 2.46+ with assembler option --gsframe-3".
[[Profiling-Decision-Records]] owns the frame-pointer versus unwind-table policy decision.

### 4. Symbolization happens off the host

The BPF unwinder emits addresses, not names.
The profiler identifies a native executable by a file ID, `SHA256(File[:4096] + File[-4096:] + BigEndianUInt64(Len(File)))` truncated to 16 bytes, and reports each frame as a file ID plus an offset inside the file.
The internals document gives the reason: native executables in production are often stripped, and shipping symbols to production "would be both wasteful in terms of disk usage and also a major friction point in initial adoption".
Interpreter and JIT frames are the exception: their symbols exist only inside the running process, so the agent symbolizes them on the host.
Go is a partial exception: the Alpha announcement lists "automatic on-target symbolization of Go executables", because `.gopclntab` ships in every Go binary.

Parca fills in the backend half.
Its agent extracts debug information from binaries it sees and uploads it; for native mappings it "prefers the GNU build ID, then the Go build ID, and uses the executable FileID hash only when neither is available" (Parca Agent README).
The Parca server symbolizes asynchronously: "in a predefined recurring interval (10 seconds by default)" it finds ingested locations that are symbolizable but not yet symbolized.
A stripped production binary is symbolized only if its unstripped twin, or separate debug info, reached the symbol store, which usually means uploading from CI by build ID; Pyroscope 2.0.0 added a `profilecli debuginfo upload` subcommand for that step.
This is GWP's "global repository" of unstripped binaries, rebuilt in the open.

### 5. Wire formats: pprof and OTLP Profiles

**pprof** (`profile.proto`) is a gzip-compressed protocol buffer that is independent of the kind of data and of how it was sampled.
A `Profile` holds `sample_type`, `sample`, `mapping`, `location`, `function`, and a `string_table` whose first entry is the empty string.
A `Sample` is a list of `location_id` values, one `value` per sample type, and a list of `Label` messages (a `key` with a `str` or a `num`).
A `Mapping` carries `memory_start`, `memory_limit`, `file_offset`, `filename`, and `build_id`, which is the hook for off-host symbolization.
Samples with identical stacks merge by adding values, which is why profiles from thousands of hosts aggregate cheaply.

**OTLP Profiles** (`opentelemetry.proto.profiles.v1development`) keeps that model and changes how it is shared and keyed.
Every message in the file is marked `Status: [Alpha]`, and the file states "Current OTLP Profiles development version: 1", a counter for incompatible changes.

| pprof | OTLP Profiles | Why it changed |
| :--- | :--- | :--- |
| One self-contained `Profile` per file | `ProfilesData` holds `resource_profiles` plus one shared `ProfilesDictionary` | Many profiles in one export share one set of tables |
| `string_table` is the only shared table | `mapping_table`, `location_table`, `function_table`, `link_table`, `string_table`, `attribute_table`, `stack_table`, each with a zero value at index 0 | Index 0 means "not set" everywhere, and identical stacks are stored once |
| `Sample.location_id[]` inline | `Sample.stack_index` into `stack_table`; a `Stack` lists `location_indices` leaf first | A hot stack is one integer reference instead of a repeated list |
| `Sample.value[]`, one per `sample_type` | `Sample.values[]` and `Sample.timestamps_unix_nano[]`, with one `sample_type` per `Profile` | A sample can be an aggregate, a list of timestamps, or both |
| `Sample.label[]` | `Sample.attribute_indices` into `attribute_table` (`KeyValueAndUnit`) | Attributes are OpenTelemetry `AnyValue` with an optional unit |
| No trace reference | `Sample.link_index` into `link_table`; a `Link` is a 16-byte `trace_id` and an 8-byte `span_id` | Sample-level correlation with traces |
| No resource | `ResourceProfiles` and `ScopeProfiles` wrap each `Profile` | Same resource identity as the logs, metrics, and traces of the process |

The proto defines a sample's identity as "the tuple of {stack_index, set_of(attribute_indices), link_index}", and says samples with the same identity "SHOULD be combined by appending timestamps and values to the data arrays".
So a link is part of a sample's key: the same stack under two different spans is two samples.
The eBPF profiler's reporter (`generate.go`) follows that rule: it adds a `link_table` entry, and sets `link_index`, for every sample whose key carries both a valid trace ID and a valid span ID, whatever the sample's profile type.

The specification (Status: Alpha) says the format "in most cases supports lossless conversions" to and from existing formats, and that profiles "MUST be linkable to logs, metrics" and traces through shared resource context.
When a source format cannot be represented, `original_payload_format` (examples in the proto: `jfr`, `pprof`, `linux_perf`) and `original_payload` carry the original bytes, and the proto requires the two to be set together or both left unset.
The Alpha announcement says pprof data "can be round-trip converted to/from OTLP Profiles with no loss of information" through a translator in Collector contrib, and it shipped a conformance checker (`profcheck`) and a Collector `pprofreceiver`.

Status matters here (as of 2026-11).
The signal entered public Alpha on 2026-03-26, and the announcement says it "should not be used for critical production workloads" and recommends Collector v0.148.0 or newer for testing.
The package name still ends in `v1development`.
Pyroscope's documentation for OTLP ingestion says "Breaking changes have occurred and may continue" and calls the feature "suitable for development and testing".

### 6. Storage and query: Parca and Pyroscope

Both are GWP's dimensional profile database for everyone else: label-selector queries over profile series, merged across time, hosts, and versions.

**Parca** has a server and an agent.
The server can also pull pprof profiles over HTTP from targets found by service discovery, the model Go's `net/http/pprof` endpoints made common.
Its storage documentation describes two stores: a metadata store on the embedded key-value store Badger, holding mappings, functions, and locations as protobuf identified by UUIDs, and a sample store on FrostDB, "a columnar database specifically developed for Observability purposes (originally created specifically for Parca)".
Labels are dynamic columns, and "sorting by the dynamic columns and immediately followed by the timestamp column" is what lets a query ignore data outside its time range.
As of 2026-11 the latest releases are Parca v0.30.0 (2026-11-06) and Parca Agent v0.51.5 (2026-11-10).

**Grafana Pyroscope** is a multi-tenant microservice system built from one binary whose `-target` flag picks the component.
Clients push through the Push RPC API or the HTTP `/ingest` API, or send OTLP Profiles over gRPC on port 4040.

- The v1 architecture is distributor, ingester, querier, query-frontend, query-scheduler, compactor, and store-gateway.
  Ingesters hold a per-tenant database on local disk, and "by default, each profile series is replicated to three ingesters, and each ingester writes its own block to the long-term storage", which the compactor later merges and deduplicates.
- The v2 architecture is distributor, segment-writer, metastore, compaction-worker, query-frontend, and query-backend.
  Distributors route profiles of the same application to the same segment-writers so that data queried together is stored together, and each writer "produces a single object per shard containing data of all tenant services per shard" to minimize object-store writes.
  "The metastore is the only stateful component, using Raft consensus for replication."
  Ingestion is synchronous by default: clients are blocked until data is in object storage and indexed, "with median latency expected to be less than 500ms using default settings".
  Without compaction the object count "can reach millions per hour", so compaction-workers merge segments into larger blocks, with "median time to the first compaction not exceeding 15 seconds".
  Local filesystem storage is allowed for a single node only, not in microservice mode.
- Pyroscope 2.0.0 (released 2026-04-20) made the v2 write path the default (`-write-path` flipped from `ingester` to `segment-writer`), changed the default storage backend to `filesystem`, and added `-architecture.storage` with values `v1`, `v1-v2-dual`, and `v2`, defaulting to `v1-v2-dual`.
  It also disabled label sanitization by default, so UTF-8 label names with dots are accepted as-is.
  The v1 page says v1 is "no longer in active development" with "the plan to be removed in a future Pyroscope v3.0 release".
  The latest release as of 2026-11 is v2.4.3 (2026-11-03).

The v2 design is the direction the field took after GWP: stateless writers, object storage as the only durable store, and a small consensus-backed index.

#### Memory profiles

An eBPF CPU agent sees time, not allocations, so heap, goroutine, and mutex profiles traditionally come from the process itself.
GWP used in-process heap and lock-contention profilers behind an HTTP server, and a Go service serves the equivalent pprof endpoints for a Parca or Pyroscope server to scrape.
The backend stores them as the same labelled pprof series as CPU profiles, so one query language and one flame graph cover both.

The OpenTelemetry profiler has started to close this gap from outside the process.
Its `heap` probe (a Collector extension at `development` stability) attaches to a USDT probe with provider `otel_memory` and signature `alloc(void *user, uint64_t size, uint64_t weighted_bytes)`, found by scanning `.note.stapsdt`.
Each hit unwinds the allocating stack and is exported as two profiles, `alloc_space` in bytes and `alloc_objects` in counts.
The process must ship that USDT site (the header calls the current provider the one "emitted by the reference sampler implementation"), so this is cooperative instrumentation, not transparent heap profiling.
[[Cpp-Memory-Profiling-heaptrack-Massif-DHAT-and-Allocators]] owns allocation profiling on a single host.

### 7. Off-CPU profiles

An on-CPU profile cannot see a thread that is blocked on a lock, a disk read, or a downstream call, because a sleeping thread is never interrupted by the CPU clock.
The OpenTelemetry profiler adds the missing half with two probes in `off_cpu.ebpf.c`, loaded by the `offcpu` Collector extension (`probes/offcpu`, stability `development`).

1. `tracepoint/sched/sched_switch` fires when a thread is switched out.
   The program returns early when `bpf_get_prandom_u32() > off_cpu_threshold`, skips processes that are too new, and otherwise stores `bpf_ktime_get_ns()` in the LRU hash map `sched_times` under the thread's `pid_tgid`.
2. `kprobe/finish_task_switch` fires when a thread is switched back in.
   If the thread has an entry, the program computes the elapsed nanoseconds, deletes the entry, drops the event if it is shorter than `off_cpu_min_duration_ns`, pushes the duration as the trace's context value, and calls `unwind_trace()`, which runs the same unwinder chain as the CPU path.

Design details visible in the source:

- The duration is measured on `CLOCK_MONOTONIC` (`bpf_ktime_get_ns()`), which does not advance during system suspend, so a laptop or VM suspend is not charged to every thread that was descheduled across it; the trace timestamp is `CLOCK_BOOTTIME`.
- The extension's `threshold` is a probability in `]0.0, 1.0]`, converted to `uint32(threshold * math.MaxUint32)` and compared with a 32-bit random number, so the test is a single comparison in BPF.
- `min_duration` drops short waits before any stack is unwound, which is the cheap way to ignore the flood of sub-microsecond switches.
- `sched_times` is sized automatically to `4096 * threshold`, clamped to [16, 4096], from an assumed 1000 Hz of scheduler events and 3 s average off-CPU time; `max_tracked_threads` overrides it, up to 2^20.
  An LRU map that is too small silently evicts in-flight entries, which loses long waits first.
- The origin registers sample type `off_cpu` in `nanoseconds` with `ReportValues: true` and no period, so each sample's values are durations, aligned with its timestamps.

The stack is the one the thread blocked in, and the value is how long it was off the CPU.
The sampling decision is per switch-out event, not per unit of time, so with a recording probability `p` the recorded durations sum to about `p` times the true off-CPU time and must be scaled by `1/p` for absolute numbers (this follows from the per-event Bernoulli test; no fetched document states it).
Parca Agent exposes the same probability as `--off-cpu-threshold=0`, and Grafana Alloy as `off_cpu_threshold` on `pyroscope.ebpf`, a float from 0 to 1 whose default 0 disables off-CPU profiling.

The OTLP proto's own example pairs `sample_type=(off_cpu, nanoseconds)` with `period_type=(context_switch, count)`; the profiler instead leaves the period unset, so a backend must not assume a period on off-CPU profiles.
[[Playbook-Diagnose-a-Production-Tail-Latency-Spike-with-eBPF-and-perf]] owns ad hoc off-CPU triage with bpftrace on one host.

### 8. Joining samples to trace spans

A flame graph for a whole service answers "where does this service spend CPU".
A trace answers "which span of this request was slow".
Joining them answers "what code was this span running, or waiting in", and there are four ways to make the join.

**In-process labels.**
The profiler runs inside the process and already knows the active span.
In Go, `runtime/pprof` attaches labels to goroutines, and its documentation states that "currently only the CPU and goroutine profiles utilize any labels information".
`grafana/otel-profiling-go` wraps the OpenTelemetry `TracerProvider`; on span start it calls `pprof.WithLabels` and `pprof.SetGoroutineLabels`, and on span end it restores the previous labels.
By default the `trace_id` label is set on the local root span and inherited by descendants, while the `span_id` label and the `pyroscope.profile.id` span attribute are set on the local root span only, so child spans' samples are counted under the root.
`WithSpanIDLabelScope(ScopeAllSpans)` labels every span, and the README warns that it "significantly increases label cardinality".
Pyroscope then selects the samples whose `span_id` label equals the span being viewed (it also accepts `profile_id` as a legacy alias).
The Python integration (`pyroscope-otel`, a `PyroscopeSpanProcessor`) works the same way and, like Go, supports CPU profiles only.

**Out-of-process reads by the eBPF agent.**
A whole-system eBPF profiler is outside the process, so the context must be somewhere the BPF program can read at the moment of the sample.
`unwind_stop()` consults three sources in a fixed order, and the order decides who wins:

1. `maybe_add_otel_span_trace_id()` looks up `bpf_get_current_pid_tgid()` in `traces_ctx_v1`, an LRU hash map of 2^14 entries that implements the OpenTelemetry eBPF Instrumentation (OBI) correlation specification.
   OBI and the profiler both create and pin it at `$PINPATH/otel/traces_ctx_v1` (`/sys/fs/bpf` by default), and OBI writes `{u8 trace_id[16]; u8 span_id[8]}` per thread.
   OBI only populates it when told a reader exists (`ebpf.populate_trace_context: true` or `OTEL_EBPF_BPF_POPULATE_TRACE_CONTEXT=true`); without that, in the specification's words, "every profile sample looks like it ran outside a span".
   This is the zero-code route: neither the application nor its SDK changes.
2. `maybe_add_golang_labels()` reads up to `MAX_GO_LABELS` (10) goroutine labels into 16-byte keys and 48-byte values, handling both the Go 1.24+ slice layout and the older map layout; the Go labels tracer is off unless enabled (`go_labels_disabled` defaults to true in BPF); Parca Agent turns it on with its default `--tracers=all` or explicitly with `--tracers=labels`, and tracer lists without `labels` or `all` leave it off (Parca Agent README).
3. `maybe_add_thread_context_info()` reads the record defined by OTEP 4947 through an ELF TLS pointer named `otel_thread_ctx_v1`, and its IDs override the first source "when either is nonzero".

The thread-local record is byte-packed, native-endian, and read in two `bpf_probe_read_user()` calls: the 28-byte header, then the attribute payload.

```c
typedef struct __attribute__((packed)) ThreadContextBuf {
  TraceID trace_id;
  SpanID span_id;
  // 0 while the writer is mid-update.
  u8 valid;
  u8 _reserved;
  // Payload length in bytes.
  u16 attrs_data_size;
} ThreadContextBuf;
```

- OTEP 4947 now names byte 25 `trace-flags` (the W3C flags byte); the profiler's struct still calls it `_reserved` and ignores it (as of 2026-11).
- The payload is a run of `[key index u8][length u8][utf-8 bytes]` entries whose key indexes point into `threadlocal.attribute_key_map`, published once per process through the OTEP 4719 process context.
  The OTEP recommends keeping the whole record at or under 640 bytes, and the profiler caps the payload at `MAX_THREAD_LABEL_DATA_SZ` (640), counting `metricID_UnwindThreadContextAttrsTruncated` instead of dropping the record.
- Discovery is ELF-driven (`interpreter/threadcontext/threadcontext.go`): an object with a `PT_TLS` segment that defines an 8-byte `otel_thread_ctx_v1` symbol is registered in `thread_context_procs`.
  The OTEP requires the symbol in `.dynsym` "even for statically linked binaries", for example through `--export-dynamic-symbol`, and recommends the TLSDESC dialect (`-mtls-dialect=gnu2`) for writer speed.
- Concurrency is deliberately cheap: only the owning thread writes its record, and readers "MUST only read thread context while the target thread is stopped or interrupted", so writers need only compiler fences (`atomic_signal_fence`) and volatile writes, not CPU barriers.
  A writer either swaps the TLS pointer to a finished record or flips `valid` to 0, edits in place, and sets it back to 1, but never both; the BPF side skips a record whose `valid` is 0.

OTEP 4947 calls SDK support optional, names its schema version `tlsdesc_v1_dev` for experimentation, and says the authors "tested it with C/C++, Rust, and Java".
Its feasibility list rates Python as "Full support using native library", with tracers on Python 3.14 or later able to track context activation through `PyContext_WatchCallback` and older versions needing to monkey-patch the runtime.
That is a feasibility assessment in a proposal, not a shipped SDK feature; this note found no released OpenTelemetry Python SDK that publishes the record (as of 2026-11).
For Go, the OTEP expects readers to keep reading pprof labels instead of a TLS record, because of the goroutine scheduler and the cost of crossing into native code.

**Off-CPU samples carry links too.**
The off-CPU kprobe ends in the same `unwind_stop()`, and the reporter attaches a link to any sample whose key has both IDs, so an off-CPU sample can be joined to the span that was waiting.
`finish_task_switch` runs as the thread is switched back in, so the context read is the blocked thread's own, and since only that thread writes its record, it is the context the thread blocked with.
This is a conclusion from reading the source; no off-CPU export was captured for this note.

**Resource and time.**
With no per-sample context, profiles and traces still share resource attributes such as `service.name`, `k8s.pod.uid`, and `container.id`.
Selecting the profile of one pod during one span's time window is a coarse join: it shows what the pod was doing, not what the request was doing.

All four share one hard limit, which is sampling arithmetic.
A profiler that samples every `T` milliseconds sees a span that used `c` milliseconds of CPU with probability about `c / T` when `c` is smaller than `T`.
At Go's 100 Hz, `T` is 10 ms, and the Pyroscope documentation warns that "spans shorter than 10ms may not be captured".
At 19 Hz, `T` is 52.6 ms, so a 5 ms span has about a 9.5% chance of containing any sample at all.
Span-level profiles are therefore useful for long spans and for aggregates over many spans of the same name, and nearly empty for one short span.

## Commands and Numbers to Know

Every value was read from the cited documentation or source in 2026-11; none was measured for this note.

| Item | Value | Platform and date | What it controls |
| :--- | :--- | :--- | :--- |
| Parca Agent `--profiling-cpu-sampling-frequency` | `19` | Linux (as of 2026-11) | Samples per second per logical CPU |
| Parca Agent `--profiling-duration` | `5s` | Linux (as of 2026-11) | "The agent profiling duration to use" (flag help text) |
| Parca Agent `--profiling-probabilistic-threshold` | `100` (always on); 1-99 enables a per-interval coin flip | Linux (as of 2026-11) | Fraction of intervals in which this host profiles |
| Parca Agent `--profiling-probabilistic-interval` | `1m` | Linux (as of 2026-11) | Length of one on-or-off interval |
| Parca Agent `--off-cpu-threshold` | `0` (off); probability 0.0-1.0 | Linux (as of 2026-11) | Off-CPU event sampling |
| Parca Agent `--remote-store-format=otlp` | off unless set | Linux (as of 2026-11) | Export OTLP Profiles instead of Parca's Arrow-based format |
| Parca Agent `--debuginfo-upload-disable` | off unless set | Linux (as of 2026-11) | Stops debug info upload, for example when the receiver is a Collector |
| Parca Agent `--metadata-discovery` | `full`; `local` leaves Kubernetes enrichment to the receiver | Linux (as of 2026-11) | Whether the agent queries Kubernetes and container runtimes |
| Parca Agent kernel requirement | 5.3+ with BTF (README) | Linux (as of 2026-11) | Minimum kernel the README states |
| OTel profiler `samples_per_second` | `20` | Linux (as of 2026-11) | Samples per second per logical CPU |
| OTel profiler CPU profile type | sample `samples`/`count`, period `cpu`/`nanoseconds` = `1e9 / samples_per_second` | Linux (as of 2026-11) | How CPU samples appear in OTLP |
| OTel profiler `reporter_interval` | `5s`, with `reporter_jitter` `0.2` | Linux (as of 2026-11) | How often traces are flushed to the pipeline |
| OTel profiler `probabilistic_threshold` | `100`; `probabilistic_interval` `1m`, valid 1 to 5 minutes | Linux (as of 2026-11) | Machine-dimension sampling, as in GWP |
| OTel profiler `max_rpc_msg_size` | `32 MiB` | Linux (as of 2026-11) | Largest export message |
| OTel profiler kernel requirement | 5.10, amd64 and arm64 | Linux (as of 2026-11) | Enforced unless `no_kernel_version_check` is set |
| OTel `offcpu` extension | `threshold` in ]0, 1], `min_duration` `0s`, `max_tracked_threads` auto in [16, 4096] | Linux (as of 2026-11) | Off-CPU probability, short-wait filter, map size |
| OTel `heap` extension | USDT `otel_memory:alloc`; `map_scale_factor` default 2, max 8 | Linux (as of 2026-11) | Cooperative allocation profiling |
| Collector feature gate | `--feature-gates=+service.profilesSupport` | Linux (as of 2026-11) | Enables the profiles pipeline in the Collector |
| OTel profiler stated overhead ceiling | 1% CPU and 250 MB memory "upper limits in testing" (README) | Linux (as of 2026-11) | Project's own limits, not measured here |
| `StackDelta` / `UnwindInfo` size | 4 bytes / 12 bytes | Linux (as of 2026-11) | Per-rule cost of the flattened unwind tables |
| `UNWIND_INFO_MAX_ENTRIES` | `16384` | Linux (as of 2026-11) | Distinct unwind rules system-wide |
| Stack delta page | 64 kB (`STACK_DELTA_PAGE_BITS` 16), at most 16 search steps | Linux (as of 2026-11) | Bounds the in-kernel binary search |
| `NATIVE_FRAMES_PER_PROGRAM` | `5` | Linux (as of 2026-11) | Native frames unwound before a tail call |
| Profiler tail-call cap | `29`; kernel `MAX_TAIL_CALL_CNT` is `33` in v6.12 | Linux (as of 2026-11) | Deep stacks end with `ERR_MAX_TAIL_CALLS` |
| `MAX_FRAME_DATA_SZ` | `sizeof(u64[3072])`, "about 1024 frames" | Linux (as of 2026-11) | Frame buffer per trace |
| `MAX_GO_LABELS` / `MAX_THREAD_LABEL_DATA_SZ` | `10` / `640` bytes | Linux (as of 2026-11) | Per-sample context budget |
| `traces_ctx_v1` | `/sys/fs/bpf/otel/traces_ctx_v1`, LRU hash, 2^14 entries | Linux (as of 2026-11) | OBI-to-profiler trace context |
| `PERF_MAX_STACK_DEPTH` | `127` | Linux v6.12 (as of 2026-11) | Depth limit of `bpf_get_stackid()` frame-pointer walks |
| `perf_event_max_sample_rate` | `100000` per second by default | Linux (as of 2026-11) | Kernel ceiling on any perf sampling rate |
| `perf_event_paranoid` | `2` by default since Linux 4.6 | Linux (as of 2026-11) | Unprivileged perf access; system-wide needs a lower value or `CAP_PERFMON` (since 5.8) |
| Alloy `pyroscope.ebpf` `sample_rate` | `19` | Linux (as of 2026-11) | Samples per second |
| Alloy `pyroscope.ebpf` `collect_interval` | `"15s"` | Linux (as of 2026-11) | How often profiles are forwarded |
| Alloy `pyroscope.ebpf` `off_cpu_threshold` | `0` (off); probability 0 to 1 | Linux (as of 2026-11) | Off-CPU event sampling |
| Alloy `pyroscope.ebpf` `demangle` | `"none"`; also `simplified`, `templates`, `full` | Linux (as of 2026-11) | C++ name demangling |
| Pyroscope OTLP ingest | gRPC on port `4040` | Linux (as of 2026-11) | Where a Collector `otlp_grpc` exporter points |
| Pyroscope `-architecture.storage` | `v1-v2-dual` since 2.0.0 | Linux (as of 2026-11) | Which storage architecture runs |
| Pyroscope `-write-path` | `segment-writer` since 2.0.0 | Linux (as of 2026-11) | `ingester` restores the v1 write path |
| Pyroscope v2 ingest latency | median under 500 ms, synchronous by default | Linux (as of 2026-11) | Documented expectation, not measured here |
| Go CPU profiler rate | 100 samples per second (Pyroscope Go span profiles page) | Both (as of 2026-11) | A span under 10 ms of CPU may have no sample |
| OTLP Profiles status | Alpha; package `...profiles.v1development`; opentelemetry-proto v1.11.1 | Both (as of 2026-11) | Wire compatibility is not yet guaranteed |
| `Link` | 16-byte `trace_id`, 8-byte `span_id` | Both (as of 2026-11) | The per-sample trace reference |
| OTEP 4947 record header | 28 bytes; whole record recommended at or under 640 bytes | Linux (as of 2026-11) | Thread context cost per sample |
| GWP aggregated overhead | less than 0.01 percent | Linux (2010 paper) | Fleet-wide cost with two-dimensional sampling |
| Fedora 38 default flags | `-fno-omit-frame-pointer -mno-omit-leaf-frame-pointer` | Linux (as of 2026-11) | Distribution-wide frame pointers |

Commands that the cited documentation gives verbatim:

```bash
# OpenTelemetry eBPF profiler as a Collector receiver (profiler README).
sudo ./otelcol-ebpf-profiler --feature-gates=+service.profilesSupport \
    --config cmd/otelcol-ebpf-profiler/local.example.yaml

# Parca Agent exporting OTLP Profiles to a Collector (Parca Agent README).
parca-agent \
  --remote-store-format=otlp \
  --remote-store-address=otel-collector.observability.svc:4317 \
  --remote-store-insecure \
  --metadata-discovery=local \
  --debuginfo-upload-disable \
  --symbolize-go
```

The off-CPU extension's configuration, as its package comment documents it:

```yaml
extensions:
  offcpu:
    threshold: 0.1            # capture probability in ]0.0, 1.0]
    min_duration: 0s          # optional; 0 = keep all sampled waits.
    max_tracked_threads: 0    # optional; 0 = auto from threshold ([16, 4096]).
```

## Multi-Language Comparison

How one whole-system eBPF agent sees each language, from the OpenTelemetry profiler's README, internals document, and source, and from OTEP 4947 (as of 2026-11, Linux).

| Concern | C++ | Python | Rust |
| :--- | :--- | :--- | :--- |
| Where the unwind rules come from | `.eh_frame`, flattened to stack deltas | CPython's own frame structures, read by `PROG_UNWIND_PYTHON`; native frames around them use `.eh_frame` | `.eh_frame`, same native path as C++ |
| Change needed in the process | None | None; the README says no reconfiguration or restart of interpreters is needed | None |
| Where symbols are resolved | Backend, from file ID plus offset and uploaded debug info | On the host, by the agent, because the names live in the interpreter | Backend, same as C++ |
| Frame-pointer build switch | `-fno-omit-frame-pointer` (and `-mno-omit-leaf-frame-pointer` in Fedora's defaults) | Not applicable to Python frames; see [[Python-CPython-perf-Trampoline-and-Tachyon-Internals]] for the interpreter's own perf support | `-C force-frame-pointers=yes`; see [[Rust-CPU-Profiling-cargo-flamegraph-samply-and-Frame-Pointers]] |
| Well-known `profile.frame.type` value | `native` | `cpython` | `rust` is defined; the convention says a more precise value than `native` "MUST be used" when possible |
| Mangled names | `Function.system_name_strindex` carries the mangled name; Alloy's `demangle` option defaults to `none` | Not mangled | Same field as C++ |
| Per-sample trace context, out of process | `otel_thread_ctx_v1` TLS record (OTEP 4947, rated "Full support") | OTEP 4947 rates it feasible through a native library (3.14+ via `PyContext_WatchCallback`); no shipped SDK found; OBI's `traces_ctx_v1` needs no SDK | `otel_thread_ctx_v1` TLS record, which needs a native library to export the symbol correctly |
| Per-sample trace context, in process | No Pyroscope span-profile package (the documented ones are Go, Java, Ruby, .NET, and Python) | `pyroscope-otel` `PyroscopeSpanProcessor`, CPU profiles only | No Pyroscope span-profile package, same list as C++ |

Go is the outlier worth remembering: no `.eh_frame` without cgo, unwind data from `.gopclntab`, on-host symbolization, and trace context from goroutine labels instead of a TLS record.

## Worked Lab Example

This note has no lab, because the mechanism needs a privileged Linux host with a BPF-capable kernel and a backend to send to.
The two walk-throughs in this section are hand derivations from the cited source, not recorded output, and the table values and IDs in them are invented for illustration.
[[Rebuild-Lab-Sampling-Profiler-to-Folded-Stacks]] is the runnable companion: it builds the sampling half that this note describes.

### A. One native frame, by hand

A thread is interrupted inside `libfoo.so` at file offset `0x41a37` with `sp = 0x7ffc00001000`, and this is the leaf frame, so no return-address adjustment applies.

1. Split the offset: the page is `0x41a37 & ~0xFFFF = 0x40000`, and the page offset is `0x41a37 & 0xFFFF = 0x1a37`.
2. Look up `(fileID, 0x40000)` in `stack_delta_page_to_info` and get `{firstDelta: 1200, numDeltas: 6, mapID: 12}`.
3. Fetch the inner array for this file ID from `exe_id_to_12_stack_deltas`; entries 1200 to 1205 are:

```text
index  addrLow  unwindInfo
1200   0x19f0   3
1201   0x19f4   7
1202   0x1a01   9
1203   0x1a30   7
1204   0x1a38   3
1205   0x1a60   0x8001   (STACK_DELTA_COMMAND_FLAG | UNWIND_COMMAND_STOP)
```

4. Binary search with `lo = 1200`, `hi = 1206`:

```text
step 1: pivot 1203, 0x1a37 >= 0x1a30  -> lo = 1204
step 2: pivot 1205, 0x1a37 <  0x1a60  -> hi = 1205
step 3: pivot 1204, 0x1a37 <  0x1a38  -> hi = 1204, lo == hi, stop
```

5. `hi` is the first entry greater than the target, so the rule in force is entry `hi - 1 = 1203`, with `unwindInfo = 7`.
6. Suppose `unwind_info_array[7]` is `{baseReg: UNWIND_REG_SP, param: 48, auxBaseReg: UNWIND_REG_CFA, auxParam: -16}`.
   This is the flattened form of a CFI row "CFA = rsp + 48, rbp saved at CFA - 16".
7. Compute `cfa = 0x7ffc00001000 + 48 = 0x7ffc00001030`.
8. Read the saved frame pointer from `cfa - 16 = 0x7ffc00001020` and the caller's PC from `cfa - 8 = 0x7ffc00001028`.
9. Set `sp = 0x7ffc00001030`, mark the frame as a return address, and repeat from step 1 with the new PC minus 1.

Seven map lookups (the page info, the inner-array fetch, three search probes, the re-fetch of the selected entry `idx` after the search, and the rule) and two user-memory reads recovered a frame from a binary with no frame pointers and no symbols.
Two rows in the listing share `unwindInfo` 7 and two share 3, which is the deduplication that keeps `unwind_info_array` small.

### B. One span, on-CPU and off-CPU, in OTLP Profiles

A request handler runs under span `00f067aa0ba902b7` of trace `4bf92f3577b34da6a3ce929d0e0e4736`.
The process publishes that context (through OBI's map, Go labels, or the OTEP 4947 record), and the agent emits a CPU profile and an off-CPU profile in one `ProfilesData` message that share one dictionary.
The profile types are the ones the profiler registers (`samples`/`count` with a `cpu` period for CPU, `off_cpu`/`nanoseconds` with no period for off-CPU).
Shown in text form, with strings written out instead of string-table indexes and timestamps abbreviated:

```yaml
dictionary:
  link_table:
    - {trace_id: 00000000000000000000000000000000, span_id: 0000000000000000}   # index 0: no link
    - {trace_id: 4bf92f3577b34da6a3ce929d0e0e4736, span_id: 00f067aa0ba902b7}   # index 1
  stack_table:
    - {location_indices: []}                 # index 0: zero value
    - {location_indices: [7, 5, 2]}          # index 1: parse_json <- handle <- serve (leaf first)
    - {location_indices: [9, 8, 5, 2]}       # index 2: futex_wait <- pool_get <- handle <- serve
resource_profiles:
  - resource: {service.name: checkout, k8s.pod.uid: "...", container.id: "..."}
    scope_profiles:
      - profiles:
          - sample_type: {type: samples, unit: count}
            period_type: {type: cpu, unit: nanoseconds}
            period: 50000000                 # 1e9 / 20 Hz
            samples:
              - {stack_index: 1, link_index: 1, timestamps_unix_nano: [t1, t2, t3]}           # 3 samples in this span
              - {stack_index: 1, link_index: 0, timestamps_unix_nano: [t4, t5, ..., t21]}     # same stack, 18 samples, no span
          - sample_type: {type: off_cpu, unit: nanoseconds}
            samples:
              - {stack_index: 2, link_index: 1, values: [412000000], timestamps_unix_nano: [t6]}   # one wait of 412 ms
```

Reading it:

- The same stack appears twice in the CPU profile because `link_index` is part of a sample's identity.
- A CPU sample has no `values`: the profiler does not report values for this origin, so the count is the number of timestamps and the CPU estimate is count times period, here 3 x 50 ms = 150 ms for the span.
- To build the span's profile, a backend selects samples whose link equals the span's IDs, in both profiles, and renders two flame graphs: about 150 ms of sampled CPU in `parse_json`, and 412 ms blocked in `pool_get`.
- The trace alone said the span took about 560 ms; the join says most of it was waiting for a connection pool, not parsing.
- The off-CPU value is exact for the event that was recorded, and events are recorded with the configured probability, so a single span may have no off-CPU sample even when it blocked; divide sums by the threshold for absolute totals.

The in-process route in Go reaches the same join with pprof labels, using the wrapper from the `grafana/otel-profiling-go` README:

```go
// Wrap it with otelpyroscope tracer provider.
otel.SetTracerProvider(otelpyroscope.NewTracerProvider(tp))
```

That route covers CPU profiles only, since Go attaches labels to CPU and goroutine profiles and the package README says "only CPU profiling is fully supported at the moment".

## Pitfalls

> [!warning] Pitfall: treating the profile as a census
> Nineteen samples per second per CPU is a statistical estimate, and its relative error for a function taking fraction `p` of `n` samples is about `sqrt((1 - p) / (n p))`.
> A function at 0.1% needs about 100,000 samples for a 10% relative error, so a five-minute view of one small pod usually cannot rank small functions.
> Widen the time range or the set of hosts before concluding anything about a thin frame.

> [!warning] Pitfall: expecting every span to have a profile
> The `pyroscope.profile.id` attribute marks spans that can have a profile, and the Pyroscope documentation says "It does not guarantee that any samples were collected for that span".
> A span that used less CPU than one sampling interval usually has none.
> Aggregate over many spans of the same name instead of reading one short span.

> [!warning] Pitfall: an empty correlation channel that looks healthy
> With OBI as the context source, the pinned `traces_ctx_v1` map exists as soon as either side starts, but OBI leaves it empty unless `populate_trace_context` is set.
> Every sample then exports without a link, which looks exactly like work done outside any span.
> Check the link count per service, not only that profiles arrive.

> [!warning] Pitfall: high-cardinality labels and links
> Labelling every span, or putting request IDs in sample attributes, makes almost every sample unique and defeats the dictionary and merge model.
> The `otel-profiling-go` README warns that `ScopeAllSpans` "significantly increases label cardinality", and the OTLP proto warns against attributes with unbounded values.
> Keep the default root-span scope unless a specific investigation needs more.

> [!warning] Pitfall: stripped binaries with no debug info in the symbol store
> The agent sends a file ID and an offset, and the name comes from debug info found by build ID.
> A binary stripped in CI whose debug info was never uploaded shows as hexadecimal addresses forever, because the host never had the symbols.
> Upload debug info from the build, and give every binary a GNU build ID; Parca falls back to a file hash only when there is neither a GNU nor a Go build ID.

> [!warning] Pitfall: truncated or broken native stacks
> A frame whose CFI uses an expression the parser does not recognize, hand-written assembly without CFI, or a JIT the agent has no unwinder for ends the trace early.
> Very deep stacks end with `ERR_MAX_TAIL_CALLS` once the profiler's cap of 29 tail calls is reached.
> Check the agent's unwind error metrics before trusting a flame graph whose towers all stop at the same odd frame.

> [!warning] Pitfall: reading off-CPU totals without the sampling probability
> Off-CPU events are recorded with a configured probability per switch-out, so raw sums understate blocked time by that factor.
> `min_duration` removes short waits entirely, so totals also omit everything below it.
> On-CPU and off-CPU values have different sampling units (time versus events), so never add the two profiles together.

> [!warning] Pitfall: a thread context symbol the profiler cannot see
> The profiler finds `otel_thread_ctx_v1` through the dynamic symbol table of an object with a `PT_TLS` segment.
> A statically linked binary that does not export the symbol into `.dynsym`, or a symbol of the wrong size, publishes context that nobody reads.
> Link with `--export-dynamic-symbol=otel_thread_ctx_v1` (or the equivalent) and check `thread_context_procs` coverage.

> [!warning] Pitfall: building production dependencies on an Alpha wire format
> OTLP Profiles lives in a `v1development` package, and both the OpenTelemetry announcement and Pyroscope's documentation warn against critical production use.
> Pin the agent, Collector, and backend versions together and test upgrades as a set.

> [!warning] Pitfall: forgetting what a whole-system agent can see
> The agent runs privileged in the host PID namespace and reads other processes' memory to unwind interpreters and read thread context.
> Parca Agent marks its deprecated `--metadata-enable-process-cmdline` flag as one that "may expose sensitive information like secrets in profiling data".
> Treat the agent as a privileged security boundary and review what metadata it exports.

## At Scale

For one engineer, continuous profiling is the wrong tool.
A single `perf record` or `samply` run at a high rate answers the question faster, and [[Linux-perf-in-Depth-and-perf-c2c]] covers that.
The value appears when nobody knows in advance which process, host, or week will matter.

### What changes with size

- **Cost is bounded per host and unbounded in aggregate.**
  The agent's sample rate caps per-host work, but ingest, storage, and symbolization grow with hosts times CPUs times distinct stacks times label combinations.
- **Precision is cheap at scale.**
  With the square-root law, 100,000 samples resolve a 0.1% function to about 10% relative error.
  At 19 Hz that is about 5,260 busy core-seconds, or 88 core-minutes, which a fleet with 10,000 busy cores produces in under a second of wall time.
  So a large fleet can profile a fraction of hosts, as GWP did, and lose nothing for fleet-level questions.
- **Small services are the opposite case.**
  GWP added application-specific profiling because the default rate did not collect enough samples for applications that used few cycles fleet-wide.
  Machine-dimension sampling must therefore be per service, not one global number.
- **Symbols become a pipeline.**
  At 1,000 engineers there are thousands of builds a day, and symbolization fails silently when one team's CI does not upload debug info.
- **Object counts become the storage problem.**
  Pyroscope's v2 documentation says objects "can reach millions per hour" without compaction, which is why shard-per-writer objects and fast compaction are part of the design, not tuning.
- **The data starts paying for itself.**
  GWP found that zlib accounted for "nearly 5 percent of all CPU cycles" fleet-wide, something no single application's profile showed.
  It also fed sampled profiles to feedback-directed optimization; [[Cpp-PGO-AutoFDO-and-BOLT-Workflow]] owns that loop.

### Decision rules

- **One to about 50 engineers, one cluster.**
  Run one agent as a DaemonSet at the default rate and one backend in single-binary mode.
  Do not tune anything until a query is too slow or a bill is too high.
- **About 50 to 1,000 engineers.**
  Give the profiling backend an owner, put debug info upload into the shared build templates, and set a label policy: resource attributes yes, per-request values no.
  Use object storage with a retention tier, and keep full resolution for days, not months.
- **1,000 to 50,000 engineers.**
  Sample hosts as well as events: set the probabilistic threshold per service so that each service still gathers enough samples for its smallest interesting function.
  Run the backend as stateless writers over object storage, set per-tenant limits, and expose the data through an API so cost attribution and compiler feedback can consume it.
  Report in money as well as percent; the GWP paper notes that "dollar amount per performance change" became a popular metric.
- **Frame pointers or tables.**
  If the organization controls its build flags, turn frame pointers on fleet-wide and accept a cost in the range distributions measured (about 1-2% in most cases, more for some interpreters).
  Keep the `.eh_frame` unwinder for third-party and distribution code that was not built that way.
  [[Profiling-Decision-Records]] holds the full decision record.
- **Trace correlation.**
  Start with OBI's `traces_ctx_v1` where OBI already runs, because it needs no application change.
  Turn on root-span labels for Go services whose spans are usually longer than a few sampling intervals, and rely on aggregates by span name elsewhere.
  Adopt the thread-context TLS mechanism only where the language SDK supports it and the organization accepts an in-development specification.
- **Off-CPU.**
  Enable it per service with a small threshold and a `min_duration`, and scale by the threshold in the backend; it multiplies samples per blocked thread, not per CPU, so its cost does not have the per-CPU cap that on-CPU sampling has.
- **OTLP Profiles.**
  Use it at the edges where it removes a custom integration, keep the ability to export pprof, and do not make an Alpha format the only copy of data that anything critical depends on.
- **Parca or Pyroscope.**
  Both store label-indexed profiles in columnar form and both ingest from the same eBPF profiler lineage, so decide on operations: existing Grafana stack, multi-tenancy needs, and who will run it.
  That comparison belongs to [[Profiling-Decision-Records]].

## Practice Drill

> [!tip] Practice Drill:
> Nothing in this drill was run for this note; it is a procedure to follow on a Linux host or VM with kernel 5.10 or later and root.
> First, on paper: take the stack delta listing from the worked example and resolve page offsets `0x19f2`, `0x1a01`, and `0x1a70` by hand, writing down each `lo`, `hi`, and pivot, and say what the unwinder does with the entry it finds for `0x1a70`.
> Second, on the host: build `otelcol-ebpf-profiler` from the opentelemetry-ebpf-profiler repository with `make agent`, start a backend that accepts OTLP Profiles, and run the profiler with the command in Commands and Numbers to Know.
> Third, run one CPU-bound C or C++ program built with `-O2` and no `-fno-omit-frame-pointer`, strip a copy of it, and compare the flame graphs of the stripped and unstripped runs to see which frames lose their names and which lose their stacks.
> Fourth, compute how many samples your program should have produced from its CPU time and the sample rate, and compare with the number of timestamps the backend reports.
> Fifth, enable the `offcpu` extension with `threshold: 0.1`, run a program that sleeps in a loop, and check that the summed off-CPU time divided by 0.1 approaches the program's sleep time.
> Then do [[Rebuild-Lab-Sampling-Profiler-to-Folded-Stacks]] to build the sampler yourself.

## Interview Questions

> [!question] L5: Why do continuous profilers default to about 19 or 20 Hz when in-process profilers such as Go's sample at 100 Hz, and why 19 and not 20?
>
> > [!success]- Answer
> > The rate bounds the per-host cost: each sample costs an interrupt, a stack walk, and a map update, so samples per second per CPU is the knob that makes "always on" affordable.
> > Precision comes from aggregation instead: summing over hosts and minutes yields far more samples than one short high-rate run.
> > The Parca Agent design document gives the reason for 19 specifically: it is prime, which avoids locking onto other periodic activity, and it names 100 Hz (every 10 ms) as a period other activity is likely to share.
> > The OpenTelemetry profiler defaults to 20, so the prime choice is a preference, not a requirement.

> [!question] L5 (predict the output): A service uses a steady 2.5 cores on an 8-CPU host for 10 seconds while the agent samples at 19 Hz per logical CPU.
> About how many on-CPU samples land in that service, and what is the most the host could produce?
>
> > [!success]- Answer
> > The clock fires 19 times per second on each CPU, and a sample is attributed to whatever is running there.
> > The service occupies 2.5 CPUs on average, so it receives about 19 x 2.5 x 10 = 475 samples.
> > The host's ceiling is 19 x 8 x 10 = 1,520 samples.
> > With 475 samples, a function taking 10% of the service has a relative standard error of about `sqrt(0.9 / 47.5)`, or 14%, so only the largest frames are trustworthy at this window.

> [!question] L5: What is in a pprof profile, and what does OTLP Profiles add?
>
> > [!success]- Answer
> > A pprof profile is a gzip-compressed protobuf holding sample types, samples, locations, functions, mappings, and a string table whose index 0 is the empty string.
> > Each sample is a list of location IDs, one value per sample type, and optional labels; each mapping can carry a build ID for later symbolization.
> > OTLP Profiles keeps the model and adds resource and scope wrappers, one dictionary shared by all profiles in a message, a stack table so each distinct stack is stored once, attributes with units, optional per-sample timestamps, and a link table of trace and span IDs.
> > It is in Alpha in a `v1development` package as of 2026-11.

> [!question] L6: Walk through how a BPF program unwinds one frame of a C++ binary that has no frame pointers.
>
> > [!success]- Answer
> > User space has already parsed the binary's `.eh_frame`, evaluated the CFI state machine, and loaded sorted 4-byte stack deltas into a per-executable BPF array, with the distinct rules in a shared unwind-info array.
> > The program converts the PC into a file ID and an offset, looks up the 64 kB page of that offset to get the range of deltas, and binary searches that range in at most 16 fixed steps.
> > The entry gives either a command, such as stop, PLT, signal frame, or frame pointer, or an index to a rule of the form "CFA = register + constant".
> > It computes the CFA, optionally reads the saved frame pointer, reads the return address at CFA minus 8 with `bpf_probe_read_user` (on x86-64), and sets the stack pointer to the CFA.
> > After five frames it tail-calls the next unwinder program, keeping state in a per-CPU record, until a stop entry or the tail-call cap ends the trace in `unwind_stop`.

> [!question] L6 (predict the output): This Python transliterates the profiler's `bsearch_step` loop.
> What does it print?
>
> ```python
> def lookup(addr_low, first, num, page_offset):
>     lo, hi = first, first + num
>     for i in range(16):
>         pivot = (lo + hi) >> 1
>         if page_offset >= addr_low[pivot]:
>             lo = pivot + 1
>         else:
>             hi = pivot
>         if not (lo < hi):
>             return hi - 1, i + 1
>     return None
>
> deltas = [0x0010, 0x0040, 0x0044, 0x0100, 0x0180]
> for po in (0x0043, 0x0180, 0x0008):
>     print(hex(po), lookup(deltas, 0, 5, po))
> ```
>
> > [!success]- Answer
> > It prints `0x43 (1, 2)`, then `0x180 (4, 2)`, then `0x8 (-1, 3)`.
> > The search finds the first entry greater than the target and steps back one, so `0x43` resolves to the rule that starts at `0x40`, and `0x180` to the rule that starts exactly there.
> > `0x8` is below the page's first entry, so the result is one before the first index.
> > In the real code that index is `firstDelta - 1`, the last delta of the previous page, which is the rule still in force at the page boundary.
> > The real code also returns `ERR_NATIVE_EXCEEDED_DELTA_LOOKUP_ITERATIONS` instead of `None` when 16 steps do not converge.
> > The output was checked by running the snippet with `python3` on darwin-arm64 on 2026-11-15; it is a model of the C code, not the BPF program itself.

> [!question] L6 (debug this): A trace span carries `pyroscope.profile.id`, but opening its profile shows nothing.
> The service-level flame graph is fine.
> What do you check?
>
> > [!success]- Answer
> > First the arithmetic: Go's CPU profiler samples 100 times per second, so a span that used under 10 ms of CPU usually has no sample, and the attribute only marks spans that can have a profile.
> > Second, time on CPU versus wall time: a span that spent 300 ms blocked on I/O has almost no CPU samples, and the Go integration supports CPU profiles only.
> > Third, scope: by default only the local root span gets a `span_id` label, so a child span's samples are filed under the root's ID.
> > Fourth, plumbing: the profiler must actually be running, and the profile's `service_name` must match the SDK's `service.name`.
> > The fix for the common case is to look at the aggregate for the span name over many requests, or to add off-CPU data, not to raise the sampling rate fleet-wide.

> [!question] L6: An engineer enables the `offcpu` extension with `threshold: 0.25` and `min_duration: 1ms`.
> The backend shows 30 seconds of off-CPU time for a worker pool over one minute.
> What does that number mean?
>
> > [!success]- Answer
> > Each switch-out is recorded with probability 0.25, so the estimate of total blocked time is 30 / 0.25 = 120 seconds of thread-time, for example two threads blocked the whole minute.
> > That estimate covers only waits of at least 1 ms, because shorter waits are dropped in the kprobe before any stack is unwound.
> > It is thread-time, not wall time, so it can exceed the window when many threads block at once.
> > If the LRU map `sched_times` was too small for the number of concurrently blocked threads, long waits were evicted and the estimate is low; `max_tracked_threads` raises the bound.

> [!question] L7 (debug this): After a release, flame graphs for one C++ service show hexadecimal addresses instead of function names, and many stacks are two frames deep.
> Other services are fine.
> Diagnose it.
>
> > [!success]- Answer
> > These are two different failures that often arrive together.
> > Addresses instead of names means symbolization failed: the backend has no debug info for this build ID, typically because the new pipeline strips the binary and no longer uploads debug info, or the binary lost its GNU build ID.
> > Check the build ID on the deployed binary (its `.note.gnu.build-id` note) and look for it in the symbol store.
> > Shallow stacks mean unwinding failed, which is independent of symbols: the agent derives unwind tables from `.eh_frame`, so check that the section still exists and that the release did not change unwind-table generation or link the binary in a way that drops it.
> > Then read the agent's unwind error metrics for this process to see which error dominates, for example an unmatched DWARF expression or an invalid stack delta.
> > Fix the pipeline in the build templates, since history for that release stays unsymbolized until its debug info is uploaded.

> [!question] L7 (predict the output): A C++ service runs under OBI with `populate_trace_context: true` and also publishes the OTEP 4947 record.
> During one sample, OBI's map holds span `A`, and the thread's record is valid with all-zero trace and span IDs.
> Which span does the sample link to, and what changes if the record holds span `B`?
>
> > [!success]- Answer
> > The sample links to span `A`.
> > `unwind_stop` first copies the IDs from `traces_ctx_v1`, then reads the thread record, and the record replaces both IDs only when its trace ID or span ID is nonzero; an all-zero record means "no active span" and preserves the earlier IDs.
> > If the record holds span `B`, the sample links to `B`, because the thread context overrides OBI's IDs.
> > If the record's `valid` byte is 0 (a write in progress), the record is skipped entirely and the sample keeps `A`.

> [!question] L7: GWP samples machines as well as events.
> Why is that the right design, and how do you size it?
>
> > [!success]- Answer
> > Per-machine overhead and fleet-wide cost are different budgets.
> > Event sampling keeps the profiled machine's overhead under a few percent, and machine sampling divides the fleet cost again, which is how the paper reaches an aggregate overhead under 0.01 percent.
> > Profile error falls as one over the square root of the sample count, so a fleet-level question needs a fixed number of samples regardless of fleet size.
> > To size it, pick the smallest function share you care about and the error you accept: 0.1% at 10% relative error needs about 100,000 samples, which at 19 Hz is about 88 busy core-minutes.
> > Then set the machine-sampling fraction per service so that the smallest service still reaches that count in the query window, because a single global fraction starves small services; GWP added application-specific profiling for exactly that reason.

> [!question] L7: Design per-request CPU and off-CPU attribution for a fleet with Go, Java, C++, and Python services.
>
> > [!success]- Answer
> > Use one whole-system eBPF agent for collection so every language gets on-CPU and off-CPU stacks without code changes, and emit OTLP Profiles so each sample can carry a link.
> > Where OBI already instruments the services, enable its `traces_ctx_v1` channel first, since it needs no SDK change.
> > For Go, have the agent read goroutine labels that the tracing wrapper sets; for C++ and Java, publish the OTEP 4947 thread-local context record; for Python, use OBI or the in-process `pyroscope-otel` integration until an SDK publishes the record.
> > Enable off-CPU sampling with a small probability and a minimum duration, and scale by the probability in the backend.
> > Label root spans only, and present per-span-name aggregates by default, because a single short span rarely contains a sample.
> > Call out the risks: the format is Alpha, the thread-context mechanism is in development, and link cardinality multiplies stored samples, so roll out per service with a cardinality budget.

> [!question] L8+: Your company has 20,000 engineers and no frame pointers in its C++ and Rust builds.
> Do you mandate frame pointers, rely on `.eh_frame` unwinding in BPF, or wait for SFrame?
>
> > [!success]- Answer
> > Mandate frame pointers for first-party code and keep table-based unwinding for everything else.
> > Frame pointers make every tool work, including the kernel's own stack walker, at a cost distributions measured at about 1-2% in most cases and up to 10% for some CPython benchmarks, so exempt the measured outliers.
> > Table-based unwinding already works in the agents and needs no rebuild, but it costs agent memory and CPU, it stops at unrecognized CFI expressions, and it lives in a privileged component that must track kernel changes.
> > SFrame is not a plan for 2026: kernel support is not in mainline as of v7.4-rc2, the series enables x86-64 only, and it needs binutils 2.46 and a fleet-wide rebuild.
> > The decision record should state the measured cost on the company's own top services, the exemption process, and a date to revisit SFrame.

> [!question] L8+: You are asked to approve a privileged eBPF profiling agent on every production host.
> What do you require before saying yes?
>
> > [!success]- Answer
> > A blast-radius analysis: the agent holds perf and BPF capabilities, runs in the host PID namespace, and reads other processes' memory; prefer the least-privilege capability set Alloy documents over `privileged: true`.
> > A data review of everything exported: stacks reveal code paths, and metadata such as command lines and environment variables can leak secrets, so those stay off by default.
> > Bounded resource use enforced from outside the agent, with cgroup limits and alerts tied to the project's stated ceilings, plus a kill switch.
> > A staged rollout with machine-dimension sampling, automatic back-off on errors as GWP's collector did, and kernel-version gating, because verifier behaviour differs across kernels.
> > Version pinning of agent, Collector, and backend as one unit while the wire format is Alpha, and a legal review of the licences and of the patent the profiler's README cites for its `.eh_frame` technique.

## Further Reading

- Ren et al., "Google-Wide Profiling: A Continuous Profiling Infrastructure for Data Centers" (IEEE Micro, 2010): the architecture, the overhead argument, and the entropy and Manhattan-distance validation that everything since has copied.
- opentelemetry-ebpf-profiler `doc/internals.md`: the clearest written account of file IDs, stack deltas, tail-call-chained unwinders, and why symbolization is a backend job.
- opentelemetry-ebpf-profiler `support/ebpf/native_stack_trace.h` and `types.h`: the actual per-frame unwinder and its data structures, short enough to read in one sitting.
- opentelemetry-ebpf-profiler `support/ebpf/interpreter_dispatcher.ebpf.c`: `unwind_stop` and the three trace-context sources in the order they are applied.
- Polar Signals, "DWARF-based Stack Walking Using eBPF" (2022): the first public write-up of flattening `.eh_frame` into BPF maps, including the verifier constraints that shaped it.
- DWARF 5, section 6.4 "Call Frame Information": the definition of the CFA and the register rules that the flattened tables encode.
- `profiles.proto` in opentelemetry-proto: the normative OTLP Profiles data model, including the sample identity rule and the `Link` message.
- "OpenTelemetry Profiles Enters Public Alpha" (2026-03-26): what Alpha covers, what is still missing, and the roadmap items for symbolization and context sharing.
- OTEP 4947, "Thread Context": the record layout, the publication protocol, and the per-language feasibility list for out-of-process context.
- OBI `devdocs/trace-profile-correlation.md`: the pinned-map contract that correlates profiles with OBI traces without SDK changes.
- Grafana Pyroscope, "About the Pyroscope v2 architecture": a current example of a stateless, object-storage-first profile store with a Raft-backed index.
- Parca Agent README and design document: the flags, the metadata model, the OTLP export mode, and the debug info upload protocol.
- Fedora "Changes/fno-omit-frame-pointer" and the Ubuntu 24.04 frame pointer announcement: measured costs and the reasoning behind distribution-wide frame pointers.
- `google/pprof` `proto/README.md`: the pprof data model in prose, which is still the fastest way to understand every later format.

## See Also

- [[Linux-perf-in-Depth-and-perf-c2c]]: owns `perf_event_open`, sampling, and the `perf` tool that the agents' perf events build on.
- [[eBPF-bpftrace-ftrace-uprobes-and-USDT]]: owns the BPF verifier, maps, tail calls, and probe types (including the USDT probes the heap extension uses) that the unwinder runs inside.
- [[Rebuild-Lab-Sampling-Profiler-to-Folded-Stacks]]: builds the sampling half of this pipeline as a runnable lab.
- [[Playbook-Diagnose-a-Production-Tail-Latency-Spike-with-eBPF-and-perf]]: ad hoc off-CPU triage on one host.
- [[Intel-VTune-AMD-uProf-and-Hardware-Tracing-PT-SPE]]: hardware counters, which the eBPF agents do not sample by default.
- [[Cpp-Memory-Profiling-heaptrack-Massif-DHAT-and-Allocators]]: allocation profiling on one host.
- [[Rust-CPU-Profiling-cargo-flamegraph-samply-and-Frame-Pointers]]: frame pointers in Rust builds.
- [[Python-CPython-perf-Trampoline-and-Tachyon-Internals]]: the interpreter's own perf support.
- [[Cpp-PGO-AutoFDO-and-BOLT-Workflow]]: where fleet profiles feed the compiler.
- [[Profiling-Decision-Records]]: the frame-pointer and backend decision records.

## Related

- Prerequisites: none
- Next: [[Profiling-Decision-Records]]
- See also: [[04-System-Design/00-Concepts/SLOs-and-Observability|SLOs and Observability]]
