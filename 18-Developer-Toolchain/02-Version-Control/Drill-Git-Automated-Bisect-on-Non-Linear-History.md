---
type: problem
track: [sde, distinguished, low-latency, quant-dev]
level:
status: draft
last_reviewed:
sources:
  - "Git Community, git-bisect(1) Manual Page, Git 2.56.0 Documentation, 2026, https://git-scm.com/docs/git-bisect"
  - "Christian Couder, Fighting regressions with git bisect, Git Project Documentation (git-bisect-lk2009), 2009, https://git-scm.com/docs/git-bisect-lk2009"
  - "Git Community, git-rev-list(1) Manual Page, Git 2.56.0 Documentation, 2026, https://git-scm.com/docs/git-rev-list"
  - "Git Community, git-merge-base(1) Manual Page, Git 2.56.0 Documentation, 2026, https://git-scm.com/docs/git-merge-base"
  - "Scott Chacon and Ben Straub, Pro Git: Git Tools - Debugging with Git, 2nd Edition, Apress, 2014, https://git-scm.com/book/en/v2/Git-Tools-Debugging-with-Git"
  - "Git Community, Git Source Repository: bisect.c implementation, 2026, https://github.com/git/git/blob/master/bisect.c"
  - "Git Community, Git 2.29 Release Notes (git bisect --first-parent), 2020, https://raw.githubusercontent.com/git/git/master/Documentation/RelNotes/2.29.0.txt"
---

# Drill: Automated Git Bisect Across Non-Linear Merge History

> [!summary] Scope
> Owns a practical bisection drill isolating a microsecond latency regression across 500 complex merge commits using automated bisection testing.
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

The underlying test harness compiles a native C order-matching benchmark (`order_matcher.c`) using `${CC:-cc} -O3`.
The production service-level agreement (SLA) requires order-book lookup latency to remain strictly below 10,000 nanoseconds (10.0 microseconds).
At the baseline tag `v1.0.0`, the engine uses a cache-aligned contiguous array lookup, executing in 4,200 nanoseconds.
On feature branch `feature_17` at commit 10, an engineer commits a data-structure change (`feat(routing): switch order lookup to dynamic linked list`) that introduces heap pointer indirection, spiking execution latency to 38,500 nanoseconds and violating the SLA.

Additionally, the repository graph contains two distinct failure modes common to production monorepos:
1. **Unbuildable Intermediate Revisions:** On feature branch `feature_17` at commit 8, an engineer committed incomplete work (`wip(17): incomplete refactoring commit 8`) containing a syntax error that halts compilation before commit 9 resolves it.
2. **Criss-Cross Merge Bases:** Feature branches `feature_12` and `feature_13` cross-merge each other midway through their development histories, generating multiple independent merge bases with ambiguous common ancestry.

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

