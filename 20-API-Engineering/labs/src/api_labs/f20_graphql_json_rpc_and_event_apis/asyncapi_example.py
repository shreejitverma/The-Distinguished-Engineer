"""A realistic AsyncAPI 3.1.0 document: one application on Kafka, AMQP 0-9-1, and WebSockets."""

from __future__ import annotations

import copy
from typing import Any

type JsonObject = dict[str, Any]


def _ref(pointer: str) -> JsonObject:
    return {"$ref": pointer}


_ORDER_DOCUMENT: JsonObject = {
    "asyncapi": "3.1.0",
    "info": {"title": "Order events", "version": "1.4.0"},
    "defaultContentType": "application/json",
    "servers": {
        "kafka": {
            "host": "kafka.example.com:9092",
            "protocol": "kafka",
            "bindings": {"kafka": {"schemaRegistryUrl": "https://registry.example.com"}},
        },
        "rabbit": {"host": "rabbit.example.com:5672", "protocol": "amqp", "pathname": "/prod"},
        "live": {"host": "ws.example.com", "protocol": "wss", "pathname": "/stream"},
    },
    "channels": {
        "orderPlaced": {
            "address": "orders.{region}.placed",
            "servers": [_ref("#/servers/kafka")],
            "parameters": {"region": {"enum": ["eu", "us"], "default": "eu"}},
            "messages": {
                "orderPlaced": _ref("#/components/messages/orderPlaced"),
                "orderCancelled": _ref("#/components/messages/orderCancelled"),
            },
            "bindings": {
                "kafka": {
                    "partitions": 12,
                    "replicas": 3,
                    "topicConfiguration": {"cleanup.policy": ["delete"], "retention.ms": 604800000},
                }
            },
        },
        "orderAudit": {
            "address": "order.audit",
            "servers": [_ref("#/servers/rabbit")],
            "messages": {"orderPlaced": _ref("#/components/messages/orderPlaced")},
            "bindings": {
                "amqp": {
                    "is": "routingKey",
                    "exchange": {"name": "audit", "type": "topic", "durable": True},
                }
            },
        },
        "orderStream": {
            "address": "/orders/{orderId}",
            "servers": [_ref("#/servers/live")],
            "parameters": {"orderId": {"description": "Order to follow"}},
            "messages": {"orderPlaced": _ref("#/components/messages/orderPlaced")},
            "bindings": {
                "ws": {
                    "method": "GET",
                    "query": {"type": "object", "properties": {"token": {"type": "string"}}},
                }
            },
        },
        "priceRequest": {
            "address": "orders.rpc",
            "messages": {"priceQuote": _ref("#/components/messages/priceQuote")},
        },
        "priceReply": {
            "address": None,
            "messages": {"priceQuoted": _ref("#/components/messages/priceQuoted")},
        },
    },
    "operations": {
        "publishOrderPlaced": {
            "action": "send",
            "channel": _ref("#/channels/orderPlaced"),
            "messages": [_ref("#/channels/orderPlaced/messages/orderPlaced")],
            "bindings": {"kafka": {"clientId": {"type": "string", "enum": ["order-writer"]}}},
        },
        "consumeOrderPlaced": {
            "action": "receive",
            "channel": _ref("#/channels/orderPlaced"),
            "bindings": {"kafka": {"groupId": {"type": "string", "enum": ["billing"]}}},
        },
        "streamOrder": {
            "action": "send",
            "channel": _ref("#/channels/orderStream"),
        },
        "quotePrice": {
            "action": "send",
            "channel": _ref("#/channels/priceRequest"),
            "reply": {
                "address": {"location": "$message.header#/replyTo"},
                "channel": _ref("#/channels/priceReply"),
            },
        },
    },
    "components": {
        "messages": {
            "orderPlaced": {
                "name": "OrderPlaced",
                "traits": [_ref("#/components/messageTraits/commonHeaders")],
                "payload": _ref("#/components/schemas/Order"),
                "correlationId": {"location": "$message.payload#/orderId"},
            },
            "orderCancelled": {
                "name": "OrderCancelled",
                "payload": {
                    "type": "object",
                    "required": ["orderId", "reason"],
                    "properties": {"orderId": {"type": "string"}, "reason": {"type": "string"}},
                },
            },
            "priceQuote": {
                "payload": {"type": "object", "required": ["sku"]},
                "headers": {"type": "object", "required": ["replyTo"]},
            },
            "priceQuoted": {"payload": {"type": "object", "required": ["cents"]}},
        },
        "messageTraits": {
            "commonHeaders": {
                "contentType": "application/json",
                "headers": {
                    "type": "object",
                    "properties": {"traceId": {"type": "string", "minLength": 4}},
                    "required": ["traceId"],
                },
            }
        },
        "schemas": {
            "Order": {
                "type": "object",
                "required": ["orderId", "amountCents"],
                "properties": {
                    "orderId": {"type": "string", "pattern": "^ord_[0-9]+$"},
                    "amountCents": {"type": "integer", "minimum": 1},
                    "currency": {"enum": ["EUR", "USD"]},
                },
                "additionalProperties": False,
            }
        },
    },
}


def order_document() -> JsonObject:
    """Return a fresh deep copy so a test can mutate it without leaking into another."""
    return copy.deepcopy(_ORDER_DOCUMENT)
