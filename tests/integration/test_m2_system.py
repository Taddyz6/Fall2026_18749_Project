import asyncio
import io
from dataclasses import replace
from itertools import permutations

import pytest

from ft_system.client.active import ActiveClientApp
from ft_system.client.app import ServerEndpoint
from ft_system.common.config import GfdConfig, LfdConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import build_client_reply, build_client_request, build_membership
from ft_system.common.transport import close_writer, read_message, write_message
from ft_system.gfd.app import GfdApp
from ft_system.lfd.app import HeartbeatEndpoint, LfdApp
from ft_system.lfd.reporter import GfdReporter
from ft_system.server.app import ServerApp
from tests.helpers import eventually


def logger(stream=None):
    return EventLogger(stream=stream if stream is not None else io.StringIO(), use_color=False)


async def make_servers():
    servers = [ServerApp(ServerConfig(f"S{i}", "127.0.0.1", "127.0.0.1", 0, 0),
                         ("C1", "C2", "C3"), logger()) for i in range(1, 4)]
    for server in servers:
        await server.start()
    return servers


def endpoints(servers):
    return [ServerEndpoint(s.config.replica_id, "127.0.0.1", s.bound_client_port) for s in servers]


@pytest.mark.parametrize("failure_order", list(permutations(range(3))))
async def test_concurrent_clients_consistency_duplicates_and_two_crashes(failure_order):
    servers = await make_servers()
    first, second, survivor = failure_order
    streams = [io.StringIO() for _ in range(3)]
    clients = [ActiveClientApp(f"C{i+1}", endpoints(servers), .2, .5, logger(streams[i])) for i in range(3)]
    try:
        for n in range(1, 11):
            replies = await asyncio.gather(*(c.send_increment() for c in clients))
            assert all(r["payload"]["client_value"] == n for r in replies)
        expected = {"C1": 10, "C2": 10, "C3": 10}
        await eventually(lambda: all(s.state_machine.snapshot() == expected for s in servers))
        await eventually(lambda: all(x.getvalue().count("Discarded duplicate reply") == 20 for x in streams))
        await servers[first].close()
        for n in range(11, 16):
            replies = await asyncio.gather(*(c.send_increment() for c in clients))
            assert all(r["payload"]["client_value"] == n for r in replies)
        await eventually(lambda: servers[second].state_machine.snapshot() == {k: 15 for k in expected})
        await servers[second].close()
        for n in range(16, 21):
            replies = await asyncio.gather(*(c.send_increment() for c in clients))
            assert all(r["replica_id"] == f"S{survivor + 1}" and r["payload"]["client_value"] == n for r in replies)
        assert servers[survivor].state_machine.snapshot() == {k: 20 for k in expected}
        assert all(x.getvalue().count("Delivered reply") == 20 for x in streams)
        await servers[survivor].close()
        await eventually(lambda: all(not c._connections for c in clients))
        with pytest.raises(ConnectionError, match="no replicas"):
            await clients[0].send_increment()
    finally:
        await asyncio.gather(*(c.close() for c in clients))
        await asyncio.gather(*(s.close() for s in servers))


async def test_gfd_zero_members_registration_health_and_lfd_disconnect():
    stream = io.StringIO()
    gfd = GfdApp(GfdConfig("127.0.0.1", "127.0.0.1", 0, .02, .1), {"LFD1": "S1"}, logger(stream))
    await gfd.start()
    reporter = GfdReporter("LFD1", "S1", replace(gfd.config, port=gfd.bound_port), logger())
    report_task = asyncio.create_task(reporter.run())
    servers = await make_servers()
    lfd = LfdApp(LfdConfig("LFD1", "S1", .02, .1, .1, .01, .05),
                 HeartbeatEndpoint("S1", "127.0.0.1", servers[0].bound_heartbeat_port),
                 logger(), reporter.set_health)
    lfd_task = None
    try:
        await eventually(lambda: "LFD1" in gfd._sessions)
        assert gfd.member_count == 0
        assert "GFD: 0 members" in stream.getvalue()
        lfd_task = asyncio.create_task(lfd.run())
        await eventually(lambda: gfd.membership == ["S1"])
        await servers[0].close()
        await eventually(lambda: gfd.member_count == 0)
        assert stream.getvalue().count("delete replica S1") == 1
        reporter.set_health(True)
        await eventually(lambda: gfd.member_count == 1)
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)
        await eventually(lambda: gfd.member_count == 0)
    finally:
        await lfd.stop()
        if lfd_task:
            await lfd_task
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)
        await gfd.close()
        await asyncio.gather(*(s.close() for s in servers))


