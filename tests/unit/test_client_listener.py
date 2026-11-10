import io

import pytest

from ft_system.common.logging import EventLogger
from ft_system.common.protocol import build_client_request, decode_message
from ft_system.server.app import ClientListener
from ft_system.server.state_machine import DeterministicStateMachine


class FakeWriter:
    def __init__(self):
        self.messages = []

    def is_closing(self):
        return False

    def write(self, data):
        self.messages.append(decode_message(data))

    async def drain(self):
        pass


def listener(stream=None, **options):
    logger = EventLogger(stream=stream or io.StringIO(), use_color=False)
    return ClientListener("S2", DeterministicStateMachine(("C1", "C2")), logger, **options)


def request(client_id, number):
    return build_client_request(client_id, "S2", number)


async def test_unready_replica_logs_requests_without_replying():
    stream = io.StringIO()
    replica = listener(stream, ready=False, strict_order=True)
    writer = FakeWriter()

    assert await replica._process(request("C1", 5), writer) is None
    assert await replica._process(request("C1", 6), writer) is None

    assert replica.high_watermark == {"C1": 6}
    assert replica._state_machine.snapshot() == {"C1": 0, "C2": 0}
    assert "i_am_ready = 0; logged <C1, S2, 6, request>" in stream.getvalue()
    assert "high_watermark_request_num[C1] = 6" in stream.getvalue()


async def test_checkpoint_install_replays_only_requests_it_does_not_cover():
    changes = []
    replica = listener(ready=False, strict_order=True, on_ready_change=changes.append)
    writer = FakeWriter()
    for number in (4, 5, 6):
        await replica._process(request("C1", number), writer)

    # The donor had applied C1's request 5 when it took the snapshot.
    assert await replica.install_checkpoint({"C1": 5, "C2": 2}, {"C1": 5, "C2": 2})

    assert replica.ready and changes == [True]
    assert replica._state_machine.snapshot() == {"C1": 6, "C2": 2}
    assert [(m["request_num"], m["payload"]["client_value"]) for m in writer.messages] == [(5, 5), (6, 6)]


async def test_checkpoint_older_than_the_log_is_rejected():
    replica = listener(ready=False, strict_order=True)
    await replica._process(request("C1", 9), FakeWriter())

    assert not await replica.install_checkpoint({"C1": 7, "C2": 0}, {"C1": 7, "C2": 0})

    assert not replica.ready
    assert replica._state_machine.snapshot() == {"C1": 0, "C2": 0}


async def test_retry_after_install_is_answered_from_rebuilt_duplicate_cache():
    replica = listener(ready=False, strict_order=True)
    await replica.install_checkpoint({"C1": 3, "C2": 0}, {"C1": 3, "C2": 0})

    reply = await replica._process(request("C1", 3), FakeWriter())

    assert reply["payload"]["client_value"] == 3
    assert replica._state_machine.snapshot() == {"C1": 3, "C2": 0}


@pytest.mark.parametrize("strict_order", [False, True])
async def test_late_request_covered_by_checkpoint_does_not_disconnect_active_client(strict_order):
    replica = listener(ready=False, strict_order=strict_order)
    await replica.install_checkpoint({"C1": 3, "C2": 0}, {"C1": 3, "C2": 0})

    reply = await replica._process(request("C1", 1), FakeWriter())

    if strict_order:
        assert reply is None
    else:
        assert reply["payload"]["code"] == "stale_request"
    assert replica._state_machine.snapshot() == {"C1": 3, "C2": 0}
    boundary = await replica._process(request("C1", 3), FakeWriter())
    assert boundary["payload"]["client_value"] == 3
    next_reply = await replica._process(request("C1", 4), FakeWriter())
    assert next_reply["payload"]["client_value"] == 4


async def test_late_request_with_different_operation_is_still_rejected():
    replica = listener(ready=False, strict_order=True)
    await replica.install_checkpoint({"C1": 3, "C2": 0}, {"C1": 3, "C2": 0})
    invalid = request("C1", 1)
    invalid["payload"]["operation"] = "decrement"

    reply = await replica._process(invalid, FakeWriter())

    assert reply["payload"]["code"] == "stale_request"
    assert replica._state_machine.snapshot() == {"C1": 3, "C2": 0}


async def test_ready_replica_that_misses_requests_asks_for_state_again():
    changes = []
    replica = listener(ready=True, strict_order=True, on_ready_change=changes.append)
    await replica._process(request("C1", 1), FakeWriter())

    assert await replica._process(request("C1", 4), FakeWriter()) is None

    assert not replica.ready and changes == [False]
    assert replica._state_machine.snapshot() == {"C1": 1, "C2": 0}


async def test_mark_ready_accepts_logged_requests_as_they_are():
    replica = listener(ready=False, strict_order=True)
    writer = FakeWriter()
    await replica._process(request("C2", 1), writer)

    assert await replica.mark_ready()
    assert not await replica.mark_ready()

    assert replica._state_machine.snapshot() == {"C1": 0, "C2": 1}
    assert writer.messages[0]["request_num"] == 1


async def test_passive_backup_refuses_requests_and_names_the_primary():
    replica = listener(role="backup")
    replica.primary_hint = "S1"

    reply = await replica._process(request("C1", 1), FakeWriter())

    assert reply["type"] == "error"
    assert reply["payload"] == {"code": "not_primary", "detail": "S1"}
    assert replica._state_machine.snapshot() == {"C1": 0, "C2": 0}
