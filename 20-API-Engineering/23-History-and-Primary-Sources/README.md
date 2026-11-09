---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# History and Primary Sources

Traces the intellectual and standards history behind modern API engineering through primary sources.
Covers Roy Fielding's work on Apache and HTTP/1.1 alongside his 2000 dissertation, the 30-year evolution of the HTTP RFCs (RFC 1945 to RFC 9110-9114 and RFC 10008), the rise and collapse of CORBA and WS-*/SOAP, the birth of JSON (RFC 8259) and OpenAPI, the evolution of Python web standards (CGI, WSGI PEP 333/3333, Tornado/Twisted, `asyncio` PEP 3156, ASGI 3.0), and the origin of Pydantic and FastAPI.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Thirty-Five Years of HTTP: From Tim Berners-Lee's 1991 HTTP/0.9 and RFC 1945/2616 to RFC 7230-7235, the 2022 RFC 9110-9114 Core Reorganization, and RFC 10008 (2026)](History-of-HTTP-From-HTTP-0-9-and-RFC-2616-to-RFC-9110-9114-and-RFC-10008.md)
2. [Reading Fielding's 2000 Dissertation (Chapters 1-6): Network-Based Architectural Styles, Hypermedia as the Engine of Application State, and Why Most 'REST' APIs Are HTTP RPC](Reading-Fieldings-2000-Dissertation-Why-REST-Was-Written-for-the-Web-and-Where-JSON-APIs-Diverged.md)
3. [The Rise and Collapse of CORBA, XML-RPC, and SOAP/WSDL/WS-*: Why Distributed Objects Failed and What Protobuf and gRPC Kept vs Discarded](The-Enterprise-RPC-Cycle-CORBA-XML-RPC-SOAP-WSDL-and-What-gRPC-Learned.md)
4. [From Douglas Crockford's JSON (RFC 4627 / RFC 8259) and Swagger 1.x/2.0 to OpenAPI 3.0, 3.1 (JSON Schema 2020-12 Alignment), and 3.2 (2025-2026)](History-of-JSON-JSON-Schema-and-the-OpenAPI-Specification-Swagger-to-3-2.md)
5. [The Evolution of API Identity: From HTTP Basic/Digest and OAuth 1.0a Request Signing to OAuth 2.0 (RFC 6749), OIDC, RFC 9700, OAuth 2.1, and Return to Proof-of-Possession (DPoP RFC 9449 / RFC 9421)](Evolution-of-API-Identity-From-HTTP-Basic-and-OAuth-1-0a-to-OAuth-2-1-OIDC-and-DPoP.md)
6. [Twenty-Five Years of Python Web Interfaces: CGI, mod_python, PEP 333 (2003), PEP 3333 (Python 3 WSGI), Django Channels, and the ASGI 3.0 Specification](History-of-Python-Web-Interfaces-CGI-Mod-Python-WSGI-PEP-333-3333-and-ASGI.md)
7. [The Road to Modern Async Python: asyncore, Twisted Deferreds, gevent Greenlets, PEP 380 (yield from), PEP 3156 (asyncio), PEP 492 (async/await), and Structured Concurrency (TaskGroup)](History-of-Async-Python-Select-Twisted-Tornado-Gevent-Yield-From-and-Asyncio.md)
8. [How Type Hints Reshaped Python APIs: PEP 484, Tom Christie's encode/Starlette/Uvicorn/httpx Ecosystem, Samuel Colvin's Pydantic v1 to Rust v2 Rewrite, and Sebastian Ramirez's FastAPI](The-Origin-and-Architecture-of-Pydantic-Starlette-and-FastAPI.md)
9. [Mike Bayer's SQLAlchemy Architecture Across Two Decades: Data Mapper vs Active Record, the 1.4/2.0 Unified Query Rewrite, PEP 484 Mapped[T], and 2.1 Async Maturity](The-Evolution-of-SQLAlchemy-From-0-1-to-1-4-Future-and-the-2-0-2-1-Typed-Async-Era.md)
10. [The Distinguished API Engineer's Primary Source Canon: Essential RFCs, Google AIPs, Systems Papers, and Source Files to Read Cover to Cover](Annotated-Primary-Source-Reading-Canon-for-Distinguished-API-Engineers.md)
