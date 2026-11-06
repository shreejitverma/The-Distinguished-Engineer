---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# In-Process BackgroundTasks vs Durable Distributed Queues (Celery 5.7.0, Dramatiq 2.2.1, and PostgreSQL Outbox)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [FastAPI Internals and Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[OpenAPI-Generation-Internals-in-FastAPI-Get-OpenAPI-and-Separate-Schemas]]

Next: [[Structuring-Large-FastAPI-Applications-APIRouter-Lifespan-and-Domain-Modules]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Demonstrates that an unhandled exception or SIGKILL inside Starlette BackgroundTasks loses the task after the client already received 202 Accepted, and contrasts it with a transactional outbox worker.

## Further reading
