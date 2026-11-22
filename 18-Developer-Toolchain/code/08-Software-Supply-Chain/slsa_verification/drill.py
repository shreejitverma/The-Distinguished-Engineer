"""Drill: verify and reject forged SLSA provenance, against a toy Fulcio, toy Rekor, and 13 bundles.

World model (everything is built here, offline, deterministically except for key material):
  * a "Fulcio" CA that issues 10-minute code-signing certificates carrying the Fulcio OID extensions
  * a "Rekor" log: RFC 6962 Merkle tree, open for anyone to append, checkpoint signed by the log key
  * one legitimate DSSE-signed SLSA v1 provenance bundle and 12 forged or misused variants

The verifier evaluates every check independently, so the matrix shows which control catches which forgery.
"""

import base64
import datetime as dt
import json
import re
import sys
import tempfile

import toylog as tl
import toyx509 as x

UTC = dt.timezone.utc
T_ISSUE = dt.datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
LIFETIME = dt.timedelta(minutes=10)

TB_ID = "https://github.com/example-org/trusted-builder/.github/workflows/build_slsa3.yml@refs/tags/v1.0.0"
CI_ID = "https://github.com/example-org/payments-agent/.github/workflows/ci.yml@refs/heads/main"
EVIL_ID = "https://github.com/mallory-labs/payments-agent/.github/workflows/release.yml@refs/tags/v1.4.2"
GH_ISSUER = "https://token.actions.githubusercontent.com"
REPO = "https://github.com/example-org/payments-agent"
EVIL_REPO = "https://github.com/mallory-labs/payments-agent"
TAG = "refs/tags/v1.4.2"
COMMIT = "3f1c9a7e5b2d4c6a8e0f1b3d5c7a9e2f4b6d8c0a"
EVIL_COMMIT = "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a291807"
PRED_V1 = "https://slsa.dev/provenance/v1"
PRED_V02 = "https://slsa.dev/provenance/v0.2"
STMT_TYPE = "https://in-toto.io/Statement/v1"
BUILD_TYPE = "https://example.org/build-types/trusted-builder/v1"
PAYLOAD_TYPE = "application/vnd.in-toto+json"
LOG_ORIGIN = "toy-rekor.example/log"

ARTIFACT = b"payments-agent v1.4.2 release binary\n"
IMPLANT = b"payments-agent v1.4.2 release binary + implant\n"

POLICY = {
    "san": TB_ID,
    "issuer": GH_ISSUER,
    "source_uri": REPO,
    "tag": TAG,
    "predicate_type": PRED_V1,
}

CHECKS = [
    "dsse_signature",
    "cert_chain",
    "cert_window",
    "tlog_checkpoint",
    "tlog_inclusion",
    "tlog_binding",
    "identity_san",
    "identity_issuer",
    "predicate_type",
    "subject_digest",
    "builder_id",
    "source_repo",
    "source_ref",
    "claims_vs_cert",
]


def b64(b):
    return base64.b64encode(b).decode("ascii")


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("ascii")


def epoch(t):
    return int(t.timestamp())


def pae(payload_type, payload):
    pt = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(pt), pt, len(payload), payload)


def statement(artifact, builder_id, repo, ref, commit, predicate_type=PRED_V1):
    return {
        "_type": STMT_TYPE,
        "subject": [{"name": "payments-agent", "digest": {"sha256": x.sha256_hex(artifact)}}],
        "predicateType": predicate_type,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {"repository": repo, "ref": ref},
                "resolvedDependencies": [
                    {"uri": "git+" + repo + "@" + ref, "digest": {"gitCommit": commit}}
                ],
            },
            "runDetails": {"builder": {"id": builder_id}},
        },
    }


