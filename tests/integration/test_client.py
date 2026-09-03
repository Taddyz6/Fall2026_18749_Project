import asyncio
import io

import pytest

from ft_system.client.app import ClientApp, ServerEndpoint
from ft_system.common.config import ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    ProtocolError,
    build_client_reply,
    build_client_request,
)
from ft_system.common.transport import (
    MAX_MESSAGE_BYTES,
    close_writer,
    read_message,
    write_message,
)
from ft_system.server.app import ServerApp


def make_logger(stream: io.StringIO | None = None) -> EventLogger:
    return EventLogger(
        stream=stream or io.StringIO(),
        clock=lambda: "2026-09-01 12:00:00.123",
        use_color=False,
    )


async def started_server() -> ServerApp:
    config = ServerConfig(
        replica_id="S1",
        bind_host="127.0.0.1",
        advertised_host="127.0.0.1",
        client_port=0,
        heartbeat_port=0,
    )
    server = ServerApp(config, ("C1", "C2", "C3"), make_logger())
    await server.start()
    return server


def endpoint_for(server: ServerApp) -> ServerEndpoint:
    return ServerEndpoint("S1", "127.0.0.1", server.bound_client_port)


@pytest.mark.asyncio
async def test_client_increments_request_num_after_valid_reply():
    server = await started_server()
    client = ClientApp("C1", endpoint_for(server), 0.2, 0.2, make_logger())
    try:
        first = await client.send_increment()
        second = await client.send_increment()

        assert first["request_num"] == 1
        assert second["request_num"] == 2
        assert first["request_id"] == "C1:1"
        assert second["request_id"] == "C1:2"
        assert client.next_request_num == 3
        assert server.state_machine.snapshot()["C1"] == 2
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_client_logs_sent_and_received_rubric_tuples():
    server = await started_server()
    stream = io.StringIO()
    client = ClientApp("C1", endpoint_for(server), 0.2, 0.2, make_logger(stream))
    try:
        await client.send_increment()

        assert stream.getvalue().splitlines() == [
            "[2026-09-01 12:00:00.123] Sending <C1, S1, 1, request>",
            "[2026-09-01 12:00:00.123] Received <C1, S1, 1, reply>",
        ]
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_multiple_requests_reuse_one_tcp_connection():
    connection_count = 0
    request_count = 0

    async def handle(reader, writer):
        nonlocal connection_count, request_count
        connection_count += 1
        try:
            for value in (1, 2):
                request = await read_message(reader)
                request_count += 1
                await write_message(writer, build_client_reply(request, value))
        finally:
            await close_writer(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = ServerEndpoint("S1", "127.0.0.1", server.sockets[0].getsockname()[1])
    client = ClientApp("C1", endpoint, 0.2, 0.2, make_logger())
    try:
        await client.send_increment()
        await client.send_increment()

        assert connection_count == 1
        assert request_count == 2
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_mismatched_reply_does_not_advance_request_num():
    async def handle(reader, writer):
        try:
            await read_message(reader)
            wrong_request = build_client_request("C1", "S1", 99)
            await write_message(writer, build_client_reply(wrong_request, 1))
        finally:
            await close_writer(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = ServerEndpoint("S1", "127.0.0.1", server.sockets[0].getsockname()[1])
    client = ClientApp("C1", endpoint, 0.2, 0.2, make_logger())
    try:
        with pytest.raises(ProtocolError, match="reply identifiers"):
            await client.send_increment()

        assert client.next_request_num == 1
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_timeout_preserves_request_num_and_next_command_reconnects():
    connection_count = 0
    seen_request_nums = []

    async def handle(reader, writer):
        nonlocal connection_count
        connection_count += 1
        this_connection = connection_count
        try:
            request = await read_message(reader)
            seen_request_nums.append(request["request_num"])
            if this_connection == 1:
                await asyncio.sleep(0.1)
                return
            await write_message(writer, build_client_reply(request, 1))
        finally:
            await close_writer(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = ServerEndpoint("S1", "127.0.0.1", server.sockets[0].getsockname()[1])
    client = ClientApp("C1", endpoint, 0.2, 0.02, make_logger())
    try:
        with pytest.raises(TimeoutError):
            await client.send_increment()
        assert client.next_request_num == 1

        reply = await client.send_increment()

        assert reply["request_num"] == 1
        assert seen_request_nums == [1, 1]
        assert connection_count == 2
        assert client.next_request_num == 2
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_close_is_idempotent_before_and_after_connection():
    server = await started_server()
    client = ClientApp("C1", endpoint_for(server), 0.2, 0.2, make_logger())
    try:
        await client.close()
        await client.send_increment()
        await client.close()
        await client.close()
    finally:
        await server.close()
