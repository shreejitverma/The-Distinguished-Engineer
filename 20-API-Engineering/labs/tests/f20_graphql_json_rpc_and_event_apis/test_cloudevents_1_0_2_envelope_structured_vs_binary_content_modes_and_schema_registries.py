"""CloudEvents 1.0.2: attributes, HTTP binary/structured/batched modes, schema evolution."""

import base64
import json
import math
from collections.abc import Callable
from typing import Any

import h11
import httpx
import pytest

from api_labs.f20_graphql_json_rpc_and_event_apis.cloudevents_envelope import (
    BATCH_MEDIA_TYPE,
    UNSET,
    CloudEvent,
    CloudEventError,
    HttpMessage,
    Mode,
    NotACloudEventError,
    UnsupportedEventFormatError,
    decode_header_value,
    dedup_key,
    detect_mode,
    encode_header_value,
    parse_message,
    to_batch,
    to_binary,
    to_structured,
)
from api_labs.f20_graphql_json_rpc_and_event_apis.cloudevents_receiver import build_app
from api_labs.f20_graphql_json_rpc_and_event_apis.cloudevents_schema_registry import (
    Compatibility,
    Field,
    IncompatibleSchemaError,
    SchemaRegistry,
    data_problems,
    frame,
    reader_violations,
    unframe,
    validate_event,
)

JSON_EVENT = CloudEvent(
    id="C234-1234-1234",
    source="/mycontext",
    type="com.example.someevent",
    time="2018-04-05T17:31:00Z",
    datacontenttype="application/json",
    extensions={"comexampleextension1": "value"},
    data={"appinfoA": "abc", "appinfoB": 123, "appinfoC": True},
)
XML_EVENT = CloudEvent(
    id="B234-1234-1234",
    source="/mycontext",
    type="com.example.someevent",
    datacontenttype="application/xml",
    data='<much wow="xml"/>',
)
BLOB_EVENT = CloudEvent(
    id="A234-1234-1234",
    source="/mycontext",
    type="com.example.someevent",
    datacontenttype="application/vnd.apache.thrift.binary",
    data=bytes(range(256)),
)
MINIMAL = CloudEvent(id="1", source="/s", type="t")


type Builder = Callable[[CloudEvent], HttpMessage]


def _batch_of_one(event: CloudEvent) -> HttpMessage:
    return to_batch([event])


def _members(message: HttpMessage) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(message.body)
    return loaded


# ---------------------------------------------------------------- context attributes


@pytest.mark.unit
@pytest.mark.parametrize("name", ["id", "source", "type", "specversion"])
def test_required_attributes_must_be_non_empty(name: str) -> None:
    fields: dict[str, Any] = {"id": "1", "source": "/s", "type": "t", "specversion": "1.0"}
    fields[name] = ""
    with pytest.raises(CloudEventError, match=name):
        CloudEvent(**fields)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    ["a\nb", "a\x7fb", "a\x85b", "a\ufdd0b", "a\U0001fffeb", "a\ud800b"],
    ids=["LF", "DEL", "C1", "FDD0", "plane1-FFFE", "lone-surrogate"],
)
def test_string_attributes_reject_controls_noncharacters_and_lone_surrogates(bad: str) -> None:
    with pytest.raises(CloudEventError):
        CloudEvent(id="1", source="/s", type="t", subject=bad)


@pytest.mark.unit
def test_a_valid_surrogate_pair_is_one_legal_code_point() -> None:
    assert CloudEvent(id="1", source="/s", type="t", subject="\U0001f600").subject == "\U0001f600"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["Foo", "foo_bar", "foo-bar", "data", "id", "data_base64"])
def test_extension_names_are_lowercase_alphanumerics_and_do_not_shadow_members(name: str) -> None:
    with pytest.raises(CloudEventError):
        CloudEvent(id="1", source="/s", type="t", extensions={name: "v"})