class World:
    def __init__(self, work):
        self.work = work
        self.keys = {}
        self.serial = 10

    def key(self, label):
        if label not in self.keys:
            path = x.gen_key(f"{self.work}/{label}.pem")
            self.keys[label] = (path, x.public_der(path))
        return self.keys[label]

    def signer(self, label):
        path, _ = self.key(label)
        return lambda data: x.sign(path, data, self.work)

    def make_ca(self, label, cn):
        _, spki = self.key(label)
        exts = [
            x.extension(x.OID_BASIC_CONSTRAINTS, x.seq(x.boolean(True)), True),
            x.extension(x.OID_KEY_USAGE, x.tlv(0x03, b"\x02\x04"), True),
        ]
        der = x.build_cert(cn, cn, spki, T_ISSUE - dt.timedelta(days=1), T_ISSUE + dt.timedelta(days=365), 1,
                           self.signer(label), exts)
        return {"label": label, "cn": cn, "der": der, "spki": spki, "subject": x.name(cn)}

    def issue(self, ca, leaf_label, san, signer_uri, repo, commit, ref, trigger):
        _, spki = self.key(leaf_label)
        self.serial += 1
        exts = [
            x.extension(x.OID_KEY_USAGE, x.tlv(0x03, b"\x07\x80"), True),
            x.extension(x.OID_EKU, x.seq(x.oid(x.OID_EKU_CODE_SIGNING))),
            x.extension(x.OID_SAN, x.seq(x.tlv(0x86, san.encode("ascii"))), True),
            x.extension(x.OID_ISSUER_V2, x.utf8(GH_ISSUER)),
            x.extension(x.OID_BUILD_SIGNER_URI, x.utf8(signer_uri)),
            x.extension(x.OID_RUNNER_ENV, x.utf8("github-hosted")),
            x.extension(x.OID_SOURCE_REPO_URI, x.utf8(repo)),
            x.extension(x.OID_SOURCE_REPO_DIGEST, x.utf8(commit)),
            x.extension(x.OID_SOURCE_REPO_REF, x.utf8(ref)),
            x.extension(x.OID_BUILD_TRIGGER, x.utf8(trigger)),
        ]
        return x.build_cert(ca["cn"], "", spki, T_ISSUE, T_ISSUE + LIFETIME, self.serial,
                            self.signer(ca["label"]), exts)


# Each scenario: who signs, what the certificate says, what the predicate claims, and what is mutated afterwards.
def scenarios():
    honest = dict(ca="fulcio", san=TB_ID, signer_uri=TB_ID, repo=REPO, commit=COMMIT, ref=TAG, trigger="push",
                  pred_builder=TB_ID, pred_repo=REPO, pred_ref=TAG, pred_commit=COMMIT, pred_type=PRED_V1,
                  signed_at=T_ISSUE + dt.timedelta(minutes=3), deploy=ARTIFACT, signed=ARTIFACT, log="rekor",
                  post=None)

    def s(name, why, fails, **over):
        d = dict(honest)
        d.update(over)
        d.update(name=name, why=why, fails=set(fails))
        return d

    return [
        s("legitimate", "trusted builder, trusted repo, release tag", []),
        s("payload-swapped-after-signing", "subject digest edited to the implant after the build signed",
          ["dsse_signature", "tlog_binding"], deploy=IMPLANT, post="swap_payload"),
        s("genuine-bundle-on-other-binary", "valid bundle replayed next to an implanted binary",
          ["subject_digest"], deploy=IMPLANT),
        s("rogue-fulcio-ca", "attacker's private CA mints the exact trusted SAN",
          ["cert_chain"], ca="rogue"),
        s("attacker-workflow-lies", "attacker repo signs; predicate claims trusted builder and repo",
          ["identity_san", "builder_id", "source_repo", "claims_vs_cert"],
          san=EVIL_ID, signer_uri=EVIL_ID, repo=EVIL_REPO, commit=EVIL_COMMIT),
        s("same-org-ci-workflow", "right org and repo, wrong workflow and ref; predicate claims the release",
          ["identity_san", "builder_id", "source_ref", "claims_vs_cert"],
          san=CI_ID, signer_uri=CI_ID, ref="refs/heads/main", trigger="pull_request"),
        s("trusted-builder-evil-source", "genuine builder run on the attacker's repo; predicate is honest",
          ["source_repo"], repo=EVIL_REPO, commit=EVIL_COMMIT, pred_repo=EVIL_REPO, pred_commit=EVIL_COMMIT),
        s("trusted-builder-branch-build", "genuine builder, right repo, built from a branch not the tag",
          ["source_ref"], ref="refs/heads/dev", pred_ref="refs/heads/dev"),
        s("signed-after-cert-expiry", "log integrated time is an hour after the 10-minute cert expired",
          ["cert_window"], signed_at=T_ISSUE + dt.timedelta(hours=1)),
        s("borrowed-log-entry", "another artifact's valid log entry and proof attached to this bundle",
          ["tlog_binding"], post="borrow_entry"),
        s("corrupted-inclusion-proof", "one bit flipped in the audit path",
          ["tlog_inclusion"], post="flip_proof"),
        s("wrong-predicate-type", "genuine signer, SLSA v0.2 predicate type where policy demands v1",
          ["predicate_type"], pred_type=PRED_V02),
        s("rogue-log-checkpoint", "tree head signed by the attacker's log key",
          ["tlog_checkpoint"], log="rogue"),
    ]


