---
type: problem
track: [sde, distinguished, low-latency, quant-dev]
level:
status: solid
last_reviewed:
sources:
  - "Git Community, git-bisect(1) Manual Page, Git 2.56.0 Documentation, 2026, https://git-scm.com/docs/git-bisect"
  - "Christian Couder, Fighting regressions with git bisect, Git Project Documentation (git-bisect-lk2009), 2009, https://git-scm.com/docs/git-bisect-lk2009"
  - "Git Community, git-rev-list(1) Manual Page, Git 2.56.0 Documentation, 2026, https://git-scm.com/docs/git-rev-list"
  - "Git Community, git-merge-base(1) Manual Page, Git 2.56.0 Documentation, 2026, https://git-scm.com/docs/git-merge-base"
  - "Scott Chacon and Ben Straub, Pro Git: Git Tools - Debugging with Git, 2nd Edition, Apress, 2014, https://git-scm.com/book/en/v2/Git-Tools-Debugging-with-Git"
  - "Git Community, Git Source Repository: bisect.c implementation, 2026, https://github.com/git/git/blob/master/bisect.c"
  - "Git Community, Git 2.29 Release Notes (git bisect --first-parent), 2020, https://raw.githubusercontent.com/git/git/master/Documentation/RelNotes/2.29.0.adoc"
  - "Git Community, Git 2.31 Release Notes (commit-graph corrected commit dates), 2021, https://raw.githubusercontent.com/git/git/v2.31.0/Documentation/RelNotes/2.31.0.txt"
  - "Git Community, Git 2.36 Release Notes (generation number v2 fixes), 2022, https://raw.githubusercontent.com/git/git/v2.36.0/Documentation/RelNotes/2.36.0.txt"
  - "Git Community, gitformat-commit-graph(5), Git Source Repository, 2026, https://raw.githubusercontent.com/git/git/master/Documentation/gitformat-commit-graph.adoc"
  - "Git Community, Git Source Repository: bisect.h exit codes, 2026, https://github.com/git/git/blob/master/bisect.h"
  - "Git Community, git-scm.com latest source release 2.56.1 (as of 2026-11), 2026, https://git-scm.com/"
  - "Git Community, Git Source Repository: GIT-VERSION-GEN (DEF_VER v2.56.GIT on master), 2026, https://github.com/git/git/blob/master/GIT-VERSION-GEN"
  - "Git Community, git-bisect(1) source (run exit codes, --first-parent), Git Source Repository, 2026, https://raw.githubusercontent.com/git/git/master/Documentation/git-bisect.adoc"
  - "Git Community, git-merge-base(1) source (criss-cross diagram), Git Source Repository, 2026, https://raw.githubusercontent.com/git/git/master/Documentation/git-merge-base.adoc"
  - "The Cargo Team, cargo-build(1) Exit Status, The Cargo Book, 2026, https://doc.rust-lang.org/cargo/commands/cargo-build.html"
  - "Git Community, Commit Graph Design Notes (technical/commit-graph), Git Source Repository, 2026, https://raw.githubusercontent.com/git/git/master/Documentation/technical/commit-graph.adoc"
  - "Git Community, git-worktree(1) Manual Page (per-worktree refs/bisect), Git 2.56.0 Documentation, 2026, https://git-scm.com/docs/git-worktree"
  - "Git Community, Git Source Repository: builtin/rev-list.c (--bisect-vars output), 2026, https://github.com/git/git/blob/master/builtin/rev-list.c"
  - "Git Community, Git Source Repository: commit.c (repo_parse_commit_internal and the commit-graph), 2026, https://github.com/git/git/blob/master/commit.c"
  - "Git Community, Git Source Repository: builtin/bisect.c (cmd_bisect exit code handling), 2026, https://github.com/git/git/blob/master/builtin/bisect.c"
---

# Drill: Automated Git Bisect Across Non-Linear Merge History

> [!summary] Scope
> Owns a practical bisection drill isolating a simulated microsecond latency regression in a 500-commit candidate range containing 27 merge commits, using automated bisection testing.
> Reaches the Use and Debug rungs by providing a harness script containing build breakages and transient test failures, teaching the engineer how to write a robust test script that uses exit code 125 for untestable commits and handles merge-base ambiguities.
> Grounds the concepts of bisection in automated CI/CD practices.

## Related

- Prerequisites: [[Git-Archaeology-Log-Blame-and-Bisect]]
- Lab: `code/02-Version-Control/drill_bisect_regression/`

## Reproduction

In low-latency automated trading systems and distributed data planes, performance regressions often manifest as fractional microsecond latency spikes across deep, non-linear commit graphs.
When hundreds of engineers merge parallel feature branches into an integration trunk, standard binary search workflows fail if intermediate commits contain compilation failures, broken tests, or criss-cross merge bases.
This drill reproduces an order-matching engine latency regression hidden within a 501-commit repository featuring 27 merge commits across 25 parallel feature branches.

The complete reproduction harness resides in `code/02-Version-Control/drill_bisect_regression/run.sh`.
Execute the harness directly from the command line:

```bash
cd code/02-Version-Control/drill_bisect_regression && bash run.sh
```

The underlying test harness compiles a native C order-matching benchmark stand-in (`order_matcher.c`) using `${CC:-cc} -O3`.
The benchmark does not time anything: `benchmark_latency()` returns a fixed constant so that the bisection path, and therefore the golden output, is deterministic.
The production service-level agreement (SLA) requires order-book lookup latency to stay at or below 10,000 nanoseconds (10.0 microseconds); the test scripts mark a commit `bad` only when the reported value is greater than 10,000.
At the baseline tag `v1.0.0`, the source comment describes a contiguous array lookup and the binary reports 4,200 nanoseconds.
On feature branch `feature_17` at commit 10, an engineer commits a data-structure change (`feat(routing): switch order lookup to dynamic linked list`) that, in the drill's story, introduces heap pointer indirection; the binary reports 38,500 nanoseconds, violating the SLA.

