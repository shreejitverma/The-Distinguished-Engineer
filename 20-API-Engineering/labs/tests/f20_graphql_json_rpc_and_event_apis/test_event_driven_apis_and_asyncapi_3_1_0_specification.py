"""Unit tests for AsyncAPI 3.1.0: channels, operations, messages, traits, and bindings."""

from __future__ import annotations

from typing import Any

import pytest

from api_labs.f20_graphql_json_rpc_and_event_apis.asyncapi_bindings import (
    BINDING_PROTOCOLS,
    KAFKA_DEFAULT_MAX_MESSAGE_BYTES,
    Finding,
    kafka_topic_problem,
    kafka_topics_collide,
    validate_binding,
)
from api_labs.f20_graphql_json_rpc_and_event_apis.asyncapi_document import (
    DEFAULT_SCHEMA_FORMAT,
    MESSAGE_TRAIT_FORBIDDEN,
    OPERATION_TRAIT_FORBIDDEN,
    AddressError,
    ExpressionError,
    MessageMatchError,
    RefError,
    TraitError,
    UnsupportedContentType,
    UnsupportedSchemaFormat,
    apply_traits,
    content_type,
    decode_payload,
    draft7_accepts_but_2020_12_rejects,
    effective_message,
    evaluate_runtime_expression,
    match_message,
    merge_patch,
    message_errors,
    operation_views,
    parse_pointer,
    render_address,
    reply_address,
    resolve_pointer,
    schema_errors,
    validate_document,
)
from api_labs.f20_graphql_json_rpc_and_event_apis.asyncapi_example import order_document

type Doc = dict[str, Any]

GOOD_HEADERS = {"traceId": "abcd1234"}
GOOD_ORDER = {"orderId": "ord_42", "amountCents": 1999, "currency": "EUR"}


def _rules(findings: tuple[Finding, ...] | list[Finding]) -> set[str]:
    return {finding.rule for finding in findings}


def _first(document: Doc) -> set[str]:
    return _rules(validate_document(document))


# --- channels, operations, and the send/receive split ---------------------------------------


@pytest.mark.unit
def test_reference_document_validates_clean() -> None:
    assert validate_document(order_document()) == ()


@pytest.mark.unit
def test_one_channel_serves_a_send_and_a_receive_operation() -> None:
    views = {view.operation_id: view for view in operation_views(order_document())}
    send, receive = views["publishOrderPlaced"], views["consumeOrderPlaced"]
    assert (send.action, receive.action) == ("send", "receive")
    assert send.channel_id == receive.channel_id == "orderPlaced"
    assert send.address == receive.address == "orders.{region}.placed"


@pytest.mark.unit
def test_omitted_messages_means_every_channel_message_and_empty_list_means_none() -> None:
    views = {view.operation_id: view for view in operation_views(order_document())}
    assert views["publishOrderPlaced"].message_ids == ("orderPlaced",)
    assert views["consumeOrderPlaced"].message_ids == ("orderPlaced", "orderCancelled")
    document = order_document()
    document["operations"]["consumeOrderPlaced"]["messages"] = []
    only = {view.operation_id: view for view in operation_views(document)}
    assert only["consumeOrderPlaced"].message_ids == ()


@pytest.mark.unit
def test_root_operation_must_reference_a_root_channel_not_components() -> None:
    document = order_document()
    document["components"]["channels"] = {"shared": document["channels"]["orderAudit"]}
    document["operations"]["publishOrderPlaced"]["channel"] = {
        "$ref": "#/components/channels/shared"
    }
    assert "operation.channel.not-root" in _first(document)

    dangling = order_document()
    dangling["operations"]["publishOrderPlaced"]["channel"] = {"$ref": "#/channels/nope"}
    assert "operation.channel.dangling" in _first(dangling)


@pytest.mark.unit
def test_operation_messages_must_be_a_subset_of_the_operation_channel() -> None:
    other_channel = order_document()
    other_channel["operations"]["publishOrderPlaced"]["messages"] = [
        {"$ref": "#/channels/orderAudit/messages/orderPlaced"}
    ]
    assert "operation.messages.not-channel-subset" in _first(other_channel)

    components = order_document()
    components["operations"]["publishOrderPlaced"]["messages"] = [
        {"$ref": "#/components/messages/orderPlaced"}
    ]
    assert "operation.messages.not-channel-subset" in _first(components)


@pytest.mark.unit
def test_operation_action_is_a_closed_enum() -> None:
    document = order_document()
    document["operations"]["publishOrderPlaced"]["action"] = "publish"
    assert "operation.action" in _first(document)


