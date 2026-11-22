---
type: playbook
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "npm, Inc. (GitHub), package.json: overrides, npm CLI documentation (npm/cli, docs/lib/content/configuring-npm/package-json.md), 2026, https://github.com/npm/cli/blob/latest/docs/lib/content/configuring-npm/package-json.md"
  - "npm, Inc. (GitHub), npm ci, npm CLI documentation (npm/cli, docs/lib/content/commands/npm-ci.md), 2026, https://github.com/npm/cli/blob/latest/docs/lib/content/commands/npm-ci.md"
  - "npm, Inc. (GitHub), npm audit: Audit Signatures, npm CLI documentation (npm/cli), 2026, https://github.com/npm/cli/blob/latest/docs/lib/content/commands/npm-audit.md"
  - "npm, Inc. (GitHub), npm config definitions (min-release-age, min-release-age-exclude, before, ignore-scripts), npm/cli workspaces/config/lib/definitions/definitions.js, 2026, https://github.com/npm/cli/blob/latest/workspaces/config/lib/definitions/definitions.js"
  - "npm, Inc. (GitHub), Arborist reify (unpack and build ordering, rollback, optional dependency failure), npm/cli workspaces/arborist/lib/arborist/reify.js, 2026, https://github.com/npm/cli/blob/latest/workspaces/arborist/lib/arborist/reify.js"
  - "npm, Inc. (GitHub), Scripts: lifecycle scripts for npm ci and npm install, npm CLI documentation, 2026, https://github.com/npm/cli/blob/latest/docs/lib/content/using-npm/scripts.md"
  - "npm, Inc. (GitHub), package-lock.json file format (lockfileVersion, integrity, resolved), npm CLI documentation, 2026, https://github.com/npm/cli/blob/latest/docs/lib/content/configuring-npm/package-lock-json.md"
  - "npm, Inc. (GitHub), npm query, npm explain, npm token, npm unpublish, npm deprecate, npm CLI documentation, 2026, https://github.com/npm/cli/tree/latest/docs/lib/content/commands"
  - "npm, Inc. (GitHub), Policies: Unpublish policy, npm Docs, 2026, https://docs.npmjs.com/policies/unpublish"
  - "npm, Inc. (GitHub), Reporting malware in an npm package, npm Docs, 2026, https://docs.npmjs.com/reporting-malware-in-an-npm-package"
  - "GitHub, Strengthening npm security: Important changes to authentication and token management, GitHub Changelog, 2025-09-29, https://github.blog/changelog/2025-09-29-strengthening-npm-security-important-changes-to-authentication-and-token-management/"
  - "GitHub, Our plan for a more secure npm supply chain, GitHub Blog, 2025, https://github.blog/security/supply-chain-security/our-plan-for-a-more-secure-npm-supply-chain/"
  - "pnpm contributors, Settings (pnpm-workspace.yaml): overrides, minimumReleaseAge, pnpm documentation, 2026, https://pnpm.io/settings"
  - "Python Packaging Authority, pip user guide: Constraints Files, and pip NEWS 26.0 and 26.1 (--uploaded-prior-to), pypa/pip, 2026, https://github.com/pypa/pip/blob/main/NEWS.rst"
  - "Python Packaging Authority, pip secure installs and hash-checking mode, pip documentation, 2026, https://pip.pypa.io/en/stable/topics/secure-installs/"
  - "Astral Software Inc., uv: Resolution (override-dependencies, exclude-dependencies, exclude-newer), uv documentation, 2026, https://docs.astral.sh/uv/concepts/resolution/"
  - "Astral Software Inc., uv: Locking and syncing (--locked, --frozen), uv documentation, 2026, https://docs.astral.sh/uv/concepts/projects/sync/"
  - "The Rust Project, cargo yank and cargo update (--precise), The Cargo Book, 2026, https://doc.rust-lang.org/cargo/commands/cargo-yank.html"
  - "The Go Authors, Go Modules Reference: retract, exclude, go mod why, go mod verify, GOSUMDB, go.dev, 2026, https://go.dev/ref/mod"
  - "OpenSSF, OSV Schema and defined database prefixes (MAL), ossf/osv-schema, 2026, https://ossf.github.io/osv-schema/"
  - "OpenSSF, Malicious Packages repository, ossf/malicious-packages, 2026, https://github.com/ossf/malicious-packages"
  - "Google, OSV-Scanner usage (osv-scanner scan -L), google/osv-scanner, 2026, https://google.github.io/osv-scanner/usage/"
  - "OpenVEX Authors, OpenVEX Specification v0.2.0, openvex/spec, 2024, https://github.com/openvex/spec/blob/main/OPENVEX-SPEC.md"
  - "Sigstore Authors, Rekor Log Monitor README (identity monitoring, certIdentities), sigstore/rekor-monitor, 2026, https://github.com/sigstore/rekor-monitor"
  - "GitHub, Secure use reference (Using third-party actions, pinning, secrets), GitHub Docs (github/docs), 2026, https://docs.github.com/en/actions/reference/security/secure-use"
  - "GitHub, GitHub Actions policy now supports blocking and SHA pinning actions, GitHub Changelog, 2025-08-15, https://github.blog/changelog/2025-08-15-github-actions-policy-now-supports-blocking-and-sha-pinning-actions/"
  - "GitHub, GITHUB_TOKEN concepts (token lifetime), GitHub Docs, 2026, https://docs.github.com/en/actions/concepts/security/github_token"
  - "GitHub, gh run list, gh workflow disable, gh cache delete, gh search code, GitHub CLI manual, 2026, https://cli.github.com/manual/"
  - "StepSecurity, Harden-Runner README (egress-policy audit and block), step-security/harden-runner, 2026, https://github.com/step-security/harden-runner"
  - "Amazon Web Services, Updating IAM user access keys, IAM User Guide, 2026, https://docs.aws.amazon.com/IAM/latest/UserGuide/id-credentials-access-keys-update.html"
  - "Amazon Web Services, aws cloudtrail lookup-events (AccessKeyId attribute, 90 days) and aws iam update-access-key, AWS CLI Command Reference, 2026, https://docs.aws.amazon.com/cli/latest/reference/cloudtrail/lookup-events.html"
  - "Ashish Kurmi, axios Compromised on npm - Malicious Versions Drop Remote Access Trojan, StepSecurity, 2026-03-30, https://www.stepsecurity.io/blog/axios-compromised-on-npm-malicious-versions-drop-remote-access-trojan"
  - "Aikido Security, npm debug and chalk packages compromised, Aikido, 2025-09-08, https://www.aikido.dev/blog/npm-debug-and-chalk-packages-compromised"
  - "Cybersecurity and Infrastructure Security Agency, Widespread Supply Chain Compromise Impacting npm Ecosystem (Shai-Hulud), CISA, 2025-09-23, https://www.cisa.gov/news-events/alerts/2025/09/23/widespread-supply-chain-compromise-impacting-npm-ecosystem"
  - "Hila Ramati et al., Shai-Hulud 2.0 Supply Chain Attack: 25K+ Repos Exposing Secrets, Wiz, 2025-11-24, https://www.wiz.io/blog/shai-hulud-2-0-ongoing-supply-chain-attack"
  - "GitHub Advisory Database, tj-actions/changed-files runner memory exfiltration (CVE-2025-30066 / GHSA-mrrh-fwg8-r2c3), GitHub, 2025, https://github.com/advisories/GHSA-mrrh-fwg8-r2c3"
  - "GitHub Advisory Database, ua-parser-js embedded malware (CVE-2021-4229 / GHSA-pjwm-rvh2-c87w), GitHub, 2021, https://github.com/advisories/GHSA-pjwm-rvh2-c87w"
  - "Alexander Nelson et al., Incident Response Recommendations and Considerations for Cybersecurity Risk Management: A CSF 2.0 Community Profile (SP 800-61 Rev. 3), NIST, 2025, https://csrc.nist.gov/pubs/sp/800/61/r3/final"
  - "npm, Inc. (GitHub), npm CLI CHANGELOG (min-release-age added in 11.10.0; min-release-age-exclude in 12.0.0-pre.1), npm/cli, 2026, https://github.com/npm/cli/blob/latest/CHANGELOG.md"
  - "pnpm contributors, pnpm v11.0.0 release notes (minimumReleaseAge default 1440), pnpm/pnpm, 2026-04-28, https://github.com/pnpm/pnpm/releases/tag/v11.0.0"
  - "GitHub, npm security update: Classic token creation disabled and granular token changes, GitHub Changelog, 2025-11-05, https://github.blog/changelog/2025-11-05-npm-security-update-classic-token-creation-disabled-and-granular-token-changes/"
  - "GitHub, npm classic tokens revoked, session-based auth and CLI token management now available, GitHub Changelog, 2025-12-09, https://github.blog/changelog/2025-12-09-npm-classic-tokens-revoked-session-based-auth-and-cli-token-management-now-available/"
  - "GitHub, Searching code (legacy): default branch only, files smaller than 384 KB, GitHub Docs, 2026, https://docs.github.com/en/search-github/searching-on-github/searching-code"
  - "GitHub, About Azure private networking for GitHub-hosted runners in your organization, GitHub Docs, 2026, https://docs.github.com/en/organizations/managing-organization-settings/about-azure-private-networking-for-github-hosted-runners-in-your-organization"
  - "StepSecurity, Harden-Runner action.yml (egress-policy audit or block, allowed-endpoints), step-security/harden-runner, 2026, https://github.com/step-security/harden-runner/blob/main/action.yml"
  - "Amazon Web Services, Revoke IAM role temporary security credentials (aws:TokenIssueTime, AWSRevokeOlderSessions), IAM User Guide, 2026, https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html"
  - "Amazon Web Services, aws iam update-access-key (Inactive means the key cannot be used), AWS CLI Command Reference, 2026, https://docs.aws.amazon.com/cli/latest/reference/iam/update-access-key.html"
  - "OpenSSF Malicious Packages via OSV, MAL-2026-2307 (axios) and MAL-2026-2306 (plain-crypto-js) records, OSV, 2026, https://api.osv.dev/v1/vulns/MAL-2026-2307 and https://api.osv.dev/v1/vulns/MAL-2026-2306"
  - "Python Packaging Authority, pip 7.1.0 changelog (constraints files, #2731), pypa/pip NEWS.rst, 2015-06-30, https://github.com/pypa/pip/blob/main/NEWS.rst"
---

# Playbook: Respond to a Compromised Transitive Dependency

> [!summary] Claim:
> A compromised transitive dependency is four separate questions, and a response that answers only the first one fails: which artifacts contain it (static scan of lockfiles and SBOMs), which machines actually installed it (a time-window question about registry resolution, not about declared dependencies), what those machines could reach (secrets and egress), and what still serves it (mirrors, CI caches, images).
> The order is fixed by what the attacker can still do: stop new installs and cut egress, scope the exposure, revoke the credentials the payload could have read, replace the version with a lock-preserving override, rebuild and prove the bad integrity values are gone, and only then communicate and audit.
> Every step ends with a check that fails loudly, because the artifact most people inspect first, `node_modules` on the affected host, is the one the malware may have rewritten.