Additionally, the repository graph contains two distinct failure modes common to production monorepos:
1. **Unbuildable Intermediate Revisions:** On feature branch `feature_17` at commit 8, an engineer committed incomplete work (`wip(17): incomplete refactoring commit 8`) containing a syntax error that halts compilation before commit 9 (`fix(17): fix build error from commit 8`) resolves it.
   A second unbuildable commit, `wip(05): incomplete refactoring commit 7` on `feature_05`, sits in the half of the graph that the first bisection step eliminates, so neither run ever tests it.
2. **Criss-Cross Merge Bases:** Feature branches `feature_12` and `feature_13` cross-merge each other after their fifth commits, so `git merge-base --all feature_12 feature_13` reports two best common ancestors, neither an ancestor of the other.

## Observed Symptom

When an automated continuous integration pipeline or engineer attempts to find the performance regression using a naive bisection test script (`test_naive.sh`), the bisection search prematurely terminates on an innocent commit and outputs a false positive.

The naive script executes standard compilation and test commands without distinguishing build failures from test assertions:

```bash
#!/usr/bin/env bash
set -e
${CC:-cc} -O3 -o order_matcher order_matcher.c
./order_matcher > bench.out

LATENCY=$(awk '/LATENCY_NS:/ {print $2}' bench.out)
if [ "$LATENCY" -gt 10000 ]; then
    exit 1
else
    exit 0
fi
```

The harness inspects the graph and then runs `git bisect run ./test_naive.sh`; the first three sections of its output, recorded on macOS (`darwin-arm64`), are:

```text
=== DRILL: AUTOMATED BISECT ACROSS NON-LINEAR MERGE HISTORY ===
Repository topology: 501 commits, 27 merge commits across 25 feature branches

--- 1. Criss-Cross Merge Base Ambiguity Detection ---
Common ancestors between feature_12 and feature_13: 2
  ancestor: d53e4cb9cf (feat(13): worker commit 5)
  ancestor: fb21e2426d (feat(12): worker commit 5)

--- 2. Internal Graph Bisection Variables (v1.0.0..HEAD) ---
bisect_rev='f779f8a4d1caae4ee42020dd13bc7224db02cb5c'
bisect_nr=254
bisect_good=254
bisect_bad=244
bisect_all=500
bisect_steps=8

--- 3. Naive Automated Bisect Run (Build Breakage Trap) ---
Naive bisection culprit: 78cd8b9 wip(17): incomplete refactoring commit 8
Naive session: 9 revisions tested: 5 good, 4 bad, 0 skip
Verdict: FALSE POSITIVE (culprit does not compile, so it cannot have introduced a latency regression)
```

The naive bisection falsely identifies commit `78cd8b9` (`wip(17): incomplete refactoring commit 8`) as the culprit that introduced the 38.5-microsecond latency regression.
In reality, commit `78cd8b9` merely failed to compile due to an unfinished syntax edit.
Because the compiler exited with a non-zero status (1 from Apple clang on the recording host), `set -e` propagated it and `git bisect` marked the revision as `bad`.
No result in the harness output is fixed text (only the headings and the "25 feature branches" label are literals): the culprit comes from `refs/bisect/bad`, the `session` lines count the good, bad, and skip entries in `git bisect log`, and each `Verdict:` line is computed.
`run.sh` finds the injected regression commit by its subject in the generated history, and when the culprit is some other revision it compiles that revision's `order_matcher.c` with `cc -fsyntax-only`, which is how `FALSE POSITIVE` is decided.
This falsely constrained the candidate search space to ancestors of commit `78cd8b9`, completely bypassing the true regression commit introduced two commits later (`28a3f78 feat(routing): switch order lookup to dynamic linked list`).

## Progressive Hint Ladder

> [!tip]- Hint 1: Inspecting the Commit Graph and Exit Codes
> Git bisect interprets exit codes from automated test scripts according to its own contract, documented in the `run` section of `git-bisect(1)`.
> Exit code 0 informs Git that the current commit is `good`.
> Any exit code between 1 and 127 (inclusive), with the sole exception of 125, informs Git that the current commit is `bad`.
> If a compilation error or runtime crash causes your script to exit with 1 or 2, Git cannot distinguish that failure from an SLA assertion breach and flags the commit as `bad`.
> How can your test harness inform Git that a commit is simply untestable without aborting the bisection?

> [!tip]- Hint 2: Handling Untestable and Broken Revisions
> Exit code 125 is reserved by `git bisect run` to indicate that a commit cannot be tested and must be skipped.
> The Git project selected 125 because POSIX shells reserve exit codes 126 (command found but not executable) and 127 (command not found).
> Any other exit code, including 128 through 255 (for example a test killed by a signal), aborts the bisect process; Git 2.55.0 prints `bisect run failed: exit code 130 from ... is < 0 or >= 128` (verified locally, macOS, as of 2026-11).
> Wrap every build step, dependency resolution check, and runtime prerequisite in your test script with an explicit fallback to `exit 125`.
> When Git receives code 125, it marks the commit as skipped (`git bisect skip`) and picks a nearby commit on the Directed Acyclic Graph (DAG) to test instead.