def build_bundles(world):
    fulcio = world.make_ca("fulcio-ca", "toy-fulcio-root")
    rogue = world.make_ca("rogue-ca", "toy-fulcio-root")  # same name, different key
    cas = {"fulcio": fulcio, "rogue": rogue}
    _, rekor_spki = world.key("rekor-log")
    scs = scenarios()
    drafts = []
    for i, sc in enumerate(scs):
        label = f"leaf-{i}"
        cert = world.issue(cas[sc["ca"]], label, sc["san"], sc["signer_uri"], sc["repo"], sc["commit"],
                           sc["ref"], sc["trigger"])
        stmt = statement(sc["signed"], sc["pred_builder"], sc["pred_repo"], sc["pred_ref"], sc["pred_commit"],
                         sc["pred_type"])
        payload = canon(stmt)
        sig = world.signer(label)(pae(PAYLOAD_TYPE, payload))
        body = canon({
            "kind": "dsse",
            "integratedTime": epoch(sc["signed_at"]),
            "certSha256": x.sha256_hex(cert),
            "payloadSha256": x.sha256_hex(payload),
            "sigSha256": x.sha256_hex(sig),
        })
        drafts.append({"sc": sc, "cert": cert, "payload": payload, "sig": sig, "body": body})

    # The log is open: it accepts any signed entry, honest or not. Fillers model unrelated tenants.
    bodies = [canon({"kind": "dsse", "filler": n, "integratedTime": epoch(T_ISSUE + dt.timedelta(minutes=2))})
              for n in range(4)]
    index_of = {}
    for d in drafts:
        index_of[d["sc"]["name"]] = len(bodies)
        bodies.append(d["body"])
    hashes = [tl.leaf_hash(b) for b in bodies]
    root = tl.mth(hashes)
    size = len(hashes)
    cp_text = f"{LOG_ORIGIN}\n{size}\n{b64(root)}\n".encode("ascii")
    cp_sig = {"rekor": world.signer("rekor-log")(cp_text), "rogue": world.signer("rogue-log")(cp_text)}

    def tlog_entry(idx, log):
        return {
            "index": idx,
            "size": size,
            "body": b64(bodies[idx]),
            "proof": [b64(h) for h in tl.audit_path(idx, hashes)],
            "checkpoint": cp_text.decode("ascii"),
            "checkpoint_sig": b64(cp_sig[log]),
        }

    bundles = []
    for d in drafts:
        sc = d["sc"]
        env = {"payloadType": PAYLOAD_TYPE, "payload": b64(d["payload"]),
               "signatures": [{"keyid": "", "sig": b64(d["sig"])}]}
        entry = tlog_entry(index_of[sc["name"]], sc["log"])
        if sc["post"] == "swap_payload":
            stmt = statement(IMPLANT, sc["pred_builder"], sc["pred_repo"], sc["pred_ref"], sc["pred_commit"])
            env["payload"] = b64(canon(stmt))
        elif sc["post"] == "borrow_entry":
            entry = tlog_entry(1, "rekor")
        elif sc["post"] == "flip_proof":
            raw = bytearray(base64.b64decode(entry["proof"][0]))
            raw[0] ^= 0x01
            entry["proof"][0] = b64(bytes(raw))
        bundles.append({"sc": sc, "bundle": {"cert": b64(d["cert"]), "envelope": env, "tlog": entry}})
    trust = {"fulcio_spki": fulcio["spki"], "fulcio_subject": fulcio["subject"], "rekor_spki": rekor_spki}
    info = {"log_size": size, "legit_index": index_of["legitimate"], "fulcio_cert": drafts[0]["cert"],
            "legit_payload": drafts[0]["payload"]}
    return bundles, trust, info


