#!/usr/bin/env python3
"""Offline lab for the compromised-transitive-dependency playbook.

Every input is a synthetic fixture built in this file. The malicious versions, the publish and
removal times, and the IoC domain are copied from public reports of the 2026-03-30/31 axios
incident (see the note's sources); every other version, repository, run, and secret name is
invented. Nothing here touches the network or a real registry.

Sections printed, each ending in CHECK lines that fail the lab (exit 1) when the mechanism breaks:
  1 advisory      2 blast radius      3 exposure window      4 secrets and egress
  5 provenance    6 override and rollback verification       7 OpenVEX notice
"""

import base64
import hashlib
import json
import re
import sys
import urllib.parse
from datetime import datetime, timedelta

FAILURES = []


def check(name, ok):
    print("CHECK %-58s %s" % (name, "PASS" if ok else "FAIL"))
    if not ok:
        FAILURES.append(name)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sri(name, ver):
    digest = hashlib.sha512(("%s@%s" % (name, ver)).encode()).digest()
    return "sha512-" + base64.b64encode(digest).decode()


# ---------------------------------------------------------------- semver subset

def vt(v):
    return tuple(int(x) for x in v.split("."))


def satisfies(v, rng):
    t = vt(v)
    if rng[0] == "^":
        lo = vt(rng[1:])
        if lo[0] > 0:
            hi = (lo[0] + 1, 0, 0)
        elif lo[1] > 0:
            hi = (0, lo[1] + 1, 0)
        else:
            hi = (0, 0, lo[2] + 1)
    elif rng[0] == "~":
        lo = vt(rng[1:])
        hi = (lo[0], lo[1] + 1, 0)
    else:
        return t == vt(rng)
    return lo <= t < hi


# ---------------------------------------------------------------- fixtures

ADVISORIES = [
    {"id": "MAL-2026-2307", "aliases": ["GHSA-fw8c-xr5c-95f9"],
     "summary": "Malicious code in axios (npm)",
     "affected": [{"ecosystem": "npm", "name": "axios", "versions": ["0.30.4", "1.14.1"]}],
     "iocs": {"domains": ["sfrclak.com"], "ips": ["142.11.206.73"]}},
    {"id": "MAL-2026-2306", "aliases": [],
     "summary": "Malicious code in plain-crypto-js (npm)",
     "affected": [{"ecosystem": "npm", "name": "plain-crypto-js", "versions": ["4.2.1"]}],
     "iocs": {"domains": [], "ips": []}},
]
BAD = {(a["name"], v) for adv in ADVISORIES for a in adv["affected"] for v in a["versions"]}
IOC_HOSTS = {d for adv in ADVISORIES for d in adv["iocs"]["domains"] + adv["iocs"]["ips"]}

# name -> version -> metadata. "unpublished" is when the registry stopped serving the version.
REGISTRY = {
    "@acme/http-client": {
        "2.3.1": {"published": "2026-02-20T10:00:00Z", "deps": {"axios": "^1.14.0"}},
        "2.4.0": {"published": "2026-04-02T10:00:00Z", "deps": {"axios": "^1.14.0"}},
    },
    "axios": {
        "0.30.3": {"published": "2026-02-01T09:00:00Z", "deps": {}, "trusted": False},
        "0.30.4": {"published": "2026-03-31T01:00:00Z", "unpublished": "2026-03-31T03:15:00Z",
                   "deps": {"plain-crypto-js": "4.2.1"}, "trusted": False},
        "1.13.0": {"published": "2026-02-15T09:00:00Z", "deps": {}, "trusted": True},
        "1.14.0": {"published": "2026-03-10T09:00:00Z", "deps": {}, "trusted": True},
        "1.14.1": {"published": "2026-03-31T00:21:00Z", "unpublished": "2026-03-31T03:15:00Z",
                   "deps": {"plain-crypto-js": "4.2.1"}, "trusted": False},
    },
    "plain-crypto-js": {
        "4.2.0": {"published": "2026-03-30T05:57:00Z", "deps": {}},
        "4.2.1": {"published": "2026-03-30T23:59:00Z", "unpublished": "2026-03-31T04:26:00Z", "deps": {}},
    },
}

