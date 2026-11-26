"""FastAPI account service whose every read and write appends to the tamper-evident audit log.

The business write and its audit row commit in one SQLite transaction, so neither exists
without the other.
The actor comes from the authenticated principal, never from the request body.
If the audit row cannot be written the endpoint fails closed with `503` and returns no data.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from api_labs.f18_observability_profiling_and_operations.audit_hash_chain import (
    Actor,
    AuditEvent,
    AuditLog,
    AuditWriteError,
    Resource,
    field_diff,
)


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified caller; `on_behalf_of` carries the delegating party of an RFC 8693 token."""

    id: str
    roles: frozenset[str] = frozenset()
    on_behalf_of: str | None = None

    def actor(self) -> Actor:
        return Actor(id=self.id, kind="user", on_behalf_of=self.on_behalf_of)


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    balance_minor: int | None = None
    status: Literal["active", "frozen"] | None = None


ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    balance_minor INTEGER NOT NULL,
    status TEXT NOT NULL
)
"""


def seed_account(log: AuditLog, account_id: str, owner: str, balance_minor: int) -> None:
    with log.transaction() as tx:
        tx.conn.execute(ACCOUNT_SCHEMA)
        tx.conn.execute(
            "INSERT INTO account (id, owner, balance_minor, status) VALUES (?, ?, ?, 'active')",
            (account_id, owner, balance_minor),
        )


def create_app(log: AuditLog, tokens: Mapping[str, Principal]) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(AuditWriteError)
    def audit_unavailable(_request: Request, _exc: AuditWriteError) -> JSONResponse:
        return JSONResponse({"detail": "audit log unavailable"}, status_code=503)

    def authenticate(authorization: Annotated[str | None, Header()] = None) -> Principal:
        scheme, _, credential = (authorization or "").partition(" ")
        principal = tokens.get(credential) if scheme.lower() == "bearer" else None
        if principal is None:
            raise HTTPException(
                401, "authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        return principal

    def require(principal: Principal, role: str, action: str, resource: Resource) -> None:
        if role in principal.roles:
            return
        log.append(
            AuditEvent(
                principal.actor(), action, resource, outcome="denied", reason=f"needs {role}"
            )
        )
        raise HTTPException(403, "forbidden")

    @app.get("/accounts/{account_id}")
    def read_account(
        account_id: str,
        principal: Annotated[Principal, Depends(authenticate)],
        x_request_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        resource = Resource("account", account_id)
        require(principal, "teller", "account.read", resource)
        with log.transaction() as tx:
            row = tx.conn.execute(
                "SELECT owner, balance_minor, status FROM account WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "no such account")
            tx.append(
                AuditEvent(
                    principal.actor(),
                    "account.read",
                    resource,
                    subject=row[0],
                    request_id=x_request_id,
                )
            )
        return {"id": account_id, "owner": row[0], "balance_minor": row[1], "status": row[2]}

    @app.patch("/accounts/{account_id}")
    def patch_account(
        account_id: str,
        body: AccountPatch,
        principal: Annotated[Principal, Depends(authenticate)],
        x_request_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        resource = Resource("account", account_id)
        require(principal, "teller", "account.update", resource)
        changes = body.model_dump(exclude_none=True)
        with log.transaction() as tx:
            row = tx.conn.execute(
                "SELECT owner, balance_minor, status FROM account WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "no such account")
            before = {"balance_minor": row[1], "status": row[2]}
            after = {**before, **changes}
            tx.conn.execute(
                "UPDATE account SET balance_minor = ?, status = ? WHERE id = ?",
                (after["balance_minor"], after["status"], account_id),
            )
            tx.append(
                AuditEvent(
                    principal.actor(),
                    "account.update",
                    resource,
                    subject=row[0],
                    request_id=x_request_id,
                    diff=field_diff(before, after),
                )
            )
        return {"id": account_id, "owner": row[0], **after}

    @app.get("/audit/subjects/{subject}")
    def who_touched(
        subject: str, principal: Annotated[Principal, Depends(authenticate)]
    ) -> list[dict[str, Any]]:
        require(principal, "auditor", "audit.query", Resource("audit_subject", subject))
        found = log.entries_for_subject(subject)
        log.append(
            AuditEvent(
                principal.actor(),
                "audit.query",
                Resource("audit_subject", subject),
                subject=subject,
            )
        )
        return [
            {
                "seq": e.seq,
                "ts": e.ts,
                "actor": e.actor_id,
                "action": e.header_data()["action"],
                "outcome": e.header_data()["outcome"],
            }
            for e in found
        ]

    @app.get("/audit/verify")
    def verify(principal: Annotated[Principal, Depends(authenticate)]) -> dict[str, Any]:
        require(principal, "auditor", "audit.verify", Resource("audit_chain", log.chain_id))
        report = log.verify()
        return {
            "ok": report.ok,
            "entries": report.entries,
            "head_seq": report.head_seq,
            "head_hash": report.head_hash.hex(),
            "problems": [{"seq": p.seq, "kind": p.kind} for p in report.problems],
        }

    return app