async def test_gfd_times_out_registered_lfd_without_ack():
    gfd = GfdApp(GfdConfig("127.0.0.1", "127.0.0.1", 0, .02, .05), {"LFD1": "S1"}, logger())
    await gfd.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", gfd.bound_port)
    try:
        await write_message(writer, build_membership("LFD1", "S1", True, register=True))
        assert (await read_message(reader))["type"] == "heartbeat"
        assert gfd.membership == ["S1"]
        await eventually(lambda: gfd.member_count == 0)
    finally:
        await close_writer(writer)
        await gfd.close()


async def test_delayed_duplicates_do_not_block_the_next_request():
    servers = await make_servers()
    slow_tasks = set()

    async def slow(reader, writer):
        task = asyncio.current_task()
        slow_tasks.add(task)
        try:
            while True:
                request = await read_message(reader)
                await asyncio.sleep(.08)
                await write_message(writer, build_client_reply(request, request["request_num"] + 100))
        except (EOFError, ConnectionError):
            pass
        finally:
            await close_writer(writer)
            slow_tasks.discard(task)

    slow_server = await asyncio.start_server(slow, "127.0.0.1", 0)
    stream = io.StringIO()
    eps = endpoints(servers)[:1] + [ServerEndpoint("S2", "127.0.0.1", slow_server.sockets[0].getsockname()[1])]
    client = ActiveClientApp("C1", eps, .2, .5, logger(stream))
    try:
        for n in range(1, 4):
            reply = await asyncio.wait_for(client.send_increment(), .07)
            assert reply["request_num"] == n
        await eventually(lambda: stream.getvalue().count("Discarded duplicate reply from S2") == 3)
        assert stream.getvalue().count("Delivered reply") == 3
        assert stream.getvalue().count("replica state divergence") == 3
    finally:
        await client.close()
        slow_server.close()
        await slow_server.wait_closed()
        await asyncio.gather(*list(slow_tasks), return_exceptions=True)
        await asyncio.gather(*(s.close() for s in servers))


async def test_retransmission_after_lost_reply_does_not_increment_twice():
    servers = await make_servers()
    server = servers[0]
    request = build_client_request("C1", "S1", 1)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_client_port)
        await write_message(writer, request)
        await eventually(lambda: server.state_machine.snapshot()["C1"] == 1)
        await close_writer(writer)  # Application never consumes the first reply.
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_client_port)
        await write_message(writer, request)
        assert (await read_message(reader))["payload"]["client_value"] == 1
        assert server.state_machine.snapshot()["C1"] == 1
        await write_message(writer, build_client_request("C1", "S1", 2))
        assert (await read_message(reader))["payload"]["client_value"] == 2
        await close_writer(writer)
    finally:
        await asyncio.gather(*(s.close() for s in servers))


async def test_reply_arriving_after_timeout_is_delivered_only_once():
    handlers = set()

    async def delayed(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            request = await read_message(reader)
            await asyncio.sleep(.06)
            await write_message(writer, build_client_reply(request, 1))
            await reader.read()
        finally:
            await close_writer(writer)
            handlers.discard(task)

    server = await asyncio.start_server(delayed, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    stream = io.StringIO()
    client = ActiveClientApp("C1", [ServerEndpoint("S1", "127.0.0.1", port)], .2, .02, logger(stream))
    try:
        with pytest.raises(TimeoutError):
            await client.send_increment()
        await eventually(lambda: client._delivered == 1)
        reply = await client.send_increment()
        assert reply["request_num"] == 1
        assert client.next_request_num == 2
        assert stream.getvalue().count("Delivered reply") == 1
        assert stream.getvalue().count("Sending <") == 1
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*list(handlers), return_exceptions=True)


@pytest.mark.parametrize("survivor", range(3))
async def test_two_simultaneous_replica_failures(survivor):
    servers = await make_servers()
    clients = [ActiveClientApp(f"C{i}", endpoints(servers), .2, .5, logger())
               for i in range(1, 4)]
    try:
        await asyncio.gather(*(client.send_increment() for client in clients))
        await eventually(lambda: all(server.state_machine.snapshot() ==
                                    {"C1": 1, "C2": 1, "C3": 1} for server in servers))
        await asyncio.gather(*(server.close() for i, server in enumerate(servers)
                               if i != survivor))
        for n in range(2, 7):
            replies = await asyncio.gather(*(client.send_increment() for client in clients))
            assert all(reply["replica_id"] == f"S{survivor + 1}" and
                       reply["payload"]["client_value"] == n for reply in replies)
        assert servers[survivor].state_machine.snapshot() == {"C1": 6, "C2": 6, "C3": 6}
    finally:
        await asyncio.gather(*(client.close() for client in clients))
        await asyncio.gather(*(server.close() for server in servers))