@pytest.mark.unit
def test_extension_integer_is_a_signed_32_bit_value() -> None:
    CloudEvent(id="1", source="/s", type="t", extensions={"lo": -(2**31), "hi": 2**31 - 1})
    with pytest.raises(CloudEventError, match="32-bit"):
        CloudEvent(id="1", source="/s", type="t", extensions={"big": 2**31})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("https://schemas.example.com/order/v1", True),
        ("urn:example:order:1", True),
        ("/schemas/order", False),
        ("https://schemas.example.com/order#/defs/x", False),
        ("https://exa mple.com", False),
    ],
)
def test_dataschema_must_be_an_absolute_uri_without_fragment(value: str, ok: bool) -> None:
    if ok:
        assert CloudEvent(id="1", source="/s", type="t", dataschema=value).dataschema == value
    else:
        with pytest.raises(CloudEventError, match="dataschema"):
            CloudEvent(id="1", source="/s", type="t", dataschema=value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value", ["2018-04-05T17:31:00Z", "2024-02-29T23:59:60+01:00", "2018-04-05t17:31:00.25Z"]
)
def test_time_accepts_rfc3339(value: str) -> None:
    assert CloudEvent(id="1", source="/s", type="t", time=value).time == value


@pytest.mark.unit
@pytest.mark.parametrize(
    "value", ["20180405", "2023-02-29T00:00:00Z", "2018-04-05T24:00:00Z", "2018-04-05T17:31:00"]
)
def test_time_rejects_what_rfc3339_forbids(value: str) -> None:
    with pytest.raises(CloudEventError, match="time"):
        CloudEvent(id="1", source="/s", type="t", time=value)


@pytest.mark.unit
def test_dedup_key_is_source_plus_id_not_id_alone() -> None:
    a = CloudEvent(id="1", source="/a", type="t")
    b = CloudEvent(id="1", source="/b", type="t")
    assert dedup_key(a) != dedup_key(b)
    assert dedup_key(a) == dedup_key(CloudEvent(id="1", source="/a", type="other"))


# ---------------------------------------------------------------- binary mode


@pytest.mark.unit
def test_binary_mode_maps_attributes_to_ce_headers_and_data_to_the_body() -> None:
    message = to_binary(JSON_EVENT)
    headers = dict(message.headers)
    assert headers["ce-specversion"] == "1.0"
    assert headers["ce-id"] == "C234-1234-1234"
    assert headers["ce-time"] == "2018-04-05T17:31:00Z"
    assert headers["ce-comexampleextension1"] == "value"
    assert headers["content-type"] == "application/json"
    assert "ce-datacontenttype" not in headers
    assert json.loads(message.body) == {"appinfoA": "abc", "appinfoB": 123, "appinfoC": True}
    assert b"specversion" not in message.body


@pytest.mark.unit
def test_binary_mode_sends_raw_bytes_untouched() -> None:
    message = to_binary(BLOB_EVENT)
    assert message.body == bytes(range(256))
    assert message.header("content-type") == "application/vnd.apache.thrift.binary"


@pytest.mark.unit
def test_header_encoding_matches_the_spec_example() -> None:
    assert encode_header_value("Euro \u20ac \U0001f600") == "Euro%20%E2%82%AC%20%F0%9F%98%80"
    assert decode_header_value("Euro%20%E2%82%AC%20%F0%9F%98%80") == "Euro \u20ac \U0001f600"


@pytest.mark.unit
def test_percent_and_double_quote_are_encoded_and_decoding_is_a_single_round() -> None:
    assert encode_header_value('50% "off"') == "50%25%20%22off%22"
    assert decode_header_value("%2541") == "%41"


@pytest.mark.unit
def test_decoding_accepts_lowercase_hex_and_legacy_quoted_strings() -> None:
    assert decode_header_value("%e2%82%ac") == "\u20ac"
    assert decode_header_value('"say \\"hi\\""') == 'say "hi"'


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["%C0%A0", "%FF", "50%", "%zz"])
def test_decoding_rejects_overlong_or_invalid_utf8_and_bare_percent(bad: str) -> None:
    with pytest.raises(CloudEventError):
        decode_header_value(bad)


