import asyncio
import io

import pytest

from ft_system.client.app import ClientApp, ServerEndpoint
from ft_system.common.config import LfdConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.lfd.app import HeartbeatEndpoint, LfdApp
from ft_system.server.app import ServerApp
from tests.helpers import eventually


def quiet_logger(stream: io.StringIO | None = None) -> EventLogger:
    return EventLogger(stream=stream or io.StringIO(), use_color=False)


@pytest.mark.asyncio
async def test_three_clients_continue_until_server_fault_is_detected():
    server = ServerApp(
        ServerConfig("S1", "127.0.0.1", "127.0.0.1", 0, 0),
        ("C1", "C2", "C3"),
        quiet_logger(),
    )
    await server.start()

    lfd_stream = io.StringIO()
    lfd = LfdApp(
        LfdConfig("LFD1", "S1", 0.02, 0.05, 0.05, 0.01, 0.05),
        HeartbeatEndpoint("S1", "127.0.0.1", server.bound_heartbeat_port),
        quiet_logger(lfd_stream),
    )
    lfd_task = asyncio.create_task(lfd.run())
    clients = [
        ClientApp(
            client_id,
            ServerEndpoint("S1", "127.0.0.1", server.bound_client_port),
            0.1,
            0.1,
            quiet_logger(),
        )
        for client_id in ("C1", "C2", "C3")
    ]

    async def send_two(client: ClientApp) -> None:
        await client.send_increment()
        await client.send_increment()

    try:
        await eventually(lambda: lfd.healthy)
        await asyncio.gather(*(send_two(client) for client in clients))

        assert server.state_machine.snapshot() == {"C1": 2, "C2": 2, "C3": 2}
        assert all(client.next_request_num == 3 for client in clients)
        assert lfd.healthy is True

        await asyncio.wait_for(server.close(), timeout=0.3)
        await eventually(lambda: not lfd.healthy, timeout=0.5)

        assert lfd_stream.getvalue().count("S1 has died") == 1
    finally:
        await asyncio.gather(*(client.close() for client in clients))
        await lfd.stop()
        await lfd_task
        await server.close()
