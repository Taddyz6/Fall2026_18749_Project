import json

import pytest

from ft_system.common.protocol import (
    ProtocolError,
    build_client_reply,
    build_client_request,
    build_error,
    build_heartbeat,
    build_heartbeat_ack,
    build_checkpoint,
    build_checkpoint_ack,
    build_hello,
    build_membership_report,
    build_replica_status,
    build_rm_command,
    build_view,
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


def test_checkpoint_carries_state_and_checkpoint_count():
    checkpoint = build_checkpoint("S1", "S2", {"C1": 4, "C2": 2}, {"C1": 4, "C2": 2}, 7)

    assert decode_message(encode_message(checkpoint)) == {
        "version": 1, "type": "checkpoint", "source": "S1", "destination": "S2",
        "payload": {"state": {"C1": 4, "C2": 2}, "last_request_nums": {"C1": 4, "C2": 2},
                    "checkpoint_count": 7},
    }


def test_checkpoint_requires_request_numbers_for_exactly_the_state_clients():
    with pytest.raises(ProtocolError, match="last_request_nums"):
        build_checkpoint("S1", "S2", {"C1": 4, "C2": 2}, {"C1": 4}, 1)


def test_checkpoint_ack_identifies_the_installed_checkpoint():
    ack = build_checkpoint_ack(build_checkpoint("S1", "S2", {"C1": 4}, {"C1": 4}, 7))
    assert decode_message(encode_message(ack)) == {
        "version": 1, "type": "checkpoint_ack", "source": "S2", "destination": "S1",
        "payload": {"checkpoint_count": 7},
    }


def test_checkpoint_ack_requires_a_positive_checkpoint_number():
    ack = build_checkpoint_ack(build_checkpoint("S1", "S2", {"C1": 4}, {"C1": 4}, 7))
    ack["payload"]["checkpoint_count"] = 0
    with pytest.raises(ProtocolError, match="checkpoint_count"):
        encode_message(ack)


def test_checkpoint_count_starts_at_one():
    with pytest.raises(ProtocolError, match="checkpoint_count"):
        build_checkpoint("S1", "S2", {"C1": 0}, {"C1": 0}, 0)


def test_rm_messages_round_trip():
    view = {"view_num": 3, "mode": "passive", "members": ["S1", "S2"], "ready": ["S1"], "primary": "S1"}
    messages = [
        build_hello("S1", "server"),
        build_membership_report(["S1", "S2"]),
        build_view("C1", view),
        build_rm_command("S1", "transfer_state", "S2"),
        build_rm_command("S2", "set_ready"),
        build_replica_status("S2", True, 4),
    ]

    for message in messages:
        assert decode_message(encode_message(message)) == message
    assert messages[2]["payload"] == view


@pytest.mark.parametrize("change", [
    {"ready": ["S3"]},
    {"primary": "S3"},
    {"mode": "active"},
    {"members": ["S1", "S1"]},
])
def test_view_rejects_inconsistent_membership(change):
    view = {"view_num": 1, "mode": "passive", "members": ["S1", "S2"], "ready": ["S1"], "primary": "S1"}

    with pytest.raises(ProtocolError):
        build_view("C1", view | change)


def test_rm_messages_check_direction():
    with pytest.raises(ProtocolError, match="addressed to RM"):
        encode_message(build_hello("S1", "server") | {"destination": "GFD"})
    with pytest.raises(ProtocolError, match="come from RM"):
        encode_message(build_rm_command("S1", "set_ready") | {"source": "S2"})
    with pytest.raises(ProtocolError, match="target"):
        build_rm_command("S1", "transfer_state")


@pytest.mark.parametrize("field", ["kind", "mode", "command"])
@pytest.mark.parametrize("value", [None, True, 42, [], {}])
def test_rm_control_fields_reject_non_strings_as_protocol_errors(field, value):
    messages = {
        "kind": build_hello("S1", "server"),
        "mode": build_view("C1", {"view_num": 1, "mode": "active", "members": ["S1"],
                                  "ready": ["S1"], "primary": None}),
        "command": build_rm_command("S1", "set_ready"),
    }
    message = messages[field]
    message["payload"][field] = value

    with pytest.raises(ProtocolError, match=field):
        decode_message(json.dumps(message).encode() + b"\n")


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