Running `git bisect run ./test_naive.sh` yields the following output recorded on macOS (`darwin-arm64`):

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
Status: FALSE POSITIVE (unbuildable intermediate commit accused instead of regression)
```

The naive bisection falsely identifies commit `78cd8b9` (`wip(17): incomplete refactoring commit 8`) as the culprit that introduced the 38.5-microsecond latency regression.
In reality, commit `78cd8b9` merely failed to compile due to an unfinished syntax edit.
Because the compiler exited with status 1 or 2, `git bisect` marked the revision as `bad`.
This falsely constrained the candidate search space to ancestors of commit `78cd8b9`, completely bypassing the true regression commit introduced two commits later (`28a3f78 feat(routing): switch order lookup to dynamic linked list`).

## Progressive Hint Ladder

> [!tip]- Hint 1: Inspecting the Commit Graph and Exit Codes
> Git bisect interprets exit codes from automated test scripts according to strict POSIX conventions documented in `git-bisect(1)`.
> Exit code 0 informs Git that the current commit is `good`.
> Any exit code between 1 and 127 (inclusive), with the sole exception of 125, informs Git that the current commit is `bad`.
> If a compilation error or runtime crash causes your script to exit with 1 or 2, Git cannot distinguish that failure from an SLA assertion breach and flags the commit as `bad`.
> How can your test harness inform Git that a commit is simply untestable without aborting the bisection?

> [!tip]- Hint 2: Handling Untestable and Broken Revisions
> Exit code 125 is reserved by `git bisect run` to indicate that a commit cannot be tested and must be skipped.
> The Git project selected 125 because POSIX shells reserve exit codes 126 (`command invoked cannot execute`) and 127 (`command not found`).
> Any exit code from 128 through 255 causes `git bisect run` to abort immediately with a fatal error.
> Wrap every build step, dependency resolution check, and runtime prerequisite in your test script with an explicit fallback to `exit 125`.
> When Git receives code 125, it marks the commit as skipped (`git bisect skip`) and picks an adjacent commit on the Directed Acyclic Graph (DAG) to test instead.

> [!tip]- Hint 3: Diagnosing Criss-Cross Merges and Ambiguous Merge Bases
> In non-linear histories with multiple active branches, criss-cross merges occur when two branches merge each other's intermediate states.
> Running `git merge-base --all <branchA> <branchB>` exposes whether multiple independent common ancestors exist.
> When bisecting across branches that have multiple merge bases, Git must first establish whether the bug existed before the branch point.
> If Git outputs `Bisecting: a merge base must be tested`, your automated script must be capable of testing that ancestral commit cleanly.
> If a merge base tests `bad`, Git halts because the foundational premise of your bisection (that the base commit was `good`) is invalid.

> [!tip]- Hint 4: Coarse-to-Fine Mainline Isolation with --first-parent
> In large enterprise monorepos with hundreds of daily pull requests, developer feature branches frequently contain unbuildable intermediate commits.
> Bisecting every individual commit across all feature branches forces Git to navigate noisy internal branch histories.
> Git 2.29 introduced `git bisect start --first-parent` (as of 2020-10, stable as of 2026-11 on Linux and macOS).
> When `--first-parent` is active, Git treats each merge commit on the trunk as an atomic unit and follows only the first parent of each merge.
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
>         F17_1 --> F17_7["feat(17): worker commit 7 (good)"]
>         F17_7 --> F17_8["wip(17): worker commit 8<br/>(UNBUILDABLE: syntax error)"]
>         F17_8 --> F17_9["fix(17): worker commit 9 (good)"]
>         F17_9 --> F17_10["feat(routing): worker commit 10<br/>(REGRESSION: latency 38.5 us)"]
>         F17_10 --> F17_19["feat(17): worker commit 19 (bad)"]
>         F17_19 -.-> M17
>     end
>
>     subgraph CrissCross["Criss-Cross Merge Topology"]
>         M12 -.-> F12_5["feat(12): step 5<br/>[Merge Base 1]"]
>         M12 -.-> F13_5["feat(13): step 5<br/>[Merge Base 2]"]
>         F12_5 --> XM12["feat(12): cross-merge 13_5"]
>         F13_5 --> XM13["feat(13): cross-merge 12_5"]
>         F13_5 -.-> XM12
>         F12_5 -.-> XM13
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
> When the test script runs `make` on `F17_8` (`wip(17): incomplete refactoring commit 8`), the build fails with an unhandled compiler syntax error.
> A naive script with `set -e` aborts and exits with status 1 or 2.
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
> Status: FALSE POSITIVE (unbuildable intermediate commit accused instead of regression)
>
> --- 4. Robust Automated Bisect Run (Exit Code 125 Skipping) ---
> Robust bisection culprit: 28a3f78 feat(routing): switch order lookup to dynamic linked list
> Status: SUCCESS (exact regression commit pinpointed in 10 bisection steps)
>
> --- 5. Mainline Integration Bisect (--first-parent) ---
> First-parent mainline culprit: 7032045 merge feature_17 into main
> Status: SUCCESS (fast pull-request / merge-commit isolation in 5 steps)
> ```
>
> ### Step-by-Step Traversal Breakdown
>
> In Phase 4, `git bisect run ./test_robust.sh` navigates 500 revisions across the non-linear DAG:
> 1. Step 1 tests commit `f779f8a4` (`merge feature_12 into main`).
>    It tests `good`, eliminating branches 1 through 12 (254 commits) in a single step.
> 2. When Git's binary search hits `78cd8b9` (`wip(17): incomplete refactoring commit 8`), compilation fails.
>    The harness catches the error and exits with 125.
> 3. Git logs `skip` on `78cd8b9`, dynamically computes an alternate midpoint on the DAG, and tests commit 9 (`fix(17): fix build error from commit 8`).
> 4. Commit 9 compiles cleanly, exhibits baseline 4.2 us latency, and tests `good`.
> 5. Git steps forward to commit 10 (`28a3f78 feat(routing): switch order lookup to dynamic linked list`), measures 38.5 us latency, and marks it `bad`.
> 6. Git terminates with exit code 0, declaring `28a3f78` the exact first bad commit.
>
> In Phase 5, running `git bisect start --first-parent HEAD v1.0.0` restricts the bisection walk exclusively to first parents along the integration trunk.
> Rather than walking all 501 commits, Git walks only the 25 merge commits.
> In exactly $\lceil \log_2(25) \rceil = 5$ steps, it pinpoints `7032045 merge feature_17 into main`.
> This two-tier pattern (fast `--first-parent` isolation in CI, followed by fine-grained branch bisection) is the standard operating model for high-throughput repositories.