The SBOM formats and the vulnerability exchange (VEX) model are owned by [[SBOM-Standards-SPDX-CycloneDX-and-VEX]].
How malicious packages reach a registry in the first place is owned by [[Dependency-Confusion-Typosquatting-and-Registry-Attacks]].
The standing perimeter that makes this incident small (cooldowns, quarantine, internal mirrors) is owned by [[Enterprise-Third-Party-Code-Ingestion-and-Quarantine-Architecture]].
Hardening the pipeline that the payload ran inside is owned by [[Audit-and-Harden-a-GitHub-Actions-Release-Pipeline]] and [[OpenSSF-Scorecard-and-CI-CD-Pipeline-Hardening]].
This playbook owns the incident procedure that uses all of them, from the first advisory to the downstream notice.

## Trigger and Symptoms

Activate this playbook when a package that your builds, images, or developer machines can resolve is reported as malicious, hijacked, or backdoored, whether or not you depend on it directly.
This is a different incident class from a vulnerability: a CVE in a dependency is code that is wrong, and a compromised dependency is code that is hostile and may already have executed with the privileges of whoever ran the install.

Signals that start the clock:
- A registry or advisory feed publishes a malware record.
  OpenSSF's malicious-packages repository feeds OSV records whose IDs start with `MAL-` (the defined OSV database prefix for that repository), so a feed subscription or an `osv-scanner` run against a lockfile can surface the hit before a human reads a blog post.
- A vendor or researcher post names versions and indicators of compromise (IoCs), as StepSecurity did for the axios releases on 2026-03-30.
- A lockfile diff in a routine bot pull request adds a dependency that no changelog mentions.
  In the axios incident the malicious releases added one new runtime dependency, `plain-crypto-js`, that the axios source never imported, so the diff of `package-lock.json` was the earliest local signal.
- Egress or DNS monitoring on build runners flags a connection to a host no baseline run ever contacted.
  StepSecurity reports that Harden-Runner flagged the axios command-and-control callback during an ordinary CI run in an open source repository (vendor-reported).
- A maintainer or registry announces that an account was taken over, even before malware is confirmed.
  Treat an account takeover on a package you resolve as a trigger and downgrade later; the cost asymmetry favors it.

### The reference incident: axios, 2026-03-30 and 2026-03-31

The lab fixtures copy this timeline, because it has every feature that makes the response hard: a transitive payload, a short window, and a takedown that did not end exposure.
All times are UTC and come from the StepSecurity report, a vendor source whose per-event timestamps are partly inferred from registry metadata (the report says so for the removal time).

| Time | Event |
| :--- | :--- |
| 2026-03-30 05:57 | `plain-crypto-js@4.2.0` published, a clean decoy that copies `crypto-js` and builds publishing history. |
| 2026-03-30 23:59 | `plain-crypto-js@4.2.1` published with a `postinstall` hook running `node setup.js`, a cross-platform dropper. |
| 2026-03-31 00:21 | `axios@1.14.1` published through a hijacked maintainer account, adding `plain-crypto-js@4.2.1` as a dependency. |
| 2026-03-31 01:00 | `axios@0.30.4` published, the same injection on the legacy 0.x line. |
| 2026-03-31 ~03:15 | npm removes both axios versions and the `latest` tag returns to `1.14.0`. |
| 2026-03-31 04:26 | npm replaces `plain-crypto-js` with a security-holder stub. |

The live windows the report derives are about 2 hours 53 minutes for `1.14.1`, about 2 hours 15 minutes for `0.30.4`, and about 4 hours 27 minutes for `plain-crypto-js@4.2.1`.
Three facts from that report drive the shape of this playbook:
- Neither axios version contained malicious code itself, so a scan for "axios is vulnerable" logic finds nothing; only the dependency edge and the new package are hostile.
- The malicious versions were published with a stolen long-lived token, outside the project's CI.
  Legitimate 1.x releases carried an npm trusted-publisher (OIDC) record, and the malicious ones had no `trustedPublisher` and no `gitHead` in registry metadata.
  Step 10 turns that into a check.
- The dropper deleted itself and replaced its own `package.json` with a clean stub, so inspecting `node_modules` afterwards shows nothing wrong.
  The evidence is the lockfile, the CI log, and the network log, never the install directory.

As of 2026-11-21 (Both), the registry state matches the report: `npm view plain-crypto-js versions --json` printed a single version, `0.0.1-security.0`, and an OSV query for `axios@1.14.1` returned `MAL-2026-2307` among many unrelated GHSA records (Step 1 shows how to filter).

### Severity classes

Classify before acting, because the class decides whether secrets must rotate.

| Class | Payload runs | Examples | What it can reach | Primary response |
| :--- | :--- | :--- | :--- | :--- |
| S1 install-time | `preinstall`, `install`, or `postinstall` on the machine running the install | axios `plain-crypto-js` dropper (2026-03); Shai-Hulud 2.0 added a `preinstall` variant (2025-11) | Everything in the job or shell: environment, files, tokens, cloud credentials | Full playbook: egress cut, secret rotation, host disposal |
| S2 runtime server-side | When the application imports it, inside your service | A backdoored library loaded by a service | The service's own credentials and data | Rebuild, redeploy, rotate that service's secrets |
| S3 runtime client-side | In the browser or app of your users | chalk, debug and 16 sibling packages (2025-09-08): code intercepting crypto-wallet activity in the browser | Your users, not your CI secrets | Rebuild and redeploy bundles built in the window; notify users; CI secrets rotate only if an install script also ran |

The Aikido report on the September 2025 incident lists 18 packages with more than 2 billion combined weekly downloads and describes browser-side payload code; that is why S3 exposure is defined by which bundles were built and shipped in the window, not by who ran `npm install`.

Not this playbook:
- A known vulnerability in a legitimate release goes through the normal patch process.
- A malicious package that you never resolved is a hunting exercise, not an incident.
- A compromised GitHub Action is the same incident class and the same steps apply, with `uses:` references instead of lockfile entries; [[Case-Study-Codecov-Bash-Uploader-and-TJ-Actions]] covers the mechanism and [[Audit-and-Harden-a-GitHub-Actions-Release-Pipeline]] the hardening.

## Decision Tree

```mermaid
flowchart TD
    Sig["Signal: malware advisory, MAL- record,<br/>vendor post, egress alert, odd lockfile diff"] --> Real{"Step 1: advisory names versions<br/>and a payload class?"}
    Real -->|"unconfirmed, account takeover only"| Hold["Treat as S1 until disproved<br/>freeze resolution, keep scoping"]
    Real -->|confirmed| Cls{"Payload class"}
    Hold --> Cls

    Cls -->|"S3 client-side only"| Bundles["Find bundles built in the window<br/>rebuild, redeploy, notify users"]
    Cls -->|"S1 install-time or S2 runtime"| Contain["Step 2: contain<br/>block resolution, cut runner egress,<br/>revoke publish tokens first"]

    Contain --> Static["Step 3: static scan<br/>lockfiles, SBOMs, caches, images"]
    Static --> Any{"Any artifact contains<br/>a bad name and version?"}
    Any -->|"No hit anywhere"| Prove["Prove the negative:<br/>scan coverage report, resolution logs"] --> Close
    Any -->|Yes| Dyn["Step 4: dynamic window<br/>which runs installed it, via which registry"]

    Dyn --> Ran{"Any install inside the window<br/>or through a cache after removal?"}
    Ran -->|"No install ran"| Fix
    Ran -->|Yes| Host["Step 5: dispose hosts,<br/>read network logs for IoCs"]
    Host --> Rot["Step 6: revoke and reissue secrets<br/>ordered by blast radius"]
    Rot --> Fix["Step 7: lock-preserving override<br/>and deny rule"]
    Bundles --> Fix
    Fix --> Rebuild["Step 8: purge caches and mirrors,<br/>rebuild and redeploy"]
    Rebuild --> Ver{"Step 9: gate passes on every<br/>repo and deployed SBOM?"}
    Ver -->|No| Fix
    Ver -->|Yes| Audit["Step 10: provenance and<br/>transparency log audit"]
    Audit --> Comm["Step 11: downstream notice,<br/>VEX, deprecations"]
    Comm --> Close["Step 12: review, controls backlog,<br/>remove temporary pins on schedule"]
```

## Prerequisites

The response runs on data you must already hold; the first incident is a bad time to learn that lockfiles are not committed or CI logs expire in 30 days.

Required inventories, in order of how much they shorten the incident:
- Lockfiles in version control for every repository, so a static scan is a `grep` over a file you can retrieve at any commit.
  A repository without a committed lockfile resolves fresh on every install, which means the exposure question has to be answered from CI run times (Step 4), not from the repository.
- An SBOM per built artifact (image, bundle, wheel), stored where it can be queried by component identity (package URL, or `purl`).
  Lockfiles describe what you declared; the SBOM of the deployed artifact describes what shipped.
  See [[SBOM-Standards-SPDX-CycloneDX-and-VEX]].
- CI run metadata with start timestamps, the install command that ran, the commit, and the registry the job talked to.
- Network telemetry from build runners: DNS or proxy logs at minimum, connection logs ideally, retained longer than the registry's takedown latency plus your detection latency.
- A map from every secret to the jobs and hosts that can read it.

Tools (as of 2026-11, Both unless noted):

```bash
command -v jq gh npm node python3 curl
npm --version          # 11.20.0 on the lab host; min-release-age needs npm >= 11.10.0 (npm CHANGELOG, 2026-02-11); npm latest is 12.2.3 (as of 2026-11)
gh --version
```

Permissions to confirm before the incident, not during it:
- Org-level rights to disable workflows, delete Actions caches, and edit the allowed-actions policy, which supports blocking with a `!` prefix (GitHub changelog, 2025-08-15).
- Rights on the artifact mirror (Artifactory, Nexus, Verdaccio, or equivalent) to delete cached versions and add a deny rule.
- Rights to revoke tokens in the registries you publish to and in your cloud accounts.
- A way to publish a status update without waiting for approval chains, including a pre-approved downstream notice template (Step 11).

Environment hygiene for the responder:
- Never run `npm install` or `npm ci` without `--ignore-scripts` on a machine that holds credentials while you investigate; `ignore-scripts` defaults to false (as of 2026-11, npm 11.20.0), and an install in the analysis environment is a second exposure.
- Fetch suspect tarballs with `npm pack pkg@ver --ignore-scripts` into an empty scratch directory on an isolated host, and read them with `tar -t`; do not extract and import.

## Commands and Numbers to Know

All entries `(as of 2026-11)`.