def verify_bundle(bundle, artifact, trust, work):
    """Return {check: bool}. Every check runs; none short-circuits another."""
    r = dict.fromkeys(CHECKS, False)
    env = bundle["envelope"]
    # DSSE: the bytes that are verified are the bytes that are parsed (never re-decode after the check).
    payload = base64.b64decode(env["payload"])
    sig = base64.b64decode(env["signatures"][0]["sig"])
    cert_der = base64.b64decode(bundle["cert"])
    cert = x.parse_cert(cert_der)

    r["dsse_signature"] = env["payloadType"] == PAYLOAD_TYPE and x.verify(
        cert["spki"], pae(env["payloadType"], payload), sig, work)

    r["cert_chain"] = (
        x.verify(trust["fulcio_spki"], cert["tbs"], cert["sig"], work)
        and cert["issuer"] == trust["fulcio_subject"]
        and x.has_code_signing_eku(cert)
        and not x.is_ca(cert)
    )

    entry = bundle["tlog"]
    body = base64.b64decode(entry["body"])
    cp_text = entry["checkpoint"].encode("ascii")
    cp_sig = base64.b64decode(entry["checkpoint_sig"])
    lines = entry["checkpoint"].split("\n")
    cp_ok = len(lines) >= 3 and lines[0] == LOG_ORIGIN and lines[1] == str(entry["size"])
    r["tlog_checkpoint"] = cp_ok and x.verify(trust["rekor_spki"], cp_text, cp_sig, work)
    if cp_ok:
        root = base64.b64decode(lines[2])
        path = [base64.b64decode(h) for h in entry["proof"]]
        r["tlog_inclusion"] = tl.verify_inclusion(tl.leaf_hash(body), entry["index"], entry["size"], path, root)

    try:
        logged = json.loads(body)
        expect = canon({
            "kind": "dsse",
            "integratedTime": logged["integratedTime"],
            "certSha256": x.sha256_hex(cert_der),
            "payloadSha256": x.sha256_hex(payload),
            "sigSha256": x.sha256_hex(sig),
        })
        r["tlog_binding"] = body == expect
        t = dt.datetime.fromtimestamp(logged["integratedTime"], UTC)
        r["cert_window"] = cert["not_before"] <= t <= cert["not_after"]
    except (ValueError, KeyError, TypeError):
        pass

    uris = x.san_uris(cert)
    r["identity_san"] = uris == [POLICY["san"]]
    r["identity_issuer"] = x.ext_string(cert, x.OID_ISSUER_V2) == POLICY["issuer"]

    try:
        st = json.loads(payload)
        pred = st["predicate"]
        ext = pred["buildDefinition"]["externalParameters"]
        dep = pred["buildDefinition"]["resolvedDependencies"][0]
        builder = pred["runDetails"]["builder"]["id"]
        r["predicate_type"] = st["_type"] == STMT_TYPE and st["predicateType"] == POLICY["predicate_type"]
        r["subject_digest"] = any(s["digest"].get("sha256") == x.sha256_hex(artifact) for s in st["subject"])
        r["builder_id"] = builder == POLICY["san"] and builder in uris
        c_repo = x.ext_string(cert, x.OID_SOURCE_REPO_URI)
        c_ref = x.ext_string(cert, x.OID_SOURCE_REPO_REF)
        c_sha = x.ext_string(cert, x.OID_SOURCE_REPO_DIGEST)
        r["source_repo"] = c_repo == POLICY["source_uri"] and ext["repository"] == POLICY["source_uri"]
        r["source_ref"] = c_ref == POLICY["tag"] and ext["ref"] == POLICY["tag"]
        r["claims_vs_cert"] = (ext["repository"] == c_repo and ext["ref"] == c_ref
                               and dep["digest"]["gitCommit"] == c_sha)
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    return r


def merkle_property_check():
    ok = bad_rejected = total = 0
    for n in range(1, 21):
        hashes = [tl.leaf_hash(b"leaf-%d" % i) for i in range(n)]
        root = tl.mth(hashes)
        for m in range(n):
            total += 1
            path = tl.audit_path(m, hashes)
            ok += tl.verify_inclusion(hashes[m], m, n, path, root)
            wrong_root = bytes([root[0] ^ 1]) + root[1:]
            bad_rejected += not tl.verify_inclusion(hashes[m], m, n, path, wrong_root)
    return total, ok, bad_rejected


