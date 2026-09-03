"""Automated Milestone 1 smoke check using public application APIs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from ft_system.client.app import ClientApp, ServerEndpoint
from ft_system.common.config import load_config
from ft_system.common.logging import EventLogger
from ft_system.lfd.app import HeartbeatEndpoint, LfdApp
from ft_system.server.app import ServerApp


PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def wait_until(predicate, timeout: float, description: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {description}")
        await asyncio.sleep(0.01)


async def run_smoke() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "local.toml")
    configured_server = config.server("S1")
    server_config = replace(configured_server, client_port=0, heartbeat_port=0)
    server = ServerApp(server_config, tuple(config.clients), EventLogger())
    await server.start()

    configured_lfd = config.lfd("LFD1")
    lfd_config = replace(
        configured_lfd,
        heartbeat_freq=0.05,
        connect_timeout=0.2,
        read_timeout=0.2,
        initial_backoff=0.02,
        max_backoff=0.1,
    )
    lfd = LfdApp(
        lfd_config,
        HeartbeatEndpoint("S1", "127.0.0.1", server.bound_heartbeat_port),
        EventLogger(),
    )
    lfd_task = asyncio.create_task(lfd.run())
    clients = [
        ClientApp(
            client_id,
            ServerEndpoint("S1", "127.0.0.1", server.bound_client_port),
            config.client(client_id).connect_timeout,
            config.client(client_id).read_timeout,
            EventLogger(),
        )
        for client_id in ("C1", "C2", "C3")
    ]

    try:
        await wait_until(lambda: lfd.healthy, 1.0, "healthy heartbeat")
        await asyncio.gather(*(client.send_increment() for client in clients))

        expected_state = {"C1": 1, "C2": 1, "C3": 1}
        actual_state = server.state_machine.snapshot()
        if actual_state != expected_state:
            raise AssertionError(f"unexpected state: {actual_state}")
        print(f"SMOKE state verified: {actual_state}", flush=True)

        await asyncio.wait_for(server.close(), timeout=1.0)
        await wait_until(lambda: not lfd.healthy, 1.0, "S1 failure detection")
        print("SMOKE failure detection verified", flush=True)
    finally:
        await asyncio.gather(*(client.close() for client in clients))
        await lfd.stop()
        await lfd_task
        await server.close()


def main() -> int:
    asyncio.run(run_smoke())
    print("SMOKE PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