REPOS = {
    "svc-web": {"roots": {"@acme/http-client": "^2.0.0"}},
    "svc-api": {"roots": {"axios": "^1.13.0"}},
    "svc-batch": {"roots": {"axios": "^1.14.0"}},
    "legacy-report": {"roots": {"axios": "^1.13.0", "legacy-pdf": "^1.0.0"}},
}
NOW = ts("2026-04-10T00:00:00Z")


def visible(meta, at, retain_removed=False):
    if ts(meta["published"]) > at:
        return False
    if not retain_removed and "unpublished" in meta and ts(meta["unpublished"]) <= at:
        return False
    return True


def resolve(roots, at, overrides=None, deny=(), prefer=None, cooldown_days=0, retain_removed=False):
    """Toy single-version resolver: highest visible version satisfying the range, lock-preferring."""
    overrides = overrides or {}
    chosen = {}
    queue = sorted(roots.items())
    while queue:
        name, rng = queue.pop(0)
        rng = overrides.get(name, rng)
        if name in chosen:
            if not satisfies(chosen[name], rng):
                raise ValueError("conflict on %s: %s vs %s" % (name, chosen[name], rng))
            continue
        age = timedelta(days=cooldown_days)
        cands = [v for v, m in REGISTRY[name].items()
                 if visible(m, at, retain_removed) and satisfies(v, rng)
                 and (name, v) not in deny and at - ts(m["published"]) >= age]
        if not cands:
            raise ValueError("no version of %s satisfies %s" % (name, rng))
        pick = max(cands, key=vt)
        if prefer and prefer.get(name) in cands:
            pick = prefer[name]
        chosen[name] = pick
        queue.extend(sorted(REGISTRY[name][pick]["deps"].items()))
    return chosen


def entry(name, ver, deps=None):
    e = {"version": ver,
         "resolved": "https://registry.npmjs.org/%s/-/%s-%s.tgz" % (name, name.split("/")[-1], ver),
         "integrity": sri(name, ver)}
    if deps:
        e["dependencies"] = dict(sorted(deps.items()))
    return e


def make_lock(pkg, roots, chosen):
    pk = {"": {"name": pkg, "version": "1.0.0", "dependencies": dict(sorted(roots.items()))}}
    for n in sorted(chosen):
        pk["node_modules/" + n] = entry(n, chosen[n], REGISTRY[n][chosen[n]]["deps"])
    return {"name": pkg, "version": "1.0.0", "lockfileVersion": 3, "requires": True, "packages": pk}


def build_locks():
    locks = {}
    r = REPOS["svc-web"]["roots"]
    locks["svc-web"] = make_lock("svc-web", r, resolve(r, ts("2026-03-31T01:10:00Z")))
    r = REPOS["svc-api"]["roots"]
    locks["svc-api"] = make_lock("svc-api", r, resolve(r, ts("2026-03-20T00:00:00Z")))
    # legacy-report: hoisted axios 1.13.0 is clean; legacy-pdf carries its own nested axios 0.30.4.
    pk = {
        "": {"name": "legacy-report", "version": "1.0.0",
             "dependencies": {"axios": "^1.13.0", "legacy-pdf": "^1.0.0"}},
        "node_modules/axios": entry("axios", "1.13.0"),
        "node_modules/legacy-pdf": entry("legacy-pdf", "1.2.0", {"axios": "~0.30.0"}),
        "node_modules/legacy-pdf/node_modules/axios": entry("axios", "0.30.4", {"plain-crypto-js": "4.2.1"}),
        "node_modules/legacy-pdf/node_modules/plain-crypto-js": entry("plain-crypto-js", "4.2.1"),
    }
    locks["legacy-report"] = {"name": "legacy-report", "version": "1.0.0", "lockfileVersion": 3,
                              "requires": True, "packages": pk}
    return locks


def purl(name, ver):
    return "pkg:npm/%s@%s" % (urllib.parse.quote(name, safe="/"), ver)


def make_sbom(app, comps, edges):
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
            "metadata": {"component": {"type": "application", "name": app, "bom-ref": "app"}},
            "components": [{"type": "library", "bom-ref": purl(n, v), "name": n, "version": v,
                            "purl": purl(n, v)} for n, v in comps],
            "dependencies": [{"ref": r, "dependsOn": d} for r, d in edges]}