@pytest.mark.unit
def test_channel_parameters_must_match_address_expressions_both_ways() -> None:
    missing = order_document()
    del missing["channels"]["orderPlaced"]["parameters"]
    assert "channel.parameter.missing" in _first(missing)

    unused = order_document()
    unused["channels"]["orderAudit"]["parameters"] = {"region": {}}
    assert "channel.parameter.unused" in _first(unused)


@pytest.mark.unit
def test_channel_servers_must_reference_root_servers() -> None:
    document = order_document()
    document["channels"]["orderPlaced"]["servers"] = [{"$ref": "#/servers/ghost"}]
    assert "channel.servers.dangling" in _first(document)
    document["channels"]["orderPlaced"]["servers"] = [{"$ref": "#/components/servers/kafka"}]
    assert "channel.servers.not-root" in _first(document)


@pytest.mark.unit
def test_render_address_applies_default_enum_and_rejects_unknown_addresses() -> None:
    document = order_document()
    assert render_address(document, "orderPlaced", {}) == "orders.eu.placed"
    assert render_address(document, "orderPlaced", {"region": "us"}) == "orders.us.placed"
    with pytest.raises(AddressError, match="enum"):
        render_address(document, "orderPlaced", {"region": "ap"})
    with pytest.raises(AddressError, match="no value or default"):
        render_address(document, "orderStream", {})
    with pytest.raises(AddressError, match="unknown until runtime"):
        render_address(document, "priceReply", {})


@pytest.mark.unit
def test_version_field_must_be_semver_and_3_1() -> None:
    document = order_document()
    document["asyncapi"] = "3.0.0"
    assert _first(document) == {"asyncapi.version.minor"}
    document["asyncapi"] = "3.1"
    assert _first(document) == {"asyncapi.version"}
    document["asyncapi"] = "3.1.7-rc.1"
    assert _first(document) == set()


# --- JSON Pointer, merge patch, and traits --------------------------------------------------


@pytest.mark.unit
def test_json_pointer_decodes_tilde_one_before_tilde_zero() -> None:
    assert parse_pointer("/a~01b") == ["a~1b"]
    assert parse_pointer("/a~1b") == ["a/b"]
    assert resolve_pointer({"a/b": 1, "a~1b": 2}, "/a~01b") == 2
    with pytest.raises(RefError):
        parse_pointer("/a~2b")
    with pytest.raises(RefError):
        parse_pointer("a/b")


@pytest.mark.unit
def test_merge_patch_replaces_arrays_and_null_deletes() -> None:
    target = {"a": "b", "c": {"d": "e", "f": "g"}, "tags": [1, 2]}
    patch = {"a": "z", "c": {"f": None}, "tags": [3]}
    assert merge_patch(target, patch) == {"a": "z", "c": {"d": "e"}, "tags": [3]}
    assert merge_patch({"a": 1}, ["x"]) == ["x"]
    assert target["c"] == {"d": "e", "f": "g"}


@pytest.mark.unit
def test_traits_merge_in_order_and_the_object_itself_wins() -> None:
    document: Doc = {}
    node = {
        "description": "A longer description.",
        "traits": [
            {"name": "UserSignup", "description": "Description from trait."},
            {"name": "Second", "tags": [{"name": "user"}]},
        ],
    }
    merged = apply_traits(document, node, MESSAGE_TRAIT_FORBIDDEN)
    assert merged == {
        "name": "Second",
        "description": "A longer description.",
        "tags": [{"name": "user"}],
    }
    assert apply_traits(document, merged, MESSAGE_TRAIT_FORBIDDEN) == merged


@pytest.mark.unit
def test_object_null_removes_a_trait_property_and_arrays_are_not_concatenated() -> None:
    node = {
        "summary": None,
        "tags": [{"name": "own"}],
        "traits": [{"summary": "from trait", "tags": [{"name": "trait"}], "title": "T"}],
    }
    assert apply_traits({}, node, OPERATION_TRAIT_FORBIDDEN) == {
        "tags": [{"name": "own"}],
        "title": "T",
    }


@pytest.mark.unit
def test_traits_may_not_carry_fields_the_specification_forbids() -> None:
    operation = {"traits": [{"channel": {"$ref": "#/channels/x"}}]}
    with pytest.raises(TraitError, match="channel"):
        apply_traits({}, operation, OPERATION_TRAIT_FORBIDDEN)
    message = {"traits": [{"payload": {"type": "string"}}]}
    with pytest.raises(TraitError, match="payload"):
        apply_traits({}, message, MESSAGE_TRAIT_FORBIDDEN)
    # `messages` is forbidden in operation traits since 3.1.0 but allowed in message traits.
    assert "messages" in OPERATION_TRAIT_FORBIDDEN
    assert "messages" not in MESSAGE_TRAIT_FORBIDDEN


