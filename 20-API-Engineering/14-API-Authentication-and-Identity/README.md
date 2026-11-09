---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# API Authentication and Identity

Provides an RFC-grounded deep dive into API authentication and cryptographic identity `(as of 2026-11)`.
Covers API key engineering, OAuth 2.0 (RFC 6749/6750), the OAuth 2.0 Security Best Current Practice (RFC 9700, January 2025), OAuth 2.0 for Browser-Based Applications (RFC 10017, August 2026), and the active OAuth 2.1 consolidation draft (`draft-ietf-oauth-v2-1-17`, updated 2026-10-31).
Examines OpenID Connect 1.0, JWT structure and RFC 8725 cryptographic vulnerabilities (`joserfc 1.8.0` and `PyJWT 2.15.1`), JWKS rotation, sender-constrained tokens via mTLS (RFC 8705) and DPoP (RFC 9449), Token Exchange (RFC 8693), HTTP Message Signatures (RFC 9421), and Selective Disclosure JWTs (RFC 9901, November 2025).

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Authentication vs Authorization Architecture: Edge Termination vs Zero-Trust End-to-End Token Propagation Across Microservices](API-Authentication-Architecture-Edge-Verification-vs-Zero-Trust-Propagation.md)
2. [Engineering Production API Keys: Secret-Scanning Prefixes (acme_live_...), SHA-256/HMAC Lookup Hashes, Constant-Time Comparison, and Dual-Key Rotation](API-Key-Engineering-Prefixed-Entropy-Hashed-Storage-and-Zero-Downtime-Rotation.md)
3. [Hashing User Passwords and High-Entropy vs Low-Entropy Secrets: Argon2id Parameters (pwdlib 0.3.1 / argon2-cffi 25.1.0) and Timing-Safe Dummy Verifies](Password-and-Client-Secret-Hashing-with-Argon2id-and-Pwdlib.md)
4. [From OAuth 2.0 (RFC 6749) to OAuth 2.1 (draft-ietf-oauth-v2-1-17) and OAuth Security BCP (RFC 9700): Mandatory PKCE and Removed Implicit/ROPC Grants](OAuth-2-0-to-OAuth-2-1-and-RFC-9700-Security-BCP.md)
5. [OAuth 2.0 for Browser-Based Applications (RFC 10017, August 2026): Backend-for-Frontend (BFF) Pattern vs In-Browser Token Storage](OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017.md)
6. [OpenID Connect (OIDC Core 1.0) vs OAuth 2.0: Why an ID Token Must Never Be Accepted as an API Access Token, Plus RFC 8414 and RFC 9728 Metadata](OpenID-Connect-OIDC-ID-Tokens-vs-Access-Tokens-and-Metadata-Discovery.md)
7. [JSON Web Tokens in Depth: JWS (RFC 7515), JWK (RFC 7517), JWA (RFC 7518), JWT (RFC 7519), and the RFC 9068 OAuth Access Token Profile](JWT-JWS-JWK-Structure-and-RFC-9068-Access-Token-Profile.md)
8. [JWT Vulnerability Anatomy and RFC 8725 Defenses: alg: none, RS256-to-HS256 Key Confusion, Unvalidated kid Path Traversal, and jku/x5u SSRF](JWT-Cryptographic-Pitfalls-and-RFC-8725-Best-Current-Practices.md)
9. [JWKS Endpoint Engineering: Public Key Caching, Graceful Signing Key Rotation (kid), and Preventing IDP DoS on Unknown kid Attacks](JWKS-Discovery-Caching-Key-Rotation-and-Thundering-Herd-Protection.md)
10. [Access and Refresh Token Lifecycles: Refresh Token Rotation with Reuse Family Revocation (RFC 9700), Token Introspection (RFC 7662), and Phantom Tokens](Refresh-Token-Rotation-Reuse-Detection-and-Token-Revocation-Architectures.md)
11. [Stopping Stolen Bearer Token Replay: Demonstrating Proof of Possession (DPoP, RFC 9449) and mTLS Certificate-Bound Tokens (RFC 8705)](Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705.md)
12. [East-West Service Authentication: OAuth Client Credentials vs RFC 8693 Token Exchange (On-Behalf-Of Delegation vs Impersonation)](Service-to-Service-Identity-Client-Credentials-and-Token-Exchange-RFC-8693.md)
13. [High-Assurance OAuth Flows: Pushed Authorization Requests (PAR, RFC 9126) and Fine-Grained Rich Authorization Requests (RAR, RFC 9396)](Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396.md)
14. [Cryptographic Request Non-Repudiation: Signing HTTP Requests with HTTP Message Signatures (RFC 9421) and Digest Fields (RFC 9530)](HTTP-Message-Signatures-RFC-9421-and-Content-Digests-RFC-9530.md)
15. [Privacy-Preserving Identity Claims with Selective Disclosure for JSON Web Tokens (SD-JWT, RFC 9901, November 2025)](Selective-Disclosure-JWTs-SD-JWT-RFC-9901.md)
16. [Integrating APIs with Enterprise Identity Providers (Okta, Entra ID): Multi-Tenant Issuer Validation and Automated SCIM 2.0 User/Group Deprovisioning](Enterprise-SSO-Integration-Multi-Tenant-OIDC-and-SCIM-Provisioning.md)
