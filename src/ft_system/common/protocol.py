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
    REGISTER = "register"
    MEMBERSHIP = "membership"
    CHECKPOINT = "checkpoint"
    HELLO = "hello"
    MEMBERSHIP_REPORT = "membership_report"
    VIEW = "view"
    RM_COMMAND = "rm_command"
    REPLICA_STATUS = "replica_status"


RM_ID = "RM"
RM_PEER_KINDS = frozenset({"gfd", "server", "client"})
REPLICATION_MODES = frozenset({"active", "passive"})
RM_COMMANDS = frozenset({"set_ready", "transfer_state"})


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


def _require_id_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ProtocolError(f"{field} must be a list")
    ids = [_require_string(item, field) for item in value]
    if len(ids) != len(set(ids)):
        raise ProtocolError(f"{field} must not repeat an id")
    return ids


def _require_rm_destination(message: dict[str, Any]) -> None:
    if message["destination"] != RM_ID:
        raise ProtocolError(f"{message['type']} messages must be addressed to RM")


def _require_rm_source(message: dict[str, Any]) -> None:
    if message["source"] != RM_ID:
        raise ProtocolError(f"{message['type']} messages must come from RM")


def _validate_view(payload: dict[str, Any]) -> None:
    payload["view_num"] = _require_non_negative_int(payload.get("view_num"), "view_num")
    if _require_string(payload.get("mode"), "mode") not in REPLICATION_MODES:
        raise ProtocolError("mode must be active or passive")
    members = _require_id_list(payload.get("members"), "members")
    ready = _require_id_list(payload.get("ready"), "ready")
    if not set(ready) <= set(members):
        raise ProtocolError("ready replicas must be members")
    primary = payload.get("primary")
    if primary is not None and (payload["mode"] != "passive" or primary not in members):
        raise ProtocolError("primary must be a member of a passive view")
    payload["members"], payload["ready"], payload["primary"] = members, ready, primary


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
    elif message_type in {MessageType.REGISTER, MessageType.MEMBERSHIP}:
        payload["replica_id"] = _require_string(payload.get("replica_id"), "replica_id")
        if not isinstance(payload.get("healthy"), bool):
            raise ProtocolError("healthy must be a boolean")
        if validated["destination"] != "GFD":
            raise ProtocolError("membership messages must be addressed to GFD")
    elif message_type is MessageType.CHECKPOINT:
        if any(field in validated for field in ("client_id", "replica_id", "request_num", "request_id")):
            raise ProtocolError("checkpoint message type cannot carry client request fields")
        payload["checkpoint_count"] = _require_positive_int(
            payload.get("checkpoint_count"), "checkpoint_count"
        )
        state = _require_mapping(payload.get("state"), "state")
        for client_id, value in state.items():
            _require_string(client_id, "state client id")
            _require_non_negative_int(value, "state value")
        payload["state"] = state
        # The last applied request per client lets a new replica rebuild duplicate
        # detection and decide which of its logged requests the checkpoint covers.
        last = _require_mapping(payload.get("last_request_nums"), "last_request_nums")
        if set(last) != set(state):
            raise ProtocolError("last_request_nums must name exactly the state's clients")
        for value in last.values():
            _require_non_negative_int(value, "last_request_nums value")
        payload["last_request_nums"] = last
    elif message_type is MessageType.HELLO:
        _require_rm_destination(validated)
        if _require_string(payload.get("kind"), "kind") not in RM_PEER_KINDS:
            raise ProtocolError("hello kind must be gfd, server, or client")
        # Optional code and config fingerprints let the RM spot mismatched computers.
        for field in ("build", "config"):
            if field in payload:
                payload[field] = _require_string(payload[field], field)
    elif message_type is MessageType.MEMBERSHIP_REPORT:
        _require_rm_destination(validated)
        payload["members"] = _require_id_list(payload.get("members"), "members")
    elif message_type is MessageType.VIEW:
        _require_rm_source(validated)
        _validate_view(payload)
    elif message_type is MessageType.RM_COMMAND:
        _require_rm_source(validated)
        if _require_string(payload.get("command"), "command") not in RM_COMMANDS:
            raise ProtocolError("command must be set_ready or transfer_state")
        if payload["command"] == "transfer_state":
            payload["target"] = _require_string(payload.get("target"), "target")
    elif message_type is MessageType.REPLICA_STATUS:
        _require_rm_destination(validated)
        if not isinstance(payload.get("ready"), bool):
            raise ProtocolError("ready must be a boolean")
        payload["checkpoint_count"] = _require_non_negative_int(
            payload.get("checkpoint_count"), "checkpoint_count"
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


def build_membership(lfd_id: str, replica_id: str, healthy: bool, *, register: bool = False) -> dict[str, Any]:
    return validate_message({
        "version": PROTOCOL_VERSION,
        "type": MessageType.REGISTER if register else MessageType.MEMBERSHIP,
        "source": lfd_id, "destination": "GFD",
        "payload": {"replica_id": replica_id, "healthy": healthy},
    })


def build_checkpoint(source: str, destination: str, state: Mapping[str, int],
                     last_request_nums: Mapping[str, int], checkpoint_count: int) -> dict[str, Any]:
    return validate_message({
        "version": PROTOCOL_VERSION,
        "type": MessageType.CHECKPOINT,
        "source": source,
        "destination": destination,
        "payload": {"state": dict(state), "last_request_nums": dict(last_request_nums),
                    "checkpoint_count": checkpoint_count},
    })


def build_hello(source: str, kind: str, identity: Mapping[str, str] | None = None) -> dict[str, Any]:
    return validate_message({
        "version": PROTOCOL_VERSION, "type": MessageType.HELLO,
        "source": source, "destination": RM_ID, "payload": {"kind": kind, **(identity or {})},
    })


def build_membership_report(members: list[str]) -> dict[str, Any]:
    return validate_message({
        "version": PROTOCOL_VERSION, "type": MessageType.MEMBERSHIP_REPORT,
        "source": "GFD", "destination": RM_ID, "payload": {"members": list(members)},
    })


def build_view(destination: str, view: Mapping[str, Any]) -> dict[str, Any]:
    return validate_message({
        "version": PROTOCOL_VERSION, "type": MessageType.VIEW,
        "source": RM_ID, "destination": destination, "payload": dict(view),
    })


def build_rm_command(destination: str, command: str, target: str | None = None) -> dict[str, Any]:
    payload = {"command": command}
    if target is not None:
        payload["target"] = target
    return validate_message({
        "version": PROTOCOL_VERSION, "type": MessageType.RM_COMMAND,
        "source": RM_ID, "destination": destination, "payload": payload,
    })


def build_replica_status(replica_id: str, ready: bool, checkpoint_count: int) -> dict[str, Any]:
    return validate_message({
        "version": PROTOCOL_VERSION, "type": MessageType.REPLICA_STATUS,
        "source": replica_id, "destination": RM_ID,
        "payload": {"ready": ready, "checkpoint_count": checkpoint_count},
    })