@pytest.mark.unit
def test_effective_message_inherits_headers_and_content_type_from_its_trait() -> None:
    message = effective_message(order_document(), "orderPlaced", "orderPlaced")
    assert message["contentType"] == "application/json"
    assert message["headers"]["required"] == ["traceId"]
    assert "traits" not in message


# --- runtime expressions and replies --------------------------------------------------------


@pytest.mark.unit
def test_runtime_expression_grammar_is_enforced() -> None:
    ok = evaluate_runtime_expression("$message.header#/a~1b", headers={"a/b": "x"}, payload=None)
    assert ok == "x"
    whole = evaluate_runtime_expression("$message.payload", headers={}, payload=[1])
    assert whole == [1]
    for bad in (
        "$message.body#/x",
        "message.header#/x",
        "$message.header#x",
        "$message.header#/~2",
    ):
        with pytest.raises(ExpressionError):
            evaluate_runtime_expression(bad, headers={}, payload={})


@pytest.mark.unit
def test_runtime_expressions_preserve_the_json_type() -> None:
    value = evaluate_runtime_expression("$message.payload#/n", headers={}, payload={"n": 7})
    assert value == 7
    assert isinstance(value, int)
    assert not isinstance(value, str)


@pytest.mark.unit
def test_invalid_expression_in_a_correlation_id_is_a_finding() -> None:
    document = order_document()
    document["components"]["messages"]["orderPlaced"]["correlationId"]["location"] = (
        "$msg.header#/id"
    )
    assert "expression.invalid" in _first(document)


@pytest.mark.unit
def test_reply_address_comes_from_the_request_message() -> None:
    document = order_document()
    address = reply_address(
        document, "quotePrice", headers={"replyTo": "inbox.7f3a"}, payload={"sku": "x"}
    )
    assert address == "inbox.7f3a"
    with pytest.raises(RefError):
        reply_address(document, "quotePrice", headers={}, payload={})


@pytest.mark.unit
def test_reply_address_and_channel_address_are_mutually_exclusive() -> None:
    document = order_document()
    document["channels"]["priceReply"]["address"] = "orders.rpc.reply"
    assert "reply.address.channel-address-set" in _first(document)
    del document["operations"]["quotePrice"]["reply"]["address"]
    assert _first(document) == set()
    assert reply_address(document, "quotePrice", headers={}, payload={}) == "orders.rpc.reply"


# --- payload conformance --------------------------------------------------------------------


@pytest.mark.unit
def test_conforming_message_passes_and_each_violation_is_reported() -> None:
    document = order_document()
    assert (
        message_errors(
            document, "orderPlaced", "orderPlaced", headers=GOOD_HEADERS, payload=GOOD_ORDER
        )
        == []
    )
    bad = {"orderId": "42", "amountCents": 0, "extra": True}
    errors = message_errors(
        document, "orderPlaced", "orderPlaced", headers={"traceId": "x"}, payload=bad
    )
    assert len(errors) == 4
    assert any(e.startswith("headers:") for e in errors)
    assert any("does not match" in e for e in errors)
    assert any("Additional properties" in e for e in errors)


@pytest.mark.unit
def test_default_schema_dialect_is_draft_07_so_2020_12_keywords_are_ignored() -> None:
    assert DEFAULT_SCHEMA_FORMAT == "application/vnd.aai.asyncapi+json;version=3.1.0"
    document = order_document()
    schema: Doc = {
        "type": "object",
        "properties": {"a": {}},
        "unevaluatedProperties": False,
        "dependentRequired": {"a": ["b"]},
    }
    instance = {"a": 1, "extra": 2}
    assert draft7_accepts_but_2020_12_rejects(schema, instance)
    assert schema_errors(document, schema, instance) == []


@pytest.mark.unit
def test_sibling_keywords_of_ref_are_ignored_in_draft_07() -> None:
    document = order_document()
    document["components"]["schemas"]["Small"] = {"type": "integer", "maximum": 5}
    with_sibling = {"$ref": "#/components/schemas/Small", "minimum": 100}
    assert schema_errors(document, with_sibling, 3) == []
    assert schema_errors(document, with_sibling, 9) == ["9 is greater than the maximum of 5"]


