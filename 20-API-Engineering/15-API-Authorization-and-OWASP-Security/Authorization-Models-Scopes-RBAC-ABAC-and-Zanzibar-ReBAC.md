---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc6749.html"
  - "https://www.rfc-editor.org/rfc/rfc6750.html"
  - "https://www.rfc-editor.org/rfc/rfc8693.html"
  - "https://www.rfc-editor.org/rfc/rfc8707.html"
  - "https://www.rfc-editor.org/rfc/rfc9068.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc9396.html"
  - "https://www.rfc-editor.org/rfc/rfc9700.html"
  - "https://www.usenix.org/system/files/atc19-pang.pdf"
  - "https://csrc.nist.gov/projects/role-based-access-control/faqs"
  - "https://nvlpubs.nist.gov/nistpubs/specialpublications/NIST.SP.800-162.pdf"
  - "https://docs.oasis-open.org/xacml/3.0/xacml-3.0-core-spec-os-en.html"
  - "https://openid.net/specs/authorization-api-1_0.html"
  - "https://api-security.owasp.org/editions/2023/en/0xa1-broken-object-level-authorization"
  - "https://api-security.owasp.org/editions/2023/en/0xa5-broken-function-level-authorization"
  - "https://authzed.com/docs/spicedb/concepts/consistency"
  - "https://openfga.dev/docs/getting-started/setup-openfga/configuration"
  - "https://github.com/advisories/GHSA-8cph-m685-6v6r"
  - "https://github.com/advisories/GHSA-w222-m46c-mgh6"
  - "https://github.com/advisories/GHSA-grjv-gjgr-66g2"
---

# API Authorization Models Compared: OAuth Coarse Scopes vs Role-Based (RBAC) vs Attribute-Based (ABAC) vs Relationship-Based (Zanzibar ReBAC)

> [!summary] Claim
> OAuth scopes cap what a client may do on a user's behalf, RBAC says what kind of operation a job function may perform, ABAC decides from attributes at request time, and Zanzibar-style ReBAC answers "does this user have this relation to this object" from a graph of tuples, so a correct API intersects a scope check at the edge with an object-level ABAC or ReBAC check, fails closed on every unknown, and pins ReBAC reads to a snapshot no older than the content it protects.

Part of [API Authorization and OWASP Security](README.md) in [API Engineering](../README.md).

## TL;DR

Mechanism: a scope is a space-delimited, case-sensitive token granted to a client (`(RFC 6749 Section 3.3)`), a role is a named bundle of (operation, object type) permissions (NIST RBAC), an ABAC policy is rules over subject, object, action, and environment attributes (`NIST SP 800-162 Section 2.2`), and a Zanzibar relation tuple is `<object>#<relation>@<user>` evaluated through userset rewrite rules (Zanzibar paper Sections 2.1 and 2.3.1).
Contract rule: the effective right is the intersection, never the union, of what the token's scope allows the client and what the user may do to that object, and a missing scope is `403` with `error="insufficient_scope"` (`(RFC 6750 Section 3.1)`) while a missing object relation is usually `404` to hide existence (`(RFC 9110 Section 15.5.4)`).
Trap: scopes and roles never name an object, so an API that stops at `docs:read` or `role=viewer` lets any holder read any document, which is OWASP API1:2023 Broken Object Level Authorization; and a ReBAC check evaluated on a stale replica re-admits a user just removed from the ACL (the "new enemy" problem, Zanzibar Section 2.2).
Portability: the lab is pure Python on FastAPI 0.162.0 and Starlette 1.10.0 and ran `17 passed` once on CPython 3.14.6, macOS 27.3 arm64; authorization semantics sit above HTTP, so HTTP/1.1, HTTP/2, and HTTP/3 change nothing but the transport of `Authorization` and `WWW-Authenticate` (`(RFC 9110 Section 11.6.1)`).
When not to use each: do not use scopes as user permissions, do not use RBAC for per-object sharing (role explosion), do not use ABAC when the grant is an explicit share rather than a property, and do not run a Zanzibar service when one tenant column plus row-level security answers every question your API asks ([[Defeating-BOLA-API1-2023-Tenant-Scoped-Queries-and-PostgreSQL-RLS]]).

## Mental model

```mermaid
flowchart LR
    C[Client with<br/>Bearer token] -->|GET /docs/roadmap| PEP1[Edge PEP<br/>token valid? scope docs:read?]
    PEP1 -->|no token or bad token| R401[401 + WWW-Authenticate: Bearer]
    PEP1 -->|scope missing| R403S[403 + error=insufficient_scope]
    PEP1 -->|scope ok| F[Function guard<br/>RBAC: role may read documents?]
    F -->|role lacks permission| R403F[403]
    F --> O{Object check}
    O -->|ABAC PDP| A[rules over subject, resource,<br/>action, environment<br/>attributes from PIP]
    O -->|ReBAC Check| Z[doc:roadmap#can_view@bob<br/>at snapshot >= zookie]
    A -->|Permit only| OK[200]
    Z -->|true| OK
    A -->|Deny, NotApplicable,<br/>Indeterminate| R404[404 hide existence<br/>or 403 if visible]
    Z -->|false, cycle, or depth limit| R404
```

The request crosses three gates in order of increasing specificity.
The edge enforcement point validates the token and compares the token's granted scope with the scopes the route requires, which answers "did the user let this client do this kind of thing", and produces `401` for missing or invalid credentials (`(RFC 9110 Section 15.5.2)`) or `403` with `insufficient_scope` (`(RFC 6750 Section 3.1)`).
The function guard asks the RBAC question "may this job function perform this operation on this type", which is the OWASP API5:2023 Broken Function Level Authorization control ([[Defeating-BFLA-API5-2023-Function-Level-Guards-and-Deny-By-Default-Routers]]).
Only the object check names `doc:roadmap`: an ABAC decision point evaluates rules over attributes fetched from an information point, or a ReBAC engine walks the tuple graph at a snapshot at least as fresh as a stored zookie.
Every arrow that is not an explicit grant ends in a denial, including the two ReBAC failure modes (a cycle under an exclusion and an exhausted depth budget) that the lab turns into denials rather than errors.

## How it works

### OAuth 2.0 scopes: delegation ceilings, not permissions

