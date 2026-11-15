---
type: concept
track: [sde, distinguished, low-latency, quant-dev]
level:
status: draft
last_reviewed:
sources:
  - "Meta Open Source, Why Buck2 (docs/about/why.md), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/docs/about/why.md"
  - "Meta Open Source, Architectural Model (docs/concepts/architecture.md) and Daemon (docs/concepts/daemon.md), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/docs/concepts/daemon.md"
  - "Meta Open Source, DICE documentation (dice/dice/docs/index.md, api.md, writing_computations.md, parallelism.md, cancellations.md, incrementality.md, projections.md, transients.md), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/tree/main/dice/dice/docs"
  - "Jakob Degen and Meta Open Source, Dice incrementality (dice/docs/incrementality.md), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/dice/docs/incrementality.md"
  - "Meta Open Source, Introduction to Modern DICE, talk transcript (docs/insights_and_knowledge/modern_dice.md), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/docs/insights_and_knowledge/modern_dice.md"
  - "Meta Open Source, DICE source: api/key.rs, api/computations.rs, api/storage_type.rs, epoch/cycles.rs, storage.rs, dice.rs, facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/tree/main/dice/dice/src"
  - "Meta Open Source, Dynamic Dependencies, Anonymous Targets, Transitive Sets, Dep Files, Incremental Actions, Content-Based Paths (docs/rule_authors/), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/tree/main/docs/rule_authors"
  - "Meta Open Source, dynamic_output and dynamic_output_new (app/buck2_action_impl/src/context/dynamic_output.rs) and dynattrs (app/buck2_dynamic_actions/src/attrs_starlark.rs), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/app/buck2_action_impl/src/context/dynamic_output.rs"
  - "Meta Open Source, BXL documentation (docs/bxl/index.md, faq.md, explanation/basics.md, explanation/bxl_cquery_vs_cli_cquery.md, how_tos/basic_how_tos.md, how_tos/how_to_cache_and_share_operations.md) and BXL RFC (docs/rfcs/implemented/bxl.md), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/tree/main/docs/bxl"
  - "Meta Open Source, buck2_critical_path crate (potential.rs) and log critical-path command (app/buck2_cmd_log_client/src/critical_path.rs), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/app/buck2_critical_path/src/potential.rs"
  - "Meta Open Source, CriticalPathEntry2 message (app/buck2_data/data.proto), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/app/buck2_data/data.proto"
  - "Meta Open Source, Debugging Excess Starlark Peak Memory (docs/users/faq/starlark_peak_mem.md) and interpreter_for_dir.rs (DEFAULT_STARLARK_MEMORY_USAGE_LIMIT), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/app/buck2_interpreter_for_build/src/interpreter/interpreter_for_dir.rs"
  - "Meta Open Source, Memory Profiling (docs/developers/perf/memory.md), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/docs/developers/perf/memory.md"
  - "Meta Open Source, Hydration settings (app/buck2_settings/src/hydration.rs), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/app/buck2_settings/src/hydration.rs"
  - "Meta Open Source, File watcher selection (app/buck2_file_watcher/src/file_watcher.rs), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/app/buck2_file_watcher/src/file_watcher.rs"
  - "Meta Open Source, Remote Execution, Deferred Materialization, Common Issues (docs/users/), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/docs/users/remote_execution.md"
  - "Meta Open Source, Benefits Compared to Buck1 (docs/about/benefits/compared_to_buck1.md), facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/blob/main/docs/about/benefits/compared_to_buck1.md"
  - "Meta Open Source, Buck2 release 2026-11-01 (binary buck2 2026-10-31-e8edae33), GitHub Releases, 2026, https://github.com/facebook/buck2/releases/tag/2026-11-01"
  - "Meta Open Source, Buck2 prelude: cxx/tools/compilation_database.bxl, rust/rust-analyzer/resolve_deps.bxl, python/typecheck/batch.bxl, go/tools/gopackagesdriver/driver.bxl, cxx/dist_lto/dist_lto.bzl, facebook/buck2 main branch, 2026, https://github.com/facebook/buck2/tree/main/prelude"
  - "Meta Open Source, starlark-rust README, docs/gc.md, docs/types.md, CHANGELOG.md, environment/modules.rs, crate version 0.14.2, facebook/starlark-rust main branch, 2026, https://github.com/facebook/starlark-rust"
  - "The Bazel Authors, Skyframe, Bazel Documentation, 2026, https://bazel.build/reference/skyframe"
  - "The Bazel Authors, Client/server implementation, Bazel Documentation, 2026, https://bazel.build/run/client-server"
  - "The Bazel Authors, Optimize Memory, Bazel Documentation, 2026, https://bazel.build/advanced/performance/memory"
  - "The Bazel Authors, Bazel 7.0 LTS release announcement, Bazel Blog, 2023, https://blog.bazel.build/2023/12/11/bazel-7-release.html"
---

# Buck2: DICE Engine, Starlark, and BXL

> [!summary] Claim:
> Buck2 is a Rust daemon whose whole build, from directory listing and `BUCK` file evaluation through configuration, rule analysis, action execution, and materialization, is one demand-driven graph on DICE, an incremental engine of async Rust computations with no phase barrier between loading, analysis, and execution.
> DICE records each computation's dependencies in series-parallel form, invalidates through reverse-dependency edges when a file or config changes, re-verifies dependencies in recorded order without speculation, and cuts off recomputation when a recomputed value compares equal to the cached one.
> Rules are pure Starlark on the `starlark-rust` interpreter, whose frozen, immutable modules can be shared across threads; `dynamic_output` and anonymous targets give rules a bounded form of monadic (dynamic) dependencies without breaking query.
> BXL is Starlark run as a DICE key that drives queries, analysis, and builds from inside the daemon; it powers IDE integrations such as compilation databases and rust-analyzer, and `buck2 log critical-path` reads the same graph to attribute wall time to the longest dependency chain.

```mermaid
flowchart TD
    subgraph CLIENT["buck2 client: one short-lived process per command"]
        CLI["buck2 build / test / cquery / bxl"]
    end
    subgraph DAEMON["buck2 daemon: one per project root and isolation dir<br/>(~/.buck/buckd/&lt;project path&gt;/v2)"]
        FW["File watcher<br/>notify (OSS default), watchman, edenfs, fs_hash_crawler"]
        UPD["DiceTransactionUpdater<br/>changed(k): dirty an untracked input<br/>changed_to(k, v): inject a value"]
        CORE["DICE core state, one thread<br/>per-key validity windows, rdep maps, branches"]
        EVAL["Per-transaction async evaluator on tokio<br/>key to task map: dedup, WeakShared cancellation"]
        subgraph GRAPH["One DICE graph, demand-driven, no phase barrier"]
            F["File contents, directory listings"]
            LD["Load: BUCK and .bzl evaluation<br/>Starlark modules frozen and shared"]
            UT["Unconfigured target node"]
            CT["Configured target node<br/>(selects resolved, transitions applied)"]
            AN["Analysis: rule impl returns providers<br/>and declares actions"]
            DY["Dynamic lambda (dynamic_output)<br/>Anon target analysis"]
            AX["Action execution<br/>local, remote (REAPI), or hybrid race"]
            MT["Materialization into buck-out<br/>(immediate or deferred)"]
            BX["BXL function key<br/>(script plus CLI args)"]
        end
    end
    CLI -- "gRPC" --> UPD
    FW --> UPD --> CORE
    EVAL <-- "lookup / check deps / report" --> CORE
    F --> LD --> UT --> CT --> AN --> AX --> MT
    AN --> DY --> AX
    AX -- "artifact contents" --> DY
    BX -.->|"cquery"| CT
    BX -.->|"analysis"| AN
    BX -.->|"build, ensure"| AX
```

Arrows point from a value to the computations that consume it; each box is a family of DICE keys, not a phase.

## The Mechanism

Buck2 is Meta's rewrite of Buck (open sourced in March 2013), written in Rust instead of Java, and available as open source from 2023 (`docs/about/why.md`).
Everything in this note is pinned to the `facebook/buck2` main branch fetched on 2026-11-14 and, where a command was run, to the `2026-11-01` release binary, which reports itself as `buck2 2026-10-31-e8edae33cd7cbc6d042c58bb55a2a900ac6ad489` (as of 2026-11, macOS).
Buck2 publishes releases as date tags on the 1st and 15th of each month, and every one of them, through `2026-11-01`, is marked "prerelease" on GitHub (as of 2026-11, Both).
[[Build-System-Theory-Schedulers-and-Rebuilders]] owns the vocabulary used here (keys, tasks, schedulers, verifying and constructive traces, early cutoff); this note shows how Buck2 instantiates it.

### 1. The process model: client, daemon, and tenants

The `buck2` command is a thin client.
On first use in a project it starts a daemon for that project root, then sends every command to it over gRPC (`docs/concepts/architecture.md`, `docs/concepts/daemon.md`).
The daemon holds the DICE graph in memory between commands, which is the entire reason a no-op rebuild is fast: Buck2's own comparison reports a no-op `buck build` that took 23 seconds in Buck1 taking 0.1 seconds in Buck2 (`docs/about/benefits/compared_to_buck1.md`, Meta internal benchmark, not reproduced here).

- **One daemon per project root and isolation directory.** The default isolation directory is `v2`, which is why outputs live under `buck-out/v2/`; `--isolation-dir <name>` runs a second, independent daemon over the same checkout.
  `buck2 debug daemon-dir` printed `~/.buck/buckd/<absolute project path>/v2` on the 2026-11-01 release (as of 2026-11, macOS).
- **The daemon watches the file system.** `app/buck2_file_watcher/src/file_watcher.rs` selects the watcher from `[buck2] file_watcher`: open-source builds default to `notify` (the cross-platform `notify` crate, pinned at `9.0.0-rc.6` in the workspace `Cargo.toml`), Meta's internal build defaults to `edenfs` on an Eden checkout and `watchman` otherwise, and `fs_hash_crawler` is a fourth option.
  Watcher events become `changed()` calls on DICE file keys at the start of the next command; the console line `File changed: a.txt` in the worked example is that event.
