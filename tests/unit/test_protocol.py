import json

import pytest

from ft_system.common.protocol import (
    ProtocolError,
    build_client_reply,
    build_client_request,
    build_error,
    build_heartbeat,
    build_heartbeat_ack,
    decode_message,
    encode_message,
)


def test_client_request_preserves_rubric_identifiers():
    message = build_client_request("C1", "S1", 7)

    assert message == {
        "version": 1,
        "type": "client_request",
        "source": "C1",
        "destination": "S1",
        "client_id": "C1",
        "replica_id": "S1",
        "request_num": 7,
        "request_id": "C1:7",
        "payload": {"operation": "increment"},
    }


def test_message_round_trip_preserves_request():
    message = build_client_request("C1", "S1", 7)

    assert decode_message(encode_message(message)) == message


def test_encoded_message_is_one_compact_json_line():
    encoded = encode_message(build_client_request("C1", "S1", 7))

    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert b" " not in encoded


def test_rejects_request_id_that_disagrees_with_request_num():
    message = build_client_request("C1", "S1", 7)
    message["request_id"] = "C1:8"

    with pytest.raises(ProtocolError, match="request_id"):
        encode_message(message)


def test_reply_preserves_request_identifiers_and_value():
    request = build_client_request("C2", "S1", 11)

    reply = build_client_reply(request, client_value=4)

    assert reply == {
        "version": 1,
        "type": "client_reply",
        "source": "S1",
        "destination": "C2",
        "client_id": "C2",
        "replica_id": "S1",
        "request_num": 11,
        "request_id": "C2:11",
        "payload": {"client_value": 4},
    }


def test_heartbeat_ack_preserves_heartbeat_count():
    heartbeat = build_heartbeat("LFD1", "S1", 42)

    ack = build_heartbeat_ack(heartbeat)

    assert heartbeat["heartbeat_count"] == 42
    assert ack == {
        "version": 1,
        "type": "heartbeat_ack",
        "source": "S1",
        "destination": "LFD1",
        "heartbeat_count": 42,
        "payload": {},
    }


def test_error_can_refer_to_a_logical_request():
    error = build_error(
        source="S1",
        destination="C1",
        code="unsupported_operation",
        detail="unsupported operation: decrement",
        request_id="C1:7",
    )

    assert error["payload"] == {
        "code": "unsupported_operation",
        "detail": "unsupported operation: decrement",
    }
    assert error["request_id"] == "C1:7"


@pytest.mark.parametrize("counter", [0, -1, True, 1.5])
def test_rejects_non_positive_integer_heartbeat_count(counter):
    with pytest.raises(ProtocolError, match="heartbeat_count"):
        build_heartbeat("LFD1", "S1", counter)


def test_rejects_unsupported_protocol_version():
    message = build_client_request("C1", "S1", 1)
    message["version"] = 2

    with pytest.raises(ProtocolError, match="version"):
        encode_message(message)


def test_rejects_unknown_message_type():
    message = build_client_request("C1", "S1", 1)
    message["type"] = "checkpoint"

    with pytest.raises(ProtocolError, match="type"):
        encode_message(message)


def test_rejects_missing_required_request_field():
    message = build_client_request("C1", "S1", 1)
    del message["client_id"]

    with pytest.raises(ProtocolError, match="client_id"):
        encode_message(message)


def test_rejects_empty_operation_but_leaves_operation_policy_to_server():
    message = build_client_request("C1", "S1", 1)
    message["payload"]["operation"] = ""

    with pytest.raises(ProtocolError, match="operation"):
        encode_message(message)

    message["payload"]["operation"] = "decrement"
    assert decode_message(encode_message(message))["payload"]["operation"] == "decrement"


@pytest.mark.parametrize(
    "line",
    [
        b"not-json\n",
        b"\xff\n",
        json.dumps(["not", "an", "object"]).encode() + b"\n",
    ],
)
def test_rejects_invalid_json_message(line):
    with pytest.raises(ProtocolError):
        decode_message(line)