`scope = scope-token *( SP scope-token )` and `scope-token = 1*( %x21 / %x23-5B / %x5D-7E )` (`(RFC 6749 Section 3.3)`), which is the 94 printable ASCII characters minus `"` and `\`, so 92 legal characters, single `SP` separators, and no empty tokens.
The value is "a list of space-delimited, case-sensitive strings" (`(RFC 6749 Section 3.3)`), so `Docs:Read` and `docs:read` are different scopes and the lab asserts it.
The authorization server "MAY fully or partially ignore the scope requested by the client", and when the issued scope differs from the request it MUST return the `scope` response parameter (`(RFC 6749 Section 3.3)`); a client that assumes it received what it asked for will call endpoints it cannot use.
If the client omits `scope`, the server MUST either process the request with a pre-defined default value or fail it as an invalid scope, and SHOULD document that default (`(RFC 6749 Section 3.3)`).

A scope therefore describes what the resource owner consented to let one client do, not what the user may do.
The defining property is that it is coarse: RFC 9396 states that the scope mechanism "is sufficient to implement static scenarios and coarse-grained authorization requests" but "not sufficient to specify fine-grained authorization requirements" (`(RFC 9396 Section 1)`).
That is why the fine-grained successor, `authorization_details`, is "in JSON notation, an array of objects" with a required `type` and the common fields `locations`, `actions`, `datatypes`, `identifier`, and `privileges` (`(RFC 9396 Section 2)`, `(RFC 9396 Section 2.2)`), and it may travel alongside `scope` "for carrying independent authorization requirements" (`(RFC 9396 Section 3.1)`).
Even `identifier` in RAR names a resource the user consented to share with a client; it does not replace the server's own check that the user may access that resource.

On the resource server, the scope arrives either as a JWT claim or in an introspection response:
- A JWT access token SHOULD include a `scope` claim when the authorization request had a `scope` parameter, and every scope string in it MUST have meaning for the resources in `aud` (`(RFC 9068 Section 2.2.3)`).
- The claim's value is "a JSON string containing a space-separated list of scopes" in the RFC 6749 format, not a JSON array (`(RFC 8693 Section 4.2)`).
- An introspection response returns `scope` the same way, and a RAR grant returns `authorization_details` as a top-level member of the introspection response (`(RFC 9396 Section 9.2)`).

RFC 9068 also lets an authorization server put the SCIM `groups`, `roles`, and `entitlements` attributes in the token as claims, with "No specific vocabulary" for roles and entitlements (`(RFC 9068 Section 2.2.3.1)`), and leaves the actual decision to the resource server, which SHOULD combine them "with any other contextual information" (`(RFC 9068 Section 4)`).
This is where scopes and RBAC meet on the wire, and it is also where they get confused: a `roles` claim is a statement about the user frozen at issuance time, while `scope` is a statement about the client.

Audience binds the scope to one API.
The resource server MUST validate that `aud` contains its own resource indicator and MUST reject the token otherwise (`(RFC 9068 Section 4)`), the client can name the target with the `resource` parameter, an absolute URI without a fragment (`(RFC 8707 Section 2)`), and the authorization server SHOULD audience-restrict the token to it (`(RFC 8707 Section 2)`).
The OAuth security BCP turns this into policy: privileges "SHOULD be restricted to the minimum required", tokens "SHOULD be audience-restricted to a specific resource server", and tokens "SHOULD be restricted to certain resources and actions" using `scope` and `authorization_details` (`(RFC 9700 Section 2.3)`).
Without `aud`, the scope `docs:read` issued for one API is equally valid at every other API that happens to use the same string.

When the scope is insufficient, the resource server SHOULD answer `403 Forbidden` with `error="insufficient_scope"`, and the `scope` attribute of the `WWW-Authenticate` challenge is "a space-delimited list of case-sensitive scope values" naming what the resource requires (`(RFC 6750 Section 3)`, `(RFC 6750 Section 3.1)`).
`invalid_token` is `401` and `invalid_request` is `400` (`(RFC 6750 Section 3.1)`).

Library semantics differ in ways that matter:
- FastAPI accumulates scopes along the dependency chain: `_get_oauth_scopes` copies the parent's scopes and appends the dependency's own, preserving order and dropping duplicates (`(fastapi 0.162.0 fastapi/dependencies/models.py, _get_oauth_scopes)`), and the solved value is handed to the dependency as `SecurityScopes(scopes=...)`, whose `scope_str` is the space-joined list (`(fastapi 0.162.0 fastapi/dependencies/utils.py, solve_dependencies)`, `(fastapi 0.162.0 fastapi/security/oauth2.py, SecurityScopes)`).
  The lab's `PATCH` route requires `docs:write` through a `reader` dependency that requires `docs:read`, so the principal dependency sees `["docs:write", "docs:read"]` and a write-only token gets the challenge `scope="docs:write docs:read"`.
- FastAPI does not check scopes for you: `SecurityScopes` only reports what is required, and your dependency must compare.
  The dependency cache key includes the sorted, de-duplicated scope set whenever the dependency or one of its sub-dependencies uses scopes (`(fastapi 0.162.0 fastapi/dependencies/models.py, _get_cache_key)`), so the same security dependency declared with two different scope lists runs twice in one request.
- `HTTPBearer` with the default `auto_error` raises `401` with `detail="Not authenticated"` and `WWW-Authenticate: Bearer` when the header is missing or not a Bearer credential (`(fastapi 0.162.0 fastapi/security/http.py, HTTPBase.make_not_authenticated_error)`); the lab asserts the header value `Bearer`.
  That bare challenge is weaker than RFC 6750 asks: the `Bearer` scheme "MUST be followed by one or more auth-param values" (`(RFC 6750 Section 3)`), while a request with no credentials SHOULD NOT get an error code (`(RFC 6750 Section 3.1)`), so a `realm` parameter (`Bearer realm="docs"`) satisfies both.
- Starlette's `requires(scopes, status_code=403, redirect=None)` decorator demands every listed scope (`has_required_scope` returns `False` on the first absent one) and raises a bare `HTTPException(status_code=403)` without any `WWW-Authenticate` challenge, or redirects with `303` when `redirect` is set (`(starlette 1.10.0 starlette/authentication.py, requires)`).
- Authlib's `TokenValidator.scope_insufficient(token_scopes, required_scopes)` treats `required_scopes` as a list of alternatives, each a space-separated AND set: `["docs:read docs:write", "admin"]` means `(docs:read AND docs:write) OR admin` (`(authlib 1.9.5 authlib/oauth2/rfc6749/resource_protector.py, TokenValidator.scope_insufficient)`), and it splits the token's scope with `str.split()` (`(authlib 1.9.5 authlib/oauth2/rfc6749/util.py, scope_to_list)`), which accepts the `TAB` and double space that the RFC grammar forbids; the lab asserts both behaviors.
- Authlib's RFC 9068 validator reuses the same function for `groups`, `roles`, and `entitlements` but raises `InvalidTokenError` (`401`) rather than `InsufficientScopeError` (`403`) when they are insufficient (`(authlib 1.9.5 authlib/oauth2/rfc9068/token_validator.py, JWTBearerTokenValidator.validate_token)`), so a client sees "get a new token" for what is really "you lack the role".

### RBAC: permissions on types, assigned through roles

The NIST model, standardized as ANSI INCITS 359-2004 and revised as INCITS 359-2012, has a reference model of four components: Core RBAC, Hierarchical RBAC, Static Separation of Duty, and Dynamic Separation of Duty (NIST CSRC RBAC FAQ, fetched 2026-11-15).
In Core RBAC "A role is essentially a collection of permissions", permissions attach to roles and never directly to users, and a session "allows activation of a subset of roles assigned to a user" (NIST CSRC RBAC FAQ).
A permission is an operation on an object, and in API practice the object is a type (`write` on `document`), which is the whole point: assignment scales with job functions, not with objects.

Hierarchies make seniors inherit juniors' permissions; RBAC1 "incorporates hierarchies as a partial order relationship between roles", while the earlier Ferraiolo-Kuhn model used a rooted tree (NIST CSRC RBAC FAQ).
The lab computes the transitive closure on every check and rejects a hierarchy that is not a partial order at construction time (`role hierarchy cycle: a -> b -> a`), because a cycle makes every role in it equivalent, silently granting the union of their permissions.

Static separation of duty limits how many roles of a conflicting set one user may hold, and with a hierarchy the inherited roles count.
The lab's `finance_lead` inherits `approver`, so assigning `finance_lead` plus `requester` violates the SSD set `{approver, requester}` with limit 2 even though `approver` was never assigned directly; a check that looked only at assigned roles would miss it.
Dynamic separation of duty applies the same limit to the roles activated in one session rather than assigned (NIST CSRC RBAC FAQ); the lab does not model sessions.

RBAC's failure is structural.
NIST SP 800-162 describes it: RBAC "does not easily support multi-factor decisions", role assignments "tend to be based upon more static organizational positions", and trying to express dynamic decisions "would require the creation of numerous roles that are ad hoc and limited in membership, leading to what is often termed 'role explosion'" (`NIST SP 800-162 Section 2.1`).
Per-object sharing is the extreme case: encoding "Bob may view `roadmap`" in RBAC needs a role per document per action (`roadmap_viewer`), which is an ACL renamed as a role.
In the lab matrix, RBAC answers `True` for every principal holding `viewer`, including a user in another tenant, which is exactly how BOLA ships.

### ABAC: rules, attributes, and the combining algorithm

NIST defines ABAC as "An access control method where subject requests to perform operations on objects are granted or denied based on assigned attributes of the subject, assigned attributes of the object, environment conditions, and a set of policies that are specified in terms of those attributes and conditions" (`NIST SP 800-162 Section 2.2`).
Environment conditions are independent of subject and object, such as "the current time, day of the week, location of a user, or the current threat level" (`NIST SP 800-162 Section 2.2`).
The functional decomposition is the one every policy engine reuses (`NIST SP 800-162 Section 2.4.3`):
- Policy Enforcement Point (PEP): "Enforces policy decisions in response to a request from a subject requesting access to a protected object".
- Policy Decision Point (PDP): "Computes access decisions by evaluating the applicable DPs and MPs", including deconflicting digital policies by metapolicies.
- Policy Information Point (PIP): "Serves as the retrieval source of attributes".
- Policy Administration Point (PAP): creates, tests, and stores policies.

The interesting engineering is not the rules but what happens when rules disagree or cannot be evaluated.
OASIS XACML 3.0 names four decisions, `Permit`, `Deny`, `NotApplicable`, and `Indeterminate`, and a rule returns `Indeterminate` when an error occurs while evaluating it (XACML 3.0 core).
Under deny-overrides, "if a single `<Rule>` or `<Policy>` element is encountered that evaluates to 'Deny'", the combined result is `Deny`; under permit-overrides a single `Permit` makes the result `Permit` (`(XACML 3.0 core Section 2.3)`).
A missing attribute is an empty bag by default, and `Indeterminate` only when the designator sets `MustBePresent="True"` (`(XACML 3.0 core Section 7.3.5)`).
That default is a trap: a rule `subject.tenant != resource.tenant -> Deny` evaluated over an empty bag can quietly not match, so the deny never fires.

The lab implements `MustBePresent` semantics for every attribute: `AccessRequest.attr` raises `MissingAttributeError`, the rule becomes `Indeterminate`, and `deny_overrides` returns `Indeterminate` whenever a `Deny` rule could not be evaluated, even if two `Permit` rules matched.
The enforcement point then lets only an explicit `Permit` through.
XACML 3.0 refines `Indeterminate` into `Indeterminate{D}`, `Indeterminate{P}`, and `Indeterminate{DP}`, recording which effect could have resulted, and a rule whose condition errors yields `{D}` or `{P}` from its own effect (`(XACML 3.0 core Section 7.10)`, Table 4).
Deny-overrides then returns `Deny` if any child is `Deny`, `Indeterminate{DP}` if an `Indeterminate{D}` meets a `Permit` or `Indeterminate{P}`, `Indeterminate{D}` if one stands alone, and only then `Permit` (`(XACML 3.0 core Appendix C.2)`); permit-overrides is the mirror image, in which a plain `Deny` beats an `Indeterminate{D}` (`(XACML 3.0 core Appendix C.4)`).
The lab's `deny_overrides` and `permit_overrides` follow those step orders with the three extended values collapsed into one `Indeterminate`, and the test pins the C.4 ordering with a `Deny` rule that cannot be evaluated next to one that matches.
The lab proves the combining algorithm is a policy decision in its own right: for an owner whose account moved to another tenant, the same three rules yield `Deny` under deny-overrides and `Permit` under permit-overrides, and for a request missing `subject.tenant` they yield `Indeterminate` (blocked) and `Permit` (allowed).

ABAC's strength is that it needs no per-object grant: "cleared members of the owning tenant may read" covers every new document with zero writes.
Its weakness is the mirror image: an explicit share is not an attribute.
In the lab matrix ABAC denies Bob, who was shared the document through a group and folder but lacks the clearance attribute, and permits Dave, who is cleared and in the tenant but was never shared it.
Encoding shares as attributes (`resource.shared_with = [...]`) turns the PIP into an ACL store, at which point you are running ReBAC without its consistency model.

### Zanzibar ReBAC: tuples, rewrites, and snapshots

#### Data model

Zanzibar stores ACLs as relation tuples with the text notation `<tuple> ::= <object>'#'<relation>'@'<user>`, `<object> ::= <namespace>':'<object_id>`, `<user> ::= <user_id> | <userset>`, and `<userset> ::= <object>'#'<relation>` (Zanzibar Section 2.1).
The paper's examples are `doc:readme#owner@10`, `group:eng#member@11`, `doc:readme#viewer@group:eng#member`, and `doc:readme#parent@folder:A#...`, where `#...` "represents a relation that does not affect the semantics of the tuple" (Zanzibar Section 2.1, Table 1).
Because a user may be a userset, groups nest and ACLs refer to other ACLs; groups are "simply ACLs with membership semantics" (Zanzibar Section 2.1).
The lab parses exactly this notation and round-trips `doc:readme#viewer@group:eng#member`.