@pytest.mark.unit
def test_encoded_values_never_contain_header_terminators() -> None:
    encoded = encode_header_value("a\r\nx-evil: 1\x00")
    assert encoded == "a%0D%0Ax-evil:%201%00"
    assert all(0x21 <= ord(c) <= 0x7E for c in encoded)


@pytest.mark.unit
def test_h11_rejects_a_raw_crlf_header_value_which_is_why_encoding_is_mandatory() -> None:
    client = h11.Connection(h11.CLIENT)
    with pytest.raises(h11.LocalProtocolError):
        client.send(
            h11.Request(
                method="POST", target="/", headers=[("host", "x"), ("ce-subject", "a\r\nx-evil: 1")]
            )
        )


def _h11_hop(message: HttpMessage) -> tuple[list[tuple[str, str]], bytes]:
    """Serialize `message` as an HTTP/1.1 request with h11 and parse it back."""
    client = h11.Connection(h11.CLIENT)
    headers: list[tuple[str, str]] = [
        ("host", "example.test"),
        ("content-length", str(len(message.body))),
    ]
    wire = client.send(
        h11.Request(method="POST", target="/events", headers=headers + list(message.headers))
    )
    assert wire is not None
    if message.body:
        wire += client.send(h11.Data(data=message.body)) or b""
    wire += client.send(h11.EndOfMessage()) or b""
    server = h11.Connection(h11.SERVER)
    server.receive_data(wire)
    request = server.next_event()
    assert isinstance(request, h11.Request)
    body = b""
    while True:
        event = server.next_event()
        if isinstance(event, h11.Data):
            body += bytes(event.data)
        elif isinstance(event, h11.EndOfMessage):
            break
    return [(k.decode("ascii"), v.decode("ascii")) for k, v in request.headers], body


@pytest.mark.unit
@pytest.mark.parametrize(
    "builder", [to_binary, to_structured, _batch_of_one], ids=["binary", "structured", "batch"]
)
def test_events_survive_a_real_http_1_1_serialization(builder: Builder) -> None:
    event = CloudEvent(
        id="e-1",
        source="https://example.com/a b".replace(" ", "%20"),
        type="com.example.t",
        subject="caf\xe9 \U0001f600 100%",
        datacontenttype="application/json",
        data={"k": [1, 2]},
    )
    headers, body = _h11_hop(builder(event))
    parsed = parse_message(headers, body)
    assert parsed.events == (event,)


# ---------------------------------------------------------------- structured mode


@pytest.mark.unit
def test_structured_mode_puts_every_attribute_and_the_data_in_the_body() -> None:
    message = to_structured(JSON_EVENT)
    assert message.header("content-type") == "application/cloudevents+json; charset=UTF-8"
    assert [n for n, _ in message.headers] == ["content-type"]
    members = _members(message)
    assert members["specversion"] == "1.0"
    assert members["datacontenttype"] == "application/json"
    assert members["comexampleextension1"] == "value"
    assert members["data"] == {"appinfoA": "abc", "appinfoB": 123, "appinfoC": True}


@pytest.mark.unit
def test_json_content_type_data_is_a_json_value_never_a_string_encoded_document() -> None:
    members = _members(to_structured(JSON_EVENT))
    assert isinstance(members["data"], dict)


@pytest.mark.unit
def test_non_json_content_type_data_is_a_string_member() -> None:
    members = _members(to_structured(XML_EVENT))
    assert members["data"] == '<much wow="xml"/>'
    assert "data_base64" not in members


@pytest.mark.unit
def test_bytes_data_uses_data_base64_and_never_data() -> None:
    members = _members(to_structured(BLOB_EVENT))
    assert "data" not in members
    assert base64.b64decode(members["data_base64"]) == bytes(range(256))


@pytest.mark.unit
def test_data_and_data_base64_together_are_rejected() -> None:
    body = json.dumps(
        {
            "specversion": "1.0",
            "id": "1",
            "source": "/s",
            "type": "t",
            "data": "x",
            "data_base64": "eA==",
        }
    )
    with pytest.raises(CloudEventError, match="mutually exclusive"):
        parse_message([("content-type", "application/cloudevents+json")], body.encode())


