import asyncio
import io

import pytest

from ft_system.common.config import ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    build_client_request,
    build_heartbeat,
)
from ft_system.common.transport import (
    MAX_MESSAGE_BYTES,
    close_writer,
    read_message,
    write_message,
)
from ft_system.server.app import ServerApp


FIXED_TIME = "2026-09-01 12:00:00.123"


def make_server_config(
    *, client_port: int = 0, heartbeat_port: int = 0
) -> ServerConfig:
    return ServerConfig(
        replica_id="S1",
        bind_host="127.0.0.1",
        advertised_host="127.0.0.1",
        client_port=client_port,
        heartbeat_port=heartbeat_port,
    )


def make_logger(stream: io.StringIO | None = None) -> EventLogger:
    return EventLogger(
        stream=stream or io.StringIO(),
        clock=lambda: FIXED_TIME,
        use_color=False,
    )


async def exchange(port: int, message: dict) -> dict:
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, limit=MAX_MESSAGE_BYTES + 1
    )
    try:
        await write_message(writer, message)
        return await asyncio.wait_for(read_message(reader), timeout=0.5)
    finally:
        await close_writer(writer)


async def send_heartbeat(port: int, count: int) -> dict:
    return await exchange(port, build_heartbeat("LFD1", "S1", count))


async def send_request(port: int, client_id: str, request_num: int) -> dict:
    return await exchange(
        port,
        build_client_request(client_id, "S1", request_num),
    )


@pytest.mark.asyncio
async def test_heartbeat_and_client_listeners_are_independent():
    stream = io.StringIO()
    app = ServerApp(
        make_server_config(),
        ("C1", "C2", "C3"),
        make_logger(stream),
    )
    await app.start()
    try:
        assert app.bound_client_port != app.bound_heartbeat_port
        heartbeat_before = app.state_machine.snapshot()

        ack = await send_heartbeat(app.bound_heartbeat_port, count=1)

        assert ack["type"] == "heartbeat_ack"
        assert ack["heartbeat_count"] == 1
        assert app.state_machine.snapshot() == heartbeat_before

        reply = await send_request(app.bound_client_port, "C1", request_num=1)

        assert reply["payload"]["client_value"] == 1
        assert app.state_machine.snapshot() == {"C1": 1, "C2": 0, "C3": 0}
        assert "[1] S1 receives heartbeat from LFD1" in stream.getvalue()
        assert "[1] S1 sending heartbeat ACK to LFD1" in stream.getvalue()
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_successful_request_prints_exact_rubric_log_group():
    stream = io.StringIO()
    app = ServerApp(
        make_server_config(),
        ("C1", "C2", "C3"),
        make_logger(stream),
    )
    await app.start()
    try:
        await send_request(app.bound_client_port, "C1", request_num=7)

        assert stream.getvalue().splitlines() == [
            "[2026-09-01 12:00:00.123] Received <C1, S1, 7, request>",
            '[2026-09-01 12:00:00.123] my_state_S1 = {"C1": 0, "C2": 0, "C3": 0} before processing <C1, S1, 7, request>',
            '[2026-09-01 12:00:00.123] my_state_S1 = {"C1": 1, "C2": 0, "C3": 0} after processing <C1, S1, 7, request>',
            "[2026-09-01 12:00:00.123] Sending <C1, S1, 7, reply>",
        ]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_malformed_client_connection_does_not_stop_listener():
    app = ServerApp(make_server_config(), ("C1", "C2", "C3"), make_logger())
    await app.start()
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", app.bound_client_port
        )
        writer.write(b"not-json\n")
        await writer.drain()

        assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
        await close_writer(writer)

        reply = await send_request(app.bound_client_port, "C2", request_num=1)
        assert reply["payload"]["client_value"] == 1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_unsupported_operation_returns_error_without_mutating_state():
    app = ServerApp(make_server_config(), ("C1", "C2", "C3"), make_logger())
    await app.start()
    try:
        request = build_client_request("C1", "S1", 1)
        request["payload"]["operation"] = "decrement"

        response = await exchange(app.bound_client_port, request)

        assert response["type"] == "error"
        assert response["request_id"] == "C1:1"
        assert response["payload"]["code"] == "unsupported_operation"
        assert app.state_machine.snapshot() == {"C1": 0, "C2": 0, "C3": 0}
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_concurrent_request_log_groups_do_not_interleave():
    stream = io.StringIO()
    app = ServerApp(
        make_server_config(),
        ("C1", "C2", "C3"),
        make_logger(stream),
    )
    await app.start()
    try:
        await asyncio.gather(
            send_request(app.bound_client_port, "C1", request_num=1),
            send_request(app.bound_client_port, "C2", request_num=1),
        )

        lines = stream.getvalue().splitlines()
        assert len(lines) == 8
        first_client = "C1" if "<C1," in lines[0] else "C2"
        second_client = "C2" if first_client == "C1" else "C1"
        assert all(f"<{first_client}," in line for line in lines[:4])
        assert all(f"<{second_client}," in line for line in lines[4:])
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_close_is_idempotent():
    app = ServerApp(make_server_config(), ("C1", "C2", "C3"), make_logger())
    await app.start()

    await app.close()
    await app.close()