def build_sboms():
    hc, ax, pc = purl("@acme/http-client", "2.3.1"), purl("axios", "1.14.1"), purl("plain-crypto-js", "4.2.1")
    frontend = make_sbom("image-frontend",
                         [("@acme/http-client", "2.3.1"), ("axios", "1.14.1"), ("plain-crypto-js", "4.2.1")],
                         [("app", [hc]), (hc, [ax]), (ax, [pc]), (pc, [])])
    ax0 = purl("axios", "1.14.0")
    gateway = make_sbom("image-gateway", [("axios", "1.14.0")], [("app", [ax0]), (ax0, [])])
    return {"image-frontend": frontend, "image-gateway": gateway}


# ---------------------------------------------------------------- scanners

def lock_find(pk, frm, dep):
    base = frm
    while True:
        cand = (base + "/" if base else "") + "node_modules/" + dep
        if cand in pk:
            return cand
        if not base:
            return None
        base = base[:base.rfind("node_modules/")].rstrip("/")


def pname(path):
    return path.rsplit("node_modules/", 1)[1]


def scan_lock(lock):
    """Full-graph scan: every package entry, with the root-to-package dependency chain."""
    pk = lock["packages"]
    parent = {"": None}
    order = [""]
    for node in order:
        for dep in sorted(pk[node].get("dependencies", {})):
            tgt = lock_find(pk, node, dep)
            if tgt is not None and tgt not in parent:
                parent[tgt] = node
                order.append(tgt)
    hits = []
    for path in sorted(pk):
        if path == "":
            continue
        n, v = pname(path), pk[path]["version"]
        if (n, v) in BAD:
            chain, cur = [], path
            while cur:
                chain.append("%s@%s" % (pname(cur), pk[cur]["version"]))
                cur = parent.get(cur)
            hits.append(("%s@%s" % (n, v), " > ".join([lock["name"]] + chain[::-1])))
    return hits


def scan_lock_top_level_only(lock):
    return [p for p, e in lock["packages"].items()
            if p.count("node_modules/") == 1 and (pname(p), e["version"]) in BAD]


def scan_lock_direct_only(lock):
    pk = lock["packages"]
    return [d for d in pk[""].get("dependencies", {})
            if ("node_modules/" + d) in pk and (d, pk["node_modules/" + d]["version"]) in BAD]


def parse_purl(p):
    m = re.match(r"pkg:npm/([^@]+(?:%40[^@]+)?)@([^@?#]+)", p)
    return urllib.parse.unquote(m.group(1)), m.group(2)


def scan_sbom(bom):
    comps = {c["bom-ref"]: parse_purl(c["purl"]) for c in bom["components"]}
    graph = {d["ref"]: d["dependsOn"] for d in bom["dependencies"]}
    root = bom["metadata"]["component"]["bom-ref"]
    parent, order = {root: None}, [root]
    for node in order:
        for ref in graph.get(node, []):
            if ref not in parent:
                parent[ref] = node
                order.append(ref)
    hits = []
    for ref in sorted(comps):
        if comps[ref] in BAD:
            chain, cur = [], ref
            while cur and cur != root:
                chain.append("%s@%s" % comps[cur])
                cur = parent.get(cur)
            hits.append(("%s@%s" % comps[ref], " > ".join([bom["metadata"]["component"]["name"]] + chain[::-1])))
    return hits


def lock_pairs(lock):
    return {(pname(p), e["version"]) for p, e in lock["packages"].items() if p}


# ---------------------------------------------------------------- CI runs

def conn(*items):
    return [tuple(i.split(" ")) for i in items]


