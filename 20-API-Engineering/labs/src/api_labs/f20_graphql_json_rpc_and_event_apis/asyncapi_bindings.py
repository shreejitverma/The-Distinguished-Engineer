"""Protocol bindings for AsyncAPI 3.1.0: Kafka 0.5.0, AMQP 0-9-1 0.3.0, and WebSockets 0.1.0.

A binding object carries protocol facts that the protocol-neutral core must not define.
Each binding README says "This object MUST contain only the properties defined above",
so every validator here rejects unknown fields before it checks values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

type Level = Literal["server", "channel", "operation", "message"]
type Severity = Literal["error", "warning"]

# The 20 protocol keys the 3.1.0 text lists in each of the four Bindings Objects.
# ROS 2 is the one added in 3.1.0.
BINDING_PROTOCOLS: Final = frozenset(
    {
        "http",
        "ws",
        "kafka",
        "anypointmq",
        "amqp",
        "amqp1",
        "mqtt",
        "mqtt5",
        "nats",
        "jms",
        "sns",
        "solace",
        "sqs",
        "stomp",
        "redis",
        "mercure",
        "ibmmq",
        "googlepubsub",
        "pulsar",
        "ros2",
    }
)

# Apache Kafka trunk, clients/.../common/internals/Topic.java: MAX_NAME_LENGTH and LEGAL_CHARS.
KAFKA_MAX_TOPIC_NAME_LENGTH: Final = 249
KAFKA_LEGAL_TOPIC_CHARS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
# ServerLogConfigs.MAX_MESSAGE_BYTES_DEFAULT = 1024 * 1024 + Records.LOG_OVERHEAD.
# Records.LOG_OVERHEAD is OFFSET_LENGTH 8 plus SIZE_LENGTH 4.
KAFKA_DEFAULT_MAX_MESSAGE_BYTES: Final = 1024 * 1024 + 12
AMQP_EXCHANGE_TYPES: Final = frozenset({"topic", "direct", "fanout", "default", "headers"})
AMQP_SHORTSTR_MAX_OCTETS: Final = 255

_KAFKA_CHANNEL_FIELDS: Final = frozenset(
    {"topic", "partitions", "replicas", "topicConfiguration", "bindingVersion"}
)
_KAFKA_SERVER_FIELDS: Final = frozenset(
    {"schemaRegistryUrl", "schemaRegistryVendor", "bindingVersion"}
)
_KAFKA_OPERATION_FIELDS: Final = frozenset({"groupId", "clientId", "bindingVersion"})
_KAFKA_MESSAGE_FIELDS: Final = frozenset(
    {"key", "schemaIdLocation", "schemaIdPayloadEncoding", "schemaLookupStrategy", "bindingVersion"}
)
_KAFKA_REGISTRY_ONLY: Final = (
    "schemaIdLocation",
    "schemaIdPayloadEncoding",
    "schemaLookupStrategy",
)
_AMQP_CHANNEL_FIELDS: Final = frozenset({"is", "exchange", "queue", "bindingVersion"})
_AMQP_OPERATION_FIELDS: Final = frozenset(
    {
        "expiration",
        "userId",
        "cc",
        "priority",
        "deliveryMode",
        "mandatory",
        "bcc",
        "timestamp",
        "ack",
        "bindingVersion",
    }
)
_AMQP_MESSAGE_FIELDS: Final = frozenset({"contentEncoding", "messageType", "bindingVersion"})
_WS_CHANNEL_FIELDS: Final = frozenset({"method", "query", "headers", "bindingVersion"})


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule violation, addressed by a JSON Pointer into the document."""

    rule: str
    path: str
    message: str
    severity: Severity = "error"


def kafka_topic_problem(name: str) -> str | None:
    """Return why Kafka rejects a topic name, mirroring Topic.detectInvalidTopic, or None."""
    if not name:
        return "the empty string is not allowed"
    if name in {".", ".."}:
        return f"{name!r} is not allowed"
    if len(name) > KAFKA_MAX_TOPIC_NAME_LENGTH:
        return f"longer than {KAFKA_MAX_TOPIC_NAME_LENGTH} characters"
    if not set(name) <= KAFKA_LEGAL_TOPIC_CHARS:
        return "contains characters other than ASCII alphanumerics, '.', '_' and '-'"
    return None


