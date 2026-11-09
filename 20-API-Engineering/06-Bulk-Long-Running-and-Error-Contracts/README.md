---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Bulk Long Running and Error Contracts

Details machine-readable error contracts, batch and bulk mutations, asynchronous long-running operations (LROs), and large media uploads.
Contrasts IETF Problem Details for HTTP APIs (RFC 9457, which obsoleted RFC 7807) with the Google AIP-193 error model (`google.rpc.Status`, `ErrorInfo`, `RetryInfo`, `BadRequest`).
Covers batch get/create/update/delete (AIP-231 to AIP-235), partial failures (`207 Multi-Status`), LRO state machines (AIP-151), resumable uploads (`draft-ietf-httpbis-resumable-upload-15`), and dry-run validation (AIP-163).

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Standardized Error Responses with Problem Details for HTTP APIs (RFC 9457 vs Obsoleted RFC 7807)](Problem-Details-for-HTTP-APIs-RFC-9457.md)
2. [The Google AIP-193 Error Model: google.rpc.Status, ErrorInfo, RetryInfo, QuotaFailure, and BadRequest](Google-AIP-193-Error-Model-and-Rich-Error-Details.md)
3. [Designing an Error Taxonomy: Separating Transient vs Permanent Failures and Preventing Information Disclosure](Error-Taxonomy-Retryability-and-Client-Contract-Design.md)
4. [Transforming FastAPI and Pydantic v2 ValidationError Lists into RFC 9457 Problem Details](Mapping-Pydantic-Validation-Errors-to-RFC-9457-Problem-Details.md)
5. [Batch Methods: BatchGet, BatchCreate, BatchUpdate, and BatchDelete (Google AIP-231 through AIP-235)](Batch-Get-and-Bulk-Mutation-Design-AIP-231-to-AIP-235.md)
6. [Atomic All-or-Nothing Batch Transactions vs Partial Success with per-Item Status (207 Multi-Status)](Atomic-vs-Partial-Batch-Execution-and-207-Multi-Status.md)
7. [Criteria-Based Purge Operations: Filter-Driven Bulk Deletion, force=False Dry Runs, and LRO Handoff (AIP-165)](Criteria-Based-Bulk-Delete-and-Purge-Operations-AIP-165.md)
8. [Long-Running Operations (LROs): 202 Accepted, Operation Resources, done/metadata/result, and Polling (AIP-151)](Long-Running-Operations-202-Accepted-and-Operation-Resources-AIP-151.md)
9. [LRO Engineering Details: Waiter Long-Polling (:wait), Operation TTL Garbage Collection, and Idempotent Replay](Asynchronous-Job-Result-Expiration-Cancellation-and-Wait-Semantics.md)
10. [Change Validation and Dry-Run Requests (validate_only, Google AIP-163) via Database Savepoints](Dry-Run-and-Validate-Only-Requests-AIP-163.md)
11. [Streaming Multipart File Uploads (python-multipart) with Strict Size, Part-Count, and Memory Limits](Multipart-Form-Streaming-and-Upload-Memory-Bounds.md)
12. [Offloading Large Payloads from API Servers with Presigned Upload URLs and Cryptographic Upload Policies](Presigned-Object-Storage-URLs-and-Direct-to-Storage-Uploads.md)
13. [Resumable Uploads for HTTP: Upload Creation, Offset Recovery, and Appending Chunks (draft-ietf-httpbis-resumable-upload-15)](Resumable-HTTP-Uploads-IETF-Draft-and-Tus-Protocol.md)
14. [Request Identification: Client request_id (AIP-155) vs Server X-Request-ID and Trace Correlation](Idempotent-Request-Identification-and-Request-ID-Correlation-AIP-155.md)