@pytest.mark.unit
def test_explicit_null_data_is_distinct_from_absent_data() -> None:
    base = {"specversion": "1.0", "id": "1", "source": "/s", "type": "t"}
    with_null = parse_message(
        [("content-type", "application/cloudevents+json")],
        json.dumps({**base, "data": None}).encode(),
    )
    absent = parse_message(
        [("content-type", "application/cloudevents+json")], json.dumps(base).encode()
    )
    assert with_null.events[0].data is None
    assert absent.events[0].data is UNSET


@pytest.mark.unit
def test_null_optional_attributes_mean_unset_but_null_required_attributes_fail() -> None:
    base = {
        "specversion": "1.0",
        "id": "1",
        "source": "/s",
        "type": "t",
        "subject": None,
        "time": None,
        "ext": None,
    }
    event = parse_message(
        [("content-type", "application/cloudevents+json")], json.dumps(base).encode()
    ).events[0]
    assert (event.subject, event.time, event.extensions) == (None, None, {})
    with pytest.raises(CloudEventError, match="REQUIRED"):
        parse_message(
            [("content-type", "application/cloudevents+json")],
            json.dumps({**base, "id": None}).encode(),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "event", [JSON_EVENT, XML_EVENT, BLOB_EVENT, MINIMAL], ids=["json", "xml", "blob", "minimal"]
)
def test_structured_round_trip_is_lossless(event: CloudEvent) -> None:
    parsed = parse_message(to_structured(event).headers, to_structured(event).body)
    assert parsed.mode is Mode.STRUCTURED
    assert parsed.events == (event,)


@pytest.mark.unit
def test_structured_extensions_keep_their_json_types() -> None:
    event = CloudEvent(id="1", source="/s", type="t", extensions={"n": 5, "flag": True, "s": "x"})
    parsed = parse_message(to_structured(event).headers, to_structured(event).body).events[0]
    assert parsed.extensions == {"n": 5, "flag": True, "s": "x"}
    assert isinstance(parsed.extensions["flag"], bool)


@pytest.mark.unit
def test_missing_datacontenttype_implies_json_and_binary_conversion_sets_it_explicitly() -> None:
    body = json.dumps(
        {"specversion": "1.0", "id": "1", "source": "/s", "type": "t", "data": "I'm just a string"}
    )
    event = parse_message([("content-type", "application/cloudevents+json")], body.encode()).events[
        0
    ]
    assert event.datacontenttype is None
    binary = to_binary(event)
    assert binary.header("content-type") == "application/json"
    assert binary.body == b'"I\'m just a string"'


@pytest.mark.unit
def test_mirrored_ce_headers_never_override_the_body() -> None:
    message = to_structured(JSON_EVENT, mirror_headers=True)
    assert message.header("ce-id") == "C234-1234-1234"
    tampered = tuple(("ce-id", "FORGED") if n == "ce-id" else (n, v) for n, v in message.headers)
    assert parse_message(tampered, message.body).events[0].id == "C234-1234-1234"


# ---------------------------------------------------------------- binary decode


@pytest.mark.unit
@pytest.mark.parametrize(
    "event", [JSON_EVENT, BLOB_EVENT, MINIMAL], ids=["json", "blob", "minimal"]
)
def test_binary_round_trip_is_lossless(event: CloudEvent) -> None:
    message = to_binary(event)
    parsed = parse_message(message.headers, message.body)
    assert parsed.mode is Mode.BINARY
    assert parsed.events == (event,)


