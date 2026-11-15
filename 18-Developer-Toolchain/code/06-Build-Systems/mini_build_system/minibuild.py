"""minibuild: a content-hashed build engine in one file.

Vocabulary from Mokhov, Mitchell, and Peyton Jones, "Build Systems a la Carte"
(ICFP 2018): a store maps keys to values, a task computes one key from the
values of the keys it fetches, a scheduler decides the order, and a rebuilder
decides whether a task must run.

This engine is a suspending scheduler with a constructive-trace rebuilder, the
combination the paper calls Cloud Shake:

- Keys are workspace-relative paths, plus three pseudo-key families:
  'env:NAME' (an environment variable), 'tool:NAME' (a tool's bytes), and
  'rule:NAME' (the source of the rule function, for self-tracking).
- A value is the SHA-256 of the key's bytes, or MISSING.
- A task is a Python generator.  It yields Need([keys]) and receives their
  values; it yields Run(...) and receives the finished action.  Yielding
  suspends the task, so dependencies are discovered while the task runs.
- A trace records, for one key, the dependency groups in the order the task
  asked for them, each with the value it saw, plus the result digest.
- Results live in a content-addressable store (CAS), so a matching trace can
  restore an output without running anything.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import subprocess
import time
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Generator, Iterable

import depfile

MISSING = "missing"
SCHEMA = 1
MAX_TRACES_PER_KEY = 4


class BuildError(Exception):
    pass


class CycleError(BuildError):
    pass


class CorruptBlob(BuildError):
    pass


# --------------------------------------------------------------------------
# Effects a task can request


@dataclass(frozen=True)
class Need:
    keys: tuple[str, ...]

    def __init__(self, keys: Iterable[str]) -> None:
        object.__setattr__(self, "keys", tuple(keys))


@dataclass
class Run:
    argv: list[str]
    outputs: list[str]
    env: dict[str, str] = field(default_factory=dict)
    inherit_env: bool = False  # the leak the lab demonstrates
    depfile: str | None = None
    depfile_target: str | None = None


@dataclass
class Finished:
    returncode: int
    stderr: str
    discovered: list[str]


Task = Generator[object, object, None]


@dataclass
class Rule:
    name: str
    fn: Callable[["Ctx", str], Task]

    @property
    def recipe_digest(self) -> str:
        return sha256_bytes(inspect.getsource(self.fn).encode())


class Rules:
    """Rules by name, plus a function that maps an output key to a rule name."""

    def __init__(self, rules: Iterable[Rule], match: Callable[[str], str | None]) -> None:
        self.by_name = {r.name: r for r in rules}
        self.match = match

    def rule_for(self, key: str) -> Rule | None:
        if ":" in key:  # pseudo-keys are leaves
            return None
        name = self.match(key)
        return None if name is None else self.by_name[name]


# --------------------------------------------------------------------------
# Hashing with a stat cache


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


STRONG_STAT = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev", "st_mode")
WEAK_STAT = ("st_size", "st_mtime_ns")  # what a naive cache keys on


class FileHasher:
    """Content digests, re-hashing a file only when its stat key changed."""

    def __init__(self, root: str, cache: dict, fields: tuple[str, ...] = STRONG_STAT) -> None:
        self.root = root
        self.cache = cache  # path -> [stat key list, digest]
        self.fields = fields
        self.hashed = 0

    def digest(self, rel: str) -> str:
        path = os.path.join(self.root, rel)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self.cache.pop(rel, None)
            return MISSING
        key = [getattr(st, f) for f in self.fields]
        hit = self.cache.get(rel)
        if hit is not None and hit[0] == key:
            return hit[1]
        with open(path, "rb") as f:
            d = hashlib.file_digest(f, "sha256").hexdigest()
        self.hashed += 1
        self.cache[rel] = [key, d]
        return d


# --------------------------------------------------------------------------
# Content-addressable store


class CAS:
    def __init__(self, root: str) -> None:
        self.root = root

    def path(self, digest: str) -> str:
        return os.path.join(self.root, digest[:2], digest[2:])

    def has(self, digest: str) -> bool:
        return os.path.exists(self.path(digest))

    def put(self, src: str, digest: str) -> None:
        dst = self.path(digest)
        if os.path.exists(dst):
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = f"{dst}.tmp.{os.getpid()}"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)  # atomic on POSIX: readers see all or nothing

    def materialize(self, digest: str, dst: str) -> None:
        blob = self.path(digest)
        with open(blob, "rb") as f:
            data = f.read()
        if sha256_bytes(data) != digest:
            os.remove(blob)  # quarantine: never serve it again
            raise CorruptBlob(f"CAS blob {digest[:12]} failed verification")
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        tmp = f"{dst}.tmp.{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)


# --------------------------------------------------------------------------
# Persistent build information


def load_json(path: str) -> dict:
    """A missing file or an unknown schema means a cold start, never a crash."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return doc.get("data", {}) if doc.get("schema") == SCHEMA else {}