| Item | Platform | Value | Why it matters |
| :--- | :--- | :--- | :--- |
| npm unpublish by the owner | Both | Within 72 hours if nothing depends on it; after that only when no dependents, under 300 downloads in the last week, and a single owner | A hijacked package with dependents cannot be removed by its (stolen) owner under this policy; removal for malware is a registry action. |
| npm malware handling | Both | Remove the package, publish a security placeholder, publish an advisory, consider banning the uploader | The placeholder replaces the name, so a later `npm view` shows one placeholder version (observed `0.0.1-security.0` for `plain-crypto-js`). |
| Name and version reuse | Both | A published `name@version` can never be reused, even after unpublish; unpublishing a whole package blocks new versions for 24 hours | A fix is always a new version number, never a re-publish of the old one. |
| `npm ci` | Both | Needs a lockfile, errors if it disagrees with `package.json`, deletes `node_modules`, never writes `package.json` or the lockfile | Lock-driven installs ignore the registry's newer versions, so they are exposed by what the lock says, not by the date. |
| `min-release-age` | Both | npm config, number of days, default `null`; only versions older than that are installed; `before` is the absolute-date form and wins if both are set in one source | Resolution-time defense; it did not apply to lock-driven installs in the lab (Step 4). |
| `min-release-age-exclude` | Both | Package names or globs exempt from the window (and from `before`); only the named package is exempt, not its dependencies; present in npm 11.20.0, added on the 12.x line in 12.0.0-pre.1 | The escape hatch when the window blocks a needed fix; per the npm 12 config docs, `npm audit fix` keeps the vulnerable version, warns, and exits non-zero if the fix is inside the window. |
| npm `overrides` | Both | Root `package.json` only; overrides in dependencies and workspaces are not considered; value can be an exact version, a range, a dist-tag, or `npm:` and Git specifiers; nesting scopes it to a parent | The emergency fix; an override in a workspace member is silently ignored. |
| pnpm `overrides` and `minimumReleaseAge` | Both | `overrides` only at the project root, selectors like `bar@^2.1.0` and `qar@1>zoo`; `minimumReleaseAge` in minutes, documented default `1440`, applies to transitive dependencies | pnpm's equivalents; pnpm 11.0.0 (2026-04-28) made 1440 the default, so pnpm 10 and older default to no delay (pnpm v11.0.0 release notes). |
| pip `--uploaded-prior-to` | Both | Added in pip 26.0 (2026-01-30); pip 26.1 (2026-04-26) accepts a duration such as `P3D`; needs `upload-time` from the index | pip's cooldown-style control; the lab host has pip 26.2.2. |
| pip constraints file | Both | `-c constraints.txt` limits versions and never triggers an install; added in pip 7.1 | The pip analog of an override for exclusion (`badpkg!=1.2.4`-style pins). |
| uv `override-dependencies`, `exclude-dependencies` | Both | In `[tool.uv]`; overrides replace a requirement everywhere (including direct), scoped forms target one parent version; an exclusion beats an override in the same scope | uv's emergency fix. |
| uv `exclude-newer` | Both | Compares each distribution file's upload time to a cutoff; needs PEP 700 `upload-time`; files without it are treated as unavailable unless opted out | Per-file, not per-release; PyPI provides the field for all packages. |
| uv `--locked`, `--frozen` | Both | `--locked` errors if the lock is out of date; `--frozen` uses the lock without checking | CI should fail on drift, not silently re-resolve. |
| `cargo yank` | Both | Removes a version from the index for new resolution but deletes no data and the download link still works; existing `Cargo.lock` files keep using it | Yank does not protect a project that already locked the bad version. |
| `cargo update foo --precise 1.2.3` | Both | Pins one package to one version (or a git revision) | The Cargo lock-preserving fix. |
| Go `retract` | Both | A directive in a new release of the module; retracted versions stay on proxies, builds that already depend on them keep working, `go get` and `go mod tidy` stop choosing them, `go list -m -u` reports them | Retraction informs; it does not remove, so a Go consumer must add an `exclude` or upgrade. |
| Go `exclude` | Both | In the main module's `go.mod` only; since Go 1.16 a requirement on an excluded version is ignored and a higher version is added | The Go emergency fix, owned by the main module. |
| OSV malware IDs | Both | Prefix `MAL`, home database is the OpenSSF malicious-packages repository, record URL `https://api.osv.dev/v1/vulns/<ID>` | One query by name and version returns malware and ordinary advisories together; filter by prefix. |
| `GITHUB_TOKEN` lifetime | Both | Expires when the job finishes or after its effective maximum lifetime | Nothing to rotate, but the token's actions during the job are in the audit log. |
| npm granular write tokens | Both | Maximum 90 days (default 7) for write-enabled granular tokens created from mid-October 2025; classic token creation disabled 2025-11-05 and all classic tokens revoked 2025-12-09, when `npm login` moved to two-hour session tokens (GitHub Changelog) | A stolen publish credential is short-lived only if you moved to granular tokens or trusted publishing. |
| CloudTrail `lookup-events` | Both | Management events from the last 90 days, per Region; `AccessKeyId` is a valid lookup attribute | The bound on how far back an exposed key's activity can be queried this way. |
| Incident scale references | Both | Shai-Hulud: over 500 packages (CISA, 2025-09-23); Shai-Hulud 2.0: about 700 packages and over 25,000 malicious repositories by 2025-11-24 (Wiz); tj-actions/changed-files: over 23,000 repositories, exposure 2025-03-14 to 2025-03-15 (GitHub advisory) | Sizing: expect the next one to be worm-shaped, with credentials stolen from victims reused against other victims. |

## Step-by-Step Execution

Each step ends with a check.
Do not continue past a failed check; go to Failure Modes.
Record the UTC time of every decision in the incident log; Step 4 and Step 11 both need it.
Shell variables set in one step are reused in later ones.

Roles, for a team larger than one: an incident commander who owns the clock and the decisions, a scoping lead (Steps 3 and 4), a credentials lead (Step 6), a build lead (Steps 7 to 9), and a communications lead (Step 11).
NIST SP 800-61 Rev. 3 (April 2025, superseding Rev. 2 of 2012) frames incident response as part of cybersecurity risk management under the CSF 2.0 functions rather than as a standalone cycle, which is the right mental model here: Steps 1 to 2 are detect and respond, Steps 6 to 9 are respond and recover, and Step 12 feeds back into govern and identify.

### Step 1: Build the indicator set from the primary record

Goal: a single machine-readable list of bad `name@version` pairs, IoC hosts and addresses, and the first-publish and last-removal times, taken from a registry or database record rather than a blog.

```bash
OSV=MAL-2026-2307
curl -sS "https://api.osv.dev/v1/vulns/${OSV}" > "/var/tmp/${OSV}.json"
jq -r '.id, .published, (.aliases | join(","))' "/var/tmp/${OSV}.json"
jq -r '.affected[] | "\(.package.ecosystem) \(.package.name) \(.versions | join(","))"' "/var/tmp/${OSV}.json"
jq -r '.database_specific.iocs // {} | to_entries[] | "\(.key): \(.value | join(" "))"' "/var/tmp/${OSV}.json"
```

Query by name and version and keep only malware records:

```bash
curl -sS -X POST https://api.osv.dev/v1/query \
  -d '{"package":{"ecosystem":"npm","name":"axios","version":"1.14.1"}}' \
  | jq -r '.vulns[].id | select(startswith("MAL-"))'
```

On 2026-11-21 the first command printed `MAL-2026-2307`, a published time of 2026-03-31T03:15:49Z, the alias `GHSA-fw8c-xr5c-95f9`, affected versions `0.30.4` and `1.14.1`, and an IoC domain `sfrclak.com` with an address and a URL under `database_specific.iocs`; the query printed `MAL-2026-2307` after the filter, while the unfiltered response also held dozens of ordinary GHSA records for `axios`.
The new package has its own record: a query for `plain-crypto-js@4.2.1` returned `MAL-2026-2306` (alias `GHSA-2x9r-6wxq-hrr7`), whose affected list holds `4.2.1`, the decoy `4.2.0`, and the placeholder `0.0.1-security`, so the record condemns the whole package, not only the payload version.
Malware records are per package, so the two records together are the indicator set, and a record for the headline package alone would have missed the transitive payload.

Record in the log: affected versions, the earliest publish time and the latest removal time (from the vendor timeline if the record lacks them), IoC hosts, and the payload class from the severity table.

**Verification:** `jq -r '.affected[].versions[]' "/var/tmp/${OSV}.json" | sort | wc -l` equals the number of bad versions you expect, and every version appears in the incident log with the ecosystem.
If the advisory is a vendor blog without a record yet, write the pairs into the log by hand and mark them `unconfirmed`.

### Step 2: Contain before you scope

Scoping takes hours; the payload takes seconds (the axios report measures the first callback at about two seconds after `npm install`, vendor-reported).
Contain with actions that are cheap to reverse, in this order.

1. Stop new resolution of the bad versions.
   - On a mirror or proxy: add a deny rule for each bad `name@version` and for the new package as a whole when the package has no legitimate use (`plain-crypto-js` had none).
   - In GitHub Actions: add the bad action to the allowed-actions blocklist with a `!` prefix; the blocklist is evaluated last and overrides any allow (GitHub changelog, 2025-08-15).
   - Pause dependency-update bots, so no bot opens a pull request that re-introduces the version.
2. Cut egress for build runners that may have executed the payload.
   - Self-hosted runners: apply a deny-by-default egress policy at the firewall or proxy and add only the registry and source hosts a build needs.
   - GitHub-hosted runners: in-job egress control needs an agent; Harden-Runner documents an audit mode (`egress-policy: audit`) and a block mode (`egress-policy: block` with an `allowed-endpoints` list), and its README lists GitHub-hosted Windows and macOS runners as audit mode only.
     Without an agent, network-level filtering of GitHub-hosted runners is documented only for runners placed in your Azure VNET through Azure private networking, which GitHub describes as giving full control over outbound network policies and network logs; the deny policy there is yours to write.
   - Everywhere: block the IoC hosts and addresses at DNS, proxy, and firewall.
     CISA's guidance for Shai-Hulud included blocking outbound connections to `webhook.site` domains, the exfiltration endpoint in that incident, and monitoring firewall logs for connections to suspicious domains.
3. Stop the jobs that would run the payload again.

```bash
# disable a workflow so it cannot start (cancel its runs separately)
gh workflow disable "release.yml" --repo ORG/REPO
gh run list --repo ORG/REPO --status in_progress --json databaseId --jq '.[].databaseId' \
  | xargs -I{} gh run cancel {} --repo ORG/REPO
```

4. If your organization publishes packages, revoke publish credentials first, before anything else in the credentials list.
   - Shai-Hulud authenticated to npm as the compromised developer and published malicious versions into the victim's other packages (CISA, 2025-09-23).
   - A stolen publish token turns your incident into everyone else's.
   - `npm token revoke` accepts the full token or a truncated id; the id comes from the JSON output of `npm token list`, not from the abbreviated listing.

**Verification:**
- A test resolution of the bad version through your mirror now fails: `npm view axios@1.14.1 --registry "$MIRROR_URL"` returns a 403 or 404, not metadata.
- `gh workflow list --repo ORG/REPO --all` shows the workflow as disabled, not active.
- From a runner in the affected pool, a connection to an IoC host fails: `curl -sS -m 5 -o /dev/null -w '%{http_code}\n' http://IOC_HOST:PORT/` prints `000`, with a timeout or refusal in the error text.

### Step 3: Static blast radius from lockfiles, SBOMs, and caches

Goal: every artifact that contains a bad pair, with the dependency chain that introduced it.
Scan the whole graph, not the top level.

For one npm lockfile, any depth and any scope:

```bash
jq -r '.packages | to_entries[]
  | select(.key != "")
  | select((.key | test("node_modules/(axios|plain-crypto-js)$")))
  | "\(.key) \(.value.version) \(.value.integrity)"' package-lock.json
```

The pattern matches the end of the key, so `node_modules/legacy-pdf/node_modules/axios` matches as well as `node_modules/axios`.
For a chain from the root, ask npm, which reads the same tree:

```bash
npm explain plain-crypto-js            # chain of dependencies causing it to be installed
npm ls axios --all                     # every copy in the tree, nested duplicates included
npm query '[name="axios"]:semver(1.14.1)'
```

