"""Async server replica with isolated client and heartbeat listeners."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ft_system.common.config import ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    MessageType,
    ProtocolError,
    build_client_reply,
    build_error,
    build_heartbeat_ack,
)
from ft_system.common.transport import (
    MAX_MESSAGE_BYTES,
    close_writer,
    read_message,
    write_message,
)
from ft_system.server.state_machine import (
    DeterministicStateMachine,
    StateMachineError,
)


ConnectionHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


class ClientListener:
    """Serve application requests without owning network-listener lifecycle."""

    def __init__(
        self,
        replica_id: str,
        state_machine: DeterministicStateMachine,
        logger: EventLogger,
    ) -> None:
        self._replica_id = replica_id
        self._state_machine = state_machine
        self._logger = logger
        self._processing_lock = asyncio.Lock()

    async def _process(self, request: dict[str, Any]) -> dict[str, Any]:
        async with self._processing_lock:
            self._logger.received(request, "request")
            before = self._state_machine.snapshot()
            self._logger.state(self._replica_id, before, "before", request)
            try:
                transition = self._state_machine.apply(
                    request["client_id"], request["payload"]["operation"]
                )
            except StateMachineError as exc:
                self._logger.error(self._replica_id, str(exc))
                code = (
                    "unsupported_operation"
                    if "unsupported operation" in str(exc)
                    else "state_machine_error"
                )
                return build_error(
                    source=self._replica_id,
                    destination=request["client_id"],
                    code=code,
                    detail=str(exc),
                    request_id=request["request_id"],
                )

            self._logger.state(
                self._replica_id, transition.after, "after", request
            )
            reply = build_client_reply(request, transition.client_value)
            self._logger.sending(reply, "reply")
            return reply

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    message = await read_message(reader)
                except EOFError:
                    return
                except ProtocolError as exc:
                    self._logger.error(self._replica_id, str(exc))
                    return

                if (
                    message["type"] != MessageType.CLIENT_REQUEST
                    or message["destination"] != self._replica_id
                ):
                    error = build_error(
                        source=self._replica_id,
                        destination=message["source"],
                        code="invalid_client_message",
                        detail="client port accepts only requests addressed to this replica",
                        request_id=message.get("request_id"),
                    )
                    await write_message(writer, error)
                    return

                response = await self._process(message)
                await write_message(writer, response)
        except (ConnectionError, OSError) as exc:
            self._logger.error(self._replica_id, f"client connection closed: {exc}")
        finally:
            await close_writer(writer)


class HeartbeatListener:
    """Serve heartbeat traffic without access to application state."""

    def __init__(self, replica_id: str, logger: EventLogger) -> None:
        self._replica_id = replica_id
        self._logger = logger

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    heartbeat = await read_message(reader)
                except EOFError:
                    return
                except ProtocolError as exc:
                    self._logger.error(self._replica_id, str(exc))
                    return

                if (
                    heartbeat["type"] != MessageType.HEARTBEAT
                    or heartbeat["destination"] != self._replica_id
                ):
                    self._logger.error(
                        self._replica_id,
                        "heartbeat port received an invalid message",
                    )
                    return

                count = heartbeat["heartbeat_count"]
                lfd_id = heartbeat["source"]
                self._logger.server_heartbeat_received(
                    self._replica_id, lfd_id, count
                )
                ack = build_heartbeat_ack(heartbeat)
                self._logger.server_heartbeat_ack_sent(
                    self._replica_id, lfd_id, count
                )
                await write_message(writer, ack)
        except (ConnectionError, OSError) as exc:
            self._logger.error(self._replica_id, f"heartbeat connection closed: {exc}")
        finally:
            await close_writer(writer)


class ServerApp:
    """Own both listener sockets and all live server-side connections."""

    def __init__(
        self,
        config: ServerConfig,
        client_ids: Iterable[str],
        logger: EventLogger,
    ) -> None:
        self.config = config
        self.state_machine = DeterministicStateMachine(client_ids)
        self.client_listener = ClientListener(
            config.replica_id, self.state_machine, logger
        )
        self.heartbeat_listener = HeartbeatListener(config.replica_id, logger)
        self._client_server: asyncio.Server | None = None
        self._heartbeat_server: asyncio.Server | None = None
        self._active_writers: set[asyncio.StreamWriter] = set()
        self._handler_tasks: set[asyncio.Task[None]] = set()

    @property
    def bound_client_port(self) -> int:
        return self._bound_port(self._client_server, "client")

    @property
    def bound_heartbeat_port(self) -> int:
        return self._bound_port(self._heartbeat_server, "heartbeat")

    @staticmethod
    def _bound_port(server: asyncio.Server | None, name: str) -> int:
        if server is None or not server.sockets:
            raise RuntimeError(f"{name} listener is not started")
        return int(server.sockets[0].getsockname()[1])

    async def _serve_connection(
        self,
        handler: ConnectionHandler,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
        self._active_writers.add(writer)
        try:
            await handler(reader, writer)
        finally:
            self._active_writers.discard(writer)
            if task is not None:
                self._handler_tasks.discard(task)

    async def start(self) -> None:
        if self._client_server is not None or self._heartbeat_server is not None:
            raise RuntimeError("server is already started")
        self._client_server = await asyncio.start_server(
            lambda reader, writer: self._serve_connection(
                self.client_listener.handle, reader, writer
            ),
            self.config.bind_host,
            self.config.client_port,
            limit=MAX_MESSAGE_BYTES + 1,
        )
        try:
            self._heartbeat_server = await asyncio.start_server(
                lambda reader, writer: self._serve_connection(
                    self.heartbeat_listener.handle, reader, writer
                ),
                self.config.bind_host,
                self.config.heartbeat_port,
                limit=MAX_MESSAGE_BYTES + 1,
            )
        except BaseException:
            self._client_server.close()
            await self._client_server.wait_closed()
            self._client_server = None
            raise

    async def serve_forever(self) -> None:
        if self._client_server is None or self._heartbeat_server is None:
            raise RuntimeError("server is not started")
        await asyncio.gather(
            self._client_server.serve_forever(),
            self._heartbeat_server.serve_forever(),
        )

    async def close(self) -> None:
        servers = [
            server
            for server in (self._client_server, self._heartbeat_server)
            if server is not None
        ]
        self._client_server = None
        self._heartbeat_server = None
        for server in servers:
            server.close()

        writers = tuple(self._active_writers)
        if writers:
            await asyncio.gather(*(close_writer(writer) for writer in writers))

        current = asyncio.current_task()
        tasks = tuple(task for task in self._handler_tasks if task is not current)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if servers:
            await asyncio.gather(*(server.wait_closed() for server in servers))
