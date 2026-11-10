"""Adversarial recovery-ordering and evidence checks from the pre-push review."""

import asyncio
import io

import pytest

import ft_system.rm.app as rm_module
from ft_system.common.config import ReplicationConfig, RmConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    MessageType, ProtocolError, build_checkpoint, build_hello, build_membership_report,
    build_replica_status, build_rm_command, build_view,
)
from ft_system.common.transport import close_writer, write_message
from ft_system.rm.app import RmApp
from ft_system.server.app import ServerApp
from scripts.m5_smoke import Run
from tests.helpers import eventually


async def connect(rm, name, kind):
    reader, writer = await asyncio.open_connection("127.0.0.1", rm.bound_port)
    await write_message(writer, build_hello(name, kind))
    return reader, writer


async def test_old_connection_cannot_mark_replacement_ready(monkeypatch):
    logger = EventLogger(stream=io.StringIO(), use_color=False)
    rm = RmApp(RmConfig("127.0.0.1", "127.0.0.1", 0, .2, transfer_delay=.2),
               "active", ["S1", "S2"], ["C1"], logger)
    await rm.start()
    blocked, release = asyncio.Event(), asyncio.Event()
    original_read = rm_module.read_message
    delay_old_status = False
    writers = []

    async def delayed_read(reader):
        message = await original_read(reader)
        if (delay_old_status and message["type"] == MessageType.REPLICA_STATUS
                and message["source"] == "S2" and message["payload"]["ready"]):
            blocked.set()
            await release.wait()
        return message

    monkeypatch.setattr(rm_module, "read_message", delayed_read)
    try:
        _, gfd = await connect(rm, "GFD", "gfd")
        writers.append(gfd)
        for replica in ("S1", "S2"):
            _, writer = await connect(rm, replica, "server")
            writers.append(writer)
            await write_message(writer, build_replica_status(replica, True, 1))
        old = writers[-1]
        await eventually(lambda: rm._reported == {"S1": True, "S2": True})
        await write_message(gfd, build_membership_report(["S1", "S2"]))
        await eventually(lambda: rm.policy.ready == {"S1", "S2"})

        # Hold an already-read old-session frame until after replacement. This
        # models a buffered TCP frame, not a new message from the new replica.
        delay_old_status = True
        await write_message(old, build_replica_status("S2", True, 1))
        await asyncio.wait_for(blocked.wait(), 1)
        await write_message(gfd, build_membership_report(["S1"]))
        await eventually(lambda: rm.membership == ["S1"])
        _, replacement = await connect(rm, "S2", "server")
        writers.append(replacement)
        await write_message(replacement, build_replica_status("S2", False, 0))
        await eventually(lambda: rm._reported.get("S2") is False)
        await write_message(gfd, build_membership_report(["S1", "S2"]))
        await eventually(lambda: rm.policy.assignments() == {"S2": "S1"})

        release.set()
        await eventually(lambda: "stale server session" in logger._stream.getvalue()
                         or rm._reported.get("S2") is True)
        assert rm._reported["S2"] is False
        assert rm.policy.ready == {"S1"}
        assert rm.policy.assignments() == {"S2": "S1"}

        delay_old_status = False
        await write_message(replacement, build_replica_status("S2", True, 2))
        await eventually(lambda: rm.policy.ready == {"S1", "S2"})
    finally:
        release.set()
        await asyncio.gather(*(close_writer(w) for w in writers))
        await rm.close()


async def test_server_registration_clears_old_readiness_before_gfd_rejoin():
    rm = RmApp(RmConfig("127.0.0.1", "127.0.0.1", 0, .2), "active", ["S1"], ["C1"],
               EventLogger(stream=io.StringIO(), use_color=False))
    await rm.start()
    writers = []
    try:
        _, old = await connect(rm, "S1", "server")
        writers.append(old)
        await write_message(old, build_replica_status("S1", True, 1))
        await eventually(lambda: rm._reported.get("S1") is True)
        peer = rm._servers["S1"]
        _, replacement = await connect(rm, "S1", "server")
        writers.append(replacement)
        await eventually(lambda: rm._servers["S1"] is not peer)
        assert not rm._reported.get("S1", False)
        # GFD may report the newborn before its first status message arrives.
        await rm._on_membership(["S1"])
        assert not rm.policy.ready
    finally:
        await asyncio.gather(*(close_writer(w) for w in writers))
        await rm.close()


@pytest.mark.parametrize("message", [
    build_rm_command("S2", "ensure_replica", "S1"),
    build_rm_command("S1", "set_ready"),
    build_view("S1", {"view_num": 1, "mode": "active", "members": ["S2"], "ready": [], "primary": None}),
])
async def test_server_rejects_lfd_only_or_misaddressed_control_messages(message):
    app = ServerApp(ServerConfig("S2", "127.0.0.1", "127.0.0.1", 0, 0), ["C1"],
                    EventLogger(stream=io.StringIO(), use_color=False))
    try:
        with pytest.raises(ProtocolError):
            await app._on_rm_message(message)
    finally:
        await app.close()


@pytest.mark.parametrize("mode", ["active", "passive"])
async def test_smoke_state_uses_installed_not_ignored_checkpoints(mode):
    stream = io.StringIO()
    app = ServerApp(ServerConfig("S2", "127.0.0.1", "127.0.0.1", 0, 0, 0), ["C1"],
                    EventLogger(stream=stream, use_color=False), ReplicationConfig(mode, .5),
                    rm=RmConfig("127.0.0.1", "127.0.0.1", 1))
    run = Run.__new__(Run)
    run.log = lambda _: stream.getvalue()
    try:
        await app._receive_checkpoint(build_checkpoint("S1", "S2", {"C1": 5}, {"C1": 5}, 1))
        assert app.state_machine.snapshot() == {"C1": 5}
        assert run.state("S2") == {"C1": 5}
        if mode == "passive":
            app._apply_view({"view_num": 2, "mode": mode, "members": ["S2"], "ready": ["S2"], "primary": "S2"})
        await app._receive_checkpoint(build_checkpoint("S1", "S2", {"C1": 99}, {"C1": 99}, 2))
        assert app.state_machine.snapshot() == {"C1": 5}
        assert run.state("S2") == {"C1": 5}
    finally:
        await app.close()