For a CycloneDX SBOM, decode the purl (a scoped package encodes `@` as `%40`):

```bash
jq -r '.components[] | select(.purl | test("^pkg:npm/(%40[^/]+/)?(axios@(0\\.30\\.4|1\\.14\\.1)|plain-crypto-js@4\\.2\\.1)"))
  | "\(.name)@\(.version)"' image-frontend.cdx.json
```

The SBOM's `dependencies` array (`ref` and `dependsOn`) holds the chain.
For other ecosystems the same question has a native command:

```bash
cargo tree -i badcrate@1.2.3          # reverse dependency tree (Cargo)
go mod why -m example.com/bad/mod     # why the main module needs it (Go)
osv-scanner scan -L package-lock.json # known-bad records for every locked version (OSV-Scanner V2)
```

Across an organization, search first, then clone what search cannot see:

```bash
gh search code '"plain-crypto-js"' --owner ORG --filename package-lock.json --limit 1000
```

Code search is an index, not a guarantee: `gh search code` uses the legacy code search engine, which indexes only the default branch and only files smaller than 384 KB on github.com (GitHub Docs, searching code), and a large `package-lock.json` often exceeds that, so for the repositories that matter keep a nightly mirror clone and grep it.

Also scan what is not in git:
- Artifact mirrors and caches: CISA's Shai-Hulud guidance explicitly lists cached versions in artifact repositories and dependency tools.
- Build caches: `~/.npm/_cacache` on developer machines and runners, Docker layers, and CI caches keyed by a lockfile hash (a cache restored by an old key still holds the bad tree).
- Shipped bundles and images: search built output for the IoC host string, since a bundle has no lockfile.
- Vendored copies and `bundleDependencies`, which no resolver override reaches.

Lab fixture results on darwin-arm64:

```text
=== 2. Blast radius: lockfiles and SBOMs ===
image-frontend.cdx.json      axios@1.14.1           via image-frontend > @acme/http-client@2.3.1 > axios@1.14.1
image-frontend.cdx.json      plain-crypto-js@4.2.1  via image-frontend > @acme/http-client@2.3.1 > axios@1.14.1 > plain-crypto-js@4.2.1
image-gateway.cdx.json       clean
legacy-report/package-lock.json axios@0.30.4           via legacy-report > legacy-pdf@1.2.0 > axios@0.30.4
legacy-report/package-lock.json plain-crypto-js@4.2.1  via legacy-report > legacy-pdf@1.2.0 > axios@0.30.4 > plain-crypto-js@4.2.1
svc-api/package-lock.json    clean
svc-web/package-lock.json    axios@1.14.1           via svc-web > @acme/http-client@2.3.1 > axios@1.14.1
svc-web/package-lock.json    plain-crypto-js@4.2.1  via svc-web > @acme/http-client@2.3.1 > axios@1.14.1 > plain-crypto-js@4.2.1
Affected artifacts: 3 of 5
Direct-dependency-only scan, lockfiles: 0 affected
Top-level node_modules-only scan, lockfiles: 1 affected
Full-graph scan, lockfiles: 2 affected
CHECK transitive hit reported with full chain                    PASS
CHECK nested duplicate found only by full-graph scan             PASS
CHECK direct-only scan misses every transitive case              PASS
CHECK clean lockfile and clean SBOM stay clean                   PASS
CHECK scoped purl %40 decodes to scoped name                     PASS
```

What the output proves and what it does not:
- The `svc-web` chain `svc-web > @acme/http-client@2.3.1 > axios@1.14.1 > plain-crypto-js@4.2.1` shows that the application never named either package.
  A scan of declared direct dependencies found 0 of the 2 affected lockfiles.
- `legacy-report` hoists a clean `axios@1.13.0` to the top level and carries `axios@0.30.4` only under `legacy-pdf`; a scan of top-level `node_modules` entries found 1 of 2, and only the full graph found both.
- The SBOM scan decodes the scoped purl `%40acme/http-client` before comparing, and the two clean artifacts stay clean, so the scanner can say "no" as well as "yes".
- The fixture is synthetic: the malicious versions, times, and IoC domain come from the public reports, and every other version and repository is invented.

**Verification:** the scan prints a coverage line, not only hits: how many repositories, lockfiles, and SBOMs it read, and how many it could not read.
"No hits" without a coverage count is not a negative result; a repository whose lockfile you could not parse is unscanned.

### Step 4: Dynamic exposure: who actually installed it

A static hit says the bad pair is in a file.
It does not say a machine installed it, and the absence of a static hit does not say no machine did.
The question that decides secret rotation is which installs resolved or fetched a bad version, which depends on the install command and the registry route.

The rules, each exercised by the lab:

| Install | Resolves from | Exposed when |
| :--- | :--- | :--- |
| `npm install`, no lockfile | The registry, at run time | The run started after first publish and before removal, and a declared range admits a bad version. |
| `npm install`, lockfile satisfies the ranges | The lockfile | The lockfile holds a bad pair (a clean lock keeps the install clean even when the range admits a newer, bad version). |
| `npm ci` | The lockfile | The lockfile at that commit holds a bad pair and the tarball was still fetchable. |
| Any install through a caching mirror | The mirror | The mirror cached the tarball, which means exposure can continue after the public registry removed it. |

Run the computation per CI run, not per repository.
For GitHub Actions, list the runs in the window and read the lockfile at each run's commit:

```bash
START=2026-03-30T23:59:00Z END=2026-03-31T06:00:00Z
gh api "repos/ORG/REPO/actions/runs?created=${START}..${END}&per_page=100" --paginate \
  --jq '.workflow_runs[] | [.id, .head_sha, .created_at, .name, .conclusion] | @tsv' > runs.tsv

while IFS=$'\t' read -r id sha created name conclusion; do
  hit=$(git show "${sha}:package-lock.json" 2>/dev/null \
        | jq -r '[.packages | to_entries[] | select(.key | test("node_modules/(axios|plain-crypto-js)$")) | .value.version] | join(",")')
  printf '%s %s %s bad-entries=%s\n' "$id" "$created" "$name" "${hit:-none}"
done < runs.tsv
```

Widen the end of the window by the mirror's retention behavior, not by the registry's takedown time.
Developer machines have no run list: read the npm cache index (`grep -rl plain-crypto-js ~/.npm/_cacache/index-v5` on a typical Linux or macOS install), shell history, and endpoint telemetry for the IoC host, and treat anyone who ran `npm install` on an affected project in the window as exposed until proven otherwise.

Lab fixture results on darwin-arm64:

```text
=== 3. Exposure window: who actually installed it ===
R-101 svc-web       npm install lock=none  via=public 03-31T01:10Z  EXPOSED: inside the publish window
R-102 svc-web       npm ci      lock=bad   via=public 03-31T02:00Z  EXPOSED: inside the publish window
R-103 svc-web       npm ci      lock=bad   via=public 03-31T09:00Z  failed: tarball already removed from the public registry (install 404, build red)
R-104 svc-web       npm ci      lock=bad   via=mirror 03-31T09:30Z  EXPOSED: served from mirror cache after removal
R-201 svc-api       npm install lock=clean via=public 03-31T01:30Z  clean: no malicious version in the installed set
R-301 svc-batch     npm install lock=none  via=public 03-31T02:30Z  EXPOSED: inside the publish window
R-302 svc-batch     npm install lock=none  via=public 03-30T20:00Z  clean: no malicious version in the installed set
R-303 svc-batch     npm install lock=none  via=public 03-31T05:00Z  clean: no malicious version in the installed set
R-401 legacy-report npm ci      lock=bad   via=public 03-31T01:40Z  EXPOSED: inside the publish window
Exposed runs: R-101, R-102, R-104, R-301, R-401
A 3-day release-age cooldown would have protected: R-101, R-301
A 3-day release-age cooldown would not have protected: R-102, R-104, R-401
CHECK run before the publish time is clean                       PASS
CHECK run after removal on the public registry fails, not exposed PASS
CHECK run after removal through a mirror is exposed              PASS
CHECK clean lockfile blocks a range-admitted bad version         PASS
CHECK unlocked install inside the window is exposed              PASS
CHECK cooldown protects resolution, not lock-driven installs     PASS
```

What the output proves and what it does not:
- `R-302` ran 4 hours before the first publish and `R-303` after the removal; both resolved `axios@1.14.0`, so the date alone separates them.
- `R-201` ran inside the window with a range (`^1.13.0`) that admits `1.14.1`, yet stayed clean because its lock pins `1.14.0`: a committed lockfile is the control, and `npm install` respects it.
- `R-103` ran from a bad lock after the public registry removed the tarball and failed; `R-104` ran from the same lock through a mirror that still held it, and is exposed 6 hours after the takedown.
  The exposure window is publish time to the last cache that can still serve the file, not publish time to takedown.
- The cooldown control (3 days) protected `R-101` and `R-301`, which resolve at run time, and did not protect `R-102`, `R-104`, or `R-401`, which install from a lock that already holds the bad pair.
  A cooldown is a resolution-time defense; once the bad version is in a lockfile, only removing it helps.
- The resolver is a toy: a single version per name, highest visible version, lockfile preferred.
  It models the decision, not npm's tree placement.
- The lab classes a lock-driven run as `failed` when any bad tarball is already gone.
  Real npm may have fetched and unpacked the still-served ones before failing.
  In arborist's `reify`, extraction (`_unpackNewModules`) runs before the build step (`_build`) that runs dependency `preinstall`, `install`, and `postinstall` scripts, and a failed extraction triggers a rollback and rethrows, so dependency lifecycle scripts do not run on a plain failed `npm ci`; the scripts doc says they run after the modules are installed (npm/cli `reify.js`, `scripts.md`, as of 2026-11, both Linux and macOS).
  Two exceptions keep such a run in scope: the root project's own `preinstall` fires before any tarball is fetched, and a missing tarball for an optional dependency is nonfatal in arborist (`_handleOptionalFailure`), so the rest of the install, scripts included, proceeds.

**Verification:** every run in the window has a verdict (`EXPOSED`, `failed`, or `clean`) and a reason, and the number of runs with a verdict equals the number returned by the API; a run you could not classify counts as exposed.

### Step 5: Dispose of exposed hosts and read the network logs

For S1 payloads an installed machine ran attacker code with the installer's privileges.
Ephemeral CI runners are discarded after the job, which is the reason to keep them ephemeral; a long-lived self-hosted runner or a developer laptop is not.

Decision rules:
- Ephemeral runner, job finished: nothing to clean, but its secrets are exposed (Step 6).
- Long-lived runner or build host that ran the install: rebuild from the base image.
  The axios report describes a dropper delivering second-stage payloads for macOS, Windows, and Linux and then deleting itself, so a clean `node_modules` proves nothing.
- Developer machine that ran the install in the window: reimage if any of the following hold: the IoC host appears in its network history, an endpoint agent shows an unexplained process, or you cannot show it had no credentials worth stealing.
- A machine that only ran S3 (client-side) code is not compromised; its users are.

Read the logs for each exposed run and host: the IoC hosts, plus any destination absent from the baseline.
The lab computes the baseline allow-list from clean runs and diffs the exposed runs against it, which also surfaces false positives (an internal mirror host that no clean run used).
The result is in Step 6, because it sets the priority of each secret.

**Verification:** for every exposed run or host there is a line in the log with one of: `C2 connection allowed`, `egress enforced, C2 denied`, `no egress log`, or `disposed, no credentials`.
The third is not an absence of evidence to be treated as clean; it is treated as exfiltration unknown (P0).