> [!tip]- Hint 3: Diagnosing Criss-Cross Merges and Ambiguous Merge Bases
> In non-linear histories with multiple active branches, criss-cross merges occur when two branches merge each other's intermediate states.
> Running `git merge-base --all <branchA> <branchB>` exposes whether multiple independent common ancestors exist.
> When a `good` commit is not an ancestor of the `bad` commit, Git must first establish that the bug did not already exist at the branch point, so it tests the merge bases of `bad` and `good` before bisecting (`bisect.c:check_merge_bases()`); a criss-cross gives it more than one to test.
> If Git outputs `Bisecting: a merge base must be tested`, your automated script must be capable of testing that ancestral commit cleanly.
> If a merge base tests `bad`, Git stops (exit status 3, `BISECT_MERGE_BASE_CHECK` in `bisect.h`) because the premise of your bisection (that history before the branch point was `good`) is invalid.
> In this drill `v1.0.0` is an ancestor of `HEAD`, so none of the three `git bisect run` sessions performs a merge-base check.
> The lab shows the two merge bases with `git merge-base --all` (section 1) and triggers the check separately with `git bisect start feature_13 feature_12` (section 6).

> [!tip]- Hint 4: Coarse-to-Fine Mainline Isolation with --first-parent
> In large enterprise monorepos with hundreds of daily pull requests, developer feature branches frequently contain unbuildable intermediate commits.
> Bisecting every individual commit across all feature branches forces Git to navigate noisy internal branch histories.
> Git 2.29 (released 2020-10) introduced `git bisect start --first-parent`; it is a regular, non-experimental option in Git 2.56.0 (as of 2026-11, Both).
> When `--first-parent` is active, Git follows only the first parent of each merge, so a regression introduced by a merged branch is attributed to the merge commit and the branch's own commits are ignored.
> This narrows the search from $N$ total commits to $M$ mainline merge commits, isolating the regressed pull request in $O(\log M)$ steps before running a second, localized bisection inside that specific topic branch.

## Model Solution