Rows are keyed by `(shard ID, object ID, relation, user, commit timestamp)`, multiple versions of a tuple live in different rows so a check can run "at any timestamp within the garbage collection window", and the ordering lets one read fetch every tuple of an object or an `(object ID, relation)` pair (Zanzibar Section 3.1.1).
The lab's `TupleStore` keeps the same shape in memory: every write or delete is a new revision, and `read(obj, relation, revision)` returns the subjects live at that revision.

#### Userset rewrite rules

A namespace config defines each relation and a rewrite expression tree whose leaves are (Zanzibar Section 2.3.1):
- `_this`: the users from stored tuples for `<object#relation>`, including indirect ACLs referenced by usersets; the default when no rewrite is given.
- `computed_userset`: another relation on the same object, which is how `viewer` includes `editor` and `editor` includes `owner`.
- `tuple_to_userset`: read a tupleset (for example `parent`) on this object and evaluate a relation on each object it points to, which expresses "look up the parent folder of the document and inherit its viewers".

Leaves combine with union, intersection, and exclusion (Zanzibar Section 2.3.1).
Figure 1 of the paper is the canonical config: `editor = _this + owner`, `viewer = _this + editor + parent->viewer`.
The paper notes `computed_userset` and `tuple_to_userset` were added for real clients: one tuple per hop expresses hierarchy compactly and lets Cloud "change ACL inheritance rules without having to update large numbers of tuples" (Zanzibar Section 4.5).
The lab's `DOCS_CONFIG` is Figure 1 plus `can_view = viewer - banned`.

#### Check

In the simple case, `CHECK(U, <object#relation>)` is true if a tuple `<object#relation@U>` exists or a tuple `<object#relation@U'>` exists with `U' = <object'#relation'>` and `CHECK(U, U')` (Zanzibar Section 3.2.3).
That recursion is "pointer chasing"; rewrites become boolean expressions, all leaves are evaluated concurrently, and when one node decides a subtree the rest is cancelled (Zanzibar Section 3.2.3).
Pointer chasing degrades on deep or wide groups, which is why Zanzibar adds Leopard, an index of `(T, s, e)` tuples with `GROUP2GROUP(s)` (all descendant groups, directly or indirectly) and `MEMBER2GROUP(s)` (direct parent groups), so membership is `(MEMBER2GROUP(U) intersect GROUP2GROUP(G)) != empty`, and intersecting sorted lists costs `O(min(|A|,|B|))` skip-list seeks (Zanzibar Section 3.2.4).
A single tuple change "may yield potentially tens of thousands of discrete Leopard tuple events", so Leopard keeps an incremental layer fed by the Watch API on top of offline snapshots (Zanzibar Section 3.2.4).

The lab's `Zanzibar._check` is the sequential version of the recursion with three production concerns made explicit:
- Depth budget: every userset hop, `computed_userset`, and `tuple_to_userset` adds one level, and exceeding `max_depth` (25, the OpenFGA `resolveNodeLimit` default) denies the request with a reason rather than raising to the caller, whereas OpenFGA documents that a query reaching its limit "errors out" (OpenFGA configuration docs for v1.20.0, as of 2026-11).
  A 30-group chain needs 31 tuple reads to succeed with `max_depth=40` and is denied after 26 reads with the default.
- Cycles in a union: `group:a#member@group:b#member` plus the reverse is legal data, and revisiting a node on the current path returns "no new members", the least fixpoint, so the check terminates.
- Cycles under an exclusion: there "no path" would mean "not banned", which grants.
  The lab marks the subtracted branch as negated and turns a cycle there into `IndeterminateCheckError`, which `check` converts into a fail-closed denial: `denied, fail closed: cycle at group:a#member under an exclusion`.
  The trade-off is explicit: a legitimate user whose ban list contains a group cycle is denied until the data is fixed, which is the safe direction.

#### Consistency: the new enemy problem and zookies