### Step 6: Revoke and reissue credentials, ordered by what an attacker can do with them

Scope: the union of the secrets readable by every exposed run and host.
That is a smaller set than "all secrets", and a larger set than "the secrets the job used": the payload reads the environment and the filesystem of the job, so every secret exposed to the job counts.
The tj-actions advisory describes a payload that extracted secrets from the runner worker's process memory, and GitHub's secure-use guidance states that a compromised action in a workflow has access to all secrets configured on the repository.

Order (lab tiers):
1. T0 publish and signing credentials (registry tokens, signing keys, passphrases): revoke first, because they let the attacker poison your downstream consumers, the Shai-Hulud mechanism.
2. T1 cloud credentials: access keys, service account keys, and anything minted from them.
3. T2 source-write credentials: personal access tokens and deploy keys that can push code.
4. T3 everything else: webhooks, read-only tokens, third-party API keys.
5. `GITHUB_TOKEN` needs no rotation: it expires when the job finishes (or at its maximum lifetime), but its actions during the job are in the audit log (below).

Revoke, do not rotate in place.
For an access key: disable it first, so the old key stops working at once, then create the replacement, then delete the old one after its activity is reviewed.

```bash
# AWS IAM user key: disable now, review, delete later
aws iam update-access-key --user-name SVC_USER --access-key-id AKIAIOSFODNN7EXAMPLE --status Inactive
aws iam get-access-key-last-used --access-key-id AKIAIOSFODNN7EXAMPLE

# What did this key do? Management events, last 90 days, per Region
aws cloudtrail lookup-events --region us-east-1 \
  --lookup-attributes AttributeKey=AccessKeyId,AttributeValue=AKIAIOSFODNN7EXAMPLE \
  --start-time 2026-03-30T00:00:00Z --query 'Events[].[EventTime,EventName,Username]' --output text

# GitHub Actions secrets: list, then replace
gh secret list --repo ORG/REPO
printf '%s' "$NEW_VALUE" | gh secret set NAME --repo ORG/REPO
```

The key shown is the AWS documentation example and is not a credential.
Inactive means the key cannot be used (AWS CLI reference for `update-access-key`).
In a normal rotation AWS advises checking last-used information before deleting; in an incident the order is disable first and verify second, accepting the outage.

Then hunt for what the credentials did, because revocation closes the door and does not say who came in:
- Cloud audit logs for the key ids, bounded by the 90-day lookup limit above; export anything older from the trail's own storage before it is needed.
- GitHub audit log and repository list for new repositories, new deploy keys, new workflows, and pushes by tokens in the exposure set.
  CISA describes Shai-Hulud uploading stolen credentials to a public repository named `Shai-Hulud` in the victim's account, and Wiz reports that Shai-Hulud 2.0 exfiltrated cross-victim: your data may be published in a repository on an unaffiliated account.
- Registry audit trails for publishes by exposed accounts.
- Active cloud sessions minted from a revoked key or role still work until they expire.
  On AWS, revoking them is a deny policy on `aws:TokenIssueTime` (the `AWSRevokeOlderSessions` inline policy attached to a role); it affects only temporary credentials, never an IAM user's long-term key, so disabling the key and revoking its sessions are two separate actions (IAM User Guide, revoking role sessions).
  This note verifies only the AWS mechanism; for any other provider, use its documented session revocation and record the action taken in the incident log.
- Tokens another vendor issued to you (npm, PyPI, container registries) are revoked at that vendor; during Shai-Hulud 2.0 (malicious versions uploaded 2025-11-21 to 2025-11-23) GitHub began revoking leaked GitHub tokens at 09:00 UTC on 2025-11-26, and Wiz observed abuse of leaked AWS long-term keys (AKIA) and GitHub PATs from 16:00 UTC that day, so provider-side revocation did not cover cloud keys.

Lab fixture results on darwin-arm64:

```text
=== 4. Secrets and egress ===
Baseline egress allow-list from 3 clean runs: api.github.com:443, github.com:443, registry.npmjs.org:443
R-101 sfrclak.com:8000             allow IOC
R-102 sfrclak.com:8000             allow IOC
R-104 artifacts.internal.example:443 allow unknown-new
R-104 sfrclak.com:8000             allow IOC
R-401 sfrclak.com:8000             deny  IOC
R-101 priority P0 (C2 connection allowed)
R-102 priority P0 (C2 connection allowed)
R-104 priority P0 (C2 connection allowed)
R-301 priority P0 (exfil unknown (no egress log))
R-401 priority P1 (egress enforced, C2 denied)
Rotation order (priority, tier, name):
  P0 T0 NPM_PUBLISH_TOKEN          publish      revoke then reissue [R-101]
  P0 T0 SIGNING_KEY_PASSPHRASE     signing      revoke then reissue [R-301]
  P0 T1 AWS_ACCESS_KEY_ID          cloud        revoke then reissue [R-101,R-102,R-401]
  P0 T1 AWS_SECRET_ACCESS_KEY      cloud        revoke then reissue [R-101,R-102,R-401]
  P0 T3 READONLY_ARTIFACT_TOKEN    other        revoke then reissue [R-104]
  P0 T3 SLACK_WEBHOOK_URL          other        revoke then reissue [R-104,R-301]
  P0 T9 GITHUB_TOKEN               ephemeral    no rotation, expires at job end [R-101,R-102]
Secrets in scope: 7 of 8 seen across all runs
CHECK only secrets of exposed runs are in scope                  PASS
CHECK publish token ranks before cloud keys                      PASS
CHECK no egress log means P0                                     PASS
CHECK enforced egress with denied C2 is P1, not exempt           PASS
CHECK mirror host is flagged unknown-new, not IOC                PASS
```

What the output proves and what it does not:
- Only the secrets of exposed runs are in scope: 7 of the 8 secret names seen across all runs, and `METRICS_API_KEY`, present only in a clean run, is not rotated.
- `NPM_PUBLISH_TOKEN` and `SIGNING_KEY_PASSPHRASE` sort before the cloud keys.
- `R-401` is P1 rather than exempt: egress was enforced and the callback was denied, which lowers the priority, but the payload still executed with `AWS_ACCESS_KEY_ID`, and the lab keeps it in the plan (the AWS pair is in scope through `R-101` and `R-102` as well).
- `R-104` shows an unknown-new destination, the internal mirror host: a baseline diff produces false positives that a human must clear, and the IoC match is what sets priority.
- `R-301` has no egress log and therefore ranks P0 regardless.
- The names are fixtures and match no real provider's key format.

**Verification:** for every secret in the plan there is a revocation time, a replacement time, and a log of the old credential's last-used time; the old credential fails an authenticated call (`aws sts get-caller-identity` with the old key returns an access-denied or invalid-token error).

### Step 7: Replace the version with a lock-preserving override

Goal: the bad version cannot be resolved and no unrelated package moves.
Two properties matter and each has a lab check: the fix must be an exact version (or a range that cannot admit the bad one), and the lockfile must be preserved, not regenerated.

npm, in the root `package.json` (overrides in dependencies and workspaces are ignored):

```json
{
  "overrides": {
    "axios": "1.14.0"
  }
}
```

To scope an override to one parent and leave other consumers alone, nest it (`"@acme/http-client": { "axios": "1.14.0" }`), or key it by a version range of the parent.
Regenerate the lock from the existing one without running any package script:

```bash
npm install --package-lock-only --ignore-scripts
git diff --stat package-lock.json
```

The same intent in other tools:

```bash
# pnpm, in pnpm-workspace.yaml:   overrides:  "axios": "1.14.0"      (root only)
# uv, in pyproject.toml [tool.uv]:  override-dependencies = ["badpkg==1.2.3"]
# pip: constraints never install, they only limit
printf 'badpkg!=1.2.4\n' > constraints.txt && pip install -c constraints.txt -r requirements.txt
# Cargo: pin one crate in Cargo.lock
cargo update badcrate --precise 1.2.3
# Go: in the main module's go.mod:   exclude example.com/bad/mod v1.2.4
```

Choose exact or range with the registry's behavior in mind.
npm removes the bad version for malware, so `^1.14.0` would stop resolving it; the Go proxy keeps retracted versions and the Cargo index keeps yanked ones downloadable, so a range override on those can re-admit the bad version.
The lab shows the difference on a registry that keeps the removed version.

Lab fixture results on darwin-arm64:

```text
=== 6. Emergency override and rollback verification ===
package.json overrides: {"overrides": {"axios": "1.14.0"}}
Removed: axios@1.14.1, plain-crypto-js@4.2.1
Added: axios@1.14.0
Unchanged: 1, collateral upgrades: 0
Control, lock deleted and re-resolved: collateral @acme/http-client 2.3.1->2.4.0
Malicious tarball integrity values left in new lock: 0
Registry that keeps the removed version: range override ^1.14.0 -> axios 1.14.1, exact pin -> axios 1.14.0
Gate svc-web (fixed)  PASS
Gate svc-api          PASS
Gate legacy-report    FAIL axios@0.30.4, plain-crypto-js@4.2.1, integrity:sha512-1rrqoBt, integrity:sha512-OReQcID
CHECK override removes both malicious packages                   PASS
CHECK lock-preserving fix changes only axios                     PASS
CHECK deleting the lock causes collateral upgrades               PASS
CHECK range override re-admits a retained bad version            PASS
CHECK gate fails an unremediated nested lock                     PASS
```

What the output proves and what it does not:
- The lock-preserving fix removes `axios@1.14.1` and `plain-crypto-js@4.2.1` (the injected dependency vanishes because `axios@1.14.0` does not declare it), adds `axios@1.14.0`, and changes nothing else: 0 collateral upgrades.
- The control, deleting the lock and re-resolving at 2026-04-10 with the same override, moved `@acme/http-client` from 2.3.1 to 2.4.0.
  An emergency change that also ships an unrelated upgrade is a second incident waiting to be diagnosed.
- On a registry that keeps the removed version, the range override `^1.14.0` resolves `axios 1.14.1` again, while the exact pin resolves `1.14.0`.
- The gate compares both names and integrity values: `legacy-report` fails it, because its nested copy is not fixed yet, which is the intended result and the remaining work item.
- It does not run npm; the resolver is a toy, so run `npm install --package-lock-only` and read the real diff.

**Verification:** the lockfile diff is exactly the expected packages: `git diff package-lock.json | grep -E '^[-+] *"(node_modules/[^"]+)"'` lists only the bad paths and the replacement, and a search of the new lock for each bad integrity string returns nothing (`grep -c 'sha512-BAD_INTEGRITY' package-lock.json` prints `0`).

### Step 8: Purge caches and mirrors, then rebuild and redeploy

Removing the version from the lock does not remove it from the machines and layers that already hold it.

1. Delete the bad versions from every caching mirror, and keep the deny rule from Step 2 in place.
2. Delete CI caches keyed by the old lockfile hash:

```bash
gh cache list --repo ORG/REPO --limit 100
gh cache delete --all --repo ORG/REPO          # or --ref refs/heads/main for one ref
```

3. Rebuild every artifact built in the window or from a bad lock, from a clean lock and with `npm ci --ignore-scripts` if the build does not need install scripts.
   Redeploy images and bundles; a container image built in the window carries the bad tree regardless of what the repository says today.