@pytest.mark.unit
def test_binary_mode_returns_non_json_data_as_bytes_and_extensions_as_strings() -> None:
    event = CloudEvent(
        id="1",
        source="/s",
        type="t",
        datacontenttype="application/xml",
        data="<a/>",
        extensions={"n": 5, "flag": True},
    )
    message = to_binary(event)
    assert dict(message.headers)["ce-n"] == "5"
    assert dict(message.headers)["ce-flag"] == "true"
    parsed = parse_message(message.headers, message.body).events[0]
    assert parsed.data == b"<a/>"
    assert parsed.extensions == {"n": "5", "flag": "true"}


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["ce-id", "ce-source", "ce-type", "ce-specversion"])
def test_binary_mode_needs_all_four_mandatory_headers(missing: str) -> None:
    headers = [h for h in to_binary(MINIMAL).headers if h[0] != missing]
    with pytest.raises(NotACloudEventError, match=missing[3:]):
        parse_message(headers, b"")


@pytest.mark.unit
def test_ce_datacontenttype_header_is_rejected() -> None:
    headers = [*to_binary(JSON_EVENT).headers, ("ce-datacontenttype", "application/json")]
    with pytest.raises(CloudEventError, match="ce-datacontenttype"):
        parse_message(headers, b"{}")


@pytest.mark.unit
def test_a_repeated_ce_header_is_rejected_not_folded() -> None:
    headers = [*to_binary(MINIMAL).headers, ("CE-ID", "2")]
    with pytest.raises(CloudEventError, match="duplicate header ce-id"):
        parse_message(headers, b"")


@pytest.mark.unit
def test_repeated_content_type_is_rejected() -> None:
    headers = [*to_binary(JSON_EVENT).headers, ("Content-Type", "text/plain")]
    with pytest.raises(CloudEventError, match="multiple Content-Type"):
        parse_message(headers, b"{}")


@pytest.mark.unit
def test_header_names_are_case_insensitive() -> None:
    headers = [(n.upper(), v) for n, v in to_binary(MINIMAL).headers]
    assert parse_message(headers, b"").events == (MINIMAL,)


@pytest.mark.unit
def test_unknown_specversion_is_rejected_in_every_mode() -> None:
    binary = [(n, "2.0" if n == "ce-specversion" else v) for n, v in to_binary(MINIMAL).headers]
    with pytest.raises(CloudEventError, match="specversion"):
        parse_message(binary, b"")
    body = json.dumps({"specversion": "2.0", "id": "1", "source": "/s", "type": "t"}).encode()
    with pytest.raises(CloudEventError, match="specversion"):
        parse_message([("content-type", "application/cloudevents+json")], body)


@pytest.mark.unit
def test_lab_policy_empty_binary_body_reads_back_as_absent_data() -> None:
    event = CloudEvent(
        id="1", source="/s", type="t", datacontenttype="application/octet-stream", data=b""
    )
    message = to_binary(event)
    assert parse_message(message.headers, message.body).events[0].data is UNSET


# ---------------------------------------------------------------- mode detection and batch


@pytest.mark.unit
def test_batch_prefix_is_tested_before_the_structured_prefix() -> None:
    assert detect_mode("application/cloudevents-batch+json; charset=UTF-8") is Mode.BATCHED
    assert detect_mode("application/cloudevents+json") is Mode.STRUCTURED


@pytest.mark.unit
@pytest.mark.parametrize(
    "ctype",
    [
        "application/cloudevents+avro",
        "application/cloudevents-batch+avro",
        "application/cloudeventsx",
    ],
)
def test_unknown_event_formats_are_unsupported_not_silently_binary(ctype: str) -> None:
    with pytest.raises(UnsupportedEventFormatError):
        detect_mode(ctype)


@pytest.mark.unit
def test_mode_detection_ignores_case_and_parameters_and_defaults_to_binary() -> None:
    assert detect_mode("Application/CloudEvents+JSON ; charset=utf-8") is Mode.STRUCTURED
    assert detect_mode("application/json") is Mode.BINARY
    assert detect_mode(None) is Mode.BINARY


@pytest.mark.unit
def test_batch_round_trip_preserves_order_and_an_empty_batch_is_valid() -> None:
    events = [JSON_EVENT, XML_EVENT, BLOB_EVENT]
    message = to_batch(events)
    assert message.header("content-type") == f"{BATCH_MEDIA_TYPE}; charset=UTF-8"
    parsed = parse_message(message.headers, message.body)
    assert (parsed.mode, parsed.events) == (Mode.BATCHED, tuple(events))
    empty = to_batch([])
    assert empty.body == b"[]"
    assert parse_message(empty.headers, empty.body).events == ()


