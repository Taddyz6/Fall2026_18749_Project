"""Async server replica with isolated client and heartbeat listeners."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ft_system.common.config import PassiveConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    MessageType,
    ProtocolError,
    build_client_reply,
    build_error,
    build_heartbeat_ack,
    build_checkpoint,
)
from ft_system.common.transport import (
    MAX_MESSAGE_BYTES,
    close_writer,
    read_message,
    write_message,
)
from ft_system.common.retry import open_connection_with_timeout
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
        self._last_replies: dict[str, tuple[int, str, dict[str, Any]]] = {}

    async def _process(self, request: dict[str, Any]) -> dict[str, Any]:
        async with self._processing_lock:
            self._logger.received(request, "request")
            previous = self._last_replies.get(request["client_id"])
            if previous is not None and request["request_num"] <= previous[0]:
                if request["request_num"] == previous[0] and request["payload"]["operation"] == previous[1]:
                    self._logger.event(f"{self._replica_id}: replaying reply for {request['request_id']}")
                    self._logger.sending(previous[2], "reply")
                    return previous[2]
                return build_error(self._replica_id, request["client_id"], "stale_request",
                                   "request number is stale or was reused for another operation",
                                   request["request_id"])
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
            self._last_replies[request["client_id"]] = (
                request["request_num"], request["payload"]["operation"], reply
            )
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

    async def checkpoint_snapshot(self) -> dict[str, int]:
        async with self._processing_lock:
            return self._state_machine.snapshot()

    async def restore_checkpoint(self, state: dict[str, int]) -> None:
        async with self._processing_lock:
            self._state_machine.restore(state)


class CheckpointListener:
    """Receive state transfers on the primary-to-backup secondary channel."""

    def __init__(self, replica_id: str, client_listener: ClientListener, logger: EventLogger) -> None:
        self._replica_id = replica_id
        self._client_listener = client_listener
        self._logger = logger
        self.checkpoint_count = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    message = await read_message(reader)
                except EOFError:
                    return
                if (message["type"] != MessageType.CHECKPOINT
                        or message["destination"] != self._replica_id):
                    raise ProtocolError("checkpoint port accepts only checkpoints addressed to this replica")
                state = message["payload"]["state"]
                count = message["payload"]["checkpoint_count"]
                await self._client_listener.restore_checkpoint(state)
                self.checkpoint_count = count
                self._logger.checkpoint_received(self._replica_id, message["source"], state, count)
        except (ConnectionError, OSError, ProtocolError) as exc:
            self._logger.error(self._replica_id, f"checkpoint connection: {str(exc) or type(exc).__name__}")
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
        passive: PassiveConfig | None = None,
        all_servers: Iterable[ServerConfig] = (),
    ) -> None:
        self.config = config
        self.state_machine = DeterministicStateMachine(client_ids)
        self.client_listener = ClientListener(
            config.replica_id, self.state_machine, logger
        )
        self.heartbeat_listener = HeartbeatListener(config.replica_id, logger)
        self._logger = logger
        self._passive = passive
        self._servers_by_id = {server.replica_id: server for server in all_servers}
        self.checkpoint_listener = CheckpointListener(config.replica_id, self.client_listener, logger)
        self._client_server: asyncio.Server | None = None
        self._heartbeat_server: asyncio.Server | None = None
        self._checkpoint_server: asyncio.Server | None = None
        self._active_writers: set[asyncio.StreamWriter] = set()
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._checkpoint_writers: dict[str, asyncio.StreamWriter] = {}
        self._checkpoint_count = 0

    @property
    def bound_client_port(self) -> int:
        return self._bound_port(self._client_server, "client")

    @property
    def bound_heartbeat_port(self) -> int:
        return self._bound_port(self._heartbeat_server, "heartbeat")

    @property
    def bound_checkpoint_port(self) -> int:
        return self._bound_port(self._checkpoint_server, "checkpoint")

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
        if self._passive is not None:
            assert self.config.checkpoint_port is not None
            self._checkpoint_server = await asyncio.start_server(
                lambda reader, writer: self._serve_connection(self.checkpoint_listener.handle, reader, writer),
                self.config.bind_host, self.config.checkpoint_port, limit=MAX_MESSAGE_BYTES + 1,
            )
            if self.config.replica_id == self._passive.primary_id:
                self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())

    async def serve_forever(self) -> None:
        if self._client_server is None or self._heartbeat_server is None:
            raise RuntimeError("server is not started")
        servers = [
            self._client_server.serve_forever(),
            self._heartbeat_server.serve_forever(),
        ]
        if self._checkpoint_server is not None:
            servers.append(self._checkpoint_server.serve_forever())
        await asyncio.gather(*servers)

    async def _checkpoint_loop(self) -> None:
        assert self._passive is not None
        backups = tuple(server for server_id, server in self._servers_by_id.items()
                        if server_id != self.config.replica_id)
        try:
            while True:
                await asyncio.sleep(self._passive.checkpoint_freq)
                state = await self.client_listener.checkpoint_snapshot()
                count = self._checkpoint_count
                for backup in backups:
                    writer = self._checkpoint_writers.get(backup.replica_id)
                    if writer is None:
                        try:
                            _, writer = await open_connection_with_timeout(
                                backup.advertised_host, backup.checkpoint_port, self._passive.checkpoint_freq
                            )
                            self._checkpoint_writers[backup.replica_id] = writer
                        except (TimeoutError, ConnectionError, OSError) as exc:
                            self._logger.error(self.config.replica_id, f"checkpoint to {backup.replica_id} unavailable: {exc}")
                            continue
                    try:
                        message = build_checkpoint(self.config.replica_id, backup.replica_id, state, count)
                        await write_message(writer, message)
                        self._logger.checkpoint_sent(self.config.replica_id, backup.replica_id, state, count)
                    except (ConnectionError, OSError, ProtocolError) as exc:
                        self._logger.error(self.config.replica_id, f"checkpoint to {backup.replica_id} failed: {exc}")
                        self._checkpoint_writers.pop(backup.replica_id, None)
                        await close_writer(writer)
                self._checkpoint_count += 1
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        servers = [
            server
            for server in (self._client_server, self._heartbeat_server, self._checkpoint_server)
            if server is not None
        ]
        self._client_server = None
        self._heartbeat_server = None
        self._checkpoint_server = None
        if self._checkpoint_task is not None:
            self._checkpoint_task.cancel()
            await asyncio.gather(self._checkpoint_task, return_exceptions=True)
            self._checkpoint_task = None
        if self._checkpoint_writers:
            await asyncio.gather(*(close_writer(writer) for writer in self._checkpoint_writers.values()))
            self._checkpoint_writers.clear()
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