4. Rebuild developer environments: delete `node_modules` and the npm cache (`npm cache clean --force`) on exposed machines, or reimage them per Step 5.
5. Add a permanent gate to CI: fail if the lockfile contains a denied name and version or a denied integrity string.
   Run `npm audit signatures`, which verifies registry signatures and provenance attestations of what is installed, in the same job.

**Verification:** for every deployed workload, the SBOM generated from the running image has no bad purl, and the image digest changed from the one built in the window.
`gh cache list` returns no entry whose key was computed from a bad lock.

### Step 9: Prove the rollback across the fleet

A fix verified in one repository is a fix in one repository.
Run the gate (Step 8, item 5) against every repository and every deployed SBOM, on a schedule that is shorter than the incident's remaining duration, and require a green result before closing containment.

```bash
for repo in $(cat repos.txt); do
  git -C "$repo" show HEAD:package-lock.json \
    | jq -e '[.packages | to_entries[] | select(.key | test("node_modules/(axios|plain-crypto-js)$"))
              | select(.value.version | IN("0.30.4","1.14.1","4.2.1"))] | length == 0' >/dev/null \
    && echo "PASS $repo" || echo "FAIL $repo"
done
```

Note the limits of this check: it matches versions per name, so extend it when the advisory lists integrity values or when a name has legitimate versions that share a number with a bad one in another ecosystem.
When the record condemns a whole package, as `MAL-2026-2306` does for `plain-crypto-js`, deny the name at any version rather than only `4.2.1`.
Also re-run the egress and DNS log queries for the IoC hosts over the period since containment: no new connections after the egress cut confirms the cut, and one confirms a missed host.

**Verification:** every repository and every deployed artifact is in the gate's report, `FAIL` count is zero, and the report names how many repositories and artifacts it covered and how many it skipped.

### Step 10: Provenance and transparency log audit

Two questions: how did the bad version get published, and did anything sign or publish in your name that you did not do?

1. Registry metadata for the bad version against its predecessors.
   - Compare who published and by what route.
     In the axios case, legitimate 1.x releases carried a trusted-publisher (OIDC) record and the malicious `1.14.1` had none and no `gitHead` (StepSecurity report).
   - `npm view axios@1.14.1 --json` shows the version's metadata; read `_npmUser` and `gitHead` and compare with the previous release.
   - The lab flags a version that lacks a trusted publisher after earlier releases on the same major line had one.
   - The same signal is silent where a line never had provenance: the fixture's `axios@0.30.4` is not flaggable, which is why the signal supplements, and never replaces, an advisory.
2. Provenance of what you install: `npm audit signatures` verifies registry signatures and provenance attestations for the installed tree.
   A version published with a stolen token has no attestation to verify, which is the absence you are looking for.
3. Your own signing identity in the Sigstore transparency log.
   - Run a Rekor monitor for your workflow identities, using the identity-monitoring configuration of `rekor_monitor` (`monitoredValues.certIdentities` with a `certSubject` regular expression and `issuers`, for example `https://token.actions.githubusercontent.com` for GitHub Actions).
   - Any entry for your identity in the incident window that you cannot map to a run is a signing event you did not authorize; that is a stolen long-lived key or a hijacked workflow identity.
   - The log is tamper-evident, not tamper-proof: monitoring is what makes the evidence useful.
   - What Rekor cannot say is that code is good: it records that a signature happened.
   See [[Sigstore-Keyless-Signing-Cosign-Fulcio-and-Rekor]].
4. For Go modules, `go.sum` and the checksum database (`GOSUMDB`) authenticate that you received the same bytes everyone else received.
   That is consistency, not safety: a malicious version published through a hijacked account is consistent for everyone, so the checksum database does not flag it.
5. Build-time provenance: [[SLSA-Framework-Build-and-Source-Tracks]] and [[In-Toto-Layouts-Link-Metadata-and-DSSE-Attestations]] describe what a provenance policy would have rejected; [[Reproducible-Builds-as-a-Security-Control]] describes the independent rebuild that detects a build-time swap.

**Verification:** a table, one row per bad version, with publisher, route (trusted publisher or token), attestation present or absent, and the matching Rekor result for your identities, so that "published outside CI" is a recorded finding rather than an inference.

### Step 11: Communicate to downstream consumers

Two audiences need different messages: your own organization (what to do now) and consumers of your software (whether they are affected and what to do).
Send both with fixed fields: what happened, who is affected, the exposure window in UTC, what you did, what the reader must do, IoCs, the next update time, and a contact.

Internal status update template:

```text
Subject: INCIDENT [ID] compromised dependency (axios 1.14.1 and 0.30.4, plain-crypto-js 4.2.1)
Status as of 2026-03-31 12:00 UTC: contained, scoping 80 percent complete, rotation in progress.
Affected: repositories svc-web, legacy-report; CI runs R-101, R-102, R-104, R-401 (list attached).
Window (UTC): 2026-03-31 00:21 to 03:15 on the public registry; later through the mirror cache.
Action required now: do not run npm install on affected projects; do not re-enable the release workflow.
Credentials: publish and signing revoked, cloud keys revoked, replacement in progress.
Next update: 2026-03-31 16:00 UTC. Incident commander: NAME. Channel: LINK.
```

Downstream advisory template, for consumers of what you ship:

```text
Title: [Product] versions 1.0.0 contained a compromised dependency (MAL-2026-2307)
Summary: Versions built between 2026-03-31 00:21 and 03:15 UTC resolved a malicious release of axios.
Affected: svc-web 1.0.0. Not affected: svc-api 1.0.0 (the component was not present).
Fixed in: svc-web 1.0.1, built from a clean lockfile and verified.
What to do: upgrade to 1.0.1; rotate any credential that was present in an environment that ran svc-web 1.0.0 installs;
check network logs for the indicators below.
Indicators: host sfrclak.com, address 142.11.206.73 (from the public malware record).
Timeline (UTC): first malicious publish 2026-03-31 00:21; registry removal about 03:15; our containment HH:MM.
Contact: security@example.invalid. Next update: DATE.
```

Machine-readable form: publish an OpenVEX document so consumers' scanners can act on it.
OpenVEX requires `@context`, `@id`, `author`, `timestamp`, `version`, and `statements`; each statement needs a `vulnerability` and a `status` from `not_affected`, `affected`, `fixed`, or `under_investigation`; `not_affected` needs a justification or an impact statement; `affected` must carry an action statement; and the document `version` increments when anything changes.
The lab builds this document and validates it, including negative cases:

```text
=== 7. Downstream notice (OpenVEX 0.2.0) ===
svc-web@1.0.0  affected  -
svc-web@1.0.1  fixed     -
svc-api@1.0.0  not_affected component_not_present
OpenVEX validation problems: 0
CHECK valid document passes the validator                        PASS
CHECK affected without action_statement is rejected              PASS
CHECK not_affected without justification is rejected             PASS
```

If you publish a library that depended on the bad version, publish a fixed release and mark the affected range with `npm deprecate`, whose message shows at install time and leaves builds working, instead of unpublishing (unpublish is limited by the policy above and cannot be undone):

```bash
npm deprecate "@acme/http-client@2.3.1" "Depends on a malicious axios release; upgrade to 2.3.2"
```

For GitHub-hosted projects, a repository security advisory gives consumers a Dependabot-visible record; see the GitHub Advisory Database entries for CVE-2025-30066 and CVE-2021-4229 for the shape (affected versions, patched versions, indicators).

**Verification:** the notice names exact versions and UTC times (not "recent versions"), lists what the reader should do in numbered steps, and the VEX document passes a validator; a second person has read it for statements you cannot support, such as "no credentials were exposed", which you can say only when the evidence in Steps 4 to 6 supports it.

### Step 12: Close and feed back

- Hold a blameless review within a week; the artifact is a timeline with decision times, the detection gap, and the minutes between publish and containment.
- Convert findings into controls, with owners and dates: cooldowns and quarantine in [[Enterprise-Third-Party-Code-Ingestion-and-Quarantine-Architecture]], trusted publishing and token hygiene, pipeline hardening in [[Audit-and-Harden-a-GitHub-Actions-Release-Pipeline]], SHA pinning, and the architecture decisions recorded in [[Supply-Chain-Decision-Records]].
- Remove temporary overrides on a schedule: each override entry gets an owner and an expiry, and it is deleted once the upstream package's own dependency range excludes the bad version and the lock is refreshed.
- Keep the evidence: the incident log, the scan reports, the lab-style per-run table, and the exported logs, for the retention your counsel states.

**Verification:** an issue exists for each control with a due date, and `git grep -n overrides -- package.json` shows no override without a linked ticket.

## Worked Example: Quarantine Response Lab

The lab in `code/08-Software-Supply-Chain/quarantine_response/` runs offline on any host with `python3`, and prints the section results shown in the steps.

1. `run.sh` calls `quarantine_response.py`, which builds all fixtures in memory: a registry with publish and removal times, four repositories (two npm lockfiles built by a toy resolver, one built by hand with a nested duplicate, and one repository with no lockfile at all), two CycloneDX SBOMs, nine CI runs, and an advisory pair modelled on the public OSV records (the fixture lists only `plain-crypto-js@4.2.1`, while the live `MAL-2026-2306` also lists `4.2.0` and the placeholder).
2. Section 2 scans lockfiles and SBOMs with the full-graph scanner and with two naive scanners (direct dependencies only, top-level `node_modules` only) to show what they miss.
3. Section 3 decides exposure per run with the rules in Step 4, then repeats the decision with a 3-day cooldown as a control.
4. Section 4 diffs exposed runs' connections against a baseline built from clean runs and builds the rotation order.
5. Section 5 finds the provenance regression, Section 6 applies the override against the toy resolver and checks the diff, the integrity values, and a gate, and Section 7 validates an OpenVEX document.
6. Every `CHECK` line can fail: it turns the lab's exit status to 1 when the mechanism behind it is absent.

What the lab does not prove: it does not run npm, a registry, or a network; the resolver is a single-version toy; secret names are fixtures; and the incident's malicious versions and times come from public reports rather than from a live capture.
The recorded run on darwin-arm64 passed all `CHECK` lines, and the one `FAIL` in the gate output is the intended result for the unremediated `legacy-report`.

### Verified output

Recorded by `python3 -m _harness.verify --record` on darwin-arm64 on 2026-11-21 with Python 3.15.2; the file is `code/08-Software-Supply-Chain/quarantine_response/expected/darwin-arm64.txt`.
The excerpts in Steps 3, 4, 6, and 11 are exact sections of that file.

## Rollback Procedure

The procedure changes network policy, disables workflows, revokes credentials, and edits dependency files; undo each in the reverse order of risk.

1. **Dependency override.** If the override breaks a build, do not remove it to green the build: keep the deny rule, pin the next-older safe version for the failing package, and fix the break.
   Remove an override only when the upstream range excludes the bad version and the lockfile no longer needs it.
   **Check:** the gate from Step 9 passes after removal.
2. **Workflow disablement.** Re-enable with `gh workflow enable "release.yml" --repo ORG/REPO` only after Step 8 completes and the gate passes.
   **Check:** `gh workflow list --repo ORG/REPO` shows the workflow `active`, and the next run on a branch uses the clean lock.
3. **Egress policy.** Loosen in steps: restore the registry and source hosts first, keep the IoC block permanently, and keep audit-mode logging.
   **Check:** a clean build in the pool completes, and the baseline of destinations has not grown.