@pytest.mark.unit
def test_multi_format_schema_object_selects_the_validator_by_schema_format() -> None:
    document = order_document()
    draft07 = {
        "schemaFormat": "application/schema+json;version=draft-07",
        "schema": {"type": "string", "maxLength": 3},
    }
    assert schema_errors(document, draft07, "abcd") == ["'abcd' is too long"]
    avro = {
        "schemaFormat": "application/vnd.apache.avro;version=1.9.0",
        "schema": {"type": "string"},
    }
    with pytest.raises(UnsupportedSchemaFormat, match="avro"):
        schema_errors(document, avro, "x")
    old = {"schemaFormat": "application/vnd.aai.asyncapi+json;version=3.0.0", "schema": True}
    with pytest.raises(UnsupportedSchemaFormat):
        schema_errors(document, old, 1)


@pytest.mark.unit
def test_exactly_one_message_must_match_an_incoming_message() -> None:
    document = order_document()
    headers = GOOD_HEADERS
    assert (
        match_message(document, "consumeOrderPlaced", headers=headers, payload=GOOD_ORDER)
        == "orderPlaced"
    )
    cancelled = {"orderId": "ord_1", "reason": "fraud"}
    assert (
        match_message(document, "consumeOrderPlaced", headers={}, payload=cancelled)
        == "orderCancelled"
    )
    with pytest.raises(MessageMatchError, match="0 messages"):
        match_message(document, "consumeOrderPlaced", headers=headers, payload={"nope": 1})
    document["components"]["messages"]["orderCancelled"]["payload"] = {"type": "object"}
    document["components"]["messages"]["orderCancelled"]["headers"] = {"type": "object"}
    with pytest.raises(MessageMatchError, match="2 messages"):
        match_message(document, "consumeOrderPlaced", headers=headers, payload=GOOD_ORDER)


@pytest.mark.unit
def test_content_type_falls_back_to_the_document_default_and_only_json_decodes() -> None:
    document = order_document()
    assert content_type(document, {"contentType": "application/avro"}) == "application/avro"
    assert content_type(document, {}) == "application/json"
    del document["defaultContentType"]
    assert content_type(document, {}) == "application/json"
    document["defaultContentType"] = "application/x-protobuf"
    with pytest.raises(UnsupportedContentType):
        decode_payload(document, {}, b"\x08\x01")
    assert decode_payload(
        document, {"contentType": "application/json; charset=utf-8"}, b'{"a": 1}'
    ) == {"a": 1}


# --- bindings -------------------------------------------------------------------------------


@pytest.mark.unit
def test_every_bindings_object_lists_the_same_twenty_protocols() -> None:
    assert len(BINDING_PROTOCOLS) == 20
    assert "ros2" in BINDING_PROTOCOLS
    found = validate_binding("ros3", "channel", {}, "/p")
    assert _rules(found) == {"binding.protocol"}
    assert validate_binding("x-vendor", "channel", {"any": 1}, "/p") == []
    assert validate_binding("mqtt", "channel", {"anything": 1}, "/p") == []


@pytest.mark.unit
def test_kafka_topic_names_follow_topic_java() -> None:
    assert kafka_topic_problem("orders.eu.placed") is None
    assert kafka_topic_problem("a" * 249) is None
    assert "longer" in (kafka_topic_problem("a" * 250) or "")
    for bad in (".", "..", "", "orders/eu", "caf\u00e9"):
        assert kafka_topic_problem(bad) is not None
    assert kafka_topics_collide("orders.eu", "orders_eu")
    assert not kafka_topics_collide("orders.eu", "orders-eu")
    assert KAFKA_DEFAULT_MAX_MESSAGE_BYTES == 1048588


@pytest.mark.unit
def test_kafka_channel_binding_rejects_bad_values_and_unknown_fields() -> None:
    good = {"topic": "t", "partitions": 3, "replicas": 2}
    assert validate_binding("kafka", "channel", good, "/b") == []
    for field in ("partitions", "replicas"):
        for value in (0, -1, True, "3"):
            found = validate_binding("kafka", "channel", {field: value}, "/b")
            assert _rules(found) == {"kafka.channel.positive"}
    policy = {"topicConfiguration": {"cleanup.policy": ["delete", "archive"]}}
    assert _rules(validate_binding("kafka", "channel", policy, "/b")) == {
        "kafka.channel.cleanup-policy"
    }
    assert _rules(validate_binding("kafka", "channel", {"topic": "a b"}, "/b")) == {
        "kafka.channel.topic"
    }
    assert _rules(validate_binding("kafka", "channel", {"acks": "all"}, "/b")) == {
        "kafka.unknown-field"
    }