RUNS = [
    {"id": "R-101", "repo": "svc-web", "cmd": "npm install", "lock": "none", "via": "public",
     "started": "2026-03-31T01:10:00Z",
     "secrets": ["NPM_PUBLISH_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"],
     "egress": {"enforced": False, "logged": True,
                "conns": conn("registry.npmjs.org:443 allow", "github.com:443 allow", "sfrclak.com:8000 allow")}},
    {"id": "R-102", "repo": "svc-web", "cmd": "npm ci", "lock": "bad", "via": "public",
     "started": "2026-03-31T02:00:00Z",
     "secrets": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"],
     "egress": {"enforced": False, "logged": True,
                "conns": conn("registry.npmjs.org:443 allow", "sfrclak.com:8000 allow")}},
    {"id": "R-103", "repo": "svc-web", "cmd": "npm ci", "lock": "bad", "via": "public",
     "started": "2026-03-31T09:00:00Z", "secrets": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
     "egress": {"enforced": False, "logged": True, "conns": conn("registry.npmjs.org:443 allow")}},
    {"id": "R-104", "repo": "svc-web", "cmd": "npm ci", "lock": "bad", "via": "mirror",
     "started": "2026-03-31T09:30:00Z", "secrets": ["READONLY_ARTIFACT_TOKEN", "SLACK_WEBHOOK_URL"],
     "egress": {"enforced": False, "logged": True,
                "conns": conn("artifacts.internal.example:443 allow", "sfrclak.com:8000 allow")}},
    {"id": "R-201", "repo": "svc-api", "cmd": "npm install", "lock": "clean", "via": "public",
     "started": "2026-03-31T01:30:00Z",
     "secrets": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "METRICS_API_KEY"],
     "egress": {"enforced": False, "logged": True,
                "conns": conn("registry.npmjs.org:443 allow", "github.com:443 allow")}},
    {"id": "R-301", "repo": "svc-batch", "cmd": "npm install", "lock": "none", "via": "public",
     "started": "2026-03-31T02:30:00Z", "secrets": ["SLACK_WEBHOOK_URL", "SIGNING_KEY_PASSPHRASE"],
     "egress": {"enforced": False, "logged": False, "conns": []}},
    {"id": "R-302", "repo": "svc-batch", "cmd": "npm install", "lock": "none", "via": "public",
     "started": "2026-03-30T20:00:00Z", "secrets": ["SLACK_WEBHOOK_URL"],
     "egress": {"enforced": False, "logged": True,
                "conns": conn("registry.npmjs.org:443 allow", "api.github.com:443 allow")}},
    {"id": "R-303", "repo": "svc-batch", "cmd": "npm install", "lock": "none", "via": "public",
     "started": "2026-03-31T05:00:00Z", "secrets": ["SLACK_WEBHOOK_URL"],
     "egress": {"enforced": False, "logged": True, "conns": conn("registry.npmjs.org:443 allow")}},
    {"id": "R-401", "repo": "legacy-report", "cmd": "npm ci", "lock": "bad", "via": "public",
     "started": "2026-03-31T01:40:00Z", "secrets": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
     "egress": {"enforced": True, "logged": True,
                "conns": conn("registry.npmjs.org:443 allow", "sfrclak.com:8000 deny")}},
]


def installed_set(run, locks, cooldown_days=0):
    at = ts(run["started"])
    if run["lock"] == "none":
        chosen = resolve(REPOS[run["repo"]]["roots"], at, cooldown_days=cooldown_days)
        return set(chosen.items())
    return lock_pairs(locks[run["repo"]]) if run["lock"] == "bad" else lock_pairs(locks["svc-api"])


def judge(run, locks, cooldown_days=0):
    at = ts(run["started"])
    got = installed_set(run, locks, cooldown_days)
    bad = sorted(p for p in got if p in BAD)
    if not bad:
        return "clean", "no malicious version in the installed set"
    removed = [p for p in bad if not visible(REGISTRY[p[0]][p[1]], at)]
    if removed and run["via"] == "public":
        return "failed", "tarball already removed from the public registry (install 404, build red)"
    why = "served from mirror cache after removal" if removed else "inside the publish window"
    return "EXPOSED", why


# ---------------------------------------------------------------- secrets triage

TIERS = [(r"^(NPM_|PYPI_|CARGO_REGISTRY)", 0, "publish"), (r"SIGNING|COSIGN|GPG", 0, "signing"),
         (r"^(AWS_|GCP_|AZURE_)", 1, "cloud"), (r"^GITHUB_TOKEN$", 9, "ephemeral"),
         (r"(PAT|GH_TOKEN)", 2, "source-write"), (r".*", 3, "other")]


def tier(name):
    for pat, rank, label in TIERS:
        if re.search(pat, name):
            return rank, label
    raise AssertionError("unreachable")


def run_priority(run, baseline):
    conns = run["egress"]["conns"]
    if not run["egress"]["logged"]:
        return "P0", "exfil unknown (no egress log)"
    if any(h.split(":")[0] in IOC_HOSTS and a == "allow" for h, a in conns):
        return "P0", "C2 connection allowed"
    new = [(h, a) for h, a in conns if h not in baseline]
    if run["egress"]["enforced"] and all(a == "deny" for _, a in new):
        return "P1", "egress enforced, C2 denied"
    return "P0", "no proof of containment"


# ---------------------------------------------------------------- main