@pytest.mark.unit
def test_batched_and_structured_bodies_have_the_right_json_shape() -> None:
    for ctype, body in (
        ("application/cloudevents-batch+json", b"{}"),
        ("application/cloudevents+json", b"[]"),
    ):
        with pytest.raises(CloudEventError):
            parse_message([("content-type", ctype)], body)


# ---------------------------------------------------------------- strict JSON


@pytest.mark.unit
def test_duplicate_json_members_are_rejected_because_parsers_disagree_on_which_wins() -> None:
    body = b'{"specversion":"1.0","id":"1","id":"2","source":"/s","type":"t"}'
    with pytest.raises(CloudEventError, match="duplicate JSON member 'id'"):
        parse_message([("content-type", "application/cloudevents+json")], body)


@pytest.mark.unit
@pytest.mark.parametrize("body", [b'{"specversion":NaN}', b"\xef\xbb\xbf{}", b"\xff", b"{"])
def test_malformed_json_is_a_cloudevent_error(body: bytes) -> None:
    with pytest.raises(CloudEventError):
        parse_message([("content-type", "application/cloudevents+json")], body)


@pytest.mark.unit
def test_pathological_nesting_is_rejected_not_a_crash() -> None:
    depth = 100_000
    body = b"[" * depth + b"]" * depth
    with pytest.raises(CloudEventError):
        parse_message([("content-type", "application/cloudevents-batch+json")], body)


@pytest.mark.unit
def test_extension_values_must_be_boolean_integer_or_string() -> None:
    base: dict[str, Any] = {"specversion": "1.0", "id": "1", "source": "/s", "type": "t"}
    for bad in ([1], {"a": 1}, 1.5):
        body = json.dumps({**base, "ext": bad}).encode()
        with pytest.raises(CloudEventError, match="extension"):
            parse_message([("content-type", "application/cloudevents+json")], body)


@pytest.mark.unit
def test_nan_data_cannot_be_serialized() -> None:
    event = CloudEvent(id="1", source="/s", type="t", data=math.nan)
    with pytest.raises(CloudEventError):
        to_structured(event)


# ---------------------------------------------------------------- size


@pytest.mark.unit
def test_structured_mode_pays_the_base64_tax_that_binary_mode_avoids() -> None:
    rows: list[str] = []
    for size in (1, 2, 3, 1024, 49152):
        event = CloudEvent(
            id="1",
            source="/s",
            type="t",
            datacontenttype="application/octet-stream",
            data=bytes(size),
        )
        binary, structured = to_binary(event), to_structured(event)
        encoded = _members(structured)["data_base64"]
        assert len(encoded) == 4 * math.ceil(size / 3)
        assert len(binary.body) == size
        rows.append(
            f"payload={size:>6} binary={binary.wire_size():>6} "
            f"structured={structured.wire_size():>6}"
        )
    print("\n".join(rows))


@pytest.mark.unit
def test_the_64_kib_budget_admits_more_raw_payload_in_binary_mode() -> None:
    budget = 64 * 1024

    def largest(builder: Builder) -> int:
        low, high = 0, budget
        while low < high:
            mid = (low + high + 1) // 2
            event = CloudEvent(
                id="1",
                source="/s",
                type="t",
                datacontenttype="application/octet-stream",
                data=bytes(mid),
            )
            if builder(event).wire_size() <= budget:
                low = mid
            else:
                high = mid - 1
        return low

    binary, structured = largest(to_binary), largest(to_structured)
    print(f"max raw payload in 65536 wire bytes: binary={binary} structured={structured}")
    assert binary > structured
    assert 0.74 < structured / binary < 0.76


# ---------------------------------------------------------------- schema evolution


V1 = {"order_id": Field("string"), "qty": Field("int")}


