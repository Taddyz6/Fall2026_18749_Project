"""Track replica health reported over registered LFD connections."""

import asyncio

from ft_system.common.config import GfdConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import MessageType, ProtocolError, build_heartbeat
from ft_system.common.transport import (
    MAX_MESSAGE_BYTES, close_writer, read_message_with_timeout, write_message,
)


class GfdApp:
    def __init__(self, config: GfdConfig, lfd_servers: dict[str, str], logger: EventLogger):
        self.config = config
        self.lfd_servers = lfd_servers
        self.logger = logger
        self._members: set[str] = set()
        self._sessions: dict[str, asyncio.StreamWriter] = {}
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._server = None

    @property
    def membership(self) -> list[str]:
        return sorted(self._members)

    @property
    def member_count(self) -> int:
        return len(self._members)

    @property
    def bound_port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    def _update(self, lfd_id: str, healthy: bool) -> None:
        replica = self.lfd_servers[lfd_id]
        if healthy == (replica in self._members):
            return
        action = "add" if healthy else "delete"
        self.logger.event(f"{lfd_id}: {action} replica {replica}")
        if healthy:
            self._members.add(replica)
        else:
            self._members.discard(replica)
        self.logger.membership(self.membership)

    def _validate_report(self, message: dict, lfd_id: str) -> None:
        if (message["source"] != lfd_id or message["destination"] != "GFD"
                or message["payload"].get("replica_id") != self.lfd_servers[lfd_id]):
            raise ProtocolError("LFD report does not match its configured replica")

    async def _handle(self, reader, writer) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(writer)
        lfd_id = None
        try:
            register = await read_message_with_timeout(reader, self.config.read_timeout)
            candidate = register["source"]
            if register["type"] != MessageType.REGISTER or candidate not in self.lfd_servers:
                raise ProtocolError("expected registration from a configured LFD")
            self._validate_report(register, candidate)
            if candidate in self._sessions:
                raise ProtocolError("LFD already has a registered connection")
            lfd_id = candidate
            self._sessions[lfd_id] = writer
            self.logger.event(f"GFD: registered {lfd_id}")
            self._update(lfd_id, register["payload"]["healthy"])
            count = 1
            # Membership events share this stream with heartbeat replies. A fixed
            # deadline prevents unrelated events from extending the ACK timeout.
            while True:
                self.logger.heartbeat_sent("GFD", lfd_id, count)
                await asyncio.wait_for(write_message(writer, build_heartbeat("GFD", lfd_id, count)),
                                       self.config.read_timeout)
                deadline = asyncio.get_running_loop().time() + self.config.read_timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError("LFD heartbeat timeout")
                    message = await read_message_with_timeout(reader, remaining)
                    if message["type"] == MessageType.MEMBERSHIP:
                        self._validate_report(message, lfd_id)
                        self._update(lfd_id, message["payload"]["healthy"])
                    elif (message["type"] == MessageType.HEARTBEAT_ACK
                          and message["source"] == lfd_id and message["destination"] == "GFD"
                          and message["heartbeat_count"] == count):
                        self.logger.heartbeat_ack("GFD", lfd_id, count)
                        break
                    else:
                        raise ProtocolError("unexpected LFD heartbeat reply")
                # Read membership changes immediately between heartbeat rounds.
                next_heartbeat = asyncio.get_running_loop().time() + self.config.heartbeat_freq
                while True:
                    remaining = next_heartbeat - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        message = await read_message_with_timeout(reader, remaining)
                    except TimeoutError:
                        break
                    if message["type"] != MessageType.MEMBERSHIP:
                        raise ProtocolError("expected membership event")
                    self._validate_report(message, lfd_id)
                    self._update(lfd_id, message["payload"]["healthy"])
                count += 1
        except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError) as exc:
            self.logger.error("GFD", f"{lfd_id or 'unregistered LFD'}: {str(exc) or type(exc).__name__}")
        finally:
            if lfd_id is not None and self._sessions.get(lfd_id) is writer:
                del self._sessions[lfd_id]
                self._update(lfd_id, False)
            await close_writer(writer)
            self._writers.discard(writer)
            self._tasks.discard(task)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.config.bind_host,
                                                 self.config.port, limit=MAX_MESSAGE_BYTES + 1)
        self.logger.membership(self.membership)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(close_writer(w) for w in list(self._writers)))