> [!success]- Solution
> ### Root Cause Analysis
>
> The failure of naive automated bisection stems from a violation of the Git bisection exit-code contract and the topology of Directed Acyclic Graphs (DAGs).
>
> In a linear Git history of $N$ commits, binary search selects commit $\lfloor N / 2 \rfloor$.
> In a non-linear history with merge commits, Git's graph walker (`bisect.c:find_bisection()`) assigns a weight to each candidate commit $C$.
> The weight represents the number of ancestor commits reachable from $C$ within the candidate range.
> Git selects a commit whose weight minimizes $|weight(C) - N / 2|$, attempting to halve the remaining candidate set regardless of whether the commit tests `good` or `bad`.
> In the drill, `git rev-list --bisect-vars v1.0.0..HEAD` picks `merge feature_12 into main` with weight 245 of 500 candidates: `bisect_good=254` and `bisect_bad=244` are the remaining counts minus one (`builtin/rev-list.c` prints `all - reaches - 1` and `reaches - 1`).
>
> The diagram is schematic: dotted edges stand for elided commits, and the 21 other feature branches are omitted.
>
> ```mermaid
> flowchart TD
>     subgraph Trunk["Mainline Integration Trunk"]
>         V1["v1.0.0 (good)<br/>latency: 4.2 us"] --> M12["merge feature_12<br/>(good)"]
>         M12 --> M13["merge feature_13<br/>(good)"]
>         M13 --> M16["merge feature_16<br/>(good)"]
>         M16 --> M17["merge feature_17<br/>(bad, latency: 38.5 us)"]
>         M17 --> HEAD["HEAD (bad)<br/>latency: 38.5 us"]
>     end
>
>     subgraph Feat17["Feature Branch 17"]
>         M16 -.-> F17_1["feat(17): worker commit 1 (good)"]
>         F17_1 -.-> F17_7["feat(17): worker commit 7 (good)"]
>         F17_7 --> F17_8["wip(17): incomplete refactoring commit 8<br/>(UNBUILDABLE: syntax error)"]
>         F17_8 --> F17_9["fix(17): fix build error from commit 8 (good)"]
>         F17_9 --> F17_10["feat(routing): commit 10<br/>(REGRESSION: latency 38.5 us)"]
>         F17_10 -.-> F17_19["feat(17): worker commit 19 (bad)"]
>         F17_19 --> M17
>     end
>
>     subgraph CrissCross["Criss-Cross Merge Topology"]
>         V1 -.-> F12_5["feat(12): worker commit 5<br/>[Merge Base 1]"]
>         V1 -.-> F13_5["feat(13): worker commit 5<br/>[Merge Base 2]"]
>         F12_5 --> XM12["feat(12): cross-merge<br/>feature_13 step 5"]
>         F13_5 --> XM13["feat(13): cross-merge<br/>feature_12 step 5"]
>         F13_5 --> XM12
>         F12_5 --> XM13
>         XM12 -.-> M12
>         XM13 -.-> M13
>     end
>
>     classDef good fill:#1b4332,stroke:#40916c,color:#d8f3dc;
>     classDef bad fill:#5c1d24,stroke:#e63946,color:#ffccd5;
>     classDef broken fill:#7f4f24,stroke:#ddb892,color:#ede0d4;
>     classDef base fill:#1d3557,stroke:#457b9d,color:#f1faee;
>
>     class V1,M12,M13,M16,F17_1,F17_7,F17_9 good;
>     class M17,HEAD,F17_10,F17_19 bad;
>     class F17_8 broken;
>     class F12_5,F13_5 base;
> ```
>
> When the test script runs `${CC:-cc} -O3 -o order_matcher order_matcher.c` on `F17_8` (`wip(17): incomplete refactoring commit 8`), the build fails with an unhandled compiler syntax error.
> A naive script with `set -e` aborts and exits with the compiler's status (1 on the recording host).
> Git records `F17_8` as `bad`.
> Because Git assumes monotonicity (any ancestor of a `bad` commit could be the first bad commit, but descendants cannot be the *first* bad commit), Git eliminates `F17_9`, `F17_10`, and all subsequent commits from consideration.
> The search converges on `F17_8`, yielding a false positive.
>
> ### Robust Automated Test Script
>
> A production-grade bisection script must isolate three execution phases:
> 1. **Build Environment Cleanliness:** Remove stale object files and binaries to prevent caching false positives across checked-out revisions.
> 2. **Build Gating with Exit 125:** Attempt compilation.
>    If the compiler fails, exit immediately with status 125 to signal an untestable commit.
> 3. **Deterministic Assertion Execution:** Execute the benchmark, capture standard output, and parse metrics.
>    If the benchmark crashes or produces invalid output, exit with status 125.
>    Only if the measured metric cleanly breaches the SLA do we exit with status 1.
>
> The robust test script (`test_robust.sh`) implements this contract:
>
> ```bash
> #!/usr/bin/env bash
> # Robust automated bisect test script
> # Employs exit code 125 for untestable commits and validates benchmark latency SLA.
>
> # Step 1: Clean build artifacts
> rm -f order_matcher bench.out
>
> # Step 2: Attempt compilation. Exit 125 marks the revision as untestable/skip.
> ${CC:-cc} -O3 -o order_matcher order_matcher.c 2>/dev/null || exit 125
>
> # Step 3: Run benchmark. If binary crashes or runner fails, mark untestable.
> ./order_matcher > bench.out 2>/dev/null || exit 125
>
> # Step 4: Extract measured latency and evaluate against the 10,000 ns (10.0 us) SLA.
> LATENCY=$(awk '/LATENCY_NS:/ {print $2}' bench.out)
> [ -n "$LATENCY" ] || exit 125
>
> if [ "$LATENCY" -gt 10000 ]; then
>     exit 1  # Performance regression detected (BAD)
> else
>     exit 0  # Within latency SLA (GOOD)
> fi
> ```
>
> ### Execution and Verification Output
>
> Executing `run.sh` evaluates both full DAG bisection and mainline first-parent bisection.
> The output below is verified against `expected/darwin-arm64.txt` on macOS (`darwin-arm64`):
>
> ```text
> === DRILL: AUTOMATED BISECT ACROSS NON-LINEAR MERGE HISTORY ===
> Repository topology: 501 commits, 27 merge commits across 25 feature branches
>
> --- 1. Criss-Cross Merge Base Ambiguity Detection ---
> Common ancestors between feature_12 and feature_13: 2
>   ancestor: d53e4cb9cf (feat(13): worker commit 5)
>   ancestor: fb21e2426d (feat(12): worker commit 5)
>
> --- 2. Internal Graph Bisection Variables (v1.0.0..HEAD) ---
> bisect_rev='f779f8a4d1caae4ee42020dd13bc7224db02cb5c'
> bisect_nr=254
> bisect_good=254
> bisect_bad=244
> bisect_all=500
> bisect_steps=8
>
> --- 3. Naive Automated Bisect Run (Build Breakage Trap) ---
> Naive bisection culprit: 78cd8b9 wip(17): incomplete refactoring commit 8
> Naive session: 9 revisions tested: 5 good, 4 bad, 0 skip
> Verdict: FALSE POSITIVE (culprit does not compile, so it cannot have introduced a latency regression)
>
> --- 4. Robust Automated Bisect Run (Exit Code 125 Skipping) ---
> Robust bisection culprit: 28a3f78 feat(routing): switch order lookup to dynamic linked list
> Robust session: 10 revisions tested: 5 good, 4 bad, 1 skip
> Verdict: CORRECT (culprit is the regression commit)
>
> --- 5. Mainline Integration Bisect (--first-parent) ---
> First-parent mainline culprit: 7032045 merge feature_17 into main
> First-parent session: 5 revisions tested: 3 good, 2 bad, 0 skip
> Verdict: CORRECT (culprit is the trunk merge that brought in the regression)
>
> --- 6. Merge-Base Check (good is not an ancestor of bad) ---
> Prompt 'a merge base must be tested': yes
> First revision checked out is a merge base: yes (d53e4cb9cf)
> Marking that merge base bad: exit status 3
> The merge base <hash> is bad.
> This means the bug has been fixed between <hash> and [<hash>].
> ```
>
> ### Step-by-Step Traversal Breakdown
>
> In Phase 4, `git bisect run ./test_robust.sh` searches the 500 candidate revisions of `v1.0.0..HEAD`; the lab prints the session size from `git bisect log` (10 revisions tested: 5 good, 4 bad, 1 skip), and the per-step hashes below were read from that log with Git 2.55.0 on macOS (as of 2026-11):
> 1. Step 1 tests `f779f8a` (`merge feature_12 into main`).
>    It tests `good`, eliminating its 245 ancestors in the range (branches 1 through 12, the first five `feature_13` commits, and the first 12 trunk merges) in a single step.
> 2. Steps 2 to 6 test `dafa623` (bad), `b5e4d94` (good), `7032045 merge feature_17 into main` (bad), `699a614 feat(17): worker commit 4` (good), and `442dd30 feat(17): worker commit 12` (bad).
> 3. Step 7 lands on `78cd8b9` (`wip(17): incomplete refactoring commit 8`); compilation fails, the script exits with 125, and Git records `skip`.
> 4. Git picks a nearby commit to test instead.
>    Step 8 tests `feat(17): worker commit 6` (good).
>    Step 9 tests `155e303 fix(17): fix build error from commit 8`, which compiles, reports the baseline 4,200 ns, and tests `good`.
> 5. Step 10 tests `28a3f78 feat(routing): switch order lookup to dynamic linked list`, which reports 38,500 ns and is marked `bad`.
> 6. `git bisect run` exits with status 0, declaring `28a3f78` the first bad commit after 10 tested revisions (one of them skipped).
>
> In Phase 5, running `git bisect start --first-parent HEAD v1.0.0` restricts the bisection walk exclusively to first parents along the integration trunk.
> Rather than the 500 candidates of the full range, Git considers only the 25 trunk merge commits.
> In 5 steps ($\lceil \log_2(25) \rceil = 5$ is the upper bound), it pinpoints `7032045 merge feature_17 into main`; the lab prints `5 revisions tested: 3 good, 2 bad, 0 skip` and checks the culprit against the trunk merge that first contains the regression commit (Git 2.55.0, macOS, as of 2026-11).
> Question 5 and Question 7 build on this to form a two-tier pattern: `--first-parent` isolation of the merge first, then a fine-grained bisection inside that merge's topic branch.