def save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schema": SCHEMA, "data": data}, f, sort_keys=True)
    os.replace(tmp, path)  # a crash leaves the old file or the new one, never half of each


class TraceDB:
    """Constructive traces: key -> newest-first list of {deps, result}.

    It lives next to the CAS, so every workspace that shares the cache shares
    the traces: this pair is the toy's action cache.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.traces: dict[str, list[dict]] = load_json(path)

    def record(self, key: str, deps: list[list[list[str]]], result: str) -> None:
        old = [t for t in self.traces.get(key, []) if t["deps"] != deps]
        self.traces[key] = ([{"deps": deps, "result": result}] + old)[:MAX_TRACES_PER_KEY]

    def save(self) -> None:
        save_json(self.path, self.traces)


# --------------------------------------------------------------------------
# The suspending scheduler


class Ctx:
    """What a rule may read besides Need: files it needed, and its declared env."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine

    def read_text(self, rel: str) -> str:
        with open(os.path.join(self._e.root, rel), encoding="ascii") as f:
            return f.read()

    def env(self, name: str) -> str | None:
        return self._e.env.get(name)

    @property
    def python(self) -> str:
        return self._e.python

    def tool(self, name: str) -> str:
        return self._e.tools[name]


@dataclass
class Job:
    key: str
    gen: Task
    seq: int
    waiting_on: set[str] = field(default_factory=set)
    need: tuple[str, ...] = ()


@dataclass
class Stats:
    executed: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)  # executed, same result as before
    corrupt: int = 0
    starts: int = 0