The paper's two examples define the hazard (Zanzibar Section 2.2):
- Example A: Alice removes Bob from a folder ACL, then Charlie moves new documents into the folder; Bob may see them if the check neglects the ordering of the two ACL changes.
- Example B: Alice removes Bob from a document ACL, then Charlie adds new contents; Bob may see them if the check uses the ACL from before his removal.

The fix needs two properties: external consistency, so causally related updates get ordered timestamps, and "snapshot reads with bounded staleness" (Zanzibar Section 2.2).
Spanner's TrueTime assigns each ACL write a microsecond-resolution timestamp, each check runs at a single snapshot timestamp across all its reads, and the protocol has two steps: the client requests a zookie through a content-change check when it saves new content and stores it with the content, then sends that zookie on later checks so the snapshot is at least that fresh (Zanzibar Section 2.2).
A zookie is "an opaque byte sequence encoding a globally meaningful timestamp", opaque to stop clients choosing arbitrary timestamps (Zanzibar Section 2.4).
A content-change check "does not carry a zookie and is evaluated at the latest snapshot" (Zanzibar Section 2.4.4).
The "at-least-as-fresh" semantics let Zanzibar pick any fresher timestamp, so most checks run at a default staleness on locally replicated data and timestamps are quantized to avoid hot spots (Zanzibar Section 2.2).

The lab reproduces Example B with a replica one revision behind the head: after the delete at revision 2 and the content-change check that returns `Zookie(2)`, a check without a zookie runs at revision 1 and admits Bob, and the same check with the stored zookie runs at revision 2 and denies him.
SpiceDB names the same choices `minimize_latency`, `at_least_as_fresh`, `at_exact_snapshot`, and `fully_consistent`, calls its zookie a ZedToken, defaults `WriteRelationships`, `DeleteRelationships`, `ReadSchema`, and `WriteSchema` to `fully_consistent` and every other API, including `CheckPermission`, to `minimize_latency`, and warns that the new enemy problem can occur if `minimize_latency` is used exclusively (SpiceDB consistency documentation, as of 2026-11).

#### The rest of the API

- Read takes tuplesets and an optional zookie and returns stored tuples only; it does not apply rewrites, so reading `viewer` does not return owners (Zanzibar Section 2.4.1).
- Write uses optimistic concurrency: read all tuples of an object including a per-object lock tuple, then write conditionally on the lock tuple being unchanged (Zanzibar Section 2.4.2).
- Watch streams tuple changes in timestamp order with a heartbeat zookie to resume from (Zanzibar Section 2.4.3), which is how secondary indexes and Leopard stay current.
- Expand returns the effective userset as a tree whose leaves are users or usersets and whose inner nodes are union, intersection, or exclusion, the building block for search indexes over access-controlled content (Zanzibar Section 2.4.5).
- Namespace configs are themselves versioned, and all servers evaluating one client request use one config snapshot timestamp (Zanzibar Section 3.2.2).

### Externalized decisions: the AuthZEN API

OpenID AuthZEN Authorization API 1.0 (Final, published 11 January 2026) standardizes the PEP-to-PDP call independent of the model behind it: an evaluation request has required `subject` and `resource` objects with `type` and `id`, a required `action` with a `name`, and an optional `context`, and the response is `{"decision": true}` or `false` (AuthZEN Sections 5.1 to 5.5, 6.1, 6.2).
The default paths are `/access/v1/evaluation`, `/access/v1/evaluations` for batches, and `/access/v1/search/subject`, `/access/v1/search/resource`, and `/access/v1/search/action` for the search APIs (AuthZEN Table 1).
Batches carry `evaluations_semantic`: `execute_all` (default), `deny_on_first_deny`, or `permit_on_first_permit`, and decisions come back in request order (AuthZEN Sections 7.1.2.1 and 7.2).
The resource search is the list-filtering question ("which documents may Bob see") that every list endpoint needs and that a per-object `Check` loop answers in `O(n)` calls.

### The comparison on one scenario

The lab asks one question, "may this principal read `doc:roadmap`" (tenant `acme`, owner `alice`, classification 2), of each model in isolation, and asserts this exact matrix:

| Scenario | Scope `docs:read` | RBAC role | ABAC (deny-overrides) | ReBAC `can_view` |
|---|---|---|---|---|
| owner | `True` | `True` | `True` | `True` |
| shared via group and folder (clearance 1) | `True` | `True` | `False` | `True` |
| same tenant, never shared | `True` | `True` | `True` | `False` |
| other tenant | `True` | `True` | `False` | `False` |
| shared, then banned | `True` | `True` | `True` | `False` |
| owner, token lacks `docs:read` | `False` | `True` | `True` | `True` |

Scopes and roles say `True` for the cross-tenant reader, which is BOLA.
ABAC gets the tenant boundary right but both over-grants (Dave) and under-grants (Bob) whenever the real rule is a share.
ReBAC answers the sharing question but knows nothing about the client: the last row is the owner reading through a token they never consented to use for documents, and only the scope check catches it.
That is the argument for intersecting a scope check with an object check rather than choosing one model.

## Numbers and limits

| Quantity | Value | Source or measurement |
|---|---|---|
| Legal characters in a `scope-token` | 92 (`%x21`, `%x23-5B`, `%x5D-7E`) | `(RFC 6749 Section 3.3)`, counted from the ABNF |
| Status for insufficient scope / invalid token / bad request | `403` / `401` / `400` (all SHOULD) | `(RFC 6750 Section 3.1)` |
| Zanzibar relation tuples | more than 2 trillion, close to 100 terabytes, replicated in more than 30 locations | Zanzibar Section 4 (paper, 2019) |
| Zanzibar namespaces | more than 1,500; config size median near 500 lines; tuples per namespace median near 15,000 | Zanzibar Section 4 |
| Zanzibar load | more than 10 million client QPS; Check peak about 4.2M, Read 8.2M, Expand 760K, Write 25K QPS | Zanzibar Section 4, 7-day sample December 2018 |
| Check latency, Safe (zookie older than 10 s) | 3.0 ms p50, 9.46 ms p95, 15.0 ms p99 | Zanzibar Table 2 (mean of each percentile over 7 days, December 2018) |
| Check latency, Recent (zookie under 10 s old) | 2.86 ms p50, 60.0 ms p95, 76.3 ms p99 | Zanzibar Table 2 (same 7-day means) |
| Write latency | 127.0 ms p50, 233.0 ms p95, 401.0 ms p99 | Zanzibar Table 2 (same 7-day means) |
| Safe vs Recent request rate | Safe about two orders of magnitude higher | Zanzibar Section 4.1, Figure 3 |
| Availability | above 99.999% over 3 years | Zanzibar abstract and Section 4.3 |
| Serving fleet | more than 10,000 servers, cluster median near 500 servers | Zanzibar Section 4 |
| Snapshot timestamp quantum | coarse granularity "such as one or ten seconds" | Zanzibar Section 3.2.5 |
| Leopard | 1.56M QPS median, 2.22M QPS p99; under 150 us median and under 1 ms p99 | Zanzibar Section 4.4 |
| OpenFGA defaults | `resolveNodeLimit` 25 (depth), `resolveNodeBreadthLimit` 10 (nodes per level evaluated concurrently), `maxTuplesPerWrite` 100, `listObjectsMaxResults` 1000, `listObjectsDeadline` 3s, `checkQueryCache.ttl` 10s | OpenFGA configuration docs for v1.20.0 (as of 2026-11) |
| AuthZEN batch semantics | 3 values (`execute_all` default, `deny_on_first_deny`, `permit_on_first_permit`) | AuthZEN Section 7.1.2.1 (1.0 Final, 2026-01-11) |
| Lab: 30-group nested chain | 31 tuple reads with `max_depth=40`; denied after 26 reads at the default 25 | lab test, CPython 3.14.6, macOS 27.3 arm64 |
| Lab: suite | 17 tests, `17 passed in 0.25s` (single run; wall time varies, `0.48s` on another run the same day) | `uv run --frozen pytest ... -q`, CPython 3.14.6, macOS 27.3 arm64 (Darwin 27.3.0), 2026-11-15 |