def regex_demo():
    weak = r"example-org/trusted-builder/.github/workflows/build_slsa3.yml@refs/tags/v.*"
    strict = (r"^https://github\.com/example-org/trusted-builder/\.github/workflows/build_slsa3\.yml"
              r"@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$")
    sans = [
        ("legit", TB_ID),
        ("lookalike-owner", "https://github.com/evil-example-org/trusted-builder/.github/workflows/build_slsa3.yml@refs/tags/v9.9.9"),
        ("tag-suffix", "https://github.com/example-org/trusted-builder/.github/workflows/build_slsa3.yml@refs/tags/vnext"),
    ]
    rows = []
    for label, san in sans:
        # re.search is unanchored, like Go's regexp.MatchString used by cosign and sigstore-go.
        rows.append((label, bool(re.search(weak, san)), bool(re.search(strict, san))))
    return rows


def main():
    with tempfile.TemporaryDirectory() as work:
        world = World(work)
        world.key("rekor-log")
        world.key("rogue-log")
        bundles, trust, info = build_bundles(world)

        print("== 1. The legitimate certificate, decoded from DER ==")
        cert = x.parse_cert(info["fulcio_cert"])
        print(f"SAN (identity)            : {x.san_uris(cert)[0]}")
        for label, o in [("Issuer v2 (1.8)", x.OID_ISSUER_V2), ("Build Signer URI (1.9)", x.OID_BUILD_SIGNER_URI),
                         ("Runner Environment (1.11)", x.OID_RUNNER_ENV), ("Source Repo URI (1.12)", x.OID_SOURCE_REPO_URI),
                         ("Source Repo Digest (1.13)", x.OID_SOURCE_REPO_DIGEST), ("Source Repo Ref (1.14)", x.OID_SOURCE_REPO_REF),
                         ("Build Trigger (1.20)", x.OID_BUILD_TRIGGER)]:
            print(f"{label:26s}: {x.ext_string(cert, o)}")
        life = (cert["not_after"] - cert["not_before"]).total_seconds() / 60
        print(f"validity                  : {cert['not_before'].strftime('%Y-%m-%dT%H:%M:%SZ')} for {life:.0f} minutes")
        print(f"code-signing EKU          : {x.has_code_signing_eku(cert)}")
        text = x.openssl_text(info["fulcio_cert"], work)
        m = re.search(r"URI:(\S+)", text)
        print(f"openssl parses the DER and reads the same SAN: {m is not None and m.group(1) == x.san_uris(cert)[0]}")
        pt = PAYLOAD_TYPE
        print(f"DSSE PAE prefix           : DSSEv1 {len(pt)} {pt} {len(info['legit_payload'])} <payload bytes>")

        print()
        print("== 2. Toy Rekor: RFC 6962 tree, open for anyone to append ==")
        print(f"log size {info['log_size']}, legitimate entry at index {info['legit_index']}")
        legit_proof = bundles[0]["bundle"]["tlog"]["proof"]
        print(f"inclusion proof length for the legitimate entry: {len(legit_proof)} hashes")
        total, ok, rej = merkle_property_check()
        print(f"RFC 9162 verifier, sizes 1..20: {ok}/{total} valid proofs accepted, {rej}/{total} wrong-root proofs rejected")

        print()
        print("== 3. Verification matrix: 13 bundles x 14 independent checks ==")
        print("checks: " + " ".join(CHECKS))
        mismatches = 0
        admitted = 0
        for n, item in enumerate(bundles):
            sc = item["sc"]
            res = verify_bundle(item["bundle"], sc["deploy"], trust, work)
            failed = [c for c in CHECKS if not res[c]]
            if set(failed) != sc["fails"]:
                mismatches += 1
            verdict = "ADMIT " if not failed else "REJECT"
            admitted += not failed
            print(f"S{n:02d} {sc['name']:32s} {verdict} {','.join(failed) if failed else '-'}")
        print(f"admitted {admitted} of {len(bundles)}; failed-check sets match the expectation for every bundle: {mismatches == 0}")

        print()
        print("== 4. Identity regex anchoring (re.search is unanchored, like Go MatchString) ==")
        print(f"{'SAN':16s} weak-regex  strict-regex")
        for label, weak, strict in regex_demo():
            print(f"{label:16s} {'ACCEPT' if weak else 'reject':10s}  {'ACCEPT' if strict else 'reject'}")

        if mismatches or admitted != 1:
            print("LAB FAILURE: verifier no longer separates the legitimate bundle from the forgeries", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
