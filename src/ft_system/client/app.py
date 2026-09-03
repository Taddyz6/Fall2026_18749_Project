"""Persistent Milestone 1 client behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    MessageType,
    ProtocolError,
    build_client_request,
)
from ft_system.common.retry import open_connection_with_timeout
from ft_system.common.transport import (
    close_writer,
    read_message_with_timeout,
    write_message,
)


@dataclass(frozen=True)
class ServerEndpoint:
    replica_id: str
    host: str
    port: int


class ClientApp:
    """Send sequential logical requests over one reconnectable TCP stream."""

    def __init__(
        self,
        client_id: str,
        endpoint: ServerEndpoint,
        connect_timeout: float,
        read_timeout: float,
        logger: EventLogger,
    ) -> None:
        self.client_id = client_id
        self.endpoint = endpoint
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.logger = logger
        self._next_request_num = 1
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def next_request_num(self) -> int:
        return self._next_request_num

    async def connect(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        await self._drop_connection()
        self._reader, self._writer = await open_connection_with_timeout(
            self.endpoint.host,
            self.endpoint.port,
            self.connect_timeout,
        )

    async def _drop_connection(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        await close_writer(writer)

    @staticmethod
    def _validate_reply(reply: dict[str, Any], request: dict[str, Any]) -> None:
        if reply["type"] == MessageType.ERROR:
            detail = reply["payload"]["detail"]
            raise ProtocolError(f"server returned error: {detail}")
        if reply["type"] != MessageType.CLIENT_REPLY:
            raise ProtocolError("expected client_reply")
        identity_fields = ("client_id", "replica_id", "request_num", "request_id")
        if any(reply.get(field) != request[field] for field in identity_fields):
            raise ProtocolError("reply identifiers do not match request")

    async def send_increment(self) -> dict[str, Any]:
        await self.connect()
        if self._reader is None or self._writer is None:
            raise RuntimeError("connection was not established")

        request = build_client_request(
            self.client_id,
            self.endpoint.replica_id,
            self._next_request_num,
        )
        self.logger.sending(request, "request")
        try:
            await write_message(self._writer, request)
            reply = await read_message_with_timeout(self._reader, self.read_timeout)
            self._validate_reply(reply, request)
        except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError):
            await self._drop_connection()
            raise

        self.logger.received(reply, "reply")
        self._next_request_num += 1
        return reply

    async def close(self) -> None:
        await self._drop_connection()


async def run_interactive(
    client: ClientApp,
    input_fn: Callable[[str], str] = input,
) -> None:
    """Read user commands without blocking the asyncio event loop."""

    while True:
        command = (
            await asyncio.to_thread(input_fn, "increment or quit> ")
        ).strip().lower()
        if command in {"quit", "exit"}:
            return
        if command not in {"", "increment"}:
            client.logger.error(client.client_id, f"unknown command: {command}")
            continue
        try:
            await client.send_increment()
        except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError) as exc:
            client.logger.error(client.client_id, str(exc))