@pytest.mark.unit
def test_backward_accepts_a_defaulted_field_and_rejects_a_new_required_field() -> None:
    registry = SchemaRegistry()
    registry.register("order", V1)
    registry.register("order", {**V1, "note": Field("string", has_default=True)})
    with pytest.raises(IncompatibleSchemaError, match="'sku'"):
        registry.register(
            "order", {**V1, "note": Field("string", has_default=True), "sku": Field("string")}
        )


@pytest.mark.unit
def test_backward_allows_removing_a_field_but_forward_does_not() -> None:
    smaller = {"order_id": Field("string")}
    backward = SchemaRegistry(compatibility=Compatibility.BACKWARD)
    backward.register("order", V1)
    backward.register("order", smaller)
    forward = SchemaRegistry(compatibility=Compatibility.FORWARD)
    forward.register("order", V1)
    with pytest.raises(IncompatibleSchemaError, match="'qty'"):
        forward.register("order", smaller)


@pytest.mark.unit
def test_full_needs_both_directions() -> None:
    registry = SchemaRegistry(compatibility=Compatibility.FULL)
    registry.register("order", V1)
    with pytest.raises(IncompatibleSchemaError):
        registry.register("order", {**V1, "sku": Field("string")})
    with pytest.raises(IncompatibleSchemaError):
        registry.register("order", {"order_id": Field("string")})
    registry.register("order", {**V1, "sku": Field("string", has_default=True)})


@pytest.mark.unit
def test_plain_backward_only_checks_the_previous_version_but_transitive_checks_all() -> None:
    v2 = {**V1, "note": Field("string", has_default=True)}
    v3 = {**V1, "note": Field("string")}  # default dropped: fine against v2, fatal against v1
    loose = SchemaRegistry(compatibility=Compatibility.BACKWARD)
    strict = SchemaRegistry(compatibility=Compatibility.BACKWARD_TRANSITIVE)
    for registry in (loose, strict):
        registry.register("order", V1)
        registry.register("order", v2)
    assert loose.register("order", v3).version == 3
    with pytest.raises(IncompatibleSchemaError, match="'note'"):
        strict.register("order", v3)


@pytest.mark.unit
def test_type_changes_are_incompatible_except_int_to_number_promotion() -> None:
    assert reader_violations({"qty": Field("int")}, {"qty": Field("number")}) == []
    assert reader_violations({"qty": Field("number")}, {"qty": Field("int")}) != []
    assert reader_violations({"qty": Field("string")}, {"qty": Field("int")}) != []


@pytest.mark.unit
def test_ids_are_global_and_versions_are_per_subject_with_dataschema_uris() -> None:
    registry = SchemaRegistry(base_uri="https://schemas.example.com")
    first = registry.register("order", V1)
    other = registry.register("invoice", {"n": Field("int")})
    second = registry.register("order", {**V1, "note": Field("string", has_default=True)})
    assert [first.schema_id, other.schema_id, second.schema_id] == [1, 2, 3]
    assert [first.version, other.version, second.version] == [1, 1, 2]
    assert second.uri == "https://schemas.example.com/subjects/order/versions/2"


@pytest.mark.unit
def test_an_incompatible_change_ships_as_a_new_type_with_its_own_subject() -> None:
    registry = SchemaRegistry()
    registry.register("com.example.order.created.v1", V1)
    v2 = {"order_id": Field("string"), "qty": Field("int"), "sku": Field("string")}
    with pytest.raises(IncompatibleSchemaError):
        registry.register("com.example.order.created.v1", v2)
    assert registry.register("com.example.order.created.v2", v2).version == 1


@pytest.mark.unit
def test_confluent_frame_layout_is_magic_zero_then_big_endian_id() -> None:
    assert frame(0x01020304, b"xyz") == b"\x00\x01\x02\x03\x04xyz"
    assert unframe(frame(70000, b"payload")) == (70000, b"payload")
    with pytest.raises(ValueError, match="magic"):
        unframe(b"\x01\x00\x00\x00\x01x")
    with pytest.raises(ValueError, match="shorter"):
        unframe(b"\x00\x00\x00")