4. **Credential revocation is not reversible** and should not be reversed.
   If an outage results, issue a new credential; do not re-enable an old key.
   **Check:** the old credential fails an authenticated call.
5. **Mirror deny rules** stay until the registry has removed the version and the mirror's own metadata cache has expired.
6. **Notices.** A correction is a new notice with an incremented VEX document `version`, never an edit that removes the old statement.

## Failure Modes of the Procedure

| Failure | How it shows up | Recovery |
| :--- | :--- | :--- |
| Scan found nothing but the incident is real | Static scan clean, yet egress logs show the IoC host from a build | The scan missed a repository without a lockfile, a nested copy, a vendored bundle, or a cache; use Step 4 from CI runs and Step 3's cache sources. |
| Direct-only or top-level-only scan | Fewer affected lockfiles than a full-graph scan (0 and 1 versus 2 in the lab) | Scan every `node_modules` path and the SBOM graph. |
| Window ended at takedown | A build after the takedown through a mirror is exposed (`R-104`) | Extend the window to the last cache that served the tarball; purge the mirror. |
| Override ignored | The lock still holds the bad version after `npm install` | The override is in a workspace member or a dependency, not the root `package.json`; move it to the root. |
| Range override re-admits the bad version | The lock resolves the bad version again on a registry that kept it | Use an exact version, or `exclude` (Go), or a constraint with `!=`. |
| Lock regenerated from scratch | The diff shows unrelated upgrades | Restore the old lock, apply the override, and run `npm install --package-lock-only --ignore-scripts` so locked versions are preferred. |
| Fix blocked by a cooldown | `npm audit fix` keeps the vulnerable version and exits non-zero | Add the package to `min-release-age-exclude`, or relax the window for this incident only. |
| Cooldown assumed to protect | A bad version was already in a lockfile | A cooldown acts at resolution; remove the pair from the lock (Step 7). |
| Secret rotated in place | The old value still works for a period | Disable first, then create a replacement; verify the old credential fails. |
| Sessions outlive the key | Activity continues after the key was disabled | Revoke the sessions minted from it; the mechanism is provider-specific. |
| Cache restores the bad tree | A rebuild from a clean commit still contains the bad package | Delete CI caches keyed by the old lockfile hash and purge the mirror. |
| Evidence read from `node_modules` | The install directory looks clean | Use the lockfile, the CI log, the network log, and the tarball in the mirror; the dropper removes its own traces. |
| Egress logs missing | Runs have no connection record | Treat exfiltration as unknown (P0), and fix logging as a control. |
| IoC list incomplete | No IoC match but unexpected destinations exist | Review every destination absent from the baseline; the record's IoCs are a minimum. |
| Notice overstates | "No credentials were exposed" later proves false | State only what the evidence supports, with the evidence's coverage, and commit to the next update time. |

## Pitfalls

> [!warning] The dependency you hunt is not the one you search for
> The axios releases had no malicious code of their own; the hostile package was a new dependency the source never imported.
> Search by the advisory's complete set of affected packages and by lockfile diffs, not by the headline package alone.

> [!warning] A takedown does not end the exposure
> The public registry removed the axios versions at about 03:15 UTC; a mirror or CI cache that fetched them earlier can serve them for as long as it holds them.
> The lab's `R-104` installs 6 hours after removal.

> [!warning] Cooldowns protect resolution, not locks
> A minimum release age stops a fresh resolution from taking a brand-new version.
> It does nothing for a lockfile that already holds the version, and it can block the fix: npm's documentation says `npm audit fix` keeps the vulnerable version and exits non-zero when the fix is inside the window.

> [!warning] Yank and retract are advisories, not removals
> A yanked Cargo version still downloads and keeps working for projects that locked it; a retracted Go version stays on module proxies and in existing builds.
> Neither removes the bad code from a consumer's lock; an exclusion or a precise pin does.

> [!warning] Do not run the install to investigate
> `npm install` runs lifecycle scripts by default.
> Fetch with `npm pack --ignore-scripts` on an isolated host, and run any repair with `--ignore-scripts`.

> [!warning] Provider-side revocation is not complete
> GitHub began revoking leaked GitHub tokens during Shai-Hulud 2.0 on 2025-11-26, and Wiz observed abuse of leaked AWS long-term keys and GitHub PATs from the same afternoon.
> Revoke your cloud keys yourself, on your own clock.

> [!warning] Hash-pinning proves sameness, not goodness
> `npm ci`, `go.sum`, and `--require-hashes` guarantee the bytes match a recorded value.
> A hash recorded after the malicious publish pins the malicious bytes; only a version and integrity deny list, or a clean lock from before, removes them.

## At Scale

At 1,000 to 50,000 engineers the response is a query, not a search: whether you can answer "who has `name@version`" in minutes depends on indexes built before the incident.

```mermaid
flowchart LR
    Feed["MAL feeds, vendor posts,<br/>registry takedowns"] --> Triage["Triage queue<br/>severity class S1 S2 S3"]
    CI["CI builds"] -->|"SBOM per artifact, purl index"| Index["Component index<br/>purl to artifact, repo, build time"]
    Mirror["Registry mirror<br/>quarantine and logs"] -->|"who fetched what, when"| Index
    Runners["Runner egress logs"] --> Hunt["Exposure engine<br/>window plus resolution mode"]
    Triage --> Q["Query by bad pairs"]
    Index --> Q
    Q --> Hunt
    Hunt --> Rot["Credential map:<br/>secret to job to owner"]
    Hunt --> Notice["VEX and advisories<br/>to downstream"]
    Q --> Deny["Deny rule pushed to mirrors,<br/>action blocklist, CI gate"]
```

- **Index by `purl`, keyed to artifact and build time.**
  The query "which artifacts contain `pkg:npm/axios@1.14.1`" must return in seconds from an index fed by SBOMs, as [[SBOM-Standards-SPDX-CycloneDX-and-VEX]] describes; a nightly mirror clone and `grep` is the fallback.
- **Route installs through a mirror with logs.**
  The mirror gives you the "who fetched what, when" answer for Step 4, a single place for the deny rule in Step 2, and the quarantine and cooldown policy in [[Enterprise-Third-Party-Code-Ingestion-and-Quarantine-Architecture]].
  Without it you reconstruct exposure from thousands of runners.
- **Credential maps decide rotation speed.**
  Rotating every secret in the organization by hand takes days; rotating the few a map says were exposed takes hours.
  Prefer short-lived credentials (OIDC-issued cloud roles, npm trusted publishing) because the exposure of a token that expires at job end is bounded by the job; classic long-lived tokens are the failure the axios report points at.
- **Cooldown policy by blast radius.**
  A 1-day to 7-day minimum release age on third-party packages, enforced at the mirror or in `min-release-age`, `minimumReleaseAge`, `--uploaded-prior-to`, or `exclude-newer`, removes the first hours when most malicious releases are live.
  The cost is delayed security fixes, so every ecosystem needs an exclusion path with an owner.
- **Egress controls are the second layer.**
  Deny-by-default egress with an allow-list on build runners turns a stolen secret into an unexfiltrated one in the cases where the destination is not on the list; keep the logs, because the lab's `R-401` shows that a blocked callback lowers priority but does not remove it.
- **Pre-write the communication.**
  At scale the notice goes to many audiences with legal review; templates, approval paths, and a VEX pipeline decide whether the notice is hours or days.

### Decision rules

- Default every incident to S1 until the payload class is confirmed from a primary record.
- If you cannot show which runs installed the version, treat every run in the window as exposed.
- If a secret was exposed to an exposed run, revoke it; the question "was it used" is answered after revocation from logs, not before.
- Prefer an exact-version, lock-preserving override over regenerating the lock; accept a delay of an hour for the diff review.
- Close containment only when the gate covers every repository and deployed artifact and reports its own coverage.

## Practice Drill

> [!tip] Practice Drill: Break each link of the chain
> Copy `code/08-Software-Supply-Chain/quarantine_response/` to a scratch directory and run `bash run.sh`; the output must equal `expected/darwin-arm64.txt` on an Apple silicon macOS host (other platforms: the content should match, since nothing in the lab depends on the platform, but only the darwin-arm64 file was recorded).
> Make one change at a time, predict the output first, and explain the difference.
> 1. Move the `R-201` run's lock from `"clean"` to `"none"`: predict its verdict (the unlocked install at 01:30 resolves `axios@1.14.1`, so `EXPOSED`), predict that the cooldown line now protects it as well, and name the three `CHECK` lines that fail (the clean-lock check, the cooldown check, and the in-scope check, because `METRICS_API_KEY` joins the plan and scope becomes 8 of 8).
> 2. Add `"R-105"`, an `npm ci` run via `"public"` at 03:00 with `lock: "bad"` for `svc-web`: predict `EXPOSED` (both tarballs are still served), then change the time to 03:20 and predict `failed` because `axios@1.14.1` is gone, and explain why that verdict is optimistic: `plain-crypto-js@4.2.1` was still served until 04:26, so a conservative responder keeps a partially fetchable run in scope.
> 3. Change the mirror run `R-104` to `via: "public"`: predict that `R-104` becomes `failed` and drops out of the secret plan, so `READONLY_ARTIFACT_TOKEN` leaves it (6 of 8 in scope, not 7 of 8), and name the three `CHECK` lines that now fail: the mirror-exposure check, the in-scope check that expects that token, and the unknown-new host check.
> 4. Replace the exact override `"1.14.0"` with `"^1.14.0"` in Section 6: on the default registry nothing changes (the bad version was removed), and with `retain_removed=True` the control line shows `1.14.1`; explain which ecosystem behaves like each registry.
> 5. Delete `prefer=prefer` from the fix: predict one collateral upgrade, `@acme/http-client 2.3.1 -> 2.4.0`, and that the `lock-preserving fix` check fails.
> 6. Stretch: add a fifth repository whose only copy of the bad package is a `bundleDependencies` entry inside a dependency tarball, and extend the scanner to read it; explain why a lockfile alone could not show it.
> Target time: under 30 minutes for steps 1 to 5, then run the procedure against a real lockfile with `osv-scanner scan -L package-lock.json` and compare its output with your scanner, and rehearse Step 11 with the templates against [[Drill-Supply-Chain-Detect-Tarball-vs-Git-Tag-Backdoor]].

## Interview Questions

> [!question] L5
> The CISO asks, "Do we use axios 1.14.1?" and a teammate answers by running `grep axios package.json` across the repositories and reporting zero hits.
> What is wrong with that answer, and what do you run?
>
> > [!success]- Answer
> > `package.json` lists only what a project declares, and the dependency can be transitive (`app > http-client > axios`), nested under another package, or in a repository that declares nothing and resolves at install time.
> > Search lockfiles, with a pattern that matches the end of the path, so nested copies match: `jq -r '.packages | to_entries[] | select(.key | test("node_modules/axios$")) | "\(.key) \(.value.version)"' package-lock.json`.
> > Add the SBOMs of deployed images, because a lockfile says what was declared and an SBOM what shipped, and `npm explain plain-crypto-js` for the chain.
> > Report coverage with the answer: how many repositories and artifacts were read and how many could not be.