@pytest.mark.unit
def test_kafka_registry_fields_need_a_server_level_registry_url() -> None:
    message = {"schemaIdLocation": "payload", "schemaIdPayloadEncoding": "4"}
    without = validate_binding("kafka", "message", message, "/m", registry=False)
    assert len(without) == 2
    assert validate_binding("kafka", "message", message, "/m", registry=True) == []
    vendor = validate_binding("kafka", "server", {"schemaRegistryVendor": "confluent"}, "/s")
    assert _rules(vendor) == {"kafka.server.vendor-without-url"}


@pytest.mark.unit
def test_document_level_registry_check_reads_the_server_binding() -> None:
    document = order_document()
    document["channels"]["orderPlaced"]["messages"]["orderPlaced"] = {
        "payload": True,
        "bindings": {"kafka": {"schemaIdLocation": "payload"}},
    }
    assert _first(document) == set()
    del document["servers"]["kafka"]["bindings"]
    assert _first(document) == {"kafka.message.registry-field"}


@pytest.mark.unit
def test_amqp_channel_binding_couples_is_with_exchange_or_queue() -> None:
    queue = {"is": "queue", "queue": {"name": "q", "durable": True}}
    assert validate_binding("amqp", "channel", queue, "/c") == []
    mixed = {"is": "queue", "exchange": {"name": "x", "type": "topic"}}
    assert _rules(validate_binding("amqp", "channel", mixed, "/c")) == {
        "amqp.channel.exchange-for-queue"
    }
    default_is = {"queue": {"name": "q"}}
    assert _rules(validate_binding("amqp", "channel", default_is, "/c")) == {
        "amqp.channel.queue-for-routing-key"
    }
    assert _rules(validate_binding("amqp", "channel", {"is": "topic"}, "/c")) == {"amqp.channel.is"}
    odd = {"exchange": {"name": "x", "type": "broadcast"}}
    assert _rules(validate_binding("amqp", "channel", odd, "/c")) == {"amqp.channel.exchange-type"}


@pytest.mark.unit
def test_amqp_names_are_limited_in_utf8_octets_not_characters() -> None:
    ascii_255 = {"queue": {"name": "q" * 255}, "is": "queue"}
    assert validate_binding("amqp", "channel", ascii_255, "/c") == []
    two_byte: Doc = {"queue": {"name": "\u00e9" * 128}, "is": "queue"}
    assert len(two_byte["queue"]["name"]) == 128
    assert _rules(validate_binding("amqp", "channel", two_byte, "/c")) == {
        "amqp.channel.name-length"
    }


@pytest.mark.unit
def test_amqp_operation_binding_checks_delivery_mode_and_expiration() -> None:
    assert validate_binding("amqp", "operation", {"deliveryMode": 2, "expiration": 0}, "/o") == []
    assert _rules(validate_binding("amqp", "operation", {"deliveryMode": 3}, "/o")) == {
        "amqp.operation.delivery-mode"
    }
    assert _rules(validate_binding("amqp", "operation", {"expiration": -1}, "/o")) == {
        "amqp.operation.expiration"
    }
    assert _rules(validate_binding("amqp", "server", {"vhost": "/"}, "/s")) == {
        "amqp.server.reserved"
    }


@pytest.mark.unit
def test_websocket_binding_is_channel_only_and_post_is_flagged_against_rfc_6455() -> None:
    assert validate_binding("ws", "channel", {"method": "GET"}, "/c") == []
    post = validate_binding("ws", "channel", {"method": "POST"}, "/c")
    assert [(f.rule, f.severity) for f in post] == [("ws.channel.post-handshake", "warning")]
    assert _rules(validate_binding("ws", "channel", {"method": "PUT"}, "/c")) == {
        "ws.channel.method"
    }
    for level in ("server", "operation", "message"):
        found = validate_binding("ws", level, {"x": 1}, "/p")
        assert _rules(found) == {f"ws.{level}.reserved"}
    flat = {"query": {"type": "object"}}
    assert _rules(validate_binding("ws", "channel", flat, "/c")) == {"ws.channel.schema"}


@pytest.mark.unit
def test_one_document_carries_kafka_amqp_and_websocket_bindings_side_by_side() -> None:
    document = order_document()
    protocols = {
        protocol
        for channel in document["channels"].values()
        for protocol in channel.get("bindings", {})
    }
    assert protocols == {"kafka", "amqp", "ws"}
    document["channels"]["orderStream"]["bindings"]["ws"]["method"] = "PUT"
    document["channels"]["orderAudit"]["bindings"]["amqp"]["exchange"]["type"] = "broadcast"
    document["channels"]["orderPlaced"]["bindings"]["kafka"]["partitions"] = 0
    assert _first(document) == {
        "ws.channel.method",
        "amqp.channel.exchange-type",
        "kafka.channel.positive",
    }
