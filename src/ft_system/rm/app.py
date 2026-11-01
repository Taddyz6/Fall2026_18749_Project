"""Replication Manager: membership from the GFD, readiness and roles for replicas."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ft_system.common.config import RmConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    MessageType, ProtocolError, build_rm_command, build_view,
)
from ft_system.common.transport import (
    MAX_MESSAGE_BYTES, close_writer, read_message, read_message_with_timeout, write_message,
)
from ft_system.rm.policy import ReplicationPolicy


@dataclass
class _Peer:
    writer: asyncio.StreamWriter
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RmApp:
    """Accept the GFD, replicas, and clients; push views and readiness commands."""

    def __init__(
        self,
        config: RmConfig,
        mode: str,
        server_ids: Iterable[str],
        client_ids: Iterable[str],
        logger: EventLogger,
    ) -> None:
        self.config = config
        self.logger = logger
        self.policy = ReplicationPolicy(mode)
        self._server_ids = frozenset(server_ids)
        self._client_ids = frozenset(client_ids)
        self._gfd: _Peer | None = None
        self._servers: dict[str, _Peer] = {}
        self._clients: dict[str, _Peer] = {}
        self._reported: dict[str, bool] = {}
        # (target, donor) -> loop time of the next attempt. donor None means set_ready.
        self._due: dict[tuple[str, str | None], float] = {}
        self._issued: set[tuple[str, str | None]] = set()
        self._waiting: set[tuple[str, str | None]] = set()
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None
        self._dispatcher: asyncio.Task | None = None

    @property
    def bound_port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    @property
    def membership(self) -> list[str]:
        return sorted(self.policy.members)

    @property
    def member_count(self) -> int:
        return len(self.policy.members)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.config.bind_host,
                                                 self.config.port, limit=MAX_MESSAGE_BYTES + 1)
        self.logger.event(f"RM: replication mode = {self.policy.mode}")
        self.logger.membership([], "RM")
        self._dispatcher = asyncio.create_task(self._dispatch())

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._tasks) + ([self._dispatcher] if self._dispatcher else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatcher = None

    # Sessions -----------------------------------------------------------

    def _register(self, hello: dict[str, Any], writer: asyncio.StreamWriter) -> tuple[str, str, _Peer]:
        if hello["type"] != MessageType.HELLO:
            raise ProtocolError("expected hello")
        kind, ident = hello["payload"]["kind"], hello["source"]
        peer = _Peer(writer)
        if kind == "gfd":
            if ident != "GFD":
                raise ProtocolError("the GFD must register as GFD")
            previous, self._gfd = self._gfd, peer
        elif kind == "server":
            if ident not in self._server_ids:
                raise ProtocolError(f"unknown replica {ident}")
            previous, self._servers[ident] = self._servers.get(ident), peer
        else:
            if ident not in self._client_ids:
                raise ProtocolError(f"unknown client {ident}")
            previous, self._clients[ident] = self._clients.get(ident), peer
        if previous is not None:
            # A restarted component replaces its stale session.
            previous.writer.close()
        self.logger.event(f"RM: {ident} registered")
        return kind, ident, peer

    def _unregister(self, kind: str, ident: str, peer: _Peer) -> None:
        if kind == "gfd" and self._gfd is peer:
            self._gfd = None
        elif kind == "server" and self._servers.get(ident) is peer:
            del self._servers[ident]
            self._reported.pop(ident, None)
        elif kind == "client" and self._clients.get(ident) is peer:
            del self._clients[ident]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        session = None
        try:
            hello = await read_message_with_timeout(reader, self.config.connect_timeout)
            session = kind, ident, peer = self._register(hello, writer)
            if kind != "gfd":
                await self._send(peer, build_view(ident, self.policy.view()))
            if kind == "server":
                self._retry_now(ident)
            while True:
                message = await read_message(reader)
                if kind == "gfd" and message["type"] == MessageType.MEMBERSHIP_REPORT:
                    await self._on_membership(message["payload"]["members"])
                elif (kind == "server" and message["type"] == MessageType.REPLICA_STATUS
                      and message["source"] == ident):
                    await self._on_status(ident, message["payload"]["ready"])
                else:
                    raise ProtocolError(f"unexpected {message['type']} from {ident}")
        except (EOFError, ConnectionError, OSError) as exc:
            # Crashed peers surface as EOF or as a reset, depending on the platform.
            if session is not None:
                reason = "" if isinstance(exc, EOFError) else f" ({str(exc) or type(exc).__name__})"
                self.logger.event(f"RM: {session[1]} disconnected{reason}")
            else:
                self.logger.error("RM", f"unregistered peer: {str(exc) or type(exc).__name__}")
        except (TimeoutError, ProtocolError) as exc:
            name = session[1] if session is not None else "unregistered peer"
            self.logger.error("RM", f"{name}: {str(exc) or type(exc).__name__}")
        finally:
            if session is not None:
                self._unregister(*session)
            await close_writer(writer)
            self._tasks.discard(task)

    async def _send(self, peer: _Peer, message: dict[str, Any]) -> bool:
        async with peer.lock:
            try:
                await asyncio.wait_for(write_message(peer.writer, message), self.config.connect_timeout)
            except (TimeoutError, ConnectionError, OSError):
                return False
            return True

    # Membership, status, and views ----------------------------------------

    async def _on_membership(self, members: list[str]) -> None:
        unknown = set(members) - self._server_ids
        if unknown:
            raise ProtocolError(f"GFD reported unknown replicas: {', '.join(sorted(unknown))}")
        self.logger.membership(sorted(members), "RM")
        before = self.policy.view()
        if self.policy.set_members(members, self._reported):
            await self._publish(before)
        self._sync_due()

    async def _on_status(self, replica_id: str, ready: bool) -> None:
        self._reported[replica_id] = ready
        before = self.policy.view()
        if self.policy.set_status(replica_id, ready):
            await self._publish(before)
        self._sync_due()

    async def _publish(self, before: dict[str, Any]) -> None:
        view = self.policy.view()
        for replica in sorted(set(view["ready"]) - set(before["ready"])):
            self.logger.event(f"RM: {replica} is ready (i_am_ready = 1)", "ready")
        for replica in sorted((set(before["ready"]) - set(view["ready"])) & set(view["members"])):
            self.logger.event(f"RM: {replica} reported i_am_ready = 0", "not_ready")
        old, new = before["primary"], view["primary"]
        if new != old:
            if new is None:
                self.logger.event("RM: no primary (no members)", "failed")
            elif old is None:
                self.logger.event(f"RM: primary is {new}", "recovered")
            elif old not in view["members"]:
                self.logger.event(f"RM: primary {old} failed; new primary is {new}", "recovered")
            else:
                self.logger.event(f"RM: primary changed from {old} to {new}", "recovered")
        self.logger.view("RM", view)
        peers = [*self._servers.items(), *self._clients.items()]
        await asyncio.gather(*(self._send(peer, build_view(ident, view)) for ident, peer in peers))

    # Readiness commands ---------------------------------------------------

    def _sync_due(self) -> None:
        now = asyncio.get_running_loop().time()
        wanted = set(self.policy.assignments().items())
        for key in set(self._due) - wanted:
            del self._due[key]
            self._issued.discard(key)
            self._waiting.discard(key)
        for key in wanted - set(self._due):
            # Delaying a transfer keeps the new replica's not-ready logging visible in demos.
            self._due[key] = now + (self.config.transfer_delay if key[1] is not None else 0)
        self._wake.set()

    def _retry_now(self, replica_id: str) -> None:
        now = asyncio.get_running_loop().time()
        for target, donor in self._due:
            if replica_id in (target, donor):
                self._due[(target, donor)] = min(self._due[(target, donor)], now)
        self._wake.set()

    async def _dispatch(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self._wake.clear()
            for key, due in list(self._due.items()):
                if due <= loop.time() and key in self._due:
                    self._due[key] = loop.time() + self.config.transfer_retry
                    await self._issue(*key)
            delay = min(self._due.values(), default=loop.time() + 3600) - loop.time()
            try:
                await asyncio.wait_for(self._wake.wait(), max(delay, 0))
            except TimeoutError:
                pass

    async def _issue(self, target: str, donor: str | None) -> None:
        key = (target, donor)
        if donor is None:
            recipient, message = target, build_rm_command(target, "set_ready")
            if self.policy.ever_ready:
                text = f"no ready replica holds state; asking {target} to set i_am_ready = 1 with its own state"
            else:
                text = f"{target} is the first replica; asking it to set i_am_ready = 1"
        else:
            recipient, message = donor, build_rm_command(donor, "transfer_state", target)
            text = f"asking {donor} to transfer its state to {target}"
        peer = self._servers.get(recipient)
        if peer is None:
            if key not in self._waiting:
                self._waiting.add(key)
                self.logger.event(f"RM: waiting for {recipient} to register, then {text}")
            return
        if await self._send(peer, message):
            self.logger.event(f"RM: {text}" + (" (retry)" if key in self._issued else ""), "sending")
            self._issued.add(key)