def kafka_topics_collide(first: str, second: str) -> bool:
    """Topic.hasCollision: '.' and '_' are interchangeable in metric names."""
    return first.replace(".", "_") == second.replace(".", "_")


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _unknown_fields(
    protocol: str, binding: dict[str, Any], allowed: frozenset[str], path: str
) -> list[Finding]:
    return [
        Finding(
            f"{protocol}.unknown-field", f"{path}/{name}", f"{name!r} is not a {protocol} field"
        )
        for name in sorted(binding)
        if name not in allowed and not name.startswith("x-")
    ]


def _must_be_empty(
    protocol: str, level: Level, binding: dict[str, Any], path: str
) -> list[Finding]:
    return [
        Finding(
            f"{protocol}.{level}.reserved",
            f"{path}/{name}",
            f"the {protocol} {level} binding is reserved and MUST NOT contain properties",
        )
        for name in sorted(binding)
        if not name.startswith("x-")
    ]


def _kafka(level: Level, binding: dict[str, Any], path: str, *, registry: bool) -> list[Finding]:
    findings: list[Finding] = []
    if level == "server":
        findings += _unknown_fields("kafka", binding, _KAFKA_SERVER_FIELDS, path)
        if "schemaRegistryVendor" in binding and "schemaRegistryUrl" not in binding:
            findings.append(
                Finding(
                    "kafka.server.vendor-without-url",
                    f"{path}/schemaRegistryVendor",
                    "schemaRegistryVendor MUST NOT be specified without schemaRegistryUrl",
                )
            )
    elif level == "channel":
        findings += _unknown_fields("kafka", binding, _KAFKA_CHANNEL_FIELDS, path)
        topic = binding.get("topic")
        if topic is not None:
            problem = kafka_topic_problem(topic) if isinstance(topic, str) else "not a string"
            if problem:
                findings.append(Finding("kafka.channel.topic", f"{path}/topic", problem))
        for name in ("partitions", "replicas"):
            if name in binding and not _is_positive_int(binding[name]):
                findings.append(
                    Finding(
                        "kafka.channel.positive",
                        f"{path}/{name}",
                        f"{name} MUST be a positive integer",
                    )
                )
        policy = binding.get("topicConfiguration", {}).get("cleanup.policy")
        if policy is not None and not (
            isinstance(policy, list) and set(policy) <= {"delete", "compact"}  # pyright: ignore[reportUnknownArgumentType]
        ):
            findings.append(
                Finding(
                    "kafka.channel.cleanup-policy",
                    f"{path}/topicConfiguration/cleanup.policy",
                    "cleanup.policy may only contain delete and/or compact",
                )
            )
    elif level == "operation":
        findings += _unknown_fields("kafka", binding, _KAFKA_OPERATION_FIELDS, path)
        for name in ("groupId", "clientId"):
            if name in binding and not isinstance(binding[name], dict):
                findings.append(
                    Finding(
                        "kafka.operation.schema",
                        f"{path}/{name}",
                        f"{name} MUST be a Schema Object",
                    )
                )
    else:
        findings += _unknown_fields("kafka", binding, _KAFKA_MESSAGE_FIELDS, path)
        findings += [
            Finding(
                "kafka.message.registry-field",
                f"{path}/{name}",
                f"{name} MUST NOT be specified when no schemaRegistryUrl is set at server level",
            )
            for name in _KAFKA_REGISTRY_ONLY
            if name in binding and not registry
        ]
    return findings


def _amqp_name_problem(kind: str, name: object, path: str) -> list[Finding]:
    if not isinstance(name, str):
        return [Finding("amqp.channel.name", path, f"{kind} name MUST be a string")]
    octets = len(name.encode("utf-8"))
    if octets > AMQP_SHORTSTR_MAX_OCTETS:
        return [
            Finding(
                "amqp.channel.name-length",
                path,
                f"{kind} name is {octets} UTF-8 octets; an AMQP 0-9-1 shortstr holds at most "
                f"{AMQP_SHORTSTR_MAX_OCTETS}",
            )
        ]
    return []