## Core Takeaway

Automated bisection across non-linear Git history requires strict discipline in graph theory, process exit codes, and test harness idempotency:

1. **The Exit-Code Contract is Inflexible:** An automated bisection script must never exit with a generic failure code (1 or 2) when an unexpected environment error, compiler failure, or test setup issue occurs.
   Reserving exit code 125 for untestable commits prevents catastrophic false positives that derail root-cause investigations.
2. **DAG Reachability Dictates Search Efficiency:** A Git repository is not an array of linear commits; it is a Directed Acyclic Graph.
   Understanding how Git weights revisions via `git rev-list --bisect-vars` allows engineers to predict bisection paths and identify topological bottlenecks like criss-cross merges.
3. **Mainline First-Parent Strategy at Scale:** In repositories with high commit churn, always execute coarse-grained bisection using `--first-parent` first.
   Isolating the offending merge commit identifies the responsible team or pull request in 4 to 6 steps, avoiding hundreds of unbuildable, intermediate worker commits.
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
> Because the script specifies `set -e`, the failure of `cargo build --release` causes the shell to exit immediately with the compiler's non-zero return code (typically status 101 for Cargo or status 1/2 for `rustc`).
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
```

The CI job exits with return code 2.
Explain why `git bisect run` failed to isolate a single commit, why it exited with status 2, and what concrete steps the engineer must take to isolate the regression.

> [!success]- Answer
> The issue arises because `make test || exit 125` converts *every* test failure into exit code 125 (`skip`).
> Under this script, a commit that passes tests exits with 0 (`good`), but a commit that fails the test *also* exits with 125 (`skip`) because `make test` returns non-zero.
> Consequently, the bisection script never emitted a single `bad` exit code (values 1 to 127 except 125).
>
> When Git skips all failing commits adjacent to the boundary, it reaches a state where the first bad commit could be any of the skipped revisions.
> Git outputs the remaining candidate set, prints `We cannot bisect more!`, and exits with return code 2 (indicating an ambiguous completion due to skipped revisions).
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
> 6. Git optimizes traversal using the heuristic helper `approx_halfway()`, which short-circuits full graph scoring once a candidate commit within a close bound of $N / 2$ is encountered.

### Question 4 (L6, Criss-Cross Merges and Merge Bases)
What is a criss-cross merge, why does it produce multiple merge bases, and how does `git bisect` respond when initialized across a revision range with multiple merge bases?

> [!success]- Answer
> A criss-cross merge occurs when two branches cross-merge each other's historical states at different points in time without one branch superseding the other:
>
> ```text
>   --- B1 --- o --- M1 (Branch A)
>        \    \ /
>         \    X
>          \  / \
>   --- B2 --- o --- M2 (Branch B)
> ```
>
> In this topology, both `B1` and `B2` are maximal common ancestors of `M1` and `M2`.
> Neither `B1` is an ancestor of `B2`, nor is `B2` an ancestor of `B1`.
> Running `git merge-base --all M1 M2` outputs both commit hashes.
>
> When `git bisect start <bad> <good>` is executed across a topology with multiple merge bases:
> 1. Git cannot determine which merge base defines the baseline clean state for bisection.
> 2. Git automatically checks out the first merge base and prints:
>    `Bisecting: a merge base must be tested`
> 3. If the test script marks the merge base as `good`, Git checks out the next merge base until all common ancestors are evaluated.
> 4. If all merge bases test `good`, normal binary search proceeds within the remaining DAG.
> 5. If *any* merge base tests `bad`, Git aborts with an error:
>    `The merge base <hash> is bad.`
>    `This means the bug has been fixed between <hash> and [<good>].`
>    This occurs because the initial assumption that `<good>` represented the ancestral state before the bug was introduced is false.

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
> 3. Because merge commits on trunk represent pull requests that passed automated CI merge gates, intermediate commits are guaranteed to compile cleanly.
> 4. Once `--first-parent` isolates the specific merge commit that introduced the regression, an engineer can run a second, isolated bisection on the isolated range `merge^1..merge^2` to find the exact commit within the feature branch.

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
>    - This avoids mutating local branch pointers, prevents index stat-cache corruption, and enables parallel bisection runs across multiple workers.
> 2. **Two-Stage Hierarchical Bisection:**
>    - **Stage 1 (Trunk Merge Isolation):** Run `git bisect start --first-parent <bad-nightly> <last-good-nightly>` to isolate the regressed merge commit in $\le 5$ iterations.
>    - **Stage 2 (PR Hunk Bisection):** Extract the merged branch range `<merge>^1..<merge>^2` and run fine-grained bisection across the topic branch commits.
> 3. **Execution Environment Determinism:**
>    - Microsecond latency benchmarks suffer from CPU frequency scaling and noisy neighbor jitter on shared CI hosts.
>    - Pin the benchmark process to isolated physical cores using `taskset -c 2,3` (Linux) or dedicated hardware instances.
>    - Lock CPU frequency governors to `performance` mode (`cpupower frequency-set -g performance`).
>    - Use monotonic CPU cycle counters (`clock_gettime(CLOCK_MONOTONIC_RAW)`) and compute statistical medians across 10,000 warmup and execution iterations.
> 4. **Resilient Harness Contract:**
>    - Enforce a strict wrapper script that traps unexpected errors:
>      ```bash
>      trap 'exit 125' ERR
>      ```
>    - Ensure the script outputs structured JSON telemetry via `git bisect log` for ingestion by observability pipelines.
> 5. **Cleanup and Teardown:**
>    - Always execute `git bisect reset` within a shell `EXIT` trap.
>    - Remove the ephemeral worktree via `git worktree remove --force /tmp/bisect-runner` to avoid leaking disk space.

### Question 8 (L7, Commit-Graph Acceleration for Large-Scale Bisection)
How do Git commit-graph chunk files (`gitformat-commit-graph(5)`) and generation numbers accelerate `git rev-list --bisect` across repositories with millions of commits, such as the Linux kernel or Windows monorepo?

> [!success]- Answer
> In traditional Git object stores without a commit-graph file, computing bisection weights requires parsing raw commit objects from loose files or packfiles.
> To find the bisection midpoint across $N$ commits, Git must traverse the graph from `bad` down to `good`, decompressing packfile zlib streams and parsing ASCII parent headers into in-memory `struct commit` representations.
> On repositories with 1,000,000+ commits, this object-parsing phase alone can take several seconds per bisection step.
>
> The commit-graph file (`.git/objects/info/commit-graph`) optimizes this process:
> 1. **Fixed-Width Binary Chunks:** Commit metadata is laid out in 36-byte binary chunks (`CDAT` chunk).
>    Root tree hashes, parent commit positions, and commit timestamps are indexed by 1-based integer offsets, eliminating text parsing and pointer-chasing.
> 2. **Corrected Commit Date Generation Numbers (`CDAT` and `GDAX` chunks):**
>    Git uses corrected commit dates (monotonic generation numbers introduced in Git 2.30) to prune reachability walks.
>    During bisection ancestry walks, if a commit's generation number is lower than the generation number of the `good` commit, Git immediately cuts off traversal down that branch without reading ancestor commits.
> 3. **Topological Level Ordering:**
>    Generation numbers allow `bisect.c` to compute candidate revision weights using priority queues sorted by generation number, converting an $O(N)$ packfile decompression walk into an in-memory binary array search that executes in milliseconds.

## Further Reading

- Christian Couder, *Fighting regressions with git bisect* (`git-bisect-lk2009`), Git Technical Documentation, 2009.
  The foundational paper on Git bisection written by the command's primary author, detailing midpoint selection mathematics and automated bisection scripting.
- Git Community, `git-bisect(1)` Manual Page, Git 2.56.0 Documentation, 2026.
  The authoritative reference manual defining the bisection CLI contract, exit status codes (0, 1, 125, 128+), and `--first-parent` integration.
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
