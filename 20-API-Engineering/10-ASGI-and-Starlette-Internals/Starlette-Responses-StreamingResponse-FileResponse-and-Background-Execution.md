---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Starlette Response Internals: JSONResponse, StreamingResponse Client-Disconnect Listening, and FileResponse Chunked File I/O

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [ASGI and Starlette Internals](README.md) in [API Engineering](../README.md).

## TL;DR

## Mental model

## How it works

## Numbers and limits

## Portability

## Worked example

## Pitfalls

## Security and robustness

## The principal's view

## Interview questions

## Related

Prerequisites: [[Starlette-Request-Internals-Stream-Consumption-and-Body-Caching]]

Next: [[Pure-ASGI-Middleware-Onion-Architecture-and-Send-Receive-Wrapping]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Verifies how Starlette 1.10.0 StreamingResponse runs listen_for_disconnect(receive) concurrently with stream_response(send) inside an anyio.create_task_group() to abort slow generators when the client drops.

## Further reading