class Engine:
    def __init__(
        self,
        root: str,
        rules: Rules,
        cache_dir: str,
        tools: dict[str, str],
        env: dict[str, str] | None = None,
        jobs: int = 4,
        stat_fields: tuple[str, ...] = STRONG_STAT,
        python: str = "python3",
        stat_name: str = "stat",
    ) -> None:
        self.root = root
        self.rules = rules
        self.tools = tools
        self.env = dict(env or {})
        self.jobs = jobs
        self.python = python
        self.cas = CAS(os.path.join(cache_dir, "cas"))
        self.db = TraceDB(os.path.join(cache_dir, "traces.json"))
        self.stat_path = os.path.join(root, ".minibuild", stat_name + ".json")
        self.hasher = FileHasher(root, load_json(self.stat_path), stat_fields)

    # ---- values of keys that have no rule

    def _leaf_value(self, key: str) -> str:
        if key.startswith("env:"):
            v = self.env.get(key[4:])
            return MISSING if v is None else sha256_bytes(v.encode())
        if key.startswith("tool:"):
            with open(self.tools[key[5:]], "rb") as f:
                return sha256_bytes(f.read())
        if key.startswith("rule:"):
            return self.rules.by_name[key[5:]].recipe_digest
        return self.hasher.digest(key)

    # ---- the rebuilder: constructive traces with early cutoff

    def _rebuild(self, key: str, rule: Rule) -> Task:
        on_disk = self.hasher.digest(key)
        for tr in self.db.traces.get(key, []):
            ok = True
            for group in tr["deps"]:
                vals = yield Need(k for k, _ in group)
                if any(v != h for v, (_, h) in zip(vals, group)):
                    ok = False
                    break
            if not ok:
                continue
            if on_disk == tr["result"]:
                self.stats.verified.append(key)
                return tr["result"]
            if self.cas.has(tr["result"]):
                try:
                    self.cas.materialize(tr["result"], os.path.join(self.root, key))
                except CorruptBlob:
                    self.stats.corrupt += 1
                    break
                self.stats.restored.append(key)
                return self.hasher.digest(key)
        previous = self.db.traces.get(key, [{}])[0].get("result")
        self.stats.starts += 1
        rule_key = "rule:" + rule.name
        deps: list[list[list[str]]] = [[[rule_key, rule.recipe_digest]]]
        gen = rule.fn(Ctx(self), key)
        reply: object = None
        while True:
            try:
                req = gen.send(reply)
            except StopIteration:
                break
            if isinstance(req, Need):
                vals = yield req
                deps.append([[k, v] for k, v in zip(req.keys, vals)])
                reply = vals
            elif isinstance(req, Run):
                fin = yield req
                if fin.returncode != 0:
                    raise BuildError(f"{key}: action failed: {fin.stderr.strip()}")
                if fin.discovered:
                    vals = yield Need(fin.discovered)
                    deps.append([[k, v] for k, v in zip(fin.discovered, vals)])
                reply = fin
            else:
                raise BuildError(f"{key}: task yielded {req!r}")
        result = self.hasher.digest(key)
        if result == MISSING:
            raise BuildError(f"{key}: rule did not write its output")
        self.cas.put(os.path.join(self.root, key), result)
        self.db.record(key, deps, result)
        self.stats.executed.append(key)
        if result == previous:
            self.stats.unchanged.append(key)
        return result

    # ---- actions run on worker threads; everything else on the main thread

    def _execute(self, run: Run) -> Finished:
        try:
            return self._execute_unsafe(run)
        except (OSError, depfile.DepfileError) as e:
            return Finished(1, f"{type(e).__name__}: {e}", [])

    def _execute_unsafe(self, run: Run) -> Finished:
        for out in run.outputs:
            os.makedirs(os.path.dirname(os.path.join(self.root, out)), exist_ok=True)
        start_ns = time.time_ns()
        env = dict(self.env) if run.inherit_env else dict(run.env)
        p = subprocess.run(run.argv, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        discovered: list[str] = []
        if p.returncode == 0 and run.depfile:
            dpath = os.path.join(self.root, run.depfile)
            with open(dpath, encoding="utf-8") as f:
                discovered = depfile.prerequisites_of(f.read(), run.depfile_target or run.outputs[0])
            os.remove(dpath)  # like Ninja with deps=gcc: the trace is the database
            # An input changed after the action started may not be what it read.
            for rel in discovered:
                try:
                    st = os.stat(os.path.join(self.root, rel))
                except FileNotFoundError:
                    continue
                if st.st_ctime_ns > start_ns:
                    return Finished(1, f"input {rel} changed while the action ran; not recording", [])
        return Finished(p.returncode, p.stderr, discovered)

    def _start(self, key: str) -> None:
        rule = self.rules.rule_for(key)
        if rule is None:
            self.values[key] = self._leaf_value(key)
            return
        self.seq += 1
        job = Job(key, self._rebuild(key, rule), self.seq)
        self.running[key] = job
        self.ready.append((job, None, None))

    def _cycle_path(self, frm: str, to: str) -> list[str] | None:
        """Path of keys from `frm` to `to` through what running jobs wait on."""
        stack, seen = [(frm, [frm])], set()
        while stack:
            k, path = stack.pop()
            if k == to:
                return path
            if k in seen or k not in self.running:
                continue
            seen.add(k)
            for w in sorted(self.running[k].waiting_on):
                stack.append((w, path + [w]))
        return None

    def _step(self, job: Job, value: object, exc: BaseException | None) -> None:
        try:
            req = job.gen.throw(exc) if exc else job.gen.send(value)
        except StopIteration as stop:
            self._finish(job, stop.value, None)
            return
        except BuildError as e:
            self._finish(job, None, e)
            return
        except Exception as e:  # a bug or I/O error inside a rule fails that key only
            self._finish(job, None, BuildError(f"{job.key}: {type(e).__name__}: {e}"))
            return
        if isinstance(req, Need):
            job.need = req.keys
            for k in req.keys:
                if k in self.values or k in job.waiting_on:
                    continue
                if k in self.failed:
                    self._finish(job, None, BuildError(f"{job.key}: dependency {k} failed"))
                    return
                if k in self.running:
                    cyc = self._cycle_path(k, job.key)
                    if cyc is not None or k == job.key:
                        path = [job.key] + (cyc or [k])
                        self._finish(job, None, CycleError("cycle: " + " -> ".join(path)))
                        return
                else:
                    self._start(k)
                    if k in self.values:
                        continue
                job.waiting_on.add(k)
                self.waiters[k].append(job)
            if not job.waiting_on:
                self.ready.append((job, [self.values[k] for k in req.keys], None))
        elif isinstance(req, Run):
            fut = self.pool.submit(self._execute, req)
            self.futures[fut] = job
        else:
            self._finish(job, None, BuildError(f"{job.key}: bad request {req!r}"))

    def _finish(self, job: Job, value: object, err: BuildError | None) -> None:
        self.running.pop(job.key, None)
        if err is None:
            self.values[job.key] = value
        else:
            self.failed[job.key] = err
        for w in self.waiters.pop(job.key, []):
            w.waiting_on.discard(job.key)
            if err is not None:
                self.ready.append((w, None, err))
            elif not w.waiting_on:
                self.ready.append((w, [self.values[k] for k in w.need], None))

    def build(self, targets: list[str]) -> Stats:
        self.stats = Stats()
        self.values: dict[str, object] = {}
        self.failed: dict[str, BuildError] = {}
        self.running: dict[str, Job] = {}
        self.waiters: dict[str, list[Job]] = defaultdict(list)
        self.ready: deque = deque()
        self.futures: dict[Future, Job] = {}
        self.seq = 0
        self.hasher.hashed = 0

        def root() -> Task:
            yield Need(targets)

        top = Job("<root>", root(), 0)
        self.ready.append((top, None, None))
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            self.pool = pool
            while self.ready or self.futures:
                while self.ready:
                    self._step(*self.ready.popleft())
                if self.futures:
                    done, _ = wait(self.futures, return_when=FIRST_COMPLETED)
                    for fut in sorted(done, key=lambda f: self.futures[f].seq):
                        job = self.futures.pop(fut)
                        self.ready.append((job, fut.result(), None))
        self.db.save()  # traces of keys that succeeded survive a failed build
        save_json(self.stat_path, self.hasher.cache)
        if "<root>" in self.failed:
            raise self.failed["<root>"]
        for k in ("executed", "verified", "restored", "unchanged"):
            getattr(self.stats, k).sort()
        return self.stats


# --------------------------------------------------------------------------
# A restarting scheduler, for comparison (no traces: every task runs)


def restarting_build(root: str, rules: Rules, tools: dict[str, str], target: str,
                     python: str) -> tuple[int, int]:
    """Bazel-style restarts: a task that needs an unbuilt key is abandoned and rerun later.

    Returns (task starts, actions run).
    """
    eng = Engine(root, rules, os.path.join(root, ".restart-cache"), tools, python=python)
    hasher = FileHasher(root, {})
    done: dict[str, str] = {}
    stack = [target]
    starts = actions = 0
    while stack:
        key = stack[-1]
        if key in done:
            stack.pop()
            continue
        rule = rules.rule_for(key)
        if rule is None:
            done[key] = eng._leaf_value(key) if ":" in key else hasher.digest(key)
            stack.pop()
            continue
        starts += 1
        gen = rule.fn(Ctx(eng), key)
        reply: object = None
        restarted = False
        while True:
            try:
                req = gen.send(reply)
            except StopIteration:
                break
            if isinstance(req, Need):
                missing = [k for k in req.keys if k not in done]
                if missing:  # all missing keys of the batch, like env.getValues()
                    gen.close()
                    stack.extend(reversed(missing))
                    restarted = True
                    break
                reply = [done[k] for k in req.keys]
            elif isinstance(req, Run):
                actions += 1
                reply = eng._execute(req)
                if reply.returncode != 0:
                    raise BuildError(reply.stderr)
        if not restarted:
            done[key] = hasher.digest(key)
            stack.pop()
    return starts, actions
