"""Minimal connection timeout and capped backoff helpers."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from ft_system.common.transport import MAX_MESSAGE_BYTES


@dataclass
class ExponentialBackoff:
    initial: float = 0.5
    maximum: float = 5.0
    attempt: int = 0

    def __post_init__(self) -> None:
        if self.initial <= 0 or self.maximum <= 0:
            raise ValueError("backoff values must be positive")
        if self.initial > self.maximum:
            raise ValueError("initial backoff must not exceed maximum")

    def next_delay(self) -> float:
        max_exponent = math.ceil(math.log2(self.maximum / self.initial))
        exponent = min(self.attempt, max_exponent)
        delay = min(self.initial * (2**exponent), self.maximum)
        self.attempt += 1
        return delay

    def reset(self) -> None:
        self.attempt = 0


async def open_connection_with_timeout(
    host: str, port: int, timeout: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open one bounded asyncio stream connection within a deadline."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    connection = asyncio.open_connection(
        host,
        port,
        limit=MAX_MESSAGE_BYTES + 1,
    )
    return await asyncio.wait_for(connection, timeout)