The Zanzibar figures are Google's internal deployment in 2018 and say nothing about an open-source engine on your hardware; use them as an existence proof of the architecture, not as a capacity plan.

## Portability

The brief for this note paused matrix runs, so only one leg was executed; every other cell cites a source and says it was not run.

| Leg | Result | Evidence |
|---|---|---|
| CPython 3.14.6 (workspace `.python-version`), macOS 27.3 arm64 | `17 passed in 0.25s` | the verifier's single run, 2026-11-15 |
| CPython 3.12.16 and 3.13.18 | not run | the module needs 3.12 for the `type` statement (PEP 695) and uses `StrEnum` (3.11) and `match` (3.10); FastAPI 0.162.0 declares `Requires-Python: >=3.10`, Starlette 1.10.0 `>=3.11`, Authlib 1.9.5 `>=3.10` (installed `METADATA`) |
| CPython 3.14.10, 3.15.2 | not run | no version-specific code paths in the lab |
| CPython 3.15.2t free-threaded | not run | the lab is pure Python with no shared mutable state across threads; `TupleStore` is not thread-safe and would need a lock if shared |
| HTTP/1.1, HTTP/2, HTTP/3 | protocol-independent | `401` MUST carry `WWW-Authenticate` regardless of version (`(RFC 9110 Section 15.5.2)`); the lab runs in-process through `httpx.ASGITransport`, with no wire protocol |
| Uvicorn, Hypercorn, Granian | not run | the decisions are made inside the ASGI app before `http.response.start` |
| FastAPI scope accumulation | parent scopes first, then own, de-duplicated | lab test on FastAPI 0.162.0; `(fastapi 0.162.0 fastapi/dependencies/models.py, _get_oauth_scopes)` |
| Authlib scope parsing | `str.split()` (any whitespace) | lab test on Authlib 1.9.5 |

## Worked example

The [implementation](../labs/src/api_labs/f15_api_authorization_and_owasp_security/authorization_models.py) contains a strict RFC 6749 scope parser and RFC 6750 challenge, a NIST-style RBAC policy with hierarchy and SSD, an ABAC rule engine with deny-overrides and permit-overrides, a Zanzibar engine (tuple parser, multi-version store, rewrite evaluator, zookies), the four-model comparison, and a FastAPI app that layers scopes and ReBAC; the [test](../labs/tests/f15_api_authorization_and_owasp_security/test_authorization_models_scopes_rbac_abac_and_zanzibar_rebac.py) drives all of it in-process with 17 `unit` tests.

The Zanzibar core, a simplified excerpt of `authorization_models.py` (the methods shown are copied unchanged; docstrings, the `_rewrite` lookup method, and the error classes are omitted):

```python
# simplified excerpt from src/api_labs/f15_api_authorization_and_owasp_security/authorization_models.py
class Zanzibar:
    def __init__(self, config: NamespaceConfig, store: TupleStore, max_depth: int = 25) -> None:
        self.config = config
        self.store = store
        self.max_depth = max_depth

    def check(
        self, obj: str, relation: str, user: str, at_least: Zookie | None = None
    ) -> CheckResult:
        revision = self.store.snapshot(at_least)
        try:
            allowed = self._check(_object_ref(obj), relation, user, revision, 0, frozenset(), False)
        except (ResolutionLimitError, IndeterminateCheckError) as exc:
            return CheckResult(False, revision, f"denied, fail closed: {exc}")
        return CheckResult(allowed, revision)

    def content_change_check(
        self, obj: str, relation: str, user: str
    ) -> tuple[CheckResult, Zookie]:
        result = self.check(obj, relation, user, Zookie(self.store.head))
        return result, Zookie(result.revision)

    def _check(
        self,
        obj: ObjectRef,
        relation: str,
        user: str,
        revision: int,
        depth: int,
        path: frozenset[tuple[ObjectRef, str]],
        negated: bool,
    ) -> bool:
        if depth > self.max_depth:
            raise ResolutionLimitError(
                f"depth {depth} exceeds {self.max_depth} at {obj}#{relation}"
            )
        node = (obj, relation)
        if node in path:
            if negated:
                raise IndeterminateCheckError(f"cycle at {obj}#{relation} under an exclusion")
            return False  # a cycle adds no members to a union: least fixpoint
        path = path | {node}
        rewrite = self._rewrite(obj, relation)
        return self._eval(rewrite, obj, relation, user, revision, depth, path, negated)

    def _eval(
        self,
        rewrite: Rewrite,
        obj: ObjectRef,
        relation: str,
        user: str,
        revision: int,
        depth: int,
        path: frozenset[tuple[ObjectRef, str]],
        negated: bool,
    ) -> bool:
        match rewrite:
            case This():
                for subject in self.store.read(obj, relation, revision):
                    if isinstance(subject, str):
                        if subject == user:
                            return True
                    elif subject.relation != "..." and self._check(
                        subject.obj, subject.relation, user, revision, depth + 1, path, negated
                    ):
                        return True
                return False
            case ComputedUserset(relation=other):
                return self._check(obj, other, user, revision, depth + 1, path, negated)
            case TupleToUserset(tupleset=tupleset, computed_relation=computed):
                for subject in self.store.read(obj, tupleset, revision):
                    target = subject.obj if isinstance(subject, Userset) else None
                    if target is not None and self._check(
                        target, computed, user, revision, depth + 1, path, negated
                    ):
                        return True
                return False
            case Union(children=children):
                return any(
                    self._eval(c, obj, relation, user, revision, depth, path, negated)
                    for c in children
                )
            case Intersection(children=children):
                return all(
                    self._eval(c, obj, relation, user, revision, depth, path, negated)
                    for c in children
                )
            case Exclusion(base=base, subtract=subtract):
                if not self._eval(base, obj, relation, user, revision, depth, path, negated):
                    return False
                return not self._eval(subtract, obj, relation, user, revision, depth, path, True)


DOCS_CONFIG: NamespaceConfig = {
    "doc": {
        "owner": This(),
        "parent": This(),
        "banned": This(),
        "editor": Union((This(), ComputedUserset("owner"))),
        "viewer": Union((This(), ComputedUserset("editor"), TupleToUserset("parent", "viewer"))),
        "can_view": Exclusion(ComputedUserset("viewer"), ComputedUserset("banned")),
    },
    "folder": {
        "parent": This(),
        "viewer": Union((This(), TupleToUserset("parent", "viewer"))),
    },
    "group": {"member": This()},
}
```

The FastAPI layering, a simplified excerpt (copied unchanged from `build_docs_app`; the `# pyright: ignore` comments are dropped):

```python
# simplified excerpt from src/api_labs/f15_api_authorization_and_owasp_security/authorization_models.py
def build_docs_app(engine: Zanzibar, tokens: Mapping[str, tuple[str, str]]) -> FastAPI:
    """tokens maps an opaque bearer token to (user id, granted scope string)."""
    bearer = HTTPBearer()  # missing or non-Bearer Authorization -> 401 with WWW-Authenticate

    async def principal(
        security_scopes: SecurityScopes,
        creds: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    ) -> str:
        entry = tokens.get(creds.credentials)
        if entry is None:
            raise HTTPException(
                401, "invalid_token", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
            )
        user, scope = entry
        # security_scopes.scopes is every scope required along this dependency chain
        if missing_scopes(parse_scope(scope), security_scopes.scopes):
            challenge = insufficient_scope_challenge(security_scopes.scopes)
            raise HTTPException(403, "insufficient_scope", headers={"WWW-Authenticate": challenge})
        return user

    async def reader(user: Annotated[str, Security(principal, scopes=["docs:read"])]) -> str:
        return user

    app = FastAPI()

    @app.get("/docs/{doc_id}")
    async def read_doc(
        doc_id: str, user: Annotated[str, Security(principal, scopes=["docs:read"])]
    ) -> dict[str, str]:
        if not engine.check(f"doc:{doc_id}", "can_view", user).allowed:
            raise HTTPException(404, "Not Found")  # RFC 9110 Section 15.5.4: hide existence
        return {"doc_id": doc_id, "user": user}

    @app.patch("/docs/{doc_id}")
    async def edit_doc(
        doc_id: str, user: Annotated[str, Security(reader, scopes=["docs:write"])]
    ) -> dict[str, str]:
        if engine.check(f"doc:{doc_id}", "editor", user).allowed:
            return {"doc_id": doc_id, "editor": user}
        if engine.check(f"doc:{doc_id}", "can_view", user).allowed:
            raise HTTPException(403, "Forbidden")  # it may see the document, so 404 would lie
        raise HTTPException(404, "Not Found")

    return app
```

