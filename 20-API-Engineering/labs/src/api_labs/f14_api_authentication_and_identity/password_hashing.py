"""Argon2id password storage with pwdlib, timing-equalized logins, and keyed client-secret digests.

Lab for the note Password-and-Client-Secret-Hashing-with-Argon2id-and-Pwdlib.
Low-entropy secrets (human passwords) get a salted, memory-hard Argon2id hash behind an HMAC
pepper; high-entropy secrets (server-generated client secrets) get one keyed HMAC-SHA-256,
because no amount of key stretching changes a 2**256 search.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import unicodedata
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import unquote_plus

from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping

    from argon2 import Parameters
    from pwdlib import PasswordHash

MAX_PASSWORD_BYTES: Final = 1024
# NIST SP 800-63B-4 Section 3.1.2.2: below 112 bits a secret needs a password hashing scheme.
HIGH_ENTROPY_BITS: Final = 112
CLIENT_SECRET_BYTES: Final = 32
# Module level: string annotations (PEP 563) are resolved against module globals, not closures.
BASIC_AUTH: Final = HTTPBasic(auto_error=False)


class InvalidPasswordInputError(ValueError):
    """The submitted password cannot be hashed (too long, or not encodable as UTF-8)."""


def argon2_hasher(params: Parameters) -> Argon2Hasher:
    """Build a pwdlib hasher from an ``argon2.Parameters`` profile."""
    return Argon2Hasher(
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=params.hash_len,
        salt_len=params.salt_len,
        type=params.type,
    )


def normalize_password(password: str) -> bytes:
    """NFC-normalize (NIST SP 800-63B-4 Section 3.1.1.2), encode as UTF-8, and cap the length."""
    try:
        data = unicodedata.normalize("NFC", password).encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogates cannot be stored or compared
        raise InvalidPasswordInputError("password is not valid Unicode text") from exc
    if len(data) > MAX_PASSWORD_BYTES:
        raise InvalidPasswordInputError(f"password exceeds {MAX_PASSWORD_BYTES} bytes")
    return data


def apply_pepper(password: bytes, pepper: bytes) -> bytes:
    """Pre-hash with HMAC-SHA-256 keyed by a pepper kept outside the user database."""
    return hmac.new(pepper, password, hashlib.sha256).digest()


@dataclass(frozen=True, slots=True)
class UserRecord:
    username: str
    password_hash: str | None  # None: SSO-only or disabled account, no usable password
    pepper_id: str | None = None


class PasswordAuthenticator:
    """Login verification whose cost does not depend on whether the account exists."""

    def __init__(
        self,
        password_hash: PasswordHash,
        users: MutableMapping[str, UserRecord],
        *,
        peppers: Mapping[str, bytes],
        current_pepper_id: str,
        max_concurrent_hashes: int = 4,
    ) -> None:
        self._ph = password_hash
        self._users = users
        self._peppers = dict(peppers)
        self._pepper_id = current_pepper_id
        # Each Argon2 call allocates memory_cost KiB; bound the peak instead of trusting the
        # size of whatever thread pool calls us.
        self._slots = threading.BoundedSemaphore(max_concurrent_hashes)
        # Hashed with the *current* parameters, so a dummy verify costs what a real one costs.
        self._dummy_hash = self._ph.hash(secrets.token_bytes(32))

    def _prepare(self, password: str, pepper_id: str) -> bytes:
        return apply_pepper(normalize_password(password), self._peppers[pepper_id])

    def register(self, username: str, password: str) -> UserRecord:
        prepared = self._prepare(password, self._pepper_id)
        with self._slots:
            stored = self._ph.hash(prepared)
        record = UserRecord(username, stored, self._pepper_id)
        self._users[username] = record
        return record

    def authenticate(self, username: str, password: str) -> UserRecord | None:
        """Return the user on success, ``None`` on any failure; one Argon2 verify either way."""
        # Raises before the lookup, so the rejection says nothing about the account.
        candidate = self._prepare(password, self._pepper_id)
        record = self._users.get(username)
        pepper_id = record.pepper_id if record is not None else None
        with self._slots:
            if (
                record is None
                or record.password_hash is None
                or pepper_id is None
                or pepper_id not in self._peppers
            ):
                self._ph.verify(candidate, self._dummy_hash)
                return None
            prepared = (
                candidate if pepper_id == self._pepper_id else self._prepare(password, pepper_id)
            )
            try:
                valid, updated = self._ph.verify_and_update(prepared, record.password_hash)
            except UnknownHashError:  # e.g. Django's "!" unusable-password marker
                self._ph.verify(candidate, self._dummy_hash)
                return None
            if not valid:
                return None
            if record.pepper_id != self._pepper_id:
                # Lazy pepper rotation: the plaintext exists only now, so re-wrap it here.
                updated = self._ph.hash(candidate)
            if updated is None:
                return record
            upgraded = replace(record, password_hash=updated, pepper_id=self._pepper_id)
        self._users[username] = upgraded
        return upgraded


EntropyStorage = Literal["keyed-fast-hash", "password-hash"]


def storage_scheme_for(entropy_bits: float) -> EntropyStorage:
    """Pick the storage class for a secret from its entropy, using the 112-bit threshold."""
    return "keyed-fast-hash" if entropy_bits >= HIGH_ENTROPY_BITS else "password-hash"


@dataclass(frozen=True, slots=True)
class ClientRecord:
    client_id: str
    secret_digest: bytes


class ClientSecretStore:
    """OAuth client secrets: 256 random bits, stored as HMAC-SHA-256 under a server key."""

    def __init__(self, server_key: bytes) -> None:
        self._key = server_key
        self._clients: dict[str, ClientRecord] = {}
        self._dummy_digest = self.digest(secrets.token_urlsafe(CLIENT_SECRET_BYTES))

    def digest(self, secret: str) -> bytes:
        return hmac.new(self._key, secret.encode("utf-8"), hashlib.sha256).digest()

    def issue(self, client_id: str) -> str:
        secret = secrets.token_urlsafe(CLIENT_SECRET_BYTES)
        self._clients[client_id] = ClientRecord(client_id, self.digest(secret))
        return secret

    def record(self, client_id: str) -> ClientRecord | None:
        return self._clients.get(client_id)

    def verify(self, client_id: str, secret: str) -> bool:
        record = self._clients.get(client_id)
        expected = record.secret_digest if record is not None else self._dummy_digest
        matches = hmac.compare_digest(self.digest(secret), expected)
        return record is not None and matches


def create_app(authenticator: PasswordAuthenticator, clients: ClientSecretStore) -> FastAPI:
    """Sync ``def`` endpoints, so FastAPI runs the Argon2 work in its thread pool."""
    app = FastAPI()

    @app.post("/login")
    def login(  # pyright: ignore[reportUnusedFunction]
        username: Annotated[str, Form()], password: Annotated[str, Form()]
    ) -> dict[str, str]:
        try:
            user = authenticator.authenticate(username, password)
        except InvalidPasswordInputError:
            user = None
        if user is None:
            # One body for unknown user, wrong password, and SSO-only account.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {"access_token": secrets.token_urlsafe(32), "token_type": "bearer"}

    @app.post("/client-token", response_model=None)
    def client_token(  # pyright: ignore[reportUnusedFunction]
        credentials: Annotated[HTTPBasicCredentials | None, Depends(BASIC_AUTH)],
    ) -> JSONResponse | dict[str, str]:
        # RFC 6749 Section 2.3.1: both halves are form-urlencoded before Basic encoding.
        if credentials is None or not clients.verify(
            unquote_plus(credentials.username), unquote_plus(credentials.password)
        ):
            # RFC 6749 Section 5.2: Basic was attempted, so 401 plus a matching challenge.
            return JSONResponse(
                {"error": "invalid_client"},
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": 'Basic realm="token"'},
            )
        return {"access_token": secrets.token_urlsafe(32), "token_type": "bearer"}

    return app
