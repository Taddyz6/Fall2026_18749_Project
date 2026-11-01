"""Async server replica with isolated client, heartbeat, and checkpoint listeners."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from typing import Any

from ft_system.common.config import ReplicationConfig, RmConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    MessageType,
    ProtocolError,
    build_checkpoint,
    build_client_reply,
    build_client_request,
    build_error,
    build_heartbeat_ack,
    build_replica_status,
    encode_message,
)
from ft_system.common.retry import open_connection_with_timeout
from ft_system.common.rm_link import RmLink
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

PEER_TIMEOUT = 1.0
BACKLOG_LIMIT = 1000


async def _flush(writers: Iterable[asyncio.StreamWriter]) -> None:
    for writer in set(writers):
        with suppress(ConnectionError, OSError):
            await writer.drain()


class ClientListener:
    """Serve application requests without owning network-listener lifecycle.

    ``ready`` is the replica's i_am_ready flag. While it is false, requests are
    logged and kept in arrival order instead of answered; becoming ready replays
    them. ``role`` is "active", "primary", or "backup"; backups refuse requests.
    """

    def __init__(
        self,
        replica_id: str,
        state_machine: DeterministicStateMachine,
        logger: EventLogger,
        *,
        ready: bool = True,
        role: str = "active",
        strict_order: bool = False,
        on_ready_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._replica_id = replica_id
        self._state_machine = state_machine
        self._logger = logger
        self._processing_lock = asyncio.Lock()
        self._last_replies: dict[str, tuple[int, str, dict[str, Any]]] = {}
        self.ready = ready
        self.role = role
        self.primary_hint: str | None = None
        # Active replicas with an RM treat a skipped request number as proof they
        # missed requests that other replicas applied, and ask for fresh state.
        self.strict_order = strict_order
        self.on_ready_change = on_ready_change
        self.high_watermark: dict[str, int] = {}
        self._backlog: list[tuple[dict[str, Any], asyncio.StreamWriter]] = []

    def _last_num(self, client_id: str) -> int:
        previous = self._last_replies.get(client_id)
        return 0 if previous is None else previous[0]

    def _set_ready(self, ready: bool, reason: str) -> None:
        self.ready = ready
        self._logger.readiness(self._replica_id, ready, reason)
        if self.on_ready_change is not None:
            self.on_ready_change(ready)

    def _apply(self, request: dict[str, Any]) -> dict[str, Any]:
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

    def _log_unready(self, request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        client_id = request["client_id"]
        watermark = max(self.high_watermark.get(client_id, 0), request["request_num"])
        self.high_watermark[client_id] = watermark
        self._logger.logged_unready(self._replica_id, request, watermark)
        if len(self._backlog) >= BACKLOG_LIMIT:
            # A later checkpoint covers what is dropped, or the gap check rejects it.
            self._backlog.pop(0)
            self._logger.error(self._replica_id, "request log is full; dropped the oldest entry")
        self._backlog.append((request, writer))

    async def _process(
        self, request: dict[str, Any], writer: asyncio.StreamWriter
    ) -> dict[str, Any] | None:
        async with self._processing_lock:
            self._logger.received(request, "request")
            if self.role == "backup":
                return build_error(self._replica_id, request["client_id"], "not_primary",
                                   self.primary_hint or "unknown", request["request_id"])
            client_id, number = request["client_id"], request["request_num"]
            last = self._last_num(client_id)
            if self.ready and number > last + 1:
                if self.strict_order:
                    self._set_ready(False, f"missed {client_id} request_num {last + 1}-{number - 1}; "
                                           "need state from a ready replica")
                elif self.role == "primary":
                    self._logger.event(
                        f"{self._replica_id}: {client_id} request_num {last + 1}-{number - 1} were applied "
                        "after the last checkpoint by the failed primary and are lost", "failed")
            if not self.ready:
                self._log_unready(request, writer)
                return None
            return self._apply(request)

    def _first_gap(self, last_request_nums: dict[str, int]) -> tuple[str, int, int] | None:
        expected = dict(last_request_nums)
        for request, _ in self._backlog:
            client_id, number = request["client_id"], request["request_num"]
            if number > expected.get(client_id, 0) + 1:
                return client_id, expected.get(client_id, 0), number
            expected[client_id] = max(expected.get(client_id, 0), number)
        return None

    def _rebuild_replies(self, last_request_nums: dict[str, int]) -> None:
        state = self._state_machine.snapshot()
        self._last_replies = {}
        for client_id, number in last_request_nums.items():
            if number:
                request = build_client_request(client_id, self._replica_id, number)
                # Only a client's own requests change its slot, so the slot holds the
                # value returned for that client's last applied request.
                self._last_replies[client_id] = (
                    number, "increment", build_client_reply(request, state[client_id])
                )

    def _activate(self, reason: str) -> list[asyncio.StreamWriter]:
        """Set i_am_ready = 1 and answer the logged requests the state does not yet include."""

        self._set_ready(True, reason)
        backlog, self._backlog = self._backlog, []
        if backlog:
            self._logger.event(f"{self._replica_id}: replaying {len(backlog)} logged requests")
        writers = []
        for request, writer in backlog:
            if request["request_num"] < self._last_num(request["client_id"]):
                continue  # Already in the installed state; the client was answered long ago.
            reply = self._apply(request)
            if writer.is_closing():
                continue
            try:
                # Write while holding the lock so later replies on this connection stay behind.
                writer.write(encode_message(reply))
            except (RuntimeError, ConnectionError, OSError):
                continue
            writers.append(writer)
        return writers

    async def install_checkpoint(self, state: dict[str, int], last_request_nums: dict[str, int]) -> bool:
        """Install donor state; an unready replica then replays its log and becomes ready."""

        async with self._processing_lock:
            if not self.ready:
                gap = self._first_gap(last_request_nums)
                if gap is not None:
                    client_id, covered, number = gap
                    self._logger.event(
                        f"{self._replica_id}: checkpoint covers {client_id} only through request_num {covered}, "
                        f"but the log continues at {number}; waiting for a newer checkpoint", "not_ready")
                    return False
            self._state_machine.restore(state)
            self._rebuild_replies(last_request_nums)
            writers = [] if self.ready else self._activate("checkpoint installed")
        await _flush(writers)
        return True

    async def mark_ready(self) -> bool:
        """Become ready with the current state (first replica, or no donor left)."""

        async with self._processing_lock:
            if self.ready:
                return False
            writers = self._activate("set by the RM")
        await _flush(writers)
        return True

    async def checkpoint_payload(self) -> tuple[dict[str, int], dict[str, int]]:
        async with self._processing_lock:
            state = self._state_machine.snapshot()
            return state, {client_id: self._last_num(client_id) for client_id in state}

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

                response = await self._process(message, writer)
                if response is not None:
                    await write_message(writer, response)
        except (ConnectionError, OSError) as exc:
            self._logger.error(self._replica_id, f"client connection closed: {exc}")
        finally:
            await close_writer(writer)


class CheckpointListener:
    """Receive checkpoints on the replica-to-replica secondary channel."""

    def __init__(
        self,
        replica_id: str,
        on_checkpoint: Callable[[dict[str, Any]], Awaitable[None]],
        logger: EventLogger,
    ) -> None:
        self._replica_id = replica_id
        self._on_checkpoint = on_checkpoint
        self._logger = logger

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
                await self._on_checkpoint(message)
        except (ConnectionError, OSError, ProtocolError, StateMachineError) as exc:
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
    """Own the listener sockets, the secondary channel, and the replica's role.

    Without an RM the replica is ready at start (Milestones 1-3), and a passive
    primary comes from the configuration. With an RM the replica starts with
    i_am_ready = 0 and follows the RM's views and commands.
    """

    def __init__(
        self,
        config: ServerConfig,
        client_ids: Iterable[str],
        logger: EventLogger,
        replication: ReplicationConfig | None = None,
        all_servers: Iterable[ServerConfig] = (),
        rm: RmConfig | None = None,
    ) -> None:
        self.config = config
        self.state_machine = DeterministicStateMachine(client_ids)
        self._logger = logger
        self._replication = replication
        self._passive = replication is not None and replication.mode == "passive"
        self._servers_by_id = {server.replica_id: server for server in all_servers}
        if rm is not None:
            ready, role = False, "backup" if self._passive else "active"
        elif self._passive:
            ready, role = True, "primary" if config.replica_id == replication.primary_id else "backup"
        else:
            ready, role = True, "active"
        self.client_listener = ClientListener(
            config.replica_id, self.state_machine, logger, ready=ready, role=role,
            strict_order=rm is not None and not self._passive, on_ready_change=self._ready_changed,
        )
        self.heartbeat_listener = HeartbeatListener(config.replica_id, logger)
        self.checkpoint_listener = CheckpointListener(config.replica_id, self._receive_checkpoint, logger)
        self._rm_link = (RmLink(config.replica_id, "server", rm, logger, self._on_rm_message, self._report_status)
                         if rm is not None else None)
        self._view: dict[str, Any] | None = None
        self.checkpoint_count = 1  # Number of the next checkpoint this replica sends.
        self.received_checkpoint_count = 0
        self._client_server: asyncio.Server | None = None
        self._heartbeat_server: asyncio.Server | None = None
        self._checkpoint_server: asyncio.Server | None = None
        self._active_writers: set[asyncio.StreamWriter] = set()
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._peer_writers: dict[str, asyncio.StreamWriter] = {}
        # One lock per peer, so an unreachable replica cannot delay checkpoints to the others.
        self._peer_locks: dict[str, asyncio.Lock] = {}

    @property
    def bound_client_port(self) -> int:
        return self._bound_port(self._client_server, "client")

    @property
    def bound_heartbeat_port(self) -> int:
        return self._bound_port(self._heartbeat_server, "heartbeat")

    @property
    def bound_checkpoint_port(self) -> int:
        return self._bound_port(self._checkpoint_server, "checkpoint")

    @property
    def role(self) -> str:
        return self.client_listener.role

    @staticmethod
    def _bound_port(server: asyncio.Server | None, name: str) -> int:
        if server is None or not server.sockets:
            raise RuntimeError(f"{name} listener is not started")
        return int(server.sockets[0].getsockname()[1])

    def _spawn(self, coroutine: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

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
            if self._replication is not None:
                assert self.config.checkpoint_port is not None
                self._checkpoint_server = await asyncio.start_server(
                    lambda reader, writer: self._serve_connection(self.checkpoint_listener.handle, reader, writer),
                    self.config.bind_host, self.config.checkpoint_port, limit=MAX_MESSAGE_BYTES + 1,
                )
        except BaseException:
            for server in (self._client_server, self._heartbeat_server):
                if server is not None:
                    server.close()
                    await server.wait_closed()
            self._client_server = self._heartbeat_server = None
            raise
        if self._rm_link is not None:
            self._logger.readiness(self.config.replica_id, False, "waiting for the RM")
            self._spawn(self._rm_link.run())
        elif self.role == "primary":
            self._start_checkpointing()

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

    # RM coordination ------------------------------------------------------

    def _ready_changed(self, ready: bool) -> None:
        self._spawn(self._report_status())
        if ready and not self._passive and self._view is not None:
            self._spawn(self._open_peer_channels())

    async def _report_status(self) -> None:
        if self._rm_link is not None:
            await self._rm_link.send(build_replica_status(
                self.config.replica_id, self.client_listener.ready, self.received_checkpoint_count))

    async def _on_rm_message(self, message: dict[str, Any]) -> None:
        if message["type"] == MessageType.VIEW:
            self._apply_view(message["payload"])
        elif message["type"] == MessageType.RM_COMMAND:
            if message["payload"]["command"] == "set_ready":
                if not await self.client_listener.mark_ready():
                    await self._report_status()
            else:
                self._spawn(self._transfer_state(message["payload"]["target"]))
        else:
            raise ProtocolError(f"unexpected {message['type']} from RM")

    def _apply_view(self, view: dict[str, Any]) -> None:
        me = self.config.replica_id
        first = self._view is None
        self._view = view
        self.client_listener.primary_hint = view["primary"]
        self._logger.view(me, view)
        for peer in set(self._peer_writers) - set(view["members"]):
            self._spawn(self._close_peer(peer))
        if self._passive:
            role = "primary" if view["primary"] == me else "backup"
            if role == self.role and not first:
                return
            self.client_listener.role = role
            if role == "primary":
                # Continue the checkpoint numbering the previous primary started.
                self.checkpoint_count = max(self.checkpoint_count, self.received_checkpoint_count + 1)
                self._logger.event(f"{me}: role = primary", "recovered")
                self._start_checkpointing()
            else:
                self._logger.event(f"{me}: role = backup (primary is {view['primary'] or 'not elected'})")
                self._stop_checkpointing()
        elif self.client_listener.ready:
            self._spawn(self._open_peer_channels())

    # Secondary channel ----------------------------------------------------

    def _checkpoint_targets(self) -> list[str]:
        me = self.config.replica_id
        if self._view is not None:
            return [member for member in self._view["members"] if member != me]
        return [replica for replica in self._servers_by_id if replica != me]

    def _peer_lock(self, peer: str) -> asyncio.Lock:
        return self._peer_locks.setdefault(peer, asyncio.Lock())

    async def _peer_writer(self, peer: str) -> asyncio.StreamWriter | None:
        """Return an open secondary-channel writer; the caller holds the peer's lock."""

        me = self.config.replica_id
        writer = self._peer_writers.get(peer)
        if writer is not None and not writer.is_closing():
            return writer
        server = self._servers_by_id.get(peer)
        if server is None or server.checkpoint_port is None:
            self._logger.error(me, f"no checkpoint address for {peer}")
            return None
        try:
            _, writer = await open_connection_with_timeout(
                server.advertised_host, server.checkpoint_port, PEER_TIMEOUT)
        except (TimeoutError, ConnectionError, OSError) as exc:
            self._logger.error(me, f"checkpoint to {peer} unavailable: {str(exc) or type(exc).__name__}")
            return None
        self._peer_writers[peer] = writer
        self._logger.event(f"{me}: secondary channel to {peer} open")
        return writer

    async def _open_peer_channel(self, peer: str) -> None:
        async with self._peer_lock(peer):
            await self._peer_writer(peer)

    async def _open_peer_channels(self) -> None:
        await asyncio.gather(*(self._open_peer_channel(peer) for peer in self._checkpoint_targets()))

    async def _close_peer(self, peer: str) -> None:
        async with self._peer_lock(peer):
            writer = self._peer_writers.pop(peer, None)
        await close_writer(writer)

    async def _send_checkpoint(
        self, peer: str, state: dict[str, int], last_request_nums: dict[str, int], count: int
    ) -> bool:
        me = self.config.replica_id
        async with self._peer_lock(peer):
            writer = await self._peer_writer(peer)
            if writer is None:
                return False
            try:
                message = build_checkpoint(me, peer, state, last_request_nums, count)
                await asyncio.wait_for(write_message(writer, message), PEER_TIMEOUT)
            except (TimeoutError, ConnectionError, OSError) as exc:
                self._logger.error(me, f"checkpoint to {peer} failed: {str(exc) or type(exc).__name__}")
                self._peer_writers.pop(peer, None)
                await close_writer(writer)
                return False
        self._logger.checkpoint_sent(me, peer, state, count)
        return True

    def _start_checkpointing(self) -> None:
        if self._checkpoint_task is None or self._checkpoint_task.done():
            self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())

    def _stop_checkpointing(self) -> None:
        if self._checkpoint_task is not None:
            self._checkpoint_task.cancel()
            self._checkpoint_task = None

    async def _checkpoint_loop(self) -> None:
        assert self._replication is not None
        while True:
            await asyncio.sleep(self._replication.checkpoint_freq)
            if not self.client_listener.ready:
                continue
            state, last_request_nums = await self.client_listener.checkpoint_payload()
            count = self.checkpoint_count
            sent = await asyncio.gather(*(self._send_checkpoint(peer, state, last_request_nums, count)
                                          for peer in self._checkpoint_targets()))
            if any(sent):
                self.checkpoint_count += 1

    async def _transfer_state(self, target: str) -> None:
        me = self.config.replica_id
        if not self.client_listener.ready:
            self._logger.error(me, f"cannot transfer state to {target}: i_am_ready = 0")
            return
        self._logger.event(f"{me}: RM requested a state transfer to {target}")
        state, last_request_nums = await self.client_listener.checkpoint_payload()
        count, self.checkpoint_count = self.checkpoint_count, self.checkpoint_count + 1
        await self._send_checkpoint(target, state, last_request_nums, count)

    async def _receive_checkpoint(self, message: dict[str, Any]) -> None:
        me = self.config.replica_id
        source, payload = message["source"], message["payload"]
        count = payload["checkpoint_count"]
        self._logger.checkpoint_received(me, source, payload["state"], count)
        listener = self.client_listener
        if self.role == "primary":
            self._logger.event(f"{me}: ignoring checkpoint from {source}; this replica is the primary")
            return
        if self._passive and self._view is not None and source != self._view["primary"]:
            self._logger.event(f"{me}: ignoring checkpoint from {source}; the primary is {self._view['primary']}")
            return
        if not self._passive and listener.ready:
            # A ready active replica may already hold requests the snapshot lacks.
            self._logger.event(f"{me}: already ready; ignoring checkpoint from {source}")
            await self._report_status()
            return
        if await listener.install_checkpoint(payload["state"], payload["last_request_nums"]):
            self.received_checkpoint_count = count

    async def close(self) -> None:
        servers = [
            server
            for server in (self._client_server, self._heartbeat_server, self._checkpoint_server)
            if server is not None
        ]
        self._client_server = None
        self._heartbeat_server = None
        self._checkpoint_server = None
        background = list(self._background)
        if self._checkpoint_task is not None:
            background.append(self._checkpoint_task)
            self._checkpoint_task = None
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        peers = list(self._peer_writers.values())
        self._peer_writers.clear()
        await asyncio.gather(*(close_writer(writer) for writer in peers))
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