Two of the 17 tests, a simplified excerpt of the test module (the functions are copied unchanged; imports, markers, and fixtures are omitted):

```python
# simplified excerpt from tests/f15_api_authorization_and_owasp_security/test_authorization_models_scopes_rbac_abac_and_zanzibar_rebac.py
def test_new_enemy_problem_and_the_zookie_that_prevents_it() -> None:
    # Zanzibar Section 2.2, Example B: remove Bob, then add new content to the document.
    store = TupleStore(replica_lag=1)
    engine = Zanzibar(DOCS_CONFIG, store)
    store.write("doc:readme#viewer@bob", "doc:readme#editor@charlie")
    store.delete("doc:readme#viewer@bob")  # 1. Alice removes Bob from the ACL
    allowed, zookie = engine.content_change_check("doc:readme", "editor", "charlie")
    assert allowed.allowed  # 2. Charlie adds content; the zookie is stored with it
    assert zookie == Zookie(2)
    stale = engine.check("doc:readme", "viewer", "bob")  # 3. no zookie: replica snapshot
    assert (stale.allowed, stale.revision) == (True, 1)  # the new enemy sees new content
    fresh = engine.check("doc:readme", "viewer", "bob", at_least=zookie)
    assert (fresh.allowed, fresh.revision) == (False, 2)


def test_four_models_answer_the_same_question_differently() -> None:
    engine = Zanzibar(DOCS_CONFIG, docs_store())
    editor, viewer = frozenset({"editor"}), frozenset({"viewer"})
    cases = [
        ("owner", Principal("alice", "acme", editor, "docs:read"), 3),
        ("shared via group and folder", Principal("bob", "acme", viewer, "docs:read"), 1),
        ("same tenant, never shared", Principal("dave", "acme", viewer, "docs:read"), 3),
        ("other tenant", Principal("mallory", "globex", viewer, "docs:read"), 3),
        ("shared, then banned", Principal("erin", "acme", viewer, "docs:read"), 3),
        ("owner, token lacks docs:read", Principal("alice", "acme", editor, "profile"), 3),
    ]
    rows = [decide_all(name, who, ROADMAP, clearance, engine) for name, who, clearance in cases]
    assert rows == [
        ModelRow("owner", scopes=True, rbac=True, abac=True, rebac=True),
        ModelRow("shared via group and folder", scopes=True, rbac=True, abac=False, rebac=True),
        ModelRow("same tenant, never shared", scopes=True, rbac=True, abac=True, rebac=False),
        ModelRow("other tenant", scopes=True, rbac=True, abac=False, rebac=False),
        ModelRow("shared, then banned", scopes=True, rbac=True, abac=True, rebac=False),
        ModelRow("owner, token lacks docs:read", scopes=False, rbac=True, abac=True, rebac=True),
    ]
```

The tuples behind `docs_store()` are `doc:roadmap#owner@alice`, `doc:roadmap#parent@folder:eng#...`, `folder:eng#viewer@group:eng#member`, `group:eng#member@bob`, `group:eng#member@erin`, and `doc:roadmap#banned@erin`.

Captured output of the gate command from `labs/` on CPython 3.14.6, macOS 27.3 arm64 (Darwin), 2026-11-15:

```text
$ uv run --frozen pytest tests/f15_api_authorization_and_owasp_security/test_authorization_models_scopes_rbac_abac_and_zanzibar_rebac.py -q
.................                                                        [100%]
17 passed in 0.25s
```

## Pitfalls

> [!warning] Exclusion or intersection plus a cycle bypasses authorization (OpenFGA CVE-2024-31452)
> - **Trigger**: OpenFGA `>= 1.5.0, < 1.5.3` evaluating `Check` or `ListObjects` on a model that uses exclusion or intersection together with cyclical relationships (GitHub advisory GHSA-8cph-m685-6v6r, CWE-285 and CWE-863, CVSS 3.1 8.1).
> - **Symptom**: "authorization bypass when calling Check or ListObjects APIs": access granted that the model denies.
> - **Fix**: upgrade to 1.5.3; in your own engines, never let "cycle, no path" flow into a negated branch.
>   The lab marks the subtracted side of an exclusion as negated and denies with `cycle at group:a#member under an exclusion` instead of returning "not banned".

> [!warning] A check cache plus a tuple cycle returns stale grants (OpenFGA CVE-2025-46331)
> - **Trigger**: OpenFGA `>= 1.3.6, < 1.8.11` with the check query cache enabled, a model with a tuple cycle, and several `Check` or `ListObjects` requests involving the cycle within the cache TTL (GHSA-w222-m46c-mgh6, CWE-284 and CWE-863).
> - **Symptom**: incorrect authorization decisions served from the cache.
> - **Fix**: upgrade to 1.8.11; key caches on the snapshot revision, as Zanzibar does by "encoding snapshot timestamps in cache keys" (Zanzibar Section 3.2.5), and never cache a result computed under cycle cut-off as if it were final.

> [!warning] Exclusion under an arrow denies a legitimate user (SpiceDB CVE-2024-38361)
> - **Trigger**: SpiceDB `< 1.33.1`, a schema with `permission view = member - banned` on `folder` and `permission view = folder->view` on `resource`, and a resource under several folders the user can view (GHSA-grjv-gjgr-66g2, CWE-281).
> - **Symptom**: `CheckPermission` returns `NO_PERMISSION` when `PERMISSION` is expected, because the exclusion dispatcher did not request all folders in which the user is a member.
> - **Fix**: upgrade to 1.33.1; the lesson is that `tuple_to_userset` must evaluate every tupleset member, which the lab's `TupleToUserset` loop does, and that the two failure directions of a ReBAC bug are a security incident and an outage.

> [!warning] Scopes treated as permissions
> - **Trigger**: a route that checks `docs:read` and then loads `doc_id` from the path.
> - **Symptom**: any user with any token carrying `docs:read` reads any document, the BOLA pattern OWASP describes as manipulating "the ID of an object that is sent within the request" (OWASP API1:2023); in the lab matrix the cross-tenant reader passes the scope check.
> - **Fix**: keep the scope check at the edge and add an object-level check that names the object; OWASP's prevention is to check that "the logged-in user has access to perform the requested action on the record" (OWASP API1:2023).