> [!question] L5
> A package is confirmed malicious, you have no idea who installed it, and your CI uses `npm ci` everywhere.
> Which runs are exposed?
>
> > [!success]- Answer
> > `npm ci` installs exactly what the lockfile says and ignores the registry's newer versions, so a run is exposed only if the lockfile at that commit holds a bad version and the tarball could be fetched.
> > Runs from a clean lockfile are not exposed even inside the window.
> > Runs from a bad lock are exposed while the registry or any mirror cache can serve the tarball; after a public takedown they fail with a not-found error unless a mirror holds the file.
> > Check the lockfile at each run's commit (`git show SHA:package-lock.json`), not today's lockfile, and treat an unclassifiable run as exposed.

> [!question] L5
> You find that three CI jobs installed the malicious version.
> Which credentials do you revoke first, and why that order?
>
> > [!success]- Answer
> > First anything that publishes or signs on your behalf (registry tokens, signing keys), because a stolen publish credential lets the attacker poison your consumers and spread, as Shai-Hulud did with the victims' npm credentials.
> > Then cloud credentials, then source-write tokens, then everything else.
> > Revoke by disabling first (an IAM key set to Inactive stops working at once), create a replacement, and review the old key's activity afterwards from audit logs, for example `aws cloudtrail lookup-events` with the `AccessKeyId` attribute (90 days, per Region).
> > `GITHUB_TOKEN` needs no rotation because it expires when the job finishes.

> [!question] L6
> Predict the output.
> The registry has `axios` versions `1.14.0` (published 2026-03-10) and `1.14.1` (published 2026-03-31T00:21Z, removed 03:15Z).
> A repository with no lockfile and the range `^1.14.0` runs `npm install` at 2026-03-31T02:30Z.
> What does it install with no cooldown, with `min-release-age=3`, and at 05:00Z with no cooldown?
>
> > [!success]- Answer
> > With no cooldown at 02:30Z it installs `1.14.1`: the version is published and not yet removed, and the highest version satisfying `^1.14.0` wins, so it is exposed (lab run `R-301`).
> > With `min-release-age=3` only versions published at least 3 days earlier qualify, so it installs `1.14.0` (lab: `R-101` and `R-301` are protected).
> > At 05:00Z with no cooldown the bad version has been removed, so it installs `1.14.0` (lab: `R-303`).
> > The same repository with a lockfile pinning `1.14.1` is exposed at 02:30Z under every setting of `min-release-age`, because the lock-driven install does not resolve.

> [!question] L6
> Debug this.
> After the incident an engineer adds `"overrides": {"axios": "1.14.0"}` to `packages/web/package.json` in a workspace monorepo, runs `npm install`, and the lockfile still contains `axios@1.14.1`.
> Why, and what is the fix?
>
> > [!success]- Answer
> > npm considers overrides only in the root `package.json`; overrides in installed dependencies, including workspaces, are not considered.
> > The fix is to move the entry to the root `package.json` and run `npm install --package-lock-only --ignore-scripts`, then verify the diff shows `axios` and `plain-crypto-js` removed and nothing else changed.
> > pnpm has the same root-only rule for its `overrides`, and uv's `override-dependencies` is also a project-level setting, so the pattern is general.
> > Verify the result, not the edit: search the new lock for the bad integrity values.

> [!question] L6
> Your team plans to enable a 7-day minimum release age fleet-wide as "the" fix for this class of incident.
> What does it not cover, and what do you add?
>
> > [!success]- Answer
> > It acts only when a version is resolved: a lockfile that already holds a bad version, an `npm ci` of an old commit, and a restored CI cache are untouched (lab: `R-102`, `R-104`, `R-401` stay exposed with a 3-day cooldown).
> > It also blocks urgent security fixes; npm documents that `npm audit fix` keeps the vulnerable version and exits non-zero when the fix is inside the window, so an exclusion list with owners is required.
> > It relies on malicious versions being found within the window, which the axios timeline (about 2 to 4.5 hours live) supports but cannot guarantee.
> > Add a lockfile gate in CI, a mirror with logs and deny rules, deny-by-default egress on build runners, and short-lived publish credentials.

> [!question] L7+
> You publish an npm library used by thousands of consumers; a hijacked maintainer account published a malicious patch release under your name 40 minutes ago.
> Sequence the first hour.
>
> > [!success]- Answer
> > Revoke every publish token and the accounts' sessions first, and rotate the account credentials with phishing-resistant authentication, because the attacker can publish again and a worm-style payload would.
> > Ask the registry to remove the version and request the security placeholder through its malware report, because owner-side unpublish is limited by policy (72 hours and no dependents, or under 300 weekly downloads and a single owner after that).
> > Publish a clean version with a new number (a number can never be reused) from CI with trusted publishing, and `npm deprecate` the bad version with a message naming the fixed one.
> > Publish an advisory with exact versions, UTC window, payload description, and IoCs, and submit a malware record so OSV-based scanners alert.
> > In parallel, audit what the attacker's token could do (other packages, repositories, CI secrets) and treat the incident as one of your own S1 incidents.
> > Compare registry metadata of the malicious release with earlier ones: a missing trusted-publisher record and a missing `gitHead` are the signature of publication outside your pipeline.

> [!question] L7+
> A language-ecosystem question.
> Your fleet has npm, Cargo, and Go services and a malicious version of a shared library was yanked in crates.io and retracted in Go.
> Is that enough, and what is the emergency action per ecosystem?
>
> > [!success]- Answer
> > It is not enough: a yanked version still downloads and projects that locked it keep using it; a retracted Go version stays on proxies and builds that already depend on it keep working, with users only notified by `go list -m -u` or on update.
> > Cargo: `cargo update badcrate --precise <good>` in each project, then commit `Cargo.lock`, and gate on the bad version; Go: add an `exclude` for the bad version to the main module's `go.mod` (since Go 1.16 a requirement on an excluded version is ignored and a higher version is added) and run `go mod tidy`, then `go mod verify`.
> > npm: a root `overrides` entry and a regenerated lock, because npm removes malware but your lockfile still names it.
> > Prefer exact pins and exclusions over ranges for the three ecosystems, since two of three registries keep the bad bytes available.

> [!question] L7+
> Design the standing capability that makes this incident a 30-minute query.
> What do you build, and what do you decline to build?
>
> > [!success]- Answer
> > Build a component index fed by an SBOM for every build, keyed by `purl` and artifact, retained as long as the artifact can run in production; a mirror in front of every registry with request logs, a deny list, and a quarantine and cooldown policy; build-runner egress allow-lists and logs; a credential map from secrets to jobs; and a pre-approved communication and VEX pipeline.
> > Decline to rely on advisories as the only trigger (malware records lag, so run lockfile diff review and registry-metadata checks such as missing provenance), on developer machines as a trusted environment, and on any single control: each of the lab's results shows a different control failing alone (a direct-only scan, a cooldown, a registry takedown).
> > Measure it with drills: an exercise with a synthetic malicious package that must be located across all artifacts, with time to scope, time to rotate, and coverage percentage as the metrics, not the incident counts.
> > The trade-off is cost and friction: cooldowns delay fixes and a mirror is a new availability dependency, so each needs an owner and an exclusion path.

## Further Reading

- npm, package.json `overrides`, https://github.com/npm/cli/blob/latest/docs/lib/content/configuring-npm/package-json.md.
  The exact scope rules for overrides: root only, nested keys, version-keyed parents, replacement specifiers.
- npm, `npm ci`, `npm audit` (audit signatures), and `npm query`, https://github.com/npm/cli/tree/latest/docs/lib/content/commands.
  Frozen installs, signature and provenance verification, and the selector syntax used in Step 3.
- npm config definitions, https://github.com/npm/cli/blob/latest/workspaces/config/lib/definitions/definitions.js.
  `min-release-age`, `min-release-age-exclude`, `before`, and `ignore-scripts` with their interactions.
- npm, Unpublish policy and Reporting malware, https://docs.npmjs.com/policies/unpublish and https://docs.npmjs.com/reporting-malware-in-an-npm-package.
  What a maintainer can remove and what the registry does for malware.
- GitHub Blog and Changelog on npm token and trusted-publishing changes, https://github.blog/changelog/2025-09-29-strengthening-npm-security-important-changes-to-authentication-and-token-management/.
  The 90-day granular token limit and the classic token sunset that bound a stolen publish credential.
- OSV schema and malicious-packages, https://ossf.github.io/osv-schema/ and https://github.com/ossf/malicious-packages.
  The `MAL-` identifier space and the record format used in Step 1.
- OSV-Scanner usage, https://google.github.io/osv-scanner/usage/.
  `osv-scanner scan -L` against lockfiles, for the cross-check in the drill.
- pip NEWS and uv resolution documentation, https://github.com/pypa/pip/blob/main/NEWS.rst and https://docs.astral.sh/uv/concepts/resolution/.
  `--uploaded-prior-to`, `exclude-newer`, constraints, overrides, and exclusions for Python.
- The Cargo Book, `cargo yank` and `cargo update`, https://doc.rust-lang.org/cargo/commands/cargo-yank.html.
  Exactly what a yank does and does not do.
- Go Modules Reference, https://go.dev/ref/mod.
  `retract`, `exclude`, `go mod why`, `go mod verify`, and the checksum database.
- OpenVEX specification, https://github.com/openvex/spec/blob/main/OPENVEX-SPEC.md.
  Required fields, statuses, justifications, and the `affected` action-statement rule used in Step 11.
- Rekor Log Monitor, https://github.com/sigstore/rekor-monitor.
  Identity monitoring for the transparency-log audit in Step 10.
- GitHub secure-use reference, https://docs.github.com/en/actions/reference/security/secure-use.
  Why a compromised action reaches every secret in the job and how pinning limits it.
- StepSecurity, axios compromise report, https://www.stepsecurity.io/blog/axios-compromised-on-npm-malicious-versions-drop-remote-access-trojan.
  The timeline and publisher-metadata forensics behind the lab fixtures.
- CISA, Shai-Hulud alert, https://www.cisa.gov/news-events/alerts/2025/09/23/widespread-supply-chain-compromise-impacting-npm-ecosystem.
  Government containment and credential guidance for a self-replicating npm worm.
- Wiz, Shai-Hulud 2.0, https://www.wiz.io/blog/shai-hulud-2-0-ongoing-supply-chain-attack.
  The `preinstall` variant, cross-victim exfiltration, and how fast leaked cloud keys were abused.
- NIST SP 800-61 Rev. 3, https://csrc.nist.gov/pubs/sp/800/61/r3/final.
  The current incident-response guidance, organized around CSF 2.0.

## Related

- Prerequisites: [[SBOM-Standards-SPDX-CycloneDX-and-VEX]], [[Dependency-Confusion-Typosquatting-and-Registry-Attacks]]
- Next: [[Audit-and-Harden-a-GitHub-Actions-Release-Pipeline]]
- See also: [[Enterprise-Third-Party-Code-Ingestion-and-Quarantine-Architecture]], [[Case-Study-Shai-Hulud-and-Self-Replicating-Registry-Worms]], [[Case-Study-Codecov-Bash-Uploader-and-TJ-Actions]], [[Case-Study-Event-Stream-and-Maintainer-Handoff]], [[Case-Study-XZ-Utils-Backdoor-CVE-2024-3094]], [[Sigstore-Keyless-Signing-Cosign-Fulcio-and-Rekor]], [[Supply-Chain-Decision-Records]]
- Lab: `code/08-Software-Supply-Chain/quarantine_response/`