- **Concurrent commands.** Commands whose "state" (configs and source files) is identical run in parallel on the same DICE version; commands with different state are serialized (`docs/users/faq/common_issues.md`).
  `--preemptible=ondifferentstate` lets a newer command kill an older, incompatible one, and `--exit-when=differentstate` makes a command fail with exit code 4 (`DaemonIsBusy`) instead of waiting; a preempted command exits with code 5 (`DaemonPreempted`) (`docs/users/commands_extra/exit_codes.md`, `buck2 build --help` on the 2026-11-01 release).
- **Recursive invocations.** A genrule or test that calls `buck2` inside a build must pass a different `--isolation-dir`, or Buck2 returns an error, because the inner command would need the outer command's daemon (`docs/users/faq/common_issues.md`).
- **Daemon sharing (new).** On main, `[buck2] share_daemon = true` lets several (project root, isolation dir) "tenants" with the same compatibility key (a BLAKE3 hash of binary version, user version, startup config, and trace-I/O mode) share one daemon, and `[buck2] share_dice = true` additionally shares one DICE engine across tenants; both default to false, and only Linux hosts can run a shared daemon (`docs/concepts/daemon.md`, as of 2026-11, Linux).
  The 2026-11-01 binary already lists `buck2 debug tenants`, so the plumbing ships in releases even though it is opt-in.

### 2. DICE: the Dynamic Incremental Computation Engine

The DICE README expands the name as "Dynamic Incremental Computation Engine"; the Modern DICE talk explains the original "D" was "Distributed", an aspirational name because DICE is not distributed (`dice/README.md`, `docs/insights_and_knowledge/modern_dice.md`).
`dice/dice/docs/index.md` describes it as "inspired by Adapton and Salsa", running every computation in parallel on tokio executors and deduplicating identical requests, and it still calls DICE "experimental and largely being rewritten".

#### 2.1 Keys, values, and the computation context

A computation is a Rust type implementing the `Key` trait in `dice/dice/src/api/key.rs`:

```rust
#[async_trait]
pub trait Key:
    Allocative + Debug + Display + Clone + Eq + Hash + Send + Sync + Pagable + PagableTagged + 'static
{
    type Value: Allocative + Send + Sync + 'static;

    async fn compute(
        &self,
        ctx: &mut DiceComputations,
        cancellations: &CancellationContext,
    ) -> Self::Value;

    fn equality_behavior() -> EqualityBehavior<Self::Value>;   // Compare(fn) or AlwaysUnequal
    fn validity(_x: &Self::Value) -> bool { true }            // false = transient, never cached
    fn storage_type() -> StorageType { StorageType::Normal }  // Normal or Injected
    fn value_serialize() -> impl ValueSerialize<Value = Self::Value>; // for paging to disk
    // plus key_type_name(), provide(), invalidation_source_priority()
}
```

The excerpt keeps the signatures and abbreviates the doc comments; the older `dice/dice/docs/api.md` still shows a pre-rewrite signature with `&DiceComputations` and a free-standing `equality`, so trust `key.rs` over the prose docs.

- **The key is the input.** A key's fields are the arguments of the function; a `ConfiguredTargetLabel` inside an analysis key is what makes "analysis of `//foo:bar` in configuration X" one node.
- **`compute` requests other keys through `ctx`.** Every `ctx.compute(&k)` both returns `k`'s value and records `k` as a dependency of the running key; that recording is the trace.
- **`EqualityBehavior::Compare(f)` enables early cutoff.** When an invalidated key recomputes to a value `f` says is equal to the old one, its dependents can be revalidated instead of recomputed; `AlwaysUnequal` declares that reuse is impossible, which also lets DICE skip reading a paged-out old value just to compare it.
- **Injected keys** (`InjectedKey`) have no `compute`; their values are set with `TransactionUpdater::changed_to` and are the leaves of the graph, used for global inputs such as the buckconfig inputs and the buck-out path (Modern DICE talk).
  The talk calls out the cost: because the buckconfig is a leaf that nearly everything reads, changing it "invalidates basically everything".
- **Projection keys** (`ProjectionKey`) are synchronous functions of another key's value, requested through `compute_opaque(k).projection(p)`; a computation that reads one field of a large value depends on the projection, so a change to other fields does not invalidate it (`dice/dice/docs/projections.md`).
- **User data** is per-transaction data that DICE does not track, such as the event dispatcher that streams events to the client; anything that affects results must not live there (Modern DICE talk).

`DiceComputations` is taken by `&mut`, which is deliberate: Rust then forbids two outstanding `compute` calls on one context, so the only way to request keys concurrently is through the structured combinators `compute2`, `compute_join`, and `compute_many`, each of which gives every branch its own child context (`dice/dice/src/api/computations.rs`).
That restriction is what lets DICE record dependencies as a series-parallel graph: sequential `compute` calls become series edges (the second read may depend on the first value), and a combinator becomes a parallel group.
`with_linear_recompute` is the escape hatch that records everything inside it as sequential, at the cost of serial dependency checking on recompute.

#### 2.2 Transactions and versions

`Dice::updater(branch)` returns a `DiceTransactionUpdater`; the caller reports `changed(keys)` (force-dirty: "what k reads outside the graph has changed") or `changed_to(key, value)` (new injected value), then commits, which mints a new version and returns a `DiceTransaction` (`dice/dice/docs/api.md`, `dice/dice/src/updater.rs`).
In Buck2, each command builds one transaction from the file watcher's events and the current configs.

- A commit whose every change is a no-op mints no version, and transactions with equal state share in-flight work; this is how two concurrent `buck2 build` commands on the same state share one computation of each key (`dice/docs/incrementality.md` section 5.3).
- DICE supports "a multi-commit transaction model, where computations/requests that is currently running do not see newly committed changes" (`dice/dice/docs/incrementality.md`): a running build keeps a consistent snapshot even if you save a file mid-build.
- On main, versions form a forest of branches (`Dice::fork`, `new_root`, `delete_branch` in `dice/dice/src/dice.rs`), written `(branch, seq)`; this is the machinery that lets several checkouts share one engine with `share_dice` (`dice/docs/incrementality.md` section 2.1, as of 2026-11).

#### 2.3 The execution path of one `ctx.compute(&k)`

The Modern DICE talk and `dice/docs/incrementality.md` describe two layers:

1. **The async evaluator**, one per transaction (roughly one per Buck2 command), holds a map from key to `DiceTask`.
   The first request for a key spawns a tokio task and inserts it; every later request in the same transaction gets a shared future for the same task, which is the deduplication guarantee.
2. **The core state**, a single-threaded actor that owns the durable graph and answers messages.
   The Modern DICE talk names this move, from fine-grained locks inside the async evaluator to one state thread, as "one of the major changes with Modern Dice".

The spawned task sends `lookup(k, version)` to the core state, which answers one of three ways:

- **Match:** the key has a value valid at this version; the task completes with it.
- **Compute:** no usable value; the task runs `k.compute(ctx, ...)`, then reports the value and the recorded dependencies back to the core state.
  The core state may hand back a different but equal instance than the one the task produced, and the talk stresses that callers must use the instance the core state returns.
- **CheckDeps:** a previous value exists but was invalidated; the core state returns the old certificate, and the task re-verifies each recorded dependency against the current version.
  If every dependency comes back at its recorded revision, the old value is revalidated without running `compute`; otherwise the task computes.

Cancellation falls out of the same map: the evaluator's map holds only `WeakShared` handles, every requester holds a `Shared`, and when the last requester drops its future the task is aborted; a task still wanted by any other request keeps running (`dice/dice/docs/cancellations.md`).
This is why pressing Ctrl-C in one terminal does not cancel work that a concurrent command on the same state still needs.

#### 2.4 Incrementality: certificates, windows, and reverse dependencies

`dice/docs/incrementality.md` (main, 2026-11) specifies the core state formally.
A completed computation is recorded as a certificate:

```text
Cert {
    key:      Key,
    revision: Revision,               // which distinct value of key was produced
    deps:     Set<(Key, Revision)>,   // what the compute read, and the revision it saw
    epsilon:  Revision,               // revision of key's untracked input ("epsilon token")
}
```

The `epsilon` field models what a key reads outside the graph, such as a file on disk; a force-dirty mints a fresh epsilon, so a dirtied file key must re-read the disk even though no tracked dependency changed.
Revisions are per-key counters, not hashes: interning compares a new value against the stored values with the key's equality function (and first checks whether an identical trace already exists), and reuses the old revision on equality, which is how early cutoff becomes visible to dependents, whose certificates name `(key, revision)` pairs (Appendix A).

Each key's slot on a branch holds at most one claim: "this certificate's revision is valid for sequence numbers `[valid_from, valid_until)`", where an open upper bound means "still valid at the head" (an attached claim).
`commit` walks the branch's reverse-dependency map breadth first from the changed keys and closes the window of every attached dependent, which is the invalidation step; the document proves that the walk reaches every transitive dependent because every attached key's dependency edges are present in the rdep map (Invariant 3).
`dice/dice/docs/incrementality.md` states the resulting cost bound: "O(invalidated subset) traversals and O(changed subset) recomputations for a given request", where both subsets are intersected with the nodes the request might need.
A key outside the changed subset whose window was closed is revalidated by CheckDeps, which costs a dependency walk but no recomputation.

The soundness theorem is deliberately one-directional: if `lookup` says `Valid(r)`, then `r` is justified by the recorded assertions and certificates; `Unknown` is always a sound answer, so forgetting state (eviction, paging, branch deletion) can never make an answer wrong, only slower (section 3.3).
The document also spells out what the engine cannot protect against: if computes are nondeterministic or the environment fails to report a change, answers stay sound relative to what was recorded but no longer match a from-scratch build (section 3.4), which is exactly the class of bug the Modern DICE talk lists as the most common: wrong key equality, and data flowing through a `Mutex` or a lazily initialized `OnceLock` inside a key or value where DICE cannot see it.

#### 2.5 Non-speculative dependency checking

