import asyncio
import io
import socket

import pytest

from ft_system.common.config import LfdConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import build_heartbeat_ack
from ft_system.common.transport import close_writer, read_message, write_message
from ft_system.lfd.app import HeartbeatEndpoint, LfdApp
from ft_system.server.app import ServerApp
from tests.helpers import eventually


def make_logger(stream: io.StringIO | None = None) -> EventLogger:
    return EventLogger(
        stream=stream or io.StringIO(),
        clock=lambda: "2026-09-01 12:00:00.123",
        use_color=False,
    )


def make_lfd_config(freq: float = 0.02) -> LfdConfig:
    return LfdConfig(
        lfd_id="LFD1",
        server_id="S1",
        heartbeat_freq=freq,
        connect_timeout=0.05,
        read_timeout=0.05,
        initial_backoff=0.01,
        max_backoff=0.05,
    )


def make_server_config(heartbeat_port: int = 0) -> ServerConfig:
    return ServerConfig(
        replica_id="S1",
        bind_host="127.0.0.1",
        advertised_host="127.0.0.1",
        client_port=0,
        heartbeat_port=heartbeat_port,
    )


async def started_server(heartbeat_port: int = 0) -> ServerApp:
    server = ServerApp(
        make_server_config(heartbeat_port),
        ("C1", "C2", "C3"),
        make_logger(),
    )
    await server.start()
    return server


def endpoint_for_heartbeat(server: ServerApp) -> HeartbeatEndpoint:
    return HeartbeatEndpoint("S1", "127.0.0.1", server.bound_heartbeat_port)


def unused_loopback_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


@pytest.mark.asyncio
async def test_lfd_counts_and_logs_each_successful_heartbeat():
    server = await started_server()
    stream = io.StringIO()
    lfd = LfdApp(
        make_lfd_config(),
        endpoint_for_heartbeat(server),
        make_logger(stream),
    )
    task = asyncio.create_task(lfd.run())
    try:
        await eventually(lambda: lfd.heartbeat_count >= 4)

        assert lfd.healthy is True
        assert "[1] LFD1 sending heartbeat to S1" in stream.getvalue()
        assert "[1] LFD1 receives heartbeat from S1" in stream.getvalue()
        assert "[3] LFD1 sending heartbeat to S1" in stream.getvalue()
    finally:
        await lfd.stop()
        await task
        await asyncio.wait_for(server.close(), timeout=0.2)


@pytest.mark.asyncio
async def test_server_loss_is_reported_once_despite_reconnect_attempts():
    server = await started_server()
    stream = io.StringIO()
    lfd = LfdApp(
        make_lfd_config(), endpoint_for_heartbeat(server), make_logger(stream)
    )
    task = asyncio.create_task(lfd.run())
    try:
        await eventually(lambda: lfd.healthy)
        await asyncio.wait_for(server.close(), timeout=0.2)
        await eventually(lambda: not lfd.healthy)
        await asyncio.sleep(0.12)

        assert stream.getvalue().count("S1 has died") == 1
    finally:
        await lfd.stop()
        await task


@pytest.mark.asyncio
async def test_restarted_server_recovers_on_fresh_connection():
    server = await started_server()
    heartbeat_port = server.bound_heartbeat_port
    stream = io.StringIO()
    lfd = LfdApp(
        make_lfd_config(), endpoint_for_heartbeat(server), make_logger(stream)
    )
    task = asyncio.create_task(lfd.run())
    replacement = None
    try:
        await eventually(lambda: lfd.healthy)
        await server.close()
        await eventually(lambda: not lfd.healthy)

        replacement = await started_server(heartbeat_port)
        await eventually(lambda: lfd.healthy, timeout=1.0)

        assert stream.getvalue().count("S1 has died") == 1
        assert stream.getvalue().count("S1 has recovered") == 1
    finally:
        await lfd.stop()
        await task
        if replacement is not None:
            await replacement.close()


@pytest.mark.asyncio
async def test_wrong_ack_count_marks_previously_healthy_server_failed():
    sent_ack_count = 0

    async def handle(reader, writer):
        nonlocal sent_ack_count
        try:
            while True:
                heartbeat = await read_message(reader)
                sent_ack_count += 1
                ack = build_heartbeat_ack(heartbeat)
                if sent_ack_count >= 2:
                    ack["heartbeat_count"] += 100
                await write_message(writer, ack)
        except (EOFError, ConnectionError):
            pass
        finally:
            await close_writer(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = HeartbeatEndpoint("S1", "127.0.0.1", server.sockets[0].getsockname()[1])
    stream = io.StringIO()
    lfd = LfdApp(make_lfd_config(), endpoint, make_logger(stream))
    task = asyncio.create_task(lfd.run())
    try:
        await eventually(lambda: lfd.healthy)
        await eventually(lambda: not lfd.healthy)

        assert "S1 has died" in stream.getvalue()
    finally:
        await lfd.stop()
        await task
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_missing_ack_logs_failed_heartbeat_as_timeout():
    heartbeat_count = 0
    release_handler = asyncio.Event()

    async def handle(reader, writer):
        nonlocal heartbeat_count
        try:
            while True:
                heartbeat = await read_message(reader)
                heartbeat_count += 1
                if heartbeat_count == 1:
                    await write_message(writer, build_heartbeat_ack(heartbeat))
                else:
                    await release_handler.wait()
        except (EOFError, ConnectionError):
            pass
        finally:
            await close_writer(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = HeartbeatEndpoint("S1", "127.0.0.1", server.sockets[0].getsockname()[1])
    stream = io.StringIO()
    lfd = LfdApp(make_lfd_config(), endpoint, make_logger(stream))
    task = asyncio.create_task(lfd.run())
    try:
        await eventually(lambda: lfd.healthy)
        await eventually(lambda: not lfd.healthy)

        assert "[2] LFD1 heartbeat to S1 failed: timeout" in stream.getvalue()
        assert stream.getvalue().count("S1 has died") == 1
    finally:
        await lfd.stop()
        await task
        release_handler.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_starting_before_server_does_not_report_a_server_death():
    port = unused_loopback_port()
    endpoint = HeartbeatEndpoint("S1", "127.0.0.1", port)
    stream = io.StringIO()
    lfd = LfdApp(make_lfd_config(), endpoint, make_logger(stream))
    task = asyncio.create_task(lfd.run())
    server = None
    try:
        await asyncio.sleep(0.08)
        assert lfd.healthy is False
        assert "S1 has died" not in stream.getvalue()

        server = await started_server(port)
        await eventually(lambda: lfd.healthy, timeout=1.0)

        assert "S1 has recovered" not in stream.getvalue()
    finally:
        await lfd.stop()
        await task
        if server is not None:
            await server.close()


@pytest.mark.asyncio
async def test_stop_interrupts_long_heartbeat_wait():
    port = unused_loopback_port()
    config = make_lfd_config(freq=30.0)
    lfd = LfdApp(
        config,
        HeartbeatEndpoint("S1", "127.0.0.1", port),
        make_logger(),
    )
    task = asyncio.create_task(lfd.run())
    await asyncio.sleep(0.02)

    await lfd.stop()
    await asyncio.wait_for(task, timeout=0.2)