## Core Takeaway

Automated bisection across non-linear Git history requires strict discipline in graph theory, process exit codes, and test harness idempotency:

1. **The Exit-Code Contract is Inflexible:** An automated bisection script must never exit with a generic failure code (1 or 2) when an unexpected environment error, compiler failure, or test setup issue occurs.
   Reserving exit code 125 for untestable commits prevents catastrophic false positives that derail root-cause investigations.
2. **DAG Reachability Dictates Search Efficiency:** A Git repository is not an array of linear commits; it is a Directed Acyclic Graph.
   Understanding how Git weights revisions via `git rev-list --bisect-vars` allows engineers to predict bisection paths and identify topological bottlenecks like criss-cross merges.
3. **Mainline First-Parent Strategy at Scale:** In repositories with high commit churn, always execute coarse-grained bisection using `--first-parent` first.
   Isolating the offending merge commit identifies the responsible team or pull request in about $\lceil \log_2 M \rceil$ steps for $M$ trunk merges (5 for the drill's 25), avoiding hundreds of unbuildable, intermediate worker commits.
4. **Architectural Handoff:** For the underlying graph traversal theory, refer to [[Git-Archaeology-Log-Blame-and-Bisect]].
   For commit range arithmetic and ancestry syntax, see [[Git-Revision-Selection-and-Graph-Walks]].
   For three-way merge mechanics and conflict resolution, see [[Git-Three-Way-Merge-ORT-and-Diff-Algorithms]].
   For merge queue architectures and trunk governance, see [[Git-Branching-Models-Merge-Queues-and-Code-Review]] and [[Decision-Record-Merge-vs-Rebase-and-Branching]].

## Interview Questions

### Question 1 (L5, Predict-the-Output)
A developer runs an automated bisection session using `git bisect run ./test.sh`.
The test script is written as follows:

```bash
#!/bin/bash
set -e
cargo build --release
target/release/benchmark --assert-p99-under-10ms
```

During step 4 of bisection, the repository checks out a commit that contains a compiler error.
What exact action does `git bisect` take, what will the final result of the bisection session be, and how would you rewrite the script to prevent this failure mode?

> [!success]- Answer
> Because the script specifies `set -e`, the failure of `cargo build --release` causes the shell to exit immediately with Cargo's return code, 101 ("Cargo failed to complete", `cargo-build(1)` Exit Status).
> `git bisect` interprets any exit status between 1 and 127 (except 125) as a test failure, marking the unbuildable commit as `bad`.
> This corrupts the bisection state: Git assumes the performance regression was present at or before this unbuildable commit, eliminating all newer revisions from the search space.
> The bisection will falsely accuse an unbuildable revision instead of the true regression commit.
>
> To fix the script, decouple build verification from test assertion using exit code 125:
>
> ```bash
> #!/bin/bash
> cargo build --release 2>/dev/null || exit 125
> target/release/benchmark --assert-p99-under-10ms
> ```
>
> When `cargo build` fails, the script exits with 125, instructing Git to skip the commit (`git bisect skip`) and test a nearby topological revision instead.

### Question 2 (L5, Debug-This)
An infrastructure engineer configures an automated bisection job in a CI runner.
The script begins with:

```bash
git bisect start HEAD v2.14.0
git bisect run sh -c "make test || exit 125"
```

The job runs for 12 steps, but upon completion, the terminal displays:

```text
There are only 'skip'ped commits left to test.
The first 'bad' commit could be any of:
8a1f49b109e2
b92f74ca018c
c03d81fa4920
We cannot bisect more!
error: bisect run cannot continue any more
```

Git prints full 40-hex object names in this list; they are shortened here.
The CI job exits with return code 2.
Explain why `git bisect run` failed to isolate a single commit, why it exited with status 2, and what concrete steps the engineer must take to isolate the regression.

> [!success]- Answer
> The issue arises because `make test || exit 125` converts *every* test failure into exit code 125 (`skip`).
> Under this script, a commit that passes tests exits with 0 (`good`), but a commit that fails the test *also* exits with 125 (`skip`) because `make test` returns non-zero.
> Consequently, the bisection script never emitted a single `bad` exit code (values 1 to 127 except 125).
>
> When Git skips all failing commits adjacent to the boundary, it reaches a state where the first bad commit could be any of the skipped revisions.
> Git outputs the remaining candidate set, prints `We cannot bisect more!`, and exits with return code 2: `error_if_skipped_commits()` in `bisect.c` returns `BISECT_ONLY_SKIPPED_LEFT` (-2 in `bisect.h`) from `bisect_next_all()`, `bisect_next()` in `builtin/bisect.c` passes it on after `bisect_skipped_commits()` logs the skipped set, and `cmd_bisect()` negates it via `return is_bisect_success(res) ? 0 : -res;`, surfacing as process exit status 2.
> Reproduced with Git 2.55.0 on macOS (as of 2026-11) by running the drill repository with `sh -c './test_robust.sh || exit 125'`: 173 revisions tested (3 good, 170 skip), exit status 2, and 171 candidate commits listed, because every bad revision was skipped.
>
> To debug and resolve:
> 1. Separate compilation failure from test failure:
>    ```bash
>    make build || exit 125
>    make test # Returns 0 on success (good), non-zero on test failure (bad)
>    ```
> 2. If intermediate commits are genuinely unbuildable across an ambiguity window, manually test the candidate revisions by applying a cherry-picked fix or stash patch to make them buildable, or test revisions on side branches that share the same logical changes.

### Question 3 (L5, Graph Bisection Mechanics)
How does `git bisect` compute the optimal commit to test next in a Directed Acyclic Graph (DAG) with merge commits, and how does this algorithm differ from simple linear binary search?

> [!success]- Answer
> In a linear history of $N$ commits, binary search is trivial: commit index $\lfloor N / 2 \rfloor$ divides the search space exactly in half.
> In a Git DAG with parallel branches and merge commits, revisions cannot be indexed linearly because commits have multiple parents and varying descendant counts.
>
> Git implements graph bisection in `bisect.c:find_bisection()`:
> 1. Git collects all candidate commits in the range `good..bad` (reachable from `bad`, excluding commits reachable from `good`).
> 2. For each commit $C$, Git calculates its topological weight $w(C)$, defined as the number of candidate commits reachable from $C$ (including $C$ itself).
> 3. If commit $C$ is tested and found `good`, commit $C$ and all its ancestors are eliminated (eliminating $w(C)$ commits).
> 4. If commit $C$ is tested and found `bad`, all commits that are *not* ancestors of $C$ are eliminated (eliminating $N - w(C)$ commits).
> 5. Git evaluates commits to minimize the worst-case remaining search space, selecting the commit whose weight is closest to half the total candidate count: $|w(C) - N / 2|$.
> 6. Git optimizes traversal using the heuristic helper `approx_halfway()`, which short-circuits full graph scoring once a candidate's weight is within half a commit of $N / 2$ (`2 * weight - nr` is -1, 0, or 1), or for large ranges within about 0.1% of it (`abs(2 * weight - nr) < nr / 1024` in `bisect.c`).
> 7. The remaining expected steps are estimated by `estimate_bisect_steps()` in `bisect.c`, computing $(e < 3 \cdot x) \ ? \ n : n - 1$ where $n = \lfloor \log_2(\text{all}) \rfloor$, $e = 2^n$, and $x = \text{all} - e$.

### Question 4 (L6, Criss-Cross Merges and Merge Bases)
What is a criss-cross merge, why does it produce multiple merge bases, and how does `git bisect` respond when initialized across a revision range with multiple merge bases?

> [!success]- Answer
> A criss-cross merge occurs when two branches cross-merge each other's historical states at different points in time without one branch superseding the other:
>
> ```text
> ---B1---o---M1 (Branch A)
>     \ /
>      X
>     / \
> ---B2---o---o---M2 (Branch B)
> ```
>
> In this topology (adapted from `git-merge-base(1)`), both `B1` and `B2` are best common ancestors of `M1` and `M2`.
> Neither `B1` is an ancestor of `B2`, nor is `B2` an ancestor of `B1`.
> Running `git merge-base --all M1 M2` outputs both commit hashes.
>
> When `git bisect start <bad> <good>` is executed and `<good>` is not an ancestor of `<bad>`, Git runs a merge-base check (`bisect.c:check_merge_bases()`, described in `git-bisect-lk2009` under "Checking merge bases"); a criss-cross only means there is more than one base to test:
> 1. Git cannot assume the history before the branch point is clean: `<good>` on a side branch does not prove its merge base with `<bad>` was good, because the bug may predate the merge base and have been fixed on the `<good>` side.
> 2. Git automatically checks out the first untested merge base and prints:
>    `Bisecting: a merge base must be tested`
> 3. If the test script marks the merge base as `good`, Git checks out the next merge base until all common ancestors are evaluated.
>    Once all common ancestors are confirmed clean, Git creates `.git/BISECT_ANCESTORS_OK` so subsequent steps bypass `check_merge_bases()`.
> 4. If all merge bases test `good`, normal binary search proceeds within the remaining DAG.
> 5. If *any* merge base tests `bad`, `handle_bad_merge_base()` in `bisect.c` prints:
>    `The merge base <hash> is bad.`
>    `This means the bug has been fixed between <hash> and [<good>].`
>    It returns `BISECT_MERGE_BASE_CHECK` (-3 in `bisect.h`), which `cmd_bisect()` negates via `return is_bisect_success(res) ? 0 : -res;`, exiting with status 3.
>    This occurs because the initial assumption that `<good>` represented the ancestral state before the bug was introduced is false.
>
> All three behaviors were reproduced on the drill repository with Git 2.55.0 on macOS (as of 2026-11), using `git bisect start feature_13 feature_12`: Git checked out `feat(13): worker commit 5`, then `feat(12): worker commit 5`, and `git bisect bad` on the first base exited with status 3.
> Section 6 of the lab output checks the prompt, that the first checkout is one of the two merge bases, and exit status 3 with the `is bad` message; the walk to the second base and the all-good path are not in the lab.

### Question 5 (L6, First-Parent Bisection Mechanics)
Analyze the asymptotic complexity and operational trade-offs of `git bisect start --first-parent` (introduced in Git 2.29).
Under what organizational branching models does this flag offer the greatest leverage?

> [!success]- Answer
> In a repository with trunk-based development and non-fast-forward merge commits (or pull-request integration merges), the history consists of a mainline trunk containing $M$ merge commits, while the underlying feature branches contain $K$ individual commits per branch, yielding $N \approx M \times K$ total commits.
>
> Full DAG bisection operates over all $N$ commits with $O(\log N) = O(\log(M \cdot K))$ testing steps.
> While asymptotically logarithmic, full DAG bisection frequently checks out intermediate, unreviewed worker commits inside feature branches that may have compilation breaks, missing database migrations, or broken test suites.
>
> Running `git bisect start --first-parent` restricts the revision walk exclusively to parent 1 of each commit:
> 1. Upon encountering a merge commit, Git ignores parent 2 (the feature branch tip) and follows only parent 1 (the trunk history).
> 2. The search space drops from $N$ to $M$, requiring only $O(\log M)$ steps.
> 3. When every trunk merge passed a CI merge gate that built the merged result, the commits the first-parent walk visits are far more likely to build than intermediate topic commits; nothing in Git guarantees it, and `git-bisect(1)` describes the option only as "particularly useful in avoiding false positives when a merged branch contained broken or non-buildable commits, but the merge itself was OK".
> 4. Once `--first-parent` isolates the specific merge commit that introduced the regression, an engineer can run a second, isolated bisection on the isolated range `merge^1..merge^2` (for example `git bisect start <merge>^2 <merge>^1`) to find the exact commit within the feature branch.
>    Because `<merge>^1` is usually not an ancestor of `<merge>^2`, Git first tests their merge base, the branch point, as Hint 3 describes.
>    If `<merge>^2` tests `good`, the regression is a semantic conflict created by the merge itself, and the merge commit is the answer.

### Question 6 (L6, Ambiguity Windows and Exit 125)
Under what conditions does `git bisect run` terminate with exit code 2?
Explain the mathematical structure of the ambiguity window and how an engineer can resolve it programmatically.

> [!success]- Answer
> `git bisect run` returns exit code 0 when it successfully isolates a single commit as the first bad revision.
> It returns exit code 2 when bisection terminates but the first bad commit cannot be uniquely identified due to skipped revisions (exit code 125).
>
> Mathematically, if commit $G$ is known `good`, commit $B$ is known `bad`, and all intermediate commits $S_1, S_2, \dots, S_k$ on the path between $G$ and $B$ were skipped:
> - The regression could have been introduced in $B$.
> - Alternatively, the regression could have been introduced in any skipped commit $S_i$ and persisted through $B$.
> - The candidate set of first bad commits is $\{S_1, S_2, \dots, S_k, B\}$.
>
> To resolve the ambiguity window programmatically:
> 1. **Synthetic Patch Injection:** Create a test harness script that dynamically applies a cherry-picked fix or patch onto skipped commits before compiling:
>    ```bash
>    git cherry-pick --no-commit <build_fix_hash> || exit 125
>    make test
>    ```
> 2. **Bisecting Alternate Branch Merges:** If $S_i$ occurred on a feature branch that was rebased or merged elsewhere, bisect along sibling branches that share the same logical changes.
> 3. **Manual Code Inspection across Window:** If the window is small (2-3 commits), inspect the diff between the last known good commit $G$ and the first known bad commit $B$ using `git diff G B`.

### Question 7 (L7, Headless Bisection Architecture in CI/CD)
Design a headless, automated bisection service for an enterprise monorepo that detects and isolates microsecond latency regressions in nightly integration builds.
Address disk caching, detached `HEAD` state, parallel testing, and noisy neighbor mitigation.

> [!success]- Answer
> An enterprise automated bisection service in CI/CD must be designed as follows:
>
> 1. **Worktree-Isolated Execution:**
>    - Avoid bisecting in the primary checkout.
>    - Use `git worktree add --detach /tmp/bisect-runner <target-commit>` to create an ephemeral, detached working tree.
>    - This avoids disturbing the primary checkout and its index, and because `refs/bisect/*` is per-worktree, it enables parallel bisection runs across multiple workers.
> 2. **Two-Stage Hierarchical Bisection:**
>    - **Stage 1 (Trunk Merge Isolation):** Run `git bisect start --first-parent <bad-nightly> <last-good-nightly>` to isolate the regressed merge commit in $\le 5$ iterations.
>    - **Stage 2 (PR Hunk Bisection):** Extract the merged branch range `<merge>^1..<merge>^2` and run fine-grained bisection across the topic branch commits.
> 3. **Execution Environment Determinism:**
>    - Microsecond latency benchmarks suffer from CPU frequency scaling and noisy neighbor jitter on shared CI hosts.
>    - Pin the benchmark process to isolated physical cores using `taskset -c 2,3` (Linux) or dedicated hardware instances.
>    - Lock CPU frequency governors to `performance` mode (`cpupower frequency-set -g performance`).
>    - Time with a monotonic clock that is not slewed by NTP (`clock_gettime(CLOCK_MONOTONIC_RAW)` on Linux; it is a clock, not a cycle counter) and compare medians and tail percentiles across many warmup and measured iterations, with a noise margin around the SLA threshold.
> 4. **Resilient Harness Contract:**
>    - Trap unexpected setup and build errors, but scope the trap so it cannot swallow the verdict:
>      ```bash
>      trap 'exit 125' ERR   # setup and build only
>      make build
>      trap - ERR            # a failing assertion below must exit 1, not 125
>      ./benchmark --assert-p99-under-10us
>      ```
>      A global `trap 'exit 125' ERR` left active over the assertion turns every genuine failure into a skip, reproducing the Question 2 failure (verified with bash on macOS: a failing last command exits 125).
>    - `git bisect log` emits a replayable plain-text log (`git bisect replay`), not JSON; the wrapper must produce any structured telemetry itself, for example by parsing that log.
> 5. **Cleanup and Teardown:**
>    - Always execute `git bisect reset` within a shell `EXIT` trap.
>    - Remove the ephemeral worktree via `git worktree remove --force /tmp/bisect-runner` to avoid leaking disk space.

### Question 8 (L7, Commit-Graph Acceleration for Large-Scale Bisection)
How do Git commit-graph chunk files (`gitformat-commit-graph(5)`) and generation numbers accelerate `git rev-list --bisect` across repositories with millions of commits, such as the Linux kernel or Windows monorepo?

> [!success]- Answer
> In traditional Git object stores without a commit-graph file, computing bisection weights requires parsing raw commit objects from loose files or packfiles.
> To find the bisection midpoint across $N$ commits, Git must traverse the graph from `bad` down to `good`, decompressing packfile zlib streams and parsing ASCII parent headers into in-memory `struct commit` representations.
> On repositories with 1,000,000+ commits, this object-parsing phase is the cost that grows with the number of commits walked; no timing is claimed here, so measure it on your own repository by timing `git rev-list --bisect-vars <good>..<bad>` before and after `git commit-graph write --reachable`.
>
> The commit-graph file (`.git/objects/info/commit-graph`) optimizes this process:
> 1. **Fixed-Width Binary Records:** The Commit Data (`CDAT`) chunk holds one fixed-width record of H + 16 bytes per commit (36 bytes with SHA-1, 48 with SHA-256).
>    Each record stores the root tree OID, the array positions of the first two parents in the OID Lookup chunk (`0x70000000` for no parent, octopus parents via the Extra Edge List), the topological level, and the commit time, so parents are read without inflating or parsing commit objects.
> 2. **Corrected Commit Date Generation Numbers (`GDA2` and `GDO2` chunks):**
>    Git 2.31 taught the commit-graph to store corrected commit dates (generation number v2) in a Generation Data chunk, with a Generation Data Overflow chunk for offsets that do not fit in 31 bits; the current chunk IDs `GDA2` and `GDO2` follow the generation number v2 fixes in Git 2.36 (as of 2026-11, Both).
>    These numbers let reachability queries such as `git merge-base` stop walking early: if commit A's generation is below commit B's, A cannot reach B (Git technical documentation, `commit-graph.adoc`).
>    `bisect.c` (master, 2026-11) never mentions generation numbers or the commit-graph (checked by search), so bisection does not itself cut its walk off by generation.
>    It benefits through the shared commit-parsing path instead: `commit.c:repo_parse_commit_internal()` reads parents and commit dates from the commit-graph via `parse_commit_in_graph()` when one exists, and `repo_parse_commit_gently()` enables that by default.
> 3. **Where Bisection Gains:**
>    `find_bisection()` still counts reachable candidates over every commit in the range, so weighting stays linear in $N$; the commit-graph removes per-commit object decompression and parsing from that walk rather than changing its complexity.
>    No per-step latency figure is claimed for million-commit repositories.

## Further Reading

- Christian Couder, *Fighting regressions with git bisect* (`git-bisect-lk2009`), Git Technical Documentation, 2009.
  The foundational paper on Git bisection, written by a long-time bisect contributor (the original algorithm is credited in it to Linus Torvalds), detailing midpoint selection, merge-base checking, and automated bisection scripting.
- Git Community, `git-bisect(1)` Manual Page, Git 2.56.0 Documentation, 2026.
  The authoritative reference manual defining the bisection CLI contract, script exit codes (0 good, 1-127 except 125 bad, 125 skip, anything else aborts), and `--first-parent` integration.
- Git Community, `bisect.c` Source Implementation, Git Source Code, 2026.
  The core C source file implementing graph reachability weighting (`find_bisection()`) and halfway short-circuit heuristics (`approx_halfway()`).
- Git Community, `git-merge-base(1)` Manual Page, Git 2.56.0 Documentation, 2026.
  Technical documentation describing maximal common ancestor computation and criss-cross merge base resolution.
- Git Community, `gitformat-commit-graph(5)` Manual Page, Git 2.56.0 Documentation, 2026.
  Specification of binary commit-graph chunk formats, generational indexes, and reachability acceleration.

## See Also

- [[Git-Archaeology-Log-Blame-and-Bisect]]: The foundational concept note detailing Git historical inspection, pickaxe searches, line-log walks, and bisection theory.
- [[Git-Revision-Selection-and-Graph-Walks]]: Deep coverage of revision range syntax (`A..B`, `A...B`), symmetric difference, and ancestry traversal mechanics.
- [[Git-Three-Way-Merge-ORT-and-Diff-Algorithms]]: Internals of three-way merge resolution, rename detection, merge conflicts, and `rerere`.
- [[Git-Branching-Models-Merge-Queues-and-Code-Review]]: Trunk-based development, automated speculative merge queues, and pull-request gating architecture.
- [[Decision-Record-Merge-vs-Rebase-and-Branching]]: Architectural trade-off analysis comparing merge-commit workflows with rebase and fast-forward policies.
- [[Git-Trace2-Telemetry-and-Diagnostics]]: Instrumenting Git CLI executions in CI/CD pipelines using performance telemetry streams.
- [[Git-GC-Maintenance-MIDX-and-Commit-Graph]]: Repository maintenance, commit-graph generation numbers, and multi-pack index optimization.
- [[Git-At-Scale-VFS-Scalar-and-Monorepos]]: Scaling Git monorepos to millions of commits using virtualized filesystems and background maintenance.
