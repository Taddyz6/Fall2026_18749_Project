import asyncio
import io

import pytest

from ft_system.common.config import RecoveryConfig, RmConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import build_hello, build_launch_result, build_membership_report
from ft_system.common.transport import close_writer, read_message_with_timeout, write_message
from ft_system.rm.app import RmApp
from tests.helpers import eventually


async def connect(rm, name, kind):
    reader, writer = await asyncio.open_connection("127.0.0.1", rm.bound_port)
    await write_message(writer, build_hello(name, kind))
    return reader, writer


def manager(enabled=True):
    return RmApp(RmConfig("127.0.0.1", "127.0.0.1", 0, .2), "active", ["S1"], ["C1"],
                 EventLogger(stream=io.StringIO(), use_color=False),
                 recovery=RecoveryConfig(enabled, .05, .08), lfd_servers={"LFD1": "S1"})


async def test_recovery_waits_for_gfd_retries_failure_and_stops_on_membership():
    rm = manager()
    await rm.start()
    writers = []
    try:
        lfd, writer = await connect(rm, "LFD1", "lfd")
        writers.append(writer)
        with pytest.raises(TimeoutError):
            await read_message_with_timeout(lfd, .1)
        _, gfd = await connect(rm, "GFD", "gfd")
        writers.append(gfd)
        started = asyncio.get_running_loop().time()
        await write_message(gfd, build_membership_report([]))
        command = await read_message_with_timeout(lfd, 1)
        assert command["payload"] == {"command": "ensure_replica", "target": "S1"}
        assert asyncio.get_running_loop().time() - started >= .05
        await write_message(writer, build_launch_result("LFD1", "S1", "failed", None, "permission denied"))
        retried = await read_message_with_timeout(lfd, 1)
        assert retried == command
        assert asyncio.get_running_loop().time() - started >= .13
        await write_message(writer, build_launch_result("LFD1", "S1", "started", 123, "spawned"))
        assert rm.membership == [] and not rm.policy.ready
        await write_message(gfd, build_membership_report(["S1"]))
        await eventually(lambda: rm.membership == ["S1"])
        with pytest.raises(TimeoutError):
            await read_message_with_timeout(lfd, .2)
        assert not rm.policy.ready  # Process existence/membership does not imply state readiness.
    finally:
        await asyncio.gather(*(close_writer(w) for w in writers))
        await rm.close()


async def test_missing_replica_recovers_after_lfd_reconnect_and_gfd_loss_pauses_commands():
    rm = manager()
    await rm.start()
    writers = []
    try:
        _, gfd = await connect(rm, "GFD", "gfd")
        writers.append(gfd)
        await write_message(gfd, build_membership_report([]))
        lfd, writer = await connect(rm, "LFD1", "lfd")
        writers.append(writer)
        await read_message_with_timeout(lfd, 1)
        await close_writer(writer)
        await eventually(lambda: not rm._lfds)
        lfd, writer = await connect(rm, "LFD1", "lfd")
        writers.append(writer)
        assert (await read_message_with_timeout(lfd, 1))["payload"]["target"] == "S1"
        await close_writer(gfd)
        await eventually(lambda: rm._gfd is None)
        with pytest.raises(TimeoutError):
            await read_message_with_timeout(lfd, .2)
    finally:
        await asyncio.gather(*(close_writer(w) for w in writers))
        await rm.close()


@pytest.mark.parametrize("enabled,name", [(False, "LFD1"), (True, "LFD9")])
async def test_recovery_rejects_disabled_or_unknown_lfd(enabled, name):
    rm = manager(enabled)
    await rm.start()
    writer = None
    try:
        reader, writer = await connect(rm, name, "lfd")
        with pytest.raises(EOFError):
            await read_message_with_timeout(reader, 1)
        assert not rm._lfds
    finally:
        await close_writer(writer)
        await rm.close()


async def test_duplicate_lfd_does_not_replace_live_owner():
    rm = manager()
    await rm.start()
    writers = []
    try:
        _, owner = await connect(rm, "LFD1", "lfd")
        writers.append(owner)
        await eventually(lambda: "LFD1" in rm._lfds)
        peer = rm._lfds["LFD1"]
        reader, duplicate = await connect(rm, "LFD1", "lfd")
        writers.append(duplicate)
        with pytest.raises(EOFError):
            await read_message_with_timeout(reader, 1)
        assert rm._lfds["LFD1"] is peer
    finally:
        await asyncio.gather(*(close_writer(w) for w in writers))
        await rm.close()


@pytest.mark.parametrize("source,replica", [("LFD2", "S1"), ("LFD1", "S2")])
async def test_launch_result_must_match_registered_lfd(source, replica):
    rm = manager()
    await rm.start()
    writer = None
    try:
        reader, writer = await connect(rm, "LFD1", "lfd")
        await write_message(writer, build_launch_result(source, replica, "started", 123, "spawned"))
        with pytest.raises(EOFError):
            await read_message_with_timeout(reader, 1)
        assert rm.membership == []
    finally:
        await close_writer(writer)
        await rm.close()