@pytest.mark.unit
def test_validate_event_resolves_dataschema_and_checks_data_against_that_version() -> None:
    registry = SchemaRegistry(compatibility=Compatibility.NONE)
    v1 = registry.register("order", V1)
    v2 = registry.register("order", {**V1, "sku": Field("string")})
    event = CloudEvent(
        id="1", source="/s", type="t", dataschema=v1.uri, data={"order_id": "o", "qty": 2}
    )
    assert validate_event(registry, event) == []
    as_v2 = CloudEvent(
        id="1", source="/s", type="t", dataschema=v2.uri, data={"order_id": "o", "qty": 2}
    )
    assert validate_event(registry, as_v2) == ["missing required field 'sku'"]


@pytest.mark.unit
def test_validate_event_reports_unresolvable_or_mistyped_data() -> None:
    registry = SchemaRegistry()
    entry = registry.register("order", V1)
    assert validate_event(registry, MINIMAL) == ["event has no dataschema"]
    unknown = CloudEvent(id="1", source="/s", type="t", dataschema="https://x.example/none")
    assert validate_event(registry, unknown) == ["unknown dataschema https://x.example/none"]
    bool_qty = CloudEvent(
        id="1", source="/s", type="t", dataschema=entry.uri, data={"order_id": "o", "qty": True}
    )
    assert validate_event(registry, bool_qty) == ["field 'qty' is not a int"]
    assert data_problems(V1, [1]) == ["data is not a JSON object"]


# ---------------------------------------------------------------- ASGI receiver


@pytest.fixture
def client() -> httpx.AsyncClient:
    app = build_app(allowed_origins=frozenset({"emitter.example.com"}), max_body=2048)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "mode", "ids"),
    [
        (to_binary(JSON_EVENT), "binary", ["C234-1234-1234"]),
        (to_structured(JSON_EVENT), "structured", ["C234-1234-1234"]),
        (to_batch([JSON_EVENT, XML_EVENT]), "batched", ["C234-1234-1234", "B234-1234-1234"]),
    ],
    ids=["binary", "structured", "batched"],
)
async def test_receiver_accepts_each_content_mode(
    client: httpx.AsyncClient, message: HttpMessage, mode: str, ids: list[str]
) -> None:
    async with client:
        response = await client.post("/events", headers=list(message.headers), content=message.body)
    assert response.status_code == 202
    assert response.json() == {"mode": mode, "ids": ids}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_receiver_answers_415_for_an_event_format_it_cannot_decode(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.post(
            "/events", headers={"content-type": "application/cloudevents+avro"}, content=b"\x00"
        )
    assert response.status_code == 415


@pytest.mark.unit
@pytest.mark.asyncio
async def test_receiver_answers_400_for_a_message_that_is_not_a_cloudevent(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.post(
            "/events", headers={"content-type": "text/plain"}, content=b"hello"
        )
    assert response.status_code == 400
    assert "mandatory headers" in response.json()["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_receiver_caps_the_body_with_413(client: httpx.AsyncClient) -> None:
    async with client:
        response = await client.post(
            "/events", headers={"content-type": "application/octet-stream"}, content=bytes(4096)
        )
    assert response.status_code == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_webhook_validation_handshake_grants_only_known_origins(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        granted = await client.options(
            "/events",
            headers={
                "WebHook-Request-Origin": "emitter.example.com",
                "WebHook-Request-Rate": "120",
            },
        )
        unknown = await client.options(
            "/events", headers={"WebHook-Request-Origin": "evil.example.net"}
        )
        absent = await client.options("/events")
    assert granted.status_code == 200
    assert granted.headers["webhook-allowed-origin"] == "emitter.example.com"
    assert granted.headers["webhook-allowed-rate"] == "120"
    assert granted.headers["allow"] == "POST"
    for refused in (unknown, absent):
        assert refused.status_code == 405
        assert "webhook-allowed-origin" not in refused.headers
        assert refused.headers["allow"] == "POST"
