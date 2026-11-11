"""Wire, source, and semantic compatibility (AIP-180) proven against a typed Python client."""

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import FastAPI
from google.protobuf import json_format  # type: ignore[import-untyped]
from pydantic import ConfigDict, ValidationError

from api_labs.f07_versioning_evolution_and_api_guidelines.compatibility_taxonomy import (
    INT32,
    INT64,
    BooksClientKeywordOnlyV1,
    BooksClientKeywordOnlyV2,
    BooksClientPositionalOnlyV1,
    BooksClientPositionalOnlyV2Renamed,
    BooksClientV1,
    BooksClientV2Inserted,
    BooksClientV2Renamed,
    BookV1,
    BookV2Sdk,
    ListBooksResponseV1,
    StrictBookV1,
    TolerantBookFormat,
    TolerantBookV1,
    build_book_message,
    build_server_v1,
    build_server_v2,
    source_breaks,
    wire_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable

V1_BOOK = b'{"name":"books/1","title":"SICP","format":"HARDBACK"}'
V2_BOOK_ADDITIVE = b'{"name":"books/1","title":"SICP","format":"HARDBACK","isbn":"978-0262510875"}'
V2_BOOK_NEW_ENUM = b'{"name":"books/1","title":"SICP","format":"EBOOK"}'
V2_BOOK_RENAMED_KEY = b'{"name":"books/1","display_name":"SICP","format":"HARDBACK"}'


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- Wire: old client reads new server bytes ---------------------------------------------


@pytest.mark.unit
def test_additive_response_field_is_wire_compatible_only_for_ignoring_clients() -> None:
    assert wire_error(BookV1, V1_BOOK) is None
    assert wire_error(BookV1, V2_BOOK_ADDITIVE) is None
    assert wire_error(StrictBookV1, V1_BOOK) is None
    assert wire_error(StrictBookV1, V2_BOOK_ADDITIVE) == "extra_forbidden"


@pytest.mark.unit
def test_new_enum_value_breaks_closed_enum_and_tolerant_enum_absorbs_it() -> None:
    assert wire_error(BookV1, V2_BOOK_NEW_ENUM) == "enum"
    tolerant = TolerantBookV1.model_validate_json(V2_BOOK_NEW_ENUM)
    assert tolerant.format is TolerantBookFormat.FORMAT_UNSPECIFIED
    assert TolerantBookV1.model_validate_json(V1_BOOK).format is TolerantBookFormat.HARDBACK


@pytest.mark.unit
def test_renamed_json_key_is_a_wire_break_reported_as_missing() -> None:
    assert wire_error(BookV1, V2_BOOK_RENAMED_KEY) == "missing"
    assert wire_error(TolerantBookV1, V2_BOOK_RENAMED_KEY) == "missing"
    # A strict reader sees two errors, the absent old key first and the unknown new key second.
    with pytest.raises(ValidationError) as excinfo:
        StrictBookV1.model_validate_json(V2_BOOK_RENAMED_KEY)
    assert [error["type"] for error in excinfo.value.errors()] == ["missing", "extra_forbidden"]
    assert wire_error(StrictBookV1, V2_BOOK_RENAMED_KEY) == "missing"


@pytest.mark.unit
def test_ignoring_reader_erases_new_fields_on_full_replacement_write() -> None:
    ignoring = json.loads(BookV1.model_validate_json(V2_BOOK_ADDITIVE).model_dump_json())
    preserving = json.loads(TolerantBookV1.model_validate_json(V2_BOOK_ADDITIVE).model_dump_json())
    assert "isbn" not in ignoring
    assert preserving["isbn"] == "978-0262510875"


# --- Source: old code against the new SDK ------------------------------------------------


class _AliasSerializingBookV2(BookV2Sdk):
    """SDK v2 model whose config makes the alias the default serialization key."""

    model_config = ConfigDict(serialize_by_alias=True)


def _v1_user_code(book: Any) -> str:
    """Code written against SDK v1, where the attribute is `title`."""
    return str(book.title).upper()


@pytest.mark.unit
def test_attribute_rename_behind_alias_is_wire_compatible_and_source_breaking() -> None:
    v2_book = BookV2Sdk.model_validate_json(V1_BOOK)
    assert v2_book.model_dump_json(by_alias=True).encode() == V1_BOOK
    assert _v1_user_code(BookV1.model_validate_json(V1_BOOK)) == "SICP"
    with pytest.raises(AttributeError, match="title"):
        _v1_user_code(v2_book)
    # `validate_by_name=True` lets v2 code build the model by its new attribute name,
    # and the alias still puts the v1 key on the wire.
    by_name = BookV2Sdk.model_validate(
        {"name": "books/1", "display_name": "SICP", "format": "HARDBACK"}
    )
    assert by_name.model_dump_json(by_alias=True).encode() == V1_BOOK


@pytest.mark.unit
def test_alias_rename_breaks_the_wire_when_the_sdk_serializes_by_field_name() -> None:
    v2_book = BookV2Sdk.model_validate_json(V1_BOOK)
    by_name = json.loads(v2_book.model_dump_json())
    assert "title" not in by_name
    assert by_name["display_name"] == "SICP"
    assert wire_error(BookV1, v2_book.model_dump_json().encode()) == "missing"
    # The fix: `serialize_by_alias=True` restores the v1 bytes without `by_alias=True`.
    fixed = _AliasSerializingBookV2.model_validate_json(V1_BOOK)
    assert fixed.model_dump_json().encode() == V1_BOOK


@pytest.mark.unit
def test_inserted_positional_parameter_silently_rebinds_old_call_sites() -> None:
    old = BooksClientV1().create_book
    new = BooksClientV2Inserted().create_book
    assert source_breaks(old, new) == ["positional: validate_only -> request_id"]
    book = BookV1.model_validate_json(V1_BOOK)
    # An untyped v1 call site: `validate_only=True` passed positionally.
    call_site: Callable[..., str] = new
    assert old("shelves/1", book, True) == "shelves/1:books/1:True"
    assert call_site("shelves/1", book, True) == "shelves/1:books/1:True:False"


@pytest.mark.unit
def test_renamed_parameter_breaks_keyword_call_sites_only() -> None:
    findings = source_breaks(BooksClientV1().create_book, BooksClientV2Renamed().create_book)
    assert findings == ["keyword: TypeError: missing a required argument: 'shelf'"]


@pytest.mark.unit
def test_keyword_only_flags_make_parameter_insertion_source_compatible() -> None:
    old = BooksClientKeywordOnlyV1().create_book
    new = BooksClientKeywordOnlyV2().create_book
    assert source_breaks(old, new) == []
    assert source_breaks(old, old) == []
    # The reverse direction breaks both call shapes: v1 never accepted `request_id`.
    assert source_breaks(new, old) == [
        "positional: TypeError: got an unexpected keyword argument 'request_id'",
        "keyword: TypeError: got an unexpected keyword argument 'request_id'",
    ]


@pytest.mark.unit
def test_positional_only_parameters_make_a_rename_source_compatible() -> None:
    old = BooksClientPositionalOnlyV1().create_book
    new = BooksClientPositionalOnlyV2Renamed().create_book
    assert source_breaks(old, new) == []
    assert source_breaks(old, old) == []
    # The rename is invisible in both directions while v2 stays positional-only too.
    assert source_breaks(new, old) == []
    # A v2 that drops `/` still accepts every v1 call, but its own call shapes cannot go back.
    loosened = BooksClientV2Renamed().create_book
    assert source_breaks(old, loosened) == []
    assert source_breaks(loosened, old) == [
        "positional: TypeError: too many positional arguments",
        "keyword: TypeError: missing a required argument: 'parent'",
    ]
    book = BookV1.model_validate_json(V1_BOOK)
    assert new("shelves/1", book, validate_only=True) == old("shelves/1", book, validate_only=True)
    # The same rename on positional-or-keyword parameters is the keyword break proven above;
    # a positional-only v1 never let a caller write `parent=`.
    call_site: Callable[..., str] = old
    with pytest.raises(TypeError, match="positional-only arguments passed as keyword"):
        call_site(parent="shelves/1", book=book)


# --- Semantic: same bytes shape, different meaning ---------------------------------------


async def _old_client_count_all_books(app: FastAPI) -> tuple[int, dict[str, Any]]:
    """v1 client logic: one `GET`, then trust `books` as the complete collection."""
    async with _client(app) as client:
        response = await client.get("/v1/books")
    assert response.status_code == 200
    raw: dict[str, Any] = response.json()
    return len(ListBooksResponseV1.model_validate(raw).books), raw


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_default_page_size_is_wire_and_source_compatible_but_semantically_breaking() -> (
    None
):
    v1_count, v1_raw = await _old_client_count_all_books(build_server_v1()[0])
    v2_count, v2_raw = await _old_client_count_all_books(build_server_v2()[0])
    assert v1_count == 5
    assert "next_page_token" not in v1_raw
    assert v2_count == 2
    assert v2_raw["next_page_token"] == "2"  # noqa: S105 - a page cursor, not a secret


@pytest.mark.unit
@pytest.mark.asyncio
async def test_v2_client_that_follows_page_tokens_recovers_the_full_collection() -> None:
    app, _ = build_server_v2()
    pages: list[list[str]] = []
    tokens: list[str] = []
    page_token = ""
    async with _client(app) as client:
        # Bounded: five books can never need more than five pages.
        for _ in range(5):
            response = await client.get("/v1/books", params={"page_token": page_token})
            payload = response.json()
            pages.append(payload["books"])
            page_token = payload["next_page_token"]
            tokens.append(page_token)
            if not page_token:
                break
        explicit = await client.get("/v1/books", params={"page_size": 5})
        rejected = await client.get("/v1/books", params={"page_size": 0})
    assert pages == [["books/1", "books/2"], ["books/3", "books/4"], ["books/5"]]
    assert tokens == ["2", "4", ""]
    # Asking for the old page explicitly restores the v1 view, with no token after the last book.
    assert explicit.json() == {
        "books": ["books/1", "books/2", "books/3", "books/4", "books/5"],
        "next_page_token": "",
    }
    assert rejected.status_code == 422
    assert rejected.json()["detail"][0]["type"] == "greater_than_equal"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_client_flag_is_silently_ignored_by_old_server_and_mutates() -> None:
    body = {"title": "Dry run only", "validate_only": True}
    old_app, old_store = build_server_v1()
    new_app, new_store = build_server_v2()
    async with _client(old_app) as client:
        old_response = await client.post("/v1/books", json=body)
    async with _client(new_app) as client:
        new_response = await client.post("/v1/books", json=body)
    assert old_response.status_code == 200
    assert old_store == ["Dry run only"]
    assert new_response.status_code == 200
    assert new_store == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_v2_validate_only_default_reproduces_v1_create_for_old_clients() -> None:
    body = {"title": "Old client create"}
    old_app, old_store = build_server_v1()
    new_app, new_store = build_server_v2()
    async with _client(old_app) as client:
        old_response = await client.post("/v1/books", json=body)
    async with _client(new_app) as client:
        new_response = await client.post("/v1/books", json=body)
    assert old_response.json() == new_response.json() == {"name": "books/6"}
    assert old_store == new_store == ["Old client create"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strict_old_server_rejects_unknown_request_field_with_422() -> None:
    app, store = build_server_v1(strict_requests=True)
    async with _client(app) as client:
        response = await client.post("/v1/books", json={"title": "T", "validate_only": True})
        plain = await client.post("/v1/books", json={"title": "T"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
    assert plain.status_code == 200
    assert plain.json() == {"name": "books/6"}
    assert store == ["T"]


# --- Protobuf: wire compatibility is a property of the encoding --------------------------


@pytest.mark.unit
def test_protobuf_field_rename_is_binary_safe_and_protojson_breaking() -> None:
    book_v1 = build_book_message("lab.v1", "title", INT32)
    book_v2 = build_book_message("lab.v2", "display_name", INT32)
    sent = book_v2(name="books/1", display_name="SICP", copies=3)
    from_binary = book_v1()
    from_binary.ParseFromString(sent.SerializeToString())
    assert from_binary.title == "SICP"
    as_json = json_format.MessageToJson(sent)
    assert '"displayName": "SICP"' in as_json
    with pytest.raises(json_format.ParseError, match="displayName"):
        json_format.Parse(as_json, book_v1())
    lenient = json_format.Parse(as_json, book_v1(), ignore_unknown_fields=True)
    assert lenient.title == ""
    assert lenient.copies == 3


@pytest.mark.unit
def test_protobuf_int32_to_int64_truncates_in_binary_and_fails_loudly_in_protojson() -> None:
    book_v1 = build_book_message("lab.v1", "title", INT32)
    book_v2 = build_book_message("lab.v2", "title", INT64)
    big = 2**31 + 5
    sent = book_v2(name="books/1", title="SICP", copies=big)
    # Field 3, wire type 0 (varint): tag byte 0x18, then a 5-byte varint for 2**31 + 5.
    assert sent.SerializeToString().endswith(b"\x18\x85\x80\x80\x80\x08")
    old_reader = book_v1()
    old_reader.ParseFromString(sent.SerializeToString())
    assert old_reader.copies == big - 2**32
    # The old reader re-encodes its negative int32 as a sign-extended 10-byte varint.
    assert old_reader.SerializeToString().endswith(b"\x18\x85\x80\x80\x80\xf8\xff\xff\xff\xff\x01")
    relayed = book_v2()
    relayed.ParseFromString(old_reader.SerializeToString())
    assert relayed.copies == big - 2**32
    # ProtoJSON quotes int64, and the int32 parser rejects the value instead of truncating it.
    assert '"copies": "2147483653"' in json_format.MessageToJson(sent)
    with pytest.raises(json_format.ParseError, match="Value out of range: 2147483653"):
        json_format.Parse(json_format.MessageToJson(sent), book_v1())


@pytest.mark.unit
def test_protobuf_binary_relay_preserves_unknown_fields_and_protojson_drops_them() -> None:
    book_v1 = build_book_message("lab.v1", "title", INT32)
    book_v2 = build_book_message("lab.v2", "title", INT32, with_isbn=True)
    sent = book_v2(name="books/1", title="SICP", copies=3, isbn="978-0262510875")
    # Field 4, wire type 2 (LEN): tag byte 0x22, length 14, then the ISBN bytes.
    assert sent.SerializeToString().endswith(b"\x22\x0e978-0262510875")
    old_relay = book_v1()
    old_relay.ParseFromString(sent.SerializeToString())
    downstream = book_v2()
    downstream.ParseFromString(old_relay.SerializeToString())
    assert downstream.isbn == "978-0262510875"
    assert "isbn" not in json_format.MessageToJson(old_relay)
    assert old_relay.SerializeToString() == sent.SerializeToString()