def main():
    locks, sboms = build_locks(), build_sboms()

    print("=== 1. Advisory fixture (OSV-shaped, modelled on public records) ===")
    for adv in ADVISORIES:
        for a in adv["affected"]:
            print("%s %s@%s" % (adv["id"], a["name"], ",".join(a["versions"])))
    print("IoC hosts: %s" % ", ".join(sorted(IOC_HOSTS)))
    check("OSV id prefix is MAL for malware records", all(a["id"].startswith("MAL-") for a in ADVISORIES))

    print("\n=== 2. Blast radius: lockfiles and SBOMs ===")
    found = {}
    for repo in sorted(locks):
        found["%s/package-lock.json" % repo] = scan_lock(locks[repo])
    for img in sorted(sboms):
        found["%s.cdx.json" % img] = scan_sbom(sboms[img])
    for art in sorted(found):
        if not found[art]:
            print("%-28s clean" % art)
        for pkg, chain in found[art]:
            print("%-28s %-22s via %s" % (art, pkg, chain))
    affected = sorted(a for a in found if found[a])
    print("Affected artifacts: %d of %d" % (len(affected), len(found)))
    lock_arts = [("%s/package-lock.json" % r, locks[r]) for r in sorted(locks)]
    direct = [a for a, lk in lock_arts if scan_lock_direct_only(lk)]
    top = [a for a, lk in lock_arts if scan_lock_top_level_only(lk)]
    full = [a for a, lk in lock_arts if scan_lock(lk)]
    print("Direct-dependency-only scan, lockfiles: %d affected" % len(direct))
    print("Top-level node_modules-only scan, lockfiles: %d affected" % len(top))
    print("Full-graph scan, lockfiles: %d affected" % len(full))
    check("transitive hit reported with full chain", any(c.count(" > ") >= 3 for _, c in found["svc-web/package-lock.json"]))
    check("nested duplicate found only by full-graph scan", "legacy-report/package-lock.json" in full
          and "legacy-report/package-lock.json" not in top)
    check("direct-only scan misses every transitive case", len(direct) == 0 and len(full) == 2)
    check("clean lockfile and clean SBOM stay clean", not found["svc-api/package-lock.json"]
          and not found["image-gateway.cdx.json"])
    check("scoped purl %40 decodes to scoped name", parse_purl(purl("@acme/http-client", "2.3.1")) == ("@acme/http-client", "2.3.1"))

    print("\n=== 3. Exposure window: who actually installed it ===")
    verdicts = {}
    for run in RUNS:
        v, why = judge(run, locks)
        verdicts[run["id"]] = v
        print("%s %-13s %-11s lock=%-5s via=%-6s %s  %s" % (
            run["id"], run["repo"], run["cmd"], run["lock"], run["via"], run["started"][5:16] + "Z",
            "%s: %s" % (v, why)))
    exposed = [r for r in RUNS if verdicts[r["id"]] == "EXPOSED"]
    print("Exposed runs: %s" % ", ".join(r["id"] for r in exposed))
    cool = {r["id"]: judge(r, locks, cooldown_days=3)[0] for r in RUNS}
    saved = [r["id"] for r in exposed if cool[r["id"]] != "EXPOSED"]
    print("A 3-day release-age cooldown would have protected: %s" % ", ".join(saved))
    print("A 3-day release-age cooldown would not have protected: %s" % ", ".join(
        r["id"] for r in exposed if cool[r["id"]] == "EXPOSED"))
    check("run before the publish time is clean", verdicts["R-302"] == "clean")
    check("run after removal on the public registry fails, not exposed", verdicts["R-103"] == "failed")
    check("run after removal through a mirror is exposed", verdicts["R-104"] == "EXPOSED")
    check("clean lockfile blocks a range-admitted bad version", verdicts["R-201"] == "clean")
    check("unlocked install inside the window is exposed", verdicts["R-101"] == "EXPOSED" and verdicts["R-301"] == "EXPOSED")
    check("cooldown protects resolution, not lock-driven installs", saved == ["R-101", "R-301"])

    print("\n=== 4. Secrets and egress ===")
    clean_runs = [r for r in RUNS if verdicts[r["id"]] == "clean" and r["egress"]["logged"]]
    baseline = sorted({h for r in clean_runs for h, _ in r["egress"]["conns"]})
    print("Baseline egress allow-list from %d clean runs: %s" % (len(clean_runs), ", ".join(baseline)))
    flagged = []
    for r in exposed:
        for h, a in r["egress"]["conns"]:
            if h not in baseline:
                kind = "IOC" if h.split(":")[0] in IOC_HOSTS else "unknown-new"
                flagged.append((r["id"], h, a, kind))
                print("%s %-28s %-5s %s" % (r["id"], h, a, kind))
    plan = {}
    for r in exposed:
        prio, why = run_priority(r, baseline)
        print("%s priority %s (%s)" % (r["id"], prio, why))
        for s in r["secrets"]:
            rank, label = tier(s)
            cur = plan.get(s)
            plan[s] = (min(prio, cur[0]) if cur else prio, rank, label, (cur[3] if cur else []) + [r["id"]])
    print("Rotation order (priority, tier, name):")
    for s in sorted(plan, key=lambda k: (plan[k][0], plan[k][1], k)):
        prio, rank, label, runs = plan[s]
        action = "no rotation, expires at job end" if label == "ephemeral" else "revoke then reissue"
        print("  %s T%d %-26s %-12s %s [%s]" % (prio, rank, s, label, action, ",".join(sorted(set(runs)))))
    all_secrets = {s for r in RUNS for s in r["secrets"]}
    print("Secrets in scope: %d of %d seen across all runs" % (len(plan), len(all_secrets)))
    check("only secrets of exposed runs are in scope", set(plan) < all_secrets and "READONLY_ARTIFACT_TOKEN" in plan)
    check("publish token ranks before cloud keys", plan["NPM_PUBLISH_TOKEN"][1] < plan["AWS_ACCESS_KEY_ID"][1])
    by_id = {r["id"]: r for r in RUNS}
    check("no egress log means P0", run_priority(by_id["R-301"], baseline)[0] == "P0")
    check("enforced egress with denied C2 is P1, not exempt", run_priority(by_id["R-401"], baseline)[0] == "P1"
          and "AWS_ACCESS_KEY_ID" in plan)
    check("mirror host is flagged unknown-new, not IOC", ("R-104", "artifacts.internal.example:443", "allow", "unknown-new") in flagged)

    print("\n=== 5. Provenance regression in registry metadata ===")
    regress = []
    for name in ("axios",):
        lines = {}
        for v, m in sorted(REGISTRY[name].items(), key=lambda kv: ts(kv[1]["published"])):
            lines.setdefault(vt(v)[0], []).append((v, m))
        for major in sorted(lines):
            seen_trusted = None
            for v, m in lines[major]:
                if m["trusted"]:
                    seen_trusted = v
                elif seen_trusted:
                    regress.append((name, v, seen_trusted))
                    print("%s@%s: no trusted publisher, but %s on the same major line had one" % (name, v, seen_trusted))
                else:
                    print("%s@%s: no trusted publisher and no earlier baseline on major %d" % (name, v, major))
    check("1.x release without OIDC provenance is flagged", ("axios", "1.14.1", "1.14.0") in regress)
    check("signal is silent where the line never had provenance", all(r[1] != "0.30.4" for r in regress))

    print("\n=== 6. Emergency override and rollback verification ===")
    old = locks["svc-web"]
    old_pairs = lock_pairs(old)
    roots = REPOS["svc-web"]["roots"]
    prefer = {n: v for n, v in old_pairs}
    new_chosen = resolve(roots, NOW, overrides={"axios": "1.14.0"}, deny=BAD, prefer=prefer)
    new = make_lock("svc-web", roots, new_chosen)
    new_pairs = lock_pairs(new)
    print("package.json overrides: %s" % json.dumps({"overrides": {"axios": "1.14.0"}}, sort_keys=True))
    print("Removed: %s" % ", ".join("%s@%s" % p for p in sorted(old_pairs - new_pairs)))
    print("Added: %s" % ", ".join("%s@%s" % p for p in sorted(new_pairs - old_pairs)))
    collateral = [p for p in new_pairs - old_pairs if p[0] != "axios"]
    print("Unchanged: %d, collateral upgrades: %d" % (len(old_pairs & new_pairs), len(collateral)))
    fresh = resolve(roots, NOW, overrides={"axios": "1.14.0"}, deny=BAD)
    fresh_collateral = sorted("%s %s->%s" % (n, dict(old_pairs).get(n, "-"), v)
                              for n, v in fresh.items() if dict(old_pairs).get(n) != v and n != "axios")
    print("Control, lock deleted and re-resolved: collateral %s" % ", ".join(fresh_collateral))
    bad_ints = {sri(n, v) for n, v in BAD}
    ints = {e["integrity"] for p, e in new["packages"].items() if p}
    print("Malicious tarball integrity values left in new lock: %d" % len(ints & bad_ints))
    keep = resolve(roots, NOW, overrides={"axios": "^1.14.0"}, prefer=None, retain_removed=True)["axios"]
    pin = resolve(roots, NOW, overrides={"axios": "1.14.0"}, retain_removed=True)["axios"]
    print("Registry that keeps the removed version: range override ^1.14.0 -> axios %s, exact pin -> axios %s" % (keep, pin))

    def gate(lock):
        return sorted(("%s@%s" % p) for p in lock_pairs(lock) if p in BAD) + \
               sorted("integrity:" + e["integrity"][:14] for p, e in lock["packages"].items()
                      if p and e["integrity"] in bad_ints)

    for name, lk in (("svc-web (fixed)", new), ("svc-api", locks["svc-api"]), ("legacy-report", locks["legacy-report"])):
        v = gate(lk)
        print("Gate %-16s %s" % (name, "PASS" if not v else "FAIL " + ", ".join(v)))
    check("override removes both malicious packages", not (new_pairs & BAD) and ("plain-crypto-js", "4.2.1") not in new_pairs)
    check("lock-preserving fix changes only axios", not collateral and new_pairs - old_pairs == {("axios", "1.14.0")})
    check("deleting the lock causes collateral upgrades", len(fresh_collateral) >= 1)
    check("range override re-admits a retained bad version", keep == "1.14.1" and pin == "1.14.0")
    check("gate fails an unremediated nested lock", gate(locks["legacy-report"]) != [] and gate(new) == [])

    print("\n=== 7. Downstream notice (OpenVEX 0.2.0) ===")
    doc = {
        "@context": "https://openvex.dev/ns/v0.2.0",
        "@id": "https://vex.example.invalid/2026/mal-2026-2307",
        "author": "Example Corp Product Security",
        "timestamp": "2026-03-31T12:00:00Z",
        "version": 1,
        "statements": [
            {"vulnerability": {"name": "MAL-2026-2307"}, "products": [{"@id": "pkg:generic/svc-web@1.0.0"}],
             "status": "affected",
             "action_statement": "Upgrade to 1.0.1, rotate credentials, rebuild from a clean lockfile."},
            {"vulnerability": {"name": "MAL-2026-2307"}, "products": [{"@id": "pkg:generic/svc-web@1.0.1"}],
             "status": "fixed"},
            {"vulnerability": {"name": "MAL-2026-2307"}, "products": [{"@id": "pkg:generic/svc-api@1.0.0"}],
             "status": "not_affected", "justification": "component_not_present"},
        ],
    }
    problems = vex_problems(doc)
    for st in doc["statements"]:
        print("%-14s %-9s %s" % (st["products"][0]["@id"].split("/")[-1], st["status"], st.get("justification", "-")))
    print("OpenVEX validation problems: %d" % len(problems))
    broken = json.loads(json.dumps(doc))
    del broken["statements"][0]["action_statement"]
    del broken["statements"][2]["justification"]
    check("valid document passes the validator", problems == [])
    check("affected without action_statement is rejected", any("action_statement" in p for p in vex_problems(broken)))
    check("not_affected without justification is rejected", any("justification" in p for p in vex_problems(broken)))

    print("\n=== Summary ===")
    print("Failures: %d" % len(FAILURES))
    return 1 if FAILURES else 0


def vex_problems(doc):
    out = []
    for k in ("@context", "@id", "author", "timestamp", "version", "statements"):
        if k not in doc:
            out.append("missing document field " + k)
    for i, st in enumerate(doc.get("statements", [])):
        if "name" not in st.get("vulnerability", {}):
            out.append("statement %d: vulnerability name missing" % i)
        status = st.get("status")
        if status not in ("not_affected", "affected", "fixed", "under_investigation"):
            out.append("statement %d: bad status %r" % (i, status))
        if status == "not_affected" and not (st.get("justification") or st.get("impact_statement")):
            out.append("statement %d: not_affected needs justification or impact_statement" % i)
        if status == "affected" and not st.get("action_statement"):
            out.append("statement %d: affected needs action_statement" % i)
    return out


if __name__ == "__main__":
    sys.exit(main())