def _amqp(level: Level, binding: dict[str, Any], path: str) -> list[Finding]:
    findings: list[Finding] = []
    if level == "server":
        return _must_be_empty("amqp", level, binding, path)
    if level == "channel":
        findings += _unknown_fields("amqp", binding, _AMQP_CHANNEL_FIELDS, path)
        kind = binding.get("is", "routingKey")
        if kind not in {"queue", "routingKey"}:
            findings.append(
                Finding("amqp.channel.is", f"{path}/is", "is MUST be queue or routingKey")
            )
        if kind == "routingKey" and "queue" in binding:
            findings.append(
                Finding(
                    "amqp.channel.queue-for-routing-key",
                    f"{path}/queue",
                    "queue is only for is=queue",
                )
            )
        if kind == "queue" and "exchange" in binding:
            findings.append(
                Finding(
                    "amqp.channel.exchange-for-queue",
                    f"{path}/exchange",
                    "exchange is only for is=routingKey",
                )
            )
        exchange = binding.get("exchange", {})
        if "type" in exchange and exchange["type"] not in AMQP_EXCHANGE_TYPES:
            findings.append(
                Finding(
                    "amqp.channel.exchange-type", f"{path}/exchange/type", "unknown exchange type"
                )
            )
        for key in ("exchange", "queue"):
            if "name" in binding.get(key, {}):
                findings += _amqp_name_problem(key, binding[key]["name"], f"{path}/{key}/name")
    elif level == "operation":
        findings += _unknown_fields("amqp", binding, _AMQP_OPERATION_FIELDS, path)
        if "expiration" in binding and not _is_nonnegative_int(binding["expiration"]):
            findings.append(
                Finding(
                    "amqp.operation.expiration", f"{path}/expiration", "expiration MUST be >= 0"
                )
            )
        if "deliveryMode" in binding and binding["deliveryMode"] not in {1, 2}:
            findings.append(
                Finding(
                    "amqp.operation.delivery-mode",
                    f"{path}/deliveryMode",
                    "deliveryMode MUST be 1 or 2",
                )
            )
    else:
        findings += _unknown_fields("amqp", binding, _AMQP_MESSAGE_FIELDS, path)
    return findings


def _websockets(level: Level, binding: dict[str, Any], path: str) -> list[Finding]:
    if level != "channel":
        return _must_be_empty("ws", level, binding, path)
    findings = _unknown_fields("ws", binding, _WS_CHANNEL_FIELDS, path)
    method = binding.get("method")
    if method is not None and method not in {"GET", "POST"}:
        findings.append(
            Finding("ws.channel.method", f"{path}/method", "method MUST be GET or POST")
        )
    if method == "POST":
        findings.append(
            Finding(
                "ws.channel.post-handshake",
                f"{path}/method",
                "RFC 6455 Section 4.1 requires the opening handshake to be a GET",
                "warning",
            )
        )
    for name in ("query", "headers"):
        schema = binding.get(name)
        if schema is not None and not (
            isinstance(schema, dict) and schema.get("type") == "object" and "properties" in schema  # pyright: ignore[reportUnknownMemberType]
        ):
            findings.append(
                Finding(
                    "ws.channel.schema",
                    f"{path}/{name}",
                    f"{name} MUST be an object schema with properties",
                )
            )
    return findings


def validate_binding(
    protocol: str, level: Level, binding: object, path: str, *, registry: bool = False
) -> list[Finding]:
    """Validate one protocol entry of a Bindings Object.

    `registry` says whether some server of the document sets kafka.schemaRegistryUrl.
    Protocols without a validator here are accepted when their key is one of the 20 in the spec.
    """
    if protocol.startswith("x-"):
        return []
    if protocol not in BINDING_PROTOCOLS:
        return [
            Finding(
                "binding.protocol",
                path,
                f"{protocol!r} is not a protocol key of the {level} Bindings Object",
            )
        ]
    if not isinstance(binding, dict):
        return [Finding("binding.type", path, "a binding MUST be an object")]
    body: dict[str, Any] = binding  # pyright: ignore[reportUnknownVariableType]
    if "bindingVersion" in body and not isinstance(body["bindingVersion"], str):
        return [
            Finding("binding.version", f"{path}/bindingVersion", "bindingVersion MUST be a string")
        ]
    if protocol == "kafka":
        return _kafka(level, body, path, registry=registry)
    if protocol == "amqp":
        return _amqp(level, body, path)
    if protocol == "ws":
        return _websockets(level, body, path)
    return []