The old DICE checked an invalidated node's dependencies in parallel and stopped at the first change, but that spawned speculative work, and a dependency that is only valid under an earlier dependency's old value could be requested in a state where it no longer makes sense (the talk's example is a key for a deleted file that panicked).
Checking strictly in order fixed that but made recomputation single-threaded.
Modern DICE replays the recorded series-parallel structure: series edges are checked in order and stop at the first changed one, parallel groups are checked concurrently (`dice/docs/incrementality.md` Appendix B).
Because the check never requests a key that the real compute would not also request with the same earlier values, the work done while checking is never wasted, and if a dependency did change, the recompute re-requests the same prefix and finds it already cached.

#### 2.6 Transients, cycles, and paging

- **Transient values.** A value for which `Key::validity` returns false is shared by every request in the current transaction but not cached; the next transaction recomputes it once (`dice/dice/docs/transients.md`).
- **Cycles.** `dice/dice/src/epoch/cycles.rs` finds cycles in the "waits on" graph of pending tasks and reports only strongly connected components with no edge leaving them, because a component that still waits on something outside may resolve itself; each task in a reported cycle gets a cycle error instead of a value.
  The source warns that a computation racing a dependency against something DICE cannot see, such as a timer, can be reported as a false cycle; `docs/concepts/daemon.md` states that cycle detection is always enabled.
- **Paging.** On main, DICE values can be paged out to an on-disk store and read back on demand; the `hydration` Buck settings section (overridable on the command line or in local settings, not a `.buckconfig` section) defaults to `enable_paging = false`, `page_out_on_idle = false`, `page_out_min_free_disk_gb = 100`, `page_out_policy = once_per_value`, `eviction_policy = on_memory_pressure`, and `pagable_storage_backend = sqlite` (`app/buck2_settings/src/hydration.rs`, as of 2026-11).
  `buck2 debug hydration` exists on the 2026-11-01 release, and a `PageIn` entry kind exists in the critical-path schema, so a page-in on the critical path is attributed like any other work.

### 3. One graph instead of phases

`docs/about/why.md` puts it plainly: "Buck2 is not phased - there are no target graph/action graph phases, just a series of dependencies in a single graph on DICE that result in whatever the user requested."
`docs/concepts/architecture.md` still names five stages (evaluation, configuration, analysis, execution, materialization) but adds that "these are the phases that a build goes through, but they are not always sequential."
Concretely, each stage is a family of DICE keys:

| Stage | What a key computes | Invalidated by |
| :--- | :--- | :--- |
| Listing and file ops | Directory listings that define packages; file contents and digests | File watcher events |
| Load | Evaluation of one `BUCK` file or `.bzl` module into a frozen module | The file, any `load()`ed module, relevant buckconfig |
| Unconfigured target node | Rule reference plus coerced attributes, `select()` unresolved | The `BUCK` file evaluation |
| Configured target node | Attributes with `select()` resolved under a target platform, transitions applied | Target node, platform and constraint targets |
| Analysis | The rule's Starlark `impl(ctx)`: providers returned, actions declared | Configured node, dependencies' providers, the rule's `.bzl` |
| Action execution | Run one action locally, remotely, or both (hybrid race) and record output digests | Input digests, command line, environment, executor config |
| Materialization | Place outputs in `buck-out` now or defer until a local consumer needs them | Requests from the client or local actions |

Because the analysis of `//:a` depends only on `//:a`'s configured node and its dependencies' providers, the analysis of one target and the execution of another proceed concurrently whenever their dependencies allow; the critical path in the worked example interleaves `listing`, `load`, `analysis`, `action`, and `materialization` entries in one chain.
Analysis does not read source contents, only paths and attributes, so a content-only edit to a source file invalidates actions and leaves analysis alone; the worked example shows a comment edit running exactly one command.

Remote execution is the default design point: "local execution is considered a special case of remote execution", so directory digests can be precomputed for upload (`docs/about/why.md`).
Buck2 speaks the Bazel Remote Execution API, configured under `[buck2_re_client]` (`engine_address`, `action_cache_address`, `cas_address`, `instance_name`, TLS keys), hashes with SHA256 by default, and accepts `[buck2] digest_algorithms = BLAKE3 | SHA1 | SHA256` (`docs/users/remote_execution.md`); [[Remote-Execution-API-and-Remote-Caching]] owns the protocol.

### 4. Starlark: the language, the interpreter, and the heap

Every Buck2 rule, macro, and toolchain is Starlark; the binary has no built-in language rules, so the C++ rules have no "magic internal features" that other rules lack (`docs/about/why.md`).
The interpreter is `starlark-rust` (`facebook/starlark-rust`, crate version 0.14.2 on main as of 2026-11), an implementation of the Bazel Starlark spec that also exists in Java and Go; it started at Google and moved to Meta at version 0.4.0 (its `README.md`).

- **Deterministic and hermetic by construction.** Starlark has no I/O, no clock, and no unordered iteration, so evaluating a `BUCK` file is a pure function of its inputs, which is what makes it safe to cache as a DICE value.
- **Freezing.** After a module evaluates, `Module::freeze` copies every reachable value into a frozen heap and returns a `FrozenModule`, which is `Clone + Send + Sync`, "so one frozen module is shared by every module that loads it" (`starlark/src/environment/modules.rs`); a `.bzl` file evaluated once serves every `BUCK` file that loads it, on any thread.
  The README states the type-level rule: "frozen values are `Send`/`Sync`, while non-frozen values aren't."
- **Garbage collection.** The unfrozen heap uses a two-space moving collector that handles cycles and costs time proportional to live data; freezing reuses the same copying walk (`docs/gc.md`).
  `Evaluator::garbage_collect` is documented as unsafe to call during evaluation, and Buck2 states that "Starlark is only garbage collected in between top-level statements in the BUCK file, but not garbage collected inside function calls/macros" (`docs/users/faq/starlark_peak_mem.md`), so a macro that allocates in a loop keeps everything until it returns.
- **Peak memory guard.** Buck2 records `starlark_peak_allocated_bytes` for every build file in the `LoadBuildFileEnd` event, and `interpreter_for_dir.rs` fails a build file whose peak exceeds `DEFAULT_STARLARK_MEMORY_USAGE_LIMIT = 2 * (1 << 30)` (2 GiB), overridable per file with `set_starlark_peak_allocated_byte_limit()`.
  The check is gated by `[buck2] check_starlark_peak_memory`, which defaults to false in the source, so the "hard 2GB limit" described in the FAQ applies only where that key is set (as of 2026-11, Both).
- **Types.** Function parameters and returns accept annotations such as `list[str]`, `dict[str, Artifact]`, and `int | None`, checked at runtime, with `record` and `enum` as typed data; `docs/types.md` calls the extension "highly experimental", and Buck2 adds a static checker reachable as `buck2 starlark typecheck`, alongside `buck2 starlark lint` and `buck2 starlark debug-attach` (DAP) (2026-11-01 release help).
- **Deviations.** The README lists extensions beyond the spec (type annotations, recursion, top-level `for`), no `bytes` type yet, and stack overflows on some circular structures.
- **Churn.** The unreleased section of `CHANGELOG.md` rewrites value lifetimes as "brands" and removes `FrozenValue`; embedders pinning `starlark` should expect breaking changes, which the README states is a non-goal to avoid ("We do not aim for API stability between releases").

### 5. Rules, providers, and bounded dynamism

A rule is `rule(impl = f, attrs = {...})`; at analysis time `f(ctx)` receives attributes, dependency provider collections, and `ctx.actions`, declares outputs with `declare_output`, registers actions with `run`, `write`, `copy_file`, and similar, and returns a list of providers that must include `DefaultInfo` (`docs/concepts/architecture.md`).
Providers are the only channel between targets, so analysis is a pure function of a node and its dependencies' providers, which is what makes each analysis a cacheable DICE key.

#### 5.1 Dynamic dependencies: `dynamic_output` and `dynamic_actions`

Static analysis cannot express "compile these OCaml modules in the order `ocamldeps` prints".
`ctx.actions.dynamic_output` defers part of analysis until listed artifacts have been built: its lambda receives the built values (`read_string`, `read_lines`, `read_json`) and must bind every declared output with new actions (`docs/rule_authors/dynamic_dependencies.md`).
The documented uses are distributed ThinLTO (the index says what each backend needs), OCaml (`ocamldeps`), and Erlang headers and BEAM ordering; in the prelude on main, `cxx/dist_lto/dist_lto.bzl`, `ocaml/ocaml.bzl`, `erlang/erlang_build.bzl`, `go/package_builder.bzl`, and `python/prebuilt_python_library.bzl` all use dynamic outputs or dynamic actions.

- Its `inputs` parameter "is accepted but ignored"; only `dynamic` (artifacts to read) and `outputs` (artifacts to bind) matter.
- The source documents a second use: a `dynamic_output` with an empty `dynamic` list defers expensive analysis, because "the `dynamic_output`'s function will not be run unless one of the actions it outputs is actually requested as part of the build" (`dynamic_output.rs`).
- The newer, typed form, described in source as "the new version of `dynamic_output` [that] will eventually replace it", is a factory `dynamic_actions(impl = f, attrs = {...})` with attribute kinds `dynattrs.output()`, `artifact_value()`, `dynamic_value()`, `value(T)`, `list`, `dict`, `tuple`, and `option`, invoked through `ctx.actions.dynamic_output_new(...)`, which returns a `DynamicValue` that other dynamic actions can consume (`dynamic_output.rs`, `attrs_starlark.rs`).
- In the critical-path schema the lambda is a `DynamicAnalysis` entry, a DICE key that depends on artifact contents and produces actions, which is exactly a monadic bind placed between two action layers.

Buck2 deliberately stops short of full generality.
`docs/rule_authors/anon_targets.md`: "Dynamic dependencies, in their full generality, enable users to do a thing, look at the result, then ask for fresh things."
"However, this full generality is not provided as it breaks processes, like query, that power the Target Determinator."
The target graph stays static and queryable; only the action graph below a target can depend on built contents.

#### 5.2 Anonymous targets

An anonymous target is identified "by the hash of its attributes, rather than its name", so two unrelated rules that ask for the same anon target with the same attributes share one analysis and its actions (`docs/rule_authors/anon_targets.md`).
It solves the sharing problem (two binaries compile shared code once even though the user graph has no common node) and the overlay problem (a shadow graph keyed by an existing target, Bazel's aspect use case).
The doc calls anon targets "the version of `dynamic_output` at analysis time": a rule can create a new analysis after inspecting providers.
The calling target's execution platform is part of the hash unless `exec_group` selects another, and only a subset of attribute types is allowed (`bool`, `int`, `str`, `enum`, already-configured `dep`, `source`, `label`, and `arg` only when declared `anon_target_compatible = True`).

#### 5.3 Transitive sets, dep files, incremental actions, content-based paths

- **Transitive sets** (`transitive_set()`, `ctx.actions.tset`) are DAGs of values with projections into command lines (`project_as_args`) or JSON (`project_as_json`) and traversal orders `preorder`, `postorder`, `topological`, `bfs`, and `dfs`; unlike Bazel's `depset`, a projection is a node in the dependency graph, so an action that depends on a link line depends on one projection node, not on every transitive artifact (`docs/rule_authors/transitive_sets.md`); the critical-path schema has an `EnsureTransitiveSetProjection` entry for it.
- **Dep files** let an action report which declared inputs it actually read, so a change to an unused header does not rerun it; they "only work if a previous invocation of the command is known to your Buck2 daemon" and are dropped on daemon restart or `buck2 debug flush-dep-files` (`docs/rule_authors/dep_files.md`); the C++ prelude uses them for headers (`prelude/cxx/compile.bzl`).
- **Incremental actions** opt out of output cleanup (`no_outputs_cleanup`) and receive a JSON metadata file of input paths and digests (`metadata_env_var`, `metadata_path`), so a packager or incremental linker can update its previous output (`docs/rule_authors/incremental_actions.md`).
- **Content-based paths** replace the configuration hash in an output path with the first 8 bytes of the output's digest, so an action that does not vary across configurations has one action digest and is deduplicated across configurations; the action sees inputs at real content paths and outputs at an `output_artifacts` placeholder (`docs/rule_authors/content_based_paths.md`).
  The worked example shows this layout even though its rules never opted in: inputs at `__a__/b6a98d9ce9a2d914/a.norm`, outputs at `__a__/output_artifacts/a.norm`, and `--show-output` returning a configuration-hash path that is a symlink into the content directory (as of 2026-11, macOS, 2026-11-01 release).

### 6. BXL: Starlark that drives the daemon

BXL (`.bxl` files) is "a Starlark-based script that enables integrators to inspect and interact with the Buck2 graph" at the unconfigured, configured, provider, and action stages (`docs/bxl/index.md`).
A script declares entry points with `bxl_main(impl = f, cli_args = {...})`, where `cli_args.bool()`, `int()`, `string()`, `list()`, `option()`, and `target_label()` define a typed command line, and runs as `buck2 bxl //path/file.bxl:name -- --arg value`; `-- --help` prints the generated usage (`docs/bxl/how_tos/basic_how_tos.md`, confirmed on the 2026-11-01 release).

The context `ctx: bxl.Context` exposes:

- **Queries:** `ctx.uquery()`, `ctx.cquery()`, `ctx.aquery()`, each with typed methods (`deps`, `rdeps`, `owner`, ...) and `eval("<query string>")`, returning target sets that support `+` and `-`.
- **Nodes and attributes:** `ctx.configured_targets(...)`, `ctx.unconfigured_targets(...)`, `node.get_attr`, `get_attrs`, `has_attr`, and `rule_type`; `attrs_eager` and `attrs_lazy` are documented as deprecated.
- **Analysis:** `ctx.analysis(configured)` returns the providers a rule returned, without building.
- **Build and ensure:** `ctx.build(...)` returns `dict[Label, bxl.BuildResult]` and blocks; `ctx.output.ensure(artifact)` schedules materialization after the script finishes and is the non-blocking way to request outputs.
- **Actions:** `ctx.bxl_actions(exec_deps = ..., toolchains = ..., target_platform = ..., exec_compatible_with = ...)` gives an `actions` factory equivalent to a rule's, which requires execution platforms to be configured; `bxl.dynamic_actions` and `bxl.anon_rule` mirror the rule APIs with a `bxl.Context` as the context.
- **Output:** `ctx.output.print` and `print_json` write results to stdout, and are replayed when the script is served from cache; plain `print()` goes to stderr and does not appear on a cached run (`docs/bxl/faq.md`).

#### 6.1 How BXL is cached

"The entire BXL script is represented as a single node on the DICE graph", keyed by the script and its arguments; if any recorded call (a uquery, cquery, analysis, or build) changes, the whole script reruns, while each underlying computation remains incrementally cached (`docs/bxl/faq.md`, `docs/rfcs/implemented/bxl.md`).
The RFC states the consequence: "the entire bxl will be reran, with no early cutoff", and recommends moving heavy computation into actions or into BXL anon targets (`bxl.anon_rule` resolved with `ctx.resolve`), which are cached by attributes, target platform, and modifiers and can be shared across different BXL commands (`docs/bxl/how_tos/how_to_cache_and_share_operations.md`).
Ensured artifacts are cached separately as actions outside the BXL node, and an ensured artifact's path is deliberately not available during the script, to stop absolute paths leaking into remote actions (`docs/bxl/faq.md`).

#### 6.2 BXL cquery is not CLI cquery

`buck2 cquery 'rdeps(root//path/..., X, 1)'` builds a target universe from both patterns, so `X` resolves to every configuration in which it appears; `ctx.cquery().rdeps("root//path/...", "X", 1)` configures each argument independently with the default platform, so `X` resolves only to its default configuration and the result can be empty (`docs/bxl/explanation/bxl_cquery_vs_cli_cquery.md`).
To match the CLI, build `ctx.target_universe([...])` and look the arguments up in it, or call `ctx.cquery().eval("rdeps(...)")`.

#### 6.3 What BXL is used for

The prelude ships BXL as the IDE integration layer (main, as of 2026-11):

- `prelude//cxx/tools/compilation_database.bxl:generate` writes a `compile_commands.json` for clangd and clang-tidy (`docs/users/how_tos/compilation_database.md`).
- `prelude/rust/rust-analyzer/resolve_deps.bxl` and `check.bxl` back the `rust-project` tool that the release ships beside `buck2`.
- `prelude//python/typecheck/batch.bxl:run` batch-typechecks Python targets (`docs/users/languages/python/typechecking.md`).
- `prelude/go/tools/gopackagesdriver/driver.bxl` implements `gopackagesdriver` for gopls.
- `prelude/ide_integrations/visual_studio/` generates Visual Studio solutions, and `prelude/ide_integrations/apple/build_index/` builds Swift index stores.

### 7. Critical-path profiling

Every command writes a zstd-compressed protobuf event log of span start, span end, and instant events (schema `app/buck2_data/data.proto`), readable as JSONL with `buck2 log show` (`docs/users/build_observability/logging.md`).
At the end of a build the daemon runs a critical-path backend over the graph of what it computed; `[buck2] critical_path_backend2` defaults to `longest-path-graph`, with `logging` as the other value (`app/buck2_server/src/ctx.rs`, `app/buck2_build_signals/src/env.rs`).

`compute_critical_path_potentials` in `app/buck2_critical_path/src/potential.rs` does, with each pair of steps on two threads:

1. Reverse the dependency graph and topologically sort it.
2. Compute the longest weighted path to every node from a source and from every node to a sink.
3. Take the node with the largest cost from a source and walk predecessors back to recover the critical path.
4. For each node on it, compute the critical-path cost if that node took zero time, which is the `potential` column: how much the build would shrink before that node stops being critical.

The walk is iterative with an explicit stack because "a chain of vertices can be far deeper than the thread's stack allows" (`potential.rs`), and the whole computation is O(V + E).

The entry kinds in `CriticalPathEntry2` (`data.proto`) are `Analysis`, `AnonAnalysis`, `DynamicAnalysis`, `ActionExecution`, `TestExecution`, `TestListing`, `Install`, `FinalMaterialization`, `ComputeCriticalPath`, `Load`, `Listing`, `EnsureTransitiveSetProjection`, `PageIn`, `Waiting`, and `GenericEntry`; that loading and listing appear in the same path as actions is the single graph made visible.
Each entry carries `total_duration`, `user_duration` (what the user can improve), `potential_improvement_duration`, `queue_duration`, `non_critical_path_duration`, `start_offset_ns`, and `was_reused`, and an action entry carries its `action_digest` and `invalidation_info`.

`buck2 log critical-path` prints the path from the last command (or `--recent N`, `--trace-id ID`) in `readable` (default), `tabulated`, `json`, or `csv` format, with all durations in microseconds; entries reused from a previous command are marked `(reused)` (`app/buck2_cmd_log_client/src/critical_path.rs`).
`buck2 log slowest-path` prints the slowest path instead, "closer to what the build actually spends its time on" because it includes overhead.
Related tools on the 2026-11-01 release: `buck2 log what-ran`, `what-failed`, `what-materialized`, `what-uploaded`, `summary`, `diff`, `chrome-trace`; `buck2 profile loading | analysis | bxl` for Starlark profiles; and `buck2 debug dice-dump`, `allocative`, `heap-dump`, and `allocator-stats` for the daemon's memory.
[[Build-Performance-Economics-and-Decision-Record]] owns how to turn a critical path into a refactoring plan.

### 8. Against Bazel: evaluation model, memory, and daemon

[[Bazel-Skyframe-Bzlmod-and-Execution-Model]] owns Skyframe; this section compares mechanisms only.
No primary source fetched for this note publishes a controlled Buck2-versus-Bazel benchmark of memory or latency on the same graph, so this comparison is architectural, and every number is attributed to its source.

| Concern | Buck2 (DICE) | Bazel (Skyframe) |
| :--- | :--- | :--- |
| Missing dependency during a compute | The Rust future suspends at `.await` and resumes when the dependency is ready | `env.getValue` returns `null`, the `SkyFunction` returns `null`, and is invoked again once the dependency is evaluated (bazel.build/reference/skyframe) |
| Cost model of discovery | No re-execution of the prefix; series-parallel structure recorded for recheck | Each restart re-runs the function body up to the missing dependency; `env.getValues()` requests several dependencies at once and evaluates them in parallel |
| Phases | Single graph; stages overlap per target | Loading and analysis, then execution; the Bazel 7 release blog describes Project Skymeld as "removing the boundary between the analysis and execution phases" |
| Early cutoff | Key equality at every node, including actions | Change pruning: "resurrected" nodes when a rebuilt value is unchanged (bazel.build/reference/skyframe) |
| Dynamic dependencies for rules | `dynamic_output`, `dynamic_actions`, anon targets | None documented in the Bazel pages fetched for this note; aspects cover overlays; Skyframe itself is dynamic internally |
| Rule language | All rules in Starlark, including C++ | Starlark rules, with some rules still native Java (UNVERIFIED: not checked against a fetched Bazel 9 page) |
| Runtime and memory | Rust with jemalloc; `why.md` cites the "absence of GC pauses" as Rust's advantage and Java's "better memory profiling tools" as its cost; tools `allocative`, `debug heap-dump` | JVM; heap capped with a startup flag such as `--host_jvm_args=-Xmx2g` (bazel.build/advanced/performance/memory) |
| Shedding memory | `hydration.enable_paging` setting (main, off by default) | `--discard_analysis_cache`, `--notrack_incremental_state`, `--nokeep_state_after_build` |
| Daemon scope | One per project root and isolation dir; optional shared daemon on Linux | "a long-lived server process" per output base, idle exit after 3 hours by default (`--max_idle_secs`) (bazel.build/run/client-server) |
| Concurrency of commands | Parallel when state is equal, else serialized or preempted | "Each server can handle at most one invocation at a time" (bazel.build/run/client-server) |
| Cache that survives a daemon restart | Remote action cache only; dep files and DICE state are lost; `sqlite_materializer_state` remembers what is on disk but does not skip execution | Local on-disk action cache: after `--discard_analysis_cache` "the on-disk action cache prevents most re-execution" (bazel.build/advanced/performance/memory) |
| Remote execution and output downloads | REAPI; deferred materialization when RE is used | REAPI; Build without the Bytes default `--remote_download_outputs=toplevel` since Bazel 7 |

Buck2's own published numbers are against Buck1, not Bazel: "2x as fast as Buck1" in internal tests (`docs/about/why.md`), CI builds "2-4x faster", and CI target determination "25% faster at P50 (moving to 40% faster at P95) with 25% less memory" (`docs/about/benefits/compared_to_buck1.md`).
Treat these as vendor-reported Meta internal results, not as measurements reproduced here.

## Commands and Numbers to Know

| Item | Value | Platform and date | What it controls |
| :--- | :--- | :--- | :--- |
| Release cadence | Date tags on the 1st and 15th, all marked prerelease | Both (as of 2026-11) | Pin `buck2` with a date tag (and a prelude hash); there is no semver |
| `buck2 --version` (2026-11-01 asset) | `buck2 2026-10-31-e8edae33cd7c...` | macOS arm64 (as of 2026-11) | The build the worked example ran |
| Daemon directory | `~/.buck/buckd/<project path>/<isolation dir>` | macOS (as of 2026-11) | Printed by `buck2 debug daemon-dir` |
| Default isolation dir | `v2` | Both (as of 2026-11) | Name of `buck-out/v2`; `--isolation-dir` runs another daemon |
| `[buck2] file_watcher` | `notify` (OSS), `watchman`, `edenfs`, `fs_hash_crawler` | Both (as of 2026-11) | How changed files reach DICE |
| `[buck2] share_daemon`, `share_dice` | `false`, `false` | Linux only for sharing (as of 2026-11, main) | Multi-tenant daemon and shared DICE engine |
| `--preemptible` | `never` (default), `always`, `ondifferentstate` | Both (as of 2026-11) | Whether a newer, incompatible command kills this one |
| `--exit-when` | `never` (default), `differentstate`, `notidle` | Both (as of 2026-11) | Fail fast instead of waiting for the daemon |
| Exit codes 4 and 5 | `DaemonIsBusy`, `DaemonPreempted` | Both (as of 2026-11) | Scriptable signals for the two flags above |
| `[buck2] digest_algorithms` | `SHA256` default; `BLAKE3`, `SHA1` accepted | Both (as of 2026-11) | Must match the REAPI backend |
| `[buck2_re_client]` keys | `engine_address`, `action_cache_address`, `cas_address`, `instance_name`, `tls_ca_certs`, `tls_client_cert`, `http_headers` | Both (as of 2026-11) | Remote execution endpoints |
| `[buck2] sqlite_materializer_state` | off unless set | Both (as of 2026-11) | Remember on-disk outputs across restarts |
| `[buck2] defer_write_actions`, `clean_stale_enabled` | off unless set | Both (as of 2026-11) | Deferred writes; `buck2 clean --stale` |
| `[buck2] check_starlark_peak_memory` | `false` in source | Both (as of 2026-11) | Enables the per-build-file Starlark peak check |
| `DEFAULT_STARLARK_MEMORY_USAGE_LIMIT` | 2 GiB per build file | Both (as of 2026-11) | Override with `set_starlark_peak_allocated_byte_limit()` |
| `[buck2] critical_path_backend2` | `longest-path-graph` (default), `logging` | Both (as of 2026-11) | Backend that computes the critical path |
| `buck2 log critical-path --format` | `readable` (default), `tabulated`, `json`, `csv`; durations in microseconds | Both (as of 2026-11) | Critical path of the last or a chosen command |
| `retained_event_logs` | `12` in the daemon startup config | macOS (as of 2026-11, observed in `buck2 status`) | How many command logs are kept |
| `hydration.enable_paging` (Buck settings section, set on the command line or in local settings) | `false`; backend `sqlite`; min free disk 100 GiB for idle page-out | Both (as of 2026-11, main) | DICE value paging to disk |
| Content-based path hash | First 8 bytes of the output digest (16 hex digits) | Both (as of 2026-11) | Output dedup across configurations |
| `dynamic_output(inputs = ...)` | Accepted and ignored | Both (as of 2026-11) | Pass `[]` or omit |
| Bazel server idle exit | 3 hours (`--max_idle_secs`) | Both (as of 2026-11) | Bazel comparison only |

## Multi-Language Comparison: C++ / Python / Rust

Buck2's core is language agnostic, so the language-specific mechanisms live in the prelude; this table maps each mechanism in this note to the prelude code that uses it on main (as of 2026-11).

| Concern | C++ (`prelude/cxx`) | Python (`prelude/python`) | Rust (`prelude/rust`) |
| :--- | :--- | :--- | :--- |
| Dynamic dependencies | Distributed ThinLTO plans and backends through `dynamic_output` (`cxx/dist_lto/dist_lto.bzl`) | `prebuilt_python_library.bzl` infers C++ header directories with `dynamic_output` | Not used on the crate path; dependency structure is static per crate |
| Avoiding unused-input reruns | Dep files for headers (`cxx/compile.bzl`) | Not applicable to interpreted sources | The prelude defines `metadata-fast` and `metadata-full` emit kinds (`rust/build_params.bzl`); whether a dependent's metadata input survives a body-only edit depends on what rustc puts in `.rmeta` (UNVERIFIED here; owned by the Cargo note) |
| Pipelining | Not applicable | Not applicable | `compared_to_buck1.md` says "Rust has experimental pipelining support"; metadata outputs are what pipelining feeds to dependents |
| IDE integration through BXL | `compilation_database.bxl` for clangd and clang-tidy | `typecheck/batch.bxl` for batch type checking | `rust-analyzer/resolve_deps.bxl` behind the `rust-project` binary |
| Transitive data | `LinkInfosTSet` (`linking/link_info.bzl`), `CPreprocessorTSet` (`cxx/preprocessor.bzl`), `CxxFlagsTSet` | `PythonLibraryManifestsTSet` and `NativeDepsInfoTSet` (`python/python.bzl`) | Transitive crate deps projected into command lines (`project_as_args` in `rust/build.bzl`) |

[[Cargo-Build-Graph-and-Rustc-Pipelining]] owns `.rmeta` pipelining, and [[Cpp-Modules-in-Build-Systems]] owns the dynamic-dependency problem of C++20 modules.

## Worked Lab Example

This note has no harness lab.
The example in this section is a complete, self-contained Buck2 project; no file outside this note is needed to reproduce it.
Every output shown was captured once on darwin-arm64 on 2026-11-14 with the `2026-11-01` release binary of Buck2, with `HOME` pointed at a scratch directory so the daemon state stayed isolated (as of 2026-11, macOS).
Timestamps, build IDs, and absolute paths in the captured output are scrubbed or elided as marked.
It uses no prelude: `.buckconfig` maps `prelude` to an empty `prelude/prelude.bzl`, and the three rules are plain Starlark over `ctx.actions` running `/bin/sh`.

Create an empty directory, enter it, and create these files.
`.buckroot` and `prelude/prelude.bzl` are empty.
`a.txt` holds `# header comment v1` then `alpha`, `b.txt` holds `bravo`, `c.txt` holds `charlie`, and `manifest.in` holds `a` then `c`, one per line.

```ini
# .buckconfig
[cells]
root = .
prelude = prelude

[buildfile]
name = BUCK
```

```python
# BUCK
load(":defs.bzl", "concat", "normalized", "selected")

normalized(name = "a", src = "a.txt")
normalized(name = "b", src = "b.txt")
normalized(name = "c", src = "c.txt")

concat(name = "report", deps = [":a", ":b", ":c"])

selected(name = "picked", deps = [":a", ":b", ":c"], manifest_src = "manifest.in")
```

```python
# defs.bzl
# Minimal rules with no prelude: every rule is plain Starlark over ctx.actions.

def _normalized_impl(ctx: AnalysisContext) -> list[Provider]:
    # Strip comment lines, so a comment-only edit yields byte-identical output.
    out = ctx.actions.declare_output(ctx.label.name + ".norm")
    ctx.actions.run(
        cmd_args(["/bin/sh", "-c", 'grep -v "^#" "$1" > "$2"', "--", ctx.attrs.src, out.as_output()]),
        category = "normalize",
    )
    return [DefaultInfo(default_output = out)]

normalized = rule(impl = _normalized_impl, attrs = {"src": attrs.source()})

def _concat_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.label.name + ".txt")
    ins = [d[DefaultInfo].default_outputs[0] for d in ctx.attrs.deps]
    ctx.actions.run(
        cmd_args(["/bin/sh", "-c", 'out="$1"; shift; cat "$@" > "$out"', "--", out.as_output(), ins]),
        category = "concat",
    )
    return [DefaultInfo(default_output = out)]

concat = rule(impl = _concat_impl, attrs = {"deps": attrs.list(attrs.dep())})

# Dynamic dependencies: which inputs the final action reads is decided by the
# *contents* of a manifest artifact, known only after it is built.
def _select_impl(actions: AnalysisActions, manifest: ArtifactValue, inputs: dict[str, Artifact], out: OutputArtifact) -> list[Provider]:
    wanted = [line.strip() for line in manifest.read_string().splitlines() if line.strip()]
    chosen = [inputs[name] for name in wanted]
    actions.run(
        cmd_args(["/bin/sh", "-c", 'out="$1"; shift; cat "$@" > "$out"', "--", out, chosen]),
        category = "select",
    )
    return []

_select = dynamic_actions(
    impl = _select_impl,
    attrs = {
        "manifest": dynattrs.artifact_value(),
        "inputs": dynattrs.value(dict[str, Artifact]),
        "out": dynattrs.output(),
    },
)

def _selected_impl(ctx: AnalysisContext) -> list[Provider]:
    manifest = ctx.actions.declare_output("manifest.txt")
    ctx.actions.run(
        cmd_args(["/bin/sh", "-c", 'cp "$1" "$2"', "--", ctx.attrs.manifest_src, manifest.as_output()]),
        category = "manifest",
    )
    inputs = {d.label.name: d[DefaultInfo].default_outputs[0] for d in ctx.attrs.deps}
    out = ctx.actions.declare_output(ctx.label.name + ".txt")
    ctx.actions.dynamic_output_new(_select(manifest = manifest, inputs = inputs, out = out.as_output()))
    return [DefaultInfo(default_output = out)]

selected = rule(impl = _selected_impl, attrs = {"deps": attrs.list(attrs.dep()), "manifest_src": attrs.source()})
```

```python
# tools.bxl
def _deps_impl(ctx: bxl.Context):
    # cli_args.target_label() yields an unconfigured TargetLabel; analysis needs a configured node.
    target = ctx.configured_targets(ctx.cli_args.target)
    # Configured graph: transitive deps of the target, in one DICE-cached query.
    deps = ctx.cquery().deps(target)
    for node in deps:
        ctx.output.print("dep {} rule={}".format(node.label.raw_target(), node.rule_type))
    # Analysis: read the providers the rule returned, without building.
    providers = ctx.analysis(target).providers()
    ctx.output.print("default_outputs={}".format(len(providers[DefaultInfo].default_outputs)))
    # Build and materialize the default output, then print its path.
    # ctx.build returns dict[Label, bxl.BuildResult]; ensure() schedules materialization.
    for label, result in ctx.build(target).items():
        for artifact in result.artifacts():
            ctx.output.print("built {} -> {}".format(label, ctx.output.ensure(artifact).rel_path()))

deps = bxl_main(
    impl = _deps_impl,
    cli_args = {"target": cli_args.target_label()},
)
```

Run the steps in this section from that directory with the 2026-11-01 release `buck2` binary on the path.
Use `buck2 kill` at the end to stop the daemon.

`BUCK` declares `normalized` targets `:a`, `:b`, `:c` over the three text files; `concat(name = "report")` cats all three; and `selected(name = "picked")` copies `manifest.in` into an artifact and passes it to `_select` through `ctx.actions.dynamic_output_new`.

### Step 1: cold build and critical path

```text
$ buck2 build //:report //:picked --show-output --console=simple
[2026-11-14T21:10:25.743-05:00] Starting new buck2 daemon...
[2026-11-14T21:10:25.922-05:00] Connected to new buck2 daemon.
[2026-11-14T21:10:25.934-05:00] Build ID: 75187a26-ebd8-445f-b4eb-2b55c0ecf6b6
[2026-11-14T21:10:25.998-05:00] Cache hits: 0%
[2026-11-14T21:10:25.998-05:00] Commands: 6 (cached: 0, remote: 0, local: 6)
BUILD SUCCEEDED
root//:picked buck-out/v2/art/root/1ef78538d8598cb2/__picked__/picked.txt
root//:report buck-out/v2/art/root/1ef78538d8598cb2/__report__/report.txt
```

`buck2 log what-ran` listed the six commands: `manifest`, `normalize` for `:a`, `:c`, and `:b`, then `select` and `concat`; the `select` command line read `buck-out/v2/art/root/__a__/b6a98d9ce9a2d914/a.norm` and `__c__/999d1d048ee91232/c.norm` and wrote `__picked__/output_artifacts/picked.txt`, the content-based layout of section 5.3.

```text
$ buck2 log critical-path
 start_offset      total    waiting       user  potential kind name category identifier execution_kind
            0       9783          0          0          0 buckd_command_init
         9783        886          0          0          0 file-watcher-wait
        10669       2503          0          0          0 other-command-start-overhead
        13173          0      18765          0          0 waiting unknown
        31938        119                   119        119 listing root//
        32058          0       9732          0          0 waiting unknown
        41790        330                   330        330 load root//
        42121          0       1853          0          0 waiting for_deps
        43975          0        341          0          0 waiting unknown
        44316          0                     0          0 configure_target
        44316          0        402          0          0 waiting unknown
        44718       5274                  5274          1 analysis root//:c (<unspecified>)
        49992          0         29          0          0 waiting unknown
        50022        387                   387         67 analysis root//:picked (<unspecified>)
        50409          0        813          0          0 waiting unknown
        51223      13112                  8406         19 action root//:c (<unspecified>) normalize  ACTION_EXECUTION_KIND_LOCAL
        64336          0         95          0          0 waiting unknown
        64431       7862                  6709         67 action root//:picked (<unspecified>) select  ACTION_EXECUTION_KIND_LOCAL
        72294          0         66          0          0 waiting unknown
        72361          0                     0          0 build_key
        72361          0          4          0          0 waiting unknown
        72365          0        247          0          0 waiting materializer_prepare
        72612        256                   256         67 materialization root//:picked (<unspecified>)  picked.txt
        73437        322          0          0          0 compute-critical-path
```

(Trailing spaces trimmed; the "Showing critical path from" header line is omitted.)

### Step 2: comment-only edit, then a manifest edit

```text
$ edit a.txt: change only the comment line
$ buck2 build //:report //:picked --console=simple
[2026-11-14T21:10:26.041-05:00] File changed: a.txt
[2026-11-14T21:10:26.050-05:00] Commands: 1 (cached: 0, remote: 0, local: 1)
BUILD SUCCEEDED
$ buck2 log what-ran
build	root//:a (<unspecified>) (normalize)	local	env -C ... /bin/sh -c 'grep -v "''^#" "$1" > "$2"' -- ./a.txt buck-out/v2/art/root/__a__/output_artifacts/a.norm

$ edit manifest.in: select b instead of c
$ buck2 build //:picked --show-output --console=simple
[2026-11-14T21:10:26.079-05:00] File changed: manifest.in
[2026-11-14T21:10:26.096-05:00] Commands: 2 (cached: 0, remote: 0, local: 2)
BUILD SUCCEEDED
$ buck2 log what-ran
build	root//:picked (<unspecified>) (manifest)	local	env -C ... /bin/sh -c 'cp "$1" "$2"' -- ./manifest.in buck-out/v2/art/root/__picked__/output_artifacts/manifest.txt
build	root//:picked (<unspecified>) (select)	local	env -C ... -- buck-out/v2/art/root/__picked__/output_artifacts/picked.txt buck-out/v2/art/root/__a__/b6a98d9ce9a2d914/a.norm buck-out/v2/art/root/__b__/5da8f23decf397b1/b.norm
```

(Abridged: Build ID lines dropped and the `env -C` environment prefix elided as `...`.)

### Step 3: daemon restart, then BXL twice

After `buck2 kill`, rebuilding `//:picked` reported `Commands: 4 (cached: 0, remote: 0, local: 4)` and the configuration-hash path `buck-out/v2/art/root/1ef78538d8598cb2/__picked__/picked.txt` held `alpha` and `bravo`.
The BXL script `tools.bxl:deps` takes `--target`, configures it with `ctx.configured_targets`, prints `ctx.cquery().deps(...)`, reads `DefaultInfo` from `ctx.analysis(...)`, and builds the target.

```text
$ buck2 bxl //tools.bxl:deps --console=simple -- --target //:report
dep root//:c rule=root//defs.bzl:normalized
dep root//:b rule=root//defs.bzl:normalized
dep root//:a rule=root//defs.bzl:normalized
dep root//:report rule=root//defs.bzl:concat
default_outputs=1
built root//:report (<unspecified>) -> <ensured <build artifact report.txt bound to root//:report (<unspecified>)>>
[2026-11-14T21:11:02.500-05:00] Commands: 4 (cached: 0, remote: 0, local: 4)
BXL SUCCEEDED

$ buck2 bxl //tools.bxl:deps --console=simple -- --target //:report
dep root//:c rule=root//defs.bzl:normalized
dep root//:b rule=root//defs.bzl:normalized
dep root//:a rule=root//defs.bzl:normalized
dep root//:report rule=root//defs.bzl:concat
default_outputs=1
built root//:report (<unspecified>) -> <ensured <build artifact report.txt bound to root//:report (<unspecified>)>>
BXL SUCCEEDED
$ buck2 log what-ran
```

(Build ID and daemon-connect lines dropped; the second `what-ran` printed only its header, meaning zero commands.)

### Reading the result

- **The critical path is one chain across all stages.** `listing`, `load`, `configure_target`, `analysis`, `action`, and `materialization` appear in a single path, which is the "not phased" claim made observable; the 73 ms build reached its first listing at 31.9 ms, of which 13.2 ms was `buckd_command_init`, `file-watcher-wait`, and command start overhead on a freshly started daemon and 18.8 ms was an unattributed `waiting` entry.
- **`potential` is what zeroing a node would save.** The `select` action has `total` 7,862 us but `potential` 67 us, because a sibling path is nearly as long; speeding it up barely moves the build, which is the trap the column exists to expose.
- **Early cutoff at the action level.** After the comment edit only `normalize` for `:a` ran; `a.norm` kept the same content hash (`b6a98d9ce9a2d914` before and after), so `concat` and `select`, whose inputs did not change, were revalidated rather than rerun, and no analysis ran because analysis does not read source contents.
- **A dynamic dependency reruns only its own lambda.** Changing the manifest reran the `manifest` copy and the `select` action, and the new `select` command read `b.norm` instead of `c.norm`: the action's inputs were chosen by artifact contents, which a static action graph cannot express.
- **Daemon state is the cache.** After `buck2 kill` the same outputs were rebuilt with four local commands, and the first BXL run on a fresh daemon ran four more, because without remote execution there is no persistent action cache and the in-memory DICE state was discarded (default config, no RE, as of 2026-11, macOS).
- **A cached BXL run executes nothing and still prints.** The second BXL run printed the same lines and ran zero commands: the whole script was one DICE node whose recorded dependencies were unchanged, and `ctx.output.print` output is replayed from the cached result.
- **The `<ensured ...>` text is not a path.** Formatting an ensured artifact into a string yields a placeholder, because the artifact is materialized only after the script returns; pass the ensured artifact itself to `ctx.output.print` to get its path (`docs/bxl/faq.md`).
- The two BXL mistakes hit while writing the script are themselves instructive: `ctx.build(...)` returns `dict[Label, bxl.BuildResult]`, so `.artifacts()` must be called on each value, and `ctx.analysis` rejects the unconfigured `TargetLabel` that `cli_args.target_label()` produces.

## Pitfalls

> [!warning] Pitfall: untracked data flow inside a DICE key or value
> A `Mutex`, `OnceLock`, or lazily computed field inside a key or value lets data reach a computation without DICE recording a dependency, so an incremental build can return a stale value that a clean build would not.
> The Modern DICE talk names this and wrong key equality as the bugs the Buck2 team hit most; anything that affects results must enter the graph as a key, an injected leaf, or a recorded dependency, never as user data.

> [!warning] Pitfall: equality that compares identity across commands
> A configured target label identifies one node within a command, but DICE compares keys and values across commands, where the same label can name a new node.
> An equality function that says "same label, so equal" makes DICE resurrect a stale value; equality must compare the content that dependents read.

> [!warning] Pitfall: buckconfig churn invalidates nearly everything
> Buckconfig inputs are injected leaves read by almost every key, so a workflow that flips a config value between commands throws away most of the graph each time.
> Keep per-command variation in target platforms and modifiers, or use a separate `--isolation-dir` for a second configuration that you switch to often.

> [!warning] Pitfall: Starlark garbage is not collected inside a macro
> Collection runs only between top-level statements of a `BUCK` file, so a macro that builds large temporary lists in a loop holds all of them until it returns; with many evaluation threads this can exhaust memory.
> Hoist allocations out of loops, avoid repeated `dict.items()` and `dict.keys()` on large dicts in hot loops, and enable `[buck2] check_starlark_peak_memory` so the 2 GiB per-file guard actually fires.

> [!warning] Pitfall: expecting BXL to be incremental inside the script
> A BXL script is one DICE node with no early cutoff; any change to anything it queried or built reruns the entire script.
> Move expensive pure Starlark into `bxl.anon_rule` anon targets or into actions, and keep the BXL body a thin orchestrator.

> [!warning] Pitfall: BXL cquery without a target universe
> `ctx.cquery().rdeps(universe, x)` configures `x` on its own, so rdeps across configurations can come back empty where `buck2 cquery` finds results.
> Build `ctx.target_universe([...])` and look arguments up in it, or use `ctx.cquery().eval(...)` for CLI semantics.

> [!warning] Pitfall: globbing `buck-out` instead of asking Buck2
> With content-based paths, `buck-out/v2/art/root/__picked__/` held three `picked.txt` files in the worked example, one of them stale content from before the manifest edit (`alpha`, `charlie`), plus a configuration-hash symlink to the current one.
> Read output locations from `--show-output`, `buck2 targets --show-output`, or BXL `ensure`, never from `find buck-out`.

> [!warning] Pitfall: assuming a restart keeps your incremental state
> DICE state and dep files live in the daemon, so `buck2 kill`, a version bump, or a crash makes the next build re-execute everything that is not in a remote action cache.
> `sqlite_materializer_state` avoids re-downloading outputs already on disk, but it does not skip execution; for local-only setups, budget for cold rebuilds after restarts or run a remote cache.

> [!warning] Pitfall: reaching for full dynamism
> `dynamic_output` cannot add target-graph edges; a rule that needs to "discover" new targets from file contents cannot be expressed, by design, because query and target determination must see a static target graph.
> Model the discovered structure as actions under a fixed target, or generate `BUCK` files ahead of the build.

## At Scale

A single developer mostly sees Buck2 through the daemon: fast no-op builds, a cold rebuild after `buck2 kill`, and a critical path that is mostly startup overhead.
The trade-offs change as the graph, the team, and the CI fleet grow.

- **Graph memory lives in one process.** DICE keeps every computed node for the current state in the daemon, so peak RSS grows with the transitive closure a developer builds, not with the repository.
  Measure with `buck2 status` snapshots, `buck2 debug allocator-stats` (track `allocated`, not RSS, because jemalloc decommits pages 10 to 30 seconds after a build, per `docs/developers/perf/memory.md`), and `buck2 debug allocative` to find which DICE keys retain memory.
- **Starlark is part of the memory budget.** On large servers with 64 or more hardware threads, many concurrent build-file evaluations without intra-macro GC is what the Buck2 FAQ calls out as a source of OOMs; turn on the per-file peak check and treat `starlark_peak_allocated_bytes` regressions in `LoadBuildFileEnd` events as build-health alerts.
- **Remote execution turns a daemon restart from expensive to cheap.** Meta builds are "always hooked up to remote execution" (`docs/about/why.md`); outside Meta, a REAPI action cache (EngFlow, BuildBarn, and BuildBuddy are the tested backends in `docs/users/remote_execution.md`) is what makes CI runners and fresh laptops fast, and Buck2 lets every user write to the cache through sandboxed RE rather than only trusted CI, per `compared_to_buck1.md`.
- **Deferred materialization changes failure modes.** With RE, outputs are not downloaded until a local action needs them, which Meta reports made real-world builds about 2.5 times faster, but if the RE backend expires a blob before Buck2 needs it, the build fails until a restart; open-source RE backends do not expose artifact TTLs, so that refresh does not work outside Meta (`docs/users/advanced/deferred_materialization.md`); enable the Restarter to recover automatically.
- **Configs are an invalidation hazard at scale.** Every mode switch that changes buckconfig invalidates almost everything; give frequently alternated configurations their own isolation directories, at the cost of one more daemon's memory each.
- **IDE integrations should be BXL, not shell glue.** A single `buck2 bxl` call replaces a chain of `cquery` and `build` subprocesses with output parsing, runs on the warm daemon, and caches as one node; the prelude's compilation-database, rust-analyzer, gopackagesdriver, and Python typecheck scripts are the patterns to copy.
- **Critical path as a fleet metric.** Export `buck2 log critical-path --format json` from CI and track the share of `user_duration` against total; when overhead entries (`buckd_command_init`, `waiting`, `file-watcher-wait`) dominate, the fix is daemon and infrastructure work, not target refactoring.

Decision rules:

- Under about 100 engineers on a mostly single-language repository, Buck2's main benefits (multi-language composability, remote-first execution, dynamic dependencies) rarely pay for the cost of owning a prelude and toolchains; prefer the language's native tool unless polyglot builds or monorepo-wide caching are already the pain.
- From hundreds to 50,000 engineers in a polyglot monorepo, Buck2 and Bazel both fit; choose Buck2 when you need rule-level dynamic dependencies (OCaml, Haskell, Erlang, distributed ThinLTO), when you want every rule in Starlark with no native special cases, or when you already run Eden or Watchman, and choose Bazel when you need its larger external ecosystem, Bzlmod registry, and the local on-disk action cache for teams without remote execution.
- Do not adopt Buck2 without a remote cache for more than a handful of engineers: the absence of a persistent local action cache means every daemon restart and every fresh CI runner pays full execution cost.
- Pin the Buck2 binary by date tag and the prelude by commit together, and upgrade both on a schedule, because releases are prereleases and the Starlark API is explicitly unstable.

[[Migrate-a-Polyglot-Codebase-to-Hermetic-Builds]] owns the migration plan, and [[Diagnose-Bazel-and-Buck2-Cache-Misses]] owns cache-miss forensics with action digests.

## Practice Drill

> [!tip] Practice Drill:
> Recreate the worked example's scratch project (no prelude, three rules over `/bin/sh`) and predict, before each command, how many commands `Commands:` will report and which targets `buck2 log what-ran` will list.
> Then break early cutoff on purpose: change the `normalize` command so that it appends `date +%s` to its output, make a comment-only edit, and predict which actions now rerun; restore determinism and confirm that `concat` goes quiet again.
> Next, add a `select()` to one attribute and build under two target platforms in the same daemon, and compare the paths `--show-output` prints with the content-hash directories under `buck-out/v2/art/root/`, explaining which actions were deduplicated across configurations.
> Finally, write a BXL script that prints `cquery().rdeps()` of `:a` within `//...`, run it once with plain arguments and once through `ctx.target_universe`, and explain any difference; then run `buck2 log critical-path --format json` and sort entries by `potential_improvement_duration`.
> Stretch: write a 200-line Rust or Python toy that records series-parallel dependencies, invalidates through reverse edges, and revalidates with an equality check, and compare its recompute counts with [[Rebuild-Content-Hashed-Build-System-Lab]].

## Interview Questions

> [!question] L5: What does "Buck2 is not phased" mean, and what observable behavior does it produce?
>
> > [!success]- Answer
> > Loading, configuration, analysis, execution, and materialization are not global barriers; each is a family of DICE keys in one graph, and a key runs as soon as the keys it requests are ready.
> > The analysis of one target can run while another target's actions execute, and a request computes only the slice of the graph it needs.
> > Observable effects: `buck2 log critical-path` shows `listing`, `load`, `analysis`, `action`, and `materialization` entries in a single chain, and an edit to a source file's contents reruns actions without rerunning analysis, because analysis never reads contents.
> > The architecture docs still name the stages, but say they "are not always sequential".

> [!question] L5 (predict the output): In the worked example, if `a.txt` changes only in a comment line that `normalize` strips, what does the next `buck2 build //:report //:picked` report, and why?
>
> > [!success]- Answer
> > It reports `Commands: 1 (cached: 0, remote: 0, local: 1)`, and `what-ran` lists only `normalize` for `root//:a`.
> > The file watcher dirties the file key for `a.txt`, which invalidates the `normalize` action that reads it; that action reruns and produces byte-identical output, so its output digest (content path `b6a98d9ce9a2d914`) and DICE value compare equal.
> > `concat` and `select` are then revalidated through their recorded dependencies instead of rerun: early cutoff.
> > No analysis or load reruns, because neither reads source contents and neither `BUCK` nor `defs.bzl` changed.

> [!question] L5: Why is `ctx.cquery().rdeps("root//path/...", "X", 1)` in BXL empty when `buck2 cquery 'rdeps(root//path/..., X, 1)'` returns targets?
>
> > [!success]- Answer
> > The CLI builds one target universe from both patterns, so `X` resolves to every configuration in which it appears in that universe, including the one its dependents use.
> > BXL configures each argument independently with the default target platform, so `X` resolves only to its own default configuration, which no dependent in the universe uses.
> > Fix it with `ctx.target_universe([...])` and `universe.lookup(...)`, or call `ctx.cquery().eval("rdeps(...)")`, which applies CLI semantics.
> > The design reason is that BXL gives explicit control over configurations, while the CLI hides them.

> [!question] L6 (debug this): A new BXL script fails with "The attribute `artifacts` is not available on the type `dict[Label, bxl.BuildResult]`" and, after a fix, with "Type of parameter `labels` doesn't match ... actual `TargetLabel`" at `ctx.analysis(target)`; diagnose both.
>
> > [!success]- Answer
> > `ctx.build(...)` returns a dict from label to `bxl.BuildResult`, even for one target, so iterate `for label, result in ctx.build(t).items()` and call `result.artifacts()` on each value.
> > `cli_args.target_label()` yields an unconfigured `TargetLabel`, while `ctx.analysis` needs a configured label, a `bxl.ConfiguredTargetNode`, or a configured target set, because analysis is defined per configuration.
> > Configure first with `ctx.configured_targets(ctx.cli_args.target)` (optionally with a target platform), then pass the node.
> > Both errors come from Starlark's typed API checking at runtime, which is why BXL fails fast with the expected and actual types rather than misbehaving.

> [!question] L6: When should a rule use `dynamic_output` (or `dynamic_actions`) rather than an anonymous target, and why does Buck2 refuse fully general dynamic dependencies?
>
> > [!success]- Answer
> > Use dynamic outputs when the actions to run depend on the contents of a built artifact: OCaml module order from `ocamldeps`, distributed ThinLTO backends from the thin-link index, Erlang header subsets.
> > The lambda runs after the listed artifacts are built and must bind every declared output; with an empty `dynamic` list it also defers expensive analysis until an output is actually requested.
> > Use anonymous targets when the problem is sharing or overlaying analysis: two rules that request the same anon rule with the same attributes share one analysis and its actions, keyed by attribute hash, which is Buck2's answer to aspects and to cross-binary sharing.
> > Fully general dynamic dependencies would let file contents add target-graph edges, which would break `uquery` and `cquery` and the target determinator that decides what CI must build, so Buck2 keeps the target graph static and allows dynamism only below a target.

> [!question] L6: After `buck2 kill`, a developer's next build reruns every action even though all outputs are in `buck-out`; explain why, and give two fixes with their costs.
>
> > [!success]- Answer
> > Buck2's incrementality lives in the daemon: DICE's validity windows, recorded dependencies, and dep files are in memory, and without remote execution there is no persistent action cache to consult, so a new daemon cannot prove that an output on disk matches its action digest.
> > The worked example shows this: after `buck2 kill`, rebuilding `//:picked` ran four local commands.
> > Fix one: configure a REAPI action cache under `[buck2_re_client]`, so a new daemon gets cache hits by digest; the cost is running or paying for a cache and making actions hermetic enough to share.
> > Fix two: avoid restarts by keeping the binary pinned and avoiding config changes that force one; `sqlite_materializer_state = true` only avoids re-downloading outputs from RE and does not skip execution.
> > Bazel differs here: its on-disk action cache survives a server restart.

> [!question] L7: Compare how DICE and Skyframe handle a computation that discovers a dependency mid-execution, and what that does to CPU cost and to recomputation after a change.
>
> > [!success]- Answer
> > In Skyframe, `env.getValue` returns `null` for a missing dependency; the `SkyFunction` returns `null`, the dependency is evaluated, and the function body runs again from the start, so a function that discovers k dependencies one at a time can run up to k + 1 times, which is why `env.getValues()` batches requests.
> > In DICE the computation is a Rust future: `ctx.compute(&k).await` suspends without losing work and resumes when `k` is ready, so discovery costs no re-execution.
> > DICE's `&mut DiceComputations` forces concurrency through `compute2` and `compute_join`, which lets it record dependencies as a series-parallel graph.
> > After a change, DICE replays that structure: series dependencies are checked in order and stop at the first change, parallel groups are checked concurrently, so checking never requests a key the real compute would not, and no speculative work is cancelled.
> > Both then apply early cutoff (Skyframe's change pruning, DICE's equality), and both pay a reverse-dependency walk proportional to the invalidated subset.

> [!question] L7: How do you diagnose and fix a build host with 96 hardware threads that intermittently OOMs while evaluating `BUCK` files, with no single large target?
>
> > [!success]- Answer
> > Starlark collects garbage only between top-level statements of a build file, not inside macro calls, so many concurrent evaluations of allocation-heavy macros can each hold large transient heaps at once.
> > Read `starlark_peak_allocated_bytes` from `LoadBuildFileEnd` events in `buck2 log show` to find the worst files, then profile them with `buck2 profile loading`.
> > Fix the macros: hoist allocations out of loops, stop calling `dict.items()` on large dicts per iteration, and generate and process targets one at a time.
> > Enable `[buck2] check_starlark_peak_memory = true` so files above the 2 GiB default (`DEFAULT_STARLARK_MEMORY_USAGE_LIMIT`) fail loudly; it is false in source, so the guard is off unless you turn it on.
> > If the remaining peak is from the DICE graph rather than Starlark, use `buck2 debug allocative` to find retaining keys and track `allocator-stats.allocated` rather than RSS.

> [!question] L7: How would you design, on Buck2, a compilation database for your IDE team that stays current as engineers edit?
>
> > [!success]- Answer
> > Write it as BXL: take a file or target, use `ctx.cquery().owner(...)` within an explicit `ctx.target_universe` to find owning configured targets, `ctx.analysis(...)` to read compile flags from providers, and write the database with BXL actions, returning the ensured path.
> > Start from `prelude//cxx/tools/compilation_database.bxl:generate`, which does exactly this for clangd and clang-tidy.
> > Because the script is one DICE node with no early cutoff, keep it thin: move per-target flag extraction into `bxl.anon_rule` anon targets so unchanged targets reuse cached results across invocations, and keep heavy JSON writing in actions.
> > Run it on the developer's warm daemon, use its own `--isolation-dir` if editor integrations must not block or be blocked by interactive builds, and export its critical path to watch latency regressions.

> [!question] L8+: Your company runs Bazel on a 30,000-engineer polyglot monorepo and a group proposes moving to Buck2; what evidence do you require, and what would make you say no?
>
> > [!success]- Answer
> > Require a measured pilot on the same graph: peak and steady-state daemon memory, no-op and incremental latency for top workflows, critical-path breakdowns, and remote-cache hit rates, because no fetched primary source publishes a controlled Buck2-versus-Bazel benchmark; Meta's published gains are against Buck1.
> > Weigh the mechanisms: Buck2 gives rule-level dynamic dependencies, all-Starlark rules, transitive sets wired into the graph, a suspending engine, and concurrent commands on equal state; Bazel gives a larger rules ecosystem, Bzlmod, Skymeld, and an on-disk action cache that survives restarts.
> > Count the migration cost: rewriting every native and third-party rule against the Buck2 prelude, owning toolchains (Meta does not open source its toolchains), retraining, and running both systems during migration.
> > Say no if the pain is not one Buck2 uniquely solves, if there is no remote execution plan (Buck2 without a remote cache restarts cold), or if the org cannot absorb biweekly prerelease upgrades and an unstable Starlark API.
> > Record the decision with [[Build-Performance-Economics-and-Decision-Record]], including the reversal criteria.

## Further Reading

- `docs/about/why.md` (facebook/buck2): the authoritative list of what differs from Buck1 and Bazel, including "not phased" and dynamic dependencies.
- `dice/docs/incrementality.md` (facebook/buck2 main): the formal model of DICE's core state, certificates, validity windows, reverse-dependency invalidation, and the soundness theorem.
- `docs/insights_and_knowledge/modern_dice.md` (facebook/buck2): the Modern DICE talk transcript, with the single-threaded core state, non-speculative dependency checks, and the pitfalls the team hit.
- `dice/dice/src/api/key.rs` and `api/computations.rs`: the actual `Key` trait and the series-parallel combinators, which are more current than the prose API docs.
- `docs/rule_authors/dynamic_dependencies.md` and `anon_targets.md`: the two bounded forms of dynamism and why full generality is refused.
- `docs/bxl/` (index, FAQ, basics, cquery divergence): the BXL API, its caching model, and its configuration semantics.
- `app/buck2_critical_path/src/potential.rs`: the critical-path and potential-improvement algorithm.
- `facebook/starlark-rust` README, `docs/gc.md`, and `docs/types.md`: the interpreter's guarantees, moving collector, and type system.
- Bazel's Skyframe, client/server, and memory pages, and the Bazel 7 release blog: the primary sources for the comparison table.
- Andrey Mokhov, Neil Mitchell, and Simon Peyton Jones, "Build Systems a la Carte" (JFP 2020): the theory Buck2's design draws on, owned by [[Build-System-Theory-Schedulers-and-Rebuilders]].

## Related

- Prerequisites: [[Build-System-Theory-Schedulers-and-Rebuilders]], [[Bazel-Skyframe-Bzlmod-and-Execution-Model]]
- Next: [[Remote-Execution-API-and-Remote-Caching]]
- See also: [[Diagnose-Bazel-and-Buck2-Cache-Misses]], [[Build-Performance-Economics-and-Decision-Record]], [[Migrate-a-Polyglot-Codebase-to-Hermetic-Builds]], [[Cargo-Build-Graph-and-Rustc-Pipelining]], [[Cpp-Modules-in-Build-Systems]], [[Rebuild-Content-Hashed-Build-System-Lab]], [[Hermetic-and-Reproducible-Builds]]
