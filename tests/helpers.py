import asyncio
import socket
from collections.abc import Callable


def free_ports(count: int) -> list[int]:
    """Reserve and release loopback ports so a relaunched component can reuse them."""

    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


async def eventually(
    predicate: Callable[[], bool],
    timeout: float = 1.0,
    interval: float = 0.01,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(interval)