> [!warning] Lenient scope parsing changes the grant
> - **Trigger**: comparing scopes with `str.split()`, `in` on the raw string, or case-insensitive matching.
> - **Symptom**: `"docs:read\tadmin"` becomes two scopes in Authlib 1.9.5 `scope_to_list`, and substring checks such as `"admin" in scope` match `superadmin` or `admin:readonly`.
> - **Fix**: parse with the RFC 6749 grammar (single `SP`, 92-character token alphabet, case-sensitive) and compare sets; the lab rejects empty values, double spaces, `TAB`, `"`, and `\`.

> [!warning] A missing attribute silently disables a deny rule
> - **Trigger**: an ABAC deny rule over an attribute the PIP did not return, under the XACML default where a missing attribute is an empty bag (`(XACML 3.0 core Section 7.3.5)`), or a `try/except` that maps evaluation errors to "rule does not apply".
> - **Symptom**: the tenant-isolation rule never fires and the permit rules win.
> - **Fix**: require every attribute (`MustBePresent`), make an `Indeterminate` deny rule block every permit, and let the enforcement point allow only an explicit `Permit`; the lab asserts the `Indeterminate` result and that permit-overrides would have allowed the same request.

> [!warning] Checking at the latest replica instead of the content's zookie
> - **Trigger**: every check uses `minimize_latency` (or a cache) and the application never stores the zookie or ZedToken returned when content changes.
> - **Symptom**: a user removed from an ACL sees content added after the removal (Zanzibar Section 2.2, Example B); the lab's stale check admits Bob at revision 1.
> - **Fix**: perform a content-change check on every write, store the token with the content, and send it as `at_least_as_fresh` on reads (SpiceDB consistency documentation); use `fully_consistent` only where the latency cost is acceptable.

> [!warning] Roles in the token outlive the role assignment
> - **Trigger**: RBAC decisions read only the `roles` claim of a JWT access token (`(RFC 9068 Section 2.2.3.1)`).
> - **Symptom**: a revoked role keeps working until the token expires, and every API in the token's `aud` honors it.
> - **Fix**: keep access tokens short-lived, audience-restricted (`(RFC 9068 Section 4)`, `(RFC 8707 Section 2)`), and resolve high-risk roles server-side at decision time.

## Security and robustness

- **OWASP API1:2023 (BOLA)**: only an object-level model closes it; scopes and roles cannot, because neither names an object.
  The structural fix is tenant-scoped queries and row-level security ([[Defeating-BOLA-API1-2023-Tenant-Scoped-Queries-and-PostgreSQL-RLS]]), and ReBAC generalizes it to explicit sharing.
- **OWASP API5:2023 (BFLA)**: OWASP notes that "modern applications can contain many types of roles, groups, and complex user hierarchies" and requires that enforcement "deny all access by default, requiring explicit grants to specific roles" (OWASP API5:2023); RBAC on routers is the natural model ([[Defeating-BFLA-API5-2023-Function-Level-Guards-and-Deny-By-Default-Routers]]).
- **Fail closed on every unknown**: an unknown token is `401`, a malformed scope is a parse error, a missing attribute is `Indeterminate`, a cycle under exclusion and an exhausted depth budget are denials with a logged reason, and only an unknown relation in the config raises, because that is a deployment bug that must page someone rather than look like a deny.
- **Status codes leak**: `403` on an object the caller cannot see confirms it exists; RFC 9110 lets the origin "hide" a forbidden resource with `404` (`(RFC 9110 Section 15.5.4)`).
  The lab answers `404` when the caller cannot view the document and `403` only when it can view but not edit, and it sends `WWW-Authenticate` only for token problems, never for object denials.
- **Audience and token confusion**: a scope string is meaningful only for the `aud` it was issued to (`(RFC 9068 Section 2.2.3)`), and authorization servers MUST use a distinct `aud` identifier per resource to prevent cross-JWT confusion (`(RFC 9068 Section 5)`).
- **Privacy of authorization claims**: putting `groups` or `roles` in an unencrypted JWT exposes them to the client, and RFC 9068 asks authorization servers to consider that exposure (`(RFC 9068 Section 6)`).
- **ReBAC denial of service**: deep or wide graphs make `Check` expensive; bound resolution depth (OpenFGA default 25), per-level concurrency (OpenFGA `resolveNodeBreadthLimit`, default 10), and `ListObjects` result count and deadline (1000, `3s`) as OpenFGA does, and treat a hit limit as a deny, not as a `500` that a client retries in a loop.
- **Write races in the tuple store**: concurrent read-modify-write of an object's ACL needs the per-object lock tuple and conditional write of Zanzibar Section 2.4.2, or two administrators can each remove a different user and resurrect the other's removal.
- **Defense in depth**: put the coarse scope check at the gateway or middleware ([[Where-Authorization-Lives-Gateway-Middleware-vs-Domain-and-Query-Layer]]) and the object check next to the query; a gateway that calls an external authorizer ([[External-Authorization-Ext-Authz-and-Global-Rate-Limit-Filters]]) still cannot see which rows the handler will load.

## The principal's view

The decision is not "which model" but "which question does each layer own, and where does the state for the object question live".
Scopes are owned by the identity platform and describe client consent; keep them few, coarse, resource-prefixed, and audience-bound, and move anything object-specific into RAR `authorization_details` ([[Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396]]) only when the user must approve a specific transaction.
Roles are owned by the organization chart and describe job functions; they belong on routes and admin surfaces, and every new role should be reviewed for whether it is secretly an object grant.
The object question is where the money is.
If every object belongs to exactly one tenant and access is "everyone in the tenant with permission X", a tenant column, a repository type that cannot omit the tenant filter, and PostgreSQL row-level security answer it inside the query that loads the data, with no network hop and no consistency problem.
When users share objects with users, groups, and folders across tenants, when inheritance follows a containment hierarchy, and when several products must agree on who can see what, a Zanzibar-style service (SpiceDB, OpenFGA, or an in-house one) pays for itself: one schema, one tuple store, one audit trail, and an Expand or resource-search API for list endpoints.
The organizational cost is real: tuples become a second source of truth that must be written transactionally with the domain data (an outbox, or the zookie dance on every write), list endpoints need reverse lookups or a Leopard-like index, and an outage of the authorization service is an outage of every product that depends on it, which is why Zanzibar reports availability above 99.999% over three years (Zanzibar Section 4.3).
ABAC fits best as the policy layer on top: environment conditions (MFA freshness, network zone, time windows) and data-classification rules expressed once in a PDP, with ReBAC relations supplied as attributes, and AuthZEN as the stable PEP-to-PDP contract so the engine can change without touching every service.
Choose a different paradigm when the decision must be made offline or at the edge without a network call: capability tokens or signed per-object grants trade revocation latency for independence, which is the same trade-off as roles in a JWT.

## Interview questions

> [!question] L5: What is the difference between an OAuth scope and a permission?
>
> > [!success]- Answer
> > A scope is what the resource owner allowed a specific client to do on their behalf, granted by the authorization server and carried in the token (`(RFC 6749 Section 3.3)`).
> > A permission is what the user may do to a resource, decided by the resource server.
> > The effective right is the intersection: a token with `docs:read` cannot read a document the user cannot see, and a user who owns a document cannot read it through a token without `docs:read`.
> > The lab matrix shows both directions.
> >
> > Follow-up: Which status code and header does the resource server return when the scope is missing?

> [!question] L5: Why does RBAC alone not prevent BOLA?
>
> > [!success]- Answer
> > An RBAC permission is an operation on an object type, so `viewer` grants `read` on every `document`.
> > Nothing in the role names the specific object, so the route must still check that this user may read this `doc_id`.
> > In the lab, a `viewer` from another tenant gets `True` from RBAC and `False` from ABAC and ReBAC.
> >
> > Follow-up: What happens to the role count if you try to fix it with per-document roles?

> [!question] L5: Explain the four leaf and operator kinds of a Zanzibar userset rewrite with the document example.
>
> > [!success]- Answer
> > `_this` returns users from stored tuples, `computed_userset` reuses another relation on the same object, and `tuple_to_userset` follows a relation such as `parent` to other objects and evaluates a relation there (Zanzibar Section 2.3.1).
> > Union, intersection, and exclusion combine them.
> > Figure 1: `editor = _this + owner`, `viewer = _this + editor + parent->viewer`, so owners are editors, editors are viewers, and folder viewers are document viewers.
> >
> > Follow-up: Why does reading the `viewer` relation with the Read API not return owners?

> [!question] L6: Predict the output: a FastAPI route declares `Security(reader, scopes=["docs:write"])`, and `reader` declares `Security(principal, scopes=["docs:read"])`; a token granted only `docs:write` calls it, and `principal` returns `403` with `insufficient_scope_challenge(security_scopes.scopes)`.
>
> > [!success]- Answer
> > `principal` receives `SecurityScopes(scopes=["docs:write", "docs:read"])`, because FastAPI 0.162.0 copies the parent chain's scopes and appends the dependency's own (`(fastapi 0.162.0 fastapi/dependencies/models.py, _get_oauth_scopes)`).
> > `docs:read` is missing, so the response is `403` with `WWW-Authenticate: Bearer error="insufficient_scope", scope="docs:write docs:read"`.
> > The lab asserts exactly that header.
> >
> > Follow-up: If the same security dependency is declared twice in one request with different scope lists, how many times does it run?

> [!question] L6: Debug this API: after an incident review removed a contractor from a project, logs show the contractor downloading a design document uploaded ten minutes later; the authorization service is SpiceDB and every check uses the default consistency.
>
> > [!success]- Answer
> > This is the new enemy problem (Zanzibar Section 2.2, Example B): `CheckPermission`, like every SpiceDB API other than the relationship-write and schema APIs, defaults to `minimize_latency`, which may serve a snapshot from before the removal (SpiceDB consistency documentation).
> > The upload did not record a ZedToken, so the download check had no lower bound on its snapshot.
> > Fix: on upload, perform the check or write that returns a ZedToken, store it with the file, and send it with `at_least_as_fresh` on every read of that file; the lab shows the same check moving from revision 1 (allowed) to revision 2 (denied) when the zookie is supplied.
> >
> > Follow-up: Why would switching everything to `fully_consistent` be the wrong fix at scale?

> [!question] L6: An ABAC policy has `deny-cross-tenant` and two permit rules; the PIP times out on the subject's tenant lookup; what should the decision be and why?
>
> > [!success]- Answer
> > The deny rule cannot be evaluated, so it is `Indeterminate`, and under deny-overrides with a deny-biased PEP the request must not be permitted even though the permit rules match.
> > XACML's default turns a missing attribute into an empty bag, and `Indeterminate` only with `MustBePresent="True"` (`(XACML 3.0 core Section 7.3.5)`), so with the default the deny rule may silently not match.
> > The lab returns `Indeterminate` with deny-overrides and `Permit` with permit-overrides for the same request.
> >
> > Follow-up: How do you keep this from becoming an availability problem when the PIP is flaky?

> [!question] L6: Your ReBAC engine meets `group:a#member@group:b#member` and `group:b#member@group:a#member`; what should `Check` return in a union and under an exclusion?
>
> > [!success]- Answer
> > In a pure union, a cycle contributes no new members, so returning "no path" for the revisited node is the least fixpoint and the check terminates with the right answer.
> > Under an exclusion, "no path" in the subtracted branch means "not excluded", which grants; that is the class of bug behind OpenFGA CVE-2024-31452 (exclusion or intersection with cycles).
> > The lab marks the subtracted branch as negated and denies with `cycle at group:a#member under an exclusion`, trading a possible false deny for safety.
> >
> > Follow-up: How would you detect such cycles at write time instead of at check time?

