"""Versioned wire-message construction and validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any


PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    """Raised when a wire message violates the protocol contract."""


class MessageType(StrEnum):
    CLIENT_REQUEST = "client_request"
    CLIENT_REPLY = "client_reply"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    ERROR = "error"


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field} must be an object")
    return dict(value)


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{field} must be a non-empty string")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProtocolError(f"{field} must be a positive integer")
    return value


def _require_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"{field} must be a non-negative integer")
    return value


def _validate_request_identity(message: dict[str, Any]) -> None:
    client_id = _require_string(message.get("client_id"), "client_id")
    replica_id = _require_string(message.get("replica_id"), "replica_id")
    request_num = _require_positive_int(message.get("request_num"), "request_num")
    request_id = _require_string(message.get("request_id"), "request_id")
    expected_request_id = f"{client_id}:{request_num}"
    if request_id != expected_request_id:
        raise ProtocolError(
            f"request_id must be {expected_request_id!r}, got {request_id!r}"
        )

    if message["type"] == MessageType.CLIENT_REQUEST:
        if message["source"] != client_id or message["destination"] != replica_id:
            raise ProtocolError("request source/destination does not match client/replica")
    elif message["source"] != replica_id or message["destination"] != client_id:
        raise ProtocolError("reply source/destination does not match replica/client")


def validate_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a message and return a plain-dict copy."""

    validated = _require_mapping(message, "message")
    if validated.get("version") != PROTOCOL_VERSION:
        raise ProtocolError(f"version must be {PROTOCOL_VERSION}")

    raw_type = _require_string(validated.get("type"), "type")
    try:
        message_type = MessageType(raw_type)
    except ValueError as exc:
        raise ProtocolError(f"unknown message type: {raw_type}") from exc

    validated["type"] = message_type.value
    validated["source"] = _require_string(validated.get("source"), "source")
    validated["destination"] = _require_string(
        validated.get("destination"), "destination"
    )
    payload = _require_mapping(validated.get("payload"), "payload")
    validated["payload"] = payload

    if message_type in {MessageType.CLIENT_REQUEST, MessageType.CLIENT_REPLY}:
        _validate_request_identity(validated)
        if message_type is MessageType.CLIENT_REQUEST:
            payload["operation"] = _require_string(
                payload.get("operation"), "operation"
            )
        else:
            payload["client_value"] = _require_non_negative_int(
                payload.get("client_value"), "client_value"
            )
    elif message_type in {MessageType.HEARTBEAT, MessageType.HEARTBEAT_ACK}:
        validated["heartbeat_count"] = _require_positive_int(
            validated.get("heartbeat_count"), "heartbeat_count"
        )
    else:
        payload["code"] = _require_string(payload.get("code"), "code")
        payload["detail"] = _require_string(payload.get("detail"), "detail")
        if "request_id" in validated:
            validated["request_id"] = _require_string(
                validated["request_id"], "request_id"
            )

    return validated


def encode_message(message: Mapping[str, Any]) -> bytes:
    """Encode one validated message as one compact JSON line."""

    validated = validate_message(message)
    text = json.dumps(validated, separators=(",", ":"), sort_keys=True)
    return f"{text}\n".encode("utf-8")


def decode_message(line: bytes) -> dict[str, Any]:
    """Decode and validate one UTF-8 JSON line."""

    try:
        decoded = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON message") from exc
    return validate_message(decoded)


def build_client_request(
    client_id: str, replica_id: str, request_num: int
) -> dict[str, Any]:
    message = {
        "version": PROTOCOL_VERSION,
        "type": MessageType.CLIENT_REQUEST.value,
        "source": client_id,
        "destination": replica_id,
        "client_id": client_id,
        "replica_id": replica_id,
        "request_num": request_num,
        "request_id": f"{client_id}:{request_num}",
        "payload": {"operation": "increment"},
    }
    return validate_message(message)


def build_client_reply(
    request: Mapping[str, Any], client_value: int
) -> dict[str, Any]:
    validated_request = validate_message(request)
    if validated_request["type"] != MessageType.CLIENT_REQUEST:
        raise ProtocolError("client reply requires a client_request")
    message = {
        "version": PROTOCOL_VERSION,
        "type": MessageType.CLIENT_REPLY.value,
        "source": validated_request["replica_id"],
        "destination": validated_request["client_id"],
        "client_id": validated_request["client_id"],
        "replica_id": validated_request["replica_id"],
        "request_num": validated_request["request_num"],
        "request_id": validated_request["request_id"],
        "payload": {"client_value": client_value},
    }
    return validate_message(message)


def build_heartbeat(
    lfd_id: str, replica_id: str, heartbeat_count: int
) -> dict[str, Any]:
    message = {
        "version": PROTOCOL_VERSION,
        "type": MessageType.HEARTBEAT.value,
        "source": lfd_id,
        "destination": replica_id,
        "heartbeat_count": heartbeat_count,
        "payload": {},
    }
    return validate_message(message)


def build_heartbeat_ack(heartbeat: Mapping[str, Any]) -> dict[str, Any]:
    validated_heartbeat = validate_message(heartbeat)
    if validated_heartbeat["type"] != MessageType.HEARTBEAT:
        raise ProtocolError("heartbeat ACK requires a heartbeat")
    message = {
        "version": PROTOCOL_VERSION,
        "type": MessageType.HEARTBEAT_ACK.value,
        "source": validated_heartbeat["destination"],
        "destination": validated_heartbeat["source"],
        "heartbeat_count": validated_heartbeat["heartbeat_count"],
        "payload": {},
    }
    return validate_message(message)


def build_error(
    source: str,
    destination: str,
    code: str,
    detail: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "type": MessageType.ERROR.value,
        "source": source,
        "destination": destination,
        "payload": {"code": code, "detail": detail},
    }
    if request_id is not None:
        message["request_id"] = request_id
    return validate_message(message)
