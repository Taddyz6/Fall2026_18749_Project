"""One registered, self-reconnecting connection from a component to the RM."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ft_system.common.config import RmConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import ProtocolError, build_hello
from ft_system.common.retry import open_connection_with_timeout
from ft_system.common.transport import close_writer, read_message, write_message


RECONNECT_DELAY = 0.5


class RmLink:
    """Register with the RM, deliver its messages, and reconnect after failures.

    The RM is an accepted single point of failure, so callers treat a missing
    connection as "not yet informed" rather than as an error.
    """

    def __init__(
        self,
        component_id: str,
        kind: str,
        config: RmConfig,
        logger: EventLogger,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]] | None = None,
        identity: Mapping[str, str] | None = None,
    ) -> None:
        self.component_id = component_id
        self.kind = kind
        self.config = config
        self.logger = logger
        self.on_message = on_message
        self.on_connected = on_connected
        self.identity = identity
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None

    def status(self) -> str:
        if self.connected:
            return "RM connected"
        return f"RM NOT connected (dialing {self.config.advertised_host}:{self.config.port})"

    async def send(self, message: dict[str, Any]) -> bool:
        """Send one message if registered; the read loop handles broken connections."""

        async with self._lock:
            if self._writer is None:
                return False
            try:
                await asyncio.wait_for(write_message(self._writer, message), self.config.connect_timeout)
            except (TimeoutError, ConnectionError, OSError):
                return False
            return True

    async def run(self) -> None:
        reported_failure = False
        while True:
            writer = None
            try:
                reader, writer = await open_connection_with_timeout(
                    self.config.advertised_host, self.config.port, self.config.connect_timeout)
                async with self._lock:
                    await write_message(writer, build_hello(self.component_id, self.kind, self.identity))
                    self._writer = writer
                self.logger.event(f"{self.component_id}: registered with RM")
                reported_failure = False
                if self.on_connected is not None:
                    await self.on_connected()
                while True:
                    await self.on_message(await read_message(reader))
            except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError) as exc:
                # Log the first failure of each outage instead of every retry.
                if not reported_failure:
                    self.logger.error(self.component_id, f"RM connection: {str(exc) or type(exc).__name__}")
                    reported_failure = True
            finally:
                async with self._lock:
                    self._writer = None
                await close_writer(writer)
            await asyncio.sleep(RECONNECT_DELAY)