> [!question] L7+: Design authorization for a multi-tenant document product that is adding cross-tenant sharing; what goes where?
>
> > [!success]- Answer
> > Scopes at the gateway for client consent (`docs:read`, `docs:write`), audience-bound per API (`(RFC 9068 Section 4)`).
> > Roles on routers for function-level rules such as admin endpoints, deny by default (OWASP API5:2023).
> > A ReBAC service for objects: `doc`, `folder`, `group`, `tenant` namespaces, Figure-1-style inheritance, and tuples written in the same logical transaction as the domain change through an outbox, with zookies stored per document.
> > Keep tenant row-level security in the database as a second, independent boundary, and use a resource-search or Expand-based index for list endpoints rather than `n` checks.
> >
> > Follow-up: What is your plan when the authorization service is unavailable: fail closed everywhere, or a cached decision for reads only?

> [!question] L7+: When would you choose ABAC over ReBAC, and when would you combine them?
>
> > [!success]- Answer
> > Choose ABAC when access follows properties that already exist on the subject, object, and environment (clearance, classification, region, MFA age) and there are no explicit shares, because it needs no per-object writes (`NIST SP 800-162 Section 2.1`).
> > Choose ReBAC when grants are explicit relationships and inheritance follows containment, because attributes cannot represent "Bob was shared this folder" without becoming an ACL store.
> > Combine them by evaluating the ReBAC relation as one input to an ABAC policy that adds environment conditions, behind an AuthZEN-style PDP API.
> >
> > Follow-up: Where do you put the audit log that explains a decision across both?

> [!question] L7+: Zanzibar reports p95 Check latency of 9.46 ms for Safe requests and 60.0 ms for Recent ones; what causes the gap and what would you do with that knowledge?
>
> > [!success]- Answer
> > Recent requests carry zookies under 10 seconds old, which often require cross-region round trips to a leader replica, while Safe requests are served from local replicas (Zanzibar Section 4.1, Table 2).
> > The design lever is to let most checks use a default staleness and to require freshness only where causality matters, by storing zookies with content rather than demanding the latest snapshot everywhere.
> > Quantizing snapshot timestamps also raises cache hit rates (Zanzibar Section 3.2.5).
> >
> > Follow-up: Which product flows in your system genuinely need Recent semantics?

## Related

Prerequisites: [[Enterprise-SSO-Integration-Multi-Tenant-OIDC-and-SCIM-Provisioning]], [[JWT-JWS-JWK-Structure-and-RFC-9068-Access-Token-Profile]]

Next: [[Where-Authorization-Lives-Gateway-Middleware-vs-Domain-and-Query-Layer]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]], [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]], [[Defeating-BOLA-API1-2023-Tenant-Scoped-Queries-and-PostgreSQL-RLS]], [[Defeating-BFLA-API5-2023-Function-Level-Guards-and-Deny-By-Default-Routers]], [[Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396]], [[OAuth-2-0-to-OAuth-2-1-and-RFC-9700-Security-BCP]], [[Service-to-Service-Identity-Client-Credentials-and-Token-Exchange-RFC-8693]], [[External-Authorization-Ext-Authz-and-Global-Rate-Limit-Filters]]

- Lab: `labs/tests/f15_api_authorization_and_owasp_security/test_authorization_models_scopes_rbac_abac_and_zanzibar_rebac.py`

## Further reading

- [Zanzibar: Google's Consistent, Global Authorization System (USENIX ATC 2019)](https://www.usenix.org/system/files/atc19-pang.pdf) - the relation tuple model, userset rewrites, zookies and the new enemy problem, Leopard, and production numbers.
- [RFC 6749: The OAuth 2.0 Authorization Framework](https://www.rfc-editor.org/rfc/rfc6749.html) - Section 3.3 defines the scope grammar and the server's freedom to narrow it.
- [RFC 6750: Bearer Token Usage](https://www.rfc-editor.org/rfc/rfc6750.html) - the `WWW-Authenticate` challenge, `insufficient_scope`, and the `401`/`403`/`400` split.
- [RFC 9068: JWT Profile for OAuth 2.0 Access Tokens](https://www.rfc-editor.org/rfc/rfc9068.html) - the `scope` claim, SCIM `groups`/`roles`/`entitlements` claims, and mandatory audience validation.
- [RFC 9396: OAuth 2.0 Rich Authorization Requests](https://www.rfc-editor.org/rfc/rfc9396.html) - why scope is coarse and how `authorization_details` carries fine-grained consent.
- [RFC 8707: Resource Indicators for OAuth 2.0](https://www.rfc-editor.org/rfc/rfc8707.html) - audience-restricting tokens to one resource.
- [RFC 9700: Best Current Practice for OAuth 2.0 Security](https://www.rfc-editor.org/rfc/rfc9700.html) - Section 2.3 on minimum privilege, audience, and resource restriction.
- [NIST SP 800-162: Guide to ABAC Definition and Considerations](https://nvlpubs.nist.gov/nistpubs/specialpublications/NIST.SP.800-162.pdf) - the ABAC definition, role explosion, and the PEP/PDP/PIP/PAP decomposition.
- [NIST RBAC project FAQ](https://csrc.nist.gov/projects/role-based-access-control/faqs) - the four components of the ANSI INCITS 359-2004 RBAC standard.
- [OASIS XACML 3.0 Core](https://docs.oasis-open.org/xacml/3.0/xacml-3.0-core-spec-os-en.html) - decisions, combining algorithms, and `MustBePresent` attribute semantics.
- [OpenID AuthZEN Authorization API 1.0](https://openid.net/specs/authorization-api-1_0.html) - the standard PEP-to-PDP evaluation and search API.
- [SpiceDB consistency](https://authzed.com/docs/spicedb/concepts/consistency) - ZedTokens and the four consistency levels of an open-source Zanzibar descendant.
- [GHSA-8cph-m685-6v6r (CVE-2024-31452)](https://github.com/advisories/GHSA-8cph-m685-6v6r) - an exclusion-plus-cycle authorization bypass in OpenFGA.
